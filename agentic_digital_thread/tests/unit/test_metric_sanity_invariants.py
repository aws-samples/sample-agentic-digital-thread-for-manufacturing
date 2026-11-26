# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline regression tests for declared metric sanity invariants.

The resolver remains deterministic: declared bounds and denominator provenance
are checked after resolution, without an LLM or invented replacement value.
"""
from __future__ import annotations

import json

import pytest

import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation,
    CodeMetricLayer,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricLayerError,
    MetricRef,
    Scalar,
    validate_metric_layer,
)
from agents.metric_resolver import PinnedAuthority, _lineage_to_dict, resolve_metric
from agents.models import ResolveMetricRequest


_SYSTEM_ID = "system-a"
_SCHEMA = "operations"


def _wire_metrics(monkeypatch, metrics, values):
    """Install a synthetic registry and deterministic graph/SQL responses."""
    layer = CodeMetricLayer(metrics)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(
        mr, "get_authority_resolver", lambda: PinnedAuthority(_SYSTEM_ID)
    )
    monkeypatch.setattr(mr, "get_schema", lambda _sid: _SCHEMA)
    monkeypatch.setattr(mr, "_connection_type", lambda _sid: "PostgreSQL")

    columns = {
        "scrap-waste": "defect_count",
        "actual-quantity": "opportunity_count",
        "runtime-hours": "runtime_total",
        "stored-composite": "stored_score",
    }
    queried_columns: list[str] = []

    def _graph(_cypher, params=None):
        concept = (params or {}).get("concept")
        return [{"table": "measurements", "column": columns[concept]}]

    def _query(_schema, sql, params=None):  # noqa: ARG001
        for column, result in values.items():
            if f"({column})" in sql:
                queried_columns.append(column)
                value, count = result
                return json.dumps({
                    "columns": ["sum", "count"],
                    "rows": [[value, count]],
                    "row_count": 1,
                })
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(mr, "neptune_query", _graph)
    monkeypatch.setattr(mr, "query_pg", _query)
    return layer, queried_columns


def _ratio_metric(
    *,
    denominator_concept="actual-quantity",
    valid_range=None,
    max_exclusive=False,
    prefer_stored=None,
):
    numerator = DirectMetric(
        id="defect-events",
        concept="scrap-waste",
        aggregation=Aggregation.SUM,
        label="Defects",
        unit="events",
    )
    denominator = DirectMetric(
        id="opportunity-total",
        concept=denominator_concept,
        aggregation=Aggregation.SUM,
        label="Opportunities",
        unit="opportunities",
    )
    ratio = DerivedMetric(
        id="defects-per-million-opportunities",
        label="Defects per million opportunities",
        unit="events/million",
        formula=Formula(
            FormulaOp.SCALE,
            (
                Formula(
                    FormulaOp.RATIO,
                    (MetricRef(numerator.id), MetricRef(denominator.id)),
                ),
                Scalar(1_000_000.0),
            ),
        ),
        prefer_stored=prefer_stored,
        valid_range=valid_range,
        max_exclusive=max_exclusive,
        denominator_provenance="actual-quantity",
    )
    return numerator, denominator, ratio


@pytest.mark.parametrize(
    "bad_range",
    [(), (0.0,), (1.0, 1.0), (2.0, 1.0), (0.0, float("inf")), ("0", 1.0)],
)
def test_registry_rejects_malformed_valid_range(bad_range):
    layer = CodeMetricLayer([
        DirectMetric(
            id="bounded-count",
            concept="actual-quantity",
            aggregation=Aggregation.SUM,
            label="Bounded count",
            unit="units",
            valid_range=bad_range,
        ),
    ])

    with pytest.raises(MetricLayerError, match="bounded-count.*valid_range"):
        validate_metric_layer(layer)


def test_registry_rejects_max_exclusive_without_valid_range():
    layer = CodeMetricLayer([
        DirectMetric(
            id="unbounded-count",
            concept="actual-quantity",
            aggregation=Aggregation.SUM,
            label="Unbounded count",
            unit="units",
            max_exclusive=True,
        ),
    ])

    with pytest.raises(MetricLayerError, match="unbounded-count.*max_exclusive"):
        validate_metric_layer(layer)


def test_registry_rejects_non_boolean_max_exclusive():
    layer = CodeMetricLayer([
        DirectMetric(
            id="bounded-count",
            concept="actual-quantity",
            aggregation=Aggregation.SUM,
            label="Bounded count",
            unit="units",
            valid_range=(0.0, 1.0),
            max_exclusive="yes",
        ),
    ])

    with pytest.raises(MetricLayerError, match="bounded-count.*max_exclusive"):
        validate_metric_layer(layer)


def test_registry_rejects_denominator_provenance_without_ratio():
    direct = DirectMetric(
        id="source-count",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Source count",
        unit="units",
    )
    derived = DerivedMetric(
        id="scaled-count",
        label="Scaled count",
        unit="units",
        formula=Formula(
            FormulaOp.SCALE,
            (MetricRef(direct.id), Scalar(100.0)),
        ),
        denominator_provenance="actual-quantity",
    )

    with pytest.raises(
        MetricLayerError,
        match="scaled-count.*denominator_provenance.*ratio",
    ):
        validate_metric_layer(CodeMetricLayer([direct, derived]))


def test_dpmo_one_million_shape_is_an_honest_invariant_violation(monkeypatch):
    """Every opportunity defective yields exactly 1,000,000 and is excluded."""
    metrics = _ratio_metric(
        valid_range=(0.0, 1_000_000.0),
        max_exclusive=True,
    )
    _wire_metrics(
        monkeypatch,
        metrics,
        {"defect_count": (10, 1), "opportunity_count": (10, 1)},
    )

    resolved = resolve_metric(
        "defects-per-million-opportunities",
        authority=PinnedAuthority(_SYSTEM_ID),
    )

    assert resolved.value is None
    assert resolved.status == "invariant_violation"
    assert resolved.detail == "value outside declared valid range"
    assert resolved.lineage.valid_range == (0.0, 1_000_000.0)
    assert resolved.lineage.max_exclusive is True
    assert resolved.lineage.invariant_violation.offending_value == 1_000_000.0

    payload = mr._resolve_metric_payload(
        ResolveMetricRequest(metric_id="defects-per-million-opportunities")
    )
    assert payload["value"] is None
    assert payload["status"] == "invariant_violation"
    assert payload["detail"] == "value outside declared valid range"
    assert payload["lineage"]["valid_range"] == [0.0, 1_000_000.0]
    assert payload["lineage"]["max_exclusive"] is True
    assert payload["lineage"]["invariant_violation"]["offending_value"] == 1_000_000.0


def test_dpmo_zero_with_samples_is_visible_at_inclusive_lower_bound(monkeypatch):
    metrics = _ratio_metric(
        valid_range=(0.0, 1_000_000.0),
        max_exclusive=True,
    )
    _wire_metrics(
        monkeypatch,
        metrics,
        {"defect_count": (0, 1), "opportunity_count": (10, 1)},
    )

    resolved = resolve_metric(
        "defects-per-million-opportunities",
        authority=PinnedAuthority(_SYSTEM_ID),
    )

    assert resolved.value == 0
    assert resolved.sample_count is None
    assert resolved.status is None
    assert resolved.lineage.invariant_violation is None


def test_denominator_provenance_mismatch_returns_cannot_compute(monkeypatch):
    metrics = _ratio_metric(denominator_concept="runtime-hours")
    _wire_metrics(
        monkeypatch,
        metrics,
        {"defect_count": (5, 1), "runtime_total": (20, 1)},
    )

    resolved = resolve_metric(
        "defects-per-million-opportunities",
        authority=PinnedAuthority(_SYSTEM_ID),
    )

    expected = (
        "cannot compute: denominator undefined — declared basis "
        "'actual-quantity' not satisfied"
    )
    assert resolved.value is None
    assert resolved.status == "invariant_violation"
    assert resolved.detail == expected
    assert resolved.lineage.denominator_provenance == "actual-quantity"
    assert (
        resolved.lineage.invariant_violation.observed_denominator_concept
        == "runtime-hours"
    )


def test_declared_denominator_forces_live_path_despite_stored_composite(monkeypatch):
    numerator, denominator, ratio = _ratio_metric(
        denominator_concept="runtime-hours",
        prefer_stored="stored-defect-rate",
    )
    stored = DirectMetric(
        id="stored-defect-rate",
        concept="stored-composite",
        aggregation=Aggregation.SUM,
        label="Stored defect rate",
        unit="events/million",
    )
    _, queried_columns = _wire_metrics(
        monkeypatch,
        (numerator, denominator, stored, ratio),
        {
            "defect_count": (5, 1),
            "runtime_total": (20, 1),
            "stored_score": (250_000, 1),
        },
    )

    resolved = resolve_metric(
        "defects-per-million-opportunities",
        authority=PinnedAuthority(_SYSTEM_ID),
    )

    assert resolved.value is None
    assert resolved.status == "invariant_violation"
    assert resolved.lineage.prefer_stored_hit is False
    assert (
        resolved.lineage.invariant_violation.observed_denominator_concept
        == "runtime-hours"
    )
    assert queried_columns == ["defect_count", "runtime_total"]
    assert "stored_score" not in queried_columns


def test_undeclared_metric_result_and_lineage_shapes_stay_unchanged(monkeypatch):
    metric = DirectMetric(
        id="plain-count",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Plain count",
        unit="units",
    )
    _wire_metrics(monkeypatch, [metric], {"opportunity_count": (7, 2)})

    resolved = resolve_metric("plain-count", authority=PinnedAuthority(_SYSTEM_ID))
    lineage = _lineage_to_dict(resolved.lineage)
    payload = mr._resolve_metric_payload(ResolveMetricRequest(metric_id="plain-count"))

    assert resolved.value == 7
    assert resolved.status is None
    assert set(lineage) == {
        "metric_id", "kind", "concept", "authority", "mapping", "query_plan",
        "sql", "executed_at", "op", "inputs", "prefer_stored_hit",
    }
    assert set(payload) == {
        "metric_id", "value", "unit", "label", "system_id", "query_plan",
        "lineage",
    }


def test_direct_metric_at_exclusive_bound_withholds_value(monkeypatch):
    metric = DirectMetric(
        id="bounded-count",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Bounded count",
        unit="units",
        valid_range=(0.0, 100.0),
        max_exclusive=True,
    )
    _wire_metrics(monkeypatch, [metric], {"opportunity_count": (100, 4)})

    resolved = resolve_metric("bounded-count", authority=PinnedAuthority(_SYSTEM_ID))

    assert resolved.value is None
    assert resolved.sample_count == 4
    assert resolved.status == "invariant_violation"
    assert resolved.lineage.invariant_violation.offending_value == 100


def test_dea_100_is_visible_at_default_inclusive_upper_bound(monkeypatch):
    metric = DirectMetric(
        id="data-envelopment-analysis-score",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Data envelopment analysis score",
        unit="percent",
        valid_range=(0.0, 100.0),
    )
    _wire_metrics(monkeypatch, [metric], {"opportunity_count": (100, 4)})

    resolved = resolve_metric(
        "data-envelopment-analysis-score",
        authority=PinnedAuthority(_SYSTEM_ID),
    )

    assert resolved.value == 100
    assert resolved.sample_count == 4
    assert resolved.status is None
    assert resolved.lineage.invariant_violation is None


def test_real_zero_with_samples_remains_visible(monkeypatch):
    metric = DirectMetric(
        id="bounded-zero",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Bounded zero",
        unit="units",
        valid_range=(-1.0, 1.0),
    )
    _wire_metrics(monkeypatch, [metric], {"opportunity_count": (0, 3)})

    resolved = resolve_metric("bounded-zero", authority=PinnedAuthority(_SYSTEM_ID))

    assert resolved.value == 0
    assert resolved.sample_count == 3
    assert resolved.status is None
    assert resolved.lineage.invariant_violation is None
