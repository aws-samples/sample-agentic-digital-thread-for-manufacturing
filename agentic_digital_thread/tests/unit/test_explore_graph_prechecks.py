# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``explore_graph`` refuses ``=~`` and dotted aliases before submit.

Evidence (two deployed rehearsal stacks, orchestrator runtime logs,
2026-10-04): Neptune Analytics rejected ``WHERE ... c.name =~ '(?i).*lot.*'``
("RegexMatch is not supported in this release", ~4 times across two
accounts) and ``RETURN ... AS md.id`` ("Invalid input '.': expected an
identifier character"). Each cost a wasted call and a retry. Two more
pre-submit UNION refusals came from branches returning UNALIASED columns
(``[a.name, a.asset_type, ...]``), so the UNION error now asks for the same
PLAIN alias in every branch.

The checks share ``cypher_union_check._mask`` (text inside quotes never
matches), refuse only confident matches, fail open on anything uncertain,
and never rewrite the query. Fully offline.
"""
import json
from pathlib import Path

import pytest

import agents.graph_tools as graph_tools
from agents import query_intelligence as qi
from agents.cypher_union_check import dotted_alias, uses_regex_match
from agents.tool_result_summary import summarize_tool_result

_LIVE_REGEX = (
    "MATCH (c:Concept) WHERE c.id = 'lot' OR c.name =~ '(?i).*(lot|supplier|material).*' "
    "RETURN c.id AS id, c.name AS name"
)
_LIVE_DOTTED = "MATCH (m:AssetModel) RETURN m.id AS md.id, m.name AS name"
_LIVE_UNION = (
    "MATCH (a:Asset) RETURN a.name, a.asset_type "
    "UNION MATCH (m:AssetModel) RETURN m.name, m.model_type"
)
_FIXTURE = (Path(__file__).resolve().parents[2]
            / "frontend" / "src" / "test" / "fixtures"
            / "tool-result-outcome-cases.json")


@pytest.fixture
def submitted(monkeypatch):
    """The model's queries that reached the engine. The reads that check a
    result for switched-off JoinPaths and IdentityRules
    (agents/explore_inactive_joins.py) are the tool's own and are left out."""
    from agents.explore_inactive_joins import OWN_READS
    own = set(OWN_READS)
    calls = []

    def checked(q, *a, **k):
        if q in own:
            return []
        calls.append(q)
        return [{"x": 1}]

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    monkeypatch.setattr(graph_tools, "neptune_query",
                        lambda q, *a, **k: calls.append(q) or [])
    return calls


# --- =~ -------------------------------------------------------------------

@pytest.mark.parametrize("cypher", [
    _LIVE_REGEX,
    "MATCH (n) WHERE n.name=~$pattern RETURN n.name AS name",
    "match (n) where toLower(n.name) =~ \"l3.*\" return n.id as id",
    "CALL { MATCH (n) WHERE n.x =~ 'a.*' RETURN n } RETURN n.id AS id",
])
def test_regex_match_operator_is_detected(cypher):
    assert uses_regex_match(cypher) is True


@pytest.mark.parametrize("cypher", [
    # Inside string literals and a backtick-quoted name: not the operator.
    "MATCH (n {note: 'use =~ here'}) RETURN n.name AS name",
    'MATCH (n) WHERE n.note = "a =~ b" RETURN n.name AS name',
    "MATCH (n) RETURN n.`a=~b` AS x",
    # Comments: fail open (comment text is not masked).
    "MATCH (n) // n.name =~ 'x'\nRETURN n.name AS name",
    "MATCH (n) /* =~ */ RETURN n.name AS name",
    "MATCH (n) WHERE n.name =~ 'x' // trailing\nRETURN n.name AS name",
    # Unterminated quote, unbalanced brackets.
    "MATCH (n) WHERE n.name =~ 'oops RETURN n",
    "MATCH (n WHERE n.name =~ 'x' RETURN n",
    # The supported spelling.
    "MATCH (c:Concept) WHERE toLower(c.name) CONTAINS 'lot' RETURN c.id AS id",
    None,
    42,
])
def test_regex_match_absent_or_uncertain_is_not_refused(cypher):
    assert uses_regex_match(cypher) is False


def test_regex_query_is_refused_before_submit(submitted):
    raw = graph_tools.explore_graph(_LIVE_REGEX)
    out = json.loads(raw)
    assert submitted == [], "a query using =~ must not reach the engine"
    assert list(out) == ["error", "results", "query_failed", "note"]
    assert out["error"] == (
        "Graph query failed (regular-expression match is not supported, not run): "
        "use toLower(x) CONTAINS 'text', STARTS WITH or ENDS WITH instead of =~."
    )
    assert out["results"] == [] and out["query_failed"] is True
    assert out["note"] == graph_tools._REJECTED_NOTE
    assert raw.startswith('{"error": "Graph query failed')
    assert qi.failure_marker(raw) == ("error", "query failed")


def test_regex_inside_a_literal_is_submitted_unchanged(submitted):
    query = "MATCH (n {note: 'use =~ here'}) RETURN n.name AS name"
    assert json.loads(graph_tools.explore_graph(query)) == {"results": [{"x": 1}]}
    assert submitted == [query]


# --- dotted alias ---------------------------------------------------------

@pytest.mark.parametrize("cypher, expected", [
    (_LIVE_DOTTED, "AS md.id"),
    ("MATCH (m) WITH m.id AS md.id RETURN 1 AS one", "AS md.id"),
    ("match (m) return m.id as md.id", "as md.id"),
    ("MATCH (m) RETURN m.id AS md . id", "AS md . id"),
    ("MATCH (m) RETURN DISTINCT m.id AS id, m.name AS md.name ORDER BY id",
     "AS md.name"),
    ("CALL { MATCH (m) RETURN m.id AS md.id } RETURN 1 AS one", "AS md.id"),
    # Keywords inside the projection do not hide its RETURN.
    ("MATCH (m) RETURN [x IN m.tags WHERE x <> '' | x] AS md.tags", "AS md.tags"),
    ("MATCH (m) RETURN m.name STARTS WITH 'L3' AS md.is_l3", "AS md.is_l3"),
    ("MATCH (m) WHERE EXISTS { MATCH (m)-->(x) RETURN x } RETURN m.id AS md.id",
     "AS md.id"),
])
def test_dotted_alias_in_return_or_with_is_detected(cypher, expected):
    assert dotted_alias(cypher) == expected


@pytest.mark.parametrize("cypher", [
    "MATCH (m) RETURN m.id AS md_id, m.name AS name",
    # A backtick-quoted alias may contain a dot.
    "MATCH (m) RETURN m.id AS `md.id`",
    # Inside a string literal.
    "MATCH (n {note: 'RETURN x AS md.id'}) RETURN n.id AS id",
    # A property named ``as`` is not the keyword.
    "MATCH (x) RETURN x.as AS value, x.as.id AS nested",
    "MATCH (x) RETURN x.as   y.z",
    # Aliases owned by UNWIND / YIELD are left to the engine.
    "UNWIND [1, 2] AS md.id RETURN 1 AS one",
    "MATCH (m) WITH m UNWIND m.tags AS t.x RETURN t",
    "CALL db.labels() YIELD label AS l.x RETURN 1 AS one",
    # Comments, unterminated quote, unbalanced brackets: fail open.
    "MATCH (m) RETURN m.id AS md.id // why",
    "MATCH (m {n: 'oops}) RETURN m.id AS md.id",
    "MATCH (m RETURN m.id AS md.id",
    None,
])
def test_plain_or_uncertain_alias_is_not_refused(cypher):
    assert dotted_alias(cypher) is None


def test_dotted_alias_is_refused_before_submit(submitted):
    raw = graph_tools.explore_graph(_LIVE_DOTTED)
    out = json.loads(raw)
    assert submitted == [], "a dotted alias must not reach the engine"
    assert list(out) == ["error", "results", "query_failed", "note"]
    assert out["error"] == (
        "Graph query failed (dotted alias, not run): an alias must be a plain name, "
        "e.g. AS md_id, not AS md.id."
    )
    assert out["results"] == [] and out["query_failed"] is True
    assert out["note"] == graph_tools._REJECTED_NOTE
    assert qi.failure_marker(raw) == ("error", "query failed")


def test_backtick_alias_is_submitted_unchanged(submitted):
    query = "MATCH (m) RETURN m.id AS `md.id`"
    assert json.loads(graph_tools.explore_graph(query)) == {"results": [{"x": 1}]}
    assert submitted == [query]


# --- UNION hint, summaries, fixture ---------------------------------------

def test_union_refusal_asks_for_the_same_plain_alias(submitted):
    out = json.loads(graph_tools.explore_graph(_LIVE_UNION))
    assert submitted == []
    assert "branch 1 returns [a.name, a.asset_type]" in out["error"]
    assert "same column names in the same order" in out["error"]
    assert ("alias every column with the same plain name in every branch "
            "(e.g. RETURN a.name AS name in each branch)") in out["error"]
    assert out["union_columns"] == [["a.name", "a.asset_type"], ["m.name", "m.model_type"]]


def test_regex_refusal_wins_over_the_union_check(submitted):
    query = ("MATCH (a) WHERE a.n =~ 'x' RETURN a.x AS x "
             "UNION MATCH (b) RETURN b.y AS y")
    out = json.loads(graph_tools.explore_graph(query))
    assert out["error"].startswith("Graph query failed (regular-expression")
    assert "union_columns" not in out and submitted == []


@pytest.mark.parametrize("cypher, head", [
    (_LIVE_REGEX, "Error: Graph query failed (regular-expression match is not supported"),
    (_LIVE_DOTTED, "Error: Graph query failed (dotted alias, not run)"),
])
def test_refusal_summarizes_as_failed_never_no_matching(cypher, head):
    raw = graph_tools.explore_graph(cypher)
    summary = summarize_tool_result("explore_graph", {"content": [{"text": raw}]})
    assert summary.startswith(head)
    assert "No matching" not in summary
    assert qi.is_failed_result(summary) is True
    assert qi.is_failed_result(raw) is True


@pytest.mark.parametrize("case_id, cypher", [
    ("explore-graph-regex-refused-before-submit", _LIVE_REGEX),
    ("explore-graph-dotted-alias-refused-before-submit", _LIVE_DOTTED),
    ("explore-graph-union-refused-before-submit", _LIVE_UNION),
])
def test_shared_outcome_fixture_pins_the_current_envelopes(case_id, cypher):
    """The UI and server parity tests read these fixture cases; keep their
    text equal to what explore_graph returns today."""
    cases = {c["id"]: c for c in json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]}
    case = cases[case_id]
    assert case["text"] == graph_tools.explore_graph(cypher)
    assert case["ui"] == "failed" and case["server"] is True
