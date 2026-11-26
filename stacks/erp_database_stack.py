# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Database Stack for ERP Beverage Simulation System

This stack creates the database infrastructure:
- Aurora Serverless v2 PostgreSQL cluster
- Secrets Manager secret for database credentials
- Database initialization Lambda function
- Custom resource to trigger database initialization
- EC2 bastion host for database access via SSM Session Manager

Requirements: 3.3, 3.8, 3.9, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10, 8.11
"""

import hashlib
from pathlib import Path

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    CustomResource,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_iam as iam,
    custom_resources as cr,
)
from constructs import Construct

from stacks import _workshop_story
from stacks._db_access import add_ingress_rule, wait_for
from stacks._db_cidr_ingress import add_legacy_cidr_ingress
from stacks._db_readers import SystemDbReaders
from stacks._direct_reset import (
    STORY_REGISTRY_PARAMETER,
    wire_reset_registry,
    wire_reset_steps,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The ERP database's view schemas (lambda-db-init: get_views_sql and
#: get_machining_views_sql), one per demo system. Each gets a read-only login
#: for the system agents (stacks/_db_readers.py); pinned equal to the views
#: db-init creates by tests/test_system_db_readers_stack.py.
ERP_READER_SCHEMAS = (
    "erp_data", "mes_milwaukee", "mes_detroit", "mes_cedarrapids", "mes_indianapolis",
)

#: The live-story history writer function (stacks/erp_lambda_stack.py
#: STORY_HISTORY_FUNCTION), which that dependent stack registers for a direct
#: reset (stacks/_direct_reset.py): a fixed name, so db-init may invoke it
#: without a reference to that stack. Pinned equal to the Lambda stack's own
#: constant by test_erp_lambda_story_history.
STORY_HISTORY_SETUP_FUNCTION = "erp-story-history"

# Bump to force the db-init custom resource to re-run. db-init DROPS and
# reseeds every ERP table, so the live-story history CR (erp_lambda_stack.py)
# carries db_init_trigger() and is rewritten on the same deploy. The init
# function gets the same value as DB_INIT_TIMESTAMP and runs a full-reset
# Update only for its own Timestamp, DatabaseName, SeedVersion,
# ClusterEndpoint and SecretArn, so the rollback of a failed one that reaches
# the new code is a no-op.
DB_INIT_TIMESTAMP = "2026-05-09-supplier-qualification"

# The db-init CR's ``SeedVersion``: the catalogue it seeds lives inline in the
# handler, so the whole handler is hashed. A SeedVersion-only Update runs the
# handler's ADDITIVE catalogue upsert (never the drop-and-reseed), so an
# existing account gets new seed rows on its next in-place update and keeps
# its data; it is deliberately NOT in db_init_trigger(). The init function
# gets the same value as DB_INIT_SEED_VERSION and runs a seed-only Update
# only for its own version, so the rollback of a failed one is a no-op.
DB_INIT_SEED_SOURCES = ("lambda-db-init/handler.py",)


def db_init_seed_version() -> str:
    """sha256 over (relative path, bytes) of DB_INIT_SEED_SOURCES, hex[:16].

    Read-only (``cdk synth`` stays pure); a missing file raises."""
    digest = hashlib.sha256()
    for rel in DB_INIT_SEED_SOURCES:
        digest.update(rel.encode() + b"\0" + (REPO_ROOT / rel).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def db_init_trigger(database_name: str) -> str:
    """The literal part of the db-init CR's properties (changes whenever a
    Timestamp bump or a database rename re-runs db-init)."""
    return f"{DB_INIT_TIMESTAMP}|{database_name}"


class DatabaseStack(Stack):
    """
    Database Stack creating Aurora Serverless PostgreSQL cluster with initialization.
    
    This stack depends on ADTNetworkStack for VPC and security groups.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        network_stack,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - Aurora Serverless v2 (ERP orders, inventory, suppliers)", **kwargs)

        # Store reference to network stack
        self.network_stack = network_stack

        # Get configuration from context (cdk.json)
        db_config = self.node.try_get_context("database")
        self.database_name = db_config.get("database_name", "erp_simulation")
        engine_version = db_config.get("engine_version", "15.15")
        min_capacity = db_config.get("min_capacity", 0.5)
        max_capacity = db_config.get("max_capacity", 2)
        backup_retention_days = db_config.get("backup_retention_days", 7)
        deletion_protection = db_config.get("deletion_protection", False)

        # Create the security groups this stack owns (moved out of ADTNetworkStack
        # so a base/engine-only deploy carries no demo SGs). Must run BEFORE the
        # Aurora cluster / init Lambda / bastion, which consume them.
        self._create_security_groups()

        # Create the Aurora Serverless v2 cluster
        self._create_aurora_cluster(
            engine_version=engine_version,
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            backup_retention_days=backup_retention_days,
            deletion_protection=deletion_protection,
        )

        # Database credentials are managed in Secrets Manager by Aurora
        # The secret is automatically created by Aurora with credentials

        # Create the database initialization Lambda
        self._create_init_lambda()

        # Add the database initialization custom resource
        self._create_init_custom_resource()

        # Create the EC2 bastion host for database access
        self._create_bastion_host()

        # Add stack exports
        self._create_exports()

    def _create_security_groups(self) -> None:
        """
        Create the security groups owned by this stack.

        - aurora_sg: Aurora PostgreSQL cluster SG (no all-outbound). It admits
          5432 from client groups only: this stack's own clients here, every
          other client in its own stack (stacks/_db_access.py; the ERP
          dashboard's ECS group in ADTERPDashboardStack), so no cycle forms. A
          VPC-wide rule exists only during phase 1 of an in-place update
          (stacks/_db_cidr_ingress.py).
        - lambda_sg: ERP init/simulation/seed/cleanup Lambda SG (all-outbound for
          Secrets Manager access). Also referenced by ADTERPLambdaStack via
          self.database_stack.lambda_sg.
        - bastion_sg: bastion host SG (restricted outbound).

        Requirements: 7.3, 7.4, 8.8, 8.11
        """
        vpc = self.network_stack.vpc

        # Aurora Security Group
        self.aurora_sg = ec2.SecurityGroup(
            self,
            "AuroraSG",
            vpc=vpc,
            description="Security group for Aurora PostgreSQL cluster",
            allow_all_outbound=False,
        )

        # Lambda Security Group
        self.lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSG",
            vpc=vpc,
            description="Security group for Lambda simulation function",
            allow_all_outbound=True,  # Needs internet for Secrets Manager
        )

        # Bastion Security Group
        # Requirements: 8.8, 8.11
        self.bastion_sg = ec2.SecurityGroup(
            self,
            "BastionSG",
            vpc=vpc,
            description="Security group for bastion host with SSM Session Manager access",
            allow_all_outbound=False,  # Restrict outbound to only necessary traffic
        )
        self._add_security_group_rules()

    def _add_security_group_rules(self) -> None:
        """The bastion's egress and Aurora's ingress from this stack's clients.

        ``lambda_rule`` is Aurora's rule for the Lambda group: the db-init and
        db-init functions wait for it (requirements 8.8, 8.11)."""
        # Allow bastion outbound to Aurora on port 5432
        self.bastion_sg.add_egress_rule(
            peer=self.aurora_sg,
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL to Aurora",
        )
        # Allow bastion outbound HTTPS for SSM Session Manager
        self.bastion_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="Allow HTTPS for SSM Session Manager",
        )
        self.lambda_rule = add_ingress_rule(
            self.aurora_sg, self.lambda_sg, 5432, "Allow PostgreSQL from Lambda")
        add_ingress_rule(self.aurora_sg, self.bastion_sg, 5432, "Allow PostgreSQL from Bastion")
        # The VPC-wide rule only while DatabaseCidrIngress is keep (phase 1 of
        # an in-place update; stacks/_db_cidr_ingress.py).
        add_legacy_cidr_ingress(self, self.aurora_sg, self.network_stack.vpc.vpc_cidr_block,
                                "Allow PostgreSQL from VPC (MES HighByte, etc.)")

    def _create_aurora_cluster(
        self,
        engine_version: str,
        min_capacity: float,
        max_capacity: float,
        backup_retention_days: int,
        deletion_protection: bool,
    ) -> None:
        """
        Create Aurora Serverless v2 PostgreSQL cluster.
        
        Requirements: 8.1, 8.2, 8.3, 8.4
        """
        # Create database credentials secret
        # Generate database credentials and store them in Secrets Manager
        self.db_secret = secretsmanager.Secret(
            self,
            "DatabaseSecret",
            description="Database credentials for ERP Beverage Simulation",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "erp_admin"}',  # nosec B106 - Secrets Manager construct name or JSON template, not credential value
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create subnet group for Aurora cluster
        subnet_group = rds.SubnetGroup(
            self,
            "AuroraSubnetGroup",
            description="Subnet group for Aurora cluster",
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create Aurora Serverless v2 cluster
        # Requirements: 8.1 (Aurora Serverless v2), 8.2 (private subnets), 8.4 (encryption, backups)
        self.cluster = rds.DatabaseCluster(
            self,
            "AuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of(
                    aurora_postgres_major_version=engine_version.split(".")[0],
                    aurora_postgres_full_version=engine_version,
                )
            ),
            credentials=rds.Credentials.from_secret(self.db_secret),
            default_database_name=self.database_name,
            vpc=self.network_stack.vpc,
            subnet_group=subnet_group,
            security_groups=[self.aurora_sg],
            serverless_v2_min_capacity=min_capacity,
            serverless_v2_max_capacity=max_capacity,
            writer=rds.ClusterInstance.serverless_v2(
                "Writer",
                auto_minor_version_upgrade=True,
            ),
            backup=rds.BackupProps(
                retention=Duration.days(backup_retention_days),
            ),
            storage_encrypted=True,
            # IAM database authentication on as well as password logins; the
            # sample's own logins keep using their secrets.
            iam_authentication=True,
            deletion_protection=deletion_protection,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Update secret with host and database information
        # Note: The secret is already attached when using Credentials.from_secret()
        # so we don't need to call attach() again

    def _create_init_lambda(self) -> None:
        """
        Create Lambda function for database initialization using Docker container.
        
        Requirements: 8.5
        
        Uses a Docker container image that installs the hash-locked pg8000
        wheels and carries pgdriver.py for PostgreSQL connectivity.
        """
        import os
        from aws_cdk import aws_ecr_assets as ecr_assets

        init_lambda_role = self._create_init_lambda_role()

        # Path to the db-init Lambda directory (contains Dockerfile and handler.py)
        db_init_path = os.path.join(os.path.dirname(__file__), "..", "lambda-db-init")

        # Create Lambda function using Docker container image
        # The image installs its locked wheels inside the Linux container
        self.init_lambda = lambda_.DockerImageFunction(
            self,
            "DatabaseInitLambda",
            function_name="erp-database-init",
            description="Database initialization Lambda for ERP Beverage Simulation",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=db_init_path,
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            timeout=Duration.minutes(10),
            memory_size=512,
            role=init_lambda_role,
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.lambda_sg],
            architecture=lambda_.Architecture.ARM_64,
            environment=self._init_lambda_environment(),
        )

    def _create_init_lambda_role(self) -> iam.Role:
        """The db-init function's role: VPC execution plus read on the database secret."""
        init_lambda_role = iam.Role(
            self,
            "InitLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Role for database initialization Lambda",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
            ],
        )
        self.db_secret.grant_read(init_lambda_role)
        return init_lambda_role

    def _init_lambda_environment(self) -> dict:
        """The db-init function's environment."""
        return {
            "DB_SECRET_ARN": self.db_secret.secret_arn,
            "DB_NAME": self.database_name,
            # The SeedVersion of the code this function runs, the same value
            # the custom resource carries: a seed-only Update asking for
            # another version (an update rollback reaching the new code) is
            # skipped (lambda-db-init/handler.py: SEED_VERSION_ENV).
            "DB_INIT_SEED_VERSION": db_init_seed_version(),
            # The Timestamp of the code this function runs, the same value
            # the custom resource carries: with DB_NAME, DB_INIT_SEED_VERSION,
            # DB_CLUSTER_ENDPOINT and DB_SECRET_ARN it pins every property
            # a full reset of this code deploys, so a full-reset Update
            # asking for anything else (an update rollback reaching the new
            # code) is skipped (lambda-db-init/handler.py: FULL_RESET_PINS).
            "DB_INIT_TIMESTAMP": DB_INIT_TIMESTAMP,
            # The custom resource's ClusterEndpoint, the same expression
            # (read only for that pin; the connection uses the secret's host).
            "DB_CLUSTER_ENDPOINT": self.cluster.cluster_endpoint.hostname,
        }

    def _create_init_custom_resource(self) -> None:
        """
        Create custom resource to trigger database initialization.
        
        Requirements: 8.6
        """
        # Create a provider for the custom resource
        provider = cr.Provider(
            self,
            "DatabaseInitProvider",
            on_event_handler=self.init_lambda,
        )

        # Create the custom resource that triggers initialization
        # Pass the database endpoint and secret ARN as parameters
        self.init_resource = CustomResource(
            self,
            "DatabaseInitResource",
            service_token=provider.service_token,
            properties={
                "ClusterEndpoint": self.cluster.cluster_endpoint.hostname,
                "SecretArn": self.db_secret.secret_arn,
                "DatabaseName": self.database_name,
                # Add a timestamp to force re-initialization on updates
                "Timestamp": DB_INIT_TIMESTAMP,
                # Catalogue hash: a change ALONE is an additive upsert.
                "SeedVersion": db_init_seed_version(),
            },
        )
        # Re-run token for dependants (the live-story history CR).
        self.init_trigger = db_init_trigger(self.database_name)

        # Ensure initialization happens after cluster is ready
        self.init_resource.node.add_dependency(self.cluster)
        self._wait_for_lambda_rule()
        # The system agents' read-only logins follow every db-init run.
        self._create_system_readers()
        # A direct db-init reset runs what these resources run after its own.
        self._wire_direct_reset()

    def _wait_for_lambda_rule(self) -> None:
        """db-init reaches Aurora through the Lambda group's rule only: each
        function and resource waits for it on create and is removed before it
        on delete."""
        waiters = [self.init_lambda, self.init_resource]
        wait_for(self.lambda_rule, waiters)

    def _create_system_readers(self) -> None:
        """The system agents' read-only logins, re-synced after every db-init
        run: a full re-initialisation drops and recreates the view schemas,
        which drops their grants, so DataVersion follows both the reset
        trigger and the catalogue hash. A direct reset re-runs the setup from
        db-init itself (_wire_direct_reset)."""
        from stacks.adt_seed_data_stack import _create_pg8000_layer

        self.pg8000_layer = _create_pg8000_layer(self)
        self.system_readers = SystemDbReaders(
            self, "ErpDbReaders",
            schemas=ERP_READER_SCHEMAS,
            owner_secret=self.db_secret,
            vpc=self.network_stack.vpc,
            layer=self.pg8000_layer,
            data_version=f"{self.init_trigger}|{db_init_seed_version()}",
            database_label="ERP database",
            after=self.init_resource,
            database_sg=self.aurora_sg,
        )

    def _wire_direct_reset(self) -> None:
        """A full reset outside CloudFormation (the ERP dashboard's Reset All,
        db-init invoked by hand) has no stack run after it, so db-init runs
        what CloudFormation runs after its own reset, in the same order
        (stacks/_direct_reset.py): the optional plant's re-apply (a full
        reset drops and reseeds the ERP tables), then the read-only logins'
        setup on the schemas that are back, then whatever dependent stacks
        registered (the live-story history, deployed with the live story). db-init may invoke those functions and read the
        registry parameters those stacks own, nothing else. Each step carries
        its function's timeout, and db-init waits on it only within what is
        left of its own (10 minutes, under the 15-minute provider framework
        function that runs it for CloudFormation)."""
        steps = []
        steps.append(self.system_readers.direct_reset_step())
        wire_reset_steps(self.init_lambda, steps)
        # Each dependent registrant owns its own registry parameter: the
        # live-story history (with the live story; erp_lambda_stack.py). The
        # story step runs after the readers, so the writer reads the reseeded
        # ERP tables.
        registries = []
        if _workshop_story.from_node(self.node) == "live":
            registries.append((STORY_REGISTRY_PARAMETER, [STORY_HISTORY_SETUP_FUNCTION]))
        wire_reset_registry(self.init_lambda, registries)

    def _create_bastion_host(self) -> None:
        """
        Create EC2 bastion host for database access via SSM Session Manager.
        
        Requirements: 8.8, 8.9, 8.10, 8.11
        
        Configuration:
        - Instance type: t3.micro
        - AMI: Amazon Linux 2023
        - Placed in private subnet
        - SSM Session Manager access (no SSH keys required)
        - PostgreSQL client tools installed
        - Secrets Manager read access for database credentials
        """
        # Create IAM role for bastion host with SSM Session Manager permissions
        bastion_role = iam.Role(
            self,
            "BastionRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Role for bastion host with SSM Session Manager access",
            managed_policies=[
                # SSM Session Manager permissions
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )

        # Grant Secrets Manager read access for database credentials
        self.db_secret.grant_read(bastion_role)

        self.bastion_host = self._bastion_instance(bastion_role)

        # Add Name tag for easy identification
        from aws_cdk import Tags
        Tags.of(self.bastion_host).add("Name", "ERP-Bastion-Host")

    def _bastion_instance(self, bastion_role: iam.Role) -> ec2.Instance:
        """The bastion EC2 instance in a private subnet (SSM access only)."""
        return ec2.Instance(
            self,
            "BastionHost",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3,
                ec2.InstanceSize.MICRO,
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_group=self.bastion_sg,
            role=bastion_role,
            user_data=self._bastion_user_data(),
            user_data_causes_replacement=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=8,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                ),
            ],
        )

    def _bastion_user_data(self) -> ec2.UserData:
        """User data that installs the PostgreSQL client and connect-db."""
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -e",
            "",
            "# Update system packages",
            "dnf update -y",
            "",
            "# Install PostgreSQL 15 client tools",
            "dnf install -y postgresql15",
            "",
            "# Install jq for JSON parsing (useful for Secrets Manager)",
            "dnf install -y jq",
            "",
            "# Install AWS CLI v2 (should be pre-installed on AL2023)",
            "# Verify AWS CLI is available",
            "aws --version",
            "",
            *self._connect_db_commands(),
            "",
            "# Create environment file with database connection info",
            "cat > /etc/profile.d/db-env.sh << 'EOF'",
            f'export DB_SECRET_ARN="{self.db_secret.secret_arn}"',
            f'export AWS_REGION="{self.region}"',
            "EOF",
            "",
            "echo 'Bastion host setup complete. Use connect-db to connect to the database.'",
        )
        return user_data

    def _connect_db_commands(self) -> list[str]:
        """Commands that write the connect-db helper script."""
        return [
            "# Create helper script for database connection",
            "cat > /usr/local/bin/connect-db << 'EOF'",
            "#!/bin/bash",
            "# Helper script to connect to the Aurora database",
            "# Usage: connect-db",
            "",
            "# Get the secret ARN from instance metadata or environment",
            f'SECRET_ARN="{self.db_secret.secret_arn}"',
            f'REGION="{self.region}"',
            "",
            "# Retrieve database credentials from Secrets Manager",
            'SECRET=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --region "$REGION" --query SecretString --output text)',
            "",
            "# Parse credentials",
            'DB_HOST=$(echo "$SECRET" | jq -r .host)',
            'DB_PORT=$(echo "$SECRET" | jq -r \'.port // 5432\')',
            'DB_NAME=$(echo "$SECRET" | jq -r \'.dbname // "erp_simulation"\')',
            'DB_USER=$(echo "$SECRET" | jq -r .username)',
            'DB_PASS=$(echo "$SECRET" | jq -r .password)',
            "",
            "# Connect to database",
            'PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"',
            "EOF",
            "",
            "chmod +x /usr/local/bin/connect-db",
        ]

    def _create_exports(self) -> None:
        """
        Export cluster endpoint, secret ARN, and database name.
        
        Requirements: 8.7
        """
        # Export the cluster endpoint
        CfnOutput(
            self,
            "ClusterEndpoint",
            value=self.cluster.cluster_endpoint.hostname,
            description="Amazon Aurora cluster endpoint",
            export_name=f"{self.stack_name}-ClusterEndpoint",
        )

        CfnOutput(
            self,
            "ClusterPort",
            value=str(self.cluster.cluster_endpoint.port),
            description="Amazon Aurora cluster port",
            export_name=f"{self.stack_name}-ClusterPort",
        )

        # Export secret ARN
        CfnOutput(
            self,
            "SecretArn",
            value=self.db_secret.secret_arn,
            description="Database credentials secret ARN",
            export_name=f"{self.stack_name}-SecretArn",
        )

        # Export database name
        CfnOutput(
            self,
            "DatabaseName",
            value=self.database_name,
            description="Database name",
            export_name=f"{self.stack_name}-DatabaseName",
        )

        # Add bastion host outputs
        # Output bastion instance ID for SSM Session Manager connection
        CfnOutput(
            self,
            "BastionInstanceId",
            value=self.bastion_host.instance_id,
            description="Bastion host instance ID for AWS Systems Manager Session Manager connection",
            export_name=f"{self.stack_name}-BastionInstanceId",
        )

        # Output connection instructions
        CfnOutput(
            self,
            "BastionConnectionCommand",
            value=f"aws ssm start-session --target {self.bastion_host.instance_id} --region {self.region}",
            description="Command to connect to bastion host via AWS Systems Manager Session Manager",
        )

        CfnOutput(
            self,
            "BastionDatabaseConnectionInstructions",
            value="After connecting via SSM, run 'connect-db' to connect to the Aurora database",
            description="Instructions for connecting to the database from the bastion host",
        )
