# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A re-provision leaves a System at its current plant only, on a real openCypher engine
(the local Memgraph backend, ``GRAPH_BACKEND=local``).

``onboard._dp_link_plant`` runs its two writes unchanged here. An unsited system must
lose every LOCATED_AT edge, including one to a Plant whose id is empty or missing (a
predicate on ``p.id`` alone evaluates null there and keeps the edge); a sited system
keeps only the edge to its own plant.

Skipped unless ``ADT_TEST_MEMGRAPH_URI`` names a THROWAWAY Memgraph (every node in it is
deleted) and the Bolt driver (``pip install .[local]``) is importable. Run as in
``test_route_seed_memgraph.py``.
"""
from __future__ import annotations

import os

import pytest

import agents.onboard as onboard

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")


@pytest.fixture
def graph(monkeypatch):
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
    run("CREATE (s:System {id: 'sys-a'}), (s)-[:LOCATED_AT]->(:Plant {id: 'north-plant', name: 'North Plant'}), "
        "(s)-[:LOCATED_AT]->(:Plant {id: '', name: ''}), (s)-[:LOCATED_AT]->(:Plant {name: 'no id'}), "
        "(s)-[:LOCATED_AT]->(:Plant {id: '---', name: '   '}), "
        "(:System {id: 'sys-b'})-[:LOCATED_AT]->(:Plant {id: 'south-plant', name: 'South Plant'})")
    monkeypatch.setattr(onboard, "neptune_write", lambda cypher, params=None: run(cypher, params))
    yield run
    run("MATCH (n) DETACH DELETE n")
    driver.close()


def _sites(run, sid):
    return sorted(str(r["pid"]) for r in run(
        "MATCH (:System {id: $sid})-[:LOCATED_AT]->(p:Plant) RETURN p.id AS pid", {"sid": sid}))


def test_an_unsited_system_loses_every_site(graph):
    assert onboard._dp_link_plant("sys-a", "") == 0
    assert _sites(graph, "sys-a") == []
    assert _sites(graph, "sys-b") == ["south-plant"]


def test_a_sited_system_keeps_only_its_own_plant(graph):
    graph("CREATE (:Plant {id: 'east-plant', name: 'East Plant'})")
    assert onboard._dp_link_plant("sys-a", "East Plant") == 1
    assert _sites(graph, "sys-a") == ["east-plant"]
    assert _sites(graph, "sys-b") == ["south-plant"]


def test_a_sited_system_already_at_its_plant_keeps_that_edge(graph):
    assert onboard._dp_link_plant("sys-a", "North Plant") == 1
    assert _sites(graph, "sys-a") == ["north-plant"]
