# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the concept-driven Work-Orders-by-Status and
Output-by-Line metric/card additions (additive — no existing card/metric is
removed or reworked). Import-pure layers only; no I/O.
"""
from __future__ import annotations

import pytest

from agents.card_layer import (
    VizType,
    get_card_layer,
    validate_card_layer,
)
from agents.catalog import CORE_CONCEPTS
from agents.metric_layer import (
    Aggregation,
    DirectMetric,
    get_metric_layer,
    validate_metric_layer,
)


# ---------------------------------------------------------------------------
# New metric: work-order-count (COUNT over the work-order concept)
# ---------------------------------------------------------------------------

def test_work_order_count_metric_defined():
    m = get_metric_layer().get_metric("work-order-count")
    assert isinstance(m, DirectMetric)
    assert m.concept == "work-order"
    assert m.aggregation is Aggregation.COUNT
    assert m.concept in CORE_CONCEPTS


def test_output_reuses_existing_actual_quantity_sum_metric():
    # The lifetime actual-quantity SUM metric still exists untouched (lineage +
    # oee-quality refs); the Output-by-Line CARD refs its WINDOWED sibling.
    m = get_metric_layer().get_metric("actual-quantity")
    assert isinstance(m, DirectMetric)
    assert m.concept == "actual-quantity"
    assert m.aggregation is Aggregation.SUM


# ---------------------------------------------------------------------------
# New cards: work-orders-by-status + output-by-line (bar + group_by seam)
# ---------------------------------------------------------------------------

def test_work_orders_by_status_card():
    card = get_card_layer().get_card("work-orders-by-status")
    assert card.viz_type is VizType.BAR
    assert card.group_by == "lifecycle-state"
    assert card.metric_refs == ("work-order-count",)
    assert card.group_by in CORE_CONCEPTS


def test_output_by_line_card():
    card = get_card_layer().get_card("output-by-line")
    assert card.viz_type is VizType.BAR
    assert card.group_by == "production-line"
    # Fix-pass re-pin: the card refs the WINDOWED output metric so the backend
    # registry and the frontend output loader agree on ONE metric id (the
    # registry↔loader drift class behind the original quality-panel bug).
    assert card.metric_refs == ("actual-quantity-windowed",)
    assert card.group_by in CORE_CONCEPTS


def test_existing_cards_not_removed():
    ids = {c.id for c in get_card_layer().list_cards()}
    # Additive-only guarantee: every pre-existing seed card is still present.
    for existing in ("spare-parts-on-hand", "oee", "quality-by-line", "throughput",
                     "machine-speed", "scrap-waste", "runtime-hours", "machine-state"):
        assert existing in ids, f"pre-existing card {existing!r} was removed"


def test_machine_state_distribution_still_present():
    # (d) confirmation: the machine-state distribution metric + bar card exist
    # (no rework — a system mapping `machine-state` lights it up).
    m = get_metric_layer().get_metric("machine-state-distribution")
    assert m.concept == "machine-state" and m.aggregation is Aggregation.COUNT
    card = get_card_layer().get_card("machine-state")
    assert card.viz_type is VizType.BAR and card.group_by == "machine-state"


# ---------------------------------------------------------------------------
# Layer validation — the whole registries (incl. the additions) validate
# ---------------------------------------------------------------------------

def test_metric_layer_validates_with_additions():
    validate_metric_layer()  # raises MetricLayerError on any bad ref/concept


def test_card_layer_validates_with_additions():
    validate_card_layer()    # raises CardLayerError on any bad ref/viz gate


# ---------------------------------------------------------------------------
# Coverage satisfiability — the cards light up exactly when the measure
# concept is covered (concept-driven, no dataset/system literals)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric_id,concept", [
    ("work-order-count", "work-order"),
    ("actual-quantity", "actual-quantity"),
    ("actual-quantity-windowed", "actual-quantity"),
])
def test_new_bar_measures_satisfiable_when_concept_covered(metric_id, concept):
    layer = get_metric_layer()
    metric = layer.get_metric(metric_id)
    assert layer.is_satisfiable(metric, frozenset({concept}))
    assert not layer.is_satisfiable(metric, frozenset())
    assert layer.missing_concepts(metric, frozenset()) == frozenset({concept})


# ===========================================================================
# New-metric-cards wave: performance gauge / active-alarms / inspections-by-
# result (+ the two minted concepts). Additive — nothing removed or reworked.
# ===========================================================================

# ---------------------------------------------------------------------------
# Minted concepts: target-machine-speed + inspection-result (generic, field-
# layer, carried through every adjacent catalog map)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cid", ["target-machine-speed", "inspection-result"])
def test_minted_concepts_are_core_field_concepts(cid):
    from agents.catalog import (
        CONCEPT_ISA95_LEVEL,
        CONCEPT_LAYER,
        CONCEPT_NAME,
        CORE_CONCEPT_DEFINITIONS,
    )
    assert cid in CORE_CONCEPTS
    assert CORE_CONCEPT_DEFINITIONS[cid]
    assert CONCEPT_NAME[cid]
    assert CONCEPT_ISA95_LEVEL[cid] in range(0, 5)
    assert CONCEPT_LAYER[cid] == "field"


def test_minted_concepts_have_mes_authority_within_affinity():
    from agents.catalog import CONCEPT_AFFINITY, CONCEPT_AUTHORITY
    for cid in ("target-machine-speed", "inspection-result"):
        assert CONCEPT_AUTHORITY[cid] == "MES"
        assert cid in CONCEPT_AFFINITY["MES"]


def test_machine_speed_description_disambiguates_target():
    # The actual-speed concept no longer claims target fields; the vocabulary
    # points target/rated setpoints at the minted `target-machine-speed`.
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    assert "target_speed" not in CORE_CONCEPT_DEFINITIONS["machine-speed"]
    assert "target-machine-speed" in CORE_CONCEPT_DEFINITIONS["machine-speed"]
    assert "target_speed" in CORE_CONCEPT_DEFINITIONS["target-machine-speed"]


# ---------------------------------------------------------------------------
# New metrics: target-machine-speed, performance-ratio (hybrid derived),
# alarm-event-count (windowed COUNT), inspection-count (COUNT)
# ---------------------------------------------------------------------------

def test_target_machine_speed_metric_defined():
    m = get_metric_layer().get_metric("target-machine-speed")
    assert isinstance(m, DirectMetric)
    assert m.concept == "target-machine-speed"
    assert m.aggregation is Aggregation.AVG


def test_performance_ratio_is_hybrid_derived_over_speeds():
    # Mirrors the `oee` mechanism (ADR 0016 Decision C): prefer_stored wins when
    # the stored `performance` concept resolves; the RATIO(actual/target speed)
    # formula is the derive fallback. Both paths are 0-1 ratios (no SCALE guard).
    from agents.metric_layer import DerivedMetric, FormulaOp, MetricRef
    m = get_metric_layer().get_metric("performance-ratio")
    assert isinstance(m, DerivedMetric)
    assert m.prefer_stored == "performance"
    assert m.unit == "ratio"
    assert m.formula.op is FormulaOp.RATIO
    assert m.formula.inputs == (
        MetricRef("machine-speed"), MetricRef("target-machine-speed"))


def test_performance_ratio_satisfiable_via_either_path():
    # prefer_stored OR-branch: stored `performance` covered ⇒ satisfiable;
    # both speed concepts covered ⇒ satisfiable; neither ⇒ not.
    layer = get_metric_layer()
    m = layer.get_metric("performance-ratio")
    assert layer.is_satisfiable(m, frozenset({"performance"}))
    assert layer.is_satisfiable(
        m, frozenset({"machine-speed", "target-machine-speed"}))
    assert not layer.is_satisfiable(m, frozenset({"machine-speed"}))
    assert not layer.is_satisfiable(m, frozenset())


def test_alarm_event_count_metric_windowed_count_over_failure_mode():
    m = get_metric_layer().get_metric("alarm-event-count")
    assert isinstance(m, DirectMetric)
    assert m.concept == "failure-mode"
    assert m.aggregation is Aggregation.COUNT
    # 24h window via the closed WINDOW_VOCAB (honest lifetime fallback when no
    # event-time mapping exists — same contract as actual-quantity-windowed).
    assert m.window == "24h"
    from agents.metric_dimension import WINDOW_VOCAB
    assert m.window in WINDOW_VOCAB


def test_inspection_count_metric_defined():
    m = get_metric_layer().get_metric("inspection-count")
    assert isinstance(m, DirectMetric)
    assert m.concept == "inspection-result"
    assert m.aggregation is Aggregation.COUNT


# ---------------------------------------------------------------------------
# New cards: performance (gauge), active-alarms (kpi), inspections-by-result
# (bar + co-located group_by, the machine-state-distribution pattern)
# ---------------------------------------------------------------------------

def test_performance_gauge_card():
    card = get_card_layer().get_card("performance")
    assert card.viz_type is VizType.GAUGE
    assert card.group_by is None
    # Ordered like the OEE gauge: composite center first, then sub-readings
    # (actual speed, target speed).
    assert card.metric_refs == (
        "performance-ratio", "machine-speed", "target-machine-speed")


def test_active_alarms_kpi_card():
    from agents.card_layer import ThresholdBand
    card = get_card_layer().get_card("active-alarms")
    assert card.viz_type is VizType.KPI
    assert card.group_by is None
    assert card.metric_refs == ("alarm-event-count",)
    # Lower-is-better bands (the scrap-waste pattern), catch-all red last.
    assert card.thresholds == (
        ThresholdBand(5.0, "green"),
        ThresholdBand(20.0, "orange"),
        ThresholdBand(None, "red"),
    )


def test_inspections_by_result_card():
    card = get_card_layer().get_card("inspections-by-result")
    assert card.viz_type is VizType.BAR
    assert card.group_by == "inspection-result"
    assert card.metric_refs == ("inspection-count",)
    assert card.group_by in CORE_CONCEPTS


def test_new_wave_layers_validate():
    validate_metric_layer()
    validate_card_layer()


@pytest.mark.parametrize("metric_id,concept", [
    ("alarm-event-count", "failure-mode"),
    ("inspection-count", "inspection-result"),
    ("target-machine-speed", "target-machine-speed"),
])
def test_new_wave_direct_measures_satisfiable_when_concept_covered(metric_id, concept):
    layer = get_metric_layer()
    metric = layer.get_metric(metric_id)
    assert layer.is_satisfiable(metric, frozenset({concept}))
    assert not layer.is_satisfiable(metric, frozenset())
    assert layer.missing_concepts(metric, frozenset()) == frozenset({concept})


def test_throughput_already_covers_units_per_interval():
    # The wave's 'throughput' requirement is satisfied by REUSE: the existing
    # `throughput` metric (AVG over the `throughput` concept) + KPI card.
    m = get_metric_layer().get_metric("throughput")
    assert isinstance(m, DirectMetric)
    assert m.concept == "throughput"
    card = get_card_layer().get_card("throughput")
    assert card.viz_type is VizType.KPI
    assert card.metric_refs == ("throughput",)
