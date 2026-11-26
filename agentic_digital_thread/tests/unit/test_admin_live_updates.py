# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the admin live-updates toggle ``GET/POST /api/admin/live-updates``.

Mirrors ``test_admin_settings_demo`` (the demo toggle route tests): the route is
admin-gated (``_check_admin`` raises 403 for non-admins), ``POST
{"live_updates_enabled": bool}`` sets the runtime override via
``live_settings.set_live_updates``, and ``GET`` reflects the current state.
Default is ON. The persister is left unconfigured so no real SSM is touched. No
AWS, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
from agents import live_settings


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


@pytest.fixture(autouse=True)
def _reset_live_state(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    live_settings._override = None
    live_settings._persister = None  # offline — no SSM write-through
    live_settings._interval_override = None
    live_settings._interval_persister = None  # hermetic — lazy default not armed
    live_settings._interval_loaded = False
    yield
    live_settings._override = None
    live_settings._persister = None
    live_settings._interval_override = None
    live_settings._interval_persister = None
    live_settings._interval_loaded = False


def _build_app(is_admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return app


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------

def test_non_admin_get_forbidden():
    client = TestClient(_build_app(is_admin=False))
    assert client.get("/api/admin/live-updates").status_code == 403


def test_non_admin_post_forbidden():
    client = TestClient(_build_app(is_admin=False))
    resp = client.post("/api/admin/live-updates", json={"live_updates_enabled": False})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET reflects current state (default ON) + POST flips it
# ---------------------------------------------------------------------------

def test_get_defaults_to_enabled():
    client = TestClient(_build_app(is_admin=True))
    assert client.get("/api/admin/live-updates").json() == {
        "live_updates_enabled": True, "interval_seconds": 120}


def test_post_pause_then_resume():
    client = TestClient(_build_app(is_admin=True))

    resp = client.post("/api/admin/live-updates", json={"live_updates_enabled": False})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "live_updates_enabled": False,
                           "interval_seconds": 120}
    assert client.get("/api/admin/live-updates").json() == {
        "live_updates_enabled": False, "interval_seconds": 120}
    assert live_settings.live_updates_enabled() is False

    resp = client.post("/api/admin/live-updates", json={"live_updates_enabled": True})
    assert resp.json() == {"status": "ok", "live_updates_enabled": True,
                           "interval_seconds": 120}
    assert client.get("/api/admin/live-updates").json() == {
        "live_updates_enabled": True, "interval_seconds": 120}


def test_post_without_key_returns_current():
    client = TestClient(_build_app(is_admin=True))
    resp = client.post("/api/admin/live-updates", json={"other": "value"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "live_updates_enabled": True,
                           "interval_seconds": 120}


def test_post_invalid_json_body():
    client = TestClient(_build_app(is_admin=True))
    resp = client.post(
        "/api/admin/live-updates",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "message": "Invalid JSON body"}


# ---------------------------------------------------------------------------
# Snapshot interval alongside the flag (30..3600; invalid → ignored)
# ---------------------------------------------------------------------------

def test_post_interval_sets_and_get_reflects_it():
    client = TestClient(_build_app(is_admin=True))
    resp = client.post("/api/admin/live-updates", json={"interval_seconds": 300})
    assert resp.json() == {"status": "ok", "live_updates_enabled": True,
                           "interval_seconds": 300}
    assert client.get("/api/admin/live-updates").json() == {
        "live_updates_enabled": True, "interval_seconds": 300}
    assert live_settings.snapshot_interval_override() == 300


def test_post_flag_and_interval_together():
    client = TestClient(_build_app(is_admin=True))
    resp = client.post("/api/admin/live-updates",
                       json={"live_updates_enabled": False, "interval_seconds": 900})
    assert resp.json() == {"status": "ok", "live_updates_enabled": False,
                           "interval_seconds": 900}


@pytest.mark.parametrize("bad", [29, 3601, 0, "soon", None, True])
def test_post_invalid_interval_is_ignored(bad):
    client = TestClient(_build_app(is_admin=True))
    resp = client.post("/api/admin/live-updates", json={"interval_seconds": bad})
    # Ignored fail-soft: the echoed state is honest — still the 120s default.
    assert resp.json() == {"status": "ok", "live_updates_enabled": True,
                           "interval_seconds": 120}
    assert live_settings.snapshot_interval_override() is None


def test_post_invalid_interval_does_not_clobber_existing_override():
    client = TestClient(_build_app(is_admin=True))
    client.post("/api/admin/live-updates", json={"interval_seconds": 300})
    client.post("/api/admin/live-updates", json={"interval_seconds": "soon"})
    assert client.get("/api/admin/live-updates").json() == {
        "live_updates_enabled": True, "interval_seconds": 300}
