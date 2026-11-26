# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline auth/route tests for the S4.3 affinity gating (ADR 0007).

Covers the ``RESOLVER_AFFINITY``-gated suggester selection on ``GET /api/coverage``
(flag-off byte-identical, flag-on enriched) and the optional read-only
``GET /api/affinity/{system_id}`` endpoint (auth + flag-off disabled + flag-on shape).

Mirrors ``test_coverage_route.py``: FastAPI + ``AuthMiddleware`` + ``TestClient`` with
the documented complete local-auth fixture, the ``get_config.cache_clear()`` step, and
the exact ``os.environ.get("RESOLVER_AFFINITY", "").strip()`` flag-read form. No AWS,
no graph, no network: the graph-read seams are patched to injected ``frozenset``s.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import coverage
from agents.auth.middleware import AuthMiddleware
from agents.catalog import CONCEPT_AFFINITY
from agents.config import get_config
from agents.coverage import router as coverage_router


@pytest.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030) and the demo-seed machinery
    has been DELETED from production (ADR-0035). This route reads a demo system's
    ``system_type`` BY NAME, so temporarily seed the shared catalog from the
    test-local ``DEMO_SYSTEMS`` fixture, then restore the empty catalog."""
    from agents.catalog import catalog as _cat
    try:
        from unit._demo_catalog import DEMO_SYSTEMS
    except ImportError:  # pragma: no cover - alternate invocation path
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from _demo_catalog import DEMO_SYSTEMS
    _cat._systems = [dict(s) for s in DEMO_SYSTEMS]
    try:
        yield
    finally:
        _cat._systems = []


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(coverage_router)
    return app


# --------------------------------------------------------------------------
# GET /api/coverage — RESOLVER_AFFINITY gating (flag-off byte-identical / flag-on)
# --------------------------------------------------------------------------

def test_coverage_flag_off_byte_identical_suggestions(monkeypatch, local_auth_env):
    # Flag unset ⇒ generic S4.2 suggestion string (byte-identical to baseline).
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "covered_concepts", lambda: frozenset({"mro-inventory"})
    )
    client = TestClient(_build_app())

    resp = client.get("/api/coverage")
    assert resp.status_code == 200
    body = resp.json()
    oee_gap = next(g for g in body["gaps"] if g["concept"] == "oee-score")
    assert oee_gap["suggestion"] == "Onboard a system providing oee-score"
    # Gap dict shape unchanged.
    for gap in body["gaps"]:
        assert set(gap.keys()) == {"concept", "blocks_cards", "suggestion"}


def test_coverage_flag_on_enriched_suggestions(monkeypatch, local_auth_env):
    # Flag set ⇒ affinity-enriched suggestion; gap keys/type unchanged.
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "covered_concepts", lambda: frozenset({"mro-inventory"})
    )
    client = TestClient(_build_app())

    resp = client.get("/api/coverage")
    assert resp.status_code == 200
    body = resp.json()
    oee_gap = next(g for g in body["gaps"] if g["concept"] == "oee-score")
    assert oee_gap["suggestion"] == "Onboard a Historian or MES — typically provides oee-score"
    assert set(oee_gap.keys()) == {"concept", "blocks_cards", "suggestion"}
    assert isinstance(oee_gap["suggestion"], str)


def test_coverage_flag_blank_is_off(monkeypatch, local_auth_env):
    # Whitespace-only flag ⇒ .strip() falsy ⇒ generic (byte-identical) path.
    monkeypatch.setenv("RESOLVER_AFFINITY", "  ")
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "covered_concepts", lambda: frozenset({"mro-inventory"})
    )
    client = TestClient(_build_app())

    body = client.get("/api/coverage").json()
    oee_gap = next(g for g in body["gaps"] if g["concept"] == "oee-score")
    assert oee_gap["suggestion"] == "Onboard a system providing oee-score"


# --------------------------------------------------------------------------
# GET /api/affinity/{system_id} — auth + flag gating + shape
# --------------------------------------------------------------------------

def test_affinity_requires_auth(monkeypatch):
    # No Cognito + DEV_NO_AUTH unset ⇒ session-authed /api/ route rejected 401.
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "system_covered_concepts", lambda _sid: frozenset()
    )
    client = TestClient(_build_app())

    resp = client.get("/api/affinity/cmms")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_affinity_flag_off_disabled(monkeypatch, local_auth_env):
    # Authed but flag off ⇒ {"enabled": False}; no graph read attempted.
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.get("/api/affinity/cmms")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_affinity_flag_on_shape_and_missing(monkeypatch, local_auth_env):
    # Authed + flag on + injected scoped covered set → full shape; missing = expected − mapped.
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "system_covered_concepts", lambda _sid: frozenset({"equipment-id"})
    )
    client = TestClient(_build_app())

    resp = client.get("/api/affinity/cmms")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "enabled", "system_id", "system_type", "expected", "mapped", "missing"
    }
    assert body["enabled"] is True
    assert body["system_id"] == "cmms"
    assert body["system_type"] == "CMMS"
    assert body["expected"] == sorted(CONCEPT_AFFINITY["CMMS"])
    assert body["mapped"] == ["equipment-id"]
    assert body["missing"] == sorted(CONCEPT_AFFINITY["CMMS"] - {"equipment-id"})


def test_affinity_flag_on_unknown_system(monkeypatch, local_auth_env):
    # Unknown system_id ⇒ no catalog entry ⇒ system_type None ⇒ empty lists.
    monkeypatch.setenv("RESOLVER_AFFINITY", "1")
    get_config.cache_clear()
    monkeypatch.setattr(
        coverage, "system_covered_concepts", lambda _sid: frozenset()
    )
    client = TestClient(_build_app())

    body = client.get("/api/affinity/does-not-exist").json()
    assert body["enabled"] is True
    assert body["system_type"] is None
    assert body["expected"] == []
    assert body["missing"] == []
    assert body["mapped"] == []
