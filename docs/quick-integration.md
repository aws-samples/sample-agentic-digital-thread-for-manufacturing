<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# AWS Quick integration — which path?

ADT has two ways for AWS Quick to reach it. They answer differently and are
documented separately. Both go through an Amazon Bedrock AgentCore gateway
authenticated by the Amazon Cognito `adt-app` user pool. Pick the row that
matches what Quick should *be*, then follow its doc.

| # | Role of Quick | Gateway / entry point | What Quick sees | Inbound auth | Answer | Deploy flag | Doc |
|---|---|---|---|---|---|---|---|
| a | **Analyst**: Quick reasons over the live tools itself | Shared AgentCore gateway `adt-query-gw` (`GatewayUrl` output) | Every onboarded system's tools + the knowledge-graph tools (`digital-thread` target) + `get_query_workflow` | CUSTOM_JWT: Cognito `adt-app` bearer token (`adt-app-service` client_credentials, scope `adt/query`) | The synthesis Quick does itself, guided by the orchestrator's Mode-B workflow | always (base deploy) | [amazon-quick-skill.md](amazon-quick-skill.md) |
| b | **Consumer** — Quick asks ADT and shows the orchestrator's answer | Dedicated AgentCore gateway `adt-quick-gw` (`QuickGatewayUrl` output), holding ONLY the `query-bridge` target | Exactly `submit_query`, `get_query_result`, `get_prompts` | CUSTOM_JWT — same Cognito `adt-app` issuer, same `adt-app-service` client, scope `adt/query` (Quick "Service authentication"); OR delegated user tokens from an external OIDC identity provider ([ADR-0079](adr/0079-quick-gateway-external-oidc-delegated-user-tokens.md)) | The orchestrator's finished answer via async submit/poll | `--quick-connector` (`quick_connector_enabled`, connector only) or `--quick-api` (`quick_api_enabled`) | [quick-web-connector.md](quick-web-connector.md) |

## How to choose

- Want Quick to **explore the data and reason** (build its own charts, ask
  follow-ups on raw rows)? Row (a). It is the "be the orchestrator" path and
  the only one that exposes system tools.
- Want Quick web / Quick Suite to give **the same answer the ADT UI gives**?
  Row (b). This is the general Quick path; anything that can hold an OAuth
  client secret uses it.
- A client that cannot do OAuth but can sign with an AWS identity can call
  the IAM-authorized REST API that `--quick-api` adds (SigV4).

## Why (b) has its own gateway

The Quick MCP connector fans out over every tool a gateway exposes. Putting the
`query-bridge` target on the shared `adt-query-gw` gave Quick the bridge tools
*and* every system's raw tools, and it reasoned over the raw tools instead of
calling the bridge — so the "consumer" path silently became a worse version of
the "analyst" path. `adt-quick-gw` holds only the bridge target, so Quick has
exactly three tools and calls them. Both gateways trust the Cognito `adt-app`
issuer, but Quick's connector signs in with its own client,
`adt-quick-service`, which only `adt-quick-gw` accepts. The secret a Quick
admin (or a workshop attendee) holds therefore cannot call `adt-query-gw`'s
system targets or `adt-onboard-gw`. See
[ADR-0071](adr/0071-dedicated-gateway-for-quick-query-bridge.md) and its
2026-11-07 amendment.

## Common facts

- Rows (a) and (b) are **bearer-JWT** gateways. Both AgentCore gateways use
  CUSTOM_JWT inbound auth; neither is SigV4. Only the `--quick-api` REST API
  is SigV4.
- `scripts/mint-query-token.sh` mints a valid `adt-app-service` token for rows
  (a) and (b) — use it to prove the credentials before configuring Quick.
- Base deploys carry only row (a). `--quick-connector` adds row (b) alone
  ([ADR-0093](adr/0093-quick-connector-in-the-workshop-package.md)); `--quick-api`
  adds row (b) plus the IAM-authorized REST API.
