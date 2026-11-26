# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The schema-rewrite mark in the shared store.

A re-provision marks its system while it rewrites the schema subgraph, and a
rewrite that ends unsettled leaves a failed marker. Both were System-node
properties only, where a model's query (``UNWIND keys(s) AS k RETURN k,
s[k]``) can read them. They move into one record per system in the shared
store the provision leases use (``schema_rewrite_state`` over
``JobStore.swap_strict``), with the compare-and-set conditions the graph
writes have. This release is phase 1 of that move: writers also keep the
System-node properties and readers honour both
(``test_schema_rewrite_mixed_versions.py``). These tests cover the store's
half as phase 2 will run it alone: the field rows below carry no System-node
mark. A reader reads the record before and after its field read and reads
again when a rewrite began in between; a store that cannot be read or
written fails closed; a removal empties the record it finds.

Neutral names: systems ``hub-1``/``hub-2``, table ``t``, field ``k``.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import agents.onboard as onboard
from agents.job_store import JobStore, JobStoreUnavailableError
from tests.unit._shared_table import SharedTable

SID = "hub-1"


def _snapshot_rows(present: bool) -> list:
    """A field row as the graph answers it when the System node carries no
    mark property (what a writer that marks only the store leaves)."""
    return [{"fields": int(present), "started": None, "failed": None,
             "edges": [{"concept": "c1", "confidence": 0.9}] if present else []}]


def test_a_stale_mark_does_not_hide_a_rewrite_during_the_field_read(monkeypatch):
    """REPRODUCTION. The record holds a stale mark (a
    run that stopped long ago). A whole rewrite (mark, delete, settle) lands
    while the field read runs, so the read saw the field deleted and the
    record after it holds no mark. fa47db8e returned the stale mark from
    before the read, judged it stale and took the in-flight read ("not in the
    graph"). The start recorded during the read must win, so the reader reads
    the field again."""
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", lambda _s: None)
    onboard.schema_rewrite_state.begin(onboard._SCHEMA_REWRITE_STATES, SID, 1_000)
    reads: list = []

    def field_read(cypher, params=None):
        assert cypher == onboard._TARGET_FIELD_SNAPSHOT_CYPHER
        reads.append(params)
        if len(reads) == 1:
            token = onboard._mark_schema_rewrite(SID)
            onboard._finish_schema_rewrite(SID, token, settled=True)
            return _snapshot_rows(present=False)
        return _snapshot_rows(present=True)
    monkeypatch.setattr(onboard, "neptune_query_checked", field_read)
    concepts, why = onboard._settled_target_concepts(
        SimpleNamespace(schema_wait_budget_s=60.0), SID, "t", "k")
    assert why is None and concepts[0] == frozenset({"c1"})
    assert len(reads) == 2


def test_a_rewrite_that_begins_and_ends_during_the_field_read_is_read_again(monkeypatch):
    """REPRODUCTION. A whole rewrite of the target (mark, delete, settle)
    lands while the reader's field read runs, and that read saw the field
    deleted. The reader sees no mark before or after, but the rewrite's start
    shows in the record, so it reads again and gets the field as the rewrite
    left it, instead of "not in the graph"."""
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", lambda _s: None)
    reads: list = []

    def field_read(cypher, params=None):
        assert cypher == onboard._TARGET_FIELD_SNAPSHOT_CYPHER
        reads.append(params)
        if len(reads) == 1:
            token = onboard._mark_schema_rewrite(SID)
            onboard._finish_schema_rewrite(SID, token, settled=True)
            return _snapshot_rows(present=False)
        return _snapshot_rows(present=True)
    monkeypatch.setattr(onboard, "neptune_query_checked", field_read)
    ctx = SimpleNamespace(schema_wait_budget_s=60.0)
    concepts, why = onboard._settled_target_concepts(ctx, SID, "t", "k")
    assert why is None and concepts[0] == frozenset({"c1"})
    assert len(reads) == 2


def test_a_rewrite_that_ends_during_the_field_read_is_read_again(monkeypatch):
    """REPRODUCTION. The target is marked when the reader starts and its
    rewrite settles while the field read runs (no new start in between). The
    field read may hold the half-written schema, so the reader reads again."""
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", lambda _s: None)
    token = onboard._mark_schema_rewrite(SID)
    reads: list = []

    def field_read(cypher, params=None):
        reads.append(params)
        if len(reads) == 1:
            onboard._finish_schema_rewrite(SID, token, settled=True)
            return _snapshot_rows(present=False)
        return _snapshot_rows(present=True)
    monkeypatch.setattr(onboard, "neptune_query_checked", field_read)
    concepts, why = onboard._settled_target_concepts(
        SimpleNamespace(schema_wait_budget_s=60.0), SID, "t", "k")
    assert why is None and concepts[0] == frozenset({"c1"})
    assert len(reads) == 2


class _FirstReadFails:
    """A store whose first consistent read fails and whose later ones answer
    no record."""

    def __init__(self):
        self.reads = 0

    def get_consistent(self, _sid):
        self.reads += 1
        if self.reads == 1:
            raise JobStoreUnavailableError("read failed")
        return None


def test_a_stale_store_mark_is_cleared_by_its_value_and_the_field_read(monkeypatch):
    """A mark only the store holds (what a phase-2 writer that stopped
    leaves), older than the staleness bound: the reader removes exactly that
    value, does not wait, and reads the field as it stands."""
    sent = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: sent.append((c, p)) or True)
    monkeypatch.setattr(onboard, "neptune_query_checked", lambda *_a, **_k: _snapshot_rows(True))
    store = onboard._SCHEMA_REWRITE_STATES
    onboard.schema_rewrite_state.begin(store, SID, 1_000)
    concepts, why = onboard._settled_target_concepts(
        SimpleNamespace(schema_wait_budget_s=0.0), SID, "t", "k")
    assert why is None and concepts[0] == frozenset({"c1"})
    assert onboard.schema_rewrite_state.read(store, SID)["started"] is None
    assert sent == [(onboard._SCHEMA_REWRITE_CLEAR_STALE_CYPHER, {"sid": SID, "started": 1_000})]


def test_a_record_unreadable_before_the_field_read_is_could_not_run(monkeypatch):
    """The read before the field read fails, the one after answers: without
    the first there is no telling whether a rewrite was under way, so the
    re-measurement could not run."""
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", _FirstReadFails())
    monkeypatch.setattr(onboard, "neptune_query_checked", lambda *_a, **_k: _snapshot_rows(True))
    snapshot, why = onboard._target_field_snapshot(SID, "t", "k")
    assert snapshot is None and "could not be read" in why


@pytest.mark.parametrize("broken", ["reads", "puts"])
def test_a_store_that_cannot_be_used_fails_closed(monkeypatch, broken):
    """No mark that cannot be confirmed: b0 stops before its delete. No
    reading of an unreadable record as "no mark": the re-measurement could
    not run. The end of a rewrite that cannot be written is logged, not
    raised (the mark then goes stale and readers wait, then refuse)."""
    table = SharedTable()
    setattr(table, f"fail_{broken}", True)
    store = JobStore("schema-rewrite", table=table)
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", store)
    sent = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: sent.append((c, p)) or True)
    monkeypatch.setattr(onboard, "neptune_query_checked", lambda *_a, **_k: _snapshot_rows(True))
    with pytest.raises(onboard.SchemaRewriteMarkError, match="shared store"):
        onboard._mark_schema_rewrite(SID)
    (mark, mark_params), (unmark, unmark_params) = sent      # the System-node mark, taken back
    assert mark == onboard._SCHEMA_REWRITE_MARK_CYPHER
    assert unmark == onboard._SCHEMA_REWRITE_CLEAR_STALE_CYPHER
    assert unmark_params == {"sid": SID, "started": mark_params["started"]}
    onboard._finish_schema_rewrite(SID, 1, settled=True)
    snapshot, why = onboard._target_field_snapshot(SID, "t", "k")
    if broken == "reads":
        assert snapshot is None and "could not be read" in why
    assert not store._mem


def test_the_end_and_the_stale_clear_keep_the_compare_and_set_conditions():
    """The end replaces only the run's own mark (or none); a stale clear
    removes only the exact value its reader saw."""
    srs = onboard.schema_rewrite_state
    store = JobStore("schema-rewrite", table=SharedTable())
    srs.begin(store, SID, 100)
    assert srs.end(store, SID, 99, failed=5)["started"] == 100          # another run's mark
    assert srs.end(store, SID, 100, failed=5) == {"started": None, "failed": 5, "begun": 100}
    assert srs.clear(store, SID, srs.FAILED, 4) is False
    assert srs.clear(store, SID, srs.FAILED, 5) is True
    srs.begin(store, SID, 200)
    assert srs.end(store, SID, 200) == {"started": None, "failed": None, "begun": 200}


def test_swap_strict_is_a_compare_and_set_across_processes():
    """Two processes share the table. Another one writes the record between
    this one's read and its write: the write is refused on the revision,
    and the swap runs again on the record as the other process left it.
    Reads are consistent, and nothing is kept in process memory."""
    table = SharedTable()
    here, there = JobStore("schema-rewrite", table=table), JobStore("schema-rewrite", table=table)
    there.swap_strict("r", lambda _cur: {"n": 1, "by": ["there"]})
    calls = []

    def add_here(cur):
        calls.append(dict(cur))
        if len(calls) == 1:
            there.swap_strict("r", lambda c: {"n": c["n"] + 1, "by": c["by"] + ["there"]})
        return {"n": cur["n"] + 1, "by": cur["by"] + ["here"]}
    assert here.swap_strict("r", add_here) == {"n": 3, "by": ["there", "there", "here"]}
    assert len(calls) == 2
    assert here.get_consistent("r") == {"n": 3, "by": ["there", "there", "here"]}
    assert table.consistent_reads == table.reads
    assert not here._mem and not there._mem
    table.fail_reads = True
    with pytest.raises(JobStoreUnavailableError):
        here.get_consistent("r")


# ── A removal clears the system's record ───────────────────────────────────

def _record(store, sid=SID) -> dict:
    return onboard.schema_rewrite_state.read(store, sid)


@pytest.mark.parametrize("state", ["active", "failed", "settled", "absent"])
def test_a_removal_clears_the_record_it_finds(monkeypatch, state):
    """REPRODUCTION. Removal deleted the system's onboarding session but left
    its rewrite record (keyed apart, ``__job__:schema-rewrite:<id>``), so a
    removed or re-created system read as rewriting or unsettled. Now the
    removal's session step clears the record it finds: an active mark, a
    failed marker, a settled record and no record at all all end empty."""
    srs = onboard.schema_rewrite_state
    store = JobStore("schema-rewrite", table=SharedTable())
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", store)
    sessions = []
    monkeypatch.setattr("agents.onboard_agent._store", lambda: SimpleNamespace(
        delete_session=lambda sid: sessions.append(sid) or True))
    now = int(time.time() * 1000)
    if state != "absent":
        srs.begin(store, SID, now)
    if state in ("failed", "settled"):
        srs.end(store, SID, now, failed=now if state == "failed" else None)
    assert onboard._delete_onboarding_session(SID) is True
    assert _record(store) == {"started": None, "failed": None, "begun": None}
    assert sessions == [SID]


def test_reset_all_clears_every_systems_record(monkeypatch):
    srs = onboard.schema_rewrite_state
    store = JobStore("schema-rewrite", table=SharedTable())
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", store)
    monkeypatch.setattr("agents.onboard_agent._store", lambda: SimpleNamespace(
        delete_session=lambda _sid: True))
    srs.begin(store, "hub-1", 5)
    srs.begin(store, "hub-2", 6)
    srs.end(store, "hub-2", 6, failed=7)
    onboard._delete_onboarding_sessions({"hub-1", "hub-2"})
    assert _record(store, "hub-1")["started"] is None and _record(store, "hub-2")["failed"] is None


def test_a_record_a_newer_owner_writes_meanwhile_is_kept():
    """The clear removes only the record it read: a begin that lands between
    its read and its write (the revision moves) is kept."""
    srs = onboard.schema_rewrite_state
    table = SharedTable()
    store, other = JobStore("schema-rewrite", table=table), JobStore("schema-rewrite", table=table)
    srs.begin(store, SID, 5)
    real = store.swap_strict

    def racing(sid, fn, **kw):
        srs.begin(other, sid, 9)               # another process starts a rewrite
        return real(sid, fn, **kw)
    store.swap_strict = racing
    assert srs.forget(store, SID) is False
    assert _record(store)["started"] == 9


def test_a_store_that_cannot_be_cleared_does_not_stop_the_removal(monkeypatch, caplog):
    table = SharedTable()
    table.fail_reads = True
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", JobStore("schema-rewrite", table=table))
    monkeypatch.setattr("agents.onboard_agent._store", lambda: SimpleNamespace(
        delete_session=lambda _sid: True))
    assert onboard._delete_onboarding_session(SID) is True
    assert any("schema-rewrite record" in r.getMessage() for r in caplog.records)
