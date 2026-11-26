# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Placeable live-story clock, app side (ADR-0089 D12/D13).

Backs ``POST /api/admin/workshop/story/clock``
(``agents/workshop_story_routes.py``). The route is thin: it validates the
body (:class:`PlaceClockRequest`) and either

* ``dry_run: true`` (a strict JSON boolean) - previews the placement (new
  anchor, alarm, loops shifted, the fixed ``effect`` text and any
  ``warnings``, each with its stable ``effect_codes`` / ``warning_codes``
  twin for the UI's i18n) with a pinned copy
  of the pure ``erp_simulation.story_replan`` arithmetic and invokes NOTHING; or
* applies it - ONE RequestResponse invoke of the ``erp-story-history``
  Lambda (action ``place``, named by :data:`STORY_HISTORY_FUNCTION_ENV`,
  set only on a live workshop deploy). The Lambda does the CAS-guarded,
  advisory-locked re-plan in seconds and returns its report, which is
  returned as-is plus ``alarm_at_iso``. The story OPC UA server picks the
  new anchor up from the simulator's mirror within about 2 minutes and
  climbs onto it by the next alarm (ADR-0092 decision 7).

The relative form ``alarm_in_minutes`` (an integer, 5 up to one loop in
minutes: 480 for the 8 h default) is resolved here
to an absolute ``alarm_at`` snapped UP to the 300 s grid from ONE ``now``, so
the Lambda ``place`` contract is unchanged; the answer echoes it as
``requested``.

The keep-history form ``keep_history: true`` (ADR-0095; with ``alarm_at`` or
``alarm_in_minutes`` only) moves ONLY the live clock: the apply is ONE invoke
of the Lambda ``place_live`` action with ``expected_live_anchor`` (the
lookup's ``live_anchor_epoch``), and the dry run reports the ``keeps_history``
/ ``moves_live`` effect instead of the rewrite. A move the model refuses (an
alarm inside the written history) comes back as the Lambda's ``invalid`` -> 422.

The reset form ``reset: true`` (ADR-0091 Design C, the failsafe; exclusive
with every placement form, combinable only with ``expected_anchor_epoch`` /
``force`` / ``dry_run``) re-plans onto the DEFAULT anchor the story-history
resource recorded (packaged or deploy): the apply is ONE invoke of the
Lambda ``reset`` action; the dry run stays Lambda-free and reads the stored
default through ``story_lookup.stored_default`` (no default -> 409
``no_default_anchor``; a read failure -> 502), reporting it with the
normalised new anchor and the ``restores_default`` effect. The payloads of
the existing forms are byte-identical.

The CAS is explicit: ``expected_anchor_epoch: null`` (skip the compare) is
a 422 unless the body also says ``force: true``. One placement at a time per
process: a second apply while one is in flight is a 409 ``in_flight`` at once
(a NON-blocking guard, the Evals one-run-lock shape; a dry run is never
refused). Across replicas the Lambda's advisory lock + CAS still serialise.

Error mapping: no function env var -> 404 (not a live workshop); a Lambda
``stale_anchor`` -> 409, ``invalid`` -> 422 (message passed through digit-masked
and capped at 300 characters, like every other mapped message); ``internal``,
an unknown error, a
FunctionError, an unreadable payload or a transport failure -> 502 with a
digit-masked message (no host, port or id leaks to the client).

The app image ships only ``agents/`` (no ``erp_simulation``), so the anchor
arithmetic below mirrors ``erp_simulation.condition_monitor`` /
``story_replan``; parity is pinned by ``tests/unit/test_story_clock_demo.py``
and, across loop lengths, ``tests/unit/test_story_clock_loop_parity.py``.
The loop length is ``STORY_LOOP_SECONDS``, read ONCE at import (by
``agents.demo.story_loop``, which holds the loop math) with the SAME
validation as the simulator (ADR-0091): unset or blank is the shipped 8 h
loop (every derived constant byte-identical); a bad value fails the import.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import story_loop as _story_loop
from .story_loop import (  # noqa: F401 - re-exported names, unchanged
    DEFAULT_LOOP_SECONDS,
    INTERVAL_SECONDS,
    LOOP_ENV,
    LOOP_SECONDS_RANGE,
    TELEMETRY_GRID_SECONDS,
    loop_seconds_from,
)

logger = logging.getLogger(__name__)

STORY_HISTORY_FUNCTION_ENV = "STORY_HISTORY_FUNCTION_NAME"
LIVE_STORY = "live"          # the only workshop story with a clock (agents.demo.workshop.STORIES)
PLACE_ACTION = "place"
RESET_ACTION = "reset"
LAMBDA_READ_TIMEOUT_S = 90   # the re-plan is DB-only and takes seconds
NOT_LIVE_DETAIL = "not a live workshop (no story-history function configured)"

# Mirrors of erp_simulation.condition_monitor / story_replan. The loop math and
# the one STORY_LOOP_SECONDS read live in story_loop (pure, no FastAPI); the
# names below bind from it, unchanged. Parity is pinned by
# tests/unit/test_story_clock_loop_parity.py across every valid loop length.
_FAIL_FRACTION = _story_loop.FAIL_FRACTION
_REPAIR_STEPS_CAP, _REPAIR_LOOP_DIVISOR = _story_loop.REPAIR_STEPS_CAP, _story_loop.REPAIR_LOOP_DIVISOR
_BPFO_HEALTHY, _BPFO_FAILED, _BPFO_ALARM_MM_S = (
    _story_loop.BPFO_HEALTHY, _story_loop.BPFO_FAILED, _story_loop.BPFO_ALARM_MM_S)

# The loop read at import (story_loop), and its geometry.
LOOP_SECONDS = _story_loop.LOOP_SECONDS                               # 28800 when unset
_GEOMETRY = _story_loop.GEOMETRY
_NUM_STEPS = _GEOMETRY.num_steps                                      # 5760
_FAIL_STEP = _GEOMETRY.fail_step                                      # 5184
_REPAIR_STEPS = _GEOMETRY.repair_steps                                # 240
_BPFO_ALARM_STEP = _GEOMETRY.bpfo_alarm_step                          # 3898
ALARM_OFFSET_SECONDS = _GEOMETRY.alarm_offset_seconds
ANCHOR_RESIDUE = _GEOMETRY.anchor_residue
# Seconds into the loop at which each phase starts, and where it ends.
PHASE_SECONDS: dict[str, tuple[int, int]] = _GEOMETRY.phase_seconds
# alarm_at must lie within [now - 1 d, now + 7 d] (the Lambda's own window).
ALARM_WINDOW_SECONDS = (86400, 7 * 86400)
# The relative form: the alarm in N whole minutes, snapped UP to the grid; at
# most one loop ahead (480 for the 8 h default, 180 for a 3 h loop).
ALARM_IN_MINUTES_RANGE = _GEOMETRY.alarm_in_minutes_range

# What a placement does, stated in the dry run (fixed text; ADR-0084 D2: the
# dry run stays Lambda-free, so there are no counts).
PLACEMENT_EFFECT = (
    "rewrites the three-day history and every run number",
    "moves the sensor feed onto the new clock by the next alarm (recorded sensor history stays)",
)
LOOPS_SHIFTED_WARNING = ("an earlier alarm already exists in the rewritten history; "
                         "the account shows a condition order now")
# Stable codes for the same statements, one per text entry (the UI maps them
# to i18n; the text lists stay for the CLI).
PLACEMENT_EFFECT_CODES = ("rewrites_history", "resets_backfill")
LOOPS_SHIFTED_WARNING_CODE = "loops_shifted"
# The keep-history move (ADR-0095): only the live side follows the new alarm.
KEEP_HISTORY_EFFECT = (
    "keeps the three-day history, the failed batch and every run number as written",
    "moves the sensor feed, the live work orders and the alarm onto the new clock by the next alarm",
)
KEEP_HISTORY_EFFECT_CODES = ("keeps_history", "moves_live")
KEEP_HISTORY_ACTION = "place_live"
# A keep-history move made once the live side is already PAST this loop's alarm
# (alarm, repair or healthy phase) puts the live sensor back into the degrading
# phase: the hour's vibration trend then reads as a drop at the move, not a
# climb (test event 28, 2026-10-06). Said in the dry run so the attendee moves
# the alarm before it fires, or demos the trend an hour after the new one.
REWINDS_LIVE_WARNING = ("the plant is already past this loop's bearing alarm; moving the alarm "
                        "ahead rewinds the live sensor to the degrading phase, so the last hour's "
                        "vibration trend reads as a drop at the move (move before the alarm "
                        "fires, or demo the trend an hour after the new alarm)")
REWINDS_LIVE_WARNING_CODE = "rewinds_live_phase"
# The reset dry run's extra statement (ADR-0091 Design C), before PLACEMENT_EFFECT.
RESET_EFFECT = "restores the default story clock (the packaged or deploy-time anchor)"
RESET_EFFECT_CODE = "restores_default"
NO_DEFAULT_DETAIL = "no default story anchor is stored; there is nothing to reset to"

# The anchor CAS is an epoch no later than 2100-01-01T00:00:00Z.
MAX_ANCHOR_EPOCH = 4102444800

_STATUS_BY_ERROR = {"stale_anchor": 409, "invalid": 422, "no_default_anchor": 409}

# Process-level one-placement guard (acquired NON-blocking: in flight -> 409).
_PLACE_IN_FLIGHT = threading.Lock()
IN_FLIGHT_DETAIL = "a story clock placement is already in flight"


# ── Request ───────────────────────────────────────────────────────────────────

class PlaceClockRequest(BaseModel):
    """Exactly one of ``{alarm_at}``, ``{alarm_in_minutes}``, ``{phase,
    elapsed_minutes}`` or ``{reset: true}``, plus the CAS anchor (null only
    with ``force: true``). ``keep_history: true`` (ADR-0095) moves only the
    live clock, with an alarm form only; its CAS anchor is then the LIVE
    anchor (``live_anchor_epoch``)."""

    model_config = ConfigDict(extra="forbid")

    alarm_at: Optional[datetime] = None
    alarm_in_minutes: Optional[int] = Field(default=None, strict=True, ge=ALARM_IN_MINUTES_RANGE[0],
                                            le=ALARM_IN_MINUTES_RANGE[1])
    phase: Optional[Literal["degrading", "bpfo_alarm", "repair", "healthy"]] = None
    elapsed_minutes: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    expected_anchor_epoch: Optional[int] = Field(..., le=MAX_ANCHOR_EPOCH)
    force: bool = Field(default=False, strict=True)
    dry_run: bool = Field(default=False, strict=True)
    reset: bool = Field(default=False, strict=True)
    keep_history: bool = Field(default=False, strict=True)

    @field_validator("alarm_at", mode="before")
    @classmethod
    def _iso_string(cls, value):
        """STRICT: a str parsed as ISO 8601 only (pydantic's lax datetime would
        also take a number or a numeric string as a Unix timestamp)."""
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("alarm_at must be an ISO 8601 string with a timezone")
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("alarm_at must be an ISO 8601 string with a timezone") from None

    @field_validator("alarm_at")
    @classmethod
    def _aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("alarm_at must carry a timezone (e.g. Z or +00:00)")
        return value

    @field_validator("elapsed_minutes", "expected_anchor_epoch", "alarm_in_minutes", mode="before")
    @classmethod
    def _plain_number(cls, value):
        if isinstance(value, (bool, str)):
            raise ValueError("must be a JSON number")
        return value

    @model_validator(mode="after")
    def _one_form(self):
        by_phase = self.phase is not None or self.elapsed_minutes is not None
        forms = (self.alarm_at is not None) + (self.alarm_in_minutes is not None) + by_phase + self.reset
        if forms != 1:
            raise ValueError("give exactly one of alarm_at, alarm_in_minutes, phase + elapsed_minutes "
                             "or reset: true")
        if by_phase and (self.phase is None or self.elapsed_minutes is None):
            raise ValueError("phase and elapsed_minutes go together")
        if (self.expected_anchor_epoch is None) != self.force:
            raise ValueError("a null expected_anchor_epoch (skip the CAS) needs force: true, "
                             "and force is only for a null expected_anchor_epoch")
        if self.keep_history and (self.reset or by_phase):
            raise ValueError("keep_history: true goes with alarm_at or alarm_in_minutes only")
        return self


# ── Pure anchor arithmetic (mirror of erp_simulation.story_replan) ────────────

def snap_anchor_nearest(epoch: float) -> int:
    """The congruent anchor (``% 300 == ANCHOR_RESIDUE``) nearest *epoch*, ties down."""
    base = int(math.floor((epoch - ANCHOR_RESIDUE) / TELEMETRY_GRID_SECONDS)) \
        * TELEMETRY_GRID_SECONDS + ANCHOR_RESIDUE
    return base + TELEMETRY_GRID_SECONDS if epoch - base > TELEMETRY_GRID_SECONDS / 2 else base


def normalize_anchor(reference_epoch: float, now: float) -> int:
    """The congruent anchor with the reference's phase inside ``(now - loop, now]``."""
    snapped = snap_anchor_nearest(reference_epoch)
    return snapped - math.ceil((snapped - now) / LOOP_SECONDS) * LOOP_SECONDS


def loops_shifted(reference_epoch: float, now: float) -> int:
    """Whole loops :func:`normalize_anchor` moved the reference back."""
    snapped = snap_anchor_nearest(reference_epoch)
    return (snapped - normalize_anchor(reference_epoch, now)) // LOOP_SECONDS


def alarm_to_anchor(alarm_epoch: float) -> int:
    """The reference anchor whose loop-0 BPFO alarm is *alarm_epoch* (grid-rounded)."""
    return snap_anchor_nearest(alarm_epoch - ALARM_OFFSET_SECONDS)


def phase_offset_to_anchor(phase: str, elapsed_minutes: float, now: float) -> int:
    """The normalised anchor putting *now* ``elapsed_minutes`` into *phase*."""
    if phase not in PHASE_SECONDS:
        raise ValueError(f"phase must be one of {tuple(PHASE_SECONDS)}, got {phase!r}")
    start, end = PHASE_SECONDS[phase]
    offset = float(elapsed_minutes) * 60
    if not 0 <= offset < end - start:
        raise ValueError(f"elapsed_minutes must be in [0, {(end - start) / 60:g}) for {phase!r}, "
                         f"got {elapsed_minutes!r}")
    return normalize_anchor(now - start - offset, now)


def alarm_in_to_epoch(minutes: int, now: float) -> int:
    """The grid point at or after ``now + minutes`` (snapped UP; a grid point stays put)."""
    return math.ceil((now + minutes * 60) / TELEMETRY_GRID_SECONDS) * TELEMETRY_GRID_SECONDS


# ── Place ─────────────────────────────────────────────────────────────────────

def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _masked(text: str, limit: int = 300) -> str:
    return re.sub(r"\d", "#", str(text))[:limit]


def _reference(req: PlaceClockRequest, now: float) -> int:
    """The un-normalised reference anchor (ValueError on an out-of-range input)."""
    if req.alarm_at is None:
        return phase_offset_to_anchor(req.phase, req.elapsed_minutes, now)
    alarm = req.alarm_at.timestamp()
    before, ahead = ALARM_WINDOW_SECONDS
    if not now - before <= alarm <= now + ahead:
        raise ValueError(f"alarm_at must be within [now - {before} s, now + {ahead} s]")
    return alarm_to_anchor(alarm)


def _resolved(req: PlaceClockRequest, now: float) -> tuple[PlaceClockRequest, Optional[dict]]:
    """*req* with ``alarm_in_minutes`` turned into ``alarm_at`` (ONE ``now``),
    plus the ``requested`` echo; other forms pass through with no echo."""
    if req.alarm_in_minutes is None:
        return req, None
    alarm = alarm_in_to_epoch(req.alarm_in_minutes, now)
    absolute = req.model_copy(update={"alarm_in_minutes": None,
                                      "alarm_at": datetime.fromtimestamp(alarm, tz=timezone.utc)})
    return absolute, {"alarm_in_minutes": req.alarm_in_minutes, "alarm_at": alarm}


def _echo(report: dict, requested: Optional[dict]) -> dict:
    return report if requested is None else {**report, "requested": requested}


def _effect(req: PlaceClockRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(texts, codes)`` of what this form does (keep-history or full)."""
    if req.keep_history:
        return KEEP_HISTORY_EFFECT, KEEP_HISTORY_EFFECT_CODES
    return PLACEMENT_EFFECT, PLACEMENT_EFFECT_CODES


def live_past_alarm(live_anchor: Optional[int], now: float) -> bool:
    """True when the live clock anchored at *live_anchor* is already past this
    loop's bearing alarm at *now* (alarm, repair or healthy phase)."""
    if live_anchor is None:
        return False
    return (now - live_anchor) % LOOP_SECONDS >= PHASE_SECONDS["degrading"][1]


def _warnings(req: PlaceClockRequest, now: float, shifted: int, alarm: int) -> tuple[list, list]:
    """``(texts, codes)`` of the dry run's warnings: the loops-shifted note for
    any form; the live-rewind note for a keep-history move that puts an alarm
    AHEAD of a live side already past its alarm (its CAS anchor is the live one)."""
    texts, codes = [], []
    if shifted >= 1:
        texts.append(LOOPS_SHIFTED_WARNING)
        codes.append(LOOPS_SHIFTED_WARNING_CODE)
    if req.keep_history and alarm > now and live_past_alarm(req.expected_anchor_epoch, now):
        texts.append(REWINDS_LIVE_WARNING)
        codes.append(REWINDS_LIVE_WARNING_CODE)
    return texts, codes


def preview(req: PlaceClockRequest, now: float) -> dict:
    """The dry-run answer: what the Lambda's re-plan WOULD place (nothing invoked)."""
    try:
        reference = _reference(req, now)
    except ValueError as err:
        raise HTTPException(status_code=422, detail={"error": "invalid", "message": str(err)})
    new_anchor = normalize_anchor(reference, now)
    alarm = snap_anchor_nearest(reference) + ALARM_OFFSET_SECONDS
    shifted = loops_shifted(reference, now)
    effect, codes = _effect(req)
    warnings, warning_codes = _warnings(req, now, shifted, alarm)
    keep = {"keep_history": True} if req.keep_history else {}
    return {"dry_run": True, **keep, "expected_anchor": req.expected_anchor_epoch,
            "new_anchor": new_anchor, "new_anchor_iso": _iso(new_anchor),
            "alarm_at": alarm, "alarm_at_iso": _iso(alarm),
            "loops_shifted": shifted, "effect": list(effect),
            "effect_codes": list(codes),
            "warnings": warnings, "warning_codes": warning_codes}


def reset_preview(req: PlaceClockRequest, now: float, default: tuple[int, str]) -> dict:
    """The reset dry run: the stored default and what re-planning onto it
    places now (the Lambda's ``reset`` normalises it the same way)."""
    anchor, source = default
    new_anchor = normalize_anchor(anchor, now)
    alarm = new_anchor + ALARM_OFFSET_SECONDS
    return {"dry_run": True, "reset": True, "expected_anchor": req.expected_anchor_epoch,
            "default_anchor_epoch": anchor, "default_anchor_iso": _iso(anchor), "default_source": source,
            "new_anchor": new_anchor, "new_anchor_iso": _iso(new_anchor),
            "alarm_at": alarm, "alarm_at_iso": _iso(alarm), "loops_shifted": 0,
            "effect": [RESET_EFFECT, *PLACEMENT_EFFECT],
            "effect_codes": [RESET_EFFECT_CODE, *PLACEMENT_EFFECT_CODES],
            "warnings": [], "warning_codes": []}


def _default_reader():
    from . import story_lookup  # lazy: the lookup is the ONE config reader
    return story_lookup.stored_default()


def _stored_default(reader: Optional[Callable]) -> tuple[int, str]:
    """The stored default (409 ``no_default_anchor`` when absent; 502 masked on a read failure)."""
    try:
        default = (reader or _default_reader)()
    except Exception as err:  # database unreachable or a failing read
        raise _bad_gateway(f"story config read failed ({type(err).__name__})") from None
    if default is None:
        raise HTTPException(status_code=409, detail={"error": "no_default_anchor", "message": NO_DEFAULT_DETAIL})
    return default


def payload(req: PlaceClockRequest) -> dict:
    """The ``erp-story-history`` invoke payload for *req* (``reset``,
    ``place_live`` or ``place``)."""
    if req.reset:
        return {"action": RESET_ACTION, "expected_anchor": req.expected_anchor_epoch}
    if req.keep_history:
        return {"action": KEEP_HISTORY_ACTION, "alarm_at": req.alarm_at.timestamp(),
                "expected_live_anchor": req.expected_anchor_epoch}
    body: dict = {"action": PLACE_ACTION}
    if req.alarm_at is not None:
        body["alarm_at"] = req.alarm_at.timestamp()
    else:
        body.update(phase=req.phase, elapsed_minutes=req.elapsed_minutes)
    body["expected_anchor"] = req.expected_anchor_epoch
    return body


def _bad_gateway(message: str) -> HTTPException:
    logger.warning("story clock place FAILED: %s", _masked(message))
    return HTTPException(status_code=502, detail={"error": "internal", "message": _masked(message)})


def _invoke(client, function_name: str, body: dict) -> dict:
    """One RequestResponse invoke; a non-object answer or FunctionError is a 502."""
    try:
        resp = client.invoke(FunctionName=function_name, InvocationType="RequestResponse",
                             Payload=json.dumps(body).encode())
        raw = resp["Payload"].read()
        report = json.loads(raw)
    except Exception as err:  # transport, timeout or an unreadable payload
        raise _bad_gateway(f"story-history invoke failed ({type(err).__name__})") from None
    if resp.get("FunctionError"):
        raise _bad_gateway(f"story-history function error ({resp['FunctionError']})")
    if not isinstance(report, dict):
        raise _bad_gateway("story-history returned a non-object payload")
    return report


def _mapped(report: dict) -> dict:
    """The report plus ``alarm_at_iso``, or the HTTPException its ``error`` maps to."""
    error = report.get("error")
    if error in _STATUS_BY_ERROR:
        raise HTTPException(status_code=_STATUS_BY_ERROR[error],
                            detail={"error": error, "message": _masked(report.get("message", ""))})
    if error is not None:
        raise _bad_gateway(f"{error}: {report.get('message', '')}")
    alarm = report.get("alarm_at")
    return {**report, "alarm_at_iso": _iso(alarm) if isinstance(alarm, (int, float)) else None}


def _default_client():
    from .. import aws_session
    return aws_session.lambda_client(read_timeout=LAMBDA_READ_TIMEOUT_S)


def place(req: PlaceClockRequest, *, now: Optional[float] = None,
          client_factory: Optional[Callable] = None, default_reader: Optional[Callable] = None) -> dict:
    """Preview (``dry_run``) or apply a clock placement or reset; see the module doc.

    ``alarm_in_minutes`` is resolved to ``alarm_at`` once, from ONE ``now``,
    before either path, so the Lambda ``place`` payload is unchanged.
    *now*, *client_factory* and *default_reader* (the reset dry run's stored
    default) are test seams.
    """
    function_name = os.environ.get(STORY_HISTORY_FUNCTION_ENV, "").strip()
    if not function_name:
        raise HTTPException(status_code=404, detail=NOT_LIVE_DETAIL)
    now = time.time() if now is None else now
    if req.reset and req.dry_run:
        return reset_preview(req, now, _stored_default(default_reader))
    req, requested = _resolved(req, now)
    if req.dry_run:
        return _echo(preview(req, now), requested)
    if not _PLACE_IN_FLIGHT.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"error": "in_flight", "message": IN_FLIGHT_DETAIL})
    try:
        client = (client_factory or _default_client)()
        return _echo(_mapped(_invoke(client, function_name, payload(req))), requested)
    finally:
        _PLACE_IN_FLIGHT.release()
