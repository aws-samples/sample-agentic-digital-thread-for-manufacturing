<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# The metric layer—KPIs, resolution, lineage, and dashboard cards

How a number on the dashboard (or in an agent's answer) is defined, resolved,
and explained. This guide describes the **current state** of the subsystem;
the decision history lives in the ADRs referenced at the end.

## The one-sentence contract

Every KPI is a **Metric** selected from a two-layer definition model—the
23 engine-owned code definitions plus steward-accepted deployment definitions
in the graph—and resolved by one deterministic function, `resolve_metric()`
in `agents/metric_resolver.py`, with **no LLM in the path**. The dashboard and
agent tool use the same composed layer and resolver, so they return the same
value and every value carries the **lineage** that produced it.

## The pieces

| Piece | Module | Role |
|---|---|---|
| Code metric layer | `agents/metric_layer.py` | Engine-owned source of truth for the 23 shared `Metric` definitions (direct or derived), op-tree formulas, and the `MetricLayer` interface. |
| Graph definition artifact + store | `agents/metric_definition.py`, `agents/metric_definition_store.py` | Validates/canonicalizes deployment definitions; MERGEs accepted `MetricDefinition` nodes; serves the bounded TTL snapshot. |
| Composed metric layer | `agents/graph_metric_layer.py`, `metric_layer.get_metric_layer()` | Presents code + accepted graph definitions through the unchanged `MetricLayer` seam; code ids always take precedence. |
| Definition lifecycle and admin read model | `agents/self_learning.py`, `agents/vocabulary_routes.py` | Files pending proposals, computes the steward diff, accepts through the existing queue, and lists code/accepted/pending/shadowed rows. |
| Metric resolver | `agents/metric_resolver.py` | Turns a Metric into a value: graph lookup → `QueryPlan` → SQL → `ResolvedMetric` (value + `Lineage`). `set_authority()` writes the graph `AuthorityRule`. |
| Card layer | `agents/card_layer.py` | Presentation: a `Card` binds metric ids to a `viz_type`; `get_card_layer()` provider mirrors the metric layer. |
| Dashboard assembly | `agents/dashboard.py` (`_build_cards`) | Resolves each card's metrics (TTL-cached), applies the honest-data statuses, fans out per site. |
| Multi-site fan-out | `agents/metric_multisite.py` | Canonical `site_of` / `site_sort_key` home (ADR-0045): per-site card copies are built inside `_build_cards`, never per-viewer. |
| API | `agents/metric_resolver.py` (router mounted by `server.py`) | `POST /api/metrics/resolve`, `POST /api/metrics/set-authority`, and the dimensional variant `/api/metrics/resolve-by-dimension`. |

## Two-layer definition model and lifecycle

The **code layer** is the shared engine vocabulary: exactly 23 immutable seed
metrics in `CodeMetricLayer`, reviewed and tested as code. The **graph layer**
is deployment vocabulary: canonical structured documents on standalone
`MetricDefinition` nodes. `ComposedMetricLayer` appends valid graph definitions
behind the unchanged `get_metric_layer()` seam, so existing resolver, card,
coverage, dimension, and concept consumers need no graph-specific branch.
Definitions remain structured op-tree data throughout; they are never rendered
into `query_guidance` or any other prompt surface.

A graph definition has one authority path:

1. An admin proposal or conversational capture validates and canonicalizes the
   document, then files it **pending** in the existing proposal lifecycle.
   `metric_definition` and source `conversation` are both deny-listed from
   auto-accept; there is no immediate-admin accept path.
2. The admin read model shows a computed structural diff. The steward accepts
   through the existing proposal queue—the only door to authority.
3. Accept re-validates against the current concept/metric universe, MERGEs the
   node by id, re-reads `definition_json`, and requires an exact canonical-byte
   match before the proposal can become accepted. The accepting process then
   invalidates its snapshot; other processes converge within one TTL.
4. Usage telemetry can prove deterministic resolution or pre-check composition,
   never that a model obeyed a definition.

Code ids always win. A collision is rejected at both propose and accept time.
If a residual colliding graph row exists, it is excluded from resolution,
logged, and listed as `shadowed` with a reason; it is never served as accepted
and never silently disappears.

### TTL, cold, warm, and stale behavior

`metric_definition_store` reads at most 500 definitions into a process-local
snapshot. The default TTL is 300 seconds and is tunable with
`METRIC_DEFINITION_TTL_SECONDS`; callers make zero extra graph reads while the
snapshot is warm. A cold store failure raises rather than fabricating an empty
deployment vocabulary. Once a good snapshot exists, a refresh failure logs and
serves that stale snapshot for another TTL window.

The two consumers contain a cold raise differently, according to what they can
honestly claim. The **metric layer** fails open to its complete code layer, so a
graph-defined id yields only the normal honest unknown-metric error—never a
claim that the definition is absent from the deployment. The metric-term
**pre-check** fails open to an empty notice, so it says nothing rather than
fabricating “undefined in this deployment.” Thus a graph outage can produce
neither a value nor an absence claim.

## Metric shapes

**Direct Metric**—a concept + an aggregation, read from whichever system the
graph says is authoritative: `{id, concept, aggregation(sum|avg|count|min|max|latest),
label, unit}`. The metric id is distinct from the concept id—one concept can
back many Metrics.

**Derived Metric**—a serializable op-tree formula over other Metrics:
`Formula{op, inputs[]}` with the closed vocabulary `product | ratio | sum |
weighted_sum | scale`; inputs are `MetricRef`s (never bare concepts—a bare
concept has no defined aggregation), `Scalar`s, or nested formulas. No
`eval`/`exec` anywhere. A derived Metric may set `prefer_stored: <metric-id>`—if
a source system publishes a precomputed composite (for example an
`OEE_Overall` field), the resolver reads that stored value and only otherwise
evaluates the formula.

The canonical example is OEE: `oee = product(availability, performance,
quality-rate)` with `prefer_stored="oee-score"`.

## Resolution semantics

1. `resolve_metric(metric_id)` builds an immutable **`QueryPlan`**
   (`system/schema/table/column/aggregation`) from the graph *before* touching
   any database—the plan is the unit-testable seam and the lineage anchor.
2. **Authority**: the concept's `AuthorityRule` in the graph picks the system
   (seeded from `catalog.CONCEPT_AUTHORITY` at onboarding, runtime-settable through
   `set_authority` / the steward-gated `POST /api/metrics/set-authority`). If no rule exists and
   exactly one onboarded system maps the concept, that system is used;
   otherwise resolution **raises** with a message naming the remedy. Never a
   silent fallback. The concept's COLUMN on that system follows ONE rule
   (ADR-0076): exactly one `MAPS_TO` binding is used; 2+ are chosen only by a
   steward-set `AuthorityRule.field` (`set-authority` with `{field, table?}`,
   validated against the candidates, read back) or WITHHELD as
   `ambiguous_binding` naming every candidate. A dotted physical column
   renders as ONE quoted identifier.
3. **Fail loud, never fabricate**: an unresolvable input (unmapped concept, no
   authority, divide-by-zero) raises a clear `MetricResolverError`; nothing
   substitutes a zero or placeholder.
4. **`LATEST` aggregation** resolves through `DISTINCT ON` ordered by the
   graph-modeled `event-time` concept; dimensional (grouped) resolution and
   render hints ride the `/resolve-by-dimension` route (ADR-0029).

## Lineage

Lineage is an **ephemeral record captured with the value**—never
reconstructed later from current graph state (which could lie after a
correction). `ResolvedMetric.lineage` carries, for a direct metric: concept,
authority (+source), mapping (table/column/role/confidence/source), the
`QueryPlan`, and the **actual composed SQL**; for a derived metric: the op,
`prefer_stored_hit`, and one nested `Lineage` per input. It flows through
`/api/metrics/resolve` into the dashboard's per-card lineage expander.

## Cards and honest statuses

A `Card{id, title, metric_refs[], viz_type, group_by?}` resolves each ref through
`resolve_metric`. Status vocabulary (pinned by `tests/unit/test_dashboard_cards.py`
and the honest-data rule, ADR-0045):

- **ok**—value present.
- **no-data**—the query resolved but nothing is behind it (`value None` /
  `sample_count == 0`). Distinct from unavailable; zeros with samples stay
  visible.
- **unavailable**—a resolver boundary error (for example `oee` with no MES
  onboarded); the reason is rendered, never a fake value.
- **uncovered** OEE sub-readings render an onboarding suggestion (the gauge
  fail-softs per sub-reading; `availability` is uncovered by design on the
  default deploy—its intended source is the optional OT layer).

Multi-site: `_build_cards` fans a card out per site using
`metric_multisite.site_of`; fan-out happens once inside the TTL cache, never
per-viewer.

## Extending

- **Shared engine KPI**: add a `Metric` (and usually a `Card`) to the code
  registries in `metric_layer.py` / `card_layer.py`. `validate_metric_layer()` /
  `validate_card_layer()` assert every reference resolves—both run cheap at
  startup and in unit tests. This is the correct path when every deployment
  should inherit the same semantic definition.
- **Deployment-specific KPI**: propose a structured metric definition through
  the admin metrics surface, review its computed diff, and accept it through the
  steward queue. Do not add customer vocabulary to engine code and do not encode
  formulas in `query_guidance`. Graph-backed cards are not part of v1.
- **New formula op**: extend the closed `FormulaOp` vocabulary and its
  evaluator arm in `metric_resolver.py`—keep it serializable and
  deterministic (no user-supplied expressions).
- **Different authority for a concept**: `POST /api/metrics/set-authority` (or
  `set_authority()`); authority is a per-deployment fact, not a universal.
  A definition's optional `authoritative_system` is advisory only;
  `AuthorityRule` remains the one authority home.
- The `MetricLayer` provider remains the consumer seam. Extend composition
  behind `get_metric_layer()`, never by adding graph branches to resolvers.

## Decision history

[ADR-0001](adr/0001-deterministic-shared-resolver-endpoints.md) (deterministic
shared endpoints), [ADR-0002](adr/0002-hybrid-metric-storage.md) (hybrid
storage), [ADR-0013](adr/0013-oee-decomposition-and-phase1-tracer.md) (OEE
decomposition), [ADR-0014](adr/0014-metric-layer-design.md) (metric layer),
[ADR-0015](adr/0015-deterministic-metric-resolver.md) (resolver),
[ADR-0016](adr/0016-derived-metric-resolution.md) (derived resolution),
[ADR-0017](adr/0017-resolve-time-lineage-capture.md) (lineage),
[ADR-0018](adr/0018-declarative-card-model.md) (cards),
[ADR-0029](adr/0029-dimensional-resolution-and-render-hints.md) (dimensions),
[ADR-0045](adr/0045-multi-site-metric-fan-out.md) (multi-site fan-out), and
[ADR-0066](adr/0066-graph-backed-metric-definitions.md) (graph-backed metric
definitions and steward lifecycle).
