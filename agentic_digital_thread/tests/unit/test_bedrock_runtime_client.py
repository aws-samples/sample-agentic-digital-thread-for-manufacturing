# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``aws_session.bedrock_runtime_client`` is the ONE bedrock-runtime
construction site (transport only: region, timeouts, retries).

Six modules used to build their own client with inconsistent regions and
timeouts (``prompts.py`` pinned ``us-east-1``). ``audit_core`` is the
documented exception — it is pinned import-pure (no intra-package imports)
and keeps a lazy client inside ``_default_converse``. Offline: boto3.client
is stubbed; nothing is constructed for real.
"""
from __future__ import annotations

import ast
from pathlib import Path

import boto3
import pytest

from agents import aws_session

_AGENTS = Path(__file__).resolve().parents[2] / "agents"
_ALLOWED_RAW_SITES = {"audit_core.py"}


def _raw_bedrock_runtime_sites() -> set[str]:
    sites = set()
    for path in sorted(_AGENTS.rglob("*.py")):
        if path.name == "aws_session.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "client"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "bedrock-runtime":
                sites.add(path.relative_to(_AGENTS).as_posix())
    return sites


def test_only_the_helper_and_audit_core_construct_bedrock_runtime_clients():
    assert _raw_bedrock_runtime_sites() == _ALLOWED_RAW_SITES


@pytest.fixture
def client_spy(monkeypatch):
    calls: list = []

    def _fake_client(service, **kwargs):
        calls.append((service, kwargs))
        return object()

    monkeypatch.setattr(boto3, "client", _fake_client)
    return calls


def test_region_resolution_order(monkeypatch, client_spy):
    monkeypatch.delenv("AWS_REGION", raising=False)
    aws_session.bedrock_runtime_client()
    assert client_spy[-1][1]["region_name"] == "us-east-1"
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    aws_session.bedrock_runtime_client()
    assert client_spy[-1][1]["region_name"] == "eu-central-1"
    aws_session.bedrock_runtime_client("us-west-2")
    assert client_spy[-1][1]["region_name"] == "us-west-2"


def test_shared_config_carries_timeouts_and_standard_retries(client_spy):
    aws_session.bedrock_runtime_client(read_timeout=300)
    cfg = client_spy[-1][1]["config"]
    assert cfg.read_timeout == 300
    assert cfg.connect_timeout == aws_session.BEDROCK_RUNTIME_CONNECT_TIMEOUT_S
    assert cfg.retries == {"max_attempts": aws_session.BEDROCK_RUNTIME_MAX_ATTEMPTS,
                           "mode": "standard"}


def test_prompts_client_follows_aws_region(monkeypatch, client_spy):
    """The old pinned us-east-1 broke every other-region deployment."""
    from agents import prompts
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    prompts._bedrock_client()
    assert client_spy[-1] == ("bedrock-runtime", client_spy[-1][1])
    assert client_spy[-1][1]["region_name"] == "ap-southeast-2"
