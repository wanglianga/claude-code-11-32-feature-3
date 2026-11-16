"""End-to-end tests for the two-period revamp comparison.

The fixture builds three explanations into the data; each test checks one:

* behaviour unchanged but the sample got younger (younger sample);
* the same people listened longer (paired change);
* older listeners left the frame (non-coverable).

None of them is asserted as causal.
"""
from __future__ import annotations

import copy

import pytest

from demo import revamp_fixtures as F


@pytest.fixture(scope="module")
def revamp_seeded(database, client):
    """Seed two distinct periods plus a fixed reference population."""
    from app import ingest, revamp_ingest
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    frames, targets, programs = {}, {}, {}
    rec = F.recruitment_batches()
    tgt = F.target_batches()
    prg = F.program_batches()
    for period in ("before", "after"):
        frames[period] = ingest.create_recruitment_batch(database, rec[period], "pytest-revamp")["batch_seq"]
        targets[period] = ingest.create_target_batch(database, tgt[period], "pytest-revamp")["batch_seq"]
        programs[period] = ingest.create_program_batch(database, prg[period], "pytest-revamp")["batch_seq"]
    reference = ingest.create_target_batch(database, tgt["reference"], "pytest-revamp")["batch_seq"]

    before_ids = {p["person_id"] for p in F.before_persons()}
    before_cutoff = 0
    after_cutoff = 0
    # Cutoffs are captured from this fixture's own submissions, so other tests
    # sharing the schema cannot shift them. Revision 1 of a before-period person
    # is the before cutoff; paired people resubmit at revision 2 after.
    for sub in F.diary_submissions():
        seq = ingest.create_diary_submission(database, sub, "pytest-revamp", tz)["submission_seq"]
        if sub["person_id"] in before_ids and sub["revision"] == 1:
            before_cutoff = max(before_cutoff, seq)
        after_cutoff = max(after_cutoff, seq)

    mapping = revamp_ingest.create_mapping(
        database, F.mapping_payload(programs["before"], programs["after"]), "pytest-editor"
    )
    return {
        "frames": frames,
        "targets": targets,
        "programs": programs,
        "reference": reference,
        "cutoffs": {"before": before_cutoff, "after": after_cutoff},
        "mapping_seq": mapping["mapping_seq"],
    }


def _body(seeded, mapping_seq=None, **overrides):
    body = {
        "note": "revamp-pytest",
        "mapping_seq": mapping_seq or seeded["mapping_seq"],
        "reference_target_batch_seq": seeded["reference"],
        "periods": {
            p: {
                "recruitment_batch_seq": seeded["frames"][p],
                "target_batch_seq": seeded["targets"][p],
                "program_batch_seq": seeded["programs"][p],
                "diary_seq_cutoff": seeded["cutoffs"][p],
            }
            for p in ("before", "after")
        },
        "config": {"bootstrap": {"replicates": 60}},
    }
    body.update(overrides)
    return body


def _create(client, seeded, **overrides):
    resp = client.post("/v1/revamp/comparisons", json=_body(seeded, **overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()["comparison"]["comparison_id"]


def _execute(client, comparison_id, steps=20):
    resp = client.post(f"/v1/revamp/comparisons/{comparison_id}/execute", json={"max_steps": steps})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _publish(client, seeded, **overrides):
    cid = _create(client, seeded, **overrides)
    _execute(client, cid)
    detail = client.get(f"/v1/revamp/comparisons/{cid}").json()
    report = client.get(f"/v1/revamp/comparisons/{cid}/report")
    return cid, detail, report


# ---------------------------------------------------------------------------
# Mapping (editor gate + completeness)
# ---------------------------------------------------------------------------
def test_mapping_requires_editor_key(client, revamp_seeded):
    payload = F.mapping_payload(
        revamp_seeded["programs"]["before"], revamp_seeded["programs"]["after"]
    )
    payload["mapping_ref"] = "editor-gate"
    resp = client.post("/v1/revamp/mappings", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "editor_key_required"
    resp = client.post(
        "/v1/revamp/mappings", json=payload, headers={"X-API-Key": "test-editor-key"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["confirmed_by"] == "duty-editor"
    assert body["slot_disclaimer"]


def test_mapping_must_cover_every_program(client, revamp_seeded):
    payload = F.mapping_payload(
        revamp_seeded["programs"]["before"], revamp_seeded["programs"]["after"]
    )
    payload = copy.deepcopy(payload)
    payload["mapping_ref"] = "incomplete-map"
    payload["links"] = [
        l for l in payload["links"] if l.get("after_program_key") != "new_show"
    ]
    resp = client.post(
        "/v1/revamp/mappings", json=payload, headers={"X-API-Key": "test-editor-key"}
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "mapping_incomplete"
    assert "new_show" in err["details"]["unmapped_after"]


def test_mapping_rejects_reused_before_key(client, revamp_seeded):
    payload = copy.deepcopy(
        F.mapping_payload(revamp_seeded["programs"]["before"], revamp_seeded["programs"]["after"])
    )
    payload["mapping_ref"] = "reused-key"
    payload["links"].append(
        {
            "relation": "renamed",
            "before_program_key": "morning_show",
            "after_program_key": "new_show",
            "comparison_key": "duplicate",
        }
    )
    resp = client.post(
        "/v1/revamp/mappings", json=payload, headers={"X-API-Key": "test-editor-key"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "mapping_program_reused"


# ---------------------------------------------------------------------------
# Creation validation
# ---------------------------------------------------------------------------
def test_comparison_requires_disjoint_periods(client, revamp_seeded):
    body = _body(revamp_seeded)
    body["periods"]["after"]["recruitment_batch_seq"] = body["periods"]["before"]["recruitment_batch_seq"]
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "revamp_same_frame"


def test_comparison_requires_matching_mapping_batch(client, revamp_seeded):
    body = _body(revamp_seeded)
    body["periods"]["before"]["program_batch_seq"] = revamp_seeded["programs"]["after"]
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "mapping_batch_mismatch"


# ---------------------------------------------------------------------------
# Published report structure
# ---------------------------------------------------------------------------
def test_published_report_separates_old_and_new(client, revamp_seeded):
    cid, detail, report = _publish(client, revamp_seeded)
    assert detail["status"] == "published", detail.get("failure")
    assert report.status_code == 200
    payload = report.json()
    data = payload["report"]
    assert payload["report_hash"]

    comparable = {i["comparison_key"]: i for i in data["comparable_items"]}
    assert set(comparable) == {"morning_show", "evening_show"}
    standalone = {i["comparison_key"]: i for i in data["new_and_discontinued"]}
    assert set(standalone) == {"new_show", "oldies_hour"}
    assert standalone["new_show"]["relation"] == "new"
    assert standalone["oldies_hour"]["relation"] == "discontinued"
    assert standalone["new_show"]["included_in_comparable_change"] is False

    # Every comparable item carries both estimates, the interval and the split.
    for item in comparable.values():
        assert item["before"]["mean_minutes_per_person"] >= 0
        assert item["after"]["mean_minutes_per_person"] >= 0
        assert "mean_minutes_per_person" in item["change_under_fixed_reference"]
        assert item["intervals"]["change_mean_minutes_per_person"]["low"] is not None
        assert item["decomposition"]["status"] in ("ok", "paused_insufficient_cells")
        assert item["paired"]["change_available"] is True

    # Provenance records the decomposition order and the weight version.
    assert data["provenance"]["decomposition_order"]["order"] in ("before_shares", "after_shares")
    assert data["provenance"]["weight_version_hash"]
    assert data["provenance"]["weight_version"]["paired"]["calibration_target"]


def test_report_never_says_the_revamp_raised_listening(client, revamp_seeded):
    _, _, report = _publish(client, revamp_seeded)
    data = report.json()["report"]
    assert data["interpretation"]["never_summarise_as"]
    # The report may *name* the forbidden summary in its interpretation field;
    # it must not actually state it as a finding anywhere else.
    body_without_guardrail = {k: v for k, v in data.items() if k != "interpretation"}
    serialized = str(body_without_guardrail).lower()
    for forbidden in ("revamp improved", "revamp raised", "caused by the revamp", "best program"):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# The three explanations
# ---------------------------------------------------------------------------
def test_younger_sample_moves_naive_average_but_not_comparable_change(client, revamp_seeded):
    """Behaviour unchanged; the after sample is younger.

    The plain diary average rises, the fixed-reference change is ~0, and the
    composition part absorbs the rise.
    """
    _, _, report = _publish(client, revamp_seeded)
    items = {i["comparison_key"]: i for i in report.json()["report"]["comparable_items"]}
    morning = items["morning_show"]

    naive = morning["unweighted_diary_average"]
    assert naive["after"] > naive["before"] + 10  # the naive average rises materially

    change = morning["change_under_fixed_reference"]["mean_minutes_per_person"]
    assert change == pytest.approx(0.0, abs=0.5)

    decomp = morning["decomposition"]
    assert decomp["status"] == "ok"
    assert decomp["behaviour_part"] == pytest.approx(0.0, abs=1e-6)
    assert decomp["composition_part"] > 5
    assert decomp["composition_part"] == pytest.approx(
        decomp["change_uncalibrated"], abs=1e-5
    )
    assert morning["paired"]["change_mean_minutes_per_person"] == pytest.approx(0.0, abs=1e-6)


def test_same_people_switched_shows_as_paired_change(client, revamp_seeded):
    _, _, report = _publish(client, revamp_seeded)
    items = {i["comparison_key"]: i for i in report.json()["report"]["comparable_items"]}
    evening = items["evening_show"]
    # The same 25-44 diarists go 20 -> 40 minutes.
    assert evening["paired"]["change_mean_minutes_per_person"] == pytest.approx(20.0, abs=20.0)
    assert evening["paired"]["paired_persons"] > 0
    # And the cross-section sees a same-sign change under the fixed reference.
    assert evening["change_under_fixed_reference"]["mean_minutes_per_person"] > 0
    interval = evening["paired"]["interval"]
    assert interval["low"] is not None and interval["low"] > 0


def test_old_listeners_leaving_are_reported_as_noncoverable(client, revamp_seeded):
    _, _, report = _publish(client, revamp_seeded)
    data = report.json()["report"]
    noncov = data["paired"]["noncoverable"]
    assert noncov["absent_from_after_frame"] == 8  # one in five of the 40 before-eligible 65+
    assert noncov["absent_share"] == pytest.approx(0.05)

    by_age = data["paired"]["noncoverable_by_group"]["age_band"]
    assert by_age["65+"]["absent_from_after_frame"] == 8
    assert by_age["65+"]["absent_share"] == pytest.approx(0.20)
    assert by_age["18-24"]["absent_from_after_frame"] == 0

    # The discontinued show is reported alone, never as a comparable change.
    standalone = {i["comparison_key"]: i for i in data["new_and_discontinued"]}
    assert standalone["oldies_hour"]["relation"] == "discontinued"
    assert all(i["comparison_key"] != "oldies_hour" for i in data["comparable_items"])


# ---------------------------------------------------------------------------
# Decomposition order is pinned and reported
# ---------------------------------------------------------------------------
def test_decomposition_order_is_stored_with_the_report(client, revamp_seeded):
    cid, _, report = _publish(
        client,
        revamp_seeded,
        config={
            "bootstrap": {"replicates": 30},
            "decomposition_order": "after_shares",
        },
    )
    data = report.json()["report"]
    assert data["provenance"]["decomposition_order"]["order"] == "after_shares"
    for item in data["comparable_items"]:
        assert item["decomposition"]["order"] == "after_shares"


def test_report_changes_with_decomposition_order(client, revamp_seeded):
    _, _, first = _publish(
        client,
        revamp_seeded,
        config={"bootstrap": {"replicates": 20}, "decomposition_order": "before_shares"},
    )
    _, _, second = _publish(
        client,
        revamp_seeded,
        config={"bootstrap": {"replicates": 20}, "decomposition_order": "after_shares"},
    )
    a = {i["comparison_key"]: i for i in first.json()["report"]["comparable_items"]}
    b = {i["comparison_key"]: i for i in second.json()["report"]["comparable_items"]}
    # The evening show has both composition and behaviour moving, so the split
    # differs by order while the calibrated change does not.
    assert (
        a["evening_show"]["decomposition"]["composition_part"]
        != b["evening_show"]["decomposition"]["composition_part"]
    )
    assert a["evening_show"]["change_under_fixed_reference"] == b["evening_show"][
        "change_under_fixed_reference"
    ]


# ---------------------------------------------------------------------------
# Immutability and corrections
# ---------------------------------------------------------------------------
def test_published_comparison_report_is_immutable(client, revamp_seeded, database):
    cid, _, report = _publish(client, revamp_seeded)
    first = report.json()["report_hash"]
    again = client.post(f"/v1/revamp/comparisons/{cid}/execute", json={"max_steps": 20})
    assert again.json()["message"] == "comparison already finalised"
    assert client.get(f"/v1/revamp/comparisons/{cid}/report").json()["report_hash"] == first

    import psycopg

    with database.scoped() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE revamp_reports SET report='{}'::jsonb WHERE comparison_id=%s", (cid,)
            )
        conn.rollback()


def test_period_scoped_correction_creates_child_and_keeps_parent(client, revamp_seeded, database):
    parent_id, parent_detail, parent_report = _publish(client, revamp_seeded)
    assert parent_detail["status"] == "published"
    parent_hash = parent_report.json()["report_hash"]

    # Correct only the after period: drop the last after-period diary and repin
    # its cutoff. The before period and mapping are untouched.
    after_cutoff_before = revamp_seeded["cutoffs"]["after"]
    body = _body(revamp_seeded)
    body["periods"]["after"]["diary_seq_cutoff"] = after_cutoff_before - 1
    body["correction"] = {
        "parent_comparison_id": parent_id,
        "period": "after",
        "reason": "late correction only to the after-period cutoff",
    }
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["comparison"]["comparison_id"]
    assert resp.json()["comparison"]["parent_comparison_id"] == parent_id

    _execute(client, child_id)
    child = client.get(f"/v1/revamp/comparisons/{child_id}").json()
    assert child["status"] == "published"
    child_report = client.get(f"/v1/revamp/comparisons/{child_id}/report").json()
    assert child_report["report_hash"] != parent_hash
    assert child_report["report"]["correction"]["corrected_period"] == "after"
    assert child_report["report"]["correction"]["other_period_unchanged"] is True

    # The parent report is byte-identical after the correction.
    assert client.get(f"/v1/revamp/comparisons/{parent_id}/report").json()["report_hash"] == parent_hash


def test_correction_cannot_change_the_other_period(client, revamp_seeded):
    parent_id, _, _ = _publish(client, revamp_seeded)
    body = _body(revamp_seeded)
    body["periods"]["before"]["diary_seq_cutoff"] = revamp_seeded["cutoffs"]["before"] - 1
    body["periods"]["after"]["diary_seq_cutoff"] = revamp_seeded["cutoffs"]["after"] - 1
    body["correction"] = {
        "parent_comparison_id": parent_id,
        "period": "after",
        "reason": "try to change both periods",
    }
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] in {"correction_touches_other_period", "correction_period_unchanged"}


def test_correction_requires_a_published_parent(client, revamp_seeded):
    pending = _create(client, revamp_seeded, note="not-yet-run")
    body = _body(revamp_seeded)
    body["periods"]["after"]["diary_seq_cutoff"] = revamp_seeded["cutoffs"]["after"] - 1
    body["correction"] = {
        "parent_comparison_id": pending,
        "period": "after",
        "reason": "parent never published",
    }
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "correction_parent_not_published"


# ---------------------------------------------------------------------------
# Sparse / missing strata pause the judgement
# ---------------------------------------------------------------------------
def test_sparse_stratum_pauses_judgement_and_is_reported(client, revamp_seeded):
    from app import ingest

    sparse = ingest.create_recruitment_batch(
        client.app.state.db, F.sparse_after_frame(), "pytest-revamp"
    )["batch_seq"]
    body = _body(revamp_seeded)
    body["periods"]["after"]["recruitment_batch_seq"] = sparse
    # The after cutoff keeps every diary; the two sparse-only persons never
    # return one, so their stratum has frame rows but no eligible sample.
    resp = client.post("/v1/revamp/comparisons", json=body)
    assert resp.status_code == 201, resp.text
    cid = resp.json()["comparison"]["comparison_id"]
    _execute(client, cid)
    detail = client.get(f"/v1/revamp/comparisons/{cid}").json()
    assert detail["status"] == "published", detail.get("failure")
    stages = {s["stage"]: s for s in detail["stages"]}
    build = stages["build_periods"]["payload"]
    assert "rural-remote" in build["after"]["strata"]["strata_without_eligible_sample"]
    assert build["judgment"]["status"] == "paused_sparse_or_missing_strata"
    report = client.get(f"/v1/revamp/comparisons/{cid}/report").json()["report"]
    assert any(j["item"] == "stratum" for j in report["judgments"])
    # The paused stratum is still visible in the published report, and the
    # comparison is published (paused judgement, not a silent rejection).
    assert "rural-remote" in report["periods"]["after"]["strata"]["strata_without_eligible_sample"]
    assert report["status"] == "published"


def test_each_period_reports_its_own_target_variant(client, revamp_seeded):
    _, _, report = _publish(client, revamp_seeded)
    data = report.json()["report"]
    before = data["periods"]["before"]
    after = data["periods"]["after"]
    # Both periods are on the fixed reference; the own-population variant is
    # kept alongside. The after target totals 10,100, the reference 10,000.
    assert before["own_target_calibration"]["success"] is True
    assert after["own_target_calibration"]["success"] is True
    assert before["weighted_population_under_reference"] == pytest.approx(10_000, abs=1)
    assert after["weighted_population_under_reference"] == pytest.approx(10_000, abs=1)
    # Own programs are exposed per period without any ranking language.
    for period in ("before", "after"):
        own = data["periods"][period]["own_programs"]
        assert own
        for metrics in own.values():
            assert "reference_population" in metrics
            assert "own_population_variant" in metrics


# ---------------------------------------------------------------------------
# Endpoint surfaces
# ---------------------------------------------------------------------------
def test_comparison_surfaces_expose_the_separate_views(client, revamp_seeded):
    cid, _, _ = _publish(client, revamp_seeded)
    paired = client.get(f"/v1/revamp/comparisons/{cid}/paired").json()
    assert paired["paired"]["change_available"] is True
    coverage = client.get(f"/v1/revamp/comparisons/{cid}/coverage").json()
    assert set(coverage["periods"]) == {"before", "after"}
    boot = client.get(f"/v1/revamp/comparisons/{cid}/bootstrap").json()
    assert boot["summary"]["requested"] == 60
    item = client.get(f"/v1/revamp/comparisons/{cid}/items/evening_show").json()
    assert item["item"]["comparison_key"] == "evening_show"
    persons = client.get(
        f"/v1/revamp/comparisons/{cid}/persons", params={"period": "before", "paired": True}
    ).json()
    assert persons and all(p["period_key"] == "before" and p["paired"] for p in persons)


def test_report_not_published_before_run(client, revamp_seeded):
    cid = _create(client, revamp_seeded, note="still-pending")
    resp = client.get(f"/v1/revamp/comparisons/{cid}/report")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "revamp_report_not_published"
