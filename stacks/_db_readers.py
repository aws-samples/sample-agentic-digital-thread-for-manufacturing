# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read-only database logins for the system agents (one per schema).

A system agent runs SQL its model wrote, so it must not connect as the
database owner. Each database ADT provisions (the seed RDS instance and the
ERP Aurora cluster) gets one login per schema it carries, named
``adt_reader_<schema>``, that can read that schema and nothing else. The app
reads the login's secret when it starts the system agent for that schema and
puts the login, never the owner's, into the agent's connection config
(``agents/demo/db_routing.py``).

Per schema rather than per database: a system agent can then read only its
own schema, not every other system on the same server. The cost is one
generated AWS Secrets Manager secret per schema (16 across both databases,
counting the optional Tulip plant's, which is kept while the plant is off),
one setup function, layer and custom resource per database, and one IAM
statement each on the setup function and the app task role that lists the
reader secrets by ARN.

The setup custom resource (``lambda-db-reader/handler.py``) re-runs whenever
the database's own seed or initialisation re-runs (``data_version``), because
a reset recreates schemas and tables and drops their grants, and whenever
its own handler or reader list changes. A function that resets the database
outside CloudFormation (the ERP db-init function, which the ERP dashboard's
Reset All invokes directly) re-runs the setup itself afterwards with the
custom resource's own Update event (``direct_reset_step``,
stacks/_direct_reset.py).

Each secret is read by ARN. Its generated name starts with the construct id
(``SeedDbReaders...`` / ``ErpDbReaders...``), which no role's name wildcard
matches (``adt*``, ``AdtDbSecret*``, ``DatabaseSecret*``), so only the roles
granted here can read it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from aws_cdk import (
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from stacks._checkov_skips import checkov_skip
from stacks._db_access import admit_postgres
from stacks._direct_reset import Step, custom_resource_step

REPO_ROOT = Path(__file__).resolve().parents[1]
READER_HANDLER_DIR = REPO_ROOT / "lambda-db-reader"
READER_ROLE_PREFIX = "adt_reader_"
POSTGRES_PORT = 5432
#: The physical id the setup handler always reports
#: (lambda-db-reader/handler.py PHYSICAL_RESOURCE_ID).
READER_PHYSICAL_RESOURCE_ID = "adt-system-db-readers"
#: What the setup restores, in the message of a direct reset it fails.
READERS_STEP_NAME = "the system agents' read-only logins"
#: The setup function's timeout. Its run on the ERP database took 4.5 s live;
#: after a direct ERP reset db-init waits on it inside its own deadline
#: (stacks/_direct_reset.py), alongside the other steps.
SETUP_TIMEOUT = Duration.minutes(2)

_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]*$")


def reader_role_name(schema: str) -> str:
    """The login a system agent for ``schema`` connects with."""
    role = f"{READER_ROLE_PREFIX}{schema}"
    if not _SCHEMA.match(schema or "") or len(role) > 63:
        raise ValueError(f"not a usable schema name for a reader login: {schema!r}")
    return role


def db_reader_version() -> str:
    """The setup custom resource's ``Version``: sha256 of its handler,
    hex[:16], so a changed setup re-runs on the next deploy. Read-only."""
    return hashlib.sha256((READER_HANDLER_DIR / "handler.py").read_bytes()).hexdigest()[:16]


def _camel(schema: str) -> str:
    return "".join(part.capitalize() for part in schema.split("_") if part)


class SystemDbReaders(Construct):
    """One reader login per schema on one database, kept by a custom resource.

    ``secrets`` maps each schema to its reader secret; ``resource`` is the
    setup custom resource (it depends on ``after``, the database's own seed or
    initialisation resource) and ``properties`` its properties but the
    ServiceToken. The database's group ``database_sg`` admits the setup
    function's group on 5432 (a rule in this construct), and both the
    function and the resource wait for that rule.

    ``secret_only_schemas`` get the same generated secret and NO login: an
    optional schema that is currently off keeps its secret, so the secret (and
    an export of it) is the same whether the schema is on or off. Those
    secrets are in ``secret_only``; they are not set on the database, the
    setup function cannot read them, and :meth:`secret_arns` leaves them out."""

    def __init__(self, scope: Construct, construct_id: str, *, schemas: tuple[str, ...],
                 owner_secret: secretsmanager.ISecret, vpc: ec2.IVpc,
                 layer: lambda_.ILayerVersion, data_version: str, database_label: str,
                 after: Construct, database_sg: ec2.ISecurityGroup,
                 secret_only_schemas: tuple[str, ...] = ()) -> None:
        super().__init__(scope, construct_id)
        every = tuple(schemas) + tuple(secret_only_schemas)
        if len(set(every)) != len(every) or not schemas:
            raise ValueError(f"reader schemas must be unique and non-empty: {every!r}")
        self.secrets = {schema: self._secret(schema, database_label) for schema in schemas}
        self.secret_only = {schema: self._secret(schema, database_label)
                            for schema in secret_only_schemas}
        self.function = self._function(vpc, layer, database_label)
        (setup_sg,) = self.function.connections.security_groups
        database_ingress = admit_postgres(
            self, "SetupDbIngress", database_sg=database_sg, client_sg=setup_sg,
            description=f"Allow PostgreSQL from the {database_label} reader setup",
            waiters=[self.function])
        owner_secret.grant_read(self.function)
        self.function.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[secret.secret_arn for secret in self.secrets.values()],
        ))
        self.provider = cr.Provider(self, "Provider", on_event_handler=self.function)
        self.properties = {
            "MasterSecretArn": owner_secret.secret_arn,
            "Readers": [{"Schema": schema, "Role": reader_role_name(schema),
                         "SecretArn": secret.secret_arn}
                        for schema, secret in self.secrets.items()],
            "Version": db_reader_version(),
            "DataVersion": data_version,
        }
        self.resource = CustomResource(
            self,
            "Resource",
            service_token=self.provider.service_token,
            properties=self.properties,
        )
        self.resource.node.add_dependency(after, database_ingress)
        self._checkov_skips(self.function, self.provider)

    def direct_reset_step(self) -> Step:
        """The step a function that drops and recreates the schemas outside
        CloudFormation runs after it (stacks/_direct_reset.py): the Update
        event CloudFormation sends the setup custom resource, with its
        deployed properties (ServiceToken included). The setup handler stays
        the only code that grants."""
        properties = {"ServiceToken": self.provider.service_token, **self.properties}
        return custom_resource_step(READERS_STEP_NAME, self.function, READER_PHYSICAL_RESOURCE_ID,
                                    properties)

    def _secret(self, schema: str, database_label: str) -> secretsmanager.Secret:
        return secretsmanager.Secret(
            self,
            f"Reader{_camel(schema)}",
            description=(f"ADT system agent read-only login on schema {schema} of the "
                         f"{database_label}; delete and redeploy the stack to regenerate."),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": reader_role_name(schema)}),
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _function(self, vpc: ec2.IVpc, layer: lambda_.ILayerVersion,
                  database_label: str) -> lambda_.Function:
        """The non-public setup function: invoked by CloudFormation and, after
        a direct reset, by the resetter that runs ``direct_reset_step``."""
        security_group = ec2.SecurityGroup(
            self, "SetupSg", vpc=vpc, allow_all_outbound=False,
            description=f"ADT system reader setup: Postgres to the {database_label} (VPC) and HTTPS only")
        security_group.add_egress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(POSTGRES_PORT),
                                       "Postgres in the VPC")
        security_group.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
                                       "Secrets Manager over HTTPS")
        return lambda_.Function(
            self,
            "SetupFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(READER_HANDLER_DIR), exclude=["__pycache__", "*.pyc"]),
            timeout=SETUP_TIMEOUT,
            memory_size=256,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[security_group],
            layers=[layer],
            description=f"ADT: keeps the system agents' read-only logins on the {database_label}",
        )

    def _checkov_skips(self, function: lambda_.Function, provider: cr.Provider) -> None:
        """checkov skips with reasons (the stacks/_checkov_skips.py contract;
        the erp_console login secret makes the same key trade-off)."""
        for secret in (*self.secrets.values(), *self.secret_only.values()):
            checkov_skip(secret, [_SECRET_CMK])
        checkov_skip(function, [_SETUP_CONCURRENCY, _SETUP_DLQ])
        checkov_skip(provider.node.find_child("framework-onEvent"),
                     [_SETUP_CONCURRENCY, _SETUP_DLQ, _FRAMEWORK_VPC, _FRAMEWORK_ENV_CMK])

    def secret_arns(self) -> dict[str, str]:
        """``{schema: secret ARN}`` (CloudFormation tokens at synth)."""
        return {schema: secret.secret_arn for schema, secret in self.secrets.items()}

    def secret_for(self, schema: str) -> secretsmanager.Secret:
        """The reader secret of ``schema``, with a login or secret-only."""
        return {**self.secret_only, **self.secrets}[schema]


_SECRET_CMK = (
    "CKV_AWS_149",
    "Encrypted with the AWS-managed Secrets Manager key. The secret only opens a "
    "login that can read one demo schema inside the VPC; a customer-managed key "
    "adds a per-account key and policy surface with no benefit for this demo workload.",
)
_SETUP_CONCURRENCY = (
    "CKV_AWS_115",
    "Invoked by CloudFormation once per create, update or delete of the reader "
    "custom resource, and on the ERP database once per direct reset by the "
    "db-init function; reserved concurrency would take capacity from the "
    "account pool for a function that runs a few times a day at most.",
)
_SETUP_DLQ = (
    "CKV_AWS_116",
    "Every caller invokes it synchronously and records a failure itself: "
    "CloudFormation as a failed stack event, the db-init function as a failed "
    "reset; a dead-letter queue would never receive an event.",
)
_FRAMEWORK_VPC = (
    "CKV_AWS_117",
    "The CDK custom-resource provider framework function only invokes the setup "
    "function and answers CloudFormation; it reaches no VPC resource. The setup "
    "function itself runs in the private subnets.",
)
_FRAMEWORK_ENV_CMK = (
    "CKV_AWS_173",
    "The provider framework function's only environment variable is the setup "
    "function's ARN, no secret; it is encrypted at rest with the AWS-managed key.",
)
