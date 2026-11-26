// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Cedar WRITE-BACK-TIER policy for the ADT QUERY gateway (ADR-0033).
//
// DEFERRED — UNATTACHED. The ENTIRE Cedar layer (policy engine +
// baseline permit + this write-back forbid + gateway policy-engine attachment)
// is NOT declared in the CDK stack. A declarative-in-stack Cedar layer is not
// possible pre-target-registration: AgentCore rejects a permit-all baseline as
// "Overly Permissive" (action = Any Future Tools, resource = gateway/*), and it
// rejects this specific forbid as "unrecognized action
// AgentCore::Action::"digital-thread___save_correlation"" — the gateway's tool
// action ids are only known AFTER its digital-thread target is registered, and
// that target is created IMPERATIVELY by agentic_digital_thread/deploy.sh (§2) AFTER
// `cdk deploy`. The gateway is CUSTOM_JWT with NO policy engine. To attach the
// layer, after deploy.sh registers the targets: create the policy engine + the
// permit-all baseline (query_gateway_baseline.cedar) + this forbid, scoped to
// the specific gateway ARN + real tool action ids, attach LOG_ONLY, soak, then
// flip to ENFORCE.
//
// ONE statement only. AgentCore's CfnPolicy `statement` accepts exactly one
// Cedar policy per Policy resource; the permit-all baseline lives in
// query_gateway_baseline.cedar as a separate CfnPolicy on the same engine.
//
// NOT attached at `cdk deploy` time (deferred — see the header note above).
// When wired it becomes CfnPolicy `QueryWritebackTierPolicy` (name
// adt_query_writeback_tier) on the query policy engine (adt_query_policy_engine),
// alongside the AWS_IAM -> CUSTOM_JWT(adt-app) authorizer flip, attached
// LOG_ONLY first; flip to ENFORCE after a clean LOG_ONLY soak.
//
// GRAMMAR (per the AWS AgentCore Policy docs/blog):
//   - Tools are referenced as `AgentCore::Action::"<target>___<tool>"`. The
//     query gateway's digital-thread target surfaces the tools from
//     agentic_digital_thread/agents/digital_thread_server.py prefixed as
//     `digital-thread___<tool>`.
//   - Cognito group membership is matched via principal TAGS on the forwarded
//     `adt-app` access token: `principal.hasTag("cognito:groups")` +
//     `principal.getTag("cognito:groups").contains("<group>")` (EXACT
//     group-name element match on the set-valued tag — no substring `like`
//     patterns, which would also match e.g. "not-integrator") — NOT
//     `Group::"..."` and NOT a bare `Action::`.
//
// RULE: the digital-thread write-back tools save_value_mapping /
// save_correlation may only be invoked by integrators (the elevated write-back
// role per ADR-0033) or Admins. Everyone else, including analysts, is forbidden;
// read/explore tools stay open via the baseline permit.
forbid (
    principal,
    action,
    resource is AgentCore::Gateway
)
when {
    action in [
        AgentCore::Action::"digital-thread___save_value_mapping",
        AgentCore::Action::"digital-thread___save_correlation"
    ]
}
unless {
    principal.hasTag("cognito:groups") &&
    (
        principal.getTag("cognito:groups").contains("integrator") ||
        principal.getTag("cognito:groups").contains("Admin")
    )
};
