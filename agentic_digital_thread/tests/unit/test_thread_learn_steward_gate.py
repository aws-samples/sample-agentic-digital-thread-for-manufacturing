# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``POST /api/thread/learn`` writes steward-grade graph rules, so only a
steward may call it.

The route writes an IdentityRule stamped ``user_confirmed`` (the top routing
tier), a Correlation or a DecisionBoundary straight into the shared graph.
It carries the router-importable steward gate (``steward_gate.check_steward``,
the same fail-closed verdict as ``server._check_steward``): a steward's
request applies as before; anyone else, including a request with no auth
verdict at all, gets 403 before any graph read or write. The proposal store
has no DecisionBoundary type, so there is no pending-proposal path for a
non-steward. Fully offline.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.resolve as resolve
from agents.steward_gate import check_steward

_BODIES = {
    "identity_rule": {
        "type": "identity_rule", "concept": "batch",
        "source_system": "sys-a", "source_field": "lots.lot_no", "source_pattern": "L-{id}",
        "target_system": "sys-b", "target_field": "batches.batch_no", "target_pattern": "B{id}",
        "confidence": 0.9,
    },
    "correlation": {
        "type": "correlation", "concept_a": "downtime", "concept_b": "scrap",
        "system_a": "sys-a", "system_b": "sys-b", "pattern": "downtime precedes scrap",
        "confidence": 0.8,
    },
    "decision_boundary": {
        "type": "decision_boundary", "concept": "scrap", "pattern": "scrap > 5%",
        "threshold": 0.05, "action": "hold", "plant_id": "plant-1",
    },
}


class _Graph:
    def __init__(self):
        self.writes: list[str] = []
        self.reads: list[str] = []

    def write(self, cypher, params=None):
        self.writes.append(cypher)
        return True

    def query(self, cypher, params=None):
        self.reads.append(cypher)
        return [{"s.id": "sys-b"}]


def _client(monkeypatch, graph, is_admin):
    monkeypatch.setattr(resolve, "neptune_write", graph.write)
    monkeypatch.setattr(resolve, "neptune_query", graph.query)
    monkeypatch.setattr(resolve, "get_schema", lambda _sys: "")
    monkeypatch.setattr(resolve, "pg", lambda *a, **k: pytest.fail("no source read expected"))
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.include_router(resolve.router)
    return TestClient(app)


@pytest.mark.parametrize("kind", sorted(_BODIES))
@pytest.mark.parametrize("is_admin", [False, None], ids=["non-steward", "no-verdict"])
def test_non_steward_is_denied_before_any_graph_io(monkeypatch, kind, is_admin):
    graph = _Graph()
    resp = _client(monkeypatch, graph, is_admin).post("/api/thread/learn", json=_BODIES[kind])
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Steward (admin) role required"}
    assert graph.writes == [] and graph.reads == []


@pytest.mark.parametrize("kind, created", [
    ("identity_rule", "CREATE (ir:IdentityRule"),
    ("correlation", "CREATE (c:Correlation"),
    ("decision_boundary", "CREATE (d:DecisionBoundary"),
])
def test_steward_request_applies_as_before(monkeypatch, kind, created):
    graph = _Graph()
    resp = _client(monkeypatch, graph, True).post("/api/thread/learn", json=_BODIES[kind])
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    assert graph.writes and graph.writes[0].startswith(created)


def test_route_is_steward_gated_and_offload_only():
    route = next(r for r in resolve.router.routes if r.path == "/api/thread/learn")
    assert any(d.call is check_steward for d in route.dependant.dependencies)
    assert route.methods == {"POST"}
