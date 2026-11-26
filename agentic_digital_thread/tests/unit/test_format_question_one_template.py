# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A question about the format of a field's values gets ONE SQL template.

Measured in an answer eval: asked what identifier format appears in a field,
five of five SQL system agents grouped the raw values and kept the top rows by
count, so a mid-sized identifier family never reached the answer. Two required
rules in the composed prompt both claimed the question. OBSERVED VALUE SHAPE
named "format, shape, pattern, family" and gave a GROUP BY raw value ORDER BY
COUNT(*) DESC template; IDENTIFIER FORMAT, after it, grouped by a normalized
shape. The first carried the concrete template and was followed every time.

Pinned on the composed system prompt of every connection type that renders an
SQL template: exactly one rule's opening clause names a value format, that rule
groups by a normalized shape, and the raw-value top-N template never sits under
a format clause. A caller asking for a sample or examples changes nothing, and
the Athena LIMIT step for exploratory reads points format questions back to the
grouping rules. Prompt contract only; fully offline.
"""
from __future__ import annotations

import re

import pytest

import agents.system_agent as sa
from agents import connection_types as ct

_ALL_TYPES = (ct.POSTGRESQL, ct.ATHENA, ct.SITEWISE_DIRECT, ct.SITEWISE_I3X,
              ct.REST_API, ct.MCP_SERVER, ct.TULIP, "")
_TABLES = "table_x (col_a varchar, col_b varchar, col_c timestamp)"
_RAW_TOP_N = "GROUP BY target_column ORDER BY COUNT(*) DESC"
_FORMAT_WORD = re.compile(r"\b(?:formats?|shapes?|patterns?|famil(?:y|ies))\b", re.I)
_RULE = re.compile(r"([A-Z][A-Z0-9 /-]*[A-Z]) \((?:required|SERVER-ENFORCED)[^)]*\):\s*(.*)",
                   re.S)


def _prompt(connection_type: str) -> str:
    return sa._compose_system_prompt(
        system_id="system-a", system_name="System A", schema="schema_a",
        system_prompt="", tables_desc=_TABLES, connection_type=connection_type)


def _rules(prompt: str) -> dict[str, str]:
    """``{rule name: body}`` for every ``NAME (required): ...`` paragraph."""
    rules = {}
    for block in prompt.split("\n\n"):
        match = _RULE.match(block.strip())
        if match:
            rules[match.group(1)] = match.group(2)
    return rules


def _opening(body: str) -> str:
    """The clause that says when a rule applies: up to its first colon or full stop."""
    return re.split(r"[:.](?:\s|$)", body, maxsplit=1)[0]


def _sql_template_rules(connection_type: str) -> dict[str, str]:
    """Rules that spell out a grouping query (a SELECT and a GROUP BY)."""
    return {name: body for name, body in _rules(_prompt(connection_type)).items()
            if re.search(r"\bSELECT\b", body) and "GROUP BY" in body}


_SQL_TYPES = [t for t in _ALL_TYPES if _sql_template_rules(t)]


def test_the_sql_engines_are_found():
    # Guards the parametrization below against going vacuous.
    assert {ct.ATHENA, ct.POSTGRESQL} <= set(_SQL_TYPES)


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_one_rule_owns_the_format_question(connection_type):
    owners = [name for name, body in _sql_template_rules(connection_type).items()
              if _FORMAT_WORD.search(_opening(body))]
    assert owners == ["IDENTIFIER FORMAT"]


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_that_rule_groups_by_a_normalized_shape(connection_type):
    body = _rules(_prompt(connection_type))["IDENTIFIER FORMAT"]
    assert "regexp_replace(" in body
    assert "GROUP BY the shape" in body
    assert "every shape with its count" in body


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_no_format_clause_carries_the_raw_value_top_n_template(connection_type):
    for name, body in _rules(_prompt(connection_type)).items():
        if _RAW_TOP_N in body:
            assert not _FORMAT_WORD.search(_opening(body)), name


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_observed_value_shape_keeps_distinct_values_and_defers_formats(connection_type):
    body = _rules(_prompt(connection_type))["OBSERVED VALUE SHAPE"]
    assert "distinct values" in _opening(body)
    assert _RAW_TOP_N in body
    assert "IDENTIFIER FORMAT" in body


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_a_sample_request_does_not_license_a_limit_or_top_n(connection_type):
    body = _rules(_prompt(connection_type))["IDENTIFIER FORMAT"].lower()
    sentence = next(s for s in re.split(r"(?<=\.)\s", body) if "limit sample" in s)
    assert "sample or examples" in sentence and "top rows" in sentence
    assert "examples from the shape groups" in body


def test_the_athena_limit_step_hands_format_questions_to_the_grouping_rules():
    body = _rules(_prompt(ct.ATHENA))["QUERY EFFICIENCY"]
    step = next(line for line in body.splitlines() if "LIMIT on exploratory reads" in line)
    assert "exploratory reads that inspect rows." in step
    assert "or values" not in step
    assert "OBSERVED VALUE SHAPE" in step and "IDENTIFIER FORMAT" in step


@pytest.mark.parametrize("connection_type", _SQL_TYPES)
def test_the_two_rules_stay_plain(connection_type):
    rules = _rules(_prompt(connection_type))
    for name in ("IDENTIFIER FORMAT", "OBSERVED VALUE SHAPE"):
        assert "\u2014" not in rules[name] and "\u2013" not in rules[name], name
