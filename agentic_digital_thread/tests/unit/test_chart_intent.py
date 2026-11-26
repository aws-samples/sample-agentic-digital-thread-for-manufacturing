# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chart intent is computed from the USER's question, once per turn, and
carried to each delegation deterministically - never inferred from the
model-rewritten sub-question.

The runtime's turn prelude computes the bit from the composed user turn and
remembers it in a context variable; a gateway-client wrapper stamps it onto
each system delegation as a RESERVED, gateway-owned tool-call argument
(``_adt_chart_intent``); the system-agent runtime pops that arg at the ASGI
seam into a context variable before any model sees the payload. It never
touches ``kg_context`` and is not a tool parameter, so neither model can
forge it. A false negative only means no chart, never a wrong one.
"""
from __future__ import annotations

import pytest

import agents.chart_intent as ci


# --------------------------------------------------------------------------- #
# 1. wants_chart: bounded, word-boundary, case-insensitive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "show me a chart of vibration",
    "plot the charts",
    "graph the coolant temperature",
    "show the trend over the last hour",
    "what is the trend",
    "give me the history for asset 1",
    "the historical readings please",
    "plot a time series",
    "show vibration over time",
    "CHART IT",
])
def test_wants_chart_true(question):
    assert ci.wants_chart(question) is True


@pytest.mark.parametrize("question", [
    "is line X going to miss today?",
    "is line 2 going to miss today",
    "what is the current vibration",
    "how many machines are down",
    "",
    "the chartreuse paint shop is grapho-centric",   # substrings, not words
    "the order is trending behind schedule",         # business wording, not a chart
    "is vibration trending up?",                      # `trending` is not a chart word
    "show me line 2",
])
def test_wants_chart_false(question):
    assert ci.wants_chart(question) is False


def test_trending_is_not_a_chart_word():
    """`trending` is removed from the bounded set: `trending behind schedule`
    is ordinary business wording, not a request to draw a trend."""
    assert ci.wants_chart("the line is trending behind") is False
    assert ci.wants_chart("show me the trend") is True   # `trend` still matches


# --------------------------------------------------------------------------- #
# 2. The per-turn hand-off: remember / read / reset
# --------------------------------------------------------------------------- #
def test_turn_intent_round_trips():
    ci.reset_turn_intent()
    assert ci.turn_intent() is False
    ci.remember_turn_intent(True)
    assert ci.turn_intent() is True
    ci.reset_turn_intent()
    assert ci.turn_intent() is False


def test_turn_block_sets_intent_from_the_user_question():
    """The prelude computes the bit from the composed USER turn and remembers
    it; a non-chart turn remembers False."""
    ci.reset_turn_intent()
    assert ci.turn_block("plot the vibration trend") is True
    assert ci.turn_intent() is True

    ci.reset_turn_intent()
    assert ci.turn_block("is line 2 going to miss today?") is False
    assert ci.turn_intent() is False


# --------------------------------------------------------------------------- #
# 3. The reserved tool-call argument (deterministic, model-proof)
# --------------------------------------------------------------------------- #
def test_arg_added_only_to_query_tools():
    args = {"question": "q", "kg_context": "ROUTE: x"}
    merged = ci.with_chart_intent_arg("gw___query_sys_x", args, True)
    assert merged[ci.RESERVED_CHART_ARG] == "requested"
    assert merged["kg_context"] == "ROUTE: x"                    # kg_context untouched
    assert args == {"question": "q", "kg_context": "ROUTE: x"}   # caller not mutated

    # Not a query tool -> untouched.
    other = {"question": "q", "kg_context": "c"}
    assert ci.with_chart_intent_arg("gw___list_assets", other, True) == other


def test_arg_states_requested_or_none_and_parses_back():
    req = ci.with_chart_intent_arg("query_sys_x", {"kg_context": ""}, True)
    none = ci.with_chart_intent_arg("query_sys_x", {"kg_context": ""}, False)
    assert ci.parse_chart_intent(req[ci.RESERVED_CHART_ARG]) is True
    assert ci.parse_chart_intent(none[ci.RESERVED_CHART_ARG]) is False
    # Both carry the arg, so a system agent can tell "no chart" from
    # "no arg at all" (a direct/background call with no turn context).
    assert req[ci.RESERVED_CHART_ARG] == "requested"
    assert none[ci.RESERVED_CHART_ARG] == "none"


def test_absent_arg_parses_as_none():
    assert ci.parse_chart_intent(None) is None
    assert ci.parse_chart_intent("") is None
    assert ci.parse_chart_intent("anything else") is None


def test_arg_never_touches_kg_context():
    merged = ci.with_chart_intent_arg(
        "query_sys_x", {"kg_context": "ROUTE:\ntarget_system: sys-x"}, True)
    assert merged["kg_context"] == "ROUTE:\ntarget_system: sys-x"
    assert merged[ci.RESERVED_CHART_ARG] == "requested"
    assert ci.RESERVED_CHART_ARG.startswith("_")                 # not a tool parameter
