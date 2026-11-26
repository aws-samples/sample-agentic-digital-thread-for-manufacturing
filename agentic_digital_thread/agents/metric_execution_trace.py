# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Request-scoped execution trace for the metric resolver (ADR-0080 D2/D4).

A dry-run preview of a PENDING metric definition runs the SAME resolver a
live card uses. Two things must differ, and only while a preview is in
flight: the Athena executions are ATTRIBUTED with a suffix (``:preview``) so
steward traffic is separable from user traffic, and the value executions are
COUNTED so the preview can report a MEASURED cost (ADR-0053: query count and
engine-reported bytes scanned — never estimated, never dollars).

The trace rides a ``ContextVar``: it is invisible to every other caller (no
active trace ⇒ the resolver's execution path is byte-identical), and a
preview running in an offload worker thread cannot leak into a concurrent
dashboard resolve on another thread.

Stdlib-only and import-pure; it records numbers the engine reported and
nothing else.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

#: The ONE attribution suffix for steward dry-run previews (ADR-0080 D4).
PREVIEW_SUFFIX = ":preview"

#: The attribution suffix for ADR-0078 per-turn defined-metric resolves (chat
#: turns), separable from live dashboard traffic and steward previews.
TURN_SUFFIX = ":metric-turn"


@dataclass
class _SystemTally:
    query_count: int = 0
    reported: int = 0
    scanned_bytes: int = 0


@dataclass
class ExecutionTrace:
    """Per-request tally of the resolver's VALUE executions, by system."""

    suffix: str
    _systems: dict[str, _SystemTally] = field(default_factory=dict)

    def attribution(self, system_id: str) -> str:
        """The ``source_system`` label an attributed execution carries."""
        return f"{system_id}{self.suffix}"

    def record(self, system_id: str, scanned_bytes: object = None) -> None:
        """Count one value execution; keep bytes only when the engine reported them."""
        tally = self._systems.setdefault(system_id, _SystemTally())
        tally.query_count += 1
        if isinstance(scanned_bytes, bool) or not isinstance(scanned_bytes, (int, float)):
            return
        tally.reported += 1
        tally.scanned_bytes += int(scanned_bytes)

    def cost(self) -> dict:
        """Measured-only cost: counts always; ``scanned_bytes`` per system ONLY
        when every one of that system's executions reported it (a partial sum
        would understate, so it is absent instead)."""
        systems = []
        for system_id in sorted(self._systems):
            tally = self._systems[system_id]
            entry: dict = {"system_id": system_id, "query_count": tally.query_count}
            if tally.query_count and tally.reported == tally.query_count:
                entry["scanned_bytes"] = tally.scanned_bytes
            systems.append(entry)
        total = sum(t.query_count for t in self._systems.values())
        return {"query_count": total, "systems": systems}


_ACTIVE: ContextVar[ExecutionTrace | None] = ContextVar(
    "adt_metric_execution_trace", default=None)


def active_trace() -> ExecutionTrace | None:
    """The trace of the current request context, or ``None`` (the live path)."""
    return _ACTIVE.get()


@contextmanager
def execution_trace(suffix: str = PREVIEW_SUFFIX) -> Iterator[ExecutionTrace]:
    """Activate a fresh trace for the enclosed resolves; always restored."""
    trace = ExecutionTrace(suffix=suffix)
    token = _ACTIVE.set(trace)
    try:
        yield trace
    finally:
        _ACTIVE.reset(token)
