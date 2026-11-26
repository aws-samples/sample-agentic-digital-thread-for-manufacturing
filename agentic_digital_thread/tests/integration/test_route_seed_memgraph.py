# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route-seed Cypher leaves out a system whose removal has started, on a
real openCypher engine (the local Memgraph backend, ``GRAPH_BACKEND=local``).

The seed's reads run in the graph, and the prompt tells the model to run the
same traversal itself (``_ROUTE_SEED_CYPHER``) when no seed was built, so the
filter has to live in the Cypher: no System row for the marked system, and
no shared concept, join, value mapping or identity rule naming it under a
surviving peer, dropped BEFORE each collection is ordered and cut to its
50-item cap. These cases run that Cypher unchanged on a graph holding one
marked system.

Skipped unless ``ADT_TEST_MEMGRAPH_URI`` names a THROWAWAY Memgraph (every
node in it is deleted) and the Bolt driver (``pip install .[local]``) is
importable. Run (from ``agentic_digital_thread/``)::

    docker run -d --rm --name adt-mg-test -p 127.0.0.1:57687:7687 \\
      memgraph/memgraph:3.12.0 --telemetry-enabled=false
    ADT_TEST_MEMGRAPH_URI=bolt://127.0.0.1:57687 \\
      PYTHONPATH=. python -m pytest tests/integration/test_route_seed_memgraph.py -q
"""
from __future__ import annotations

import os

import pytest

import agents.orchestrator as orch
from agents.catalog import REMOVING_SYSTEMS_READ

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")

SERVED, PEER, REMOVING = "sys-a", "sys-b", "sys-x"
QUESTION = "What is the state of Y-3301?"


@pytest.fixture
def graph():
    if not _URI:
        pytest.skip("set ADT_TEST_MEMGRAPH_URI to a throwaway Memgraph to run these")
    neo4j = pytest.importorskip("neo4j")
    from agents.db import _to_plain
    driver = neo4j.GraphDatabase.driver(_URI, auth=("", ""))

    def run(cypher, params=None):
        with driver.session() as session:
            return [{k: _to_plain(v) for k, v in record.items()}
                    for record in session.run(cypher, **(params or {}))]

    run("MATCH (n) DETACH DELETE n")
    run("CREATE (:System {id: $a, name: 'A'}), (:System {id: $b, name: 'B'}), "
        "(:System {id: $x, name: 'X', removal_incomplete: true})", {"a": SERVED, "b": PEER, "x": REMOVING})
    # Each system has one field mapped to the same concept (a shared concept).
    run("CREATE (:Concept {id: 'equipment-id'})")
    for sid in (SERVED, PEER, REMOVING):
        run("MATCH (s:System {id: $sid}), (c:Concept {id: 'equipment-id'}) "
            "CREATE (s)-[:HAS_SCHEMA]->(:SchemaEntry {name: 'assets'})-[:HAS_FIELD]->"
            "(f:Field {name: 'asset_code', system_id: $sid}) CREATE (f)-[:MAPS_TO]->(c)",
            {"sid": sid})
    yield run
    run("MATCH (n) DETACH DELETE n")
    driver.close()


def _identity_rule(run, rid, src, tgt, *, method, confidence, src_pat, tgt_pat):
    run("MATCH (c:Concept {id: 'equipment-id'}) "
        "CREATE (:IdentityRule {id: $id, concept: 'equipment-id', source_system: $src, "
        "source_field: 'asset_code', target_system: $tgt, target_field: 'asset_code', "
        "source_pattern: $sp, target_pattern: $tp, method: $m, confidence: $conf})"
        "-[:LINKS_CONCEPT]->(c)",
        {"id": rid, "src": src, "tgt": tgt, "sp": src_pat, "tp": tgt_pat, "m": method, "conf": confidence})


def _join_path(run, jid, frm, to, *, method, confidence):
    run("MATCH (f:System {id: $frm}), (t:System {id: $to}) "
        "CREATE (j:JoinPath {id: $id, from_system: $frm, from_table: 'assets', from_field: 'asset_code', "
        "to_system: $to, to_table: 'assets', to_field: 'asset_code', method: $m, confidence: $conf}), "
        "(j)-[:FROM_SYSTEM]->(f), (j)-[:TO_SYSTEM]->(t)",
        {"id": jid, "frm": frm, "to": to, "m": method, "conf": confidence})


def _rows_by_system(run) -> dict:
    return {row["system_id"]: row for row in run(orch._ROUTE_SEED_CYPHER)}


def _named(row) -> set:
    names = {c.get("peer_system") for c in row["shared_concepts"]}
    for kind in ("join_paths", "value_mappings", "identity_rules"):
        for rule in row[kind]:
            names |= {rule.get(k) for k in ("from_system", "to_system", "source_system", "target_system")}
    return names


def test_the_models_fallback_traversal_returns_nothing_of_a_marked_system(graph):
    """``_ROUTE_SEED_CYPHER`` run as is, the way the model runs it when no
    seed was built."""
    _identity_rule(graph, "ir-a-x", SERVED, REMOVING, method="user_confirmed", confidence=0.9,
                   src_pat="X-{id}", tgt_pat="P-{id}")
    _identity_rule(graph, "ir-a-b", SERVED, PEER, method="user_confirmed", confidence=0.9,
                   src_pat="Y-{id}", tgt_pat="B-{id}")
    _join_path(graph, "jp-a-x", SERVED, REMOVING, method="user_confirmed", confidence=0.9)
    _join_path(graph, "jp-a-b", SERVED, PEER, method="user_confirmed", confidence=0.9)
    rows = _rows_by_system(graph)
    assert set(rows) == {SERVED, PEER}
    for row in rows.values():
        assert REMOVING not in _named(row), row
    assert {r["id"] for r in rows[SERVED]["identity_rules"]} == {"ir-a-b"}
    assert {r["id"] for r in rows[SERVED]["join_paths"]} == {"jp-a-b"}
    assert PEER in {c["peer_system"] for c in rows[SERVED]["shared_concepts"]}


def test_fifty_marked_rules_ranked_first_never_take_the_served_rules_slot(graph):
    """50 steward-confirmed rules to the marked system outrank one lower-tier
    rule to a served peer; the served rule (the only one the question
    needs) still reaches its system's 50 slots, and its translation renders."""
    for n in range(orch._ROUTE_SEED_RULE_CAP):
        _identity_rule(graph, f"ir-a-x-{n:02d}", SERVED, REMOVING, method="user_confirmed",
                       confidence=0.99, src_pat=f"X{n:02d}-{{id}}", tgt_pat=f"P{n:02d}-{{id}}")
        _join_path(graph, f"jp-a-x-{n:02d}", SERVED, REMOVING, method="user_confirmed", confidence=0.99)
    _identity_rule(graph, "ir-a-b", SERVED, PEER, method="onboarding-llm", confidence=0.5,
                   src_pat="Y-{id}", tgt_pat="B-{id}")
    _join_path(graph, "jp-a-b", SERVED, PEER, method="onboarding-llm", confidence=0.5)
    served = _rows_by_system(graph)[SERVED]
    assert [r["id"] for r in served["identity_rules"]] == ["ir-a-b"]
    assert [r["id"] for r in served["join_paths"]] == ["jp-a-b"]
    seed = orch.build_route_seed(query=graph, question=QUESTION)
    assert "B-3301" in seed and REMOVING not in seed


def test_the_authority_and_inventory_reads_leave_the_marked_system_out(graph):
    graph("CREATE (:AuthorityRule {id: 'ar-x', concept: 'machine-state', system_id: $x}), "
          "(:AuthorityRule {id: 'ar-a', concept: 'equipment-id', system_id: $a})",
          {"x": REMOVING, "a": SERVED})
    assert [r["id"] for r in graph(orch._ROUTE_SEED_AUTHORITY_CYPHER)] == ["ar-a"]
    assert [r["system_id"] for r in graph(orch._ROUTE_SEED_INVENTORY_CYPHER)] == [SERVED, PEER]
    assert [r["sid"] for r in graph(REMOVING_SYSTEMS_READ)] == [REMOVING]


def test_with_nothing_marked_every_system_and_rule_is_returned(graph):
    graph("MATCH (s:System {id: $x}) SET s.removal_incomplete = false", {"x": REMOVING})
    _identity_rule(graph, "ir-a-x", SERVED, REMOVING, method="user_confirmed", confidence=0.9,
                   src_pat="X-{id}", tgt_pat="P-{id}")
    rows = _rows_by_system(graph)
    assert set(rows) == {SERVED, PEER, REMOVING}
    assert {r["id"] for r in rows[SERVED]["identity_rules"]} == {"ir-a-x"}
    assert REMOVING in orch.build_route_seed(query=graph, question=QUESTION)
