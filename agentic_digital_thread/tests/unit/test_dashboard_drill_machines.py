# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``GET /api/dashboard/line-state/machines`` (Task 2C —
the Line State drill fan-out) plus the systematic drill-hint spec over the seed
cards (plan item 3).

No AWS, no graph, no DB, no network. Mirrors ``test_dashboard_line_state.py``:

  • Direct route-coroutine calls (``asyncio.run``) drive the handler with
    ``catalog.get_onboarded`` and ``metric_dimension.resolve_metric_by_dimension``
    monkeypatched (the route imports the grouped core lazily as ``md`` and calls
    it on that module, so a single ``setattr`` on ``agents.metric_dimension``
    drives it).
  • A minimal FastAPI app + ``AuthMiddleware`` + ``TestClient`` proves the route
    inherits the app-wide auth exactly like the other ``/api/dashboard/*`` routes.

The endpoint stays GENERIC: it fans out over every onboarded system, pins
authority to each in turn, and contributes per-machine LATEST rows where the
system resolves them (event-time mapped), the per-state distribution where it
does not, and skips systems that map neither — no system_type / plant / line
literals decide who contributes.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.dashboard as dashboard
import agents.dashboard_drill as drill
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.auth.middleware import AuthMiddleware
from agents.card_layer import get_card_layer
from agents.config import get_config
from agents.metric_layer import Aggregation
from agents.metric_resolver import (
    AuthoritySource,
    Lineage,
    MappingProvenance,
    MetricResolverError,
    QueryPlan,
    ResolvedMetric,
)


# --- Fakes -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_line_state_cache():
    """The drill route now serves through the shared line-state TTL cache
    (event-loop fix) — clear it around every test so canned payloads don't leak."""
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
        rows=[types.SimpleNamespace(dimension_value=a, value=b) for a, b in rows])


# Two MES sites plus an ERP that maps neither machine-state view.
_SYSTEMS = [
    _sys("mes-det", "Detroit MES", "Detroit", "MES"),
    _sys("mes-mil", "Milwaukee MES", "", "MES"),
    _sys("erp", "ERP", "All Sites", "ERP"),
]

# mes-det has an event-time mapping → per-machine LATEST rows resolve.
_LATEST = {
    "mes-det": _grouped([("CNC-1", "RUNNING"), ("CNC-2", "DOWN")]),
}

# Both MES systems resolve the per-state distribution.
_DIST = {
    "mes-det": _grouped([("RUNNING", 1), ("DOWN", 1)]),
    "mes-mil": _grouped([("RUNNING", 2), ("IDLE", 1)]),
}


def _install(monkeypatch, systems=_SYSTEMS, latest=None, dist=None):
    latest = dict(_LATEST) if latest is None else latest
    dist = dict(_DIST) if dist is None else dist
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(systems))

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("machine-state")
        if metric_id == "machine-state-latest":
            assert dimension_concept == "asset"
            if sid not in latest:
                raise MetricResolverError(f"no event-time mapping in {sid!r}")
            return latest[sid]
        if metric_id == "machine-state-distribution":
            assert dimension_concept == "machine-state"
            if sid not in dist:
                raise MetricResolverError(f"machine-state not mapped in {sid!r}")
            return dist[sid]
        raise KeyError(metric_id)

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)


def _run(plant="all"):
    return asyncio.run(drill.dashboard_line_state_machines(plant=plant))


# --- Mixed envelope: machines where event-time is mapped, distribution else ---

def test_mixed_envelope_machines_and_distribution(monkeypatch):
    _install(monkeypatch)
    payload = _run()

    assert set(payload.keys()) == {"status", "systems", "error"}
    assert payload["status"] == "ok"
    assert payload["error"] is None

    systems = payload["systems"]
    # ERP maps neither view → skipped generically (no system_type test).
    # Unified site sort rule (fix-pass): (site, name) with the empty/unsited
    # site FIRST — mes-mil (site "") now leads mes-det (site "Detroit"),
    # matching the multi-site card breakdown order.
    assert [s["system_id"] for s in systems] == ["mes-mil", "mes-det"]
    for s in systems:
        assert set(s.keys()) == {"system_id", "name", "site", "kind", "rows"}

    det = next(s for s in systems if s["system_id"] == "mes-det")
    assert det["kind"] == "machines"
    assert det["site"] == "Detroit"
    assert det["name"] == "Detroit MES"
    assert det["rows"] == [["CNC-1", "RUNNING"], ["CNC-2", "DOWN"]]

    mil = next(s for s in systems if s["system_id"] == "mes-mil")
    assert mil["kind"] == "distribution"
    assert mil["site"] == ""          # empty plant → no site (frontend uses name)
    assert mil["rows"] == [["RUNNING", 2], ["IDLE", 1]]

    json.dumps(payload)  # no dataclass/enum leftovers


def test_systems_sorted_by_site_then_name(monkeypatch):
    _install(monkeypatch)
    payload = _run()
    # THE unified rule (metric_multisite.site_sort_key): (site, name)
    # case-insensitive, empty site FIRST.
    keys = [((s["site"] or "").lower(), (s["name"] or "").lower())
            for s in payload["systems"]]
    assert keys == sorted(keys)


def test_wildcard_plant_yields_empty_site(monkeypatch):
    # 'All Sites' is not the wildcard; only 'all' (case-insensitive) / empty are.
    systems = [_sys("mes-x", "X MES", "ALL", "MES")]
    _install(monkeypatch, systems=systems,
             latest={}, dist={"mes-x": _grouped([("RUNNING", 1)])})
    payload = _run()
    assert payload["systems"][0]["site"] == ""


# --- Fallback semantics -------------------------------------------------------

def test_empty_latest_rows_fall_back_to_distribution(monkeypatch):
    # LATEST resolves but yields zero rows → the drill must never be emptier
    # than the card: that system contributes its distribution instead.
    _install(monkeypatch, latest={"mes-det": _grouped([])})
    payload = _run()
    by_id = {s["system_id"]: s for s in payload["systems"]}
    assert by_id["mes-det"]["kind"] == "distribution"
    assert by_id["mes-det"]["rows"] == [["RUNNING", 1], ["DOWN", 1]]


def test_generic_error_in_latest_falls_back_to_distribution(monkeypatch):
    # An unexpected (non-boundary) error on the LATEST path is contained per
    # system: log + fall back, never a 500.
    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("machine-state")
        if metric_id == "machine-state-latest":
            raise RuntimeError("boom")
        if sid in _DIST:
            return _DIST[sid]
        raise MetricResolverError("unmapped")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    payload = _run()
    assert payload["status"] == "partial-error"
    assert payload["error"]
    assert {s["system_id"]: s["kind"] for s in payload["systems"]} == {
        "mes-det": "distribution", "mes-mil": "distribution"}
    assert not dashboard._cacheable_payload(payload)


def test_generic_error_in_distribution_skips_system(monkeypatch):
    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        sid = authority.authoritative_system("machine-state")
        if metric_id == "machine-state-latest":
            if sid in _LATEST:
                return _LATEST[sid]
            raise MetricResolverError("no event-time")
        if sid == "mes-mil":
            raise RuntimeError("boom")
        raise MetricResolverError("unmapped")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    payload = _run()
    assert payload["status"] == "partial-error"
    assert payload["error"]
    assert [s["system_id"] for s in payload["systems"]] == ["mes-det"]
    assert not dashboard._cacheable_payload(payload)


def test_drill_total_unexpected_failure_is_an_honest_error(monkeypatch):
    """Same rule as the Line State card: nothing resolved AND something broke ⇒
    an honest error the cache refuses, never empty success."""
    def _resolve(*_a, **_k):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    payload = _run()
    assert payload["status"] == "error"
    assert payload["error"]
    assert payload["systems"] == []
    assert not dashboard._cacheable_payload(payload)


def test_drill_recovers_immediately_after_a_transient_failure(monkeypatch):
    monkeypatch.setenv("DASHBOARD_LINE_STATE_TTL", "9999")
    state = {"fail": True, "calls": 0}

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("transient")
        sid = authority.authoritative_system("machine-state")
        if metric_id == "machine-state-latest":
            if sid in _LATEST:
                return _LATEST[sid]
            raise MetricResolverError("no event-time")
        if sid in _DIST:
            return _DIST[sid]
        raise MetricResolverError("unmapped")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    assert _run()["status"] == "error"
    calls_after_first = state["calls"]
    state["fail"] = False

    second = _run()
    assert state["calls"] > calls_after_first, "failed drill was cached"
    assert second["status"] == "ok"
    assert [s["system_id"] for s in second["systems"]] == ["mes-mil", "mes-det"]


def test_empty_when_no_system_maps_either_view(monkeypatch):
    _install(monkeypatch, latest={}, dist={})
    payload = _run()
    assert payload == {"status": "ok", "systems": [], "error": None}


def test_pins_authority_to_each_system(monkeypatch):
    seen = []

    def _resolve(metric_id, dimension_concept, *, authority=None, **_k):
        seen.append((metric_id, authority.authoritative_system("anything")))
        raise MetricResolverError("unmapped")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _resolve)

    _run()
    # Every onboarded system was probed with ITS OWN pinned authority — LATEST
    # first, then the distribution fallback.
    assert seen == [
        ("machine-state-latest", "mes-det"),
        ("machine-state-distribution", "mes-det"),
        ("machine-state-latest", "mes-mil"),
        ("machine-state-distribution", "mes-mil"),
        ("machine-state-latest", "erp"),
        ("machine-state-distribution", "erp"),
    ]


def test_plant_filter_narrows_the_fanout(monkeypatch):
    _install(monkeypatch)
    payload = _run(plant="Detroit")
    ids = [s["system_id"] for s in payload["systems"]]
    # Only Detroit + wildcard-plant systems match; mes-mil ('' → wildcard) stays.
    assert "mes-det" in ids


# --- AuthMiddleware coverage via TestClient ----------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(drill.router)
    return app


def test_route_requires_auth(monkeypatch):
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())
    resp = client.get("/api/dashboard/line-state/machines")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_route_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    get_config.cache_clear()
    _install(monkeypatch)
    client = TestClient(_build_app())
    resp = client.get("/api/dashboard/line-state/machines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Unified sort: unsited mes-mil (distribution) leads sited mes-det (machines).
    assert [s["kind"] for s in body["systems"]] == ["distribution", "machines"]


# --- Systematic drill coverage (plan item 3): every scalar seed card with a ---
# --- physical query_plan emits a drill hint under a mocked resolver         ---

def _canned_for(metric_id: str) -> ResolvedMetric:
    """A generically-canned ResolvedMetric carrying a PHYSICAL query_plan (the
    drill-hint precondition) — table derived from the metric id so each card's
    hint is distinguishable."""
    plan = QueryPlan(
        system_id="sys-a",
        schema="s",
        table="t_" + metric_id,
        column="c",
        aggregation=Aggregation.SUM,
    )
    lineage = Lineage(
        metric_id=metric_id,
        kind="direct",
        concept="concept-x",
        authority=AuthoritySource(system_id="sys-a", source="single_system_map"),
        mapping=MappingProvenance(
            table=plan.table, column="c", role="measure",
            confidence=0.9, source="agent_proposed",
        ),
        query_plan=plan,
        sql=f'SELECT SUM("c") FROM "s"."{plan.table}"',  # nosec B608 - test fixture composing expected SQL from module constants
        executed_at="2026-07-15T00:00:00+00:00",
    )
    return ResolvedMetric(
        metric_id=metric_id, value=1, unit="u", label=metric_id,
        query_plan=plan, system_id="sys-a", lineage=lineage,
    )


def _scalar_seed_card_ids() -> list[str]:
    return [c.id for c in get_card_layer().list_cards() if c.group_by is None]


@pytest.mark.parametrize("card_id", _scalar_seed_card_ids())
def test_every_scalar_seed_card_emits_drill_hint_under_physical_plans(
    monkeypatch, card_id,
):
    """Spec: for EVERY scalar seed card, when its metrics resolve with a
    physical ``query_plan`` (mocked resolver), the card payload carries the
    ``drill`` hint {system_id, table, entity_label} — the modal's
    click-to-drill contract. (By-dimension cards are declarative descriptors
    resolved client-side and carry no scalar drill hint by design.)"""
    monkeypatch.setattr(mr, "resolve_metric", _canned_for)
    monkeypatch.setattr(dashboard.catalog, "get_all", lambda: [])
    card = get_card_layer().get_card(card_id)
    payload = dashboard._build_card(card_id)
    assert "drill" in payload, f"card {card_id!r} emitted no drill hint"
    drill_hint = payload["drill"]
    # The hint is stamped by the FIRST ok metric backed by a physical plan.
    first_ref = card.metric_refs[0]
    assert drill_hint == {
        "system_id": "sys-a",
        "table": "t_" + first_ref,
        "entity_label": card.title,
    }
    json.dumps(payload)


def test_by_dimension_seed_cards_emit_no_drill_hint(monkeypatch):
    """Counter-spec: by-dimension (group_by) cards are descriptors — no scalar
    resolve, no drill hint (their drill affordance is the panel/endpoint path)."""
    monkeypatch.setattr(mr, "resolve_metric", _canned_for)
    monkeypatch.setattr(dashboard.catalog, "get_all", lambda: [])
    dim_cards = [c for c in get_card_layer().list_cards() if c.group_by is not None]
    assert dim_cards, "seed registry should contain by-dimension cards"
    for card in dim_cards:
        payload = dashboard._build_card(card.id)
        assert "drill" not in payload
        assert payload["resolver"] == "by-dimension"
