# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A question that writes a state's time column does not exempt the window.

A query that windows one state's rows on another state's own time column is
refused once (or, when the question names the window's state word, run and
disclosed). The question writing the window column's physical name used to
exempt the query from both, so it ran silently. Words cannot tell whether a
written column is the window or something else the question mentions (the
end of a duration, an elapsed time "until now", a column to report), and
every reading of those phrasings left a new way to keep the name visible.
Now the physical name counts for nothing: the window is refused once and the
identical resend runs, disclosed, or it is disclosed when the state word is
there. The same holds for a window on a column no sampled state owns (which
may be the time of a state the samples never showed). Fully offline;
synthetic names.
"""
from __future__ import annotations

import pytest

from agents import state_times as st
from agents import storage_bound_rule

_TS = "timestamp(6) with time zone"
_COLUMNS = {"id": "varchar", "state": "varchar", "start_time": _TS, "complete_time": _TS,
            "fail_time": _TS, "updated_time": _TS, "line": "varchar"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
         f"start_time: {_TS}; complete_time: {_TS}; fail_time: {_TS}; updated_time: {_TS}; "
         "line: varchar e.g. l1, l2)")
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


_P90_ON_START = ("SELECT approx_percentile(date_diff('second', start_time, complete_time), 0.9) "
                 f"AS p90 FROM events WHERE state = 'COMPLETED' AND {_on('start_time')}")
_FAILED_ON_COMPLETE = (f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' "
                       f"AND {_on('complete_time')}")
_WRONG_OR = ("SELECT state, COUNT(*) AS n FROM events WHERE "
             f"(state = 'FAILED' AND {_on('complete_time')}) OR "
             f"(state = 'STARTED' AND {_on('complete_time')}) GROUP BY state")
_GROUPED_ON_UPDATED = (f"SELECT state, COUNT(*) AS n FROM events WHERE {_on('updated_time')} "
                       "GROUP BY state")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


def _decide(sql, question):
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    return st.decide(sql, "events", bounded, _COLUMNS, st.from_tables_desc(_DESC)["events"],
                     question=question)


def _outcome(found) -> str:
    return "none" if found is None else ("refuse" if found.refuse else "mark")


@pytest.mark.parametrize("question", [
    # A duration's other end written in words: only one physical name remains.
    "For completed rows on 1/5, calculate TP90 duration using start_time and the completion "
    "timestamp.",
    # An elapsed time to now: one physical name, no second column.
    "For completed rows on 1/5, calculate TP90 elapsed time from start_time until now.",
    # The column written as the window: still refused once, then the resend runs.
    "TP90 for completed rows whose start_time falls on 1/5.",
    "For completed rows, use start_time on 1/5 as the cohort window.",
])
def test_a_written_column_name_does_not_exempt_another_states_window(question):
    assert _outcome(_decide(_P90_ON_START, question)) == "refuse"


def test_a_written_column_name_after_a_negator_is_refused():
    """The question says the rows have no completion time, and the query
    windows them on it: written names were exempt even here."""
    question = "How many rows failed on 1/5 with no complete_time?"
    assert _outcome(_decide(_FAILED_ON_COMPLETE, question)) == "refuse"


def test_the_state_word_still_runs_disclosed():
    """Unchanged: the window's state word in a positive mention runs the
    query, disclosed."""
    question = "TP90 for completed rows that started on 1/5."
    assert _outcome(_decide(_P90_ON_START, question)) == "mark"


def test_an_or_branch_on_a_written_column_is_refused():
    """The OR form had the same exemption: every branch window written by
    name skipped the state entirely."""
    question = "Count failed and started rows by state whose complete_time falls on 1/5."
    assert _outcome(_decide(_WRONG_OR, question)) == "refuse"


def test_an_or_branch_with_the_state_word_runs_disclosed():
    question = "Count failed and started rows by state for rows completed on 1/5."
    assert _outcome(_decide(_WRONG_OR, question)) == "mark"


def test_a_written_column_no_state_owns_is_refused_once():
    """A window on a column no sampled state owns, which the question writes,
    is refused once like any other: the name exempts nothing here either."""
    question = "Count rows by state whose updated_time falls on 1/5."
    assert _outcome(_decide(_GROUPED_ON_UPDATED, question)) == "refuse"


_CANCEL_COLUMNS = {**_COLUMNS, "cancel_time": _TS}
_CANCEL_DESC = _DESC.replace("updated_time: ", f"cancel_time: {_TS}; updated_time: ")


def test_an_unsampled_states_time_written_as_a_duration_end_is_refused():
    """``cancel_time`` is the time of a state the samples never showed, so no
    sampled state owns it; the question writes it only as a duration's end.
    It used to run silently, counting completed and failed rows by when they
    were cancelled."""
    sql = ("SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', 'FAILED') "
           f"AND {_on('cancel_time')} GROUP BY state")
    question = ("For completed and failed rows on 1/5, count each state and report duration "
                "using cancel_time and the completion timestamp.")
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    found = st.decide(sql, "events", bounded, _CANCEL_COLUMNS,
                      st.from_tables_desc(_CANCEL_DESC)["events"], question=question)
    assert _outcome(found) == "refuse"
