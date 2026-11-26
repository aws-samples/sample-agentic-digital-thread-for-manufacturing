<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Mapping quality is validated by eval against authored ground truth

---
Status: accepted
---

The digital thread's core value is **semantic mapping**: heterogeneous, realistic source
fields (`spare_parts.quantity_on_hand`, `qty_on_hand`, `inv_count`, …) get mapped to canonical
`Concept`s. Tests must prove that mapping is *correct*, not merely *present*. But the mapping is
LLM-produced and non-deterministic, so exact-match assertions on raw output are brittle, and
structural assertions ("≥1 `MAPS_TO` exists") prove nothing about quality. Surfaced while
planning the integration harness. Refs ADR 0001 (deterministic resolver consumes the
mappings), ADR 0011 (canonical graph shape the eval reads).

## Decision

Validate mapping quality as an **eval against a ground-truth key authored from the seed data's
real-world meaning**, with thresholds rather than equality:

- **Ground-truth key, authored from domain semantics, blind to LLM output.** Alongside each
  seed schema we maintain an expected-mapping key (field → one or more acceptable concept ids). The key
  encodes what the fields *mean*, authored independently of what the model produced.
- **Per-field acceptable set, not a single concept.** A field may legitimately map to one of
  several defensible concepts; only a mapping outside the set is a real miss.
- **Two-level threshold.** A small **must-map tracer set** (the fields the demo narrative
  depends on) is asserted at **100%**; an **overall accuracy floor** covers the rest. A
  **confidence gate** applies to the must-map set.
- **Name-independence generalization.** To prove the mapper keys on *meaning* and not memorized
  field names, a synonym-field variant of a schema (different column names, same concept
  targets) must also clear the thresholds.

## Integrity guardrail

The ground-truth key MUST be authored from domain meaning, **never** by transcribing the
model's output. A key written from observed LLM output is a circular, self-fulfilling eval and
is explicitly disallowed.

## Considered Options

- **Exact-match on LLM output:** rejected — brittle and flaky against non-deterministic mapping;
  would penalize legitimate-but-different concept choices.
- **Structural-only (`≥1 MAPS_TO`, status onboarded):** rejected — green even when the mapping
  is wrong; tests none of the actual value proposition.
- **Eval with authored ground truth + thresholds (chosen):** robust to nondeterminism, catches
  real regressions, and the score doubles as a demo artifact ("mapped 28/30 fields correctly,
  all critical ones exact").

## Consequences

- Mapping evals run in the cloud WARM/HOT loop, **not** as a gating CI check (consistent with
  the offline/deterministic lane being deferred); threshold flakiness is not a
  release blocker.
- The eval score is a **demo artifact**, not just a test result.
- Expected-mapping key files are maintained **alongside** the seed data; drift between seed and
  key is a maintenance cost we accept.
- The generalization variant adds a synonym-field seed schema + a catalog system + an onboarding
  run (heavier; sequenced accordingly).
