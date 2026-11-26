# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The zoned time line lists each local period start as its own UTC instant.

One offset, taken at the current instant, is wrong for a local time on the
other side of a daylight-saving change: at 03:00 MST on the day Denver falls
back, that day's midnight was still MDT, so "today" starts at 06:00 UTC, not
07:00. The server now converts the bounds the model needs most (start of
today, of yesterday, of this week, of this month and of last month, and now)
with ZoneInfo, each at its own instant, and the offset sentence says it holds
for the current instant only. A shift has no server bound: its window comes
from a system's records, or the answer says it could not be resolved.

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

from datetime import datetime, timezone

from agents.time_context import render_time_context


def _line(zone: str, *args) -> str:
    return render_time_context(zone, now=datetime(*args, tzinfo=timezone.utc))


def test_fall_back_day_starts_on_its_own_offset():
    # 2026-11-01 10:00 UTC is 03:00 MST; local midnight was 00:00 MDT.
    line = _line("America/Denver", 2026, 11, 1, 10, 0)
    assert "MST is UTC-07:00 at this instant: add 7 hours to a local time to get UTC" in line
    assert ("Local period starts in UTC (a week starts on Monday): start of today "
            "2026-11-01 00:00 MDT is 2026-11-01T06:00:00Z; start of yesterday "
            "2026-10-31 00:00 MDT is 2026-10-31T06:00:00Z; start of this week "
            "2026-10-26 00:00 MDT is 2026-10-26T06:00:00Z; start of this month "
            "2026-11-01 00:00 MDT is 2026-11-01T06:00:00Z; start of last month "
            "2026-10-01 00:00 MDT is 2026-10-01T06:00:00Z; now 2026-11-01 03:00 MST is "
            "2026-11-01T10:00:00Z.") in line


def test_spring_forward_day_starts_on_its_own_offset():
    # 2026-03-08 18:00 UTC is 12:00 MDT; local midnight was 00:00 MST.
    line = _line("America/Denver", 2026, 3, 8, 18, 0)
    assert "MDT is UTC-06:00 at this instant: add 6 hours to a local time to get UTC" in line
    assert "start of today 2026-03-08 00:00 MST is 2026-03-08T07:00:00Z" in line
    assert "start of yesterday 2026-03-07 00:00 MST is 2026-03-07T07:00:00Z" in line
    assert "start of this week 2026-03-02 00:00 MST is 2026-03-02T07:00:00Z" in line
    assert "now 2026-03-08 12:00 MDT is 2026-03-08T18:00:00Z." in line


def test_offset_sentence_says_it_holds_for_now_only():
    line = _line("America/Denver", 2026, 11, 1, 10, 0)
    assert ("That offset holds for the current instant only: a local time across a "
            "daylight-saving change has its own offset, so use the period starts below.") in line
    assert line.index("at this instant") < line.index("Local period starts in UTC")


def test_half_hour_zone_bounds():
    # 2026-07-15 05:03 UTC is 10:33 IST on Wednesday 15 July.
    line = _line("Asia/Kolkata", 2026, 7, 15, 5, 3)
    assert "start of today 2026-07-15 00:00 IST is 2026-07-14T18:30:00Z" in line
    assert "start of yesterday 2026-07-14 00:00 IST is 2026-07-13T18:30:00Z" in line
    assert "start of this week 2026-07-13 00:00 IST is 2026-07-12T18:30:00Z" in line
    assert "now 2026-07-15 10:33 IST is 2026-07-15T05:03:00Z." in line


def test_positive_offset_zone_bounds():
    # 2026-07-15 05:03 UTC is 14:03 JST.
    line = _line("Asia/Tokyo", 2026, 7, 15, 5, 3)
    assert "start of today 2026-07-15 00:00 JST is 2026-07-14T15:00:00Z" in line
    assert "start of this week 2026-07-13 00:00 JST is 2026-07-12T15:00:00Z" in line


def test_week_start_on_a_monday_is_that_day():
    # 2026-07-13 is a Monday: this week and today start together.
    line = _line("America/Denver", 2026, 7, 13, 18, 0)
    assert "start of today 2026-07-13 00:00 MDT is 2026-07-13T06:00:00Z" in line
    assert "start of this week 2026-07-13 00:00 MDT is 2026-07-13T06:00:00Z" in line


def test_a_day_whose_midnight_is_skipped_starts_when_the_gap_starts():
    # America/Santiago springs forward at local midnight on 2026-09-06: the
    # day begins at 01:00 -03, the instant 00:00 -04 would have been.
    line = _line("America/Santiago", 2026, 9, 6, 15, 0)
    assert "start of today 2026-09-06 01:00 -03 is 2026-09-06T04:00:00Z" in line
    assert "start of yesterday 2026-09-05 00:00 -04 is 2026-09-05T04:00:00Z" in line


def test_utc_only_line_has_no_bounds():
    line = render_time_context(now=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
    assert "Local period starts" not in line
    assert "at this instant" not in line


def test_month_starts_across_fall_back_use_their_own_offset():
    # 2026-11-15 18:00 UTC is 11:00 MST; 1 November and 1 October began in MDT.
    line = _line("America/Denver", 2026, 11, 15, 18, 0)
    assert "start of this month 2026-11-01 00:00 MDT is 2026-11-01T06:00:00Z" in line
    assert "start of last month 2026-10-01 00:00 MDT is 2026-10-01T06:00:00Z" in line


def test_month_starts_across_spring_forward_use_their_own_offset():
    # 2026-03-20 18:00 UTC is 12:00 MDT; 1 March and 1 February began in MST.
    line = _line("America/Denver", 2026, 3, 20, 18, 0)
    assert "start of this month 2026-03-01 00:00 MST is 2026-03-01T07:00:00Z" in line
    assert "start of last month 2026-02-01 00:00 MST is 2026-02-01T07:00:00Z" in line


def test_last_month_in_january_is_december_of_the_year_before():
    line = _line("Asia/Kolkata", 2026, 1, 10, 5, 0)
    assert "start of this month 2026-01-01 00:00 IST is 2025-12-31T18:30:00Z" in line
    assert "start of last month 2025-12-01 00:00 IST is 2025-11-30T18:30:00Z" in line


def test_bounds_are_listed_in_order_and_end_with_now():
    line = _line("America/Denver", 2026, 11, 15, 18, 0)
    labels = ["start of today", "start of yesterday", "start of this week",
              "start of this month", "start of last month", "now 2026-11-15"]
    positions = [line.index(label) for label in labels]
    assert positions == sorted(positions)


def test_a_shift_window_comes_from_the_data_not_the_offset():
    """No server bound exists for a shift: its local start can sit on either
    side of a daylight-saving change, and only a system's records know when a
    shift started. The line says to take the shift's start and end as
    absolute times from a system, and to say so when none gives them."""
    for zone in ("America/Denver", None):
        line = render_time_context(zone, now=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
        assert "'this shift'" not in line
        assert ("A shift's start and end come from a system's records as absolute times; when "
                "no system gives them, say the shift window could not be resolved instead of "
                "guessing it.") in line

