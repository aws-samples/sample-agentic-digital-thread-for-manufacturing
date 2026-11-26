// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Which dataset row the OPC UA server serves at a given instant. Pure: no I/O,
// no globals, so the rule is pinned against the simulator's condition model by
// agentic_digital_thread/tests/unit/test_opcua_story_clock.py (node evaluates it).
//
// Two clocks:
//
// * WALL CLOCK (every demo deploy, the frozen story, and every site without a
//   story): row = (epoch ms mod loop length) / interval, so every server and
//   every restart is at the same row at the same instant, and the condition
//   monitor (erp_simulation/condition_monitor.py, loop_step) evaluates the
//   same row (ADR-0073).
// * STORY ANCHOR (the live workshop story's mill, ADR-0092): the loop starts
//   at the stored anchor epoch and lasts the packaged loop length
//   (STORY_LOOP_SECONDS, default 28800 s). Before the anchor the mill is
//   healthy (row 0), except inside the history loop (the loop containing
//   anchor - 1 day), exactly as the simulator's story clock defines it. The
//   dataset is generated on the 8 h loop; a shorter packaged loop plays it
//   time-scaled, so every phase boundary (BPFO alarm, failure, repair) lands
//   at the same fraction of the loop as the condition model's.
//   HELD (ADR-0092 decision 8): while a package that auto-places on the first
//   sign-in has not been placed yet, the mirror sends "<anchor>;healthy" and
//   the server publishes no value (BadWaitingForInitialData, see publishes),
//   so SiteWise holds nothing before the first placement; that placement
//   climbs from the healthy row. WAITING publishes nothing either.

export const MODEL_INTERVAL_SECONDS = 5;        // condition_monitor.INTERVAL_SEC
export const FAIL_FRACTION = 0.9;               // condition_monitor.FAIL_FRACTION
export const REPAIR_STEPS_CAP = 240;            // condition_monitor.REPAIR_STEPS_CAP (20 min at 5 s)
export const REPAIR_LOOP_DIVISOR = 24;          // condition_monitor.REPAIR_LOOP_DIVISOR
export const DEFAULT_LOOP_SECONDS = 28800;      // condition_monitor.DEFAULT_LOOP_SECONDS
export const LOOP_SECONDS_MULTIPLE = 300;       // condition_monitor.LOOP_SECONDS_MULTIPLE
export const LOOP_SECONDS_RANGE = [3600, DEFAULT_LOOP_SECONDS];
export const HISTORY_OFFSET_SECONDS = 86400;    // story_history: the history loop holds anchor - 1 day
export const PENDING = "pending";               // the parameter's value before the first mirror
export const HELD_SUFFIX = ";healthy";          // story_anchor_mirror.HELD_SUFFIX: not placed yet
export const HEALTHY_FROM = 0;                  // story_anchor_mirror.HEALTHY_FROM: catch up from row 0

export function wallClockRow(nowMs, dataSize, intervalMs) {
    if (!dataSize) return 0;
    const loopMs = dataSize * intervalMs;
    return Math.floor((nowMs % loopMs) / intervalMs);
}

// The packaged loop length, validated like condition_monitor.loop_seconds_from:
// unset or empty is the 8 h loop; anything else must be a multiple of 300 s in
// [3600, 28800] or this throws (fail closed, the server refuses to start).
export function loopSecondsFrom(raw) {
    if (raw === undefined || raw === null || String(raw).trim() === "") return DEFAULT_LOOP_SECONDS;
    const text = String(raw).trim();
    const value = Number(text);
    const [low, high] = LOOP_SECONDS_RANGE;
    if (!/^\d+$/.test(text) || value < low || value > high || value % LOOP_SECONDS_MULTIPLE !== 0) {
        throw new Error(`STORY_LOOP_SECONDS=${text}: need a multiple of ${LOOP_SECONDS_MULTIPLE} in [${low}, ${high}]`);
    }
    return value;
}

// The clock a parameter value selects. Missing, empty, "pending" or not a
// number: WAITING (the mirror has not written yet; publish nothing, never
// guess the wall clock). Zero or negative: WALL (no anchor row, the
// same rule as the simulator's "absent means wall clock"). Positive: ANCHORED.
// "<anchor>;<previous anchor>;<start>;<end>" is ANCHORED with a CATCH-UP
// (a placement moved the anchor; erp_simulation/story_anchor_mirror.py); a
// previous anchor of HEALTHY_FROM climbs from the healthy row (the server was
// HELD). "<anchor>;healthy" is HELD: the package auto-places on the first
// sign-in and nobody has placed it yet, so publish nothing
// (ADR-0092 decision 8).
const NUMBER = /^-?\d+(\.\d+)?$/;
const CATCH_UP = /^(\d+(?:\.\d+)?);(\d+);(\d+);(\d+)$/;
const HELD = /^(\d+(?:\.\d+)?);healthy$/;

export function clockFromParameter(raw) {
    const text = raw === undefined || raw === null ? "" : String(raw).trim();
    const withCatchUp = CATCH_UP.exec(text);
    if (withCatchUp) {
        const [anchor, from, start, end] = withCatchUp.slice(1).map(Number);
        if (anchor > 0 && from >= HEALTHY_FROM && start < end) {
            return { mode: "anchored", anchor, catchUp: { from, start, end } };
        }
        return { mode: "waiting" };
    }
    const held = HELD.exec(text);
    if (held) {
        const anchor = Number(held[1]);
        return anchor > 0 ? { mode: "held", anchor } : { mode: "waiting" };
    }
    if (text === "" || text === PENDING || !NUMBER.test(text)) return { mode: "waiting" };
    const anchor = Number(text);
    return anchor > 0 ? { mode: "anchored", anchor } : { mode: "wall" };
}

export function historyLoopStart(anchor, loopSeconds) {
    return anchor + Math.floor(-HISTORY_OFFSET_SECONDS / loopSeconds) * loopSeconds;
}

export function storyRow(nowMs, anchor, loopSeconds, dataSize) {
    if (!dataSize) return 0;
    const t = nowMs / 1000;
    const history = historyLoopStart(anchor, loopSeconds);
    if (t < anchor && !(history <= t && t < history + loopSeconds)) return 0;
    const position = (((t - anchor) % loopSeconds) + loopSeconds) % loopSeconds;
    const steps = Math.floor(loopSeconds / MODEL_INTERVAL_SECONDS);
    const step = Math.floor(position / MODEL_INTERVAL_SECONDS);
    return Math.min(dataSize - 1, Math.floor((step * dataSize) / steps));
}

// A placement cannot rewrite what this server already sent to SiteWise, so
// it does not jump to the new anchor's row: from `start` it plays linearly
// from the previous anchor's row to the new anchor's alarm row, which it
// reaches at `end` (the new anchor's next BPFO alarm). Before `start` it is
// still the previous anchor's row; from `end` it is the new anchor's. A move
// BACK (the previous row is past the target) jumps, as a reset should.
// From HEALTHY_FROM the previous row is the healthy row the held server was
// serving. Stateless, so a restart mid-catch-up serves the same row.
export function catchUpRow(nowMs, clock, loopSeconds, dataSize) {
    const { from, start, end } = clock.catchUp;
    const t = nowMs / 1000;
    if (t >= end) return null;
    const fromRow = from === HEALTHY_FROM ? 0
        : storyRow(Math.min(t, start) * 1000, from, loopSeconds, dataSize);
    if (t < start) return fromRow;
    const target = storyRow(end * 1000, clock.anchor, loopSeconds, dataSize);
    if (fromRow > target) return null;
    return fromRow + Math.floor(((target - fromRow) * (t - start)) / (end - start));
}

export function rowFor(clock, nowMs, { dataSize, intervalMs, loopSeconds }) {
    if (clock.mode === "anchored") {
        const caught = clock.catchUp ? catchUpRow(nowMs, clock, loopSeconds, dataSize) : null;
        return caught ?? storyRow(nowMs, clock.anchor, loopSeconds, dataSize);
    }
    if (clock.mode === "waiting" || clock.mode === "held") return 0;
    return wallClockRow(nowMs, dataSize, intervalMs);
}

// Whether the server publishes a value at all. WAITING (no usable parameter
// yet) and HELD (not placed yet) publish none: every tag reads
// BadWaitingForInitialData, which SFC never forwards, so SiteWise records
// nothing a later placement could contradict (ADR-0092 decision 8). rowFor
// still names row 0 for them: the healthy row a catch-up climbs from.
export function publishes(clock) {
    return clock.mode === "anchored" || clock.mode === "wall";
}

export function describeClock(clock) {
    if (clock.mode === "anchored") {
        const c = clock.catchUp;
        const from = c && c.from === HEALTHY_FROM ? "the healthy row" : c && c.from;
        return c ? `story anchor ${clock.anchor} (catching up from ${from}, ${c.start} to ${c.end})`
            : `story anchor ${clock.anchor}`;
    }
    if (clock.mode === "held") return `story anchor ${clock.anchor} held until the first placement (no value published)`;
    return clock.mode === "waiting" ? "waiting for the story anchor (no value published)" : "wall clock";
}

// ---------------------------------------------------------------------------
// One-time history replay (ADR-0092 decision 8 amendment; the replay ADR)
//
// SFC v1.11.0 cannot HistoryRead and a burst on the LIVE nodes collapses to
// one point per 5 s. So on the first placement out of the HOLD the story
// mill replays its history window ONCE, on SEPARATE history nodes, paced so
// each point is held long enough for SFC's faster history schedule to read
// it, each carrying its grid epoch as the SourceTimestamp. SFC reads those
// nodes through a second source whose schedule sets TimestampLevel "Channel",
// so each point lands at its historical time. SFC stays the only SiteWise
// writer; the live 5 s feed is untouched.
//
// This module is the PURE rule only (no I/O): server.mjs owns the nodes, the
// SSM poll of the second parameter and the OPC UA subscription events, and
// feeds them to the small state machine below.
//
// Pacing is anchored on the SERVER'S OWN observation, never on a time written
// by the simulator: the server first sees a new window up to one poll late,
// and a replay clock that started before that would skip its first points.
// The replay starts when the server has seen the window AND a reader (an OPC
// UA subscription) monitors a history node; a NEW reader starts a new pass
// from point 0, and losing the reader puts the replay back to waiting. A
// restarted SFC loses whatever it had buffered, and SiteWise overwrites an
// equal timestamp, so repeating the replay loses nothing (S3 Tables keeps the
// copies, see the replay ADR). A server restart loses this state, so the
// replay runs again from point 0.
// Completion is EVIDENCE, not elapsed time: the server records which values it
// actually SENT to the reader, item by item (the SourceTimestamp and the
// clientHandle of each notification in its PublishResponses). A pass that
// ends with a gap starts over; one that delivered every value each node owes
// through every one of the reader's items on that node, with an item on every
// history node, is done after a grace with the same reader still subscribed,
// and the server then marks the window done in the parameter so a later
// restart does not replay it. Passes are bounded (REPLAY_MAX_PASSES).
// ---------------------------------------------------------------------------

export const BACKFILL_STEP_SECONDS = 300;   // condition_monitor.TELEMETRY_GRID_SECONDS: the replay grid
export const BACKFILL_PACE_MS = 2000;       // wall-clock ms per replayed point (spike: 2 s step)
// After the last step, a reader that subscribes again still gets the whole
// replay until this much time has passed with the same reader subscribed:
// SFC's S3 Tables target holds up to a minute of rows
// (sfc_config.S3_TABLES_INTERVAL_MS) and an append takes 10-17 s, so a
// restart just after the last point would otherwise lose the tail.
export const BACKFILL_DONE_GRACE_MS = 300000;
export const BACKFILL_DONE = "done";

// "<H>;<from>;<to>;<id>" is a window to replay: the history anchor, the
// half-open window [from, to) on the grid, and the replay's identity (the
// wall epoch the simulator first published this window). The server appends
// ";done" once it has replayed it. Anything else (missing, "pending",
// malformed, from >= to) is null: no replay.
const BACKFILL = /^(\d+);(\d+);(\d+);(\d+)(;done)?$/;

export function parseBackfill(raw) {
    const text = raw === undefined || raw === null ? "" : String(raw).trim();
    const m = BACKFILL.exec(text);
    if (!m) return null;
    const [anchor, from, to, id] = m.slice(1, 5).map(Number);
    if (!(anchor > 0 && from < to && id > 0)) return null;
    return { anchor, from, to, id, key: `${anchor};${from};${to};${id}`, done: m[5] !== undefined };
}

// The value the server writes once it has replayed `bf`.
export function backfillDoneValue(bf) {
    return `${bf.key};${BACKFILL_DONE}`;
}

// Every grid epoch in [from, to) congruent with the anchor's grid residue,
// oldest first. A loop is a whole number of 300 s slots, so the history
// loop's points are on this grid and a re-replay writes the same timestamps.
function gridEpochs(from, to, step, anchor) {
    const residue = ((anchor % step) + step) % step;
    const epochs = [];
    for (let e = Math.ceil((from - residue) / step) * step + residue; e < to; e += step) epochs.push(e);
    return epochs;
}

// The replay order: the history loop (the loop containing anchor - 1 day)
// first, so anything reading yesterday's failure gets its data within the
// first minutes, then every other grid point. EACH segment runs oldest to
// newest: SFC forwards a value only when it differs from the last one it got
// (the OPC UA StatusValue trigger), so a Boolean such as BPFO_Alarm reaches
// SiteWise only at the points where it changes, and those must be its real
// leading edges in time. Between the two segments one step is `null`: the
// history nodes read Bad for one pace, which SFC does not forward but which
// makes the next segment's first value a change, so every channel has a point
// at the start of the second segment too.
export function backfillOrder(from, to, step, anchor, loopSeconds) {
    const start = historyLoopStart(anchor, loopSeconds);
    const inLoop = (e) => e >= start && e < start + loopSeconds;
    const all = gridEpochs(from, to, step, anchor);
    const loop = all.filter(inLoop);
    const rest = all.filter((e) => !inLoop(e));
    return loop.length && rest.length ? [...loop, null, ...rest] : [...loop, ...rest];
}

// The dataset row for the replay point `index` at `gridEpoch`. Inside the
// history loop it is exactly the condition model's row (storyRow); everywhere
// else, and across the whole [H, P) stretch, a healthy POST-REPAIR row chosen
// by index, so the value varies like a real sensor and passes SFC's change
// trigger (a repeated row 0 would collapse to one point), and the live
// catch-up climbs from the healthy row with no step at the placement.
export function backfillRow(gridEpoch, index, anchor, loopSeconds, dataSize) {
    const start = historyLoopStart(anchor, loopSeconds);
    if (gridEpoch >= start && gridEpoch < start + loopSeconds) {
        return storyRow(gridEpoch * 1000, anchor, loopSeconds, dataSize);
    }
    return healthyRow(index, loopSeconds, dataSize);
}

// The server's replay state. `key` is the window being replayed (null: none);
// `nodes` the history nodes; `expected` maps each node to the steps at which
// it owes the reader a value: every non-separator step at which that node's
// OWN value differs from its value at the step before, plus the first step and
// the first step after the separator (each follows a Bad value). A node whose
// value repeats is dropped by its item's change filter by design, so it owes
// nothing there. `pass` is the current attempt, bound to ONE reader (an OPC UA
// subscription id): when it started, and for each of the reader's monitored
// items (by clientHandle) the steps SENT through that item, and when it covered
// every expected value. `passes` counts attempts for this window; after
// REPLAY_MAX_PASSES the replay stops (`stopped`, in memory only, so a server
// restart tries again; `stoppedGroup` is the reader it stopped on). A NEW
// reader after that (SFC restarted, as the SFC freshness watchdog does when
// the replay is overdue) revives it ONCE for one fresh bounded replay
// (`revived`); a window never revives twice. `done` is the durable mark read
// back or earned, and a done window never replays again.
export const REPLAY_MAX_PASSES = 5;
export const REPLAY_IDLE = Object.freeze({
    key: null, bf: null, order: [], nodes: [], expected: new Map(), indexOf: new Map(),
    done: false, stopped: false, passes: 0, pass: null, revived: false, stoppedGroup: null,
});
const NO_STEPS = new Set();

// Whether a story-mill leaf gets a history twin. A STRING that never changes
// is the mill's identity (AssetName, EquipmentID, MaintenanceAssetNum): no
// modelled measurement, so SFC's history source never reads it
// (sfc_config.resolve_channels), and a twin would be a node no reader monitors.
export function hasHistoryTwin(values, isString) {
    return !(isString && new Set(values).size <= 1);
}

// The history nodes whose own value at replay step k differs from step k-1's.
// `columns` is [node, the dataset values of its leaf] per history node, read
// at the rows the replay serves (backfillRow); the first step and the first
// step after the separator change on every node.
export function backfillChangedNodes(k, order, bf, columns, { loopSeconds, dataSize }) {
    if (order[k] === null) return [];
    if (k === 0 || order[k - 1] === null) return columns.map(([node]) => node);
    const [a, b] = [k, k - 1].map((i) => backfillRow(order[i], i, bf.anchor, loopSeconds, dataSize));
    const pick = (arr, row) => (row >= arr.length ? arr[0] : arr[row]);
    return columns.filter(([, arr]) => pick(arr, a) !== pick(arr, b)).map(([node]) => node);
}

// The values that must be delivered, node -> steps (see REPLAY_IDLE).
// `changed(k)` names the nodes whose own value at step k differs from step
// k-1's (backfillChangedNodes); without it every node owes every step.
export function backfillExpected(order, nodes, changed) {
    const expected = new Map(nodes.map((node) => [node, new Set()]));
    for (let k = 0; k < order.length; k++) {
        if (order[k] === null) continue;
        const all = k === 0 || order[k - 1] === null || !changed;
        for (const node of all ? nodes : changed(k)) {
            const steps = expected.get(node);
            if (steps) steps.add(k);
        }
    }
    return expected;
}

// How many (node, step) values the window owes in all (logs, reports).
export function expectedCount(state) {
    let n = 0;
    for (const steps of state.expected.values()) n += steps.size;
    return n;
}

// A new pass for reader `group`, from point 0: nothing delivered yet. It
// opens with the history nodes Bad for `leadMs` (one pace), so point 0 is a
// change on every monitored item, including the items of a reader that was
// served the previous pass and last reported the same value. The sixth
// attempt stops the replay instead (a reader that keeps coming back would
// otherwise replay, and load SFC, for ever).
function startPass(state, nowMs, group, leadMs = BACKFILL_PACE_MS) {
    if (state.passes >= REPLAY_MAX_PASSES) return { ...state, stopped: true, pass: null, stoppedGroup: group };
    return { ...state, passes: state.passes + 1,
             pass: { group, startedAtMs: nowMs + leadMs, received: new Map(), completedAtMs: null } };
}

// The parameter was read. A new window starts a pass now when a reader is
// subscribed (`group`, else null: wait for one); the same window keeps its
// state (a done mark read back only confirms it). No window is idle.
// `nodes` names the history nodes, `changed(k, order, bf)` the nodes whose own
// value changes at step k (backfillChangedNodes).
export function replayOnFact(state, bf, nowMs, group,
                             { loopSeconds, step = BACKFILL_STEP_SECONDS, nodes = [], changed }) {
    if (!bf) return REPLAY_IDLE;
    if (state.key === bf.key) return bf.done && !state.done ? { ...state, done: true, pass: null } : state;
    const order = backfillOrder(bf.from, bf.to, step, bf.anchor, loopSeconds);
    const indexOf = new Map(order.map((e, k) => [e, k]).filter(([e]) => e !== null));
    const expected = backfillExpected(order, nodes, changed && ((k) => changed(k, order, bf)));
    const fresh = { ...REPLAY_IDLE, key: bf.key, bf, order, indexOf, done: bf.done, nodes: [...nodes], expected };
    return !bf.done && group !== null && group !== undefined ? startPass(fresh, nowMs, group) : fresh;
}

// A reader (subscription `group`) began monitoring a history node. Its first
// item starts a new pass from point 0 (a new reader has none of the earlier
// points); further items of the same subscription change nothing. A stopped
// replay restarts once, from point 0 with its pass count reset, for a reader
// other than the one it stopped on; a done one never.
export function replayOnReader(state, nowMs, group) {
    if (!state.key || state.done) return state;
    if (state.stopped) {
        if (state.revived || group === state.stoppedGroup) return state;
        return startPass({ ...state, stopped: false, passes: 0, revived: true, stoppedGroup: null }, nowMs, group);
    }
    if (state.pass && state.pass.group === group) return state;
    return startPass(state, nowMs, group);
}

// The current reader's last history item went away: back to waiting, and
// whatever it got before is no longer evidence (it may not have written it).
export function replayOnReaderGone(state, group) {
    return state.pass && state.pass.group === group ? { ...state, pass: null } : state;
}

function replayIndex(state, nowMs, paceMs) {
    if (!state.key || state.done || state.stopped || !state.pass) return null;
    return Math.floor((nowMs - state.pass.startedAtMs) / paceMs);
}

// The point the history nodes serve now ({ index, gridEpoch, row }), or null
// (no replay, waiting for a reader, the separator step, past the end, done
// or stopped).
export function replayPoint(state, nowMs, { loopSeconds, dataSize, paceMs = BACKFILL_PACE_MS }) {
    const index = replayIndex(state, nowMs, paceMs);
    if (index === null || index < 0 || index >= state.order.length) return null;
    const gridEpoch = state.order[index];
    if (gridEpoch === null) return null;
    return { index, gridEpoch, row: backfillRow(gridEpoch, index, state.bf.anchor, loopSeconds, dataSize) };
}

// The (SourceTimestamp in epoch s, clientHandle) of each Good value in one
// PublishResponse's NotificationMessage for the monitored items in `handles`:
// what the server handed this reader, item by item.
export function deliveredValues(notificationMessage, handles) {
    const delivered = [];
    for (const nd of notificationMessage.notificationData || []) {
        for (const item of nd.monitoredItems || []) {
            const value = item.value;
            if (!handles.has(item.clientHandle) || !value || !value.sourceTimestamp) continue;
            if (value.statusCode && value.statusCode.isGood && !value.statusCode.isGood()) continue;
            delivered.push([Math.round(value.sourceTimestamp.getTime() / 1000), item.clientHandle]);
        }
    }
    return delivered;
}

// The server SENT reader `group` these values ([epoch s, clientHandle], from
// deliveredValues) in a PublishResponse: the evidence a step reached it
// through that item.
export function replayOnDelivered(state, group, delivered) {
    const pass = state.pass;
    if (!pass || pass.group !== group || pass.completedAtMs !== null) return state;
    let received = null;
    for (const [epoch, handle] of delivered) {
        const k = state.indexOf.get(epoch);
        if (k === undefined || (pass.received.get(handle) || NO_STEPS).has(k)) continue;
        if (!received) received = new Map(pass.received);
        if (received.get(handle) === pass.received.get(handle)) {
            received.set(handle, new Set(pass.received.get(handle) || []));
        }
        received.get(handle).add(k);
    }
    return received ? { ...state, pass: { ...pass, received } } : state;
}

// How many expected values the current pass has not delivered (0: complete).
// `items` are the reader's monitored items on history nodes now, as
// [clientHandle, node]. Every item on a node owes that node's steps (SFC
// reads a Boolean through two items, one per channel); a node the reader
// holds no item on is missing all of its steps, and at least one.
export function replayMissing(state, items = []) {
    const received = state.pass ? state.pass.received : new Map();
    const byNode = new Map();
    for (const [handle, node] of items) {
        if (!byNode.has(node)) byNode.set(node, []);
        byNode.get(node).push(handle);
    }
    let missing = 0;
    for (const [node, steps] of state.expected) {
        const handles = byNode.get(node) || [];
        if (!handles.length) missing += Math.max(1, steps.size);
        for (const handle of handles) {
            const got = received.get(handle) || NO_STEPS;
            for (const k of steps) if (!got.has(k)) missing += 1;
        }
    }
    return missing;
}

// Called every second with the pass reader's current `items` (see
// replayMissing). Past the last step, a pass that delivered every expected
// value through every item, on a reader holding an item on every history
// node, is complete; one with gaps (a stall skipped a step, the connection
// carried nothing for a while, an item's value never went out) starts over
// from point 0. Done only after the flush grace with the SAME reader still
// subscribed and still holding every item it was credited through.
export function replayTick(state, nowMs, { paceMs = BACKFILL_PACE_MS, graceMs = BACKFILL_DONE_GRACE_MS, items = [] } = {}) {
    const index = replayIndex(state, nowMs, paceMs);
    if (index === null || index < state.order.length) return state;
    const pass = state.pass;
    if (pass.completedAtMs !== null && nowMs - pass.completedAtMs < graceMs) return state;
    const covered = state.expected.size > 0 && replayMissing(state, items) === 0;
    if (!covered) return startPass(state, nowMs, pass.group, paceMs);
    if (pass.completedAtMs === null) return { ...state, pass: { ...pass, completedAtMs: nowMs } };
    return { ...state, done: true, pass: null };
}

// A post-repair healthy row (steps after the repair ends, before the loop
// wraps), chosen by replay index so consecutive points differ.
export function healthyRow(index, loopSeconds, dataSize) {
    const steps = Math.floor(loopSeconds / MODEL_INTERVAL_SECONDS);
    const fail = Math.floor(steps * FAIL_FRACTION);
    const repair = Math.min(REPAIR_STEPS_CAP, Math.floor(steps / REPAIR_LOOP_DIVISOR));
    const firstHealthy = fail + repair;
    const tail = Math.max(1, steps - firstHealthy);
    const step = firstHealthy + (((index % tail) + tail) % tail);
    return Math.min(dataSize - 1, Math.floor((step * dataSize) / steps));
}
