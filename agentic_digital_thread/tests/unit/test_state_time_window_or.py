# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A date window written as an OR of two ranges on ONE column is read.

Measured on an analytics deployment, in repeated runs: a request to compare two
days wrote the window as an OR of two ranges on one time column
(``(done_at >= d1 AND done_at < d1n) OR (done_at >= d2 AND done_at < d2n)``),
and the state-time check stayed silent, because the bounds reader skipped every
top-level AND conjunct that contained an OR. The same query with ONE range on
that column is refused today. So the window rules now read a same-column OR of
ranges as a bound on that column; the storage consumers are unchanged.

Neutral names (the domain-neutrality ratchet): a table with ``status``,
``done_at`` and ``failed_at`` where ``failed_at`` is the FAILED state's own
time. Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import storage_bounds

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_TS = "timestamp(6) with time zone"
_COLUMNS = {"id": "varchar", "status": "varchar", "start_at": _TS,
            "done_at": _TS, "failed_at": _TS, "created": "timestamp(6)",
            "line": "varchar", "duration_s": "bigint"}
_DESC = ("events(id: varchar; status: varchar e.g. DONE, FAILED, STARTED; "
         f"start_at: {_TS}; done_at: {_TS}; failed_at: {_TS}; "
         "created: timestamp(6); line: varchar e.g. l1, l2; duration_s: bigint)")
_R1 = ("done_at >= TIMESTAMP '2026-07-20 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-21 00:00:00 UTC'")
_R2 = ("done_at >= TIMESTAMP '2026-07-27 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-28 00:00:00 UTC'")
_OR_WINDOW = f"(({_R1}) OR ({_R2}))"
_QUESTION = "Compare how many rows failed on 7/20 with 7/27."


# The measured shape, neutral: FAILED rows grouped, no status filter, windowed
# on the OR of two done_at ranges (done_at is DONE's own time, not FAILED's).
_GROUPED_STATE_OR_WINDOW = (
    f"SELECT line, status, COUNT(*) AS n FROM events WHERE line IN ('l1', 'l2') "
    f"AND {_OR_WINDOW} GROUP BY line, status")
_FAILED_OR_ON_DONE = (f"SELECT COUNT(*) AS n FROM events WHERE status = 'FAILED' "
                      f"AND {_OR_WINDOW}")


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
    _st().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _st().reset()


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
# The helper: a same-column OR of ranges reads as a bound on that column
# --------------------------------------------------------------------------- #
def test_bounded_columns_opt_in_reads_a_same_column_or_window():
    """With the opt-in flag, the OR conjunct counts as a bound on done_at; the
    conjunct text recorded is the whole OR conjunct."""
    sbr = importlib.import_module("agents.storage_bound_rule")
    parts, bounded = sbr.bounded_columns(_GROUPED_STATE_OR_WINDOW, include_same_column_or=True)
    assert "done_at" in bounded
    assert "line" in bounded
    assert bounded["done_at"] and "OR" in bounded["done_at"][0]


def test_bounded_columns_default_still_skips_the_or_conjunct():
    """The default call is byte-identical: the OR conjunct is not read."""
    sbr = importlib.import_module("agents.storage_bound_rule")
    _parts, bounded = sbr.bounded_columns(_GROUPED_STATE_OR_WINDOW)
    assert "done_at" not in bounded
    assert "line" in bounded


@pytest.mark.parametrize("sql", [
    # mixed columns under the OR
    ("SELECT COUNT(*) AS n FROM events WHERE status = 'FAILED' AND "
     "((done_at >= TIMESTAMP '2026-07-20 00:00:00 UTC') OR "
     "(failed_at >= TIMESTAMP '2026-07-27 00:00:00 UTC'))"),
    # a branch with a non-bound conjunct
    ("SELECT COUNT(*) AS n FROM events WHERE "
     f"(({_R1} AND status <> 'X')) OR ({_R2})"),
    # a nested OR inside a branch
    ("SELECT COUNT(*) AS n FROM events WHERE "
     f"(({_R1}) OR (({_R2}) OR (done_at >= TIMESTAMP '2026-07-29 00:00:00 UTC')))"),
    # NOT around the OR
    (f"SELECT COUNT(*) AS n FROM events WHERE NOT (({_R1}) OR ({_R2}))"),
    # a computed bound in one branch
    ("SELECT COUNT(*) AS n FROM events WHERE "
     f"((date(done_at) = DATE '2026-07-20')) OR ({_R2})"),
])
def test_bounded_columns_opt_in_leaves_other_or_shapes_unread(sql):
    sbr = importlib.import_module("agents.storage_bound_rule")
    read = sbr.bounded_columns(sql, include_same_column_or=True)
    if read is None:
        return
    _parts, bounded = read
    assert "done_at" not in bounded and "failed_at" not in bounded


# --------------------------------------------------------------------------- #
# The grouped OR-window shape is refused the same as the single-range form
# --------------------------------------------------------------------------- #
def test_the_grouped_or_window_is_refused_like_the_single_range_form(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_GROUPED_STATE_OR_WINDOW],
                               question="How many rows by status on 7/20 and 7/27?")
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "done_at"
    assert payload["own_columns"]["FAILED"] == "failed_at"
    assert lake.data_queries() == []


def test_a_pinned_failed_count_on_a_done_or_window_is_refused(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_FAILED_OR_ON_DONE])
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "done_at"
    assert payload["own_columns"] == {"FAILED": "failed_at"}
    assert lake.data_queries() == []


def test_the_or_window_repeat_runs_and_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake,
                                   [_FAILED_OR_ON_DONE, f"\n{_FAILED_OR_ON_DONE}  "])
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]]
    assert second["state_time_window"]["window_column"] == "done_at"
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer


# --------------------------------------------------------------------------- #
# A correct window (an OR of two ranges on the state's own column) still passes
# --------------------------------------------------------------------------- #
def test_a_done_count_on_a_done_or_window_still_passes(monkeypatch):
    sql = f"SELECT COUNT(*) AS n FROM events WHERE status = 'DONE' AND {_OR_WINDOW}"
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql],
                              question="How many rows done on 7/20 and 7/27?")
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert lake.data_queries() and "state time, not asked for" not in answer


def test_a_grouped_or_window_on_the_states_own_times_per_branch_still_passes(monkeypatch):
    """A proven per-state OR (each branch bounds its own state's column) is
    still read by _decide_or and runs; the same-column OR reader does not
    change that verdict."""
    sql = ("SELECT status, COUNT(*) AS n FROM events WHERE "
           f"(status = 'DONE' AND {_R1}) OR (status = 'FAILED' AND failed_at >= "
           "TIMESTAMP '2026-07-27 00:00:00 UTC' AND failed_at < "
           "TIMESTAMP '2026-07-28 00:00:00 UTC') GROUP BY status")
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql], question="How many rows by status?")
    assert payload["rows"] == [["42"]] and "state_time_window" not in payload
    assert "state time, not asked for" not in answer
