<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Deterministic metric resolver: one `resolve_metric` over an explicit QueryPlan, with graph-sourced runtime-settable authority

---
Status: accepted
---

This change builds the **resolver** (ADR 0001) — the single deterministic path that turns a Metric
into a value, used identically by the dashboard render and by agents-as-tools. The design review
fixed the resolver's shape against the existing code and ADRs. Key grounding: the existing
`agents/resolve.py` is **cross-system identity resolution** (`resolve_entity`: identifier →
unified record), a *different* concern; the closest metric→value prototype is the F9
integration test, which reads a `MAPS_TO` mapping from the graph and composes a `SELECT`.
The building blocks already exist (`agents.db.neptune_query`/`query_pg`/`query_system`/
`validate_identifier`; the `System-HAS_SCHEMA->SchemaEntry-HAS_FIELD->Field-MAPS_TO->Concept`
schema; `AuthorityRule`). Refs ADR 0001 (resolver = values, no LLM), 0002 (graph =
relationships incl. authority; metric layer = formulas), 0004 (lineage), 0008 (config seam),
0011 (canonical graph; F1), 0013 + its amendment (tracer = `mro-inventory`), 0014 (metric layer).

## Decision A — A new `agents/metric_resolver.py`, separate from `resolve.py`

Metric→value resolution is distinct from entity identity resolution and gets its own module.
The core is a pure function `resolve_metric(metric_id) -> ResolvedMetric`; a thin FastAPI route
in `server.py` wraps it. The dashboard and agent-as-tool both call the **same** function,
guaranteeing byte-identical values (ADR 0001). The resolver is the source of truth for *values*.

## Decision B — An explicit `QueryPlan` intermediate

`resolve_metric` produces an immutable `QueryPlan` (`{system_id, schema, table, column,
aggregation}`) *before* touching a database. This separates **graph-resolution** (Metric +
graph → plan; unit-testable by mocking `neptune_query`) from **execution** (plan → SQL → value;
needs a DB). The plan is also the natural seam for lineage capture (ADR 0004).

## Decision C — Authority is read through an interface, sourced from the graph, settable at runtime

The authoritative system for a concept is read via an interface whose default implementation
reads the graph `AuthorityRule` (seeded at onboarding from `catalog.CONCEPT_AUTHORITY`), per
ADR 0002. A minimal **`set_authority(concept, system_id)`** writer (one `AuthorityRule` upsert),
exposed as a small internal route, makes authority **runtime-settable**. This is the ADR-0008
seam *with teeth*: programmatic read+set are in; the admin **page/UX/persistence-of-config** is
the part ADR 0008 still defers. Authority is a per-deployment fact, not a universal constant.

- *Boundary (the noted ADR-0008 tension):* interface + `set_authority` = built now; admin UI =
  deferred. Recorded here so the sliver of "configurability" is intentional, not scope creep.

## Decision D — Fail loud; never guess

Resolution order for a direct Metric's concept: use the `AuthorityRule` if present; else if
**exactly one** onboarded system maps the concept (`MAPS_TO`), use it (unambiguous, not a
guess); else **raise** with an actionable message naming `set_authority` as the remedy. No
silent fallback (consistent with ADR-0011 F5 and the standing no-fallback principle).

## Decision E — Direct metrics only in v1; derived deferred to a later change

`resolve_metric` resolves **direct** Metrics now: authority → `MAPS_TO` → table.column →
`SELECT <AGG>(<column>) FROM <schema>.<table>` (identifiers via `validate_identifier`;
execution routed by `connection_type` — `query_pg` for PostgreSQL, else `query_system`). A
**derived** Metric raises `NotImplementedError` until This change brings the op-tree evaluator online.
`latest` aggregation also defers (needs a time/ordering dimension we don't model yet) —
**resolved in [ADR-0029](0029-dimensional-resolution-and-render-hints.md)**: `LATEST` now
resolves via `DISTINCT ON` ordered by the graph-modeled `event-time` concept.

## Consequences

- New `agents/metric_resolver.py` (`resolve_metric`, `QueryPlan`, `ResolvedMetric`, the authority
  interface + graph-default impl + `set_authority`) and a thin `server.py` route. No LLM imported
  (ADR 0001; the existing `no_llm_guard` integration fixture enforces it).
- **F1 cleanup (ADR-0011), separate commit:** `resolve.py` reads `LINKS_CONCEPT`
  (not `RESOLVES`) and drops the `RESOLVES` writes; the `xfail` on
  `test_f1_resolver_cross_system_resolve` is removed.
- Offline unit tests mock the graph to assert plan-building, SQL composition, the authority
  rules (incl. fail-loud and single-system), and derived/`latest` → `NotImplementedError`. The
  live end-to-end (graph + local PG) extends the existing gated resolver integration test.
- Sets up the follow-on work
  (lineage from the `QueryPlan`).
