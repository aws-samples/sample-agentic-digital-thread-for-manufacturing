<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0089: The live story clock is placeable (a steward re-plan) and packageable (`--story-anchor`)

## Status

**Amended: 2026-10-04** by [ADR-0095](0095-two-story-clocks-live-anchor-beside-history-anchor.md): the live side may follow a second, live-only anchor (`story_live_anchor_epoch`) so the bearing alarm can move without rewriting the history; absent that key, everything below is unchanged.

**Proposed: 2026-09-29.** The re-plan engine, the story-history Lambda's
`place` action and packaged `Anchor`, the stack and packaging changes, the app
route `POST /api/admin/workshop/story/clock` and `scripts/place_story_clock.py`
are implemented, with the fixes of 2026-09-29 (one era at a time, explicit
`force`, the in-flight guard).
Verified offline only.
Amended 2026-09-30 ([below](#amendment-2026-09-30-the-attendee-places-the-clock)):
the relative `alarm_in_minutes` form, the `effect` text of the Lambda-free
dry run, and the attendee as the placer.

Builds on [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md)
(the one story clock). Amends [ADR-0085](0085-live-story-clock-history-and-lookup.md)
decisions 1, 3 and 5 (the anchor now has three sources and provenance keys,
and an anchor change is a re-plan rather than insert-only) and its known
limit "`live -> frozen -> live` writes a second failed batch" (a placement
retires the prior era; the Delete path is unchanged).

## Context

ADR-0085 anchors the live story at the history CR's Create time. Where an
account sits in the degrade, alarm, repair cycle on workshop day therefore
depends on when it was deployed, and a Workshop Studio package is deployed
at a time nobody controls. Module 6 needs the BPFO alarm to land while the
room is in module 6. The anchor also cannot simply be edited: run numbers
embed their dates (`RUN-<YYYYMMDD>-9dkkk`), the history is insert-only, and
the telemetry sweep, the CMMS orders and the story runs all derive from the
anchor (ADR-0088).

## Decisions

1. **A placement re-plans by RETIRE + REWRITE (D9).**
   `erp_simulation/story_replan.py::replan` deletes exactly what
   `story_history.plan(old_anchor)` wrote plus the old era's story runs, then
   writes `plan(new_anchor)` through the existing insert-only writer.
   Re-deriving dates in place was rejected (run numbers would disagree with
   their times), and keeping two eras would ship a second failed batch and
   make "the most recent shipped failed batch" wrong. Ownership is computed
   from the deterministic plan, so no marker column exists.
   `retire_era` enumerates ids first and deletes children first, in FK
   order: the finance simulator's `payments`, `invoice_line_items` and
   `invoices` of the retired orders and shipments; `shipment_items`,
   `shipments`, `order_line_items`, `customer_orders`; the MES
   `mes_quality_checks`, `mes_production_events`, `mes_downtime_events`,
   `mes_material_consumption`, `lot_mes_tracking`, `mes_work_orders`; then
   `lot_production_usage`, `production_materials` and the `production_runs`.
   History runs match on run number AND sku, line and start time at the
   story site; orders on order number AND customer code and order date. A
   simulator-owned row never matches.

2. **The orphaned story run goes with its era (D10).** The story runs of
   every old-era loop whose alarm is at or before now
   (`story_run_numbers`, the SAME numbering function the minute step uses)
   are deleted when they have no order and sit on the story site, line and
   part; their MES work orders and children go with them. They were never
   shipped and carry no lot usage. Left in place, one would become
   `live_defect_run` under the new clock.

3. **The seed side follows (D11).** In the seed transaction:
   `_purge_condition_orders_before(cur, anchor, now)` deletes every
   `VIB-CMX-*` order (and its log) on the story asset reported before now
   that is not this era's (this era's = the wonum AND its alarm-instant
   `reported_date`); a new anchor can be earlier than old-era orders, so
   "before the anchor" would not be enough. Then `story_history._write_seed`
   re-applies the history CMMS rows (the upsert moves `3101`/`3107`/`3108`;
   supersede and prior-era marks are idempotent), and
   `MESSimulator.insert_condition_work_order` catches up `VIB-CMX-<loop>`
   for every new-era loop whose alarm is at or before now.

4. **Normalisation keeps the phase (D12).** `normalize_anchor(reference,
   now)` snaps to the grid congruence (`condition_monitor.snap_anchor(...,
   "nearest")`, `anchor % 300 == 10`) and then shifts by whole 8 h loops into
   `(now - 28800, now]`: the history must end at or before now and SiteWise
   rejects future values, and the loop is periodic, so a 14:10Z alarm stays
   14:10Z (and every 8 h after). Inputs: `alarm_to_anchor(alarm_epoch)` (the
   alarm rounded to the nearest 5 min grid point, minus 19490 s) or
   `phase_offset_to_anchor(phase, elapsed_minutes, now)` for `degrading`,
   `bpfo_alarm`, `repair` or `healthy` (an elapsed time outside the phase
   raises). `loops_shifted` reports how many loops normalisation moved the
   reference back. An alarm more than 19490 s (5 h 24 m 50 s) ahead shifts
   back one loop, so loop 0's alarm is already past and its `VIB-CMX` is
   caught up.

5. **Placement runs in the story-history Lambda, serialised and CAS-guarded
   (D13).** `erp-story-history` (`lambda_/lambda_story_history.py`) answers a
   direct RequestResponse invoke with `{"action": "place"}` (contract
   below). `replan` takes `pg_advisory_xact_lock` (key `b"STORYCLK"` as a
   signed 64-bit integer) in ONE ERP transaction, re-reads the stored
   anchor, checks it against `expected_anchor` (`StaleAnchor` otherwise, with
   nothing written), retires, writes, upserts the story config (anchor,
   `story_anchor_source`, failed run) and resets the telemetry sweep state
   (`story_telemetry_anchor` = the new anchor, cursor one grid point before
   `anchor - 3 d`) in the same transaction; then the seed transaction (ERP
   first, the ADR-0085 order). The minute step republishes the telemetry
   from the reset cursor (ADR-0088 decision 4), so no SiteWise work happens
   inside the request. A placement onto the STORED anchor retires and writes
   nothing: it resets the sweep and re-applies the idempotent seed side, so
   a retry after a seed failure converges. That same-anchor case holds only
   while the stored anchor is within `(now - 8 h, now]`: normalisation always
   lands there, so once the stored anchor is older than one loop the same
   placement normalises to a NEWER congruent anchor and re-plans (retire +
   rewrite) instead. No reserved concurrency (fresh accounts can have low
   limits).

   **One era at a time.** The per-cycle writers take the SAME lock key
   SHARED (`pg_advisory_xact_lock_shared`, `story_replan.SHARED_LOCK_SQL`)
   and re-read `story_anchor_epoch` in the same transaction before any
   insert: `story_live.apply_cycle` (story-run start and check insert, ERP)
   and `MESSimulator.monitor_asset_condition` (the `VIB-CMX` order, seed;
   the re-read goes through `condition_anchor_reader`). A stored anchor that
   differs from the one the cycle loaded (moved, cleared or malformed) skips
   the cycle: 0, one log line, nothing written. The seed half of `replan`
   takes the lock EXCLUSIVE on the seed connection and then confirms the
   committed ERP anchor is still its own; if a later placement committed its
   ERP half in between, it raises `StaleAnchor` with the seed untouched (the
   later placement's seed half owns the seed). Story runs are retired on
   run number AND MES work-order number (plus site, line, part, no order),
   so a simulator run whose random number lands in the story's `8NNNN`
   space is never retired. The history's telemetry cursor is clamped to
   `grid_floor(now - 7 d + 300)` (SiteWise rejects values older than 7 days).

6. **A packaged anchor (D14).** The Workshop Studio packager and `deploy.sh --story-anchor` (both require the live
   story) parse the value with the ONE parser,
   `stacks/_workshop_story.resolve_anchor` (a positive epoch or a tz-aware
   ISO string; a naive ISO string is refused), and forward whole epoch
   seconds as CDK context `story_anchor` (package.py: to BOTH synths, and
   recorded as `asset-manifest.json` `story_anchor` and
   `cdk.out.engine/adt-story-anchor.json` for `--skip-synth`).
   `erp_lambda_stack.story_anchor_property` puts it on the history CR as
   `Anchor` (`""` when unset; a non-live synth with an anchor fails).
   `packaging_lib.check_story_anchor` cross-checks the CR against the
   manifest. On Create the Lambda uses `normalize_anchor(Anchor, now)`
   (source `packaged`); on Update it re-plans with `source="packaged"` (no
   CAS) ONLY when `Anchor` is set and differs from `OldResourceProperties`.
   Otherwise the writer keeps the stored anchor, so a placement survives
   redeploys, and dropping the flag (Anchor `""`) never moves the clock.
   Without an anchor, a live package or deploy prints a WARNING: the phase
   then depends on deploy time until a facilitator places the clock.

7. **The deploy default snaps down (D15).** Without a packaged anchor the
   Create anchor is `condition_monitor.snap_anchor(now, "down")`, at most
   5 minutes before the Create, source `deploy`.

8. **Provenance is recorded.** `story_history.CONFIG_KEYS` gains
   `story_anchor_source` (`deploy` | `packaged` | `placed`,
   `story_history.ANCHOR_SOURCES`), `story_telemetry_anchor` and
   `story_telemetry_cursor_epoch`; the CR `Data` carries `AnchorEpoch`,
   `AnchorSource` and `FailedRunNumber`. The CR's `Version` hash now also
   covers the writer's import closure: `erp_simulation/story_replan.py`,
   `condition_monitor.py`, `story_live.py` and `mes_simulator.py`
   (`STORY_HISTORY_SOURCES`). The minute-step sweep
   (`erp_simulation/story_telemetry.py`) never runs in this Lambda and is not
   hashed.

## The Lambda `place` contract

Request (a direct invoke, no `RequestType`):

```json
{"action": "place", "alarm_at": 1790019500, "expected_anchor": 1790000010}
{"action": "place", "phase": "bpfo_alarm", "elapsed_minutes": 12, "expected_anchor": null}
```

- Exactly one of `alarm_at` (epoch seconds, within `[now - 1 d, now + 7 d]`)
  or `phase` + `elapsed_minutes`; any other key is refused.
- `expected_anchor` is REQUIRED; `null` skips the compare-and-swap.
- Response: the `replan` report, `{old_anchor, new_anchor, source:
  "placed", retired{...}, written{...}, seed{superseded_orders,
  prior_era_marked}, telemetry{anchor, cursor_epoch, backfill: "pending"},
  failed_run_number, alarm_at, loops_shifted}`. `retired` counts rows per
  table key (`payments` ... `runs`, `story_runs`) plus `condition_orders`;
  `written` carries `config`, the history writer's insert counts,
  `cmms_work_orders`, `cmms_log` and the caught-up `condition_orders`.
  `alarm_at` is the placed alarm (rounded to the grid, before loop
  normalisation).
- It NEVER raises (the `story_replan` import is inside the guard): a CAS
  mismatch returns `{"error": "stale_anchor", "message"}`, bad INPUT
  `{"error": "invalid", "message"}`, anything else - a `ValueError` raised
  server-side inside the re-plan included - `{"error": "internal",
  "message": <exception class name>}`; the detail (up to 300 characters) is
  logged by the Lambda, never returned.

## The app route

`POST /api/admin/workshop/story/clock`, `Depends(check_steward)`, offloaded
(`return await offload(workshop_story_clock, ...)`, the `agents/offload.py`
rule), 404 outside workshop mode or when the function is not wired, 409 when
the story is not `live`. The body is a pydantic model with
`extra="forbid"`: `alarm_at` (a STRING, ISO 8601 with a timezone; numbers and
numeric strings are refused) OR `phase` + `elapsed_minutes`,
`expected_anchor_epoch` (required; an integer epoch `<= 4102444800`, the
value from the GET; `null` skips the CAS and is a 422 unless the body also
says `force: true`, and `force` is refused with a non-null anchor) and
`dry_run` (default false). One placement at a time per process: an apply
while another is in flight is a 409 `{"error": "in_flight"}` at once (a
non-blocking process-level lock, the Evals one-run-lock shape; a dry run is
never refused; across replicas the Lambda's advisory lock and CAS still
serialise). A dry run previews the
normalisation app-side and never invokes the Lambda; an apply invokes
`erp-story-history` RequestResponse through `agents/demo/story_clock.py` (one
attempt, bounded read timeout) and maps `stale_anchor` to 409, `invalid` to
422 and `internal`, a function error or a transport failure to 502 with a
digit-masked message. The compute stack already grants it: on a `live`
synth only, the task role's inline policy `StoryClockLambda`
(`lambda:InvokeFunction` on `function:erp-story-history`, named by the ERP
stack's `STORY_HISTORY_FUNCTION` constant rather than a cross-stack export)
and the container env `STORY_HISTORY_FUNCTION_NAME`. The GET stays
read-only. `scripts/place_story_clock.py` is the facilitator's client.

## Invariants

- The story anchor moves ONLY through `story_replan.replan` (the `place`
  action behind the steward POST, or a changed packaged `Anchor`); never
  edit `story_anchor_epoch` by hand.
- A placement is CAS-guarded, serialised by the advisory lock, and retires
  the prior era exactly (enumerated from the same `StoryPlan` the writer
  consumes; no parallel lists).
- Commit order stays ERP, then seed.
- Frozen and demo deploys have no anchor, no `Anchor` property, no
  `StoryClockLambda` and no invoke env: every new path is inert there.

## Known limits

- **The shared attendee login can place the clock.** The workshop's shared
  login is in the Cognito `Admin` group (ADR-0085 decision 6), so it passes
  `check_steward` and can POST a placement. Accepted and documented like
  the lookup: the CAS stops a stale or concurrent placement, not a
  deliberate one. A facilitator Cognito group is the follow-up.
  Since 2026-09-30 this is the
  design, not a limit: the attendee IS the placer (amendment below).
- **A shipped simulator-owned order survives a placement.** A simulator
  order of the story part that took old-era `DIMENSIONAL` checks and was
  shipped is not story-owned, so it is not retired and can outrank the new
  history run as the most recent shipped failed batch. Rare (LINE3 is
  mostly idle); the lookup flags it with `is_history_run false`.
- **A placement deletes finance rows of the retired shipments** (invoices,
  their lines and payments), because they cannot outlive their order (FK).
- **A seed failure leaves the ERP on the new era.** The ERP transaction has
  committed; a retry must target the STORED (new) anchor (read it from the
  GET) or skip the CAS (`expected_anchor_epoch: null` with `force: true`),
  and then converges.
- **Telemetry lags a placement** by the sweep (about 8 cycles); until then
  SiteWise still shows the old era's points outside the head.
- **A changed packaged `Anchor` overrides a placement** on the next Update
  (it re-plans without a CAS).
- **The Delete path is unchanged**: `live -> frozen` still leaves the ERP
  history rows (ADR-0085 known limits).

## Verification (offline)

`erp_simulation/tests/test_story_replan.py` (pure: normalisation,
congruence, the shift into `(now - 8 h, now]`, phase kept, the alarm and
phase forms, `story_run_numbers`; PG-gated: write A1, a RUNNING story run,
replan A2 leaves no A1 row, the A2 rows present, CAS mismatch, two threads
serialised with one stale, a re-plan onto the stored anchor writes nothing,
seed purge and catch-up, `restore_frozen_seed` round trip, the story lookup
after a re-plan reporting the new failed batch),
`erp_simulation/tests/test_lambda_story_history.py` (Create without and with
`Anchor`, Update with an unchanged or cleared `Anchor` writes only, a changed
`Anchor` re-plans as `packaged`, `place` validation and error mapping, Delete
unchanged), `tests/test_workshop_story_context.py` (`resolve_anchor`),
`tests/test_erp_lambda_story_history.py` (`Anchor`, sources hash),
`tests/test_adt_compute_env_wiring.py` (invoke grant and env only under
`live`) and the Workshop Studio packager plus the packaging
gate (manifest `story_anchor` against CR `Anchor`).

Not yet verified in a deployed account: a placement through CloudFront
(200, `anchor_source placed`, 19 runs retired and written), the GET right
after it, the catch-up and sweep completion, and a no-op redeploy keeping
`placed`.

## Amendment 2026-09-30: the attendee places the clock

Plan `plan-20260930-attendee-clock` (W1a, W3a, W4a). Full rationale and the
packaged loop length: [ADR-0091](0091-attendee-placed-clock-and-packaged-loop.md).

1. **Who places the clock.** Workshop Studio gives each attendee their own
   account. The facilitator cannot sign in to those accounts. Event
   operators cannot set CloudFormation parameters at event time. So the
   ATTENDEE places the clock, in module 1. The primary path is the app UI:
   Admin > Workshop, "Bearing alarm in [N] minutes", Preview, then Place.
   The packaged `--story-anchor` (decision 6) is the fallback for an
   account that is never placed. `scripts/place_story_clock.py` stays the
   steward's and the scripted test client.
2. **The relative form `alarm_in_minutes`.** The route body accepts a third
   exclusive form: `alarm_in_minutes`, a strict JSON integer in 5..480
   (`story_clock.ALARM_IN_MINUTES_RANGE`). Booleans, strings and floats are
   refused (422). The app resolves it ONCE, from ONE `now`, to an absolute
   `alarm_at`: the 300 s grid point at or after `now + N min` (snapped UP;
   a grid point stays put). Both the dry run and the apply use that
   `alarm_at`. The Lambda receives `{"action": "place", "alarm_at": <epoch>,
   "expected_anchor": ...}`, byte-identical to the absolute form. The Lambda
   `place` contract does not change. The answer echoes
   `requested: {alarm_in_minutes, alarm_at}`. The CLI gains
   `--alarm-in-minutes N` as its fourth exclusive form.
3. **The dry run stays Lambda-free.** It still invokes nothing and takes no
   in-flight lock. It gains two keys. `effect` is a fixed text list:
   "rewrites the three-day history and every run number" and "resets the
   sensor backfill, about 10 minutes". `warnings` holds one text when
   `loops_shifted >= 1`: an earlier alarm already exists in the rewritten
   history, so the account shows a condition order now. There are no
   retire or write counts, and there is no `preview` Lambda action.
4. **The shared login is the design.** The "shared attendee login can place
   the clock" known limit is restated: that login is the placer. The CAS
   still stops a stale or concurrent placement. It does not stop a
   deliberate one, and it is not meant to. A facilitator Cognito group
   stays deferred: gating the GET on
   it would hide the tab and the chip from the attendee (ADR-0091 D6).
5. **Loop length.** Decision 4's "whole 8 h loops" means whole loops of the
   packaged length. Unset, that is 8 h. With `STORY_LOOP_SECONDS` set on a
   live synth it is that length, and the alarm offset follows (ADR-0091 D5).
