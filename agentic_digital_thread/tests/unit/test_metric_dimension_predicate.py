# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0075 — the GROUPED resolver for filtered DIRECT metrics.

The predicate lands on the MEASURE side (``m.`` in the joined shape) after the
Athena prune and window WHEREs; an unbound concept raises
``UnresolvedPredicateError`` ⊂ ``CoverageGapError`` → the route's existing
``uncovered + missing_concepts`` envelope; LATEST + filter and non-SQL
backends fail closed; filter-less grouped SQL is byte-identical. Offline;
synthetic names only (seams mirror ``test_metric_dimension_window``).
"""
from __future__ import annotations

import pytest

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_resolver import (
    CoverageGapError,
    MetricResolverError,
    StructuralMetricError,
    UnresolvedPredicateError,
    _resolve_by_dimension_payload,
)
from agents.models import ResolveMetricByDimensionRequest

STATE = "widget-color"
_FILTER = (Predicate(STATE, Op.EQ, ("BLUE",)),)


def _wire(monkeypatch, *, filter=_FILTER, bound=True, conn_type="PostgreSQL",
          joined=False, aggregation=Aggregation.COUNT, window=None):
    metric = DirectMetric(
        id="m-wid", label="Widgets", unit="widgets", concept="widget-event",
        aggregation=aggregation, filter=filter, window=window,
    )

    class _Layer:
        def get_metric(self, mid):
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    auth = mr.AuthoritySource(system_id="sys-a", source="authority_rule")
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    mapping = mr.MappingProvenance(table="widget_events", column="widget_id",
                                   role=None, confidence=None, source="test")
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mapping)
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    reads: list[str] = []

    def _neptune(cypher, params=None):
        if cypher == mr._PREDICATE_ELSEWHERE_CYPHER:
            return []
        assert cypher == mr._PREDICATE_BINDING_CYPHER
        assert params["table"] == "widget_events"
        reads.append(params["concept"])
        row = {"table": "widget_events", "column": "color_code"}
        return [row] if bound else []

    monkeypatch.setattr(mr, "neptune_query", _neptune)
    if joined:
        dim = mr.MappingProvenance(table="lines", column="line", role=None,
                                   confidence=None, source="test")
        join = md.JoinPathProvenance(join_id="j1", from_table="widget_events",
                                     from_field="line_id", to_table="lines", to_field="id")
    else:
        dim = mr.MappingProvenance(table="widget_events", column="line", role=None,
                                   confidence=None, source="test")
        join = None
    monkeypatch.setattr(md, "_resolve_dimension_mappings", lambda *a, **k: [dim])
    monkeypatch.setattr(md, "_select_dimension_mapping", lambda *a, **k: (dim, join))
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, table, query=None: "event_ts")
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(md, "_athena_time_window_where", lambda *a, **k: None)
    captured: dict = {"reads": reads}

    def _exec(plan, sql):
        captured["sql"] = sql
        return [("L1", 4)]

    monkeypatch.setattr(md, "_execute_grouped", _exec)
    return captured


def _resolve():
    return md.resolve_metric_by_dimension("m-wid", "widget-line")


def test_colocated_predicate_lands_before_group_by(monkeypatch):
    captured = _wire(monkeypatch)
    out = _resolve()
    assert captured["sql"] == (
        'SELECT "line", COUNT("widget_id") FROM "plant"."widget_events" '
        "WHERE \"color_code\" = 'BLUE' "
        'GROUP BY "line" ORDER BY "line" LIMIT 200'
    )
    assert out.rows[0].lineage.sql == captured["sql"]


def test_joined_predicate_is_measure_qualified(monkeypatch):
    captured = _wire(monkeypatch, joined=True)
    _resolve()
    assert "WHERE m.\"color_code\" = 'BLUE' GROUP BY d.\"line\"" in captured["sql"]


def test_postgres_window_then_predicate(monkeypatch):
    captured = _wire(monkeypatch, window="24h")
    _resolve()
    assert (
        "WHERE \"event_ts\" >= (now() - INTERVAL '24 hours') "
        "AND \"color_code\" = 'BLUE' GROUP BY" in captured["sql"]
    )


def test_athena_prune_then_predicate(monkeypatch):
    # The up-front prune remains only for an aggregate the connector cannot
    # compute exactly from pieces (here AVG); COUNT/SUM/MIN/MAX run as defined.
    captured = _wire(monkeypatch, conn_type="Athena", aggregation=Aggregation.AVG)
    monkeypatch.setattr(mr, "_athena_recent_partition_where",
                        lambda sid, table, qualifier="": f"{qualifier}\"dt\" >= '2026-07'")
    _resolve()
    assert (
        "WHERE \"dt\" >= '2026-07' AND \"color_code\" = 'BLUE' GROUP BY"
        in captured["sql"]
    )


def test_unbound_predicate_is_a_named_coverage_gap(monkeypatch):
    captured = _wire(monkeypatch, bound=False)
    with pytest.raises(UnresolvedPredicateError) as e:
        _resolve()
    assert isinstance(e.value, CoverageGapError) and e.value.concept == STATE
    assert "sql" not in captured


def test_route_maps_unbound_predicate_to_uncovered(monkeypatch):
    _wire(monkeypatch, bound=False)
    payload = _resolve_by_dimension_payload(
        ResolveMetricByDimensionRequest(metric_id="m-wid", dimension_concept="widget-line"))
    assert payload["status"] == "uncovered"
    assert payload["missing_concepts"] == [STATE]


def test_latest_with_filter_fails_closed(monkeypatch):
    captured = _wire(monkeypatch, aggregation=Aggregation.LATEST)
    with pytest.raises(MetricResolverError, match="latest"):
        _resolve()
    assert "sql" not in captured and captured["reads"] == []


def test_sitewise_with_filter_is_structural(monkeypatch):
    captured = _wire(monkeypatch, conn_type="SiteWise Direct")
    with pytest.raises(StructuralMetricError):
        _resolve()
    assert "sql" not in captured


def test_hostile_literal_fails_before_execution(monkeypatch):
    captured = _wire(monkeypatch, filter=(Predicate(STATE, Op.EQ, ("x';--",)),))
    with pytest.raises(MetricResolverError, match="invalid metric predicate"):
        _resolve()
    assert "sql" not in captured


def test_filter_less_grouped_sql_is_unchanged(monkeypatch):
    captured = _wire(monkeypatch, filter=None)
    _resolve()
    assert captured["sql"] == (
        'SELECT "line", COUNT("widget_id") FROM "plant"."widget_events" '
        'GROUP BY "line" ORDER BY "line" LIMIT 200'
    )
    assert captured["reads"] == []
