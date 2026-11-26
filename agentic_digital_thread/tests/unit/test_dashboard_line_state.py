# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``GET /api/dashboard/line-state`` (Line State fan-out).

No AWS, no graph, no DB, no network. Mirrors ``test_resolve_by_dimension_route.py``:

  • Direct route-coroutine calls (``asyncio.run``) drive the handler with
    ``catalog.get_onboarded`` and ``metric_dimension.resolve_metric_by_dimension``
    monkeypatched (the route imports the grouped core lazily as ``md`` and calls it
    on that module, so a single ``setattr`` on ``agents.metric_dimension`` drives it).
  • A minimal FastAPI app + ``AuthMiddleware`` + ``TestClient`` proves the route
    inherits the app-wide auth exactly like the other ``/api/dashboard/*`` routes.

The endpoint stays GENERIC: it fans out over every onboarded system, pins authority
to each in turn, and includes ONLY those that actually resolve ``machine-state`` —
no system_type / plant / line literals decide who contributes.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.dashboard as dashboard
import agents.metric_dimension as md
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.metric_resolver import MetricResolverError


# --- Fakes -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_line_state_cache():
    """The route now serves through the short line-state TTL cache (event-loop
    fix) — clear it around every test so canned distributions don't leak."""
    dashboard.invalidate_line_state_cache()
    yield
    dashboard.invalidate_line_state_cache()


def _sys(sid, name, plant, stype):
    return {"id": sid, "name": name, "plant": plant, "system_type": stype,
            "status": "onboarded"}


def _grouped(rows):
    """Minimal grouped result — the route reads only ``.rows[].dimension_value``
    and ``.rows[].value``."""
    return types.SimpleNamespace(
        rows=[types.SimpleNamespace(dimension_value=s, value=c) for s, c in rows])


# Three MES sites (distinct plants; mes-mil has an empty plant → site falls back to
# the system name) plus a CMMS and an ERP. The ERP does NOT map machine-state.
_SYSTEMS = [
    _sys("mes-mil", "Milwaukee MES", "", "MES"),
    _sys("mes-det", "Detroit MES", "Detroit", "MES"),
    _sys("mes-cr", "Cedar Rapids MES", "Cedar Rapids", "MES"),
    _sys("erp", "ERP", "All Sites", "ERP"),
]

# Per-system canned distributions (state, count). One row per line ⇒ count = #lines.
_DIST = {
    "mes-mil": _grouped([("RUNNING", 2), ("IDLE", 1)]),
    "mes-det": _grouped([("IDLE", 3)]),
    "mes-cr": _grouped([("RUNNING", 2), ("IDLE", 1)]),
}


def _install(monkeypatch, systems=_SYSTEMS, dist=None):
    dist = _DIST if dist is None else dist
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(systems))

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        assert metric_id == "machine-state-distribution"
        assert dimension_concept == "machine-state"
        sid = authority.authoritative_system("machine-state")
        if sid not in dist:
            raise MetricResolverError(f"machine-state not mapped in {sid!r}")
        return dist[sid]

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)


# --- Aggregation over all contributing systems ------------------------------

def test_line_state_aggregates_all_mapping_sites(monkeypatch):
    _install(monkeypatch)
    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))

    assert payload["status"] == "ok"
    assert payload["error"] is None
    # Only the 3 MES systems map machine-state; the ERP is skipped generically.
    assert payload["source_system_types"] == ["MES"]
    sites = payload["sites"]
    assert len(sites) == 3
    by_id = {s["system_id"]: s for s in sites}
    assert set(by_id) == {"mes-mil", "mes-det", "mes-cr"}

    # Detroit: plant label kept; states carried through as RAW strings + counts.
    det = by_id["mes-det"]
    assert det["site"] == "Detroit"
    assert det["system_type"] == "MES"
    assert det["states"] == [{"state": "IDLE", "count": 3}]

    # mes-mil has an empty plant → site is "" (frontend falls back to the name).
    assert by_id["mes-mil"]["site"] == ""
    assert by_id["mes-mil"]["name"] == "Milwaukee MES"

    json.dumps(payload)  # no dataclass/enum leftovers


def test_line_state_sites_sorted_by_site_then_name(monkeypatch):
    _install(monkeypatch)
    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    keys = [(s["site"] or s["name"]).lower() for s in payload["sites"]]
    assert keys == sorted(keys)


def test_line_state_badge_includes_every_contributing_type(monkeypatch):
    # A CMMS that ALSO maps machine-state contributes → badge = "CMMS + MES"
    # (sorted distinct types), purely from the data (no type literal in the route).
    systems = _SYSTEMS + [_sys("cmms", "CMMS", "All Sites", "CMMS")]
    dist = dict(_DIST, cmms=_grouped([("DOWN", 1)]))
    _install(monkeypatch, systems=systems, dist=dist)

    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert payload["source_system_types"] == ["CMMS", "MES"]
    assert {s["system_id"] for s in payload["sites"]} == {
        "mes-mil", "mes-det", "mes-cr", "cmms"}


# --- Fail-soft --------------------------------------------------------------

def test_line_state_skips_system_that_raises(monkeypatch):
    # One system's resolve throws a generic (non-boundary) error → that site is
    # omitted and the others are still present (never a 500). The envelope is
    # HONEST about the degradation: an UNEXPECTED per-system failure is not a
    # coverage gap, so the status is 'partial-error' with a message rather than a
    # silent 'ok' — which also keeps the result OUT of the panel-read cache.
    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("machine-state")
        if sid == "mes-det":
            raise RuntimeError("boom")
        if sid not in _DIST:
            raise MetricResolverError("unmapped")
        return _DIST[sid]

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert payload["status"] == "partial-error"
    assert payload["error"]
    assert {s["system_id"] for s in payload["sites"]} == {"mes-mil", "mes-cr"}
    assert not dashboard._cacheable_payload(payload)


def test_line_state_empty_when_no_system_maps(monkeypatch):
    _install(monkeypatch, dist={})  # nothing maps machine-state
    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert payload["status"] == "ok"
    assert payload["sites"] == []
    assert payload["source_system_types"] == []
    assert payload["error"] is None


def test_line_state_skips_system_with_no_rows(monkeypatch):
    dist = dict(_DIST, mes_det=_grouped([]))
    dist["mes-det"] = _grouped([])   # resolves but returns zero rows → skipped
    _install(monkeypatch, dist=dist)
    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert {s["system_id"] for s in payload["sites"]} == {"mes-mil", "mes-cr"}


def test_line_state_pins_authority_to_each_system(monkeypatch):
    seen = []

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("anything")
        seen.append(sid)
        if sid not in _DIST:
            raise MetricResolverError("unmapped")
        return _DIST[sid]

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    asyncio.run(dashboard.dashboard_line_state(plant="all"))
    # Every onboarded system was probed with ITS OWN pinned authority.
    assert seen == ["mes-mil", "mes-det", "mes-cr", "erp"]


# --- A transient source failure is NEVER cached as empty success -------------
# The fan-out is fail-soft per system, so a TOTAL transient failure used to look
# byte-identical to "no system maps machine-state": status 'ok', sites [], error
# None. Admitted to the panel-read cache, that pinned a fabricated empty Line
# State in front of every viewer for a whole TTL window. The two cases are now
# distinguishable and only the honest one is cacheable.

def test_line_state_total_unexpected_failure_is_an_honest_error(monkeypatch):
    def _resolve(*_a, **_k):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    # Nothing resolved AND something broke ⇒ an honest error, not empty success.
    assert payload["status"] == "error"
    assert payload["error"]
    assert payload["sites"] == []
    assert not dashboard._cacheable_payload(payload)


def test_line_state_recovers_immediately_after_a_transient_failure(monkeypatch):
    """The failed read is not cached, so the very NEXT request recomputes and
    serves real data — no waiting out the TTL behind a fabricated empty panel."""
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "9999")
    state = {"fail": True, "calls": 0}

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("transient")
        sid = authority.authoritative_system("machine-state")
        if sid not in _DIST:
            raise MetricResolverError("unmapped")
        return _DIST[sid]

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    first = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert first["status"] == "error"
    calls_after_first = state["calls"]

    state["fail"] = False
    second = asyncio.run(dashboard.dashboard_line_state(plant="all"))

    assert state["calls"] > calls_after_first, "failed read was cached"
    assert second["status"] == "ok"
    assert len(second["sites"]) == 3


def test_line_state_coverage_gap_stays_cacheable_ok(monkeypatch):
    """The EXPECTED skip (a system that does not map the concept) is not a fault:
    an all-unmapped fan-out is still honest empty success AND cacheable, so the
    mid-onboarding empty state does not lose its sharing."""
    _install(monkeypatch, dist={})
    payload = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert payload["status"] == "ok"
    assert payload["error"] is None
    assert dashboard._cacheable_payload(payload)


def test_line_state_partial_success_is_shared_only_when_clean(monkeypatch):
    """Two requests where the first sweep hit an unexpected per-system error:
    the partial answer is served but not shared, so the second request re-probes
    the system that failed."""
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "9999")
    state = {"fail_det": True}

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("machine-state")
        if sid == "mes-det" and state["fail_det"]:
            raise RuntimeError("transient")
        if sid not in _DIST:
            raise MetricResolverError("unmapped")
        return _DIST[sid]

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    first = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert first["status"] == "partial-error"
    assert len(first["sites"]) == 2

    state["fail_det"] = False
    second = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert second["status"] == "ok"
    assert len(second["sites"]) == 3

    # The clean sweep IS shared — a third request reuses it.
    state["fail_det"] = True
    third = asyncio.run(dashboard.dashboard_line_state(plant="all"))
    assert third == second


# --- AuthMiddleware coverage via TestClient ----------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(dashboard.router)
    return app


def test_route_requires_auth(monkeypatch):
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())
    resp = client.get("/api/dashboard/line-state")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_route_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    get_config.cache_clear()
    _install(monkeypatch)
    client = TestClient(_build_app())
    resp = client.get("/api/dashboard/line-state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["source_system_types"] == ["MES"]
    assert len(body["sites"]) == 3
