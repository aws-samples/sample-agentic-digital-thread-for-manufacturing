# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Dashboard Stack for ERP Beverage Simulation System

This stack creates the Streamlit dashboard infrastructure:
- ECR repository for dashboard container image
- ECS Fargate cluster and task definition
- ECS service running the dashboard
- Internal Application Load Balancer (HTTP:80)
- CloudFront distribution (VPC origin over the internal ALB) + AWS WAF
- Cognito User Pool for authentication
- Pre-created demo users (erp, ADT); each gets the password generated into the
  Secrets Manager secret adt-demo-password, which operators retrieve post-deploy
- IAM permissions for Secrets Manager access

Requirements: 3.5, 3.7, 3.9, 3.10, 10.1-10.15
"""

import json
import os
import re
import time
from typing import Optional

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    CustomResource,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

from stacks import _workshop_story
from stacks._checkov_skips import checkov_skip
from stacks._os_patch import patched_image_options
from stacks._cognito_domain import (
    UNRESOLVED_ACCOUNT_PLACEHOLDER,
    account_is_concrete,
    pin_domain_to_account,
)
from stacks._edge_auth import (
    STREAMLIT_STATIC_PATH,
    ExternalOidcConfig,
    add_demo_user,
    create_callback_updater,
    create_demo_user_provider,
    create_edge_oauth_client,
    create_external_oidc_provider,
    create_standard_web_acl,
    deploy_edge_auth,
    edge_behavior_parts,
    edge_http_headers,
    public_bundle_behavior,
)


class DashboardStack(Stack):
    """
    Dashboard Stack creating ECS Fargate service with ALB, HTTPS, and Cognito auth.
    
    This stack depends on ADTNetworkStack for VPC and security groups,
    and DatabaseStack for database secret.
    
    Requirements: 3.5, 3.9, 10.1-10.15
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        network_stack,
        database_stack,
        ui_domain_name=None,
        external_oidc: Optional[ExternalOidcConfig] = None,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - ERP Streamlit dashboard (ECS Fargate)", **kwargs)

        self.network_stack = network_stack
        self.database_stack = database_stack
        # Optional custom-domain FQDN (erp.<domain>) — consumed by the
        # callback updater so Cognito sign-in works through the alias too.
        # The alias/certificate/DNS themselves are wired additively from
        # app.py via stacks/_custom_domain.py::wire_subdomain_alias.
        self.ui_domain_name = ui_domain_name
        # Optional external OIDC identity provider for the hosted UI (app.py
        # resolves the sticky oidc_* trio; None ⇒ template unchanged).
        self.external_oidc = external_oidc

        dashboard_config = self.node.try_get_context("dashboard") or {}
        self.cpu = dashboard_config.get("cpu", 512)
        self.memory_mb = dashboard_config.get("memory_mb", 1024)
        self.desired_count = dashboard_config.get("desired_count", 1)

        # Create the security groups this stack owns (ecs_sg, alb_sg) — moved out
        # of ADTNetworkStack so the base engine-only deploy carries no demo SGs.
        # Must run before _create_alb (uses alb_sg) and _create_ecs_service
        # (uses ecs_sg).
        self._create_security_groups()

        self._create_ecr_repository()
        self._create_ecs_cluster()
        self._create_task_definition()
        # Workshop OT feed: SiteWise degradation publisher, invoked by the
        # dashboard's "Start machine degradation feed" button. Lives in this
        # stack because it is demo/workshop-facing and simulator-gated —
        # exactly like the dashboard itself — and never touches the engine.
        self._create_sitewise_feed_lambda()
        self._create_cognito_user_pool()
        self._create_alb()
        self._create_ecs_service()
        # The SAR Authorization@Edge app must exist before the distribution so
        # its Lambda@Edge version ARNs can be wired into the L2 behaviors.
        self._create_edge_auth()
        self._create_cloudfront()
        self._restrict_alb_ingress_to_cloudfront()
        self._create_callback_updater()
        self._create_default_user()
        self._create_post_deployment_reset()
        self._create_exports()

    def _create_security_groups(self) -> None:
        """
        Create the security groups owned by this stack (ecs_sg, alb_sg).

        Moved out of ADTNetworkStack so the base engine-only deploy carries no
        demo networking. Preserves the original ports/peers/allow_all_outbound.

        - ecs_sg: ECS Fargate dashboard tasks (all-outbound for Secrets Manager).
        - alb_sg: internal ALB (no all-outbound). The :80 CloudFront ingress is
          still added later as a standalone CfnSecurityGroupIngress in
          _restrict_alb_ingress_to_cloudfront (scoped to the CloudFront
          VPC-origin service SG), exactly as before.

        Rules defined here:
        - ecs_sg ingress  <- alb_sg  on 8501 (ALB -> Streamlit)
        - alb_sg egress   -> ecs_sg  on 8501
        - aurora_sg ingress <- ecs_sg on 5432 (cross-stack): aurora_sg lives in
          ADTERPDatabaseStack. This stack depends on that stack, so the rule is
          added here as a standalone CfnSecurityGroupIngress against the imported
          aurora_sg id (adding it in the database stack would create a cycle).

        Requirements: 7.3, 7.4
        """
        vpc = self.network_stack.vpc

        # ECS Security Group
        self.ecs_sg = ec2.SecurityGroup(
            self,
            "ECSSG",
            vpc=vpc,
            description="Security group for ECS Fargate dashboard tasks",
            allow_all_outbound=True,  # Needs internet for Secrets Manager
        )

        # ALB Security Group
        self.alb_sg = ec2.SecurityGroup(
            self,
            "ALBSG",
            vpc=vpc,
            description="Security group for Application Load Balancer",
            allow_all_outbound=False,
        )

        # Allow HTTP traffic from ALB to ECS on port 8501 (Streamlit)
        self.ecs_sg.add_ingress_rule(
            peer=self.alb_sg,
            connection=ec2.Port.tcp(8501),
            description="Allow HTTP from ALB to Streamlit",
        )

        # Allow ALB outbound to ECS on port 8501
        self.alb_sg.add_egress_rule(
            peer=self.ecs_sg,
            connection=ec2.Port.tcp(8501),
            description="Allow outbound to ECS",
        )

        # Cross-stack rule: Aurora (owned by ADTERPDatabaseStack) must allow
        # PostgreSQL from this stack's ecs_sg. Implemented as a standalone
        # CfnSecurityGroupIngress in THIS stack so the rule resource lives here
        # (this stack already depends on the database stack), referencing the
        # imported aurora_sg id and the local ecs_sg — no dependency cycle.
        ec2.CfnSecurityGroupIngress(
            self,
            "AuroraIngressFromEcs",
            group_id=self.database_stack.aurora_sg.security_group_id,
            ip_protocol="tcp",
            from_port=5432,
            to_port=5432,
            source_security_group_id=self.ecs_sg.security_group_id,
            description="Allow PostgreSQL from ECS",
        )

    def _create_ecr_repository(self) -> None:
        self.ecr_repository = ecr.Repository(
            self,
            "DashboardEcrRepository",
            repository_name="erp-dashboard",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_images=True,
            image_scan_on_push=True,
        )

    def _create_ecs_cluster(self) -> None:
        self.cluster = ecs.Cluster(
            self,
            "DashboardCluster",
            cluster_name="erp-dashboard-cluster",
            vpc=self.network_stack.vpc,
            container_insights=True,
        )

    def _create_task_definition(self) -> None:
        self.task_execution_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        self.database_stack.db_secret.grant_read(self.task_role)

        # Add EventBridge permissions for simulation control (pause/resume)
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "events:EnableRule",
                    "events:DisableRule",
                    "events:DescribeRule",
                    "events:ListRules",
                ],
                resources=["arn:aws:events:*:*:rule/erp-simulation-*"],
            )
        )

        # Add Lambda invoke permissions for reset/seed operations
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:InvokeFunction",
                ],
                resources=[
                    "arn:aws:lambda:*:*:function:erp-*",
                ],
            )
        )

        self.log_group = logs.LogGroup(
            self,
            "DashboardLogGroup",
            log_group_name="/ecs/erp-dashboard",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_WEEK,
        )

        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "DashboardTaskDefinition",
            family="erp-dashboard",
            cpu=self.cpu,
            memory_limit_mib=self.memory_mb,
            execution_role=self.task_execution_role,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.ARM64,
            ),
        )

        project_root = os.path.join(os.path.dirname(__file__), "..")
        dockerfile_path = os.path.join("docker", "Dockerfile.dashboard")

        self.container = self.task_definition.add_container(
            "DashboardContainer",
            container_name="erp-dashboard",
            image=ecs.ContainerImage.from_asset(
                directory=project_root,
                file=dockerfile_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
                **patched_image_options(),
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="dashboard",
                log_group=self.log_group,
            ),
            environment={
                "DB_SECRET_NAME": self.database_stack.db_secret.secret_arn,
                "REGION": self.region,
                "USE_SECRETS_MANAGER": "true",
            },
            port_mappings=[
                ecs.PortMapping(
                    container_port=8501,
                    protocol=ecs.Protocol.TCP,
                )
            ],
        )

    def _create_console_login_secret(self) -> None:
        """The SQL console's own database login (erp_simulation/console_login.py).

        The console never runs operator SQL as the admin login: the dashboard
        creates a SELECT-only role from this secret on the first console query
        and connects as it. Generated per account, no punctuation so it needs
        no quoting. No fixed name: the task reads it by ARN, and the generated
        name matches none of the secret-name wildcards other roles are granted.
        """
        self.console_db_secret = secretsmanager.Secret(
            self,
            "ConsoleLoginSecret",
            description="ERP dashboard SQL console login (SELECT-only database role)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "erp_console"}',  # nosec B106 - JSON template, not a credential value
                generate_string_key="password",
                exclude_punctuation=True,
                include_space=False,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.console_db_secret.grant_read(self.task_role)
        self.container.add_environment(
            "CONSOLE_DB_SECRET_NAME", self.console_db_secret.secret_arn)
        why = ("a generated per-account login for the dashboard's SELECT-only console role; "
               "the dashboard sets it on the role at its first console query, so rotate by "
               "replacing the secret value and restarting the task")
        NagSuppressions.add_resource_suppressions(self.console_db_secret, [NagPackSuppression(
            id="AwsSolutions-SMG4", reason=f"Demo sample, no automatic rotation: {why}.")])
        checkov_skip(self.console_db_secret, [
            ("CKV2_AWS_57", f"No automatic rotation in this demo sample: {why}."),
            ("CKV_AWS_149", "Encrypted with the AWS-managed Secrets Manager key. The "
                            "secret only opens a SELECT-only console login inside the "
                            "VPC; a customer-managed key adds a per-account key and "
                            "policy surface with no benefit for this demo workload."),
        ])

    def _create_sitewise_feed_lambda(self) -> None:
        """Workshop OT feed: Lambda that backfills the CMX-441 degradation
        trend into AWS IoT SiteWise (lambda-sitewise-feed/handler.py).

        Placed in this stack (not a new one, not the engine stacks) because:
        - it deploys with --simulator, the same gate as the dashboard whose
          button invokes it — no app.py change needed;
        - it is demo/workshop-facing; the ADT engine never references it.

        Permissions are granted in _grant_sitewise_feed_permissions.

        Not on a live workshop synth (ADR-0092 decision 6 follow-up): there
        the story site's OPC UA server, read by SFC, is the only SiteWise
        writer, so this Lambda is not packaged and the dashboard container is
        told to show the OT row's caption instead of the button.
        """
        if _workshop_story.from_node(self.node) == "live":
            self.sitewise_feed_lambda = None
            self.container.add_environment(
                _workshop_story.OT_FEED_ENV_NAME, _workshop_story.OT_FEED_FROM_OPCUA)
            return
        project_root = os.path.join(os.path.dirname(__file__), "..")
        self.sitewise_feed_lambda = lambda_.Function(
            self,
            "SitewiseFeedLambda",
            function_name="erp-sitewise-degradation-feed",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            timeout=Duration.minutes(10),
            memory_size=256,
            code=lambda_.Code.from_asset(os.path.join(project_root, "lambda-sitewise-feed"),
                                         exclude=["__pycache__", "*.pyc"]),
            description="Backfills the CMX-441 degrading vibration trend into AWS IoT SiteWise (workshop OT chapter)",
        )
        self._grant_sitewise_feed_permissions()

    def _grant_sitewise_feed_permissions(self) -> None:
        """IAM for the SiteWise feed Lambda + dashboard invoke grant.

        Least privilege as the SiteWise API allows:
        - Discovery statement uses Resource "*": the Lambda resolves the
          asset/model BY NAME (no UUIDs exist at deploy time), and the
          List* APIs are account-level operations not scopable to an ARN.
          All four actions are read-only.
        - Write/create statement is bounded to this account+region's
          asset/* and asset-model/* ARNs: asset ids are runtime-resolved
          UUIDs, so a narrower resource is impossible at synth time.

        The dashboard task role's existing invoke grant on
        arn:aws:lambda:*:*:function:erp-* already covers this function
        (named erp-sitewise-degradation-feed); an exact-function grant is
        added anyway so the intent survives if that wildcard is narrowed.
        """
        self.sitewise_feed_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="SitewiseNameDiscoveryReadOnly",
                actions=[
                    "iotsitewise:ListAssetModels",
                    "iotsitewise:ListAssets",
                    "iotsitewise:DescribeAsset",
                    "iotsitewise:DescribeAssetModel",
                ],
                resources=["*"],  # read-only name->id discovery; List* not ARN-scopable
            )
        )
        self.sitewise_feed_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="SitewiseWriteAndCreateAsset",
                actions=[
                    "iotsitewise:BatchPutAssetPropertyValue",
                    "iotsitewise:CreateAsset",
                ],
                resources=[
                    f"arn:aws:iotsitewise:{self.region}:{self.account}:asset/*",
                    f"arn:aws:iotsitewise:{self.region}:{self.account}:asset-model/*",
                ],
            )
        )
        # Exact-function invoke grant for the dashboard button (see docstring).
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeSitewiseFeedLambda",
                actions=["lambda:InvokeFunction"],
                resources=[self.sitewise_feed_lambda.function_arn],
            )
        )

    def _create_cognito_user_pool(self) -> None:
        self.user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            user_pool_name="erp-dashboard-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(
                username=True,
                email=True,
            ),
            auto_verify=cognito.AutoVerifiedAttrs(
                email=False,
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self._create_cognito_domain()
        self._create_auth_client()

    def _create_cognito_domain(self) -> None:
        """Create the stable account-resolved hosted-UI domain."""
        # Concrete accounts keep the historical last-12-digit literal; an
        # account-agnostic workshop synth uses a non-numeric stand-in that
        # pin_domain_to_account swaps for AWS::AccountId at deploy time.
        suffix = (
            self.account[-12:]
            if account_is_concrete(self.account)
            else UNRESOLVED_ACCOUNT_PLACEHOLDER
        )
        domain_prefix = f"erp-dashboard-{suffix}"
        self.user_pool_domain = self.user_pool.add_domain(
            "DashboardUserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=domain_prefix,
            ),
        )
        self.user_pool_domain_value = pin_domain_to_account(
            self.user_pool_domain, domain_prefix, self.account
        )

    def _create_auth_client(self) -> None:
        """Create the edge OAuth client at its historical user-pool scope."""
        # Optional external OIDC provider on this pool (ADR-0072) — listed by
        # the client below so the hosted UI offers both sign-in choices.
        self.external_idp = (
            create_external_oidc_provider(
                self.user_pool,
                construct_id="DashboardExternalIdp",
                user_pool=self.user_pool,
                config=self.external_oidc,
            )
            if self.external_oidc
            else None
        )
        self.auth_client = create_edge_oauth_client(
            self.user_pool,
            construct_id="DashboardAuthClient",
            user_pool=self.user_pool,
            external_idp=self.external_idp,
            oidc_only=bool(self.external_oidc and self.external_oidc.oidc_only),
        )

    def _create_edge_auth(self) -> None:
        """Deploy the shared auth-only SAR application and edge versions."""
        self.edge_headers = edge_http_headers(allow_websockets=True)
        self.edge_auth, self.edge_versions = deploy_edge_auth(
            self,
            user_pool=self.user_pool,
            client=self.auth_client,
            http_headers=self.edge_headers,
        )

    def _create_callback_updater(self) -> None:
        """Patch OAuth callbacks after the distribution token resolves."""
        create_callback_updater(
            self,
            function_name="erp-dashboard-callback-updater",
            user_pool=self.user_pool,
            client=self.auth_client,
            distribution=self.distribution,
            custom_domain=self.ui_domain_name,
        )

    def _create_alb(self) -> None:
        # Internal ALB — public access is only via the CloudFront VPC origin
        # (see _create_cloudfront). CloudFront terminates TLS; the ALB serves
        # plain HTTP on port 80 in-VPC. Mirrors adt_compute_stack's internal ALB.
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "DashboardALB",
            # No explicit load_balancer_name: a fixed name collides on replacement
            # (CFN create-before-delete) since the old ALB still exists. Let CDK
            # generate a unique name so stack updates that replace the ALB succeed.
            vpc=self.network_stack.vpc,
            internet_facing=False,
            security_group=self.alb_sg,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        self.target_group = elbv2.ApplicationTargetGroup(
            self,
            "DashboardTargetGroup",
            # No explicit target_group_name (same replacement-collision reason as the ALB).
            vpc=self.network_stack.vpc,
            port=8501,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200,302",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )

        # Single plain HTTP:80 listener forwarding to the Streamlit target group.
        # open=False: do NOT auto-open the ALB SG to 0.0.0.0/0. Reachability is
        # granted solely by the service-SG :80 ingress rule added in
        # _restrict_alb_ingress_to_cloudfront (scoped to the CloudFront
        # VPC-origin service SG), so only the in-VPC CloudFront VPC origin ENIs
        # can reach the ALB.
        self.http_listener = self.alb.add_listener(
            "HTTPListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
            default_target_groups=[self.target_group],
        )
        _origin_hop = (
            "The ALB is internal and its security group admits only the "
            "CloudFront VPC origin. Viewers connect to CloudFront over HTTPS; "
            "the hop from CloudFront to this in-VPC origin is HTTP by design, "
            "the same as the ADT app's load balancer."
        )
        checkov_skip(self.http_listener, [("CKV_AWS_2", _origin_hop), ("CKV_AWS_103", _origin_hop)])

    def _create_ecs_service(self) -> None:
        # The task's SQL console login, wired before the service runs the task.
        self._create_console_login_secret()
        self.service = ecs.FargateService(
            self,
            "DashboardService",
            service_name="erp-dashboard-service",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=self.desired_count,
            security_groups=[self.ecs_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            assign_public_ip=False,
            health_check_grace_period=Duration.seconds(60),
        )

        self.service.attach_to_application_target_group(self.target_group)

    def _create_cloudfront(self) -> None:
        """CloudFront VPC origin with shared WAF and Authorization@Edge."""
        web_acl = create_standard_web_acl(
            self,
            construct_id="ERPDashboardWebAcl",
            metric_name="ERPDashboardWebAcl",
            bare_callback_redirect_headers=self.edge_headers,
        )
        alb_origin = origins.VpcOrigin.with_application_load_balancer(
            self.alb,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            http_port=80,
            read_timeout=Duration.seconds(120),
            keepalive_timeout=Duration.seconds(60),
            vpc_origin_name=f"erp-dashboard-alb-origin-{self.region}",
        )
        _, default, additional = edge_behavior_parts(
            origin=alb_origin,
            versions=self.edge_versions,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
        )
        # Streamlit's public frontend bundle bypasses edge auth so the first
        # load after sign-in is not a 200+ invocation Lambda@Edge burst
        # (blank first page on a fresh account); see public_bundle_behavior.
        additional[STREAMLIT_STATIC_PATH] = public_bundle_behavior(origin=alb_origin)
        self.distribution = cloudfront.Distribution(
            self,
            "ERPDashboardDistribution",
            web_acl_id=web_acl.attr_arn,
            default_behavior=default,
            additional_behaviors=additional,
        )
        self.distribution.node.add_dependency(self.edge_auth)

    def _restrict_alb_ingress_to_cloudfront(self) -> None:
        """Restrict the internal ALB SG to ONLY the CloudFront VPC-origin
        service-managed security group (AWS-documented Option 2 — tightest).

        CloudFront VPC-origin traffic always arrives from the account/VPC-specific
        service-managed SG named `CloudFront-VPCOrigins-Service-SG`, which AWS
        creates automatically once a VPC origin exists. Its id is NOT stable
        across accounts/VPCs, so we look it up at deploy time and add a single
        :80 ingress rule sourced from it. There is no broad VPC-CIDR :80 rule on
        the ALB SG, so nothing else in the VPC can reach the internal ALB.

        The ingress is a standalone CfnSecurityGroupIngress rather than
        alb_sg.add_ingress_rule because the source (the CloudFront service SG) is
        only known at deploy time via the lookup below — it does not exist until
        the VPC origin is created. Using CfnSecurityGroupIngress lets us gate the
        rule on that deploy-time lookup / distribution dependency.
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
                "erp-cloudfront-vpcorigins-service-sg-lookup"
            ),
        )
        service_sg_lookup = cr.AwsCustomResource(
            self,
            "CloudFrontVpcOriginSgLookup",
            on_create=service_sg_call,
            on_update=service_sg_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
            install_latest_aws_sdk=False,  # only built-in describeSecurityGroups; avoids deploy-time npm + non-reproducible builds
        )
        # The service SG only exists after the VPC origin (distribution) is created.
        service_sg_lookup.node.add_dependency(self.distribution)

        service_sg_id = service_sg_lookup.get_response_field("SecurityGroups.0.GroupId")

        alb_ingress = ec2.CfnSecurityGroupIngress(
            self,
            "AlbIngressFromCloudFrontServiceSg",
            group_id=self.alb_sg.security_group_id,
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            source_security_group_id=service_sg_id,
            description="Allow HTTP:80 ONLY from CloudFront VPC-origin service SG",
        )
        # Create the ingress after the VPC origin exists (service SG present) and
        # after the lookup resolves the service SG id.
        alb_ingress.node.add_dependency(self.distribution)
        alb_ingress.node.add_dependency(service_sg_lookup)

    def _create_default_user(self) -> None:
        """Create the historical ERP and ADT users through the shared provider."""
        secret_name = "adt-demo-password"  # nosec B105 - secret name, not value
        provider = create_demo_user_provider(
            self,
            function_name="erp-dashboard-create-user",
            user_pool=self.user_pool,
            secret_name=secret_name,
            preserve_indented_blank_lines=True,
        )
        add_demo_user(
            self,
            construct_id="DefaultUser",
            provider=provider,
            user_pool=self.user_pool,
            username="erp",
            secret_name=secret_name,
        )
        add_demo_user(
            self,
            construct_id="ActDemoUser",
            provider=provider,
            user_pool=self.user_pool,
            username="ADT",
            secret_name=secret_name,
        )

    def _create_post_deployment_reset(self) -> None:
        """
        Create a Custom Resource that clears simulation_events table after dashboard deployment.
        
        This runs at the END of the deployment (after Dashboard stack completes) to ensure
        the simulation starts with a clean slate. The Lambda stack's EventBridge rule may
        have already triggered simulation runs during deployment, so we clear any stale
        events here.
        """
        reset_lambda = lambda_.Function(
            self,
            "DashboardPostDeployResetLambda",
            function_name="erp-dashboard-post-deploy-reset",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            code=lambda_.Code.from_inline(self._get_reset_lambda_code()),
        )

        # Grant permission to invoke the cleanup Lambda
        reset_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=["arn:aws:lambda:*:*:function:erp-cleanup-old-data"],
            )
        )

        reset_provider = cr.Provider(
            self,
            "DashboardPostDeployResetProvider",
            on_event_handler=reset_lambda,
        )

        CustomResource(
            self,
            "DashboardPostDeployReset",
            service_token=reset_provider.service_token,
            properties={
                "DeploymentTimestamp": str(int(time.time())),
            },
        )

    def _get_reset_lambda_code(self) -> str:
        return '''
import json
import boto3

def handler(event, context):
    print(f"Event: {json.dumps(event)}")
    
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "dashboard-reset")}
    
    try:
        lambda_client = boto3.client("lambda")
        response = lambda_client.invoke(
            FunctionName="erp-cleanup-old-data",
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": "clear_events_only"})
        )
        result = json.loads(response["Payload"].read().decode())
        print(f"Clear events result: {result}")
    except Exception as e:
        print(f"Error clearing events: {e}")
    
    return {"PhysicalResourceId": "dashboard-reset", "Data": {"Status": "Complete"}}
'''

    def _create_exports(self) -> None:
        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="Dashboard URL (Amazon CloudFront - VPC origin over internal ALB, AWS WAF-protected)",
            export_name=f"{self.stack_name}-DashboardUrl",
        )

        CfnOutput(
            self,
            "ALBDnsName",
            value=self.alb.load_balancer_dns_name,
            description="Application Load Balancer DNS name",
            export_name=f"{self.stack_name}-ALBDnsName",
        )

        CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.user_pool.user_pool_id,
            description="Amazon Cognito User Pool ID",
            export_name=f"{self.stack_name}-CognitoUserPoolId",
        )

        CfnOutput(
            self,
            "CognitoUserPoolDomain",
            value=self.user_pool_domain_value,
            description="Amazon Cognito User Pool Domain",
            export_name=f"{self.stack_name}-CognitoUserPoolDomain",
        )

        CfnOutput(
            self,
            "LoginCredentials",
            value="See Secrets Manager for credentials. Username: ADT",
            description="Login info - password stored in AWS Secrets Manager",
        )

        CfnOutput(
            self,
            "EcrRepositoryUri",
            value=self.ecr_repository.repository_uri,
            description="Amazon ECR repository URI for dashboard container image",
            export_name=f"{self.stack_name}-EcrRepositoryUri",
        )
