# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time tests for the ``sfc_enabled`` flag in ``app.py`` (ADR-0082 D7).

``sfc_enabled`` deploys the SFC stacks and IMPLIES ``opcua_enabled``, because
the OPC UA servers are SFC's only source: SFC never deploys with nothing to
read. HighByte stays independent of it. Synthesized offline with the vendored
CDK CLI, like ``tests/test_highbyte_stack.py``; skips if it is unavailable.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
OPCUA_STACK = "ADTHighByteOpcuaStack"
SFC_STACKS = {"ADTSfcS3TablesStack", "ADTSfcStack"}


def _synth(tmp_path_factory, *context: str) -> Path:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp("cdk_synth")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    # Credential-blind, as the CI lanes run it (the recipe of
    # test_cognito_domain_pinning.py, plus the instance metadata service): with
    # ANY credential the CLI resolves a concrete account, the stacks become
    # environment-aware and the VPC asks EC2 for the availability zones. On a
    # host whose instance role cannot do that, synth fails with
    # "not authorized to perform: ec2:DescribeAvailabilityZones".
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(key, None)
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    args = [str(CDK_BIN), "synth", "--all", "-c", "region=us-east-1"]
    for flag in context:
        args += ["-c", flag]
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        args + ["--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, f"synth {context} failed:\n{result.stderr[-2000:]}"
    return out_dir


def _stack_ids(out_dir: Path) -> set[str]:
    manifest = json.loads((out_dir / "manifest.json").read_text())
    return {
        k for k, a in manifest["artifacts"].items()
        if a.get("type") == "aws:cloudformation:stack"
    }


@pytest.fixture(scope="session")
def sfc_only(tmp_path_factory) -> Path:
    return _synth(tmp_path_factory, "sfc_enabled=true")


def test_sfc_enabled_brings_the_opcua_servers_without_highbyte(sfc_only):
    ids = _stack_ids(sfc_only)
    assert SFC_STACKS | {OPCUA_STACK} <= ids
    template = json.loads((sfc_only / f"{OPCUA_STACK}.template.json").read_text())
    types = {r["Type"] for r in template["Resources"].values()}
    assert "AWS::CloudFront::Distribution" not in types, "HighByte must stay off"


def test_sfc_ships_its_freshness_watchdog_reading_every_opcua_server(sfc_only):
    """app.py wires the watchdog into the SFC stack with every OPC UA server
    (stacks/_sfc_watchdog.py), the servers referenced from their own stack."""
    template = json.loads((sfc_only / "ADTSfcStack.template.json").read_text())
    rules = [r for r in template["Resources"].values() if r["Type"] == "AWS::Events::Rule"]
    assert [r["Properties"]["ScheduleExpression"] for r in rules] == ["rate(3 minutes)"]
    (fn,) = [r for lid, r in template["Resources"].items()
             if r["Type"] == "AWS::Lambda::Function" and lid.startswith("FreshnessWatchdog")]
    config = json.dumps(fn["Properties"]["Environment"]["Variables"]["WATCHDOG_CONFIG"])
    servers = json.loads((sfc_only / f"{OPCUA_STACK}.template.json").read_text())
    names = [r["Properties"]["ServiceName"] for r in servers["Resources"].values()
             if r["Type"] == "AWS::ECS::Service"]
    imports = [i for i in config.split('"Fn::ImportValue": ')[1:]]
    assert len(names) == 4
    assert sum(1 for i in imports if i.startswith(f'"{OPCUA_STACK}:')) >= len(names) + 1


def test_sfc_beside_a_hub_that_writes_sitewise_ships_no_watchdog(tmp_path_factory):
    """With the hub's NameSpace_to_SiteWise pipeline on (a raw synth, or
    deploy.sh --highbyte-sitewise-pipeline enabled) there are two writers on
    the same properties, so the watchdog would be blind and is not built
    (stacks/_sfc_watchdog.py); with the pipeline off, deploy.sh's default for
    --highbyte --sfc, it is."""
    both = _synth(tmp_path_factory, "sfc_enabled=true", "highbyte_enabled=true", "highbyte_accept_eula=true")
    template = json.loads((both / "ADTSfcStack.template.json").read_text())
    assert not [lid for lid in template["Resources"] if lid.startswith("FreshnessWatchdog")]
    one = _synth(tmp_path_factory, "sfc_enabled=true", "highbyte_enabled=true", "highbyte_accept_eula=true",
                 "highbyte_sitewise_pipeline=disabled")
    template = json.loads((one / "ADTSfcStack.template.json").read_text())
    assert [lid for lid in template["Resources"] if lid.startswith("FreshnessWatchdogFunction")]


def test_sfc_is_absent_unless_enabled(tmp_path_factory):
    for context in ((), ("highbyte_enabled=true", "highbyte_accept_eula=true"), ("opcua_enabled=true",)):
        ids = _stack_ids(_synth(tmp_path_factory, *context))
        assert not (SFC_STACKS & ids), context
