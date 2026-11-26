<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Local development

A **hybrid** local stack for fast iteration on the ADT app. The FastAPI app, the
orchestrator, the system agents, **PostgreSQL**, and the **knowledge graph**
(local Memgraph) run locally; only **Amazon Bedrock** (model inference) is used
in the cloud using your AWS credentials. This bypasses the Amazon Bedrock AgentCore
Gateway/Runtime layer and the full CDK deploy—the edit-and-test loop is a uvicorn
hot-reload.

All commands are subcommands of `scripts/local_dev.sh` (run from the repo root),
matching the repo's `deploy.sh` / `cleanup.sh` convention.

> **Only working on the UI?** There is a much lighter alternative that needs
> no Docker, PostgreSQL, or local backend: run the Vite dev server and proxy
> `/api` to an already-deployed environment
> (`VITE_PROXY_TARGET=https://<YOUR_CLOUDFRONT_DOMAIN> npm run dev`). Sign-in,
> chat, and all read/write API flows work against the deployed backend while
> the frontend hot-reloads locally. See
> [`agentic_digital_thread/frontend/README.md`](../agentic_digital_thread/frontend/README.md#frontend-only-development-against-a-deployed-backend)—and
> note the caveat there: the target backend is live, so writes are real.

> Design decisions and verified findings live in the ADRs:
> [0022 hybrid stack](adr/0022-hybrid-local-development-stack.md) ·
> [0023 `has_runtime` local fallback](adr/0023-local-mode-has-runtime-fallback.md) ·
> [0024 Amazon Neptune reachability + local openCypher backend](adr/0024-neptune-private-graph-not-reachable-locally.md) ·
> [0025 local transactional data through the simulation engine](adr/0025-local-transactional-data-via-simulation.md).

## What runs where

| Capability | Local? | How |
|---|---|---|
| Web UI + FastAPI API | Yes | native uvicorn, hot reload |
| Orchestrator + system agents | Yes | `AGENT_MODE=local` |
| Auth | Yes (bypassed) | Exact local trio: `DEV_NO_AUTH=1`, `DEV_MODE=1`, `AGENT_MODE=local` → user `dev-user-001` |
| PostgreSQL source data | Yes | local container, auto-seeded |
| Amazon Bedrock reasoning + `/api/ask` | Yes | real cloud Bedrock through your AWS credentials |
| **Knowledge-graph onboarding + analytics** | Yes (local Memgraph) | `GRAPH_BACKEND=local`—the discover, map, resolve, and provision steps and the analytics endpoints all run against a local graph |
| Agents fleet page | Yes (local provider) | in-process roster + status + per-system model + invocation/tool-call counters and orchestrator token/cost; metrics are cumulative since app start. System agents *do* run the configured `SYSTEM_AGENT_MODEL` (Claude Haiku 4.5 by default) locally (the `query_<id>` NL tool; `execute_sql_<id>` bypasses the LLM), but the fleet metrics don't record their token usage yet, so system-agent tokens show 0 |
| Amazon DynamoDB, AWS IoT SiteWise, and Amazon Bedrock Guardrails | No (off) | Optional; no-op locally. |

> **One set of openCypher, two backends.** `agents/db.py` has a single
> `GRAPH_BACKEND` switch (`neptune` default, or `local` → Memgraph over Bolt).
> The *same* queries run on both—local Memgraph is the fast inner loop. Before
> relying on graph-sensitive changes, validate against a deployed Amazon Neptune
> Analytics graph, which is not reachable from a laptop
> ([ADR 0024](adr/0024-neptune-private-graph-not-reachable-locally.md)). Run
> `scripts/graph_parity_check.py` to catch dialect drift early.

## Prerequisites

- **Docker** (or a compatible engine) for the PostgreSQL + Memgraph containers.
  Memgraph is distributed under the Business Source License 1.1—review its
  license terms before use (details in [`NOTICE`](../NOTICE)).
- **Python 3.12** (the tested interpreter; `pyproject.toml` nominally allows 3.11+).
- **AWS credentials** (a profile or ambient creds) with **Amazon Bedrock** access
  in your Region, and the `BEDROCK_MODEL` / `SYSTEM_AGENT_MODEL` models enabled
  there (see [`deployment.md`](deployment.md)).

## First-time setup

Complete the [prerequisites](#prerequisites) before starting.

1. Install the application and local extras, including the Neo4j Bolt driver:

   ```bash
   scripts/local_dev.sh install
   ```

2. Create the local environment file:

   ```bash
   cp .env.local.example .env.local
   ```

   The shipped example sets **`ADT_DEMO=1`** (without it the onboarding catalog is
   empty and step 6 fails with "absent from the live onboarding catalog") and
   **`ADT_SETTINGS_STORE=none`** (without it the app reads the per-deployment SSM
   toggles from whichever account your profile points at, so an account with
   `/adt/demo-enabled=false` silently disables the demo addon locally and
   overrides `ADT_DEMO`).

   Local use without signing in requires the complete, exact trio already present in the
   example: **`DEV_NO_AUTH=1`**, **`DEV_MODE=1`**, and **`AGENT_MODE=local`**.
   Partial or differently spelled values are rejected. Amazon ECS runtime markers and
   stack-owned cloud wiring override the trio and fail closed, so this bypass
   must never be added to a deployed task definition.

   `GRAPH_BACKEND=local` is the default, so the graph runs in the local Memgraph
   container and does not require Amazon Neptune Analytics or `GRAPH_IDENTIFIER`. Edit
   `.env.local` to set `AWS_REGION` and, when using a named profile,
   `AWS_PROFILE`.

3. Start PostgreSQL and Memgraph. The first run also seeds the schema, reference
   data, and ERP and MES view schemas with `scripts/seed_local_db.py`:

   ```bash
   scripts/local_dev.sh up
   ```

4. Generate transactional data. Without this step, the database contains only
   reference data and chat answers "no data":

   ```bash
   scripts/local_dev.sh simulate --cycles 40 --time-step "1 day"
   ```

5. Run the application with hot reload and leave it running:

   ```bash
   scripts/local_dev.sh run
   ```

   The web UI is served from `agentic_digital_thread/frontend/dist`—on the
   first run this command builds it automatically (needs Node 20+ / npm).
   The app listens on `127.0.0.1:8000` only; to reach it from another
   machine, set `ADT_DEV_HOST` (for example `ADT_DEV_HOST=0.0.0.0`, in the
   shell or `.env.local`), and only on a network you trust.
   After changing frontend code,
   rebuild with `cd agentic_digital_thread/frontend && npm run build`, or use
   the hot-reloading Vite dev server instead
   (`npm run dev`—see the [frontend README](../agentic_digital_thread/frontend/README.md)).

6. In a second terminal, onboard the systems into the knowledge graph. The
   application from step 5 must be running; all six systems take about 5–8
   minutes:

   ```bash
   .venv-local/bin/python scripts/onboarding_smoketest.py \
       --base-url http://127.0.0.1:8000 --password dev --region "$AWS_REGION"
   ```

You now have a fully usable local stack: open <http://localhost:8000> and ask
cross-system questions, or run the [smoke check](#verifying-the-stack). On boot
the app seeds the 65 core manufacturing concepts into the local graph (look for
`Seeded N core concepts` in the log).

> **Memgraph is in-memory.** After `down`/`db-reset` or a Memgraph restart the
> graph resets—concepts re-seed on app startup, but re-run step 6
> (`onboarding_smoketest.py`) to repopulate the onboarded systems. PostgreSQL data persists
> in the `adt-local-pgdata` volume (re-run step 4 only after `db-reset`).

## Onboarding a system locally

The knowledge-graph onboarding flow runs fully locally for **all the SQL
systems**. Drive it with the same harness the smoke test uses, pointed at
localhost:

```bash
.venv-local/bin/python scripts/onboarding_smoketest.py \
    --base-url http://127.0.0.1:8000 --password dev \
    --region "$AWS_REGION" --systems erp,mes-mil,mes-det,mes-cr,cmms,plm
```

The flow runs end-to-end on the local stack: `discover` (PostgreSQL), then `map`
(Bedrock), then `resolve` (cross-system identity rules), then `provision`,
ending in catalog status `onboarded`, with `System`/`SchemaEntry`/`Field`/`MAPS_TO`/`IdentityRule` written
to Memgraph and the `/api/thread/analytics/*` endpoints reading them back. For
example, `erp` discovers 25 view-tables, producing 229 field mappings, and
`mes-cr` forms 9 cross-system identity rules.

### How the SQL systems are seeded

The catalog points systems at two kinds of schema, both seeded into the single
local PostgreSQL:

- `cmms_maintenance`, `plm_engineering`—from `lambda-adt-seed/*.sql`,
  auto-loaded by the PostgreSQL container on first `up`.
- `erp_data`, `mes_milwaukee`, `mes_detroit`, `mes_cedarrapids`—**view**
  schemas that, in a deployment, `lambda-db-init` builds in the ERP database.
  `scripts/seed_local_db.py` runs that same create/seed logic against the local
  DB. `scripts/local_dev.sh up` runs it automatically on first start (when
  `erp_data` is absent); re-run anytime with `scripts/local_dev.sh seed`.

### Generating transactional data

`lambda-db-init` (the seed) creates the schema + **reference** data (customers,
products, assets, production lines) and the simulation *config*—but the
transactional rows (orders, work orders, shipments, invoices, quality checks)
are produced at runtime by the `erp_simulation` engine, which in a deployment
runs continuously as an ECS task. Generate them locally with:

```bash
scripts/local_dev.sh simulate --cycles 40 --time-step "1 day"
```

The engine gates state transitions on real wall-clock time (orders take hours,
shipping days, payment weeks), so `simulate_local.py` fast-forwards a simulated
clock (ages timestamps each cycle) to flow the lifecycle in bounded cycles
instead of real days. A typical run produces roughly 240 customer orders, 237
shipments, and 226 invoices, plus production runs and work orders across all
four sites (Milwaukee, Detroit, Cedar Rapids and Indianapolis).

The engine writes timestamps with naive `datetime.now()` and PostgreSQL runs in
UTC, so on a non-UTC host the two clocks diverge and work orders complete before
they're observed `RUNNING`. `simulate_local.py` runs the engine with `TZ=UTC`
(matching the deployment ECS task) to fix this, so MES work orders, production
events, and **quality checks** populate across all four sites.

> **Known limitation—MES downtime is sparse.** Downtime events fire
> probabilistically (20%) on `RUNNING` work orders; under day-scale steps each
> work order is `RUNNING` for only one cycle, so few/no downtime events generate.
> Use a smaller `--time-step`, or apply the `equipment_down` scenario (below)
> which raises downtime probability to 50%. Tracked in
> [ADR 0025](adr/0025-local-transactional-data-via-simulation.md).

### Firing simulation scenarios

The simulator reads tunable parameters (downtime probability, quality pass rate,
lead times, payment rate, …) from the `simulation_config` table. Predefined
**scenarios** set those in one shot—the *same* scenarios behind the ERP
dashboard's buttons, defined once in
[`erp_simulation/scenarios.py`](../erp_simulation/scenarios.py):
`normal`, `demand_surge`, `mes_outage`, `supply_crisis`, `equipment_down`,
`cash_crunch`, `quality_issues`, `capacity_plus`.

Apply one before generating data:

```bash
# for example frequent breakdowns (also the workaround for the sparse-downtime note above):
scripts/local_dev.sh simulate --scenario equipment_down --cycles 40 --time-step "1 day"
```

To mimic the deployed continuous simulator (a `rate(1 minute)` AWS Lambda function running
`single_cycle`), run a real-time loop—one cycle every `--interval` seconds:

```bash
scripts/local_dev.sh sim-loop --interval 60          # add --scenario NAME to bias it
```

### ERP dashboard (optional)

The cloud "ERP dashboard" (Streamlit, ECS in a deployment) can run locally
against the same PostgreSQL—it gives the scenario **buttons** and live KPIs:

```bash
scripts/local_dev.sh dashboard      # http://127.0.0.1:8501 (installs streamlit on first use)
```

The buttons only *set config*—run `sim-loop` in another shell so button-presses
produce changing data over time.

### Chat / cross-system Q&A

The orchestrator reasoning loop runs locally. Two endpoints share the **same**
orchestrator path (differ only in transport): `POST /api/ask` streams SSE (the
real UI path) and `POST /api/ask/sync` returns one JSON blob. For a faithful
smoke check use the streaming tool:

```bash
.venv-local/bin/python scripts/smoke_prompts.py \
    --base-url http://127.0.0.1:8000 --password dev --count 3
```

A cross-system question issues a chain of tool calls
(`explore_graph` → `get_system_schema` → `execute_sql_mes_det`) and
returns a grounded answer.

> **Known local-mode chat gap** (see
> [ADR 0025](adr/0025-local-transactional-data-via-simulation.md)): the local orchestrator is a
> single in-process Agent, so firing multiple queries at once can 500—ask one
> at a time. (The earlier tool-name mismatch, duplicate-tool rebuild error, and
> missing `intelligence_summary` are all fixed—the streaming smoke gate works
> locally.)

### Steward business context in local mode

Per-system business context (ADR-0054 D4) works locally: saving through
`PUT /api/admin/business-context/{id}` writes the local graph, and each
local agent receives the enabled context in its start-up config
(same include rule as the cloud secret path). The one difference from
cloud: there is **no live refresh** of a running local agent—the PUT
response reports `runtime_refresh: "skipped"`, and the saved context is
picked up at the agent's next start (restart the app, or re-onboard the
system). This is delivery-at-start, not a write-only gap.

## Verifying the stack

```bash
# 1. Onboard every SQL system and assert the graph was populated:
.venv-local/bin/python scripts/onboarding_smoketest.py \
    --base-url http://127.0.0.1:8000 --password dev --region "$AWS_REGION"
# => PASS — 6 systems onboarded; "graph populated: N fields -> M concepts"

# 2. End-to-end cross-system chat grounding (real /api/ask SSE path; needs the
#    onboarded systems + transactional data + the app running):
.venv-local/bin/python scripts/smoke_prompts.py \
    --base-url http://127.0.0.1:8000 --password dev --count 3
# => PASS — prompts grounded; suite proves system_queries>=2 and neptune_queries>=1

# 3. Graph dialect parity (local backend vs the app's query shapes).
#    NOTE: this WIPES the local graph as part of its test — run it LAST, then
#    re-run step 1 (onboarding_smoketest.py) to restore the onboarded systems.
.venv-local/bin/python scripts/graph_parity_check.py
# => OK N/N passed
```

Three gates: `onboarding_smoketest.py` asserts each system reaches `status=onboarded`
**and** that the analytics endpoints read the written `Field`→`Concept` edges
back (a backend-agnostic graph-populated gate that also runs against real
Neptune); `smoke_prompts.py` proves the orchestrator grounds answers in real
systems + the graph over the real streaming path; and `graph_parity_check.py`
catches openCypher dialect drift (and resets the local graph).

## Daily loop

```bash
scripts/local_dev.sh up      # start PostgreSQL + Memgraph
scripts/local_dev.sh run     # hot-reloading app
```

Editing anything under `agentic_digital_thread/agents/` reloads uvicorn—no rebuild.
PostgreSQL persists in the `adt-local-pgdata` volume; **Memgraph is in-memory**, so
its graph resets on container restart (core concepts re-seed on app startup;
re-run onboarding to repopulate systems).

## Keeping the graph backends DRY / in-sync

- **One query set.** All graph access funnels through `db.neptune_query` /
  `db.neptune_write`; nothing builds its own `neptune-graph` client except the
  control-plane `get_graph` (metadata). So there is no per-engine query
  duplication—only the transport differs.
- **Parity guard.** `scripts/graph_parity_check.py` replays the app's
  representative query shapes against the local backend and fails on dialect
  drift. Run it after touching graph queries:
  ```bash
  scripts/local_dev.sh up
  .venv-local/bin/python scripts/graph_parity_check.py   # expect: OK N/N passed
  ```
- **Fidelity check.** Before relying on graph-sensitive changes, validate
  against a deployed Amazon Neptune Analytics graph
  ([ADR 0024](adr/0024-neptune-private-graph-not-reachable-locally.md)).

## Inspecting state

```bash
scripts/local_dev.sh psql        # PostgreSQL shell
# Memgraph (Bolt) — quick query through the installed driver:
.venv-local/bin/python -c "from neo4j import GraphDatabase as G; \
  d=G.driver('bolt://localhost:7687',auth=('','')); \
  print(list(d.session().run('MATCH (n) RETURN labels(n)[0] AS l, count(*) AS n')))"
```

## Tear down / costs

```bash
scripts/local_dev.sh down        # stop PostgreSQL + Memgraph (PostgreSQL data kept)
scripts/local_dev.sh db-reset    # wipe + re-seed PostgreSQL
```

Local containers are free; the only ongoing cloud cost in local dev is **Bedrock
tokens** per request. (`scripts/local_dev.sh neptune-create/-destroy` still exist
for the in-VPC real-Neptune workflow, but are not needed for local dev.)

## Environment variables

| Var | Purpose |
|---|---|
| `DEV_NO_AUTH=1`, `DEV_MODE=1`, `AGENT_MODE=local` | Exact three-signal local contract: bypass Amazon Cognito and run in-process agents. Missing/malformed values or any ECS/cloud marker fail closed ([ADR 0023](adr/0023-local-mode-has-runtime-fallback.md)). Never set the bypass in a deployed task. |
| `GRAPH_BACKEND=local` | Route graph reads/writes to local Memgraph (vs `neptune`) |
| `MEMGRAPH_URI` | Bolt URI for the local graph (default `bolt://localhost:7687`) |
| `POSTGRES_*` | Local PostgreSQL connection |
| `AWS_REGION` / `AWS_PROFILE` | Cloud Bedrock access (`AWS_PROFILE` optional with ambient creds) |
| `BEDROCK_MODEL` / `SYSTEM_AGENT_MODEL` | Region-specific inference-profile model IDs |
| `GRAPH_IDENTIFIER` | Only when `GRAPH_BACKEND=neptune` (in-VPC only) |
| `GUARDRAIL_ENABLED=false` | No guardrail provisioned locally |

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `POSTGRES_PASSWORD is not set` | Run with `scripts/local_dev.sh run` (sources `.env.local`). |
| First chat query errors about `AGENTCORE_RUNTIME_ARN` | Set `AGENT_MODE=local` / `DEV_MODE=1` ([ADR 0023](adr/0023-local-mode-has-runtime-fallback.md)). |
| `[Errno 48] Address already in use` on `run` | A previous app is alive: `lsof -ti tcp:8000 \| xargs kill -9`. |
| Onboarding `discover: 0 tables` | The ERP/MES view-schemas aren't seeded—run `scripts/local_dev.sh seed` (or it auto-seeds on a fresh `up`). |
| `graph_parity_check.py` reports DRIFT | A query uses an engine-specific construct—reconcile the openCypher or the backend. |
| Neptune `Could not connect to the endpoint URL` | You set `GRAPH_BACKEND=neptune` locally—the private graph isn't laptop-reachable ([ADR 0024](adr/0024-neptune-private-graph-not-reachable-locally.md)). Use `local`. |
| Bedrock `AccessDenied` / model errors | Enable the models in that Region and check the inference-profile prefix. |
| Finch/nerdctl `Ignoring … HealthCheck` / `volume already exists` | Benign backend messages; `db-reset` still truly wipes + re-seeds. |
| Chat: `Unknown tool` for `<id>___execute_sql_<id>` | Fixed—local agents now register gateway-prefixed names. If seen, restart the app so agents re-register with current code. |
| Chat: `Tool name 'query_<id>' already exists` on first query | Fixed ([ADR 0025](adr/0025-local-transactional-data-via-simulation.md))—`_mcp_clients` is keyed by system_id and tool names are uniquely prefixed. If seen, restart so agents re-register with current code. |
| Chat: 500s when several queries run at once | The local orchestrator is a single in-process Agent—ask one question at a time. |
| Chat answers "no data" for orders/quality | Run `scripts/local_dev.sh simulate` to generate transactional data (quality/downtime are a known gap). |
