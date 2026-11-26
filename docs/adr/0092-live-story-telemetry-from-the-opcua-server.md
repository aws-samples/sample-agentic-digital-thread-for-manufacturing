<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0092: The live story's telemetry comes from the story site's OPC UA server, on the plant clock

## Status

**Proposed: 2026-10-01.** Implemented. Verified offline (unit lane, ERP simulator lane against PostgreSQL 16, root
and packaging tests, a local server run) only; the checks still open
are listed under [Not yet verified in a deployed account](#not-yet-verified-in-a-deployed-account).

Supersedes [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md)
decisions 0, 1, 3 (as the source of the SiteWise values), 4, 8 and 9: no
Lambda function writes SiteWise in a live account. Keeps ADR-0088 decisions 2,
5 and 6 (the grid, the alarm-stamped order, the alarm-aligned checks) and
decision 7 (the feed button is inert in a live account). Amends
[ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md) (the story
site's server clock) and [ADR-0089](0089-placeable-and-packaged-story-clock.md)
(what a placement does to the telemetry).

## Context

The product owner's direction (2026-09-22, recorded in
[ADR-0083](0083-indianapolis-fourth-site.md)) is that telemetry originates
from the OPC UA servers and that a Lambda writing SiteWise is not a valid
pattern for the demo or the workshop. ADR-0088 made an exception for the live
workshop story because the workshop profile shipped no OPC UA server. That
premise no longer holds: the live package now ships the four per-site OPC UA
servers and SFC, and SFC writes the
story mill `5-Axis-Milling-CMX-441` under AnyCompany / Indianapolis /
Machining / Line3. Keeping the Lambda writer as well would put two writers on
one asset.

The OPC UA server played its dataset on the wall clock. The live story's CMMS
order and MES checks follow the stored anchor
(`simulation_config.story_anchor_epoch`, ADR-0085), which an attendee or a
facilitator can place (ADR-0089, ADR-0091). The server has no database access.

## Decisions

1. **The story site's OPC UA server is the only source of the live story's
   telemetry.** SFC reads it into SiteWise. The minute story step's SiteWise
   publisher (`erp_simulation/story_telemetry_step.py`,
   `story_sitewise.py`, `story_telemetry.py`), its sweep and its
   `iotsitewise:*` grants on the simulation role are removed. A live
   package turns the OPC UA servers and SFC on
   (`workshop_engine_stack.telemetry_enabled`), and so does
   `deploy.sh --workshop --workshop-story live` for that deploy (implied,
   not persisted, as the story flag itself is not sticky).

2. **The server picks its row from a pure clock module.**
   `highbyte/components/opc-server/clock.mjs` holds the rule:
   - Wall clock (every other site, every demo and frozen deploy): row =
     epoch mod loop / interval, unchanged (ADR-0073).
   - Story anchor (the story site on a live synth): the loop starts at the
     anchor and lasts the packaged loop length (`STORY_LOOP_SECONDS`,
     default 28800 s). The dataset is generated on the 8 h loop, so a
     shorter loop plays it time-scaled: every phase boundary lands at the
     same fraction of the loop as the condition model's. Before the anchor
     the mill reads healthy (row 0), except inside the history loop (the
     loop containing anchor minus one day), the simulator's own rule.
   - `agentic_digital_thread/tests/unit/test_opcua_story_clock.py` runs the
     module in node and checks every step of five loop lengths against
     `erp_simulation/condition_monitor.py`. On the 8 h loop the row is the
     model's step exactly.

3. **The anchor reaches the server through one SSM parameter, a derived
   copy.** A live synth creates `/adt/workshop/story-anchor-epoch` with the
   value `pending`
   (`stacks/erp_lambda_stack.py::_create_story_anchor_mirror`). Each
   simulator cycle, after the story step and the condition monitor,
   `erp_simulation/story_anchor_mirror.py::sync` copies the clock into it:
   `present` writes the anchor, `absent` writes `0` (wall clock), `unknown`
   (the read failed) writes nothing. It writes only when the value differs,
   so a failed write heals on the next cycle. The story site's server polls
   it every 30 s with `@aws-sdk/client-ssm` (pinned, the release train the
   HighByte setup Lambda already uses); `pending` publishes no value
   (decision 8; it served the healthy row before), a
   failed read keeps the clock in use (never a guessed wall clock). The task
   role may read exactly that parameter; the simulation role may read and
   overwrite exactly that parameter. The database row stays the one source
   of truth and the parameter is never edited by hand. A placement reaches
   the sensor within about 90 s.

   Rejected: the server reading the database (it would hold the ERP owner
   credential for one row), hand-rolled request signing instead of the SDK,
   restarting the service with the anchor as an environment variable (gaps
   and wider IAM), and pushing the anchor to a listener on the server
   (an unauthenticated port).

4. **The feed confirmation reuses the existing rows.** After a sync that
   leaves the parameter equal to the anchor, the simulator records it in
   `story_telemetry_anchor` and the grid point in
   `story_telemetry_cursor_epoch`, compare-and-set on the stored anchor
   (`story_anchor_mirror.confirm`). The story history and every placement
   reset both, so the lookup's `telemetry.backfill_complete` (name kept for
   the API) now means "the sensor feed follows the current anchor". The
   Workshop tab says "Sensor feed follows the clock"; the preflight check is
   unchanged.

5. **SiteWise shows what the sensor reported, from the moment SFC starts.**
   Nothing backfills it. Yesterday's failure, the deferred PM and the
   bearing replacement live in the CMMS and MES records the history writer
   seeds; SiteWise shows the climb from deploy onward. A placement cannot
   rewrite what SiteWise already recorded, so the server CATCHES UP to the
   new clock instead of jumping to it (decision 7). The dataset's baseline
   is about 1.0 mm/s on the X axis (the Lambda used 1.8). The X axis reads
   about 2.35 mm/s at the BPFO alarm and about 2.8 mm/s just before the
   bearing fails. The spectral tags carry the generator's ±3 % sensor noise,
   so the BPFO band in SiteWise agrees with the "0.70 mm/s" in the order and
   the check notes to within that noise, not to 2 dp.

6. **The feed button and its Lambda are unchanged for now.** The button stays
   inert in a live account (ADR-0088 decision 7). The
   `erp-sitewise-degradation-feed` Lambda still ships on every `--simulator`
   synth for the frozen story and demo deploys; it is removed once this path
   passes on a fresh Workshop Studio deployment (the order agreed on
   2026-09-23). That removal starts with the live story: a live synth
   packages no feed Lambda and no grant to it, and sets
   `OT_FEED_SOURCE=opcua` on the dashboard container so the OT row shows its
   caption without reading the database (the packaging gate pins SFC as the
   only SiteWise value writer in the live package).

7. **A placement pulls the alarm forward; it never steps the sensor.**
   (Added 2026-10-02.) The first sign-in places the clock with the alarm
   about 80 minutes out, which on the 8 h loop is 4 h 05 min into the wear.
   The server had been playing the deploy anchor since the account came up,
   so jumping to the new position would put a step into the SiteWise trend
   (measured: about 1.4 to 2.0 mm/s on the X axis for an account that was up
   90 minutes). Instead:
   - Every re-plan that moves the anchor records
     `story_feed_catch_up = "<previous anchor>;<start>;<end>"`
     (`story_replan.catch_up_row`): `start` is two minutes after the
     placement (`CATCH_UP_LEAD_SECONDS`, time for the mirror and the
     server's poll), `end` is the new anchor's first BPFO alarm after
     `start`. A re-plan onto the same anchor leaves it alone; the story
     Delete clears it.
   - The mirror appends it to the parameter until `end`:
     `"<anchor>;<previous anchor>;<start>;<end>"`.
   - `clock.mjs::catchUpRow`: before `start` the server keeps the previous
     anchor's row; from `start` to `end` it plays linearly from that row to
     the new anchor's alarm row; from `end` it is the new anchor's row. A
     move back (the previous row is already past the target, as in a reset
     after the alarm) jumps. Stateless, so a restart mid-catch-up serves the
     same row.
   - The records are unaffected: the `VIB-CMX` order and the `DIMENSIONAL`
     checks only start at the alarm, and the server serves exactly the
     model's alarm row at the alarm instant and the model's row after it.
     The chip, the lookup and the placement API are unchanged.

   Rejected: a 2 h packaged loop with the wear starting at sign-in (no step,
   but the alarm window shrinks from 107 to 27 minutes, so a table running
   late misses it, and every phase quote changes), and accepting the step
   (the trend question in module 6 would read it).

8. **Until the first placement, a package that auto-places publishes no
   story-site telemetry.** (Added 2026-10-02.) Workshop Studio provisions
   accounts 24 h before doors, so on the deploy anchor SiteWise already held
   about three full cycles by sign-in: alarms, failures and 0.01 repair dips
   that the placement's CMMS and MES records no longer explain, since the
   placement retires that era's records but cannot rewrite SiteWise. The
   history the placement writes makes it worse: its failed batch sits about
   22.5 h before the placement, so on a 24 h account that window is AFTER
   SFC started, and a live walk's root-cause answer for the failed batch
   (2026-10-02) quoted whatever the sensor history held there beside MES
   checks that cite BPFO 0.87 to 1.14 mm/s. On the deploy clock that is an
   unrelated phase (simulated: X 1.37 to 1.80 mm/s); a published healthy row
   would be 0.99 mm/s; either contradicts the checks. Decision 7 removed the
   step at the placement, not that history. Now:
   - A live synth with an auto-place policy also sets
     `STORY_FEED_HOLD_UNTIL_PLACED=1` on the simulation Lambda
     (`stacks/erp_lambda_stack.py::_create_story_anchor_mirror`; yes or no
     only, the minutes stay on the compute task as the packaging gate
     requires). A synth without a policy (`--no-auto-place`, or
     `--story-anchor` without `--auto-place-minutes`, or a plain
     `deploy.sh --workshop-story live`) does not set it.
   - Each cycle the mirror reads `story_anchor_source` with the catch-up
     (`story_anchor_mirror.read_feed_state`). With the flag set and a
     source that nobody placed (`deploy` or `packaged`, the sources the UI
     auto-places from) it writes `"<anchor>;healthy"`. `clock.mjs` reads
     that as HELD and `server.mjs` answers every tag with
     `BadWaitingForInitialData` and no value (`clock.mjs::publishes`); the
     same goes for WAITING (`pending` or an unusable value), which used to
     serve row 0. SFC's OPC UA adapter forwards only Good values: in
     SUBSCRIPTION mode, the default our `sfc_config` leaves in place,
     `OpcuaSource.onSubscribedDataReceived` stores a value only when
     `statusCode.isGood`; in POLLING mode `serverReadsInPollingMode` logs an
     error and skips it. So SiteWise records nothing for the story site until
     the placement, and a history read of an earlier window returns no points
     (`agents/sitewise_read.py` reads raw history with no quality filter, so
     it would pass a bad point through, but none arrives). An unreadable
     source leaves the parameter as it is. The feed confirmation still runs,
     so the Workshop tab and the preflight read "Sensor feed follows the
     clock".
   - Every re-plan that moves the anchor now records the previous era's
     source in the catch-up row (`"<previous anchor>;<start>;<end>;<source>"`,
     `story_replan.catch_up_row`). When that source was unplaced and the flag
     is set, the mirror sends the catch-up with a previous anchor of `0`
     (`HEALTHY_FROM`) and the server climbs from the healthy row (X 0.99,
     BPFO 0.05) to the new anchor's alarm row. `story_replan` only records
     the fact; the mirror applies the policy.
   - A Reset onto the packaged default puts the source back to `deploy` or
     `packaged`, so the account is held again and the server stops
     publishing (as decision 7 allows a move back to jump); the next page
     load auto-places it again.
   - Without the flag nothing changes after the first mirror: the server
     follows its anchor exactly as before. Frozen and demo deploys have no
     parameter at all.

   The server plays ONE row for the whole site, so while held every
   Indianapolis tag, not only the story mill's, publishes nothing: SiteWise
   has no Indianapolis data before the first sign-in. Holding only the
   story mill's tags would need a per-tag clock in `server.mjs`.

   What the deploy era still writes before the placement, and what the
   placement does with it:
   - `VIB-CMX-<loop>` condition orders at each deploy-era alarm: purged by the
     placement (`_purge_condition_orders_before`), since the auto-placed
     era has no alarm at or before the placement.
   - The story runs (`RUN-...-8<loop>`) and their `DIMENSIONAL` checks:
     retired with every child row (`retire_era`).
   - A `DIMENSIONAL` check that `story_live` adds to a simulator-scheduled
     order of the story part running on the story line in a deploy-era alarm
     window: NOT retired (the row is the simulator's, and the retire pins
     that). After the placement its notes cite a BPFO band of 0.7 to 1.2
     mm/s at an instant for which SiteWise has no data, and if that run
     shipped it can become the newest shipped failure. Proposed follow-up:
     hold the story step and the condition monitor under the same rule, so
     a held account writes no deploy-era story record at all.
   - The history records the placement writes (yesterday's failure at the
     new anchor minus one day) were never in SiteWise (decision 5); a read
     of that window now returns no points instead of an unrelated
     deploy-clock phase.

   Rejected: serving the healthy row while held (published). SFC's default
   subscription mode then stores one 0.99 point when it subscribes and
   nothing more while the value stays constant, so it mostly reads as no
   data too, but the latest value before sign-in is that stale point, every
   SFC restart adds another, and in polling mode (proposed once for SFC and
   reverted) it writes 0.99 every 5 s, which is
   the same contradiction with the failed batch's checks. Also rejected:
   provisioning accounts closer to doors (the 24 h preflight gate needs the
   lead time); rewriting or deleting SiteWise history at the placement (not
   possible, and it would put a writer other than the OPC UA server on the
   asset); holding on the anchor source alone (a package that never
   auto-places would never alarm while its CMMS order and checks appear);
   giving the simulation Lambda the auto-place minutes (the packaging gate
   keeps them on the compute task); and letting the re-plan decide where the
   server climbs from (the history writer would need the policy too).

## Invariants

- No Lambda function writes SiteWise in a live account; the story site's OPC
  UA server (through SFC) is the only source of its telemetry.
- `simulation_config.story_anchor_epoch` is the one anchor. The SSM
  parameter is a copy written only by the simulator's mirror.
- A server without `STORY_ANCHOR_PARAMETER` plays the wall clock and its
  template is unchanged.
- `clock.mjs` is pure and pinned against `condition_monitor` by test.
- A placement's catch-up never moves the sensor back and reaches the model's
  alarm row at the alarm instant (decision 7, pinned by the same test).
- An unplaced account whose package auto-places publishes no value, and its
  first placement climbs from the healthy row; without an auto-place policy
  the server follows its anchor (decision 8, pinned by the same test).

## Consequences

- A live account runs four more Fargate services plus SFC, so provisioning
  takes longer and costs more. The SFC asset tree's setup time on a fresh
  account is not yet measured.
- An attendee sees a shorter SiteWise trend when the account is young. How
  much trend is visible depends on how long before the session the account
  is ready.
- The frozen story and demo deploys still have one Lambda SiteWise writer
  (the feed button). Removing it is decision 6's follow-up.

## Not yet verified in a deployed account

- The server reads the parameter with the task role and follows a placement
  within about 90 s.
- SFC publishes CMX-441's `Spectrum_BPFO`, `BPFO_Alarm` and `Vibration_*`
  into the asset SFC created, and nothing else writes it.
- At the alarm, SiteWise `BPFO_Alarm` turns true within one SFC sample of the
  `VIB-CMX` order's `reported_date`.
- The Workshop tab and the preflight read "Sensor feed follows the clock"
  within about 2 minutes of a placement.
- After a first-sign-in placement the SiteWise X axis climbs to the alarm
  with no step (decision 7).
- On an account packaged with auto-place, the server logs "held until the
  first placement (no value published)" before sign-in, SiteWise holds no
  Indianapolis value before the first placement, and that placement climbs
  from 0.99 mm/s (decision 8). Verified offline only: the real `server.mjs`
  in a network-less container, with SSM stubbed, answered
  `BadWaitingForInitialData` while waiting and held and Good 0.99 after the
  placement, and a node-opcua subscription saw exactly those transitions.

## Amendment: a one-time history replay backfills the window (supersedes decision 5's "Nothing backfills it")

Decision 5 recorded that nothing backfilled the three days before a placement:
on a workshop account SiteWise and S3 Tables held no story-mill data until the
first sign-in placed the clock, so module 5's root-cause question found no
vibration around the failed batch and module 6's chart began only at sign-in.
The product owner asked for a backfill. This amendment adds one WITHOUT
breaking the decision-0 rule (telemetry comes from the OPC UA servers; no
Lambda writes SiteWise).

**What happens.** On the first placement out of the HOLD the Indianapolis OPC
UA server replays the mill's history window once, on SEPARATE history nodes of
the story mill, paced so each point survives SFC's read. The history nodes sit
under their own root folder, `StoryHistoryReplay/<leaf alias>`, outside the
site's branch: HighByte's project reads each site as one Branch input
(`AnyCompany/Indianapolis`), and under the site the replay would show up in its
model and its subscriptions would restart the replay. SFC reads the history
nodes through a SECOND source on the same server whose schedule sets
`TimestampLevel: "Channel"`, so each point lands at its historical
`SourceTimestamp`. SFC stays the only SiteWise writer. The live 5 s feed on the
ordinary nodes is untouched during and after the replay, and SiteWise keeps the
newest value by timestamp, so historical points never clobber the latest value.

**The window.** On the anchor's 300 s grid, ending at `grid_floor(P)` (P the
placement) and starting at the latest of: three days before the history anchor
H (the story's depth), three days plus one loop before P (never deeper; a
placement normalises H into the loop before P), and the oldest time SiteWise
still accepts one day after P. At most 961 points per property, about 32 min at
the 2 s pace, and at most 3 d 8 h old at the placement: every point is inside
SiteWise's inclusive `[-7 d, +10 min]` ingest window for any replay sent within
3 d 16 h of the placement. An older anchor cuts the window; it never widens it.

**Order and rows.** The history loop (the loop containing H − 1 day) first, so
anything reading yesterday's failure gets its data within the first minutes,
then every other grid point; EACH segment oldest to newest. SFC subscribes each
node with queue size 1 and no filter (SFC v1.11.0 `OpcuaSource.kt:748-757`), so
the server's default StatusValue trigger reports a value only when it differs
from the last one reported (node-opcua `monitored_item.ts:286-290`): a Boolean
such as `BPFO_Alarm` reaches SiteWise only where it changes, and running each
segment forward makes those its real leading edges. One Bad step separates the
segments, so every channel also gets a point at the start of the second. Inside
the history loop each point is exactly the condition model's row (`BPFO_Alarm`
true across the alarm window, `Spectrum_BPFO` in its band under the seeded
`DIMENSIONAL` checks); everywhere else, and across [H, P), a healthy post-repair
row, so the live catch-up climbs from the healthy row with no step at the
placement and each point varies enough to pass the change trigger.

**Trigger, pacing and restarts.** `story_replan` records the window as a fact
(`story_feed_backfill = "<H>;<from>;<to>"`) on a full re-plan whose previous era
was unplaced — the record-only pattern of the catch-up. The anchor mirror copies
it to a SECOND SSM parameter, `/adt/workshop/story-feed-backfill`, as
`"<H>;<from>;<to>;<id>"`; `id` (recorded once as `story_feed_live_since`) is the
replay's identity, not its start. The server paces from its OWN observation:
a pass starts when it has seen the window and a reader (an OPC UA
subscription) monitors a history node, a NEW reader starts a new pass from
point 0 (a restarted SFC has lost what it buffered; SiteWise overwrites an
equal timestamp, so repeating loses nothing), and losing the reader puts the
replay back to waiting. A server restart loses that state, so the replay runs
again from point 0 once SFC reconnects (SFC must be restarted after a server
restart anyway).

**Completion is evidence, not elapsed time.** The server records which values
it actually SENT to the reader, item by item: the SourceTimestamp and the
clientHandle of each Good history value in the notifications of its
PublishResponses (node-opcua's subscription `notification` event fires only
when the client has a Publish request pending). Each history node owes a value
at every step where its OWN dataset value differs from its value at the step
before, and at the first step and the first step after the separator (both
follow a Bad value); a node whose value repeats is dropped by its item's change
filter and owes nothing there, and a step at which no node changes (the stopped
spindle) owes nothing at all. A pass is complete only when the reader holds a
monitored item on every history node and every owed value went out through
each of its items on that node (SFC reads a Boolean such as `BPFO_Alarm`
through two items, one per channel, and both must carry it), so a missing
`BPFO_Alarm` value at its leading edge is a gap even when every other node
carried the same epoch. The history nodes are exactly the story-mill leaves
SFC's history source reads: the mill's constant identity strings (AssetName,
EquipmentID, MaintenanceAssetNum) are no modelled measurement and get no twin.
Each pass opens with the history nodes Bad for one pace, so point 0 is a change
on every item of a reader that was served the pass before. A pass that
reaches its last step with a gap (an event-loop stall longer than a pace
skipped a step, the connection stopped carrying notifications while the
subscription stayed, or one item's value never went out) starts over from
point 0. A complete pass is done five minutes later (SFC's S3 Tables target
holds up to a minute of rows) if the same reader is still subscribed with the
items it was credited through; the server then writes
`"<value>;done"`, and the mirror keeps that mark for the same window, so a
later restart of either side does not replay it again. A reader that keeps
coming back is bounded: after five passes for one window the server stops
replaying it (logged; in memory only, so a server restart tries again) and
writes no mark. The task role may read and write exactly that one parameter.
An Admin Reset deletes both keys in the re-plan transaction, so the parameter
returns to `pending` and the next placement replays a fresh window with a
fresh identity from point 0.

**SFC load.** The history schedule writes only through its own two targets,
`sitewise-history-target` (the mill's 29 properties, sent at least every 5 s
so the replay's last partial batch is not left in the buffer) and
`s3tables-history-target` (28 mappings). SFC's SiteWise writer walks every
configured property, and its S3 Tables writer every mapping, for each value it
receives, so history entries in the live targets would have added that work to
every live 5 s read; the live targets, schedules and sources are exactly the
replay-off ones. A target of its own does cost memory: each SiteWise target
caches every asset model and asset of the account, and each S3 Tables target
loads its own copy of the S3 Tables writer libraries and opens its own Iceberg
REST catalog client. The second S3 Tables target could not be measured
offline, on a task that already peaked at 99.95 % of 4 GB, so with the replay
on the SFC task gets 8 GB at the same 1 vCPU (a valid Fargate size; no added
vCPU). `JAVA_TOOL_OPTIONS` is unchanged: 37.5 % of 8 GB is a 3 GB heap (and the
same default direct-buffer limit) with 5 GB beside it, both twice what ran at
4 GB. Replay off keeps the 4 GB task unchanged.

**S3 Tables.** The history target writes its own table, `sfc.readings_replay`:
the narrow schema plus `ingest_time` (SFC's clock when it handed the row on),
created only when the live story packages the replay. Append-only tables keep
every copy a repeated replay writes; `sfc_config.REPLAY_READINGS_SQL` keeps the
newest copy by `ingest_time` per (site, area, line, asset, channel,
event_time), which is the value SiteWise keeps. Nothing in the workshop reads
the mill's S3 Tables rows today (no module, onboarded system or golden reads
`sfc.readings`); anyone who does reads the replay through that query. The live
`readings` table is not touched (an Iceberg schema change replaces the table).

**Limits.**

- A re-placement (Admin → Workshop "Move the bearing alarm", or
  `scripts/place_story_clock.py`) out of another UNPLACED era records a fresh
  window and replays it; a move out of an already-placed era records none
  (SiteWise already holds that history).
- Any OPC UA client that subscribes to a history node starts a new pass. Only
  SFC's history source does; a client that kept subscribing would use up the
  five passes, and the replay would stop without a mark.
- The evidence is that the server SENT every owed value to SFC through each of
  its items, not that SFC's targets wrote it: a target that silently drops what
  it was handed while SFC stays subscribed would still let the window be marked
  done. SiteWise's own history is the check (the live check reads it).
- "Sent" is transport evidence only: the `notification` event fires once the
  server has a Publish request to answer, before the PublishResponse goes out,
  and the five-minute grace with the same reader stands in for a client
  receipt; the server does not track Publish sequence acknowledgements.
- Values that do not change from one point to the next are dropped by the
  change trigger (Booleans, integers), as in the live feed; SiteWise step
  semantics make that harmless.

**Switchable (decision 8 follow-up).** A synth-time context flag
`story_feed_backfill` (default ON for the workshop live profile, live-only)
gates the whole feature: off packages no second SSM parameter, no history
nodes, no SFC history source or targets and no replay table — a byte-identical
template. No new public endpoint, no new external fetch, no new IAM wildcard
(the sim Lambda reads and writes exactly the one new parameter, and so does the
Indianapolis task). Fargate vCPU budget unchanged (the SFC task's memory rises
to 8 GB at the same 1 vCPU, see SFC load).

**Verified offline** (no account). A local rig ran the real `server.mjs` for
all four sites, the real SFC v1.11.0 (built from `docker/sfc/Dockerfile`,
1 vCPU / 4 GB like the task) twice side by side, replay off and replay on,
each writing to a mock SiteWise that keeps the newest value per timestamp, on
an internal Docker network. The S3 Tables targets were left out of both (they
need the S3 Tables Iceberg catalog). Through SFC's real subscription and
change filter, `BPFO_Alarm` was true at all three seeded failed-batch checks
and false from the failure; SFC restarted mid-replay and the replay ran again
from point 0, delivering every point except exact repeats of the previous
step (the stopped spindle's zeros); the replay-off SFC wrote no historical
point; the live 5 s feed kept its 5 s cadence and the latest value stayed the
live one. The same rig found that SFC's SiteWise target only sends a partial
batch on a timer when `Interval` is set, so the history target sets it to 5 s
(without it the last eight points never arrived). Replay on against replay
off: container memory +22 MiB (mean 418 vs 396 MiB), JVM committed +37 MiB
(428 vs 391 MiB; metaspace +15 MiB, since SFC loads each target with its own
class loader, heap after GC +6 MiB), CPU mean 4.1 % vs 3.3 % of one vCPU. A
second S3 Tables target was not measured; it loads its own copy of the much
larger S3 Tables target libraries and opens its own Iceberg catalog client, so
expect several times the SiteWise target's cost.
Unproven until a live check: SiteWise accepting backdated points behind newer
values across the window, the S3 Tables rows of the replay table, and the SFC
task's real memory on a workshop account. The rows, order, window and pacing
are pinned by `tests/unit/test_opcua_history_replay.py` against
`condition_monitor` and `story_history`.
