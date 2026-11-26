# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The module 5 Q1 and module 6 goldens of the live workshop story.

Both questions name the machine by one system's identifier while another
system holds the answer under its own, so they are the goldens that fail
when the identity reaches the maintenance system by neither the graph nor
the registration text. Answers below follow the pages' "Your answer should
contain" text (content/5-ask-it-again section 1, content/6-the-machine-is-
still-running section 2); the scorer is ``scripts.eval_answers``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.eval_core import fill_story_fields, load_golden_set, should_skip  # noqa: E402
from scripts.eval_answers import DEFAULT_GOLDEN_SET, score_question  # noqa: E402

_SUMMARY = {"system_queries": 4, "neptune_queries": 0, "rules_applied": []}

M5_ANSWER = ("**No.** The 90-day spindle-bearing PM on this machine (asset **1001-CMX-441**; "
             "PM **PM-1001-0007**, raised as work order **3101**) was **deferred** at "
             "production planning's request and never performed. It was overdue by at least "
             "five days when the batch started.")
M5_GUESS = ("Yes. The machine was maintained on schedule: the last PM on IND-L3-MC1 was "
            "completed before the run.")
M6_ANSWER = ("Vibration on the machine is trending up: the X axis rose from 1.4 to 2.3 mm/s "
             "over the hour (AWS IoT SiteWise). Line 3 is running work order WO-20261017-90001 "
             "(MES). The open spindle-bearing PM, work order 3101 (PM-1001-0007), is still "
             "deferred (CMMS). Open orders for the Precision Shaft MACH-HSG-7742 would be at "
             "risk (ERP).")


def _golden(qid: str) -> dict:
    return next(q for q in load_golden_set(DEFAULT_GOLDEN_SET) if q["id"] == qid)


def test_both_run_only_on_the_live_story_after_a_cmms_is_onboarded():
    for qid in ("g-workshop-live-m5-q1-maintenance", "g-workshop-live-m6-machine-at-risk"):
        question = _golden(qid)
        live = {"ADT_WORKSHOP_STORY": "live"}
        assert should_skip(question, environ={}) is not None
        assert should_skip(question, environ=live, connected_types=set()) is not None
        assert should_skip(question, environ=live, connected_types={"CMMS"}) is None


def test_module_5_q1_asks_the_page_question_with_the_accounts_run_number():
    question = _golden("g-workshop-live-m5-q1-maintenance")
    story = {"failed_batch": {"history_run_number": "RUN-20261017-90001"}}
    filled, reason = fill_story_fields(question, lambda: story)
    assert reason is None
    assert filled["text"] == ("Was machine L3-M1 maintained on schedule before production run "
                              "RUN-20261017-90001 ran?")


def test_module_5_q1_passes_the_page_answer_and_fails_a_guess():
    question = _golden("g-workshop-live-m5-q1-maintenance")
    assert score_question(question["expect"], _SUMMARY, M5_ANSWER, [],
                          question_text=question["text"]) == ("pass", [])
    verdict, failures = score_question(question["expect"], _SUMMARY, M5_GUESS, [],
                                       question_text=question["text"])
    assert verdict == "fail" and any("forbidden pattern" in f for f in failures), failures


def test_module_6_passes_the_page_answer_and_needs_every_leg():
    question = _golden("g-workshop-live-m6-machine-at-risk")
    assert question["text"] == ("Is vibration on the CMX-441 trending up over the last hour, "
                                "and which current work orders and open shipments are at "
                                "risk if it fails?")
    assert score_question(question["expect"], _SUMMARY, M6_ANSWER, [],
                          question_text=question["text"]) == ("pass", [])
    idle = M6_ANSWER.replace("is running work order WO-20261017-90001", "is idle")
    assert score_question(question["expect"], _SUMMARY, idle, [],
                          question_text=question["text"])[0] == "pass"
    for leg in ("work order 3101 (PM-1001-0007)", "WO-20261017-90001",
                "the Precision Shaft MACH-HSG-7742"):
        verdict, _failures = score_question(question["expect"], _SUMMARY,
                                            M6_ANSWER.replace(leg, "it"), [],
                                            question_text=question["text"])
        assert verdict == "fail", leg
