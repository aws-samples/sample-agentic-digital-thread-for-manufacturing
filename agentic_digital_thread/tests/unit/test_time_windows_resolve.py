# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolving date expressions to half-open UTC windows under a calendar policy.

The policy says what a date with no time means (the UTC calendar day or the
site's working day) and what today, yesterday, this week and last N days mean.
These pins cover:

* the UTC day, and the site working day in each site's own zone and day start,
  on both sides of a daylight-saving change (a 23 h and a 25 h day);
* a question naming one site, several sites, or none (each configured site
  then gets its own window, and the block says why);
* a site named only through the selected plant;
* the rendered block: the phrase, the rule applied, ``[start, end)`` in UTC
  and the local equivalent, plain text with no dash punctuation, and nothing
  at all when the question has no date expression.

Offline and pure: a fixed clock, synthetic sites.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agents import calendar_policy as cp
from agents import time_windows as tw

#: Thursday 2026-10-15 23:34:45 UTC: 19:34 at site-a, 00:34 on 10-16 at site-b.
NOW = datetime(2026, 10, 15, 23, 34, 45, tzinfo=timezone.utc)

SITE_A = cp.SiteZone(site="site-a", timezone="America/New_York")
SITE_B = cp.SiteZone(site="site-b", timezone="Europe/London")
SITE_DAY = cp.CalendarPolicy(bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY,
                             site_day_start="06:30", sites=(SITE_A, SITE_B))
MIXED = cp.CalendarPolicy(bare_date=cp.UTC_DAY, relative_days=cp.SITE_DAY,
                          site_day_start="06:30", sites=(SITE_A, SITE_B))


def _utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _windows(question, policy=SITE_DAY, now=NOW):
    return [(w.site, w.start_utc, w.end_utc)
            for item in tw.resolve(question, policy, now=now) for w in item.windows]


# --------------------------------------------------------------------------- #
# UTC day
# --------------------------------------------------------------------------- #
def test_a_bare_date_under_the_utc_rule_is_the_utc_calendar_day():
    [item] = tw.resolve("Units on 8/20 at site-a?", MIXED, now=NOW)
    assert item.rule == cp.UTC_DAY and item.category == "date"
    [window] = item.windows
    assert (window.start_utc, window.end_utc) == (_utc("2026-08-20 00:00"), _utc("2026-08-21 00:00"))
    assert [s.site for s in item.local_sites] == ["site-a"]


def test_a_relative_day_under_the_utc_rule_ignores_the_sites():
    policy = cp.CalendarPolicy(bare_date=cp.UTC_DAY, relative_days=cp.UTC_DAY,
                               sites=(SITE_A,))
    assert _windows("What failed yesterday?", policy) == [
        ("", _utc("2026-10-14 00:00"), _utc("2026-10-15 00:00"))]


# --------------------------------------------------------------------------- #
# Site working day
# --------------------------------------------------------------------------- #
def test_a_named_site_uses_its_own_zone_and_day_start():
    assert _windows("Units on 7/25 at site-a?") == [
        ("site-a", _utc("2026-07-25 10:30"), _utc("2026-07-26 10:30"))]
    assert _windows("Units on 7/25 at SITE-B?") == [
        ("site-b", _utc("2026-07-25 05:30"), _utc("2026-07-26 05:30"))]


def test_the_working_day_that_has_not_started_yet_is_still_yesterday():
    """09:00 UTC is 05:00 at site-a, before its 06:30 start: 'today' is the
    working day that began the previous morning."""
    early = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    assert _windows("What ran today at site-a?", now=early) == [
        ("site-a", _utc("2026-10-14 10:30"), _utc("2026-10-15 10:30"))]


@pytest.mark.parametrize("question, start, end", [
    # spring forward (2026-03-08 02:00 local): the day before is 23 h
    ("Units on 3/7 at site-a", "2026-03-07 11:30", "2026-03-08 10:30"),
    ("Units on 3/8 at site-a", "2026-03-08 10:30", "2026-03-09 10:30"),
    ("Units 3/7-3/9 at site-a", "2026-03-07 11:30", "2026-03-10 10:30"),
    # fall back (2026-11-01 02:00 local): that working day is 25 h
    ("Units on 10/31/2026 at site-a", "2026-10-31 10:30", "2026-11-01 11:30"),
    ("Units on 11/1/2026 at site-a", "2026-11-01 11:30", "2026-11-02 11:30"),
    # the other zone changes on other dates (2026-03-29 and 2026-10-25)
    ("Units on 3/28 at site-b", "2026-03-28 06:30", "2026-03-29 05:30"),
    ("Units on 10/24 at site-b", "2026-10-24 05:30", "2026-10-25 06:30"),
])
def test_site_days_on_both_sides_of_a_daylight_saving_change(question, start, end):
    assert _windows(question, now=datetime(2026, 12, 1, tzinfo=timezone.utc)) == [
        (question.split(" at ")[1], _utc(start), _utc(end))]


def test_several_named_sites_get_one_window_each():
    [item] = tw.resolve("What failed yesterday at site-a and site-b?", SITE_DAY, now=NOW)
    assert item.site_named is True
    assert [(w.site, w.start_utc, w.end_utc) for w in item.windows] == [
        ("site-a", _utc("2026-10-14 10:30"), _utc("2026-10-15 10:30")),
        ("site-b", _utc("2026-10-14 05:30"), _utc("2026-10-15 05:30")),
    ]


def test_no_named_site_reads_a_site_day_rule_as_the_utc_day_and_says_why():
    """A site's working day applies only to a site the question names. With
    none named, a relative phrase is the UTC day, in one window."""
    [item] = tw.resolve("What failed yesterday?", SITE_DAY, now=NOW)
    assert item.site_named is False and item.rule == cp.UTC_DAY
    assert [(w.site, w.start_utc, w.end_utc) for w in item.windows] == [
        ("", _utc("2026-10-14 00:00"), _utc("2026-10-15 00:00"))]
    block = tw.render_block((item,))
    assert "No calendar-policy site was recognized in the question, so these are UTC days" in block
    assert "a site's working day (starting 06:30 local) applies to a calendar-policy site the question names" in block


def test_a_bare_date_under_a_site_day_rule_with_no_site_is_the_utc_day_too():
    [item] = tw.resolve("Units on 7/25", SITE_DAY, now=NOW)
    assert [(w.site, w.start_utc) for w in item.windows] == [("", _utc("2026-07-25 00:00"))]
    assert "No calendar-policy site was recognized in the question, so these are UTC days" in tw.render_block((item,))


def test_a_site_not_in_the_policy_gets_the_utc_day_with_the_note():
    [item] = tw.resolve("What failed yesterday at site-zz9?", SITE_DAY, now=NOW)
    assert [(w.site, w.start_utc) for w in item.windows] == [("", _utc("2026-10-14 00:00"))]
    block = tw.render_block((item,))
    assert "No calendar-policy site was recognized in the question, so these are UTC days" in block
    # The user DID name a site; the block must not say none was named.
    assert "No site was named" not in block
    assert "a site the question names that is not in the calendar policy has no configured working day" in block


#: Many sites across many zones (generic names, real IANA zones).
WIDE_ZONES = ("America/New_York", "Europe/London", "America/New_York", "America/New_York",
              "America/New_York", "America/New_York", "America/Toronto", "America/Chicago",
              "America/Chicago", "America/Edmonton", "America/Los_Angeles",
              "America/Los_Angeles", "Europe/Rome", "Europe/Warsaw", "Europe/Paris",
              "Europe/Berlin", "Australia/Sydney", "Asia/Tokyo")
WIDE = cp.CalendarPolicy(bare_date=cp.UTC_DAY, relative_days=cp.SITE_DAY, site_day_start="06:30",
                         sites=tuple(cp.SiteZone(site=f"site-{i:02d}", timezone=zone)
                                     for i, zone in enumerate(WIDE_ZONES, start=1)))
LATE = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def test_a_wide_policy_unscoped_relative_phrase_is_one_utc_window_that_fits():
    resolved = tw.resolve("How many units completed over the past two weeks?", WIDE, now=LATE)
    [item] = resolved
    assert [(w.site, w.first_day, w.last_day) for w in item.windows] == [
        ("", date(2026, 10, 3), date(2026, 10, 16))]
    full, compact = tw.render_for_turn(resolved)
    assert len(full) <= tw.KG_CONTEXT_MAX_CHARS and "No window is given" not in full
    assert "No calendar-policy site was recognized in the question, so these are UTC days" in full
    assert "site-01" in full and "and 14 more sites" in full
    assert "site-05 " not in full
    assert "Local equivalent" not in compact


def test_a_wide_policy_named_site_gets_that_sites_working_day():
    [item] = tw.resolve("How many units completed at site-11 over the past two weeks?",
                        WIDE, now=LATE)
    assert [(w.site, w.timezone, w.start_utc, w.end_utc) for w in item.windows] == [
        ("site-11", "America/Los_Angeles", _utc("2026-10-03 13:30"), _utc("2026-10-17 13:30"))]
    assert "No site was named" not in tw.render_block((item,))


def test_a_wide_policy_two_named_sites_get_two_site_day_windows():
    [item] = tw.resolve("What failed yesterday at site-01 and site-02?", WIDE, now=LATE)
    assert [(w.site, w.start_utc) for w in item.windows] == [
        ("site-01", _utc("2026-10-15 10:30")), ("site-02", _utc("2026-10-15 05:30"))]


def test_an_unscoped_bare_date_lists_at_most_four_local_equivalents():
    resolved = tw.resolve("Compare 7/20 vs 7/27", WIDE, now=LATE)
    full, compact = tw.render_for_turn(resolved)
    assert len(full) <= tw.KG_CONTEXT_MAX_CHARS
    assert full.count("and 14 more sites") == 2
    assert all(f"site-0{i} " in full for i in range(1, 5)) and "site-05 " not in full
    assert "Local equivalent" not in compact


def test_a_site_named_by_the_selected_plant_counts():
    prompt = ("[Plant context: site-b \u2014 scope Neptune queries to systems at this "
              "plant. If data isn't found here, mention that and ask if the user wants to "
              "search other plants.] What failed yesterday?")
    assert [w.site for item in tw.resolve(prompt, SITE_DAY, now=NOW)
            for w in item.windows] == ["site-b"]


def test_a_site_override_day_start_wins():
    policy = cp.CalendarPolicy(
        bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY, site_day_start="06:30",
        sites=(cp.SiteZone(site="site-a", timezone="America/New_York", day_start="07:00"),))
    assert _windows("Units on 7/25 at site-a", policy) == [
        ("site-a", _utc("2026-07-25 11:00"), _utc("2026-07-26 11:00"))]


def test_a_site_name_must_be_a_whole_word():
    assert [w.site for item in tw.resolve("Units on 7/25 at site-ab", SITE_DAY, now=NOW)
            for w in item.windows] == [""]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_the_block_states_phrase_rule_utc_window_and_local_equivalent():
    block = tw.render_block(tw.resolve("Units on 8/20 at site-a, and yesterday?", MIXED, now=NOW))
    assert block.startswith(tw.HEADER)
    assert '"8/20"' in block and "UTC calendar day" in block
    assert "[2026-08-20 00:00:00 UTC, 2026-08-21 00:00:00 UTC)" in block
    assert "2026-08-19 20:00 to 2026-08-20 20:00 (UTC-04:00)" in block
    assert '"yesterday"' in block and "working day" in block
    assert "[2026-10-14 10:30:00 UTC, 2026-10-15 10:30:00 UTC)" in block
    assert "Use exactly these windows" in block
    assert "TIMESTAMP '2026-08-20 00:00:00 UTC'" in block
    assert "2026-08-20T00:00:00Z" in block


def test_a_window_across_a_change_shows_each_local_offset():
    block = tw.render_block(tw.resolve("Units 3/7-3/9 at site-a", SITE_DAY,
                                       now=datetime(2026, 12, 1, tzinfo=timezone.utc)))
    assert "2026-03-07 06:30 (UTC-05:00) to 2026-03-10 06:30 (UTC-04:00)" in block
    assert "2026-03-07 through 2026-03-09" in block


def test_the_block_uses_no_dash_punctuation():
    block = tw.render_block(tw.resolve("Units 7/17\u20137/28 and yesterday", SITE_DAY, now=NOW))
    assert block and "\u2014" not in block and "\u2013" not in block


def test_no_expression_renders_nothing():
    assert tw.render_block(()) == ""
    assert tw.render_block(tw.resolve("Which line ran?", SITE_DAY, now=NOW)) == ""


def test_the_compact_form_drops_the_local_equivalents_only():
    resolved = tw.resolve("Units on 8/20, and yesterday?", MIXED, now=NOW)
    full, compact = tw.render_block(resolved), tw.render_block(resolved, compact=True)
    assert len(compact) < len(full)
    assert "[2026-08-20 00:00:00 UTC, 2026-08-21 00:00:00 UTC)" in compact
    assert "[2026-10-14 00:00:00 UTC, 2026-10-15 00:00:00 UTC)" in compact
    assert "UTC-04:00" in full and "UTC-04:00" not in compact


def test_resolution_is_fixed_by_the_clock_and_policy():
    first = tw.render_block(tw.resolve("yesterday", SITE_DAY, now=NOW))
    assert first == tw.render_block(tw.resolve("yesterday", SITE_DAY, now=NOW))
    assert date(2026, 10, 14).isoformat() in first


def test_a_seventh_site_gets_its_own_window():
    sites = tuple(cp.SiteZone(site=f"site-{i}", timezone="UTC") for i in range(8))
    policy = cp.CalendarPolicy(bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY, sites=sites)
    question = "What failed yesterday at " + ", ".join(f"site-{i}" for i in range(8)) + "?"
    [item] = tw.resolve(question, policy, now=NOW)
    assert [w.site for w in item.windows] == [f"site-{i}" for i in range(8)]
    block = tw.render_block((item,))
    assert all(f"site-{i} (UTC" in block for i in range(8))


def test_more_windows_than_the_limit_resolve_nothing_and_say_so():
    count = tw.MAX_WINDOWS // 2 + 1
    sites = tuple(cp.SiteZone(site=f"site-{i}", timezone="UTC") for i in range(count))
    policy = cp.CalendarPolicy(bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY, sites=sites)
    question = "Compare yesterday and today at " + ", ".join(f"site-{i}" for i in range(count))
    resolved = tw.resolve(question, policy, now=NOW)
    assert [item.windows for item in resolved] == [()]
    block = tw.render_block(resolved)
    assert "No window is given" in block and "narrow the question" in block
    assert "UTC," not in block


def test_a_block_too_long_for_the_turn_falls_back_then_says_so(monkeypatch):
    question = "Units on 8/20 at site-a, and yesterday?"
    full = tw.render_block(tw.resolve(question, MIXED, now=NOW))
    compact = tw.render_block(tw.resolve(question, MIXED, now=NOW), compact=True)
    monkeypatch.setattr(tw, "MAX_BLOCK_CHARS", len(compact))
    assert tw.turn_block(question, policy=MIXED, now=NOW) == compact
    assert tw.turn_windows() == (compact, compact)
    monkeypatch.setattr(tw, "MAX_BLOCK_CHARS", len(compact) - 1)
    block = tw.turn_block(question, policy=MIXED, now=NOW)
    assert block != full and "No window is given" in block and "UTC," not in block


def test_one_reference_day_picks_the_year_for_every_site():
    """At 06:00 UTC on 10-16 site-b's working day (07:00 local, past 06:30)
    is 10-16; site-a's (02:00 local) is still 10-15. A date with no year is
    read in the same year for both sites."""
    now = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
    [item] = tw.resolve("Units on 10/16 at site-a and site-b", SITE_DAY, now=now)
    assert [(w.site, w.first_day) for w in item.windows] == [
        ("site-a", date(2026, 10, 16)), ("site-b", date(2026, 10, 16))]


# --------------------------------------------------------------------------- #
# A working-day boundary at a clock change
# --------------------------------------------------------------------------- #
def _starting(start: str) -> cp.CalendarPolicy:
    return cp.CalendarPolicy(bare_date=cp.SITE_DAY, relative_days=cp.SITE_DAY,
                             site_day_start=start, sites=(SITE_A, SITE_B))


LATER = datetime(2026, 12, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("question", ["Units on 3/8 at site-a", "Units on 3/7 at site-a"])
def test_a_boundary_in_the_spring_gap_is_left_unresolved_with_a_note(question):
    """02:30 does not exist at site-a on 2026-03-08 (clocks go from 02:00 to
    03:00): the working day that starts or ends there has no window."""
    [item] = tw.resolve(question, _starting("02:30"), now=LATER)
    assert item.windows == ()
    assert [site for site, _reason in item.unresolved] == ["site-a"]
    block = tw.render_block((item,))
    assert "could not be resolved" in block and "02:30" in block
    assert "does not exist on 2026-03-08" in block
    assert "do not derive one" in block
    assert "UTC," not in block


def test_a_gap_at_one_site_leaves_the_other_sites_resolved():
    [item] = tw.resolve("Units on 3/8 at site-a and site-b", _starting("02:30"), now=LATER)
    assert [w.site for w in item.windows] == ["site-b"]
    assert [site for site, _reason in item.unresolved] == ["site-a"]
    lines = tw.render_block((item,)).splitlines()
    site_lines = [line.strip().split(" ", 1)[0] for line in lines if line.startswith("  ")]
    assert site_lines == ["site-a:", "site-b"]          # policy order kept


@pytest.mark.parametrize("question, start, end, day", [
    ("Units on 11/1/2026 at site-a", "2026-11-01 05:30", "2026-11-02 06:30", "2026-11-01"),
    ("Units on 10/31/2026 at site-a", "2026-10-31 05:30", "2026-11-01 05:30", "2026-11-01"),
])
def test_a_boundary_in_the_fall_overlap_uses_the_earlier_time_and_says_so(question, start,
                                                                          end, day):
    """01:30 happens twice at site-a on 2026-11-01: the earlier one (still on
    daylight time) is used, and the block says so."""
    [item] = tw.resolve(question, _starting("01:30"), now=LATER)
    [window] = item.windows
    assert (window.start_utc, window.end_utc) == (_utc(start), _utc(end))
    block = tw.render_block((item,))
    assert f"01:30 happens twice on {day}" in block and "the earlier one is used" in block


def test_every_boundary_round_trips_to_its_local_start():
    for day in range(1, 32):
        question = f"Units on 3/{day}/2026 at site-a and site-b"
        for item in tw.resolve(question, SITE_DAY, now=LATER):
            for w in item.windows:
                local = w.start_utc.astimezone(ZoneInfo(w.timezone))
                assert local.strftime("%H:%M") == "06:30", (question, w)


def test_a_since_phrase_says_it_runs_through_today():
    block = tw.render_block(tw.resolve("Totals since 10/1", MIXED, now=NOW))
    assert "counted through today" in block
    assert "2026-10-01 through 2026-10-15" in block


def test_the_delegation_merge_falls_back_to_the_compact_block_then_to_a_note():
    windows = ("F" * 900, "C" * 600)
    args = {"question": "q", "kg_context": "R" * 1000}
    assert tw.with_kg_context_windows("sys___query_sys", args, windows)["kg_context"] == (
        "R" * 1000 + "\n\n" + "F" * 900)
    args = {"question": "q", "kg_context": "R" * 1300}
    merged = tw.with_kg_context_windows("sys___query_sys", args, windows)["kg_context"]
    assert merged.endswith("\n\n" + "C" * 600) and len(merged) <= tw.KG_CONTEXT_MAX_CHARS
    room = tw.KG_CONTEXT_MAX_CHARS - len(tw.NO_ROOM_NOTE) - 2
    args = {"question": "q", "kg_context": "R" * room}
    merged = tw.with_kg_context_windows("sys___query_sys", args, windows)["kg_context"]
    assert merged == "R" * room + "\n\n" + tw.NO_ROOM_NOTE
    assert "do not derive" in tw.NO_ROOM_NOTE


@pytest.mark.parametrize("length", [
    tw.KG_CONTEXT_MAX_CHARS - len(tw.NO_ROOM_NOTE) - 1,   # one character short of room
    1990,                                                   # near the bound
    tw.KG_CONTEXT_MAX_CHARS,                                # at the bound
    5000,                                                   # past it (the agent would cut it)
])
def test_the_no_room_note_always_lands_and_trims_only_the_model_context(length):
    windows = ("F" * 900, "C" * 600)
    base = "ROUTE:\ntarget_system: sys-a\n" + "R" * (length - 27)
    args = {"question": "q", "kg_context": base}
    merged = tw.with_kg_context_windows("sys___query_sys", args, windows)
    sent = merged["kg_context"]
    assert sent.endswith("\n\n" + tw.NO_ROOM_NOTE)             # the note is never trimmed
    assert len(sent) <= tw.KG_CONTEXT_MAX_CHARS
    context = sent[:-len("\n\n" + tw.NO_ROOM_NOTE)]
    assert context.endswith("\n" + tw.TRIM_MARKER)
    kept = context[:-len("\n" + tw.TRIM_MARKER)]
    assert base.startswith(kept) and kept.startswith("ROUTE:\ntarget_system: sys-a")
    # just enough was trimmed: one more character of context would not fit
    assert len(sent) + 1 > tw.KG_CONTEXT_MAX_CHARS
    assert args == {"question": "q", "kg_context": base}     # the caller's dict is untouched
    assert merged["question"] == "q"


def test_the_note_survives_the_system_agents_own_bound():
    from agents import system_agent
    args = {"question": "q", "kg_context": "R" * 1990}
    sent = tw.with_kg_context_windows("sys___query_sys", args, ("F" * 900, "C" * 600))
    assert system_agent._sanitize_kg_context(sent["kg_context"]).endswith(tw.NO_ROOM_NOTE)


def test_the_note_and_marker_always_fit_the_bound():
    assert len(tw.NO_ROOM_NOTE) + len(tw.TRIM_MARKER) + 3 < tw.KG_CONTEXT_MAX_CHARS // 2


def test_the_kg_context_bound_matches_the_system_agent():
    from agents import system_agent
    assert tw.KG_CONTEXT_MAX_CHARS == system_agent._KG_CONTEXT_MAX_CHARS
