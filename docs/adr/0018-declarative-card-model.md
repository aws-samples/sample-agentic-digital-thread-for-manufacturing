<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Declarative metric-card model: a `CardLayer` provider, with a future `DashboardLayer` seam

---
Status: accepted
---

This change closes the phase by formalizing the **card** — the presentation unit that binds Metrics to a
visualization — and generalizing the single hardcoded tracer card into N declaratively-
configured metric cards. Reviewing the design against the as-built dashboard (`dashboard.py`
`fetch_dashboard_data`) scoped the model and set the forward-looking abstraction seam. Refs ADR
0001 (deterministic same-function path), 0002/0014 (Metric layer), 0008 (configurability deferred
behind a provider seam), 0015 (resolver), 0016 (derived), 0017 (lineage). Closes the acceptance gate together with
the derived-metric and lineage work.

## Grounding (as-built)

`fetch_dashboard_data` is mostly **hardcoded SQL returning tables of rows** — `UNIFIED_ERP_SQL`
(orders/suppliers), `UNIFIED_MES_SQL` (production_runs/quality_checks/downtime), `ALL_LINES_SQL`
(line state), per-system queries. The **only** metric/KPI card is the flag-gated tracer card
(one resolver-backed value + lineage). A separate `DashboardCardStorage` (DynamoDB) persists
**AI-generated** cards via prompts — a different feature from declarative metric cards.

## Decision A — The card model is metric-bound; tabular sections are out of scope

A **Card** binds Metric ids to a visualization and resolves to values (+lineage) via
`resolve_metric`. The acceptance gate — "multiple direct + derived metrics render from card configs" — is
about metrics, so the declarative model covers **metric/KPI cards only**. The hardcoded **tabular
operational sections** (orders/production_runs/line-state) are row lists, not Metrics; they stay
untouched (like `UNIFIED_*_SQL`) and are a separate, non-metric concern (deferred indefinitely).
"Migrate existing cards" therefore means the **KPI/metric cards** (today: the tracer; plus any
client-side-computed KPIs in `dashboard.js`, inventoried at plan time) — not the data tables.

- *Rejected — a generic "card" = any data section (tables too):* dilutes the metric-card model,
  binds to no Metric/lineage, and over-reaches the gate.

## Decision B — `Card` schema + `CardLayer` provider (mirror `MetricLayer`)

Frozen `Card{id, title, metric_refs: tuple[str, …], viz_type, group_by: str | None = None}`.
`metric_refs` is a list so one card can show several values. Provider seam mirrors the metric
layer exactly: `CardLayer` ABC + `CodeCardLayer` default + `@lru_cache get_card_layer()`, seeding
the metric cards; `validate_card_layer()` asserts every `metric_ref` resolves against the metric
layer (`get_metric_layer`). Definitions live in code now (ADR 0008 seam); the provider is
swappable later for a storage- or AI-backed one.

## Decision C — `viz_type`/`group_by` declared; only `kpi` implemented now

`viz_type` vocabulary is declared (closed enum: `kpi` now; `bar`/`line`/`gauge` reserved), but
only **`kpi`** (single value per metric_ref) is implemented — the resolver's `group_by` is a
deferred seam, so grouped visualizations are declared-but-gated and fail loud / are skipped until
the resolver supports grouped resolution. `group_by` on a card is carried but not yet resolved.

## Decision D — Honest empty state (generalize the tracer's `unavailable`)

Resolving a card resolves each `metric_ref`; on any resolver boundary error the card renders an
explicit `status:"unavailable"` with the reason and `value:None` — never a fake/zero value
(generalizes `_build_tracer_card`). `oee` with no MES onboarded is the canonical unavailable card.
This is the seam coverage builds on.

## Decision E — Strangler discipline: flag-gated `cards` array, byte-identical off

`fetch_dashboard_data` emits a flag-gated `cards` array built from the `CardLayer` (each card
resolved via `resolve_metric`, carrying value + lineage); the tracer card becomes one entry.
Flag-off ⇒ no `cards` key, `resolve_metric` never called, payload byte-identical (ADR 0001).
`UNIFIED_*_SQL` and the tabular sections are untouched. The live multi-card visual is gated like
the earlier flag-gated cards.

## Decision F — Architect for a future `DashboardLayer` (seam only, no build now)

The **provider is the abstraction seam**. Today `CardLayer` yields an ordered list of cards = the
single default dashboard. A future **`DashboardLayer`** composes cards into dashboards — ordering,
layout, multiple/role-scoped dashboards, and merging the AI-generated `DashboardCardStorage`
cards — by wrapping or swapping the provider, with **no change to the `Card` model**. This change builds
none of that complexity; it only keeps the card model and `get_card_layer()` clean enough that a
DashboardLayer sits cleanly above it (per the user's forward-looking intent).

## Consequences

- New `agents/card_layer.py`: `Card`, `CardLayer`/`CodeCardLayer`/`get_card_layer()`,
  `validate_card_layer()`, the `viz_type` enum, seed cards (`spare-parts-on-hand` kpi, `oee` kpi).
- `dashboard.py`: a `_build_cards()` that resolves each card's metric_refs (value + lineage +
  honest unavailable), behind the existing flag; the tracer card folds into this `cards` array.
- `dashboard.js`: render the `cards` array generically by `viz_type` (kpi), reusing the
  lineage expander per card; additive, flag-off DOM byte-identical.
- Offline-testable: card validation, resolution, unavailable state, byte-identical-off proven with
  mocked resolves; the live multi-card visual is gated.
- Closes **the acceptance gate**: multiple direct + derived metrics render from card configs, each value
  carrying its captured lineage.
