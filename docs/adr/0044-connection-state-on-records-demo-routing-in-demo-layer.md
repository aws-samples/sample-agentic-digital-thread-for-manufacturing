<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Connection state lives on persisted records; demo DB routing lives in the demo layer

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


---
Status: accepted — Decision 4 (the CDK strip) remains deferred; see
the maintainers' deferred-work list.

Related: completes [ADR-0030](0030-demo-data-is-opt-in-base-default.md) Layer 2
and [ADR-0031](0031-onboarding-driven-dynamic-connection-management.md).
---

## Context

[ADR-0030](0030-demo-data-is-opt-in-base-default.md) made demonstration data
opt-in and moved it into its own stacks, but named **one remaining compile-time
seam**: the engine compute stack (`VFComputeStack`) still receives the seed/ERP
database stacks at synth and conditionally injects a shared-DB-pointer env
(`POSTGRES_*`/`DB_SECRET_ARN`, `ERP_POSTGRES_*`/`ERP_DB_SECRET_ARN`) plus the
matching ECS `secrets=` credential block and param-gated task-role
`GetSecretValue` grants on the demo DB secrets; `HighByteOpcuaStack` likewise
receives the ERP/VF database stacks for its setup lambda; and `app.py` passes
the demo stacks **into** the engine constructors.
[ADR-0031](0031-onboarding-driven-dynamic-connection-management.md) made the
runtime connection **node-authoritative** (the Neptune `System` node is the sole
runtime source, with the shared-secret **ARN pointer** determined server-side —
Option 3) and established that **demo systems get no special treatment**: they
register through the same discover→map→resolve→provision API a human drives.

Two problems remained on the current tree:

1. **The schema→DB routing table is demo knowledge, quadruplicated in the
   engine.** The "`erp_*`/`mes_*`/`public` → ERP Aurora, everything else → the
   `adt_demo` RDS" rule existed in FOUR drifting copies:
   `catalog._pg_conn_str`, `db._get_conn_params_for_schema`,
   `connection_resolver.db_secret_arn_for_schema`, and
   `onboarding_smoketest._ERP_SCHEMA_PREFIXES`. This is exactly the kind of
   demo literal the project constraint forbids in core engine files: demo
   content must onboard through the same dynamic path as customer systems, with
   no special cases in engine code.
   The drift was not hypothetical — one copy's docstring described routing the
   code did not perform.

2. **Reads were env-first, not record-first.** Even after onboarding persisted
   a system's connection to the `System` node (per ADR-0031), several read
   sites — the hot `db.query_pg` path, the discover-time bootstrap, and client
   tooling — consulted the deploy-time env snapshot first or exclusively.
   `scripts/reonboard.py` derived DSNs from the **operator's local shell env**,
   which previously caused a defect: a re-onboard from a laptop wrote a
   `localhost` DSN into a deployed system's connection and 500'd the system
   until manually repaired. Env-first reads reproduce the stale-snapshot
   failure mode ADR-0031 exists to eliminate.

The engine already has the right idiom for a demo-shaped pointer resolved
dynamically: `catalog._get_highbyte_url` does a lazy env→SSM read (env first,
then SSM, no `os.environ` write-back, fail-soft) for the HighByte/SiteWise
connection. The demo DB pointer wants the same shape.

## Decision

### Decision 1 — Connection state lives on persisted records; every read is records-first

The persisted onboarding record is the source of truth for a system's
connection, exactly as ADR-0031 specified: the `System` node carries the
non-secret config (`host`/`port`/`dbname`/`schema`) plus the server-determined
`secret_arn` pointer (the shared demo-DB secret for sim SQL systems, a
`conn/<id>` secret for everything else), and the catalog entry mirrors it via
`set_connection_config`.

**All connection reads become records-first with the env bootstrap as
fallback.** Concretely: `db._get_conn_params_for_schema` first maps the schema
to a registered system and resolves its node (`resolve_connection` →
`pg_conn_params`, with a short TTL cache because `query_pg` is on hot dashboard
paths); only on a miss, an unconfigured node, or a fail-soft error does it fall
through to the env-derived params. This precedence is **honest**: a persisted
record wins, absence falls through, and nothing ever serves a stale in-memory
snapshot. An unconfigured node stays the honest "not registered yet" state per
ADR-0031 — the fallback is a live env read, never a persisted default.

### Decision 2 — ONE demo routing module: `agents/demo/db_routing.py`, bootstrapped env→SSM

All demo schema→DB routing knowledge collapses into a single module in the
sanctioned demo layer, `agents/demo/db_routing.py`:

- `ERP_SCHEMA_PREFIXES` / `is_erp_schema` — the single canonical copy of the
  routing tuple. The four engine-side copies are deleted; core agents files
  (`catalog.py`, `db.py`, `connection_resolver.py`, `onboard.py`) contain **no
  ERP-prefix tuple and no demo DB literals**. `connection_resolver.
  db_secret_arn_for_schema` keeps its public name but delegates to the demo
  module; `onboarding_smoketest.py` imports the shared tuple.
- `demo_conn_params_for_schema` / `demo_pg_dsn` / `demo_db_secret_arn` — the
  moved behavior of the deleted copies, reading **env first, then an SSM
  pointer** (`/adt/demo/seed-db`, `/adt/demo/erp-db`,
  JSON `{host,port,dbname,secret_arn}`), mirroring `catalog._get_highbyte_url`
  verbatim in shape: lazy boto3, no write-back, fail-soft.
- The module is one-way: it must not import engine modules (`agents.db`,
  `agents.catalog`), preserving the `agents/demo/` seam direction.

`deploy.sh` publishes the SSM pointers from the demo stacks' CloudFormation
outputs when `--seed`/`--simulator` are passed, and **deletes them on a
flagless deploy** (belt-and-suspenders against ADR-0030's non-sticky-flags
pitfall: a base deploy leaves no stale demo pointer behind). Only the secret
**ARN** is written to SSM — never a secret value. This pointer is a
**registration/connect-time bootstrap** — the ADR-0031 constraint-2 "live env
read at registration" capability, kept but moved demo-side — not a persisted
snapshot.

### Decision 3 — Client tooling reads persisted records; env is a loud demo-layer fallback

`scripts/reonboard.py` derives each system's connection from the **persisted
record**, read server-side via the admin `describe_connection` route (extended
to return the non-secret `host`/`port`/`dbname`/`search_path`/`secret_arn`),
fetching credentials via the operator's own AWS access to the referenced
secret. Only when the record is absent or unconfigured (e.g. a wiped node after
a graph reset — the incident case) does it fall back **per-system** to the
env-derived inputs, with a loud log line naming the system and the reason; an
underivable connection stays fail-loud. A `--from-env` flag forces the pure-env
path for bootstrap scenarios. The operator's laptop env can no longer silently
override a deployed system's persisted connection.

### Decision 4 — The CDK strip is deferred to a future change (prove-then-remove)

Removing the synth-time wiring — the `VFComputeStack` demo-stack constructor
params, the `POSTGRES_*`/`ERP_POSTGRES_*` env blocks, the ECS `secrets=`
credential block, the param-gated task-role `GetSecretValue` grants on the demo
DB secrets, the `HighByteOpcuaStack` demo kwargs, and the `app.py` demo-stack
arguments — is **explicitly out of scope for this deploy cycle** and lands in
the next one.

**Rationale (the two-deploy ordering guarantee):** this plan's code lands in a
single deploy, and nothing may remove a working path in the same deploy that
introduces its replacement. Additive-first cut-over requires proving the
records/SSM path **on a deployed environment** before removing the env source —
which takes two deploys by construction: this cycle's deploy proves the
replacement (re-onboarding all systems from persisted records, SSM pointer
values verified, no env fallback logged for surviving nodes); the next removes
the source. Two orderings are strict:

1. **Prove, then strip.** The env wiring stays until the deployed validation
   gate passes. Keeping it is harmless under Decision 1's precedence: records
   win wherever records exist.
2. **Overlay grant before IAM strip.** The records-first secret merge reads the
   shared demo-DB secret via the task role's `GetSecretValue` grants, which
   today are the param-gated engine-side statements. Removing them before the
   demo overlay attaches a Secrets Manager **resource policy** granting the
   engine task role access would AccessDeny the records path. The overlay
   policy lands first; the engine-side grants go second.

No document in this repository should claim the CDK strip lands this cycle.

### Project-constraint alignment and sanctioned exceptions

After this ADR, the one sanctioned home for demo DB knowledge in the app
package is `agents/demo/db_routing.py` (plus the existing seed manifest in
`agents/demo/systems.py`). Two exceptions to the project constraint are recorded:

- **UI demo/example buttons (sanctioned).** Prefill affordances in the web UI
  may reference demo/example content, but they must drive the **same flow a
  user would** — fill the form, call the same routes — never a parallel code
  path. This is a UI convenience, not an engine special case.
- **The curated concept map (a sanctioned exception, open for revisiting).** The
  [ADR-0021](0021-two-layer-concept-model.md) two-layer concept vocabulary
  remains as-is; revisiting it is explicitly not part of this decision.

## Considered alternatives

- **Keep the env-first reads and just deduplicate the routing tuple in place
  (engine-side constant).** Rejected: a shared engine constant still puts demo
  knowledge in core files (the project-constraint violation), and env-first reads keep
  the stale-snapshot failure mode ADR-0031 eliminated — the reonboard incident
  shows it bites in practice.
- **Skip the SSM pointer; have the overlay/harness push everything through the
  onboarding API (push-only).** The harness already supplies host + credential
  as input; but the `secret_arn` must be determined **server-side** (ADR-0031's
  security guardrail forbids accepting an ARN from a request body), so a
  server-side source is needed regardless. The env→SSM pointer read is the
  smallest change and is symmetric with the existing HighByte/SiteWise idiom.
- **Land the CDK strip in the same deploy ("while we're at it").** Rejected:
  it collapses prove-then-remove into one deploy, so a defect in the
  records/SSM path (or the missing overlay IAM policy) would break demo
  connectivity with no working fallback. The two-deploy ordering is the point.
- **Gate the env routing fallback on `ADT_DEMO`.** Deferred: local/simulator env
  parity for that gate is unverified, and env-presence gating is
  behavior-identical to today. Tightening later is additive.
- **`DemoStage`/`app_demo` composition split in `app.py`.** Unchanged from the
  v1 analysis: wrapping stacks in a CDK `Stage` churns logical IDs (resource
  replacement risk); the top-level data-mode conditional stays.

## Consequences

- **Positive:** one canonical routing source in the demo layer; core agents
  files carry no demo DB literals (a grep-gate unit test enforces it); every
  connection read honors the persisted record first, completing ADR-0031's
  node-authoritative contract end-to-end; reonboard can no longer poison a
  deployed system from a laptop env; a demo DB endpoint/secret rotation is
  picked up via the record (or the SSM pointer) without a redeploy.
- **Negative / risks:** the hot `query_pg` path gains a graph read (mitigated
  by a short TTL cache and fail-soft env parity); a stale or empty
  `/adt/demo/*` pointer yields an *unconfigured* demo node — the
  honest state, mitigated by deploy.sh's delete-on-base behavior and by
  verifying pointer **values** (not presence) post-deploy; the records-first
  path depends this cycle on the existing engine-side demo-secret IAM grants,
  which is exactly why Decision 4's ordering is strict.
- **Verification:** offline — parity unit suites for the moved routing
  behavior, a no-recursion test for the `pg_conn_params` ↔
  `_get_conn_params_for_schema` seam, and the grep gate; deployed — SSM pointer
  values, re-onboarding all six demo systems from persisted records (no env
  fallback logged for surviving nodes), and the standard grounding/e2e gates.
  The Neptune graph is VPC-private
  ([ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)), so graph
  behavior is verified against a deployed environment.
