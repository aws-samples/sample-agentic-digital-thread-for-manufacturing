<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0055 — Bedrock Mantle models, one provider seam, and live model switching

Status: Accepted (2026-07-28)
Related: ADR-0028 (strands pin), ADR-0049 (reserved tool args), ADR-0053
(measured-only cost), ADR-0054 (measurement honesty + experiment harness)

## Context

Two model surfaces now exist in Amazon Bedrock:

* **Converse on `bedrock-runtime`** — Anthropic, Amazon Nova, and the
  open-weight `openai.gpt-oss*` family. This is what ADT has always used;
  strands builds a `BedrockModel` implicitly from a model-id string.
* **Bedrock Mantle** (`bedrock-mantle.{region}.api.aws`) — an
  OpenAI-compatible surface and the ONLY way to reach the GPT-5.x family.

Investigated live on 2026-07-28 from both demo accounts (111122223333,
444455556666):

* Mantle is reachable and SigV4-authenticated with **no account
  configuration** — no account-level entitlement or allowlist change is
  required. Access is not entitlement-gated.
* `GET /v1/models` lists `openai.gpt-5.6-luna|sol|terra` with
  `"status": "available"`.
* **GPT-5.x is served only at `/openai/v1/…`.** The generic `/v1/responses`
  path answers `400 "The model 'openai.gpt-5.6-luna' does not support the
  '/v1/responses' API"`. That message resembles a permissions error but
  indicates the wrong path. `openai.gpt-oss-20b` returns 200 on the same
  generic path, which confirms that inference itself is not gated.
* `strands-agents==1.42.0` — the existing pin — **already ships** the provider
  (`models/openai_responses.py`, `models/_openai_bedrock.py`) with ToolSpec
  support. No upgrade is needed. Verified end-to-end before wiring: a Luna
  agent executed a function tool and returned usage metrics (198 in / 37 out),
  so the ADR-0053 cost ledger keeps working.
* strands' own `bedrock_mantle_config` helper builds the GENERIC `/v1` base
  url (`_openai_bedrock.py`), so it cannot reach GPT-5.x today.

## Decision

**(a) One provider seam.** `agents/model_provider.py::resolve_model(model_id)`
is the single place a configured id becomes what `strands.Agent(model=...)`
takes, used by BOTH the orchestrator (`orchestrator.py`) and every system
agent (`system_agent.py`). Converse ids are returned **unchanged** so
pre-existing behavior is byte-identical (test-pinned). Mantle-only ids build an
`OpenAIResponsesModel` with an explicit `base_url` of
`https://bedrock-mantle.{region}.api.aws/openai/v1` and a bearer token minted
per call via `aws_bedrock_token_generator.provide_token`.

**(b) Mantle-only is a prefix rule, deliberately narrow.** Only
`openai.gpt-5*` routes to Mantle. `gpt-oss` answers on Converse, so it rides
the plain path and inherits retries, streaming, and metrics unchanged.

**(c) Token lifetime is a non-issue by construction.** ADT builds its agents
PER TURN, so every turn mints a fresh token. A cached model object would
outlive its token — do not add one without a refresh.

**(d) Failure is LOUD.** A Mantle model that cannot be constructed (missing
`openai` extra, token-mint failure, missing IAM) raises. Silently falling back
to another model would misreport which model produced an answer and corrupt
every A/B comparison the ADR-0054 harness makes.

**(e) Mantle ids are never Region-prefixed.** `model_catalog.resolve_id`
returns `openai.*` ids untouched; `us.openai.gpt-5.6-luna` is rejected by the
service. `model_provider.strip_region_prefix` defends the same invariant on
the way in.

**(f) Catalog carries the serving surface.** `Model.mantle` marks these so the
UI can say the surface differs. Prices are the verified STANDARD public rates
(per MTok: Sol $5/$30, Terra $2.50/$15, Luna $1/$6) — introductory rates stay
forbidden (ADR-0054). Admin selectors need no change: they already read the
catalog.

**(g) Both model roles switch LIVE — no re-onboard.** `SYSTEM_AGENT_MODEL` is
baked into a system-agent runtime's env at provision, which historically made
an agent-model change a re-onboard. `AgentManager.refresh_agent_model` now
updates that env in place and deploys a new runtime version, reusing the same
`_bump_runtime_version` seam the business-context refresh uses (ADR-0054 D4);
existing env is preserved and only the model key overridden. `PUT
/api/admin/llm-settings` applies it fleet-wide and reports an honest per-system
tally (`ok`/`failed`/`skipped`). `reonboard_required` is now set ONLY when the
live bump did not take — a failed system still runs its old model, so the
steward is told the fallback.

**(h) A Converse stream that stalls before any content is retried once
(2026-10-10).** Seen live: `converse_stream` returned its headers, then no bytes
arrived for the 120 s read timeout. botocore had already returned, so its
retries never apply, and strands' event loop retries only throttling, so the
whole answer failed with "Query failed." `build_model` installs
`agents/model_stream_retry.guard_stream_stalls` on every Converse model (both
roles; same object, class and config). On the first attempt it holds leading
`messageStart` chunks, and while no content chunk has been released it retries
a `urllib3.exceptions.ReadTimeoutError` ONCE with the identical request. Safe
because nothing has reached the caller and no tool has run. The final attempt
passes every chunk straight through. After any content chunk, and for every
other error (throttling, validation, connection resets, botocore's
request-phase `ReadTimeoutError`, which botocore handles under its configured
retry policy), the error propagates unchanged. No retry is sent when the turn
was cancelled (`Agent.cancel()`), or when the caller's declared window has less
than `STALL_RETRY_MIN_REMAINING_S` (100 s) left. A Mode-A system query declares
a 300 s window (`SYSTEM_QUERY_CALLER_WINDOW_S`): the orchestrator's gateway MCP
client keeps the mcp default 300 s read timeout, so a retry the orchestrator
could no longer receive is not sent. The 100 s threshold is a measured
heuristic, not a guarantee of the caller's deadline (the window starts when the
system runtime begins the query, after gateway dispatch): it covers the slowest
whole system call that completed on a deployed workshop (78.5 s over
376 calls), so a stall on a system agent's first model call (180 s left) is
retried. A retried stall's wait is recorded in `invocation_state`
(`STALL_SECONDS_STATE_KEY`) and the system agent's 60 s tool wall budget
(`_ToolCallBudget`) does not count it, so the retried answer still gets its
data tools; the 300 s caller window above is what bounds the total. The
orchestrator declares no window: its runtime sends 30 s heartbeats, so the
app's 300 s read stays alive through the wait. Cost: at most one extra model call and one extra
120 s wait per model call. Mantle models use another transport and are not
wrapped. Limits: strands still checks the cancel signal only per chunk, so a
cancel during a stall is noticed when that attempt ends; a stall after content
still fails the turn.

**(i) Onboarding's helper calls follow both roles onto Mantle (2026-10-23).**
`db.bedrock_call` (onboarding discovery, classification and mapping helpers,
the onboarding chat's `map_fields` and `resolve_identities`, the
knowledge-graph node analysis and the context interview) tries the configured
system-agent model, then the orchestrator model. A GPT-5.x id there is built by
`build_model` (8192-token cap) and run as one tool-free strands turn with no
retry of its own; every other id keeps its Converse request. A Mantle throttle,
5xx or 408 is retried like a Converse throttle, service-unavailable or model
timeout. The saved agent model is read on every call (one SSM client per
process), so a save applies to the next call. Falling back from the agent model
to the orchestrator model on a failed call is this helper's own policy for
every model family, logged with the model id; it is not the construction-time
substitution (d) rejects, and the ADR-0054 harness does not measure these
calls. The onboarding runtime role carries the Mantle grant.

**(j) The memory extraction model is a third live switch (amendment
2026-10-27).** The AgentCore Memory's custom strategies name the model that
extracts and consolidates long-term records. The Admin LLM settings page now
offers it on base and demo deploys, from `model_catalog.MEMORY_EXTRACTION_FAMILIES`
(the five models AgentCore Memory accepted as a custom-strategy model on
2026-10-26 in us-east-1; no Mantle model), intersected with `ALLOWED_MODELS`.
`POST /api/admin/llm-settings/memory-model` with `{"memory_model": "<id>"}`
is refused with 400 before any write when the model is not offered or on
the workshop (which keeps its Nova Pro pin), checked from the deployment
alone: the request never loads the app's config, whose discovery runs on
default clients (the settings GET likewise reads the memory id only from an
already-loaded config, `config.peek_config`, and until then says the
settings are loading and starts one background load). Otherwise it
starts a background job (`agents/memory_model_job.py`) and answers 202 with
its id; the worker finds the memory and ends the job with an error when
there is none. The page polls `GET /api/admin/llm-settings/memory-model/job/{id}`
(and reads `.../job`, the latest switch, on load). The switch is a job, not
a request, because AgentCore took about 131 s to settle one on adt-demo
(2026-10-27) while a request through CloudFront and the ALB lasts at most
120 s: the first version ran it inside the settings PUT, whose client got an
empty-body error at 120 s and whose own 120 s wait expired about 11 s before
the memory settled, so the memory ran the new model with nothing saved (the
offboard-504 class, ADR-0061). `PUT /api/admin/llm-settings` now refuses a
`memory_model` with 400 and is otherwise as before this amendment. The
job's worker (`agents/memory_model.py`) sends ONE UpdateMemory built from
the current GetMemory configuration with only each `modelId` replaced, waits
(bounded, 600 s) until the memory and every strategy are ACTIVE on the new
model, and only then writes SSM `/adt/llm-memory-model`. A failed or
unconfirmed update writes nothing. `deploy.sh` passes the saved model back as
`-c memory_extraction_model`, which the stack resolves for its Region
(`MEMORY_EXTRACTION_MODEL` still wins) and refuses on a workshop synth, so
the choice survives a deploy and a template with nothing saved is unchanged.
The switch is serialised by the JobStore slot `memory-model-save`
(`JobStore.claim_slot`, shared by every app task; a second switch is refused
with 409; the job id is the slot owner; the claim is heartbeated while the
job waits, released in the worker's `finally`, and taken over after 900 s,
above the job's longest run), not `RUNTIME_CONFIG_LOCK`, because it waits for
the memory and that lock is never held across a wait. The claim names the
job, its model and when it started, so it is the running switch's record on
every task, and the POST writes nothing else: the job record is the
worker's first write, and a worker whose record the shared table will not
take stops before it reads or changes the memory. The worker records the
outcome twice, in its job record (its own JobStore kind) and in the
exact-owner release of the slot, which carries the same result. A status
read takes the slot first and then the job record: the slot's result for
this job or a finished record is the answer; a slot still holding the job
reads as running until it has been quiet for 300 s (a running job writes it
at least every `JOB_SILENCE_MAX_SECONDS`, 106 s), then as `unknown`; a
record still running that the slot no longer holds is `unknown` too. Nothing
is kept as a task-local copy. Every call to the shared table goes through a
bounded client (`memory_model.store_client_config`: 1 s connect, 3 s read,
two attempts in botocore standard mode, at most 14 s with botocore's wait
before the retry), and the POST and each status read make at most two such
calls one after the other, so each answers within 28 s, well inside the
origin's 120 s. The AgentCore and SSM clients make two attempts in all
(`total_max_attempts`; a bare `max_attempts: 2` is three), at most 32 s a
call, so the job's longest run from its `started_at` (`JOB_MAX_SECONDS`: the
600 s wait, one poll interval, five such calls and seven store calls, the
POST's claim included) is 863 s, below the 900 s stale bound. These are
bounds under the failures the clients handle (connect, read, attempts), not
wall-clock limits: DNS, a refresh of the task's credentials and an answer
that trickles are not bounded, here or on any other AWS call the app makes.
The worker therefore shows it still holds the slot (a strict heartbeat)
immediately before UpdateMemory and before the SSM write; what is left is a
mutating call that stalls past the stale bound after that check (the
`memory_model` docstring and `docs/deployment.md`). A strict claim also
accepts a slot held by its own owner, so botocore's retry of a claim that
landed but whose answer was lost takes the slot instead of refusing it.
The switch uses the slot's `strict=True` mode: with the shared table
configured, a backend error or a backend the fail-soft path disabled refuses
the claim (503, before any read or write) instead of deciding from one task's
memory, and a heartbeat that cannot be written stops the wait before SSM; the
other slot consumers keep the fail-soft default.
"Unchanged" means the memory and every strategy are ACTIVE on the model: a
FAILED or still changing memory is refused, never reported unchanged; an
unchanged switch writes SSM only, which is how the page repairs a model that
nothing saved. After
writing SSM the switch reads the memory once more and reports, without
repairing SSM, a memory that is not ACTIVE on the model (FAILED, still
changing, or moved by something else). The GET compares the live model with
what the next deploy would apply: the saved value as that deploy resolves it
(`model_catalog.effective_memory_model`, the rule
`_model_allowlist.py allows ... memory` and the stack apply), reported as
`memory_model_saved` when it differs, a value the next deploy ignores as
`memory_model_saved_invalid`, and with nothing valid saved the stack default
for the Region, reported as `memory_model_unsaved` with
`memory_model_next_deploy` when the memory runs another model. `deploy.sh`
reads the parameter with
`get-parameters`, so a missing parameter keeps the default while a failed
read stops the deploy before CDK.

## Consequences

* GPT-5.6 Sol/Terra are selectable as orchestrator, Luna as a system agent,
  in the existing admin dropdowns.
* An orchestrator OR agent model change is now a runtime version bump
  (~1–2 min) instead of a re-onboard (~90 min for 8 systems).
* **Data-retention caveat that must reach the steward:** Luna's
  `allowed_modes` are `default` and `provider_data_share` only, with no
  `none` mode. The [Amazon Bedrock data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html)
  docs say a model's `allowed_modes` decide how Amazon Bedrock retains a
  request, and a model that supports `none` uses zero data retention.
  Acceptable for demo data; read those docs before customer traffic.
* **Known risk, deliberately staged.** `openai_responses.py`'s own docstring
  flags MCP support as PARTIAL ("approval flow and `mcp_list_tools`/`mcp_call`
  events not surfaced"). The ORCHESTRATOR's tools arrive over the AgentCore
  Gateway via MCP, so a GPT-5.x orchestrator is the higher-risk case; system
  agents use plain function tools. The models are therefore offered for BOTH
  roles but the first measured experiment is a system-agent swap (Luna vs
  Haiku 4.5) through the ADR-0054 A/B harness.
* At Amazon Bedrock list prices as of 2026-07-29, Luna is $1/$6 per MTok and
  Claude Haiku 4.5 is $1/$5 (prices change; see
  [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)). The
  experiment measures capability, not cost.

## Considered and rejected

* **Upgrading strands to get Mantle support** — unnecessary; 1.42.0 has it, and
  the ADR-0028 pin exists because 1.43+/1.44 regressed the tool-use loop.
* **Using `bedrock_mantle_config=`** — cannot reach GPT-5.x (generic `/v1`).
  Kept as the preferred path for any Mantle model that DOES accept `/v1`, so
  we inherit strands' token refresh there.
* **Per-request model override via a reserved tool arg** — rejected for the
  same reason ADR-0049 keeps `_adt_identity` narrow.
* **Silent fallback to a Converse model when Mantle fails** — rejected; see (d).
