# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0075 — the ``filter`` block of the ADR-0066 metric-definition document:
parse / canonicalize / validate / to_metric / render / diff. Pure, offline,
neutral fixture vocabulary only."""
from __future__ import annotations

import pytest

import agents.metric_definition as md
from agents.metric_definition import MetricDefinitionError as MDE
from agents.metric_layer import DirectMetric, Predicate, PredicateOp as Op

MEASURE = "widget-event"
STATE = "widget-color"
TYPE = "widget-size"
KNOWN = frozenset({MEASURE, STATE, TYPE})

_EXAMPLE_FILTER = [
    {"concept": TYPE, "op": "in", "values": ["LARGE"]},
    {"concept": STATE, "op": "eq", "value": "BLUE"},
]


def _doc(**overrides) -> dict:
    doc = {
        "id": "blue-large-widgets",
        "kind": "direct",
        "label": "Blue Large Widgets",
        "unit": "widgets",
        "concept": MEASURE,
        "aggregation": "count",
        "filter": _EXAMPLE_FILTER,
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


# --- Happy path + canonical form -------------------------------------------- #

def test_worked_example_parses_and_canonicalizes_sorted():
    norm = md.parse_definition(_doc())
    assert norm["filter"] == [
        {"concept": STATE, "op": "eq", "value": "BLUE"},
        {"concept": TYPE, "op": "in", "values": ["LARGE"]},
    ]
    assert list(norm).index("filter") == list(norm).index("aggregation") + 1


def test_canonical_bytes_are_clause_and_value_order_independent():
    a = _doc(filter=[
        {"concept": STATE, "op": "eq", "value": "BLUE"},
        {"concept": TYPE, "op": "in", "values": ["B", "A"]},
    ])
    b = _doc(filter=[
        {"concept": TYPE, "op": "in", "values": ["A", "B"]},
        {"concept": STATE, "op": "eq", "value": "BLUE"},
    ])
    assert md.canonical_json(a) == md.canonical_json(b)


def test_numbers_canonicalize_as_floats_and_booleans_stay_booleans():
    norm = md.parse_definition(_doc(filter=[
        {"concept": STATE, "op": "eq", "value": 3},
        {"concept": TYPE, "op": "eq", "value": True},
        {"concept": MEASURE, "op": "in", "values": [2, 1.5]},
    ]))
    by_concept = {c["concept"]: c for c in norm["filter"]}
    assert by_concept[STATE]["value"] == 3.0 and isinstance(by_concept[STATE]["value"], float)
    assert by_concept[TYPE]["value"] is True
    assert by_concept[MEASURE]["values"] == [1.5, 2.0]


def test_null_ops_carry_no_value():
    norm = md.parse_definition(_doc(filter=[
        {"concept": STATE, "op": "is_null"}, {"concept": TYPE, "op": "not_null"}]))
    assert norm["filter"] == [
        {"concept": STATE, "op": "is_null"}, {"concept": TYPE, "op": "not_null"}]


def test_filter_less_direct_document_is_byte_identical():
    doc = _doc(filter=...)
    assert md.canonical_json(doc) == (
        '{"aggregation":"count","concept":"widget-event",'
        '"id":"blue-large-widgets","kind":"direct",'
        '"label":"Blue Large Widgets","max_exclusive":false,'
        '"synonyms":[],"unit":"widgets"}'
    )
    assert md.render_formula(doc) == "count(concept:widget-event)"
    assert md.to_metric(doc).filter is None
    assert md.canonical_json(_doc(filter=None)) == md.canonical_json(doc)


# --- Fail-loud paths name filter[i].<field> ---------------------------------- #

def test_derived_document_with_filter_is_rejected():
    doc = {"id": "d-x", "kind": "derived", "label": "D", "unit": "u",
           "formula": {"op": "sum", "inputs": [{"metric": "m-a"}]},
           "filter": [{"concept": STATE, "op": "is_null"}]}
    _raises_naming(doc, "filter")


def test_latest_with_filter_is_rejected():
    _raises_naming(_doc(aggregation="latest"), "filter")


@pytest.mark.parametrize("bad", [[], "state eq X", {"concept": STATE}, 7])
def test_malformed_filter_container_is_rejected(bad):
    _raises_naming(_doc(filter=bad), "filter")


def test_too_many_clauses_is_rejected():
    clauses = [{"concept": f"c-{i}", "op": "is_null"} for i in range(9)]
    _raises_naming(_doc(filter=clauses), "filter")


def test_unknown_op_names_the_op_path():
    _raises_naming(_doc(filter=[{"concept": STATE, "op": "like", "value": "X%"}]),
                   "filter[0].op")


@pytest.mark.parametrize("clause,field", [
    ({"concept": STATE, "op": "eq"}, "filter[0].value"),
    ({"concept": STATE, "op": "eq", "values": ["A"]}, "filter[0].value"),
    ({"concept": STATE, "op": "in"}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "value": "A"}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": []}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": ["A", 1]}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": ["A", "A"]}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": [True]}, "filter[0].values"),
    ({"concept": STATE, "op": "is_null", "value": "A"}, "filter[0].value"),
    ({"concept": STATE, "op": "not_null", "values": ["A"]}, "filter[0].values"),
    ({"op": "eq", "value": "A"}, "filter[0].concept"),
    ({"concept": "has space", "op": "eq", "value": "A"}, "filter[0].concept"),
    ("not-a-clause", "filter[0]"),
])
def test_clause_shape_failures_name_the_path(clause, field):
    _raises_naming(_doc(filter=[clause]), field)


@pytest.mark.parametrize("hostile", [
    "x'; DROP TABLE t;--", 'a"b', "a;b", "a\\b", "a\nb", "", "x" * 65,
    float("nan"), None,
])
def test_hostile_literal_names_the_value_path(hostile):
    _raises_naming(_doc(filter=[{"concept": STATE, "op": "eq", "value": hostile}]),
                   "filter[0].value")


# Malformed literals are REJECTED naming the path — never a raw TypeError
# (unhashable) or OverflowError (float(10**400)) escaping the parser.
MALFORMED_CLAUSES = [
    ({"concept": STATE, "op": "in", "values": [["x"]]}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": [{"x": 1}]}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": [1, ["x"]]}, "filter[0].values"),
    ({"concept": STATE, "op": "in", "values": [10 ** 400]}, "filter[0].values"),
    ({"concept": STATE, "op": "eq", "value": 10 ** 400}, "filter[0].value"),
    ({"concept": STATE, "op": "eq", "value": 2 ** 53 + 1}, "filter[0].value"),
    ({"concept": STATE, "op": "eq", "value": ["x"]}, "filter[0].value"),
    ({"concept": STATE, "op": "eq", "value": {"x": 1}}, "filter[0].value"),
]


@pytest.mark.parametrize("clause,field", MALFORMED_CLAUSES)
def test_malformed_literal_is_a_named_definition_error(clause, field):
    _raises_naming(_doc(filter=[clause]), field)
    import json
    _raises_naming(json.dumps(_doc(filter=[clause])), field)  # the JSON-text door


def test_canonical_literal_is_total():
    from agents.metric_filter_document import canonical_literal
    assert canonical_literal(3) == 3.0 and isinstance(canonical_literal(3), float)
    assert canonical_literal(10 ** 400) == 10 ** 400  # passed through, then rejected
    assert canonical_literal(True) is True
    assert canonical_literal(["x"]) == ["x"]


def test_duplicate_concept_names_the_second_clause():
    _raises_naming(_doc(filter=[
        {"concept": STATE, "op": "eq", "value": "A"},
        {"concept": STATE, "op": "is_null"},
    ]), "filter[1].concept")


# --- Cross-reference validation ----------------------------------------------- #

def test_validate_accepts_known_predicate_concepts():
    assert _validate(_doc()) is None


def test_validate_rejects_unknown_predicate_concept_by_index():
    doc = _doc(filter=[{"concept": "zzz-unknown", "op": "is_null"},
                       {"concept": STATE, "op": "is_null"}])
    with pytest.raises(MDE) as e:
        _validate(doc)
    # Canonical sort puts STATE first, the unknown second.
    assert "'filter[1].concept'" in str(e.value)
    assert "zzz-unknown" in str(e.value)


# --- to_metric / render / diff / round trip ----------------------------------- #

def test_to_metric_builds_sorted_predicates():
    metric = md.to_metric(_doc())
    assert isinstance(metric, DirectMetric)
    assert metric.filter == (
        Predicate(STATE, Op.EQ, ("BLUE",)),
        Predicate(TYPE, Op.IN, ("LARGE",)),
    )


def test_render_formula_shows_the_predicate():
    assert md.render_formula(_doc()) == (
        "count(concept:widget-event) where widget-color eq "
        "'BLUE' and widget-size in ('LARGE')"
    )
    assert md.render_formula(_doc(filter=[
        {"concept": STATE, "op": "is_null"},
        {"concept": TYPE, "op": "eq", "value": 2},
    ])) == "count(concept:widget-event) where widget-color is_null and widget-size eq 2"


def test_diff_reports_filter_added_and_changed():
    diff = md.diff_definitions(_doc(filter=...), _doc())
    assert "filter" in diff["added"] and "filter" in diff["changed_fields"]
    assert "where" in diff["formula"]["to"] and "where" not in diff["formula"]["from"]
    changed = md.diff_definitions(
        _doc(), _doc(filter=[{"concept": STATE, "op": "eq", "value": "OPEN"}]))
    assert changed["changed_fields"] == ["filter"]


def test_filter_document_round_trip():
    clauses = md.parse_definition(_doc())["filter"]
    assert md.filter_to_document(md.filter_from_document(clauses)) == clauses
    assert md.filter_from_document(None) is None
    assert md.filter_to_document(None) is None


def test_code_collision_preview_document_carries_the_filter():
    from agents import self_learning
    code = md.to_metric(_doc())
    doc = self_learning._code_metric_document(code)
    assert doc["filter"] == md.parse_definition(_doc())["filter"]
    assert md.canonical_json(doc) == md.canonical_json(_doc())
    assert "filter" not in self_learning._code_metric_document(md.to_metric(_doc(filter=...)))
