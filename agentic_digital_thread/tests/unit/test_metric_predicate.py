# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0075 — the ONE predicate shape/literal rule (``metric_layer``) and the
pure SQL renderer (``metric_predicate``). Binding-row selection moved to the
ONE binding rule (``metric_binding`` — ADR-0076; see test_metric_binding.py).

Pure: no graph, no DB, no network. Neutral fixture vocabulary only.
"""
from __future__ import annotations

import pytest

from agents.metric_layer import (
    MAX_FILTER_CLAUSES,
    MAX_IN_VALUES,
    Predicate,
    PredicateOp as Op,
    check_filter,
    check_predicate,
    check_predicate_literal,
)
from agents.metric_predicate import (
    PredicateBinding,
    render_literal,
    render_predicate_sql,
)


# --- Literal rule ------------------------------------------------------------ #

@pytest.mark.parametrize("value", [
    "BLUE", "A", "x" * 64, "code 7-B", "LARGE", 3, 2.5, -1, 0,
    True, False,
])
def test_accepted_literals(value):
    assert check_predicate_literal(value) is None


@pytest.mark.parametrize("value", [
    "", "x" * 65, "it's", 'say "hi"', "a;b", "back\\slash", "line\nbreak",
    "carriage\rreturn", "tab\there", "\x00", "caf\u00e9",
    float("nan"), float("inf"), float("-inf"), None, [], {}, b"bytes",
])
def test_rejected_literals(value):
    assert check_predicate_literal(value) is not None


@pytest.mark.parametrize("value", [2 ** 53, -(2 ** 53)])
def test_exactly_representable_integer_bounds_are_accepted(value):
    assert check_predicate_literal(value) is None


@pytest.mark.parametrize("value", [2 ** 53 + 1, -(2 ** 53) - 1, 10 ** 400])
def test_non_representable_integers_are_rejected_never_raise(value):
    assert "2^53" in check_predicate_literal(value)


# The rule is TOTAL: malformed (unhashable / nested / huge) values yield a
# problem string, never a raw TypeError or OverflowError.
@pytest.mark.parametrize("pred", [
    Predicate("c", Op.IN, (["x"],)),
    Predicate("c", Op.IN, ({"x": 1},)),
    Predicate("c", Op.IN, (1.0, ["x"])),
    Predicate("c", Op.IN, (10 ** 400,)),
    Predicate("c", Op.IN, (None,)),
    Predicate("c", Op.EQ, (["x"],)),
    Predicate("c", Op.EQ, ({"x": 1},)),
    Predicate("c", Op.EQ, (10 ** 400,)),
])
def test_malformed_values_are_problems_not_exceptions(pred):
    assert isinstance(check_predicate(pred), str)
    assert isinstance(check_filter((pred,)), str)


# --- Clause shape ------------------------------------------------------------- #

def test_valid_clauses_pass():
    assert check_predicate(Predicate("widget-color", Op.EQ, ("BLUE",))) is None
    assert check_predicate(Predicate("widget-color", Op.EQ, (True,))) is None
    assert check_predicate(Predicate("widget-size", Op.IN, ("A", "B"))) is None
    assert check_predicate(Predicate("widget-rank", Op.IN, (1.0, 2.0))) is None
    assert check_predicate(Predicate("widget-note", Op.IS_NULL)) is None
    assert check_predicate(Predicate("widget-note", Op.NOT_NULL)) is None


@pytest.mark.parametrize("pred", [
    Predicate("", Op.EQ, ("A",)),
    Predicate("c", "eq", ("A",)),
    Predicate("c", Op.EQ, ()),
    Predicate("c", Op.EQ, ("A", "B")),
    Predicate("c", Op.EQ, ("bad'lit",)),
    Predicate("c", Op.IN, ()),
    Predicate("c", Op.IN, tuple(f"v{i}" for i in range(MAX_IN_VALUES + 1))),
    Predicate("c", Op.IN, (True, False)),
    Predicate("c", Op.IN, ("A", 1.0)),
    Predicate("c", Op.IN, ("A", "A")),
    Predicate("c", Op.IN, ("A", "x;y")),
    Predicate("c", Op.IS_NULL, ("A",)),
    Predicate("c", Op.NOT_NULL, (1,)),
    Predicate("c", Op.EQ, ["A"]),
    "not-a-predicate",
])
def test_invalid_clauses_are_named(pred):
    assert check_predicate(pred) is not None


def test_filter_rule():
    one = Predicate("a", Op.IS_NULL)
    assert check_filter(None) is None
    assert check_filter((one,)) is None
    assert check_filter(()) is not None
    assert check_filter([one]) is not None
    too_many = tuple(Predicate(f"c{i}", Op.IS_NULL) for i in range(MAX_FILTER_CLAUSES + 1))
    assert "at most" in check_filter(too_many)
    assert "one clause per concept" in check_filter(
        (one, Predicate("a", Op.NOT_NULL)))
    assert check_filter((one, Predicate("b", Op.EQ, ("x'",)))).startswith("filter[1]")


# --- Renderer ------------------------------------------------------------------ #

def _b(column, op, *values, concept="c"):
    return PredicateBinding(concept, column, op, tuple(values))


def test_render_each_op_exactly():
    assert render_predicate_sql([_b("color_code", Op.EQ, "BLUE")]) == \
        "\"color_code\" = 'BLUE'"
    assert render_predicate_sql([_b("size_code", Op.IN, "A", "B")]) == \
        "\"size_code\" IN ('A', 'B')"
    assert render_predicate_sql([_b("note", Op.IS_NULL)]) == '"note" IS NULL'
    assert render_predicate_sql([_b("note", Op.NOT_NULL)]) == '"note" IS NOT NULL'


def test_render_ands_clauses_and_qualifies():
    sql = render_predicate_sql(
        [_b("color_code", Op.EQ, "BLUE", concept="a"),
         _b("rank", Op.IN, 1.0, 2.5, concept="b")],
        qualifier="m.",
    )
    assert sql == "m.\"color_code\" = 'BLUE' AND m.\"rank\" IN (1, 2.5)"


def test_render_literals():
    assert render_literal(True) == "TRUE"
    assert render_literal(False) == "FALSE"
    assert render_literal(1.0) == "1"
    assert render_literal(7) == "7"
    assert render_literal(0.25) == "0.25"
    assert render_literal("X") == "'X'"


@pytest.mark.parametrize("hostile", [
    "x'; DROP TABLE t;--", "x' OR '1'='1", "a\\", "a\nb", 'a"b',
])
def test_injection_shaped_literal_raises_before_any_sql(hostile):
    with pytest.raises(ValueError):
        render_literal(hostile)
    with pytest.raises(ValueError):
        render_predicate_sql([_b("color_code", Op.EQ, hostile)])
    with pytest.raises(ValueError):
        render_predicate_sql([_b("color_code", Op.IN, "OK", hostile)])


def test_accepted_string_literal_never_carries_an_inner_quote():
    sql = render_predicate_sql([_b("c", Op.IN, "A-1", "B 2", "C_3")])
    inner = sql.split("IN (", 1)[1].rstrip(")")
    assert inner.count("'") == 6


@pytest.mark.parametrize("column", ["", "1col", "bad-col", 'x"; --', "a..b",
                                    ".a", "a.", "a.b-c", 'a."b'])
def test_invalid_identifier_raises(column):
    with pytest.raises(ValueError):
        render_predicate_sql([_b(column, Op.IS_NULL)])


def test_dotted_column_renders_as_one_quoted_identifier():
    """ADR-0076 D4: a Glue-flattened struct member is ONE physical column."""
    assert render_predicate_sql([_b("payload.color_code", Op.IS_NULL)]) == (
        '"payload.color_code" IS NULL')
    assert render_predicate_sql([_b("a.b.c", Op.EQ, "X")], qualifier="m.") == (
        "m.\"a.b.c\" = 'X'")


def test_empty_bindings_and_unknown_qualifier_raise():
    with pytest.raises(ValueError):
        render_predicate_sql([])
    with pytest.raises(ValueError):
        render_predicate_sql([_b("c", Op.IS_NULL)], qualifier="d.")
