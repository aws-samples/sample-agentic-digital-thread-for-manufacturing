# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``POST /api/metrics/resolve-heatmap`` (Wave 3 backend).

No AWS, no graph, no DB, no network. Mirrors ``test_resolve_by_dimension_route``:
the route imports the heatmap core lazily as ``hm`` and calls ``hm.resolve_heatmap``,
so a single ``setattr`` on ``agents.heatmap_resolver`` drives the path. A minimal
FastAPI app + ``AuthMiddleware`` + ``TestClient`` proves the route inherits the
app-wide auth like the other ``/api/metrics/*`` routes.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.heatmap_resolver as hm
import agents.metric_resolver as mr
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.metric_resolver import MetricResolverError, resolve_heatmap_route
from agents.models import ResolveHeatmapRequest


def _envelope() -> dict:
    return {
        "kind": "heatmap", "status": "ok",
        "metric_id": "sensor-reading-latest", "label": "Latest Sensor Reading",
        "unit": "", "system_id": "mes-mil",
        "dims": ["asset", "production-line"],
        "threshold_concept": "specification", "threshold_status": "covered",
        "columns": [
            {"label": "Asset", "field_concept": "asset"},
            {"label": "Production Line", "field_concept": "production-line"},
            {"label": "Latest Sensor Reading", "field_concept": "sensor-reading"},
        ],
        "rows": [["A1", "Line1", 105, 100, "critical"]],
        "lineage": [{"severity": "critical"}],
    }


_BODY = ResolveHeatmapRequest(
    metric_id="sensor-reading-latest", dimension_concept_x="asset",
    dimension_concept_y="production-line", threshold_concept="specification")


def test_resolve_heatmap_success(monkeypatch):
    monkeypatch.setattr(hm, "resolve_heatmap", lambda *a, **k: _envelope())
    result = asyncio.run(resolve_heatmap_route(_BODY))
    assert result["status"] == "ok"
    assert result["kind"] == "heatmap"
    assert result["dims"] == ["asset", "production-line"]
    assert result["rows"][0][4] == "critical"
    json.dumps(result)


def test_resolve_heatmap_zero_coverage_is_uncovered_without_resolution(monkeypatch):
    monkeypatch.setattr(hm, "covered_concepts", lambda **_kwargs: frozenset())

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("authority/database resolver must not run for uncovered heatmap")

    monkeypatch.setattr(mr, "_resolve_authority", _unexpected)
    monkeypatch.setattr(mr, "query_pg", _unexpected)

    result = asyncio.run(resolve_heatmap_route(_BODY))

    assert result == {
        "kind": "heatmap",
        "status": "uncovered",
        "metric_id": "sensor-reading-latest",
        "label": "Latest Sensor Reading",
        "unit": "",
        "dims": ["asset", "production-line"],
        "threshold_concept": "specification",
        "threshold_status": "uncovered",
        "missing_concepts": ["sensor-reading"],
        "suggestion": "onboard a system that provides real-time sensor data",
        "columns": [],
        "rows": [],
        "lineage": [],
    }
    assert "set_authority" not in json.dumps(result)


def test_resolve_heatmap_coverage_failure_remains_error(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("graph backend unavailable")

    monkeypatch.setattr(hm, "covered_concepts", _boom)
    result = asyncio.run(resolve_heatmap_route(_BODY))

    assert result["status"] == "error"
    assert result["rows"] == []
    assert result["columns"] == []
    assert result["lineage"] == []
    assert result["error"] == "heatmap coverage query failed while reading graph metadata"
    assert "set_authority" not in json.dumps(result)


def test_resolve_heatmap_passes_request_args(monkeypatch):
    seen = {}

    def _capture(mid, dx, dy, threshold, *a, **k):
        seen.update(mid=mid, dx=dx, dy=dy, threshold=threshold)
        return _envelope()

    monkeypatch.setattr(hm, "resolve_heatmap", _capture)
    asyncio.run(resolve_heatmap_route(_BODY))
    assert seen == {"mid": "sensor-reading-latest", "dx": "asset",
                    "dy": "production-line", "threshold": "specification"}


@pytest.mark.parametrize("exc", [
    MetricResolverError("no MAPS_TO field for heatmap dimension"),
    NotImplementedError("deferred"),
    KeyError("unknown metric id"),
])
def test_resolve_heatmap_error_returns_friendly_dict(monkeypatch, exc):
    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(hm, "resolve_heatmap", _boom)
    result = asyncio.run(resolve_heatmap_route(_BODY))
    assert result["status"] == "error"
    assert result["kind"] == "heatmap"
    assert result["dims"] == ["asset", "production-line"]
    assert result["rows"] == []
    assert result["columns"] == []
    assert result["lineage"] == []
    assert result["threshold_status"] == "uncovered"
    assert result["error"]
    json.dumps(result)


# --- AuthMiddleware coverage via TestClient ----------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(mr.router)
    return app


_JSON_BODY = {
    "metric_id": "sensor-reading-latest", "dimension_concept_x": "asset",
    "dimension_concept_y": "production-line", "threshold_concept": "specification",
}


def test_route_requires_auth(monkeypatch):
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())
    resp = client.post("/api/metrics/resolve-heatmap", json=_JSON_BODY)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_route_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    get_config.cache_clear()
    monkeypatch.setattr(hm, "resolve_heatmap", lambda *a, **k: _envelope())
    client = TestClient(_build_app())
    resp = client.post("/api/metrics/resolve-heatmap", json=_JSON_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kind"] == "heatmap"
    assert body["rows"][0][0] == "A1"


def test_route_threshold_concept_optional(monkeypatch, local_auth_env):
    get_config.cache_clear()
    seen = {}

    def _capture(mid, dx, dy, threshold, *a, **k):
        seen["threshold"] = threshold
        return _envelope()

    monkeypatch.setattr(hm, "resolve_heatmap", _capture)
    client = TestClient(_build_app())
    resp = client.post("/api/metrics/resolve-heatmap", json={
        "metric_id": "sensor-reading-latest", "dimension_concept_x": "asset",
        "dimension_concept_y": "production-line",
    })
    assert resp.status_code == 200
    assert seen["threshold"] is None
