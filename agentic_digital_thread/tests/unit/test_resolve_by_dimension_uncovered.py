# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests: coverage gaps classify as ``status: "uncovered"``.

A resolution failure caused by a MAPPING GAP (concept not mapped by any / the
authoritative onboarded system — the normal state mid-onboarding) must reach
the dashboard as the honest needs-data status, NOT ``status: "error"`` — the
panels render error styling (red + Retry + raw internals) only for genuine
faults. Pins:

  • ``CoverageGapError`` subclasses ``MetricResolverError`` (existing broad
    handlers keep working) and carries the missing concept id;
  • the by-dimension route classifies it into the ``uncovered`` envelope
    (missing_concepts + detail; empty rows) while other resolver errors keep
    the historical ``error`` envelope;
  • the two gap raise sites raise the subtype: no-systems-map-concept
    (authority resolution) and no-MAPS_TO-for-dimension.
"""
from __future__ import annotations

import asyncio

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_resolver import (
    CoverageGapError,
    MetricResolverError,
    resolve_metric_by_dimension_route,
)
from agents.models import ResolveMetricByDimensionRequest


def _call(monkeypatch, exc: Exception) -> dict:
    def _raise(*a, **k):
        raise exc
    monkeypatch.setattr(md, "resolve_metric_by_dimension", _raise)
    body = ResolveMetricByDimensionRequest(
        metric_id="quality-rate", dimension_concept="production-line"
    )
    return asyncio.run(resolve_metric_by_dimension_route(body))


def test_coverage_gap_is_a_metric_resolver_error():
    assert issubclass(CoverageGapError, MetricResolverError)
    e = CoverageGapError("no MAPS_TO", concept="production-line")
    assert e.concept == "production-line"


def test_route_classifies_coverage_gap_as_uncovered(monkeypatch):
    payload = _call(
        monkeypatch,
        CoverageGapError(
            "no MAPS_TO field for dimension concept 'production-line' in system 'erp'",
            concept="production-line",
        ),
    )
    assert payload["status"] == "uncovered"
    assert payload["missing_concepts"] == ["production-line"]
    assert "no MAPS_TO" in payload["detail"]
    assert payload["rows"] == [] and payload["kind"] == "bar"
    # the error key is reserved for genuine faults
    assert "error" not in payload


def test_route_keeps_error_envelope_for_genuine_faults(monkeypatch):
    payload = _call(monkeypatch, MetricResolverError("join composition failed"))
    assert payload["status"] == "error"
    assert "join composition failed" in payload["error"]


def test_authority_gap_raises_coverage_subtype(monkeypatch):
    """0 systems map the concept → CoverageGapError (mid-onboarding state);
    ambiguity (2+ systems, no rule) stays a genuine MetricResolverError."""
    monkeypatch.setattr(mr, "neptune_query", lambda *a, **k: [])
    try:
        mr._resolve_authority("quality-yield-rate", None)
        raise AssertionError("expected CoverageGapError")
    except CoverageGapError as e:
        assert e.concept == "quality-yield-rate"

    rows = [{"sid": "mes-mil"}, {"sid": "mes-det"}]
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda q, p=None, *a, **k: [] if "AuthorityRule" in q else rows,
    )
    try:
        mr._resolve_authority("quality-yield-rate", None)
        raise AssertionError("expected MetricResolverError")
    except CoverageGapError:
        raise AssertionError("ambiguity must NOT classify as a coverage gap")
    except MetricResolverError as e:
        assert "set_authority" in str(e)


def test_missing_field_mapping_raises_coverage_subtype(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", lambda *a, **k: [])
    try:
        mr._resolve_table_column("erp", "lifecycle-state")
        raise AssertionError("expected CoverageGapError")
    except CoverageGapError as e:
        assert e.concept == "lifecycle-state"
