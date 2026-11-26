<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0059: Failure-signal accumulator and runtime steward questions

## Status

**Proposed — 2026-08-07.** Written before implementation to pin the contract;
the mechanism ships in the same sweep behind this decision record. The
`steward_question` proposal grain and its never-auto-accept registration land
with this ADR (Wave 0); the accumulator, templates, routes, and bell source
follow in later waves.

Builds on [ADR-0048](0048-identifier-miss-reactive-translation-loop.md) (the
identifier-miss marker contract — the failure taxonomy's richest signal),
[ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md) (post-turn elicited
learning, scoped), [ADR-0054](0054-measurement-honesty-and-experiment-harness.md)
(steward text is untrusted: bounded, secret-shape-rejected at write, fenced at
compose, zero evidence weight), [ADR-0003](0003-unified-proposal-lifecycle.md)
(all answers land as proposals), [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md)
(churn guards), and [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(the acknowledge-only accept posture and the 90-day decay precedent this grain
reuses).

## Context

The guided context interview asks a steward good questions exactly once — at
onboarding, before the system has seen a single real query. Everything learned
afterwards arrives passively: the identifier-miss marker fires per turn and is
consumed by the reactive-translation loop, empty cross-system joins are
classified per turn by `query_intelligence.is_failed_result`, and corroborated
feedback-exploration gaps file insights. **Each signal is consumed in its turn
and discarded.** A terminology gap that breaks the same cross-system join three
times a week produces three transient retries and zero durable questions —
while the matured-deployment snapshot diff (see
[ADR-0058](0058-post-provision-cross-system-mining-and-value-crosswalk-evidence.md))
shows that precisely this steward-elicited knowledge is what fresh deployments
lack. The product should notice recurring failure shapes and *ask*.

## Decision

### D1 — The accumulator is operational state: DynamoDB, not the graph

Failure counters are operational bookkeeping, not semantic knowledge. The graph
is a rules engine that never stores production data, and per-turn failure
counts would breach that boundary. The accumulator reuses the existing
self-learning DynamoDB table with a distinct key prefix
(`failure_signal#<shape_key>`) — no new infrastructure.

Rejected: `JobStore` — per-replica, fail-soft KV (its non-atomicity is
documented at its feedback-exploration call site); counters must survive
replica routing. Rejected for now: a dedicated DynamoDB table — acceptable
fallback if key-prefix cohabitation proves too clever in review, but it costs a
CDK change this sweep explicitly avoids.

### D2 — Failure shapes are deterministically classified; no LLM

Three shapes at v1, each computed from signals that already exist at defined
hook points:

- `terminology_miss` — the ADR-0048 identifier-miss marker observed for the
  same (system, term) — hooked where the orchestrator already parses the marker;
- `empty_join` — a cross-system turn where one leg returns an empty/failed
  envelope while the other returns rows — hooked at the existing post-turn
  usage-recording site, classified by a pure helper;
- `unit_confusion` — repeated corroborated feedback-exploration gaps naming the
  same concept — read from existing `insight` rows, no new hook.

Each observation increments a counter row keyed
`shape|system|subject-digest`, deduplicated per turn (one turn never
double-counts), with `first_seen`/`last_seen`/`count` and a **90-day TTL**
(the tombstone-decay precedent — stale failure evidence must not generate
questions forever). Hooks are best-effort single writes: a store failure logs
and drops, never touching chat-path behavior.

### D3 — Threshold crossings file a `steward_question` through the one lifecycle

When a shape row crosses its threshold (default: three distinct turns), a sweep
generates ONE question from a declarative, placeholder-shaped template set
(the context-interview bank gains a runtime scope; the neutrality ratchet
applies) and files it via `log_proposed_mapping` under a **new diagnostic
grain, `steward_question`** — so dedup, ingestion gates, churn quarantine, and
rejection tombstones all apply unchanged.

Grain (in `proposal_key._DISPATCH`): subject `(from_system, concept, reason)` —
the failure-shape key (`reason` carries the shape) — and the RHS digests the
question text. One pending question per shape-subject by construction; a
reworded question on the same subject supersedes; an identical re-file is
`dup_pending`.

The pending row is a QUEUE ENTRY, not the answer channel. Accepting a
`steward_question` is **acknowledge-only** (the schema_drift/coverage_gap
branch in `accept_mapping`): it means "seen", performs **zero graph writes,
ever**, and stamps no applied-node id. The grain sits in the
`_NEVER_AUTO_ACCEPT_TYPES` deny-tuple, which is checked before the allowlist in
both auto-accept paths — a future allowlist edit cannot silently admit it.

Rejected: reusing the `insight` grain. Insights are narrative findings with
different review semantics, and the notification bell needs to distinguish
"the system has questions for you" from "proposals await review".

### D4 — The answer path is the existing landing machinery, unchanged

Answers route through `context_interview.landing.land_answer`: sanitized and
validated at write (ADR-0054), business context lands as draft only, value and
term answers land as pending `value_mapping`/`concept_mapping`/domain-note
proposals. **No auto-application anywhere.** Answering marks the question row
`superseded` — the machine's only legal terminal status — with
`superseded_reason="answered"` and the landed proposal ids recorded on the row.

Rejected: a bespoke answer store for runtime questions. It would duplicate the
sanitize-at-write and draft-only invariants and create a second path steward
text could enter the system through.

### D5 — One bell source; bounded question volume

A new `questions` source in the notification bell counts pending
`steward_question` rows as one notification ("The system has N question(s) for
you"). Volume bounds: no filing below threshold, a per-sweep filing cap, one
pending question per shape-subject by grain construction, and the sweep
supersedes questions whose underlying signal has decayed or resolved
(replace-not-accrete, ADR-0052). The writer bumps the usage ledger on file.

## Considered alternatives

- **Ask immediately, in-turn, when a failure is detected.** Rejected: a single
  failure is noise (transient outages, typos); interrupting an answer to
  interrogate the user optimizes for the machine's curiosity over the user's
  task. Accumulate-then-ask trades latency for precision.
- **An LLM classifier for failure shapes.** Rejected: the taxonomy is small,
  the signals are already structured, and computed classification is the only
  kind that can be pinned by offline tests (the ADR-0054 discipline).
- **Auto-generate and auto-accept the fix when the answer seems obvious.**
  Rejected outright — every landing terminates in a pending proposal; the
  never-auto-accept deny-tuple on the question grain and on the landed types is
  the enforcement.
- **Store counters in the graph as diagnostic nodes.** Rejected: violates the
  rules-engine boundary, and failure counts over production queries are
  operational telemetry, not translation metadata.

## Consequences

- **Positive:** recurring, real failure patterns become a short, bounded queue
  of well-formed questions with evidence behind them — the interview continues
  for the lifetime of the deployment instead of ending at onboarding. Every
  answer lands as reviewable proposals in the existing queue.
- **Negative / risks:** two new best-effort hooks in the turn path (bounded to
  single conditional writes; pinned by tests that make the store raise);
  question fatigue if thresholds are tuned too low (bounded by D5's caps and
  the 90-day decay); the shape taxonomy is deliberately small and will miss
  failure classes it does not model.
- **Verification:** shape-key stability, per-turn dedup, threshold semantics,
  TTL attribute, per-sweep cap, steward-wins conditional writes, and the
  never-raise boundary — all offline; deny-set tests prove `steward_question`
  can never auto-accept and its accept writes nothing to the graph; the
  deployed-env lane seeds deliberately failing prompts and watches the bell.

## Amendment 2026-09-24 — questions are asked IN the onboarding chat; empty-join needs a real empty leg

- **Surface.** The answer surface is no longer a side panel. The bell deep
  link (`/#onboard/questions`) opens a `runtimeQuestions` entry in the
  onboarding chat flow where the onboarding agent asks ONE pending question at
  a time; answers render as user bubbles and the landing outcome as the
  agent's reply. D4 is unchanged: the same `runtime-questions` /
  `runtime-turn` endpoints and landing machinery, no auto-application,
  rejections relayed verbatim and re-askable; "Skip for now" posts nothing.
- **Classifier honesty (affects D2 shape 2).** `is_failed_result` is now
  envelope-scoped (word-boundary needles on the result's head only, never on a
  result carrying a data table). Before, an honest Mode-A prose answer that
  mentioned "no results" anywhere counted as the empty leg of an empty join
  (observed live 2026-09-24 on an ERP answer backed by eight successful
  queries), which could file questions from non-failures.

