# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``POST /api/metrics/resolve-by-dimension`` (W2 backend).

No AWS, no graph, no DB, no network. Mirrors ``test_correction_route.py``:

  • Direct route-coroutine calls (``asyncio.run``) drive the handler with
    ``metric_dimension.resolve_metric_by_dimension`` monkeypatched (the route
    imports the grouped core lazily as ``md`` and calls it on that module, so a
    single ``setattr`` on ``agents.metric_dimension`` drives the path). The real
    ``_row_lineage_to_dict`` serializer renders the per-row lineage.
  • A minimal FastAPI app + ``AuthMiddleware`` + ``TestClient`` proves the route
    inherits the app-wide auth exactly like the other ``/api/metrics/*`` routes.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.metric_layer import Aggregation
from agents.metric_resolver import (
    AuthoritySource,
    GroupedQueryPlan,
    GroupedResolvedMetric,
    GroupedRow,
    JoinPathProvenance,
    MappingProvenance,
    MetricResolverError,
    RowLineage,
    resolve_metric_by_dimension_route,
)
from agents.models import ResolveMetricByDimensionRequest


# --- Canned grouped result (the joined quality-by-line shape) ----------------

def _plan() -> GroupedQueryPlan:
    return GroupedQueryPlan(
        system_id="mes-mil", schema="mes_milwaukee",
        measure_table="quality_checks", measure_column="pass_rate",
        aggregation=Aggregation.AVG,
        dimension_table="work_orders", dimension_column="line",
        join=JoinPathProvenance(
            join_id="jp-internal-mes-mil-quality_checks-work_order_id-work_orders",
            from_table="quality_checks", from_field="work_order_id",
            to_table="work_orders", to_field="work_order_id",
            transform="direct", method="foreign_key_match",
        ),
    )


def _row(plan, dim_value, value) -> GroupedRow:
    lineage = RowLineage(
        metric_id="quality-rate", concept="quality-metric",
        dimension_concept="production-line",
        authority=AuthoritySource(system_id="mes-mil", source="authority_rule"),
        measure_mapping=MappingProvenance(
            table="quality_checks", column="pass_rate", role="measure",
            confidence=0.95, source="agent_proposed"),
        dimension_mapping=MappingProvenance(
            table="work_orders", column="line", role="dimension",
            confidence=0.9, source="user"),
        join_path=plan.join, query_plan=plan,
        sql='SELECT d."line", AVG(m."pass_rate") FROM ...',
        executed_at="2026-06-17T00:00:00+00:00", dimension_value=dim_value,
    )
    return GroupedRow(dimension_value=dim_value, value=value, lineage=lineage)


def _grouped() -> GroupedResolvedMetric:
    plan = _plan()
    rows = (_row(plan, "Line1", 0.95), _row(plan, "Line2", 0.91),
            _row(plan, "Line3", 0.88))
    return GroupedResolvedMetric(
        metric_id="quality-rate", unit="ratio", label="Quality Rate",
        dimension_concept="production-line", system_id="mes-mil",
        query_plan=plan, rows=rows,
    )


_BODY = ResolveMetricByDimensionRequest(
    metric_id="quality-rate", dimension_concept="production-line")


# --- Success: row-shaped bar payload ----------------------------------------

def test_resolve_by_dimension_success_row_shape(monkeypatch):
    monkeypatch.setattr(md, "resolve_metric_by_dimension",
                        lambda mid, dim, **_k: _grouped())

    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "ok"
    assert result["kind"] == "bar"
    assert result["group_by"] == "production-line"
    assert result["metric_id"] == "quality-rate"
    assert result["unit"] == "ratio"
    assert result["label"] == "Quality Rate"
    assert result["system_id"] == "mes-mil"

    # Two columns: dimension (CONCEPT_NAME label) then measure (metric label).
    assert result["columns"] == [
        {"label": "Production Line", "field_concept": "production-line"},
        {"label": "Quality Rate", "field_concept": "quality-metric"},
    ]
    # Ordered [dimension_value, value] rows.
    assert result["rows"] == [["Line1", 0.95], ["Line2", 0.91], ["Line3", 0.88]]

    # Per-row lineage, one dict per row, with the embedded aggregation enum
    # rendered as its ``.value`` (no enum/dataclass leftovers → JSON round-trips).
    assert len(result["lineage"]) == 3
    lin0 = result["lineage"][0]
    assert lin0["dimension_value"] == "Line1"
    assert lin0["query_plan"]["aggregation"] == "avg"
    assert lin0["join_path"]["from_table"] == "quality_checks"
    json.dumps(result)


def test_resolve_by_dimension_passes_request_args(monkeypatch):
    seen = {}

    def _capture(mid, dim, **_k):
        seen["mid"], seen["dim"] = mid, dim
        return _grouped()

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _capture)
    asyncio.run(resolve_metric_by_dimension_route(_BODY))
    assert seen == {"mid": "quality-rate", "dim": "production-line"}


# --- Error paths: friendly row-shaped error dict (HTTP 200) ------------------

@pytest.mark.parametrize("exc", [
    MetricResolverError("no JoinPath connecting tables"),
    NotImplementedError("'latest' aggregation deferred"),
    KeyError("unknown metric id"),
])
def test_resolve_by_dimension_error_returns_friendly_dict(monkeypatch, exc):
    def _boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _boom)
    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "error"
    assert result["kind"] == "bar"
    assert result["group_by"] == "production-line"
    assert result["rows"] == []
    assert result["columns"] == []
    assert result["lineage"] == []
    assert result["error"]  # short message present
    json.dumps(result)


# --- ADR-0094 for the grouped route: a per-site authority answers per site --
#
# Event 26 (2026-10-04, four-plant account): "Quality by line" and the second
# grouped panel rendered "unavailable ... ambiguous authority for concept
# 'quality-yield-rate': 4 active AuthorityRules name different systems" with a
# Retry button, although every plant's MES answered three lines when pinned.
# The grouped route now does what the scalar cards and the chat notice already
# do: when the ambiguity is a per-site partition it resolves each site pinned
# and returns one bar per (site, line).

_PLANTS = {"mes-east": "East Plant", "mes-north": "North Plant",
           "mes-south": "South Plant", "mes-west": "West Plant"}


def _sited_catalog():
    return [{"id": sid, "name": f"{plant} MES", "plant": plant}
            for sid, plant in _PLANTS.items()]


def _grouped_for(sid: str) -> GroupedResolvedMetric:
    g = _grouped()
    rows = tuple(GroupedRow(dimension_value=r.dimension_value,
                            value=round(r.value - 0.01 * len(sid), 3),
                            lineage=r.lineage) for r in g.rows)
    return GroupedResolvedMetric(
        metric_id=g.metric_id, unit=g.unit, label=g.label,
        dimension_concept=g.dimension_concept, system_id=sid,
        query_plan=g.query_plan, rows=rows)


def _partitioned_resolver(unpinned_exc, pinned=None, failing=()):
    """Unpinned ⇒ raise ``unpinned_exc``; pinned ⇒ that site's grouped result,
    or raise the exception listed for it in ``failing``."""
    calls = []

    def resolve(mid, dim, *, authority=None, **_k):
        if authority is None:
            raise unpinned_exc
        sid = authority.authoritative_system("quality-metric")
        calls.append(sid)
        if sid in failing:
            raise failing[sid]
        return (pinned or _grouped_for)(sid)
    resolve.calls = calls
    return resolve


def _ambiguity():
    return mr.AmbiguousAuthorityError(
        "ambiguous authority for concept 'quality-metric': 4 active "
        "AuthorityRules name different systems",
        concept="quality-metric", system_ids=sorted(_PLANTS))


def test_per_site_authority_fans_out_one_bar_per_site_and_line(monkeypatch):
    resolve = _partitioned_resolver(_ambiguity())
    monkeypatch.setattr(md, "resolve_metric_by_dimension", resolve)
    monkeypatch.setattr(mr.catalog, "get_onboarded", _sited_catalog)

    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "ok" and result["kind"] == "bar"
    assert sorted(resolve.calls) == sorted(_PLANTS)
    # Sites in site order, lines in each site's own order; every bar names
    # its site so lines sharing a name across plants never collide.
    assert [r[0] for r in result["rows"]] == [
        f"{plant} · {line}"
        for plant in ("East Plant", "North Plant", "South Plant", "West Plant")
        for line in ("Line1", "Line2", "Line3")]
    assert len(result["lineage"]) == 12
    assert result["lineage"][0]["dimension_value"] == "Line1"
    # No SINGLE system stands behind the panel; the per-site breakdown does.
    assert result["system_id"] is None
    assert result["sites"] == [
        {"site": "East Plant", "system_id": "mes-east", "status": "ok", "rows": 3},
        {"site": "North Plant", "system_id": "mes-north", "status": "ok", "rows": 3},
        {"site": "South Plant", "system_id": "mes-south", "status": "ok", "rows": 3},
        {"site": "West Plant", "system_id": "mes-west", "status": "ok", "rows": 3},
    ]
    assert result["columns"][1]["label"] == "Quality Rate"
    json.dumps(result)


def test_a_site_that_cannot_answer_is_reported_and_the_others_still_render(monkeypatch):
    failing = {"mes-north": mr.CoverageGapError("no dimension mapping", concept="production-line"),
               "mes-south": MetricResolverError("no JoinPath connecting tables")}
    resolve = _partitioned_resolver(_ambiguity(), failing=failing)
    monkeypatch.setattr(md, "resolve_metric_by_dimension", resolve)
    monkeypatch.setattr(mr.catalog, "get_onboarded", _sited_catalog)

    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "ok"
    assert len(result["rows"]) == 6
    assert [s["status"] for s in result["sites"]] == ["ok", "uncovered", "error", "ok"]


def test_every_site_uncovered_is_the_honest_uncovered_envelope(monkeypatch):
    failing = {sid: mr.CoverageGapError("no dimension mapping", concept="production-line")
               for sid in _PLANTS}
    resolve = _partitioned_resolver(_ambiguity(), failing=failing)
    monkeypatch.setattr(md, "resolve_metric_by_dimension", resolve)
    monkeypatch.setattr(mr.catalog, "get_onboarded", _sited_catalog)

    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "uncovered"
    assert result["missing_concepts"] == ["production-line"]


def test_a_genuine_conflict_keeps_the_error(monkeypatch):
    """Two systems at ONE plant (not a partition) is a steward's conflict."""
    resolve = _partitioned_resolver(mr.AmbiguousAuthorityError(
        "ambiguous authority", concept="quality-metric",
        system_ids=["mes-west", "mes-west-legacy"]))
    monkeypatch.setattr(md, "resolve_metric_by_dimension", resolve)
    monkeypatch.setattr(mr.catalog, "get_onboarded", lambda: _sited_catalog() + [
        {"id": "mes-west-legacy", "name": "Old MES", "plant": "West Plant"}])

    result = asyncio.run(resolve_metric_by_dimension_route(_BODY))

    assert result["status"] == "error" and "ambiguous authority" in result["error"]
    assert resolve.calls == []


def test_a_pinned_request_never_fans_out(monkeypatch):
    resolve = _partitioned_resolver(_ambiguity())
    monkeypatch.setattr(md, "resolve_metric_by_dimension", resolve)
    monkeypatch.setattr(mr.catalog, "get_onboarded", _sited_catalog)
    monkeypatch.setattr(mr.catalog, "get_onboarded_ids", lambda: set(_PLANTS))

    body = ResolveMetricByDimensionRequest(
        metric_id="quality-rate", dimension_concept="production-line", system_id="mes-north")
    result = asyncio.run(resolve_metric_by_dimension_route(body))

    assert result["status"] == "ok" and result["system_id"] == "mes-north"
    assert resolve.calls == ["mes-north"]
    assert "sites" not in result


# --- AuthMiddleware coverage via TestClient ----------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(mr.router)
    return app


_JSON_BODY = {"metric_id": "quality-rate", "dimension_concept": "production-line"}


def test_route_requires_auth(monkeypatch):
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.post("/api/metrics/resolve-by-dimension", json=_JSON_BODY)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_route_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    get_config.cache_clear()
    monkeypatch.setattr(md, "resolve_metric_by_dimension",
                        lambda mid, dim, **_k: _grouped())
    client = TestClient(_build_app())

    resp = client.post("/api/metrics/resolve-by-dimension", json=_JSON_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kind"] == "bar"
    assert [r[0] for r in body["rows"]] == ["Line1", "Line2", "Line3"]
