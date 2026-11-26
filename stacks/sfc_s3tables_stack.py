# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
SFC S3 Tables Foundation Stack

This stack owns the S3 Tables infrastructure that Shop Floor Connectivity (SFC)
writes shop-floor telemetry into, per ADR-0082:

- an S3 Tables **table bucket**,
- a **namespace** inside it, and
- a narrow, day-partitioned **table** (one row per reading).

It also creates the SFC **writer IAM policy**, scoped to the data-plane actions
SFC needs at run time and deliberately *excluding* the resource-creation actions.

ADR-0082 decision 4 — S3 Tables is CDK-owned; SFC runs `AutoCreate: false`.
    The `AWS-S3-TABLES` target defaults to `AutoCreate: true`, which would let
    SFC create the bucket, namespace and table itself — but that requires
    `CreateTableBucket`, `CreateNamespace` and `CreateTable` on SFC's task role.
    CDK creates all three here instead, so SFC's role narrows to the data-plane
    actions only (see `_create_writer_policy`). This also makes the table a
    teardownable stack resource and gives Athena a known target.

ADR-0082 decision 5 — the table is narrow, one row per reading, day-partitioned.
    Columns are `event_time` (`timestamptz`, required), `site`, `area`, `line`,
    `asset`, `channel` (`string`), `value` (`double`) and `quality` (`string`);
    the partition spec is `day(event_time)`. A narrow table answers "this
    property over this window" without a schema change per tag, which is what
    an Athena query path needs.

ADR-0082 note 2026-10-04: snapshots expire after `SNAPSHOT_MAX_AGE_HOURS`.
    The 120-hour service default let the Iceberg metadata.json, rewritten on
    every SFC commit, grow to tens of MB and slow every append.

The S3 Tables identifiers (bucket name/ARN, namespace, table name/ARN) and the
writer policy are exposed as stack attributes so the SFC runtime stack (T4) can
attach the policy to its task role and the flag/packaging wiring (T5) can gate
and instantiate this stack. This mirrors how `highbyte_opcua_stack.py` exposes
`self.opc_ua_endpoints` for downstream consumers.

This is a standalone stack: it creates its own resources and imports nothing, so
it synthesizes on its own for the unit lane.
"""

from typing import Optional

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_iam as iam,
    aws_s3tables as s3tables,
)
from constructs import Construct

from stacks import _workshop_story

# S3 Tables uses the Apache Iceberg open table format.
OPEN_TABLE_FORMAT = "ICEBERG"

# Default identifiers. S3 Tables bucket / namespace / table names are lowercase
# and free of demo literals (no site, tag or asset names appear here), so this
# module is engine configuration, not a demo literal home (ADR-0044).
DEFAULT_TABLE_BUCKET_NAME = "adt-sfc-telemetry"
DEFAULT_NAMESPACE = "sfc"
DEFAULT_TABLE_NAME = "readings"
# The one-time story history replay's own table (the replay ADR), created only
# when the live story packages the replay: the narrow schema plus
# `ingest_time`, so a point replayed twice resolves to its newest copy. A
# column on `readings` itself would replace that table on every existing
# deployment (an Iceberg schema change is a replacement in CloudFormation).
DEFAULT_REPLAY_TABLE_NAME = "readings_replay"   # sfc_config.REPLAY_TABLE_NAME
_REPLAY_EXTRA_FIELDS = [("ingest_time", "timestamptz", True)]

# ADR-0082 decision 5: the narrow one-row-per-reading schema. `event_time` is
# required (`timestamptz`); the remaining columns allow nulls. `value` is a
# double and the string dimensions carry the SiteWise hierarchy plus channel
# and quality.
_SCHEMA_FIELDS = [
    ("event_time", "timestamptz", True),
    ("site", "string", False),
    ("area", "string", False),
    ("line", "string", False),
    ("asset", "string", False),
    ("channel", "string", False),
    ("value", "double", False),
    ("quality", "string", False),
]

# The column the table is partitioned on, and the Iceberg partition transform.
_PARTITION_COLUMN = "event_time"
_PARTITION_TRANSFORM = "day"

# S3 Tables snapshot management for the readings table (ADR-0082 note
# 2026-10-04). The service default keeps every snapshot for 120 hours, and
# every SFC append rewrites the Iceberg metadata.json, which lists every
# retained snapshot. At about three commits a minute that file grows by
# roughly 0.75 KB per commit with nothing expiring for five days: a rehearsal
# table 2.7 days old carried 64,900 snapshots in a 49.5 MB metadata.json, and
# each append slowed with it. Six hours keeps about 1,100 snapshots (under
# 1 MB) at that rate. The maintenance job's cadence is not documented (one
# account showed about 19 hours between runs), so the retained window is this
# value plus that cadence; a smaller value would barely shorten it. Nothing in
# the app reads an older snapshot (Athena queries the current one). Changing
# these settings is an in-place update (CloudFormation SnapshotManagement:
# "No interruption"), so a deployed table is kept.
SNAPSHOT_MAX_AGE_HOURS = 6
SNAPSHOT_MIN_TO_KEEP = 1

# ADR-0082 decision 4: the SFC writer task role gets exactly these data-plane
# actions and NOTHING that creates resources. `CreateTableBucket`,
# `CreateNamespace` and `CreateTable` are DELIBERATELY absent — CDK owns the
# bucket, namespace and table, so SFC runs with `AutoCreate: false` and cannot
# create (or thereby mutate the shape of) the S3 Tables resources. Excluding
# those three actions is the whole point of decision 4.
SFC_WRITER_ACTIONS = [
    "s3tables:PutTableData",
    "s3tables:GetTable",
    "s3tables:GetTableData",
    "s3tables:GetTableMetadataLocation",
    "s3tables:UpdateTableMetadataLocation",
    "s3tables:ListNamespaces",
    "s3tables:ListTables",
    # The Iceberg REST catalog reads the bucket before loading the table
    # (2026-09-26 sandbox: ForbiddenException on GetTableBucket once
    # ListTableBuckets was granted). Read-only and bucket-scoped.
    "s3tables:GetTableBucket",
]

# SFC resolves the configured bucket NAME to its ARN by listing the account's
# table buckets (AwsS3TablesHelper.getTableBucketArn -> ListTableBuckets). That
# action is account-level -- it takes no bucket ARN -- so it cannot share the
# bucket-scoped statement and needs `Resource: *`. It is read-only (returns
# bucket names/ARNs) and creates nothing, so decision 4 still holds. Without it
# SFC reports the bucket as "does not exist" and never writes (seen on the
# 2026-09-26 sandbox deploy: 4x AccessDenied on ListTableBuckets in CloudTrail).
SFC_ACCOUNT_LIST_ACTIONS = ["s3tables:ListTableBuckets"]


def _iceberg_schema(fields=_SCHEMA_FIELDS) -> s3tables.CfnTable.IcebergSchemaProperty:
    """Iceberg schema — ADR-0082 decision 5's narrow one-row-per-reading shape."""
    return s3tables.CfnTable.IcebergSchemaProperty(
        schema_field_list=[
            s3tables.CfnTable.SchemaFieldProperty(
                name=name,
                type=field_type,
                required=required,
            )
            for name, field_type, required in fields
        ]
    )


def _partition_spec() -> s3tables.CfnTable.IcebergPartitionSpecProperty:
    """Partition spec — day-partitioned on `event_time`."""
    # `source_id` is the 1-based position of `event_time` in the schema field
    # list (Iceberg field ids are assigned in declaration order starting at 1).
    partition_source_id = (
        next(i for i, f in enumerate(_SCHEMA_FIELDS) if f[0] == _PARTITION_COLUMN)
        + 1
    )
    return s3tables.CfnTable.IcebergPartitionSpecProperty(
        fields=[
            s3tables.CfnTable.IcebergPartitionFieldProperty(
                name=_PARTITION_COLUMN,
                source_id=partition_source_id,
                transform=_PARTITION_TRANSFORM,
            )
        ]
    )


class SfcS3TablesStack(Stack):
    """
    SFC S3 Tables foundation: table bucket, namespace, narrow day-partitioned
    table, and the narrow SFC writer IAM policy (ADR-0082 decisions 4 and 5).

    - Creates the S3 Tables bucket / namespace / table with the decision-5 schema.
    - Builds the SFC writer managed policy (data-plane only; no Create* actions).
    - Exposes bucket / namespace / table identifiers and the writer policy as
      stack attributes for the SFC runtime stack (T4) and the packaging wiring (T5).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table_bucket_name: str = DEFAULT_TABLE_BUCKET_NAME,
        namespace: str = DEFAULT_NAMESPACE,
        table_name: str = DEFAULT_TABLE_NAME,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description="ADT - SFC S3 Tables foundation (table bucket, namespace, telemetry table)",
            **kwargs,
        )

        # Identifiers exposed for downstream stacks (T4/T5).
        self.table_bucket_name = table_bucket_name
        self.namespace_name = namespace
        self.table_name = table_name
        self.replay_table_name = DEFAULT_REPLAY_TABLE_NAME

        self._create_table()
        if _workshop_story.backfill_from_node(self.node):
            self._create_replay_table()
        self._create_writer_policy()
        self._create_exports()

    def _create_table(self) -> None:
        """Create the table bucket, namespace and narrow day-partitioned table."""
        # Table bucket. CDK-owned so it is teardownable with the stack.
        self.table_bucket = s3tables.CfnTableBucket(
            self,
            "SfcTableBucket",
            table_bucket_name=self.table_bucket_name,
        )
        self.table_bucket.apply_removal_policy(RemovalPolicy.DESTROY)
        self.table_bucket_arn = self.table_bucket.attr_table_bucket_arn

        # Namespace inside the bucket.
        self.namespace = s3tables.CfnNamespace(
            self,
            "SfcNamespace",
            namespace=self.namespace_name,
            table_bucket_arn=self.table_bucket_arn,
        )
        # The namespace must exist before the table is created within it.
        self.namespace.add_dependency(self.table_bucket)

        self.table = s3tables.CfnTable(
            self,
            "SfcTable",
            namespace=self.namespace_name,
            open_table_format=OPEN_TABLE_FORMAT,
            table_bucket_arn=self.table_bucket_arn,
            table_name=self.table_name,
            iceberg_metadata=s3tables.CfnTable.IcebergMetadataProperty(
                iceberg_schema=_iceberg_schema(),
                iceberg_partition_spec=_partition_spec(),
            ),
            # Compaction stays at the service default (not set here).
            snapshot_management=s3tables.CfnTable.SnapshotManagementProperty(
                status="enabled",
                min_snapshots_to_keep=SNAPSHOT_MIN_TO_KEEP,
                max_snapshot_age_hours=SNAPSHOT_MAX_AGE_HOURS,
            ),
        )
        self.table.add_dependency(self.namespace)
        self.table.apply_removal_policy(RemovalPolicy.DESTROY)
        self.table_arn = self.table.attr_table_arn

    def _create_replay_table(self) -> None:
        """The story history replay's table (the replay ADR): same bucket,
        namespace, partitioning and snapshot policy as ``readings``, plus
        ``ingest_time``. The writer policy's ``/table/*`` already covers it."""
        self.replay_table = s3tables.CfnTable(
            self,
            "SfcReplayTable",
            namespace=self.namespace_name,
            open_table_format=OPEN_TABLE_FORMAT,
            table_bucket_arn=self.table_bucket_arn,
            table_name=self.replay_table_name,
            iceberg_metadata=s3tables.CfnTable.IcebergMetadataProperty(
                iceberg_schema=_iceberg_schema(_SCHEMA_FIELDS + _REPLAY_EXTRA_FIELDS),
                iceberg_partition_spec=_partition_spec(),
            ),
            snapshot_management=s3tables.CfnTable.SnapshotManagementProperty(
                status="enabled",
                min_snapshots_to_keep=SNAPSHOT_MIN_TO_KEEP,
                max_snapshot_age_hours=SNAPSHOT_MAX_AGE_HOURS,
            ),
        )
        self.replay_table.add_dependency(self.namespace)
        self.replay_table.apply_removal_policy(RemovalPolicy.DESTROY)

    def _create_writer_policy(self) -> None:
        """Build the narrow SFC writer IAM policy (ADR-0082 decision 4).

        Scoped to this stack's own table bucket and the tables within it. The
        allowed actions are the data-plane set in ``SFC_WRITER_ACTIONS``;
        ``CreateTableBucket`` / ``CreateNamespace`` / ``CreateTable`` are
        deliberately NOT granted, so SFC (running ``AutoCreate: false``) can
        write readings but cannot create or reshape the S3 Tables resources.
        """
        self.sfc_writer_policy = iam.ManagedPolicy(
            self,
            "SfcWriterPolicy",
            description="SFC S3 Tables writer — data-plane only, no resource creation (ADR-0082 D4)",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=SFC_WRITER_ACTIONS,
                    # The bucket ARN covers the bucket-level list actions
                    # (ListNamespaces / ListTables); the `/table/*` pattern
                    # covers the table-level data actions. Scoping to this
                    # stack's own bucket keeps the grant narrow.
                    resources=[
                        self.table_bucket_arn,
                        f"{self.table_bucket_arn}/table/*",
                    ],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=SFC_ACCOUNT_LIST_ACTIONS,
                    # Account-level list action: no resource-level scoping exists.
                    resources=["*"],
                ),
            ],
        )

    def _create_exports(self) -> None:
        """Add stack outputs for the S3 Tables identifiers T4/T5 consume."""
        CfnOutput(
            self,
            "SfcTableBucketName",
            value=self.table_bucket_name,
            description="Amazon S3 Tables bucket name for SFC telemetry",
        )
        CfnOutput(
            self,
            "SfcTableBucketArn",
            value=self.table_bucket_arn,
            description="Amazon S3 Tables bucket ARN for SFC telemetry",
        )
        CfnOutput(
            self,
            "SfcNamespaceName",
            value=self.namespace_name,
            description="Amazon S3 Tables namespace for SFC telemetry",
        )
        CfnOutput(
            self,
            "SfcTableName",
            value=self.table_name,
            description="Amazon S3 Tables table name for SFC telemetry (one row per reading)",
        )
        CfnOutput(
            self,
            "SfcTableArn",
            value=self.table_arn,
            description="Amazon S3 Tables table ARN for SFC telemetry",
        )
