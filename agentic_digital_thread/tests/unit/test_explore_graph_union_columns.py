# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``explore_graph`` refuses a UNION whose branches RETURN different names.

Evidence (a fresh workshop account, 2026-10-03): the orchestrator wrote
openCypher UNIONs whose branches returned different column names; Neptune
rejected them ("All sub queries in an UNION must have the same column names")
without saying which names differ. The tool now reads each top-level branch's
RETURN list before submit and, ONLY when the read is confident and the names
differ, returns a failure envelope naming every branch's columns. Anything
uncertain is submitted unchanged (fail open); the query is never rewritten.
Fully offline.
"""
import json

import pytest

import agents.graph_tools as graph_tools
from agents import query_intelligence as qi
from agents.cypher_union_check import union_column_mismatch
from agents.tool_result_summary import summarize_tool_result


_MISMATCHED = (
    "MATCH (s:System) RETURN s.id AS id, s.name AS name "
    "UNION MATCH (c:Concept) RETURN c.id AS concept_id, c.name AS name"
)


@pytest.mark.parametrize("cypher, expected", [
    (_MISMATCHED, [["id", "name"], ["concept_id", "name"]]),
    ("MATCH (a:Asset) RETURN a.name UNION ALL MATCH (m:AssetModel) RETURN m.name",
     [["a.name"], ["m.name"]]),
    ("MATCH (s:System) RETURN DISTINCT s.id AS id ORDER BY id LIMIT 5 "
     "union match (p:Plant) return p.id as plant",
     [["id"], ["plant"]]),
    ("MATCH (a) RETURN a.x AS x UNION MATCH (b) RETURN b.x AS x "
     "UNION MATCH (c) RETURN c.y AS y",
     [["x"], ["x"], ["y"]]),
])
def test_confident_mismatch_names_every_branch(cypher, expected):
    assert union_column_mismatch(cypher) == expected


@pytest.mark.parametrize("cypher", [
    # Aligned branches (aliases, plain items, DISTINCT/ORDER BY tails).
    "MATCH (s:System) RETURN s.id AS id, s.name AS name "
    "UNION MATCH (c:Concept) RETURN c.id AS id, c.label AS name",
    "MATCH (n:Asset) RETURN n UNION MATCH (n:AssetModel) RETURN n",
    "MATCH (s) WITH s, count(*) AS c RETURN s.id AS id, c AS n ORDER BY n DESC "
    "UNION ALL MATCH (p) RETURN p.id AS id, 0 AS n",
    # Same names, different order: the engine decides.
    "MATCH (a) RETURN a.x AS x, a.y AS y UNION MATCH (b) RETURN b.y AS y, b.x AS x",
    # No UNION at all.
    "MATCH (s:System) RETURN s.id AS id",
])
def test_aligned_or_single_query_passes(cypher):
    assert union_column_mismatch(cypher) is None


@pytest.mark.parametrize("cypher", [
    # The word UNION only inside a string literal.
    "MATCH (s:System {name: 'x UNION y'}) RETURN s.id AS id",
    # Comments.
    "MATCH (a) RETURN a.x AS x // note\nUNION MATCH (b) RETURN b.y AS y",
    "MATCH (a) RETURN a.x AS x /* c */ UNION MATCH (b) RETURN b.y AS y",
    # CALL blocks / procedures.
    "CALL { MATCH (a) RETURN a.x AS x UNION MATCH (b) RETURN b.y AS y } RETURN x",
    "MATCH (a) RETURN a.x AS x UNION CALL db.labels() YIELD label RETURN label",
    # A UNION nested inside brackets.
    "MATCH (a) WHERE EXISTS { MATCH (b) RETURN b.x AS x UNION MATCH (c) RETURN c.y AS y } "
    "RETURN a.id AS id",
    # RETURN *, an un-aliased expression, a branch with no RETURN.
    "MATCH (a) RETURN * UNION MATCH (b) RETURN b.y AS y",
    "MATCH (a) RETURN count(a) UNION MATCH (b) RETURN b.y AS y",
    "MATCH (a) UNION MATCH (b) RETURN b.y AS y",
    # Unterminated string, unbalanced brackets, a property named union.
    "MATCH (a {n: 'oops}) RETURN a.x AS x UNION MATCH (b) RETURN b.y AS y",
    "MATCH (a RETURN a.x AS x UNION MATCH (b) RETURN b.y AS y",
    "MATCH (a) RETURN a.union AS u",
    None,
])
def test_uncertain_parse_fails_open(cypher):
    assert union_column_mismatch(cypher) is None


def test_mismatched_union_is_refused_before_submit(monkeypatch):
    submitted = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked",
                        lambda *a, **k: submitted.append(a) or [])
    monkeypatch.setattr(graph_tools, "neptune_query",
                        lambda *a, **k: submitted.append(a) or [])

    raw = graph_tools.explore_graph(_MISMATCHED)
    out = json.loads(raw)

    assert submitted == [], "a confidently mismatched UNION must not reach the engine"
    assert out["query_failed"] is True and out["results"] == []
    assert out["union_columns"] == [["id", "name"], ["concept_id", "name"]]
    assert "branch 1 returns [id, name]" in out["error"]
    assert "branch 2 returns [concept_id, name]" in out["error"]
    assert "same column names in the same order" in out["error"]
    assert raw.startswith('{"error": "Graph query failed')
    assert qi.failure_marker(raw) == ("error", "query failed")


def test_uncertain_union_is_submitted_unchanged(monkeypatch):
    from agents.explore_inactive_joins import OWN_READS
    own = set(OWN_READS)
    submitted = []

    def checked(q, *a, **k):
        if q in own:  # the tool's own switched-off JoinPath check
            return []
        submitted.append(q)
        return [{"x": 1}]

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    # A UNION nested inside brackets. (CALL { } subqueries are refused by the
    # read-only check, test_explore_graph_read_only.py.)
    query = ("MATCH (n) WHERE EXISTS { MATCH (a) RETURN a.x AS x "
             "UNION MATCH (b) RETURN b.y AS y } RETURN n.x AS x")
    out = json.loads(graph_tools.explore_graph(query))
    assert submitted == [query]
    assert out == {"results": [{"x": 1}]}


def test_refused_union_summarizes_as_failed_never_no_matching_entities():
    raw = graph_tools.explore_graph(_MISMATCHED)
    summary = summarize_tool_result("explore_graph", {"content": [{"text": raw}]})
    assert summary.startswith("Error: Graph query failed (UNION column mismatch")
    assert "branch 2 returns [concept_id, name]" in summary
    assert "No matching" not in summary
    assert qi.is_failed_result(summary) is True


def test_query_failed_marker_alone_still_reads_failed():
    # The explicit marker wins even when no error text came back.
    payload = json.dumps({"results": [], "query_failed": True})
    summary = summarize_tool_result("explore_graph", {"content": [{"text": payload}]})
    assert summary == "Error: Graph query failed"
    assert qi.is_failed_result(summary) is True
