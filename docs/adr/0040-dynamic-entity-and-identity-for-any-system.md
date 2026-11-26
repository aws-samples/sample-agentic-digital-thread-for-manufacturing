<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Dynamic entity + identity resolution for ANY onboarded system

---
Status: accepted

Related: extends [ADR-0021](0021-two-layer-concept-model.md).
---

## Context

[ADR-0021](0021-two-layer-concept-model.md) established the two-layer concept
model: an **entity layer** (`SchemaEntry-[:MAPS_TO]->Concept`, primary and *total* —
every table maps to exactly one entity concept) and an **earned field layer**
(`Field-[:MAPS_TO]->Concept`). It also anticipated an "LLM-constrained fallback for
unseen systems" on the totality-critical entity edge, and a one-time backfill for
already-onboarded systems.

As built, both the entity edge and the field-minting path are gated to the **seven
curated demo SQL schemas**:

- `entity_for_table(schema, table)`
  ([`concept_tagging.py`](../../agentic_digital_thread/agents/concept_tagging.py)) returns
  `None` for any non-curated / excluded schema and only raises (fail-loud totality)
  for an unmapped *domain* table inside a curated schema.
- The provision entity edge is written only when `entity_for_table` is truthy AND
  `connection_type == 'PostgreSQL'`
  ([`onboard.py`](../../agentic_digital_thread/agents/onboard.py) `_do_provision`).
- Cross-system identity is discovered purely from **field-concept intersection** on
  `role='identifier'` (`_do_resolve`).

Consequence: an Athena or AWS IoT SiteWise table gets **field mappings but no entity
edge and no explicit identity key**, so a novel system cannot reliably join the
digital thread — the entity anchor and the join key it needs are both absent. The
graph-shape contract that resolve relies on is only partially populated for
non-curated systems.

We want the ADR-0021 model to work for **any** onboarded system while keeping the
curated deterministic path **byte-identical** (its fail-loud totality is load-bearing
and eval-covered), and while preserving import purity (ADR-0020/0021): no
`pg8000`/`boto3` at import in `concept_tagging.py` / `concept_layer.py`, graph reads
only through the lazy injected `neptune_query` seam.

## Decision

Layer a new **impure inference seam** on top of the unchanged pure curated path, and
carry the chosen `(entity, identity_fields)` per table through the provision payload
into two graph edges that `resolve` then consumes. Six decisions are locked:

### D1 — Entity inference is layered: curated fast-path → heuristic → LLM/mint

A NEW impure resolver (in `onboard.py`, the impure layer) resolves a table's entity
in strict order, first hit wins:

1. **Curated fast-path.** `entity_for_table(schema, table)` stays **pure and
   unchanged** as the deterministic precedent for curated schemas. If it returns a
   concept, use it verbatim — curated behavior is byte-identical.
2. **Pure heuristic.** Otherwise `concept_layer.entity_suggestions(field_concepts,
   system_type, table_name)` ranks `ENTITY_CONCEPT_IDS` by precedent (entity concepts
   other SchemaEntries map that co-occur with the same field concepts — one graph
   read), type affinity (ADR-0007), and table-name match. A confident top candidate is
   taken.
3. **LLM classify, constrained.** Otherwise a single `bedrock_call` classifies over
   `sorted(ENTITY_CONCEPT_IDS) + ["__MINT__"]` using the table name, field names, and
   sampled values.

The LLM is **never** the authority on the totality-critical curated edge — it only
acts for genuinely novel tables, matching ADR-0021's "curated map … LLM-constrained
fallback for unseen systems".

### D2 — Entity assignment is TOTAL; minting a NEW entity concept is the fallback

Every onboarded table must resolve to **exactly one** entity — unlike field minting,
which is earned/gated (ADR-0021 asymmetry, unchanged for the field layer). Reuse an
existing `ENTITY_CONCEPT_IDS` concept whenever one fits. **Mint a new entity concept
only** when the LLM confidently rejects every existing entity — `__MINT__` with
**confidence ≥ 0.7**. Minted entity concepts are written `source='minted'`,
`layer='entity'` (distinct from field mints, which are field-layer and shared-entity
gated). **Fail-soft:** if inference cannot decide, the table is left
**entity-unassigned** (its Field nodes are still written) rather than committing a
wrong edge — a wrong entity pollutes the thread, an absent one is recoverable.

### D3 — Identity key = one or more dedicated `SchemaEntry-[:IDENTIFIED_BY]->Field` edges

A table's identity key is represented as one (or, for composite keys, several)
`SchemaEntry-[:IDENTIFIED_BY]->Field` edges. This is cleaner than overloading
`MAPS_TO` properties, supports composite keys, and is independently queryable. The
existing `role='identifier'` on `Field-[:MAPS_TO]->Concept` is **untouched** for
backward compatibility; `IDENTIFIED_BY` is **additive and authoritative** for
identity. Candidate keys are proposed by the pure
`concept_layer.identity_field_candidates(columns, mappings)`: an `role='identifier'`
column wins; else a business-key name (`*_id`/`*_number`/`*_code`/`barcode`/`serial`,
EXCLUDING the bare surrogate `id`); else none.

### D4 — resolve consumes entity + identity

`_do_resolve` gains an **entity-anchored candidate pass** after the existing
shared-field-concept pass: two SchemaEntries mapping the SAME entity concept →their
`IDENTIFIED_BY` fields are join-key candidate pairs, sampled and overlap-measured
exactly like today's shared-concept path, and fed into the SAME single rule-proposal
`bedrock_call` (no second LLM call, same 60-field cap). This lets a novel table join
on the ENTITY match even when its identity field's own field-concept mapping is
weak/absent. `resolve_entity` ([`resolve.py`](../../agentic_digital_thread/agents/resolve.py))
gains an entity-edge fallback lookup for the same reason.

*Amendment 2026-10-17 (declared related concepts):* the shared-field-concept
pass also measures a pair whose two concepts the core vocabulary declares the
same kind of thing at different breadth (`catalog.NARROWER_CONCEPTS`, today
`operator` within `personnel`). The field mapper can file one person column
under either concept, so the pass paired a CMMS log author with the MES
`resolved_by` it copies in some runs and not others, and the identity rule
came and went with the mapper's word choice. Such a pair is sampled,
overlap-measured and shown to the model (marked as declared related) like a
same-concept pair. The relation decides only what is MEASURED: the evidence
gates (`resolve_gates`) and the site gate (ADR-0051 P8) still decide every
rule, a pair with no overlap adds no evidence, and coverage-gap
recommendations stay keyed to same-concept measurements. The relation is
symmetric and not transitive. A prompt with no related pair is unchanged.

### D5 — MAP UI submits two new per-table pieces

The field→concept checkpoint gains, per table, an **entity-concept `<select>`**
(options = `ENTITY_CONCEPT_IDS` + "＋ mint new…") and an **identity-key `<select>`**
(options = the table's columns, pre-selected to the inferred key), both with
confirm/override. They are submitted as a new **`table_entities`** array on the
provision payload (each item `{"table","entity","identity_fields","mint"}`, `mint` =
`{"id","name","description"}` when a new entity concept is chosen). The same array is
carried into `OnboardResolveRequest` so resolve reads UI-confirmed entities
order-independently.

*Note 2026-10-04:* in the React UI these per-table selects (and the Wave 4
per-table notes) only ever rendered on the catalog's Re-map screen, which was
removed because its Save ran the full, destructive re-provision of an
onboarded system. No UI sends `table_entities` or `table_notes` now; the
provision and resolve requests still accept both, and the curated fast path
and the dynamic resolver (D1-D4) are unchanged.

### D6 — Backward-compat / migration is additive

- **Curated schemas:** the fast-path (D1.1) makes the dynamic resolver byte-identical
  to today — no migration, existing entity edges unchanged.
- **Already-onboarded NON-curated systems** (Athena / SiteWise): a one-time,
  idempotent **additive backfill** (`scripts/backfill_entity_identity.py`) writes their
  entity + `IDENTIFIED_BY` edges. It only adds net-new edges; nothing is removed, and
  curated systems that already have edges are skipped.

## Considered Options

- **LLM alone on the entity edge.** Rejected: the entity edge is totality-critical and
  eval-covered on curated schemas; an LLM regressing it would silently break the
  thread. The curated fast-path guarantees byte-identical curated behavior.
- **Overload `MAPS_TO` with an `identity` property instead of a new edge.** Rejected:
  worse for composite keys and less queryable; conflates the entity mapping with the
  key. `IDENTIFIED_BY` is additive and leaves `role='identifier'` intact.
- **Force-assign a best-guess entity when inference is unsure.** Rejected: a wrong
  entity edge actively pollutes cross-system joins, whereas an absent one is
  recoverable (fail-soft, D2) and re-runnable via backfill (D6).
- **A second LLM call for entity-anchored resolve candidates.** Rejected on cost:
  entity-anchored pairs feed the existing single rule-proposal call (D4).

## Consequences

- Novel (Athena/SiteWise/custom) systems get an entity anchor and an explicit identity
  key, so they join the digital thread the same way curated systems do.
- The entity edge is now written for **all** systems (the `PostgreSQL`-only guard is
  removed for the entity edge); field-concept **minting** stays gated exactly as
  ADR-0021 specified (shared-entity earned).
- New graph shape: `SchemaEntry-[:IDENTIFIED_BY]->Field`, plus minted entity concepts
  carrying `source='minted'`, `layer='entity'`.
- Import purity is preserved: all inference/LLM lives in `onboard.py` (impure); the new
  pure helpers in `concept_layer.py` read only through the injected `neptune_query`
  seam; `concept_tagging.py` `entity_for_table` is **not modified**.
- Human-in-the-loop safety: the MAP UI confirms/overrides both the entity and the
  identity key before provision, bounding LLM misclassification (D5), backed by the
  confidence threshold and fail-soft (D2).
- Additive migration: curated environments need none; already-onboarded non-curated
  systems are backfilled once (D6).

## Related

- [ADR-0021](0021-two-layer-concept-model.md) — the two-layer concept model this ADR
  extends to any system (curated path unchanged; entity totality preserved; field-layer
  asymmetric-minting framing intact).
- [ADR-0020](0020-single-source-of-concept-truth.md) — import purity / single source of
  concept truth; the new inference seam keeps `concept_tagging.py`/`concept_layer.py`
  import-pure.
- [ADR-0007](0007-concept-system-type-affinity.md) — Concept Affinity, the type-level
  prior used by `entity_suggestions` (distinct from instance-level precedent).
- [ADR-0003](0003-unified-proposal-lifecycle.md) — the proposal/correction lifecycle
  reused for entity-concept minting and identity-key confirmation.
- [ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) — value-overlap
  evidence reused by the entity-anchored resolve pass.
- [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md) — the Neptune graph
  is VPC-private, so graph behavior (including the new edges) is verified against a
  deployed environment; offline tests inject the graph seam.
