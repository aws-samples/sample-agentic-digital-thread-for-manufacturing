# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A figure an approximate aggregate computed is reported as approximate.

Measured on an analytics deployment: the system agent was asked for discrete
percentiles (an actual observed value), its exact attempts failed, it fell
back to ``approx_percentile``, and neither its answer nor the final answer
said the figures were estimates. The 90th percentile it reported was 8.5%
below the exact discrete value of the same rows. The result payload looked
exactly like an exact one: no label, no note.

Now:

* the guarded Athena tool labels a result whose SQL calls an approximate
  aggregate (``approximate``: the functions and the result columns that use
  them) and adds a note telling the agent to report those figures as
  approximate;
* the system agent's answer gets an ``[engine] Approximate:`` line naming the
  approximate figures its own prose states (none stated: no line);
* the orchestrator's final answer gets a "How this was computed:" sentence
  when it states those figures attributed to that system;
* an approximate exact-split result says approximate in its engine line too;
* the scan-budget suggestion that offers ``approx_percentile`` says to report
  the figures as approximate.

A function name in a string literal, a longer identifier or a comment is not
a call. Fully offline; synthetic names and numbers.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import answer_provenance, eval_core, storage_bounds
from agents.answer_runs import AnswerRuns

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_COLUMNS = {"id": "varchar", "line": "varchar", "duration_s": "bigint",
            "created": "timestamp(6)"}
_WINDOW = ("created >= TIMESTAMP '2026-01-05 00:00:00' "
           "AND created < TIMESTAMP '2026-01-06 00:00:00'")
_APPROX = ("SELECT line, COUNT(*) AS n, approx_percentile(duration_s, 0.90) AS p90_s, "
           f"approx_percentile(duration_s, 0.99) AS p99_s FROM events WHERE {_WINDOW} "
           "GROUP BY line ORDER BY line")
_APPROX_ROWS = (["line", "n", "p90_s", "p99_s"],
                [["l1", "4873", "713.4951", "860.2294"], ["l2", "23144", "223.1", "439.0"]])
_EXACT = ("WITH ranked AS (SELECT line, duration_s, ROW_NUMBER() OVER (PARTITION BY line "
          "ORDER BY duration_s) AS rn, COUNT(*) OVER (PARTITION BY line) AS n FROM events "
          f"WHERE {_WINDOW} AND duration_s IS NOT NULL) SELECT line, n, MAX(CASE WHEN rn = "
          "CAST(CEIL(n * 0.90) AS INTEGER) THEN duration_s END) AS p90_s FROM ranked "
          "GROUP BY line, n ORDER BY line")
_EXACT_ROWS = (["line", "n", "p90_s"], [["l1", "4873", "716"], ["l2", "23144", "224"]])
_TOOL = "system-a___query_system_a"
_OTHER_TOOL = "system-b___query_system_b"


def _approx():
    return importlib.import_module("agents.approximate_aggregates")


class _Lake:
    """One unpartitioned Iceberg table; a data query gets ``answer(sql)``."""

    def __init__(self, answer):
        self.answer = answer
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events", "Columns": [{"Name": n, "Type": t} for n, t in _COLUMNS.items()],
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
            names, rows = ["partition"], []
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in _COLUMNS.items()]
        else:
            names, rows = self.answer(sql)
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self) -> list[str]:
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


def _by_query(sql: str):
    return _EXACT_ROWS if "ROW_NUMBER" in sql else _APPROX_ROWS


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


def _run_tool(monkeypatch, sqls, answer=_by_query) -> tuple[list[dict], _Lake]:
    """The guarded Athena tool's payloads for ``sqls``, inside one Mode A call."""
    lake = _Lake(answer)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    cfg = at._resolve_config(_CFG)
    payloads: list[dict] = []

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "Done.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    asyncio.run(sa._run_mode_a_query(object(), "system-a", "What are the p90 and p99?"))
    return payloads, lake


def _state_after(*payloads) -> dict:
    """The invocation state the capture hook leaves after these tool results."""
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for n, payload in enumerate(payloads):
        result = {"toolUseId": f"t-{n}", "status": "success",
                  "content": [{"text": json.dumps(payload)}]}
        capture._on_after_tool_call(SimpleNamespace(
            selected_tool=None,
            tool_use={"toolUseId": f"t-{n}", "name": "athena_run_query",
                      "input": {"sql": "SELECT 1"}},
            invocation_state=state, result=result, exception=None, retry=False,
            agent=SimpleNamespace(messages=[])))
    return state


def _mode_a(answer: str, state: dict) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _agent, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", "question"))


def _approx_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[engine] Approximate: ")]


# --------------------------------------------------------------------------- #
# The result is labelled
# --------------------------------------------------------------------------- #
def test_a_grouped_query_with_two_approximate_outputs_is_labelled(monkeypatch):
    (payload,), lake = _run_tool(monkeypatch, [_APPROX])
    assert payload["rows"] == _APPROX_ROWS[1]
    assert payload["approximate"] == {"functions": ["approx_percentile"],
                                      "column_indexes": [2, 3]}
    note = payload["note"]
    assert "approx_percentile" in note and "approximate" in note
    assert "not exact" in note
    assert "\u2014" not in note and "\u2013" not in note
    assert len(lake.data_queries()) == 1


def test_an_exact_query_carries_no_label(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_EXACT])
    assert payload["rows"] == _EXACT_ROWS[1]
    assert "approximate" not in payload
    assert "approx" not in str(payload.get("note", ""))


@pytest.mark.parametrize("sql", [
    f"SELECT line, 'approx_percentile(x, 0.9)' AS how, COUNT(*) AS n FROM events "
    f"WHERE {_WINDOW} GROUP BY line",
    f"SELECT line, MAX(duration_s) AS approx_percentile_hours FROM events WHERE {_WINDOW} "
    "GROUP BY line",
    f"SELECT line, COUNT(*) AS n -- approx_percentile(duration_s, 0.9)\nFROM events "
    f"WHERE {_WINDOW} GROUP BY line",
    f"SELECT line, COUNT(*) AS n /* approx_distinct(id) */ FROM events WHERE {_WINDOW} "
    "GROUP BY line",
])
def test_a_function_name_that_is_not_a_call_is_not_counted(monkeypatch, sql):
    assert _approx().functions(sql) == ()
    (payload,), _lake = _run_tool(monkeypatch, [sql], answer=lambda _s: (["line", "n"],
                                                                       [["l1", "3"]]))
    assert "approximate" not in payload


def test_the_approximate_set_is_closed_and_read_from_calls_only():
    a = _approx()
    assert a.functions(
        "SELECT approx_distinct(id) AS d, APPROX_MOST_FREQUENT(3, line, 100) AS f, "
        "numeric_histogram(4, duration_s) AS h, value_at_quantile(qdigest_agg(duration_s), "
        "0.5) AS m FROM events") == ("approx_distinct", "approx_most_frequent",
                                     "numeric_histogram", "value_at_quantile", "qdigest_agg")
    # A qualified name is not one of the engine's built-ins.
    assert a.functions("SELECT udf.approx_percentile(duration_s, 0.9) FROM events") == ()
    # An uncertain tokenization reads nothing.
    assert a.functions("SELECT approx_percentile(duration_s, 0.9) FROM events /* x") == ()


# --------------------------------------------------------------------------- #
# Which output columns are approximate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql, expected", [
    (_APPROX, (2, 3)),
    # A projection of one CTE's columns, by name or with *.
    ("WITH p AS (SELECT line, COUNT(*) AS n, approx_percentile(duration_s, 0.9) AS p90 "
     "FROM events GROUP BY line) SELECT line, n, p90 / 60.0 AS p90_min FROM p", (2,)),
    ("WITH p AS (SELECT line, COUNT(*) AS n, approx_percentile(duration_s, 0.9) AS p90 "
     "FROM events GROUP BY line) SELECT * FROM p ORDER BY line", (2,)),
    ("SELECT * FROM (SELECT line, approx_distinct(id) AS d FROM events GROUP BY line) s",
     (1,)),
    # An approximate call that feeds no output column.
    ("SELECT line, COUNT(*) AS n FROM events GROUP BY line "
     "HAVING approx_percentile(duration_s, 0.9) > 10", ()),
    # A scalar subquery in the SELECT list.
    ("SELECT (SELECT approx_distinct(id) FROM events) AS d, 1 AS one", (0,)),
])
def test_the_approximate_output_columns_are_read(sql, expected):
    assert _approx().outputs(sql) == expected


@pytest.mark.parametrize("sql", [
    # * over a physical table, a join, or a set operation cannot be indexed.
    "SELECT *, approx_percentile(duration_s, 0.9) AS p90 FROM events",
    "WITH p AS (SELECT line, approx_percentile(duration_s, 0.9) AS p90 FROM events "
    "GROUP BY line) SELECT * FROM p JOIN lines l ON l.line = p.line",
    "SELECT approx_distinct(id) AS d FROM events UNION ALL SELECT COUNT(*) FROM events",
    "SELECT approx_percentile(duration_s, 0.9) FROM events /* unterminated",
])
def test_an_unreadable_select_list_gives_no_columns(sql):
    assert _approx().outputs(sql) is None


def test_the_label_keeps_the_functions_when_the_columns_cannot_be_read(monkeypatch):
    sql = (f"WITH p AS (SELECT line, approx_percentile(duration_s, 0.9) AS p90 FROM events "
           f"WHERE {_WINDOW} GROUP BY line) SELECT * FROM p JOIN events e ON e.line = p.line")
    (payload,), _lake = _run_tool(monkeypatch, [sql], answer=lambda _s: (
        ["line", "p90"], [["l1", "713.5"]]))
    assert payload["approximate"] == {"functions": ["approx_percentile"]}
    assert "approximate" in payload["note"]


# --------------------------------------------------------------------------- #
# The system agent's answer: one engine line, only for figures it states
# --------------------------------------------------------------------------- #
def test_an_answer_stating_the_percentile_figures_gets_one_approximate_line(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    answer = _mode_a("Line l1: p90 713.50 s and p99 860.23 s over 4,873 rows.",
                     _state_after(payload))
    assert _approx_lines(answer) == [
        "[engine] Approximate: 713.50; 860.23 in this answer are estimates of the same "
        "measure from approx_percentile, not exact values."]


def test_one_stated_figure_reads_in_the_singular(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    answer = _mode_a("Line l2 p90 is 223.1 s.", _state_after(payload))
    assert _approx_lines(answer) == [
        "[engine] Approximate: 223.1 in this answer is an estimate of the same measure from "
        "approx_percentile, not an exact value."]


_QUERY_LINE = ("[engine] Approximate: this system's query also computed approximate values "
               "with approx_percentile; any value taken or converted from those outputs is an "
               "estimate.")
_QUERY_SENTENCE = ("How this was computed: system-a's query also computed approximate values "
                   "with approx_percentile; any value taken or converted from those outputs is "
                   "an estimate.")
_SEVEN_LINE = ("[engine] Approximate: 7 in this answer is an estimate of the same measure from "
               "approx_percentile, not an exact value.")
_P90_MINUTES = (f"SELECT line, approx_percentile(duration_s, 0.90) / 60.0 AS p90_min FROM events "
                f"WHERE {_WINDOW} GROUP BY line")
_P50_APPROX = f"SELECT approx_percentile(duration_s, 0.5) AS p50 FROM events WHERE {_WINDOW}"
_P50_EXACT = ("WITH ranked AS (SELECT duration_s, ROW_NUMBER() OVER (ORDER BY duration_s) AS rn, "
              f"COUNT(*) OVER () AS n FROM events WHERE {_WINDOW} AND duration_s IS NOT NULL) "
              "SELECT MAX(CASE WHEN rn = GREATEST(1, CAST(CEIL(n * 0.5) AS BIGINT)) "
              "THEN duration_s END) AS p50 FROM ranked")


def _minutes(_sql):
    return ["line", "p90_min"], [["l1", "60"]]


def _seven(_sql):
    return ["p50"], [["7"]]


def test_an_answer_stating_only_the_count_gets_the_neutral_line(monkeypatch):
    """A COUNT beside approx_percentile in one result; the answer states only
    the count, so no figure is called an estimate and the line stays neutral."""
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    answer = _mode_a("Line l1 had 4,873 rows and l2 had 23,144.", _state_after(payload))
    assert _approx_lines(answer) == [_QUERY_LINE]


def test_a_converted_estimate_gets_the_neutral_line(monkeypatch):
    """60 minutes from approx_percentile, stated as 1 hour: no figure matches."""
    (payload,), _lake = _run_tool(monkeypatch, [_P90_MINUTES], answer=_minutes)
    assert payload["approximate"]["column_indexes"] == [1]
    answer = _mode_a("The p90 is 1 hour.", _state_after(payload))
    assert _approx_lines(answer) == [_QUERY_LINE]


def test_an_answer_with_no_number_still_gets_the_neutral_line(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_P90_MINUTES], answer=_minutes)
    reply = _mode_a("The p90 could not be read.", _state_after(payload))
    assert _approx_lines(reply) == [_QUERY_LINE]


def test_an_approximate_result_with_no_rows_gets_no_line(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_P50_APPROX], answer=lambda _s: (["p50"], []))
    assert payload["approximate"]["functions"] == ["approx_percentile"]
    assert _approx_lines(_mode_a("No rows matched.", _state_after(payload))) == []


def test_an_exact_result_equal_to_an_earlier_estimate_gets_no_figure_line(monkeypatch):
    (approx, exact), _lake = _run_tool(monkeypatch, [_P50_APPROX, _P50_EXACT], answer=_seven)
    assert "approximate" in approx and "approximate" not in exact
    reply = _mode_a("The exact p50 from ROW_NUMBER is 7.", _state_after(approx, exact))
    assert _approx_lines(reply) == [_QUERY_LINE]
    facts = answer_provenance.facts_in_tool_text(
        f"{reply}\n\n[engine] Cite this result as [1].", "system-a", 1)
    assert [f["figures"] for f in facts if f["kind"] == "approximate"] == [[]]
    assert answer_provenance.final_lines(facts, "The p50 is 7 [1].", {"system-a"},
                                         {1: "system-a"}) == [_QUERY_SENTENCE]


def _filler(n: int) -> dict:
    return {"columns": ["n"], "rows": [[str(1000 + n)]], "row_count": 1,
            "_adt_executed_query": f"SELECT COUNT(*) AS n FROM events WHERE id = 'r{n}'"}


@pytest.mark.parametrize("position", [32, 33])
def test_the_sidecar_cap_keeps_the_estimate_and_its_later_exact_value(monkeypatch, position):
    """The approximate result is statement ``position`` and an exact result
    with the same value follows it: the sidecar holds 32 rows, the
    approximate list holds both, so 7 is never called an estimate."""
    (approx, exact), _lake = _run_tool(monkeypatch, [_P50_APPROX, _P50_EXACT], answer=_seven)
    fillers = [_filler(n) for n in range(position - 1)]
    state = _state_after(*fillers, approx, exact)
    assert len(state[trc.SIDECAR_STATEMENTS_STATE_KEY]) == trc.sidecar_statement_cap() == 32
    assert [e["kind"] for e in state[trc.APPROXIMATE_STATE_KEY]] == ["approximate", "shadow"]
    assert _approx_lines(_mode_a("The exact p50 is 7.", state)) == [_QUERY_LINE]
    alone = _state_after(*fillers, approx)
    assert _approx_lines(_mode_a("The p50 is 7.", alone)) == [_SEVEN_LINE]


def test_the_approximate_list_is_bounded_at_the_tool_call_budget(monkeypatch):
    (approx,), _lake = _run_tool(monkeypatch, [_P50_APPROX], answer=_seven)
    budget = sa._tool_call_budget()
    state = _state_after(approx, *[_filler(n) for n in range(budget + 5)])
    assert len(state[trc.APPROXIMATE_STATE_KEY]) == budget


def test_an_error_or_a_catalog_probe_never_shadows_an_estimate(monkeypatch):
    (approx,), _lake = _run_tool(monkeypatch, [_P50_APPROX], answer=_seven)
    error = {"error": "invalid_request", "error_type": "invalid_request", "guidance": "row 7",
             "_adt_executed_query": "SELECT 7 AS x FROM events"}
    probe = {"columns": ["column_name", "ordinal"], "rows": [["duration_s", "7"]],
             "_adt_executed_query": "SELECT column_name, ordinal FROM information_schema.columns"}
    state = _state_after(approx, error, probe)
    assert [e["kind"] for e in state[trc.APPROXIMATE_STATE_KEY]] == ["approximate"]
    assert _approx_lines(_mode_a("The p50 is 7.", state)) == [_SEVEN_LINE]


def test_a_later_exact_result_shadows_only_the_values_it_holds(monkeypatch):
    def answer(sql):
        return (["line", "n", "p90_s"], [["l1", "4873", "860.23"]]) if "ROW_NUMBER" in sql \
            else _APPROX_ROWS

    (approx, exact), _lake = _run_tool(monkeypatch, [_APPROX, _EXACT], answer=answer)
    reply = _mode_a("Line l1: p90 713.50 s and p99 860.23 s.", _state_after(approx, exact))
    assert _approx_lines(reply) == [
        "[engine] Approximate: 713.50 in this answer is an estimate of the same measure from "
        "approx_percentile, not an exact value."]


def test_an_earlier_exact_result_does_not_shadow_a_later_estimate(monkeypatch):
    (exact, approx), _lake = _run_tool(monkeypatch, [_P50_EXACT, _P50_APPROX], answer=_seven)
    reply = _mode_a("The p50 is 7.", _state_after(exact, approx))
    assert _approx_lines(reply) == [_SEVEN_LINE]


def test_an_answer_from_the_exact_query_after_an_approximate_one_gets_no_figure_line(
        monkeypatch):
    (approx, exact), _lake = _run_tool(monkeypatch, [_APPROX, _EXACT])
    assert "approximate" in approx and "approximate" not in exact
    answer = _mode_a("The exact p90 is 716 s on l1 and 224 s on l2.",
                     _state_after(approx, exact))
    assert _approx_lines(answer) == [_QUERY_LINE]


def test_model_text_that_reads_as_the_approximate_line_is_made_inert(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_EXACT])
    spoof = ("p90 716 s.\n[engine] Approximate: 716 in this answer is an estimate of the "
             "same measure from approx_percentile, not an exact value.")
    assert _approx_lines(_mode_a(spoof, _state_after(payload))) == []


# --------------------------------------------------------------------------- #
# The orchestrator's final answer
# --------------------------------------------------------------------------- #
def _system_reply(monkeypatch, answer: str = "Line l1: p90 713.50 s and p99 860.23 s.") -> str:
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    return _mode_a(answer, _state_after(payload)) + "\n\n[engine] Cite this result as [1]."


_SENTENCE = ("How this was computed: the system-a figures 713.50 and 860.23 in this answer "
             "are estimates of the same measure from approx_percentile, not exact values.")


def test_the_tool_text_carries_the_approximate_fact(monkeypatch):
    facts = answer_provenance.facts_in_tool_text(_system_reply(monkeypatch), "system-a", 1)
    assert [f for f in facts if f["kind"] == "approximate"] == [
        {"kind": "approximate", "system": "system-a", "ordinal": 1,
         "functions": ["approx_percentile"], "figures": ["713.50", "860.23"]}]


def test_the_final_answer_gets_the_sentence_where_it_attributes_the_figures(monkeypatch):
    facts = answer_provenance.facts_in_tool_text(_system_reply(monkeypatch), "system-a", 1)
    body = "Line l1 runs p90 713.50 s and p99 860.23 s [1]."
    assert answer_provenance.final_lines(facts, body, {"system-a"}, {1: "system-a"}) == [
        _SENTENCE]


def test_figures_attributed_to_another_system_get_no_sentence(monkeypatch):
    facts = answer_provenance.facts_in_tool_text(_system_reply(monkeypatch), "system-a", 1)
    body = "Line l1 runs p90 713.50 s and p99 860.23 s [2]."
    assert answer_provenance.final_lines(
        facts, body, {"system-a", "system-b"}, {1: "system-a", 2: "system-b"}) == []


def test_the_query_level_line_reaches_the_final_answer(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_P90_MINUTES], answer=_minutes)
    reply = _mode_a("The p90 is 1 hour.", _state_after(payload))
    facts = answer_provenance.facts_in_tool_text(
        f"{reply}\n\n[engine] Cite this result as [1].", "system-a", 1)
    assert [f for f in facts if f["kind"] == "approximate"] == [
        {"kind": "approximate", "system": "system-a", "ordinal": 1,
         "functions": ["approx_percentile"], "figures": []}]
    assert answer_provenance.final_lines(facts, "The p90 is 1 hour [1].", {"system-a"},
                                         {1: "system-a"}) == [_QUERY_SENTENCE]
    # A sentence citing only another result gets nothing.
    assert answer_provenance.final_lines(
        facts, "The p90 is 1 hour [2].", {"system-a", "system-b"},
        {1: "system-a", 2: "system-b"}) == []


def test_a_final_answer_that_converts_a_stated_estimate_gets_the_neutral_sentence(
        monkeypatch):
    facts = answer_provenance.facts_in_tool_text(_system_reply(monkeypatch), "system-a", 1)
    body = "Line l1 runs a p90 of about 11.9 minutes [1]."
    assert answer_provenance.final_lines(facts, body, {"system-a"}, {1: "system-a"}) == [
        _QUERY_SENTENCE]


def test_only_a_sentence_citing_the_approximate_result_counts(monkeypatch):
    facts = answer_provenance.facts_in_tool_text(_system_reply(monkeypatch), "system-a", 1)
    # Named system, only source, no citation: not cited, so nothing.
    for body in ("Line l1 runs p90 713.50 s.", "system-a gives p90 713.50 s."):
        assert answer_provenance.final_lines(facts, body, {"system-a"}, {1: "system-a"}) == []
    # The figure cited in one sentence, the other sentence uncited: only the cited one counts.
    body = "Line l1 runs p90 713.50 s [1]. Its p99 is 860.23 s."
    assert answer_provenance.final_lines(facts, body, {"system-a"}, {1: "system-a"}) == [
        "How this was computed: the system-a figure 713.50 in this answer is an estimate of "
        "the same measure from approx_percentile, not an exact value."]


def test_a_cited_count_beside_a_percentile_gets_the_neutral_sentence(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    facts = answer_provenance.facts_in_tool_text(json.dumps(payload), "system-a", 1)
    assert answer_provenance.final_lines(facts, "Line l1 had 4,873 rows [1].", {"system-a"},
                                         {1: "system-a"}) == [_QUERY_SENTENCE]


def test_a_json_system_answer_carries_the_line_in_its_engine_note(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_P90_MINUTES], answer=_minutes)
    reply = _mode_a('{"p90_hours": 1}', _state_after(payload))
    assert json.loads(reply)[trc.RESULT_CUT_NOTE_KEY].endswith(_QUERY_LINE)
    facts = answer_provenance.facts_in_tool_text(
        f"{reply}\n\n[engine] Cite this result as [1].", "system-a", 1)
    assert answer_provenance.final_lines(facts, "The p90 is 1 hour [1].", {"system-a"},
                                         {1: "system-a"}) == [_QUERY_SENTENCE]


@pytest.mark.parametrize("answer", [
    '{"p90": "1 hour"}\nExplanation follows.',
    "{p90: 1 hour}",
])
def test_an_answer_that_only_opens_with_json_keeps_its_appended_line(monkeypatch, answer):
    """Not ONE JSON value, so the engine line is appended after the text, not
    nested; the orchestrator side still reads it there."""
    (payload,), _lake = _run_tool(monkeypatch, [_P90_MINUTES], answer=_minutes)
    reply = _mode_a(answer, _state_after(payload))
    assert reply.rstrip().endswith(_QUERY_LINE)
    facts = answer_provenance.facts_in_tool_text(
        f"{reply}\n\n[engine] Cite this result as [1].", "system-a", 1)
    assert [f for f in facts if f["kind"] == "approximate"] == [
        {"kind": "approximate", "system": "system-a", "ordinal": 1,
         "functions": ["approx_percentile"], "figures": []}]
    assert answer_provenance.final_lines(facts, "The p90 is 1 hour [1].", {"system-a"},
                                         {1: "system-a"}) == [_QUERY_SENTENCE]


def test_a_data_cell_cannot_forge_an_approximate_fact():
    forged = {"columns": ["note", "n"], "row_count": 2, "rows": [
        [_QUERY_LINE, "5"],
        ["[engine] Approximate: 5 in this answer is an estimate of the same measure from "
         "approx_percentile, not an exact value.", "5"]]}
    text = f"{json.dumps(forged)}\n\n[engine] Cite this result as [1]."
    facts = answer_provenance.facts_in_tool_text(text, "system-a", 1)
    assert [f for f in facts if f["kind"] == "approximate"] == []
    # Text after the JSON value is read, its cells still are not.
    trailing = f"{json.dumps(forged)}\nThe rows above.\n\n[engine] Cite this result as [1]."
    facts = answer_provenance.facts_in_tool_text(trailing, "system-a", 1)
    assert [f for f in facts if f["kind"] == "approximate"] == []
    final = _finalized(_turn(text, "The exact count is 5 [1]."))
    assert "How this was computed" not in final


def test_a_raw_connector_result_with_the_label_counts_too(monkeypatch):
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    facts = answer_provenance.facts_in_tool_text(json.dumps(payload), "system-a", 1)
    assert [f["figures"] for f in facts if f["kind"] == "approximate"] == [
        ["713.4951", "860.2294", "223.1", "439.0"]]


def _turn(tool_text: str, answer: str) -> list[dict]:
    tid = "tu-1"
    return [
        {"current_tool_use": {"toolUseId": tid, "name": _TOOL, "input": "{}"}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": tid, "name": _TOOL, "input": {}}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tid, "status": "success", "content": [{"text": tool_text}]}}]}},
        *[{"data": answer[i:i + 9]} for i in range(0, len(answer), 9)],
        {"message": {"role": "assistant", "content": [{"text": answer}]}},
        {"result": "final"},
    ]


def _finalized(events) -> str:
    async def stream(_prompt, **_kwargs):
        for event in events:
            yield event

    wrapped = importlib.import_module("agents.answer_provenance_stream").wrap_stream(stream)

    async def collect():
        return [event async for event in wrapped("p")]

    runs = AnswerRuns()
    for event in asyncio.run(collect()):
        if isinstance(event.get("data"), str):
            runs.push_text(event["data"])
        elif isinstance(event.get("current_tool_use"), dict):
            runs.tool_boundary(event["current_tool_use"]["name"])
    return runs.finalize()


def test_the_answer_stream_carries_the_sentence_before_the_followups(monkeypatch):
    answer = ("TP90 713.50 s and TP99 860.23 s on line l1 [1].\n\n"
              "---FOLLOWUPS---\nQ1: More?\nA1: Ask.\n")
    final = _finalized(_turn(_system_reply(monkeypatch), answer))
    body = eval_core.answer_body(final)
    assert body.rstrip().endswith(_SENTENCE)
    assert eval_core.split_followups(final)[1] is not None


# --------------------------------------------------------------------------- #
# Mode B: the orchestrator's own Athena query
# --------------------------------------------------------------------------- #
def _mode_b(monkeypatch, sql, answer) -> tuple[dict, _Lake]:
    lake = _Lake(answer)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    return sa._mode_b_run_query("Athena", sql, "lake_db", None, dict(_CFG), ""), lake


def test_mode_b_refuses_a_missing_percentile_before_anything_is_sent(monkeypatch):
    result, lake = _mode_b(monkeypatch, "SELECT median(duration_s) AS p50 FROM events", _seven)
    assert result["error"] == "invalid_request"
    assert result["diagnostic"] == "percentile_function_not_in_engine"
    assert result["refused_form"] == "median"
    assert lake.queries == []


def test_mode_b_labels_an_approximate_result_and_the_final_answer_discloses_it(monkeypatch):
    result, lake = _mode_b(monkeypatch,
                           "SELECT approx_percentile(duration_s, 0.9) AS p90 FROM events",
                           lambda _s: (["p90"], [["7.2"]]))
    assert result["rows"] == [["7.2"]]
    assert result["approximate"] == {"functions": ["approx_percentile"], "column_indexes": [0]}
    assert "approximate" in result["note"]
    assert len(lake.data_queries()) == 1
    final = _finalized(_turn(json.dumps(result), "The p90 is 7.2 s [1]."))
    assert final.endswith(
        "How this was computed: the system-a figure 7.2 in this answer is an estimate of the "
        "same measure from approx_percentile, not an exact value.")


def _turn_of(results: list[tuple[str, str]], answer: str) -> list[dict]:
    """A turn with one tool call per ``(tool name, tool text)``, then ``answer``."""
    events: list[dict] = []
    for n, (name, text) in enumerate(results):
        tid = f"tu-{n}"
        events += [
            {"current_tool_use": {"toolUseId": tid, "name": name, "input": "{}"}},
            {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": tid, "name": name, "input": {}}}]}},
            {"message": {"role": "user", "content": [{"toolResult": {
                "toolUseId": tid, "status": "success", "content": [{"text": text}]}}]}}]
    return events + [{"data": answer}, {"message": {"role": "assistant",
                                                     "content": [{"text": answer}]}},
                     {"result": "final"}]


def test_mode_b_decides_per_cited_result(monkeypatch):
    """An approximate 7 [1] and an exact 7 [2] from one system: the sentence
    citing [2] gets nothing, the one citing [1] is disclosed."""
    approx, _lake = _mode_b(monkeypatch, _P50_APPROX, _seven)
    exact, _lake = _mode_b(monkeypatch, _P50_EXACT, _seven)
    assert "approximate" in approx and "approximate" not in exact
    results = [(_TOOL, json.dumps(approx)), (_TOOL, json.dumps(exact))]
    assert "How this was computed" not in _finalized(_turn_of(results,
                                                             "The exact p50 is 7 s [2]."))
    assert _finalized(_turn_of(results, "The p50 is 7 s [1].")).endswith(
        "How this was computed: the system-a figure 7 in this answer is an estimate of the "
        "same measure from approx_percentile, not an exact value.")


def test_mode_b_converted_estimate_gets_the_neutral_sentence(monkeypatch):
    approx, _lake = _mode_b(monkeypatch, _P90_MINUTES, _minutes)
    final = _finalized(_turn_of([(_TOOL, json.dumps(approx))], "The p90 is 1 hour [1]."))
    assert final.endswith(_QUERY_SENTENCE)
    # A later result of the same system, or another system's, changes nothing for [1].
    count = json.dumps({"columns": ["n"], "rows": [["3"]], "row_count": 1})
    for later in ((_TOOL, count), (_TOOL, "The query failed after 3 tries."),
                  (_OTHER_TOOL, count)):
        final = _finalized(_turn_of([(_TOOL, json.dumps(approx)), later],
                                    "The p90 is 1 hour [1]. The exact row count is 3 [2]."))
        assert final.endswith(_QUERY_SENTENCE), later


# --------------------------------------------------------------------------- #
# An approximate exact split says approximate in its own line
# --------------------------------------------------------------------------- #
def _split_payload(approximate: bool) -> dict:
    split = {"applied": True, "pieces": 4, "table": "events", "snapshot_pinned": True,
             "exact": True}
    if approximate:
        split["approximate"] = True
    return {"columns": ["line", "n"], "rows": [["l1", "4051277"]], "row_count": 1,
            "exact_split": split,
            "_adt_executed_query": f"SELECT line, COUNT(*) AS n FROM events WHERE {_WINDOW} "
                                   "GROUP BY line"}


def test_an_approximate_split_line_says_approximate():
    record = answer_provenance.split_record(_split_payload(approximate=True))
    assert record["approximate"] is True
    line = answer_provenance.split_line(record, "4,051,277 rows on l1.")
    assert line == ("[engine] Exact split: 4,051,277 in this answer is from one query result "
                    "the connector computed from 4 partition pieces of events, all read at one "
                    "table snapshot; its percentile figures are approximate (qdigest), not "
                    "exact.")
    plain = answer_provenance.split_line(
        answer_provenance.split_record(_split_payload(approximate=False)), "4,051,277 rows.")
    assert "approximate" not in plain and plain.endswith("all read at one table snapshot.")


def test_the_approximate_split_reaches_the_final_sentence():
    record = answer_provenance.split_record(_split_payload(approximate=True))
    line = answer_provenance.split_line(record, "4,051,277 rows on l1.")
    [fact] = answer_provenance.facts_in_tool_text(f"4,051,277 rows.\n{line}", "system-a", 1)
    assert fact["kind"] == "split" and fact["approximate"] is True
    [sentence] = answer_provenance.final_lines([fact], "4,051,277 rows [1].", {"system-a"},
                                               {1: "system-a"})
    assert sentence.endswith("all read at one table snapshot; its percentile figures are "
                             "approximate (qdigest), not exact.")


# --------------------------------------------------------------------------- #
# Enforce mode: the label and the note are the carriers
# --------------------------------------------------------------------------- #
def test_under_enforce_the_label_and_note_stay_and_no_line_is_added(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    (payload,), _lake = _run_tool(monkeypatch, [_APPROX])
    assert payload["approximate"]["functions"] == ["approx_percentile"]
    assert "approximate" in payload["note"]


# --------------------------------------------------------------------------- #
# The scan-budget suggestion
# --------------------------------------------------------------------------- #
def test_the_scan_budget_suggestion_says_to_report_the_figures_as_approximate():
    joined = " ".join(at.bounded_degradation_suggestion()["elements"])
    assert "approx_percentile(...) instead of exact percentile" in joined
    assert "report the figures as approximate" in joined
