# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A block's conditional aggregates decide together, as the filtered or
grouped equivalent does.

Each conditional aggregate is read not alone but with the others in the block,
so a query whose conditional spelling is equivalent to a filtered COUNT, a
GROUP BY or an IN pin decides exactly as that equivalent does. These tests pin
the forms where a per-aggregate-only reading diverged from the block
equivalent:

* a time window written only inside the conditional (no window in the outer
  WHERE) is a decision of the outer query, exactly as its pinned equivalent
  (which is refused);
* two conditional counts that together form a breakdown decide as the GROUP BY
  or IN pin of those states: on a window one of them owns, the pair is refused
  once; on a column no state owns, the pair is refused like the IN pin;
* a CASE count with a FILTER ANDs both conditions into every counting path;
* an OR whose branches carry branch-specific predicates decides as the same OR
  carried into the WHERE (the OR reader reads its branches);

plus two text-fidelity properties that change no decision: an earlier branch
is excluded as ``c IS NOT TRUE`` (``NOT COALESCE((c), FALSE)`` in Trino, which
keeps a row where ``c`` is UNKNOWN, unlike ``NOT (c)``), and the built
equivalent keeps the relation alias so a reader's ``b.state`` binds.

Each test pairs the conditional spelling with the hand-written filtered,
GROUP BY or IN-pin equivalent and asserts the same decision, on the logged
``events`` shape and on a differently named ``job_runs`` table, to prove it
generalizes beyond the seed data.

Fully offline; synthetic names.
"""
from __future__ import annotations

import importlib

import pytest

import agents.athena_tools as at
from agents import storage_bounds

_TS = "timestamp(6) with time zone"

# The events-shaped parity schema (a qty column, so SUM of a column reads).
_COLUMNS = {"id": "varchar", "state": "varchar", "complete_time": _TS, "fail_time": _TS,
            "start_time": _TS, "updated_time": _TS, "qty": "double", "cell": "varchar"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
         f"complete_time: {_TS}; fail_time: {_TS}; start_time: {_TS}; updated_time: {_TS}; "
         "qty: double; cell: varchar e.g. c1, c2)")

# A differently named schema: job_runs(status FINISHED/ABORTED/QUEUED) with
# finish_time/abort_time/queue_time own columns and an unowned updated_time.
_JR_COLUMNS = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "updated_time": _TS, "elapsed_s": "double", "line": "varchar"}
_JR_DESC = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED; "
            f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; updated_time: {_TS}; "
            "elapsed_s: double; line: varchar e.g. a, b)")

_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"
_WIN = f"complete_time >= {_DAY} AND complete_time < {_NEXT}"
_UPD = f"updated_time >= {_DAY} AND updated_time < {_NEXT}"
_FAIL_WIN = f"fail_time >= {_DAY} AND fail_time < {_NEXT}"
_JR_WIN = f"finish_time >= {_DAY} AND finish_time < {_NEXT}"
_JR_UPD = f"updated_time >= {_DAY} AND updated_time < {_NEXT}"


def _st():
    return importlib.import_module("agents.state_times")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass


def _decide(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    from agents import storage_bound_rule
    st = _st()
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    mapping = st.from_tables_desc(desc)[table]
    return st.decide(sql, table, bounded, columns, mapping, question=question)


def _flag(found):
    """``(refuse, sorted states, window)`` or ``None``, to compare parity
    between a conditional spelling and its pinned/grouped/IN equivalent."""
    if found is None:
        return None
    rec = found.to_dict()
    return (found.refuse, tuple(sorted(rec["states"])), rec["window_column"])


def _parity(conditional_sql, equivalent_sql, *, question, **kw):
    got = _decide(conditional_sql, question=question, **kw)
    want = _decide(equivalent_sql, question=question, **kw)
    assert _flag(got) == _flag(want), (_flag(got), _flag(want))
    return _flag(got)


# =========================================================================== #
# R3-1: a time window written only inside the conditional is a decision of the
# outer query, exactly as its pinned equivalent (which is refused).
# =========================================================================== #
_R31_Q = "How many failed records occurred on January 5?"
_R31_FORMS = [
    f"COUNT(CASE WHEN state = 'FAILED' AND {_WIN} THEN 1 END)",
    f"COUNT(*) FILTER (WHERE state = 'FAILED' AND {_WIN})",
    f"COUNT_IF(state = 'FAILED' AND {_WIN})",
]


@pytest.mark.parametrize("agg", _R31_FORMS)
def test_r31_a_path_only_window_matches_its_pinned_equivalent(agg):
    """A window only inside the conditional decides like
    ``SELECT COUNT(*) WHERE state = 'FAILED' AND <window>``: refused FAILED."""
    cond = f"SELECT {agg} FROM events"
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_WIN}"
    assert _parity(cond, pinned, question=_R31_Q) == (True, ("FAILED",), "complete_time")


def test_r31_generalizes_to_job_runs():
    cond = f"SELECT COUNT(CASE WHEN status = 'ABORTED' AND {_JR_WIN} THEN 1 END) FROM job_runs"
    pinned = f"SELECT COUNT(*) FROM job_runs WHERE status = 'ABORTED' AND {_JR_WIN}"
    assert _parity(cond, pinned, question="How many aborted runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS) == (
        True, ("ABORTED",), "finish_time")


# =========================================================================== #
# R3-2: two conditional counts that together form a breakdown decide as their
# GROUP BY / IN-pin equivalent (owned window: refused once; unowned: refused).
# =========================================================================== #
_R32_Q = "How many completed and failed records occurred on January 5?"


def test_r32_owned_two_conditional_counts_are_a_breakdown_refused_once():
    """``COUNT(CASE COMPLETED)`` beside ``COUNT(CASE FAILED)`` on COMPLETED's
    own column decides like the IN-pin of those two states: a breakdown,
    refused once (window state COMPLETED counted beside FAILED on its own
    column)."""
    cond = ("SELECT COUNT(CASE WHEN state = 'COMPLETED' THEN 1 END) AS completed, "
            "COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS failed "
            f"FROM events WHERE {_WIN}")
    in_pin = ("SELECT state, COUNT(*) FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_WIN} GROUP BY state")
    assert _parity(cond, in_pin, question=_R32_Q) == (True, ("FAILED",), "complete_time")


def test_r32_unowned_two_conditional_counts_are_refused_like_the_in_pin():
    """The same pair on a column no state owns decides like the IN-pin of the
    two counted states: refused, both states, on the unowned column."""
    cond = ("SELECT COUNT(CASE WHEN state = 'COMPLETED' THEN 1 END) AS completed, "
            "COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS failed "
            f"FROM events WHERE {_UPD}")
    in_pin = ("SELECT state, COUNT(*) FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_UPD} GROUP BY state")
    assert _parity(cond, in_pin, question=_R32_Q) == (
        True, ("COMPLETED", "FAILED"), "updated_time")


def test_r32_owned_generalizes_to_job_runs():
    cond = ("SELECT COUNT(CASE WHEN status = 'FINISHED' THEN 1 END) AS finished, "
            "COUNT(CASE WHEN status = 'ABORTED' THEN 1 END) AS aborted "
            f"FROM job_runs WHERE {_JR_WIN}")
    in_pin = ("SELECT status, COUNT(*) FROM job_runs WHERE status IN ('FINISHED', 'ABORTED') "
              f"AND {_JR_WIN} GROUP BY status")
    assert _parity(cond, in_pin, question="How many finished and aborted runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS) == (
        True, ("ABORTED",), "finish_time")


def test_r32_unowned_generalizes_to_job_runs():
    cond = ("SELECT COUNT(CASE WHEN status = 'FINISHED' THEN 1 END) AS finished, "
            "COUNT(CASE WHEN status = 'ABORTED' THEN 1 END) AS aborted "
            f"FROM job_runs WHERE {_JR_UPD}")
    in_pin = ("SELECT status, COUNT(*) FROM job_runs WHERE status IN ('FINISHED', 'ABORTED') "
              f"AND {_JR_UPD} GROUP BY status")
    assert _parity(cond, in_pin, question="How many finished and aborted runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS) == (
        True, ("ABORTED", "FINISHED"), "updated_time")


def test_r32_one_conditional_count_on_an_owned_window_other_state_still_disclosed():
    """A single conditional count of FAILED on COMPLETED's own column (no
    window-state count beside it) decides like ``COUNT(*) WHERE state =
    'FAILED'``: a decision disclosed (the question names the window state
    word), not a breakdown."""
    cond = f"SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS c FROM events WHERE {_WIN}"
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_WIN}"
    # The question names "completed": disclosed, not refused.
    q = "Of the records completed today, how many are now failed?"
    assert _parity(cond, pinned, question=q) == (False, ("FAILED",), "complete_time")


def test_r32_one_state_cohort_on_an_unowned_column_still_runs():
    """A single-state conditional count on a column no state owns is a cohort
    of that state's rows: it runs (no decision). The breakdown refusal is only
    for the window's own state counted beside another, or an IN-pin / GROUP BY
    of two or more states on the shared window."""
    # One FAILED count on an unowned column: a lone cohort, so it runs.
    cond = ("SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS f "
            f"FROM events WHERE {_UPD} GROUP BY cell")
    assert _decide(cond, question="How many failed by cell on 1/5?") is None


# =========================================================================== #
# R3-3: a CASE count with a FILTER ANDs both conditions.
# =========================================================================== #
def test_r33_case_plus_filter_ands_both_conditions():
    """``COUNT(CASE WHEN state = 'FAILED' THEN 1 END) FILTER (WHERE qty > 0)``
    decides like ``WHERE state = 'FAILED' AND qty > 0``: refused FAILED."""
    cond = ("SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) FILTER (WHERE qty > 0) "
            f"FROM events WHERE {_WIN}")
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND qty > 0 AND {_WIN}"
    assert _parity(cond, pinned,
                   question="How many failed positive-quantity records on January 5?") == (
        True, ("FAILED",), "complete_time")


def test_r33_filter_supplies_the_state_pin_and_case_a_time_window():
    """The FILTER carries the state pin and the CASE a time window: both are
    ANDed, so the equivalent names the counted state's own time (no decision),
    exactly as its pinned form."""
    cond = (f"SELECT COUNT(CASE WHEN {_FAIL_WIN} THEN 1 END) FILTER (WHERE state = 'FAILED') "
            f"FROM events WHERE {_WIN}")
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_FAIL_WIN} AND {_WIN}"
    assert _parity(cond, pinned, question=_R31_Q) is None  # two bounded windows: no decision


def test_r33_generalizes_to_job_runs():
    cond = ("SELECT COUNT(CASE WHEN status = 'ABORTED' THEN 1 END) FILTER (WHERE elapsed_s > 0) "
            f"FROM job_runs WHERE {_JR_WIN}")
    pinned = (f"SELECT COUNT(*) FROM job_runs WHERE status = 'ABORTED' AND elapsed_s > 0 "
              f"AND {_JR_WIN}")
    assert _parity(cond, pinned, question="How many aborted positive runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS) == (
        True, ("ABORTED",), "finish_time")


# =========================================================================== #
# R3-4: an OR whose branches carry branch-specific predicates decides like the
# same OR in the WHERE (the OR reader).
# =========================================================================== #
_R34_Q = "How many failed or started records occurred on January 5?"


def test_r34_or_with_branch_specific_filters_matches_the_or_pinned_equivalent():
    """``COUNT_IF((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty
    <= 0))`` decides like the same OR carried into the WHERE (refused, under
    OR, FAILED and STARTED)."""
    cond = ("SELECT COUNT_IF((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty <= 0)) "
            f"FROM events WHERE {_WIN}")
    or_pinned = ("SELECT COUNT(*) FROM events WHERE "
                 "((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty <= 0)) "
                 f"AND {_WIN}")
    got = _decide(cond, question=_R34_Q)
    want = _decide(or_pinned, question=_R34_Q)
    assert got is not None
    assert _flag(got) == _flag(want)
    assert got.under_or is True


def test_r34_case_form_of_the_branch_specific_or_matches_its_pinned_equivalent():
    cond = ("SELECT COUNT(CASE WHEN (state = 'FAILED' AND qty > 0) "
            "OR (state = 'STARTED' AND qty <= 0) THEN 1 END) "
            f"FROM events WHERE {_WIN}")
    or_pinned = ("SELECT COUNT(*) FROM events WHERE "
                 "((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty <= 0)) "
                 f"AND {_WIN}")
    assert _flag(_decide(cond, question=_R34_Q)) == _flag(_decide(or_pinned, question=_R34_Q))


def test_r34_bare_or_of_state_pins_still_matches_its_or_pinned_equivalent():
    """The already-supported bare OR of state pins stays correct."""
    cond = (f"SELECT COUNT_IF(state = 'FAILED' OR state = 'STARTED') FROM events WHERE {_WIN}")
    or_pinned = (f"SELECT COUNT(*) FROM events WHERE (state = 'FAILED' OR state = 'STARTED') "
                 f"AND {_WIN}")
    assert _flag(_decide(cond, question=_R34_Q)) == _flag(_decide(or_pinned, question=_R34_Q))


def test_r34_generalizes_to_job_runs():
    cond = ("SELECT COUNT_IF((status = 'ABORTED' AND elapsed_s > 0) "
            "OR (status = 'QUEUED' AND elapsed_s <= 0)) "
            f"FROM job_runs WHERE {_JR_WIN}")
    or_pinned = ("SELECT COUNT(*) FROM job_runs WHERE "
                 "((status = 'ABORTED' AND elapsed_s > 0) OR (status = 'QUEUED' AND elapsed_s <= 0)) "
                 f"AND {_JR_WIN}")
    q = "How many aborted or queued runs on January 5?"
    got = _decide(cond, question=q, table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    want = _decide(or_pinned, question=q, table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    assert got is not None and _flag(got) == _flag(want)


# =========================================================================== #
# R3-6: the built equivalent SQL is faithful -- an earlier branch excluded as
# "c IS NOT TRUE" (NOT COALESCE((c), FALSE)), and the relation alias kept.
# =========================================================================== #
def _captured_equivalents(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    """Run decide() and capture every built equivalent SQL string."""
    st = _st()
    from agents import storage_bound_rule
    captured = []
    original = st._decide_equivalent

    def capture(eq_sql, eq_table, eq_types, eq_mappings):
        captured.append(eq_sql)
        return original(eq_sql, eq_table, eq_types, eq_mappings)

    st._decide_equivalent = capture
    try:
        bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
        mapping = st.from_tables_desc(desc)[table]
        st.decide(sql, table, bounded, columns, mapping, question=question)
    finally:
        st._decide_equivalent = original
    return captured


def test_r36_earlier_branch_exclusion_uses_is_not_true_semantics():
    """``CASE WHEN qty > 0 THEN NULL WHEN state = 'FAILED' THEN 1 END``: the
    later branch's path excludes the earlier UNKNOWN-or-TRUE condition with
    ``NOT COALESCE((qty > 0), FALSE)``, not ``NOT (qty > 0)``, so a FAILED row
    whose qty is NULL is kept (CASE fallthrough semantics)."""
    sql = ("SELECT COUNT(CASE WHEN qty > 0 THEN NULL WHEN state = 'FAILED' THEN 1 END) "
           f"FROM events WHERE {_WIN}")
    captured = _captured_equivalents(sql, question=_R31_Q)
    assert captured, "no equivalent was built"
    joined = " ".join("".join(c.split()) for c in captured)
    assert "NOTCOALESCE((qty>0),FALSE)" in joined or "NOTCOALESCE((qty>0),false)" in joined, captured
    assert "NOT(qty>0)AND" not in joined, captured


def test_r36_earlier_branch_exclusion_parity_is_unchanged():
    """The fidelity fix changes no decision: the earlier-branch form still
    decides like its pinned equivalent (the WHERE names FAILED's own column
    via the negation, so no decision)."""
    cond = ("SELECT COUNT(CASE WHEN fail_time IS NULL THEN NULL WHEN state = 'FAILED' THEN 1 END) "
            f"FROM events WHERE {_WIN}")
    pinned = (f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' "
              f"AND NOT COALESCE((fail_time IS NULL), FALSE) AND {_WIN}")
    assert _parity(cond, pinned, question=_R31_Q) is None


def test_r36_cte_reader_keeps_the_relation_alias_in_the_equivalent():
    """A CTE/derived reader that qualifies the state column with its alias
    (``b.state``) builds an equivalent whose FROM keeps that alias, so the
    built text is executable SQL (the alias binds the qualifier)."""
    cte = (f"WITH base AS (SELECT * FROM events WHERE {_WIN}) "
           "SELECT COUNT(CASE WHEN b.state = 'FAILED' THEN 1 END) FROM base b")
    captured = _captured_equivalents(cte, question=_R31_Q)
    assert captured, "no equivalent was built"
    # Every captured equivalent that qualifies with b. must bind b in FROM.
    for eq in captured:
        flat = " ".join(eq.split())
        if "b.state" in flat.replace("b . state", "b.state"):
            assert "events b" in flat or "events AS b" in flat or "events as b" in flat, eq


def test_r36_derived_reader_keeps_the_relation_alias_in_the_equivalent():
    derived = ("SELECT COUNT(CASE WHEN b.state = 'FAILED' THEN 1 END) "
               f"FROM (SELECT * FROM events WHERE {_WIN}) b")
    captured = _captured_equivalents(derived, question=_R31_Q)
    assert captured, "no equivalent was built"
    for eq in captured:
        flat = " ".join(eq.split())
        if "b.state" in flat.replace("b . state", "b.state"):
            assert "events b" in flat or "events AS b" in flat or "events as b" in flat, eq
