<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# SiteWise event/metric models via richer same-system discovery

---
Status: accepted
---

> Implementation status: **in progress** — concepts, the shared reader seam,
> richer discovery, Mode A/B property read, concept-mapping validation, and the
> determinism flywheel are implemented. Deployed re-onboard and IAM confirmation
> follow. Pairs with
> [ADR-0035](0035-system-agents-own-their-query.md) and
> [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md).

## Context

The generic `SiteWise (Direct)` connector already reaches AWS IoT SiteWise via
boto3 (list models/assets, describe asset, get/history property values — Mode A).
But onboarding **discovery is blind to asset-model properties**: the SiteWise
branch of `onboard_discover` enumerated only asset *names* into one
`asset_hierarchy` pseudo-table. Event/metric properties (a state transition's
duration/from-state/to-state, a produced-count or waste rate, a machine speed, an
event timestamp, an equipment identifier) therefore never became `Field` nodes and
never got concept-mapped. With no `SchemaEntry → Field → MAPS_TO → Concept` for
those properties, the orchestrator has **no graph knowledge** to route or compose
OT event/metric questions, even though the read capability exists.

Three smaller gaps compounded it:

- The canonical concept vocabulary lacked generic OT concepts for **machine state**,
  **throughput**, and **machine speed**, so even a correct discovery would have no
  target concept to map produced-count / state / speed properties to.
- SiteWise SDK reads were duplicated between the Mode A `@tool` factory and the
  onboarding discover branch (two copies of list/describe/history), inviting drift.
- Mode B (orchestrator-owned query) returned a flat "run_query is SQL-only" string
  for SiteWise, so a Mode B deployment could enumerate nothing and read nothing.

## Decision

**Extend the existing generic `SiteWise (Direct)` connector with richer,
property-level discovery — do NOT onboard a second system.**

- **Same-system richer discovery.** Discovery enumerates ALL asset models and their
  properties generically via `describe_asset_model`, emitting **one pseudo-table per
  asset model** whose columns are that model's properties. The existing
  onboarding pipeline (discover → map → resolve → provision) and the existing graph
  shape (`System-[:HAS_SCHEMA]->SchemaEntry-[:HAS_FIELD]->Field-[:MAPS_TO]->Concept`)
  are reused unchanged; the map step assigns canonical concepts and provision writes
  the Fields/edges. The backward-compatible `asset_hierarchy` pseudo-table is kept so
  existing hierarchy resolution / value-mappings keep working. **No model-name
  literals** — the connector maps whatever models exist at runtime.
- **A single shared reader seam** (`agents/sitewise_read.py`) is the DRY entry point
  for every SiteWise SDK read (`list_asset_models_with_properties`, `describe_asset`,
  `get_property_history`/`get_property_value`), built through
  `aws_session.boto3_client_for` so cross-account `role_arn` assumption is honored.
  It is reused by discovery, the Mode A tool shims, and the Mode B property-read path.
- **Three new generic concepts** — `machine-state`, `throughput`, `machine-speed` —
  are added as dataset-agnostic definitions (field synonyms only). Equipment
  identifiers (`machineId`/`source_id`/`device_id`) map to the existing
  `equipment-id` concept so cross-system joins and accepted value-mappings work; a
  state-transition reason/fault code maps to the existing `failure-mode` concept; a
  stopped-state duration maps to `downtime`; an event timestamp to `event-time`.
- **Mode B gets a full structured property-read verb** (not enumerate-only): its
  `run_query` SiteWise branch accepts a structured JSON read request
  (`{op:"property_history"|"property_value", ...}`) dispatched through the same shared
  reader, and returns a coherent guidance message (never SQL, never a crash) for a
  non-structured input.

### Rules-engine principle (unchanged)

Event/metric property **VALUES are read LIVE at query time and are NEVER persisted**
to the graph. Only schema/concept **METADATA** persists (the model→property Fields
and their concept mappings). Discovery may gather a few sample values transiently to
give the map prompt evidence, but those samples ride only in the discover payload →
map prompt and are never written to a `Field`/`SchemaEntry` — consistent with the
existing provision-sample discipline. The graph stays a rules/metadata engine, not a
time-series store.

### Determinism flywheel (and how it is measured)

Two existing mechanisms are extended to SiteWise so repeated OT questions get more
deterministic over successive runs:

1. **Accepted value-mappings surfaced into TRANSLATE.** A machineId↔site/cell↔asset
   UUID mapping accepted into the graph (`ValueMapping`/`IdentityRule`/`Asset`
   `source_id`) is resolved by the orchestrator's TRANSLATE step *before* delegation,
   so a segment's machine id is turned into a SiteWise asset UUID up front rather than
   re-derived each turn.
2. **Corrections captured as `query_guidance`.** The existing post-turn LEARN loop
   promotes a recurring SiteWise correction (e.g. "property X on model M is the
   downtime measure", "translate machineId via ValueMapping before describe_asset")
   into the system's persisted `query_guidance` via `append_query_guidance_note`
   (idempotent, bounded). That guidance is read back at route time (orchestrator) and
   into the Mode A system prompt, closing the loop. Note content is runtime-derived —
   no dataset literals.

**Measurement.** `scripts/smoke_prompts.py --report` emits a scorecard with
`deterministic_pct` and an error taxonomy (incl. `RuntimeUnavailable` and
fabrication-fallback). Across ≥2 successive runs (allowing the flywheel to
accumulate), `deterministic_pct` should trend **up** and the repeat-error rate
**down** for OT cross-system questions.

## Considered options

- **Onboard a second SiteWise system for event/metric models — rejected.** One
  physical source should be one graph identity + one runtime. Value-mappings and
  IdentityRules already key off machineId↔site/cell↔asset; a second system would fork
  that identity, double routing, and split the accepted mappings across two systems.
- **Persist sampled time-series into the graph for faster answers — rejected.**
  Violates the rules-engine principle and would make the graph a stale data cache.
- **Mode B enumerate-only (no structured read verb) — rejected.** Leaves Mode B
  unable to answer OT questions; the shared reader makes a full structured read verb
  cheap and DRY.

## Consequences

- Onboarding a SiteWise system now yields per-model property Fields with concept
  mappings, so the orchestrator can route and compose OT event/metric questions.
- SiteWise SDK reads live in one module; Mode A behavior is preserved (thin shims)
  and SQL systems (Athena/PostgreSQL) are untouched.
- Mode B can enumerate model properties and perform structured live reads.
- Cross-account IAM must permit the property read actions (incl.
  `BatchGetAssetPropertyValueHistory`) — confirm against a deployed environment
  during re-onboard.
