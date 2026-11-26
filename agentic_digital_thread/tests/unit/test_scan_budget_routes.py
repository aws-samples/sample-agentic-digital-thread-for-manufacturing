# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the admin per-system Athena scan-budget routes
(``GET/PUT /api/admin/scan-budget/{system_id}``).

Admin-gated (``_check_admin`` -> 403 for non-admins), 404 for an unknown system.
The persistence + resolution are stubbed with an in-memory store (the real graph
is VPC-private, so ``write_scan_budget``/``resolve_connection`` are patched) — this
proves the route round-trip (set -> GET reflects, clear -> GET null) and the
fail-closed validation. No AWS, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
from agents import catalog as catalog_mod
from agents import connection_resolver as cr


_KNOWN = "sys_athena"


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


def _build_app(is_admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return app


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for the System-node connection_config budget."""
    _store: dict = {}

    def fake_get(system_id):
        return {"id": system_id} if system_id == _KNOWN else None

    def fake_resolve(system_id):
        return {"system_id": system_id, "connection_type": "Athena",
                "max_scanned_bytes": _store.get(system_id)}

    def fake_write(system_id, budget):
        if budget is None:
            _store.pop(system_id, None)
        else:
            _store[system_id] = int(budget)

    monkeypatch.setattr(catalog_mod.catalog, "get", fake_get)
    monkeypatch.setattr(cr, "resolve_connection", fake_resolve)
    monkeypatch.setattr(cr, "write_scan_budget", fake_write)
    # Silence the audit log write path (no real logging assertions here).
    monkeypatch.setattr(cr, "audit_connection_event", lambda *a, **k: None)
    return _store


# --- admin gating -----------------------------------------------------------

def test_non_admin_get_forbidden(store):
    client = TestClient(_build_app(is_admin=False))
    assert client.get(f"/api/admin/scan-budget/{_KNOWN}").status_code == 403


def test_non_admin_put_forbidden(store):
    client = TestClient(_build_app(is_admin=False))
    resp = client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": 1000})
    assert resp.status_code == 403


# --- unknown system ---------------------------------------------------------

def test_get_unknown_system_404(store):
    client = TestClient(_build_app(is_admin=True))
    assert client.get("/api/admin/scan-budget/nope").status_code == 404


def test_put_unknown_system_404(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/scan-budget/nope", json={"max_scanned_bytes": 1000})
    assert resp.status_code == 404


# --- round-trip -------------------------------------------------------------

def test_get_default_is_null(store, monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    client = TestClient(_build_app(is_admin=True))
    body = client.get(f"/api/admin/scan-budget/{_KNOWN}").json()
    assert body["system_id"] == _KNOWN
    assert body["max_scanned_bytes"] is None
    assert body["effective_max_scanned_bytes"] is None
    assert body["connection_type"] == "Athena"


def test_put_then_get_round_trips(store, monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    client = TestClient(_build_app(is_admin=True))

    resp = client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": 4096})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "system_id": _KNOWN, "max_scanned_bytes": 4096}

    body = client.get(f"/api/admin/scan-budget/{_KNOWN}").json()
    assert body["max_scanned_bytes"] == 4096
    assert body["effective_max_scanned_bytes"] == 4096


def test_put_null_clears_budget(store, monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    client = TestClient(_build_app(is_admin=True))
    client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": 4096})

    resp = client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": None})
    assert resp.status_code == 200
    assert resp.json()["max_scanned_bytes"] is None
    assert store.get(_KNOWN) is None
    assert client.get(f"/api/admin/scan-budget/{_KNOWN}").json()["max_scanned_bytes"] is None


def test_get_reflects_env_override_when_unset(store, monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "7777")
    client = TestClient(_build_app(is_admin=True))
    body = client.get(f"/api/admin/scan-budget/{_KNOWN}").json()
    # No per-system value, but the env override drives the effective cap.
    assert body["max_scanned_bytes"] is None
    assert body["effective_max_scanned_bytes"] == 7777


# --- validation -------------------------------------------------------------

def test_put_malformed_value_400(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": "abc"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_input"


def test_put_nonpositive_value_400(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/scan-budget/{_KNOWN}", json={"max_scanned_bytes": 0})
    assert resp.status_code == 400
