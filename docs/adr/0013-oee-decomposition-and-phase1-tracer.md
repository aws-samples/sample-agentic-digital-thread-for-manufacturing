<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# OEE is a derived metric over direct-concept inputs; tracer is `inventory-level`

---
Status: accepted
---

Two decisions from the concept inventory & gaps spike. The inventory confirmed the
catalog defines ~50 concepts but (a) had **no `availability` or `performance` concept** —
both are needed before OEE can be brought online as a derived metric — and (b)
defined `oee` as a **direct concept** (mapping `OEE_Availability/Performance/Quality/Overall`
fields straight to `oee`) while the glossary and the roadmap treat `oee` as a **derived
metric** ("never stored as a source field"). That is a direct contradiction. Refs ADR 0001
(deterministic resolver), ADR 0002 (hybrid metric storage), the glossary (Concept / Direct
Concept / Derived Metric), the tracer-bullet.

Guiding principle (per project standing guidance): the product is unreleased, so a
contradictory/conflated representation is **fixed**, not deprecated around — one meaning,
one representation.

## Decision A — OEE decomposition: `oee` is a derived metric; its inputs are direct concepts

- **`oee` is a derived metric**, not a concept: `oee = availability × performance × quality`
  (quality supplied by `quality-metric`). It is defined once in the metric layer,
  never stored as a source field. The name `oee` is reserved for that derived metric.
- **`availability` and `performance` are new direct concepts** (the OEE inputs). Authority =
  `MES` (OEE inputs come from execution data), consistent with `CONCEPT_AUTHORITY`. `quality`
  is already covered by the existing `quality-metric` concept.
- **The old `oee` direct concept is renamed to `oee-score`** — the *optional precomputed
  composite* a source system may emit (`OEE_Overall`, `oee_score`). Per ADR 0002 the resolver
  reads this stored value directly when a system supplies it, **otherwise** the `oee` metric
  is derived from the three inputs. This is the hybrid-storage shortcut, kept distinct from
  the derived metric so the "stored vs derived" choice is explicit, not ambiguous.
- The onboarding-agent mapping hint (`onboard_agent.py`) is corrected to route each OEE tag
  to its proper concept: `OEE_Availability → availability`, `OEE_Performance → performance`,
  `OEE_Quality → quality-metric`, `OEE_Overall → oee-score` (all role `measure`).

- *Rejected — keep `oee` as a direct concept and also call it a derived metric:* that is the
  contradiction itself; a field-mapped `oee` makes correctness depend on which system happens
  to precompute it.
- *Rejected — remove `oee` entirely (no stored composite):* loses the legitimate ADR-0002
  hybrid case where a historian/MES already publishes an overall OEE; we would have nowhere to
  map `OEE_Overall`.

**Blast radius (verified before the change):** the `oee` concept id had exactly three
functional references — the definition, the `CONCEPT_AUTHORITY` entry, and the one
onboarding-agent hint; all three are updated here. No resolver, metric definition, or
dashboard card keys off `oee` (the metric layer does not exist yet), and no MES system is
onboarded, so there is **no live data impact**. The maintenance-domain mapping keys
(`expected_mappings/cmms*.yaml`) do not reference any OEE concept.

## Decision B — tracer concept is `inventory-level`

The tracer-bullet needs one **direct** concept (not OEE) carried
end-to-end through the new architecture against the only onboarded system (`cmms`).

**Decision:** the tracer is **`inventory-level`** (`cmms_maintenance.spare_parts.quantity_on_hand`).

- It is the **only concept whose full concept→SQL→value path is already proven** — the
  earlier integration tracer returned a real value (18) against local PostgreSQL. This maximally
  de-risks the work where the resolver spine is first built.
- It is direct, numeric, and a legitimate maintenance KPI ("spare parts on hand"), mapped by
  the onboarded `cmms` system.
- It fits the first resolver-backed card exactly: rendered as a *new* card behind a feature flag,
  leaving the hardcoded `UNIFIED_*_SQL` (ERP/MES) paths untouched. This satisfies all three
  acceptance clauses — dashboard shows a real KPI via the resolver; the same endpoint called as an
  agent tool returns an identical value; flag off ⇒ original behavior (no card, no
  regression). The gate does **not** require strangling an already-rendered metric — that is
  later cutover work.
- Authority nuance (not a blocker): `CONCEPT_AUTHORITY` assigns `inventory-level → ERP`, so
  authority flips ERP-ward once an ERP system onboards. Harmless while only the CMMS is onboarded, and
  it sets up a genuine authority demonstration once an ERP is onboarded.

- *Rejected — `lifecycle-state` / `criticality` (already rendered cmms fields):* non-numeric,
  resolver path unproven, and the slice does not need an already-rendered target — strictly worse.
- *Rejected — an ERP/MES KPI already on the dashboard:* requires onboarding ERP/MES (not done
  in the initial scope) and abandons the proven F9 path.

## Consequences

- **The derived-metric path** defines the `oee` derived metric over `availability`,
  `performance`, and `quality-metric`, preferring a mapped `oee-score` when present (ADR 0002).
- The metric layer is seeded with the tracer concept `inventory-level`.
- Adding `availability`/`performance`/`oee-score` propagates automatically: onboarding,
  `server.py` Concept-node creation, and `graph_reset` reseed all iterate
  `CORE_CONCEPT_DEFINITIONS`/`CORE_CONCEPTS` dynamically — no list to hand-edit.
- The mapping-eval keys remain valid (no OEE concepts referenced); concept-id validation in
  `mapping_eval.load_key` continues to pass.

## Amendment — design review (2026-06-11): tracer concept is `mro-inventory`

Decision B accepted `inventory-level` as the tracer and flagged the
`CONCEPT_AUTHORITY: inventory-level → ERP` mismatch as a "harmless nuance" on cmms-only
the CMMS-only deployment. The design review found it is **not** harmless: an authority-based resolver
resolves `inventory-level` to ERP raw-material stock (`inventory_items`), not the CMMS
spare-parts value the tracer — and the integration path, and the seed `spare-parts-on-hand` —
intend. Those are two genuinely different inventories conflated under one concept (the same
lumping the OEE decomposition fixed).

**Resolution — split the concept (supersedes Decision B's tracer id):**
- Add **`mro-inventory`** — a *measure* concept for maintenance/MRO on-hand quantity
  (`quantity_on_hand`, `on_hand_qty`), authority **CMMS**. A legitimate, generic inventory
  class, distinct from raw-material / WIP / finished-goods.
- Keep **`spare-part`** as the part *entity* (part_number, supplier, cost, lead time) — drop
  the `quantity_on_hand`/`reorder_point` hints (they move to `mro-inventory`).
- Keep **`inventory-level`** as ERP material/product stock — drop `quantity_on_hand`; keep
  `stock_level`/`balance_quantity`/`inventory_balances`/`inventory_items`.
- The seed metric `spare-parts-on-hand` re-points its concept to `mro-inventory`; the
  integration test and the `expected_mappings` ground truth update accordingly.

This mirrors the existing **entity↔measure** pair `material` ↔ `inventory-level`, and gives
the tracer an unambiguous CMMS authority with no per-metric system pinning. Underlying rule
worth keeping: a direct *numeric* Metric needs a **measure** concept (one numeric field), not
an **entity** concept (many fields).

**Authority is a deployment default, not a universal:** `mro-inventory → CMMS` is correct for
this topology (CMMS owns spare parts); an ERP-MM-centric org would assign it to ERP.
`CONCEPT_AUTHORITY` is the code-default seed (ADR 0008); the live authority is the graph
`AuthorityRule`, which is runtime-settable (see ADR 0015, Decision C).

## OEE sub-metric coverage on the default deploy

The dashboard OEE gauge fail-softs per sub-reading (`metric_refs` ordered
`oee, availability, performance, quality-rate`); a missing input renders as an honest
**"uncovered"** state with an onboarding suggestion — never an error or a fabricated
number. On the default deploy (no HighByte / AWS IoT SiteWise):

- **quality-metric — covered** (mapped to the MES quality `pass_rate`).
- **performance — deferred.** Derivable in principle as a `RATIO` derived metric
  (ADR 0014) over the MES work-order `planned_quantity`/`actual_quantity`, but that
  first requires mapping those columns to direct concepts — a clean follow-up, not a bug.
- **availability — uncovered by design.** `run-time / planned-production-time` is not
  cleanly derivable from the seed MES schema (no scheduled/planned-production-time
  column, and the resolver has no constant/window primitive). The intended source is the
  optional HighByte / IoT SiteWise OT layer, which publishes `OEE_Availability` directly.
  Leaving it uncovered on the default deploy is the correct, honest behavior — no
  fabricated availability value.
