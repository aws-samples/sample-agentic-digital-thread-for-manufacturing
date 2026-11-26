<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0084: One ERP for every site; the workshop storyline is a synth-time `workshop_story` setting (`frozen` | `live`)

## Status

**Accepted: 2026-09-27.** Implemented: one ERP for all sites, story-aware
workshop registration, the `workshop_story` setting, the live-story clock,
history and FG routing. Not yet verified in a deployed account (see
Verification).

**Amended 2026-09-27 — [ADR-0086](0086-identity-rule-wildcard-for-one-to-many-cores.md):**
the frozen MES → PLM fact "`7742` ↔ `PN-7742-REV-C`" (decision 3) is now the
rule `{id}` ↔ `PN-{id}-REV-{*}`; the Released `PN-7742-REV-C` is chosen at
answer time from the PLM's status.

**Amended 2026-09-29 (decision 2; [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md) wave):**
there are now FOUR machined SKUs. `MACH-MNFD-5520` ("Hydraulic Manifold
Block 5520", FG `FG-MNFD-5520`, BOM `BAR-AL7075-150`, `COOL-SYN-IND`,
`INS-CARB-IND`, `PKG-CRATE-SM`) cross-references `PN-5520-REV-B` through
`PLM_PART_XREF`, beside the three listed in decision 2. The live history
(`story_history.PRODUCTS`) is deliberately unchanged. The body below is
unchanged.

**Amended 2026-09-30 (Consequences, "Upgraded (non-fresh) environments …"):
new seed rows land on the next in-place update, without data loss.** A
frozen-story update showed the 5520 rows above never reaching an existing
account: the seed custom resource re-ran, but `003`/`006`–`009` used plain
`CREATE TABLE` and aborted before any new row (`relation "production_runs"
already exists`), `005` succeeded only because it dropped and recreated the
PLM tables, `011` duplicated its `work_order_log` rows on every re-run, and
db-init never re-ran. Now:

- every `lambda-adt-seed/*.sql` file is re-runnable on an existing schema:
  `CREATE … IF NOT EXISTS`, every INSERT `ON CONFLICT (<primary key>) DO
  NOTHING` or `WHERE NOT EXISTS` on the natural key (batch/run number; full
  row for fact rows without one; equipment + type + timestamp for the
  `generate_series` readings), and no `DROP TABLE`. Pinned by
  `tests/test_adt_seed_rerun_idempotent.py` (text
  checks, plus a PostgreSQL double-apply that proves the second run changes
  no row, including the B-2847 chain and the planted-mapping asset rows);
- `AdtSeedResource` carries `Version` = `adt_seed_version()` (sha256[:16]
  over every seed SQL file) instead of the MD5 `ContentHash`;
- `DatabaseInitResource` carries `SeedVersion` = `db_init_seed_version()`
  (the same hash over `lambda-db-init/handler.py`). An Update that changes
  ONLY `SeedVersion` runs `seed_catalogue_upsert` (the machining-site
  catalogue `seed_machining_site`, all `ON CONFLICT DO NOTHING`, then the
  `CREATE OR REPLACE` views) and never `drop_all_tables`; `SeedVersion` is
  not in `db_init_trigger()`, so the live-story history does not re-run.
  Create, a `DB_INIT_TIMESTAMP` bump, a rename or a new cluster still drop
  and reseed.

Existing rows are never updated, so a changed value in an existing seed row
still needs a fresh account (or, for the ERP, the reset); ERP seed data
outside the machining site still needs the `DB_INIT_TIMESTAMP` reset. One
exception: the site CMMS seed (`011`) owns the people named on its rows, so a
re-run sets `work_orders.reported_by` and `work_order_log.author` to the
seed's names (a log entry is matched without its author) and leaves every
other column as the account holds it.

Supersedes:

- [ADR-0083](0083-indianapolis-fourth-site.md) decision 2 ("The plant ERP
  stays") — there is no plant ERP;
- [ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md)
  decision 6 only where it names the plant ERP ("Indianapolis Plant ERP"); the
  MES and maintenance names stand;
- [ADR-0074](0074-plant-systems-fed-from-the-simulator.md) entirely — nothing
  is push-synced into plant schemas; the live story registers the simulator's
  own plant MES view instead (decision 3 below).

Companion: [ADR-0085](0085-live-story-clock-history-and-lookup.md) (what the
`live` story does at runtime). Related: [ADR-0030](0030-demo-data-is-opt-in-base-default.md)
(data flags are not sticky), [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(demo content stays in `agents/demo/`), [ADR-0065](0065-unattended-provisioning-fresh-account-findings.md)
(fresh Workshop Studio deployments).

## Context

The workshop's Indianapolis storyline was built on a SAP-style **plant ERP**
next to the demo's corporate ERP: module 2 traced a customer complaint from a
plant-ERP material lot to MES batch `B-2847` through a discovered identity
rule. ADR-0072 then made Indianapolis a live simulator site, and ADR-0074
proposed push-syncing simulator rows into the plant ERP and plant MES so the
two would stop disagreeing.

A design review on 2026-09-26 rejected that premise and set the requirement:
one ERP serves every site. Order entry, purchasing, warehousing, shipping and
accounting are central functions, and the MES is decoupled as a pull
operation from the order system.

In other words, the workshop should tell the demo's story on the demo's
system roles. The same review asked that the Indianapolis systems appear on
the demo's onboarding page, and set the live, simulator-driven storyline as
the target once it is proven. Until then the deterministic seeded storyline
has to stay available, without a plant ERP, as the fallback.

## Decisions

1. **One ERP.** Removed 2026-09-27: the plant ERP system `erp-ind`, its seed
   schema `erp_indianapolis` (its `lambda-adt-seed/` file `002` deleted,
   `CREATE SCHEMA` dropped from `lambda-adt-seed/001_schemas.sql`), its manifest
   entry, its routing exception, its place in both seed-system lists, and the
   OPC UA `MaterialLot` tag that carried its lot number (the story mill now
   publishes the one ERP's raw lot `LOT-IND-BARAL7075150-SEED`). Every site's
   orders, lots, finished goods and shipments live in the corporate ERP.
   `tests/unit/test_seed_defect_chain.py::test_no_seed_file_defines_a_plant_erp_schema`
   refuses a reintroduced plant ERP schema.

2. **Indianapolis is a full ERP site.** `lambda-db-init/handler.py` seeds a
   `MACHINING` customer segment (four customers, `CUST-MCH-0001..0004`,
   placeholder names) whose orders carry only `Machined Components`
   (`erp_simulation/segments.py::SEGMENT_CATEGORIES`, one source, parity-tested
   against db-init); PLM cross-references for the three machined SKUs
   (`MACH-HSG-7742 → PN-7742-REV-C`, `MACH-SHFT-8810 → PN-8810-REV-B`,
   `MACH-BRKT-9901 → PN-9901-REV-A`, re-applied at the end of
   `seed_machining_site` because the MACH products are created after
   `seed_products`); product names aligned to the PLM families; the
   `DIMENSIONAL` quality-check type; `simulation_events.mes_ind_*` columns;
   and one finished-goods location map (`IND-FG` included) for replenishment,
   backorders, order availability and shipping. The two ADR-0072 deferrals
   (no machined sales orders; IND FG resolved to `FG-WAREHOUSE`) are closed.

3. **One setting, `workshop_story`, picks the storyline.** Values `frozen`
   (default) and `live`. `stacks/_workshop_story.py::resolve` is the rule:
   workshop deploy → the value or `frozen`; non-workshop deploy → no story,
   and `live` there is a synth error; any other value is a synth error.

   | | `frozen` | `live` |
   |---|---|---|
   | Seed CR pre-onboards | `erp, mes-mil, mes-det, mes-cr, plm, mes-ind, historian, sitewise-direct` | `erp, mes-mil, mes-det, mes-cr, plm, mes-ind, sitewise-direct` |
   | `mes-ind` schema | `mes_line3` (seed DB, the B-2847 archive) | `mes_indianapolis` (the simulator's plant MES view, ERP DB) |
   | Plant Historian | registered and onboarded | not registered |
   | Register-only | `cmms-ind` (`cmms_indianapolis`) | `cmms-ind` (`cmms_indianapolis`) |
   | `Custom::ADTStoryHistory` | absent | present in `ADTERPLambdaStack` (ADR-0085) |

   The frozen customer-impact leg now runs MES → PLM → corporate ERP: MES
   `product_code` `7742` ↔ PLM `item_id` `PN-7742-REV-C` (identifier-core
   affinity, `value_core_match`), then PLM → ERP through
   `products.engineering_part_number`. The live leg is a direct ERP join
   (`shipment_items.batch_id` = the failed run number, ADR-0085).

4. **Synth-time CDK context, never a CloudFormation parameter.** The story
   decides which resources EXIST (the history CR, its provider and Lambda
   function), literal lists (the seed CR's `Systems`), and the app's env.
   As a parameter, every one of those would need CFN Conditions threaded
   across nested-stack boundaries that the Workshop Studio packager copies
   byte-for-byte, and — the real reason — a Workshop Studio event could set
   the parameter independently of the content build, pairing live pages with
   frozen data. Resolved once at synth, the story is a property of the
   PACKAGE and is recorded three ways that tooling cross-checks:
   the ECS app env `ADT_WORKSHOP_STORY` (`stacks/adt_compute_stack.py::_env_workshop`),
   the workshop root's Output `WorkshopStory` plus `Metadata.AdtWorkshopStory`,
   and `asset-manifest.json` `"workshop_story"` (`null` for the engine
   profile). The seed CR also receives `Story` and echoes `WorkshopStory`.
   `package.py --story` forwards the value to BOTH synths (engine children
   and root) and its structural self-check
   (`packaging_lib.check_workshop_story`) fails the build when the manifest,
   root Output, compute env, seed CR `Systems` and history-CR presence
   disagree. `deploy.sh --workshop-story` sets the same context; like every
   data flag it is NOT sticky (ADR-0030).

5. **The runtime env and `db_routing` do not read the story.** AgentCore
   runtimes need nothing story-specific, so `agents/runtime_env_contract.py`
   is untouched and `deploy.sh` forwards only `ADT_WORKSHOP` (not the story)
   to the runtime envs. `agents/demo/db_routing.py` answers "where does this
   schema live", which is identical in both stories: `mes_line3` is the one
   `SEED_RESIDENT_ERP_FAMILY_SCHEMAS` exception, `mes_indianapolis` routes to
   the ERP database by the prefix rule. The app reads the story in exactly one
   place, registration (`agents/demo/workshop.py::story`, unset → `frozen`,
   unknown → `ValueError`). The simulator never reads it (ADR-0085).

6. **The demo also registers the plant systems.** A non-workshop deploy with
   the demo addon registers `workshop.DEMO_PLANT_SYSTEMS` — the live plant
   MES (`mes-ind`, `mes_indianapolis`) and plant maintenance (`cmms-ind`) — on
   top of the demo six. Registered only: `DEMO_SYSTEMS` stays the six, and no
   bulk-onboard default contains them.

7. **The registry heals a stale schema or flags it, never silently keeps it.**
   One id (`mes-ind`) maps to two schemas across modes, so a catalog entry
   left by an earlier boot in the other mode would keep the wrong connection.
   At startup `server._reconcile_demo_schema` rewires a NOT-onboarded entry to
   the manifest schema (WARNING); an ONBOARDED entry is left unchanged (its
   runtime and graph were built from the old schema) and an ERROR names the
   fix, `scripts/reonboard.py <id>`.

8. **`reonboard.py --all` never picks `ONBOARD_EXCLUDE`.** Its
   nothing-onboarded-yet fallback used to be every catalog entry, which on a
   fresh workshop included the attendee exercise `cmms-ind`. The fallback now
   drops `workshop.ONBOARD_EXCLUDE`; an already-onboarded excluded system is
   kept (`scripts/reonboard.py::_discover_all_systems`).

## Shared login and memory

The workshop uses one shared login, so every attendee has the same AgentCore
Memory `actorId`. With actor-wide long-term-memory retrieval, a fresh chat got
facts from other attendees' chats and answered from memory with no Sources
(live walk R2b: `WO-3101` was quoted without a CMMS call). The workshop
profile therefore sets `MEMORY_LTM_SCOPE=session` for both stories. The
orchestrator then retrieves only `/summaries/{actorId}/{sessionId}/`: a chat
keeps its own earlier turns, and actor-wide facts/preferences are no longer
injected into a fresh chat's MODEL CONTEXT. That is the whole guarantee — it is
not isolation between attendees: on the shared login they can still see and
continue each other's chats through the history sidebar and the memory
routes, and facts/preferences are still WRITTEN for the shared actor. The
default everywhere else stays `actor`, and a redeploy without `--workshop`
returns to it, so the workshop teardown purges the shared actor's facts and
preferences namespaces (`scripts/purge_actor_memory.py`). The knob lives in
`agents/config.py`. The runtime value comes only from
`runtime_env_contract.WORKSHOP_ORCHESTRATOR_ENV`, and the ECS app gets it from
`_env_workshop`.

## Consequences

- Module 2's frozen translation lesson depends on affinity discovering
  `mes-ind.product_code` ↔ `plm.item_id` unaided. The seed guarantees the
  data shape (B-2847's product has exactly one Released PLM revision sharing
  its digit core — `test_mes_story_batch_product_has_a_released_plm_revision`),
  not the discovery; if the seed log's identity-rule report lacks the line,
  the content's fallback is an attendee-proposed guidance note.
- Upgraded (non-fresh) `deploy.sh` environments keep the retired plant-ERP
  schema in the seed database; nothing drops it (non-destructive default). It
  is unregistered and therefore invisible to the platform.
- **Upgraded (non-fresh) environments do not get the one-ERP database changes
  until db-init re-runs.** The ERP database initializer (`lambda-db-init`)
  runs on the first deploy and re-runs only when its custom resource changes
  (the `DB_INIT_TIMESTAMP` token in `stacks/erp_database_stack.py`). An environment deployed before the one-ERP
  change therefore lacks its DDL and data — the `MACHINING` customers, the
  `DIMENSIONAL` quality-check type, the `mes_ind_*` simulation-event columns,
  the `IND-FG` location and the PLM part cross-reference — until db-init
  re-runs. Bumping `DB_INIT_TIMESTAMP` re-runs it, and that run **drops and
  reseeds every ERP table** (all simulated ERP data is lost). Workshop accounts
  are always fresh deploys, so they always have it. The simulator tolerates the
  missing `simulation_events` columns (it skips them rather than failing).
- A workshop package is one story. Switching an account's story is a
  redeploy with the other value (`live → frozen` runs the history CR's Delete,
  ADR-0085).
- Workshop content (the separate workshop repository) must be rewritten for
  module 2 and every page that names the plant ERP; tracked there, not here.

## Verification

Offline (the Wave 1 and Wave 2 integration gates): `agentic_digital_thread/tests/unit/test_workshop_mode.py`
(per-story manifests, `story()`, both seed lists pinned against the CR handler
and the engine stack), `test_register_demo_systems.py` (demo plant systems;
schema heal/flag), `test_demo_db_routing.py`, `test_reonboard_lib.py`
(`--all` fallback), `test_seed_defect_chain.py`, `test_erp_plm_xref_seed.py`;
`erp_simulation/tests/test_machining_site_seed.py`, `test_order_segments.py`,
`test_segment_parity.py`, `test_fg_location_routing.py`; root
`tests/test_workshop_story_context.py`, `tests/test_adt_compute_env_wiring.py`;
the Workshop Studio packager, `test_workshop_root_wiring.py`,
`test_packaging_lib.py`, `test_packaging_gate.py` (parametrized `engine` /
`frozen` / `live`).

Not yet verified in a deployed account, frozen and live: the module walks, the
seed log's `value_core_match` line, the live Outputs.
