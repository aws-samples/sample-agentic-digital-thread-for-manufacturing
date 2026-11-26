<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# AWS Quick web connector: the `query-bridge` gateway target

> Choosing between the three Quick integration paths? Start with
> [quick-integration.md](quick-integration.md).

The `query-bridge` target exposes the ADT async query experience to Amazon
Quick (web connector / Quick Suite) through a **dedicated Amazon Bedrock AgentCore Gateway**
(`adt-quick-gw`) — no Amazon API Gateway involved. This doc covers what it is and the
hands-on step of registering the connector + chat agent in the Quick console.

Deployed only when `quick_api_enabled` is on (deploy.sh `--quick-api`), or on
the connector-only path below; base deploys are unchanged.

### Connector-only path (`quick_connector_enabled`, deploy.sh `--quick-connector`)

`-c quick_connector_enabled=true` (what `deploy.sh --quick-connector`
passes; not sticky, so pass the flag on every
deploy that should keep it) builds everything on this page except `QuickApiGatewayStack` ([ADR-0093](adr/0093-quick-connector-in-the-workshop-package.md)): the
gateway, the bridge env, a per-account `x-quick-auth` token in AWS Secrets
Manager, ingress from the AgentCore runtime security group to the internal
ALB, and two extra `ADTAgentCoreStack` outputs. Every connector value is then
an output or one secret read:

| Field | Output / secret |
|---|---|
| MCP endpoint | `QuickGatewayUrl` |
| Token URL | `QuickTokenUrl` |
| Client id | `QuickClientId` (Quick's own client `adt-quick-service`) |
| Client secret | `aws secretsmanager get-secret-value --secret-id adt-quick-client-secret --query SecretString --output text` |

`deploy.sh --quick-connector`
prints them (and the secret-read command, never the value) at the end of the
deploy, after waiting for a READY `query-bridge` target. A later deploy that
omits the flag stops before CDK while `adt-quick-gw` still has targets; to
remove Quick on purpose, follow [Rollback](#rollback-fully-additive--clean-removal)
(the target and the `adt_query_bridge` runtime), then deploy without the
flag. With delegated user tokens configured, the deploy prints only the MCP
URL and a note, not the Cognito service values. The account needs an Amazon
Quick Enterprise subscription, and the person creating the connector a Quick
user with the Author Pro or Admin Pro role, before the
connector can be created. `deploy.sh` never creates either: subscribe the
account in the Quick console first.

## What gets provisioned

- **Gateway:** `adt-quick-gw` (`ADTAgentCoreStack`, construct `AdtQuickGateway`,
  outputs `QuickGatewayId` / `QuickGatewayUrl`). Inbound auth is
  **CUSTOM_JWT** against the Amazon Cognito `adt-app` user pool, the same issuer
  as the shared `adt-query-gw`, and it alone also accepts Quick's own client
  `adt-quick-service` (output `QuickClientId`). Its ID/URL reach the app as the
  `QUICK_GATEWAY_ID` / `QUICK_GATEWAY_URL` task env.
- **Runtime:** `adt_query_bridge` (AgentCore, VPC, MCP protocol), running the
  standard agent image with `SYSTEM_ID=query-bridge`. Env carries
  `ADT_APP_URL` (the internal ALB URL) and `QUICK_GATEWAY_TOKEN_REF`: the ARN
  of the AWS Secrets Manager secret that holds the `x-quick-auth` shared
  secret the app's middleware validates. The bridge reads the value on first
  use with the AgentCore runtime role, which may read that one ARN, so the
  secret itself is not in the runtime env and `GetAgentRuntime` does not
  reveal it. The connector path and the `--quick-api` path use the same
  generated secret (`ADTComputeStack.quick_gateway_token_secret`); on
  `--quick-api` the API Gateway integration injects it through a
  CloudFormation dynamic reference, so no template holds the value. A bridge provisioned before the
  reference existed still carries the plaintext `QUICK_GATEWAY_TOKEN`; the app
  reprovisions it with the reference at its next start (see
  [Moving an existing bridge to the reference](#moving-an-existing-bridge-to-the-reference)).
- **Gateway target:** `query-bridge` on `adt-quick-gw` — the ONLY target on
  that gateway. The shared gateway and its targets are untouched.
- **Tools exposed** (gateway-prefixed as `query-bridge___<tool>`):
  `submit_query`, `get_query_result`, `get_prompts`.
- **Flow:** Quick -> `adt-quick-gw` (bearer JWT) -> query-bridge runtime ->
  HTTP over the VPC to the ADT app `/api/ask/async` +
  `/api/ask/result/{job_id}` (async submit/poll, beats the Quick 60s per-call
  limit) -> the app runs the orchestrator. `get_prompts` reads the app's
  `GET /api/prompts` catalog.

Under service authentication the connector serves one user per deployment.
Every call that carries the shared `x-quick-auth` secret (the connector and,
if deployed, the `--quick-api` REST API) reaches the app as the same identity, and the app
runs one async query at a time. When `submit_query` answers `busy`, it names
the running job only if that identity submitted it, so the job may belong to
anyone using the shared secret. A job a signed-in web user started is never
named to the connector, and the reverse. Give each user their own deployment,
or use delegated user tokens (below). With delegated
tokens a busy reply names a job only to the user who submitted it; anyone
else is told to wait and submit once more.

### Why a dedicated gateway

The Quick MCP connector fans out over **every** tool the gateway exposes. On the
shared `adt-query-gw` that is every onboarded system's tools, the graph tools
and the bridge tools; Quick then reasons over the raw tools itself instead of
calling `submit_query` / `get_query_result`, and the answer is not the
orchestrator's. A gateway that holds only the bridge target leaves Quick
exactly three tools, so it calls the bridge. The app provisions the target on
`adt-quick-gw` from `QUICK_GATEWAY_ID` in the task env; if that env is absent
while the integration is enabled it refuses to provision (the app logs
`QuickGatewayNotConfiguredError` and comes up without a bridge) rather than
putting the bridge on the shared gateway. On the same startup it removes a
`query-bridge` target that an older deployment left on the shared gateway.

## Gateway connection values for Quick

| Field | Value |
|---|---|
| MCP endpoint | `QuickGatewayUrl` output — `https://<quick-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp` |
| Inbound auth | **CUSTOM_JWT** (bearer token) issued by the Cognito `adt-app` user pool — NOT SigV4. |
| Quick auth type | **Service authentication (service-to-service)** — OAuth 2.0 `client_credentials`. |
| Token URL | `https://<adt-app domain>.auth.<region>.amazoncognito.com/oauth2/token` |
| Client id | `adt-quick-service`, the `QuickClientId` output of `ADTAgentCoreStack` |
| Client secret | the `adt-quick-service` app-client secret (Cognito console → user pool `adt-app` → App clients → `adt-quick-service` → Show client secret; on the connector-only path also the Secrets Manager secret `adt-quick-client-secret`) |
| Scope | `adt-quick/bridge` (Cognito grants it when the field is left empty) |
| Region | your deployment Region |

The gateway's authorizer is verifiable with
`aws bedrock-agentcore-control get-gateway --gateway-identifier <quick-gateway-id>`
→ `authorizerType: CUSTOM_JWT`. Quick has its OWN confidential client,
`adt-quick-service` (client credentials only, its own `adt-quick/bridge`
scope), and only `adt-quick-gw` accepts it: `adt-query-gw` (every system
target) and `adt-onboard-gw` refuse its tokens. So whoever holds the Quick
connector secret can call the three bridge tools and nothing else. The app's
own `adt-app-service-v2` client (the orchestrator's background turns) stays
accepted on `adt-quick-gw` too, for operator checks; never give its secret to
Quick. The original `adt-app-service` client is retired: no gateway accepts
it, so a connector set up with it before 2026-11-07 must be reconfigured with
the values above.

Sanity-check the credentials before touching the Quick console:

```bash
# mints a client_credentials token for Quick's own client (adt-quick-service,
# scope adt-quick/bridge) from the deployed pool, prints the adt-quick-gw MCP
# URL on stderr and an "Authorization: Bearer ..." header, the same kind of
# token Quick's connector will present (--json for the raw response)
AWS_PROFILE=<your-profile> ./scripts/mint-query-token.sh --quick
```

A token that mints here but is rejected by the gateway means the wrong
gateway URL (shared vs Quick) or a stale `QuickGatewayUrl`. Without `--quick`
the script mints the app's own `adt-app-service-v2` token for `adt-query-gw`,
which is not what Quick holds.

## Quick console steps

1. Connectors → Create → MCP.
   - Name: `Agentic Digital Thread`
   - Endpoint: the `QuickGatewayUrl` output (the `adt-quick-gw` MCP URL above)
   - Authentication: **Service authentication (service-to-service)**
     - Client ID: `QuickClientId` output
     - Client secret: the `adt-quick-service` secret
     - Token URL: the Cognito `oauth2/token` URL above
     - Scope (if asked): `adt-quick/bridge`
   - Create → Quick discovers exactly `submit_query`, `get_query_result`,
     `get_prompts`. If it discovers dozens of tools you pointed it at the
     shared `adt-query-gw` — use `QuickGatewayUrl`.
2. Chat agent → Create (e.g. `ADT Manufacturing Intelligence`):
   - On greeting/hello: call `get_prompts` and present the persona query list.
   - For a question: call `submit_query`, then poll `get_query_result` every
     15-30s until status is `done`; show the `answer`.
   - Enable the connector. Share with the account.
3. Test: "hello" → persona list; a cross-system query → submit/poll → answer,
   no timeout.

## External OIDC identity provider (delegated user tokens)

Everything above describes the default **Cognito service authentication**
mode: one shared service identity for every user. An enterprise assistant
tenant may instead require each USER to authenticate through the enterprise's
external OIDC identity provider (an on-behalf-of flow), so that actions carry
user identity. In that mode `adt-quick-gw` accepts **delegated
(on-behalf-of) user tokens** from that provider *instead of* the Cognito
tokens ([ADR-0079](adr/0079-quick-gateway-external-oidc-delegated-user-tokens.md)).
One mode applies per deployment. The service-auth table above then no longer
applies to this gateway.

What changes when it is enabled:

- The authorizer stays `CUSTOM_JWT`, but its discovery URL is the provider's,
  and it validates `allowedAudience` = the gateway URL (including `/mcp`).
  `allowedClients` is deliberately omitted, because delegated tokens may carry
  the client id only under an actor claim.
- A REQUEST interceptor AWS Lambda function mints the ADR-0049 app-signed identity claim
  (`_adt_identity`, subject only, no groups) for every tool call. The bridge
  forwards it to the app, and the app verifies it. The app then runs the query
  as that user and returns the result only to the same user. A claim that is
  present but cannot be verified gets a 401. It never falls back to the shared
  identity.
- The MCP protocol version stays `2025-03-26`. The gateway id and URL do not
  change (an in-place update).

### Enable (two steps — the audience IS the gateway URL)

1. Deploy with `--quick-api` or `--quick-connector` and note the `QuickGatewayUrl` output.
2. In the identity provider's console (an operator step, outside this repo),
   register a resource-server profile whose resource identifier is EXACTLY
   `QuickGatewayUrl`. Register a client profile for the enterprise assistant
   with the vendor's fixed redirect URL, and set up the trust that lets it
   obtain delegated user tokens for that resource.
3. Redeploy with both flags. They are sticky afterwards (SSM
   `/adt/quick-oidc-discovery-url`, `/adt/quick-oidc-audience`):

   ```bash
   ./deploy.sh --profile <P> --region <R> --quick-api \
     --quick-oidc-discovery https://<issuer>/.well-known/openid-configuration \
     --quick-oidc-audience <QuickGatewayUrl>
   ```

   deploy.sh stops BEFORE deploying anything in any of these cases: only one
   of the two flags is given, neither `--quick-api` nor `--quick-connector` is given, the Quick gateway does
   not exist yet, or the audience is not `QuickGatewayUrl`. `cdk synth` fails
   loud on a partial pair or a pair with neither `quick_api_enabled` nor `quick_connector_enabled`.

### Values for the assistant vendor's connector form

| Field | Value |
|---|---|
| MCP endpoint | `QuickGatewayUrl` — `https://<quick-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp` |
| Authentication type | **User authentication** (OAuth 2.0 authorization code, on behalf of the signed-in user) |
| Client id | `<client-id>` — the client profile registered at the identity provider |
| Client secret | `<client-secret>` — that client profile's secret |
| Authorize URL | `https://<issuer>/<authorize-path>` — from the provider's discovery document (`authorization_endpoint`) |
| Token URL | `https://<issuer>/<token-path>` — from the provider's discovery document (`token_endpoint`) |
| Redirect URL | fixed by the assistant vendor; register it on the client profile at the provider |

The connector must discover exactly `submit_query`, `get_query_result` and
`get_prompts`.

To check the deployed gateway, run `aws bedrock-agentcore-control get-gateway
--gateway-identifier <quick-gateway-id>`. It should show `discoveryUrl` = the
provider, `allowedAudience` = the gateway URL, no `allowedClients`, and one
REQUEST interceptor. The `QuickGatewayIdentityMode` output reads
`external-oidc`.

### Switch back to service authentication

Delete the two SSM parameters `/adt/quick-oidc-discovery-url` and
`/adt/quick-oidc-audience`, then redeploy with the same Quick flag (`--quick-api` or `--quick-connector`). The authorizer
returns to the Cognito shape in place and the interceptor is removed. Then
reconfigure the connector for service authentication (table above).

## Moving an existing bridge to the reference

A new deployment provisions the bridge with `QUICK_GATEWAY_TOKEN_REF`, so a
fresh account never has the token in runtime
env. An `adt_query_bridge` runtime provisioned by an older app may still carry
the plaintext `QUICK_GATEWAY_TOKEN`. On the `--quick-api` path that value is
the old token, because the token moved to the generated secret. When the app
holds the reference and finds such a runtime, it does not take the fast path:
it reprovisions the runtime, which rewrites its env, and if the old env is
still there afterwards the provisioner fails and retries
(`AgentManager._provision_query_bridge`). If the move keeps failing, recreate
the runtime once:

```bash
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id <adt_query_bridge-runtime-id> --region <region>
aws ecs update-service --cluster <cluster> --service <service> \
  --force-new-deployment --region <region>
```

The restarted app sees the target without a READY runtime and provisions the
runtime again from the current image, with the reference and no plaintext
key. Check it with `aws bedrock-agentcore-control get-agent-runtime`: the
`environmentVariables` keys include `QUICK_GATEWAY_TOKEN_REF` and not
`QUICK_GATEWAY_TOKEN`.

## Rollback (fully additive — clean removal)

```bash
# delete the target then the runtime (removes the connector's backing; the
# shared gateway and its targets are untouched). The adt-quick-gw gateway
# itself is CDK-owned and goes away on the next deploy without --quick-api
# and --quick-connector; deploy.sh refuses that deploy while the gateway
# still lists a target, so delete the target first. cleanup.sh deletes the
# gateway's targets itself before removing the stacks.
aws bedrock-agentcore-control delete-gateway-target \
  --gateway-identifier <quick-gateway-id> --target-id <query-bridge-target-id> --region <region>
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id <adt_query_bridge-runtime-id> --region <region>
```

## Source

- `stacks/agentcore_stack.py::_build_quick_gateway` — the `adt-quick-gw`
  gateway (app.py passes it `quick_api_enabled or quick_connector_enabled`).
- `stacks/_quick_connector.py`: the connector-only wiring (bridge env, the
  generated `x-quick-auth` secret, the ALB ingress, `QuickTokenUrl`,
  `QuickClientSecretName`, `adt-quick-client-secret`).
- `agentic_digital_thread/deploy.sh`: `--quick-connector`, the stop before
  CDK when `adt-quick-gw` still has targets and no Quick flag is given, and the
  READY wait plus connector values printed at the end.
- `stacks/adt_compute_stack.py::_env_quick_integration` — the
  `QUICK_GATEWAY_ID` / `QUICK_GATEWAY_URL` task env;
  `wire_quick_bridge_token_ref`: the `QUICK_GATEWAY_TOKEN_REF` task env and
  the runtime role's read grant on exactly that ARN.
- `agentic_digital_thread/agents/quick_gateway_token.py`: how the bridge
  reads the token from the reference (format:
  `runtime_env_contract.parse_quick_gateway_token_ref`).
- `agentic_digital_thread/agents/query_bridge_server.py` — the FastMCP tool server.
- `agentic_digital_thread/agents/runtime_app.py` — dispatch on
  `SYSTEM_ID=query-bridge`.
- `agentic_digital_thread/agents/agent_manager.py::ensure_query_bridge_target`
  — provisioning on the Quick gateway (same idempotent path as the
  `digital-thread` target), started at app startup only when
  `QUICK_API_ENABLED` is set on the task.
- ADR: [0071-dedicated-gateway-for-quick-query-bridge.md](adr/0071-dedicated-gateway-for-quick-query-bridge.md).
- Delegated user tokens: `stacks/_quick_gateway_auth.py` (the ONE home for the
  authorizer modes + the interceptor), `lambda-quick-interceptor/index.py`,
  `agentic_digital_thread/agents/auth/delegated_identity.py` (the app-side
  verifier); ADR: [0079](adr/0079-quick-gateway-external-oidc-delegated-user-tokens.md).
