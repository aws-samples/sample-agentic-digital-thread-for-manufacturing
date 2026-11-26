<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0085: The live workshop story runs on a data-keyed anchor, starts from a deterministic deploy-time history, and is looked up by facilitators through one steward-gated GET

## Status

**Accepted: 2026-09-27.** Decisions 1 to 5 are implemented, and so is
decision 6, the facilitator lookup (`agents/workshop_story_routes.py`,
`agents/demo/story_lookup.py`), pinned by
`tests/unit/test_workshop_story_lookup.py` and
`tests/unit/test_workshop_story_routes.py`. Not yet verified in a
deployed account.

> **2026-09-29 notes (one story clock).** The body below is unchanged.
> (a) [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md)
> amends decision 2: `DIMENSIONAL` checks land on alarm-aligned slots
> `alarm + 900 * n` (n = 0..7, exactly 8 per window, the first AT the alarm
> instant), each stamped with its slot and the BPFO of the slot's step; a
> slot before the work order's `actual_start_time` is skipped, and the story
> run starts at the current 5 min slot counted from the alarm (the alarm
> instant on the first alarm cycle). The "7-8 checks, a late cycle can drop
> one" wording no longer applies. ADR-0088 also resolves, for SiteWise in
> the live story, the known limit "OPC UA playback and the SiteWise feed
> are wall-clock": the minute story step publishes the telemetry from the
> same anchored model; the OPC UA playback stays wall-clock.
> (b) [ADR-0089](0089-placeable-and-packaged-story-clock.md) amends
> decisions 1, 3 and 5: the anchor has three sources (`deploy`, the Create
> time snapped down onto the grid; `packaged`, the CR `Anchor`; `placed`, a
> steward placement), recorded as `story_anchor_source` next to the
> telemetry sweep keys in `story_history.CONFIG_KEYS`; an anchor change is a
> re-plan (`story_replan.replan`: retire the prior era exactly, then write)
> rather than insert-only. A placement retires the prior era, so it does
> not leave a second failed batch; the `live -> frozen -> live` Delete path
> and its known limit are unchanged.
> (c) The run view of the 2026-09-29 hop amendment is now a realistic MES
> run view: `production_runs(production_run_id, run_number, sku,
> planned_quantity, status, actual_start_time, actual_end_time, line)`
> (see the [ADR-0087](0087-exact-agreement-cross-system-joins.md) note).

Builds on [ADR-0084](0084-one-erp-and-the-workshop-story-setting.md) (the
`workshop_story` setting; this ADR is what `live` means at runtime),
[ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md) (the
condition monitor and its wall-clock `loop_step`) and
[ADR-0083](0083-indianapolis-fourth-site.md) (Indianapolis as a simulator
site; decision 5's "same failure shape, not same timestamp" precedent).

## Context

The live story replaces the February seed with the simulator's own plant:
the attendee asks about the batch line 3 most recently failed, the part it
was built to, and the customers who received it. Three things make that hard
to run as a workshop:

- ADR-0073 aligned the condition-monitor loop to the wall clock (loops start
  at 00:00, 08:00 and 16:00 UTC), so where an account sits in the
  degrade → alarm → repair cycle depends on when the event starts.
- A fresh account has no history: no failed batch, no shipped customers, no
  deferred PM, until the simulator has run for hours.
- Facilitators cannot see which batch, part and customers their account
  produced, because every account's live data differs.

## Decisions

1. **The live story is keyed by DATA, not by the setting.** The simulator
   never reads `workshop_story`. Its live steps run only when
   `simulation_config.story_anchor_epoch` holds a finite positive number, and
   only the deploy-time history writer (decision 3) creates that row. Each
   cycle `SimulationOrchestrator._load_story_clock` resolves one of three
   states (`erp_simulation/simulation_orchestrator.py`):

   | State | When | Condition monitor | Story step |
   |---|---|---|---|
   | `absent` | no key (demo, frozen) | wall clock (anchor `0.0`) — byte-identical to before | not run |
   | `present` | key holds a valid number | on the anchor: loop 0 step 0 starts at the anchor (`condition_monitor.story_seconds`, `loop_start_epoch`) | run |
   | `unknown` | the anchor read failed | SKIPPED this cycle (`condition_anchor_epoch = None`) | SKIPPED this cycle |

   `unknown` never falls back to the wall clock: guessing could raise a stray
   condition work order on the wrong loop. A malformed or non-positive stored
   value reads as `absent` with one warning per bad value
   (`story_live.read_anchor`).

2. **The live defect rule.** `condition_monitor.phase(step)` names the four
   phases (`degrading` < `BPFO_ALARM_STEP` ≤ `bpfo_alarm` < `BEARING_FAIL_STEP`
   ≤ `repair` < fail + `BEARING_REPAIR_STEPS` ≤ `healthy`). While the anchored
   loop is in `bpfo_alarm`, `story_live.apply_cycle` inserts a failing
   `DIMENSIONAL` check (sample 20, inspector `QC-SPC`, notes naming the BPFO
   band) per RUNNING work order OF THE STORY PART (`story_history.FAILED_SKU`,
   `MACH-HSG-7742`) on `condition_monitor.STORY_MES_LINE`
   (`LINE3`) at `MESSimulator.CONDITION_MONITOR_SITE` every
   `story_live.DEFECT_CHECK_INTERVAL_MINUTES` (15): NOT EXISTS on a
   `DIMENSIONAL` check of that work order newer than the interval, so a
   cycle re-run inside one interval inserts nothing. With the one-minute
   Lambda a full-length story run gets 8 checks (0, 15, …, 105 min into the
   ~107 min window; a late cycle stretches a gap to 16 min and can drop one,
   so 7-8), failing 3, 4, 4, 5, 6, 7, 7, 8 of 20. Until 2026-09-29 the guard
   was one check per work order, so the fail share never climbed (the
   alarm walk saw 3 of 20 for the whole window). An order of another product running on LINE3
   gets NO check: otherwise a shipped simulator run of another part would
   become the failed batch and drag the part and customers with it. The fail share follows
   `condition_monitor.defect_reject_fraction` at each check's own step: 0
   outside the window, a linear climb from 0.15 to 0.40 inside it.
   Work-order completion already sums every check into the yield (`mes_simulator.derive_good_reject`), so the
   batch's `reject_quantity` rises without touching the production-event
   simulator. The step runs in its own connection and never raises; since
   [ADR-0090](0090-simulation-cycle-isolation-and-collision-free-numbers.md)
   it runs BEFORE the simulator steps, so a slow simulator cannot starve it.

   The simulator leaves LINE3 idle most of the time (one deployed run: one
   LINE3 work order in 6 h, none in 43 min of alarm), so a check "per RUNNING
   work order" alone left the module 6 MES sign unobservable. Before the
   check, therefore, the step ENSURES a RUNNING story-line work order
   (`story_live.story_work_order`, ONE all-or-nothing statement): when no
   work order is RUNNING on the story line and this loop's story run was not
   started yet, it inserts an ERP production run (`IN_PROGRESS`, the
   history's failed part `MACH-HSG-7742`, FG location of the site, no order,
   line set) with its linked RUNNING MES work order (`actual_start_time` =
   now) and `START` event, exactly the rows
   `MESSimulator.simulate_production_events` leaves when it starts an order.
   Numbers follow the simulator's format, `WO-<YYYYMMDD>-8<loop%10000>` /
   `RUN-…` (dated on the loop's alarm instant; the history owns `9xxxx`), so
   the whole alarm window names the same rows: NOT EXISTS on that work-order
   number plus `ON CONFLICT (run_number)` give at most one story run per loop,
   and a simulator order of the story part already RUNNING on the line takes
   the check instead (the story run then starts on the first alarm cycle
   after that order completes). A RUNNING order of ANOTHER part also makes
   the story run wait (the line is busy), but it takes no check, so that
   stretch of the window has no live defect. The run is planned to end at the loop's bearing failure
   (`planned_end_time` = `BEARING_FAIL_STEP`), so LINE3 stays RUNNING
   (`mes_line_status`, from the next cycle's line-status step) through the
   whole ~107 min alarm window. `target_rate_per_minute` is an INTEGER
   column, so the history's 60 units / 90 min (0.67/min) cannot be stored and
   rounding it up to 1 would make the completion step's OEE performance read
   ~65 % for a healthy run; the story run is therefore planned at an honest
   1/min for the WHOLE alarm window: `story_live.STORY_PLANNED_QUANTITY` =
   floor((`BEARING_FAIL_STEP` − `BPFO_ALARM_STEP`) × 5 s / 60) = 107 units,
   derived from `condition_monitor`, whenever in the window the run starts
   (the page states one number; until 2026-09-29 it was the minutes left at
   the start, 106 when the run began 24 s late). `planned_end_time` stays the
   bearing failure, so a run that starts late reads a capped 100 % OEE
   performance. No hook was needed for completion: it is an ordinary
   run-linked order, so the simulator's own completion step (`planned_end_time
   <= NOW()`) closes it at the first cycle after the failure, summing the
   check into `reject_quantity`. It gets NO lot allocation: shipping picks
   only runs with `lot_production_usage` as `shipment_items.batch_id`, so the
   story run never becomes a shipped batch. The history's failed run
   therefore stays the newest shipped failure (module 2) UNLESS a
   simulator-scheduled order of the story part was RUNNING on LINE3 in an
   alarm window and was later shipped: that order genuinely failed the
   check, so it becomes the newer failed batch — same part, with the
   customers it shipped to. A shipped run of another part never can. Timeline in the loop's alarm phase:
   cycle N (first alarm cycle) — run + work order + START + the failing
   `DIMENSIONAL` check commit together; every 15 min after — the next,
   higher-failing check; cycle N+1 — `mes_line_status` shows
   LINE3 RUNNING; first cycle after `BEARING_FAIL_STEP` — `COMPLETED` with
   rejects.

3. **A deterministic deploy-time history.** `Custom::ADTStoryHistory`
   (`ADTERPLambdaStack`, live story only; function `erp-story-history` from
   the SAME image asset as the simulation Lambda, handler
   `lambda_/lambda_story_history.py`) writes `erp_simulation/story_history.py`'s
   plan for the anchor = the Create's wall clock and three simulated days
   (`Days` property). `plan()` is pure (no I/O, no RNG, no clock):
   - six completed Indianapolis runs per simulated day across LINE1..LINE3
     (`RUN-<YYYYMMDD>-9dkkk`, normal 1-in-60 rejects, one visual check each);
   - ONE failed batch, `RUN-<YYYYMMDD>-90001`, on LINE3, product
     `MACH-HSG-7742`, starting 10 minutes into the BPFO window of the loop
     that contains `anchor − 1 day`; three `DIMENSIONAL` checks whose fail
     share follows decision 2's rule; a `MECHANICAL` / `VIB-BEARING` downtime
     from the bearing failure to the end of the repair; lot usage from
     `LOT-IND-BARAL7075150-SEED`;
   - three `MACHINING` customers (`CUST-MCH-0001..0003`) whose orders for
     that part shipped from that batch (`shipment_items.batch_id` = the
     failed run number; two `DELIVERED`, one `SHIPPED`);
   - in the plant maintenance schema: PM `3101` (`WAPPR`, `PM-1001-0007`,
     target five days before the failed batch, rescheduled to anchor + 2 d)
     with its deferral log entry; condition-monitor order `3107` (`CM`,
     `BEARING`, `COMP`; its description is the live monitor's own
     `MESSimulator._condition_work_order_description`, so it reads like each
     `VIB-CMX-*` order: `Condition monitor: spindle vibration alarm. BPFO
     band …` — the word "vibration" since 2026-09-29, because the module's
     CMMS sign asks for an "open vibration work order" and the agent missed
     text that said only "BPFO band") and emergency repair `3108` (`EM`, `BEARING`, `COMP`,
     root cause naming `3101`). The frozen era on the story asset (the 011
     rows reported before the anchor, which the frozen story needs as they
     are; the seed SQL is never edited) is retired in place: its OPEN orders
     (PM `1182` `WAPPR`, PM `1201` `APPR`) are set `CAN` with one supersede
     log entry each that records the prior status (`Superseded by the live
     plant history (status was APPR)`) and ` [superseded by the live plant
     history]` appended to the description; its COMPLETED orders (CM `1187`,
     EM `1195`, whose descriptions cite frozen-story ids such as `B-2847` /
     `WO-9921` that do not exist in the live plant) keep their status under
     the description prefix `[Prior CMMS era, before the live history] `.
     They are marked, not deleted: they are real maintenance history of the
     asset, marking is idempotent and reversible by string, and a delete
     would force restore to re-insert byte-identical rows and log entries.
     Both marks fit `description VARCHAR(250)` for every 011 row (test-pinned
     against the DDL; the prefix is shorter than an earlier wording
     because that one overflowed `1187` by two characters), and a row a mark
     would overflow is left unmarked rather than failing the deploy.
     `CLOSE`d cadence PMs (`1004`, `1096`) and other assets are untouched, so
     `3101` is the only open order on the asset. Condition-monitor orders on
     the story asset older than the anchor are deleted so they cannot collide
     with this era's loop numbers;
   - the `story_*` `simulation_config` rows (`story_history.CONFIG_KEYS`:
     anchor, loop geometry, history days, failed run number, deferred PM),
     written in the SAME ERP transaction as the rows.

   The ERP side is insert-only on natural keys (`ON CONFLICT DO NOTHING`,
   children only under new parents); an Update rebuilds the plan from the
   STORED anchor, so it never moves the clock. The ERP transaction commits
   before the seed transaction, so a failed seed write is retried on the same
   clock. Create/Update errors raise and fail the stack visibly.

4. **Delete restores the frozen seed.** A `live → frozen` redeploy removes the
   CR; its Delete, in two independent best-effort steps that never raise,
   clears every `story_*` key (the anchor goes, so the next cycle is
   `absent`: wall clock, no defect step) and runs
   `story_history.restore_frozen_seed` (each superseded order back to the
   status its supersede entry records, `1182` `WAPPR` and `1201` `APPR`, with
   the entry and the description suffix removed; the prior-era prefix
   stripped from `1187`/`1195`; `3101`/`3107`/`3108` and their log deleted),
   so the seed CMMS tables equal a fresh 011 load (PG round-trip test). An
   entry written before the status was recorded (bare `Superseded by the
   live plant history`) only ever superseded a `WAPPR` PM and restores as
   such. The ERP history rows stay as ordinary demo data.

5. **A db-init re-run rewrites the history.** db-init drops and reseeds every
   ERP table, anchor included. The history CR carries the db-init CR's re-run
   token (`erp_database_stack.db_init_trigger`: the db-init `Timestamp` plus
   the database name) and the ERP secret ARN as properties, and depends on
   the db-init resource, so the same deploy sends it an Update; with no stored
   anchor it takes a NEW anchor, writes the ERP side fresh and UPSERTS the
   seed-side maintenance rows so their dates follow the new clock.

6. **Facilitator lookup (implemented; third wave).** `GET /api/admin/workshop/story`,
   gated by the steward check (`Depends(check_steward)`), offloaded per the
   `agents/offload.py` rule, read-only (read-only sessions, statement timeout,
   every query `LIMIT`ed), 404 when workshop mode is off. Response:
   `{story, generated_at, clock|null, failed_batch, part, maintenance,
   customers[], total_value, live_defect_run|null, warnings[]}`; a missing source adds a warning,
   never a 500. Frozen: `clock` is `null` and the rows come from the seeded
   storyline (B-2847, its Released PLM revision, the ERP customers ordering
   that part, the deferred PM and bearing repair). Live: `clock` is the phase
   math on the stored `story_*` numbers (no `erp_simulation` import), the
   failed batch is the most recent LINE3 work order with a failing
   `DIMENSIONAL` check whose run was SHIPPED (`shipment_items.batch_id` =
   `production_runs.run_number`, the linkage
   `ShippingSimulator._traceable_batch_id` and the history write) or is the
   history's failed run — so module 2's "most recent shipped failed batch"
   stays the history run while a story run is live (a later-shipped
   simulator order of the story part that ran in an alarm window replaces it,
   see decision 2); `live_defect_run` (same
   columns) is the most recent failing LINE3 work order that is NEITHER the
   failed batch NOR the history's failed run — the RUNNING story run the defect step starts in a BPFO
   window — and `null` before the first alarm (and always in frozen); it
   never feeds `part` or `customers`. Customers come through
   `shipment_items.batch_id`, and maintenance is THIS era's only (reported
   within `story_history_days` of the anchor, or one of the history's own
   work orders — so the frozen-era seed orders on the same asset, e.g. EM
   `1195`, stay out). A list cut at its `LIMIT` says so in `warnings`.
   No new unauthenticated surface — but **in workshop mode the answer key is
   readable by every attendee**: the shared attendee login is a member of the
   Cognito `Admin` group, so it passes `check_steward` and can `GET
   /api/admin/workshop/story` like a facilitator. Decision: accept and
   document (the workshop is a guided exercise in a throwaway account, and the
   data it reveals is in the attendee's own account anyway); do not treat the
   lookup as secret from attendees. A separate facilitator identity is the
   fix if that ever matters.

   **Connection exception to ADR-0031 constraint 2.** The lookup opens its
   read-only sessions from `agents.demo.db_routing.demo_pg_dsn(...)` on EVERY
   request — a steady-state runtime use of the demo DSN builder that
   ADR-0031 constraint 2 otherwise reserves for seed/registration-time
   bootstrap. It is a documented, demo-only exception: the lookup reads the
   story's fixed demo schemas (never an onboarded system's connection), lives
   in `agents/demo/`, and is reachable only in workshop mode behind the
   steward gate.

## Amendment 2026-09-29 — the MES→ERP traceability hop

Fresh live-story chats answered module-2 Q1 with "None of
these batches have shipped". The ERP records a shipment line's batch as
`shipment_items.batch_id` = ERP `production_runs.run_number`
(`ShippingSimulator._traceable_batch_id`, and the history write), but no
onboarded system exposed `production_runs`: the MES work order carried only
the run's UUID. The ERP agent joined `lot_mes_tracking.batch_id` instead —
a dead end, because that column holds the MES WORK-ORDER number, never a run
number — and found nothing; passing runs matched the WO/RUN numeric suffix by
guess.

The hop is DATA plus a machine-derived join; nothing curated
([ADR-0087](0087-exact-agreement-cross-system-joins.md)). DATA: every site
MES view schema (`agents/demo/site_views.py`, db-init
`get_machining_views_sql`) carries `production_runs(production_run_id,
run_number)` for the runs its work orders execute, so provision's `*_id`
heuristic links it to `work_orders.production_run_id`; and the history ships
what the plant makes: every completed history run gets ONE delivered order to
one site customer (rotating over the four MACHINING customers) whose shipment
line carries the run number as `shipment_items.batch_id`, exactly like the
failed run's three orders. A fresh deploy therefore has 19 shipped history
runs (18 normal + the failed one, still $10,200 over three customers), and
the failed run stays the newest shipped run with a failing `DIMENSIONAL`
check (normal runs carry one passing-share `VISUAL` check). JOIN: the
exact-agreement affinity pass (`identity_affinity.exact_rule`) derives
`mes-ind production_runs.run_number -> erp shipment_items.batch_id` from that
evidence (same concept, same value shape, 19 probed exact values) and writes
it through the Identity Rules checkpoint as `jp-mes-ind-<digest>`, method
`value_exact_match`. The earlier curated pre-accepted JoinPath seed is
removed.

## Amendment 2026-11-04 — the story machine runs only story work

The MES simulator gave live work orders to any free Indianapolis line, LINE3
included, from the deploy's first cycle. A placement re-times the history
(ADR-0089) but leaves the simulator's rows where they are, so a live LINE3
run could sit inside a re-timed history run: on 2026-11-04 live
`RUN-20261104-00003` (09:01 to 09:04Z, made during the deploy) ran inside
history run `-91005` on `L3-M1`. Module 6's containment answer then differed
by account and by chat, because the runs the machine made after the failed
batch included whatever the simulator had put there.

Decision: while the live story is on, `MESSimulator.reserved_lines` takes
the story line at the condition-monitor site out of `get_next_available_line`.
LINE3 there runs only the history's runs and decision 2's story run. "On" is
the anchor row present this cycle OR `STORY_LINE_RESERVED=1` on the
simulation Lambda (`stacks/_workshop_story.STORY_LINE_ENV_NAME`, live synths
only). The flag is needed because the row is written by the history CR,
which runs after the Lambda's first cycles, and a Reset All wipes the row
until the CR rewrites it. An unknown clock (read failed) without the flag
reserves nothing, so demo and frozen plants are unchanged.

A `SCHEDULED` work order already stored on the reserved line (only an older
build makes one) is handled by a cycle step of its own,
`MESSimulator.clear_reserved_lines` (`mes_reserved_lines`), which runs
before `mes_work_orders` and `mes_events`; the event step itself is
unchanged. The order has not started, so its line is only an assignment:
it moves to the next free line with that line's target rate and its
`lot_mes_tracking` rows, the only rows that record the line before a start,
and is due at once. With no free line its start goes 10 minutes ahead, so
the event step does not start it, and the next cycle tries again, the way a
new run waits in `RELEASED`. Running before `mes_work_orders` lets it take a
freed line ahead of new work. Leaving it waiting on the reserved line for as
long as the story runs would strand its run and its allocated lots, and its
quantity would count as pipeline stock that never arrives. The step is
optional like `mes_events`; past the cycle time budget an earlier skipped
step means every later optional step is skipped too. It fails closed: when
it fails (rolled back and counted like any step), that cycle runs neither
`mes_work_orders` nor `mes_events` (`CycleStep.blocks`), so a due order on
the reserved line cannot start through the unchanged event step; the rest
of the cycle runs as before and the next cycle tries again. No other step
blocks anything, and off the live story the step runs no SQL.

Fresh accounts: every writer of a line at the story site is either the
simulator's `get_next_available_line`, the history writer or the story step.
Production and backorder runs are created without a line, no seed (db-init,
the seed Lambda, the workshop seed) writes `mes_work_orders`, and every cloud
cycle runs in `erp-simulation-orchestrator`, whose environment CloudFormation
creates with the function, before the schedule rule that invokes it.

Consequences: on a fresh deploy no simulator run on the story machine can
overlap the history on any placement path (first sign-in, a facilitator's
first Place, a re-Place, Move the alarm, a reset), since the only runs there
are the plan's and the story step's. The story run always finds LINE3 idle at the alarm. Decision
2's "UNLESS a simulator-scheduled order of the story part was RUNNING on
LINE3" can no longer arise on a fresh deploy; its guards stay for accounts
whose MES ran before this change. The other two Indianapolis lines take the
simulator's work (runs last minutes; a run waits in `RELEASED` when both are
busy). LINE3's line state and OEE come from the story's runs only. Not
changed here: LINE3 can read `CHANGEOVER` for up to about 20 minutes after
the alarm run starts, because the event step's changeover rule matches any
earlier completed order of another part on the same line (deferred.md).
Rejected: holding LINE3 only until placement (live runs after it still land
in the containment answer, may take the alarm checks, and need the
simulator to know the placement state and the auto-place policy), and
retiring the simulator's LINE3 rows at placement (they carry shipments,
invoices, lot usage and inventory moves).

Upgrade path. An account updated in place from an older build keeps the
simulator's LINE3 rows at the story site: its `SCHEDULED` ones move (above),
its `RUNNING` ones complete within minutes as usual, and its `COMPLETED`
ones stay, with their shipments. Placement does not retire or refuse them
(a failed placement at an event is worse than the overlap), so until they
age out they can sit inside a placed history run and appear among the runs
the machine made after the failed batch. A placement puts the history in
the `story_history_days` (3) days before an anchor inside the last loop, so
rows older than that plus one loop (8 h) no longer matter. To clear them at
once, use the ERP dashboard's Reset All (it drops the ERP tables and the
history custom resource rewrites the story on a new clock) or deploy a fresh
account. Event accounts are always fresh.

## Known limits

- **OPC UA playback and the SiteWise feed are wall-clock, not anchored.** The
  OPC UA server (`highbyte/components/opc-server/server.mjs`) still serves
  `(epoch mod loop) / interval`, and the dashboard's degradation-feed button
  backfills 72 h ending now. In a live account the CMMS/MES loop and the
  telemetry show the same failure shape at different times (ADR-0072
  decision 5's precedent). The workshop profile ships no OPC UA servers, so
  only the feed matters there.
- **`live → frozen → live` without a db-init re-run writes a second failed
  batch.** Delete clears the anchor but leaves the ERP history rows; the next
  Create takes a new anchor, and the new plan's run numbers carry the new
  date. The previous failed batch and its three orders remain.
- **`live → frozen` leaves the ERP history orders.** Delete does not remove
  the ERP history rows, so every history run's delivered order stays; the
  frozen story's part-customer lookup then widens beyond the B-2847
  customers (measured on the PG lane: 9 orders, 4 customers, $51,000).
- **Mixed-version Delete is not a full restore.** A Delete run by OLDER
  lambda code after a newer write only undoes what that older code knew
  about, so it can leave the newer description suffix (`1182`/`1201`) or
  prior-era prefix (`1187`/`1195`) in the seed CMMS tables. A fresh 011
  reload (`lambda-adt-seed/011_cmms_indianapolis.sql`) restores them.
- **The plant MES has three lines, hardcoded** (`story_history.LINES`,
  db-init's `IND-L1..L3`). Whether the story plant should be a single-line
  site is an open question.
- The live story registers no Plant Historian, so module 6's historian
  routing lesson has no live counterpart yet (workshop content question).
- Demo (non-workshop) deploys never get the anchor, history or defect.
- **Attendees can read the facilitator lookup** (decision 6): the shared
  workshop login is in the Cognito `Admin` group, so `GET
  /api/admin/workshop/story` is not a facilitator-only secret in a workshop
  account.
- **How the history CR re-runs.** On a CloudFormation deploy the CR only acts
  on a property change: a db-init re-run (its trigger token is a CR property —
  decision 5), a changed history writer, or a `live → frozen → live` redeploy
  (Delete, then a fresh Create with a new anchor, see the previous-batch
  caveat above). Outside CloudFormation it also re-runs on a direct ERP full
  reset (the dashboard's Reset All, db-init invoked by hand): db-init runs it
  as a direct-reset follow-up step with the same no-change Update
  (stacks/_direct_reset.py, `erp_lambda_stack._register_story_history_reset`,
  the `STORY_REGISTRY_PARAMETER` registry), and with the anchor dropped by the
  reset it takes a new one and rewrites the history. Its `Version` property is
  computed at synth time
  (`erp_lambda_stack.story_history_version()`, read-only so `cdk synth` stays
  pure): sha256 over the relative path and bytes of
  `erp_simulation/story_history.py` and `lambda_/lambda_story_history.py`,
  hex[:16]. A stack update that ships an edited writer therefore sends the CR
  an Update, which re-derives the plan from the STORED anchor (insert-only ERP
  side, idempotent seed-side retire/upsert), so the fix reaches a running
  account without moving the clock. A change to anything else the writer
  imports (`condition_monitor`, `mes_simulator`) does NOT re-run it — add the
  file to `STORY_HISTORY_SOURCES` if its change must reach deployed rows.
  Redeploying `live` over `live` with no db-init or writer change is a no-op.
  An environment deployed before the hash shipped carries the literal
  `Version` `'1'`; its first redeploy on the hashing code sends ONE Update as
  `Version` moves from `'1'` to the hash. That Update is expected and safe —
  the same stored-anchor re-derivation (idempotent, the clock does not move)
  — and later redeploys are no-ops until the writer changes.
- **The lookup's live maintenance list is era-bounded, not exhaustive.** It
  shows the story asset's open PM, bearing (`CM`/`EM` + `BEARING`) and
  condition-monitor orders reported since `anchor − story_history_days` plus
  the history's own work orders, newest first, at most 20 (a truncation adds
  a warning). An order on that asset outside those rules, or older than the
  window, is not shown. Without the `story_*` config (history not run) the
  list is unrestricted and a warning says so. An order the simulator
  completed after its loop ended (a `VIB-CMX-*` condition order, or an
  `IND-*` order reported by the MES bridge, with status `COMP`; ADR-0073 note
  2026-10-10) is routine
  history like the PM's last completed cycle and is not shown either, so
  however long the account runs the list carries at most one condition order
  (the current loop's) and only the current loop's bridge orders. The frozen
  list leaves the same completed simulator orders out (a frozen account's
  bridge orders would otherwise push the seed's 1182 and 1195 off its end);
  a fresh frozen account has none, so its list is unchanged. A completed
  `IND-*` order someone else reported stays listed in both. The 20-row limit
  stays: a sane account's list is the history's three orders, one condition
  order, any orders a placement cancelled and the current loop's open
  MES-triggered bearing orders, so a list that still reaches 20 is worth the
  warning.

## Consequences

- Demo and frozen deploys are byte-identical at runtime: no anchor row, one
  extra `SELECT` per cycle.
- Every live account shows the same story geometry relative to its own
  deploy time; answer TEXT still differs per account (run numbers carry the
  date), which is why decision 6 exists.
- "Reset All Data" on the ERP dashboard runs the ERP db-init full reset
  (`erp_simulation/streamlit_dashboard.py`, after the cleanup and a
  configuration reset), so it DROPS and reseeds every ERP table,
  `simulation_config` and its `story_*` anchor keys with them. db-init runs
  its direct-reset follow-up steps afterwards (the plant re-seed, the reader
  logins, the registered steps), and the live-story history CR is one of them
  (`erp_lambda_stack.py`, registered in `STORY_REGISTRY_PARAMETER`): with no
  stored anchor left it takes a NEW anchor and rewrites the whole story, the
  same the Update a CloudFormation db-init re-run sends it would leave
  (decision 5). So a Reset All in a live account ends with the story rebuilt
  on a new clock, not without one. The new clock means new run numbers, so
  facilitators still shouldn't press it during an event. db-init waits for
  the writer only as long as its own timeout leaves: a writer slower than
  that is reported as not restored (a second Reset All restores it), and the
  writer db-init already invoked can still finish afterwards
  (`tests/test_db_init_reader_resync.py`, the story step test).

## Verification

Offline: `erp_simulation/tests/test_story_live.py` (anchor read, one defect
per work order per 15-min interval, story site/line and story part only, the story run: one per loop, numbers in
the simulator's format, planned to the bearing failure at an honest 1/min, 107 units whenever it starts; never raises, no
story SQL without an anchor; PostgreSQL-gated: an idle LINE3 gets the story
run and its check in the first alarm cycle, `mes_line_status` RUNNING, the
simulator completes it after the failure, a minute-by-minute walk of the
whole window lands 8 climbing checks and completion sums them all, a RUNNING simulator order of the story part takes the
check first, a RUNNING order of another part makes the story run wait and gets no check,
the history batch stays the newest shipped failure), `erp_simulation/tests/test_story_history.py` (determinism, failed
batch inside the BPFO window, PM before failure before repair, idempotent
write, stored anchor reused, Delete restore), `tests/test_erp_lambda_story_history.py`
(CR and function only under `live`, same image asset, db-init trigger
wiring), `agentic_digital_thread/tests/unit/test_condition_monitor.py`
(anchor 0 equals the wall clock; phase boundaries; defect fraction),
`agentic_digital_thread/tests/unit/test_mes_cmms_bridge.py` (monitor raises
at `anchor + BPFO_ALARM_STEP × 5 s`),
`agentic_digital_thread/tests/unit/test_workshop_story_lookup.py` and
`agentic_digital_thread/tests/unit/test_workshop_story_routes.py` (decision 6:
frozen/live payloads, warnings instead of 500s, steward gate, 404 outside
workshop mode; the duplicated predicate constants pinned against the seed SQL
and `story_history.plan`; the shipped-or-history `failed_batch` predicate
pinned to `ShippingSimulator._traceable_batch_id`'s run-number linkage, and
`live_defect_run` excluding it), and — PostgreSQL-gated (`ERP_SIM_TEST_PG_DSN`) —
`agentic_digital_thread/tests/integration/test_story_lookup_pg.py` (every
lookup statement `EXPLAIN`s against db-init + seed DDL; frozen and live
end to end; after `story_live.apply_cycle` at the BPFO alarm the story run is
`live_defect_run` and `failed_batch` stays the history's shipped run). Not yet verified in a deployed live account:
the history in a fresh account, the defect within two cycles of the BPFO
window, the lookup.
