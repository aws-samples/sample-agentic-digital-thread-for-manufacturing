# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""": the duration ``measure`` + ``percentiles`` fields of the ADR-0066
metric-definition document: parse / canonicalize / validate / to_metric /
render. Pure, offline, neutral fixture vocabulary only."""
from __future__ import annotations

import json

import pytest

import agents.metric_definition as md
from agents.metric_definition import MetricDefinitionError as MDE
from agents.metric_layer import DirectMetric
from agents.metric_measure_rule import DurationMeasure, FieldRef

MEASURE_CONCEPT = "cycle-time"
TIME = "event-time"
STATE = "widget-color"
TYPE = "widget-size"
INK = "ink-colour"
KNOWN = frozenset({MEASURE_CONCEPT, TIME, STATE, TYPE, INK})

_MEASURE = {
    "kind": "duration",
    "start": {"concept": TIME, "column": "start_ts"},
    "end": {"concept": TIME, "column": "complete_ts"},
    "unit": "hours",
    "clamp_negative": True,
}


def _doc(**overrides) -> dict:
    doc = {
        "id": "tp90-cycle-time",
        "kind": "direct",
        "label": "Cycle Time Percentiles",
        "unit": "hours",
        "concept": MEASURE_CONCEPT,
        "aggregation": "count",
        "measure": _MEASURE,
        "percentiles": [0.9, 0.99],
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not ...}


def _validate(doc, **overrides):
    kwargs = {"known_concepts": KNOWN, "known_metric_ids": frozenset(),
              "code_metric_ids": frozenset(), "window_vocab": {"24h"}}
    kwargs.update(overrides)
    return md.validate_definition(doc, **kwargs)


def _raises_naming(doc, field):
    with pytest.raises(MDE) as e:
        md.parse_definition(doc)
    assert f"'{field}'" in str(e.value), str(e.value)


# --- happy path + canonical form -------------------------------------------- #

def test_measure_document_parses_and_canonicalizes():
    norm = md.parse_definition(_doc())
    assert norm["measure"] == _MEASURE
    assert norm["percentiles"] == [0.9, 0.99]


def test_canonical_bytes_are_percentile_order_independent():
    a = md.canonical_json(_doc(percentiles=[0.99, 0.9]))
    b = md.canonical_json(_doc(percentiles=[0.9, 0.99]))
    assert a == b


def test_measure_metric_builds_direct_dataclass():
    metric = md.to_metric(_doc())
    assert isinstance(metric, DirectMetric)
    assert metric.measure == DurationMeasure(
        kind="duration", start=FieldRef(TIME, "start_ts"),
        end=FieldRef(TIME, "complete_ts"), unit="hours", clamp_negative=True)
    assert metric.percentiles == (0.9, 0.99)
    # concept is retained as the authority / question-matching concept
    assert metric.concept == MEASURE_CONCEPT


def test_measure_with_filter_round_trips():
    doc = _doc(filter=[{"concept": STATE, "op": "eq", "value": "BLUE"},
                       {"concept": TYPE, "op": "eq", "value": "LARGE"}])
    metric = md.to_metric(doc)
    assert metric.measure is not None and len(metric.filter) == 2


# --- rejections ------------------------------------------------------------- #

def test_percentiles_without_a_measure_is_rejected():
    _raises_naming(_doc(measure=...), "percentiles")


def test_measure_without_percentiles_is_rejected():
    # a duration measure is only ever read as a percentile this release
    _raises_naming(_doc(percentiles=...), "percentiles")


@pytest.mark.parametrize("bad", [[], [0.0], [1.0], [0.1] * 6, [0.9, 0.9],
                                 ["0.9"], 0.9, "0.9"])
def test_bad_percentiles_named(bad):
    _raises_naming(_doc(percentiles=bad), "percentiles")


@pytest.mark.parametrize("over,field", [
    (dict(measure={**_MEASURE, "kind": "elapsed"}), "measure"),
    (dict(measure={**_MEASURE, "unit": "days"}), "measure"),
    (dict(measure={**_MEASURE, "start": "start_ts"}), "measure.start"),
    (dict(measure={**_MEASURE, "start": {"concept": TIME}}), "measure.start.column"),
    (dict(measure="duration"), "measure"),
])
def test_bad_measure_named(over, field):
    _raises_naming(_doc(**over), field)


def test_measure_on_a_derived_document_is_rejected():
    doc = {"id": "d-x", "kind": "derived", "label": "D", "unit": "u",
           "formula": {"op": "sum", "inputs": [{"metric": "m-a"}]},
           "measure": _MEASURE}
    _raises_naming(doc, "measure")


# --- cross-reference validation (field-ref concepts must be known) ---------- #

def test_validate_accepts_known_field_ref_concepts():
    assert _validate(_doc()) is None


def test_validate_rejects_unknown_field_ref_concept():
    doc = _doc(measure={**_MEASURE, "end": {"concept": "zzz", "column": "c"}})
    with pytest.raises(MDE) as e:
        _validate(doc)
    assert "zzz" in str(e.value) and "measure" in str(e.value)


# --- render ----------------------------------------------------------------- #

def test_render_formula_shows_measure_and_percentiles():
    rendered = md.render_formula(_doc())
    assert "p90" in rendered and "p99" in rendered
    assert "start_ts" in rendered and "complete_ts" in rendered
    assert "hours" in rendered


def test_render_formula_with_filter_and_group():
    doc = _doc(filter=[{"concept": STATE, "op": "eq", "value": "DONE"}])
    rendered = md.render_formula(doc)
    assert "where" in rendered and STATE in rendered


def test_measureless_direct_document_is_byte_identical():
    doc = {"id": "plain", "kind": "direct", "label": "Plain", "unit": "u",
           "concept": MEASURE_CONCEPT, "aggregation": "count"}
    assert "measure" not in md.canonical_json(doc)
    assert "percentiles" not in md.canonical_json(doc)
    assert md.to_metric(doc).measure is None
    assert md.to_metric(doc).percentiles is None


def test_the_json_text_door_rejects_bad_percentiles():
    _raises_naming(json.dumps(_doc(percentiles=[2.0])), "percentiles")
