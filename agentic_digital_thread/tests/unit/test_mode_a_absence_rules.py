# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pins for the Mode-A integrity block's absence-honesty rules.

These are PROMPT rules — the deterministic fabrication detector
(``eval_core.fabrication_flags``) is the gate; the rules exist to make the
model produce answers that pass it. Measured live across two 5-repeat golden
campaigns: the residual failures were all one class — a digit in the same
sentence as an absent metric (an "e.g." example instruction with a value, a
formula template, a hypothetical conversion, or a grounded count sharing the
sentence). The rules below name each shape; this pin keeps them present and
keeps the block composed into the Mode-A system prompt.
"""
from __future__ import annotations

import agents.orchestrator as orch


def test_mode_a_block_names_every_measured_absence_shape():
    block = orch._MODE_A_INTEGRITY_BLOCK
    for phrase in (
        "never write an illustrative",          # hypothetical value
        "formula",                               # formula templates
        "example INSTRUCTION",                   # 'e.g. goal is 30 min'
        "ABSENT-METRIC SENTENCES CARRY NO DIGITS",  # sentence-level separation
        "COUNT GRAIN IS EVIDENCE",               # stored-grain honesty
    ):
        assert phrase in block, phrase


def test_mode_a_block_is_composed_into_the_prompt(monkeypatch):
    monkeypatch.setattr(orch, "_response_integrity_mode", lambda: "annotate", raising=False)
    composed = None
    for name in ("_mode_a_prompt", "_compose_mode_a_prompt", "mode_a_system_prompt"):
        fn = getattr(orch, name, None)
        if callable(fn):
            try:
                composed = fn("base prompt")
                break
            except TypeError:
                continue
    if composed is None:
        # Fall back to the source-level pin: the block is appended somewhere.
        import inspect
        assert "+ _MODE_A_INTEGRITY_BLOCK" in inspect.getsource(orch)
    else:
        assert "ABSENT-METRIC SENTENCES CARRY NO DIGITS" in composed
