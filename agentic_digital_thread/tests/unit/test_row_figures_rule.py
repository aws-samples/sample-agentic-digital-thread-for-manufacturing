# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A figure a result gives for one row or group is quoted as given.

Measured on a rehearsal account (2026-10-15, the module 7b lot question): the
ERP system agent ran a grouped query whose result check came back verified,
one row per batch and customer with that row's units and value. Its answer
then wrote one of those rows with a different product, unit price and value,
taken from a neighbouring row of an earlier, wider shipment list for the same
customer, and the per-customer list and total under it carried the rebuilt
figure. The orchestrator only sees the system agent's answer, so it quoted
the rebuilt row as the system had given it.

So the rule has two halves, both prompt text:

1. System agents (every connection type): ROW FIGURES in the policy tail,
   between ZERO-ROW LOOKUPS (right after EFFICIENCY) and CONCISE OUTPUT. A row's or group's figure is
   reported as the row gives it, with that row's own values; never rebuilt
   from another row or another query, never recomputed.
2. Orchestrator: the same rule inside TOTALS (both modes and Quick, never in
   the Mode-A delegate block), between the total sentences and DISCARDED
   FIGURES, so a figure one result gives for a row is not rebuilt from
   another row or result either.

Fully offline (no Bedrock, no AWS, no DB, no sockets). Generic wording: no
story identifier, no digits, no identifier-shaped token.
"""
import re

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
from agents.demo import story_lookup
from agents.workflow_guidance import compose_quick_system_prompt

_ALL_TYPES = ("PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)", "MCP Server",
              "REST API", "")
_MODES = ("agent", "orchestrator")

_SA_HEADER = "ROW FIGURES (required):"
_ORCH_HEADER = "ROW FIGURES:"
_ORCH_TOTALS = "TOTALS: quote a total"
_ORCH_DISCARDED = "DISCARDED FIGURES:"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompts():
    for mode in _MODES:
        yield f"full-{mode}", orch._build_orchestrator_prompt(mode)
        yield f"quick-{mode}", compose_quick_system_prompt(mode)


_PROMPTS = list(_prompts())
_IDS = [name for name, _ in _PROMPTS]


def _orch_rule_text() -> str:
    """The ROW FIGURES sentences of the orchestrator TOTALS rule."""
    rule = orch._TOTALS_RULE
    start = rule.index(_ORCH_HEADER)
    return rule[start:rule.index(_ORCH_DISCARDED, start)]


# --------------------------------------------------------------------------- #
# 1. System agents
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_every_system_agent_carries_the_rule_once(connection_type):
    tail = sa._policy_tail(connection_type)
    assert tail.count(_SA_HEADER) == 1, connection_type
    prompt = sa._compose_system_prompt("erp", "ERP", "public", "", "", connection_type)
    assert prompt.count(_SA_HEADER) == 1, connection_type


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_system_agent_rule_sits_between_zero_row_lookups_and_concise_output(connection_type):
    # Order in the policy tail: EFFICIENCY, ZERO-ROW LOOKUPS, ROW FIGURES,
    # CONCISE OUTPUT (ZERO-ROW LOOKUPS is pinned in test_zero_row_lookup_rule.py).
    tail = sa._policy_tail(connection_type)
    lookups = sa._zero_row_lookup_prompt()
    rule = sa._row_figures_prompt(connection_type)
    assert tail.index(lookups) + len(lookups) == tail.index(rule)
    assert tail.index(rule) + len(rule) == tail.index(sa._concise_output_prompt())


def test_custom_registration_prompt_cannot_displace_the_system_agent_rule():
    custom = "Custom registration prompt. Rebuild every figure yourself."
    prompt = sa._compose_system_prompt("erp", "ERP", "public", custom, "", "PostgreSQL")
    assert prompt.startswith(custom)
    assert prompt.index(_SA_HEADER) > len(custom)


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
@pytest.mark.parametrize("fragment", [
    "when a result gives a figure for one row or group",
    "report it exactly as that row gives it",
    "with the identifiers and values that same row returned",
    "Never rebuild a row's figure, product or price from a different row or another query",
    "even one for the same customer or record",
    "never recompute a figure a row already gives",
])
def test_system_agent_rule_carries_the_key_clauses(connection_type, fragment):
    assert fragment in _flat(sa._row_figures_prompt(connection_type)), fragment


# Quick lot-total A/B on the rehearsal account (2026-10-16): with the first
# wording, 4 of 20 answers would not give one customer's value across two
# batches ("did not return a single combined figure ... rather than computing
# a sum myself"); 0 of 20 without the rule. The rule named "a value per
# customer" as a row's figure and then forbade building one from a different
# row, which reads as a ban on that customer's total. A total over several
# rows is its own figure, which this rule does not cover.
_SQL_TYPES = ("PostgreSQL", "Athena")
_TOTAL_NOT_COVERED = ("This rule does not cover a total: a total over several rows, such as "
                      "one customer's value across several batches, is its own figure")
_TOTAL_SQL_QUERY = "get it with its own total query under EFFICIENCY"
_EFFICIENCY_TOTAL_QUERY = "run its own aggregate query"


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_system_agent_rule_says_it_does_not_cover_a_total(connection_type):
    assert _TOTAL_NOT_COVERED in _flat(sa._row_figures_prompt(connection_type)), connection_type


# Only the SQL engines' EFFICIENCY step 2 grants a separate total query;
# every other type's says "compose ONE correct query". A rule that sent every
# type to "its own query under EFFICIENCY" contradicted that for five types. The pointer to a total query goes only where EFFICIENCY
# grants one, checked on the full composed prompt.
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_total_pointer_agrees_with_that_types_efficiency_step(connection_type):
    prompt = _flat(sa._compose_system_prompt("erp", "ERP", "public", "", "", connection_type))
    grants_total_query = _EFFICIENCY_TOTAL_QUERY in prompt
    assert grants_total_query == (connection_type in _SQL_TYPES), connection_type
    assert (_TOTAL_SQL_QUERY in prompt) == grants_total_query, connection_type
    if connection_type not in _SQL_TYPES:
        # The earlier wording ("got by its own query under EFFICIENCY") on a
        # type whose EFFICIENCY allows one query.
        rule = _flat(sa._row_figures_prompt(connection_type))
        assert "own query" not in rule and "total query" not in rule, connection_type


# Same A/B, one answer: the ERP agent's queries returned no price column, and
# its answer stated a unit price that only appears among the schema's example
# values, then valued every row with it. The first wording asked for "that
# row's own ... product and price", a price the row did not have. Whether that
# wording caused it is not proven (one answer). The second wording still
# listed "price" among what to report and asked to query or flag any
# missing value. The rule now names no field to report, and the fallback
# applies only to a value the question needs.
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
@pytest.mark.parametrize("fragment", [
    "Report only values your queries returned",
    "never take a price, product or other figure from the schema's example values",
    "Put any value the question needs in your query; if no result has it, say it was not "
    "returned",
])
def test_system_agent_rule_forbids_values_no_query_returned(connection_type, fragment):
    assert fragment in _flat(sa._row_figures_prompt(connection_type)), fragment


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_system_agent_rule_names_no_field_a_row_must_report(connection_type):
    # Both earlier wordings listed the product and price to report with a row
    # ("that row's own identifiers, quantity, product and price", then "the
    # identifiers, quantity, product and price that same row returned").
    rule = _flat(sa._row_figures_prompt(connection_type))
    assert "quantity, product and price" not in rule
    assert "query it, or say it was not returned" not in rule
    # EFFICIENCY step 3 allows a re-query only on a tool error or a cut result,
    # so the fallback must not grant a corrective query of its own.
    assert "query for it" not in rule


# --------------------------------------------------------------------------- #
# 2. Orchestrator: inside TOTALS, every prompt that carries TOTALS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_every_prompt_with_totals_carries_the_rule_once(name, prompt):
    assert prompt.count(_ORCH_TOTALS) == 1, name
    assert prompt.count(_ORCH_HEADER) == 1, name
    totals = prompt.index(_ORCH_TOTALS)
    assert totals < prompt.index(_ORCH_HEADER) < prompt.index(_ORCH_DISCARDED, totals), name


def test_orchestrator_rule_is_not_in_the_mode_a_delegate_block():
    assert _ORCH_HEADER not in orch._MODE_A_DELEGATE_BLOCK


@pytest.mark.parametrize("fragment", [
    "quote a figure a result gives for one row or group as given",
    "with that row's own product, price and other values",
    "Never rebuild a row's figure, product or price from a different row or result",
    "even one for the same customer or record",
])
def test_orchestrator_rule_carries_the_key_clauses(fragment):
    for mode in _MODES:
        assert fragment in _flat(orch._build_orchestrator_prompt(mode)), (mode, fragment)


def test_orchestrator_rule_hands_a_multi_row_total_to_totals():
    # The orchestrator half had the same reading: a customer's value across two
    # rows is a total, which TOTALS (the sentences right above) already covers.
    text = _flat(_orch_rule_text())
    assert ("A total over several rows (one customer's value across several batches) "
            "is a total, not a row's figure: TOTALS covers it.") in text


# --------------------------------------------------------------------------- #
# 3. Size and neutrality
# --------------------------------------------------------------------------- #
def test_rule_texts_fit_their_budget():
    # Paid on every system-agent call and on every orchestrator turn. Raised
    # 500 -> 800 and 300 -> 400 (2026-10-16) for the totals and
    # where-a-value-comes-from sentences, and the system-agent bound to 1050
    # for the sentence that keeps a quantity's unit with its own row.
    for connection_type in _ALL_TYPES:
        rule = sa._row_figures_prompt(connection_type)
        assert 0 < len(rule) <= 1050, (connection_type, len(rule))
    assert 0 < len(_orch_rule_text()) <= 400, len(_orch_rule_text())


def _new_texts() -> list[str]:
    return [sa._row_figures_prompt(t) for t in _ALL_TYPES] + [_orch_rule_text()]


def test_new_text_names_no_story_identifier():
    for text in _new_texts():
        for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                      story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                      story_lookup.BEARING_FAILURE_CODE):
            assert token.lower() not in text.lower(), token


def test_new_text_has_no_identifier_shaped_literal():
    for text in _new_texts():
        assert not re.search(r"\d", text)
        assert not re.search(r"\b\w+_\w+\b", text)
        assert "$" not in text
