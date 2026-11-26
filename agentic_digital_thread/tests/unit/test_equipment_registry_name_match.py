# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equipment identity across registries from equal machine names (ADR-0098).

Offline: no AWS, no graph, no database. Two equipment registries (systems
with an 'equipment-id' field) whose rows carry the same machine name, unique
in each and carrying an equipment designation, at one known site, get one
ValueMapping between their tags: method ``equipment_name_match``, 0.95,
computed provenance, the lower system id on the ``from`` side. No model
call. Synthetic values only.

Seams: ``onboard``'s ``neptune_query`` / ``pg`` / ``bedrock_call`` (the
generator) and its write seams (the persist), monkeypatched.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap

import pytest

import agents.onboard as onboard
from agents.models import ProvisionRequest
from tests.unit._value_mapping_write_seam import route_equipment_checked_reads

_NAME = "Fixture Lathe QX-210"
#: Two synthetic plants, as a System's ``plant`` and as the site key
#: ``_registry_name_rows`` derives from it.
_PLANT, _OTHER_PLANT = "Plant North", "Plant South"
_SITE, _OTHER_SITE = "plantnorth", "plantsouth"


def _reg(sys_id, rows, *, table="machines", tag_field="machine_code", name_field="machine_name",
         site=_SITE, row_sites=None, site_field=None):
    """A registry as ``_registry_name_rows`` returns it: ``site`` is its
    System's plant key (``""`` for a system that serves no one plant, whose
    rows then carry ``row_sites``)."""
    return {"sys_id": sys_id, "db_schema": f"{sys_id}_db", "table": table,
            "tag_field": tag_field, "name_field": name_field, "rows": list(rows),
            "site": site, "row_sites": dict(row_sites or {}), "site_field": site_field}


# --------------------------------------------------------------------------- #
# the pure match
# --------------------------------------------------------------------------- #

def test_equal_unique_designated_names_give_one_mapping_from_the_lower_system_id():
    alpha = _reg("reg-alpha", [("LN2-W1", _NAME), ("LN2-W2", "Bench Grinder 9")])
    beta = _reg("reg-beta", [("77-QX-210", "fixture  lathe qx-210"), ("77-ZZ-990", "Saw 4")],
                table="assets", tag_field="asset_code", name_field="description")
    (vm,) = onboard._cross_registry_name_vms([beta, alpha])
    assert vm == {
        "from_system": "reg-alpha", "from_field": "machines.machine_code", "from_value": "LN2-W1",
        "to_system": "reg-beta", "to_field": "assets.asset_code", "to_value": "77-QX-210",
        "to_element_id": "", "to_path": "", "concept": "equipment", "confidence": 0.95,
        "method": "equipment_name_match", "provenance": vm["provenance"]}
    assert onboard.EQUIPMENT_NAME_METHOD == "equipment_name_match"
    prov = vm["provenance"]
    assert "reg-alpha machines.machine_name" in prov and "reg-beta assets.description" in prov
    assert "(2 and 2 rows compared)" in prov
    assert "both at one site (reg-alpha: the system's plant; reg-beta: the system's plant)" in prov
    for value in ("LN2-W1", "77-QX-210", "QX-210", "Lathe"):
        assert value not in prov, value         # provenance names columns, never values


def test_the_mapping_id_does_not_depend_on_which_system_provisioned_last():
    alpha = _reg("reg-alpha", [("LN2-W1", _NAME)])
    beta = _reg("reg-beta", [("77-QX-210", _NAME)])
    ids = {onboard._equipment_vm_id(vm) for order in ([alpha, beta], [beta, alpha])
           for vm in onboard._cross_registry_name_vms(order)}
    assert len(ids) == 1 and next(iter(ids)).startswith("vm-equip-")


@pytest.mark.parametrize("alpha_rows, beta_rows", [
    ([("LN2-W1", _NAME), ("LN2-W3", _NAME)], [("77-QX-210", _NAME)]),       # twice in one
    ([("LN2-W1", _NAME)], [("77-QX-210", _NAME), ("77-QX-211", _NAME)]),    # twice in the other
    ([("LN2-W1", _NAME), ("LN2-W1", "Spare Lathe QX-777")], [("77-QX-210", _NAME)]),  # tag with two names
])
def test_a_name_that_is_not_on_one_row_of_each_registry_gives_nothing(alpha_rows, beta_rows):
    assert onboard._cross_registry_name_vms(
        [_reg("reg-alpha", alpha_rows), _reg("reg-beta", beta_rows)]) == []


def test_the_same_row_read_twice_is_still_one_row():
    alpha = _reg("reg-alpha", [("LN2-W1", _NAME), ("LN2-W1", _NAME)])
    beta = _reg("reg-beta", [("77-QX-210", _NAME)])
    assert len(onboard._cross_registry_name_vms([alpha, beta])) == 1


@pytest.mark.parametrize("name", ["Fixture Lathe", "Fixture Lathe 210", "Line 3 Lathe"])
def test_equal_names_without_an_equipment_designation_give_nothing(name):
    assert onboard._equipment_designations(name) == set()
    assert onboard._cross_registry_name_vms(
        [_reg("reg-alpha", [("LN2-W1", name)]), _reg("reg-beta", [("77-QX-210", name)])]) == []


def test_three_registries_give_one_mapping_per_matching_pair():
    regs = [_reg("reg-gamma", [("G-1", _NAME)]), _reg("reg-alpha", [("LN2-W1", _NAME)]),
            _reg("reg-beta", [("77-QX-210", _NAME), ("77-ZZ-990", "Saw ZZ-990")]),
            _reg("reg-delta", [("D-7", "Saw ZZ-990")])]
    pairs = sorted((vm["from_system"], vm["to_system"]) for vm in onboard._cross_registry_name_vms(regs))
    assert pairs == [("reg-alpha", "reg-beta"), ("reg-alpha", "reg-gamma"),
                     ("reg-beta", "reg-delta"), ("reg-beta", "reg-gamma")]


def test_registries_without_a_name_column_are_skipped():
    assert onboard._cross_registry_name_vms([None, _reg("reg-beta", [("77-QX-210", _NAME)])]) == []


# --------------------------------------------------------------------------- #
# the site policy (ADR-0098 decision 7): both rows at one known site
# --------------------------------------------------------------------------- #

def test_registries_of_two_plants_never_match_on_equal_names():
    """The same unique, designated machine name at two plants is two
    machines: plants built from one template reuse names and designations."""
    alpha = _reg("reg-alpha", [("LN2-W1", _NAME)], site=_SITE)
    beta = _reg("reg-beta", [("77-QX-210", _NAME)], site=_OTHER_SITE)
    assert onboard._cross_registry_name_vms([alpha, beta]) == []


def test_registries_of_one_plant_match():
    alpha = _reg("reg-alpha", [("LN2-W1", _NAME)], site=_SITE)
    beta = _reg("reg-beta", [("77-QX-210", _NAME)], site=_SITE)
    assert [(vm["from_value"], vm["to_value"]) for vm in onboard._cross_registry_name_vms(
        [alpha, beta])] == [("LN2-W1", "77-QX-210")]


def test_an_all_sites_registry_matches_a_plant_only_on_rows_its_site_column_puts_there():
    """An All Sites registry's row is at the site its own site column names:
    the row at the plant matches, the row at another plant does not."""
    corp = _reg("reg-corp", [("C-1", _NAME), ("C-2", "Spare Lathe QX-777")], site="",
                row_sites={"C-1": _SITE, "C-2": _OTHER_SITE}, site_field="site_ref")
    plant = _reg("reg-plant", [("LN2-W1", _NAME), ("LN2-W2", "Spare Lathe QX-777")])
    (vm,) = onboard._cross_registry_name_vms([plant, corp])
    assert (vm["from_system"], vm["from_value"], vm["to_value"]) == ("reg-corp", "C-1", "LN2-W1")
    assert "reg-corp: machines.site_ref; reg-plant: the system's plant" in vm["provenance"]


def test_an_all_sites_registry_without_a_site_column_matches_nothing():
    corp = _reg("reg-corp", [("C-1", _NAME)], site="", row_sites={})
    plant = _reg("reg-plant", [("LN2-W1", _NAME)])
    assert onboard._cross_registry_name_vms([corp, plant]) == []


def test_a_name_repeated_inside_a_multi_site_registry_matches_nothing():
    """The name is at two sites of the All Sites registry, one of them the
    plant's: not one row of that registry, so no match."""
    corp = _reg("reg-corp", [("C-1", _NAME), ("C-2", _NAME)], site="",
                row_sites={"C-1": _SITE, "C-2": _OTHER_SITE})
    plant = _reg("reg-plant", [("LN2-W1", _NAME)])
    assert onboard._cross_registry_name_vms([corp, plant]) == []


@pytest.mark.parametrize("site_a, site_b, matches", [
    (_SITE, _SITE, True), (_SITE, _OTHER_SITE, False), (_SITE, "", False), ("", "", False)])
def test_two_all_sites_registries_match_only_on_rows_at_one_known_site(site_a, site_b, matches):
    corp_a = _reg("reg-corp-a", [("C-1", _NAME)], site="", row_sites={"C-1": site_a})
    corp_b = _reg("reg-corp-b", [("K-1", _NAME)], site="", row_sites={"K-1": site_b})
    assert len(onboard._cross_registry_name_vms([corp_a, corp_b])) == (1 if matches else 0)


# --------------------------------------------------------------------------- #
# the generator over a fake graph and database
# --------------------------------------------------------------------------- #

def _install(monkeypatch, *, registries, fields, rows, historians=(), assets=(), failing=()):
    """``neptune_query`` routed by cypher fragment, ``pg`` by schema; a schema
    in ``failing`` raises on its row read."""
    def fake_query(cypher, params=None):
        params = params or {}
        if "Concept {id: $concept}" in cypher:
            return [dict(r) for r in registries]
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in fields[params["sid"]]]
        if "(:Asset)-[:FROM_SYSTEM]" in cypher:
            return [{"sys_id": h} for h in historians]
        if "HAS_PROPERTY" in cypher:
            out = [dict(a) for a in assets]
            if "CONTAINS $site" in cypher:
                out = [a for a in out if params["site"] in a["path"].lower()]
            return out
        return []

    def fake_pg(schema, sql, params=None):
        if schema in failing:
            raise RuntimeError("connection reset")
        return {"rows": [list(r) for r in rows[schema]]}

    def no_model(_prompt):
        raise AssertionError("the equipment pass called the model")

    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    monkeypatch.setattr(onboard, "pg", fake_pg)
    monkeypatch.setattr(onboard, "bedrock_call", no_model)


def _registry_row(sys_id, table, field, plant=_PLANT):
    return {"sys_id": sys_id, "db_schema": f"{sys_id}_db", "table_name": table,
            "field_name": field, "is_key": True, "plant": plant}


#: A registry shaped like a site MES equipment view: a tag without a
#: designation, a name, a type, a line, and no site column.
_CELL_FIELDS = ["cell_code", "cell_name", "cell_type", "line", "status"]
_CELL_ROWS = [("LN2-W1", _NAME, "LATHE", None, "LINE2"), ("LN2-W2", "Bench Grinder 9", "GRINDER", None, "LINE2")]
#: A maintenance registry: a tag with the designation, a description, a site.
_MAINT_FIELDS = ["asset_code", "description", "site_ref", "location", "asset_kind"]
_MAINT_ROWS = [("77-QX-210", _NAME, None, "77", "N-C2-L1"), ("77-ZZ-990", "Saw ZZ-990", None, "77", "N-C2-S1")]


def test_a_mes_shaped_registry_and_a_maintenance_registry_pair_without_a_model_call(monkeypatch):
    """The cell registry (no designation in its tags, no site column) gets no
    telemetry mapping and never reaches the model; the maintenance registry
    keeps its telemetry mapping; the two pair by name."""
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code"),
                         _registry_row("reg-maint", "assets", "asset_code")],
             fields={"reg-cell": _CELL_FIELDS, "reg-maint": _MAINT_FIELDS},
             rows={"reg-cell_db": _CELL_ROWS, "reg-maint_db": _MAINT_ROWS},
             historians=["hist-x"],
             assets=[{"name": "Fixture-Lathe-QX-210", "path": "North/Cell2/Fixture-Lathe-QX-210",
                      "source_id": "el-qx", "system": "hist-x"}])
    out = onboard._generate_equipment_value_mappings()
    got = sorted((vm["from_system"], vm["from_value"], vm["to_system"], vm["to_value"], vm["method"])
                 for vm in out)
    assert got == [
        ("reg-cell", "LN2-W1", "reg-maint", "77-QX-210", "equipment_name_match"),
        ("reg-maint", "77-QX-210", "hist-x", "Fixture-Lathe-QX-210", "value_format_match"),
    ]
    assert not [vm for vm in out if vm["from_system"] == "reg-cell" and vm["to_system"] == "hist-x"]


def test_a_registry_whose_name_companion_is_its_tag_gives_no_name_match(monkeypatch):
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code"),
                         _registry_row("reg-maint", "assets", "asset_code")],
             fields={"reg-cell": ["cell_code", "line"], "reg-maint": _MAINT_FIELDS},
             rows={"reg-cell_db": [("LN2-W1", "LN2-W1", None, None, "LINE2")], "reg-maint_db": _MAINT_ROWS})
    regs = onboard._discover_equipment_registries()
    assert [onboard._registry_name_rows(r) is None for r in regs] == [True, False]
    assert onboard._generate_equipment_value_mappings() == []


def test_one_registry_failing_costs_only_its_own_pairs(monkeypatch):
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code"),
                         _registry_row("reg-maint", "assets", "asset_code"),
                         _registry_row("reg-broken", "kit", "kit_code")],
             fields={"reg-cell": _CELL_FIELDS, "reg-maint": _MAINT_FIELDS,
                     "reg-broken": ["kit_code", "kit_name"]},
             rows={"reg-cell_db": _CELL_ROWS, "reg-maint_db": _MAINT_ROWS},
             failing={"reg-broken_db"})
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_system"], vm["to_system"]) for vm in out] == [("reg-cell", "reg-maint")]


def test_one_registry_reads_no_name_rows(monkeypatch):
    """A lone registry has nothing to pair with: no name read at all."""
    reads = []
    _install(monkeypatch, registries=[_registry_row("reg-cell", "cells", "cell_code")],
             fields={"reg-cell": _CELL_FIELDS}, rows={"reg-cell_db": _CELL_ROWS})
    real = onboard._registry_name_rows
    monkeypatch.setattr(onboard, "_registry_name_rows", lambda reg: reads.append(reg) or real(reg))
    assert onboard._generate_equipment_value_mappings() == []
    assert reads == []


@pytest.mark.parametrize("cell_plant, maint_plant, pairs", [
    (_PLANT, _PLANT, 1),             # one plant
    (_PLANT, _OTHER_PLANT, 0),       # two plants
    (_PLANT, "All Sites", 0),        # All Sites, its site column holds a site code
    (_PLANT, "Custom", 0),           # the retired placeholder is no plant
    (_PLANT, None, 0),               # no plant
])
def test_the_system_plant_from_discovery_decides_the_site(monkeypatch, cell_plant, maint_plant,
                                                          pairs):
    """Discovery carries each System's plant to the name match. The maintenance
    registry's site column holds a site code (``77``), which is no plant: as
    an All Sites or unsited registry its rows are at no known plant."""
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code", cell_plant),
                         _registry_row("reg-maint", "assets", "asset_code", maint_plant)],
             fields={"reg-cell": _CELL_FIELDS, "reg-maint": _MAINT_FIELDS},
             rows={"reg-cell_db": _CELL_ROWS, "reg-maint_db": _MAINT_ROWS})
    regs = onboard._discover_equipment_registries()
    assert sorted((r["sys_id"], r["plant"]) for r in regs) == [
        ("reg-cell", cell_plant), ("reg-maint", maint_plant or "")]
    out = onboard._generate_equipment_value_mappings()
    assert len([vm for vm in out if vm["method"] == "equipment_name_match"]) == pairs


#: An All Sites registry: its rows' sites come from its own site column.
_CORP_FIELDS = ["kit_code", "kit_name", "plant_ref", "bay"]
_CORP_ROWS = [("K-1", _NAME, None, _PLANT, "B1"),
              ("K-2", "Spare Lathe QX-777", None, _OTHER_PLANT, "B2"),
              ("K-3", "Edge Lathe QX-888", None, _PLANT, "B3"),
              ("K-3", "Edge Lathe QX-888", None, _OTHER_PLANT, "B3")]
_PLANT_CELL_ROWS = [("LN2-W1", _NAME, "LATHE", None, "LINE2"),
                    ("LN2-W2", "Spare Lathe QX-777", "LATHE", None, "LINE2"),
                    ("LN2-W3", "Edge Lathe QX-888", "LATHE", None, "LINE2")]


def test_an_all_sites_registry_matches_on_the_rows_its_site_column_puts_at_the_plant(monkeypatch):
    """The row at the plant matches; the row at another plant does not; a tag
    whose rows name two plants is at no known site."""
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code"),
                         _registry_row("reg-corp", "kits", "kit_code", "All Sites")],
             fields={"reg-cell": _CELL_FIELDS, "reg-corp": _CORP_FIELDS},
             rows={"reg-cell_db": _PLANT_CELL_ROWS, "reg-corp_db": _CORP_ROWS})
    corp = onboard._registry_name_rows(
        [r for r in onboard._discover_equipment_registries() if r["sys_id"] == "reg-corp"][0])
    assert (corp["site"], corp["site_field"]) == ("", "plant_ref")
    assert corp["row_sites"] == {"K-1": _SITE, "K-2": _OTHER_SITE, "K-3": ""}
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_system"], vm["from_value"], vm["to_system"], vm["to_value"]) for vm in out] \
        == [("reg-cell", "LN2-W1", "reg-corp", "K-1")]


# A row's site column value goes through the same rule as a System's plant
# (``_registry_site_key``): a value that names no one plant is no site, so two
# All Sites registries whose site columns both say "All Sites" are not at one
# known site. Before, the row values were only normalized, and equal wildcard
# text read as one site.

#: Site column values that name no one plant: the All Sites wildcards, the
#: retired placeholder, blank.
_UNSITED = ("All", "All Sites", "All Plants", "Custom", "", None)
#: The same values in other spellings a site column can carry.
_UNSITED_SPELLINGS = ("all", "ALL SITES", "All-Sites", "all_plants", "  All  ", "CUSTOM", "custom")
_UNIT_FIELDS = ["unit_code", "unit_name", "plant_ref", "bay"]


def _two_all_sites_registries(monkeypatch, site_a, site_b):
    """Two All Sites registries (System plant ``All Sites``), each one row
    named ``_NAME``, their site columns holding ``site_a`` and ``site_b``."""
    _install(monkeypatch,
             registries=[_registry_row("reg-corp-a", "kits", "kit_code", "All Sites"),
                         _registry_row("reg-corp-b", "units", "unit_code", "All Sites")],
             fields={"reg-corp-a": _CORP_FIELDS, "reg-corp-b": _UNIT_FIELDS},
             rows={"reg-corp-a_db": [("K-1", _NAME, None, site_a, "B1")],
                   "reg-corp-b_db": [("U-1", _NAME, None, site_b, "B1")]})


def _name_matches() -> list:
    return [(vm["from_value"], vm["to_value"]) for vm in onboard._generate_equipment_value_mappings()
            if vm["method"] == "equipment_name_match"]


@pytest.mark.parametrize("value", _UNSITED + _UNSITED_SPELLINGS)
def test_a_site_column_value_that_names_no_plant_is_no_site(monkeypatch, value):
    _two_all_sites_registries(monkeypatch, value, value)
    regs = onboard._discover_equipment_registries()
    assert [onboard._registry_name_rows(r)["row_sites"] for r in regs] == [{"K-1": ""}, {"U-1": ""}]
    assert onboard._registry_site_key(value) == ""


@pytest.mark.parametrize("value", _UNSITED + _UNSITED_SPELLINGS)
def test_two_all_sites_registries_whose_site_columns_name_no_plant_never_match(monkeypatch, value):
    _two_all_sites_registries(monkeypatch, value, value)
    assert _name_matches() == []


@pytest.mark.parametrize("value", _UNSITED)
def test_an_all_sites_registry_whose_site_column_names_no_plant_never_matches(monkeypatch, value):
    """The value on ONE All Sites registry: against another All Sites
    registry whose row is at the plant (both ways round), and against a plant
    registry."""
    for site_a, site_b in ((value, _PLANT), (_PLANT, value)):
        _two_all_sites_registries(monkeypatch, site_a, site_b)
        assert _name_matches() == [], (site_a, site_b)
    _install(monkeypatch,
             registries=[_registry_row("reg-cell", "cells", "cell_code"),
                         _registry_row("reg-corp", "kits", "kit_code", "All Sites")],
             fields={"reg-cell": _CELL_FIELDS, "reg-corp": _CORP_FIELDS},
             rows={"reg-cell_db": _PLANT_CELL_ROWS, "reg-corp_db": [("K-1", _NAME, None, value, "B1")]})
    assert _name_matches() == []


def test_two_all_sites_registries_whose_site_columns_name_one_plant_match(monkeypatch):
    """The control: the same two registries with both rows at one plant."""
    _two_all_sites_registries(monkeypatch, _PLANT, "PLANT  north")
    assert _name_matches() == [("K-1", "U-1")]
    assert onboard._registry_site_key(_PLANT) == _SITE


# --------------------------------------------------------------------------- #
# persist: provenance only on the mappings that carry one
# --------------------------------------------------------------------------- #

def test_persist_sets_provenance_only_on_a_name_match(monkeypatch):
    telemetry = {"from_system": "reg-maint", "from_field": "assets.asset_code",
                 "from_value": "77-QX-210", "to_system": "hist-x", "to_field": "asset_name",
                 "to_value": "Fixture-Lathe-QX-210", "to_element_id": "el-qx",
                 "to_path": "North/Cell2/Fixture-Lathe-QX-210", "concept": "equipment",
                 "confidence": 0.95, "method": "value_format_match"}
    (name_match,) = onboard._cross_registry_name_vms(
        [_reg("reg-cell", [("LN2-W1", _NAME)]), _reg("reg-maint", [("77-QX-210", _NAME)])])
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: [telemetry, name_match])
    writes = []
    monkeypatch.setattr(onboard, "_db_neptune_write_returning",
                        lambda cypher, params=None: writes.append((cypher, params or {}))
                        or [{"id": (params or {}).get("vmid")}], raising=False)
    monkeypatch.setattr(onboard, "neptune_write", lambda cypher, params=None: True)
    route_equipment_checked_reads(monkeypatch)

    assert onboard._persist_equipment_value_mappings() == 2

    merges = {p["method"]: (c, p) for c, p in writes if "MERGE (vm:ValueMapping" in c}
    cypher, params = merges["equipment_name_match"]
    assert "vm.provenance = $prov" in cypher and params["prov"] == name_match["provenance"]
    assert params["teid"] == "" and "MAPS_TO_ASSET" not in cypher
    cypher, params = merges["value_format_match"]
    assert "provenance" not in cypher and "prov" not in params


# --------------------------------------------------------------------------- #
# post-pass: the result lists the mappings the post-pass wrote
# --------------------------------------------------------------------------- #

def _quiet_neighbours(monkeypatch):
    import agents.metamodel as metamodel
    monkeypatch.setattr(metamodel, "write_metamodel", lambda: None)
    monkeypatch.setattr(onboard, "_seed_demo_curated_annotations", lambda: 0)
    monkeypatch.setattr(onboard, "_replay_accepted_field_annotations", lambda _s: 0)


_WRITTEN = [{"id": "vm-equip-0", "from_system": "reg-cell", "from_value": "LN2-W1",
             "to_system": "reg-maint", "to_value": "77-QX-210", "concept": "equipment",
             "confidence": 0.95, "method": "equipment_name_match"}]


def test_post_pass_result_lists_the_mappings_it_wrote(monkeypatch):
    _quiet_neighbours(monkeypatch)
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: 1)
    reads = []
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda cypher, params=None: reads.append((cypher, params)) or list(_WRITTEN))
    result = {"value_mappings": [], "value_mappings_written": 0}
    onboard._provision_knowledge_post_pass(
        ProvisionRequest(system_id="reg-cell", connection_type="PostgreSQL"), result)
    assert result["value_mappings"] == _WRITTEN and result["value_mappings_written"] == 1
    assert reads == [(onboard._WRITTEN_VALUE_MAPPINGS_CYPHER, {"sid": "reg-cell"})]
    assert "MATCH (vm:ValueMapping) WHERE vm.from_system = $sid OR vm.to_system = $sid" in reads[0][0]


def test_post_pass_keeps_the_earlier_list_when_the_re_read_fails(monkeypatch):
    _quiet_neighbours(monkeypatch)
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: 1)
    tried = []

    def refused(cypher, params=None):
        tried.append(cypher)
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(onboard, "neptune_query_checked", refused)
    earlier = [{"id": "vm-other"}]
    result = {"value_mappings": list(earlier)}
    onboard._provision_knowledge_post_pass(
        ProvisionRequest(system_id="reg-cell", connection_type="PostgreSQL"), result)
    assert tried == [onboard._WRITTEN_VALUE_MAPPINGS_CYPHER]
    assert result["value_mappings"] == earlier


def test_post_pass_does_not_re_read_when_it_wrote_nothing(monkeypatch):
    _quiet_neighbours(monkeypatch)
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: 0)

    def no_read(cypher, params=None):
        raise AssertionError("re-read without a write")
    monkeypatch.setattr(onboard, "neptune_query_checked", no_read)
    result = {"value_mappings": [{"id": "vm-other"}]}
    onboard._provision_knowledge_post_pass(
        ProvisionRequest(system_id="reg-cell", connection_type="PostgreSQL"), result)
    assert result["value_mappings"] == [{"id": "vm-other"}]


def test_post_pass_re_reads_when_the_earlier_list_holds_a_generated_row(monkeypatch):
    """A pass that writes nothing can still retire a generated row the
    earlier list shows (the reconcile), so the list is read again."""
    _quiet_neighbours(monkeypatch)
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: 0)
    reads = []
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda cypher, params=None: reads.append(cypher) or [{"id": "vm-other"}])
    result = {"value_mappings": [{"id": "vm-other"}, {"id": "vm-equip-gone"}],
              "value_mappings_written": 0}
    onboard._provision_knowledge_post_pass(
        ProvisionRequest(system_id="reg-cell", connection_type="PostgreSQL"), result)
    assert reads == [onboard._WRITTEN_VALUE_MAPPINGS_CYPHER]
    assert result["value_mappings"] == [{"id": "vm-other"}]
    assert result["value_mappings_written"] == 0


# --------------------------------------------------------------------------- #
# size and genericity
# --------------------------------------------------------------------------- #

_MATCHER = (
    "_registry_name_rows", "_registry_site_key", "_registry_row_site", "_equipment_name_key",
    "_one_to_one_names", "_registry_site_basis", "_name_match_provenance", "_name_match_vms",
    "_cross_registry_name_vms", "_cross_registry_vms_or_none", "_cross_registry_pass",
)
_NEW_AND_CHANGED = (
    "_generate_equipment_value_mappings", *_MATCHER, "_write_equipment_vm",
    "_dp_written_artifacts", "_refresh_result_value_mappings", "_provision_knowledge_post_pass",
)


@pytest.mark.parametrize("name", _NEW_AND_CHANGED)
def test_new_and_changed_functions_stay_within_50_lines(name):
    assert len(inspect.getsource(getattr(onboard, name)).splitlines()) <= 50, name


def test_the_name_match_code_carries_no_demo_literal():
    """String constants of the new functions (docstrings excluded) name no
    system, schema, table or site: every participant comes from the graph."""
    constants = []
    for name in _MATCHER:
        node = ast.parse(textwrap.dedent(inspect.getsource(getattr(onboard, name)))).body[0]
        body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                 and isinstance(node.body[0].value, ast.Constant)) else node.body
        constants += [c.value for stmt in body for c in ast.walk(stmt)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    text = "\n".join(constants).lower()
    hits = re.findall(r"\b(cmms|mes|sitewise|historian|assets|asset_num|equipment_id|line3)\b", text)
    assert hits == []
