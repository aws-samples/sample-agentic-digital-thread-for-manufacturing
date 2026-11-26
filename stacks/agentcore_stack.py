# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AgentCore Stack for the Agentic Digital Thread agents

Provisions:
- ECR repository for the system agent container image
- IAM role for AgentCore Runtime (Bedrock, RDS, Secrets Manager, Neptune)
- AgentCore Gateway with IAM inbound auth and MCP protocol
- Cognito User Pool for M2M (machine-to-machine) outbound auth (gateway → runtimes)
- AgentCore OAuth2 Credential Provider for gateway outbound auth
- Stack outputs for gateway ID, URL, ECR repo URI, runtime role ARN, credential provider ARN

The gateway uses IAM auth for inbound (ECS orchestrator → gateway) and
OAuth2 M2M for outbound (gateway → runtime MCP servers). This is required
because AgentCore gateway outbound to MCP targets only supports No Auth or OAuth,
not IAM SigV4.

Ported from the original TypeScript CDK (cdk/lib/agentcore-stack.ts)
with M2M (machine-to-machine) OAuth added for gateway-to-runtime auth.
See the official Amazon Bedrock AgentCore Gateway documentation:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html
"""

from aws_cdk import (
    Stack,
    Aws,
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Token,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_bedrockagentcore as agentcore,
    custom_resources as cr,
    CustomResource,
)
from constructs import Construct
from cdk_nag import NagPackSuppression, NagSuppressions

from stacks._cross_account import (
    grant_cross_account_assume,
)
import os
import re
from typing import Optional

from stacks._account_arns import (
    account_ec2_arn,
    account_log_group_arn,
    account_log_stream_arn,
    agentcore_runtime_log_group_arn,
)
from stacks._bedrock_access import bedrock_marketplace_auto_subscribe_statement, mantle_inference_statements
from stacks._edge_auth import (
    ExternalOidcConfig,
    create_edge_oauth_client,
    create_external_oidc_provider,
)
from stacks._graph_access import graph_data_plane_statement
from stacks import _model_allowlist
from stacks import _workshop_models
from stacks._quick_gateway_auth import (
    QuickOidcConfig,
    create_quick_service_client,
    quick_gateway_auth_props,
)
from stacks._demo_credentials import get_demo_password

# NOTE (Cedar not attached): the query gateway is CUSTOM_JWT with NO
# declarative Cedar policy engine. A declarative-in-stack Cedar layer is not
# possible pre-target-registration — AgentCore rejects a permit-all baseline as
# "Overly Permissive" (action=Any Future Tools, resource=gateway/*) and a
# specific forbid as "unrecognized action", because the gateway's tool action
# ids are only known AFTER its targets are registered, and targets are
# registered IMPERATIVELY by agentic_digital_thread/deploy.sh AFTER `cdk deploy`.
# The Cedar sources stay in the repo UNATTACHED (policies/query_gateway.cedar +
# policies/query_gateway_baseline.cedar); create the policy engine + policies
# and flip to ENFORCE after target sync, scoped to the specific
# gateway ARN + real tool action ids.

# Memory extraction model for the CUSTOM long-term strategies. Custom override
# strategies require an explicit model_id (unlike the built-in strategies, which
# use a service-managed model), and it must be a model AgentCore Memory's
# CreateMemory validation accepts in the Region, a set that has been NARROWER
# than `bedrock:InvokeModel`: in us-east-1 in 2026-07 it rejected the Anthropic
# Claude inference profiles ("Bedrock model is not available in region
# us-east-1") and accepted Amazon Nova. A 2026-10-23 probe in us-east-1 found
# CreateMemory accepting Claude Haiku 5.5, Haiku 4.5 and Sonnet 5 (global and
# us. profiles) as well as Nova Pro. Acceptance in other Regions is unprobed.
#
# Base and demo deploys use the catalog's memory default, Claude Haiku 5.5
# (agentic_digital_thread/agents/model_catalog.py, MEMORY_DEFAULT_FAMILY: the
# one home, with the comparison behind the choice and the five models
# AgentCore Memory accepted, MEMORY_EXTRACTION_FAMILIES). Its global profile
# is the same id in every Region. The Admin LLM settings page can switch a
# base or demo memory to another of those models in place; it saves the
# choice in SSM (/adt/llm-memory-model) and deploy.sh passes it back as the
# `memory_extraction_model` context so the next deploy keeps it.
#
# The workshop (`with_workshop` synths: deploy.sh --workshop and package.py
# --profile workshop) keeps Nova Pro, pinned in stacks/_workshop_models.py, so
# its templates are unchanged, and refuses the context. Nova Pro has no global
# inference profile, so that id is the model catalog's geo profile for the
# deploy Region (us./eu./apac., model_catalog.resolve_id; see
# memory_extraction_model).
#
# This is ONLY the async memory-extraction model; the orchestrator and system
# agents are unaffected and keep their own models. A model change updates the
# strategies in place (CloudFormation: ModelId is "No interruption"); the
# memory resource and its records stay.
MEMORY_EXTRACTION_FAMILY = _model_allowlist.load_model_catalog().MEMORY_DEFAULT_FAMILY

#: The workshop's family (Nova Pro), from the one home for its model pins.
WORKSHOP_MEMORY_EXTRACTION_FAMILY = _workshop_models.WORKSHOP_MEMORY_EXTRACTION_FAMILY

#: Context key app.py, the network/compute stacks and package.py use for a
#: workshop synth; only ``true`` (any case) counts.
_WITH_WORKSHOP_KEY = "with_workshop"

#: Synth-time override: a full model id, used as given, for accounts/Regions
#: with different AgentCore Memory support.
MEMORY_EXTRACTION_MODEL_ENV = "MEMORY_EXTRACTION_MODEL"

#: CDK context carrying the model saved on the Admin LLM settings page
#: (deploy.sh reads SSM /adt/llm-memory-model into it): a model id or family
#: of model_catalog.MEMORY_EXTRACTION_FAMILIES, resolved for the stack's Region.
MEMORY_EXTRACTION_MODEL_CONTEXT = "memory_extraction_model"

#: Region an account-agnostic synth resolves for when it was given no
#: ``region`` context (package.py always passes one; its default is us-east-1).
_UNKNOWN_REGION_FALLBACK = "us-east-1"

# Appended to every extraction/consolidation prompt so the extracted long-term
# records (Facts / Summaries / Preferences) are written in ENGLISH.
#
# The Agents page / Memory sidebar surfaced records (notably Summaries) in
# Spanish even though the demo conversation is in English. Root cause: the
# strategy prompts did not pin an output language, so the extraction model
# (then Nova Pro, multilingual) sometimes drifted to another language. We PIN
# ENGLISH here via append_to_prompt on every strategy. (Nova Pro, still the
# workshop's model, has been seen ignoring it on a Spanish conversation.)
#
# CAVEAT (documented, not code-controllable): append_to_prompt only APPENDS to
# AgentCore's managed extraction/consolidation prompt — the base prompt and the
# `<topic>...</topic>` summary structure are service-managed. If the managed
# model still emits another language despite this instruction, that origin is
# the AgentCore-managed strategy and is out of this repo's control; the UI also
# strips the `<topic>` markup post-hoc (see memory_client._strip_topic_markup).
LANG_INSTRUCTION = (
    "Write every extracted record, summary, and all field values in ENGLISH, "
    "regardless of the language used in the source conversation. Translate any "
    "non-English source content into English; never emit another language."
)


# Shared with the ERP dashboard stack (stacks/_cognito_domain.py): every
# Cognito hosted-UI domain in this repo must be pinned to the deploying
# account, or the account-agnostic workshop package bakes one literal prefix
# into every attendee account and only the first deploy wins.
from stacks._cognito_domain import (  # noqa: E402
    account_is_concrete as _account_is_concrete,
    domain_suffix as _domain_suffix,
    pin_domain_to_account as _pin_domain_to_account,
)


def memory_extraction_family(with_workshop: bool) -> str:
    """The catalog family of the extraction model: Nova Pro on the workshop,
    Claude Haiku 5.5 otherwise."""
    return WORKSHOP_MEMORY_EXTRACTION_FAMILY if with_workshop else MEMORY_EXTRACTION_FAMILY


def _saved_memory_family(saved, with_workshop: bool) -> str:
    """The catalog family of the ``memory_extraction_model`` context *saved*,
    ``""`` when unset or blank. Fails the synth on a workshop (its model is
    pinned) and on anything that is not one of the memory extraction models."""
    text = str(saved or "").strip()
    if not text:
        return ""
    if with_workshop:
        raise ValueError(
            f"-c {MEMORY_EXTRACTION_MODEL_CONTEXT}={text} is not accepted on a workshop "
            "synth: the workshop pins its memory extraction model "
            "(stacks/_workshop_models.py)")
    catalog = _model_allowlist.load_model_catalog()
    family = catalog.family_of(text)
    if family not in catalog.MEMORY_EXTRACTION_FAMILIES:
        raise ValueError(
            f"-c {MEMORY_EXTRACTION_MODEL_CONTEXT}={text} is not a memory extraction "
            f"model; expected one of {', '.join(catalog.MEMORY_EXTRACTION_FAMILIES)} "
            "(agentic_digital_thread/agents/model_catalog.py)")
    return family


def memory_extraction_model(region: str, region_context: Optional[str] = None, *,
                            with_workshop: bool = False, saved: Optional[str] = None) -> str:
    """Model id for the memory's custom strategies.

    ``MEMORY_EXTRACTION_MODEL`` (non-blank) wins as given. Then *saved*, the
    ``memory_extraction_model`` context (the model saved on the Admin LLM
    settings page; refused on a workshop and outside the memory models, see
    :func:`_saved_memory_family`, even when the env wins). Otherwise
    :func:`memory_extraction_family`. Either family is resolved with the
    catalog for *region*, the stack's Region, so an id saved in one Region
    resolves for this one. Haiku 5.5 resolves to its global profile in every
    Region. Nova Pro (the workshop) takes the Region's geo prefix: an
    account-agnostic synth (the workshop package, the root suite) has the
    ``AWS::Region`` token there, so it resolves for the ``region`` context
    the synth was given, *region_context* (package.py passes its
    ``--region``, us-east-1 by default), and us-east-1 without one: a plain
    string in the template, the same ``us.`` id as before in the US. A
    deployment in a European or Asia Pacific Region is synthesized with that
    Region.
    """
    saved_family = _saved_memory_family(saved, with_workshop)
    override = (os.environ.get(MEMORY_EXTRACTION_MODEL_ENV) or "").strip()
    if override:
        return override
    if Token.is_unresolved(region):
        region = str(region_context or "").strip() or _UNKNOWN_REGION_FALLBACK
    return _model_allowlist.load_model_catalog().resolve_id(
        saved_family or memory_extraction_family(with_workshop), region)


class AgentCoreStack(Stack):
    """AgentCore Gateway + ECR + Runtime IAM role + M2M OAuth auth."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        quick_api_enabled: bool = False,
        edge_auth_enabled: bool = False,
        external_oidc: Optional[ExternalOidcConfig] = None,
        quick_oidc: Optional[QuickOidcConfig] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - AgentCore Gateway, ECR repo, Cognito M2M, IAM roles", **kwargs)
        # Deploy-shape toggles, all resolved in app.py (explicit context >
        # sticky SSM > default). Each is OFF-path byte-neutral:
        #   quick_api_enabled — the SECOND, bridge-only gateway (`adt-quick-gw`,
        #     _build_quick_gateway); base deploys synth exactly one gateway.
        #   edge_auth_enabled — THIS stack owns the Authorization@Edge OAuth
        #     client (+ optional external IdP) on the adt-app pool
        #     (_create_edge_auth_client); OFF ⇒ edge-less template.
        #   external_oidc — optional external OIDC IdP for that edge client.
        self.quick_api_enabled = quick_api_enabled
        self.edge_auth_enabled = edge_auth_enabled
        self.external_oidc = external_oidc
        #   quick_oidc — ADR-0079 delegated user tokens on `adt-quick-gw`
        #     (resolved by stacks/_quick_gateway_auth.resolve_quick_oidc, which
        #     rejects it without quick_api_enabled). None ⇒ Cognito service auth.
        self.quick_oidc = quick_oidc

        # --- ECR repo for the system agent container ---
        self._build_ecr_repo()
        # --- IAM role for AgentCore Runtime ---
        self._build_runtime_role()
        self.agent_repo.grant_pull(self.runtime_role)
        self._grant_cross_account_assume()
        # --- AgentCore Memory (see the step comments below) ---
        self._build_memory_execution_role()
        self._build_memory()
        self._grant_memory_access()
        # --- Cognito User Pool for M2M (outbound auth: gateway -> runtime) ---
        m2m_domain = self._build_m2m_cognito()
        self._build_m2m_credential_provider(m2m_domain)
        gateway_role = self._build_gateway_role()
        # App-login Cognito (+ domain pin + edge client) MUST run before the
        # gateway below — its CUSTOM_JWT authorizer references their outputs.
        self._create_app_identity()
        self._grant_service_token_read()
        self._build_gateway(gateway_role)
        self._build_quick_gateway(gateway_role)
        self._outputs()

    def _create_app_identity(self) -> None:
        """adt-app pool + hosted-UI domain pin + Authorization@Edge client.

        Ordered: the domain uniqueness repair for the account-agnostic
        (workshop) synth is applied HERE rather than inside _create_app_cognito
        so that long-standing method is left untouched; the edge client reads
        the pool and is in turn read by _build_gateway's allowed_clients. The
        service-client retirement is applied here for the same reason.
        """
        self._create_app_cognito()
        self._retire_original_service_client()
        _pin_domain_to_account(
            self.adt_app_domain,
            f"adt-app-{_domain_suffix(self.account)}",
            self.account,
        )
        self._create_edge_auth_client()

    def _retire_original_service_client(self) -> None:
        """Move every consumer to a new service client; keep the old one inert.

        RETIRED (ADR-0071 amendment 2026-11-07): the original adt-app-service
        client that _create_app_cognito builds. Its secret was copied into the
        attendee-readable adt-quick-client-secret, so no gateway lists it any
        more and nothing uses it. It stays, with the same construct id and
        settings, only because ADTComputeStack and ADTOnboardingStack import
        its id on deployed stacks and CloudFormation refuses to change or
        remove an imported export: export_value keeps that export for one
        update while those stacks move to the new client's export. Cleanup
        (later release, once nothing imports it): delete the old client and
        the export_value line. Tripwire: tests/test_app_service_client_v2.py.

        The new client has the same settings and becomes adt_service_client,
        the one every consumer uses: the gateways' allowed clients, the app
        task env and the AppServiceClientId output (which deploy.sh and the
        workshop bootstrap pass to the orchestrator runtime).
        """
        self.retired_service_client = self.adt_service_client
        self.export_value(self.retired_service_client.user_pool_client_id)
        app_resource_server = self.adt_user_pool.node.find_child("AppResourceServer")
        app_query_scope = cognito.ResourceServerScope(
            scope_name="query", scope_description="Query the digital thread")
        self.adt_service_client = self.adt_user_pool.add_client(
            "AdtAppServiceClientV2",
            user_pool_client_name="adt-app-service-v2",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(app_resource_server, app_query_scope),
                ],
            ),
        )

    # ------------------------------------------------------------------
    def _build_ecr_repo(self) -> None:
        self.agent_repo = ecr.Repository(
            self, "SystemAgentRepo",
            repository_name="adt-system-agent",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,  # Scanner baseline (CKV_AWS_163); no behavior change.
        )

    def _build_runtime_role(self) -> None:
        """IAM role for AgentCore Runtime.

        Statement ORDER is template-visible — the helpers below are spliced in
        the original statement order; do not reorder them.
        """
        self.runtime_role = iam.Role(
            self, "RuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "AgentCorePolicy": iam.PolicyDocument(statements=[
                    *self._runtime_tracing_statements(),
                    *self._runtime_bedrock_statements(),
                    *self._runtime_rds_data_statements(),
                    *self._runtime_secret_read_statements(),
                    *self._runtime_graph_network_statements(),
                    *self._runtime_logs_and_table_statements(),
                    *self._runtime_guidance_table_statements(),
                    *self._runtime_s3tables_read_statements(),
                ]),
            },
        )
        # No checkov skip on this role: the Workshop Studio scanner counts
        # every skip in its rule set as a finding (stacks/_checkov_skips.py).
        # The ENI, log and Mantle inference writes are scoped to this
        # account; what stays on '*' is the Mantle bearer-token call, X-Ray
        # telemetry, describes and SiteWise reads (no resource types).

    def _runtime_tracing_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                # OTLP span export to CloudWatch Transaction Search: the
                # runtime's aws-opentelemetry-distro ships trace spans via
                # the X-Ray OTLP endpoint. Without these the exporter gets
                # 403 Forbidden, aws/spans never ingests a single event,
                # and the admin session drill-down (which reads aws/spans
                # via Logs Insights) stays permanently empty. These X-Ray
                # write APIs have no resource-level permissions ("*").
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutSpans",
                    "xray:PutSpansForIndexing",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            ),
        ]

    def _runtime_bedrock_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                # InvokeModel against cross-region inference profiles requires
                # access to both the inference-profile ARN and the underlying
                # foundation models it routes to (foundation-model ARNs carry no
                # account, hence the *::foundation-model/* form).
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            ),
            bedrock_marketplace_auto_subscribe_statement(),
            # Bedrock MANTLE (ADR-0055): stacks/_bedrock_access.py.
            *mantle_inference_statements(self),
            iam.PolicyStatement(
                # Scope to gateways in this account/region. The concrete gateway
                # is created later in this same stack, so its ARN is not yet
                # available when this role is defined; the wildcard is bounded to
                # account + region.
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                ],
            ),
            iam.PolicyStatement(
                # agents/config.py discovers the orchestrator + per-system agent
                # runtime ARNs BY NAME via list_agent_runtimes at startup (ADR-0035
                # Mode A routes NL questions to query_<id> tools whose runtime ARNs
                # come from this lookup; Mode B resolves system runtimes the same way).
                # Without it the orchestrator logs AccessDenied and cannot resolve
                # its fleet. List has no per-resource condition (bounded to this
                # account/region's runtimes); Get is scoped to those runtime ARNs.
                actions=[
                    "bedrock-agentcore:ListAgentRuntimes",
                    "bedrock-agentcore:GetAgentRuntime",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*",
                ],
            ),
        ]

    def _runtime_rds_data_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                # Aurora Data API: scope to the specific actions the agent uses
                # and to clusters in this account/region. The target cluster is
                # provisioned in a separate stack, so its ARN is not referenced
                # here; the wildcard is bounded to account + region clusters.
                actions=[
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                resources=[f"arn:aws:rds:{self.region}:{self.account}:cluster:*"],
            ),
        ]

    def _runtime_secret_read_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                # Scope to only the secrets the runtimes actually read:
                #  - adt*  : per-system connection-config secrets created
                #    at runtime by agent_manager (adt/agent-config/<id>)
                #    and per-system connection secrets (adt/conn/<id>,
                #    e.g. the HighByte MCP token at conn/highbyte, read by catalog.py
                #    / connection_resolver — ADR 0008 node-only).
                #  - bedrock-agentcore* : AgentCore gateway/identity OAuth secrets
                #    consumed during gateway-to-runtime auth.
                # NOT the demo database owner secrets (AdtDbSecret* /
                # DatabaseSecret*): a system agent for a schema of a database
                # ADT provisions connects with that schema's read-only login,
                # which the app copies into the system's agent-config secret
                # (agents/demo/db_routing.py, stacks/_db_readers.py), and a
                # PostgreSQL runtime makes no other secret read at start
                # (runtime_app._resolve_runtime_connection_config). IAM resource
                # matching is CASE-SENSITIVE, so "adt*" does not match
                # "AdtDbSecret…" or the reader secrets ("SeedDbReaders…" /
                # "ErpDbReaders…"): no runtime can read an owner login.
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt*",
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore*",
                ],
            ),
        ]

    def _runtime_graph_network_statements(self) -> list[iam.PolicyStatement]:
        return [
            # ADR-0069: the ONE graph IAM helper. Write tier (proposals / usage
            # are CREATE/MERGE/SET; accept + replace-not-accrete deletes run in
            # the app process, so NO delete tier — least privilege). The
            # digital-thread graph is created in a separate stack, so its ARN
            # is not referenced here; graph/* is bounded to account+region.
            graph_data_plane_statement(
                region=self.region, account=self.account,
                graph_id=None, write=True),
            iam.PolicyStatement(
                # AgentCore attaches the runtime to the VPC with its own
                # service-linked role (AWSServiceRoleForBedrockAgentCoreNetwork),
                # not with this role. These ENI grants stay for code that
                # manages ENIs itself, scoped to this account and Region; the
                # describe has no resource type, so it stays on "*".
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DeleteNetworkInterface",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses",
                ],
                resources=[account_ec2_arn(self)],
            ),
            iam.PolicyStatement(
                actions=["ec2:DescribeNetworkInterfaces"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                # IoT SiteWise List*/Describe*/Get* asset-discovery actions do not
                # support resource-level permissions and must be granted on "*".
                actions=[
                    "iotsitewise:ListAssets",
                    "iotsitewise:ListAssetModels",
                    "iotsitewise:DescribeAsset",
                    "iotsitewise:DescribeAssetModel",
                    "iotsitewise:GetAssetPropertyValue",
                    "iotsitewise:BatchGetAssetPropertyValue",
                    "iotsitewise:BatchGetAssetPropertyValueHistory",
                    "iotsitewise:GetAssetPropertyValueHistory",
                    "iotsitewise:ListAssociatedAssets",
                ],
                resources=["*"],
            ),
        ]

    def _runtime_logs_and_table_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                # The runtime's log group name is assigned by the AgentCore
                # service at run time, so the writes cover every log group in
                # this account and Region. DescribeLogGroups has no resource
                # type and stays on "*".
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[account_log_group_arn(self), account_log_stream_arn(self)],
            ),
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                # AgentCore attaches a resource policy to each runtime's own
                # log group as it creates the runtime, with this role (the AWS
                # execution-role policy for AgentCore Runtime grants it). One
                # policy per log group, so no account-level quota is used.
                actions=["logs:PutResourcePolicy"],
                resources=[agentcore_runtime_log_group_arn(self)],
            ),
            iam.PolicyStatement(
                # Branding overrides the Admin tab saves (agents/branding.py
                # reads them at import and falls back to its defaults).
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter/adt/branding-company",
                    f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter/adt/branding-tagline",
                ],
            ),
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                    "dynamodb:Scan",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-proposed-mappings",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-proposed-mappings/index/*",
                ],
            ),
            iam.PolicyStatement(
                # Wave 5 provenance sidecar: system-agent runtimes write bounded
                # provenance-query entries through JobStore (get_item/put_item/
                # delete_item — exactly these three calls) onto the onboarding-
                # sessions table. Literal table name, same rationale as the
                # adt-proposed-mappings statement above: the table lives in
                # ADTOnboardingStack, which depends on THIS stack, so a construct
                # reference is impossible. Single-table scope — no index, no Scan.
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/adt-onboarding-sessions",
                ],
            ),
        ]

    def _runtime_guidance_table_statements(self) -> list[iam.PolicyStatement]:
        """System runtimes consume guidance; the ECS app is the sole writer."""
        return [
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:"
                    "table/adt-system-guidance",
                ],
            ),
        ]

    def _runtime_s3tables_read_statements(self) -> list[iam.PolicyStatement]:
        """Least-privilege S3 Tables READ for the S3 Tables (DuckDB) connector
        (ADR-0099), SAME-ACCOUNT case: the agent reads the table bucket with this
        task role's ambient credentials.

        Scoped to a context-supplied bucket ARN (``s3TablesBucketArn``), NEVER a
        wildcard on a data boundary. Returns NO statement when the context is
        absent, so a deploy that does not use the DuckDB connector grants nothing
        (fail-closed). Read actions only; no write/create action is granted. A
        Lake-Formation-integrated bucket additionally needs an LF grant to this
        role (not expressible here); a CROSS-account bucket is handled by the
        DuckDB STS path / the data-read role, not this statement (see ADR-0099)."""
        bucket_arn = self.node.try_get_context("s3TablesBucketArn")
        if not bucket_arn:
            return []
        return [
            iam.PolicyStatement(
                sid="S3TablesRead",
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:ListNamespaces",
                    "s3tables:GetNamespace",
                    "s3tables:ListTables",
                    "s3tables:GetTable",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:GetTableData",
                ],
                resources=[bucket_arn, f"{bucket_arn}/table/*"],
            ),
        ]

    def _grant_cross_account_assume(self) -> None:
        # Optional cross-account data onboarding (docs/cross-account-onboarding.md):
        # the ONE caller-side grant lives in stacks/_cross_account.py and is a
        # no-op when the deploy provides no `crossAccountDataRoleArn`.
        grant_cross_account_assume(self, self.runtime_role)

    # =====================================================================
    # AgentCore Memory (Task 0.A)
    # =====================================================================
    # A managed AgentCore Memory store backs the Events / Facts / Summaries /
    # Preferences tabs. It holds short-term conversation events (with a 7-day
    # TTL) and three built-in long-term extraction strategies. Conversation
    # data is the only thing persisted here; per D2 the raw events expire
    # after 7 days.
    #
    # B4 (MANDATORY): the built-in extraction strategies run async Bedrock
    # extraction to populate the long-term records that back the
    # Facts/Summaries/Preferences tabs. That extraction needs an execution
    # role the AgentCore Memory service assumes to call Bedrock. Without it,
    # only short-term events populate and the other three tabs stay empty.
    def _build_memory_execution_role(self) -> None:
        self.memory_execution_role = iam.Role(
            self, "MemoryExecutionRole",
            # Confused-deputy guard: constrain the AgentCore Memory service
            # principal so it may only assume this role on behalf of THIS
            # account. aws:SourceAccount pins the trust to self.account, so a
            # memory resource in another account cannot induce the service to
            # assume this role (cross-account confused-deputy). This is a trust
            # condition only — it does not over-constrain the in-account service,
            # which always presents this account as the source.
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            inline_policies={
                "MemoryExtractionPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        # Mirrors the runtime_role bedrock statement above:
                        # InvokeModel against cross-region inference profiles requires
                        # access to both the inference-profile ARN and the underlying
                        # foundation models it routes to (foundation-model ARNs carry no
                        # account, hence the *::foundation-model/* form). The built-in
                        # extraction strategies use AgentCore-managed Bedrock models whose
                        # concrete model IDs are selected by the service and are not
                        # determinable at synth time, so the foundation-model breadth
                        # cannot be narrowed to a specific model family without risking
                        # broken extraction. The inference-profile ARN IS scoped to this
                        # account+region. This mirrors the documented runtime pattern and
                        # is covered by the IAM5 cdk-nag suppression (wildcard resource on
                        # foundation-model/* required by the managed-extraction runtime).
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[
                            "arn:aws:bedrock:*::foundation-model/*",
                            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                        ],
                    ),
                    # Memory extraction is a model invoke too: in a fresh account
                    # it can be the FIRST Anthropic call and must be allowed to
                    # auto-subscribe (see stacks/_bedrock_access.py).
                    bedrock_marketplace_auto_subscribe_statement(),
                ]),
            },
        )

    # Three CUSTOM extraction strategies (semantic / summary /
    # userPreference overrides). These mirror the built-in strategy
    # behaviour but add an output-language instruction
    # (LANG_INSTRUCTION) to each extraction/consolidation prompt so records
    # are written in English. Custom overrides require an explicit
    # model_id (memory_extraction_model, resolved once per stack). Namespace
    # templates have leading + trailing slashes exactly so the read client
    # matches; {actorId}/{sessionId} are substituted by the service at write
    # time. Namespaces are UNCHANGED from the prior built-in strategies.
    #
    # FORCE-REPLACE: the construct id is "AdtMemoryLang" (was "AdtMemory").
    # CloudFormation forbids changing a strategy's TYPE in place
    # (built-in -> custom), so the rename forces a clean replacement of the
    # memory resource. The cross-stack MemoryId export consumer is being
    # removed in this same migration, so deleting the old export is not
    # blocked.
    def _build_memory(self) -> None:
        self.memory_extraction_model = memory_extraction_model(
            self.region, self.node.try_get_context("region"),
            with_workshop=str(self.node.try_get_context(_WITH_WORKSHOP_KEY)).lower() == "true",
            saved=self.node.try_get_context(MEMORY_EXTRACTION_MODEL_CONTEXT))
        self.memory = agentcore.CfnMemory(
            self, "AdtMemoryLang",
            # ENCRYPTION (demonstration sample, not production-hardened): no
            # encryption_key_arn is set, so AgentCore Memory encrypts data with
            # an AWS-owned key by default. For production, supply a
            # customer-managed KMS key via the encryption_key_arn property (and
            # grant this account/role kms:Decrypt + kms:GenerateDataKey on it) so
            # conversation data at rest is under your own key with auditable
            # key-usage and rotation control.
            # Distinctive, collision-proof name: the account holds 5 unrelated
            # leftover prototype memories (mfg_thread_memory, factoryfloor,
            # customersupport, testharness, mfg_thread_discovery_memory) — do NOT
            # reuse a generic name (LIVE-STATE ADDENDUM).
            name="adt_memory",
            # event_expiry_duration is in DAYS (valid range 7-365). 7 = the 7-day
            # short-term TTL mandated by D2.
            event_expiry_duration=7,
            memory_execution_role_arn=self.memory_execution_role.role_arn,
            memory_strategies=[
                self._facts_strategy(),
                self._summaries_strategy(),
                self._preferences_strategy(),
            ],
        )
        self.memory.apply_removal_policy(RemovalPolicy.DESTROY)

        # Expose memory id/arn for cross-stack env wiring (Task 1.C consumes
        # memory_id as MEMORY_ID in the ECS task + orchestrator runtime env).
        self.memory_id = self.memory.attr_memory_id
        self.memory_arn = self.memory.attr_memory_arn

    def _facts_strategy(self) -> agentcore.CfnMemory.MemoryStrategyProperty:
        # facts: custom semantic override (extraction + consolidation).
        return agentcore.CfnMemory.MemoryStrategyProperty(
            custom_memory_strategy=agentcore.CfnMemory.CustomMemoryStrategyProperty(
                name="facts",
                namespaces=["/facts/{actorId}/"],
                configuration=agentcore.CfnMemory.CustomConfigurationInputProperty(
                    semantic_override=agentcore.CfnMemory.SemanticOverrideProperty(
                        extraction=agentcore.CfnMemory.SemanticOverrideExtractionConfigurationInputProperty(
                            append_to_prompt=LANG_INSTRUCTION,
                            model_id=self.memory_extraction_model,
                        ),
                        consolidation=agentcore.CfnMemory.SemanticOverrideConsolidationConfigurationInputProperty(
                            append_to_prompt=LANG_INSTRUCTION,
                            model_id=self.memory_extraction_model,
                        ),
                    ),
                ),
            ),
        )

    def _summaries_strategy(self) -> agentcore.CfnMemory.MemoryStrategyProperty:
        # summaries: custom summary override (consolidation only).
        return agentcore.CfnMemory.MemoryStrategyProperty(
            custom_memory_strategy=agentcore.CfnMemory.CustomMemoryStrategyProperty(
                name="summaries",
                namespaces=["/summaries/{actorId}/{sessionId}/"],
                configuration=agentcore.CfnMemory.CustomConfigurationInputProperty(
                    summary_override=agentcore.CfnMemory.SummaryOverrideProperty(
                        consolidation=agentcore.CfnMemory.SummaryOverrideConsolidationConfigurationInputProperty(
                            append_to_prompt=LANG_INSTRUCTION,
                            model_id=self.memory_extraction_model,
                        ),
                    ),
                ),
            ),
        )

    def _preferences_strategy(self) -> agentcore.CfnMemory.MemoryStrategyProperty:
        # preferences: custom user-preference override (extraction + consolidation).
        return agentcore.CfnMemory.MemoryStrategyProperty(
            custom_memory_strategy=agentcore.CfnMemory.CustomMemoryStrategyProperty(
                name="preferences",
                namespaces=["/preferences/{actorId}/"],
                configuration=agentcore.CfnMemory.CustomConfigurationInputProperty(
                    user_preference_override=agentcore.CfnMemory.UserPreferenceOverrideProperty(
                        extraction=agentcore.CfnMemory.UserPreferenceOverrideExtractionConfigurationInputProperty(
                            append_to_prompt=LANG_INSTRUCTION,
                            model_id=self.memory_extraction_model,
                        ),
                        consolidation=agentcore.CfnMemory.UserPreferenceOverrideConsolidationConfigurationInputProperty(
                            append_to_prompt=LANG_INSTRUCTION,
                            model_id=self.memory_extraction_model,
                        ),
                    ),
                ),
            ),
        )

    def _grant_memory_access(self) -> None:
        # AgentCore Memory data-plane grant (Task 1.B) — added here, AFTER
        # self.memory is created, so self.memory_arn resolves to this stack's
        # concrete memory ARN (Fn::GetAtt) rather than a memory/* wildcard. This
        # scopes the orchestrator runtime to ONLY this stack's memory; the
        # account holds 5 unrelated leftover prototype memories that must not be
        # in scope. The orchestrator writes conversation turns (CreateEvent) and
        # reads short-term events / long-term extracted records (records live
        # under sub-paths of the memory ARN, hence the f"{arn}/*" resource).
        self.runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[
                    self.memory_arn,
                    f"{self.memory_arn}/*",
                ],
            )
        )

        # ListMemories is granted account-wide (resource "*") in a SEPARATE
        # statement because the action does not support resource-level scoping
        # (no per-memory ARN condition). The runtime needs it because it
        # discovers this memory by NAME at startup rather than by a wired-in id
        # (ADR-0006: MEMORY_ID cross-stack env wiring removed; discovery is
        # primary in both cloud and local modes). The data-plane actions above
        # remain scoped to this stack's concrete memory ARN, so the broad scope
        # here only covers enumeration, not read/write of conversation data.
        self.runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:ListMemories"],
                resources=["*"],
            )
        )

    # =====================================================================
    # Cognito User Pool for M2M (outbound auth: gateway → runtime)
    # =====================================================================
    def _build_m2m_cognito(self) -> cognito.UserPoolDomain:
        self.m2m_pool = cognito.UserPool(
            self, "M2MUserPool",
            user_pool_name="adt-m2m",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Resource server defines the scopes the gateway can request
        resource_server = self.m2m_pool.add_resource_server(
            "McpResourceServer",
            identifier="mcp",
            scopes=[
                cognito.ResourceServerScope(scope_name="tools.read", scope_description="Read MCP tools"),
                cognito.ResourceServerScope(scope_name="tools.execute", scope_description="Execute MCP tools"),
            ],
        )

        # M2M app client — confidential client with client_credentials grant
        self.m2m_client = self.m2m_pool.add_client(
            "M2MClient",
            user_pool_client_name="adt-gw-m2m",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(custom=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(resource_server, 
                        cognito.ResourceServerScope(scope_name="tools.read", scope_description="Read MCP tools")),
                    cognito.OAuthScope.resource_server(resource_server,
                        cognito.ResourceServerScope(scope_name="tools.execute", scope_description="Execute MCP tools")),
                ],
            ),
        )

        # Cognito domain for token endpoint. The account suffix keeps the prefix
        # stable + collision-safe; see _domain_suffix for the local-synth
        # placeholder behavior.
        m2m_prefix = f"adt-m2m-{_domain_suffix(self.account)}"
        m2m_domain = self.m2m_pool.add_domain(
            "M2MDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=m2m_prefix,
            ),
        )
        _pin_domain_to_account(m2m_domain, m2m_prefix, self.account)
        return m2m_domain

    # =====================================================================
    # AgentCore OAuth2 Credential Provider (CDK-native)
    # =====================================================================
    def _build_m2m_credential_provider(self, m2m_domain: cognito.UserPoolDomain) -> None:
        # Tells the gateway how to obtain M2M tokens for outbound calls to the
        # per-system runtime MCP targets. The provider name is referenced by the
        # gateway targets (agent_manager) and MUST remain "adt-m2m".
        # The Cognito client secret is surfaced to CloudFormation by CDK via a
        # DescribeUserPoolClient custom resource (the client sets generate_secret).
        self.cognito_discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/{self.m2m_pool.user_pool_id}"
            f"/.well-known/openid-configuration"
        )
        self.m2m_credential_provider = agentcore.CfnOAuth2CredentialProvider(
            self, "M2MOauth2CredentialProvider",
            name="adt-m2m",
            credential_provider_vendor="CustomOauth2",
            oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                custom_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                    client_id=self.m2m_client.user_pool_client_id,
                    client_secret=self.m2m_client.user_pool_client_secret.unsafe_unwrap(),  # noqa: S106 - CFN token reference, not a literal secret
                    oauth_discovery=agentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                        discovery_url=self.cognito_discovery_url,
                    ),
                ),
            ),
        )
        # The provider validates the Cognito OIDC discovery document (whose
        # token endpoint lives behind the user-pool domain) at create time, so
        # ensure the client + domain exist first.
        self.m2m_credential_provider.node.add_dependency(self.m2m_client)
        self.m2m_credential_provider.node.add_dependency(m2m_domain)

    # =====================================================================
    # Gateway IAM role — needs permissions for OAuth token acquisition
    # =====================================================================
    def _build_gateway_role(self) -> iam.Role:
        return iam.Role(
            self, "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "GatewayIdentityPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:GetWorkloadAccessToken"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:GetResourceOauth2Token"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/*",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default/oauth2credentialprovider/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore*",
                            f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity*",
                        ],
                    ),
                ]),
            },
        )

    def _grant_service_token_read(self) -> None:
        # The orchestrator runtime, when invoked WITHOUT a forwarded end-user
        # token (fleet-page test invoke, warmup, scheduler, briefings), mints a
        # adt-app-service client-credentials token and must read that client's
        # secret via cognito-idp:DescribeUserPoolClient — the same call the app
        # task role makes. adt-app-service lives in self.adt_user_pool (created by
        # _create_app_cognito above), so grant the runtime role that read on that
        # pool; without it _fetch_service_token -> _resolve_gateway_bearer ->
        # build_orchestrator raises AccessDeniedException and tokenless
        # invocations cannot reach any gateway tool.
        self.runtime_role.add_to_policy(iam.PolicyStatement(
            actions=["cognito-idp:DescribeUserPoolClient"],
            resources=[self.adt_user_pool.user_pool_arn],
        ))
        # ...and the pool itself, to read its ACTUAL Cognito domain rather than
        # rebuilding `adt-app-{last 8 of account}` by convention. The
        # account-agnostic workshop packaging deploys a deploy-time-resolved
        # prefix (see _pin_domain_to_account), so the guess would point at a
        # domain that does not exist and every background mint would fail
        # closed (orchestrator._resolve_app_domain_prefix).
        self.runtime_role.add_to_policy(iam.PolicyStatement(
            actions=["cognito-idp:DescribeUserPool"],
            resources=[self.adt_user_pool.user_pool_arn],
        ))

    # =====================================================================
    # Cedar planned but not attached; no declarative policy engine here
    # =====================================================================
    # The query gateway below is CUSTOM_JWT(adt-app) with NO policy engine.
    # A declarative-in-stack Cedar layer is not possible pre-target-
    # registration: AgentCore rejects a permit-all baseline as "Overly
    # Permissive" and a specific forbid as "unrecognized action", because the
    # gateway's tool action ids are only known AFTER its targets are
    # registered — and targets are registered IMPERATIVELY by
    # agentic_digital_thread/deploy.sh (§2) AFTER `cdk deploy`. Create the policy
    # engine + policies (scoped to the specific gateway ARN + real tool
    # action ids) and flip to ENFORCE after target sync. The Cedar
    # sources stay UNATTACHED in policies/query_gateway*.cedar.
    # --- AgentCore Gateway (L1 CfnGateway) — P5 identity cutover ----------
    # AUTHORIZER: AWS_IAM -> CUSTOM_JWT(adt-app). The orchestrator now reaches
    # the gateway with an adt-app bearer JWT (the interactive user's token, or
    # a adt-app-service client-credentials token for background callers), so
    # per-user identity (cognito:groups) is visible to Cedar. BOTH the login
    # client (adt-app-client) and the service client (adt-app-service) are
    # allowed, because both mint tokens under the same adt-app issuer.
    # Semantic search is UNCHANGED (kept SEMANTIC). Cedar is DEFERRED to
    # a later attachment step (no policy engine is attached here).
    #
    # ── ROLLBACK (single-deploy revert; see P5 in the deploy-readiness plan) ──
    # To revert this cutover (query path returns to the known-good SigV4 state):
    #   1. Set authorizer_type="AWS_IAM" and DELETE authorizer_configuration.
    #   2. In agentic_digital_thread/agents/orchestrator.py revert the gateway MCP
    #      client to SigV4 (_create_mcp_client(..., use_sigv4=True)).
    # The gateway authorizer and the orchestrator client MUST flip TOGETHER
    # (a CUSTOM_JWT gateway rejects SigV4 and vice-versa). Then redeploy and
    # re-sync the gateway targets (agent_manager).
    # REPLACEMENT (P5 in-place cutover): the authorizer type CANNOT be
    # updated on an existing gateway (the service rejects it: "Authorizer
    # type cannot be updated for an existing gateway"). Flipping AWS_IAM ->
    # CUSTOM_JWT therefore requires REPLACING the gateway. The construct id
    # AND name are changed (from "AdtGateway" /
    # "adt-gw") so CloudFormation creates a fresh CUSTOM_JWT
    # gateway and deletes the old AWS_IAM one. The name must differ because
    # CFN creates the replacement before deleting the original and AgentCore
    # gateway names are unique. The old gateway's targets must be deleted
    # first (targets block gateway deletion); deploy.sh re-syncs targets onto
    # this new gateway afterward. Output keys (GatewayId/GatewayUrl) are
    # unchanged so all downstream wiring repoints automatically.
    def _build_gateway(self, gateway_role: iam.Role) -> None:
        self.gateway = agentcore.CfnGateway(
            self, "AdtQueryGateway",
            name="adt-query-gw",
            authorizer_type="CUSTOM_JWT",
            protocol_type="MCP",
            role_arn=gateway_role.role_arn,
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=self.adt_app_discovery_url,
                    # Single-issuer (adt-app): the interactive login client
                    # (adt-app-client) AND the background service client
                    # (adt-app-service-v2) must both be allowed or those callers
                    # are rejected. Never the retired adt-app-service.
                    allowed_clients=[
                        self.adt_app_client.user_pool_client_id,
                        self.adt_service_client.user_pool_client_id,
                        # Edge-authenticated browsers hold tokens minted by the
                        # EDGE client (ADR-0070), and the app forwards that
                        # end-user access token as the gateway bearer
                        # (ADR-0049) — without this entry every interactive
                        # query from an edge session is 403'd. Off ⇒ absent,
                        # keeping the template byte-identical.
                        *(
                            [self.edge_auth_client.user_pool_client_id]
                            if self.edge_auth_client is not None
                            else []
                        ),
                    ],
                ),
            ),
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    # Semantic search kept ON (unchanged from the AWS_IAM baseline).
                    search_type="SEMANTIC",
                    supported_versions=["2025-03-26"],
                ),
            ),
        )
        self.gateway_id = self.gateway.attr_gateway_identifier
        self.gateway_url = self.gateway.attr_gateway_url

    # --- Dedicated Quick gateway (opt-in, quick_api_enabled) ---------------
    # AWS Quick's MCP connector fans out over EVERY tool the gateway
    # exposes. On the shared `adt-query-gw` that is every onboarded system's
    # tools + the graph tools + the bridge tools, and Quick reasons over the
    # raw tools instead of calling the bridge's submit_query/get_query_result.
    # A SECOND gateway that holds ONLY the `query-bridge` target fixes that
    # (agent_manager.ensure_query_bridge_target provisions there via the
    # QUICK_GATEWAY_ID/URL task env). Same issuer as the shared gateway:
    # CUSTOM_JWT against adt-app, both app clients allowed, PLUS Quick's own
    # client_credentials client adt-quick-service (scope adt-quick/bridge),
    # which is what Quick's connector signs in with. Only THIS gateway lists
    # it, so the secret Quick holds (and every workshop attendee reads) cannot
    # call adt-query-gw's system targets or adt-onboard-gw. The shared gateway
    # construct is NOT touched (its authorizer TYPE cannot be updated in
    # place; see the note above).
    # ADR-0079: with quick_oidc set this gateway instead accepts delegated
    # user tokens from an external OIDC identity provider — same type
    # (CUSTOM_JWT), so the switch is an in-place update of THIS gateway (its
    # URL is the registered audience and must survive).
    def _build_quick_gateway(self, gateway_role: iam.Role) -> None:
        self.quick_gateway = None
        self.quick_gateway_id = ""
        self.quick_gateway_url = ""
        self.quick_service_client = None
        if not self.quick_api_enabled:
            return
        # Before the gateway: its Cognito-mode authorizer lists this client.
        self.quick_service_client = create_quick_service_client(self)
        self.quick_gateway = agentcore.CfnGateway(
            self, "AdtQuickGateway",
            name="adt-quick-gw",
            authorizer_type="CUSTOM_JWT",
            protocol_type="MCP",
            role_arn=gateway_role.role_arn,
            # ADR-0079: Cognito service auth (default, unchanged) OR delegated
            # user tokens from an external OIDC identity provider + the
            # REQUEST interceptor — built in the ONE home
            # stacks/_quick_gateway_auth.py. Type stays CUSTOM_JWT (in place).
            **quick_gateway_auth_props(self, self.quick_oidc, gateway_role),
            # MCP 2025-03-26 is a CONTRACT in both modes (ADR-0079 D8): some
            # assistant tenants do not accept 2025-11-25 yet.
            # NO semantic search (ADR-0071 amendment 2026-10-02): SEMANTIC
            # adds the built-in x_amz_bedrock_agentcore_search tool to
            # tools/list, so Quick's connector found FOUR actions where the
            # workshop and the bridge contract promise three. The gateway holds
            # one fixed three-tool target, the same case as the onboarding
            # gateway (small fixed toolset, no search).
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=["2025-03-26"],
                ),
            ),
        )
        self.quick_gateway_id = self.quick_gateway.attr_gateway_identifier
        self.quick_gateway_url = self.quick_gateway.attr_gateway_url

    def _outputs(self) -> None:
        # --- Outputs ---
        # Discovery URL (self.cognito_discovery_url) is defined above, alongside
        # the OAuth2 credential provider that consumes it.
        CfnOutput(self, "GatewayId", value=self.gateway_id)
        CfnOutput(self, "GatewayUrl", value=self.gateway_url)
        if self.quick_gateway is not None:
            CfnOutput(self, "QuickGatewayId", value=self.quick_gateway_id)
            CfnOutput(self, "QuickGatewayUrl", value=self.quick_gateway_url)
            # The client id Quick's connector uses (Service authentication).
            # NOT AppServiceClientId: that client is the app's own and works
            # on every adt-app gateway.
            CfnOutput(self, "QuickClientId",
                      value=self.quick_service_client.user_pool_client_id,
                      description="AWS Quick MCP connector client id (adt-quick-service; "
                                  "accepted only by adt-quick-gw)")
            if self.quick_oidc is not None:
                # ADR-0079: read by deploy.sh / operators to confirm the mode
                # (absent ⇒ Cognito service auth, keeping that template unchanged).
                CfnOutput(self, "QuickGatewayIdentityMode", value="external-oidc")
        CfnOutput(self, "EcrRepoUri", value=self.agent_repo.repository_uri)
        CfnOutput(self, "RuntimeRoleArn", value=self.runtime_role.role_arn)
        CfnOutput(self, "M2MUserPoolId", value=self.m2m_pool.user_pool_id)
        CfnOutput(self, "M2MClientId", value=self.m2m_client.user_pool_client_id)
        CfnOutput(self, "M2MCognitoDiscoveryUrl", value=self.cognito_discovery_url)
        CfnOutput(self, "M2MCredentialProviderArn",
                  value=self.m2m_credential_provider.attr_credential_provider_arn)
        CfnOutput(self, "MemoryId", value=self.memory_id)
        CfnOutput(self, "AppUserPoolId", value=self.adt_user_pool.user_pool_id)
        CfnOutput(self, "AppClientId", value=self.adt_app_client.user_pool_client_id)
        CfnOutput(self, "AppServiceClientId", value=self.adt_service_client.user_pool_client_id)
        CfnOutput(self, "AppUserPoolIssuer", value=self.adt_app_user_pool_issuer)

    # =====================================================================
    # Cognito User Pool for the ADT app (INBOUND user login: adt-app)
    # =====================================================================
    # Lives here (not in ADTComputeStack) to avoid a gateway <-> adt-app stack
    # dependency cycle (F1 / ADR-0033). The
    # gateway's CUSTOM_JWT authorizer (below, P5) must trust the adt-app
    # issuer at synth time, but the gateway lives in this (upstream) stack;
    # a pool downstream in ADTComputeStack would be a
    # backward cross-stack reference, which CDK rejects as a circular
    # dependency. ADTAgentCoreStack already owns the M2M pool, so co-locating
    # the inbound issuer here is the least-surprising home. ADTComputeStack
    # consumes the pool via the passed agentcore_stack handle for the ECS
    # COGNITO_* env.
    #
    # MUST run BEFORE the query gateway below: the CUSTOM_JWT authorizer and
    # its allowed_clients reference self.adt_app_discovery_url /
    # self.adt_app_client / self.adt_service_client, all created here.

    # ------------------------------------------------------------------
    def _create_app_cognito(self) -> None:
        """Cognito User Pool for the ADT app login (adt-app + ADT demo user).

        Relocated verbatim from ADTComputeStack._create_cognito (F1 / ADR-0033) so
        the gateway and the issuer live in the same upstream stack. Exposes
        ``self.adt_user_pool`` / ``self.adt_app_client`` for ADTComputeStack to read
        via the agentcore_stack handle, plus ``self.adt_app_user_pool_issuer`` /
        ``self.adt_app_discovery_url`` for the future CUSTOM_JWT authorizer.
        """
        self.adt_user_pool = cognito.UserPool(
            self, "AdtAppUserPool",
            user_pool_name="adt-app",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_uppercase=False,
                require_lowercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.adt_app_client = self.adt_user_pool.add_client(
            "AdtAppClient",
            user_pool_client_name="adt-app-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
            ),
        )

        # Issuer + OIDC discovery URL for the adt-app pool. Consumed downstream
        # by the query gateway CUSTOM_JWT authorizer and the onboarding
        # gateway; surfaced here so the cross-stack reference is the
        # cycle-free direction (compute/gateway → this stack).
        self.adt_app_user_pool_issuer = (
            f"https://cognito-idp.{self.region}.amazonaws.com/{self.adt_user_pool.user_pool_id}"
        )
        self.adt_app_discovery_url = (
            f"{self.adt_app_user_pool_issuer}/.well-known/openid-configuration"
        )

        # Create Admin group
        cognito.CfnUserPoolGroup(
            self, "AdtAdminGroup",
            user_pool_id=self.adt_user_pool.user_pool_id,
            group_name="Admin",
            description="ADT app administrators",
        )

        # --- Resource server + scopes (ADR-0033) --------------------------------
        # Consolidate gateway-INBOUND identity onto the adt-app issuer: a single
        # resource server `adt` defines the OAuth scopes the gateway CUSTOM_JWT
        # authorizer and planned Cedar policies will check. These are inert
        # here — nothing trusts them until the gateway flip.
        #   adt/query          — query the digital thread (orchestrator gateway)
        #   adt/onboard.explore — read-only exploration during onboarding
        #   adt/onboard.write   — commit/write-back during onboarding
        app_scopes = [
            cognito.ResourceServerScope(scope_name="query", scope_description="Query the digital thread"),
            cognito.ResourceServerScope(scope_name="onboard.explore", scope_description="Explore systems during onboarding (read-only)"),
            cognito.ResourceServerScope(scope_name="onboard.write", scope_description="Commit onboarding changes (write-back)"),
        ]
        app_query_scope = app_scopes[0]
        app_resource_server = self.adt_user_pool.add_resource_server(
            "AppResourceServer",
            identifier="adt",
            scopes=app_scopes,
        )

        # --- Service (machine/background) client — client_credentials -------
        # For non-interactive callers (warmup / scheduler / briefings / Quick
        # service flows) that must present an adt-app-issued token under the SAME
        # issuer the gateway trusts. Confidential client (generate_secret=True)
        # with the client_credentials grant. Scoped to adt/query ONLY
        # (least-privilege): background/service callers query the digital thread
        # but never onboard, so onboard.explore / onboard.write are withheld. No
        # custom auth flow — the client_credentials machine grant doesn't use it.
        # The interactive login client (adt-app-client) is intentionally LEFT
        # UNCHANGED, as is the adt-m2m outbound provider.
        self.adt_service_client = self.adt_user_pool.add_client(
            "AdtAppServiceClient",
            user_pool_client_name="adt-app-service",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(app_resource_server, app_query_scope),
                ],
            ),
        )

        # --- Cognito domain (token endpoint required by client_credentials) -
        # A user-pool domain is required for the OAuth2 token endpoint the
        # client_credentials flow uses. Prefix is adt-app-<acct8> (deliberately
        # DIFFERENT from the pre-relocation ADTComputeStack copy's prefix) so the
        # relocated domain does not collide with the old one during the migration
        # deploy — Cognito domain prefixes are globally unique. Kept in sync with
        # orchestrator._fetch_service_token, which builds the token endpoint from
        # this same prefix.
        self.adt_app_domain = self.adt_user_pool.add_domain(
            "AdtAppDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"adt-app-{_domain_suffix(self.account)}",
            ),
        )

        # --- Role groups alongside the existing Admin group ---------------------
        # `analyst` and `integrator` carry the per-user role Cedar reads from
        # cognito:groups. integrator is the write-back/commit role;
        # analyst is read/query. Admin (above) is unchanged.
        cognito.CfnUserPoolGroup(
            self, "AdtAnalystGroup",
            user_pool_id=self.adt_user_pool.user_pool_id,
            group_name="analyst",
            description="ADT analysts — query/read access",
        )
        cognito.CfnUserPoolGroup(
            self, "AdtIntegratorGroup",
            user_pool_id=self.adt_user_pool.user_pool_id,
            group_name="integrator",
            description="ADT integrators — digital-thread write-back / onboarding commit access",
        )

        # Create demo user (ADT) via Lambda custom resource.
        #
        # The Lambda reads the password from the `adt-demo-password` Secrets
        # Manager secret AT RUNTIME (by name) rather than receiving it as a
        # synth-time property. That secret is the single source of truth that
        # operators retrieve post-deploy, so the Cognito password can never
        # drift from it. The secret is owned by ADTNetworkStack; this stack now
        # declares an explicit dependency on ADTNetworkStack (app.py) so the secret
        # exists before this custom resource runs. The `PasswordTrigger` property
        # below changes only when the password override changes, forcing the
        # custom resource to re-assert Cognito == secret (self-healing).
        demo_secret_name = "adt-demo-password"  # nosec B105 - Secrets Manager secret name, not credential value; see stacks/_demo_credentials.py
        user_lambda = lambda_.Function(
            self, "AdtCreateUserLambda",
            # No explicit function_name: this Lambda is RELOCATED from
            # ADTComputeStack (ADR-0033/F1). During the migration deploy the old
            # ADTComputeStack copy (which had an explicit physical name) still
            # exists when this new copy is created (ADTAgentCoreStack deploys before
            # ADTComputeStack removes it), so reusing that explicit name collides
            # ("already exists") — and manual pre-deletion would let ADTComputeStack
            # later delete the physical name it still owns, clobbering this one.
            # Auto-generated names are stack-unique, so both coexist safely and the
            # old one is removed cleanly when ADTComputeStack updates. Invoked by
            # CloudFormation (custom resource), never referenced by name.
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            code=lambda_.Code.from_inline(self._get_create_user_lambda_code()),
            environment={
                "USER_POOL_ID": self.adt_user_pool.user_pool_id,
                "DEMO_PASSWORD_SECRET_NAME": demo_secret_name,
            },
        )
        user_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
                     "cognito-idp:AdminDeleteUser", "cognito-idp:AdminAddUserToGroup"],
            resources=[self.adt_user_pool.user_pool_arn],
        ))
        # Read-only access to the demo password secret. Secrets Manager appends
        # a 6-char suffix to the ARN, hence the trailing wildcard.
        user_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{demo_secret_name}-*"],
        ))
        user_provider = cr.Provider(self, "AdtCreateUserProvider", on_event_handler=user_lambda)
        CustomResource(
            self, "AdtDemoUser",
            service_token=user_provider.service_token,
            properties={
                "Username": "ADT",
                "GroupName": "Admin",
                "UserPoolId": self.adt_user_pool.user_pool_id,
                "SecretName": demo_secret_name,
                # Stable change-trigger: a constant sentinel when no override
                # is set (redeploys do NOT re-run this resource); an explicit
                # -c demo_password override changes the value so the resource
                # re-runs and re-asserts the password from the secret. NOT used
                # as the password itself.
                "PasswordTrigger": get_demo_password(self),
            },
        )

    # ------------------------------------------------------------------
    def _create_edge_auth_client(self) -> None:
        """Edge-auth OAuth client (+ optional external IdP) on the adt-app pool.

        Relocated here from ADTComputeStack._create_edge_auth_cognito
        (2026-09-23, ADR-0070 amendment): under edge auth the browser's bearer
        is minted by THIS client, and the forwarded end-user token (ADR-0049)
        reaches the query gateway's CUSTOM_JWT authorizer — so the gateway
        must list the edge client in allowed_clients. The gateway lives in
        this stack while ADTComputeStack is DOWNSTREAM, so referencing the
        client id from there would be a backward cross-stack reference (a
        cycle) — the same reason the adt-app pool itself was relocated here
        (F1 / ADR-0033). ADTComputeStack reads ``edge_auth_client`` /
        ``edge_external_idp`` via its agentcore handle for the SAR app, the
        callback updater and the EDGE_AUTH_CLIENT_ID task env.

        Construction stays in the ONE Authorization@Edge home
        (stacks/_edge_auth.py facade — tripwire: test_edge_auth_one_home.py).
        MUST run before _build_gateway; both handles are always set (None when
        edge auth is off) so downstream readers need no getattr guards, and
        the off path leaves this template byte-identical.
        """
        self.edge_external_idp = None
        self.edge_auth_client = None
        if not self.edge_auth_enabled:
            return
        # Optional external OIDC provider on the adt-app pool (ADR-0072) —
        # created before the client, which lists and depends on it.
        self.edge_external_idp = (
            create_external_oidc_provider(
                self,
                construct_id="AdtEdgeExternalIdp",
                user_pool=self.adt_user_pool,
                config=self.external_oidc,
            )
            if self.external_oidc
            else None
        )
        # OAuth app client for Authorization@Edge. Placeholder callback/logout
        # URLs are intentional — the real CloudFront domain lives downstream in
        # ADTComputeStack; its callback-updater custom resource rewrites them
        # post-deploy (create_callback_updater).
        self.edge_auth_client = create_edge_oauth_client(
            self,
            construct_id="AdtEdgeAuthClient",
            user_pool=self.adt_user_pool,
            external_idp=self.edge_external_idp,
            oidc_only=bool(self.external_oidc and self.external_oidc.oidc_only),
        )

    def _get_create_user_lambda_code(self) -> str:
        return '''
import boto3, os
def handler(event, context):
    rt = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    pool_id = props.get("UserPoolId") or os.environ.get("USER_POOL_ID")
    username = props.get("Username", "ADT")
    group = props.get("GroupName", "Admin")
    client = boto3.client("cognito-idp")
    if rt == "Delete":
        try:
            client.admin_delete_user(UserPoolId=pool_id, Username=username)
        except Exception:
            pass
        return {"PhysicalResourceId": f"user-{username}"}
    # Read the password from Secrets Manager (the single source of truth that
    # operators retrieve post-deploy). Fail fast if it is missing rather than
    # falling back to a default — a wrong password would lock the demo user.
    secret_name = props.get("SecretName") or os.environ.get("DEMO_PASSWORD_SECRET_NAME")
    if not secret_name:
        raise RuntimeError("DEMO_PASSWORD_SECRET_NAME / SecretName not configured")
    password = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name).get("SecretString")
    if not password:
        raise RuntimeError(f"Secret {secret_name} has no SecretString value")
    try:
        client.admin_create_user(UserPoolId=pool_id, Username=username,
            TemporaryPassword=password, MessageAction="SUPPRESS")
    except client.exceptions.UsernameExistsException:
        pass
    client.admin_set_user_password(UserPoolId=pool_id, Username=username,
        Password=password, Permanent=True)
    try:
        client.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
    except Exception:
        pass
    return {"PhysicalResourceId": f"user-{username}", "Data": {"Username": username}}
'''
