# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No write path makes a category or status into an identity rule.

The resolve step withholds a rule on a category or status concept (a reason
code, a status) on both the wizard's and the chat's paths. Two other paths
write identity rules to the graph without passing through resolve:

* the provision request (``POST /api/onboard/provision``) carries the rules
  the caller sends; ``_dp_identity_rules`` validated their shape, never their
  concept, so a direct request could still write a reason-code rule;
* accepting a pending ``identity_rule`` proposal (one at a time, accept-all,
  the sweeps) went straight to ``self_learning._apply_identity_rule``.

Both now apply the same decision as resolve (``resolve_gates.category_rejection``,
with the graph's entity-layer concepts exempt). The provision lists the rule
in ``rejected_details`` with the reason; an accept leaves the proposal
pending and says why. Fixture names are generic.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.models import DiscoverRequest, ProvisionRequest  # noqa: E402
from tests.unit._value_mapping_write_seam import route_returning_writes  # noqa: E402
from tests.unit.test_accept_all_pending_skips_insight import _FakeTable  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, _wait_for, world)
from tests.unit.test_onboard_rule_writer_quality import (  # noqa: E402,F401 - fixture
    _ir_params, _jp_ids, _make_ctx, _rule, writes)

CATEGORY_REASON = "concept 'failure-mode' names a category or status, not one record"


def _ir_concepts(recorded):
    return [p["concept"] for p in _ir_params(recorded)]


# ── the provision's rule writer ──────────────────────────────────────────────

def test_the_rule_writer_lists_a_category_rule_in_rejected_details(writes):
    category = _rule(concept="failure-mode", source_field="failure_code",
                     target_field="stop_reason")
    good = _rule()
    ctx = _make_ctx([category, good])
    onboard._dp_identity_rules(ctx)
    assert ctx.rules_rejected == [{"rule": category, "reason": CATEGORY_REASON}]
    assert _ir_concepts(writes) == ["order-id"]
    onboard._dp_rule_joinpaths(ctx)
    assert _jp_ids(writes) == [f"jp-source-sys-{onboard._rule_digest_for('source-sys', good)}"]


def test_the_rule_writer_writes_a_rule_on_a_minted_entity_concept(writes, monkeypatch):
    def query(cypher, params=None):
        if "layer" in cypher and params and "ids" in params:
            return [{"id": i} for i in params["ids"] if i == "state-transitions"]
        if "HAS_FIELD" in cypher:
            return [{"tbl": "target_table"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    entity = _rule(concept="state-transitions", source_field="transition_id",
                   target_field="transition_ref")
    ctx = _make_ctx([entity])
    onboard._dp_identity_rules(ctx)
    assert ctx.rules_rejected == []
    assert _ir_concepts(writes) == ["state-transitions"]


def _direct_provision(world, rows: list, rules: list) -> dict:
    """The provision request a caller sends straight to the API: discover's
    schema provenance on the mapped rows, then POST /provision with the
    caller's own rules, and its poll."""
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    rows = onboard._attach_map_provenance(
        {"mappings": copy.deepcopy(rows)},
        onboard._mapping_schema_metadata(disc["tables"]))["mappings"]
    started = asyncio.run(onboard.onboard_provision(ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or entry["schema"], rules=rules, mappings=rows,
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={})))
    return _wait_for(lambda: (lambda r: r if r.get("status") != "running" else None)(
        asyncio.run(onboard.onboard_provision_status(started["job_id"]))))


def test_a_direct_provision_request_reports_the_category_rule_and_writes_the_rest(world):
    world.catalog.add({"id": "peer-mes", "name": "Peer MES", "status": "onboarded",
                       "plant": world.catalog.get(SID)["plant"], "system_type": "MES",
                       "connection_type": "REST API", "schema": ""})
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    rows = world.store.get_session(SID)["mappings"]
    down = next(r["field"] for r in rows if r["field"].endswith("Down"))
    table = down.split(".")[0]
    category = {"concept": "failure-mode", "source_field": down, "source_table": table,
                "source_pattern": "", "target_system": "peer-mes",
                "target_field": "stops.stop_reason", "target_table": "stops",
                "target_pattern": "", "confidence": 0.8, "reasoning": "shared codes"}
    good = {**category, "concept": "downtime", "target_field": "stops.stop_flag"}
    done = _direct_provision(world, rows, [category, good])
    assert done["status"] == "done", done
    assert [(d["rule"]["concept"], d["reason"]) for d in done["rejected_details"]] == [
        ("failure-mode", CATEGORY_REASON)]
    written = [p["concept"] for c, p in world.writes if "MERGE (ir:IdentityRule" in c]
    assert written == ["downtime"]


# ── accepting a pending identity-rule proposal ───────────────────────────────

def _pending(mapping_id, concept, from_field, to_field):
    return {"mapping_id": mapping_id, "discovered_at": "2026-01-01T00:00:00+00:00",
            "proposed_type": "identity_rule", "status": "pending", "version": "1",
            "subject_key": f"identity_rule|sys-a|{from_field}|sys-b|{to_field}|{concept}",
            "from_system": "sys-a", "from_field": from_field, "from_value": "",
            "to_system": "sys-b", "to_field": to_field, "to_value": "",
            "concept": concept, "confidence": "0.9", "source": "agent"}


@pytest.fixture
def store(monkeypatch):
    fake = _FakeTable()
    fake.items = [
        _pending("ir-category", "failure-mode", "work_orders.failure_code",
                 "stops.stop_reason"),
        _pending("ir-identity", "equipment-id", "assets.asset_tag", "machines.equipment_id"),
        _pending("ir-entity", "state-transitions", "transitions.transition_id",
                 "changes.transition_ref"),
    ]
    graph_writes: list = []

    def query(cypher, params=None):
        if "layer" in cypher and params and "ids" in params:
            return [{"id": i} for i in params["ids"] if i == "state-transitions"]
        return []

    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_query", query)
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: graph_writes.append((c, p or {})) or True)
    route_returning_writes(monkeypatch, lambda c, p=None: True)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    fake.graph_writes = graph_writes
    return fake


def _status(fake, mapping_id):
    return next(i for i in fake.items if i["mapping_id"] == mapping_id)["status"]


def _rule_concepts(fake):
    return [p["concept"] for c, p in fake.graph_writes if "MERGE (ir:IdentityRule" in c]


def test_accepting_a_category_rule_proposal_leaves_it_pending_with_the_reason(store):
    result = self_learning.accept_mapping("ir-category", actor="steward-1")
    assert result["error"] == f"{CATEGORY_REASON}; the proposal stays pending"
    assert result["blocked_reason"] == CATEGORY_REASON
    assert result["http_status"] == 400
    assert _status(store, "ir-category") == "pending"
    assert _rule_concepts(store) == []


def test_accept_all_leaves_the_category_proposal_pending_and_accepts_the_rest(store):
    result = self_learning.accept_all_pending()
    assert result["status"] == "accepted_all"
    assert result["count"] == 2
    assert result["errors"] == [f"ir-category: {CATEGORY_REASON}; the proposal stays pending"]
    assert _status(store, "ir-category") == "pending"
    assert _status(store, "ir-identity") == "accepted"
    assert _status(store, "ir-entity") == "accepted"
    assert sorted(_rule_concepts(store)) == ["equipment-id", "state-transitions"]


# ── a steward's correction ───────────────────────────────────────────────────

def _correction(concept, from_field="work_orders.failure_code", to_field="stops.stop_reason"):
    from agents.models import UserDefinedMappingRequest
    return UserDefinedMappingRequest(
        from_system="sys-a", from_field=from_field, from_value="X-1",
        to_system="sys-b", to_field=to_field, to_value="X-1",
        concept=concept, proposed_type="identity_rule")


@pytest.fixture
def steward_route(monkeypatch, store):
    import agents.schema_check as schema_check
    import agents.server as server
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)
    store.items = []
    return lambda body: server.user_define_mapping(body, steward=True, actor="steward-1")


def test_a_stewards_category_identity_correction_is_refused_and_nothing_is_written(
        steward_route, store):
    import json as _json
    response = steward_route(_correction("failure-mode"))
    assert response.status_code == 400
    error = _json.loads(response.body)["error"]
    assert error.startswith(CATEGORY_REASON)
    assert "not applied" in error
    assert store.items == []
    assert store.graph_writes == []


def test_record_user_correction_refuses_a_category_identity_rule_before_any_write(store):
    store.items = []
    with pytest.raises(ValueError, match="names a category or status, not one record"):
        self_learning.record_user_correction(
            from_system="sys-a", from_field="work_orders.failure_code", from_value="",
            to_system="sys-b", to_field="stops.stop_reason", to_value="",
            concept="failure-mode", proposed_type="identity_rule")
    assert store.items == [] and store.graph_writes == []


@pytest.mark.parametrize("concept, from_field, to_field", [
    ("equipment-id", "assets.asset_tag", "machines.equipment_id"),
    ("state-transitions", "transitions.transition_id", "changes.transition_ref"),
], ids=["identity-concept", "minted-entity-concept"])
def test_a_stewards_identity_correction_on_a_record_concept_is_written(
        steward_route, store, concept, from_field, to_field):
    result = steward_route(_correction(concept, from_field, to_field))
    assert result["status"] == "accepted", result
    assert _rule_concepts(store) == [concept]


def test_a_category_value_mapping_correction_is_not_refused(steward_route, store):
    """The check is about identity rules: a value mapping between two codes
    of a category is what a value mapping is for."""
    from agents.models import UserDefinedMappingRequest
    result = steward_route(UserDefinedMappingRequest(
        from_system="sys-a", from_field="work_orders.failure_code", from_value="X-1",
        to_system="sys-b", to_field="stops.stop_reason", to_value="Y-1",
        concept="failure-mode", proposed_type="value_mapping"))
    assert result["status"] == "accepted", result
