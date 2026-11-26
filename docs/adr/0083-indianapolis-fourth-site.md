<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0083: Indianapolis is a fourth simulated site; CMX-441 telemetry comes from its OPC UA server

## Status

Proposed — 2026-09-23.

> **2026-09-27 — decision 2 superseded by [ADR-0084](0084-one-erp-and-the-workshop-story-setting.md).**
> There is no plant ERP any more: one corporate ERP serves every site, and
> module 2's customer-impact leg runs MES → PLM → ERP. The two deferrals in
> decision 1 (no `Machined Components` sales orders; Indianapolis FG resolved
> to `FG-WAREHOUSE`) and the "plant-ERP write-through" follow-up are closed by
> the same change. The live MES view `mes_indianapolis` IS now registered
> (the live story and the demo register it as `mes-ind`). Decisions 1 and 3–8
> stand; the body below is unchanged.

> **2026-09-29: decisions 5 and 6 amended by [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md).**
> In the `live` workshop story the CMX-441 telemetry is published into
> SiteWise by the ERP simulation Lambda's minute story step, computed from
> `condition_monitor` on the stored story anchor, so SiteWise, the CMMS
> condition order and the MES checks share one clock. This is a recorded,
> scoped exception to the 2026-09-22 direction above: the workshop profile
> ships no OPC UA server and no HighByte, so its only SiteWise writer was
> already a Lambda (the decision 6 fallback). [ADR-0092](0092-live-story-telemetry-from-the-opcua-server.md)
> later removed the exception. Decision 6: in a live account the dashboard button is
> replaced by a read-only caption (keyed by the anchor row); it stays for
> frozen and demo, and its removal condition is unchanged. Decision 5's
> "same failure shape, not the same timestamp" no longer applies to the live
> story's SiteWise data; the OPC UA dataset is unchanged. The body below is
> unchanged.

> **2026-10-12: the live workshop package now ships the HighByte hub.** The
> Context below says the workshop profile "does not ship HighByte"; that was
> true when this ADR was written. A `--story live` package now deploys the
> HighByte Intelligence Hub as the UNS beside the four OPC UA servers and SFC
> (`LIVE_TELEMETRY_CONTEXT` in the Workshop Studio packager), with the hub's
> SiteWise pipeline imported off, so SFC stays the only SiteWise writer
> ([ADR-0082](0082-sfc-writes-sitewise-for-unattended-deployments.md)
> decision 1). The frozen package still ships no hub. For what the hub
> deploys and how to reach it, see [`highbyte.md`](../highbyte.md). The
> decisions and the body below are unchanged.

Related: [ADR-0030](0030-demo-data-is-opt-in-base-default.md) (demo data is
opt-in), [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md) (demo literals live
outside the engine), `docs/workshop-mode.md` (the seeded B-2847 story).

## Context

The workshop's OT chapter (module 6) asks attendees to find the CMX-441 spindle
bearing failure in AWS IoT SiteWise. Until now that telemetry reached SiteWise
only through `lambda-sitewise-feed`: a Lambda function behind a dashboard
button that backfills a 72-hour vibration and coolant-temperature trend onto
the `5-Axis-Milling-CMX-441` asset. It was introduced (2026-07-28) as a
deliberate, documented, licensing-free path for the workshop profile, which
does not ship HighByte, and module 6 tells attendees as much.

Indianapolis itself existed only as static seed SQL (`lambda-adt-seed/`): a
plant ERP modeled after SAP, MES line 3, a CMMS modeled after IBM Maximo and the
historian. The ERP simulator knew three sites (Milwaukee, Detroit, Cedar
Rapids); the
`ADTHighByteOpcuaStack` ran three OPC UA servers; nothing generated
Indianapolis orders, built them, or emitted its telemetry.

The direction given on 2026-09-22 is that writing telemetry into SiteWise from
a Lambda function is not a valid pattern for the demo or the workshop. Telemetry
must originate from the OPC UA servers, one per site, and every site must be
defined in the ERP simulator (so orders are generated for the MES to build),
have an MES, have CMMS events, and have an OPC UA server. Indianapolis becomes
the fourth site.

## Decision

1. **Indianapolis is a real simulator site.** `lambda-db-init/handler.py::
   seed_machining_site` adds site `INDIANAPOLIS_MACHINING` ("Indianapolis
   Precision Machining", Indianapolis, IN), product category `Machined
   Components` (three products whose SKUs carry the plant-ERP material numbers
   7742 / 8810 / 9901), seven raw materials with APPROVED lots at
   `IND-WH-RAW`, BOMs, finished goods at `IND-FG`, two suppliers, routing
   (`Machined Components` priority 1, parent `Machining` priority 2), lines
   `IND-L1..L3`, `site_product_config` and initial `mes_line_status` rows,
   plus the live MES view schema `mes_indianapolis` (`get_machining_views_sql`).
   Production orders reach the site through the replenishment path:
   `production_simulator.create_production_runs` plans runs for every routed
   product whose finished-goods stock is below the reorder point, keyed by
   `product_site_routing`, and the MES builds them on the IND lines;
   `SITE_FG_LOCATION` / `SITE_RAW_LOCATION` map the site to `IND-FG` /
   `IND-WH-RAW` for deposits and consumption. Both seed entry points call the
   same function (`seed_data()` in the handler and `erp_simulation/seed_data.py::
   _apply_multisite_seed`). Two pieces are deferred because they live inside
   functions frozen by the 50-line maintainability gate (`order_simulator.
   generate_orders`, 216 lines, and the replenishment query in
   `create_production_runs`, 224 lines, neither unit-tested): customer sales
   orders do not yet include `Machined Components`, and the replenishment
   query resolves an unlisted site's FG stock to `FG-WAREHOUSE` (so
   Indianapolis is always planned up to the reorder point rather than against
   `IND-FG`). See Consequences.

2. **The plant ERP stays.** `erp_indianapolis` is not folded into the
   simulated corporate ERP. Workshop module 2 is built on the corporate-ERP to
   plant-ERP handoff and the Indianapolis ERP ↔ Indianapolis MES identity rule.
   The simulator does NOT write its Indianapolis orders through to
   `erp_indianapolis` in this change (the plant ERP is in the seed database on
   a different host, and the simulator has no seed-DB writer besides the CMMS
   bridge); the seeded plant-ERP rows stand as they are. Recorded as a gap, see
   Consequences.

3. **Indianapolis maintenance lands in `cmms_indianapolis`.** The MES-downtime
   to CMMS bridge (`erp_simulation/mes_simulator.py`) gains `CMMS_SITE_TARGET`:
   for `INDIANAPOLIS_MACHINING` it writes a row in the IBM Maximo shape into
   `cmms_indianapolis.work_orders` (asset picked by ASSETNUM prefix `1001-`,
   preferring the `CNC_MILL` for vibration and drift reasons; `EM`/priority 1
   for mechanical downtime, `CM`/priority 2 otherwise; `VIB-BEARING` maps to
   the story's failure code `BEARING`; `wonum` is `IND-` plus eight hex chars
   of the downtime id). Routing it into the already-onboarded
   `cmms_maintenance` would have made Indianapolis maintenance visible before
   module 4 and erased the module-3 wall. The three legacy sites are unchanged.

4. **A fourth OPC UA server, "Indianapolis".** `stacks/highbyte_opcua_stack.py`
   adds `("Indianapolis", "OpcIndianapolis")`; the setup Lambda receives
   `OPC_UA_ENDPOINT_4` and substitutes it into HighByte connection
   `opc_indianapolis`; output `OpcUaEndpoint4`. `highbyte/generate_datasets.py::
   machining_tags` produces the dataset under `AnyCompany/Indianapolis/
   Machining` with lines `Line1..Line3`. `Line3CNC1` is the story mill: it
   carries every measurement of the SiteWise `CNC` model (pinned by test
   against `DeploySitewiseModels.py`) plus three identity tags —
   `AssetName = 5-Axis-Milling-CMX-441`, `EquipmentID = L3-M1` (the MES
   equipment id), `MaintenanceAssetNum = 1001-CMX-441` (the CMMS asset) — so
   the graph can discover the cross-system identity from the data instead of
   being told it. The Dockerfile bakes `site4_indianapolis_machining.csv.gz`
   as `/app/datasets/Indianapolis.csv.gz`; `app.py`, `deploy.sh` and the
   docker-context hygiene test list the fourth file.

5. **The degradation is always recent, and deterministic.** The dataset loop is
   `NUM_STEPS` × 5 s (today 5,760 steps = 8 hours; the generator's "24-hour"
   docstring predates `TOTAL_SECONDS = 28800`). Within each loop, Line3's
   vibration follows `cmx441_trend`: a linear climb from the healthy baseline
   1.0 mm/s to the alarm level 2.8 mm/s over the first 90 % of the loop (X
   axis; Y and Z track at 0.92 and 0.85), then a **fixed** bearing-replacement
   downtime of 240 steps (20 minutes) during which the mill reads `alarm` and
   ~0 mm/s, then the healthy baseline until the loop wraps. Coolant temperature
   follows the same cycle 68 → 84 °C. Jitter is ±2 % (±0.6 % for temperature)
   and computed from `(step, salt)`, never from RNG state, so two generations
   are byte-identical (test-pinned). The numbers are the seed's: alarm 2.8 mm/s
   and post-repair 0.9–1.3 mm/s from CMMS work order 1195, coolant range from
   the feed handler. When the live path replaces the button, the MES-downtime
   simulator's `VIB-BEARING` reason on an Indianapolis line is what produces the
   matching CMMS emergency work order through decision 3; the OPC UA dataset
   and the MES simulator are not clock-synchronised (the dataset loops from
   container start), so "CMMS and telemetry agree" means the same failure
   shape appears in both systems, not the same timestamp.

6. **The feed button stays as a fallback, marked for removal.**
   `lambda-sitewise-feed` and the dashboard button are untouched and now
   documented as the fallback. They are removed once the
   live OPC UA → SiteWise path has run on a fresh Workshop Studio deployment.

7. **No new deploy flag.** The fourth server ships inside `highbyte_enabled`
   like the other three. The separate `opcua_enabled` flag (OPC UA servers
   without HighByte) came later, with SFC
   ([ADR-0082](0082-sfc-writes-sitewise-for-unattended-deployments.md)
   decision 7).

8. **HighByte config gets the mechanical entries.** `config/highbyte/
   intelligencehub-configuration.json` gains the `opc_indianapolis` connection,
   the `AnyCompany_Indianapolis` input and the `Indianapolis` namespace node,
   cloned from Cedar Rapids. The existing flows iterate `event.value.AnyCompany`
   by key, so the new site is picked up without a flow change. The HighByte
   configuration is owned by the workshop owner and has not been validated
   against a running Hub in this change.

## What changes for the workshop

Nothing in the seeded story moves: B-2847, WO-9921, CMX-441, work orders
1182/1195 and their dates are untouched (`tests/unit/test_seed_defect_chain.py`
still passes). `erp_indianapolis` and `mes_line3` remain the systems modules 2,
5 and 7 trace. The live MES view `mes_indianapolis` is a distinct schema the
workshop manifest does not register. Module 6 changes in a **workshop-repo**
follow-up: from "press the button, then look in SiteWise" to "the Indianapolis
OPC UA server is feeding SiteWise; find the climb", once the pipeline (decision
9 below) delivers the fourth server's tags to the existing asset.

## Consequences and follow-ups

- **OPC UA → SiteWise for all four servers** is owned separately (the
  SiteWise Fleet Connector leg). The Indianapolis dataset is shaped so the
  existing `5-Axis-Milling-CMX-441` asset can be fed without changing SiteWise
  onboarding or module 6's questions; the mapping from
  `AnyCompany/Indianapolis/Machining/Line3/Line3CNC1` to that asset name lives
  in the pipeline configuration, not in this repository.
- **Sales orders and replenishment for the fourth site**: extract the
  segment → category map from `order_simulator.generate_orders` and the FG
  location `CASE` from `create_production_runs` into small helpers (both are
  gate-frozen legacy functions), then add `Machined Components` to the
  `AUTOMOTIVE` segment and `IND-FG` to the replenishment query. Until then the
  site produces to its reorder point and ships from stock.
- **Plant-ERP write-through** (decision 2): simulated Indianapolis orders live
  in the corporate ERP only. If the demo needs the plant ERP to show live
  orders, add a seed-DB writer modelled on the CMMS bridge.
- **Feed removal**: delete `lambda-sitewise-feed`, the `ADTERPDashboardStack`
  Lambda, the dashboard control row and the `architecture.md` /
  `highbyte.md` sections after one validated fresh Workshop Studio deployment.
- **Workshop content**: module 6 rewrite and the seed-defect-chain test
  extension for the live path (workshop repository).
- **Dataset size**: a fourth site adds roughly a third to the generated
  datasets and the OPC image; the image still builds from the `highbyte/`
  allowlist context.

## Verification

- `agentic_digital_thread/tests/unit/test_mes_cmms_bridge.py` — prefix map has
  four sites; Indianapolis downtime writes `cmms_indianapolis.work_orders`
  (IBM Maximo shape, `BEARING` code, `wonum` ≤ 15 chars) and never touches
  `cmms_maintenance`; legacy sites unchanged.
- `agentic_digital_thread/tests/unit/test_generate_datasets_fourth_site.py` —
  trend monotone then alarm then healthy; emitted samples on the trend within
  jitter; stop reads ~0 with `alarm` status; every `CNC` model property has a
  tag; identity tags constant; byte-identical regeneration.
- `tests/test_highbyte_stack.py::test_four_opc_ua_sites_are_synthesized` —
  four `IDO-OPC-Server-*` services, `SITE_NAME` per task, `OpcUaEndpoint4`,
  `OPC_UA_ENDPOINT_4` on the setup Lambda.
- `tests/test_docker_context_hygiene.py` — fourth dataset allow-listed and
  copied.
- Domain-neutrality ratchet unchanged: the new tests address the site through
  the simulator's own constants, not literals.
