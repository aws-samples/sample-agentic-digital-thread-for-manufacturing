# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADR-0079: the Quick gateway accepts delegated user tokens from an external OIDC IdP.

Synth pins (credential-blind, the test_quick_stacks.py pattern):
  * delegated mode: CUSTOM_JWT against the provider discovery URL,
    ``AllowedAudience`` = the configured gateway URL(s), NO ``AllowedClients``;
    Cognito mode: ``AllowedClients``, NO ``AllowedAudience``;
  * the gateway keeps its logical id, name, type, protocol and MCP pin
    (``2025-03-26``) in BOTH modes — the switch is an in-place update;
  * exactly ONE REQUEST interceptor (headers passed) ONLY in delegated mode;
    the gateway role may invoke exactly that function; the function reads
    exactly the identity-claim key; the ECS task role gains that same read
    ONLY in delegated mode;
  * the Cognito-mode (default ``--quick-api``) templates carry none of it;
  * a pair without ``quick_api_enabled`` fails the synth LOUD;
  * one-home tripwire: ``allowed_audience=`` / ``interceptor_configurations=``
    appear in no stack file but ``stacks/_quick_gateway_auth.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest

from test_quick_stacks import (  # noqa: E402 - sibling test module (rootdir import)
    _app_task_env,
    _resources_of_type,
    _synth_assembly,
    _template,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTCORE = "ADTAgentCoreStack"
_COMPUTE = "ADTComputeStack"
DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"
AUDIENCE = "https://gateway.example.com/mcp"
_KEY_PATTERN = re.compile(r"secret:adt/identity-claim-key-\*$")


@pytest.fixture(scope="module")
def quick_cognito(tmp_path_factory) -> Path:
    return _synth_assembly(tmp_path_factory, context={"quick_api_enabled": "true"},
                           output_slug="cdk_synth_quick_cognito")


@pytest.fixture(scope="module")
def quick_oidc(tmp_path_factory) -> Path:
    return _synth_assembly(tmp_path_factory, context={
        "quick_api_enabled": "true",
        "quick_oidc_discovery_url": DISCOVERY,
        "quick_oidc_audience": AUDIENCE,
    }, output_slug="cdk_synth_quick_oidc")


def _quick_gateway(assembly: Path) -> tuple[str, dict]:
    gateways = _resources_of_type(_template(assembly, _AGENTCORE),
                                  "AWS::BedrockAgentCore::Gateway")
    ((logical_id, resource),) = [
        (lid, res) for lid, res in gateways.items()
        if res["Properties"]["Name"] == "adt-quick-gw"]
    return logical_id, resource["Properties"]


def _flatten(value) -> str:
    return json.dumps(value, sort_keys=True)


def _statements(template: dict, type_name: str):
    for res in _resources_of_type(template, type_name).values():
        props = res["Properties"]
        docs = [props.get("PolicyDocument")] if "PolicyDocument" in props else [
            p["PolicyDocument"] for p in props.get("Policies", [])]
        for doc in docs:
            yield from doc["Statement"]


def _all_statements(template: dict):
    yield from _statements(template, "AWS::IAM::Policy")
    yield from _statements(template, "AWS::IAM::ManagedPolicy")
    yield from _statements(template, "AWS::IAM::Role")


def _as_list(value):
    return value if isinstance(value, list) else [value]


def test_delegated_authorizer_omits_allowed_clients_and_sets_audience(quick_oidc):
    _, props = _quick_gateway(quick_oidc)
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt == {"DiscoveryUrl": DISCOVERY, "AllowedAudience": [AUDIENCE]}


def test_cognito_authorizer_has_no_audience(quick_cognito):
    _, props = _quick_gateway(quick_cognito)
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert set(jwt) == {"DiscoveryUrl", "AllowedClients"}
    # adt-app-client, adt-app-service and Quick's own adt-quick-service.
    assert len(jwt["AllowedClients"]) == 3


def test_gateway_identity_is_stable_across_modes(quick_cognito, quick_oidc):
    cognito_id, cognito = _quick_gateway(quick_cognito)
    oidc_id, oidc = _quick_gateway(quick_oidc)
    assert cognito_id == oidc_id and cognito_id.startswith("AdtQuickGateway")
    for key in ("Name", "AuthorizerType", "ProtocolType", "ProtocolConfiguration", "RoleArn"):
        assert cognito[key] == oidc[key], key
    assert oidc["AuthorizerType"] == "CUSTOM_JWT"


@pytest.mark.parametrize("mode", ["quick_cognito", "quick_oidc"])
def test_quick_gateway_pins_mcp_2025_03_26(mode, request):
    _, props = _quick_gateway(request.getfixturevalue(mode))
    mcp = props["ProtocolConfiguration"]["Mcp"]
    assert mcp == {"SupportedVersions": ["2025-03-26"]}


def test_interceptor_present_only_in_oidc_mode(quick_cognito, quick_oidc):
    _, cognito = _quick_gateway(quick_cognito)
    assert "InterceptorConfigurations" not in cognito
    _, oidc = _quick_gateway(quick_oidc)
    (interceptor,) = oidc["InterceptorConfigurations"]
    assert interceptor["InterceptionPoints"] == ["REQUEST"]
    assert interceptor["InputConfiguration"] == {"PassRequestHeaders": True}
    arn = interceptor["Interceptor"]["Lambda"]["Arn"]
    assert "QuickGatewayRequestInterceptor" in _flatten(arn)


def _interceptor_logical_id(template: dict) -> str:
    fns = [lid for lid in _resources_of_type(template, "AWS::Lambda::Function")
           if lid.startswith("QuickGatewayRequestInterceptor")]
    assert len(fns) == 1, fns
    return fns[0]


def test_interceptor_lambda_is_absent_in_cognito_mode(quick_cognito):
    fns = _resources_of_type(_template(quick_cognito, _AGENTCORE), "AWS::Lambda::Function")
    assert not [lid for lid in fns if lid.startswith("QuickGatewayRequestInterceptor")]
    outputs = _template(quick_cognito, _AGENTCORE).get("Outputs", {})
    assert "QuickGatewayIdentityMode" not in outputs


def test_gateway_role_can_invoke_only_the_interceptor(quick_oidc):
    template = _template(quick_oidc, _AGENTCORE)
    fn_id = _interceptor_logical_id(template)
    invokes = [s for s in _all_statements(template)
               if "lambda:InvokeFunction" in _as_list(s["Action"])]
    assert invokes, "gateway role cannot invoke the interceptor"
    for stmt in invokes:
        resources = _flatten(stmt["Resource"])
        if fn_id in resources:
            assert resources.count("Fn::GetAtt") == 1


def test_interceptor_lambda_reads_only_the_identity_key(quick_oidc):
    template = _template(quick_oidc, _AGENTCORE)
    fn_id = _interceptor_logical_id(template)
    role_ref = _resources_of_type(template, "AWS::Lambda::Function")[fn_id]["Properties"]["Role"]
    role_id = role_ref["Fn::GetAtt"][0]
    policies = [res for res in _resources_of_type(template, "AWS::IAM::Policy").values()
                if {"Ref": role_id} in res["Properties"]["Roles"]]
    statements = [s for p in policies for s in p["Properties"]["PolicyDocument"]["Statement"]]
    assert statements, "interceptor role has no inline policy"
    for stmt in statements:
        assert _as_list(stmt["Action"]) == ["secretsmanager:GetSecretValue"]
        (resource,) = _as_list(stmt["Resource"])
        joined = "".join(p if isinstance(p, str) else "" for p in resource["Fn::Join"][1])
        assert _KEY_PATTERN.search(joined), joined


def _task_role_key_grants(template: dict) -> list:
    return [s for s in _all_statements(template)
            if "identity-claim-key" in _flatten(s.get("Resource"))]


def test_task_role_reads_the_key_only_in_oidc_mode(quick_cognito, quick_oidc):
    assert _task_role_key_grants(_template(quick_cognito, _COMPUTE)) == []
    grants = _task_role_key_grants(_template(quick_oidc, _COMPUTE))
    assert len(grants) == 1
    assert _as_list(grants[0]["Action"]) == ["secretsmanager:GetSecretValue"]


#: Spelled literally: the app reads the key only when its task carries this.
_KEY_READABLE_ENV = "IDENTITY_CLAIM_KEY_READABLE"


def test_task_env_flags_the_key_grant_exactly_when_it_is_granted(quick_cognito, quick_oidc):
    """The app's by-name key readers (delegated-identity verifier, onboarding
    agent test) read only when this flag is set. It rides with the grant, so
    a deploy without the grant (Cognito mode, base, the workshop) makes no
    read that could only be AccessDenied, and the delegated mode still reads."""
    cognito_env = _app_task_env(_template(quick_cognito, _COMPUTE))
    oidc_env = _app_task_env(_template(quick_oidc, _COMPUTE))
    assert _KEY_READABLE_ENV not in cognito_env
    assert oidc_env.get(_KEY_READABLE_ENV) == "1"
    for assembly, env in ((quick_cognito, cognito_env), (quick_oidc, oidc_env)):
        granted = bool(_task_role_key_grants(_template(assembly, _COMPUTE)))
        assert (_KEY_READABLE_ENV in env) is granted


def test_oidc_mode_outputs_its_identity_mode(quick_oidc):
    outputs = _template(quick_oidc, _AGENTCORE)["Outputs"]
    assert outputs["QuickGatewayIdentityMode"]["Value"] == "external-oidc"
    assert "QuickGatewayUrl" in outputs


def test_interceptor_asset_is_staged_with_the_one_claim_module(quick_oidc):
    manifest = json.loads((quick_oidc / f"{_AGENTCORE}.assets.json").read_text())
    dirs = [quick_oidc / f["source"]["path"] for f in manifest["files"].values()
            if f["source"].get("packaging") == "zip"]
    asset = next((d for d in dirs if (d / "index.py").is_file()
                  and "quick_interceptor" in (d / "index.py").read_text()), None)
    assert asset is not None, dirs
    claim_copy = (asset / "identity_claim.py").read_text()
    source = (REPO_ROOT / "agentic_digital_thread/agents/auth/identity_claim.py").read_text()
    assert claim_copy == source  # a COPY of the ONE claim implementation


def test_pair_without_quick_api_fails_the_synth_loud(tmp_path):
    env = dict(os.environ)
    for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "CDK_DEFAULT_ACCOUNT"):
        env.pop(key, None)
    env.update({
        "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "CDK_CONTEXT_JSON": json.dumps({
            "region": "us-east-1", "quick_oidc_discovery_url": DISCOVERY,
            "quick_oidc_audience": AUDIENCE}),
        "CDK_OUTDIR": str(tmp_path / "cdk.out"),
    })
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [sys.executable, "app.py"], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=600)
    assert result.returncode != 0
    assert "--quick-api" in result.stderr
    assert "/adt/quick-oidc-discovery-url" in result.stderr


def test_quick_gateway_auth_one_home():
    offenders = []
    for path in sorted((REPO_ROOT / "stacks").glob("*.py")):
        if path.name == "_quick_gateway_auth.py":
            continue
        text = path.read_text()
        for needle in ("allowed_audience=", "interceptor_configurations="):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []
