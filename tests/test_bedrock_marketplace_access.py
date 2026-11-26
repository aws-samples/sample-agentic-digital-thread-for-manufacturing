# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression: every role that invokes a Bedrock model must also be
allowed to let Bedrock auto-subscribe the model via AWS Marketplace.

Found live in Workshop Studio the 2026-09-10 test event: in a freshly
vended account the orchestrator's first Anthropic invoke failed with
AccessDeniedException because the runtime role had bedrock:InvokeModel* but not
aws-marketplace:{ViewSubscriptions,Subscribe}. See stacks/_bedrock_access.py.

Synthesized offline (no AWS calls); skips if the vendored CDK CLI is missing,
mirroring tests/test_agentcore_app_cognito.py.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"

MARKETPLACE_ACTIONS = {"aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"}


def _synth(tmp_path_factory, stack: str) -> dict:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(f"cdk_synth_{stack}")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", stack, "-c", "region=us-east-1",
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    template_path = out_dir / f"{stack}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


def _statements(template: dict):
    """Yield (owner_logical_id, statement) for every IAM statement in the template."""
    for logical_id, res in template["Resources"].items():
        props = res.get("Properties", {})
        docs = []
        if res["Type"] == "AWS::IAM::Role":
            docs += [p["PolicyDocument"] for p in props.get("Policies", [])]
        elif res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            docs.append(props["PolicyDocument"])
        for doc in docs:
            for st in doc.get("Statement", []):
                yield logical_id, st


def _actions(st) -> set[str]:
    a = st.get("Action", [])
    return set(a if isinstance(a, list) else [a])


def _assert_invokers_can_auto_subscribe(template: dict, stack: str) -> None:
    invoking_owners = {
        owner for owner, st in _statements(template)
        if st.get("Effect") == "Allow" and "bedrock:InvokeModel" in _actions(st)
    }
    assert invoking_owners, f"{stack}: expected at least one role with bedrock:InvokeModel"

    subscribing_owners = set()
    for owner, st in _statements(template):
        if st.get("Effect") == "Allow" and MARKETPLACE_ACTIONS <= _actions(st):
            cond = st.get("Condition", {}).get("StringEquals", {})
            assert cond.get("aws:CalledViaLast") == "bedrock.amazonaws.com", (
                f"{stack}/{owner}: Marketplace subscribe statement must be "
                "conditioned on aws:CalledViaLast=bedrock.amazonaws.com"
            )
            subscribing_owners.add(owner)

    # Policies may be inline on the role or attached as AWS::IAM::Policy or
    # AWS::IAM::ManagedPolicy; the
    # owner id of an attached policy differs from the role id, so compare on
    # the role side by requiring the statement to exist wherever InvokeModel does.
    missing = invoking_owners - subscribing_owners
    assert not missing, (
        f"{stack}: roles/policies grant bedrock:InvokeModel without the Marketplace "
        f"auto-subscribe statement: {sorted(missing)}"
    )


@pytest.fixture(scope="session")
def agentcore_template(tmp_path_factory) -> dict:
    return _synth(tmp_path_factory, "ADTAgentCoreStack")


@pytest.fixture(scope="session")
def compute_template(tmp_path_factory) -> dict:
    return _synth(tmp_path_factory, "ADTComputeStack")


def test_agentcore_runtime_role_can_auto_subscribe(agentcore_template):
    _assert_invokers_can_auto_subscribe(agentcore_template, "ADTAgentCoreStack")


def test_compute_task_role_can_auto_subscribe(compute_template):
    _assert_invokers_can_auto_subscribe(compute_template, "ADTComputeStack")
