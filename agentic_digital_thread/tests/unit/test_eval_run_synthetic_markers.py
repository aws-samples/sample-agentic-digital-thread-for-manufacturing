# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pinned invariant — eval-run traffic carries the synthetic markers.

The Evals admin tab's server-side golden-eval runner mints
``eval-run-<runid>-q<i>`` session ids. That prefix is the no-learning
transport (the project's class: synthetic flows never write): it must trip
``runtime_app._is_synthetic_session`` so every post-turn learning capture
writer is suppressed via the ONE shared ``_skip_post_turn_capture`` gate,
AND be excluded from the user-facing session picker
(``memory_client.AUTOMATED_SESSION_PREFIXES`` — a SEPARATE policy: listing
hygiene, not write suppression; the comment at the runtime tuple REQUIRES
both stay in sync, cross-pinned behaviorally in
``test_memory_session_list.py``).

Offline: no AWS, no network — pure predicate/constant pins (the
``test_tryit_containment`` idiom). The briefing gate is untouched.
"""
from __future__ import annotations

from types import SimpleNamespace

import agents.memory_client as mc
from agents import runtime_app


def _legacy_turn(session_id: str) -> SimpleNamespace:
    """The legacy finally-block ``s`` shape the capture writers consume
    (the ``test_tryit_containment`` idiom — only the fields the gate reads)."""
    return SimpleNamespace(prompt="compare supplier ids", session_id=session_id)


# --------------------------------------------------------------------------- #
# Write suppression — runtime_app._SYNTHETIC_SESSION_PREFIXES
# --------------------------------------------------------------------------- #
def test_eval_run_session_is_synthetic():
    assert runtime_app._is_synthetic_session("eval-run-abc") is True


def test_eval_run_prefix_present_in_synthetic_tuple():
    # The literal prefix the eval runner mints with — pinned so a tuple
    # rewrite can never silently drop it.
    assert "eval-run-" in runtime_app._SYNTHETIC_SESSION_PREFIXES


def test_skip_predicate_covers_eval_run_sessions():
    # Learning writers suppressed for eval-run turns; an ordinary chat
    # session still writes (over-suppression tripwire).
    assert runtime_app._skip_post_turn_capture(_legacy_turn("eval-run-abc"))
    assert not runtime_app._skip_post_turn_capture(_legacy_turn("sess-1"))


# --------------------------------------------------------------------------- #
# Picker exclusion — memory_client.AUTOMATED_SESSION_PREFIXES (the cross-pin
# the runtime tuple's comment REQUIRES; behavior through the public list
# path is covered in test_memory_session_list.py)
# --------------------------------------------------------------------------- #
def test_eval_run_prefix_present_in_automated_prefixes():
    assert "eval-run-" in mc.AUTOMATED_SESSION_PREFIXES


def test_eval_run_session_is_automated_for_the_picker():
    assert mc._is_automated_session("eval-run-abc")
    # Anchored: 'eval-run' mid-id never excludes a real chat.
    assert not mc._is_automated_session("session-eval-run-999")
