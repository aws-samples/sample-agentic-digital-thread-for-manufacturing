# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the routes refused during a background startup restore answer.

While ``startup_restore`` is up, a refused route answers a retryable 503 whose
body carries ``reason: "startup_restore"`` beside the plain-words detail, so a
client can tell it from another 503 on the same route without matching text.
On admin routes the admin check runs first, and on onboarding routes the
onboarding role check: a caller without the role gets the 403 it always got,
never a hint that the app restarted. The graph snapshot import is refused;
reconcile is refused only when it asks for the repair (the check alone keeps
answering).

The routes run on a small app carrying the real routers, with a middleware
that sets ``request.state.is_admin`` the way the auth middleware does.
Nothing behind a refusal runs: each route body would reach AWS or the graph.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agents import admin_routes, graph_snapshot_routes, startup_restore

#: Every admin route refused during a restore: (method, path, JSON body).
ADMIN_REFUSED = [
    ("PUT", "/api/admin/connection/sys-a", {}),
    ("PUT", "/api/admin/system-type/sys-a", {"system_type": "MES"}),
    ("PUT", "/api/admin/highbyte-token", {"token": "x"}),
    ("PUT", "/api/admin/scan-budget/sys-a", {"max_bytes": 1}),
    ("PUT", "/api/admin/business-context/sys-a", {"business_context": "x"}),
    ("PUT", "/api/admin/llm-settings", {}),
    ("POST", "/api/admin/restore-runtime-auth", {}),
    ("POST", "/api/admin/graph/snapshot/import", {"confirm": "wipe-and-restore"}),
    ("POST", "/api/admin/graph/snapshot/reconcile", {"repair": True}),
]
#: Every onboarding route refused during a restore (the onboard router's
#: role check runs first).
ONBOARD_REFUSED = [
    ("POST", "/api/onboard/connect", {}),
    ("POST", "/api/onboard/provision", {}),
    ("POST", "/api/onboard/provision/confirm", {}),
    ("POST", "/api/onboard/reset", {}),
    ("POST", "/api/onboard/reset-all", {}),
    ("POST", "/api/onboard/reset/sys-a", {}),
    ("POST", "/api/onboard/reseed", {}),
    ("POST", "/api/onboard/credentials", {}),
    ("POST", "/api/onboard/sync/sys-a", {}),
    ("POST", "/api/onboard/guided/confirm-context", {}),
]
REFUSED = ADMIN_REFUSED + ONBOARD_REFUSED


def _app() -> FastAPI:
    from agents import onboard
    app = FastAPI()
    startup_restore.install(app)

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.is_admin = request.headers.get("x-test-admin") == "1"
        request.state.is_integrator = False
        request.state.user = None
        return await call_next(request)

    app.include_router(admin_routes.router)
    app.include_router(graph_snapshot_routes.router)
    app.include_router(onboard.router)
    return app


@pytest.fixture
def client():
    return TestClient(_app())


@pytest.fixture
def restoring():
    startup_restore.begin()
    yield
    startup_restore.end()


def _send(client, method, path, body, admin):
    return client.request(method, path, json=body,
                          headers={"x-test-admin": "1" if admin else "0"})


def test_the_reason_is_the_stable_code_the_frontend_reads():
    assert startup_restore.RESTORING_REASON == "startup_restore"


@pytest.mark.parametrize("method,path,body", REFUSED,
                         ids=[f"{m} {p}" for m, p, _b in REFUSED])
def test_an_admin_gets_the_restore_refusal(client, restoring, method, path, body):
    response = _send(client, method, path, body, admin=True)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": startup_restore.RESTORING_DETAIL,
                               "reason": "startup_restore"}
    assert response.headers["Retry-After"] == str(startup_restore.RETRY_AFTER_S)


@pytest.mark.parametrize("method,path,body", REFUSED,
                         ids=[f"{m} {p}" for m, p, _b in REFUSED])
def test_a_caller_without_the_role_still_gets_403_during_a_restore(
        client, restoring, method, path, body):
    response = _send(client, method, path, body, admin=False)
    assert response.status_code == 403, response.text
    assert "reason" not in response.json()


def test_a_check_only_reconcile_still_answers_during_a_restore(client, restoring, monkeypatch):
    monkeypatch.setattr(graph_snapshot_routes, "_reconcile_report",
                        lambda: {"husks": [], "checked": 0})
    monkeypatch.setattr(graph_snapshot_routes, "_claim_repair_slot",
                        lambda: pytest.fail("a check-only reconcile claimed the repair slot"))
    response = _send(client, "POST", "/api/admin/graph/snapshot/reconcile", {}, admin=True)
    assert response.status_code == 200, response.text
    assert response.json() == {"report": {"husks": [], "checked": 0}}


def test_a_repair_reconcile_claims_nothing_during_a_restore(client, restoring, monkeypatch):
    monkeypatch.setattr(graph_snapshot_routes, "_reconcile_report",
                        lambda: pytest.fail("the repair read the report before its refusal"))
    monkeypatch.setattr(graph_snapshot_routes, "_claim_repair_slot",
                        lambda: pytest.fail("a refused repair claimed the repair slot"))
    response = _send(client, "POST", "/api/admin/graph/snapshot/reconcile",
                     {"repair": True}, admin=True)
    assert response.status_code == 503


def test_a_refused_import_starts_no_job(client, restoring, monkeypatch):
    monkeypatch.setattr(graph_snapshot_routes.graph_snapshot, "validate_snapshot",
                        lambda _s: pytest.fail("a refused import read its snapshot"))
    monkeypatch.setattr(graph_snapshot_routes, "_run_import_job",
                        lambda _s: pytest.fail("a refused import started its job"))
    response = _send(client, "POST", "/api/admin/graph/snapshot/import",
                     {"confirm": "wipe-and-restore", "snapshot": {}}, admin=True)
    assert response.status_code == 503


# ── Graph repair: refused only when it would write ───────────────────────────
# ``POST /api/neptune/graph/repair`` with ``{"dry_run": true}`` runs every step
# against a no-op writer (``server._repair_graph_sync``), so it stays allowed
# during a restore, like a check-only reconcile. Any other body is refused,
# after the steward check and after the body is read.

_REPAIR = "/api/neptune/graph/repair"


def _repair_client() -> TestClient:
    from fastapi.routing import APIRoute
    from agents import server
    app = FastAPI()
    startup_restore.install(app)

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.is_admin = request.headers.get("x-test-admin") == "1"
        return await call_next(request)

    app.router.routes.append(next(r for r in server.app.routes
                                  if isinstance(r, APIRoute) and r.path == _REPAIR))
    return TestClient(app)


@pytest.fixture
def graph_writes(monkeypatch):
    """Every graph read answers no rows; every graph write is recorded."""
    from agents import db
    writes: list = []
    monkeypatch.setattr(db, "neptune_query", lambda cypher, parameters=None: [])
    monkeypatch.setattr(db, "neptune_write",
                        lambda cypher, parameters=None: writes.append(cypher) or True)
    return writes


@pytest.fixture
def repair_runs(monkeypatch):
    """Every body ``server._repair_graph_sync`` is called with (the repair
    itself, the first step that can write), and the real one still runs. The
    graph above answers no rows, so a repair that ran would write nothing:
    the refusal tests check the repair never started, not just no writes."""
    from agents import server
    real, bodies = server._repair_graph_sync, []
    monkeypatch.setattr(server, "_repair_graph_sync",
                        lambda body=None: bodies.append(body) or real(body))
    return bodies


def test_a_dry_run_graph_repair_runs_during_a_restore(restoring, graph_writes, repair_runs):
    response = _repair_client().post(_REPAIR, json={"dry_run": True},
                                     headers={"x-test-admin": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True
    assert repair_runs == [{"dry_run": True}]
    assert graph_writes == []


@pytest.mark.parametrize("body", [None, {}, {"dry_run": False}, {"dry_run": "true"}],
                         ids=["no body", "empty", "dry_run false", "dry_run not a bool"])
def test_a_writing_graph_repair_is_refused_during_a_restore(restoring, graph_writes,
                                                           repair_runs, body):
    response = _repair_client().post(_REPAIR, json=body, headers={"x-test-admin": "1"})
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": startup_restore.RESTORING_DETAIL,
                               "reason": "startup_restore"}
    assert repair_runs == []
    assert graph_writes == []


@pytest.mark.parametrize("body", [None, {"dry_run": True}], ids=["repair", "dry run"])
def test_a_non_steward_graph_repair_gets_403_during_a_restore(restoring, graph_writes,
                                                             repair_runs, body):
    response = _repair_client().post(_REPAIR, json=body, headers={"x-test-admin": "0"})
    assert response.status_code == 403, response.text
    assert repair_runs == []
    assert graph_writes == []
