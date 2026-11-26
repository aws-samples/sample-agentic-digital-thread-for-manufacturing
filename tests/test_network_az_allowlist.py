# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the deploy-readiness Wave 1 changes (P1a + P2).

* **P1a** — NetworkStack pins the VPC to the AgentCore-supported AZ allowlist
  (``agentcore_supported_azs`` in cdk.json) in a configured region, so no private
  subnet lands in an unsupported AZ (ADR-0010) and the declarative onboarding
  ``CfnRuntime`` (which is pinned to those private subnets) can launch. The
  onboarding runtime references those cross-stack private subnets.
* **P2** (retired 2026-10-29) pinned the onboarding provisioner Lambda's image;
  that Lambda had no caller and was removed. Its absence is pinned in
  tests/test_onboarding_runtime_stack.py.

Synthesized offline (no AWS calls); requires the vendored CDK CLI + Node under
node_modules — skips if unavailable (mirrors tests/test_onboarding_runtime_stack.py
/ tests/test_highbyte_stack.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"

# AgentCore-supported AZ names for us-east-1 (from cdk.json agentcore_supported_azs).
ALLOWLISTED_AZS = {"us-east-1b", "us-east-1c", "us-east-1d"}
UNSUPPORTED_AZ = "us-east-1a"  # use1-az6 — AgentCore does not support it (ADR-0010)


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


def _synth(stack: str, tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    out_dir = tmp_path_factory.mktemp(f"cdk_synth_{stack.lower()}")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            cdk_bin, "synth", stack,
            "-c", "region=us-east-1",
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600,
    )
    template_path = out_dir / f"{stack}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


@pytest.fixture(scope="session")
def network_template(tmp_path_factory) -> dict:
    return _synth("NetworkStack", tmp_path_factory)


@pytest.fixture(scope="session")
def onboarding_template(tmp_path_factory) -> dict:
    return _synth("OnboardingStack", tmp_path_factory)


def _resources(template: dict, cfn_type: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == cfn_type
    }


# --------------------------------------------------------------------------- #
# P1a — NetworkStack VPC pinned to the AgentCore-supported AZ allowlist
# --------------------------------------------------------------------------- #
def _subnet_azs(template: dict, subnet_kind: str) -> list[str]:
    """Literal AvailabilityZone values for subnets of a given cdk subnet-name.

    ``subnet_kind`` is the aws-cdk:subnet-name tag value ("Private"/"Public").
    With an explicit ``availability_zones`` list the AZ is a literal string.
    """
    azs: list[str] = []
    for r in _resources(template, "AWS::EC2::Subnet").values():
        props = r["Properties"]
        tags = {t["Key"]: t["Value"] for t in props.get("Tags", []) if isinstance(t, dict)}
        if tags.get("aws-cdk:subnet-name") == subnet_kind:
            az = props.get("AvailabilityZone")
            if isinstance(az, str):
                azs.append(az)
    return azs


def test_network_private_subnets_only_in_allowlisted_azs(network_template):
    private_azs = _subnet_azs(network_template, "Private")
    assert private_azs, "expected at least one private subnet with a literal AZ"
    assert set(private_azs) <= ALLOWLISTED_AZS, (
        f"private subnets must be pinned to the AgentCore-supported AZs "
        f"{sorted(ALLOWLISTED_AZS)}; got {sorted(set(private_azs))}"
    )
    assert UNSUPPORTED_AZ not in private_azs, (
        f"private subnet in unsupported AZ {UNSUPPORTED_AZ} (use1-az6) — would "
        f"CREATE_FAIL the declarative onboarding CfnRuntime (ADR-0010)"
    )


def test_network_all_subnets_avoid_unsupported_az(network_template):
    # Public subnets host the NAT gateway; keep the whole VPC off the unsupported AZ.
    all_azs = _subnet_azs(network_template, "Private") + _subnet_azs(network_template, "Public")
    assert UNSUPPORTED_AZ not in all_azs, (
        f"no subnet may be in {UNSUPPORTED_AZ}; got {sorted(set(all_azs))}"
    )


# --------------------------------------------------------------------------- #
# P1a — onboarding CfnRuntime references the (now-allowlisted) private subnets
# --------------------------------------------------------------------------- #
def test_onboarding_runtime_references_private_subnets(onboarding_template):
    runtimes = _resources(onboarding_template, "AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 1, f"expected exactly one Runtime, found {len(runtimes)}"
    props = next(iter(runtimes.values()))["Properties"]
    subnets = props["NetworkConfiguration"]["NetworkModeConfig"]["Subnets"]
    assert len(subnets) == 2, f"expected 2 private subnets pinned on the runtime, got {subnets}"
    # Cross-stack refs to NetworkStack's private subnet ids (Fn::ImportValue),
    # i.e. the runtime is pinned to the subnets NetworkStack constrained to
    # supported AZs — not an inline/hardcoded subnet id.
    for s in subnets:
        assert isinstance(s, dict) and "Fn::ImportValue" in s, (
            f"runtime subnet must be a cross-stack import of a NetworkStack "
            f"private subnet, got {s!r}"
        )
