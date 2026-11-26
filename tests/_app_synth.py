# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline synth of the engine app (repo-root ``app.py``) for template tests.

Runs ``app.py`` the way the CDK CLI does (context in ``CDK_CONTEXT_JSON``,
output in ``CDK_OUTDIR``), with the cdk.json context underneath, no asset
bundling and no AWS credentials. Skips without node (aws-cdk-lib needs it).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed internal argv list only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Context every synth here starts from (no cdk-nag, no edge auth, us-east-1).
BASE_CONTEXT = {"cdk_nag_disabled": "true", "edge_auth": "false", "region": "us-east-1",
                "aws:cdk:bundling-stacks": []}


def synth_app(out_dir: Path, context: dict) -> dict[str, dict]:
    """Every ``<stack>.template.json`` app.py writes for ``context``."""
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH (aws-cdk-lib needs it to synthesize)")
    merged = {**json.loads((REPO_ROOT / "cdk.json").read_text()).get("context", {}),
              **BASE_CONTEXT, **context}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "CDK_"))}
    env.update({"CDK_CONTEXT_JSON": json.dumps(merged), "CDK_OUTDIR": str(out_dir),
                "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "AWS_EC2_METADATA_DISABLED": "true", "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [sys.executable, "app.py"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stderr[-3000:]
    return {p.name.removesuffix(".template.json"): json.loads(p.read_text())
            for p in out_dir.glob("*.template.json")}


_NO_VALUE = {"Ref": "AWS::NoValue"}


def resolved(template: dict, parameters: dict[str, str]) -> dict:
    """``template`` as CloudFormation applies it for ``parameters`` (others at
    their defaults): conditional resources and outputs kept or dropped,
    ``Fn::If`` replaced by its branch, ``AWS::NoValue`` removed. Supports the
    condition forms the app's templates use (Equals, Not, And, Or, Condition)."""
    values = {name: spec.get("Default") for name, spec in template.get("Parameters", {}).items()}
    values.update({name: value for name, value in parameters.items() if name in values})
    conditions = template.get("Conditions", {})

    def operand(node):
        return values.get(node["Ref"]) if isinstance(node, dict) and "Ref" in node else node

    def true(expr) -> bool:
        (op, args), = expr.items()
        if op == "Condition":
            return true(conditions[args])
        if op == "Fn::Equals":
            return operand(args[0]) == operand(args[1])
        results = [true(arg) for arg in args]
        return {"Fn::Not": lambda: not results[0], "Fn::And": lambda: all(results),
                "Fn::Or": lambda: any(results)}[op]()

    def walk(node):
        if isinstance(node, list):
            return [item for item in map(walk, node) if item != _NO_VALUE]
        if not isinstance(node, dict):
            return node
        if set(node) == {"Fn::If"}:
            name, yes, no = node["Fn::If"]
            return walk(yes if true(conditions[name]) else no)
        pairs = ((key, walk(value)) for key, value in node.items())
        return {key: value for key, value in pairs if value != _NO_VALUE}

    out = {key: value for key, value in template.items() if key not in ("Resources", "Outputs")}
    for section in ("Resources", "Outputs"):
        out[section] = {lid: walk(body) for lid, body in template.get(section, {}).items()
                        if "Condition" not in body or true(conditions[body["Condition"]])}
    return out
