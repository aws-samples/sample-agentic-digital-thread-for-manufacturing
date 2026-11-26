# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``POST /api/onboard/vocabulary/import`` (ADR-0068 flag 2).

The onboarding surface needs a vocabulary-import door an INTEGRATOR can use
(the admin route is Admin-gated via ``ADMIN_PREFIXES``), so a thin
onboard-gated route delegates to the SAME ``vocabulary_routes._run_import``
writer — one classifier, one writer, no drift. Pins:

  * both routes call the ONE writer with identical ``(slug, rows)``;
  * the onboard route honors ``require_onboard_access`` (403 for a caller who
    is neither integrator nor Admin; 200 for an integrator);
  * boundary validation parity — a bad body 400s before the writer runs;
  * the admin route's behavior is untouched (same writer, same response).

``fastapi.testclient`` with ``_run_import`` monkeypatched — no AWS, no graph.
(The route-level gate wiring is additionally covered by
``test_onboard_route_gate.py``'s router walker, which asserts EVERY
/api/onboard route inherits the dependency.)
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.vocabulary_routes as vr  # noqa: E402
from agents.onboard import router as onboard_router  # noqa: E402

_BODY = {
    "metadata": {"name": "onboarding-proposal-system-a"},
    "concepts": [
        {"id": "work_state", "name": "Work state", "description": "d"},
    ],
}


class _StateMiddleware(BaseHTTPMiddleware):
    """Stand-in for AuthMiddleware's request.state group flags."""

    def __init__(self, app, *, is_admin=False, is_integrator=False):
        super().__init__(app)
        self._flags = {"is_admin": is_admin, "is_integrator": is_integrator}

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._flags["is_admin"]
        request.state.is_integrator = self._flags["is_integrator"]
        return await call_next(request)


def _client(*, is_admin=False, is_integrator=False) -> TestClient:
    app = FastAPI()
    app.include_router(vr.router)
    app.include_router(onboard_router)
    app.add_middleware(_StateMiddleware, is_admin=is_admin,
                       is_integrator=is_integrator)
    return TestClient(app)


@pytest.fixture
def run_import_spy(monkeypatch):
    """Replace the ONE writer with a spy — both routes must reach exactly it."""
    calls: list[tuple] = []

    def fake(slug, rows):
        calls.append((slug, rows))
        return {"status": "ok", "slug": slug, "inserted": len(rows),
                "skipped": 0, "failed": 0, "skipped_ids": [], "collisions": []}

    monkeypatch.setattr(vr, "_run_import", fake)
    return calls


def test_onboard_route_denies_without_integrator_or_admin(run_import_spy):
    resp = _client().post("/api/onboard/vocabulary/import", json=_BODY)
    assert resp.status_code == 403
    assert run_import_spy == []  # gate runs BEFORE the writer


def test_onboard_route_allows_integrator_and_calls_the_one_writer(run_import_spy):
    resp = _client(is_integrator=True).post(
        "/api/onboard/vocabulary/import", json=_BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert len(run_import_spy) == 1


def test_both_routes_call_the_same_writer_with_identical_args(run_import_spy):
    admin = _client(is_admin=True)
    admin.post("/api/admin/vocabulary/import", json=_BODY)
    admin.post("/api/onboard/vocabulary/import", json=_BODY)
    assert len(run_import_spy) == 2
    assert run_import_spy[0] == run_import_spy[1]  # same (slug, rows) both ways
    slug, rows = run_import_spy[0]
    assert slug == "onboarding_proposal_system_a"
    assert rows == [{"id": "work_state", "name": "Work state",
                     "description": "d"}]


def test_onboard_route_shares_the_boundary_validation(run_import_spy):
    resp = _client(is_integrator=True).post(
        "/api/onboard/vocabulary/import", json={"concepts": []})
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert run_import_spy == []  # a bad body never reaches the writer


def test_admin_route_response_shape_is_unchanged(run_import_spy):
    resp = _client(is_admin=True).post("/api/admin/vocabulary/import", json=_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status", "slug", "inserted", "skipped", "failed",
                         "skipped_ids", "collisions"}
