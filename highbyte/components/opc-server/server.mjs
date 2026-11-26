// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as opc from "node-opcua";
import { DataType } from "node-opcua";
import { createReadStream } from 'fs';
import { createInterface } from 'readline';
import { argv } from 'process';
import { basename } from 'path';
import { clockFromParameter, describeClock, loopSecondsFrom, publishes, rowFor,
         parseBackfill, backfillDoneValue, backfillChangedNodes, deliveredValues, expectedCount,
         hasHistoryTwin, REPLAY_IDLE, replayOnFact, replayOnReader, replayOnReaderGone,
         replayOnDelivered, replayPoint, replayTick, replayMissing } from './clock.mjs';

// ---------------------------------------------------------------------------
// Data store — keyed by ALIAS path, each value is an array of time-series
// values that the server cycles through.
// ---------------------------------------------------------------------------
const data = {};
let dataIndex = 0;
let dataSize = 0;

// How often (ms) to advance to the next data point.  Default 5 000 ms.
const UPDATE_INTERVAL_MS = parseInt(process.env.UPDATE_INTERVAL_MS || "5000", 10);

// Which row is served comes from clock.mjs. By default the dataset is played
// WALL-CLOCK ALIGNED (row = epoch ms mod loop length / interval), so every
// server and every restart is at the same row at the same instant, and the
// simulator's condition monitor sees the value this server is serving
// (ADR-0073). With the default 5 760-row / 5 s datasets the loop is 8 h,
// aligned to 00:00, 08:00 and 16:00 UTC.
//
// When STORY_ANCHOR_PARAMETER names an SSM parameter (the live workshop
// story's mill only, ADR-0092) the server follows the plant clock instead:
// it polls the parameter, which the simulator mirrors from
// simulation_config.story_anchor_epoch, and plays the loop from that anchor.
// After a placement the parameter also carries a catch-up window, and the
// server climbs to the new alarm instead of stepping there (clock.mjs).
// Until the parameter carries a usable clock, and while an auto-placing
// package is held before its first placement, it publishes no value.
const ANCHOR_PARAMETER = process.env.STORY_ANCHOR_PARAMETER || "";
const STORY_LOOP_SECONDS = loopSecondsFrom(process.env.STORY_LOOP_SECONDS);
const ANCHOR_POLL_MS = 30000;
let clock = ANCHOR_PARAMETER ? { mode: "waiting" } : { mode: "wall" };

// One-time history replay (the replay ADR; ADR-0092 decision 8 amendment).
// When STORY_FEED_BACKFILL_PARAMETER names an SSM parameter (the live story
// mill only, set beside STORY_ANCHOR_PARAMETER), the server grows a parallel
// set of HISTORY nodes under the story mill and, on the first placement out
// of the HOLD, replays the history window once on them — paced so each point
// survives SFC's faster history schedule, each stamped with its grid epoch as
// the SourceTimestamp. SFC reads those nodes through a second source whose
// schedule sets TimestampLevel "Channel", so each point lands at its
// historical time; the live 5 s feed on the ordinary nodes is untouched.
// The replay is paced from the server's own first sight of the window and a
// subscribed reader, starts over for a new reader or a pass with gaps, and is
// marked done in the parameter only once every value each history node owes
// was SENT through each of one reader's items on it (clock.mjs).
// Absent (every other server, demo and frozen synths) nothing here runs: no
// history nodes are created and no second parameter is polled.
const BACKFILL_PARAMETER = process.env.STORY_FEED_BACKFILL_PARAMETER || "";
// History nodes twin the story mill's CNC leaves. The mill's process node is
// the only one under this prefix. Each leaf's twin sits under its own ROOT
// folder, "<HISTORY_ROOT>/<leaf alias>", outside the site's branch: a reader
// of the plant's branch (HighByte's project reads the whole site) never sees
// the replay, and never restarts it by subscribing.
const STORY_MILL_PREFIX = process.env.STORY_MILL_PREFIX
    || "AnyCompany/Indianapolis/Machining/Line3/Line3CNC1/Line3CNC1";
const HISTORY_ROOT = "StoryHistoryReplay";   // sfc_config.HISTORY_ROOT
const REPLAY_TICK_MS = 1000;         // how often a pass is checked for its end
let replay = REPLAY_IDLE;            // clock.mjs replay state for the second parameter
let lastFact = null;                 // the parameter as last read (or written ;done)
const historyAliases = new Set();    // NodeId strings of the history nodes
const historyColumns = [];           // [history NodeId string, the live leaf's dataset values]
const historyReaders = new Map();    // subscription id -> Map(monitoredItem -> [clientHandle, history node])

// The history nodes whose own value at replay step k differs from step k-1's:
// a node whose value repeats is dropped by its item's change filter, so it
// owes no value there (clock.mjs backfillExpected).
function historyChangedNodes(k, order, bf) {
    return backfillChangedNodes(k, order, bf, historyColumns, { loopSeconds: STORY_LOOP_SECONDS, dataSize });
}

// A reader's monitored items on history nodes, as [clientHandle, node].
function readerItems(group) {
    const items = group === null || group === undefined ? null : historyReaders.get(group);
    return items ? [...items.values()] : [];
}

function currentRow(nowMs = Date.now()) {
    return rowFor(clock, nowMs, {
        dataSize, intervalMs: UPDATE_INTERVAL_MS, loopSeconds: STORY_LOOP_SECONDS,
    });
}

// A failed read keeps the clock already in use: never fall back to the wall
// clock on an error (the simulator's rule for an unreadable anchor).
async function pollAnchor(ssm, GetParameterCommand) {
    try {
        const out = await ssm.send(new GetParameterCommand({ Name: ANCHOR_PARAMETER }));
        const next = clockFromParameter(out.Parameter && out.Parameter.Value);
        if (describeClock(next) !== describeClock(clock)) {
            console.log(`Story clock: ${describeClock(next)}`);
        }
        clock = next;
    } catch (err) {
        console.warn(`Story anchor read failed (${err.name || err}); keeping ${describeClock(clock)}`);
    }
    dataIndex = currentRow();
}

async function startAnchorPolling() {
    if (!ANCHOR_PARAMETER && !BACKFILL_PARAMETER) return;
    // Loaded only when a parameter is configured, so a demo server never
    // loads the SDK. Region and credentials come from the ECS task.
    const { SSMClient, GetParameterCommand, PutParameterCommand } = await import("@aws-sdk/client-ssm");
    const ssm = new SSMClient({});
    if (ANCHOR_PARAMETER) {
        await pollAnchor(ssm, GetParameterCommand);
        setInterval(() => pollAnchor(ssm, GetParameterCommand), ANCHOR_POLL_MS);
    }
    if (BACKFILL_PARAMETER) {
        const commands = { GetParameterCommand, PutParameterCommand };
        await pollBackfill(ssm, commands);
        setInterval(() => pollBackfill(ssm, commands), ANCHOR_POLL_MS);
        setInterval(() => tickBackfill(ssm, PutParameterCommand), REPLAY_TICK_MS);
    }
}

// The reader a new pass binds to: any subscription that monitors a history
// node (null: none, wait for one).
function anyReader() {
    const first = historyReaders.keys().next();
    return first.done ? null : first.value;
}

// Log what changed between two replay states (pass started, gap, stopped, done).
function logReplay(before, after, why) {
    if (after.key !== before.key) {
        const state = !after.key ? "none" : after.done ? "already done"
            : after.pass ? "replaying from point 0" : "waiting for a history reader";
        console.log(`Story history replay: ${after.key || "none"} (${after.order.length} steps, `
            + `${expectedCount(after)} node values to deliver, ${state})`);
    } else if (after.stopped && !before.stopped) {
        console.log(`Story history replay stopped after ${after.passes} passes without full delivery `
            + `(${why}); it runs again after a server restart`);
    } else if (after.passes !== before.passes && after.pass) {
        console.log(`Story history replay pass ${after.passes}: serving ${after.key} from point 0 (${why})`);
    } else if (!after.pass && before.pass && !after.done) {
        console.log(`Story history replay: the reader went away; waiting for one`);
    }
}

// The second parameter carries "<H>;<from>;<to>;<id>" once the first
// placement sets it ("pending" before: no replay), and "<...>;done" once this
// server has replayed it. A failed read keeps the replay state as it is.
async function pollBackfill(ssm, { GetParameterCommand, PutParameterCommand }) {
    let bf;
    try {
        const out = await ssm.send(new GetParameterCommand({ Name: BACKFILL_PARAMETER }));
        bf = parseBackfill(out.Parameter && out.Parameter.Value);
    } catch (err) {
        console.warn(`Story backfill read failed (${err.name || err}); keeping replay ${replay.key || "off"}`);
        return;
    }
    const before = replay;
    replay = replayOnFact(replay, bf, Date.now(), anyReader(),
                          { loopSeconds: STORY_LOOP_SECONDS, nodes: [...historyAliases], changed: historyChangedNodes });
    logReplay(before, replay, "new window");
    lastFact = bf;
    if (replay.done && bf && !bf.done) await markBackfillDone(ssm, bf, PutParameterCommand);
}

// Every second: end a pass (complete, or start over on a gap) and, after the
// grace, mark the window done. Completion is judged against the items the
// pass's reader holds now.
async function tickBackfill(ssm, PutParameterCommand) {
    const before = replay;
    const items = readerItems(before.pass && before.pass.group);
    replay = replayTick(replay, Date.now(), { items });
    const why = replay.passes !== before.passes || (replay.stopped && !before.stopped)
        ? `${replayMissing(before, items)} expected values never went out through the reader's items` : "";
    logReplay(before, replay, why);
    if (replay.done && !before.done && lastFact && !lastFact.done) {
        await markBackfillDone(ssm, replay.bf, PutParameterCommand);
    }
}

// Write "<value>;done" so a later restart of the server or SFC does not replay
// this window again. Only this exact parameter is writable by the task; a
// failure is retried on the next poll (the in-memory state is already done).
async function markBackfillDone(ssm, bf, PutParameterCommand) {
    try {
        await ssm.send(new PutParameterCommand({
            Name: BACKFILL_PARAMETER, Value: backfillDoneValue(bf), Type: "String", Overwrite: true,
        }));
        lastFact = parseBackfill(backfillDoneValue(bf));
        console.log(`Story history replay done: ${bf.key}`);
    } catch (err) {
        console.warn(`Story backfill done mark failed (${err.name || err}); retrying next poll`);
    }
}

// SFC's history source is one OPC UA subscription over every history node: a
// READER. Its first history item starts a new pass from point 0 (SFC started,
// or restarted and lost what it had buffered); the values the server SENT
// through each of its items in PublishResponses are the evidence a pass
// delivered them (each item is recorded with its clientHandle and node, since
// SFC reads a Boolean through two items, one per channel); when its last
// history item goes (SFC gone, session timed out) the replay waits again, and
// another reader still subscribed (an overlapping old/new SFC) gets a new
// pass. A server restart loses the state too, so SFC, which must be
// restarted after it anyway, gets the whole replay again.
function watchHistoryReaders(server) {
    server.on("create_session", (session) => {
        session.on("new_subscription", (subscription) => {
            const group = subscription.id;
            subscription.on("monitoredItem", (monitoredItem, itemToMonitor) => {
                const nodeId = itemToMonitor && itemToMonitor.nodeId && itemToMonitor.nodeId.value;
                if (!historyAliases.has(String(nodeId))) return;
                const items = historyReaders.get(group) || new Map();
                items.set(monitoredItem, [monitoredItem.clientHandle, String(nodeId)]);
                historyReaders.set(group, items);
                const before = replay;
                replay = replayOnReader(replay, Date.now(), group);
                logReplay(before, replay, `reader ${group} subscribed`);
            });
            subscription.on("notification", (message) => {
                const items = historyReaders.get(group);
                if (!items) return;
                const handles = new Set([...items.values()].map(([handle]) => handle));
                replay = replayOnDelivered(replay, group, deliveredValues(message, handles));
            });
            subscription.on("removeMonitoredItem", (monitoredItem) => {
                const items = historyReaders.get(group);
                if (!items || !items.delete(monitoredItem) || items.size > 0) return;
                historyReaders.delete(group);
                const before = replay;
                replay = replayOnReaderGone(replay, group);
                const other = anyReader();
                if (other !== null) replay = replayOnReader(replay, Date.now(), other);
                logReplay(before, replay, `reader ${group} gone`);
            });
        });
    });
}

const supportedDataTypes = {
    'INTEGER': { type: DataType.Int32, transform: (v) => Number(v) },
    'DOUBLE':  { type: DataType.Double, transform: (v) => Number(v) },
    'BOOLEAN': { type: DataType.Boolean, transform: (v) => v === "True" },
    'STRING':  { type: DataType.String, transform: (v) => v },
};

const defaultValues = {
    [DataType.Int32]:   0,
    [DataType.Double]:  0.0,
    [DataType.Boolean]: false,
    [DataType.String]:  "(none)",
};

function getData(fullVariablePath, type) {
    const arr = data[fullVariablePath];
    if (!arr || arr.length === 0) return defaultValues[type];
    return dataIndex >= arr.length ? arr[0] : arr[dataIndex];
}

// The tag's DataValue now. While the story clock publishes nothing (waiting
// for the anchor, or held until the first placement, ADR-0092 decision 8)
// every tag reads BadWaitingForInitialData with no value; SFC forwards only
// Good values, so SiteWise records nothing for that time.
function currentDataValue(alias, dataType) {
    const now = new Date();
    if (!publishes(clock)) {
        return new opc.DataValue({
            sourceTimestamp: now, serverTimestamp: now, statusCode: opc.StatusCodes.BadWaitingForInitialData,
        });
    }
    return new opc.DataValue({
        sourceTimestamp: now,
        serverTimestamp: now,
        value: new opc.Variant({ dataType, value: getData(alias, dataType) }),
    });
}

// A story-mill leaf alias ".../Line3CNC1/Line3CNC1_<leaf>" and its HISTORY
// twin "<HISTORY_ROOT>/.../Line3CNC1/Line3CNC1_<leaf>". The replay publishes
// the replay row's value on the twin; the ordinary node keeps the live feed.
function isStoryMillLeaf(alias) {
    return BACKFILL_PARAMETER !== "" && alias.startsWith(`${STORY_MILL_PREFIX}_`);
}

function historyAlias(alias) {
    return `${HISTORY_ROOT}/${alias}`;
}

// The DataValue a HISTORY node serves. Outside the one-time replay (no window,
// waiting for a reader, the separator step, finished or done) it is
// BadWaitingForInitialData — SFC forwards only Good values, so nothing is
// written. During the replay it is the replay row's value for this leaf,
// stamped with the point's GRID epoch as the SourceTimestamp so SFC's
// "Channel"-level schedule lands it at its historical time. The ordinary
// (live) node is unaffected.
function historyDataValue(alias, dataType) {
    const now = new Date();
    const point = replayPoint(replay, now.getTime(), { loopSeconds: STORY_LOOP_SECONDS, dataSize });
    const arr = data[alias];
    if (!point || !arr || arr.length === 0) {
        return new opc.DataValue({
            sourceTimestamp: now, serverTimestamp: now,
            statusCode: opc.StatusCodes.BadWaitingForInitialData,
        });
    }
    const sourceTime = new Date(point.gridEpoch * 1000);
    const row = point.row >= arr.length ? arr[0] : arr[point.row];
    return new opc.DataValue({
        sourceTimestamp: sourceTime,
        serverTimestamp: now,
        value: new opc.Variant({ dataType, value: row }),
    });
}

// ---------------------------------------------------------------------------
// Import dataset CSV — populates `data` and discovers the address-space
// structure dynamically.
//
// Returns a Map<alias, dataType> for every unique tag found.
// ---------------------------------------------------------------------------
async function importDataset(fileName) {
    const tags = new Map();  // alias -> DataType enum

    // Defense-in-depth (detect-non-literal-fs-filename): confine the read to
    // a file in the working directory — no path traversal. The arg is already
    // fixed to "dataset.csv" by package.json/server.sh inside the container.
    const fileStream = createReadStream(basename(fileName));
    const rl = createInterface({ input: fileStream, crlfDelay: Infinity });

    for await (const line of rl) {
        const tokens = line.split(',');
        const alias = tokens[0];
        if (!alias.includes('/')) continue;           // skip header

        const typeKey = tokens[1];
        const typeInfo = supportedDataTypes[typeKey];
        if (!typeInfo) continue;                      // unsupported type

        const value = typeInfo.transform(tokens[5]);

        if (!data[alias]) {
            data[alias] = [];
            tags.set(alias, typeInfo.type);
        }
        data[alias].push(value);
    }

    dataSize = Math.max(0, ...Object.values(data).map(a => a.length));
    console.log(`Loaded ${tags.size} unique tags, max depth ${dataSize} samples`);
    return tags;
}

// ---------------------------------------------------------------------------
// Build the OPC UA address space dynamically from the tag map.
//
// Each alias like "AnyCompany/Milwaukee/Bottling/Line1/Line1Filler/Filler_Good"
// is split on '/'.  Intermediate segments become Folders; the final segment
// becomes a Variable.
// ---------------------------------------------------------------------------
function constructAddressSpace(server, tags) {
    const addressSpace = server.engine.addressSpace;
    const namespace    = addressSpace.registerNamespace('ManufacturingPlatform');
    const objectsFolder = addressSpace.rootFolder.objects;
    const accessLevel  = opc.accessLevelFlagToString(opc.AccessLevelFlag.CurrentRead);

    // Cache of created folder nodes keyed by their full path string.
    const folders = new Map();

    function getOrCreateFolder(parentNode, parentPath, name) {
        const fullPath = parentPath ? `${parentPath}/${name}` : name;
        if (folders.has(fullPath)) return { node: folders.get(fullPath), path: fullPath };

        const node = namespace.addFolder(parentNode, {
            browseName: name,
            nodeId: { identifierType: opc.NodeIdType.STRING, namespace: namespace.index, value: fullPath },
        });
        folders.set(fullPath, node);
        return { node, path: fullPath };
    }

    for (const [alias, dataType] of tags) {
        const parts = alias.split('/');
        if (parts.length < 2) continue;

        // Walk the path, creating folders for every segment except the last.
        let currentNode = objectsFolder;
        let currentPath = '';
        for (let i = 0; i < parts.length - 1; i++) {
            const seg = parts[i];
            const result = getOrCreateFolder(currentNode, currentPath, seg);
            currentNode = result.node;
            currentPath = result.path;
        }

        // Last segment is the variable (leaf tag).
        const varName = parts[parts.length - 1];

        namespace.addVariable({
            componentOf: currentNode,
            browseName: varName,
            nodeId: { identifierType: opc.NodeIdType.STRING, namespace: namespace.index, value: alias },
            dataType: dataType,
            minimumSamplingInterval: UPDATE_INTERVAL_MS,
            accessLevel: accessLevel,
            value: {
                timestamped_get: () => currentDataValue(alias, dataType),
            },
        });

        // The story mill's HISTORY twin (the replay ADR): a parallel node
        // under its own root folder (outside the site's branch) that the
        // one-time replay drives, read by SFC's second source. Created only
        // when the backfill parameter is set and only for the story mill's
        // leaves SFC reads (not the constant identity strings, see
        // clock.mjs hasHistoryTwin), so every other node and every other
        // server is unchanged. Its minimum sampling interval is 1 s so SFC's
        // faster history schedule can keep up with the paced replay.
        if (isStoryMillLeaf(alias) && hasHistoryTwin(data[alias], dataType === DataType.String)) {
            const hAlias = historyAlias(alias);
            historyAliases.add(hAlias);
            historyColumns.push([hAlias, data[alias]]);
            const hParts = hAlias.split('/');
            let hNode = objectsFolder, hPath = '';
            for (let i = 0; i < hParts.length - 1; i++) {
                const r = getOrCreateFolder(hNode, hPath, hParts[i]);
                hNode = r.node; hPath = r.path;
            }
            namespace.addVariable({
                componentOf: hNode,
                browseName: hParts[hParts.length - 1],
                nodeId: { identifierType: opc.NodeIdType.STRING, namespace: namespace.index, value: hAlias },
                dataType: dataType,
                minimumSamplingInterval: 1000,
                accessLevel: accessLevel,
                value: {
                    timestamped_get: () => historyDataValue(alias, dataType),
                },
            });
        }
    }

    console.log(`Address space built: ${folders.size} folders, ${tags.size} variables`);
}

// ---------------------------------------------------------------------------
// Re-derive the data index from the clock — called on a timer. Not a +1:
// an increment drifts with timer jitter and restarts from row 0, which would
// desynchronise the server from the simulator's view of the same loop.
// ---------------------------------------------------------------------------
function advanceDataIndex() {
    dataIndex = currentRow();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
(async () => {
    const port = parseInt(process.env.OPC_UA_PORT || "26543", 10);
    // Listen address. Unset (the container default) keeps node-opcua's
    // all-interfaces listen, which the in-VPC SFC reader needs. Tests set
    // 127.0.0.1 so a developer or CI host never exposes the server.
    const host = process.env.OPC_UA_HOST || undefined;
    // Defense-in-depth (detect-non-literal-fs-filename): the arg is fixed to
    // "dataset.csv" by package.json/server.sh inside the container, but
    // basename() confines any override to a file in the working directory
    // (no path traversal).
    const datasetFile = basename(argv[2] || "dataset.csv");

    const server = new opc.OPCUAServer({
        port,
        host,
        resourcePath: "/UA/ManufacturingServer",
        buildInfo: {
            productName: "ManufacturingPlatformOPCUA",
            buildNumber: "1000",
            buildDate: new Date(2026, 2, 20),
        },
    });

    await server.initialize();
    console.log("Server initialized");

    console.log(`Loading dataset from ${datasetFile}...`);
    const tags = await importDataset(datasetFile);

    constructAddressSpace(server, tags);
    if (BACKFILL_PARAMETER) watchHistoryReaders(server);

    // Serve the clock's row from the first read, then re-derive it every
    // UPDATE_INTERVAL_MS (default 5 s). With a story anchor parameter the
    // first poll completes before the server starts listening.
    await startAnchorPolling();
    dataIndex = currentRow();
    console.log(`Dataset loop ${dataSize * UPDATE_INTERVAL_MS / 1000} s; ${describeClock(clock)}; starting at row ${dataIndex}`);
    setInterval(advanceDataIndex, UPDATE_INTERVAL_MS);

    server.start(() => {
        console.log(`OPC UA server listening on port ${port}`);
        console.log(`Data update interval: ${UPDATE_INTERVAL_MS} ms`);
        const ep = server.endpoints[0].endpointDescriptions()[0].endpointUrl;
        console.log(`Endpoint: ${ep}`);
    });
})();
