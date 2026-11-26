# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ADT Compute Stack

Provisions:
- ECS Fargate service running the ADT dashboard + agents
- Internal ALB
- IoT Core policy + endpoint lookup
- VPC endpoints for ECR, S3, Secrets Manager, Bedrock

Ported from the original TypeScript CDK (cdk/lib/ecs-stack.ts, iot-stack.ts).
"""

import json
import os
from typing import Optional

from aws_cdk import (
    Stack,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_iot as iot,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
    aws_bedrock as bedrock,
    aws_ecr_assets as ecr_assets,
    custom_resources as cr,
)
from constructs import Construct
from cdk_nag import NagPackSuppression, NagSuppressions

from stacks._bedrock_access import bedrock_marketplace_auto_subscribe_statement, mantle_inference_statements
# (Cognito hosted-UI domain is owned by ADTAgentCoreStack — no pinning here)
from stacks._edge_auth import (
    ExternalOidcConfig,
    create_callback_updater,
    create_standard_web_acl,
    deploy_edge_auth,
    edge_behavior_parts,
    edge_http_headers,
)
from stacks._graph_access import graph_data_plane_statement
from stacks._os_patch import patched_image_options
from stacks._quick_header_strip import (
    create_quick_header_strip,
    quick_header_strip_associations,
)
from stacks import _build_path_endpoints
from stacks import _model_allowlist
from stacks import _workshop_story
from stacks.erp_lambda_stack import STORY_HISTORY_FUNCTION
from stacks._cross_account import (
    grant_cross_account_assume,
)

#: App task env naming WHERE the x-quick-auth token lives: one ARN, of a
#: Secrets Manager secret or an SSM parameter (format:
#: agents/runtime_env_contract.parse_quick_gateway_token_ref). The app passes
#: it to the query-bridge runtime in place of the token itself.
QUICK_GATEWAY_TOKEN_REF_ENV = "QUICK_GATEWAY_TOKEN_REF"  # nosec B105 - env var NAME, not a credential

# What each wildcard on the app task role is. Each grant on '*' or with a
# wildcard action is bounded. On '*': the bedrock-mantle bearer-token call
# (no resource type; inference itself is scoped to this account's Mantle
# projects); read and query calls for cloudwatch metrics, ec2
# network describes, logs queries, iotsitewise reads and
# sts:GetCallerIdentity; polly:SynthesizeSpeech; aws-marketplace Subscribe and
# ViewSubscriptions only when called through Amazon Bedrock
# (aws:CalledViaLast); iam:CreateServiceLinkedRole only for the AgentCore
# service-linked role (iam:AWSServiceName). The bedrock-agentcore actions are
# an enumerated list (_agentcore_runtime_statements), never the service
# wildcard: five with no resource type on '*', the rest limited to AgentCore
# resources in this account and Region, which the app creates and manages at
# run time. iam:PassRole names
# the two runtime roles. checkov passes CKV_AWS_109 on this role (the one
# permissions-management grant on '*' carries a condition), so the role
# carries no checkov skip: the Workshop Studio scanner counts every skip in
# its rule set as a finding (stacks/_checkov_skips.py).


#: The resource-addressable bedrock-agentcore actions the app task calls, by
#: name (ADTComputeStack._agentcore_runtime_statements scopes them to this
#: account and Region). Enumerated instead of the service wildcard.
_AGENTCORE_SCOPED_ACTIONS = (
    "bedrock-agentcore:UpdateAgentRuntime",
    "bedrock-agentcore:GetAgentRuntime",
    "bedrock-agentcore:DeleteAgentRuntime",
    # Implicit side effects AgentCore authorizes against the
    # caller: CreateAgentRuntime creates the DEFAULT endpoint
    # and the runtime's workload identity, DeleteAgentRuntime
    # removes them, an update rewrites the endpoint. Without
    # these, provisioning fails AccessDenied on
    # CreateAgentRuntime (tests/test_compute_agentcore_iam.py;
    # the same set the workshop engine role needed).
    "bedrock-agentcore:CreateAgentRuntimeEndpoint",
    "bedrock-agentcore:GetAgentRuntimeEndpoint",
    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
    "bedrock-agentcore:CreateWorkloadIdentity",
    "bedrock-agentcore:GetWorkloadIdentity",
    "bedrock-agentcore:DeleteWorkloadIdentity",
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:StopRuntimeSession",
    "bedrock-agentcore:CreateGatewayTarget",
    "bedrock-agentcore:UpdateGatewayTarget",
    "bedrock-agentcore:GetGatewayTarget",
    "bedrock-agentcore:DeleteGatewayTarget",
    "bedrock-agentcore:ListGatewayTargets",
    "bedrock-agentcore:SynchronizeGatewayTargets",
    "bedrock-agentcore:CreateOauth2CredentialProvider",
    "bedrock-agentcore:ListOauth2CredentialProviders",
)


class ADTComputeStack(Stack):
    """Agentic Digital Thread ECS Fargate service + IoT Core."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack,
        adt_database_stack,
        erp_database_stack=None,
        seed_data_stack=None,
        agentcore_stack=None,
        onboarding_stack=None,
        edge_auth_enabled: bool = False,
        external_oidc: Optional[ExternalOidcConfig] = None,
        quick_api_enabled: bool = False,
        ui_domain_name=None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - ECS Fargate (ADT app), ALB, CloudFront, S3 media", **kwargs)
        self.network_stack = network_stack
        self.adt_db = adt_database_stack
        self.erp_db = erp_database_stack
        # Opt-in demo source-system RDS (ADTSeedDataStack). None in base. This is the
        # ONLY remaining compile-time link to demo data and is the seam the connection
        # overhaul removes (sources discovered at runtime). See ADR-0030.
        self.seed_db = seed_data_stack
        self.agentcore = agentcore_stack
        # ADTOnboardingStack handle — read for the ONBOARDING_GATEWAY_URL task-def env
        # (the in-app onboarding agent federates tools from the onboarding gateway,
        # CUSTOM_JWT). Always provided by app.py; the cross-stack read creates the
        # cycle-free compute -> onboarding link.
        self.onboarding = onboarding_stack
        # Cognito Authorization@Edge gate on the app distribution (toggleable —
        # app.py resolves `edge_auth`: explicit context > sticky SSM > default
        # ON except --workshop). OFF ⇒ the synthesized template is unchanged.
        self.edge_auth_enabled = edge_auth_enabled
        # Optional external OIDC identity provider config (app.py resolves the
        # sticky oidc_* trio). KEPT for signature compatibility (app.py and
        # tests pass it) but NOT used to create the provider/client here —
        # both live in ADTAgentCoreStack (ADR-0070 amendment) so the gateway
        # CUSTOM_JWT authorizers can allow the edge client; see
        # _create_edge_auth_cognito.
        self.external_oidc = external_oidc
        # AWS Quick integration opt-in (app.py resolves quick_api_enabled).
        # Gates the Quick env vars + the
        # task-start SSM secrets below so BASE deploys reference NO
        # /adt/quick-* parameters (they need not exist) and never start the
        # query-bridge provisioner.
        self.quick_api_enabled = quick_api_enabled
        # Optional custom-domain FQDN — consumed by the edge-auth callback
        # updater so hosted-UI sign-in works through the alias too (the
        # alias/cert/DNS themselves are wired additively by _custom_domain.py).
        self.ui_domain_name = ui_domain_name
        self._build_resources()

    def _build_resources(self) -> None:
        """Resource creation sequence (order matters — see each method)."""
        # --- IoT Core ---
        self._create_iot_resources()

        # --- VPC Endpoints ---
        self._create_vpc_endpoints()

        # --- Static Media S3 Bucket ---
        self._create_media_bucket()

        # --- Cognito User Pool for ADT app login ---
        # The adt-app pool now lives in ADTAgentCoreStack (upstream) to break the
        # gateway <-> pool dependency cycle (F1 / ADR-0033). Reference it via the
        # passed agentcore_stack handle for the ECS COGNITO_* env below. The
        # handle is always provided by app.py; if it is absent the attribute
        # access raises (no fallback, per project steering).
        self.adt_user_pool = self.agentcore.adt_user_pool
        self.adt_app_client = self.agentcore.adt_app_client

        # --- Edge-auth Cognito handles (domain + OAuth client + optional
        # external IdP — all owned upstream by ADTAgentCoreStack, read via the
        # handle). Read BEFORE the ECS service so the task-def env can
        # reference the edge client id; the SAR app and the distribution
        # wiring follow inside _create_cloudfront.
        if self.edge_auth_enabled:
            self._create_edge_auth_cognito()

        # --- Default Bedrock Guardrail (applied to model-generated content) ---
        self._create_guardrail()

        # --- ECS Fargate ---
        self._create_ecs_service()

        # The x-quick-auth token: on --quick-api it is generated here, in
        # Secrets Manager, and QuickApiGatewayStack injects it through a
        # dynamic reference. The connector path asks for the same secret.
        if self.quick_api_enabled:
            self.quick_gateway_token_secret()

    def quick_gateway_token_secret(self) -> secretsmanager.ISecret:
        """The x-quick-auth shared secret, generated per account (created once).

        CloudFormation generates the value, so no template holds it. The app
        task reads it at start (``QUICK_GATEWAY_TOKEN``), and the query-bridge
        runtime gets its ARN (``wire_quick_bridge_token_ref``). No fixed name:
        the task references it by ARN, and a generated name stays outside the
        adt-* prefix the workshop participant policy can read. The generator
        uses letters and digits only, so the value can sit inside the quoted
        static header value of an API Gateway integration.
        """
        existing = self.node.try_find_child("QuickGatewayTokenSecret")
        if existing is not None:
            return existing
        token = secretsmanager.Secret(
            self, "QuickGatewayTokenSecret",
            description=(
                "x-quick-auth shared secret: the Quick query-bridge runtime and the "
                "Quick API Gateway present it to the ADT app's async ask endpoints "
                "(generated per account)"),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=48, exclude_punctuation=True, include_space=False),
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(token, [NagPackSuppression(
            id="AwsSolutions-SMG4",
            reason="Demo sample, no automatic rotation: a generated per-account shared "
                   "secret read at task start; rotate by replacing the secret and "
                   "restarting the task.")])
        container = self.node.find_child("AdtTaskDef").default_container
        container.add_secret("QUICK_GATEWAY_TOKEN", ecs.Secret.from_secrets_manager(token))
        self.wire_quick_bridge_token_ref(token.secret_arn, "secretsmanager:GetSecretValue")
        return token

    def wire_quick_bridge_token_ref(self, ref_arn: str, action: str) -> None:
        """Hand the query-bridge runtime the x-quick-auth token BY REFERENCE.

        The app task gets ``QUICK_GATEWAY_TOKEN_REF`` = *ref_arn* (plain env:
        an ARN is not a secret) and passes it to the bridge runtime instead of
        the token (agents/runtime_provision_helpers._query_bridge_env). The
        bridge reads the value with the AgentCore runtime role, so that role
        gets *action* on exactly *ref_arn* and nothing else. The role is
        shared by every ADT runtime (orchestrator, system agents, the bridge);
        there is no bridge-only role. Still narrower than before: the token
        was readable by any caller of GetAgentRuntime.

        A standalone policy in THIS stack attached to the upstream role, like
        RuntimeDatasetRefWrite: an inline grant would push this stack's ARN
        into ADTAgentCoreStack and create a dependency cycle. Called once, by
        quick_gateway_token_secret; a second call fails the synth on the
        duplicate construct id.
        """
        container = self.node.find_child("AdtTaskDef").default_container
        container.add_environment(QUICK_GATEWAY_TOKEN_REF_ENV, ref_arn)
        iam.Policy(
            self, "RuntimeQuickGatewayTokenRead",
            roles=[self.agentcore.runtime_role],
            statements=[iam.PolicyStatement(actions=[action], resources=[ref_arn])],
        )

    # ------------------------------------------------------------------
    # Cognito Authorization@Edge (toggleable)
    #
    # Browser sign-in happens at the CloudFront edge: the first unauthenticated
    # request is REDIRECTED TO THE COGNITO HOSTED UI by the shared
    # Authorization@Edge facade, so the application never serves its own
    # credential form. This is the SAME pattern as the ERP dashboard
    # (erp_dashboard_stack.py) and the HighByte UI (highbyte_opcua_stack.py):
    # one authentication boundary for every browser surface. /api/* and
    # /health bypass CheckAuth by
    # design: APIs keep their own 401 auth (a 401 is already an unambiguous
    # boundary), so the CLI eval/smoke lanes (POST /api/auth/login) and the
    # SSE stream keep working unchanged.
    # ------------------------------------------------------------------
    def _create_edge_auth_cognito(self) -> None:
        """Read the edge-auth Cognito handles owned upstream by ADTAgentCoreStack.

        The pool, its hosted-UI domain, the edge OAuth client AND the optional
        external IdP all live upstream in ADTAgentCoreStack. The client + IdP
        were relocated there (2026-09-23, ADR-0070 amendment): the query and
        onboarding gateways' CUSTOM_JWT authorizers must list the edge client
        in allowed_clients (the forwarded end-user bearer per ADR-0049 is an
        edge-client token under edge auth), and referencing the client id from
        THIS downstream stack would be a backward cross-stack reference (a
        cycle) — the same reason the pool itself moved (F1 / ADR-0033). Same
        handle-read pattern as ``self.adt_user_pool`` above; the SAR app,
        the callback updater and the EDGE_AUTH_CLIENT_ID task env below now
        consume the cross-stack client id (a CFN export — this stack already
        depends on ADTAgentCoreStack). The existing adt-app-client
        (USER_PASSWORD_AUTH — /api/auth/login and the CLI eval lanes) is
        deliberately untouched.
        """
        # References only — all owned by ADTAgentCoreStack.
        self.edge_auth_domain = self.agentcore.adt_app_domain
        self.edge_external_idp = self.agentcore.edge_external_idp
        self.edge_auth_client = self.agentcore.edge_auth_client

    @property
    def edge_headers(self) -> str | None:
        """The SAR HttpHeaders JSON (edge auth on); the bare-callback WAF
        redirect carries the same security headers."""
        if not self.edge_auth_enabled:
            return None
        return edge_http_headers(allow_websockets=False)

    def _create_edge_auth(self) -> None:
        """Deploy the shared auth-only SAR application and edge versions."""
        self.edge_auth, self.edge_versions = deploy_edge_auth(
            self,
            user_pool=self.adt_user_pool,
            client=self.edge_auth_client,
            http_headers=self.edge_headers,
        )

    def _create_edge_callback_updater(self) -> None:
        """Patch the OAuth callbacks after the distribution token resolves."""
        create_callback_updater(
            self,
            function_name="adt-app-callback-updater",
            user_pool=self.adt_user_pool,
            client=self.edge_auth_client,
            distribution=self.distribution,
            custom_domain=self.ui_domain_name,
        )

    # ------------------------------------------------------------------
    def _create_media_bucket(self) -> None:
        """Private S3 buckets for generated media (AI video briefings) and demo
        forensics (stream captures), served via presigned URLs."""
        # Video briefing bucket — stores AI-generated MP4 briefings (presigned URL access)
        self.video_bucket = s3.Bucket(
            self, "AdtVideoBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,  # Adds an aws:SecureTransport-deny bucket policy (TLS-only).
            versioned=True,  # Scanner baseline (CKV_AWS_21); noncurrent versions expire below.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-briefings",
                    prefix="briefings/",
                    expiration=Duration.days(7),  # Auto-delete generated videos after 7 days
                ),
                s3.LifecycleRule(
                    id="expire-noncurrent-versions",
                    noncurrent_version_expiration=Duration.days(7),
                ),
            ],
        )
        # Checkov CKV_AWS_18 (S3 access logging): demo-grade transient buckets
        # (7-day expiry) — the access-logging chain is deliberately omitted.
        self.video_bucket.node.default_child.add_metadata("checkov", {"skip": [
            {"id": "CKV_AWS_18", "comment": "demo-grade bucket, 7-day lifecycle expiry; access logging deliberately omitted"},
        ]})

        # Stream-capture bucket — DEMO FORENSICS ONLY. chat_client.py writes each
        # query's raw AgentCore SSE capture (a /tmp jsonl of every wire line) here
        # when STREAM_CAPTURE_BUCKET is set, so streaming incidents (e.g. the
        # 2026-07-16 iter_lines 1KB-buffering delay, diagnosed from these captures)
        # can be analyzed offline from the raw envelopes. Deliberately small and
        # self-cleaning: nothing here is a system of record — objects expire after
        # 7 days and the bucket auto-empties + deletes on stack destroy.
        self.stream_capture_bucket = s3.Bucket(
            self, "AdtStreamCaptureBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,  # Adds an aws:SecureTransport-deny bucket policy (TLS-only).
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # CDK's auto-delete handler removes all versions too.
            versioned=True,  # Scanner baseline (CKV_AWS_21); noncurrent versions expire below.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-stream-captures",
                    expiration=Duration.days(7),  # forensics window only
                    noncurrent_version_expiration=Duration.days(7),
                )
            ],
        )
        self.stream_capture_bucket.node.default_child.add_metadata("checkov", {"skip": [
            {"id": "CKV_AWS_18", "comment": "demo forensics bucket, 7-day expiry + destroy-on-teardown; access logging deliberately omitted"},
        ]})
        self._grant_dataset_ref_writes()

    def _grant_dataset_ref_writes(self) -> None:
        """Wave 4 claim-check redemption (agents/dataset_store.py): the
        per-system AgentCore runtimes persist withheld telemetry series under
        dataset-refs/ on the stream-capture bucket at cap time; the ECS app
        (which already holds Get/PutObject via _media_bucket_policy) reads
        them back for the admin-gated Tier-3 redemption route. Write-only and
        prefix-scoped for the runtime role; the bucket's 7-day lifecycle rule
        is the physical deletion horizon (the store enforces a deliberate
        24 h read TTL on top). The policy is a standalone resource in THIS
        stack (attached to the upstream role) — an inline
        add_to_principal_policy would push this stack's bucket ARN into
        ADTAgentCoreStack and create a dependency cycle. None-safe:
        agentcore is optional."""
        if self.agentcore is None:
            return
        iam.Policy(
            self, "RuntimeDatasetRefWrite",
            roles=[self.agentcore.runtime_role],
            statements=[
                iam.PolicyStatement(
                    actions=["s3:PutObject"],
                    resources=[
                        self.stream_capture_bucket.bucket_arn + "/dataset-refs/*",
                    ],
                )
            ],
        )

    # ------------------------------------------------------------------
    def _create_guardrail(self) -> None:
        """Provision a minimal default Amazon Bedrock Guardrail for this sample.

        The guardrail applies standard content filters plus a prompt-attack filter so
        the model-generated path has a sensible default safety net rather than relying
        solely on the bring-your-own-guardrail config. Its id/version are surfaced to
        the app via the GUARDRAIL_ID / GUARDRAIL_VERSION container environment variables
        (see _create_ecs_service) and as stack outputs so the AgentCore orchestrator/
        system-agent runtimes (whose env is wired by deploy.sh) can adopt the same id.

        Adopters who prefer their own guardrail can override GUARDRAIL_ID at runtime.
        """
        self.guardrail = bedrock.CfnGuardrail(
            self, "DefaultGuardrail",
            name="adt-default",
            description="Default content + prompt-attack guardrail for the ADT sample.",
            blocked_input_messaging="This request was blocked by the content guardrail.",
            blocked_outputs_messaging="The response was blocked by the content guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=ftype, input_strength="MEDIUM", output_strength="MEDIUM",
                    )
                    for ftype in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")
                ] + [
                    # PROMPT_ATTACK is only evaluated on input; output_strength must be NONE.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE",
                    ),
                ],
            ),
        )

        CfnOutput(self, "DefaultGuardrailId", value=self.guardrail.attr_guardrail_id)
        CfnOutput(self, "DefaultGuardrailVersion", value=self.guardrail.attr_version)

    # ------------------------------------------------------------------
    def _create_iot_resources(self) -> None:
        """IoT Core policy + endpoint lookup (from the original iot-stack.ts)."""
        iot.CfnPolicy(
            self, "AdtMqttPolicy",
            policy_name="AdtMqttPolicy",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "iot:Connect",
                        "Resource": f"arn:aws:iot:{self.region}:{self.account}:client/adt-*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Publish", "iot:Receive"],
                        "Resource": f"arn:aws:iot:{self.region}:{self.account}:topic/Adt/*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": "iot:Subscribe",
                        "Resource": f"arn:aws:iot:{self.region}:{self.account}:topicfilter/Adt/*",
                    },
                ],
            },
        )

        # describeEndpoint is idempotent and returns the same ATS endpoint for
        # the account/region. The call is wired to BOTH on_create and on_update:
        # without on_update, a stack update sends the custom resource an Update
        # event for which it returns no data, so `endpointAddress` disappears and
        # any resource referencing it (the ECS task def) fails with
        # "Vendor response doesn't contain endpointAddress attribute".
        _iot_endpoint_call = cr.AwsSdkCall(
            service="Iot",
            action="describeEndpoint",
            parameters={"endpointType": "iot:Data-ATS"},
            physical_resource_id=cr.PhysicalResourceId.of("IotEndpoint"),
        )
        endpoint_lookup = cr.AwsCustomResource(
            self, "IotEndpoint",
            on_create=_iot_endpoint_call,
            on_update=_iot_endpoint_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
            ),
            install_latest_aws_sdk=False,  # only built-in iot:describeEndpoint; avoids deploy-time npm + non-reproducible builds
        )
        self.iot_endpoint = endpoint_lookup.get_response_field("endpointAddress")

    # ------------------------------------------------------------------
    def _create_vpc_endpoints(self) -> None:
        """VPC endpoints for ECR, S3, Secrets Manager, Neptune Analytics.

        Workshop mode: every endpoint is declared here AND in ADTNetworkStack,
        each copy under a condition on the ``BuildPathEndpoints`` parameter
        (stacks/_build_path_endpoints.py). A fresh Workshop Studio create
        passes ``network-all``: all of them exist with the VPC, before the
        root's VPC CodeBuild and before any child's VPC workload. A stack
        created before a move holds the moved endpoints here and is updated
        with the value it was created with (``compute`` or ``network``). This
        copy keeps its construct ids, so such a stack sees no change, and keeps
        its references to the Network route tables, so Network keeps the
        exports it imports. Standalone deploys create them here
        unconditionally."""
        vpc = self.network_stack.vpc

        if self._with_workshop():
            _build_path_endpoints.add_build_path_endpoints(
                self, vpc, _build_path_endpoints.COMPUTE)
            _build_path_endpoints.add_service_endpoints(
                self, vpc, _build_path_endpoints.COMPUTE)
            return

        ec2.InterfaceVpcEndpoint(self, "EcrApi", vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))

        ec2.InterfaceVpcEndpoint(self, "EcrDkr", vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))

        ec2.GatewayVpcEndpoint(self, "S3Endpoint", vpc=vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3)

        # Secrets Manager + the two Neptune Analytics endpoints (private DNS).
        _build_path_endpoints.service_endpoints(self, vpc)

        # Bedrock VPC endpoint removed — Sonnet 4.x requires inference profiles
        # which are not supported through VPC endpoints. Traffic goes via NAT gateway.

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _create_ecs_service(self) -> None:
        """ECS Fargate service for the ADT app (from the original ecs-stack.ts)."""
        vpc = self.network_stack.vpc
        graph_id = getattr(self.adt_db, "graph_id", "adt")
        # Bedrock model ID for the orchestrator/app. Sourced from cdk.json context
        # ('bedrock_model'); falls back to the runtime-env contract's default.
        # A single source is shared by the ECS app env and the orchestrator
        # CfnRuntime so both always agree on the model.
        bedrock_model = (self.node.try_get_context("bedrock_model")
                         or _model_allowlist.default_bedrock_model(self.region))
        self.bedrock_model = bedrock_model  # BedrockModel output (workshop CR)
        # System-agent model: '-c system_agent_model' (the workshop pins Haiku
        # 4.5, stacks/_workshop_models.py), else the contract's default.
        self.system_agent_model = (
            self.node.try_get_context("system_agent_model")
            or _model_allowlist.default_system_agent_model(self.region))
        runtime_asset = self._build_runtime_asset()

        cluster = ecs.Cluster(self, "AdtCluster", vpc=vpc,
            container_insights=True)

        task_role = self._build_app_task_role(graph_id)
        self._grant_cross_account_access(task_role)

        token_secrets = self._import_token_secrets()
        task_def = self._build_task_definition(task_role, token_secrets)
        container = self._add_app_container(
            task_def, graph_id, bedrock_model, runtime_asset, token_secrets)

        alb, alb_sg = self._build_internal_alb(vpc)
        svc_sg, agentcore_sg = self._build_service_security_groups(vpc)

        service = ecs.FargateService(self, "AdtService",
            cluster=cluster, task_definition=task_def, desired_count=1,
            assign_public_ip=False,
            health_check_grace_period=Duration.seconds(300),
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[svc_sg])

        service.connections.allow_from(alb, ec2.Port.tcp(8000))

        self._attach_alb_target(vpc, alb, service)
        self._wire_runtime_networking(container, vpc, alb, agentcore_sg)

        self.alb_dns_name = alb.load_balancer_dns_name
        self.alb_arn = alb.load_balancer_arn
        self.alb = alb  # exposed for app.py's scanner-skip annotations

        self._create_cloudfront(alb, alb_sg)
        self._emit_compute_outputs(vpc, agentcore_sg, runtime_asset)

    def _build_runtime_asset(self):
        """Agent runtime image (Dockerfile.runtime) — built and published by CDK
        as a DockerImageAsset.

        The SAME image backs the orchestrator CfnRuntime (created below) and the
        per-system agent runtimes that the app provisions at run time
        (agent_manager.py reads AGENTCORE_ECR_IMAGE). The AgentCore runtime role
        is granted pull on the asset's ECR repository so those runtimes can start.
        """
        if not self.agentcore:
            return None
        runtime_asset = ecr_assets.DockerImageAsset(
            self, "OrchestratorRuntimeImage",
            directory=os.path.join(os.path.dirname(__file__), "..", "agentic_digital_thread"),
            file="Dockerfile.runtime",
            # Only the app image copies static/ (the i3X Explorer bundle).
            # Without this, a rebuilt bundle gives this image a new tag.
            exclude=["static/"],
        )
        runtime_asset.repository.grant_pull(self.agentcore.runtime_role)
        return runtime_asset

    def _build_app_task_role(self, graph_id):
        """Task role with Bedrock, Neptune, Secrets Manager, IoT permissions."""
        role = iam.Role(self, "AdtTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        self._attach_task_role_managed_policies(role, {
                **self._bedrock_neptune_metrics_policies(graph_id),
                **self._secrets_policies(),
                **self._reader_secrets_policy(),
                **self._iot_seed_dynamodb_policies(),
                **self._system_guidance_dynamodb_policy(),
                **self._usage_dynamodb_policy(),
                **self._media_bucket_policy(),
                **self._agentcore_policy(),
                **self._conversations_memory_policy(),
                **self._memory_records_policy(),
                **self._story_clock_policy(),
                # Cognito: ADT app user pool — group lookup for the admin
                # check, and ListUsers for the admin Conversations page, which
                # shows each memory actor id (a Cognito sub) as its login.
                "CognitoAppAuth": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=[
                            "cognito-idp:AdminListGroupsForUser",
                            "cognito-idp:AdminGetUser",
                            "cognito-idp:ListUsers",
                        ],
                        resources=[self.adt_user_pool.user_pool_arn],
                    ),
                ]),
            })
        return role

    def _attach_task_role_managed_policies(self, role, documents: dict) -> None:
        """Attach *documents* to *role* as customer managed policies, in order,
        packed so each stays under IAM's 6,144-character managed-policy limit.

        IAM caps a role's inline policies at 10,240 characters in total, which
        this role outgrew (tests/test_compute_task_role_policy_size.py). The
        statements are unchanged; only where they live moves. A document is
        never split. The weight is the document resolved in this stack, which
        undercounts references to other stacks (they synthesize as longer
        Fn::ImportValue), so the budget leaves wide room under the limit.
        """
        budget = 3_500
        stack = Stack.of(self)
        batches: list[list] = [[]]
        size = 0
        for name, doc in documents.items():
            body = doc.to_json()
            # A statement without a Sid gets "<former policy name><index>", so
            # the console and tests still show which policy a grant came from.
            for index, statement in enumerate(body["Statement"]):
                statement.setdefault("Sid", f"{name}{index}")
            # Round-trip each statement: the document exposes no statement list.
            statements = [iam.PolicyStatement.from_json(s) for s in body["Statement"]]
            weight = len(json.dumps(stack.resolve(body), separators=(",", ":")))
            if batches[-1] and size + weight > budget:
                batches.append([])
                size = 0
            batches[-1].extend(statements)
            size += weight
        # Attached through the role's ManagedPolicyArns, so CloudFormation
        # creates every policy before the role update that drops the old
        # inline policies. shortcut: that one role update is not atomic in
        # IAM; a stack holding the old inline grants may see a moment without
        # them. A two-deploy migration would close it; fresh deploys don't need it.
        for index, statements in enumerate(batches, start=1):
            role.add_managed_policy(iam.ManagedPolicy(
                self, f"AdtTaskPolicy{index}", document=iam.PolicyDocument(statements=statements)))

    def _bedrock_neptune_metrics_policies(self, graph_id):
        """Bedrock invoke/profile, Neptune graph, CloudWatch metric read policies."""
        return {
            "Bedrock": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    # Scope InvokeModel to foundation models plus this account/region's
                    # inference profiles. The app uses cross-Region inference profiles
                    # (global. by default; us./eu. when chosen) which route to
                    # foundation models. Foundation-model ARNs carry no account, and a
                    # global profile is also checked against the Region-less
                    # arn:aws:bedrock:::foundation-model/<model>, which the Region
                    # wildcard matches; hence *::foundation-model/*.
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=[
                        "arn:aws:bedrock:*::foundation-model/*",
                        f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                    ],
                ),
                # First Anthropic invoke in a fresh account auto-subscribes via
                # Marketplace and needs these (see stacks/_bedrock_access.py).
                bedrock_marketplace_auto_subscribe_statement(),
                *mantle_inference_statements(self),
                iam.PolicyStatement(
                    # The admin/model UI enumerates inference profiles to filter the
                    # selectable model list (agents/admin_routes.list_inference_profiles).
                    # Without this the app logs an AccessDenied and simply shows an
                    # unfiltered list. List/Get profile actions are scoped to this
                    # account/region's inference-profile ARNs.
                    actions=["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                    ],
                ),
            ]),
            "Neptune": iam.PolicyDocument(statements=[
                # Full read+write tier on the specific graph (the app runs
                # onboarding/offboarding and self-learning writes in-process).
                # ONE helper home for every graph-touching role (ADR-0069).
                graph_data_plane_statement(
                    region=self.region, account=self.account,
                    graph_id=graph_id, delete=True),
            ]),
            "CloudWatchMetrics": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    # CloudWatch metric read actions (GetMetricStatistics, ListMetrics,
                    # GetMetricData) do not support resource-level permissions, so they
                    # must be granted on "*".
                    actions=["cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics", "cloudwatch:GetMetricData"],
                    resources=["*"],
                ),
            ]),
        }

    def _delegated_identity_key_resources(self) -> list:
        """ADR-0079: the identity-claim signing-key ARN pattern, or []."""
        if getattr(self.agentcore, "quick_oidc", None) is None:
            return []
        from stacks._quick_gateway_auth import identity_claim_key_arn_pattern
        return [identity_claim_key_arn_pattern(self)]

    def _reader_secret_arns(self) -> list:
        """Every system-agent reader secret of the deployed demo databases."""
        arns: list = []
        for database in (self.seed_db, self.erp_db):
            readers = getattr(database, "system_readers", None) if database else None
            if readers is not None:
                arns.extend(readers.secret_arns().values())
        return arns

    def _reader_secrets_policy(self) -> dict:
        """Read on the system agents' read-only login secrets
        (stacks/_db_readers.py), listed by ARN: the app copies the login for
        a system's schema into that system's agent-config secret when it
        starts it. Absent without demo databases."""
        arns = self._reader_secret_arns()
        if not arns:
            return {}
        return {"SystemReaderSecrets": iam.PolicyDocument(statements=[
            iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=arns),
        ])}

    def _env_reader_secrets(self) -> dict:
        """``{schema: reader secret ARN}`` per demo database, as JSON, read by
        agents/demo/db_routing.py (``system_agent_reader_login``)."""
        env: dict = {}
        for name, database in (("DB_READER_SECRET_ARNS", self.seed_db),
                               ("ERP_DB_READER_SECRET_ARNS", self.erp_db)):
            readers = getattr(database, "system_readers", None) if database else None
            if readers is not None:
                env[name] = Stack.of(self).to_json_string(readers.secret_arns())
        return env

    def _secrets_policies(self):
        """Secrets Manager read/write policy for the app task role."""
        return {
            "Secrets": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=[
                        "secretsmanager:GetSecretValue",
                    ],
                    resources=[
                        *([self.seed_db.db_secret.secret_arn] if self.seed_db else []),
                        *([self.erp_db.db_secret.secret_arn] if self.erp_db else []),
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt-demo-password*",
                        # Node-only connection resolution (ADR 0008): the app reads
                        # per-system connection secrets (adt/conn/<id>,
                        # e.g. conn/highbyte) by the server-determined secret_arn on
                        # the System node. Scoped to conn/* (least-privilege).
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt/conn/*",
                        # Per-system credentials (adt/systems/<id>) the app
                        # writes itself: the onboarding chat's store_credentials
                        # and the Onboard view's credentials route. Read back by
                        # the app's provision and connection resolution.
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt/systems/*",
                        # Dashboard metric resolution (ADR 0015): the app-side
                        # resolver reads the per-system connection secret
                        # (adt/agent-config/<id>) to obtain the
                        # cross-account role_arn/external_id so it queries the
                        # correct account for Athena/SiteWise cards. Scoped to
                        # agent-config/* (least-privilege).
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt/agent-config/*",
                        # ADR-0079: ONLY when the Quick gateway accepts
                        # delegated user tokens — the app verifies the
                        # interceptor-minted identity claim with the signing
                        # key (read BY NAME). Absent ⇒ template unchanged.
                        *self._delegated_identity_key_resources(),
                    ],
                ),
                *self._secret_write_statements(),
            ]),
        }

    def _secret_write_statements(self):
        """The app task role's Secrets Manager write, stage-move and delete
        statements, each scoped to the per-system prefixes the app writes."""
        return [
                # The secrets the app creates and overwrites, by prefix, in
                # this account and Region: a registration's own connection
                # secret (adt/conn/<id>), a system's credentials
                # (adt/systems/<id>, the onboarding chat's store_credentials
                # and the Onboard view's credentials route; the onboarding
                # runtime does not write them) and a system agent's config
                # (adt/agent-config/<id>). Not every adt-named secret.
                iam.PolicyStatement(
                    actions=[
                        "secretsmanager:PutSecretValue",
                        "secretsmanager:CreateSecret",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{prefix}"
                        for prefix in ("adt/conn/*", "adt/systems/*", "adt/agent-config/*")
                    ],
                ),
                # Compare-and-set promotion of a pending version and rollback of
                # AWSCURRENT to a baseline (agent_manager._cas_put_payload and
                # _restore_baseline, connection_resolver's own-secret rollback).
                iam.PolicyStatement(
                    actions=["secretsmanager:UpdateSecretVersionStage"],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{prefix}"
                        for prefix in ("adt/conn/*", "adt/agent-config/*")
                    ],
                ),
                # Offboard (full removal) of a USER Connect New System
                # registration schedules deletion of ITS OWN adt/conn/<id>
                # secret, always with a recovery window
                # (connection_resolver.schedule_own_secret_deletion); a later
                # registration of the same id inside that window restores it
                # before writing its own payload (_store_connection_secret).
                # Scoped to the per-system connection prefix only; the app
                # never deletes any other secret.
                iam.PolicyStatement(
                    actions=["secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret"],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt/conn/*",
                    ],
                ),
        ]

    def _iot_seed_dynamodb_policies(self):
        """IoT Core pub/sub, seed-Lambda invoke, and DynamoDB table policies."""
        return {
            "IoT": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["iot:Connect"],
                    resources=[f"arn:aws:iot:{self.region}:{self.account}:client/adt-*"],
                ),
                iam.PolicyStatement(
                    actions=["iot:Publish", "iot:Receive"],
                    resources=[f"arn:aws:iot:{self.region}:{self.account}:topic/Adt/*"],
                ),
                iam.PolicyStatement(
                    actions=["iot:Subscribe"],
                    resources=[f"arn:aws:iot:{self.region}:{self.account}:topicfilter/Adt/*"],
                ),
            ]),
            "SeedLambda": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:adt-seed-data"],
                ),
            ]),
            "DynamoDB": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=[
                        "dynamodb:PutItem",
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                        "dynamodb:UpdateItem",
                        "dynamodb:Scan",
                        # DeleteItem: the self-learning reconcile (purge of proposals
                        # that reference a column absent from the onboarded schema)
                        # and the clear_rejected / clear_all_learnings admin helpers
                        # all delete rows from adt-proposed-mappings.
                        "dynamodb:DeleteItem",
                    ],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-proposed-mappings",
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-proposed-mappings/index/*",
                        # Prompt Library + dashboard-card persistence (ADTDatabaseStack).
                        # get_by_prompt uses a scan filter (no GSI), so no index ARN.
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-prompt-templates",
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-dashboard-cards",
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-onboarding-sessions",
                    ],
                ),
            ]),
            **self._feedback_dynamodb_policy(),
        }

    def _with_workshop(self) -> bool:
        return str(self.node.try_get_context("with_workshop")).lower() == "true"

    def _is_live_story(self) -> bool:
        return self._with_workshop() and _workshop_story.from_node(self.node) == "live"

    def _story_clock_policy(self):
        """Live workshop story only: the steward clock placement POST invokes
        the ERP story-history Lambda (RequestResponse). Named by the ERP
        stack's constant, not a cross-stack export (live -> frozen must be
        able to drop it). Absent otherwise, so the template is unchanged."""
        if not self._is_live_story():
            return {}
        return {
            "StoryClockLambda": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[
                        f"arn:{self.partition}:lambda:{self.region}:{self.account}:"
                        f"function:{STORY_HISTORY_FUNCTION}",
                    ],
                ),
            ]),
        }

    def _system_guidance_dynamodb_policy(self):
        """App owns exact delivery writes and bounded reconciliation scans."""
        return {
            "DynamoDBSystemGuidance": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:Scan",
                    ],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:"
                        "table/adt-system-guidance",
                    ],
                ),
            ]),
        }

    def _usage_dynamodb_policy(self):
        """Least-privilege grant for the adt-usage table: the app writes one
        row per answered turn (``UsageStorage.store`` -> PutItem) and the
        admin dashboard reads its 7-day totals (``get_by_time_range`` ->
        Scan). The table has no index, and nothing gets, updates or deletes a
        usage row, so the grant names the one table and those two actions."""
        return {
            "DynamoDBUsage": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["dynamodb:PutItem", "dynamodb:Scan"],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-usage",
                    ],
                ),
            ]),
        }

    def _conversations_memory_policy(self):
        """The admin Conversations page's memory reads
        (``agents/admin_conversations.py``): GetMemory, ListSessions and
        ListEvents, on the app's own memory only (``memory/adt_memory-*``, the
        stack-owned name prefix ``config._discover_memory_id`` binds).
        GetMemory confirms the configured memory is the app's (ACTIVE, that
        name prefix) before a ListSessions "Actor <sub> not found" may mean a
        user who has never chatted. No ListActors: the
        page takes its users from the app user pool, never from the memory's
        actor list (thousands of automated-lane actors on a deployed memory).
        The broad AgentCore lifecycle grant (``_agentcore_core_statements``)
        already covers these actions; this names exactly what the page needs
        so the page keeps working if that grant is narrowed. The memory id is
        not imported from ADTAgentCoreStack: that export once pinned the
        memory resource and blocked replacing it (see the MEMORY_ID note in
        the task env). Present only when AgentCore (and so the memory) is
        deployed."""
        if not self.agentcore:
            return {}
        return {
            "AdminConversationsMemoryRead": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:GetMemory",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:ListEvents",
                    ],
                    resources=[
                        f"arn:{self.partition}:bedrock-agentcore:{self.region}:"
                        f"{self.account}:memory/adt_memory-*",
                    ],
                ),
            ]),
        }

    def _memory_records_policy(self):
        """memory_client reads and prunes individual memory records
        (list/get/delete_memory_record) and switches the extraction model
        (UpdateMemory, from the admin memory-model page), scoped to the app's
        own memory (``memory/adt_memory-*``). Its own policy document so it is
        not merged with the listing-reads statement, and named here rather than
        on the broad lifecycle grant so the records stay on the app prefix.
        Present only when AgentCore (and so the memory) is deployed."""
        if not self.agentcore:
            return {}
        return {
            "AdtMemoryRecords": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:ListMemoryRecords",
                        "bedrock-agentcore:GetMemoryRecord",
                        "bedrock-agentcore:DeleteMemoryRecord",
                        "bedrock-agentcore:UpdateMemory",
                    ],
                    resources=[
                        f"arn:{self.partition}:bedrock-agentcore:{self.region}:"
                        f"{self.account}:memory/adt_memory-*",
                    ],
                ),
            ]),
        }

    def _feedback_dynamodb_policy(self):
        """Least-privilege grant for the adt-feedback table.

        Deliberately SEPARATE from the shared six-action DynamoDB statement
        above: the app only writes feedback rows (``FeedbackStorage.store`` ->
        PutItem) and reads them back through the date GSI
        (``get_by_time_range`` -> Query). It never gets, scans, updates or
        deletes feedback, so it must not inherit Scan/DeleteItem/UpdateItem.
        The index ARN names the ONE index rather than ``/index/*`` for the same
        reason. No wildcard is needed, so no cdk-nag suppression applies.
        """
        return {
            "DynamoDBFeedback": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["dynamodb:PutItem", "dynamodb:Query"],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-feedback",
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-feedback/index/feedback-date-index",
                    ],
                ),
            ]),
        }

    def _media_bucket_policy(self):
        """S3 video/stream-capture bucket access + Polly synth policy."""
        return {
            "MediaBucket": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["s3:GetObject", "s3:PutObject"],
                    resources=[
                        self.video_bucket.bucket_arn + "/*",
                        # chat_client's stream-capture upload (PutObject only in
                        # practice; grouped here with the other app buckets).
                        self.stream_capture_bucket.bucket_arn + "/*",
                    ],
                ),
                iam.PolicyStatement(
                    # polly:SynthesizeSpeech does not support resource-level
                    # permissions and must be granted on "*".
                    actions=["polly:SynthesizeSpeech"],
                    resources=["*"],
                ),
            ]),
        }

    def _agentcore_policy(self):
        """AgentCore lifecycle policy — present only when AgentCore is deployed."""
        if not self.agentcore:
            return {}
        return {
            "AgentCore": iam.PolicyDocument(statements=[
                *self._agentcore_core_statements(),
                *self._agentcore_secrets_statements(),
                *self._agentcore_discovery_statements(),
                *self._agentcore_logs_network_statements(),
            ]),
        }

    def _agentcore_runtime_statements(self):
        """The enumerated bedrock-agentcore lifecycle actions the app calls."""
        return [
            # Only these five take no resource type in the Service
            # Authorization Reference, so an account+region ARN would not
            # authorize them: they go on "*".
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:ListAgentRuntimes",
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                    "bedrock-agentcore:ListAgentRuntimeVersions",
                    "bedrock-agentcore:ListMemories",
                ],
                resources=["*"],
            ),
            # The resource-addressable lifecycle actions the app code actually
            # calls (agent_manager, chat_client, agents_routes): runtime,
            # gateway-target and credential-provider verbs plus invoke. Enumerated
            # instead of the service wildcard so unrelated AgentCore capabilities
            # (payment, browser, code-interpreter, policy-engine, evaluation) are
            # not granted, and bounded to AgentCore resources in this
            # account+region. iam:PassRole for the runtime role is granted
            # separately in _agentcore_core_statements.
            iam.PolicyStatement(
                actions=list(_AGENTCORE_SCOPED_ACTIONS),
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"],
            ),
        ]

    def _agentcore_sync_token_statement(self):
        """SynchronizeGatewayTargets runs the target's OAuth handshake as the
        CALLER, so the app task needs the workload-identity and token-vault
        reads too (live-proven for the workshop engine role, which failed
        SYNCHRONIZE_UNSUCCESSFUL "not authorized to perform:
        bedrock-agentcore:GetWorkloadAccessToken" without them; the
        AgentCore-managed secret read is in _agentcore_secrets_statements)."""
        arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
        return iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                "bedrock-agentcore:GetResourceOauth2Token",
            ],
            resources=[
                f"{arn}:workload-identity-directory/default",
                f"{arn}:workload-identity-directory/default/workload-identity/*",
                f"{arn}:token-vault/default",
                f"{arn}:token-vault/default/oauth2credentialprovider/*",
            ],
        )

    def _agentcore_core_statements(self):
        """AgentCore lifecycle, PassRole, and Cognito client-secret statements."""
        return [
            *self._agentcore_runtime_statements(),
            self._agentcore_sync_token_statement(),
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                # The ONBOARDING runtime role is included so the
                # restore-runtime-auth repair can heal adt_onboarding too:
                # update_agent_runtime re-sends roleArn, which requires PassRole,
                # and without it that one runtime failed AccessDenied on every
                # repair run (observed 2026-07-30). app.py ALWAYS passes
                # onboarding_stack; the `if self.onboarding` guard mirrors the
                # ONBOARDING_GATEWAY_URL env below, which guards the same handle
                # for the same reason (the kwarg carries a None default).
                resources=[self.agentcore.runtime_role.role_arn] + (
                    [self.onboarding.runtime_role.role_arn]
                    if self.onboarding else []),
            ),
            iam.PolicyStatement(
                actions=["cognito-idp:DescribeUserPoolClient"],
                resources=[
                    # m2m_pool: reading the M2M credential-provider client secret.
                    self.agentcore.m2m_pool.user_pool_arn,
                    # adt_user_pool: reading the adt-app-service client secret when a
                    # background/scheduled flow (no user token) mints the gateway
                    # service token via _fetch_service_token.
                    self.agentcore.adt_user_pool.user_pool_arn,
                ],
            ),
        ]

    def _agentcore_secrets_statements(self):
        """AgentCore credential-provider secrets + service-linked-role statements."""
        return [
            iam.PolicyStatement(
                # CreateSecret/PutSecretValue: the app provisions the AgentCore
                # OAuth M2M credential provider at run time. GetSecretValue/
                # DescribeSecret are REQUIRED for gateway-target synchronization:
                # syncing a target reads the provider's client secret
                # (bedrock-agentcore-identity!default/oauth2/adt-m2m-*).
                # Without read access every target fails SYNCHRONIZE_UNSUCCESSFUL
                # (AgentCredentialProvider 403) and the orchestrator cannot execute
                # any system query.
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore*"],
            ),
            iam.PolicyStatement(
                # iam:CreateServiceLinkedRole is required for AgentCore to
                # provision its service-linked roles; it is idempotent and the
                # SLR path is fixed by the service, so the resource is "*" but
                # the grant is confined to AgentCore's own service principals
                # (network.bedrock-agentcore, runtime-identity.bedrock-agentcore).
                actions=["iam:CreateServiceLinkedRole"],
                resources=["*"],
                conditions={
                    "StringLike": {"iam:AWSServiceName": "*bedrock-agentcore.amazonaws.com"}
                },
            ),
            iam.PolicyStatement(
                # sts:GetCallerIdentity does not support resource-level
                # permissions and must be granted on "*".
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            ),
        ]

    def _agentcore_discovery_statements(self):
        """SiteWise asset-discovery and SSM parameter statements."""
        return [
            # SiteWise: read asset hierarchy for onboarding (creates Asset nodes in Neptune).
            # List*/Describe*/Get* asset-discovery actions do not support
            # resource-level permissions, so they are granted on "*".
            iam.PolicyStatement(
                actions=[
                    "iotsitewise:ListAssetModels",
                    "iotsitewise:ListAssets",
                    "iotsitewise:DescribeAsset",
                    "iotsitewise:DescribeAssetModel",
                    "iotsitewise:ListAssetModelProperties",
                    "iotsitewise:ListAssociatedAssets",
                    "iotsitewise:GetAssetPropertyValue",
                    "iotsitewise:BatchGetAssetPropertyValue",
                ],
                resources=["*"],
            ),
            # SSM: read orchestrator runtime ARN written by deploy.sh
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/adt/*"],
            ),
            # SSM: write settings (branding, LLM config) from Admin tab
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/adt/*"],
            ),
        ]

    def _agentcore_logs_network_statements(self):
        """CloudWatch Logs, EC2 describe, and Cognito pool-describe statements."""
        return [
            # CloudWatch Logs: read AgentCore runtime logs and run Insights queries.
            # The runtime log group names are assigned by AgentCore at run time
            # and Insights query actions span log groups, so granted on "*".
            iam.PolicyStatement(
                actions=[
                    "logs:StartQuery",
                    "logs:GetQueryResults",
                    "logs:StopQuery",
                    "logs:GetLogEvents",
                    "logs:FilterLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            ),
            # EC2: describe network interfaces (used when checking VPC config for runtimes).
            # ec2:Describe* actions do not support resource-level permissions, so
            # they are granted on "*".
            iam.PolicyStatement(
                actions=["ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs"],
                resources=["*"],
            ),
            # Cognito: describe user pool (needed for domain lookup in warmup)
            iam.PolicyStatement(
                actions=["cognito-idp:DescribeUserPool"],
                resources=[self.agentcore.m2m_pool.user_pool_arn],
            ),
        ]

    def _grant_cross_account_access(self, task_role):
        """Optional cross-account data onboarding (docs/cross-account-onboarding.md).

        When the deploy provides a `crossAccountDataRoleArn` context value, allow
        the ADT app task role to sts:AssumeRole the READ-ONLY role(s) in the data
        account(s) so onboarding can query real Athena / IoT SiteWise data
        cross-account. The value is a comma-separated ARN list or the keyword
        `convention` (naming-convention wildcard) — parsed by
        stacks/_cross_account.py, which also carries the security justification
        (the data-account trust policy + sts:ExternalId are the real gate).
        Nothing is hardcoded — it is a deploy parameter, so this is a no-op when
        unset.
        """
        grant_cross_account_assume(self, task_role)

    def _import_token_secrets(self):
        """Import optional bearer-token secrets referenced by deploy context.

        Bearer tokens are optional: deploy.sh supplies these non-secret ARNs
        only after the corresponding Secrets Manager secret exists. Raw/offline
        CDK synth and base deployments therefore do not reference absent secrets.
        """
        token_secrets: dict[str, secretsmanager.ISecret] = {}
        sitewise_token_arn = str(
            self.node.try_get_context("sitewise_i3x_token_secret_arn") or ""
        ).strip()
        if sitewise_token_arn:
            token_secrets["SITEWISE_I3X_TOKEN"] = secretsmanager.Secret.from_secret_complete_arn(
                self, "SiteWiseI3XTokenSecret", sitewise_token_arn
            )
        highbyte_token_arn = str(
            self.node.try_get_context("highbyte_i3x_token_secret_arn") or ""
        ).strip()
        if highbyte_token_arn:
            token_secrets["HIGHBYTE_I3X_TOKEN"] = secretsmanager.Secret.from_secret_complete_arn(
                self, "HighByteI3XTokenSecret", highbyte_token_arn
            )
        return token_secrets

    def _build_task_definition(self, task_role, token_secrets):
        """Fargate task definition; execution role can read the token secrets."""
        task_def = ecs.FargateTaskDefinition(self, "AdtTaskDef",
            memory_limit_mib=1024, cpu=512, task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        # ECS fetches injected secrets with the task execution role, not the app
        # task role. grant_read emits secretsmanager:GetSecretValue scoped to
        # each imported secret ARN.
        execution_role = task_def.obtain_execution_role()
        for token_secret in token_secrets.values():
            token_secret.grant_read(execution_role)
        return task_def

    def _add_app_container(self, task_def, graph_id, bedrock_model,
                           runtime_asset, token_secrets):
        """Add the adt-app container: image, env, secret injection, health check."""
        # Build container from agentic_digital_thread/ directory. The app
        # version is a RUNTIME env var (see _env_modes_and_misc) — not a
        # build-arg — so the image asset hash depends on the context only.
        adt_dir = os.path.join(os.path.dirname(__file__), "..", "agentic_digital_thread")
        container = task_def.add_container("adt-app",
            image=ecs.ContainerImage.from_asset(
                adt_dir, file="Dockerfile", **patched_image_options()),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="adt",
                log_group=logs.LogGroup(self, "AdtLogGroup",
                    retention=logs.RetentionDays.ONE_WEEK,
                    removal_policy=RemovalPolicy.DESTROY),
            ),
            environment=self._app_container_environment(
                graph_id, bedrock_model, runtime_asset),
            secrets=self._app_container_secrets(token_secrets),
            # Relaxed in step with the ALB target-group health check (see
            # AdtTargetGroup below): the old 5s/3-retry profile let a brief
            # event-loop stall kill the single task via the container check
            # even when the ALB tolerated it. 10s timeout / 5 retries rides
            # out slow-but-alive responses.
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL",
                         'python3 -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/health\')" || exit 1'],
                interval=Duration.seconds(15),
                timeout=Duration.seconds(10),
                retries=5,
            ),
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8000, protocol=ecs.Protocol.TCP))
        return container

    def _app_container_environment(self, graph_id, bedrock_model, runtime_asset):
        """The adt-app container environment (plain, non-secret values)."""
        return {
            **self._env_data_stores(graph_id),
            **self._env_features_and_models(bedrock_model),
            **self._env_agentcore_wiring(runtime_asset),
            **self._env_modes_and_misc(),
            **self._env_quick_integration(),
            **self._env_delegated_identity_key(),
        }

    def _env_delegated_identity_key(self):
        """ADR-0079: ``IDENTITY_CLAIM_KEY_READABLE=1`` exactly when the task
        role is granted read on the identity-claim signing key (decided by
        ``_delegated_identity_key_resources``, so the flag and the grant
        cannot drift). The app's by-name readers (the delegated-identity
        verifier and the onboarding agent test) read the key only when it is
        set: without the grant every read is AccessDenied. Absent ⇒ the
        template is unchanged."""
        if not self._delegated_identity_key_resources():
            return {}
        from stacks._quick_gateway_auth import IDENTITY_CLAIM_KEY_READABLE_ENV
        return {IDENTITY_CLAIM_KEY_READABLE_ENV: "1"}

    def _env_quick_integration(self):
        """AWS Quick opt-in env (empty on base deploys — quick_api_enabled
        gates it). QUICK_API_ENABLED tells the app to ensure the query-bridge
        gateway target at startup (server.py::_start_query_bridge_provisioner);
        absent, the provisioner never runs. QUICK_GATEWAY_ID/URL point that
        provisioner at the DEDICATED bridge-only gateway (`adt-quick-gw`,
        ADTAgentCoreStack._build_quick_gateway) instead of the shared
        `adt-query-gw`; agent_manager.ensure_query_bridge_target REFUSES to
        provision when they are absent (QuickGatewayNotConfiguredError) rather
        than falling back to the shared gateway, so this method must always
        emit them together with QUICK_API_ENABLED."""
        if not self.quick_api_enabled:
            return {}
        return {
            "QUICK_API_ENABLED": "true",
            **({"QUICK_GATEWAY_ID": self.agentcore.quick_gateway_id,
                "QUICK_GATEWAY_URL": self.agentcore.quick_gateway_url,
                } if self.agentcore and self.agentcore.quick_gateway_id else {}),
        }

    def _env_data_stores(self, graph_id):
        """Data-store pointers: seed DB, graph, DynamoDB tables, ERP DB, demo flag."""
        return {
            # Shared source-SQL DB (ADTSeedDataStack) is OPT-IN. When deployed
            # (--seed/--simulator) it provides the shared DB pointer env consumed
            # by connection_resolver: POSTGRES_HOST + DB_SECRET_ARN are the
            # server-side ARN/host pointers that db_secret_arn_for_schema and
            # pg_conn_params read to resolve a System node's SQL connection
            # (node-only resolution, ADR-0031). In BASE mode the stack is not
            # deployed, so the app runs with NO POSTGRES_*/DB_SECRET_ARN — SQL
            # resolution is fail-soft and sources are registered via runtime
            # onboarding instead (ADR-0030 + ADR-0031). This collapses the former
            # deploy-time POSTGRES_* "seam" into the dynamic-resolution model.
            **({"POSTGRES_HOST": self.seed_db.db_endpoint,
                "POSTGRES_PORT": "5432",
                # "adt_demo" here is the fictitious demo company's database — demo content, not engine identity (see rename plan)
                "POSTGRES_DB": "adt_demo",
                "POSTGRES_USER": "postgres",
                "DB_SECRET_ARN": self.seed_db.db_secret.secret_arn} if self.seed_db else {}),
            "GRAPH_IDENTIFIER": graph_id,
            **self._env_dynamodb_tables(),
            # ADT_DEMO is the runtime base-vs-demo signal (ADR-0019 demo-as-addon
            # seam + ADR-0030 base-default): present ONLY when the demo-data
            # stacks are deployed (--seed/--simulator wires self.seed_db). In
            # BASE mode self.seed_db is None, so ADT_DEMO is ABSENT and
            # agents.demo.is_enabled() is False — the app ships ZERO demo tiles,
            # example questions, or the "Example" button. Mirrors the POSTGRES_*
            # conditional-spread idiom above.
            **({"ADT_DEMO": "1"} if self.seed_db else {}),
            **self._env_workshop(),
            **({"ERP_POSTGRES_HOST": self.erp_db.cluster.cluster_endpoint.hostname,
                "ERP_POSTGRES_PORT": "5432",
                "ERP_POSTGRES_DB": self.erp_db.database_name,
                # Node-only resolution (ADR 0008): ERP-family schemas point at
                # the Aurora secret; expose its ARN for db_secret_arn_for_schema.
                "ERP_DB_SECRET_ARN": self.erp_db.db_secret.secret_arn,
                } if self.erp_db else {}),
            **self._env_reader_secrets(),
        }

    def _env_dynamodb_tables(self):
        """DynamoDB table-name pointers for the adt-app container.

        Every one of these is load-bearing: ``agents/config.py`` reads them and
        ``DynamoDBStorage`` silently no-ops on any that is absent, so a missing
        entry presents as a write that reports success and vanishes. Names are
        repeated as literals (not cross-stack Refs) to match the existing
        convention — a Ref would add a hard stack dependency for a constant.
        """
        return {
            # Prompt Library + dashboard-card persistence (ADTDatabaseStack).
            # Absent: POST /api/prompts succeeds but GET /api/prompts stays
            # empty, and saved cards do not survive a container restart.
            "PROMPT_TEMPLATES_TABLE_NAME": "adt-prompt-templates",
            "DASHBOARD_CARDS_TABLE_NAME": "adt-dashboard-cards",
            # User feedback persistence (ADTDatabaseStack::FEEDBACK_TABLE_NAME).
            # Absent: FeedbackStorage.store() no-oped while POST /api/feedback
            # still answered {"status": "success"}, so every thumbs-up/down was
            # discarded on every deployed environment (live gap, 2026-08-17).
            "FEEDBACK_TABLE_NAME": "adt-feedback",
            # Usage records (ADTDatabaseStack::USAGE_TABLE_NAME). Absent:
            # every chat turn's usage record went to UsageStorage(None) and the
            # admin dashboard's usage totals read nothing (deployed account,
            # 2026-10-21).
            "USAGE_TABLE_NAME": "adt-usage",
            # Durable JobStore backend signal (agents/job_store.py): absent,
            # onboarding job state AND the Tier-3 provenance-query store
            # silently degrade to per-process memory — redemption then 404s
            # across workers/replicas/restarts (live gap, 2026-08-04). Table
            # owned by ADTOnboardingStack (deployed in every data mode).
            "ONBOARDING_SESSIONS_TABLE": "adt-onboarding-sessions",
            # Durable exact delivery + reconciliation; absence selects the
            # fail-soft NoOpBackend and would make accepted delivery impossible.
            "ADT_SYSTEM_GUIDANCE_TABLE": "adt-system-guidance",
        }

    def _env_workshop(self):
        """Workshop-profile env (docs/workshop-mode.md).

        Present ONLY when deploy.sh --workshop sets the ``with_workshop``
        context (which implies --simulator → --seed, so seed_db is always
        present alongside it). ADT_WORKSHOP enables the agents/demo/workshop.py
        seam (register-only workshop systems + pinned refusal);
        ADT_MAPPING_OVERRIDES_FILE points the generic mapping-override seam
        (agents/onboard_agent.py) at the wrong-mapping fixture baked into the
        image at /app. ADT_WORKSHOP_STORY carries the synth-time
        ``workshop_story`` context (stacks/_workshop_story.py; ``frozen`` by
        default) so registration matches the storyline the data stacks ship.
        Absent (the default) every seam is inert and the template is
        byte-identical — mirrors the ADT_DEMO conditional-spread idiom. NOT
        sticky (ADR-0030). MEMORY_LTM_SCOPE=session (both stories) matches the
        orchestrator runtime's contract fragment
        (runtime_env_contract.WORKSHOP_ORCHESTRATOR_ENV): the workshop shares
        ONE login, so actor-wide long-term-memory retrieval would leak other
        attendees' chats into a fresh one; ``session`` keeps only per-chat
        summaries (agents/config.py::memory_ltm_scope).
        """
        if str(self.node.try_get_context("with_workshop")).lower() != "true":
            return {}
        return {
            "ADT_WORKSHOP": "1",
            "ADT_WORKSHOP_STORY": _workshop_story.from_node(self.node),
            "ADT_MAPPING_OVERRIDES_FILE":
                "/app/agents/demo/fixtures/workshop_mapping_overrides.json",
            "MEMORY_LTM_SCOPE": "session",
            **self._env_story_clock(),
        }

    def _env_story_clock(self):
        """Live story only: the function the clock placement POST invokes
        (paired with the ``StoryClockLambda`` task-role grant), plus the
        packaged loop length when set and the auto-place policy when set
        (ADR-0091 Design C: the UI reads it from the lookup; THIS task only).
        A loop or a policy off live raises."""
        loop = _workshop_story.loop_from_node(self.node)
        auto_place = _workshop_story.auto_place_from_node(self.node)
        if not self._is_live_story():
            return {}
        env = {"STORY_HISTORY_FUNCTION_NAME": STORY_HISTORY_FUNCTION}
        if loop is not None:
            env[_workshop_story.LOOP_ENV_NAME] = str(loop)
        if auto_place is not None:
            env[_workshop_story.AUTO_PLACE_ENV_NAME] = str(auto_place)
        return env

    def _env_features_and_models(self, bedrock_model):
        """Feature flags, guardrail, model IDs, IoT/bucket pointers."""
        # System agents: the model resolved in _create_ecs_service (context,
        # else the catalog's AGENT default as spelled in the runtime-env
        # contract).
        system_agent_model = self.system_agent_model
        return {
            # Phase 3–4 digital-thread features — enabled at the G4 deploy.
            # Default-off in code (byte-identical core); deploy enables, per each slice's ADR.
            "RESOLVER_REDERIVE": "1",   # S3.2 re-derivation ripple + card_update SSE (ADR 0005)
            "RESOLVER_COVERAGE": "1",   # S4.2 proactive coverage gating (ADR 0006)
            "RESOLVER_AFFINITY": "1",   # S4.3 affinity suggestions + onboarding hint (ADR 0007)
            # Live card updates over the existing card_update SSE channel for ANY
            # connected system (streamed sources push via mqtt_subscriber; query
            # sources refresh on a schedule). Default-off in code; enabled here.
            # Poll interval kept conservative to bound query cost on pull sources.
            "CARD_LIVE": "1",
            "CARD_LIVE_POLL_SECONDS": "180",
            "AWS_REGION": self.region,
            # The deployment's own account: connection_resolver refuses to read
            # a full-ARN secret in any other account, and refuses every one while
            # this cannot be established (env first, else one STS call).
            "AWS_ACCOUNT_ID": self.account,
            # Default Bedrock Guardrail (see _create_guardrail). config.py reads these
            # so the app's guardrail path is configured by default; GUARDRAIL_ENABLED
            # defaults to true. Override GUARDRAIL_ID to bring your own guardrail.
            "GUARDRAIL_ID": self.guardrail.attr_guardrail_id,
            "GUARDRAIL_VERSION": self.guardrail.attr_version,
            "BEDROCK_MODEL": bedrock_model,
            "SYSTEM_AGENT_MODEL": system_agent_model,
            # Optional deployment model allowlist (-c allowed_models): limits
            # what the Admin LLM settings page offers and accepts. Absent
            # unless the context is set (stacks/_model_allowlist.py).
            **_model_allowlist.allowed_models_env(
                self.node.try_get_context(_model_allowlist.CONTEXT_KEY),
                {"BEDROCK_MODEL": bedrock_model,
                 "SYSTEM_AGENT_MODEL": system_agent_model}),
            "MQTT_USE_IOT_CORE": "true",
            "IOT_ENDPOINT": self.iot_endpoint,
            # The sample-database loader exists only with ADTSeedDataStack; a
            # base deploy gets no name, so Reset All skips the reseed there.
            **({"ADT_SEED_LAMBDA": "adt-seed-data"} if self.seed_db is not None else {}),
            "VIDEO_BUCKET": self.video_bucket.bucket_name,
            # Demo-forensics stream captures (see _create_media_bucket):
            # chat_client uploads each query's raw SSE capture jsonl here.
            "STREAM_CAPTURE_BUCKET": self.stream_capture_bucket.bucket_name,
        }

    def _env_agentcore_wiring(self, runtime_asset):
        """AgentCore gateway/runtime wiring, onboarding gateway, Cognito auth."""
        return {
            **({"AGENTCORE_GATEWAY_ID": self.agentcore.gateway_id,
                "AGENTCORE_GATEWAY_URL": self.agentcore.gateway_url,
                "AGENTCORE_ECR_IMAGE": runtime_asset.image_uri,
                "AGENTCORE_RUNTIME_ROLE": self.agentcore.runtime_role.role_arn,
                "AGENTCORE_M2M_POOL_ID": self.agentcore.m2m_pool.user_pool_id,
                "AGENTCORE_M2M_CLIENT_ID": self.agentcore.m2m_client.user_pool_client_id,
                "AGENTCORE_M2M_DISCOVERY_URL": self.agentcore.cognito_discovery_url,
                # adt-app-service-v2 (client_credentials) client id, read by the
                # orchestrator to mint its background/service bearer token for
                # the CUSTOM_JWT query gateway (P5, ADR-0033). Without it,
                # non-interactive turns (warmup/scheduler/sync) cannot mint a
                # token and cloud mode fails closed. Sourced from
                # ADTAgentCoreStack's AppServiceClientId output.
                "ADT_APP_SERVICE_CLIENT_ID": self.agentcore.adt_service_client.user_pool_client_id,
                # MEMORY_ID is intentionally NOT wired here. Injecting
                # self.agentcore.memory_id created an Fn::ImportValue on the
                # ADTAgentCoreStack AdtMemory MemoryId export, which pinned the
                # memory resource and blocked replacing it. The app resolves
                # the memory by name at runtime via config._discover_memory_id
                # (see config.py / ADR-0006), so no cross-stack link is needed.
                # AGENTCORE_RUNTIME_ARN is read at container startup from SSM — not baked in at CDK synth time.
                # This avoids the chicken-and-egg problem where CDK synth runs before the runtime exists.
                # The app reads it via boto3 SSM at startup (see config.py _discover_orchestrator_arn fallback).
                } if self.agentcore else {}),
            # Onboarding gateway URL (ADTOnboardingStack output). The in-app
            # onboarding agent federates its tools from this CUSTOM_JWT gateway
            # with the forwarded user bearer token; cloud mode fails closed
            # without it (onboard_agent._get_onboard_tools). config.py reads
            # ONBOARDING_GATEWAY_URL -> has_onboarding_gateway.
            **({"ONBOARDING_GATEWAY_URL": self.onboarding.gateway.attr_gateway_url}
               if self.onboarding else {}),
            # ADT app Cognito auth — user pool created by this stack
            "COGNITO_USER_POOL_ID": self.adt_user_pool.user_pool_id,
            "COGNITO_CLIENT_ID": self.adt_app_client.user_pool_client_id,
            # Cognito Authorization@Edge (toggleable). When on, the middleware
            # accepts the SAR's CognitoIdentityServiceProvider.<clientId>.*
            # cookies (validated against the edge client id) so edge-
            # authenticated humans are not asked to log in a SECOND time, and
            # /api/auth/logout preserves the edge cookies for the SAR SignOut
            # handler + sends the frontend to /signout. Absent (edge auth off)
            # both seams are
            # inert — agents/auth/edge_session.py.
            **({"EDGE_AUTH_ENABLED": "true",
                "EDGE_AUTH_CLIENT_ID": self.edge_auth_client.user_pool_client_id,
                } if self.edge_auth_enabled else {}),
        }

    def _env_modes_and_misc(self):
        """Deploy-context toggles (query mode, self-learning) and misc pointers."""
        return {
            # System query mode (ADR-0035 A/B toggle). "agent" (default) = each
            # system runtime exposes a per-system NL query_<id> agent (Mode A);
            # "orchestrator" = generic describe_schema/run_query tools driven by
            # the orchestrator (Mode B). agent_manager reads this when provisioning
            # system runtimes; deploy.sh mirrors it into the orchestrator runtime
            # (ORCH_ENV). Mode A (agent) is the DEFAULT (ADR-0038 amendment,
            # 2026-07-14); override at deploy time with:
            #   cdk deploy -c system_query_mode=orchestrator   (Mode B)
            "SYSTEM_QUERY_MODE": str(
                self.node.try_get_context("system_query_mode") or "agent"
            ).strip().lower(),
            # Response-integrity posture (ADR-0047). Empty string ⇒ the code
            # default (report-only) applies, so existing deployments are
            # unchanged. `annotate` is the ADR's recommended posture: the
            # model-authored answer stays the transport and the capture layer
            # becomes an audit (grounding sidecar + refusal when the turn
            # captured ZERO trusted observations). `enforce` is the strict
            # ADR-0046 gate. Set at deploy time with:
            #   cdk deploy -c response_integrity_mode=annotate
            # deploy.sh threads the same value into every runtime through the
            # runtime-env contract — the flag MUST match app and agents.
            "RESPONSE_INTEGRITY_MODE": str(
                self.node.try_get_context("response_integrity_mode") or ""
            ).strip().lower(),
            # Self-learning: auto-accept high-confidence value_mappings on ingestion.
            # Default off (conservative); enable per-demo with:
            #   cdk deploy -c self_learning_auto_accept=on   (or deploy.sh --auto-accept on)
            "SELF_LEARNING_AUTO_ACCEPT": str(
                self.node.try_get_context("self_learning_auto_accept") or "off"
            ).strip().lower(),
            # Identity Center portal URL for AWS console access (Bedrock, SiteWise).
            # Override at deploy time with: cdk deploy -c identity_center_start_url=<your-url>
            "CONSOLE_START_URL": self.node.try_get_context("identity_center_start_url")
                or "",
            # DynamoDB table for self-learning proposed mappings
            "PROPOSED_MAPPINGS_TABLE": "adt-proposed-mappings",
            # App version badge (deploy.sh computes "<sha>[-dirty] · <date>" and
            # passes -c app_version). A RUNTIME env var, not an image build-arg:
            # a build-arg is part of the Docker asset hash, so the daily date
            # minted a new ~200 MB image asset + ECR push on every deploy even
            # with no code change (cdk.out grew to 21 GB). agents/server.py reads
            # the env var at import; the Dockerfile default "dev" still applies
            # to a raw `cdk deploy` without the context.
            **({"APP_VERSION": app_version} if (app_version := self.node.try_get_context("app_version")) else {}),
        }

    def _app_container_secrets(self, token_secrets):
        """Injected container secrets: bearer tokens, quick links, DB passwords."""
        return {
            **{
                name: ecs.Secret.from_secrets_manager(secret)
                for name, secret in token_secrets.items()
            },
            **self._quicklink_ssm_secrets(),
            **({"POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(
                self.seed_db.db_secret, "password",
            )} if self.seed_db else {}),
            **({"ERP_POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(
                self.erp_db.db_secret, "password",
            ),
            "ERP_POSTGRES_USER": ecs.Secret.from_secrets_manager(
                self.erp_db.db_secret, "username",
            )} if self.erp_db else {}),
        }

    def _quicklink_ssm_secrets(self):
        """Quick-link URLs resolved from SSM at TASK START (not synth time).

        Quick-link URLs (HIGHBYTE_URL, ERP_DASHBOARD_URL, SITEWISE_I3X_URL) and
        I3X_TLS_VERIFY are intentionally NOT plain environment values.
        ssm.StringParameter.value_from_lookup() resolves at SYNTH time and
        caches in cdk.context.json — every `cdk deploy` would then silently
        revert the task definition to whatever stale URL was cached
        (e.g. a long-deleted CloudFront domain), clobbering the values
        update-links.sh had written. They are injected at TASK START via
        the `secrets` block (ECS resolves SSM on every task launch), so they
        are always current and deploys cannot clobber them.

        deploy.sh seeds every parameter below before `cdk deploy` (a task
        definition that references a missing SSM parameter fails to launch);
        update-links.sh owns the values afterwards. ECS fetches these with the
        task EXECUTION role — from_ssm_parameter emits the parameter-scoped
        ssm:GetParameters grant on it automatically.
        """
        return {
            env_name: ecs.Secret.from_ssm_parameter(
                ssm.StringParameter.from_string_parameter_name(
                    self, f"QuickLink{env_name}", param_name
                )
            )
            for env_name, param_name in {
                "HIGHBYTE_URL": "/adt/highbyte-url",
                "ERP_DASHBOARD_URL": "/adt/erp-dashboard-url",
                "SITEWISE_I3X_URL": "/adt/sitewise-i3x-url",
                # "true" = verify TLS on i3X calls (secure default,
                # seeded by deploy.sh). Deployments whose i3X server
                # presents a self-signed certificate flip this SSM
                # parameter to "false" (e.g. update-links.sh with
                # I3X_TLS_VERIFY=false).
                "I3X_TLS_VERIFY": "/adt/i3x-tls-verify",
                # AWS Quick integration (opt-in — quick_api_enabled ONLY;
                # deploy.sh --quick-api seeds both parameters, so base deploys
                # never reference them and they need not exist):
                #   * QUICK_DESKTOP_URL — an optional Quick desktop URL for the
                #     admin-panel tile ("placeholder" = tile hidden).
                # The x-quick-auth token is a Secrets Manager secret
                # (quick_gateway_token_secret), not one of these parameters.
                **({"QUICK_DESKTOP_URL": "/adt/quick-desktop-url",
                    } if self.quick_api_enabled else {}),
            }.items()
        }

    def _build_internal_alb(self, vpc):
        """Internal ALB + its security group (CloudFront VPC-origin only)."""
        alb = elbv2.ApplicationLoadBalancer(self, "AdtALB",
            vpc=vpc, internet_facing=False,
            idle_timeout=Duration.seconds(120),
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))
        # Drop requests carrying headers with invalid characters before they
        # reach the app (scanner baseline CKV_AWS_131; no legitimate client
        # sends such headers).
        alb.set_attribute("routing.http.drop_invalid_header_fields.enabled", "true")

        alb_sg = ec2.SecurityGroup(self, "AdtAlbSg", vpc=vpc,
            description="Internal ALB for the Agentic Digital Thread sample", allow_all_outbound=True)
        # No broad :80 ingress here. The internal ALB is reachable ONLY from the
        # CloudFront VPC-origin service-managed SG (`CloudFront-VPCOrigins-Service-SG`),
        # added as a standalone CfnSecurityGroupIngress in _create_cloudfront after
        # the VPC origin (and thus the service SG) exists. The listener below uses
        # open=False so CDK does not auto-add a 0.0.0.0/0 :80 rule to either ALB SG.
        alb.add_security_group(alb_sg)
        return alb, alb_sg

    def _build_service_security_groups(self, vpc):
        """ECS service SG + the separate AgentCore-runtime SG."""
        svc_sg = ec2.SecurityGroup(self, "AdtSvcSg", vpc=vpc,
            description="ADT ECS Service", allow_all_outbound=True)

        # Separate SG for AgentCore runtimes — their ENIs are AWS-managed and
        # can take 5+ minutes to release after runtime deletion. Keeping them
        # out of the ECS service SG prevents DELETE_FAILED on stack teardown.
        agentcore_sg = ec2.SecurityGroup(self, "AgentCoreRuntimeSg", vpc=vpc,
            description="AgentCore Runtime ENIs (separate from ECS to avoid delete conflicts)",
            allow_all_outbound=True)
        # AgentCore runtimes need to reach RDS (Postgres 5432) and the ALB (8000)
        agentcore_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443))
        svc_sg.add_ingress_rule(agentcore_sg, ec2.Port.tcp(8000), "AgentCore to ALB")
        return svc_sg, agentcore_sg

    def _attach_alb_target(self, vpc, alb, service):
        """Target group + :80 listener binding the service to the internal ALB."""
        # Relaxed health check (timeout 10s, 5 fails) — the previous 5s/3-fail
        # profile caused a "death loop": any transient event-loop stall in the
        # single app task tripped 3 consecutive 5s timeouts (~45s), the ALB
        # marked the task unhealthy, ECS replaced it, and each replacement was
        # a 60-100s outage (8 replacements observed in one day). Tolerating
        # slow-but-alive responses (10s < 15s interval, per ALB timeout<interval
        # rule) keeps the task in service through brief stalls.
        tg = elbv2.ApplicationTargetGroup(self, "AdtTargetGroup",
            vpc=vpc, port=8000, protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health", healthy_http_codes="200",
                interval=Duration.seconds(15), timeout=Duration.seconds(10),
                healthy_threshold_count=2, unhealthy_threshold_count=5))

        # open=False: do NOT auto-add a 0.0.0.0/0 :80 ingress to the ALB SGs.
        # Reachability is granted solely by the CloudFront VPC-origin service-SG
        # ingress added in _create_cloudfront.
        listener = alb.add_listener("AdtListener", port=80, open=False, default_target_groups=[tg])
        # Checkov CKV_AWS_2/CKV_AWS_103 (ALB HTTPS/TLS1.2): internal ALB behind
        # CloudFront — TLS terminates at the edge; the origin hop stays HTTP by
        # design (same rationale as the cdk-nag suppression).
        listener.node.default_child.add_metadata("checkov", {"skip": [
            {"id": "CKV_AWS_2", "comment": "TLS terminates at CloudFront; internal ALB origin is HTTP by design"},
            {"id": "CKV_AWS_103", "comment": "TLS terminates at CloudFront; internal ALB origin is HTTP by design"},
        ]})
        service.attach_to_application_target_group(tg)

    def _wire_runtime_networking(self, container, vpc, alb, agentcore_sg):
        """Late container env: internal ALB URL + AgentCore runtime VPC config."""
        # Set ADT_APP_INTERNAL_URL now that ALB DNS is known (CDK token resolves at deploy)
        container.add_environment("ADT_APP_INTERNAL_URL", f"http://{alb.load_balancer_dns_name}")
        # VPC config for AgentCore runtimes the APP provisions imperatively
        # (agents/agent_manager._start_cloud): the per-system agent runtimes AND
        # the built-in `digital-thread` knowledge-graph runtime. Set
        # UNCONDITIONALLY — not gated on the ERP DB — because the digital-thread
        # runtime must reach the private Neptune Analytics graph (public
        # connectivity disabled, ADR-0024) in EVERY data mode, including BASE
        # (no ERP DB). When these are absent the app falls back to PUBLIC network
        # mode (runtime_provision_helpers.network_config), and a PUBLIC runtime
        # cannot reach the private graph — get_system_schema / explore_graph then
        # fail with "Could not connect to the endpoint URL
        # https://<graph>.<region>.neptune-graph.amazonaws.com/queries". Private
        # RDS access (seed/simulator modes) rides on the same VPC config.
        # Derived from the network stack — never hardcoded.
        private_subnet_ids = ",".join(s.subnet_id for s in vpc.private_subnets)
        container.add_environment("VPC_PRIVATE_SUBNETS", private_subnet_ids)
        container.add_environment("AGENTCORE_RUNTIME_SG", agentcore_sg.security_group_id)

    def _emit_compute_outputs(self, vpc, agentcore_sg, runtime_asset):
        """Stack outputs consumed by deploy.sh (ALB, IoT, runtime networking)."""
        CfnOutput(self, "ADTAlbDnsName", value=self.alb_dns_name)
        CfnOutput(self, "ADTIotEndpoint", value=self.iot_endpoint)
        # Expose the AgentCore runtime networking so deploy.sh can create the
        # orchestrator runtime in VPC mode (it must reach the private Neptune
        # Analytics graph through the VPC interface endpoints, like the per-system
        # agent runtimes do).
        CfnOutput(self, "AgentCoreRuntimeSgId", value=agentcore_sg.security_group_id,
            description="Security group for Amazon Bedrock AgentCore runtimes (VPC mode)")
        CfnOutput(self, "AgentCoreRuntimeSubnetIds",
            value=",".join(s.subnet_id for s in vpc.private_subnets),
            description="Private subnet IDs for Amazon Bedrock AgentCore runtimes (VPC mode)")

        # ── Orchestrator AgentCore Runtime (created imperatively by deploy.sh) ──
        # The adt_orchestrator runtime is NOT created declaratively here. AgentCore
        # VPC runtimes can only launch in the AZs the service supports, and the
        # supported list is expressed as physical AZ *IDs* (e.g. use1-az1/az2/az4)
        # while CDK subnets carry AZ *names* (us-east-1a/b/…). The name→ID mapping is
        # account-specific and not resolvable at synth, so a declarative CfnRuntime
        # that pins all private subnets fails to deploy whenever the VPC includes an
        # AZ AgentCore does not support for the account (here us-east-1a = use1-az6).
        #
        # Instead the orchestrator is created imperatively by deploy.sh, which mirrors
        # the per-system agent path in agents/agent_manager.py: it attempts VPC mode
        # with all private subnets and, on a CREATE_FAILED "unsupported availability
        # zone" failure, strips the offending subnet(s) and retries (see ADR 0010).
        # That path is already proven by the 6 READY system-agent runtimes and is
        # region/account-agnostic (no AZ allowlist to rot). The networking + image are
        # surfaced as stack outputs below; deploy.sh consumes them. The app discovers
        # the orchestrator ARN by name at runtime (config._discover_orchestrator_arn).
        if self.agentcore and runtime_asset is not None:
            CfnOutput(self, "OrchestratorRuntimeImageUri",
                value=runtime_asset.image_uri,
                description="Amazon ECR image URI (Dockerfile.runtime asset) for the "
                            "adt_orchestrator Amazon Bedrock AgentCore runtime created by deploy.sh")
        # The app's BEDROCK_MODEL, for the workshop runtime-bootstrap CR: it
        # builds the orchestrator runtime env and warms the model, so both
        # sides run the same model (deploy.sh passes BEDROCK_MODEL itself).
        CfnOutput(self, "BedrockModel", value=self.bedrock_model,
            description="Orchestrator Amazon Bedrock model id (the app's BEDROCK_MODEL)")
        self._emit_model_outputs()

    def _emit_model_outputs(self):
        """The system-agent model and the model allowlist, for the workshop
        runtime-bootstrap CR: it warms the system-agent model and writes the
        allowlist onto the runtimes, so a runtime's catalog fallback stays
        inside it. ``AllowedModels`` exists only when the allowlist is set."""
        CfnOutput(self, "SystemAgentModel", value=self.system_agent_model,
            description="System-agent Amazon Bedrock model id (the app's SYSTEM_AGENT_MODEL)")
        allowlist = _model_allowlist.allowed_models_value(
            self.node.try_get_context(_model_allowlist.CONTEXT_KEY))
        if allowlist:
            CfnOutput(self, "AllowedModels", value=allowlist,
                description="Model allowlist (the app's ALLOWED_MODELS)")


    def _create_cloudfront(self, alb, alb_sg):
        """CloudFront with VPC origin for internal ALB — pass-through, no caching.

        With edge auth enabled (self.edge_auth_enabled), the SAR
        Authorization@Edge app must exist before the distribution so its
        Lambda@Edge version ARNs can ride the L2 behaviors — same ordering as
        the ERP dashboard / HighByte UI.
        """
        web_acl = self._create_web_acl()

        alb_origin = origins.VpcOrigin.with_application_load_balancer(alb,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY, http_port=80,
            read_timeout=Duration.seconds(120), keepalive_timeout=Duration.seconds(60),
            vpc_origin_name=f"adt-alb-origin-{self.region}")

        if self.edge_auth_enabled:
            self._create_edge_auth()
        default_behavior, additional_behaviors = self._distribution_behaviors(alb_origin)

        self.distribution = distribution = cloudfront.Distribution(self, "AdtDistribution",
            web_acl_id=web_acl.attr_arn,
            default_behavior=default_behavior,
            additional_behaviors=additional_behaviors)
        if self.edge_auth_enabled:
            # Keep SAR dependency ordering: the distribution's Lambda@Edge
            # associations reference the SAR nested-stack outputs.
            distribution.node.add_dependency(self.edge_auth)
            self._create_edge_callback_updater()
        self.cloudfront_url = f"https://{distribution.distribution_domain_name}"
        CfnOutput(self, "ADTCloudFrontUrl", value=self.cloudfront_url, description="ADT Dashboard URL (Amazon CloudFront)")

        self._restrict_alb_ingress_to_cloudfront(alb_sg, distribution)

    def _distribution_behaviors(self, alb_origin):
        """Build app behaviors; API and health paths intentionally bypass auth.

        The behavior that serves ``/api/*`` strips the Quick gateway secret
        (``x-quick-auth``) from viewer requests (stacks/_quick_header_strip.py):
        the default behavior with edge auth off, the ``/api/*`` behavior with
        edge auth on (the default carries CheckAuth on viewer request there,
        and one event type cannot hold both a CloudFront Function and
        Lambda@Edge).
        """
        policy = cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
        strip = quick_header_strip_associations(create_quick_header_strip(self))
        if not self.edge_auth_enabled:
            common = dict(
                origin=alb_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            )
            return cloudfront.BehaviorOptions(**common, function_associations=strip), {}
        common, default, additional = edge_behavior_parts(
            origin=alb_origin,
            versions=self.edge_versions,
            origin_request_policy=policy,
        )
        no_auth = cloudfront.BehaviorOptions(**common)
        api = cloudfront.BehaviorOptions(**common, function_associations=strip)
        # /js/vendor/* holds ONLY vendored open-source libraries (Chart.js).
        # Answer charts render in a sandbox="allow-scripts" iframe whose OPAQUE
        # origin sends no edge cookies, so under CheckAuth the script request
        # was 307'd to Cognito and every chart failed to load (live-found
        # 2026-09-24, test_charts e2e). Public library code carries no data.
        additional.update({"/api/*": api, "/health": no_auth,
                           "/js/vendor/*": no_auth})
        return default, additional

    def _create_web_acl(self):
        """WAF managed rules plus ADT's 64-KB body inspection exception."""
        return create_standard_web_acl(
            self,
            construct_id="AdtWebAcl",
            metric_name="AdtWebAcl",
            body_inspection_limit="KB_64",
            count_common_rules=("SizeRestrictions_BODY",),
            bare_callback_redirect_headers=self.edge_headers,
        )

    def _restrict_alb_ingress_to_cloudfront(self, alb_sg, distribution):
        """Restrict the internal ALB to ONLY the CloudFront VPC-origin
        service-managed SG (AWS-documented Option 2 — tightest). CloudFront
        VPC-origin traffic always arrives from the account/VPC-specific SG
        named `CloudFront-VPCOrigins-Service-SG`, which AWS creates once a VPC
        origin exists. Its id is not stable, so we look it up at deploy time
        and add a single :80 ingress sourced from it. The broad 0.0.0.0/0 and
        VPC-CIDR :80 rules have been removed (listener open=False + dropped
        add_ingress_rule), so nothing else in the VPC can reach the ALB.
        """
        service_sg_filters = [
            {"Name": "group-name", "Values": ["CloudFront-VPCOrigins-Service-SG"]},
            {"Name": "vpc-id", "Values": [self.network_stack.vpc.vpc_id]},
        ]
        service_sg_call = cr.AwsSdkCall(
            service="EC2",
            action="describeSecurityGroups",
            parameters={"Filters": service_sg_filters},
            physical_resource_id=cr.PhysicalResourceId.of(
                "adt-cloudfront-vpcorigins-service-sg-lookup"
            ),
        )
        service_sg_lookup = cr.AwsCustomResource(
            self,
            "AdtCloudFrontVpcOriginSgLookup",
            on_create=service_sg_call,
            on_update=service_sg_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
            install_latest_aws_sdk=False,  # only built-in describeSecurityGroups; avoids deploy-time npm + non-reproducible builds
        )
        # The service SG only exists after the VPC origin (distribution) is created.
        service_sg_lookup.node.add_dependency(distribution)

        service_sg_id = service_sg_lookup.get_response_field("SecurityGroups.0.GroupId")

        alb_ingress = ec2.CfnSecurityGroupIngress(
            self,
            "AdtAlbIngressFromCloudFrontServiceSg",
            group_id=alb_sg.security_group_id,
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            source_security_group_id=service_sg_id,
            description="Allow HTTP:80 ONLY from CloudFront VPC-origin service SG",
        )
        alb_ingress.node.add_dependency(distribution)
        alb_ingress.node.add_dependency(service_sg_lookup)
