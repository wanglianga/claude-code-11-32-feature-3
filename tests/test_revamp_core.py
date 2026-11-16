"""Pure unit tests for the revamp comparison math."""
from __future__ import annotations

import pytest

from app.analytics.revamp import (
    cell_stats,
    composition_decomposition,
    paired_base_weights,
    paired_change,
    retention_table,
    share_table,
)


def _person(pid, age="25-44", region="north", sex="F", p=0.5, hh=None):
    return {
        "person_id": pid,
        "household_id": hh or f"H-{pid}",
        "age_band": age,
        "region": region,
        "sex": sex,
        "base_weight": 1.0 / p,
        "inclusion_probability": p,
    }


def test_composition_decomposition_identities_both_orders():
    before = [_person("b1", age="18-24"), _person("b2", age="45-64"), _person("b3", age="45-64")]
    after = [_person("a1", age="18-24"), _person("a2", age="18-24"), _person("a3", age="45-64")]
    # Both composition and within-cell rates move, so the interaction term is
    # non-zero and the two orders book it differently.
    values = {"b1": 40, "b2": 10, "b3": 10, "a1": 50, "a2": 50, "a3": 30}
    for order in ("before_shares", "after_shares"):
        result = composition_decomposition(
            before,
            [1.0] * 3,
            after,
            [1.0] * 3,
            lambda p: values[p["person_id"]],
            ("age_band",),
            order,
            1,
        )
        assert result["status"] == "ok"
        assert result["identity_residual"] == pytest.approx(0.0, abs=1e-9)
        assert result["composition_part"] + result["behaviour_part"] == pytest.approx(
            result["change_under_common_reference"], abs=1e-5
        )
    # The two orders differ: interaction is booked differently.
    first = composition_decomposition(
        before, [1.0] * 3, after, [1.0] * 3, lambda p: values[p["person_id"]],
        ("age_band",), "before_shares", 1,
    )
    second = composition_decomposition(
        before, [1.0] * 3, after, [1.0] * 3, lambda p: values[p["person_id"]],
        ("age_band",), "after_shares", 1,
    )
    assert first["composition_part"] != second["composition_part"]


def test_composition_isolates_composition_when_behaviour_is_constant():
    # Same age-specific rate in both periods; only the sample composition
    # changes. The whole move must land in the composition part.
    before = [_person("b1", age="18-24"), _person("b2", age="45-64")]
    after = [_person("a1", age="18-24"), _person("a2", age="18-24"), _person("a3", age="45-64")]
    values = {"b1": 60, "b2": 0, "a1": 60, "a2": 60, "a3": 0}
    result = composition_decomposition(
        before, [1.0] * 2, after, [1.0] * 3, lambda p: values[p["person_id"]],
        ("age_band",), "after_shares", 1,
    )
    assert result["behaviour_part"] == pytest.approx(0.0, abs=1e-9)
    assert result["composition_part"] == pytest.approx(result["change_under_common_reference"], abs=1e-6)
    assert result["composition_part"] > 0


def test_decomposition_pauses_on_missing_or_sparse_cells():
    before = [_person("b1", age="18-24")]
    after = [_person("a1", age="45-64")]
    result = composition_decomposition(
        before, [1.0], after, [1.0], lambda p: 10.0, ("age_band",), "before_shares", 3
    )
    assert result["status"] == "paused_insufficient_cells"
    assert result["composition_part"] is None
    assert result["behaviour_part"] is None

    # Sparse: both cells present but below the minimum per cell.
    before = [_person("b1", age="18-24"), _person("b2", age="45-64")]
    after = [_person("a1", age="18-24"), _person("a2", age="45-64")]
    result = composition_decomposition(
        before, [1.0] * 2, after, [1.0] * 2, lambda p: 10.0, ("age_band",), "before_shares", 5
    )
    assert result["status"] == "paused_insufficient_cells"
    assert result["sparse_cells"]


def test_retention_separates_absence_attrition_and_paired():
    before = [
        _person("p1", age="18-24"),
        _person("p2", age="18-24"),
        _person("p3", age="18-24"),
        _person("p4", age="65+"),
        _person("p5", age="65+"),
    ]
    after_frame = {"p1", "p2", "p3", "p4", "new"}
    after_eligible = {"p1", "p2", "p4"}
    result = retention_table(
        before,
        after_frame,
        after_eligible,
        ("age_band",),
        retention_floor=0.3,
        min_paired_cell_persons=1,
        max_absent_share=0.5,
    )
    assert result["paired"] == 3
    assert result["attrition"] == 1  # p3 present but not eligible
    assert result["absent_from_after_frame"] == 1  # p5 left the frame
    assert result["absent_share"] == pytest.approx(1 / 5)
    assert result["noncoverable_by_group"]["age_band"]["65+"]["absent_from_after_frame"] == 1
    by_cell = result["by_cell_retention"]
    # Retention is paired / present-in-frame, not paired / before-eligible:
    # p3 (18-24) is present but not eligible, p5 (65+) left the frame entirely.
    assert by_cell[("18-24",)] == pytest.approx(2 / 3)
    assert by_cell[("65+",)] == pytest.approx(1 / 1)


def test_retention_pauses_on_low_retention_cell():
    before = [_person(f"p{i}", age="18-24") for i in range(4)] + [
        _person(f"q{i}", age="65+") for i in range(4)
    ]
    after_frame = {f"p{i}" for i in range(4)} | {f"q{i}" for i in range(4)}
    after_eligible = {f"p{i}" for i in range(4)} | {"q0"}
    result = retention_table(
        before,
        after_frame,
        after_eligible,
        ("age_band",),
        retention_floor=0.5,
        min_paired_cell_persons=2,
        max_absent_share=0.9,
    )
    assert result["status"] == "paused_insufficient_paired_sample"
    flags = {tuple(p["cell"].values()): set(p["flags"]) for p in result["problems"]}
    assert any("retention_below_floor" in v for v in flags.values())
    assert any("sparse_paired_cell" in v for v in flags.values())


def test_retention_pauses_on_high_frame_absence():
    before = [_person(f"p{i}", age="65+") for i in range(10)]
    after_frame = {"p0", "p1"}
    after_eligible = {"p0", "p1"}
    result = retention_table(
        before,
        after_frame,
        after_eligible,
        ("age_band",),
        retention_floor=0.1,
        min_paired_cell_persons=1,
        max_absent_share=0.3,
    )
    assert result["status"] == "paused_insufficient_paired_sample"
    assert result["absent_share"] == pytest.approx(0.8)
    assert result["problems"][0]["flags"] == ["frame_absence_above_limit"]


def test_paired_base_weight_divides_design_weight_by_retention():
    persons = [_person("p1", age="18-24", p=0.5), _person("p2", age="65+", p=0.25)]
    retention = {
        "by_cell_retention": {("18-24",): 0.5, ("65+",): 0.25},
    }
    base = paired_base_weights(persons, retention, ("age_band",))
    assert base == [pytest.approx(1.0 / 0.5 / 0.5), pytest.approx(1.0 / 0.25 / 0.25)]


def test_paired_change_is_a_person_level_difference():
    persons = [
        {**_person("p1"), "before": 20.0, "after": 40.0},
        {**_person("p2"), "before": 40.0, "after": 40.0},
    ]
    result = paired_change(
        persons,
        [1.0, 3.0],
        lambda p: p["before"],
        lambda p: p["after"],
    )
    # Weighted (after - before): (1*20 + 3*0)/4 = 5
    assert result["change"] == pytest.approx(5.0)
    assert result["before_estimate"] == pytest.approx((1 * 20 + 3 * 40) / 4)


def test_cell_stats_and_share_table_agree():
    persons = [_person("p1", age="18-24"), _person("p2", age="18-24"), _person("p3", age="65+")]
    weights = [1.0, 3.0, 4.0]
    stats = cell_stats(persons, weights, lambda p: 0.0, ("age_band",))
    shares = share_table(persons, weights, ("age_band",))
    assert shares[("18-24",)] == pytest.approx(stats[("18-24",)]["weight"] / 8.0)
    assert shares[("65+",)] == pytest.approx(0.5)
