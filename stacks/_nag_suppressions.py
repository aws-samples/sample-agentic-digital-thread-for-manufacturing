# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
cdk-nag suppressions for the Agentic Digital Thread sample.

This sample is designed to be deployed quickly for demonstration. The
suppressions documented here capture architectural decisions that are
appropriate for a *demo workload* — they are NOT recommendations for
production. Each rule includes a `reason` describing the trade-off so that
adopters can review and replace them before promoting any portion of this
stack to production.

Usage from `app.py`::

    from stacks._nag_suppressions import (
        apply_sample_suppressions,
        apply_network_suppressions,
        apply_erp_database_suppressions,
        apply_erp_lambda_suppressions,
        apply_erp_dashboard_suppressions,
        apply_adt_database_suppressions,
        apply_agentcore_suppressions,
        apply_adt_compute_suppressions,
        apply_highbyte_opcua_suppressions,
    )

    apply_sample_suppressions(network)
    apply_network_suppressions(network)
    # ... etc.
"""
from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo

from stacks import _apigw_log_role, _sample_hardening


def _with_cog2_reason(stack: cdk.Stack, pool: str,
                      suppressions: list[NagPackSuppression]) -> list[NagPackSuppression]:
    """*suppressions* with the AwsSolutions-COG2 reason worded for the profile:
    unchanged on a workshop synth, the MFA-OPTIONAL wording elsewhere
    (stacks/_sample_hardening.py)."""
    out = []
    for item in suppressions:
        if item.id == "AwsSolutions-COG2":
            item = NagPackSuppression(
                id=item.id, reason=_sample_hardening.cog2_reason(stack.node, pool, item.reason))
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Common: applied to every stack
# ---------------------------------------------------------------------------
def apply_sample_suppressions(stack: cdk.Stack) -> None:
    """Apply common sample-grade suppressions to the given stack.

    Covers the broad categories that surface across nearly every stack in the
    sample (managed policies on Lambda execution roles, wildcard permissions
    scoped to demo workloads, and CDK framework-controlled Lambda runtimes).
    """
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Sample uses AWS managed policies (e.g. "
                    "AWSLambdaBasicExecutionRole, AWSLambdaVPCAccessExecutionRole, "
                    "AmazonECSTaskExecutionRolePolicy, AmazonSSMManagedInstanceCore) "
                    "for brevity. Production deployments should replace these with "
                    "customer-managed least-privilege policies."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Sample IAM policies use wildcards scoped to ARNs of demo "
                    "workloads (e.g. CDK custom-resource framework Lambdas, the "
                    "sample's own buckets, IoT topics under a fixed prefix, demo "
                    "secrets). Production deployments should narrow each Action "
                    "and Resource further; see CONTRIBUTING.md for guidance."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Lambda runtime version is controlled by the CDK framework "
                    "for provider/custom-resource Lambdas (BucketDeployment, "
                    "Provider framework-onEvent, AWS679f53fac002430cb0da5b7982bd2287, "
                    "etc.). The sample does not pin the runtime; adopters can "
                    "override via a CDK Aspect."
                ),
            ),
        ],
        apply_to_nested_stacks=True,
    )


# ---------------------------------------------------------------------------
# ADTNetworkStack
# ---------------------------------------------------------------------------
#: Outside the workshop ADTNetworkStack owns the account's API Gateway
#: CloudWatch role setting (stacks/_apigw_log_role.py); these cover only that
#: role, its deploy-time setter and the setter's CDK provider.
_NETWORK_ACCOUNT_LOG_ROLE_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-IAM4",
        reason=(
            "The API Gateway CloudWatch role uses "
            "AmazonAPIGatewayPushToCloudWatchLogs, the policy the API Gateway "
            "guide names for it; the deploy-time role setter and its CDK "
            "provider use the AWS-managed basic execution role for their own "
            "logs only."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-IAM5",
        reason=(
            "The deploy-time API Gateway CloudWatch role setter reads whichever "
            "role the account already names (iam:GetRole and "
            "iam:SimulatePrincipalPolicy on this account's roles) to keep a "
            "usable one, and the CDK custom-resource provider's own role may "
            "invoke every version of that one setter function (<function ARN>:*)."
        ),
        applies_to=[
            "Resource::arn:<AWS::Partition>:iam::<AWS::AccountId>:role/*",
            RegexAppliesTo(regex="/^Resource::<ApiGatewayAccountLogRoleFn[A-Za-z0-9]+\\.Arn>:\\*$/g"),
        ],
    ),
]


def apply_network_suppressions(stack: cdk.Stack) -> None:
    """Suppressions specific to the ADTNetworkStack (VPC, security groups, demo password secret).
    Outside the workshop also the account log-role owner's resources."""
    if not _sample_hardening.is_workshop(stack.node):
        for child_id in ("", "Fn", "Provider"):
            child = stack.node.try_find_child(_apigw_log_role.ID_PREFIX + child_id)
            if child is not None:
                NagSuppressions.add_resource_suppressions(
                    child, _NETWORK_ACCOUNT_LOG_ROLE_SUPPRESSIONS, apply_to_children=True)
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-SMG4",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "DemoPasswordSecret stores a stable demo password set once "
                    "and preserved across redeployments. Automatic rotation is "
                    "not configured for this demonstration sample; rotate or "
                    "replace the credential for any non-demonstration use."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTERPDatabaseStack
# ---------------------------------------------------------------------------
def apply_erp_database_suppressions(stack: cdk.Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-SMG4",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Sample Aurora database-owner credential secret and the system agents' "
                    "read-only login secrets (one per schema, set on the database "
                    "by a deploy-time custom resource). Rotation is not configured "
                    "to keep the demo deployment simple; a rotated reader password "
                    "would need the custom resource to re-run. Enable for "
                    "production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-RDS10",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Aurora deletion protection is intentionally disabled so that "
                    "`cdk destroy` can clean up the sample without manual "
                    "intervention. Enable for any non-ephemeral deployment."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-EC28",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Bastion host is for demo SSM Session Manager access only. "
                    "Detailed monitoring is not enabled because the bastion is "
                    "short-lived. Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-EC29",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Bastion host termination protection is disabled so that "
                    "`cdk destroy` can clean up the sample. Enable for "
                    "production bastions."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTERPLambdaStack
# ---------------------------------------------------------------------------
def apply_erp_lambda_suppressions(stack: cdk.Stack) -> None:
    """No stack-specific findings beyond the common IAM4/IAM5 set."""
    # Common IAM4/IAM5/L1 suppressions cover all current ADTERPLambdaStack findings.
    return None


# ---------------------------------------------------------------------------
# ADTERPDashboardStack
# ---------------------------------------------------------------------------
_ERP_DASHBOARD_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-COG1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo Cognito user pool intentionally permits the documented "
            "demo password so anonymous reviewers can log into the "
            "dashboard without coordination. Strengthen the policy for "
            "production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-COG2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "MFA is disabled on the demo user pool to keep the reviewer "
            "experience friction-free. Enable MFA for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-COG8",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Plus-tier Cognito features (advanced security, threat "
            "protection) are not required for the sample. Enable for "
            "production user pools that handle real identities."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-ECS2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "ECS task environment variables are non-sensitive (region, "
            "log group, dashboard URL). Sensitive values such as DB "
            "credentials are already injected via Secrets Manager, not "
            "as plaintext env vars."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-ELB2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "ELB access logs are not configured to keep the deployment "
            "simple and to avoid the additional S3 bucket required for "
            "log delivery. Enable for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront distribution does not configure geographic "
            "restrictions. Configure as appropriate for your audience "
            "in production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR3",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront distribution does not enable access "
            "logging. Enable for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR4",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront distribution uses the default viewer "
            "certificate; the minimum TLS version is governed by the "
            "default certificate. Configure a custom certificate with "
            "TLS 1.2+ for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR5",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront origin uses the ALB's HTTP listener for "
            "simplicity. Production deployments should use HTTPS to the "
            "origin with TLS 1.2+."
        ),
    ),
]


def apply_erp_dashboard_suppressions(stack: cdk.Stack) -> None:
    """ERP dashboard stack (see _ERP_DASHBOARD_SUPPRESSIONS)."""
    NagSuppressions.add_stack_suppressions(
        stack, _with_cog2_reason(stack, "erp-dashboard-users", _ERP_DASHBOARD_SUPPRESSIONS))


# ---------------------------------------------------------------------------
# ADTDatabaseStack
# ---------------------------------------------------------------------------
def apply_adt_database_suppressions(stack: cdk.Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-DDB3",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "DynamoDB Point-in-Time Recovery is not enabled for the "
                    "adt-proposed-mappings table; the data is regenerated on "
                    "each demo run, so PITR offers no value here. Enable for "
                    "tables containing durable production data."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTSeedDataStack (opt-in demo source-system RDS + seed)
# ---------------------------------------------------------------------------
def apply_adt_seed_data_suppressions(stack: cdk.Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-SMG4",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "AdtDbSecret is a sample database-owner credential secret; "
                    "the SeedDbReaders secrets are the system agents' read-only "
                    "logins (one per schema, set on the database by a deploy-time "
                    "custom resource). Rotation is not configured for the demo. "
                    "Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-RDS3",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo single-AZ PostgreSQL instance to minimize cost. "
                    "Enable Multi-AZ for production high-availability."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-RDS10",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Deletion protection disabled on the demo RDS instance to "
                    "allow `cdk destroy` to clean up without manual steps. "
                    "Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-RDS11",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo RDS instance uses the default PostgreSQL port (5432). "
                    "Production deployments should use port obfuscation as a "
                    "defence-in-depth measure."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTAgentCoreStack
# ---------------------------------------------------------------------------
_AGENTCORE_M2M_COG2 = NagPackSuppression(
    id="AwsSolutions-COG2",
    reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
        "M2M user pool: MFA is not applicable to the client-credentials "
        "flow used for service-to-service auth. Enable for any user "
        "pools that authenticate humans."
    ),
)

_AGENTCORE_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-COG1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "M2M Cognito user pool used for service-to-service auth in "
            "the demo. The password policy is the Cognito default; "
            "real users are not provisioned in this pool."
        ),
    ),
    _AGENTCORE_M2M_COG2,
    NagPackSuppression(
        id="AwsSolutions-COG8",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Plus-tier features are not required for the M2M user pool "
            "in this sample."
        ),
    ),
]


def apply_agentcore_suppressions(stack: cdk.Stack) -> None:
    """AgentCore stack. The workshop keeps the stack-level set as rehearsed.
    Elsewhere COG2 is per pool: adt-app (people) is MFA OPTIONAL + TOTP,
    adt-m2m (client credentials, no users) has MFA off."""
    if _sample_hardening.is_workshop(stack.node):
        NagSuppressions.add_stack_suppressions(stack, _AGENTCORE_SUPPRESSIONS)
        return
    NagSuppressions.add_stack_suppressions(
        stack, [item for item in _AGENTCORE_SUPPRESSIONS if item.id != "AwsSolutions-COG2"])
    m2m = getattr(stack, "m2m_pool", None)
    if m2m is not None:
        NagSuppressions.add_resource_suppressions(m2m, [_AGENTCORE_M2M_COG2], apply_to_children=True)
    app_pool = getattr(stack, "adt_user_pool", None)
    if app_pool is not None:
        NagSuppressions.add_resource_suppressions(app_pool, [NagPackSuppression(
            id="AwsSolutions-COG2",
            reason=_sample_hardening.cog2_reason(stack.node, "adt-app", ""),
        )], apply_to_children=True)


# ---------------------------------------------------------------------------
# ADTComputeStack
# ---------------------------------------------------------------------------
def apply_adt_compute_suppressions(stack: cdk.Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-S1",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo media/video buckets (AdtMediaBucket, AdtVideoBucket) "
                    "used by the sample dashboard. Server access logging is not "
                    "enabled to avoid the dependency on a separate log bucket. "
                    "Production deployments should enable access logging."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-COG1",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "AdtAppUserPool is a demo user pool with the documented demo "
                    "password. Strengthen the policy for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-COG2",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "MFA disabled on the demo AdtAppUserPool to keep reviewer "
                    "login friction-free. Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-COG8",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Plus-tier Cognito features not required for the sample ADT "
                    "user pool."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-ECS2",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "ECS task env vars are non-sensitive configuration (region, "
                    "table names, bucket names). Sensitive values are injected "
                    "via Secrets Manager."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo ALB access logs disabled to keep deployment simple. "
                    "Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR1",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo CloudFront distribution does not configure geographic "
                    "restrictions. Configure as appropriate for your audience "
                    "in production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR3",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo CloudFront distribution does not enable access "
                    "logging. Enable for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR4",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo CloudFront distribution uses the default viewer "
                    "certificate; the minimum TLS version is governed by the "
                    "default certificate. Configure a custom certificate with "
                    "TLS 1.2+ for production."
                ),
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR5",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "Demo CloudFront origin uses the ALB's HTTP listener for "
                    "simplicity. Production deployments should use HTTPS to the "
                    "origin with TLS 1.2+."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTOnboardingStack (session table + runtime role + JWT gateway)
# ---------------------------------------------------------------------------
def apply_onboarding_suppressions(stack: cdk.Stack) -> None:
    """Suppressions specific to the onboarding control-plane stack.

    The common IAM4/IAM5/L1 set (apply_sample_suppressions) already covers the
    least-privileged runtime role wildcards (each scoped to account+region
    ARNs and documented inline). This covers the DynamoDB finding the
    onboarding stack adds.
    """
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-DDB3",
                reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                    "DynamoDB Point-in-Time Recovery is not enabled for the "
                    "adt-onboarding-sessions table; onboarding session state is "
                    "ephemeral demo state (TTL-reaped, regenerated on re-onboard), "
                    "so PITR offers no value here. Enable for durable production data."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTHighByteOpcuaStack
# ---------------------------------------------------------------------------
# ADTHighByteOpcuaStack suppressions — kept as a module-level list so the
# applier function below stays small; each entry documents its demo trade-off.
_HIGHBYTE_OPCUA_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-EFS1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Sample EFS file system. Encryption at rest is left at the "
            "default to avoid KMS-key dependencies in fresh regions "
            "during the demo. Enable explicit KMS encryption for "
            "production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-SMG4",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "HighBytePassword is a demo bootstrap secret for the "
            "HighByte container. Rotation not configured for the demo. "
            "Enable for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-ECS2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "OPC UA and HighByte ECS tasks pass non-sensitive "
            "configuration via env vars. Sensitive values are injected "
            "via Secrets Manager."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-ECS4",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "CloudWatch Container Insights is disabled on the demo "
            "ECS cluster to avoid additional CloudWatch costs during "
            "short-lived demos. Enable for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-ELB2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo ALB access logs disabled across the OPC services to "
            "keep deployment simple. Enable for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-COG1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo Cognito user pool (highbyte-ui-users) intentionally "
            "permits the documented demo password so reviewers can log "
            "into the HighByte UI without coordination. Strengthen the "
            "policy for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-COG2",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "MFA is disabled on the demo HighByte UI user pool to keep "
            "the reviewer experience friction-free. Enable MFA for "
            "production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-COG8",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Plus-tier Cognito features (advanced security, threat "
            "protection) are not required for the sample HighByte UI "
            "user pool. Enable for production user pools that handle "
            "real identities."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR1",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront distribution in front of HighByte does "
            "not configure geographic restrictions."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR3",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront access logging disabled. Enable for "
            "production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR4",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront uses the default viewer certificate when no "
            "custom domain is configured. Configure a custom cert with "
            "TLS 1.2+ for production."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-CFR5",
        reason=(  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
            "Demo CloudFront origin uses HTTP for simplicity. "
            "Use TLS 1.2+ to origin in production."
        ),
    ),
]


def apply_highbyte_opcua_suppressions(stack: cdk.Stack) -> None:
    """HighByte/OPC UA stack suppressions (see _HIGHBYTE_OPCUA_SUPPRESSIONS).

    No AwsSolutions-CFR2 (no WAF) suppression: the HighByte distribution
    carries an AWS WAF web ACL.
    """
    NagSuppressions.add_stack_suppressions(
        stack, _with_cog2_reason(stack, "highbyte-ui-users", _HIGHBYTE_OPCUA_SUPPRESSIONS))


# ---------------------------------------------------------------------------
# Quick stacks (opt-in: quick_api_enabled)
# ---------------------------------------------------------------------------
_QUICK_API_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-COG4",
        reason=(
            "Every method uses AWS_IAM (SigV4) authorization by design. The "
            "callers are machines with in-account AWS identities (an "
            "instance role or customer account credentials), not browser "
            "users; a Cognito user-pool authorizer does not fit this "
            "machine-to-machine surface. The removed API-key path must not "
            "return (tests/test_quick_stacks.py pins zero ApiKey/UsagePlan)."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-APIG1",
        reason=(
            "Demo-scale: stage access logging requires an account-level API "
            "Gateway CloudWatch role this sample does not manage. Enable "
            "access logs before any non-demonstration use."
        ),
    ),
    NagPackSuppression(
        id="AwsSolutions-APIG6",
        reason=(
            "Demo-scale: per-method CloudWatch execution logging requires the "
            "same account-level API Gateway CloudWatch role (see APIG1)."
        ),
    ),
]


#: Outside the workshop the Quick API stage writes access logs; the account's
#: API Gateway CloudWatch role it needs is owned by ADTNetworkStack
#: (stacks/_apigw_log_role.py). APIG1 no longer applies.
_QUICK_API_LOGGED_SUPPRESSIONS = [
    item for item in _QUICK_API_SUPPRESSIONS
    if item.id not in ("AwsSolutions-APIG1", "AwsSolutions-APIG6")
] + [
    NagPackSuppression(
        id="AwsSolutions-APIG6",
        reason=(
            "No per-method execution logging: the stage access log records "
            "every request, and execution logs would add request and response "
            "detail this IAM-only machine API does not need."
        ),
    ),
]


def apply_quick_api_suppressions(stack: cdk.Stack) -> None:
    """Quick API Gateway stack (IAM/SigV4 machine surface — see each reason).
    The workshop keeps the pre-access-log set (its stage is unchanged)."""
    NagSuppressions.add_stack_suppressions(
        stack,
        _QUICK_API_SUPPRESSIONS if _sample_hardening.is_workshop(stack.node)
        else _QUICK_API_LOGGED_SUPPRESSIONS)


# ---------------------------------------------------------------------------
# SFC stack (opt-in: sfc_enabled — ADR-0082)
# ---------------------------------------------------------------------------
_SFC_SUPPRESSIONS = [
    NagPackSuppression(
        id="AwsSolutions-ECS2",
        reason=(
            "The SFC task's env vars are non-sensitive: the module directory "
            "and the internal OPC UA NLB host names its generated config "
            "expands. It holds no credentials: SiteWise and S3 Tables are "
            "reached with the task role."
        ),
    ),
]


def apply_sfc_suppressions(stack: cdk.Stack) -> None:
    """SFC writer stack (non-sensitive task env — see the reason)."""
    NagSuppressions.add_stack_suppressions(stack, _SFC_SUPPRESSIONS)
