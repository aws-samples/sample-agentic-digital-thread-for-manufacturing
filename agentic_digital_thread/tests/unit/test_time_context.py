# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for per-request temporal context (agents.time_context).

Covers:
  (a) the composed per-request question (``server._prepare_question``) carries
      the CURRENT date — rendered at request time, never import time;
  (b) a valid client timezone adds a local-time line (for resolving relative
      phrases; answer times stay UTC);
  (c) an invalid/unknown timezone degrades to the UTC-only line (never raises);
  (d) MODE_B_WORKFLOW_GUIDANCE stays VERBATIM in the orchestrator Mode-B prompt
      (the temporal line is composed ALONGSIDE the canonical block, per-turn,
      never inside it).

No AWS, no Neptune, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agents.models import AskRequest
from agents.time_context import render_time_context

# A fixed instant with distinct UTC/local dates (23:03 Denver = 05:03 UTC next day)
# so timezone conversion is actually observable.
_FIXED_NOW = datetime(2026, 7, 15, 5, 3, 35, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# render_time_context
# --------------------------------------------------------------------------- #
def test_utc_line_has_iso_timestamp_and_weekday():
    line = render_time_context(now=_FIXED_NOW)
    assert "Current UTC time: 2026-07-15T05:03:35Z (Wednesday)." in line
    assert line.startswith("[Temporal context: ")
    assert line.endswith("]")


def test_valid_timezone_adds_local_time_line():
    line = render_time_context("America/Denver", "en-US", now=_FIXED_NOW)
    # 05:03 UTC on the 15th is 23:03 MDT on the 14th (Tuesday).
    assert "User timezone: America/Denver — local time 2026-07-14T23:03:35-0600 (Tuesday)." in line
    assert "User locale: en-US." in line
    # UTC anchor is still present alongside.
    assert "Current UTC time: 2026-07-15T05:03:35Z (Wednesday)." in line


def test_invalid_timezone_falls_back_to_utc_only():
    for bad in ("Not/AZone", "'; DROP TABLE x; --", "a" * 200, "", None, 42):
        line = render_time_context(bad, now=_FIXED_NOW)  # type: ignore[arg-type]
        assert "User timezone" not in line, f"expected UTC-only for {bad!r}"
        assert "Current UTC time: 2026-07-15T05:03:35Z" in line


def test_invalid_locale_is_dropped_not_composed():
    line = render_time_context("America/Denver", "en US injected]", now=_FIXED_NOW)
    assert "User locale" not in line
    assert "injected" not in line


def test_default_now_is_current_clock():
    """No injected clock → the line carries TODAY's UTC date (freshness)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert today in render_time_context()


def test_timezone_present_keeps_answer_times_in_utc():
    """With a known user tz, the zone only resolves relative phrases: answer
    times stay UTC and the browser shows them in the viewer's zone."""
    line = render_time_context("America/Denver", now=_FIXED_NOW)
    assert "present timestamps in the answer in the user's local timezone" not in line
    assert "Data timestamps from tools are UTC unless stated otherwise" in line
    assert "Write every time in the answer in UTC as YYYY-MM-DD HH:MM UTC" in line


def test_utc_only_has_no_presentation_directive():
    """No tz (or invalid tz) → no conversion instruction — nothing to convert to."""
    for bad in (None, "Not/AZone", ""):
        line = render_time_context(bad, now=_FIXED_NOW)
        assert "present timestamps" not in line, f"unexpected directive for {bad!r}"
        assert "local timezone" not in line
        assert "user's screen" not in line


def test_utc_only_line_keeps_a_handed_absolute_time_as_given():
    """The system agent's line (no zone) still resolves against UTC, and now
    says an absolute time it was handed is used as given, never shifted: the
    orchestrator sends times as UTC with Z, and a second conversion on the
    system side was one way a window end came out a full offset off."""
    for bad in (None, "Not/AZone", ""):
        line = render_time_context(bad, now=_FIXED_NOW)
        assert "Current UTC time: 2026-07-15T05:03:35Z" in line
        assert "otherwise UTC" in line
        assert ("A time given with Z or an offset is already absolute: use it as given and "
                "never convert it to another zone.") in line
        assert "local timezone" not in line


def test_zoned_line_does_not_carry_the_as_given_sentence():
    """The zoned line says the same thing its own way: never convert an
    answer time to the user's zone."""
    line = render_time_context("America/Denver", now=_FIXED_NOW)
    assert "use it as given" not in line
    assert "never converted to the user's zone" in line


# --------------------------------------------------------------------------- #
# server._prepare_question composes the temporal line per request
# --------------------------------------------------------------------------- #
def test_prepared_question_contains_current_date():
    from agents.server import _prepare_question

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = _prepare_question(AskRequest(question="What failed today?"))
    assert "[Temporal context: " in out
    assert today in out
    assert out.endswith("What failed today?")


def test_prepared_question_with_timezone_has_local_line():
    from agents.server import _prepare_question

    out = _prepare_question(
        AskRequest(question="q", timezone="America/Denver", locale="en-US")
    )
    assert "User timezone: America/Denver" in out
    assert "User locale: en-US." in out


def test_prepared_question_invalid_timezone_is_utc_only():
    from agents.server import _prepare_question

    out = _prepare_question(AskRequest(question="q", timezone="Not/AZone"))
    assert "Current UTC time:" in out
    assert "User timezone" not in out


def test_prepared_question_empty_question_stays_empty():
    from agents.server import _prepare_question

    assert _prepare_question(AskRequest(question="")) == ""


def test_prepared_question_keeps_plant_context_bracket():
    from agents.server import _prepare_question

    out = _prepare_question(AskRequest(question="q", plant="plant-1"))
    assert "[Plant context: plant-1" in out
    assert "[Temporal context: " in out


# --------------------------------------------------------------------------- #
# (d) The canonical Mode-B workflow guidance stays verbatim in the prompt —
# the temporal line lives in the per-turn user context, never inside it.
# --------------------------------------------------------------------------- #
def test_mode_b_workflow_guidance_still_verbatim():
    import agents.orchestrator as orch
    from agents.workflow_guidance import MODE_B_WORKFLOW_GUIDANCE

    prompt = orch._build_orchestrator_prompt("orchestrator")
    assert MODE_B_WORKFLOW_GUIDANCE in prompt
    # And the system prompt carries NO temporal line (it is import-time-built
    # and remote in cloud mode — 'now' must only ever be per-turn).
    # The prompt names the block, valueless, in the rule that the blocks a turn
    # carries are never narrated (orchestrator._TURN_CONTEXT_RULE), so the
    # check is for a rendered line, which always opens with the UTC clock.
    assert "[Temporal context: Current UTC time" not in prompt
    assert render_time_context(now=_FIXED_NOW).startswith("[Temporal context: Current UTC time")


def test_timezone_line_states_the_offset_in_words_with_a_worked_conversion():
    """Event 27: the model read ``-0600`` and still subtracted seven hours
    (22:02Z became "15:02 MDT"). The line spells the arithmetic out and shows
    it applied to the current instant, so the abbreviation cannot be mistaken
    for a different offset. It runs local -> UTC, the one direction the model
    still converts in (a local bound such as local midnight into UTC)."""
    line = render_time_context("America/Denver", now=_FIXED_NOW)
    assert "MDT is UTC-06:00 at this instant: add 6 hours to a local time to get UTC" in line
    assert "(23:03 MDT is 05:03 UTC)" in line


def test_offset_wording_for_positive_and_half_hour_zones():
    tokyo = render_time_context("Asia/Tokyo", now=_FIXED_NOW)
    assert "JST is UTC+09:00 at this instant: subtract 9 hours from a local time to get UTC" in tokyo
    assert "(14:03 JST is 05:03 UTC)" in tokyo
    kolkata = render_time_context("Asia/Kolkata", now=_FIXED_NOW)
    assert ("IST is UTC+05:30 at this instant: subtract 5 hours 30 minutes from a local time "
            "to get UTC") in kolkata
    assert "(10:33 IST is 05:03 UTC)" in kolkata
    utc = render_time_context("Etc/UTC", now=_FIXED_NOW)
    assert "UTC is UTC+00:00 at this instant: a local time equals UTC" in utc


# --------------------------------------------------------------------------- #
# PreparedQuestion survives copy and pickle. Strands deep-copies the agent's
# messages before every model call, and the memory session manager deep-copies
# each message it stores, so a PreparedQuestion that cannot be rebuilt fails
# every local chat turn.
# --------------------------------------------------------------------------- #

import copy  # noqa: E402
import pickle  # noqa: E402

import pytest  # noqa: E402

from agents.time_context import PreparedQuestion  # noqa: E402


@pytest.mark.parametrize("clone", [
    copy.copy,
    copy.deepcopy,
    lambda q: pickle.loads(pickle.dumps(q)),
    lambda q: copy.deepcopy([{"role": "user", "content": [{"text": q}]}])[0]["content"][0]["text"],
], ids=["copy", "deepcopy", "pickle", "deepcopy-in-message"])
def test_prepared_question_clones_keep_type_text_and_raw_question(clone):
    q = PreparedQuestion("[Temporal context: x] How many orders?", "How many orders?")
    out = clone(q)
    assert type(out) is PreparedQuestion
    assert out == q
    assert out.raw_question == "How many orders?"
