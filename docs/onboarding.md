<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Onboarding deep-dive: from raw system to answered questions

> Terminology follows [`glossary.md`](glossary.md). For where onboarding sits
> in the overall design, see [`architecture.md`](architecture.md).

Onboarding is how a raw source system (a database, an Amazon Athena data lake, an
AWS IoT SiteWise / i3X source) becomes something the orchestrator can answer
questions about. The pipeline discovers the system's schema, maps its fields
to canonical manufacturing concepts, proposes identity rules for translating
identifiers to and from other systems, and provisions the graph metadata plus
a dynamic system agent. Everything it writes to the graph is **metadata**—
the knowledge graph is a rules engine, not a data store
([`architecture.md`](architecture.md#what-the-knowledge-graph-stores)).

All endpoints below live in `agents/onboard.py` (mounted under
`/api/onboard`) unless noted; the guided-flow endpoints live in
`agents/context_interview/routes.py` (mounted under `/api/onboard/guided`,
inheriting the same onboarding access gate, `require_onboard_access`).

## The pipeline at a glance

```
connect → [preflight] → discover → [pre-map probe] → map → resolve → provision → verify
```

The deterministic wizard (`frontend/src/views/onboard/wizard.ts`) drives
exactly this sequence—discover → map (polled job) → resolve (polled job) →
provision (polled job) → verify—gated by steward confirm checkpoints, with
every step wrapped in a retry affordance (`step-retry.ts`) so a failed step
renders visibly instead of dead-ending the flow.

The long-running phases (map, resolve, provision, preflight) are
**asynchronous jobs**: the POST spawns a background worker and returns a
`job_id`; the client polls `…/status/{job_id}`. This is deliberate—the
Amazon CloudFront origin cap is 120 s, so no onboarding phase may be a blocking POST
(`agents/context_interview/routes.py` module docstring;
[ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md) decision (a)).

| Endpoint | Phase |
|----------|-------|
| `GET /api/onboard/catalog` | List registered systems (onboarded and not). |
| `POST /api/onboard/connect` | Register a connection. |
| `POST /api/onboard/preflight` (202) + `GET /preflight/{job_id}` | Pre-discover diagnostic: connection probe + bounded per-table sample reads. |
| `POST /api/onboard/discover` | AI-powered schema discovery; persists server-owned physical provenance. |
| `POST /api/onboard/guided/probe/{system_id}` | Pre-map probe: per-system day-0 questions, between discover and map. |
| `POST /api/onboard/map` + `GET /map/status/{job_id}` | AI concept mapping (background job). |
| `POST /api/onboard/resolve` + `GET /resolve/status/{job_id}` | AI identity-rule proposals (background job). |
| `POST /api/onboard/provision` + `GET /provision/status/{job_id}` | Write graph metadata and create the dynamic system agent. `provision/preview` returns a pure metadata-only plan; `provision/confirm` executes it. |
| `POST /api/onboard/verify` | Post-provision verification, by connection type (answers `system_id`, `verified`, `check`, `rows_counted`, `tables_accessible`). PostgreSQL needs its schema and is checked in the database (`check: database`: tables listed, `total_rows` totalled). Every other type has no schema to read and is checked in the knowledge graph (`check: knowledge_graph`: the System node reads onboarded, with the entities and `fields` provision wrote; `rows_counted: false` and no `total_rows`). |
| `POST /api/onboard/verify/agent-test` + `GET /verify/agent-test/{job_id}` | The agent test, run after verify by the wizard and the chat's verify card: checks the system's runtime and gateway target, then calls its own query tool through the gateway with a row count of one of its tables. The result names the step that failed (`runtime`, `gateway_target`, `tool`); each wait is bounded by a 180 s deadline (`agents/onboard_verify_agent.py`). |
| `POST /api/onboard/reset` / `reset/{system_id}` | Global / per-system removal. |

### Registering a connection (`POST /api/onboard/connect`)

The Connect New System form and the onboarding chat's `connect_new_system`
tool share one implementation (`onboard_agent.connect_new_system`, body model
`models.ConnectRequest`). The body:

| Field | Meaning |
|-------|---------|
| `name` | Required. The system id is derived from it unless `system_id` (alias `id`) is given. |
| `connection_type` | `PostgreSQL` (default), `MCP Server`, `Athena`, `SiteWise (Direct)`, `SiteWise (i3X)` or `Tulip (Tables API)`. `REST API` is refused: a generic REST connector cannot be provisioned yet. |
| `connection_string` | The endpoint URL for URL-based types (MCP Server, i3X, Tulip). |
| `connection_config` | Type-specific settings (below). Secret keys are moved to the system's own AWS Secrets Manager secret `adt/conn/<id>` and never appear on the catalog entry, in the response or in `GET /catalog`. |
| `system_type`, `plant`, `schema`, `query_guidance`, `capabilities`, `tool_prefix` | Optional descriptive fields and guidance carried to the System node at provision. |

`connection_config` per type:

- **PostgreSQL**: `{"dsn": "postgresql://user:password@host:5432/db?options=-csearch_path%3D<schema>"}`.
  The full connection string goes only to `adt/conn/<id>`; the schema comes from
  its `search_path` (a different `schema` field is refused, and a schema already
  registered by another system is refused). Use a dedicated read-only login with
  CONNECT on the database, USAGE on that schema and SELECT on its tables, and
  nothing else: the system agent runs SQL its model writes, and that SQL can
  read whatever the login can read. A superuser, or a login that can create
  objects or change data in the schema, is refused: preflight reports it as
  blocked, the chat's connection test fails, provisioning stops before the
  system agent starts, and an Edit to such a login is refused, each naming
  the grants to use. A login whose privileges cannot be read is not refused,
  and onboarding (or the Edit) warns that it was not checked. The grants are in
  [Connecting your own data sources](deployment.md#connecting-your-own-data-sources).
- **MCP Server**: `{"token": "<token>"}`, optional. The server's URL goes in
  `connection_string`, once: a registration that also carries
  `connection_config.url` or `base_url` is refused unless every one of them is
  the same URL (same scheme, host, port and exact path and query: case and an
  explicit default port do not matter, a trailing slash does).
  The token goes only to
  `adt/conn/<id>`. It is sent as `Authorization: Bearer <token>`, or as entered
  when it already starts with `Bearer `. An `auth_scheme` in the registration
  body is not stored; to send the token another way (`bearer_literal` = the
  stored text as the whole `Authorization` value, `api_key` = an `X-API-Key`
  header), choose it under **Authorization header** in the system's Edit
  connection form (Bearer token / X-API-Key / Token as entered; it shows the
  scheme in use), or set `auth_scheme` with the admin
  `PUT /api/admin/connection/{system_id}` (body `{"auth_scheme": "..."}`, one of
  `bearer`, `api_key`, `bearer_literal`; a token rotation is `{"token": "..."}`).
- **Athena**: `{"workgroup", "database", "catalog"?, "region"?, "output_location"?,
  "role_arn", "external_id"?}`. `role_arn` is required on a deployed app (no app
  role can query Athena itself): deploy
  `stacks/cross-account/adt-data-readonly-role.yaml` in the data account with
  `TrustedAccountId` (the ADT account) set; `DataAccountId` defaults to the
  account you deploy the role in. Set `S3TablesAccountId` (and the other Amazon
  S3 Tables parameters) only when your tables are in Amazon S3 Tables, to the
  account that owns the S3 Tables catalog; left empty, the role gets no S3
  Tables access. For tables over a plain Amazon S3 bucket, set `DataBucketName`
  to that bucket's name instead of editing the role. Deploy the app with
  `--cross-account-role convention`. Before redeploying over an existing role
  stack, read [Updating an existing role stack](cross-account-onboarding.md#updating-an-existing-role-stack). `external_id` goes to the secret.
- **SiteWise (Direct)**: `{"region"?, "role_arn"?, "external_id"?}`.
- **SiteWise (i3X)**: `{"base_url", "token"?}`.
- **Tulip (Tables API)**: `{"base_url", "api_key", "api_secret",
  "table_label_prefix"?}`; the server encodes the HTTP Basic token into the
  secret. `table_label_prefix` is optional and non-secret: when set, the system
  is only the Tulip tables whose label starts with it (matched exactly,
  trailing space included), so a workspace shared with other sites' tables
  is discovered, listed and read as this system's tables alone, and a read of
  any other table is refused with a reason that names the prefix. Up to 128
  printable characters. A system that takes its credentials from a named
  Secrets Manager secret gets the prefix from that secret (for example a site
  prefix such as `"Assembly Line - "`; a secret without one carries none).

Every URL field a registration carries (`connection_string`, and the
`url` / `base_url` keys of `connection_config`) passes the connection-target
guard (`connection_security.validate_connection_target`) before anything is
stored; for an MCP Server they must also all be the same URL.
A success answers `{"status": "created", "system_id", "name", "message"}` plus
a `note` when credentials were stored; a refusal answers `{"error": "..."}`
and stores nothing.

### Preflight: blocked / degraded / ready

Before discover runs, the preflight
([ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md) decision (a))
extends the existing connection probe (`agents/connection_probes.py`) with
bounded, serial per-table sample reads (LIMIT 1 per table) and returns one of
three pinned verdicts:

- **`blocked`**—the connection/role probe failed. The UI refuses to start
  discover; the API refuses nothing—preflight is a *diagnostic* gate, not
  an authorization gate.
- **`degraded`**—connection OK but at least one table sample-read failed
  (the classic failure: an assumed role that can read the catalog but not the
  data). The UI warns with
  per-table detail; proceeding is allowed.
- **`ready`**—all probes and sample reads passed.

No sample *values* appear in the result—presence/error class only (the
rules-engine constraint), and the preflight never writes to the graph or the
catalog.

### Discover

`POST /api/onboard/discover` reads the source's schema (tables, columns,
types; i3X sources short-circuit to their own discovery path rather than the
generic OpenAPI parse) and **persists server-owned physical provenance**
(`agents/onboard.py`, `_onboard_discover_integrity` /
`_store_discovered_provenance`). That provenance is the trust anchor for the
later phases: `/map` and `/provision` validate the client-supplied tables and
mappings against it and return `400 schema_provenance_invalid` on mismatch
(`_trusted_map_tables`, `_trusted_provision_body`)—a client cannot smuggle
tables the server never discovered.

### The pre-map probe and the guided flow

Between discover and map the wizard pauses for the **pre-map probe**
(`frontend/src/views/onboard/probe-step.ts`): a deliberately **failure-open,
skippable** step—`/map` is never touched, and a client that never probes
behaves exactly as before. Probing is a separate client call rather than a
hook inside `/map` because `/map` is a polled background job; pausing it for
human input would put a person inside a polling contract and risk
half-mapped state (`agents/context_interview/routes.py` module docstring).

The probe is one of the guided-onboarding endpoints
(`agents/context_interview/routes.py`); the steward-facing core is:

| Endpoint | Purpose |
|----------|---------|
| `GET  /api/onboard/guided/state` | First-launch + draft + answered state. |
| `POST /api/onboard/guided/dismiss` | Steward declines the flow. |
| `GET  /api/onboard/guided/questions` | The deployment-scope question set. |
| `POST /api/onboard/guided/turn` | One deployment-scope question/answer. |
| `POST /api/onboard/guided/probe/{system_id}` | Per-system pre-map questions. |
| `POST /api/onboard/guided/confirm-context` | Write the drafted business context, batched. One confirm at a time, and never while `PUT /api/admin/business-context/{id}` is saving one of its systems: an overlapping save gets 409 and writes nothing. |

The guided flow has two stages (`agents/context_interview/__init__.py`): a
**day-0 deployment interview** and **per-system probing** inserted between
discover and map—the last moment at which map-time context is free rather
than costing a re-onboard.

### The day-0 questions

The question bank (`agents/context_interview/questions.py`) is the
declarative form of the intake questions established in
[`domain-context-intake.md`](domain-context-intake.md)—read that document
for the questions themselves and why each one matters; this section covers
only the mechanics. Two properties are pinned:

- **Deterministic selection, no LLM in the decision path.** Which questions
  are asked, and in what order, is computed from schema facts: a question is
  asked only when the schema gives it something to point at (*answerable*)
  and the answer can still change something (*material*), capped at
  `MAX_QUESTIONS_PER_SYSTEM = 4` and ordered map-time-first (map-time answers
  are the ones that get expensive after onboarding).
- **Nothing is written directly.** Every answer lands through
  `agents/context_interview/landing.py` as a **proposal** in the
  [ADR-0003](adr/0003-unified-proposal-lifecycle.md) lifecycle (or a
  business-context *draft* pending explicit confirm)—see
  [`self-learning.md`](self-learning.md) for the lifecycle.

The probe also **derives** what the schema already answers before asking
anything (`guided_probe` in `agents/context_interview/routes.py`), so a fully
automated API onboarding that never answers a question still produces real
coverage and query-strategy context.

### Map: concept mapping with evidence

`POST /api/onboard/map` runs a per-column semantic LLM pass proposing a
concept + role + confidence for each column. Two deterministic layers correct
and complete the LLM's output:

- **Evidence-based concept suggestion**
  ([ADR-0034](adr/0034-evidence-based-concept-suggestion-at-map-time.md),
  `agents/concept_suggest.py`): for columns the semantic pass leaves
  uncertain (`needs_review` or low confidence), the classifier samples the
  column's values and **measures overlap** against the value populations of
  concepts that peer systems already established. Strong, non-coincidental
  overlap (at least 2 distinct shared tokens and a size-normalized ratio
  ≥ 0.5) upgrades the column's proposed concept, basis-tagged
  `value_overlap`—measured evidence outranks a weak semantic guess, and no
  dataset-specific name synonym is ever encoded.
- **Concept↔system-type affinity**
  ([ADR-0007](adr/0007-concept-system-type-affinity.md), `agents/catalog.py`):
  `CONCEPT_AFFINITY` records which concepts a system *type* plausibly
  supplies (many-to-many) and drives the non-blocking `expected_unmapped`
  completeness hint at the mapping checkpoint ("an MES typically also
  provides downtime—not mapped"). It is kept deliberately distinct from
  `CONCEPT_AUTHORITY` (1:1), which answers the different question of *who
  wins* for a concept—and affinity is enforced at import time to be a
  superset of authority so the two cannot drift.

The mapping checkpoint in the wizard shows the proposed mappings as editable
rows; the confirmed rows—not the raw LLM output—are what proceed.

### Resolve: identity-rule proposals

`POST /api/onboard/resolve` proposes identity rules—how to translate
identifiers between this system and the ones already onboarded. It measures
value overlap between fields that share a concept across systems
(`agents/resolve_evidence.py`,
[ADR-0034](adr/0034-evidence-based-concept-suggestion-at-map-time.md)
context) and reads the entity + identity edges written by prior provisions
to pair the new system's identity fields with each existing system's
(`_entity_anchored_pairs` in `agents/onboard.py`,
[ADR-0040](adr/0040-dynamic-entity-and-identity-for-any-system.md) D4).
Cross-system join/crosswalk candidates discovered here are filed as
**proposals** through the unified lifecycle and surfaced as compact summaries
in the resolve checkpoint (`_join_candidate_summaries` reads them back via
`self_learning.get_pending_mappings()`).

### Provision: graph writes and crosswalk capture

`POST /api/onboard/provision` is the only phase that writes to the graph.
Writes are strict inside the provision transaction—a failed graph write
raises rather than proceeding silently (`neptune_write` wrapper in
`agents/onboard.py`). Provision persists the schema metadata (`SchemaEntry`
and `Field` nodes with `relation_kind` and `data_type` provenance), creates
the dynamic system agent, and captures intra-system structure at the moment
it is cheapest
([ADR-0041](adr/0041-onboarding-time-capture-same-row-and-intra-system.md)):

- **Same-row identifier sets (D1):** columns on one table that are
  interchangeable identifiers of the same record (same concept via this
  provision's mappings, or both single-column DDL-UNIQUE) become internal
  `JoinPath` nodes with `transform='same_row'`—so the agent never burns
  dozens of per-value proposals rediscovering that a serial number and an
  asset tag identify the same row.
- **Intra-system join discovery (D2):** `join_discovery.discover_intra_system`
  compares identifier-like fields across tables of the same system using the
  value-overlap transform ladder.
- **The direct-write rule (D3):** *deterministic evidence writes directly;
  weaker evidence proposes.* FK constraints, DDL UNIQUE constraints, same-row
  concept matches, and transform-ladder hits over the overlap and
  key-cardinality floors are machine-checkable facts—they MERGE graph nodes
  during provision with provenance (`method`, `overlap_count`,
  `overlap_ratio`). Crosswalk-grade candidates (name similarity, no verified
  transform) go through the [ADR-0003](adr/0003-unified-proposal-lifecycle.md)
  proposal queue for human review instead.

`provision/preview` returns a metadata-only plan without executing;
`provision/confirm` executes the validated plan.

## What lands in the graph—and what never does

Provision writes `System`, `SchemaEntry`, `Field`, `Concept` edges,
`IdentityRule` and `JoinPath` nodes, and authority/entity edges—**metadata
only**. No production rows, identifier values, or sensor readings are ever
persisted: the graph tells the agent *where* to look and *how* to translate,
and the data stays in the source systems
([`architecture.md`](architecture.md#what-it-never-stores)). The same
constraint shapes the preflight (presence/error class only, never sample
values) and schema-drift detection (below).

## Re-onboarding and schema drift

Schemas change after onboarding. Two mechanisms respond, with a pinned
division of labor
([ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)
decision (b); `agents/schema_drift.py`):

- **Drift detection files proposals—it never mutates the graph.** A daily
  scheduled job (`DRIFT_CHECK_INTERVAL_SECONDS`, default 86400, `0` disables)
  and an on-demand admin route (`POST /api/admin/learning/drift-check`, an
  async job) re-run discovery read-only and diff the fresh schema against the
  graph's onboarded baseline. Each finding (column added/removed/retyped,
  table added/removed) files ONE pending `schema_drift` proposal through
  `self_learning.log_proposed_mapping`—dedup applies, so the same
  unresolved drift never duplicates. Accepting a removal/retype row is an
  **acknowledge-only** accept: zero graph writes
  (`self_learning._accept_schema_drift`); accepting `column_added`/
  `table_added` MATERIALISES the column/table (validate → source re-read →
  MERGE → readback) and `POST /api/neptune/graph/repair-schema` re-applies
  already-accepted additive drift in bulk
  ([ADR-0077](adr/0077-additive-schema-repair-on-drift-accept.md)). Athena
  provision records `se.discovery_complete`/`se.discovered_columns` so a
  partial physical discovery is visible in `get_system_schema`. Skips are honest and per-system
  (`skipped:no-baseline`, `skipped:<error class>`)—never a fabricated empty
  diff.
- **Re-onboarding is the steward's explicit fix for destructive drift.** The real remedy for removed/retyped drift
  is to run the pipeline again—`scripts/reonboard.py` or the
  per-system reset + onboard flow. Drift detection exists to make the need
  visible, not to act on it.

## Where to go next

- [`domain-context-intake.md`](domain-context-intake.md)—the intake
  questions the guided flow asks, and why each one matters.
- [`self-learning.md`](self-learning.md)—the proposal lifecycle every
  onboarding-time discovery and interview answer lands in, and how the system
  keeps learning after onboarding.
- [`architecture.md`](architecture.md)—the rules-engine concept and the
  overall data flow.
