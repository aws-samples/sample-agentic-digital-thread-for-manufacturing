# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AuthorityRule lifecycle contracts (T1.4, ADR-0050 addendum).

AuthorityRule gains the same soft deactivate/reactivate lifecycle as
IdentityRule: ``_set_rule_lifecycle`` matches BOTH labels (IdentityRule first;
a repeat of the decision an IdentityRule already carries keeps its who/when),
the ``/api/thread/rules/*`` routes
are admin-gated (they sit OUTSIDE the ``/api/admin`` middleware prefix, so the
gate is explicit and fails closed), and every DECISION-MAKING authority reader
filters ``status='deactivated'`` — while the graph-summary / topology-drawer
read keeps deactivated rules VISIBLE with their status (audit view; free-form
cypher via explore_graph is advisory-filtered only, per the ADR-0050 addendum).

Offline: monkeypatched query seams (same pattern as
test_rule_provenance_lifecycle). GENERIC — synthetic systems/concepts only.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


def _req(admin: bool | None):
    """Fake request: admin=None → NO is_admin attribute (fails closed)."""
    state = SimpleNamespace()
    if admin is not None:
        state.is_admin = admin
    return SimpleNamespace(state=state)


# ── _set_rule_lifecycle handles AuthorityRule ────────────────────────────────

def test_lifecycle_falls_through_to_authority_rule(monkeypatch):
    import agents.server as server
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        if "IdentityRule" in cypher:
            return []
        return [{"id": (params or {}).get("rid")}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle(
        "ar-sys-a-site", "reviewer@example.com", deactivate=True)
    assert result["status"] == "deactivated"
    assert result["id"] == "ar-sys-a-site"
    assert result["deactivated_by"] == "reviewer@example.com"
    assert result["deactivated_at"]
    ar_cypher, ar_params = calls[-1]
    assert "MATCH (ar:AuthorityRule {id: $rid})" in ar_cypher
    assert "SET ar.status = $status" in ar_cypher
    assert "ar.deactivated_by = $actor" in ar_cypher
    assert "ar.deactivated_at = $now" in ar_cypher
    assert ar_params["status"] == "deactivated"


def test_lifecycle_reactivates_authority_rule(monkeypatch):
    import agents.server as server

    def fake_query(cypher, params=None):
        if "IdentityRule" in cypher:
            return []
        return [{"id": (params or {}).get("rid")}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle(
        "ar-sys-a-site", "reviewer@example.com", deactivate=False)
    assert result["status"] == "active"
    assert result["reactivated_by"] == "reviewer@example.com"
    assert result["reactivated_at"]


def test_identity_rule_short_circuits_before_authority_query(monkeypatch):
    """Pin the IdentityRule contract: an IdentityRule match issues its one
    rule query (pinned below) — the AuthorityRule fallthrough must never run
    for it. The only other query is the rule's JoinPath cascade
    (agents/joinpath_rule_link.py)."""
    import agents.server as server
    from agents import joinpath_rule_link
    calls: list[tuple[str, dict]] = []

    def fake_query(cypher, params=None):
        calls.append((cypher, params or {}))
        return [{"id": (params or {}).get("rid")}]

    monkeypatch.setattr(server, "neptune_query", fake_query)
    result = server._set_rule_lifecycle(
        "ir-x-1", "reviewer@example.com", deactivate=True)
    assert result == {
        "status": "deactivated", "id": "ir-x-1",
        "deactivated_by": "reviewer@example.com",
        "deactivated_at": result["deactivated_at"],
    }
    assert len(calls) == 2
    assert not any("AuthorityRule" in cypher for cypher, _p in calls)
    cypher = calls[0][0]
    # A repeat of the decision the rule already carries keeps its who/when
    # (server._identity_rule_lifecycle_cypher); the answer carries them.
    assert cypher == (
        "MATCH (ir:IdentityRule {id: $rid}) "
        "WITH ir, coalesce(ir.status, '') = $status AS same_decision "
        "SET ir.status = $status, "
        "ir.deactivated_by = CASE WHEN same_decision THEN ir.deactivated_by ELSE $actor END, "
        "ir.deactivated_at = CASE WHEN same_decision THEN ir.deactivated_at ELSE $now END "
        "RETURN ir.id AS id, ir.deactivated_by AS deactivated_by, ir.deactivated_at AS deactivated_at"
    )
    cascade, params = calls[1]
    assert cascade.startswith("MATCH (jp:JoinPath) WHERE ("
                              + joinpath_rule_link.linked_rule_id_expr() + " = $rid ")
    assert params["rid"] == "ir-x-1"


def test_unknown_id_is_404_from_route(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", lambda c, p=None: [])
    resp = asyncio.run(server.deactivate_identity_rule_route("nope", _req(True)))
    assert resp.status_code == 404


# ── Admin gate (routes sit OUTSIDE /api/admin — explicit, fails closed) ──────

def test_deactivate_route_403_for_non_admin(monkeypatch):
    import agents.server as server
    from fastapi import HTTPException
    monkeypatch.setattr(server, "neptune_query",
                        lambda c, p=None: [{"id": "ir-x-1"}])
    # Absent verdict (no is_admin attribute at all) is denied like an
    # explicit non-admin verdict — the gate FAILS CLOSED.
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server.deactivate_identity_rule_route("ir-x-1", _req(None)))
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        asyncio.run(server.reactivate_identity_rule_route("ir-x-1", _req(False)))
    assert ei2.value.status_code == 403


def test_lifecycle_routes_pass_for_admin(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query",
                        lambda c, p=None: [{"id": (p or {}).get("rid")}])
    result = asyncio.run(server.deactivate_identity_rule_route("ir-x-1", _req(True)))
    assert result["status"] == "deactivated"
    result = asyncio.run(server.reactivate_identity_rule_route("ir-x-1", _req(True)))
    assert result["status"] == "active"


# ── Every decision-making reader filters deactivated rules ──────────────────

_AR_FILTER = "coalesce(ar.status, '') <> 'deactivated'"


def test_metric_resolver_authority_read_filters_deactivated(monkeypatch):
    """GraphAuthorityResolver (the ADR-0008 read seam behind metric cards,
    dashboard, multisite fan-out, dimension drills) excludes deactivated
    rules in the query itself."""
    import agents.metric_resolver as mr
    captured: list[str] = []
    monkeypatch.setattr(mr, "neptune_query",
                        lambda c, p=None: captured.append(c) or [])
    assert mr.GraphAuthorityResolver().authoritative_system("site") is None
    assert _AR_FILTER in captured[0]


def test_vouch_verdict_read_filters_deactivated(monkeypatch):
    """self_learning._authority_vouch_verdict (the P8 cross-site vouching
    read) excludes deactivated rules — a deactivated authority must not
    vouch for cross-site linkage."""
    import agents.self_learning as sl
    captured: list[str] = []
    monkeypatch.setattr(sl, "_neptune_query",
                        lambda c, p=None: captured.append(c) or [])
    verdict, ar_id = sl._authority_vouch_verdict("site", {})
    assert (verdict, ar_id) == ("none", "")
    assert _AR_FILTER in captured[0]


# ── Graph view: deactivated rules stay VISIBLE with their status ─────────────

def test_graph_summary_projects_authority_lifecycle_fields():
    """The topology drawer buckets deactivated authority rules — the summary
    endpoint must NOT filter them, and must project the lifecycle fields."""
    import agents.server as server
    src = inspect.getsource(server.thread_graph_summary)  # the sync body (route wrapper: thread_graph_summary_route)
    ar_query = src.split("MATCH (ar:AuthorityRule)")[1].split("\n")[0]
    assert "deactivated" not in ar_query.split("RETURN")[0]  # no WHERE filter
    for field in ("ar.status", "ar.deactivated_by", "ar.deactivated_at"):
        assert field in ar_query


def test_graph_summary_returns_deactivated_authority_rule_with_status(monkeypatch):
    import agents.server as server

    def fake_query(cypher, params=None):
        if "AuthorityRule" in cypher:
            return [{"id": "ar-sys-a-site", "concept": "site",
                     "system_id": "sys-a", "rule": "sys-a wins",
                     "status": "deactivated",
                     "deactivated_by": "reviewer@example.com",
                     "deactivated_at": "2026-07-29T00:00:00Z"}]
        return []

    monkeypatch.setattr(server, "neptune_query", fake_query)
    body = server.thread_graph_summary()  # sync body (route wrapper offloads it)
    ar = body["authority_rules_detail"][0]
    assert ar["status"] == "deactivated"
    assert ar["deactivated_by"] == "reviewer@example.com"


def test_metamodel_advises_free_cypher_readers_about_status():
    """explore_graph free-form cypher cannot be hard-gated — the metamodel
    AuthorityRule entry must carry the human-legible advisory so ad-hoc
    readers know to exclude deactivated rules (ADR-0050 addendum)."""
    from agents.metamodel import META_NODES
    entry = next(n for n in META_NODES if n["label"] == "AuthorityRule")
    assert "deactivated" in entry["description"]
    assert "status" in entry["key_properties"]
