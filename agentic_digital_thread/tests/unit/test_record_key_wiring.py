# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The declared RECORD KEY, end to end through a Mode A call's guarded Athena
tool: a single-table query that keeps one row per a STRICT SUBSET of the key is
refused once; the identical resend runs and the answer discloses it.

The dry-run miss: ``ROW_NUMBER() OVER (PARTITION BY id ...)``,
``WHERE rn = 1``, ``COUNT(DISTINCT id)`` -- ``id`` is one column of a seven
column record key, so each partitioned-out row is a separate record. The key is
remembered for the system at bake time (``record_key.remember``) the same way
the record-time / state-time / storage rules remember their schema facts.

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
_COLUMNS = {"id": "varchar", "segment_type": "varchar", "entity_type": "varchar",
            "revision": "integer", "cell": "varchar",
            "manufacturing_attempt_id": "varchar", "lifecycle_id": "varchar",
            "state": "varchar", "complete_date": "timestamp(6) with time zone"}
_DESC = ("records(id: varchar; segment_type: varchar; entity_type: varchar; "
         "revision: integer; cell: varchar; manufacturing_attempt_id: varchar; "
         "lifecycle_id: varchar; state: varchar; "
         "complete_date: timestamp(6) with time zone)")
_KEY = ["id", "segment_type", "entity_type", "revision", "cell",
        "manufacturing_attempt_id", "lifecycle_id"]
_QUESTION = "How many segments did C1 complete yesterday?"

_DAY = "TIMESTAMP '2026-10-17 05:30:00 UTC'"
_NEXT = "TIMESTAMP '2026-10-18 05:30:00 UTC'"
_DEDUPE_BY_ID = ("WITH latest AS (SELECT id, revision, state, complete_date, "
       "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
       f"FROM records WHERE cell = 'C1' AND complete_date >= {_DAY} "
       f"AND complete_date < {_NEXT}) "
       "SELECT COUNT(DISTINCT id) AS n FROM latest WHERE rn = 1 AND state = 'COMPLETED'")
# The right query: a plain COUNT(*) over the window, no dedupe.
_PLAIN = ("SELECT COUNT(*) AS n FROM records WHERE cell = 'C1' AND state = 'COMPLETED' "
          f"AND complete_date >= {_DAY} AND complete_date < {_NEXT}")


def _rk():
    return importlib.import_module("agents.record_key")


class _Lake:
    def __init__(self, columns=None):
        self.columns = dict(columns or _COLUMNS)
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "records", "Columns": [{"Name": n, "Type": t} for n, t in self.columns.items()],
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
            names, rows = ["partition"], [["{}"]]
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
    state_times.reset()
    record_time.reset()
    _rk().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    state_times.reset()
    record_time.reset()
    _rk().reset()


def _ask(monkeypatch, lake, sqls, *, question=_QUESTION, key=_KEY):
    """One Mode A call on a system that has ``key`` declared on ``records``.
    Returns ``(tool payloads, final answer)``."""
    sa._remember_turn_time_facts("system-a", [], _DESC)
    # The steward-declared key the bake read from the graph.
    _rk().remember("system-a", {"records": list(key)} if key else {})
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
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", question, ""))
    return payloads, answer


def test_the_p4_subset_dedupe_is_refused_before_it_runs(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_DEDUPE_BY_ID])
    assert payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "RECORD_KEY_DEDUPE"
    assert payload["retryable"] is True
    assert lake.data_queries() == []
    assert "record key" in payload["guidance"].lower()


def test_an_identical_repeat_runs_and_the_answer_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_DEDUPE_BY_ID, f"\n{_DEDUPE_BY_ID}  "])
    assert first["error_name"] == "RECORD_KEY_DEDUPE"
    assert second["rows"] == [["42"]]
    assert len(lake.data_queries()) == 1
    assert "record key" in answer.lower()


def test_the_right_query_is_not_refused(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_PLAIN])
    assert payload["rows"] == [["42"]]
    assert len(lake.data_queries()) == 1
    assert "record_key_dedupe" not in payload


def test_with_no_declared_key_the_subset_dedupe_runs(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_DEDUPE_BY_ID], key=[])
    assert payload["rows"] == [["42"]]


# --------------------------------------------------------------------------- #
# Through the production lookup: table-name resolution (mixed-case, qualified,
# quoted) and a derived table, all via _record_key_rule's remembered key.
# --------------------------------------------------------------------------- #
def _cte(from_clause: str) -> str:
    return ("WITH x AS (SELECT id, "
            "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
            f"FROM {from_clause}) SELECT COUNT(*) AS n FROM x WHERE rn = 1")


@pytest.mark.parametrize("from_clause", [
    "Records",              # mixed-case unquoted, case-insensitive in the engine
    "lake_db.records",      # database-qualified, matched by its last name part
    '"lake_db"."records"',  # quoted-qualified, terminal quoted but same case
])
def test_the_production_lookup_resolves_the_table(monkeypatch, from_clause):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_cte(from_clause)])
    assert payload["error_name"] == "RECORD_KEY_DEDUPE"
    assert lake.data_queries() == []


def test_a_quoted_table_of_a_different_case_does_not_match(monkeypatch):
    """A quoted identifier is case-sensitive, so "Records" is a different table
    from the remembered records: no refusal, the query runs."""
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_cte('"Records"')])
    assert payload["rows"] == [["42"]]
    assert "record_key_dedupe" not in payload


# --------------------------------------------------------------------------- #
# The outer WHERE: AND-only, after stripping balanced parentheses.
# --------------------------------------------------------------------------- #
def _cte_where(where: str) -> str:
    return ("WITH x AS (SELECT id, "
            "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
            f"FROM records) SELECT COUNT(*) AS n FROM x WHERE {where}")


@pytest.mark.parametrize("where", [
    "rn = 1",
    "(rn = 1)",
    "state = 'DONE' AND (rn = 1)",
    "x.rn = 1",
])
def test_an_and_only_rn_equals_one_conjunct_is_refused(monkeypatch, where):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_cte_where(where)])
    assert payload["error_name"] == "RECORD_KEY_DEDUPE"
    assert lake.data_queries() == []


@pytest.mark.parametrize("where", [
    "rn = 1 OR 1 = 1",
    "NOT rn = 1",
    "rn <> 1",
    "rn = 1 IS FALSE",
])
def test_an_or_not_or_non_equality_filter_is_not_refused(monkeypatch, where):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_cte_where(where)])
    assert payload["rows"] == [["42"]]
    assert "record_key_dedupe" not in payload


# --------------------------------------------------------------------------- #
# Out of scope: derived tables, QUALIFY in the producer, a pass-through CTE.
# --------------------------------------------------------------------------- #
_DERIVED = ("SELECT COUNT(*) AS n FROM "
            "(SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
            "FROM records) x WHERE x.rn = 1")
_DERIVED_AS = ("SELECT COUNT(*) AS n FROM "
               "(SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
               "FROM records) AS x WHERE rn = 1")
_QUALIFY = ("WITH x AS (SELECT id, "
            "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
            "FROM records QUALIFY rn = 1) SELECT COUNT(*) AS n FROM x")
_PASS_THROUGH = ("WITH x AS (SELECT id, "
                 "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
                 "FROM records), y AS (SELECT id, rn FROM x) "
                 "SELECT COUNT(*) AS n FROM y WHERE rn = 1")


@pytest.mark.parametrize("sql", [_DERIVED, _DERIVED_AS, _QUALIFY, _PASS_THROUGH])
def test_forms_outside_the_direct_cte_grammar_are_not_read(monkeypatch, sql):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [sql])
    assert payload["rows"] == [["42"]]
    assert "record_key_dedupe" not in payload
