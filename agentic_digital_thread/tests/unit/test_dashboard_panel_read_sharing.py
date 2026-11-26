# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests: the four remaining PER-VIEWER dashboard panel endpoints
share ONE compute per key per window.

Before this, ``/api/dashboard`` cards and the line-state sweep were the only
shared reads. Sections, active-issues and the two grouped resolves recomputed
for EVERY request, so one browser wave (or several viewers) multiplied the same
backend scans — a measured contributor to the 2026-08-24 single-task CPU
saturation. Each route now goes through the ONE bounded
``dashboard.dashboard_read_cached`` helper.

The pins here are behavioral (route in → compute count out), not structural:
  • a repeat request with the SAME inputs computes once;
  • every request discriminator the API actually accepts partitions the cache;
  • an ``status='error'`` envelope is served, NOT cached, and recovers;
  • the two production invalidation paths that already dropped the CARD cache
    now drop the panel-read cache with it.

No AWS, no graph, no DB, no LLM.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agents.dashboard as dashboard
import agents.heatmap_resolver as hm
import agents.metric_dimension as md
import agents.metric_resolver as mr
import agents.onboard as onboard
from agents.models import ResolveHeatmapRequest, ResolveMetricByDimensionRequest


@pytest.fixture(autouse=True)
def _long_ttl(monkeypatch):
    """A long window so the assertions are about SHARING, never about timing."""
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "9999")
    dashboard.invalidate_dashboard_read_cache()
    yield
    dashboard.invalidate_dashboard_read_cache()


def _counting(payload):
    calls = {"n": 0}

    def compute(*_a, **_k):
        calls["n"] += 1
        return payload() if callable(payload) else payload

    return calls, compute


# --- GET /api/dashboard/sections --------------------------------------------

def test_sections_route_shares_one_render_per_plant(monkeypatch):
    calls, render = _counting(lambda: [{"entity": "work_order", "status": "ok"}])
    monkeypatch.setattr(dashboard.entity_render, "render_all_sections", render)

    first = asyncio.run(dashboard.dashboard_sections(plant="all"))
    second = asyncio.run(dashboard.dashboard_sections(plant="all"))

    assert first == second
    assert calls["n"] == 1
    # A DIFFERENT plant filter is a different answer — it must recompute.
    asyncio.run(dashboard.dashboard_sections(plant="plant-b"))
    assert calls["n"] == 2


def test_targeted_sections_partition_cache_on_plant_system_and_table(monkeypatch):
    systems = [
        {"id": "sys-a", "plant": "plant-a"},
        {"id": "sys-c", "plant": "plant-a"},
        {"id": "sys-a", "plant": "plant-b"},
    ]
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: systems)
    calls, render = _counting(lambda: [{"status": "ok"}])
    monkeypatch.setattr(dashboard.entity_render, "_render_system_sections_safe", render)

    asyncio.run(dashboard.dashboard_sections("plant-a", "sys-a", "orders"))
    asyncio.run(dashboard.dashboard_sections("plant-a", "sys-a", "orders"))
    asyncio.run(dashboard.dashboard_sections("plant-a", "sys-a", "assets"))
    asyncio.run(dashboard.dashboard_sections("plant-a", "sys-c", "orders"))
    asyncio.run(dashboard.dashboard_sections("plant-b", "sys-a", "orders"))

    assert calls["n"] == 4


def test_targeted_sections_unknown_or_mismatched_target_queries_nothing(monkeypatch):
    systems = [{"id": "sys-a", "plant": "plant-a"}]
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: systems)
    render = Mock(side_effect=AssertionError("must not query another system"))
    monkeypatch.setattr(dashboard.entity_render, "_render_system_sections_safe", render)

    assert asyncio.run(
        dashboard.dashboard_sections("plant-a", "missing", "orders")) == {"sections": []}
    assert asyncio.run(
        dashboard.dashboard_sections("plant-b", "sys-a", "orders")) == {"sections": []}
    assert asyncio.run(
        dashboard.dashboard_sections("plant-a", "sys-a", None)) == {"sections": []}
    render.assert_not_called()


def test_targeted_sections_use_safe_graph_boundary_and_never_query_sibling(monkeypatch):
    target = {"id": "sys-a", "plant": "plant-a"}
    sibling = {"id": "sys-b", "plant": "plant-a"}
    monkeypatch.setattr(
        dashboard.catalog, "get_onboarded", lambda: [target, sibling],
    )
    safe = Mock(return_value=[])
    unsafe = Mock(side_effect=AssertionError("unguarded graph read escaped"))
    monkeypatch.setattr(dashboard.entity_render, "_render_system_sections_safe", safe)
    monkeypatch.setattr(dashboard.entity_render, "render_system_sections", unsafe)

    result = asyncio.run(
        dashboard.dashboard_sections("plant-a", "sys-a", "orders"))

    assert result == {"sections": []}
    safe.assert_called_once_with(target, table="orders")
    unsafe.assert_not_called()


def test_sections_route_does_not_cache_an_inner_section_error(monkeypatch):
    states = iter([[{"status": "error"}], [{"status": "ok"}]])
    calls, render = _counting(lambda: next(states))
    monkeypatch.setattr(dashboard.entity_render, "render_all_sections", render)

    assert asyncio.run(dashboard.dashboard_sections())["sections"][0]["status"] == "error"
    assert asyncio.run(dashboard.dashboard_sections())["sections"][0]["status"] == "ok"
    assert calls["n"] == 2


def test_targeted_graph_exception_returns_empty_and_queries_no_sibling(monkeypatch, caplog):
    target = {"id": "sys-a", "plant": "plant-a"}
    sibling = {"id": "sys-b", "plant": "plant-a"}
    monkeypatch.setattr(
        dashboard.catalog, "get_onboarded", lambda: [target, sibling],
    )
    calls = []

    def graph_failure(system, *, table=None, **_kwargs):
        calls.append((system["id"], table))
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(dashboard.entity_render, "render_system_sections", graph_failure)
    with caplog.at_level("WARNING", logger="agents.entity_render"):
        result = asyncio.run(
            dashboard.dashboard_sections("plant-a", "sys-a", "orders"))

    assert result == {"sections": []}
    assert calls == [("sys-a", "orders")]
    assert len([record for record in caplog.records
                if "system render failed" in record.getMessage()]) == 1


def test_targeted_section_error_is_not_cached_and_recovers(monkeypatch):
    monkeypatch.setattr(
        dashboard.catalog, "get_onboarded",
        lambda: [{"id": "sys-a", "plant": "plant-a"}],
    )
    states = iter([[{"status": "error"}], [{"status": "ok"}]])
    calls, render = _counting(lambda: next(states))
    monkeypatch.setattr(dashboard.entity_render, "_render_system_sections_safe", render)

    first = asyncio.run(dashboard.dashboard_sections("plant-a", "sys-a", "orders"))
    second = asyncio.run(dashboard.dashboard_sections("plant-a", "sys-a", "orders"))

    assert first["sections"][0]["status"] == "error"
    assert second["sections"][0]["status"] == "ok"
    assert calls["n"] == 2


# --- GET /api/dashboard/active-issues ---------------------------------------

def test_active_issues_route_shares_one_composition(monkeypatch):
    canned = [{"severity": "critical", "title": "t"}]
    calls, compose = _counting(canned)
    monkeypatch.setattr(dashboard.active_issues, "compose_active_issues", compose)

    first = asyncio.run(dashboard.dashboard_active_issues(plant="all"))
    second = asyncio.run(dashboard.dashboard_active_issues(plant="all"))

    assert first == second == {"kind": "active-issues", "issues": canned, "status": "ok"}
    assert calls["n"] == 1


def test_active_issues_error_envelope_is_not_cached_and_recovers(monkeypatch):
    calls = {"n": 0}

    def compose(_systems):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB failure")
        return [{"severity": "warn", "title": "recovered"}]

    monkeypatch.setattr(dashboard.active_issues, "compose_active_issues", compose)

    assert asyncio.run(dashboard.dashboard_active_issues())["status"] == "error"
    recovered = asyncio.run(dashboard.dashboard_active_issues())
    assert recovered["status"] == "ok"
    assert recovered["issues"][0]["title"] == "recovered"
    # And the recovered answer IS then shared.
    asyncio.run(dashboard.dashboard_active_issues())
    assert calls["n"] == 2


# --- POST /api/metrics/resolve-by-dimension ---------------------------------

def _bar(rows=()):
    return {"kind": "bar", "status": "ok", "rows": list(rows), "columns": [],
            "lineage": []}


def _by_dim(**kwargs) -> ResolveMetricByDimensionRequest:
    return ResolveMetricByDimensionRequest(
        **{"metric_id": "quality-rate", "dimension_concept": "production-line",
           **kwargs})


def test_by_dimension_route_shares_identical_bodies(monkeypatch):
    calls, resolve = _counting(_bar)
    monkeypatch.setattr(mr, "_resolve_by_dimension_payload", resolve)

    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim()))
    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim()))

    assert calls["n"] == 1


@pytest.mark.parametrize("changed", [
    {"metric_id": "work-order-count"},
    {"dimension_concept": "machine-state"},
    {"system_id": "mes-mil"},
])
def test_by_dimension_route_partitions_on_every_request_field(monkeypatch, changed):
    calls, resolve = _counting(_bar)
    monkeypatch.setattr(mr, "_resolve_by_dimension_payload", resolve)

    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim()))
    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim(**changed)))

    assert calls["n"] == 2


def test_by_dimension_route_distinguishes_absent_pin_from_empty_pin(monkeypatch):
    """``system_id=None`` (no pin) and ``system_id=''`` are different requests as
    far as the cache is concerned — the key carries the raw optional value, so a
    pinned/unpinned answer can never be served for the other."""
    calls, resolve = _counting(_bar)
    monkeypatch.setattr(mr, "_resolve_by_dimension_payload", resolve)

    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim(system_id=None)))
    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim(system_id="")))

    assert calls["n"] == 2


def test_by_dimension_route_does_not_cache_error_envelope(monkeypatch):
    def _raise(*_a, **_k):
        raise mr.MetricResolverError("join composition failed")

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _raise)
    assert asyncio.run(
        mr.resolve_metric_by_dimension_route(_by_dim()))["status"] == "error"

    monkeypatch.setattr(md, "resolve_metric_by_dimension",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            mr.CoverageGapError("gap", concept="production-line")))
    assert asyncio.run(
        mr.resolve_metric_by_dimension_route(_by_dim()))["status"] == "uncovered"


def test_by_dimension_route_caches_the_uncovered_state(monkeypatch):
    """``uncovered`` is an honest, stable answer (the mapping gap is not a
    fault) and is just as expensive to recompute — so it IS shared."""
    calls = {"n": 0}

    def _raise(*_a, **_k):
        calls["n"] += 1
        raise mr.CoverageGapError("no MAPS_TO", concept="production-line")

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _raise)
    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim()))
    asyncio.run(mr.resolve_metric_by_dimension_route(_by_dim()))

    assert calls["n"] == 1


# --- POST /api/metrics/resolve-heatmap --------------------------------------

def _heatmap(**kwargs) -> ResolveHeatmapRequest:
    return ResolveHeatmapRequest(
        **{"metric_id": "sensor-reading-latest", "dimension_concept_x": "asset",
           "dimension_concept_y": "production-line",
           "threshold_concept": "specification", **kwargs})


def test_heatmap_route_shares_identical_bodies(monkeypatch):
    calls, resolve = _counting({"kind": "heatmap", "status": "ok", "rows": []})
    monkeypatch.setattr(hm, "resolve_heatmap", resolve)

    asyncio.run(mr.resolve_heatmap_route(_heatmap()))
    asyncio.run(mr.resolve_heatmap_route(_heatmap()))

    assert calls["n"] == 1


@pytest.mark.parametrize("changed", [
    {"metric_id": "other-metric"},
    {"dimension_concept_x": "production-line"},
    {"dimension_concept_y": "asset"},
    {"threshold_concept": "other-spec"},
    {"threshold_concept": None},
])
def test_heatmap_route_partitions_on_every_request_field(monkeypatch, changed):
    calls, resolve = _counting({"kind": "heatmap", "status": "ok", "rows": []})
    monkeypatch.setattr(hm, "resolve_heatmap", resolve)

    asyncio.run(mr.resolve_heatmap_route(_heatmap()))
    asyncio.run(mr.resolve_heatmap_route(_heatmap(**changed)))

    assert calls["n"] == 2


def test_heatmap_route_does_not_cache_error_envelope(monkeypatch):
    calls = {"n": 0}

    def resolve(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise mr.MetricResolverError("threshold join failed")
        return {"kind": "heatmap", "status": "no-data", "rows": []}

    monkeypatch.setattr(hm, "resolve_heatmap", resolve)

    assert asyncio.run(mr.resolve_heatmap_route(_heatmap()))["status"] == "error"
    assert asyncio.run(mr.resolve_heatmap_route(_heatmap()))["status"] == "no-data"
    assert calls["n"] == 2


# --- invalidation: the SAME two production paths that drop the card cache ----

def _warm_read_cache() -> None:
    dashboard.dashboard_read_cached(("sections", "all"), lambda: {"sections": []})


def test_correct_mapping_route_invalidates_the_panel_read_cache(monkeypatch):
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    monkeypatch.setattr(
        mr, "record_user_correction",
        Mock(return_value={"status": "accepted", "version": 2, "neptune_node_id": "x"}))
    monkeypatch.setattr(mr.rederive, "affected_cards", lambda concept: ["oee"])
    _warm_read_cache()
    assert dashboard._READ_CACHE

    steward = SimpleNamespace(state=SimpleNamespace(is_admin=True))
    result = asyncio.run(mr.correct_mapping_route(mr.CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand"), steward))

    assert result["status"] == "accepted"
    assert dashboard._READ_CACHE == {}


def test_provision_result_invalidates_the_panel_read_cache(monkeypatch):
    """Onboarding changed what the panels would resolve to — the provision
    result drops BOTH dashboard caches, exactly as it already dropped cards."""
    monkeypatch.setattr(onboard, "_dp_written_artifacts", lambda sid: ([], []))
    monkeypatch.setattr(onboard, "_dp_reapply_self_learned", lambda sid: 0)
    _warm_read_cache()
    assert dashboard._READ_CACHE

    result = onboard._dp_result(_provision_state())

    assert result["status"] == "provisioned"
    assert dashboard._READ_CACHE == {}


def _provision_state():
    """Minimal cross-phase carrier — ``_dp_result`` reads only the counters and
    the two ids, and both artifact/re-apply helpers are stubbed by the caller."""
    return onboard._ProvisionState(
        body=None, system_id="sys-a", system_name="System A", plant="plant-a",
        schema="schema_a", rules=[], mappings=[], connection_type="postgres",
        cm_items=[], tool_name="adt_sys_a", mcp_url="https://example.test/mcp")
