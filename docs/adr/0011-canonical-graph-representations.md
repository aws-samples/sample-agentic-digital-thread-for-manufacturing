<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Canonical graph representations the resolver depends on

---
Status: accepted
---

Where the onboarding code writes a single fact under **two representations** (two edge
names, or an edge plus a property), the resolver must depend on exactly one. This ADR
names the canonical representation for each such case so the resolver work builds on a
stable target instead of a coin-flip. Surfaced by the graph audit; pins the provisional rows in
`docs/graph-shape-contract.md` §5. Refs ADR 0001 (deterministic resolver), ADR 0002
(relationships live in the graph).

Guiding principle (set in F1): the resolver reads the representation the **authoritative
writer always sets at onboard time**. A second representation written later (lazily, or by
the resolver itself) is a latent silent-miss hazard. The product is unreleased, so
redundant representations are **removed**, not deprecated — one fact, one representation.

## Decision F1 — IdentityRule → Concept: `LINKS_CONCEPT` is canonical; remove `RESOLVES`

Onboarding, self-learning, and repair write `(:IdentityRule)-[:LINKS_CONCEPT]->(:Concept)`.
The resolver instead *read* `(:IdentityRule)-[:RESOLVES]->(:Concept)` and was the only
writer of `RESOLVES`. An IdentityRule created by onboarding and never re-touched by the
resolver was therefore reachable by `LINKS_CONCEPT` but **invisible** to the resolver's
`RESOLVES` read — a silent cross-system join miss (no error, just empty/wrong joins). Not
observable on a lone system (`cmms` has zero IdentityRules); it bites on a ≥2-system onboard.

**Decision:** `LINKS_CONCEPT` is the single canonical `IdentityRule→Concept` (and
`ValueMapping→Concept`) edge. The resolver reads `LINKS_CONCEPT`. `RESOLVES` is removed
entirely — the resolver stops writing it.

- *Rejected — make `RESOLVES` canonical, change onboarding to write it:* the read should
  follow the authoritative writer (onboarding is where IdentityRules are born). Keeping
  `RESOLVES` canonical makes correctness depend on the resolver having previously run —
  exactly the latent-bug condition.
- *Rejected — keep both, resolver reads `LINKS_CONCEPT` and also writes `RESOLVES` as a
  cache:* two edges meaning the same thing is the root cause of the hazard.

## Decision F3 — Authority → Concept: `AuthorityRule.concept` property is canonical; remove the `ABOUT_CONCEPT` edge

`AuthorityRule.concept` (a property) is set at onboard time and is always present. The
`ABOUT_CONCEPT` edge `(:AuthorityRule)-[:ABOUT_CONCEPT]->(:Concept)` is created **lazily**
by the graph-repair routine in `server.py` ~690, **not** at onboard — confirmed absent on
cmms (`about_concept_edge_count=0`). A resolver joining authority via the edge would miss
every freshly-onboarded system until the repair path happened to run.

A code check (design review) found **no reader of `ABOUT_CONCEPT` anywhere** — only the audit
script probes it. The repair routine's real job is to *materialize Concept nodes* from
`AuthorityRule.concept` / `ValueMapping.concept` properties; the `ABOUT_CONCEPT` edge is an
incidental side-effect of one `MERGE`.

**Decision:** `AuthorityRule.concept` (property) is the canonical authority→concept
representation; the resolver reads the property. The `ABOUT_CONCEPT` edge is removed — drop
the single `MERGE (ar)-[:ABOUT_CONCEPT]->(c)` line in `server.py` ~690, keeping the
Concept-node materialization around it. Same rationale as F1: a write-only second
representation with no consumer is pure drift risk.

## Decision F4 — JoinPath endpoints: properties are canonical; keep `FROM_SYSTEM`/`TO_SYSTEM` edges as a consistent index

Each `JoinPath` stores its endpoints **both** as properties (`from_system`/`from_table`/
`from_field`, `to_system`/`to_table`/`to_field`) and as `FROM_SYSTEM`/`TO_SYSTEM` edges to
System nodes (`onboard.py` ~1631/1632). Both are written at onboard and agree on cmms
(5 JoinPaths, property=5, edges 5/5). Unlike F1/F3 this is not a silent-miss-at-onboard
hazard but a **drift** hazard — and the audit noted that for non-FK / cross-system joins
only one form may get written.

**Decision:** the **properties** are the canonical endpoint representation; the resolver
reads them. They carry the full endpoint tuple (system **and** table **and** field) — the
edges only encode the System endpoints and cannot express table/field, so the resolver
needs the properties regardless. The `FROM_SYSTEM`/`TO_SYSTEM` edges are **retained** as a
legitimate secondary traversal index (e.g. "all JoinPaths touching System X"), which the
properties cannot serve efficiently. They must stay consistent with the properties; on any
disagreement, **the properties win**.

- *Rejected — edges canonical:* table/field must still come from properties, so edges
  alone can't drive resolution.
- *Rejected — remove the edges (F1/F3 treatment):* unlike `RESOLVES`/`ABOUT_CONCEPT`, these
  edges are written at onboard and serve real graph traversal — they are a useful index, not
  dead weight.

## Consequences

- `docs/graph-shape-contract.md`: §5 row 9 (identity) becomes a single-edge `LINKS_CONCEPT`
  claim and the `RESOLVES` row in §3 is dropped; §5 row 6 (authority) stands as
  property-canonical and the `ABOUT_CONCEPT` §3 row is marked for removal; §5 rows 7/8
  (joins) record properties as canonical with edges as a consistent index.
- **Code follow-ups (implementation follow-up):**
  1. F1: `resolve.py` (~91) reads `LINKS_CONCEPT`; remove the `RESOLVES` writes (~503/511).
  2. F1: add a ≥2-system onboard case to the audit (`agentic_digital_thread/tests/audit/`) to
     regression-guard cross-system identity resolution live — the only way to confirm the
     fix, since a single system never creates IdentityRules.
  3. F3: remove the `ABOUT_CONCEPT` `MERGE` in `server.py` ~690 (keep Concept-node creation);
     the audit's `ABOUT_CONCEPT` probe can stay as a guard that the edge is not relied upon.
