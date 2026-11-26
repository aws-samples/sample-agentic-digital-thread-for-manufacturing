<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Self-learning deep-dive: proposals, crosswalk mining, and feedback

> Terminology follows [`glossary.md`](glossary.md). For how systems get into
> the graph in the first place, see [`onboarding.md`](onboarding.md).

The engine gets smarter by accumulating **rules**—value mappings, join
paths, identity rules, findings—but it never trusts itself to apply them.
Every machine discovery is a *proposal* that a human steward reviews; the
graph holds only applied truth. This document covers the proposal lifecycle,
how crosswalks are mined at query time and onboarding time, the safety rails
that keep the learning loop honest, and the feedback-triggered exploration
path.

## The unified proposal lifecycle (ADR-0003)

Everything flows through one door: `self_learning.log_proposed_mapping`
(`agents/self_learning.py`). Agent-discovered candidates enter `pending`;
user corrections enter already `accepted` (source: user). Amazon DynamoDB
(table `adt-proposed-mappings`) is the proposal log, lifecycle, and audit
trail; Amazon Neptune Analytics holds the applied truth
([ADR-0003](adr/0003-unified-proposal-lifecycle.md)).

Statuses: `pending → accepted / rejected / superseded`, plus `expired`
(insight findings past their TTL, below) and a `quarantined` flag on pending
rows held for adjudication. `status=rejected` is written **only** by the
steward route (`reject_mapping`, behind the steward check)—the machine can
never reject (see tombstones, below).

Idempotency and conflict are governed by a **type-aware, two-grained
canonical key** (`agents/proposal_key.py`—pure, stdlib-only, no LLM): a
`subject_key` (the left-hand side that can have only one right answer)
governs conflict/supersede, and a `target_key` (subject + right-hand side)
governs idempotency. The grains per `proposed_type`:

| proposed_type | subject (conflict grain) | target adds (idempotency) |
|---|---|---|
| `value_mapping` | (from_system, from_field, from_value) | (to_system, to_field, to_value) |
| `join_path` | (from_system, from_field) | (to_system, to_field) |
| `identity_rule` | (from_system, from_field, concept) | (to_system, to_field) |
| `field_annotation` | (from_system, from_field) | digest(annotation) |
| `insight` | (from_system, concept, digest(annotation)) | digest(annotation) |
| `correlation` | (from_system, to_system, concept) | digest(annotation) |
| `schema_drift` | (from_system, from_field) | digest(reason + description) |

(Full table in `agents/proposal_key.py`; the vocabulary grains—
`concept_vocab`, `guidance_note`, `concept_overlay`, `metric_definition`—
are keyed in `agents/self_learning.py`.)
Collision policy: an exact duplicate `pending` returns the existing proposal;
an exact duplicate already `accepted` is a no-op; same subject with a
different target **supersedes** (prior kept as `superseded`). Supersede is
source-aware: a user correction wins over anything, and an agent proposal
never clobbers an accepted user correction—it is recorded as disagreement
instead ([ADR-0003](adr/0003-unified-proposal-lifecycle.md) C).

**Auto-accept is OFF by default** (`SELF_LEARNING_AUTO_ACCEPT`, default off—
`agents/self_learning.py`). Even when enabled it is narrow: only
`value_mapping`/`identity_rule` with high computed score; a deny-tuple
(`_NEVER_AUTO_ACCEPT_TYPES`) excludes insights, correlations, annotations,
vocabulary writes, `schema_drift`, and more; positional
(`hierarchy_position`) matches never auto-accept and every target—UUID- or
name-shaped—requires a passing graph existence/name check
([ADR-0051](adr/0051-insight-finding-grain-graduation-and-site-scope.md) P4,
generalized to all targets by
[ADR-0063](adr/0063-proposal-precision-and-requirement-promotion.md) D1);
and `source="user-feedback"` rows are denied regardless of type.

## Crosswalk mining

Value mappings—"`LINE1` here is `DET-L1` there"—are discovered at three
moments:

- **Onboarding time.** Provision captures same-row identifier sets and
  intra-system joins directly (deterministic evidence), and files
  crosswalk-grade candidates as proposals
  ([ADR-0041](adr/0041-onboarding-time-capture-same-row-and-intra-system.md);
  detail in [`onboarding.md`](onboarding.md#provision-graph-writes-and-crosswalk-capture)).
- **Query time.** Learning capture is **post-turn**: candidate value
  mappings and correlation insights are extracted after the answer is
  delivered (`_capture_value_mapping_proposals` /
  `_capture_correlation_insights` in `agents/runtime_app.py`), never as
  inline writes polluting the interactive turn
  ([ADR-0036](adr/0036-learning-loop-post-turn-elicited-scoped.md)).
  Synthetic runs (try-it replays, feedback explorations) are suppressed from
  every post-turn capture writer (`_skip_post_turn_capture`).
- **Promotion.** Accumulated per-value observations generalize into rules
  ([ADR-0043](adr/0043-per-value-to-rule-promotion.md),
  `agents/value_promotion.py`): the operator-triggered sweep
  (`POST /api/proposed-mappings/promote-values`—no scheduler) groups
  pending `value_mapping` rows by field pair and promotes a group holding at
  least 3 **distinct** normalized `from_value`s—a count of independent
  confirmations, explicitly *not* LLM confidence, which was measured not to
  discriminate. Promotion emits ONE `join_path` (transform found) or
  `crosswalk` (aligned exemplar) proposal through `log_proposed_mapping`,
  inheriting the full collision policy; the subsumed per-value rows are
  superseded with `subsumed_by`.

### Churn control (ADR-0042)

Rediscovery loops can thrash a subject. Three agent-only branches damp it
([ADR-0042](adr/0042-subject-churn-cooldown-and-quarantine.md); constants in
`agents/self_learning.py`, env-overridable):

- **Supersede cooldown (D1):** a would-be supersede within
  `SUBJECT_SUPERSEDE_COOLDOWN_HOURS` (default 24) of the last one is a
  dup-style no-op—two contradictory "discoveries" in one day are noise.
- **Flip-flop quarantine (D2):** a subject whose supersede chain reaches
  `SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD` (default 3) distinct targets is
  frozen: the incoming proposal is stored `pending` with
  `quarantined="true"` and a stated `review_reason`, and further agent
  proposals on the subject also quarantine until a human adjudicates. The
  breaker re-arms on human adjudication.
- **Rejection damping (D3):** an exact re-proposal whose subject has ≥ 2
  rejections with the most recent inside 7 days is dropped (logged, no row).

User-source proposals bypass all three—user truth always wins.

## Safety rails

- **Learning health gate.** `scripts/audit_learning.py`
  ([ADR-0052](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md))
  runs probes against a deployed environment through `GET
  /api/thread/graph-summary`, `GET /api/proposed-mappings`, and `GET
  /api/admin/learning/health` (shared stat builders in
  `agents/learning_health.py`): duplicate-content census on table-qualified
  keys, id-shape audit, forbidden-pairing scan, auto-accept-method audit,
  cross-site vouching probe, quarantine/pending-age trends, and usage-ledger
  liveness. The default gate fails only on structural regressions.
- **Evidence ledger.** A learning writer ships with its read path and usage
  telemetry—no artifact class is minted without a surface that reads it and
  ledger evidence it fired (`agents/artifact_usage.py`,
  [ADR-0052](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)).
- **Authority vouching: machine claims never vouch for machine claims.** The
  cross-site gate is satisfied only by steward-confirmed authority or
  evidence-bearing linkage; an LLM-minted authority rule cannot wave through
  cross-site proposals, and an unverified vouch flags-not-drops
  ([ADR-0052](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)).
- **Tombstones are steward claims, with 90-day decay.** Rejected rows *are*
  the tombstones—no separate registry. An exact agent re-proposal of a
  shape a steward rejected within `TOMBSTONE_TTL_DAYS` (default 90) still
  inserts `pending` but **flagged** with a review reason naming the rejection
  date and mapping id—flag-not-drop, so nothing is silently suppressed.
  Repeat offenders still hard-drop via D3 damping; user source bypasses; a
  steward can reopen a rejected row
  (`POST /api/proposed-mappings/{id}/reopen`). The machine never writes
  `status=rejected`
  ([ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)
  decision (c)).
- **Replace-not-accrete.** Regeneration of any generated artifact class
  replaces that class's prior generated rows; steward rows—structurally
  unmatchable by generated ids—always survive
  ([ADR-0052](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)).
- **Schema drift never mutates the graph on detection.** Drift detection
  files `schema_drift` proposals; accepting a removal/retype is
  acknowledge-only, while accepting `column_added`/`table_added`
  materialises the Field/SchemaEntry through `agents/schema_fields.py`
  ([ADR-0077](adr/0077-additive-schema-repair-on-drift-accept.md);
  [ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md);
  see [`onboarding.md`](onboarding.md#re-onboarding-and-schema-drift)).

## Insight findings and graduation (ADR-0051)

Narrative findings (`insight`) carry a **per-finding grain**—the narrative
digest is folded into the subject key—so distinct findings on the same
system + topic coexist instead of superseding each other into quarantine.
Pending insights are findings, not rules: they expire lazily after
`INSIGHT_PENDING_TTL_DAYS` (default 30; status → `expired`, no scheduler).
When a `(from_system, to_system, concept)` group accumulates
`INSIGHT_GRADUATION_MIN_CORROBORATIONS` (default 3) distinct corroborating
narratives, `sweep_insight_graduation` emits ONE pending `correlation`
proposal—count of independent confirmations, never confidence, and never
auto-accepted
([ADR-0051](adr/0051-insight-finding-grain-graduation-and-site-scope.md);
`agents/self_learning.py`).

## Feedback solicitation and exploration

A thumbs-down with a free-text comment (`POST /api/feedback`,
`agents/feedback.py`) MAY trigger one **bounded background exploration**
(`agents/feedback_exploration.py`) that re-examines the original question and
compares the re-run's routing against the citations the user was shown:

- **Bounds (pinned by tests; do not relax without an ADR):** 12 tool calls
  per run, 240 s wall clamp per run, 540 s total across both runs, at most
  3 explorations per user per hour, one concurrent exploration slot
  (per-replica, best-effort—the containment is a throttle, not a
  correctness guarantee), 24 h dedupe per (session, question).
- **Corroboration:** the exploration runs the same composed prompt **twice**
  in independent sessions and files only the **intersection** of the two
  runs' gaps—a gap in one run only is nondeterminism, recorded but never
  filed. Only a GAP files (systems/rules the re-run used that the cited
  answer did not); a re-run that merely queried fewer systems is variance,
  not a finding.
- **Filing:** a corroborated gap files EXACTLY ONE `insight` proposal via
  `log_proposed_mapping(source="user-feedback")`—never auto-accepted
  (`insight` is in the type deny-tuple AND `user-feedback` is in the source
  deny-tuple), deduped per (question, gap-set) for 24 h. The exploration's
  synthetic sessions (`feedback-explore-` prefix) suppress every other
  learning writer, so it files its one insight and nothing else
  (`agents/runtime_app.py` `_SYNTHETIC_SESSION_PREFIXES`).
- **The comment is UNTRUSTED text:** sanitized and validated at write time
  (`agents/feedback.py` via `agents/untrusted_text.py`), rendered into the
  exploration prompt only inside a fenced it-is-data block, carrying zero
  evidence weight—it never appears in proposal fields or logs.
- **Toggle precedence: env-kill > admin flag > default OFF**
  (`agents/feedback_exploration_settings.py`). The
  `FEEDBACK_EXPLORATION_ENABLED` env var is a fail-closed ops kill switch
  (any set non-truthy value kills; a truthy value does not force-enable);
  the single ON switch is the persisted admin flag behind
  `GET`/`PUT /api/admin/feedback-exploration` (`agents/admin_routes.py`).
  Nothing set anywhere means OFF.
- **Eval lane:** `scripts/eval_feedback_exploration.py` scores the path end
  to end on a deployed environment with computed-only verdicts (the LLM never
  judges—[ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)):
  goldens with deliberately incomplete citations must file exactly one
  correctly-shaped insight; adversarial/injection goldens must file nothing
  and never echo the canary.

## How stewards see and act on proposals

The **Self-Learning panel** (`frontend/src/views/architecture/proposals.ts`
+ `ProposalsPanel.tsx`) is the review surface: proposals filtered by
lifecycle bucket (`pending` is the actionable default; `accepted`,
`rejected`, `superseded` are inspectable), with optimistic accept/reject
(card flips immediately, silent background reconcile, revert on failure),
an edit overlay, a user-define flow for corrections (a steward's enter
`accepted` directly as user source; anyone else's are filed `pending` for a
steward and touch no graph until accepted), and the Learned Guidance list. Quarantined and
tombstone-flagged rows carry their stated `review_reason` so the steward
sees *why* a row needs adjudication. Server routes:
`GET /api/proposed-mappings` and the per-mapping
`accept` / `reject` / `requeue` / `reopen` actions (`agents/server.py`).
Accepting a proposal writes the applied truth to Neptune
(`accept_mapping`); the steward digest (`frontend/src/views/dashboard/
StewardDigest.tsx`) surfaces pending-review pressure outside the panel.

## Where to go next

- [`onboarding.md`](onboarding.md)—where onboarding-time proposals come
  from, and the interview answers that land in this lifecycle.
- [`architecture.md`](architecture.md)—the rules-engine boundary all of
  this writes within.
- [ADR-0003](adr/0003-unified-proposal-lifecycle.md),
  [ADR-0042](adr/0042-subject-churn-cooldown-and-quarantine.md),
  [ADR-0043](adr/0043-per-value-to-rule-promotion.md),
  [ADR-0051](adr/0051-insight-finding-grain-graduation-and-site-scope.md),
  [ADR-0052](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md),
  [ADR-0053](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)—
  the decision records behind each mechanism.
