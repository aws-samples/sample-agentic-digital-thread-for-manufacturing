<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Onboarding is the single source of truth for a system's connection

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


---
Status: accepted
---

Today a manufacturing system's connection is fixed at two **snapshot** points: a
CDK `value_from_lookup` bakes URLs/tokens into the ECS task-def env at deploy
time, and `catalog.py` reads SSM/Secrets Manager once into `os.environ` at
import. Changing a URL or rotating a token therefore requires a redeploy or at
least an app restart, and the two snapshots can drift from what onboarding
actually recorded. We make the **onboarding record the single source of truth**
and resolve a system's connection **dynamically at connect-time**.

## Decision

Onboarding writes a system's connection to two places, split by sensitivity:

- **Non-secret config** (`url`/`host`/`port`/`scheme`/`base_url`) is stored on
  the Neptune `System` node as `connection_config`. This is still *metadata* —
  it reaffirms the project rule that **the graph is a rules/metadata engine, not
  a data store** (see [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)):
  the node holds *how to reach* a system, never production data and never a
  credential value.
- **Secrets** (token/password/credentials) live only in AWS Secrets Manager at
  `adt/conn/<system_id>`, and the node carries the secret's **ARN**
  (`secret_arn`) — never the secret value.

A shared foundation module, `agents/connection_resolver.py`, owns
`resolve_connection(system_id)`: it reads the node's non-secret config, fetches
the secret by ARN, and merges them into a resolved connection dict. The secret
fetch is **TTL-cached** (default 45s, `CONN_SECRET_TTL_SEC`, target 30–60s) so a
rotated token is picked up within the window without a restart, while steady-state
load does not hammer Secrets Manager. `invalidate_secret(arn)` forces freshness
right after an update.

### Reconciliation with existing secret conventions

Two secret naming schemes already exist: `adt/agent-config/<id>`
(per-system config the cloud agent runtime persists) and
`adt/highbyte-mcp-token`. The new `adt/conn/<system_id>`
is **canonical** going forward. **(Updated in Step 3.)** During Steps 1–2 the
resolver read the old names for backward compatibility. As of Step 3 the legacy
bridge has been **dropped**: there is no `highbyte-mcp-token` dual-write and no
fallback reader of the old names, so `conn/*` is the single canonical store with
no dual-write. Systems are (re)registered through the onboarding API, which
writes the `System` node (and the `conn/<id>` secret) directly — there is no
separate migration step. We do not write the same credential to two names.

### Local-mode behavior

There is no Neptune locally. Under `GRAPH_BACKEND=local` the same
`neptune_query`/`neptune_write` code path routes to Memgraph (per
[ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)), so the node
config round-trips locally too — i.e. the node **is** reachable locally, just
empty until seeded. Secrets Manager *is* reachable locally with cloud
credentials (the same reasoning as Memory in
[ADR-0027](0027-local-mode-uses-cloud-agentcore-memory.md)). Because the node is
the sole source (see *Step 3* below), a fresh/empty local Memgraph simply has no
registered systems until the **seed/registration step** is run — the same action
a new tenant takes — and `resolve_connection` never raises on the read path. The
earlier proposal's "fall back to the catalog/env snapshot" is **superseded by the
Step 3 decision** (no catalog fallback); `CONN_SOURCE` remains only as a
transitional cut-over valve.

### Incremental rollout (fallback, then flip)

The work ships in three independent steps on top of the foundation module, which
**deliberately falls back to the existing catalog/env snapshot** so the source of
truth does not move until the final step:

1. **Test Connection** — an admin endpoint resolves and probes a system
   (PostgreSQL / REST / MCP / SiteWise), returning pass/fail + latency. Pure
   addition; no source-of-truth change.
2. **Update + live reconnect** — an admin endpoint validates the submitted
   config first, writes the `conn/<id>` secret + non-secret config, then
   live-reconnects the system agent (drop cached MCP client, re-resolve, restart)
   with **no app restart**.
3. **Flip the source of truth** — make `resolve_connection` node-authoritative
   behind `CONN_SOURCE=node`, route all read sites through it, retire the CDK
   `value_from_lookup` task-def env injection and the `catalog.py` `os.environ`
   caching, and (re)register already-onboarded systems through the onboarding
   API, which writes the node directly.

### Security posture

- Secrets live **only** in Secrets Manager and are referenced by ARN; secret
  values never touch the node, responses, or logs.
- A canonical `mask_connection` helper redacts `token`/`password`/`secret`/
  `api_key` and rewrites `user:pass@` in URLs to `***@` in every response and log
  line.
- An **SSRF guard** validates targets before any probe: a scheme allowlist
  (`https`, `opc.tcp`, `postgres`/`postgresql`, `iotsitewise`), blocking the
  cloud metadata IP / link-local range, but **deliberately not blocking RFC1918**
  — the real targets are private in-VPC hosts and must remain reachable.
  Every credentialed connector request repeats the check just before it
  connects and is sent to the validated address with the original Host header
  and TLS SNI, never following a redirect (`agents/pinned_http.py`; note
  2026-10-12).
- Admin authorization reuses the existing Cognito **Admin** group check
  (`_check_admin`); IAM is least-privilege, scoped to `adt/conn/*`.
- Every test/update is audit-logged (action, system_id, actor, outcome) with no
  secret material.

## Step 3 — final decisions

Step 3 flips the source of truth from the two preload snapshots to the
`System` node and settles the open questions the proposal left to the cut-over.
The decisions below are now **accepted**.

**Legacy drop — no backward-compat readers.** The incremental rollout above kept
the old secret names readable during Steps 1–2; Step 3 removes that bridge
entirely. The `highbyte-mcp-token` dual-write and every fallback reader of the
old names are deleted, so `conn/*` is the only canonical store. The earlier
"reads the old names for backward compatibility" note applied to Steps 1–2 only
and no longer holds.

**Registration is uniform — demo systems get no special treatment.** There is
exactly **one** registration mechanism (discover → map → resolve → provision →
node write); the only difference between a demo system and a customer's system is
**where the inputs come from** — a seed manifest versus a human in the UI. The
seed/demo step is an **automated user**: `scripts/onboarding_smoketest.py` drives
the same REST onboarding API a person would, **supplying each demo system's
connection (URL/host + secret) as input** (derived from deploy env: the sim-DB
params, the HighByte URL/token). It does **not** rely on a baked-in catalog of
demo connections. `catalog._INITIAL_SYSTEMS` is demoted to a **seed manifest**
(which demo systems exist + how to derive their connection from env), consumed
**only by the seed step** and read by **nothing at runtime**. There is no
separate seed writer and no demo-only branch anywhere.

**SQL credential handling (Option 3) — a server-determined `secret_arn`
pointer.** The node stores a `secret_arn` **pointer** and `resolve_connection`
**always** fetches whatever that ARN points at — there is **no SQL special-case
branch**. For the shared-sim SQL systems the node's `secret_arn` points at the
**existing shared DB secret** the app already reads, so **no `conn/<id>` copy of
the password is minted**. For MCP/REST and any real external system the
`secret_arn` points at a dedicated `adt/conn/<id>` secret that
`write_connection` creates. The node itself holds only credential-free
`host`/`port`/`dbname`/`schema` plus the ARN. **Security guardrail:** the
`secret_arn` is **always determined server-side** — the known shared DB-secret
ARN for sim SQL systems, or the `conn/<id>` ARN minted by `write_connection` —
and is **never accepted from a request body**. Admins submit a secret *value*
(a token or password), never an ARN, so an admin cannot point the node at an
arbitrary secret the task role can read. This is **better for IAM** than
per-system SQL secrets: the SQL path reuses the existing DB-secret read grant,
mints zero new secrets, and avoids duplicating the password.

**`mask_connection` masks `base_url`.** The canonical masking helper now redacts
`base_url` as well (`user:pass@` rewritten to `***@`, secret query params
redacted), closing the gap where only `url`/`connection_string` were masked.

**`CONN_SOURCE` — the node is the sole runtime source; no catalog fallback.**
The runtime reads the connection **only** from the `System` node. There is **no
fall-back to a baked-in catalog snapshot**: falling back to a stale snapshot is
exactly the failure this ADR exists to eliminate (the HighByte stale-connection
scare), so an empty/unconfigured node means "not registered yet" — the honest
state — rather than a silent stale default. A fresh environment (including local
Memgraph, which is ephemeral) is populated by **running the seed/registration
step**, the same action a new tenant takes to onboard its first system; this is
already the documented local workflow (re-run the onboarding harness after a
graph reset). The only "fallback" permitted is a **live env read at
registration-time** to bootstrap the very first `discover` (before the node
exists) — never a persisted in-memory snapshot. `CONN_SOURCE` is retained only as
a transitional safety valve during the cut-over; the end state is node-only.

## Consequences

- **Positive:** operators can retest and update a system's connection from the
  admin UI and have it take effect live — no redeploy, no restart. A changed URL
  or rotated token is picked up within the TTL window. There is one resolve path
  and one masking helper, reducing drift.
- **Negative / risks:** cloud (gateway) and local reconnect paths diverge —
  gateway mode does not cache per-system MCP clients, so reconnect branches on
  mode and both paths need explicit tests. Legacy secret naming conventions
  (`agent-config/<id>`, `highbyte-mcp-token`) may still hold stale values from
  before the cut-over, but nothing reads them: `conn/*` on the `System` node is
  the only store consulted at runtime. The SSRF guard must be tuned
  carefully to avoid over-blocking legitimate private targets.
