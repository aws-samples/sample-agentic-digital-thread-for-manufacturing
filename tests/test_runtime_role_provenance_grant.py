# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth pin for the Wave-5 provenance-sidecar runtime-role DynamoDB grant.

The system-agent runtimes (shared RuntimeRole in ADTAgentCoreStack) write
bounded provenance-query entries through ``agents/job_store.JobStore`` onto
the ``adt-onboarding-sessions`` table (kind="provenance-query" — Wave 5,
frozen contract F2). JobStore uses EXACTLY ``get_item``/``put_item``/
``delete_item``, so the grant must carry exactly those three actions on the
single literal table ARN — no index wildcard, no Scan (least privilege; the
literal name mirrors the adjacent adt-proposed-mappings statement because
ADTOnboardingStack depends on ADTAgentCoreStack, making a construct
reference impossible).

Offline synth via the CDK CLI subprocess with AWS config pointed at
/dev/null (mirrors tests/test_runtime_role_secret_grant.py).

Run:  PYTHONPATH=. python -m pytest tests/test_runtime_role_provenance_grant.py -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
AGENTCORE_STACK = "ADTAgentCoreStack"

SESSIONS_TABLE_SUFFIX = ":table/adt-onboarding-sessions"
EXPECTED_ACTIONS = {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"}


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _cached_lookup_account() -> str | None:
    """Account id with cached context lookups (offline synth — see
    tests/test_runtime_role_secret_grant.py for the rationale)."""
    ctx_path = REPO_ROOT / "cdk.context.json"
    if not ctx_path.exists():
        return None
    for key in json.loads(ctx_path.read_text()):
        m = re.match(r"availability-zones:account=(\d+):region=us-east-1", key)
        if m:
            return m.group(1)
    return None


@pytest.fixture(scope="session")
def agentcore_template(tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    account = _cached_lookup_account()
    if not account:
        pytest.skip("no cached SSM lookup account in cdk.context.json for offline synth")
    out_dir = tmp_path_factory.mktemp("cdk_synth_provenance_grant")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "CDK_DEFAULT_ACCOUNT": account,
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN"):
        env.pop(key, None)
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", AGENTCORE_STACK, "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    template_path = out_dir / f"{AGENTCORE_STACK}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


def _dynamodb_statements(template: dict) -> list[dict]:
    """All IAM statements carrying any dynamodb:* action (both declaration
    shapes: inline role Policies and standalone AWS::IAM::Policy)."""
    statements: list[dict] = []
    for resource in template.get("Resources", {}).values():
        docs: list[dict] = []
        if resource.get("Type") == "AWS::IAM::Role":
            for policy in resource.get("Properties", {}).get("Policies", []) or []:
                docs.append(policy.get("PolicyDocument", {}))
        elif resource.get("Type") == "AWS::IAM::Policy":
            docs.append(resource.get("Properties", {}).get("PolicyDocument", {}))
        for doc in docs:
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if any(a.startswith("dynamodb:") for a in actions):
                    statements.append(stmt)
    return statements


def _flat_resources(stmt: dict) -> list[str]:
    res = stmt.get("Resource", [])
    if isinstance(res, (str, dict)):
        res = [res]
    return [r if isinstance(r, str) else json.dumps(r) for r in res]


def test_runtime_role_grants_sessions_table_get_put_delete_only(agentcore_template):
    """Exactly JobStore's three actions on the single sessions-table ARN —
    no index wildcard, no Scan/Query/UpdateItem on this table."""
    matching = [
        s for s in _dynamodb_statements(agentcore_template)
        if any(r.endswith(SESSIONS_TABLE_SUFFIX) for r in _flat_resources(s))
    ]
    assert matching, (
        "no DynamoDB statement grants the adt-onboarding-sessions table — "
        "Wave-5 system-agent sidecar writes would AccessDeny"
    )
    for stmt in matching:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        assert set(actions) == EXPECTED_ACTIONS, (
            f"sessions-table statement must carry exactly {sorted(EXPECTED_ACTIONS)} "
            f"(JobStore's call set), found {sorted(actions)}"
        )
        for res in _flat_resources(stmt):
            assert "/index/" not in res, "no index wildcard on the sessions table"
