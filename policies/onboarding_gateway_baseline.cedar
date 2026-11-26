// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Cedar BASELINE policy for the ADT ONBOARDING gateway (ADR-0033).
//
// DEFERRED — UNATTACHED. The ENTIRE Cedar layer (policy engine +
// baseline permit + commit-tier forbid + gateway policy-engine attachment) is
// NOT declared in the CDK stack. A declarative-in-stack Cedar layer is not
// possible pre-target-registration: AgentCore rejects this permit-all as "Overly
// Permissive" (action = Any Future Tools, resource = gateway/*) at deploy time.
// The gateway is CUSTOM_JWT with NO policy engine; create the engine + policies
// AFTER deploy.sh registers the gateway target (real tool action ids known),
// scope the permits to the specific gateway ARN, then flip to ENFORCE.
//
// AgentCore's CfnPolicy `statement` accepts EXACTLY ONE Cedar policy per Policy
// resource, so the baseline permit-all and the commit-tier forbid live in
// SEPARATE files / SEPARATE CfnPolicy resources on the same policy engine (see
// onboarding_gateway.cedar for the forbid).
//
// When wired it becomes CfnPolicy `OnboardingBaselinePolicy` (name
// adt_onboard_baseline) on the onboarding policy engine (adt_onboard_policy_engine),
// LOG_ONLY, scoped to the specific gateway ARN.
permit (
    principal,
    action,
    resource is AgentCore::Gateway
);
