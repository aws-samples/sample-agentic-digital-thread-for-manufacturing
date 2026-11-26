<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0049: User-identity propagation to system agents uses an app-signed claim, not gateway passthrough

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


## Status

Accepted — 2026-07-20. Live probes run 2026-07-19/20 (Management account,
us-east-1): the control-plane model exposes JWT_PASSTHROUGH, but the live
API rejects it for MCP-server targets (write-probe evidence below), so the
app-signed-claim decision stands with a re-probe watch item.

## Live-probe findings (2026-07-19, read-only)

- Fresh botocore (1.43.51) `CreateGatewayTarget.credentialProviderType` enum:
  `['GATEWAY_IAM_ROLE', 'OAUTH', 'API_KEY', 'CALLER_IAM_CREDENTIALS',
  'JWT_PASSTHROUGH']` — **`JWT_PASSTHROUGH` exists**, contradicting the
  older CDK-generated shapes (aws-cdk-lib 2.257.0) that showed a closed
  3-value enum. `oauthCredentialProvider.grantType` now also includes
  `TOKEN_EXCHANGE`. `CreateGateway` additionally exposes
  `interceptorConfigurations` and `policyEngineConfiguration`.
- Live query gateway (`adt-query-gw`) confirmed: CUSTOM_JWT
  authorizer (app-pool discovery URL, two allowed clients), MCP protocol
  config carries only search/version fields; targets (erp, mes-cr,
  digital-thread, …) READY on the OAUTH/CLIENT_CREDENTIALS wiring the repo
  code describes.
- Implication — **RESOLVED by write-probe (2026-07-20)**: a disposable
  `create_gateway_target` with `credentialProviderType: JWT_PASSTHROUGH`
  against the live query gateway was REJECTED with
  `ValidationException: MCP server target does not support JWT_PASSTHROUGH
  credential provider type` (boto3/botocore 1.43.51). The enum value exists
  for other target types, but NOT for the MCP-server targets this
  architecture uses. **Option A is not available for this system today; the
  decision below stands.** Watch item: re-probe when MCP-server targets gain
  passthrough support — the one-command probe is preserved in this ADR's
  history.

## Context

Interactive users authenticate to the ADT app (Cognito user pool) and the
orchestrator forwards the user's JWT on the orchestrator → gateway hop
(`agents/auth/forwarding.py`, `userAccessToken`; service/background turns use
`adt-app-service` client credentials — no fallback between the two). The
gateway → system-agent hop, however, is machine-to-machine only: every gateway
target is configured with `credentialProviderType: OAUTH`, `grantType:
CLIENT_CREDENTIALS` against the `adt-m2m` provider
(`agents/agent_manager.py`, `agents/provisioner/handlers.py`, `deploy.sh`).
System agents therefore act with service identity and cannot make per-user
authorization decisions at the source systems.

We evaluated two options for carrying the interactive user's identity across
the gateway to system-agent runtimes.

## Option A — native gateway passthrough: NOT SUPPORTED today

Verified against the installed API shapes (aws-cdk-lib 2.257.0
`aws_bedrockagentcore` L1s and the `bedrock_agentcore` SDK 1.1.1, both
generated from the service model):

1. **Closed credential-provider enum.** Gateway-target
   `credentialProviderType ∈ {API_KEY, OAUTH, GATEWAY_IAM_ROLE}` — no
   passthrough or inbound-token-reuse mode; `Create/UpdateGatewayTarget` has
   no field referencing caller identity or header forwarding.
2. **The gateway re-authenticates outbound by design.** The inbound JWT is
   consumed by the gateway's CUSTOM_JWT authorizer and dropped; outbound
   credentials are minted from Token Vault as the TARGET's own 2LO identity.
3. **`protocolConfiguration` (MCP)** carries only `searchType` +
   `supportedVersions` — no identity options.
4. **The OBO primitive exists but cannot close the loop here.**
   `OAuth2CredentialProvider.onBehalfOfTokenExchangeConfig` (RFC 8693-style)
   signals AWS is building toward this, but (a) nothing lets the gateway
   supply the inbound caller's JWT as the exchange's subject token, and
   (b) the targets' IdP is Cognito, which does not support the
   token-exchange grant. A raw user-token passthrough would additionally be
   rejected by each runtime's inbound `customJWTAuthorizer`, which trusts
   only the M2M client.

Option A would also break AWS Quick and other external M2M callers, whose
inbound tokens carry no user identity by construction, and any authorizer
change forces a gateway replacement (authorizer type is immutable).

## Decision (Option B)

The orchestrator mints a **short-lived, app-signed identity claim**
(subject + groups, ≤5 minute expiry, deploy-provisioned signing secret) and
attaches it in a reserved tool-call argument on **interactive turns only**.
System-agent runtimes verify the signature, pop the argument before query
handling, and apply a single reference enforcement hook
(`SYSTEM_REQUIRED_GROUP`; unset ⇒ allow-all, keeping the engine generic).
When the group IS set, the policy **fails closed**: a missing signing-key
ARN on the runtime, an unreadable key, or a missing/invalid claim all deny
(honest deny text naming the system) — a half-configured runtime never
silently voids an operator's group restriction.
Service, background, and external-M2M (Quick) tool calls attach nothing and
remain byte-identical.

Rules that keep this safe in this codebase:

- Runtime deny texts MUST NOT substring-match `_AUTH_TOOL_ERROR_MARKERS` or
  the transient-retry markers (pinned by unit test) so genuine denials are
  never silently retried.
- The claim is never logged and never reaches memory or the graph.
- The gateway transport hop is unchanged (still M2M): the platform does not
  vouch for the claim — acceptable and documented for a reference sample.

## Consequences

- Per-user authorization becomes possible at system agents with zero gateway
  or platform changes, offline-testable end to end.
- **Under Authorization@Edge (ADR-0070, amended 2026-09-23) the forwarded
  end-user bearer is the EDGE client's access token.** The app forwards the
  signed-in user's Cognito access token to the orchestrator
  (`agents/auth/forwarding.py`), which uses it as the gateway bearer
  (`_resolve_gateway_bearer`). With edge auth on, that token is minted by the
  edge OAuth client (`AdtEdgeAuthClient`, owned by `ADTAgentCoreStack`), not
  by `adt-app-client` — so BOTH gateway `CUSTOM_JWT` authorizers (query and
  onboarding) allow the edge client alongside `adt-app-client` and
  `adt-app-service`. A gateway authorizer that omits the edge client 403s
  every interactive query from an edge-authenticated session (verified live
  2026-09-23; pinned by `tests/test_edge_external_oidc.py`).
- **2026-09-24 — the forwarded bearer must be refreshed BEFORE it can expire
  mid-turn.** Observed live on a deployed environment: the origin's `AuthMiddleware` only
  refreshed AFTER `TokenExpiredError`, so a token with a few seconds left
  validated at the origin, was forwarded as the gateway bearer, and the
  gateway 403'd it mid-turn ("Failed to start MCP client … 403 Forbidden" →
  "Query failed.") — a ~30 s burst at every 60-min token boundary (login
  17:37:43Z, 403 burst 18:37:15–18:37:43Z) that ate 10 golden runs. Fixed by
  proactive refresh within `REFRESH_LEEWAY` (120 s) in
  `agents/auth/middleware.py` (one shared refresh helper with the expired
  path; near-expiry refresh failure falls through with the still-valid
  token), plus writing the refreshed session back to the `adt_session`
  cookie. The eval/smoke lanes additionally re-login every
  `SESSION_MAX_AGE_SECONDS` (45 min) before each ask
  (`smoke_prompts.App.ensure_fresh_session`).
- We own the trust anchor (key rotation, compromise handling) — documented
  demo-grade limitation.
- **Watch item:** revisit Option A when AgentCore ships gateway-level
  on-behalf-of / subject-token injection; the `onBehalfOfTokenExchangeConfig`
  primitive suggests it is coming. Re-run the probe checklist at CDK/botocore
  upgrades.

## Live-probe checklist (Management account, read-only — pending)

1. Fresh-botocore introspection of `CreateGatewayTarget.credentialProviderType`
   enum (confirm no new PASSTHROUGH value) and `oauthCredentialProvider.grantType`
   values (confirm no TOKEN_EXCHANGE).
2. `get-gateway` / `get-gateway-target` on the deployed query gateway —
   confirm live shapes expose no identity-forwarding fields and targets match
   the in-repo OAUTH/CLIENT_CREDENTIALS wiring.
3. Optional negative probe: one gateway tool call with a user bearer,
   capturing the target runtime's auth log line showing the gateway's M2M
   principal (not the user).
