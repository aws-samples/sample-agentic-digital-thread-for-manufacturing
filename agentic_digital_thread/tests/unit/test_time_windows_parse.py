# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Date expressions in a question, parsed deterministically (agents.time_windows).

Table tests for every supported form, read under a plain UTC-day policy so the
expected calendar days are the only thing each row pins: M/D, M/D/YY(YY),
YYYY-MM-DD, month-name forms, inclusive ranges, today / yesterday / this week /
last week / past N days or weeks, and ``since``. A date without a year is the
most recent occurrence on or before today, including across a year boundary.
Anything the parser cannot read with certainty is left alone (no window).

Offline and pure: a fixed clock, no graph.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agents import calendar_policy as cp
from agents import time_windows as tw

#: Thursday.
NOW = datetime(2026, 10, 15, 23, 34, 45, tzinfo=timezone.utc)
UTC_POLICY = cp.CalendarPolicy(bare_date=cp.UTC_DAY, relative_days=cp.UTC_DAY)


def _days(question: str, now: datetime = NOW) -> list:
    """``[(phrase, first day, last day), ...]`` under the UTC-day policy."""
    out = []
    for item in tw.resolve(question, UTC_POLICY, now=now):
        (window,) = item.windows
        out.append((item.phrase, window.first_day, window.last_day))
    return out


D = date


@pytest.mark.parametrize("question, phrase, first, last", [
    ("How many units completed on 8/20?", "8/20", D(2026, 8, 20), D(2026, 8, 20)),
    ("Totals for 8/20/2025 please", "8/20/2025", D(2025, 8, 20), D(2025, 8, 20)),
    ("Totals for 8/20/25 please", "8/20/25", D(2025, 8, 20), D(2025, 8, 20)),
    ("Totals for 8/20/26 please", "8/20/26", D(2026, 8, 20), D(2026, 8, 20)),
    ("What failed on 2026-08-20?", "2026-08-20", D(2026, 8, 20), D(2026, 8, 20)),
    ("What failed on Aug 20?", "Aug 20", D(2026, 8, 20), D(2026, 8, 20)),
    ("What failed on August 20th?", "August 20th", D(2026, 8, 20), D(2026, 8, 20)),
    ("What failed on Aug. 20, 2025?", "Aug. 20, 2025", D(2025, 8, 20), D(2025, 8, 20)),
    ("What failed on 20 August 2025?", "20 August 2025", D(2025, 8, 20), D(2025, 8, 20)),
    ("What ran on the 3rd of March?", "3rd of March", D(2026, 3, 3), D(2026, 3, 3)),
    ("What ran on May 5?", "May 5", D(2026, 5, 5), D(2026, 5, 5)),
    # today counts as a past occurrence; a later day this year means last year
    ("And on 10/15?", "10/15", D(2026, 10, 15), D(2026, 10, 15)),
    ("And on 11/3?", "11/3", D(2025, 11, 3), D(2025, 11, 3)),
])
def test_single_dates(question, phrase, first, last):
    assert _days(question) == [(phrase, first, last)]


@pytest.mark.parametrize("question, phrase, first, last", [
    ("Output for 7/17-7/28", "7/17-7/28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output for 7/17 - 7/28", "7/17 - 7/28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output between 7/17 and 7/28", "between 7/17 and 7/28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output from 7/17 to 7/28", "from 7/17 to 7/28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output 7/17 through 7/28", "7/17 through 7/28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output from July 17 until July 28", "from July 17 until July 28",
     D(2026, 7, 17), D(2026, 7, 28)),
    ("Output for July 17-28", "July 17-28", D(2026, 7, 17), D(2026, 7, 28)),
    ("Output for 2026-07-17 to 2026-07-28", "2026-07-17 to 2026-07-28",
     D(2026, 7, 17), D(2026, 7, 28)),
    # a range with no year is the latest one that has ended by today
    ("Plan for 10/10-10/20", "10/10-10/20", D(2025, 10, 10), D(2025, 10, 20)),
    ("Plan for 10/10-10/15", "10/10-10/15", D(2026, 10, 10), D(2026, 10, 15)),
    ("Output for Oct 12-17", "Oct 12-17", D(2025, 10, 12), D(2025, 10, 17)),
])
def test_ranges_include_the_last_day(question, phrase, first, last):
    assert _days(question) == [(phrase, first, last)]


@pytest.mark.parametrize("question, phrase, first, last", [
    ("What failed today?", "today", D(2026, 10, 15), D(2026, 10, 15)),
    ("What failed yesterday?", "yesterday", D(2026, 10, 14), D(2026, 10, 14)),
    ("What failed this week?", "this week", D(2026, 10, 12), D(2026, 10, 18)),
    ("What failed last week?", "last week", D(2026, 10, 5), D(2026, 10, 11)),
    ("What failed in the last 7 days?", "last 7 days", D(2026, 10, 9), D(2026, 10, 15)),
    ("What failed in the past 3 weeks?", "past 3 weeks", D(2026, 9, 25), D(2026, 10, 15)),
    ("Totals for the past two weeks", "past two weeks", D(2026, 10, 2), D(2026, 10, 15)),
    ("Totals for the past week", "past week", D(2026, 10, 9), D(2026, 10, 15)),
    ("Totals since 10/1", "since 10/1", D(2026, 10, 1), D(2026, 10, 15)),
    ("Totals since yesterday", "since yesterday", D(2026, 10, 14), D(2026, 10, 15)),
])
def test_relative_forms(question, phrase, first, last):
    assert _days(question) == [(phrase, first, last)]


def test_several_expressions_keep_question_order():
    assert _days("Compare 7/20 vs 7/27, and yesterday") == [
        ("7/20", D(2026, 7, 20), D(2026, 7, 20)),
        ("7/27", D(2026, 7, 27), D(2026, 7, 27)),
        ("yesterday", D(2026, 10, 14), D(2026, 10, 14)),
    ]


# --------------------------------------------------------------------------- #
# Year inference across a year boundary
# --------------------------------------------------------------------------- #
JAN = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("question, first, last", [
    ("On 12/28?", D(2026, 12, 28), D(2026, 12, 28)),
    ("On 1/3?", D(2027, 1, 3), D(2027, 1, 3)),
    ("On 1/12?", D(2026, 1, 12), D(2026, 1, 12)),
    ("From 12/28 to 1/3?", D(2026, 12, 28), D(2027, 1, 3)),
    ("Dec 30-Jan 2 totals", D(2026, 12, 30), D(2027, 1, 2)),
    ("From 1/5 to 1/20?", D(2026, 1, 5), D(2026, 1, 20)),
    ("Dec 28-Jan 15 totals", D(2025, 12, 28), D(2026, 1, 15)),
    ("Last week?", D(2026, 12, 28), D(2027, 1, 3)),
])
def test_year_inference_across_the_year_boundary(question, first, last):
    [(_phrase, got_first, got_last)] = _days(question, now=JAN)
    assert (got_first, got_last) == (first, last)


def test_a_leap_day_without_a_year_is_the_latest_one_that_exists():
    assert _days("On 2/29?")[0][1:] == (D(2024, 2, 29), D(2024, 2, 29))


@pytest.mark.parametrize("question, phrase", [
    ("Totals for 8/20/99", "8/20/99"),
    ("Totals for 8/20/27", "8/20/27"),
    ("Totals for 8/20/26-8/20/27", "8/20/26-8/20/27"),
])
def test_a_two_digit_year_after_this_year_is_not_given_a_century(question, phrase):
    """A two-digit year YY is 20YY only when that is not after this year; any
    other is named as unresolved (no window), never read as 2099 or 1999."""
    [item] = tw.resolve(question, UTC_POLICY, now=NOW)
    assert (item.phrase, item.windows) == (phrase, ())
    block = tw.render_block((item,))
    assert f'"{phrase}" could not be resolved' in block
    assert "four-digit year" in block and "do not derive one" in block
    assert "2099" not in block and "UTC," not in block


# --------------------------------------------------------------------------- #
# Left alone: no guess
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "What is 1/2 of the units?",
    "Is 3/4 of the line idle?",
    "Totals for 13/20",
    "Totals for 2/30",
    "Totals for 20/8",
    "Totals on 8/20 at 3pm",
    "Totals on 8/20 at 14:00",
    "Totals at 2026-08-20T10:00:00",
    "Totals at 2026-08-20 10:00",
    "Totals before 8/20",
    "Totals after 8/20",
    "Totals until 8/20",
    "Totals as of 8/20",
    "What happened the day before yesterday?",
    "What happened in the last 24 hours?",
    "What happened in May?",
    "Which units may 5 lines run?",
    "Look up order WO-2026-08-20-01",
    "Look up order 8/20/2026/1",
    "From 7/28 to 7/17 backwards",
    "",
])
def test_unparseable_or_ambiguous_forms_are_left_alone(question):
    assert tw.resolve(question, UTC_POLICY, now=NOW) == ()


def test_a_seventh_date_is_resolved_too():
    question = "Compare " + ", ".join(f"{m}/1" for m in range(1, 8))
    resolved = tw.resolve(question, UTC_POLICY, now=NOW)
    assert [item.phrase for item in resolved] == [f"{m}/1" for m in range(1, 8)]
    assert all(item.windows for item in resolved)


def test_more_expressions_than_the_limit_resolve_nothing_and_say_so():
    dates = [f"{m}/{d}" for m in range(1, 10) for d in (1, 2)][:tw.MAX_EXPRESSIONS + 1]
    question = "Compare " + ", ".join(dates)
    assert len(tw.find_expressions(question)) == tw.MAX_EXPRESSIONS + 1
    resolved = tw.resolve(question, UTC_POLICY, now=NOW)
    assert [item.windows for item in resolved] == [()]
    block = tw.render_block(resolved)
    assert "No window is given" in block and "narrow the question" in block
    assert "UTC," not in block
