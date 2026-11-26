<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Multi-site metric fan-out: concept-driven, TTL-cache-only, honest by construction

---
Status: accepted — 2026-07-15

Related: builds on [ADR-0016](0016-derived-metric-resolution.md) and
[ADR-0021](0021-two-layer-concept-model.md); verified against a deployed
environment per [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md).
---

## Context

Dashboard cards resolved every metric through a single authority: the resolver
picked ONE system for a metric and the card showed that system's value. On a
deployment with several plants' systems onboarded (each mapping the same
concepts), a "Production Output" card silently showed one site's number
labeled as if it were the whole operation — misleading, and there was no
per-site breakdown at all.

Fixing this must respect the project constraint: **no demo, dataset, system,
or plant literals in engine code**. Which systems participate, and what "site" means, must come from graph metadata and
the catalog — the knowledge graph is a rules engine
([ADR-0021](0021-two-layer-concept-model.md)), so the fan-out has to be
derived from `Field-[:MAPS_TO]->Concept` coverage, never from a list of known
plants.

Two adjacent honesty problems surfaced in the same overhaul: cards rendered
dead "Unavailable" tiles when a metric resolved fine but had no rows behind it
(indistinguishable from a broken pipeline), and by-line output bars showed
lifetime totals with no indication of the accumulation window.

## Decision

### Decision 1 — Site is the catalog `plant` of concept-covering systems

`agents/metric_multisite.py` is the fan-out engine. Participation is derived
from ONE graph read (the `coverage.covered_concepts` traversal, returning
`system_id` per pair) intersected with the onboarded catalog: a system covers
a metric iff the metric is satisfiable against that system's covered concept
set alone. Each contributing system's site label is its catalog `plant` field;
the wildcard `'all'` (case-insensitive) or an empty value means "no site" and
is carried as `""` — an unsited system still contributes to the aggregate.
`site_of` and `site_sort_key` (unsited first, then sites alphabetically, then
name) are the single canonical homes for these rules, shared by
`dashboard._system_context` / `dashboard_line_state` and `dashboard_drill`.
The canonical per-system pin is `metric_resolver.PinnedAuthority` (the former
local copies in `dashboard` and `metric_multisite` collapsed into that one
class). No dataset, system, plant, or schema names appear in the module.

### Decision 2 — Simple-mean AVG semantics (weighted later via `sample_count`)

`resolve_metric_multi` resolves the metric per covering system (authority
pinned) and combines surviving values by the metric's aggregation:

- SUM / COUNT → arithmetic sum.
- AVG → **simple arithmetic mean** of per-system values — a deliberate,
  documented simplification. Row-weighted means need per-site sample counts;
  the `sample_count` now carried on `ResolvedMetric`/lineage (see Decision 4)
  enables weighting later **without a payload change**.
- MAX / MIN → max / min.
- LATEST → the aggregate is honestly SKIPPED (`value: None`, method
  `"latest-not-combined"`): there is no cross-system event ordering to pick a
  single "latest" from. Per-site values still ride in `sites`.
- Derived metrics ([ADR-0016](0016-derived-metric-resolution.md)) → the WHOLE
  formula is evaluated per pinned system, then per-site results combine with
  the simple mean (per-site OEE → overall OEE = mean of site OEE).

Per-system failures are fail-soft (that site is marked `"error"` and skipped,
the `dashboard_line_state` pattern); zero contributors is fail-loud
(`MetricResolverError`) — callers fail soft, never fabricate.

### Decision 3 — Fan-out ONLY in the TTL-cached `_build_cards` path, behind the ≥2-onboarded gate

> **Amended by [ADR-0094](0094-per-site-authority-answers-per-site.md)
> (2026-10-02):** the per-site fan-out may also run inside the ADR-0078
> per-turn notice, which is TTL-cached per metric, generation-guarded,
> timeboxed and on its own pool, when the metric's authority is a per-site
> partition. Still never per-viewer or per SSE tick.

The fan-out multiplies backend queries by the number of covering systems, so
it runs exclusively inside the TTL-cached `_build_cards` path — never
per-viewer or per-SSE-tick. `dashboard._multi_site_covering` gates it cheaply:
with fewer than two onboarded systems NO graph read happens at all (a
single-system deployment pays zero extra cost and its card payload stays
byte-identical — the exact-key-set snapshot discipline), and the fan-out
applies only when MORE than one onboarded system covers the card's primary
metric. The per-site breakdown rides as a conditional `sites` key on the
primary metric only; sub-readings stay aggregate-level. The gate is
fail-safe → single-authority on any error, but the swallow is observable
(warn-once per card per process).

### Decision 4 — The `no-data` status and the honest-data hiding rule

The scalar resolve additionally reports its non-null `sample_count` (one
query; conditional key). Card metric entries are classified three ways:

- resolver boundary error → `"unavailable"` (unchanged — the pipeline broke);
- resolve OK but `value is None` or `sample_count == 0` → the NEW status
  **`"no-data"`** (the pipeline works, the data is absent);
- otherwise → `"ok"`.

The frontend enforces the hiding rule: a covered card whose data is degenerate
(card-level `no-data`, or every metric `no-data`) collapses into the existing
"Awaiting data" strip machinery under a distinct **"No data yet"** bucket
(`kind: 'no-data'` vs `'structural'`) instead of rendering a dead tile; panels
whose rows are all-null hide via the shared `hasNonDegenerateRows` helper.
Zeros with samples are honest values and stay visible (with an idle
annotation where the line-state slice corroborates). Nothing fabricates,
nothing renders dead.

### Decision 5 — The windowed-metric seam (`WINDOW_VOCAB`, lineage-carried honesty)

Lifetime totals are rarely decision-useful. `metric_dimension.WINDOW_VOCAB` is
a **closed vocabulary** of window tokens (`1h`/`24h`/`7d`/`30d`), each
carrying the PostgreSQL interval literal and the Athena `(quantity, unit)`
spelling. A `DirectMetric.window` token MUST be a vocabulary key (fail loud —
a registry error, never user input); the time predicate applies only when the
measure table has a mapped event-time column, identifiers go through
`validate_identifier`, and no string-interpolated user input ever reaches the
SQL. Honesty is lineage-carried: `window`/`window_column` are additive plan
fields (default None, pre-existing plans byte-compatible) set only when the
predicate ACTUALLY applied; on the honest lifetime fallback (no event-time
mapping / non-SQL system) `window` stays None, and the frontend caption keys
off the lineage-carried value ("24h window" vs "lifetime total").

## Considered alternatives

- **Fan out inside the resolver itself (every `resolve_metric` call).**
  Rejected: multiplies query cost on hot paths (chat tools, SSE ticks) and
  changes the single-authority contract every existing caller depends on. The
  card pipeline is the one consumer that wants the breakdown, and it is
  already TTL-cached.
- **A `site` property on the graph, or per-plant Cypher.** Rejected: plants
  are dataset knowledge; the graph stores field→concept metadata
  ([ADR-0021](0021-two-layer-concept-model.md)). The catalog `plant` field is
  already the deployment's own metadata, and the wildcard convention existed.
- **Row-weighted AVG now.** Rejected for this cycle: it requires the per-site
  sample counts to be reliable across every resolver backend first. The
  simple mean is documented and the `sample_count` seam makes weighting a
  later, payload-compatible change.
- **Hide `no-data` cards entirely (no strip entry).** Rejected: silence is
  dishonest — the operator can't distinguish "not mapped" from "mapped, no
  rows yet" from "broken". The two strip buckets plus `unavailable` keep the
  three states visually distinct.
- **Free-form window strings on metrics.** Rejected: interval SQL composed
  from open input is an injection surface and untestable. The closed
  vocabulary is grep-able, fail-loud, and covers the decision-useful windows.

## Consequences

- **Positive:** multi-plant deployments get true aggregates plus per-site
  breakdowns with zero engine literals; single-system deployments are
  byte-identical and pay no cost; dead tiles are gone — every card state
  (`ok` / `no-data` / `unavailable` / uncovered) is visually and structurally
  distinct; windowed metrics can never silently claim a window they didn't
  apply.
- **Negative / risks:** the simple mean over-weights small sites until
  weighting lands; LATEST metrics have no cross-site aggregate (deliberate);
  the fan-out's graph read and N pinned resolves run on every TTL cards
  rebuild in multi-system deployments (bounded by the cache TTL and the
  cheap ≥2 gate); every new payload key had to be conditional to preserve the
  exact-key-set snapshot discipline.
- **Verification:** offline — `tests/unit/test_metric_multisite.py`,
  `test_dashboard_multisite.py`, window-composition tests (mocked graph/
  resolver; no plant/system literals, grep-gated); deployed — per-site card
  values, OEE per-site chips, windowed output captions (the Neptune graph is
  VPC-private per
  [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)).
