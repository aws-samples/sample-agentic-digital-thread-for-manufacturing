# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A failed manifest read never lets a table run as confirmed absent.

Before this change, a failed manifest read paused a fully known system: for a
fixed time no guarded query read the manifest again, so a steward's first
exclusion, set during that time, was not applied, and an ordinary query of the
table ran unfiltered with no disclosure. A failed read with no pause had the
same gap for a set that landed just before it: the last manifest read was
taken as proof that a table it did not list carries no exclusion.

Now every guarded query reads the manifest, and a read that fails is tried
once more at once. When both attempts fail, every data read of the system is
held: a table is taken as carrying no exclusion, or as carrying the exclusion
last read for it, only on a manifest read made for that query. A fact last
read may have been replaced since (by a rule neither stricter nor looser), so
it only names, in the hold's explanation, what could not be confirmed.
Structural discovery is not held, and a system that never declared an
exclusion answers through a single failed read.

Fully offline (``_row_exclusion_graph.RxGraph``, a fake Athena lake); synthetic
names (the schema of ``test_row_exclusion_unknown_states``).
"""
from __future__ import annotations

import pytest

from agents import record_key
from agents import row_exclusion as rx
from tests.unit.test_row_exclusion_graph_faults import _registry_graph, _steps
from tests.unit.test_row_exclusion_unknown_states import (
    _COUNT, _DISCOVERY, _LEDGER_SUM, _RULE, _SYS, _WIDE, _graph, _pred, _resets)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    for reset in _resets():
        reset()
    yield
    for reset in _resets():
        reset()


def _set_rule(g):
    def run():
        return rx.set_row_exclusion(_SYS, _WIDE, _RULE["conditions"], label=_RULE["label"],
                                    write=g.write, query=g.query)
    return run


def _fail_manifest(g, times: int):
    def run():
        g.fail_next["manifest"] = times
    return run


def _ran(payload: dict) -> bool:
    return "rows" in payload


# --------------------------------------------------------------------------- #
# A steward's first exclusion and a failed read (the review's schedule).
# --------------------------------------------------------------------------- #
def test_a_first_exclusion_set_after_a_failed_read_applies_on_the_next_query(monkeypatch):
    """A never-configured system's manifest read fails once and the graph is
    back; the system still answers. A steward then sets a first exclusion:
    the very next query of that table is refused for the missing predicate,
    never run unfiltered."""
    g = _graph()
    out = _steps(monkeypatch, g, [_fail_manifest(g, 1), _COUNT, _set_rule(g), _COUNT])
    _fail, before, stored, after = out
    assert before["rows"] == [["42"]] and "row_exclusion" not in before
    assert stored["status"] == "ok"
    assert not _ran(after) and after["error_name"] == "ROW_EXCLUSION"


@pytest.mark.parametrize("failures", [1, 2], ids=["one_failed_read", "both_attempts_failed"])
def test_a_set_that_lands_just_before_a_failed_read_is_never_run_unfiltered(
        monkeypatch, failures):
    """The set lands, then the next manifest read fails. One failure is
    absorbed by the second attempt, which sees the set: refused for the
    missing predicate. When both attempts fail, the last manifest read did not
    list the table, so it is held as unavailable; once the graph is back the
    same query is checked against the new fact."""
    g = _graph()
    out = _steps(monkeypatch, g, [_set_rule(g), _fail_manifest(g, failures), _COUNT, _COUNT])
    _stored, _fail, first, later = out
    assert not _ran(first)
    if failures == 1:
        assert first["error_name"] == "ROW_EXCLUSION"
    else:
        assert first["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
        assert first["unavailable"] == "read_failed"
        assert later["error_name"] == "ROW_EXCLUSION"


# --------------------------------------------------------------------------- #
# Both attempts fail: every data read is held, a listed table's included.
# --------------------------------------------------------------------------- #
def test_both_attempts_failing_hold_every_data_read_and_discovery_runs(monkeypatch):
    g = _graph(listed=(_WIDE,))

    def manifest_down():
        g.fail.add("manifest")

    def manifest_back():
        g.fail.discard("manifest")
    out = _steps(monkeypatch, g, [manifest_down, _COUNT, _COUNT, _LEDGER_SUM, _DISCOVERY,
                                  manifest_back, _LEDGER_SUM])
    _down, first, resend, ledger, discovery, _back, healed = out
    # The listed table: the fact last read for it cannot be confirmed current.
    for held in (first, resend):
        assert not _ran(held) and held["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
        assert held["unavailable"] == "read_failed"
        assert "may have changed since" in held["guidance"]
    # A table the manifest did not list is not confirmed absent: held.
    assert not _ran(ledger) and ledger["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert "this system's tables" in ledger["guidance"]
    # Structural discovery still runs.
    assert _ran(discovery)
    # The next query after the graph is back reads the manifest and runs.
    assert healed["rows"] == [["42"]]


#: A second rule for the wide table, neither stricter nor looser than ``_RULE``.
_OTHER_RULE = {"label": "test rows", "conditions": [{"column": "state", "op": "empty"}]}


def test_a_replaced_exclusion_is_not_applied_from_its_cached_copy_after_two_failed_reads(
        monkeypatch):
    """The table carries one rule and a query that applies it runs. A steward
    replaces it with a rule neither stricter nor looser, and both manifest
    reads of the next query fail. A query carrying the OLD rule's predicate is
    held before it reaches the engine: the cached rule cannot be confirmed
    current. Once the graph is back the same query is checked against the new
    rule (refused for its missing predicate)."""
    from tests.unit.test_row_exclusion_unknown_states import _Lake
    sent: list = []
    original = _Lake.start_query_execution

    def recorded(self, QueryString, **kwargs):
        sent.append(QueryString)
        return original(self, QueryString, **kwargs)
    monkeypatch.setattr(_Lake, "start_query_execution", recorded)
    g = _graph(listed=(_WIDE,))
    old_rule = f"{_COUNT} AND {_pred()}"

    def replace_rule():
        return rx.set_row_exclusion(_SYS, _WIDE, _OTHER_RULE["conditions"],
                                    label=_OTHER_RULE["label"], write=g.write, query=g.query)
    out = _steps(monkeypatch, g, [old_rule, replace_rule, _fail_manifest(g, 2), old_rule,
                                  old_rule])
    applied, stored, _fail, held, later = out
    assert applied["rows"] == [["42"]] and applied["row_exclusion"]["applied"] is True
    assert stored["status"] == "ok"
    assert not _ran(held) and held["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert held["unavailable"] == "read_failed"
    assert "may have changed since" in held["guidance"]
    assert later["error_name"] == "ROW_EXCLUSION" and "COALESCE(state, '') = ''" in (
        later["guidance"])
    data = [q for q in sent if "$partitions" not in q and "information_schema" not in q]
    assert len(data) == 1                     # only the first query reached the engine


def test_a_pending_table_is_held_and_stays_held_when_the_manifest_cannot_be_read(
        monkeypatch):
    """A pending entry with nothing stored is held (an interrupted set). If
    the set then completes and the next manifest read fails, reads of the
    table stay held, not run unfiltered."""
    g = _registry_graph(monkeypatch, listed=())
    g.list_table("sys", "job_events", token="t1", pending=True)
    rx.remember_from_graph("sys")
    pending = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
    assert pending is not None and pending.kind == rx.WRITE_PENDING
    g.store("sys", "job_events", {"label": "shadow copies",
                                  "conditions": [{"column": "pipeline", "op": "not_empty"}]})
    g.list_table("sys", "job_events", token="t1", pending=False)
    g.fail.add("manifest")
    held = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
    assert held is not None and held.kind in (rx.WRITE_PENDING, rx.READ_FAILED)


# --------------------------------------------------------------------------- #
# No pause: every guarded query reads the manifest.
# --------------------------------------------------------------------------- #
def test_every_guarded_query_reads_the_manifest_after_a_failed_read(monkeypatch):
    g = _registry_graph(monkeypatch, listed=())
    rx.remember_from_graph("sys")
    g.fail.add("manifest")
    g.calls.clear()
    for _ in range(3):
        held = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
        assert held is not None and held.table == rx.ALL_TABLES
    assert g.kinds() == ["manifest"] * 6          # each query: a read and one more attempt
    g.fail.clear()
    g.calls.clear()
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    assert g.kinds() == ["manifest", "manifest"]
