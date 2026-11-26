# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A SQL system agent's answer carries the WHERE filters its queries ran with.

Measured on an analytics deployment: asked for an exact count over a window,
a SQL system agent hit the scan limit, added a bound on another date column on
its own to get under it, and answered "where the completion time falls
between ..." as if only the asked window applied. The executed SQL was
captured (``_adt_executed_query``), but only the agent's prose crosses the
``query_<id>`` boundary, so the orchestrator could not see the added bound and
stated the figure as exact.

The engine half of the fix: ``_run_mode_a_query`` appends ONE fixed engine
line listing the WHERE conditions of this turn's queries that returned
results, parsed from the trusted executed SQL, bounded in length. The
orchestrator prompt tells it to disclose a bound the question did not ask for.
General for every SQL engine; no per-system logic. Fully offline.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

import agents.chat_client as chat_client
import agents.orchestrator as orch
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import query_intelligence as qi
from agents.response_integrity_query import bounded_query_result, strict_json_dumps


def _ef():
    return importlib.import_module("agents.executed_filters")


# --------------------------------------------------------------------------- #
# helpers (the same capture-hook path the cut-line tests drive)
# --------------------------------------------------------------------------- #
def _tool_result(sql: str, rows: int = 2) -> dict:
    body = {**bounded_query_result(["a", "n"], [[f"v{i}", i] for i in range(rows)], False),
            "_adt_executed_query": sql}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _event(state: dict, result):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "query_public", "input": {"sql": "SELECT 1"}},
        invocation_state=state, result=result, exception=None, retry=False,
        agent=SimpleNamespace(messages=[]),
    )


def _state_after(*results) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result in results:
        capture._on_after_tool_call(_event(state, result))
    return state


def _run_query(answer: str, state: dict) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _agent, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", "question"))


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


_ASKED = ("SELECT count(*) AS n FROM table_x WHERE done_at >= TIMESTAMP '2026-01-05 00:00:00' "
          "AND done_at < TIMESTAMP '2026-01-12 00:00:00' AND state = 'done'")
_ADDED = _ASKED + " AND created_at >= DATE '2025-12-20'"


# --------------------------------------------------------------------------- #
# 1. WHERE clauses parsed from executed SQL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql, expected", [
    ("SELECT a FROM t WHERE x = 1 AND y > 2 GROUP BY a", ["x = 1 AND y > 2"]),
    ("SELECT a FROM t WHERE x = 1 ORDER BY a LIMIT 5", ["x = 1"]),
    ("WITH r AS (SELECT * FROM t WHERE d >= DATE '2026-01-01') "
     "SELECT count(*) FROM r WHERE k = 'a'", ["d >= DATE '2026-01-01'", "k = 'a'"]),
    ("SELECT * FROM (SELECT a FROM t WHERE b = 2) s WHERE a > 1 ORDER BY a",
     ["b = 2", "a > 1"]),
    # A subquery inside a WHERE stays part of that WHERE, once.
    ("SELECT a FROM t WHERE a IN (SELECT a FROM u WHERE v = 'x') AND c = 3",
     ["a IN (SELECT a FROM u WHERE v = 'x') AND c = 3"]),
    ("SELECT a FROM t WHERE x = 1 UNION ALL SELECT a FROM u WHERE y = 2 LIMIT 9",
     ["x = 1", "y = 2"]),
    # Keywords inside a string literal do not end the clause.
    ("SELECT a FROM t WHERE note = 'x GROUP BY y' AND n = 1", ["note = 'x GROUP BY y' AND n = 1"]),
    # Comments are dropped and layout collapses to single spaces.
    ("SELECT a FROM t WHERE\n  x =\t1 -- why\n  AND /* note */ y = 2", ["x = 1 AND y = 2"]),
    ('SELECT a FROM t WHERE "a.b" = \'v\' AND f(c, 2) > 0', ['"a.b" = \'v\' AND f(c, 2) > 0']),
    ("SELECT a FROM t WHERE x = 1;", ["x = 1"]),
    ("SELECT a, count(*) FROM t GROUP BY a", []),
    ("", []),
])
def test_where_clauses_follow_the_query_structure(sql, expected):
    assert _ef().where_clauses(sql) == expected


def test_a_window_function_order_by_does_not_end_the_clause():
    sql = ("SELECT * FROM (SELECT a, row_number() OVER (ORDER BY b) AS r FROM t "
           "WHERE x = 1 AND y IN (1, 2)) s WHERE r <= 3")
    assert _ef().where_clauses(sql) == ["x = 1 AND y IN (1, 2)", "r <= 3"]




def test_where_clause_conjuncts_split_only_at_top_level_and():
    ef = _ef()
    assert ef.where_conjuncts(
        "SELECT a FROM t WHERE d BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
        "AND (x = 1 OR y = 2) AND CASE WHEN p AND q THEN 1 ELSE 0 END = 1 "
        "AND f(a AND b) AND k = 'and'") == [[
            "d BETWEEN DATE '2026-01-01' AND DATE '2026-01-07'",
            "(x = 1 OR y = 2)",
            "CASE WHEN p AND q THEN 1 ELSE 0 END = 1",
            "f(a AND b)",
            "k = 'and'",
        ]]


# --------------------------------------------------------------------------- #
# 2. The line
# --------------------------------------------------------------------------- #
def _stmt(sql: str, rows=2) -> dict:
    return {"query_text": sql, "row_count": rows}


_PREFIX = ("[engine] WHERE filters of this turn's queries that returned results, in run "
           "order, as executed (a query with no WHERE is not listed): ")


def test_no_query_with_a_where_clause_means_no_line():
    ef = _ef()
    assert ef.executed_filters_line([]) == ""
    assert ef.executed_filters_line([_stmt("SELECT a FROM t"),
                                     _stmt("SELECT count(*) FROM u")]) == ""


def test_one_query_renders_exactly_with_its_ordinal_and_row_count():
    line = _ef().executed_filters_line([_stmt(_ADDED, 1204)])
    assert line == _PREFIX + (
        "Q1 (1,204 rows): WHERE done_at >= TIMESTAMP '2026-01-05 00:00:00' AND done_at < "
        "TIMESTAMP '2026-01-12 00:00:00' AND state = 'done' AND created_at >= DATE "
        "'2025-12-20'")


def test_ordinals_follow_run_order_and_each_query_carries_its_row_count():
    ef = _ef()
    cte = ("WITH r AS (SELECT * FROM t WHERE d >= DATE '2026-01-01') "
           "SELECT count(*) FROM r WHERE k = 'a'")
    line = ef.executed_filters_line([
        _stmt(_ASKED, 1), _stmt("SELECT a FROM t", 3), _stmt(_ASKED, 0), _stmt(cte, None)])
    assert line == _PREFIX + (
        "Q1 (1 row): WHERE done_at >= TIMESTAMP '2026-01-05 00:00:00' AND done_at < "
        "TIMESTAMP '2026-01-12 00:00:00' AND state = 'done' | "
        "Q3 (0 rows): WHERE done_at >= TIMESTAMP '2026-01-05 00:00:00' AND done_at < "
        "TIMESTAMP '2026-01-12 00:00:00' AND state = 'done' | "
        "Q4 (row count not known): WHERE d >= DATE '2026-01-01' / WHERE k = 'a'")


def test_a_zero_row_query_is_labelled_not_dropped():
    line = _ef().executed_filters_line([_stmt(_ADDED, 0)])
    assert "Q1 (0 rows): WHERE " in line


def test_bare_query_texts_are_still_accepted_without_a_count():
    line = _ef().executed_filters_line([_ASKED])
    assert line.startswith(_PREFIX + "Q1 (row count not known): WHERE done_at >= ")


def test_metadata_reads_are_not_listed_or_numbered():
    ef = _ef()
    meta = "SELECT column_name FROM information_schema.columns WHERE table_name = 't'"
    assert ef.executed_filters_line([_stmt(meta)]) == ""
    assert "Q1 (2 rows): WHERE state" in ef.executed_filters_line(
        [_stmt(meta), _stmt("SELECT a FROM t WHERE state = 'done'")])


@pytest.mark.parametrize("probe", [
    pytest.param("SELECT relname FROM pg_catalog.pg_class WHERE relkind = 'r'", id="qualified"),
    pytest.param("SELECT attname FROM pg_attribute WHERE attnum > 0", id="unqualified-pg"),
])
def test_postgres_catalog_probes_are_not_listed_or_numbered(probe):
    """The no-data line's catalog test (provenance.is_discovery_statement) is
    the one this line uses too: a catalog probe's WHERE selects catalog rows,
    not the user's data, so it is no scope limit to disclose."""
    ef = _ef()
    assert ef.executed_filters_line([_stmt(probe)]) == ""
    assert "Q1 (0 rows): WHERE k = 'x'" in ef.executed_filters_line(
        [_stmt(probe), _stmt("SELECT a FROM runs WHERE k = 'x'", 0)])


def test_a_catalog_probe_beside_an_empty_lookup_keeps_only_the_data_filter():
    """End to end with the no-data line: the data query's filter is listed,
    the probe's is not."""
    probe = "SELECT relname FROM pg_catalog.pg_class WHERE relkind = 'r'"
    state = _state_after(_tool_result(probe, rows=2),
                         _tool_result("SELECT a FROM runs WHERE k = 'x'", rows=0))
    answer = _run_query("No record matched.", state)
    assert "Q1 (0 rows): WHERE k = 'x'" in answer
    assert "relkind" not in answer
    assert "No query this call ran returned data" in answer


_IDS = ", ".join(f"'cell_{i:03d}'" for i in range(40))
_LATE = (f"SELECT count(*) FROM table_x WHERE cell IN ({_IDS}) "
         "AND complete_date >= TIMESTAMP '2026-01-05 00:00:00' "
         "AND complete_date < TIMESTAMP '2026-01-12 00:00:00' "
         "AND created_at >= DATE '2025-12-20'")


def test_a_late_predicate_past_the_clause_cap_survives():
    ef = _ef()
    assert _LATE.index("created_at") > ef.MAX_CLAUSE_CHARS
    line = ef.executed_filters_line([_stmt(_LATE, 1)])
    assert "cell IN (<40 values>)" in line
    assert "complete_date >= TIMESTAMP '2026-01-05 00:00:00'" in line
    assert "complete_date < TIMESTAMP '2026-01-12 00:00:00'" in line
    assert line.endswith("AND created_at >= DATE '2025-12-20'")


def test_a_short_in_list_is_kept_verbatim():
    line = _ef().executed_filters_line([_stmt("SELECT a FROM t WHERE c IN ('a', 'b') AND n > 1")])
    assert line.endswith("WHERE c IN ('a', 'b') AND n > 1")


def test_a_long_literal_is_clipped_and_the_predicate_after_it_survives():
    ef = _ef()
    sql = ("SELECT a FROM t WHERE note LIKE '%" + "z" * 400 + "%' "
           "AND created_at >= DATE '2025-12-20'")
    line = ef.executed_filters_line([_stmt(sql)])
    assert "note LIKE '%zzz" in line and "...'" in line
    assert line.endswith("AND created_at >= DATE '2025-12-20'")
    assert len(line) < len(_PREFIX) + ef.MAX_CLAUSE_CHARS


def test_every_predicate_keeps_its_column_and_operator_under_the_clause_cap():
    ef = _ef()
    long_preds = [f"coalesce(col_{k}, '{'x' * 30}') <> '{'y' * 20}'" for k in range(6)]
    sql = ("SELECT a FROM t WHERE " + " AND ".join(long_preds)
           + " AND created_at >= DATE '2025-12-20'")
    clause = ef.executed_filters_line([_stmt(sql)]).split(": WHERE ", 1)[1]
    assert len(clause) <= ef.MAX_CLAUSE_CHARS
    for k in range(6):
        assert f"coalesce(col_{k}" in clause
    assert clause.count("<>") == 6
    assert clause.endswith("created_at >= DATE '2025-12-20'")


def test_many_predicates_keep_every_column_and_operator_within_the_line_cap():
    ef = _ef()
    preds = [f"some_long_column_name_{k:02d} >= TIMESTAMP '2026-01-05 00:00:00'"
             for k in range(20)]
    line = ef.executed_filters_line([_stmt("SELECT a FROM t WHERE " + " AND ".join(preds))])
    assert len(line) <= ef.MAX_LINE_CHARS
    for k in range(20):
        assert f"some_long_column_name_{k:02d} >=" in line


def test_the_line_stays_bounded_and_keeps_the_most_recent_queries():
    ef = _ef()
    many = [_stmt(f"SELECT a FROM t WHERE k = 'v{i:03d}' AND {'x' * 200} > 0") for i in range(40)]
    line = ef.executed_filters_line(many)
    assert len(line) <= ef.MAX_LINE_CHARS
    assert "k = 'v039'" in line and "k = 'v000'" not in line
    assert "Q40 (2 rows)" in line
    assert "earlier quer" in line


def test_the_line_never_carries_a_newline():
    line = _ef().executed_filters_line([_stmt("SELECT a FROM t WHERE note = 'two\nlines'")])
    assert "\n" not in line and "\r" not in line


def test_one_oversized_query_is_cut_with_an_explicit_incomplete_marker():
    """Ten long CTE WHERE clauses plus an outer bound: one query alone is over
    the line cap, so the cut must say the filters are incomplete."""
    ef = _ef()
    ctes = ", ".join(
        f"c{i} AS (SELECT * FROM t WHERE "
        + " AND ".join(f"some_long_column_name_{i}_{k:02d} >= TIMESTAMP '2026-01-05 00:00:00'"
                       for k in range(8)) + ")"
        for i in range(10))
    sql = f"WITH {ctes} SELECT count(*) FROM c9 WHERE hidden_added_bound >= DATE '2025-01-01'"
    line = ef.executed_filters_line([_stmt(sql)])
    assert len(line) <= ef.MAX_LINE_CHARS
    assert "hidden_added_bound" not in line
    assert line.endswith(ef.INCOMPLETE_MARK)


def test_a_line_within_the_cap_carries_no_incomplete_marker():
    ef = _ef()
    line = ef.executed_filters_line([_stmt("SELECT a FROM t WHERE k = 1 AND d >= DATE '2026-01-01'")])
    assert "incomplete" not in line


# --------------------------------------------------------------------------- #
# 3. _run_mode_a_query carries it
# --------------------------------------------------------------------------- #
def test_an_added_bound_reaches_the_orchestrator_in_the_answer():
    state = _state_after(_tool_result(_ADDED))
    answer = _run_query("The answer is 1,204.", state)
    assert "created_at >= DATE '2025-12-20'" in answer
    assert answer == "The answer is 1,204.\n\n" + _ef().executed_filters_line([_stmt(_ADDED, 2)])
    assert answer.count("[engine]") == 1


def test_the_captured_row_count_and_ordinal_reach_the_answer():
    state = _state_after(_tool_result("SELECT a, n FROM t", rows=5),
                         _tool_result(_LATE, rows=0), _tool_result(_ADDED, rows=3))
    answer = _run_query("Answer.", state)
    assert "Q2 (0 rows): WHERE cell IN (<40 values>)" in answer
    assert "Q3 (3 rows): WHERE done_at >= " in answer
    assert "Q1 " not in answer


def test_a_query_the_system_refused_is_never_listed():
    refused = {"toolUseId": "t-3", "status": "success", "content": [{"text": json.dumps({
        "error": "Athena query cancelled: the engine scan limit was exceeded",
        "error_type": "scan_budget_exceeded"})}]}
    state = _state_after(refused, _tool_result(_ADDED, rows=1))
    answer = _run_query("Answer.", state)
    assert "Q1 (1 row): WHERE done_at >= " in answer


def test_annotate_mode_carries_it_too(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    answer = _run_query("Answer.", _state_after(_tool_result(_ADDED)))
    assert answer.endswith(_ef().executed_filters_line([_stmt(_ADDED, 2)]))


def test_enforce_mode_envelope_is_untouched(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    envelope = json.dumps({"answer": "gated", "claims": [], "evidence": []})
    assert _run_query(envelope, _state_after(_tool_result(_ADDED))) == envelope


def test_a_query_without_a_where_clause_leaves_the_answer_byte_identical():
    text = "Three rows.\n\n| a | n |\n|---|---|\n| v0 | 0 |\n"
    assert _run_query(text, _state_after(_tool_result("SELECT a, n FROM t ORDER BY n"))) == text


def test_a_json_answer_carries_it_as_the_reserved_member():
    answer = _run_query(json.dumps({"total": 1}), _state_after(_tool_result(_ADDED)))
    parsed = json.loads(answer)
    assert parsed["total"] == 1
    assert parsed[trc.RESULT_CUT_NOTE_KEY] == _ef().executed_filters_line([_stmt(_ADDED, 2)])


def test_it_follows_the_cut_line_after_a_blank_line():
    cut = {"toolUseId": "t-2", "status": "success", "content": [{"text": strict_json_dumps({
        **bounded_query_result(["a", "n"], [[f"v{i}", i] for i in range(500)], True),
        "_adt_executed_query": _ADDED})}]}
    state = _state_after(cut)
    answer = _run_query("Answer.", state)
    cut_line = trc.result_cut_line(state)
    assert cut_line
    assert answer == (f"Answer.\n\n{cut_line}\n\n"
                      f"{_ef().executed_filters_line([_stmt(_ADDED, 500)])}")


def test_a_failure_building_the_line_leaves_the_answer_unchanged(monkeypatch):
    ef = _ef()

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ef, "executed_filters_line", _boom)
    assert _run_query("Answer.", _state_after(_tool_result(_ADDED))) == "Answer."


# --------------------------------------------------------------------------- #
# 4. It reads as data, never as a failure
# --------------------------------------------------------------------------- #
def test_a_short_answer_with_the_line_is_neither_failed_nor_empty():
    text = "The count is 1,204.\n\n" + _ef().executed_filters_line([_stmt(_ADDED, 0)])
    assert qi.failure_marker(text) is None and not qi.is_failed_result(text)
    assert qi.count_wasted_calls([text]) == 0
    hits = [c for c, pattern in chat_client._ERROR_TEXT_CLASSIFIERS if pattern.search(text)]
    assert hits == []


# --------------------------------------------------------------------------- #
# 5. The orchestrator is told what to do with it
# --------------------------------------------------------------------------- #
def _filters_rule() -> str:
    prompt = orch.ORCHESTRATOR_PROMPT
    assert "EXECUTED FILTERS:" in prompt
    rule = prompt[prompt.index("EXECUTED FILTERS:"):]
    return rule[:rule.index("\n\n") if "\n\n" in rule else len(rule)]


def test_the_orchestrator_prompt_says_to_disclose_a_bound_not_asked_for():
    rule = _filters_rule()
    assert "did not ask for" in rule
    assert "\u2014" not in rule and "\u2013" not in rule


def test_the_orchestrator_ties_a_filter_to_a_figure_only_when_the_answer_does():
    rule = " ".join(_filters_rule().split())
    assert "row count" in rule
    assert "only when the system's answer ties that query to the figure" in rule
    assert "otherwise say the system's results this turn were limited by it" in rule
    assert "filters incomplete" in rule and "never treat it as the whole scope" in rule
