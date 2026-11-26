<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Metric layer design: a uniform Metric (direct or derived) with serializable op-tree formulas

---
Status: accepted
---

This change introduces the **metric layer** (ADR 0002) — the source of truth for *how a value is
defined*, separate from the graph (relationships) and the resolver (values). The design review
surfaced two foundational, hard-to-reverse decisions about its shape plus
the interface seam. The layer is greenfield: no metric-layer code exists, `resolve.py`
handles only cross-system *identity* resolution, and the dashboard reads hardcoded
`UNIFIED_*_SQL`. Refs ADR 0001 (resolver = values, no LLM), 0002 (metric layer = formulas),
0004 (lineage captured at resolve), 0008 (configurability is a deferred seam), 0013
(`oee` is derived over `availability`/`performance`/`quality-metric`).

## Decision A — A uniform `Metric` is the unit of resolution

The resolver, Cards, and agents all reference **Metrics** by id. A Metric is either
**direct** (a *concept-resolution ref*: a concept + an aggregation + optional
dimension/filter, read from the mapped field) or **derived** (a formula over other Metrics).
This replaces the looser "a concept *or* metric request" split and makes the tracer
(a direct concept) a first-class, seedable Metric.

- *Rejected — two request types (bare concept-id for direct, metric-id for derived):* leaves
  "seed the metric layer with the tracer concept" meaningless and scatters aggregation/
  presentation into Cards with no shared handle.

## Decision B — A direct Metric carries aggregation + presentation; v1 resolver implements aggregation only

Schema for a direct Metric: `id` (distinct from the concept id — one concept can back many
Metrics), `concept`, `aggregation` (`sum|avg|count|min|max|latest`), `label`, `unit`, and
`group_by?`/`filter?`/`window?` as **schema seams that are present but resolver-deferred**
(support deferred to the cards/derived path). The graph gives `concept → system.table.column`;
the Metric supplies the aggregation/presentation the graph does not store. The tracer is
`spare-parts-on-hand` = `inventory-level`, `sum(quantity_on_hand)`.

## Decision C — Derived formulas are serializable structured op-trees

A derived formula is a structured tree `{op, inputs[]}` over a **fixed vocabulary**
(`product`, `ratio`, `sum`, `weighted_sum`, `scale`); `inputs` are Metric/concept refs.
The op-tree **type** is defined here (the "derived formula defs" schema this change
charters); the **evaluator** is built when OEE comes online.

Rationale — it is the only representation that satisfies all four governing ADRs at once:
serializable so a future admin store can supply formulas as data (ADR 0008 seam); **safe**,
no arbitrary `eval` (ADR 0001 determinism); **self-describing** so lineage renders
"availability × performance × quality" for free (ADR 0004); and a tiny, closed-vocabulary,
unit-testable, versionable evaluator (ADR 0002's "validated, unit-tested" goal).

- *Rejected — Python callable:* simplest and testable, but **not serializable**, so the
  ADR-0008 admin store could not supply a formula without `Metric.formula` diverging in type
  across implementations (consumers would have to handle both).
- *Rejected — sandboxed string expression:* serializable and self-describing, but needs a
  vetted safe-expression evaluator (added safety surface / dependency) and cuts against the
  determinism-by-simplicity grain. The closed op vocabulary is itself a validation win.

## Decision D — The metric layer is a definition provider behind an interface (ADR 0008)

`MetricLayer` interface exposes `get_metric(id)` and `list_metrics()`. The default
`CodeMetricLayer` reads an in-module registry of code-defined Metrics; consumers obtain it
via a `get_metric_layer()` factory and never see the storage. A future `AdminMetricLayer`
swaps the source without touching the resolver. The **resolver** — not the layer —
turns definitions into a query plan, SQL, and a value (ADR 0001/0002 split preserved).

## Consequences

- New `agentic_digital_thread/agents/metric_layer.py`: `Metric` types, op-tree formula types, the
  `MetricLayer` interface + `CodeMetricLayer` default + `get_metric_layer()`, and
  `validate_metric_layer()`. The op-tree **evaluator** is intentionally *not* built here.
- `validate_metric_layer()` asserts every Metric's concept refs are in `catalog.CORE_CONCEPTS`
  and every derived input resolves to a defined Metric/concept — unit-tested and cheap enough
  to call at startup (ADR 0002 consequence: metric-layer concept refs must resolve to real
  concepts).
- The initial seed provides the direct tracer `spare-parts-on-hand`, and defines `oee` as the first derived
  Metric purely to exercise/validate the op-tree schema — **not wired to the resolver** until
  the evaluator lands.
- The resolver reads Metric definitions → query plan → SQL → value; a follow-up change adds the op-tree
  evaluator and brings OEE online; Cards bind Metric ids; lineage (ADR 0004) renders the
  op-tree.
- the glossary refined: `Metric`, `Metric Layer`, `Resolver Endpoint`.
