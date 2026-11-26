# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guided domain-context capture (T3.1).

Turns the seven intake questions in ``docs/domain-context-intake.md`` into an
agentic flow with two stages: a day-0 deployment interview, and per-system
probing inserted between ``discover`` and ``map`` — the last moment at which
map-time context is free rather than costing a re-onboard.

Module split:

* :mod:`questions` — the declarative question bank + the pure, deterministic
  selection rule (answerable ∧ material, capped, map-time first).
* :mod:`landing` — the bridge to the ADR-0003 proposal lifecycle. Every answer
  becomes a proposal (or a business-context DRAFT); nothing is written directly.

Both are import-pure so they can be imported from route modules and tests
without dragging in the agent stack.
"""
