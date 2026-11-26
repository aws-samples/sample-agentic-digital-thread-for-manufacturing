<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0074: The workshop's Indianapolis plant systems are fed from the simulator

## Status

**Superseded by [ADR-0084](0084-one-erp-and-the-workshop-story-setting.md) — 2026-09-27.**
A design review decided (2026-09-26) that every site runs on the ONE
corporate ERP simulator and each site's MES pulls its runs from it, so there
is no plant ERP to sync into: the plant ERP was removed, the frozen story
keeps its seeded plant MES, and the live story registers the simulator's own
plant MES view (`mes_indianapolis`) instead of syncing into `mes_line3`. The
two questions below were answered: the plant maintenance system stays
separate (`cmms_indianapolis`), and the plant systems DO appear on the demo's
onboarding page (registered only). Nothing in this ADR was built. The body
below is kept unchanged as the historical proposal.

Originally: Proposed — 2026-09-23, design only, with two questions open
(feed the plant systems live? one CMMS or separate?). No code lands under
this ADR until they are answered. Depends on
[ADR-0083](0083-indianapolis-fourth-site.md) (Indianapolis is a simulated site)
and [ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md) (the
site's maintenance events already flow to its own maintenance system).

## Context

The workshop registers three systems the demo does not: **Indianapolis Plant
ERP** (`erp_indianapolis`: `suppliers`, `materials`, `orders`), **Indianapolis MES**
(`mes_line3`: `production_runs`, `quality_checks`) and **Indianapolis Plant
Maintenance** (`cmms_indianapolis`). They carry the B-2847 story the workshop's
modules 2 through 7 walk through: production order `000060009921`, material lot
`IND-2026-02847`, MES batch `B-2847` on `L3-M1`, quality drift, the deferred
spindle-bearing PM `1182`. The identity rule the graph learns at onboarding
(`erp-ind.material_lot IND-2026-0{id}` ↔ `mes-ind.batch_number B-{id}`) is
what module 2's routing and translation demonstrate.

Since ADR-0083 the simulator also runs Indianapolis as a live site: production
runs on three lines, MES work orders, downtime, and (ADR-0073) condition-based
maintenance. Those rows land in the simulator's own ERP and MES tables
(`production_run`, `mes_work_orders`, `mes_production_events`,
`mes_quality_checks`), keyed by the simulator's identifiers, while the plant
systems still hold their February seed. An attendee who asks "what is running
on line 3 right now" gets a live answer from the simulator's MES and a
February answer from the Indianapolis MES. The intended reading of the plan
(Indianapolis is another site in the ERP, and the MES queries the ERP for
orders to build) is true of the simulator's systems and false of the workshop's.
Closing that gap is the point of this ADR.

## Decision (proposed)

1. **Push-sync from the MES simulator cycle, same pattern as the CMMS bridge.**
   `MESSimulator` already holds a connection to the seed database
   (`_get_seed_db_conn`) and already writes Indianapolis maintenance there.
   A `sync_plant_systems(cycle_id)` step runs after the site's runs and work
   orders are created and upserts them into the plant schemas. PostgreSQL
   `postgres_fdw`/`UNION` views over the two databases were considered and
   rejected: onboarding introspects tables, the plant schemas are a different
   shape from the simulator's, and a view cannot carry the plant systems'
   vendor-convention identifiers.

2. **Field mapping keeps the identity rule true.** For each simulator
   `production_run` at `INDIANAPOLIS_MACHINING`:

   | simulator | Indianapolis Plant ERP `orders` | Indianapolis MES `production_runs` |
   |---|---|---|
   | `run_number` (numeric core N) | `order_number` = `0000600` + N zero-padded to 5 | `work_order_ref` = `WO-` + N |
   | N | `material_lot` = `IND-2026-0` + N zero-padded to 4 | `batch_number` = `B-` + N |
   | product | `material_number` (18-char padded, from a fixed product→material map) | `product_code` (`PN-…` from the same map) |
   | `line` | `production_line` = `L3` for line 3 | `line_id` = `L3`, `equipment_id` = `L3-M1` |
   | status | `CRTD` / `REL` / `CNF` / `TECO` | `Scheduled` / `Running` / `Stopped` / `Completed` |
   | quantities, times | `quantity`, `scheduled_start/finish`, `actual_finish` | `planned_qty`, `good_qty`, `reject_qty`, `start_time`, `end_time` |

   Simulator `mes_quality_checks` for the site map to `mes_line3.quality_checks`
   (`parameter`, `nominal`, `lsl`, `usl`, `measured`, `result`, `check_time`).
   The numeric core N is chosen from a counter that starts above the seeded
   range, so live rows never collide with the story rows.

3. **The seeded story is never touched.** Story rows (`B-2847` and every
   seeded order, run and check) sit below a watermark the sync does not cross;
   sync statements are `INSERT … ON CONFLICT (pk) DO UPDATE` restricted to
   rows the sync itself created. A test pins that `B-2847`, `000060009921` and
   `IND-2026-02847` are byte-identical before and after a full sync.

4. **No-op where the plant systems are absent.** A deploy without the workshop
   seed (the demo) has no `erp_indianapolis`/`mes_line3` schemas; the sync
   detects that once per loop, logs one line, and returns 0, exactly as the
   condition monitor does. Demo behaviour is unchanged.

5. **One MES per plant, not two.** `mes_indianapolis` (views over the
   simulator, ADR-0083) stops being registered as a system once the Indianapolis MES is
   live; its views stay for the simulator's own reporting. The workshop
   registers three Indianapolis systems and no more.

## What this does not decide

* Whether Indianapolis maintenance goes into its own system or the shared CMMS.
  This was an open question; ADR-0073's routing stands until it is answered.
* Whether the plant systems also appear on the demo's onboarding page. Also
  asked; this ADR keeps them workshop-only.
* Module 6 content (the live-feed step) and module 2's expected answers, which
  move to the live spectrum and to live orders in the workshop repo once this
  lands.

## Consequences

* Modules 2, 5 and 7 answer from live plant data instead of February data;
  module 2's identity rule keeps working because the sync writes both halves
  of it.
* The sync adds one seed-DB round trip per MES cycle for the Indianapolis
  site; the CMMS bridge already pays the same cost.
* Maintainability gate: the sync is several small functions (`_plant_order_row`,
  `_line3_run_row`, `_upsert_plant_rows`), none over 50 lines.

## Verification (planned)

Unit: mapping functions are pure and tested per row; the story-row watermark
test; the absent-schema no-op test; idempotence across two cycles. Local: seed
+ simulator cycle against PostgreSQL, then `SELECT` from `erp_indianapolis.orders`
and `mes_line3.production_runs` shows today's runs beside February's, and the
`B-2847` rows are unchanged. Not before a deploy: module 2 golden run.
