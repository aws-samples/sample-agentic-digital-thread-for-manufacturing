# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the S1.1 metric layer (ADR 0014).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import agents.metric_layer
from agents.catalog import CORE_CONCEPTS
from agents.metric_layer import (
    Aggregation,
    CodeMetricLayer,
    ConceptRef,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricKind,
    MetricLayer,
    MetricLayerError,
    MetricRef,
    Scalar,
    get_metric_layer,
    validate_metric_layer,
)


def test_direct_metric_shape():
    m = DirectMetric(
        id="m1", concept="inventory-level", aggregation=Aggregation.SUM,
        label="L", unit="units",
    )
    assert m.kind is MetricKind.DIRECT
    assert m.id == "m1"
    assert m.concept == "inventory-level"
    assert m.aggregation is Aggregation.SUM
    assert m.label == "L"
    assert m.unit == "units"
    # three seams default to None
    assert m.group_by is None
    assert m.filter is None
    assert m.window is None
    # immutable (frozen)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.unit = "x"  # type: ignore[misc]


def test_aggregation_vocabulary():
    assert {a.value for a in Aggregation} == {
        "sum", "avg", "count", "min", "max", "latest",
    }


def test_formula_op_vocabulary():
    assert {o.value for o in FormulaOp} == {
        "product", "ratio", "sum", "weighted_sum", "scale",
    }


def test_optree_shape_and_serializable():
    formula = Formula(
        FormulaOp.PRODUCT,
        (
            ConceptRef("availability"),
            ConceptRef("performance"),
            ConceptRef("quality-metric"),
        ),
    )
    assert formula.op is FormulaOp.PRODUCT
    assert len(formula.inputs) == 3
    assert all(isinstance(i, ConceptRef) for i in formula.inputs)

    # serializes with stdlib only — no custom encoder
    as_dict = dataclasses.asdict(formula)
    assert isinstance(as_dict, dict)
    assert json.dumps(as_dict)  # does not raise

    # the tree recurses: nest a Formula inside inputs
    nested = Formula(
        FormulaOp.RATIO,
        (MetricRef("a"), Formula(FormulaOp.SUM, (Scalar(1.0), ConceptRef("performance")))),
    )
    assert isinstance(nested.inputs[1], Formula)
    assert json.dumps(dataclasses.asdict(nested))  # nested tree serializes too

    # a full DerivedMetric carrying prefer_stored must serialize too — locks the
    # new ADR 0016 Decision C field into the JSON contract (dataclasses.asdict).
    derived = DerivedMetric(
        id="oee", label="Overall Equipment Effectiveness", unit="ratio",
        formula=Formula(
            FormulaOp.PRODUCT,
            (
                MetricRef("availability"),
                MetricRef("performance"),
                MetricRef("quality-rate"),
            ),
        ),
        prefer_stored="oee-score",
    )
    derived_dict = dataclasses.asdict(derived)
    assert derived_dict["prefer_stored"] == "oee-score"
    assert json.dumps(derived_dict)  # DerivedMetric incl. prefer_stored serializes


def test_derived_metric_shape():
    # Mirrors the seed `oee` (ADR 0016): a MetricRef PRODUCT over the three input
    # Metrics with the prefer_stored hybrid shortcut.
    m = DerivedMetric(
        id="oee", label="Overall Equipment Effectiveness", unit="ratio",
        formula=Formula(
            FormulaOp.PRODUCT,
            (
                MetricRef("availability"),
                MetricRef("performance"),
                MetricRef("quality-rate"),
            ),
        ),
        prefer_stored="oee-score",
    )
    assert m.kind is MetricKind.DERIVED
    assert isinstance(m.formula, Formula)
    assert m.formula.op is FormulaOp.PRODUCT
    assert all(isinstance(i, MetricRef) for i in m.formula.inputs)
    assert m.prefer_stored == "oee-score"


def test_get_metric_layer_is_default_and_memoized():
    layer = get_metric_layer()
    assert isinstance(layer, MetricLayer)
    assert isinstance(layer, CodeMetricLayer)
    # memoized via lru_cache → same instance
    assert get_metric_layer() is layer


def test_seed_registry_contains_tracer_and_oee():
    ids = {m.id for m in get_metric_layer().list_metrics()}
    assert "spare-parts-on-hand" in ids
    assert "oee" in ids
    tracer = get_metric_layer().get_metric("spare-parts-on-hand")
    assert tracer.concept == "mro-inventory"
    assert tracer.concept in CORE_CONCEPTS


def test_seed_oee_is_normalized_product_with_prefer_stored():
    # Task 1A: the seed `oee` is a documented-normalization op-tree —
    # SCALE(PRODUCT(availability, performance, SCALE(oee-quality, 0.01)), 100) —
    # so the DERIVED value is a true A×P×Q percentage: A/P are 0-1 ratios,
    # oee-quality is 0-100 (normalized to 0-1 by the inner guard), and the outer
    # SCALE presents the product on 0-100. prefer_stored="oee-score" stays.
    oee = get_metric_layer().get_metric("oee")
    assert isinstance(oee, DerivedMetric)
    assert oee.prefer_stored == "oee-score"
    # outer: SCALE(product, 100) → 0-100 presentation
    assert oee.formula.op is FormulaOp.SCALE
    product, out_scale = oee.formula.inputs
    assert isinstance(out_scale, Scalar) and out_scale.value == 100.0
    # inner: PRODUCT over A, P, and the 0-1-normalized quality
    assert isinstance(product, Formula) and product.op is FormulaOp.PRODUCT
    a, p, q_norm = product.inputs
    assert a == MetricRef("availability")
    assert p == MetricRef("performance")
    # quality guard: SCALE(oee-quality, 0.01) — 0-100 → 0-1
    assert isinstance(q_norm, Formula) and q_norm.op is FormulaOp.SCALE
    assert q_norm.inputs == (MetricRef("oee-quality"), Scalar(0.01))


def test_seed_oee_quality_is_prefer_stored_alias_of_quality_yield_rate():
    # Task 1A: `oee-quality` prefers the stored `quality-yield-rate` (the 0-100
    # first-pass yield, deployed-verified) and keeps the good/actual RATIO yield
    # only as the derive fallback. Label is the sub-reading contract "Quality".
    layer = get_metric_layer()
    q = layer.get_metric("oee-quality")
    assert isinstance(q, DerivedMetric)
    assert q.prefer_stored == "quality-yield-rate"
    assert q.label == "Quality"
    # satisfiable via the stored path ALONE (quality-yield-rate covered) …
    assert layer.is_satisfiable(q, frozenset({"quality-yield-rate"})) is True
    # … or via the fallback inputs path (good-count + actual-quantity) …
    assert layer.is_satisfiable(
        q, frozenset({"quality-metric", "actual-quantity"})) is True
    # … but not with neither.
    assert layer.is_satisfiable(q, frozenset()) is False
    # prefer_stored counts as a dependency: correcting quality-yield-rate
    # re-derives oee-quality AND the oee gauge composite.
    assert layer.metrics_for_concept("quality-yield-rate") == frozenset(
        {"quality-yield-rate", "oee-quality", "oee"})


def test_seed_ot_native_metrics_resolve_their_concept():
    # The four abstract OT-native metrics are DIRECT metrics over CORE concepts
    # the onboarded systems commonly cover. Each resolves to its own concept
    # (id == concept) with the seeded aggregation + unit, is a valid CORE concept,
    # and is satisfiable exactly when that concept is covered.
    layer = get_metric_layer()
    expected = {
        "throughput": (Aggregation.AVG, "units/interval"),
        "machine-speed": (Aggregation.AVG, "units/min"),
        "scrap-waste": (Aggregation.SUM, "units"),
        "runtime-hours": (Aggregation.SUM, "hours"),
    }
    for metric_id, (aggregation, unit) in expected.items():
        m = layer.get_metric(metric_id)
        assert isinstance(m, DirectMetric)
        assert m.concept == metric_id            # concept id == metric id
        assert m.concept in CORE_CONCEPTS        # a real core concept
        assert m.aggregation is aggregation
        assert m.unit == unit
        # satisfiable iff its single concept is covered
        assert layer.is_satisfiable(m, frozenset({metric_id})) is True
        assert layer.is_satisfiable(m, frozenset()) is False
        # Each OT metric is the sole metric for its concept — EXCEPT `actual-quantity`,
        # which is the denominator term of the OEE quality yield
        # (`oee-quality`, referenced by the derived `oee`), so its closure includes
        # those two derived metrics too, and `machine-speed`, which is the
        # numerator term of the derived `performance-ratio` (actual vs target).
        if metric_id == "actual-quantity":
            assert layer.metrics_for_concept(metric_id) == frozenset(
                {"actual-quantity", "oee-quality", "oee"})
        elif metric_id == "machine-speed":
            assert layer.metrics_for_concept(metric_id) == frozenset(
                {"machine-speed", "performance-ratio"})
        else:
            assert layer.metrics_for_concept(metric_id) == frozenset({metric_id})


def test_ot_native_metrics_pass_validation():
    # The seed layer (which now includes the OT-native metrics) validates: every
    # concept ref resolves against CORE_CONCEPTS.
    assert validate_metric_layer() is None


def test_seed_machine_state_distribution_metric():
    # PART B: the machine-state distribution measure — a COUNT over the
    # `machine-state` concept, resolved GROUP BY the machine-state dimension by
    # the machine-state BAR card (count of records per current state). Satisfiable
    # exactly when `machine-state` is covered.
    layer = get_metric_layer()
    m = layer.get_metric("machine-state-distribution")
    assert isinstance(m, DirectMetric)
    assert m.concept == "machine-state"
    assert m.concept in CORE_CONCEPTS
    assert m.aggregation is Aggregation.COUNT
    assert m.unit == "machines"
    assert layer.is_satisfiable(m, frozenset({"machine-state"})) is True
    assert layer.is_satisfiable(m, frozenset()) is False
    # `machine-state` now backs two measures: the COUNT distribution (dashboard panel) and
    # the LATEST per-asset state (S5.4 click-to-drill "which machines"), both direct.
    assert layer.metrics_for_concept("machine-state") == frozenset(
        {"machine-state-distribution", "machine-state-latest"})
    latest = layer.get_metric("machine-state-latest")
    assert isinstance(latest, DirectMetric)
    assert latest.concept == "machine-state"
    assert latest.aggregation is Aggregation.LATEST


def test_seed_contains_oee_input_metrics():
    # The four direct Metrics the derived `oee` depends on, with the concept /
    # aggregation / unit each was seeded with (ADR 0016 Decision A; AVG per Flag 3).
    layer = get_metric_layer()
    expected = {
        "availability": ("availability", Aggregation.AVG, "ratio"),
        "performance": ("performance", Aggregation.AVG, "ratio"),
        "quality-rate": ("quality-metric", Aggregation.AVG, "ratio"),
        "oee-score": ("oee-score", Aggregation.AVG, "ratio"),
    }
    for metric_id, (concept, aggregation, unit) in expected.items():
        m = layer.get_metric(metric_id)
        assert isinstance(m, DirectMetric)
        assert m.concept == concept
        assert m.aggregation is aggregation
        assert m.unit == unit


def test_get_metric_known_and_unknown():
    layer = get_metric_layer()
    tracer = layer.get_metric("spare-parts-on-hand")
    assert isinstance(tracer, DirectMetric)
    with pytest.raises(KeyError):
        layer.get_metric("does-not-exist")


def test_validate_happy_path():
    assert validate_metric_layer() is None


def test_validate_bad_concept_raises():
    layer = CodeMetricLayer([
        DirectMetric(
            id="broken-direct", concept="not-a-real-concept",
            aggregation=Aggregation.SUM, label="x", unit="x",
        ),
    ])
    with pytest.raises(MetricLayerError, match="broken-direct"):
        validate_metric_layer(layer)


def test_validate_bad_derived_input_raises():
    # bad ConceptRef
    bad_concept = CodeMetricLayer([
        DerivedMetric(
            id="broken-derived", label="x", unit="x",
            formula=Formula(FormulaOp.PRODUCT, (ConceptRef("nope-concept"),)),
        ),
    ])
    with pytest.raises(MetricLayerError, match="broken-derived"):
        validate_metric_layer(bad_concept)

    # bad MetricRef
    bad_metric = CodeMetricLayer([
        DerivedMetric(
            id="broken-derived", label="x", unit="x",
            formula=Formula(FormulaOp.SUM, (MetricRef("nope-metric"),)),
        ),
    ])
    with pytest.raises(MetricLayerError, match="broken-derived"):
        validate_metric_layer(bad_metric)


def test_validate_duplicate_ids_raises():
    # CodeMetricLayer dedups by id at construction; a non-deduping layer surfaces
    # the duplicate so validate_metric_layer's duplicate guard is exercised.
    class _DupLayer(MetricLayer):
        def __init__(self, metrics):
            self._metrics = list(metrics)

        def get_metric(self, metric_id):
            for m in self._metrics:
                if m.id == metric_id:
                    return m
            raise KeyError(metric_id)

        def list_metrics(self):
            return list(self._metrics)

    layer = _DupLayer([
        DirectMetric(
            id="dup-id", concept="inventory-level", aggregation=Aggregation.SUM,
            label="a", unit="units",
        ),
        DirectMetric(
            id="dup-id", concept="inventory-level", aggregation=Aggregation.AVG,
            label="b", unit="units",
        ),
    ])
    with pytest.raises(MetricLayerError, match="dup-id"):
        validate_metric_layer(layer)


def test_module_is_import_pure():
    # No DB/LLM/I-O symbol leaked into the module namespace (ADR 0001). The real
    # guarantee is that the source imports nothing with I/O.
    assert not hasattr(agents.metric_layer, "boto3")
    assert not hasattr(agents.metric_layer, "neptune_query")


# --------------------------------------------------------------------------
# metrics_for_concept reverse index (S3.2 / W1.2) — concept → closure of
# Metric ids that depend on it (directly, via MetricRef, or via prefer_stored).
# Uses the REAL seed concept ids.
# --------------------------------------------------------------------------

def test_metrics_for_concept_oee_input_includes_oee():
    # `availability` is an OEE input concept (direct metric `availability` +
    # the derived `oee` MetricRef-PRODUCT). Its closure includes both.
    layer = get_metric_layer()
    assert layer.metrics_for_concept("availability") == frozenset({"availability", "oee"})
    assert "oee" in layer.metrics_for_concept("performance")
    # `quality-metric` feeds the direct `quality-rate` (quality-by-line bar) AND the
    # good-count term `quality-good-count` of the derived `oee-quality` yield, which
    # the derived `oee` references → closure includes both derived metrics + `oee`.
    assert layer.metrics_for_concept("quality-metric") == frozenset(
        {"quality-rate", "quality-good-count", "oee-quality", "oee"})


def test_metrics_for_concept_prefer_stored_input_counts():
    # ADR 0005 FLAG: a DerivedMetric's prefer_stored input is a dependency, so
    # correcting the stored OEE column (`oee-score` concept) re-derives `oee`.
    layer = get_metric_layer()
    assert layer.metrics_for_concept("oee-score") == frozenset({"oee-score", "oee"})


def test_metrics_for_concept_mro_inventory():
    layer = get_metric_layer()
    assert layer.metrics_for_concept("mro-inventory") == frozenset({"spare-parts-on-hand"})


def test_metrics_for_concept_unknown_is_empty_frozenset():
    layer = get_metric_layer()
    result = layer.metrics_for_concept("does-not-exist")
    assert result == frozenset()
    assert isinstance(result, frozenset)


def test_metrics_for_concept_cyclic_layer_terminates():
    # A synthetic CYCLIC layer (a→b→a) must terminate via the `seen` guard.
    # Both derived metrics close over the direct concept `c1`.
    cyclic = CodeMetricLayer([
        DirectMetric(
            id="d1", concept="c1", aggregation=Aggregation.SUM, label="D1", unit="u",
        ),
        DerivedMetric(
            id="a", label="A", unit="u",
            formula=Formula(FormulaOp.SUM, (MetricRef("b"),)),
        ),
        DerivedMetric(
            id="b", label="B", unit="u",
            formula=Formula(FormulaOp.SUM, (MetricRef("a"), ConceptRef("c1"))),
        ),
    ])
    # Does not hang / recurse infinitely; both cyclic metrics reach `c1`.
    assert cyclic.metrics_for_concept("c1") == frozenset({"d1", "a", "b"})
    # A concept reached by nothing → empty.
    assert cyclic.metrics_for_concept("nope") == frozenset()


def test_metrics_for_concept_concrete_on_abc():
    # The ABC method is CONCRETE now — a minimal subclass implementing only the
    # two abstract methods inherits a working metrics_for_concept (mirrors the
    # _DupLayer pattern; no NotImplementedError).
    class _MinLayer(MetricLayer):
        def __init__(self, metrics):
            self._m = list(metrics)

        def get_metric(self, metric_id):
            for m in self._m:
                if m.id == metric_id:
                    return m
            raise KeyError(metric_id)

        def list_metrics(self):
            return list(self._m)

    layer = _MinLayer([
        DirectMetric(id="x", concept="cX", aggregation=Aggregation.SUM, label="X", unit="u"),
    ])
    assert layer.metrics_for_concept("cX") == frozenset({"x"})
    assert layer.metrics_for_concept("cY") == frozenset()


# --------------------------------------------------------------------------
# Resolver-mirroring satisfiability + cheapest-path missing_concepts (S4.2 W1).
# Offline: `covered` injected as a frozenset (no graph/DB). Uses the REAL seed
# metric ids / concepts:
#   spare-parts-on-hand → concept "mro-inventory" (direct)
#   oee (derived) → PRODUCT(availability, performance, quality-rate) over the
#     concepts {availability, performance, quality-metric};
#     prefer_stored="oee-score" → stored concept "oee-score".
# ADR 0016: direct → concept covered; derived(no prefer_stored) → ALL inputs;
#   derived(prefer_stored) → stored OR all inputs (NOT the flat _concepts_of AND).
# --------------------------------------------------------------------------

# The OEE input *concepts* (oee-quality needs quality-metric + actual-quantity)
_OEE_INPUTS = frozenset({"availability", "performance", "quality-metric", "actual-quantity"})
_OEE_STORED = frozenset({"oee-score"})


def test_is_satisfiable_direct_in_and_out():
    layer = get_metric_layer()
    sp = layer.get_metric("spare-parts-on-hand")
    assert layer.is_satisfiable(sp, frozenset({"mro-inventory"})) is True
    assert layer.is_satisfiable(sp, frozenset()) is False
    assert layer.is_satisfiable(sp, frozenset({"something-else"})) is False


def test_is_satisfiable_derived_no_prefer_stored_and_over_inputs():
    # A derived metric WITHOUT prefer_stored requires ALL (recursive) input
    # concepts. Build oee-like derived over the seed input MetricRefs.
    layer = CodeMetricLayer([
        DirectMetric(id="availability", concept="availability",
                     aggregation=Aggregation.AVG, label="A", unit="ratio"),
        DirectMetric(id="performance", concept="performance",
                     aggregation=Aggregation.AVG, label="P", unit="ratio"),
        DirectMetric(id="quality-rate", concept="quality-metric",
                     aggregation=Aggregation.AVG, label="Q", unit="ratio"),
        DerivedMetric(
            id="oee-nostore", label="OEE no store", unit="ratio",
            formula=Formula(FormulaOp.PRODUCT, (
                MetricRef("availability"), MetricRef("performance"), MetricRef("quality-rate"),
            )),
        ),
    ])
    m = layer.get_metric("oee-nostore")
    assert layer.is_satisfiable(m, _OEE_INPUTS) is True
    # Missing any single input → not satisfiable (AND over inputs).
    assert layer.is_satisfiable(m, frozenset({"availability", "performance"})) is False
    assert layer.is_satisfiable(m, frozenset()) is False


def test_is_satisfiable_recursive_nested_metricref_requires_all_leaves():
    # A nested-MetricRef formula requires ALL leaf concepts (recursive AND).
    layer = CodeMetricLayer([
        DirectMetric(id="d1", concept="c1", aggregation=Aggregation.SUM, label="D1", unit="u"),
        DirectMetric(id="d2", concept="c2", aggregation=Aggregation.SUM, label="D2", unit="u"),
        DerivedMetric(id="inner", label="inner", unit="u",
                      formula=Formula(FormulaOp.SUM, (MetricRef("d1"), ConceptRef("c2")))),
        DerivedMetric(id="outer", label="outer", unit="u",
                      formula=Formula(FormulaOp.RATIO, (MetricRef("inner"), Scalar(2.0)))),
    ])
    outer = layer.get_metric("outer")
    assert layer.is_satisfiable(outer, frozenset({"c1", "c2"})) is True
    assert layer.is_satisfiable(outer, frozenset({"c1"})) is False
    assert layer.is_satisfiable(outer, frozenset({"c2"})) is False


def test_is_satisfiable_prefer_stored_or_branch():
    # The seed `oee` hybrid: stored OR inputs (proves the flat _concepts_of union
    # — which requires BOTH stored AND inputs — would FALSELY gate it).
    layer = get_metric_layer()
    oee = layer.get_metric("oee")
    # stored-only covered → satisfiable (stored path)
    assert layer.is_satisfiable(oee, _OEE_STORED) is True
    # inputs-only covered → satisfiable (inputs path)
    assert layer.is_satisfiable(oee, _OEE_INPUTS) is True
    # neither → not satisfiable
    assert layer.is_satisfiable(oee, frozenset()) is False
    # both → satisfiable
    assert layer.is_satisfiable(oee, _OEE_INPUTS | _OEE_STORED) is True
    # partial inputs and no stored → not satisfiable
    assert layer.is_satisfiable(oee, frozenset({"availability", "performance"})) is False


def test_missing_concepts_direct():
    layer = get_metric_layer()
    sp = layer.get_metric("spare-parts-on-hand")
    assert layer.missing_concepts(sp, frozenset()) == frozenset({"mro-inventory"})
    assert layer.missing_concepts(sp, frozenset({"mro-inventory"})) == frozenset()


def test_missing_concepts_oee_cheapest_path():
    # oee empty-covered → CHEAPEST unmet path is the single stored concept
    # {"oee-score"} (len 1), NOT the 3 inputs (len 3).
    layer = get_metric_layer()
    oee = layer.get_metric("oee")
    assert layer.missing_concepts(oee, frozenset()) == frozenset({"oee-score"})
    # each path individually covered → ∅
    assert layer.missing_concepts(oee, _OEE_STORED) == frozenset()
    assert layer.missing_concepts(oee, _OEE_INPUTS) == frozenset()


def test_missing_concepts_prefer_stored_ties_prefer_inputs():
    # When stored and inputs paths have EQUAL unmet count, prefer the inputs path.
    layer = CodeMetricLayer([
        DirectMetric(id="in1", concept="cin1", aggregation=Aggregation.AVG, label="I1", unit="u"),
        DirectMetric(id="stored1", concept="cstored", aggregation=Aggregation.AVG, label="S", unit="u"),
        DerivedMetric(
            id="hybrid", label="hybrid", unit="u",
            formula=Formula(FormulaOp.PRODUCT, (MetricRef("in1"),)),
            prefer_stored="stored1",
        ),
    ])
    m = layer.get_metric("hybrid")
    # both paths len 1 unmet → tie → inputs path → {"cin1"}
    assert layer.missing_concepts(m, frozenset()) == frozenset({"cin1"})


def test_satisfiable_iff_missing_empty_invariant():
    # is_satisfiable(m, c) is True  <=>  missing_concepts(m, c) == frozenset()
    # across every seed metric × representative covered sets.
    layer = get_metric_layer()
    covered_sets = [
        frozenset(),
        frozenset({"mro-inventory"}),
        _OEE_STORED,
        _OEE_INPUTS,
        _OEE_INPUTS | _OEE_STORED,
        frozenset({"availability"}),
        frozenset({"availability", "performance"}),
    ]
    for m in layer.list_metrics():
        for c in covered_sets:
            sat = layer.is_satisfiable(m, c)
            missing = layer.missing_concepts(m, c)
            assert (sat is True) == (missing == frozenset()), (m.id, c, sat, missing)
            assert isinstance(missing, frozenset)


def test_satisfiability_cyclic_layer_terminates():
    # The `seen` cycle guard makes a→b→a terminate (mirror _walk_metric).
    cyclic = CodeMetricLayer([
        DirectMetric(id="d1", concept="c1", aggregation=Aggregation.SUM, label="D1", unit="u"),
        DerivedMetric(id="a", label="A", unit="u",
                      formula=Formula(FormulaOp.SUM, (MetricRef("b"),))),
        DerivedMetric(id="b", label="B", unit="u",
                      formula=Formula(FormulaOp.SUM, (MetricRef("a"), ConceptRef("c1")))),
    ])
    a = cyclic.get_metric("a")
    # Does not hang; with c1 covered the only real concept is met.
    assert cyclic.is_satisfiable(a, frozenset({"c1"})) is True
    assert cyclic.missing_concepts(a, frozenset({"c1"})) == frozenset()
    assert cyclic.is_satisfiable(a, frozenset()) is False
    assert cyclic.missing_concepts(a, frozenset()) == frozenset({"c1"})


def test_satisfiability_concrete_on_abc():
    # The ABC methods are CONCRETE — a minimal subclass implementing only the two
    # abstract methods inherits working is_satisfiable/missing_concepts.
    class _MinLayer(MetricLayer):
        def __init__(self, metrics):
            self._m = list(metrics)

        def get_metric(self, metric_id):
            for m in self._m:
                if m.id == metric_id:
                    return m
            raise KeyError(metric_id)

        def list_metrics(self):
            return list(self._m)

    layer = _MinLayer([
        DirectMetric(id="x", concept="cX", aggregation=Aggregation.SUM, label="X", unit="u"),
    ])
    x = layer.get_metric("x")
    assert layer.is_satisfiable(x, frozenset({"cX"})) is True
    assert layer.is_satisfiable(x, frozenset()) is False
    assert layer.missing_concepts(x, frozenset()) == frozenset({"cX"})


# --- ADR-0075: DirectMetric.filter (row predicates over concepts) ---

from agents.metric_layer import Predicate, PredicateOp  # noqa: E402


def _filtered(**overrides):
    kwargs = dict(
        id="filtered-inspections", concept="inspection-result",
        aggregation=Aggregation.COUNT, label="F", unit="inspections",
        filter=(Predicate("shift", PredicateOp.EQ, ("NIGHT",)),),
    )
    kwargs.update(overrides)
    return DirectMetric(**kwargs)


def test_seed_metrics_declare_no_filter():
    assert all(getattr(m, "filter", None) is None for m in CodeMetricLayer().list_metrics())


def test_filtered_metric_validates():
    assert validate_metric_layer(CodeMetricLayer([_filtered()])) is None


@pytest.mark.parametrize("overrides,match", [
    ({"filter": (Predicate("not-a-real-concept", PredicateOp.IS_NULL),)},
     "not in CORE_CONCEPTS"),
    ({"filter": (Predicate("shift", PredicateOp.EQ, ("bad'lit",)),)},
     "filter\\[0\\]"),
    ({"filter": ()}, "non-empty"),
    ({"aggregation": Aggregation.LATEST}, "latest"),
])
def test_validate_rejects_malformed_filter(overrides, match):
    with pytest.raises(MetricLayerError, match=match):
        validate_metric_layer(CodeMetricLayer([_filtered(**overrides)]))


def test_predicate_concepts_join_the_closure():
    metric = _filtered()
    layer = CodeMetricLayer([metric])
    assert layer.metrics_for_concept("shift") == frozenset({"filtered-inspections"})
    assert layer.is_satisfiable(metric, frozenset({"inspection-result"})) is False
    assert layer.missing_concepts(metric, frozenset({"inspection-result"})) == \
        frozenset({"shift"})
    both = frozenset({"inspection-result", "shift"})
    assert layer.is_satisfiable(metric, both) is True
    assert layer.missing_concepts(metric, both) == frozenset()
    assert layer.missing_concepts(metric, frozenset()) == \
        frozenset({"inspection-result", "shift"})


def test_filter_less_closure_is_unchanged():
    metric = _filtered(filter=None)
    layer = CodeMetricLayer([metric])
    assert layer.metrics_for_concept("shift") == frozenset()
    assert layer.is_satisfiable(metric, frozenset({"inspection-result"})) is True
