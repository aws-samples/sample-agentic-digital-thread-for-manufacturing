<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0063: Proposal precision, requirement promotion, and onboarding schema-shape facts

## Status

**Accepted — 2026-08-24.** D1–D6 shipped with the proposal-precision sweep;
D7 adds the P0 guidance lifecycle integrity contract after a deployed
accepted-versus-delivered mismatch exposed the former best-effort mirror.

Builds on [ADR-0003](0003-unified-proposal-lifecycle.md) (one proposal
lifecycle — every candidate files through it),
[ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) (P4
auto-accept hardening: `hierarchy_position` never auto-accepts; UUID targets
need a passing existence check),
[ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) (churn damping —
nothing here may defeat it),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(a learning writer ships with its read path and usage telemetry;
replace-not-accrete),
[ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (measurement
honesty; computed verdicts — an LLM never produces one),
[ADR-0054](0054-measurement-honesty-and-experiment-harness.md) (business context is
UNTRUSTED steward text and carries no evidence weight),
[ADR-0058](0058-post-provision-cross-system-mining-and-value-crosswalk-evidence.md)
(the miner whose precision this ADR measures),
[ADR-0059](0059-failure-signal-accumulator-and-runtime-steward-questions.md)
(the signal accumulator and steward-question surface this ADR extends), and
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no dataset literals in any mechanism here).

## Context

A steward pass over a matured deployment's queue adjudicated **101 pending
cross-system proposals against ground truth** read from the source systems
(telemetry asset listings + the transactional system's own equipment values,
never the proposals' own reasoning). The outcome measured the miner rather
than the models:

- **7 proposals were correct** and were entered as steward assertions.
- **48 were demonstrably false.** The dominant failure was not bad reasoning
  but an **unverified target**: the proposal named a target-system value that
  does not exist there at all (an asset name pattern-extrapolated from a
  sibling, a numeric-suffix family assumed to align across two unrelated
  naming schemes, a virtual routing station claimed as a telemetry asset).
- The rest were duplicates, same-system pairings, or acknowledge-only drift.

Three engine-level facts explain most of that 48, and all three are fixable
without a stronger model:

1. **Target verification is UUID-only.** `_verify_uuid_target` (ADR-0051 P4)
   already encodes the right idea — the target node must EXIST and its name
   must be consistent with the source value — but it fires only when the
   target is UUID-shaped. Every false row above proposed a **name** target,
   so the check never ran. One deterministic graph read per proposal would
   have refuted them at filing time.
2. **Trust signals the engine already computes are invisible to the steward.**
   The engine knows `hierarchy_position` is "a positional guess, whatever its
   confidence" (it is denied auto-accept for exactly that reason), yet those
   rows reach review at confidence 0.7–0.9 looking identical to verified
   ones. A steward — and a careless operator following the same reasoning —
   cannot see the difference. (Observed: an operator accepted seven such rows
   through the normal accept route; the learning-health gate's P4 probe was
   what caught it, after the graph writes had already happened.)
3. **Nothing reports the miner's own precision.** Accept/reject decisions
   persist with method and source, so the accept rate per method is
   computable — but no surface shows it, so a miner can degrade indefinitely
   with no signal to anyone.

Separately, two knowledge-placement findings recurred across the same period:

4. **Business context states FACTS; it cannot state ANSWER REQUIREMENTS.**
   Two goldens resisted business-context edits for days — the fact was
   written, enabled, and verifiably reaching the agent — then passed on the
   first attempt when the SAME knowledge was expressed as a requirement on
   the answer through a `guidance_note` ("the answer MUST say X, not Y").
   Business context is reference material the agent may use; a guidance note
   is an instruction it must follow. Stewards have no way to learn that
   distinction except by discovering it, and nothing detects the case where a
   context edit demonstrably did not change behavior.
5. **Schema-shape facts are learned from failures instead of from the
   schema.** One dataset's physical column names contain dots; that single
   fact produced three distinct repeated tool failures (bare reference,
   underscored rewrite, segment-quoted access) before it became an engine
   dialect rule. Onboarding READS that schema and could derive the shape
   deterministically at connect time.

## Decision

### D1 — Target verification generalizes from UUIDs to ALL targets

`_verify_uuid_target` becomes a general **target verification** seam covering
UUID-shaped AND name-shaped targets: the target must exist in the target
system's graph projection, and where both the target node's name and the
proposal's source value are present they must be consistent (the existing
case-insensitive containment test). Semantics preserved exactly:

- **Fail-closed.** Any failure, empty result, or backend error ⇒ unverified.
  Accept-on-doubt remains forbidden; an unverified proposal simply stays
  pending.
- **Best-effort stamp.** A passing check stamps `verified="true"`/
  `verified_at` as today, so review surfaces can distinguish a CHECKED
  target. **No text may claim confirmation without the stamp.**
- **Verification NEVER deletes or rejects.** An unverified proposal is
  pending, not wrong: the check gates AUTO-ACCEPT and informs review. Only a
  steward rejects. (A name absent from the graph may mean the target was
  never onboarded, not that the claim is false — honest absence.)
- **Grounding is attempted at FILE time, not only at the accept gate.** Every
  cross-system proposal records its verification state when it is filed, so
  the steward NEVER sees a row whose grounding is simply unknown-because-
  nobody-looked. The check is one bounded graph read on a path that already
  performs graph reads; a failure to run it leaves the state ABSENT (unknown),
  never a fabricated "false".
- Cross-system name targets are verified against the TO-system's projection
  specifically; a name that exists only in the FROM system does not verify.

**Authority repair (2026-08-24):** a positive verdict requires an
explicit `to_system` projection and persists `verified_scope="system"`.
A provenance-free match is `unknown/unscoped`, never `target_verified=true`,
never a confirmed/high score, and never auto-accept authority. Legacy rows with
`verified=true` or `target_verified=true` but no scope are non-authoritative.

### D2 — Trust signals are surfaced, never silently applied

Every pending row carries the trust signals the engine already computes:
`target_verified` (present only when checked — absent ≠ false), and the
auto-accept block reason when one applies (`_auto_accept_block_reason`, the
existing single source of truth — this ADR adds no second policy).

The review surface renders them as a plain-language distinction ("positional
guess — needs a human check" vs "target checked against the system"). This is
DISPLAY of existing policy: it changes no accept semantics, and it must not
become a parallel trust model. The bulk and single accept routes keep their
current gates.

### D3 — Miner precision is measured and reported (computed, never estimated)

The learning digest gains a **precision block** computed from persisted
steward decisions: for a bounded recent window, per `method` (and overall),
the accepted / (accepted + rejected) rate and the decision count.

- **Computed only.** No LLM, no inference; decisions or nothing.
- **Honest absence.** Below a minimum decision count the rate is ABSENT (not
  zero, not a guess) with the count shown — a small sample states its own
  smallness (ADR-0053 measurement honesty).
- **Report-only.** Precision NEVER gates filing, accepting, or auto-accept.
  It is a quality signal about the machinery, for humans.
- Requeued rows are excluded from both numerator and denominator: a requeue
  is a correction of the ACCEPT, not a steward verdict on the proposal.

Iteration 2 narrows the denominator to exact ADR-0058 machine-miner artifacts:
`source=agent`, `method=cross-system-miner`,
`provenance=adr-0058-machine-miner`, and type `join_path` or `crosswalk`.
Only recent terminal decisions carrying the relevant steward actor stamp count.
Human, guidance, schema, context-placement, unattended, legacy-unmarked, and
requeued rows are excluded.

### D4 — Batch adjudication is explicit-list, never wildcard

A steward may adjudicate a GROUP of pending rows in one action. The group is
computed on read (grain: `method` + `from_field`→`to_field` + proposed_type)
and the request carries the **explicit list of mapping ids**; the response
echoes per-id outcomes.

- **No wildcard, no "reject everything matching".** A blind predicate would
  let a UI bug tombstone the queue. Bounded per request.
- Each id runs the SAME single-row accept/reject path (gates, damping,
  tombstone semantics unchanged) — batching is transport, not new policy.
- Partial failure is honest: per-id results, never an all-or-nothing lie.

Iteration 2 makes eligibility action-specific and computed on read. The backend
reloads every explicit id immediately before mutation; `accept` refuses flows
that require specialized confirmation (currently `guidance_note`) while
`reject` remains independently eligible for a pending row. Per-id failures use
a bounded fixed error-code taxonomy and never return exception or backend text.

### D5 — A context edit that does not take becomes a steward question

When business context for a system is saved and subsequent answers still
contradict it, that is a detectable **signal**, not a mystery. The failure
signal accumulator (ADR-0059) gains a `context_not_reflected` class, and the
runtime steward-question surface offers **promote to answer requirement**:
one click files a `guidance_note` proposal carrying the requirement phrasing,
through the normal lifecycle (never auto-accepted; the steward still accepts).

- **Detection is bounded and evidence-bearing** — the question names the
  edit and the answers that contradicted it; no claim without those.
- Business context is NOT rewritten. A guidance note is ADDITIVE; the
  steward's own text is never machine-edited (ADR-0054: untrusted steward
  text is the steward's).
- The promotion is a PROPOSAL. Nothing about this path auto-accepts.

### D6 — Onboarding captures schema-shape facts deterministically

Provisioning derives **schema-shape facts** from the schema and samples it
already reads, with no extra connector round-trips beyond the existing ones:
dotted physical column names, serialized-collection columns (an empty
collection stored as a literal token rather than NULL/empty), case-uniform
enum vocabularies, and sparsely-populated columns.

- **Engine-generic dialect facts** (how to reference a dotted name) belong in
  the connection type's un-displaceable prompt, NOT in steward text — the
  precedent ADR-0060 already generalizes.
- **Deployment-specific shape facts** (which columns are sparse, which enum
  is uppercase) are filed as PROPOSALS or raised as intake questions; they
  are never silently written as steward-authored context.
- Best-effort: a shape-derivation failure never fails provisioning.

Iteration 2 tightens the data boundary: raw schema names and sampled values are
transient derivation inputs only. Persisted/logged proposal envelopes contain
only the shape class, bounded counts/ratios, and full SHA-256 digests. They are
filed as metadata-only `insight` proposals with empty field/value endpoints;
raw table, column, value, and secret-shaped text never crosses the proposal IO
boundary.

### D7 — Guidance acceptance is loss-aware and accepted implies delivered

A `guidance_note` uses a specialized acceptance transaction before any proposal
terminal status. The generic proposal lifecycle remains unchanged for every
other type, and the machine still never writes `rejected`.

1. `GET /api/proposed-mappings/{mapping_id}/guidance-preview` is steward-gated
   and side-effect-free. Its bounded outcome is exactly `append`, `duplicate`,
   `no_room`, `would_evict`, or `unavailable`. It returns lengths and full
   SHA-256 digests (including oldest-first evicted-note digests), never system
   text, note text, exception detail, or secret material.
2. `duplicate`, `no_room`, unavailable state, delivery failure, and an
   unconfirmed eviction leave the proposal `pending`; `guidance-noop-*` applied
   identifiers no longer exist. A `would_evict` accept must echo the fresh
   `current_digest` and the exact ordered `evicted_note_digests`. Unknown keys,
   wildcards, and force flags are refused; a changed current digest is a 409
   stale preview.
3. The graph write is expected-current compare-and-set followed by exact
   readback. Delivery then uses `SystemGuidanceStore.publish_exact`, whose
   persisted readback must byte-match. Only after both proofs does the proposal
   become `accepted`, stamped with graph and delivery digests plus the persisted
   delivery version.
4. A delivery failure after the graph CAS attempts an expected-current graph
   compensation. Proven compensation returns unavailable with the proposal
   pending. If compensation cannot be proven, the response reports opaque drift
   and leaves the proposal pending for reconciliation; it never claims success.
5. `GET /api/query-guidance/reconciliation` is steward-gated and read-only. It
   reports accepted/graph/delivered counts, per-source honest unavailable
   states, and bounded opaque digest mismatches. It emits no customer text.
6. P0 exposes no repair or all-systems mirror mutation route. Any future repair
   must have a separate preview and accept only one explicit system id plus an
   exact digest list; `all`, wildcard, predicate, and force selection remain
   forbidden.
7. Guidance telemetry proves only that an artifact was delivered to the agent
   store or composed into a turn. It must never be named or interpreted as
   model obedience, answer compliance, or causal effectiveness.

## Consequences

- The dominant false-proposal class is refuted at filing time by one
  deterministic read instead of a steward's manual ground-truth pass — the
  fix is verification, not a larger model (measured precedent: the frontier
  models scored the same verdicts at ~1.9x tokens on this project's golden
  lane, and the model-free detectors in this pipeline were the accurate ones).
- Stewards see WHY a row is weak, so the P4 policy stops being a trap that
  only the audit lane catches.
- The miner becomes measurable, which is the precondition for tuning it.
- Two failure classes that previously required an analyst to diagnose by hand
  (context-not-reflected, schema-shape traps) become things the system
  notices and asks about.
- Nothing here relaxes a gate: no new auto-accept path, no machine-written
  terminal status, no wildcard mutation, and every new number is computed
  with honest absence.
