<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Evidence-based concept suggestion at map time (value-overlap classifier)

---
Status: accepted
---

## Context

Onboarding maps a source system's columns to the shared core concepts in two
steps that run at different times:

- **Map** — a per-column semantic LLM pass proposes a concept + role +
  confidence for each column. Columns the model cannot place land as
  `needs_review` (or below the `0.4` confidence floor). These are dropped by the
  downstream resolve filter, so an unmapped column never participates in
  cross-system linking.
- **Resolve** — after mapping, the resolve step MEASURES value overlap between
  fields that already share a concept across systems
  ([`agents/resolve_evidence.py`](../../agentic_digital_thread/agents/resolve_evidence.py)),
  turning an LLM hunch into evidence.

The gap: resolve's measured evidence can only act on concepts the map step
*already agreed on*. A column the semantic pass leaves `needs_review` never
reaches a shared-concept state, so it is invisible to resolve — even when its
actual VALUES are a dead ringer for a concept a peer system already established.
Live onboarding showed exactly this: a source's `cell` column (values `SITE1`,
`SITE2`, …) never mapped to any concept, so the `site` value population a peer
system exposed could never be bridged, despite the values overlapping perfectly.

Encoding `cell → site` as a name synonym is the wrong fix: it is a
dataset-specific naming assumption (a "cell" is elsewhere a work-center-level
location BELOW site — see [`catalog.py`](../../agentic_digital_thread/agents/catalog.py)
`CORE_CONCEPT_DEFINITIONS`), and it would not generalize to the next source's
naming.

## Decision

Apply **resolve's value-overlap evidence at MAP time** as a generic classifier
for UNCERTAIN columns. New module
[`agents/concept_suggest.py`](../../agentic_digital_thread/agents/concept_suggest.py),
wired into `onboard._run_map_job` right after the per-field `needs_review`
decision.

### Mechanism

1. **Gather peer concept populations.** One bounded graph query for every peer
   field→concept mapping
   (`MATCH (s:System)-[:HAS_SCHEMA]->()-[:HAS_FIELD]->(f:Field)-[:MAPS_TO]->(c:Concept) WHERE s.id <> $sid`,
   the spine guaranteed by
   [docs/graph-shape-contract.md](../graph-shape-contract.md) §4a), then sample
   each field's values (bounded) and accumulate a per-concept value population,
   cached.
2. **Classify each uncertain column.** Sample the column's values (bounded);
   MEASURE `value_overlap` against every peer concept population. When the
   overlap is STRONG and non-coincidental — at least `_MIN_DISTINCT_SUPPORT` (2)
   distinct shared tokens AND a size-normalized ratio ≥ `_MIN_OVERLAP_RATIO`
   (0.5) — propose that concept with an evidence-backed confidence and a `basis`
   annotation carrying the measured metric and contributing peer systems.
3. **Rank candidates, basis-tagged.** Produce a ranked `candidates` list per
   uncertain column, each tagged `semantic` | `isa95_synonym` | `value_overlap`.
   Strong value-evidence outranks a weak semantic guess (prefer measured evidence
   on disagreement); the weak semantic guess is still surfaced. On strong
   evidence the column's proposed concept is upgraded and tagged
   `suggestion_basis='value_overlap'`.

### Why it is generic (no hardcoding)

The classifier reaches `cell → site` **purely because the column's values overlap
the population a peer already mapped to `site`** — zero name assumptions, no
dataset literal. The same code bridges any source's coded column to any concept a
peer established, in either naming direction, because the only signal it consumes
is measured value overlap. The `isa95_synonym` candidate source is likewise
generic: it reads the `Synonyms (…)` clause already present in the catalog
concept definitions (no per-source literal).

### DRY / relationship to the resolve step

The heavy value logic is **not** re-implemented: `concept_suggest` reuses
`resolve_evidence.value_overlap` (token extraction + measured overlap) and
`resolve_evidence.sample_field_values` (multi-source bounded sampling) verbatim,
inheriting their caps. This is deliberately the SAME evidence resolve uses — only
applied one step earlier so that a `needs_review` column can *become* a shared
concept and thereby reach the resolve step at all.

### Bootstrapping

When no peer concept populations exist yet (the first system onboarded, or peers
whose values could not be sampled), the classifier is a **no-op** and mapping
falls back to the pure semantic result. It only adds value once ≥1 peer has
established populated concepts.

### Cost gating

- Gated to **uncertain** columns only (`needs_review` or confidence < 0.4) — a
  confident column is never sampled.
- Gated to **identifier-ish/coded** columns — free-text and raw timestamps are
  skipped, both to avoid coincidental overlap and to avoid sampling large text
  columns.
- Peer populations are gathered **once and cached** per map job; every sample
  call is bounded by `resolve_evidence`'s existing caps; per-concept population
  size is capped (`_PEER_VALUE_CAP`).
- The entire step is **best-effort**: any IO error leaves the semantic result
  untouched (mapping never fails because suggestion failed).

### False-link safety

Strong measured evidence produces a strong **suggestion** — an upgraded proposed
concept plus a `basis` annotation and a ranked `candidates` list — never an
auto-committed hard cross-system identity link. The evidence confidence is capped
below `1.0` (`_EVIDENCE_CONF_MAX = 0.85`) so it never reads as a certainty. The
actual identity link is still formed and confirmed **downstream in the resolve
step**, exactly as before. A weak/coincidental overlap never upgrades the
concept; it can only appear as a lower-ranked candidate.

## Consequences

- Columns the semantic pass leaves uncertain can now be rescued by data evidence,
  so more genuine cross-system links become measurable at resolve time — without
  any dataset-specific naming rules.
- The map proposal payload gains an optional per-column `candidates` list (and,
  on upgrade, `suggestion_basis`); the result-dict shape is unchanged (the
  additive `expected_unmapped` contract in
  [ADR-0007] and the `{"mappings"}` key set are untouched).
- Adds one graph query + bounded sampling to the map job when uncertain
  identifier-ish columns exist and peers are present; a no-op otherwise.
- Verified offline with synthetic-only unit tests
  ([`tests/unit/test_concept_suggest.py`](../../agentic_digital_thread/tests/unit/test_concept_suggest.py)):
  overlapping column → suggested with `basis=value_overlap`; non-overlapping →
  not suggested; free-text/timestamp column → skipped; no peers → no-op; ranked
  ordering. Live confirmation deferred (offline lane only).
