# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-drift guard for the TRANSLATION IS NOT VERIFICATION block.

Live evidence (2026-08-06, deployed env): asked which asset in the telemetry
system corresponds to a specific machine identifier — "and how do you know?" —
the orchestrator answered entirely from graph metadata (3 graph reads, ZERO
system queries) across all 3 eval repeats. The translation itself was right,
but the answer presented a stored mapping as a live fact: nothing confirmed
the mapped asset still exists (mappings go stale after re-onboarding/renames),
and the same run's smoke lane showed sibling questions inventing identifiers
by extrapolating naming patterns from graph memory.

The fix is a compact addendum inside Step 3 (TRANSLATE IDENTIFIERS) of
``_build_orchestrator_prompt``: translation from the graph tells you WHICH
identifier to use; when the question is about the entity itself (which asset
corresponds, what is it called, does it exist, "how do you know"), confirm the
translated target against the live target system this turn and cite both the
mapping's provenance and the live check. Purely-about-the-mapping questions
and already-confirmed-this-turn entities are the two named exemptions.

These tests pin the block in BOTH modes, exactly once, inside Step 3, before
Step 5. Style follows ``test_followup_grounding_prompt.py``.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch

# The exact addendum header — pinned verbatim so a rewording/drop fails loudly.
_BLOCK_HEADER = "TRANSLATION IS NOT VERIFICATION — CONFIRM LIVE WHEN THE ANSWER IS ABOUT THE ENTITY:"


@pytest.mark.parametrize("mode", ["agent", "quick"])
def test_block_present_exactly_once_in_both_modes(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(_BLOCK_HEADER) == 1


@pytest.mark.parametrize("mode", ["agent", "quick"])
def test_block_sits_inside_step3_translate(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    step3 = prompt.index("Step 3 — TRANSLATE IDENTIFIERS")
    step5 = prompt.index("Step 5 — SYNTHESIZE")
    assert step3 < prompt.index(_BLOCK_HEADER) < step5


def test_block_carries_the_operative_rules():
    prompt = orch._build_orchestrator_prompt("agent")
    i = prompt.index(_BLOCK_HEADER)
    block = prompt[i : i + 1200]
    # routing metadata, not live facts
    assert "routing metadata, not live facts" in block
    # the live confirmation demand + dual citation
    assert "confirm the translated target" in block
    assert "cite BOTH sources" in block
    # staleness rationale — why a stored mapping is not a current fact
    assert "stale" in block
    # the two named exemptions survive (no over-querying)
    assert "purely about" in block and "THIS turn" in block


def test_block_names_the_provenance_question_trigger():
    """The 'how do you know?' phrasing is the live-observed trigger — pinned so
    the block keeps addressing evidence-seeking questions explicitly."""
    prompt = orch._build_orchestrator_prompt("agent")
    i = prompt.index(_BLOCK_HEADER)
    assert '"how do you know?"' in prompt[i : i + 600]
