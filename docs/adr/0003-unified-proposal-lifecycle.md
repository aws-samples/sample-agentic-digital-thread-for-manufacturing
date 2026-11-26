<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Unified proposal lifecycle for corrections, with conflict policy

---
Status: accepted
---

Both agent-discovered and user-initiated corrections flow through one proposal
lifecycle (`pending → accepted / rejected / superseded`). DynamoDB is the proposal log,
lifecycle, and audit trail; Neptune holds the applied truth. User corrections enter the
lifecycle already `accepted` (source: user); agent proposals enter `pending`.

## Considered Options

- **Direct Neptune writes for user edits:** rejected — produces two correction paths and
  two audit models, and would need re-unifying shortly.

## Consequences

- One audit trail and one re-derivation trigger: any accepted proposal invalidates the
  affected metrics and the resolver re-runs.
- Proposals must be keyed by a **canonical relationship key** (not a per-instance id)
  for idempotency. Collision policy:
  - exact duplicate `pending` → accept the existing proposal rather than insert
  - exact duplicate already `accepted` → no-op
  - exact duplicate previously `rejected` → allow, but surface the rejection history
  - same subject, different object → **supersede** (new wins; prior kept as superseded)
- Conflict detection is at the *subject* level; idempotency is at the *full-target*
  level. The two are needed together.
- Today proposals are keyed by a random `mapping_id`; adding the canonical key as an
  attribute + GSI (lookup-before-write) is the least-disruptive path to this.

## Refinement (design review, 2026-06-12)

The decision above predates confronting the as-built `self_learning.py`, where proposals
come in **four** `proposed_type`s (`value_mapping`, `join_path`, `identity_rule`,
`field_annotation`). The following sharpen it for implementation.

### A. The canonical key is type-aware and two-grained

A pure, deterministic, normalized (lowercased/trimmed) function computes two keys per
proposal:

- **`subject_key`** = `proposed_type` + the left-hand side (the part that can have only one
  right answer) — governs conflict/supersede:
  - `value_mapping` → `(from_system, from_field, from_value)`
  - `join_path` → `(from_system, from_field)`
  - `identity_rule` → `(from_system, from_field, concept)`
  - `field_annotation` → `(from_system, from_field)`
- **`target_key`** = `subject_key` + the right-hand side (`to_system, to_field, to_value` /
  annotation digest) — governs idempotency.

### B. One new GSI on `subject_key`

A single subject-key-partitioned GSI (sort `discovered_at`) serves both jobs: a query
returns all prior proposals for a subject; an exact `target_key` match is the idempotency
branch, a differing `target_key` is the supersede branch. The existing `status-index` GSI
is retained for the Review UI.

### C. Supersede is source-aware (user truth is protected)

Proposals carry a `source` (`agent` | `user`). A **user Correction** (enters `accepted`)
supersedes anything on its subject — user wins. An **agent** proposal on a subject that
already has an `accepted` **user** Correction does **not** auto-supersede it; it is recorded
(`pending`) and surfaced as disagreement rather than silently overwriting human truth.
Agent-over-agent on the same subject is normal supersede (latest wins; prior → `superseded`).
Rationale: a corrections loop whose agent can clobber explicit human corrections destroys
the trust it exists to build.

### D. Applied-node identity = subject key (the version invariant)

The graph holds **exactly one applied mapping per subject**. The applied Neptune node's
identity is the canonical **subject key**, not the random `mapping_id` — so accept/supersede
**MERGE-upserts the same node in place** (`SET` new props + `version = prior + 1`). This
folds out the as-built `MERGE (... {id: "vm-learned-" + mapping_id[:8]})` keying, which would
otherwise leave a stale node beside the new one and let the resolver match **both** and
double-read. Superseded versions are **not** competing graph nodes — they live only as
history rows in the DynamoDB log. The resolver therefore never selects a version: it reads
the single live node and its `version` property is provenance, not a selector.

### E. Version closes the lineage-record seam

`version = 1 + (count of superseded predecessors on the subject)` — a monotonic integer that
falls out of the subject-key GSI query. Written onto the applied node at accept-time.

**Two disjoint mapping families (found while planning).** The resolver reads the **onboarding
`MAPS_TO` edge** (`(f:Field)-[r:MAPS_TO]->(c:Concept)`) — *not* the self-learned
`ValueMapping`/`JoinPath`/`IdentityRule` nodes, which are off the resolve path and serve the
orchestrator's cross-system exploration. So lineage's two version fields version different
things: `mapping_version` ← the authoritative `MAPS_TO` edge the resolver reads;
`value_mapping_version` ← the self-learned value-mapping family (no resolve-time read site).

Therefore this change closes **`mapping_version` end-to-end**: onboarding stamps
`r.version = coalesce(r.version, 1)` on each `MAPS_TO` edge it creates (re-onboard-safe), and the
resolver reads `r.version` into `MappingProvenance.mapping_version` (additive, absent → `None`).
The self-learned applied nodes are independently versioned by the supersede chain above (their own
provenance). `value_mapping_version` stays an explicit deferred seam — it has no resolve-time read
site until/unless the resolve path consumes value mappings. The `MAPS_TO`-*correction* that bumps
`r.version` via a human edit is deferred to the corrections-loop work (lineage-view correction).

### F. Fail-loud on the correctness-critical path

`log_proposed_mapping`, `record_user_correction`, the key lookup, and accept/supersede
**raise** on DynamoDB/Neptune failure rather than logging-and-returning success — a silent
GSI-query failure would degrade lookup-before-write to "always insert," defeating the slice
invisibly. Bulk admin helpers (`clear_*`, `reset_*`, `accept_all_pending`, read-only `get_*`)
are left as-is to bound blast radius; the orchestrator's fire-and-forget discovery call gets a
thin guard that logs loudly without mistaking failure for success.

### Scope and deferrals

**In scope here:** the canonical key, the subject-key GSI, lookup-before-write, the full collision/
supersede policy, the `source` field, the `superseded` state, `record_user_correction()`
(enters `accepted`, applies to the graph immediately — exercised by tests), the subject-key
applied-node re-keying, and version population. Fully offline-testable.

**Deferred (do not lose):** the lineage-view "correct this mapping" route + UI and the
re-derivation / invalidation / SSE ripple on accept → **deferred** (ADR 0005);
field-level authority override → future (CONTEXT Authority); broad fail-loud cleanup of the
bulk admin helpers → optional later.
