"""Editor-confirmed before/after program mapping.

The mapping is the only place where "the old show" and "the new show" are tied
together. The service never guesses it from titles or slots: a human with the
``editor`` role must confirm every link, and the confirmed list (including its
slot-level detail) is stored verbatim and hashed.

Rules enforced here:

* Comparable links (``unchanged`` / ``renamed`` / ``revamp``) name a program
  key in both periods. Without one the only allowed relations are
  ``discontinued`` (before only) and ``new`` (after only).
* A program key may appear at most once as a before key and at most once as an
  after key across all comparable links. The same before show cannot be
  "mapped" to two after shows.
* Every key referenced must exist in the pinned program batch.
* ``new`` and ``discontinued`` items never carry a comparable change; they are
  reported on their own.
"""
from __future__ import annotations

from typing import Any

from .db import Database
from .errors import invalid, not_found
from .jsonutil import canonical_hash, jb, loads

COMPARABLE = ("unchanged", "renamed", "revamp")
RELATIONS = COMPARABLE + ("discontinued", "new")

SLOT_DISCLAIMER = (
    "Slot-level before/after detail is what the editor confirmed. The service does not "
    "assume the new slot is comparable to the old one and reports both slots verbatim; all "
    "estimates are computed per period over each period's own schedule."
)


def _program_keys(cur, batch_seq: int) -> set[str]:
    cur.execute("SELECT DISTINCT program_key FROM program_airings WHERE batch_seq=%s", (batch_seq,))
    return {r["program_key"] for r in cur.fetchall()}


def _program_slots(cur, batch_seq: int) -> dict[str, list[dict[str, str]]]:
    cur.execute(
        """
        SELECT program_key, station, start_local, end_local
          FROM program_airings WHERE batch_seq=%s
         GROUP BY program_key, station, start_local, end_local
         ORDER BY program_key, station, start_local
        """,
        (batch_seq,),
    )
    out: dict[str, list[dict[str, str]]] = {}
    for r in cur.fetchall():
        out.setdefault(r["program_key"], []).append(
            {"station": r["station"], "start_local": r["start_local"], "end_local": r["end_local"]}
        )
    return out


def _normalise_links(
    cur,
    links: list[dict[str, Any]],
    before_seq: int,
    after_seq: int,
) -> list[dict[str, Any]]:
    before_keys = _program_keys(cur, before_seq)
    after_keys = _program_keys(cur, after_seq)
    if not before_keys:
        raise invalid("program_batch_empty", "before program batch has no airings", {"batch_seq": before_seq})
    if not after_keys:
        raise invalid("program_batch_empty", "after program batch has no airings", {"batch_seq": after_seq})

    seen_before: dict[str, str] = {}
    seen_after: dict[str, str] = {}
    seen_comparison: set[str] = set()
    out: list[dict[str, Any]] = []
    for idx, link in enumerate(links):
        relation = link.get("relation")
        if relation not in RELATIONS:
            raise invalid("mapping_relation_invalid", f"unknown relation {relation!r}", {"index": idx})
        before_key = link.get("before_program_key")
        after_key = link.get("after_program_key")
        if relation in COMPARABLE:
            if not before_key or not after_key:
                raise invalid("mapping_link_incomplete", "comparable link needs both keys", {"index": idx})
        if relation == "discontinued" and after_key:
            raise invalid("mapping_link_shape", "discontinued link must not name an after key", {"index": idx})
        if relation == "new" and before_key:
            raise invalid("mapping_link_shape", "new link must not name a before key", {"index": idx})

        for key, keyset, label, seen in (
            (before_key, before_keys, "before", seen_before),
            (after_key, after_keys, "after", seen_after),
        ):
            if key is None:
                continue
            if key not in keyset:
                raise invalid(
                    "mapping_program_unknown",
                    f"{label} program key {key!r} is not in the pinned program batch",
                    {"index": idx, "period": label, "program_key": key},
                )
            if key in seen:
                raise invalid(
                    "mapping_program_reused",
                    f"{label} program {key!r} is already mapped by another link",
                    {"index": idx, "period": label, "program_key": key, "first_seen_index": seen[key]},
                )
            seen[key] = str(idx)

        if relation in COMPARABLE:
            comparison_key = link.get("comparison_key") or after_key
        elif relation == "discontinued":
            comparison_key = link.get("comparison_key") or f"discontinued:{before_key}"
        else:
            comparison_key = link.get("comparison_key") or f"new:{after_key}"
        if comparison_key in seen_comparison:
            raise invalid(
                "mapping_comparison_key_reused",
                f"comparison_key {comparison_key!r} appears twice",
                {"index": idx, "comparison_key": comparison_key},
            )
        seen_comparison.add(comparison_key)

        out.append(
            {
                "comparison_key": comparison_key,
                "relation": relation,
                "comparable": relation in COMPARABLE,
                "before_program_key": before_key,
                "after_program_key": after_key,
                "before_slots": link.get("before_slots") or [],
                "after_slots": link.get("after_slots") or [],
                "note": link.get("note"),
            }
        )

    # Every program key present in either batch should be accounted for exactly
    # once. An omitted key is not silently dropped: the mapping would otherwise
    # hide a program from the report by never mentioning it.
    unmapped_before = sorted(before_keys - set(seen_before))
    unmapped_after = sorted(after_keys - set(seen_after))
    if unmapped_before or unmapped_after:
        raise invalid(
            "mapping_incomplete",
            "every program in both batches must be explicitly mapped, as comparable or as new/discontinued",
            {"unmapped_before": unmapped_before, "unmapped_after": unmapped_after},
        )
    return out


def create_mapping(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    before_seq = int(payload["before_program_batch_seq"])
    after_seq = int(payload["after_program_batch_seq"])
    if before_seq == after_seq:
        raise invalid(
            "mapping_same_batch",
            "a revamp comparison needs two distinct program batches",
            {"batch_seq": before_seq},
        )
    with db.scoped() as conn:
        cur = conn.cursor()
        if payload.get("mapping_ref"):
            cur.execute(
                "SELECT mapping_seq FROM revamp_program_mappings WHERE mapping_ref=%s",
                (payload["mapping_ref"],),
            )
            existing = cur.fetchone()
            if existing:
                return {"mapping_seq": existing["mapping_seq"], "idempotent": True}
        for seq in (before_seq, after_seq):
            cur.execute("SELECT 1 FROM program_batches WHERE batch_seq=%s", (seq,))
            if cur.fetchone() is None:
                raise not_found("program_batch_not_found", f"program batch {seq} not found")

        links = _normalise_links(cur, payload["links"], before_seq, after_seq)
        before_slots = _program_slots(cur, before_seq)
        after_slots = _program_slots(cur, after_seq)
        for link in links:
            # Slot detail is stored both as confirmed and as actually scheduled,
            # so a confirmation that disagrees with the batch is visible.
            if link["comparison_key"] and link["before_program_key"]:
                link["before_scheduled_slots"] = before_slots.get(link["before_program_key"], [])
            if link["after_program_key"]:
                link["after_scheduled_slots"] = after_slots.get(link["after_program_key"], [])
            for side, scheduled in (
                ("before", link.get("before_scheduled_slots")),
                ("after", link.get("after_scheduled_slots")),
            ):
                declared = link[f"{side}_slots"]
                if declared and scheduled and not any(
                    d["station"] == s["station"]
                    and d["start_local"] == s["start_local"]
                    and d["end_local"] == s["end_local"]
                    for d in declared
                    for s in scheduled
                ):
                    raise invalid(
                        "mapping_slot_not_scheduled",
                        f"declared {side} slot is not in the pinned program batch",
                        {
                            "comparison_key": link["comparison_key"],
                            "declared": declared,
                            "scheduled": scheduled,
                        },
                    )

        body = {
            "before_program_batch_seq": before_seq,
            "after_program_batch_seq": after_seq,
            "links": links,
        }
        payload_hash = canonical_hash(body)
        cur.execute(
            """
            INSERT INTO revamp_program_mappings
                (mapping_ref, before_program_batch_seq, after_program_batch_seq,
                 confirmed_by, confirmed_role, slot_disclaimer, payload, payload_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING mapping_seq, confirmed_at
            """,
            (
                payload.get("mapping_ref"),
                before_seq,
                after_seq,
                payload["confirmed_by"],
                "editor",
                SLOT_DISCLAIMER,
                jb(body),
                payload_hash,
            ),
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,'revamp_mapping',%s,%s)",
            (
                actor,
                "revamp_mapping.create",
                str(row["mapping_seq"]),
                jb(
                    {
                        "confirmed_by": payload["confirmed_by"],
                        "payload_hash": payload_hash,
                        "comparable": [l["comparison_key"] for l in links if l["comparable"]],
                        "new": [l["comparison_key"] for l in links if l["relation"] == "new"],
                        "discontinued": [
                            l["comparison_key"] for l in links if l["relation"] == "discontinued"
                        ],
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "mapping_seq": row["mapping_seq"],
        "confirmed_at": row["confirmed_at"].isoformat(),
        "confirmed_by": payload["confirmed_by"],
        "payload_hash": payload_hash,
        "slot_disclaimer": SLOT_DISCLAIMER,
        "links": links,
        "idempotent": False,
    }


def load_mapping(cur, mapping_seq: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM revamp_program_mappings WHERE mapping_seq=%s", (mapping_seq,))
    row = cur.fetchone()
    if row is None:
        raise not_found("revamp_mapping_not_found", f"mapping {mapping_seq} not found")
    body = loads(row["payload"]) or {}
    return {
        "mapping_seq": row["mapping_seq"],
        "mapping_ref": row["mapping_ref"],
        "before_program_batch_seq": row["before_program_batch_seq"],
        "after_program_batch_seq": row["after_program_batch_seq"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_role": row["confirmed_role"],
        "confirmed_at": row["confirmed_at"].isoformat(),
        "payload_hash": row["payload_hash"],
        "slot_disclaimer": row["slot_disclaimer"],
        **body,
    }


def list_mappings(db: Database) -> list[dict[str, Any]]:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT mapping_seq, mapping_ref, before_program_batch_seq, after_program_batch_seq, "
            "confirmed_by, payload_hash, confirmed_at FROM revamp_program_mappings ORDER BY mapping_seq"
        )
        return [{**r, "confirmed_at": r["confirmed_at"].isoformat()} for r in cur.fetchall()]
