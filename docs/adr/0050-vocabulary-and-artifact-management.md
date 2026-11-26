<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0050: Vocabulary and deterministic-artifact management — one lifecycle, a fixed customization boundary

## Status

Accepted — 2026-07-21. Builds on
[ADR-0003](0003-unified-proposal-lifecycle.md) (the lifecycle every write
flows through), [ADR-0020](0020-single-source-of-concept-truth.md) /
[ADR-0021](0021-two-layer-concept-model.md) (concept sources of truth),
[ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) (the
evidence machinery reused at authoring time),
[ADR-0043](0043-per-value-to-rule-promotion.md) (agent-side generalization
into the same lifecycle),
[ADR-0046](0046-deterministic-response-evidence-contract.md) /
[ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) (the shadow
capture that usage instrumentation reads), and
[ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (the identity
groundwork the steward gate stands on).

## Context

The knowledge graph is a rules engine, not a data store: it holds the shared
concept vocabulary and the deterministic artifacts that translate between
systems. Today those artifacts accumulate almost entirely through **agent
learning** — onboarding mints field concepts
([ADR-0021](0021-two-layer-concept-model.md)), the learning loop proposes
`value_mapping`/`identity_rule`/`join_path` rows, and the promotion sweep
generalizes them ([ADR-0043](0043-per-value-to-rule-promotion.md)) — all
through the ONE proposal lifecycle
([ADR-0003](0003-unified-proposal-lifecycle.md): `pending → accepted /
rejected / superseded`, DynamoDB log, Neptune applied truth).

What a customer deployment cannot do yet is **manage this vocabulary as its
own**: assert a concept the plant actually uses, bulk-load an
organization-specific glossary, correct or retire a concept, curate a
system's query guidance, or see which artifacts actually earn their keep in
answers. Three gaps block that:

1. **No declared customization boundary.** Nothing states which parts of the
   graph a customer may edit and which parts are engine invariants — so every
   editing surface has had to be designed ad hoc, and a naive "graph CRUD"
   admin page would let a user break the traversal contract the resolver and
   orchestrator depend on.
2. **Human writes lack a uniform entry point.** `record_user_correction`
   exists for corrections and `POST /api/proposed-mappings/user-define` lets
   any authenticated user originate a proposal, but there is no
   human-assertion path for vocabulary itself (concepts, synonyms,
   descriptions, guidance notes) — and the acceptance routes are currently
   open to ANY authenticated user, so the review step that the lifecycle's
   trust model assumes is not actually enforced.
3. **No utility signal.** The Item-C loop counts rule usage
   (`agents/usage_tracking.py` → `record_rule_usage`), but concepts and
   guidance notes have no usage record, so a steward cannot distinguish a
   load-bearing artifact from noise.

## Decision

### D1 — The customization boundary: nodes and their metadata are DATA; the edge taxonomy is ENGINE

Customers edit the **node/metadata layer**: `Concept` (name, description,
synonyms, retired flag, provenance), `ValueMapping`, `IdentityRule`,
`AuthorityRule`, `JoinPath`, and `System.query_guidance`. These carry the
vocabulary and the deterministic translation knowledge — they are the
customer's domain language and are legitimately theirs to shape.

The **edge taxonomy** — `MAPS_TO`, `LINKS_CONCEPT`, `APPLIES_TO`,
`HAS_SCHEMA`, `HAS_FIELD`, and the rest of the graph-shape contract — is
**invariant engine structure**. It is what the resolver, the onboarding
spine, and the orchestrator's context queries traverse; no management
surface may create, rename, or repurpose edge types. Management code writes
node PROPERTIES and instantiates the existing edge types only. This is the
same discipline that keeps [ADR-0020](0020-single-source-of-concept-truth.md)'s
no-drift guarantee intact: the core vocabulary stays code-seeded; graph-side
edits extend it, never fork it.

### D2 — ONE write model: three origination sources, one ADR-0003 lifecycle, review gated on stewardship

Vocabulary and artifact writes originate from exactly three sources:

- **agent-proposed** (today's path — learning loop, onboarding mint,
  promotion sweep),
- **human-asserted** (new — a user states a concept, synonym, value mapping,
  or guidance note as fact),
- **correction-derived** (new — an edit to an existing artifact, entering as
  a correction the way `record_user_correction` already does).

All three flow through the SAME [ADR-0003](0003-unified-proposal-lifecycle.md)
lifecycle: propose → accept, supersede-on-conflict at the canonical subject
key, versioned applied nodes, one audit trail. There is deliberately **no
raw-CRUD path** for these artifacts — a direct-write admin surface would
recreate the two-paths problem ADR-0003 was written to kill.

Authorization splits by lifecycle stage:

- **Proposing is open to any authenticated user.** The existing
  `POST /api/proposed-mappings/user-define` route IS the open origination
  path and is not duplicated; new vocabulary-assertion entry points follow
  the same posture. Frontline knowledge should flow in with no ceremony.
- **Acceptance (and reject/edit/bulk-clear) is gated on an admin/steward
  check** — `request.state.is_admin`, derived from Cognito group membership.
  This closes the gap where acceptance routes were open to any authenticated
  user. The identity plumbing is the
  [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) groundwork
  (Cognito groups already ride the app session); a dedicated `adt-steward`
  group distinct from admin is an explicit follow-up (D7) — until it exists,
  admin is the steward.

Assertions by an admin/steward land `accepted` and apply immediately
(mirroring `record_user_correction`); assertions by other users land
`pending` for steward review.

**Accepted risk — `user-define` is acceptance-equivalent for ANY
authenticated user (demo posture).** The open origination route is not
merely a proposal path: it delegates to `record_user_correction`, so a
user-defined mapping lands `accepted`, is applied to Neptune immediately,
and "user wins" supersedes any active agent proposal on the same subject —
functionally equivalent to the steward-gated acceptance routes, without the
gate. This is deliberately retained in phase 1 and explicitly
risk-accepted: in the demo posture every authenticated user is a trusted
operator (Cognito login is the only tenancy boundary this sample deploys),
and closing it would remove the no-ceremony frontline-knowledge path D2
exists to protect. The production-grade resolution is NOT a bigger gate on
the route but the D2 split applied to it — non-admin `user-define`
assertions land `pending` for steward review — recorded as an explicit
follow-up (D7). Deployments that cannot accept this interim posture should
restrict app access to steward-equivalent users until that follow-up ships.

**Amendment 2026-10-13: follow-up 5 has shipped, and the accepted risk above
is closed.** `user-define` and `POST /api/metrics/correct-mapping` read the
same fail-closed `request.state.is_admin` verdict as the steward gate
(`steward_gate.is_steward`; an absent verdict is not a steward). A steward's
assertion keeps the accepted-on-entry path through `record_user_correction`.
Anyone else's goes through `self_learning.record_user_proposal`: the same
canonical keys filed `pending` with `method=user_proposed`, nothing
superseded, and no graph access at all, reads included (no target grounding
is attached, and the schema-backed field check is skipped on the route).
Those checks run at the steward's accept instead (`_user_proposal_refusal`:
each named system still onboarded, no field the schema knows is absent). A
pending `source=user` row never auto-accepts.

**Note — accepted guidance notes are prompt-injected content.** A system's
`query_guidance` (including learned notes accepted through this lifecycle)
is concatenated into the orchestrator's prompt context. Human steward
review at acceptance time is the SOLE content control on that channel —
there is no secondary sanitization or model-side filter between an accepted
note and the prompt. Steward-facing documentation must flag this explicitly:
reviewing a guidance note is reviewing text that will be injected into the
orchestrator prompt verbatim (D7 follow-up).

### D3 — Human assertions are evidence-checked at authoring time; informed override is allowed

A human assertion that binds vocabulary to data (for example "this field
carries this concept") is scored at authoring time by REUSING the
[ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) machinery
— `resolve_evidence.value_overlap` over sampled field values, scored through
`concept_suggest`'s evidence-confidence calibration. The measured score is
recorded on the proposal row (`evidence_score`, feeding `confidence`).

A weak score does **not** block the assertion: the user may proceed with an
explicit informed override, and the override is recorded verbatim alongside
the score. Rationale: the human may know something the data sample does not
show, but the disagreement must be visible to the steward reviewing the row
— evidence informs review, it does not veto humans. Scoring failures swallow
to "no score" (authoring must never break because sampling did).

### D4 — Pinning: accepted user assertions are never silently superseded by agent learning

[ADR-0003](0003-unified-proposal-lifecycle.md) refinement C already protects
user truth in the correction flow; this decision extends it to every
artifact this ADR covers. When an agent-sourced proposal collides (same
subject key) with an accepted `source='user'` row, the collision classifier
lands the agent proposal **`pending` with
`review_reason='conflicts-with-user-assertion'`** — never a supersede. The
steward conflict surface shows both sides; only a human can overturn a
human. Agent-over-agent collisions keep the normal latest-wins supersede.
This is the trust invariant of the whole write model: a system whose
learning can clobber explicit human assertions destroys the confidence that
makes humans willing to assert at all.

### D5 — Bulk import generalizes the pack-install path; single-concept authoring is the lifecycle; retire is soft and guarded

Two ingestion shapes, deliberately distinct:

- **Bulk import** (a customer glossary, dozens-to-hundreds of concepts) is
  the EXISTING concept-pack install path generalized: the same flat pack
  shape, provenance stamped `pack:custom:<slug>` on each Concept node's
  `source` property (the established pack provenance convention), id-match →
  skip-and-report (never overwrite an existing concept), per-row failure
  isolation with an inserted/skipped/failed report. Bulk import is the ONE
  sanctioned non-proposal write, exactly as bundled pack installs already
  are — it creates new nodes only and cannot touch anything accepted.
- **Single-concept authoring** (add/edit/retire one concept, propose a
  guidance note) goes through the D2 proposal lifecycle — per-item review is
  feasible and valuable at that grain.

**Retire is soft**: acceptance sets `c.retired = true`; the node, its
provenance, and its history remain. A retire is refused while the concept is
load-bearing — the same mapped-concept guard the pack-uninstall path uses
(`(c)<-[:MAPS_TO]-()` ⇒ refuse with an actionable message). Hard deletion of
vocabulary is not offered; retired concepts drop out of suggestion surfaces
but keep historical mappings interpretable.

### D6 — Usage instrumentation extends the Item-C loop and reads the ADR-0046 shadow capture; it must work in report-only mode

"Used in K answers (30d)" extends the EXISTING utility loop — the same
best-effort, post-turn hook that calls `record_rule_usage` — to concepts and
guidance notes: artifact references are extracted from the turn's tool
results and captured evidence, and counts land as day-bucketed atomic-ADD
rows in the existing proposals table (no status attribute, so lifecycle
reads never see them). Rule-id extraction REUSES
`usage_tracking.extract_rule_node_ids`; concept references come only from
typed capture fields, never regex over free text.

The evidence source is the [ADR-0046](0046-deterministic-response-evidence-contract.md)
capture machinery **running in shadow** — which
[ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) guarantees runs
in every mode, including the default `report-only`. Hard requirement: usage
instrumentation MUST function with `RESPONSE_INTEGRITY_MODE` unset or
`report-only`; any dependence on `enforce`-mode structures is a defect.
Like the rest of the utility loop it is ADR-0003 category-F best-effort:
failures swallow, a turn is never degraded by usage accounting.

### D7 — initial scope and explicit follow-ups

The initial scope ships: the boundary (D1), the write model with the steward gate on
acceptance (D2), evidence-at-authoring (D3), pinning (D4), bulk import +
soft retire (D5), usage instrumentation (D6), and admin surfaces for the
concept vocabulary, `ValueMapping` review, and `query_guidance`.

Explicit follow-ups — recorded here so implementers do not gold-plate:

1. **IdentityRule / AuthorityRule authoring surfaces.** The lifecycle
   already carries these types; dedicated authoring/review UI is deferred.
2. **Non-admin proposal UI.** The initial authoring surfaces live in the admin
   page; a proposal surface for non-admin users (who can already propose via
   the open API) is deferred.
3. **Bundled-pack refit onto the generalized import path.** The existing
   bundled concept-pack install keeps its current route in phase 1;
   refitting it onto the generalized import is a later consolidation.
4. **Dedicated `adt-steward` Cognito group.** The initial scope gates on the existing
   admin group; separating stewardship from administration is a follow-up
   on the [ADR-0049](0049-user-identity-propagation-app-signed-claim.md)
   identity groundwork.
5. **Non-admin `user-define` lands `pending`.** Closes the risk-accepted
   acceptance-equivalence of the open origination route (D2): apply the D2
   authorization split inside `user-define` itself, so a non-admin
   assertion enters the lifecycle as `pending` for steward review instead
   of applying immediately. Shipped 2026-10-13 for `user-define` and
   `correct-mapping` (see the amendment under D2).
6. **Steward docs must flag guidance notes as prompt-injected content.**
   Human review is the sole content control on the `query_guidance` →
   orchestrator-prompt channel (D2 note); the steward-facing documentation
   for the review surface must state this explicitly.
7. **`self_learning.py` module split.** The lifecycle module has absorbed
   proposal ingestion, acceptance/apply dispatch, corrections, guidance
   notes, usage recording, and sweep entry points; splitting it into
   cohesive submodules is deferred housekeeping, recorded here so the next
   wave does not treat the monolith as intentional.

## Considered alternatives

- **Direct graph CRUD for admins.** Rejected: recreates the two-write-paths
  problem [ADR-0003](0003-unified-proposal-lifecycle.md) exists to prevent —
  two audit models, no supersede semantics, no pinning — and an unguarded
  editor invites edge-taxonomy damage that D1 forbids.
- **A separate vocabulary store for customer terms.** Rejected for the same
  reason [ADR-0021](0021-two-layer-concept-model.md) rejected it: `MAPS_TO`
  edges reference concept identity in the graph, so a second authoritative
  store is two sources of truth — the drift
  [ADR-0020](0020-single-source-of-concept-truth.md) killed.
- **Evidence as a hard gate on human assertions.** Rejected: sampled value
  overlap is a strong signal, not ground truth
  ([ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) itself
  caps evidence confidence below certainty); blocking humans on it inverts
  the trust model. Score-and-record with informed override keeps the
  disagreement visible without vetoing domain knowledge.
- **Let stronger-evidenced agent proposals supersede user assertions.**
  Rejected: violates the ADR-0003 C source-aware protection and D4's trust
  invariant. Agent evidence lands as a visible conflict for a human to
  adjudicate, mirroring
  [ADR-0043](0043-per-value-to-rule-promotion.md)'s posture that promotion
  is evidence, not privilege.
- **Hard-delete for retired concepts.** Rejected: destroys the
  interpretability of historical mappings and lineage; soft retire plus the
  mapped-concept guard keeps the graph honest.
- **A new usage table (or CDK-owned resource) for the ledger.** Rejected:
  day-bucketed, status-free rows in the existing proposals table are
  invisible to every status-indexed read, need no infrastructure change, and
  follow the established `record_rule_usage` idiom.

## Consequences

- **Positive:** customers manage their vocabulary through one audited,
  versioned lifecycle with human truth pinned; the engine/data boundary is
  explicit, so management surfaces can be built (and reviewed) against a
  stated invariant; stewards see utility signal before curating; bulk
  glossary onboarding reuses a proven ingestion path with honest reporting.
- **Negative / risks:** the steward gate is a behavior change on previously
  open acceptance routes (low blast radius — the admin UI is the only
  caller, and its users are admins — but pinned by tests on both the 403 and
  the admin-200 paths). The usage ledger cohabits the proposals table:
  status-free rows must stay invisible to lifecycle reads (pinned by unit
  tests). Evidence scoring adds bounded sampling to authoring, best-effort
  and skippable.
- **Verification:** offline — unit tests for the lifecycle extensions,
  pinning classification, steward gate, import report shapes, and the
  report-only usage path (the unit lane is network-blocked; graph
  interactions are tested against mocked query seams). Deployed — bulk
  import and the vocabulary surfaces against a live environment, since the
  Neptune graph is VPC-private
  ([ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)).

## Addendum (2026-07-29) — T1.4: AuthorityRule lifecycle + multi-rule determinism

Two live-confirmed defects motivated this addendum: (1) the rule-lifecycle
routes matched IdentityRule nodes only, so a demonstrably wrong AuthorityRule
could not be corrected from any surface — despite D1 promising customers may
edit `AuthorityRule`; (2) `set_authority` encodes the system in the rule id
(`ar-{system}-{concept}`), so a competing rule for the same concept creates a
SECOND node, and `GraphAuthorityResolver.authoritative_system` returned
`rows[0]` of an unordered MATCH — authority flip-flopped per query.

**Lifecycle extension.** `_set_rule_lifecycle` (and
`POST /api/thread/rules/{rule_id}/deactivate|reactivate`) now covers
`AuthorityRule` as well as `IdentityRule` (IdentityRule behavior unchanged;
IdentityRule is matched first). Soft-disable only — a `status='deactivated'`
property plus `deactivated_by`/`deactivated_at` audit stamps; node deletion
stays outside the customization boundary (D1). The routes sit OUTSIDE the
`/api/admin` middleware prefix, so they carry an explicit steward gate that
fails closed (absent verdict == non-admin). Edge taxonomy is untouched
(engine-invariant, D1): lifecycle is node properties only.

**Multi-rule resolution fails loud.** Two ACTIVE AuthorityRules naming
DIFFERENT systems for one concept now raise `MetricResolverError` naming the
concept, the competing systems, and the remediation (deactivate the wrong
rule, or `set_authority`) — consistent with `_resolve_authority`'s existing
ambiguous-⇒-fail-loud posture. Duplicates naming the SAME system dedupe
deterministically. **This can surface pre-existing "ambiguous authority"
landscapes in existing environments as errors where a coin-flip answer was
silently returned before — that is intended honest behavior:** the conflict
was already corrupting answers nondeterministically; now it is visible and
actionable.

**Readers.** Decision-making readers filter deactivated rules in the query:
`metric_resolver.GraphAuthorityResolver` (the ADR-0008 seam behind metric
cards, dashboard, multisite, dimension drills) and
`self_learning._authority_vouch_verdict` (the P8 cross-site vouching read — a
deactivated authority never vouches). Presentation reads
(`/api/thread/graph-summary`, `/api/neptune/graph`) deliberately do NOT
filter: deactivated rules stay visible with their status and audit stamps
(the topology-drawer bucketing precedent from the IdentityRule lifecycle).

**Free-cypher reads are advisory-filtered only.** `explore_graph` advertises
`AuthorityRule` to the orchestrator's free-form cypher; no reader filter can
gate an ad-hoc MATCH. The deactivate write therefore sets a human-legible
status property that graph-view queries RETURN, and the metamodel's
`AuthorityRule` entry instructs readers to exclude
`status='deactivated'` — guidance, not enforcement. Anything that must hold
across models belongs in code; the enforced filters above are the code path.
