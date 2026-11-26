# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live story's loop geometry, app side (ADR-0091). Pure, stdlib only.

The app image ships only ``agents/`` (no ``erp_simulation``), so this mirrors
the loop math in ``erp_simulation.condition_monitor``. It lives apart from
``story_clock`` (the place route, which imports FastAPI and pydantic) so the
geometry can be imported and tested on its own.

``geometry_for(loop_seconds)`` computes every step boundary for one loop
length. ``STORY_LOOP_SECONDS`` is read ONCE, here, at import, with the SAME
validation as the simulator: unset or blank is the shipped 8 h loop (every
derived constant byte-identical), a bad value fails the import.
``story_clock`` binds its constants from ``GEOMETRY``. Parity with the
simulator, across every valid loop length, is pinned by
``tests/unit/test_story_clock_loop_parity.py``.
"""
from __future__ import annotations

import math
import os
from typing import NamedTuple, Optional

TELEMETRY_GRID_SECONDS = 300
INTERVAL_SECONDS = 5
LOOP_ENV = "STORY_LOOP_SECONDS"
DEFAULT_LOOP_SECONDS = 28800           # 8 h dataset loop
LOOP_SECONDS_RANGE = (3600, DEFAULT_LOOP_SECONDS)  # inclusive, a multiple of 300; capped at 8 h (ADR-0091)
FAIL_FRACTION = 0.9                    # the bearing stops 90 % into the loop
REPAIR_STEPS_CAP, REPAIR_LOOP_DIVISOR = 240, 24
# BPFO band (healthy, failed) mm/s and its alarm threshold (cm.FEATURES["bpfo"], cm.BPFO_ALARM_MM_S).
BPFO_HEALTHY, BPFO_FAILED, BPFO_ALARM_MM_S = 0.05, 1.20, 0.70
# The earliest alarm a placement may ask for, in minutes from now.
ALARM_IN_MINUTES_MIN = 5


def _checked(value: int) -> int:
    low, high = LOOP_SECONDS_RANGE
    if value % TELEMETRY_GRID_SECONDS or not low <= value <= high:
        raise ValueError(f"{LOOP_ENV} must be a multiple of {TELEMETRY_GRID_SECONDS} in "
                         f"[{low}, {high}]; got {value}")
    return value


def loop_seconds_from(raw: Optional[str]) -> int:
    """Mirror of ``condition_monitor.loop_seconds_from`` (ADR-0091): ``None`` or
    blank -> DEFAULT_LOOP_SECONDS; else a base-10 integer, a multiple of 300 in
    LOOP_SECONDS_RANGE (inclusive); anything else raises ValueError."""
    if raw is None or not raw.strip():
        return DEFAULT_LOOP_SECONDS
    text = raw.strip()
    if not (text.isascii() and text.lstrip("-").isdigit()):
        raise ValueError(f"{LOOP_ENV} must be an integer number of seconds; got {raw!r}")
    return _checked(int(text, 10))


class LoopGeometry(NamedTuple):
    """Step boundaries of one loop length (5 s steps)."""

    loop_seconds: int
    num_steps: int
    fail_step: int            # the bearing stops
    repair_steps: int         # length of the repair downtime
    bpfo_alarm_step: int      # the BPFO band reaches its alarm threshold
    alarm_offset_seconds: int  # seconds into the loop of the BPFO alarm
    anchor_residue: int       # a congruent anchor is ``% TELEMETRY_GRID_SECONDS`` this

    @property
    def phase_seconds(self) -> dict[str, tuple[int, int]]:
        """Seconds into the loop at which each phase starts, and where it ends."""
        alarm = self.bpfo_alarm_step * INTERVAL_SECONDS
        fail = self.fail_step * INTERVAL_SECONDS
        repaired = (self.fail_step + self.repair_steps) * INTERVAL_SECONDS
        return {"degrading": (0, alarm), "bpfo_alarm": (alarm, fail),
                "repair": (fail, repaired), "healthy": (repaired, self.num_steps * INTERVAL_SECONDS)}

    @property
    def alarm_in_minutes_range(self) -> tuple[int, int]:
        """The relative placement form: at least 5 minutes, at most one loop ahead."""
        return (ALARM_IN_MINUTES_MIN, self.loop_seconds // 60)


def geometry_for(loop_seconds: int) -> LoopGeometry:
    """The geometry of a loop of *loop_seconds* (validated as the env value is)."""
    loop = _checked(int(loop_seconds))
    num_steps = loop // INTERVAL_SECONDS
    fail_step = int(num_steps * FAIL_FRACTION)
    repair_steps = min(REPAIR_STEPS_CAP, num_steps // REPAIR_LOOP_DIVISOR)
    alarm_step = math.ceil(
        math.sqrt((BPFO_ALARM_MM_S - BPFO_HEALTHY) / (BPFO_FAILED - BPFO_HEALTHY)) * fail_step)
    offset = alarm_step * INTERVAL_SECONDS
    return LoopGeometry(loop, num_steps, fail_step, repair_steps, alarm_step, offset,
                        (-offset) % TELEMETRY_GRID_SECONDS)


# Read ONCE, at import (the same env the simulator reads; unset on demo/frozen).
LOOP_SECONDS = loop_seconds_from(os.environ.get(LOOP_ENV))   # 28800 when unset
GEOMETRY = geometry_for(LOOP_SECONDS)
