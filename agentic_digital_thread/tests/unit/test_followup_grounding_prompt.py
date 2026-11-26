# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-drift guard for the FOLLOW-UP GROUNDING block.

Live evidence (2026-07-16, deployed env): an answer's key finding was
"YEG4 does not exist in the SiteWise asset hierarchy... I cannot give you a
live machine state breakdown for YEG4 from SiteWise this turn" — yet the
generated recommendations led with actions against that same unavailable
SiteWise data, and the follow-up questions chased the subject without
acknowledging the gap. (YEG4 DID exist in MES/Athena — follow-ups against the
systems that returned data are legitimate; the failure mode is suggesting
actions that require the data the answer just declared unavailable, without
remediation framing.)

The fix is a compact FOLLOW-UP GROUNDING addendum inside the shared
FOLLOW-UP SUGGESTIONS section of ``_build_orchestrator_prompt``:
  1. never suggest a follow-up/action requiring data the answer explicitly
     declared nonexistent/unavailable, unless reframed as the remediation
     ("onboard X", "fix the mapping") and labeled as such;
  2. follow-up questions must be answerable with the systems/data that
     successfully returned results this turn;
  3. when the answer contains a negative finding, at most ONE recommended
     action may address it, remediation-framed.

These tests pin that block in BOTH modes, exactly once, inside the
FOLLOW-UP SUGGESTIONS section. Style follows
``test_orchestrator_missing_system_honesty.py`` / ``test_workflow_guidance_no_drift.py``.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch

# The exact addendum header — pinned verbatim so a rewording/drop fails loudly.
_BLOCK_HEADER = "FOLLOW-UP GROUNDING (NON-NEGOTIABLE):"

# Sentinel phrases for the three grounding rules.
_SENTINELS = (
    # Rule 1: no chasing declared-unavailable data, unless remediation-framed.
    "NEVER suggest a follow-up or action that requires data your answer explicitly declared",
    "nonexistent or unavailable",
    "reframed as the remediation",
    "labeled as such",
    # Rule 2: follow-ups grounded in the systems that worked this turn.
    "answerable with the systems and data that successfully",
    "returned results this turn",
    # Rule 3: at most one remediation-framed action per negative finding.
    "at most ONE recommended action may address",
    "remediation-framed",
    "never a re-query of the missing data",
)

_MODES = ("agent", "orchestrator")


# --------------------------------------------------------------------------- #
# (a) The block and its sentinels are present in BOTH mode prompts.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_grounding_block_present_in_both_modes(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert _BLOCK_HEADER in prompt, (
        f"mode={mode}: the FOLLOW-UP GROUNDING block is missing — follow-ups will "
        "chase data the answer declared unavailable (the YEG4/SiteWise failure)"
    )


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("sentinel", _SENTINELS)
def test_grounding_sentinels_present_in_both_modes(mode, sentinel):
    prompt = orch._build_orchestrator_prompt(mode)
    assert sentinel in prompt, (
        f"mode={mode}: grounding sentinel {sentinel!r} missing — the block drifted "
        "or was weakened"
    )


# --------------------------------------------------------------------------- #
# (b) Placement: exactly once, inside the FOLLOW-UP SUGGESTIONS section
#     (after the ---FOLLOWUPS--- template, before CHART RENDERING).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_grounding_block_appears_exactly_once(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(_BLOCK_HEADER) == 1, (
        f"mode={mode}: expected exactly one FOLLOW-UP GROUNDING block, "
        f"found {prompt.count(_BLOCK_HEADER)}"
    )


@pytest.mark.parametrize("mode", _MODES)
def test_grounding_block_inside_followup_section(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    suggestions_idx = prompt.index("FOLLOW-UP SUGGESTIONS:")
    template_idx = prompt.index("---FOLLOWUPS---", suggestions_idx)
    block_idx = prompt.index(_BLOCK_HEADER)
    chart_idx = prompt.index("CHART RENDERING", block_idx)
    assert template_idx < block_idx < chart_idx, (
        f"mode={mode}: the grounding block must sit inside the FOLLOW-UP "
        "SUGGESTIONS section (after the ---FOLLOWUPS--- template, before "
        "CHART RENDERING) so it reads as part of the generation contract"
    )


def test_grounding_block_text_identical_across_modes():
    """The addendum is mode-independent — it lives in the shared body, so the
    extracted text must be byte-identical in both mode renders."""

    def _extract(prompt: str) -> str:
        start = prompt.index(_BLOCK_HEADER)
        end = prompt.index("CHART RENDERING", start)
        return prompt[start:end]

    agent_block = _extract(orch._build_orchestrator_prompt("agent"))
    orch_block = _extract(orch._build_orchestrator_prompt("orchestrator"))
    assert agent_block == orch_block, (
        "the grounding block drifted into a mode-specific fragment — it must be "
        "identical across modes"
    )


# --------------------------------------------------------------------------- #
# (c) Composition guards: the FOLLOWUPS generation contract itself is intact
#     (the frontend parses the ---FOLLOWUPS--- marker), and the module-level
#     default prompt carries the block.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_followups_template_still_present(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert "---FOLLOWUPS---" in prompt, (
        f"mode={mode}: the grounding addendum must compose with the FOLLOWUPS "
        "template, not replace it — the frontend parses this marker"
    )


@pytest.mark.parametrize("mode", _MODES)
def test_marker_matches_the_eval_scoring_boundary(mode):
    """The GENERATED marker and the SCORED marker must be the same string.

    ``eval_core.answer_body`` strips everything after this marker before any
    golden/smoke gate runs (suggestion chips are questions, not claims). The
    two live in different layers by necessity — the prompt template here, the
    stdlib-only scorer in eval_core — so a reworded marker would silently
    restore chip scoring on every lane at once. Pinned in both directions.
    """
    from agents.eval_core import FOLLOWUPS_MARKER

    prompt = orch._build_orchestrator_prompt(mode)
    assert FOLLOWUPS_MARKER in prompt, (
        f"mode={mode}: the prompt no longer emits the marker eval_core scores "
        f"on ({FOLLOWUPS_MARKER!r}) — the eval/smoke lanes would resume "
        "scoring the generated follow-up chips as answer text"
    )


def test_module_level_default_prompt_carries_block():
    """ORCHESTRATOR_PROMPT (the import-time Mode-A render existing callers read)
    must carry the block — the fix has to reach the default path too."""
    assert _BLOCK_HEADER in orch.ORCHESTRATOR_PROMPT


@pytest.mark.parametrize("mode", _MODES)
def test_marker_line_formatting_is_pinned(mode):
    """Rehearsal leak: the model emitted `**FOLLOWUPS**` / `## FOLLOWUPS`
    variants. The renderers now tolerate them, but the prompt still asks for
    the exact protocol line — inside the FOLLOW-UP SUGGESTIONS section."""
    prompt = orch._build_orchestrator_prompt(mode)
    section = prompt[prompt.index("FOLLOW-UP SUGGESTIONS:"):prompt.index(_BLOCK_HEADER)]
    assert ("emit the marker line exactly `---FOLLOWUPS---`, on its own line, "
            "no formatting") in section
