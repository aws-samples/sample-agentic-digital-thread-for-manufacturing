<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0030: Demo data is opt-in; `base` is the default deployment

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


**Status:** Accepted
**Date:** 2026-06-24
**Related:** [0019-demo-content-is-an-addon](0019-demo-content-is-an-addon.md),
[0025-local-transactional-data-via-simulation](0025-local-transactional-data-via-simulation.md)

## Context

The sample shipped with its bundled demonstration data sources always deployed.
There are two distinct sample-data systems, and they are **not** the same thing:

- **Static seed** — `lambda-vf-seed/*.sql`, loaded once (via a content-hashed
  CloudFormation custom resource) into the platform's own `adt_demo`
  Amazon RDS instance. Creates the `erp_*`, `mes_*`, `cmms_maintenance`,
  `plm_engineering`, and `historian` schemas plus a frozen multi-plant dataset.
- **Live simulator** — `erp_simulation/` running on a *separate* Amazon Aurora
  cluster (`ADTERPDatabaseStack`), advanced every minute by an Amazon
  EventBridge-driven Lambda (`ADTERPLambdaStack`), with its own Streamlit dashboard
  (`ADTERPDashboardStack`).

Two facts drove this decision:

1. **Adopters want to connect their own systems.** The platform is the value;
   the bundled data is a demonstration. There was no supported way to deploy the
   engine alone.
2. **The two layers overlap and are coupled.** The simulator writes
   *cross-system* records into seed-defined schemas — e.g. `mes_simulator.py`
   maps a site to a CMMS asset and inserts into `cmms_maintenance.work_orders`
   (a schema the static seed creates). That write is guarded (skipped if the
   schema is absent), so the simulator does not hard-crash without the seed, but
   the cross-system story is incoherent without it.

**Schema → database routing** (`agents/db.py :: _get_conn_params_for_schema`):
`erp_*`, `mes_*`, and `public` schemas route to the ERP Aurora **only when**
`ERP_POSTGRES_HOST` is set; everything else (`cmms_*`, `plm_*`, `historian`)
always routes to the `adt_demo` RDS. `ERP_POSTGRES_HOST` is set only when
the compute stack receives the ERP database stack. This is what makes the modes
coherent and keeps the app from ever pointing at a database that is not deployed.

## Decision

Make the demonstration data **opt-in**, with `base` as the default, and keep all
demo infrastructure in **its own stacks** so the engine stacks contain no demo
artifacts at all:

| Mode | Flag | Stacks | Demo data infra |
|------|------|--------|-----------------|
| **base** | *(default)* | 4 (engine) | **none** — no source RDS, no seed, no simulator |
| **seed** | `--seed` | 5 (engine + `ADTSeedDataStack`) | `adt_demo` RDS + static sample schemas |
| **full** | `--simulator` | 8 (+ `ERPDatabase/Lambda/Dashboard`) | static seed **+** live ERP simulation + dashboard |

Rules, enforced in `app.py` via CDK context flags (`with_seed`,
`with_simulator`) that `deploy.sh` sets from `--seed` / `--simulator`:

- **`--simulator` implies `--seed`** — the simulator depends on seed-defined
  schemas (see Context #2).
- **`--highbyte` (HighByte) is independent of the data flags** — `ADTHighByteOpcuaStack`
  deploys the OPC UA → AWS IoT SiteWise layer on top of any data mode (it needs only
  `ADTNetworkStack`). `erp_database_stack`/`vf_database_stack` are optional: when
  `--simulator`/`--seed` are present the HighByte setup also wires the ERP/historian
  PostgreSQL connections; when absent those connections stay inactive (the stack and
  setup lambda guard on empty secret ARNs).
- **The flags are NOT sticky** (unlike the HighByte `/adt/highbyte-enabled` setting).
  Each deploy reflects exactly the flags passed. This is deliberate: a sticky
  "data on" flag would hide the fact that demo data is present, and the goal is
  an engine-first default.

### Base is demo-unaware (the contract)

The engine stacks — `ADTNetworkStack`, `ADTDatabaseStack` (Neptune + DynamoDB only),
`ADTAgentCoreStack`, `ADTComputeStack` — **must not instantiate, reference, or depend
on any seed/simulator infrastructure.** Demo data lives entirely in opt-in stacks:

- **`ADTSeedDataStack`** (new) holds the `adt_demo` RDS + the
  `lambda-vf-seed` loader. Instantiated only when `with_seed`.
- `ADTERPDatabaseStack` / `ADTERPLambdaStack` / `ADTERPDashboardStack` hold the live
  simulator. Instantiated only when `with_simulator`.

`ADTDatabaseStack` was **stripped to engine datastores only** (Neptune graph +
the self-learning DynamoDB table) — the RDS that used to live there moved to
`ADTSeedDataStack`. So in base, no engine stack mentions demo data.

**The one remaining compile-time seam:** `ADTComputeStack` is a single app image
used in all modes, so it still *optionally* injects `POSTGRES_*` env from
`ADTSeedDataStack` (and `ERP_POSTGRES_*` from `ADTERPDatabaseStack`) **when those
stacks exist** — `seed_data_stack=None`/`erp_database_stack=None` in base means
the app runs with **no** source-DB env at all. This deploy-time injection is the
*only* place the compute stack "knows" about demo data, and it exists solely
because source connections are wired at deploy time rather than discovered at
runtime. Collapsing it — so every source (demo or real) is registered through
runtime onboarding and the compute task injects **zero** source config — is the
job of the planned **connection-logic overhaul** (Layer 2). This ADR delivers
**Layer 1**: the structural separation. **Layer 2 is decided in
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md):**
connection reads become records-first (the persisted `System` node wins; this
env injection is demoted to a fallback), and the demo schema→DB routing
knowledge collapses into `agents/demo/db_routing.py` with an env→SSM-pointer
bootstrap (`/adt/demo/*`). The physical **CDK strip** of this seam
(the constructor params, env blocks, ECS `secrets=`, and the param-gated IAM
grants) is **explicitly deferred to a later deploy cycle** under ADR-0044's
prove-then-remove two-deploy ordering — the records/SSM replacement must be
proven on a deployed environment before the env source is removed.

## Consequences

- **Not default-safe for existing environments.** Because `base` is the new
  default, an environment that was deployed with demo data and is then deployed
  **without** the flags will have its ERP simulator stacks (incl. the Aurora
  database and its data) **removed** by CDK. Operators of a full-demo environment
  must pass `--simulator` (or `--seed`) on every deploy. This caveat is called
  out in `deploy.sh` output and [`docs/deployment.md`](../deployment.md).
- **Bring-your-own-data is now first-class.** A base deployment is a clean engine;
  connecting real systems is the runtime onboarding flow (the graph holds
  metadata, never data — consistent with the "rules engine, not a data store"
  principle).
- **Connector types are still limited** to PostgreSQL/Aurora, MCP, and SiteWise
  (i3X). Supporting genuinely new *types* of systems is a planned extension of
  the onboarding/registration flow — out of scope for this ADR.
- **Stack move in existing seed/full environments.** Because the
  `adt_demo` RDS moved out of `ADTDatabaseStack` into the new
  `ADTSeedDataStack`, the next deploy of an existing seed/full environment will
  delete the RDS from `ADTDatabaseStack` and recreate it in `ADTSeedDataStack`.
  The data is static seed (re-loaded from SQL) plus regenerated historian
  telemetry, so there is no durable data loss — but it is a replace, not an
  in-place move. The `ADTDatabaseStack` *name* is kept so the Neptune graph is
  not recreated.
- Verified via `cdk ls` (base = 4 engine stacks, seed = 5 with `ADTSeedDataStack`,
  full = 8) and `cdk synth`: base `ADTDatabaseStack` has **0** RDS (Neptune +
  DynamoDB present); base `ADTComputeStack` has **0** `POSTGRES_*`;
  `ADTSeedDataStack` carries the RDS + `vf-seed-data` loader; seed/full compute
  wires `POSTGRES_HOST` from it.
