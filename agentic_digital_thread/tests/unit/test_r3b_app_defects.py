# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline regressions for the R3b/R2b walk app defects.

1. The provision §i replay (``onboard._dp_reapply_self_learned``) re-applies
   ONLY accepted items whose applier IS a ValueMapping
   (``self_learning.VALUE_MAPPING_REPLAY_TYPES``). A delivered
   ``guidance_note`` (ADR-0063 — prompt text, never a graph rule) came back
   as a ValueMapping with an empty concept; so could every other non-VM type.
2. Equipment ValueMappings MERGE on the mapping's identity
   (from_system, from_value, to_system, to_value, concept) — the registry
   FIELD the value was read from is not identity, so a re-provision that picks
   a different equipment-id field never accretes a second identical node, and
   the writer retires any prior GENERATED (``vm-equip-``) duplicate.
3. ``scripts/reonboard.py`` defaults ``--delete-deadline`` to 600 s (a VPC
   runtime teardown outlived 180 s).
"""
from __future__ import annotations

import sys
from pathlib import Path

import agents.onboard as onboard
import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_equipment_checked_reads


def _item(proposed_type: str | None, **extra) -> dict:
    item = {"mapping_id": f"m-{proposed_type}-0001", "status": "accepted",
            "from_system": "hist-a", "from_field": "", "from_value": "",
            "to_system": "", "to_field": "", "to_value": "note text",
            "concept": "", "confidence": 1.0}
    if proposed_type is not None:
        item["proposed_type"] = proposed_type
    item.update(extra)
    return item


def _capture_writes(monkeypatch) -> list[tuple[str, dict]]:
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: writes.append((cypher, params or {})) or True)
    # The one-statement ValueMapping write returns the node id it wrote.
    monkeypatch.setattr(onboard, "_db_neptune_write_returning",
                        lambda cypher, params=None: writes.append((cypher, params or {}))
                        or [{"id": (params or {}).get("vmid")}], raising=False)
    return writes


# --------------------------------------------------------------------------- #
# 1 — replay allowlist
# --------------------------------------------------------------------------- #
def test_replay_allowlist_is_exactly_the_value_mapping_appliers():
    assert self_learning.VALUE_MAPPING_REPLAY_TYPES == frozenset(
        {"value_mapping", "crosswalk"})


def test_guidance_note_is_never_replayed_as_a_value_mapping(monkeypatch):
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda _sid: [_item("guidance_note")])
    writes = _capture_writes(monkeypatch)
    assert onboard._dp_reapply_self_learned("hist-a") == 0
    assert not any("ValueMapping" in c for c, _ in writes)


def test_non_graph_rule_types_are_skipped(monkeypatch):
    skipped = ("guidance_note", "insight", "metric_definition", "concept_vocab",
               "concept_overlay", "correlation", "concept_mapping",
               "field_concept_mint", "identity_rule", "join_path", "schema_drift")
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda _sid: [_item(t) for t in skipped])
    writes = _capture_writes(monkeypatch)
    assert onboard._dp_reapply_self_learned("hist-a") == 0
    assert writes == []


def test_value_mapping_and_crosswalk_still_replay(monkeypatch):
    vm = _item("value_mapping", from_value="P1", to_system="sys-b",
               to_value="PLANT-1", concept="site-id")
    legacy = _item(None, mapping_id="m-legacy-0002", from_value="P2",
                   to_system="sys-b", to_value="PLANT-2", concept="site-id")
    xw = _item("crosswalk", mapping_id="m-xw-0003", from_value="A",
               to_system="sys-b", to_value="B", concept="product")
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda _sid: [vm, legacy, _item("guidance_note"), xw])
    # Both endpoint systems are onboarded (a replay needs both: a mapping to
    # an absent system is not replayed; test_offboard_deletes_value_mappings).
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda cypher, params=None: [{"id": "hist-a"}, {"id": "sys-b"}])
    writes = _capture_writes(monkeypatch)
    assert onboard._dp_reapply_self_learned("hist-a") == 3
    merges = [p for c, p in writes if "MERGE (vm:ValueMapping" in c]
    assert [p["tval"] for p in merges] == ["PLANT-1", "PLANT-2", "B"]
    # the crosswalk keeps its acceptance-time provenance
    assert [p["method"] for p in merges] == [
        "user_confirmed", "user_confirmed", "crosswalk_confirmed"]


# --------------------------------------------------------------------------- #
# 2 — equipment ValueMapping MERGE key
# --------------------------------------------------------------------------- #
def _equip_vm(from_field: str = "assets.asset_num") -> dict:
    return {"from_system": "maint-a", "from_field": from_field,
            "from_value": "TAG-441", "to_system": "hist-a",
            "to_field": "asset_name", "to_value": "Mill-441",
            "to_element_id": "el-441", "to_path": "/site/Line3/Mill-441",
            "concept": "equipment", "confidence": 0.95,
            "method": "value_format_match"}


def test_equipment_vm_id_ignores_the_registry_field():
    a = onboard._equipment_vm_id(_equip_vm("assets.asset_num"))
    b = onboard._equipment_vm_id(_equip_vm("assets.asset_tag"))
    assert a == b and a.startswith("vm-equip-")
    for key, other in (("from_system", "maint-b"), ("from_value", "TAG-442"),
                       ("to_system", "hist-b"), ("to_value", "Mill-442"),
                       ("concept", "tool")):
        assert onboard._equipment_vm_id({**_equip_vm(), key: other}) != a


def test_reprovision_with_a_different_field_writes_one_node(monkeypatch):
    runs = iter([[_equip_vm("assets.asset_num")], [_equip_vm("assets.asset_tag")]])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings",
                        lambda: next(runs))
    writes = _capture_writes(monkeypatch)
    route_equipment_checked_reads(monkeypatch)
    onboard._persist_equipment_value_mappings()
    onboard._persist_equipment_value_mappings()
    ids = {p["vmid"] for c, p in writes if "MERGE (vm:ValueMapping" in c}
    assert len(ids) == 1


def test_same_mapping_twice_in_one_run_is_written_once(monkeypatch):
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings",
                        lambda: [_equip_vm("assets.asset_num"),
                                 _equip_vm("assets.asset_tag")])
    writes = _capture_writes(monkeypatch)
    route_equipment_checked_reads(monkeypatch)
    assert onboard._persist_equipment_value_mappings() == 1
    assert sum("MERGE (vm:ValueMapping" in c for c, _ in writes) == 1


def test_persist_retires_prior_generated_duplicates_only(monkeypatch):
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings",
                        lambda: [_equip_vm()])
    writes = _capture_writes(monkeypatch)
    route_equipment_checked_reads(monkeypatch)
    onboard._persist_equipment_value_mappings()
    retire = [(c, p) for c, p in writes if "DETACH DELETE" in c]
    assert len(retire) == 1
    cypher, params = retire[0]
    # generated rows only (steward rows always survive) and never the live id
    assert "vm.id STARTS WITH $prefix" in cypher and "vm.id <> $vmid" in cypher
    assert params["prefix"] == "vm-equip-"
    for key in ("fsys", "fval", "tsys", "tval", "concept"):
        assert f"${key}" in cypher
    assert params["vmid"] == onboard._equipment_vm_id(_equip_vm())
    assert params["fval"] == "TAG-441" and params["tval"] == "Mill-441"


# --------------------------------------------------------------------------- #
# 3 — reonboard delete deadline default
# --------------------------------------------------------------------------- #
def _reonboard_module():
    scripts = str(Path(__file__).resolve().parents[3] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import reonboard
    return reonboard


def test_reonboard_delete_deadline_defaults_to_600():
    import inspect
    reonboard = _reonboard_module()
    args = reonboard._build_arg_parser().parse_args([])
    assert args.delete_deadline == 600.0
    sig = inspect.signature(reonboard.reonboard_systems)
    assert sig.parameters["delete_deadline_s"].default == 600
