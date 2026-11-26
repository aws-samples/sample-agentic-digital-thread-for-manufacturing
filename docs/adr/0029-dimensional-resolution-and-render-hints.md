<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Dimensional metric resolution (grouped + LATEST), the sensor heatmap, active-issues, and the `kind` render-hint contract

---
Status: accepted
---

The dashboard cutover replaced the fixed-shape `UNIFIED_*_SQL` dashboard with the mapping-driven
resolver + tabular sections, but three "rich" panels (quality-by-line bars, the sensor heatmap,
the active-issues list) had no mapping-driven equivalent and were dropped to a tracked follow-up
("option (c)"). This ADR records how they were rebuilt on the metric/entity foundation. The
grounding constraint: the scalar resolver (ADR 0015) collapses a Metric to **one** value, and
both ADR 0015 (Decision E) and ADR 0016 (Decision E) explicitly **deferred** a group-by /
time-ordering dimension. Rebuilding the panels is exactly that deferred work. Refs ADR 0001
(deterministic resolver, no LLM in the render path), 0006 (coverage / honest uncovered state),
0011 (canonical graph; single-hop `JoinPath`), 0015 (scalar `resolve_metric` + `QueryPlan` +
authority), 0016 (derived path; group-by deferral), 0017 (resolve-time lineage), 0021
(two-layer concepts; `entity_render` sections).

## Decision A — A dimensional resolver, separate from the scalar resolver

`agents/metric_dimension.py` adds `resolve_metric_by_dimension(metric_id, dimension_concept)`,
the grouped sibling of ADR-0015's `resolve_metric`. It produces an immutable `GroupedQueryPlan`
and a `GroupedResolvedMetric` carrying **rows** of `{dimension_value, value}` instead of one
scalar. It reuses the scalar resolver's machinery verbatim — authority resolution, the
`MAPS_TO` traversal, `validate_identifier`, and the `connection_type` execution routing — so the
measure column, its aggregation, and the grouping column are all graph-sourced, never guessed.
The composed `GROUP BY` SQL for `AVG`/`SUM`/`COUNT` is **byte-identical** to the scalar SQL plus
the group-by clause (locked by golden-string tests), so the grouped path cannot silently diverge
from the proven scalar path. A thin `POST /api/metrics/resolve-by-dimension` route wraps it; the
dashboard and agents-as-tools call the same function (ADR 0001).

## Decision B — `LATEST` resolution via `DISTINCT ON`, ordered by a graph-modeled `event-time`

ADR 0015 deferred `latest` because we did not model a time/ordering dimension. We now do: a new
`event-time` **core concept** marks the timestamp/sequence column a system maps. `LATEST`
resolution composes `SELECT DISTINCT ON (<dims>) … ORDER BY <dims>, <order_column> DESC`, where
`order_column` is resolved from the `event-time` concept and `GroupedQueryPlan` gains an
`order_column` field. If a `LATEST` plan has no resolvable `event-time`, resolution **fails loud**
(it does not silently fall back to an arbitrary row). The `AVG`/`SUM` SQL is unchanged and stays
byte-identical (Decision A); `LATEST` is a distinct branch with its own golden + regression tests.
This closes the ADR 0015 / ADR 0016 group-by + latest deferrals.

## Decision C — The sensor heatmap is metric-by-TWO-dimensions, with an optional threshold join

`agents/heatmap_resolver.py` adds `resolve_heatmap(metric_id, dim_x, dim_y, threshold_concept?)`:
a `LATEST` measure (Decision B) grouped over **two** dimension concepts into a grid, with an
optional threshold comparison joined in over a **single-hop `JoinPath`** (ADR 0011) — no
fabricated multi-hop traversal; fan-out beyond one hop **fails loud**. Severity (ok / warning /
breach vs the threshold) is computed **server-side** so the renderer stays presentational
(ADR 0001). Cells/rows are `LIMIT`-capped. When the required concepts are not mapped, the panel
degrades to an honest uncovered state (ADR 0006), never a 500. `POST /api/metrics/resolve-heatmap`
wraps it.

## Decision D — Active-issues is a cross-entity union over a closed source set, via a closed predicate vocabulary

`agents/active_issues.py` unions operationally-interesting rows across entities (e.g. quality
`pass_rate < 90`, work-orders `status IN (CANCELLED, PAUSED)`, orders `BACKORDERED`) into a
uniform `{severity, title, detail, source_system, entity, lineage}` shape. To filter rows
safely it adds `entity_render.build_filtered_section_sql` driven by a **closed predicate
vocabulary**: a `PredicateOp` enum (a fixed token set — no free-form operators), values that are
either number-coerced or allowlisted against `^[A-Z][A-Z0-9_]*$` then single-quoted, and every
identifier passed through `validate_identifier`. **No user/graph value is ever interpolated raw
into SQL.** An independent security review verdict confirmed this surface is injection-safe by
construction (closed vocabulary + identifier allowlist + read-only connection + universal
`LIMIT`). `GET /api/dashboard/active-issues` returns the friendly envelope; per-source row caps
bound the query.

## Decision E — A forward-compatible `kind` render-hint contract

Every rich-panel payload carries a `kind` discriminator (`grouped`, `heatmap`, …) — a
render-hint, not a schema. The frontend dispatches on `kind` to a pure-DOM renderer, so a new
panel type is an **additive** server `kind` + renderer, with no change to existing payloads or
renderers. Payloads stay row-shaped and carry per-row/ -cell **lineage** (ADR 0017), so a value's
provenance travels with it into the panel exactly as it does for scalar cards. This is the
contract the cutover research proposed; it is what makes the panels forward-compatible rather
than a second fixed-shape coupling.

## Consequences

- New modules `agents/metric_dimension.py`, `agents/heatmap_resolver.py`, `agents/active_issues.py`,
  and the `build_filtered_section_sql` + `PredicateOp` additions to `agents/entity_render.py`;
  new routes `POST /api/metrics/resolve-by-dimension`, `POST /api/metrics/resolve-heatmap`,
  `GET /api/dashboard/active-issues`. New `event-time` core concept. No LLM imported (ADR 0001;
  `no_llm_guard` enforces it).
- **Supersedes the dimension deferrals** in ADR 0015 (Decision E, `latest`) and ADR 0016
  (Decision E, group-by) — both now updated to point here.
- Tests: golden byte-identical `AVG`/`SUM` SQL (grouped == scalar + GROUP BY), a `LATEST`
  regression test, injection tests over the closed predicate vocabulary, and a heatmap
  integration test that executes the composed `DISTINCT ON` + threshold-join SQL against real
  PostgreSQL.
- Quality-by-line, the sensor heatmap, and active-issues were rebuilt on this foundation,
  **deployed, and verified live** (Playwright, 0 console errors). The heatmap degrades to an
  honest uncovered state until a sensor/HighByte system maps `sensor-reading` + `event-time`.
- The closed predicate vocabulary is the reusable safe-filter seam: any future filtered section
  goes through it rather than composing SQL ad hoc.
