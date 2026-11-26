# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A record-own-time window written as an OR of two ranges on ONE column.

The same OR shape that hid a state-time miss also hid a record-own-time one: a
defect-code query windowed on a lifecycle time written as an OR of two ranges
on that one column. The bounds reader skipped the OR conjunct, so the
record-time check stayed silent. The window rules now read a same-column OR of
ranges; record_time routes its own OR handling through the same helper (one
implementation), and its CONSTRAINED-value OR skip (the population column under
an OR) is unchanged.

Neutral names: a flattened struct ``rec.a.code`` with its sibling time
``rec.a.detected_at``, plus a lifecycle time ``done_at``. Fully offline.
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
_COLUMNS = {"id": "varchar", "status": "varchar",
            "rec.a.code": "varchar", "rec.a.detected_at": _TS,
            "done_at": _TS, "failed_at": _TS, "created": "timestamp(6)", "line": "varchar"}
_DESC = ("events(id: varchar; status: varchar e.g. DONE, FAILED; "
         "rec.a.code: varchar e.g. CodeA, CodeB; "
         f"rec.a.detected_at: {_TS}; done_at: {_TS}; failed_at: {_TS}; "
         "created: timestamp(6); line: varchar)")
_R1 = ("done_at >= TIMESTAMP '2026-07-20 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-21 00:00:00 UTC'")
_R2 = ("done_at >= TIMESTAMP '2026-07-27 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-28 00:00:00 UTC'")
_OR_WINDOW = f"(({_R1}) OR ({_R2}))"
_QUESTION = "Compare defects with code CodeA on 7/20 with 7/27."

# A defect-code population (rec.a.code, whose own time is rec.a.detected_at),
# windowed on an OR of two done_at ranges the question did not name.
_DEFECT_OR_ON_DONE = ("SELECT COUNT(*) AS n FROM events WHERE "
                      "rec.a.code IS NOT NULL "
                      "AND LOWER(rec.a.code) LIKE '%codea%' "
                      f"AND {_OR_WINDOW}")
_DEFECT_OR_ON_OWN = ("SELECT COUNT(*) AS n FROM events WHERE "
                     "rec.a.code IS NOT NULL "
                     "AND ((rec.a.detected_at >= TIMESTAMP '2026-07-20 00:00:00 UTC' "
                     "AND rec.a.detected_at < TIMESTAMP '2026-07-21 00:00:00 UTC') OR "
                     "(rec.a.detected_at >= TIMESTAMP '2026-07-27 00:00:00 UTC' "
                     "AND rec.a.detected_at < TIMESTAMP '2026-07-28 00:00:00 UTC'))")


def _rt():
    return importlib.import_module("agents.record_time")


class _Lake:
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
    _rt().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _rt().reset()


def _ask(monkeypatch, lake, sqls, *, question=_QUESTION, kg_context="", desc=_DESC):
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
# sql_candidate sees the OR window as a bound
# --------------------------------------------------------------------------- #
def test_sql_candidate_true_for_a_same_column_or_window_on_a_struct_field():
    assert _rt().sql_candidate(_DEFECT_OR_ON_DONE) is True


# --------------------------------------------------------------------------- #
# Refused, own column is the sibling time
# --------------------------------------------------------------------------- #
def test_the_defect_or_window_on_a_lifecycle_time_is_refused(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_DEFECT_OR_ON_DONE])
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == "window_not_on_record_time"
    assert payload["population_column"] == "rec.a.code"
    assert payload["own_column"] == "rec.a.detected_at"
    assert payload["window_column"] == "done_at"
    assert lake.data_queries() == []


def test_the_defect_or_window_repeat_runs_and_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake,
                                   [_DEFECT_OR_ON_DONE, f"\n{_DEFECT_OR_ON_DONE}  "])
    assert first["reason"] == "window_not_on_record_time"
    assert second["rows"] == [["42"]]
    assert second["record_time_window"]["window_column"] == "done_at"
    assert len(lake.data_queries()) == 1
    assert "other than record time" in answer


# --------------------------------------------------------------------------- #
# Not refused: the OR window is on the record's own sibling time
# --------------------------------------------------------------------------- #
def test_a_defect_or_window_on_its_own_time_still_passes(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_DEFECT_OR_ON_OWN])
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "record_time_window" not in payload
    assert lake.data_queries() and "other than record time" not in answer


# --------------------------------------------------------------------------- #
# The CONSTRAINED-value OR skip is unchanged: the population column under an OR
# is still not read as a constraint.
# --------------------------------------------------------------------------- #
def test_the_population_column_under_an_or_is_still_not_a_constraint(monkeypatch):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_R1} AND (rec.a.code = 'CodeA' OR line = 'l1')")
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [sql])
    # done_at is a single top-level range here, and the population column is
    # under an OR, so record_time does not fire.
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "record_time_window" not in payload
