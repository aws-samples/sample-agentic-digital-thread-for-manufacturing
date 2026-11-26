# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A query stopped while still QUEUED credits the time our polls saw it queued.

Measured in an answer eval: an exact split's piece sat 110 s in Athena's
queue. With each tool call now stopped at the question's deadline, such a
query is stopped while still QUEUED, and Athena reports no
``QueryQueueTimeInMillis`` for it, so the queueing gave the question's budget
nothing back. The poll now records the span its own polls saw the query
QUEUED (from the first QUEUED poll to the stop) when it is stopped while
QUEUED with no statistic: its window ran out (the call deadline or the poll
window), or it ended CANCELLED straight from the queue (the split stopped
it). A reported statistic is used alone; a query stopped while RUNNING adds
nothing beyond it. Spans go through the one ``EngineWait`` union, so the
stopped attempt of a retry is not counted twice.

Fully offline: a scripted Athena double and one test clock; synthetic names.

After a stop, the poll reads the query's statistics once more (at most two
reads): Athena reports the time it held a stopped query (queue, service
pre-processing and service processing, ``WAIT_STATISTICS``) only then. Those
statistics replace the in-flight ones; planning and engine execution are
never credited; a stop whose statistics cannot be read credits what the
in-flight statistics and the observed QUEUED span gave before.
"""
from __future__ import annotations

import types

import pytest

import agents.athena_call_time as act
import agents.athena_tools as at


class _Scripted:
    """An Athena double: each poll moves the clock ``step`` seconds and reads
    the next state (the last one repeats), with ``statistics`` reported."""

    def __init__(self, clock, states, *, step=10.0, statistics=None):
        self.clock, self.states, self.step = clock, list(states), step
        self.statistics = {"DataScannedInBytes": 0, **(statistics or {})}
        self.stopped: list[str] = []

    def get_query_execution(self, QueryExecutionId):
        self.clock["t"] += self.step
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"QueryExecution": {"Status": {"State": state}, "Statistics": dict(self.statistics)}}

    def stop_query_execution(self, QueryExecutionId=None, **_kwargs):
        self.stopped.append(QueryExecutionId)


@pytest.fixture
def clock(monkeypatch):
    """One clock for the poll's own deadline and the recorded spans."""
    now = {"t": 1000.0}
    monkeypatch.setattr(act, "now", lambda: now["t"])
    monkeypatch.setattr(at, "time", types.SimpleNamespace(monotonic=lambda: now["t"],
                                                          sleep=lambda _s: None))
    return now


def _poll(fake, timeout=30.0):
    """The poll's outcome: its terminal state, or ``"stopped"`` when its window ran out."""
    try:
        return at._poll_athena_query(fake, "q1", timeout, None)[0]
    except at.AthenaQueryTimeoutError:
        return "stopped"


def _credited(clock, states, **kwargs) -> tuple[str, float]:
    clock["t"] = 1000.0
    with act.recording() as waited:
        outcome = _poll(_Scripted(clock, states, **kwargs))
    return outcome, waited.seconds()


def test_a_query_stopped_while_queued_credits_what_our_polls_saw(clock, monkeypatch):
    # Polls at 1010, 1020, 1030 saw QUEUED; the window ended at 1030 and the
    # query was stopped: queued from the first QUEUED poll to the stop.
    assert _credited(clock, ["QUEUED"]) == ("stopped", pytest.approx(20.0))
    # Stopped while RUNNING (it left the queue): nothing beyond the statistic.
    assert _credited(clock, ["QUEUED", "RUNNING"]) == ("stopped", 0.0)
    # A query that finished with no statistic still waited while our polls
    # saw it QUEUED (1010 to 1020): that span is credited, nothing more.
    assert _credited(clock, ["QUEUED", "QUEUED", "SUCCEEDED"]) == ("SUCCEEDED", pytest.approx(10.0))
    # Outside a recording the poll reads no clock for it.
    monkeypatch.setattr(act, "now", lambda: (_ for _ in ()).throw(AssertionError("clock read")))
    assert _poll(_Scripted(clock, ["QUEUED"])) == "stopped"


def test_a_query_cancelled_straight_from_the_queue_credits_its_queued_polls(clock):
    # Stopped by someone else (the split, at its deadline) between the QUEUED
    # poll at 1020 and the CANCELLED one at 1030: the queued polls' span.
    assert _credited(clock, ["QUEUED", "QUEUED", "CANCELLED"], step=10.0) == (
        "CANCELLED", pytest.approx(10.0))
    assert _credited(clock, ["QUEUED", "RUNNING", "CANCELLED"]) == ("CANCELLED", 0.0)


def _stopped_from(clock, start: float, statistics=None) -> float:
    """Polled at ``start`` + 20 and + 40 (QUEUED both times), its 40 s window
    then over: stopped at ``start`` + 40 while still QUEUED."""
    clock["t"] = start
    with act.recording() as waited:
        assert _poll(_Scripted(clock, ["QUEUED"], step=20.0, statistics=statistics),
                     timeout=40.0) == "stopped"
    return waited.seconds()


def test_a_query_stopped_while_queued_credits_at_least_what_our_polls_saw(clock):
    # QUEUED at t=200 and t=220, stopped at 220. The span the polls saw
    # (20 s) is a lower bound a reported value cannot shrink.
    def stopped_with(statistic) -> float:
        return _stopped_from(clock, 180.0, {"QueryQueueTimeInMillis": statistic})

    assert _stopped_from(clock, 180.0) == pytest.approx(20.0)
    assert stopped_with(0) == pytest.approx(20.0)
    assert stopped_with(5_000) == pytest.approx(20.0)
    assert stopped_with(30_000) == pytest.approx(30.0)
    # Never more than the time since the poll began (40 s), however large the report.
    assert stopped_with(500_000) == pytest.approx(40.0)
    # Not a report (negative, a bool): what the polls saw.
    assert stopped_with(-5_000) == pytest.approx(20.0)
    assert stopped_with(True) == pytest.approx(20.0)


def test_a_query_that_left_the_queue_credits_only_its_reported_wait(clock):
    # Time a query spent RUNNING is the engine's work, never
    # engine waiting. Only what Athena reports as waiting before the engine
    # starts counts: the queue (QueryQueueTimeInMillis) and the service's
    # pre-processing (ServicePreProcessingTimeInMillis).
    assert _credited(clock, ["RUNNING"]) == ("stopped", 0.0)
    assert _credited(clock, ["RUNNING"], statistics={
        "ServicePreProcessingTimeInMillis": 7_000}) == ("stopped", pytest.approx(7.0))
    assert _credited(clock, ["QUEUED", "RUNNING"], statistics={
        "QueryQueueTimeInMillis": 3_000, "ServicePreProcessingTimeInMillis": 7_000,
        "EngineExecutionTimeInMillis": 15_000}) == ("stopped", pytest.approx(10.0))
    assert _credited(clock, ["SUCCEEDED"], statistics={
        "ServicePreProcessingTimeInMillis": 4_000}) == ("SUCCEEDED", pytest.approx(4.0))
    # A report longer than the poll, or not a report, adds no more than it can.
    assert _credited(clock, ["RUNNING"], statistics={
        "ServicePreProcessingTimeInMillis": 900_000}) == ("stopped", pytest.approx(30.0))
    assert _credited(clock, ["RUNNING"], statistics={
        "ServicePreProcessingTimeInMillis": -1}) == ("stopped", 0.0)


class _Retrying:
    """An Athena double for the connector's one-shot retry: the first query
    stays RUNNING with ``first`` statistics until stopped, the retry succeeds.
    Each poll moves the clock 10 s."""

    def __init__(self, clock, first=None):
        self.clock, self.first = clock, dict(first or {})
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start_query_execution(self, QueryString, **_kwargs):
        self.started.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, QueryExecutionId):
        self.clock["t"] += 10.0
        first = QueryExecutionId == "q1"
        statistics = {"DataScannedInBytes": 10, **(self.first if first else {})}
        state = "RUNNING" if first and QueryExecutionId not in self.stopped else (
            "CANCELLED" if first else "SUCCEEDED")
        return {"QueryExecution": {"Status": {"State": state}, "Statistics": statistics}}

    def stop_query_execution(self, QueryExecutionId=None, **_kwargs):
        self.stopped.append(QueryExecutionId)

    def get_query_results(self, QueryExecutionId, **_kwargs):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "n", "Type": "bigint"}]},
                              "Rows": [{"Data": [{"VarCharValue": "n"}]},
                                       {"Data": [{"VarCharValue": "3"}]}]}}


def _retried(clock, first=None) -> float:
    fake = _Retrying(clock, first)
    clock["t"] = 1000.0
    with act.recording() as waited:
        result = at._run_once_retried("SELECT COUNT(*) AS n FROM table_x", {
            "workgroup": "wg", "database": "db", "region": "us-east-1",
            "poll_timeout_seconds": 30}, {"client": fake})
    assert result["rows"] == [["3"]] and fake.stopped == ["q1"] and len(fake.started) == 2
    return waited.seconds()


def test_a_running_attempt_stopped_and_retried_credits_nothing(clock):
    # The poll window of the stopped attempt is never credited as such: it
    # ran for 30 s, and the engine was working, not waiting.
    assert _retried(clock) == 0.0


def test_a_retried_attempt_credits_exactly_its_reported_pre_processing(clock):
    assert _retried(clock, {"ServicePreProcessingTimeInMillis": 7_000}) == pytest.approx(7.0)
    assert _retried(clock, {"QueryQueueTimeInMillis": 2_000,
                            "ServicePreProcessingTimeInMillis": 7_000}) == pytest.approx(9.0)


# --------------------------------------------------------------------------- #
# The statistics read once more after the stop
# --------------------------------------------------------------------------- #
# The terminal statistics of a live query that was stopped after its 150 s
# poll window (it scanned 0 bytes): Athena reported the time it held the
# query as ServiceProcessingTimeInMillis, after the cancel, and no in-flight
# poll had shown it. Planning and engine execution are the query's work.
_STOPPED_IN_SERVICE = {
    "EngineExecutionTimeInMillis": 5219, "DataScannedInBytes": 0,
    "TotalExecutionTimeInMillis": 150281, "QueryQueueTimeInMillis": 266,
    "QueryPlanningTimeInMillis": 3344, "ServiceProcessingTimeInMillis": 144796,
}


class _AfterStop:
    """An Athena double: RUNNING (with ``inflight`` statistics) for every poll,
    each moving the clock ``step`` seconds; once stopped, each read returns
    the next of ``after`` (``(state, statistics)``, the last repeating, or an
    exception to raise) without moving the clock."""

    def __init__(self, clock, *, step=10.0, inflight=None, after=(("CANCELLED", {}),),
                 first="RUNNING"):
        self.clock, self.step, self.first = clock, step, first
        self.inflight = {"DataScannedInBytes": 0, **(inflight or {})}
        self.after, self.stopped, self.reads_after_stop = list(after), [], 0

    def get_query_execution(self, QueryExecutionId):
        if not self.stopped:
            self.clock["t"] += self.step
            return {"QueryExecution": {"Status": {"State": self.first},
                                       "Statistics": dict(self.inflight)}}
        self.reads_after_stop += 1
        found = self.after.pop(0) if len(self.after) > 1 else self.after[0]
        if isinstance(found, Exception):
            raise found
        state, statistics = found
        return {"QueryExecution": {"Status": {"State": state}, "Statistics": dict(statistics)}}

    def stop_query_execution(self, QueryExecutionId=None, **_kwargs):
        self.stopped.append(QueryExecutionId)


def _stopped_credit(clock, fake, timeout) -> float:
    clock["t"] = 1000.0
    with act.recording() as waited:
        assert _poll(fake, timeout=timeout) == "stopped"
    return waited.seconds()


def test_the_credited_statistics_are_named_in_one_constant():
    assert act.WAIT_STATISTICS == ("QueryQueueTimeInMillis", "ServicePreProcessingTimeInMillis",
                                   "ServiceProcessingTimeInMillis")
    assert not {"QueryPlanningTimeInMillis", "EngineExecutionTimeInMillis",
                "TotalExecutionTimeInMillis"} & set(act.WAIT_STATISTICS)


def test_a_query_stopped_in_service_processing_credits_its_terminal_wait(clock):
    # Polled RUNNING for 160 s with no wait statistic in flight; after the
    # stop Athena reports 266 ms queued and 144,796 ms of service time:
    # 145.062 s, not the planning or engine time, within the 160 s polled.
    fake = _AfterStop(clock, after=[("CANCELLED", _STOPPED_IN_SERVICE)])
    assert _stopped_credit(clock, fake, 150.3) == pytest.approx(145.062)
    assert fake.reads_after_stop == 1
    # Bounded by the time polled: a 60 s window credits 60 s at most.
    fake = _AfterStop(clock, after=[("CANCELLED", _STOPPED_IN_SERVICE)])
    assert _stopped_credit(clock, fake, 60.0) == pytest.approx(60.0)


def test_the_read_after_the_stop_is_bounded_to_two_reads(clock):
    # Not yet terminal on the first read: read once more, then use it.
    fake = _AfterStop(clock, after=[("RUNNING", {}), ("CANCELLED", _STOPPED_IN_SERVICE)])
    assert _stopped_credit(clock, fake, 150.3) == pytest.approx(145.062)
    assert fake.reads_after_stop == 2
    # Never terminal: two reads, and the newest statistics they gave.
    fake = _AfterStop(clock, after=[("RUNNING", {"ServiceProcessingTimeInMillis": 9_000})])
    assert _stopped_credit(clock, fake, 60.0) == pytest.approx(9.0)
    assert fake.reads_after_stop == 2


def test_a_stop_whose_statistics_cannot_be_read_credits_what_it_did_before(clock):
    lost = RuntimeError("throttled")
    # RUNNING with nothing reported in flight: nothing.
    assert _stopped_credit(clock, _AfterStop(clock, after=[lost]), 60.0) == 0.0
    # What the in-flight statistics reported, as before.
    fake = _AfterStop(clock, inflight={"ServicePreProcessingTimeInMillis": 7_000}, after=[lost])
    assert _stopped_credit(clock, fake, 60.0) == pytest.approx(7.0)
    # Still QUEUED: the span our polls saw (1010 to the stop at 1060).
    fake = _AfterStop(clock, first="QUEUED", after=[lost])
    assert _stopped_credit(clock, fake, 60.0) == pytest.approx(50.0)
    assert fake.reads_after_stop == 1


def test_the_terminal_statistics_replace_the_in_flight_ones_never_add_to_them(clock):
    # 5 s queued, in flight and again after the stop, plus 100 s of service
    # time after it: 105 s, not 110.
    fake = _AfterStop(clock, inflight={"QueryQueueTimeInMillis": 5_000}, after=[(
        "CANCELLED", {"QueryQueueTimeInMillis": 5_000, "ServiceProcessingTimeInMillis": 100_000})])
    assert _stopped_credit(clock, fake, 150.3) == pytest.approx(105.0)
    # Still QUEUED at the stop: the larger of what our polls saw (50 s) and
    # the reported wait, as one span ending at the stop.
    fake = _AfterStop(clock, first="QUEUED", after=[("CANCELLED", {"QueryQueueTimeInMillis": 0})])
    assert _stopped_credit(clock, fake, 60.0) == pytest.approx(50.0)


def test_a_query_that_ends_on_its_own_uses_its_terminal_poll(clock):
    # SUCCEEDED: the poll that saw it finish carries the final statistics,
    # so no read follows; its service time counts (publishing its results).
    clock["t"] = 1000.0
    fake = _Scripted(clock, ["RUNNING", "SUCCEEDED"], statistics={
        "QueryQueueTimeInMillis": 300, "ServiceProcessingTimeInMillis": 400,
        "QueryPlanningTimeInMillis": 2_000, "EngineExecutionTimeInMillis": 9_000})
    with act.recording() as waited:
        assert _poll(fake) == "SUCCEEDED"
    assert waited.seconds() == pytest.approx(0.7)
    assert fake.stopped == []


def test_outside_a_recording_nothing_is_read_after_the_stop(clock):
    fake = _AfterStop(clock, after=[("CANCELLED", _STOPPED_IN_SERVICE)])
    clock["t"] = 1000.0
    assert _poll(fake, timeout=60.0) == "stopped"
    assert fake.stopped == ["q1"] and fake.reads_after_stop == 0


# --------------------------------------------------------------------------- #
# A stop whose statistics were not final yet is settled later
# --------------------------------------------------------------------------- #
# Athena's cancellation is asynchronous: the two reads right after the stop can
# both still see RUNNING, with the service time not reported yet. Such a stop
# is kept (EngineWait.pending) and read again by reconcile(), which the
# question's budget runs before its next check: bounded polling until the
# state is terminal, crediting WAIT_STATISTICS through the same union.
@pytest.fixture
def paused(clock, monkeypatch):
    """reconcile()'s pauses move the test clock instead of sleeping."""
    pauses: list[float] = []

    def pause(seconds):
        pauses.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(act, "pause", pause, raising=False)
    return pauses


def _stopped(clock, fake, timeout=150.3):
    clock["t"] = 1000.0
    waited = act.EngineWait()
    with act.recording(waited):
        assert _poll(fake, timeout=timeout) == "stopped"
    return waited


def test_a_stop_still_running_after_its_two_reads_is_settled_later(clock, paused):
    fake = _AfterStop(clock, after=[("RUNNING", {}), ("RUNNING", {}),
                                    ("CANCELLED", _STOPPED_IN_SERVICE)])
    waited = _stopped(clock, fake)
    assert waited.seconds() == 0.0 and fake.reads_after_stop == 2
    assert waited.has_pending()
    act.reconcile(waited, 5.0)
    assert waited.seconds() == pytest.approx(145.062)
    assert fake.reads_after_stop == 3 and not waited.has_pending()
    # Settling again changes nothing: the same span, counted once.
    act.reconcile(waited, 5.0)
    assert waited.seconds() == pytest.approx(145.062) and fake.reads_after_stop == 3


def test_a_stop_that_never_becomes_terminal_credits_nothing_more(clock, paused):
    fake = _AfterStop(clock, after=[("RUNNING", {})])
    waited = _stopped(clock, fake)
    began = clock["t"]
    act.reconcile(waited, 30.0)
    assert waited.seconds() == 0.0 and not waited.has_pending()
    # At most RECONCILE_MAX_SECONDS of pauses, whatever the caller allows.
    assert sum(paused) <= act.RECONCILE_MAX_SECONDS
    assert clock["t"] - began <= act.RECONCILE_MAX_SECONDS
    assert fake.reads_after_stop - 2 <= act.RECONCILE_MAX_SECONDS / act.RECONCILE_POLL_SECONDS + 1


def test_reconcile_stays_inside_the_time_it_is_given(clock, paused):
    running = ("RUNNING", {})
    fake = _AfterStop(clock, after=[running] * 5 + [("CANCELLED", _STOPPED_IN_SERVICE)])
    waited = _stopped(clock, fake)
    act.reconcile(waited, 1.0)                # reads and pauses inside 1 s, then stops
    assert sum(paused) <= 1.0 and waited.seconds() == 0.0
    # Under 5 s of reconciling in all: kept for the next check, which settles it.
    assert waited.has_pending()
    act.reconcile(waited, 5.0)
    assert waited.seconds() == pytest.approx(145.062) and not waited.has_pending()
    # No time at all: nothing is read, and the stop stays kept.
    fake = _AfterStop(clock, after=[running])
    waited = _stopped(clock, fake)
    reads = fake.reads_after_stop
    act.reconcile(waited, 0.0)
    assert fake.reads_after_stop == reads and waited.has_pending()


def test_an_unreadable_stop_is_read_again_and_a_final_one_is_not_kept(clock, paused):
    lost = RuntimeError("throttled")
    fake = _AfterStop(clock, after=[lost, ("CANCELLED", _STOPPED_IN_SERVICE)])
    waited = _stopped(clock, fake)
    assert waited.seconds() == 0.0 and waited.has_pending()
    act.reconcile(waited, 5.0)
    assert waited.seconds() == pytest.approx(145.062)
    # Final on the first read after the stop: settled then, nothing kept.
    fake = _AfterStop(clock, after=[("CANCELLED", _STOPPED_IN_SERVICE)])
    assert not _stopped(clock, fake).has_pending()


def test_a_stop_still_queued_is_settled_as_one_span_ending_at_the_stop(clock, paused):
    # Seen QUEUED 1010..1060 and stopped at 1060; final later with 80 s queued:
    # one span [980, 1060] cut to the poll's start, i.e. 60 s, never more.
    fake = _AfterStop(clock, first="QUEUED", after=[("QUEUED", {}), ("QUEUED", {}),
                                                    ("CANCELLED", {"QueryQueueTimeInMillis": 80_000})])
    waited = _stopped(clock, fake, timeout=60.0)
    assert waited.seconds() == pytest.approx(50.0)
    act.reconcile(waited, 5.0)
    assert waited.seconds() == pytest.approx(60.0)


def test_outside_a_recording_no_stop_is_kept(clock, paused):
    fake = _AfterStop(clock, after=[("RUNNING", {})])
    clock["t"] = 1000.0
    assert _poll(fake, timeout=60.0) == "stopped"
    assert fake.reads_after_stop == 0
    act.reconcile(None, 5.0)                   # nothing to do, nothing read
    assert fake.reads_after_stop == 0 and paused == []


# --------------------------------------------------------------------------- #
# The deferred reads, on the real clock: each read is bounded, and none starts
# once the allowance is spent
# --------------------------------------------------------------------------- #
import threading  # noqa: E402
import time  # noqa: E402

_TERMINAL = (True, _STOPPED_IN_SERVICE)


def _kept(waited, read, *, polled=200.0):
    """One stopped query kept on ``waited``, polled for ``polled`` seconds
    before its stop (so its credit can be seen), read by ``read()``."""
    with act.recording(waited):
        watch = act.PollWatch()
        watch._began -= polled
        watch.stopped()
        watch.defer(read)


def _slow(seconds, result=(False, {})):
    def read():
        time.sleep(seconds)
        return result
    return read


def test_slow_reads_never_run_past_the_allowance():
    waited = act.EngineWait()
    for _ in range(8):
        _kept(waited, _slow(0.3))
    started = time.monotonic()
    act.reconcile(waited, 0.1)
    took = time.monotonic() - started
    assert took < 0.25, took                   # not 8 x 0.3 s
    # The read cut off and the seven never started are all kept.
    assert waited.pending_count() == 8 and waited.seconds() == 0.0


def test_a_hung_read_is_abandoned_at_the_bound_and_its_late_result_ignored():
    release = threading.Event()

    def hung():
        release.wait(10)
        return _TERMINAL

    waited = act.EngineWait()
    _kept(waited, hung)
    started = time.monotonic()
    act.reconcile(waited, 0.2)
    assert time.monotonic() - started < 0.35
    release.set()                              # the abandoned read now finishes
    time.sleep(0.05)
    assert waited.seconds() == 0.0 and waited.has_pending()
    # A later check reads it again (it now answers at once) and credits it then.
    act.reconcile(waited, 1.0)
    assert waited.seconds() == pytest.approx(145.062) and not waited.has_pending()


def test_a_read_that_finishes_in_time_is_credited_on_the_real_clock():
    waited = act.EngineWait()
    _kept(waited, _slow(0.05, _TERMINAL))
    act.reconcile(waited, 1.0)
    assert waited.seconds() == pytest.approx(145.062) and not waited.has_pending()


# --------------------------------------------------------------------------- #
# Kept stops are read side by side, each charged only its own time
# --------------------------------------------------------------------------- #
# All kept stops are read at once under the one caller allowance, so a read
# that hangs never keeps a later stop from being read. Each stop is charged
# only for the time this reconcile spent reading it, and its own 5 s cap
# bounds its own reads.
_OLD_STOP = (True, {"ServiceProcessingTimeInMillis": 10_000})


def _counted(read):
    calls = []

    def counted():
        calls.append(time.monotonic())
        return read()
    return counted, calls


def test_a_hung_stop_never_keeps_a_later_stop_from_being_read_and_credited():
    release = threading.Event()

    def hung():
        release.wait(10)
        return _OLD_STOP

    hung_read, hung_calls = _counted(hung)
    quick_read, quick_calls = _counted(lambda: _TERMINAL)
    waited = act.EngineWait()
    _kept(waited, hung_read, polled=1000.0)    # credited, it would add 10 s
    _kept(waited, quick_read)
    started = time.monotonic()
    act.reconcile(waited, 0.3)
    took = time.monotonic() - started
    release.set()
    # The second stop is read at once and credited with the r4 statistics.
    assert len(quick_calls) == 1 and len(hung_calls) == 1
    assert waited.seconds() == pytest.approx(145.062)
    # The hung stop is kept, charged only the 0.3 s its own read ran.
    [kept] = waited.take_pending()
    assert kept.read is hung_read
    assert 0.25 <= kept.spent <= 0.35, kept.spent
    assert took < 0.45, took


def test_a_first_read_hung_through_the_whole_cap_costs_the_second_nothing(monkeypatch):
    # A first read that hangs through the whole cap and a second that is
    # terminal at once, with the 5 s cap scaled to 0.3 s: the second is read
    # and credited; only the first, whose own read used its whole cap, is dropped.
    monkeypatch.setattr(act, "RECONCILE_MAX_SECONDS", 0.3)
    release = threading.Event()
    quick_read, quick_calls = _counted(lambda: _TERMINAL)
    waited = act.EngineWait()
    _kept(waited, lambda: (release.wait(10), _OLD_STOP)[1], polled=1000.0)
    _kept(waited, quick_read)
    act.reconcile(waited, 0.3)
    release.set()
    assert len(quick_calls) == 1
    assert waited.seconds() == pytest.approx(145.062)
    assert not waited.has_pending()


def test_an_old_stop_gets_only_its_own_time_left_and_a_new_one_is_unaffected():
    # The old stop has been reconciled for 4.9 s: its read (which would answer
    # terminal after 0.3 s) is cut at its last 0.1 s, nothing is credited for
    # it, and it is dropped. The new stop beside it is read and credited.
    waited = act.EngineWait()
    _kept(waited, _slow(0.3, _OLD_STOP), polled=1000.0)
    _kept(waited, _slow(0.2, _TERMINAL))
    old, new = waited.take_pending()
    old.spent = 4.9
    waited.keep([old, new])
    started = time.monotonic()
    act.reconcile(waited, 1.0)
    took = time.monotonic() - started
    assert waited.seconds() == pytest.approx(145.062)   # the new one only, not +10 s
    assert not waited.has_pending()                     # the old one is dropped
    assert took < 0.5, took


def test_a_stop_whose_read_never_started_is_not_charged():
    waited = act.EngineWait()
    _kept(waited, _slow(0.0, _TERMINAL))
    _kept(waited, _slow(0.0, _TERMINAL))
    act.reconcile(waited, 0.0)
    act.reconcile(waited, act.RECONCILE_MIN_READ_SECONDS / 2)
    kept = waited.take_pending()
    assert len(kept) == 2 and [item.spent for item in kept] == [0.0, 0.0]


def test_a_read_still_in_flight_from_an_earlier_check_is_followed_by_a_fresh_one():
    # The first read hangs past the first check; the next check starts no
    # second read while it is in flight, and reads again as soon as it ends
    # (its own answer belongs to the earlier check and is ignored).
    release = threading.Event()
    calls = []

    def read():
        calls.append(time.monotonic())
        if len(calls) == 1:
            release.wait(10)
            return False, {}
        return _TERMINAL

    waited = act.EngineWait()
    _kept(waited, read)
    act.reconcile(waited, 0.1)
    assert len(calls) == 1 and waited.has_pending()
    threading.Timer(0.1, release.set).start()
    started = time.monotonic()
    act.reconcile(waited, 1.0)
    took = time.monotonic() - started
    assert len(calls) == 2
    assert waited.seconds() == pytest.approx(145.062) and not waited.has_pending()
    assert took < 0.4, took


def test_a_stop_is_dropped_after_five_seconds_of_reconciling_in_all(clock, paused):
    fake = _AfterStop(clock, after=[("RUNNING", {})])
    waited = _stopped(clock, fake)
    for _ in range(4):
        act.reconcile(waited, 2.0)
    # 2 s + 2 s + 1 s: at the 5 s total it is dropped, and nothing is read after.
    assert not waited.has_pending() and waited.seconds() == 0.0
    assert sum(paused) <= act.RECONCILE_MAX_SECONDS
