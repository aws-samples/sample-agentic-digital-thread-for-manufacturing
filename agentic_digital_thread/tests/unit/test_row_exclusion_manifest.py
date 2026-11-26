# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-system row-exclusion MANIFEST and the steward route's write order.

Which tables of a system carry a row exclusion is kept on the System node
(``row_exclusion_manifest``), so a system agent can tell "no table carries one"
from "unknown" in one read. The steward's set and clear keep manifest and fact
consistent in a crash-safe order: on set the table is listed (pending) before
its fact is written and settled after; on clear it is marked pending, its fact
cleared, and only then removed. A crash between any two writes leaves every
stored fact listed, never unlisted.

Fully offline (``_row_exclusion_graph.RxGraph``); synthetic names.
"""
from __future__ import annotations

import json

import pytest

from agents import row_exclusion as rx
from agents import row_exclusion_manifest as manifest
from tests.unit._row_exclusion_graph import RxGraph

_JOB = {"pipeline": "varchar", "run_ref": "varchar", "step_kind": "varchar", "qty": "integer"}
_LEDGER = {"batch_tag": "varchar", "amount": "double"}
_CONDITIONS = [{"column": "pipeline", "op": "not_empty"},
               {"column": "run_ref", "op": "not_empty"},
               {"column": "step_kind", "op": "not_in", "values": ["ASSEMBLE", "PACK"]}]


@pytest.fixture
def g():
    rx.reset()
    yield RxGraph({"sys-a": {"job_events": _JOB, "ledger_lines": _LEDGER}})
    rx.reset()


def _set(g, table="job_events", conditions=_CONDITIONS):
    return rx.set_row_exclusion("sys-a", table, conditions, label="shadow copies",
                                actor="dana", source="owner note", write=g.write, query=g.query)


def _clear(g, table="job_events"):
    return rx.clear_row_exclusion("sys-a", table, actor="dana", write=g.write, query=g.query)


# --------------------------------------------------------------------------- #
# The stored value.
# --------------------------------------------------------------------------- #
def test_no_property_on_an_existing_system_is_a_confirmed_empty_manifest():
    assert manifest.from_rows([{"row_exclusion_manifest": None}]) == (None, {})
    assert manifest.from_rows([{}]) == (None, {})


@pytest.mark.parametrize("rows", [
    [], None, [{"row_exclusion_manifest": "not json"}],
    [{"row_exclusion_manifest": json.dumps({"tables": ["job_events"]})}],
    [{"row_exclusion_manifest": json.dumps({"tables": {"job_events": {"token": ""}}})}],
    [{"row_exclusion_manifest": json.dumps(
        {"tables": {"job_events": {"token": "t", "pending": "yes"}}})}],
], ids=["no_system_row", "no_answer", "not_json", "list", "empty_token", "pending_not_bool"])
def test_no_system_row_or_an_unparsable_value_is_unknown_never_empty(rows):
    with pytest.raises(manifest.ManifestUnknown):
        manifest.from_rows(rows)


def test_the_value_round_trips_and_an_empty_map_removes_the_property():
    entries = {"job_events": manifest.Entry("a1", True), "ledger_lines": manifest.Entry("b2")}
    assert manifest.parse(manifest.serialize(entries)) == entries
    assert manifest.serialize({}) is None


# --------------------------------------------------------------------------- #
# Set: listed (pending) before the fact, settled after.
# --------------------------------------------------------------------------- #
def _writes(g) -> list:
    return [k for k in g.kinds() if k in {"swap", "set_fact"}]


def test_set_lists_the_table_pending_before_writing_the_fact_and_settles_it(g):
    seen = []
    g.before_write = lambda kind, _p: seen.append(
        (kind, dict(g.manifest("sys-a")), g.stored("sys-a", "job_events")))
    assert _set(g)["status"] == "ok"
    assert _writes(g) == ["swap", "set_fact", "swap"]
    # The fact is written only once the table is listed (pending).
    kind, listed, stored = seen[1]
    assert kind == "set_fact" and listed["job_events"]["pending"] is True and stored is None
    final = g.manifest("sys-a")["job_events"]
    assert final["pending"] is False and final["token"] == listed["job_events"]["token"]
    assert g.stored("sys-a", "job_events")["conditions"] == _CONDITIONS


def test_set_crash_before_the_fact_leaves_the_table_listed_and_nothing_stored(g):
    g.refuse.add("set_fact")
    with pytest.raises(rx.RowExclusionWriteFailed):
        _set(g)
    assert g.manifest("sys-a")["job_events"]["pending"] is True
    assert g.stored("sys-a", "job_events") is None


def test_set_crash_after_the_fact_leaves_it_stored_and_listed_pending(g):
    def refuse_the_settle(kind, _p):
        if kind == "swap" and g.stored("sys-a", "job_events") is not None:
            g.refuse.add("swap")
    g.before_write = refuse_the_settle
    with pytest.raises(manifest.ManifestWriteFailed):
        _set(g)
    assert g.stored("sys-a", "job_events") is not None
    assert g.manifest("sys-a")["job_events"]["pending"] is True


def test_a_refused_first_manifest_write_writes_no_fact(g):
    g.refuse.add("swap")
    with pytest.raises(manifest.ManifestWriteFailed):
        _set(g)
    assert "set_fact" not in g.kinds()
    assert g.manifest("sys-a") == {} and g.stored("sys-a", "job_events") is None


def test_a_corrupt_manifest_is_never_overwritten(g):
    g.manifests["sys-a"] = "not json"
    with pytest.raises(manifest.ManifestUnknown):
        _set(g)
    assert g.manifests["sys-a"] == "not json"
    assert _writes(g) == []


def test_an_invalid_body_writes_nothing_at_all(g):
    out = _set(g, conditions=[{"column": "qty", "op": "empty"}])
    assert out["status"] == "validation_failed" and out["reason"] == "column_not_string"
    assert _writes(g) == [] and g.manifest("sys-a") == {}


def test_a_concurrent_writer_never_loses_an_entry(g):
    """Two stewards set different tables at once: each compare-and-set sees
    the other's entry and keeps it."""
    def other_steward_lists_ledger(kind, _p):
        if kind == "swap" and "ledger_lines" not in g.manifest("sys-a"):
            g.list_table("sys-a", "ledger_lines", token="other")
    g.before_write = other_steward_lists_ledger
    assert _set(g)["status"] == "ok"
    listed = g.manifest("sys-a")
    assert set(listed) == {"job_events", "ledger_lines"}
    assert listed["ledger_lines"]["token"] == "other"


def test_a_writer_that_keeps_changing_the_manifest_is_reported(g):
    counter = {"n": 0}

    def keeps_changing(kind, _p):
        if kind == "swap":
            counter["n"] += 1
            g.list_table("sys-a", f"other_{counter['n']}")
    g.before_write = keeps_changing
    with pytest.raises(manifest.ManifestWriteFailed):
        _set(g)
    assert "set_fact" not in g.kinds()


# --------------------------------------------------------------------------- #
# Clear: pending, the fact cleared, then removed.
# --------------------------------------------------------------------------- #
def test_clear_marks_pending_clears_the_fact_then_removes_the_table(g):
    _set(g)
    g.calls.clear()
    seen = []
    g.before_write = lambda kind, _p: seen.append(
        (kind, dict(g.manifest("sys-a")), g.stored("sys-a", "job_events")))
    assert _clear(g)["status"] == "ok"
    assert _writes(g) == ["swap", "set_fact", "swap"]
    kind, listed, stored = seen[1]
    assert kind == "set_fact" and listed["job_events"]["pending"] is True and stored is not None
    kind, listed, stored = seen[2]
    assert kind == "swap" and stored is None and "job_events" in listed
    assert g.manifest("sys-a") == {} and g.manifests["sys-a"] is None
    assert g.stored("sys-a", "job_events") is None


def test_clear_crash_before_the_fact_is_cleared_keeps_it_listed(g):
    _set(g)
    g.refuse.add("set_fact")
    with pytest.raises(rx.RowExclusionWriteFailed):
        _clear(g)
    assert g.stored("sys-a", "job_events") is not None
    assert g.manifest("sys-a")["job_events"]["pending"] is True


def test_clear_crash_after_the_fact_is_cleared_leaves_the_table_listed_pending(g):
    _set(g)

    def refuse_the_removal(kind, _p):
        if kind == "swap" and g.stored("sys-a", "job_events") is None:
            g.refuse.add("swap")
    g.before_write = refuse_the_removal
    with pytest.raises(manifest.ManifestWriteFailed):
        _clear(g)
    assert g.stored("sys-a", "job_events") is None
    assert g.manifest("sys-a")["job_events"]["pending"] is True


def test_clearing_a_listed_table_whose_entry_is_gone_only_unlists_it(g):
    g.list_table("sys-a", "retired_table")
    assert _clear(g, "retired_table")["status"] == "ok"
    assert "set_fact" not in g.kinds() and g.manifest("sys-a") == {}


def test_clearing_an_unlisted_unknown_table_is_refused(g):
    out = _clear(g, "ghost")
    assert out["status"] == "validation_failed" and out["reason"] == "unknown_table"
    assert _writes(g) == []


def test_an_unreadable_manifest_refuses_the_clear_and_writes_nothing(g):
    _set(g)
    g.calls.clear()
    g.fail.add("manifest")
    with pytest.raises(RuntimeError):
        _clear(g)
    assert _writes(g) == [] and g.stored("sys-a", "job_events") is not None


def test_the_route_reads_and_writes_through_checked_seams():
    from agents import admin_routes, db
    write, query = admin_routes._row_exclusion_graph_seams()
    assert write is db.neptune_write_returning_cas and query is db.neptune_query_checked
