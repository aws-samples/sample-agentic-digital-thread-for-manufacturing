# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ADT onboarding control-plane stack (additive and offline-synthesizable).

Builds the onboarding control plane on top of the existing session table:

* A least-privileged onboarding runtime IAM role with VPC egress, Secrets
  Manager read access for per-system credentials and DynamoDB read/write on
  the onboarding-session table. It deliberately has no
  ``bedrock-agentcore:Create*``, ``iam:PassRole``, ``agent-config/*`` write
  or ``states:StartExecution`` access. The ``adt_onboarding`` VPC runtime is
  created imperatively by ``deploy.sh`` so unsupported availability zones can
  be removed before an in-place deploy (ADR-0010).
* A separate onboarding gateway using ``CUSTOM_JWT(adt-app)`` with semantic
  search disabled. Cedar policy resources are planned but not attached because
  tool action IDs are unavailable until the target is registered. The single
  onboarding target is also created by ``deploy.sh`` after the runtime MCP URL
  exists. Any future Cedar attachment must use the real tool action IDs and the
  one-target commit-tier model in ADR-0033.
* The security group the onboarding runtime's ENIs use.

Provisioning a system (create its runtime and gateway target) runs in the app
(``onboard_chat_lifecycle.provision_system`` and the Onboard view's Provision),
not in this stack. The Step Functions provisioner that used to live here had no
caller and was removed on 2026-10-29.

Cross-stack direction is onboarding -> agentcore (and -> network) only, avoiding
any dependency cycle. ``app.py`` wires this as an always-on engine stack.

Not included here: the live ``onboard_chat`` cutover (P4), AdtTaskRole reduction
and retirement of the in-app ``agent_manager`` create path, and the query-gateway
authorizer flip (P5). The onboarding runtime and gateway target are deployment-
time resources managed by ``deploy.sh``; this stack remains synthesizable offline.
"""

from aws_cdk import (
    DefaultStackSynthesizer,
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ssm as ssm,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct

from stacks._account_arns import (
    account_ec2_arn,
    account_log_group_arn,
    account_log_stream_arn,
    agentcore_runtime_log_group_arn,
)
from stacks._graph_access import graph_data_plane_statement
from stacks._bedrock_access import mantle_inference_statements
from stacks._cross_account import grant_cross_account_assume

# Keep the table name in sync with agents.onboarding_session.TABLE_NAME.
ONBOARDING_SESSIONS_TABLE_NAME = "adt-onboarding-sessions"

# The onboarding agent runs the reused system-agent image under this reserved
# SYSTEM_ID (drives runtime_app._run_onboarding -> create_onboarding_server,
# which registers RUNTIME_TOOLS, the gateway subset of ONBOARD_TOOLS).
# Mirrors the digital-thread reserved-SYSTEM_ID pattern.
# The runtime is created IMPERATIVELY by deploy.sh (§2e); this constant documents
# the SYSTEM_ID contract deploy.sh must pass as an env var.
ONBOARDING_SYSTEM_ID = "onboarding"

# Outbound (gateway -> runtime) MCP scopes, matching the adt-m2m
# resource server (mcp/tools.read, mcp/tools.execute) — same scopes the dynamic
# system-agent targets request via agent_manager.
M2M_OUTBOUND_SCOPES = ["mcp/tools.read", "mcp/tools.execute"]

# NOTE (Cedar not attached): the onboarding gateway is CUSTOM_JWT with NO
# declarative Cedar policy engine. A declarative-in-stack Cedar layer is not
# possible pre-target-registration — AgentCore rejects a permit-all baseline as
# "Overly Permissive" and a specific forbid as "unrecognized action", because the
# gateway's tool action ids are only known AFTER its target is registered, and
# the target is registered IMPERATIVELY by agentic_digital_thread/deploy.sh (§2e) AFTER
# `cdk deploy`. The Cedar sources stay in the repo UNATTACHED
# (policies/onboarding_gateway.cedar + policies/onboarding_gateway_baseline.cedar);
# create the policy engine + policies and flip to ENFORCE after target sync,
# target-sync, scoped to the specific gateway ARN + real tool action ids.


class OnboardingStack(Stack):
    """ADT onboarding control plane (session store + runtime role + gateway)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack,
        agentcore_stack,
        **kwargs,
    ) -> None:
        super().__init__(
            scope, construct_id,
            description="ADT - onboarding runtime and gateway (CUSTOM_JWT; Cedar deferred)",
            **kwargs,
        )
        self.network_stack = network_stack
        self.agentcore = agentcore_stack

        self._create_session_table()
        self._create_onboarding_sg()
        # Create only the onboarding runtime IAM role here; the runtime is created
        # IMPERATIVELY by deploy.sh (§2e), mirroring the orchestrator (§2c), so it
        # can AZ-strip and deploy in-place on the existing VPC.
        self._create_runtime_role()
        # Create the onboarding CUSTOM_JWT gateway without Cedar attached. The
        # gateway TARGET is also
        # created imperatively by deploy.sh (needs the runtime MCP URL, unknown at
        # synth — like the query gateway's agent_manager-created targets).
        self._create_gateway()

        self._outputs()

    # ------------------------------------------------------------------ #
    # Existing onboarding session table
    # ------------------------------------------------------------------ #
    def _create_session_table(self) -> None:
        """DynamoDB table for durable onboarding session state.

        PK ``system_id``; ``ttl`` is the TTL attribute (epoch seconds). On-demand
        billing; destroyed on teardown (demo state, regenerated on re-onboard).
        """
        self.session_table = dynamodb.Table(
            self, "OnboardingSessionsTable",
            table_name=ONBOARDING_SESSIONS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="system_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        ssm.StringParameter(
            self, "OnboardingSessionsTableNameParam",
            parameter_name="/adt/onboarding-sessions-table",
            string_value=self.session_table.table_name,
        )

        CfnOutput(self, "OnboardingSessionsTableName", value=self.session_table.table_name)

    # ------------------------------------------------------------------ #
    # Onboarding runtime security group
    # ------------------------------------------------------------------ #
    def _create_onboarding_sg(self) -> None:
        """The onboarding runtime's security group (VPC mode).

        Separate from the ECS service SG: AgentCore/managed ENIs can take
        minutes to release after deletion, so keeping them out of the app SG
        avoids DELETE_FAILED on teardown (mirrors ADTComputeStack.AgentCoreRuntimeSg).
        The construct id and description are unchanged from when the removed
        provisioner Lambda shared it, so a deploy does not replace the group
        (``stacks/_db_access.py`` admits it to the demo databases).
        """
        vpc = self.network_stack.vpc
        self.onboarding_sg = ec2.SecurityGroup(
            self, "OnboardingRuntimeSg", vpc=vpc,
            description="Onboarding runtime + provisioner Lambda ENIs (VPC mode)",
            allow_all_outbound=True,
        )
        # Parity with the system-agent runtime SG: the AgentCore data plane
        # connects INTO the VPC-mode runtime through its ENIs. Without
        # VPC-local ingress the gateway's MCP initialize never reaches the
        # container and the target lands SYNCHRONIZE_UNSUCCESSFUL.
        self.onboarding_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443),
            "AgentCore data-plane ingress (VPC-mode runtime ENIs)",
        )

    # ------------------------------------------------------------------ #
    # 2.1R: onboarding runtime IAM role (runtime created IMPERATIVELY by deploy.sh)
    # ------------------------------------------------------------------ #
    def _create_runtime_role(self) -> None:
        """Least-privileged onboarding runtime IAM role.

        The onboarding AgentCore runtime (``adt_onboarding``) is NOT created
        declaratively here — it is created IMPERATIVELY by ``deploy.sh`` (§2e),
        mirroring the orchestrator runtime (§2c). A declarative ``CfnRuntime``
        pinned to all private subnets CANNOT self-heal unsupported availability
        zones: AgentCore VPC runtimes only launch in service-supported AZs, whose
        list is expressed as physical AZ *IDs* (use1-az1/az2/az4) while CDK subnets
        carry AZ *names* (us-east-1a/b/…), and the name->ID mapping is
        account-specific and unresolvable at synth. So a declarative runtime
        ``CREATE_FAILED``s in any VPC that includes an unsupported AZ (the parked
        AZ-fragility). deploy.sh instead attempts VPC mode with all private subnets
        and, on a ``CREATE_FAILED "availability zone"`` failure, strips the
        offending subnet(s) and retries (ADR-0010) — the proven per-system-agent
        path. This keeps the deploy IN-PLACE on the existing VPC with no AZ change.

        This method builds ONLY the runtime's IAM role (its ARN is a stack output
        consumed by deploy.sh as ``--role-arn``) and grants it pull on the shared
        system-agent image. The gateway TARGET pointing at the runtime is likewise
        created imperatively (deploy.sh §2e), because it needs the runtime MCP URL
        that is unknown at synth — exactly like the query gateway's system-agent
        targets are created by ``agent_manager``, not declaratively.

        NB: deliberately NO bedrock-agentcore:Create*, NO iam:PassRole, NO
        agent-config/* secret writes: provisioning runs in the app, under the
        app task role. The role grants only what the eight exported tools call: secret, graph
        and saved agent-model READS, the SiteWise reads, the session table, and
        no secret write, graph write or provisioning (the call inventory
        is in tests/test_onboarding_runtime_stack.py).
        """
        self.runtime_role = iam.Role(
            self, "OnboardingRuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Onboarding agent runtime - least-privileged (no Create*/PassRole)",
            inline_policies={
                "OnboardingRuntimePolicy": iam.PolicyDocument(statements=[
                    # Statement ORDER is template-visible: the original two
                    # helpers keep their order, the ADR-0069 graph statement between.
                    *self._runtime_model_and_secret_statements(),
                    *self._runtime_graph_statements(),
                    *self._runtime_session_and_network_statements(),
                    *self._runtime_agent_model_statements(),
                ]),
            },
        )
        # No checkov skip on this role: the Workshop Studio scanner counts
        # every skip in its rule set as a finding (stacks/_checkov_skips.py),
        # and the ENI and log writes above are scoped to the account.
        self._grant_runtime_image_pull()

    def _runtime_agent_model_statements(self) -> list[iam.PolicyStatement]:
        """The configured system-agent model (the Admin > Settings save):
        map_fields and resolve_identities try it first (``db.bedrock_call``
        via ``agents/model_config.configured_agent_model``). Read-only, that
        one parameter. A GPT-5.x agent or
        orchestrator model runs on Bedrock Mantle (ADR-0055), with the grant
        the app task role and the query runtime role carry."""
        return [iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:{self.partition}:ssm:{self.region}:{self.account}"
                ":parameter/adt/llm-agent-model",
            ],
        ), *mantle_inference_statements(self)]

    def _runtime_model_and_secret_statements(self) -> list[iam.PolicyStatement]:
        region = self.region
        account = self.account
        return [
            # The onboarding agent/tools invoke a Bedrock model.
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                ],
            ),
            # Secrets Manager READ of the secret a System node points at:
            # discover and test_connection resolve a registration's own
            # adt/conn/<id> and a system's adt/systems/<id>. No tool this
            # runtime exports writes, describes, restores or deletes a secret
            # (registration, store_credentials and removal run in the app).
            # NOT agent-config/* (provisioning runs in the app).
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:adt/conn/*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:adt/systems/*",
                ],
            ),
            # Same-account SiteWise (Direct) reads: test_connection lists asset
            # models; discover lists models, their properties and assets, and
            # reads one current value per model. Cross-account systems read
            # through the assumed read-only role instead (sts:AssumeRole,
            # _grant_runtime_image_pull). These List*/Describe*/Get* calls are
            # granted on "*", as on the app task role and the query runtime
            # role.
            iam.PolicyStatement(
                actions=[
                    "iotsitewise:ListAssetModels",
                    "iotsitewise:DescribeAssetModel",
                    "iotsitewise:ListAssetModelProperties",
                    "iotsitewise:ListAssets",
                    "iotsitewise:DescribeAsset",
                    "iotsitewise:GetAssetPropertyValue",
                ],
                resources=["*"],
            ),
            # READ-ONLY on the CDK-generated demo DB owner secrets. The
            # onboarding runtime's discover tool resolves records-first
            # connections (onboard_agent.discover →
            # connection_resolver.resolve_connection → _discover_postgres
            # fetches the node's secret_arn). Demo SQL System nodes point
            # at the seed-DB secret ("AdtDbSecret…", capital A) or the ERP
            # owner secret ("DatabaseSecret…"); IAM matching is case-sensitive,
            # so adt/* above does not cover them. Kept as a SEPARATE
            # statement so the write actions above are never granted on
            # owner secrets. Secret objects are deliberately not imported
            # (ADR-0030: demo stacks stay removable on a flag-less deploy).
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:AdtDbSecret*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:DatabaseSecret*",
                ],
            ),
        ]

    def _runtime_graph_statements(self) -> list[iam.PolicyStatement]:
        """Knowledge-graph data plane for the onboarding runtime (ADR-0069).

        The ``adt_onboarding`` runtime carries the in-process wizard's
        ``ONBOARD_TOOLS`` code and executes the tools it exports
        (``onboarding_runtime_server.RUNTIME_TOOLS``), and this role carried NO
        ``neptune-graph`` grant — so onboarding-chat catalog reads always
        failed and ``neptune_query``'s ``[]``-on-failure degrade surfaced as
        false "no systems registered" answers.

        READ tier only (no write, no delete), grounded in a static call
        inventory of the eight exported tools, not guessed:
        ``get_system_info``, ``assess_vocabulary_fit``, ``propose_concepts``,
        ``map_fields``, ``resolve_identities`` and ``verify_system`` read the
        catalog, the concept vocabulary and identity evidence, and
        ``test_connection`` and ``discover`` read the System node a connection
        resolves from. None of them runs a CREATE, MERGE, SET or DELETE: the
        System-node writes (provision, a stored credential's pointer, a
        connection edit) and ``reset_system``'s ``DETACH DELETE`` all run in
        the app.

        ``graph/*`` (account/Region-bounded), not the graph ARN: the graph
        lives in ADTDatabaseStack and this stack deliberately has no edge to
        it — same posture as the shared AgentCore runtime role.
        """
        return [graph_data_plane_statement(
            region=self.region, account=self.account, graph_id=None)]

    def _runtime_session_and_network_statements(self) -> list[iam.PolicyStatement]:
        return [
            # discover creates the onboarding session; map_fields and
            # resolve_identities move it on (DDB on the session table only).
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                ],
                resources=[self.session_table.table_arn],
            ),
            # No states:StartExecution: provisioning runs in the app, and
            # this stack has no state machine.
            # VPC ENIs: AgentCore attaches the runtime with its own
            # service-linked role (AWSServiceRoleForBedrockAgentCoreNetwork),
            # not this role. The ENI writes stay scoped to this account and
            # Region; the describe has no resource type and stays on "*".
            iam.PolicyStatement(
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
            # CloudWatch Logs: AgentCore names the log group at run time, so
            # the writes cover log groups in this account and Region.
            # DescribeLogGroups has no resource type and stays on "*".
            iam.PolicyStatement(
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
            # AgentCore attaches a resource policy to the runtime's own log
            # group with this role as it creates the runtime (the AWS
            # execution-role policy for AgentCore Runtime grants it).
            iam.PolicyStatement(
                actions=["logs:PutResourcePolicy"],
                resources=[agentcore_runtime_log_group_arn(self)],
            ),
        ]

    def _grant_runtime_image_pull(self) -> None:
        # The imperatively-created runtime pulls the shared system-agent image.
        self.agentcore.agent_repo.grant_pull(self.runtime_role)
        # Cross-account data onboarding: the onboarding runtime runs Athena /
        # SiteWise DISCOVERY against the data account's read-only role, so it
        # needs the SAME caller-side sts:AssumeRole grant as the app task role
        # and the query runtime role (ONE helper; no-op when unset).
        grant_cross_account_assume(self, self.runtime_role)
        # deploy.sh §2e actually runs the runtime on the ORCHESTRATOR image
        # (ADTComputeStack.OrchestratorRuntimeImageUri), which is a CDK
        # container ASSET — it lives in the bootstrap assets repo, not
        # agent_repo. Grant pull there too, by the deterministic bootstrap
        # name (this stack deploys BEFORE ADTComputeStack, so the asset object
        # can't be referenced without a dependency cycle). Without this the
        # runtime 403s on image pull, never answers MCP initialize, the
        # gateway target lands SYNCHRONIZE_UNSUCCESSFUL, and the tool-less
        # onboarding agent fabricates catalog answers.
        assets_repo_arn = self.format_arn(
            service="ecr", resource="repository",
            resource_name=f"cdk-{DefaultStackSynthesizer.DEFAULT_QUALIFIER}"
                          f"-container-assets-{self.account}-{self.region}",
        )
        self.runtime_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
                     "ecr:GetDownloadUrlForLayer"],
            resources=[assets_repo_arn],
        ))

    # ------------------------------------------------------------------ #
    # Onboarding CUSTOM_JWT gateway (Cedar not attached) + single M2M target
    # ------------------------------------------------------------------ #
    def _create_gateway_role(self) -> iam.Role:
        """Gateway IAM role (mirrors ADTAgentCoreStack.gateway_role)."""
        region = self.region
        account = self.account
        return iam.Role(
            self, "OnboardingGatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "GatewayIdentityPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:GetWorkloadAccessToken"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                            f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:GetResourceOauth2Token"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                            f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
                            f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default",
                            f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default/oauth2credentialprovider/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore*",
                            f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity*",
                        ],
                    ),
                ]),
            },
        )

    def _gateway_allowed_clients(self) -> list[str]:
        """CUSTOM_JWT allowed_clients on the single adt-app issuer.

        BOTH the interactive login client (adt-app-client) and the service
        client (adt-app-service-v2) must be allowed or those callers are
        rejected (R-E); never the retired adt-app-service (ADR-0071 amendment
        2026-11-07). Under edge auth (ADR-0070) the signed-in user's bearer is minted
        by the EDGE client (owned by ADTAgentCoreStack) and forwarded per
        ADR-0049 — the in-app onboarding agent presents it here too. Off ⇒
        absent, keeping the template byte-identical.
        """
        clients = [
            self.agentcore.adt_app_client.user_pool_client_id,
            self.agentcore.adt_service_client.user_pool_client_id,
        ]
        if self.agentcore.edge_auth_client is not None:
            clients.append(self.agentcore.edge_auth_client.user_pool_client_id)
        return clients

    def _create_gateway(self) -> None:
        """A SEPARATE onboarding gateway: CUSTOM_JWT(adt-app) (Cedar deferred to
        target registration) plus one target via the adt-m2m provider.

        The query gateway (ADTAgentCoreStack.gateway) is UNCHANGED (still AWS_IAM,
        SEMANTIC) — this is a greenfield second gateway, not a flip.
        """
        gateway_role = self._create_gateway_role()

        # --- Cedar planned but not attached; no declarative policy engine here ---
        # The gateway below is CUSTOM_JWT(adt-app) with NO policy engine. A
        # declarative-in-stack Cedar layer is not possible pre-target-
        # registration (AgentCore rejects permit-all as "Overly Permissive" and
        # a specific forbid as "unrecognized action"); the single onboarding
        # target is registered IMPERATIVELY by deploy.sh (§2e) AFTER `cdk deploy`.
        # Create the engine + policies (scoped to the specific gateway ARN + real
        # tool action ids) and flip to ENFORCE after target sync. The
        # Cedar sources stay UNATTACHED in policies/onboarding_gateway*.cedar.

        # --- Gateway: CUSTOM_JWT(adt-app), semantic search OFF ---------------
        self.gateway = agentcore.CfnGateway(
            self, "OnboardingGateway",
            name="adt-onboard-gw",
            protocol_type="MCP",
            authorizer_type="CUSTOM_JWT",
            role_arn=gateway_role.role_arn,
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=self.agentcore.adt_app_discovery_url,
                    # Single-issuer (adt-app): BOTH the interactive login client
                    # (adt-app-client) and the service client (adt-app-service-v2)
                    # must be allowed or those callers are rejected (R-E).
                    allowed_clients=self._gateway_allowed_clients(),
                ),
            ),
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    # Semantic search OFF — the onboarding toolset is small/fixed.
                    # search_type is deliberately omitted (NOT "SEMANTIC" like the
                    # query gateway). No policy engine is attached (see NOTE
                    # at the top of this module).
                    supported_versions=["2025-03-26"],
                ),
            ),
        )

        # NB: the onboarding gateway TARGET (gateway -> runtime, via the
        # adt-m2m provider) is NOT declared here. A target needs the
        # runtime MCP invocation URL, which is only known once the runtime ARN
        # exists — and the runtime is created IMPERATIVELY by deploy.sh (§2e) so it
        # can AZ-strip and deploy in-place. deploy.sh therefore also creates/syncs
        # the single onboarding target after the runtime is READY, exactly like the
        # query gateway's system-agent targets are created by agent_manager (never
        # declaratively).
        #
        # What protects onboarding today, with no policy engine attached: the
        # app's integrator/Admin Cognito-group check on every /api/onboard/*
        # route (onboard.require_onboard_access), and the tool set the target
        # serves. The onboarding runtime exports only the read/explore tools
        # (onboarding_runtime_server.RUNTIME_TOOLS, which drops
        # onboard_chat_lifecycle.NOT_ON_GATEWAY); provision, reset, connect and
        # credential storage run in the app, behind that check.

    # ------------------------------------------------------------------ #
    def _outputs(self) -> None:
        # Networking + role for the IMPERATIVE runtime create in deploy.sh (§2e),
        # mirroring the orchestrator's AgentCoreRuntimeSgId / SubnetIds / RuntimeRoleArn.
        vpc = self.network_stack.vpc
        CfnOutput(self, "OnboardingRuntimeRoleArn", value=self.runtime_role.role_arn,
            description="IAM role ARN for the adt_onboarding runtime (deploy.sh --role-arn)")
        CfnOutput(self, "OnboardingRuntimeSgId", value=self.onboarding_sg.security_group_id,
            description="Security group for the adt_onboarding runtime (VPC mode)")
        CfnOutput(self, "OnboardingRuntimeSubnetIds",
            value=",".join(s.subnet_id for s in vpc.private_subnets),
            description="Private subnet IDs for the adt_onboarding runtime (VPC mode, AZ-stripped by deploy.sh)")
        # Gateway (declarative) id/url consumed by deploy.sh to create/sync the
        # onboarding gateway target and set the runtime env.
        CfnOutput(self, "OnboardingGatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "OnboardingGatewayUrl", value=self.gateway.attr_gateway_url)
