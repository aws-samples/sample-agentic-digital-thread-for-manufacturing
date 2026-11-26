<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0058: Post-provision cross-system mining and value-crosswalk evidence

## Status

**Proposed — 2026-08-07.** Written before implementation to pin the contract;
the mechanism ships in the same sweep behind this decision record.

Builds on [ADR-0003](0003-unified-proposal-lifecycle.md) (every candidate files
through the one proposal lifecycle),
[ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md) and
[ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) (join
discovery and its chance-overlap guards),
[ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) (churn quarantine —
the flood damper this mechanism must not defeat),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(a learning writer ships with its read path and usage telemetry;
replace-not-accrete), and
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — no dataset literals anywhere in this mechanism).

## Context

A graph-snapshot diff of a matured, production-like deployment against a fresh
re-onboarding of the same sources (the `scripts/graph_snapshot.py` instrument;
onboarding is temperature-0 deterministic, so the diff is pure signal) showed
that the highest-value knowledge in the graph is **steward-earned and never
rediscovered**: the one cross-system join path that carries every cross-system
answer, roughly ten per-value crosswalks bridging disjoint identifier
vocabularies, and promoted query guidance. Fresh onboarding reproduces schema
and concept mappings faithfully — and none of the above. A team redeploying, or
a new customer onboarding equivalent systems, starts from zero on exactly the
knowledge class that took weeks of steward interaction to accumulate.

Half the machinery already exists. `agents/join_discovery.py` discovers
cross-system value-overlap joins (transform ladder, both directions,
order-independent) and files `join_path` and `crosswalk` proposals with
computed evidence. But it runs only inside the optional resolve job — a system
provisioned without that step is never compared against its peers — and its
crosswalk emission fires only for name-similar shared concepts under an
already-established join. The steward-earned crosswalk class is per-VALUE
bridges (`from_value → to_value`) between fields whose vocabularies are
disjoint, which name similarity cannot find but bounded value sampling can.

## Decision

### D1 — Two triggers: post-provision, plus an on-demand steward route

Mining runs from `_run_provision_job` after the coverage-gap check (the same
best-effort, never-raise boundary — a raising miner must never fail
provisioning), **and** from a steward-gated on-demand route. The resolve-job
path keeps its existing discovery unchanged.

Rationale: resolve is skippable and runs before the system's schema is
provisioned into the graph; only a post-provision hook guarantees a second and
subsequent system is always compared against every prior one. Idempotency is
free — canonical-key dedup classifies re-runs as `dup_pending` — so
double-firing from both hooks costs only bounded sampling.

Rejected: a scheduled sweep as the only trigger. It surfaces knowledge hours
late for no benefit, and the coverage-gap precedent already rejected
"wait for the next sweep" for onboarding-adjacent findings.

### D2 — Value-crosswalk mining from the same bounded samples

The miner extends WHICH candidates reach the existing `crosswalk` grain; it
does not add a grain. From the same bounded server-side samples join discovery
already takes (`resolve_evidence.sample_field_values`, 200 values per field,
cached per run — reused verbatim, never re-implemented), it computes per-value
alignment candidates: affix-stripped equality after a transform-ladder rung,
and near-token pairs from the existing `crosswalk_candidates` similarity.
Digit-only and sequential-integer value shapes are refused (chance-overlap
guards carry over). Each candidate files as one `crosswalk` proposal — pending,
independently reviewable, direction-canonicalized, and **never auto-accepted**
(`crosswalk` is already in the deny-tuple).

Rejected: a new proposed_type for mined value bridges. The `crosswalk` grain is
value_mapping-shaped and already carries exactly these review semantics;
a parallel grain would split the steward queue for no distinction a reviewer
cares about.

### D3 — Evidence strings are computed, never LLM

Every filed candidate carries a computed evidence string in `query_context` —
counts and method, e.g. *"cross-system mining <date>: N of M sampled values
from <field A> align with <field B> after <transform> (e.g. […])"*. All overlap
math stays in pure helpers; no LLM appears anywhere in the mechanism. Raw
sampled values never enter an LLM token path (the claim-check policy): at most
five example values ride the proposal row, which is steward-facing data —
pending-proposal text is never composed into prompts.

Rejected: LLM-scored candidate plausibility. It would put production values
into a token path and make the evidence unreproducible; the golden-lane
discipline (computed verdicts only) applies to writers too.

### D4 — Hard cost and flood bounds

- Sampling: 200 values per field (the existing limit), cached per field per run.
- A per-run field-pair budget (`MINER_MAX_FIELD_PAIRS`) — when a wide schema
  exceeds it, the miner truncates deterministically (sorted order) and logs.
- Per-pair candidate cap (the existing `CROSSWALK_MAX_CANDIDATES`) and an
  absolute per-run filed-row cap, so one fresh onboarding can never re-flood a
  triaged queue. Structural dedup (`dup_pending`) and ADR-0042 quarantine
  remain beneath the caps as backstops.

### D5 — Read path and telemetry ship with the writer (ADR-0052)

Mined rows surface through the existing vocabulary review queue and the
existing bell `proposals` count — deliberately **no new bell kind**. The writer
bumps the artifact-usage ledger on file so the audit lane can prove it fired.

## Considered alternatives

- **Auto-accept high-support value crosswalks.** Rejected outright: a crosswalk
  is a human-confirmed bridge by definition; support counts from 200-value
  samples are suggestive, not authoritative, and the deny-tuple exists
  precisely so confidence can never become acceptance.
- **Mine at query time (lazily, when a join fails).** Rejected as the primary
  path: it puts sampling latency in the chat path and only fires after a user
  has already received a bad answer. (The failure-signal mechanism of
  [ADR-0059](0059-failure-signal-accumulator-and-runtime-steward-questions.md)
  covers the runtime side without sampling.)
- **Ship the matured graph as a seed artifact instead of re-mining.** Rejected:
  the artifact is customer-specific applied truth and would violate the export
  boundary posture ([ADR-0057](0057-semantic-layer-export-boundary.md)) as well
  as engine neutrality; mining regenerates candidates from the customer's own
  data, which is the only neutral source.

## Consequences

- **Positive:** a second onboarded system always gets compared; the knowledge
  class the snapshot diff proved unreproducible becomes *pending candidates*
  after a fresh onboard — still requiring steward accept clicks, which is the
  design, not a gap. Measurement is built in: the snapshot-diff instrument
  scores how much of the known steward-earned set the miner recovers.
- **Negative / risks:** more pending rows per onboard (bounded by D4 and
  asserted ≤ 25 per fresh onboard in the deployed-env verification); sampling
  cost on wide schemas (bounded, truncated deterministically); a candidate the
  heuristics cannot align stays invisible — the mechanism raises the floor, it
  does not promise completeness.
- **Verification:** pure-helper table tests (alignment, caps, refusals,
  evidence format, determinism); provision-job tests proving the never-raise
  boundary and idempotent re-runs; the neutrality ratchet runs over all new
  code; deployed-env check with the full smoke and audit lanes.
