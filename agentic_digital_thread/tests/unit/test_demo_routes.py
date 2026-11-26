# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline auth tests for ``GET /api/demo/status`` (S4.4b demo runtime-toggle).

``workshop`` reports workshop mode, so the UI skips the steward story GET
(``/api/admin/workshop/story``, a 404 when workshop mode is off) on a demo
deploy instead of logging a failed request on every page load.

The route is **session-authed** — neither public nor under ``/api/admin`` — so
``AuthMiddleware`` requires a valid session for any logged-in user (admin not
required). Mirrors the FastAPI + ``AuthMiddleware`` + ``TestClient`` pattern from
``test_correction_route.py`` (with the complete local-auth fixture). No AWS, no
Cognito, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import demo
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.demo_routes import router as demo_router


@pytest.fixture(autouse=True)
def _reset_demo_state():
    demo._override = None
    demo._persister = None
    yield
    demo._override = None
    demo._persister = None


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(demo_router)
    return app


def test_demo_status_requires_auth(monkeypatch):
    # No Cognito configured and DEV_NO_AUTH unset ⇒ the /api/ route is rejected
    # 401 (session-authed, not public).
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_demo_status_ok_for_non_admin_session(monkeypatch, local_auth_env):
    # The complete local-auth fixture grants the documented development session;
    # /api/demo/status is not under /api/admin, so it returns 200.
    monkeypatch.delenv("ADT_DEMO", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"enabled", "workshop"}
    assert isinstance(body["enabled"], bool)
    assert body["enabled"] is False
    assert body["workshop"] is False


def test_demo_status_reflects_runtime_override(monkeypatch, local_auth_env):
    # The status mirrors demo.is_enabled(): a runtime override flips it on.
    monkeypatch.delenv("ADT_DEMO", raising=False)
    get_config.cache_clear()
    demo.set_override(True)
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "workshop": False}


def test_demo_status_hidden_in_workshop_mode(monkeypatch, local_auth_env):
    # --workshop implies --simulator, so ADT_DEMO is on — but the ▶ Demo kiosk
    # button hijacks an attendee's chat/session. Workshop mode reports the
    # button as disabled while leaving the demo addon itself enabled.
    monkeypatch.setenv("ADT_DEMO", "1")
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "workshop": True}
    # The gate is on the BUTTON only — the addon stays on for the seed/registration.
    assert demo.is_enabled() is True


def test_demo_status_workshop_gate_beats_runtime_override(monkeypatch, local_auth_env):
    # An admin runtime override cannot resurrect the button in a workshop.
    monkeypatch.delenv("ADT_DEMO", raising=False)
    monkeypatch.setenv("ADT_WORKSHOP", "true")
    get_config.cache_clear()
    demo.set_override(True)
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "workshop": True}


def test_demo_status_workshop_off_does_not_gate(monkeypatch, local_auth_env):
    # Explicit ADT_WORKSHOP=0 (or unset) leaves the pre-existing behaviour intact.
    monkeypatch.setenv("ADT_DEMO", "1")
    monkeypatch.setenv("ADT_WORKSHOP", "0")
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/demo/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "workshop": False}
