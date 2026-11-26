# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A removed system's display name never stands for another system's id.

The catalog knows a system being removed by its id AND its display name (a
model's proposal may name a system either way). A check keyed by system id
must read the ids alone: a removed system whose display name happens to be
another, healthy system's id would otherwise take the healthy system out of
every candidate list, inventory, authority read and route seed (review
round 4 reproduced it: removed ``{id: "gone", name: "live-id"}`` plus healthy
``{id: "live-id"}`` left no served system). Model-written fields are matched
by id or display name, and there a healthy system's exact id wins over a
removed system's name. Each test below is that reproduction against one
caller. Fixture ids are generic.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_module  # noqa: E402
import agents.join_discovery as jd  # noqa: E402
import agents.metric_resolver as mr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.orchestrator as orch  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.catalog import REMOVING, REMOVING_SYSTEMS_READ, SystemCatalog  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402

GONE = "gone"
LIVE = "live-id"     # a healthy system's id, and the removed system's display name
NEW = "plant-cmms"


def _catalog() -> SystemCatalog:
    return SystemCatalog([
        {"id": GONE, "name": LIVE, "status": REMOVING, "connection_type": "REST API",
         "schema": "", "plant": "Plant 1", "system_type": "MES"},
        {"id": LIVE, "name": "Live System", "status": "onboarded", "connection_type": "PostgreSQL",
         "schema": "", "plant": "Plant 1", "system_type": "ERP"},
        {"id": NEW, "name": "Plant CMMS", "status": "registered", "connection_type": "PostgreSQL",
         "schema": "", "plant": "Plant 1", "system_type": "CMMS"},
    ])


@pytest.fixture(autouse=True)
def collision(monkeypatch) -> SystemCatalog:
    """The same catalog in every module that holds one."""
    cat = _catalog()
    for module in (catalog_module, onboard, oa, mr):
        monkeypatch.setattr(module, "catalog", cat)
    return cat


# ---------------------------------------------------------------- the helpers

def test_the_id_answer_holds_ids_only():
    assert catalog_module.systems_being_removed() == {GONE}
    assert catalog_module.removing_system_ids([]) == {GONE}
    rows = [{"id": GONE, "removal_incomplete": False}, {"id": LIVE, "removal_incomplete": False}]
    assert catalog_module.removed_ids(rows, "id") == {GONE}


def test_the_alias_answer_never_holds_a_healthy_systems_id(collision):
    assert catalog_module.removal_aliases() == {GONE}
    assert LIVE not in collision.get_removal_keys()
    collision.add({"id": "other", "name": "Other API", "status": REMOVING})
    assert catalog_module.removal_aliases() == {GONE, "other", "Other API"}


# ---------------------------------------------------------------- join discovery

def test_join_discovery_keeps_the_healthy_system(monkeypatch):
    monkeypatch.setattr(jd, "_neptune_query",
                        lambda *_a, **_k: [{"id": GONE, "removal_incomplete": False},
                                           {"id": LIVE, "removal_incomplete": False}])
    assert jd._onboarded_systems() == [LIVE]


def test_join_discovery_files_a_proposal_against_the_healthy_system(monkeypatch):
    filed: list = []
    monkeypatch.setattr(jd, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(jd, "_log_proposed_mapping", lambda **kw: filed.append(kw) or "m-1")
    assert jd._file_unless_removing(from_system=NEW, to_system=LIVE, proposed_type="join_path") == "m-1"
    assert jd._file_unless_removing(from_system=NEW, to_system=GONE, proposed_type="join_path") is None


# ---------------------------------------------------------------- candidate reads

def test_the_affinity_pass_keeps_the_healthy_peer(monkeypatch):
    rows = [{"sys_id": sid, "sys_schema": "", "table_name": "assets", "field_name": "asset_code",
             "concept": "equipment-id", "concepts": ["equipment-id"], "removal_incomplete": False}
            for sid in (GONE, LIVE)]
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: copy.deepcopy(rows))
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_k: ["EQ-1", "EQ-2"])
    assert [f.system_id for f in onboard._affinity_existing_fields(NEW)] == [LIVE]


def test_the_entity_pairs_keep_the_healthy_peer(monkeypatch):
    rows = [{"sys_id": sid, "table_name": "assets", "entity": "equipment", "id_fields": ["asset_code"],
             "removal_incomplete": False} for sid in (NEW, GONE, LIVE)]
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: copy.deepcopy(rows))
    assert [p["tgt_system"] for p in onboard._entity_anchored_pairs(NEW)] == [LIVE]


# ---------------------------------------------------------------- provision

def test_the_provision_inventory_keeps_the_healthy_system(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda *_a, **_k: [{"id": sid, "removal_incomplete": False}
                                           for sid in (NEW, GONE, LIVE)])
    assert onboard._graph_system_ids_checked() == {NEW, LIVE}


def test_provision_writes_the_rule_to_the_healthy_system_and_refuses_the_removed_one(monkeypatch):
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))

    def query(cypher, params=None):
        if cypher.startswith("MATCH (s:System) RETURN"):
            return [{"sid": sid, "removal_incomplete": False} for sid in (NEW, GONE, LIVE)]
        if "HAS_FIELD" in cypher:
            return [{"tbl": "t"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    rule = {"concept": "equipment-id", "source_field": "asset_num", "source_table": "work_orders",
            "source_pattern": "", "target_system": LIVE, "target_field": "asset_code",
            "target_pattern": "", "confidence": 0.9}
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id=NEW, system_name="Plant CMMS",
        plant="", schema="", rules=[rule, dict(rule, target_system=GONE)],
        mappings=[], connection_type="MCP Server", cm_items=[], committable=set())
    onboard._dp_identity_rules(ctx)
    assert [p["tgt_sys"] for c, p in writes if c.startswith("MERGE (ir:IdentityRule")] == [LIVE]
    assert [r["rule"]["target_system"] for r in ctx.rules_rejected] == [GONE]


# ---------------------------------------------------------------- authority

def test_the_healthy_system_still_competes_for_authority(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda *_a, **_k: [{"sys_id": LIVE, "sys_name": "Live System",
                                            "removal_incomplete": False}])
    assert [r["sys_id"] for r in onboard._dp_same_plant_competitors("c", NEW, "Plant 1")] == [LIVE]


def test_the_healthy_systems_authority_rule_is_selected(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query",
                        lambda c, p=None: [{"sid": LIVE, "removal_incomplete": False}]
                        if "AuthorityRule" in c else [])
    assert mr.GraphAuthorityResolver().authoritative_system("c") == LIVE


def test_the_healthy_system_is_the_single_mapping_authority(monkeypatch):
    def query(cypher, params=None):
        if "AuthorityRule" in cypher:
            return []
        return [{"sid": LIVE, "removal_incomplete": False}, {"sid": GONE, "removal_incomplete": False}]
    monkeypatch.setattr(mr, "neptune_query", query)
    source = mr._resolve_authority("c", mr.GraphAuthorityResolver())
    assert (source.system_id, source.source) == (LIVE, "single_system_map")


def test_the_healthy_systems_rule_still_vouches_across_sites(monkeypatch):
    rows = [{"id": "ar-live", "concept": "c", "sid": LIVE, "method": "user_confirmed",
             "approved_by": "", "evidence": "", "removal_incomplete": False}]
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: copy.deepcopy(rows))
    assert self_learning._authority_vouch_verdict("c", {}) == ("steward", "ar-live")


# ---------------------------------------------------------------- route seed

def test_the_route_seed_keeps_the_healthy_system():
    def query(cypher):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": LIVE, "system_name": "Live System"}]
        return []
    seed = orch.build_route_seed(query=query)
    assert seed is not None and LIVE in seed


def test_the_route_seeds_fallback_marker_answer_is_ids_only():
    def failing(cypher):
        raise RuntimeError("graph unavailable")
    assert orch._systems_being_removed_now(failing) == {GONE}


# ---------------------------------------------------------------- model-written fields

def _result() -> dict:
    return {"rules": [{"concept": "c", "target_system": LIVE}, {"concept": "c", "target_system": GONE}],
            "value_mappings": [{"from_system": NEW, "to_system": LIVE}, {"from_system": NEW, "to_system": GONE}],
            "evidence": [{"target_system": LIVE}, {"target_system": GONE}]}


def test_a_model_rule_naming_the_healthy_id_is_kept_and_the_removed_one_dropped(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    kept = onboard._drop_removed_candidates(_result(), NEW)
    assert [r["target_system"] for r in kept["rules"]] == [LIVE]
    assert [v["to_system"] for v in kept["value_mappings"]] == [LIVE]
    assert [e["target_system"] for e in kept["evidence"]] == [LIVE]


def test_the_chats_resolve_keeps_the_healthy_id(monkeypatch):
    monkeypatch.setattr(oa, "neptune_query", lambda *_a, **_k: [])
    result = _result()
    rules, vms = oa._without_peers_being_removed(NEW, result["rules"], result["value_mappings"])
    assert [r["target_system"] for r in rules] == [LIVE]
    assert [v["to_system"] for v in vms] == [LIVE]


def test_the_resolve_job_keeps_the_healthy_systems_rule_and_join_proposal(monkeypatch):
    pending = [{"mapping_id": "m-live", "discovered_at": "t1", "status": "pending", "source": jd.MINER_SOURCE,
                "proposed_type": "join_path", "from_system": NEW, "to_system": LIVE, "confidence": 0.9}]
    superseded: list = []
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: copy.deepcopy(pending))
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_ids", lambda ids, reason: superseded.extend(ids) or 0)
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: _result())
    monkeypatch.setattr(resolve_gates, "gate_overlap_rules", lambda result: result)
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": ["m-live"], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda cypher, params=None: [] if cypher == REMOVING_SYSTEMS_READ else [])
    onboard._run_resolve_job("job-collision", OnboardResolveRequest(system_id=NEW, mappings=[]))
    job = onboard._resolve_jobs.pop("job-collision")
    assert job["status"] == "done", job
    assert [r["target_system"] for r in job["result"]["rules"]] == [LIVE]
    assert [c["mapping_id"] for c in job["result"]["join_candidates"]] == ["m-live"]
    assert superseded == []
