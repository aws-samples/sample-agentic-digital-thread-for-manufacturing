# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A system with no plant is never a same-plant competitor (ADR-0045).

Onboarding retires competing authority claims of other systems at the SAME
plant, and skips minting a rule for an undefined-authority concept another
same-plant system already maps. The plant comes from the System node, matched
by equality. A system registered without a plant carries an empty string, and
``'' = ''`` is true, so two unsited systems used to compete: onboarding a second
CMMS-type system with no plant (or the placeholder every Connect New System
registration used to send) retired the first one's CMMS authority rules,
although nothing said the two serve the same site. Seen live with two vendor
systems, one per site: the second turned off 9 of the first one's 11 rules.

Unsited means no plant at all (missing, empty or blank). A real plant, the
wildcard ``All Sites`` included, keeps its same-plant behaviour.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.onboard as onboard


class _Catalog:
    def get(self, system_id):
        return {"system_type": "CMMS"} if system_id == "cmms-b" else None


def _ctx(plant):
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]),
        system_id="cmms-b",
        system_name="CMMS B",
        plant=plant,
        schema="",
        rules=[],
        mappings=[{"concept": "maintenance-event"}],
        connection_type="MCP Server",
        cm_items=[],
    )


def _run(monkeypatch, plant, evidenced=("maintenance-event", "custom-note")):
    """Run the c4 authority step offline; return (writes, queries).

    Every competitor query answers with a same-plant system mapping the concept,
    so a run that asks at all would see a competitor.
    """
    writes: list[tuple[str, dict]] = []
    queries: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: writes.append((cypher, params or {})))

    def query(cypher, params=None):
        queries.append((cypher, params or {}))
        if "RETURN DISTINCT c.id AS concept" in cypher:
            return [{"concept": c} for c in evidenced]
        if "s.plant = $plant" in cypher:
            return [{"sys_id": "cmms-a", "sys_name": "CMMS A"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "removed_ids", lambda rows, key, cat: set())
    from agents import catalog as catalog_module
    monkeypatch.setattr(catalog_module, "CONCEPT_AUTHORITY",
                        {**catalog_module.CONCEPT_AUTHORITY, "maintenance-event": "CMMS"})
    monkeypatch.delitem(catalog_module.CONCEPT_AUTHORITY, "custom-note", raising=False)
    ctx = _ctx(plant)
    onboard._dp_authority_rules(ctx)
    return writes, queries


def _superseding(writes):
    return [(c, p) for c, p in writes if "'superseded" in c]


def _minted(writes):
    return [p for c, p in writes if c.startswith("MERGE (ar:AuthorityRule {id: $arid})")]


@pytest.mark.parametrize("plant", ["", "   ", None])
def test_an_unsited_system_retires_no_other_systems_claims(monkeypatch, plant):
    writes, _q = _run(monkeypatch, plant)
    assert _superseding(writes) == []


@pytest.mark.parametrize("plant", ["North Plant", "All Sites"])
def test_a_sited_system_still_retires_same_plant_claims(monkeypatch, plant):
    writes, _q = _run(monkeypatch, plant)
    superseded = _superseding(writes)
    assert superseded, "a real plant keeps the same-plant supersede"
    assert all(p["plant"] == plant for _c, p in superseded)


@pytest.mark.parametrize("plant", ["", "   ", None])
def test_an_unsited_system_has_no_same_plant_competitor(monkeypatch, plant):
    """The undefined-authority concept still gets this system's own rule."""
    writes, queries = _run(monkeypatch, plant)
    assert not [c for c, _p in queries if "s.plant = $plant" in c]
    assert {p["arid"] for p in _minted(writes)} == {
        "ar-cmms-b-maintenance-event", "ar-cmms-b-custom-note"}


def test_a_sited_system_still_defers_to_a_same_plant_competitor(monkeypatch):
    writes, queries = _run(monkeypatch, "North Plant")
    assert [c for c, _p in queries if "s.plant = $plant" in c]
    assert {p["arid"] for p in _minted(writes)} == {"ar-cmms-b-maintenance-event"}


@pytest.mark.parametrize("plant", ["", "   ", None])
def test_an_unsited_rule_names_no_plant(monkeypatch, plant):
    writes, _q = _run(monkeypatch, plant)
    rules = {p["arid"]: p["rule"] for p in _minted(writes)}
    assert rules["ar-cmms-b-maintenance-event"] == (
        "CMMS B is the authoritative source for maintenance-event")


def test_a_sited_rule_names_its_plant(monkeypatch):
    writes, _q = _run(monkeypatch, "North Plant")
    rules = {p["arid"]: p["rule"] for p in _minted(writes)}
    assert rules["ar-cmms-b-maintenance-event"] == (
        "CMMS B is the authoritative source for maintenance-event at North Plant")
