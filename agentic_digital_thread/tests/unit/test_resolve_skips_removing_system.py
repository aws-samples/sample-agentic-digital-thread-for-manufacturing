# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding never proposes identity rules against a system whose removal runs.

A removal writes ``removal_incomplete = true`` on the :System node FIRST
(``onboard._run_reset_steps``), then spends minutes deleting the runtime and
gateway target, and only then deletes the system's Field nodes and returns
its catalog entry to 'registered'. For those minutes the system still has
fields, values and an 'onboarded' catalog entry. Onboarding another system in
that window (observed on a workshop walk: a site CMMS re-onboarded 18 s after
a REST CMMS serving the same records started its removal) proposed identity
rules against the system being removed, backed by 100% value overlap.

The cross-system candidate reads of the resolve step (the entity-anchored
pairs, the affinity pass, join discovery, the chat's resolve_identities)
leave out a peer whose node carries the marker; the wizard's resolve job
drops whatever still names one when it ends (the concept pass's model rules,
and anything from a removal that started mid-resolve); and the provision
refuses a rule whose target is being removed. Fixture ids are generic.
"""
import copy
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.identity_affinity as aff  # noqa: E402
import agents.join_discovery as jd  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)

NEW = "plant-cmms"
REMOVING = "site-api"   # its removal started: the node carries removal_incomplete
PEER = "corp-erp"
CODES = [f"EQ-{n:04d}" for n in range(1, 13)]


class _Catalog:
    """Both peers are 'onboarded': the catalog entry of a system being removed
    keeps that status until the removal's last step."""

    _TYPES = {NEW: "CMMS", REMOVING: "MES", PEER: "ERP"}

    def get(self, sid):
        kind = self._TYPES.get(sid)
        return None if kind is None else {
            "id": sid, "system_type": kind, "plant": "Plant 1", "schema": f"s_{sid.replace('-', '_')}",
            "connection_type": "PostgreSQL", "status": "onboarded"}

    def get_onboarded_ids(self):
        return set(self._TYPES)


def _peer_field_rows():
    """The identifier-field read as Neptune answers it: one row per peer field,
    the removing peer's carrying its node's marker."""
    return [
        {"sys_id": REMOVING, "sys_name": "Site API", "sys_schema": "s_site_api",
         "table_name": "assets", "field_name": "asset_code", "concept": "equipment-id",
         "concepts": ["equipment-id"], "role": "identifier", "removal_incomplete": True},
        {"sys_id": PEER, "sys_name": "Corp ERP", "sys_schema": "s_corp_erp",
         "table_name": "equipment", "field_name": "equipment_code", "concept": "equipment-id",
         "concepts": ["equipment-id"], "role": "identifier", "removal_incomplete": False},
    ]


def _new_mapping():
    return {"table": "work_orders", "field": "work_orders.asset_num", "concept": "equipment-id",
            "confidence": 0.95,
            "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.95}]}


def _columns():
    """Every system serves the same equipment codes (100% overlap)."""
    return {(NEW, "work_orders", "asset_num"): CODES,
            (REMOVING, "assets", "asset_code"): CODES,
            (PEER, "equipment", "equipment_code"): CODES}


def _sample(*, system_id, table, field, **_kw):
    # The concept pass samples the mapping's field as the mapper spelled it
    # (``table.column``); the affinity pass passes the bare column.
    return list(_columns().get((system_id, table, field.split(".")[-1]), []))


def _probe(*, system_id, table, field, values, **_kw):
    return sorted(set(values) & set(_columns().get((system_id, table, field), [])))


@pytest.fixture
def graph(monkeypatch):
    """The offline world: the catalog above, Neptune answering the identifier
    read with both peers, sampling and probing from :func:`_columns`."""
    seen: list = []

    def query(cypher, params=None):
        seen.append(cypher)
        if "WHERE s.removal_incomplete = true" in cypher:
            return [{"sid": REMOVING}]
        if "IDENTIFIED_BY" in cypher:
            return []
        if "HAS_FIELD" in cypher and "MAPS_TO" in cypher:
            return copy.deepcopy(_peer_field_rows())
        return []

    monkeypatch.setattr(onboard, "catalog", _Catalog())
    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(resolve_evidence, "sample_field_values", _sample)
    monkeypatch.setattr(resolve_evidence, "probe_field_values", _probe)
    import agents.connection_resolver as connection_resolver
    monkeypatch.setattr(connection_resolver, "resolve_connection", lambda *_a, **_k: {})
    return seen


# ---------------------------------------------------------------- affinity pass

def test_affinity_peer_read_leaves_out_a_system_being_removed(graph):
    peers = onboard._affinity_existing_fields(NEW)
    assert [f.system_id for f in peers] == [PEER]
    read = next(c for c in graph if "r.role = 'identifier'" in c)
    assert "removal_incomplete" in read


def test_affinity_pass_proposes_no_rule_against_a_system_being_removed(graph):
    body = SimpleNamespace(system_id=NEW, mappings=[_new_mapping()])
    out = onboard._augment_with_affinity_rules(body, {"rules": []})
    targets = {r["target_system"] for r in out["rules"]}
    assert REMOVING not in targets
    # The same evidence against a fully onboarded peer still yields its rule.
    assert targets == {PEER}
    assert {r["method"] for r in out["rules"]} == {aff.EXACT_METHOD}


# ---------------------------------------------------------------- concept pass

def test_concept_pass_rule_against_a_system_being_removed_never_reaches_review(graph, monkeypatch):
    """The wizard's resolve job end to end (concept pass, overlap gate,
    affinity pass): the model proposes a rule to each peer, both backed by
    100% measured overlap; the steward is shown only the one whose target is
    not being removed."""
    def model(prompt, *_a, **_k):
        rule = {"concept": "equipment-id", "source_field": "work_orders.asset_num",
                "source_pattern": "EQ-{n}", "target_pattern": "EQ-{n}", "confidence": 0.9,
                "reasoning": "same equipment codes"}
        return json.dumps({"rules": [dict(rule, target_system=REMOVING, target_field="assets.asset_code"),
                                     dict(rule, target_system=PEER,
                                          target_field="equipment.equipment_code")],
                           "value_mappings": []})

    monkeypatch.setattr(onboard, "bedrock_call", model)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    body = OnboardResolveRequest(system_id=NEW, mappings=[_new_mapping()])
    onboard._run_resolve_job("job-concept", body)
    job = onboard._resolve_jobs.pop("job-concept")
    assert job["status"] == "done", job
    result = job["result"]
    named = {r.get("target_system") for key in ("rules", "rejected_rules", "evidence")
             for r in result.get(key) or []}
    assert REMOVING not in named
    assert PEER in {r["target_system"] for r in result["rules"]}


def test_entity_anchored_pairs_leave_out_a_system_being_removed(monkeypatch):
    rows = [
        {"sys_id": NEW, "table_name": "work_orders", "entity": "equipment", "id_fields": ["asset_num"],
         "removal_incomplete": False},
        {"sys_id": REMOVING, "table_name": "assets", "entity": "equipment", "id_fields": ["asset_code"],
         "removal_incomplete": True},
        {"sys_id": PEER, "table_name": "equipment", "entity": "equipment",
         "id_fields": ["equipment_code"], "removal_incomplete": False},
    ]
    seen: list = []
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda cypher, params=None: seen.append(cypher) or copy.deepcopy(rows))
    pairs = onboard._entity_anchored_pairs(NEW)
    assert [p["tgt_system"] for p in pairs] == [PEER]
    assert "removal_incomplete" in seen[0]


# ---------------------------------------------------------------- join discovery

def test_join_discovery_leaves_out_a_system_being_removed(monkeypatch):
    seen: list = []
    rows = [{"id": NEW, "removal_incomplete": False}, {"id": REMOVING, "removal_incomplete": True},
            {"id": PEER, "removal_incomplete": False}]
    monkeypatch.setattr(jd, "_neptune_query",
                        lambda cypher, params=None: seen.append(cypher) or copy.deepcopy(rows))
    assert jd._onboarded_systems() == sorted([NEW, PEER])
    assert "removal_incomplete" in seen[0]


# ---------------------------------------------------------------- the resolve job

def test_resolve_job_drops_what_names_a_system_whose_removal_started_mid_resolve(monkeypatch, caplog):
    """The candidate reads ran before the removal's marker was written; the
    job's result is read again against the marker when it ends."""
    to_removing = {"concept": "equipment-id", "source_field": "work_orders.asset_num",
                   "target_system": REMOVING, "target_field": "assets.asset_code", "confidence": 0.9}
    to_peer = dict(to_removing, target_system=PEER, target_field="equipment.equipment_code")
    found = {
        "rules": [to_removing, to_peer],
        "value_mappings": [
            {"from_system": NEW, "from_field": "asset_num", "from_value": "A1", "to_system": REMOVING,
             "to_field": "asset_code", "to_value": "B1", "concept": "equipment-id"},
            {"from_system": NEW, "from_field": "asset_num", "from_value": "A1", "to_system": PEER,
             "to_field": "equipment_code", "to_value": "C1", "concept": "equipment-id"}],
        "evidence": [{"concept": "equipment-id", "target_system": REMOVING, "overlap_count": 12},
                     {"concept": "equipment-id", "target_system": PEER, "overlap_count": 12}],
    }
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: copy.deepcopy(found))
    monkeypatch.setattr(resolve_gates, "gate_overlap_rules", lambda result: result)
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})

    def query(cypher, params=None):
        if "removal_incomplete" in cypher:
            return [{"sid": REMOVING}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    body = OnboardResolveRequest(system_id=NEW, mappings=[_new_mapping()])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        onboard._run_resolve_job("job-rm", body)
    job = onboard._resolve_jobs.pop("job-rm")
    assert job["status"] == "done", job
    result = job["result"]
    assert [r["target_system"] for r in result["rules"]] == [PEER]
    assert [v["to_system"] for v in result["value_mappings"]] == [PEER]
    assert [e["target_system"] for e in result["evidence"]] == [PEER]
    assert any(REMOVING in r.getMessage() and "being removed" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------- provision

class _ProvisionCatalog(_Catalog):
    def get(self, _sid):
        return None   # no schema: the Postgres column check is skipped


def test_provision_refuses_a_rule_whose_target_is_being_removed(monkeypatch):
    """The review step showed a rule; the target's removal started before the
    steward pressed Confirm & Provision."""
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))

    def query(cypher, params=None):
        if cypher.startswith("MATCH (s:System) RETURN"):
            return [{"sid": NEW, "removal_incomplete": False},
                    {"sid": REMOVING, "removal_incomplete": True},
                    {"sid": PEER, "removal_incomplete": False}]
        if "HAS_FIELD" in cypher:
            return [{"tbl": "t"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog", _ProvisionCatalog())
    rule = {"concept": "equipment-id", "source_field": "asset_num", "source_table": "work_orders",
            "source_pattern": "", "target_system": REMOVING, "target_field": "asset_code",
            "target_pattern": "", "confidence": 0.9}
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=NEW, system_name="Plant CMMS",
        plant="", schema="", rules=[rule, dict(rule, target_system=PEER, target_field="equipment_code")],
        mappings=[], connection_type="MCP Server", cm_items=[], committable=set())
    onboard._dp_identity_rules(ctx)
    written = [p["tgt_sys"] for c, p in writes if c.startswith("MERGE (ir:IdentityRule")]
    assert written == [PEER]
    (rejected,) = ctx.rules_rejected
    assert rejected["rule"]["target_system"] == REMOVING
    assert "being removed" in rejected["reason"]


# ---------------------------------------------------------------- the chat's resolve

def test_chat_resolve_shows_the_model_no_system_being_removed(world):
    world.catalog.add({"id": REMOVING, "name": "Site API", "status": "onboarded",
                       "connection_type": "REST API", "schema": "", "system_type": "MES"})
    rows = [{"sys_id": REMOVING, "sys_name": "Site API", "sys_schema": "", "field_name": "stop_code",
             "concept": "downtime", "table_name": "stops", "removal_incomplete": True}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer
    prompts: list = []

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        prompts.append(prompt)
        return json.dumps({"rules": [{"concept": "downtime", "target_system": REMOVING}],
                           "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert prompts == []
    assert resolved.get("rules") == [], resolved
