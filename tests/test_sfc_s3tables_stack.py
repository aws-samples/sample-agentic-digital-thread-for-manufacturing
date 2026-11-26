# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the SFC S3 Tables foundation stack.

These guard the two ADR-0082 decisions the stack implements:

* **Decision 5** — the narrow one-row-per-reading schema (`event_time`
  ``timestamptz`` and required, plus the string/double columns) and the
  ``day(event_time)`` partitioning.
* **Decision 4** — the SFC writer IAM policy grants exactly the seven
  data-plane actions and NONE of the ``Create*`` actions, so SFC (running
  ``AutoCreate: false``) cannot create or reshape the S3 Tables resources.

The stack is synthesized in-process into its own ``cdk.App`` (it is standalone
and imports nothing), and the resulting CloudFormation template is asserted
against with ``aws_cdk.assertions.Template``. Requires ``aws-cdk-lib`` to be
importable — the tests skip with an actionable message if it is not (mirrors
the CDK-prerequisite skips in ``tests/test_cross_account_grant.py``).

Run:  PYTHONPATH=. python -m pytest tests/test_sfc_s3tables_stack.py -q
"""
from __future__ import annotations

import importlib.util

import pytest

if importlib.util.find_spec("aws_cdk") is None:
    pytest.skip(
        "aws-cdk-lib not importable — activate the .venv and "
        "`pip install -r requirements.txt`",
        allow_module_level=True,
    )

import aws_cdk as cdk  # noqa: E402
from aws_cdk import assertions  # noqa: E402

from stacks.sfc_s3tables_stack import (  # noqa: E402
    SFC_ACCOUNT_LIST_ACTIONS,
    SFC_WRITER_ACTIONS,
    SNAPSHOT_MAX_AGE_HOURS,
    SNAPSHOT_MIN_TO_KEEP,
    SfcS3TablesStack,
)

# Placeholder account id per the repo convention for unit tests/docs.
_ENV = cdk.Environment(account="111122223333", region="us-east-1")

# The exact ADR-0082 decision-5 schema: (name, iceberg type, required).
_EXPECTED_SCHEMA = [
    ("event_time", "timestamptz", True),
    ("site", "string", False),
    ("area", "string", False),
    ("line", "string", False),
    ("asset", "string", False),
    ("channel", "string", False),
    ("value", "double", False),
    ("quality", "string", False),
]

# The Create* actions decision 4 deliberately withholds from the writer.
_FORBIDDEN_ACTIONS = {
    "s3tables:CreateTableBucket",
    "s3tables:CreateNamespace",
    "s3tables:CreateTable",
}


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    """Synthesize the standalone stack once and return its CFN template."""
    app = cdk.App()
    stack = SfcS3TablesStack(app, "TestSfcS3TablesStack", env=_ENV)
    return assertions.Template.from_stack(stack)


def _table_props(template: assertions.Template) -> dict:
    tables = template.find_resources("AWS::S3Tables::Table")
    assert len(tables) == 1, f"expected exactly one S3 Tables table, found {len(tables)}"
    return next(iter(tables.values()))["Properties"]


# ---------------------------------------------------------------------------
# Resources exist (bucket, namespace, table)
# ---------------------------------------------------------------------------
def test_creates_bucket_namespace_and_table(template):
    template.resource_count_is("AWS::S3Tables::TableBucket", 1)
    template.resource_count_is("AWS::S3Tables::Namespace", 1)
    template.resource_count_is("AWS::S3Tables::Table", 1)


def test_table_uses_iceberg_open_format(template):
    assert _table_props(template)["OpenTableFormat"] == "ICEBERG"


# ---------------------------------------------------------------------------
# Decision 5: narrow one-row-per-reading schema, day-partitioned
# ---------------------------------------------------------------------------
def test_schema_columns_names_types_and_order(template):
    fields = _table_props(template)["IcebergMetadata"]["IcebergSchema"]["SchemaFieldList"]
    got = [(f["Name"], f["Type"]) for f in fields]
    expected = [(name, field_type) for name, field_type, _ in _EXPECTED_SCHEMA]
    assert got == expected, f"schema columns/types differ from ADR-0082 D5: {got}"


def test_event_time_is_timestamptz_and_required(template):
    fields = _table_props(template)["IcebergMetadata"]["IcebergSchema"]["SchemaFieldList"]
    event_time = next(f for f in fields if f["Name"] == "event_time")
    assert event_time["Type"] == "timestamptz"
    assert event_time.get("Required") is True, "event_time must not be optional (ADR-0082 D5)"


def test_value_is_double(template):
    fields = _table_props(template)["IcebergMetadata"]["IcebergSchema"]["SchemaFieldList"]
    value = next(f for f in fields if f["Name"] == "value")
    assert value["Type"] == "double"


def test_partitioned_by_day_on_event_time(template):
    spec = _table_props(template)["IcebergMetadata"]["IcebergPartitionSpec"]
    part_fields = spec["Fields"]
    assert len(part_fields) == 1, "table must be partitioned on a single field (day/event_time)"
    field = part_fields[0]
    assert field["Name"] == "event_time"
    assert field["Transform"] == "day"


# ---------------------------------------------------------------------------
# Maintenance: snapshots expire in hours, not the 120 h service default
# (ADR-0082 note 2026-10-04)
# ---------------------------------------------------------------------------
def test_snapshot_management_expires_snapshots_within_hours(template):
    # Every SFC commit rewrites metadata.json, which lists every retained
    # snapshot; at the 120 h default it reached 49.5 MB on a rehearsal table.
    assert _table_props(template)["SnapshotManagement"] == {
        "Status": "enabled",
        "MinSnapshotsToKeep": SNAPSHOT_MIN_TO_KEEP,
        "MaxSnapshotAgeHours": SNAPSHOT_MAX_AGE_HOURS,
    }
    assert SNAPSHOT_MIN_TO_KEEP == 1
    assert 1 <= SNAPSHOT_MAX_AGE_HOURS <= 24, "keep the retained snapshot window short"


def test_compaction_stays_at_the_service_default(template):
    assert "Compaction" not in _table_props(template)


# ---------------------------------------------------------------------------
# Decision 4: narrow writer IAM policy, no resource-creation actions
# ---------------------------------------------------------------------------
def _writer_actions(template: assertions.Template) -> set[str]:
    policies = template.find_resources("AWS::IAM::ManagedPolicy")
    assert len(policies) == 1, f"expected exactly one writer managed policy, found {len(policies)}"
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    actions: set[str] = set()
    for stmt in statements:
        act = stmt["Action"]
        actions.update([act] if isinstance(act, str) else act)
    return actions


def test_writer_actions_are_exactly_the_data_plane_set(template):
    assert _writer_actions(template) == set(SFC_WRITER_ACTIONS) | set(SFC_ACCOUNT_LIST_ACTIONS)


def test_only_the_account_list_action_is_unscoped(template):
    """``Resource: *`` may carry ListTableBuckets and nothing else; every
    data-plane action stays scoped to this stack's own bucket."""
    policies = template.find_resources("AWS::IAM::ManagedPolicy")
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    for stmt in statements:
        act = stmt["Action"]
        acts = {act} if isinstance(act, str) else set(act)
        res = stmt["Resource"]
        if res == "*" or (isinstance(res, list) and "*" in res):
            assert acts == {"s3tables:ListTableBuckets"}, sorted(acts)
        else:
            assert not acts & {"s3tables:ListTableBuckets"}


def test_sfc_can_resolve_the_bucket_name_to_its_arn():
    # SFC looks the bucket up by name via ListTableBuckets; without it the
    # writer reports the bucket as missing and never writes (sandbox 2026-09-26).
    assert SFC_ACCOUNT_LIST_ACTIONS == ["s3tables:ListTableBuckets"]


def test_writer_excludes_resource_creation_actions(template):
    actions = _writer_actions(template)
    leaked = actions & _FORBIDDEN_ACTIONS
    assert not leaked, (
        f"SFC writer must NOT be able to create S3 Tables resources (ADR-0082 D4); "
        f"found {sorted(leaked)}"
    )


def test_writer_actions_match_adr_list(template):
    # The exact eight bucket-scoped actions ADR-0082 decision 4 names.
    assert set(SFC_WRITER_ACTIONS) == {
        "s3tables:GetTableBucket",
        "s3tables:PutTableData",
        "s3tables:GetTable",
        "s3tables:GetTableData",
        "s3tables:GetTableMetadataLocation",
        "s3tables:UpdateTableMetadataLocation",
        "s3tables:ListNamespaces",
        "s3tables:ListTables",
    }


# ---------------------------------------------------------------------------
# Downstream contract: identifiers exposed as stack attributes for T4/T5
# ---------------------------------------------------------------------------
def test_exposes_identifiers_as_attributes():
    app = cdk.App()
    stack = SfcS3TablesStack(app, "AttrsSfcS3TablesStack", env=_ENV)
    assert stack.table_bucket_name == "adt-sfc-telemetry"
    assert stack.namespace_name == "sfc"
    assert stack.table_name == "readings"
    # ARNs and the writer policy are resolvable tokens/constructs, not None.
    assert stack.table_bucket_arn is not None
    assert stack.table_arn is not None
    assert stack.sfc_writer_policy is not None


# ---------------------------------------------------------------------------
# The story history replay's own table (the replay ADR): only when the live
# story packages the replay; the default stack is unchanged.
# ---------------------------------------------------------------------------
_LIVE = {"with_workshop": "true", "workshop_story": "live"}


def _tables(context: dict) -> dict:
    app = cdk.App(context=context)
    stack = SfcS3TablesStack(app, "TestSfcS3TablesStack", env=_ENV)
    found = assertions.Template.from_stack(stack).find_resources("AWS::S3Tables::Table")
    return {r["Properties"]["TableName"]: r["Properties"] for r in found.values()}


def test_the_live_story_adds_the_replay_table_and_leaves_readings_as_it_was():
    tables = _tables(_LIVE)
    assert set(tables) == {"readings", "readings_replay"}
    assert tables["readings"] == _tables({})["readings"], "the live table is not touched"
    replay = tables["readings_replay"]
    fields = replay["IcebergMetadata"]["IcebergSchema"]["SchemaFieldList"]
    assert [(f["Name"], f["Type"], f.get("Required", False)) for f in fields] == [
        *_EXPECTED_SCHEMA, ("ingest_time", "timestamptz", True)]
    assert replay["IcebergMetadata"]["IcebergPartitionSpec"] == tables["readings"]["IcebergMetadata"][
        "IcebergPartitionSpec"]
    assert replay["SnapshotManagement"] == tables["readings"]["SnapshotManagement"]


def test_the_replay_table_name_matches_the_sfc_config():
    from stacks.sfc_stack import load_sfc_config_generator
    from stacks.sfc_s3tables_stack import DEFAULT_REPLAY_TABLE_NAME
    assert load_sfc_config_generator().REPLAY_TABLE_NAME == DEFAULT_REPLAY_TABLE_NAME


def test_no_replay_table_without_the_switch():
    assert set(_tables({})) == {"readings"}
    assert set(_tables({**_LIVE, "story_feed_backfill": "false"})) == {"readings"}
    assert set(_tables({"with_workshop": "true", "workshop_story": "frozen"})) == {"readings"}
