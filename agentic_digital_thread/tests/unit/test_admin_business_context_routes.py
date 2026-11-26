# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the admin business-context routes (ADR-0054 C4a) —
``GET/PUT /api/admin/business-context/{system_id}``.

Admin-gated (``_check_admin`` → 403 non-admin), 404 for an unknown system,
validation 400s with the pinned T3 reasons
(``invalid_input`` | ``too_long`` | ``secret_like_content``), 500 fail-loud on
a graph-write failure (BEFORE any runtime refresh), and truthful
``runtime_refresh`` reporting against the T6 delivery seam
(``agent_manager.refresh_business_context`` → ``"ok"|"failed"|"skipped"``).

Style mirrors ``test_scan_budget_routes.py``: the REAL router is mounted on a
bare app whose middleware stamps ``request.state.is_admin``; the graph IO
(``business_context.write/read``) is replaced by an in-memory store; the pure
sanitize/validate/hash helpers run FOR REAL so the pinned 400 reasons are
exercised genuinely. No AWS, no Neptune, no network.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
import agents.business_context as bc
from agents import catalog as catalog_mod
from agents import connection_resolver as cr


_KNOWN = "sys_alpha"


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


class _FakeManager:
    """T6-contract stand-in: refresh_business_context(system_id) -> str."""

    def __init__(self, result="ok", exc=None):
        self.result = result
        self.exc = exc
        self.calls: list[str] = []

    def refresh_business_context(self, system_id: str) -> str:
        self.calls.append(system_id)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture(autouse=True)
def _no_ambient_manager():
    """Isolate the injected AgentManager per test (module-global seam)."""
    admin_routes.set_agent_manager(None)
    yield
    admin_routes.set_agent_manager(None)


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for the System node's business-context props."""
    _store: dict[str, dict] = {}

    def fake_get(system_id):
        return {"id": system_id} if system_id == _KNOWN else None

    def fake_write(system_id, text, enabled):
        _store[system_id] = {"business_context": text, "enabled": bool(enabled),
                             "updated_at": "2026-07-27T18:00:00Z"}
        return True

    def fake_read(system_id):
        row = _store.get(system_id, {})
        text = row.get("business_context") or ""
        return {"business_context": text,
                "enabled": bool(row.get("enabled")),
                "updated_at": row.get("updated_at") or None,
                "context_hash": bc.context_hash(text)}

    monkeypatch.setattr(catalog_mod.catalog, "get", fake_get)
    monkeypatch.setattr(bc, "write_business_context", fake_write)
    monkeypatch.setattr(bc, "read_business_context", fake_read)
    return _store


@pytest.fixture
def audit_events(monkeypatch):
    """Spy on audit_connection_event — captures every call's full payload."""
    events: list[dict] = []

    def spy(action, system_id, actor, outcome, *, extra=None):
        events.append({"action": action, "system_id": system_id,
                       "actor": actor, "outcome": outcome,
                       "extra": dict(extra) if extra else {}})

    monkeypatch.setattr(cr, "audit_connection_event", spy)
    return events


# --- admin gating (house pattern: in-handler _check_admin) -------------------

def test_put_non_admin_403(store, audit_events):
    client = TestClient(_build_app(is_admin=False))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 403
    assert store == {}  # gate fires before any write


def test_get_non_admin_403(store):
    client = TestClient(_build_app(is_admin=False))
    assert client.get(f"/api/admin/business-context/{_KNOWN}").status_code == 403


# --- unknown system -----------------------------------------------------------

def test_put_unknown_system_404(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/business-context/nope",
                      json={"business_context": "context"})
    assert resp.status_code == 404


def test_get_unknown_system_404(store):
    client = TestClient(_build_app(is_admin=True))
    assert client.get("/api/admin/business-context/nope").status_code == 404


# --- PUT happy path -----------------------------------------------------------

def test_put_valid_context_writes_and_refreshes(store, audit_events):
    mgr = _FakeManager(result="ok")
    admin_routes.set_agent_manager(mgr)
    client = TestClient(_build_app(is_admin=True))

    text = "Plant-floor system of record for production orders."
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": text, "enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    # C4a keys EXACT — no extras, no omissions. (T1.5 amendment: the response
    # gained the additive `readiness` block — the batched roll coordinator's
    # per-system report; null when the manager has no coordinator seam.)
    assert set(body) == {"status", "system_id", "enabled", "context_hash",
                         "runtime_refresh", "readiness"}
    assert body["readiness"] is None  # T6-only fake manager — no coordinator
    assert body["status"] == "ok"
    assert body["system_id"] == _KNOWN
    assert body["enabled"] is True
    assert body["context_hash"] == bc.context_hash(text)
    assert body["runtime_refresh"] == "ok"
    assert store[_KNOWN]["business_context"] == text
    assert mgr.calls == [_KNOWN]


def test_put_enabled_defaults_true_and_empty_clears(store):
    admin_routes.set_agent_manager(_FakeManager())
    client = TestClient(_build_app(is_admin=True))
    # enabled omitted → default true (C4a).
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "some context"})
    assert resp.json()["enabled"] is True
    # "" clears: hash null, write still lands.
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "", "enabled": False})
    assert resp.status_code == 200
    assert resp.json()["context_hash"] is None
    assert store[_KNOWN]["business_context"] == ""


def test_put_sanitizes_before_write(store):
    admin_routes.set_agent_manager(_FakeManager())
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "  line one\r\nline two\x00  "})
    assert resp.status_code == 200
    assert store[_KNOWN]["business_context"] == "line one\nline two"


# --- validation 400s (pinned T3 reasons) ---------------------------------------

def test_put_secret_like_rejected_400_reason_pinned(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "password: hunter2-not-a-real-one"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "validation_failed"
    assert body["reason"] == "secret_like_content"
    assert "hunter2" not in json.dumps(body)  # detail never echoes the text
    assert store == {}  # rejected writes never reach the graph


def test_put_too_long_rejected_400(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "x" * (bc.MAX_BUSINESS_CONTEXT_LEN + 1)})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "too_long"
    assert store == {}


@pytest.mark.parametrize("body", [
    {"business_context": 42},          # non-string context
    {"enabled": True},                 # missing business_context field
    {"business_context": "x", "enabled": "yes"},  # non-bool enabled
], ids=["non-string", "missing-field", "non-bool-enabled"])
def test_put_invalid_input_400(store, body):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}", json=body)
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_input"
    assert store == {}


def test_put_malformed_json_400(store):
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      content=b"not json",
                      headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_input"


# --- graph-write failure (fail-loud 500, refresh never runs) --------------------

def test_put_graph_write_failure_500_after_no_refresh(store, audit_events, monkeypatch):
    monkeypatch.setattr(bc, "write_business_context", lambda *a, **k: False)

    class _Boom:
        def refresh_business_context(self, _sid):
            raise AssertionError("refresh must not run after a failed graph write")

    admin_routes.set_agent_manager(_Boom())
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 500
    assert resp.json() == {"status": "error",
                           "detail": "business context update failed (see server logs)"}
    assert [e["outcome"] for e in audit_events] == ["error"]


def test_put_graph_write_exception_also_500(store, monkeypatch, audit_events):
    def _raise(*_a, **_k):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(bc, "write_business_context", _raise)
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 500


# --- runtime refresh reporting (truthful, never rolls back) ---------------------

def test_put_refresh_failure_reports_failed_not_500(store, audit_events):
    admin_routes.set_agent_manager(_FakeManager(result="failed"))
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 200
    assert resp.json()["runtime_refresh"] == "failed"
    assert store[_KNOWN]["business_context"] == "context"  # durable write kept


def test_put_refresh_exception_reports_failed(store):
    admin_routes.set_agent_manager(_FakeManager(exc=RuntimeError("boto down")))
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 200
    assert resp.json()["runtime_refresh"] == "failed"


def test_put_refresh_skipped_when_no_manager(store, monkeypatch):
    # No live AgentManager anywhere (local mode) → "skipped". The
    # agents_routes fallback is pinned to None so a sibling test's injected
    # manager can never leak into this worker.
    monkeypatch.setattr(admin_routes, "_get_agent_manager", lambda: None)
    client = TestClient(_build_app(is_admin=True))
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": "context"})
    assert resp.status_code == 200
    assert resp.json()["runtime_refresh"] == "skipped"


# --- GET shape -------------------------------------------------------------------

def test_get_roundtrip_shape(store):
    admin_routes.set_agent_manager(_FakeManager())
    client = TestClient(_build_app(is_admin=True))
    # Unset → the honest empty C4a shape.
    body = client.get(f"/api/admin/business-context/{_KNOWN}").json()
    assert body == {"system_id": _KNOWN, "business_context": "",
                    "enabled": False, "updated_at": None, "context_hash": None}
    # PUT then GET reflects the stored value + hash.
    text = "Maintenance history and work orders live here."
    client.put(f"/api/admin/business-context/{_KNOWN}",
               json={"business_context": text, "enabled": True})
    body = client.get(f"/api/admin/business-context/{_KNOWN}").json()
    assert set(body) == {"system_id", "business_context", "enabled",
                         "updated_at", "context_hash"}
    assert body["business_context"] == text
    assert body["enabled"] is True
    assert body["updated_at"] == "2026-07-27T18:00:00Z"
    assert body["context_hash"] == bc.context_hash(text)


# --- audit hygiene ------------------------------------------------------------------

def test_audit_event_carries_hash_never_text(store, audit_events):
    admin_routes.set_agent_manager(_FakeManager())
    client = TestClient(_build_app(is_admin=True))
    text = "Distinctive-steward-sentence that must never be audited."
    resp = client.put(f"/api/admin/business-context/{_KNOWN}",
                      json={"business_context": text, "enabled": True})
    assert resp.status_code == 200
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["action"] == "business_context"
    assert event["outcome"] == "ok"
    assert event["extra"] == {"context_hash": bc.context_hash(text),
                              "enabled": True, "length": len(text)}
    assert "Distinctive-steward-sentence" not in json.dumps(event)
