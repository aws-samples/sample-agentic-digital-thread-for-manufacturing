<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0079: The Quick gateway accepts delegated (on-behalf-of) user tokens from an external OIDC identity provider

## Status

**Proposed — 2026-09-29.** Synth-pinned design. It has NOT been run against a
live identity provider or a live enterprise-assistant tenant. Two items are
live-verification gates (see Verification): the in-place update of the Quick
gateway's `AuthorizerConfiguration` (V1) and the exact REQUEST-interceptor
event shape (V2).

Related: [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (the
ONE identity path), [ADR-0071](0071-dedicated-gateway-for-quick-query-bridge.md)
(the bridge-only gateway), [ADR-0072](0072-external-oidc-identity-provider-option.md)
(vendor-neutral, sticky OIDC configuration),
[ADR-0070](0070-cognito-authorization-at-edge-default.md) (edge auth; its
2026-09-23 amendment changed `AllowedClients` on a live gateway in place).

## Context

Today the AWS Quick connector reaches ADT through the dedicated gateway
`adt-quick-gw` (`stacks/agentcore_stack.py::_build_quick_gateway`). Its
authorizer is `CUSTOM_JWT` against the discovery URL of the deployment's own
Cognito `adt-app` pool, with `allowed_clients = [adt-app-client,
adt-app-service]` and MCP `supported_versions=["2025-03-26"]`. The connector
signs in with the `adt-app-service` client-credentials client
(`docs/quick-web-connector.md`), so every question asked through the assistant
arrives under ONE shared service identity.

The gateway uses up the inbound JWT and calls its only target, `query-bridge`,
with `adt-m2m` client credentials (ADR-0049: `JWT_PASSTHROUGH` is rejected for
MCP-server targets). The bridge (`agents/query_bridge_server.py`) calls the app
over the VPC with the shared `x-quick-auth` secret. The app admits the request
as the fixed identity `quick-gateway` (`agents/auth/middleware.py`) and runs the
orchestrator as a service turn (`server.py::_run_ask_job`: no forwarded token,
`user_id` defaults to `async-user`). No user identity exists anywhere on that
chain.

An enterprise assistant tenant may require MCP connectors to authenticate each
USER through the enterprise's external OIDC identity provider (an
on-behalf-of flow) instead of a shared credential, so that actions carry user
identity. The assistant vendor's connector form for user authentication takes
a client id, a client secret, an authorize URL, a token URL and a redirect URL
fixed by the vendor. The operator registers the identity provider side (a
client profile, a resource-server profile whose resource identifier is the
gateway URL, and the trust between them) in the provider's console. That is
out of scope for this repository.

The design has to respect these constraints:

1. **One identity path.** Under ADR-0049, user identity reaches runtimes ONLY
   as the app-signed compact claim in the reserved tool-call argument
   `_adt_identity` (`agents/auth/identity_claim.py`; `mint_claim` carries
   `sub` + `groups` ONLY and lives at most 5 minutes). Every runtime pops the
   argument unconditionally (`agents/runtime_asgi.py::enforce_identity_claim`).
   A half-configured policy fails CLOSED, and raw token passthrough is
   forbidden.
2. **Delegated tokens may nest the client id.** Some providers put the client
   id under an actor claim (`act.client_id`) instead of the top-level client
   claim, so `allowed_clients` would reject every delegated token. The
   authorizer must use `allowed_audience` instead, and the audience must equal
   the gateway URL including `/mcp`.
3. **The audience is only known after the gateway exists.** `attr_gateway_url`
   is set when the gateway is created, and an authorizer property cannot
   reference its own resource. Enabling delegated tokens therefore takes two
   steps, and the authorizer change MUST update the existing gateway in place.
   A replacement would mint a new URL and invalidate the registered audience.
4. **Authorizer mutability (checked).** The CloudFormation resource reference
   for `AWS::BedrockAgentCore::Gateway` lists `AuthorizerConfiguration`,
   `AuthorizerType` and `InterceptorConfigurations` all as *Update requires:
   No interruption*. The comment above `_build_gateway` records the one
   service-side limit: the authorizer TYPE cannot change on an existing
   gateway ("Authorizer type cannot be updated for an existing gateway"). This
   design keeps the type `CUSTOM_JWT` and changes only the configuration
   contents (discovery URL, clients → audience). The ADR-0070 amendment made
   exactly that kind of change (it appended to `AllowedClients`) on a live
   gateway without replacement. So the constraint that applies to the SHARED
   gateway (type change ⇒ replacement) does not apply here. V1 still gates the
   first enabling deploy with `cdk diff`.
5. **The MCP protocol pin stays.** Some assistant tenants do not yet accept MCP
   protocol `2025-11-25`, so `supported_versions=["2025-03-26"]` must not move.
6. **The gateway offers one code hook.** aws-cdk-lib 2.257.0 exposes
   `CfnGateway.interceptor_configurations`
   (`GatewayInterceptorConfigurationProperty(interception_points,
   interceptor=InterceptorConfigurationProperty(lambda_=…), input_configuration=
   InterceptorInputConfigurationProperty(pass_request_headers))`). A gateway
   has at most one REQUEST interceptor, and it runs before the gateway calls
   the target. For MCP targets the AgentCore developer guide documents this
   input: `{"interceptorInputVersion":"1.0","mcp":{"gatewayRequest":{"path":"/mcp",
   "httpMethod":"POST","headers":{…,"Authorization":"<bearer>"},"body":{<parsed
   JSON-RPC>}}}}`. `headers` is present only with `passRequestHeaders=true`.
   The output is `{"interceptorOutputVersion":"1.0","mcp":{"transformedGatewayRequest":
   {"body":…}}}`. The code offers no simpler bounded alternative: the gateway
   drops the inbound JWT, `JWT_PASSTHROUGH` is unavailable for MCP-server
   targets, and the bridge only ever sees `adt-m2m`.
7. **What the code already provides.** The bridge runtime is served by
   `runtime_asgi.serve_mcp_app`, so it already sits behind
   `IdentityClaimASGIMiddleware`, which pops `_adt_identity` from every
   `tools/call`. `provenance_context_var` is the existing per-request pattern
   for exposing a popped reserved argument. `chat_client._compose_runtime_payload`
   is an additive seam for sending a reserved payload key to the runtime.
8. **IAM today.** The runtime role reads `secret:adt*`. The ECS app task role
   does NOT read the identity-claim key. deploy.sh creates the key imperatively
   BY NAME (`adt/identity-claim-key`), so CDK can only grant it by an ARN
   pattern.
9. **Vendor neutrality is enforced by a tripwire**
   (`tests/test_no_internal_identity_strings.py`, ADR-0072 D7). No
   company-internal identity provider, single-sign-on product, hostname or
   corporate tenant may be named.

## Decisions

### D1 — Configuration is a sticky, vendor-neutral pair: `quick_oidc_discovery_url` + `quick_oidc_audience`

`app.py` resolves the CDK context keys `quick_oidc_discovery_url` and
`quick_oidc_audience` (exactly ONE value: the Quick gateway URL including `/mcp`) the
same way as the ADR-0072 trio. The order is: an explicit `-c` value, then the
sticky SSM parameters `/adt/quick-oidc-discovery-url` and
`/adt/quick-oidc-audience`, then absent. `deploy.sh --quick-oidc-discovery <url>
--quick-oidc-audience <gateway-url>/mcp` writes the SSM parameters. The pure resolver
`stacks/_quick_gateway_auth.py::resolve_quick_oidc(discovery_url, audience, *,
quick_api_enabled)` validates:

- both or neither — a partial pair raises `ValueError` and fails LOUD at synth;
- the discovery URL is `https://` and ends with `/.well-known/openid-configuration`;
- the audience is a single `https://` URL ending in `/mcp`; a list (comma or
  whitespace separated) is REJECTED, because every extra audience is another
  resource whose tokens this gateway would accept; deploy.sh
  additionally requires exact equality with the `QuickGatewayUrl` output;
- setting the pair while `quick_api_enabled` is off raises `ValueError` and
  names both fixes ("pass `--quick-api`, or delete the two SSM parameters").
  `quick_api_enabled` is NOT sticky, so without this check a routine flag-less
  deploy would delete the gateway an enterprise assistant depends on.

**Why sticky:** a flag-less redeploy must never switch the authorizer back to
Cognito without the operator knowing; every enterprise-assistant call would
then get a 403. To switch it off, delete the two SSM parameters and redeploy.

**Why not reuse the ADR-0072 `oidc_*` trio:** that trio configures browser
sign-in on the hosted UI and needs a client secret. This pair configures a
gateway authorizer and needs no secret.

deploy.sh ENFORCES the two-step enable. When a `--quick-oidc-*` flag or a
sticky value is present, it reads the `QuickGatewayUrl` output before
`cdk deploy`. If the output is absent, it exits 1 ("deploy `--quick-api`
first, register the gateway URL at the identity provider, then enable
delegated tokens"). If the output is present but not in the audience list, it
also exits 1. Both checks run before anything is deployed.

### D2 — The delegated authorizer OMITS `allowed_clients` and sets `allowed_audience`

With `QuickOidcConfig` set, `quick_gateway_authorizer(stack, cfg)` builds the
`CustomJWTAuthorizerConfigurationProperty` with `discovery_url` and
`allowed_audience`, and **never sets `allowed_clients`**. The Cognito shape
never sets `allowed_audience`. Both rules are tested. `allowed_scopes` and
`custom_claims` are not used in v1: nobody has verified whether a nested
`act.client_id` can be addressed, so pinning the actor client is a documented
follow-up. The interceptor's telemetry records only whether an actor client is
present. Both modes keep the same `protocol_type`, `search_type`,
`supported_versions`, role, construct id `AdtQuickGateway` and name
`adt-quick-gw`.

### D3 — Topology: switch the existing `adt-quick-gw` authorizer in place; no second gateway

A `CUSTOM_JWT` authorizer has exactly one discovery URL, so one gateway cannot
accept both token families. The decision is to SWITCH `adt-quick-gw`:

- the gateway serves only the bridge (ADR-0071), so a change affects only this
  one connector. The kiosk path (`QuickApiGatewayStack`, SigV4) and the shared
  `adt-query-gw` are untouched;
- constraint 3 makes an in-place update the only viable mechanism anyway, and
  constraint 4 shows it is an update;
- no identified deployment needs the service-auth connector AND an enterprise
  assistant on the same gateway at the same time. If that need appears, a
  second gateway ("dual door") can start from ADR-0071's `gateway_id`/
  `gateway_url` override in `agent_manager.ensure_query_bridge_target`. It is
  not built now.

With the pair set, the Cognito service-auth connector stops working on THIS
gateway. The operator reconfigures the connector for user authentication in
the assistant vendor's connector form.

### D4 — A gateway REQUEST interceptor is the SOLE writer of `_adt_identity` on the Quick gateway

With the pair set, `ADTAgentCoreStack` attaches ONE REQUEST interceptor Lambda
to `adt-quick-gw` with `pass_request_headers=True`. Without the pair there is
no interceptor and no Lambda. All construction lives in the ONE home
`stacks/_quick_gateway_auth.py`, enforced by a tripwire: `allowed_audience=`
and `interceptor_configurations=` appear in no other stack file.

The handler (`lambda-quick-interceptor/index.py`, stdlib only; the directory
follows the `lambda-sitewise-feed/` precedent) works as follows:

- Every JSON-RPC message other than `tools/call` passes through unchanged.
- For `tools/call`, `extract_delegated_subject(event)` looks for the subject
  in order: (1) a validated-claims block, if the event carries one (to be
  pinned from a sanitized live capture — V2); otherwise (2) the payload
  segment of the `Authorization` bearer in `mcp.gatewayRequest.headers`. It
  decodes WITHOUT verifying the signature, as
  `identity_claim.identity_from_access_token` does. The gateway's authorizer
  has already checked signature, issuer, expiry and audience before it invokes
  the interceptor; the interceptor only re-packages the subject.
- `rewrite_tool_call` then acts on the subject. With a subject, it sets
  `params.arguments._adt_identity = mint_claim(sub, [], secret)` and OVERWRITES
  any value the caller supplied. Without a subject, or when the signing key
  cannot be read, it pops the argument. The effect is that the interceptor is
  the only writer: no caller-supplied value survives. `groups` is always `[]`
  because a delegated user has no pool groups, so a `SYSTEM_REQUIRED_GROUP`
  runtime keeps denying. That is fail-closed by design.
- The signing key is read BY NAME
  (`identity_claim.IDENTITY_CLAIM_SECRET_NAME`) and cached in the process.
- The output contains ONLY the rewritten `body`. Headers are never echoed, so
  the inbound bearer is never forwarded toward the target. Headers, tokens,
  claims and subjects are never logged.
- Packaging keeps `cdk synth` PURE: `stacks/_staging.py::stage_tree` assembles
  the handler and a COPY of `agents/auth/identity_claim.py` (the ONE claim
  implementation — not re-implemented) under `cdk.out/adt-staging/`.
- IAM: the Lambda role gets `secretsmanager:GetSecretValue` on
  `secret:adt/identity-claim-key-*` only. `GatewayRole` gets
  `lambda:InvokeFunction` on the function ARN only.

### D5 — The bridge forwards the popped claim VERBATIM as `x-adt-identity`

`runtime_asgi._process_one_mcp_message` sets a per-request
`identity_claim_var` to the raw `_adt_identity` string before enforcement pops
it, and to `None` otherwise. It is reset on every `tools/call` and never
logged. `query_bridge_server._gateway_headers()` adds `x-adt-identity` when
the var is non-empty. The header name is defined ONCE, in
`agents/auth/delegated_identity.py`. The bridge holds no key, verifies
nothing, and parses nothing. Without a claim its headers are byte-identical
to today.

### D6 — The app is the ONE verifier: absent ⇒ anonymous, valid ⇒ delegated user, present-but-unverifiable ⇒ 401

`agents/auth/delegated_identity.py::resolve_delegated_identity(headers)`
verifies the header with `identity_claim.verify_claim`, the same function the
runtimes use. It reads the signing key BY NAME and caches it. In
`AuthMiddleware`, the change is confined to the branch that has already been
admitted through `x-quick-auth`:

- no header ⇒ `quick-gateway`, as today;
- a valid header ⇒ `UserInfo(user_id=sub, username=sub)`,
  `request.state.delegated = True`, with no admin or integrator role;
- a header that is present but malformed, badly signed or expired, or a
  signing key that cannot be read ⇒ `401 {"detail": "delegated identity could
  not be verified"}`. It never falls back to the shared identity.

With no header, the key is never read. The ECS task role gains
`GetSecretValue` on `secret:adt/identity-claim-key-*` ONLY when the pair is
configured, so the default quick-API template stays unchanged.

Amendment 2026-10-21: the app task also carries `IDENTITY_CLAIM_KEY_READABLE=1`
under exactly that condition (`ADTComputeStack._env_delegated_identity_key`),
and the app reads the key only when it is set. Without it a read could only be
refused: on a 2026-10-20 workshop deploy the onboarding agent test, the other
by-name reader, logged an AccessDenied in CloudTrail on every run. Unset, a
present header is still rejected with the 401 above and the agent test signs
no claim. Outside that mode the template gains no claim-key environment
variable or grant; only the app image asset changes, as with any app code
change.

### D7 — The delegated subject owns the job and travels to the orchestrator as a reserved payload key

- `ask_async(body, actor)`: when a delegated actor is present, `user_id =
  actor.sub` overrides any `user_id` in the body, because the body is
  untrusted. The job record gains `actor` and `actor_kind: "delegated"`.
  Without an actor, the behaviour is unchanged.
- `ask_result`: a job with an `actor` is returned ONLY to a poller whose
  verified subject is that actor. Any other poller gets the existing
  `{"status": "unknown"}` shape. A job without an `actor` is returned only to
  a poller with no delegated actor whose admitted principal is the job's
  recorded `submitter` (the shared Quick service principal is matched like any
  other), and gets the same `unknown` shape otherwise.
- `_run_ask_job` sends `extra_payload={"delegatedSubject": sub}` through
  `AgentCoreChatClient.stream`. The constant `DELEGATED_SUBJECT_FIELD` lives
  in `agents/auth/forwarding.py`.
- In the orchestrator runtime, `_runtime_entrypoint` pops `delegatedSubject`
  unconditionally, on both the integrity and the legacy path, and stores it in
  a per-invocation context variable. `_chart_injecting_build` passes
  `delegated_subject=` to `build_orchestrator` only when it is set, so the
  legacy invoke body stays unchanged. `build_orchestrator` passes it on to
  `_get_orchestrator_tools` and then to `_build_gateway_client`.
- `_build_gateway_client` keeps the bearer from `_resolve_gateway_bearer`.
  With no user token, that is the `adt-app-service` token; the delegated token
  is never forwarded, because that would be raw passthrough and the shared
  gateway would reject it. For a delegated turn,
  `_wrap_gateway_client_with_delegated_claim` mints a fresh
  `_adt_identity(sub, [])` per tool call. It shares the mint loop with the
  interactive wrapper through `_attach_identity_claim`. Provenance context
  applies too, and `rebuild_client` is `None`, so a rebuilt client can never
  drop the claim. With neither a token nor a subject, payloads stay
  byte-identical to today (existing pin).

### D8 — Invariant: the Quick gateway's MCP version stays `2025-03-26` in both modes

This is pinned by synth tests over both the Cognito and the delegated-token
templates. Bumping it requires re-testing every assistant tenant and amending
this ADR.

### D9 — Provenance records the delegated user; telemetry proves delivery, never obedience

The job record's `actor`/`actor_kind` and the orchestrator payload's `userId`
are the provenance stamps. Telemetry carries no values. The interceptor logs
`{"event":"quick_interceptor","identity":"minted|absent|stripped",
"actor_client_present":bool}`. The app logs `quick_delegated_identity
outcome=absent|verified|rejected`. No subject, claim, token or header value is
logged. Evidence that a runtime enforced the identity remains its deny texts
(ADR-0049).

### D10 — Rollback and coexistence

- **Rollback:** delete the two SSM parameters, redeploy with `--quick-api`,
  and switch the connector back to service authentication. The authorizer
  returns to the Cognito shape in place and the interceptor is removed. The
  app, bridge and orchestrator changes do nothing when no header arrives.
- **Coexistence:** each deployment has ONE inbound identity model for the
  Quick gateway. The kiosk path, the shared gateway, edge auth and the
  interactive orchestrator path are untouched.
- **Upgrade in place:** the enabling deploy keeps the same gateway id, URL and
  bridge target. No target re-sync is needed.

## Consequences

- Per-user identity on the enterprise-assistant path, without changing the
  shared gateway, the claim format, or the runtimes' enforcement rules. Every
  misconfiguration fails LOUD: audience mismatch, partial pair, missing
  `--quick-api`.
- The design rests on two live facts: V1 (in-place update) and V2 (the event
  shape).
- Each gateway call through the assistant adds one interceptor Lambda invoke,
  with the key cached. This applies only when the pair is set.
- Delegated users are plain users with no groups. A `SYSTEM_REQUIRED_GROUP`
  system denies them, as with ADR-0072.
- Identities are not unified across doors. A person who also signs in to the
  web UI through the same provider (ADR-0072) appears there as the
  Cognito-linked user. On the Quick path they appear as the provider's raw
  `sub`.
- On the delegated path a job result belongs to its owner. A poll that lost
  its identity sees `unknown`.
- The first enable is a two-step deploy with an operator round-trip through
  the provider console.
- The Lambda and the app read the key by NAME, while the runtimes use the ARN
  env. The name is spelled in ONE constant.

## Alternatives rejected

- **Dual door (a second gateway).** No current need, and it doubles the
  provisioning surface (D3).
- **Rename-and-replace the gateway.** It changes the URL and so invalidates
  the registered audience.
- **An unsigned identity header from the bridge to the app.** Anyone holding
  `QUICK_GATEWAY_TOKEN` could assert any subject.
- **An interceptor that forwards claims or headers to the target.** That
  would be a second identity channel next to `_adt_identity`, and header
  forwarding to MCP targets is not a verified capability.
- **Forward the delegated token as `userAccessToken`.** That is raw
  passthrough, and the shared gateway would return a 403.
- **Reuse the ADR-0072 trio.** It is a different concern and carries a secret
  with no meaning here.
- **`custom_claims` on `act.client_id` in v1.** Unverified nesting support
  (D2).
- **A richer claim payload** (email, issuer, actor). It violates ADR-0049's
  sub+groups minimality.

## Verification

**Offline (unit + synth):**

- `agentic_digital_thread/tests/unit/test_quick_oidc_context.py` — the D1
  resolver rules.
- `agentic_digital_thread/tests/unit/test_quick_interceptor_handler.py` —
  pass-through, mint, overwrite of a forged value, strip without a subject or
  key, envelope shape, no values in logs.
- `agentic_digital_thread/tests/unit/test_delegated_identity_middleware.py` —
  anonymous, verified, rejected (401), non-quick paths ignored, no key read
  without a header.
- `agentic_digital_thread/tests/unit/test_ask_async_delegated_actor.py` — actor
  override, job ownership, payload ride.
- `agentic_digital_thread/tests/unit/test_runtime_delegated_subject.py` — the
  reserved `delegatedSubject` key is popped on both runtime paths and passed
  to the build only when set.
- `agentic_digital_thread/tests/unit/test_deploy_sh_quick_oidc.py` — runs the
  deploy.sh pre-check in bash with stubs. Every misconfiguration exits 1
  before any SSM write; a valid pair persists and emits the CDK flags.
- Extended: `test_runtime_identity_hook.py`, `test_query_bridge_server.py`,
  `test_orchestrator_identity_attach.py`.
- `tests/test_quick_gateway_delegated_oidc.py` (synth) — authorizer shapes, the
  MCP pin in both modes, stable gateway identity, the interceptor only in
  delegated mode, the IAM scopes, the one-home tripwire, and the default
  quick-API template unchanged.
- `tests/test_docker_context_hygiene.py` — the interceptor asset is staged under
  `cdk.out`, never in the checkout.

**Live (deployed env with `--quick-api`), in order:**

- V1 — `cdk diff ADTAgentCoreStack` with the pair shows `AdtQuickGateway` as an
  UPDATE, never a replacement. If a replacement appears, STOP and amend this ADR.
- V2 — capture one interceptor event with values redacted (keys only). If it
  carries a validated-claims block, adjust lookup step (1).
- `get-gateway`: `discoveryUrl` is the provider's, `allowedAudience` is the
  gateway URL, there is no `allowedClients`, `supportedVersions` is
  `["2025-03-26"]`, and there is one REQUEST interceptor.
- Round trip through the assistant. The app logs `quick_delegated_identity
  outcome=verified`, and the job's `actor` is the token's `sub`.
- Negative: the old Cognito service token gets a 403, and a forged
  `_adt_identity` is overwritten.
- `smoke_prompts.py` / `audit_learning.py` stay green.

## Operator runbook (outside this repo; placeholders only)

1. Deploy with `--quick-api` and note `QuickGatewayUrl`
   (`https://<quick-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`).
2. In the external OIDC identity provider's console, register a
   resource-server profile whose resource identifier (audience) is EXACTLY
   that URL. Register a client profile for the enterprise assistant with the
   vendor's fixed redirect URL, and establish the trust that lets the client
   obtain delegated user tokens for that resource. Record the discovery URL,
   authorize URL, token URL, client id and client secret.
3. `./deploy.sh --profile <P> --region <R> --quick-api --quick-oidc-discovery
   https://<issuer>/.well-known/openid-configuration --quick-oidc-audience
   <QuickGatewayUrl>`
4. In the assistant vendor's connector form, set the endpoint to
   `QuickGatewayUrl` and choose user authentication with the values from
   step 2.
5. Verify, then keep the rollback path from D10 at hand.

### Review iteration 1 (independent reviewer, 2026-09-29)

One HIGH finding: the audience was a comma-separated list, and deploy.sh only
checked that the gateway URL was a member. Every extra audience is another
resource whose tokens this gateway would then accept, which widens the trust
boundary the ADR claims to fix. Closed: `quick_oidc_audience` is exactly ONE
value (`parse_audience` rejects commas and whitespace-separated lists),
`quick_gateway_authorizer` emits `allowed_audience=[cfg.audience]`, and
deploy.sh requires exact equality with the `QuickGatewayUrl` output before it
writes anything. Tests: `test_audience_list_is_rejected` (3 shapes),
`test_single_audience_is_emitted_as_a_one_element_list`,
`test_audience_is_trimmed_and_a_list_is_refused`,
`test_audience_list_is_refused_even_when_it_contains_the_gateway_url` (deploy.sh).


## Note 2026-11-02: `--quick-connector` also satisfies the gateway check

Wherever this ADR says the pair needs `--quick-api` (`quick_api_enabled`),
`--quick-connector` (`quick_connector_enabled`,
[ADR-0093](0093-quick-connector-in-the-workshop-package.md)) now works too,
because either flag builds `adt-quick-gw`. app.py passes
`quick_api_enabled or quick_connector_enabled` to `resolve_quick_oidc`, and
deploy.sh accepts either flag in its two-step checks. Rollback redeploys with
the same Quick flag the gateway was deployed with. The live gates V1 and V2
are unchanged and still pending.

## Amendment 2026-11-07: the Cognito mode has Quick's own client

The Context above describes the Cognito mode before this date. Since the
[ADR-0071 amendment of 2026-11-07](0071-dedicated-gateway-for-quick-query-bridge.md),
the Cognito-mode authorizer of `adt-quick-gw` lists three clients:
`adt-app-client`, the app's service client `adt-app-service-v2` (the
original `adt-app-service` is retired and listed nowhere) and Quick's own
`adt-quick-service`
(`client_credentials` only, scope `adt-quick/bridge`, output
`QuickClientId`), and the connector signs in with `adt-quick-service`, not
`adt-app-service`. No other gateway lists that client, so the connector's
secret reaches only the bridge tools. Every question asked through the
assistant still arrives under ONE shared service identity, so the problem
this ADR solves is unchanged. The delegated-token mode is unchanged: it lists
no Cognito client, so `adt-quick-service` tokens are refused there, and
`stacks/_quick_gateway_auth.py` still builds the client in that mode.
