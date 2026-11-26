<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Experiments—running a variant A/B with the golden eval lane

> **Maintainer documentation**—working notes for contributors and AI agents,
> not part of the user-facing guide.

How to measure the effect of a configuration change (per-system business
context, self-learning on/off, an architecture variant) on the golden
questions, using `scripts/eval_answers.py`'s variant-aware A/B diff.
Design and rationale: [ADR-0054](adr/0054-measurement-honesty-and-experiment-harness.md)
(decisions D3/D4); the informational-by-default posture is ADR-0053 D-E2.

The recipe is always the same: **run a baseline, change ONE thing, run the
variant with `--previous`, read the rollup.** The concrete example below is
the business-context A/B (the first measured configuration); any other
toggle substitutes its own step 1/3.

## Prerequisites

- A deployed environment (the eval lane runs against `/api/ask` live).
- `ADT_PASSWORD` for the target env (from the `adt-demo-password` secret).
- Admin credentials for the business-context API (the PUT is admin-gated).
- A run directory for the report JSONs—they are the evidence; keep them
  (example below uses `/tmp/adt-m123-gates/`).

### Measurement hygiene—deliveries are not verdicts (2026-08-07)

- **Do not measure a freshly deployed environment.** A deploy rolls the app
  task and the agent runtimes; turns issued during the settling window can
  fail to deliver end-to-end. The eval lane now refuses to start until one
  throwaway warmup ask completes (`--no-warmup` opts out—only for
  environments you have ALREADY verified serving). A run aborted by the
  warmup gate exits 2 and writes no report: re-run later rather than
  trusting a scorecard measured against a sick environment.
- **Transport failures are labeled, not blended.** A repeat whose stream
  never delivers its `done` frame is retried once on a fresh session; if it
  still fails it carries `transport_failure: true` and no
  `content_verdict`. Content majority is computed only from delivered
  repeats. The folded row separately reports `content_verdict` /
  `majority_verdict`, `delivered_count`, `transport_failure_count`,
  `measurement_valid`, and a bounded `invalidity_reason`. Any transport
  failure makes `measurement_valid: false` and the completed CLI campaign
  exits 1 even when the delivered answers retain a passing content majority;
  it must never be described as a content failure. Re-run before drawing a
  campaign conclusion.
- **Campaign majority mode uses `--repeats 5` or more.** `--repeats 1`
  preserves the legacy single-run report shape; values 2–4 are rejected as
  usage errors (exit 2), because a three-run majority is too fragile for a
  campaign conclusion. The Evals admin API remains run-all/single-run and
  rejects a repeats parameter.
- **State instability invalidates the measurement.** Disagreement in the
  pinned graph-state signals across delivered repeats sets
  `state_instability: true`, `measurement_valid: false`, and exit 1 without
  changing the computed content majority. In A/B output, any pair containing
  an invalid arm is `non-comparable` (never a content regression), contributes
  no metric deltas, and makes the overall diff `comparable: false`.
- **Heavy goldens legitimately run 150–170 s.** The per-question timeout
  default is 300 s; do not lower it below ~2× the heaviest observed turn or
  slow-but-honest answers will be scored as transport failures.
- **Never write an exact-count golden over a window that is still accruing
  (2026-08-18).** Measured live: three goldens scored FAIL while the
  orchestrator was RIGHT. Their expectations were exact point counts derived
  by hand on one date, but the source table receives LATE-ARRIVING rows for
  windows that have already closed—so the true answer keeps moving and the
  golden drifts forever, and every drift reads as a model regression that is
  not one. Prefer a **magnitude band** (`about 5.2M`, i.e. at least 5,200,000
  and under 5,300,000) or **named categorical codes** over a count. **How to
  check before authoring:** compare each row's *ingestion* timestamp against
  its *event* timestamp for the window you are about to assert on; if any row
  landed after the window closed, the window is accruing. `agents/eval_core.py`
  exposes that decision as pure helpers—`accrual_signal(observed)`
  (`"accruing"` / `"settled"` / **absent** when the evidence cannot tell—an
  unmeasured window never reads as settled) and
  `suggest_count_band(count)` (the pasteable band). They ride the **non-gating
  `hints` block** of a promoted draft as ADVICE TO YOU: nothing generates an
  `answer_must_match`, and a hint can neither pass nor fail a question
  ([ADR-0062](adr/0062-review-queue-promote-to-golden-findings-ledger.md) D5).
  You write the expectation.

## Step 1—write the steward context, **disabled**

Write the business context for each system under test with
`enabled: false`, so the text is durable (and hash-stamped) before the
baseline runs but not yet in any prompt:

```
PUT /api/admin/business-context/<SYSTEM_ID>
{"business_context": "<steward text>", "enabled": false}
```

(and the same for each other system, for example `<SYSTEM_ID_2>`). Keep the text
≤2000 characters; writes matching secret-shaped patterns (keys, PEM
headers, `password:`/`token=`, bearer tokens) are rejected with 400
`reason: "secret_like_content"`. **Record the `context_hash` from each PUT
response**—it ties the A/B evidence to the exact text version. Send the PUTs
one at a time per system: a PUT (or guided confirm) of a system that is
still being saved is refused with 409, and nothing is written.

## Step 2—baseline run

```bash
python3 scripts/eval_answers.py --base-url https://<YOUR_ADT_DOMAIN> \
  --password $ADT_PASSWORD --variant baseline \
  --report /tmp/adt-m123-gates/eval-baseline.json
```

Add `--no-seed-fixtures` if the env lacks the seeded fixture rows (match
how the env's standing eval gate is run). The report is stamped with
`variant: "baseline"` and a `golden_set_sha` of the golden file bytes.

## Step 3—flip the toggle

Re-PUT each system with `"enabled": true` (same text). Require
`runtime_refresh: "ok"` in every response—`failed` means the durable
value saved but the running agent was NOT refreshed (fix before
proceeding); `skipped` means local mode / no cloud runtime. Then wait for
the runtime replacement to go READY (~1–2 min per system; poll, or re-PUT
and GET to confirm).

## Step 4—variant run with the A/B diff

```bash
python3 scripts/eval_answers.py --base-url https://<YOUR_ADT_DOMAIN> \
  --password $ADT_PASSWORD --variant plus-business-context \
  --previous /tmp/adt-m123-gates/eval-baseline.json \
  --report /tmp/adt-m123-gates/eval-ab.json
```

## Step 5—read the rollup

The console prints a one-line suite rollup after the report path, for example:

```
A/B plus-business-context vs baseline: pass 9→11, tool calls −18%, tokens n/m
```

The report's `trend` block carries the full diff:

- `variant` / `previous_variant`—the two configurations compared.
- `comparable`—`true` iff both reports ran the same golden set
  (`golden_set_sha` equality). A mismatch prints a WARN and sets `false`;
  per-id deltas are still computed, but treat conclusions with suspicion.
- `questions[]`—per-id `transition` (`regression` = pass→fail,
  `improvement` = fail→pass, else `stable`) and signed `deltas` on every
  numeric metric (plus `_pct` where the previous side is a nonzero
  number). `null` deltas mean a side was not measured—never
  imputed, never zero-filled.
- `flags`—informational `metric-drift` annotations at pinned thresholds
  (tool calls +25% AND +2 absolute; total tokens +25%; latency +50% AND
  +10 s). These NEVER affect the exit code.
- `suite`—pass counts before/after, regression/improvement counts, and
  `metric_totals` computed only over ids measured in BOTH runs.

Finally, restore the env to its desired steady state (leave the toggle
enabled unless told otherwise—record which) and attach the report path,
the rollup line, and the step-1 `context_hash` values to your session
evidence.

## Fired versus loaded—reading `rules_applied` against `planned_with`

The platform's effectiveness metrics measure **two different things**, and a
campaign readout is ambiguous unless you hold both definitions:

- **Loaded at planning** (`planned_with.*`)—artifacts fetched into the
  Mode-A route seed *before* the model turn. Proves the planner *had* the
  context. A near-zero-variance function of graph state.
- **Used in this answer** (`rules_applied`)—accepted rules that actually
  *fired* while answering (their saved knowledge showed up in this turn's
  tool path). Proves the answer *consumed* the context.

| Metric | Exact source | Counts | Does NOT count |
|---|---|---|---|
| `rules_applied` (and the eval row's `rules_applied_count`) | runtime finally-hook: accepted-rule Amazon Neptune Analytics node ids observed in tool results (`usage_tracking.extract_rule_node_ids` → `self_learning.get_accepted_rules_by_node_ids` → `_capped_rules_applied`) | accepted rules that **fired** during this answer (use-time) | anything merely present in the route seed; pending proposals; graph metadata the model read but never applied |
| `planned_with.systems` | Mode-A route seed (`runtime_app._planned_with_from_seed`, parsed after `ROUTE SEED RESULT:`) | connected systems **loaded** into the seed | systems actually queried this turn (see `system_queries`) |
| `planned_with.shared_concepts` | same seed parse | deduped shared-concept names loaded (capped at 10) | concepts the answer actually traversed |
| `planned_with.join_paths` / `value_mappings` / `identity_rules` | same seed parse | rule-grain artifacts **loaded** at planning time (per-kind seed cap 50/system) | whether any of them fired—that is `rules_applied`'s job |
| `planned_with.authority_rules` *(additive, T1.6)* | same seed parse; the seed builder's dedicated active-`AuthorityRule` property read (deactivated rules excluded) | active authority rules **loaded** at planning | fired authority decisions; deactivated rules. **Honest null:** ABSENT (not 0) on reports from deployments predating the key or turns without a route seed—0 means "consulted, none exist", absent means "not measured" |
| eval `planned_with_present` | `_metrics_block` | whether a route seed opened the turn at all | any per-kind detail |

**How to read them together.** `planned_with` is the supply side,
`rules_applied` the consumption side. Four readings:

| `planned_with.*` | `rules_applied` | Reading |
|---|---|---|
| 0 / absent | empty | the graph gave the planner nothing—supply problem |
| > 0 | non-empty | loaded AND fired—the intended path |
| > 0 | empty | **consumption gap**: the planner had it, the answer never used it |
| 0 / absent | non-empty | rules fired from outside the seed path (for example, Mode B)—check the mode before concluding anything |

**Worked example (observed on a deployed environment).** After seeding 10 value
mappings into the deployment's graph, every re-asked question reported
`planned_with.value_mappings: 12`—up from 0—while `rules_applied` stayed
`[]`. Both numbers are correct and they do **not** contradict each other:
the seeding raised the *loaded* count (the route seed now carries the
mappings), but no accepted rule *fired* in those answers. That is the
consumption-gap row above—the exact discriminator the measurement
campaign's PIVOT branch turns on. Before T1.6, `authority_rules` was missing
from `planned_with` entirely, so "authority context was never even loaded"
and "loaded but never fired" were indistinguishable for that artifact class;
now an arm can show `planned_with.authority_rules ≥ 1` (loaded) separately
from whether anything fired.

Honest-data rules apply throughout (ADR-0053/ADR-0045): absent means "not
measured", never coerced to zero; a zero is a real measurement. Reports
written before this change simply lack the new key and still parse—the
eval harness reads it null-safe (`planned_with_authority_rules: null`).

## `--strict-regression` semantics

By default the A/B diff is **informational**—the exit code is the
standard golden-expectations contract, unchanged. Passing
`--strict-regression` (env `EVAL_STRICT_REGRESSION`) makes the run exit 1
iff the diff contains ≥1 verdict `pass→fail` regression. Metric drift
never gates, even under the flag: single-run deltas on a live LLM path
flake (routing, retries, model nondeterminism), so only a verdict flip is
treated as a gating signal (ADR-0053 D-E2; ADR-0054 D3).

## What this lane is NOT

- Not a replacement for `smoke_prompts.py` (grounding) or
  `audit_learning.py` (learning health)—it complements both.
- Not Amazon Bedrock AgentCore dataset evaluations—those are a documented follow-up
  seam (public preview, Transaction Search required, LLM-judged metrics;
  see ADR-0054), complementary and never a dependency of this lane.

## Swapping a model as a measured experiment (ADR-0055)

Both model roles change LIVE—a runtime version bump (~1–2 min), no redeploy
and no re-onboard. That makes a model swap a normal A/B variant.

```bash
# 1. Baseline on the current model
python3 scripts/eval_answers.py --base-url <YOUR_URL> --password $ADT_PASSWORD \
  --variant baseline --report /tmp/eval-baseline.json

# 2. Switch the model (orchestrator and/or agent). Agent changes apply
#    fleet-wide; the response carries an honest per-system tally.
curl -s -b jar -X PUT -H 'Content-Type: application/json' \
  -d '{"agent_model":"openai.gpt-5.6-luna"}' \
  "<YOUR_URL>/api/admin/llm-settings"
#    Expect: {"agent_runtime_refresh":{"ok":[...],"failed":[],"skipped":[]}}
#    reonboard_required present ⇒ some system did NOT take the new model and
#    is still running the old one. Re-onboard those before measuring.

# 3. Wait for new runtime versions to be picked up (~1–2 min), then measure
python3 scripts/eval_answers.py --base-url <YOUR_URL> --password $ADT_PASSWORD \
  --variant luna-agents --previous /tmp/eval-baseline.json \
  --report /tmp/eval-luna.json
```

Read the printed rollup (`pass N→M`, tool-call and token deltas) plus the
per-question `trend` block. Remember what the numbers can and cannot say: a
single run per variant carries live variance, so treat metric drift as a
signal to investigate, not proof—which is why drift never gates
(ADR-0053 D-E2).

**Cost note:** a lower per-token price does not by itself mean a lower cost
per answer; the tokens-per-answer metric decides whether a swap saves anything.
At Amazon Bedrock list prices as of 2026-07-30, GPT-5.6 Luna is $1/$6 per MTok
and Claude Haiku 4.5 is $1/$5. Prices change; see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) for current rates.

### VOID—GPT-5.6 Luna versus Anthropic Claude Haiku 4.5 as the system-agent model (2026-07-28)

> **This result is VOID. Do not cite it.** The Luna run started minutes after
> the first fleet agent-model switch, which was executed on code predating the
> ADR-0056 protocol round-trip and therefore downgraded all nine system-agent
> runtimes MCP->HTTP. Every "extra call" and "wasted call" below is that
> breakage, not Luna. The tell was visible in the data but went unnoticed: the clean 4.6
> baseline reads `wasted_calls: 0`, and every post-switch run reads non-zero.
> Re-run on a repaired fleet. Original text kept for the audit trail:

Measured on a second deployment, orchestrator held constant on Sonnet 4.6,
11 golden questions,
one run per variant. **Verdicts: 11/11 for both.** The other measures:

| Measure | Haiku 4.5 | Luna | Delta |
|---|---|---|---|
| system-agent calls | 36 | 103 | +186% |
| total tool calls | 47 | 114 | +143% |
| **wasted calls** | **0** | **36** | measured, not inferred |
| orchestrator input tokens | 672,555 | 1,194,486 | +78% |
| latency (worst question) | 142 s | 425 s | ~3× |
| orchestrator spend, 11 questions | $2.44 | $3.94 | +$1.50 |

The call counts land at a near-exact 3× on every row (1→3, 4→12, 5→15), which
looks systematic rather than Luna choosing to explore more—the `wasted_calls`
The dollar figures above are estimates computed from Amazon Bedrock list
prices as of 2026-07-28. Prices change; see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) for current rates.
Note the token/cost figures are ORCHESTRATOR scope (ADR-0053);
Luna's own agent-side tokens are not in the measured ledger, so the true cost
gap is larger than the table shows.

**Decision: Claude Haiku 4.5 stayed the system-agent default.** Whether the
3× call pattern came from the integration or the model was left open (the
repaired-fleet run below answers it).

### VOID—Sonnet 5 as orchestrator (2026-07-29)

> **This result is VOID. Do not cite it.** Measured on the same broken fleet
> (system-agent runtimes in HTTP mode answering no MCP initialize), so the
> `TimeoutError`s, the 8-of-13 wasted calls and the 3x latency were
> infrastructure. The root-cause narrative below—that Sonnet 5 ignores the
> prompt-only serial-telemetry rule—was NOT established: a direct gateway
> probe later showed serial, parallel-same-system and parallel-cross-system
> calls ALL failing identically with
> `McpInitializationException ... did not respond to the initialize request
> within 40 seconds`, that is, parallelism was never the variable. Sonnet 5 is
> unmeasured, not rejected. Original text kept for the audit trail:

Measured on a second deployment, agents held constant on Haiku 4.5, 11 golden
questions:

| Measure | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| verdicts | **11/11 pass** | **7/11 pass** |
| system-agent calls (typical question) | 4 | 12 |
| wasted calls (tool results reporting an error) | 0 | 8 of 13 |
| latency (typical question) | 65–135 s | 285–295 s |
| total tokens, suite | 700,998 | 1,066,497 |

Failures were 2 × `TimeoutError` plus 2 questions whose stream was cut before
`intelligence_summary` (which the eval reads for grounding counters).

**Root cause—a prompt-only constraint that Sonnet 5 does not honor.** The
orchestrator prompt says telemetry calls must be issued SERIALLY
(`orchestrator.py`, "NEVER fan out parallel sitewise tool calls") because each
parallel invocation lands on a fresh cold runtime. That rule is enforced ONLY by
prompt text—`test_orchestrator_i3x_prompt_rules.py` asserts the sentence is
present in the prompt, not that the behavior follows. Sonnet 4.6 complied;
Sonnet 5 fans out anyway. Evidence: app-side tool-result envelopes log errors in
simultaneous groups of three, repeatedly, and 8 of 13 calls come back errored.
The retries triple the work, which triples latency, which pushes the heaviest
questions past the request timeout.

Why the first diagnosis looked wrong: re-running one or two questions in
isolation PASSES, because individually they land just under the timeout. Only
the full suite exposes it. Single-question retests are not evidence for this
class of failure—use `--repeats`.

**Decision: Claude Sonnet 4.6 stayed the default and Claude Sonnet 5 stayed
selectable.** (Since changed: see "Default changed to Sonnet 5 (2026-10-12)"
below.) The durable fix is to enforce telemetry serialization in CODE rather
than by prompt, then re-measure Sonnet 5; the constraint was never actually
enforced.


### How a fleet fault can present as a model regression

Two model verdicts were recorded and both were wrong, for the same reason: the
measurements ran on a fleet that a model switch had left non-functional. The
chain:

1. A fleet agent-model switch called `update_agent_runtime` on deployed code that
   predated the ADR-0056 protocol round-trip. `update_agent_runtime` is
   FULL-REPLACEMENT, so omitting `protocolConfiguration` silently reset nine
   system-agent runtimes from MCP to the default HTTP.
2. Those runtimes then answered no MCP `initialize`. Calls failed after 40 s,
   the orchestrator's cold/busy-runtime retries (5/15/30 s) fired, and latency
   tripled into the request timeout.
3. Both the Luna and the Sonnet 5 runs happened after step 1, so both inherited
   the damage and both looked like model regressions.

**Guards that would have caught it sooner, in order of cheapness:**
- `wasted_calls` in the `intelligence_summary` is 0 on a healthy fleet. Any
  non-zero value means investigate the FLEET before the model.
- Probe the gateway directly (`call_tool_sync` on one system tool). A persistent
  `McpInitializationException` across SERIAL calls is infrastructure by
  definition—a cold start would clear on the second call.
- Compare against a baseline captured in the same session, and re-capture the
  baseline after any runtime-touching action.

### VALID: Sonnet 5 re-measured on a REPAIRED fleet (2026-07-29, later)

Fleet health proven first—`wasted_calls: 0`, 11/11 on Sonnet 4.6 + Haiku
(`s46-haiku-repaired`). Then Sonnet 5, agents unchanged:

| Measure | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| verdicts | 11/11 | **9/11** |
| total tool calls | 55 | **45 (FEWER)** |
| wasted calls | 0 | 3 |
| latency | 16–176 s | 13–277 s (comparable) |
| total tokens | 822,028 | 1,245,600 (+51.5%) |

**The void run's results do not reproduce.** Sonnet 5 made fewer tool calls
(45 against 55), latency was comparable, and nothing timed out. The "3x
amplification" was the broken fleet, start to finish.

What remains is real but different: an INTERMITTENT failure, 2 of 11 in this run
(`g-supplier-quality-trace`, `g-downtime-root-cause`), presenting as a relayed
`Query failed.` with `intelligence_summary` absent at NORMAL latency. It is not
deterministic—re-running `g-downtime-root-cause` immediately afterwards
succeeded with zero error events. Characterizing it needs `--repeats`; a single
run cannot separate a 2/11 intermittent from noise.

**Decision: Claude Sonnet 4.6 stayed the default.** It passed 11/11 across three
clean runs; Sonnet 5 passed 9/11 with an intermittent failure not yet
characterized. (Since changed: see "Default changed to Sonnet 5 (2026-10-12)"
below.)

### The serial-telemetry rule is OBSOLETE on current infrastructure

Measured directly against the gateway on the repaired fleet, no model involved,
three calls of identical work:

| Trial | Wall time | Outcome |
|---|---|---|
| serial, 3× same system | 14.7 s | all success |
| parallel, 3× same system—the prompt FORBIDS this | **5.1 s** | **all success** |
| parallel, 3 different systems | 4.5 s | all success |

Parallel is ~3x faster and errors zero times, INCLUDING the same-system case the
prompt calls STRICT. The rule's stated rationale—"each parallel invocation
spawns a fresh cold runtime and is far slower than reusing the warm one"—did
not reproduce: every call landed in 4–6 s warm.

So the constraint is currently costing latency for no measured benefit. Before
relaxing it, note what this probe did NOT cover: all runtimes were WARM, and it
was a single trial per shape. A parallel burst against COLD runtimes is the case
the rule was presumably written for, and it is the case still to test—that is
the gate on removing the prompt text. (And per the invariant above, if serialism
is ever required it belongs in CODE, not prompt text.)

### VALID: GPT-5.6 Luna versus Haiku 4.5 as system-agent model (2026-07-29, repaired fleet)

Two invalidating bugs had to be fixed before this number meant anything: the
runtime protocol repair, and—found by the pre-measurement health probe—`_system_agent_model()`
returning a CONSTRUCTED `BedrockModel`, which made
`resolve_model()` a silent no-op so **Luna was never actually serving** in
either earlier attempt. Orchestrator held at Sonnet 4.6 throughout.

| Measure | Haiku 4.5 | Luna |
|---|---|---|
| verdicts | 11/11 | **11/11** |
| total tool calls | 55 | 53 (−3.6%) |
| wasted calls | 0 | 2 |
| orchestrator tokens | 822,028 | 877,022 (+6.7%) |
| latency | comparable per question | |

**The two models measured the same here.** The void run's numbers (+186%
calls, 3x latency, 36 wasted) do not reproduce.

**Decision: Claude Haiku 4.5 stayed the system-agent default.** The measured
results do not separate the two, so the default did not change. For
reference: at Amazon Bedrock list prices as of 2026-07-29, Luna is $1/$6 per
MTok and Claude Haiku 4.5 is $1/$5 (prices change; see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)), and the GPT-5.x
models' `allowed_modes` are `default` and `provider_data_share`, with no `none`
mode; the [Amazon Bedrock data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html)
docs say a model that supports `none` uses zero data retention. Caveat: token counts are ORCHESTRATOR scope
(ADR-0053), so agent-side model spend is NOT in the table; the +6.7% is
orchestrator context growth, not Luna's own consumption.

### PARTLY RETRACTED: the Sonnet 5 intermittent is real, but NOT shown to be model-specific

`--repeats 5` on the two questions that failed, then the identical run with the
orchestrator on 4.6 as a control. Same fleet, same session, agents unchanged:

| Question | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| `g-supplier-quality-trace` | **5/5 pass** | 2/5 pass |
| `g-downtime-root-cause` | **5/5 pass** | 1/5 pass |
| `state_instability` flag | False | **True** |

10/10 versus 3/10. This is not fleet noise and not run-to-run variance—the
control is perfect on the same infrastructure in the same window. Failure
signature is identical every time: a relayed `Query failed.` with
`intelligence_summary` absent, at NORMAL latency (no timeout). Runs that pass
look completely normal (`wasted_calls: 0`, 4–5 system queries).

**Sonnet 4.6 remained the default.** (Since changed: see "Default changed to
Sonnet 5 (2026-10-12)" below.) Selection-bias caveat: these two questions
were chosen BECAUSE they failed, so 3/10 is not Sonnet 5's suite-wide rate—the
full-suite run scored 9/11. The comparison against the 4.6 control on the same
two questions is the valid part.

**Root cause is still unnamed, and the reason is an observability gap.** The
error text is destroyed twice over: the runtime scrubs its own copy to
`Query failed.`, and a server-side frame filter replaces the public frame with
the same generic string. A fingerprint log now sits at the one point that still
sees the original (`chat_client._dict_to_sse_event`, digit-masked). It needs a
deploy, then one reproduction, to name the error.


### Correction to the above: the model attribution does not hold (2026-07-29, late)

After deploying the worker-exception log, the SAME two questions on the SAME
Sonnet 5 orchestrator and the same fleet went:

| Window | Config | Result |
|---|---|---|
| 16:33 | Sonnet 5 | 3/10 pass |
| 19:00 | Sonnet 4.6 (control) | 10/10 pass |
| 21:09 | Sonnet 5 | 0/3 pass |
| 21:50 (after an app restart) | Sonnet 5 | **10/10 pass** |

Sonnet 5 goes from 3/10 to 10/10 with no model change. So the failure is REAL and
reproducible in some windows, but "model-specific" is NOT established—and the
4.6 control cannot settle it, because it ran in a DIFFERENT time window.

**The methodological error is the same one that produced the two void verdicts:
comparing sequential BLOCKS across time instead of interleaving within one
window.** Environment drift (app process age, simulator load, container
freshness) is uncontrolled in a block design, and here it plausibly dominates.
The only sound way to attribute this to a model is an INTERLEAVED A/B/A sample—alternate
4.6 and Sonnet 5 question-by-question inside a single window—so
drift affects both arms equally.

What IS solid: the failure exists, its signature is a relayed `Query failed.`
with `intelligence_summary` absent at normal latency, and it does not correlate
with `wasted_calls` (0 on passing runs). Two candidate confounds worth testing
first: time since the last app restart, and simulator write load.

Diagnostics are now deployed and armed at all three points where the error text
used to be destroyed (`runtime_app.consume`, `chat_client._dict_to_sse_event`,
`server._start_stream_worker`). The failure did not recur while they were live,
so it remains UNNAMED—but the next occurrence will log its own cause.

## RESOLVED: the Sonnet 5 failures were caused by a missing token cap (2026-07-30)

### The interleaved A/B that made attribution valid

Arms alternated inside ONE window, order counterbalanced (ABBA), two questions
per cell—so drift hit both arms equally:

| Question | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| `g-supplier-quality-trace` | 4/4 pass | 1/4 |
| `g-downtime-root-cause` | 4/4 pass | 1/4 |

**8/8 vs 2/8.** This is the design the earlier block comparisons lacked, and it
does establish a model-dependent effect—which then had to be explained, not
just attributed.

### Naming the error required fixing three layers of scrubbing

The generic `Query failed.` the client sees had four candidate origins. Adding a
digit-masked fingerprint at each eliminated them one at a time until the real
chain appeared:

```
eval sees "Query failed."
  <- server frame filter replaces any public error frame
  <- chat_client._generic_events relays the runtime dict {"error": "Agent invocation failed"}
  <- runtime_app._run_orchestrator catch-all around agent.stream_async
  <- strands.types.exceptions.MaxTokensReachedException
```

A separate defect hid the last step: **the orchestrator's OTel log handler drops
`logger.exception`'s `exc_info`**, so every `logger.exception` in that runtime
logged a location with no cause. Formatting the traceback into the MESSAGE is
what exposed it. That fix stands on its own—no exception in that runtime was
traceable before it.

### Root cause

**The orchestrator never set `max_tokens`.** It passed a bare model-id string and
inherited the Amazon Bedrock default, while the SYSTEM AGENTS explicitly capped at 8192—backwards,
since the orchestrator writes the long final answers. A model
verbose enough to exceed the default hits the ceiling mid tool-use, and strands
treats `MaxTokensReachedException` as UNRECOVERABLE and aborts the loop. Sonnet
4.6 stayed under the default; Sonnet 5 did not. That is the whole of the
"model-specific" effect.

This is an availability bug that PREDATES Sonnet 5—any sufficiently verbose
orchestrator model would have hit it.

### Results after the fix

Both roles now resolve through one builder (`build_model(id, max_tokens=...)`),
so the cap cannot be set on one role and forgotten on the other.

| Measure | Sonnet 4.6 | Sonnet 5 (fixed) |
|---|---|---|
| verdicts | 11/11 | **11/11** |
| total tool calls | 55 | **51 (−7.3%)** |
| total tokens | 822,028 | **744,433 (−9.4%)** |
| wasted calls | 0 | **0** |
| the two hard questions | 5/5, 5/5 | **3/3, 3/3** (was 1/4, 1/4) |

Verdicts were equal, and the previously failing questions passed on repeats.
The default was left unchanged pending a same-day controlled run (next section;
its token numbers supersede the ones in this table).

## Sonnet 5 as default: same-day controlled run (2026-07-30)

Ran the gate the invariant asks for: FULL lane, `--repeats 2`, plus a SAME-DAY
4.6 control at identical repeats.

| Measure (per pass over 11 goldens) | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| verdicts | **22/22** | **22/22** |
| `state_instability` | none | none |
| input tokens | 763,613 | 1,458,468 (+91.0%) |
| output tokens | 30,519 | 48,006 (+57.3%) |
| tool calls | 52 | 56 (+7.7%) |
| **estimated cost at \$3/\$15 per MTok (Amazon Bedrock list price)** | **\$2.75** | **\$5.10 (+85.4%)** |

Amazon Bedrock list prices as of 2026-07-30. Prices change; see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) for
current rates.

**Stability gate PASSES**—22/22 with no instability, and the two questions that
failed pre-fix are solid. The `max_tokens` fix fully resolved the failure mode.

Verdicts were identical. At the list prices above the Sonnet 5 run cost about
1.9x as much, with 7.7% more tool calls; the Agents page shows that estimate.

**Decision: Claude Sonnet 4.6 stayed the default.** (Since changed: see the next
section.) Claude Sonnet 5 stayed selectable, and the token-cap fix made it safe
to select. Re-measure when list prices change or the golden lane shows a
capability difference.

### Default changed to Sonnet 5 (2026-10-12)

The orchestrator default is now Sonnet 5, and it's the only orchestrator
fallback. The workshop package already pinned Sonnet 5 for the orchestrator and
onboarding, and its model allowlist holds only Sonnet 5 and Haiku 4.5. Making
the catalog default match means a deployment that sets no model runs the same
models the workshop does. Sonnet 4.6 stays selectable outside the workshop
allowlist. On a freshly created deployment running Sonnet 5 (2026-10-12), the
golden lane passed 13/13 and strict smoke 15/15. The token and cost comparison
above wasn't re-measured.

### Correction to the 2026-07-30 (early) entry

That entry reported Sonnet 5 using FEWER tokens (744,433 vs 822,028, "−9.4%").
That was a single run compared against the PREVIOUS DAY's baseline while the
simulator kept adding data—a cross-window comparison, the same flaw as the
block-design A/B. The same-day controlled numbers above supersede it: Sonnet 5
uses ~90% MORE tokens, not fewer. The earlier figure should not be cited.

### Serial-telemetry rule REMOVED (2026-07-30)

Removed `_I3X_SERIAL_RULE_ADDITION` from the orchestrator prompt on the measured
evidence above (serial 14.7 s vs parallel-same-system 5.1 s wall, all success).
Its unit test is inverted: it now asserts the sentence is ABSENT, so the rule
cannot be reintroduced as prompt text without a deliberate change.

**Live regression check (2026-07-30):** golden lane
**11/11**, `wasted_calls` 0, tool calls **-11.3%** and tokens **-15.3%** against
the same-day 4.6 control. No regression—fewer calls for the same verdicts.

Latency is NOT cleanly comparable: the control was a `--repeats` run whose
per-repeat rows carry no `elapsed_seconds`. Against the single-pass 4.6 baseline
the 11-golden wall time is 953 s -> 920 s (-3.5%) with the worst question
176 s -> 144 s, but that spans two days of simulator growth, so treat it as
directional only. The verdict and call-count deltas are the sound part.
