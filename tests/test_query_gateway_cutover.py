# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the P5 query-gateway identity cutover (ADR-0033).

Guards the AWS_IAM -> CUSTOM_JWT flip on the QUERY gateway (ADTAgentCoreStack.gateway).
The flip is done by REPLACING the gateway (authorizer type cannot be updated in
place) — the gateway is named ``adt-query-gw``:

* the query gateway (``adt-query-gw``) is ``CUSTOM_JWT`` (NOT ``AWS_IAM``);
* its ``CustomJWTAuthorizer`` uses the adt-app discovery URL and allows BOTH the
  login client (adt-app-client) and the service client (adt-app-service);
* semantic search is KEPT (``SearchType == "SEMANTIC"``);
* the Cedar layer (PolicyEngine + Policy + enforce) is DEFERRED — it
  cannot be attached declaratively before the gateway targets are registered, so
  NO PolicyEngine/Policy is present here and the gateway has no
  PolicyEngineConfiguration.

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node
under node_modules — skips if unavailable (mirrors test_agentcore_app_cognito.py).
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
STACK = "ADTAgentCoreStack"
QUERY_GW_NAME = "adt-query-gw"


def _resolve_cdk() -> str | None:
    if CDK_BIN.exists():
        return str(CDK_BIN)
    return shutil.which("cdk")


@pytest.fixture(scope="session")
def agentcore_template(tmp_path_factory) -> dict:
    cdk_bin = _resolve_cdk()
    if not cdk_bin:
        pytest.skip("CDK CLI not found (run `npm install`, or install cdk on PATH)")

    out_dir = tmp_path_factory.mktemp("cdk_synth_query_gw")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            cdk_bin, "synth", STACK,
            "-c", "region=us-east-1",
            # Pin the OFF path: this suite is the edge-auth-off baseline for
            # the query gateway (exactly TWO AllowedClients). Without the pin,
            # default resolution (ON except --workshop, plus ambient sticky
            # SSM) would add the edge client (ADR-0070 amendment 2026-09-23);
            # the edge-on 3-client shape is pinned in
            # tests/test_edge_external_oidc.py.
            "-c", "edge_auth=false",
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=420,
    )
    template_path = out_dir / f"{STACK}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


def _resources(template: dict, cfn_type: str) -> dict:
    return {
        lid: r for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == cfn_type
    }


def _query_gateway(template: dict) -> dict:
    gws = _resources(template, "AWS::BedrockAgentCore::Gateway")
    match = [g for g in gws.values() if g["Properties"].get("Name") == QUERY_GW_NAME]
    assert match, f"query gateway {QUERY_GW_NAME!r} not found among {list(gws)}"
    return match[0]


# --------------------------------------------------------------------------- #
# Authorizer: AWS_IAM -> CUSTOM_JWT (adt-app, both clients)
# --------------------------------------------------------------------------- #
def test_query_gateway_is_custom_jwt_not_iam(agentcore_template):
    props = _query_gateway(agentcore_template)["Properties"]
    assert props.get("AuthorizerType") == "CUSTOM_JWT", props.get("AuthorizerType")


def test_query_gateway_jwt_discovery_and_both_clients(agentcore_template):
    props = _query_gateway(agentcore_template)["Properties"]
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    blob = json.dumps(jwt.get("DiscoveryUrl"))
    assert ".well-known/openid-configuration" in blob, jwt.get("DiscoveryUrl")
    allowed = jwt.get("AllowedClients", [])
    # Both the login client and the service client (two refs into the adt-app pool).
    assert len(allowed) == 2, f"expected both adt-app client ids in AllowedClients: {allowed}"


def test_query_gateway_keeps_semantic_search(agentcore_template):
    props = _query_gateway(agentcore_template)["Properties"]
    mcp = props.get("ProtocolConfiguration", {}).get("Mcp", {})
    assert mcp.get("SearchType") == "SEMANTIC", (
        "query gateway must KEEP semantic search (P5 only flips the authorizer)"
    )


# --------------------------------------------------------------------------- #
# Cedar: DEFERRED (no PolicyEngine/Policy; no gateway attachment)
# --------------------------------------------------------------------------- #
def test_query_cedar_deferred_no_engine_or_policy(agentcore_template):
    engines = _resources(agentcore_template, "AWS::BedrockAgentCore::PolicyEngine")
    policies = _resources(agentcore_template, "AWS::BedrockAgentCore::Policy")
    assert len(engines) == 0, f"Cedar deferred: expected no PolicyEngine, found {len(engines)}"
    assert len(policies) == 0, f"Cedar deferred: expected no Policy, found {len(policies)}"
    # The gateway must NOT attach a policy engine (Cedar layer deferred).
    gprops = _query_gateway(agentcore_template)["Properties"]
    assert "PolicyEngineConfiguration" not in gprops, (
        f"Cedar deferred: gateway must not attach a policy engine: "
        f"{gprops.get('PolicyEngineConfiguration')}"
    )
