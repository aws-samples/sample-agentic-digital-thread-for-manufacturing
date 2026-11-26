# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The OPC UA server's one-time history replay agrees with the condition model,
survives SFC's change filter, and reaches every point.

On the first placement out of the HOLD the story site's OPC UA server replays
the mill's history window once, on SEPARATE history nodes, paced so each point
survives SFC's read, each carrying its grid epoch as the SourceTimestamp. The
pure rule lives in ``highbyte/components/opc-server/clock.mjs``
(``parseBackfill``, ``backfillOrder``, ``backfillRow``, the per-node owed
values ``backfillChangedNodes`` / ``backfillExpected``, ``hasHistoryTwin``,
``deliveredValues`` and the replay state machine ``replayOnFact`` /
``replayOnReader`` / ``replayOnReaderGone`` / ``replayPoint`` /
``replayOnDelivered`` / ``replayTick``); node evaluates it and every row is pinned against
``erp_simulation.condition_monitor`` and ``erp_simulation.story_history`` so
the replayed sensor, the chart, the CMMS order and the MES/CMMS history cannot
drift. See ADR-0092 (the history replay amendment).
"""
import functools
import gzip
import importlib.util
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import story_history as sh  # noqa: E402

CLOCK = ROOT / "highbyte" / "components" / "opc-server" / "clock.mjs"
DATASET_ROWS = cm.DEFAULT_LOOP_SECONDS // cm.INTERVAL_SEC          # 5760
ANCHOR = 1_790_000_010                                             # congruent: % 300 == 10
GRID = cm.TELEMETRY_GRID_SECONDS                                   # 300
LOOP = cm.DEFAULT_LOOP_SECONDS
FROM, TO = ANCHOR - 3 * sh.DAY, ANCHOR - 600                       # a window ending before H
OPTS = f"{{loopSeconds: {LOOP}, dataSize: {DATASET_ROWS}}}"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _node(expression: str):
    script = f"import * as c from {json.dumps(CLOCK.as_uri())};\nconsole.log(JSON.stringify({expression}));"
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _order(frm=FROM, to=TO, anchor=ANCHOR):
    return _node(f"c.backfillOrder({frm}, {to}, {GRID}, {anchor}, {LOOP})")


def _history_loop_bounds(anchor, loop_seconds=LOOP):
    start = cm.loop_start_epoch(anchor - sh.DAY, anchor, loop_seconds)
    return start, start + loop_seconds


# ---------------------------------------------------------------------------
# Grammar: "<H>;<from>;<to>;<id>[;done]", else null; "pending" is null.
# ---------------------------------------------------------------------------

def test_backfill_parameter_parses_or_is_null():
    good = f"{ANCHOR};{FROM};{TO};{ANCHOR + 50}"
    assert _node(f"c.parseBackfill({json.dumps(good)})") == {
        "anchor": ANCHOR, "from": FROM, "to": TO, "id": ANCHOR + 50, "key": good, "done": False}
    done = _node(f"c.parseBackfill({json.dumps(good + ';done')})")
    assert done["key"] == good and done["done"] is True
    assert _node(f"c.backfillDoneValue(c.parseBackfill({json.dumps(good)}))") == good + ";done"
    for bad in [None, "", "pending", "garbage", f"{ANCHOR};{ANCHOR}", f"{good};later",
                f"{ANCHOR};{ANCHOR};{ANCHOR};x", f"{ANCHOR};{TO};{FROM};{ANCHOR}"]:
        assert _node(f"c.parseBackfill({json.dumps(bad)})") is None, bad


def test_constants_match_the_grid_and_the_window():
    got = _node("[c.BACKFILL_STEP_SECONDS, c.HISTORY_OFFSET_SECONDS]")
    assert got == [GRID, sh.DAY]


# ---------------------------------------------------------------------------
# Replay order: the history loop first, then the rest, EACH oldest to newest,
# with one Bad (null) step between them. Every epoch is on the anchor's grid
# and inside [from, to).
# ---------------------------------------------------------------------------

def test_replay_order_is_history_loop_first_then_the_rest_each_oldest_first():
    order = _order()
    epochs = [e for e in order if e is not None]
    assert all(FROM <= e < TO and e % GRID == ANCHOR % GRID for e in epochs)
    assert len(epochs) == len(set(epochs)) == (TO - FROM) // GRID
    h_start, h_end = _history_loop_bounds(ANCHOR)
    loop = [e for e in epochs if h_start <= e < h_end]
    assert order.count(None) == 1 and order.index(None) == len(loop), "one separator, after the loop"
    assert order[:len(loop)] == sorted(loop)
    rest = order[len(loop) + 1:]
    assert rest == sorted(rest) and not any(h_start <= e < h_end for e in rest)


def test_backfill_rows_match_the_condition_model():
    order = _order()
    rows = _node(f"{json.dumps(order)}.map((e, k) => e === null ? null "
                 f": c.backfillRow(e, k, {ANCHOR}, {LOOP}, {DATASET_ROWS}))")
    h_start, h_end = _history_loop_bounds(ANCHOR)
    healthy_rows = set(range(cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS, cm.NUM_STEPS))
    for e, row in zip(order, rows):
        if e is None:
            continue
        if h_start <= e < h_end:
            assert row == cm.loop_step(cm.story_seconds(e, ANCHOR), LOOP)
        else:
            assert row in healthy_rows, (e, row)


def test_the_failed_batch_window_is_covered_and_shows_the_alarm():
    """The seeded failed batch (story_history.FAILED_OFFSET inside the BPFO
    window of the history loop) is inside the replayed window, and the BPFO
    band is in alarm across it, so module 5's root-cause finds the vibration."""
    h_start, _h_end = _history_loop_bounds(ANCHOR)
    alarm_epoch = cm.alarm_epoch(h_start, ANCHOR)
    assert FROM <= alarm_epoch < TO
    row = _node(f"c.storyRow({alarm_epoch} * 1000, {ANCHOR}, {LOOP}, {DATASET_ROWS})")
    assert cm.phase(row) in ("bpfo_alarm", "repair")


# ---------------------------------------------------------------------------
# What SFC stores. A model of the path from a history node to SiteWise, from
# the pinned sources rather than from memory:
#   * SFC v1.11.0 adapters/opcua/.../OpcuaSource.kt:748-749 subscribes each
#     node with sampling interval -1 (the publishing interval), queue size 1,
#     discardOldest, and no DataChangeFilter unless the channel sets a
#     NodeChangeFilter (:756-757; ours never does);
#   * with no filter the server's default applies: node-opcua 2.178
#     node-opcua-server/source/monitored_item.ts:286-290 reports a sample only
#     when its StatusCode or value differs from the last one it REPORTED
#     (:1337 keeps that one), i.e. the OPC UA StatusValue trigger;
#   * SFC keeps only Good values, with the SourceTimestamp
#     (OpcuaSource.kt:875-881), and the "Channel" schedule writes that time.
# A replay step holds one value for a whole pace (2 s) against a 1 s
# publishing interval, so the queue of one drops nothing at steady state: the
# filter is what decides which points SiteWise gets, and SiteWise then holds
# each value until the next stored point (step semantics).
# ---------------------------------------------------------------------------

def _stored(steps):
    """``steps`` = [(epoch or None, value)] in emission order (None: a Bad
    step) -> the (epoch, value) points SFC stores, sorted by timestamp."""
    last, stored = ("bad", None), []
    for epoch, value in steps:
        sample = ("bad", None) if epoch is None else ("good", value)
        if sample != last:
            last = sample
            if epoch is not None:
                stored.append((epoch, value))
    return sorted(stored)


def _value_at(stored, t):
    before = [v for e, v in stored if e <= t]
    return before[-1] if before else None


def _alarm_steps(order, anchor=ANCHOR):
    """The dataset's BPFO_Alarm at each replay step (row as clock.mjs serves it)."""
    h_start, h_end = _history_loop_bounds(anchor)
    steps = []
    for epoch in order:
        if epoch is None:
            steps.append((None, None))
        elif h_start <= epoch < h_end:
            row = cm.loop_step(cm.story_seconds(epoch, anchor), LOOP)
            steps.append((epoch, bool(cm.spectral_features(row)["bpfo_alarm"])))
        else:
            steps.append((epoch, False))    # healthy post-repair rows never alarm
    return steps


def test_bpfo_alarm_reaches_sitewise_at_its_real_leading_edges():
    """Through SFC's change filter, SiteWise holds BPFO_Alarm true at every
    seeded failed-batch check and false again from the bearing failure."""
    stored = _stored(_alarm_steps(_order()))
    win = sh.story_windows(ANCHOR)
    start = win["alarm"] + sh.FAILED_OFFSET
    for offset in sh.FAILED_CHECK_OFFSETS:
        assert _value_at(stored, start + offset) is True, offset
    first_after_fail = next(e for e in _order() if e is not None and e >= win["fail"])
    assert _value_at(stored, first_after_fail) is False
    assert _value_at(stored, win["alarm"] - GRID) is False, "not in alarm before the alarm"


def test_every_channel_has_a_point_at_the_start_of_each_segment():
    """A Boolean that is false at the end of the history loop and false at the
    start of the rest still gets a point there (the Bad separator step)."""
    order = _order()
    stored = _stored(_alarm_steps(order))
    first_rest = order[order.index(None) + 1]
    assert (first_rest, False) in stored
    assert stored[0][0] == min(e for e in order if e is not None)


# ---------------------------------------------------------------------------
# Pacing and completion. The replay starts when the server has seen the window
# AND a reader (an OPC UA subscription) monitors a history node; a NEW reader
# starts a new pass from point 0. ';done' needs EVIDENCE, item by item: the
# reader must hold a monitored item on every history node, and every value
# that can reach SFC through an item (a node whose own value at a step equals
# its value at the step before is dropped by that item's change filter, by
# design) must have been SENT through that item in a PublishResponse during
# one pass; then a grace with the reader still subscribed. A pass that ends
# with gaps starts over; passes are bounded.
# _simulate drives the pure state machine second by second over the real
# dataset columns of a few story-mill leaves: a 30 s poll, a 1 s tick, and a
# 1 s sampler per monitored item that reports a value only when it differs
# from the last one that item reported (the StatusValue trigger, Bad counts
# as a value), and "sends" it while the reader receives.
# Events: subscribe:<g> (a new subscription with fresh items), drop / resume
# (the connection stops / restarts carrying notifications; the subscription
# stays), gone (the current subscription's monitored items are removed),
# stall:<s> (event loop blocked), serverRestart (state lost).
# Reader items are [clientHandle, history node]; SFC reads a Boolean through
# two channels (the value and its __num twin), so the default reader holds
# two items on BPFO_Alarm, as SFC's history source does.
# ---------------------------------------------------------------------------

SIM_LEAVES = ("Spectrum_BPFO", "BPFO_Alarm", "Status", "Vibration_X")
READER = [[1, "Spectrum_BPFO"], [2, "BPFO_Alarm"], [3, "BPFO_Alarm"], [4, "Status"], [5, "Vibration_X"]]
ALARM_HANDLES = (2, 3)


GENERATOR = ROOT / "highbyte" / "generate_datasets.py"
_GENERATED = tempfile.TemporaryDirectory(prefix="adt-story-mill-")  # removed at interpreter exit


@functools.lru_cache(maxsize=1)
def _mill():
    """(a dataset file holding the story mill's line, the story mill's leaf alias
    prefix). The prefix comes from the SFC generator's own story-mill channels.

    The site datasets are gitignored and only ``deploy.sh`` generates them, so
    a CI checkout has none. The file is written here by the dataset generator
    itself (its own ``generate_site_csv`` and CSV formatting), for the one line
    that carries the story mill, in about a second. Every story-mill column
    that follows the condition model (the spectrum, the alarm, the vibration
    and coolant gauges, the status) is deterministic and matches the deployed
    file value for value; the noise columns vary like the deployed ones but
    draw different random values, because the generator seeds its RNG once and
    the deployed run spends it on the other sites first. These tests read the
    values they feed the server from this same file, so they never depend on a
    particular draw. The global RNG is restored afterwards: resolving the SFC
    channels and importing the generator both reseed it."""
    rng = random.getstate()
    try:
        from agents.demo import sfc_config
        ch = sfc_config._story_mill_channels(sfc_config.resolve_channels())[0]
        prefix = f"{ch.alias.rsplit('/', 1)[0]}/{ch.node}_"
        spec = importlib.util.spec_from_file_location("generate_datasets_replay_under_test", str(GENERATOR))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        assert gen.NUM_STEPS == DATASET_ROWS, (gen.NUM_STEPS, DATASET_ROWS)
        lines = [(tags, sched) for tags, sched in gen.machining_site_lines()
                 if any(alias.startswith(prefix) for alias in tags)]
        assert len(lines) == 1, f"exactly one line carries {prefix}"
        gen.OUTPUT_DIR = Path(_GENERATED.name)
        gen.generate_site_csv("story_mill_line.csv.gz", lines)
    finally:
        random.setstate(rng)
    return Path(_GENERATED.name) / "story_mill_line.csv.gz", prefix


@functools.lru_cache(maxsize=1)
def _mill_columns():
    """{leaf: (DATA_TYPE, [values as server.mjs parses them])} for the story mill."""
    parse = {"DOUBLE": float, "INTEGER": int, "BOOLEAN": lambda v: v == "True", "STRING": str}
    dataset, prefix = _mill()
    cols: dict[str, tuple[str, list]] = {}
    with gzip.open(dataset, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith(prefix):
                continue
            alias, kind, _s, _n, _q, raw = line.rstrip("\r\n").split(",", 5)
            cols.setdefault(alias[len(prefix):], (kind, []))[1].append(parse[kind](raw))
    return cols


def _node_with(expression: str, payload):
    """Evaluate ``expression`` with ``P`` bound to ``payload`` (sent on stdin)."""
    script = (f"import * as c from {json.dumps(CLOCK.as_uri())};\nimport {{ readFileSync }} from 'node:fs';\n"
              f"const P = JSON.parse(readFileSync(0, 'utf8'));\nconsole.log(JSON.stringify({expression}));")
    out = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=120, check=True)
    return json.loads(out.stdout)


_SIMULATE = r"""(() => {
    const bf = c.parseBackfill(P.value); const t0 = P.t0 * 1000; const BAD = "\u0000bad";
    const columns = Object.entries(P.columns); const nodes = columns.map(([n]) => n);
    const opts = { loopSeconds: P.loop, dataSize: P.dataSize };
    const order = c.backfillOrder(bf.from, bf.to, P.grid, bf.anchor, P.loop);
    const pick = (arr, row) => (row >= arr.length ? arr[0] : arr[row]);
    const vals = Object.fromEntries(columns.map(([n, arr]) => [n, order.map((e, k) =>
        e === null ? null : pick(arr, c.backfillRow(e, k, bf.anchor, P.loop, P.dataSize)))]));
    const silent = new Set(P.silent);
    for (const k of [...silent].sort((a, b) => a - b)) for (const n of nodes) vals[n][k] = vals[n][k - 1];
    const changed = (k) => nodes.filter((n) => vals[n][k] !== vals[n][k - 1]);
    const factOpts = { ...opts, nodes, changed };
    let state = c.REPLAY_IDLE, group = P.group, receiving = group !== null, stalledUntil = -1;
    let items = group !== null ? P.items : [], last = new Map();
    let done = false, doneAt = null, doneMarks = 0, stoppedAt = null, revivedAt = null;
    const served = new Set(), lastPass = [], watched = new Set();
    for (let t = 0; t <= P.until; t += 1) {
        const ms = t0 + t * 1000;
        const before = state.passes;
        for (const ev of P.events[t] || []) {
            const [kind, arg] = ev.split(":");
            if (kind === "drop") receiving = false;
            if (kind === "resume") receiving = true;
            if (kind === "gone") { state = c.replayOnReaderGone(state, group); group = null; receiving = false; items = []; }
            if (kind === "stall") stalledUntil = t + Number(arg);
            if (kind === "serverRestart") { state = c.REPLAY_IDLE; group = null; receiving = false; items = []; }
            if (kind === "subscribe") {
                group = arg; receiving = true; items = P.items; last = new Map();
                state = c.replayOnReader(state, ms, group);
            }
        }
        if (t <= stalledUntil) continue;
        if (t >= P.pickup && (t - P.pickup) % 30 === 0) {
            state = c.replayOnFact(state, done ? c.parseBackfill(c.backfillDoneValue(bf)) : bf, ms, group, factOpts);
        }
        const wasDone = state.done;
        state = c.replayTick(state, ms, { items });
        if (state.done && !wasDone) doneMarks += 1;
        if (state.done && !done) { done = true; doneAt = t; }
        if (state.stopped && stoppedAt === null) stoppedAt = t;
        if (state.revived && revivedAt === null) revivedAt = t;
        if (state.passes !== before || state.stopped) lastPass.length = 0;
        if (group === null) continue;
        const p = c.replayPoint(state, ms, opts);
        if (p && receiving) { served.add(p.gridEpoch); lastPass.push(p.index); }
        const sent = [];
        for (const [handle, node] of items) {
            const sample = p ? vals[node][p.index] : BAD;
            if (last.has(handle) && last.get(handle) === sample) continue;
            last.set(handle, sample);
            if (!p || !receiving) continue;
            const held = (P.withhold[handle] || []).includes(p.index) && (P.withholdAll || state.passes === 1);
            if (held) continue;
            if (p.index === P.watch && state.passes === 1) watched.add(handle);
            sent.push([p.gridEpoch, handle]);
        }
        if (sent.length) state = c.replayOnDelivered(state, group, sent);
    }
    return { served: [...served], done, doneAt, doneMarks, passes: state.passes, stopped: state.stopped,
             revived: state.revived, stoppedAt, revivedAt,
             firstOfLastPass: lastPass[0] ?? null, lastPass: [...new Set(lastPass)].length,
             steps: state.order.length, watched: [...watched] };
})()"""


def _simulate(events: dict, *, until: int, group: str | None = "g1", pickup: int = 29, silent=(),
              items=READER, withhold=None, withhold_all=False, watch=None):
    cols = _mill_columns()
    payload = {
        "value": f"{ANCHOR};{FROM};{TO};{ANCHOR + 50}", "t0": ANCHOR + 50, "grid": GRID, "loop": LOOP,
        "dataSize": DATASET_ROWS, "events": events, "until": until, "group": group, "pickup": pickup,
        "silent": list(silent), "items": [list(i) for i in items], "withhold": withhold or {},
        "withholdAll": withhold_all, "watch": watch,
        "columns": {leaf: cols[leaf][1] for leaf in SIM_LEAVES},
    }
    return _node_with(_SIMULATE, payload)


def _step_values(leaf: str, frm=FROM, to=TO, anchor=ANCHOR):
    """The dataset value of ``leaf`` at each replay step (None: the separator)."""
    order = _order(frm, to, anchor)
    rows = _node(f"{json.dumps(order)}.map((e, k) => e === null ? null "
                 f": c.backfillRow(e, k, {anchor}, {LOOP}, {DATASET_ROWS}))")
    col = _mill_columns()[leaf][1]
    return [None if r is None else col[r if r < len(col) else 0] for r in rows]


def _alarm_leading_edge() -> int:
    """The replay step at which BPFO_Alarm first turns true in the history loop."""
    alarm = _step_values("BPFO_Alarm")
    return next(k for k in range(1, len(alarm)) if alarm[k] is True and alarm[k - 1] is False)


def _all_points():
    return {e for e in _order() if e is not None}


def test_a_window_seen_one_poll_late_is_still_replayed_from_point_zero():
    out = _simulate({}, until=2500, pickup=29)
    assert set(out["served"]) == _all_points()
    assert out["done"] is True and out["passes"] == 1


def test_a_reader_that_subscribes_after_the_window_starts_the_replay():
    out = _simulate({120: ["subscribe:g1"]}, until=2600, group=None)
    assert set(out["served"]) == _all_points() and out["done"] is True


def test_done_needs_the_grace_with_the_reader_still_subscribed():
    nominal = _simulate({}, until=2500)["doneAt"]
    early = _simulate({}, until=nominal - 1)
    assert early["done"] is False, "not done before every point and the grace period"
    gone = _simulate({nominal - 60: ["gone"]}, until=nominal + 600)
    assert gone["done"] is False, "a reader that left during the grace may have lost its buffered rows"


def test_a_disconnect_through_the_nominal_end_never_writes_done_and_reconnect_restarts_at_zero():
    # SFC loses its connection at 300 s; the server removes its subscription
    # at 420 s (session timeout) and nothing reads until 4000 s, far past the
    # nominal end plus grace. No ';done' without a reader; a new reader then
    # gets the whole replay from point 0.
    out = _simulate({300: ["drop"], 420: ["gone"]}, until=4000)
    assert out["done"] is False
    back = _simulate({300: ["drop"], 420: ["gone"], 4000: ["subscribe:g2"]}, until=6400)
    assert back["firstOfLastPass"] == 0 and back["done"] is True and back["passes"] == 2


def test_a_connection_that_stops_carrying_notifications_leaves_a_gap_and_a_new_pass():
    # The subscription stays (the server cannot tell) but nothing is sent for
    # 100 s: the first pass ends with a gap, a second pass from point 0 fills it.
    out = _simulate({300: ["drop"], 400: ["resume"]}, until=4600)
    assert out["passes"] == 2 and out["firstOfLastPass"] == 0 and out["done"] is True
    assert set(out["served"]) == _all_points()


def test_an_event_loop_stall_longer_than_a_pace_is_not_marked_done_from_the_clock():
    # A 5 s stall skips two 2 s steps that no reader ever got: the pass ends
    # with a gap, so the replay runs again from point 0 before it is done.
    out = _simulate({600: ["stall:5"]}, until=4600)
    assert out["passes"] == 2 and out["firstOfLastPass"] == 0
    assert out["lastPass"] == len(_all_points()) and out["done"] is True


def test_an_sfc_restart_mid_replay_replays_from_point_zero():
    # The old subscription goes at 300 s, a new one arrives at 360 s.
    out = _simulate({300: ["gone"], 360: ["subscribe:g2"]}, until=2800)
    assert out["passes"] == 2 and out["firstOfLastPass"] == 0 and out["done"] is True
    # a new subscription that arrives BEFORE the old one is removed restarts too
    overlap = _simulate({300: ["subscribe:g2"]}, until=2800)
    assert overlap["passes"] == 2 and overlap["done"] is True


def test_a_server_restart_mid_replay_replays_from_point_zero():
    out = _simulate({300: ["serverRestart"], 400: ["subscribe:g2"]}, until=2900)
    assert set(out["served"]) == _all_points() and out["done"] is True
    assert out["firstOfLastPass"] == 0


def test_a_resubscribe_loop_is_bounded():
    # SFC restarting every 60 s for two hours (a crash loop): after
    # REPLAY_MAX_PASSES passes the server stops replaying, without ';done'.
    # The next new reader revives it once for one more bounded replay
    # (an intentional SFC restart recovers a stopped replay); after that it
    # stays stopped, so the loop is bounded at two replays.
    loop = {60 * k: [f"subscribe:g{k}"] for k in range(1, 121)}
    out = _simulate(loop, until=7300)
    limit = _node("c.REPLAY_MAX_PASSES")
    assert out["passes"] == limit and out["stopped"] is True and out["done"] is False
    assert out["revived"] is True
    assert out["lastPass"] == 0, "nothing is served after the replay stopped"


def test_a_new_reader_after_a_stopped_replay_starts_one_fresh_bounded_replay():
    # The connection carries nothing from 300 s: every pass ends with a gap and
    # the replay stops after REPLAY_MAX_PASSES, with no ';done'. A new reader
    # (SFC restarted, as the freshness watchdog does) replays it once more
    # from point 0 and earns ';done'.
    stopped = _simulate({300: ["drop"]}, until=8950)
    assert stopped["stopped"] is True and stopped["done"] is False and stopped["revived"] is False
    out = _simulate({300: ["drop"], 9000: ["subscribe:g2"]}, until=11600)
    assert out["revived"] is True and out["stopped"] is False
    assert out["done"] is True and out["passes"] == 1 and out["firstOfLastPass"] == 0
    assert set(out["served"]) == _all_points()


_WINDOW = f"{ANCHOR};{FROM};{TO};{ANCHOR + 50}"


def _readers(script: str, value: str = _WINDOW):
    """Run ``script`` (JS statements over ``s``, pushing to ``log``) on a window's replay state."""
    return _node(f"(() => {{ const log = []; const snap = (x) => [x.stopped, x.passes, x.revived === true, "
                 f"x.done, x.pass && x.pass.group]; "
                 f"let s = c.replayOnFact(c.REPLAY_IDLE, c.parseBackfill({json.dumps(value)}), 0, null, "
                 f"{{loopSeconds: {LOOP}, nodes: ['n']}}); {script}; return log; }})()")


def test_a_stopped_replay_is_revived_once_and_never_by_the_reader_that_stopped_it():
    limit = _node("c.REPLAY_MAX_PASSES")
    log = _readers(f"for (let k = 1; k <= {limit + 1}; k++) s = c.replayOnReader(s, k * 1000, 'g' + k); "
                   "log.push(snap(s)); "
                   f"s = c.replayOnReader(s, 90000, 'g{limit + 1}'); log.push(snap(s)); "
                   "s = c.replayOnReader(s, 91000, 'gNew'); log.push(snap(s)); "
                   f"for (let k = 1; k <= {limit}; k++) s = c.replayOnReader(s, 92000 + k, 'h' + k); "
                   "log.push(snap(s)); "
                   "s = c.replayOnReader(s, 99000, 'hNew'); log.push(snap(s));")
    assert log == [
        [True, limit, False, False, None],           # stopped by the reader that took the last pass
        [True, limit, False, False, None],           # that same reader again: nothing
        [False, 1, True, False, "gNew"],             # a new reader: one fresh replay from point 0
        [True, limit, True, False, None],            # the fresh replay is bounded too
        [True, limit, True, False, None],            # and nothing revives it a second time
    ]


def test_a_done_replay_never_restarts_for_a_new_reader():
    log = _readers("s = c.replayOnReader(s, 1000, 'g1'); log.push(snap(s));", value=_WINDOW + ";done")
    assert log == [[False, 0, False, True, None]]
    # a ';done' read back while stopped stays done, and no reader revives it
    limit = _node("c.REPLAY_MAX_PASSES")
    log = _readers(f"for (let k = 1; k <= {limit + 1}; k++) s = c.replayOnReader(s, k * 1000, 'g' + k); "
                   f"s = c.replayOnFact(s, c.parseBackfill({json.dumps(_WINDOW + ';done')}), 9000, null, "
                   f"{{loopSeconds: {LOOP}, nodes: ['n']}}); "
                   "s = c.replayOnReader(s, 10000, 'gNew'); log.push(snap(s));")
    assert log == [[True, limit, False, True, None]]


def test_a_new_window_after_a_revived_replay_gets_the_full_bound_again():
    limit = _node("c.REPLAY_MAX_PASSES")
    other = f"{ANCHOR};{FROM};{TO};{ANCHOR + 999}"          # a Reset and a new placement: a new identity
    log = _readers(f"for (let k = 1; k <= {limit + 2}; k++) s = c.replayOnReader(s, k * 1000, 'g' + k); "
                   "log.push(snap(s)); "
                   f"s = c.replayOnFact(s, c.parseBackfill({json.dumps(other)}), 20000, 'g9', "
                   f"{{loopSeconds: {LOOP}, nodes: ['n']}}); log.push(snap(s));")
    assert log == [[False, 1, True, False, f"g{limit + 2}"], [False, 1, False, False, "g9"]]


def test_steps_the_change_filter_drops_do_not_block_done():
    # Steps identical to the previous one on every node (the stopped spindle)
    # can never reach SFC; they are not required.
    out = _simulate({}, until=2500, silent=(88, 89, 90))
    assert out["done"] is True and out["passes"] == 1


def _one_clean_pass_done_at() -> int:
    return _simulate({}, until=2500)["doneAt"]


def test_a_withheld_alarm_value_at_its_leading_edge_is_a_gap_not_done():
    # Module 5's alarm: the BPFO_Alarm item's value at the leading edge never
    # reaches the reader while the other items (Spectrum_BPFO among them)
    # carry the same epoch. Either of SFC's two channels on the alarm node
    # missing it leaves a gap: the pass starts over at point 0, and only a
    # pass that delivers it through both items earns ';done'.
    edge, single = _alarm_leading_edge(), _one_clean_pass_done_at()
    for handle in ALARM_HANDLES:
        out = _simulate({}, until=single + 10, withhold={str(handle): [edge]}, watch=edge)
        assert 1 in out["watched"], "another item carried the leading edge's epoch"
        assert handle not in out["watched"]
        assert out["done"] is False and out["passes"] == 2 and out["firstOfLastPass"] == 0
    later = _simulate({}, until=2 * single + 60, withhold={"2": [edge]})
    assert later["done"] is True and later["passes"] == 2 and later["doneMarks"] == 1
    never = _simulate({}, until=6 * single, withhold={"2": [edge]}, withhold_all=True)
    assert never["done"] is False and never["stopped"] is True


def test_a_reader_without_an_item_on_the_alarm_node_never_earns_done():
    no_alarm = [item for item in READER if item[1] != "BPFO_Alarm"]
    out = _simulate({}, until=6 * _one_clean_pass_done_at(), items=no_alarm)
    assert out["done"] is False and out["stopped"] is True
    assert out["passes"] == _node("c.REPLAY_MAX_PASSES")


def test_full_delivery_on_every_item_completes_once_and_marks_done_once():
    out = _simulate({}, until=4000)
    assert out["done"] is True and out["passes"] == 1 and out["doneMarks"] == 1
    pace, grace = _node("[c.BACKFILL_PACE_MS / 1000, c.BACKFILL_DONE_GRACE_MS / 1000]")
    assert out["doneAt"] >= 29 + out["steps"] * pace + grace


def test_a_reader_that_saw_the_last_pass_gets_every_value_again_on_the_next():
    # A gap on the same reader starts the next pass on that reader's existing
    # items. Each pass opens with the history nodes Bad for one pace, so point
    # 0 is a change on every item even where its value equals the last step's
    # (BPFO_Alarm is false at both ends of the window).
    alarm = _step_values("BPFO_Alarm")
    first, final = alarm[0], [v for v in alarm if v is not None][-1]
    assert first is False and final is False
    out = _simulate({300: ["drop"], 400: ["resume"]}, until=4600)
    assert out["passes"] == 2 and out["done"] is True


def test_delivered_values_keep_each_items_handle_and_only_good_values():
    good = "statusCode: { isGood: () => true }"
    stamp = "sourceTimestamp: new Date(1790000010000)"
    expr = ("c.deliveredValues({ notificationData: [ { monitoredItems: ["
            f"{{ clientHandle: 2, value: {{ {stamp}, {good} }} }},"
            f"{{ clientHandle: 1, value: {{ {stamp}, {good} }} }},"
            f"{{ clientHandle: 3, value: {{ {stamp}, statusCode: {{ isGood: () => false }} }} }},"
            f"{{ clientHandle: 9, value: {{ {stamp}, {good} }} }},"
            "{ clientHandle: 4, value: null } ] }, { events: [] } ] }, new Set([1, 2, 3, 4]))")
    assert _node(expr) == [[1790000010, 2], [1790000010, 1]]


def test_expected_values_are_each_nodes_own_changes_in_the_dataset():
    """A history node owes a value at a step only where its OWN dataset value
    differs from the step before, or after a Bad value (the first step and
    the first step after the separator), which every item reports."""
    cols = _mill_columns()
    leaves = ("BPFO_Alarm", "Spectrum_BPFO", "Status", "ToolChangeEvent")
    payload = {"value": f"{ANCHOR};{FROM};{TO};{ANCHOR + 50}", "grid": GRID, "loop": LOOP,
               "dataSize": DATASET_ROWS, "columns": [[leaf, cols[leaf][1]] for leaf in leaves]}
    expr = """(() => { const bf = c.parseBackfill(P.value);
        const order = c.backfillOrder(bf.from, bf.to, P.grid, bf.anchor, P.loop);
        const opts = { loopSeconds: P.loop, dataSize: P.dataSize };
        const exp = c.backfillExpected(order, P.columns.map(([n]) => n),
                                       (k) => c.backfillChangedNodes(k, order, bf, P.columns, opts));
        return Object.fromEntries([...exp].map(([n, s]) => [n, [...s].sort((a, b) => a - b)])); })()"""
    got = _node_with(expr, payload)
    order = _order()
    sep = order.index(None)
    for leaf in leaves:
        vals = _step_values(leaf)
        want = [k for k, e in enumerate(order) if e is not None
                and (k == 0 or order[k - 1] is None or vals[k] != vals[k - 1])]
        assert got[leaf] == want, leaf
    edge = _alarm_leading_edge()
    assert edge in got["BPFO_Alarm"] and edge in got["Spectrum_BPFO"]
    assert _step_values("BPFO_Alarm")[edge + 1] is True and edge + 1 not in got["BPFO_Alarm"]
    assert got["ToolChangeEvent"] == [0, sep + 1], "a constant channel owes a value only after Bad"


def test_every_history_node_is_one_sfc_reads_and_sfc_reads_every_one():
    """';done' needs the reader to hold an item on EVERY history node, so the
    server's history twins must be exactly the nodes SFC's history source
    subscribes: the mill's constant identity strings get no twin."""
    from agents.demo import sfc_config
    leaves = [[leaf, kind == "STRING", sorted({str(v) for v in values})[:2]]
              for leaf, (kind, values) in _mill_columns().items()]
    twinned = _node_with("P.filter(([, isString, vals]) => c.hasHistoryTwin(vals, isString)).map(([l]) => l)",
                         leaves)
    sfc = {ch.alias[len(_mill()[1]):]
           for ch in sfc_config._story_mill_channels(sfc_config.resolve_channels())}
    assert set(twinned) == sfc
    assert {"BPFO_Alarm", "Spectrum_BPFO", "Status", "Bad", "ToolChangeEvent"} <= sfc


def test_the_server_twins_the_mill_sfc_reads():
    """The server picks the story mill by its own STORY_MILL_PREFIX default
    (no packaged environment sets it); it must name the mill whose channels
    SFC's history source subscribes, or the twins land under another node."""
    import re
    source = (CLOCK.parent / "server.mjs").read_text(encoding="utf-8")
    match = re.search(r'const STORY_MILL_PREFIX = process\.env\.STORY_MILL_PREFIX\s*\|\|\s*"([^"]+)"', source)
    assert match, "server.mjs no longer declares the STORY_MILL_PREFIX default this test reads"
    assert f"{match.group(1)}_" == _mill()[1]


def test_a_finished_replay_is_marked_done_and_not_replayed_again():
    value = f"{ANCHOR};{FROM};{TO};{ANCHOR + 50};done"
    served = _node(f"(() => {{ const ms = {ANCHOR + 50} * 1000; "
                   f"let s = c.replayOnFact(c.REPLAY_IDLE, c.parseBackfill({json.dumps(value)}), ms, 'g1', {OPTS}); "
                   f"s = c.replayOnReader(s, ms, 'g2'); return c.replayPoint(s, ms + 2000, {OPTS}); }})()")
    assert served is None
    out = _simulate({2600: ["gone"], 2610: ["subscribe:g2"]}, until=2700)
    assert out["done"] is True and out["passes"] == 1


def test_no_window_or_pending_is_inert():
    for raw in [None, "pending", ""]:
        assert _node(f"c.replayPoint(c.replayOnFact(c.REPLAY_IDLE, c.parseBackfill({json.dumps(raw)}), "
                     f"0, 'g1', {OPTS}), 1000, {OPTS})") is None


def test_history_nodes_sit_outside_every_highbyte_branch_input():
    """A reader of the plant's branch (HighByte's project reads each site as a
    Branch input) must neither see the replay nor restart it by subscribing:
    the history nodes are under their own root folder, the same in the server
    and in the SFC config."""
    import re
    from agents.demo import sfc_config
    server = (CLOCK.parent / "server.mjs").read_text(encoding="utf-8")
    (root,) = re.findall(r'const HISTORY_ROOT = "([^"]+)"', server)
    assert root == sfc_config.HISTORY_ROOT
    project = json.loads((ROOT / "config" / "highbyte" / "intelligencehub-configuration.json").read_text())
    branches = [i["qualifier"]["identifier"]["value"] for i in project["project"]["inputs"]
                if i.get("qualifier", {}).get("type") == "Branch"]
    assert branches, "expected the HighByte project's site Branch inputs"
    for branch in branches:
        assert not f"{root}/".startswith(f"{branch}/") and root != branch, branch


# ---------------------------------------------------------------------------
# The window (story_history.backfill_window) against SiteWise's ingest limit:
# every point is inside [-7 d, +10 min] when it is sent, including the whole
# replay, a restart and the margin; never more than 3 d + one loop of points.
# ---------------------------------------------------------------------------

def _schedule_ages(anchor, placed):
    frm, to = sh.backfill_window(anchor, placed)
    order = _order(frm, to, anchor)
    pace = _node("c.BACKFILL_PACE_MS") / 1000
    pickup = 90                                # mirror cycle + one server poll
    duration = len(order) * pace
    # worst case: one full replay lost to a restart just before its end
    ages = [placed + pickup + duration + k * pace - e for k, e in enumerate(order) if e is not None]
    return order, duration, ages


@pytest.mark.parametrize("lag", [0, 3 * 3600, sh.DAY, 4 * sh.DAY, 30 * sh.DAY])
def test_every_point_is_inside_the_sitewise_window_when_it_is_sent(lag):
    placed = ANCHOR + 3600
    anchor = ANCHOR - lag                    # a normalised anchor is at most one loop behind
    order, duration, ages = _schedule_ages(anchor, placed)
    points = [e for e in order if e is not None]
    assert len(points) <= sh.BACKFILL_MAX_DEPTH_SECONDS // GRID + 1
    assert duration <= 33 * 60, duration
    assert max(ages) <= sh.SITEWISE_WINDOW_SECONDS - GRID, max(ages) - sh.SITEWISE_WINDOW_SECONDS
    assert min(points) >= placed - sh.BACKFILL_MAX_DEPTH_SECONDS


def test_the_window_covers_the_story_depth_for_a_placed_anchor():
    """A placement normalises the anchor into the loop before it: the window is
    then exactly the 3-day story depth ending at the placement."""
    placed = ANCHOR + 1800
    frm, to = sh.backfill_window(ANCHOR, placed)
    assert (frm, to) == (ANCHOR - 3 * sh.DAY, cm.grid_floor(placed))
    assert sh.BACKFILL_MAX_DEPTH_SECONDS + sh.REPLAY_HORIZON_SECONDS <= sh.SITEWISE_WINDOW_SECONDS - GRID
