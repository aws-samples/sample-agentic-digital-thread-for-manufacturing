# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline auth/route tests for ``GET /api/coverage`` (S4.2).

The route is **session-authed** — neither public nor under ``/api/admin`` — so
``AuthMiddleware`` requires a valid session for any logged-in user (admin NOT
required). Mirrors the FastAPI + ``AuthMiddleware`` + ``TestClient`` pattern from
``test_demo_routes.py`` / ``test_correction_route.py`` (with the complete
local-auth fixture). No AWS, no graph, no network: ``covered_concepts``
is patched to an injected ``frozenset`` so the route never hits Neptune.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import coverage
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.coverage import router as coverage_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(coverage_router)
    return app


def test_coverage_requires_auth(monkeypatch):
    # No Cognito configured and DEV_NO_AUTH unset ⇒ the /api/ route is rejected
    # 401 (session-authed, not public). Path is NOT in PUBLIC_ROUTES and NOT under
    # /api/admin, so it is also NOT a 403-admin route.
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    monkeypatch.setattr(coverage, "covered_concepts", lambda: frozenset())
    client = TestClient(_build_app())

    resp = client.get("/api/coverage")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_coverage_ok_for_non_admin_session_empty_covered(monkeypatch, local_auth_env):
    # The complete local-auth fixture grants the development session. This route
    # is not under /api/admin, so it returns 200 without relying on that role.
    # Empty covered set ⇒ both seed cards uncovered with gaps.
    get_config.cache_clear()
    monkeypatch.setattr(coverage, "covered_concepts", lambda: frozenset())
    client = TestClient(_build_app())

    resp = client.get("/api/coverage")
    assert resp.status_code == 200
    body = resp.json()

    # Locked shape: {cards:[...], gaps:[...], summary:{total,satisfiable,uncovered}}.
    assert set(body.keys()) == {"cards", "gaps", "summary"}
    assert set(body["summary"].keys()) == {"total", "satisfiable", "uncovered"}
    for card in body["cards"]:
        assert set(card.keys()) == {"card_id", "satisfiable", "missing_concepts"}
    for gap in body["gaps"]:
        assert set(gap.keys()) == {"concept", "blocks_cards", "suggestion"}

    # Empty covered ⇒ nothing satisfiable; every card is uncovered.
    assert body["summary"]["satisfiable"] == 0
    assert body["summary"]["uncovered"] == body["summary"]["total"]
    assert body["summary"]["total"] == len(body["cards"])
    assert body["gaps"], "empty covered set should surface gaps"


def test_coverage_reflects_injected_covered_set(monkeypatch, local_auth_env):
    # Patching covered_concepts to satisfy 'mro-inventory' makes the spare-parts
    # card satisfiable while 'oee' remains uncovered (cheapest path: oee-score).
    # This test asserts the flag-OFF (generic) suggestion text, so pin the affinity
    # flag off — keeps it deterministic regardless of ambient env / deployment.
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "covered_concepts", lambda: frozenset({"mro-inventory"})
    )
    client = TestClient(_build_app())

    resp = client.get("/api/coverage")
    assert resp.status_code == 200
    body = resp.json()

    by_id = {c["card_id"]: c for c in body["cards"]}
    assert by_id["spare-parts-on-hand"]["satisfiable"] is True
    assert by_id["oee"]["satisfiable"] is False
    # The oee GAUGE unions the stored shortcut (oee-score) with its sub-readings
    # (availability / performance / the oee-quality yield → its
    # quality-yield-rate prefer_stored shortcut).
    assert by_id["oee"]["missing_concepts"] == [
        "availability", "oee-score", "performance", "quality-yield-rate"]

    gap_concepts = {g["concept"] for g in body["gaps"]}
    assert "oee-score" in gap_concepts
    oee_gap = next(g for g in body["gaps"] if g["concept"] == "oee-score")
    assert oee_gap["blocks_cards"] == ["oee"]
    assert oee_gap["suggestion"] == "Onboard a system providing oee-score"
