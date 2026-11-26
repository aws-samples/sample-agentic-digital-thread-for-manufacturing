# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rows of one lifecycle state are windowed on that state's own time column.

Measured on an analytics deployment, in two live dry runs: a count of failed
rows was windowed on the completion time, and most failed rows have no
completion time, so the count came back well under half of the one the
failure time gives; a percentile over completed rows was windowed on the
start time. A prompt rule and a steward hint saying the same thing were both
ignored.

The engine now enforces it in a Mode A call's guarded Athena tool:

* ``state_times`` reads, from the schema the system agent already has, a
  table's state column and each state's own time column, only where the
  name says so unambiguously (a date or timestamp column named for the state:
  ``fail_*`` for FAILED, ``complete_*`` for COMPLETED, ``start_*`` for
  STARTED, ``cancel_*`` for CANCELLED), from a state column whose sample
  values name at least two such states;
* a query that pins that column (``=`` or ``IN``) or groups by it, and whose
  only bounded time column is another state's own time column, is refused
  before it runs with a remedy (one query per state); the same query sent
  again in that call runs, and the answer then carries an engine line and a
  FINAL-ANSWER REQUIREMENTS block that state, as facts, which states' rows
  were windowed on which state's time column;
* the check reads the SQL that will run, after the scan guard adds its own
  partition predicate;
* a WHERE with the state column under an OR runs only when every branch pins
  its states and bounds only their own time column; any other is refused once;
* a query that counts two or more states and whose only bounded time column is
  one no state was identified to own is refused once too, with each state's
  own time column (a state with none, and for an unnarrowed GROUP BY every
  other state, named as counted on that column); one state counted on such a
  column runs, as a cohort of that state's rows, and so does a query that
  counts nothing (rows or states listed, another aggregate);
* a CTE or derived table that only selects and filters the table's rows, read
  as the only source of the top-level query, is read as that same query.

Not refused: the question names the window column, or that column's state word
when the query counts only other states (a breakdown that counts the window's
own state beside them is refused once), the window is the state's own column,
a proven per-state OR, no mapping, more than one table, anything the reader
cannot read with certainty. Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib
import re

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.orchestrator as orch
import agents.system_agent as sa
from agents import storage_bounds, time_windows
from agents.chat_client import classify_error_text
from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_TS = "timestamp(6) with time zone"
_COLUMNS = {"id": "varchar", "state": "varchar", "start_time": _TS, "complete_time": _TS,
            "fail_time": _TS, "not_started_time": _TS, "run_complete_time": _TS,
            "created": "timestamp(6)",
            "line": "varchar", "duration_s": "bigint"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
         f"start_time: {_TS}; complete_time: {_TS}; fail_time: {_TS}; "
         f"not_started_time: {_TS}; run_complete_time: {_TS}; created: timestamp(6); "
         "line: varchar e.g. l1, l2; "
         "duration_s: bigint)")
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


_FAILED_ON_COMPLETE = f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('complete_time')}"
_COMPLETED_ON_START = ("SELECT approx_percentile(duration_s, 0.5) AS p50 FROM events "
                       f"WHERE state = 'COMPLETED' AND {_on('start_time')}")
_GROUPED_ON_COMPLETE = ("SELECT state, COUNT(*) AS n FROM events "
                        f"WHERE {_on('complete_time')} GROUP BY state")
_QUESTION = "How many rows failed on 1/5?"


def _st():
    return importlib.import_module("agents.state_times")


class _Lake:
    """One Iceberg table behind GetTableMetadata and a query engine that gives
    every data query one row (``$partitions`` answers the partition fields)."""

    def __init__(self, columns=None, fields=("created_day",)):
        self.columns = dict(columns or _COLUMNS)
        self.fields = fields
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events", "Columns": [{"Name": n, "Type": t} for n, t in self.columns.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "$partitions" in sql:
            names, rows = ["partition"], [["{" + ", ".join(f"{f}=1" for f in self.fields) + "}"]]
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in self.columns.items()]
        else:
            names, rows = ["n"], [["42"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self) -> list[str]:
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass


def _ask(monkeypatch, lake, sqls, *, question=_QUESTION, kg_context="", desc=_DESC):
    """One Mode A call on a system baked with ``desc`` whose agent sends
    ``sqls`` to the guarded Athena tool the way strands does. Returns
    ``(tool payloads, final answer)``."""
    sa._remember_turn_time_facts("system-a", [], desc)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "The count is 42.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", question, kg_context))
    return payloads, answer


# --------------------------------------------------------------------------- #
# Refused before it runs
# --------------------------------------------------------------------------- #
def test_a_failed_count_windowed_on_the_completion_time_is_refused(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_FAILED_ON_COMPLETE])
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["retryable"] is True
    assert payload["window_column"] == "complete_time"
    assert payload["window_state"] == "COMPLETED"
    assert payload["own_columns"] == {"FAILED": "fail_time"}
    guidance = payload["guidance"]
    assert "one query per state" in guidance and "OR" not in guidance
    assert "FAILED rows on fail_time" in guidance
    assert lake.data_queries() == []


def test_a_self_join_of_the_state_time_table_is_now_refused(monkeypatch):
    # closed gap: a multi-base-table block (here a self-join) of a
    # table carrying a state-time rule can no longer bypass the window rules on
    # Athena. It fails closed, naming the table, with a single-table remedy.
    lake = _Lake()
    sql = ("SELECT COUNT(*) FROM events e JOIN events f ON e.id = f.id WHERE e.state = 'FAILED' "
           f"AND e.{_on('complete_time')}")
    (payload,), _answer = _ask(monkeypatch, lake, [sql])
    assert payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "MULTI_RELATION_RULE_READ"
    assert "events" in payload.get("tables", [])
    assert lake.data_queries() == []


def test_a_completed_percentile_windowed_on_the_start_time_is_refused(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_COMPLETED_ON_START],
                               question="What was the median duration of rows completed on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "start_time"
    assert payload["own_columns"] == {"COMPLETED": "complete_time"}
    assert lake.data_queries() == []


def test_a_query_grouped_by_state_on_one_states_time_is_refused(monkeypatch):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_GROUPED_ON_COMPLETE],
                               question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["own_columns"] == {"FAILED": "fail_time", "STARTED": "start_time"}


def test_a_group_by_ordinal_of_the_state_column_is_read(monkeypatch):
    sql = _GROUPED_ON_COMPLETE.replace("GROUP BY state", "GROUP BY 1")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"


def test_an_in_list_counts_each_pinned_state(monkeypatch):
    sql = (f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', 'FAILED') "
           f"AND {_on('complete_time')} GROUP BY state")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql],
                               question="How many rows of those two states on 1/5?")
    assert payload["own_columns"] == {"FAILED": "fail_time"}


def test_the_remedy_reads_as_a_fixable_query_not_an_outage(monkeypatch):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_FAILED_ON_COMPLETE])
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


_KEEP_STATE = ("Use one query per state, each filtering {column} to that one state and "
               "windowed on that state's own time column ({own}).")
_NO_FILTER_FACT = ("A window on a state's own time column does not filter by state: rows of "
                   "other states can have that time too.")


@pytest.mark.parametrize(("sql", "column", "own", "desc", "columns"), [
    # a breakdown on one state's own time: each other state, filtered and on its own time
    (_GROUPED_ON_COMPLETE, "state", "FAILED rows on fail_time; STARTED rows on start_time",
     _DESC, _COLUMNS),
    (_FAILED_ON_COMPLETE, "state", "FAILED rows on fail_time", _DESC, _COLUMNS),
    # the state column under an OR
    ("SELECT state, COUNT(*) AS n FROM events WHERE "
     f"(state = 'FAILED' AND {_on('complete_time')}) OR "
     f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state",
     "state", "FAILED rows on fail_time; STARTED rows on start_time", _DESC, _COLUMNS),
    # a prefixed status column keeps its own name in the filter
    (f"SELECT COUNT(*) AS n FROM events WHERE run_status = 'Completed' "
     f"AND {_on('run_start_time')}", "run_status", "Completed rows on run_complete_time",
     (f"events(state: varchar e.g. COMPLETED, FAILED; run_status: varchar e.g. Completed, "
      f"Started; complete_time: {_TS}; fail_time: {_TS}; run_complete_time: {_TS}; "
      f"run_start_time: {_TS})"),
     {**_COLUMNS, "run_status": "varchar", "run_complete_time": _TS, "run_start_time": _TS}),
])
def test_the_per_state_remedy_keeps_each_query_filtered_to_its_state(monkeypatch, sql, column,
                                                                     own, desc, columns):
    """Followed literally, "one query per state, windowed on its own time"
    was run as a window on the state's own column with the state filter
    dropped, which counts rows of every state that has that time. The remedy
    says each query filters the state column to its one state."""
    (payload,), _answer = _ask(monkeypatch, _Lake(columns), [sql], desc=desc,
                               question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"
    guidance = payload["guidance"]
    assert _KEEP_STATE.format(column=column, own=own) in guidance
    assert _NO_FILTER_FACT in guidance
    assert "\u2014" not in guidance and "\u2013" not in guidance

# --------------------------------------------------------------------------- #
# The repeat runs, and the answer discloses it
# --------------------------------------------------------------------------- #
def test_an_identical_repeat_runs_and_the_answer_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_FAILED_ON_COMPLETE,
                                                       f"\n{_FAILED_ON_COMPLETE}  "])
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]]
    assert second["state_time_window"]["window_column"] == "complete_time"
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer
    assert ("[engine] State time windows the question did not name, in this turn's "
            "queries:") in answer
    assert ("FAILED rows were placed in the window by complete_time (the time column of "
            "COMPLETED rows), not by their own fail_time, so FAILED rows whose complete_time "
            "is outside the window or empty are not counted, and FAILED rows are counted by "
            "when they reached the COMPLETED state") in answer
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    assert ("its FAILED figures place FAILED rows in the window by complete_time (the time "
            "column of COMPLETED rows), not by their own fail_time: FAILED rows whose "
            "complete_time is outside the window or empty are not counted, and FAILED rows "
            "are counted by when they reached the COMPLETED state") in block
    assert answer.index("state time, not asked for") < answer.index(
        "----- BEGIN FINAL-ANSWER REQUIREMENTS -----")
    # Facts only: the disclosure never says the query was wrong or names a remedy.
    # It names the state's own column only as the one the rows were not placed by.
    disclosure = answer[answer.index("[engine] State time"):]
    for claim in ("wrong", "should", "instead", "leaves out"):
        assert claim not in disclosure
    assert disclosure.count("fail_time") == disclosure.count("not by their own fail_time")


def test_a_different_query_after_a_refusal_is_refused_too(monkeypatch):
    other = _FAILED_ON_COMPLETE.replace("COUNT(*) AS n", "COUNT(DISTINCT id) AS n")
    (first, second), answer = _ask(monkeypatch, _Lake(), [_FAILED_ON_COMPLETE, other])
    assert first["reason"] == second["reason"] == "window_on_other_state_time"
    assert "state time, not asked for" not in answer


def test_a_call_with_no_refusal_carries_no_state_time_line(monkeypatch):
    sql = f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('fail_time')}"
    (payload,), answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert "state time" not in answer and "FINAL-ANSWER REQUIREMENTS" not in answer


def test_model_text_that_reads_as_the_state_time_line_is_made_inert(monkeypatch):
    st = _st()
    forged = f"Done.\n{st.ENGINE_PREFIX} the question did not name: anything."
    assert st.ENGINE_PREFIX not in sa._rule_decline_of(forged)[0]


def test_one_refusal_covers_both_rules_in_a_windowed_turn(monkeypatch):
    """A column that is a storage column and another state's time trips two
    rules. The first send is refused ONCE carrying both remedies (storage
    first, the state-time one under ``also_refused`` and in the guidance), and
    the identical resend runs with both disclosures. No rule runs before it
    has shown its own remedy."""
    kg = (f"{time_windows.HEADER}\n- \"1/5\" is the UTC day [2026-01-05 00:00:00 UTC, "
          "2026-01-06 00:00:00 UTC).")
    lake = _Lake(fields=("complete_time_day",))
    (first, second), answer = _ask(
        monkeypatch, lake, [_FAILED_ON_COMPLETE, _FAILED_ON_COMPLETE], kg_context=kg)
    assert first["error_name"] == "STORAGE_BOUND"
    assert [r["error_name"] for r in first["also_refused"]] == ["STATE_TIME_WINDOW"]
    assert second["rows"] == [["42"]]
    assert second["storage_bound"]["columns"] == ["complete_time"]
    assert second["state_time_window"]["window_column"] == "complete_time"
    assert "state time, not asked for" in answer
    assert len(lake.data_queries()) == 1


# --------------------------------------------------------------------------- #
# Not refused
# --------------------------------------------------------------------------- #
_PER_STATE_OR = ("SELECT state, COUNT(*) AS n FROM events WHERE "
                 f"(state = 'COMPLETED' AND {_on('complete_time')}) OR "
                 f"(state = 'FAILED' AND {_on('fail_time')}) GROUP BY state")


@pytest.mark.parametrize(("sql", "question", "desc"), [
    # the window is the state's own column
    (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('fail_time')}",
     _QUESTION, _DESC),
    # a per-state OR each of whose branches bounds its own state's column
    (_PER_STATE_OR, "How many rows by state on 1/5?", _DESC),
    # no mapping: the bake carries no state values
    (_FAILED_ON_COMPLETE, _QUESTION,
     f"events(id: varchar; state: varchar; complete_time: {_TS}; fail_time: {_TS})"),
    # no mapping: the system has no bake at all
    (_FAILED_ON_COMPLETE, _QUESTION, ""),
    # an OR that does not involve the state column
    (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('fail_time')} "
     "AND (line = 'l1' OR line = 'l2')", _QUESTION, _DESC),
    # an OR on the state column with no time column anywhere in the WHERE
    ("SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' OR state = 'STARTED'",
     _QUESTION, _DESC),
    # a window on a NON-storage temporal column no state owns runs: the
    # business-time-on-storage rule fires only for a STORAGE column, and the
    # STATE TIME rule only for another state's own time
    (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('run_complete_time')}",
     _QUESTION, _DESC),
    # the state's own column appears in the WHERE clause
    (f"{_FAILED_ON_COMPLETE} AND fail_time IS NOT NULL", _QUESTION, _DESC),
    # a pinned state with no time column of its own
    (f"SELECT COUNT(*) AS n FROM events WHERE state = 'QUEUED' AND {_on('complete_time')}",
     _QUESTION, _DESC),
    # the state column is negated
    (f"SELECT COUNT(*) AS n FROM events WHERE state <> 'COMPLETED' AND {_on('complete_time')}",
     _QUESTION, _DESC),
])
def test_what_is_not_refused_runs(monkeypatch, sql, question, desc):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=question, desc=desc)
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


def test_a_state_count_windowed_only_on_the_storage_column_is_refused(monkeypatch):
    # The live accuracy gap: a failed-row count windowed on the write/creation
    # (storage) column, no resolved block in the turn. The storage column is
    # not a state's own time, so STATE TIME did not catch it; the
    # business-time-on-storage rule does, naming a state's own time column.
    lake = _Lake()
    sql = f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('created')}"
    (payload,), _answer = _ask(monkeypatch, lake, [sql])
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["reason"] == "business_time_on_storage_column"
    assert payload["storage_columns"] == ["created"]
    assert "fail_time" in payload["guidance"] or "complete_time" in payload["guidance"]
    assert lake.data_queries() == []


# --------------------------------------------------------------------------- #
# The question names the window's own state or column
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("sql", "question"), [
    (_FAILED_ON_COMPLETE, "How many failed rows had completed on 1/5?"),
    (_COMPLETED_ON_START, "What was the median duration of completed rows started on 1/5?"),
    # a state word that is not tied to the window
    (_FAILED_ON_COMPLETE, "How many rows failed on 1/5, and what does completed mean?"),
    (_FAILED_ON_COMPLETE,
     "How many rows failed on 1/5, including rows not subsequently marked as completed?"),
    # under an OR on the state column, with the window's state word in the question
    (("SELECT state, COUNT(*) AS n FROM events WHERE "
      f"(state = 'FAILED' AND {_on('complete_time')}) OR "
      f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state"),
     "How many rows by state were completed on 1/5?"),
    # the column written with a space reads as the state word
    (_FAILED_ON_COMPLETE, "How many failed rows have a complete time on 1/5?"),
])
def test_a_named_state_word_runs_but_is_always_disclosed(monkeypatch, sql, question):
    """A state word in the question can mean the window is the question's own,
    or be incidental to it; words alone cannot tell which. The query runs
    with no refusal, and the answer always says which time the rows were
    windowed on, as facts only. A written column name exempts nothing."""
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=question)
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "state_time_window" in payload
    assert lake.data_queries()
    assert "state time, not asked for" in answer and "FINAL-ANSWER REQUIREMENTS" in answer
    assert "are not counted" in answer
    assert "wrong" not in answer.lower()
    # The question named the state word: the line does not say it did not.
    assert "the question did not name" not in answer
    assert ("[engine] State time windows the question named only by a state word, in this "
            "turn's queries:") in answer


@pytest.mark.parametrize(("sql", "question"), [
    (_GROUPED_ON_COMPLETE, "How many rows by state on 1/5?"),
    (_FAILED_ON_COMPLETE, _QUESTION),
])
def test_the_live_failure_shape_is_still_refused(monkeypatch, sql, question):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question=question)
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "complete_time"


@pytest.mark.parametrize("question", [
    "How many rows failed on 1/5, including rows that were never completed?",
    "How many rows failed on 1/5 that were not completed?",
    "How many rows failed on 1/5 that hadn't completed?",
    "How many rows failed on 1/5 with no completed step?",
    "How many rows failed on 1/5 without complete records?",
    "How many non-completed rows failed on 1/5?",
    "How many un-completed rows failed on 1/5?",
    "How many uncompleted rows failed on 1/5?",
    "How many incomplete rows failed on 1/5?",
])
def test_a_negated_state_word_does_not_exempt_the_live_shape(monkeypatch, question):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_FAILED_ON_COMPLETE], question=question)
    assert payload["reason"] == "window_on_other_state_time"


@pytest.mark.parametrize(("sql", "question"), [
    (_FAILED_ON_COMPLETE, "How many rows failed on 1/5, and what does completed mean?"),
    (_GROUPED_ON_COMPLETE, "How many records by current state were completed on 1/5?"),
])
def test_under_enforce_a_state_word_question_is_refused_once(monkeypatch, sql, question):
    """Enforce mode appends no engine lines, so the disclosed path would be
    silent there: a state-word question is refused the first time, as an
    unnamed one is."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=question)
    assert payload["reason"] == "window_on_other_state_time"
    assert lake.data_queries() == []


def test_a_positive_state_word_beside_a_negated_one_runs_disclosed(monkeypatch):
    question = "How many rows completed on 1/5 had failed, not counting test rows?"
    (payload,), answer = _ask(monkeypatch, _Lake(), [_FAILED_ON_COMPLETE], question=question)
    assert payload["rows"] == [["42"]] and "state_time_window" in payload
    assert "state time, not asked for" in answer


def test_a_written_column_name_after_a_negator_is_refused_then_the_resend_runs(monkeypatch):
    """The question says the rows have no completion time and the query
    windows them on it: the written name no longer exempts it."""
    question = "How many rows failed on 1/5 with no complete_time?"
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_FAILED_ON_COMPLETE, _FAILED_ON_COMPLETE],
                                   question=question)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and "state_time_window" in second
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer


# --------------------------------------------------------------------------- #
# A breakdown that counts the window's own state beside other states
# --------------------------------------------------------------------------- #
_IN_ON_COMPLETE = ("SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', "
                   f"'FAILED') AND {_on('complete_time')} GROUP BY state")
_OTHERS_ON_COMPLETE = ("SELECT state, COUNT(*) AS n FROM events WHERE state IN ('FAILED', "
                       f"'STARTED') AND {_on('complete_time')} GROUP BY state")


@pytest.mark.parametrize(("sql", "question"), [
    (_GROUPED_ON_COMPLETE, "How many records by current state were completed on 1/5?"),
    (_GROUPED_ON_COMPLETE, "Give the completed, failed and started counts for 1/5."),
    (_IN_ON_COMPLETE, "Give the completed and failed counts for 1/5."),
    (_IN_ON_COMPLETE, "Only include completed or terminal rows: how many by state on 1/5?"),
    (_GROUPED_ON_COMPLETE.replace("GROUP BY state", "GROUP BY 1"),
     "How many completed rows by state on 1/5?"),
])
def test_a_state_breakdown_on_one_states_time_is_refused_though_the_state_is_named(
        monkeypatch, sql, question):
    """The state word justifies the window for that state's own rows, not for
    the other states' rows counted in the same result: refused once, with
    each state's own time column."""
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=question)
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "complete_time"
    assert payload["window_state"] == "COMPLETED"
    assert "FAILED rows on fail_time" in payload["guidance"]
    assert "one query per state" in payload["guidance"]
    assert lake.data_queries() == []


def test_the_refused_breakdown_sent_again_runs_and_is_disclosed(monkeypatch):
    lake = _Lake()
    question = "Give the completed and failed counts for 1/5."
    (first, second), answer = _ask(monkeypatch, lake, [_IN_ON_COMPLETE, _IN_ON_COMPLETE],
                                   question=question)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]]
    assert second["state_time_window"]["own_columns"] == {"FAILED": "fail_time"}
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer and "FINAL-ANSWER REQUIREMENTS" in answer


@pytest.mark.parametrize(("sql", "question"), [
    (_FAILED_ON_COMPLETE, "How many of the rows completed on 1/5 had failed?"),
    (_OTHERS_ON_COMPLETE, "Of the rows completed on 1/5, how many are failed or started?"),
])
def test_a_count_of_other_states_only_on_a_named_window_stays_disclosed(
        monkeypatch, sql, question):
    """The cohort reading: the query does not count the window's own state,
    so the state word can name the window, and the query runs disclosed."""
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=question)
    assert payload["rows"] == [["42"]] and "state_time_window" in payload
    assert lake.data_queries()
    assert "state time, not asked for" in answer


def test_a_breakdown_on_a_window_column_the_question_writes_is_refused_once(monkeypatch):
    question = "How many rows by state have a complete_time on 1/5?"
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED_ON_COMPLETE, _GROUPED_ON_COMPLETE],
                                   question=question)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and "state_time_window" in second
    assert "state time, not asked for" in answer


# --------------------------------------------------------------------------- #
# A HAVING on the state column decides which states a grouped query returns
# --------------------------------------------------------------------------- #
_NAMED = "How many completed rows on 1/5?"


@pytest.mark.parametrize("having", [
    "HAVING state = 'COMPLETED'",
    "HAVING state IN ('COMPLETED')",
    "HAVING state = 'COMPLETED' AND COUNT(*) > 0",
])
@pytest.mark.parametrize("question", [_NAMED, "How many rows by state on 1/5?"])
def test_a_having_that_keeps_only_the_windows_own_state_runs(monkeypatch, having, question):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [f"{_GROUPED_ON_COMPLETE} {having}"],
                              question=question)
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert lake.data_queries() and "state time" not in answer


@pytest.mark.parametrize("having", [
    "HAVING state IN ('COMPLETED', 'FAILED')",
    "HAVING COUNT(*) > 0",
])
def test_a_having_that_keeps_other_states_is_a_breakdown_refused_once(monkeypatch, having):
    sql = f"{_GROUPED_ON_COMPLETE} {having}"
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql], question=_NAMED)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and "state_time_window" in second
    assert len(lake.data_queries()) == 1 and "state time, not asked for" in answer


@pytest.mark.parametrize("having", [
    "HAVING state <> 'STARTED'",
    "HAVING state = 'COMPLETED' OR state = 'FAILED'",
    "HAVING COUNT(CASE WHEN state = 'FAILED' THEN 1 END) > 0",
])
def test_a_having_on_the_state_it_cannot_read_is_disclosed_not_refused(monkeypatch, having):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [f"{_GROUPED_ON_COMPLETE} {having}"],
                              question=_NAMED)
    assert payload["rows"] == [["42"]] and "state_time_window" in payload
    assert lake.data_queries() and "state time, not asked for" in answer


def test_an_unread_having_does_not_exempt_an_unnamed_question(monkeypatch):
    sql = f"{_GROUPED_ON_COMPLETE} HAVING state <> 'STARTED'"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql],
                               question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"


def test_the_decision_marks_only_the_mixed_breakdown_refused():
    st = _st()
    question = "How many completed rows on 1/5?"

    def decided(sql, desc=_DESC, columns=_COLUMNS):
        from agents import storage_bound_rule
        bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
        mapping = st.from_tables_desc(desc)["events"]
        return st.decide(sql, "events", bounded, columns, mapping, question=question)

    assert decided(_IN_ON_COMPLETE).refuse is True
    assert decided(_GROUPED_ON_COMPLETE).refuse is True
    assert decided(_OTHERS_ON_COMPLETE).refuse is False
    assert decided(_FAILED_ON_COMPLETE).refuse is False
    unowned = decided(_GROUPED_ON_UPDATED, _UPD_DESC, _UPD_COLUMNS)
    assert unowned.refuse is True
    assert unowned.owners == (("updated_time", ""),)


# --------------------------------------------------------------------------- #
# A GROUP BY no pin narrows, on one state's own time: every other state too
# --------------------------------------------------------------------------- #
_QUEUED_OWN_DESC = _DESC.replace("e.g. COMPLETED, FAILED, STARTED;",
                                 "e.g. COMPLETED, FAILED, STARTED, QUEUED;")
# Each fact as (refused before running, ran): the refusal's wording is
# unchanged; the line for a query that ran names the state's own column and
# the effect.
_FAILED_FACT = ("FAILED rows are windowed on complete_time (the time column of COMPLETED "
                "rows), so FAILED rows with no complete_time would not be counted",
                "FAILED rows were placed in the window by complete_time (the time column of "
                "COMPLETED rows), not by their own fail_time, so FAILED rows whose "
                "complete_time is outside the window or empty are not counted, and FAILED "
                "rows are counted by when they reached the COMPLETED state")
_STARTED_FACT = tuple(f.replace("FAILED", "STARTED").replace("fail_time", "start_time")
                      for f in _FAILED_FACT)
_OTHERS_FACT = ("rows of any other state value are windowed on complete_time (the time "
                "column of COMPLETED rows) too, so those rows with no complete_time would not "
                "be counted",
                "rows of any other state value were placed in the window by complete_time "
                "(the time column of COMPLETED rows) too, so those rows whose complete_time "
                "is outside the window or empty are not counted, and those rows are counted "
                "by when they reached the COMPLETED state")
_OWNED_WHY = ("A row's time column for another state can be empty or outside the window when "
              "its own state happened inside it, so the result may not count the rows the "
              "question means.")
_OWNED_TAIL = ("A window on a state's own time column does not filter by state: rows of other "
               "states can have that time too. If the question asks for that window itself, "
               "send the same query again unchanged: it runs, and the answer says how those "
               "rows were windowed.")
_FAILED_REQUIRED = ("REQUIRED: the answer must say that its FAILED figures place FAILED rows "
                    "in the window by complete_time (the time column of COMPLETED rows), not "
                    "by their own fail_time: FAILED rows whose complete_time is outside the "
                    "window or empty are not counted, and FAILED rows are counted by when they "
                    "reached the COMPLETED state.")
_STARTED_REQUIRED = _FAILED_REQUIRED.replace("FAILED", "STARTED").replace("fail_time",
                                                                          "start_time")
_OTHERS_REQUIRED = ("REQUIRED: the answer must say that its figures for any other state value "
                    "place those rows in the window by complete_time (the time column of "
                    "COMPLETED rows): those rows whose complete_time is outside the window or "
                    "empty are not counted, and those rows are counted by when they reached the COMPLETED state.")
_REQUIRED_RE = re.compile(r"REQUIRED: [^\"]*")


def _owned_texts(facts, own, required):
    refused = "; ".join(refused for refused, _ran in facts)
    ran = "; ".join(ran for _refused, ran in facts)
    return (f"Refused before running: this query counts rows of events by state, and "
            f"{refused}. {_OWNED_WHY} Use one query per state, each filtering state to that "
            f"one state and windowed on that state's own time column ({own}). {_OWNED_TAIL}",
            f"[engine] State time windows the question did not name, in this turn's queries: "
            f"in events (by state), {ran}. Each is a state time, not asked for.",
            required)


def _owned_decided(sql, desc=_DESC):
    from agents import storage_bound_rule
    st = _st()
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    found = st.decide(sql, "events", bounded, _COLUMNS, st.from_tables_desc(desc)["events"],
                      question=_BY_STATE)
    block = st.requirement_block([found])
    return (st.StateTimeWindowError(found).to_dict()["guidance"], st.engine_line([found]),
            _REQUIRED_RE.findall(block))


def test_an_unnarrowed_grouped_count_on_one_states_time_names_every_other_state(monkeypatch):
    """A sampled state with no own time column (or any state the samples did
    not show) comes back as its own group, windowed on the same column: the
    refusal, the engine line and the answer requirements say so."""
    expected = _owned_texts([_FAILED_FACT, _STARTED_FACT, _OTHERS_FACT],
                            "FAILED rows on fail_time; STARTED rows on start_time",
                            [_FAILED_REQUIRED, _STARTED_REQUIRED, _OTHERS_REQUIRED])
    for desc in (_QUEUED_OWN_DESC, _DESC):
        assert _owned_decided(_GROUPED_ON_COMPLETE, desc) == expected
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED_ON_COMPLETE] * 2,
                                   question=_BY_STATE, desc=_QUEUED_OWN_DESC)
    assert first["other_states_counted"] is True
    assert first["states"] == ["FAILED", "STARTED"]
    assert first["guidance"] == expected[0]
    assert second["state_time_window"]["other_states_counted"] is True
    assert expected[1] in answer
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    assert _OTHERS_REQUIRED in block


@pytest.mark.parametrize(("sql", "facts", "own", "required"), [
    (_FAILED_ON_COMPLETE, [_FAILED_FACT], "FAILED rows on fail_time", [_FAILED_REQUIRED]),
    (_IN_ON_COMPLETE, [_FAILED_FACT], "FAILED rows on fail_time", [_FAILED_REQUIRED]),
    (f"{_GROUPED_ON_COMPLETE} HAVING state IN ('COMPLETED', 'FAILED')", [_FAILED_FACT],
     "FAILED rows on fail_time", [_FAILED_REQUIRED]),
    (f"{_GROUPED_ON_COMPLETE} HAVING state <> 'STARTED'", [_FAILED_FACT, _STARTED_FACT],
     "FAILED rows on fail_time; STARTED rows on start_time",
     [_FAILED_REQUIRED, _STARTED_REQUIRED]),
])
def test_every_other_owned_window_text_is_unchanged(sql, facts, own, required):
    """Only a GROUP BY that no WHERE or HAVING pin narrows (and whose HAVING
    is read) names the other states; a pinned, narrowed or unread grouping
    gets no fact about them."""
    assert _owned_decided(sql, _QUEUED_OWN_DESC) == _owned_texts(facts, own, required)
    assert _owned_decided(sql) == _owned_texts(facts, own, required)


_QUEUED_FACT = (_FAILED_FACT[0].replace("FAILED", "QUEUED"),
                "QUEUED rows were placed in the window by complete_time (the time column of "
                "COMPLETED rows), and the engine identified no time column of their own, so "
                "QUEUED rows whose complete_time is outside the window or empty are not "
                "counted, and QUEUED rows are counted by when they reached the COMPLETED state")
_QUEUED_REQUIRED = ("REQUIRED: the answer must say that its QUEUED figures place QUEUED rows "
                    "in the window by complete_time (the time column of COMPLETED rows): "
                    "QUEUED rows whose complete_time is outside the window or empty are not "
                    "counted, and QUEUED rows are counted by when they reached the COMPLETED state.")


@pytest.mark.parametrize("sql", [
    f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('FAILED', 'QUEUED') "
    f"AND {_on('complete_time')} GROUP BY state",
    f"{_GROUPED_ON_COMPLETE} HAVING state IN ('FAILED', 'QUEUED')",
])
def test_a_pinned_state_with_no_own_time_on_one_states_time_is_named_too(sql):
    """A pinned state the name rule gives no own time column is counted on
    the same window as the others: the refusal, the engine line and the
    answer requirements name it, and the remedy says it has no own column."""
    guidance, line, required = _owned_decided(sql, _QUEUED_OWN_DESC)
    refused = "; ".join(refused for refused, _ran in (_FAILED_FACT, _QUEUED_FACT))
    ran = "; ".join(ran for _refused, ran in (_FAILED_FACT, _QUEUED_FACT))
    assert guidance == (
        f"Refused before running: this query counts rows of events by state, and {refused}. "
        f"{_OWNED_WHY} Use one query per state, each filtering state to that one state and "
        "windowed on that state's own time column (FAILED rows on fail_time). The engine "
        f"identified no time column of their own for QUEUED rows. {_OWNED_TAIL}")
    assert line == (f"[engine] State time windows the question did not name, in this turn's "
                    f"queries: in events (by state), {ran}. Each is a state time, not asked for.")
    assert required == [_FAILED_REQUIRED, _QUEUED_REQUIRED]


# --------------------------------------------------------------------------- #
# Several states counted on a time column that no state owns
# --------------------------------------------------------------------------- #
_UPD_COLUMNS = {**_COLUMNS, "updated_time": _TS, "cancel_time": _TS, "q.code": "varchar",
                "q.found_time": _TS, "run_status": "varchar", "run_start_time": _TS}
_UPD_DESC = _DESC.replace("created: timestamp(6); ",
                          f"created: timestamp(6); updated_time: {_TS}; "
                          f"q.code: varchar e.g. c1, c2; q.found_time: {_TS}; ")
_GROUPED_ON_UPDATED = ("SELECT state, COUNT(*) AS n FROM events "
                       f"WHERE {_on('updated_time')} GROUP BY state")
_IN_ON_UPDATED = ("SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', "
                  f"'FAILED') AND {_on('updated_time')} GROUP BY state")
_EVERY_OWN = {"COMPLETED": "complete_time", "FAILED": "fail_time", "STARTED": "start_time"}
_BY_STATE = "How many rows by state on 1/5?"
_FOUND = '"q.found_time"'


_OTHERS_REFUSED = "rows of any other state value are windowed on updated_time too"
_NOT_IDENTIFIED = ("The engine did not identify updated_time as any state's own time column, "
                   "and a row's updated_time can be empty or outside the window when its own "
                   "state was reached inside it, or inside the window when its state was "
                   "reached earlier, so the result may not count the rows the question means.")


@pytest.mark.parametrize(("sql", "own", "others"), [
    (_GROUPED_ON_UPDATED, _EVERY_OWN, True),
    (_GROUPED_ON_UPDATED.replace("GROUP BY state", "GROUP BY 1"), _EVERY_OWN, True),
    (_IN_ON_UPDATED, {"COMPLETED": "complete_time", "FAILED": "fail_time"}, False),
    (f"{_GROUPED_ON_UPDATED} HAVING state IN ('FAILED', 'STARTED')",
     {"FAILED": "fail_time", "STARTED": "start_time"}, False),
])
def test_a_count_of_several_states_on_a_time_no_state_owns_is_refused(monkeypatch, sql, own,
                                                                       others):
    """Every counted state's rows share one time that is none of their own:
    refused once, with each state's own time column in the remedy. A
    grouped count no pin narrows also counts states the schema did not list,
    and the refusal says so."""
    lake = _Lake(_UPD_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_QUESTION, desc=_UPD_DESC)
    assert payload["error"] == "state_time_window"
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "updated_time"
    assert payload["window_state"] is None
    assert payload["own_columns"] == own
    assert payload.get("other_states_counted", False) is others
    guidance = payload["guidance"]
    for state, column in own.items():
        assert (f"{state} rows are windowed on updated_time, not on their own "
                f"{column}") in guidance
        assert f"{state} rows on {column}" in guidance
    assert (_OTHERS_REFUSED in guidance) is others
    assert "no state's own time column" not in guidance
    assert _NOT_IDENTIFIED in guidance
    assert "Rows of a state with no time column of its own can stay on updated_time." in guidance
    assert _KEEP_STATE.format(column="state", own="; ".join(
        f"{s} rows on {c}" for s, c in sorted(own.items()))) in guidance
    assert _NO_FILTER_FACT in guidance
    assert "would not be counted" not in guidance
    assert "\u2014" not in guidance and "\u2013" not in guidance
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in guidance.lower()
    assert classify_error_text(guidance) != "timeout"
    assert lake.data_queries() == []


def test_the_refused_count_on_a_time_no_state_owns_sent_again_runs_disclosed(monkeypatch):
    lake = _Lake(_UPD_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED_ON_UPDATED, _GROUPED_ON_UPDATED],
                                   question=_BY_STATE, desc=_UPD_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]]
    record = second["state_time_window"]
    assert record["window_column"] == "updated_time" and record["window_state"] is None
    assert record["own_columns"] == _EVERY_OWN
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer
    assert record["other_states_counted"] is True
    line = answer[answer.index("[engine] State time"):]
    assert ("COMPLETED rows were windowed on updated_time, not on their own complete_time; "
            "FAILED rows were windowed on updated_time, not on their own fail_time; "
            "STARTED rows were windowed on updated_time, not on their own start_time; "
            "rows of any other state value were windowed on updated_time too.") in line
    assert "no state's own time column" not in answer
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    for state, column in _EVERY_OWN.items():
        assert (f"its {state} figures count {state} rows whose updated_time is in the window, "
                f"not rows whose {column} is") in block
    assert ("the answer must say that its figures for any other state value count rows whose "
            "updated_time is in the window.") in block
    assert answer.index("state time, not asked for") < answer.index(
        "----- BEGIN FINAL-ANSWER REQUIREMENTS -----")
    for claim in ("wrong", "should", "instead", "leaves out"):
        assert claim not in line


_PREFIXED_DESC = (f"events(state: varchar e.g. COMPLETED, FAILED; run_status: varchar e.g. "
                  f"Completed, Started; complete_time: {_TS}; fail_time: {_TS}; "
                  f"run_complete_time: {_TS}; run_start_time: {_TS}; updated_time: {_TS})")


@pytest.mark.parametrize(("sql", "question", "desc"), [
    # one state: a cohort of that state's rows by a time no state owns
    (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_on('updated_time')}",
     _QUESTION, _UPD_DESC),
    (f"SELECT state, COUNT(*) AS n FROM events WHERE state = 'COMPLETED' "
     f"AND {_on('updated_time')} GROUP BY state", _QUESTION, _UPD_DESC),
    (f"{_GROUPED_ON_UPDATED} HAVING state = 'FAILED'", _BY_STATE, _UPD_DESC),
    # the counted states are not certain
    (f"SELECT state, COUNT(*) AS n FROM events WHERE state NOT IN ('STARTED') "
     f"AND {_on('updated_time')} GROUP BY state", _BY_STATE, _UPD_DESC),
    (f"{_GROUPED_ON_UPDATED} HAVING state <> 'STARTED'", _BY_STATE, _UPD_DESC),
    # the WHERE names a counted state's own column
    (f"SELECT state, COUNT(*) AS n FROM events WHERE complete_time IS NOT NULL "
     f"AND {_on('updated_time')} GROUP BY state", _BY_STATE, _UPD_DESC),
    # the window is a state's own time column under the table's other state column
    (f"SELECT run_status, COUNT(*) AS n FROM events WHERE state = 'COMPLETED' "
     f"AND {_on('complete_time')} GROUP BY run_status", _BY_STATE, _PREFIXED_DESC),
    (f"SELECT state, COUNT(*) AS n FROM events WHERE run_status = 'Completed' "
     f"AND {_on('run_complete_time')} GROUP BY state", _BY_STATE, _PREFIXED_DESC),
    # the window is a pinned state's own column by name, a state the samples did not show
    (f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('CANCELLED', 'FAILED') "
     f"AND {_on('cancel_time')} GROUP BY state", _BY_STATE,
     _UPD_DESC.replace("not_started_time", "cancel_time")),
    # a constrained record field whose own sibling time is the window
    (f"SELECT state, COUNT(*) AS n FROM events WHERE \"q.code\" IS NOT NULL "
     f"AND {_on(_FOUND)} GROUP BY state", "How many c1 findings by state on 1/5?", _UPD_DESC),
])
def test_what_the_rule_for_a_time_no_state_owns_leaves_alone_runs(monkeypatch, sql, question,
                                                                   desc):
    lake = _Lake(_UPD_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=question, desc=desc)
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


def test_a_written_window_no_state_owns_is_refused_once_then_runs_disclosed(monkeypatch):
    """Known cost: a question that writes the window column (``by updated
    time``) pays one refusal; the identical resend runs, disclosed. The name
    alone cannot tell a chosen window from a duration's end."""
    question = "How many rows by state by updated time on 1/5?"
    lake = _Lake(_UPD_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED_ON_UPDATED, _GROUPED_ON_UPDATED],
                                   question=question, desc=_UPD_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and "state_time_window" in second
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer


def test_the_same_states_under_an_or_on_a_time_no_state_owns_stay_refused(monkeypatch):
    """The OR reader already refuses this window; the plain reader now agrees."""
    sql = ("SELECT state, COUNT(*) AS n FROM events WHERE "
           f"(state = 'COMPLETED' OR state = 'FAILED') AND {_on('updated_time')} GROUP BY state")
    (payload,), _answer = _ask(monkeypatch, _Lake(_UPD_COLUMNS), [sql], question=_BY_STATE,
                               desc=_UPD_DESC)
    assert payload["reason"] == "window_on_other_state_time" and payload["under_or"] is True
    assert payload["own_columns"] == {"COMPLETED": "complete_time", "FAILED": "fail_time"}


_CANCEL_DESC = _UPD_DESC.replace("not_started_time", "cancel_time")


def test_a_window_the_schema_may_give_an_unlisted_state_is_not_called_no_states_own(
        monkeypatch):
    """The samples list no state that owns cancel_time, but the name rule
    would give it to a state they did not show. The refusal and the
    disclosure state only what the engine read: which column windowed the
    rows, and that it did not identify that column as any state's own."""
    sql = (f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', 'FAILED') "
           f"AND {_on('cancel_time')} GROUP BY state")
    (first, second), answer = _ask(monkeypatch, _Lake(_UPD_COLUMNS), [sql, sql],
                                   question=_BY_STATE, desc=_CANCEL_DESC)
    assert first["reason"] == "window_on_other_state_time"
    guidance = first["guidance"]
    assert "no state's own time column" not in guidance
    assert ("COMPLETED rows are windowed on cancel_time, not on their own complete_time; "
            "FAILED rows are windowed on cancel_time, not on their own fail_time.") in guidance
    assert "The engine did not identify cancel_time as any state's own time column" in guidance
    assert second["rows"] == [["42"]]
    assert "no state's own time column" not in answer
    line = answer[answer.index("[engine] State time"):]
    assert "COMPLETED rows were windowed on cancel_time, not on their own complete_time" in line


_QUEUED_DESC = _UPD_DESC.replace("e.g. COMPLETED, FAILED, STARTED;",
                                 "e.g. COMPLETED, FAILED, STARTED, QUEUED;")


def test_a_counted_state_with_no_time_column_of_its_own_is_still_named(monkeypatch):
    """A pinned state the name rule gives no own time column is counted on
    the shared column too, so the refusal and the answer name it."""
    sql = (f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', "
           f"'QUEUED') AND {_on('updated_time')} GROUP BY state")
    (first, second), answer = _ask(monkeypatch, _Lake(_UPD_COLUMNS), [sql, sql],
                                   question=_BY_STATE, desc=_QUEUED_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert first["states"] == ["COMPLETED", "QUEUED"]
    assert first["own_columns"] == {"COMPLETED": "complete_time"}
    assert "other_states_counted" not in first
    guidance = first["guidance"]
    assert ("QUEUED rows are windowed on updated_time, and the engine identified no time "
            "column of their own") in guidance
    assert "(COMPLETED rows on complete_time)" in guidance
    assert second["rows"] == [["42"]]
    assert ("QUEUED rows were windowed on updated_time, and the engine identified no time "
            "column of their own") in answer
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    assert ("the answer must say that its QUEUED figures count QUEUED rows whose "
            "updated_time is in the window.") in block
    assert "any other state value" not in answer


def test_a_grouped_count_names_states_the_schema_did_not_list(monkeypatch):
    """A sampled state with no own time column (and any state the samples
    did not show) comes back as its own group: the refusal says those rows
    are on the shared column too."""
    (payload,), _answer = _ask(monkeypatch, _Lake(_UPD_COLUMNS), [_GROUPED_ON_UPDATED],
                               question=_BY_STATE, desc=_QUEUED_DESC)
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["states"] == ["COMPLETED", "FAILED", "STARTED"]
    assert payload["other_states_counted"] is True
    assert _OTHERS_REFUSED in payload["guidance"]


_OWNLESS_DESC = _UPD_DESC.replace("e.g. COMPLETED, FAILED, STARTED;",
                                  "e.g. COMPLETED, FAILED, QUEUED, PENDING;")
_OWNLESS_PAIR = (f"SELECT state, COUNT(*) FROM events WHERE state IN ('QUEUED', 'PENDING') "
                 f"AND updated_time BETWEEN {_DAY} AND {_NEXT} GROUP BY state")


@pytest.mark.parametrize("sql", [
    _OWNLESS_PAIR,
    _OWNLESS_PAIR.replace("state IN ('QUEUED', 'PENDING') AND ", "").replace(
        "GROUP BY state", "GROUP BY state HAVING state IN ('QUEUED', 'PENDING')"),
])
def test_a_count_of_states_with_no_time_column_of_their_own_runs_disclosed(monkeypatch, sql):
    """Every counted state lacks an own time column: no other query would
    count them better, so the count runs the first time and the answer says
    how those rows were windowed (a refusal would only cost a resend)."""
    lake = _Lake(_UPD_COLUMNS)
    (first,), answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_OWNLESS_DESC)
    assert "error" not in first, first
    assert first["rows"] == [["42"]] and len(lake.data_queries()) == 1
    record = first["state_time_window"]
    assert record["window_column"] == "updated_time" and record["window_state"] is None
    assert record["states"] == ["PENDING", "QUEUED"] and record["own_columns"] == {}
    line = answer[answer.index("[engine] State time"):]
    assert ("PENDING rows were windowed on updated_time, and the engine identified no time "
            "column of their own; QUEUED rows were windowed on updated_time, and the engine "
            "identified no time column of their own.") in line
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    for state in ("PENDING", "QUEUED"):
        assert (f"the answer must say that its {state} figures count {state} rows whose "
                "updated_time is in the window.") in block


@pytest.mark.parametrize("sql", [
    _OWNLESS_PAIR,
    _OWNLESS_PAIR.replace("state IN ('QUEUED', 'PENDING') AND ", "").replace(
        "GROUP BY state", "GROUP BY state HAVING state IN ('QUEUED', 'PENDING')"),
])
def test_under_enforce_a_count_of_states_with_no_time_column_of_their_own_is_refused_once(
        monkeypatch, sql):
    """Enforce mode appends no engine lines, so that count is refused once
    there. The remedy names no own column; it says to send the query again
    unchanged."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake(_UPD_COLUMNS)
    (first,), _answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_OWNLESS_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert first["window_column"] == "updated_time" and first["window_state"] is None
    assert first["states"] == ["PENDING", "QUEUED"]
    assert first["own_columns"] == {}
    assert "other_states_counted" not in first
    guidance = first["guidance"]
    for state in ("PENDING", "QUEUED"):
        assert (f"{state} rows are windowed on updated_time, and the engine identified no "
                "time column of their own") in guidance
    assert _NOT_IDENTIFIED in guidance
    assert "own time column (" not in guidance and "each state's own time column" not in guidance
    assert "Use one query per state" not in guidance
    assert ("The engine names no other time column for these states. Send the same query "
            "again unchanged: it runs, and the answer says how those rows were windowed.") \
        in guidance
    assert "\u2014" not in guidance and "\u2013" not in guidance
    assert classify_error_text(guidance) != "timeout"
    assert lake.data_queries() == []


def test_one_state_with_no_time_column_of_its_own_stays_a_cohort(monkeypatch):
    sql = (f"SELECT COUNT(*) AS n FROM events WHERE state = 'QUEUED' "
           f"AND updated_time BETWEEN {_DAY} AND {_NEXT}")
    lake = _Lake(_UPD_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_OWNLESS_DESC)
    assert "error" not in payload and "state_time_window" not in payload
    assert "state time, not asked for" not in answer


# --------------------------------------------------------------------------- #
# The counting block: a transparent wrapper, and what is not a count
# --------------------------------------------------------------------------- #
_INNER_UPDATED = f"SELECT * FROM events WHERE {_on('updated_time')}"
_CTE_ON_UPDATED = (f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) AS n FROM w "
                   "GROUP BY state")
_DERIVED_ON_UPDATED = (f"SELECT state, COUNT(*) AS n FROM ({_INNER_UPDATED}) AS w "
                       "GROUP BY state")
_PAIR = {"COMPLETED": "complete_time", "FAILED": "fail_time"}


@pytest.mark.parametrize(("sql", "window", "window_state", "own"), [
    (_CTE_ON_UPDATED, "updated_time", None, _EVERY_OWN),
    (_DERIVED_ON_UPDATED, "updated_time", None, _EVERY_OWN),
    (_DERIVED_ON_UPDATED.replace(") AS w", ") w"), "updated_time", None, _EVERY_OWN),
    (f"{_CTE_ON_UPDATED} HAVING state IN ('FAILED', 'STARTED') ORDER BY state LIMIT 10",
     "updated_time", None, {"FAILED": "fail_time", "STARTED": "start_time"}),
    # a plain column projection, a distinct count, a GROUP BY ordinal
    (f"WITH w AS (SELECT id, state, updated_time FROM events WHERE {_on('updated_time')}) "
     "SELECT state, COUNT(DISTINCT id) AS n FROM w GROUP BY 1", "updated_time", None,
     _EVERY_OWN),
    # the states pinned inside the wrapper, counted outside it
    (f"SELECT COUNT(id) AS n FROM (SELECT * FROM events WHERE state IN ('COMPLETED', "
     f"'FAILED') AND {_on('updated_time')}) w", "updated_time", None, _PAIR),
    # a wrapper on another state's own time is read the same way
    (f"WITH w AS (SELECT * FROM events WHERE {_on('complete_time')}) "
     "SELECT state, COUNT(*) AS n FROM w GROUP BY state", "complete_time", "COMPLETED",
     {"FAILED": "fail_time", "STARTED": "start_time"}),
])
def test_a_count_over_a_wrapper_that_only_filters_the_table_is_read(monkeypatch, sql, window,
                                                                    window_state, own):
    """A CTE or derived table that only selects and filters the table's rows
    is the same count: its outer GROUP BY, HAVING and COUNT are read."""
    lake = _Lake(_UPD_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert payload.get("reason") == "window_on_other_state_time", payload
    assert payload["window_column"] == window and payload["window_state"] == window_state
    assert payload["own_columns"] == own
    assert lake.data_queries() == []


@pytest.mark.parametrize("sql", [
    # a wrapper that renames a column
    f"WITH w AS (SELECT line AS state FROM events WHERE {_on('updated_time')}) "
    "SELECT state, COUNT(*) AS n FROM w GROUP BY state",
    # a wrapper that de-duplicates
    f"SELECT state, COUNT(*) AS n FROM (SELECT DISTINCT id, state FROM events "
    f"WHERE {_on('updated_time')}) w GROUP BY state",
])
def test_a_wrapper_that_reshapes_the_rows_is_not_read_through(monkeypatch, sql):
    # A SINGLE-relation wrapper (rename, de-dup) is not traced through, but it
    # is one scan, so it runs. A wrapper JOIN is a different case (below).
    lake = _Lake(_UPD_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert "error" not in payload, payload
    assert "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


def test_a_cte_joined_to_itself_over_the_state_time_table_is_refused(monkeypatch):
    # a CTE self-join combines two relation-producing inputs over
    # the state-time rule table; it can no longer run unguarded on Athena. It
    # fails closed, naming the table, like a direct base-table self-join.
    sql = (f"WITH w AS ({_INNER_UPDATED}) SELECT a.state, COUNT(*) AS n FROM w a JOIN w b "
           "ON a.id = b.id GROUP BY a.state")
    lake = _Lake(_UPD_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert payload["error_name"] == "MULTI_RELATION_RULE_READ"
    assert "events" in payload.get("tables", [])
    assert lake.data_queries() == []


_INNER_COMPLETE = f"SELECT * FROM events WHERE {_on('complete_time')}"
_ON_COMPLETE_FAILED = ("complete_time", "COMPLETED", {"FAILED": "fail_time"})


@pytest.mark.parametrize(("sql", "read"), [
    # the states pinned outside the wrapper, by IN, on a time no state owns
    (f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) FROM w "
     "WHERE state IN ('COMPLETED', 'FAILED') GROUP BY state", ("updated_time", None, _PAIR)),
    (f"SELECT state, COUNT(*) AS n FROM ({_INNER_UPDATED}) AS w "
     "WHERE state IN ('COMPLETED', 'FAILED') GROUP BY state", ("updated_time", None, _PAIR)),
    # one state pinned outside by =, on another state's own time
    (f"WITH w AS ({_INNER_COMPLETE}) SELECT COUNT(*) AS n FROM w WHERE state = 'FAILED'",
     _ON_COMPLETE_FAILED),
    (f"SELECT COUNT(*) AS n FROM ({_INNER_COMPLETE}) w WHERE w.state = 'FAILED'",
     _ON_COMPLETE_FAILED),
    # two outer pins, ANDed and parenthesised
    (f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) AS n FROM w "
     "WHERE (state IN ('COMPLETED', 'FAILED', 'STARTED') AND state IN ('COMPLETED', 'FAILED')) "
     "GROUP BY state", ("updated_time", None, _PAIR)),
    # the state pins beside a filter on another column, which only narrows the rows
    (f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) AS n FROM w "
     "WHERE state IN ('COMPLETED', 'FAILED') AND line = 'l1' GROUP BY state",
     ("updated_time", None, _PAIR)),
    (f"SELECT state, COUNT(*) AS n FROM ({_INNER_UPDATED}) w "
     "WHERE (line = 'l1' OR line = 'l2') AND w.state IN ('COMPLETED', 'FAILED') "
     "GROUP BY state", ("updated_time", None, _PAIR)),
])
def test_a_wrapper_filtered_again_outside_by_state_pins_is_read(monkeypatch, sql, read):
    """A filter outside a wrapper that only filters the table is read when
    it is only ``=`` or ``IN`` pins of the state column: the pins narrow the
    counted states as if they were inside."""
    window, window_state, own = read
    lake = _Lake(_UPD_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert payload.get("reason") == "window_on_other_state_time", payload
    assert payload["window_column"] == window and payload["window_state"] == window_state
    assert payload["own_columns"] == own
    assert "other_states_counted" not in payload
    assert lake.data_queries() == []


@pytest.mark.parametrize("sql", [
    # a filter the reader does not read: an OR, a negation
    f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) AS n FROM w "
    "WHERE state IN ('COMPLETED', 'FAILED') OR line = 'l1' GROUP BY state",
    f"SELECT state, COUNT(*) AS n FROM ({_INNER_UPDATED}) w "
    "WHERE state NOT IN ('STARTED') GROUP BY state",
    # a computed state value
    f"WITH w AS ({_INNER_UPDATED}) SELECT state, COUNT(*) AS n FROM w "
    "WHERE lower(state) IN ('completed', 'failed') GROUP BY state",
    f"SELECT state, COUNT(*) AS n FROM ({_INNER_UPDATED}) AS w "
    "WHERE upper(state) = 'FAILED' OR upper(state) = 'COMPLETED' GROUP BY state",
    # the state column renamed inside the wrapper
    f"WITH w AS (SELECT id, state AS status FROM events WHERE {_on('updated_time')}) "
    "SELECT status, COUNT(*) AS n FROM w WHERE status IN ('COMPLETED', 'FAILED') "
    "GROUP BY status",
    # one state pinned outside: a cohort of that state's rows
    f"WITH w AS ({_INNER_UPDATED}) SELECT COUNT(*) AS n FROM w WHERE state = 'FAILED'",
    # a proven per-state OR inside, the states pinned again outside
    "WITH w AS (SELECT * FROM events WHERE "
    f"(state = 'COMPLETED' AND {_on('complete_time')}) OR "
    f"(state = 'FAILED' AND {_on('fail_time')})) SELECT state, COUNT(*) AS n FROM w "
    "WHERE state IN ('COMPLETED', 'FAILED') GROUP BY state",
])
def test_an_outer_filter_the_reader_does_not_read_keeps_the_inner_reading(monkeypatch, sql):
    lake = _Lake(_UPD_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert "error" not in payload, payload
    assert "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


@pytest.mark.parametrize("sql", [
    # rows, not a count
    f"SELECT * FROM events WHERE state IN ('COMPLETED', 'FAILED') AND {_on('updated_time')}",
    # the states listed, not counted
    f"SELECT state FROM events WHERE {_on('updated_time')} GROUP BY state",
    # another aggregate
    f"SELECT state, MAX(updated_time) AS last_seen FROM events WHERE {_on('updated_time')} "
    "GROUP BY state",
    # a wrapper that aggregates otherwise, its groups counted outside
    f"WITH w AS (SELECT state, MAX(updated_time) AS t FROM events WHERE {_on('updated_time')} "
    "GROUP BY state) SELECT state, COUNT(*) AS n FROM w GROUP BY state",
    # a window count over the result groups: the number of states returned
    f"SELECT state, COUNT(*) OVER () AS number_of_states FROM events "
    f"WHERE {_on('updated_time')} GROUP BY state",
    f"SELECT state, line, COUNT(*) OVER (PARTITION BY line) AS states_per_line FROM events "
    f"WHERE {_on('updated_time')} GROUP BY state, line",
    # a filtered window count over the result groups
    f"SELECT state, COUNT(*) FILTER (WHERE state <> 'CANCELLED') OVER () AS kept_states "
    f"FROM events WHERE {_on('updated_time')} GROUP BY state",
])
def test_what_does_not_count_the_tables_rows_on_a_time_no_state_owns_runs(monkeypatch, sql):
    """The rule on a time no state was identified to own is about counts:
    rows retrieved, states listed or another aggregate run as before."""
    lake = _Lake(_UPD_COLUMNS)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert "error" not in payload, payload
    assert "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


@pytest.mark.parametrize("item", [
    "COUNT(*) AS n",
    "COUNT(id) n",
    "COUNT(DISTINCT id) AS n",
    "100.0 * COUNT(*) / 7 AS share",
    "COUNT(*) AS n, COUNT(*) OVER () AS number_of_states",
    "COUNT(*) FILTER (WHERE line = 'l1') AS n",
])
def test_a_row_count_in_any_supported_form_is_read_as_a_count(monkeypatch, item):
    sql = f"SELECT state, {item} FROM events WHERE {_on('updated_time')} GROUP BY state"
    lake = _Lake(_UPD_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question=_BY_STATE, desc=_UPD_DESC)
    assert payload.get("reason") == "window_on_other_state_time", payload
    assert lake.data_queries() == []


# --------------------------------------------------------------------------- #
# The SQL that runs, after the scan guard's own partition predicate
# --------------------------------------------------------------------------- #
_RECENT = "\"complete_time\" IN (TIMESTAMP '2026-01-05 00:00:00 UTC')"
_UNWINDOWED = "SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED'"


def _auto_scoped_on_complete(monkeypatch):
    """The scan guard knows ``complete_time`` as the table's partition column
    and adds a recent-partition predicate on it to an unscoped query."""
    monkeypatch.setattr(at, "list_partition_columns",
                        lambda *_a, **_k: [{"name": "complete_time", "data_type": _TS}])
    monkeypatch.setattr(at, "build_recent_partition_predicate", lambda *_a, **_k: _RECENT)


def test_an_auto_scoped_window_on_another_states_time_is_refused(monkeypatch):
    _auto_scoped_on_complete(monkeypatch)
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_UNWINDOWED])
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "complete_time"
    assert lake.data_queries() == []


def test_the_auto_scoped_repeat_runs_and_is_disclosed(monkeypatch):
    _auto_scoped_on_complete(monkeypatch)
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_UNWINDOWED, _UNWINDOWED])
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]]
    assert second["state_time_window"]["window_column"] == "complete_time"
    assert "complete_time" in lake.data_queries()[0]
    assert "state time, not asked for" in answer


# --------------------------------------------------------------------------- #
# The state column under an OR
# --------------------------------------------------------------------------- #
_WRONG_OR = ("SELECT state, COUNT(*) AS n FROM events WHERE "
             f"(state = 'FAILED' AND {_on('complete_time')}) OR "
             f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state")
_OUTER_WINDOW_OR = ("SELECT state, COUNT(*) AS n FROM events WHERE "
                    f"{_on('complete_time')} AND (state = 'FAILED' OR state = 'STARTED') "
                    "GROUP BY state")


@pytest.mark.parametrize("sql", [
    _WRONG_OR,
    _OUTER_WINDOW_OR,
    # one branch is right, the other is not
    ("SELECT state, COUNT(*) AS n FROM events WHERE "
     f"(state = 'FAILED' AND {_on('fail_time')}) OR "
     f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state"),
    # a branch the reader cannot read
    ("SELECT COUNT(*) AS n FROM events WHERE "
     f"(state = 'FAILED' AND date(fail_time) = DATE '2026-01-05') OR "
     f"(state = 'COMPLETED' AND {_on('complete_time')})"),
])
def test_a_state_bearing_or_it_cannot_prove_is_refused(monkeypatch, sql):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [sql], question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["under_or"] is True
    assert "one query per state" in payload["guidance"]
    assert lake.data_queries() == []


def test_the_wrong_or_names_each_states_window(monkeypatch):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_WRONG_OR],
                               question="How many rows by state on 1/5?")
    assert payload["windows"] == {"FAILED": ["complete_time"], "STARTED": ["complete_time"]}
    assert payload["own_columns"] == {"FAILED": "fail_time", "STARTED": "start_time"}


def test_a_refused_or_repeated_runs_and_is_disclosed(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_WRONG_OR, _WRONG_OR],
                                   question="How many rows by state on 1/5?")
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and second["state_time_window"]["under_or"] is True
    assert "STARTED rows were placed in the window by complete_time" in answer
    assert "state time, not asked for" in answer and "FINAL-ANSWER REQUIREMENTS" in answer


def test_a_proven_or_with_a_null_test_on_another_time_column_runs(monkeypatch):
    """``complete_time IS NULL`` filters rows; it is not a window, so the
    FAILED branch's own-time bound still proves it."""
    sql = ("SELECT state, COUNT(*) AS n FROM events WHERE "
           f"(state = 'FAILED' AND {_on('fail_time')} AND complete_time IS NULL) OR "
           f"(state = 'COMPLETED' AND {_on('complete_time')}) GROUP BY state")
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql], question="How many rows by state on 1/5?")
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert "state time" not in answer


def test_a_not_null_test_outside_the_or_keeps_it_proven(monkeypatch):
    sql = ("SELECT state, COUNT(*) AS n FROM events WHERE start_time IS NOT NULL AND ("
           f"(state = 'FAILED' AND {_on('fail_time')}) OR "
           f"(state = 'COMPLETED' AND {_on('complete_time')})) GROUP BY state")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question="How many rows by state on 1/5?")
    assert payload["rows"] == [["42"]]


def test_a_branch_that_bounds_a_second_time_column_in_a_range_is_still_refused(monkeypatch):
    sql = ("SELECT state, COUNT(*) AS n FROM events WHERE "
           f"(state = 'FAILED' AND {_on('fail_time')} AND complete_time >= {_DAY}) OR "
           f"(state = 'COMPLETED' AND {_on('complete_time')}) GROUP BY state")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question="How many rows by state on 1/5?")
    assert payload["reason"] == "window_on_other_state_time" and payload["under_or"] is True
    assert payload["windows"]["FAILED"] == ["complete_time", "fail_time"]


def test_an_or_that_does_not_involve_the_state_column_keeps_the_old_verdict(monkeypatch):
    sql = f"{_FAILED_ON_COMPLETE} AND (line = 'l1' OR line = 'l2')"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["reason"] == "window_on_other_state_time" and payload["under_or"] is False


def test_the_connector_alone_never_applies_the_rule():
    sa._remember_turn_time_facts("system-a", [], _DESC)
    result = at.run_athena_query_exact(_FAILED_ON_COMPLETE, _CFG, source_system="system-a",
                                       client=_Lake())
    assert result["rows"] == [["42"]]


# --------------------------------------------------------------------------- #
# The mapping, from the schema only
# --------------------------------------------------------------------------- #
def test_the_mapping_comes_from_the_state_samples_and_the_time_column_names():
    mapping = _st().from_tables_desc(_DESC)["events"][0]
    assert mapping.state_column == "state"
    assert mapping.time_of("FAILED") == "fail_time"
    assert mapping.time_of("COMPLETED") == "complete_time"   # never run_complete_time
    assert mapping.time_of("STARTED") == "start_time"   # never not_started_time
    assert mapping.state_of("complete_time") == "COMPLETED"
    assert mapping.state_of("created") is None


def test_a_doubled_consonant_and_a_value_outside_the_samples_map_by_the_same_rule():
    desc = _DESC.replace("not_started_time", "cancel_time")
    mapping = _st().from_tables_desc(desc)["events"][0]
    assert mapping.time_of("CANCELLED") == "cancel_time"
    assert mapping.time_of("CANCELED") == "cancel_time"
    assert mapping.time_of("QUEUED") is None


def test_an_ambiguous_time_column_is_left_out():
    desc = _DESC.replace("created: timestamp(6)", f"start_plan_time: {_TS}")
    mapping = _st().from_tables_desc(desc)["events"][0]
    assert mapping.time_of("STARTED") is None
    assert mapping.time_of("FAILED") == "fail_time"


@pytest.mark.parametrize("desc", [
    # one state with its own column is not a lifecycle column
    f"events(state: varchar e.g. FAILED, PASSED; fail_time: {_TS})",
    # a non-temporal column named for a state is not a time column
    "events(state: varchar e.g. FAILED, COMPLETED; fail_reason: varchar; complete_note: varchar)",
    # two state columns claim the same time columns
    (f"events(state: varchar e.g. FAILED, COMPLETED; status: varchar e.g. FAILED, COMPLETED; "
     f"fail_time: {_TS}; complete_time: {_TS})"),
    # no samples at all
    f"events(state: varchar, fail_time: {_TS}, complete_time: {_TS})",
])
def test_no_mapping_where_the_schema_is_not_unambiguous(desc):
    assert _st().from_tables_desc(desc) == {}


def test_a_prefixed_status_column_keeps_its_own_prefixed_time_columns():
    """A record's state and a sub-record's status: each maps to the time
    columns under its own name prefix, never to the other's."""
    desc = (f"events(state: varchar e.g. COMPLETED, FAILED; run_status: varchar e.g. Completed, "
            f"Started; complete_time: {_TS}; fail_time: {_TS}; run_complete_time: {_TS}; "
            f"run_start_time: {_TS}; run_canceled_time: {_TS})")
    state, run = _st().from_tables_desc(desc)["events"]
    assert (state.state_column, state.time_of("COMPLETED")) == ("state", "complete_time")
    assert (run.state_column, run.time_of("Completed")) == ("run_status", "run_complete_time")
    assert run.time_of("Canceled") == "run_canceled_time" and state.time_of("CANCELED") is None


def test_a_prefixed_status_pinned_on_its_sibling_time_column_is_refused(monkeypatch):
    desc = (f"events(state: varchar e.g. COMPLETED, FAILED; run_status: varchar e.g. Completed, "
            f"Started; complete_time: {_TS}; fail_time: {_TS}; run_complete_time: {_TS}; "
            f"run_start_time: {_TS})")
    columns = {**_COLUMNS, "run_status": "varchar", "run_complete_time": _TS,
               "run_start_time": _TS}
    sql = (f"SELECT COUNT(*) AS n FROM events WHERE run_status = 'Completed' "
           f"AND {_on('run_start_time')}")
    (payload,), _answer = _ask(monkeypatch, _Lake(columns), [sql], desc=desc,
                               question="How many runs finished on 1/5?")
    assert payload["state_column"] == "run_status"
    assert payload["own_columns"] == {"Completed": "run_complete_time"}


def test_the_orchestrator_applies_the_requirement_block_after_the_state_time_line():
    prompt = orch.ORCHESTRATOR_PROMPT
    rule = prompt[prompt.index("EXECUTED FILTERS:"):]
    rule = " ".join(rule[:rule.index("\n\n") if "\n\n" in rule else len(rule)].split())
    assert "state time, not asked for" in rule
    assert "storage bound, not asked for" in rule and "FINAL-ANSWER REQUIREMENTS block" in rule


def test_the_state_time_prompt_is_kept():
    assert "STATE TIME (required)" in sa._state_time_prompt("Athena")
