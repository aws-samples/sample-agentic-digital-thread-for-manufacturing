<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0051: Insight finding grain, corroboration graduation, and proposal-quality guards

---
Status: accepted

Related: amends [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) (insight
churn semantics) and refines [ADR-0003](0003-unified-proposal-lifecycle.md)
(collision policy) / [ADR-0043](0043-per-value-to-rule-promotion.md)
(count-not-confidence evidence); honors [ADR-0045](0045-multi-site-metric-fan-out.md)
(`site_of` canonical home).
---

## Context

The 2026-07-24 learning audits of two deployed environments found the
proposal engine generating **volume, not learning**, along five mechanisms this
ADR amends or refines:

1. **Insight churn.** `insight` proposals are keyed on `(from_system, concept)`
   with the narrative digest only in the target key, so a DIFFERENT finding on
   the same system+topic **supersedes** the previous one
   (`proposal_key._DISPATCH` said this was "acceptable for findings"). Under
   the [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) flip-flop
   breaker, three distinct findings on one subject trip **quarantine**: one
   audited environment's insight quarantine grows without bound, and no finding is
   ever adjudicable on its own.
2. **No generalization path for findings.** Corroborating observations of the
   same cross-system correlation accumulate as individual narrative rows; the
   graph's `Correlation` node type exists with **count 0** in both audited
   environments.
3. **Silent bidirectional contradiction.** An accepted `A→B` mapping and a
   later pending `B→A′` (same endpoints, reversed direction, different
   counterpart value) key to DIFFERENT subjects under
   [ADR-0003](0003-unified-proposal-lifecycle.md)'s directional grain, so they
   coexist with no flag — the audit found exactly this shape.
4. **Confidence-shaped auto-accept holes.** A `hierarchy_position` match is a
   POSITIONAL guess (the onboarding LLM matched "the Nth element"), yet it can
   ride a 0.95 confidence into the flag-gated auto-accept; UUID-shaped targets
   auto-accept on FORMAT alone ("UUID confirmed") with no check that the target
   exists.
5. **Evidence-free propose + key aliasing.** A single co-occurrence in one
   turn becomes a reviewable row (40-card queues), and the same physical
   column keyed as `cell` in one proposal and
   `<composite_raw_record>.cell` in another produces colliding-in-meaning but
   distinct-in-key subjects.

## Decision

### (a) — P1: insight findings get a PER-FINDING grain; replace-in-place + 30-day TTL replace the churn breaker

The `insight` subject key **folds the narrative digest into the LHS**:
subject = `(from_system, concept, digest(annotation))`. Distinct findings on
the same system+topic are distinct subjects — they **coexist** instead of
superseding each other into [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md)
quarantine. This AMENDS ADR-0042 for insights only: the breaker never sees
insights as one churning subject because insight supersede is abolished by
construction (the join-rule types keep ADR-0042 unchanged). The
`guidance_note` grain (notes ACCUMULATE per system, identical re-proposal
dedupes) is the established precedent for this shape.

Consequences of the grain:

- **Pending cap = 1 per subject, replace-in-place.** Subject and target now
  carry the same discriminator, so an identical re-observed finding classifies
  `dup_pending` (no new row) — the cap needs no new machinery.
- **30-day TTL.** Insight pendings are findings, not rules; stale ones decay.
  `INSIGHT_PENDING_TTL_DAYS` (default 30, env-overridable per module
  convention) expires pending insight rows **lazily at read/propose time**
  (status → `expired`, stamped `expired_at`; no scheduler). Old-grain pending
  rows are not migrated — the TTL predicate is key-agnostic, so they decay out
  on the same clock (the build's stated backward-compat policy).

### (b) — P2: corroboration→Correlation graduation ladder; never auto-accepts

A sweep-shaped helper (`sweep_insight_graduation`, mirroring the
[ADR-0043](0043-per-value-to-rule-promotion.md) `promote-values` idiom) groups
pending+accepted insight rows by `(from_system, to_system, concept)` — both
systems non-empty and distinct — and, when a group holds at least
`INSIGHT_GRADUATION_MIN_CORROBORATIONS` (default 3, env-overridable)
**distinct narrative digests** (count of independent corroborations, expressly
NOT confidence — ADR-0043 D2 generalized), emits **ONE** `correlation`
proposal through `log_proposed_mapping` (`source="agent"`, `pending`).

- `correlation` is a new `proposed_type` with a `proposal_key._DISPATCH`
  entry (subject = `(from_system, to_system, concept)`, target adds the
  evidence digest), an `_apply_correlation` accept path MERGE-writing the
  EXISTING `Correlation` metamodel node shape (the `resolve.py` reader shape:
  `pattern`/`confidence`/`concept_a`/`concept_b`), and membership in
  `_NEVER_AUTO_ACCEPT_TYPES` — the deny-tuple beats the allowlist by
  construction, so a graduated proposal can never auto-accept (ADR-0043 D5
  posture). Unknown-type review rendering falls back gracefully in
  `proposal_scoring.score_proposal` (pinned by test).
- The sweep is operator-triggered/sweep-shaped, not scheduled; it never calls
  accept; re-runs are idempotent (same group → same canonical target →
  `dup_pending`); a fresh corroboration changes the evidence digest → normal
  supersede of the prior pending graduation.

### (c) — P3: unordered-pair bidirectional conflict flag (ADR-0003 collision refinement)

A pure, exported canonicalization (`proposal_key.unordered_value_pair`)
normalizes `(A,B)`/`(B,A)` endpoint triples to one ordering. At propose time,
an agent-sourced `value_mapping`/`join_path` whose reversed direction
contradicts an ACCEPTED row (same reversed field pair; shared pivot value;
different counterpart) is stored pending with
`review_reason = "conflicts-with-accepted-reverse-mapping <id>"` — the
[ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) D4 review-card
channel. A consistent restatement (`B→A` with the SAME value pair — the equal
unordered pair) is NOT flagged. Subject grains are unchanged: this is a
*lookup* refinement, not a key change. User-source proposals remain untouched
(ADR-0003 C).

### (d) — P5: ≥2 observed co-references before an agent VM/JP row is written (policy decided; implementation Wave 2)

Agent-sourced `value_mapping`/`join_path` proposals require **two observed
co-references** before a reviewable pending row exists: the first observation
writes a staging row (`mapping_id` prefix `staged#`, NO `status` attribute —
invisible to every lifecycle read, exactly the `artifact_usage` ledger idiom;
durable, never process-local); a second distinct observation within
`STAGING_TTL_DAYS` (default 30) promotes it to a real pending row whose
`query_context` names both observations. User-source and same-system
proposals bypass by construction (ADR-0003 C; a same-system observation is
single-system evidence, not a co-reference). Provenance bypasses (kept in
`self_learning._STAGING_BYPASS_PROVENANCES` — this list is the binding
alignment): `heuristic` (join_discovery — the measured value-overlap counts
ARE the evidence), `value_promotion` (the ADR-0043 sweep — promoted artifacts
already carry ≥ 3-value evidence), and `deterministic-response-integrity`
(Mode-A response-integrity typed proposals — each is bound to verified
current-turn evidence ids, so the ≥2-co-reference bar is met by
construction). This generalizes ADR-0043 D2's count-not-confidence
principle to propose time. **Implementation note:** the staging buffer lands
in the Wave-2 P-guards task (same owner, same module) together with P7, which
feeds the same corroboration count — the Wave-1 P-core task descoped it after
the scope check showed the buffer interacts with the ADR-0043 promotion
sweep's own emission path and with pinned suites outside P-core file
ownership. Descope is recorded in the P-core handoff; this section is the
binding design.

### (e) — P8: cross-site same-system-class proposals need an All-Sites authority (or a human)

At propose time, when `site_of(from_system) != site_of(to_system)` AND the two
systems share a `system_type` (same system class, different plants), the
concept must be owned by an All-Sites authority system or carried by a
steward/user assertion; otherwise the proposal is rejected with the stated
reason (site-local namespaces collide on format across plants). `site_of`
is imported from `agents/metric_multisite.py` — the canonical home
([ADR-0045](0045-multi-site-metric-fan-out.md)); it is never copied.
Unknown site metadata — or a missing/`unknown` concept (the ingest default
when no concept was derived) — ⇒ the guard does not fire (fail-open: this is
a proposal-quality gate, not a security gate); the All-Sites authority
concept comparison is casefolded/normalized on both sides. User source
bypasses (ADR-0003 C).
Lands in the Wave-2 P-guards task with the rest of the guard family.

> **Amended 2026-10-16 (onboarding identity rules, every class).** The
> onboarding Resolve step (the wizard's resolve job and the chat's
> `resolve_identities`) now withholds a proposed identity rule whenever the
> systems it names serve different plants, of ANY class
> (`agents/resolve_site_gate.py`). A site maintenance system's log `author`
> shared its four technician logins with the downtime `resolved_by` of every
> plant's MES, because every plant writes the same login names; only the same
> plant's MES names the same people. The same-class condition above did not
> cover that pair (CMMS and MES), and the concept pass's model rules were not
> gated at all. The P8 lever is unchanged: a steward-confirmed All-Sites
> authority for the rule's concept (`self_learning._authority_vouch_verdict`)
> lets the rule through; an unconfirmed one does not (ADR-0052 B4). Here only
> an authority whose owner is a KNOWN unsited system vouches
> (`known_owner_only`): an unknown owner or an empty system inventory vouches
> for nothing, where the proposal gate above keeps its fail-open reading. An
> unsited or All-Sites system has no plant, so its rules to any plant pass.
> The gate resolves a model-written target to one catalog system (an exact
> id, the one entry with that display name, or
> `resolve_site_gate.match_system_id`: exactly one id by containment, so
> `mes` for `mes-mil`) and writes that id onto each rule it keeps, so the
> identity-rule writer, which matches ids only and now uses the same
> containment rule, writes the system the gate judged. For a system that
> serves a plant, a target that names no system or more than one is withheld,
> and the writer refuses an ambiguous target instead of taking the first match.
> A withheld rule moves to `rejected_rules` with its reason, and the
> Identity Rules checkpoint lists withheld candidates under the kept rules as
> well as when none were kept. Self-learning proposals and the exact-agreement
> pass keep the same-class rule above.

> **Amended 2026-10-19 (self-learning proposals, every class).** Self-learning
> proposals now follow the same site scope as onboarding identity rules:
> `self_learning._gate_cross_site_namespace` fires for two systems at
> different plants whatever their `system_type`, so a crosswalk, value
> mapping, join path or identity rule between one plant's MES and another
> plant's historian or maintenance system is dropped (or flagged, when only an
> unverified All-Sites authority owns the concept). A workshop walk found the
> cross-system miner offering "Translate <field> in <plant A>'s MES to
> <field> in <telemetry system> at <plant B>"; the same-class condition
> let it through. Plants compare without case. Everything else in (e) is
> unchanged: unsited and All-Sites systems link to any plant, a
> steward-confirmed All-Sites authority lets the link through, user source
> bypasses, and missing site metadata, an absent or `unknown` concept and
> backend failures fail open. The exact-agreement pass keeps its own
> same-class check (`onboard._exact_rule_refusal`).

## Considered Options

- **Keep the insight grain and exempt insights from the ADR-0042 breaker by
  type-checking inside `_classify_collision`.** Rejected: still supersedes
  distinct findings (loses information a human should adjudicate) and adds a
  type special-case to the one collision classifier; the per-finding grain
  removes the churn at the key level with no classifier branching.
- **Auto-accept graduated correlations at high corroboration counts.**
  Rejected: violates the binding auto-accept-stays-off posture (ADR-0043 D5);
  graduation strengthens evidence, it does not replace review.
- **Reuse an existing grain (`join_path`/`identity_rule`) for the graduated
  correlation.** Rejected: a correlation is a narrative-backed pattern, not a
  deterministic translation rule; emitting it as a rule type would let the
  bulk accept paths write a fictitious JoinPath/IdentityRule from narrative
  evidence.
- **Widen subject grains to make `A→B`/`B→A′` collide directly.** Rejected
  for the same reason ADR-0042 rejected target-widening: it abolishes
  supersede semantics and orphans existing rows; a propose-time lookup with a
  stated review reason keeps the grain and surfaces the contradiction.
- **Hard TTL deletion of stale insight pendings.** Rejected: destroys the
  audit trail; a terminal `expired` status keeps the row queryable while
  removing it from review pressure.

## Consequences

- Distinct findings coexist as separate adjudicable pendings; the audited
  quarantine-growth pathology cannot recur (insight supersede no longer
  exists). Old-grain rows and stale pendings decay via the 30-day TTL — no
  migration.
- Repeated corroboration now has a destination: ONE reviewable `correlation`
  proposal carrying its measured support, entering the full ADR-0003/0042
  lifecycle. `Correlation` nodes can finally be produced by the learning loop
  (previously count 0) — but only through human accept.
- Reverse-direction contradictions surface on the review card instead of
  silently coexisting; consistent restatements stay quiet.
- `hierarchy_position` (positional-guess) proposals and unverified UUID
  targets can no longer auto-accept; a passing target check stamps
  `verified="true"` on the row so review surfaces can distinguish checked
  targets — no text may claim confirmation without it.
- Field-path normalization (`table.column` tail; raw/composite payload
  qualifiers stripped) makes aliased references of the same column collide at
  key time. Rows keyed under the old normalization are not migrated; they
  resolve/TTL out (stated backward-compat policy). The
  [ADR-0050](0050-vocabulary-and-artifact-management.md) `key_norm`/
  `key_digest` aliases are byte-unchanged.
- P5/P8 implementations land in Wave 2 (P-guards) against this ADR — do not
  write a second ADR for them.

## Related

- [ADR-0003](0003-unified-proposal-lifecycle.md) — lifecycle, grains, and the
  collision policy refined by (c).
- [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) — churn controls;
  amended by (a) for insights only.
- [ADR-0043](0043-per-value-to-rule-promotion.md) — the count-not-confidence
  evidence model generalized by (b) and (d); auto-accept posture unchanged.
- [ADR-0045](0045-multi-site-metric-fan-out.md) — `site_of` canonical
  home used by (e).
- [ADR-0050](0050-vocabulary-and-artifact-management.md) — the usage-ledger
  row idiom (d) reuses; the `key_norm`/`key_digest` seam kept intact.
