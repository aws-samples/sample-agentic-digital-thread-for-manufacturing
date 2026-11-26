# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admission coordinator for orchestrator-runtime turns — ownership, not a flag.

Concurrent ``InvokeAgentRuntime`` streams on the SAME orchestrator runtime cause
``IncompleteRead``, so the app has always throttled its BACKGROUND invocations
(the ops briefing, the prompt scheduler, the keep-warm ping) against whatever
else is in flight. Interactive /api/ask turns are NOT throttled — they mint fresh
``runtimeSessionId``s and are allowed to overlap; that concurrency is the existing
contract and this module preserves it.

What it replaces is the mechanism: one process-global ``bool`` written by every
owner. Three defects came out of sharing a single cell:

* **Clobbering.** A release was ``= False`` — anyone's admission, not the
  caller's. Two overlapping user turns, or a keep-warm ping landing on top of a
  live turn, cleared each other's state and let a briefing in on top.
* **Check-then-act.** The briefing guard read the flag in the request handler but
  the flag was SET by the response body generator, which Starlette starts only
  after the handler has returned. Two briefings arriving inside that window both
  passed the guard.
* **Leaks.** Nothing tied a set to a specific exit path, so an abandoned turn
  latched the flag ``True`` forever.

Ownership tokens fix all three: acquisition and the idle test happen together
under ONE lock, and a release names the exact token it is releasing.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


LEAK_THRESHOLD_SECONDS = 3600
_OWNER_KINDS = frozenset({"briefing", "query", "scheduler", "warmup"})


@dataclass(frozen=True)
class QueryToken:
    """One owner's admission. Compared by identity of ``id``, never by label."""

    id: int
    label: str


@dataclass(frozen=True)
class _HeldAdmission:
    label: str
    acquired_monotonic: float


def _owner_kind(label: str) -> str:
    """Bounded non-customer owner kind for diagnostics."""
    return label if label in _OWNER_KINDS else "other"


class QueryAdmission:
    """Lock-protected admission registry issuing idempotent ownership tokens."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._held: dict[int, _HeldAdmission] = {}
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._briefings_admitted: int | None = None
        self._briefings_skipped_busy: int | None = None
        self._last_release_at: datetime | None = None

    @property
    def busy(self) -> bool:
        """Is ANY owner currently admitted?"""
        with self._lock:
            return bool(self._held)

    def owners(self) -> tuple[str, ...]:
        """Labels of the current owners, oldest first (diagnostics/tests)."""
        with self._lock:
            return tuple(held.label for held in self._held.values())

    def acquire(self, label: str) -> QueryToken:
        """Admit *label* unconditionally — the interactive-turn path.

        User turns keep their existing allowed concurrency; they claim so the
        serialized background workloads can SEE them, not to exclude each other.
        """
        with self._lock:
            token = QueryToken(next(self._ids), label)
            self._held[token.id] = _HeldAdmission(label, self._monotonic())
            return token

    def acquire_if_idle(self, label: str) -> QueryToken | None:
        """Admit *label* ONLY if nothing else is admitted — atomically.

        The serialized background workloads (briefing, scheduler, keep-warm ping)
        use this: the test and the claim are one critical section, so there is no
        window in which two of them both decide they are the only one.
        """
        with self._lock:
            if self._held:
                if label == "briefing":
                    self._briefings_skipped_busy = (
                        self._briefings_skipped_busy or 0) + 1
                return None
            token = QueryToken(next(self._ids), label)
            self._held[token.id] = _HeldAdmission(label, self._monotonic())
            if label == "briefing":
                self._briefings_admitted = (self._briefings_admitted or 0) + 1
            return token

    def release(self, token: QueryToken | None) -> None:
        """Release EXACTLY *token* — idempotent, and never another owner's.

        ``None`` (a caller that was never admitted) is a no-op, so every exit
        path can release unconditionally.
        """
        if token is None:
            return
        with self._lock:
            if self._held.pop(token.id, None) is not None:
                self._last_release_at = self._utc_now()

    def snapshot(self) -> dict:
        """Current-replica measurements, computed under the coordinator lock.

        Reading never acquires, releases, or repairs a token. Counter and release
        fields are absent until a real event measured them; leak is an age signal,
        not a verdict or an auto-release instruction.
        """
        with self._lock:
            held = tuple(self._held.values())
            result = {
                "state": "busy" if held else "idle",
                "tokens_outstanding": len(held),
                "leak_signal": False,
                "leak_threshold_seconds": LEAK_THRESHOLD_SECONDS,
            }
            if held:
                age = max(0.0, self._monotonic() - held[0].acquired_monotonic)
                kinds = {_owner_kind(item.label) for item in held}
                result["current_owner_kind"] = next(iter(kinds)) if len(kinds) == 1 else "mixed"
                result["oldest_token_age_seconds"] = round(age, 3)
                result["leak_signal"] = age >= LEAK_THRESHOLD_SECONDS
            if self._briefings_admitted is not None:
                result["briefings_admitted"] = self._briefings_admitted
            if self._briefings_skipped_busy is not None:
                result["briefings_skipped_busy"] = self._briefings_skipped_busy
            if self._last_release_at is not None:
                result["last_release_at"] = self._last_release_at.isoformat()
            return result

    def reset(self) -> None:
        """Drop admissions and measurements — test-lane hygiene ONLY."""
        with self._lock:
            self._held.clear()
            self._briefings_admitted = None
            self._briefings_skipped_busy = None
            self._last_release_at = None


_admission = QueryAdmission()


def get_admission() -> QueryAdmission:
    """The process-wide admission coordinator."""
    return _admission
