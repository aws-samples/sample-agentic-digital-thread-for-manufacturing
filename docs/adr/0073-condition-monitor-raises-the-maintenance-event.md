<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0073: The story mill's maintenance event is raised from its vibration spectrum, not from a downtime

## Status

Proposed — 2026-09-23. Extends [ADR-0083](0083-indianapolis-fourth-site.md)
(fourth site, CMX-441 carried live by its OPC UA server). Requirement: the
trigger comes from the frequency conversion of the raw vibration data.

> **2026-09-27 notes.** (a) Decision 6: the "Indianapolis Plant ERP" name is
> retired with the plant ERP itself ([ADR-0084](0084-one-erp-and-the-workshop-story-setting.md));
> "Indianapolis MES" and "Indianapolis Plant Maintenance" and the naming rule
> stand. (b) Decision 4: the condition monitor now runs on a data-keyed
> anchor when `simulation_config.story_anchor_epoch` exists (the live
> workshop story, [ADR-0085](0085-live-story-clock-history-and-lookup.md));
> without it the loop is the wall clock described here, unchanged. The OPC
> UA server stays wall-clock in both cases, so under the anchor decision 4's
> "same row at the same instant" no longer holds for the live story (an
> ADR-0079 known limit). The body below is unchanged.

> **2026-09-29 notes ([ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md)).**
> (a) Decision 5: the monitor's order is written by
> `MESSimulator.insert_condition_work_order` with `reported_date =
> target_start =` the loop's BPFO alarm instant (`condition_monitor.alarm_epoch`,
> naive UTC) rather than the noticing cycle's `NOW()`, and `sched_start` =
> the bearing failure; its description carries the features at
> `BPFO_ALARM_STEP` ("BPFO band 0.70 mm/s") whatever cycle notices the alarm.
> This holds on both clocks, so frozen and demo `VIB-CMX` timestamps move to
> the alarm instant (at most one cycle earlier). (b) Decision 4: in the live
> story the SiteWise telemetry now follows the same anchored model on an
> epoch 300 s grid (congruent anchors, `anchor % 300 == 10`, put the alarm on
> a grid point), so "the value the CMMS rule evaluates is the value
> SiteWise shows" holds again there; the OPC UA server stays wall-clock. The
> body below is unchanged.

> **2026-10-10 note (decision 5, the order's lifecycle).** One order per loop
> left every earlier loop's order open forever, so an account that runs for
> days collected one open `VIB-CMX` order per loop (13 on one deployment
> after four days) and the facilitator answer key cut its maintenance list at
> its 20-row limit. Each order now stays open (`WAPPR`) through its own loop,
> as before: the alarm, the bearing failure, the repair stop and the healthy
> tail. Once that loop has ended, the next cycle completes it in the same seed
> transaction as the monitor's insert
> (`MESSimulator.complete_condition_work_orders`): status `COMP`,
> `actual_start` = its `sched_start` (the bearing failure of its own loop),
> `actual_finish` = the end of that loop's repair stop, and one `COMPLETE`
> `work_order_log` entry dated `actual_finish`, the shape of the history's own
> completed condition order `3107`. Every value comes from the order, so the
> close-out is deterministic and idempotent across cycles and restarts, and
> it runs in any phase from loop 1 on, so an account that already piled up
> orders is brought back on its first cycle. Loop 0 of a story clock has no
> earlier loop (the history and every placement delete the condition orders
> before the anchor) and is unchanged: no close-out and no extra seed round
> trip, so a fresh workshop account behaves exactly as before. The wall-clock
> loop (demo, frozen) follows the same rule. The alternative, raising no new
> order while an earlier one is open, was rejected: from the second loop on
> the open order would carry an earlier alarm instant and a bearing failure
> already in the past, so the CMMS sign ("is the newest order scheduled for a
> moment still ahead?") would say the plant is not in its alarm when it is.
> The live answer key leaves completed condition orders out
> ([ADR-0085](0085-live-story-clock-history-and-lookup.md) known limits).
> The MES-downtime bridge's `IND-*` orders (decision 5's last sentence) follow
> the same rule in the same transaction and under the same story-clock check:
> open through the loop they were raised in (the facilitator table's "stays on
> file, waiting" in repair and healthy), completed once that loop has ended
> (`MESSimulator.complete_bridge_work_orders`), worked in the downtime they
> were raised from as the ERP records it (`actual_start` / `actual_finish` =
> its start and end, the log entry by whoever resolved it, dated no earlier
> than the order itself). The ERP downtime is read through
> `MESSimulator.read_downtimes` on the orchestrator's own connection. Only a
> PROVEN bridge order is touched: reported by the bridge (`MES bridge`) and
> its description in the writer's form, ending in the canonical (lower-case)
> id of the downtime, which reproduces the order's number. The `IND-` prefix
> alone proves nothing: a person or another integration can raise an
> `IND-*` order, and the simulator has no business closing it. An unproven
> `IND-*` order of an ended loop stays exactly as it is, and the monitor names
> it in one warning per loop. A proven order whose downtime is still open
> waits; a proven one whose downtime row is gone (a placement retires the old
> era's story runs with their downtime rows; a db-init re-run wipes the ERP)
> is completed at the moment it was entered, by the planner, so no bridge
> order stays open forever. The completion re-checks the reporter and the
> exact text it proved, so an edit landing in between leaves the order open.
> Both answer keys, live and frozen, leave completed simulator orders out:
> the `VIB-CMX-*` number space only the monitor writes, and `IND-*` orders
> the bridge reported. A completed `IND-*` order someone else reported is that
> person's maintenance record and stays listed, as do the seed's and the
> history's completed orders.

> **2026-10-14 note (decision 5, the bridge on the story mill).** A random
> MES downtime no longer plays the story mill's failure. At the
> condition-monitor site the downtime draw leaves out the story mill's own
> failure modes (`MESSimulator.STORY_FAILURE_REASONS`, the reasons the site
> CMMS codes as `BEARING` and `DIM-DRIFT` and the bridge aims at the mill),
> on every line, since the bridge picks the asset by reason and not by line.
> Before this, a random `MECHANICAL` / `VIB-BEARING` draw opened a priority-1
> `EM` bearing order on the mill whenever it fell: on a workshop run on
> 2026-10-14 one was open two hours before the story's alarm, and every answer
> from module 4 on cited it. The condition monitor's `VIB-CMX-<loop>` order
> and the story history's bearing downtime are the story's only bearing
> events there; the bridge, its writer and the other sites are unchanged,
> and other random stops at the site still raise their orders on other
> assets. This replaces an earlier same-day reservation that left out
> `VIB-BEARING` on the story line only: a draw on another line still reached
> the mill, and `INSTR-DRIFT` was not covered.

## Context

ADR-0079 made the CMX-441 mill's vibration climb to the 2.8 mm/s story alarm
over each OPC UA dataset loop and stop for a fixed bearing replacement. The
maintenance side, however, still came from the MES-downtime bridge: a random
MES downtime with a `VIB-BEARING` reason produced the CMMS work order, so the
work order *followed* a stop instead of predicting one, and nothing tied it to
the vibration the attendee was looking at.

In a production condition-monitoring deployment the raw accelerometer waveform
is captured at high rate at the edge, an FFT is applied locally, and only the
derived features flow up: overall RMS velocity, the amplitude in the bearing
defect bands (BPFO, BPFI, BSF, FTF), running-speed harmonics, crest factor and
kurtosis, keyed to spindle speed. Alarms are band-specific, and an exceedance
raises the CMMS work order with the offending spectrum attached, so the
technician sees *which* fault is developing. That closed loop is what makes
vibration a leading signal instead of a lagging one.

Two facts about the deployment shape constrain how we simulate that loop:

* The OPC UA servers are containers playing a pre-generated CSV; nothing in the
  cloud reads their tags until Shop Floor Connectivity lands them in SiteWise
  (a separate, not yet built leg).
* The MES/CMMS simulation runs in a Lambda against PostgreSQL and has no OPC UA
  client.

## Decision

1. **One model, two consumers.** `erp_simulation/condition_monitor.py` holds
   the spindle-bearing degradation curve as pure, dependency-free functions:
   the RMS trend ADR-0079 already used, plus the frequency-domain features an
   edge analyzer would publish (`one_x`, `bpfo`, `bpfi`, `crest`, `kurtosis`)
   and the band alarm. It lives in the `erp_simulation` package because the
   Lambda images ship that package; `highbyte/generate_datasets.py` imports it
   across the repo root. A test pins that the module imports nothing but
   `math` and `typing`.

2. **The mill publishes the spectrum.** Every machining CNC in the fourth
   site's dataset gains `Spectrum_1X`, `Spectrum_BPFO`, `Spectrum_BPFI`,
   `CrestFactor`, `Kurtosis`, `BPFO_Alarm`, `BPFO_Hz` and `BPFI_Hz` next to
   `Vibration_X/Y/Z`; the story mill's follow the degradation, the others sit
   at healthy values. The defect frequencies are computed from the spindle
   speed and a fixed bearing geometry (9 rolling elements, d/D·cos α = 0.33:
   BPFO 311.5 Hz, BPFI 618.5 Hz at 6200 rpm). The SiteWise `CNC` model gains
   the same eight properties, so the existing asset receives them without an
   onboarding change.

3. **The BPFO band leads.** The band grows with the *square* of the
   degradation fraction and crosses its threshold (0.70 mm/s) at loop step
   3898, while RMS is still 2.35 mm/s; the mill stops at step 5184. That is a
   1.8 h leading window in an 8 h loop: the work order exists while the mill
   is running, and the fixed bearing-replacement downtime that follows is the
   repair it scheduled (its `sched_start`), not the event that discovered the
   fault.

4. **Wall-clock alignment makes "from the vibration data" true.** The OPC UA
   server (`components/opc-server/server.mjs`) now serves row
   `(epoch ms mod loop length) / interval` instead of incrementing from 0 at
   start, and the simulator derives the same row from the same clock
   (`condition_monitor.loop_step`). At any instant the value the CMMS rule
   evaluates is the value the OPC UA server is serving; a restart of either
   side changes nothing. Loops align to 00:00, 08:00 and 16:00 UTC.

5. **The condition monitor raises the work order.**
   `MESSimulator.monitor_asset_condition` runs every cycle (from the downtime
   step): when the band alarm is set it inserts one `CM` / priority 1 /
   `BEARING` work order per loop into the site's own CMMS
   (`cmms_indianapolis.work_orders`, `wonum VIB-CMX-<loop>`, reported by
   "Condition monitor", description naming the band, its frequency, the
   threshold and the supporting features). `ON CONFLICT DO NOTHING` makes it
   idempotent across cycles; a missing seed DB or a failure logs loudly and
   returns 0, never breaking the MES cycle. The MES-downtime bridge is
   unchanged: an unplanned stop still raises an `EM` order, which is also how
   a real plant behaves.

6. **The workshop's Indianapolis systems are named as products.** The three
   workshop-only registrations become "Indianapolis Plant ERP", "Indianapolis MES"
   and "Indianapolis Plant Maintenance" (ids and schemas unchanged). "Indianapolis
   CMMS" next to the demo's all-sites "CMMS" read as a site filter on the same
   system, which is exactly how a reviewer read it. The MES keeps the
   per-plant form the demo already uses ("Milwaukee MES", "Detroit MES"): there
   is no shared MES to be a filter on, and the `mes_line3` seed holds runs on
   L3, L5 and L7, so a line-scoped name ("Line 3 MES", briefly considered)
   would have been both unrealistic and wrong about its own data. Vendor names were
   considered and rejected for attendee-facing text: the workshop is public,
   the schemas only *imitate* vendor conventions, and no attendee-facing
   content names a vendor today. A test refuses any workshop name that is a
   demo name or a demo name prefixed with the plant. Demo names do not change.

## What this is and is not

It is a simulation of the closed loop at the fidelity the workshop needs: the
features are generated from the degradation model, not computed by an FFT over
a synthesized waveform, and the rule evaluates the shared model rather than
subscribing to the OPC UA tag. Both are stated in the code. When SFC → SiteWise
exists, the production-shaped alternative is a SiteWise alarm on
`Spectrum_BPFO` driving the work order through EventBridge; the tag names and
the work-order shape chosen here are the ones that path would use.

## Consequences and follow-ups

* Every OPC UA server's playback is now wall-clock aligned, including the
  three legacy sites (harmless: their data is random; their CSVs are
  byte-identical to before).
* `simulate_downtime_events` returns `condition_work_orders_created` alongside
  the downtime count.
* The onboarding mapping guidance treats spectral bands, crest factor,
  kurtosis and band alarms as `vibration` concepts.
* Still open from ADR-0079 and unchanged here: the workshop's plant ERP and
  Indianapolis MES remain February snapshots next to the live simulator; feeding
  them from the simulator's Indianapolis production is the next piece. Module
  6 content moves from the feed button to the live spectrum in a workshop-repo
  follow-up; the workshop content also carries the three renamed system names.
* Not verified: a deploy, the HighByte connection importing the new tags, and
  the SiteWise path (needs SFC).

## Verification

* `agentic_digital_thread/tests/unit/test_condition_monitor.py` — geometry
  parity with the generator, dependency-free module, alarm leads the stop by
  ≥ 1 h, monotone features, stop/reset, wall-clock `loop_step`, server.mjs
  pinned to `Date.now()` alignment, description fits `VARCHAR(250)`.
* `test_generate_datasets_fourth_site.py` — spectral tags on every mill,
  alarm window in the emitted CSV, band climbs/stops/resets, other mills
  never alarm, defect-frequency tags constant while running.
* `test_mes_cmms_bridge.py` — monitor quiet under threshold / stopped /
  after repair, raises once per loop with the expected shape, idempotent,
  loud-but-safe on failure, wired into the downtime step.
* `test_workshop_mode.py` — renamed manifest; names read as distinct products.
* Regenerated datasets: story mill alarm flips at step 3898, clears at 5184,
  healthy from 5424; legacy site files byte-identical.
* Maintainability gate and domain-neutrality ratchet green.
