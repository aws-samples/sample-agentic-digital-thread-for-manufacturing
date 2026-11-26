<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Local transactional data via the erp_simulation engine

---
Status: accepted
---

The local stack seeds schema + **reference** data through `lambda-db-init`
(`scripts/seed_local_db.py`), but the **transactional** rows (customer orders,
shipments, invoices, MES work orders, production events, quality checks) are not
part of that seed. In a deployment they are produced at runtime by the
`erp_simulation` engine, which runs continuously as an ECS task and advances a
state machine (orders → production → MES work orders → shipping → invoicing).
Without it, the local database has only reference data, so cross-system chat
questions about orders/work-orders/quality return "no data".

## Decision

Add `scripts/simulate_local.py` (exposed as `scripts/local_dev.sh simulate`) to
drive the **same** `erp_simulation` engine against the local PostgreSQL for a
fixed number of cycles. Because the engine gates state transitions on real
wall-clock intervals (`NOW() - INTERVAL '…'`; orders take hours, shipping days,
payment weeks), the runner **fast-forwards a simulated clock** — after each
cycle it ages every `public` timestamp column by `--time-step` (default
`1 day`), preserving relative ordering while letting records become "old enough"
for the next cycle to advance. This flows the lifecycle in ~40 cycles instead of
real weeks. We keep `lambda-db-init` as the schema/reference seed; the simulation
layers transactional data on top (it does not recreate schema or reference data).

## Consequences

Verified locally: ~240 customer orders, ~237 shipments, ~226 invoices, plus
production runs and work orders across all three plants — enough for the
order-to-cash and batch→customer cross-system stories. The chat reasoning loop
runs locally end to end (`query_neptune_graph` → `get_system_schema` →
`execute_sql_<id>`).

Known limitations / follow-ups:

1. **MES quality checks (fixed) / downtime (sparse).** The root cause was a
   **timezone skew**, not the linkage drift first suspected. The engine writes
   timestamps with naive `datetime.now()` while PostgreSQL runs in UTC and the
   columns are `timestamp without time zone`; on a non-UTC host every just-set
   `planned_end_time` looked already in the past, so work orders completed in the
   same cycle they started and were never observed `RUNNING` — and quality checks
   /downtime (which require a `RUNNING` work order) never generated. Deployment is
   unaffected because the ECS task runs in UTC. Fixed by running
   `simulate_local.py` with `TZ=UTC` (matching deployment): quality checks now
   populate (~1 per work order; ~21 across plants in a 40-cycle run). Downtime is
   probabilistic (20% per RUNNING work order per cycle) and stays sparse under
   day-scale steps, where each work order is RUNNING for only one cycle — a
   smaller `--time-step` keeps work orders RUNNING longer. Minor follow-up.

2. **Multi-timescale tension.** MES runs in minutes/hours, ERP order-to-cash in
   days/weeks; a single uniform `--time-step` can't optimally populate both. A
   `1 day` step gives the best order-to-cash coverage. A cleaner fix would be a
   demo "fast clock" *inside* the engine (config-driven intervals) rather than the
   external timestamp-aging in the runner.

3. **Local chat fidelity gaps** (independent of data):
   - **Tool-name mismatch — fixed.** Local direct-MCP registered bare
     `execute_sql_<id>` while the prompt/per-system query_guidance expect the
     gateway-prefixed `<id>___execute_sql_<id>`, so guidance-following calls hit
     "Unknown tool". `system_agent.create_mcp_server` now accepts a
     `tool_name_prefix` (default `""`, so the cloud gateway path is unchanged) and
     the local entry point passes `<id>___` to emulate the gateway — local tool
     names now match the cloud names in both modes. Verified: a previously-failing
     ERP question returns real data (215 orders, 206 shipments) via
     `erp___execute_sql_erp`.
   - **`intelligence_summary` on the local stream — fixed.** `LocalChatClient`
     now tallies tool calls and emits the same `intelligence_summary` metadata the
     cloud runtime does, so `scripts/smoke_prompts.py` grades grounding on the real
     `/api/ask` SSE path locally. Verified: 3/3 prompts PASS with system_queries up
     to 15 and neptune_queries up to 3 (cross-system + graph-guided proven).
   - **Orchestrator-rebuild duplicate-tool error — resolved.** Earlier this
     surfaced as `Tool name 'query_<id>' already exists` on the first query. The
     cause is eliminated by two earlier fixes: keying `_mcp_clients` by system_id
     (one client per system) and the gateway tool-name prefix (globally unique
     names per system), so a rebuild can never present two same-named tools.
     Verified: firing 3 concurrent first-queries during/after restore produced 0
     `already exists` / `Failed to load tool` errors. Two benign residuals remain:
     (a) a query arriving before restore finishes may briefly see a not-yet-
     registered system ("Unknown tool") and self-heals once restore completes;
     (b) the local orchestrator is a single in-process Agent, so truly concurrent
     queries can 500 — the normal local path (one query at a time) is unaffected.

4. **`/api/ask/sync` is dead code.** It existed for an AWS Quick Lambda proxy
   that is no longer in the repo (zero callers remain); it also blocks for the full
   agent run (~30–80 s) with no keepalive, so it is timeout-exposed behind API
   Gateway / ALB / CloudFront. It and `/api/ask` share the orchestrator path. This
   is a removal candidate (separate change).

## Enhancement: simulation as a first-class local data source

Longer term, the local simulation could **replace the static base seed data** so
local data is live and evolving, matching deployment behavior instead of a
static snapshot. Path to get there: (a) fix the work-order→production_run linkage
so MES quality/downtime populate; (b) move time-compression into the engine as a
config-driven "demo clock" rather than the external aging hack; (c) optionally
run the engine as a background loop (e.g. an opt-in mode of `local_dev.sh up`) so
the local thread continuously accrues data the way the ECS task does in a
deployment.
