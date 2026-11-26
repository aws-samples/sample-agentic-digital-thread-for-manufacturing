<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Monitor agent fleet metrics

`GET /api/admin/agents` powers the Agents management page. To keep the page
fast, the request path never runs Amazon CloudWatch Logs Insights queries or
serial per-agent metric calls—it serves a warmed snapshot cache computed in
the background.

## Native metrics + snapshot cache

The request path serves a **warmed snapshot cache** and never runs Logs Insights.
The snapshot is computed in the background (startup warm + an interval poll) from
a **single batched `GetMetricData`** call for the whole fleet.

Sources (all natively populated CloudWatch metrics):

| Metric on the page | Native source (one `GetMetricData`) |
|---|---|
| `tool_calls_24h` (per system) | `AWS/Bedrock-AgentCore` `Invocations`, dims `Operation=InvokeGateway, Method=tools/call, Protocol=MCP, Name=<system_id>___<tool>`—summed per system across that system's tool `Name`s (generic, off the `___` prefix). |
| tool latency / errors | same namespace `Latency` (Average), `UserErrors`/`SystemErrors`/`Throttles` (Sum). |
| `invocations_24h`, `hourly_sessions` | `AWS/Bedrock-AgentCore` runtime `Invocations` (Sum + hourly `Period=3600`), dims `Resource=<runtime-ARN>, Operation=InvokeAgentRuntime, Name=adt_x::DEFAULT`. |
| runtime latency / errors / memory | runtime `Duration`, `Errors`, `MemoryUsed-GBHours` (per-runtime `Resource` dim). |
| `input_tokens_24h`, `output_tokens_24h`, `cost_usd_24h`, `avg_latency_s` | `AWS/Bedrock` `InputTokenCount`/`OutputTokenCount` (Sum) + `InvocationLatency` (Average) by `ModelId`; cost estimated from the Amazon Bedrock list prices in the curated catalog (`agents/model_catalog.py`; current rates: [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)). |

What is **not** a native metric and stays on Logs Insights—but runs **only** in
the background snapshot compute, never on the request:

* the orchestrator's session count, hourly sparkline, `avg_events_per_session`
  and tool calls—from its `ORCHESTRATOR COMPLETE` lines (`total_events=`,
  `tool_calls=`) and, for lines written before `tool_calls=`, each turn's
  `DETERMINISM` line.
* `eval_scores`—sparse orchestrator evaluation results (see
  [Evaluation scores](#evaluation-scores-managed-agentcore-evaluations)).

A deployed runtime writes every log line twice: as plain text in its
per-session runtime stream and as OpenTelemetry JSON in the shared
`otel-rt-logs` stream, sometimes more than once, and a window can hold one copy
of some turns and the other copy of others. Each turn runs in its own trace, so
the one Insights query (`_TURNS_QUERY` in `agents/agents_routes.py`) first folds
all lines of a turn into one row by trace id, then counts those rows; the total,
the sparkline and the average all come from the same rows. A line with no trace
id is counted on its own, never folded into another turn. The orchestrator's
tool calls are every tool its turns called (including the graph tool, which
does not go through the gateway); when a turn's tool count cannot be read, the
card shows the gateway count instead. Per-session stream counts (system agents,
and the orchestrator's fallback when the query fails) leave out the shared
`otel-rt-logs` and `spans` streams.

Session count (`invocations_24h`) is served from the native runtime `Invocations`
metric; the orchestrator additionally reconciles to the app-semantic
`ORCHESTRATOR COMPLETE` count in the background.

Code: `agents/fleet_metrics.py` (pure query build + parse), the
`_compute_cloud_fleet` / `_cached_fleet` / `refresh_fleet_cache` path in
`agents/agents_routes.py`, and the startup-warm + interval-poll wiring in
`agents/server.py` (mirrors the dashboard cards cache).

Configuration (environment variables):

* `AGENTS_FLEET_TTL`—snapshot TTL seconds (default `150`; `0` disables caching).
* `AGENTS_FLEET_POLL`—set to enable the background refresh poll (serve-stale while warm).
* `AGENTS_FLEET_PRIMARY_HOURS`—the window the warmer keeps fresh (default `24`).

## Optional runtime-log metric filters (opt-in)

The `ORCHESTRATOR COMPLETE` / `Tool #` markers can be turned into native custom
metrics, readable in the same batched `GetMetricData`. These metrics are unused
scaffolding for now (see below): the Agents tab keeps its Logs Insights query
until the application emits one metric point per turn.

**Why `put_metric_filter` at startup, not CDK `MetricFilter`:** the Amazon Bedrock AgentCore
runtimes are created at deploy and onboarding time by the provisioning code
(`bedrock-agentcore:CreateAgentRuntime`: `scripts/provision_runtimes.py` and the
app's onboarding provision), so their
log groups (`/aws/bedrock-agentcore/runtimes/{id}-DEFAULT`) are created
**implicitly by the service** and do not exist at CDK synth time (new ones appear
on every onboard). A static CDK construct cannot target them. So `agents/
fleet_metric_filters.py` provides an **idempotent** `put_metric_filter` bootstrap
that discovers the runtime log groups and upserts:

* `SessionCount`—1 per `ORCHESTRATOR COMPLETE` line, on the **orchestrator**
  runtime log group only (dimensionless—a filter over unstructured text cannot
  stamp a per-runtime dimension, and sessions are an orchestrator concept).
* `ToolCall`—1 per `Tool #` line, on every runtime log group (nothing reads it).

The Agents tab does **not** read the custom `SessionCount` for the orchestrator
for now: a metric filter adds one point per matching line, so it counts both
copies of every COMPLETE line, twice the real number of turns. The card keeps
the Insights count until the metric is emitted once per turn. (`Tool #` also
matches no line the runtimes write today.) Filters are **not
retroactive**—they only score events written after creation.

To enable end-to-end (both required):

* `AGENTS_METRIC_FILTERS=1`—create the filters at startup (needs
  `logs:PutMetricFilter`).
* `AGENTS_CUSTOM_METRIC_NAMESPACE=ADT/Agents`—make the batched query request them
  (the Agents tab does not use them yet, see above).

Default (both unset): no filters are created, and the background compute uses
the Insights fallback.

## Console deep-links

Each agent row carries two additive, optional fields built server-side (AWS Region
from `AWS_REGION`, default `us-east-1`) by the pure helper
`agents/console_links.py::agentcore_console_links(region, log_group)`:

* `observability_url`—the CloudWatch **GenAI Observability** page (AgentCore
  tab): `https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#gen-ai-observability`.
* `logs_url`—the exact runtime **log-group** deep-link:
  `https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{enc}`
  where `{enc}` is the runtime log group with every `/` replaced by `$252F`
  (the console fragment's double-URL-encoding). Omitted / `None` when the log
  group is unknown (for example the local provider); `observability_url` is
  still emitted. The frontend (`frontend/src/shared/agents/cards.tsx` `AgentCard`)
  renders a small `Observability ↗` / `Logs ↗` link row per card and renders
  **nothing** when `observability_url` is absent and degrades gracefully
  with older or local payloads.

## Evaluation scores (managed AgentCore evaluations)

The orchestrator row's `eval_scores` come from **managed AgentCore
evaluations**: built-in Faithfulness / ResponseRelevance / Correctness
evaluators scored over **sampled production traces** of the
`adt_orchestrator` runtime (default sampling `10%`), plus the code-based
[figure check](#the-figure-check-code-based-evaluator). (The service's built-in
catalog has no `Builtin.Groundedness` / `Builtin.AnswerRelevance`;
`Builtin.Faithfulness` is the grounding-in-context metric and
`Builtin.ResponseRelevance` the answer-relevance metric.)

**Where the config comes from.** The evaluation config is a **CDK-owned
AWS CloudFormation resource**: `AWS::BedrockAgentCore::OnlineEvaluationConfig`
named `adt_orchestrator_eval`, created by `stacks/_evaluations.py` on
`ADTComputeStack` together with its evaluation execution role. `deploy.sh`
section **`[2c-eval]`** only **persists the READY orchestrator runtime ARN**
to the SSM parameter `/adt/orchestrator-runtime-arn` (automatic,
no flag, **non-fatal**: a failed write only delays evaluations to a later
deploy). app.py resolves the ARN at CDK synth (sticky:
`-c orchestrator_runtime_arn` > SSM > empty; fail-soft) and the construct
derives the trace log group + OTEL service name from it, then sets the
`EVAL_CONFIG_ORCHESTRATOR` Amazon ECS task env **directly from the resource's id
attribute** (no SSM round-trip for the id). While the ARN is absent (first
deploy, offline synth) nothing is added and the template is unchanged.
Teardown is the ordinary stack delete—no imperative cleanup.

**How scores are read.** `agents/agents_routes.py` maps the env into
`EVAL_CONFIG_MAP` (orchestrator-only by design—system agents have no
evaluation configs) and `_get_eval_scores` reads the results from the
`/aws/bedrock-agentcore/evaluations/*` CloudWatch log groups (known paths
first, then prefix discovery). AgentCore writes one record per scored trace
in the OpenTelemetry GenAI evaluation shape: `name` is
`gen_ai.evaluation.result`, and `attributes.gen_ai.evaluation.name`,
`.score.value` and `.score.label` carry the evaluator, its score and its
label. Two Logs Insights queries read those fields for every evaluator, over
one log group and window: each evaluator's average score
(`EVAL_SCORE_QUERY`, parsed by the pure `parse_eval_rows`) and the counts by
evaluator and label (`EVAL_LABEL_QUERY`, used for the figure check). The
reader's earlier three guessed formats returned no rows on a real results
log, so the page showed no scores before this. Because evaluations are sparse and
lag behind sessions, the reader always widens the query window to **at least
7 days** regardless of the page's `hours` selector. This runs only in the
background snapshot compute, never on the request path.

**First-deploy chicken-and-egg (narrowed).** Deploy #1's CDK synth runs
*before* `[2c-eval]` persists the runtime ARN, so neither the config nor the
env exists yet. On deploy #2's stack update **both land together** (the env
references the resource attribute, so they cannot drift apart). Scores appear
after that deploy plus enough live traffic for sampled evaluations to
accumulate.

**Complement, not replacement.** Managed evaluations = continuous
statistical quality over production traces; `scripts/smoke_prompts.py` =
deterministic pre/post-deploy grounding gate with exit-code contract—keep
both. Evaluations are not a CI gate, and the response-integrity posture
(`RESPONSE_INTEGRITY_MODE`, ADR-0046/0047) is unchanged by them.

### The figure check (code-based evaluator)

Beside the three judges, the same config runs **`adt_figure_check`**, a
code-based evaluator (`AWS::BedrockAgentCore::Evaluator`, level `TRACE`, one
orchestrator turn) backed by an AWS Lambda function that runs
`agents/eval_figure_check.py`. A judge grades an answer against the agent's
own findings, so it cannot see that a total is not the sum of what the system
agents returned; this check can, in code, with no model call.

**What it checks.** For the sampled turn it reads the final answer, the
turn's prompt and every tool result from the trace's spans, then runs the
app's own figure reader (`agents/answer_figures.py`, the same one the engine
runs inside each system agent): each figure the answer presents as a total or
count must be in a tool result of that turn or in the prompt (which carries
the route seed and metric-layer results), be a sum or difference of two
counts the results state, or be the sum of the figures the answer lists next
to it. A figure a system agent's own engine line already named as in no
result fails even though that line writes it. Ids and UUIDs in tool text
never account for a figure (they are blanked first), nor do the citation
stamps or percentages.

**A pair holds a figure only from counts.** System-agent results are prose,
so the reader's pair rule (any sum or difference of two returned values)
took every number in them as an operand: a percentage, a line or station
number, minutes, a small count. A figure off by any such number passed (on
real turns, a wrong total passed as the right one plus a "42%" written in the
same result). The evaluator blanks percentages from tool results and gives the
reader pair operands that are only counts a result states: the COUNT/SUM
cells of a structured result and their sums, count-named fields, the
figures it reads as totals or counts in a result's prose, and the engine's
totals lines, never a figure an engine line flagged. Direct support (a
number in a result, the prompt, a listed sum) is unchanged, and so are the
reader and the engine.

**Counts stated without a total word.** Measured on a deployment's real
answers, the orchestrator writes most counts as `N <count noun>` (`4,941
SITE9 item records`), `<label naming a count noun>: N` (`CLOSED orders:
8,182`, `Records matching part name X: 318`) or in a table under a
multi-word count header (`Record Count`), and the app's reader reads a count
only under a total, sum or count word or a one-word count header, so at
first the check read no figure at all in most turns. The evaluator reads
these shapes as well, with the reader's own count nouns, number spelling and
skip filters (no percentage, decimal, signed figure, date, time, duration,
approximation, average/rate/share/change wording, per-unit rate, or
identifier word before the figure or in the label's head), and holds them to
the same support tests. The engine's in-app check (the same reader inside
each system agent) has the same blind spot; `answer_figures.stated_figures`
is left unchanged here because widening it changes what live answers carry,
so that is a follow-up with its own live check.

| Label | Value | Meaning |
|---|---|---|
| `PASS` | 1.0 | Every stated total or count is accounted for. |
| `FAIL` | 0.0 | At least one is not, or was flagged; the explanation names up to three. |
| `NO_FIGURES` | none | The answer states no total or count the reader reads. |
| `UNCHECKED` | none | It states some, but there was nothing whole to compare them with (no tool result, a result too long to read whole, or a cut result that left nothing unaccounted). |

A payload it cannot read (wrong schema, no answer, a tool result missing or
cut short in the spans, or a tool result whose call the spans do not record)
returns the error shape, never a label. A list's length never accounts for a
figure: a count must be one a result states.

**What it cannot check.** It compares the answer with what the agents
returned, never with the data: it re-queries nothing, so an answer whose
agents were wrong in the same way passes. It checks only the figures it reads
(labelled totals, total phrases, count-column table cells, and the count
shapes above); a figure written another way (a figure quoted with no count
noun) is not checked. It holds the reader's own rule for plain text: a
correct total of three or more agent figures, stated without listing them
next to it, counts as unaccounted. A count a result writes without its own
count noun (`1,200 and 800 records`: only 800 is read) is no pair operand, so
a correct sum of such counts fails unless the answer lists them beside it.
And where a result lists many small counts, a figure off by a small amount
can still be the sum or difference of two of them.

**Cost.** One Lambda invocation per sampled trace (10% of orchestrator
traces), typically well under a second; no model call. The function needs no
network access and has no environment variables; it runs under its own role,
with no managed policy, that may only create streams in and write to its own
log group. Its asset is plain Python
staged at synth (the handler in `lambda-figure-check/` plus the check's
import closure, standard library only), so nothing is installed or built at
deploy time.

**On the Evals page** (Admin > Evals, Managed AgentCore Evaluations) it shows
as **Figure check**: its average is the pass rate over PASS and FAIL, and the
NO_FIGURES and UNCHECKED counts sit beside it, never averaged in (the
orchestrator's Agents card shows the pass rate alone). The reader counts its
results by its label field (`EVAL_LABEL_QUERY` in `agents/agents_routes.py`,
on the figure check's own rows, under its name or its id), so a rate of 0% is
shown, not dropped, and the judges' averages are unchanged by it.

**Changing it.** While the online config is enabled, AgentCore locks the
evaluator: the Lambda's code can change on any deploy, but a change to the
evaluator resource itself (name, level, Lambda ARN or timeout) needs the
config disabled first.

## Per-session drill-down (opt-in)

The Agents page includes an in-app per-session drill-down built from
OpenTelemetry (OTEL) spans. It is **strictly opt-in** (default off) because it
depends on CloudWatch Transaction Search span ingestion, which bills
continuously while enabled.

**Enable it** by deploying with the `--observability` flag:

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --observability
```

The flag is **sticky** (SSM parameter `/adt/observability-enabled`—same
discipline as `--highbyte`): later deploys without the flag keep the
drill-down. The deploy performs three things:

1. **Transaction Search enablement** (account/Region-level, idempotent,
   non-fatal on failure): `aws xray update-trace-segment-destination
   --destination CloudWatchLogs` turns on span ingestion into CloudWatch Logs
   (where the spans land is described under "Where spans land" below), and an
   indexing rule is set with a conservative
   sampling percentage (`OBSERVABILITY_SAMPLING_PCT` env at deploy time,
   default `10`).
2. **Reader env**: `AGENTS_OBSERVABILITY_DRILLDOWN=true` on the ECS app task
   (wired by `stacks/_observability.py` from the sticky flag).
3. **Reader IAM**: flag-gated `logs:StartQuery` on the `aws/spans` log group
   and on the ADT runtimes' own log groups
   (`/aws/bedrock-agentcore/runtimes/adt_*`), `logs:DescribeLogGroups` (to
   check which of those groups exist; the action has no resource-level
   permissions), and `xray:GetTraceSegmentDestination` for the app task role.
   The runtime listing (`bedrock-agentcore:ListAgentRuntimes`) is already
   covered by the task role's AgentCore policy. The scoped `StartQuery`
   statement is what the reader needs, but on the app task role it adds no
   new access: that role already has `logs:StartQuery` on `*` for the Agents
   page's other Logs Insights queries (metrics, log streams, evaluations),
   and narrowing that is a separate change. What keeps another application's
   AgentCore runtime groups out of the drill-down is the routes' own source
   selection, described below.

Span **emission** on the agent runtimes (`AGENT_OBSERVABILITY_ENABLED`) is on
with this flag (`AGENT_SPAN_EMISSION` on the app task).
Without the flag no span destination exists, so the runtimes get
`AGENT_OBSERVABILITY_ENABLED=false` and every OpenTelemetry exporter set to
`none`.

**What the drill-down shows.** Two admin-only routes
(`agents/agents_trace_routes.py`) read spans per chat session through Logs
Insights:

* `GET /api/admin/agents/traces/sessions?hours=`—recent sessions
  (`session_id`, first/last-seen timestamps, span count).
* `GET /api/admin/agents/traces/sessions/{session_id}?hours=`—the ordered
  span timeline for one session (span name, start, duration, status), per-span
  token attribution (`gen_ai.usage.*` attributes when present), a tool-call
  timeline (gateway tool-call spans), and session totals (tokens in/out, tool
  calls).

The frontend panel (`SessionDrilldown` on the Agents page) feature-detects:
when the env gate is off both routes return `{"enabled": false}` (HTTP 200,
zero AWS calls) and the panel renders nothing. On a Logs Insights failure the
routes keep the response shape, leave the rows empty, and add an `"error"`
string—an empty timeline is always distinguishable from a failed query.

**Where spans land.** AgentCore Runtime writes an agent's spans to one of two
places, and the routes read both:

* the `spans` log stream of the agent's own runtime log group,
  `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`. This is AgentCore's
  unified span destination, the default for agents created once the Region
  supports it, so it is where the ADT runtimes that `deploy.sh` creates write.
  The runtimes are created at deploy time, so on each request the routes list
  the AgentCore runtimes, keep the ADT ones (names starting `adt_`), and read
  the `spans` stream of each one's log group that exists (a runtime that was
  never invoked has no log group yet; the other streams hold the agent's
  application logs);
* the shared `aws/spans` log group, where agents created before that default
  (or with `UNIFIED_TRACES_DESTINATION_ENABLED=false` on the runtime) keep
  writing.

Only this deployment's live runtimes are read. Another application's AgentCore
runtimes in the same account and Region are never queried, and neither is the
log group a deleted ADT runtime left behind: once a runtime is retired (for
example offboarded, or replaced by a redeploy under a new id), its sessions
drop out of the drill-down even though its log group still holds them.

All sources are read by one Logs Insights query per request, so a session that
appears in several of them is shown once with all its spans counted. That
query takes at most 50 log groups (only groups that exist count, so an absent
`aws/spans` or a runtime not yet invoked takes no slot); with more span
sources than that, the routes return an error and run
no query rather than splitting the work. Working out the sources is bounded
too: each request lists the runtimes in at most 10 pages of 100 (stopping as
soon as there are too many ADT runtimes for one query) and then checks
which of the candidate log groups exist by name, in at most two calls. An account with
more runtimes than 10 pages hold gets an error, not a partial list. If the
runtimes or log groups cannot be listed, or one of the groups cannot be
queried, the routes return the error rather than the sessions from the sources
that did work
([AgentCore span destination](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)).

> **Cost.** Transaction Search ingests trace spans into CloudWatch Logs
> (the runtime log groups' `spans` streams and `aws/spans`) and indexes a
> percentage of them; ingestion, storage, and
> indexing bill continuously while enabled. To turn it off: delete the
> `/adt/observability-enabled` SSM parameter, redeploy, and
> revert the destination with
> `aws xray update-trace-segment-destination --destination XRay`.

> **Deployed-env confirmation note.** The exact span attribute names emitted
> by the ADOT auto-instrumentation with the pinned agent framework
> (`gen_ai.usage.*` token attributes, the session-correlation attribute) are
> encoded as **data**—candidate-name tables in
> `agents/agents_trace_routes.py`, from which both the Insights queries and
> the parser are generated. A deployed environment (measured 2026-10-22,
> us-east-1) confirmed `attributes.session.id`, `name` and
> `attributes.gen_ai.usage.input_tokens` / `output_tokens` on the spans the
> ADT runtimes write to the `spans` stream of their own runtime log groups;
> the shared `aws/spans` group existed but held no events. If a name changes
> in a later framework version, the fix is a one-line table edit. Until a
> `--observability` deploy and live traffic put spans in one of those places,
> the drill-down honestly shows no sessions.

`avg_events_per_session` still uses the background CloudWatch Logs Insights
fallback described above—it was deliberately not retrofitted onto spans.
