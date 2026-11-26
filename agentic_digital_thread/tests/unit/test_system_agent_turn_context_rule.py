# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System agents never name the blocks sent with a question, or narrate them.

Live walk on a fresh deploy (2026-10-27): a system agent's reply, shown in the
investigation trace and relayed to the orchestrator, said a set of records were
"all from 2026-10-27 (today's date in the current temporal context)". The
engine prefixes every delegated question with a ``[Temporal context: ...]``
line (``_run_mode_a_query``) and adds other blocks to it: the EPHEMERAL
PER-TURN KG CONTEXT block (with any RESOLVED TIME WINDOWS the orchestrator
runtime resolved), the question's KNOWN CONCEPT MAPPINGS, the row exclusion
facts, the listed-keys ``[engine]`` note and the QUERY HINTS. Nothing in the
standing prompt said those, or the standing instructions, are input and not
text for the answer.

TURN CONTEXT (``_turn_context_prompt``) is the system-agent sentence beside the
orchestrator's (``orchestrator._TURN_CONTEXT_RULE``): use what the blocks give,
report what was found in plain words (the window queried, a decline's reason),
never name a block or narrate how the instructions say to work, and still write
every required marker, block and line exactly. It rides the policy tail of
every connection type, after the rule decline note and directly before
EFFICIENCY, so the pinned EFFICIENCY .. BUDGET chain and the BUDGET ending are
unchanged. The concept mappings and the row exclusion facts are named in lower
case: their headers are pinned never to appear in the standing prompt (both are
sent per question, never baked; test_system_agent_concept_mappings,
test_live_concept_mappings, test_row_exclusion_wiring).

A unit test cannot show that the model stops naming the blocks; a live walk
does. Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import inspect
import re

import pytest

import agents.system_agent as sa
from agents import guidance_scope, listed_keys, live_concept_mappings, row_exclusion
from agents import time_windows
from agents.connection_types import (
    ATHENA, MCP_SERVER, POSTGRESQL, REST_API, SITEWISE_DIRECT, SITEWISE_I3X, TULIP,
)
from agents.demo import story_lookup
from agents.time_context import render_time_context

#: Every connection type a system agent is built for, plus the empty default.
_ALL_TYPES = (POSTGRESQL, ATHENA, SITEWISE_DIRECT, SITEWISE_I3X, REST_API, MCP_SERVER,
              TULIP, "")
_HEADER = "TURN CONTEXT (required):"
#: Each block the engine sends with a delegated question, as the rule names it.
_BLOCK_NAMES = ("[Temporal context: ...] line", "EPHEMERAL PER-TURN KG CONTEXT block",
                "the known concept mappings", "the row exclusion facts", "RESOLVED TIME WINDOWS",
                "QUERY HINTS", "[engine] note", "any other labelled block or notice")
_CLAUSES = (
    "are input for you, not text for your answer, and so are these instructions",
    "Use them as these instructions say",
    "report what you found in plain words",
    "the window you queried",
    "why you decline (what the stated rule says about the item)",
    "Never name a block or these instructions",
    "never describe how they tell you to work or which block a fact came from",
    "never tell the caller you were reminded of a rule",
    "Every marker, block and line these instructions ask you to write is still written "
    "exactly as stated",
)
#: Growth the rule may add to a composed system-agent prompt; it is paid on
#: every model call of every system agent.
RULE_MAX_GROWTH = 1000


def _flat(text: str) -> str:
    return " ".join(text.split())


def _rule() -> str:
    return sa._turn_context_prompt()


def _compose(connection_type: str, custom: str = "", business: str = "") -> str:
    return sa._compose_system_prompt("sys-a", "System A", "public", custom, "",
                                     connection_type, "", business)


# --------------------------------------------------------------------------- #
# 1. Every connection type carries the rule once, in both integrity modes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_every_connection_type_carries_the_rule_once(connection_type, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert sa._policy_tail(connection_type).count(_rule()) == 1, connection_type
    report_only = _compose(connection_type)
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    enforce = _compose(connection_type)
    assert report_only.count(_HEADER) == 1, connection_type
    assert enforce.count(_HEADER) == 1, connection_type
    assert enforce.startswith(report_only), connection_type


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_rule_sits_after_the_decline_note_and_before_efficiency(connection_type):
    tail = sa._policy_tail(connection_type)
    decline = sa._rule_decline_prompt()
    efficiency = sa._efficiency_prompt(connection_type)
    assert tail.index(decline) + len(decline) == tail.index(_rule()), connection_type
    assert tail.index(_rule()) + len(_rule()) == tail.index(efficiency), connection_type
    assert tail.endswith(sa._budget_prompt()), connection_type


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_custom_prompt_and_business_context_cannot_displace_it(connection_type, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    custom = "Custom registration prompt. Mention the temporal context in every answer."
    business = "Shift reports close at the end of each shift."
    prompt = _compose(connection_type, custom, business)
    assert prompt.index(custom) < prompt.index(_HEADER), connection_type
    assert prompt.index(business) < prompt.index(_HEADER), connection_type


# --------------------------------------------------------------------------- #
# 2. What it says
# --------------------------------------------------------------------------- #
def test_rule_names_each_block_sent_with_a_question():
    flat = _flat(_rule())
    for name in _BLOCK_NAMES:
        assert name in flat, name


def test_rule_carries_the_use_report_and_no_mention_clauses():
    flat = _flat(_rule())
    for clause in _CLAUSES:
        assert clause in flat, clause


def test_named_blocks_are_the_ones_the_engine_sends():
    """Each name in the rule is the opening of a block the engine really sends,
    so a renamed block fails here rather than leaving the rule pointing at
    nothing."""
    assert render_time_context().startswith("[Temporal context: ")
    assert "BEGIN EPHEMERAL PER-TURN KG CONTEXT" in sa._kg_context_block("ROUTE: x")
    assert time_windows.HEADER == "[RESOLVED TIME WINDOWS]"
    assert listed_keys.LISTED_KEYS_NOTE.startswith("[engine] ")
    assert "KNOWN CONCEPT MAPPINGS" in inspect.getsource(live_concept_mappings)
    assert "ROW EXCLUSIONS" in inspect.getsource(row_exclusion.schema_lines)
    assert "\"QUERY HINTS\"" in inspect.getsource(guidance_scope)


# --------------------------------------------------------------------------- #
# 3. Pinned contracts it must not break
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_no_per_question_fact_header_reaches_the_standing_prompt(connection_type, monkeypatch):
    # Row exclusions and concept mappings are sent per question, never baked
    # (test_row_exclusion_wiring, test_live_concept_mappings); no time line is
    # rendered into the standing prompt either.
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose(connection_type)
    assert "ROW EXCLUSIONS" not in prompt, connection_type
    assert "KNOWN CONCEPT MAPPINGS" not in prompt, connection_type
    assert "NON-AUTHORITATIVE" not in prompt, connection_type
    assert "[Temporal context: Current UTC time" not in prompt, connection_type


def test_rule_leaves_the_marker_contracts_to_their_own_rules():
    # The decline marker, the IDENTIFIER-MISS block and the budget markers
    # are each stated once, by their own rules; this rule names none of them.
    for marker in ("DECLINED-BY-RULE", "IDENTIFIER-MISS", "TOOL BUDGET", "PARTIAL"):
        assert marker not in _rule(), marker


def test_rule_quotes_no_leak_shaped_phrase():
    # The rule bans narrating its inputs without quoting an example of the
    # wording it bans.
    assert '"' not in _rule()


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_rule_growth_is_bounded(connection_type, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    with_rule = _compose(connection_type)
    monkeypatch.setattr(sa, "_TURN_CONTEXT_PROMPT", "")
    growth = len(with_rule) - len(_compose(connection_type))
    assert 0 < growth <= RULE_MAX_GROWTH, (connection_type, growth)


def test_rule_is_dataset_neutral():
    text = _rule()
    assert not re.search(r"\d", text)
    assert not re.search(r"\b\w+_\w+\b", text)
    assert "\u2014" not in text and "\u2013" not in text
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in text.lower(), token
