<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0052: Learning-health gate, usage-ledger evidence classes, and authority vouching

---
Status: accepted

Related: builds on [ADR-0050](0050-vocabulary-and-artifact-management.md)
(the D6 usage-ledger idiom and the report-only hard requirement) and
[ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) (whose
decision (e) — the P8 cross-site gate — is refined here); honors
[ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) (report-only is
the default mode and its behavioral delta surface stays small) and
[ADR-0024](0024-neptune-private-graph-not-reachable-locally.md) (the graph is
VPC-private, which shapes the gate as an HTTP client).
---

## Context

The 2026-07-24 live learning audit of the Management environment (8 systems
READY; **13 identity rules** — 9 digest-shaped
`ir-<sys>-<16hex>` + 4 steward `subj-` rows — and **182 joinpaths**) produced
three findings this ADR turns into standing contracts:

1. **The 17-fake-dups false alarm.** An ad-hoc duplicate census keyed
   joinpaths on concept-level fields and reported **17 duplicate groups** —
   every one of them a set of *distinct table-qualified joins*, not
   duplicates. `GET /api/thread/graph-summary` `join_paths_detail` returns
   `from_field`/`to_field` that ARE table-qualified (`table.column`); a
   correct census must key on `(from_system, from_field, to_system,
   to_field)` with those full fields (or the `jp-` content digests). The
   lesson is baked into the gate so it can never be re-learned the hard way.
2. **The `ar-cmms-operator` residual.** **3 pending MES↔MES `operator_id`
   join proposals passed the P8 cross-site gate** because an All-Sites
   AuthorityRule ("CMMS is the authoritative source for operator at All
   Sites") vouched for them — and that AuthorityRule was itself
   **LLM-minted** during onboarding. A machine claim vouched for a machine
   claim; nothing in the gate distinguished it from a steward assertion.
3. **The starving usage ledger.** On the default `report-only` integrity
   mode, **2 live questions produced 0 ledger rows** — report-only turns
   retain no verified relationship proofs
   (`IntegrityContext.trusted_relationships` stays empty), so
   `_integrity_rule_usage` extracts nothing and the
   [ADR-0050](0050-vocabulary-and-artifact-management.md) D6 utility signal
   is empty exactly where the default demo deployments run. The D6 hard
   requirement ("usage instrumentation MUST function in report-only") was
   met by the *writer*, but the *evidence source* it reads is enforce-shaped.

The same audit also found proposal/rule **writers whose output nothing ever
read** — artifacts minted with no review surface and no usage telemetry, so
their value was unmeasurable and their defects invisible. And it re-confirmed
two working controls worth pinning: **0 accepted `hierarchy_position` rows**
(the [ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) P4
auto-accept deny holds), and the 2026-07-24 provision semantics under which
**regeneration replaces a class's prior generated rows while steward rows —
structurally unmatchable by generated ids — always survive**
(replace-not-accrete).

## Decision

### (a) — A post-deploy learning-health gate: `scripts/audit_learning.py`

A repo-root, stdlib-only HTTP client (the `smoke_prompts.py` skeleton:
cookie login, env fallbacks, `--report` JSON artifact, exit 0 = pass) runs
seven probes against a deployed environment — the graph is VPC-private
([ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)), so the
gate reads `GET /api/thread/graph-summary`, `GET /api/proposed-mappings`,
and `GET /api/admin/learning/health`; probe functions are pure over the
parsed JSON so they unit-test offline:

1. **Duplicate-content census** — identity rules keyed `(source_system,
   source_field, target_system, target_field, concept)`; joinpaths keyed on
   the **table-qualified** `from_field`/`to_field` — never concept-coarse
   keys (the 17-fake-dups lesson). Groups with more than one LIVE
   (non-deactivated) member are duplicates; under replace-not-accrete they
   indicate a regeneration that accreted instead of replacing.
2. **Id-shape audit** — live ids match `^(ir|jp)-.+-[0-9a-f]{16}$` or
   steward `^subj-`; legacy index-keyed ids (`ir-cmms-14`) are flagged.
3. **Forbidden-pairing scan** — identifier→measure endpoints,
   stream-suffix values used as equipment ids, site⊃equipment grain
   pairings.
4. **Auto-accept-method audit** — accepted `method=="hierarchy_position"`
   rows must be 0; accepted UUID-shaped-target rows must carry
   `verified=="true"`.
5. **Cross-site vouching probe** — pending same-`system_type`,
   different-site proposals report their vouching authority id and whether
   the decision-(c) `review_reason` is present.
6. **Quarantine growth + pending-age distribution** — trend data,
   always report-only; never gates.
7. **Usage-ledger liveness** — rows in the trailing window with the
   `evidence_classes` flags (classes seen, never per-class counts — the
   ledger's day buckets are last-write-wins on the evidence attr, so a
   per-class partition of the total is not recoverable); because `/health`
   echoes `RESPONSE_INTEGRITY_MODE`,
   0 rows on a report-only env that predates decision (b) is reported as
   `expected-gap`, not failure.

**Exit-code contract:** the default gate fails (exit 1) only on structural
regressions — probe 1 duplicate groups > 0, probe 2 legacy ids > 0, probe 3
forbidden pairings > 0, probe 4 violations > 0. `--strict` adds probe 5
(cross-site pendings lacking a `review_reason`) and probe 7 (ledger
starvation unless `expected-gap`). Probe 6 never gates. The gate is a
sibling of `smoke_prompts.py` — it complements the grounding gate, never
replaces it.

Two process contracts ride with the gate as definition-of-done:

- **A learning writer ships with its read path + usage telemetry.** No
  proposal/rule writer merges without the surface that reads its output and
  the ledger evidence that proves it fired. This is what makes the audit's
  "writers whose output nothing ever read" class structurally impossible to
  reintroduce.
- **Replace-not-accrete is the standing contract for ALL generated artifact
  classes.** Regeneration replaces the class's prior generated rows; steward
  rows are structurally unmatchable and always survive. Probe 1 is its
  regression detector.

### (b) — Usage-ledger evidence classes, with a route-intersection fallback for report-only turns

Every usage-ledger row carries an `evidence` class (additive, non-empty-only
attribute on the day-bucket row; legacy rows fold as `"tool-text"` on read —
the honest default, since that extraction was the only pre-existing writer):

- `"relationship-proof"` — enforce-path extraction from verified
  relationship proofs (the existing `_integrity_rule_usage` source). The
  strongest class.
- `"tool-text"` — the legacy non-integrity extraction
  (`usage_tracking.extract_rule_node_ids` over tool results).
- `"route-intersection"` — the new fallback, written only when the stronger
  extractions yielded nothing.

Three options were analyzed for closing the report-only gap; the analysis is
recorded in full because the rejected options will look attractive again:

- **Route-seed grounding — CHOSEN.** After the turn, intersect the Mode-A
  route seed's artifact ids (the seed's jp/ir entries already carry `id`)
  with the systems the turn actually queried (the invocation's tracked tool
  names). Bump matching artifacts with `evidence="route-intersection"`.
  *Cost:* zero extra queries — the seed is already built and parsed this
  turn, tool names already tracked; a few dozen string comparisons.
  *Weakness:* the evidence class is honest-but-weak — it proves "this
  artifact was in routing context AND its systems were queried", NOT "this
  rule's translation was applied". Surfaces that display usage counts must
  label the class distinctly.
- **Capture-lite verification on every report-only turn — REJECTED.** Run
  relationship verification on report-only turns (compute proofs, don't
  gate), yielding `"relationship-proof"` evidence everywhere. Strongest
  evidence, but it adds N Neptune round-trips **per live user turn, on
  100 % of turns, forever**, purely to feed an admin telemetry surface —
  latency and cost on the hot path for a cold-path display. It also
  enlarges the report-only/enforce behavioral delta surface that
  [ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) deliberately
  keeps small. Steady-state cost ≫ value; revisit only if
  route-intersection proves too weak in practice.
- **Enforce-only ledger + honest UI copy — REJECTED.** Zero cost, but the
  default demo environments run report-only — the ledger would stay empty
  exactly where stewards look at it; this is the audited gap restated. Its
  honest-copy idea IS kept: the UI shows the evidence class, and the health
  endpoint echoes `RESPONSE_INTEGRITY_MODE` so the decision-(a) gate can
  report 0 rows as honest-expected versus regression.

The fallback is strictly additive: it runs only when the strong extractions
returned empty, it never touches `_integrity_rule_usage`'s semantics, and a
ledger write still never fails a turn (the
[ADR-0050](0050-vocabulary-and-artifact-management.md) category-F posture).

### (c) — Authority vouching: machine claims never vouch for machine claims

The P8 cross-site gate
([ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md)
decision (e)) accepted ANY All-Sites AuthorityRule as a vouching authority —
including ones the onboarding LLM minted itself, which is how the 3
`operator_id` pendings passed. Refinement:

- An All-Sites AuthorityRule **vouches only if steward-confirmed** — its
  node carries a user-tier stamp (`method` in
  `{"user_confirmed","crosswalk_confirmed"}` or a non-empty `approved_by`) —
  **or if it carries a non-empty `evidence` property** (evidence-bearing
  linkage).
- **An unverified authority flags-not-drops.** When an All-Sites authority
  exists but is unverified, the proposal is NOT suppressed: it proceeds to
  pending with `review_reason = "cross-site link vouched only by unverified
  authority <id>"` (exact wording — the audit gate's probe 5 and the review
  UI's conflict display key on it, through the
  [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) D4 review-card
  channel). This replaces log-only suppression, which was invisible to
  stewards.
- **No authority at all still drops** with the stated reason (unchanged
  ADR-0051 (e) behavior); `source == "user"` still bypasses; every backend
  failure stays fail-open — this is a proposal-quality gate, not a security
  gate.
- **Deferred — value-overlap minting of the `evidence` property.** The
  `evidence` seam exists so a future path can stamp an authority with
  *measured* cross-system value overlap, making an LLM-minted authority
  earn verification from data rather than a steward click. The computation
  itself is deferred: it requires propose-time live queries against source
  systems, which is out of scope for the deterministic gate. Recorded here
  so implementers neither gold-plate now nor treat the seam as dead code.

The 3 pre-existing `operator_id` pendings predate this refinement and do not
retroactively gain the reason; audit surfaces note them as legacy. Any NEW
cross-site pending must carry it.

### (d) — Try-it is the honest replacement for the deleted before/after fiction

The `tool_calls_before`/`tool_calls_after` columns were deleted on 2026-07-24
because they were a fiction — a fabricated saved-calls delta that no
measurement backed. **They must never return.** The honest replacement is
the steward try-it run: an async job that runs a proposal's original
question twice (baseline, then with the candidate rule injected into the
route seed as an ephemeral tier-2 entry — never persisted) and reports what
each run actually DID: systems queried, rules applied, translation steps,
answer excerpts, elapsed time, and the diff. Real evidence from real runs,
or nothing.

## Considered options

Beyond the option analysis inlined in decision (b):

- **Fold this into ADR-0051.** Rejected: 0051 is accepted and shipped; this
  is a new program (a deployed gate + an evidence taxonomy + a vouching
  refinement), and burying the 17-fake-dups and machine-vouching lessons in
  an amendment would hide exactly the history the gate exists to preserve.
- **Give the gate direct graph/DynamoDB access.** Rejected: the graph is
  VPC-private ([ADR-0024](0024-neptune-private-graph-not-reachable-locally.md));
  an HTTP client against the deployed app is the same posture as
  `smoke_prompts.py` and exercises the same surfaces stewards use.
- **Make the unverified-authority case drop (fail-closed).** Rejected: P8 is
  a proposal-quality gate, and silent suppression is how the gap stayed
  invisible; a pending row with a stated `review_reason` puts the decision
  in front of the steward, which is where an ambiguous vouch belongs.
- **Gate on probe 6 (quarantine growth / pending age).** Rejected: trend
  data gates poorly — a growing backlog is a stewardship signal, not a
  structural regression; gating it would train operators to ignore the gate.

## Consequences

- Structural learning-graph regressions (duplicate accretion, legacy ids,
  forbidden pairings, auto-accept holes) now fail a deployment check instead
  of waiting for a manual audit; the table-qualified-key lesson is encoded
  where it cannot be forgotten.
- The usage ledger produces rows in the default report-only mode, so the
  [ADR-0050](0050-vocabulary-and-artifact-management.md) utility loop has
  signal where stewards actually look — labeled honestly by evidence class,
  with the weak class distinguishable from verified use.
- An LLM-minted authority can no longer silently satisfy the cross-site
  gate; stewards see WHY a cross-site pending exists and which authority
  vouched. Legacy pre-refinement pendings are noted, not retrofitted.
- Writers without readers are a definition-of-done violation, not a latent
  audit finding; replace-not-accrete has a named regression detector.
- The deleted before/after columns stay deleted; proposal impact claims must
  come from try-it runs.

## Related

- [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md) — the
  VPC-private graph that shapes the gate as an HTTP client.
- [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) — the D4
  `review_reason` review-card channel decision (c) writes to.
- [ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) — report-only
  is the default; decision (b) keeps its behavioral delta surface small.
- [ADR-0050](0050-vocabulary-and-artifact-management.md) — the D6 usage
  ledger this ADR classifies; the category-F best-effort posture.
- [ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) — the
  P8 cross-site gate refined by decision (c); the P4 auto-accept deny that
  probe 4 pins.
