# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Each window group of a block's conditional aggregates is its own decision.

A block's conditional aggregates can count states on several windows, and can
count the SAME state on two windows as two separate figures (failed records by
their completion time beside failed records by their start time). One record
cannot hold one state on two windows as two figures, so the engine returns each
window group and each OR path as its own :class:`StateTimeWindow` part
(``state_times.decide_parts``), each decided exactly as the filtered or IN-pin
query it equals:

* every part is disclosed when the query runs: the engine line and the
  FINAL-ANSWER REQUIREMENTS carry each part's rows, so the same state counted
  on two windows is described on both;
* a refusing part refuses with its own facts: when only one window is named,
  the refusal names the unnamed window that caused it;
* each part keeps its own ``named`` flag, so a block with one named and one
  unnamed window is marked per part;
* in the system agent, each refusing part fires its own refusal, so one
  refusal carries every refusing part's remedy (and any other rule's), and the
  identical resend runs and discloses every part.

Each case is paired with its hand-written equivalent queries, on the ``events``
shape and a differently named ``job_runs`` table. Fully offline; synthetic
names.
"""
from __future__ import annotations

import importlib

import pytest

import agents.athena_tools as at
from agents import storage_bounds

from .test_state_time_case_count import _ask, _Lake

_TS = "timestamp(6) with time zone"

_COLUMNS = {"id": "varchar", "state": "varchar", "complete_time": _TS, "fail_time": _TS,
            "start_time": _TS, "cancel_time": _TS, "updated_time": _TS, "qty": "double",
            "cell": "varchar"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED, CANCELLED; "
         f"complete_time: {_TS}; fail_time: {_TS}; start_time: {_TS}; cancel_time: {_TS}; "
         f"updated_time: {_TS}; qty: double; cell: varchar e.g. c1, c2)")

_JR_COLUMNS = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "cancel_time": _TS, "updated_time": _TS, "elapsed_s": "double",
               "line": "varchar"}
_JR_DESC = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED, CANCELLED; "
            f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; cancel_time: {_TS}; "
            f"updated_time: {_TS}; elapsed_s: double; line: varchar e.g. a, b)")

_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _w(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


def _st():
    return importlib.import_module("agents.state_times")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _st().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _st().reset()


def _args(sql, table, desc):
    from agents import storage_bound_rule
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    return bounded, _st().from_tables_desc(desc)[table]


def _parts(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    bounded, mapping = _args(sql, table, desc)
    return _st().decide_parts(sql, table, bounded, columns, mapping, question=question)


def _decide(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    bounded, mapping = _args(sql, table, desc)
    return _st().decide(sql, table, bounded, columns, mapping, question=question)


def _facts(found):
    """Everything a part decides: refuse, named, under_or and its record."""
    return (found.refuse, found.named, found.under_or, found.to_dict())


def _windows(found):
    return {s: sorted(ws) for s, ws in found.to_dict()["windows"].items()}


def _pinned(table, column, state, window):
    return f"SELECT COUNT(*) FROM {table} WHERE {column} = '{state}' AND {_w(window)}"


# --------------------------------------------------------------------------- #
# The same state counted on two windows: two parts, both described.
# --------------------------------------------------------------------------- #
_DUP = ("SELECT "
        f"COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) AS by_complete, "
        f"COUNT(CASE WHEN state = 'FAILED' AND {_w('start_time')} THEN 1 END) AS by_start "
        "FROM events")
_DUP_BOTH_Q = "For failed records, how many were completed and how many were started on January 5?"
_DUP_COMPLETED_Q = "Of records completed on January 5, how many are failed?"

_JR_DUP = ("SELECT "
           f"COUNT(CASE WHEN status = 'ABORTED' AND {_w('finish_time')} THEN 1 END) AS by_finish, "
           f"COUNT(CASE WHEN status = 'ABORTED' AND {_w('queue_time')} THEN 1 END) AS by_queue "
           "FROM job_runs")
_JR_DUP_BOTH_Q = "For aborted runs, how many were finished and how many were queued on January 5?"
_JR_DUP_FINISHED_Q = "Of runs finished on January 5, how many are aborted?"

_CASES = {
    "events": dict(table="events", desc=_DESC, columns=_COLUMNS, column="state",
                   sql=_DUP, both_q=_DUP_BOTH_Q, first_q=_DUP_COMPLETED_Q, state="FAILED",
                   first="complete_time", second="start_time"),
    "job_runs": dict(table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS, column="status",
                     sql=_JR_DUP, both_q=_JR_DUP_BOTH_Q, first_q=_JR_DUP_FINISHED_Q,
                     state="ABORTED", first="finish_time", second="queue_time"),
}


def _equivalents(case, question):
    kw = dict(table=case["table"], desc=case["desc"], columns=case["columns"])
    return tuple(_decide(_pinned(case["table"], case["column"], case["state"], w),
                         question=question, **kw)
                 for w in (case["first"], case["second"]))


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_same_state_on_two_windows_is_two_parts_like_its_two_pinned_queries(name):
    case = _CASES[name]
    kw = dict(table=case["table"], desc=case["desc"], columns=case["columns"])
    parts = _parts(case["sql"], question=case["both_q"], **kw)
    want = _equivalents(case, case["both_q"])
    assert all(w is not None for w in want)
    assert len(parts) == 2
    assert [_facts(p) for p in parts] == [_facts(w) for w in want]
    assert [_windows(p) for p in parts] == [{case["state"]: [case["first"]]},
                                            {case["state"]: [case["second"]]}]
    # Both owner words named: nothing refuses, both are disclosed.
    assert not any(p.refuse for p in parts)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_both_windows_of_the_same_state_reach_the_line_and_the_requirements(name):
    st = _st()
    case = _CASES[name]
    kw = dict(table=case["table"], desc=case["desc"], columns=case["columns"])
    parts = _parts(case["sql"], question=case["both_q"], **kw)
    line = st.engine_line(parts)
    block = st.requirement_block(parts)
    # The line equals the two hand-written queries' windows rendered together.
    assert line == st.engine_line(list(_equivalents(case, case["both_q"])))
    s = case["state"]
    assert f"{s} rows were placed in the window by {case['first']}" in line
    assert f"{s} rows were placed in the window by {case['second']}" in line
    assert block.count("REQUIRED:") == 2
    assert f"place {s} rows in the window by {case['first']}" in block
    assert f"place {s} rows in the window by {case['second']}" in block


@pytest.mark.parametrize("name", sorted(_CASES))
def test_only_the_first_window_named_refuses_with_the_unnamed_part(name):
    st = _st()
    case = _CASES[name]
    kw = dict(table=case["table"], desc=case["desc"], columns=case["columns"])
    parts = _parts(case["sql"], question=case["first_q"], **kw)
    want = _equivalents(case, case["first_q"])
    assert [_facts(p) for p in parts] == [_facts(w) for w in want]
    named, unnamed = parts
    assert named.refuse is False and unnamed.refuse is True
    guidance = st.StateTimeWindowError(unnamed).to_dict()["guidance"]
    assert case["second"] in guidance
    assert case["first"] not in guidance


def test_the_decide_contract_none_one_or_every_part():
    """``decide`` never returns one part of several: ``None`` for none, the
    part for one, the tuple for several."""
    st = _st()
    assert _decide(f"SELECT COUNT(*) FROM events WHERE {_w('fail_time')}",
                   question="How many on January 5?") is None
    one = _decide(_pinned("events", "state", "FAILED", "complete_time"),
                  question=_DUP_COMPLETED_Q)
    assert isinstance(one, st.StateTimeWindow)
    several = _decide(_DUP, question=_DUP_BOTH_Q)
    assert isinstance(several, tuple) and len(several) == 2
    assert several == _parts(_DUP, question=_DUP_BOTH_Q)


# --------------------------------------------------------------------------- #
# Mixed named status: each part keeps its own named flag in the line.
# --------------------------------------------------------------------------- #
_MIXED = ("SELECT "
          f"COUNT(CASE WHEN state = 'CANCELLED' AND {_w('complete_time')} THEN 1 END), "
          f"COUNT(CASE WHEN state = 'STARTED' AND {_w('fail_time')} THEN 1 END) "
          "FROM events")
_MIXED_Q = "Of records completed on January 5, how many are cancelled or started?"
_JR_MIXED = ("SELECT "
             f"COUNT(CASE WHEN status = 'CANCELLED' AND {_w('finish_time')} THEN 1 END), "
             f"COUNT(CASE WHEN status = 'QUEUED' AND {_w('abort_time')} THEN 1 END) "
             "FROM job_runs")
_JR_MIXED_Q = "Of runs finished on January 5, how many are cancelled or queued?"


@pytest.mark.parametrize("sql,question,kw", [
    (_MIXED, _MIXED_Q, {}),
    (_JR_MIXED, _JR_MIXED_Q, dict(table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)),
], ids=["events", "job_runs"])
def test_a_named_and_an_unnamed_window_are_marked_per_part(sql, question, kw):
    st = _st()
    parts = _parts(sql, question=question, **kw)
    assert [(p.named, p.refuse) for p in parts] == [(True, False), (False, True)]
    line = st.engine_line(parts)
    assert ", a window the question named only by a state word)" in line
    assert ", a window the question did not name)" in line
    assert f"{st.ENGINE_PREFIX} the question did not name," not in line
    assert f"{st.ENGINE_PREFIX} the question named only by a state word," not in line


# --------------------------------------------------------------------------- #
# The system agent: one refusal carrying every refusing part, then the resend
# runs and discloses every part.
# --------------------------------------------------------------------------- #
_TWO_REFUSING = ("SELECT "
                 f"COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) AS f, "
                 f"COUNT(CASE WHEN state = 'STARTED' AND {_w('fail_time')} THEN 1 END) AS s "
                 "FROM events")
# Names no state word of either window's owner (COMPLETED, FAILED).
_UNNAMED_Q = "How many records of each of those two kinds were there on January 5?"


def test_two_refusing_parts_are_one_refusal_with_both_remedies(monkeypatch):
    lake = _Lake(_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_TWO_REFUSING, _TWO_REFUSING],
                                   question=_UNNAMED_Q, desc=_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert [r["error_name"] for r in first["also_refused"]] == ["STATE_TIME_WINDOW"]
    guidance = first["guidance"]
    assert "FAILED rows are windowed on complete_time" in guidance
    assert "STARTED rows are windowed on fail_time" in guidance
    assert second["rows"] == [["42"]]
    record = second["state_time_window"]
    assert isinstance(record, list) and len(record) == 2
    assert [r["windows"] for r in record] == [{"FAILED": ["complete_time"]},
                                              {"STARTED": ["fail_time"]}]
    assert len(lake.data_queries()) == 1
    assert "FAILED rows were placed in the window by complete_time" in answer
    assert "STARTED rows were placed in the window by fail_time" in answer


def test_one_refusing_part_refuses_alone_then_the_resend_discloses_both(monkeypatch):
    lake = _Lake(_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_DUP, _DUP],
                                   question=_DUP_COMPLETED_Q, desc=_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert "also_refused" not in first
    assert first["windows"] == {"FAILED": ["start_time"]}
    assert "start_time" in first["guidance"]
    assert "complete_time" not in first["guidance"]
    assert second["rows"] == [["42"]]
    record = second["state_time_window"]
    assert isinstance(record, list)
    assert [r["windows"] for r in record] == [{"FAILED": ["complete_time"]},
                                              {"FAILED": ["start_time"]}]
    assert len(lake.data_queries()) == 1
    assert "FAILED rows were placed in the window by complete_time" in answer
    assert "FAILED rows were placed in the window by start_time" in answer


def test_both_named_runs_first_time_and_discloses_both(monkeypatch):
    lake = _Lake(_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [_DUP], question=_DUP_BOTH_Q, desc=_DESC)
    assert payload["rows"] == [["42"]]
    assert [r["windows"] for r in payload["state_time_window"]] == [
        {"FAILED": ["complete_time"]}, {"FAILED": ["start_time"]}]
    assert "FAILED rows were placed in the window by complete_time" in answer
    assert "FAILED rows were placed in the window by start_time" in answer


def test_one_part_keeps_the_single_record(monkeypatch):
    """One part: ``state_time_window`` stays the one dict it always was."""
    lake = _Lake(_COLUMNS)
    sql = _pinned("events", "state", "FAILED", "complete_time")
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_DUP_COMPLETED_Q, desc=_DESC)
    assert isinstance(payload["state_time_window"], dict)
    assert payload["state_time_window"]["window_column"] == "complete_time"


# A population column with its own sibling time, beside lifecycle states: the
# record-time rule and two state-time parts all refuse the same SQL.
_RT_COLUMNS = {"id": "varchar", "state": "varchar",
               "quality_data.defect_type": "varchar",
               "quality_data.defect_detected_date": _TS,
               "fail_date": _TS, "complete_date": _TS, "start_date": _TS, "qty": "double"}
_RT_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
            "quality_data.defect_type: varchar e.g. TypeA, TypeB; "
            f"quality_data.defect_detected_date: {_TS}; "
            f"fail_date: {_TS}; complete_date: {_TS}; start_date: {_TS}; qty: double)")
_RT_SQL = ("SELECT COUNT(CASE WHEN state = 'COMPLETED' THEN 1 END) AS c, "
           "COUNT_IF((state = 'STARTED' AND qty > 0) OR (state = 'COMPLETED' AND qty <= 0)) "
           "AS o FROM events WHERE "
           f"{_w('fail_date')} AND quality_data.defect_type IS NOT NULL "
           "AND LOWER(quality_data.defect_type) LIKE '%typea%'")


def test_state_parts_and_a_record_time_refusal_are_one_refusal(monkeypatch):
    parts = _parts(_RT_SQL, question="How many TypeA defects on January 5?",
                   desc=_RT_DESC, columns=_RT_COLUMNS)
    assert len(parts) == 2 and all(p.refuse for p in parts)
    lake = _Lake(_RT_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_RT_SQL, _RT_SQL],
                                   question="How many TypeA defects on January 5?",
                                   desc=_RT_DESC)
    reasons = [first["reason"]] + [r["reason"] for r in first["also_refused"]]
    assert sorted(reasons) == sorted(["window_on_other_state_time"] * 2
                                     + ["window_not_on_record_time"])
    assert "quality_data.defect_detected_date" in first["guidance"]
    assert "COMPLETED rows are windowed on fail_date" in first["guidance"]
    assert "under an OR" in first["guidance"]
    assert second["rows"] == [["42"]]
    assert isinstance(second["state_time_window"], list)
    assert len(second["state_time_window"]) == 2
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer
