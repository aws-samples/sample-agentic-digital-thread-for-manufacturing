# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Answers write every time in UTC; the browser shows it in the viewer's zone.

A test event answer gave a window end six hours off: the model converted UTC
to the user's zone itself and got the arithmetic wrong. The model no longer
converts any time. The per-turn time line keeps the user's zone and local
"now" only to resolve relative phrases ("today", "this month") into UTC
bounds; every time in the answer is written once, in UTC, as
``YYYY-MM-DD HH:MM UTC``; the page renders each one in the viewer's own zone
(frontend ``lib/answer-times.ts``).

Pins, in every prompt that carries the standing rules (both modes, both Quick
variants, never ``_MODE_A_DELEGATE_BLOCK``):

1. The zoned time line no longer asks for local-time presentation and states
   the UTC answer form; its offset sentence runs local -> UTC, the one
   direction the model still converts in (a local bound into UTC).
2. The standing times rule carries the answer form and says nothing converts.
3. The window echo states its window as UTC instants in the same form.

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import agents.orchestrator as orch
from agents.time_context import render_time_context
from agents.workflow_guidance import compose_quick_system_prompt

_MODES = ("agent", "orchestrator")
# 05:03 UTC on the 15th is 23:03 MDT on the 14th, so a conversion is visible.
_NOW = datetime(2026, 7, 15, 5, 3, 35, tzinfo=timezone.utc)
_FORM = "YYYY-MM-DD HH:MM UTC"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _all_prompts():
    return ([(f"full-{m}", orch._build_orchestrator_prompt(m)) for m in _MODES]
            + [(f"quick-{m}", compose_quick_system_prompt(m)) for m in _MODES])


_ALL = _all_prompts()
_IDS = [name for name, _ in _ALL]


# --------------------------------------------------------------------------- #
# 1. Per-turn time line
# --------------------------------------------------------------------------- #
def test_zoned_line_drops_local_time_presentation():
    line = render_time_context("America/Denver", "en-US", now=_NOW)
    assert "present timestamps in the answer in the user's local timezone" not in line
    assert "keeping the UTC value in parentheses" not in line
    assert "converting with the offset rule" not in line


def test_zoned_line_states_the_utc_answer_form_and_no_conversion():
    line = _flat(render_time_context("America/Denver", "en-US", now=_NOW))
    assert (f"Write every time in the answer in UTC as {_FORM}, never converted to the "
            "user's zone: the user's screen shows each one in their own zone.") in line
    assert ("Use the user's zone only to turn those phrases into UTC bounds.") in line
    assert "Data timestamps from tools are UTC unless stated otherwise." in line


def test_zoned_line_keeps_the_zone_and_local_now_for_relative_phrases():
    line = render_time_context("America/Denver", "en-US", now=_NOW)
    assert "User timezone: America/Denver — local time 2026-07-14T23:03:35-0600 (Tuesday)." in line
    assert "against the user's local time when known, otherwise UTC" in line


def test_offset_sentence_runs_from_local_time_to_utc():
    denver = render_time_context("America/Denver", now=_NOW)
    assert ("MDT is UTC-06:00 at this instant: add 6 hours to a local time to get UTC "
            "(23:03 MDT is 05:03 UTC).") in denver
    assert "from a UTC timestamp" not in denver
    kolkata = render_time_context("Asia/Kolkata", now=_NOW)
    assert ("IST is UTC+05:30 at this instant: subtract 5 hours 30 minutes from a local time "
            "to get UTC (10:33 IST is 05:03 UTC).") in kolkata
    utc = render_time_context("Etc/UTC", now=_NOW)
    assert "UTC is UTC+00:00 at this instant: a local time equals UTC (05:03 UTC is 05:03 UTC)." in utc


def test_utc_only_line_is_unchanged_by_the_answer_form():
    # System agents and Quick get this line; the answer form rides the
    # standing orchestrator rule instead, so a system agent's line is unchanged.
    line = render_time_context(now=_NOW)
    assert "A time given with Z or an offset is already absolute" in line
    assert _FORM not in line


# --------------------------------------------------------------------------- #
# 2. Standing times rule, every prompt
# --------------------------------------------------------------------------- #
_RULE = (
    "TIMES ARE UTC: a time you send to a system, in a delegated question, a tool's time "
    "argument or a query, is absolute UTC, never a local time",
    "Write it as tools return it (ISO with Z) in questions and time arguments",
    f"In the answer write every time in UTC as {_FORM} (seconds only when the record has "
    "them), always with its date (each end of a range written in full, with its own date and "
    "UTC), and never convert it to another zone.",
)


@pytest.mark.parametrize("name,prompt", _ALL, ids=_IDS)
def test_every_prompt_carries_the_utc_answer_rule_once(name, prompt):
    flat = _flat(prompt)
    for fragment in _RULE:
        assert flat.count(fragment) == 1, (name, fragment)
    assert "Convert to the user's local time only in the answer" not in flat, name


@pytest.mark.parametrize("name,prompt", _ALL, ids=_IDS)
def test_no_prompt_asks_for_local_time_in_the_answer(name, prompt):
    flat = _flat(prompt).lower()
    for phrase in ("user's local time only in", "in the user's local timezone",
                   "(america/new_york)", "when no user timezone is known"):
        assert phrase not in flat, (name, phrase)


def test_rule_is_not_inside_the_mode_a_delegate_block():
    assert "TIMES ARE UTC:" not in orch._MODE_A_DELEGATE_BLOCK


# --------------------------------------------------------------------------- #
# 3. Window echo in UTC instants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,prompt", _ALL, ids=_IDS)
def test_window_echo_is_in_utc_instants(name, prompt):
    flat = _flat(prompt)
    assert ("you actually queried, as UTC instants in the answer's time form, e.g. "
            "\"window: 2026-08-18 06:00 UTC → 2026-09-01 06:00 UTC\", once, near the "
            "figures it scopes.") in flat, name
    assert flat.index("RESOLVED TIME WINDOW") < flat.index("TIMES ARE UTC:"), name
