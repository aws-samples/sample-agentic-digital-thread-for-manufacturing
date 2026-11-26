# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin feedback-exploration toggle ``GET/PUT /api/admin/feedback-exploration``.

Mirrors ``test_admin_live_updates`` (route harness) — admin-gated both ways,
offline (injected fake persister; no SSM). Pins:

* A9 GET and PUT are 403 for non-admins;
* A10 GET reports the effective state AND provenance for all three sources;
* A11 a persister write failure surfaces the llm-settings error shape; the
  happy path echoes the EFFECTIVE state;
* A12 a PUT under an active env kill is 409 and never touches the persister.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
import agents.feedback_exploration_settings as fxs


class _FlagPersister:
    def __init__(self, value=None, write_error=None):
        self.value = value
        self.write_error = write_error
        self.set_calls: list[bool] = []

    def get_flag(self):
        return self.value

    def set_flag(self, value: bool) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.set_calls.append(value)
        self.value = value


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


@pytest.fixture(autouse=True)
def _reset_seam(monkeypatch):
    monkeypatch.delenv("FEEDBACK_EXPLORATION_ENABLED", raising=False)
    fxs._reset()
    yield
    fxs._reset()


def _build_app(is_admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return app


# ── A9: admin gating on BOTH verbs ──────────────────────────────────────────

def test_non_admin_get_forbidden():
    client = TestClient(_build_app(is_admin=False))
    assert client.get("/api/admin/feedback-exploration").status_code == 403


def test_non_admin_put_forbidden():
    client = TestClient(_build_app(is_admin=False))
    resp = client.put("/api/admin/feedback-exploration",
                      json={"feedback_exploration_enabled": True})
    assert resp.status_code == 403


# ── A10: GET provenance for all three sources ───────────────────────────────

@pytest.mark.parametrize("env,flag,expected", [
    pytest.param("0", True, {"feedback_exploration_enabled": False,
                             "source": "env-kill", "env_kill_active": True},
                 id="env-kill-beats-admin-on"),
    pytest.param(None, True, {"feedback_exploration_enabled": True,
                              "source": "admin-flag", "env_kill_active": False},
                 id="admin-flag-on"),
    pytest.param(None, False, {"feedback_exploration_enabled": False,
                               "source": "admin-flag", "env_kill_active": False},
                 id="admin-flag-off"),
    pytest.param(None, None, {"feedback_exploration_enabled": False,
                              "source": "default", "env_kill_active": False},
                 id="default-off"),
    pytest.param("1", None, {"feedback_exploration_enabled": False,
                             "source": "default", "env_kill_active": False},
                 id="truthy-env-is-not-an-enable"),
])
def test_get_reports_state_and_provenance(monkeypatch, env, flag, expected):
    if env is not None:
        monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", env)
    fxs.configure(_FlagPersister(flag))
    client = TestClient(_build_app(is_admin=True))
    assert client.get("/api/admin/feedback-exploration").json() == expected


# ── A11: PUT happy path + fail-loud SSM error shape ─────────────────────────

def test_put_enables_and_echoes_effective_state():
    persister = _FlagPersister(None)
    fxs.configure(persister)
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/feedback-exploration",
                      json={"feedback_exploration_enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok",
                           "feedback_exploration_enabled": True,
                           "source": "admin-flag", "env_kill_active": False}
    assert persister.set_calls == [True]

    resp = client.put("/api/admin/feedback-exploration",
                      json={"feedback_exploration_enabled": False})
    assert resp.json()["feedback_exploration_enabled"] is False
    assert resp.json()["source"] == "admin-flag"


def test_put_ssm_write_failure_surfaces_error():
    persister = _FlagPersister(None, write_error=RuntimeError("boom"))
    fxs.configure(persister)
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/feedback-exploration",
                      json={"feedback_exploration_enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["message"].startswith("SSM write failed:")
    # the failed write never flipped the effective state
    assert fxs.exploration_enabled() is False


# ── A12: PUT under an active env kill ───────────────────────────────────────

def test_put_under_env_kill_is_409_and_persister_untouched(monkeypatch):
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", "0")
    persister = _FlagPersister(None)
    fxs.configure(persister)
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/feedback-exploration",
                      json={"feedback_exploration_enabled": True})
    assert resp.status_code == 409
    assert "kill switch" in resp.json()["detail"]
    assert persister.set_calls == []         # no phantom write


# ── live-updates conventions: keyless PUT + invalid JSON ────────────────────

def test_put_without_key_returns_current():
    fxs.configure(_FlagPersister(True))
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/feedback-exploration", json={"other": 1})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok",
                           "feedback_exploration_enabled": True,
                           "source": "admin-flag", "env_kill_active": False}


def test_put_invalid_json_body():
    client = TestClient(_build_app(is_admin=True))
    resp = client.put("/api/admin/feedback-exploration",
                      content=b"not-json",
                      headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "message": "Invalid JSON body"}


# ── B3: status route 403/404/200 ────────────────────────────────────────────

def _seed_status(monkeypatch, exploration_id: str, record: dict):
    import agents.feedback_exploration as fx
    from agents.job_store import JobStore
    store = JobStore("feedback-explore", table=None)
    store[exploration_id] = record
    monkeypatch.setattr(fx, "_JOBS", store)


def test_status_non_admin_forbidden(monkeypatch):
    _seed_status(monkeypatch, "c" * 16, {"status": "done", "outcome": "filed"})
    client = TestClient(_build_app(is_admin=False))
    resp = client.get(f"/api/admin/feedback-exploration/status/{'c' * 16}")
    assert resp.status_code == 403


def test_status_unknown_id_is_404(monkeypatch):
    _seed_status(monkeypatch, "c" * 16, {"status": "done"})
    client = TestClient(_build_app(is_admin=True))
    assert client.get(
        f"/api/admin/feedback-exploration/status/{'d' * 16}").status_code == 404
    # reserved registry keys are 404 too — the shape anchor holds at the route
    assert client.get(
        "/api/admin/feedback-exploration/status/active").status_code == 404


def test_status_known_id_returns_allowlisted_record(monkeypatch):
    _seed_status(monkeypatch, "c" * 16,
                 {"status": "done", "outcome": "filed", "mapping_id": "mid-9",
                  "diff": {"systems_beyond_cited": ["cmms"]},
                  "started_at": 123.0})
    client = TestClient(_build_app(is_admin=True))
    resp = client.get(f"/api/admin/feedback-exploration/status/{'c' * 16}")
    assert resp.status_code == 200
    assert resp.json() == {"status": "done", "outcome": "filed",
                           "mapping_id": "mid-9",
                           "diff": {"systems_beyond_cited": ["cmms"]}}
