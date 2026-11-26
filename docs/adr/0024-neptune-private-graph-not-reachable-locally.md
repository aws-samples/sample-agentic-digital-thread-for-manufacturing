<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# A private Neptune Analytics graph is not reachable from a local machine

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


---
Status: accepted
---

ADR 0001 assumed the local app could call the real Neptune Analytics graph over
the `neptune-graph` `ExecuteQuery` data-plane API the same way the deployed app
does. Live verification disproved this. The CDK creates the graph with
`public_connectivity=False` (stacks/vf_database_stack.py), so its data-plane
endpoint (`https://<graph-id>.<region>.neptune-graph.amazonaws.com`) resolves
and connects **only from inside the VPC**. From a laptop every call fails with
`Could not connect to the endpoint URL`. The deployed app works because the ECS
task runs in private subnets; a local process does not.

We are **not** making the dev graph publicly reachable: deployed environments
keep the graph VPC-private with no internet ingress by design. So in the hybrid
local stack, **Neptune-backed features are degraded locally by design** (the
calls fail soft and log warnings) and are exercised a different way.

## Consequences

- **Works fully locally:** the web UI/API, PostgreSQL-backed queries, Amazon
  Bedrock model reasoning, the in-process orchestrator, and the `/api/ask` chat
  path. Verified end-to-end.
- **Does NOT work from a pure-local machine:** anything that reads/writes the
  graph — system **onboarding provisioning**, identity-rule resolution, and the
  knowledge-graph analytics endpoints. These fail soft locally.
- **To work on graph features**, in priority order:
  1. **Test against a deployed environment** — zero new code; exact fidelity.
  2. **Run the app from inside the VPC** (in-VPC EC2/Cloud9 with VS Code
     Remote-SSH, or an SSM port-forward to a private graph endpoint) — the
     preferred way to *iterate* on graph code: exact Neptune fidelity, reuses the
     real boto3 path, **zero code divergence**.
  3. **Local openCypher substitute** (Memgraph/Neo4j behind a `GRAPH_BACKEND`
     switch in `db.py`) — only if a concrete **offline / zero-cost** need arises.
- `scripts/dev_neptune.sh` still provisions a real graph, but it is only useful
  to a process running inside the VPC — not to a laptop.

## Decision: build a local openCypher substitute (Memgraph) behind a flag

Initial framing treated graph features as a minority of local-testing value and
deferred the substitute. That premise was wrong: the knowledge-graph onboarding
flow is the **majority** of what is worth testing in this app, so a local graph
is what makes local dev actually valuable — not a nice-to-have.

A dialect spike (Memgraph over Bolt) replayed 20 representative statements
covering the app's real patterns — `MERGE … ON CREATE
SET`, the orphan-cleanup pattern predicate `WHERE NOT (c)<-[:MAPS_TO]-()`,
`count(DISTINCT)`, `OPTIONAL MATCH`, `… IN $list`, multi-pattern `MATCH`, chained
`DETACH DELETE` — with **20/20 PASS, 0 FAIL**. Dialect drift for this app's query
shapes is therefore near-zero.

**Decision:** build the substitute as a **velocity inner loop** with real
Neptune as the **fidelity gate**:
- A `GRAPH_BACKEND=neptune|local` switch in `agents/db.py`; `local` routes
  `neptune_query`/`neptune_write` to Memgraph over Bolt and maps results into the
  existing `{results: [...]}` / list-of-dict shape.
- Consolidate the ~3 direct `boto3.client("neptune-graph")` callers
  (`orchestrator.py` tools, `agents_routes.py`) to go through `db.py` so there is
  one switch point.
- Add a Memgraph service to `docker-compose.local.yml`; seed core concepts on
  startup as today.

**Caveats / fidelity gate:** the spike covered representative statements, not all
call sites; Neptune-specific functions or semantic edge cases (null ordering,
aggregation specifics) in the long tail are still validated against **real
Neptune** (a deploy or in-VPC) before merge. Local green ≠ final sign-off for
graph-semantics-sensitive changes.

## Implementation status: built and verified

Implemented behind `GRAPH_BACKEND` in `agents/db.py` (helpers `_graph_backend`,
`_memgraph_run`, `_neptune_execute`; `_to_plain` maps Bolt nodes to the Neptune
`{~id,~labels,~properties}` shape). The ~direct callers were consolidated onto
`db.neptune_query`/`neptune_write` (orchestrator tools, both server.py graph-viz
blocks); only the control-plane `get_graph` keeps its own client.

Verified locally (GRAPH_BACKEND=local, Memgraph in `docker-compose.local.yml`):
- App boot seeded 51 core concepts into Memgraph.
- Real onboarding of `mes-det` via `scripts/onboarding_smoketest.py` wrote System(1),
  SchemaEntry(2), Field(26), MAPS_TO(26); `/api/thread/analytics/*` read them back.
- `scripts/graph_parity_check.py` (DRY/in-sync guard) passes 15/15 query shapes.

**Fidelity gate unchanged:** node-detail click-through (`id(n)=$nid`) differs
between engines and is a known local gap; graph-semantics-sensitive changes are
still validated against real Neptune before merge.

**Local seeding (resolved):** the catalog points `erp`/`mes-mil`/`mes-det`/`mes-cr`
at view-schemas (`erp_data`, `mes_milwaukee`, `mes_detroit`, `mes_cedarrapids`)
that a deployment builds via `lambda-db-init` in the ERP database;
`cmms`/`plm` use `lambda-vf-seed` schemas. `scripts/seed_local_db.py` runs the
`lambda-db-init` create/seed logic against the single local PostgreSQL (and
`drop_all_tables` clears the stale raw schemas `lambda-vf-seed` left), so all six
SQL systems onboard locally. `scripts/local_dev.sh up` auto-runs it on first
start. Verified: `erp` (25 view-tables → 229 mappings), `mes-mil`/`mes-cr`
(6 views, cross-system identity rules), `cmms`, `plm`, `mes-det` all onboard.
