<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0071: Dedicated AgentCore gateway for the Quick query-bridge

## Status

Accepted — 2026-09-22. Related: [ADR-0030](0030-demo-data-is-opt-in-base-default.md)
(infrastructure opt-in flags such as `quick_api_enabled` default off) and
[ADR-0070](0070-cognito-authorization-at-edge-default.md) (the `adt-app`
Cognito pool owned by `ADTAgentCoreStack` is the single issuer).

## Context

AWS Quick reaches ADT through MCP. The `query-bridge` target (a stateless
tool server exposing `submit_query`, `get_query_result`, `get_prompts`) was
registered on the shared query gateway `adt-query-gw`, alongside every
onboarded system's tools, the knowledge-graph tools and the workflow-guidance
tool. That gateway is the one the orchestrator itself federates from.

The Quick MCP connector discovers and fans out over every tool a gateway
exposes. With the bridge on the shared gateway, Quick saw dozens of tools and
reasoned over the raw system tools itself instead of calling the bridge, so
the "give me the orchestrator's answer" path silently degraded into a weaker
copy of the "Quick as analyst" path. Pointing Quick at a gateway that holds
only the bridge target was verified to restore the intended behaviour: Quick
has exactly three tools and calls them.

Separately, the connector runbook (`docs/quick-web-connector.md`) was ported
from a branch that predated the identity cutover. It described the gateway's
inbound authorization as AWS_IAM (SigV4) and told operators to fall back to the
kiosk's local MCP server when a Quick connector could not sign requests. On
`main` the query gateway has been CUSTOM_JWT against the Cognito `adt-app`
user pool since the cutover; the `adt-app-service` confidential client
(`client_credentials`, scope `adt/query`) is how non-interactive callers
authenticate, and `scripts/mint-query-token.sh` already mints such tokens.
The doc therefore sent operators down a path that could not work and hid the
one that could. The AWS Quick connector supports exactly that
service-to-service OAuth shape (client id, secret, token URL, scopes).

The kiosk path is different: Quick Desktop's local stdio connector runs
under an EC2 instance role that cannot perform an OAuth exchange, so it uses
`QuickApiGatewayStack` (REST API, SigV4 via the instance role). That path is
unaffected by this decision.

## Decision

1. **A second, bridge-only gateway.** When `quick_api_enabled` is on,
   `ADTAgentCoreStack` builds `adt-quick-gw` (construct id `AdtQuickGateway`)
   next to the shared `adt-query-gw`. It is CUSTOM_JWT against the same
   `adt-app` discovery URL with the same two allowed clients (`adt-app-client`,
   `adt-app-service`), the same gateway role, MCP protocol and semantic
   search. Its id and URL are exposed as `QuickGatewayId` / `QuickGatewayUrl`
   outputs and as the `QUICK_GATEWAY_ID` / `QUICK_GATEWAY_URL` task env of the
   app. Base deploys synth exactly one gateway and are byte-unaffected.
2. **The shared gateway is not touched.** The `AdtQueryGateway` construct is
   left as is: an AgentCore gateway's authorizer type cannot be updated in
   place, so any change to that construct forces a replacement and a target
   re-sync. Adding a sibling gateway is purely additive.
3. **The bridge target provisions on the Quick gateway.**
   `AgentManager.ensure_query_bridge_target` reads `QUICK_GATEWAY_ID` /
   `QUICK_GATEWAY_URL` and passes them as an explicit `gateway_id` /
   `gateway_url` override through `start_agent`, `_start_cloud` and the
   gateway-target upsert, reuse and sync helpers, which previously addressed
   `self.gateway_id` unconditionally. Onboarded systems and the
   `digital-thread` target keep the default (shared) gateway. The instance
   records which gateway its target lives on so reset/delete addresses the
   right one.
4. **Refuse, don't fall back.** If `QUICK_GATEWAY_ID` is unset while the
   Quick integration is enabled, `ensure_query_bridge_target` raises
   `QuickGatewayNotConfiguredError` and makes no control-plane call. The
   compute stack emits `QUICK_GATEWAY_ID` together with `QUICK_API_ENABLED`
   from the same flag, so on any deploy from one build the env is always
   present; a missing id means a stack/image mismatch. Provisioning the bridge
   on the shared gateway instead would quietly recreate the fan-out this ADR
   removes, behind a log line nobody reads. The startup provisioner already
   catches and logs the error, so the app comes up without a bridge rather
   than with a wrong one. Deletion (`_deletion_gateway_id`) does NOT refuse:
   with no `QUICK_GATEWAY_ID` it addresses the shared gateway, because that
   is where a pre-dedicated deployment put the target and cleanup must reach it.
5. **Sweep the stale target.** An account deployed with `--quick-api` before
   this change has a `query-bridge` target on the shared gateway. Left there
   it re-exposes `submit_query` / `get_query_result` to the analyst-path
   connector alongside the raw tools. After the dedicated target is up,
   `_remove_stale_bridge_target_from_shared_gateway` lists the shared gateway
   and deletes any `query-bridge` target it finds (and nothing else). It runs
   after a successful provision so a failed one leaves the environment
   untouched, and its own failures are logged, never raised.
6. **Quick uses the existing service client.** The Quick connector is
   configured with "Service authentication": the Cognito `adt-app` hosted-UI
   token URL, the `adt-app-service` client id and secret, scope `adt/query`.
   No Cognito client is added for Quick.
7. **The docs tell the truth and route the reader.** `quick-web-connector.md`
   documents the CUSTOM_JWT gateway, the service-to-service connector fields
   and why the gateway is dedicated; the kiosk fallback advice is removed.
   `quick-integration.md` is the single decision table (analyst / consumer /
   kiosk) and every Quick doc links to it from the top. The generated
   `amazon-quick-skill.md` gets its link line through its template in
   `agents/workflow_guidance.py`, never by hand.

## Consequences

- Quick web / Quick Suite users configure the connector against
  `QuickGatewayUrl` with OAuth service credentials and see exactly three
  tools. Pointing Quick at `GatewayUrl` (the shared gateway) is now a visible
  misconfiguration (dozens of tools discovered) rather than a subtle one.
- `quick_api_enabled` deploys carry one more AgentCore gateway. Turning the
  flag off removes it through CloudFormation; the bridge target must be
  deleted first (targets block gateway deletion), which
  `delete_cloud_agent("query-bridge")` now does against the right gateway.
- Existing `quick_api_enabled` deployments converge on redeploy: the new task
  env carries `QUICK_GATEWAY_ID`, the bridge is provisioned on `adt-quick-gw`,
  and the stale target on the shared gateway is swept on the same startup.
  Until redeployed they keep working as before (old image, old env, no
  refusal); the refusal only fires for a task that runs the new image with an
  env that lacks the id, which a deploy from one build cannot produce.
- Open follow-up: `QuickApiGatewayStack` (REST API Gateway, VPC Link, SigV4)
  still deploys under `--quick-api` although its only consumer is the kiosk's
  `kiosk_mcp_server.py`. It cannot yet be gated on `quick_kiosk_enabled`
  alone because it also mints `/adt/quick-gateway-token`, the `x-quick-auth`
  secret the bridge runtime uses against the app's async ask endpoints. Moving
  that minting into the compute or AgentCore stack, then gating the API
  Gateway on the kiosk flag, is separate work. The API Gateway is NOT
  load-bearing for the web-connector path.
- The `gateway_id` override is a general seam: any future target that must
  live on a gateway other than the shared one can use it without another
  round of hardwired-`self.gateway_id` refactoring.
- Doc-drift finding: a runbook ported from a pre-cutover branch carried the
  old authorizer model unchanged and was not caught because no test pins docs
  to infrastructure. The synth test now asserts the gateway count and
  authorizer type per flag; the auth model in the docs is cross-referenced to
  the stack code in the connector doc's Source section.

## Alternatives rejected

- **Keep one gateway and steer Quick by prompt.** Instructing the chat agent
  to only call the bridge tools does not stop the connector from discovering
  and offering every tool; the behaviour was observed to regress to raw-tool
  fan-out regardless. Isolation has to be at the gateway.
- **Cedar policy on the shared gateway restricting Quick to the bridge
  tools.** Tool action ids are only known after targets are registered
  (imperatively, after `cdk deploy`), so a declarative policy in the stack is
  not possible today, and a per-caller allowlist on the shared gateway would
  still show Quick the full tool list at discovery time.
- **Move the bridge to the kiosk REST API for everyone.** Would force every
  Quick caller onto SigV4 and an API Gateway hop that exists only because the
  kiosk's local connector cannot do OAuth. The general path should use the
  gateway and OAuth that the rest of the platform already standardises on.
- **A separate Cognito client for Quick.** Adds a credential to manage for no
  isolation gain; the gateway boundary, not the client, is what separates the
  tool sets. Cedar per-caller policy remains available later if needed.

## Amendment 2026-10-02: no semantic search on `adt-quick-gw`

Decision 1 gave the Quick gateway the shared gateway's semantic search. With
`SearchType: SEMANTIC`, AgentCore adds its built-in
`x_amz_bedrock_agentcore_search` tool to `tools/list`, so the gateway listed
four tools: the three bridge tools plus the search tool. Measured on two
accounts with an MCP client making the same calls as the Quick
connector. The workshop's module 1b and `docs/quick-web-connector.md` tell
the attendee to expect exactly three actions, and treat more than three as
the wrong endpoint.

The Quick gateway now omits `SearchType`. It holds one fixed three-tool
target, which is the onboarding gateway's case (small fixed toolset, no
search, `tests/test_onboarding_runtime_stack.py`). The shared `adt-query-gw`
keeps semantic search. ADR-0079's rule that both authorizer modes keep the
same protocol configuration still holds: neither mode sets it.

## Amendment 2026-10-06: one poll call covers about two app holds

**Observed (a fresh workshop deployment, module 7b run as a participant).** Quick submitted
the recall picture as one job, polled for about four minutes, then posted
"the query is still being processed ... I'll continue polling" and stopped;
"Check the result of that query now" returned the finished answer at once.
The bridge's `next_action` and the persona's keep-polling instruction were in
place; what ended the loop was Quick's own bound on tool calls per turn, about
twelve, which at one ~20 s app hold per call is about four minutes.

**Decision.** `get_query_result` re-polls the app inside one tool call while
another ~20 s hold still fits `POLL_CALL_BUDGET_SECONDS` (44 s), so one Quick
call covers about two holds and the same number of calls covers twice the
wall time (a 10-minute query in about fifteen calls instead of thirty). The
budget sits with margin under Quick's 60 s per-call limit and the AgentCore
Runtime's 60 s MCP tool interruption; the app's own 20 s hold is unchanged
(it also serves the kiosk path behind the 29 s API Gateway integration). The
tool description now says "about 40 seconds". A longer query than the call
budget covers still ends in a `running` reply with the same `next_action`.

**Correction 2026-10-07 (post-merge review).** The 44 s budget was only
checked before starting another hold; each GET still carried the client's
full 45 s timeout, so a first hold returning late followed by a slow second
one could run the call to about 67 s, past both 60 s limits. The call now
fixes one deadline at its start and caps every GET's timeout at what is left
of the budget less the 2 s serialisation allowance. Capping the timeout alone
would lose answers: the result route then freed a finished job as it
delivered it (no longer, see the 2026-10-16 correction),
and its hold (fourteen 1.5 s steps, about 21 s) can outlast a capped second
GET, so a job finishing near the end of that hold was delivered to a bridge
that had stopped listening. The route therefore takes an optional `wait`
(seconds, capped at its own 20 s, `<= 0` or NaN means no hold) and bounds
its hold's wall time by it: a step is taken only while its sleep plus a
record read as slow as the slowest so far still ends inside `wait`. The
bridge asks for its GET timeout less 3 s, so the app answers first. One
case remained: a single record read or delivery delete slower than that 3 s
margin, arriving after the last step, could still outlast the bridge's
timeout; delivery was pop-on-read, so that answer would be lost (it was, on
2026-10-16; see the correction below). Without
`wait` the route holds exactly as before (the kiosk path behind API Gateway
is unchanged). A later hold that still times out or
loses its connection returns the `running` reply already in hand; a failure
on the first hold still returns `error` as before. httpx applies a timeout
per phase (connect, read, write, pool), not to the whole request; the budget
keeps 16 s of margin under the 60 s limits for that.

**Correction 2026-10-16 (an answer lost on a deployed workshop).** That
remaining case happened. A Quick lot-total ask finished (the orchestrator
logged its completion at 22:02:01.9Z) while a `get_query_result` call that
ran from 22:01:23 to 22:02:07 (44.3 s) was holding; that call returned the
stale `running`, and the next one got `No such job`. Only the bridge polls a
job, so the app had built the `done` reply for that call's last GET, popping
the record as it did, and the reply never reached the bridge. Delivery is
now repeatable: the result route never frees a finished
record, so every poll of a finished job (a `done`, or an `error` whether
abandoned or not) answers the same terminal reply until the record expires,
an hour after its last write (the `ask` kind's 1 h TTL). The store enforces
that on read, because DynamoDB deletes an expired item lazily (it can be
hours later) and serves it to every read until then: `JobStore.get`,
`get_many` and `get_strict` treat an item whose `ttl` is at or before now as
absent and never cache it, so an expired record answers `unknown` to every
poller, owner included. Leases keep reading by their own `expires_at` (the
attribute their conditional write tests) and `mutate` keeps reading the item
its revision condition is checked against. In memory, a finished copy
(`done`, or a non-abandoned `error`: the records delivery used to free)
stops answering at the same horizon, its table item's own `ttl` when it was
read from the table, and the store keeps at most the newest 256 of them
(`JobStore(memory_cap=...)`); running records and abandoned tombstones are
never dropped from memory. A store backed by the table drops only its
process's copy, never the item. Owner visibility (ADR-0079) is checked on
every poll, so a re-delivery reaches only the job's owner. The `wait` hold
and the bridge's 3 s margin are unchanged: they still keep the reply inside
the GET that asked for it, so the call does not end on a stale `running`; a
reply that misses its GET now only costs one more call.

## Amendment 2026-10-19: a `running` reply names the next call

**Observed (Team 2 and Team 3 walks).** Quick twice wrote "I'm continuing to
wait for the results ... Hang tight!" to the user and stopped calling
`get_query_result` until the user replied, although the persona says never
to stop. The `running` reply's `next_action` said to call again, but not to
call now, in the same turn, nor that a message to the user is what ends the
turn and the polling. The next call was given only as prose.

**Decision.** A `running` reply keeps every key it carried (`status`,
`job_id`, `elapsed_seconds`, `expected_total_seconds_up_to`, `next_action`)
and adds the next call as data: `next_tool` (`get_query_result`),
`next_arguments` (`{"job_id": ...}`, only when the id has the shape the app
mints) and `retry_after_seconds` 0, the field the busy reply already uses.
`next_action` now says the reply has no answer yet, to call again now with
the same job_id in the same turn before writing anything to the user, and
that a progress message ends the turn so the user never gets the answer.
The `get_query_result` and `submit_query` descriptions say the same. `done`,
`error` and `unknown` replies are unchanged and name no next call. Nothing
here can force a client to keep calling; it removes the ambiguity the
client acted on.

## Amendment 2026-10-26: an abandoned ask is stopped

**Observed.** A job still running at the 20-minute ceiling was reported as an
error, but its run went on: the chat client minted the AgentCore runtime
session id inside the stream and nothing kept it, so the run could not be
stopped, and the worker stayed blocked on the stream while the orchestrator
kept calling the model and the systems. With two such workers alive every new
submit was refused.

**Decision.** The submit mints the runtime session id, keeps it on the job
record (never in a poll's reply) and invokes the runtime with it. The worker's
own process also keeps it, with the run's start, for as long as the worker
lives. When the job is abandoned at the ceiling, by a poll or, when nobody
polls, by a ceiling check the lease renewer starts on its own thread (so a slow
store read never delays a lease), the app calls `StopRuntimeSession` for that
session in the background and, once the runtime answers stopped or not found,
closes the worker's stream so the worker ends and its lease is released. The
ceiling check also stops a local worker whose job another replica abandoned,
or whose record has expired. A failed stop, or a stream close that raised, is
retried on every pass while the worker lives, using the id the process kept;
until then the worker keeps its lease and still counts toward the abandoned
bound. The 600 s takeover never stops a run. Records written before this change
carry no session id and are abandoned as before. The app role's existing
`bedrock-agentcore:*` grant covers the call.

Every write that keeps a given-up worker counted is checked. If recording it
as detached does not land, that is logged and the ceiling check puts the entry
back while the worker lives. A lease renewal whose write does not land is
logged and retried after 10 s instead of a full interval. A stop that answers
not found closes our side only if the runtime had already answered. If it is
answered before the invoke was sent, the cancel handle refuses the invoke, so
no run starts. If the invoke is in flight, the worker keeps reading and the
next check stops the session. The handle refuses an invoke for every caller,
so a web chat client that leaves before its turn is invoked no longer starts a
run either. A losing expiry undoes its detached entry only when the record is
the worker's own terminal write (`done`, or an `error` that is not abandoned);
a stale, failed or malformed read keeps it.

Live check on a rehearsal account (three stopped runs, one unstopped control
and one run where only our side of the stream was closed, all the same
question): `StopRuntimeSession` answered 200 and a second stop 5 s later
answered not found. The stopped orchestrator sessions never logged their
completion and logged nothing more about a minute after the stop; tool calls
the orchestrator had already sent to the system agents finish on their own.
The control completed. Closing our side alone does NOT end the run: that
session went on to log its completion 2.5 minutes after the close. Without our
side closing it, a stopped session's response stream stayed open for about two
minutes after the stop, which is why the app also closes it.

## Note 2026-11-02: a second flag builds `adt-quick-gw`

Decision 1 still holds, with one more trigger: app.py builds `adt-quick-gw`
when `quick_api_enabled` or `quick_connector_enabled` is on
([ADR-0093](0093-quick-connector-in-the-workshop-package.md); `deploy.sh
--quick-connector` or the Workshop Studio packager). On the
Consequences line about turning the flag off: because the app provisions the
`query-bridge` target outside CloudFormation, `deploy.sh` now stops before CDK
when neither Quick flag is given and the gateway still lists a target, and
`cleanup.sh` deletes that gateway's targets before removing the stacks.

## Amendment 2026-11-07: Quick has its own Cognito client (supersedes decision 6)

Decision 6 had Quick's connector sign in with the app's own `adt-app-service`
client. `adt-query-gw` and `adt-onboard-gw` accept that client too, so anyone
holding the connector secret could mint a token and call every system target
directly, bypassing the orchestrator and the bridge. On the workshop
connector path that is every attendee, who reads the secret
`adt-quick-client-secret` in module 1b.

Quick now has its own confidential client, `adt-quick-service`
(`client_credentials` only, no user sign-in flow, its own resource server
`adt-quick` with the one scope `adt-quick/bridge`), built with `adt-quick-gw`
in `stacks/_quick_gateway_auth.py`. Only `adt-quick-gw` lists it; the
`ADTAgentCoreStack` output `QuickClientId` names it, and the connector secret
copies its secret. `adt-quick-gw` also accepts `adt-app-client` and the
app's service client, so the app's background turns work there too.
`scripts/mint-query-token.sh` mints the app's token for `adt-query-gw` by
default and Quick's own token for `adt-quick-gw` with `--quick`. Row (a) of
[quick-integration.md](../quick-integration.md), Quick as an analyst on
`adt-query-gw` (`scripts/quick-integration.sh`), still uses the app's service
client by design and is for an operator's own Quick. Pinned by
`tests/test_quick_stacks.py`.

**The disclosed client is revoked in the same update.** On a deploy that
already handed out the `adt-app-service` secret, a new Quick secret alone
would not help: the old value stays valid on `adt-query-gw` and
`adt-onboard-gw`, and Secrets Manager keeps it as the `AWSPREVIOUS` version of
`adt-quick-client-secret`. Replacing that client outright is not possible in
one update: its id reaches `ADTComputeStack` (the app task env) and
`ADTOnboardingStack` (the onboarding gateway) through a CloudFormation
export, and CloudFormation refuses to change or remove an export another
stack imports. So the release:

1. adds a new service client, `adt-app-service-v2` (construct
   `AdtAppServiceClientV2`, the same `client_credentials` settings and
   `adt/query` scope), and makes it the one every consumer uses: the
   allowed clients of `adt-query-gw`, `adt-onboard-gw` and `adt-quick-gw`,
   the app task env `ADT_APP_SERVICE_CLIENT_ID` (through a NEW export), and
   the `AppServiceClientId` output that `deploy.sh` and the workshop
   bootstrap pass to the orchestrator runtime;
2. keeps the original client (construct `AdtAppServiceClient`) unchanged and
   keeps its export with `export_value`, so no export is modified or
   removed, but lists it on no gateway. Once every stack has updated, a
   token minted with the disclosed secret is refused by every gateway.

The update is not atomic across stacks, so the old client is refused in
stages:

- after `ADTAgentCoreStack` updates (new client, new export, gateways
  switched), `adt-query-gw` and `adt-quick-gw` refuse it;
- `adt-onboard-gw` keeps accepting it until `ADTOnboardingStack` updates
  and moves its import to the new export;
- after `ADTComputeStack` and the orchestrator runtime env update, every
  service caller uses the new client again.

Between the first step and the last, background calls still holding a token
from the old client get 401 from the gateways that have switched. A
standalone deploy that stops part way must be resumed or rolled back: it is
not evidence that the retirement completed. Pinned by
`tests/test_app_service_client_v2.py`.

Later cleanup (not built): once no deployed stack imports the old export,
delete the old client and the `export_value` line in one release.
