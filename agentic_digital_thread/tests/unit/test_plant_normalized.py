# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One plant value everywhere: trimmed, and the retired form placeholder reads as no plant.

Before Connect New System had a Plant field it handed the wizard the placeholder plant
``'Custom'`` for every connection type, and provision wrote it onto the System node. A
restart rebuilds the catalog entry from that node, so a registration made then keeps
``'Custom'`` after an upgrade and a card re-onboard sends it again. Two such systems would
still compete as if they shared a site. And a blank-but-not-empty plant (``'   '``, which
the HTTP route and the chat tool accept) was unsited to the authority guards while the
graph writers still made a Plant node and a LOCATED_AT edge for it.

So the plant is normalized once, where it enters: registration, the catalog (every entry
added or rebuilt), and the provision state every graph writer reads. A re-provision also
drops LOCATED_AT edges to any plant other than the current one, so a system re-onboarded
without a plant (or at a different one) isn't left at its old site.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.catalog as catalog_module
import agents.onboard as onboard
from agents.catalog import SystemCatalog, normalize_plant


@pytest.mark.parametrize("raw, want", [
    ("North Plant", "North Plant"),
    ("  North Plant ", "North Plant"),
    ("All Sites", "All Sites"),
    ("", ""),
    ("   ", ""),
    (None, ""),
    ("Custom", ""),
    (" Custom ", ""),
    ("Custom Works", "Custom Works"),
])
def test_normalize_plant(raw, want):
    assert normalize_plant(raw) == want


def test_a_catalog_entry_never_holds_the_placeholder_or_blank():
    cat = SystemCatalog([])
    cat.add({"id": "a", "name": "A", "plant": "Custom", "status": "registered"})
    cat.add({"id": "b", "name": "B", "plant": "   ", "status": "registered"})
    cat.add({"id": "c", "name": "C", "plant": " North Plant ", "status": "registered"})
    assert [cat.get(s)["plant"] for s in ("a", "b", "c")] == ["", "", "North Plant"]


def test_a_restart_rebuilds_a_legacy_entry_without_the_placeholder():
    cat = SystemCatalog([])
    cat._graph_rows = lambda: [{"id": "legacy-cmms", "name": "Legacy CMMS", "plant": "Custom",
                                "connection_type": "Tulip (Tables API)", "status": "onboarded"}]
    assert cat.hydrate() == 1
    assert cat.get("legacy-cmms")["plant"] == ""


def test_a_half_removed_legacy_entry_is_rebuilt_without_the_placeholder():
    cat = SystemCatalog([])
    entry = cat.ensure_removal_failed({"id": "legacy-cmms", "name": "Legacy CMMS", "plant": "Custom",
                                       "connection_type": "Tulip (Tables API)"})
    assert entry["plant"] == ""


@pytest.mark.parametrize("plant", ["Custom", " custom ", "CUSTOM"])
def test_a_new_registration_cannot_name_the_retired_placeholder(monkeypatch, plant):
    """The HTTP route and the chat tool register through connect_new_system. The old form's
    placeholder now reads as no plant, so a new registration may not use it as a site name:
    it is refused with a message, never silently stored as no plant."""
    import agents.onboard_agent as oa
    added = []
    monkeypatch.setattr(oa, "catalog", type("C", (), {
        "lookup": lambda self, sid: None, "add": lambda self, e, **kw: added.append(e) or True})())
    monkeypatch.setattr(oa, "_registration_id_refusal", lambda sid: None)
    res = oa.connect_new_system(name="Quality Lab", system_type="Custom", plant=plant,
                                connection_type="SiteWise (Direct)",
                                connection_config={"region": "us-west-2"})
    assert res == {"error": "Plant can't be 'Custom'. Leave it blank if the system isn't tied to one site."}
    assert added == []


def test_a_plant_that_only_contains_the_word_is_kept(monkeypatch):
    import agents.onboard_agent as oa
    added = []
    monkeypatch.setattr(oa, "catalog", type("C", (), {
        "lookup": lambda self, sid: None, "add": lambda self, e, **kw: added.append(e) or True})())
    monkeypatch.setattr(oa, "_registration_id_refusal", lambda sid: None)
    monkeypatch.setattr(oa, "_claim_and_store", lambda sid, entry, cfg, pg, **kw: (added.append(entry), (None, None))[1])
    res = oa.connect_new_system(name="Quality Lab", system_type="Custom", plant="Custom Works",
                                connection_type="SiteWise (Direct)",
                                connection_config={"region": "us-west-2"})
    assert res["status"] == "created"
    assert added[0]["plant"] == "Custom Works"


@pytest.mark.parametrize("plant", ["Custom", "   "])
def test_registration_stores_no_placeholder_or_blank(monkeypatch, plant):
    """The HTTP route and the chat tool both register through register_system."""
    import agents.onboard_agent as oa
    added = {}

    class _Cat:
        def lookup(self, sid):
            return None

        def add(self, entry, **_kw):
            added.update(entry)
            return True

    monkeypatch.setattr(oa, "catalog", _Cat())
    monkeypatch.setattr(oa, "_registration_id_refusal", lambda sid: None)
    from agents.connection_resolver import ORIGIN_DEMO
    res = oa.register_system(name="Quality Lab", system_type="Custom", plant=plant,
                             connection_type="SiteWise (Direct)",
                             connection_config={"region": "us-west-2"}, origin=ORIGIN_DEMO)
    assert res["status"] == "created"
    assert added["plant"] == ""


def _provision_body(plant):
    return SimpleNamespace(system_id="cmms-b", system_name="CMMS B", plant=plant, schema="",
                           rules=[], mappings=[], connection_type="MCP Server",
                           connection_string="", connection_config={}, value_mappings=[])


@pytest.mark.parametrize("plant, want", [("Custom", ""), ("   ", ""), (" North Plant ", "North Plant")])
def test_the_provision_state_carries_the_normalized_plant(monkeypatch, plant, want):
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    assert onboard._make_provision_state(_provision_body(plant)).plant == want


def _ctx(plant):
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="cmms-b", system_name="CMMS B",
        plant=plant, schema="", rules=[], mappings=[], connection_type="MCP Server", cm_items=[])


def _writes(monkeypatch):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: writes.append((cypher, params or {})))
    return writes


@pytest.mark.parametrize("plant", ["", "   ", "Custom"])
def test_an_unsited_system_gets_no_plant_node_and_loses_any_old_site(monkeypatch, plant):
    writes = _writes(monkeypatch)
    onboard._dp_persist_sql_connection(_ctx(plant))
    assert not [c for c, _p in writes if "MERGE (p:Plant" in c or "MERGE (s)-[:LOCATED_AT]" in c]
    drops = [(c, p) for c, p in writes if "LOCATED_AT" in c and "DELETE" in c]
    assert len(drops) == 1
    assert drops[0][1]["pid"] == ""


def test_a_sited_system_drops_edges_to_other_plants_then_links_its_own(monkeypatch):
    writes = _writes(monkeypatch)
    onboard._dp_persist_sql_connection(_ctx("North Plant"))
    kinds = [("drop" if "DELETE" in c else "link") for c, _p in writes if "LOCATED_AT" in c]
    assert kinds == ["drop", "link"]
    drop = next(p for c, p in writes if "LOCATED_AT" in c and "DELETE" in c)
    assert drop["pid"] == "north-plant"


@pytest.mark.parametrize("plant", ["   ", "Custom"])
def test_the_system_node_writes_no_plant_node_for_an_unsited_system(monkeypatch, plant):
    writes = _writes(monkeypatch)
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(get=lambda sid: {}))
    monkeypatch.setattr(onboard, "_existing_node_guidance", lambda sid: {})
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    ctx = onboard._make_provision_state(_provision_body(plant))
    onboard._dp_system_node(ctx)
    assert not [c for c, _p in writes if "MERGE (p:Plant" in c]
    node = next(p for c, p in writes if c.startswith("MERGE (s:System {id: $sid})"))
    assert node["plant"] == ""


def test_a_legacy_placeholder_system_supersedes_nothing_and_revives_its_own_rules(monkeypatch):
    """Two systems onboarded at the placeholder before the fix: re-onboarding one revives its
    own machine-deactivated rules and retires none of the other's."""
    writes = _writes(monkeypatch)

    def query(cypher, params=None):
        if "RETURN DISTINCT c.id AS concept" in cypher:
            return [{"concept": "maintenance-event"}]
        if "s.plant = $plant" in cypher:
            return [{"sys_id": "cmms-a", "sys_name": "CMMS A"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "removed_ids", lambda rows, key, cat: set())
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(
        get=lambda sid: {"system_type": "CMMS"} if sid == "cmms-b" else None))
    monkeypatch.setattr(catalog_module, "CONCEPT_AUTHORITY",
                        {**catalog_module.CONCEPT_AUTHORITY, "maintenance-event": "CMMS"})
    onboard._dp_authority_rules(_ctx("Custom"))
    assert not [c for c, _p in writes if "'superseded" in c]
    revive = [p for c, p in writes if "ar.status = 'active'" in c]
    assert [p["arid"] for p in revive] == ["ar-cmms-b-maintenance-event"]
