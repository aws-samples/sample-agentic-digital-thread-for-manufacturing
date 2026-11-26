# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding-determinism pins (live-found 2026-08-06).

Two same-day UI onboards of the same system diverged on 13 identity rules,
a dozen mapping confidences/roles, and tied best-concept picks. Two fixable
variance sources are pinned here:

1. bedrock_call's request body must carry temperature 0 (greedy decoding) —
   the provider default (~1.0) was the dominant wobble source for what are
   all structured-output tasks.
2. _annotate_best_concepts must break confidence ties by concept id, never
   by LLM output order.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.db import _converse_request  # noqa: E402
from agents.onboard_agent import _annotate_best_concepts  # noqa: E402


def test_request_body_pins_temperature_zero_for_both_families():
    nova = _converse_request("p", "us.amazon.nova-pro-v1:0")
    assert nova["inferenceConfig"]["temperature"] == 0.0
    claude = _converse_request("p", "us.anthropic.claude-sonnet-4-20250514-v1:0")
    assert claude["inferenceConfig"]["temperature"] == 0.0


def test_best_concept_tie_breaks_alphabetically_not_by_output_order():
    # Two identifier-role candidates, identical confidence, reversed order:
    # the winner must be the same either way (alphabetically first).
    a = {"concept": "asset-class", "confidence": 0.8, "role": "identifier"}
    b = {"concept": "equipment-id", "confidence": 0.8, "role": "identifier"}
    m1 = {"mappings": [a, b]}
    m2 = {"mappings": [b, a]}
    _annotate_best_concepts([m1])
    _annotate_best_concepts([m2])
    assert m1["concept"] == m2["concept"] == "asset-class"


def test_best_concept_still_prefers_higher_confidence():
    rows = [{"mappings": [
        {"concept": "aaa", "confidence": 0.5, "role": "identifier"},
        {"concept": "zzz", "confidence": 0.9, "role": "identifier"},
    ]}]
    _annotate_best_concepts(rows)
    assert rows[0]["concept"] == "zzz"
