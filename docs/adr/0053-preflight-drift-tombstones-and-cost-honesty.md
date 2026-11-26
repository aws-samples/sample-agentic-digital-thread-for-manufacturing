<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0053: Onboarding preflight verdicts, schema drift as proposals, rejection tombstones, and cost honesty

---
Status: accepted

Related: builds on [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(the writer-ships-with-its-read-path contract and the "machine claims never
vouch for machine claims" invariant, both of which bind every decision here)
and [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) (the D3
rejection-damping and D4 `review_reason` review-card channel that decision (c)
extends); honors [ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md)
(the flag-not-drop posture for proposal-quality gates) and
[ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (steward routes
fail closed — the reopen route added here rides `_check_steward` like its
siblings).
---

## Context

The product-hardening program adds five capabilities in one sweep: an
onboarding **preflight** (E1), a **golden eval lane** (E2), **schema-drift
detection** (E5), **negative learning from rejections** (E6), and a
**cost-per-answer** surface (E7). Each one is an opportunity to fabricate —
a verdict that overpromises, a score an LLM hallucinated, a graph mutation
nobody reviewed, a suppression nobody can see, a dollar figure nothing
measured. This ADR pins the honesty posture for all five before any of them
ships.

Motivating failures:

- **The lake-athena failure class.** An AssumeRole that works for the Glue
  catalog but grants no data read (or leaves struct paths unverifiable)
  passed connection setup and then silently degraded discovery — the only
  trace was a log line (`"Athena physical-column discovery skipped for
  {tbl}: …"`, `agents/onboard.py:4117`). The operator learned about it from
  wrong answers, not from onboarding.
- **Rejections taught the system nothing.** A steward could reject a
  proposal and watch the same shape re-propose on the next observation;
  only the narrow ADR-0042 D3 damping window (≥2 rejections in 7 days)
  suppressed repeats, and it suppressed them *silently*.
- **No deterministic answer-quality gate.** `smoke_prompts.py` proves
  grounding and `audit_learning.py` proves graph health, but nothing pinned
  expected *answers* — regressions in answer content surfaced only in
  manual review.
- **No cost signal at all** — and the tempting fixes (estimated tokens,
  dollar math) are fabrications of exactly the kind the honest-data rule
  (ADR-0045) exists to prevent.

## Decision

### (a) — E1: preflight is an async job that BLOCKS on connection failure, WARNS on degradation

`probe_connection` (connection + AssumeRole check, `agents/connection_probes.py`)
already exists — the preflight adds the missing layers: per-table **sample
reads** and a **completeness score**, surfaced BEFORE discover→map runs.
Sample-reading every table of a large source can exceed 60 s, and CloudFront
caps requests at 120 s, so the preflight is a `JobStore("preflight")` async
job with a fast poll GET (the reset/try-it pattern), never a blocking POST.
Verdict semantics (pinned):

- `blocked` — the connection/role probe failed (`assume_role_denied`,
  unreachable, bad credentials). The UI refuses to start discover; the API
  refuses nothing (a steward can still force discover via the existing
  route — **preflight is a diagnostic gate, not an authz gate**).
- `degraded` — connection OK but ≥1 table sample-read failed, or Athena
  physical-column discovery would be skipped (the lake-athena failure
  class: AssumeRole works for the catalog but grants no data read, or
  struct paths unverifiable). The UI shows an honest warning with
  per-table detail; proceed is allowed.
- `ready` — all probes and sample reads passed.

Sample reads are BOUNDED (LIMIT 1 per table / one asset per model / one
node list per MCP-i3X source) and SERIAL (the telemetry-serial invariant
generalizes). No sample VALUES appear in the result — presence/error class
only (the rules-engine constraint: production data never enters the graph
or the job record), and the payload carries no ARNs, hosts, or secrets
(the `probe_connection` echo posture). The preflight never writes to the
graph or the catalog.

### (b) — E5: schema drift is a proposal class, never a graph mutation

Scheduled (daily, `DRIFT_CHECK_INTERVAL_SECONDS`, `0` disables) and
on-demand re-discovery reuses `onboard_discover` AS-IS — it is read-only
with respect to the graph (verified: graph writes happen only in
provision). The diff compares fresh connector tables against
`schema_check.load_system_schema` (the graph's onboarded schema, which
carries `data_type`, so retype detection is possible). Each drift finding
files ONE pending proposal through `log_proposed_mapping` — the ingestion
gates apply, dedup applies — under a NEW `proposed_type="schema_drift"`
with its own proposal-key grain: subject `(from_system, from_field)`
(`table.column`, or `table.` for table-level events); target adds a digest
of the drift descriptor, so the same unresolved drift never duplicates
(dup_pending) while a type-flap files a distinct row.

The steward resolves drift in the same consequence-ordered queue as every
other proposal. Accepting a `schema_drift` row is an **acknowledge-only**
accept: NO graph write (an explicit branch in `accept_mapping`); the
acknowledgment optionally triggers the existing
`reconcile_blocked_proposals` cleanup for removals. **Re-onboarding — the
real fix for drift — stays the steward's explicit action.** Drift detection
never auto-mutates the graph, full stop.

**Amended 2026-09-26 by [ADR-0077](0077-additive-schema-repair-on-drift-accept.md):**
the acknowledge-only accept holds for `column_removed`/`column_retyped`/
`table_removed`; re-onboarding stays the fix for destructive drift. Accepting
`column_added`/`table_added` now MATERIALISES the column/table through the
ONE physical-field writer shared with onboarding (`agents/schema_fields.py`)
— additive drift is repaired on accept. Detection still never mutates the
graph; only the steward's accept does.

Skips are honest and per-system: no baseline in the graph ⇒
`skipped:no-baseline`; a discover error ⇒ `skipped:<error class>` — never a
fabricated empty diff. The read path (ADR-0052 writer contract): the
steward queue reads the proposals, the digest gains a drift section, and
audit probe 8 reports pending-drift ages (report-only by default; `--strict`
gates on age).

### (c) — E6: tombstones ARE the rejected rows; flag-not-drop; 90-day decay

No new registry: rejected rows already carry `target_key` (the content-hash
shape) and the subject-GSI query in `_lpm_classify` already fetches them.
The tombstone consult therefore lives in `_classify_collision` — NOT in
`_lpm_ingestion_gates`. The design's "new gate in the pinned order" is
honored in SPIRIT; the pinned gate order runs PRE-canonical-key and cannot
see rejected rows, so the mechanically correct slot is the classifier —
recorded here as a deliberate deviation. New action
`("tombstoned", None, rejected_rows)`: an exact target-key match on a
rejected row (1+ rejections) whose latest rejection is within
`TOMBSTONE_TTL_DAYS` (default **90**, env-overridable) and which did NOT
already trip D3 damping ⇒ the proposal still inserts PENDING but flagged
with the exact review_reason
`"re-proposal of a shape the steward rejected on <date> (mapping <id>)"`.
Order inside `_classify_collision` (pinned): user-source bypass →
dup_pending/noop → **D3 rejection_damped (unchanged, still drops)** →
**tombstoned (new, flags)** → the rest unchanged.

**TTL/decay analysis:** permanent tombstones suppress later-correct
proposals — schemas evolve, and a shape rejected against last year's data
model may be right today. Full drop-on-first-rejection would also silently
hide steward-relevant signal (suppressions must
be visible). The chosen posture is **flag-not-drop with a 90-day decay** —
nothing is ever silently lost (the row reaches pending WITH the
prior-rejection provenance so the steward can dismiss it in one glance),
repeat offenders still hard-drop via the existing D3 damping (≥2 rejections
in 7 days), and after 90 days the flag decays to a clean re-proposal. The
flag text carries the rejection date so recency is judged by a human, not a
heuristic.

**Vouching-invariant analysis:** a tombstone is a STEWARD (human) claim —
`status="rejected"` is written ONLY by `reject_mapping` behind
`_check_steward` (pinned by a new test; the ingestion gates DROP without
writing rejected rows). A human claim gating a machine proposal does not
violate ADR-0052's "machine claims never vouch for machine claims" — the
invariant binds in the opposite direction: **the machine must never mint a
tombstone**, and E6 adds the test that pins it.

**Escape hatches (pinned):** `source=="user"` (user-define /
`record_user_correction`) bypasses tombstones entirely (the existing
user-wins branch, untouched). "Re-open" = a new steward route
`POST /api/proposed-mappings/{id}/reopen` flipping a REJECTED row back to
pending — the row itself becomes reviewable again and, being pending, it no
longer matches the tombstone predicate (rejected-status rows only).
**Staging interplay (pinned):** `staged#` rows have no status and never
match the tombstone predicate; the promotion path re-enters
`log_proposed_mapping` → `_classify_collision`, so a promoted co-reference
of a tombstoned shape gets flagged like any other — staging cannot
resurrect a rejected shape.

### (d) — E7: cost = measured orchestrator tokens only; no dollars; absence = absent

strands 1.42.0 VERIFIED surface:
`AgentResult.metrics.accumulated_usage` carries
`inputTokens`/`outputTokens`/`totalTokens` (ints, Required, per the `Usage`
TypedDict in `strands/types/event_loop.py`), and
`accumulated_metrics` carries `latencyMs`. What is NOT measurable:
system-agent (inner runtime) model usage — those are separate AgentCore
runtimes behind the gateway; their tokens never reach the orchestrator's
`AgentResult`.

Forbidden, permanently:

- **Fabricated estimates** — no per-system extrapolation of unmeasured
  usage.
- **Dollar conversion** — pricing varies by Region and contract; users go
  to the AWS Pricing Calculator. No `usd`/`dollar`/`price` field exists
  anywhere in the payload.
- **Zeros pretending to be measurements** — the additive
  `intelligence_summary.cost` key is ABSENT when usage could not be read
  (the ADR-0045 honest-data rule: absence = absent).

Scope is stamped on the payload (`"scope": "orchestrator-model-only"`) and
rendered in the UI copy, so a reader can never mistake the number for total
platform cost. Tool-call/Neptune counts already exist in the summary
(`total_tool_calls`, `neptune_queries`, `system_queries`) — the cost
surface reuses them, never recounts. Trend: a day-bucketed cost ledger (the
`artifact_usage` idiom — best-effort swallow, a ledger write never fails a
turn) feeds a `cost_trend` section in the steward digest, which is the
ledger's read path (ADR-0052 writer contract).

### (e) — E2: golden evals are deterministic-only; goldens live in the demo layer

The scorer is COMPUTED, never LLM-judged (the determinism invariant — the
existing smoke-scorecard idiom). An LLM judge is explicitly OUT of v1; if
ever added it may only ANNOTATE flagged failures, never produce the score
or the exit code. Golden questions with dataset literals live in
`agents/demo/golden_evals.json` (the demo layer — core stays literal-free);
the engine `scripts/eval_answers.py` is generic and takes `--golden-set`.
The lane COMPLEMENTS `smoke_prompts.py` and `audit_learning.py` — it never
replaces either. Trend = the scorecard JSON diffed against a `--previous`
report; the GATE is against golden expectations only — a previous-run diff
is informational, because gating on run-to-run deltas flakes.

## Considered options

- **E1 as a synchronous POST.** Rejected: sample-reading a large source
  exceeds the CloudFront 120 s cap and the frontend's 30 s SWR timeout;
  the async-job-plus-poll pattern already exists (reset/try-it) and is the
  standing rule for any endpoint that can exceed ~60 s.
- **E1 verdict as an authorization gate.** Rejected: the preflight is
  diagnostic — the existing discover route stays reachable so a steward can
  override a false `blocked`; refusing at the API would turn a probe bug
  into an onboarding outage.
- **E5 auto-applying drift to the graph.** Rejected outright: the graph is
  a steward-reviewed rules engine; an unreviewed mutation path would bypass
  the entire proposal lifecycle (ADR-0003) and make probe-1-style
  regressions untraceable. Proposals-with-gates reuses the whole existing
  review surface for free.
- **E6 as a new ingestion gate in the pinned gate order.** Rejected on
  mechanics: the gates run PRE-canonical-key and cannot see rejected rows;
  the classifier already fetches them. Documented as a deliberate deviation
  from the session's framing.
- **E6 permanent tombstones / drop-on-first-rejection.** Rejected: see the
  TTL/decay analysis in decision (c) — schemas evolve, and silent
  suppression is how gaps stay invisible.
- **E7 estimated or dollar-denominated cost.** Rejected: see decision (d) —
  every alternative to measured-tokens-only is a fabrication.
- **E2 LLM-judged scoring.** Rejected: verdicts must be reproducible and
  free of judge drift; the determinism invariant already bans LLM-produced
  scores for the same reason (`agents/determinism.py`).

## Consequences

- Onboarding failures of the lake-athena class surface as an honest
  `blocked`/`degraded` verdict with actionable per-table detail BEFORE
  discovery, instead of as silent degradation found via wrong answers.
- Schema drift becomes visible, reviewable work in the steward queue with
  ages audited by probe 8 — and the graph gains no new mutation path.
- A steward rejection now has teeth: one rejection flags re-proposals with
  named provenance for 90 days, repeat offenders still drop via D3, and
  nothing is ever silently suppressed. The machine cannot mint a tombstone;
  the steward can always re-open one.
- Cost numbers can be trusted precisely because they are narrow: measured
  orchestrator tokens or nothing, scope stamped everywhere the number
  appears.
- Answer regressions fail a deterministic lane (`scripts/eval_answers.py`)
  that runs offline against pinned expectations and live against both
  environments — complementing, never replacing, the grounding and
  learning-health gates.

## Related

- [ADR-0003](0003-unified-proposal-lifecycle.md) — the proposal lifecycle
  drift findings ride through.
- [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) — D3 damping
  (unchanged, still drops) and the D4 `review_reason` channel the tombstone
  flag writes to.
- [ADR-0045](0045-multi-site-metric-fan-out.md) — the honest-data rule
  (absence = absent, never fabricated zeros) that decision (d) extends to
  cost.
- [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) —
  fail-closed steward gating; the reopen route follows it.
- [ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) — the
  flag-not-drop posture for proposal-quality gates.
- [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
  — every writer ships with its read path; machine claims never vouch for
  machine claims; the audit gate probe 8 extends.
