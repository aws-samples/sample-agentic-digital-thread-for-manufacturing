# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generated equipment mappings a complete pass no longer produces are retired.

``onboard._persist_equipment_value_mappings`` MERGEs the pass's mappings and
retires an older generated row with the same identity. A generated row whose
identity the pass no longer produces at all (the registry's tag pick moved to
a field with other values, a name match went away) was never retired, and the
route seed puts generated rows in front of the assistant as fact. After a
COMPLETE pass the reconcile (``_sweep_stale_equipment_vms``) retires them,
in the scope the pass read in full (``_EquipmentPass``). A whole discovery
reads every System in the graph, so a system it no longer returns is proven
to be no registry now (its last equipment-id field moved to another concept,
or only ineligible fields are left): the generated rows from it, and every
generated name match with it at either end, go too. Nothing is swept after
an incomplete read anywhere in that scope, a failed discovery or a failed
write; steward, learned and LLM-bridged rows are never swept.

Offline. The generation pass reads a fake graph through the REAL
``db.neptune_query`` (an injected failure raises inside it and is degraded to
no rows, as in production) and a fake ``pg`` that answers like ``db.pg``
(``unavailable`` / ``truncated``). The writes go to an in-memory graph
(``tests.unit._equipment_vm_graph``). Synthetic values only.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

import pytest

import agents.db as db
import agents.onboard as onboard
from tests.unit._equipment_vm_graph import EquipmentVmGraph

_PLANT = "Plant North"
_NAME = "Fixture Lathe QX-210"
_ASSET = {"name": "Fixture-Lathe-QX-210", "path": "North/Cell2/Fixture-Lathe-QX-210",
          "source_id": "el-qx", "system": "hist-x"}
_SYSTEMS = ("reg-cell", "reg-maint", "hist-x", "reg-gone", "reg-shop", "reg-part")

#: The rows each pass writes, as ``EquipmentVmGraph.rows`` lists them.
TELEMETRY_1 = ("reg-maint", "77-QX-210", "hist-x", "Fixture-Lathe-QX-210", "value_format_match")
NAMES_1 = ("reg-cell", "LN2-W1", "reg-maint", "77-QX-210", "equipment_name_match")
TELEMETRY_2 = ("reg-maint", "LG-QX-210", "hist-x", "Fixture-Lathe-QX-210", "value_format_match")
NAMES_2 = ("reg-cell", "LN2-W1", "reg-maint", "LG-QX-210", "equipment_name_match")


class _World:
    """What the generation pass reads: graph rows routed by cypher fragment,
    PostgreSQL tables answered for the three statement shapes the pass sends
    (the pick's value sample and reference count, the registry row read).
    ``graph_faults`` (cypher, params) and ``pg_faults`` (schema, sql, nth
    read of that statement) inject failures (a graph fault is degraded to no
    rows by ``db.neptune_query``; ``graph_crashes`` raise past it);
    ``unexpected`` collects any read the fakes do not know."""

    def __init__(self):
        self.registries = [_registry("reg-cell", "cells", "cell_code"),
                           _registry("reg-maint", "assets", "asset_tag")]
        self.fields = {"reg-cell": ["cell_code", "cell_name", "cell_type", "line"],
                       "reg-maint": ["asset_tag", "legacy_tag", "description", "site_ref",
                                     "location"]}
        self.tables = {
            "reg-cell_db": {"cells": [
                {"cell_code": "LN2-W1", "cell_name": _NAME, "cell_type": "LATHE", "line": "LINE2"},
                {"cell_code": "LN2-W2", "cell_name": "Bench Grinder 9", "cell_type": "GRINDER",
                 "line": "LINE2"}]},
            "reg-maint_db": {"assets": [
                {"asset_tag": "77-QX-210", "legacy_tag": "LG-QX-210", "description": _NAME,
                 "site_ref": "77", "location": "N-C2-L1"},
                {"asset_tag": "77-ZZ-990", "legacy_tag": "LG-ZZ-990", "description": "Saw ZZ-990",
                 "site_ref": "77", "location": "N-C2-S1"}]},
        }
        self.historians = ["hist-x"]
        self.assets = [dict(_ASSET)]
        self.graph_faults: list = []
        self.graph_crashes: list = []
        self.pg_faults: list = []
        self.reads: Counter = Counter()
        self.unexpected: list = []
        self.model = None
        self.model_calls: list = []

    def install(self, monkeypatch) -> "_World":
        monkeypatch.setattr(db, "neptune_query_checked", self.graph_read)
        monkeypatch.setattr(onboard, "neptune_query", db.neptune_query)
        monkeypatch.setattr(onboard, "pg", self.pg)
        monkeypatch.setattr(onboard, "bedrock_call", self.bedrock)
        return self

    def graph_read(self, cypher, params=None):
        params = params or {}
        if any(crash(cypher, params) for crash in self.graph_crashes):
            raise RuntimeError("connection reset by peer")
        if any(fault(cypher, params) for fault in self.graph_faults):
            raise db.GraphQueryError("ValidationException", "injected")
        if "Concept {id: $concept}" in cypher:
            return [dict(r) for r in self.registries]
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in self.fields.get(params["sid"], [])]
        if "(:Asset)-[:FROM_SYSTEM]" in cypher:
            return [{"sys_id": h} for h in self.historians]
        if "HAS_PROPERTY" in cypher:
            out = [dict(a) for a in self.assets]
            if "CONTAINS $site" in cypher:
                out = [a for a in out if params["site"] in a["path"].lower()]
            return out
        self.unexpected.append(cypher)
        return []

    def pg(self, schema, sql, params=None):
        self.reads[(schema, sql)] += 1
        nth = self.reads[(schema, sql)]
        fault = next((f(schema, sql, nth) for f in self.pg_faults if f(schema, sql, nth)), None)
        if fault == "raise":
            raise RuntimeError("connection reset")
        if fault == "unavailable":
            return {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                    "error_category": "OperationalError"}
        rows = self._answer(schema, sql)
        if fault == "truncated":
            return {"rows": rows[:1], "row_count": 1, "truncated": True,
                    "partial_reason": "row_limit"}
        return {"rows": rows, "row_count": len(rows)}

    def _answer(self, schema, sql) -> list:
        m = re.fullmatch(r"SELECT DISTINCT CAST\((\w+) AS TEXT\) FROM (\w+) "
                         r"WHERE \1 IS NOT NULL LIMIT (\d+)", sql)
        if m:
            col, table = m.group(1), self.tables[schema][m.group(2)]
            return [[v] for v in sorted({str(r[col]) for r in table if r.get(col) is not None})]
        m = re.fullmatch(r"SELECT COUNT\(\*\), COUNT\(DISTINCT v\), COUNT\(n\), COUNT\(DISTINCT n\) "
                         r"FROM \(SELECT CAST\((\w+) AS TEXT\) AS v, CAST\((\w+) AS TEXT\) AS n "
                         r"FROM (\w+) WHERE \1 IS NOT NULL LIMIT (\d+)\) AS s", sql)
        if m:
            tag, name, table = m.group(1), m.group(2), self.tables[schema][m.group(3)]
            rows = [r for r in table if r.get(tag) is not None][:int(m.group(4))]
            tags = [str(r[tag]) for r in rows]
            names = [str(r[name]) for r in rows if r.get(name) is not None]
            return [[len(tags), len(set(tags)), len(names), len(set(names))]]
        m = re.fullmatch(r"SELECT (.+) FROM (\w+) ORDER BY 1", sql)
        if m:
            cols = [c.strip() for c in m.group(1).split(",")]
            out = [[None if c == "NULL" else r.get(c) for c in cols]
                   for r in self.tables[schema][m.group(2)]]
            return sorted(out, key=lambda r: str(r[0]))
        self.unexpected.append(sql)
        return []

    def bedrock(self, prompt):
        self.model_calls.append(prompt)
        if self.model is None:
            raise AssertionError("the equipment pass called the model")
        return self.model(prompt)


def _registry(sys_id, table, field, is_key=True):
    return {"sys_id": sys_id, "db_schema": f"{sys_id}_db", "plant": _PLANT,
            "table_name": table, "field_name": field, "is_key": is_key}


@pytest.fixture
def world(monkeypatch):
    return _World().install(monkeypatch)


@pytest.fixture
def graph(monkeypatch):
    return EquipmentVmGraph(_SYSTEMS, assets=["el-qx"]).install(monkeypatch)


def _persist(world, graph) -> int:
    world.reads.clear()
    written = onboard._persist_equipment_value_mappings()
    assert world.unexpected == [] and world.model_calls == []
    return written


def _move_the_pick(world) -> None:
    """The registry's tag pick moves to another field of the same table, with
    other values (the mapper put ``legacy_tag`` on 'equipment-id' instead)."""
    world.registries[1] = _registry("reg-maint", "assets", "legacy_tag")


def _rename_the_cell(world) -> None:
    """The cell registry renames its machine: the name match goes away."""
    world.tables["reg-cell_db"]["cells"][0]["cell_name"] = "Fixture Lathe QX-211"


def _stale_deletes(graph) -> list:
    return graph.statements("_RETIRE_STALE_EQUIPMENT_VMS")


# --------------------------------------------------------------------------- #
# a complete pass retires what it no longer produces
# --------------------------------------------------------------------------- #

def test_a_pick_moved_to_another_field_retires_the_old_rows_and_writes_the_new(world, graph):
    assert _persist(world, graph) == 2
    assert graph.rows() == [NAMES_1, TELEMETRY_1]

    _move_the_pick(world)
    assert _persist(world, graph) == 2
    assert graph.rows() == [NAMES_2, TELEMETRY_2]
    (tele_id,) = [i for i, n in graph.nodes.items() if n["method"] == "value_format_match"]
    assert graph.anchors == {tele_id: "el-qx"}


def test_a_name_match_that_disappears_is_retired(world, graph):
    _persist(world, graph)
    _rename_the_cell(world)
    assert _persist(world, graph) == 1
    assert graph.rows() == [TELEMETRY_1]


def test_an_identical_pass_sweeps_nothing_and_leaves_the_same_rows(world, graph):
    """A generated row a previous pick wrote goes on the first pass; the
    second, identical pass finds nothing stale, deletes nothing and leaves
    every node and anchor as it was."""
    graph.add("vm-equip-0123456789abcdef", from_system="reg-maint", from_value="77-OLD-1",
              to_system="hist-x", to_value="Old-Lathe-OLD-1", concept="equipment",
              method="value_format_match")
    _persist(world, graph)
    assert graph.rows() == [NAMES_1, TELEMETRY_1]
    nodes = {i: dict(n) for i, n in graph.nodes.items()}
    anchors = dict(graph.anchors)
    deletes = len(_stale_deletes(graph))

    assert _persist(world, graph) == 2
    assert len(_stale_deletes(graph)) == deletes == 1
    assert graph.nodes == nodes and graph.anchors == anchors


def test_steward_learned_and_bridged_rows_survive_a_sweep(world, graph):
    """Only GENERATED deterministic rows go. A steward (``subj-``) or learned
    (``vm-learned-``, ``guidance-``) row survives even with a generated
    method; an LLM-bridged row is not swept. A generated row from a system
    the whole discovery did not return, and a name match with one such end,
    are no registry's rows now and go."""
    keep = {
        "subj-0000000000000001": dict(from_system="reg-maint", from_value="77-QX-210",
                                      to_system="hist-x", to_value="Lathe", method="user_confirmed"),
        "vm-learned-00000002": dict(from_system="reg-maint", from_value="77-ZZ-990",
                                    to_system="hist-x", to_value="Saw", method="value_format_match"),
        "guidance-subj-0000003": dict(from_system="reg-cell", from_value="LN2-W2",
                                      to_system="reg-maint", to_value="77-ZZ-990",
                                      method="equipment_name_match"),
        "vm-equip-bridged000004": dict(from_system="reg-maint", from_value="77-ZZ-990",
                                       to_system="hist-x", to_value="Saw Station",
                                       method="semantic_similarity"),
    }
    gone = {
        "vm-equip-gone000000005": dict(from_system="reg-gone", from_value="G-1",
                                       to_system="hist-x", to_value="Fixture-Lathe-QX-210",
                                       method="value_format_match"),
        "vm-equip-halfgone00006": dict(from_system="reg-cell", from_value="LN2-W1",
                                       to_system="reg-gone", to_value="G-1",
                                       method="equipment_name_match"),
    }
    for vm_id, props in {**keep, **gone}.items():
        graph.add(vm_id, concept="equipment", **props)
    _persist(world, graph)
    _move_the_pick(world)
    _persist(world, graph)
    assert set(keep) <= set(graph.nodes)
    assert not set(gone) & set(graph.nodes)
    generated = sorted((n["from_system"], n["from_value"], n["to_system"], n["to_value"],
                        n["method"]) for i, n in graph.nodes.items()
                       if i.startswith("vm-equip-") and i not in keep)
    assert generated == [NAMES_2, TELEMETRY_2]


def test_the_bridge_failing_does_not_keep_a_stale_deterministic_row(world, graph):
    """LLM-bridged rows are outside the sweep, so a bridge failure (swallowed,
    the deterministic matches stand) does not make the pass incomplete."""
    world.tables["reg-maint_db"]["assets"][1]["site_ref"] = "North"
    world.assets.append({"name": "Saw Station", "path": "north/Saw Station",
                         "source_id": "el-saw", "system": "hist-x"})

    def bridge_down(_prompt):
        raise RuntimeError("bedrock_call exhausted all retries")
    world.model = bridge_down
    onboard._persist_equipment_value_mappings()
    _move_the_pick(world)
    world.model_calls.clear()
    onboard._persist_equipment_value_mappings()
    assert world.model_calls  # the leftover reached the (failing) bridge
    assert graph.rows() == [NAMES_2, TELEMETRY_2]


# --------------------------------------------------------------------------- #
# an incomplete pass sweeps nothing; the same pass made complete does
# --------------------------------------------------------------------------- #

def _maint_rows(sql):
    return sql.endswith("FROM assets ORDER BY 1")


def _cell_rows(sql):
    return sql.endswith("FROM cells ORDER BY 1")


_FAILURES = {
    # one registry's own reads
    "registry rows unavailable": ("pick", lambda w: w.pg_faults.append(
        lambda s, sql, n: "unavailable" if _maint_rows(sql) else None)),
    "registry rows cut at the row limit": ("pick", lambda w: w.pg_faults.append(
        lambda s, sql, n: "truncated" if _maint_rows(sql) else None)),
    "registry row read raises": ("pick", lambda w: w.pg_faults.append(
        lambda s, sql, n: "raise" if _maint_rows(sql) else None)),
    "companion fields unreadable": ("pick", lambda w: w.graph_faults.append(
        lambda c, p: "RETURN f.name AS fname" in c and p.get("sid") == "reg-maint")),
    "historians unreadable": ("pick", lambda w: w.graph_faults.append(
        lambda c, p: "(:Asset)-[:FROM_SYSTEM]" in c)),
    "telemetry assets unreadable": ("pick", lambda w: w.graph_faults.append(
        lambda c, p: "HAS_PROPERTY" in c)),
    # the name match
    "name rows unavailable": ("rename", lambda w: w.pg_faults.append(
        lambda s, sql, n: "unavailable" if _cell_rows(sql) and n == 2 else None)),
    "cross match raises": ("rename", None),
    # discovery
    "discovery unreadable": ("pick", lambda w: w.graph_faults.append(
        lambda c, p: "Concept {id: $concept}" in c)),
    "discovery raises": ("pick", lambda w: w.graph_crashes.append(
        lambda c, p: "Concept {id: $concept}" in c)),
    "a tag pick sample unavailable": ("evidence", lambda w: w.pg_faults.append(
        lambda s, sql, n: "unavailable" if "CAST(asset_tag AS TEXT) FROM" in sql
        and "DISTINCT" in sql else None)),
    # the writes
    "a mapping write refused": ("pick", "refuse upsert"),
    "a duplicate retirement refused": ("pick", "refuse retire"),
}


def _change(world, kind) -> None:
    if kind == "rename":
        _rename_the_cell(world)
    elif kind == "evidence":
        # Two eligible fields: the pick reads both samples and reference checks,
        # then takes the identity key.
        world.registries[1:] = [_registry("reg-maint", "assets", "asset_tag", is_key=False),
                                _registry("reg-maint", "assets", "legacy_tag")]
    else:
        _move_the_pick(world)


@pytest.mark.parametrize("failure", sorted(_FAILURES))
def test_an_incomplete_pass_keeps_the_last_complete_set(world, graph, monkeypatch, failure):
    kind, inject = _FAILURES[failure]
    _persist(world, graph)
    last_complete = dict(graph.nodes)
    assert graph.rows() == [NAMES_1, TELEMETRY_1]

    _change(world, kind)
    world.reads.clear()
    with monkeypatch.context() as m:
        if inject == "refuse upsert":
            graph.refuse = lambda name, p: name == "upsert" and p.get("fval") == "LG-QX-210"
        elif inject == "refuse retire":
            graph.refuse = lambda name, p: (name == "_RETIRE_EQUIPMENT_VM_DUPLICATES"
                                            and p.get("fval") == "LG-QX-210")
        elif inject is None:
            def no_match(_named):
                raise RuntimeError("name match failed")
            m.setattr(onboard, "_cross_registry_name_vms", no_match)
        else:
            inject(world)
        onboard._persist_equipment_value_mappings()
    assert set(last_complete) <= set(graph.nodes), failure
    assert _stale_deletes(graph) == [], failure

    # The same change, read and written whole: the stale rows go.
    world.graph_faults.clear()
    world.graph_crashes.clear()
    world.pg_faults.clear()
    graph.refuse = None
    _persist(world, graph)
    expected = [TELEMETRY_1] if kind == "rename" else [NAMES_2, TELEMETRY_2]
    assert graph.rows() == expected, failure


# --------------------------------------------------------------------------- #
# a system that is no registry now
# --------------------------------------------------------------------------- #

TELEMETRY_SHOP = ("reg-shop", "SH-QX-210", "hist-x", "Fixture-Lathe-QX-210",
                  "value_format_match")
NAMES_CELL_SHOP = ("reg-cell", "LN2-W1", "reg-shop", "SH-QX-210", "equipment_name_match")
NAMES_MAINT_SHOP = ("reg-maint", "77-QX-210", "reg-shop", "SH-QX-210", "equipment_name_match")
#: Rows an earlier provision wrote for a registry discovery returns but whose
#: rows never read whole (``reg-part``): its own rows stay, a name match with
#: a system that is no registry now does not.
PART_ROWS = {
    "vm-equip-parttele00001": dict(from_system="reg-part", from_value="TL-QX-210",
                                   to_system="hist-x", to_value="Fixture-Lathe-QX-210",
                                   method="value_format_match"),
    "vm-equip-partname00002": dict(from_system="reg-maint", from_value="77-QX-210",
                                   to_system="reg-part", to_value="TL-QX-210",
                                   method="equipment_name_match"),
}
PART_SHOP = {"vm-equip-partshop00003": dict(from_system="reg-part", from_value="TL-QX-210",
                                            to_system="reg-shop", to_value="SH-QX-210",
                                            method="equipment_name_match")}
#: Rows about ``reg-shop`` the reconcile never sweeps, whatever discovery says.
SHOP_PROTECTED = {
    "subj-shop000000000001": dict(from_system="reg-shop", from_value="SH-QX-210",
                                  to_system="hist-x", to_value="Fixture-Lathe-QX-210",
                                  method="user_confirmed", concept="equipment"),
    "vm-learned-shop00002": dict(from_system="reg-shop", from_value="SH-QX-210",
                                 to_system="hist-x", to_value="Lathe", method="value_format_match",
                                 concept="equipment"),
    "guidance-subj-shop003": dict(from_system="reg-cell", from_value="LN2-W1",
                                  to_system="reg-shop", to_value="SH-QX-210",
                                  method="equipment_name_match", concept="equipment"),
    "vm-equip-shopbridge04": dict(from_system="reg-shop", from_value="SH-QX-210",
                                  to_system="hist-x", to_value="Lathe Station",
                                  method="semantic_similarity", concept="equipment"),
    "vm-equip-shopbatch005": dict(from_system="reg-shop", from_value="SH-QX-210",
                                  to_system="hist-x", to_value="Batch 5",
                                  method="value_format_match", concept="batch"),
}
_DETERMINISTIC = ("value_format_match", "hierarchy_position", "equipment_name_match")


def _add_the_shop(world) -> None:
    """A third registry with the same machine: a telemetry match to the
    historian (its tag carries the asset's designation) and a name match with
    each of the other two."""
    world.registries.append(_registry("reg-shop", "machines", "machine_code"))
    world.fields["reg-shop"] = ["machine_code", "machine_name", "serial_no"]
    world.tables["reg-shop_db"] = {"machines": [
        {"machine_code": "SH-QX-210", "machine_name": _NAME, "serial_no": "SN-0042"}]}


def _add_a_partial_registry(world, graph) -> None:
    """A registry discovery returns whose row reads are never whole, with the
    rows an earlier provision wrote for it."""
    world.registries.append(_registry("reg-part", "tools", "tool_code"))
    world.fields["reg-part"] = ["tool_code", "tool_name"]
    world.tables["reg-part_db"] = {"tools": [{"tool_code": "TL-QX-210", "tool_name": _NAME}]}
    world.pg_faults.append(
        lambda s, sql, n: "unavailable" if sql.endswith("FROM tools ORDER BY 1") else None)
    for vm_id, props in {**PART_ROWS, **PART_SHOP}.items():
        graph.add(vm_id, concept="equipment", **props)


_NO_LONGER_A_REGISTRY = {
    # The mapper's correction moved the shop's last equipment-id field to
    # another concept: the traversal no longer returns the system at all.
    "its last field corrected off the concept": lambda w: w.registries.remove(
        _registry("reg-shop", "machines", "machine_code")),
    # Only fields that describe the machine are left on equipment-id: the
    # pick returns None.
    "only ineligible fields left": lambda w: w.registries.__setitem__(
        slice(None), [r for r in w.registries if r["sys_id"] != "reg-shop"]
        + [_registry("reg-shop", "machines", "machine_name"),
           _registry("reg-shop", "machines", "serial_no")]),
}


def _generated(graph) -> list:
    return sorted((n.get("from_system"), n.get("from_value"), n.get("to_system"),
                   n.get("to_value"), n.get("method")) for i, n in graph.nodes.items()
                  if i.startswith("vm-equip-") and n.get("concept") == "equipment"
                  and n.get("method") in _DETERMINISTIC
                  and i not in PART_ROWS and i not in PART_SHOP)


@pytest.mark.parametrize("change", sorted(_NO_LONGER_A_REGISTRY))
def test_a_system_that_is_no_registry_now_has_its_generated_rows_retired(world, graph, change):
    """One of several registries stops being one: its telemetry rows and
    every name match with it go; the other registries' rows, the protected
    rows about it and the rows of a returned registry read in part stay. A
    second identical pass deletes nothing."""
    _add_the_shop(world)
    _add_a_partial_registry(world, graph)
    for vm_id, props in SHOP_PROTECTED.items():
        graph.add(vm_id, **props)
    assert _persist(world, graph) == 5
    assert _generated(graph) == sorted([NAMES_1, NAMES_CELL_SHOP, NAMES_MAINT_SHOP,
                                        TELEMETRY_1, TELEMETRY_SHOP])
    assert set(PART_ROWS) | set(PART_SHOP) | set(SHOP_PROTECTED) <= set(graph.nodes)

    _NO_LONGER_A_REGISTRY[change](world)
    assert _persist(world, graph) == 2
    assert _generated(graph) == [NAMES_1, TELEMETRY_1]
    assert set(PART_ROWS) | set(SHOP_PROTECTED) <= set(graph.nodes)
    assert not set(PART_SHOP) & set(graph.nodes)
    (tele_id,) = [i for i, n in graph.nodes.items() if i.startswith("vm-equip-")
                  and n.get("from_system") == "reg-maint" and n["method"] == "value_format_match"]
    assert graph.anchors == {tele_id: "el-qx"}

    nodes, anchors = {i: dict(n) for i, n in graph.nodes.items()}, dict(graph.anchors)
    deletes = len(_stale_deletes(graph))
    assert _persist(world, graph) == 2
    assert len(_stale_deletes(graph)) == deletes
    assert graph.nodes == nodes and graph.anchors == anchors


def test_a_whole_discovery_with_no_registry_retires_every_generated_deterministic_row(
        world, graph):
    """The traversal answered, whole, and no System maps an eligible field to
    equipment-id any more: no generated deterministic row has a registry
    behind it. Protected rows stay."""
    _add_the_shop(world)
    for vm_id, props in SHOP_PROTECTED.items():
        graph.add(vm_id, **props)
    _persist(world, graph)
    assert len(_generated(graph)) == 5

    world.registries.clear()
    assert _persist(world, graph) == 0
    assert _generated(graph) == []
    assert set(graph.nodes) == set(SHOP_PROTECTED)
    assert graph.anchors == {}
    assert len(_stale_deletes(graph)) == 1


_GONE_BUT_INCOMPLETE = {
    "discovery unreadable": lambda w, g: w.graph_faults.append(
        lambda c, p: "Concept {id: $concept}" in c),
    "discovery raises": lambda w, g: w.graph_crashes.append(
        lambda c, p: "Concept {id: $concept}" in c),
    "a tag pick sample unavailable": lambda w, g: (
        w.registries.__setitem__(1, _registry("reg-maint", "assets", "asset_tag")),
        w.registries.insert(2, _registry("reg-maint", "assets", "legacy_tag", is_key=False)),
        w.pg_faults.append(lambda s, sql, n: "unavailable"
                           if "CAST(asset_tag AS TEXT) FROM" in sql and "DISTINCT" in sql
                           else None)),
    "a mapping write refused": lambda w, g: setattr(
        g, "refuse", lambda name, p: name == "upsert" and p.get("fval") == "77-QX-210"
        and p.get("tsys") == "hist-x"),
}


@pytest.mark.parametrize("failure", sorted(_GONE_BUT_INCOMPLETE))
def test_a_system_that_is_no_registry_now_keeps_its_rows_after_an_incomplete_pass(
        world, graph, failure):
    """A degraded or failed discovery (or a failed write) proves nothing
    about a system it did not return: every row stays. A traversal that
    degrades to no rows or raises is recorded, so it never reads as a whole
    discovery that found no registry."""
    _add_the_shop(world)
    _persist(world, graph)
    last_complete = dict(graph.nodes)
    _NO_LONGER_A_REGISTRY["its last field corrected off the concept"](world)
    _GONE_BUT_INCOMPLETE[failure](world, graph)
    world.reads.clear()
    onboard._persist_equipment_value_mappings()
    assert set(last_complete) <= set(graph.nodes), failure
    assert _stale_deletes(graph) == [], failure


_SHOP_ROW_GAPS = {
    "no schema": lambda row: row.update(db_schema=None),
    "no table": lambda row: row.update(table_name=None),
    "no field": lambda row: row.update(field_name=""),
}


@pytest.mark.parametrize("gap", sorted(_SHOP_ROW_GAPS))
def test_a_registry_row_without_its_schema_table_or_field_keeps_that_systems_rows(
        world, graph, gap):
    """The traversal answers, but the shop's row comes back without what the
    pass needs: whether the shop is still a registry is unknown, so it is
    not taken for a system that is no registry now and none of its rows
    goes. A second identical pass deletes nothing either."""
    _add_the_shop(world)
    assert _persist(world, graph) == 5
    shop_rows = {i for i, n in graph.nodes.items()
                 if "reg-shop" in (n.get("from_system"), n.get("to_system"))}
    assert len(shop_rows) == 3
    (shop,) = [row for row in world.registries if row["sys_id"] == "reg-shop"]
    _SHOP_ROW_GAPS[gap](shop)
    _persist(world, graph)
    _persist(world, graph)
    assert shop_rows <= set(graph.nodes), gap
    assert _generated(graph) == sorted([NAMES_1, NAMES_CELL_SHOP, NAMES_MAINT_SHOP,
                                        TELEMETRY_1, TELEMETRY_SHOP])


@pytest.mark.parametrize("row", [
    {"sys_id": sys_id, "db_schema": "x_db", "plant": _PLANT, "table_name": "t",
     "field_name": "f", "is_key": False}
    for sys_id in (None, "", "   ", " reg-shop", 123, True)])
def test_a_registry_row_that_names_no_system_makes_the_discovery_incomplete(world, graph, row):
    """A row with no system id cannot be told apart from any system's: the
    discovery is not whole, so nothing is swept, not even the rows of a
    registry it read in full whose pick moved."""
    _add_the_shop(world)
    _persist(world, graph)
    last_complete = dict(graph.nodes)
    _move_the_pick(world)
    world.registries.append(dict(row))
    world.reads.clear()
    upserts = len(graph.statements("upsert"))
    retires = len(graph.statements("_RETIRE_EQUIPMENT_VM_DUPLICATES"))
    assert onboard._persist_equipment_value_mappings() == 0
    assert set(last_complete) <= set(graph.nodes)
    assert _stale_deletes(graph) == []
    # The row may be a hidden candidate of any system: nothing is written either.
    assert graph.statements("upsert")[upserts:] == []
    assert graph.statements("_RETIRE_EQUIPMENT_VM_DUPLICATES")[retires:] == []


# --------------------------------------------------------------------------- #
# the reconcile's own reads and delete
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fault, message", [
    ("count unreadable", "not counted"),
    ("delete refused", "not retired"),
    ("readback unreadable", "unreadable after the retirement"),
    ("one left after the delete", "still found after the retirement"),
])
def test_a_failed_reconcile_is_logged_and_never_taken_for_success(world, graph, caplog,
                                                                   monkeypatch, fault, message):
    _persist(world, graph)
    _move_the_pick(world)
    counts = []

    def unreadable(name, _params):
        if name != "_STALE_EQUIPMENT_VMS_LEFT":
            return False
        counts.append(name)
        return fault == "count unreadable" or (fault == "readback unreadable" and len(counts) == 2)
    graph.unreadable = unreadable
    if fault == "delete refused":
        graph.refuse = lambda name, _p: name == "_RETIRE_STALE_EQUIPMENT_VMS"
    if fault == "one left after the delete":
        graph.left_on_delete = {onboard._equipment_vm_id(
            {"from_system": "reg-maint", "from_value": "77-QX-210", "to_system": "hist-x",
             "to_value": "Fixture-Lathe-QX-210", "concept": "equipment"})}
    outcomes = []
    real = onboard._sweep_stale_equipment_vms
    monkeypatch.setattr(onboard, "_sweep_stale_equipment_vms",
                        lambda vms, keep: outcomes.append(real(vms, keep)) or outcomes[-1])

    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        assert onboard._persist_equipment_value_mappings() == 2
    assert outcomes == [False]
    assert [r for r in caplog.records if message in r.getMessage()
            and "the next provision retries" in r.getMessage()], caplog.text
    if fault in ("count unreadable", "delete refused"):
        assert TELEMETRY_1 in graph.rows() and NAMES_1 in graph.rows()


def test_a_plain_list_says_nothing_so_nothing_is_swept(graph, monkeypatch):
    """A stub, the legacy inert call site or a failed pass hands persist a
    plain list: the reconcile reads nothing and deletes nothing."""
    graph.add("vm-equip-stale000000001", from_system="reg-maint", from_value="77-OLD-1",
              to_system="hist-x", to_value="Old", concept="equipment", method="value_format_match")
    vm = {"from_system": "reg-maint", "from_field": "assets.asset_tag", "from_value": "77-QX-210",
          "to_system": "hist-x", "to_field": "asset_name", "to_value": "Fixture-Lathe-QX-210",
          "to_element_id": "", "to_path": "", "concept": "equipment", "confidence": 0.95,
          "method": "value_format_match"}
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [vm])
    assert onboard._persist_equipment_value_mappings() == 1
    assert "vm-equip-stale000000001" in graph.nodes
    assert graph.statements("_STALE_EQUIPMENT_VMS_LEFT") == [] and _stale_deletes(graph) == []


# --------------------------------------------------------------------------- #
# the recorder
# --------------------------------------------------------------------------- #

def test_a_degraded_graph_read_is_noted_only_inside_a_recording_scope(monkeypatch):
    def refused(_cypher, _params=None):
        raise db.GraphQueryError("ValidationException", "refused")
    monkeypatch.setattr(db, "neptune_query_checked", refused)
    assert db.neptune_query("MATCH (n) RETURN n") == []
    assert db.incomplete_read_count() == 0
    with db.recording_incomplete_reads() as sink:
        assert db.neptune_query("MATCH (n) RETURN n") == []
        db.note_incomplete_read("row_limit")
        assert db.incomplete_read_count() == 2
    assert sink == ["ValidationException", "row_limit"]
    assert db.incomplete_read_count() == 0


_SHOP_COPIES = {
    # Two System nodes answer for the shop: one with a stale schema, one with
    # another plant, one where the same field is and is not the identity key.
    "another schema": {"db_schema": "stale_db"},
    "another plant": {"plant": "Plant South"},
    "key and no key": {"is_key": False},
}


@pytest.mark.parametrize("order", ["as returned", "reversed"])
@pytest.mark.parametrize("copy", sorted(_SHOP_COPIES))
def test_conflicting_copies_of_one_registry_row_keep_that_systems_rows(world, graph, copy, order):
    """The traversal returns the shop's field twice with different metadata:
    which copy is right is unknown, whatever order the graph used, so the
    shop is read as neither a registry read in full nor one that is gone,
    and none of its rows goes, on this pass or the next."""
    _add_the_shop(world)
    assert _persist(world, graph) == 5
    shop_rows = {i for i, n in graph.nodes.items()
                 if "reg-shop" in (n.get("from_system"), n.get("to_system"))}
    (shop,) = [row for row in world.registries if row["sys_id"] == "reg-shop"]
    stale = {**shop, **_SHOP_COPIES[copy]}
    i = world.registries.index(shop)
    world.registries[i:i + 1] = [shop, stale] if order == "as returned" else [stale, shop]
    # The stale copy reads as well as the right one, with another machine.
    world.tables["stale_db"] = {"machines": [
        {"machine_code": "SH-QX-999", "machine_name": "Spare Mill QX-999", "serial_no": "SN-9"}]}
    _persist(world, graph)
    _persist(world, graph)
    assert shop_rows <= set(graph.nodes), (copy, order)
    assert _generated(graph) == sorted([NAMES_1, NAMES_CELL_SHOP, NAMES_MAINT_SHOP,
                                        TELEMETRY_1, TELEMETRY_SHOP])


def test_an_exact_duplicate_registry_row_is_read_once(world, graph):
    """The same row twice (two paths to one field) is one registry: nothing
    is held back and nothing is written twice."""
    _add_the_shop(world)
    (shop,) = [row for row in world.registries if row["sys_id"] == "reg-shop"]
    world.registries.append(dict(shop))
    assert _persist(world, graph) == 5
    assert _generated(graph) == sorted([NAMES_1, NAMES_CELL_SHOP, NAMES_MAINT_SHOP,
                                        TELEMETRY_1, TELEMETRY_SHOP])


@pytest.mark.parametrize("order", ["as returned", "reversed"])
@pytest.mark.parametrize("gap", sorted(_SHOP_ROW_GAPS))
def test_one_row_read_in_part_holds_the_whole_system_back(world, graph, caplog, gap, order):
    """The shop's valid row comes back beside a second shop row with a
    missing value: the second may be its right candidate, so the shop gets
    no pick. Nothing new is written for it, none of its rows goes, and the
    warning that says so is true."""
    _add_the_shop(world)
    assert _persist(world, graph) == 5
    before = {i: dict(n) for i, n in graph.nodes.items()}
    world.fields["reg-shop"].append("asset_ref")
    world.tables["reg-shop_db"]["machines"][0]["asset_ref"] = "SH-NEW-001"
    (shop,) = [row for row in world.registries if row["sys_id"] == "reg-shop"]
    partial = _registry("reg-shop", "machines", "asset_ref")
    _SHOP_ROW_GAPS[gap](partial)
    i = world.registries.index(shop)
    world.registries[i:i + 1] = [shop, partial] if order == "as returned" else [partial, shop]
    upserts = len(graph.statements("upsert"))
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        _persist(world, graph)
    assert {i: n for i, n in graph.nodes.items() if "reg-shop" in (
        n.get("from_system"), n.get("to_system"))} == {
        i: n for i, n in before.items() if "reg-shop" in (n.get("from_system"), n.get("to_system"))}
    shop_writes = [p for p in graph.statements("upsert")[upserts:]
                   if "reg-shop" in (p.get("fsys"), p.get("tsys"))]
    assert shop_writes == [], (gap, order)
    assert any("reg-shop" in r.getMessage() and "no mapping is written or retired"
               in r.getMessage() for r in caplog.records)



# --------------------------------------------------------------------------- #
# an incomplete read writes nothing for the system it was about
# --------------------------------------------------------------------------- #

def _writes(graph, since=0) -> list:
    return [(p.get("fsys"), p.get("fval"), p.get("tsys"), p.get("tval"), p.get("method"))
            for p in graph.statements("upsert")[since:]]


def test_a_failed_tag_sample_writes_nothing_for_that_registry(world, graph):
    """The maintenance registry has two candidate tags and the sample of one
    is unavailable: the pick that remains is a guess, so nothing is written
    for that registry (the cell registry, read whole, still writes nothing
    with it since its only match is to the maintenance system)."""
    world.registries[1:] = [_registry("reg-maint", "assets", "asset_tag", is_key=False),
                            _registry("reg-maint", "assets", "legacy_tag", is_key=False)]
    world.pg_faults.append(lambda s, sql, n: "unavailable"
                           if "CAST(asset_tag AS TEXT) FROM" in sql and "DISTINCT" in sql
                           else None)
    _persist(world, graph)
    assert [w for w in _writes(graph) if "reg-maint" in (w[0], w[2])] == []
    assert _stale_deletes(graph) == []


def test_a_cut_registry_row_read_writes_no_telemetry_for_that_registry(world, graph):
    """The maintenance registry's row read is cut at its limit: the row it
    kept matches, but the rest are unknown, so its telemetry is not written.
    The name match reads the rows whole and still writes."""
    world.pg_faults.append(lambda s, sql, n: "truncated" if _maint_rows(sql) and n == 1 else None)
    _persist(world, graph)
    writes = _writes(graph)
    assert [w for w in writes if w[4] in _DETERMINISTIC and w[4] != "equipment_name_match"] == []
    assert [w for w in writes if w[4] == "equipment_name_match"] == [NAMES_1]


def test_a_cut_name_read_hiding_a_duplicate_writes_no_name_match(world, graph):
    """The cell registry holds the machine's name twice, so the name is not
    one to one and no name match is right. Its name read is cut to the first
    row, which hides the second: nothing is written for that name."""
    world.tables["reg-cell_db"]["cells"].append(
        {"cell_code": "LN2-W3", "cell_name": _NAME, "cell_type": "LATHE", "line": "LINE2"})
    world.pg_faults.append(lambda s, sql, n: "truncated" if _cell_rows(sql) and n == 2 else None)
    _persist(world, graph)
    assert [w for w in _writes(graph) if w[4] == "equipment_name_match"] == []
    assert TELEMETRY_1 in _writes(graph)


def test_the_same_registry_read_whole_writes_its_name_match_only_when_one_to_one(world, graph):
    """Control for the test above: read whole, the duplicated name makes no
    name match either, and the unduplicated world writes it."""
    world.tables["reg-cell_db"]["cells"].append(
        {"cell_code": "LN2-W3", "cell_name": _NAME, "cell_type": "LATHE", "line": "LINE2"})
    _persist(world, graph)
    assert [w for w in _writes(graph) if w[4] == "equipment_name_match"] == []


# --------------------------------------------------------------------------- #
# a system the pass cannot read with SQL is no registry of it
# --------------------------------------------------------------------------- #

def _sql_cannot_read(world, sys_id="hist-x", ctype="SiteWise (Direct)") -> None:
    """A system whose model mapped two of its fields to 'equipment-id' but
    that ``pg`` cannot read: a SiteWise historian (the workshop's
    ``sitewise-direct`` ``asset_hierarchy.asset_id`` and ``process_name``,
    every read ``UndefinedTable``). The fake has no table for it, so a read
    raises, as the evidence and row reads would in production."""
    for field in ("asset_id", "process_name"):
        world.registries.append(dict(_registry(sys_id, "asset_hierarchy", field, is_key=False),
                                     db_schema="sitewise-direct", connection_type=ctype))


def _reads_of(world, schema) -> list:
    return [sql for (s, sql) in world.reads if s == schema]


@pytest.mark.parametrize("ctype", ["SiteWise (Direct)", "MCP Server", "REST API", "Athena"])
def test_a_system_sql_cannot_read_is_never_read_and_leaves_discovery_whole(world, graph, ctype):
    """Its pick reads used to fail every provision, so discovery was never
    whole on a workshop account and a moved pick left its old rows live.
    Now it is no registry of the pass: nothing of it is read, and the sweep
    runs."""
    _sql_cannot_read(world, ctype=ctype)
    assert _persist(world, graph) == 2
    assert graph.rows() == [NAMES_1, TELEMETRY_1]
    assert _reads_of(world, "sitewise-direct") == []

    _move_the_pick(world)
    assert _persist(world, graph) == 2
    assert graph.rows() == [NAMES_2, TELEMETRY_2]
    assert _reads_of(world, "sitewise-direct") == []


def test_a_system_sql_cannot_read_is_not_returned_by_discovery(world):
    _sql_cannot_read(world)
    regs, unread = onboard._discover_equipment_registries_and_unread()
    assert sorted(r["sys_id"] for r in regs) == ["reg-cell", "reg-maint"]
    assert unread == set()


@pytest.mark.parametrize("ctype", [None, "PostgreSQL"])
def test_a_postgresql_or_untyped_system_stays_a_registry(world, ctype):
    """An older System node with no connection type, or a PostgreSQL one, is
    read as before."""
    for reg in world.registries:
        reg["connection_type"] = ctype
    regs, unread = onboard._discover_equipment_registries_and_unread()
    assert sorted(r["sys_id"] for r in regs) == ["reg-cell", "reg-maint"]
    assert unread == set()


def test_one_system_answering_two_connection_types_is_held_back(world, graph):
    """Two System nodes under one id, one PostgreSQL and one not: the pick
    never chooses between them by graph order, so the system is unread and
    keeps its rows."""
    _persist(world, graph)
    world.registries.append(dict(world.registries[1], connection_type="MCP Server"))
    regs, unread = onboard._discover_equipment_registries_and_unread()
    assert [r["sys_id"] for r in regs] == ["reg-cell"] and unread == {"reg-maint"}
    _move_the_pick(world)
    world.registries.append(dict(world.registries[1], connection_type="MCP Server"))
    _persist(world, graph)
    assert TELEMETRY_1 in graph.rows() and NAMES_1 in graph.rows()
    assert _stale_deletes(graph) == []
