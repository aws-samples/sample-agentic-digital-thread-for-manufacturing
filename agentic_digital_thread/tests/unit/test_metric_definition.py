# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure unit tests for the metric-definition artifact (ADR-0066 D1, Task 1A).

Offline and dependency-free: no AWS, no graph, no DB, no LLM. Every test either
exercises a fail-loud clause (asserting the message NAMES the offending field),
pins canonical-JSON determinism, or proves ``to_metric`` yields dataclasses
indistinguishable from hand-built registry entries.

Fixture ids/labels are NEUTRAL placeholders, and every reference to a real
concept or code metric id is DERIVED from the code registry rather than spelled
out — so this file carries no domain literal (ADR-0044 / the neutrality
ratchet).
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess  # nosec B404 - fixed argv, no shell (import-purity probe)
import sys

import pytest

import agents.metric_definition as md
from agents.catalog import CORE_CONCEPTS
from agents.metric_layer import (
    Aggregation,
    CodeMetricLayer,
    ConceptRef,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricRef,
    Scalar,
    validate_metric_layer,
)
from agents.metric_definition import MetricDefinitionError as MDE
from agents.untrusted_text import FENCE_OPEN

# --- Neutral fixture vocabulary -------------------------------------------- #

CONCEPT_A = "concept-a"
CONCEPT_B = "concept-b"
KNOWN_CONCEPTS = frozenset({CONCEPT_A, CONCEPT_B})
KNOWN_METRICS = frozenset({"metric-alpha", "metric-beta", "metric-stored"})
WINDOWS = frozenset({"24h", "7d"})


def _code_metric_ids() -> frozenset[str]:
    return frozenset(m.id for m in CodeMetricLayer().list_metrics())


def direct_doc(**overrides) -> dict:
    doc = {
        "id": "line-utilization",
        "kind": "direct",
        "label": "Line Utilization",
        "synonyms": ["LU", "line util"],
        "unit": "ratio",
        "concept": CONCEPT_A,
        "aggregation": "avg",
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not ...}


def derived_doc(**overrides) -> dict:
    doc = {
        "id": "unit-yield-index",
        "kind": "derived",
        "label": "Unit Yield Index",
        "unit": "ratio",
        "formula": {
            "op": "scale",
            "inputs": [
                {"op": "ratio",
                 "inputs": [{"metric": "metric-alpha"},
                            {"metric": "metric-beta"}]},
                {"value": 100},
            ],
        },
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not ...}


def _validate(doc, **overrides):
    kwargs = {
        "known_concepts": KNOWN_CONCEPTS,
        "known_metric_ids": KNOWN_METRICS,
        "code_metric_ids": frozenset(),
        "window_vocab": WINDOWS,
    }
    kwargs.update(overrides)
    return md.validate_definition(doc, **kwargs)


def _names(exc: pytest.ExceptionInfo, field: str) -> bool:
    return field in str(exc.value)


# --------------------------------------------------------------------------- #
# Happy paths — round trip parse -> validate -> to_metric -> canonical_json
# --------------------------------------------------------------------------- #

def test_direct_document_round_trips():
    doc = direct_doc(valid_range=[0, 100], max_exclusive=True, window="24h",
                     grain="one row per line and shift",
                     authoritative_system="system-a")
    parsed = md.parse_definition(doc)
    _validate(parsed)
    metric = md.to_metric(parsed)
    assert isinstance(metric, DirectMetric)
    assert parsed == md.parse_definition(md.canonical_json(parsed))


def test_derived_document_round_trips():
    doc = derived_doc(prefer_stored="metric-stored",
                      denominator_provenance=CONCEPT_B,
                      synonyms=["UYI"], window="7d")
    parsed = md.parse_definition(doc)
    _validate(parsed)
    metric = md.to_metric(parsed)
    assert isinstance(metric, DerivedMetric)
    assert parsed == md.parse_definition(md.canonical_json(parsed))


def test_parse_accepts_the_canonical_string_form():
    parsed = md.parse_definition(json.dumps(direct_doc()))
    assert parsed["id"] == "line-utilization"


def test_normalization_fills_the_always_present_fields():
    parsed = md.parse_definition(
        {"id": "unit-count", "kind": "direct", "label": "Unit Count",
         "concept": CONCEPT_A, "aggregation": "sum"})
    assert parsed["unit"] == ""
    assert parsed["synonyms"] == []
    assert parsed["max_exclusive"] is False
    # Optional fields stay ABSENT rather than null (the digest must not depend
    # on whether a steward supplied an empty optional).
    for absent in ("window", "grain", "authoritative_system", "valid_range"):
        assert absent not in parsed


def test_text_fields_are_sanitized_not_rejected_for_control_chars():
    parsed = md.parse_definition(direct_doc(label="  Line\tUtilization\r\n"))
    assert parsed["label"] == "Line\tUtilization"


# --------------------------------------------------------------------------- #
# to_metric equivalence with hand-built dataclasses
# --------------------------------------------------------------------------- #

def test_to_metric_direct_equals_hand_built_dataclass():
    doc = direct_doc(valid_range=[0, 100], max_exclusive=True, window="24h",
                     grain="advisory", authoritative_system="system-a")
    built = md.to_metric(doc)
    hand = DirectMetric(
        id="line-utilization",
        concept=CONCEPT_A,
        aggregation=Aggregation.AVG,
        label="Line Utilization",
        unit="ratio",
        window="24h",
        valid_range=(0.0, 100.0),
        max_exclusive=True,
    )
    assert built == hand
    assert repr(built) == repr(hand)
    assert dataclasses.asdict(built) == dataclasses.asdict(hand)


def test_to_metric_derived_equals_hand_built_dataclass():
    doc = derived_doc(prefer_stored="metric-stored",
                      denominator_provenance=CONCEPT_B)
    built = md.to_metric(doc)
    hand = DerivedMetric(
        id="unit-yield-index",
        label="Unit Yield Index",
        unit="ratio",
        formula=Formula(
            FormulaOp.SCALE,
            (
                Formula(FormulaOp.RATIO,
                        (MetricRef("metric-alpha"), MetricRef("metric-beta"))),
                Scalar(100.0),
            ),
        ),
        prefer_stored="metric-stored",
        denominator_provenance=CONCEPT_B,
    )
    assert built == hand
    assert repr(built) == repr(hand)


def test_to_metric_builds_concept_and_scalar_leaves():
    doc = derived_doc(formula={"op": "sum",
                               "inputs": [{"concept": CONCEPT_A},
                                          {"value": 1.5}]})
    built = md.to_metric(doc)
    assert built.formula == Formula(
        FormulaOp.SUM, (ConceptRef(CONCEPT_A), Scalar(1.5)))


def test_definition_side_metadata_never_enters_the_dataclass():
    doc = direct_doc(synonyms=["LU"], grain="advisory",
                     authoritative_system="system-a")
    fields = {f.name for f in dataclasses.fields(md.to_metric(doc))}
    assert {"synonyms", "grain", "authoritative_system"} & fields == set()


def test_a_graph_metric_validates_inside_the_code_layer_unchanged():
    """A definition over a CORE concept, converted by ``to_metric``, passes the
    existing ``validate_metric_layer`` beside the code seed — the proof that the
    resolver consumers need no edits."""
    concept = sorted(CORE_CONCEPTS)[0]
    doc = direct_doc(id="graph-defined-measure", concept=concept,
                     aggregation="sum")
    seed = CodeMetricLayer().list_metrics()
    layer = CodeMetricLayer([*seed, md.to_metric(doc)])
    validate_metric_layer(layer)
    assert "graph-defined-measure" in layer.metrics_for_concept(concept)


def test_a_derived_definition_over_code_refs_validates_in_the_code_layer():
    seed = CodeMetricLayer().list_metrics()
    ref = sorted(m.id for m in seed)[0]
    doc = derived_doc(id="graph-derived-measure",
                      formula={"op": "scale",
                               "inputs": [{"metric": ref}, {"value": 100}]})
    layer = CodeMetricLayer([*seed, md.to_metric(doc)])
    validate_metric_layer(layer)


# --------------------------------------------------------------------------- #
# canonical_json determinism
# --------------------------------------------------------------------------- #

def test_canonical_json_is_key_order_independent():
    doc = direct_doc()
    shuffled = {k: doc[k] for k in reversed(list(doc))}
    assert md.canonical_json(doc) == md.canonical_json(shuffled)


def test_canonical_json_is_idempotent_and_sorted():
    once = md.canonical_json(direct_doc(valid_range=[0, 1]))
    twice = md.canonical_json(md.parse_definition(once))
    assert once == twice
    keys = list(json.loads(once))
    assert keys == sorted(keys)
    assert ", " not in once and ": " not in once   # compact separators


def test_equal_meaning_documents_share_canonical_bytes():
    """Idempotent re-proposal depends on this: a steward supplying an empty
    optional must digest identically to one omitting it."""
    lean = md.canonical_json(direct_doc())
    verbose = md.canonical_json(direct_doc(max_exclusive=False, window=None,
                                           grain="", authoritative_system=""))
    assert lean == verbose


def test_canonical_json_coerces_numbers_deterministically():
    a = md.canonical_json(direct_doc(valid_range=[0, 100]))
    b = md.canonical_json(direct_doc(valid_range=[0.0, 100.0]))
    assert a == b
    scalar_int = md.canonical_json(
        derived_doc(formula={"op": "scale",
                             "inputs": [{"metric": "metric-alpha"},
                                        {"value": 100}]}))
    scalar_float = md.canonical_json(
        derived_doc(formula={"op": "scale",
                             "inputs": [{"metric": "metric-alpha"},
                                        {"value": 100.0}]}))
    assert scalar_int == scalar_float


def test_canonical_json_rejects_an_invalid_document():
    with pytest.raises(MDE):
        md.canonical_json({"id": "x", "kind": "direct"})


# --------------------------------------------------------------------------- #
# parse/shape failures — one test per raise clause, each naming its field
# --------------------------------------------------------------------------- #

def test_non_json_string_fails_naming_document():
    with pytest.raises(MDE) as e:
        md.parse_definition("{not json")
    assert _names(e, "'document'")


def test_json_array_fails_naming_document():
    with pytest.raises(MDE) as e:
        md.parse_definition("[]")
    assert _names(e, "'document'")


def test_non_mapping_input_fails_naming_document():
    with pytest.raises(MDE) as e:
        md.parse_definition(7)
    assert _names(e, "'document'")


def test_validate_of_a_non_mapping_fails_naming_document():
    with pytest.raises(MDE) as e:
        _validate(["not", "a", "document"])
    assert _names(e, "'document'")


@pytest.mark.parametrize("bad", [
    "Line-Utilization",            # uppercase
    "-leading-hyphen",
    "under_score",
    "with space",
    "",
    "a" * 65,                      # over the 64-char cap
    None,
    5,
])
def test_bad_id_fails_naming_id(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(id=bad))
    assert _names(e, "'id'")


def test_unknown_field_fails_naming_that_field():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(nonsense_field="x"))
    assert _names(e, "'nonsense_field'")


@pytest.mark.parametrize("bad", ["Direct", "composite", "", None, 1])
def test_bad_kind_fails_naming_kind(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(kind=bad))
    assert _names(e, "'kind'")


@pytest.mark.parametrize("bad,reason", [
    ("   ", "non-empty"),
    ("x" * (md.MAX_LABEL_LEN + 1), "too_long"),
    ("AKIA" + "A" * 16, "secret_like_content"),
    (FENCE_OPEN + " smuggled", "fence_delimiter"),
    (5, "invalid_input"),
])
def test_bad_label_fails_naming_label(bad, reason):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(label=bad))
    assert _names(e, "'label'") and reason in str(e.value)


def test_synonyms_must_be_a_list():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(synonyms="LU"))
    assert _names(e, "'synonyms'")


def test_too_many_synonyms_fails_naming_synonyms():
    with pytest.raises(MDE) as e:
        md.parse_definition(
            direct_doc(synonyms=[f"s{i}" for i in range(md.MAX_SYNONYMS + 1)]))
    assert _names(e, "'synonyms'") and str(md.MAX_SYNONYMS) in str(e.value)


def test_duplicate_synonym_fails_naming_synonyms():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(synonyms=["LU", "LU"]))
    assert _names(e, "'synonyms'")


@pytest.mark.parametrize("bad", ["", "x" * (md.MAX_SYNONYM_LEN + 1), 3,
                                FENCE_OPEN])
def test_bad_synonym_entry_fails_naming_its_index(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(synonyms=["LU", bad]))
    assert _names(e, "'synonyms[1]'")


def test_over_long_unit_fails_naming_unit():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(unit="u" * (md.MAX_UNIT_LEN + 1)))
    assert _names(e, "'unit'")


def test_over_long_grain_fails_naming_grain():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(grain="g" * (md.MAX_GRAIN_LEN + 1)))
    assert _names(e, "'grain'")


def test_over_long_authoritative_system_fails_naming_that_field():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(
            authoritative_system="s" * (md.MAX_AUTHORITATIVE_SYSTEM_LEN + 1)))
    assert _names(e, "'authoritative_system'")


@pytest.mark.parametrize("field,value", [
    ("formula", {"op": "sum", "inputs": [{"value": 1}]}),
    ("prefer_stored", "metric-stored"),
    ("denominator_provenance", CONCEPT_B),
])
def test_derived_only_field_on_a_direct_definition_fails(field, value):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(**{field: value}))
    assert _names(e, f"'{field}'")


@pytest.mark.parametrize("field,value", [
    ("concept", CONCEPT_A),
    ("aggregation", "sum"),
    ("filter", [{"concept": CONCEPT_A, "op": "is_null"}]),
])
def test_direct_only_field_on_a_derived_definition_fails(field, value):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(**{field: value}))
    assert _names(e, f"'{field}'")


@pytest.mark.parametrize("bad", [None, "", "  ", "with space", "x" * 65, 4])
def test_missing_or_malformed_concept_fails_naming_concept(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(concept=bad))
    assert _names(e, "'concept'")


@pytest.mark.parametrize("bad", [None, "", "average", "SUM", 2])
def test_bad_aggregation_fails_naming_aggregation(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(aggregation=bad))
    assert _names(e, "'aggregation'")


def test_missing_formula_on_derived_fails_naming_formula():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(formula=None))
    assert _names(e, "'formula'")


@pytest.mark.parametrize("bad", ["Metric-Alpha", "with space", "under_score", 9])
def test_bad_prefer_stored_fails_naming_prefer_stored(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(prefer_stored=bad))
    assert _names(e, "'prefer_stored'")


def test_an_empty_optional_means_not_supplied():
    """``None``/``""`` on an OPTIONAL field is absence, not a malformed value —
    which is what keeps equal-meaning documents byte-identical."""
    parsed = md.parse_definition(derived_doc(prefer_stored="", window=None,
                                             denominator_provenance=""))
    for field in ("prefer_stored", "window", "denominator_provenance"):
        assert field not in parsed


def test_denominator_provenance_requires_a_ratio_operation():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            denominator_provenance=CONCEPT_B,
            formula={"op": "sum", "inputs": [{"metric": "metric-alpha"},
                                             {"value": 1}]}))
    assert _names(e, "'denominator_provenance'")
    assert "ratio" in str(e.value)


def test_denominator_provenance_must_be_a_string_id():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(denominator_provenance=5))
    assert _names(e, "'denominator_provenance'")


def test_nested_ratio_satisfies_the_denominator_provenance_rule():
    md.parse_definition(derived_doc(denominator_provenance=CONCEPT_B))


@pytest.mark.parametrize("bad", [
    [0],
    [0, 1, 2],
    "0,1",
    [1, 1],
    [2, 1],
    [float("inf"), 1],
    [0, float("nan")],
    [True, 1],
    ["0", "1"],
])
def test_bad_valid_range_fails_naming_valid_range(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(valid_range=bad))
    assert _names(e, "'valid_range'")


def test_inclusive_lower_bound_is_accepted():
    """Code-registry semantics: the lower bound is INCLUSIVE, so a range whose
    minimum is a legitimate value stays valid."""
    parsed = md.parse_definition(direct_doc(valid_range=[0, 100]))
    assert parsed["valid_range"] == [0.0, 100.0]
    assert md.to_metric(parsed).valid_range == (0.0, 100.0)


def test_max_exclusive_must_be_boolean():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(valid_range=[0, 1], max_exclusive="yes"))
    assert _names(e, "'max_exclusive'")


def test_max_exclusive_requires_valid_range():
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(max_exclusive=True))
    assert _names(e, "'max_exclusive'")
    assert "valid_range" in str(e.value)


def test_upper_bound_is_exclusive_only_when_declared():
    assert md.to_metric(direct_doc(valid_range=[0, 1])).max_exclusive is False
    assert md.to_metric(
        direct_doc(valid_range=[0, 1], max_exclusive=True)).max_exclusive is True


@pytest.mark.parametrize("bad", ["with space", "x" * 65, 24])
def test_malformed_window_fails_naming_window(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc(window=bad))
    assert _names(e, "'window'")


def test_document_over_the_byte_cap_fails_naming_document(monkeypatch):
    """The per-field caps make 8 KiB unreachable in practice — this clause is
    the backstop, so it is pinned against a shrunken cap."""
    monkeypatch.setattr(md, "MAX_DOCUMENT_BYTES", 64)
    with pytest.raises(MDE) as e:
        md.parse_definition(direct_doc())
    assert _names(e, "'document'") and "byte" in str(e.value)


# --------------------------------------------------------------------------- #
# Formula op-tree shape failures
# --------------------------------------------------------------------------- #

def test_formula_must_be_an_object():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(formula=["sum"]))
    assert _names(e, "'formula'")


def test_unknown_formula_node_field_fails_naming_it():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": [{"value": 1}], "extra": 1}))
    assert _names(e, "'formula.extra'")


@pytest.mark.parametrize("bad", ["divide", "SUM", "", None, 1])
def test_bad_formula_op_fails_naming_the_op(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": bad, "inputs": [{"value": 1}]}))
    assert _names(e, "'formula.op'")


@pytest.mark.parametrize("bad", [[], None, "metric-alpha", {}])
def test_empty_or_malformed_inputs_fail_naming_inputs(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(formula={"op": "sum", "inputs": bad}))
    assert _names(e, "'formula.inputs'")


@pytest.mark.parametrize("op", ["ratio", "scale"])
def test_fixed_arity_ops_reject_the_wrong_term_count(op):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": op, "inputs": [{"metric": "metric-alpha"}]}))
    assert _names(e, "'formula.inputs'") and "exactly 2" in str(e.value)


def test_weighted_sum_requires_pairs():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "weighted_sum",
                     "inputs": [{"metric": "metric-alpha"}, {"value": 1},
                                {"metric": "metric-beta"}]}))
    assert _names(e, "'formula.inputs'") and "even" in str(e.value)


def test_weighted_sum_pairs_are_accepted():
    md.parse_definition(derived_doc(
        formula={"op": "weighted_sum",
                 "inputs": [{"metric": "metric-alpha"}, {"value": 0.5},
                            {"metric": "metric-beta"}, {"value": 0.5}]}))


def test_non_object_term_fails_naming_its_path():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": ["metric-alpha"]}))
    assert _names(e, "'formula.inputs[0]'")


def test_term_with_extra_field_fails_naming_it():
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum",
                     "inputs": [{"metric": "metric-alpha", "weight": 2}]}))
    assert _names(e, "'formula.inputs[0].weight'")


@pytest.mark.parametrize("term", [
    {},
    {"metric": "metric-alpha", "concept": CONCEPT_A},
    {"concept": CONCEPT_A, "value": 1},
])
def test_term_must_carry_exactly_one_leaf_key(term):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": [term]}))
    assert _names(e, "'formula.inputs[0]'")


@pytest.mark.parametrize("bad", ["Metric-Alpha", "with space", "", 1])
def test_bad_metric_leaf_fails_naming_its_path(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": [{"metric": bad}]}))
    assert _names(e, "'formula.inputs[0].metric'")


@pytest.mark.parametrize("bad", ["", "with space", 1, None])
def test_bad_concept_leaf_fails_naming_its_path(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": [{"concept": bad}]}))
    assert _names(e, "'formula.inputs[0].concept'")


@pytest.mark.parametrize("bad", ["1", True, None, float("inf"),
                                float("nan")])
def test_bad_scalar_leaf_fails_naming_its_path(bad):
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(
            formula={"op": "sum", "inputs": [{"value": bad}]}))
    assert _names(e, "'formula.inputs[0].value'")


def test_formula_deeper_than_the_cap_fails_naming_the_depth():
    node = {"metric": "metric-alpha"}
    for _ in range(md.MAX_FORMULA_DEPTH + 1):
        node = {"op": "scale", "inputs": [node, {"value": 2}]}
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(formula=node))
    assert "depth" in str(e.value) and "formula" in str(e.value)


def test_formula_at_the_depth_cap_is_accepted():
    node = {"metric": "metric-alpha"}
    for _ in range(md.MAX_FORMULA_DEPTH - 1):
        node = {"op": "scale", "inputs": [node, {"value": 2}]}
    md.parse_definition(derived_doc(formula=node))


def test_too_many_formula_nodes_fails_naming_formula():
    inputs = [{"metric": "metric-alpha"}
              for _ in range(md.MAX_FORMULA_NODES)]
    with pytest.raises(MDE) as e:
        md.parse_definition(derived_doc(formula={"op": "sum",
                                                 "inputs": inputs}))
    assert _names(e, "'formula'") and str(md.MAX_FORMULA_NODES) in str(e.value)


def test_node_count_at_the_cap_is_accepted():
    inputs = [{"metric": "metric-alpha"}
              for _ in range(md.MAX_FORMULA_NODES - 1)]
    md.parse_definition(derived_doc(formula={"op": "sum", "inputs": inputs}))


# --------------------------------------------------------------------------- #
# Cross-reference validation
# --------------------------------------------------------------------------- #

def test_code_metric_id_collision_fails_naming_id():
    code_ids = _code_metric_ids()
    doc = direct_doc(id=sorted(code_ids)[0])
    with pytest.raises(MDE) as e:
        _validate(doc, code_metric_ids=code_ids)
    assert _names(e, "'id'") and "code" in str(e.value)


def test_unknown_direct_concept_fails_naming_concept():
    with pytest.raises(MDE) as e:
        _validate(direct_doc(concept="concept-unmapped"))
    assert _names(e, "'concept'")


def test_unknown_concept_leaf_fails_naming_its_path():
    with pytest.raises(MDE) as e:
        _validate(derived_doc(formula={"op": "sum",
                                       "inputs": [{"concept": "concept-x"}]}))
    assert _names(e, "'formula.inputs[0].concept'")


def test_unresolvable_metric_leaf_fails_naming_its_path():
    with pytest.raises(MDE) as e:
        _validate(derived_doc(formula={"op": "sum",
                                       "inputs": [{"metric": "metric-ghost"}]}))
    assert _names(e, "'formula.inputs[0].metric'")
    assert "does not resolve" in str(e.value)


def test_unresolvable_nested_metric_leaf_fails_naming_its_path():
    with pytest.raises(MDE) as e:
        _validate(derived_doc(formula={
            "op": "scale",
            "inputs": [{"op": "ratio",
                        "inputs": [{"metric": "metric-alpha"},
                                   {"metric": "metric-ghost"}]},
                       {"value": 2}]}))
    assert _names(e, "'formula.inputs[0].inputs[1].metric'")


def test_unresolvable_prefer_stored_fails_naming_prefer_stored():
    with pytest.raises(MDE) as e:
        _validate(derived_doc(prefer_stored="metric-ghost"))
    assert _names(e, "'prefer_stored'")


def test_unknown_denominator_provenance_concept_fails_naming_it():
    with pytest.raises(MDE) as e:
        _validate(derived_doc(denominator_provenance="concept-ghost"))
    assert _names(e, "'denominator_provenance'")


def test_self_referencing_formula_fails_naming_formula():
    doc = derived_doc(id="self-ref-measure",
                      formula={"op": "sum",
                               "inputs": [{"metric": "self-ref-measure"}]})
    with pytest.raises(MDE) as e:
        _validate(doc, known_metric_ids=KNOWN_METRICS | {"self-ref-measure"})
    assert _names(e, "'formula'") and "cycle" in str(e.value)


def test_self_referencing_prefer_stored_fails_naming_prefer_stored():
    doc = derived_doc(id="self-stored-measure",
                      prefer_stored="self-stored-measure")
    with pytest.raises(MDE) as e:
        _validate(doc, known_metric_ids=KNOWN_METRICS | {"self-stored-measure"})
    assert _names(e, "'prefer_stored'") and "cycle" in str(e.value)


def test_cycle_through_a_peer_definition_fails_naming_formula():
    peer = {"id": "peer-measure", "kind": "derived", "label": "Peer",
            "formula": {"op": "ratio",
                        "inputs": [{"metric": "cycling-measure"},
                                   {"value": 2}]}}
    doc = derived_doc(id="cycling-measure",
                      formula={"op": "sum",
                               "inputs": [{"metric": "peer-measure"}]})
    with pytest.raises(MDE) as e:
        _validate(doc, known_metric_ids=KNOWN_METRICS | {"peer-measure"},
                  definitions={"peer-measure": peer})
    assert _names(e, "'formula'") and "cycle" in str(e.value)
    assert "cycling-measure -> peer-measure -> cycling-measure" in str(e.value)


def test_cycle_through_prefer_stored_of_a_peer_is_detected():
    peer = {"id": "peer-stored", "kind": "derived", "label": "Peer",
            "prefer_stored": "cycling-measure",
            "formula": {"op": "sum", "inputs": [{"value": 1}]}}
    doc = derived_doc(id="cycling-measure", prefer_stored="peer-stored")
    with pytest.raises(MDE) as e:
        _validate(doc,
                  known_metric_ids=KNOWN_METRICS | {"peer-stored"},
                  definitions={"peer-stored": peer})
    assert _names(e, "'formula'") and "cycle" in str(e.value)


def test_the_cycle_walk_tolerates_a_partial_peer_document():
    """The closure walk reads PEER documents as-is (they are not re-validated on
    a read path), so a peer with no formula must terminate the walk instead of
    raising."""
    peer = {"id": "peer-partial", "prefer_stored": "cycling-measure"}
    doc = derived_doc(id="cycling-measure",
                      formula={"op": "sum",
                               "inputs": [{"metric": "peer-partial"}]})
    with pytest.raises(MDE) as e:
        _validate(doc, known_metric_ids=KNOWN_METRICS | {"peer-partial"},
                  definitions={"peer-partial": peer})
    assert _names(e, "'formula'") and "cycle" in str(e.value)

    acyclic_peer = {"id": "peer-partial", "label": "Peer"}
    _validate(doc, known_metric_ids=KNOWN_METRICS | {"peer-partial"},
              definitions={"peer-partial": acyclic_peer})


def test_an_acyclic_peer_chain_validates():
    peer = {"id": "peer-measure", "kind": "derived", "label": "Peer",
            "formula": {"op": "sum", "inputs": [{"metric": "metric-alpha"}]}}
    doc = derived_doc(id="chained-measure",
                      formula={"op": "sum",
                               "inputs": [{"metric": "peer-measure"}]})
    _validate(doc, known_metric_ids=KNOWN_METRICS | {"peer-measure"},
              definitions={"peer-measure": peer})


def test_window_outside_the_vocabulary_fails_naming_window():
    with pytest.raises(MDE) as e:
        _validate(direct_doc(window="90d"))
    assert _names(e, "'window'")


def test_a_window_without_a_supplied_vocabulary_fails_closed():
    with pytest.raises(MDE) as e:
        _validate(direct_doc(window="24h"), window_vocab=None)
    assert _names(e, "'window'")


def test_validate_returns_none_on_success():
    assert _validate(direct_doc()) is None


def test_validate_reruns_the_shape_checks():
    """A caller that validates never has to trust that its input was parsed."""
    with pytest.raises(MDE) as e:
        _validate(direct_doc(aggregation="average"))
    assert _names(e, "'aggregation'")


# --------------------------------------------------------------------------- #
# render_formula + diff_definitions (computed, never LLM-judged)
# --------------------------------------------------------------------------- #

def test_render_formula_derived_is_deterministic():
    doc = derived_doc()
    rendered = md.render_formula(doc)
    assert rendered == "scale(ratio(metric-alpha, metric-beta), 100)"
    assert rendered == md.render_formula(md.parse_definition(doc))


def test_render_formula_direct_names_the_aggregation_and_concept():
    assert md.render_formula(direct_doc()) == f"avg(concept:{CONCEPT_A})"


def test_render_formula_renders_concept_leaves_and_fractional_scalars():
    doc = derived_doc(formula={"op": "weighted_sum",
                               "inputs": [{"concept": CONCEPT_A},
                                          {"value": 0.25},
                                          {"metric": "metric-beta"},
                                          {"value": 0.75}]})
    assert md.render_formula(doc) == (
        f"weighted_sum(concept:{CONCEPT_A}, 0.25, metric-beta, 0.75)")


def test_diff_of_a_new_definition_reports_every_field_as_added():
    diff = md.diff_definitions(None, direct_doc())
    assert diff["is_new"] is True
    assert diff["removed"] == {} and diff["changed"] == {}
    assert diff["added"]["id"] == "line-utilization"
    assert diff["formula"]["from"] is None
    assert diff["formula"]["to"] == f"avg(concept:{CONCEPT_A})"
    assert diff["changed_fields"] == sorted(diff["added"])


def test_diff_reports_changed_added_and_removed_fields():
    current = direct_doc(unit="ratio", window="24h", synonyms=["LU"])
    proposed = direct_doc(unit="percent", grain="one row per line",
                          synonyms=["LU"])
    diff = md.diff_definitions(current, proposed)
    assert diff["is_new"] is False
    assert diff["changed"]["unit"] == {"from": "ratio", "to": "percent"}
    assert diff["removed"] == {"window": "24h"}
    assert diff["added"] == {"grain": "one row per line"}
    assert diff["changed_fields"] == ["grain", "unit", "window"]


def test_diff_renders_both_formulas_for_a_formula_change():
    current = derived_doc()
    proposed = derived_doc(formula={"op": "ratio",
                                    "inputs": [{"metric": "metric-alpha"},
                                               {"metric": "metric-beta"}]})
    diff = md.diff_definitions(current, proposed)
    assert diff["formula"]["from"] == "scale(ratio(metric-alpha, metric-beta), 100)"
    assert diff["formula"]["to"] == "ratio(metric-alpha, metric-beta)"
    assert "formula" in diff["changed"]


def test_diff_of_identical_documents_is_empty_and_stable():
    doc = direct_doc()
    reordered = {k: doc[k] for k in reversed(list(doc))}
    diff = md.diff_definitions(doc, reordered)
    assert diff["added"] == {} and diff["removed"] == {}
    assert diff["changed"] == {} and diff["changed_fields"] == []
    assert md.diff_definitions(doc, reordered) == diff        # deterministic
    assert json.dumps(diff, sort_keys=True)                   # JSON-serializable


def test_diff_is_computed_not_judged():
    """Structural pin: the diff carries only field-level facts — no score, no
    verdict, no narrative (ADR-0066: computed, never LLM-judged)."""
    diff = md.diff_definitions(direct_doc(), direct_doc(unit="percent"))
    assert set(diff) == {"is_new", "added", "removed", "changed",
                         "changed_fields", "formula"}


def test_diff_rejects_an_invalid_proposed_document():
    with pytest.raises(MDE):
        md.diff_definitions(None, {"id": "x", "kind": "nonsense"})


# --------------------------------------------------------------------------- #
# Import purity (ADR 0001) — stdlib + import-pure in-repo modules only
# --------------------------------------------------------------------------- #

def test_module_exposes_no_runtime_dependency_symbols():
    for name in ("boto3", "pgdriver", "pg8000", "requests", "botocore"):
        assert not hasattr(md, name)


def test_module_source_has_no_io_or_eval():
    source = pathlib.Path(md.__file__).read_text(encoding="utf-8")
    for forbidden in ("import boto3", "import pg8000", "import pgdriver", "import requests",
                      "eval(", "exec(", "open("):
        assert forbidden not in source, forbidden


def test_imports_with_no_pg8000_or_aws():
    """Fresh interpreter with pg8000/agents.pgdriver/boto3/botocore BLOCKED still
    imports the module and runs the full parse -> validate -> to_metric path."""
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.metric_definition as md\n"
        "doc = md.parse_definition('{\"id\": \"a-measure\", \"kind\": "
        "\"direct\", \"label\": \"A Measure\", \"concept\": \"concept-a\", "
        "\"aggregation\": \"sum\"}')\n"
        "md.validate_definition(doc, known_concepts={'concept-a'}, "
        "known_metric_ids=set(), code_metric_ids=set())\n"
        "assert md.to_metric(doc).id == 'a-measure'\n"
        "assert md.canonical_json(doc).startswith('{\"aggregation\"')\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


def test_no_new_formula_op_or_aggregation_vocabulary():
    """Scope discipline: the closed vocabularies are REUSED from the code
    registry, never extended here."""
    assert md.FORMULA_OPS == frozenset(o.value for o in FormulaOp)
    assert md.AGGREGATIONS == frozenset(a.value for a in Aggregation)
