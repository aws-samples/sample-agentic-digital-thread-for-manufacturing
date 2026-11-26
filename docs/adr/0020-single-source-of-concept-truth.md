<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Concept vocabulary has a single source of truth (catalog.py), the graph seed derives from it

---
Status: accepted
---

_Rescoped by ADR 0021: this single-source rule governs the **core** vocabulary; the two-layer
model adds **dynamic field concepts** minted into the graph at onboarding (graph = source of
truth for concept identity; `catalog.py` is the core provider). The no-drift guard stays for the
core set._

The canonical manufacturing concept vocabulary — which concepts exist and their full
metadata (`id`, `name`, `isa95_level`, `description`, `source`) — lives in **one place,
`agents/catalog.py`**. Every consumer (the onboarding mapping prompt, the resolver, coverage,
affinity, the architecture visualization) and **every graph-seed path** (the one-script
`deploy.sh`, `graph_reset`, the app's reset handler) derives from it. The hand-maintained
`data/neptune/core.cypher` is **removed**.

## Context

A feedback-loop investigation (the live `mro-inventory` mapping miss, 2026-06-15) exposed that
the concept vocabulary had **two divergent sources**:

- `data/neptune/core.cypher` — 68 `MERGE (:Concept …)` lines, **read by `deploy.sh [2/6]`**
  and `graph_reset` direct mode. Rich (`name`, `isa95_level`, `source`, `description`) but
  **stale**: missing 7 current concepts (`mro-inventory`, the OEE set `availability`/
  `performance`/`oee-score`, `food-safety`, `tool-life`, `payment`), retaining ~14 dropped
  ones, and its `spare-part` description **wrongly listed `quantity_on_hand`/`reorder_point`**
  (which belong to `mro-inventory`) — actively teaching the wrong measure-vs-entity boundary.
- `catalog.py CORE_CONCEPT_DEFINITIONS` — 54 concepts, **`id → description` only**, used by the
  app (prompt/resolver/coverage/affinity + the endpoint reset handler). Current, but lacks
  `name`/`isa95_level`.

So a COLD-deployed graph got the stale 68; an endpoint reset got the current-but-poorer 54.
Two sources, guaranteed to drift. This is **our tooling defect**, not realistic customer data
(ADR 0019 demo philosophy): the source-system seed data stays deliberately messy; the concept
vocabulary is ours and must be clean and singular.

`isa95_level`/`name`/`source` are not vestigial — they are consumed by the ISA-95 architecture
view (`graph_analytics.py`) and the **core-vs-emergent cleanup** (`onboard.py:180` deletes
concepts with `isa95_level IS NULL AND source IS NULL`), so the single source must carry them.

## Decision

- **`catalog.py` is the single source.** Enrich the concept definitions to carry the full model
  (`name`, `isa95_level`, `description`; `source='core'` implied for all catalog concepts) —
  either by widening `CORE_CONCEPT_DEFINITIONS` or via adjacent maps; the implementer chooses the
  lowest-reader-migration form (the `id` key set must stay stable so existing readers and the
  ADR-0007 `authority ⊆ affinity` invariant keep working). `isa95_level` for the current concepts
  is migrated from `core.cypher`; the 7 new ones are assigned by meaning.
- **One seeder.** A single routine emits the canonical full Concept `MERGE`s from the catalog;
  `deploy.sh`, `graph_reset`, and the app reset handler all use it (DRY — no three near-copies).
- **Delete `data/neptune/core.cypher`.** No second source, generated or hand-maintained.
- **No-drift guard (test).** A unit test asserts the seeder/seeded concept id-set equals
  `CORE_CONCEPT_DEFINITIONS` exactly, and that every concept carries `name`/`isa95_level`/`source`.
- The `spare-part` description pollution disappears for free: catalog's `spare-part` already omits
  `quantity_on_hand`, and `mro-inventory`/OEE concepts are present — so unifying on catalog *is*
  the data fix.

## Considered Options

- **Generate `core.cypher` from catalog (committed artifact + guard).** Rejected: still a second
  (derived) file, against "no redundant/contradictory sources"; `deploy.sh` would keep a
  file-read path. Direct seed-from-catalog is simpler and leaves nothing to drift.
- **Make `core.cypher` the source, fix its membership.** Rejected: the app logic, prompt, and
  resolver already live on `catalog.py`; hand-maintained cypher is the harder thing to keep
  correct, and it is not where the code reads concepts.

## Consequences

- One concept model, impossible to drift; a fresh COLD deploy now seeds the *current* vocabulary
  (incl. `mro-inventory` + the OEE set) with correct `isa95_level`/`name`.
- A wider but mechanical reader migration (everything reading `CORE_CONCEPT_DEFINITIONS`).
- `deploy.sh`'s seed step imports the app catalog at deploy time (the venv is active; the app
  already imports it) — verified by re-running the seed snippet.
- Industry concept-packs (`/api/admin/concept-packs`, separate pack files) are unaffected — this
  ADR governs only the **core** vocabulary.

## Related

- Pairs with a mapping-prompt disambiguation change (measure-vs-entity, attribute-vs-entity) —
  the other half of the same feedback-loop fix. Both target "default onboarding just-works on the
  messy seed (must-map@100% + overall ≥ 0.90), corrections as the safety net" (ADR 0012 eval is
  the calibration gauge).
- ADR 0019 (demo philosophy: fix our vocabulary/capability, keep source data realistically messy).
- ADR 0007 (`authority ⊆ affinity` invariant keys off the same `CORE_CONCEPT_DEFINITIONS`).
