# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A resolved time window applied only to a storage column is refused.

Measured on an analytics deployment: the turn carried a RESOLVED TIME WINDOWS
block for one day, and the SQL system agent windowed every query only on the
Iceberg table's partition column (a record-creation time), with no bound on
any event time. It counted rows created that day, not that day's events.

The connector cannot see the question, so the system agent's guarded Athena
tool applies ``storage_bound_rule.decide_window`` before the query runs, in a
Mode A call whose delegation carries the block: on an Iceberg table, a query
whose only bounded date or timestamp column is a storage column is refused
with a remedy, unless the question names that column, the table has no other
time column that is not a storage source, or such a column appears in the
WHERE clause. The same query sent again in that call runs (the storage column
can be the event's own time, an Iceberg ``day()`` partition of it), and its
result's ``storage_bound`` record then puts the bound in the answer's engine
lines. Not refused either: a turn with no resolved windows, a non-Iceberg
table, a non-temporal storage column.

Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import storage_bound_rule, storage_bounds, time_windows
from agents.chat_client import classify_error_text
from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS

from .test_storage_bound_disclosure import _mode_a, _state_after
from .test_storage_bound_refusal import _CFG, _WINDOW, _Lake

_ASKED_CREATED = "How many rows were created on 1/5?"
_QUESTION = "How many rows were done on 1/5?"
_KG = (f"Routing note for this question.\n\n{time_windows.HEADER}\n"
       "The engine resolved the date expressions in the user's question with this "
       "deployment's calendar rules, before this turn:\n"
       "- \"1/5\" is a date with no time, read as the UTC calendar day: 2026-01-05 = "
       "[2026-01-05 00:00:00 UTC, 2026-01-06 00:00:00 UTC).\n"
       "Use exactly these windows for these dates and periods; do not re-derive them.")
_STORAGE_ONLY = ("SELECT state, COUNT(*) AS n FROM events WHERE created >= "
                 "TIMESTAMP '2026-01-05 00:00:00' AND created < TIMESTAMP '2026-01-06 00:00:00' "
                 "GROUP BY state")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()


def _run(lake, sql):
    return at.run_athena_query_exact(sql, _CFG, source_system="system-a", client=lake)


# --------------------------------------------------------------------------- #
# The guarded tool inside a Mode A call
# --------------------------------------------------------------------------- #
def _ask(monkeypatch, lake, sqls, *, kg_context=_KG, question=_QUESTION):
    """Run one Mode A call whose agent sends ``sqls`` to the guarded Athena
    tool the way strands does (its worker thread, then ``asyncio.to_thread``
    for a sync tool), and return each tool payload."""
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "Answer.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    asyncio.run(sa._run_mode_a_query(object(), "system-a", question, kg_context))
    return payloads


def test_a_window_only_on_a_storage_column_is_refused_before_it_runs(monkeypatch):
    lake = _Lake()
    payload, = _ask(monkeypatch, lake, [_STORAGE_ONLY])
    assert payload["error"] == "storage_bound"
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == storage_bound_rule.REASON_WINDOW_ON_STORAGE
    assert payload["retryable"] is True
    assert payload["storage_columns"] == ["created"]
    guidance = payload["guidance"]
    assert "time column that records the state being counted" in guidance
    assert "each state on its own time" in guidance
    assert "no partition bound is needed" in guidance
    assert lake.data_queries() == []


def test_the_remedy_reads_as_a_fixable_query_not_an_outage(monkeypatch):
    payload, = _ask(monkeypatch, _Lake(), [_STORAGE_ONLY])
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]
    # It names no other column: the event column is for the agent to choose.
    assert "done_at" not in payload["guidance"]


def test_the_same_query_sent_again_runs_and_is_disclosed(monkeypatch):
    """The storage column can be the event's own time (an Iceberg ``day()``
    partition of it), so a deliberate repeat runs, labelled for disclosure."""
    lake = _Lake()
    first, second = _ask(monkeypatch, lake, [_STORAGE_ONLY, f"  {_STORAGE_ONLY}\n"])
    assert first["error_name"] == "STORAGE_BOUND"
    assert second["rows"] == [["42"]]
    assert second["storage_bound"]["columns"] == ["created"]
    assert len(lake.data_queries()) == 1
    answer = _mode_a("42 rows are done.", _state_after(second), question=_QUESTION)
    assert "storage bound, not asked for" in answer and "FINAL-ANSWER REQUIREMENTS" in answer


def test_a_different_storage_only_query_is_refused_too(monkeypatch):
    other = _STORAGE_ONLY.replace("GROUP BY state", "AND state = 'failed' GROUP BY state")
    first, second = _ask(monkeypatch, _Lake(), [_STORAGE_ONLY, other])
    assert first["error_name"] == second["error_name"] == "STORAGE_BOUND"


def test_the_refusal_holds_in_a_call_that_ran_another_query_first(monkeypatch):
    lake = _Lake()
    window = f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW}"
    ran, refused = _ask(monkeypatch, lake, [window, _STORAGE_ONLY])
    assert ran["rows"] == [["42"]] and refused["error_name"] == "STORAGE_BOUND"


@pytest.mark.parametrize(("lake", "sql", "question", "kg_context"), [
    # the event column carries the window
    (_Lake(), f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW}", _QUESTION, _KG),
    # the question names the storage column
    (_Lake(), _STORAGE_ONLY, _ASKED_CREATED, _KG),
    # no resolved windows in this turn
    (_Lake(), _STORAGE_ONLY, _QUESTION, "Routing note for this question."),
    (_Lake(), _STORAGE_ONLY, _QUESTION, ""),
    # a table that is not Iceberg (a Hive partition key the scan guard requires)
    (_Lake(iceberg=False, keys=("created",)), _STORAGE_ONLY, _QUESTION, _KG),
    # a non-temporal storage column
    (_Lake(fields=("region",)), "SELECT COUNT(*) AS n FROM events WHERE region = 'r1'",
     _QUESTION, _KG),
    # every time column of the table is a storage source: nothing else to window on
    (_Lake(fields=("created_day", "done_at_day")), _STORAGE_ONLY, _QUESTION, _KG),
    # another time column appears in the WHERE clause in a form the rule does not read
    (_Lake(), _STORAGE_ONLY.replace("GROUP BY", "AND date(done_at) = DATE '2026-01-05' GROUP BY"),
     _QUESTION, _KG),
])
def test_what_is_not_refused_runs(monkeypatch, lake, sql, question, kg_context):
    payload, = _ask(monkeypatch, lake, [sql], kg_context=kg_context, question=question)
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]]
    assert lake.data_queries()


def test_the_competing_bound_refusal_is_unchanged_in_a_windowed_turn(monkeypatch):
    both = (f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW} "
            "AND created >= DATE '2026-01-01'")
    payload, = _ask(monkeypatch, _Lake(), [both])
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["reason"] == storage_bound_rule.REASON_COMPETING
    assert payload["time_columns"] == ["done_at"]
    assert "Remove the bound on created" in payload["guidance"]


def test_the_connector_alone_never_applies_the_window_rule():
    """Outside a system agent call (no question, no windows) the connector
    runs a storage-only bound and labels it, as before."""
    result = _run(_Lake(), _STORAGE_ONLY)
    assert result["rows"] == [["42"]] and result["storage_bound"]["columns"] == ["created"]


# --------------------------------------------------------------------------- #
# The pure decision and the block test
# --------------------------------------------------------------------------- #
_TYPES = {"created": "timestamp(6)", "done_at": "timestamp(6)", "state": "varchar"}


def _decide(sql, *, storage=("created",), types=_TYPES, iceberg=True, question=_QUESTION):
    table, bounded = storage_bound_rule.bounded_columns(sql)
    return storage_bound_rule.decide_window(
        ".".join(table), bounded, storage, types, iceberg=iceberg, sql=sql, question=question)


def test_decide_window_refuses_only_the_storage_only_window():
    with pytest.raises(storage_bound_rule.StorageBoundError) as info:
        _decide(_STORAGE_ONLY)
    assert info.value.reason == storage_bound_rule.REASON_WINDOW_ON_STORAGE
    assert info.value.refused == ("created",)
    assert _decide(f"SELECT COUNT(*) FROM events WHERE {_WINDOW}") is None
    assert _decide(_STORAGE_ONLY, iceberg=False) is None
    assert _decide(_STORAGE_ONLY, question=_ASKED_CREATED) is None
    assert _decide(_STORAGE_ONLY, storage=("created", "done_at")) is None
    assert _decide(_STORAGE_ONLY, types={**_TYPES, "created": "varchar"}) is None
    assert _decide("SELECT COUNT(*) FROM events WHERE state = 'done'") is None


def test_a_typed_literal_or_function_word_is_not_a_mention_of_a_column():
    """A time column named like a type or function word is mentioned only by
    a reference to it, not by ``DATE '...'`` or ``date(...)``."""
    types = {"created": "timestamp(6)", "date": "date", "state": "varchar"}
    literal = ("SELECT COUNT(*) FROM events WHERE created >= DATE '2026-01-05' "
               "AND created < DATE '2026-01-06'")
    with pytest.raises(storage_bound_rule.StorageBoundError):
        _decide(literal, types=types)
    names = storage_bound_rule.where_names(literal)
    assert "created" in names and "date" not in names
    assert _decide(f"{literal} AND \"date\" IS NOT NULL", types=types) is None
    assert storage_bound_rule.where_names(
        "SELECT 1 FROM a JOIN b ON a.id = b.id WHERE a.x = 1") is None


def test_the_block_test_reads_the_header_at_a_line_start_only():
    assert time_windows.carries_block(_KG)
    assert time_windows.carries_block(time_windows.NO_ROOM_NOTE)
    assert not time_windows.carries_block("Routing note.")
    assert not time_windows.carries_block(f"see {time_windows.HEADER} later")
    assert not time_windows.carries_block(None)
