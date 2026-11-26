<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0066: Graph-backed metric definitions — formulas are data, learned through the one lifecycle, resolved deterministically

## Status

**Accepted — 2026-09-04.** Implemented across Waves 1–4 and accepted with
the final boundary guard tests and operating documentation.

Amended by [ADR-0078](0078-defined-metric-terms-answered-by-the-metric-layer.md)
(D7 result-lineage clarification: a per-turn metric-layer RESULT with its
executed lineage is delivery data, not a definition in prompt text).

Builds on [ADR-0003](0003-unified-proposal-lifecycle.md) (the ONE proposal
lifecycle every write flows through),
[ADR-0014](0014-metric-layer-design.md) /
[ADR-0015](0015-deterministic-metric-resolver.md) /
[ADR-0016](0016-derived-metric-resolution.md) (the code metric layer and the
deterministic resolver this ADR extends without editing),
[ADR-0050](0050-vocabulary-and-artifact-management.md) (the customization
boundary — nodes and their metadata are DATA, the edge taxonomy is ENGINE;
`AuthorityRule` is the one authority home),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(a learning writer ships with its read path and usage telemetry;
replace-not-accrete),
[ADR-0057](0057-semantic-layer-export-boundary.md) (rules engine, not a data
store — a definition is metadata about what a number MEANS, never production
data),
[ADR-0063](0063-proposal-precision-and-requirement-promotion.md) (D7: the
guidance-note delivery saga this artifact deliberately does NOT need, and the
telemetry-never-proves-obedience rule it inherits),
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no demo or customer literals in `agents/*.py` outside
`agents/demo/`), and
[ADR-0054](0054-measurement-honesty-and-experiment-harness.md) (untrusted steward
text is bounded and sanitized; honest absence beats fabricated claims).

## Context

The 23 seed metrics are code: frozen dataclasses in `agents/metric_layer.py`,
resolved deterministically by `metric_resolver.resolve_metric` through
`MAPS_TO` edges and `AuthorityRule` nodes. That code-seeded set is the
engine's vocabulary — and it is the ONLY metric vocabulary a deployment can
have. A plant that measures "line utilization" or any organization-specific
KPI has no way to define it after deploy: the term is undefined to the
pre-check (`metric_term_resolution` honestly says so), and the only fix is a
repo change — which the engine-neutrality rule (ADR-0044) forbids for any
customer-specific term, by design.

Meanwhile the machinery to close the gap already exists in pieces:

- **The lifecycle carries structured artifacts.** `concept_overlay` rides a
  JSON document in a proposal's `query_context`, is validated fail-loud at
  accept, and lands as a standalone graph node — exactly the shape a metric
  definition needs.
- **The graph already holds deployment vocabulary.** Graph-resident concepts
  (ADR-0050) extend the code-seeded core without forking it; the same
  extend-never-fork discipline applies to metrics.
- **Resolution is already deterministic and seam-isolated.**
  `get_metric_layer()` is the single seam behind all ten metric consumers;
  a composed layer behind that unchanged seam can serve graph definitions
  without a single resolver edit.

What is genuinely new — and what this ADR pins before code exists — is the
authority question. A metric formula is the highest-authority artifact this
system carries: it defines what a number MEANS. A wrong value mapping garbles
one translation; a wrong formula silently redefines every answer that touches
the metric. The lifecycle placement, collision policy, staleness posture, and
telemetry honesty below all follow from taking that seriously.

## Decision

### D1 — The `metric_definition` artifact: a formula is DATA, not code

A new proposal type `metric_definition` carries a canonical JSON document
(sorted keys — the byte form that is digested, stored, and read back):

```json
{
  "id": "<^[a-z0-9][a-z0-9-]{0,63}$>",
  "kind": "direct | derived",
  "label": "<1..128 chars, untrusted-text sanitized>",
  "synonyms": ["<each 1..64 chars, max 8, sanitized>"],
  "unit": "<0..32 chars>",
  "concept": "<direct only — must exist in core ∪ graph concepts>",
  "aggregation": "<direct only — closed Aggregation vocab>",
  "formula": {"op": "<closed FormulaOp vocab>", "inputs": ["<{metric}|{concept}|{value}|nested>"]},
  "prefer_stored": "<optional metric id>",
  "denominator_provenance": "<optional; requires a ratio op — same rule as code>",
  "valid_range": [0, 100],
  "max_exclusive": false,
  "window": "<optional; must be in metric_dimension.WINDOW_VOCAB>",
  "grain": "<optional advisory text, ≤128 chars>",
  "authoritative_system": "<optional advisory system id — display/validation hint ONLY (D8)>"
}
```

Caps: formula depth ≤ 6, total formula nodes ≤ 32, document ≤ 8 KiB.
Provenance (`source`, `created_by`, `version`, timestamps) is stamped by the
lifecycle — never user-supplied inside the document.

The document is **data, not code**, and that is the load-bearing property:

- The formula is an op-tree over the **closed** `FormulaOp` and `Aggregation`
  vocabularies the code metrics already use — no user-supplied expressions,
  no `eval`/`exec`, ever. Accepting a definition can extend WHAT is computed,
  never HOW computation works.
- Because it is data, it is **validatable fail-loud** by a pure, import-pure,
  stdlib-only validator (mirroring `metric_layer._validate_valid_range`
  semantics exactly, plus id charset, closed-vocab membership, formula caps,
  concept/metric reference existence against core ∪ graph, and **cycle
  detection** over the reference closure — the code seed is acyclic and graph
  definitions must be proven so). Label and synonyms pass through the
  untrusted-text sanitization posture (ADR-0054): hostile text never reaches
  the graph.
- Because it is data, it is **diffable**: a computed, structural, field-level
  diff with deterministically rendered before/after formula strings — what
  the steward reviews. Never LLM-judged (ADR-0053 posture).
- Because it is data, it stays inside the ADR-0057 boundary: a definition is
  metadata about what a value means — the rules-engine side of the line —
  never a production row.

An accepted definition becomes a **standalone `(:MetricDefinition)` node** —
node/metadata layer, ZERO new edge types (the ADR-0050 D1 customization
boundary; the `ConceptOverlay` precedent). MERGE keyed on `id`; a superseding
edit replaces in place with a version bump (replace-not-accrete, ADR-0052).

### D2 — Code wins every collision, and the collision is always VISIBLE

A `ComposedMetricLayer` wraps the code layer plus a TTL-cached graph snapshot
behind the **unchanged** `MetricLayer` ABC and the unchanged
`get_metric_layer()` seam — the resolver and all ten existing consumers work
with zero edits, and the 23 code metrics stay byte-identical (pinned by a
hash-stable guard test).

**Code metric ids always win an id collision, and a collision is never
silent.** The policy has three arms, all loud:

1. **Propose time:** a document whose `id` collides with a code metric is
   rejected 400, naming the field.
2. **Accept time:** the same collision raises `ValueError`; the accept fails
   and the row stays pending. (Fail loud at both doors — a proposal filed
   before a code metric shipped must not slip through at accept.)
3. **Read time:** a residual colliding graph row (a row predating the check,
   or seed drift) is SKIPPED by the composed layer with a `logger.warning`
   and reported as **`shadowed`** — with its reason — in the admin metrics
   list. It never resolves, and it never disappears from view.

**No override marker ships in v1 — deliberately.** Overriding a code metric
is a change to engine semantics that every deployment shares; that is a repo
change with review, tests, and the neutrality gate — not a graph write. An
override marker would turn the highest-authority artifact into the easiest
thing to silently redefine. If a real deployment need emerges, it gets its
own ADR with its own visibility design; until then the absence of the
mechanism IS the policy.

### D3 — Lifecycle: never auto-accepted from any source; accept = validate → MERGE → exact readback; NO delivery saga

`metric_definition` enters the ONE ADR-0003 lifecycle with the C-contract
keys: `subject_key = "metric_definition|" + key_norm(id)` (ONE live proposal
per metric id — a later edit supersedes a pending add) and
`target_key = subject + ">>" + key_digest(canonical JSON)` (idempotent
re-proposal dedupes; any content change is a real change).

**Nothing machine-sourced or human-sourced auto-accepts a formula:**

- `"metric_definition"` joins `_NEVER_AUTO_ACCEPT_TYPES` — a formula defines
  what a number means; nothing machine-sourced may apply one.
- The new source `"conversation"` (D5) joins `_NEVER_AUTO_ACCEPT_SOURCES`
  alongside `"user-feedback"` and `"answer-audit"`.
- The propose route has **no immediate-accept path even for admins** (unlike
  `assert_vocab`): acceptance always goes through the queue so the steward
  sees the computed diff. For the same reason `record_user_correction`
  **REFUSES** `proposed_type="metric_definition"` fail-loud, before any read or
  write — that entry point is accepted-on-entry and dispatches generically
  through `_apply_to_neptune`, which HAS a `metric_definition` arm, so an
  omitted branch would leave the one-door invariant resting on caller
  discipline. The refusal keeps it AT the door: corrections route through
  propose, where the preview/diff look is mandatory.
- Accept and reject stay the EXISTING proposal routes — one door, no new
  accept plumbing (ADR-0050 D2).

**Accept is validate → MERGE → exact readback.** The accept path re-validates
the document fail-loud against the current concept and metric universe (a
stale proposal referencing a retired concept raises; the row stays pending),
MERGEs the node, then **re-reads it and byte-compares the stored
`definition_json` to the intent**. A mismatch raises; the accept fails; the
row stays pending. Only a proven readback yields `accepted`.

**Explicitly: there is NO delivery-mirror saga, and that is correct, not a
shortcut.** The contrast with `guidance_note` (ADR-0063 D7) is the point:
a guidance note has TWO homes — the graph truth and the
`SystemGuidanceStore` delivery copy the agent actually reads — so its
acceptance needs CAS, `publish_exact`, byte-matched delivery readback, and a
compensation path for the window between them. A metric definition has
exactly ONE home: the graph node the composed layer and the pre-check read
through the TTL snapshot. There is no second store to drift from the first,
so there is nothing to CAS against and nothing to compensate — **the exact
readback IS the whole integrity proof.** Cargo-culting the guidance saga here
would add failure modes without adding a guarantee.

### D4 — Reads ride a TTL snapshot; staleness is bounded by the TTL; cold failure RAISES, warm failure serves stale

The composed layer and the metric-term pre-check resolve graph definitions
(ids, labels, synonyms) through ONE module-level TTL-cached snapshot
(default 300 s, env-tunable `METRIC_DEFINITION_TTL_SECONDS` — which must be a
**FINITE** positive number of seconds; `inf` is rejected as hard as `nan`,
because an infinite TTL is not a large bound but the ABSENCE of the very bound
this decision states as its staleness guarantee, and it would stop
cross-process convergence outright). Per-turn cost
is bounded by construction: **zero added graph round trips per turn** —
amortized at most one bounded read (`LIMIT 500`) per TTL window per process.
The staleness bound is the TTL: after an accept, the accepting process
invalidates in-process and every other process converges within one TTL
window.

The failure asymmetry is deliberate and is the honesty core of this decision:

- **Cold-cache store load failure RAISES.** The store must not fabricate an
  empty snapshot: a partial vocabulary presented as complete would let the
  pre-check claim a graph-defined term is *undefined in this deployment*, a
  fabricated absence claim delivered to the user as fact. The raise is
  unconditional — including inside the store's **cold-failure cooldown**
  (`COLD_FAILURE_COOLDOWN_SECONDS`, 60 s), which changes only WHERE the raise
  comes from. Two callers read the store DIRECTLY on every metric-shaped turn
  (the pre-check notice's vocabulary build and the D6 usage bump) and the
  composed layer's own cooldown covers neither, so without this a cold outage
  cost ~2 failing graph reads per turn forever. The first cold failure re-raises
  the graph's own exception and remembers it; callers inside the window raise
  from that remembered failure without touching the graph — one attempt per
  window across ALL callers, automatic recovery after it, and `invalidate()`
  clears it so an accept never waits out a cooldown. A WARM snapshot is never
  subject to it.
- **Warm-cache refresh failure serves the last good snapshot** and logs. A
  slightly stale definition set is honest (it WAS the truth, bounded moments
  ago); a fabricated "undefined" is not. Staleness is recoverable and
  bounded; a false absence claim in an answer is neither.

Downstream, the store's cold raise is contained honestly: the **metric layer
fails open to code-only**, where a graph-defined id yields only the normal
honest unknown-metric error and never an "undefined in this deployment" claim,
while the pre-check's notice fails open to empty — therefore a graph outage can
never fabricate either an answer or an absence claim. **A graph outage must
never let the system claim a term is undefined.** Rows that fail shape
validation or collide with a code id are excluded from the snapshot and
surfaced via the shadowed/invalid report (D2) — never silently served, never
silently dropped.

### D5 — Conversational capture files a PENDING proposal; the steward queue is the only door to authority

When the pre-check surfaces an unresolved term and the user answers with a
definition, the orchestrator captures it with an in-process tool that builds
the C1 document deterministically and files it through
`log_proposed_mapping` with source `"conversation"` — which lands **pending,
never applied** (D3 deny tuples, both arms). The tool's return text states
exactly that ("filed for steward review — not yet active") so the model
relays honest status, and the orchestrator rule is explicit: **do not use the
captured definition to compute an answer in this or any later turn until it
is accepted.** A captured formula has zero authority until the steward door
opens — the machine-claims-never-vouch posture (ADR-0052) applied to the
highest-authority artifact.

Two deliberate narrowings keep the capture honest:

- **No free-text fallback.** If the structured document does not validate,
  the tool returns the validation error to the model so it can ask the user
  a better question — it never files prose as a formula.
- **A synonym answer is not a metric.** When the user maps the term to an
  EXISTING concept or metric, the tool files a `concept_vocab` synonym
  assertion through the existing pending machinery — reuse, not invention.

### D6 — Telemetry proves delivery and composition, NEVER obedience

Usage telemetry for graph definitions inherits ADR-0063 D7 verbatim: a
usage-ledger bump records that a definition **resolved a value**
(`evidence="metric-resolve"`) or **resolved a pre-check term**
(`evidence="term-precheck-resolved"`) — delivery and composition events. No
evidence string, surface, or doc may claim the model "followed", "obeyed", or
"used" a formula in producing its answer; that claim is unprovable and stays
unmade. Bumps are fail-open and bounded: a ledger failure can never fail a
resolve or degrade a turn. This satisfies the ADR-0052 gate — the writer
(accept) ships with its read paths (composed layer, pre-check, admin list)
and ledger evidence it fired — and code metrics bump nothing (their usage
story is the existing card/rule machinery).

### D7 — Definitions never enter `query_guidance`; the 4,000-char budget is out of their path BY CONSTRUCTION

Metric definitions are structured data resolved deterministically through the
snapshot (D4) — they are never rendered into `query_guidance` prompt text,
never into the route seed, never into any per-turn prompt surface. The
4,000-character guidance budget (`guidance_acceptance.MAX_GUIDANCE_LEN`)
exists to bound a prompt surface that GROWS automatically as notes are
accepted; metric definitions never compete for it because they are not prompt
text at all — the budget is out of their path by construction, not by
discipline. A guard test pins this: accepting a `metric_definition` performs
zero writes to `System.query_guidance`, and no new module imports
`MAX_GUIDANCE_LEN`. This is also why the artifact scales where guidance
cannot: a deployment can accept hundreds of definitions without adding one
token to any prompt.

### D8 — `authoritative_system` is ADVISORY; `AuthorityRule` remains the one authority home

The optional `authoritative_system` field in the document is a display and
validation hint only — it informs the steward's review and the admin surface.
Resolution authority is decided EXCLUSIVELY by the existing `AuthorityRule`
machinery (`set_authority`, the ADR-0050 addendum lifecycle, the fail-loud
multi-rule posture). Accepting a definition never writes an `AuthorityRule`,
and the resolver never reads `authoritative_system`. Two homes for authority
would reintroduce exactly the flip-flop class the ADR-0050 addendum fixed.

### D9 — Scope discipline: what is deliberately NOT built

Recorded so implementers do not gold-plate and reviewers can hold the line:

- **No graph-backed Cards** — `card_layer` stays code-only; a new definition
  gets no dashboard card in v1 (cards are presentation; separate decision).
- **No new `FormulaOp` or `Aggregation` vocabulary** — the closed sets are
  reused verbatim; no user-supplied expressions, no eval.
- **No override marker for code ids** (D2) — overriding a code metric is a
  repo change.
- **No authority auto-write** (D8) — `authoritative_system` is advisory;
  `AuthorityRule` is the one authority home.
- **No route-seed or `query_guidance` embedding of definitions** (D7) — the
  seed budget (8,000) and guidance budget (4,000) are untouched.
- **No auto-accept of any kind, ever** (D3) — including admin
  immediate-accept on the propose route; the diff-look is mandatory.
- **No free-text-metric fallback in the capture tool** (D5) — an unparseable
  definition returns an error; prose is never filed as a formula.
- **No delivery-mirror saga for the metric accept** (D3) — there is no
  second store; the readback is the proof.
- **No LLM in validation, diffing, or scoring** — all computed (ADR-0053).
- **No RDF/ontology/export surface** — the ADR-0057 boundary stands.
- **No editing/retiring flow beyond supersede-by-new-proposal** — the
  subject-key supersede already gives replace semantics; retire is a later
  ADR if a real need emerges.

## Considered alternatives

- **Definitions as `query_guidance` text.** Rejected: a formula in prose is
  unvalidatable, undiffable, competes for the 4,000-char budget, and its
  "resolution" would be model obedience — exactly what D6 refuses to claim.
  Structured data resolved deterministically is the whole point.
- **A graph-side override marker for code metric ids.** Rejected for v1
  (D2): it makes the highest-authority artifact the easiest to silently
  redefine, and no deployment need has demonstrated it.
- **Auto-accept for high-confidence or admin-originated definitions.**
  Rejected (D3): a formula defines meaning; the steward diff-look is the
  sole content control, mirroring the guidance-note posture (ADR-0050 D2
  note) at strictly higher stakes.
- **Reusing the guidance delivery saga for the accept.** Rejected (D3):
  there is no second store; a saga against a nonexistent delivery copy adds
  failure modes and proves nothing the readback does not already prove.
- **Treating a cold store failure as a successfully empty snapshot.** Rejected
  (D4): the store and pre-check must not convert an infrastructure failure into
  fabricated "undefined in this deployment" claims. The metric layer's
  code-only degradation is different and accepted: it makes no absence claim
  and returns only the normal honest unknown-metric error for a graph-only id.
- **A per-turn graph read for the pre-check.** Rejected (D4): unbounded
  added latency and load on every turn; the TTL snapshot gives a bounded,
  stated staleness window instead.

## Consequences

- **Customer neutrality becomes real for metrics.** This is the consequence
  the feature exists for: a deployment defines its own metrics AFTER deploy —
  through the lifecycle, into its own graph — without a single customer
  literal entering this repository. The engine ships 23 neutral seed metrics
  and the machinery; the deployment's vocabulary lives in the deployment
  (the ADR-0044 neutrality rule and the ADR-0064 repository-boundary posture,
  extended from golden sets to metric vocabulary). Test fixtures stay
  neutral placeholders; the neutrality ratchet extends to the new modules.
- **Positive:** one audited lifecycle for the highest-authority artifact,
  with computed diffs at review; deterministic resolution unchanged (zero
  resolver edits, 23 code metrics byte-identical); collisions visible at
  every stage instead of silent at any; the pre-check stops honestly
  reporting gaps the deployment has already closed; usage telemetry tells
  stewards which definitions earn their keep (ADR-0052).
- **Negative / risks:** a bounded staleness window (TTL) between accept and
  cross-process visibility — stated, tunable, and honest rather than hidden;
  `shadowed` rows require a steward eye on the admin list (mitigated: they
  are loud in logs and labeled with reasons); the cold-raise posture makes
  first-load graph availability part of the pre-check path (mitigated: warm
  processes serve stale, and the notice fails open to silence, never to a
  false claim).
- **Verification:** offline — unit tests with fake query seams for the
  validator clauses (range, cycle, collision, caps, hostile text), the TTL
  and cold/warm asymmetry, the deny-tuple membership on both auto-accept
  arms, readback-mismatch-stays-pending, the code byte-identity pin, the
  collision-visibility pin, and the guidance-budget isolation pin (the unit
  lane is network-blocked; the Neptune graph is VPC-private,
  [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)). Deployed
  — the full smoke/goldens lanes byte-stable on the pre-check notice, the
  ADR-0052 learning-health gate showing the new artifact class with read
  path and ledger evidence, and a manual end-to-end of the loop: undefined
  term → notice → conversational capture → pending row → steward diff →
  accept → term resolves with no notice and a ledger bump.
