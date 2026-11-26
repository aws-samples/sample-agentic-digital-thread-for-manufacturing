# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0080 D1/D3 — ``resolve_definition`` is the ONE computation home.

Pins: ``resolve_definition`` equals ``resolve_metric`` for EVERY code-registry
metric (with and without an explicit layer); an explicit layer resolves
derived inputs THERE (a pending pair resolves together) and a missing input
WITHHOLDS as ``unresolved_input`` naming the innermost id; the layer path
fires no ADR-0066 D6 usage telemetry; the execution trace attributes Athena
executions with ``:preview`` and reports MEASURED cost only, while the live
path is byte-identical (no ``run`` keyword). Offline — every seam patched.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_resolver as mr
from agents.metric_execution_trace import (
    PREVIEW_SUFFIX,
    active_trace,
    execution_trace,
)
from agents.metric_layer import (
    Aggregation,
    CodeMetricLayer,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricRef,
    Scalar,
)

_AUTH = mr.AuthoritySource(system_id="sys-a", source="authority_rule")


def _mapping(concept):
    ident = concept.replace("-", "_")
    return mr.MappingProvenance(table=f"t_{ident}", column=f"c_{ident}",
                                role=None, confidence=None, source="onboarding")


@pytest.fixture
def stubbed(monkeypatch):
    """Every graph/DB seam stubbed: each direct metric reads (7, 3)."""
    executed = []
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer())
    monkeypatch.setattr(mr, "_resolve_authority", lambda concept, auth: _AUTH)
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: _mapping(c))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: "PostgreSQL")
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: "2026-09-25T00:00:00Z")
    monkeypatch.setattr(mr, "_apply_scalar_window",
                        lambda plan, sql, window: (window, "event_ts", sql))

    def _row(plan, sql):
        executed.append(sql)
        return [7, 3]
    monkeypatch.setattr(mr, "_execute_row", _row)
    return executed


@pytest.mark.parametrize("metric", CodeMetricLayer().list_metrics(),
                         ids=lambda m: m.id)
def test_resolve_definition_matches_resolve_metric_for_every_code_metric(
        stubbed, metric):
    """D1 parity: the factored body IS the served path, for every code metric
    — including the fail-loud outcome (e.g. the deferred ``latest``)."""
    live = _outcome(mr.resolve_metric, metric.id)
    assert _outcome(mr.resolve_definition, metric) == live
    assert _outcome(mr.resolve_definition, metric, layer=CodeMetricLayer()) == live


def _outcome(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (mr.MetricResolverError, NotImplementedError) as exc:
        return (type(exc), str(exc))


def _pair():
    num = DirectMetric(id="pending-num", concept="widget-event",
                       aggregation=Aggregation.COUNT, label="Num", unit="u")
    ratio = DerivedMetric(
        id="pending-ratio", label="Ratio", unit="ratio",
        formula=Formula(FormulaOp.RATIO, (MetricRef("pending-num"),
                                          MetricRef("availability"))))
    return num, ratio


def test_explicit_layer_resolves_a_pending_pair_together(stubbed):
    num, ratio = _pair()
    layer = CodeMetricLayer([*CodeMetricLayer().list_metrics(), num, ratio])

    resolved = mr.resolve_definition(ratio, layer=layer)

    assert resolved.status is None
    assert resolved.value == 1.0
    assert [i.metric_id for i in resolved.lineage.inputs] == [
        "pending-num", "availability"]


def test_missing_input_withholds_as_unresolved_input_naming_the_id(stubbed):
    _num, ratio = _pair()
    layer = CodeMetricLayer()  # the pending numerator is NOT supplied

    resolved = mr.resolve_definition(ratio, layer=layer)

    assert resolved.status == mr.UNRESOLVED_INPUT_STATUS == "unresolved_input"
    assert resolved.value is None
    assert "'pending-num'" in resolved.detail
    assert resolved.lineage is None


def test_nested_missing_input_names_the_innermost_id(stubbed):
    inner = DerivedMetric(id="inner", label="Inner", unit="u",
                          formula=Formula(FormulaOp.SUM, (MetricRef("ghost"),
                                                          Scalar(1.0))))
    outer = DerivedMetric(id="outer", label="Outer", unit="u",
                          formula=Formula(FormulaOp.SCALE, (MetricRef("inner"),
                                                            Scalar(2.0))))
    layer = CodeMetricLayer([inner, outer])

    resolved = mr.resolve_definition(outer, layer=layer)

    assert resolved.status == mr.UNRESOLVED_INPUT_STATUS
    assert "'ghost'" in resolved.detail
    assert stubbed == []  # nothing executed for a withheld tree


def test_live_path_never_produces_unresolved_input(stubbed):
    """Without a layer an unknown input stays the historical boundary error."""
    _num, ratio = _pair()
    with pytest.raises(mr.MetricResolverError, match="unresolvable"):
        mr.resolve_definition(ratio)


def test_explicit_layer_fires_no_usage_telemetry(stubbed, monkeypatch):
    bumps = []
    monkeypatch.setattr(mr, "_record_graph_definition_usage", bumps.append)
    num, ratio = _pair()
    layer = CodeMetricLayer([*CodeMetricLayer().list_metrics(), num, ratio])

    mr.resolve_definition(ratio, layer=layer)
    assert bumps == []
    mr.resolve_metric("oee")  # the served path still bumps (D6 unchanged)
    assert "oee" in bumps


# --- execution trace: attribution + measured cost ----------------------------

def test_trace_is_scoped_and_restored():
    assert active_trace() is None
    with execution_trace() as trace:
        assert active_trace() is trace
        assert trace.attribution("sys-a") == "sys-a" + PREVIEW_SUFFIX
    assert active_trace() is None


def test_trace_cost_is_measured_only():
    with execution_trace() as trace:
        trace.record("sys-b", 100)
        trace.record("sys-b", 50)
        trace.record("sys-a", None)
        trace.record("sys-c", 10)
        trace.record("sys-c", True)  # a bool is not a byte count
    assert trace.cost() == {
        "query_count": 5,
        "systems": [
            {"system_id": "sys-a", "query_count": 1},
            {"system_id": "sys-b", "query_count": 2, "scanned_bytes": 150},
            {"system_id": "sys-c", "query_count": 2},
        ],
    }
    assert "usd" not in json.dumps(trace.cost()).lower()


def _athena_plan():
    return mr.QueryPlan(system_id="sys-a", schema="db", table="t", column="c",
                        aggregation=Aggregation.COUNT)


def _patch_athena(monkeypatch, calls):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"workgroup": "w"})

    def _retry(compose, cols, cfg, *, essential=(), **kwargs):
        calls.append(kwargs)
        run = kwargs.get("run")
        result = (run(compose(cols), cfg) if run
                  else {"rows": [[4, 4]], "row_count": 1})
        return {"result": result, "columns": cols, "denied": []}
    monkeypatch.setattr(mr.athena_tools, "run_athena_select_with_column_retry", _retry)


def test_live_athena_execution_is_byte_identical(monkeypatch):
    calls = []
    _patch_athena(monkeypatch, calls)
    assert mr._execute_row(_athena_plan(), "SELECT 1") == [4, 4]
    assert calls == [{}]  # no ``run`` keyword on the live path


def test_traced_athena_execution_is_attributed_and_counted(monkeypatch):
    calls, seen = [], []
    _patch_athena(monkeypatch, calls)

    def _query(sql, cfg, *, source_system=""):
        seen.append(source_system)
        return {"rows": [[4, 4]], "row_count": 1, "scanned_bytes": 2048}
    monkeypatch.setattr(mr.athena_tools, "run_athena_query", _query)

    with execution_trace() as trace:
        assert mr._execute_row(_athena_plan(), "SELECT 1") == [4, 4]
    assert seen == ["sys-a:preview"]
    assert trace.cost()["systems"] == [
        {"system_id": "sys-a", "query_count": 1, "scanned_bytes": 2048}]


def test_traced_postgres_execution_is_counted_without_bytes(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get", lambda sid: None)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql: json.dumps(
        {"rows": [[1, 1]]}))
    with execution_trace() as trace:
        mr._execute_row(_athena_plan(), "SELECT 1")
    assert trace.cost() == {"query_count": 1, "systems": [
        {"system_id": "sys-a", "query_count": 1}]}
