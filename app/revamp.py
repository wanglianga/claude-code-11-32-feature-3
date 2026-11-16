"""Two-period revamp comparison orchestration.

A *comparison* pins two period inputs (recruitment / target / program / diary
cutoff for each), one editor-confirmed program mapping, one **fixed reference
population** both periods are calibrated to, and a configuration. It produces,
never as a single causal sentence:

* each period's own valid-sample estimate (calibrated to the fixed reference
  population, with the period's own-population variant alongside);
* the cross-section change under that common reference;
* a composition/behaviour accounting split whose order is stored with the
  report;
* a paired change among people eligible in both periods, reweighted by
  inclusion and retention probabilities and calibrated back to the same
  reference population;
* the groups each period covers poorly, and the part the paired design cannot
  cover at all (frame absence);
* new and discontinued items listed separately from comparable change.

Corrections are period-scoped: a corrected input for one period creates a new
comparison whose parent is the earlier one. Earlier published reports are never
updated.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analysis import (
    analyze_person,
    build_frame,
    day_windows,
    program_exposure,
    survey_days,
)
from .analytics.estimate import _percentile
from .analytics.raking import RakingConfig, rake
from .analytics.revamp import (
    cell_key,
    paired_base_weights,
    retention_table,
)
from .config import Settings
from .db import Database
from .errors import conflict, invalid, not_found
from .ingest import load_programs, load_recruitment, load_submissions, load_targets
from .jsonutil import canonical_hash, jb, loads
from .revamp_ingest import load_mapping
from .runs import _load_resolutions
from .timeutil import UTC, iso

PERIODS = ("before", "after")
STAGES = [
    "validate_inputs",
    "build_periods",
    "paired",
    "bootstrap",
    "estimate",
    "publish_report",
]
DIMENSIONS = ("age_band", "region", "sex")
DEFAULT_DIARY_WINDOW = {"start_local": "00:00", "end_local": "24:00"}

REPORT_INTERPRETATION = {
    "comparable_change": (
        "A change under a common fixed reference population. It is not causal attribution: programme "
        "content, schedule, sample composition and coverage all move together between the two periods."
    ),
    "never_summarise_as": "revamp improved (or reduced) listening",
    "cross_section_knows_identities": False,
    "decomposition_is_accounting": True,
    "new_and_discontinued_are_listed_separately": True,
}


# ---------------------------------------------------------------------------
# Serialisation / loading helpers
# ---------------------------------------------------------------------------
def serialize_comparison(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["comparison_id"] = str(out["comparison_id"])
    if out.get("parent_comparison_id"):
        out["parent_comparison_id"] = str(out["parent_comparison_id"])
    for key in (
        "periods",
        "config",
        "correction",
        "decomposition_order",
        "weight_version",
        "calibration",
        "paired",
        "bootstrap",
        "failure",
    ):
        if key in out and out[key] is not None:
            out[key] = loads(out[key])
    for key in ("created_at", "updated_at", "finished_at", "published_at"):
        if out.get(key) is not None:
            out[key] = iso(out[key])
    return out


def fetch_comparison(cur, comparison_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM revamp_comparisons WHERE comparison_id=%s", (str(comparison_id),))
    row = cur.fetchone()
    if row is None:
        raise not_found("revamp_comparison_not_found", "comparison does not exist")
    return serialize_comparison(row)


def _row(cur, comparison_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM revamp_comparisons WHERE comparison_id=%s", (str(comparison_id),))
    row = cur.fetchone()
    if row is None:
        raise not_found("revamp_comparison_not_found", "comparison does not exist")
    return row


def _config(row) -> dict[str, Any]:
    return loads(row["config"])


def _tz(row) -> ZoneInfo:
    name = _config(row)["timezone"]
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover
        raise invalid("timezone_unknown", "unknown timezone", {"timezone": name}) from exc


def _raking_config(config: dict[str, Any]) -> RakingConfig:
    r = config["raking"]
    return RakingConfig(
        tolerance=float(r["tolerance"]),
        max_iterations=int(r["max_iterations"]),
        lower_factor=float(r["lower_factor"]),
        upper_factor=float(r["upper_factor"]),
        dimensions=tuple(r["dimensions"]),
    )


def _max_diary_seq(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
    return int(cur.fetchone()["m"])


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_comparison(db: Database, settings: Settings, body: dict[str, Any], actor: str) -> dict[str, Any]:
    from .models import RevampConfigIn

    config = RevampConfigIn.model_validate(body.get("config") or {}).model_dump(mode="json")
    seed = int(body.get("seed") if body.get("seed") is not None else config["bootstrap"]["seed"])
    periods_in = body["periods"]
    correction = body.get("correction")

    with db.scoped() as conn:
        cur = conn.cursor()
        mapping = load_mapping(cur, int(body["mapping_seq"]))
        reference = load_targets(cur, int(body["reference_target_batch_seq"]))
        reference_margins = loads(reference["dimensions"])
        _require_dimensions(reference_margins, config)

        if int(periods_in["before"]["recruitment_batch_seq"]) == int(
            periods_in["after"]["recruitment_batch_seq"]
        ):
            raise invalid(
                "revamp_same_frame",
                "the two periods must come from distinct recruitment batches; one frame cannot be its own comparison",
            )

        plans: dict[str, dict[str, Any]] = {}
        program_tz = None
        for period in PERIODS:
            pin = {k: int(v) for k, v in periods_in[period].items()}
            recruitment = load_recruitment(cur, pin["recruitment_batch_seq"])
            targets = load_targets(cur, pin["target_batch_seq"])
            programs = load_programs(cur, pin["program_batch_seq"])
            if program_tz is None:
                program_tz = programs["timezone"]
            elif programs["timezone"] != program_tz:
                raise invalid(
                    "revamp_timezone_mismatch",
                    "both periods must use the same schedule timezone",
                    {"period": period, "timezone": programs["timezone"], "expected": program_tz},
                )
            expected_batch = (
                mapping["before_program_batch_seq"]
                if period == "before"
                else mapping["after_program_batch_seq"]
            )
            if pin["program_batch_seq"] != int(expected_batch):
                raise invalid(
                    "mapping_batch_mismatch",
                    "the period's program batch does not match the batch named in the mapping",
                    {
                        "period": period,
                        "period_program_batch_seq": pin["program_batch_seq"],
                        "mapping_program_batch_seq": int(expected_batch),
                    },
                )
            unknown = [r["person_id"] for r in recruitment["rows"] if r["inclusion_probability"] is None]
            if unknown:
                raise invalid(
                    "inclusion_probability_unknown",
                    "a formal two-period estimate needs a known inclusion probability for every frame person",
                    {"period": period, "persons": unknown},
                )
            expected_days = survey_days(recruitment["survey_period_start"], recruitment["survey_period_end"])
            outside = sorted(
                {a["air_date"] for a in programs["airings"]}
                - {
                    a["air_date"]
                    for a in programs["airings"]
                    if recruitment["survey_period_start"] <= a["air_date"] <= recruitment["survey_period_end"]
                }
            )
            if outside:
                raise invalid(
                    "program_outside_survey_period",
                    "program airings fall outside the recruitment survey period",
                    {"period": period, "air_dates": [d.isoformat() for d in outside]},
                )
            max_seq = _max_diary_seq(cur)
            if pin["diary_seq_cutoff"] > max_seq:
                raise invalid(
                    "diary_cutoff_beyond_available",
                    "diary_seq_cutoff exceeds available submissions",
                    {"period": period, "cutoff": pin["diary_seq_cutoff"], "max_available": max_seq},
                )
            plans[period] = {
                "period": period,
                "inputs": pin,
                "survey_period": {
                    "start": recruitment["survey_period_start"].isoformat(),
                    "end": recruitment["survey_period_end"].isoformat(),
                    "days": len(expected_days),
                },
                "population_total": int(targets["population_total"]),
                "program_timezone": programs["timezone"],
                "program_batch_seq": pin["program_batch_seq"],
                "target_batch_seq": pin["target_batch_seq"],
                "recruitment_batch_seq": pin["recruitment_batch_seq"],
                "diary_seq_cutoff": pin["diary_seq_cutoff"],
            }
            plans[period]["input_hash"] = _period_input_hash(cur, plans[period], config["raking"], seed)

        b, a = plans["before"]["survey_period"], plans["after"]["survey_period"]
        if b["start"] <= a["end"] and a["start"] <= b["end"]:
            raise invalid(
                "revamp_periods_overlap",
                "the two survey periods overlap; a before/after comparison needs disjoint periods",
                {"before": b, "after": a},
            )
        if config.get("timezone") and config["timezone"] != program_tz:
            raise invalid(
                "timezone_mismatch",
                "run timezone must match the program batch timezone",
                {"run_timezone": config["timezone"], "program_timezone": program_tz},
            )

        parent_id = None
        correction_payload: dict[str, Any] = {}
        if correction:
            parent_id = str(correction["parent_comparison_id"])
            parent = fetch_comparison(cur, parent_id)
            if parent["status"] != "published":
                raise invalid(
                    "correction_parent_not_published",
                    "only a published comparison can be corrected",
                    {"parent_status": parent["status"]},
                )
            period = correction["period"]
            other = "after" if period == "before" else "before"
            parent_periods = parent["periods"]
            if parent_periods[period]["input_hash"] == plans[period]["input_hash"]:
                raise invalid(
                    "correction_period_unchanged",
                    f"the {period} inputs are identical to the parent; nothing was corrected",
                    {"period": period},
                )
            if parent_periods[other]["input_hash"] != plans[other]["input_hash"]:
                raise invalid(
                    "correction_touches_other_period",
                    "a correction may only change the named period; the other period keeps its pinned inputs",
                    {"corrected_period": period, "other_period": other},
                )
            if parent_periods[period].get("program_batch_seq") != plans[period]["inputs"]["program_batch_seq"]:
                raise invalid(
                    "correction_requires_new_mapping",
                    "correcting a program schedule needs a new editor-confirmed mapping",
                    {"period": period},
                )
            correction_payload = {
                "corrected_period": period,
                "reason": correction["reason"],
                "parent_period_input_hash": parent_periods[period]["input_hash"],
                "new_period_input_hash": plans[period]["input_hash"],
                "other_period_unchanged": True,
                "other_period_input_hash": parent_periods[other]["input_hash"],
            }

        config = {**config, "timezone": program_tz, "diary_window": DEFAULT_DIARY_WINDOW}
        decomposition_order = config["decomposition_order"]
        weight_version = {
            "design_weight": "1 / inclusion_probability",
            "reference_target_batch_seq": int(body["reference_target_batch_seq"]),
            "calibration": {
                "method": "bounded raking",
                "dimensions": config["raking"]["dimensions"],
                "lower_factor": config["raking"]["lower_factor"],
                "upper_factor": config["raking"]["upper_factor"],
                "tolerance": config["raking"]["tolerance"],
                "max_iterations": config["raking"]["max_iterations"],
            },
            "paired": {
                "base_weight": "before design weight / retention probability of the calibration cell",
                "retention_cells": config["raking"]["dimensions"],
                "retention_floor": config["paired"]["retention_floor"],
                "calibration_target": "the same fixed reference population",
            },
            "completeness": config["completeness"],
            "decomposition_order": decomposition_order,
            "reach_threshold_minutes": config["thresholds"]["reach_minutes"],
            "period_specific_targets_used_for": "own_population_estimates_only",
        }
        config_hash = canonical_hash(config)
        weight_version["config_hash"] = config_hash
        weight_version["weight_version_hash"] = canonical_hash(weight_version)

        input_hash = canonical_hash(
            {
                "mapping_seq": int(body["mapping_seq"]),
                "mapping_payload_hash": mapping["payload_hash"],
                "reference_target_batch_seq": int(body["reference_target_batch_seq"]),
                "periods": {p: {**plans[p]["inputs"], "input_hash": plans[p]["input_hash"]} for p in PERIODS},
                "config": config,
                "seed": seed,
            }
        )
        comparison_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO revamp_comparisons
                (comparison_id, status, parent_comparison_id, mapping_seq,
                 reference_target_batch_seq, seed, periods, config, config_hash, input_hash,
                 correction, decomposition_order, weight_version, note, created_by)
            VALUES (%s,'created',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(comparison_id),
                parent_id,
                int(body["mapping_seq"]),
                int(body["reference_target_batch_seq"]),
                seed,
                jb(plans),
                jb(config),
                config_hash,
                input_hash,
                jb(correction_payload),
                jb({"order": decomposition_order, "cells": list(DIMENSIONS)}),
                jb(weight_version),
                body.get("note"),
                actor,
            ),
        )
        for stage in STAGES:
            cur.execute(
                "INSERT INTO revamp_comparison_stages (comparison_id, stage, status) VALUES (%s,%s,'pending')",
                (str(comparison_id), stage),
            )
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,'revamp_comparison',%s,%s)",
            (
                actor,
                "revamp_comparison.create",
                str(comparison_id),
                jb(
                    {
                        "input_hash": input_hash,
                        "mapping_seq": int(body["mapping_seq"]),
                        "correction_of": parent_id,
                        "decomposition_order": decomposition_order,
                    }
                ),
            ),
        )
        cur.execute(
            "SELECT comparison_id, status FROM revamp_comparisons WHERE input_hash=%s AND comparison_id<>%s LIMIT 1",
            (input_hash, str(comparison_id)),
        )
        duplicate = cur.fetchone()
        conn.commit()
        created = fetch_comparison(cur, comparison_id)
    return {
        "comparison": created,
        "deterministic_inputs": {
            "input_hash": input_hash,
            "config_hash": config_hash,
            "weight_version_hash": weight_version["weight_version_hash"],
            "duplicate_of_existing_equivalent_comparison": (
                {"comparison_id": str(duplicate["comparison_id"]), "status": duplicate["status"]}
                if duplicate
                else None
            ),
        },
        "interpretation": REPORT_INTERPRETATION,
    }


def _require_dimensions(margins: dict[str, Any], config: dict[str, Any]) -> None:
    missing = [d for d in config["raking"]["dimensions"] if d not in margins]
    if missing:
        raise invalid(
            "target_dimension_missing",
            "the reference target has no margin for a calibration dimension",
            {"missing": missing, "available": sorted(margins)},
        )


def _period_input_hash(cur, plan: dict[str, Any], raking: dict[str, Any], seed: int) -> str:
    inputs = plan["inputs"]
    recruitment = load_recruitment(cur, inputs["recruitment_batch_seq"])
    targets = load_targets(cur, inputs["target_batch_seq"])
    programs = load_programs(cur, inputs["program_batch_seq"])
    margins = {
        dim: {cat: float(v) for cat, v in cats.items()}
        for dim, cats in loads(targets["dimensions"]).items()
    }
    cur.execute(
        """
        SELECT s.person_id, s.submission_seq, s.revision, s.replacement_scope,
               e.station, e.source_serial, e.start_utc, e.end_utc
          FROM diary_submissions s
          LEFT JOIN diary_entries e ON e.submission_seq = s.submission_seq
         WHERE s.submission_seq <= %s
         ORDER BY s.submission_seq, e.entry_id
        """,
        (int(inputs["diary_seq_cutoff"]),),
    )
    diary_rows = [
        {
            "person_id": r["person_id"],
            "submission_seq": r["submission_seq"],
            "revision": r["revision"],
            "scope": loads(r["replacement_scope"]),
            "station": r["station"],
            "source_serial": r["source_serial"],
            "start": iso(r["start_utc"]),
            "end": iso(r["end_utc"]),
        }
        for r in cur.fetchall()
    ]
    return canonical_hash(
        {
            "inputs": inputs,
            "raking_dimensions": raking["dimensions"],
            "seed": seed,
            "recruitment": [
                {
                    "person_id": r["person_id"],
                    "household_id": r["household_id"],
                    "strata": r["strata"],
                    "age_band": r["age_band"],
                    "region": r["region"],
                    "sex": r["sex"],
                    "p": float(r["inclusion_probability"]),
                }
                for r in recruitment["rows"]
            ],
            "targets": {"seq": inputs["target_batch_seq"], "margins": margins},
            "programs": [
                {
                    "key": a["program_key"],
                    "station": a["station"],
                    "air_date": a["air_date"].isoformat(),
                    "start": a["start_local"],
                    "end": a["end_local"],
                }
                for a in programs["airings"]
            ],
            "diary_rows": diary_rows,
        }
    )


# ---------------------------------------------------------------------------
# Staged execution
# ---------------------------------------------------------------------------
def _stage_status(cur, comparison_id, stage: str) -> str:
    cur.execute(
        "SELECT status FROM revamp_comparison_stages WHERE comparison_id=%s AND stage=%s",
        (str(comparison_id), stage),
    )
    row = cur.fetchone()
    return row["status"] if row else "pending"


def _set_stage(conn, cur, comparison_id, stage: str, status: str, payload=None, error=None) -> None:
    now = datetime.now(UTC)
    cur.execute(
        """
        UPDATE revamp_comparison_stages
           SET status=%s, started_at=COALESCE(started_at,%s),
               finished_at=CASE WHEN %s IN ('done','failed') THEN %s ELSE finished_at END,
               payload=COALESCE(%s,payload), error=%s
         WHERE comparison_id=%s AND stage=%s
        """,
        (
            status,
            now,
            status,
            now,
            None if payload is None else jb(payload),
            None if error is None else jb(error),
            str(comparison_id),
            stage,
        ),
    )
    conn.commit()


def _fail(conn, cur, comparison_id, stage: str, exc: Exception) -> dict[str, Any]:
    conn.rollback()
    failure = {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "code": getattr(exc, "code", None),
        "details": getattr(exc, "details", None),
    }
    cur.execute(
        "UPDATE revamp_comparisons SET status='failed', failure=%s, finished_at=now(), updated_at=now() WHERE comparison_id=%s",
        (jb(failure), str(comparison_id)),
    )
    cur.execute(
        "UPDATE revamp_comparison_stages SET status='failed', error=%s, started_at=COALESCE(started_at,now()), finished_at=now() WHERE comparison_id=%s AND stage=%s",
        (jb(failure), str(comparison_id), stage),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES ('system','revamp_comparison.failed','revamp_comparison',%s,%s)",
        (str(comparison_id), jb(failure)),
    )
    conn.commit()
    return failure


def execute_comparison(db: Database, comparison_id, max_steps: int = 20) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        comp = fetch_comparison(cur, comparison_id)
        if comp["status"] in ("published", "rejected"):
            return {"comparison": comp, "executed": [], "message": "comparison already finalised"}
        if comp["status"] == "failed":
            raise conflict("revamp_comparison_failed", "comparison failed; create a new one", comp.get("failure"))
        cur.execute(
            "UPDATE revamp_comparisons SET status='running', updated_at=now() WHERE comparison_id=%s",
            (str(comparison_id),),
        )
        conn.commit()
        executed: list[str] = []
        steps = 0
        while steps < max_steps:
            if fetch_comparison(cur, comparison_id)["status"] in ("published", "rejected", "failed"):
                break
            pending = [
                s for s in STAGES if _stage_status(cur, comparison_id, s) in ("pending", "running")
            ]
            if not pending:
                break
            stage = pending[0]
            executed.append(stage)
            _set_stage(conn, cur, comparison_id, stage, "running")
            try:
                payload = _run_stage(db, conn, cur, comparison_id, stage)
                _set_stage(conn, cur, comparison_id, stage, "done", payload=payload)
            except Exception as exc:  # noqa: BLE001
                _fail(conn, cur, comparison_id, stage, exc)
                final = fetch_comparison(cur, comparison_id)
                if final["status"] == "failed":
                    raise conflict(
                        "revamp_stage_failed", f"stage {stage} failed", final.get("failure")
                    ) from exc
                raise
            steps += 1
        final = fetch_comparison(cur, comparison_id)
        if final["status"] == "rejected":
            cur.execute(
                """
                UPDATE revamp_comparison_stages
                   SET status='done', started_at=COALESCE(started_at,now()), finished_at=now(),
                       payload=COALESCE(payload,%s)
                 WHERE comparison_id=%s AND status='pending'
                """,
                (jb({"skipped": True, "reason": "comparison rejected"}), str(comparison_id)),
            )
            conn.commit()
        final = fetch_comparison(cur, comparison_id)
    return {"comparison": final, "executed": executed}


def _run_stage(db, conn, cur, comparison_id, stage: str) -> dict[str, Any]:
    handlers = {
        "validate_inputs": _stage_validate,
        "build_periods": _stage_build_periods,
        "paired": _stage_paired,
        "bootstrap": _stage_bootstrap,
        "estimate": _stage_estimate,
        "publish_report": _stage_publish,
    }
    return handlers[stage](db, conn, cur, comparison_id)


# ---------------------------------------------------------------------------
# Period analysis (shared by build and any later recomputation)
# ---------------------------------------------------------------------------
def _period_analysis(cur, plan: dict[str, Any], config: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    recruitment = load_recruitment(cur, plan["recruitment_batch_seq"])
    submissions = load_submissions(cur, int(plan["diary_seq_cutoff"]))
    frame = build_frame(recruitment, submissions, config)
    expected = survey_days(recruitment["survey_period_start"], recruitment["survey_period_end"])
    resolutions = _load_resolutions(cur, [r["person_id"] for r in recruitment["rows"]])
    programs = load_programs(cur, plan["program_batch_seq"])
    program_map: dict[str, dict[str, Any]] = {}
    for a in programs["airings"]:
        program_map.setdefault(a["program_key"], {"title": a["title"], "airings": []})["airings"].append(a)
    windows = day_windows(expected, config, tz)
    threshold = float(config["thresholds"]["reach_minutes"])

    persons: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for fp in frame["persons"]:
        subs = submissions.get(fp.person_id, [])
        analysis = analyze_person(fp, subs, expected, config, resolutions, tz)
        if fp.exclusion_code is None and analysis["resolved"]["pending_conflicts"]:
            fp.exclusion_code = "unresolved_conflict"
            fp.exclusion_detail = {
                "reason": "cross-station conflict is unconfirmed; affected minutes are unknown",
                "imputed_zero": False,
            }
        if fp.exclusion_code is None and not analysis["completeness"]["satisfied"]:
            fp.exclusion_code = "incomplete_diary"
            fp.exclusion_detail = {
                "reason": "pre-declared complete-recall condition not met",
                "complete_days": analysis["completeness"]["complete_days"],
            }
        fp.eligible = fp.exclusion_code is None
        if not fp.eligible:
            excluded[fp.exclusion_code] = excluded.get(fp.exclusion_code, 0) + 1
        minutes: dict[str, float] = {}
        listening_total = 0.0
        if fp.eligible:
            exposure = program_exposure(
                analysis["resolved"]["station_intervals"],
                analysis["resolved"]["unknown_ranges"],
                analysis["resolved"]["confirmed_nonlisten_ranges"],
                program_map,
                expected,
                windows,
                threshold,
            )
            minutes = {k: float(v["minutes"]) for k, v in exposure.items()}
            listening_total = sum(
                float(rep["listening_minutes"]) for rep in analysis["day_reports"]
            )
        persons.append(
            {
                "person_id": fp.person_id,
                "household_id": fp.household_id,
                "strata": fp.strata,
                "strata_effective": fp.strata_effective,
                "age_band": fp.age_band,
                "region": fp.region,
                "sex": fp.sex,
                "inclusion_probability": fp.inclusion_probability,
                "base_weight": fp.base_weight,
                "eligible": fp.eligible,
                "exclusion_code": fp.exclusion_code,
                "exclusion_detail": fp.exclusion_detail,
                "program_minutes": minutes,
                "listening_minutes_total": round(listening_total, 3),
            }
        )
    return {
        "persons": persons,
        "excluded": excluded,
        "program_map": program_map,
        "expected_days": expected,
        "diary_only_persons": frame["diary_only_persons"],
        "probability_range": frame["probability_range"],
        "threshold": threshold,
    }


def _calibrate(persons, margins, config, base_weights) -> Any:
    rc = _raking_config(config)
    attrs = [{"age_band": p["age_band"], "region": p["region"], "sex": p["sex"]} for p in persons]
    return rake(attrs, base_weights, margins, rc)


def _eligible(persons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in persons if p["eligible"]]


# ---------------------------------------------------------------------------
# Stage: validate
# ---------------------------------------------------------------------------
def _stage_validate(db, conn, cur, comparison_id) -> dict[str, Any]:
    row = _row(cur, comparison_id)
    periods = loads(row["periods"])
    mapping = load_mapping(cur, row["mapping_seq"])
    reference = load_targets(cur, row["reference_target_batch_seq"])
    reference_margins = loads(reference["dimensions"])
    checks: dict[str, Any] = {}
    bad_cells = []
    for period in PERIODS:
        plan = periods[period]
        recruitment = load_recruitment(cur, plan["recruitment_batch_seq"])
        targets = load_targets(cur, plan["target_batch_seq"])
        programs = load_programs(cur, plan["program_batch_seq"])
        totals = loads(targets["dimension_totals"])
        total = float(targets["population_total"])
        inconsistent = {dim: float(v) for dim, v in totals.items() if abs(float(v) - total) > 1e-6}
        if inconsistent:
            raise invalid(
                "target_totals_inconsistent",
                f"{period} period target margins point at different populations",
                {"period": period, "population_total": total, "conflicting": inconsistent},
            )
        frame_cats = {
            "age_band": {r["age_band"] for r in recruitment["rows"]},
            "region": {r["region"] for r in recruitment["rows"]},
            "sex": {r["sex"] for r in recruitment["rows"]},
        }
        for dim, cats in reference_margins.items():
            for cat, target in cats.items():
                if float(target) > 0 and cat not in frame_cats.get(dim, set()):
                    bad_cells.append(
                        {"period": period, "dimension": dim, "category": cat, "target": float(target)}
                    )
        checks[period] = {
            "survey_period": plan["survey_period"],
            "timezone": programs["timezone"],
            "population_total": total,
            "target_dimension_totals": totals,
            "reference_dimension_totals": loads(reference["dimension_totals"]),
            "frame_persons": len(recruitment["rows"]),
            "households": len({r["household_id"] for r in recruitment["rows"]}),
            "programs": sorted({a["program_key"] for a in programs["airings"]}),
            "input_hash": plan["input_hash"],
        }
    if bad_cells:
        raise invalid(
            "reference_cell_without_frame_sample",
            "the fixed reference population asks for a cell a period's frame cannot sample",
            {"cells": bad_cells},
        )
    checks["mapping"] = {
        "mapping_seq": mapping["mapping_seq"],
        "confirmed_by": mapping["confirmed_by"],
        "confirmed_role": mapping["confirmed_role"],
        "payload_hash": mapping["payload_hash"],
        "comparable": [l["comparison_key"] for l in mapping["links"] if l["comparable"]],
        "new": [l["comparison_key"] for l in mapping["links"] if l["relation"] == "new"],
        "discontinued": [l["comparison_key"] for l in mapping["links"] if l["relation"] == "discontinued"],
        "slot_disclaimer": mapping["slot_disclaimer"],
    }
    checks["reference_target"] = {
        "batch_seq": row["reference_target_batch_seq"],
        "population_total": reference["population_total"],
        "margins": reference_margins,
        "note": "Both periods are calibrated to exactly these margins, whatever their own target batch says.",
    }
    checks["correction"] = loads(row["correction"]) or {}
    checks["decomposition_order"] = loads(row["decomposition_order"])["order"]
    checks["weight_version"] = loads(row["weight_version"])
    return checks


# ---------------------------------------------------------------------------
# Stage: build both periods
# ---------------------------------------------------------------------------
def _stratum_summary(persons: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    min_cell = int(config["thresholds"]["min_cell_persons"])
    grouped: dict[str, dict[str, Any]] = {}
    for p in persons:
        bucket = grouped.setdefault(
            p["strata_effective"],
            {"frame_persons": 0, "eligible_persons": 0, "households": set()},
        )
        bucket["frame_persons"] += 1
        if p["eligible"]:
            bucket["eligible_persons"] += 1
            bucket["households"].add(p["household_id"])
    empty = sorted(n for n, b in grouped.items() if b["eligible_persons"] == 0)
    sparse = sorted(n for n, b in grouped.items() if 0 < b["eligible_persons"] < min_cell)
    return {
        "by_stratum": {
            name: {
                "frame_persons": b["frame_persons"],
                "eligible_persons": b["eligible_persons"],
                "eligible_households": len(b["households"]),
            }
            for name, b in sorted(grouped.items())
        },
        "strata_without_eligible_sample": empty,
        "sparse_strata": sparse,
        "min_cell_persons": min_cell,
        "note": "Empty and sparse strata are reported here and pause definitive judgement; no synthetic respondent is added.",
    }


def _coverage(persons, reference_margins, config) -> dict[str, Any]:
    sample_n: dict[tuple[str, str], int] = {}
    for p in persons:
        for dim in DIMENSIONS:
            sample_n[(dim, p[dim])] = sample_n.get((dim, p[dim]), 0) + 1
    total = len(persons)
    ratio_floor = float(config["thresholds"]["under_representation_ratio"])
    min_cell = int(config["thresholds"]["min_cell_persons"])
    cells = []
    undercovered, no_sample = [], []
    for dim, cats in reference_margins.items():
        dim_total = sum(float(v) for v in cats.values())
        for cat, target in cats.items():
            n = sample_n.get((dim, cat), 0)
            sample_share = n / total if total else 0.0
            target_share = float(target) / dim_total if dim_total else 0.0
            ratio = sample_share / target_share if target_share else 0.0
            if n == 0:
                status = "no_sample"
                no_sample.append(f"{dim}={cat}")
            elif ratio < ratio_floor or n < min_cell:
                status = "undercovered"
                undercovered.append(f"{dim}={cat}")
            else:
                status = "acceptable"
            cells.append(
                {
                    "dimension": dim,
                    "category": cat,
                    "reference_population": float(target),
                    "reference_share": round(target_share, 6),
                    "eligible_persons": n,
                    "sample_share": round(sample_share, 6),
                    "representation_ratio": round(ratio, 6),
                    "status": status,
                }
            )
    return {
        "eligible_persons": total,
        "undercovered_groups": undercovered,
        "no_sample_groups": no_sample,
        "cells": cells,
        "judged_on": "unweighted eligible diarists versus fixed reference population shares",
        "warning": "do not read a program difference across groups as a performance difference",
    }


def _stage_build_periods(db, conn, cur, comparison_id) -> dict[str, Any]:
    row = _row(cur, comparison_id)
    config = _config(row)
    tz = _tz(row)
    periods = loads(row["periods"])
    reference_margins = loads(load_targets(cur, row["reference_target_batch_seq"])["dimensions"])

    cur.execute("DELETE FROM revamp_comparison_persons WHERE comparison_id=%s", (str(comparison_id),))
    cur.execute("DELETE FROM revamp_comparison_periods WHERE comparison_id=%s", (str(comparison_id),))

    summary: dict[str, Any] = {}
    for period in PERIODS:
        plan = periods[period]
        built = _period_analysis(cur, plan, config, tz)
        eligible = _eligible(built["persons"])
        if not eligible:
            raise invalid(
                "revamp_period_empty_sample",
                f"{period} period has no eligible diarists; no comparison is possible",
                {"period": period},
            )
        base = [float(p["base_weight"]) for p in eligible]
        ref = _calibrate(eligible, reference_margins, config, base)
        own_margins = loads(load_targets(cur, plan["target_batch_seq"])["dimensions"])
        own = _calibrate(eligible, own_margins, config, base)
        if not ref.success:
            raise invalid(
                "revamp_reference_calibration_failed",
                f"{period} period cannot be calibrated to the fixed reference population",
                {"period": period, "calibration": ref.summary()},
            )
        ref_weights = {p["person_id"]: w for p, w in zip(eligible, ref.weights)}
        own_weights = (
            {p["person_id"]: w for p, w in zip(eligible, own.weights)} if own.success else {}
        )
        for p in built["persons"]:
            cur.execute(
                """
                INSERT INTO revamp_comparison_persons
                    (comparison_id, period_key, person_id, household_id, strata, strata_effective,
                     age_band, region, sex, inclusion_probability, base_weight, reference_weight,
                     own_target_weight, eligible, exclusion_code, exclusion_detail,
                     program_minutes, listening_minutes_total)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(comparison_id),
                    period,
                    p["person_id"],
                    p["household_id"],
                    p["strata"],
                    p["strata_effective"],
                    p["age_band"],
                    p["region"],
                    p["sex"],
                    p["inclusion_probability"],
                    p["base_weight"],
                    ref_weights.get(p["person_id"]),
                    own_weights.get(p["person_id"]),
                    p["eligible"],
                    p["exclusion_code"],
                    jb(p["exclusion_detail"]),
                    jb(p["program_minutes"]),
                    p["listening_minutes_total"],
                ),
            )
        payload = {
            "status": "ok",
            "survey_period": plan["survey_period"],
            "eligible_persons": len(eligible),
            "eligible_households": len({p["household_id"] for p in eligible}),
            "weighted_population_under_reference": round(sum(ref.weights), 3),
            "reference_calibration": ref.summary(),
            "own_target_calibration": own.summary()
            if own.success
            else {
                "success": False,
                "status": own.status,
                "reason": "own-population calibration failed; only the reference-population estimate is shown",
                "max_rel_residual": own.max_rel_residual,
                "empty_cells": own.empty_cells,
                "unreachable_cells": own.unreachable_cells,
            },
            "exclusions": built["excluded"],
            "strata": _stratum_summary(built["persons"], config),
            "coverage": _coverage(eligible, reference_margins, config),
            "probability_range": built["probability_range"],
            "diary_only_persons": built["diary_only_persons"],
        }
        cur.execute(
            """
            INSERT INTO revamp_comparison_periods
                (comparison_id, period_key, survey_period, input_hash, eligible_persons,
                 eligible_households, eligible_weighted_population, calibration,
                 own_target_calibration, coverage, strata, estimates)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(comparison_id),
                period,
                jb(plan["survey_period"]),
                plan["input_hash"],
                len(eligible),
                payload["eligible_households"],
                payload["weighted_population_under_reference"],
                jb(payload["reference_calibration"]),
                jb(payload["own_target_calibration"]),
                jb(payload["coverage"]),
                jb(payload["strata"]),
                # Filled by the estimate stage once the per-program numbers exist.
                jb({"status": "pending_estimate"}),
            ),
        )
        summary[period] = payload
    summary["judgment"] = _period_judgment(summary)
    return summary


def _period_judgment(summary: dict[str, Any]) -> dict[str, Any]:
    paused = []
    for period in PERIODS:
        strata = summary[period]["strata"]
        if strata["strata_without_eligible_sample"]:
            paused.append({"period": period, "kind": "missing_stratum", "strata": strata["strata_without_eligible_sample"]})
        if strata["sparse_strata"]:
            paused.append({"period": period, "kind": "sparse_stratum", "strata": strata["sparse_strata"]})
        if summary[period]["coverage"]["no_sample_groups"]:
            paused.append({"period": period, "kind": "no_sample_group", "groups": summary[period]["coverage"]["no_sample_groups"]})
    return {
        "status": "paused_sparse_or_missing_strata" if paused else "ok",
        "triggers": paused,
        "rule": (
            "Sparse or empty strata stop a definitive judgement for the affected items; the comparison still "
            "shows point estimates but says explicitly that they are not conclusive."
        ),
    }


# ---------------------------------------------------------------------------
# Stage: paired sample
# ---------------------------------------------------------------------------
def _load_period_persons(cur, comparison_id, period: str, eligible_only: bool = True) -> list[dict[str, Any]]:
    clause = "AND eligible" if eligible_only else ""
    cur.execute(
        f"""
        SELECT person_id, household_id, strata_effective, age_band, region, sex,
               inclusion_probability, base_weight, reference_weight, own_target_weight,
               program_minutes, listening_minutes_total, paired, retention_probability,
               paired_design_weight, paired_weight, eligible
          FROM revamp_comparison_persons
         WHERE comparison_id=%s AND period_key=%s {clause}
         ORDER BY person_id
        """,
        (str(comparison_id), period),
    )
    out = []
    for r in cur.fetchall():
        out.append(
            {
                **r,
                "base_weight": float(r["base_weight"]) if r["base_weight"] is not None else None,
                "reference_weight": float(r["reference_weight"])
                if r["reference_weight"] is not None
                else None,
                "own_target_weight": float(r["own_target_weight"])
                if r["own_target_weight"] is not None
                else None,
                "paired_design_weight": float(r["paired_design_weight"])
                if r["paired_design_weight"] is not None
                else None,
                "paired_weight": float(r["paired_weight"]) if r["paired_weight"] is not None else None,
                "retention_probability": float(r["retention_probability"])
                if r["retention_probability"] is not None
                else None,
                "program_minutes": loads(r["program_minutes"]) or {},
                "listening_minutes_total": float(r["listening_minutes_total"] or 0.0),
            }
        )
    return out


def _stage_paired(db, conn, cur, comparison_id) -> dict[str, Any]:
    row = _row(cur, comparison_id)
    config = _config(row)
    paired_cfg = config["paired"]
    periods = loads(row["periods"])
    before_days = int(periods["before"]["survey_period"]["days"])
    after_days = int(periods["after"]["survey_period"]["days"])

    before_eligible = _load_period_persons(cur, comparison_id, "before", eligible_only=True)
    after_all = _load_period_persons(cur, comparison_id, "after", eligible_only=False)
    after_eligible = [p for p in after_all if p["eligible"]]
    after_frame_ids = {p["person_id"] for p in after_all}
    after_eligible_ids = {p["person_id"] for p in after_eligible}
    after_by_id = {p["person_id"]: p for p in after_eligible}

    dims = tuple(config["raking"]["dimensions"])
    retention = retention_table(
        before_eligible,
        after_frame_ids,
        after_eligible_ids,
        dims,
        retention_floor=float(paired_cfg["retention_floor"]),
        min_paired_cell_persons=int(paired_cfg["min_paired_cell_persons"]),
        max_absent_share=float(paired_cfg["max_absent_share"]),
    )

    paired_persons = [
        {**p, "after": after_by_id[p["person_id"]]}
        for p in before_eligible
        if p["person_id"] in after_eligible_ids
    ]
    cur.execute("DELETE FROM revamp_comparison_households WHERE comparison_id=%s", (str(comparison_id),))

    if not paired_persons:
        payload = {
            "status": "unavailable_no_paired_sample",
            "retention": {k: v for k, v in retention.items() if k != "by_cell_retention"},
            "calibration": None,
            "change_available": False,
            "paired_persons": 0,
            "paired_households": 0,
            "reason": "no person is eligible in both periods; same-person change cannot be estimated",
            "noncoverable": {
                "absent_from_after_frame": retention["absent_from_after_frame"],
                "absent_share": retention["absent_share"],
                "interpretation": retention["interpretation"],
            },
            "judgment": {"status": "paused_no_paired_sample", "reasons": retention["problems"]},
        }
        cur.execute(
            "UPDATE revamp_comparisons SET paired=%s, updated_at=now() WHERE comparison_id=%s",
            (jb(payload), str(comparison_id)),
        )
        return payload

    base = paired_base_weights(paired_persons, retention, dims)
    reference_margins = loads(load_targets(cur, row["reference_target_batch_seq"])["dimensions"])
    calib = _calibrate(paired_persons, reference_margins, config, base)

    before_household_members: dict[str, set[str]] = {}
    for p in before_eligible:
        before_household_members.setdefault(p["household_id"], set()).add(p["person_id"])
    paired_by_household: dict[str, set[str]] = {}
    for p in paired_persons:
        paired_by_household.setdefault(p["household_id"], set()).add(p["person_id"])
    households = {
        hid: {
            "frame_members_before": len(members),
            "paired_members": len(paired_by_household.get(hid, set())),
            "all_before_members_paired": paired_by_household.get(hid, set()) == members,
        }
        for hid, members in before_household_members.items()
    }
    for hid, meta in households.items():
        cur.execute(
            "INSERT INTO revamp_comparison_households (comparison_id, household_id, payload) VALUES (%s,%s,%s)",
            (str(comparison_id), hid, jb(meta)),
        )

    if not calib.success:
        payload = {
            "status": "paused_paired_calibration_failed",
            "retention": {k: v for k, v in retention.items() if k != "by_cell_retention"},
            "calibration": calib.summary(),
            "change_available": False,
            "paired_persons": len(paired_persons),
            "paired_households": len(households),
            "reason": "paired weights cannot be calibrated back to the reference population",
            "noncoverable": {
                "absent_from_after_frame": retention["absent_from_after_frame"],
                "absent_share": retention["absent_share"],
                "attrition": retention["attrition"],
                "interpretation": retention["interpretation"],
            },
            "judgment": {"status": "paused_paired_calibration_failed", "reasons": retention["problems"]},
        }
        cur.execute(
            "UPDATE revamp_comparisons SET paired=%s, updated_at=now() WHERE comparison_id=%s",
            (jb(payload), str(comparison_id)),
        )
        return payload

    for person, w in zip(paired_persons, calib.weights):
        key = cell_key(person, dims)
        r = retention["by_cell_retention"][key]
        cur.execute(
            """
            UPDATE revamp_comparison_persons
               SET paired=TRUE, retention_probability=%s, paired_design_weight=%s, paired_weight=%s
             WHERE comparison_id=%s AND period_key='before' AND person_id=%s
            """,
            (
                round(r, 8),
                round(float(person["base_weight"]) / r, 8),
                round(w, 8),
                str(comparison_id),
                person["person_id"],
            ),
        )

    judged = retention["status"] == "ok"
    retention_payload = {k: v for k, v in retention.items() if k != "by_cell_retention"}
    payload = {
        "status": "ok" if judged else "paused_retention_model",
        "retention": retention_payload,
        "calibration": calib.summary(),
        "change_available": True,
        "paired_persons": len(paired_persons),
        "paired_households": len(paired_by_household),
        "households": households,
        "before_days": before_days,
        "after_days": after_days,
        "judgment": {
            "status": "ok" if judged else "paused_retention_model",
            "sensitivity": (
                "The paired change is weighted by estimated retention. Cells with low retention or high "
                "frame absence make it depend heavily on that model, so it is reported but not treated as "
                "conclusive."
            ),
            "reasons": retention["problems"],
        },
        "noncoverable": {
            "absent_from_after_frame": retention["absent_from_after_frame"],
            "absent_share": retention["absent_share"],
            "attrition": retention["attrition"],
            "before_eligible": retention["before_eligible"],
            "interpretation": retention["interpretation"],
        },
        "interpretation": retention["interpretation"],
    }
    cur.execute(
        "UPDATE revamp_comparisons SET paired=%s, updated_at=now() WHERE comparison_id=%s",
        (jb(payload), str(comparison_id)),
    )
    return payload


# ---------------------------------------------------------------------------
# Stage: bootstrap
# ---------------------------------------------------------------------------
def _statistics(
    persons: list[dict[str, Any]],
    weights: list[float],
    program_key: str,
    threshold: float,
    days: int,
) -> dict[str, Any]:
    total_w = sum(weights)
    weighted_minutes = 0.0
    listener_w = 0.0
    listener_n = 0
    for p, w in zip(persons, weights):
        m = float(p["program_minutes"].get(program_key, 0.0))
        weighted_minutes += w * m
        if m >= threshold:
            listener_w += w
            listener_n += 1
    return {
        "reach_persons": round(listener_w, 6),
        "reach_rate": round(listener_w / total_w, 6) if total_w else 0.0,
        "mean_minutes_per_person": round(weighted_minutes / total_w, 6) if total_w else 0.0,
        "mean_minutes_per_person_day": round(weighted_minutes / total_w / days, 6)
        if total_w and days
        else 0.0,
        "mean_minutes_per_listener": round(weighted_minutes / listener_w, 6) if listener_w else None,
        "listener_unweighted_n": listener_n,
        "unweighted_n": len(persons),
        "weight_sum": round(total_w, 6),
    }


def _overall_stats(persons, weights, days: int) -> dict[str, Any]:
    total_w = sum(weights)
    weighted = sum(w * p["listening_minutes_total"] for p, w in zip(persons, weights))
    return {
        "mean_listening_minutes_per_person_day": round(weighted / total_w / days, 6)
        if total_w and days
        else 0.0,
        "weight_sum": round(total_w, 6),
        "unweighted_n": len(persons),
    }


def _stage_bootstrap(db, conn, cur, comparison_id) -> dict[str, Any]:
    row = _row(cur, comparison_id)
    config = _config(row)
    periods = loads(row["periods"])
    reference_margins = loads(load_targets(cur, row["reference_target_batch_seq"])["dimensions"])
    requested = int(config["bootstrap"]["replicates"])
    failure_limit = float(config["bootstrap"]["max_calibration_failure_rate"])
    seed = int(row["seed"])
    threshold = float(config["thresholds"]["reach_minutes"])
    mapping = load_mapping(cur, row["mapping_seq"])
    comparable = [l for l in mapping["links"] if l["comparable"]]
    paired_row = loads(row["paired"]) or {}
    paired_ok = bool(paired_row.get("change_available"))
    days = {p: int(periods[p]["survey_period"]["days"]) for p in PERIODS}

    cur.execute("DELETE FROM revamp_comparison_replicates WHERE comparison_id=%s", (str(comparison_id),))
    if requested <= 0:
        summary = {
            "requested": 0,
            "success": 0,
            "failed": 0,
            "failure_rate": 1.0,
            "intervals_allowed": False,
            "interval_status": "bootstrap_disabled",
            "seed": seed,
        }
        cur.execute(
            "UPDATE revamp_comparisons SET bootstrap=%s, interval_status=%s, updated_at=now() WHERE comparison_id=%s",
            (jb(summary), summary["interval_status"], str(comparison_id)),
        )
        return summary

    data = {p: _load_period_persons(cur, comparison_id, p, eligible_only=True) for p in PERIODS}
    paired_persons = (
        _load_paired_persons(cur, comparison_id) if paired_ok else []
    )

    # One bootstrap stream per period, drawn up front from the pinned seed.
    reps: dict[str, list[list[int]]] = {}
    for offset, period in enumerate(PERIODS):
        persons = data[period]
        by_stratum: dict[str, dict[str, list[int]]] = {}
        for idx, p in enumerate(persons):
            by_stratum.setdefault(p["strata_effective"], {}).setdefault(p["household_id"], []).append(idx)
        buckets = {s: [m for _, m in sorted(h.items())] for s, h in by_stratum.items()}
        reps[period] = _stratified(buckets, requested, seed + offset)
    paired_reps = None
    if paired_persons:
        by_stratum = {}
        for idx, p in enumerate(paired_persons):
            by_stratum.setdefault(p["strata_effective"], {}).setdefault(p["household_id"], []).append(idx)
        buckets = {s: [m for _, m in sorted(h.items())] for s, h in by_stratum.items()}
        paired_reps = _stratified(buckets, requested, seed + 1000)

    successes = 0
    failures: dict[str, int] = {}
    for rep_no in range(requested):
        period_weights: dict[str, list[float]] = {}
        period_picks: dict[str, list[int]] = {}
        failed = None
        for period in PERIODS:
            persons = data[period]
            picks = reps[period][rep_no]
            sub = [persons[i] for i in picks]
            base = [persons[i]["base_weight"] for i in picks]
            res = _calibrate(sub, reference_margins, config, base)
            if not res.success:
                failed = f"{period}:{res.status}"
                break
            period_weights[period] = res.weights
            period_picks[period] = picks
        if failed:
            failures[failed] = failures.get(failed, 0) + 1
            cur.execute(
                "INSERT INTO revamp_comparison_replicates (comparison_id, replicate, payload) VALUES (%s,%s,%s)",
                (str(comparison_id), rep_no, jb({"success": False, "failure": failed})),
            )
            continue
        items: dict[str, Any] = {}
        for link in comparable:
            b_persons = data["before"]
            a_persons = data["after"]
            bsub = [b_persons[i] for i in period_picks["before"]]
            asub = [a_persons[i] for i in period_picks["after"]]
            bstat = _statistics(
                bsub, period_weights["before"], link["before_program_key"], threshold, days["before"]
            )
            astat = _statistics(
                asub, period_weights["after"], link["after_program_key"], threshold, days["after"]
            )
            items[link["comparison_key"]] = {
                "before": bstat,
                "after": astat,
                "change_mean_minutes_per_person": round(
                    astat["mean_minutes_per_person"] - bstat["mean_minutes_per_person"], 6
                ),
                "change_reach_rate": round(astat["reach_rate"] - bstat["reach_rate"], 6),
            }
        bsub = [data["before"][i] for i in period_picks["before"]]
        asub = [data["after"][i] for i in period_picks["after"]]
        b_overall = _overall_stats(bsub, period_weights["before"], days["before"])
        a_overall = _overall_stats(asub, period_weights["after"], days["after"])
        items["overall_listening"] = {
            "before": b_overall,
            "after": a_overall,
            "change_mean_listening_minutes_per_person_day": round(
                a_overall["mean_listening_minutes_per_person_day"]
                - b_overall["mean_listening_minutes_per_person_day"],
                6,
            ),
        }
        items["paired"] = _paired_replicate(cur, comparison_id, paired_persons, paired_reps, rep_no, reference_margins, config, comparable, threshold, days)
        successes += 1
        cur.execute(
            "INSERT INTO revamp_comparison_replicates (comparison_id, replicate, payload) VALUES (%s,%s,%s)",
            (str(comparison_id), rep_no, jb({"success": True, "items": items})),
        )

    failure_rate = (requested - successes) / requested if requested else 1.0
    allowed = failure_rate <= failure_limit
    summary = {
        "requested": requested,
        "success": successes,
        "failed": requested - successes,
        "failure_rate": round(failure_rate, 6),
        "failure_codes": failures,
        "max_calibration_failure_rate": failure_limit,
        "seed": seed,
        "intervals_allowed": allowed,
        "interval_status": None if allowed else "suppressed_calibration_failure_rate",
        "resampling_unit": "household, within effective stratum, independently per period",
        "duplicate_identifier_copies_deduplicated": False,
        "paired_replicates_requested": bool(paired_persons),
        "decomposition_order": loads(row["decomposition_order"])["order"],
        "replicate_streams": {
            "before_seed": seed,
            "after_seed": seed + 1,
            "paired_seed": seed + 1000 if paired_persons else None,
        },
    }
    cur.execute(
        "UPDATE revamp_comparisons SET bootstrap=%s, interval_status=%s, updated_at=now() WHERE comparison_id=%s",
        (jb(summary), summary["interval_status"], str(comparison_id)),
    )
    return summary


def _stratified(buckets: dict[str, list[list[int]]], requested: int, seed: int) -> list[list[int]]:
    import random

    rng = random.Random(seed)
    ordered = sorted(buckets)
    out: list[list[int]] = []
    for _ in range(requested):
        picks: list[int] = []
        for stratum in ordered:
            bucket = buckets[stratum]
            for _h in range(len(bucket)):
                picks.extend(bucket[rng.randrange(len(bucket))])
        out.append(picks)
    return out


def _load_paired_persons(cur, comparison_id) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT person_id, household_id, strata_effective, age_band, region, sex, base_weight, "
        "retention_probability, paired_design_weight, paired_weight, program_minutes, "
        "listening_minutes_total FROM revamp_comparison_persons "
        "WHERE comparison_id=%s AND period_key='before' AND paired ORDER BY person_id",
        (str(comparison_id),),
    )
    out = []
    for r in cur.fetchall():
        out.append(
            {
                **r,
                "base_weight": float(r["base_weight"]),
                "retention_probability": float(r["retention_probability"]),
                "paired_design_weight": float(r["paired_design_weight"]),
                "paired_weight": float(r["paired_weight"]),
                "program_minutes": loads(r["program_minutes"]) or {},
                "listening_minutes_total": float(r["listening_minutes_total"] or 0.0),
            }
        )
    return out


def _paired_replicate(
    cur,
    comparison_id,
    paired_persons,
    paired_reps,
    rep_no,
    reference_margins,
    config,
    comparable,
    threshold,
    days,
) -> dict[str, Any]:
    if not paired_persons or not paired_reps:
        return {"success": False, "failure": "no_paired_sample"}
    picks = paired_reps[rep_no]
    sub = [paired_persons[i] for i in picks]
    # The replicate uses the same retention model as the point estimate:
    # base = (1/p) / retention, then re-calibrated to the fixed reference.
    base = [paired_persons[i]["paired_design_weight"] for i in picks]
    res = _calibrate(sub, reference_margins, config, base)
    if not res.success:
        return {"success": False, "failure": res.status}
    after_by_id = _after_minutes(cur, comparison_id)
    per_item: dict[str, Any] = {}
    total = sum(res.weights)
    for link in comparable:
        delta = 0.0
        for p, w in zip(sub, res.weights):
            a_min = after_by_id.get(p["person_id"], {}).get(link["after_program_key"], 0.0)
            b_min = p["program_minutes"].get(link["before_program_key"], 0.0)
            delta += w * (float(a_min) - float(b_min))
        per_item[link["comparison_key"]] = round(delta / total, 6) if total else None
    totals = _after_totals(cur, comparison_id)
    overall = 0.0
    for p, w in zip(sub, res.weights):
        after_total = totals.get(p["person_id"], 0.0)
        overall += w * (after_total / days["after"] - p["listening_minutes_total"] / days["before"])
    per_item["overall_listening"] = round(overall / total, 6) if total else None
    return {"success": True, "changes": per_item, "paired_draws": len(sub)}


_AFTER_CACHE: dict[str, dict[str, Any]] = {}


def _after_minutes(cur, comparison_id) -> dict[str, dict[str, float]]:
    key = f"{comparison_id}:after_minutes"
    if key not in _AFTER_CACHE:
        cur.execute(
            "SELECT person_id, program_minutes FROM revamp_comparison_persons "
            "WHERE comparison_id=%s AND period_key='after' AND eligible",
            (str(comparison_id),),
        )
        _AFTER_CACHE[key] = {r["person_id"]: loads(r["program_minutes"]) or {} for r in cur.fetchall()}
    return _AFTER_CACHE[key]


def _after_totals(cur, comparison_id) -> dict[str, float]:
    key = f"{comparison_id}:after_totals"
    if key not in _AFTER_CACHE:
        cur.execute(
            "SELECT person_id, listening_minutes_total FROM revamp_comparison_persons "
            "WHERE comparison_id=%s AND period_key='after' AND eligible",
            (str(comparison_id),),
        )
        _AFTER_CACHE[key] = {
            r["person_id"]: float(r["listening_minutes_total"] or 0.0) for r in cur.fetchall()
        }
    return _AFTER_CACHE[key]


# ---------------------------------------------------------------------------
# Stage: estimate
# ---------------------------------------------------------------------------
def _interval(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"low": None, "high": None, "replicates": 0}
    ordered = sorted(values)
    return {
        "low": round(_percentile(ordered, 0.025), 6),
        "high": round(_percentile(ordered, 0.975), 6),
        "replicates": len(ordered),
        "level": 0.95,
        "method": "percentile_household_bootstrap_recalibrated",
    }


def _replicate_items(cur, comparison_id) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT payload FROM revamp_comparison_replicates WHERE comparison_id=%s ORDER BY replicate",
        (str(comparison_id),),
    )
    out = []
    for r in cur.fetchall():
        payload = loads(r["payload"]) or {}
        if payload.get("success"):
            out.append(payload["items"])
    return out


def _stat_values(rep_items: list[dict[str, Any]], key: str, side: str, metric: str) -> list[float]:
    values = []
    for items in rep_items:
        item = items.get(key)
        if not item:
            continue
        if side == "change":
            v = item.get(metric)
        elif side == "paired":
            changes = (items.get("paired") or {}).get("changes") or {}
            v = changes.get(key)
        else:
            v = (item.get(side) or {}).get(metric)
        if v is not None:
            values.append(float(v))
    return values


def _domain_breakdown(
    persons: list[dict[str, Any]],
    weights: list[float],
    program_key: str,
    threshold: float,
    days: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dim in DIMENSIONS:
        cats: dict[str, dict[str, float]] = {}
        for p, w in zip(persons, weights):
            bucket = cats.setdefault(p[dim], {"w": 0.0, "minutes": 0.0, "listeners_w": 0.0, "n": 0, "listeners": 0})
            m = float(p["program_minutes"].get(program_key, 0.0))
            bucket["w"] += w
            bucket["minutes"] += w * m
            bucket["n"] += 1
            if m >= threshold:
                bucket["listeners_w"] += w
                bucket["listeners"] += 1
        rendered = {}
        for cat, b in sorted(cats.items()):
            rendered[cat] = {
                "reach_persons": round(b["listeners_w"], 3),
                "reach_rate": round(b["listeners_w"] / b["w"], 6) if b["w"] else None,
                "mean_minutes_per_person": round(b["minutes"] / b["w"], 3) if b["w"] else None,
                "mean_minutes_per_person_day": round(b["minutes"] / b["w"] / days, 3) if b["w"] and days else None,
                "unweighted_n": b["n"],
                "listener_unweighted_n": b["listeners"],
                "sample_weight": round(b["w"], 3),
                "reliability": "low" if b["n"] < 5 else "adequate",
            }
        out[dim] = rendered
    return out


def _stage_estimate(db, conn, cur, comparison_id) -> dict[str, Any]:
    row = _row(cur, comparison_id)
    config = _config(row)
    periods = loads(row["periods"])
    bootstrap = loads(row["bootstrap"]) or {}
    order = loads(row["decomposition_order"])["order"]
    threshold = float(config["thresholds"]["reach_minutes"])
    min_cell = int(config["thresholds"]["min_cell_persons"])
    mapping = load_mapping(cur, row["mapping_seq"])
    days = {p: int(periods[p]["survey_period"]["days"]) for p in PERIODS}

    # The two periods were calibrated in build_periods. Read their stored
    # summaries; a period that failed there would already have stopped the run.
    period_calibration = {}
    for period in PERIODS:
        prow = _period_row(cur, comparison_id, period)
        if prow is None:
            raise invalid("revamp_period_missing", f"{period} period was not built")
        period_calibration[period] = loads(prow["calibration"])

    data = {}
    for period in PERIODS:
        persons = _load_period_persons(cur, comparison_id, period, eligible_only=True)
        data[period] = {
            "persons": persons,
            "weights": [p["reference_weight"] for p in persons],
            "own_weights": [
                p["own_target_weight"] if p["own_target_weight"] is not None else p["reference_weight"]
                for p in persons
            ],
        }

    rep_items = _replicate_items(cur, comparison_id)
    item_payloads: list[dict[str, Any]] = []
    comparable_items = [l for l in mapping["links"] if l["comparable"]]
    for link in comparable_items:
        key = link["comparison_key"]
        bkey, akey = link["before_program_key"], link["after_program_key"]
        bstat = _statistics(data["before"]["persons"], data["before"]["weights"], bkey, threshold, days["before"])
        astat = _statistics(data["after"]["persons"], data["after"]["weights"], akey, threshold, days["after"])
        b_own = _statistics(data["before"]["persons"], data["before"]["own_weights"], bkey, threshold, days["before"])
        a_own = _statistics(data["after"]["persons"], data["after"]["own_weights"], akey, threshold, days["after"])
        change = {
            "mean_minutes_per_person": round(astat["mean_minutes_per_person"] - bstat["mean_minutes_per_person"], 6),
            "mean_minutes_per_person_day": round(
                astat["mean_minutes_per_person_day"] - bstat["mean_minutes_per_person_day"], 6
            ),
            "reach_rate": round(astat["reach_rate"] - bstat["reach_rate"], 6),
            "reach_persons": round(astat["reach_persons"] - bstat["reach_persons"], 6),
        }
        intervals = {
            "change_mean_minutes_per_person": _interval(
                _stat_values(rep_items, key, "change", "change_mean_minutes_per_person")
            ),
            "change_reach_rate": _interval(_stat_values(rep_items, key, "change", "change_reach_rate")),
            "before_mean_minutes_per_person": _interval(
                _stat_values(rep_items, key, "before", "mean_minutes_per_person")
            ),
            "after_mean_minutes_per_person": _interval(
                _stat_values(rep_items, key, "after", "mean_minutes_per_person")
            ),
        }
        # Composition / behaviour split on the joint calibration cells, using
        # each side's *uncalibrated design weight* so the split describes the
        # achieved sample composition rather than the calibration that removes
        # it. The before-side metric uses the before key and vice versa.
        b_persons = data["before"]["persons"]
        a_persons = data["after"]["persons"]
        comp = _joint_decomposition(
            b_persons,
            a_persons,
            bkey,
            akey,
            order,
            min_cell,
            days,
        )
        paired = _paired_item(cur, comparison_id, bkey, akey, days, rep_items, key)
        item = {
            "comparison_key": key,
            "relation": link["relation"],
            "comparable": True,
            "title_before": _program_title(cur, periods["before"]["program_batch_seq"], bkey),
            "title_after": _program_title(cur, periods["after"]["program_batch_seq"], akey),
            "slots": {
                "before": link.get("before_slots"),
                "after": link.get("after_slots"),
                "before_scheduled": link.get("before_scheduled_slots"),
                "after_scheduled": link.get("after_scheduled_slots"),
            },
            "before": bstat,
            "after": astat,
            "before_own_population_variant": b_own,
            "after_own_population_variant": a_own,
            "unweighted_diary_average": {
                "before": round(_naive_mean(data["before"]["persons"], bkey), 6),
                "after": round(_naive_mean(data["after"]["persons"], akey), 6),
                "change": round(
                    _naive_mean(data["after"]["persons"], akey)
                    - _naive_mean(data["before"]["persons"], bkey),
                    6,
                ),
                "note": (
                    "Plain mean over returned diaries. It answers 'what did the average returned diary "
                    "record', not 'what did the reference population listen'. It is shown only so the "
                    "difference from the calibrated change is visible."
                ),
            },
            "change_under_fixed_reference": change,
            "intervals": intervals,
            "decomposition": {
                **comp,
                "baseline": (
                    "Computed on uncalibrated design weights, before the fixed-reference calibration, so "
                    "the composition part reflects who returned diaries. The calibrated comparable change "
                    "above is the number to quote; the split explains why the naive diary average moves."
                ),
            },
            "paired": paired,
            "by_domain": {
                "before": _domain_breakdown(data["before"]["persons"], data["before"]["weights"], bkey, threshold, days["before"]),
                "after": _domain_breakdown(data["after"]["persons"], data["after"]["weights"], akey, threshold, days["after"]),
            },
        }
        item_payloads.append(item)

    # New and discontinued items: listed with their own estimates but never
    # combined into the comparable change.
    standalone = []
    for link in mapping["links"]:
        if link["relation"] not in ("new", "discontinued"):
            continue
        if link["relation"] == "new":
            stat = _statistics(data["after"]["persons"], data["after"]["weights"], link["after_program_key"], threshold, days["after"])
            standalone.append(
                {
                    "comparison_key": link["comparison_key"],
                    "relation": "new",
                    "title": _program_title(cur, periods["after"]["program_batch_seq"], link["after_program_key"]),
                    "period": "after",
                    "estimates": stat,
                    "included_in_comparable_change": False,
                }
            )
        else:
            stat = _statistics(data["before"]["persons"], data["before"]["weights"], link["before_program_key"], threshold, days["before"])
            standalone.append(
                {
                    "comparison_key": link["comparison_key"],
                    "relation": "discontinued",
                    "title": _program_title(cur, periods["before"]["program_batch_seq"], link["before_program_key"]),
                    "period": "before",
                    "estimates": stat,
                    "included_in_comparable_change": False,
                }
            )

    overall = _overall_change(cur, comparison_id, data, days, threshold, rep_items)
    item_payloads_out = {"comparable": item_payloads, "new_and_discontinued": standalone, "overall_listening": overall}

    cur.execute("DELETE FROM revamp_comparison_items WHERE comparison_id=%s", (str(comparison_id),))
    cur.execute(
        "INSERT INTO revamp_comparison_items (comparison_id, comparison_key, payload) VALUES (%s,%s,%s)",
        (str(comparison_id), "summary", jb(item_payloads_out)),
    )
    for item in item_payloads:
        cur.execute(
            "INSERT INTO revamp_comparison_items (comparison_id, comparison_key, payload) VALUES (%s,%s,%s)",
            (str(comparison_id), item["comparison_key"], jb(item)),
        )
    # Persist each period's per-program estimates next to its sample metadata.
    for period in PERIODS:
        cur.execute(
            "UPDATE revamp_comparison_periods SET estimates=%s WHERE comparison_id=%s AND period_key=%s",
            (
                jb(
                    {
                        "status": "ok",
                        "own_programs": _period_program_estimates(
                            data[period]["persons"],
                            data[period]["weights"],
                            data[period]["own_weights"],
                            threshold,
                            days[period],
                        ),
                    }
                ),
                str(comparison_id),
                period,
            ),
        )
    cur.execute(
        "UPDATE revamp_comparisons SET calibration=%s, updated_at=now() WHERE comparison_id=%s",
        (
            jb(
                {
                    "status": "ok",
                    "before": period_calibration["before"],
                    "after": period_calibration["after"],
                    "reference_target_batch_seq": row["reference_target_batch_seq"],
                    "note": "Both periods were calibrated to the same fixed reference population before any change was computed.",
                }
            ),
            str(comparison_id),
        ),
    )
    return {
        "comparable_items": len(item_payloads),
        "new_or_discontinued_items": len(standalone),
        "intervals": "emitted" if bootstrap.get("intervals_allowed") else "suppressed",
    }


def _period_program_estimates(persons, weights, own_weights, threshold, days) -> dict[str, Any]:
    """Every program the period itself schedules, under both calibrations."""
    keys = sorted({k for p in persons for k in p["program_minutes"]})
    return {
        key: {
            "reference_population": _statistics(persons, weights, key, threshold, days),
            "own_population_variant": _statistics(persons, own_weights, key, threshold, days),
        }
        for key in keys
    }


def _naive_mean(persons: list[dict[str, Any]], program_key: str) -> float:
    if not persons:
        return 0.0
    return sum(float(p["program_minutes"].get(program_key, 0.0)) for p in persons) / len(persons)


def _period_row(cur, comparison_id, period: str) -> dict[str, Any]:
    cur.execute(
        "SELECT * FROM revamp_comparison_periods WHERE comparison_id=%s AND period_key=%s",
        (str(comparison_id), period),
    )
    return cur.fetchone()


def _program_title(cur, batch_seq: int, program_key: str) -> str | None:
    cur.execute(
        "SELECT MIN(title) AS t FROM program_airings WHERE batch_seq=%s AND program_key=%s",
        (batch_seq, program_key),
    )
    row = cur.fetchone()
    return row["t"] if row else None


def _joint_decomposition(
    b_persons: list[dict[str, Any]],
    a_persons: list[dict[str, Any]],
    bkey: str,
    akey: str,
    order: str,
    min_cell: int,
    days: dict[str, int],
) -> dict[str, Any]:
    """Composition/behaviour split on uncalibrated design weights.

    The two sides use their own program key, so a revamp is compared old-key
    against new-key. Minutes are per person-day so unequal period lengths do
    not leak into the composition term.
    """
    # Imported here to keep the module's public import surface small.
    from .analytics.revamp import composition_decomposition

    result = composition_decomposition(
        b_persons,
        [float(p["base_weight"]) for p in b_persons],
        a_persons,
        [float(p["base_weight"]) for p in a_persons],
        lambda p: float(p["program_minutes"].get(bkey, 0.0)) / days["before"],
        DIMENSIONS,
        order,
        min_cell,
        value_of_after=lambda p: float(p["program_minutes"].get(akey, 0.0)) / days["after"],
    )
    result["metric"] = "mean_minutes_per_person_day_under_uncalibrated_design_weights"
    # Explicit aliases: the report speaks of an "uncalibrated" change, while the
    # pure function describes it as "under the common (uncalibrated) reference".
    result["change_uncalibrated"] = result.get("change_under_common_reference")
    if result["status"] == "ok":
        result["before_uncalibrated_mean"] = round(
            sum(r["before_share"] * r["before_rate"] for r in result["cells"]), 6
        )
        result["after_uncalibrated_mean"] = round(
            sum(r["after_share"] * r["after_rate"] for r in result["cells"]), 6
        )
    return result


def _paired_item(
    cur,
    comparison_id,
    bkey: str,
    akey: str,
    days: dict[str, int],
    rep_items: list[dict[str, Any]],
    comparison_key: str,
) -> dict[str, Any]:
    paired_row = loads(_row(cur, comparison_id)["paired"]) or {}
    if not paired_row.get("change_available"):
        return {
            "status": paired_row.get("status") or "unavailable",
            "change_available": False,
            "reason": paired_row.get("reason"),
            "noncoverable": paired_row.get("noncoverable"),
        }
    paired_persons = _load_paired_persons(cur, comparison_id)
    weights = [p["paired_weight"] for p in paired_persons]
    before_vals = [float(p["program_minutes"].get(bkey, 0.0)) for p in paired_persons]
    after_map = _after_minutes(cur, comparison_id)
    after_vals = [float(after_map.get(p["person_id"], {}).get(akey, 0.0)) for p in paired_persons]
    before_by_id = {p["person_id"]: v for p, v in zip(paired_persons, before_vals)}
    after_by_id = {p["person_id"]: v for p, v in zip(paired_persons, after_vals)}
    from .analytics.revamp import paired_change as _paired_change

    summary = _paired_change(
        paired_persons,
        weights,
        lambda p: before_by_id[p["person_id"]],
        lambda p: after_by_id[p["person_id"]],
    )
    total_w = sum(weights)
    before_mean = summary["before_estimate"]
    after_mean = summary["after_estimate"]
    change = summary["change"]
    change_reach = None
    threshold = float(_config(_row(cur, comparison_id))["thresholds"]["reach_minutes"])
    if total_w:
        before_reach = sum(w for w, v in zip(weights, before_vals) if v >= threshold) / total_w
        after_reach = sum(w for w, v in zip(weights, after_vals) if v >= threshold) / total_w
        change_reach = round(after_reach - before_reach, 6)
    change_values = []
    for items in rep_items:
        paired = items.get("paired") or {}
        if not paired.get("success"):
            continue
        v = (paired.get("changes") or {}).get(comparison_key)
        if v is not None:
            change_values.append(float(v))
    household_view = _paired_household_view(cur, comparison_id, paired_persons, weights, bkey, akey)
    return {
        "status": paired_row.get("status"),
        "change_available": True,
        "paired_persons": len(paired_persons),
        "before_mean_minutes_per_person": round(before_mean, 6) if before_mean is not None else None,
        "after_mean_minutes_per_person": round(after_mean, 6) if after_mean is not None else None,
        "change_mean_minutes_per_person": change,
        "change_mean_minutes_per_person_day": round(
            (after_mean / days["after"] - before_mean / days["before"]), 6
        )
        if before_mean is not None and days["before"] and days["after"]
        else None,
        "change_reach_rate": change_reach,
        "interval": _interval(change_values),
        "households": household_view,
        "noncoverable": paired_row.get("noncoverable"),
        "judgment": paired_row.get("judgment"),
        "interpretation": (
            "Same-person change, weighted by inclusion and estimated retention and re-calibrated to the fixed "
            "reference. It observes people who stayed reachable; frame absence is not covered, and the result is "
            "sensitive to the retention model."
        ),
    }


def _household_rows(cur, comparison_id) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT household_id, payload FROM revamp_comparison_households WHERE comparison_id=%s",
        (str(comparison_id),),
    )
    return cur.fetchall()


def _paired_household_view(cur, comparison_id, paired_persons, weights, bkey, akey) -> dict[str, Any]:
    after_map = _after_minutes(cur, comparison_id)
    grouped: dict[str, dict[str, Any]] = {}
    for p, w in zip(paired_persons, weights):
        b = float(p["program_minutes"].get(bkey, 0.0))
        a = float(after_map.get(p["person_id"], {}).get(akey, 0.0))
        bucket = grouped.setdefault(p["household_id"], {"members": 0, "delta": 0.0, "before": 0.0, "after": 0.0})
        bucket["members"] += 1
        bucket["delta"] += a - b
        bucket["before"] += b
        bucket["after"] += a
    unweighted = 0.0
    rows = []
    meta = {r["household_id"]: loads(r["payload"]) or {} for r in _household_rows(cur, comparison_id)}
    for hid, bucket in sorted(grouped.items()):
        mean_delta = bucket["delta"] / bucket["members"]
        m = meta.get(hid, {})
        rows.append(
            {
                "household_id": hid,
                "paired_members": bucket["members"],
                "frame_members_before": m.get("frame_members_before"),
                "all_before_members_paired": m.get("all_before_members_paired"),
                "household_mean_change": round(mean_delta, 6),
                "household_before_mean": round(bucket["before"] / bucket["members"], 6),
                "household_after_mean": round(bucket["after"] / bucket["members"], 6),
            }
        )
        unweighted += mean_delta
    return {
        "paired_households": len(grouped),
        "unweighted_mean_household_change": round(unweighted / len(grouped), 6) if grouped else None,
        "households": rows,
        "interpretation": (
            "Household means of paired members only. A household with some members not paired is flagged "
            "all_before_members_paired=false; its mean describes only the paired members."
        ),
    }


def _overall_change(
    cur,
    comparison_id,
    data: dict[str, Any],
    days: dict[str, int],
    threshold: float,
    rep_items: list[dict[str, Any]],
) -> dict[str, Any]:
    b = _overall_stats(data["before"]["persons"], data["before"]["weights"], days["before"])
    a = _overall_stats(data["after"]["persons"], data["after"]["weights"], days["after"])
    b_own = _overall_stats(data["before"]["persons"], data["before"]["own_weights"], days["before"])
    a_own = _overall_stats(data["after"]["persons"], data["after"]["own_weights"], days["after"])
    change = round(
        a["mean_listening_minutes_per_person_day"] - b["mean_listening_minutes_per_person_day"], 6
    )
    change_values = []
    for items in rep_items:
        item = items.get("overall_listening") or {}
        v = item.get("change_mean_listening_minutes_per_person_day")
        if v is not None:
            change_values.append(float(v))
    paired_row = loads(_row(cur, comparison_id)["paired"]) or {}
    paired_change_value = None
    paired_interval = {"low": None, "high": None, "replicates": 0}
    if paired_row.get("change_available"):
        pp = _load_paired_persons(cur, comparison_id)
        weights = [p["paired_weight"] for p in pp]
        total = sum(weights)
        if total:
            bt = sum(w * p["listening_minutes_total"] for p, w in zip(pp, weights)) / total
            totals = _after_totals(cur, comparison_id)
            at = sum(w * totals.get(p["person_id"], 0.0) for p, w in zip(pp, weights)) / total
            paired_change_value = round(at / days["after"] - bt / days["before"], 6)
        vals = []
        for items in rep_items:
            paired = items.get("paired") or {}
            if paired.get("success"):
                v = (paired.get("changes") or {}).get("overall_listening")
                if v is not None:
                    vals.append(float(v))
        paired_interval = _interval(vals)
    return {
        "before": {**b, "own_population_variant": b_own},
        "after": {**a, "own_population_variant": a_own},
        "change_under_fixed_reference": {
            "mean_listening_minutes_per_person_day": change,
            "interval": _interval(change_values),
        },
        "paired_change": {
            "status": paired_row.get("status"),
            "change_mean_listening_minutes_per_person_day": paired_change_value,
            "interval": paired_interval,
        },
        "interpretation": (
            "Two different comparable changes for the whole schedule: the cross-section change uses every valid "
            "diarist in each period but not identities; the paired change uses the same people twice. Neither is "
            "a causal effect of the revamp."
        ),
    }


def _stage_publish(db, conn, cur, comparison_id) -> dict[str, Any]:
    from .revamp_report import build_revamp_report

    row = _row(cur, comparison_id)
    if row["status"] == "rejected":
        raise invalid("revamp_comparison_rejected", "rejected comparisons cannot publish a report")
    cur.execute(
        "SELECT payload FROM revamp_comparison_items WHERE comparison_id=%s AND comparison_key='summary'",
        (str(comparison_id),),
    )
    summary = cur.fetchone()
    if summary is None:
        raise invalid("revamp_no_estimates", "nothing to publish")
    report = build_revamp_report(cur, comparison_id)
    report_hash = canonical_hash(report)
    cur.execute(
        "INSERT INTO revamp_reports (comparison_id, report, report_hash) VALUES (%s,%s,%s) ON CONFLICT (comparison_id) DO NOTHING",
        (str(comparison_id), jb(report), report_hash),
    )
    cur.execute(
        "UPDATE revamp_comparisons SET status='published', published_at=now(), finished_at=now(), updated_at=now() WHERE comparison_id=%s",
        (str(comparison_id),),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES ('system','revamp_comparison.publish','revamp_comparison',%s,%s)",
        (str(comparison_id), jb({"report_hash": report_hash})),
    )
    conn.commit()
    return {"report_hash": report_hash, "published": True}
