# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A block's conditional aggregates decide across several time windows at once.

When a block's counting paths carry windows on more than one time column, or
when one aggregate is an OR beside another per-state aggregate, the block is
decided by partitioning the paths into window groups and deciding each group
as the filtered or IN-pin equivalent it equals; every group decision and every
OR decision is its own part (``state_times.decide_parts``). No sub-decision is
returned before the rest of the block is read, so every counted state's window
reaches the refusal, the engine line and the answer requirements.

These tests pin:

* several path-only windows on different columns (one state windowed on
  another's time in each path): both states are refused, each on its own
  window, as two parts matching the two hand-written pinned queries;
* an owned path-only window where the window state and another state are each
  counted on that path-only window: a breakdown refused once, matching the IN
  pin of those two states on that window;
* an OR aggregate with branch-specific predicates beside another per-state
  aggregate: the block keeps the OR decision and the other aggregate's
  state as parts, so that state is present in the disclosure, not dropped.

Each case is paired with its hand-written equivalent and asserted equal, on the
``events`` shape and a differently named ``job_runs`` table, to prove it
generalizes beyond the seed data. Fully offline; synthetic names.
"""
from __future__ import annotations

import importlib

import pytest

import agents.athena_tools as at
from agents import storage_bounds

_TS = "timestamp(6) with time zone"

_COLUMNS = {"id": "varchar", "state": "varchar", "complete_time": _TS, "fail_time": _TS,
            "start_time": _TS, "cancel_time": _TS, "updated_time": _TS, "qty": "double",
            "cell": "varchar"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED, CANCELLED; "
         f"complete_time: {_TS}; fail_time: {_TS}; start_time: {_TS}; cancel_time: {_TS}; "
         f"updated_time: {_TS}; qty: double; cell: varchar e.g. c1, c2)")

_JR_COLUMNS = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "cancel_time": _TS, "updated_time": _TS, "elapsed_s": "double",
               "line": "varchar"}
_JR_DESC = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED, CANCELLED; "
            f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; cancel_time: {_TS}; "
            f"updated_time: {_TS}; elapsed_s: double; line: varchar e.g. a, b)")

_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _w(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


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


def _parts(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    from agents import storage_bound_rule
    st = _st()
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    mapping = st.from_tables_desc(desc)[table]
    return st.decide_parts(sql, table, bounded, columns, mapping, question=question)


def _parts_windows_by_state(parts):
    """``{state: sorted windows}`` across every part's record."""
    out: dict = {}
    for part in parts:
        for s, ws in part.to_dict()["windows"].items():
            out.setdefault(s, set()).update(ws)
    return {s: sorted(ws) for s, ws in out.items()}


def _parts_states(parts):
    return sorted({s for part in parts for s in part.to_dict()["states"]})


def _flag(found):
    """``(refuse, sorted states, sorted windows)`` or ``None``."""
    if found is None:
        return None
    rec = found.to_dict()
    windows = sorted({w for ws in rec["windows"].values() for w in ws})
    return (found.refuse, tuple(sorted(rec["states"])), tuple(windows))


def _windows_by_state(found):
    """``{state: sorted windows}`` from the decision record."""
    return {s: sorted(ws) for s, ws in found.to_dict()["windows"].items()}


# =========================================================================== #
# R3-1: several path-only windows on different columns are each decided as
# their own part, so the block is refused on both windows (not flattened to no
# decision).
# =========================================================================== #
_R31_Q = "How many failed and started records occurred on January 5?"


def test_r31_two_path_only_windows_on_different_columns_are_two_parts():
    """FAILED windowed on complete_time in one CASE and STARTED on fail_time in
    another: the block is refused, as two parts naming both states on their
    own windows (the question names "failed", so the STARTED-on-fail_time part
    alone would run disclosed, as its pinned query does)."""
    sql = ("SELECT "
           f"COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) AS failed, "
           f"COUNT(CASE WHEN state = 'STARTED' AND {_w('fail_time')} THEN 1 END) AS started "
           "FROM events")
    parts = _parts(sql, question=_R31_Q)
    assert parts, "the block must decide, not flatten to no decision"
    assert len(parts) == 2 and any(p.refuse for p in parts) is True
    assert [p.refuse for p in parts] == [True, False]
    wbs = _parts_windows_by_state(parts)
    assert wbs.get("FAILED") == ["complete_time"], wbs
    assert wbs.get("STARTED") == ["fail_time"], wbs
    assert _parts_states(parts) == ["FAILED", "STARTED"]


def test_r31_parts_match_the_two_pinned_equivalents():
    """The parts' states and per-state windows equal the two single-state
    pinned equivalents, one part each."""
    sql = ("SELECT "
           f"COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) AS failed, "
           f"COUNT(CASE WHEN state = 'STARTED' AND {_w('fail_time')} THEN 1 END) AS started "
           "FROM events")
    pinned_failed = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_w('complete_time')}"
    pinned_started = f"SELECT COUNT(*) FROM events WHERE state = 'STARTED' AND {_w('fail_time')}"
    parts = _parts(sql, question=_R31_Q)
    f1 = _decide(pinned_failed, question=_R31_Q)
    f2 = _decide(pinned_started, question=_R31_Q)
    assert f1 is not None and f2 is not None
    assert len(parts) == 2
    wbs = _parts_windows_by_state(parts)
    assert wbs.get("FAILED") == _windows_by_state(f1).get("FAILED")
    assert wbs.get("STARTED") == _windows_by_state(f2).get("STARTED")
    assert [p.refuse for p in parts] == [f1.refuse, f2.refuse]
    assert any(p.refuse for p in parts) is (f1.refuse or f2.refuse)


def test_r31_generalizes_to_job_runs():
    sql = ("SELECT "
           f"COUNT(CASE WHEN status = 'ABORTED' AND {_w('finish_time')} THEN 1 END) AS a, "
           f"COUNT(CASE WHEN status = 'QUEUED' AND {_w('abort_time')} THEN 1 END) AS q "
           "FROM job_runs")
    parts = _parts(sql, question="How many aborted and queued runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    assert parts and any(p.refuse for p in parts) is True
    assert [p.refuse for p in parts] == [True, False]
    wbs = _parts_windows_by_state(parts)
    assert wbs.get("ABORTED") == ["finish_time"], wbs
    assert wbs.get("QUEUED") == ["abort_time"], wbs


# =========================================================================== #
# R3-2: an owned path-only window: the window state and another state each
# counted on that path-only window is a breakdown, refused like the IN pin.
# =========================================================================== #
_R32_Q = "How many completed and failed records occurred on January 5?"


def test_r32_path_only_owned_breakdown_matches_the_in_pin():
    """COMPLETED and FAILED each counted with a path-only complete_time window:
    the owner state (COMPLETED) is not dropped; the pair is the breakdown the
    IN pin of those two states on complete_time is refused for."""
    cond = ("SELECT "
            f"COUNT(CASE WHEN state = 'COMPLETED' AND {_w('complete_time')} THEN 1 END) AS c, "
            f"COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) AS f "
            "FROM events")
    in_pin = ("SELECT COUNT(*) FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_w('complete_time')}")
    got = _decide(cond, question=_R32_Q)
    want = _decide(in_pin, question=_R32_Q)
    assert got is not None and got.refuse is True
    assert _flag(got) == _flag(want), (_flag(got), _flag(want))
    assert _flag(got) == (True, ("FAILED",), ("complete_time",))


def test_r32_path_only_owned_breakdown_generalizes_to_job_runs():
    cond = ("SELECT "
            f"COUNT(CASE WHEN status = 'FINISHED' AND {_w('finish_time')} THEN 1 END) AS c, "
            f"COUNT(CASE WHEN status = 'ABORTED' AND {_w('finish_time')} THEN 1 END) AS a "
            "FROM job_runs")
    in_pin = ("SELECT COUNT(*) FROM job_runs WHERE status IN ('FINISHED', 'ABORTED') "
              f"AND {_w('finish_time')}")
    q = "How many finished and aborted runs on January 5?"
    got = _decide(cond, question=q, table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    want = _decide(in_pin, question=q, table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    assert got is not None and got.refuse is True
    assert _flag(got) == _flag(want)


# =========================================================================== #
# R3-4: an OR aggregate with branch-specific predicates beside another
# per-state aggregate: both are parts, the other state is not dropped.
# =========================================================================== #
_R34_Q = "Of records completed on January 5, how many failed, started, or cancelled?"


def test_r34_or_aggregate_beside_another_keeps_the_other_state():
    """COUNT_IF((FAILED AND qty>0) OR (STARTED AND qty<=0)) beside COUNT(CASE
    CANCELLED): the block must disclose/refuse FAILED, STARTED AND CANCELLED;
    CANCELLED (on complete_time, its non-own window) must not drop out."""
    sql = ("SELECT "
           "COUNT_IF((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty <= 0)) AS fs, "
           "COUNT(CASE WHEN state = 'CANCELLED' THEN 1 END) AS cancelled "
           f"FROM events WHERE {_w('complete_time')}")
    parts = _parts(sql, question=_R34_Q)
    assert parts
    states = set(_parts_states(parts))
    assert "CANCELLED" in states, (states, "CANCELLED dropped by early OR return")
    assert {"FAILED", "STARTED"} & states
    # CANCELLED is windowed on complete_time (not its own cancel_time).
    assert _parts_windows_by_state(parts).get("CANCELLED") == ["complete_time"]


def test_r34_cancelled_present_in_engine_line_and_requirements():
    """CANCELLED's mandatory caveat must appear in both the engine line and
    the FINAL-ANSWER REQUIREMENTS block (it was dropped by the early OR
    return)."""
    st = _st()
    sql = ("SELECT "
           "COUNT_IF((state = 'FAILED' AND qty > 0) OR (state = 'STARTED' AND qty <= 0)) AS fs, "
           "COUNT(CASE WHEN state = 'CANCELLED' THEN 1 END) AS cancelled "
           f"FROM events WHERE {_w('complete_time')}")
    parts = _parts(sql, question="How many failed, started, or cancelled records on January 5?")
    assert parts
    line = st.engine_line(parts)
    block = st.requirement_block(parts)
    # The rendered line/requirements name every counted state. Placeholders
    # stand in for withheld names, so assert the state count via the records.
    assert len(_parts_states(parts)) >= 3, _parts_states(parts)
    assert line and "state time, not asked for" in line
    assert block.count("REQUIRED:") >= 3, block


def test_r34_generalizes_to_job_runs():
    sql = ("SELECT "
           "COUNT_IF((status = 'ABORTED' AND elapsed_s > 0) OR (status = 'QUEUED' AND elapsed_s <= 0)) AS aq, "
           "COUNT(CASE WHEN status = 'CANCELLED' THEN 1 END) AS cancelled "
           f"FROM job_runs WHERE {_w('finish_time')}")
    parts = _parts(sql, question="How many aborted, queued, or cancelled runs on January 5?",
                   table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    assert parts
    states = set(_parts_states(parts))
    assert "CANCELLED" in states, states
    assert {"ABORTED", "QUEUED"} & states


# =========================================================================== #
# Controls: single path cohort, both-own-windows, path-plus-outer unchanged.
# =========================================================================== #
def test_single_path_cohort_named_still_runs_disclosed():
    """One FAILED count with a path-only complete_time window, question naming
    the completed cohort: runs disclosed (not refused), as its pinned
    equivalent does."""
    sql = (f"SELECT COUNT(CASE WHEN state = 'FAILED' AND {_w('complete_time')} THEN 1 END) "
           "FROM events")
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_w('complete_time')}"
    q = "Of records completed on January 5, how many are failed?"
    assert _flag(_decide(sql, question=q)) == _flag(_decide(pinned, question=q))
    assert _flag(_decide(sql, question=q)) == (False, ("FAILED",), ("complete_time",))


def test_both_states_on_their_own_windows_is_no_decision():
    """COMPLETED on complete_time and FAILED on fail_time, each in its own
    path: both measures use their own columns, so no window on another state's
    time: no decision."""
    sql = ("SELECT "
           f"COUNT(CASE WHEN state = 'COMPLETED' AND {_w('complete_time')} THEN 1 END), "
           f"COUNT(CASE WHEN state = 'FAILED' AND {_w('fail_time')} THEN 1 END) "
           "FROM events")
    assert _decide(sql, question=_R32_Q) is None


def test_path_plus_outer_two_windows_is_no_decision():
    """A FAILED own-time path combined with an outer COMPLETED-time window has
    two bounded time columns in the equivalent, so no decision (unchanged)."""
    sql = (f"SELECT COUNT(CASE WHEN state = 'FAILED' AND {_w('fail_time')} THEN 1 END) "
           f"FROM events WHERE {_w('complete_time')}")
    assert _decide(sql, question="Of records completed on January 5, how many failed on January 5?"
                   ) is None
