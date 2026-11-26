# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda Stack for ERP Beverage Simulation System

This stack creates all Lambda functions for the ERP simulation:
- Simulation orchestrator Lambda (runs every minute via EventBridge)
- Cleanup Lambda (cleans transactional data for dashboard reset)
- Seed Data Lambda (seeds reference data and runs migrations)
- Story History Lambda + custom resource (live workshop story only)

Each Lambda has:
- ECR repository for container image
- Lambda function using container image
- IAM permissions for Secrets Manager, CloudWatch Logs, and VPC

Requirements: 3.4, 3.6, 3.7, 3.9, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7
"""

import hashlib
import os
from pathlib import Path
from aws_cdk import (
    Stack,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecr_assets as ecr_assets,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from stacks import _workshop_story
from stacks._direct_reset import (
    STORY_REGISTRY_PARAMETER,
    custom_resource_step,
    register_reset_steps,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The live-story history writer (ADR-0085). Its content hash is the history
# CR's ``Version``, so a stack update that ships a changed writer re-runs it.
# The set is the writer's import closure (story_replan -> story_live ->
# condition_monitor / mes_simulator).
STORY_HISTORY_SOURCES = (
    "erp_simulation/story_history.py",
    "lambda_/lambda_story_history.py",
    "erp_simulation/story_replan.py",
    "erp_simulation/condition_monitor.py",
    "erp_simulation/story_live.py",
    "erp_simulation/mes_simulator.py",
)

# The history writer's function name. ADTComputeStack names it by this SAME
# constant (invoke grant + STORY_HISTORY_FUNCTION_NAME) rather than a
# cross-stack export, which a live -> frozen deploy could not drop.
STORY_HISTORY_FUNCTION = "erp-story-history"
# What the direct-reset step restores (db-init's log and failure message), and
# the physical id the history handler always reports
# (lambda_/lambda_story_history.py PHYSICAL_ID), so the Update event db-init
# sends it after a direct reset carries the physical id it expects.
STORY_HISTORY_RESET_STEP_NAME = "the live workshop story history"
STORY_HISTORY_PHYSICAL_ID = "adt-story-history"


def story_history_version() -> str:
    """sha256 over (relative path, bytes) of each writer file, hex[:16].

    Read-only (``cdk synth`` stays pure); a missing file raises."""
    digest = hashlib.sha256()
    for rel in STORY_HISTORY_SOURCES:
        digest.update(rel.encode() + b"\0" + (REPO_ROOT / rel).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def story_anchor_property(node) -> str:
    """The history CR's ``Anchor`` (D14): the packaged anchor as whole epoch
    seconds in a decimal string, ``""`` when the package carries none.

    Raises ``ValueError`` for an anchor on a non-live synth
    (:func:`stacks._workshop_story.anchor_from_node`)."""
    anchor = _workshop_story.anchor_from_node(node)
    return "" if anchor is None else str(anchor)


# The history CR property carrying the packaged loop length. ABSENT when
# the package has no loop, so a default template is byte-identical.
STORY_LOOP_PROPERTY = "LoopSeconds"


def story_loop_env(node) -> dict:
    """``{"STORY_LOOP_SECONDS": "<s>"}`` for a live synth with a packaged loop,
    else ``{}``. Raises ``ValueError`` for a loop on a non-live synth."""
    loop = _workshop_story.loop_from_node(node)
    return {} if loop is None else {_workshop_story.LOOP_ENV_NAME: str(loop)}


def story_loop_property(node) -> dict:
    """``{"LoopSeconds": "<s>"}`` for the history CR (a change re-plans), else
    ``{}``. Raises ``ValueError`` for a loop on a non-live synth."""
    loop = _workshop_story.loop_from_node(node)
    return {} if loop is None else {STORY_LOOP_PROPERTY: str(loop)}


class LambdaStack(Stack):
    """
    Lambda Stack creating all Lambda functions for ERP simulation.
    
    This stack depends on ADTNetworkStack for VPC and security groups,
    and DatabaseStack for database secret.
    
    Lambda functions created:
    - erp-simulation-orchestrator: Runs simulation cycles (EventBridge triggered)
    - erp-cleanup-old-data: Cleans transactional data (dashboard reset)
    - erp-seed-data-deployer: Seeds reference data and runs migrations
    
    Requirements: 3.4, 3.9
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        network_stack,
        database_stack,
        seed_data_stack=None,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - ERP seed + simulation Lambda functions", **kwargs)

        # Store references to dependent stacks
        self.network_stack = network_stack
        self.database_stack = database_stack
        # Optional: the ADT seed-data stack (adt_demo RDS). When present, the
        # simulation Lambda gets SEED_DB_SECRET_NAME so the MES-downtime→CMMS
        # work-order bridge can write into cmms_maintenance (which lives in the
        # seed DB, not the ERP DB). None ⇒ the bridge logs one loud warning and
        # skips (mes_simulator._get_seed_db_conn).
        self.seed_data_stack = seed_data_stack

        # Get configuration from context (cdk.json)
        lambda_config = self.node.try_get_context("lambda")
        self.timeout_seconds = lambda_config.get("timeout_seconds", 300)
        self.memory_mb = lambda_config.get("memory_mb", 512)
        self.schedule = lambda_config.get("schedule", "rate(1 minute)")

        # Create ECR repositories for all Lambda images
        self._create_ecr_repositories()

        # Create Lambda functions
        self._create_simulation_lambda()
        self._create_cleanup_lambda()
        self._create_seed_lambda()

        # Grant permissions to all Lambdas
        self._grant_permissions()

        # Live workshop story only: deploy-time history custom resource, the
        # story-anchor mirror the story site's OPC UA server follows, and the
        # story machine's line kept for the story's own runs
        self._create_story_history()
        self._create_story_anchor_mirror()
        self._reserve_story_line()

        # Create EventBridge rule for simulation orchestrator
        self._create_eventbridge_rule()

        # Add stack exports
        self._create_exports()

    def _create_ecr_repositories(self) -> None:
        """
        Create ECR repositories for all Lambda container images.
        
        Requirements: 9.1
        - Configure auto-delete images on stack deletion
        - Set removal policy to DESTROY
        """
        # ECR repository for simulation orchestrator Lambda
        self.ecr_repository = ecr.Repository(
            self,
            "LambdaEcrRepository",
            repository_name="erp-simulation-lambda",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_images=True,
            image_scan_on_push=True,
        )
        
        # ECR repository for cleanup Lambda
        self.cleanup_ecr_repository = ecr.Repository(
            self,
            "CleanupEcrRepository",
            repository_name="erp-cleanup-lambda",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_images=True,
            image_scan_on_push=True,
        )
        
        # ECR repository for seed data Lambda
        self.seed_ecr_repository = ecr.Repository(
            self,
            "SeedEcrRepository",
            repository_name="erp-seed-lambda",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_images=True,
            image_scan_on_push=True,
        )

    def _one_week_log_group(self, construct_id: str) -> logs.LogGroup:
        """A function's own log group, kept one week.

        Without one, Lambda writes to ``/aws/lambda/<function>``, which never
        expires: on a demo account the simulator's group grew to 2.25 TB while
        a stuck cycle logged ~220 GB of tracebacks a day (found 2026-10-22).
        The name is generated, because the old ``/aws/lambda/<function>`` group
        still exists on updated accounts and a fixed name would collide with
        it; ``log_retention`` is not used because it adds a helper function
        whose role can set retention on any log group in the account.
        """
        return logs.LogGroup(
            self,
            construct_id,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_simulation_lambda(self) -> None:
        """
        Create the simulation orchestrator Lambda function.
        
        Requirements: 9.2, 9.3, 9.4, 9.6
        - Use container image from ECR
        - Configure VPC access with Lambda security group
        - Set timeout to 5 minutes, memory to 512 MB
        - Add environment variables: DB_SECRET_NAME, REGION
        """
        project_root = os.path.join(os.path.dirname(__file__), "..")
        dockerfile_path = os.path.join("docker", "Dockerfile.lambda")
        
        self.lambda_function = lambda_.DockerImageFunction(
            self,
            "SimulationLambda",
            function_name="erp-simulation-orchestrator",
            log_group=self._one_week_log_group("SimulationLambdaLogs"),
            description="ERP Beverage Simulation Orchestrator - runs simulation cycles",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=project_root,
                file=dockerfile_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            timeout=Duration.seconds(self.timeout_seconds),
            memory_size=self.memory_mb,
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.database_stack.lambda_sg],
            environment={
                "DB_SECRET_NAME": self.database_stack.db_secret.secret_arn,
                "REGION": self.region,
                "USE_SECRETS_MANAGER": "true",
                # MES-downtime→CMMS bridge: cmms_maintenance lives in the seed
                # DB. Empty string when the seed stack is absent — the
                # simulator logs one loud warning and skips the bridge.
                "SEED_DB_SECRET_NAME": (
                    self.seed_data_stack.db_secret.secret_arn
                    if self.seed_data_stack is not None else ""
                ),
                **story_loop_env(self.node),  # live + packaged loop only
            },
            architecture=lambda_.Architecture.ARM_64,
        )

    def _create_cleanup_lambda(self) -> None:
        """
        Create the cleanup Lambda function for dashboard reset.
        
        This Lambda cleans transactional data and resets inventory balances.
        Called by dashboard "Reset All Data" button.
        """
        project_root = os.path.join(os.path.dirname(__file__), "..")
        dockerfile_path = os.path.join("docker", "Dockerfile.cleanup")
        
        self.cleanup_lambda = lambda_.DockerImageFunction(
            self,
            "CleanupLambda",
            function_name="erp-cleanup-old-data",
            log_group=self._one_week_log_group("CleanupLambdaLogs"),
            description="ERP Cleanup Lambda - cleans transactional data for dashboard reset",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=project_root,
                file=dockerfile_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            timeout=Duration.seconds(self.timeout_seconds),
            memory_size=self.memory_mb,
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.database_stack.lambda_sg],
            environment={
                "DB_SECRET_NAME": self.database_stack.db_secret.secret_arn,
                "REGION": self.region,
            },
            architecture=lambda_.Architecture.ARM_64,
        )

    def _create_seed_lambda(self) -> None:
        """
        Create the seed data Lambda function for reference data and migrations.
        
        This Lambda seeds reference data (customers, products, BOMs) and runs
        database migrations. Called by dashboard "Reset All Data" button.
        """
        project_root = os.path.join(os.path.dirname(__file__), "..")
        dockerfile_path = os.path.join("docker", "Dockerfile.seed")
        
        self.seed_lambda = lambda_.DockerImageFunction(
            self,
            "SeedLambda",
            function_name="erp-seed-data-deployer",
            log_group=self._one_week_log_group("SeedLambdaLogs"),
            description="ERP Seed Data Lambda - seeds reference data and runs migrations",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=project_root,
                file=dockerfile_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            timeout=Duration.seconds(self.timeout_seconds),
            memory_size=self.memory_mb,
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.database_stack.lambda_sg],
            environment={
                "DB_SECRET_NAME": self.database_stack.db_secret.secret_arn,
                "REGION": self.region,
                "USE_SECRETS_MANAGER": "true",
            },
            architecture=lambda_.Architecture.ARM_64,
        )

    def _grant_permissions(self) -> None:
        """
        Grant permissions to all Lambda functions.
        
        Requirements: 9.5
        - Grant read access to Secrets Manager secret
        - Grant CloudWatch Logs write permissions (automatic with Lambda)
        - Grant VPC network interface management (automatic with VPC Lambda)
        - Grant CloudWatch PutMetricData for simulation metrics
        """
        # Grant Secrets Manager read access to all Lambdas
        self.database_stack.db_secret.grant_read(self.lambda_function)
        self.database_stack.db_secret.grant_read(self.cleanup_lambda)
        self.database_stack.db_secret.grant_read(self.seed_lambda)
        # MES-downtime→CMMS bridge: the simulation Lambda reads the seed-DB
        # secret to write work orders into cmms_maintenance (seed database).
        if self.seed_data_stack is not None:
            self.seed_data_stack.db_secret.grant_read(self.lambda_function)

        # Grant CloudWatch PutMetricData for simulation metrics
        self.lambda_function.add_to_role_policy(
            iam.PolicyStatement(
                # cloudwatch:PutMetricData does not support resource-level permissions
                # (access is constrained via the cloudwatch:namespace condition key, not
                # by ARN), so it must be granted on "*".
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # Allow the cleanup Lambda to pause/resume the simulation schedule during
        # "Reset All Data" so the TRUNCATE isn't blocked by the simulator's locks.
        # Deliberately scoped to the ONE named rule this stack creates — no wildcards.
        self.cleanup_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "events:DisableRule",
                    "events:EnableRule",
                    "events:DescribeRule",
                ],
                resources=[
                    f"arn:{self.partition}:events:{self.region}:{self.account}:rule/erp-simulation-schedule"
                ],
            )
        )

    def _create_story_history(self) -> None:
        """Live workshop story only (ADR-0085): ``Custom::ADTStoryHistory``
        writes the deploy-time history (erp_simulation/story_history.py) into
        the ERP and seed databases. Frozen / non-workshop synths add nothing
        (and fail on a packaged anchor, which only the live story can use).
        """
        anchor = story_anchor_property(self.node)
        if _workshop_story.from_node(self.node) != "live":
            return
        if self.seed_data_stack is None:
            raise ValueError(
                "workshop_story=live needs the seed-data stack: the story history "
                "writes the site CMMS rows into the seed database"
            )
        self.story_history_lambda = self._create_story_history_lambda()
        self.database_stack.db_secret.grant_read(self.story_history_lambda)
        self.seed_data_stack.db_secret.grant_read(self.story_history_lambda)
        provider = cr.Provider(
            self, "StoryHistoryProvider", on_event_handler=self.story_history_lambda,
        )
        # ErpDbInit / ErpDbSecretArn mirror the db-init CR's re-run triggers:
        # when db-init drops and reseeds the ERP tables, this CR gets an Update
        # and (no stored anchor left) rewrites the history. The cluster
        # endpoint is deliberately NOT mirrored: it would add a cross-stack
        # export only live imports, which a live -> frozen deploy could not drop.
        # Version is the writer's content hash: shipping a changed writer is an
        # Update (idempotent: it re-derives the plan from the stored anchor).
        # Anchor is the packaged anchor ("" = none); the writer re-plans ONLY
        # when it differs between Old/New properties (D14).
        history_properties = {
            "Days": "3", "Version": story_history_version(), "Anchor": anchor,
            "ErpDbInit": self.database_stack.init_trigger,
            "ErpDbSecretArn": self.database_stack.db_secret.secret_arn,
            **story_loop_property(self.node),
        }
        self.story_history = CustomResource(
            self,
            "StoryHistory",
            service_token=provider.service_token,
            resource_type="Custom::ADTStoryHistory",
            properties=history_properties,
        )
        self.story_history.node.add_dependency(self.database_stack.init_resource)
        self._register_story_history_reset(provider, history_properties)

    def _register_story_history_reset(self, provider, history_properties) -> None:
        """A full ERP reset outside CloudFormation (the ERP dashboard's Reset
        All, db-init invoked by hand) drops and reseeds every ERP table, the
        story_* anchor keys with them, so the live story clock is gone. A
        CloudFormation db-init re-run sends this CR the same Update (it carries
        the db-init trigger); a direct reset has no stack run after it, so the
        ERP db-init function re-runs this CR itself afterwards with the Update
        event CloudFormation sends a custom resource whose properties did not
        change. With no stored anchor the writer takes a fresh anchor and
        rewrites the whole history, exactly as a CloudFormation re-run would.

        This stack depends on the ERP database stack, so it cannot reach
        db-init's environment or role; it registers the step in its own SSM
        parameter (another dependent registrant owns the other) that db-init
        reads, and the ERP stack lets db-init invoke ``erp-story-history`` by
        its fixed name (stacks/_direct_reset.py). The step runs after the
        plant re-seed and the reader logins, both ordered before it by the ERP
        stack's own steps; the writer reads the reseeded ERP tables and writes
        the ERP and
        seed databases with the master secret."""
        properties = {"ServiceToken": provider.service_token, **history_properties}
        register_reset_steps(
            self, "StoryHistoryResetSteps",
            [custom_resource_step(STORY_HISTORY_RESET_STEP_NAME, self.story_history_lambda,
                                  STORY_HISTORY_PHYSICAL_ID, properties)],
            parameter=STORY_REGISTRY_PARAMETER)

    def _create_story_history_lambda(self) -> lambda_.DockerImageFunction:
        """The history writer runs from the SAME image asset as the simulation
        Lambda (same directory / Dockerfile / platform, so the same asset hash);
        only the CMD differs, so no extra image is built or replicated."""
        return lambda_.DockerImageFunction(
            self,
            "StoryHistoryLambda",
            function_name=STORY_HISTORY_FUNCTION,
            log_group=self._one_week_log_group("StoryHistoryLambdaLogs"),
            description="ERP live workshop story - writes the deploy-time story history",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=os.path.join(os.path.dirname(__file__), ".."),
                file=os.path.join("docker", "Dockerfile.lambda"),
                platform=ecr_assets.Platform.LINUX_ARM64,
                cmd=["lambda_story_history.lambda_handler"],
            ),
            timeout=Duration.minutes(10),
            memory_size=self.memory_mb,
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.database_stack.lambda_sg],
            environment={
                "DB_SECRET_NAME": self.database_stack.db_secret.secret_arn,
                "SEED_DB_SECRET_NAME": self.seed_data_stack.db_secret.secret_arn,
                "REGION": self.region,
                "USE_SECRETS_MANAGER": "true",
                **story_loop_env(self.node),
            },
            architecture=lambda_.Architecture.ARM_64,
        )

    def _create_story_anchor_mirror(self) -> None:
        """Live workshop story only (ADR-0092): the SSM parameter the
        simulation Lambda mirrors ``simulation_config.story_anchor_epoch``
        into every cycle, and the story site's OPC UA server polls. The
        Lambda may read and overwrite exactly this parameter; it publishes
        nothing to SiteWise (the OPC UA server -> SFC path does)."""
        if _workshop_story.from_node(self.node) != "live":
            return
        self.story_anchor_parameter = ssm.StringParameter(
            self,
            "StoryAnchorParameter",
            parameter_name=_workshop_story.ANCHOR_PARAMETER_NAME,
            string_value=_workshop_story.ANCHOR_PARAMETER_PENDING,
            description="Copy of simulation_config.story_anchor_epoch for the story OPC UA server "
                        "(written by the simulation Lambda; never by hand).",
        )
        self.story_anchor_parameter.grant_read(self.lambda_function)
        self.story_anchor_parameter.grant_write(self.lambda_function)
        self.lambda_function.add_environment(
            _workshop_story.ANCHOR_PARAMETER_ENV_NAME, self.story_anchor_parameter.parameter_name
        )
        # ADR-0092 decision 8: the first sign-in will place the clock, so the
        # story site publishes nothing until then (yes/no; no minutes here).
        if _workshop_story.auto_place_from_node(self.node) is not None:
            self.lambda_function.add_environment(_workshop_story.FEED_HOLD_ENV_NAME, "1")
        self._create_story_backfill_mirror()

    def _reserve_story_line(self) -> None:
        """Live workshop story only: tell the simulation Lambda that the story
        machine's line runs only story work (``STORY_LINE_RESERVED=1``; read by
        ``MESSimulator.story_line_reserved``). It holds from the first cycle,
        so no simulator run made during the deploy can sit inside a history
        run the first placement re-times. Frozen and non-workshop synths add
        nothing (their templates are unchanged)."""
        if _workshop_story.from_node(self.node) != "live":
            return
        self.lambda_function.add_environment(_workshop_story.STORY_LINE_ENV_NAME, "1")

    def _create_story_backfill_mirror(self) -> None:
        """The one-time history replay's SECOND SSM parameter (the replay ADR):
        the simulation Lambda writes the replay window into it and the story
        site's OPC UA server polls it. Live story with the switch on only; the
        Lambda may read and overwrite exactly this parameter (SFC does the
        SiteWise write, never the Lambda)."""
        if not _workshop_story.backfill_from_node(self.node):
            return
        self.story_backfill_parameter = ssm.StringParameter(
            self,
            "StoryFeedBackfillParameter",
            parameter_name=_workshop_story.BACKFILL_PARAMETER_NAME,
            string_value=_workshop_story.BACKFILL_PARAMETER_PENDING,
            description="One-time history replay window for the story OPC UA server "
                        "(written by the simulation Lambda; never by hand).",
        )
        self.story_backfill_parameter.grant_read(self.lambda_function)
        self.story_backfill_parameter.grant_write(self.lambda_function)
        self.lambda_function.add_environment(
            _workshop_story.BACKFILL_PARAMETER_ENV_NAME,
            self.story_backfill_parameter.parameter_name,
        )

    def _create_eventbridge_rule(self) -> None:
        """
        Create EventBridge rule to trigger simulation Lambda every minute.
        
        Requirements: 3.6, 9.7
        - Configure rate(1 minute) schedule
        - Set Lambda function as target
        - Add payload: {"action": "single_cycle"}
        """
        self.schedule_rule = events.Rule(
            self,
            "SimulationScheduleRule",
            rule_name="erp-simulation-schedule",
            description="Triggers ERP simulation Lambda every minute",
            schedule=events.Schedule.expression(self.schedule),
            enabled=True,
        )

        self.schedule_rule.add_target(
            targets.LambdaFunction(
                self.lambda_function,
                event=events.RuleTargetInput.from_object({
                    "action": "single_cycle"
                }),
            )
        )

    def _create_exports(self) -> None:
        """
        Export Lambda function ARNs and other useful outputs.
        """
        # Simulation orchestrator Lambda exports
        CfnOutput(
            self,
            "LambdaFunctionArn",
            value=self.lambda_function.function_arn,
            description="Simulation orchestrator AWS Lambda function ARN",
            export_name=f"{self.stack_name}-LambdaFunctionArn",
        )

        CfnOutput(
            self,
            "LambdaFunctionName",
            value=self.lambda_function.function_name,
            description="Simulation orchestrator AWS Lambda function name",
            export_name=f"{self.stack_name}-LambdaFunctionName",
        )

        CfnOutput(
            self,
            "EcrRepositoryUri",
            value=self.ecr_repository.repository_uri,
            description="Amazon ECR repository URI for the simulation AWS Lambda container image",
            export_name=f"{self.stack_name}-EcrRepositoryUri",
        )

        CfnOutput(
            self,
            "ScheduleRuleArn",
            value=self.schedule_rule.rule_arn,
            description="Amazon EventBridge schedule rule ARN",
            export_name=f"{self.stack_name}-ScheduleRuleArn",
        )

        CfnOutput(
            self,
            "ScheduleExpression",
            value=self.schedule,
            description="AWS Lambda trigger schedule expression",
        )
        
        # Cleanup Lambda exports
        CfnOutput(
            self,
            "CleanupLambdaArn",
            value=self.cleanup_lambda.function_arn,
            description="Cleanup AWS Lambda function ARN",
            export_name=f"{self.stack_name}-CleanupLambdaArn",
        )

        CfnOutput(
            self,
            "CleanupLambdaName",
            value=self.cleanup_lambda.function_name,
            description="Cleanup AWS Lambda function name",
            export_name=f"{self.stack_name}-CleanupLambdaName",
        )
        
        # Seed Lambda exports
        CfnOutput(
            self,
            "SeedLambdaArn",
            value=self.seed_lambda.function_arn,
            description="Seed data AWS Lambda function ARN",
            export_name=f"{self.stack_name}-SeedLambdaArn",
        )

        CfnOutput(
            self,
            "SeedLambdaName",
            value=self.seed_lambda.function_name,
            description="Seed data AWS Lambda function name",
            export_name=f"{self.stack_name}-SeedLambdaName",
        )
