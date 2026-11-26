# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-drift guard for the E5 missing-required-system honesty block.

Live evidence (2026-07-14 audit, E5): asked a UNS/HighByte question in an environment
with no HighByte onboarded, the orchestrator derived fake "UNS names" from
``cmms.asset_tag`` and emitted 40 per-value proposals framed as the missing system —
honest data, fabricated framing. The fix is ONE shared (mode-independent) section in
``_build_orchestrator_prompt``, adjacent to "HANDLING SYSTEM ERRORS":
``SYSTEM NOT ONBOARDED — SAY SO, NEVER SUBSTITUTE (NON-NEGOTIABLE)``.

These tests pin that the block exists in BOTH modes, appears exactly once, forbids
substitution/mis-framed proposals, and composes with (does not weaken) NEVER REFUSE.
Style follows ``test_workflow_guidance_no_drift.py``.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import pytest

import agents.orchestrator as orch

# The exact section header — pinned verbatim so a rewording/drop fails loudly.
_BLOCK_HEADER = "SYSTEM NOT ONBOARDED — SAY SO, NEVER SUBSTITUTE (NON-NEGOTIABLE):"

# Sentinel phrases pinned per the plan ("not onboarded", "never substitute" and the
# block's concrete forbid/require lines).
_SENTINELS = (
    "not onboarded",
    "NEVER SUBSTITUTE",
    "derive, rename, or re-label another system's data",
    'presenting CMMS asset tags as "UNS names" is FORBIDDEN',
    "MUST NOT emit discovery or value-mapping proposals framed as the missing system",
    "being honestly partial is",
)

_MODES = ("agent", "orchestrator")


# --------------------------------------------------------------------------- #
# (a) The block and its sentinels are present in BOTH mode prompts (shared body).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_block_header_present_in_both_modes(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert _BLOCK_HEADER in prompt, (
        f"mode={mode}: the SYSTEM NOT ONBOARDED honesty block is missing from the "
        "orchestrator prompt — the E5 fabricated-'UNS names' failure regresses without it"
    )


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("sentinel", _SENTINELS)
def test_sentinel_phrases_present_in_both_modes(mode, sentinel):
    prompt = orch._build_orchestrator_prompt(mode)
    assert sentinel in prompt, (
        f"mode={mode}: sentinel phrase {sentinel!r} missing — the honesty block "
        "drifted or was weakened"
    )


# --------------------------------------------------------------------------- #
# (b) The block is SHARED — rendered exactly once, adjacent to the error-honesty
#     section, in both modes.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_block_appears_exactly_once(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count(_BLOCK_HEADER) == 1, (
        f"mode={mode}: the honesty block must be rendered exactly once (shared body), "
        f"found {prompt.count(_BLOCK_HEADER)} occurrences"
    )


@pytest.mark.parametrize("mode", _MODES)
def test_block_adjacent_to_handling_system_errors(mode):
    """The plan places the block adjacent to HANDLING SYSTEM ERRORS — between it and
    NEVER REFUSE — so the two honesty contracts read as one unit."""
    prompt = orch._build_orchestrator_prompt(mode)
    errors_idx = prompt.index("HANDLING SYSTEM ERRORS")
    block_idx = prompt.index(_BLOCK_HEADER)
    refuse_idx = prompt.index("NEVER REFUSE OR STOP EARLY:")
    assert errors_idx < block_idx < refuse_idx, (
        f"mode={mode}: the SYSTEM NOT ONBOARDED block must sit between "
        "HANDLING SYSTEM ERRORS and NEVER REFUSE"
    )


def test_block_text_identical_across_modes():
    """The section is mode-independent — the extracted block text must be byte-identical
    in both mode renders (it lives in the shared body, not a mode fragment)."""

    def _extract(prompt: str) -> str:
        start = prompt.index(_BLOCK_HEADER)
        end = prompt.index("NEVER REFUSE OR STOP EARLY:", start)
        return prompt[start:end]

    agent_block = _extract(orch._build_orchestrator_prompt("agent"))
    orch_block = _extract(orch._build_orchestrator_prompt("orchestrator"))
    assert agent_block == orch_block, (
        "the honesty block must be identical across modes — it drifted into a "
        "mode-specific fragment"
    )


# --------------------------------------------------------------------------- #
# (c) Composition guards: NEVER REFUSE stays present (the block composes with it,
#     not replaces it) and the module-level default prompt carries the block too.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_never_refuse_contract_still_present(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert "NEVER REFUSE OR STOP EARLY:" in prompt, (
        f"mode={mode}: the honesty block must compose with NEVER REFUSE, not remove it"
    )


def test_module_level_default_prompt_carries_block():
    """ORCHESTRATOR_PROMPT (the import-time Mode-A render existing callers read) must
    carry the block — the fix has to reach the default path, not only fresh builds."""
    assert _BLOCK_HEADER in orch.ORCHESTRATOR_PROMPT
