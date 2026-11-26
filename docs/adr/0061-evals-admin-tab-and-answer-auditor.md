<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0061: Evals admin tab (centralized golden runs) and the deep-context answer auditor

## Status

**Accepted — 2026-08-09.** Design reviewed 2026-08-07; Wave 1 (scoring-core
extraction, synthetic markers, the auditor script) shipped first and Wave 2
(server runner, admin routes, the tab) lands in the same sweep behind this
decision record.

Builds on [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md)
(measured-only cost; the annotate-never-gate posture; computed verdicts — an
LLM never produces one), [ADR-0054](0054-measurement-honesty-and-experiment-harness.md)
(measurement honesty; the informational-by-default A/B diff),
[ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
(replace-not-accrete for generated artifact classes),
[ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(no demo literals in `agents/*.py` outside `agents/demo/`), and the
feedback-exploration runner precedent (`agents/feedback_exploration.py` —
JobStore lock + daemon thread + internal chat-client stream).

## Context

The golden eval lane (`scripts/eval_answers.py`, ADR-0053/0054) is a CLI-only
lane: a developer runs it from a laptop through CloudFront. That gives it three
structural gaps. First, a steward without a checkout cannot see evaluation
health at all — the scores live in a JSON file on whoever's machine last ran
the lane. Second, a full golden run takes minutes, and the CloudFront VPC
origin's `read_timeout` is 120 s — any naive "run it from the server on a
button press" blocking POST reproduces the documented offboard-504 failure
class (the server keeps working while the client sees a 504). Third, eval
questions are synthetic traffic; if they flow through the interactive path
unmarked, the learning writers and AgentCore Memory would train on the test
set.

Separately, the gating lanes say *what* failed but not *why*. A failed golden
carries deterministic failure annotations, but connecting a wrong claim to its
root cause (a missing mapping, a schema drift, a prompt gap) is manual
archaeology across traces, rules, and schema exports.

## Decision

### D1 — Server-side golden runs are async JobStore-backed jobs with a one-run lock

`agents/eval_runs.py` (modeled on `feedback_exploration.py` throughout) runs
the suite in a daemon thread: `POST /api/admin/evals/run` claims the active
slot and returns 202 with a `run_id` immediately — the 120 s CloudFront origin
timeout never bites because no request ever waits on the run. Status/progress
live in `JobStore("eval-run", ttl 4 h)` and finished reports in
`JobStore("eval-report", ttl 30 days)` — both on the existing
onboarding-sessions table, no new infrastructure — so a poll surviving a
replica re-route still resolves. The one-run-at-a-time lock is the
`_ACTIVE_KEY` check-then-set with stale-slot takeover (the same documented
non-atomic tradeoff as feedback_exploration); a second start while active is a
409. The run history is a single bounded `index` item — the newest 50 run
summaries, rewritten whole on each completion (replace-not-accrete,
ADR-0052), size-guarded under the DynamoDB item limit.

Server runs stream through the internal chat client
(`AgentCoreChatClient`/`LocalChatClient`, the sanctioned server-side
synthetic-ask path), not loopback HTTP — it sidesteps cookie auth and is the
reason the `run_path` tag exists (D4).

Rejected: a blocking POST (reproduces the 504 class); loopback HTTP self-calls
(cookie auth gymnastics, and the latency would be neither the CLI path nor the
internal path).

### D2 — The scoring core lives in `agents/eval_core.py`; scripts re-export

The container image copies ONLY `agents/` (`agentic_digital_thread/Dockerfile`)
— the server cannot import repo-root `scripts/`. The pure, smoke-independent
scoring pieces (loaders, the fabrication detector, `score_question`,
`build_eval_report`, suite totals, `golden_file_sha`) therefore moved into
`agents/eval_core.py` (stdlib-only), and `scripts/eval_answers.py` re-exports
every moved name so the 14 pre-existing unit-test files pass unchanged. The
smoke-dependent `--strict` layer, the A/B diff, transport, and the parser stay
CLI-only in `scripts/`. Two implementation consequences worth recording:
`INJECTION_CANARY_MARKER` moved to `agents/demo/injection_canary.py`, because
the demo-literal guard test forbids that literal in `agents/*.py` outside
`agents/demo/` (ADR-0044); and `run_path` was added to the pinned legacy
report key set in `test_eval_repeats.py` — the one logically forced test edit.

### D3 — Server and UI offer run-ALL only

Partial runs can flip the suite-level cross-system proof (the standing
project rule for the deployed lanes), so the server surface refuses subsets
outright: the POST body model is `extra="forbid"` — any subset-selection key
(`count`, `category`, `ids`, `question_ids`) is a 422; the only accepted key
is an optional `variant` label. The tab renders exactly one run affordance
("Run full golden suite", confirm-gated — a run is long and billable) and no
subset control of any kind. Server-side `--strict`, `--repeats`, A/B
`--previous`, and custom golden sets stay CLI-lane features (out of scope,
below). **Amended 2026-08-19 for custom golden sets only
([ADR-0064](0064-customer-golden-set-runs-are-upload-per-run.md)): the tab gains
a SECOND, distinct run-ALL affordance for a set the steward uploads per run — no
selection parameter exists on it, and this route's `extra="forbid"` body model is
byte-unchanged. `--strict`/`--repeats`/A-B stay CLI-lane.**

### D4 — Every report carries a required `run_path` tag

`build_eval_report` gained a REQUIRED `run_path` field: `"server"` (internal
chat client, inside the VPC) vs `"cli"` (through CloudFront). The two paths'
latencies are incomparable — a server run skips the edge, WAF, and origin
hops — so any latency comparison across untagged reports would be a
measurement-honesty violation (ADR-0054). The tag is stamped at build time,
persisted in the report and in each run summary, and surfaced as a badge in
the tab's history and the A/B tooling.

### D5 — Eval traffic is synthetic: `eval-run-` containment, `write_memory=False`

Every server-run question is asked under a session id prefixed `eval-run-`,
which is in BOTH `runtime_app._SYNTHETIC_SESSION_PREFIXES` (learning writers
suppressed, cost-trend excluded) and `memory_client.AUTOMATED_SESSION_PREFIXES`
(never in the user-facing session picker) — the two tuples are cross-pinned
per the existing convention. Memory writes are additionally suppressed at the
source: every stream call carries `write_memory=False`. Net effect: the system
never trains on its own test set, and eval runs leave zero learning or memory
residue (proven per run by the Wave-3 full-smoke + `audit_learning.py --strict`
check).

### D6 — Managed AgentCore Evaluations are surfaced on the same tab — complement, never replace

The tab shows the managed AgentCore Evaluations scores (continuous sampled
scoring of production traces, `stacks/_evaluations.py`) beside the golden-run
history, reusing the existing fleet endpoint (`GET /api/admin/agents`) — no
new route, no stack change, no bespoke LLM judge. The managed scores
COMPLEMENT the deterministic golden lane — never replace it (the ADR-0053/0054
posture): golden scoring is computed and the LLM never produces a verdict;
managed evals are sampled (10%), lag by hours, and are NOT a CI gate.

### D7 — `scripts/audit_answer.py`: findings annotate, never gate

The fourth sibling of `smoke_prompts.py`, `audit_learning.py`, and
`eval_answers.py` — it COMPLEMENTS all three, never replaces any of them.
Where those lanes GATE, this lane EXPLAINS: given a finished eval/smoke report
plus captured evidence files (v1 is strictly file-in/file-out — no live fetch,
no raw production data), it asks an LLM to propose root-cause findings per
failed question, then applies DETERMINISTIC post-LLM validation before
anything is written:

- **Exit contract 0/2 with NO exit 1.** 0 = audit completed REGARDLESS of
  findings; 2 = usage/input/model error. Findings never touch verdicts or exit
  codes — no other code exists *by design*, so no CI wiring can ever turn the
  auditor into a gate.
- **Pinned 7-value taxonomy:** `data_gap`, `mapping_gap`, `schema_drift`,
  `prompt_gap`, `code_bug`, `ux_gap`, `golden_candidate`. Anything else is
  discarded.
- **Referential integrity:** every `evidence_refs` entry must resolve to an id
  present in the provided report+evidence bundle. Invalid findings are
  DISCARDED and counted (`discarded_invalid`) — never silently kept.
- **`suggested_mapping` is field-compatible with `log_proposed_mapping`**
  (`agents/self_learning.py`), pinned by signature introspection, so LATER
  phases can route findings through the existing proposal plumbing without a
  schema migration — v1 ONLY writes the findings file; routing is explicitly
  out of scope.
- **Measured-only cost:** the `usage` block carries token counts summed from
  the converse responses and is present ONLY when EVERY response carried
  measured counts — otherwise the key is ABSENT (the strictest
  measured-or-absent reading). Never estimated, never dollars (ADR-0053).
- The LLM seam is the module-level `_converse_factory` (the `_stream_factory`
  rederive idiom), so the unit lane never touches the network; the default
  model id comes from `agents/model_catalog.py` — no literal model ids.

Rejected: exit 1 on findings ("fail the build when the auditor is worried") —
that is gate creep by another name, and an LLM opinion must never influence an
exit code; live evidence fetch in v1 (pulls raw production data into an LLM
prompt path this lane has no claim-check machinery for).

## Explicitly out of scope (later phases)

- Review queue / promote-to-golden for auditor findings.
- Findings ledger UI or routing through `log_proposed_mapping` (v1 ships
  schema COMPATIBILITY only).
- A nightly/scheduled product auditor.
- A golden-set DynamoDB editor (the `golden_evals.json` file schema remains
  the sole interchange format).
- Server-side `--strict`, `--repeats`, and A/B `--previous` — these stay
  CLI-lane features. **Custom golden sets were deferred here and are now
  superseded IN PART by [ADR-0064](0064-customer-golden-set-runs-are-upload-per-run.md):
  the tab may RUN an uploaded customer set (upload-per-run, zero server-side
  persistence of the set). Server-side STORAGE of a golden set, and a
  golden-set editor, remain out of scope.**
- Any `stacks/` change or new DynamoDB table.

## Considered alternatives

- **Keep evals CLI-only.** Rejected: evaluation health invisible to stewards
  without a checkout; and ad-hoc "run it for me" requests were already
  producing partial runs (which flip the cross-system proof).
- **Subset-run controls with a warning label.** Rejected: the cross-system
  proof is suite-level; a warning label does not make a flipped proof honest.
  Grouping remains a CLI-lane, run-time-selection feature.
- **A bespoke server-side LLM judge for answer quality.** Rejected: the
  managed AgentCore Evaluations service already does sampled LLM scoring, and
  the deterministic lane's whole value is that its verdicts are computed.
  Lean into the managed service; keep the computed lane computed.

## Consequences

- **Positive:** stewards get on-demand, centrally persisted golden runs with
  honest run-path provenance and zero learning/memory residue; developers get
  a root-cause annotation lane whose findings are validated deterministically
  and are pre-shaped for future proposal routing.
- **Negative / risks:** the lock is check-then-set, not atomic — two
  simultaneous POSTs can race in a window (same accepted bound as
  feedback_exploration; the stale-takeover keeps a crashed worker from
  wedging the tab). On unseeded (`base`) deployments, `requires_seed` goldens
  honestly FAIL server runs — an accepted v1 limitation surfaced in the tab's
  copy; the runner deliberately does not grow a no-seed toggle. The auditor's
  findings are only as good as the evidence files supplied — file-in/file-out
  means stale evidence produces stale findings, flagged by nothing.
- **Verification:** the pinned-by table below; plus the Wave-3 deployed-env
  pass (server run + CLI run cross-checking `run_path`, full
  `smoke_prompts.py` + `audit_learning.py --strict` proving zero residue).

## Pinned-by

| Invariant | Test | Wave |
|---|---|---|
| Auditor findings never flip verdicts/exit codes (exit 0 with findings; report byte-unchanged; no exit-1 path exists) | `tests/unit/test_audit_answer.py` | 1 |
| UI/API offers run-ALL only (422 on subset keys; full list reaches the seam; no subset control rendered) | `tests/unit/test_eval_routes.py` + `EvalsTab` vitest spec | 2 |
| Eval traffic carries synthetic markers (`eval-run-` prefix in both tuples; `write_memory=False` on every seam call) | `tests/unit/test_eval_run_synthetic_markers.py` (+ `test_memory_session_list.py` cross-pin) and `tests/unit/test_eval_runs.py` | 1+2 |
| Run-path tag present in persisted reports (`"cli"` from the CLI; `"server"` persisted by the runner) | `tests/unit/test_eval_run_path.py` + `tests/unit/test_eval_runs.py` | 1+2 |
| Findings taxonomy pinned (7 values) + referential integrity + discard counter | `tests/unit/test_audit_answer_findings.py` | 1 |
| Cost measured-only (tokens or absent, never dollars/estimates) | `tests/unit/test_audit_answer.py` usage-block assertions | 1 |
| `log_proposed_mapping` schema compatibility of `suggested_mapping` | signature-introspection test in `tests/unit/test_audit_answer_findings.py` | 1 |
