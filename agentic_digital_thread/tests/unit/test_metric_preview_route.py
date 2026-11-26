# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0080 D1/D4 — the preview route's ``resolve`` / ``with`` parameters.

Pins: without ``resolve`` the helper is called EXACTLY as before (positional
mapping id — structural preview byte-unchanged); ``resolve=1`` forwards the
parsed ``with=`` ids; a malformed ``with=`` is a named 400 that never reaches
the helper; a non-boolean ``resolve`` is 422; the route sits under the
admin-gated prefix; and the async handler only offloads (resolver lane).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.self_learning as self_learning_mod
import agents.vocabulary_routes as vr
from agents.auth.middleware import AuthMiddleware

_PATH = "/api/admin/vocabulary/metrics/prop-1/preview"


class _Admin(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_Admin)
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    def helper(*args, **kwargs):
        seen.append((args, kwargs))
        return {"diff": None, **({"dry_run": {"status": "ok"}}
                                 if kwargs.get("resolve") else {})}
    monkeypatch.setattr(self_learning_mod, "preview_metric_definition_mapping", helper)
    return seen


def test_without_resolve_the_helper_call_is_unchanged(client, calls):
    resp = client.get(_PATH)
    assert resp.status_code == 200
    assert resp.json() == {"diff": None}
    assert calls == [(("prop-1",), {})]


def test_resolve_false_is_the_structural_path(client, calls):
    assert client.get(_PATH, params={"resolve": "0", "with": "x"}).json() == {"diff": None}
    assert calls == [(("prop-1",), {})]


def test_resolve_forwards_parsed_with_ids(client, calls):
    resp = client.get(_PATH, params={"resolve": "1", "with": "prop-2, prop-3"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] == {"status": "ok"}
    assert calls == [(("prop-1",), {"resolve": True,
                                    "with_ids": ("prop-2", "prop-3")})]


@pytest.mark.parametrize("raw", ["prop-1", "a,a", "bad id"])
def test_malformed_with_is_a_named_400(client, calls, raw):
    resp = client.get(_PATH, params={"resolve": "1", "with": raw})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_with_parameter"
    assert calls == []


def test_resolved_responses_are_never_http_cacheable(client, calls):
    """A dry-run is computed from CURRENT data per request: the 200 AND the
    400 carry ``Cache-Control: private, no-store``."""
    ok = client.get(_PATH, params={"resolve": "1"})
    bad = client.get(_PATH, params={"resolve": "1", "with": "a,a"})
    for resp in (ok, bad):
        assert resp.headers["cache-control"] == "private, no-store"


def test_structural_response_headers_are_unchanged(client, calls):
    assert "cache-control" not in client.get(_PATH).headers


def test_resolved_helper_error_status_is_no_store(client, monkeypatch):
    monkeypatch.setattr(self_learning_mod, "preview_metric_definition_mapping",
                        lambda *a, **k: {"http_status": 404, "error": "gone"})
    resp = client.get(_PATH, params={"resolve": "1"})
    assert resp.status_code == 404 and resp.json() == {"error": "gone"}
    assert resp.headers["cache-control"] == "private, no-store"


def test_non_boolean_resolve_is_422(client, calls):
    assert client.get(_PATH, params={"resolve": "maybe"}).status_code == 422
    assert calls == []


def test_helper_error_status_survives_resolve(client, monkeypatch):
    monkeypatch.setattr(
        self_learning_mod, "preview_metric_definition_mapping",
        lambda *a, **k: {"error": "with_mapping_not_found", "http_status": 404,
                         "mapping_id": "prop-9"})
    resp = client.get(_PATH, params={"resolve": "1", "with": "prop-9"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "with_mapping_not_found", "mapping_id": "prop-9"}


def test_preview_route_is_admin_gated_get():
    routes = [r for r in vr.router.routes
              if r.path == "/api/admin/vocabulary/metrics/{mapping_id}/preview"]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    assert any(routes[0].path.startswith(p) for p in AuthMiddleware.ADMIN_PREFIXES)
