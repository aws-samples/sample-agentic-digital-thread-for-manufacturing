<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0068: Conversational onboarding context — interview and concept proposals ride the onboarding chat

## Status

Accepted — 2026-09-11. Builds on [ADR-0021](0021-two-layer-concept-model.md)
(two-layer concept model), [ADR-0050](0050-vocabulary-and-artifact-management.md)
(vocabulary import), [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
/ [ADR-0063](0063-proposal-precision-and-requirement-promotion.md) (authority
posture), and [ADR-0054](0054-measurement-honesty-and-experiment-harness.md) D4/D5
(business context is untrusted steward text).

## Context

Business-context and concept collection lived in a standalone sidebar panel
(`GuidedContextFlow`) on the onboard page, shown before any system existed. It
was disconnected from the onboarding conversation where the schema knowledge
actually accumulates: the steward answered deployment questions in one surface
and onboarded systems in another, and nothing connected a poor vocabulary fit
(discovered columns with no concept candidates) to the moment the steward could
still fix it — between `discover` and `map_fields`.

The interleave is valuable because `map_fields` rebuilds the concept vocabulary
live per call: concepts accepted between discover and map are picked up with NO
re-map.

Two constraints shaped the design:

- **Process boundary.** The guided draft + answered-topics store
  (`agents/context_interview/state.py`) is in-memory in the FastAPI app
  process; onboarding tools execute in the onboarding AgentCore runtime. A
  runtime-side `record_business_context`/`ask_business_context` tool can never
  append to the app's draft — such tools are infeasible as specified and were
  rejected.
- **Untrusted-text discipline (ADR-0054 D4).** Steward answers are untrusted
  text. Routing them through agent tool calls would pass them through the LLM;
  keeping the interview in a frontend card posting directly to the guided
  endpoints means steward answers never transit the model, and steward
  authority stays in the card.

## Decision

### 1. The `GuidedContextFlow` panel is retired

Rollback is `git revert` (no feature flag, no shim — replace-not-accrete). Its
pure helpers, types, and `GUIDED_*` URL constants moved to
`frontend/src/views/onboard/components/context-shared.ts`. `PreMapProbe`
remains the WIZARD-path surface, repointed to the shared helpers with no
behavior change.

### 2. Two read-only onboarding-agent tools (explore tier)

Both appended to `ONBOARD_TOOLS` in the explore tier (now 9 explore + 4
commit); no LLM call participates in either result:

- `assess_vocabulary_fit(system_id)` — a deterministic fit report from
  `agents/vocab_fit.py::compute_fit` (import-pure, stdlib-only; pinned
  thresholds `FIT_OK_PCT = 70` / `FIT_LOW_PCT = 40` drive
  `suggestion: ok|ask_context|consider_pack`; empty schema ⇒ `ok`, never nag).
  It reports `context_questions` as a COUNT only — question text stays
  server-owned in `agents/context_interview/questions.py`.
- `propose_concepts(system_id, concepts)` — a ZERO-write preview classifying
  proposed rows through the ONE import classifier home
  (`agents/vocab_import_core.py`, shared by `/import` and `/import/preview` —
  the C6/C7 no-drift-by-construction contract). The tool imports no write
  seam; a test monkeypatches the write seam to raise and asserts it is
  untouched.

The SIGNAL is the tool's; the judgment to surface or propose is the agent's.
The agent NEVER claims a concept was added — the steward accepts in the card.

### 3. Results ride the EXISTING checkpoint pipeline; two chat cards

`OnboardToolHook._on_tool_done` (AfterToolCallEvent, `agents/onboard.py`)
pushes `{'event': 'checkpoint', 'data': {'step': <name>, 'result': <dict>}}`;
`chat.ts::obStreamChat` calls `appendChatCheckpoint(step, result)`;
`ObFlow.ChatCheckpoint` renders per-step bodies. Two new steps: `vocab_fit`
and `concept_proposal`. No new event name, no new HTTP route for delivery.

- **Context Interview card** (`ContextInterviewCard.tsx`) fetches questions
  from and posts answers to the EXISTING guided backend
  (`/api/onboard/guided/{state,questions,turn,probe,confirm-context,dismiss}`)
  — exactly `PreMapProbe`'s transport. Question wording, sanitization, and
  landing stay in their single home (`agents/context_interview/`); the card
  carries ZERO question text, and steward answers never transit the LLM.
- **Concept Proposal card** (`ConceptProposalCard.tsx`) renders the
  `propose_concepts` preview; the steward click is the ONLY write trigger.

### 4. Recorded flag decisions (binding, taken 2026-09-11)

- **Flag 1 (ask/record tools) — rejected.** App-process draft state is
  unreachable from the runtime, and answers must bypass the LLM (see Context).
  The card is the transport; `assess_vocabulary_fit` only reports that
  questions are WORTH asking (a count), never the questions.
- **Flag 2 (import gate) — option (b).** The admin import route is Admin-gated
  while onboarding is integrator-OR-Admin, so an integrator's accept would
  403. A thin onboard-gated route `POST /api/onboard/vocabulary/import`
  (guarded by `require_onboard_access`) delegates to the SAME
  `vocabulary_routes._run_import` — no second classifier, no second writer.
  The Concept Proposal card calls THIS route; the admin route is
  byte-unchanged. Provenance lands as `pack:custom:<slug>` via the untouched
  writer.
- **Flag 3 (question scopes).** The card asks BOTH scopes: deployment
  questions (`GET /guided/questions` → `POST /guided/turn`) only while
  `guided_state().first_launch` and not dismissed, then per-system ones
  (`POST /guided/probe/{id}` — the server derives questions from the stored
  agent-path discovery; the card sends NO tables). No question text is
  duplicated anywhere.
- **Flag 4 (overlays) — v1 is NEW concepts only.** `propose_concepts` accepts
  items WITHOUT `overlay_on`; an item naming an existing/universal concept id
  classifies `skip_exact`/`skip_collision` and the card explains "already in
  vocabulary — refine its meaning via a proposal". Full overlay proposals
  (ADR-0021 `ConceptOverlay` through the proposal lifecycle's
  `concept_overlay` type, filed via `log_proposed_mapping`) are DEFERRED.
- **Flag 5 (confirm timing).** Pre-provision, `POST /guided/confirm-context`
  cannot roll a not-yet-provisioned agent, so the roll reports non-`ok` even
  though the graph write landed. The card treats `written: true` + non-ok
  roll as success-with-note ("agent not provisioned yet — the context is
  saved and read when the agent starts") — helper `preProvisionConfirmLine`
  in the card. Agents read business context at container start
  (provision-time compose).
- **Flag 6 (gateway exposure) — verified during implementation.** `stacks/`
  pins NO tool names: the onboarding gateway target is created imperatively
  by `deploy.sh` §2e and its tools sync from the runtime's MCP `tools/list`.
  Deployed environments therefore need the onboarding runtime IMAGE
  redeployed (the new tools ship in the image; `deploy.sh` handles it) plus
  the onboarding gateway target re-synchronized — NO per-system re-onboard.
  Cedar (ADR-0033 — deferred, unattached) must account for the two new
  explore-tier tool action ids (`onboarding___assess_vocabulary_fit`,
  `onboarding___propose_concepts`) when the policy engine is eventually
  attached; as explore-tier reads they ride the baseline permit, not the
  commit-tier forbid.
- **Flag 7 (day-0 home) — accepted trade-off.** The deployment interview
  loses its pre-first-system home: deployment-scope questions are asked
  during the FIRST system's onboarding conversation. Nothing strands —
  `first_launch` stays true until confirm (the draft-preserving rule in
  `state.guided_state()`), and dismiss reuses `POST /guided/dismiss`.

### 5. Authority posture

The agent proposes; the steward accepts through existing routes. The machine
never writes concepts or context: both tools are read-only by construction,
vocabulary fit is COMPUTED (never LLM-judged — the determinism discipline of
`agents/determinism.py`), and the accept click in the card is the only write
trigger (ADR-0052/0063 posture; consistent with "Schema drift never mutates
the graph", ADR-0053).

## Consequences

- Context capture happens where the schema knowledge is — in the onboarding
  conversation, between discover and map, when accepting a concept still
  changes the mapping outcome without a re-map.
- One classifier home (`vocab_import_core`), one writer (`_run_import`), one
  question-text home (`context_interview/questions.py`) — the cards and tools
  added zero duplicated logic and zero new delivery events.
- `chatCheckpoint` entries are collapsible; an answered interview folding away
  on step-collapse is correct.
- The day-0 interview now requires a first onboarding to trigger; a
  deployment that never onboards a system is never asked (accepted, flag 7).
- Overlay proposals remain a gap (flag 4): refining a universal concept's
  meaning still goes through the manual proposal lifecycle.
- The wizard path is unchanged: `PreMapProbe` stays. `RuntimeQuestions`
  is NOT a checkpoint-payload card; since 2026-09-24 it is a conversation
  entry in the onboarding chat opened by the bell deep link (ADR-0059
  amendment), not a side panel.
- e2e: the guided-panel journey reduces to an API-level assertion
  (`/api/onboard/guided/state` serves; panel selectors gone); the full
  conversational e2e is a follow-up needing a live system.

## Amendment 2026-10-18: the onboarding runtime exports only the tools the chat calls on the gateway

Decision 2 counted the onboarding tools as 9 explore + 4 commit, and flag 6
said the gateway target's tools sync from the runtime's `tools/list`. Until
now that list was all of `ONBOARD_TOOLS`. The onboarding gateway is
CUSTOM_JWT and accepts tokens from the app's interactive, service and edge
clients with no group check, and the per-tool Cedar policy is still deferred.
The app's integrator/Admin check protects only the app's own routes, so a
client holding any app token could call `connect_new_system`,
`provision_system`, `reset_system` or `get_catalog` on the gateway directly.
The runtime's `provision_system` also starts the older Step Functions
provisioner, which writes an agent config without the schema or concept list.

The deployed chat never calls those four on the gateway. The app answers
them itself (`onboard_chat_lifecycle`) and its gateway client hides the
gateway's (`GATEWAY_TOOL_FILTERS`). `discover` is also the app's in a
deployment, but the app forwards a case the runtime owns to the gateway's
`onboarding___discover`, so it stays.

Decision: the runtime exports `ONBOARD_TOOLS` without
`onboard_chat_lifecycle.NOT_ON_GATEWAY`, which is `APP_TOOL_NAMES` minus
`FORWARDED_TO_GATEWAY` (`{"discover"}`). The chat's filter and the exported
list come from the same names, so they cannot drift.
`test_onboarding_runtime_exports_only_gateway_tools.py` pins
exported == `ONBOARD_TOOLS` - (`APP_TOOL_NAMES` - `{"discover"}`), the chat's
filtered gateway set inside the exported set, and the forwarded discover
exported. The runtime now exports 9 tools: 8 explore and 1 commit
(`store_credentials`). `ONBOARD_TOOLS` is unchanged, so local mode
(`AGENT_MODE=local`, no gateway) keeps all 13 in-process tools.

The deferred commit-tier forbid (`policies/onboarding_gateway.cedar`) no
longer names `connect_new_system`, `provision_system` or `reset_system`. An
action the gateway target does not have would be rejected as "unrecognized
action" when the policy is attached. It still names `store_credentials` and
`discover`. The per-tool policy is still deferred, and it is still the
remaining fix for `store_credentials`: any app token can call that tool on
the gateway until the policy is attached and enforced.

Not changed: gateway authentication, the JWT authorizer settings, the
allowed client list, the Cedar attach step, and the runtime role's IAM
grants. Some of those grants are documented as being for the tools that are
no longer exported (Step Functions `StartExecution` for `provision_system`,
the graph delete for `reset_system`); narrowing them is a separate change.
A deployment picks this up when `deploy.sh` §2e updates the onboarding
runtime image and re-synchronizes its gateway target.

ADR-0032 and ADR-0033, which the code cites for the gateway tool split and
the Cedar layer, were never written (see the ADR index), so this amendment
is recorded here.

## Amendment 2026-10-21: `store_credentials` runs in the app

The 2026-10-18 amendment left one commit-tier tool on the gateway,
`store_credentials`, which creates or overwrites `adt/systems/<id>` in AWS
Secrets Manager. Any app token could still call it on the gateway directly,
past the app's integrator/Admin check, because the gateway has no per-tool
policy attached.

Decision: `store_credentials` joins `APP_TOOL_NAMES`. The deployed chat gets
the app's own tool of the same name and parameters
(`onboard_chat_lifecycle.store_credentials`), which writes the secret from the
app inside the `/api/onboard/chat` request. That request is behind the
router-level integrator/Admin gate, and the onboarding agent is built nowhere
else.

The app and the onboarding runtime are separate processes, and every later
reader (the runtime's test_connection and discover, the app's provision, a
restarted app's catalog) resolves a connection from the System node. The
secure credential form the chat shows (`POST /api/onboard/credentials`) and
the chat's tool run ONE implementation
(`onboard_chat_lifecycle.store_system_credentials`). It is one durable step,
held under the system's connection lock (the one Edit connection and
provision take; it gives up at once behind a provision): the System node is
read checked and the system classified from it, not from the app's catalog;
the secret is written and the app's cached copy of it dropped; the node's
`secret_arn` is pointed at it by one compare-and-set statement that lands only
while the node's connection type and pointer are still the ones read
(`connection_resolver.publish_secret_pointer`, which changes nothing else on
the node); and only then does the app's catalog change and the answer say
"stored". A node that changed meanwhile (another task's Edit) is a
`conflict`, and a pointer the graph did not take is `pointer_not_saved`; both
say what the written secret now means and leave the catalog unchanged.
Every store writes the same named secret, so a rotation keeps its ARN: an
agent the system already runs keeps the credentials it was started with until
it is provisioned again, and a process that read the secret in the last
minute may keep the old value for up to its cache lifetime. The stored answer
says so in a note, which the form shows, instead of claiming the new
credentials are in use. The store writes nothing, and says why, for a
system whose next steps would never read a secret from there: a Connect New System registration, a system
whose credentials are kept in its own `adt/conn/<id>` secret (the built-in
HighByte, a system an Admin edited), an MCP Server, a system that connects
with the deployment's own credentials (PostgreSQL routed to the deployment's
database, SiteWise (Direct), Athena), and a system with no connection record
on its node yet. That leaves the REST family (REST API, SiteWise (i3X), Tulip)
registered by the deployment.

The runtime now exports 8 tools, all explore tier, and no commit-tier tool.
`discover` stays on the gateway: Decision 2 classes it as explore, and the
chat forwards a runtime-owned case to it. The deferred forbid
(`policies/onboarding_gateway.cedar`) names only `discover`, pending Decision
2. Until the Cedar layer is attached and enforced, any app token can still
call the explore tools on the gateway (the maintainers' deferred-work list).

IAM: the onboarding runtime role grants only what the eight exported tools
call, from a static call inventory of them: secret reads (`adt/conn/*`,
`adt/systems/*`, the demo database owner secrets), the graph read tier, the
same-account SiteWise reads test_connection and discover make, and the
session table. It no longer carries any secret write, describe, restore or
delete, the graph write and delete tiers, or `states:StartExecution`: no
exported tool reaches any of them. The app task role reads `adt/systems/*`,
gains `iotsitewise:ListAssetModelProperties` for the same discovery, and its
secret writes are scoped to `adt/conn/*`, `adt/systems/*` and
`adt/agent-config/*` in its own account and Region instead of every
`adt`-named secret. Local mode is unchanged: it has no gateway and keeps the
in-process tools.

## Update 2026-10-29

The Step Functions provisioner this ADR describes `provision_system` starting is gone: the onboarding runtime no longer exports that tool, the app runs its own `provision_system` in local and cloud mode, and the state machine had no executions. The state machine, its Lambda, image and roles, `ONBOARDING_PROVISIONER_SFN_ARN` and the old tool body were removed. `onboard_agent.provision_system` is now a named slot the app's tool replaces.
