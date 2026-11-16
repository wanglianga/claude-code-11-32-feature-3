"""Fictional two-period dataset for the revamp comparison.

Three explanations are deliberately built into the data so each can be checked
separately (no dataset alone proves causality):

1. **Behaviour unchanged, sample younger.** The morning programme has exactly
   the same age-specific minutes in both periods, but the after-period frame is
   much younger: the youth cells are refreshed with many new diarists while the
   older cells change little. The *unweighted diary average* rises; calibrating
   both periods to one fixed reference population returns zero change.
2. **The same people switched.** The evening programme is revamped; the same
   25-44 diarists listen 20 minutes before and 40 minutes after. The paired
   estimate sees the switch; the cross-section estimate sees the same change
   without knowing identities.
3. **Old listeners left.** One of every five 65+ before-period diarists is
   absent from the after-period frame. They are the audience of the discontinued
   ``oldies_hour``. The paired design reports them as non-coverable rather than
   pretending they were retained or that they listened zero.

Households hold two members, so the paired design can show which households are
only partly paired (one member left) instead of silently averaging them.

Periods: 2026-06-01..03 (before) and 2026-09-01..03 (after), Asia/Shanghai.
All values are constructed; no real audience data is used.
"""
from __future__ import annotations

from datetime import date, timedelta

TZ = "Asia/Shanghai"
BEFORE_DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(3)]
AFTER_DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(3)]

AGE_BANDS = ["18-24", "25-44", "45-64", "65+"]
REGIONS = ["north", "south"]
SEXES = ["F", "M"]
AGE_SHARES = {"18-24": 0.22, "25-44": 0.31, "45-64": 0.30, "65+": 0.17}
REGION_SHARES = {"north": 0.55, "south": 0.45}
SEX_SHARES = {"F": 0.51, "M": 0.49}
REFERENCE_TOTAL = 10_000
AFTER_TOTAL = 10_100  # a corrected after-period target, used only for the own-population variant

# Age-specific minutes of the morning programme, per survey day. Identical in
# both periods: any apparent change is sample composition, not behaviour.
MORNING_MINUTES = {"18-24": 60, "25-44": 30, "45-64": 15, "65+": 5}
EVENING_MINUTES_BEFORE = {"25-44": 20}
EVENING_MINUTES_AFTER = {"25-44": 40}  # same people, more minutes after the revamp
OLDIES_MINUTES = {"65+": 30}
NEW_SHOW_MINUTES = {"18-24": 20}

BEFORE_PROGRAMS = [
    ("morning_show", "Morning Show", "RADIO-1", "07:00", "08:00"),
    ("evening_show", "Evening Show", "RADIO-2", "20:00", "21:00"),
    ("oldies_hour", "Oldies Hour", "RADIO-4", "06:00", "07:00"),
]
AFTER_PROGRAMS = [
    ("morning_show_v2", "Morning Show (revamped)", "RADIO-1", "07:00", "08:00"),
    ("evening_show_v2", "Evening Show (revamped)", "RADIO-2", "20:00", "21:00"),
    ("new_show", "New Midday Show", "RADIO-3", "12:00", "13:00"),
]

BEFORE_PER_CELL = 10
# After-period diarists per cell. The 18-24 cells are refreshed heavily, which
# makes the achieved after sample young; the other cells keep their 10 diarists.
AFTER_PER_CELL = {"18-24": 30, "25-44": 10, "45-64": 10, "65+": 8}
# Persons 1..N of a before cell are the ones who stay in the after frame. One
# in five 65+ diarists leaves the frame with no replacement, so the paired design
# has a real non-coverable part and the after 65+ cell is genuinely smaller.
STAYERS_PER_CELL = {"18-24": 10, "25-44": 10, "45-64": 10, "65+": 8}
HOUSEHOLD_SIZE = 2


def _household_id(prefix: str, age: str, region: str, sex: str, k: int) -> str:
    return f"H-{prefix}-{age}-{region}-{sex}-{(k - 1) // HOUSEHOLD_SIZE + 1}"


def _cell_persons(age: str, region: str, sex: str, count: int, prefix: str) -> list[dict]:
    return [
        {
            "person_id": f"{prefix}-{age}-{region}-{sex}-{k}",
            "household_id": _household_id(prefix, age, region, sex, k),
            "stratum": "urban" if region == "north" else "rural",
            "age_band": age,
            "region": region,
            "sex": sex,
            "k": k,
        }
        for k in range(1, count + 1)
    ]


def before_persons() -> list[dict]:
    out = []
    for age in AGE_BANDS:
        for region in REGIONS:
            for sex in SEXES:
                out.extend(_cell_persons(age, region, sex, BEFORE_PER_CELL, "B"))
    return out


def after_persons() -> list[dict]:
    """Same IDs for stayers (paired), new IDs for refreshers and replacements."""
    out = []
    for age in AGE_BANDS:
        for region in REGIONS:
            for sex in SEXES:
                stayers = STAYERS_PER_CELL[age]
                total = AFTER_PER_CELL[age]
                out.extend(_cell_persons(age, region, sex, stayers, "B"))
                if total > stayers:
                    out.extend(_cell_persons(age, region, sex, total, "A")[stayers:])
    return out


def _period(day: date, hh: int, mm: int) -> str:
    return f"{day.isoformat()}T{hh:02d}:{mm:02d}:00+08:00"


def _day_window(day: date) -> dict[str, str]:
    return {"start": _period(day, 0, 0), "end": _period(day + timedelta(days=1), 0, 0)}


def _entries_for(person: dict, day: date, period: str) -> list[dict]:
    entries: list[dict] = []
    age = person["age_band"]
    pid = person["person_id"]
    tag = "B" if period == "before" else "A"

    morning = MORNING_MINUTES[age]
    if morning:
        entries.append(
            {
                "start": _period(day, 7, 0),
                "end": _period(day, 7 + morning // 60, morning % 60),
                "station": "RADIO-1",
                "source_serial": f"{pid}-{tag}-{day.isoformat()}-M",
            }
        )
    table = EVENING_MINUTES_BEFORE if period == "before" else EVENING_MINUTES_AFTER
    if age in table and table[age]:
        entries.append(
            {
                "start": _period(day, 20, 0),
                "end": _period(day, 20, table[age]),
                "station": "RADIO-2",
                "source_serial": f"{pid}-{tag}-{day.isoformat()}-E",
            }
        )
    if period == "before" and age in OLDIES_MINUTES:
        entries.append(
            {
                "start": _period(day, 6, 0),
                "end": _period(day, 6, OLDIES_MINUTES[age]),
                "station": "RADIO-4",
                "source_serial": f"{pid}-{tag}-{day.isoformat()}-O",
            }
        )
    if period == "after" and age in NEW_SHOW_MINUTES:
        entries.append(
            {
                "start": _period(day, 12, 0),
                "end": _period(day, 12, NEW_SHOW_MINUTES[age]),
                "station": "RADIO-3",
                "source_serial": f"{pid}-{tag}-{day.isoformat()}-N",
            }
        )
    return entries


def _submission(person: dict, days: list[date], period: str, revision: int) -> dict:
    return {
        "person_id": person["person_id"],
        "household_id": person["household_id"],
        "revision": revision,
        "submitted_at": f"{days[-1].isoformat()}T22:00:00+08:00",
        "coverage": [_day_window(d) for d in days],
        "entries": [e for d in days for e in _entries_for(person, d, period)],
    }


def recruitment_batches() -> dict[str, dict]:
    before = before_persons()
    after = after_persons()
    p_before = len(before) / REFERENCE_TOTAL
    p_after = len(after) / REFERENCE_TOTAL
    return {
        "before": {
            "batch_ref": "revamp-frame-before",
            "survey_period": {"start": BEFORE_DAYS[0].isoformat(), "end": BEFORE_DAYS[-1].isoformat()},
            "rows": [
                {
                    "household_id": p["household_id"],
                    "person_id": p["person_id"],
                    "stratum": p["stratum"],
                    "age_band": p["age_band"],
                    "region": p["region"],
                    "sex": p["sex"],
                    "inclusion_probability": round(p_before, 10),
                }
                for p in before
            ],
        },
        "after": {
            "batch_ref": "revamp-frame-after",
            "survey_period": {"start": AFTER_DAYS[0].isoformat(), "end": AFTER_DAYS[-1].isoformat()},
            "rows": [
                {
                    "household_id": p["household_id"],
                    "person_id": p["person_id"],
                    "stratum": p["stratum"],
                    "age_band": p["age_band"],
                    "region": p["region"],
                    "sex": p["sex"],
                    "inclusion_probability": round(p_after, 10),
                }
                for p in after
            ],
        },
    }


def _targets(total: int, ref: str) -> dict:
    margins = {
        "age_band": {age: round(total * share) for age, share in AGE_SHARES.items()},
        "region": {r: round(total * share) for r, share in REGION_SHARES.items()},
        "sex": {s: round(total * share) for s, share in SEX_SHARES.items()},
    }
    for cats in margins.values():
        last = list(cats)[-1]
        cats[last] += total - sum(cats.values())
    return {"batch_ref": ref, "population_total": total, "margins": margins}


def target_batches() -> dict[str, dict]:
    return {
        "reference": _targets(REFERENCE_TOTAL, "revamp-targets-reference"),
        "before": _targets(REFERENCE_TOTAL, "revamp-targets-before"),
        "after": _targets(AFTER_TOTAL, "revamp-targets-after"),
    }


def _program_batch(programs, days, ref: str) -> dict:
    airings = []
    for day in days:
        for key, title, station, start, end in programs:
            airings.append(
                {
                    "program_key": key,
                    "title": title,
                    "station": station,
                    "air_date": day.isoformat(),
                    "start_local": start,
                    "end_local": end,
                }
            )
    return {"batch_ref": ref, "timezone": TZ, "airings": airings}


def program_batches() -> dict[str, dict]:
    return {
        "before": _program_batch(BEFORE_PROGRAMS, BEFORE_DAYS, "revamp-schedule-before"),
        "after": _program_batch(AFTER_PROGRAMS, AFTER_DAYS, "revamp-schedule-after"),
    }


def mapping_payload(before_batch_seq: int, after_batch_seq: int) -> dict:
    return {
        "mapping_ref": "revamp-mapping-v1",
        "before_program_batch_seq": before_batch_seq,
        "after_program_batch_seq": after_batch_seq,
        "confirmed_by": "duty-editor",
        "confirmed_role": "editor",
        "links": [
            {
                "relation": "revamp",
                "before_program_key": "morning_show",
                "after_program_key": "morning_show_v2",
                "comparison_key": "morning_show",
                "before_slots": [{"station": "RADIO-1", "start_local": "07:00", "end_local": "08:00"}],
                "after_slots": [{"station": "RADIO-1", "start_local": "07:00", "end_local": "08:00"}],
                "note": "Same slot and same age-specific behaviour; the after sample is younger.",
            },
            {
                "relation": "revamp",
                "before_program_key": "evening_show",
                "after_program_key": "evening_show_v2",
                "comparison_key": "evening_show",
                "before_slots": [{"station": "RADIO-2", "start_local": "20:00", "end_local": "21:00"}],
                "after_slots": [{"station": "RADIO-2", "start_local": "20:00", "end_local": "21:00"}],
                "note": "Same slot; the same 25-44 diarists listen longer after the revamp.",
            },
            {
                "relation": "discontinued",
                "before_program_key": "oldies_hour",
                "comparison_key": "oldies_hour",
                "note": "Discontinued; part of its older audience is absent from the after frame.",
            },
            {
                "relation": "new",
                "after_program_key": "new_show",
                "comparison_key": "new_show",
                "note": "New midday programme; reported alone, never folded into comparable change.",
            },
        ],
    }


def diary_submissions() -> list[dict]:
    out = []
    for person in before_persons():
        out.append(_submission(person, BEFORE_DAYS, "before", 1))
    for person in after_persons():
        paired = person["person_id"].startswith("B-")
        out.append(_submission(person, AFTER_DAYS, "after", 2 if paired else 1))
    return out


SPARSE_ONLY_PERSONS = ["A-sparse-1", "A-sparse-2"]


def sparse_after_frame() -> dict:
    """After-period frame with one effective stratum that returns no diary.

    Two extra frame persons are placed alone in ``rural-remote`` and never
    submit a diary, so the stratum has frame rows but zero eligible sample. A
    definitive judgement pauses instead of the stratum being smoothed over.
    """
    batch = recruitment_batches()["after"]
    p = batch["rows"][0]["inclusion_probability"]
    for idx, pid in enumerate(SPARSE_ONLY_PERSONS, start=1):
        batch["rows"].append(
            {
                "household_id": f"H-sparse-{idx}",
                "person_id": pid,
                "stratum": "rural-remote",
                "age_band": "25-44",
                "region": "south",
                "sex": "F" if idx == 1 else "M",
                "inclusion_probability": p,
            }
        )
    batch["batch_ref"] = "revamp-frame-after-sparse"
    return batch


SPARSE_MISSING_PERSONS = set(SPARSE_ONLY_PERSONS)
