<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Concept↔system-type affinity drives mapping recommendations, separate from authority

---
Status: accepted
---

Map-time mapping recommendations are driven by a **concept↔system-type affinity** — a
many-to-many prior of which concepts a system *type* typically supplies (e.g. an MES
plausibly has `production-run`, `oee`, `downtime`, `quality-metric`). This is kept
distinct from `CONCEPT_AUTHORITY`, which is 1:1 and decides *who wins* for a concept at
provision time.

## Context

Authority answers "who is authoritative" (MES owns `downtime`). Recommendations need
"who plausibly has it" (a CMMS also records `downtime`). Different questions.

## Considered Options

- **Reuse `CONCEPT_AUTHORITY` for recommendations:** rejected. Being 1:1, it under-suggests
  — it would never prompt a CMMS operator to map `downtime` because MES is authoritative —
  systematically missing legitimate mappings.

## Consequences

- A second small lookup (affinity) to maintain, and the discipline to keep "plausibly has"
  and "is authoritative for" distinct so they don't re-merge.
- The affinity is also the input to the onboarding mapping guidance (ADR 0006, option 2).

## Implementation refinement

The design review of 2026-06-15 locked how the affinity is realised:

- **Shape / home.** `CONCEPT_AFFINITY: dict[system_type, frozenset[concept]]` lives in
  `catalog.py` beside `CONCEPT_AUTHORITY` (the two static priors stay adjacent but named
  distinctly). The natural authoring unit is `system_type → concepts` ("what an MES has");
  the inverse `system_types_for(concept)` is a derived helper. **Import-time invariants
  (fail-loud):** every concept id ∈ `CORE_CONCEPT_DEFINITIONS`, **and `authority ⊆ affinity`**
  — for every `CONCEPT_AUTHORITY[c] = T`, `c ∈ CONCEPT_AFFINITY[T]`. The authoritative system
  by definition plausibly has the concept, so affinity is a guaranteed superset of authority
  and the two cannot silently drift or re-merge. Data is hand-authored **from meaning**, never
  from observed LLM output (ADR 0012 integrity rule).

- **Consumers (deterministic only).**
  1. `coverage.suggestion_for(concept)` is enriched to name the plausible types
     ("Onboard a CMMS or MES — typically provides X"), with a generic fallback when a concept
     has no affinity types. Returns `str`; the `/api/coverage` gap shape is unchanged (no UI
     contract change).
  2. A pure `missing_expected(system_type, mapped_concepts)` surfaced at the **mapping-proposal
     stage** as an additive, non-blocking `expected_unmapped` hint ("an MES typically also
     provides downtime, quality-metric — not mapped"). It is a completeness check over the
     *proposed* mappings; it does **not** change what is mapped and touches no LLM/control flow.
     An optional read-only per-system endpoint may expose the same diff.

- **Deferred: affinity → the mapping recommendations themselves.** Feeding the affinity into
  the onboarding **LLM prompt** (so the model proposes better mappings) is the higher-value but
  nondeterministic path. It is deferred because we cannot yet *regression-test that it improves
  mapping quality* — the mapping-quality eval (ADR 0012) is non-gating and cloud-only, and the
  offline/deterministic graph lane is deferred. Revisit once that eval can gate CI.

- **Flag.** All affinity behavior sits behind a single `RESOLVER_AFFINITY` flag, **default off**
  (core byte-identical: generic suggestions, no `expected_unmapped`, endpoint unmounted). The
  flag is enabled in the compute stack at deploy-time, alongside `RESOLVER_REDERIVE` /
  `RESOLVER_COVERAGE`, after the integrated verification. Pure helpers stay pure — the gate is
  applied at the call-site boundaries.
