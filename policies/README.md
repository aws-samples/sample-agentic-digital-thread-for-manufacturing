<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Cedar policy sources (ADR-0033)

Cedar authorization policies for the Amazon Bedrock AgentCore gateways. **These files are
inert** — kept as source/assets only. The **entire Cedar layer (policy engine +
policies + gateway attachment) is planned but not attached** and is **not declared in
the CDK stacks**. Both gateways keep the `CUSTOM_JWT` authorizer (the
identity-propagation seam) but have **no policy engine attached**.

| File | Target gateway | Attached |
|------|----------------|----------|
| `query_gateway_baseline.cedar` | Query gateway (`adt-gw`) | **Planned, not attached** (permit-all baseline — unattached) |
| `query_gateway.cedar` | Query gateway (`adt-gw`) | **Planned, not attached** (write-back forbid — unattached) |
| `onboarding_gateway_baseline.cedar` | Onboarding gateway (greenfield) | **Planned, not attached** (permit-all baseline — unattached) |
| `onboarding_gateway.cedar` | Onboarding gateway (greenfield) | **Planned, not attached** (commit-tier forbid — unattached) |

## The Cedar layer is not attached

Declarative-in-stack Cedar is **not possible pre-target-registration**. Two
distinct AgentCore rejections force the deferral:

- A **permit-all baseline** is rejected as **"Overly Permissive"** (action = Any
  Future Tools, resource = `gateway/*`).
- A **specific forbid** is rejected as an **"unrecognized action"** (e.g.
  `AgentCore::Action::"digital-thread___save_correlation"`), because a forbid's
  action ids are validated against the gateway's **registered** tools.

Both problems share a root cause: the gateway's tool action ids are only known
**after** its targets are registered, and the targets are registered
**imperatively** by `agentic_digital_thread/deploy.sh` (§2 / §2e) *after* `cdk deploy`.

**Planned attachment:** after `deploy.sh` registers the gateway targets (tool action
ids recognized), create the Cedar policy engine + policies **outside the CDK
stack**, scoping the permits to the **specific gateway ARN + real tool action
ids**, attach LOG_ONLY, soak, then flip each engine to **ENFORCE**.

## One statement per file (AgentCore constraint)

The AgentCore `CfnPolicy` `statement` accepts **exactly ONE** Cedar policy per
Policy resource. A file with both a `permit` and a `forbid` fails at
`CreatePolicy` with `ValidationException: unexpected token forbid`. So each
gateway's Cedar is split into two files / two `CfnPolicy` resources on the SAME
policy engine: a permit-all `*_baseline.cedar` and a single-`forbid`
`*_gateway.cedar`. A Cedar engine is deny-by-default, so the baseline permit is
what makes the engine usable in LOG_ONLY.

## Model

- **Principal role** comes from the `cognito:groups` claim on the forwarded
  `adt-app` access token, surfaced to Cedar as a **principal tag**. Groups are
  matched with `principal.hasTag("cognito:groups")` +
  `principal.getTag("cognito:groups").contains("<group>")` (exact group-name
  match) — **not** `Group::"..."` and **not** substring `like` patterns.
- Each engine carries a **permit-all baseline** plus the first real **`forbid`**
  (separate files / separate `CfnPolicy` resources):
  - Query: the digital-thread write-back tools `save_value_mapping` /
    `save_correlation` require the `integrator` or `Admin` group.
  - Onboarding: the onboarding runtime exports no commit-tier tool, so the
    forbid names only `discover` (pending Decision 2), which then requires the
    `integrator` or `Admin` group. `connect_new_system`, `store_credentials`,
    `provision_system`, `reset_system` and `get_catalog` are not on the
    gateway: the app runs them itself behind its own group check, and the
    runtime does not export them (ADR-0068 amendments 2026-10-18 and
    2026-10-21).
- Tools are referenced as **`AgentCore::Action::"<target>___<tool>"`** (never a
  bare `Action::`). The query gateway's target prefix is `digital-thread`; the
  onboarding gateway's target is `onboarding` (deploy.sh §2e, `--name onboarding`).

## Attachment (planned post-target-registration, outside the CDK stack)

```python
engine = agentcore.CfnPolicyEngine(self, "GwPolicyEngine", name=...)
agentcore.CfnPolicy(
    self, "QueryGwPolicy",
    policy_engine_id=engine.attr_policy_engine_id,
    definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
        cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=<file contents>),
    ),
    name=...,
)
# Attach to the gateway in LOG_ONLY first:
agentcore.CfnGateway(..., policy_engine_configuration=
    agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
        arn=engine.attr_policy_engine_arn, mode="LOG_ONLY"))
```

Rollback: set `mode` back to `LOG_ONLY` (or drop `policy_engine_configuration`)
and revert the authorizer to `AWS_IAM` — a single CDK deploy.
