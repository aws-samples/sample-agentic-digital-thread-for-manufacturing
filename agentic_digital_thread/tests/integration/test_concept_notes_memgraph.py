# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The concept-notes vocabulary read on a real openCypher engine (the local
Memgraph backend, ``GRAPH_BACKEND=local``).

``concept_notes.CONCEPT_NOTES_CYPHER`` must return every live concept (with
or without a description) with the connected systems that map a field to it
and those that map a table to it, leave out a retired concept, and leave out a
system whose removal has started.

Skipped unless ``ADT_TEST_MEMGRAPH_URI`` names a THROWAWAY Memgraph (every
node in it is deleted) and the Bolt driver (``pip install .[local]``) is
importable. Run (from ``agentic_digital_thread/``)::

    docker run -d --rm --name adt-mg-test -p 127.0.0.1:57687:7687 \\
      memgraph/memgraph:3.12.0 --telemetry-enabled=false
    ADT_TEST_MEMGRAPH_URI=bolt://127.0.0.1:57687 \\
      PYTHONPATH=. python -m pytest tests/integration/test_concept_notes_memgraph.py -q
"""
from __future__ import annotations

import os

import pytest

import agents.concept_notes as cn

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")


@pytest.fixture
def run():
    if not _URI:
        pytest.skip("set ADT_TEST_MEMGRAPH_URI to a throwaway Memgraph to run these")
    neo4j = pytest.importorskip("neo4j")
    from agents.db import _to_plain
    driver = neo4j.GraphDatabase.driver(_URI, auth=("", ""))

    def _run(cypher, params=None):
        with driver.session() as session:
            return [{k: _to_plain(v) for k, v in record.items()}
                    for record in session.run(cypher, **(params or {}))]

    _run("MATCH (n) DETACH DELETE n")
    _run("CREATE (:System {id: 'sys-a'}), (:System {id: 'sys-b'}), "
         "(:System {id: 'sys-x', removal_incomplete: true})")
    _run("CREATE (:Concept {id: 'stalled_unit', name: 'Stalled Unit', "
         "description: 'Open past its limit.', synonyms: ['held unit']}), "
         "(:Concept {id: 'bare_term', name: 'Bare Term'}), "
         "(:Concept {id: 'old_term', name: 'Old Term', description: 'Gone.', retired: true})")
    for sid in ("sys-a", "sys-x"):
        _run("MATCH (s:System {id: $sid}), (c:Concept {id: 'stalled_unit'}) "
             "CREATE (s)-[:HAS_SCHEMA]->(:SchemaEntry {name: 'table_x'})"
             "-[:HAS_FIELD]->(:Field {name: 'col_a'})-[:MAPS_TO]->(c)", {"sid": sid})
    # A table-level (entity) mapping only, from a live and a removed system.
    for sid in ("sys-b", "sys-x"):
        _run("MATCH (s:System {id: $sid}), (c:Concept {id: 'bare_term'}) "
             "CREATE (s)-[:HAS_SCHEMA]->(:SchemaEntry {name: 'table_y'})-[:MAPS_TO]->(c)",
             {"sid": sid})
    yield _run
    _run("MATCH (n) DETACH DELETE n")
    driver.close()


def test_the_vocabulary_read_on_a_real_engine(run):
    rows = run(cn.CONCEPT_NOTES_CYPHER)
    assert [row["id"] for row in rows] == ["bare_term", "stalled_unit"]
    by_id = {row["id"]: row for row in rows}
    assert by_id["stalled_unit"]["systems"] == ["sys-a"]
    assert by_id["stalled_unit"]["synonyms"] == ["held unit"]
    assert by_id["bare_term"]["systems"] == []
    # Table-level mappings ride the same read; a removed system is left out.
    assert by_id["bare_term"]["entity_systems"] == ["sys-b"]
    assert by_id["stalled_unit"]["entity_systems"] == []
    concepts = cn.concepts_from_rows(rows)
    notes = cn.match_notes("How many stalled units?", concepts)
    assert [n.concept for n in notes] == ["stalled_unit"]
    assert notes[0].systems == ("sys-a",)
