# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A date expression the question itself writes in UTC is the UTC calendar day.

Under a ``site_day`` calendar policy a date with no time at a named site is
that site's working day. A small, strict grammar decides when the question
names the UTC frame for an expression instead, so the phrase is read as the
UTC calendar day whatever ``bare_date`` and ``relative_days`` say, and the
block says why. A cue counts in three forms only:

1. right after the expression, then the clause ends ("between 2026-10-05 and
   2026-10-11 UTC inclusive", "10/5 (UTC).", "10/5 UTC, at site-a", "on 10/5
   in UTC?");
2. a frame noun right before it, which also frames the list it begins ("the
   UTC days 10/5 and 10/6");
3. a sentence that starts with "In UTC,", "Using UTC days," or "Using UTC
   dates,".

Anything else, including a UTC on a time of day, in a quoted string or in a
site's name, keeps the site's working day.

Offline and pure: a fixed clock, synthetic sites.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents import calendar_policy as cp
from agents import time_windows as tw

#: Wednesday 2026-10-21 21:23 UTC (17:23 at site-a, 22:23 at site-b).
NOW = datetime(2026, 10, 21, 21, 23, tzinfo=timezone.utc)

SITE_A = cp.SiteZone(site="site-a", timezone="America/New_York")
SITE_B = cp.SiteZone(site="site-b", timezone="Europe/London")
SITE_DAY = cp.CalendarPolicy(bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY,
                             site_day_start="06:30", sites=(SITE_A, SITE_B))
MIXED = cp.CalendarPolicy(bare_date=cp.UTC_DAY, relative_days=cp.SITE_DAY,
                          site_day_start="06:30", sites=(SITE_A, SITE_B))


def _utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _windows(question, policy=SITE_DAY, now=NOW):
    return [(item.phrase, w.site, w.start_utc, w.end_utc)
            for item in tw.resolve(question, policy, now=now) for w in item.windows]


UTC_RANGE = ("", _utc("2026-10-05 00:00"), _utc("2026-10-12 00:00"))
UTC_DAY_5 = ("", _utc("2026-10-05 00:00"), _utc("2026-10-06 00:00"))
UTC_DAY_6 = ("", _utc("2026-10-06 00:00"), _utc("2026-10-07 00:00"))
UTC_DAY_7 = ("", _utc("2026-10-07 00:00"), _utc("2026-10-08 00:00"))
SITE_A_DAY_5 = ("site-a", _utc("2026-10-05 10:30"), _utc("2026-10-06 10:30"))
SITE_A_DAY_6 = ("site-a", _utc("2026-10-06 10:30"), _utc("2026-10-07 10:30"))


# --------------------------------------------------------------------------- #
# The live case
# --------------------------------------------------------------------------- #
def test_an_explicit_utc_range_at_a_named_site_is_the_utc_calendar_days():
    question = ("Exactly how many records did the site-a cell complete between 2026-10-05 "
                "and 2026-10-11 UTC inclusive? Give one exact number.")
    [item] = tw.resolve(question, SITE_DAY, now=NOW)
    assert item.rule == cp.UTC_DAY
    assert [(w.site, w.start_utc, w.end_utc) for w in item.windows] == [UTC_RANGE]
    assert [s.site for s in item.local_sites] == ["site-a"]
    block = tw.render_block((item,))
    assert ('"between 2026-10-05 and 2026-10-11" is a date range with no time, read as the '
            "UTC calendar day because the question names the UTC frame for it") in block
    assert '"UTC" right after it' in block
    assert "not as a site's working day" in block
    assert "[2026-10-05 00:00:00 UTC, 2026-10-12 00:00:00 UTC)" in block
    assert "Local equivalent: site-a 2026-10-04 20:00 to 2026-10-11 20:00 (UTC-04:00)" in block
    assert "No calendar-policy site was recognized" not in block
    assert "10:30:00 UTC" not in block


# --------------------------------------------------------------------------- #
# The phrasings of each form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question, cue", [
    # Form 1: right after the expression, then the clause ends.
    ("Units at site-a on 2026-10-05 UTC?", '"UTC" right after it'),
    ("Units at site-a on 10/5 GMT?", '"GMT" right after it'),
    ("Units at site-a on 10/5 utc?", '"utc" right after it'),
    ("Units at site-a on 10/5, UTC?", '"UTC" right after it'),
    ("Units at site-a on 10/5 (UTC)?", '"(UTC)" right after it'),
    ("Units at site-a on 10/5 (UTC).", '"(UTC)" right after it'),
    ("Units at site-a on 10/5 in UTC?", '"in UTC" right after it'),
    ("Units on 10/5 UTC, at site-a", '"UTC" right after it'),
    ("Units on 10/5 UTC at site-a", '"UTC" right after it'),
    ("Units on 10/5 UTC for site-a", '"UTC" right after it'),
    ("Units on 10/5 UTC and the scrap at site-a", '"UTC" right after it'),
    ("Did site-a run on 10/5 UTC or not?", '"UTC" right after it'),
    ("Units at site-a on 10/5 in UTC; scrap too", '"in UTC" right after it'),
    ("Units at site-a on 10/5 UTC: list them", '"UTC" right after it'),
    ("Units at site-a on 10/5 UTC!", '"UTC" right after it'),
    ("Units at site-a (on 10/5 UTC) please", '"UTC" right after it'),
    ("Units at site-a on 10/5 UTC\nand the scrap", '"UTC" right after it'),
    # Form 2: a frame noun right before it.
    ("Units at site-a for the UTC day 10/5?", '"UTC day" just before it'),
    ("Units at site-a for UTC day of 10/5?", '"UTC day" just before it'),
    ("Units at site-a on the UTC calendar day 10/5?", '"UTC calendar day" just before it'),
    ("Units at site-a on the GMT day 10/5?", '"GMT day" just before it'),
    # Form 3: a sentence-leading frame.
    ("In UTC, how many units did site-a complete on 10/5?", '"In UTC" leading its sentence'),
    ("Using UTC days, how many units did site-a complete on 10/5?",
     '"Using UTC days" leading its sentence'),
    ("Using UTC dates, how many units did site-a complete on 10/5?",
     '"Using UTC dates" leading its sentence'),
])
def test_each_explicit_utc_phrasing_reads_the_date_as_the_utc_day(question, cue):
    [item] = tw.resolve(question, SITE_DAY, now=NOW)
    assert [(w.site, w.start_utc, w.end_utc) for w in item.windows] == [UTC_DAY_5], question
    assert item.rule == cp.UTC_DAY
    assert cue in tw.render_block((item,)), question


@pytest.mark.parametrize("question", [
    "Units at site-a between 10/5 and 10/11 UTC?",
    "Units at site-a 10/5-10/11 UTC inclusive?",
    "Units at site-a from 10/5 to 10/11 (UTC)?",
    "Units at site-a over the UTC days 10/5 through 10/11?",
    "Units at site-a between 2026-10-05 and 2026-10-11 inclusive, UTC?",
    "Units at site-a between Oct 5 and Oct 11 in UTC?",
    "In UTC, how many units did site-a complete between 10/5 and 10/11?",
])
def test_an_explicit_utc_range_is_the_utc_days(question):
    assert [w[1:] for w in _windows(question)] == [UTC_RANGE], question


@pytest.mark.parametrize("question, start, end", [
    ("What did site-a complete yesterday in UTC?", "2026-10-20 00:00", "2026-10-21 00:00"),
    ("What did site-a complete today (UTC)?", "2026-10-21 00:00", "2026-10-22 00:00"),
    ("What did site-a complete over the past 7 days UTC?", "2026-10-15 00:00",
     "2026-10-22 00:00"),
    ("What did site-a complete since 10/18 UTC?", "2026-10-18 00:00", "2026-10-22 00:00"),
])
def test_an_explicit_utc_relative_phrase_is_the_utc_day_too(question, start, end):
    """``relative_days`` is ``site_day`` too, and the question still wins."""
    assert [w[1:] for w in _windows(question)] == [("", _utc(start), _utc(end))], question


def test_two_named_sites_get_one_utc_window_with_both_local_equivalents():
    [item] = tw.resolve("Units at site-a and site-b on 10/5 UTC?", SITE_DAY, now=NOW)
    assert [(w.site, w.start_utc) for w in item.windows] == [UTC_DAY_5[:2]]
    assert [s.site for s in item.local_sites] == ["site-a", "site-b"]
    block = tw.render_block((item,))
    assert "site-a 2026-10-04 20:00 to 2026-10-05 20:00 (UTC-04:00)" in block
    assert "site-b 2026-10-05 01:00 to 2026-10-06 01:00 (UTC+01:00)" in block


def test_no_named_site_gives_the_explicit_reason_not_the_unrecognized_site_note():
    [item] = tw.resolve("Units on 10/5 UTC?", SITE_DAY, now=NOW)
    assert [(w.site, w.start_utc) for w in item.windows] == [UTC_DAY_5[:2]]
    block = tw.render_block((item,))
    assert "because the question names the UTC frame for it" in block
    assert "No calendar-policy site was recognized" not in block
    assert "site-a " in block and "site-b " in block       # the local equivalents


def test_a_utc_policy_is_unchanged_by_an_explicit_utc():
    """The rule was already the UTC day: the cue decides nothing, the block is
    the one the same date gets without it."""
    with_cue = tw.render_block(tw.resolve("Units at site-a on 10/5 UTC?", MIXED, now=NOW))
    without = tw.render_block(tw.resolve("Units at site-a on 10/5?", MIXED, now=NOW))
    assert with_cue == without
    assert "because the question" not in with_cue


def test_the_compact_block_keeps_the_reason():
    resolved = tw.resolve("Units at site-a on 10/5 UTC?", SITE_DAY, now=NOW)
    compact = tw.render_block(resolved, compact=True)
    assert "because the question names the UTC frame for it" in compact
    assert "Local equivalent" not in compact


# --------------------------------------------------------------------------- #
# Form 1: a cue right after the date counts whatever came earlier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "After reviewing the timestamp format, count units at site-a on 10/5 in UTC.",
    "Give the event times at site-a for 10/5 in UTC.",
    "Convert the site-a stop log for 10/5 in UTC.",
])
def test_a_cue_right_after_the_date_counts_after_earlier_words_about_times(question):
    assert [w[1:] for w in _windows(question)] == [UTC_DAY_5], question


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5 UTC format.",
    "Units at site-a on 10/5 in UTC timestamps.",
    "Units at site-a on Oct 5 UTC day?",
    "Units at site-a on 10/5 in UTC time?",
    "Units at site-a on 10/5 (UTC) please",
    "Units at site-a on 10/5, in UTC.",
    "Units at site-a on 10/5 (in UTC).",
    "Units at site-a on 10/5 in UTC and GMT timestamps.",
    "Units at site-a on 10/5 UTC at 06:00.",
    "Units at site-a on 10/5 UTC, 06:00 onward.",
    'Units at site-a on 10/5 UTC "as logged"',
])
def test_a_form_one_near_miss_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_a_cue_followed_by_a_word_outside_the_grammar_keeps_the_working_day():
    assert [w[1:] for w in _windows("Compare 10/5 UTC with 10/6 at site-a")] == [
        SITE_A_DAY_5, SITE_A_DAY_6]


# --------------------------------------------------------------------------- #
# Form 2: a frame noun right before the date, or the list it begins
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Compare the UTC days 10/5 and 10/6 at site-a.",
    "Units at site-a for the UTC days 10/5 and 10/6",
    "Units at site-a for the UTC dates 10/5, 10/6",
    "Units at site-a on the UTC calendar days 10/5 and 10/6",
    "Units at site-a on the GMT days 10/5 and 10/6",
    "Units at site-a on the UTC day 10/5, and 10/6",
])
def test_a_frame_noun_frames_every_date_of_the_list_it_begins(question):
    assert [w[1:] for w in _windows(question)] == [UTC_DAY_5, UTC_DAY_6], question


def test_a_frame_noun_list_of_three_and_its_reasons():
    resolved = tw.resolve("Compare the UTC dates 10/5, 10/6 and 10/7 at site-a.", SITE_DAY,
                          now=NOW)
    assert [(w.site, w.start_utc, w.end_utc) for item in resolved for w in item.windows] == [
        UTC_DAY_5, UTC_DAY_6, UTC_DAY_7]
    assert [item.utc_cue for item in resolved] == [
        '"UTC dates" just before it', '"UTC dates" just before the list it is in',
        '"UTC dates" just before the list it is in']


def test_a_utc_day_just_before_one_date_belongs_to_that_date():
    """"with" does not join a list, so 10/6 keeps site-a's working day."""
    assert _windows("Compare the UTC day 10/5 with 10/6 at site-a") == [
        ("10/5", *UTC_DAY_5), ("10/6", *SITE_A_DAY_6)]


@pytest.mark.parametrize("question", [
    "Compare the UTC days, 10/5 and 10/6, at site-a.",
    "Compare UTC day totals for 10/5 and 10/6 at site-a.",
    "List the UTC dates for 10/5 and 10/6 at site-a.",
    "Compare UTC daily totals 10/5 and 10/6 at site-a.",
])
def test_a_form_two_near_miss_keeps_the_working_days(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5, SITE_A_DAY_6], question


def test_a_utc_day_before_a_date_counts_even_with_timestamps_in_the_clause():
    assert [w[1:] for w in _windows("Show the timestamps for the UTC day 10/5 at site-a")] == [
        UTC_DAY_5]


# --------------------------------------------------------------------------- #
# Form 3: a sentence-leading frame
# --------------------------------------------------------------------------- #
def test_a_sentence_leading_frame_frames_every_date_in_its_sentence():
    assert [w[1:] for w in _windows("In UTC, compare 10/5 and 10/6 at site-a.")] == [
        UTC_DAY_5, UTC_DAY_6]


def test_a_sentence_leading_frame_in_a_later_sentence_frames_only_that_sentence():
    assert _windows("Units at site-a on 10/5. In UTC, what about 10/6?") == [
        ("10/5", *SITE_A_DAY_5), ("10/6", *UTC_DAY_6)]


@pytest.mark.parametrize("question", [
    "In UTC, units at site-a on 10/5; and on 10/6 at site-a",
    "In UTC, units at site-a on 10/5. And on 10/6 at site-a?",
])
def test_a_sentence_leading_frame_stops_at_its_sentence(question):
    assert _windows(question) == [("10/5", *UTC_DAY_5), ("10/6", *SITE_A_DAY_6)], question


@pytest.mark.parametrize("question", [
    "Source data in UTC, report site-a units on 10/5.",
    "In UTC: how many units did site-a complete on 10/5?",
    "In UTC timestamps, report site-a units on 10/5.",
    "Report, in UTC, the site-a units on 10/5.",
    "Using UTC time, report site-a units on 10/5.",
    "Timestamps in UTC, please: units at site-a on 10/5.",
    "In UTC, at 06:00 on 10/5, report site-a units.",
])
def test_a_form_three_near_miss_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_a_leading_frame_needs_its_comma():
    assert [w[1:] for w in _windows("Using UTC days: compare 10/5 and 10/6 at site-a.")] == [
        SITE_A_DAY_5, SITE_A_DAY_6]


# --------------------------------------------------------------------------- #
# A cue the question rejects, or one that is about display
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Use the non-UTC day 10/5 at site-a.",
    "Do not use the UTC day 10/5; report site-a working-day units instead.",
    "Rather than the UTC day 10/5, use the site-a working day.",
    "Not the UTC day 10/5: units at site-a.",
    "Instead of the UTC day 10/5, count site-a units on its working day.",
    "Don't read 10/5 UTC, report site-a units on its working day.",
    "Units at site-a on 10/5, not 10/5 UTC.",
    "Do not use (the UTC day 10/5); report site-a working-day units instead.",
    "Rather than (the GMT day 10/5), use the site-a working day.",
    "Do not use, for this report, the UTC day 10/5 at site-a.",
])
def test_a_utc_frame_the_question_rejects_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5] * len(_windows(question)), question


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5 UTC for display only.",
    "Units at site-a on 10/5 GMT for timestamp rendering.",
    "Units at site-a on 10/5 UTC for output formatting.",
    "Units at site-a on 10/5 UTC in the report header.",
    "Use site-a's working day on 10/5 UTC for site-a display only.",
    "Use site-a's working day on 10/5 GMT for the site-a report header only.",
    "Query site-a's working day on 10/5 UTC for site-a timestamp rendering only.",
])
def test_a_cue_followed_by_a_purpose_or_place_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


@pytest.mark.parametrize("question", [
    "Using UTC days, at 06:00 compare site-a units on 10/5.",
    "Using UTC dates, at 6 PM compare site-a units on 10/5.",
    "Using UTC days, 06:00 to 08:00 site-a units on 10/5.",
])
def test_a_leading_frame_noun_is_not_a_time_of_days_cue(question):
    assert [w[1:] for w in _windows(question)] == [UTC_DAY_5], question


# --------------------------------------------------------------------------- #
# What it must not change
# --------------------------------------------------------------------------- #
def test_a_bare_date_at_a_named_site_is_still_its_working_day():
    assert [w[1:] for w in _windows("Units at site-a on 10/5?")] == [SITE_A_DAY_5]
    [item] = tw.resolve("Units at site-a on 10/5?", SITE_DAY, now=NOW)
    assert "because the question" not in tw.render_block((item,))


def test_a_utc_after_one_date_does_not_change_another():
    assert _windows("Compare 10/5 UTC at site-a with 10/6") == [
        ("10/5", *UTC_DAY_5), ("10/6", *SITE_A_DAY_6)]


def test_an_in_utc_right_after_one_date_belongs_to_that_date():
    assert _windows("Units at site-a on 10/5 in UTC and on 10/6") == [
        ("10/5", *UTC_DAY_5), ("10/6", *SITE_A_DAY_6)]


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5? Show the timestamps in UTC.",
    "Units at site-a on 10/5; the other report is in UTC",
    "Units at site-a on 10/5\nIn UTC, what about the other line?",
])
def test_a_utc_in_another_clause_changes_nothing(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_a_utc_that_belongs_to_a_time_of_day_changes_nothing():
    """"10/4 23:00 UTC" is a date with a time (no expression); its "UTC"
    belongs to that time, not to 10/5."""
    question = "After the stop at 10/4 23:00 UTC, how many units did site-a make on 10/5?"
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5]
    question = "Units at site-a on 10/5 from 06:30 in UTC"
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5]


def test_a_utc_offset_is_not_the_utc_frame():
    for question in ("Units at site-a on 10/5 UTC-4?", "Units at site-a on 10/5 in UTC+2",
                     "Units at site-a on 10/5 GMT +1"):
        assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_a_utc_inside_a_site_name_is_not_read():
    policy = cp.CalendarPolicy(
        bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY, site_day_start="06:30",
        sites=(cp.SiteZone(site="Plant UTC", timezone="America/New_York"),
               cp.SiteZone(site="utc-east", timezone="America/New_York")))
    for question, site in (("Units on 10/5 at Plant UTC?", "Plant UTC"),
                           ("Units on 10/5 in UTC-East", "utc-east"),
                           ("Units at utc-east (UTC-East line) on 10/5", "utc-east")):
        assert [w[1:] for w in _windows(question, policy)] == [
            (site, _utc("2026-10-05 10:30"), _utc("2026-10-06 10:30"))], question


@pytest.mark.parametrize("question", [
    'Units at site-a on 10/5 whose note says "in UTC"?',
    "Units at site-a on 10/5 whose note says 'in UTC'?",
    "Units at site-a on 10/5 labelled \u201c(UTC)\u201d?",
    "Units at site-a on 10/5 where `zone` = `UTC days`?",
    'Units at site-a on "10/5 UTC"',
])
def test_a_utc_inside_a_quoted_string_is_not_read(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_an_apostrophe_is_not_a_quote():
    """site-a's possessive does not open a quoted string that would hide the cue."""
    assert [w[1:] for w in _windows("What was site-a's count on 10/5 in UTC, and Bob's?")] == [
        UTC_DAY_5]


def test_utc_as_part_of_a_word_is_not_read():
    for question in ("Units at site-a on 10/5 by utc_offset?", "Units at site-a on 10/5 UTCX"):
        assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


def test_an_abbreviated_month_dot_does_not_end_the_sentence():
    assert [w[1:] for w in _windows("In UTC, how many units did site-a make on Oct. 5?")] == [
        UTC_DAY_5]


def test_a_question_with_no_utc_is_byte_identical():
    for question in ("Units at site-a between 10/5 and 10/11?", "What failed yesterday at site-b?",
                     "Units on 10/5", "Compare 7/20 vs 7/27 at site-a and site-b"):
        block = tw.render_block(tw.resolve(question, SITE_DAY, now=NOW))
        assert "because the question" not in block, question
        assert "UTC frame" not in block, question


# --------------------------------------------------------------------------- #
# A UTC about how times are shown, about a time of day, or not next to the
# date: the site's working day stays
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5, show the returned timestamps in UTC.",
    "Units at site-a on 10/5 and show the returned timestamps in UTC.",
    "Units at site-a on 10/5: timestamps in UTC.",
    "Units at site-a on 10/5 (timestamps in UTC).",
    "Units at site-a on 10/5, displayed in UTC.",
    "Units at site-a on 10/5, with the times shown in UTC.",
    "Show the timestamps in UTC on 10/5 at site-a.",
    "Show them in UTC for 10/5 at site-a.",
    "Units at site-a on 10/5 in UTC format.",
    "Timestamps in UTC, please: units at site-a on 10/5.",
])
def test_a_utc_about_how_times_are_shown_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


@pytest.mark.parametrize("question", [
    "At 06:00 (UTC) on 10/5, report site-a units.",
    "At 6 PM in UTC on 10/5, report site-a units.",
    "At 6 p.m. (GMT) on 10/5, report site-a units.",
    "At 06:00 in UTC for 10/5, report site-a units.",
    "At 18:00 UTC on 10/5, report site-a units.",
    "Units at site-a on 10/5 UTC 06:00 onward.",
])
def test_a_utc_on_a_time_of_day_before_or_after_a_date_keeps_the_working_day(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5, counted in UTC?",
    "Units on 10/5 at site-a (UTC)?",
    "Units at site-a between Oct 5 and Oct 11, all in UTC?",
])
def test_a_utc_not_next_to_the_date_and_not_leading_keeps_the_working_day(question):
    windows = [w[1] for w in _windows(question)]
    assert windows == ["site-a"], question


@pytest.mark.parametrize("question", [
    "Compare 10/5 at site-a, and 10/6 is expressed in UTC.",
    "Compare 10/5 at site-a, and 10/6, which is expressed in UTC.",
    "Units at site-a on 10/5 and on 10/6, counted in UTC",
])
def test_a_cue_on_one_date_does_not_change_every_date_in_the_sentence(question):
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5, SITE_A_DAY_6], question


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5 and 10/6 UTC",
    "Units at site-a on 10/5, 10/6 in UTC",
    "Units at site-a on 10/5 or on 10/6 (UTC)",
])
def test_a_cue_on_only_the_last_date_of_a_list_keeps_the_working_day_for_all(question):
    """The cue may be meant for the whole list or for one date: in doubt,
    neither moves, so the list is not read in two frames."""
    assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5, SITE_A_DAY_6], question


def test_a_cue_on_only_the_last_of_three_dates_keeps_the_working_day_for_all():
    assert [w[1] for w in _windows("Units at site-a on 10/5, 10/6 and 10/7 UTC")] == [
        "site-a", "site-a", "site-a"]


@pytest.mark.parametrize("question", [
    "Units at site-a on 10/5, 10/6 UTC and 10/7 UTC",
    "Units at site-a on 10/5 UTC, 10/6 and 10/7 UTC",
])
def test_a_list_with_a_date_left_uncued_before_a_cued_one_keeps_the_working_day_for_all(
        question):
    """Each cue that follows an uncued date may reach back, so no member of the
    list moves and the list is not read in mixed frames."""
    assert [w[1] for w in _windows(question)] == ["site-a", "site-a", "site-a"], question


def test_a_cue_on_the_first_date_of_a_list_belongs_to_that_date():
    assert _windows("Units at site-a on 10/5 UTC and 10/6") == [
        ("10/5", *UTC_DAY_5), ("10/6", *SITE_A_DAY_6)]


def test_a_list_whose_every_date_carries_its_cue_is_utc_days():
    assert [w[1:] for w in _windows("Units at site-a on 10/5 UTC and 10/6 UTC")] == [
        UTC_DAY_5, UTC_DAY_6]


def test_a_cue_after_a_date_led_by_a_time_of_day_keeps_the_working_day():
    for question in ("Units at site-a at 06:00 on 10/5 in UTC",
                     "Units at site-a from 6 PM on 10/5 UTC"):
        assert [w[1:] for w in _windows(question)] == [SITE_A_DAY_5], question


# --------------------------------------------------------------------------- #
# A site named UTC or GMT
# --------------------------------------------------------------------------- #
def _frame_named_policy(name):
    return cp.parse_policy({
        "bare_date": "site_day", "relative_days": "site_day", "site_day_start": "06:30",
        "sites": [{"site": name, "timezone": "America/New_York"},
                  {"site": "site-b", "timezone": "Europe/London"}]})


@pytest.mark.parametrize("name", ["UTC", "GMT"])
def test_a_site_named_like_the_frame_does_not_erase_a_cue_elsewhere(name):
    policy = _frame_named_policy(name)
    question = f"At {name}, report units on 10/5 in {name}."
    [item] = tw.resolve(question, policy, now=NOW)
    assert [(w.site, w.start_utc, w.end_utc) for w in item.windows] == [UTC_DAY_5]
    assert [s.site for s in item.local_sites] == [name]
    assert f'"in {name}" right after it' in tw.render_block((item,))


@pytest.mark.parametrize("name", ["UTC", "GMT"])
def test_a_site_named_like_the_frame_named_only_in_a_cue_shape_keeps_its_working_day(name):
    """The one mention may be the site: in doubt, the site's working day."""
    policy = _frame_named_policy(name)
    for question in (f"Units in {name} on 10/5", f"Units on 10/5 in {name}",
                     f"Units at {name} on 10/5"):
        assert [w[1:] for w in _windows(question, policy)] == [
            (name, _utc("2026-10-05 10:30"), _utc("2026-10-06 10:30"))], question


def test_a_site_named_utc_leading_a_sentence_keeps_its_working_day():
    """"In UTC," may name the site; its only mention has the cue's shape."""
    policy = _frame_named_policy("UTC")
    assert [w[1:] for w in _windows("In UTC, report units on 10/5.", policy)] == [
        ("UTC", _utc("2026-10-05 10:30"), _utc("2026-10-06 10:30"))]
