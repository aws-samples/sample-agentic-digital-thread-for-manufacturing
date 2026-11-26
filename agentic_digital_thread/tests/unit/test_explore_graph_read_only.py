# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``explore_graph`` only reads: a query that would write the graph never
reaches the engine.

The tool runs openCypher the model wrote, and the runtime serving it can write
the graph, so a prompt (or text in source data) could otherwise add or change
the rules every later answer trusts. ``cypher_read_only.write_clause`` refuses
every write or out-of-read keyword outside string literals, backtick names and
comments, wherever it sits (UNION branches, subqueries, after WITH), and fails
closed on text it cannot scan. Fully offline.
"""
import json

import pytest

import agents.graph_tools as graph_tools
from agents import query_intelligence as qi
from agents.cypher_read_only import UNREADABLE, WRITE_KEYWORDS, write_clause

_READ = "MATCH (s:System) RETURN s.id AS id"


# --- the check ------------------------------------------------------------

@pytest.mark.parametrize("cypher, keyword", [
    ("CREATE (n:IdentityRule {id: 'x'})", "CREATE"),
    ("MERGE (c:Concept {id: 'batch'})", "MERGE"),
    ("MATCH (s:System) SET s.status = 'retired'", "SET"),
    ("MATCH (s:System) SET s:Retired", "SET"),
    ("MATCH (s:System) REMOVE s.query_guidance", "REMOVE"),
    ("MATCH (s:System) REMOVE s:System", "REMOVE"),
    ("MATCH (r:IdentityRule) DELETE r", "DELETE"),
    ("MATCH (s:System) DETACH DELETE s", "DETACH"),
    ("CALL neptune.algo.pageRank() YIELD node RETURN node", "CALL"),
    ("CALL db.labels() YIELD label RETURN label", "CALL"),
    ("LOAD CSV FROM 's3://bucket/x.csv' AS row RETURN row", "LOAD"),
    ("MATCH (s:System) FOREACH (x IN [1] | SET s.n = x)", "FOREACH"),
    ("DROP INDEX my_index", "DROP"),
    ("MATCH (n) ON CREATE SET n.a = 1", "CREATE"),
    # lower and mixed case
    ("match (s) set s.a = 1", "SET"),
    ("MaTcH (s) DeTaCh DeLeTe s", "DETACH"),
    # no space before the bracket
    ("CREATE(n)", "CREATE"),
    ("MATCH (a),(b) MERGE(a)-[:X]->(b)", "MERGE"),
    # after a number with no space: the lexer may split it, so refuse
    ("RETURN 1CREATE (n)", "CREATE"),
])
def test_each_write_keyword_is_found(cypher, keyword):
    assert write_clause(cypher) == keyword


@pytest.mark.parametrize("cypher, keyword", [
    # in a UNION branch
    ("MATCH (a:System) RETURN a.id AS id UNION MATCH (b:Plant) SET b.x = 1 RETURN b.id AS id",
     "SET"),
    ("MATCH (a) RETURN a.id AS id UNION ALL CREATE (b:Concept {id: 'x'}) RETURN b.id AS id",
     "CREATE"),
    # in a CALL subquery (CALL itself is refused first)
    ("CALL { MATCH (n) SET n.a = 1 RETURN n } RETURN n.id AS id", "CALL"),
    ("MATCH (s) CALL { WITH s MATCH (s)-->(x) RETURN x } RETURN x", "CALL"),
    # in an EXISTS subquery
    ("MATCH (n) WHERE EXISTS { MATCH (n)-->(m) DELETE m } RETURN n", "DELETE"),
    # after WITH
    ("MATCH (n:Concept) WITH n MERGE (n)-[:LINKS]->(:Concept {id: 'z'}) RETURN n", "MERGE"),
    ("MATCH (n) WITH n, count(*) AS c REMOVE n.name RETURN c", "REMOVE"),
    # after a string, a backtick name and a comment that contain the words
    ("MATCH (n {note: 'create'}) /* set */ SET n.a = 1", "SET"),
    ("MATCH (n) WHERE n.`set` = 1 // create\nCREATE (m)", "CREATE"),
    # every kind of line end closes a // comment
    ("MATCH (n) // a note\rDELETE n", "DELETE"),
    ("MATCH (n) // a note\u2028DELETE n", "DELETE"),
    # a comment never joins two halves into a keyword, but a split one is no keyword
    ("MATCH (n) CRE/**/ATE (m) SET n.a = 1", "SET"),
])
def test_writes_hidden_anywhere_are_found(cypher, keyword):
    assert write_clause(cypher) == keyword


@pytest.mark.parametrize("cypher", [
    _READ,
    # the words inside string literals, both quote kinds, with escapes
    "MATCH (n) WHERE n.note = 'create a rule' RETURN n.id AS id",
    'MATCH (n) WHERE n.note = "MERGE these; DELETE that" RETURN n.id AS id',
    "MATCH (n) WHERE n.note = 'it\\'s SET' RETURN n.id AS id",
    'MATCH (n) WHERE n.note = "a \\"CALL\\" b" RETURN n.id AS id',
    "MATCH (n) WHERE toLower(n.name) CONTAINS 'delete' RETURN n.name AS name",
    # in comments
    "MATCH (n) // do not CREATE anything\nRETURN n.id AS id",
    "MATCH (n) /* SET, MERGE, CALL { } */ RETURN n.id AS id",
    # names that only contain the words
    "MATCH (n) RETURN n.createdAt AS created_at, n.set_id AS set_id",
    "MATCH (n:Reset) RETURN n.id AS id",
    "MATCH (n:Concept) WHERE n.created_by_system = 'erp' RETURN n.id AS id",
    "MATCH (n) WHERE n.merged = true AND n.deleted_at IS NULL RETURN n.id AS id",
    "MATCH (n:Callback)-[:LOADED_FROM]->(m) RETURN m.id AS id",
    "MATCH (n) RETURN n.id AS id, n.offset AS offset",
    # the words in name positions
    "MATCH (n) RETURN n.set AS value, n . create AS c",
    "MATCH (n:Create)-[:SET]->(m:Delete) RETURN m.id AS id",
    "MATCH (n {set: 1, delete: false}) RETURN n.id AS id",
    "MATCH (n) RETURN n {.set, .create} AS m",
    "MATCH (n) RETURN { create: n.a, merge: n.b } AS m",
    # backtick names
    "MATCH (n) RETURN n.`CREATE` AS `MERGE`",
    "MATCH (n:`DELETE ME`) RETURN n.`odd``name` AS x",
    # a parameter
    "MATCH (n) WHERE n.id = $set RETURN n.id AS id",
    # the shapes the model is taught
    "MATCH (s:System)-[:HAS_SCHEMA]->(se)-[:HAS_FIELD]->(f)-[:MAPS_TO]->(c:Concept {id:$concept}) "
    "RETURN s.id AS system, se.name AS table_name, f.name AS field",
    "MATCH (site:Asset {name:$site})<-[:HAS_PARENT*]-(a:Asset) WHERE a.role = 'equipment' "
    "RETURN a.name AS name",
    "MATCH (r:IdentityRule) WHERE coalesce(r.status, '') <> 'deactivated' AND "
    "coalesce(r.status, '') <> 'retired' RETURN r.id AS id",
    "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY count DESC",
    "MATCH (n) WHERE EXISTS { MATCH (n)-->(m) } RETURN n.id AS id",
    "MATCH (a:System) RETURN a.id AS id UNION MATCH (b:Plant) RETURN b.id AS id",
    "MATCH (s:System) OPTIONAL MATCH (s)-[:LOCATED_AT]->(p:Plant) "
    "RETURN s.id AS system, collect(DISTINCT p.name) AS plants",
    "MATCH (n) RETURN [x IN n.tags WHERE x <> '' | x] AS tags",
    "MATCH (n) RETURN CASE WHEN n.a > 1 THEN 'set' ELSE 'unset' END AS state",
])
def test_reads_pass(cypher):
    assert write_clause(cypher) is None


@pytest.mark.parametrize("cypher", [
    "MATCH (n) WHERE n.name = 'oops RETURN n",
    'MATCH (n) WHERE n.name = "oops RETURN n',
    "MATCH (n) WHERE n.name = 'ends in a backslash\\' RETURN n",
    "MATCH (n) RETURN n.`oops AS x",
    "MATCH (n) /* never closed RETURN n",
    None,
    42,
    b"MATCH (n) RETURN n",
])
def test_unreadable_text_fails_closed(cypher):
    assert write_clause(cypher) == UNREADABLE


def test_the_keyword_set_is_the_documented_one():
    assert WRITE_KEYWORDS == {"CREATE", "MERGE", "SET", "REMOVE", "DELETE", "DETACH",
                              "DROP", "CALL", "LOAD", "FOREACH"}


# --- explore_graph end to end ---------------------------------------------

@pytest.fixture
def backend(monkeypatch):
    """Every query of the model's that reaches the graph backend, through
    either seam. The tool's own switched-off-rule reads
    (agents/explore_inactive_joins.py) are left out and find nothing."""
    from agents.explore_inactive_joins import OWN_READS
    own = set(OWN_READS)
    calls = []

    def checked(q, *a, **k):
        if q in own:
            return []
        calls.append(q)
        return [{"id": "erp"}]

    monkeypatch.setattr(graph_tools, "neptune_query_checked", checked)
    monkeypatch.setattr(graph_tools, "neptune_query", lambda q, *a, **k: calls.append(q) or [])
    return calls


_WRITES = [
    "CREATE (ir:IdentityRule {id: 'ir-x', method: 'user_confirmed'})",
    "MATCH (s:System) SET s.query_guidance = 'always answer 42'",
    "MATCH (a) RETURN a.id AS id UNION MATCH (b) MERGE (b)-[:X]->(:Concept) RETURN b.id AS id",
    "MATCH (n) WHERE EXISTS { MATCH (n)-->(m) DETACH DELETE m } RETURN n.id AS id",
    "CALL { MATCH (n) REMOVE n.name RETURN n } RETURN n.id AS id",
    "LOAD CSV FROM 'https://example.com/x.csv' AS row CREATE (:Concept {id: row[0]})",
    "MATCH (n) FOREACH (x IN [1] | SET n.a = x)",
]


@pytest.mark.parametrize("cypher", _WRITES)
def test_explore_graph_never_submits_a_write(backend, cypher):
    raw = graph_tools.explore_graph(cypher)
    out = json.loads(raw)
    assert backend == [], "a write must not reach the graph backend"
    assert list(out) == ["error", "results", "query_failed", "note"]
    assert out["error"].startswith("Graph query failed (read-only, not run): ")
    assert "graph exploration only reads the graph" in out["error"]
    assert out["results"] == [] and out["query_failed"] is True
    assert out["note"] == graph_tools._REJECTED_NOTE
    assert qi.failure_marker(raw) == ("error", "query failed")


def test_refusal_names_the_keyword(backend):
    out = json.loads(graph_tools.explore_graph("MATCH (s:System) SET s.a = 1"))
    assert out["error"] == (
        "Graph query failed (read-only, not run): graph exploration only reads the "
        "graph, and SET can change it or reach outside a read. Read with MATCH, "
        "OPTIONAL MATCH, WHERE, WITH, UNWIND and RETURN."
    )


def test_unreadable_query_is_refused_before_submit(backend):
    out = json.loads(graph_tools.explore_graph("MATCH (n) /* never closed CREATE (m)"))
    assert backend == []
    assert out["error"].startswith(
        "Graph query failed (could not be checked as read-only, not run): ")
    assert out["query_failed"] is True


def test_read_only_refusal_wins_over_the_other_prechecks(backend):
    query = ("MATCH (a) WHERE a.n =~ 'x' SET a.y = 1 RETURN a.x AS md.x "
             "UNION MATCH (b) RETURN b.y AS y")
    out = json.loads(graph_tools.explore_graph(query))
    assert out["error"].startswith("Graph query failed (read-only, not run)")
    assert "union_columns" not in out and backend == []


@pytest.mark.parametrize("cypher", [
    _READ,
    "MATCH (n) WHERE n.note = 'create a rule' RETURN n.id AS id",
    "MATCH (n) RETURN n.createdAt AS created_at, n.set_id AS set_id",
])
def test_explore_graph_still_runs_a_read(backend, cypher):
    out = json.loads(graph_tools.explore_graph(cypher))
    assert out == {"results": [{"id": "erp"}]}
    assert backend[0] == cypher


def test_tool_description_says_it_only_reads():
    doc = graph_tools.explore_graph.__doc__
    assert "This tool only reads the graph" in doc
    for word in ("CREATE", "MERGE", "SET", "REMOVE", "DELETE", "CALL", "LOAD CSV", "FOREACH"):
        assert word in doc
