# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Answers name records by their business keys, not by UUID row ids.

Observed: event 23 (2026-10-02) and the 2026-10-04 rehearsal answered with a
work order's UUID row id. The event-23 fix lived in one system's
query_guidance, which the system agent that wrote the UUID never reads and
which guidance delivery truncation drops on a nine-system account. The rule
now lives in the standing Step-5 text the answer composer always gets.
"""

import re

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_both_modes_carry_the_rule_right_after_the_window_echo(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(orch._RECORD_KEY_RULE) == 1
    echo_end = (prompt.index(orch._RESOLVED_WINDOW_ECHO_RULE)
                + len(orch._RESOLVED_WINDOW_ECHO_RULE))
    assert prompt.index(orch._RECORD_KEY_RULE) == echo_end
    assert prompt.index("Step 5 — SYNTHESIZE") < echo_end


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_quick_instructions_carry_the_rule(mode):
    assert orch._RECORD_KEY_RULE.strip() in compose_quick_system_prompt(mode)


def test_rule_defaults_prose_to_business_keys():
    rule = " ".join(orch._RECORD_KEY_RULE.split()).lower()
    assert "in prose, name a record by its business key" in rule
    assert "not a uuid row id" in rule


def _exceptions(rule: str) -> list[str]:
    """The conditions listed after 'only when', up to the end of that sentence."""
    flat = re.sub(r"\([^)]*\)", "", " ".join(rule.split()).lower())
    clause = flat.split("only when", 1)[1].split(". ", 1)[0]
    return [c.strip() for c in re.split(r",\s*(?:or\s+)?", clause) if c.strip()]


def test_an_ordinary_cross_system_answer_keeps_internal_ids_out_of_prose():
    """Control: a normal answer that joined two
    systems through an opaque id used that id only inside the orchestrator's
    own tool calls. Nothing in the rule may let it into the prose; every
    exception must come from the user or from a record with no other key."""
    rule = " ".join(orch._RECORD_KEY_RULE.split()).lower()
    assert "ids used only inside tool calls stay out of prose" in rule
    assert "show an id, labelled by role, only when" in rule
    assert "needed to use or verify" not in rule
    conditions = _exceptions(orch._RECORD_KEY_RULE)
    assert conditions == [
        "the user asks for ids or raw",
        "verbatim or audit output",
        "needs it to act in another system",
        "the record has no other key",
    ]


def test_rule_keeps_ids_for_raw_verbatim_or_audit_output():
    """Module 7 asks the chat to "report the raw results" of a graph query;
    raw output must keep its ids rather than rewrite them."""
    rule = " ".join(orch._RECORD_KEY_RULE.split()).lower()
    assert "raw, verbatim or audit output" in rule
    assert "asks for ids" in rule


def test_rule_keeps_an_id_the_user_will_use_elsewhere_labelled_by_role():
    """An Asset's source_id is the telemetry element id a user may look up in
    the telemetry system, even when the asset also has a display name."""
    rule = " ".join(orch._RECORD_KEY_RULE.split()).lower()
    assert "labelled by role" in rule
    assert "the user asks for ids" in rule
    assert "needs it to act in another system (e.g. a telemetry element id to look up)" in rule
    assert "the record has no other key" in rule


def test_rule_names_no_story_identifier_or_system():
    rule = orch._RECORD_KEY_RULE
    assert not re.search(r"\b(?:RUN|LOT|WO|SHIP)-\d", rule)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", rule)
    # Company and site literals are held by test_engine_domain_neutrality,
    # which scans agents/orchestrator.py.
    for name in ("ERP", "MES"):
        assert name not in rule
    assert "\u2014" not in rule and "\u2013" not in rule
