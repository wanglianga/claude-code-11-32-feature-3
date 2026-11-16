"""Assemble the immutable two-period revamp comparison report.

The report keeps the two explanations apart on purpose. It never prints a single
sentence such as "the revamp raised listening". Each comparable item carries:

* the before estimate and the after estimate *under the same fixed reference
  population*, next to each period's own-population variant;
* the cross-section change under that reference, with a bootstrap interval;
* the composition/behaviour accounting split, with the order that was used and
  the cell detail behind it;
* the paired (both-period) change, its interval, the households behind it, and
  the part the paired design cannot cover;
* the groups each period covers poorly.

New and discontinued programs are listed in their own section. Sparse or empty
strata, missing reference cells and retention-model problems are reported as
paused judgement, not smoothed over.
"""
from __future__ import annotations

from typing import Any

from .jsonutil import loads


def build_revamp_report(cur, comparison_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM revamp_comparisons WHERE comparison_id=%s", (str(comparison_id),))
    comp = cur.fetchone()
    periods_payload = loads(comp["periods"])
    mapping = _mapping_payload(cur, comp["mapping_seq"])
    weight_version = loads(comp["weight_version"])

    cur.execute(
        "SELECT period_key, survey_period, input_hash, eligible_persons, eligible_households, "
        "eligible_weighted_population, calibration, own_target_calibration, coverage, strata, estimates "
        "FROM revamp_comparison_periods WHERE comparison_id=%s",
        (str(comparison_id),),
    )
    period_rows = {r["period_key"]: r for r in cur.fetchall()}

    cur.execute(
        "SELECT comparison_key, payload FROM revamp_comparison_items WHERE comparison_id=%s",
        (str(comparison_id),),
    )
    items = {r["comparison_key"]: loads(r["payload"]) for r in cur.fetchall()}
    summary = items.pop("summary")

    comparable = []
    for item in summary["comparable"]:
        comparable.append(item)

    per_period = {}
    for period in ("before", "after"):
        row = period_rows.get(period)
        if row is None:
            per_period[period] = None
            continue
        per_period[period] = {
            "survey_period": loads(row["survey_period"]),
            "eligible_persons": row["eligible_persons"],
            "eligible_households": row["eligible_households"],
            "weighted_population_under_reference": float(row["eligible_weighted_population"] or 0.0),
            "reference_calibration": loads(row["calibration"]),
            "own_target_calibration": loads(row["own_target_calibration"]),
            "coverage": loads(row["coverage"]),
            "strata": loads(row["strata"]),
            # Every program the period itself schedules, under both calibrations.
            # This is the raw per-period panel: it never ranks programs.
            "own_programs": (loads(row["estimates"]) or {}).get("own_programs"),
        }

    cur.execute(
        "SELECT period_key, exclusion_code, COUNT(*) AS c FROM revamp_comparison_persons "
        "WHERE comparison_id=%s AND NOT eligible GROUP BY period_key, exclusion_code ORDER BY period_key, exclusion_code",
        (str(comparison_id),),
    )
    excluded_by_period: dict[str, dict[str, Any]] = {}
    for r in cur.fetchall():
        excluded_by_period.setdefault(r["period_key"], {})[r["exclusion_code"] or "unknown"] = r["c"]

    paired = loads(comp["paired"]) or {}
    bootstrap = loads(comp["bootstrap"]) or {}
    calibration = loads(comp["calibration"]) or {}
    correction = loads(comp["correction"]) or {}

    # A program-level judgement summarising what is and is not asserted.
    judgments = []
    for period in ("before", "after"):
        strata = per_period[period]["strata"]
        cov = per_period[period]["coverage"]
        if strata["strata_without_eligible_sample"]:
            judgments.append(
                {
                    "period": period,
                    "item": "stratum",
                    "status": "paused_missing_sample",
                    "detail": strata["strata_without_eligible_sample"],
                }
            )
        if strata["sparse_strata"]:
            judgments.append(
                {
                    "period": period,
                    "item": "stratum",
                    "status": "paused_sparse_sample",
                    "detail": strata["sparse_strata"],
                }
            )
        if cov["no_sample_groups"]:
            judgments.append(
                {
                    "period": period,
                    "item": "group",
                    "status": "paused_no_sample",
                    "detail": cov["no_sample_groups"],
                }
            )
    for item in comparable:
        comp_part = item["decomposition"]
        if comp_part["status"] != "ok":
            judgments.append(
                {
                    "item": "decomposition",
                    "comparison_key": item["comparison_key"],
                    "status": "paused_insufficient_cells",
                    "detail": {
                        "missing": comp_part.get("missing_cells"),
                        "sparse": comp_part.get("sparse_cells"),
                    },
                }
            )
    if paired.get("judgment", {}).get("status") not in (None, "ok"):
        judgments.append(
            {
                "item": "paired",
                "status": paired["judgment"]["status"],
                "detail": paired["judgment"].get("reasons"),
            }
        )

    report = {
        "comparison_id": str(comp["comparison_id"]),
        "status": "published",
        "parent_comparison_id": str(comp["parent_comparison_id"])
        if comp["parent_comparison_id"]
        else None,
        "correction": correction,
        "note": comp["note"],
        "seed": comp["seed"],
        "report_version": "revamp-comparison/1",
        "generated_from_inputs": {
            "mapping": {
                "mapping_seq": comp["mapping_seq"],
                "confirmed_by": mapping["confirmed_by"],
                "confirmed_role": mapping["confirmed_role"],
                "confirmed_at": mapping["confirmed_at"],
                "payload_hash": mapping["payload_hash"],
                "slot_disclaimer": mapping["slot_disclaimer"],
                "links": mapping["links"],
            },
            "reference_target_batch_seq": comp["reference_target_batch_seq"],
            "periods": {
                p: {
                    "survey_period": periods_payload[p]["survey_period"],
                    "recruitment_batch_seq": periods_payload[p]["recruitment_batch_seq"],
                    "target_batch_seq": periods_payload[p]["target_batch_seq"],
                    "program_batch_seq": periods_payload[p]["program_batch_seq"],
                    "diary_seq_cutoff": periods_payload[p]["diary_seq_cutoff"],
                    "input_hash": periods_payload[p]["input_hash"],
                }
                for p in ("before", "after")
            },
        },
        "provenance": {
            "input_hash": comp["input_hash"],
            "config_hash": comp["config_hash"],
            "weight_version": weight_version,
            "weight_version_hash": weight_version.get("weight_version_hash"),
            "decomposition_order": loads(comp["decomposition_order"]),
            "decomposition_order_note": (
                "The composition/behaviour split depends on which period's composition is held fixed. "
                "The order used here is stored with this report and must be quoted with it."
            ),
            "calibration_approach": (
                "Both periods were calibrated to the same fixed reference population before any change was "
                "computed, so their cross-section estimates share one population definition."
            ),
            "calibration": calibration,
        },
        "periods": per_period,
        "exclusions": excluded_by_period,
        "comparable_items": comparable,
        "new_and_discontinued": summary["new_and_discontinued"],
        "overall_listening": summary["overall_listening"],
        "paired": {
            "status": paired.get("status"),
            "change_available": paired.get("change_available"),
            "paired_persons": paired.get("paired_persons"),
            "paired_households": paired.get("paired_households"),
            "retention": paired.get("retention"),
            "calibration": paired.get("calibration"),
            "noncoverable": paired.get("noncoverable"),
            "noncoverable_by_group": (paired.get("retention") or {}).get("noncoverable_by_group"),
            "judgment": paired.get("judgment"),
            "interpretation": paired.get("interpretation"),
        },
        "coverage": {
            "before": per_period["before"]["coverage"],
            "after": per_period["after"]["coverage"],
            "note": (
                "Coverage is judged on unweighted eligible diarists against the fixed reference population. "
                "Calibrated weights would always look complete by construction."
            ),
        },
        "judgments": judgments,
        "bootstrap": bootstrap,
        "interval_status": comp["interval_status"],
        "interpretation": {
            "comparable_change": (
                "A comparable change under one fixed reference population. It is not causal attribution: "
                "programme content, schedule, sample composition and coverage move together between periods."
            ),
            "cross_section_vs_paired": (
                "The cross-section estimate uses every valid diarist in each period but does not know who is "
                "who. The paired estimate watches the same people change but can only represent people who "
                "stayed reachable in both frames."
            ),
            "decomposition": (
                "The composition/behaviour split is accounting, not causal. Its order is stored in provenance; "
                "a different order books the interaction term to the other part."
            ),
            "noncoverable": (
                "People absent from the after-period frame are not covered by the paired design and are "
                "reported by count and estimated population size, not imputed."
            ),
            "never_summarise_as": "revamp improved (or reduced) listening",
            "different_explanations_require_different_checks": [
                "behaviour unchanged but sample younger: compare composition_part with behaviour_part",
                "same people switched: read the paired change and its interval",
                "old listeners left: read frame absence, retention flags and the paired noncoverable block",
            ],
        },
        "limitations": [
            "Fictional data only; no real population register or listening device is used.",
            "Two-period changes are comparable estimates, never causal effects.",
            "Paired estimates depend on the retention model; low-retention or high-absence cells pause judgement.",
            "Sparse or empty strata suppress a definitive judgement rather than being smoothed over.",
            "A correction to one period creates a new comparison; published reports are never rewritten.",
        ],
    }
    return report


def _mapping_payload(cur, mapping_seq: int) -> dict[str, Any]:
    cur.execute(
        "SELECT payload, confirmed_by, confirmed_role, confirmed_at, payload_hash, slot_disclaimer "
        "FROM revamp_program_mappings WHERE mapping_seq=%s",
        (mapping_seq,),
    )
    row = cur.fetchone()
    body = loads(row["payload"]) or {}
    return {
        **body,
        "confirmed_by": row["confirmed_by"],
        "confirmed_role": row["confirmed_role"],
        "confirmed_at": row["confirmed_at"].isoformat(),
        "payload_hash": row["payload_hash"],
        "slot_disclaimer": row["slot_disclaimer"],
    }


def public_comparison_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "comparison_id": str(row["comparison_id"]),
        "status": row["status"],
        "parent_comparison_id": str(row["parent_comparison_id"]) if row["parent_comparison_id"] else None,
        "mapping_seq": row["mapping_seq"],
        "reference_target_batch_seq": row["reference_target_batch_seq"],
        "decomposition_order": loads(row["decomposition_order"]),
        "interval_status": row["interval_status"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
    }
