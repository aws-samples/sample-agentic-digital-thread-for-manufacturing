# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The equipment post-pass reconcile on a real openCypher engine (the local
Memgraph backend, ``GRAPH_BACKEND=local``).

``onboard._persist_equipment_value_mappings`` runs its real statements here: the
one-statement mapping write with its Asset anchor, the generated-duplicate
retirement, and the reconcile after a complete pass
(``_sweep_stale_equipment_vms``: a count, ``DETACH DELETE``, a readback). The
pass itself is given (an ``_EquipmentPass`` naming its complete scope and the
registries a whole discovery returned), so this pins the cypher:
``NOT (vm.id IN $keep)``, the method and system scope, the two ends of a name
match, the rows of a system that is no registry now (``NOT (... IN $regs)``,
either end), and that steward, learned, LLM-bridged and out-of-scope rows
stay. The offline twin is ``tests/unit/test_equipment_vm_reconcile.py``.

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


def _vm(from_system, from_value, to_system, to_value, method, element_id=""):
    return {"from_system": from_system, "from_field": "t.f", "from_value": from_value,
            "to_system": to_system, "to_field": "asset_name", "to_value": to_value,
            "to_element_id": element_id, "to_path": "", "concept": "equipment",
            "confidence": 0.95, "method": method}


#: What the pass writes now (the tag pick moved to another field).
TELEMETRY = _vm("reg-maint", "LG-QX-210", "hist-x", "Fixture-Lathe-QX-210",
                "value_format_match", "el-qx")
NAMES = _vm("reg-cell", "LN2-W1", "reg-maint", "LG-QX-210", "equipment_name_match")
#: What earlier provisions wrote. ``stale`` rows go; the others stay.
STALE = {
    "telemetry": _vm("reg-maint", "77-QX-210", "hist-x", "Fixture-Lathe-QX-210",
                     "value_format_match", "el-qx"),
    "hierarchy": _vm("reg-maint", "77-ZZ-990", "hist-x", "Saw Station", "hierarchy_position"),
    "names": _vm("reg-cell", "LN2-W1", "reg-maint", "77-QX-210", "equipment_name_match"),
}
KEPT = {
    "subj-0000000000000001": _vm("reg-maint", "77-QX-210", "hist-x", "Lathe", "user_confirmed"),
    "vm-learned-00000002": _vm("reg-maint", "77-ZZ-990", "hist-x", "Saw", "value_format_match"),
    "guidance-subj-0000003": _vm("reg-cell", "LN2-W2", "reg-maint", "77-ZZ-990",
                                 "equipment_name_match"),
    "vm-equip-bridged000004": _vm("reg-maint", "77-ZZ-990", "hist-x", "Saw Station B",
                                  "semantic_similarity"),
    "vm-equip-gone000000005": _vm("reg-gone", "G-1", "hist-x", "Fixture-Lathe-QX-210",
                                  "value_format_match"),
    "vm-equip-halfgone00006": _vm("reg-cell", "LN2-W1", "reg-gone", "G-1",
                                  "equipment_name_match"),
    "vm-equip-otherconcept7": dict(_vm("reg-maint", "77-QX-210", "hist-x", "Batch 7",
                                       "value_format_match"), concept="batch"),
}
#: Generated rows with ``reg-gone`` at one end, beside ``KEPT``'s two: they
#: stay while ``reg-gone`` is a registry discovery returned (read in part),
#: and go once a whole discovery no longer returns it.
GONE = {
    "vm-equip-gone000000005": KEPT["vm-equip-gone000000005"],
    "vm-equip-halfgone00006": KEPT["vm-equip-halfgone00006"],
    "vm-equip-gonefrom00008": _vm("reg-gone", "G-1", "reg-maint", "77-QX-210",
                                  "equipment_name_match"),
    "vm-equip-gonehier00009": _vm("reg-gone", "G-2", "hist-x", "Saw Station",
                                  "hierarchy_position"),
}
#: Rows with ``reg-gone`` at one end that are never swept.
GONE_PROTECTED = {
    "subj-gone00000000010": _vm("reg-gone", "G-1", "hist-x", "Fixture-Lathe-QX-210",
                                "user_confirmed"),
    "vm-learned-gone00011": _vm("reg-gone", "G-1", "hist-x", "Lathe", "value_format_match"),
    "vm-equip-gonebridge12": _vm("reg-gone", "G-1", "hist-x", "Lathe Station",
                                 "semantic_similarity"),
    "vm-equip-gonebatch013": dict(_vm("reg-gone", "G-1", "hist-x", "Batch 13",
                                      "value_format_match"), concept="batch"),
}
KEPT.update({**GONE, **GONE_PROTECTED})
#: Every registry of this graph: the default for a whole discovery that
#: returned them all (some read only in part).
_REGISTRIES = {"reg-cell", "reg-maint", "reg-gone"}
#: An index-keyed legacy id with the identity of ``TELEMETRY``: the duplicate
#: retirement (not the reconcile) removes it.
LEGACY_DUPLICATE = "vm-equip-LG-QX-210-0"


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
    run("UNWIND $ids AS sid CREATE (:System {id: sid})",
        {"ids": ["reg-cell", "reg-maint", "hist-x", "reg-gone"]})
    run("CREATE (:Asset {source_id: 'el-qx', name: 'Fixture-Lathe-QX-210'})")
    rows = {**{onboard._equipment_vm_id(vm): vm for vm in STALE.values()}, **KEPT,
            LEGACY_DUPLICATE: TELEMETRY}
    for vm_id, vm in rows.items():
        run("MATCH (fs:System {id: $fsys}), (ts:System {id: $tsys}) "
            "CREATE (vm:ValueMapping {id: $id}) SET vm += $props "
            "CREATE (vm)-[:FROM_SYSTEM]->(fs), (vm)-[:TO_SYSTEM]->(ts)",
            {"fsys": vm["from_system"], "tsys": vm["to_system"], "id": vm_id, "props": vm})
        if vm["to_element_id"]:
            run("MATCH (vm:ValueMapping {id: $id}), (a:Asset {source_id: $aid}) "
                "CREATE (vm)-[:MAPS_TO_ASSET]->(a)", {"id": vm_id, "aid": vm["to_element_id"]})
    monkeypatch.setattr(onboard, "_db_neptune_write_returning", run, raising=False)
    monkeypatch.setattr(onboard, "neptune_query_checked", run)
    yield run
    run("MATCH (n) DETACH DELETE n")
    driver.close()


def _pass(monkeypatch, vms, complete, names_complete=True, registries=_REGISTRIES):
    """The pass persist sees: ``complete`` read in full, ``registries`` the
    registries a whole discovery returned (``None``: discovery not whole)."""
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings",
                        lambda: onboard._EquipmentPass(vms, complete, names_complete,
                                                       registries=registries))


def _ids(run) -> set:
    return {r["id"] for r in run("MATCH (vm:ValueMapping) RETURN vm.id AS id")}


def _anchors(run) -> list:
    return sorted((r["vm"], r["aid"]) for r in run(
        "MATCH (vm:ValueMapping)-[:MAPS_TO_ASSET]->(a:Asset) RETURN vm.id AS vm, a.source_id AS aid"))


def test_a_complete_pass_retires_exactly_its_stale_generated_rows(graph, monkeypatch):
    _pass(monkeypatch, [TELEMETRY, NAMES], {"reg-cell", "reg-maint"})
    assert onboard._persist_equipment_value_mappings() == 2
    now = {onboard._equipment_vm_id(TELEMETRY), onboard._equipment_vm_id(NAMES)}
    assert _ids(graph) == now | set(KEPT)
    assert _anchors(graph) == [(onboard._equipment_vm_id(TELEMETRY), "el-qx")]
    assert graph("MATCH (a:Asset {source_id: 'el-qx'}) RETURN count(a) AS n") == [{"n": 1}]
    assert graph("MATCH (vm:ValueMapping)-[e]->(:System) RETURN count(e) AS n") == [
        {"n": 2 * len(_ids(graph))}]


def test_a_second_identical_pass_deletes_nothing(graph, monkeypatch):
    _pass(monkeypatch, [TELEMETRY, NAMES], {"reg-cell", "reg-maint"})
    onboard._persist_equipment_value_mappings()
    before = graph("MATCH (vm:ValueMapping) RETURN vm.id AS id, properties(vm) AS p ORDER BY id")
    anchors = _anchors(graph)
    deletes = []
    real = onboard._db_neptune_write_returning

    def spy(cypher, params=None):
        if cypher == onboard._RETIRE_STALE_EQUIPMENT_VMS:
            deletes.append(params)
        return real(cypher, params)
    monkeypatch.setattr(onboard, "_db_neptune_write_returning", spy, raising=False)
    assert onboard._persist_equipment_value_mappings() == 2
    assert deletes == []
    assert graph("MATCH (vm:ValueMapping) RETURN vm.id AS id, properties(vm) AS p "
                 "ORDER BY id") == before
    assert _anchors(graph) == anchors


def test_a_name_match_needs_both_ends_in_the_complete_scope(graph, monkeypatch):
    """Only the cell registry's reads were incomplete: the maintenance
    registry's stale telemetry rows go, the name match (one end the cell
    registry) stays, as does every name match when the match itself failed."""
    _pass(monkeypatch, [TELEMETRY], {"reg-maint"})
    onboard._persist_equipment_value_mappings()
    stale_names = onboard._equipment_vm_id(STALE["names"])
    assert stale_names in _ids(graph)
    assert onboard._equipment_vm_id(STALE["telemetry"]) not in _ids(graph)
    assert onboard._equipment_vm_id(STALE["hierarchy"]) not in _ids(graph)

    _pass(monkeypatch, [TELEMETRY], {"reg-cell", "reg-maint"}, names_complete=False)
    onboard._persist_equipment_value_mappings()
    assert stale_names in _ids(graph)
    _pass(monkeypatch, [TELEMETRY], {"reg-cell", "reg-maint"})
    onboard._persist_equipment_value_mappings()
    assert stale_names not in _ids(graph)


def test_an_empty_complete_pass_retires_every_generated_row_of_its_scope(graph, monkeypatch):
    """Nothing produced for a registry read in full: ``$keep`` is empty, and
    every generated deterministic row of that registry goes."""
    _pass(monkeypatch, [], {"reg-cell", "reg-maint"})
    assert onboard._persist_equipment_value_mappings() == 0
    assert _ids(graph) == set(KEPT)
    assert _anchors(graph) == []


def test_no_complete_registry_sweeps_nothing(graph, monkeypatch):
    before = _ids(graph)
    _pass(monkeypatch, [], set())
    onboard._persist_equipment_value_mappings()
    assert _ids(graph) == before


# --------------------------------------------------------------------------- #
# after a whole discovery: systems that are no registry now
# --------------------------------------------------------------------------- #

def _stale_statements(monkeypatch) -> list:
    """Record every reconcile statement sent (the count and the delete)."""
    sent = []
    read, write = onboard.neptune_query_checked, onboard._db_neptune_write_returning

    def spy_read(cypher, params=None):
        if cypher == onboard._STALE_EQUIPMENT_VMS_LEFT:
            sent.append("count")
        return read(cypher, params)

    def spy_write(cypher, params=None):
        if cypher == onboard._RETIRE_STALE_EQUIPMENT_VMS:
            sent.append("delete")
        return write(cypher, params)
    monkeypatch.setattr(onboard, "neptune_query_checked", spy_read)
    monkeypatch.setattr(onboard, "_db_neptune_write_returning", spy_write, raising=False)
    return sent


def test_a_whole_discovery_retires_the_rows_of_a_system_that_is_no_registry_now(graph,
                                                                                 monkeypatch):
    """``reg-gone`` is not returned: its telemetry rows (both methods) and the
    name matches with it at either end go; the protected rows about it stay.
    ``reg-cell`` was returned but read in part: its stale name match stays."""
    _pass(monkeypatch, [TELEMETRY], {"reg-maint"}, registries={"reg-cell", "reg-maint"})
    assert onboard._persist_equipment_value_mappings() == 1
    assert _ids(graph) == ({onboard._equipment_vm_id(TELEMETRY),
                            onboard._equipment_vm_id(STALE["names"])} | (set(KEPT) - set(GONE)))
    assert _anchors(graph) == [(onboard._equipment_vm_id(TELEMETRY), "el-qx")]


def test_an_absent_end_retires_a_name_match_even_when_the_name_match_failed(graph, monkeypatch):
    _pass(monkeypatch, [TELEMETRY, NAMES], {"reg-cell", "reg-maint"}, names_complete=False,
          registries={"reg-cell", "reg-maint"})
    onboard._persist_equipment_value_mappings()
    ids = _ids(graph)
    assert not set(GONE) & ids
    assert onboard._equipment_vm_id(STALE["names"]) in ids
    assert set(GONE_PROTECTED) <= ids


def test_a_whole_discovery_with_no_registry_retires_every_generated_deterministic_row(
        graph, monkeypatch):
    _pass(monkeypatch, [], set(), registries=set())
    assert onboard._persist_equipment_value_mappings() == 0
    assert _ids(graph) == set(KEPT) - set(GONE)
    assert _anchors(graph) == []


def test_a_discovery_not_known_whole_sends_nothing(graph, monkeypatch):
    before = _ids(graph)
    sent = _stale_statements(monkeypatch)
    _pass(monkeypatch, [], {"reg-cell", "reg-maint"}, registries=None)
    onboard._persist_equipment_value_mappings()
    assert sent == []
    assert _ids(graph) == before


def test_after_a_system_stops_being_a_registry_a_second_pass_deletes_nothing(graph, monkeypatch):
    _pass(monkeypatch, [TELEMETRY, NAMES], {"reg-cell", "reg-maint"},
          registries={"reg-cell", "reg-maint"})
    onboard._persist_equipment_value_mappings()
    assert not set(GONE) & _ids(graph)
    before = graph("MATCH (vm:ValueMapping) RETURN vm.id AS id, properties(vm) AS p ORDER BY id")
    anchors = _anchors(graph)
    sent = _stale_statements(monkeypatch)
    assert onboard._persist_equipment_value_mappings() == 2
    assert sent == ["count"]
    assert graph("MATCH (vm:ValueMapping) RETURN vm.id AS id, properties(vm) AS p "
                 "ORDER BY id") == before
    assert _anchors(graph) == anchors
