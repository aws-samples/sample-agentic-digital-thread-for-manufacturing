# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the admin Conversations page's IAM.

``GET /api/admin/conversations`` (agents/admin_conversations.py) reads every
user's chat sessions from the app's AgentCore Memory and shows each memory
actor id (a Cognito ``sub``) as its login. The task role must hold exactly
those reads, narrowly:

* ``bedrock-agentcore:GetMemory`` / ``ListSessions`` / ``ListEvents`` on the
  app's own memory only (``memory/adt_memory-*``, the stack-owned name prefix
  the app discovers its memory by), in ONE statement of its own. GetMemory
  confirms the configured memory is the app's before "Actor <sub> not found"
  may mean an empty history. No ``ListActors``: the page takes its users from
  the user pool, never from the memory's actor list;
* ``cognito-idp:ListUsers`` on the app user pool only, beside the existing
  admin-check lookups.

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
COMPUTE_STACK = "ADTComputeStack"
MEMORY_READS = {"bedrock-agentcore:GetMemory", "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:ListEvents"}


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


@pytest.fixture(scope="session")
def compute_template(tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")
    out_dir = tmp_path_factory.mktemp("cdk_synth_conversations")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [cdk_bin, "synth", COMPUTE_STACK, "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    path = out_dir / f"{COMPUTE_STACK}.template.json"
    if not path.exists():
        pytest.skip(f"synth did not produce {COMPUTE_STACK}:\n{result.stderr[-2000:]}")
    return json.loads(path.read_text())


def _statements(template: dict):
    for _, document in named_documents(template):
        for statement in document["Statement"]:
            actions = statement["Action"]
            yield statement, {actions} if isinstance(actions, str) else set(actions)


def test_memory_listing_reads_are_scoped_to_the_apps_memory(compute_template):
    narrow = [actions for _, actions in _statements(compute_template)
              if "bedrock-agentcore:*" not in actions]
    assert not any("bedrock-agentcore:ListActors" in actions for actions in narrow)
    hits = [s for s, actions in _statements(compute_template)
            if actions & MEMORY_READS and "bedrock-agentcore:*" not in actions]
    assert len(hits) == 1, hits
    statement = hits[0]
    assert set(statement["Action"]) == MEMORY_READS
    resource = statement["Resource"]
    assert not isinstance(resource, list) or len(resource) == 1
    assert ":memory/adt_memory-*" in json.dumps(resource)


def test_list_users_is_scoped_to_the_app_user_pool(compute_template):
    hits = [(s, actions) for s, actions in _statements(compute_template)
            if "cognito-idp:ListUsers" in actions]
    assert len(hits) == 1, hits
    statement, actions = hits[0]
    assert actions == {"cognito-idp:AdminListGroupsForUser", "cognito-idp:AdminGetUser",
                       "cognito-idp:ListUsers"}
    resource = statement["Resource"]
    assert resource != "*" and not isinstance(resource, list)
    assert "UserPool" in json.dumps(resource)
