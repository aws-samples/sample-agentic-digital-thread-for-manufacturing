# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0048 Task 2 pins: the Mode-A IDENTIFIER-MISS RECOVERY carve-out + flywheel.

The reactive identifier-miss translation loop is a PROMPT contract, not code:
a system agent's fenced ``IDENTIFIER-MISS`` marker gates ONE graph translation
lookup + ONE re-delegation (hard cap 2 per turn), with disclosure on
substitution and a channel-conditional learning mandate on a confirmed
recovery (the flywheel): the full orchestrator prompt emits the
``---VALUE-MAPPING-PROPOSAL---`` block (ingested by
``runtime_app._ingest_value_mapping_proposals``), while the Quick Mode-A
surface — where the proposals section is sliced out — saves the same six
values via the ``save_value_mapping`` tool (``_QUICK_LEARNING_TAIL``). This
file pins:

- the recovery fragments in the Mode-A orchestrator prompt (and their ABSENCE
  from the Mode-B prompt — Mode B has structured schema_mismatch self-correct
  instead, no analog needed);
- the anti-thrash rule staying verbatim in BOTH modes (the recovery is an
  explicit exception, not a relaxation);
- placement of the recovery text INSIDE ``_MODE_A_DELEGATE_BLOCK`` — the
  load-bearing mechanism that keeps the Quick Mode-B ``replace()`` scrub
  working with zero edits to ``workflow_guidance.py``;
- the flywheel round-trip: a proposal block shaped exactly as the prompt
  mandates is accepted by the REAL ``runtime_app._ingest_value_mapping_proposals``
  scanner with ``confidence: high`` → 0.9.

Offline / pure import — no AWS, no network. Placeholder systems only.
"""
from __future__ import annotations

import agents.orchestrator as orch
from agents.workflow_guidance import (
    MODE_B_WORKFLOW_GUIDANCE,
    compose_quick_system_prompt,
)

_ANTI_THRASH_RULE = "STOP RE-QUERYING THE GRAPH AFTER EMPTY RESULTS"

# Exact fragments the recovery contract pins.
_PINNED_FRAGMENTS = [
    "IDENTIFIER-MISS RECOVERY — BOUNDED, DISCLOSED, NEVER SILENT",
    # Two triggers: the block, or a wrong-form no-row miss (IDENTIFIER FORMS).
    "Two signals, and only these two, trigger this bounded recovery",
    "ONE sanctioned exception to the 'STOP RE-QUERYING",  # cross-reference
    "ValueMapping (from_value → to_value)",  # lookup order 1
    "IdentityRule field pairs",  # lookup order 2
    "Asset name →",  # lookup order 3
    "re-delegate ONCE to the SAME system",
    "AT MOST 2 such recoveries per turn",
    "DISCLOSE the substitution",
    "NEVER substitute silently",
    "INCLUDE the system's my_format hint",  # graph-empty → gap + format hint
    "CONFIRMED RECOVERY → LEARN IT",
    # Learning-channel-conditional wording (fix pass): the recovery text must
    # not unconditionally mandate a proposal block — the Quick Mode-A prompt
    # has no 'VALUE-MAPPING PROPOSALS' section and forbids proposal text blocks.
    "WHICHEVER learning channel this prompt provides",
    "VALUE-MAPPING-PROPOSAL",
    "confidence: high",
    # Presentation contract (user steering): answer-first, subordinate footnote.
    "ANSWER FIRST, LEARNING SECOND",
    "visually subordinate footnote",
    "never crowd out the answer",
]

# The six fields the runtime_app scanner REQUIRES — the prompt must name each.
_REQUIRED_VM_FIELDS = (
    "from_system", "from_field", "from_value",
    "to_system", "to_field", "to_value",
)


# ---------------------------------------------------------------------------
# Prompt-fragment pins + mode split
# ---------------------------------------------------------------------------

def test_mode_a_prompt_contains_all_recovery_fragments():
    prompt = orch._build_orchestrator_prompt("agent")
    for fragment in _PINNED_FRAGMENTS:
        assert fragment in prompt, f"Mode-A prompt missing fragment: {fragment!r}"


def test_mode_b_prompt_has_no_identifier_miss_text():
    assert "IDENTIFIER-MISS" not in orch._build_orchestrator_prompt("orchestrator")


def test_anti_thrash_rule_preserved_verbatim_in_both_modes():
    assert _ANTI_THRASH_RULE in orch._build_orchestrator_prompt("agent")
    assert _ANTI_THRASH_RULE in orch._build_orchestrator_prompt("orchestrator")


# ---------------------------------------------------------------------------
# Placement pin — the Quick Mode-B replace() scrub mechanism
# ---------------------------------------------------------------------------

def test_recovery_constant_lives_inside_mode_a_delegate_block():
    assert orch._MODE_A_IDENTIFIER_MISS_RECOVERY in orch._MODE_A_DELEGATE_BLOCK


def test_adr0059_observation_is_passive_to_the_recovery_loop():
    """ADR-0059 Task 2B: the terminology-miss OBSERVATION (failure-signal
    accumulator) rides its own thin gateway-client wrapper and never alters
    this loop: the recovery prompt text carries no accumulator reference, and
    the observation wrapper returns the result to the model unchanged
    (passivity pinned end-to-end in ``test_failure_signal_hooks.py``)."""
    import inspect

    assert "failure_signal" not in orch._MODE_A_IDENTIFIER_MISS_RECOVERY
    src = inspect.getsource(orch._wrap_gateway_client_with_miss_observation)
    assert "_observe_identifier_miss(result, args, kwargs)" in src
    assert "return result" in src  # the loop's input is byte-identical


# ---------------------------------------------------------------------------
# Quick-prompt leak pins (Mode B analog explicitly NOT needed — run_query
# returns structured schema_mismatch + schema_hint; no per-system agent)
# ---------------------------------------------------------------------------

def test_mode_b_workflow_guidance_has_no_identifier_miss_text():
    assert "IDENTIFIER-MISS" not in MODE_B_WORKFLOW_GUIDANCE


def test_quick_mode_b_prompt_has_no_identifier_miss_text():
    assert "IDENTIFIER-MISS" not in compose_quick_system_prompt("orchestrator")


def test_quick_mode_a_prompt_carries_recovery_via_delegate_block():
    assert "IDENTIFIER-MISS RECOVERY" in compose_quick_system_prompt("agent")


def test_quick_mode_a_learning_mandate_is_channel_conditional():
    """The Quick Mode-A prompt slices the workflow EXCLUSIVE of the
    'VALUE-MAPPING PROPOSALS — MANDATORY' section and appends the Quick
    learning tail ('Never emit proposal text blocks; use the
    save_value_mapping tool or nothing'). The recovery text must therefore be
    learning-channel-conditional — never an unconditional block-emission
    mandate referencing a section the Quick prompt does not have."""
    quick_a = compose_quick_system_prompt("agent")
    assert "WHICHEVER learning channel this prompt provides" in quick_a
    assert "save_value_mapping" in quick_a  # the tool channel the tail provides
    # The old unconditional mandate must be gone.
    assert "exact format in VALUE-MAPPING PROPOSALS" not in quick_a
    assert (
        "exact format in VALUE-MAPPING PROPOSALS"
        not in orch._MODE_A_IDENTIFIER_MISS_RECOVERY
    )


# ---------------------------------------------------------------------------
# Flywheel round-trip — the prompt's mandated block shape MUST be accepted by
# the real runtime_app scanner (six required fields; confidence high → 0.9)
# ---------------------------------------------------------------------------

def test_prompt_names_every_scanner_required_field():
    for field in _REQUIRED_VM_FIELDS:
        assert field in orch._MODE_A_IDENTIFIER_MISS_RECOVERY, (
            f"recovery prompt must name scanner-required field {field!r}"
        )


def test_confirmed_recovery_proposal_block_round_trips_through_real_ingest():
    import agents.runtime_app as runtime_app

    # Exactly the emission shape the prompt mandates (placeholder systems).
    block = "\n".join((
        "from_system: sys-a",
        "from_field: records.external_id",
        "from_value: WO-1001",
        "to_system: sys-b",
        "to_field: entities.canonical_id",
        "to_value: 4400001001",
        "method: value_format_match",
        "confidence: high",
        "reason: confirmed recovery — re-delegation with the translated identifier returned data",
    ))
    writes: list[dict] = []

    def log_mapping(**kwargs):
        writes.append(kwargs)
        return f"mapping-{len(writes)}"

    accepted = runtime_app._ingest_value_mapping_proposals(
        [block],
        prompt="cross-system identifier question",
        tool_calls_count=1,
        log_mapping=log_mapping,
    )

    assert accepted == 1
    assert len(writes) == 1
    write = writes[0]
    assert write["from_system"] == "sys-a"
    assert write["from_field"] == "records.external_id"
    assert write["from_value"] == "WO-1001"
    assert write["to_system"] == "sys-b"
    assert write["to_field"] == "entities.canonical_id"
    assert write["to_value"] == "4400001001"
    assert write["confidence"] == 0.9  # 'high' maps to 0.9
    assert write["method"] == "value_format_match"
    assert write["provenance"] == "orchestrator"
