# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tier-3 provenance-query store (provenance plan, Task 3.1 — D5/D6 pins).

Pins the replica-coherent store contract the Wave-3 tasks build against:

- round-trip put/get keyed by ``(session_id, query_ref)``; unknown/expired/
  invalid ⇒ ``None`` (the route's structured-404 signal — never fabricated);
- D6 bounds enforced AT WRITE: ≤32 entries/turn; ≤16 KB query text — an
  over-cap entry is stored HONESTLY (``executed_query: ""`` + explicit
  note), never truncated mid-SQL silently;
- entries normalize to the D5 redemption body shape (sidecar spellings
  ``query_text``/``kg_artifact_ids`` accepted);
- the DynamoDB item carries a TTL attribute (JobStore write-through);
- fail-soft: in-memory fallback (the offline unit-lane path) and backend
  errors never raise;
- logging NEVER carries query text — lengths + sha256[:12] only.

Fully offline: the JobStore backend is either absent (in-memory fallback)
or a fake in-memory Table double; no AWS, no network.
"""
from __future__ import annotations

import logging

import pytest

import agents.provenance_store as provenance_store
from agents.job_store import JobStore
from agents.provenance import mint_query_ref
from agents.provenance_store import (
    MAX_ENTRIES_PER_TURN,
    MAX_QUERY_TEXT_BYTES,
    OVER_BOUND_NOTE,
    get_query,
    put_turn_queries,
)


class _FakeTable:
    """Minimal shared DynamoDB Table double (same as test_job_store.py)."""

    def __init__(self, store: dict):
        self._store = store

    def put_item(self, Item):  # noqa: N803 (boto3 kwarg)
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


class _BoomTable:
    """Backend that always errors — exercises the fail-soft path."""

    def put_item(self, Item):  # noqa: N803
        raise RuntimeError("ddb down")

    def get_item(self, Key):  # noqa: N803
        raise RuntimeError("ddb down")

    def delete_item(self, Key):  # noqa: N803
        raise RuntimeError("ddb down")


_SID = "sess-abc.123"


def _ref(tool_id: str = "toolu_001") -> str:
    return mint_query_ref(_SID, tool_id)


def _entry(**overrides) -> dict:
    entry = {
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "query_text": "SELECT part_no FROM orders WHERE qty > 5",
        "kg_artifact_ids": ["subj-0a1b", "vm-learned-0c2d"],
        "captured_at": "2026-08-03T19:00:00Z",
    }
    entry.update(overrides)
    return entry


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    """Fresh in-memory JobStore per test (the offline fail-soft branch —
    exactly what runs in the unit lane). Tests that need a backend swap in
    their own JobStore built on a fake table."""
    monkeypatch.setattr(provenance_store, "_job_store", JobStore(provenance_store._KIND, table=None))


def _use_table(monkeypatch, table) -> None:
    # Mirrors _jobs(): the DELIBERATE per-kind TTL rides every backend write.
    monkeypatch.setattr(
        provenance_store, "_job_store",
        JobStore(provenance_store._KIND, table=table,
                 ttl_seconds=provenance_store.PROVENANCE_QUERY_TTL_SECONDS))


# --------------------------------------------------------------------------- #
# Round-trip + honest miss
# --------------------------------------------------------------------------- #

def test_round_trip_put_get_returns_d5_shape():
    ref = _ref()
    assert put_turn_queries(_SID, {ref: _entry()}) == 1
    got = get_query(_SID, ref)
    assert got == {
        "query_ref": ref,
        "session_id": _SID,
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "executed_query": "SELECT part_no FROM orders WHERE qty > 5",
        "kg_artifacts": ["subj-0a1b", "vm-learned-0c2d"],
        "captured_at": "2026-08-03T19:00:00Z",
    }


def test_executed_query_spelling_accepted_verbatim():
    ref = _ref()
    entry = _entry(executed_query="SELECT 1", query_text=None)
    put_turn_queries(_SID, {ref: entry})
    assert get_query(_SID, ref)["executed_query"] == "SELECT 1"


def test_unknown_query_ref_is_none():
    assert get_query(_SID, _ref("never-stored")) is None


def test_session_isolation():
    """Same query_ref under a different session must miss — the key is the
    (session_id, query_ref) PAIR."""
    ref = _ref()
    put_turn_queries(_SID, {ref: _entry()})
    assert get_query("other-session", ref) is None


def test_invalid_ids_miss_without_raising():
    assert get_query(_SID, "q-NOTHEX000000") is None
    assert get_query(_SID, "q-" + "a" * 11) is None
    assert get_query(_SID, "'; DROP TABLE--") is None
    assert get_query("bad session id!", _ref()) is None
    assert get_query(None, _ref()) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Bounds at write (D6)
# --------------------------------------------------------------------------- #

def test_over_cap_query_text_stored_honestly():
    """>16 KB query text ⇒ executed_query '' + explicit note — never a
    silent mid-SQL truncation."""
    ref = _ref()
    big = "SELECT 987654321 -- " + "x" * MAX_QUERY_TEXT_BYTES
    put_turn_queries(_SID, {ref: _entry(query_text=big)})
    got = get_query(_SID, ref)
    assert got["executed_query"] == ""
    assert got["note"] == OVER_BOUND_NOTE
    assert big[:64] not in str(got)


def test_query_text_at_exact_bound_kept_whole():
    ref = _ref()
    text = "S" * MAX_QUERY_TEXT_BYTES
    put_turn_queries(_SID, {ref: _entry(query_text=text)})
    got = get_query(_SID, ref)
    assert got["executed_query"] == text
    assert "note" not in got


def test_entries_capped_at_32_per_turn():
    entries = {_ref(f"toolu_{i:03d}"): _entry() for i in range(MAX_ENTRIES_PER_TURN + 8)}
    assert put_turn_queries(_SID, entries) == MAX_ENTRIES_PER_TURN


def test_malformed_keys_and_entries_dropped():
    good = _ref()
    entries = {
        "not-a-ref": _entry(),          # wrong handle shape
        "q-XYZXYZXYZXYZ": _entry(),     # non-hex
        good: _entry(),
        _ref("toolu_bad"): "not-a-dict",
    }
    assert put_turn_queries(_SID, entries) == 1
    assert get_query(_SID, good) is not None
    assert put_turn_queries(_SID, "not-a-dict") == 0  # type: ignore[arg-type]
    assert put_turn_queries("bad session!", {good: _entry()}) == 0


def test_metadata_fields_bounded_and_coerced():
    ref = _ref()
    entry = _entry(
        system_id="s" * 500,
        tool_name=12345,
        kg_artifact_ids=["a" * 500] * 64 + [None, 7],
        captured_at=None,
    )
    put_turn_queries(_SID, {ref: entry})
    got = get_query(_SID, ref)
    assert len(got["system_id"]) == 100
    assert got["tool_name"] == ""  # non-str ⇒ honest empty, not str()-coerced
    assert len(got["kg_artifacts"]) == 32
    assert all(len(a) == 200 for a in got["kg_artifacts"])
    assert got["captured_at"] == ""


# --------------------------------------------------------------------------- #
# Backend behaviour (TTL, cross-replica, fail-soft)
# --------------------------------------------------------------------------- #

def test_backend_item_carries_ttl_attribute(monkeypatch):
    shared: dict = {}
    _use_table(monkeypatch, _FakeTable(shared))
    put_turn_queries(_SID, {_ref(): _entry()})
    assert len(shared) == 1
    item = next(iter(shared.values()))
    assert isinstance(item.get("ttl"), int) and item["ttl"] > 0


def test_ttl_magnitude_is_the_deliberate_24_hours(monkeypatch):
    """MED pin: this kind's TTL is a DELIBERATE 24 h (transcripts get
    reopened across a workday; entries are ≤16 KB, ≤32/turn) — NOT the 1 h
    async-job default JobStore ships for map/provision jobs."""
    import time as _time
    assert provenance_store.PROVENANCE_QUERY_TTL_SECONDS == 24 * 3600
    # The module singleton is constructed WITH it (the wiring, not just the
    # constant): reset and rebuild through _jobs().
    monkeypatch.setattr(provenance_store, "_job_store", None)
    assert provenance_store._jobs()._ttl_seconds == 24 * 3600
    # …and a backend write stamps ttl ≈ now + 24 h.
    shared: dict = {}
    _use_table(monkeypatch, _FakeTable(shared))
    before = int(_time.time())
    put_turn_queries(_SID, {_ref(): _entry()})
    item = next(iter(shared.values()))
    assert before + 24 * 3600 <= item["ttl"] <= int(_time.time()) + 24 * 3600


def test_job_store_default_ttl_unchanged_for_other_kinds():
    """The ttl_seconds kwarg is ADDITIVE: a plain JobStore (map/provision
    jobs) keeps the short 1 h horizon byte-identically."""
    shared: dict = {}
    store = JobStore("map", table=_FakeTable(shared))
    store["job-1"] = {"status": "running"}
    import time as _time
    item = next(iter(shared.values()))
    assert item["ttl"] <= int(_time.time()) + 3600
    assert item["ttl"] > int(_time.time()) + 3600 - 60


def test_entry_written_on_one_replica_readable_on_another(monkeypatch):
    shared: dict = {}
    ref = _ref()
    _use_table(monkeypatch, _FakeTable(shared))
    put_turn_queries(_SID, {ref: _entry()})
    # "Different replica": a fresh store over the same durable table.
    _use_table(monkeypatch, _FakeTable(shared))
    got = get_query(_SID, ref)
    assert got is not None and got["query_ref"] == ref


def test_backend_failure_is_fail_soft(monkeypatch):
    _use_table(monkeypatch, _BoomTable())
    ref = _ref()
    assert put_turn_queries(_SID, {ref: _entry()}) == 1  # never raises
    assert get_query(_SID, ref)["executed_query"].startswith("SELECT")


# --------------------------------------------------------------------------- #
# Logging posture (claim-check: no query text, masked or not, in logs)
# --------------------------------------------------------------------------- #

def test_logs_never_carry_query_text_digits(caplog):
    """A put (incl. an over-cap entry) must log lengths + sha256[:12] only —
    no digit or fragment from the query text appears in any record."""
    marker_digits = "8675309424242"
    small = f"SELECT * FROM orders WHERE id = {marker_digits}"  # nosec B608 - test fixture SQL
    big = small + " -- " + "p" * MAX_QUERY_TEXT_BYTES
    with caplog.at_level(logging.DEBUG):
        put_turn_queries(_SID, {_ref("a"): _entry(query_text=small),
                                _ref("b"): _entry(query_text=big)})
        get_query(_SID, _ref("a"))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert marker_digits not in logged
    assert "SELECT" not in logged
    assert "orders" not in logged


# --------------------------------------------------------------------------- #
# Wave-5 additive F2 fields (entity/row_count/statement_count/origin)
# --------------------------------------------------------------------------- #

def _f2_entry(**overrides) -> dict:
    entry = _entry(
        executed_query="SELECT part_no FROM orders",
        entity={"table": "orders", "columns": ["part_no", "qty"]},
        row_count=7,
        statement_count=2,
        origin="system-agent",
    )
    entry.pop("query_text", None)
    entry.update(overrides)
    return entry


def test_f2_entry_stores_all_additive_fields_bounded():
    ref = _ref("toolu_f2")
    assert put_turn_queries(_SID, {ref: _f2_entry()}) == 1
    got = get_query(_SID, ref)
    assert got["entity"] == {"table": "orders", "columns": ["part_no", "qty"]}
    assert got["row_count"] == 7
    assert got["statement_count"] == 2
    assert got["origin"] == "system-agent"
    # Existing D5 keys unchanged alongside the additive ones.
    assert got["executed_query"] == "SELECT part_no FROM orders"
    assert got["system_id"] == "erp"


def test_legacy_entry_without_f2_fields_stores_byte_identically():
    """The frozen pre-Wave-5 fixture: an entry WITHOUT additive fields must
    store the EXACT seven-key D5 dict — no additive key may appear."""
    ref = _ref("toolu_legacy")
    assert put_turn_queries(_SID, {ref: _entry()}) == 1
    got = get_query(_SID, ref)
    assert got == {
        "query_ref": ref,
        "session_id": _SID,
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "executed_query": "SELECT part_no FROM orders WHERE qty > 5",
        "kg_artifacts": ["subj-0a1b", "vm-learned-0c2d"],
        "captured_at": "2026-08-03T19:00:00Z",
    }
    for key in ("entity", "row_count", "statement_count", "origin"):
        assert key not in got


def test_f2_bounds_enforced_at_write():
    ref = _ref("toolu_bounds")
    entry = _f2_entry(
        entity={"table": "t" * 500, "columns": [f"c{i}" * 100 for i in range(40)]},
        statement_count=999,
        origin="x" * 200,
    )
    put_turn_queries(_SID, {ref: entry})
    got = get_query(_SID, ref)
    assert len(got["entity"]["table"]) == provenance_store._MAX_TOOL_NAME_LEN
    assert len(got["entity"]["columns"]) == provenance_store._MAX_ENTITY_COLUMNS
    assert all(len(c) <= provenance_store._MAX_ENTITY_COLUMN_LEN
               for c in got["entity"]["columns"])
    assert got["statement_count"] == provenance_store._MAX_STATEMENT_COUNT
    assert len(got["origin"]) == provenance_store._MAX_ORIGIN_LEN


def test_f2_row_count_null_and_hostile_shapes():
    # Explicit null row_count is stored honestly (unknown ≠ absent).
    ref_null = _ref("toolu_null")
    put_turn_queries(_SID, {ref_null: _f2_entry(row_count=None)})
    got = get_query(_SID, ref_null)
    assert "row_count" in got and got["row_count"] is None
    # Hostile shapes (bool / negative / str / non-dict entity) drop out.
    ref_bad = _ref("toolu_bad")
    put_turn_queries(_SID, {ref_bad: _f2_entry(
        row_count=True, entity="DROP TABLE", statement_count="9", origin=7)})
    got = get_query(_SID, ref_bad)
    for key in ("row_count", "entity", "statement_count", "origin"):
        assert key not in got


def test_f2_over_cap_query_text_keeps_additive_fields_and_honest_note():
    """The existing ""+note path composes with the additive fields."""
    ref = _ref("toolu_overcap")
    big = "SELECT " + "x" * MAX_QUERY_TEXT_BYTES
    put_turn_queries(_SID, {ref: _f2_entry(executed_query=big)})
    got = get_query(_SID, ref)
    assert got["executed_query"] == ""
    assert got["note"] == OVER_BOUND_NOTE
    assert got["origin"] == "system-agent"
    assert got["statement_count"] == 2


def test_query_shape_fields_are_additive_and_validated_as_a_pair():
    ref = _ref("toolu_shape")
    fingerprint = "a" * 64
    assert put_turn_queries(_SID, {ref: _f2_entry(
        query_fingerprint=fingerprint, query_shape_version="v1")}) == 1
    got = get_query(_SID, ref)
    assert got["query_fingerprint"] == fingerprint
    assert got["query_shape_version"] == "v1"

    bad_ref = _ref("toolu_bad_shape")
    put_turn_queries(_SID, {bad_ref: _f2_entry(
        query_fingerprint="not-a-digest", query_shape_version="v1")})
    bad = get_query(_SID, bad_ref)
    assert "query_fingerprint" not in bad
    assert "query_shape_version" not in bad


def test_over_cap_query_has_honest_fingerprint_absence_even_if_supplied():
    ref = _ref("toolu_shape_overcap")
    big = "SELECT " + "x" * MAX_QUERY_TEXT_BYTES
    put_turn_queries(_SID, {ref: _f2_entry(
        executed_query=big, query_fingerprint="b" * 64,
        query_shape_version="v1")})
    got = get_query(_SID, ref)
    assert got["executed_query"] == ""
    assert got["note"] == OVER_BOUND_NOTE
    assert "query_fingerprint" not in got
    assert "query_shape_version" not in got


# --------------------------------------------------------------------------- #
# The API-read note: a source read through its API has no query text
# --------------------------------------------------------------------------- #
def _api_read_entry(**overrides) -> dict:
    """The system agent's read entry for a call that ran no SQL."""
    entry = {"system_id": "line-telemetry", "tool_name": "query_line_telemetry",
             "executed_query": "", "kg_artifacts": [],
             "captured_at": "2026-10-26T21:00:00Z",
             "entity": {"table": "", "columns": []}, "statement_count": 1,
             "origin": "system-agent",
             "note": getattr(provenance_store, "API_READ_NOTE", "")}
    entry.update(overrides)
    return entry


def test_the_api_read_note_is_stored_on_an_entry_with_no_query_text():
    """Without it the card's Tier 3 says "executed query not captured for
    this call", which reads like a capture failure for a source that has no
    query text to capture."""
    ref = _ref("toolu_api_read")
    put_turn_queries(_SID, {ref: _api_read_entry()})
    got = get_query(_SID, ref)
    assert got["note"] == provenance_store.API_READ_NOTE
    assert got["executed_query"] == ""
    assert got["statement_count"] == 1
    assert "row_count" not in got


@pytest.mark.parametrize("variant", ["free_text", "upper_case", "trailing_space",
                                     "over_bound_note", "not_a_string", "beside_query_text"])
def test_no_other_note_and_no_api_read_note_beside_query_text_is_stored(variant):
    """Closed vocabulary: only that exact note, and only on an entry with no
    query text. The over-bound note stays the store's own."""
    note = getattr(provenance_store, "API_READ_NOTE", "")
    overrides = {
        "free_text": {"note": "any text a writer chooses"},
        "upper_case": {"note": note.upper()},
        "trailing_space": {"note": note + " "},
        "over_bound_note": {"note": OVER_BOUND_NOTE},
        "not_a_string": {"note": 7},
        "beside_query_text": {"executed_query": "SELECT part_no FROM orders"},
    }[variant]
    ref = _ref("toolu_api_read_other")
    put_turn_queries(_SID, {ref: _api_read_entry(**overrides)})
    assert "note" not in get_query(_SID, ref)


def test_an_over_bound_query_keeps_the_stores_own_note():
    ref = _ref("toolu_api_read_overcap")
    big = "SELECT " + "x" * MAX_QUERY_TEXT_BYTES
    put_turn_queries(_SID, {ref: _api_read_entry(executed_query=big)})
    assert get_query(_SID, ref)["note"] == OVER_BOUND_NOTE


def test_the_chat_reads_the_same_api_read_note():
    """The evidence card words its read count by this note, so the chat's
    copy (``frontend/src/api/provenance.ts``) is the store's text exactly."""
    import json
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "api"
              / "provenance.ts").read_text(encoding="utf-8")
    assert (f"export const API_READ_NOTE = {json.dumps(provenance_store.API_READ_NOTE)};"
            in source)
