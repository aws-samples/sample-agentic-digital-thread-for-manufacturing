# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Spindle-bearing condition model for the fourth site's story mill (ADR-0073).

ONE model, two consumers, so the trigger really is "from the vibration data":

* ``highbyte/generate_datasets.py`` emits the mill's OPC UA tags from it —
  the per-axis RMS velocity that already existed plus the frequency-domain
  features an edge analyzer would publish (1x running-speed amplitude, the
  BPFO / BPFI defect bands, crest factor, kurtosis, and the band alarm);
* ``erp_simulation.mes_simulator.MESSimulator.monitor_asset_condition`` reads
  the same features at the same loop step and raises the site's CMMS work
  order when the BPFO band crosses its threshold.

Both sides derive the loop step from the WALL CLOCK (``loop_step``), and the
OPC UA server plays its dataset wall-clock aligned the same way
(``components/opc-server/server.mjs``), so at any instant the value the CMMS
rule evaluates is the value the OPC UA server is serving.

The live workshop story (ADR-0085) may shift the simulator's loop to start at
a stored anchor epoch (``story_seconds`` / ``loop_start_epoch``); anchor 0 is
exactly the wall clock above, so demo and frozen deploys are unchanged.

Everything here is pure and dependency-free: no RNG, no package imports, so
the dataset generator can import it from outside the package. The ONE
exception to "no I/O" is the loop length: ``STORY_LOOP_SECONDS`` is read
from the environment ONCE, at import, by module-level code (ADR-0091). Unset
or empty it is the shipped 8 h loop and every derived constant is
byte-identical; set, it must be a multiple of 300 s in [3600, 28800], else
the import raises ``ValueError`` (fail-closed; the same rules as
``stacks/_workshop_story.resolve_loop_seconds``). Every step boundary scales
with the loop; the bearing repair is 1/24 of the loop capped at the shipped
20 min, so it always ends inside the loop. Only a live-story synth sets it;
the OPC UA dataset generator runs with it unset.

Shape of one loop (``TOTAL_SECONDS``, 5 s steps):

    healthy ──── degradation climbs ────┬─ BPFO alarm ─┬─ RMS alarm ─┬─ repair ─┬─ healthy
    step 0                     BPFO_ALARM_STEP   BEARING_FAIL_STEP  +REPAIR      wrap

The BPFO band grows with the SQUARE of the degradation fraction (a developing
outer-race defect adds impulsive energy that accelerates), so it crosses its
threshold well before the overall RMS reaches the 2.8 mm/s story alarm. That
gap is the leading-indicator window: the work order exists while the mill is
still running, and the fixed bearing-replacement downtime that follows is the
repair it scheduled, not the event that discovered the fault.
"""

from __future__ import annotations

import math
import os
from typing import Dict, NamedTuple, Optional, Tuple

# Loop geometry — must match highbyte/generate_datasets.py (pinned by test).
INTERVAL_SEC = 5
FAIL_FRACTION = 0.9         # the bearing stops 90 % into the loop
REPAIR_STEPS_CAP = 240      # 20 min at 5 s: the shipped repair, never exceeded
REPAIR_LOOP_DIVISOR = 24    # a shorter loop repairs for 1/24 of it (5760 // 24 == 240)
LOOP_ENV = "STORY_LOOP_SECONDS"
DEFAULT_LOOP_SECONDS = 28800           # 8 h dataset loop
LOOP_SECONDS_MULTIPLE = 300            # whole telemetry grid slots (== TELEMETRY_GRID_SECONDS, pinned)
TELEMETRY_GRID_SECONDS = 300           # the one story clock's grid (see "One story clock" below)
# Inclusive. Capped at the 8 h default: above it the history's failed batch
# can overlap a normal line-3 run (42600..51300 s, 75600..86400 s; ADR-0091).
LOOP_SECONDS_RANGE = (3600, DEFAULT_LOOP_SECONDS)


def loop_seconds_from(raw: Optional[str]) -> int:
    """The loop length for a ``STORY_LOOP_SECONDS`` value (ADR-0091).

    ``None`` or blank -> DEFAULT_LOOP_SECONDS. Otherwise a base-10 integer,
    a multiple of LOOP_SECONDS_MULTIPLE inside LOOP_SECONDS_RANGE (inclusive).
    Anything else raises ValueError; there is no silent fallback to the
    default.
    """
    if raw is None or not raw.strip():
        return DEFAULT_LOOP_SECONDS
    text = raw.strip()
    if not (text.isascii() and text.lstrip("-").isdigit()):
        raise ValueError(f"{LOOP_ENV} must be an integer number of seconds; got {raw!r}")
    return _checked_loop(int(text, 10))


def _checked_loop(value: int) -> int:
    low, high = LOOP_SECONDS_RANGE
    if value % LOOP_SECONDS_MULTIPLE or not low <= value <= high:
        raise ValueError(f"{LOOP_ENV} must be a multiple of {LOOP_SECONDS_MULTIPLE} in "
                         f"[{low}, {high}]; got {value}")
    return value


# Read ONCE, at import (the module's one environment read). The step
# boundaries derive from it below, through geometry_for.
TOTAL_SECONDS = loop_seconds_from(os.environ.get(LOOP_ENV))  # 28800 when unset

# Story numbers (seed: cmms work order 1195; feed handler): overall RMS.
VIB_HEALTHY_MM_S = 1.0
VIB_ALARM_MM_S = 2.8

# The story mill's identity in each system (the graph discovers these are
# one machine; nothing here tells it so).
STORY_SITEWISE_ASSET = "5-Axis-Milling-CMX-441"   # SiteWise asset (model CNC)
STORY_MES_EQUIPMENT_ID = "L3-M1"                  # line-3 MES production_runs.equipment_id
STORY_CMMS_ASSET_NUM = "1001-CMX-441"             # site CMMS assets.asset_num
STORY_MES_LINE = "LINE3"                          # MES production_lines key of STORY_MES_EQUIPMENT_ID

# Spindle / bearing geometry for the defect frequencies a technician reads
# off the spectrum. BPFO = (n/2) * f_r * (1 - (d/D) cos a); BPFI uses (1 + ..).
SPINDLE_RPM = 6200.0
BEARING_ROLLERS = 9
BEARING_DIAMETER_RATIO = 0.33  # (d/D) * cos(contact angle)
SHAFT_HZ = SPINDLE_RPM / 60.0
BPFO_HZ = round((BEARING_ROLLERS / 2.0) * SHAFT_HZ * (1.0 - BEARING_DIAMETER_RATIO), 1)
BPFI_HZ = round((BEARING_ROLLERS / 2.0) * SHAFT_HZ * (1.0 + BEARING_DIAMETER_RATIO), 1)

# Feature name -> (healthy value, value at the moment the bearing stops,
# growth law). Amplitudes in mm/s velocity for the bands; crest factor and
# kurtosis are dimensionless (a healthy Gaussian signal sits near 3.0).
FEATURES: Dict[str, Tuple[float, float, str]] = {
    "one_x": (0.60, 0.90, "linear"),    # imbalance line: barely moves
    "bpfo": (0.05, 1.20, "square"),     # outer-race defect band: the leading indicator
    "bpfi": (0.03, 0.30, "square"),     # inner-race band: secondary
    "crest": (3.0, 6.5, "square"),      # impulsiveness
    "kurtosis": (3.0, 9.0, "square"),   # peakedness
}
BPFO_ALARM_MM_S = 0.70


class StoryGeometry(NamedTuple):
    """Step boundaries of one loop length (INTERVAL_SEC steps; ADR-0091)."""

    total_seconds: int
    num_steps: int
    fail_step: int             # the bearing stops
    repair_steps: int          # length of the repair downtime
    bpfo_alarm_step: int       # the BPFO band reaches BPFO_ALARM_MM_S
    alarm_offset_seconds: int  # seconds into the loop of the BPFO alarm
    anchor_residue: int        # a congruent anchor is ``% TELEMETRY_GRID_SECONDS`` this


def _alarm_step_for(fail_step: int) -> int:
    healthy, failed, _law = FEATURES["bpfo"]
    frac = math.sqrt((BPFO_ALARM_MM_S - healthy) / (failed - healthy))
    return math.ceil(frac * fail_step)


def geometry_for(total_seconds: int) -> StoryGeometry:
    """The geometry of a loop of ``total_seconds`` (validated as the env value
    is). Pure: the module constants below are ``geometry_for(TOTAL_SECONDS)``."""
    total = _checked_loop(int(total_seconds))
    num_steps = total // INTERVAL_SEC
    fail_step = int(num_steps * FAIL_FRACTION)
    repair_steps = min(REPAIR_STEPS_CAP, num_steps // REPAIR_LOOP_DIVISOR)
    alarm_step = _alarm_step_for(fail_step)
    offset = alarm_step * INTERVAL_SEC
    return StoryGeometry(total, num_steps, fail_step, repair_steps, alarm_step, offset,
                         (-offset) % TELEMETRY_GRID_SECONDS)


GEOMETRY = geometry_for(TOTAL_SECONDS)
NUM_STEPS = GEOMETRY.num_steps  # 5760
BEARING_FAIL_STEP = GEOMETRY.fail_step  # 5184
BEARING_REPAIR_STEPS = GEOMETRY.repair_steps  # 240
DOWNTIME: Tuple[int, int] = (BEARING_FAIL_STEP, BEARING_REPAIR_STEPS)

# Values while the spindle is stopped for the replacement.
_DOWN = {"one_x": 0.0, "bpfo": 0.0, "bpfi": 0.0, "crest": 0.0, "kurtosis": 0.0}


def loop_step(epoch_seconds: float, total_seconds: int = TOTAL_SECONDS,
              interval_sec: int = INTERVAL_SEC) -> int:
    """Dataset row the OPC UA server is serving at ``epoch_seconds``.

    Loops are aligned to the epoch (with the 8 h default: 00:00, 08:00 and
    16:00 UTC), which is exactly what server.mjs computes with Date.now().
    """
    return int((epoch_seconds % total_seconds) // interval_sec)


def loop_number(epoch_seconds: float, total_seconds: int = TOTAL_SECONDS) -> int:
    """Ordinal of the loop containing ``epoch_seconds`` (one work order per loop)."""
    return int(epoch_seconds // total_seconds)


def degradation(step: int, downtime: Tuple[int, int] = DOWNTIME) -> Optional[float]:
    """Degradation fraction in [0, 1) before the bearing stops, ``None`` while
    it is being replaced, 0.0 (healthy again) after the repair."""
    fail_step, repair_steps = downtime
    if step < fail_step:
        return step / fail_step
    if step < fail_step + repair_steps:
        return None
    return 0.0


def trend(step: int, healthy: float, alarm: float,
          downtime: Tuple[int, int] = DOWNTIME) -> float:
    """Linear climb healthy -> alarm, the alarm value during the repair,
    healthy after. This is the RMS-velocity story trend (ADR-0079)."""
    frac = degradation(step, downtime)
    if frac is None:
        return alarm
    return healthy + (alarm - healthy) * frac


def _grow(healthy: float, failed: float, frac: float, law: str) -> float:
    x = frac * frac if law == "square" else frac
    return healthy + (failed - healthy) * x


def spectral_features(step: int, downtime: Tuple[int, int] = DOWNTIME) -> Dict[str, object]:
    """Frequency-domain features the edge analyzer publishes at ``step``.

    Returns one_x, bpfo, bpfi (mm/s), crest, kurtosis (unitless) and
    ``bpfo_alarm`` (True once the BPFO band is at or over BPFO_ALARM_MM_S
    while the spindle is turning). Deterministic; the generator adds jitter.
    """
    frac = degradation(step, downtime)
    if frac is None:
        return {**_DOWN, "bpfo_alarm": False}
    out: Dict[str, object] = {
        name: round(_grow(h, f, frac, law), 4) for name, (h, f, law) in FEATURES.items()
    }
    out["bpfo_alarm"] = float(out["bpfo"]) >= BPFO_ALARM_MM_S
    return out


def bpfo_alarm_step(downtime: Tuple[int, int] = DOWNTIME) -> int:
    """First loop step at which the BPFO band reaches its threshold —
    strictly before the bearing stops, so the work order leads the failure."""
    return _alarm_step_for(downtime[0])


BPFO_ALARM_STEP = GEOMETRY.bpfo_alarm_step  # == bpfo_alarm_step()


# ---------------------------------------------------------------------------
# Anchored story clock (live workshop story, ADR-0085)
#
# ``anchor`` is ``simulation_config.story_anchor_epoch`` when the live-story
# history wrote one, else 0.0. Anchor 0 (or any non-positive value) means the
# story clock IS the wall clock, so demo / frozen behaviour is unchanged.
# ---------------------------------------------------------------------------

DEFECT_REJECT_MIN = 0.15  # reject fraction at the first BPFO-alarm step
DEFECT_REJECT_MAX = 0.40  # reject fraction at the last step before the bearing stops


def story_seconds(epoch: float, anchor: float = 0.0) -> float:
    """Story time for ``epoch``: ``epoch - anchor`` when ``anchor > 0``, else
    ``epoch`` unchanged (anchor 0 == today's wall-clock behaviour).

    Feed the result to :func:`loop_step` / :func:`loop_number`; with a positive
    anchor loop 0 step 0 starts exactly at ``anchor``.
    """
    return epoch - anchor if anchor > 0 else epoch


def loop_start_epoch(epoch: float, anchor: float = 0.0,
                     total_seconds: int = TOTAL_SECONDS) -> float:
    """Absolute epoch at which the loop containing ``epoch`` began, given the
    anchor (``anchor <= 0``: loops are epoch-aligned, as :func:`loop_step`).

    Always ``start <= epoch < start + total_seconds``; epochs before a positive
    anchor fall in negative-numbered loops (floor division), never an error.
    """
    base = anchor if anchor > 0 else 0.0
    return base + loop_number(story_seconds(epoch, anchor), total_seconds) * total_seconds


def phase(step: int) -> str:
    """Story phase at loop ``step``: ``degrading`` before BPFO_ALARM_STEP,
    ``bpfo_alarm`` until BEARING_FAIL_STEP, ``repair`` for BEARING_REPAIR_STEPS,
    then ``healthy`` until the loop wraps."""
    if step < BPFO_ALARM_STEP:
        return "degrading"
    if step < BEARING_FAIL_STEP:
        return "bpfo_alarm"
    if step < BEARING_FAIL_STEP + BEARING_REPAIR_STEPS:
        return "repair"
    return "healthy"


def defect_reject_fraction(step: int) -> float:
    """Fraction of a dimensional check's sample that fails at loop ``step``.

    0.0 outside the ``bpfo_alarm`` phase; inside it a linear climb from
    DEFECT_REJECT_MIN at BPFO_ALARM_STEP to DEFECT_REJECT_MAX at
    BEARING_FAIL_STEP - 1 (monotone non-decreasing).
    """
    if phase(step) != "bpfo_alarm":
        return 0.0
    span = max(1, BEARING_FAIL_STEP - 1 - BPFO_ALARM_STEP)
    frac = (step - BPFO_ALARM_STEP) / span
    return DEFECT_REJECT_MIN + (DEFECT_REJECT_MAX - DEFECT_REJECT_MIN) * frac


def story_geometry() -> Dict[str, int]:
    """Loop geometry the live-story history records in ``simulation_config``
    (``story_loop_seconds`` etc.) so readers need not import this module."""
    return {
        "loop_seconds": TOTAL_SECONDS,
        "interval_seconds": INTERVAL_SEC,
        "bpfo_alarm_step": BPFO_ALARM_STEP,
        "fail_step": BEARING_FAIL_STEP,
        "repair_steps": BEARING_REPAIR_STEPS,
    }


# ---------------------------------------------------------------------------
# One story clock: the telemetry grid (live story, ADR-0088 and ADR-0089)
#
# SiteWise telemetry, the CMMS condition order and the MES DIMENSIONAL checks
# share ONE epoch-aligned grid (``ts % TELEMETRY_GRID_SECONDS == 0``, the same
# grid the degradation feed uses). A story anchor is CONGRUENT when
# ``anchor % TELEMETRY_GRID_SECONDS == ANCHOR_RESIDUE``: then the BPFO alarm
# instant ``anchor + ALARM_OFFSET_SECONDS`` of every loop is a grid point
# (a loop is a whole number of slots), so a re-anchor republishes the same
# timestamps and the sensor, the order and the check agree to the second.
# ---------------------------------------------------------------------------

# (TELEMETRY_GRID_SECONDS is defined with the loop geometry above.)
ALARM_OFFSET_SECONDS = GEOMETRY.alarm_offset_seconds
ANCHOR_RESIDUE = GEOMETRY.anchor_residue  # 10 with the shipped geometry
_SNAP_MODES = ("down", "nearest")


def alarm_epoch(epoch: float, anchor: float = 0.0) -> float:
    """Epoch of the BPFO alarm in the loop containing ``epoch``."""
    return loop_start_epoch(epoch, anchor) + ALARM_OFFSET_SECONDS


def fail_epoch(epoch: float, anchor: float = 0.0) -> float:
    """Epoch at which the bearing stops in the loop containing ``epoch``."""
    return loop_start_epoch(epoch, anchor) + BEARING_FAIL_STEP * INTERVAL_SEC


def grid_floor(epoch: float) -> int:
    """Latest telemetry grid point at or before ``epoch``."""
    return int(math.floor(epoch / TELEMETRY_GRID_SECONDS)) * TELEMETRY_GRID_SECONDS


def snap_anchor(epoch: float, mode: str = "down") -> int:
    """Congruent anchor for ``epoch`` (``% TELEMETRY_GRID_SECONDS == ANCHOR_RESIDUE``).

    ``down`` (the deploy default, ADR-0089 D15) returns the latest congruent epoch at
    or before ``epoch`` (at most one slot earlier); ``nearest`` (placement,
    ADR-0089 D12) the closest one, ties going down. Any other mode raises ValueError.
    """
    if mode not in _SNAP_MODES:
        raise ValueError(f"snap_anchor mode must be one of {_SNAP_MODES}; got {mode!r}")
    base = grid_floor(epoch - ANCHOR_RESIDUE) + ANCHOR_RESIDUE
    if mode == "nearest" and epoch - base > TELEMETRY_GRID_SECONDS / 2:
        return base + TELEMETRY_GRID_SECONDS
    return base
