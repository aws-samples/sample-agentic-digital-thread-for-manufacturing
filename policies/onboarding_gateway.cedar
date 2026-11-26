// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Cedar COMMIT-TIER policy for the ADT ONBOARDING gateway (ADR-0033).
//
// DEFERRED — UNATTACHED. The ENTIRE Cedar layer (policy engine +
// baseline permit + this commit-tier forbid + gateway policy-engine attachment)
// is NOT declared in the CDK stack. A declarative-in-stack Cedar layer is not
// possible pre-target-registration: AgentCore rejects a permit-all baseline as
// "Overly Permissive" (action = Any Future Tools, resource = gateway/*), and it
// rejects this specific forbid as an "unrecognized action
// AgentCore::Action::"onboarding___..."" — the gateway's tool action ids are
// only known AFTER its target is registered, and that target is created
// IMPERATIVELY by agentic_digital_thread/deploy.sh (§2e) AFTER `cdk deploy`. The
// gateway is CUSTOM_JWT with NO policy engine. To attach the layer, after
// deploy.sh registers the target: create the policy engine + the permit-all
// baseline (onboarding_gateway_baseline.cedar) + this forbid, scoped to the
// specific gateway ARN + real tool action ids, attach LOG_ONLY, soak, then flip
// to ENFORCE.
//
// ONE statement only. AgentCore's CfnPolicy `statement` accepts exactly one
// Cedar policy per Policy resource; the permit-all baseline lives in
// onboarding_gateway_baseline.cedar as a separate CfnPolicy on the same engine.
//
// NOT attached at `cdk deploy` time (deferred — see the header note above).
// When wired it becomes CfnPolicy `OnboardingCommitTierPolicy` (name
// adt_onboard_commit_tier) on the onboarding policy engine
// (adt_onboard_policy_engine), attached LOG_ONLY first; flip to ENFORCE
// after a clean LOG_ONLY soak.
//
// GRAMMAR (per the AWS AgentCore Policy docs/blog):
//   - Tools are referenced as `AgentCore::Action::"<target>___<tool>"`. The
//     onboarding gateway target is created imperatively by
//     agentic_digital_thread/deploy.sh §2e with `--name "onboarding"` (confirmed at
//     deploy.sh line 605), so tools surface prefixed as `onboarding___<tool>`.
//   - Cognito group membership is matched via principal TAGS on the forwarded
//     `adt-app` access token: `principal.hasTag("cognito:groups")` +
//     `principal.getTag("cognito:groups").contains("<group>")` (EXACT
//     group-name element match on the set-valued tag — no substring `like`
//     patterns, which would also match e.g. "not-integrator") — NOT
//     `Group::"..."` and NOT a bare `Action::`.
//
// TIER PARITY: tests/unit/test_cedar_policies.py pins this action list against
// the tools the onboarding runtime exports (onboard_agent.ONBOARD_TOOLS without
// onboard_chat_lifecycle.NOT_ON_GATEWAY): every exported commit-tier tool is
// forbidden, no explore-tier tool is except `discover` (see the NOTE below), and
// no action the target does not have is named (an unknown action id is
// rejected when the policy is attached). The ADR-0068 tools
// `assess_vocabulary_fit` and `propose_concepts` are explore-tier and
// ZERO-WRITE (steward accept goes through the app's onboard-gated import
// route), so they ride the baseline permit and are deliberately absent here.
//
// NOT ON THE GATEWAY: every commit-tier onboarding tool (`connect_new_system`,
// `store_credentials`, `provision_system`, `reset_system`) and `get_catalog`
// (explore tier) are run by the app itself in a deployment, inside the
// onboarding chat request that the app's integrator/Admin check gates, and the
// runtime does not export them, so they are not named below. In particular
// `store_credentials`, which writes `adt/systems/<id>` in AWS Secrets Manager,
// is written from the app and is no longer callable on the gateway.
//
// WHAT THE GATEWAY STILL SERVES: only the explore tier, that is `get_system_info`,
// `test_connection`, `discover`, `assess_vocabulary_fit`, `propose_concepts`,
// `map_fields`, `resolve_identities` and `verify_system`. They read, probe and
// analyse a system and record its onboarding progress; none of them writes a
// secret. `discover` stays on the gateway by design (Decision 2 classes it as
// explore, and the app's chat forwards a runtime-owned case to it). Until this
// layer is attached and ENFORCED, any app token can call these tools on the
// gateway directly (the maintainers' deferred-work list).
//
// RULE: `discover` requires the `integrator` role (onboard.write) or Admin
// once attached. The other explore tools stay open via the baseline permit.
//
// NOTE: `onboarding___discover` is listed below even though it is a probe
// tool, because it reads the source and starts a durable onboarding session.
// If Decision 2's EXPLORE classification is authoritative at the enforce step,
// drop it, and this file then has no action left to forbid. LOG_ONLY
// tolerates the difference (decisions are logged, not enforced), so this is
// safe to refine later without a deploy risk.
forbid (
    principal,
    action,
    resource is AgentCore::Gateway
)
when {
    action in [
        AgentCore::Action::"onboarding___discover"
    ]
}
unless {
    principal.hasTag("cognito:groups") &&
    (
        principal.getTag("cognito:groups").contains("integrator") ||
        principal.getTag("cognito:groups").contains("Admin")
    )
};
