# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The steward-declared ROW EXCLUSION, end to end through a Mode A call's guarded
Athena tool and the system agent's prompt.

A query that certainly reads the table without the owner's exclusion predicate
is refused once (with the label and the exact predicate to AND in); the
identical resend runs and the answer discloses that its figures may include the
rows the owner marks as not records. A query that carries the predicate runs at
once and the answer says it excluded them. A query with several reads of the
table (a union, a join, a subquery) is decided on every read, and one that may
read it but cannot be read with certainty is refused once the same way. Another
table of the same system and a system with no exclusion are untouched. The exclusion
is remembered for the system at bake time, as the record key is, and each
question the system agent is sent carries it as a schema fact.

Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_key, record_time, state_times, storage_bounds

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_COLUMNS = {"pipeline": "varchar", "run_ref": "varchar", "step_kind": "varchar",
            "state": "varchar", "finished_at": "timestamp(6) with time zone",
            "id": "varchar", "revision": "integer"}
_DESC = ("job_events(pipeline: varchar; run_ref: varchar; step_kind: varchar; "
         "state: varchar; finished_at: timestamp(6) with time zone; id: varchar; "
         "revision: integer), ledger_lines(batch_tag: varchar; amount: double)")
_QUESTION = "How many steps failed yesterday?"
_WIN = ("finished_at >= TIMESTAMP '2026-10-17 05:30:00 UTC' "
        "AND finished_at < TIMESTAMP '2026-10-18 05:30:00 UTC'")
_LABEL = "shadow copies"


def _rx():
    return importlib.import_module("agents.row_exclusion")


def _fact():
    return _rx().exclusion_from(_LABEL, [
        {"column": "pipeline", "op": "not_empty"},
        {"column": "run_ref", "op": "not_empty"},
        {"column": "step_kind", "op": "not_in", "values": ["ASSEMBLE", "PACK"]}])


def _pred() -> str:
    return _rx().predicate_sql(_fact())


def _missing() -> str:
    return f"SELECT COUNT(*) AS n FROM job_events WHERE state = 'FAILED' AND {_WIN}"


def _present() -> str:
    return f"{_missing()} AND {_pred()}"


class _Lake:
    def __init__(self):
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "job_events",
            "Columns": [{"Name": n, "Type": t} for n, t in _COLUMNS.items()],
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
            rows = [[n, t, ""] for n, t in _COLUMNS.items()]
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
    for reset in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, storage_bounds.reset,
                  state_times.reset, record_time.reset, record_key.reset, _rx().reset):
        reset()
    yield
    for reset in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, storage_bounds.reset,
                  state_times.reset, record_time.reset, record_key.reset, _rx().reset):
        reset()


def _ask(monkeypatch, lake, sqls, *, exclusions=None, keys=None, graph=None):
    """One Mode A call on ``system-a``. Returns ``(tool payloads, answer)``.
    With ``graph`` (a row-exclusion graph read seam) the facts are what the bake
    reads through it; otherwise ``exclusions`` is remembered directly."""
    monkeypatch.setattr(_rx(), "_graph_query",
                        graph or (lambda: (lambda *_a, **_k: [{}])))
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    sa._remember_turn_time_facts("system-a", [], _DESC)
    if graph is None:
        _rx().remember("system-a",
                       {"job_events": _fact()} if exclusions is None else exclusions)
    record_key.remember("system-a", keys or {})
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
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", _QUESTION, ""))
    return payloads, answer


def _requirements(answer: str) -> str:
    marker = "----- BEGIN FINAL-ANSWER REQUIREMENTS -----"
    return answer[answer.index(marker):] if marker in answer else ""


def test_a_read_without_the_predicate_is_refused_before_it_runs(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_missing()])
    assert payload["error_name"] == "ROW_EXCLUSION"
    assert payload["error_type"] == "invalid_request"
    assert payload["retryable"] is True
    assert lake.data_queries() == []
    assert f"AND {_pred()}" in payload["guidance"] and _LABEL in payload["guidance"]


def test_the_identical_resend_runs_and_the_answer_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_missing(), f"\n{_missing()}  "])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert second["rows"] == [["42"]]
    assert second["row_exclusion"]["applied"] is False
    assert len(lake.data_queries()) == 1
    assert "[engine] Row exclusion" in answer
    assert "without the source owner's row exclusion" in answer
    block = _requirements(answer)
    assert "may include rows" in block and _LABEL in block


def test_a_read_with_the_predicate_runs_and_says_it_excluded_them(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_present()])
    assert payload["rows"] == [["42"]]
    assert payload["row_exclusion"]["applied"] is True
    assert len(lake.data_queries()) == 1
    assert _pred() in lake.data_queries()[0]
    assert "with the source owner's row exclusion" in answer
    block = _requirements(answer)
    assert "one short clause" in block and "excluded" in block and _LABEL in block


def test_another_table_of_the_same_system_is_untouched(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, ["SELECT SUM(amount) AS n FROM ledger_lines"])
    assert payload["rows"] == [["42"]] and "row_exclusion" not in payload
    assert "Row exclusion" not in answer


def test_with_no_exclusion_the_read_runs(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_missing()], exclusions={})
    assert payload["rows"] == [["42"]] and "row_exclusion" not in payload
    assert "Row exclusion" not in answer


def test_a_join_without_the_predicate_is_refused_once_then_disclosed(monkeypatch):
    lake = _Lake()
    sql = ("SELECT COUNT(*) AS n FROM job_events j JOIN ledger_lines l "
           "ON j.run_ref = l.batch_tag")
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert first["reads"][0]["predicate"] == _rx().predicate_sql(_fact(), qualifier="j")
    assert second["rows"] == [["42"]] and second["row_exclusion"]["applied"] is False
    assert len(lake.data_queries()) == 1
    assert "may include rows" in _requirements(answer)


def _union(*carried: bool) -> str:
    return " UNION ALL ".join(_present() if c else _missing() for c in carried)


def test_a_union_whose_every_branch_carries_it_runs_and_says_so_once(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_union(True, True, True)])
    assert payload["rows"] == [["42"]] and payload["row_exclusion"]["applied"] is True
    assert answer.count("job_events was read with the source owner's row exclusion") == 1


def test_a_union_with_a_branch_missing_it_is_refused_naming_the_branch(monkeypatch):
    lake = _Lake()
    sql = _union(True, False)
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert "branch 2 of the UNION ALL" in first["guidance"]
    (read,) = first["reads"]
    assert "branch 2 of the UNION ALL" in read["read"] and read["predicate"] == _pred()
    assert second["rows"] == [["42"]] and second["row_exclusion"]["applied"] is False
    assert len(lake.data_queries()) == 1
    assert "may include rows" in _requirements(answer)


def test_a_read_it_cannot_prove_is_refused_once_then_disclosed(monkeypatch):
    """The owner's conditions written only after a pass-through CTE: the
    read of the table cannot be proven, so it fails closed (a comma-joined
    FROM, the other shape, is already held back by the scan guard)."""
    lake = _Lake()
    sql = (f"WITH x AS (SELECT * FROM job_events WHERE {_WIN}) SELECT COUNT(*) AS n FROM x "
           f"WHERE {_rx().predicate_sql(_fact(), qualifier='x')}")
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql])
    assert first["error_name"] == "ROW_EXCLUSION" and first["unproven"] == "conditions_elsewhere"
    assert f"AND {_pred()}" in first["guidance"]
    assert second["rows"] == [["42"]] and second["row_exclusion"]["applied"] is False
    assert "may have been read without the source owner's row exclusion" in answer
    assert "may include rows" in _requirements(answer)


def test_under_enforce_a_union_missing_a_branch_is_refused_once(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    sql = _union(False, True)
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert "branch 1 of the UNION ALL" in first["guidance"]
    assert second["rows"] == [["42"]] and len(lake.data_queries()) == 1
    assert "[engine] Row exclusion" not in json.dumps(answer)


def test_under_enforce_the_first_send_is_refused_and_no_line_is_added(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_missing(), _missing()])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert len(lake.data_queries()) == 1
    assert second["rows"] == [["42"]]
    assert "[engine] Row exclusion" not in json.dumps(answer)


def test_a_record_key_dedupe_and_a_missing_exclusion_are_refused_together(monkeypatch):
    lake = _Lake()
    sql = ("WITH x AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) "
           f"AS rn FROM job_events WHERE {_WIN}) SELECT COUNT(*) AS n FROM x WHERE rn = 1")
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql],
                                   keys={"job_events": ["id", "revision", "pipeline"]})
    reasons = {first.get("reason")} | {r["reason"] for r in first.get("also_refused", [])}
    assert reasons == {"dedupe_subset_of_record_key", "row_exclusion_not_applied"}
    assert second["rows"] == [["42"]]
    assert "Record key" in answer and "Row exclusion" in answer


# --------------------------------------------------------------------------- #
# The bake and the schema fact in the system prompt.
# --------------------------------------------------------------------------- #
def _stored() -> str:
    return json.dumps({"label": _LABEL, "conditions": [
        {"column": "pipeline", "op": "not_empty"},
        {"column": "run_ref", "op": "not_empty"},
        {"column": "step_kind", "op": "not_in", "values": ["ASSEMBLE", "PACK"]}]})


def _listed_graph(job_types: dict):
    """The graph a bake reads: system-a's manifest lists job_events, which
    holds the stored exclusion and whose Fields are ``job_types``;
    ledger_lines is not listed."""
    from tests.unit._row_exclusion_graph import RxGraph
    g = RxGraph({"system-a": {"job_events": job_types,
                              "ledger_lines": {"batch_tag": "varchar", "amount": "double"}}})
    g.list_table("system-a", "job_events")
    g.entries["system-a.job_events"]["props"]["row_exclusion"] = _stored()
    return g


def test_a_drifted_fact_refuses_reads_of_that_table_only(monkeypatch):
    """The stored exclusion names pipeline, which is now a bigint: reads of
    job_events are refused before Athena with the steward repair named, and a
    read of another table of the system runs."""
    types = {**_COLUMNS, "pipeline": "bigint"}
    lake = _Lake()
    (first, other), answer = _ask(
        monkeypatch, lake, [_present(), "SELECT SUM(amount) AS n FROM ledger_lines"],
        graph=_listed_graph(types).seam())
    assert first["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert first["unavailable"] == "column_not_string" and first["columns"] == ["pipeline"]
    assert "A steward must repair or clear the row exclusion of job_events" in first["guidance"]
    assert other["rows"] == [["42"]]
    assert lake.data_queries() == ["SELECT SUM(amount) AS n FROM ledger_lines LIMIT 500"]
    assert "no longer fits the table" in answer


def test_the_bake_remembers_the_exclusion_and_each_question_states_it(monkeypatch):
    monkeypatch.setattr(_rx(), "_graph_query", _listed_graph(_COLUMNS).seam())
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    sa._remember_turn_time_facts("system-a", [], _DESC)
    assert _rx().exclusion_for("system-a", "job_events") == _fact()
    prompt = sa._compose_system_prompt("system-a", "System A", "lake_db", "", _DESC, "Athena")
    assert "ROW EXCLUSIONS" not in prompt
    block = sa._row_exclusion_block("system-a")
    assert "ROW EXCLUSIONS (facts" in block and block.count(_pred()) == 1


def _broken_graph():
    def query(*_a, **_k):
        raise RuntimeError("graph down")
    return query


def test_a_graph_failure_at_bake_keeps_the_query_away_from_athena(monkeypatch):
    """The bake could not read the facts: no ROW EXCLUSIONS line, and a read of
    the table is refused before Athena every time (no resend path), and the
    answer says its figures could not be confirmed."""
    lake = _Lake()
    payloads, answer = _ask(monkeypatch, lake, [_missing(), _missing(), _present()],
                            graph=lambda: _broken_graph())
    assert [p["error_name"] for p in payloads] == ["ROW_EXCLUSION_UNAVAILABLE"] * 3
    assert all(p["retryable"] is False for p in payloads)
    assert lake.data_queries() == []
    assert "could not be read" in answer and "could not be confirmed" in answer
    prompt = sa._compose_system_prompt("system-a", "System A", "lake_db", "", _DESC, "Athena")
    assert "ROW EXCLUSIONS" not in prompt
    assert sa._row_exclusion_block("system-a") == ""


def test_a_graph_failure_holds_back_reads_of_every_described_table(monkeypatch):
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, ["SELECT SUM(amount) AS n FROM ledger_lines"],
                               graph=lambda: _broken_graph())
    assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert lake.data_queries() == []


def test_a_successful_empty_read_lets_the_query_run(monkeypatch):
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_missing()],
                              graph=lambda: (lambda *_a, **_k: [{}]))
    assert payload["rows"] == [["42"]] and "row_exclusion" not in payload
    assert "Row exclusion" not in answer


def test_a_later_successful_re_read_heals_it_without_a_rebuild(monkeypatch):
    """The bake fails; the graph comes back before the first query: that
    query's one re-read heals the state, so it is checked like any other read
    (refused once for the missing predicate), and the predicate form runs."""
    g = _listed_graph(_COLUMNS)
    g.fail.add("manifest")
    lake = _Lake()
    monkeypatch.setattr(sa, "_remember_turn_time_facts", _then(sa._remember_turn_time_facts,
                                                               g.fail.clear))
    payloads, answer = _ask(monkeypatch, lake, [_missing(), _present()], graph=g.seam())
    assert payloads[0]["error_name"] == "ROW_EXCLUSION"
    assert payloads[1]["rows"] == [["42"]] and payloads[1]["row_exclusion"]["applied"] is True
    assert len(lake.data_queries()) == 1
    assert "with the source owner's row exclusion" in answer


def _then(first, after):
    def run(*args, **kwargs):
        out = first(*args, **kwargs)
        after()
        return out
    return run


# --------------------------------------------------------------------------- #
# A query-time catalog fault skips only the catalog-dependent rules.
# --------------------------------------------------------------------------- #
def _catalog_down(monkeypatch):
    def _raises(*_a, **_k):
        raise RuntimeError("metadata throttled")
    monkeypatch.setattr(at, "storage_facts", _raises)


def test_a_catalog_fault_still_refuses_a_read_without_the_predicate(monkeypatch):
    _catalog_down(monkeypatch)
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake, [_missing(), _missing()])
    assert first["error_name"] == "ROW_EXCLUSION"
    assert second["rows"] == [["42"]]
    assert second["row_exclusion"]["applied"] is False
    assert len(lake.data_queries()) == 1
    assert "without the source owner's row exclusion" in answer
    assert "may include rows" in _requirements(answer)


def test_a_catalog_fault_still_discloses_a_read_with_the_predicate(monkeypatch):
    _catalog_down(monkeypatch)
    lake = _Lake()
    (payload,), answer = _ask(monkeypatch, lake, [_present()])
    assert payload["rows"] == [["42"]] and payload["row_exclusion"]["applied"] is True
    assert "with the source owner's row exclusion" in answer


def test_a_catalog_fault_still_refuses_a_record_key_dedupe(monkeypatch):
    _catalog_down(monkeypatch)
    lake = _Lake()
    sql = ("WITH x AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) "
           f"AS rn FROM job_events WHERE {_WIN} AND {_pred()}) "
           "SELECT COUNT(*) AS n FROM x WHERE rn = 1")
    (first, second), answer = _ask(monkeypatch, lake, [sql, sql],
                                   keys={"job_events": ["id", "revision", "pipeline"]})
    assert first["error_name"] == "RECORD_KEY_DEDUPE"
    assert second["rows"] == [["42"]] and "record_key_dedupe" in second
    assert len(lake.data_queries()) == 1
    assert "Record key" in answer
