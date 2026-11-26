# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the graph-driven equipment ValueMapping generator
and its provision post-pass persist.

No AWS, no Neptune, no network. Covers the task's acceptance matrix:

  1. no 'equipment-id' Concept mapping ⇒ [] + zero pg calls;
  2. companion fields are OPTIONAL — a registry lacking type/site/area still
     matches by identifier evidence (asset name embedded in the tag), unique
     hit only, no LLM without a positional slice; every 'equipment-id'
     registry is matched, not just one;
  3. unique type match inside the site+line hierarchy slice ⇒ confidence 0.95
     / hierarchy_position, carrying ``to_element_id`` + ``to_path``;
  4. cross-site guard — an identical asset name at two sites can NEVER match
     across sites (the row's facility carves the candidate slice);
  5. an LLM pick outside the graph-approved candidate set is DISCARDED
     (a pick inside it lands as 0.85 / semantic_similarity);
  6. persist writes the MAPS_TO_ASSET edge ONLY when ``to_element_id`` is
     non-empty, and SETs ``to_element_id``/``to_path`` on the node;
  7. the post-pass caller runs UN-GATED (no cmms/system-id condition) on a
     non-CMMS provision, and the legacy frozen in-``_do_provision`` call site
     (non-empty schema arg) is INERT.

Seams: ``onboard``'s module globals (``neptune_query``, ``neptune_write``,
``pg``, ``bedrock_call``) are monkeypatched — the fake graph router keys on
stable cypher fragments and honors the site/line parameters the way the
backend would, so the structural path-slice guard is exercised for real.
"""
from __future__ import annotations

import inspect
import json
import re

import pytest

import agents.onboard as onboard
from tests.unit._value_mapping_write_seam import route_equipment_checked_reads
from agents.models import ProvisionRequest


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

def _install_graph(monkeypatch, *, registry_rows=None, table_fields=None,
                   historian_rows=None, assets=None):
    """Route ``onboard.neptune_query`` by cypher fragment. The candidate query
    filters by the ``$site``/``$line`` params like the backend would; the
    python-side fallback (parameterless re-fetch) returns everything."""
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        params = params or {}
        calls.append((cypher, params))
        if "Concept {id: $concept}" in cypher:
            return list(registry_rows or [])
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in (table_fields or [])]
        if "(:Asset)-[:FROM_SYSTEM]" in cypher:
            return list(historian_rows or [])
        if "HAS_PROPERTY" in cypher:
            rows = [dict(a) for a in (assets or [])]
            if "CONTAINS $site" in cypher:
                rows = [a for a in rows if params["site"] in a["path"].lower()]
                if "line" in params:
                    rows = [a for a in rows if params["line"] in a["path"]]
            return rows
        return []

    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    return calls


def _install_pg(monkeypatch, rows=None, *, forbid=False):
    calls: list[tuple] = []

    def fake_pg(schema, sql, params=None):
        if forbid:
            raise AssertionError("pg must not be called on this path")
        calls.append((schema, sql))
        return {"rows": list(rows or [])}

    monkeypatch.setattr(onboard, "pg", fake_pg)
    return calls


def _forbid_bedrock(monkeypatch):
    def boom(_prompt):
        raise AssertionError("bedrock_call must not be reached on this path")
    monkeypatch.setattr(onboard, "bedrock_call", boom)


_REGISTRY = [{"sys_id": "maint-sys", "db_schema": "maint",
              "table_name": "assets", "field_name": "asset_tag"}]
_FIELDS = ["asset_tag", "asset_name", "asset_type", "facility", "area_code"]
_HISTORIANS = [{"sys_id": "hist-a"}, {"sys_id": "maint-sys"}]


# --------------------------------------------------------------------------- #
# 1+2 — discovery no-ops
# --------------------------------------------------------------------------- #

def test_no_equipment_concept_mapping_returns_empty_and_never_reads_rows(monkeypatch):
    _install_graph(monkeypatch, registry_rows=[])
    _install_pg(monkeypatch, forbid=True)
    _forbid_bedrock(monkeypatch)
    assert onboard._generate_equipment_value_mappings() == []


def test_registry_missing_companion_fields_still_matches_by_identifier(monkeypatch):
    """Companions are OPTIONAL: a registry with only tag+name (no type/site/
    area) reads its rows (NULL-projected companions) and matches the one asset
    whose name is embedded in the tag — identifier evidence needs no slice."""
    assets = [{"name": "CMX-441", "path": "CMX-441", "source_id": "el-cmx",
               "system": "hist-a"}]
    _install_graph(monkeypatch, registry_rows=_REGISTRY,
                   table_fields=["asset_tag", "asset_name"],  # no type/site/area
                   historian_rows=_HISTORIANS, assets=assets)
    pg_calls = _install_pg(monkeypatch,
                           rows=[("1001-CMX-441", "5-Axis Mill", None, None, None)])
    _forbid_bedrock(monkeypatch)
    out = onboard._generate_equipment_value_mappings()
    assert len(out) == 1
    assert out[0]["to_element_id"] == "el-cmx"
    assert out[0]["method"] == "value_format_match"
    assert out[0]["confidence"] == 0.95
    sql = pg_calls[0][1]
    assert sql.count("NULL") == 3 and "ORDER BY 1" in sql  # fixed 5-tuple shape


def test_flat_hierarchy_with_site_code_registry_matches_by_identifier(monkeypatch):
    """Live 2026-09-22 shape: one top-level asset (path == name, no site
    segment), registry keyed on a site CODE with a ``location`` column. The
    positional slice is empty; the tag containing the asset name is the
    evidence. Other rows with no embedded identifier land nothing and never
    reach the LLM (no slice ⇒ no positional authority)."""
    assets = [{"name": "CMX-441", "path": "CMX-441", "source_id": "el-cmx",
               "system": "hist-a"}]
    _install_graph(monkeypatch, registry_rows=[
        {"sys_id": "maint-sys", "db_schema": "maint",
         "table_name": "assets", "field_name": "asset_num"}],
        table_fields=["asset_num", "description", "site_id", "location", "asset_type"],
        historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch, rows=[
        ("1001-CMX-441", "5-Axis Milling Center CMX-441", "CNC_MILL", "1001", "PLANT-L3-MC1"),
        ("1001-CGR-502", "Cylindrical Grinder 502", "GRINDER", "1001", "PLANT-L3-GRD2"),
    ])
    _forbid_bedrock(monkeypatch)
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_value"], vm["to_element_id"]) for vm in out] == \
        [("1001-CMX-441", "el-cmx")]
    assert out[0]["from_field"] == "assets.asset_num"


def test_descriptive_asset_name_sharing_the_designation_token_matches(monkeypatch):
    """The historian names the machine ``5-Axis-Milling-CMX-441`` while the
    registry tag is ``1001-CMX-441``: neither whole identifier contains the
    other, but both carry the designation token ``CMX-441``. Token evidence
    must land the edge — whole-string containment alone left the live graph
    with zero MAPS_TO_ASSET edges. A grinder ``CGR-502`` row shares no token
    with it and lands nothing."""
    assets = [{"name": "5-Axis-Milling-CMX-441", "path": "5-Axis-Milling-CMX-441",
               "source_id": "el-cmx", "system": "hist-a"}]
    _install_graph(monkeypatch, registry_rows=[
        {"sys_id": "maint-sys", "db_schema": "maint",
         "table_name": "assets", "field_name": "asset_num"}],
        table_fields=["asset_num", "description", "site_id", "location", "asset_type"],
        historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch, rows=[
        ("1001-CMX-441", "5-Axis Milling Center CMX-441", "CNC_MILL", "1001", "PLANT-L3-MC1"),
        ("1001-CGR-502", "Cylindrical Grinder 502", "GRINDER", "1001", "PLANT-L3-GRD2"),
    ])
    _forbid_bedrock(monkeypatch)
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_value"], vm["to_element_id"], vm["method"]) for vm in out] == \
        [("1001-CMX-441", "el-cmx", "value_format_match")]


@pytest.mark.parametrize("texts,expected", [
    (("5-Axis-Milling-CMX-441",), {"cmx441"}),
    (("1001-CMX-441",), {"cmx441"}),          # the site code 1001 is NOT a token
    (("Cylindrical Grinder 502",), set()),     # bare number: not a designation
    (("Pump", "Main Coolant Pump"), set()),    # vocabulary only
    (("P101", "HQ-FILL-001"), {"p101", "fill001"}),
    (("Line 3",), set()),                     # a single digit is not a designation
])
def test_equipment_designation_tokens(texts, expected):
    assert onboard._equipment_designations(*texts) == expected


def test_tag_without_designation_is_not_evidence_even_when_name_has_it(monkeypatch):
    """Live 2026-09-26: a registry whose 'equipment-id' field was a DATE column
    (``installed_date``) produced ``ValueMapping 2022-06-15 -> <asset>`` at
    0.95, because the designation token came from the companion NAME while
    the tag became the from_value. The from_value is the identifier the
    mapping teaches, so the TAG itself must carry the designation (or contain
    the asset identifier); a companion name can confirm but never substitute.
    A bare row id (``42``) is the same shape and lands nothing either. A date
    column is now also never a registry tag (``_registry_tag_eligible``), so
    the identifier rule is pinned on its own as well."""
    assets = [{"name": "5-Axis-Milling-CMX-441", "path": "5-Axis-Milling-CMX-441",
               "source_id": "el-cmx", "system": "hist-a"}]
    for tag in ("2022-06-15", "42"):
        assert onboard._equipment_identifier_hits(tag, "5-Axis Milling Center CMX-441",
                                                  assets) == []
    _install_graph(monkeypatch, registry_rows=[
        {"sys_id": "maint-sys", "db_schema": "maint",
         "table_name": "equipment_registry", "field_name": "installed_date"}],
        table_fields=["installed_date", "equipment_name", "equipment_type"],
        historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch, rows=[
        ("2022-06-15", "5-Axis Milling Center CMX-441", "CNC_MILL", None, None),
        ("42", "5-Axis Milling Center CMX-441", "CNC_MILL", None, None),
    ])
    _forbid_bedrock(monkeypatch)
    assert onboard._generate_equipment_value_mappings() == []


def test_identifier_present_at_two_sites_is_ambiguous_without_a_slice(monkeypatch):
    """Cross-site guard for the identifier tier: the same asset name under
    two sites with a registry that carries no site ⇒ two hits ⇒ nothing."""
    assets = [
        {"name": "CMX-441", "path": "/north/Line3/CMX-441", "source_id": "el-n",
         "system": "hist-a"},
        {"name": "CMX-441", "path": "/south/Line3/CMX-441", "source_id": "el-s",
         "system": "hist-a"},
    ]
    _install_graph(monkeypatch, registry_rows=_REGISTRY,
                   table_fields=["asset_tag", "asset_name"],
                   historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch, rows=[("1001-CMX-441", "Mill", None, None, None)])
    _forbid_bedrock(monkeypatch)
    assert onboard._generate_equipment_value_mappings() == []


def test_type_word_asset_name_is_not_identifier_evidence(monkeypatch):
    """'Pump' embedded in 'Main Coolant Pump' is vocabulary, not an
    identifier (no digits) — it must NOT match without a positional slice."""
    assets = [{"name": "Pump", "path": "Pump", "source_id": "el-p", "system": "hist-a"}]
    _install_graph(monkeypatch, registry_rows=_REGISTRY,
                   table_fields=["asset_tag", "asset_name"],
                   historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch, rows=[("PUMP-001", "Main Coolant Pump", None, None, None)])
    _forbid_bedrock(monkeypatch)
    assert onboard._generate_equipment_value_mappings() == []


def test_every_equipment_registry_is_matched_not_just_the_tag_field_one(monkeypatch):
    """Two systems map 'equipment-id' (a second maintenance system onboarded
    later, with a ``*_tag`` field). Both get their own pass — the old
    single-registry pick let the second silently replace the first."""
    assets = [
        {"name": "CMX-441", "path": "CMX-441", "source_id": "el-cmx", "system": "hist-a"},
        {"name": "FILL-001", "path": "FILL-001", "source_id": "el-fill", "system": "hist-a"},
    ]
    regs = [
        {"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
         "field_name": "asset_num"},
        {"sys_id": "maint-corp", "db_schema": "maint_corp", "table_name": "assets",
         "field_name": "asset_tag"},
        {"sys_id": "maint-corp", "db_schema": "maint_corp", "table_name": "assets",
         "field_name": "asset_id"},  # numeric PK on the same system — not preferred
    ]
    _install_graph(monkeypatch, registry_rows=regs, table_fields=["asset_num", "asset_tag"],
                   historian_rows=[{"sys_id": "hist-a"}], assets=assets)
    rows_by_schema = {"maint_ind": [("1001-CMX-441", "1001-CMX-441", None, None, None)],
                      "maint_corp": [("HQ-FILL-001", "HQ-FILL-001", None, None, None)]}
    monkeypatch.setattr(onboard, "pg",
                        lambda schema, sql, params=None: {"rows": rows_by_schema[schema]})
    _forbid_bedrock(monkeypatch)
    regs_found = onboard._discover_equipment_registries()
    assert sorted((r["sys_id"], r["tag_field"]) for r in regs_found) == \
        [("maint-corp", "asset_tag"), ("maint-ind", "asset_num")]
    out = onboard._generate_equipment_value_mappings()
    assert sorted((vm["from_system"], vm["to_element_id"]) for vm in out) == \
        [("maint-corp", "el-fill"), ("maint-ind", "el-cmx")]


@pytest.mark.parametrize("name", [
    "_discover_equipment_registries",
    "_discover_equipment_registry",
    "_registry_tag_eligible",
    "_registry_reference_shaped",
    "_pick_registry_field",
    "_registry_companion_fields",
    "_equipment_registry_rows",
    "_equipment_telemetry_assets",
    "_equipment_candidates",
    "_equipment_designations",
    "_equipment_identifier_hits",
    "_match_equipment_rows",
    "_equipment_vms_for_registry",
    "_generate_equipment_value_mappings",
    # the reconcile after a complete pass
    "_equipment_pg_rows",
    "_registry_designation_density",
    "_equipment_vms_for_registry_or_none",
    "_EquipmentPass",
    "_equipment_pass",
    "_cross_registry_pass",
    "_persist_equipment_value_mappings",
    "_sweep_stale_equipment_vms",
    # discovery that never guesses between conflicting rows
    "_discover_equipment_registries_and_unread",
    "_group_registry_rows",
    "_discovery_text",
    "_registry_rows_conflict",
    # systems neither SQL nor a record listing can read are no registry
    "_readable_registries",
    "_registry_table_readable",
    "_registry_field_density",
    "_designation_share",
    "_registry_reference_counts",
])
def test_new_and_changed_equipment_functions_stay_within_50_lines(name):
    func = getattr(onboard, name)
    assert len(inspect.getsource(func).splitlines()) <= 50, name


def test_tag_looking_field_preferred_over_numeric_pk(monkeypatch):
    rows = [{"sys_id": "maint-sys", "db_schema": "maint",
             "table_name": "assets", "field_name": "asset_id"},
            {"sys_id": "maint-sys", "db_schema": "maint",
             "table_name": "assets", "field_name": "asset_tag"}]
    _install_graph(monkeypatch, registry_rows=rows)
    reg = onboard._discover_equipment_registry()
    assert reg["tag_field"] == "asset_tag"


# --------------------------------------------------------------------------- #
# 3 — deterministic match inside the site+line slice
# --------------------------------------------------------------------------- #

def test_unique_type_match_in_site_line_slice_is_hierarchy_position(monkeypatch):
    assets = [
        {"name": "Line1 Coolant Pump", "path": "/northside/Line1/Coolant Pump",
         "source_id": "el-123", "system": "hist-a"},
        {"name": "Line2 Coolant Pump", "path": "/northside/Line2/Coolant Pump",
         "source_id": "el-456", "system": "hist-a"},
    ]
    _install_graph(monkeypatch, registry_rows=_REGISTRY, table_fields=_FIELDS,
                   historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch,
                rows=[("PUMP-001", "Main Coolant Pump", "Pump", "Northside", "NS-L1")])
    _forbid_bedrock(monkeypatch)  # unique hit ⇒ deterministic, no LLM

    out = onboard._generate_equipment_value_mappings()
    assert len(out) == 1
    vm = out[0]
    assert vm["confidence"] == 0.95
    assert vm["method"] == "hierarchy_position"
    assert vm["from_system"] == "maint-sys"
    assert vm["from_field"] == "assets.asset_tag"
    assert vm["from_value"] == "PUMP-001"
    assert vm["to_system"] == "hist-a"
    assert vm["to_element_id"] == "el-123"          # the Line1 asset — the
    assert vm["to_path"] == "/northside/Line1/Coolant Pump"  # -L1 → /Line1/ slice
    assert vm["concept"] == "equipment"


def test_historian_discovery_excludes_registry_itself(monkeypatch):
    _install_graph(monkeypatch, historian_rows=_HISTORIANS)
    assert onboard._discover_equipment_historians("maint-sys") == ["hist-a"]


# --------------------------------------------------------------------------- #
# 4 — cross-site structural guard
# --------------------------------------------------------------------------- #

def test_cross_site_candidate_never_matches(monkeypatch):
    # IDENTICAL asset name at two sites; the row's facility says southside.
    assets = [
        {"name": "CNC Mill", "path": "/northside/Line1/CNC Mill",
         "source_id": "el-north", "system": "hist-a"},
        {"name": "CNC Mill", "path": "/southside/Line1/CNC Mill",
         "source_id": "el-south", "system": "hist-a"},
    ]
    _install_graph(monkeypatch, registry_rows=_REGISTRY, table_fields=_FIELDS,
                   historian_rows=_HISTORIANS, assets=assets)
    # area has NO -L<n> pattern ⇒ no line constraint (heuristic miss is benign)
    _install_pg(monkeypatch,
                rows=[("MILL-9", "Big Mill", "Mill", "Southside", "BLDG-2")])
    _forbid_bedrock(monkeypatch)

    out = onboard._generate_equipment_value_mappings()
    assert len(out) == 1
    assert out[0]["to_element_id"] == "el-south"
    assert "el-north" not in json.dumps(out)


# --------------------------------------------------------------------------- #
# 5 — LLM bridge is candidate-set-validated
# --------------------------------------------------------------------------- #

def _tie_fixture(monkeypatch, llm_response: str):
    # Two same-type candidates inside the slice ⇒ deterministic tie ⇒ LLM.
    assets = [
        {"name": "Coolant Pump A", "path": "/northside/Line1/Pump A",
         "source_id": "el-a", "system": "hist-a"},
        {"name": "Coolant Pump B", "path": "/northside/Line1/Pump B",
         "source_id": "el-b", "system": "hist-a"},
    ]
    _install_graph(monkeypatch, registry_rows=_REGISTRY, table_fields=_FIELDS,
                   historian_rows=_HISTORIANS, assets=assets)
    _install_pg(monkeypatch,
                rows=[("PUMP-7", "Aux Pump", "Pump", "Northside", "NS-L1")])
    monkeypatch.setattr(onboard, "bedrock_call", lambda _p: llm_response)


def test_llm_pick_outside_candidate_set_is_discarded(monkeypatch):
    _tie_fixture(monkeypatch, '{"PUMP-7": "Imaginary Asset"}')
    assert onboard._generate_equipment_value_mappings() == []


def test_llm_pick_inside_candidate_set_lands_at_085(monkeypatch):
    _tie_fixture(monkeypatch, '{"PUMP-7": "Coolant Pump B"}')
    out = onboard._generate_equipment_value_mappings()
    assert len(out) == 1
    assert out[0]["method"] == "semantic_similarity"
    assert out[0]["confidence"] == 0.85
    assert out[0]["to_element_id"] == "el-b"


def test_llm_garbage_response_yields_no_matches(monkeypatch):
    _tie_fixture(monkeypatch, "not json at all")
    assert onboard._generate_equipment_value_mappings() == []


# --------------------------------------------------------------------------- #
# 6 — persist: to_element_id/to_path SETs + guarded MAPS_TO_ASSET edge
# --------------------------------------------------------------------------- #

def test_persist_writes_maps_to_asset_only_for_nonempty_element_id(monkeypatch):
    vms = [
        {"from_system": "maint-sys", "from_field": "assets.asset_tag",
         "from_value": "PUMP-001", "to_system": "hist-a", "to_field": "asset_name",
         "to_value": "Line1 Coolant Pump", "to_element_id": "el-123",
         "to_path": "/northside/Line1/Coolant Pump",
         "concept": "equipment", "confidence": 0.95, "method": "hierarchy_position"},
        {"from_system": "maint-sys", "from_field": "assets.asset_tag",
         "from_value": "MILL-9", "to_system": "hist-a", "to_field": "asset_name",
         "to_value": "CNC Mill", "to_element_id": "", "to_path": "",
         "concept": "equipment", "confidence": 0.85, "method": "semantic_similarity"},
    ]
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: vms)
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: writes.append((cypher, params or {})) or True)
    # The one-statement mapping write returns the id; the checked reads see
    # one Asset per element id and no duplicate left behind.
    monkeypatch.setattr(onboard, "_db_neptune_write_returning",
                        lambda cypher, params=None: writes.append((cypher, params or {}))
                        or [{"id": (params or {}).get("vmid")}], raising=False)
    route_equipment_checked_reads(monkeypatch)

    assert onboard._persist_equipment_value_mappings() == 2

    set_writes = [(c, p) for c, p in writes if "MERGE (vm:ValueMapping" in c]
    assert len(set_writes) == 2
    for cypher, params in set_writes:
        assert "vm.to_element_id = $teid" in cypher
        assert "vm.to_path = $tpath" in cypher
        assert "$" in cypher and "'" not in cypher.replace("''", "")  # parameterized
    assert set_writes[0][1]["teid"] == "el-123"
    assert set_writes[1][1]["teid"] == ""

    edge_writes = [(c, p) for c, p in writes if "MAPS_TO_ASSET" in c]
    assert len(edge_writes) == 1  # ONLY the vm with a non-empty element id
    assert edge_writes[0][1]["aid"] == "el-123"


def test_failed_merge_never_retires_duplicates_and_is_not_counted(monkeypatch):
    """Outside strict writes a failed ``neptune_write`` returns False. Retiring
    the prior generated duplicates after a FAILED MERGE would delete the only
    surviving row for that identity, so the retire (and the count) is gated
    on the MERGE bool."""
    base = {"from_system": "maint-sys", "from_field": "assets.asset_tag",
            "to_system": "hist-a", "to_field": "asset_name", "to_element_id": "",
            "to_path": "", "concept": "equipment", "confidence": 0.9,
            "method": "hierarchy_position"}
    vms = [dict(base, from_value="PUMP-001", to_value="Coolant Pump"),
           dict(base, from_value="MILL-9", to_value="CNC Mill")]
    monkeypatch.setattr(onboard, "_generate_equipment_value_mappings", lambda: vms)
    writes: list[tuple[str, dict]] = []

    def fake_write(cypher, params=None):
        writes.append((cypher, params or {}))
        # The PUMP-001 node MERGE fails; everything else succeeds.
        return not ("MERGE (vm:ValueMapping" in cypher
                    and (params or {}).get("fval") == "PUMP-001")

    monkeypatch.setattr(onboard, "neptune_write", fake_write)
    monkeypatch.setattr(onboard, "_db_neptune_write_returning",
                        lambda cypher, params=None: [{"id": (params or {}).get("vmid")}]
                        if fake_write(cypher, params) else None, raising=False)
    route_equipment_checked_reads(monkeypatch)

    assert onboard._persist_equipment_value_mappings() == 1  # failed one NOT counted

    retires = [p for c, p in writes if "DETACH DELETE" in c]
    assert [p["fval"] for p in retires] == ["MILL-9"]  # no retire for the failed MERGE


# --------------------------------------------------------------------------- #
# 7 — un-gated post-pass caller + inert legacy call site
# --------------------------------------------------------------------------- #

def test_legacy_in_provision_call_site_is_inert(monkeypatch):
    calls = _install_graph(monkeypatch)  # any traversal would be recorded
    _install_pg(monkeypatch, forbid=True)
    _forbid_bedrock(monkeypatch)
    assert onboard._generate_equipment_value_mappings("some cmms schema") == []
    assert calls == []  # ZERO graph reads — the legacy site is retired


def _quiet_post_pass_neighbors(monkeypatch):
    import agents.metamodel as metamodel
    monkeypatch.setattr(metamodel, "write_metamodel", lambda: None)
    monkeypatch.setattr(onboard, "_seed_demo_curated_annotations", lambda: 0)
    monkeypatch.setattr(onboard, "_replay_accepted_field_annotations", lambda _s: 0)


def test_post_pass_runs_equipment_persist_ungated_on_non_cmms_provision(monkeypatch):
    _quiet_post_pass_neighbors(monkeypatch)
    called = []
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings",
                        lambda: called.append(True) or 3)
    body = ProvisionRequest(system_id="rest-sys", connection_type="REST API")
    result: dict = {"value_mappings_written": 1}
    onboard._provision_knowledge_post_pass(body, result)
    assert called == [True]  # no cmms/system-id/catalog-status gate
    assert result["value_mappings_written"] == 4  # additive counter update
    assert result["value_mappings_created"] == 3
    assert result["neptune_nodes_created"] == 3


def test_post_pass_equipment_failure_is_nonfatal(monkeypatch, caplog):
    _quiet_post_pass_neighbors(monkeypatch)

    def boom():
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", boom)
    body = ProvisionRequest(system_id="rest-sys", connection_type="REST API")
    with caplog.at_level("WARNING"):
        onboard._provision_knowledge_post_pass(body, {})  # must not raise
    assert "non-fatal" in caplog.text


def test_post_pass_writes_metamodel_nonfatally(monkeypatch):
    import agents.metamodel as metamodel
    order: list[str] = []
    monkeypatch.setattr(metamodel, "write_metamodel",
                        lambda: order.append("metamodel"))
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings",
                        lambda: order.append("equipment") or 0)
    monkeypatch.setattr(onboard, "_seed_demo_curated_annotations",
                        lambda: order.append("seed") or 0)
    monkeypatch.setattr(onboard, "_replay_accepted_field_annotations",
                        lambda _s: order.append("replay") or 0)
    body = ProvisionRequest(system_id="sys-x", connection_type="PostgreSQL")
    onboard._provision_knowledge_post_pass(body, {})
    assert order == ["metamodel", "equipment", "seed", "replay"]

    # a metamodel failure degrades documentation, never the provision
    def mm_boom():
        raise RuntimeError("metamodel write failed")
    monkeypatch.setattr(metamodel, "write_metamodel", mm_boom)
    onboard._provision_knowledge_post_pass(body, {})  # must not raise


# --------------------------------------------------------------------------- #
# genericity — the new engine code carries no demo literals
# --------------------------------------------------------------------------- #

def test_equipment_generator_source_has_no_demo_literals():
    """Denylist over the code's STRING CONSTANTS (docstrings excluded): no
    hardcoded system ids, catalog keys, or site names steer the generator —
    every participant is graph-discovered. Identifier names (e.g. the
    ``_legacy_cmms_schema`` parameter documenting the retired frozen call
    site) are not routing literals and are out of scope."""
    import ast
    import inspect
    import textwrap
    constants: list[str] = []
    for fn in (
        onboard._generate_equipment_value_mappings,
        onboard._equipment_vms_for_registry,
        onboard._discover_equipment_registries,
        onboard._discover_equipment_registry,
        onboard._registry_tag_eligible,
        onboard._registry_reference_shaped,
        onboard._pick_registry_field,
        onboard._registry_companion_fields,
        onboard._equipment_registry_rows,
        onboard._equipment_telemetry_assets,
        onboard._equipment_candidates,
        onboard._equipment_designations,
        onboard._equipment_identifier_hits,
        onboard._match_equipment_rows,
        onboard._llm_bridge_leftovers,
        onboard._discover_equipment_historians,
        onboard._persist_equipment_value_mappings,
        onboard._equipment_pg_rows,
        onboard._equipment_pass,
        onboard._sweep_stale_equipment_vms,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        node = tree.body[0]
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]  # strip the docstring
        constants.extend(
            c.value for stmt in body for c in ast.walk(stmt)
            if isinstance(c, ast.Constant) and isinstance(c.value, str))
    src = "\n".join(constants)
    for literal in ("cmms", "sitewise-direct", "mes-mil", "mes-det", "mes-cr",
                    "Milwaukee", "Detroit"):
        assert literal not in src, f"demo literal {literal!r} in engine code"


# --------------------------------------------------------------------------- #
# registry field pick — evidence, not Neptune row order
# --------------------------------------------------------------------------- #
# Live 2026-10-03 (two fresh workshop accounts): the mapper tagged SIX
# ``assets`` columns of the plant CMMS as 'equipment-id' (asset_num,
# description, install_date, manufacturer, model_number, serial_number). The
# picker took the first row the graph returned, which was a date or a
# manufacturer, so every registry row read as a value with no designation
# token and the pass wrote ZERO equipment mappings: "0 matches (0 identifier
# evidence ...)" while the one asset the tag names sat in the pool.

_SIX_FIELD_REGISTRY = [
    {"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
     "field_name": f, "is_key": f == "asset_num"}
    for f in ("install_date", "manufacturer", "description", "model_number",
              "serial_number", "asset_num")
]

_SIX_FIELD_SAMPLES = {
    "install_date": ["2022-06-15", "2019-11-05", "2021-07-22"],
    "manufacturer": ["Example Machine Tools", "Example Presses"],
    "description": ["5-Axis Milling Center CMX-441", "Hydraulic Press 402",
                    "Surface Grinder 501"],
    "model_number": ["MC-600", "PR-200", "SG-1224"],
    "serial_number": ["MC600-IND-01", "PR200-IND-01", "SG1224-IND-01"],
    "asset_num": ["1001-CMX-441", "1001-HYP-402", "1001-SGR-501"],
}


def _install_sampling_pg(monkeypatch, samples, *, fail_for=()):
    """``pg`` that answers the picker's per-field sample by the column named
    in the SQL; registry-row reads get the asset_num rows."""
    calls: list[str] = []

    def fake_pg(schema, sql, params=None):
        calls.append(sql)
        for field, values in samples.items():
            if f"SELECT DISTINCT CAST({field} AS TEXT)" in sql:
                if field in fail_for:
                    raise RuntimeError("connection reset")
                return {"rows": [(v,) for v in values]}
        return {"rows": [(v, v, None, None, None) for v in samples["asset_num"]]}

    monkeypatch.setattr(onboard, "pg", fake_pg)
    return calls


def test_registry_pick_uses_value_evidence_not_row_order(monkeypatch):
    _install_graph(monkeypatch, registry_rows=_SIX_FIELD_REGISTRY)
    _install_sampling_pg(monkeypatch, _SIX_FIELD_SAMPLES)
    regs = onboard._discover_equipment_registries()
    assert [(r["sys_id"], r["tag_field"]) for r in regs] == [("maint-ind", "asset_num")]


def test_registry_pick_never_takes_a_model_or_serial_field_even_as_the_identity_key(monkeypatch):
    """model_number and serial_number are as designation-dense as asset_num,
    and here serial_number is even the table's identity key: neither is ever
    the tag (``_registry_tag_eligible``); the identifier-shaped field is."""
    rows = [dict(r) for r in _SIX_FIELD_REGISTRY if r["field_name"] in
            ("model_number", "serial_number", "asset_num")]
    for r in rows:
        r["is_key"] = r["field_name"] == "serial_number"
    _install_graph(monkeypatch, registry_rows=rows)
    _install_sampling_pg(monkeypatch, _SIX_FIELD_SAMPLES)
    regs = onboard._discover_equipment_registries()
    assert regs[0]["tag_field"] == "asset_num"


def test_registry_pick_prefers_the_identity_key_among_designation_dense_fields(monkeypatch):
    """Two identifier-shaped, designation-dense fields: the table's identity
    key (IDENTIFIED_BY) breaks the tie, never the name."""
    rows = [{"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
             "field_name": f, "is_key": f == "asset_num"} for f in ("asset_num", "unit_code")]
    _install_graph(monkeypatch, registry_rows=rows)
    _install_sampling_pg(monkeypatch, dict(_SIX_FIELD_SAMPLES,
                                           unit_code=["UC-100", "UC-200", "UC-300"]))
    regs = onboard._discover_equipment_registries()
    assert regs[0]["tag_field"] == "asset_num"


@pytest.mark.parametrize("fields", [
    ("install_date", "manufacturer", "description", "model_number", "serial_number"),
    ("installed_date",),
    ("asset_name", "serial_number"),
])
def test_a_system_whose_equipment_id_fields_all_describe_the_asset_is_no_registry(monkeypatch,
                                                                                  fields):
    rows = [{"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
             "field_name": f, "is_key": False} for f in fields]
    _install_graph(monkeypatch, registry_rows=rows)
    calls = _install_sampling_pg(monkeypatch, _SIX_FIELD_SAMPLES)
    assert onboard._discover_equipment_registries() == []
    assert calls == []  # nothing sampled, no row read


def test_registry_pick_falls_back_to_names_when_sampling_fails(monkeypatch):
    rows = [dict(r, is_key=False) for r in _SIX_FIELD_REGISTRY]
    _install_graph(monkeypatch, registry_rows=rows)
    _install_sampling_pg(monkeypatch, _SIX_FIELD_SAMPLES,
                         fail_for=tuple(_SIX_FIELD_SAMPLES))
    regs = onboard._discover_equipment_registries()
    # No evidence: a date, a name or a maker's name is never the tag; *_num wins.
    assert regs[0]["tag_field"] == "asset_num"


def test_registry_pick_never_samples_a_lone_candidate(monkeypatch):
    graph = _install_graph(monkeypatch, registry_rows=_REGISTRY)
    calls = _install_sampling_pg(monkeypatch, {"asset_num": ["1001-CMX-441"]})
    onboard._discover_equipment_registries()
    assert calls == []
    assert not [c for c, _p in graph if "RETURN f.name AS fname" in c]  # no reference check either


# --------------------------------------------------------------------------- #
# registry field pick: the equipment reference table, never the graph's row order
# --------------------------------------------------------------------------- #
# The tag column of an equipment reference table is also a column of the fact tables
# that reference it (a run, a reading, a work order), and the mapper can put
# all of them on 'equipment-id'. Same field name, same values, same density,
# no identity key: the pick kept whichever row the graph returned first, and a
# fact table has no name column, so the cross-registry name match lost the
# system. A table with a name column of its own and one row per tag value is
# the reference table (``_registry_reference_shaped``).

_LIMIT = onboard._REGISTRY_REFERENCE_SAMPLE_LIMIT
#: table -> (Fields, tag per row, name per row or None without a name column)
_CELL_TABLES = {
    # the reference table: a name column, one row per tag
    "cells": (["cell_code", "cell_name", "cell_type", "line"], ["LN1-W1", "LN2-W1", "LN3-W1"],
              ["Weld Cell One", "Weld Cell Two", "Weld Cell Three"]),
    # a fact table: no name column, the tag repeated per run
    "cell_runs": (["run_no", "cell_code", "qty", "started"],
                  ["LN1-W1", "LN1-W1", "LN2-W1", "LN3-W1", "LN3-W1"], None),
    # a name column, but the tag repeated per job
    "cell_jobs": (["job_no", "cell_code", "job_name"], ["LN1-W1", "LN2-W1", "LN2-W1"],
                  ["Night", "Night", "Day"]),
    # one row per tag, but no name column
    "cell_specs": (["cell_code", "spec_note"], ["LN1-W1", "LN2-W1", "LN3-W1"], None),
    # a second reference table
    "cell_reference": (["cell_code", "cell_label_name"], ["LN1-W1", "LN2-W1"], ["Cell A", "Cell B"]),
    # a small fact table: one row per tag, but its descriptive column repeats
    "cell_shifts": (["shift_no", "cell_code", "shift_name"], ["LN1-W1", "LN2-W1", "LN3-W1"],
                    ["Night", "Night", "Day"]),
    # unique tags and names, but more rows than the check reads in full
    "cell_big": (["cell_code", "cell_name"], [f"C{i}" for i in range(_LIMIT + 1)],
                 [f"Cell {i}" for i in range(_LIMIT + 1)]),
    # unique over the first _LIMIT rows, the next row repeats the first tag
    "cell_late_dup": (["cell_code", "cell_name"], [f"C{i}" for i in range(_LIMIT)] + ["C0"],
                      [f"Cell {i}" for i in range(_LIMIT + 1)]),
    # the real registry, keyed in the schema, too large to read in full
    "cell_registry": (["cell_code", "cell_name"], [f"LN{i}-W1" for i in range(_LIMIT + 1)],
                      [f"Weld Cell {i}" for i in range(_LIMIT + 1)]),
    # a fact table whose column holds other equipment's ids (gauges), which
    # carry a designation where the cells' tags carry none
    "cell_checks": (["check_no", "cell_code", "measured"], ["CMM-01", "CMM-01", "CMM-02"], None),
}


def _cell_rows(*tables, key=()) -> list:
    return [{"sys_id": "cell-sys", "db_schema": "cells_db", "table_name": t,
             "field_name": "cell_code", "is_key": t in key} for t in tables]


def _install_cell_tables(monkeypatch, registry_rows, *, count_fails=False):
    """The graph names each table's own Fields; ``pg`` answers the density
    sample and the reference check's counts from ``_CELL_TABLES``, honouring the
    query's LIMIT."""
    def fake_query(cypher, params=None):
        if "Concept {id: $concept}" in cypher:
            return [dict(r) for r in registry_rows]
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in _CELL_TABLES[params["tbl"]][0]]
        return []

    def fake_pg(schema, sql, params=None):
        _, tags, names = _CELL_TABLES[re.search(r"FROM (\w+)", sql).group(1)]
        limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
        if sql.startswith("SELECT COUNT(*)"):
            if count_fails:
                raise RuntimeError("connection reset")
            tags = tags[:limit]
            names = (names or [None] * len(tags))[:limit]
            named = [n for n in names if n is not None]
            return {"rows": [(len(tags), len(set(tags)), len(named), len(set(named)))]}
        if sql.startswith("SELECT DISTINCT CAST("):
            return {"rows": [(v,) for v in list(dict.fromkeys(tags))[:limit]]}
        raise AssertionError(sql)

    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    monkeypatch.setattr(onboard, "pg", fake_pg)


def _picks_both_ways(monkeypatch, rows, **kw) -> list:
    """The picked table with the graph rows in order, then reversed."""
    out = []
    for order in (rows, rows[::-1]):
        _install_cell_tables(monkeypatch, order, **kw)
        (reg,) = onboard._discover_equipment_registries()
        out.append(reg["table"])
    return out


@pytest.mark.parametrize("other", ["cell_runs", "cell_jobs", "cell_specs"])
def test_registry_pick_takes_the_reference_table_in_either_graph_order(monkeypatch, other):
    assert _picks_both_ways(monkeypatch, _cell_rows("cells", other)) == ["cells", "cells"]


def test_registry_pick_takes_the_reference_table_among_several_fact_tables(monkeypatch):
    rows = _cell_rows("cell_runs", "cells", "cell_jobs", "cell_specs")
    assert _picks_both_ways(monkeypatch, rows) == ["cells", "cells"]


def test_registry_pick_breaks_a_full_evidence_tie_by_table_name_in_either_order(monkeypatch):
    """Two reference tables, nothing else to tell them apart: the same one both ways."""
    assert _picks_both_ways(monkeypatch, _cell_rows("cell_reference", "cells")) == ["cells", "cells"]


def test_registry_pick_reads_no_reference_table_when_the_count_fails(monkeypatch):
    """A failed reference check is no evidence (0 for every table); the pick
    still returns one registry, and the same one both ways."""
    rows = _cell_rows("cell_runs", "cells")
    assert _picks_both_ways(monkeypatch, rows, count_fails=True) == ["cells", "cells"]
    _install_cell_tables(monkeypatch, rows, count_fails=True)
    assert onboard._registry_reference_shaped(rows[1]) == 0


def test_registry_reference_check_reads_the_name_column_and_the_tag_counts(monkeypatch):
    _install_cell_tables(monkeypatch, [])
    assert {t: onboard._registry_reference_shaped(_cell_rows(t)[0]) for t in _CELL_TABLES} == {
        "cells": 1, "cell_runs": 0, "cell_jobs": 0, "cell_specs": 0, "cell_reference": 1,
        "cell_shifts": 0, "cell_big": 0, "cell_late_dup": 0, "cell_registry": 0,
        "cell_checks": 0}


@pytest.mark.parametrize("small", ["cells", "cell_reference"])
def test_registry_pick_keeps_the_identity_key_over_a_small_reference_shaped_table(monkeypatch, small):
    """Reference shape is read from rows, so a small table that
    happens to hold each tag and name once reads as a reference table while the real
    registry, keyed in the schema but too large to read in full, reads 0.
    The identity key wins, in either graph order."""
    rows = _cell_rows("cell_registry", small, key=("cell_registry",))
    assert _picks_both_ways(monkeypatch, rows) == ["cell_registry", "cell_registry"]


def test_registry_pick_uses_reference_shape_only_when_no_key_settles_it(monkeypatch):
    """No identity key on either table: the reference shape still decides."""
    rows = _cell_rows("cell_runs", "cells")
    assert _picks_both_ways(monkeypatch, rows) == ["cells", "cells"]
    rows = _cell_rows("cell_runs", "cells", key=("cell_runs",))
    assert _picks_both_ways(monkeypatch, rows) == ["cell_runs", "cell_runs"]


def test_registry_reference_check_needs_the_whole_table_read(monkeypatch):
    """Uniqueness is proven over the whole table, never over a window: one
    row more than the bound is fetched, and a table that has it is no reference table,
    whether that row repeats a tag (cell_late_dup) or not (cell_big)."""
    _install_cell_tables(monkeypatch, [])
    for table in ("cell_big", "cell_late_dup"):
        assert onboard._registry_reference_shaped(_cell_rows(table)[0]) == 0, table


def test_registry_reference_check_needs_a_name_per_row(monkeypatch):
    """A small fact table with one row per tag but a descriptive column that
    repeats (a shift or job name) is no reference table: the name must be unique too."""
    _install_cell_tables(monkeypatch, [])
    assert onboard._registry_reference_shaped(_cell_rows("cell_shifts")[0]) == 0
    assert _picks_both_ways(monkeypatch, _cell_rows("cell_shifts", "cells")) == ["cells", "cells"]


def test_registry_pick_without_a_reference_table_keeps_the_identity_key_winner(monkeypatch):
    """When no candidate is a proven reference table the earlier evidence decides as
    before: here the table's identity key, in either graph order."""
    rows = _cell_rows("cell_runs", "cell_specs", key=("cell_runs",))
    assert _picks_both_ways(monkeypatch, rows) == ["cell_runs", "cell_runs"]


@pytest.mark.parametrize("key", [(), ("cells",)])
def test_registry_pick_never_takes_a_designation_dense_fact_column_over_the_reference(monkeypatch,
                                                                                   key):
    """A fact table's column that holds OTHER equipment's ids
    (gauges, ``CMM-01``) carries a designation where the reference table's tags
    (``LN2-W1``) carry none. Density read first took it (the frozen MES's
    ``quality_checks.measurement_equipment`` in all five replays of its map),
    and the system left the name match: no name column there. The table is
    settled first (key, then reference shape), with or without the reference table's key."""
    rows = _cell_rows("cells", "cell_checks", key=key)
    assert _picks_both_ways(monkeypatch, rows) == ["cells", "cells"]


def test_registry_pick_keeps_the_dense_tag_over_its_own_tables_numeric_key(monkeypatch):
    """The key settles the TABLE, not the field: in one table, an integer
    identity key beside the designation-dense tag (the corporate CMMS shape,
    ``assets.asset_id`` beside ``asset_tag``) leaves the tag to density."""
    rows = [{"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
             "field_name": f, "is_key": f == "asset_id"} for f in ("asset_id", "asset_num")]
    _install_sampling_pg(monkeypatch, dict(_SIX_FIELD_SAMPLES, asset_id=["17", "18", "19"]))
    for order in (rows, rows[::-1]):
        _install_graph(monkeypatch, registry_rows=order)
        assert onboard._discover_equipment_registries()[0]["tag_field"] == "asset_num"


def test_registry_reference_check_reads_a_bounded_number_of_rows(monkeypatch):
    """A historian's readings table can hold millions of rows: the count is
    taken over at most ``_REGISTRY_REFERENCE_SAMPLE_LIMIT`` + 1 of them."""
    _install_cell_tables(monkeypatch, [])
    reads = []
    real = onboard.pg
    monkeypatch.setattr(onboard, "pg", lambda schema, sql, params=None: reads.append(sql)
                        or real(schema, sql, params))
    assert onboard._registry_reference_shaped(_cell_rows("cell_jobs")[0]) == 0
    (count,) = [s for s in reads if s.startswith("SELECT COUNT(*)")]
    assert re.search(rf"\bLIMIT {onboard._REGISTRY_REFERENCE_SAMPLE_LIMIT + 1}\)", count), count


def test_six_field_registry_end_to_end_writes_the_cmx_mapping(monkeypatch):
    """The live shape, whole pass: six equipment-id columns, the asset in the
    pool, one mapping out (identifier evidence), not zero."""
    assets = [{"name": "5-Axis-Milling-CMX-441",
               "path": "Northside/Machining/Cell7/5-Axis-Milling-CMX-441",
               "source_id": "el-cmx", "system": "hist-a"},
              {"name": "Cell7Packaging", "path": "Northside/Machining/Cell7/Cell7Packaging",
               "source_id": "el-pack", "system": "hist-a"}]
    _install_graph(monkeypatch, registry_rows=_SIX_FIELD_REGISTRY,
                   table_fields=["asset_num", "description", "site_id", "location",
                                 "asset_type"],
                   historian_rows=[{"sys_id": "hist-a"}], assets=assets)
    rows = [("1001-CMX-441", "5-Axis Milling Center CMX-441", "CNC_MILL", "1001", "NS-C7-MC1"),
            ("1001-HYP-402", "Hydraulic Press 402", "PRESS", "1001", "NS-C7-PRS1")]

    def fake_pg(schema, sql, params=None):
        for field, values in _SIX_FIELD_SAMPLES.items():
            if f"SELECT DISTINCT CAST({field} AS TEXT)" in sql:
                return {"rows": [(v,) for v in values]}
        return {"rows": rows}

    monkeypatch.setattr(onboard, "pg", fake_pg)
    _forbid_bedrock(monkeypatch)
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_value"], vm["to_element_id"], vm["method"]) for vm in out] == \
        [("1001-CMX-441", "el-cmx", "value_format_match")]


# --------------------------------------------------------------------------- #
# the mapping-review trap gates the cross-registry name match (ADR-0098)
# --------------------------------------------------------------------------- #
# The workshop review step plants the maintenance tag column on another
# concept; the attendee moves it back to 'equipment-id'. With the tag off, the
# other five measured columns are still on 'equipment-id'. Before the
# eligibility rule the designation-dense serial_number won the pick and the
# name match tied the cell registry's machine to a SERIAL NUMBER before anyone
# fixed the tag.

_TRAP_PLANT = "Plant North"
_SIX_FIELD_ASSETS = [
    {"asset_num": "1001-CMX-441", "description": "5-Axis Milling Center CMX-441",
     "install_date": "2022-06-15", "manufacturer": "Example Machine Tools",
     "model_number": "MC-600", "serial_number": "MC600-IND-01", "site_id": "1001",
     "location": "NS-C7-MC1", "asset_type": "CNC_MILL"},
    {"asset_num": "1001-HYP-402", "description": "Hydraulic Press 402",
     "install_date": "2019-11-05", "manufacturer": "Example Presses",
     "model_number": "PR-200", "serial_number": "PR200-IND-01", "site_id": "1001",
     "location": "NS-C7-PRS1", "asset_type": "PRESS"},
]
_CELLS = [{"cell_code": "LN3-W1", "cell_name": "5-Axis Milling Center CMX-441",
           "cell_type": "CNC_MILL", "line": "LINE3"},
          {"cell_code": "LN1-W1", "cell_name": "Turning Center 310", "cell_type": "CNC_LATHE",
           "line": "LINE1"}]
_TABLES = {("maint_ind", "assets"): _SIX_FIELD_ASSETS, ("cells_db", "cells"): _CELLS}


def _projection_pg(schema, sql, params=None):
    """``pg`` over ``_TABLES``: answers the picker's DISTINCT sample and the
    registry row read by the columns the SQL names (``NULL`` as None)."""
    sample = re.match(r"SELECT DISTINCT CAST\((\w+) AS TEXT\) FROM (\w+)", sql)
    if sample:
        col, table = sample.groups()
        return {"rows": [(v,) for v in dict.fromkeys(r[col] for r in _TABLES[(schema, table)])]}
    cols, table = re.match(r"SELECT (.+) FROM (\w+) ORDER BY 1", sql).groups()
    names = [c.strip() for c in cols.split(",")]
    return {"rows": sorted(tuple(None if c == "NULL" else r[c] for c in names)
                           for r in _TABLES[(schema, table)])}


def _trap_graph(monkeypatch, maint_fields_on_equipment_id):
    registry_rows = [{"sys_id": "maint-ind", "db_schema": "maint_ind", "table_name": "assets",
                      "field_name": f, "is_key": f == "asset_num", "plant": _TRAP_PLANT}
                     for f in maint_fields_on_equipment_id]
    registry_rows.append({"sys_id": "cell-sys", "db_schema": "cells_db", "table_name": "cells",
                          "field_name": "cell_code", "is_key": True, "plant": _TRAP_PLANT})
    fields = {"maint-ind": list(_SIX_FIELD_ASSETS[0]), "cell-sys": list(_CELLS[0])}

    def fake_query(cypher, params=None):
        if "Concept {id: $concept}" in cypher:
            return [dict(r) for r in registry_rows]
        if "RETURN f.name AS fname" in cypher:
            return [{"fname": f} for f in fields[params["sid"]]]
        return []  # no historian: only the name match can write

    monkeypatch.setattr(onboard, "neptune_query", fake_query)
    monkeypatch.setattr(onboard, "pg", _projection_pg)
    _forbid_bedrock(monkeypatch)


def test_the_review_trap_gates_the_name_match_until_the_tag_is_corrected(monkeypatch):
    six = [r["field_name"] for r in _SIX_FIELD_REGISTRY]
    trapped = [f for f in six if f != "asset_num"]

    _trap_graph(monkeypatch, trapped)
    assert [r["sys_id"] for r in onboard._discover_equipment_registries()] == ["cell-sys"]
    assert onboard._generate_equipment_value_mappings() == []

    _trap_graph(monkeypatch, six)
    out = onboard._generate_equipment_value_mappings()
    assert [(vm["from_system"], vm["from_field"], vm["from_value"], vm["to_system"],
             vm["to_field"], vm["to_value"], vm["method"]) for vm in out] == [
        ("cell-sys", "cells.cell_code", "LN3-W1", "maint-ind", "assets.asset_num",
         "1001-CMX-441", "equipment_name_match")]
