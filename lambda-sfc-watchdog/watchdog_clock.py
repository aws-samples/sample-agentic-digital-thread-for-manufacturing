# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How a story-clock OPC UA server behaves right now, read from its parameter.

A server configured with a story anchor parameter plays its dataset from the
anchor that parameter carries, not from the wall clock. While it waits for
the parameter, or is held until the first placement, it publishes no value
at all; before its anchor it serves one row, so no value changes. In each of
those states SFC stores nothing new for that source, and the watchdog must
not read that as a stall.

This is a port of the server's own rule in
``highbyte/components/opc-server/clock.mjs`` (``clockFromParameter``,
``publishes``, ``rowFor`` and the functions they call). The unit suite
evaluates that module with node and compares, so the two cannot drift.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

MODEL_INTERVAL_SECONDS = 5          # clock.mjs MODEL_INTERVAL_SECONDS
HISTORY_OFFSET_SECONDS = 86400      # clock.mjs HISTORY_OFFSET_SECONDS
PENDING = "pending"                 # clock.mjs PENDING
HEALTHY_FROM = 0                    # clock.mjs HEALTHY_FROM

# clock.mjs NUMBER, CATCH_UP and HELD, with JavaScript's ASCII-only \d.
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_CATCH_UP = re.compile(r"([0-9]+(?:\.[0-9]+)?);([0-9]+);([0-9]+);([0-9]+)")
_HELD = re.compile(r"([0-9]+(?:\.[0-9]+)?);healthy")


def _num(text: str) -> float | int:
    """JavaScript ``Number`` of a matched decimal: an int when it is whole."""
    value = float(text)
    return int(value) if value.is_integer() else value


def clock_from_parameter(raw: Optional[str]) -> dict:
    """The clock a parameter value selects (clock.mjs ``clockFromParameter``)."""
    text = "" if raw is None else str(raw).strip()
    match = _CATCH_UP.fullmatch(text)
    if match:
        anchor, frm, start, end = (_num(g) for g in match.groups())
        if anchor > 0 and frm >= HEALTHY_FROM and start < end:
            return {"mode": "anchored", "anchor": anchor,
                    "catch_up": {"from": frm, "start": start, "end": end}}
        return {"mode": "waiting"}
    match = _HELD.fullmatch(text)
    if match:
        anchor = _num(match.group(1))
        return {"mode": "held", "anchor": anchor} if anchor > 0 else {"mode": "waiting"}
    if text in ("", PENDING) or not _NUMBER.fullmatch(text):
        return {"mode": "waiting"}
    anchor = _num(text)
    return {"mode": "anchored", "anchor": anchor} if anchor > 0 else {"mode": "wall"}


def publishes(clock: dict) -> bool:
    """Whether the server publishes a value at all (clock.mjs ``publishes``)."""
    return clock["mode"] in ("anchored", "wall")


def _history_loop_start(anchor: float, loop_seconds: int) -> float:
    return anchor + math.floor(-HISTORY_OFFSET_SECONDS / loop_seconds) * loop_seconds


def _story_row(now: float, anchor: float, loop_seconds: int, rows: int) -> int:
    if not rows:
        return 0
    history = _history_loop_start(anchor, loop_seconds)
    if now < anchor and not (history <= now < history + loop_seconds):
        return 0
    position = (((now - anchor) % loop_seconds) + loop_seconds) % loop_seconds
    steps = math.floor(loop_seconds / MODEL_INTERVAL_SECONDS)
    step = math.floor(position / MODEL_INTERVAL_SECONDS)
    return min(rows - 1, math.floor((step * rows) / steps))


def _catch_up_row(now: float, clock: dict, loop_seconds: int, rows: int) -> Optional[int]:
    catch_up = clock["catch_up"]
    frm, start, end = catch_up["from"], catch_up["start"], catch_up["end"]
    if now >= end:
        return None
    from_row = 0 if frm == HEALTHY_FROM else _story_row(min(now, start), frm, loop_seconds, rows)
    if now < start:
        return from_row
    target = _story_row(end, clock["anchor"], loop_seconds, rows)
    if from_row > target:
        return None
    return from_row + math.floor(((target - from_row) * (now - start)) / (end - start))


def row_for(clock: dict, now: float, *, rows: int, interval_seconds: float, loop_seconds: int) -> int:
    """The dataset row the server serves at ``now`` (clock.mjs ``rowFor``)."""
    if clock["mode"] == "anchored":
        caught = _catch_up_row(now, clock, loop_seconds, rows) if clock.get("catch_up") else None
        return caught if caught is not None else _story_row(now, clock["anchor"], loop_seconds, rows)
    if clock["mode"] in ("waiting", "held"):
        return 0
    if not rows:
        return 0
    interval_ms = interval_seconds * 1000
    now_ms = now * 1000
    return math.floor((now_ms % (rows * interval_ms)) / interval_ms)


@dataclass(frozen=True)
class ServerView:
    """What the watchdog may expect from the server now.

    ``publishing`` is False when the server sends no new values by design;
    ``since`` is when it started sending them again after a placement (the
    catch-up start), or None when nothing changed recently."""

    publishing: bool
    reason: str = ""
    since: Optional[float] = None


def server_view(raw: Optional[str], now: float, *, rows: int, loop_seconds: int,
                window_seconds: float, interval_seconds: float = MODEL_INTERVAL_SECONDS) -> ServerView:
    """Read the parameter the way the server does and say whether it is
    publishing values that change within the last ``window_seconds``."""
    clock = clock_from_parameter(raw)
    if not publishes(clock):
        why = ("held until the first placement" if clock["mode"] == "held"
               else "waiting for its story clock")
        return ServerView(False, f"the server publishes no value ({why})")
    opts = {"rows": rows, "interval_seconds": interval_seconds, "loop_seconds": loop_seconds}
    if row_for(clock, now, **opts) == row_for(clock, now - window_seconds, **opts):
        return ServerView(False, f"the server has served one row for the last {int(window_seconds)} s")
    catch_up = clock.get("catch_up")
    return ServerView(True, since=float(catch_up["start"]) if catch_up else None)


def unreadable(error: str) -> ServerView:
    """The parameter could not be read: never guess what the server serves."""
    return ServerView(False, f"cannot read the story clock parameter ({error})")


# ---------------------------------------------------------------------------
# The one-time history replay (clock.mjs parseBackfill and backfillOrder).
#
# The story server replays a history window once, on separate history nodes
# that SFC reads through its own source. Its progress is not visible in any
# live sentinel, so the watchdog reads the window parameter the server polls:
# "pending" (no window), "<anchor>;<from>;<to>;<id>" (to replay) or the same
# with ";done" (the server wrote it once SFC took every value). A window that
# is still not done well after a clean replay would have finished is overdue.
# ---------------------------------------------------------------------------

BACKFILL_STEP_SECONDS = 300          # clock.mjs BACKFILL_STEP_SECONDS: one point per grid slot
BACKFILL_PACE_MS = 2000              # clock.mjs BACKFILL_PACE_MS: wall time per replayed point
BACKFILL_DONE_GRACE_MS = 300000      # clock.mjs BACKFILL_DONE_GRACE_MS: before ";done" is written
REPLAY_MAX_PASSES = 5                # clock.mjs REPLAY_MAX_PASSES
#: The watchdog's own allowance before a replay is due: SFC's start, the
#: server's 30 s poll of the window and the simulator's mirror cycle.
REPLAY_STARTUP_SECONDS = 600
#: Clean passes a replay may take before it counts as overdue: one pass with a
#: gap is the server's own recovery, so it is given a second one.
REPLAY_PASSES_BEFORE_ACTING = 2

_BACKFILL = re.compile(r"([0-9]+);([0-9]+);([0-9]+);([0-9]+)(;done)?")


def parse_backfill(raw: Optional[str]) -> Optional[dict]:
    """clock.mjs ``parseBackfill``: the window, or None (pending, malformed)."""
    text = "" if raw is None else str(raw).strip()
    match = _BACKFILL.fullmatch(text)
    if not match:
        return None
    anchor, frm, to, ident = (int(g) for g in match.groups()[:4])
    if not (anchor > 0 and frm < to and ident > 0):
        return None
    return {"anchor": anchor, "from": frm, "to": to, "id": ident,
            "key": f"{anchor};{frm};{to};{ident}", "done": match.group(5) is not None}


def backfill_points(bf: dict) -> int:
    """How many grid points the replay serves (clock.mjs gridEpochs over
    ``[from, to)``, on the anchor's grid residue)."""
    step = BACKFILL_STEP_SECONDS
    residue = bf["anchor"] % step
    first = math.ceil((bf["from"] - residue) / step) * step + residue
    return max(0, math.ceil((bf["to"] - first) / step))


@dataclass(frozen=True)
class ReplayView:
    """``status``: ``none`` (no window), ``done``, ``running`` (not due yet) or
    ``overdue``. ``due`` is when a running replay is overdue (two clean passes
    could have finished); ``stopped_by`` is when a replay whose every pass had
    a gap must have reached ``REPLAY_MAX_PASSES`` and been stopped by the
    server, which revives it once for a reader that arrives after that."""

    status: str
    key: Optional[str] = None
    due: Optional[float] = None
    reason: str = ""
    stopped_by: Optional[float] = None


def replay_view(raw: Optional[str], now: float, *, since: float) -> ReplayView:
    """Read the window parameter. ``since`` is the latest restart that makes
    the server replay from point 0 (its own task or SFC's); the replay is due
    two clean passes plus the done grace after the later of it and the
    window's first publication (``id``), plus the start-up allowance, and
    stopped by every pass plus the same allowances."""
    bf = parse_backfill(raw)
    if bf is None:
        return ReplayView("none", reason="no replay window")
    if bf["done"]:
        return ReplayView("done", key=bf["key"], reason="the replay is done")
    one_pass = (backfill_points(bf) + 2) * BACKFILL_PACE_MS / 1000
    start = max(bf["id"], since) + REPLAY_STARTUP_SECONDS + BACKFILL_DONE_GRACE_MS / 1000
    due = start + REPLAY_PASSES_BEFORE_ACTING * one_pass
    stopped_by = start + REPLAY_MAX_PASSES * one_pass
    if now <= due:
        return ReplayView("running", key=bf["key"], due=due, stopped_by=stopped_by,
                          reason=f"the replay is due in {round(due - now)} s")
    return ReplayView("overdue", key=bf["key"], due=due, stopped_by=stopped_by,
                      reason=f"the replay is not done {round(now - due)} s after it was due")
