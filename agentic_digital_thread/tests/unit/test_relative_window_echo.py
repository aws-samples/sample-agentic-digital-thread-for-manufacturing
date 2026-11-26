# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Answer honesty — relative-date window echo + Mode A per-call time context.

Fully offline (no Bedrock, no AWS, no sockets). Pins:

1. The RESOLVED TIME WINDOW echo rule is present in BOTH composed mode
   prompts AND both Quick prompts (the shared-section placement contract —
   the rule must survive the Mode-B Quick ``replace()`` scrub, so it never
   rides ``_MODE_A_DELEGATE_BLOCK``).
2. Mode A per-invocation temporal context: ``_run_mode_a_query`` prefixes a
   FRESH ``render_time_context()`` line onto the delegated question on EVERY
   call — two calls under two different patched clocks yield two different
   date lines (proves the clock is read at call time, never captured at the
   long-lived singleton's construction).
3. Mode B tool payloads are unchanged: no time-context reference anywhere in
   the Mode B register path.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import agents.orchestrator as orch
import agents.system_agent as sa
import agents.time_context as tc
from agents.workflow_guidance import compose_quick_system_prompt

# The exact rule fragments the answers contract keys on.
_ECHO_HEADING = "RESOLVED TIME WINDOW — ECHO THE ABSOLUTE DATES"
_ECHO_FRAGMENTS = (
    _ECHO_HEADING,
    "MUST state the resolved ABSOLUTE window",
    "never leave a relative phrase as the only statement",
)


# --------------------------------------------------------------------------- #
# 1. Prompt pins — both modes, both Quick surfaces (shared-section placement)
# --------------------------------------------------------------------------- #
def test_echo_rule_present_in_both_mode_prompts():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        for fragment in _ECHO_FRAGMENTS:
            assert fragment in prompt, (mode, fragment)


def test_echo_rule_survives_both_quick_prompts():
    for mode in ("agent", "orchestrator"):
        quick = compose_quick_system_prompt(mode)
        for fragment in _ECHO_FRAGMENTS:
            assert fragment in quick, (mode, fragment)


def test_echo_rule_is_not_inside_the_mode_a_delegate_block():
    # Placement contract: the rule applies to BOTH modes, so it must live in
    # a shared fragment — inside _MODE_A_DELEGATE_BLOCK the Mode-B Quick
    # scrub (workflow.replace(...)) would silently drop it.
    assert _ECHO_HEADING not in orch._MODE_A_DELEGATE_BLOCK


def test_echo_anchor_post_pass_fails_loud_on_drift():
    try:
        orch._apply_answer_honesty_prompt_rules("no anchors here")
    except RuntimeError as exc:
        assert "anchor drifted" in str(exc)
    else:
        raise AssertionError("missing anchor must raise, never silently skip")


# --------------------------------------------------------------------------- #
# 2. Mode A per-invocation freshness — two patched clocks, two different lines
# --------------------------------------------------------------------------- #
class _CapturingInvoke:
    """Stub for _invoke_mode_a_agent_with_state capturing the delegated text."""

    def __init__(self):
        self.questions: list[str] = []

    def __call__(self, _agent, question):
        self.questions.append(question)
        return ("stub answer", {})


def _patch_clock(monkeypatch, fixed: datetime) -> None:
    class _Clock:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(tc, "datetime", _Clock)


def test_time_context_is_rendered_fresh_on_every_mode_a_call(monkeypatch):
    invoke = _CapturingInvoke()
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context",
                        lambda _s, q, _k: q)

    _patch_clock(monkeypatch, datetime(2026, 3, 5, 8, 30, tzinfo=timezone.utc))
    asyncio.run(sa._run_mode_a_query(object(), "sys-a", "how many units?"))
    _patch_clock(monkeypatch, datetime(2026, 7, 9, 22, 15, tzinfo=timezone.utc))
    asyncio.run(sa._run_mode_a_query(object(), "sys-a", "how many units?"))

    first, second = invoke.questions
    assert "Current UTC time: 2026-03-05T08:30:00Z" in first
    assert "Current UTC time: 2026-07-09T22:15:00Z" in second
    # Same question, different clocks → different delegated prompts: the
    # line is rendered AT CALL TIME, never captured at agent construction.
    assert first != second
    # The bracketed line rides BEFORE the delegated question text.
    assert first.startswith("[Temporal context: ")
    assert first.index("Temporal context") < first.index("how many units?")


def test_time_context_prefix_preserves_the_delegated_question(monkeypatch):
    invoke = _CapturingInvoke()
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context",
                        lambda _s, q, _k: f"{q}|augmented")
    _patch_clock(monkeypatch, datetime(2026, 1, 2, tzinfo=timezone.utc))
    asyncio.run(sa._run_mode_a_query(object(), "sys-a", "q-body"))
    (question,) = invoke.questions
    # The kg-context augmentation output is intact after the time prefix.
    assert question.endswith("q-body|augmented")


# --------------------------------------------------------------------------- #
# 3. Mode B tool payloads unchanged
# --------------------------------------------------------------------------- #
def test_mode_b_register_path_carries_no_time_context():
    for fn in (sa._register_mode_b_describe, sa._register_mode_b_run):
        assert "time_context" not in inspect.getsource(fn)
