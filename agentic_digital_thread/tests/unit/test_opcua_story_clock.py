# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The OPC UA server's playback row agrees with the simulator's condition model.

``highbyte/components/opc-server/clock.mjs`` picks the dataset row the server
serves. The live workshop story's mill follows the stored story anchor
(ADR-0092); every other server, and every demo or frozen deploy, follows the
wall clock (ADR-0073). Node evaluates the module here and every row is checked
against ``erp_simulation/condition_monitor.py``, the model the CMMS order and
the MES checks are raised from, so the sensor and the records cannot drift.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402

CLOCK = ROOT / "highbyte" / "components" / "opc-server" / "clock.mjs"
DATASET_ROWS = cm.DEFAULT_LOOP_SECONDS // cm.INTERVAL_SEC          # 5760, the generator's loop
ANCHOR = 1_790_000_010                                              # congruent: % 300 == 10

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _node(expression: str):
    """Evaluate ``expression`` (JSON-serialisable) with clock.mjs imported as ``c``."""
    script = f"import * as c from {json.dumps(CLOCK.as_uri())};\nconsole.log(JSON.stringify({expression}));"
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _phase(step: int, steps: int) -> str:
    """The condition model's phase at ``step`` of a loop of ``steps`` steps."""
    fail = int(steps * cm.FAIL_FRACTION)
    repair = min(cm.REPAIR_STEPS_CAP, steps // cm.REPAIR_LOOP_DIVISOR)
    if step < cm.bpfo_alarm_step((fail, repair)):
        return "degrading"
    if step < fail:
        return "bpfo_alarm"
    return "repair" if step < fail + repair else "healthy"


def test_wall_clock_row_is_the_condition_models_loop_step():
    epochs = [0, 4.9, 5, 28_799, 28_800, 1_790_123_457.25, ANCHOR + 19_490]
    rows = _node(f"{json.dumps(epochs)}.map(e => c.wallClockRow(e * 1000, {DATASET_ROWS}, 5000))")
    assert rows == [cm.loop_step(e, cm.DEFAULT_LOOP_SECONDS) for e in epochs]


def test_on_the_8h_loop_the_anchored_row_is_exactly_the_model_step():
    offsets = [0, 4, 5, 19_489, 19_490, 25_919, 25_920, 27_119, 27_120, 28_799, 28_800, 57_605]
    rows = _node(f"{json.dumps(offsets)}.map(o => c.storyRow(({ANCHOR} + o) * 1000, {ANCHOR}, 28800, {DATASET_ROWS}))")
    expected = [cm.loop_step(cm.story_seconds(ANCHOR + o, ANCHOR), cm.DEFAULT_LOOP_SECONDS) for o in offsets]
    assert rows == expected
    assert rows[4] == cm.BPFO_ALARM_STEP == 3898          # the alarm instant serves the alarm row


@pytest.mark.parametrize("loop_seconds", [3600, 4800, 7200, 14_400, 28_800])
def test_a_packaged_loop_plays_every_step_in_the_models_phase(loop_seconds):
    steps = loop_seconds // cm.INTERVAL_SEC
    rows = _node(f"Array.from({{length: {steps}}}, (_, k) => "
                 f"c.storyRow(({ANCHOR} + 5 * k + 2.5) * 1000, {ANCHOR}, {loop_seconds}, {DATASET_ROWS}))")
    mismatched = [k for k, row in enumerate(rows) if _phase(row, DATASET_ROWS) != _phase(k, steps)]
    assert mismatched == []
    assert rows == sorted(rows)                               # one pass through the dataset, no jumps back


def test_before_the_anchor_the_mill_is_healthy_except_in_the_history_loop():
    loop = 4800
    history = cm.loop_start_epoch(ANCHOR - 86_400, ANCHOR, loop)
    probes = {
        "an hour before the anchor": (ANCHOR - 3600, 0),
        "history loop start": (history, 0),
        "history loop at its alarm": (history + 0.75 * loop, None),
        "just after the history loop": (history + loop, 0),
    }
    rows = _node(f"{json.dumps([t for t, _ in probes.values()])}"
                 f".map(t => c.storyRow(t * 1000, {ANCHOR}, {loop}, {DATASET_ROWS}))")
    got = dict(zip(probes, rows))
    assert got["an hour before the anchor"] == 0
    assert got["history loop start"] == 0
    assert _phase(got["history loop at its alarm"], DATASET_ROWS) == "bpfo_alarm"
    assert got["just after the history loop"] == 0
    assert _node(f"c.historyLoopStart({ANCHOR}, {loop})") == history


def test_parameter_values_select_the_clock_and_never_guess_the_wall_clock():
    values = [None, "", "pending", "garbage", "0", "-5", str(ANCHOR), f"{ANCHOR}.0"]
    clocks = _node(f"{json.dumps(values)}.map(v => c.clockFromParameter(v))")
    assert [c["mode"] for c in clocks] == ["waiting", "waiting", "waiting", "waiting",
                                          "wall", "wall", "anchored", "anchored"]
    assert clocks[6]["anchor"] == ANCHOR
    waiting = _node(f"c.rowFor({{mode: 'waiting'}}, {ANCHOR + 20_000} * 1000, "
                    f"{{dataSize: {DATASET_ROWS}, intervalMs: 5000, loopSeconds: 28800}})")
    assert waiting == 0                                       # the healthy row, not the wall clock
    assert _node("c.publishes({mode: 'waiting'})") is False   # ...and nothing published (decision 8)


def test_loop_length_validation_matches_the_simulators():
    good = _node("[undefined, '', '3600', '4800', '28800'].map(v => c.loopSecondsFrom(v))")
    assert good == [cm.loop_seconds_from(v) for v in (None, "", "3600", "4800", "28800")]
    for bad in ("3000", "3700", "30000", "abc", "4800.0"):
        with pytest.raises(subprocess.CalledProcessError):
            _node(f"c.loopSecondsFrom({json.dumps(bad)})")
        with pytest.raises(ValueError):
            cm.loop_seconds_from(bad)


def _catch_up_rows(old: int, new: int, placed: int, probes, loop_seconds: int = 28800):
    """Rows the server serves at ``probes`` (epochs) after a placement at
    ``placed`` moved the anchor ``old`` -> ``new``, with the parameter value
    the mirror writes for it."""
    from erp_simulation import story_anchor_mirror as mirror
    from erp_simulation import story_replan as sr
    start, end = sr.catch_up_window(new, placed)
    value = mirror.mirror_value("present", new, (old, start, end), now=placed)
    assert value == f"{new};{old};{start};{end}"
    rows = _node(f"(() => {{ const k = c.clockFromParameter({json.dumps(value)}); "
                 f"return {json.dumps(list(probes))}.map(t => c.rowFor(k, t * 1000, "
                 f"{{dataSize: {DATASET_ROWS}, intervalMs: 5000, loopSeconds: {loop_seconds}}})); }})()")
    return rows, start, end


def test_after_a_placement_the_mill_climbs_to_the_new_alarm_instead_of_stepping():
    """First sign-in: the deploy anchor is 90 min old, the placement puts the
    alarm 80 min out (4 h 05 min into the wear). The server keeps the deploy
    row until the catch-up starts, climbs without a jump back or a step, and
    serves exactly the condition model's alarm row at the alarm instant and
    the model's row after it, so the CMMS order and the MES checks agree."""
    from erp_simulation import story_replan as sr
    deploy = ANCHOR
    placed = deploy + 90 * 60
    new = sr.normalize_anchor(sr.alarm_to_anchor(placed + 80 * 60), placed)
    probes = list(range(placed - 300, placed + 120 * 60, 15))
    rows, start, end = _catch_up_rows(deploy, new, placed, probes)
    by_t = dict(zip(probes, rows))
    model = lambda t, a: cm.loop_step(cm.story_seconds(t, a), cm.DEFAULT_LOOP_SECONDS)  # noqa: E731
    assert all(by_t[t] == model(t, deploy) for t in probes if t < start)
    assert rows == sorted(rows)                                   # never back
    climb = [b - a for a, b in zip(rows, rows[1:])]
    assert max(climb) <= 15                                       # 15 s probes: no step
    assert end == new + cm.ALARM_OFFSET_SECONDS
    assert all(by_t[t] == model(t, new) for t in probes if t >= end)
    assert model(end, new) == cm.BPFO_ALARM_STEP


def test_a_move_back_jumps_and_a_finished_catch_up_is_the_plain_anchor():
    from erp_simulation import story_anchor_mirror as mirror
    from erp_simulation import story_replan as sr
    placed = ANCHOR + 6 * 3600                                    # past the old alarm: a reset back
    new = sr.normalize_anchor(ANCHOR + 3600, placed)              # 5 h into its loop
    probes = [placed + 600, placed + 3600]
    rows, start, _end = _catch_up_rows(ANCHOR, new, placed, probes)
    assert rows == [cm.loop_step(cm.story_seconds(t, new), cm.DEFAULT_LOOP_SECONDS) for t in probes]
    window = sr.catch_up_window(new, placed)
    assert mirror.mirror_value("present", new, (ANCHOR, *window), now=window[1]) == str(new)


def test_catch_up_parameter_values_parse_or_fall_back_to_waiting():
    values = [f"{ANCHOR};{ANCHOR - 300};100;200", f"{ANCHOR};{ANCHOR - 300};200;100", f"{ANCHOR};x;1;2",
              f"{ANCHOR};0;100;200"]
    clocks = _node(f"{json.dumps(values)}.map(v => c.clockFromParameter(v))")
    assert clocks[0] == {"mode": "anchored", "anchor": ANCHOR,
                         "catchUp": {"from": ANCHOR - 300, "start": 100, "end": 200}}
    assert [c["mode"] for c in clocks[1:3]] == ["waiting", "waiting"]
    assert clocks[3] == {"mode": "anchored", "anchor": ANCHOR, "catchUp": {"from": 0, "start": 100, "end": 200}}


# ---------------------------------------------------------------------------
# ADR-0092 decision 8: an account whose package auto-places on the first
# sign-in publishes no value until it is placed, and the first placement
# climbs from the healthy row.
# ---------------------------------------------------------------------------

def test_the_held_value_and_the_healthy_catch_up_are_the_mirrors():
    from erp_simulation import story_anchor_mirror as mirror
    assert _node("[c.HELD_SUFFIX, c.HEALTHY_FROM]") == [mirror.HELD_SUFFIX, mirror.HEALTHY_FROM]


def test_a_held_parameter_publishes_no_value():
    values = [f"{ANCHOR};healthy", f"{ANCHOR}.5;healthy", "0;healthy", "x;healthy", f"{ANCHOR};healthy;1"]
    clocks = _node(f"{json.dumps(values)}.map(v => c.clockFromParameter(v))")
    assert clocks[:2] == [{"mode": "held", "anchor": ANCHOR}, {"mode": "held", "anchor": ANCHOR + 0.5}]
    assert [c["mode"] for c in clocks[2:]] == ["waiting", "waiting", "waiting"]
    # nothing published, at what would be the anchored clock's alarm and its failure; row 0 is
    # the healthy row a catch-up climbs from
    probes = [ANCHOR + cm.ALARM_OFFSET_SECONDS, ANCHOR + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC + 60]
    rows = _node(f"{json.dumps(probes)}.map(t => c.rowFor(c.clockFromParameter({json.dumps(values[0])}), "
                 f"t * 1000, {{dataSize: {DATASET_ROWS}, intervalMs: 5000, loopSeconds: 28800}}))")
    assert rows == [0, 0]
    assert "held until the first placement" in _node(
        f"c.describeClock(c.clockFromParameter({json.dumps(values[0])}))")


def test_only_an_anchored_or_wall_clock_publishes_a_value():
    values = [None, "pending", "garbage", f"{ANCHOR};healthy", "0", str(ANCHOR),
              f"{ANCHOR};0;100;200", f"{ANCHOR};{ANCHOR - 300};100;200"]
    published = _node(f"{json.dumps(values)}.map(v => c.publishes(c.clockFromParameter(v)))")
    assert published == [False, False, False, False, True, True, True, True]


def _held_account_rows(up_seconds: float, probes_after_sign_in):
    """Rows a held account serves (the deploy anchor's era, then the first
    placement at sign-in) at sign-in + each probe, through the mirror."""
    from erp_simulation import story_anchor_mirror as mirror
    from erp_simulation import story_replan as sr
    deploy = ANCHOR
    placed = deploy + up_seconds
    new = sr.normalize_anchor(sr.alarm_to_anchor(placed + 80 * 60), placed)
    start, end = sr.catch_up_window(new, placed)
    held = mirror.mirror_value("present", deploy, source="deploy", hold=True)
    after = mirror.mirror_value("present", new, (deploy, start, end, "deploy"), now=placed,
                                source="placed", hold=True)
    assert (held, after) == (f"{deploy};healthy", f"{new};0;{start};{end}")
    script = (f"(() => {{ const held = c.clockFromParameter({json.dumps(held)}); "
              f"const after = c.clockFromParameter({json.dumps(after)}); "
              f"const opts = {{dataSize: {DATASET_ROWS}, intervalMs: 5000, loopSeconds: 28800}}; "
              f"return {{published: [c.publishes(held), c.publishes(after)], "
              f"before: {json.dumps([deploy + k * 60 for k in range(0, int(up_seconds // 60))])}"
              f".map(t => c.rowFor(held, t * 1000, opts)), "
              f"after: {json.dumps([placed + p for p in probes_after_sign_in])}"
              f".map(t => c.rowFor(after, t * 1000, opts))}}; }})()")
    return _node(script), new, start, end


@pytest.mark.parametrize("up_seconds", [30 * 60, 6 * 3600, 24 * 3600])
def test_a_held_account_publishes_nothing_until_placed_then_climbs_without_a_step(up_seconds):
    probes = list(range(0, 2 * 3600, 5))
    got, new, start, end = _held_account_rows(up_seconds, probes)
    assert got["published"] == [False, True]                     # SiteWise gets nothing before sign-in
    assert set(got["before"]) == {0}                              # the row a catch-up climbs from
    rows = got["after"]
    assert rows == sorted(rows)                                   # never back
    by_t = dict(zip(probes, rows))
    placed = ANCHOR + up_seconds
    assert all(by_t[p] == 0 for p in probes if placed + p < start)
    assert max(b - a for a, b in zip(rows, rows[1:])) <= 5        # at most a few dataset rows per 5 s
    model = lambda t: cm.loop_step(cm.story_seconds(t, new), cm.DEFAULT_LOOP_SECONDS)  # noqa: E731
    assert all(by_t[p] == model(placed + p) for p in probes if placed + p >= end)
    assert model(end) == cm.BPFO_ALARM_STEP


def test_without_auto_place_an_unplaced_account_follows_its_anchor():
    from erp_simulation import story_anchor_mirror as mirror
    for source in ("deploy", "packaged"):
        assert mirror.mirror_value("present", ANCHOR, source=source, hold=False) == str(ANCHOR)
    alarm = ANCHOR + cm.ALARM_OFFSET_SECONDS
    row = _node(f"c.rowFor(c.clockFromParameter({json.dumps(str(ANCHOR))}), {alarm} * 1000, "
                f"{{dataSize: {DATASET_ROWS}, intervalMs: 5000, loopSeconds: 28800}})")
    assert row == cm.BPFO_ALARM_STEP
