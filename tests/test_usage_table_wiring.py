# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the usage table.

The live gap (a deployed account, 2026-10-21): no stack provisioned a usage table and
the ``adt-app`` task had no ``USAGE_TABLE_NAME``, so every usage write went to
``UsageStorage(None)`` and the admin usage stats read nothing.

What must hold in the synthesized templates:

* ADTDatabaseStack provisions ``adt-usage`` keyed ``user_id`` + ``timestamp``,
  with the feedback table's conventions (on-demand billing, the same
  encryption and point-in-time-recovery settings, DESTROY, TTL on ``ttl``);
* the ``adt-app`` container carries ``USAGE_TABLE_NAME``;
* the task role's usage grant is PutItem + Scan on that ONE table, and no
  other policy names it.

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node
under node_modules and skips if unavailable (mirrors
tests/test_feedback_table_wiring.py).
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
USAGE_TABLE = "adt-usage"
FEEDBACK_TABLE = "adt-feedback"


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


@pytest.fixture(scope="session")
def templates(tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_usage")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", DB_STACK, COMPUTE_STACK, "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    out: dict = {}
    for stack in (DB_STACK, COMPUTE_STACK):
        path = out_dir / f"{stack}.template.json"
        if not path.exists():
            pytest.skip(f"synth did not produce {stack}:\n{result.stderr[-2000:]}")
        out[stack] = json.loads(path.read_text())
    return out


def _table(template: dict, name: str) -> dict:
    for resource in template.get("Resources", {}).values():
        if (resource.get("Type") == "AWS::DynamoDB::Table"
                and resource["Properties"].get("TableName") == name):
            return resource
    pytest.fail(f"{name} table not found in {DB_STACK}")


def _policies(template: dict):
    yield from named_documents(template)


# ── the usage table ────────────────────────────────────────────────────────

def test_database_stack_provisions_the_usage_table(templates):
    props = _table(templates[DB_STACK], USAGE_TABLE)["Properties"]
    assert props["KeySchema"] == [
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}


def test_usage_table_follows_the_feedback_tables_conventions(templates):
    usage = _table(templates[DB_STACK], USAGE_TABLE)
    feedback = _table(templates[DB_STACK], FEEDBACK_TABLE)
    for key in ("BillingMode", "SSESpecification", "PointInTimeRecoverySpecification"):
        assert usage["Properties"].get(key) == feedback["Properties"].get(key), key
    assert usage.get("DeletionPolicy") == feedback.get("DeletionPolicy") == "Delete"


def test_task_def_injects_usage_table_name(templates):
    found = None
    for resource in templates[COMPUTE_STACK].get("Resources", {}).values():
        if resource.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for cdef in resource["Properties"].get("ContainerDefinitions", []):
            if cdef.get("Name") != "adt-app":
                continue
            for entry in cdef.get("Environment", []):
                if entry.get("Name") == "USAGE_TABLE_NAME":
                    found = entry.get("Value")
    assert found == USAGE_TABLE


def test_usage_grant_is_that_table_only(templates):
    docs = dict(_policies(templates[COMPUTE_STACK]))
    doc = docs["DynamoDBUsage"]
    assert len(doc["Statement"]) == 1
    statement = doc["Statement"][0]
    assert sorted(statement["Action"]) == ["dynamodb:PutItem", "dynamodb:Scan"]
    rendered = json.dumps(statement["Resource"])
    assert rendered.count(f"table/{USAGE_TABLE}") == 1
    assert f"table/{USAGE_TABLE}/" not in rendered and '"*"' not in rendered
    others = {name: json.dumps(d) for name, d in docs.items() if name != "DynamoDBUsage"}
    assert not [n for n, text in others.items() if f"table/{USAGE_TABLE}" in text]
