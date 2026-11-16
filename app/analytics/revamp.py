"""Pure math for the two-period revamp comparison.

Three distinct quantities are computed here; none of them is a causal effect:

1. **Cross-section change.** Both periods' achieved samples are calibrated to
   the *same* fixed reference population, then a program's after estimate is
   compared with its before estimate. This uses every valid respondent in each
   period but does not know who the same people are.

2. **Composition decomposition.** The cross-section change is split into a
   sample-composition part and a behaviour part with a two-part Oaxaca
   decomposition on joint calibration cells. The split depends on which period's
   composition is held fixed, so the order is an input that is stored with the
   report. The two parts add exactly to the change; they are accounting, not
   causal attribution.

3. **Paired (both-period) change.** People eligible in both periods are
   reweighted by their before-period inclusion probability and their estimated
   retention probability, then calibrated to the same reference population.
   This observes the same people changing, but it can only represent people who
   stayed reachable in both frames.

Every function returns explicit status codes instead of guessing when a cell has
too little sample.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence


# ---------------------------------------------------------------------------
# Weighted cell summaries
# ---------------------------------------------------------------------------
def cell_key(person: dict[str, Any], dimensions: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(person[d]) for d in dimensions)


def cell_stats(
    persons: Sequence[dict[str, Any]],
    weights: Sequence[float],
    value_of: Callable[[dict[str, Any]], float | None],
    dimensions: Sequence[str],
) -> dict[tuple[str, ...], dict[str, float]]:
    """Weighted cell count, weight and value total.

    ``value_of`` returning ``None`` means the metric is undefined for that person
    (for example a listener mean with no listener). Such a person still counts in
    the cell weight, which keeps the shares population-based.
    """
    out: dict[tuple[str, ...], dict[str, float]] = {}
    for person, w in zip(persons, weights):
        key = cell_key(person, dimensions)
        bucket = out.setdefault(key, {"weight": 0.0, "value": 0.0, "n": 0, "value_n": 0})
        bucket["weight"] += w
        bucket["n"] += 1
        value = value_of(person)
        if value is not None:
            bucket["value"] += w * value
            bucket["value_n"] += 1
    return out


def _rates(stats: dict[tuple[str, ...], dict[str, float]], total_weight: float) -> dict[tuple[str, ...], dict[str, float]]:
    out = {}
    for key, bucket in stats.items():
        share = bucket["weight"] / total_weight if total_weight else 0.0
        rate = bucket["value"] / bucket["weight"] if bucket["weight"] else None
        out[key] = {**bucket, "share": share, "rate": rate}
    return out


def composition_decomposition(
    before_persons: Sequence[dict[str, Any]],
    before_weights: Sequence[float],
    after_persons: Sequence[dict[str, Any]],
    after_weights: Sequence[float],
    value_of: Callable[[dict[str, Any]], float | None],
    dimensions: Sequence[str],
    order: str,
    min_cell_persons: int,
    value_of_after: Callable[[dict[str, Any]], float | None] | None = None,
) -> dict[str, Any]:
    """Split ``E_after - E_before`` into composition and behaviour parts.

    ``order="before_shares"``::

        composition = sum_c (s_a,c - s_b,c) * m_b,c     (before-cell rates)
        behaviour   = sum_c  s_a,c * (m_a,c - m_b,c)    (after-cell shares)

    ``order="after_shares"`` swaps which side supplies shares and which rates::

        composition = sum_c (s_a,c - s_b,c) * m_a,c
        behaviour   = sum_c  s_b,c * (m_a,c - m_b,c)

    Both identities are exact. The order changes how the interaction between
    composition and rates is booked, so it must travel with the report.

    ``value_of_after`` lets the two sides use different measures (a revamp
    compares the old program key against the new one); it defaults to the same
    ``value_of`` for a single program measured in both periods.
    """
    after_value = value_of_after or value_of
    b_stats = _rates(cell_stats(before_persons, before_weights, value_of, dimensions),
                     sum(before_weights))
    a_stats = _rates(cell_stats(after_persons, after_weights, after_value, dimensions),
                     sum(after_weights))
    all_cells = sorted(set(b_stats) | set(a_stats))
    missing: list[dict[str, Any]] = []
    sparse: list[dict[str, Any]] = []
    for cell in all_cells:
        b = b_stats.get(cell)
        a = a_stats.get(cell)
        if b is None or a is None or b["rate"] is None or a["rate"] is None or b["weight"] <= 0 or a["weight"] <= 0:
            missing.append(
                {
                    "cell": dict(zip(dimensions, cell)),
                    "before_persons": 0 if b is None else b["n"],
                    "after_persons": 0 if a is None else a["n"],
                    "reason": "no_sample_in_cell",
                }
            )
            continue
        if b["n"] < min_cell_persons or a["n"] < min_cell_persons:
            sparse.append(
                {
                    "cell": dict(zip(dimensions, cell)),
                    "before_persons": b["n"],
                    "after_persons": a["n"],
                    "min_cell_persons": min_cell_persons,
                }
            )

    before_total = sum(b["share"] * b["rate"] for b in b_stats.values() if b["rate"] is not None)
    after_total = sum(a["share"] * a["rate"] for a in a_stats.values() if a["rate"] is not None)
    change = after_total - before_total

    if missing or sparse:
        return {
            "status": "paused_insufficient_cells",
            "order": order,
            "change_under_common_reference": round(change, 6),
            "composition_part": None,
            "behaviour_part": None,
            "missing_cells": missing,
            "sparse_cells": sparse,
            "min_cell_persons": min_cell_persons,
            "reason": (
                "At least one joint calibration cell has no usable sample or falls below the minimum cell "
                "size. The comparable change is still shown, but no behaviour/composition split is asserted."
            ),
        }

    composition = 0.0
    behaviour = 0.0
    rows = []
    for cell in all_cells:
        b = b_stats[cell]
        a = a_stats[cell]
        if order == "before_shares":
            comp = (a["share"] - b["share"]) * b["rate"]
            beh = a["share"] * (a["rate"] - b["rate"])
        elif order == "after_shares":
            comp = (a["share"] - b["share"]) * a["rate"]
            beh = b["share"] * (a["rate"] - b["rate"])
        else:
            raise ValueError(f"unknown decomposition order {order!r}")
        composition += comp
        behaviour += beh
        rows.append(
            {
                "cell": dict(zip(dimensions, cell)),
                "before_share": round(b["share"], 8),
                "after_share": round(a["share"], 8),
                "before_rate": round(b["rate"], 8),
                "after_rate": round(a["rate"], 8),
                "composition_part": round(comp, 8),
                "behaviour_part": round(beh, 8),
                "before_persons": b["n"],
                "after_persons": a["n"],
            }
        )
    return {
        "status": "ok",
        "order": order,
        "change_under_common_reference": round(change, 6),
        "composition_part": round(composition, 6),
        "behaviour_part": round(behaviour, 6),
        "identity_residual": round(change - composition - behaviour, 9),
        "cells": rows,
        "min_cell_persons": min_cell_persons,
        "interpretation": (
            "Accounting split, not a causal effect: 'behaviour' is the part left after holding composition "
            "fixed at the stated side, and it still includes unmodelled differences between the two samples."
        ),
    }


# ---------------------------------------------------------------------------
# Retention model and paired weights
# ---------------------------------------------------------------------------
def retention_table(
    before_eligible: Sequence[dict[str, Any]],
    after_frame_ids: Iterable[str],
    after_eligible_ids: Iterable[str],
    dimensions: Sequence[str],
    *,
    retention_floor: float,
    min_paired_cell_persons: int,
    max_absent_share: float,
) -> dict[str, Any]:
    """Per-cell retention and frame-absence of the before-period sample.

    * ``present``: before-eligible people who appear in the after recruitment
      frame at all.
    * ``paired``: those also eligible in the after period (completed diaries).
    * ``absent_from_after_frame``: before-eligible people the after frame does
      not contain. No weighting can bring them back; they are reported as
      non-coverable by the paired design.
    * ``attrition``: present but not paired.
    """
    after_frame = set(after_frame_ids)
    after_eligible = set(after_eligible_ids)
    cells: dict[tuple[str, ...], dict[str, Any]] = {}
    for person in before_eligible:
        key = cell_key(person, dimensions)
        bucket = cells.setdefault(
            key,
            {
                "before_eligible": 0,
                "present": 0,
                "paired": 0,
                "absent": 0,
                "attrition": 0,
                "before_weight": 0.0,
                "absent_before_weight": 0.0,
            },
        )
        bucket["before_eligible"] += 1
        weight = float(person.get("base_weight") or 0.0)
        bucket["before_weight"] += weight
        pid = person["person_id"]
        if pid in after_frame:
            bucket["present"] += 1
            if pid in after_eligible:
                bucket["paired"] += 1
            else:
                bucket["attrition"] += 1
        else:
            bucket["absent"] += 1
            bucket["absent_before_weight"] += weight

    rows = []
    problems: list[dict[str, Any]] = []
    by_cell: dict[tuple[str, ...], float | None] = {}
    for key, b in sorted(cells.items()):
        present = b["present"]
        retention = (b["paired"] / present) if present else None
        absent_share = b["absent"] / b["before_eligible"] if b["before_eligible"] else 0.0
        by_cell[key] = retention
        flags = []
        if present == 0:
            flags.append("missing_present_sample")
        elif b["paired"] == 0:
            flags.append("no_paired_persons")
        elif b["paired"] < min_paired_cell_persons:
            flags.append("sparse_paired_cell")
        if retention is not None and retention < retention_floor:
            flags.append("retention_below_floor")
        if absent_share > max_absent_share:
            flags.append("frame_absence_above_limit")
        if flags:
            problems.append(
                {
                    "cell": dict(zip(dimensions, key)),
                    "flags": flags,
                    "before_eligible": b["before_eligible"],
                    "present_in_after_frame": present,
                    "paired": b["paired"],
                    "retention_probability": None if retention is None else round(retention, 6),
                    "absent_from_after_frame": b["absent"],
                    "absent_share": round(absent_share, 6),
                }
            )
        rows.append(
            {
                "cell": dict(zip(dimensions, key)),
                "before_eligible": b["before_eligible"],
                "present_in_after_frame": present,
                "paired": b["paired"],
                "attrition": b["attrition"],
                "absent_from_after_frame": b["absent"],
                "retention_probability": None if retention is None else round(retention, 6),
                "absent_share": round(absent_share, 6),
                "flags": flags,
            }
        )

    total_paired = sum(b["paired"] for b in cells.values())
    total_before = sum(b["before_eligible"] for b in cells.values())
    total_present = sum(b["present"] for b in cells.values())
    total_absent = sum(b["absent"] for b in cells.values())
    # Non-coverable counts by each single dimension, so "old listeners left" is
    # visible as a group fact and not only as a total.
    absent_by_dimension: dict[str, dict[str, int]] = {d: {} for d in dimensions}
    before_by_dimension: dict[str, dict[str, int]] = {d: {} for d in dimensions}
    for cell, b in cells.items():
        for dim, cat in zip(dimensions, cell):
            absent_by_dimension[dim][cat] = absent_by_dimension[dim].get(cat, 0) + b["absent"]
            before_by_dimension[dim][cat] = before_by_dimension[dim].get(cat, 0) + b["before_eligible"]
    noncoverable_by_group = {
        dim: {
            cat: {
                "absent_from_after_frame": absent_by_dimension[dim].get(cat, 0),
                "before_eligible": before_by_dimension[dim].get(cat, 0),
                "absent_share": round(
                    absent_by_dimension[dim].get(cat, 0) / before_by_dimension[dim][cat], 6
                )
                if before_by_dimension[dim].get(cat)
                else None,
            }
            for cat in sorted(before_by_dimension[dim])
        }
        for dim in dimensions
    }
    status = "ok" if (total_paired > 0 and not problems) else "paused_insufficient_paired_sample"
    return {
        "status": status,
        "dimensions": list(dimensions),
        "before_eligible": total_before,
        "present_in_after_frame": total_present,
        "paired": total_paired,
        "attrition": total_present - total_paired,
        "absent_from_after_frame": total_absent,
        "absent_share": round(total_absent / total_before, 6) if total_before else None,
        "retention_floor": retention_floor,
        "min_paired_cell_persons": min_paired_cell_persons,
        "max_absent_share": max_absent_share,
        "cells": rows,
        "problems": problems,
        "noncoverable_by_group": noncoverable_by_group,
        "by_cell_retention": by_cell,
        "interpretation": (
            "The paired design can only represent people who are present in the after-period frame and who "
            "returned a complete after-period diary. Frame absence is non-coverable; attrition is handled only "
            "through the retention weights and is sensitive to this model."
        ),
    }


def paired_base_weights(
    paired_persons: Sequence[dict[str, Any]],
    retention: dict[str, Any],
    dimensions: Sequence[str],
) -> list[float]:
    """Base weight = (1 / before inclusion probability) / retention(cell)."""
    by_cell = retention["by_cell_retention"]
    out = []
    for person in paired_persons:
        key = cell_key(person, dimensions)
        r = by_cell.get(key)
        if not r or r <= 0:
            raise ValueError(f"paired person {person['person_id']} has no usable retention probability")
        out.append(float(person["base_weight"]) / r)
    return out


# ---------------------------------------------------------------------------
# Paired change summaries
# ---------------------------------------------------------------------------
def paired_change(
    paired_persons: Sequence[dict[str, Any]],
    weights: Sequence[float],
    before_value: Callable[[dict[str, Any]], float],
    after_value: Callable[[dict[str, Any]], float],
) -> dict[str, Any]:
    total = 0.0
    before_sum = 0.0
    after_sum = 0.0
    delta_sum = 0.0
    for person, w in zip(paired_persons, weights):
        b = before_value(person)
        a = after_value(person)
        total += w
        before_sum += w * b
        after_sum += w * a
        delta_sum += w * (a - b)
    if not total:
        return {
            "paired_persons": len(paired_persons),
            "before_estimate": None,
            "after_estimate": None,
            "change": None,
        }
    return {
        "paired_persons": len(paired_persons),
        "before_estimate": round(before_sum / total, 6),
        "after_estimate": round(after_sum / total, 6),
        "change": round(delta_sum / total, 6),
    }


# ---------------------------------------------------------------------------
# Small helpers shared with the report
# ---------------------------------------------------------------------------
def share_table(
    persons: Sequence[dict[str, Any]],
    weights: Sequence[float],
    dimensions: Sequence[str],
) -> dict[tuple[str, ...], float]:
    stats = cell_stats(persons, weights, lambda _p: 0.0, dimensions)
    total = sum(b["weight"] for b in stats.values())
    return {k: (b["weight"] / total if total else 0.0) for k, b in stats.items()}
