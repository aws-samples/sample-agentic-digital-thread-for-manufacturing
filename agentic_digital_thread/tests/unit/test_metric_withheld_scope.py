# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A WITHHELD metric-layer block withholds the metric's number, not the question.

Measured on an analytics deployment: a question asked how many units were in
a state right now AND what rule decides that state. A defined metric covered
the count and was WITHHELD (its concept had no mapped system). In most runs
the orchestrator answered at once, with no delegation, saying the metric layer
"already resolved this exact question", and so never gave the rule. Nothing in
the block or the standing prompt said the block covers the number only; the
prompt had that carve-out for absence claims alone.

Both sides now say it: the rendered notice (when a block gives no number) and
the standing prompt, right after the absence-lookup rule. The rule that no
substitute number is given stays exactly as it was.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch
from agents import metric_term_resolution as mtr
from agents import metric_turn_notice as mtn
from agents import metric_turn_render as render
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")

_WITHHELD = {"outcome": "withheld", "term": "widgets stuck", "metric_id": "widget-stuck",
             "label": "Widgets stuck", "status": "unavailable",
             "detail": "no onboarded system maps concept 'stuck_widget' yet"}
_UNAVAILABLE = {"outcome": "result_unavailable", "term": "widgets stuck",
                "metric_id": "widget-stuck", "label": "Widgets stuck", "reason": "timebox"}
_COMPUTED = {"outcome": "computed", "term": "widget gauge", "metric_id": "widget-gauge",
             "label": "Widget gauge", "kind": "direct", "source": "graph", "value": 4.0,
             "unit": "count", "window": "", "system_id": "sys-a", "lineage": "t.c",
             "query_count": 1, "systems": ["sys-a"], "sample_count": 3}


def _line() -> str:
    return getattr(render, "WITHHELD_SCOPE_LINE", "<missing WITHHELD_SCOPE_LINE>")


@pytest.mark.parametrize("result", [_WITHHELD, _UNAVAILABLE], ids=["withheld", "unavailable"])
def test_a_block_with_no_number_says_the_rest_of_the_question_is_still_answered(result):
    notice = mtn.render_metric_layer_notice([result])

    assert _line() in notice
    low = " ".join(_line().split()).lower()
    assert "only that metric's number" in low
    for part in ("defined", "decided", "measured", "concept notes", "holds the definition"):
        assert part in low, part
    # The never-substitute rules are unchanged and still ride the notice.
    assert render.REQUIRED_RESPONSE in notice
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in notice
    assert "give NO number for that metric" in render.REQUIRED_RESPONSE


def test_the_line_rides_only_when_a_block_gives_no_number():
    assert _line() in mtn.render_metric_layer_notice([_COMPUTED, _WITHHELD])
    assert _line() not in mtn.render_metric_layer_notice([_COMPUTED])
    assert _line() in render.FALLBACK_NOTICE


def test_three_maximal_withheld_blocks_still_fit_with_the_line():
    big = mtn.sanitize_result(
        {"outcome": "withheld", "term": "t" * 99, "metric_id": "m" * 999,
         "label": "l" * 999, "status": "unavailable", "detail": "d" * 999})
    notice = mtn.render_metric_layer_notice([big] * 3)

    assert _line() in notice
    assert len(notice) <= mtn.MAX_NOTICE_CHARS
    assert notice.count("): WITHHELD —") == 3


def _rule() -> str:
    return getattr(orch, "_WITHHELD_SCOPE_RULE", "<missing _WITHHELD_SCOPE_RULE>")


@pytest.mark.parametrize("mode", MODES)
def test_every_prompt_carries_the_rule_after_the_absence_rule(mode):
    prompt = orch._build_orchestrator_prompt(mode)

    assert (orch._ABSENCE_LOOKUP_RULE + _rule()) in prompt
    assert prompt.count(_rule()) == 1
    # The metric-layer rule that forbids a substitute number is unchanged.
    assert (orch._METRIC_LAYER_RESULT_RULE + orch._ABSENCE_LOOKUP_RULE) in prompt
    assert "give NO number for that metric" in orch._METRIC_LAYER_RESULT_RULE


@pytest.mark.parametrize("mode", MODES)
def test_quick_prompts_leave_the_rule_out_and_keep_the_absence_rule(mode):
    """The rule is about the metric-layer block, which only the
    orchestrator's own turn carries; the Quick prompts leave both out and keep
    the absence rule beside them."""
    prompt = compose_quick_system_prompt(mode)
    assert _rule() not in prompt
    assert orch._METRIC_LAYER_RESULT_RULE not in prompt
    assert prompt.count(orch._ABSENCE_LOOKUP_RULE) == 1


def test_the_rule_names_the_parts_still_answered_and_keeps_the_number_withheld():
    low = " ".join(_rule().split()).lower()
    assert "withheld" in low and "number" in low
    for part in ("defined", "decided", "measured", "concept notes", "holds the definition"):
        assert part in low, part
    assert "no number" in low
    assert "\u2014" not in _rule() and "\u2013" not in _rule()
    assert "\u2014" not in _line() and "\u2013" not in _line()
    assert len(_rule()) <= 500, len(_rule())
