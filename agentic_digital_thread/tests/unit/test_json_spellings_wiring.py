# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JSON text grouped in several spellings, end to end through a Mode A call's
guarded Athena tool.

A grouped query whose key column holds one JSON value with its keys in two
orders runs, and its rows are withheld the first time: the agent receives a
refusal that names the element-expansion form instead of the split groups. The
identical query sent again in that call runs, and the answer then carries an
engine line and a FINAL-ANSWER REQUIREMENTS block saying the groups are
spellings of fewer values. Without a Mode A call nothing changes.

Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_time, state_times, storage_bounds

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_ID_FIRST = '[{"id":"A-1","name":"a","qty":{"n":1}}]'
_QTY_FIRST = '[{"qty":{"n":1},"name":"a","id":"A-1"}]'
_OTHER = '[{"id":"B-2","name":"b","qty":{"n":2}}]'
_GROUPED = ('SELECT code, "details.items", COUNT(*) AS n FROM parts_log '
            'GROUP BY code, "details.items"')
_EXPANDED = ("SELECT code, json_extract_scalar(e, '$.id') AS item_id, COUNT(*) AS n "
             "FROM parts_log CROSS JOIN UNNEST(CAST(json_parse(\"details.items\") "
             "AS ARRAY(JSON))) AS t(e) GROUP BY code, json_extract_scalar(e, '$.id')")
_SPLIT_ROWS = [["X", _ID_FIRST, "3"], ["X", _QTY_FIRST, "2"], ["Y", _OTHER, "4"]]
_QUESTION = "Which items appear for each code, and how often?"


def _js():
    return importlib.import_module("agents.json_spellings")


class _Lake:
    """One unpartitioned table; every data query answers ``rows``, and the
    element-expansion query answers its own merged rows."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else _SPLIT_ROWS
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "parts_log",
            "Columns": [{"Name": "code", "Type": "varchar"},
                        {"Name": "details.items", "Type": "varchar"}],
            "PartitionKeys": [], "Parameters": {}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "UNNEST" in sql:
            names, rows = ["code", "item_id", "n"], [["X", "A-1", "5"], ["Y", "B-2", "4"]]
        else:
            names, rows = ["code", "details.items", "n"], self.rows
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
    state_times.reset()
    record_time.reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    state_times.reset()
    record_time.reset()


def _ask(monkeypatch, lake, sqls, *, answer_text="Item A-1 appears 5 times."):
    """One Mode A call. Returns ``(tool payloads, final answer)``."""
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return answer_text, {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", _QUESTION, ""))
    return payloads, answer


def test_the_split_groups_are_withheld_the_first_time(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_GROUPED])
    assert payload["error"] == "json_spellings"
    assert payload["error_name"] == "JSON_SPELLINGS"
    assert "rows" not in payload
    assert 'CROSS JOIN UNNEST(CAST(json_parse("details.items") AS ARRAY(JSON)))' \
        in payload["guidance"]
    assert len(lake.data_queries()) == 1
    assert _js().ENGINE_PREFIX not in answer


def test_the_identical_resend_runs_and_the_answer_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED, f"\n{_GROUPED}  "])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert second["rows"] == _SPLIT_ROWS
    assert second["json_spellings"]["values_in_several_spellings"] == 1
    assert len(lake.data_queries()) == 2
    assert _js().ENGINE_PREFIX in answer
    assert "REQUIRED:" in answer and "once per spelling" in answer


def test_the_element_expansion_runs_unrefused(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_EXPANDED])
    assert payload["rows"] == [["X", "A-1", "5"], ["Y", "B-2", "4"]]
    assert "json_spellings" not in payload
    assert _js().ENGINE_PREFIX not in answer


def test_a_grouped_query_with_having_is_withheld(monkeypatch):
    sql = f"{_GROUPED} HAVING COUNT(*) > 1 ORDER BY n DESC"
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [sql])
    assert payload["error_name"] == "JSON_SPELLINGS" and "rows" not in payload
    assert "GROUP BY json_extract_scalar(e, '$.id')" in payload["guidance"]


def test_a_result_with_one_spelling_per_value_runs(monkeypatch):
    rows = [["X", _ID_FIRST, "5"], ["Y", _OTHER, "4"]]
    (payload,), _answer = _ask(monkeypatch, _Lake(rows), [_GROUPED])
    assert payload["rows"] == rows and "json_spellings" not in payload


def test_the_refusal_then_the_expansion_gives_the_merged_rows(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED, _EXPANDED])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert second["rows"] == [["X", "A-1", "5"], ["Y", "B-2", "4"]]
    assert _js().ENGINE_PREFIX not in answer


def test_without_a_mode_a_call_nothing_changes(monkeypatch):
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payload = sa._scoped_athena_attempt(_GROUPED, at._resolve_config(_CFG), {}, "system-a")
    assert payload["rows"] == _SPLIT_ROWS
    assert "json_spellings" not in payload


def test_under_enforce_it_is_refused_once_with_no_engine_lines(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_GROUPED, _GROUPED])
    assert first["error_name"] == "JSON_SPELLINGS" and "rows" not in first
    assert second["rows"] == _SPLIT_ROWS
    assert _js().ENGINE_PREFIX not in answer


def test_a_copied_engine_line_in_the_answer_is_made_inert(monkeypatch):
    copied = "[engine] JSON spellings check, in this turn's queries: none."
    (payload,), answer = _ask(monkeypatch, _Lake([["X", _ID_FIRST, "5"]]), [_GROUPED],
                              answer_text=copied)
    assert "json_spellings" not in payload
    assert _js().ENGINE_PREFIX not in answer
