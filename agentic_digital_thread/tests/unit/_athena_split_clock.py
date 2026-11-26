# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A clock for an exact split called through the system agent's Athena tool.

The tool runs the model's query first and starts the split only after the
engine refuses it for scan size, while the question's deadline was set before
that query. So the time a split starts with, measured from the test's setup,
shrinks by however long the plain query and the tool's checks took on the
machine, and a slow run can leave it under one piece's time
(``PIECE_SECONDS_FLOOR``), declining at its start for want of time.

:class:`SplitClock`, installed as the split module's ``time`` by
:func:`split_clock`, stands at the instant the question has ``left`` seconds
until the split first reads it, when it takes its deadline
(``_deadline_and_caps``), and from then on runs with the real clock. The split
starts with exactly the time the test gave it however slowly the machine ran
what came before, and still waits, polls and stops in real time. Synthetic
names only.
"""
from __future__ import annotations

import threading
import time

import agents.system_agent as sa
from agents import athena_exact_split as sp


class SplitClock:
    """The split's clock (``sp.time``): ``at`` until its first reading, then
    the real clock's progress since that reading added to ``at``."""

    def __init__(self, at: float):
        self._at, self._since = at, None
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            if self._since is None:
                self._since = time.monotonic()
        return self.now()

    def now(self) -> float:
        """The clock's reading, without starting it."""
        with self._lock:
            since = self._since
        return self._at if since is None else self._at + (time.monotonic() - since)


def split_clock(monkeypatch, state: dict, left: float) -> SplitClock:
    """A :class:`SplitClock` as the split's clock, standing at the instant the
    question of the invocation ``state`` has ``left`` seconds: the split starts
    with ``left`` less what its caller keeps back."""
    clock = SplitClock(sa._question_deadline(state) - left)
    monkeypatch.setattr(sp, "time", clock)
    return clock
