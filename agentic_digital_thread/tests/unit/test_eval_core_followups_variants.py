# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""eval_core.strip_followups — the tolerant FOLLOWUPS scoring boundary.

Rehearsal leak: the model emitted a formatting VARIANT of the protocol
marker (``---\\nFOLLOWUPS\\n---``, ``**FOLLOWUPS**``, ``## FOLLOWUPS``,
``FOLLOWUPS:``) — the exact-marker split missed it and the chip text was
scored as answer. Mirror of frontend/src/chat/followups-marker.ts.
"""
import pytest

from agents import eval_core

BODY = "Line 2 ran at 92% OEE yesterday."
CHIPS = "Q1: What about 1200 units?\nA1: Schedule PM"

VARIANTS = [
    "---FOLLOWUPS---",
    "---\n\nFOLLOWUPS\n\n---",
    "---\nFOLLOWUPS\n---",
    "**FOLLOWUPS**",
    "**FOLLOWUPS:**",
    "## FOLLOWUPS",
    "### FOLLOWUPS:",
    "FOLLOWUPS:",
    "FOLLOWUPS",
]


@pytest.mark.parametrize("marker", VARIANTS)
def test_strip_followups_cuts_every_variant(marker):
    text = f"{BODY}\n\n{marker}\n{CHIPS}\n"
    assert eval_core.strip_followups(text) == BODY
    assert eval_core.answer_body(text) == BODY


@pytest.mark.parametrize("marker", VARIANTS)
def test_empty_block_leaves_no_label(marker):
    assert eval_core.strip_followups(f"{BODY}\n\n{marker}\n") == BODY


def test_canonical_marker_still_matches_mid_line_and_first_wins():
    assert eval_core.strip_followups(f"{BODY} ---FOLLOWUPS---\nQ1: x") == BODY
    assert eval_core.strip_followups(
        "a\nFOLLOWUPS:\nQ1: b\n---FOLLOWUPS---\nQ2: c") == "a"


@pytest.mark.parametrize("prose", [
    "We discussed the FOLLOWUPS from the audit yesterday.",
    "The followups: none were filed.",
    "FOLLOWUPS were scheduled for Tuesday.",
    "## FOLLOWUPS from the audit",
    "- FOLLOWUPS",
])
def test_prose_mentioning_followups_is_never_matched(prose):
    text = f"{BODY}\n{prose}\nMore text."
    assert eval_core.strip_followups(text) == text


def test_strip_followups_is_safe_and_idempotent():
    assert eval_core.strip_followups("") == ""
    assert eval_core.strip_followups(None) == ""
    once = eval_core.strip_followups(f"{BODY}\n**FOLLOWUPS**\n{CHIPS}")
    assert eval_core.strip_followups(once) == once


def test_answer_body_delegates_to_strip_followups():
    # ONE scoring-body regex: answer_body must reach strip_followups, never
    # a second split.
    import inspect
    assert "strip_followups(" in inspect.getsource(eval_core.answer_body)
