# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two orchestrator rules: an absence claim rests on a lookup, and a decline is final.

ABSENCE: answers said a named entity (a cell with no telemetry) was absent from
a system without asking that system this turn. The routing coverage is
onboarding-time graph state, and a WITHHELD metric-layer block read as "nothing
to fetch", so most runs made no system call at all. The rule asks for ONE
scoped lookup to that system; it must not read as a re-run of the systems
inventory, which the route seed forbids.

DECLINE: a system agent declined a sub-question and cited the rule and the
source it lacked; the orchestrator then asked the same system for the same
thing under other wording (a list of open rows instead of a count) and
reported what came back. The rule: report the decline, do not re-ask.

Both rules ride the standing prompt in both modes, so they are pinned for
presence, placement, wording limits and size.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch

MODES = ("agent", "orchestrator")


@pytest.mark.parametrize("mode", MODES)
def test_absence_rule_asks_for_one_scoped_lookup(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert "ABSENCE NEEDS A LOOKUP" in prompt
    rule = orch._ABSENCE_LOOKUP_RULE
    lowered = rule.lower()
    assert "one scoped" in lowered and "this turn" in lowered
    assert "withheld" in lowered            # a WITHHELD block does not replace it
    assert "not a re-run of the systems inventory" in lowered
    for kind in ("site", "cell", "asset"):
        assert kind in lowered


@pytest.mark.parametrize("mode", MODES)
def test_absence_rule_sits_right_after_the_metric_layer_rule(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert (orch._METRIC_LAYER_RESULT_RULE + orch._ABSENCE_LOOKUP_RULE) in prompt
    # The metric-term pins stay contiguous.
    assert (orch._METRIC_TERM_MISS_RULE + orch._METRIC_LAYER_RESULT_RULE) in prompt


@pytest.mark.parametrize("mode", MODES)
def test_decline_rule_forbids_a_reworded_re_ask(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert "A DECLINE IS FINAL" in prompt
    lowered = orch._DECLINE_FINAL_RULE.lower()
    assert "declines" in lowered and "cites a rule" in lowered
    assert "do not ask that system for the same thing again" in lowered
    assert "report the decline" in lowered


@pytest.mark.parametrize("mode", MODES)
def test_decline_rule_says_to_report_it_in_plain_words_without_engine_codes(mode):
    """Measured on a test event (2026-11-11): the rule said to report the
    decline "and its reason", and the answers quoted the engine's typed line
    to the user ("reason: not_connected, concept batch"). Before: nothing in
    the rule kept the line or its codes out of the answer. Quick's prompts
    carry the rule too."""
    from agents.workflow_guidance import compose_quick_system_prompt
    sentence = "Report the decline in plain words; never quote [engine] lines or codes."
    rule = orch._DECLINE_FINAL_RULE
    assert sentence in rule.replace("\n", " ")
    assert "its reason in your answer" not in rule.replace("\n", " ")
    assert rule in orch._build_orchestrator_prompt(mode)
    assert rule in compose_quick_system_prompt(mode)


@pytest.mark.parametrize("mode", MODES)
def test_decline_rule_follows_the_miss_follow_up_budget(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    anchor = orch._DECLINE_FINAL_ANCHOR
    assert prompt.count(anchor) == 1
    assert (anchor + orch._DECLINE_FINAL_RULE) in prompt


@pytest.mark.parametrize("mode", MODES)
def test_concept_notes_line_explains_the_section(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    line = orch._CONCEPT_NOTES_RULE
    assert line in prompt
    lowered = line.lower()
    assert "concept_notes" in lowered and "definitions" in lowered
    assert "the user" in lowered and "reference" in lowered


def test_rule_texts_are_short_and_use_no_em_or_en_dashes():
    for rule in (orch._ABSENCE_LOOKUP_RULE, orch._DECLINE_FINAL_RULE, orch._CONCEPT_NOTES_RULE):
        assert "\u2014" not in rule and "\u2013" not in rule
        assert len(rule) <= 650, len(rule)
    total = (len(orch._ABSENCE_LOOKUP_RULE) + len(orch._DECLINE_FINAL_RULE)
             + len(orch._CONCEPT_NOTES_RULE))
    assert total <= 1600, total
