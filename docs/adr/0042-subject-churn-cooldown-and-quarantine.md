<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Subject-level churn control: supersede cooldown, flip-flop quarantine, rejection damping

---
Status: accepted

Related: refines [ADR-0003](0003-unified-proposal-lifecycle.md); grain unchanged.
---

## Context

[ADR-0003](0003-unified-proposal-lifecycle.md) keys `join_path` proposals by
the subject `(from_system, from_field)` and resolves same-subject/different-
target collisions by **unbounded supersede** — latest agent proposal wins,
prior row → `superseded`. The 2026-07-14 audit showed what that does under a
rediscovery loop: `discover_joins_for_system` re-runs on every resolve, target
choice flips between plausible candidates, and one subject
(`mes-mil work_orders.reject_quantity`) accumulated **53 supersedes** of pure
churn. Nothing damps the loop: superseded rows are never consulted, and
rejection history (`prior_rejections`, written by
`insert_with_rejection_history` per ADR-0003's "surface the rejection history"
policy) has **zero consumers** — a human can reject the same discovery every
day forever.

The tempting fix — widen the subject grain to include the target — is wrong
twice over: it abolishes supersede semantics entirely (every target becomes
its own subject, so nothing ever supersedes), and it orphans/splits every
existing queue row keyed at the current grain, forcing a DynamoDB migration.

The goal state (from the capture-at-onboarding session): churny subjects are
**held for human review with a stated logical reason**, not silently
thrashed; and rejected discoveries stop re-appearing on a treadmill.

## Decision

The `join_path` subject grain `(from_system, from_field)` is **KEPT**. Churn
is damped at the lifecycle level, in `_classify_collision`, with three new
**agent-only** branches — user-source proposals are untouched and still win
unconditionally (ADR-0003 C):

### D1 — Supersede cooldown

An agent proposal that would supersede on a subject whose most recent
supersede happened within `SUBJECT_SUPERSEDE_COOLDOWN_HOURS` (default 24,
env-overridable) is recorded as a dup-style no-op: no new row, no supersede,
the existing active mapping id is returned. Rationale: two contradictory
"discoveries" inside one day are rediscovery noise, not new evidence.

### D2 — Flip-flop circuit breaker (quarantine)

When a subject's supersede chain reaches
`SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD` (default 3) distinct
targets/versions, the incoming proposal is **not** allowed to supersede.
Instead it is stored as a `pending` row with `quarantined="true"` and a
stated `review_reason` naming the evidence:
*"flip-flopped K times between targets X, Y — adjudicate."* Prior active rows
are NOT superseded. From that point the subject is **frozen**: further agent
proposals on it (any target) also classify as quarantine, until a human
accept/reject resolves the adjudication. A user correction bypasses the
freeze entirely — user source always wins, unchanged.

**Re-arm semantics.** The breaker's threshold input is the count of
supersedes SINCE the most recent human-adjudicated row on the subject — a row
with status `accepted`/`rejected`, dated by its `accepted_at`/`rejected_at`
stamp; supersedes recorded before that adjudication are settled history and
never count again. (The raw lifetime superseded count is monotonic — using it
would re-quarantine the subject forever after the first adjudication.) Human
adjudication of the quarantined row therefore both lifts the freeze and
re-arms the breaker: the next agent proposal resumes the normal
supersede/cooldown path, and only a FRESH flip-flop streak of `THRESHOLD`
supersedes after the adjudication trips it again. Superseded rows without a
usable timestamp are treated as pre-adjudication when an adjudication exists;
with no adjudicated row on the subject, every superseded row counts (the
original trip condition).

### D3 — Rejection damping (consuming `prior_rejections`)

An exact re-proposal whose subject has `>= REJECTION_DAMP_MIN_COUNT`
(default 2) rejections with the most recent inside
`REJECTION_REPROPOSAL_COOLDOWN_DAYS` (default 7) is **dropped** (logged, no
row). Outside those conditions it inserts as today, but with the history made
consumable: `prior_rejections` becomes a list of
`{"mapping_id", "rejected_at"}` dicts (additive shape change, new rows only;
readers handle both shapes), `reject_mapping` stamps `rejected_at`, and the
row carries `review_reason = "re-proposed; last rejected on <date> — state
what changed before accepting"`.

### D4 — `review_reason` is the shared review-card contract

All three branches (plus the ambiguous-system-reference case, e.g. "MES"
matching mes-cr/mes-det/mes-mil) populate one additive attribute,
`review_reason`, which the review UI renders as a banner on pending cards.
Every held proposal states *why* it is held — reasoned review, not a bare
queue. All new attributes (`quarantined`, `review_reason`, `rejected_at`,
the enriched `prior_rejections`) are additive DynamoDB item attributes: no
GSI change, no schema migration, no rewrite of existing rows.

## Considered Options

- **Widen the subject grain to include the target.** Rejected: destroys
  supersede semantics (nothing ever supersedes) and orphans/splits existing
  queue rows keyed at the current grain — a data migration to buy a worse
  invariant. Keeping the grain kills that risk by construction.
- **Silently drop proposals on churny subjects.** Rejected: hides a real
  signal (the discovery loop genuinely cannot decide between targets) that a
  human should adjudicate; quarantine-with-reason keeps the signal and states
  the required action.
- **Auto-resolve flip-flops by picking the majority target.** Rejected:
  the targets flip because the evidence is genuinely ambiguous; auto-picking
  reintroduces the churn one level up and violates the auto-accept-stays-off
  posture.
- **Damp rejections forever (permanent block after N rejections).** Rejected:
  schemas and data change; a time-boxed cooldown plus a surfaced
  "what changed" reason lets a genuinely-new situation re-enter review.

## Consequences

- Supersede chains are bounded: at most `THRESHOLD` flips, then a quarantined
  `pending` row with a stated reason; the 53-supersede pathology cannot recur.
  Human adjudication re-arms the breaker (supersedes are counted since the
  most recent adjudication, not over the subject's lifetime), so an
  adjudicated subject resumes the normal supersede path instead of
  re-quarantining forever — and a fresh flip-flop streak re-trips at the same
  threshold.
- A frozen subject needs a human accept/reject to resume — if operators
  ignore the queue, legitimate target corrections stall behind the freeze.
  Accepted trade-off (queue trust over recall); the review card makes the
  required action explicit, and a user correction always cuts through.
- `prior_rejections` (E2) finally has consumers: the damping branch and the
  re-proposal review card. The ids→dicts shape change applies to new rows
  only; one lifecycle test updates its assertion, readers handle both shapes.
- Defaults (24h / 3 flips / 2 rejections / 7 days) are module constants,
  env-overridable per existing self_learning conventions — tunable without
  code change.
- Rejected/damped/cooled-down proposals never reach the graph, so no graph
  shape changes; ADR-0003's applied-node invariant (one live node per
  subject) is preserved.

## Related

- [ADR-0003](0003-unified-proposal-lifecycle.md) — the lifecycle and
  collision policy this ADR refines: same grain, same source-awareness
  (user wins), new agent-only damping branches.
- [ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md) —
  captures deterministic relationships at onboarding so fewer discoveries
  churn at query time; its degeneracy-gate refinement is backstopped by this
  ADR's damping.
- [ADR-0043](0043-per-value-to-rule-promotion.md) — promotion of accumulated
  per-value evidence; emits at existing grains, so it inherits this ADR's
  churn controls unchanged.
- [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md) — the learning
  loop whose rediscovery-on-every-resolve behavior produces the churn being
  damped.
