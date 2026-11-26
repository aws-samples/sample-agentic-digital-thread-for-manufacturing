<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Two-layer concept model: entity (primary/total) + field (earned), graph-sourced, reuse-first

---
Status: accepted
---

_Status: accepted (design). **Revised 2026-06-16**
after the full 6-system onboarding validation: the two layers are
**not symmetric** — the entity layer is primary and total; the field layer is secondary and earned._

> **Extended in part by [ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md)
> (2026-07-09):** the two-layer model is generalized to work for ANY onboarded system
> (not just the seven curated demo SQL schemas). The curated `entity_for_table`
> fast-path and the asymmetric field-minting framing below are unchanged; ADR-0040 adds
> a layered entity-inference seam (curated → heuristic → LLM/mint), a dedicated
> `SchemaEntry-[:IDENTIFIED_BY]->Field` identity edge, entity-anchored resolve, and the
> non-curated backfill.

The concept vocabulary splits into two layers with **asymmetric roles**.

**Entity layer — primary, total, curated.** An **Entity Concept** names a *kind of record*
(`supplier`, `work-order`, `production-run`, `asset`) and maps to a source **table** via a new
`SchemaEntry -[:MAPS_TO]-> Concept` edge (A follow-up change adds this). **Every onboarded table maps to exactly
one entity concept**, so the entity layer must be **complete over all tables**. Completing it
surfaces deliberate gaps (e.g. `asset` for the CMMS `assets` table; a customer/sales-order entity
for ERP `customer_orders`; line-item tables fold into their parent entity per existing vocab
guidance). Curated/closed — you rarely invent a new *kind of record*.

**Field layer — secondary, selective, earned.** A **Field Concept** names the meaning of a single
**column** (`mro-inventory`, `payment`, `criticality`). Because every field sits on a table that
maps to an entity, **every field already belongs to an entity** — `belongs-to` is **total and
automatic**, the per-system traversal
`(entity)<-[:MAPS_TO]-(SchemaEntry)-[:HAS_FIELD]->(Field)-[:MAPS_TO]->(fieldConcept)` (many-to-many,
instance-level).

The "selective/earned" quality applies to **minting**, **not to mapping** — these are different acts:

1. **Mapping a column to an *existing* field concept (core or already-minted): unconditional.**
   Whenever a column's meaning matches a concept in the vocabulary, map it — single system or not.
   This is the existing onboarding behavior and it is load-bearing: the cross-system thread is built
   by *intersecting Field→Concept mappings* (`onboard._run_resolve_job`), and the mapping-quality
   contract (`tests/integration/expected_mappings/*.yaml`, 0.90 floor, must-map tracers) is entirely
   **core** concepts on single-system onboards. Gating core mappings would zero out both. **Do not.**
2. **Minting a *new dynamic* field concept: earned.** Create a new concept (and map the column to it)
   only when (a) the meaning recurs on an entity **shared by ≥2 systems** (so the minted concept
   anchors cross-system relationships — e.g. an ERP-only domain field that a second ERP-like system
   also has), or (b) the column feeds a Card / derived Metric. Otherwise the column stays
   **entity-homed but unlabeled** — the one-off domain long tail and the generic/audit columns
   (`created_at`, `notes`, `is_active`, `entity_type`, `source_system`, surrogate ids). This is what
   keeps the dynamic vocabulary convergent — **no minting noise**.

So a single-system onboard still maps freely to core (thread + eval intact); minting has the lazy
lifecycle (few/no mints on the first system of a kind; the second same-entity system earns the
shared dynamic concepts). When a later onboard makes an entity newly shared, the dynamic-concept
**backfill** re-labels the earlier system's matching columns and re-runs resolve (see Consequences).

**Reuse-first is entity-scoped.** When onboarding, for each `table→entity`, column suggestions are
drawn from **other systems already mapped to the same entity** (precedent), plus type **Affinity**
(ADR 0007) and name/alias match. Minting a new field concept is the deliberate fallback when no
existing concept fits *and* the earned test passes.

**Graph is the single source of truth** for concept identity and all mappings. A flat **Concept
Layer** read-model/index (core ∪ dynamic) powers the entity-scoped reuse-first suggestion, but is a
*derived projection plus a mint-into-graph write path* — **not** a competing authority. Entity
concepts + core field concepts are code-seeded (`catalog.py`, the Concept Layer's **core provider**
— ADR 0020 rescoped); dynamic field concepts are minted into the graph at onboarding.

## Empirical validation (2026-06-16, full ERP/MES×3/CMMS/PLM onboard)

- **0 concepts minted today** — onboarding maps to the 54 core concepts or leaves fields unmapped;
  it never creates new ones. ⇒ the dynamic field layer is genuinely new behavior.
- **Large unmapped long tail, esp. ERP (164/229 fields).** Splits into recurring generic/audit
  columns (reuse-first convergence or ignore) and real domain fields — exactly the field-concept gap.
- **Cross-system thread forms on shared entities:** 23 IdentityRules, 8 ValueMappings, 55 JoinPaths.
- Tagging corrections from the data: `operator` mapped by 3 systems → reconsider the operator→
  personnel merge; `site` by 4 → confirms `site` as the place-dimension survivor. (Details in
  the 6-system onboarding validation.)

## Considered Options

- **Flat single-layer vocabulary (status quo).** Rejected: can't drive adaptive tables; conflates
  record-type with field-meaning; forces the hardcoded SQL.
- **Symmetric entity/field layers** (the original 2026-06-16 framing). **Rejected after validation:**
  the data shows entity must be *total* (every table needs a home) while field is *partial/earned*
  (most ERP columns rightly have no field concept). Treating them symmetrically over-mints noise.
- **A separate vocabulary store as the source of truth** (mirroring the code-owned Metric/Card
  layers). Rejected: `MAPS_TO` edges already reference concept identity in the graph, so a second
  authoritative store = two sources of truth = the drift ADR 0020 kills, and it forfeits the
  `Concept←Field←System` traversal reuse-first needs. The Metric/Card-Layer analogy fails a profile
  check (those are stable & code-owned; dynamic field concepts are runtime & relational).
- **Schema-anchored tables without a field layer.** Rejected as a half-measure: adaptive shapes but
  no *shared* field concepts, so systems never link.

## Consequences

- **ADR 0020 rescoped:** code is the single source for the **core** vocabulary; dynamic field
  concepts are graph-resident. No-drift guard stays for the core set.
- The **entity layer must be completed over all tables** (bounded curation; surfaces a few gaps).
  Totality is enforced **fail-loud**, but only over *domain* tables: infrastructure/config tables
  (`system`, `simulation_*`, `*_config`) are an explicit **table-level exclude-set**, and base
  tables behind perpetual-freshness views (`*_raw`) fold to their view's entity. **Dimension/reference
  tables are entities** (e.g. `customers→customer`, `suppliers→supplier`); foreign-key columns
  referencing them (`site_id`, `customer_id`) are structural references that stay unmapped for now
  (relationship modeling comes later). The table→entity assignment uses a **curated map for the closed
  seed systems** (deterministic, testable, guarantees totality) with an LLM-constrained fallback for
  unseen systems — not the LLM alone on the totality-critical primary edge.
- **Dynamic-concept backfill (required for correctness):** because the cross-system thread is built
  from Field→Concept intersection, when an onboard makes an entity newly shared *and* mints a dynamic
  concept, the earlier system's matching columns are re-labeled and resolve re-runs. (Core mappings
  need no backfill — they are written unconditionally on every onboard.)
- **Migration of already-onboarded systems:** the 6 live systems have core Field→Concept mappings
  (thread intact) but **no `SchemaEntry→entity` edges**. A one-time backfill writes entity edges for
  them; since the new edge is not *read* until a later change, this can run with the relationship work but is tracked now.
- Adds an **entity-resolution** capability (render a record list, not just a value) alongside metric
  resolution — same graph/determinism, different output shape.
- Onboarding maps at **two levels** (table→entity primary, column→field) with entity-scoped
  reuse-first suggestion; field-concept CRUD reuses the ADR 0003 proposal/correction lifecycle
  (minting vocabulary is the one new verb).
- The hardcoded `UNIFIED_*_SQL` path is removed; the dashboard becomes fully mapping-driven (the acceptance gate)
  — tabular sections render from `table→entity` + the discovered schema, labeled by earned field
  concepts.

## Related

- ADR 0020 (single source of concept truth — rescoped to the core vocabulary here), ADR 0001 (graph
  owns relationships), ADR 0007 (Concept Affinity — type-level prior, distinct from instance-level
  precedent), ADR 0003 (proposal/correction lifecycle reused for field-concept CRUD), ADR 0008 (the
  Concept Layer could later swap its dynamic store behind the facade).

- [ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md) — extends this model
  to any onboarded system (layered entity inference, `IDENTIFIED_BY` identity edge,
  entity-anchored resolve, non-curated backfill); the curated path and the field-layer
  asymmetric-minting rule here are unchanged.
