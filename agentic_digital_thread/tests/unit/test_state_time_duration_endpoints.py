# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A duration's endpoints are not the window; the disclosure names the effect.

The state-time rule refuses, once, a query that windows a state's rows on
another state's own time, unless the question names that time: the physical
column name used to exempt it from the refusal and the disclosure (it no
longer does: test_state_time_column_name_not_exempt), and the state word
(``started`` for a start time) from the refusal only. Both counted ANY mention,
so a question that spelled out a duration ("complete timestamp minus start
timestamp", ``date_diff('second', start_time, complete_time)``, "between X and
Y") named both of its endpoints, and the case the rule was built for (a
percentile over completed rows windowed on the start time) could no longer be
refused; with the physical names it was not even disclosed.

Now a time mentioned inside a duration or difference phrase (X minus Y, X - Y,
the difference between X and Y, from X to Y, the arguments of a date_diff or
date_add style call) does not name the window. A mention tied to the window
("whose start timestamp falls in the window", "started on", "by start date")
still does. When the reader is unsure, the mention does not name the window:
the cost is one refusal, and the resend path runs it.

The disclosure, when the query ran, says which column placed the rows in the
window, the state's own column it was not, and the effect: rows whose window
column is outside the window or empty are not counted, and rows are counted by
when that other state was reached. Its opener no longer says the question did
not name the window when the question did name the state word. Fully offline;
synthetic names.
"""
from __future__ import annotations

import pytest

from agents import state_times as st
from agents import storage_bound_rule

_TS = "timestamp(6) with time zone"
_COLUMNS = {"id": "varchar", "state": "varchar", "start_time": _TS, "complete_time": _TS,
            "fail_time": _TS, "updated_time": _TS, "line": "varchar", "duration_s": "bigint"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
         f"start_time: {_TS}; complete_time: {_TS}; fail_time: {_TS}; updated_time: {_TS}; "
         "line: varchar e.g. l1, l2; duration_s: bigint)")
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


# A percentile over completed rows windowed on the start time: the case the
# rule was built for.
_P90_ON_START = ("SELECT approx_percentile(date_diff('second', start_time, complete_time), 0.9) "
                 f"AS p90 FROM events WHERE state = 'COMPLETED' AND {_on('start_time')}")
_FAILED_ON_COMPLETE = (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' "
                       f"AND {_on('complete_time')}")
_GROUPED_ON_UPDATED = (f"SELECT state, COUNT(*) AS n FROM events WHERE {_on('updated_time')} "
                       "GROUP BY state")
_WRONG_OR = ("SELECT state, COUNT(*) AS n FROM events WHERE "
             f"(state = 'FAILED' AND {_on('complete_time')}) OR "
             f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


def _decide(sql, question, desc=_DESC, columns=_COLUMNS, table="events"):
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    return st.decide(sql, table, bounded, columns, st.from_tables_desc(desc)[table],
                     question=question)


def _outcome(found) -> str:
    return "none" if found is None else ("refuse" if found.refuse else "mark")


# --------------------------------------------------------------------------- #
# A duration's endpoints do not name the window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    # X minus Y, in words
    "What is the TP90 of complete timestamp minus start timestamp for rows completed on 1/5?",
    "What is the TP90 of the complete time minus the start time for rows completed on 1/5?",
    # the difference between X and Y
    "What is the TP90 of the difference between the complete time and the start time, "
    "for rows completed on 1/5?",
    "What is the TP90 of the time between when a row started and when it completed, on 1/5?",
    # from X to Y as an elapsed time
    "What is the TP90 elapsed time from start to completion for rows completed on 1/5?",
    "What is the TP90 of the start-to-complete time for rows completed on 1/5?",
    "What is the TP90 of the time from start_time to complete_time for rows completed on 1/5?",
    # date_diff / date_add style call arguments
    "What is the TP90 of date_diff('second', start_time, complete_time) for rows completed "
    "on 1/5?",
    "What is the TP90 duration_s for rows completed on 1/5, where completion is "
    "date_add('second', duration_s, start_time)?",
    # X - Y in SQL-ish text
    "What is the TP90 of (complete_time - start_time) for rows completed on 1/5?",
    "What is the TP90 of complete_time \u2013 start_time for rows completed on 1/5?",
    "What is the TP90 of to_unixtime(complete_time) - to_unixtime(start_time) for rows "
    "completed on 1/5?",
    # the delegation shape of a recorded run: a call name, then between X and Y
    "On rows completed on 1/5, compute the duration as the date_diff in seconds between the "
    "row's complete_time and start_time, and return its TP90.",
])
def test_a_duration_endpoint_does_not_name_the_window(question):
    """Before: each was counted as naming the start time (by word, so marked,
    or by column, so not even disclosed). Now the window is refused once."""
    found = _decide(_P90_ON_START, question)
    assert _outcome(found) == "refuse", found
    assert found.windows() == ["start_time"]


@pytest.mark.parametrize(("question", "outcome"), [
    # tied to a window word: the state word still names it (runs, disclosed)
    ("What is the TP90 duration for completed rows whose start timestamp falls in the window "
     "of 1/5?", "mark"),
    ("What is the TP90 duration of completed rows started on 1/5?", "mark"),
    ("What is the TP90 duration of completed rows on 1/5 by start date?", "mark"),
    ("What is the TP90 duration of completed rows started between 1/5 and 1/6?", "mark"),
    # a written column name exempts nothing: refused once, the resend runs;
    # the spaced form reads as the state word (runs, disclosed)
    ("What is the TP90 duration of completed rows with a start_time on 1/5?", "refuse"),
    ("What is the TP90 duration of completed rows by start time on 1/5?", "mark"),
    # a window mention beside a duration: the window mention counts
    ("Among completed rows whose start timestamp falls in the window of 1/5, compute the TP90 "
     "of complete timestamp minus start timestamp.", "mark"),
    ("For completed rows with a start_time on 1/5, the TP90 of complete_time - start_time.",
     "refuse"),
])
def test_a_mention_tied_to_the_window_still_names_it(question, outcome):
    assert _outcome(_decide(_P90_ON_START, question)) == outcome


def test_a_duration_on_a_time_no_state_owns_does_not_name_that_window():
    question = "How many rows by state on 1/5, and the average complete_time minus updated_time?"
    found = _decide(_GROUPED_ON_UPDATED, question)
    assert _outcome(found) == "refuse"
    assert found.windows() == ["updated_time"]
    assert _outcome(_decide(_GROUPED_ON_UPDATED,
                            "How many rows by state by updated_time on 1/5?")) == "refuse"


@pytest.mark.parametrize("question", [
    "How many rows by state on 1/5, and the average of complete_time - start_time?",
    "How many rows by state on 1/5, and the average of complete timestamp minus start "
    "timestamp?",
])
def test_a_duration_does_not_prove_an_or_on_the_state_column(question):
    found = _decide(_WRONG_OR, question)
    assert _outcome(found) == "refuse"
    assert found.under_or is True


def test_the_resend_path_still_runs_a_refused_duration_question():
    """A refusal keeps its "send the same query again unchanged" path."""
    question = "What is the TP90 of complete timestamp minus start timestamp on 1/5?"
    guidance = st.StateTimeWindowError(_decide(_P90_ON_START, question)).to_dict()["guidance"]
    assert ("If the question asks for that window itself, send the same query again "
            "unchanged") in guidance


# --------------------------------------------------------------------------- #
# The disclosure names the state's own column and the effect
# --------------------------------------------------------------------------- #
_STARTED_EFFECT = ("COMPLETED rows were placed in the window by start_time (the time column of "
                   "STARTED rows), not by their own complete_time, so COMPLETED rows whose "
                   "start_time is outside the window or empty are not counted, and COMPLETED "
                   "rows are counted by when they reached the STARTED state")
_COMPLETED_EFFECT = ("FAILED rows were placed in the window by complete_time (the time column "
                     "of COMPLETED rows), not by their own fail_time, so FAILED rows whose "
                     "complete_time is outside the window or empty are not counted, and FAILED "
                     "rows are counted by when they reached the COMPLETED state")


def test_the_named_window_line_names_the_own_column_and_the_effect():
    found = _decide(_P90_ON_START, "What is the TP90 duration of completed rows started on 1/5?")
    assert _outcome(found) == "mark"
    assert st.engine_line([found]) == (
        "[engine] State time windows the question named only by a state word, in this turn's "
        f"queries: in events (by state), {_STARTED_EFFECT}. Each is a state time, not asked for.")


def test_the_unnamed_window_line_keeps_its_opener_and_names_the_effect():
    found = _decide(_FAILED_ON_COMPLETE, "How many rows failed on 1/5?")
    assert _outcome(found) == "refuse"
    assert st.engine_line([found]) == (
        "[engine] State time windows the question did not name, in this turn's queries: "
        f"in events (by state), {_COMPLETED_EFFECT}. Each is a state time, not asked for.")


def test_a_line_over_a_named_and_an_unnamed_window_says_which_is_which():
    named = _decide(_P90_ON_START, "What is the TP90 duration of completed rows started on 1/5?")
    unnamed = _decide(_FAILED_ON_COMPLETE, "How many rows failed on 1/5?")
    assert st.engine_line([named, unnamed]) == (
        "[engine] State time windows in this turn's queries: in events (by state, a window "
        f"the question named only by a state word), {_STARTED_EFFECT}; in events (by state, a "
        f"window the question did not name), {_COMPLETED_EFFECT}. Each is a state time, not "
        "asked for.")


def test_the_requirement_says_the_same_effect():
    found = _decide(_P90_ON_START, "What is the TP90 duration of completed rows started on 1/5?")
    block = st.requirement_block([found])
    assert ("REQUIRED: the answer must say that its COMPLETED figures place COMPLETED rows in "
            "the window by start_time (the time column of STARTED rows), not by their own "
            "complete_time: COMPLETED rows whose start_time is outside the window or empty are "
            "not counted, and COMPLETED rows are counted by when they reached the STARTED "
            "state.") in block
    assert "with no start_time" not in block


@pytest.mark.parametrize(("owner", "other"), [("OPEN", "CLOSED"), ("REJECTED", "OPENED")])
def test_every_state_reads_as_reaching_that_state_never_as_a_verb(owner, other):
    """Dataset-neutral: the rows are counted by when they reached the state
    whose time placed them, said that way for any printable state value, so a
    value is never inflected into a verb ("when they rejected")."""
    own, win = f"{other.lower()}_time", f"{owner.lower()}_time"
    desc = (f"tickets(id: varchar; status: varchar e.g. {owner}, {other}; {win}: {_TS}; "
            f"{own}: {_TS})")
    columns = {"id": "varchar", "status": "varchar", win: _TS, own: _TS}
    sql = f"SELECT COUNT(*) AS n FROM tickets WHERE status = '{other}' AND {_on(win)}"
    found = _decide(sql, f"How many tickets are {other.lower()} on 1/5?", desc, columns,
                    "tickets")
    assert _outcome(found) == "refuse"
    line = st.engine_line([found])
    assert (f"{other} rows were placed in the window by {win} (the time column of {owner} "
            f"rows), not by their own {own}, so {other} rows whose {win} is outside the window "
            f"or empty are not counted, and {other} rows are counted by when they reached the "
            f"{owner} state") in line
    assert f"when they {owner.lower()}" not in line


# --------------------------------------------------------------------------- #
# Every dash that can mean minus, and a date call used as the window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015",
                                  "\u2212", "\u2796", "\u2796\ufe0f", "\ufe58", "\ufe63", "\uff0d"])
def test_every_minus_like_dash_joins_a_duration(dash):
    """A copied formula may carry any of these (an explicit set); each reads as
    minus, so the physical names in it are a duration's ends and the window is
    refused."""
    question = f"TP90 complete_time {dash} start_time for completed rows on 1/5"
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


@pytest.mark.parametrize("compare", ["> 3600", "between 0 and 3600", "in (60, 120)"])
def test_a_compared_duration_of_two_times_is_still_a_duration(compare):
    """Two times in one date call are a duration whether or not it is compared
    to a value: comparing it filters by elapsed time, it does not choose the
    window, so the start-windowed cohort is still refused."""
    question = (f"For completed rows on 1/5, return TP90 where date_diff('second', start_time, "
                f"complete_time) {compare}.")
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


@pytest.mark.parametrize("other_end", [
    "TIMESTAMP '2026-01-01 00:00:00 UTC'",
    "from_iso8601_timestamp(raw_end)",
    "date_add('hour', 1, TIMESTAMP '2026-01-01 00:00:00 UTC')",
    "coalesce(from_iso8601_timestamp(raw_end), current_timestamp)",
    "current_timestamp_snapshot",
])
def test_a_compared_call_with_one_time_is_still_a_duration(other_end):
    """A date call holding a record time, compared to a value, may be an
    elapsed-time filter whatever its other end is (a literal, a converted
    column, a fallback to now), so the start-windowed cohort is still
    refused."""
    question = (f"For completed rows on 1/5, return TP90 where date_diff('second', start_time, "
                f"{other_end}) > 3600.")
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


@pytest.mark.parametrize("question", [
    "For completed rows on 1/5, calculate TP90 duration using start_time and complete_time.",
    "For completed rows on 1/5, calculate TP90 duration based on start_time and complete_time.",
    "For completed rows on 1/5, TP90 with start_time and complete_time as the ends.",
    "For completed rows on 1/5, TP90 of the elapsed seconds (start_time, complete_time).",
    "TP90 for completed rows whose start_time falls on 1/5, measured to complete_time.",
])
def test_two_named_time_columns_name_neither_as_the_window(question):
    """When a question names two of the table's own time columns, the names
    alone cannot say which one is the window (one may be a duration's end,
    however it is worded), so neither counts as naming it: refused once, then
    the resend runs."""
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


def test_one_named_time_column_no_longer_names_the_window():
    """A written column name counts for nothing, even alone: the window is
    refused once and the resend runs (test_state_time_column_name_not_exempt)."""
    question = "TP90 for completed rows whose start_time falls on 1/5."
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


def test_a_fixed_calendar_date_call_window_is_refused_once():
    """Known limit: a date call that does pick rows on a fixed date reads as a
    duration like any date call, so the window is refused once; the resend
    then runs."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE state = 'COMPLETED' AND "
           f"{_on('start_time')}")
    question = "Count completed rows where date_diff('day', start_time, DATE '2026-01-05') = 0."
    assert _outcome(_decide(sql, question)) == "refuse"


@pytest.mark.parametrize("question", [
    "Count completed rows where date_diff('day', start_time, current_date) = 0.",
    "Count completed rows where 0 = date_diff('day', start_time, current_date).",
    "Count completed rows with date_diff('hour', start_time, current_timestamp) < 24.",
])
def test_a_date_call_compared_to_now_is_refused_once(question):
    """Known limit: a date call compared to a value that does pick rows
    relative to now is read like any date call, as a duration, so the window
    on its column is refused once; the resend then runs."""
    sql = ("SELECT COUNT(*) AS n FROM events WHERE state = 'COMPLETED' AND "
           f"{_on('start_time')}")
    assert _outcome(_decide(sql, question)) == "refuse"
