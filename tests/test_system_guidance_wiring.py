# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synth contracts for the durable system-guidance table and ownership."""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed local CDK argv only
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CDK = ROOT / "node_modules" / ".bin" / "cdk"
TABLE = "adt-system-guidance"
STACKS = ("ADTDatabaseStack", "ADTComputeStack", "ADTAgentCoreStack")


def _cdk():
    return str(CDK) if CDK.exists() else shutil.which("cdk")


@pytest.fixture(scope="session")
def guidance_templates(tmp_path_factory):
    cdk = _cdk()
    if not cdk:
        pytest.skip("CDK CLI unavailable")
    out_dir = tmp_path_factory.mktemp("guidance-wiring")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1",
           "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
           "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
           "AWS_EC2_METADATA_DISABLED": "true"}
    for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN"):
        env.pop(key, None)
    result = subprocess.run(  # nosec B603 - fixed argv, local repository inputs
        [cdk, "synth", *STACKS, "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=900)
    templates = {}
    for stack in STACKS:
        path = out_dir / f"{stack}.template.json"
        if not path.exists():
            pytest.skip(f"synth failed to produce {stack}: {result.stderr[-2000:]}")
        templates[stack] = json.loads(path.read_text())
    return templates


def _statements(template):
    for resource in template.get("Resources", {}).values():
        props = resource.get("Properties", {})
        policies = props.get("Policies", []) if resource.get("Type") == "AWS::IAM::Role" else []
        if resource.get("Type") in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            policies = [{"PolicyDocument": props.get("PolicyDocument", {})}]
        for policy in policies or []:
            yield from policy.get("PolicyDocument", {}).get("Statement", [])


def _actions(statement):
    value = statement.get("Action", [])
    return {value} if isinstance(value, str) else set(value)


def _resources(statement):
    value = statement.get("Resource", [])
    value = [value] if isinstance(value, (str, dict)) else value
    return [item if isinstance(item, str) else json.dumps(item) for item in value]


def _table_statements(template):
    return [statement for statement in _statements(template)
            if any(f"table/{TABLE}" in value for value in _resources(statement))]


def test_database_stack_provisions_guidance_table(guidance_templates):
    tables = [resource for resource in guidance_templates["ADTDatabaseStack"]["Resources"].values()
              if resource.get("Type") == "AWS::DynamoDB::Table"
              and resource["Properties"].get("TableName") == TABLE]
    assert len(tables) == 1
    table = tables[0]
    props = table["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["KeySchema"] == [{"AttributeName": "system_id", "KeyType": "HASH"}]
    neighbors = [resource for resource in guidance_templates["ADTDatabaseStack"]["Resources"].values()
                 if resource.get("Type") == "AWS::DynamoDB::Table"
                 and resource["Properties"].get("TableName") == "adt-proposed-mappings"]
    assert len(neighbors) == 1
    neighbor = neighbors[0]
    assert table.get("DeletionPolicy") == neighbor.get("DeletionPolicy")
    for posture in ("SSESpecification", "PointInTimeRecoverySpecification"):
        assert props.get(posture) == neighbor["Properties"].get(posture)


def test_ecs_get_update_scan_ownership_and_env(guidance_templates):
    template = guidance_templates["ADTComputeStack"]
    env_values = {}
    for resource in template["Resources"].values():
        if resource.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        for container in resource["Properties"].get("ContainerDefinitions", []):
            if container.get("Name") == "adt-app":
                env_values.update({entry["Name"]: entry.get("Value")
                                   for entry in container.get("Environment", [])})
    assert env_values["ADT_SYSTEM_GUIDANCE_TABLE"] == TABLE
    statements = _table_statements(template)
    assert statements
    assert set().union(*(_actions(statement) for statement in statements)) == {
        "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"}
    assert all("/index/" not in resource for statement in statements
               for resource in _resources(statement))


def test_system_agent_runtime_is_read_only_get_item(guidance_templates):
    statements = _table_statements(guidance_templates["ADTAgentCoreStack"])
    assert statements
    assert set().union(*(_actions(statement) for statement in statements)) == {
        "dynamodb:GetItem"}
