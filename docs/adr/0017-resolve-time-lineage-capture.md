<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Lineage is an ephemeral record captured on the value, not a persisted store

---
Status: accepted
---

This change implements the ADR 0004 contract — "the resolver stamps each value with the plan, source
ids, and mapping/value-mapping versions it used." Reviewing the design against the as-built resolver
(`metric_resolver.py`) and the graph fixed *what the record is, how it is captured, and how deep
the source/version data goes today*. Refs ADR 0001 (deterministic, no LLM/eval), 0002 (hybrid),
0003 (proposal lifecycle / canonical key — owns versioning), 0004 (lineage captured at
resolve-time, query-provenance depth, confidence+source per relationship, NOT live-reconstructed),
0015 (resolver / `QueryPlan` two-phase split), 0016 (derived evaluator).

## Grounding (as-built)

- `resolve_metric` already builds a serializable `QueryPlan` (system/schema/table/column/aggregation)
  for direct metrics — the lineage seam called out in the module docstring. `_compose_sql`
  builds the SQL string but **discards** it after `_execute`. Derived metrics return `query_plan=None`
  and `_eval_formula` keeps only the sub-metrics' **floats**, discarding their plans.
- `MAPS_TO` edges carry **`confidence`** and **`role`** (set by the onboarding agent). Source
  (agent vs user) is stamped as `method='user_confirmed'` on `IdentityRule`/`JoinPath`/`ValueMapping`
  by `self_learning.py`, but **not** consistently on `MAPS_TO` (onboarding maps default to
  agent-proposed). **Mapping/value-mapping versions do not exist as properties** anywhere yet —
  versioning is the canonical-key + supersede work (ADR 0003).

## Decision A — Ephemeral capture that travels with the value (no persisted store)

`resolve_metric` builds a `Lineage` record in the *same pass* that produces the value and returns
it **on** `ResolvedMetric.lineage`; it flows through `/api/metrics/resolve` and the dashboard
payload. Inspection reads the record that arrived **with** the number — never a live rebuild from
current graph state (ADR 0004's rejected option, which could lie after a correction). Re-derivation
(ADR 0005) re-resolves and re-captures.

- *Rejected for v1 — persisted lineage store:* heavier (new store + lifecycle); its value (audit
  history, out-of-band inspection) isn't needed until the corrections loop. Left as a clean
  later add-on behind the same record type (Decision E seam), not speculative infrastructure.

## Decision B — `Lineage` shape (frozen, serializable; direct + recursive derived)

A frozen dataclass on `ResolvedMetric.lineage: Lineage | None`:
- **Direct:** `{metric_id, kind="direct", concept, authority:{system_id, source}, mapping:{table,
  column, role, confidence, source, mapping_version=None}, query_plan, sql, executed_at}`.
- **Derived:** a recursive composite `{metric_id, kind="derived", op, inputs:[Lineage, …],
  prefer_stored_hit:bool}`. On a `prefer_stored` hit, `inputs` holds the stored metric's lineage and
  `prefer_stored_hit=True`; otherwise it holds one `Lineage` per formula input.

`asdict`-serializable (the `query_plan`'s `Aggregation` enum is rendered `.value` as the route
already does). No `eval`/`exec`; built from data the resolver already touched (ADR 0001).

## Decision C — Capture the actual composed SQL (query provenance)

The `sql` string from `_compose_sql` is stamped into the direct record (it is discarded today).
It contains only validated identifiers (schema/table/column) — no literals, no secrets — and is
safe to expose to authenticated users. Makes row-level drill-through ("re-run this stored query")
a cheap future add-on per ADR 0004, with no rows stored in v1.

## Decision D — Thread lineage through the derived evaluator

`_eval_formula` changes to carry the sub-metrics' `ResolvedMetric` (not bare floats) so derived
lineage nests its inputs' lineage. The numeric result is unchanged; the evaluator now returns
both value and child lineage (e.g. via a small internal result object), keeping `_apply_op`
pure-numeric.

## Decision E — Source/version is a typed seam, captured at today's depth

`source` is taken from `method` where the graph carries it, else `"agent_proposed"`.
`mapping_version`/`value_mapping_version` are typed fields defaulting to `None` until a later change
introduces canonical-key versioning; the corrections loop populates them and may persist records (Decision A seam).
Capturing at today's depth is honest — the record never claims a version/source it doesn't have.

## Consequences

- `metric_resolver.py`: add the `Lineage` dataclass; `resolve_metric` (direct) stamps
  `authority/mapping/query_plan/sql`; `_resolve_table_column` (or a sibling) also returns the
  edge `role`/`confidence`/`source`; `_resolve_derived`/`_eval_formula` thread child lineage;
  `ResolvedMetric.lineage` added; the resolve route emits `lineage`.
- **Inspectable per the acceptance gate:** every resolved value carries its captured lineage in the API +
  dashboard payload; a minimal additive lineage panel on the tracer card (visually gated like
  the tracer card). The polished lineage view UI rides with the card model.
- Sets up the follow-on work and the
  deferred row-level provenance (re-run the captured SQL).
- Offline-testable: lineage shape/threading/SQL-capture proven with mocked resolves; the live
  panel is gated.
