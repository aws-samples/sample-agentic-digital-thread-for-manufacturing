<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Per-value → rule promotion: count of independent confirmations, not confidence

---
Status: accepted

Related: complements [ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md)/[ADR-0042](0042-subject-churn-cooldown-and-quarantine.md); auto-accept stays OFF by default.
---

## Context

The agent's finest-grained discovery, the `value_mapping` proposal
(`LINE1 → DET-L1`, one value pair per row), accumulates in the queue without
ever generalizing. The 2026-07-14 audit found the pending queue carrying
3 per-plant `line_id` value mappings and **40** serial↔tag value mappings —
each one an independent per-value rediscovery of a single underlying rule
(a `join_path` with a transform, or a `crosswalk`). A human reviewing the
queue faces 40 cards where one would do, and accepting them one by one never
produces the rule.

Two facts shape the design:

- **LLM confidence does not discriminate.** Audit evidence: every per-value
  proposal scores 0.7–0.9 regardless of merit. Any promotion threshold based
  on confidence would be noise-driven.
- **The primitives already exist.** `sweep_auto_accept` is the queue-sweep
  shape; `join_discovery.discover_transform` + the key-cardinality floor can
  turn accumulated value pairs into a verified transform;
  [ADR-0003](0003-unified-proposal-lifecycle.md) defines the canonical grains
  the promoted proposal must be keyed at.

## Decision

### D1 — Promotion grain is the FIELD PAIR

The sweep groups PENDING `value_mapping` rows by
`(from_system, from_field, to_system, to_field)` — the field pair whose
repeated per-value confirmations are evidence of one rule. Only groups whose
fields are `table.column`-shaped and whose systems or tables differ are
eligible (same-system same-table pairs are
[ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md)'s
same-row territory, captured at provision, not promoted from the queue).

### D2 — Threshold = count of independent confirmations, explicitly NOT confidence

A group is promoted when it holds at least `min_confirmations` (default 3)
**distinct normalized `from_value`s**. Distinct values are independent
observations of the rule holding; N copies of the same pair are one
observation. LLM confidence plays no role in the threshold — it does not
discriminate (0.7–0.9 across the board) and would let one overconfident
observation outvote three consistent ones.

### D3 — Promotion emits ONE proposal at the EXISTING grains

For each qualifying group the sweep runs `discover_transform` over the
accumulated value pairs:

- transform found → ONE `join_path` proposal (transform + measured overlap
  carried; `query_context` names the N confirmations);
- no transform but aligned pairs → ONE `crosswalk` proposal (first aligned
  pair as the exemplar, context names N).

Both are emitted through `log_proposed_mapping` with `source="agent"` at the
**existing** `proposal_key` grains — no `_DISPATCH` change, no new
`proposed_type`, so the promoted proposal inherits the full ADR-0003
collision policy and the [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md)
churn controls unchanged.

### D4 — Subsumed per-value rows are superseded with `subsumed_by`

The group's per-value rows move to `superseded` with
`subsumed_by=<new mapping_id>` — an additive attribute distinguishing
"generalized into a rule" from "lost a target-collision supersede." The
audit trail keeps every per-value observation; the review queue shows one
card. The sweep is per-group best-effort (one bad group never aborts the
sweep) and returns counts (`promoted` / `subsumed` / `skipped_below_threshold`).

### D5 — The promoted rule enters `pending`; auto-accept stays OFF

Promotion produces a *better-evidenced proposal*, not an applied rule. The
emitted proposal enters `pending` with its measured support for a human to
review — by construction: `join_path` is outside `_AUTO_ACCEPT_TYPES` and
`crosswalk` is in the deny-tuple. The sweep never calls accept. It runs as an
operator-triggered admin route (`POST /api/proposed-mappings/promote-values`),
not on a scheduler — consistent with the auto-accept-stays-off posture:
enabling any automatic application remains a separate, explicit user decision.

## Considered Options

- **Confidence-threshold promotion.** Rejected on evidence: per-value
  confidences cluster at 0.7–0.9 regardless of merit, so any cut line is
  arbitrary; count of distinct confirming values is the signal that actually
  discriminates.
- **Auto-accept the promoted rule when support is high.** Rejected: violates
  the binding auto-accept-stays-off posture; promotion strengthens evidence,
  it does not replace review.
- **A new `promoted_rule` proposed_type / new grain.** Rejected: the existing
  `join_path`/`crosswalk` types already express the outcome; a new type would
  need new `proposal_key` dispatch, new review-UI wiring, and would bypass the
  ADR-0042 churn controls that apply at the existing grains.
- **Emit one proposal per value-group member (status quo).** Rejected: this
  is the 40-card problem being solved.
- **Delete subsumed per-value rows.** Rejected: destroys the audit trail;
  `superseded` + `subsumed_by` keeps the observations queryable and states
  where they went.

## Consequences

- The existing pending backlog (3×/plant `line_id`, 40× serial↔tag)
  collapses into single `join_path`/`crosswalk` proposals with measured
  support; the per-value rows show `superseded`/`subsumed_by`.
- Review effort scales with the number of RULES, not the number of observed
  value pairs.
- The threshold (default ≥3 distinct from_values) is a parameter of the
  sweep; below it, per-value rows simply wait — no data is lost.
- `subsumed_by` is an additive DynamoDB attribute; no GSI/schema change. The
  `proposal_key` grain table is untouched.
- A promoted `join_path` lands on the ADR-0003 subject grain
  `(from_system, from_field)` — if that subject is quarantined or cooling
  down per ADR-0042, the promotion classifies accordingly (held with reason,
  not force-inserted). Intended: promotion is evidence, not privilege.
- Operator-triggered for now; a scheduled sweep is a later, separate decision
  once queue behavior under promotion is observed.

## Related

- [ADR-0003](0003-unified-proposal-lifecycle.md) — the lifecycle, grains, and
  supersede semantics the promoted proposal is emitted into unchanged;
  `subsumed_by` refines its `superseded` state with a cause.
- [ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md) — the
  onboarding-time complement: what provision captures deterministically never
  accumulates as per-value rows; same-row sets are explicitly excluded from
  promotion.
- [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) — churn controls
  the promoted proposal inherits by emitting at existing grains.
- [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md) — the learning
  loop that produces the per-value observations being promoted.

## Note 2026-10-28: the onboarding identity-rule cap no longer lifts on a one-column fit

Provision applies this ADR's finding to the identity rules onboarding writes
(the W4 calibration, `onboard._calibrated_rule_confidence`): a provision-written
rule is stored at min(its confidence, 0.7), then divided by K when one source
column maps to K target columns in the same run. The 0.7 is one constant,
`identifier_translation.MODEL_CONFIDENCE_CEILING`, shared by every writer that
stores a model-supplied number. No ADR recorded that calibration until now,
so this note records it here, next to the finding it rests on.

The cap used to be lifted whenever the provision pattern check found one
source value with the pattern's shape (`sample_check_passed`). For a rule with
no measured method that let the model's own number through (0.74, 0.96 seen
on a walk) for a rule provision counts unchecked: a fit shows one column has
the pattern's shape, not that a value joins the two columns. The lift now
applies only to a rule with a measured method (`onboard._has_measured_method`:
`value_core_match` or `value_exact_match`, read after provision's own
re-measurement, the same test that decides the unchecked count). Everything
else is unchanged:

- 0.7 stays a ceiling, not a value: a rule the model scored 0.5 is written at
  0.5.
- An exact-agreement rule ([ADR-0087](0087-exact-agreement-cross-system-joins.md))
  keeps its probe-derived confidence, with no cap and no division.
- A core-match rule keeps its number when the sample check passed, is capped
  at 0.7 when it did not, and is never divided.
- A `provision-request` rule is capped at 0.7 whatever its check found.

Rules a person confirmed (`user_confirmed`, the route seed's top tier) are
written outside provision, by two writers:

- Accepting a learning proposal (`self_learning.accept_mapping`, first accept
  and re-accept after a requeue). The relationship is human-confirmed; the
  stored working confidence of a model-supplied number is capped. The steward
  confirms the relationship, not the number: the proposal's confidence is its
  proposer's, so `ir.confidence` is min(it, 0.7). The proposal's own number
  stays on its ledger row (the rule's `proposal_id` points there) and is not
  copied onto the graph node: the chat's free graph queries can enumerate a
  node's properties (`keys(ir)`), so anything on the node can reach the model.
- Teaching a rule directly (`/api/thread/learn`, `resolve._learn_identity_rule`)
  keeps the number the steward's request carries: the app's own Thread
  proposals send 0.7, and a steward calling the API is making a human
  assertion. A steward's own correction through the user-define route
  (`record_user_correction`) keeps its number the same way, also when it is
  requeued and accepted again: the accept path caps a row unless the row is
  that correction (`source='user'` and `method='user_corrected'`, both kept by
  requeue; a filer chooses its row's method, so the method alone is not enough).
  Editing a pending proposal cannot set either field: the edit route takes
  `EditMappingRequest` (extra keys refused) and `edit_mapping` refuses any key
  outside `EDITABLE_MAPPING_FIELDS`, the relationship's own fields.

Readers that order on stored confidence now see these rules level at 0.7
where the number came from a model. Among provision-written rules, a
method-less rule with a pattern fit ties with the other method-less rules in
the route seed's lowest provenance tier; among confirmed rules, an accepted
proposal ties with a rule taught at 0.7 in the top tier. Each tie, and a
conflict between two translations of one identifier into one target column,
falls to the rule id (both orders end on it). The Thread panel's ranked paths
and identity-rule cards show such rules at 70% instead of the model's number.

The cap applies to rules written from this change on. Nothing migrates a
live graph or re-caps a graph snapshot on restore, so an identity rule written
before it keeps its stored number: an accepted proposal stored at the model's
number keeps it until it is re-accepted, which applies the cap. A fresh deploy
holds no such rule.
Unit pins: `tests/unit/test_identity_rule_confidence_without_a_measured_method.py`
(provision) and `tests/unit/test_identity_rule_confidence_steward_confirmed.py`
(confirmed rules, with the two readers).
