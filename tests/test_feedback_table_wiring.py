# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression test for the user-feedback datastore (2026-08-17 fix).

The live gap: no stack provisioned a feedback table and nothing set
``FEEDBACK_TABLE_NAME``, so ``FeedbackStorage.store`` no-oped while
``POST /api/feedback`` answered ``{"status": "success"}`` — every thumbs-up/down
on every deployed environment was discarded. Confirmed on the deployed
Management task definition, whose only table env vars were
``ONBOARDING_SESSIONS_TABLE``, ``DASHBOARD_CARDS_TABLE_NAME``,
``PROMPT_TEMPLATES_TABLE_NAME`` and ``PROPOSED_MAPPINGS_TABLE``.

Three things must hold in the synthesized templates:

* ADTDatabaseStack provisions ``adt-feedback`` with the key schema, the date
  GSI, and TTL ENABLED (feedback carries customer-derived free text);
* the ``adt-app`` container carries ``FEEDBACK_TABLE_NAME``;
* the task role's feedback grant is LEAST PRIVILEGE — PutItem + Query on the
  table and the one index, and nothing else.

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node
under node_modules — skips if unavailable (mirrors
tests/test_adt_compute_env_wiring.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest
from _role_policies import named_documents

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
DB_STACK = "ADTDatabaseStack"
COMPUTE_STACK = "ADTComputeStack"
FEEDBACK_TABLE = "adt-feedback"
FEEDBACK_INDEX = "feedback-date-index"


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


@pytest.fixture(scope="session")
def templates(tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    out_dir = tmp_path_factory.mktemp("cdk_synth_feedback")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            cdk_bin, "synth", DB_STACK, COMPUTE_STACK,
            "-c", "region=us-east-1",
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=900,
    )
    out: dict = {}
    for stack in (DB_STACK, COMPUTE_STACK):
        path = out_dir / f"{stack}.template.json"
        if not path.exists():
            pytest.skip(f"synth did not produce {stack}:\n{result.stderr[-2000:]}")
        out[stack] = json.loads(path.read_text())
    return out


def _feedback_table(template: dict) -> dict:
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::DynamoDB::Table":
            continue
        if resource["Properties"].get("TableName") == FEEDBACK_TABLE:
            return resource["Properties"]
    pytest.fail(f"{FEEDBACK_TABLE} table not found in {DB_STACK}")


def test_database_stack_provisions_the_feedback_table(templates):
    props = _feedback_table(templates[DB_STACK])
    assert props["KeySchema"] == [
        {"AttributeName": "feedback_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ]
    assert props["BillingMode"] == "PAY_PER_REQUEST"


def test_feedback_table_enables_ttl_on_the_documented_attribute(templates):
    """Feedback is customer-derived free text — retention is bounded by design
    (90 days, agents/storage.py::FEEDBACK_TTL_DAYS)."""
    props = _feedback_table(templates[DB_STACK])
    assert props["TimeToLiveSpecification"] == {
        "AttributeName": "ttl", "Enabled": True}


def test_feedback_table_has_the_date_index_for_time_range_reads(templates):
    """The admin surface reads by time range; without this GSI the read is a
    full-table scan, which would also force a wider IAM grant."""
    props = _feedback_table(templates[DB_STACK])
    indexes = {i["IndexName"]: i for i in props.get("GlobalSecondaryIndexes", [])}
    assert FEEDBACK_INDEX in indexes, sorted(indexes)
    assert indexes[FEEDBACK_INDEX]["KeySchema"] == [
        {"AttributeName": "feedback_date", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ]


def test_feedback_table_is_destroyed_on_teardown(templates):
    """Sibling convention (RemovalPolicy.DESTROY) — a demo teardown must not
    leave a billable table behind."""
    for resource in templates[DB_STACK].get("Resources", {}).values():
        if (resource.get("Type") == "AWS::DynamoDB::Table"
                and resource["Properties"].get("TableName") == FEEDBACK_TABLE):
            assert resource.get("DeletionPolicy") == "Delete"
            return
    pytest.fail(f"{FEEDBACK_TABLE} table not found in {DB_STACK}")


def test_task_def_injects_feedback_table_name(templates):
    """The missing env var IS the bug."""
    found = None
    for resource in templates[COMPUTE_STACK].get("Resources", {}).values():
        if resource.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for cdef in resource["Properties"].get("ContainerDefinitions", []):
            if cdef.get("Name") != "adt-app":
                continue
            for entry in cdef.get("Environment", []):
                if entry.get("Name") == "FEEDBACK_TABLE_NAME":
                    found = entry.get("Value")
    assert found == FEEDBACK_TABLE


def _feedback_policy(template: dict) -> dict:
    for name, document in named_documents(template):
        if name == "DynamoDBFeedback":
            return document
    pytest.fail("DynamoDBFeedback policy not found on any role")


def test_feedback_iam_grant_is_least_privilege(templates):
    doc = _feedback_policy(templates[COMPUTE_STACK])
    assert len(doc["Statement"]) == 1
    statement = doc["Statement"][0]
    # Only the actions the code actually performs: store() -> PutItem,
    # get_by_time_range() -> Query on the GSI.
    assert sorted(statement["Action"]) == ["dynamodb:PutItem", "dynamodb:Query"]
    rendered = json.dumps(statement["Resource"])
    assert f"table/{FEEDBACK_TABLE}" in rendered
    assert f"table/{FEEDBACK_TABLE}/index/{FEEDBACK_INDEX}" in rendered
    # Never a wildcard index or a wildcard resource.
    assert "index/*" not in rendered
    assert statement["Resource"] != "*"


def test_broad_dynamodb_grant_does_not_cover_the_feedback_table(templates):
    """The six-action shared statement must not silently re-grant Scan/Delete on
    feedback — that is what the separate narrow policy exists to prevent."""
    names = [name for name, _ in named_documents(templates[COMPUTE_STACK])]
    assert "DynamoDB" in names
    for name, document in named_documents(templates[COMPUTE_STACK]):
        if name != "DynamoDB":
            continue
        rendered = json.dumps(document)
        assert f"table/{FEEDBACK_TABLE}" not in rendered
