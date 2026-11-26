# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Permits bound the calls exact splits run, without costing unrelated answers.

A review of the previous bound (``MAX_ABANDONED``) found two faults. It was a
snapshot of the calls splits had stopped waiting for, checked when a split
started, so two stalled splits (each leaving its pieces and a stop call) made
every later split in the process refuse, even one on another client with ample
time. And the check and the registration were separate, so a split admitted
with one slot left could add five calls, and splits started together could all
pass it.

Now every call a split starts (a catalog, workgroup or metadata call, a client
build, a piece, a stop call) takes a permit from one process-wide pool
(``MAX_SPLIT_CALLS``) BEFORE it starts, and gives it back in its own ending,
not when the split stops waiting for it. A split waits for a permit up to its
own deadline and refuses (``wall_time``, no total) only when none frees in that
time. The split's thread blocks for a permit only while it holds none, a
piece or worker never takes a second one, and a stop call that finds no permit
free is not started (the pieces' own polls stop their queries at the
deadline), so a split never waits on itself. A client a call still running
holds is still never used again until that call ends.

Real threads and the real clock, except that the test of a split called
through the agent's tool runs the split on a clock that starts when the split
does (``_athena_split_clock``); offline (``FakeLake``); synthetic names only.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import agents.athena_tools as at
from agents import athena_exact_split as sp

from . import test_athena_exact_split as base
from . import test_athena_exact_split_retry as retry
from . import test_system_agent_call_budget as cb
from ._athena_split_clock import split_clock
from ._athena_split_fakes import FakeLake
from .test_athena_exact_split_reserve_holds import (_Blocking, _deadline, _drained, _lake,
                                                    _metadata, _pieces, _split)

_RESERVE_ENV = "SYSTEM_AGENT_SPLIT_RESERVE_SECONDS"
_NO_SLOT = "none came free in time"


@pytest.fixture(autouse=True)
def _quick_polls_fresh_state(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)
    for clear in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, sp.clear_layout_cache):
        clear()
    yield
    for clear in (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, sp.clear_layout_cache):
        clear()


def _bound(monkeypatch, total: int):
    """A pool of ``total`` permits in place of the process's own."""
    permits = sp._Permits(total)
    monkeypatch.setattr(sp, "_PERMITS", permits)
    return permits


def _together(calls) -> list:
    """Run every call in its own thread, all released at once by a barrier;
    each outcome is ``("ok", value)`` or ``("error", exception)``."""
    barrier = threading.Barrier(len(calls))
    outcomes: list = [None] * len(calls)

    def run(index, call):
        barrier.wait()
        try:
            outcomes[index] = ("ok", call())
        except BaseException as exc:  # noqa: BLE001 - recorded for the test
            outcomes[index] = ("error", exc)
    threads = [threading.Thread(target=run, args=(i, c), daemon=True) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return outcomes


def _stalled_workgroup(n: int, seconds: float) -> list:
    """``n`` splits started together, each on its own client, whose workgroup
    read hangs: each is refused at its deadline and leaves that read running."""
    athenas = [_Blocking(_lake(), "get_work_group", 30.0) for _ in range(n)]
    outcomes = _together([lambda a=a: _split(a, a.inner.glue, limit=_deadline(seconds))
                          for a in athenas])
    assert all(kind == "error" and isinstance(exc, sp.ExactSplitRefused) for kind, exc in outcomes)
    return athenas


def _stalled_pieces(n: int, seconds: float) -> list:
    """``n`` splits started together, each on its own client, whose pieces
    never finish and whose stop calls hang: each leaves its probe piece and
    its stop call running."""
    lakes = [_lake() for _ in range(n)]
    for lake in lakes:
        lake.slow_piece_containing = "FOR VERSION AS OF"
    athenas = [_Blocking(lake, "stop_query_execution", 30.0) for lake in lakes]
    outcomes = _together([lambda a=a: _split(a, a.inner.glue, limit=_deadline(seconds))
                          for a in athenas])
    assert all(kind == "error" and isinstance(exc, sp.ExactSplitRefused) for kind, exc in outcomes)
    return athenas


def _release(athenas) -> None:
    for athena in athenas:
        athena.release.set()


def _grouped_lake() -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=4_000_000, estimate_factor=2.0)


# --------------------------------------------------------------------------- #
# The pool
# --------------------------------------------------------------------------- #
def test_a_permit_is_taken_whole_and_given_back_once():
    permits = sp._Permits(2)
    assert permits.take(0) and permits.take(0)
    assert not permits.take(0) and not permits.take(0.05)
    assert permits.used == permits.peak == 2
    permits.give()
    assert permits.take(0)
    permits.give()
    permits.give()
    assert permits.used == 0
    with pytest.raises(RuntimeError):
        permits.give()


def test_the_permits_cover_every_split_the_app_runs_at_once():
    # The app resolves at most RESOLVER_MAX_CONCURRENCY metrics and
    # METRIC_TURN_MAX_WORKERS notice metrics at once; each split runs at most
    # CONCURRENCY pieces and one stop call.
    from agents import metric_turn_notice, offload
    splits = offload.RESOLVER_MAX_CONCURRENCY + metric_turn_notice.METRIC_TURN_MAX_WORKERS
    assert sp.MAX_SPLIT_CALLS >= splits * (sp.CONCURRENCY + 1)
    assert sp._PERMITS.total == sp.MAX_SPLIT_CALLS


# --------------------------------------------------------------------------- #
# The bound holds
# --------------------------------------------------------------------------- #
def test_one_permit_left_and_a_split_leaving_many_calls_stays_in_the_bound(monkeypatch):
    permits = _bound(monkeypatch, 5)
    held = []
    try:
        held += _stalled_workgroup(4, 1.2)              # all but one permit held
        assert permits.used == 4 and len(sp._still_running()) == 4
        # Its pieces never finish and its stop calls hang: on the commit
        # before, this split was admitted and left a piece and a stop call.
        lake = _lake()
        lake.slow_piece_containing = "FOR VERSION AS OF"
        athena = _Blocking(lake, "stop_query_execution", 30.0)
        held.append(athena)
        with pytest.raises(sp.ExactSplitRefused) as info:
            _split(athena, lake.glue, limit=_deadline(3.0))
        assert info.value.reason == "wall_time"
        assert permits.peak <= 5 and permits.used <= 5
        assert len(sp._still_running()) <= 5
        slow = [qid for qid, sql in lake._queries.items() if "FOR VERSION AS OF" in sql]
        assert slow                                     # its piece ran on the last permit
    finally:
        _release(held)
    assert _drained()
    assert permits.used == 0
    # No stop call was free to start, so the piece's own poll stopped its query.
    assert set(slow) <= set(lake.stopped)


def test_splits_started_together_never_hold_more_permits_than_the_bound(monkeypatch):
    permits = _bound(monkeypatch, 3)
    athenas = [_Blocking(_lake(), "get_work_group", 30.0) for _ in range(8)]
    try:
        outcomes = _together([lambda a=a: _split(a, a.inner.glue, limit=_deadline(2.0))
                              for a in athenas])
        assert all(kind == "error" and isinstance(exc, sp.ExactSplitRefused)
                   and exc.reason == "wall_time" for kind, exc in outcomes)
        assert permits.peak <= 3
        assert 1 <= sum(a.calls for a in athenas) <= 3         # in flight at once, never ended
        assert len(sp._still_running()) <= 3
        waited = [exc for _kind, exc in outcomes if _NO_SLOT in exc.detail]
        assert len(waited) >= len(athenas) - 3
    finally:
        _release(athenas)
    assert _drained()
    assert permits.used == 0


# --------------------------------------------------------------------------- #
# Unrelated answers are not lost
# --------------------------------------------------------------------------- #
def test_a_fresh_split_with_time_to_spare_computes_while_other_splits_are_stalled():
    # Five stalled splits leave ten calls running (on the commit before, ten
    # refused every later split in the process). A split on another client
    # with permits free still computes its exact answer.
    held = _stalled_pieces(5, 1.5)
    try:
        assert len(sp._still_running()) == 10
        fresh = _lake()
        result = _split(fresh, fresh.glue, limit=_deadline(10.0))
        assert result["exact_split"]["applied"] is True
        assert result["rows"] == fresh.reference(base._P1)
    finally:
        _release(held)
    assert _drained()


@pytest.mark.parametrize("total, held", [(3, 2), (1, 0)])
def test_a_split_with_one_permit_free_computes_without_waiting_on_itself(monkeypatch, total, held):
    # A grouped query planned over the piece cap: a probe, then re-planned
    # pieces. With one permit free they run one at a time; the split never
    # holds a permit while it waits for one.
    permits = _bound(monkeypatch, total)
    for _ in range(held):
        assert permits.take(0)
    try:
        lake = _grouped_lake()
        started = time.monotonic()
        result = _split(lake, lake.glue, sql=retry._GROUPED, limit=_deadline(20.0))
        assert time.monotonic() - started < 10.0
    finally:
        for _ in range(held):
            permits.give()
    assert result["rows"] == lake.reference(retry._GROUPED)
    assert result["exact_split"]["pieces"] > 1
    assert permits.peak <= total and permits.used == 0


def test_a_split_waits_for_a_permit_that_frees_in_time_and_then_computes(monkeypatch):
    permits = _bound(monkeypatch, 2)
    assert permits.take(0) and permits.take(0)
    timer = threading.Timer(0.8, permits.give)
    timer.start()
    try:
        lake = _lake()
        started = time.monotonic()
        result = _split(lake, lake.glue, limit=_deadline(6.0))
        took = time.monotonic() - started
    finally:
        timer.join()
        permits.give()
    assert result["exact_split"]["applied"] is True
    assert result["rows"] == lake.reference(base._P1)
    assert took >= 0.75
    assert permits.used == 0


def test_a_split_whose_permit_never_frees_refuses_at_its_deadline_with_the_reserve_intact(
        monkeypatch):
    monkeypatch.delenv(_RESERVE_ENV, raising=False)
    permits = _bound(monkeypatch, 2)
    assert permits.take(0) and permits.take(0)
    try:
        lake = _lake()
        split_seconds = 2.5
        left = 15.0 + sp.DEADLINE_OVERRUN_SECONDS + split_seconds
        guarded, state = cb._lake_tool(monkeypatch, lake, {}, left=left)
        # The split's clock stands at the instant the question has ``left``
        # seconds until the split reads it, so a slow plain query before the
        # split cannot leave it under a piece's time (``not enough time``).
        clock = split_clock(monkeypatch, state, left)
        started = clock.now()
        payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=cb._Ctx(state)))
        took = clock.now() - started
    finally:
        permits.give()
        permits.give()
    assert payload["error_type"] == "scan_budget_exceeded", payload
    split = payload["exact_split"]
    assert split["reason"] == "wall_time" and _NO_SLOT in split["detail"]
    assert "Give no total" in split["detail"] and "narrower query" in split["detail"]
    assert "rows" not in payload
    # Waited to its own deadline, and ended within the overrun: the reserve is kept.
    assert split_seconds - 1e-6 <= took <= split_seconds + sp.DEADLINE_OVERRUN_SECONDS
    assert _metadata(lake) == [] and _pieces(lake) == []


def test_the_refusal_for_no_free_permit_says_no_total_and_is_not_transient():
    from agents.chat_client import classify_error_text
    from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
    for reserve in (0.0, 15.0):
        text = sp._no_slot(reserve).to_dict()["detail"]
        low = text.lower()
        assert _NO_SLOT in text and "Give no total" in text
        assert not any(m in low for m in _TRANSIENT_TOOL_ERROR_MARKERS + _AUTH_TOOL_ERROR_MARKERS)
        assert classify_error_text(text) != "timeout"
        assert "\u2014" not in text and "\u2013" not in text
    assert "narrower query" in sp._no_slot(15.0).to_dict()["detail"]
    assert sp._no_slot(15.0).reason == "wall_time"
