# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Card layer (ADR 0018): the source of truth for *how a value is presented* —
a ``Card`` binds one or more Metric ids to a visualization, separate from the
Metric layer (how a value is defined) and the resolver (values).

A ``Card`` is declarative (Decision B): an id + title + ordered ``metric_refs``
+ a ``viz_type`` from a closed vocabulary, with an optional ``group_by`` carried
+ for the metric-by-dimension seam (Decision C). ``kpi`` and ``gauge`` (composite
+ + sub-readings) render scalar metric values; ``bar`` carries a ``group_by`` and
is resolved client-side via ``/api/metrics/resolve-by-dimension``; ``line`` stays
reserved/gated. The layer is a definition provider behind the ``CardLayer`` interface with a ``CodeCardLayer`` default
obtained via ``get_card_layer()`` (Decision B, ADR 0008 seam).
``validate_card_layer()`` asserts every ``metric_ref`` resolves against the
metric layer (``get_metric_layer().get_metric``) and that each card's
``viz_type`` is implemented.

This module is import-pure (ADR 0001 / ADR 0008): no I/O, no LLM, no
``eval``/``exec``; it imports only ``get_metric_layer`` from ``.metric_layer``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Iterable

from .metric_layer import get_metric_layer


# --- Fixed vocabulary (str mixin → JSON-serializable values for ADR 0008) ---

class VizType(str, Enum):
    KPI = "kpi"        # implemented — scalar (Decision C)
    BAR = "bar"        # implemented WITH group_by (S5.3 §6.3 by-dimension)
    LINE = "line"      # reserved — declared, gated
    GAUGE = "gauge"    # implemented — composite + sub-readings (S5.3 §1)


# --- Optional declarative presentation (thresholds + target) ---
# A ``ThresholdBand`` is one entry of an ordered band list (the PoC Grafana
# pattern, e.g. Total Waste green<500 / orange<1000 / red>=1000): ``value`` is
# the INCLUSIVE upper cutoff for the band and ``color`` a generic semantic token
# (``green``/``orange``/``red``). A band with ``value=None`` is the final
# catch-all (no upper bound). Both fields are pure data — the frontend picks the
# band a metric value falls in and colors the value accordingly. No dataset,
# model, or vendor literals — bands/targets are generic, declarative references.

@dataclass(frozen=True)
class ThresholdBand:
    value: float | None    # inclusive upper cutoff; None ⇒ catch-all (last band)
    color: str             # generic semantic token: "green" | "orange" | "red"


# --- Optional card context metadata (S5.4) ---
# Site / cell / system labels for grouping + display. NOT part of the metric (a metric is
# system-agnostic); it is declarative context on the CARD. The dashboard auto-derives
# system + site from the resolving system when possible; any field declared here OVERRIDES
# the auto value (and `cell`, which has no auto source, is supplied here). All optional.
@dataclass(frozen=True)
class CardContext:
    site: str | None = None
    cell: str | None = None
    system: str | None = None


# --- Card type (Decision B) ---

@dataclass(frozen=True)
class Card:
    id: str
    title: str
    metric_refs: tuple[str, ...]
    viz_type: VizType
    group_by: str | None = None    # carried, resolver-deferred (Decision C)
    # OPTIONAL declarative presentation (both default None ⇒ backward-compatible;
    # existing cards render byte-identically). ``thresholds`` is an ordered tuple
    # of ``ThresholdBand`` cutoffs; ``target`` a numeric reference for an
    # actual-vs-target readout. Neither composes SQL or affects resolution — they
    # are pure render hints carried through to the card payload.
    thresholds: tuple[ThresholdBand, ...] | None = None
    target: float | None = None
    # OPTIONAL declarative context (site/cell/system) for grouping + display (S5.4). None ⇒
    # auto-derived system/site only. Declared fields override the auto-derived ones.
    context: CardContext | None = None


# --- Interface + default impl + factory (Decision B, ADR 0008 seam) ---

class CardLayerError(Exception):
    """Raised by validation; carries the offending card/ref id."""


class CardLayer(ABC):
    @abstractmethod
    def get_card(self, card_id: str) -> Card: ...

    @abstractmethod
    def list_cards(self) -> list[Card]: ...

    def cards_for_metric(self, metric_id: str) -> list[Card]:
        """Reverse index: every card whose ``metric_refs`` contains
        ``metric_id``, in ``list_cards()`` order. Concrete — derived from
        ``list_cards`` (ADR 0005)."""
        return [c for c in self.list_cards() if metric_id in c.metric_refs]


class CodeCardLayer(CardLayer):
    def __init__(self, cards: Iterable[Card] | None = None) -> None:
        source = list(_SEED_CARDS if cards is None else cards)
        self._by_id = {c.id: c for c in source}  # construction-time registry, dedup by id

    def get_card(self, card_id: str) -> Card:
        try:
            return self._by_id[card_id]
        except KeyError:
            raise KeyError(f"unknown card id: {card_id!r}") from None

    def list_cards(self) -> list[Card]:
        return list(self._by_id.values())


# --- Seed registry ---
# Seed cards (Decision E, additive): `spare-parts-on-hand` (the folded
# S1.3 tracer, direct Metric) ships as `kpi`; `oee` ships as a `gauge` whose
# ordered `metric_refs` are the composite OEE plus its three sub-readings
# (availability, performance, quality-rate) so the gauge can render OEE as the
# center value with three sub-readings (S5.3 §1); `quality-by-line` ships as a
# `bar` card carrying a `group_by` dimension concept — a DECLARATIVE descriptor
# the frontend resolves via POST /api/metrics/resolve-by-dimension (S5.3 §6.3),
# NOT via the scalar `_build_card` path. Following them are the abstract
# OT-native KPI cards (throughput, machine-speed, scrap-waste, runtime-hours)
# — one per generic OT concept the onboarded systems commonly cover, so any
# system mapping that concept lights up the card (no dataset/model literals).

_SEED_CARDS: list[Card] = [
    Card(
        id="spare-parts-on-hand",
        title="Spare Parts On Hand",
        metric_refs=("spare-parts-on-hand",),
        viz_type=VizType.KPI,
    ),
    Card(
        id="oee",
        title="Overall Equipment Effectiveness",
        # Ordered: composite first (gauge center value), then the three OEE
        # sub-readings rendered as sub-readings. The quality sub-reading is
        # `oee-quality` (the 0-100 weighted first-pass yield), NOT the direct
        # `quality-rate` (whose per-line AVG powers the quality-by-line bar but is a
        # good-COUNT here, not a 0-100 rate).
        metric_refs=("oee", "availability", "performance", "oee-quality"),
        viz_type=VizType.GAUGE,
    ),
    Card(
        id="quality-by-line",
        title="Quality by Line",
        # Uses quality-yield-rate (maps to pass_rate 0-100 column), NOT quality-rate
        # (which maps to good_COUNT and averaged ~300 when grouped by line).
        metric_refs=("quality-yield-rate",),
        viz_type=VizType.BAR,
        group_by="production-line",
    ),
    # --- Abstract OT-native KPI cards (generic manufacturing telemetry). Each
    # binds one DIRECT OT metric over a CORE concept the onboarded systems
    # commonly cover, so any system mapping the concept lights up the card. No
    # dataset/model/vendor literals — purely generic OT KPIs. Where meaningful a
    # card carries an OPTIONAL declarative ``thresholds`` band list (color the
    # value green/orange/red) and/or a numeric ``target`` (actual-vs-target) —
    # the PoC Grafana pattern. The band cutoffs / targets are generic, reasonable
    # illustrative defaults, NOT dataset-specific.
    Card(
        id="throughput",
        title="Throughput",
        metric_refs=("throughput",),
        viz_type=VizType.KPI,
        target=1000.0,          # generic actual-vs-target reference (units/interval)
    ),
    Card(
        id="machine-speed",
        title="Machine Speed",
        metric_refs=("machine-speed",),
        viz_type=VizType.KPI,
        target=60.0,            # generic actual-vs-target reference (units/min)
    ),
    Card(
        id="scrap-waste",
        title="Scrap / Waste",
        metric_refs=("scrap-waste",),
        viz_type=VizType.KPI,
        # Lower-is-better waste bands (PoC "Total Waste" pattern): green under
        # 500, orange under 1000, red at/above 1000 (catch-all final band).
        thresholds=(
            ThresholdBand(500.0, "green"),
            ThresholdBand(1000.0, "orange"),
            ThresholdBand(None, "red"),
        ),
    ),
    Card(
        id="runtime-hours",
        title="Runtime Hours",
        metric_refs=("runtime-hours",),
        viz_type=VizType.KPI,
        target=168.0,           # generic reference (hours in a week)
    ),
    # --- Machine-state card (categorical OT concept the SiteWise system covers).
    # A full Grafana-style time-series state-timeline (Gantt) would need a
    # charting library the frontend does not bundle, so this ships the FEASIBLE,
    # honest representation: the CURRENT machine-state DISTRIBUTION — a `bar`
    # grouped by the `machine-state` dimension, rendering the count per current
    # state via the existing group_by + /api/metrics/resolve-by-dimension path
    # (same seam as `quality-by-line`). It validates and is satisfiable exactly
    # when the `machine-state` concept is covered. Deliberately NOT a broken
    # partial state-timeline — the distribution is a correct, complete view with
    # today's viz vocabulary.
    Card(
        id="machine-state",
        title="Machine State",
        metric_refs=("machine-state-distribution",),
        viz_type=VizType.BAR,
        group_by="machine-state",
    ),
    # --- Work Orders by Status (concept-driven, generic). COUNT over the
    # `work-order` concept grouped by the `lifecycle-state` dimension — the same
    # declarative bar + group_by seam as `quality-by-line`, resolved client-side
    # via /api/metrics/resolve-by-dimension. Lights up for ANY system mapping
    # both concepts (no dataset/system literals).
    Card(
        id="work-orders-by-status",
        title="Work Orders by Status",
        metric_refs=("work-order-count",),
        viz_type=VizType.BAR,
        group_by="lifecycle-state",
    ),
    # --- Output by Line (concept-driven, generic). Windowed SUM of the
    # `actual-quantity` concept grouped by the `production-line` dimension —
    # refs the `actual-quantity-windowed` DirectMetric (Task 2D) so the card
    # registry and the frontend output loader agree on ONE metric id (the
    # registry↔loader drift class that caused the original quality-panel bug).
    # Same concept + coverage gating as the lifetime `actual-quantity`; the
    # grouped execution goes through the same resolve-by-dimension seam (incl.
    # the honest lifetime fallback when no event-time mapping exists).
    Card(
        id="output-by-line",
        title="Output by Line",
        metric_refs=("actual-quantity-windowed",),
        viz_type=VizType.BAR,
        group_by="production-line",
    ),
    # --- 2A: additional generically-satisfiable by-line breakdowns. Same
    # declarative bar + group_by seam as `quality-by-line`/`output-by-line`:
    # each reuses an EXISTING DirectMetric over a core OT concept grouped by the
    # `production-line` dimension, resolved client-side via
    # /api/metrics/resolve-by-dimension. Coverage-gated automatically — they
    # light up ONLY when both concepts are covered by an onboarded system (no
    # dataset/system literals).
    Card(
        id="scrap-by-line",
        title="Scrap by Line",
        metric_refs=("scrap-waste",),
        viz_type=VizType.BAR,
        group_by="production-line",
    ),
    Card(
        id="throughput-by-line",
        title="Throughput by Line",
        metric_refs=("throughput",),
        viz_type=VizType.BAR,
        group_by="production-line",
    ),
    # --- Performance gauge (actual vs target machine speed). Mirrors the `oee`
    # gauge mechanism: the composite `performance-ratio` (prefer_stored →
    # `performance`, derive fallback → RATIO of the two speed metrics) renders
    # as the center value, with the actual and target speed metrics as
    # sub-readings. Lights up for ANY system mapping the stored `performance`
    # concept OR both speed concepts (no dataset/system literals).
    Card(
        id="performance",
        title="Performance",
        metric_refs=("performance-ratio", "machine-speed", "target-machine-speed"),
        viz_type=VizType.GAUGE,
    ),
    # --- Active alarms KPI (concept-driven, generic). The 24h-windowed COUNT of
    # records carrying a failure/fault/reason code (`failure-mode`). Lower-is-
    # better bands (the scrap-waste pattern): green under 5, orange under 20,
    # red at/above 20 — generic illustrative defaults, NOT dataset-specific.
    Card(
        id="active-alarms",
        title="Active Alarms",
        metric_refs=("alarm-event-count",),
        viz_type=VizType.KPI,
        thresholds=(
            ThresholdBand(5.0, "green"),
            ThresholdBand(20.0, "orange"),
            ThresholdBand(None, "red"),
        ),
    ),
    # --- Inspections by Result (concept-driven, generic). COUNT over the
    # `inspection-result` concept grouped by the SAME `inspection-result`
    # dimension (co-located — the machine-state distribution pattern), rendering
    # the count of inspections per outcome (PASS/FAIL/...) via the existing
    # group_by + /api/metrics/resolve-by-dimension seam. The inspection YIELD is
    # directly visible from the distribution; a single "% pass" KPI would need a
    # filtered-count ratio the resolver does not implement (the ``filter`` seam
    # is resolver-deferred, S2.x) — this is the honest, feasible representation
    # with today's viz vocabulary (ADR 0045).
    Card(
        id="inspections-by-result",
        title="Inspections by Result",
        metric_refs=("inspection-count",),
        viz_type=VizType.BAR,
        group_by="inspection-result",
    ),
]


@lru_cache(maxsize=1)
def get_card_layer() -> CardLayer:
    """Process-wide default ``CodeCardLayer`` over the seed registry."""
    return CodeCardLayer()


# --- Validation ---

def validate_card_layer(layer: CardLayer | None = None) -> None:
    """Validate every Card. Raise ``CardLayerError`` (with the offending id) on
    the first problem; return ``None`` on success.

    Rules (mirror ``validate_metric_layer``): (1) duplicate card-id guard;
    (2) each ``metric_ref`` must resolve via ``get_metric_layer().get_metric``;
    (3) viz-gate — ``kpi`` is implemented (Decision C); a ``gauge`` card is
    accepted WHEN it has >=1 ``metric_ref`` and NO ``group_by`` (the S5.3 §1
    OEE gauge: composite + sub-readings); a ``bar`` card is accepted WHEN it
    carries a ``group_by`` (a dimension concept) — the S5.3 §6.3
    metric-by-dimension seam (resolved client-side via
    ``/api/metrics/resolve-by-dimension``). ``bar`` without ``group_by``,
    ``gauge`` with a ``group_by``, and ``line`` stay gated.
    """
    if layer is None:
        layer = get_card_layer()

    cards = layer.list_cards()
    metric_layer = get_metric_layer()

    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise CardLayerError(f"duplicate card id: {card.id}")
        seen.add(card.id)

    for card in cards:
        for ref in card.metric_refs:
            try:
                metric_layer.get_metric(ref)
            except KeyError:
                raise CardLayerError(
                    f"card {card.id!r}: metric ref {ref!r} does not resolve"
                ) from None
        if not _viz_is_implemented(card):
            raise CardLayerError(
                f"card {card.id!r}: viz_type {card.viz_type.value!r} "
                f"not implemented ('kpi'; 'gauge' with >=1 metric_ref and NO "
                f"group_by; or 'bar' with a 'group_by' dimension)"
            )
        _validate_presentation(card)


def _validate_presentation(card: Card) -> None:
    """Validate the OPTIONAL declarative presentation seams (thresholds/target).

    Absent (``None``) fields are always valid (backward-compatible). When
    present: ``thresholds`` must be a non-empty ordered band list where every
    band carries a non-empty ``color``, only the FINAL band may be the
    ``value=None`` catch-all, and the numeric cutoffs are non-decreasing
    (ordered); ``target`` must be a real (non-bool) number. Raises
    ``CardLayerError`` (with the offending card id) on the first problem."""
    if card.thresholds is not None:
        bands = card.thresholds
        if not bands:
            raise CardLayerError(f"card {card.id!r}: thresholds must be non-empty when set")
        last_value: float | None = None
        for i, band in enumerate(bands):
            if not isinstance(band.color, str) or not band.color:
                raise CardLayerError(
                    f"card {card.id!r}: threshold band {i} has an empty color")
            if band.value is None:
                if i != len(bands) - 1:
                    raise CardLayerError(
                        f"card {card.id!r}: only the final threshold band may be "
                        f"the value=None catch-all")
            else:
                if isinstance(band.value, bool) or not isinstance(band.value, (int, float)):
                    raise CardLayerError(
                        f"card {card.id!r}: threshold band {i} value must be numeric")
                if last_value is not None and band.value < last_value:
                    raise CardLayerError(
                        f"card {card.id!r}: threshold band cutoffs must be non-decreasing")
                last_value = band.value
    if card.target is not None:
        if isinstance(card.target, bool) or not isinstance(card.target, (int, float)):
            raise CardLayerError(f"card {card.id!r}: target must be numeric")


def _viz_is_implemented(card: Card) -> bool:
    """A card's viz is renderable now: ``kpi`` always; ``gauge`` WITH >=1
    ``metric_ref`` and NO ``group_by`` (S5.3 §1 OEE gauge — composite + sub-
    readings); ``bar`` only WITH a ``group_by`` dimension concept (S5.3 §6.3
    metric-by-dimension seam). ``line``, a ``bar`` without ``group_by``, and a
    ``gauge`` carrying a ``group_by`` remain gated."""
    if card.viz_type is VizType.KPI:
        return True
    if card.viz_type is VizType.GAUGE:
        return card.group_by is None and len(card.metric_refs) >= 1
    if card.viz_type is VizType.BAR and card.group_by is not None:
        return True
    return False
