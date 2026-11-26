<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0088: One story clock: the live story's SiteWise telemetry is computed from the condition model on the stored anchor

## Status

**Amended: 2026-10-04** by [ADR-0095](0095-two-story-clocks-live-anchor-beside-history-anchor.md): the live side may follow a second, live-only anchor (`story_live_anchor_epoch`) so the bearing alarm can move without rewriting the history; absent that key, everything below is unchanged.

**Superseded in part: 2026-10-01.** Decisions 0, 1, 3 (as the source of the
SiteWise values), 4, 8 and 9 are superseded by
[ADR-0092](0092-live-story-telemetry-from-the-opcua-server.md): the live
story's telemetry comes from the story site's OPC UA server through SFC, and
no Lambda writes SiteWise. Decisions 2, 5, 6 and 7 stand.

**Proposed: 2026-09-29.** Implemented. Verified offline only (unit lane,
ERP simulator lane, PG-gated tests); the checks still open are listed under [Not yet verified in a deployed account](#not-yet-verified-in-a-deployed-account).
Decision 0 recorded an exception to the [ADR-0083](0083-indianapolis-fourth-site.md)
direction of 2026-09-22; [ADR-0092](0092-live-story-telemetry-from-the-opcua-server.md)
removed it.

Amends [ADR-0083](0083-indianapolis-fourth-site.md) decisions 5 and 6 (the
feed button is inert in a live account; its removal condition is unchanged),
[ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md) decisions 4
and 5 (the monitor stamps its order with the alarm instant and the alarm-step
features) and [ADR-0085](0085-live-story-clock-history-and-lookup.md)
decision 2 (alarm-aligned check slots) and its known limit "OPC UA playback
and the SiteWise feed are wall-clock" (resolved for SiteWise in the live
story). Companion: [ADR-0089](0089-placeable-and-packaged-story-clock.md)
(how the anchor is placed).

## Context

ADR-0085 put the live story's CMMS and MES on a stored anchor,
`simulation_config.story_anchor_epoch`, but left the telemetry on the wall
clock. A live account therefore showed two clocks in module 6: the
condition-monitor order `VIB-CMX-*` and the failing `DIMENSIONAL` checks
followed the anchor, while SiteWise showed whatever the last click of the
dashboard's degradation-feed button wrote (a 72 h ramp ending at the click,
re-anchored on every click). The sensor, the work order and the quality
check told the same story at different times and with different numbers.

ADR-0083 (2026-09-22) set the direction that telemetry originates from the
OPC UA servers and that "writing telemetry into SiteWise from a Lambda
function is not a valid pattern for the demo or the workshop". That path
needs HighByte (`--highbyte`) or the separate `opcua_enabled` flag, and
**the workshop profile ships neither**: no OPC UA server, no HighByte
(ADR-0083 decision 7; the flag does not exist on this branch). In a workshop
account the only SiteWise writer has always been a Lambda function, the
`erp-sitewise-degradation-feed` behind the dashboard button, which ADR-0083
decision 6 kept as the documented fallback. The choice for the live workshop
story was therefore between a Lambda publisher on the story clock and a
Lambda publisher on the wall clock, not between a Lambda and OPC UA.

## Decisions

0. **An exception to the ADR-0083 direction, scoped to the live workshop
   story.** The anchored writer is a Lambda publishing into SiteWise: the
   ERP simulation Lambda's minute story step. It runs only while the
   anchor row exists (the live story; ADR-0085 decision 1), and its IAM
   exists only on a `live` synth (decision 8). The OPC UA -> HighByte ->
   SiteWise path of `--highbyte` deploys, the frozen story and every demo
   deploy are unchanged, and the ADR-0083 direction stands for them. The
   exception makes the existing fallback coherent (one clock) rather than
   adding a new pattern. It is recorded here because it deviates from a
   stated direction.

1. **The publisher is the minute story step, not the feed Lambda.**
   `SimulationOrchestrator._run_story_telemetry`
   (`erp_simulation/simulation_orchestrator.py`) runs after
   `_run_story_step` and the condition monitor, BEFORE the simulator steps
   ([ADR-0090](0090-simulation-cycle-isolation-and-collision-free-numbers.md)),
   in both `start()` and `run_single_cycle()`, only when
   the clock state is `present`, on its own best-effort connection, and
   calls `erp_simulation/story_telemetry_step.py::run`, which never raises.
   The boto3 `iotsitewise` client is built on first use (region from
   `REGION`, else `AWS_REGION`). The feed Lambda could not do this job: it
   has no VPC or database access, so it cannot read the anchor.

2. **One epoch-aligned 300 s grid, and congruent anchors.**
   `erp_simulation/condition_monitor.py` adds `TELEMETRY_GRID_SECONDS = 300`
   (the feed's grid), `ALARM_OFFSET_SECONDS = BPFO_ALARM_STEP * INTERVAL_SEC`
   (3898 x 5 s = 19490 s), `ANCHOR_RESIDUE = (-ALARM_OFFSET_SECONDS) % 300`
   (10), `alarm_epoch`, `fail_epoch`, `grid_floor` and `snap_anchor(epoch,
   "down" | "nearest")`. A congruent anchor (`anchor % 300 == 10`) puts every
   loop's BPFO alarm instant `anchor + 19490 + 28800 * n` on a grid point,
   so the alarm, each check slot (`alarm + 900 * n`) and every telemetry
   timestamp coincide. The module still imports only `math` and `typing`
   (pinned).

3. **The telemetry is a pure function of (grid timestamp, stored anchor).**
   `erp_simulation/story_telemetry.py::sample(epoch, anchor)` reads the
   degradation fraction and spectral features off `condition_monitor` and
   returns ten streams of the SiteWise `CNC` model
   (`highbyte/DeploySitewiseModels.py`):

   | Property | Type, unit | Running (fraction `f` in [0, 1)) | Repair |
   |---|---|---|---|
   | `Vibration_X` / `_Y` / `_Z` | DOUBLE, mm/s | `(1.8 + 1.0 f + jitter(ts, 0.05))` x 1.0 / 0.92 / 0.85, 4 dp | `0.05` |
   | `CoolantTemp` | DOUBLE, °C | `68 + 16 f + jitter(ts + 1, 0.5)`, 2 dp | `68 + jitter(ts + 1, 0.5)` |
   | `Spectrum_1X`, `Spectrum_BPFO`, `Spectrum_BPFI` | DOUBLE, mm/s | the model's `one_x`, `bpfo`, `bpfi` (no jitter) | `0.0` |
   | `CrestFactor`, `Kurtosis` | DOUBLE, none | the model's `crest`, `kurtosis` (no jitter) | `0.0` |
   | `BPFO_Alarm` | BOOLEAN | the model's `bpfo_alarm` | `False` |

   The jitter is the feed's `sha256(str(ts))` rule, copied and
   parity-tested against `lambda-sitewise-feed/handler.py::_jitter`, as are
   the axis scales. `BPFO_Hz`, `BPFI_Hz` and `SpindleSpeed` are not
   published: nothing in the CMMS or MES cites them.

   **Before the anchor the telemetry follows the history (D4).**
   `story_step` returns the healthy baseline (step 0, fraction 0) before
   the anchor, EXCEPT inside the history loop
   `[history_loop_start(anchor), + 28800)` (the loop containing
   `anchor - 1 d`, the same loop `story_history` puts the failed batch,
   `3107` and `3108` in), which runs the model's degrade, alarm and repair.
   From the anchor on every loop runs the model. A 72 h read therefore
   shows: healthy at about 1.8 mm/s, the history loop's ramp and repair,
   healthy, then loop 0's ramp from the anchor.

   **Numbers on the grid.** At the alarm instant `Spectrum_BPFO` is 0.7002
   (the "0.70" in the work-order description and the check notes) and
   `BPFO_Alarm` is true; one grid point earlier it is false. `Vibration_X`
   reads about 2.59 at the alarm (plus or minus the 0.05 jitter) and spans
   about 1.76 to 2.82 while running; the repair reads 0.05 and a spectrum of
   0.0. **The highest `Spectrum_BPFO` on the grid is 1.1885, not 1.20**
   (correction recorded in Task A: the model's last pre-failure step is not
   a grid point). The healthy baseline is 1.8 mm/s here against 1.0 in the
   OPC UA dataset (ADR-0083 decision 5); the workshop ships no OPC UA, so
   no account shows both.

4. **Head plus a paced backfill sweep, with a CAS cursor (D3).** Each cycle,
   inside a 20 s budget (`CYCLE_BUDGET_SECONDS`), the step:
   - resolves the target BY NAME (`story_sitewise.resolve_target`:
     `ListAssetModels` for `CNC`, `ListAssets` for `5-Axis-Milling-CMX-441`,
     `DescribeAsset`, ACTIVE, all ten property ids), cached 600 s. It NEVER
     creates anything: the model and asset belong to the workshop seed CR
     and `deploy.sh`. Absent -> the cycle skips (`no_target`) with one
     warning per 600 s, and heals once the asset appears;
   - ensures the sweep state `story_telemetry_anchor` /
     `story_telemetry_cursor_epoch` in `simulation_config` (absent rows
     inserted `ON CONFLICT DO NOTHING`; a telemetry anchor that differs from
     the story anchor or a malformed cursor is reset by CAS to one grid
     step before `max(anchor - 3 d, now - 7 d + 1 h)`);
   - publishes the HEAD, the last 3 grid points at or before now (one
     request);
   - advances the SWEEP by at most 120 grid points after the cursor, one
     request per 10 points (10 entries x 10 values), stopping when the
     budget cannot cover the next request;
   - moves the cursor by ONE CAS (`UPDATE ... WHERE config_value = <read>
     AND EXISTS (telemetry anchor = <read>)`) to the last point actually
     written, never past a failed put. Once the sweep reaches the head, the
     cursor follows the head.

   3 d before the anchor plus at most 8 h after it is at most 960 points, so
   a fresh sweep completes in about 8 cycles. The writer mirrors the feed's
   limits (parity-tested): at most 10 entries per request and 10 values per
   entry, 0.4 s between requests, `ThrottlingException` retried up to 6 times
   with a doubling 1 s backoff and never dropped, a
   `TimestampOutOfRangeException` entry skipped and counted, any other error
   raised as `StorySitewiseError` (the step logs it, drops the target cache
   and leaves the cursor). The step holds no transaction open across the
   SiteWise calls.

5. **The monitor order is stamped with the alarm instant (D5).**
   `MESSimulator.insert_condition_work_order(seed_cursor, loop_no,
   alarm_epoch, fail_epoch)` writes `VIB-CMX-<loop % 1e6, 6 digits>` with
   `reported_date = target_start = alarm_epoch` and `sched_start =
   fail_epoch`, both naive UTC, and a description built from the features
   at `BPFO_ALARM_STEP` (so it always reads "BPFO band 0.70 mm/s"), `ON
   CONFLICT (wonum) DO NOTHING`. `monitor_asset_condition` calls it with
   `condition_monitor.alarm_epoch(now, anchor)`; the re-plan catch-up
   (ADR-0089) calls the same writer. On the wall-clock path (frozen, demo;
   anchor `0.0`) the timestamps move from the noticing cycle's `NOW()` to
   the loop's alarm instant: normally at most one cycle earlier, but after
   a simulator outage that ends mid alarm window the first cycle back stamps
   the order with the (past) alarm instant, backdated up to the whole window
   (`(BEARING_FAIL_STEP - BPFO_ALARM_STEP) * 5 s` = 6430 s, about 107 min).
   Frozen content does not quote them; demo and frozen are therefore NOT
   byte-identical to the pre-ADR `NOW()` stamps.

6. **DIMENSIONAL checks on alarm-aligned 15 min slots (D6).**
   `story_live.defect_check_slot(now, anchor)` is the latest `alarm + 900 *
   n` at or before now inside the `bpfo_alarm` phase (`None` outside it);
   `DEFECT_CHECKS_PER_WINDOW` is 8. Each check is stamped with its slot and
   fails and notes the BPFO of the slot's step, so every check value is a
   published `Spectrum_BPFO` point. `_INSERT_DEFECT_CHECKS_SQL` requires
   `mwo.actual_start_time <= slot` (a slot before the order started is
   skipped) and `NOT EXISTS (check_time >= slot)` (one per order per slot).
   The story run starts at `alarm + grid_floor(now - alarm)`, the current
   5 min slot counted from the alarm, which on the first alarm cycle is the
   alarm instant itself; so when LINE3 is idle at the alarm, check 0, the
   `VIB-CMX` `reported_date` and the story run's `actual_start_time` are the
   same second. A simulator order of the story part that is already
   RUNNING takes the checks instead, from the first slot at or after its
   start.

7. **The feed button is inert in a live account, keyed by DATA (D7).**
   `streamlit_dashboard.render_ot_feed_controls` calls
   `_story_clock_active(conn)` (a `story_anchor_epoch` row exists) on each
   render; when true it shows the caption "This plant's sensor feed follows
   the plant clock. See the workshop answer key." instead of the button. A
   database error falls back to the button, so frozen and demo never lose
   it. The dashboard never reads `workshop_story`. The feed Lambda itself
   is unchanged and still deployed on every `--simulator` synth; ADR-0083
   decision 6's removal condition (a validated live OPC UA -> SiteWise test
   event) has not been met.

8. **Least-privilege IAM, live synths only (D8).**
   `stacks/erp_lambda_stack.py::_grant_story_telemetry` adds two statements
   to the simulation Lambda's role only when `workshop_story` is `live`:
   `iotsitewise:ListAssetModels`, `ListAssets`, `DescribeAsset` on `*`
   (read-only; List* is not ARN-scopable) and
   `iotsitewise:BatchPutAssetPropertyValue` on
   `arn:<partition>:iotsitewise:<region>:<account>:asset/*`. No
   `CreateAsset`. Frozen and engine templates carry no `iotsitewise:*` on
   that role (packaging gate). The calls leave the VPC through the existing
   NAT gateway.

9. **Out-of-window points are accepted, never deleted (D16).** Every point
   inside a republished window lands on the same grid timestamps, and
   SiteWise upserts replace them, so a republish leaves no off-grid residue.
   Points older than the republished window predate `anchor - 3 d`, outside
   any 72 h read ending now. No delete API is used (`DeleteTimeSeries`
   removes a whole stream; a bulk history delete was not verified to exist).

## Invariants

- The live story has ONE clock: SiteWise telemetry, `VIB-CMX` orders and
  `DIMENSIONAL` checks all derive from `condition_monitor` on the stored
  anchor, on the epoch 300 s grid (`anchor % 300 == 10`).
- The minute story step is the only live SiteWise publisher; nothing
  publishes from a custom resource.
- `story_telemetry` is pure (no I/O, no RNG); `story_sitewise` never creates
  a model, asset or property.
- The feed button is keyed by the anchor ROW, never by the story setting.
- SiteWise IAM on the simulation role exists only on a `live` synth.

## Consequences

- Module 6 reads one story: the SiteWise chart, the CMMS order and the MES
  checks agree to the number (2 dp) and to the second.
- A fresh live account shows telemetry only once the `CNC` asset exists
  (the workshop seed CR in the root stack, after the engine children); the
  sweep then fills the 72 h window in about 8 minutes.
- An account whose anchor predates this change is not congruent. Its
  telemetry and checks still follow the same model, but the alarm falls
  between grid points, so values agree to one 5 min slot rather than
  exactly, until the clock is placed (ADR-0089, which always snaps).
- MES check timestamps are built with `datetime.fromtimestamp` (host local
  time) while the CMMS order uses explicit UTC; they agree on the Lambda,
  which runs in UTC. A non-UTC host running the simulator locally would
  shift the MES stamps by its offset.
- Invoking `erp-sitewise-degradation-feed` directly in a live account would
  still overwrite the grid with a wall-clock ramp until the next sweep; only
  the dashboard button is hidden.

## Not yet verified in a deployed account

- SiteWise accepts the batches as built (boolean `BPFO_Alarm`, 10 x 10
  entries, pacing) and the sweep completes within about 15 minutes in a
  real account.
- `get-asset-property-value-history` on `Spectrum_BPFO` over 72 h: every
  timestamp `% 300 == 0`, about 864 points, the history loop peaking at
  1.1885 then reading 0.0 in the repair, loop 0 ramping from the anchor.
- At the alarm: `VIB-CMX-000000` with `reported_date` equal to the alarm,
  the story run's `actual_start_time` and first check at the same instant,
  and each later check's BPFO equal to SiteWise `Spectrum_BPFO` at that
  timestamp to 2 dp.
- The frozen account: the simulation role has no `iotsitewise:*`, no
  story-telemetry log line, and the button still writes its series.
- Module 6 answers in fresh chats (the SiteWise pill's latest point within
  5 minutes of now).

## Verification (offline)

`erp_simulation/tests/test_story_telemetry.py` (grid alignment, congruent
alarm on the grid, the table's values, pre-anchor shape, determinism, jitter
and axis-scale parity with the feed, `history_loop_start` equals
`story_history.story_windows`), `erp_simulation/tests/test_story_sitewise.py`
(caps, `booleanValue`, throttle retry, out-of-range skip, no `create_asset`,
missing asset returns `None`), `erp_simulation/tests/test_story_telemetry_step.py`
(head of 3, sweep advance and stop, CAS no-op on anchor mismatch, budget,
never raises, no SiteWise call without an anchor),
`erp_simulation/tests/test_story_live.py` (PG-gated: 8 checks at exactly
`alarm + 900 n`, check 0 equals the `VIB-CMX` `reported_date`, each check's
BPFO equals `story_telemetry.sample` to 2 dp),
`erp_simulation/tests/test_dashboard_ot_feed.py`,
`agentic_digital_thread/tests/unit/test_condition_monitor.py`
(`ANCHOR_RESIDUE == 10`, import purity), `test_mes_cmms_bridge.py`
(`reported_date` equals the alarm on both clocks) and
`tests/test_erp_lambda_story_history.py` (the SiteWise statements only under
`live`, exact actions and resources).
