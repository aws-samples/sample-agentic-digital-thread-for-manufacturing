# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identity-rule provenance + soft-deactivation lifecycle contracts.

Maintain-side audit trail: an accepted identity_rule proposal stamps WHO
approved it, WHEN, the producing pipeline, the triggering query context, and a
DynamoDB proposal back-pointer onto the graph node; onboarding-authored rules
stamp method/created_at/origin_context at write. Deactivation is a SOFT
disable (status='deactivated' + actor/timestamp) and every rule READER
(orchestrator route seed, resolve, graph_analytics) filters deactivated rules
so they stop participating in routing — while the graph-summary endpoint keeps
returning them for the topology drawer's audit view.

Offline: FakeTable + NeptuneSpy (same pattern as
test_join_path_transform_timekey). GENERIC — synthetic systems/fields only.
"""
from __future__ import annotations

import asyncio
import inspect

import agents.self_learning as self_learning


class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_part = UpdateExpression.strip()
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                if "=" not in clause:
                    continue
                lhs, rhs = (s.strip() for s in clause.split("=", 1))
                item[names.get(lhs, lhs)] = values[rhs]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)


class NeptuneSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, cypher: str, parameters: dict | None = None):
        self.calls.append((cypher, parameters))
        return True

    def identity_write(self):
        for cypher, params in self.calls:
            if "MERGE (ir:IdentityRule" in cypher:
                return cypher, params
        return None, None


def _install(monkeypatch):
    table = FakeTable()
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    return table, spy


def _log_identity_rule():
    return self_learning.log_proposed_mapping(
        "sys-a", "wo.cell", "",
        "sys-b", "asset.site_name", "",
        "site", 0.9, "Which site produced batch B-1?",
        proposed_type="identity_rule", provenance="orchestrator",
    )


# ── Provenance stamping on accept ────────────────────────────────────────────

def test_accepted_identity_rule_stamps_full_provenance(monkeypatch):
    table, spy = _install(monkeypatch)
    mid = _log_identity_rule()
    self_learning.accept_mapping(mid, actor="reviewer@example.com")

    cypher, params = spy.identity_write()
    assert cypher is not None
    for clause in ("ir.approved_by = $actor", "ir.created_at = $created_at",
                   "ir.proposal_id = $pid", "ir.origin_context = $octx",
                   "ir.provenance = $prov"):
        assert clause in cypher
    assert params["actor"] == "reviewer@example.com"
    assert params["pid"] == mid
    assert params["prov"] == "orchestrator"
    assert params["octx"] == "Which site produced batch B-1?"
    assert params["created_at"]  # ISO timestamp, non-empty

    # DynamoDB side records the approver too.
    assert table.by_id(mid)["accepted_by"] == "reviewer@example.com"


def test_accept_without_actor_stays_unattributed(monkeypatch):
    table, spy = _install(monkeypatch)
    mid = _log_identity_rule()
    self_learning.accept_mapping(mid)  # local/auth-disabled mode
    _cypher, params = spy.identity_write()
    assert params["actor"] == ""
    assert "accepted_by" not in table.by_id(mid)


# ── Onboarding write stamps provenance ───────────────────────────────────────

def test_onboarding_provenance_stamp_is_idempotent_and_scoped(monkeypatch):
    """The post-provision stamp targets ONLY this system's unstamped rules
    (method IS NULL) — learned rules and already-stamped rules are untouched."""
    import agents.onboard as onboard
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: calls.append((cypher, params or {})) or True)
    onboard._stamp_identity_rule_provenance("sys-a")
    cypher, params = calls[0]
    assert "ir.id STARTS WITH $prefix" in cypher
    assert "ir.method IS NULL" in cypher  # idempotent: never overwrites
    assert "ir.method = 'onboarding-llm'" in cypher
    assert "ir.created_at = $created_at" in cypher
    # An origin_context the provision already wrote (a direct match's
    # re-check evidence) is kept; an unset one gets the stamp's words.
    assert "ir.origin_context = coalesce(ir.origin_context, $origin_context)" in cypher
    assert params["prefix"] == "ir-sys-a-"
    assert "Identity Rules checkpoint" in params["origin_context"]


def test_onboarding_provenance_stamp_never_fails_provision(monkeypatch):
    import agents.onboard as onboard

    def boom(cypher, params=None):
        raise RuntimeError("graph down")

    monkeypatch.setattr(onboard, "neptune_write", boom)
    onboard._stamp_identity_rule_provenance("sys-a")  # must not raise


# ── Soft-deactivation lifecycle (server helper) ──────────────────────────────

def test_set_rule_lifecycle_deactivates_with_actor_and_timestamp(monkeypatch):
    import agents.server as server
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        return [{"id": params["rid"]}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle("ir-x-1", "reviewer@example.com", deactivate=True)
    assert result == {
        "status": "deactivated", "id": "ir-x-1",
        "deactivated_by": "reviewer@example.com",
        "deactivated_at": result["deactivated_at"],
    }
    cypher, params = calls[0]
    assert "SET ir.status = $status" in cypher
    # The actor is stamped unless the rule already carries this decision.
    assert "ir.deactivated_by = CASE WHEN same_decision THEN ir.deactivated_by ELSE $actor END" in cypher
    assert params["status"] == "deactivated"


def test_set_rule_lifecycle_reactivate_and_not_found(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query",
                        lambda cypher, params=None: [{"id": params["rid"]}])
    result = server._set_rule_lifecycle("ir-x-1", "reviewer@example.com", deactivate=False)
    assert result["status"] == "active"
    assert result["reactivated_by"] == "reviewer@example.com"

    monkeypatch.setattr(server, "neptune_query", lambda cypher, params=None: [])
    assert "error" in server._set_rule_lifecycle("ir-gone", "x", deactivate=True)


def test_request_actor_falls_back_to_username(monkeypatch):
    """The demo 'ADT' user has no email — username is the provenance identity."""
    import agents.server as server

    class _User:
        email = ""
        username = "ADT"

    class _State:
        user = _User()

    class _Req:
        state = _State()

    assert server._request_actor(_Req()) == "ADT"
    _User.email = "someone@example.com"
    assert server._request_actor(_Req()) == "someone@example.com"

    class _NoUser:  # auth-disabled/local mode — request.state has no user
        state = object()

    assert server._request_actor(_NoUser()) == ""


# ── Every rule READER filters deactivated rules ──────────────────────────────

_FILTER = "coalesce(ir.status, '') <> 'deactivated'"


def test_route_seed_cypher_filters_deactivated_rules():
    import agents.orchestrator as orch
    assert _FILTER in orch._ROUTE_SEED_CYPHER


def test_resolve_fetch_filters_deactivated_rules(monkeypatch):
    import agents.resolve as resolve
    captured: list[str] = []

    def fake_query(cypher, params=None):
        captured.append(cypher)
        return []

    monkeypatch.setattr(resolve, "neptune_query", fake_query)
    resolve._fetch_identity_rules("site")
    assert _FILTER in captured[0]


def test_graph_analytics_paths_filter_deactivated_rules():
    import agents.graph_analytics as ga
    src = inspect.getsource(ga)
    # Both the direct and the two-hop indirect path queries filter, through
    # the shared switched-off predicate (agents/rule_status.py).
    assert src.count("_rule_active('ir") >= 3  # ir, ir1, ir2


def test_graph_summary_still_returns_deactivated_rules_for_audit():
    """The topology drawer buckets deactivated rules — the summary endpoint
    must NOT filter them (the audit view needs them)."""
    import agents.server as server
    src = inspect.getsource(server.thread_graph_summary)  # the sync body (route wrapper: thread_graph_summary_route)
    assert "MATCH (ir:IdentityRule)" in src
    assert "deactivated" not in src.split("MATCH (ir:IdentityRule)")[1].split("RETURN")[0]
    # Provenance + lifecycle fields are projected for the drawer.
    for field in ("ir.provenance", "ir.origin_context", "ir.approved_by",
                  "ir.created_at", "ir.deactivated_by", "ir.deactivated_at"):
        assert field in src


def test_graph_summary_authority_detail_projects_vouching_fields(monkeypatch):
    """``authority_rules_detail`` carries the vouching provenance (2.2 additive):
    method/approved_by/evidence/provenance — the B1 cross-site probe and the
    review UI show WHY an authority does (or does not) vouch under the B4 P8
    rule. Existing fields (id/concept/system_id/rule) are untouched."""
    import agents.server as server

    def fake_query(cypher, params=None):
        if "AuthorityRule" in cypher:
            return [{"id": "ar-sys-a-1", "concept": "operator-id",
                     "system_id": "sys-a", "rule": "authoritative for operator",
                     "method": None, "approved_by": None, "evidence": None,
                     "provenance": "runtime-insight"}]
        return []

    monkeypatch.setattr(server, "neptune_query", fake_query)
    body = server.thread_graph_summary()  # sync body (route wrapper offloads it)
    ar = body["authority_rules_detail"][0]
    for key in ("id", "concept", "system_id", "rule",
                "method", "approved_by", "evidence", "provenance"):
        assert key in ar
    src = inspect.getsource(server.thread_graph_summary)  # the sync body (route wrapper: thread_graph_summary_route)
    ar_query = src.split("MATCH (ar:AuthorityRule)")[1].split("\n")[0]
    for field in ("ar.method", "ar.approved_by", "ar.evidence", "ar.provenance"):
        assert field in ar_query
