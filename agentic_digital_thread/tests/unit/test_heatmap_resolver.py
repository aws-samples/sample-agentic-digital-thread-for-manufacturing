# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the sensor-heatmap resolver (Wave 3, S5.3 §6.3).

No AWS, no graph, no DB, no LLM. The graph (``neptune_query``) and Postgres
(``query_pg``) seams are monkeypatched on ``agents.metric_resolver`` (the heatmap
resolver reuses them by attribute, like ``metric_dimension``). SQL-shape tests
build a ``HeatmapPlan`` directly; full-resolve tests drive a canned graph.
"""
from __future__ import annotations

import dataclasses

import pytest as _pytest_seed


@_pytest_seed.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030) and the demo-seed machinery
    has been DELETED from production (ADR-0035). These tests exercise demo systems
    (schemas) BY NAME, so temporarily seed the shared catalog from the test-local
    ``DEMO_SYSTEMS`` fixture, then restore the empty base-mode catalog afterward."""
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

import json

import pytest

import agents.metric_resolver as mr
import agents.heatmap_resolver as hm
from agents.heatmap_resolver import (
    HeatmapPlan,
    MAX_CELLS,
    _compose_heatmap_sql,
    _severity,
    resolve_heatmap,
)
from agents.metric_dimension import JoinPathProvenance
from agents.metric_resolver import AuthorityResolver, MetricResolverError
from agents.metric_layer import Aggregation


class _FakeAuthority(AuthorityResolver):
    def __init__(self, system_id):
        self._sid = system_id

    def authoritative_system(self, concept):
        return self._sid


# --- severity (deterministic, closed vocabulary) ----------------------------

def test_severity_boundaries():
    assert _severity(100, 100) == "critical"   # at threshold → critical
    assert _severity(150, 100) == "critical"   # above
    assert _severity(95, 100) == "warning"     # within WARN_FRACTION (0.9)
    assert _severity(90, 100) == "warning"     # exactly 0.9*t
    assert _severity(80, 100) == "ok"          # below warn band
    assert _severity(50, None) == "uncovered"  # no threshold → never fabricated
    assert _severity(None, 100) == "uncovered"
    assert _severity("not-a-number", 100) == "uncovered"
    # zero-threshold guard (no divide-by-zero)
    assert _severity(0, 0) == "ok"
    assert _severity(5, 0) == "critical"


# --- SQL composition shapes -------------------------------------------------

def _latest_plan(**kw):
    base = dict(
        system_id="mes-mil", schema="mes_milwaukee",
        measure_table="sensor_readings", measure_column="value",
        aggregation=Aggregation.LATEST,
        dim_x_column="asset_id", dim_y_column="line",
        order_column="reading_time",
    )
    base.update(kw)
    return HeatmapPlan(**base)


_LATEST_NO_THRESHOLD = (
    'SELECT DISTINCT ON ("asset_id", "line") "asset_id", "line", "value" '
    'FROM "mes_milwaukee"."sensor_readings" '
    'ORDER BY "asset_id", "line", "reading_time" DESC LIMIT 200'
)
_LATEST_COLOCATED_THRESHOLD = (
    'SELECT DISTINCT ON ("asset_id", "line") "asset_id", "line", "value", "threshold" '
    'FROM "mes_milwaukee"."sensor_readings" '
    'ORDER BY "asset_id", "line", "reading_time" DESC LIMIT 200'
)
_LATEST_JOINED_THRESHOLD = (
    'SELECT DISTINCT ON (m."asset_id", m."line") m."asset_id", m."line", m."value", t."usl" '
    'FROM "mes_milwaukee"."sensor_readings" m '
    'JOIN "mes_milwaukee"."specifications" t ON m."spec_id" = t."spec_id" '
    'ORDER BY m."asset_id", m."line", m."reading_time" DESC LIMIT 200'
)
_AVG_NO_THRESHOLD = (
    'SELECT "asset_id", "line", AVG("value") '
    'FROM "mes_milwaukee"."sensor_readings" '
    'GROUP BY "asset_id", "line" ORDER BY "asset_id", "line" LIMIT 200'
)
_AVG_COLOCATED_THRESHOLD = (
    'SELECT "asset_id", "line", AVG("value"), MAX("threshold") '
    'FROM "mes_milwaukee"."sensor_readings" '
    'GROUP BY "asset_id", "line" ORDER BY "asset_id", "line" LIMIT 200'
)


def test_compose_latest_no_threshold():
    assert _compose_heatmap_sql(_latest_plan()) == _LATEST_NO_THRESHOLD


def test_compose_latest_colocated_threshold():
    plan = _latest_plan(threshold_table="sensor_readings", threshold_column="threshold")
    assert _compose_heatmap_sql(plan) == _LATEST_COLOCATED_THRESHOLD


def test_compose_latest_joined_threshold():
    join = JoinPathProvenance(
        join_id="jp1", from_table="sensor_readings", from_field="spec_id",
        to_table="specifications", to_field="spec_id",
        transform="direct", method="foreign_key_match",
    )
    plan = _latest_plan(threshold_table="specifications", threshold_column="usl",
                        threshold_join=join)
    assert _compose_heatmap_sql(plan) == _LATEST_JOINED_THRESHOLD


def test_compose_avg_no_threshold():
    plan = _latest_plan(aggregation=Aggregation.AVG, order_column=None)
    assert _compose_heatmap_sql(plan) == _AVG_NO_THRESHOLD


def test_compose_avg_colocated_threshold():
    plan = _latest_plan(aggregation=Aggregation.AVG, order_column=None,
                        threshold_table="sensor_readings", threshold_column="threshold")
    assert _compose_heatmap_sql(plan) == _AVG_COLOCATED_THRESHOLD


def test_compose_latest_fail_loud_without_order_column():
    plan = _latest_plan(order_column=None)
    with pytest.raises(MetricResolverError, match="time/ordering column"):
        _compose_heatmap_sql(plan)


def test_compose_rejects_injection():
    bad = _latest_plan(dim_x_column="asset; DROP TABLE x")
    with pytest.raises(ValueError):
        _compose_heatmap_sql(bad)
    bad2 = _latest_plan(threshold_table="specs", threshold_column='usl" --',
                        threshold_join=JoinPathProvenance(
                            join_id="j", from_table="sensor_readings", from_field="spec_id",
                            to_table="specs", to_field="spec_id"))
    with pytest.raises(ValueError):
        _compose_heatmap_sql(bad2)


def test_max_cells_cap():
    assert MAX_CELLS == 200
    assert _compose_heatmap_sql(_latest_plan()).endswith("LIMIT 200")


# --- full resolve (canned graph) --------------------------------------------

def _hm_nq(*, measure_row, dim_map, time_rows, join_edges=None):
    """neptune_query dispatcher: measure read (``mr._MEASURE_BINDING_CYPHER``), concept-keyed dimension/
    threshold reads, the event-time read, and the JoinPath read."""
    def nq(cypher, params=None):
        p = params or {}
        c = p.get("concept")
        if "RETURN DISTINCT c.id AS id" in cypher:
            return [{"id": "sensor-reading"}]
        if "JoinPath" in cypher:
            return join_edges or []
        if c == "event-time":
            return time_rows
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return [measure_row]
        return dim_map.get(c, [])
    return nq


_MEASURE_ROW = {"table": "sensor_readings", "column": "value", "role": "measure",
                "confidence": 0.9, "source": "fk"}
_DIMX = [{"table": "sensor_readings", "column": "asset_id", "role": "dimension",
          "confidence": 1.0, "source": "user_confirmed"}]
_DIMY = [{"table": "sensor_readings", "column": "line", "role": "dimension",
          "confidence": 1.0, "source": "user_confirmed"}]
_TIME = [{"table": "sensor_readings", "column": "reading_time", "role": "dimension",
          "confidence": 1.0, "source": "user_confirmed"}]


def test_resolve_heatmap_uncovered_measure_short_circuits_resolution(monkeypatch):
    def _unexpected(*_args, **_kwargs):
        raise AssertionError("authority/database resolver must not run for uncovered heatmap")

    monkeypatch.setattr(mr, "_resolve_authority", _unexpected)
    monkeypatch.setattr(mr, "query_pg", _unexpected)

    env = resolve_heatmap(
        "sensor-reading-latest",
        "asset",
        "production-line",
        "specification",
        query=lambda *_args, **_kwargs: [],
    )

    assert env["status"] == "uncovered"
    assert env["missing_concepts"] == ["sensor-reading"]
    assert env["columns"] == []
    assert env["rows"] == []
    assert env["lineage"] == []
    assert "set_authority" not in json.dumps(env)


def test_resolve_heatmap_mapped_but_empty_is_no_data(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY},
        time_rows=_TIME,
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: json.dumps({
        "columns": ["asset_id", "line", "value"],
        "rows": [],
        "row_count": 0,
    }))

    env = resolve_heatmap(
        "sensor-reading-latest",
        "asset",
        "production-line",
        authority=_FakeAuthority("mes-mil"),
    )

    assert env["status"] == "no-data"
    assert env["rows"] == []
    assert env["lineage"] == []
    assert "missing_concepts" not in env


def test_resolve_heatmap_full_with_colocated_threshold(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY,
                 "specification": [{"table": "sensor_readings", "column": "threshold",
                                    "role": "dimension", "confidence": 1.0, "source": "x"}]},
        time_rows=_TIME,
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: json.dumps({
        "columns": ["asset_id", "line", "value", "threshold"],
        "rows": [["A1", "Line1", 105, 100], ["A1", "Line2", 80, 100],
                 ["A2", "Line1", 95, 100]],
        "row_count": 3,
    }))
    env = resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                          "specification", authority=_FakeAuthority("mes-mil"))

    assert env["kind"] == "heatmap"
    assert env["status"] == "ok"
    assert env["dims"] == ["asset", "production-line"]
    assert env["threshold_status"] == "covered"
    assert env["system_id"] == "mes-mil"
    assert env["rows"] == [
        ["A1", "Line1", 105, 100, "critical"],
        ["A1", "Line2", 80, 100, "ok"],
        ["A2", "Line1", 95, 100, "warning"],
    ]
    # per-cell lineage, JSON-serializable, no enums
    assert len(env["lineage"]) == 3
    assert env["lineage"][0]["severity"] == "critical"
    assert env["lineage"][0]["aggregation"] == "latest"
    assert env["lineage"][0]["sql"] == _LATEST_COLOCATED_THRESHOLD
    json.dumps(env)


def test_resolve_heatmap_uncovered_threshold_degrades(monkeypatch):
    # threshold concept 'specification' UNMAPPED → degrade cleanly to uncovered
    # (no fabricated threshold; severity 'uncovered' per cell), status still ok.
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY, "specification": []},
        time_rows=_TIME,
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: json.dumps({
        "columns": ["asset_id", "line", "value"],
        "rows": [["A1", "Line1", 105], ["A2", "Line2", 80]],
        "row_count": 2,
    }))
    env = resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                          "specification", authority=_FakeAuthority("mes-mil"))

    assert env["status"] == "ok"
    assert env["threshold_status"] == "uncovered"
    assert env["rows"] == [
        ["A1", "Line1", 105, None, "uncovered"],
        ["A2", "Line2", 80, None, "uncovered"],
    ]
    assert env["lineage"][0]["sql"] == _LATEST_NO_THRESHOLD


def test_resolve_heatmap_no_threshold_concept(monkeypatch):
    # No threshold_concept passed at all → uncovered, no threshold column selected.
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY},
        time_rows=_TIME,
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: json.dumps({
        "columns": ["asset_id", "line", "value"],
        "rows": [["A1", "Line1", 50]], "row_count": 1,
    }))
    env = resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                          authority=_FakeAuthority("mes-mil"))
    assert env["threshold_status"] == "uncovered"
    assert env["rows"] == [["A1", "Line1", 50, None, "uncovered"]]


def test_resolve_heatmap_threshold_join_fan_out_fails_loud(monkeypatch):
    # threshold on a separate table; the only JoinPath stores the measure on the
    # PK/one (``to``) side → fan-out orientation → fail loud (never silently reversed).
    fan_out_edge = {
        "join_id": "jp-fanout",
        "from_table": "mes_milwaukee.specifications", "from_field": "reading_id",
        "to_table": "mes_milwaukee.sensor_readings", "to_field": "reading_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY,
                 "specification": [{"table": "specifications", "column": "usl",
                                    "role": "dimension", "confidence": 1.0, "source": "x"}]},
        time_rows=_TIME, join_edges=[fan_out_edge],
    ))
    with pytest.raises(MetricResolverError, match="fan out"):
        resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                        "specification", authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_joined_threshold(monkeypatch):
    # threshold on specifications, single-hop joinable in the SAFE canonical
    # orientation (measure on the FK/from side) → joined threshold SQL + severity.
    join_edge = {
        "join_id": "jp1",
        "from_table": "mes_milwaukee.sensor_readings", "from_field": "spec_id",
        "to_table": "mes_milwaukee.specifications", "to_field": "spec_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY,
                 "specification": [{"table": "specifications", "column": "usl",
                                    "role": "dimension", "confidence": 1.0, "source": "x"}]},
        time_rows=_TIME, join_edges=[join_edge],
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: json.dumps({
        "columns": ["asset_id", "line", "value", "usl"],
        "rows": [["A1", "Line1", 50, 100]], "row_count": 1,
    }))
    env = resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                          "specification", authority=_FakeAuthority("mes-mil"))
    assert env["threshold_status"] == "covered"
    assert env["rows"] == [["A1", "Line1", 50, 100, "ok"]]
    assert env["lineage"][0]["sql"] == _LATEST_JOINED_THRESHOLD
    assert env["lineage"][0]["threshold_join"]["from_table"] == "sensor_readings"


def test_resolve_heatmap_dimension_not_colocated_fails_loud(monkeypatch):
    # second dimension maps to a DIFFERENT table than the measure → cross-table
    # heatmap dimension deferred (fail loud).
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX,
                 "production-line": [{"table": "work_orders", "column": "line",
                                      "role": "dimension", "confidence": 1.0, "source": "x"}]},
        time_rows=_TIME,
    ))
    with pytest.raises(MetricResolverError, match="not co-located"):
        resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_latest_no_time_concept_fails_loud(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY},
        time_rows=[],  # event-time unmapped
    ))
    with pytest.raises(MetricResolverError, match="event-time"):
        resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_derived_metric_fails_loud():
    with pytest.raises(MetricResolverError, match="derived"):
        resolve_heatmap("oee", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_unknown_metric_fails_loud():
    with pytest.raises(MetricResolverError, match="unknown metric"):
        resolve_heatmap("nope-metric", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_db_error_wrapped(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"asset": _DIMX, "production-line": _DIMY},
        time_rows=_TIME,
    ))

    def _boom(_schema, _sql, params=None):
        raise RuntimeError("column reading_time does not exist")

    monkeypatch.setattr(mr, "query_pg", _boom)
    with pytest.raises(MetricResolverError, match="heatmap query failed"):
        resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))


def test_resolve_heatmap_unmapped_dimension_is_a_coverage_gap(monkeypatch):
    """Test event 18 (2026-09-18): on every workshop deployment the sensor
    heatmap read 'unavailable' (error styling + Retry) because the authority
    system had no MAPS_TO field for the 'asset' dimension. That is a MAPPING
    gap — the same condition the by-dimension route already classifies as
    ``CoverageGapError`` → 'uncovered' — not a fault a retry could fix."""
    from agents.metric_resolver import CoverageGapError
    monkeypatch.setattr(mr, "neptune_query", _hm_nq(
        measure_row=_MEASURE_ROW,
        dim_map={"production-line": _DIMY},  # 'asset' unmapped on the authority
        time_rows=_TIME,
    ))
    with pytest.raises(CoverageGapError) as excinfo:
        resolve_heatmap("sensor-reading-latest", "asset", "production-line",
                        authority=_FakeAuthority("mes-mil"))
    assert excinfo.value.concept == "asset"
    assert "no MAPS_TO field for heatmap dimension 'asset'" in str(excinfo.value)


def test_heatmap_route_renders_a_dimension_gap_as_uncovered(monkeypatch):
    from agents import metric_resolver as route_mr
    from agents.metric_resolver import CoverageGapError, ResolveHeatmapRequest
    import agents.heatmap_resolver as hm

    def _gap(*_a, **_k):
        raise CoverageGapError("no MAPS_TO field for heatmap dimension 'asset' in system 'sys-a'",
                               concept="asset")
    monkeypatch.setattr(hm, "resolve_heatmap", _gap)
    body = ResolveHeatmapRequest(metric_id="sensor-reading-latest", dimension_concept_x="asset",
                                 dimension_concept_y="production-line",
                                 threshold_concept="specification")
    env = route_mr._resolve_heatmap_payload(body)
    assert env["status"] == "uncovered"
    assert env["kind"] == "heatmap"
    assert env["missing_concepts"] == ["asset"]
    assert env["dims"] == ["asset", "production-line"]
    assert env["rows"] == [] and env["columns"] == [] and env["lineage"] == []
    assert "error" not in env
