# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the admin demo toggle ``GET/PUT /api/admin/settings`` (S4.4b).

The settings route is admin-gated (``_check_admin`` raises 403 for non-admins)
and coordinates the demo seam: ``PUT {"demo_enabled": true}`` sets the runtime
override and seeds the registry; ``false`` clears it and unseeds. A tiny
admin-injecting middleware drives ``request.state.is_admin`` (the AuthMiddleware
admin gate itself is covered in test_correction_route / test_demo_routes). The
demo persister is left unconfigured so no real SSM is touched, and the prompt
store is the in-memory ``_FakePromptStore``. No AWS, no DynamoDB, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
from agents import demo, prompts


class _FakePromptStore:
    """Tiny in-memory stand-in for PromptTemplateStorage."""

    def __init__(self):
        self._items = {}

    def list_all(self, source=None):
        if source is None:
            return list(self._items.values())
        return [v for v in self._items.values() if v.get("source") == source]

    def save(self, item):
        self._items[item["template_id"]] = item

    def delete(self, template_id):
        self._items.pop(template_id, None)

    def get(self, template_id):
        return self._items.get(template_id)


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    """Set ``request.state.is_admin`` so ``_check_admin`` can be exercised
    without a Cognito session."""

    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


@pytest.fixture(autouse=True)
def _reset_demo_state():
    demo._override = None
    demo._persister = None  # offline — no SSM write-through
    yield
    demo._override = None
    demo._persister = None


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakePromptStore()
    monkeypatch.setattr(prompts, "_prompt_store", store)
    return store


def _build_app(is_admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return app


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------

def test_non_admin_put_forbidden(fake_store):
    client = TestClient(_build_app(is_admin=False))
    resp = client.put("/api/admin/settings", json={"demo_enabled": True})
    assert resp.status_code == 403


def test_non_admin_get_forbidden(fake_store):
    client = TestClient(_build_app(is_admin=False))
    resp = client.get("/api/admin/settings")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Admin toggle → seed / unseed
# ---------------------------------------------------------------------------

def test_admin_put_enable_seeds_registry(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    client = TestClient(_build_app(is_admin=True))

    resp = client.put("/api/admin/settings", json={"demo_enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "demo_enabled": True}

    seeded = fake_store.list_all(source="registry")
    assert len(seeded) == len(demo.demo_prompts())
    assert all(item["source"] == "registry" for item in seeded)
    assert demo.is_enabled() is True


def test_admin_put_disable_unseeds_registry(fake_store, monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    client = TestClient(_build_app(is_admin=True))

    # Pre-seed via enable, plus a user prompt that must survive the disable.
    client.put("/api/admin/settings", json={"demo_enabled": True})
    fake_store.save({"template_id": "u1", "source": "user", "text": "mine"})
    assert len(fake_store.list_all(source="registry")) == len(demo.demo_prompts())

    resp = client.put("/api/admin/settings", json={"demo_enabled": False})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "demo_enabled": False}

    assert fake_store.list_all(source="registry") == []
    assert demo.is_enabled() is False
    # User prompt untouched.
    users = fake_store.list_all(source="user")
    assert len(users) == 1
    assert users[0]["template_id"] == "u1"


# ---------------------------------------------------------------------------
# GET reflects current state
# ---------------------------------------------------------------------------

def test_admin_get_reflects_current_demo_enabled(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    client = TestClient(_build_app(is_admin=True))

    assert client.get("/api/admin/settings").json() == {"demo_enabled": False}

    client.put("/api/admin/settings", json={"demo_enabled": True})
    assert client.get("/api/admin/settings").json() == {"demo_enabled": True}


def test_admin_put_without_demo_enabled_returns_current(fake_store, monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    client = TestClient(_build_app(is_admin=True))

    resp = client.put("/api/admin/settings", json={"other": "value"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "demo_enabled": False}


def test_admin_put_invalid_json_body(fake_store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(
        "/api/admin/settings",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "message": "Invalid JSON body"}
