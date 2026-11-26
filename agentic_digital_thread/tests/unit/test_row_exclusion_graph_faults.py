# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A graph fault only matters for the tables a system's manifest lists.

Before this change, making an unread fact unavailable was right, but every
described table of every Athena system then depended on the row-exclusion
graph read, and each tool attempt re-read every table. A graph fault refused
reads on systems that never declared an exclusion.

Now the system agent reads the system's MANIFEST before each guarded query and
only the listed tables that need it:

* a system whose manifest is confirmed empty reads no fact, so a fact-read
  fault never refuses its reads;
* with one table listed, a fault refuses reads of that table only;
* a failed manifest re-read is tried once more; when that fails too, every
  data read is held, a listed table's included
  (``test_row_exclusion_failed_manifest_read``);
* a settled table is not read again until its token changes; the tables that
  need a read are read in ONE batch (facts) plus ONE batch (Fields);
* a later consistent read heals an unavailable table on the next guarded call,
  and a steward's set or clear takes effect on the next query, without a
  rebuild.

Fully offline; synthetic names (the measured-shape schema of
``test_row_exclusion_unknown_states``).
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_key
from agents import row_exclusion as rx
from tests.unit._row_exclusion_graph import RxGraph
from tests.unit.test_row_exclusion_unknown_states import (
    _CFG, _COUNT, _LEDGER, _LEDGER_SUM, _RULE, _SYS, _WIDE, _Lake, _ask, _graph, _pred, _resets)

_FACT_KINDS = {"fact", "facts", "field_types", "fields"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    for reset in _resets():
        reset()
    yield
    for reset in _resets():
        reset()


def _then(first, after):
    def run(*args, **kwargs):
        out = first(*args, **kwargs)
        after()
        return out
    return run


def _fault_after_bake(monkeypatch, g, kinds):
    """``kinds`` start failing once the bake has read the graph."""
    monkeypatch.setattr(sa, "_remember_turn_time_facts",
                        _then(sa._remember_turn_time_facts, lambda: g.fail.update(kinds)))


# --------------------------------------------------------------------------- #
# A fault in the fact read.
# --------------------------------------------------------------------------- #
def test_a_never_configured_system_reads_no_fact_so_a_fact_read_fault_refuses_nothing(
        monkeypatch):
    g = _graph()
    g.fail.update(_FACT_KINDS)
    (count, total), answer, lake = _ask(monkeypatch, g, [_COUNT, _LEDGER_SUM])
    assert count["rows"] == [["42"]] and total["rows"] == [["42"]]
    assert "row_exclusion" not in count and "Row exclusion" not in answer
    assert not _FACT_KINDS & set(g.kinds())
    assert len(lake.data_queries()) == 2


def test_with_one_configured_table_a_fact_read_fault_refuses_only_that_table(monkeypatch):
    g = _graph(listed=(_WIDE,))
    g.fail.update(_FACT_KINDS)
    (count, again, total), answer, lake = _ask(monkeypatch, g, [_COUNT, _COUNT, _LEDGER_SUM])
    assert count["error_name"] == again["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert count["table"] == _WIDE
    assert total["rows"] == [["42"]]
    assert lake.data_queries() == [f"{_LEDGER_SUM} LIMIT 500"]
    assert "could not be confirmed" in answer


@pytest.mark.parametrize("listed", [(), (_WIDE,)], ids=["never_configured", "one_configured"])
def test_a_fault_after_a_good_bake_holds_every_data_read(monkeypatch, listed):
    """The bake read the graph; then every graph read fails. While the
    manifest cannot be read no table is taken as carrying no exclusion, nor
    as carrying the one last read for it (it may have been replaced since):
    every data read is held, on a never-configured system too. The fact last
    read only names what the hold could not confirm."""
    g = _graph(listed=listed)
    _fault_after_bake(monkeypatch, g, {"manifest"} | _FACT_KINDS)
    (first, resend, total), answer, lake = _ask(monkeypatch, g, [_COUNT, _COUNT, _LEDGER_SUM])
    for held in (first, resend, total):
        assert held["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
        assert "this system's tables" in held["guidance"]
    assert ("may have changed since" in first["guidance"]) is bool(listed)
    assert "could not be confirmed" in answer
    assert lake.data_queries() == []


def test_a_later_consistent_read_heals_an_unavailable_table_on_the_next_call(monkeypatch):
    """The bake's fact read fails; the graph is back before the first query:
    that query's re-read heals the table, so it is checked like any other read
    (refused once for the missing predicate), and the predicate form runs. No
    rebuild."""
    g = _graph(listed=(_WIDE,))
    g.fail.update(_FACT_KINDS)
    monkeypatch.setattr(sa, "_remember_turn_time_facts",
                        _then(sa._remember_turn_time_facts, g.fail.clear))
    (first, second), answer, lake = _ask(monkeypatch, g, [_COUNT, f"{_COUNT} AND {_pred()}"])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert second["rows"] == [["42"]] and second["row_exclusion"]["applied"] is True
    assert len(lake.data_queries()) == 1
    assert "with the source owner's row exclusion" in answer


# --------------------------------------------------------------------------- #
# A steward's change reaches a running agent on its next query.
# --------------------------------------------------------------------------- #
def _steps(monkeypatch, g, steps) -> list:
    """Bake system-a from ``g``, then in ONE Mode A call run each step: a SQL
    string through the guarded attempt, or a callable (a steward write)."""
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], "", connection_type="Athena")
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    cfg = at._resolve_config(_CFG)
    call = sa._ModeACall("How many steps failed?", windowed=False)
    token = sa._MODE_A_CALL.set(call)
    out = []
    try:
        for step in steps:
            out.append(step() if callable(step)
                       else sa._scoped_athena_attempt(step, cfg, {}, _SYS))
    finally:
        sa._MODE_A_CALL.reset(token)
    return out


def test_a_running_agent_sees_a_set_and_a_clear_on_its_next_query(monkeypatch):
    g = _graph()

    def set_rule():
        return rx.set_row_exclusion(_SYS, _WIDE, _RULE["conditions"], label=_RULE["label"],
                                    write=g.write, query=g.query)

    def clear_rule():
        return rx.clear_row_exclusion(_SYS, _WIDE, write=g.write, query=g.query)
    before, _set, after_set, _clear, after_clear = _steps(
        monkeypatch, g, [_COUNT, set_rule, f"{_COUNT} AND state <> 'X'", clear_rule,
                         f"{_COUNT} AND state <> 'Y'"])
    assert before["rows"] == [["42"]]
    assert _set["status"] == "ok" and _clear["status"] == "ok"
    assert after_set["error_name"] == "ROW_EXCLUSION"
    assert after_clear["rows"] == [["42"]] and "row_exclusion" not in after_clear


# --------------------------------------------------------------------------- #
# What is read, and when (the registry directly).
# --------------------------------------------------------------------------- #
_JOB_RULE = {"label": "shadow copies", "conditions": [{"column": "pipeline", "op": "not_empty"}]}
_LEDGER_RULE = {"label": "test rows", "conditions": [{"column": "batch_tag", "op": "empty"}]}
_TABLES = {"job_events": {"pipeline": "varchar", "state": "varchar"},
           "ledger_lines": {"batch_tag": "varchar", "amount": "double"},
           "shift_notes": {"author": "varchar", "body": "varchar"}}


def _registry_graph(monkeypatch, listed=("job_events", "ledger_lines")) -> RxGraph:
    g = RxGraph({"sys": _TABLES})
    rules = {"job_events": _JOB_RULE, "ledger_lines": _LEDGER_RULE,
             "shift_notes": {"label": "drafts", "conditions": [{"column": "author",
                                                                "op": "empty"}]}}
    for table in listed:
        g.list_table("sys", table)
        g.store("sys", table, rules[table])
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    return g


def test_the_tables_that_need_a_read_are_read_in_one_batch(monkeypatch):
    g = _registry_graph(monkeypatch, listed=tuple(_TABLES))
    rx.remember_from_graph("sys")
    assert g.kinds() == ["manifest", "facts", "fields"]
    facts = [p for kind, p in g.calls if kind == "facts"][0]
    assert sorted(facts["seids"]) == sorted(f"sys.{t}" for t in _TABLES)
    assert set(rx.exclusions_for("sys")) == set(_TABLES)


def test_a_settled_table_is_not_read_again(monkeypatch):
    g = _registry_graph(monkeypatch)
    rx.remember_from_graph("sys")
    g.calls.clear()
    for _ in range(3):
        assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    assert g.kinds() == ["manifest"] * 3


def test_only_a_retokened_table_is_read_again_and_a_failed_read_keeps_the_others(
        monkeypatch):
    g = _registry_graph(monkeypatch)
    rx.remember_from_graph("sys")
    g.list_table("sys", "ledger_lines", token="t1")          # a steward set it again
    g.fail.update(_FACT_KINDS)
    g.calls.clear()
    held = rx.unavailable_for("sys", "SELECT SUM(amount) FROM ledger_lines")
    assert held is not None and held.table == "ledger_lines" and held.kind == "read_failed"
    assert [p["seids"] for kind, p in g.calls if kind == "facts"] == [["sys.ledger_lines"]]
    # job_events was not re-read and keeps its last-known-good fact.
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    assert rx.decide_for("sys", "SELECT COUNT(*) FROM job_events").refuse is True


def test_a_table_the_manifest_no_longer_lists_is_dropped(monkeypatch):
    g = _registry_graph(monkeypatch)
    rx.remember_from_graph("sys")
    g.manifests["sys"] = None
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    assert rx.exclusions_for("sys") == {}


def test_a_pending_entry_is_read_on_every_query_until_it_settles(monkeypatch):
    g = _registry_graph(monkeypatch, listed=("job_events",))
    g.list_table("sys", "job_events", token="t1", pending=True)
    rx.remember_from_graph("sys")
    g.calls.clear()
    rx.unavailable_for("sys", "SELECT 1 AS n")
    rx.unavailable_for("sys", "SELECT 1 AS n")
    assert g.kinds().count("facts") == 2
    g.list_table("sys", "job_events", token="t1", pending=False)   # the write settled
    g.calls.clear()
    rx.unavailable_for("sys", "SELECT 1 AS n")
    rx.unavailable_for("sys", "SELECT 1 AS n")
    assert g.kinds().count("facts") == 1


def test_an_unknown_system_is_asked_on_every_query_and_heals_at_once(monkeypatch):
    g = _registry_graph(monkeypatch, listed=())
    g.fail.add("manifest")
    rx.remember_from_graph("sys")
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events").table == rx.ALL_TABLES
    g.fail.clear()
    assert rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events") is None
    # The bake and the first query each try twice; the healed query reads once.
    assert g.kinds() == ["manifest"] * 5


def test_a_whole_bake_fault_keeps_the_last_known_good_state(monkeypatch):
    g = _registry_graph(monkeypatch)
    rx.remember_from_graph("sys")
    rx.mark_unavailable("sys")
    assert set(rx.exclusions_for("sys")) == {"job_events", "ledger_lines"}
    rx.reset()                                       # a new process: nothing known yet
    g.fail.add("manifest")
    rx.mark_unavailable("sys")
    held = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
    assert held is not None and held.table == rx.ALL_TABLES


def test_a_fault_while_applying_a_manifest_read_never_raises_into_the_query(monkeypatch):
    """A fault after the manifest was read (in taking it in) is decided as a
    failed read: every data read is held, the facts last read are kept only
    to explain the hold, and nothing raises into the guarded query; with
    nothing known the system is unknown."""
    g = _registry_graph(monkeypatch)
    rx.remember_from_graph("sys")

    def broken(*_a, **_k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(rx, "_apply", broken)
    g.list_table("sys", "ledger_lines", token="t1")
    held = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
    assert held is not None and held.table == rx.ALL_TABLES
    assert held.last_read == ("job_events", "ledger_lines")
    assert set(rx.exclusions_for("sys")) == {"job_events", "ledger_lines"}
    rx.reset()
    rx.remember_from_graph("sys")
    held = rx.unavailable_for("sys", "SELECT COUNT(*) FROM job_events")
    assert held.table == rx.ALL_TABLES and held.last_read == ()
