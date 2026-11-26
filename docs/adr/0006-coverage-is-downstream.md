<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Dashboard coverage is a downstream concern; onboarding is never blocked by it

---
Status: accepted
---

The set of concepts the dashboard needs is computed from card→metric→concept bindings,
not hand-maintained. Coverage is evaluated across **all** onboarded systems (a concept is
covered if any onboarded system maps it; a card is live when all its input concepts are
covered by someone). Unmet needs surface as honest per-card gating on the dashboard
("onboard a system providing `performance` to enable") and as a thread-level gap report —
**never** as a block on an individual system's onboarding.

## Context

Mapping during onboarding is motivated by building the semantic thread for all consumers
(agents, queries, dashboard), not by dashboard cards specifically. The dashboard is one
consumer. Coverage therefore flows *out* of onboarding as a downstream property, not *into*
it as a requirement.

## Considered Options

- **Block onboarding until required concepts are mapped:** rejected. Coverage is a
  cross-system property; blocking one system for dashboard-wide needs would punish a
  system (e.g. an ERP) for not containing data it was never meant to have (e.g. OEE).

## Consequences

- Needs a maintained card→metric→concept dependency index (the ADR 0005 reverse index,
  read the other direction).
- Improving *mapping recommendations* during onboarding is a separate concern, handled via
  concept↔system-type affinity (its own decision).

## Refinement (design review, 2026-06-15)

- **Proactive, two-state gating.** Coverage is computed *proactively* from the graph, distinct from
  the reactive resolve-time failure. A Card is **uncovered** (structural gap — no onboarded system
  maps a needed concept; actionable "onboard a system providing X") vs **unavailable** (covered but
  resolution errored at runtime). The pre-existing `unavailable` state (ADR 0018) stays for the latter.
- **Coverage mirrors the resolver, not a flat concept union.** `metric_layer.is_satisfiable(metric,
  covered_set)` follows ADR 0016 branching: direct → its concept covered; derived → all inputs
  (recursively) covered; derived with `prefer_stored` → stored concept covered **OR** all inputs
  covered. (The flat `_concepts_of` closure — built for re-derivation impact — would wrongly gate a
  hybrid metric as uncovered.) A missing-concepts helper reports the cheapest unmet path.
- **A pure `agents/coverage.py` module.** One graph read returns the covered-concept set for onboarded
  systems (`(s:System)-[:HAS_SCHEMA]->…-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->(c:Concept)`; offboarding
  deletes a system's edges, so ≥1 `MAPS_TO` ≡ covered), injected into the pure satisfiability
  composition. Feeds **both** the dashboard per-card gating and the thread-level gap report (one
  satisfiability source). The existing `/concept-coverage` (graph-vocabulary view) is left untouched.
- **Thread-level gap report.** `GET /api/coverage` (session-authed) → `{cards:[{card_id,satisfiable,
  missing_concepts}], gaps:[{concept,blocks_cards,suggestion}]}`, surfaced as a compact coverage
  summary on the dashboard. Suggestions are plain/deterministic now ("Onboard a system providing X")
  with a seam for **type affinity** (ADR 0007) to enrich them ("a CMMS/MES typically provides this").
- Unchanged invariants: coverage is read-only/downstream (never blocks onboarding); pure graph read,
  no LLM (ADR 0001).
