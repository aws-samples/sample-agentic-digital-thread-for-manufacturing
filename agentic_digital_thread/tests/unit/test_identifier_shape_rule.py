# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier-format questions are answered by value SHAPE, never by a sample.

Measured in an answer eval: asked whether a field carries machine identifiers,
or what identifier format appears in it, SQL system agents sampled rows with
LIMIT or took the top-N raw values by count. The field held about twenty id
shapes and one family made up a few percent of the populated rows, so whether
that family showed up in the answer was luck: a top-20 of raw JSON strings was
filled by a handful of frequent values in two key orders and could never
contain it.

The SQL-engine system prompt therefore carries a short rule: group the
populated values by a normalized shape (digits to N, UUID and long hex runs to
one token) and count each shape. Prompt contract only; fully offline.
"""
from __future__ import annotations

import re

import pytest

import agents.system_agent as sa
from agents.connection_types import ATHENA


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_sql_engines_get_the_identifier_shape_rule(connection_type):
    rule = sa._identifier_shape_prompt(connection_type)
    assert "IDENTIFIER FORMAT (required)" in rule
    low = rule.lower()
    for needle in ("shape", "each digit", "uuid", "count(*)", "every shape",
                   "never answer from a limit sample", "top"):
        assert needle in low, needle
    assert "regexp_replace(" in rule


def test_the_postgresql_example_replaces_every_match():
    # PostgreSQL's regexp_replace replaces only the first match without 'g';
    # Athena (Trino) replaces every match by default.
    assert "'g')" in sa._identifier_shape_prompt("PostgreSQL")
    assert "'g')" not in sa._identifier_shape_prompt(ATHENA)


@pytest.mark.parametrize("connection_type", ["MCP Server", "SiteWise (Direct)",
                                             "SiteWise (i3X)", "REST API", ""])
def test_non_sql_connection_types_get_no_identifier_shape_rule(connection_type):
    assert sa._identifier_shape_prompt(connection_type) == ""
    assert "IDENTIFIER FORMAT" not in sa._policy_tail(connection_type)


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_the_policy_tail_carries_it_after_the_observed_value_shape_rule(connection_type):
    tail = sa._policy_tail(connection_type)
    assert tail.index("OBSERVED VALUE SHAPE (required)") < tail.index(
        "IDENTIFIER FORMAT (required)")


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_the_rule_stays_short_and_plain(connection_type):
    rule = sa._identifier_shape_prompt(connection_type)
    # 900 -> 1000 when the rule became the one template for every format,
    # shape, pattern or family question and said a sample request changes nothing.
    # 1000 -> 1700 when it named the list expansion for each engine, the order
    # and the absent LIMIT, and what a cut or uncomputed result must say.
    assert len(rule) <= 1700
    assert "\u2014" not in rule and "\u2013" not in rule


# --------------------------------------------------------------------------- #
# The normalization the rule gives, executed. Python ``re`` reads these
# patterns (character classes, bounded repeats, a lookahead) the way Trino
# and PostgreSQL do, so applying them in order checks the shapes they yield.
# --------------------------------------------------------------------------- #
_REPLACE_RE = re.compile(r"'([^']+)', '(UUID|HEX|N)'")


def _normalize(connection_type: str, value: str) -> str:
    steps = _REPLACE_RE.findall(sa._identifier_shape_prompt(connection_type))
    assert [token for _pattern, token in steps] == ["UUID", "HEX", "N"]
    for pattern, token in steps:  # innermost regexp_replace first
        value = re.sub(pattern, token, value)
    return value


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
@pytest.mark.parametrize("value, shape", [
    ("be759864-b112-4773-b304-e1cf8986814e", "UUID"),
    ("9f86d081884c7d659a2feaa0c55ad015", "HEX"),
    ("9F86D081884C7D65", "HEX"),
    ("seg_a1b2c3d4e5f6a7b8c9d0", "seg_HEX"),
    ("ROUTING_ABC5_19", "ROUTING_ABCN_NN"),
    ("ABC5-CS6900Z-03", "ABCN-CSNNNNZ-NN"),
    ("M0D2092822052", "MNDNNNNNNNNNN"),
    ("010979851785659321", "N" * 18),  # a long all-digit number is not a hex run
])
def test_the_normalization_collapses_each_identifier_family(connection_type, value, shape):
    assert _normalize(connection_type, value) == shape


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_values_of_one_family_share_one_shape(connection_type):
    hashes = ["9f86d081884c7d659a2feaa0c55ad015", "e3b0c44298fc1c149afbf4c8996fb924"]
    uuids = ["be759864-b112-4773-b304-e1cf8986814e", "2d2e952a-e1a7-4caa-907a-cc73c90f54cd"]
    for family in (hashes, uuids, ["ROUTING_ABC5_19", "ROUTING_ABC5_42"]):
        assert len({_normalize(connection_type, v) for v in family}) == 1


# --------------------------------------------------------------------------- #
# A complete breakdown, or one that says what it covers (answer eval: the
# shape query over list elements was refused, the answer came from a capped
# listing of whole lists, and a mid-sized family was missing).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type, expansion", [
    (ATHENA, "CROSS JOIN UNNEST(CAST(json_parse(col) AS ARRAY(JSON))) AS t(e)"),
    ("PostgreSQL", "CROSS JOIN LATERAL jsonb_array_elements(col::jsonb) AS t(e)"),
])
def test_a_list_value_is_expanded_to_its_elements_in_the_engine_s_own_sql(connection_type, expansion):
    rule = sa._identifier_shape_prompt(connection_type)
    assert expansion in rule
    assert "never the whole list" in rule


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_the_breakdown_is_ordered_by_count_with_no_limit_of_its_own(connection_type):
    rule = sa._identifier_shape_prompt(connection_type)
    assert "GROUP BY the shape ORDER BY COUNT(*) DESC with no LIMIT of your own" in rule


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_a_cut_result_is_never_presented_as_every_format(connection_type):
    rule = sa._identifier_shape_prompt(connection_type)
    sentence = next(s for s in re.split(r"(?<=\.)\s", rule) if "rows were cut" in s)
    for needle in ("most frequent shapes only", "how many shapes it shows",
                   "rarer ones may exist", "never present it as every format"):
        assert needle in sentence, needle


@pytest.mark.parametrize("connection_type", [ATHENA, "PostgreSQL"])
def test_an_uncomputed_full_count_names_the_window_it_covered(connection_type):
    rule = sa._identifier_shape_prompt(connection_type)
    sentence = next(s for s in re.split(r"(?<=\.)\s", rule) if "scan limit" in s)
    assert "narrow the question's window" in sentence
    assert "name the window you covered" in sentence
