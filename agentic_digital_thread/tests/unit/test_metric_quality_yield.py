# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the OEE quality sub-reading `oee-quality`.

Task 1A: the PREFERRED source is the stored `quality-yield-rate` concept (an AVG
over a 0-100 first-pass-yield column — single-table granularity, deployed-verified
92-94%), presented under the stable `oee-quality` id via the prefer_stored hybrid
shortcut. The good/actual RATIO yield remains ONLY as the derive fallback.

Regression guard for the "Quality reads 377.8" bug: `quality-metric` maps to a
good-COUNT column (e.g. work_orders.good_quantity), so a single-column AVG of it
is a units figure (~378), NOT a 0-100 quality. And regression guard for the
"Quality reads 2.84%" bug: the good/actual fallback as lifetime SUMs can pin
numerator and denominator to DIFFERENT systems/tables — which is why the
same-granularity stored yield is now the primary path.

Determinism-pure: the leaf direct resolves are stubbed so no DB/graph is touched.
"""
from __future__ import annotations

import pytest

from agents import metric_resolver as mr
from agents.metric_resolver import MetricResolverError, ResolvedMetric
from agents.metric_layer import (
    Aggregation,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricRef,
    Scalar,
    get_metric_layer,
)


# --- Seed shape: prefer_stored alias of quality-yield-rate; the good/actual
# --- SCALE(RATIO(good, actual), 100) yield stays as the derive FALLBACK ---

def test_oee_quality_is_weighted_yield_derived_metric():
    layer = get_metric_layer()

    good = layer.get_metric("quality-good-count")
    assert isinstance(good, DirectMetric)
    assert good.concept == "quality-metric"
    assert good.aggregation is Aggregation.SUM   # SUM(good), NOT a per-row rate

    q = layer.get_metric("oee-quality")
    assert isinstance(q, DerivedMetric)
    # Task 1A: the stored 0-100 yield is the preferred source; the sub-reading
    # label contract is "Quality" (frontend renders metric.label verbatim).
    assert q.prefer_stored == "quality-yield-rate"
    assert q.label == "Quality"
    # outer op multiplies the fraction by 100 → a 0-100 percentage
    assert q.formula.op is FormulaOp.SCALE
    ratio, scale = q.formula.inputs
    assert isinstance(scale, Scalar) and scale.value == 100.0
    # inner op is a RATIO good / actual (actual = good + reject)
    assert isinstance(ratio, Formula) and ratio.op is FormulaOp.RATIO
    numerator, denominator = ratio.inputs
    assert numerator == MetricRef("quality-good-count")
    # The denominator is now actual-quantity (direct metric ref, not a Formula SUM)
    assert denominator == MetricRef("actual-quantity")


def _patch_leaves(monkeypatch, good, actual):
    """Stub the two FALLBACK leaf resolves; delegate `oee-quality` to the real path.

    `quality-yield-rate` (the prefer_stored source) is deliberately ABSENT from
    the canned values, so its resolve raises MetricResolverError — the resolver
    swallows the stored miss and derives from the good/actual fallback formula,
    which is exactly the path under test here.
    """
    real_resolve = mr.resolve_metric
    # Now uses actual-quantity instead of scrap-waste (same table/granularity)
    values = {"quality-good-count": good, "actual-quantity": actual}

    def wrapper(metric_id, *, authority=None):
        if metric_id == "oee-quality":
            return real_resolve(metric_id, authority=authority)
        if metric_id not in values:
            raise MetricResolverError(f"unexpected ref {metric_id!r}")
        return ResolvedMetric(
            metric_id=metric_id, value=values[metric_id], unit="units",
            label=metric_id, query_plan=None, system_id="mes-det",
        )

    monkeypatch.setattr(mr, "resolve_metric", wrapper)


@pytest.mark.parametrize(
    "good, actual, expected",
    [
        (940, 1000, 94.0),        # typical MES first-pass yield (940 good / 1000 actual)
        (99, 100, 99.0),          # near-perfect
        (500, 1000, 50.0),        # half
        (0, 100, 0.0),            # all reject → 0, not negative/NaN
        (377810, 418574, pytest.approx(90.26, abs=0.5)),  # observed-magnitude sanity
    ],
)
def test_oee_quality_resolves_0_100(monkeypatch, good, actual, expected):
    _patch_leaves(monkeypatch, good, actual)
    rm = mr.resolve_metric("oee-quality")
    assert rm.value == expected
    # the invariant the 377.8 bug violated: quality is a 0-100 rate
    assert 0.0 <= rm.value <= 100.0


def test_oee_quality_never_exceeds_100(monkeypatch):
    # Whatever the (non-negative) good/actual counts, good/actual*100 is
    # bounded to [0, 100] — it can NEVER read 377.8 (an un-normalized count AVG).
    for good, actual in [(1, 1), (10_000, 10_003), (250, 500), (1, 10_000)]:
        _patch_leaves(monkeypatch, good, actual)
        v = mr.resolve_metric("oee-quality").value
        assert 0.0 <= v <= 100.0


# --- Task 1A acceptance: oee-quality tracks quality-yield-rate ----------------

def _patch_stored_yield(monkeypatch, stored_value, *, calls=None):
    """Stub quality-yield-rate (the prefer_stored source); delegate `oee-quality`
    to the real derived path. The fallback leaves are deliberately ABSENT so any
    fall-through would raise — proving the stored hit short-circuits."""
    real_resolve = mr.resolve_metric

    def wrapper(metric_id, *, authority=None):
        if metric_id == "oee-quality":
            return real_resolve(metric_id, authority=authority)
        if calls is not None:
            calls.append(metric_id)
        if metric_id != "quality-yield-rate":
            raise MetricResolverError(f"unexpected ref {metric_id!r}")
        return ResolvedMetric(
            metric_id=metric_id, value=stored_value, unit="ratio",
            label="Quality Yield Rate", query_plan=None, system_id="mes-det",
        )

    monkeypatch.setattr(mr, "resolve_metric", wrapper)


@pytest.mark.parametrize("stored", [92.6, 93.5, 94.0])
def test_oee_quality_resolves_to_same_value_as_quality_yield_rate(monkeypatch, stored):
    # Task 1A acceptance: under a mocked graph where quality-yield-rate resolves,
    # `oee-quality` presents the SAME value (prefer_stored hit) under its own
    # id/label — never the cross-table good/actual fallback.
    calls: list[str] = []
    _patch_stored_yield(monkeypatch, stored, calls=calls)

    rm = mr.resolve_metric("oee-quality")

    assert rm.value == stored
    assert rm.metric_id == "oee-quality"
    assert rm.label == "Quality"
    assert rm.lineage is not None and rm.lineage.prefer_stored_hit is True
    # the stored source was read exactly once; the fallback leaves never resolved
    assert calls == ["quality-yield-rate"]


def test_oee_quality_stored_miss_falls_back_to_yield(monkeypatch):
    # Stored source unavailable → derive from the good/actual fallback (the
    # pre-1A behavior, preserved for systems without quality-yield-rate mapped).
    _patch_leaves(monkeypatch, 940, 1000)
    rm = mr.resolve_metric("oee-quality")
    assert rm.value == 94.0
    assert rm.lineage is not None and rm.lineage.prefer_stored_hit is False
