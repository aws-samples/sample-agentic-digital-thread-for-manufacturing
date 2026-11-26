<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0057: The semantic-layer export boundary — what ADT can hand to a formal ontology, and what it cannot

## Status

**Proposed — 2026-08-04.** Not accepted; no implementation is authorised by this
document. Written to scope an external contract before code exists, and to give
the positioning work a technically honest floor.

Builds on [ADR-0050](0050-vocabulary-and-artifact-management.md) (the
customization boundary this export reads across),
[ADR-0021](0021-two-layer-concept-model.md) (what the vocabulary actually
contains), [ADR-0020](0020-single-source-of-concept-truth.md) (core vocabulary
is code-seeded), [ADR-0011](0011-canonical-graph-representations.md) (which
representation of each fact is canonical), and
[ADR-0003](0003-unified-proposal-lifecycle.md) (only accepted artifacts are
applied truth).

## Context

Two pressures point at the same missing seam.

**Positioning.** The graph is increasingly described in field conversations as a
*semantic layer* that could feed a formal ontology — the enterprise pattern
built on cataloged lakehouse data, with the ontology materialized in a graph
database for sharing across business units and with outside organizations. That framing is
architecturally reasonable. But the term "semantic layer" appears nowhere in this
repository, while the workshop's `contentspec.yaml` already uses it, so the
codebase and the field are drifting apart on vocabulary with no decision record
either way.

**Interop.** A hand-built spike has demonstrated that an RDF/Turtle file can be
generated from this graph and imported into a graph database. Nothing in the repo
captures what such an export should contain, what it means, or what it omits — so
the first real implementation would invent a customer-visible contract by
accident.

There is a third pressure worth naming: an export is the first artifact this
system would emit for consumption by something outside it. Every other output is
an answer to a question, gated by
[ADR-0046](0046-deterministic-response-evidence-contract.md). An export has no
such gate, and a wrong one is silently wrong in someone else's system.

## Decision

### D1 — Both terms are correct; they answer different questions, and neither is retired

- **"Rules engine, not a data store"** is the **governance** claim: it states
  what is *inside* the graph — metadata about systems, never production rows.
  This is load-bearing for customer trust and is directly verified by the
  workshop's audit exercise. It must not be softened.
- **"Semantic layer"** is the **architectural-position** claim: it states what
  *role* the graph plays — the meaning-and-translation tier between agents and
  systems.

They are compatible because they describe different axes. Documentation should
use the first when the question is "what is in it" and the second when the
question is "where does it sit". Neither term may be used to imply the
capabilities disclaimed in D2.

### D2 — ADT is a semantic layer and deliberately NOT a formal ontology

Stated plainly so that no export is over-sold:

- The graph is a **labeled property graph** with a **fixed edge taxonomy** that
  is engine-invariant ([ADR-0050](0050-vocabulary-and-artifact-management.md)
  D1). Arbitrary relationship types cannot be expressed.
- There is **no class hierarchy, no subsumption, no inference or entailment**,
  and no SPARQL surface.
- Most consequentially: **entity-to-entity domain relationships are not
  modeled.** [ADR-0021](0021-two-layer-concept-model.md) records that
  foreign-key columns referencing dimension tables "stay unmapped for now
  (relationship modeling comes later)". The graph knows that `customer` and
  `production-run` are both entity concepts; it does not represent that a
  production run is *for* a customer order.

This is a deliberate scope choice, not a gap to apologise for — the value
delivered is deterministic cross-system translation over data in place, which
needs none of the above.

### D3 — The export is a projection of the customer-owned node layer, and read-only

[ADR-0050](0050-vocabulary-and-artifact-management.md) D1 already partitions the
graph: node types and their metadata are **customer data**; the edge taxonomy is
**engine structure**. An export is therefore a *read across an already-declared
boundary* and introduces no new customization surface.

In scope for export — the node layer plus the relationships the fixed taxonomy
already gives meaning to:

| Exported | Carries |
|---|---|
| `Concept` (entity + field layers) | Vocabulary: name, description, synonyms, retired flag, provenance |
| `IdentityRule` | Cross-system identifier crosswalks |
| `ValueMapping` | Coded-value translation |
| `AuthorityRule` | Which system is authoritative per concept |
| `JoinPath` | Endpoint tuples (system/table/field) |
| `System` | Identity and `query_guidance` |

Out of scope, and each for a stated reason:

- **Production data.** Non-negotiable. The export must be verifiable as
  metadata-only by the same audit that proves the graph itself is.
- **`Field` / `SchemaEntry` nodes verbatim.** They describe a customer's
  physical schema, not their semantics. Exporting them leaks table and column
  inventory into a document intended for sharing.
- **Pending proposals.** Only `accepted` applied truth is exportable
  ([ADR-0003](0003-unified-proposal-lifecycle.md)). Exporting proposals would
  publish unreviewed machine guesses as asserted fact.
- **Deactivated rules.** Consistent with the reader-filter posture in
  [ADR-0050](0050-vocabulary-and-artifact-management.md)'s T1.4 addendum.
- **Business context.** It is untrusted user-authored prose that never carries
  evidence weight ([ADR-0054](0054-measurement-honesty-and-experiment-harness.md)
  D5). Emitting it into a shared ontology would launder unvalidated text
  across a trust boundary.

### D4 — The export MUST declare its own thinness

Because D2's omissions are invisible in the output format, a consumer receiving
concepts and crosswalks with no relationship assertions cannot distinguish "this
domain has no relationships" from "this exporter does not emit them". The export
therefore carries machine-readable provenance stating the source system, the
graph shape version, the fact that entity relationships are not represented,
and that only accepted artifacts are included.

Rationale: the failure mode of a silent thin ontology is that a downstream team
builds on it, finds the structure missing, and concludes the source was
low-quality rather than intentionally scoped.

### D5 — Direction is outbound-only in the initial scope

Export only. **Import is explicitly not in scope** and should not be added
without its own decision record. An inbound ontology-to-graph path would need to
answer where imported assertions land in the
[ADR-0003](0003-unified-proposal-lifecycle.md) lifecycle, how they interact with
D4 pinning of human assertions, and whether an external ontology may claim
authority — a materially harder problem than emitting a file, and one that
touches the trust invariant directly.

## Considered alternatives

- **Adopt "semantic layer" as the primary term and retire "rules engine."**
  Rejected: "rules engine, not a data store" is the claim the audit exercise
  verifies and the sharpest governance differentiator this architecture has.
  Trading it for a more fashionable term would cost the strongest trust moment
  for no capability gain.
- **Export the full graph including `Field` / `SchemaEntry`.** Rejected: it turns
  a semantics document into a schema inventory, expanding the blast radius of
  sharing it while adding nothing an ontology consumer needs.
- **Model entity relationships now so the export is not thin.** Rejected as a
  prerequisite — that is a substantial change to the concept model with its own
  resolver consequences, and blocking a useful vocabulary export behind it
  delivers nothing sooner. If relationship modeling lands later, the export
  extends; D4 exists so that consumers are not misled in the interim.
- **Emit only when a formal-ontology consumer is confirmed.** Rejected as the
  reason to skip this ADR: the spike already exists, so the contract is being
  defined either way. Better deliberately than by accident.
- **Treat the export as a customer-editable surface.** Rejected: it is a
  projection. Editing belongs upstream in the
  [ADR-0050](0050-vocabulary-and-artifact-management.md) lifecycle, where review
  and pinning apply.

## Consequences

- **Positive:** the vocabulary and crosswalk knowledge — the least
  reproducible thing the system accumulates — becomes portable without weakening
  the metadata-only guarantee. The three-tier story (lakehouse; data in place via
  agents; semantic layer over both) becomes explainable with a concrete artifact
  at the seam. Positioning gains an honest floor: reviewers can point at D2
  rather than negotiating scope in a meeting.
- **Negative / risks:** the resulting ontology is genuinely thin, and D4's
  disclosure is the only thing preventing that from reading as poor quality.
  Adding a first outbound artifact creates a compatibility surface — a shipped
  export shape is hard to change. D1 leaves two terms in circulation, so
  documentation drift is possible and reviewers must apply the axis test.
- **Verification (were this accepted):** an export golden test asserting the
  node-type allowlist and the absence of production values; a test that pending
  and deactivated artifacts are excluded; a provenance-block assertion for D4.
  Reuse the existing audit harness rather than adding infrastructure.

## Open questions

1. Which serialization is the actual target — Turtle for human review, or
   whatever the consuming accelerator ingests natively? The spike produced
   Turtle; that is evidence of feasibility, not a decision.
2. Do `Concept` synonyms map to `skos:altLabel`, or does the consumer expect a
   proprietary vocabulary shape? This determines whether the export is
   standards-shaped or consumer-shaped, and the two diverge.
3. Is there a confirmed downstream consumer with a schema to conform to, or is
   this speculative interop? If speculative, the correct initial scope may be
   narrower than D3.
4. Who owns the exported artifact once it leaves — is it a support burden, a
   point-in-time snapshot, or a subscribed feed? D5 keeps this simple for now by
   ruling out round-trips.
