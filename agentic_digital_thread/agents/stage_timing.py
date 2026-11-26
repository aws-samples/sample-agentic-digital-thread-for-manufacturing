# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-stage wall-clock timing for one background job (durations and counts only).

A job opens :func:`recording` once; code anywhere below it marks stages with
:func:`stage` and sizes with :func:`count` / :func:`incr`, and the I/O seams
carry :func:`timed` (graph reads and writes, source samples and probes, model
calls), without a timer threaded through any signature. Outside a recording
all of these are no-ops, so a function shared with another caller (the
onboarding agent's resolve tool, a unit test) runs exactly as before.

What is recorded is DURATIONS (seconds) and COUNTS (integers) under the fixed
names in :data:`KNOWN_STAGES` / :data:`KNOWN_COUNTS` — never a sampled value,
a field name, a prompt or a model answer — so the result can be logged and
returned on a job payload on a customer install without carrying customer data.

Stages accumulate: a name entered twice records the sum. A stage that raises
still records its time, and timing itself never raises into the job.
"""
from __future__ import annotations

import functools
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, TypeVar

F = TypeVar("F", bound=Callable[..., Any])
_current: ContextVar["StageTimer | None"] = ContextVar("adt_stage_timer", default=None)
#: True while a :func:`timed` seam runs in THIS context (per context, so two
#: threads sharing one timer never clear each other's flag).
_in_seam: ContextVar[bool] = ContextVar("adt_stage_timer_in_seam", default=False)

#: The ONLY names a timer reports (``as_dict`` / ``summary`` drop anything
#: else), so a payload can never carry a name that did not come from this
#: list. Every stage/count/incr/timed literal in ``agents/`` is pinned to it
#: by ``tests/unit/test_stage_timing.py``; the workshop seed keeps a literal
#: copy (it is vendored without the app), pinned equal by its own tests.
#: ``io.*`` categories are disjoint (see :func:`timed`), so they can be summed.
KNOWN_STAGES: tuple[str, ...] = (
    "concept_pass", "overlap_gate", "affinity_pass", "affinity.sample_new",
    "affinity.sample_peers", "affinity.match_and_probe", "join_discovery",
    "io.graph_read", "io.graph_write", "io.model", "io.source_sample", "io.source_probe",
)
KNOWN_COUNTS: tuple[str, ...] = (
    "rules", "affinity.new_fields", "affinity.peer_fields",
    "io.graph_read.calls", "io.graph_write.calls", "io.model.calls", "io.model.throttled",
    "io.model.retried", "io.source_sample.calls", "io.source_sample.memo_hits",
    "io.source_probe.calls", "timing.unknown_names",
)


class StageTimer:
    """Accumulated seconds per stage name plus integer counts, in first-seen order."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()  # a copied context may share this timer
        self.stages: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        # Registered on entry, so the report reads in start order (an outer
        # stage before the stages nested in it).
        with self._lock:
            self.stages.setdefault(name, 0.0)
        start = self._clock()
        try:
            yield
        finally:
            self.add(name, self._clock() - start)

    def add(self, name: str, seconds: float) -> None:
        with self._lock:
            self.stages[name] = self.stages.get(name, 0.0) + max(0.0, float(seconds))

    def count(self, name: str, value: object) -> None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        with self._lock:
            self.counts[name] = number

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + by

    def as_dict(self) -> dict:
        """``{"total_s", "stages_s": {name: seconds}, "counts": {name: n}}``,
        rounded to 10 ms; only :data:`KNOWN_STAGES` / :data:`KNOWN_COUNTS`
        names. A dropped name is never silent: ``timing.unknown_names`` counts
        them (a stage someone added without listing it)."""
        with self._lock:
            stages, counts = dict(self.stages), dict(self.counts)
        out = {
            "total_s": round(self._clock() - self._started, 2),
            "stages_s": {k: round(v, 2) for k, v in stages.items() if k in KNOWN_STAGES},
            "counts": {k: v for k, v in counts.items() if k in KNOWN_COUNTS},
        }
        dropped = sum(k not in KNOWN_STAGES for k in stages) + sum(
            k not in KNOWN_COUNTS for k in counts)
        if dropped:
            out["counts"]["timing.unknown_names"] = dropped
        return out

    def summary(self) -> str:
        """One log line: ``total=12.34s a=1.00s b=2.50s | n=3 m=4``."""
        data = self.as_dict()
        parts = [f"total={data['total_s']:.2f}s"]
        parts += [f"{k}={v:.2f}s" for k, v in data["stages_s"].items()]
        line = " ".join(parts)
        if data["counts"]:
            line += " | " + " ".join(f"{k}={v}" for k, v in data["counts"].items())
        return line


@contextmanager
def recording(clock: Callable[[], float] = time.perf_counter) -> Iterator[StageTimer]:
    """Bind a fresh :class:`StageTimer` for the duration of the block (this
    thread / context only) and yield it."""
    timer = StageTimer(clock)
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time the block under *name* on the bound timer; a no-op without one."""
    timer = _current.get()
    if timer is None:
        yield
        return
    with timer.stage(name):
        yield


def count(name: str, value: object) -> None:
    """Record an integer count on the bound timer; a no-op without one."""
    timer = _current.get()
    if timer is not None:
        timer.count(name, value)


def incr(name: str, by: int = 1) -> None:
    """Add *by* to a count on the bound timer; a no-op without one."""
    timer = _current.get()
    if timer is not None:
        timer.incr(name, by)


def timed(name: str) -> Callable[[F], F]:
    """Decorator: inside a recording, each call adds its wall time to stage
    *name* and 1 to the count ``<name>.calls``; outside one, the call runs
    untouched. Used on the I/O seams (graph, source sampling, model) so a job
    reports time by category without editing every caller.

    The OUTERMOST seam owns the time: a seam reached from inside another
    (SiteWise sampling reads the graph) runs untimed, so the ``io.*``
    categories never double-count and can be summed."""
    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            timer = _current.get()
            if timer is None or _in_seam.get():
                return fn(*args, **kwargs)
            timer.incr(f"{name}.calls")
            token = _in_seam.set(True)
            try:
                with timer.stage(name):
                    return fn(*args, **kwargs)
            finally:
                _in_seam.reset(token)
        return wrapper  # type: ignore[return-value]
    return decorate
