# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How long one model tool call's Athena queries may run, and how long the
engine made them wait.

The system agent's question budget (``system_agent._ToolCallBudget``) is
checked before each tool call. Measured in an answer eval, that let ONE
Athena call run 146-189 s inside a 60 s question (an exact split's piece
queued 110 s; a plain query sat 145 s in the engine, its poll window ran out
and it was retried), and the next call was cancelled. Two context-local
parts, both inert outside a call that sets them:

* :func:`call_deadline`: the absolute ``time.monotonic()`` deadline of the
  current call, the question's remaining budget at the call's start. Inside
  it every poll window is capped by it (:func:`capped_poll_window`, never
  below :data:`MIN_POLL_SECONDS`), and the connector's one-shot retry of a
  stalled poll runs only when :func:`retry_fits`. It is not a hard wall: a
  call can overrun it by about one poll interval and a few API calls (a poll
  keeps at least :data:`MIN_POLL_SECONDS`, a boto3 call or result page in
  flight is not interrupted, and after a stop the statistics are read up to
  :data:`TERMINAL_READS` times).
* :func:`recording`: the engine waiting of the queries run inside it, as
  spans on the monotonic clock (one :class:`PollWatch` per poll): the time
  each query reports the service held it outside its own planning and
  engine execution (:data:`WAIT_STATISTICS`: queue, service pre-processing,
  service processing), read again after a stop because Athena reports it
  only then, and the span our own polls saw the query QUEUED (for a query
  stopped while still QUEUED, at least that span). Never time RUNNING as
  such, and never a poll window (a retried attempt credits only what its
  own poll recorded). :meth:`EngineWait.seconds` is the length of their
  union, so queries waiting at the same time count once.
* :func:`reconcile`: Athena's cancellation is asynchronous, so the reads
  right after a stop can still see it RUNNING with the service time not yet
  reported. Such a stop is kept (:meth:`EngineWait.defer`), and the
  question's budget reads it again before its next check, polling until the
  state is terminal within the time it is given (the caller's window, and at
  most :data:`RECONCILE_MAX_SECONDS`), then credits it through the same
  union. All kept stops are read at once, each in its own daemon thread
  (one read in flight per stop), under that one deadline; a read is
  abandoned at the deadline or at the end of its stop's own time left (a
  late answer is ignored), and no read starts once either is spent, so a
  read that hangs never keeps another stop from being read. A stop is
  charged only the time its own reads took here (none when none ran), and
  is dropped, crediting nothing more, once it has been reconciled for
  :data:`RECONCILE_MAX_SECONDS` in all; a stop not read in time is kept
  for the next check.

Context variables follow the call into ``asyncio.to_thread`` and into the
exact split's piece threads (``athena_exact_split`` submits each piece with a
copy of the context).
"""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

#: The shortest poll window a call deadline leaves a query: at least one poll.
MIN_POLL_SECONDS = 1.0
#: A stalled poll is retried only with at least this long left before the
#: call deadline (the exact split's piece retry uses the same bound).
RETRY_MIN_SECONDS = 5.0

#: The deferred reads of stopped queries (:func:`reconcile`): at most this
#: long per budget check and per stop in all, this far apart, for at most
#: this many stops; no read starts with less than the last of these left.
RECONCILE_MAX_SECONDS = 5.0
RECONCILE_POLL_SECONDS = 0.5
MAX_PENDING_STOPS = 8
RECONCILE_MIN_READ_SECONDS = 0.05

_DEADLINE: ContextVar["float | None"] = ContextVar("athena_call_deadline", default=None)
_WAIT: ContextVar["EngineWait | None"] = ContextVar("athena_engine_wait", default=None)


def now() -> float:
    """The clock every deadline and span here is on."""
    return time.monotonic()


def pause(seconds: float) -> None:
    """The wait between two deferred reads (:func:`reconcile`)."""
    time.sleep(seconds)  # bounded by RECONCILE_MAX_SECONDS  # nosemgrep: arbitrary-sleep


@contextmanager
def call_deadline(deadline: "float | None"):
    """Inside: the call's queries are bounded by ``deadline`` (absolute
    :func:`now`); ``None`` sets no deadline. A call can still overrun it by
    about one poll interval and a few API calls (see the module doc)."""
    token = _DEADLINE.set(None if deadline is None else float(deadline))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def current_deadline() -> "float | None":
    return _DEADLINE.get()


def _left() -> "float | None":
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - now()


def capped_poll_window(timeout: float) -> float:
    """``timeout``, capped by the time left before the call deadline (at least
    :data:`MIN_POLL_SECONDS`, never above ``timeout``)."""
    left = _left()
    return timeout if left is None else min(timeout, max(MIN_POLL_SECONDS, left))


def retry_fits() -> bool:
    """Whether a fresh attempt of a stalled query fits before the call deadline."""
    left = _left()
    return left is None or left >= RETRY_MIN_SECONDS


class EngineWait:
    """Spans of time a call's queries waited in the engine, and the stopped
    queries whose statistics were not final yet (thread-safe)."""

    def __init__(self):
        self._spans: list[tuple[float, float]] = []
        self._pending: list = []
        self._lock = threading.Lock()

    def defer(self, watch: "PollWatch", read) -> None:
        """Keep a stopped query for :func:`reconcile`: ``read()`` returns
        ``(terminal, statistics)``; ``watch`` credits them. At most
        :data:`MAX_PENDING_STOPS` are kept."""
        self.keep([_Pending(watch, read)])

    def keep(self, items: list) -> None:
        """Keep these :class:`_Pending` stops (again), up to the cap."""
        with self._lock:
            room = max(0, MAX_PENDING_STOPS - len(self._pending))
            self._pending.extend(items[:room])

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def take_pending(self) -> list:
        """The kept stops, removed (:func:`reconcile` keeps again the ones it
        does not settle or drop), so two checks never read one at once."""
        with self._lock:
            pending, self._pending = self._pending, []
        return pending

    def add(self, start: float, end: float) -> None:
        if not (_finite(start) and _finite(end)) or end <= start:
            return
        with self._lock:
            self._spans.append((float(start), float(end)))

    def seconds(self) -> float:
        """The length of the union of the spans."""
        with self._lock:
            spans = sorted(self._spans)
        total, low, high = 0.0, None, None
        for start, end in spans:
            if high is None or start > high:
                total += 0.0 if high is None else high - low
                low, high = start, end
            else:
                high = max(high, end)
        return total + (0.0 if high is None else high - low)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@contextmanager
def recording(wait: "EngineWait | None" = None):
    """Collect the engine waiting of the queries run inside into ``wait`` (a
    new record when ``None``, else one shared by several calls: a span they
    waited through together counts once); yields the record."""
    wait = wait if wait is not None else EngineWait()
    token = _WAIT.set(wait)
    try:
        yield wait
    finally:
        _WAIT.reset(token)


def reported_seconds(statistics, key: str) -> "float | None":
    """The time a query's statistics report under ``key`` (milliseconds), in
    seconds, or ``None`` when they report none (missing, not a number,
    negative)."""
    value = statistics.get(key) if isinstance(statistics, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0:
        return None
    return float(value) / 1000.0


#: The Athena statistics that measure the service holding a query outside its
#: own planning and engine execution: its time in the queue, the service's
#: pre-processing, and the service's processing. Measured on a live query
#: stopped after its 150 s poll window with 0 bytes scanned: 266 ms queued,
#: 3,344 ms planning, 5,219 ms engine, and 144,796 ms of service processing,
#: reported only after the stop. For a query that finishes on its own, service
#: processing is the time to publish its results, normally under a second.
#: Planning and engine execution are the query's own work and never count.
WAIT_STATISTICS = ("QueryQueueTimeInMillis", "ServicePreProcessingTimeInMillis",
                   "ServiceProcessingTimeInMillis")
#: After a stop the statistics are read at most this many times, this far
#: apart, until the state is terminal: up to two more API calls, inside the
#: overrun the call deadline already allows (see the module doc). A stop
#: still not terminal then is read again later (:func:`reconcile`).
TERMINAL_READS = 2
TERMINAL_READ_PAUSE_SECONDS = 0.25


class PollWatch:
    """What one poll of one query saw of the engine making it wait, recorded
    into the current :func:`recording` when the poll ends (:meth:`end`).
    Outside a recording it is inert and reads no clock."""

    def __init__(self):
        self._wait = _WAIT.get()
        self._began = now() if self._wait is not None else None
        self._queued: "tuple[float, float] | None" = None   # first and last QUEUED poll
        self._waiting = ""                                  # the last non-terminal state seen
        self._stopped_at: "float | None" = None
        self._end_state = ""

    @property
    def recording(self) -> bool:
        return self._wait is not None

    def saw(self, state: str) -> None:
        """One poll read the query in ``state``."""
        if self._wait is None:
            return
        if state == "QUEUED":
            at = now()
            self._queued = (self._queued[0] if self._queued else at, at)
        if state in ("QUEUED", "RUNNING"):
            self._waiting = state

    def stopped(self) -> None:
        """The poll's window ran out and the query was just stopped."""
        if self._wait is not None:
            self._stopped_at = now()

    def defer(self, read) -> None:
        """This stopped query's statistics were not final: keep it for
        :func:`reconcile` (``read()`` returns ``(terminal, statistics)``)."""
        if self._wait is not None:
            self._wait.defer(self, read)

    def end(self, statistics, state: str, terminal=None) -> None:
        """The poll ended with the query in ``state`` and ``statistics`` the
        last it read; ``terminal`` the statistics read after a stop, which
        replace those (never added to them; ``None`` when none could be read).

        The waiting credited is the reported :data:`WAIT_STATISTICS`, never
        past the stop (or now) and never longer than the poll, joined with
        the span our polls saw the query QUEUED. Still QUEUED when stopped
        (its window ran out, or it ended CANCELLED straight from the queue,
        so up to its last QUEUED poll): the larger of the two, as one span
        ending there. Time RUNNING is never credited as such, and neither is
        a poll window."""
        if self._wait is None:
            return
        self._end_state = state
        self._credit(terminal if isinstance(terminal, dict) else statistics)

    def settle(self, statistics) -> None:
        """The final statistics of this stopped query, read later
        (:func:`reconcile`): credited as :meth:`end` would have, through the
        same union, so nothing it already credited counts twice."""
        if self._wait is not None and isinstance(statistics, dict):
            self._credit(statistics)

    def _credit(self, reported) -> None:
        waited = sum(reported_seconds(reported, key) or 0.0 for key in WAIT_STATISTICS)
        end = self._stopped_at if self._stopped_at is not None else now()
        if self._queued is not None and self._waiting == "QUEUED" and (
                self._stopped_at is not None or self._end_state == "CANCELLED"):
            end = end if self._stopped_at is not None else self._queued[1]
            length = min(max(end - self._queued[0], waited), end - self._began)
            self._wait.add(end - length, end)
            return
        self._wait.add(self._began, min(end, self._began + waited))
        if self._queued is not None:
            self._wait.add(*self._queued)


class _Pending:
    """One kept stop: its watch, its read, the seconds it has been reconciled
    for (:data:`RECONCILE_MAX_SECONDS` in all), the done flag of a read still
    in flight (a read is abandoned at its bound but its thread runs on; no
    second read starts until it ends), and the event of the reconcile waiting
    on it, set when a read ends."""

    __slots__ = ("watch", "read", "spent", "inflight", "wake")

    def __init__(self, watch, read):
        self.watch, self.read, self.spent, self.inflight, self.wake = watch, read, 0.0, None, None

    def own_left(self) -> float:
        """What is left of this stop's own :data:`RECONCILE_MAX_SECONDS`."""
        return RECONCILE_MAX_SECONDS - self.spent

    def reading(self) -> bool:
        return self.inflight is not None and not self.inflight.is_set()


def _start_read(item: _Pending) -> dict:
    """Start ``item.read()`` in a daemon thread. The returned box gets
    ``value`` (its ``(terminal, statistics)``, absent when the read raised)
    and ``at`` (:func:`now` when it ended); its end also sets ``item.wake``."""
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = item.read()
        except Exception:  # noqa: BLE001 - an unreadable stop is read again later
            pass
        finally:
            box["at"] = now()
            done.set()
            wake = item.wake
            if wake is not None:
                wake.set()

    item.inflight = done
    threading.Thread(target=run, name="athena-stop-read", daemon=True).start()
    return box


class _Reading:
    """One kept stop during one :func:`reconcile`. Its time there runs from
    its first read (or from the start, when a read from an earlier check is
    still in flight) to its end: settled, or no further read of it fits. It
    ends at the latest at ``min(deadline, start + its own time left)``; that
    span is what it is charged."""

    def __init__(self, item: _Pending, began: float, deadline: float):
        self.item, self.deadline = item, deadline
        self.box: "dict | None" = None     # the read this reconcile started
        self.next_at = began               # the next read starts no earlier
        self.start: "float | None" = began if item.reading() else None
        self.until: "float | None" = None  # where its time ends (None when no read ran)
        self.settled = self.done = False

    @property
    def ended(self) -> bool:
        return self.done

    def end_by(self, at: "float | None" = None) -> float:
        """The latest its time here can end (from ``at`` when not started)."""
        start = self.start if self.start is not None else at
        return min(self.deadline, start + self.item.own_left())

    def _end(self, at: float) -> None:
        self.done = True
        self.until = at if self.start is not None else None

    def step(self, t: float) -> None:
        """Take the answer of its read, or start its next read when due."""
        item = self.item
        if self.box is not None:
            if "at" not in self.box:              # still reading
                if t >= self.end_by():
                    self._end(self.end_by())      # abandoned; a late answer is ignored
                return
            box, self.box = self.box, None
            value = box.get("value")
            if box["at"] > self.end_by():
                self._end(self.end_by())
                return
            if isinstance(value, tuple) and len(value) == 2 and value[0]:
                item.watch.settle(value[1])
                self.settled = True
                self._end(box["at"])
                return
            self.next_at = box["at"] + RECONCILE_POLL_SECONDS
        elif item.reading():                       # an earlier check's read, still in flight
            if t >= self.end_by():
                self._end(self.end_by())
            return
        end_by = self.end_by(max(t, self.next_at))
        if max(t, self.next_at) > end_by - RECONCILE_MIN_READ_SECONDS:
            self._end(min(max(t, self.next_at), end_by))   # no further read fits
            return
        if t >= self.next_at:
            if self.start is None:
                self.start = t
            self.box = _start_read(item)

    def wake_at(self) -> float:
        return self.end_by() if self.box is not None or self.item.reading() else self.next_at

    def charge(self, at: float) -> None:
        """Add its time here to the stop's total (``at`` when not ended)."""
        if self.start is not None:
            until = self.until if self.until is not None else min(at, self.end_by())
            self.item.spent += max(0.0, until - self.start)


def reconcile(wait: "EngineWait | None", seconds: float) -> None:
    """Read the stops ``wait`` kept (:meth:`EngineWait.defer`) until each is
    terminal, all at once under one deadline (``seconds``, at most
    :data:`RECONCILE_MAX_SECONDS`), each stop's reads
    :data:`RECONCILE_POLL_SECONDS` apart, and credit each one that answers
    terminal in time (``PollWatch.settle``). Each stop has at most one read in
    flight, in its own daemon thread, waited on at most to the deadline and
    to the end of its own time left; a late answer is ignored, and no read
    starts with less than :data:`RECONCILE_MIN_READ_SECONDS` of either left,
    so this returns within the allowance plus thread starts. A stop is
    charged only its own reading time here (none when no read of it ran);
    the stops not settled are kept for the next check, except one with less
    than :data:`RECONCILE_MIN_READ_SECONDS` of its own
    :data:`RECONCILE_MAX_SECONDS` left: it is dropped and credits nothing
    more. Never raises."""
    pending = wait.take_pending() if wait is not None else []
    if not pending:
        return
    began = now()
    deadline = began + max(0.0, min(float(seconds), RECONCILE_MAX_SECONDS))
    changed = threading.Event()
    for item in pending:
        item.wake = changed
    readings = [_Reading(item, began, deadline) for item in pending]
    try:
        while True:
            changed.clear()
            t = now()
            for reading in readings:
                if not reading.ended:
                    reading.step(t)
            live = [reading for reading in readings if not reading.ended]
            if not live:
                break
            delay = min(reading.wake_at() for reading in live) - now()
            if any(reading.box is not None or reading.item.reading() for reading in live):
                changed.wait(max(0.0, delay))
            elif delay > 0:
                pause(delay)
    finally:
        at = now()
        kept = []
        for reading in readings:
            reading.item.wake = None
            reading.charge(at)
            if not reading.settled and reading.item.own_left() >= RECONCILE_MIN_READ_SECONDS:
                kept.append(reading.item)
        wait.keep(kept)
