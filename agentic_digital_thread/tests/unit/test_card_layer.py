# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the S2.3 card layer (ADR 0018).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane. Mirrors tests/unit/test_metric_layer.py.
"""
from __future__ import annotations

import dataclasses

import pytest

import agents.card_layer
from agents.card_layer import (
    Card,
    CardLayer,
    CardLayerError,
    CodeCardLayer,
    ThresholdBand,
    VizType,
    get_card_layer,
    validate_card_layer,
)


def test_card_shape_and_frozen():
    c = Card(
        id="c1", title="T", metric_refs=("m1", "m2"), viz_type=VizType.KPI,
    )
    assert c.id == "c1"
    assert c.title == "T"
    assert c.metric_refs == ("m1", "m2")
    assert isinstance(c.metric_refs, tuple)
    assert c.viz_type is VizType.KPI
    # group_by defaults to None
    assert c.group_by is None
    # optional declarative presentation seams default to None (backward-compat)
    assert c.thresholds is None
    assert c.target is None
    # immutable (frozen)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.title = "x"  # type: ignore[misc]


def test_viz_type_vocabulary():
    assert {v.value for v in VizType} == {"kpi", "bar", "line", "gauge"}


def test_get_card_layer_is_default_and_memoized():
    layer = get_card_layer()
    assert isinstance(layer, CardLayer)
    assert isinstance(layer, CodeCardLayer)
    # memoized via lru_cache → same instance
    assert get_card_layer() is layer


def test_seed_registry_contains_spare_parts_and_oee():
    layer = get_card_layer()
    ids = {c.id for c in layer.list_cards()}
    assert "spare-parts-on-hand" in ids
    assert "oee" in ids
    assert "quality-by-line" in ids

    spare = layer.get_card("spare-parts-on-hand")
    assert spare.viz_type is VizType.KPI
    assert spare.metric_refs == ("spare-parts-on-hand",)

    # oee is now a GAUGE: composite OEE first, then its three sub-readings
    # (S5.3 §1). No group_by (gauge renders scalar values).
    oee = layer.get_card("oee")
    assert oee.viz_type is VizType.GAUGE
    assert oee.group_by is None
    assert oee.metric_refs == ("oee", "availability", "performance", "oee-quality")

    # quality-by-line is a BAR carrying a group_by dimension concept (S5.3 §6.3),
    # resolved client-side via /api/metrics/resolve-by-dimension.
    qbl = layer.get_card("quality-by-line")
    assert qbl.viz_type is VizType.BAR
    assert qbl.group_by == "production-line"
    # Now uses quality-yield-rate (0-100 rate), NOT quality-rate (good-count)
    assert qbl.metric_refs == ("quality-yield-rate",)

def test_seed_registry_contains_ot_native_kpi_cards():
    # The four abstract OT-native KPI cards: each is a VizType.KPI card whose
    # single metric_ref matches the metric id (== concept id), so any system
    # covering the concept lights up the card. No group_by (scalar KPIs).
    layer = get_card_layer()
    expected = {
        "throughput": "Throughput",
        "machine-speed": "Machine Speed",
        "scrap-waste": "Scrap / Waste",
        "runtime-hours": "Runtime Hours",
    }
    for card_id, title in expected.items():
        card = layer.get_card(card_id)
        assert card.viz_type is VizType.KPI
        assert card.group_by is None
        assert card.title == title
        assert card.metric_refs == (card_id,)
    # The seed layer (now including the OT-native cards) validates.
    assert validate_card_layer() is None


def test_seed_ot_cards_carry_thresholds_and_targets():
    # PART A: the OT KPI cards carry the OPTIONAL declarative presentation seams
    # where meaningful — scrap-waste has lower-is-better green/orange/red bands;
    # throughput/machine-speed/runtime-hours carry a numeric actual-vs-target.
    layer = get_card_layer()

    scrap = layer.get_card("scrap-waste")
    assert scrap.target is None
    assert scrap.thresholds == (
        ThresholdBand(500.0, "green"),
        ThresholdBand(1000.0, "orange"),
        ThresholdBand(None, "red"),
    )
    # ordered by increasing cutoff, catch-all last
    assert [b.color for b in scrap.thresholds] == ["green", "orange", "red"]

    for card_id, target in (("throughput", 1000.0), ("machine-speed", 60.0),
                            ("runtime-hours", 168.0)):
        card = layer.get_card(card_id)
        assert card.target == target
        assert card.thresholds is None

    # The seed layer with thresholds/targets attached still validates.
    assert validate_card_layer() is None


def test_seed_registry_contains_2a_by_line_cards():
    # 2A: two additional generically-satisfiable by-line breakdowns — each
    # reuses an EXISTING DirectMetric (scrap-waste / throughput) grouped by the
    # production-line dimension via the same bar + group_by seam as
    # quality-by-line. Coverage-gated automatically; no dataset literals.
    layer = get_card_layer()
    for card_id, title, ref in (
        ("scrap-by-line", "Scrap by Line", "scrap-waste"),
        ("throughput-by-line", "Throughput by Line", "throughput"),
    ):
        card = layer.get_card(card_id)
        assert card.viz_type is VizType.BAR
        assert card.title == title
        assert card.group_by == "production-line"
        assert card.metric_refs == (ref,)
        assert card.thresholds is None
        assert card.target is None
    # The extended seed layer still validates.
    assert validate_card_layer() is None


def test_seed_registry_contains_machine_state_bar_card():
    # PART B: the machine-state card is the FEASIBLE honest representation — the
    # CURRENT machine-state distribution as a `bar` grouped by the machine-state
    # dimension (count per current state), resolved client-side via
    # /api/metrics/resolve-by-dimension (same seam as quality-by-line). NOT a
    # time-series state-timeline (would need a charting lib we do not bundle).
    layer = get_card_layer()
    card = layer.get_card("machine-state")
    assert card.viz_type is VizType.BAR
    assert card.group_by == "machine-state"
    assert card.metric_refs == ("machine-state-distribution",)
    assert card.thresholds is None
    assert card.target is None
    # It validates as a bar+group_by card.
    assert validate_card_layer() is None



def test_get_card_known_and_unknown():
    layer = get_card_layer()
    card = layer.get_card("spare-parts-on-hand")
    assert isinstance(card, Card)
    with pytest.raises(KeyError):
        layer.get_card("does-not-exist")


def test_validate_happy_path():
    assert validate_card_layer() is None


def test_validate_bad_metric_ref_raises():
    layer = CodeCardLayer([
        Card("broken", "x", ("nope-metric",), VizType.KPI),
    ])
    with pytest.raises(CardLayerError, match="broken"):
        validate_card_layer(layer)


def test_validate_reserved_viz_raises():
    # `bar` is declared but gated — only `kpi` is implemented (Decision C). The
    # metric_ref resolves, so validation reaches the reserved-viz guard.
    layer = CodeCardLayer([
        Card("bar-card", "x", ("spare-parts-on-hand",), VizType.BAR),
    ])
    with pytest.raises(CardLayerError, match="bar-card"):
        validate_card_layer(layer)


def test_validate_duplicate_ids_raises():
    # CodeCardLayer dedups by id at construction; a non-deduping layer surfaces
    # the duplicate so validate_card_layer's duplicate guard is exercised.
    class _DupLayer(CardLayer):
        def __init__(self, cards):
            self._cards = list(cards)

        def get_card(self, card_id):
            for c in self._cards:
                if c.id == card_id:
                    return c
            raise KeyError(card_id)

        def list_cards(self):
            return list(self._cards)

    layer = _DupLayer([
        Card("dup-id", "a", ("spare-parts-on-hand",), VizType.KPI),
        Card("dup-id", "b", ("oee",), VizType.KPI),
    ])
    with pytest.raises(CardLayerError, match="dup-id"):
        validate_card_layer(layer)


def test_module_is_import_pure():
    # No DB/LLM/I-O symbol leaked into the module namespace (ADR 0001). The real
    # guarantee is that the source imports nothing with I/O.
    assert not hasattr(agents.card_layer, "boto3")
    assert not hasattr(agents.card_layer, "neptune_query")


# --------------------------------------------------------------------------
# cards_for_metric reverse index (S3.2 / W1.3) — metric_id → cards whose
# metric_refs contain it, in list_cards() order. Concrete on the CardLayer ABC.
# --------------------------------------------------------------------------

def test_cards_for_metric_oee():
    layer = get_card_layer()
    cards = layer.cards_for_metric("oee")
    assert [c.id for c in cards] == ["oee"]
    assert all(isinstance(c, Card) for c in cards)


def test_cards_for_metric_spare_parts():
    layer = get_card_layer()
    cards = layer.cards_for_metric("spare-parts-on-hand")
    assert [c.id for c in cards] == ["spare-parts-on-hand"]


def test_cards_for_metric_quality_rate_referenced_by_gauge_and_bar():
    # `quality-rate` (the direct good-count AVG) is NOT referenced by any card anymore
    # (quality-by-line now uses quality-yield-rate 0-100 rate instead).
    # The `oee` gauge's quality sub-reading is the derived 0-100 yield `oee-quality`,
    # so the reverse index for that metric returns the gauge.
    layer = get_card_layer()
    assert [c.id for c in layer.cards_for_metric("quality-rate")] == []
    assert [c.id for c in layer.cards_for_metric("oee-quality")] == ["oee"]
    # quality-yield-rate is now used by quality-by-line
    assert [c.id for c in layer.cards_for_metric("quality-yield-rate")] == ["quality-by-line"]


def test_cards_for_metric_unknown_is_empty():
    layer = get_card_layer()
    assert layer.cards_for_metric("nope") == []


def test_cards_for_metric_preserves_list_order():
    # Two cards referencing the same metric come back in list_cards() order.
    layer = CodeCardLayer([
        Card("c-a", "A", ("spare-parts-on-hand",), VizType.KPI),
        Card("c-b", "B", ("spare-parts-on-hand",), VizType.KPI),
    ])
    assert [c.id for c in layer.cards_for_metric("spare-parts-on-hand")] == ["c-a", "c-b"]


def test_cards_for_metric_concrete_on_abc():
    # The ABC method is CONCRETE — a minimal subclass implementing only the two
    # abstract methods inherits a working cards_for_metric (mirrors _DupLayer).
    class _MinLayer(CardLayer):
        def __init__(self, cards):
            self._c = list(cards)

        def get_card(self, card_id):
            for c in self._c:
                if c.id == card_id:
                    return c
            raise KeyError(card_id)

        def list_cards(self):
            return list(self._c)

    layer = _MinLayer([Card("only", "O", ("m1",), VizType.KPI)])
    assert [c.id for c in layer.cards_for_metric("m1")] == ["only"]
    assert layer.cards_for_metric("m2") == []


# --------------------------------------------------------------------------
# Wave 1 seam: `bar` + `group_by` validation (S5.3 §6.3). A card may declare
# viz_type `bar` WHEN it also carries a `group_by` (a dimension concept id).
# `kpi` is unchanged; `bar` WITHOUT group_by, and `line`/`gauge`, stay gated.
# --------------------------------------------------------------------------

def test_validate_bar_with_group_by_accepted():
    layer = CodeCardLayer([
        Card("quality-by-line", "Quality by Line", ("spare-parts-on-hand",),
             VizType.BAR, group_by="production-line"),
    ])
    assert validate_card_layer(layer) is None


def test_validate_bar_without_group_by_rejected():
    layer = CodeCardLayer([
        Card("bar-nogroup", "x", ("spare-parts-on-hand",), VizType.BAR),
    ])
    with pytest.raises(CardLayerError, match="bar-nogroup"):
        validate_card_layer(layer)


def test_validate_line_with_group_by_still_rejected():
    # The seam un-gates BAR only — LINE with a group_by is still not implemented.
    layer = CodeCardLayer([
        Card("line-card", "x", ("spare-parts-on-hand",),
             VizType.LINE, group_by="production-line"),
    ])
    with pytest.raises(CardLayerError, match="line-card"):
        validate_card_layer(layer)


def test_validate_gauge_with_metric_refs_accepted():
    # The OEE gauge seam (S5.3 §1): a gauge with >=1 metric_ref and NO group_by
    # is implemented (composite + sub-readings). Note: the actual OEE gauge uses
    # oee-quality (derived) for quality, not quality-rate (direct).
    layer = CodeCardLayer([
        Card("oee-gauge", "OEE", ("oee", "availability", "performance",
                                  "oee-quality"), VizType.GAUGE),
    ])
    assert validate_card_layer(layer) is None


def test_validate_gauge_with_group_by_rejected():
    # A gauge carrying a group_by is NOT a by-dimension card — still gated.
    layer = CodeCardLayer([
        Card("gauge-grouped", "x", ("oee",), VizType.GAUGE,
             group_by="production-line"),
    ])
    with pytest.raises(CardLayerError, match="gauge-grouped"):
        validate_card_layer(layer)


def test_validate_gauge_without_metric_refs_rejected():
    # A gauge needs at least one metric_ref to render a value.
    layer = CodeCardLayer([
        Card("gauge-empty", "x", (), VizType.GAUGE),
    ])
    with pytest.raises(CardLayerError, match="gauge-empty"):
        validate_card_layer(layer)


def test_validate_bar_group_by_still_checks_metric_refs():
    # The bar+group_by acceptance does NOT bypass the metric-ref resolution guard.
    layer = CodeCardLayer([
        Card("bad-bar", "x", ("nope-metric",), VizType.BAR, group_by="production-line"),
    ])
    with pytest.raises(CardLayerError, match="bad-bar"):
        validate_card_layer(layer)


# --------------------------------------------------------------------------
# PART A — optional declarative presentation (thresholds/target) validation.
# Absent fields are always valid; present fields are shape-checked.
# --------------------------------------------------------------------------

def test_validate_thresholds_and_target_happy():
    layer = CodeCardLayer([
        Card("t", "T", ("spare-parts-on-hand",), VizType.KPI,
             thresholds=(ThresholdBand(1.0, "green"), ThresholdBand(2.0, "orange"),
                         ThresholdBand(None, "red")),
             target=5.0),
    ])
    assert validate_card_layer(layer) is None


def test_validate_empty_thresholds_rejected():
    layer = CodeCardLayer([
        Card("empty-th", "x", ("spare-parts-on-hand",), VizType.KPI, thresholds=()),
    ])
    with pytest.raises(CardLayerError, match="empty-th"):
        validate_card_layer(layer)


def test_validate_threshold_band_empty_color_rejected():
    layer = CodeCardLayer([
        Card("bad-color", "x", ("spare-parts-on-hand",), VizType.KPI,
             thresholds=(ThresholdBand(1.0, ""),)),
    ])
    with pytest.raises(CardLayerError, match="bad-color"):
        validate_card_layer(layer)


def test_validate_threshold_catchall_must_be_last():
    # A value=None catch-all band anywhere but the final position is rejected.
    layer = CodeCardLayer([
        Card("early-catchall", "x", ("spare-parts-on-hand",), VizType.KPI,
             thresholds=(ThresholdBand(None, "green"), ThresholdBand(1.0, "red"))),
    ])
    with pytest.raises(CardLayerError, match="early-catchall"):
        validate_card_layer(layer)


def test_validate_threshold_cutoffs_must_be_non_decreasing():
    layer = CodeCardLayer([
        Card("unordered", "x", ("spare-parts-on-hand",), VizType.KPI,
             thresholds=(ThresholdBand(10.0, "green"), ThresholdBand(5.0, "red"))),
    ])
    with pytest.raises(CardLayerError, match="unordered"):
        validate_card_layer(layer)


def test_validate_non_numeric_target_rejected():
    layer = CodeCardLayer([
        Card("bad-target", "x", ("spare-parts-on-hand",), VizType.KPI,
             target="lots"),  # type: ignore[arg-type]
    ])
    with pytest.raises(CardLayerError, match="bad-target"):
        validate_card_layer(layer)
