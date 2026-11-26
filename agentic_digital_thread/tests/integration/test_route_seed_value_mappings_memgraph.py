# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The question-scoped value-mapping read of the route seed, on a real
openCypher engine (the local Memgraph backend, ``GRAPH_BACKEND=local``).

``orchestrator._route_seed_value_mapping_cypher`` is the one read of row-level
ValueMapping values the seed makes (ADR-0035 amendment 2026-10-18). These
cases run it unchanged, with the parameters ``build_route_seed`` passes, on a
graph holding equal, trailing-segment, in-segment, hop and two-hop mappings,
a mapping to a system whose removal has started, a mapping whose value is
stored as a number, and rows the block may never carry (a model-proposed
method, a method the model picked under the model writer's ids, or a
steward-confirmed row without the one-row accept's stamp), then build the
seed through the same seam.

Skipped unless ``ADT_TEST_MEMGRAPH_URI`` names a THROWAWAY Memgraph (every
node in it is deleted) and the Bolt driver (``pip install .[local]``) is
importable. Run (from ``agentic_digital_thread/``)::

    docker run -d --rm --name adt-mg-test -p 127.0.0.1:57687:7687 \\
      memgraph/memgraph:3.12.0 --telemetry-enabled=false
    ADT_TEST_MEMGRAPH_URI=bolt://127.0.0.1:57687 \\
      PYTHONPATH=. python -m pytest tests/integration/test_route_seed_value_mappings_memgraph.py -q
"""
from __future__ import annotations

import os

import pytest

import agents.identifier_translation as it
import agents.orchestrator as orch

pytestmark = pytest.mark.integration

_URI = os.environ.get("ADT_TEST_MEMGRAPH_URI")
QUESTION = "Was asset QX-101 maintained before it ran?"


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
    for sid, removing in (("sys-a", False), ("sys-b", False), ("sys-c", False),
                          ("sys-d", False), ("sys-e", False), ("sys-x", True)):
        run("CREATE (:System {id: $id, name: $name, removal_incomplete: $rm})",
            {"id": sid, "name": f"System {sid[-1].upper()}", "rm": removing})
    yield run
    run("MATCH (n) DETACH DELETE n")
    driver.close()


#: The one-row accept's stamp on a steward-confirmed node.
STAMP = {"confirmation_mode": "single_row", "accepted_by": "steward@example.test"}


def _vm(run, vid, fs, fv, ts, tv, *, method="value_format_match", confidence=0.95,
        provenance=None, stamp=None):
    run("MATCH (f:System {id: $fs}), (t:System {id: $ts}) "
        "CREATE (vm:ValueMapping {id: $id, from_system: $fs, from_field: 'assets.code', "
        "from_value: $fv, to_system: $ts, to_field: 'units.tag', to_value: $tv, "
        "concept: 'equipment', method: $m, confidence: $c}) "
        "CREATE (vm)-[:FROM_SYSTEM]->(f), (vm)-[:TO_SYSTEM]->(t)",
        {"id": vid, "fs": fs, "fv": fv, "ts": ts, "tv": tv, "m": method, "c": confidence})
    if provenance is not None:
        run("MATCH (vm:ValueMapping {id: $id}) SET vm.provenance = $p", {"id": vid, "p": provenance})
    for prop, value in (stamp or {}).items():
        run(f"MATCH (vm:ValueMapping {{id: $id}}) SET vm.{prop} = $v", {"id": vid, "v": value})


def _story(run):
    _vm(run, "vm-equip-exact", "sys-a", "QX-101", "sys-b", "K-55")
    _vm(run, "subj-segment", "sys-c", "Unit_QX-101", "sys-d", "M-9", method="user_confirmed",
        confidence=0.8, stamp=STAMP)
    _vm(run, "vm-equip-inside", "sys-c", "AQX-101", "sys-d", "N-1")    # never: inside a segment
    _vm(run, "vm-equip-hop", "sys-b", "K-55", "sys-e", "P-3", method="equipment_name_match",
        provenance="measured")
    _vm(run, "vm-equip-two-hops", "sys-e", "P-3", "sys-a", "R-7")       # never: two hops
    _vm(run, "vm-equip-removing", "sys-a", "QX-101", "sys-x", "X-QX-101")  # never: removal started
    _untrusted(run)
    run("MATCH (f:System {id: 'sys-a'}), (t:System {id: 'sys-b'}) "
        "CREATE (vm:ValueMapping {id: 'vm-equip-number', from_system: 'sys-a', from_field: 'n', "
        "from_value: 101, to_system: 'sys-b', to_field: 'n', to_value: 7, "
        "method: 'value_format_match', confidence: 0.5}) "
        "CREATE (vm)-[:FROM_SYSTEM]->(f), (vm)-[:TO_SYSTEM]->(t)")


#: Rows that name the question's identifier but that the block may never carry.
UNTRUSTED = ("vm-xsys-sys-a-sys-c-0", "vm-xsys-sys-a-sys-c-1", "vm-equip-bridge",
             "vm-equip-position", "subj-bulk", "vm-learned-old", "subj-nobody", "subj-batch")


def _untrusted(run):
    _vm(run, UNTRUSTED[0], "sys-a", "QX-101", "sys-c", "MODEL-1", method="user_confirmed",
        confidence=1.0)
    _vm(run, UNTRUSTED[1], "sys-a", "QX-101", "sys-c", "MODEL-2", confidence=1.0)
    _vm(run, UNTRUSTED[2], "sys-a", "QX-101", "sys-e", "BRIDGE-1", method="semantic_similarity")
    _vm(run, UNTRUSTED[3], "sys-a", "QX-101", "sys-e", "POS-1", method="hierarchy_position")
    # Steward-confirmed method and ids, but no one-row stamp: a bulk accept,
    # a row from before the stamp, a mode with nobody named, a batch.
    _vm(run, UNTRUSTED[4], "sys-a", "QX-101", "sys-d", "BULK-1", method="user_confirmed")
    _vm(run, UNTRUSTED[5], "sys-a", "QX-101", "sys-d", "OLD-1", method="user_confirmed")
    _vm(run, UNTRUSTED[6], "sys-a", "QX-101", "sys-d", "NOBODY-1", method="user_confirmed",
        stamp={"confirmation_mode": it.SINGLE_ROW_CONFIRMATION, "accepted_by": ""})
    _vm(run, UNTRUSTED[7], "sys-a", "QX-101", "sys-d", "BATCH-1", method="crosswalk_confirmed",
        stamp={"confirmation_mode": "", "accepted_by": "steward@example.test"})


def _params(**values) -> dict:
    return {**orch._route_seed_value_mapping_trust()[1], **values}


def test_the_direct_read_returns_equal_and_suffix_values_and_leaves_out_a_removing_system(graph):
    _story(graph)
    exact, suffixes = it.value_mapping_terms(QUESTION)
    params = _params(exact=exact, **{f"s{i}": s for i, s in enumerate(suffixes)})
    rows = graph(orch._route_seed_value_mapping_cypher(len(suffixes)), params)
    # Ordered as the rule-grain read: steward tier first, then confidence, then id.
    assert [r["id"] for r in rows] == ["subj-segment", "vm-equip-exact"]
    assert rows[1]["from_value"] == "QX-101" and rows[1]["provenance"] is None


def test_the_hop_read_returns_the_rows_sharing_a_value(graph):
    _story(graph)
    rows = graph(orch._route_seed_value_mapping_cypher(0, "linked"), _params(linked=["k-55"]))
    assert [r["id"] for r in rows] == ["vm-equip-hop", "vm-equip-exact"]
    assert rows[0]["provenance"] == "measured"


def test_the_read_is_capped(graph):
    for n in range(orch._ROUTE_SEED_VALUE_MAPPING_READ_CAP + 5):
        _vm(graph, f"vm-equip-{n:02d}", "sys-a", "QX-101", "sys-b", f"T-{n}")
    rows = graph(orch._route_seed_value_mapping_cypher(0), _params(exact=["qx-101"]))
    assert len(rows) == orch._ROUTE_SEED_VALUE_MAPPING_READ_CAP


def test_the_seed_built_on_the_engine_carries_exactly_the_reachable_mappings(graph):
    _story(graph)
    seed = orch.build_route_seed(query=graph, question=QUESTION)
    found = [(m["from_value"], m["to_value"], m["match"])
             for m in it.parse_value_mappings(seed)]
    assert found == [("Unit_QX-101", "M-9", "segment"), ("K-55", "P-3", "hop"),
                     ("QX-101", "K-55", "exact")]
    assert "sys-x" not in seed and "AQX-101" not in seed and "R-7" not in seed
    assert not any(value in seed for value in ("MODEL-1", "MODEL-2", "BRIDGE-1", "POS-1",
                                                "BULK-1", "OLD-1", "NOBODY-1", "BATCH-1"))
    assert "steward@example.test" not in seed
    assert seed.startswith(orch._ROUTE_SEED_VALUE_MAPPING_HEADERS[orch._ROUTE_SEED_HEADER])


def test_the_read_never_returns_a_row_the_block_may_not_carry(graph):
    _untrusted(graph)
    _vm(graph, "vm-equip-kept", "sys-a", "QX-101", "sys-b", "K-1")
    exact, suffixes = it.value_mapping_terms(QUESTION)
    rows = graph(orch._route_seed_value_mapping_cypher(len(suffixes)),
                 _params(exact=exact, **{f"s{i}": s for i, s in enumerate(suffixes)}))
    assert [r["id"] for r in rows] == ["vm-equip-kept"]
    hop = graph(orch._route_seed_value_mapping_cypher(0, "linked"), _params(linked=["qx-101"]))
    assert [r["id"] for r in hop] == ["vm-equip-kept"]


def test_the_read_returns_a_stamped_steward_row_with_its_stamp(graph):
    _vm(graph, "subj-one", "sys-a", "QX-101", "sys-b", "K-1", method="user_confirmed", stamp=STAMP)
    _vm(graph, "vm-learned-one", "sys-a", "QX-101", "sys-c", "K-2", method="user_confirmed",
        stamp=STAMP)
    _untrusted(graph)
    rows = graph(orch._route_seed_value_mapping_cypher(0), _params(exact=["qx-101"]))
    assert [r["id"] for r in rows] == ["subj-one", "vm-learned-one"]
    assert all(it.value_mapping_trusted(r) for r in rows)
    assert {(r["confirmation_mode"], r["accepted_by"]) for r in rows} == {
        (it.SINGLE_ROW_CONFIRMATION, "steward@example.test")}


@pytest.mark.parametrize("question, stored", [
    ("Is ASSET#1 down?", "ASSET#1"),
    ("Is M\u00dc-1 down?", "MU\u0308-1"),       # composed question, decomposed stored value
    ("Is MU\u0308-1 down?", "M\u00dc-1"),       # and the other way round
    ("Is m\u00fc-1 down?", "M\u00dc-1"),        # the engine lowers ASCII letters only
])
def test_a_hash_or_non_ascii_identifier_is_read_by_exact_value(graph, question, stored):
    _vm(graph, "vm-equip-wide", "sys-a", stored, "sys-b", "K-1")
    _vm(graph, "vm-equip-longer", "sys-a", f"LINE-{stored}", "sys-b", "K-2")
    exact, suffixes = it.value_mapping_terms(question)
    assert suffixes == []
    rows = graph(orch._route_seed_value_mapping_cypher(0), _params(exact=exact))
    assert [r["id"] for r in rows] == ["vm-equip-wide"]
    seed = orch.build_route_seed(query=graph, question=question)
    assert [m["to_value"] for m in it.parse_value_mappings(seed)] == ["K-1"]


def test_a_non_ascii_value_one_link_further_is_read(graph):
    _vm(graph, "vm-equip-direct", "sys-a", "QX-101", "sys-b", "M\u00dc-9")
    _vm(graph, "vm-equip-hop", "sys-b", "M\u00dc-9", "sys-c", "P-1")
    seed = orch.build_route_seed(query=graph, question="Is QX-101 down?")
    assert [(m["from_value"], m["to_value"], m["match"]) for m in it.parse_value_mappings(seed)] \
        == [("QX-101", "M\u00dc-9", "exact"), ("M\u00dc-9", "P-1", "hop")]


@pytest.mark.parametrize("question, stored", [
    ("Is \u00df1 down?", "\u1e9e1"),            # ß asked, ẞ stored: "ß".upper() is "SS"
    ("Is \u1e9e1 down?", "\u00df1"),            # and the other way round
    ("Is \u00c4k1 down?", "\u00c4\u212a1"),     # an ASCII k asked, the Kelvin sign stored
])
def test_a_stored_value_in_any_capital_the_match_accepts_is_read(graph, question, stored):
    _vm(graph, "vm-equip-wide", "sys-a", stored, "sys-b", "K-1")
    exact, _suffixes = it.value_mapping_terms(question)
    rows = graph(orch._route_seed_value_mapping_cypher(0), _params(exact=exact))
    assert [r["id"] for r in rows] == ["vm-equip-wide"]
    seed = orch.build_route_seed(query=graph, question=question)
    assert [m["to_value"] for m in it.parse_value_mappings(seed)] == ["K-1"]


def test_a_value_one_link_further_in_another_capital_is_read(graph):
    _vm(graph, "vm-equip-direct", "sys-a", "QX-101", "sys-b", "\u00df9")
    _vm(graph, "vm-equip-hop", "sys-b", "\u1e9e9", "sys-c", "P-1")
    seed = orch.build_route_seed(query=graph, question="Is QX-101 down?")
    assert [(m["to_value"], m["match"]) for m in it.parse_value_mappings(seed)] \
        == [("\u00df9", "exact"), ("P-1", "hop")]
