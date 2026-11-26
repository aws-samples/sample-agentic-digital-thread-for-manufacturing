# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Metric layer (ADR 0002 / ADR 0014): the source of truth for *how a value is
defined*, separate from the graph (relationships) and the resolver (values).

A ``Metric`` is uniform (Decision A): either DIRECT (a concept + aggregation +
presentation seams) or DERIVED (a serializable op-tree formula over other
Metrics/concepts). The op-tree *type* is defined here; its evaluator is deferred
to S2.1 (Decision C). The layer is a definition provider behind the
``MetricLayer`` interface with a ``CodeMetricLayer`` default obtained via
``get_metric_layer()`` (Decision D, ADR 0008 seam). ``validate_metric_layer()``
asserts concept/metric refs resolve against ``agents.catalog.CORE_CONCEPTS``.

This module is import-pure (ADR 0001): no I/O, no LLM, no ``eval``/``exec``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import math
from typing import Iterable, Union

from .catalog import CORE_CONCEPTS
# ADR-0075 row-predicate types + the ONE shape/literal rule (import-pure
# sibling), re-exported so the metric-type surface stays one import.
from .metric_predicate_rule import (  # noqa: F401  (re-export, ADR-0075)
    MAX_EXACT_INTEGER,
    MAX_FILTER_CLAUSES,
    MAX_IN_VALUES,
    MAX_PREDICATE_LITERAL_LEN,
    Predicate,
    PredicateLiteral,
    PredicateOp,
    check_filter,
    check_predicate,
    check_predicate_literal,
)
# the duration measure + field refs + percentile aggregation types and the
# ONE closed rule for them (import-pure sibling), re-exported so the metric-type
# surface stays one import.
from .metric_measure_rule import (  # noqa: F401  (re-export)
    DURATION_UNITS,
    MAX_PERCENTILES,
    DurationMeasure,
    FieldRef,
    check_field_ref,
    check_measure,
    check_percentiles,
    classify_timestamp_type,
    dialect_is_approximate,
)


# Ratio metrics may declare ``denominator_provenance`` to bind the denominator
# to a specific concept. Definitions without that declaration retain their
# historical behavior and serialized shape.


# --- Fixed vocabularies (str mixin → JSON-serializable values for ADR 0008) ---

class MetricKind(str, Enum):
    DIRECT = "direct"
    DERIVED = "derived"


class Aggregation(str, Enum):
    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    LATEST = "latest"


class FormulaOp(str, Enum):
    PRODUCT = "product"
    RATIO = "ratio"
    SUM = "sum"
    WEIGHTED_SUM = "weighted_sum"
    SCALE = "scale"


# --- Op-tree TYPE only; evaluator → S2.1 ---
# A derived formula is a uniform, serializable {op, inputs[]} tree whose leaves
# are refs (Decision C). No evaluate()/numeric reduction lives here — that is
# S2.1. No eval/exec; closed vocabulary only (ADR 0001 determinism).

@dataclass(frozen=True)
class MetricRef:      # leaf: reference to another Metric by id
    metric: str


@dataclass(frozen=True)
class ConceptRef:     # leaf: reference to a direct concept by id
    concept: str


@dataclass(frozen=True)
class Scalar:         # leaf: literal constant (e.g. weight/factor)
    value: float


@dataclass(frozen=True)
class Formula:        # node: an op over inputs (leaves or nested Formulas)
    op: FormulaOp
    inputs: tuple["FormulaTerm", ...]


FormulaTerm = Union[MetricRef, ConceptRef, Scalar, "Formula"]


# --- Metric types (Decisions A + B) ---

@dataclass(frozen=True)
class DirectMetric:
    id: str                 # distinct from concept id (one concept → many Metrics)
    concept: str
    aggregation: Aggregation
    label: str
    unit: str
    group_by: str | None = None              # ADR 0014 seam — resolver-deferred (S2.x)
    filter: tuple[Predicate, ...] | None = None  # ADR-0075: row predicates via concept bindings
    window: str | None = None                # ADR 0014 seam — resolver-deferred (S2.x)
    valid_range: tuple[float, float] | None = None
    max_exclusive: bool = False
    # a DURATION measure (a percentile of the clamped, unit-scaled gap
    # between two timestamp field refs of the measure table) resolved as a
    # PERCENTILE, instead of aggregating the ``concept`` column. ``concept``
    # stays the authority / question-matching concept; ``measure`` supplies the
    # expression and ``percentiles`` the 1..5 p-values. Both default None so a
    # plain direct metric is byte-identical.
    measure: DurationMeasure | None = None
    percentiles: tuple[float, ...] | None = None
    # a steward-pinned GROUP BY field ref (``{concept, column}``) for a
    # duration-percentile metric, so the turn path reports one row per group
    # (e.g. a matrix broken down by a dimension). Valid only WITH a measure;
    # None keeps the scalar/lifetime shape. The dimension column is pinned the
    # same way the measure field refs are (resolved against live MAPS_TO).
    group_by: FieldRef | None = None
    kind: MetricKind = MetricKind.DIRECT


@dataclass(frozen=True)
class DerivedMetric:
    id: str
    label: str
    unit: str
    formula: Formula
    prefer_stored: str | None = None         # ADR 0002/0016 Decision C — hybrid stored-composite id
    group_by: str | None = None              # ADR 0014 seam — resolver-deferred (S2.x)
    window: str | None = None                # ADR 0014 seam — resolver-deferred (S2.x)
    valid_range: tuple[float, float] | None = None
    max_exclusive: bool = False
    denominator_provenance: str | None = None
    kind: MetricKind = MetricKind.DERIVED


Metric = Union[DirectMetric, DerivedMetric]


def filter_concepts(metric: DirectMetric) -> frozenset[str]:
    """Concept ids a direct metric's ``filter`` depends on (ADR-0075 D7);
    ∅ for a filter-less metric, so its closure is unchanged."""
    return frozenset(p.concept for p in (metric.filter or ()))


# --- Interface + default impl + factory (Decision D, ADR 0008 seam) ---

class MetricLayerError(Exception):
    """Raised by validation; carries the offending metric/concept id."""


class MetricLayer(ABC):
    @abstractmethod
    def get_metric(self, metric_id: str) -> Metric: ...

    @abstractmethod
    def list_metrics(self) -> list[Metric]: ...

    def metrics_for_concept(self, concept_id: str) -> frozenset[str]:
        """Reverse index: ids of every Metric whose op-tree closure depends on
        ``concept_id`` (directly, transitively via MetricRefs, or via a
        DerivedMetric's ``prefer_stored`` input). Concrete — derived from
        ``list_metrics``/``get_metric`` (ADR 0005/0017)."""
        return frozenset(
            m.id for m in self.list_metrics() if concept_id in self._concepts_of(m)
        )

    # --- Concept closure over the op-tree (ADR 0001: pure, no I/O/eval) ---

    def _concepts_of(self, metric: Metric) -> frozenset[str]:
        """Concept ids ``metric`` depends on; the ``seen`` set makes ref cycles
        terminate. Subclasses may override to memoize."""
        return self._walk_metric(metric, set())

    def _walk_metric(self, metric: Metric, seen: set[str]) -> frozenset[str]:
        if metric.id in seen:
            return frozenset()  # cycle guard (defensive; seed is acyclic)
        seen = seen | {metric.id}
        if isinstance(metric, DirectMetric):
            return frozenset({metric.concept}) | filter_concepts(metric)
        acc = set(self._walk_term(metric.formula, seen))
        if metric.prefer_stored is not None:  # ADR 0005 FLAG: stored input counts
            acc |= self._walk_metric(self.get_metric(metric.prefer_stored), seen)
        return frozenset(acc)

    def _walk_term(self, term: FormulaTerm, seen: set[str]) -> frozenset[str]:
        if isinstance(term, ConceptRef):
            return frozenset({term.concept})
        if isinstance(term, MetricRef):
            return self._walk_metric(self.get_metric(term.metric), seen)
        if isinstance(term, Formula):
            acc: set[str] = set()
            for sub in term.inputs:
                acc |= self._walk_term(sub, seen)
            return frozenset(acc)
        return frozenset()  # Scalar → no concept

    # --- Resolver-mirroring satisfiability (ADR 0016) — NOT the flat union ---

    def is_satisfiable(self, metric: Metric, covered: frozenset[str]) -> bool:
        """Resolver-mirroring satisfiability (ADR 0016), NOT the flat
        ``_concepts_of`` union. direct → concept covered; derived(no
        prefer_stored) → all inputs covered; derived(prefer_stored) → stored
        covered OR all inputs covered."""
        return self._sat_metric(metric, covered, set())

    def _sat_metric(self, metric: Metric, covered: frozenset[str], seen: set[str]) -> bool:
        if metric.id in seen:
            return True                      # cycle guard (seed is acyclic; mirror _walk_metric)
        seen = seen | {metric.id}
        if isinstance(metric, DirectMetric):
            return metric.concept in covered and filter_concepts(metric) <= covered
        inputs_ok = self._sat_term(metric.formula, covered, seen)   # AND over op-tree
        if metric.prefer_stored is not None:                        # OR-branch (ADR 0016 C)
            stored_ok = self._sat_metric(self.get_metric(metric.prefer_stored), covered, seen)
            return stored_ok or inputs_ok
        return inputs_ok

    def _sat_term(self, term: FormulaTerm, covered: frozenset[str], seen: set[str]) -> bool:
        if isinstance(term, ConceptRef):
            return term.concept in covered
        if isinstance(term, MetricRef):
            return self._sat_metric(self.get_metric(term.metric), covered, seen)
        if isinstance(term, Formula):
            return all(self._sat_term(t, covered, seen) for t in term.inputs)
        return True                          # Scalar always satisfiable

    def missing_concepts(self, metric: Metric, covered: frozenset[str]) -> frozenset[str]:
        """The CHEAPEST unmet path: ∅ if satisfiable; for prefer_stored,
        whichever of {stored} or {inputs} has fewer unmet concepts
        (ties → inputs path)."""
        return self._missing_metric(metric, covered, set())

    def _missing_metric(self, metric: Metric, covered: frozenset[str], seen: set[str]) -> frozenset[str]:
        if metric.id in seen:
            return frozenset()
        seen = seen | {metric.id}
        if isinstance(metric, DirectMetric):
            return (frozenset({metric.concept}) | filter_concepts(metric)) - covered
        inputs_missing = self._missing_term(metric.formula, covered, seen)
        if metric.prefer_stored is not None:
            stored_missing = self._missing_metric(self.get_metric(metric.prefer_stored), covered, seen)
            if not stored_missing or not inputs_missing:
                return frozenset()           # one path fully covered ⇒ satisfiable ⇒ nothing missing
            return stored_missing if len(stored_missing) < len(inputs_missing) else inputs_missing
        return inputs_missing

    def _missing_term(self, term: FormulaTerm, covered: frozenset[str], seen: set[str]) -> frozenset[str]:
        if isinstance(term, ConceptRef):
            return frozenset() if term.concept in covered else frozenset({term.concept})
        if isinstance(term, MetricRef):
            return self._missing_metric(self.get_metric(term.metric), covered, seen)
        if isinstance(term, Formula):
            acc: set[str] = set()
            for t in term.inputs:
                acc |= self._missing_term(t, covered, seen)
            return frozenset(acc)
        return frozenset()                   # Scalar


class CodeMetricLayer(MetricLayer):
    def __init__(self, metrics: Iterable[Metric] | None = None) -> None:
        source = list(_SEED_METRICS if metrics is None else metrics)
        self._by_id = {m.id: m for m in source}  # construction-time registry
        self._concepts_memo: dict[str, frozenset[str]] = {}  # lazy per-metric closure

    def get_metric(self, metric_id: str) -> Metric:
        try:
            return self._by_id[metric_id]
        except KeyError:
            raise KeyError(f"unknown metric id: {metric_id!r}") from None

    def list_metrics(self) -> list[Metric]:
        return list(self._by_id.values())

    def metrics_for_concept(self, concept_id: str) -> frozenset[str]:
        return frozenset(
            m.id for m in self.list_metrics() if concept_id in self._concepts_of(m)
        )

    # --- Concept closure: memoized override of the ABC's pure walk ---

    def _concepts_of(self, metric: Metric) -> frozenset[str]:
        """Memoized override of ``MetricLayer._concepts_of`` (the registry is
        construction-time immutable, so caching the closure is safe)."""
        cached = self._concepts_memo.get(metric.id)
        if cached is None:
            cached = self._walk_metric(metric, set())  # inherited concrete walk
            self._concepts_memo[metric.id] = cached
        return cached


# --- Seed registry ---
# `oee` exists ONLY to exercise/validate the op-tree schema and is wired to NO
# resolver until S2.1 (per ADR 0014 consequences).

_SEED_METRICS: list[Metric] = [
    DirectMetric(
        id="spare-parts-on-hand",
        concept="mro-inventory",
        aggregation=Aggregation.SUM,
        label="Spare Parts On Hand",
        unit="units",
    ),
    # --- OEE input Metrics (ADR 0016 Decision A) — AVG, not LATEST (Decision E /
    # plan Flag 3: LATEST raises in _compose_sql, which would defeat prefer_stored).
    DirectMetric(
        id="availability",
        concept="availability",
        aggregation=Aggregation.AVG,
        label="Availability",
        unit="ratio",
    ),
    DirectMetric(
        id="performance",
        concept="performance",
        aggregation=Aggregation.AVG,
        label="Performance",
        unit="ratio",
    ),
    # `quality-rate` stays a DIRECT metric over `quality-metric` (its per-line
    # grouping powers the `quality-by-line` bar via resolve-by-dimension, which is
    # direct-only). The OEE gauge's quality SUB-READING, however, must be a proper
    # 0-100 RATE — see `oee-quality` below.
    DirectMetric(
        id="quality-rate",
        concept="quality-metric",
        aggregation=Aggregation.AVG,
        label="Quality Rate",
        unit="ratio",
    ),
    # --- OEE quality sub-reading — the WEIGHTED first-pass yield (0-100), computed
    # as good / (good + reject) * 100 from the good-count and reject (scrap-waste)
    # metrics. NOTE: an actual-quantity-based yield (good/actual) would avoid the
    # cross-system scrap-waste granularity mismatch, but needs actual-quantity mapped
    # via onboarding + a grouped ratio in resolve-by-dimension — deferred.
    DirectMetric(
        id="quality-good-count",
        concept="quality-metric",
        aggregation=Aggregation.SUM,
        label="Good Count",
        unit="units",
    ),
    # Direct metrics for actual-quantity and quality-yield-rate concepts (mapped via onboarding)
    DirectMetric(
        id="actual-quantity",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Actual Quantity",
        unit="units",
    ),
    # --- Windowed output (Task 2D — first activation of the ADR 0014 ``window``
    # seam). The SAME concept + SUM as `actual-quantity`, restricted to a recent
    # event-time window by the grouped resolver (metric_dimension.WINDOW_VOCAB,
    # closed vocabulary) so the Output-by-Line panel shows a decision-useful
    # 24h rate instead of lifetime millions. ADDITIVE — `actual-quantity` keeps
    # its id/semantics (lineage + oee-quality refs unchanged). HONEST fallback:
    # a system with no `event-time` field-concept mapped on the measure table
    # resolves this metric as the lifetime shape with `window: null` in lineage
    # (the frontend renders a 'lifetime total' caption instead).
    DirectMetric(
        id="actual-quantity-windowed",
        concept="actual-quantity",
        aggregation=Aggregation.SUM,
        label="Output",
        unit="units",
        window="24h",
    ),
    DirectMetric(
        id="quality-yield-rate",
        concept="quality-yield-rate",
        aggregation=Aggregation.AVG,
        label="Quality Yield Rate",
        unit="ratio",
    ),
    # --- OEE quality sub-reading (Task 1A). The PREFERRED source is the stored
    # `quality-yield-rate` concept (an AVG over a 0-100 first-pass-yield column at
    # a single-table granularity — deployed-verified 92-94%), presented under the
    # stable `oee-quality` id via the prefer_stored hybrid shortcut (ADR 0016
    # Decision C) so card refs and lineage stay stable. The good/actual RATIO
    # formula is ONLY the derive fallback for systems that map
    # quality-metric + actual-quantity but not quality-yield-rate: as lifetime
    # SUMs it is vulnerable to a cross-system/cross-table granularity mismatch
    # (numerator and denominator pinned to DIFFERENT authorities → an observed
    # 2.84% reading), which is why it is not the primary path.
    DerivedMetric(
        id="oee-quality",
        label="Quality",
        unit="ratio",
        formula=Formula(
            FormulaOp.SCALE,
            (
                Formula(
                    FormulaOp.RATIO,
                    (
                        MetricRef("quality-good-count"),
                        MetricRef("actual-quantity"),
                    ),
                ),
                Scalar(100.0),
            ),
        ),
        prefer_stored="quality-yield-rate",
    ),
    # --- Sensor heatmap measure (S5.3 §6.3 / Wave 3) — latest value per group.
    # LATEST needs a time/ordering column (the `event-time` field-concept) mapped
    # on the sensor table; absent that mapping the heatmap stays correctly
    # uncovered. Used by the heatmap resolver, not by any seed Card.
    DirectMetric(
        id="sensor-reading-latest",
        concept="sensor-reading",
        aggregation=Aggregation.LATEST,
        label="Latest Sensor Reading",
        unit="",
    ),
    DirectMetric(
        id="oee-score",
        concept="oee-score",
        aggregation=Aggregation.AVG,
        label="OEE (Stored)",
        unit="ratio",
    ),
    # --- Derived OEE (Task 1A normalization). The DERIVED value is a true
    # A × P × Q percentage on a DOCUMENTED, uniform scale:
    #   - `availability` / `performance` are 0-1 ratios (unit "ratio" — the AVG
    #     of a fractional utilization/efficiency column);
    #   - `oee-quality` is a 0-100 percentage (it tracks `quality-yield-rate`,
    #     a 0-100 first-pass yield) → the inner SCALE(·, 0.01) guard normalizes
    #     it to 0-1 so all three PRODUCT operands multiply on the SAME 0-1 scale;
    #   - the outer SCALE(·, 100) presents the 0-1 product as a 0-100 OEE
    #     percentage (matching the stored `oee-score` presentation).
    # The guards make the per-operand scale contract explicit in the op-tree and
    # visible in resolve lineage. `prefer_stored="oee-score"` stays: when the
    # stored composite resolves it wins (the gauge center is "system-reported" —
    # frontend labeling handles the distinction); the normalized formula is the
    # derive fallback.
    DerivedMetric(
        id="oee",
        label="Overall Equipment Effectiveness",
        unit="ratio",
        formula=Formula(
            FormulaOp.SCALE,
            (
                Formula(
                    FormulaOp.PRODUCT,
                    (
                        MetricRef("availability"),
                        MetricRef("performance"),
                        Formula(
                            FormulaOp.SCALE,
                            (MetricRef("oee-quality"), Scalar(0.01)),
                        ),
                    ),
                ),
                Scalar(100.0),
            ),
        ),
        prefer_stored="oee-score",
    ),
    # --- Abstract OT-native Metrics (generic manufacturing telemetry). Each is a
    # DIRECT metric over a CORE concept the onboarded systems commonly cover, so
    # any system mapping the concept lights up the matching Card — no dataset,
    # model, or vendor literals. Aggregations chosen by concept semantics: rates
    # AVG over the window, counts/durations SUM.
    DirectMetric(
        id="throughput",
        concept="throughput",
        aggregation=Aggregation.AVG,
        label="Throughput",
        unit="units/interval",
    ),
    DirectMetric(
        id="machine-speed",
        concept="machine-speed",
        aggregation=Aggregation.AVG,
        label="Machine Speed",
        unit="units/min",
    ),
    # --- Machine-speed target + actual-vs-target performance ratio. The target
    # setpoint is its own DIRECT metric over the `target-machine-speed` concept
    # (a rated/design speed column, mapped via onboarding like any field concept).
    DirectMetric(
        id="target-machine-speed",
        concept="target-machine-speed",
        aggregation=Aggregation.AVG,
        label="Target Machine Speed",
        unit="units/min",
    ),
    # --- Performance ratio (actual vs target speed). Mirrors the `oee` hybrid
    # (ADR 0016 Decision C): the PREFERRED source is the stored `performance`
    # concept (a 0-1 OEE performance ratio supplied directly by a source system);
    # the RATIO(actual speed / target speed) formula is the derive fallback for
    # systems that map the two speed concepts but not the stored ratio. Both
    # paths present on the SAME 0-1 "ratio" scale (like the availability/
    # performance OEE sub-readings) — no SCALE guard needed.
    DerivedMetric(
        id="performance-ratio",
        label="Performance (Actual vs Target)",
        unit="ratio",
        formula=Formula(
            FormulaOp.RATIO,
            (
                MetricRef("machine-speed"),
                MetricRef("target-machine-speed"),
            ),
        ),
        prefer_stored="performance",
    ),
    DirectMetric(
        id="scrap-waste",
        concept="scrap-waste",
        aggregation=Aggregation.SUM,
        label="Scrap / Waste",
        unit="units",
    ),
    DirectMetric(
        id="runtime-hours",
        concept="runtime-hours",
        aggregation=Aggregation.SUM,
        label="Runtime Hours",
        unit="hours",
    ),
    # --- Work-order distribution measure (concept-driven, generic). A COUNT over
    # the `work-order` concept; grouped by the `lifecycle-state` dimension via
    # /api/metrics/resolve-by-dimension it yields the count of work orders per
    # status — the "Work Orders by Status" bar card. Lights up for ANY system
    # mapping `work-order` (+ `lifecycle-state` for the grouped view) — no
    # dataset/system literals. Satisfiable exactly when `work-order` is covered.
    DirectMetric(
        id="work-order-count",
        concept="work-order",
        aggregation=Aggregation.COUNT,
        label="Work Orders",
        unit="orders",
    ),
    # --- Machine-state distribution measure (feasible categorical OT view). A
    # COUNT over the `machine-state` concept, resolved GROUP BY the same
    # `machine-state` dimension (co-located) yields the count of records per
    # current state — the honest "current machine-state distribution" the
    # `machine-state` BAR card renders via /api/metrics/resolve-by-dimension.
    # NOT a time-series state-timeline (that needs a charting lib the frontend
    # does not bundle). Satisfiable exactly when `machine-state` is covered.
    DirectMetric(
        id="machine-state-distribution",
        concept="machine-state",
        aggregation=Aggregation.COUNT,
        label="Machines by State",
        unit="machines",
    ),
    # --- Per-asset current machine state (S5.4) — the LATEST machine-state value grouped by
    # the `asset` dimension, i.e. "which machine is in what state right now". Resolved ONLY on
    # demand (the Line State panel's click-to-drill), never on the periodic snapshot, so it
    # adds no recurring scan cost. Like sensor-reading-latest, LATEST needs a time/ordering
    # (`event-time`) field mapped; absent that it stays correctly uncovered/empty.
    DirectMetric(
        id="machine-state-latest",
        concept="machine-state",
        aggregation=Aggregation.LATEST,
        label="Current Machine State",
        unit="",
    ),
    # --- Active alarm/fault measure (concept-driven, generic). A COUNT over the
    # `failure-mode` concept — every record carrying a failure/fault/reason code
    # (alarm and fault event tables) — restricted to a recent 24h window via the
    # ADR 0014 ``window`` seam (same closed WINDOW_VOCAB token as
    # `actual-quantity-windowed`), so the KPI reads as "alarms/faults raised
    # recently" rather than a lifetime total. HONEST fallback: with no
    # `event-time` field mapped on the measure table the resolve proceeds as the
    # lifetime shape with `window: null` in lineage (frontend captions it).
    # Satisfiable exactly when `failure-mode` is covered.
    DirectMetric(
        id="alarm-event-count",
        concept="failure-mode",
        aggregation=Aggregation.COUNT,
        label="Active Alarms",
        unit="events",
        window="24h",
    ),
    # --- Inspection-result distribution measure (concept-driven, generic). A
    # COUNT over the `inspection-result` concept; grouped by the SAME
    # `inspection-result` dimension (co-located — the machine-state-distribution
    # pattern) it yields the count of inspections per outcome (PASS/FAIL/...),
    # from which yield is directly visible. A single KPI "% pass" would need a
    # filtered-count ratio; the DirectMetric ``filter`` (ADR-0075) can express
    # it once a deployment binds the outcome concept, but no seed metric
    # declares one — the distribution is the honest representation with the
    # core vocabulary (ADR 0045).
    DirectMetric(
        id="inspection-count",
        concept="inspection-result",
        aggregation=Aggregation.COUNT,
        label="Inspections",
        unit="inspections",
    ),
]


@lru_cache(maxsize=1)
def get_metric_layer() -> MetricLayer:
    """Process-wide default metric layer: the seed ``CodeMetricLayer`` registry,
    composed with graph-resident definitions (ADR-0066 D2/C5).

    ``ComposedMetricLayer`` IS a ``CodeMetricLayer``, so the seed registry stays
    byte-identical (same objects, same order) and the graph half only ever ADDS
    ids — code metrics win every collision, loudly (see ``graph_metric_layer``).

    The graph import is LAZY and lives inside this function so this module stays
    import-pure (ADR 0001), and it fails OPEN: a deployment without the graph
    half (local dev, the CDK venv) keeps the seed registry.
    """
    try:
        from .graph_metric_layer import ComposedMetricLayer
    except Exception:
        return CodeMetricLayer()
    return ComposedMetricLayer()


def invalidate_metric_layer() -> None:
    """Drop the graph half of the composed layer's view (ADR-0066 C4/D4).

    Called in-process by the accept path so an accepted definition resolves
    immediately; other processes converge within the snapshot TTL. The
    ``lru_cache``d layer object is deliberately kept (it re-reads the snapshot),
    and a code-only layer has nothing to drop, so this is then a no-op.
    """
    invalidate = getattr(get_metric_layer(), "invalidate", None)
    if callable(invalidate):
        invalidate()
    # ADR-0078 D3: cached per-turn metric-layer RESULTS drop with the layer, so
    # an accept is visible on the next chat turn in-process. Lazy + fail-open
    # (import purity; a cache drop never fails an accept).
    from .metric_turn_invalidation import invalidate_metric_turn_results
    invalidate_metric_turn_results()


# --- Validation ---

def _validate_valid_range(metric: Metric) -> None:
    """Fail loud when optional range semantics are malformed."""
    valid_range = metric.valid_range
    if not isinstance(metric.max_exclusive, bool):
        raise MetricLayerError(
            f"metric {metric.id!r}: max_exclusive must be a boolean"
        )
    if valid_range is None:
        if metric.max_exclusive:
            raise MetricLayerError(
                f"metric {metric.id!r}: max_exclusive requires valid_range"
            )
        return
    if not isinstance(valid_range, tuple) or len(valid_range) != 2:
        raise MetricLayerError(
            f"metric {metric.id!r}: valid_range must be a two-number tuple"
        )
    lower, upper = valid_range
    if (
        isinstance(lower, bool)
        or isinstance(upper, bool)
        or not isinstance(lower, (int, float))
        or not isinstance(upper, (int, float))
        or not math.isfinite(float(lower))
        or not math.isfinite(float(upper))
        or lower >= upper
    ):
        raise MetricLayerError(
            f"metric {metric.id!r}: valid_range must contain finite bounds "
            "with minimum < maximum"
        )


def _formula_contains_ratio(term: FormulaTerm) -> bool:
    """Return whether an op-tree contains a ratio/divide operation."""
    if not isinstance(term, Formula):
        return False
    return term.op is FormulaOp.RATIO or any(
        _formula_contains_ratio(child) for child in term.inputs
    )


def _validate_term(
    term: FormulaTerm,
    *,
    metric_id: str,
    concepts: frozenset[str],
    defined_ids: frozenset[str],
) -> None:
    """Recursively validate a formula term. Scalar is always valid."""
    if isinstance(term, ConceptRef):
        if term.concept not in concepts:
            raise MetricLayerError(
                f"metric {metric_id!r}: concept {term.concept!r} not in CORE_CONCEPTS"
            )
    elif isinstance(term, MetricRef):
        if term.metric not in defined_ids:
            raise MetricLayerError(
                f"metric {metric_id!r}: metric ref {term.metric!r} does not resolve"
            )
    elif isinstance(term, Formula):
        for sub in term.inputs:
            _validate_term(
                sub, metric_id=metric_id, concepts=concepts, defined_ids=defined_ids
            )
    # Scalar → always valid (no branch needed).


def _validate_filter(metric: DirectMetric, concepts: frozenset[str]) -> None:
    """Fail loud on a malformed ``filter`` (ADR-0075 D1/D5): the ONE rule,
    predicate concepts in ``CORE_CONCEPTS``, and never with ``latest``."""
    if metric.filter is None:
        return
    problem = check_filter(metric.filter)
    if problem:
        raise MetricLayerError(f"metric {metric.id!r}: {problem}")
    if metric.aggregation is Aggregation.LATEST:
        raise MetricLayerError(
            f"metric {metric.id!r}: filter is not valid with the 'latest' aggregation"
        )
    for pred in metric.filter:
        if pred.concept not in concepts:
            raise MetricLayerError(
                f"metric {metric.id!r}: predicate concept {pred.concept!r} "
                "not in CORE_CONCEPTS"
            )


def _validate_measure(metric: DirectMetric, concepts: frozenset[str]) -> None:
    """Fail loud on a malformed duration ``measure`` / ``percentiles``.

    ``measure`` and ``percentiles`` come as a pair; the measure's two field-ref
    concepts (and ``group_by``'s, if any) must be in ``CORE_CONCEPTS``. A
    measure metric keeps the ``count`` pairing; ``latest`` is rejected (a
    duration has no ordering)."""
    if metric.measure is None and metric.percentiles is None:
        return
    _validate_measure_pairing(metric)
    if metric.aggregation is Aggregation.LATEST:
        raise MetricLayerError(
            f"metric {metric.id!r}: a duration measure is not valid with the "
            "'latest' aggregation"
        )
    for role, ref in (("start", metric.measure.start), ("end", metric.measure.end)):
        if ref.concept not in concepts:
            raise MetricLayerError(
                f"metric {metric.id!r}: measure {role} concept {ref.concept!r} "
                "not in CORE_CONCEPTS"
            )
    _validate_group_by(metric, concepts)


def _validate_measure_pairing(metric: DirectMetric) -> None:
    """The measure⇔percentiles pairing + shape/charset rules."""
    has_measure = metric.measure is not None
    has_percentiles = metric.percentiles is not None
    if has_percentiles and not has_measure:
        raise MetricLayerError(
            f"metric {metric.id!r}: percentiles are valid only with a "
            "duration measure")
    if has_measure and not has_percentiles:
        raise MetricLayerError(
            f"metric {metric.id!r}: a duration measure requires percentiles")
    problem = check_measure(metric.measure)
    if problem:
        raise MetricLayerError(f"metric {metric.id!r}: {problem}")
    problem = check_percentiles(metric.percentiles)
    if problem:
        raise MetricLayerError(f"metric {metric.id!r}: {problem}")


    def _validate_group_by(metric: DirectMetric, concepts: frozenset[str]) -> None:
        """A ``group_by`` field ref is valid only WITH a measure, is itself
        a well-formed ``{concept, column}``, and names a KNOWN concept."""
        if metric.group_by is None:
            return
        if metric.measure is None:
            raise MetricLayerError(
                f"metric {metric.id!r}: group_by is valid only with a duration measure")
        problem = check_field_ref(metric.group_by)
        if problem:
            raise MetricLayerError(f"metric {metric.id!r}: group_by {problem}")
        if metric.group_by.concept not in concepts:
            raise MetricLayerError(
                f"metric {metric.id!r}: group_by concept {metric.group_by.concept!r} "
                "not in CORE_CONCEPTS")


def validate_metric_layer(layer: MetricLayer | None = None) -> None:
    """Validate every Metric's refs resolve. Raise ``MetricLayerError`` (with the
    offending id) on the first problem; return ``None`` on success."""
    if layer is None:
        layer = get_metric_layer()

    metrics = layer.list_metrics()
    concepts = frozenset(CORE_CONCEPTS)

    seen: set[str] = set()
    for m in metrics:
        if m.id in seen:
            raise MetricLayerError(f"duplicate metric id: {m.id}")
        seen.add(m.id)
    defined_ids = frozenset(seen)

    for m in metrics:
        _validate_valid_range(m)
        if isinstance(m, DirectMetric):
            if m.concept not in concepts:
                raise MetricLayerError(
                    f"metric {m.id!r}: concept {m.concept!r} not in CORE_CONCEPTS"
                )
            _validate_filter(m, concepts)
            _validate_measure(m, concepts)
        elif isinstance(m, DerivedMetric):
            _validate_term(
                m.formula,
                metric_id=m.id,
                concepts=concepts,
                defined_ids=defined_ids,
            )
            if m.denominator_provenance is not None:
                if (
                    not isinstance(m.denominator_provenance, str)
                    or not m.denominator_provenance.strip()
                ):
                    raise MetricLayerError(
                        f"metric {m.id!r}: denominator_provenance must be a "
                        "non-empty concept id"
                    )
                if not _formula_contains_ratio(m.formula):
                    raise MetricLayerError(
                        f"metric {m.id!r}: denominator_provenance requires a "
                        "ratio formula operation"
                    )
