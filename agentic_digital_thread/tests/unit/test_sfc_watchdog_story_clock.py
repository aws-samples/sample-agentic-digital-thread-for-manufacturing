# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The watchdog's reading of a story-clock server (``lambda-sfc-watchdog/watchdog_clock.py``).

A server that follows the stored story anchor publishes nothing while it waits
for the anchor or is held until the first placement, and serves one row (so
nothing changes) before its anchor. The watchdog must not read either as a
stalled SFC. It reads the same SSM parameter the server polls and applies the
server's own rule, ported from ``highbyte/components/opc-server/clock.mjs``;
node evaluates that module here so the port cannot drift from it.
"""
from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv list, no shell
from pathlib import Path

import pytest

from tests.unit._sfc_watchdog_modules import load

ROOT = Path(__file__).resolve().parents[3]
CLOCK = ROOT / "highbyte" / "components" / "opc-server" / "clock.mjs"

sc = load("watchdog_clock")

ANCHOR = 1_790_000_010
ROWS = 5760
_needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_VALUES = [None, "", "pending", "garbage", "0", "-5", "12.5", str(ANCHOR), f"{ANCHOR}.0",
           f"{ANCHOR};healthy", f"{ANCHOR}.5;healthy", "0;healthy", "x;healthy",
           f"{ANCHOR};{ANCHOR - 300};100;200", f"{ANCHOR};{ANCHOR - 300};200;100",
           f"{ANCHOR};0;100;200", f"{ANCHOR};x;1;2", " 1790000010 ", "١٢٣"]


def _node(expression: str):
    script = f"import * as c from {json.dumps(CLOCK.as_uri())};\nconsole.log(JSON.stringify({expression}));"
    out = subprocess.run(["node", "--input-type=module", "-e", script],  # nosec B603 B607
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _js_clock(clock: dict) -> dict:
    """The port's clock in clock.mjs's own shape."""
    out = {"mode": clock["mode"]}
    if "anchor" in clock:
        out["anchor"] = clock["anchor"]
    if clock.get("catch_up"):
        out["catchUp"] = dict(clock["catch_up"])
    return out


@_needs_node
def test_parameter_parsing_matches_the_server():
    ported = [_js_clock(sc.clock_from_parameter(v)) for v in _VALUES]
    assert ported == _node(f"{json.dumps(_VALUES)}.map(v => c.clockFromParameter(v))")


@_needs_node
def test_publishing_matches_the_server():
    ported = [sc.publishes(sc.clock_from_parameter(v)) for v in _VALUES]
    assert ported == _node(f"{json.dumps(_VALUES)}.map(v => c.publishes(c.clockFromParameter(v)))")


@_needs_node
@pytest.mark.parametrize("loop_seconds", [3600, 4800, 28800])
def test_rows_match_the_server_across_the_loop_the_history_loop_and_a_catch_up(loop_seconds):
    values = [str(ANCHOR), "0", f"{ANCHOR};healthy",
              f"{ANCHOR};{ANCHOR - 7200};{ANCHOR + 600};{ANCHOR + 4200}",
              f"{ANCHOR};0;{ANCHOR - 300};{ANCHOR + 3000}",
              f"{ANCHOR};{ANCHOR + 9000};{ANCHOR};{ANCHOR + 600}"]          # a move back jumps
    probes = [ANCHOR + off for off in (-90_000, -86_400, -60_000, -3600, -1, 0, 2.5, 4.9, 5, 599, 600,
                                       2400.5, 4199, 4200, 19_490, 28_799, 28_800, 57_605)]
    opts = {"dataSize": ROWS, "intervalMs": 5000, "loopSeconds": loop_seconds}
    expected = _node(f"{json.dumps(values)}.map(v => {json.dumps(probes)}.map(t => "
                     f"c.rowFor(c.clockFromParameter(v), t * 1000, {json.dumps(opts)})))")
    ported = [[sc.row_for(sc.clock_from_parameter(v), t, rows=ROWS, interval_seconds=5,
                          loop_seconds=loop_seconds) for t in probes] for v in values]
    assert ported == expected


def _view(value, now, **kw):
    return sc.server_view(value, now, rows=ROWS, loop_seconds=kw.pop("loop_seconds", 28800),
                          window_seconds=300, **kw)


def test_a_waiting_or_held_server_publishes_nothing():
    for value in (None, "pending", "garbage", f"{ANCHOR};healthy"):
        view = _view(value, ANCHOR + 5000)
        assert view.publishing is False, value
        assert "no value" in view.reason
    assert "first placement" in _view(f"{ANCHOR};healthy", ANCHOR + 5000).reason


def test_a_wall_clock_or_running_anchor_publishes_and_moves():
    for value in ("0", str(ANCHOR)):
        view = _view(value, ANCHOR + 5000)
        assert view.publishing is True, value
        assert view.since is None


def test_before_its_anchor_the_server_serves_one_row_and_is_not_judged():
    """An anchor in the future (outside the history loop): every tag sits on
    the healthy row, so no value changes and SFC stores nothing new."""
    view = _view(str(ANCHOR), ANCHOR - 3600)
    assert view.publishing is False
    assert "one row" in view.reason


def test_a_placement_out_of_the_hold_starts_the_clock_at_the_catch_up_start():
    """Just placed (held -> climbing from the healthy row): values are expected
    from the catch-up start, so the watchdog's grace runs from there."""
    start = ANCHOR + 7000
    view = _view(f"{ANCHOR};0;{start};{start + 4800}", start + 60)
    assert view.publishing is True
    assert view.since == start


def test_a_slow_catch_up_that_moves_less_than_a_row_per_window_is_not_judged():
    start = ANCHOR + 7000
    value = f"{ANCHOR};{ANCHOR + 840};{start};{start + 28_000}"   # 8 rows over ~7.8 h
    assert sc.row_for(sc.clock_from_parameter(value), start + 28_000 - 1, rows=ROWS,
                      interval_seconds=5, loop_seconds=28800) == sc.row_for(
        sc.clock_from_parameter(value), start, rows=ROWS, interval_seconds=5, loop_seconds=28800) + 7
    view = _view(value, start + 10_000)
    assert view.publishing is False
    assert "one row" in view.reason


def test_an_unreadable_parameter_is_never_guessed():
    view = sc.unreadable("AccessDeniedException")
    assert view.publishing is False
    assert "AccessDeniedException" in view.reason


# --- the one-time history replay: the backfill parameter the story server polls

_HOUR = 3600
FROM_, TO_ = ANCHOR - 3 * 86400, ANCHOR - 600
WINDOW = f"{ANCHOR};{FROM_};{TO_};{ANCHOR + 50}"
_BACKFILL_VALUES = [None, "", "pending", "garbage", WINDOW, WINDOW + ";done", WINDOW + ";later",
                    f"{ANCHOR};{TO_};{FROM_};{ANCHOR}", f"{ANCHOR};{FROM_};{TO_};0", " " + WINDOW + " "]


@_needs_node
def test_backfill_parameter_parsing_matches_the_server():
    expected = _node(f"{json.dumps(_BACKFILL_VALUES)}.map(v => c.parseBackfill(v))")
    assert [sc.parse_backfill(v) for v in _BACKFILL_VALUES] == expected


@_needs_node
@pytest.mark.parametrize("window", [(FROM_, TO_), (ANCHOR - 86400 - 7, ANCHOR + 4 * _HOUR + 13),
                                    (ANCHOR - 600, ANCHOR - 300), (ANCHOR, ANCHOR + 1)])
def test_backfill_point_count_matches_the_servers_replay_order(window):
    frm, to = window
    expected = _node(f"c.backfillOrder({frm}, {to}, c.BACKFILL_STEP_SECONDS, {ANCHOR}, 28800)"
                     ".filter(e => e !== null).length")
    assert sc.backfill_points({"anchor": ANCHOR, "from": frm, "to": to}) == expected


@_needs_node
def test_replay_constants_match_the_server():
    expected = _node("[c.BACKFILL_STEP_SECONDS, c.BACKFILL_PACE_MS, c.BACKFILL_DONE_GRACE_MS, c.REPLAY_MAX_PASSES]")
    assert [sc.BACKFILL_STEP_SECONDS, sc.BACKFILL_PACE_MS, sc.BACKFILL_DONE_GRACE_MS,
            sc.REPLAY_MAX_PASSES] == expected


def _due(window: str = WINDOW, since: float = 0.0) -> float:
    return sc.replay_view(window, 0.0, since=since).due


def test_a_pending_or_malformed_backfill_is_no_replay():
    for value in (None, "pending", "garbage", WINDOW + ";later"):
        view = sc.replay_view(value, ANCHOR + 10 * _HOUR, since=0.0)
        assert view.status == "none" and view.key is None, value


def test_a_done_replay_is_done():
    view = sc.replay_view(WINDOW + ";done", ANCHOR + 10 * _HOUR, since=0.0)
    assert view.status == "done" and view.key == WINDOW


def test_an_active_replay_is_running_until_its_deadline_then_overdue():
    due = _due()
    assert sc.replay_view(WINDOW, due - 1, since=0.0).status == "running"
    overdue = sc.replay_view(WINDOW, due + 120, since=0.0)
    assert overdue.status == "overdue" and overdue.key == WINDOW
    assert "120 s" in overdue.reason


def test_the_deadline_is_two_clean_passes_plus_the_done_grace_after_start_up():
    points = sc.backfill_points(sc.parse_backfill(WINDOW))
    one_pass = (points + 2) * sc.BACKFILL_PACE_MS / 1000
    expected = (ANCHOR + 50 + sc.REPLAY_STARTUP_SECONDS + sc.REPLAY_PASSES_BEFORE_ACTING * one_pass
                + sc.BACKFILL_DONE_GRACE_MS / 1000)
    assert _due() == pytest.approx(expected)


def test_the_deadline_restarts_when_sfc_or_the_server_restarted_later():
    """A new SFC reader or a restarted server replays from point 0."""
    later = ANCHOR + 50 + 5 * _HOUR
    assert _due(since=later) == pytest.approx(_due() + 5 * _HOUR)
    assert _due(since=ANCHOR) == pytest.approx(_due())          # before the window: the window counts


def test_the_stopped_bound_is_every_failed_pass_plus_the_grace_after_start_up():
    """By then a replay whose every pass had a gap has reached the server's
    REPLAY_MAX_PASSES and stopped; only a reader after it revives the replay."""
    points = sc.backfill_points(sc.parse_backfill(WINDOW))
    one_pass = (points + 2) * sc.BACKFILL_PACE_MS / 1000
    view = sc.replay_view(WINDOW, 0.0, since=ANCHOR + 7200)
    assert view.stopped_by == pytest.approx(ANCHOR + 7200 + sc.REPLAY_STARTUP_SECONDS
                                            + sc.REPLAY_MAX_PASSES * one_pass + sc.BACKFILL_DONE_GRACE_MS / 1000)
    assert view.stopped_by - view.due == pytest.approx((sc.REPLAY_MAX_PASSES - sc.REPLAY_PASSES_BEFORE_ACTING)
                                                       * one_pass)
