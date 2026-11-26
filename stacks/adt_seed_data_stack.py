# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ADT Seed Data Stack — bundled sample source-system data (DEMO ONLY).

This stack is **opt-in** (deployed only in `--seed` / `--simulator` modes) and is
NOT part of the base engine. It hosts the `adt_demo` Amazon RDS Postgres
instance and loads the bundled sample source-system schemas (`erp_*`, `mes_*`,
`cmms_maintenance`, `plm_engineering`, `iot_historian`) into it via the
`lambda-adt-seed` SQL files.

It exists purely to *represent external source systems* for the demo — it is not
the application's datastore (the app's own state lives in Neptune + DynamoDB + SSM
+ AgentCore Memory). The base deployment never instantiates this stack and has no
compile-time or runtime dependency on it. See ADR-0030.
"""

import hashlib
import os
from pathlib import Path

from aws_cdk import (
    Stack,
    BundlingOptions,
    CfnOutput,
    CustomResource,
    DockerImage,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_iam as iam,
    custom_resources as cr,
)
from constructs import Construct

from stacks._db_access import admit_postgres
from stacks._db_cidr_ingress import add_legacy_cidr_ingress
from stacks._db_readers import SystemDbReaders

REPO_ROOT = Path(__file__).resolve().parents[1]
ADT_SEED_DIR = "lambda-adt-seed"

#: Every schema the seed SQL creates. Each gets a read-only login for the
#: system agents (stacks/_db_readers.py); pinned equal to the seed files'
#: CREATE SCHEMA statements by tests/test_system_db_readers_stack.py.
SEED_READER_SCHEMAS = (
    "mes_line3", "cmms_maintenance", "plm_engineering", "erp_detroit", "mes_detroit",
    "cmms_detroit", "iot_historian", "cmms2_maintenance", "cmms_indianapolis",
    "supplier_master",
)


def adt_seed_version() -> str:
    """The seed CR's ``Version``: sha256 over (relative path, bytes) of every
    ``lambda-adt-seed/*.sql`` in name order (the order the Lambda applies
    them), hex[:16]. A new file changes it too; the handler does not.

    Read-only (``cdk synth`` stays pure); no SQL file raises."""
    paths = sorted((REPO_ROOT / ADT_SEED_DIR).glob("*.sql"))
    if not paths:
        raise FileNotFoundError(f"no seed SQL under {REPO_ROOT / ADT_SEED_DIR}")
    digest = hashlib.sha256()
    for path in paths:
        rel = f"{ADT_SEED_DIR}/{path.name}"
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


_PYTHON_BUNDLING_IMAGE = (
    "public.ecr.aws/docker/library/python:3.12-slim@"
    "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)


def _create_pg8000_layer(scope: Construct) -> lambda_.LayerVersion:
    """Build the seed Lambda's x86_64 PostgreSQL client layer: hash-locked
    pg8000 wheels plus the sample's ``pgdriver.py`` module."""
    layer_source = os.path.join(
        os.path.dirname(__file__), "..", "lambda-layers", "pg8000",
    )
    install = " ".join(
        (
            "python -m pip install --no-cache-dir --no-compile",
            "--require-hashes --only-binary=:all:",
            "--platform manylinux2014_x86_64 --implementation cp",
            "--python-version 3.12 --abi cp312",
            "--target /asset-output/python",
            "-r /asset-input/requirements-py312-linux-x86_64.lock",
        )
    )
    module = "cp /asset-input/pgdriver.py /asset-output/python/"
    normalize = (
        "find /asset-output -type d -exec chmod 755 {} + && "
        "find /asset-output -type f -exec chmod 644 {} + && "
        "find /asset-output -exec touch -h -d @0 {} +"
    )
    code = lambda_.Code.from_asset(
        layer_source,
        bundling=BundlingOptions(
            image=DockerImage.from_registry(_PYTHON_BUNDLING_IMAGE),
            command=["sh", "-c", f"{install} && {module} && {normalize}"],
            environment={
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_INDEX_URL": "https://pypi.org/simple",
            },
        ),
    )
    return lambda_.LayerVersion(
        scope,
        "Pg8000Layer",
        code=code,
        compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
        compatible_architectures=[lambda_.Architecture.X86_64],
        description="Hash-locked pg8000 1.31.6 and the pgdriver module for the demo seed Lambda",
    )


def _create_seed_function(scope, seed_role: iam.Role,
                          pg8000_layer: lambda_.ILayerVersion) -> lambda_.Function:
    """Create the architecture-aligned seed function with its local layer."""
    return lambda_.Function(
        scope,
        "AdtSeedLambda",
        function_name="adt-seed-data",
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.X86_64,
        handler="handler.handler",
        code=lambda_.Code.from_asset(
            os.path.join(os.path.dirname(__file__), "..", "lambda-adt-seed"),
            exclude=["__pycache__", "*.pyc"],
        ),
        timeout=Duration.minutes(5),
        memory_size=256,
        role=seed_role,
        vpc=scope.network_stack.vpc,
        vpc_subnets=ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
        ),
        environment={
            "DB_SECRET_ARN": scope.db_secret.secret_arn,
            "DB_HOST": scope.db_endpoint,
            "DB_NAME": "adt_demo",
            # The Version of the SQL this function ships, the same value the
            # custom resource carries: a CloudFormation Update asking for
            # another Version (an update rollback reaching the new code) is
            # skipped (lambda-adt-seed/handler.py: SEED_VERSION_ENV).
            "ADT_SEED_VERSION": adt_seed_version(),
        },
        layers=[pg8000_layer],
    )


class ADTSeedDataStack(Stack):
    """Demo-only: the `adt_demo` source-system RDS + static seed loader."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack,
        **kwargs,
    ) -> None:
        super().__init__(
            scope, construct_id,
            description="ADT - DEMO source-system RDS (adt_demo) + static seed (opt-in)",
            **kwargs,
        )
        self.network_stack = network_stack

        # --- RDS Postgres (adt_demo) — host for bundled sample data ---
        self.db_secret = secretsmanager.Secret(
            self, "AdtDbSecret",
            description="ADT demo RDS credentials",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "postgres"}',  # nosec B106 - Secrets Manager construct name or JSON template, not credential value
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.db_instance = self._create_database()
        # The VPC-wide rule only while DatabaseCidrIngress is keep (phase 1 of
        # an in-place update; stacks/_db_cidr_ingress.py).
        add_legacy_cidr_ingress(self, self.db_sg, network_stack.vpc.vpc_cidr_block,
                                "Allow Postgres from VPC")
        self.db_endpoint = self.db_instance.db_instance_endpoint_address

        self._create_seed_lambda()

        CfnOutput(self, "ADTRdsEndpoint", value=self.db_endpoint)
        CfnOutput(self, "ADTRdsSecretArn", value=self.db_secret.secret_arn)

    def _create_database(self) -> rds.DatabaseInstance:
        """The ``adt_demo`` instance and its own group.

        The group admits no CIDR (outside phase 1 of an in-place update, see
        stacks/_db_cidr_ingress.py): each client group gets its own 5432 rule,
        in this stack for the seed loader and the reader setup, in the
        client's stack for everything else (stacks/_db_access.py)."""
        adt_sg = ec2.SecurityGroup(
            self, "AdtDbSg",
            vpc=self.network_stack.vpc,
            description="ADT demo RDS",
            allow_all_outbound=False,
        )
        return rds.DatabaseInstance(
            # "AdtDatabase" is a legacy pre-rename construct ID, kept to avoid
            # resource replacement (renaming replaces the demo RDS instance =
            # data loss). Sanctioned residual — see the rename-plan allowlist.
            self, "AdtDatabase",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16,
            ),
            # db.t4g.small, not micro: on 2026-10-02 RDS stopped offering
            # db.t4g.micro in every us-east-1 AZ (CreateDBInstance failed with
            # "no Availability Zones with sufficient capacity"), which rolled
            # back a fresh deploy. The workshop package deploys only to
            # us-east-1, where small and t3.micro stay orderable in 5 AZs.
            # Changing the class modifies the instance in place (no
            # replacement); pinned by tests/test_seed_cr_versions.py.
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T4G, ec2.InstanceSize.SMALL,
            ),
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            security_groups=[adt_sg],
            # "adt_demo" here is the fictitious demo company's database — demo content, not engine identity (see rename plan)
            database_name="adt_demo",
            credentials=rds.Credentials.from_secret(self.db_secret),
            publicly_accessible=False,
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
            storage_encrypted=True,
            # Password logins keep working; IAM database authentication is
            # turned on as well (as on the ERP Aurora cluster). Changing it
            # modifies the instance in place.
            iam_authentication=True,
        )

    @property
    def db_sg(self) -> ec2.ISecurityGroup:
        """The demo RDS instance's own security group (``AdtDbSg``).

        A dependent stack whose client group must reach the database adds its
        own 5432 ingress on this group's id, so no dependency cycle forms
        (stacks/_db_access.py)."""
        (security_group,) = self.db_instance.connections.security_groups
        return security_group

    # ------------------------------------------------------------------
    def _create_seed_lambda(self) -> None:
        """Lambda that loads the bundled demo SQL seed files into the RDS instance."""
        seed_role = iam.Role(
            self, "AdtSeedRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole",
                ),
            ],
        )
        self.db_secret.grant_read(seed_role)

        self.pg8000_layer = _create_pg8000_layer(self)
        seed_lambda = _create_seed_function(self, seed_role, self.pg8000_layer)
        # The loader reaches the instance through its own group only, and
        # waits for that rule (so does the resource, through its provider).
        admit_postgres(self, "AdtDbIngressFromSeedLoader", database_sg=self.db_sg,
                       client_sg=seed_lambda.connections.security_groups[0],
                       description="Allow PostgreSQL from the seed loader", waiters=[seed_lambda])

        provider = cr.Provider(self, "AdtSeedProvider", on_event_handler=seed_lambda)

        # CloudFormation re-invokes a custom resource only when its properties
        # change: Version is the SQL content hash, so an Update re-runs the
        # (re-runnable, ADR-0084 amendment) seed files whenever they change.
        # A file that fails fails the custom resource; the function runs a
        # CloudFormation Update only for its own ADT_SEED_VERSION (the same
        # hash), so the rollback of a failed one that reaches the new code is
        # a no-op.
        seed_resource = CustomResource(
            self, "AdtSeedResource",
            service_token=provider.service_token,
            properties={"Version": adt_seed_version()},
        )
        seed_resource.node.add_dependency(self.db_instance)
        self._create_system_readers(seed_resource)

    def _create_system_readers(self, seed_resource: CustomResource) -> None:
        """The system agents' read-only logins, re-synced after every seed run
        (DataVersion is the same SQL hash)."""
        self.system_readers = SystemDbReaders(
            self, "SeedDbReaders",
            schemas=SEED_READER_SCHEMAS,
            owner_secret=self.db_secret,
            vpc=self.network_stack.vpc,
            layer=self.pg8000_layer,
            data_version=adt_seed_version(),
            database_label="demo seed database",
            after=seed_resource,
            database_sg=self.db_sg,
        )
