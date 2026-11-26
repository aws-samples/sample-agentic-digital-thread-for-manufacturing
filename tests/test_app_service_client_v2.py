# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app's service client is replaced in ONE stack update (ADR-0071
amendment 2026-11-07).

The original ``adt-app-service`` client's secret was copied into the
attendee-readable ``adt-quick-client-secret``, so on an upgraded deploy that
secret (and its ``AWSPREVIOUS`` version) is disclosed. The release revokes it
for gateway use without touching anything CloudFormation would refuse:

1. No export is modified or removed. ``ADTComputeStack`` and
   ``ADTOnboardingStack`` import the old client's id on deployed stacks, and
   CloudFormation refuses to change or delete an export while it is imported.
   So the old client keeps its logical id and settings, and its export is
   kept explicitly (``export_value``) after the importers move off it.
2. The old client still exists (deleting it is a later cleanup, once nothing
   imports its export).
3. No gateway lists the old client, so a token minted with the disclosed
   secret is refused everywhere.
4. Every consumer uses the new client ``adt-app-service-v2`` (same settings):
   the three gateways, the app task env (through a NEW export), and the
   ``AppServiceClientId`` output that deploy.sh and the workshop bootstrap read
   for the orchestrator runtime env.

Credential-blind synths, the tests/test_quick_stacks.py pattern.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_quick_stacks import (  # sibling test module (rootdir import)
    _resources_of_type,
    _synth_assembly,
    _template,
)

_AGENTCORE = "ADTAgentCoreStack"
_COMPUTE = "ADTComputeStack"
_ONBOARDING = "ADTOnboardingStack"
_OLD_NAME = "adt-app-service"
_NEW_NAME = "adt-app-service-v2"
#: The original client's logical id and its export, both as deployed before
#: this change. Neither may change: deployed stacks import this export.
_OLD_LOGICAL_ID = "AdtAppUserPoolAdtAppServiceClient4EA28BBA"
_OLD_EXPORT = ("ADTAgentCoreStack:ExportsOutputRefAdtAppUserPoolAdtAppServiceClient"
               "4EA28BBA23A96187")
#: The original client's properties as deployed before this change.
_OLD_PROPERTIES = {
    "AllowedOAuthFlows": ["client_credentials"],
    "AllowedOAuthFlowsUserPoolClient": True,
    "AllowedOAuthScopes": [{"Fn::Join": ["", [
        {"Ref": "AdtAppUserPoolAppResourceServer703948BE"}, "/query"]]}],
    "ClientName": _OLD_NAME,
    "GenerateSecret": True,
    "SupportedIdentityProviders": ["COGNITO"],
    "UserPoolId": {"Ref": "AdtAppUserPoolD42C8A50"},
}
#: Every ADTAgentCoreStack export before this change (default synth), each of
#: which another stack imports on deployed stacks, with its value.
_FEAT_V2_EXPORTS = {
    "ADTAgentCoreStack:ExportsOutputFnGetAttAdtAppUserPoolD42C8A50Arn304A63CE":
        {"Fn::GetAtt": ["AdtAppUserPoolD42C8A50", "Arn"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttAdtQueryGatewayGatewayIdentifier0167AAD9":
        {"Fn::GetAtt": ["AdtQueryGateway", "GatewayIdentifier"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttAdtQueryGatewayGatewayUrl93D280D7":
        {"Fn::GetAtt": ["AdtQueryGateway", "GatewayUrl"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttM2MUserPool57D37EF2Arn5EF6C10C":
        {"Fn::GetAtt": ["M2MUserPool57D37EF2", "Arn"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttRuntimeRoleFD8790A4Arn0EA9A900":
        {"Fn::GetAtt": ["RuntimeRoleFD8790A4", "Arn"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttSystemAgentRepo90B4F6C3Arn996C0BA7":
        {"Fn::GetAtt": ["SystemAgentRepo90B4F6C3", "Arn"]},
    "ADTAgentCoreStack:ExportsOutputRefAdtAppUserPoolAdtAppClient168D6307EF1D6ED4":
        {"Ref": "AdtAppUserPoolAdtAppClient168D6307"},
    _OLD_EXPORT: {"Ref": _OLD_LOGICAL_ID},
    "ADTAgentCoreStack:ExportsOutputRefAdtAppUserPoolD42C8A50A3206CB8":
        {"Ref": "AdtAppUserPoolD42C8A50"},
    "ADTAgentCoreStack:ExportsOutputRefAdtEdgeAuthClientAF73B718FFC93DAF":
        {"Ref": "AdtEdgeAuthClientAF73B718"},
    "ADTAgentCoreStack:ExportsOutputRefM2MUserPool57D37EF2E4F4AE6D":
        {"Ref": "M2MUserPool57D37EF2"},
    "ADTAgentCoreStack:ExportsOutputRefM2MUserPoolM2MClient57ACF7E1E8C54F90":
        {"Ref": "M2MUserPoolM2MClient57ACF7E1"},
    "ADTAgentCoreStack:ExportsOutputRefRuntimeRoleFD8790A4BB1F344E":
        {"Ref": "RuntimeRoleFD8790A4"},
}
#: The two more before this change with the Quick connector.
_FEAT_V2_CONNECTOR_EXPORTS = {
    **_FEAT_V2_EXPORTS,
    "ADTAgentCoreStack:ExportsOutputFnGetAttAdtQuickGatewayGatewayIdentifier92B0AD87":
        {"Fn::GetAtt": ["AdtQuickGateway", "GatewayIdentifier"]},
    "ADTAgentCoreStack:ExportsOutputFnGetAttAdtQuickGatewayGatewayUrlEDF515C9":
        {"Fn::GetAtt": ["AdtQuickGateway", "GatewayUrl"]},
}
_OIDC = {
    "quick_api_enabled": "true",
    "quick_oidc_discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "quick_oidc_audience": "https://gateway.example.com/mcp",
}
_ASSEMBLIES = {
    "base": {},
    "connector": {"quick_connector_enabled": "true"},
    "quick-api": {"quick_api_enabled": "true"},
    "quick-oidc": _OIDC,
    "edge-off": {"edge_auth": "false"},
}


@pytest.fixture(scope="module", params=sorted(_ASSEMBLIES))
def assembly(request, tmp_path_factory) -> tuple[str, Path]:
    name = request.param
    return name, _synth_assembly(tmp_path_factory, context=_ASSEMBLIES[name],
                                 output_slug=f"cdk_synth_svc_v2_{name}")


def _exports(template: dict) -> dict:
    return {o["Export"]["Name"]: o["Value"]
            for o in template.get("Outputs", {}).values() if "Export" in o}


def _client_id(template: dict, name: str) -> str:
    (logical_id,) = [lid for lid, res in _resources_of_type(
        template, "AWS::Cognito::UserPoolClient").items()
        if res["Properties"].get("ClientName") == name]
    return logical_id


def _gateways(assembly: Path) -> dict[str, dict]:
    found = {}
    for stack in (_AGENTCORE, _ONBOARDING):
        for res in _resources_of_type(_template(assembly, stack),
                                      "AWS::BedrockAgentCore::Gateway").values():
            found[res["Properties"]["Name"]] = res["Properties"]
    return found


def _allowed(gateway: dict) -> list:
    return gateway["AuthorizerConfiguration"]["CustomJWTAuthorizer"].get("AllowedClients", [])


def _app_task_env(template: dict) -> dict:
    (task_def,) = _resources_of_type(template, "AWS::ECS::TaskDefinition").values()
    (container,) = task_def["Properties"]["ContainerDefinitions"]
    return {e["Name"]: e.get("Value") for e in container.get("Environment", [])}


def _export_of(template: dict, logical_id: str) -> str:
    """The export whose value is a Ref to *logical_id*."""
    (name,) = [n for n, v in _exports(template).items() if v == {"Ref": logical_id}]
    return name


# 1. No export is modified or removed ----------------------------------------
def test_no_feat_v2_export_is_modified_or_removed(assembly):
    name, path = assembly
    if name not in ("base", "connector"):
        pytest.skip("pinned for the default and connector synths")
    pinned = _FEAT_V2_CONNECTOR_EXPORTS if name == "connector" else _FEAT_V2_EXPORTS
    exports = _exports(_template(path, _AGENTCORE))
    missing = sorted(set(pinned) - set(exports))
    changed = sorted(n for n in set(pinned) & set(exports) if exports[n] != pinned[n])
    assert not missing and not changed, {"removed": missing, "modified": changed}


def test_the_old_client_export_is_kept_with_its_value(assembly):
    _name, path = assembly
    assert _exports(_template(path, _AGENTCORE)).get(_OLD_EXPORT) == {"Ref": _OLD_LOGICAL_ID}


# 2. The old client still exists, unchanged ----------------------------------
def test_the_old_client_keeps_its_logical_id_and_settings(assembly):
    _name, path = assembly
    clients = _resources_of_type(_template(path, _AGENTCORE), "AWS::Cognito::UserPoolClient")
    assert clients[_OLD_LOGICAL_ID]["Properties"] == _OLD_PROPERTIES


def test_the_new_client_has_the_old_clients_settings(assembly):
    _name, path = assembly
    template = _template(path, _AGENTCORE)
    clients = _resources_of_type(template, "AWS::Cognito::UserPoolClient")
    new = dict(clients[_client_id(template, _NEW_NAME)]["Properties"])
    assert new.pop("ClientName") == _NEW_NAME
    old = dict(_OLD_PROPERTIES)
    old.pop("ClientName")
    assert new == old


# 3. No gateway lists the old client -----------------------------------------
def test_no_gateway_lists_the_old_client(assembly):
    _name, path = assembly
    gateways = _gateways(path)
    assert {"adt-query-gw", "adt-onboard-gw"} <= set(gateways)
    for gw_name, gateway in gateways.items():
        assert _OLD_LOGICAL_ID not in json.dumps(_allowed(gateway)), gw_name


# 4. Every consumer uses the new client --------------------------------------
def test_every_gateway_that_lists_clients_lists_the_new_one(assembly):
    name, path = assembly
    agentcore = _template(path, _AGENTCORE)
    new = _client_id(agentcore, _NEW_NAME)
    gateways = _gateways(path)
    assert {"Ref": new} in _allowed(gateways["adt-query-gw"])
    assert {"Fn::ImportValue": _export_of(agentcore, new)} in _allowed(gateways["adt-onboard-gw"])
    if name in ("connector", "quick-api"):
        assert {"Ref": new} in _allowed(gateways["adt-quick-gw"])
    if name == "quick-oidc":
        assert _allowed(gateways["adt-quick-gw"]) == []  # delegated mode: no Cognito clients


def test_the_app_task_and_the_output_use_the_new_client(assembly):
    _name, path = assembly
    agentcore = _template(path, _AGENTCORE)
    new = _client_id(agentcore, _NEW_NAME)
    env = _app_task_env(_template(path, _COMPUTE))
    assert env["ADT_APP_SERVICE_CLIENT_ID"] == {"Fn::ImportValue": _export_of(agentcore, new)}
    # deploy.sh and the workshop bootstrap read this for the orchestrator runtime.
    assert agentcore["Outputs"]["AppServiceClientId"]["Value"] == {"Ref": new}


def test_only_the_kept_export_still_names_the_old_client(assembly):
    _name, path = assembly
    agentcore = _template(path, _AGENTCORE)
    for stack in (_COMPUTE, _ONBOARDING):
        assert _OLD_LOGICAL_ID not in json.dumps(_template(path, stack)), stack
    users = [lid for lid, res in agentcore["Resources"].items()
             if lid != _OLD_LOGICAL_ID and _OLD_LOGICAL_ID in json.dumps(res)]
    assert users == []
    outputs = [key for key, out in agentcore["Outputs"].items()
               if _OLD_LOGICAL_ID in json.dumps(out)]
    assert len(outputs) == 1 and agentcore["Outputs"][outputs[0]]["Export"]["Name"] == _OLD_EXPORT


# The app's own sign-in check is unchanged -------------------------------------
# agents/auth/cognito.py::validate_token accepts a token only when its
# client_id equals the one CognitoAuth was built with: COGNITO_CLIENT_ID (the
# login client) or EDGE_AUTH_CLIENT_ID (the edge client). Neither service
# client is, or becomes, one of them.
_SIGN_IN_ENV = {
    "COGNITO_CLIENT_ID":
        "ADTAgentCoreStack:ExportsOutputRefAdtAppUserPoolAdtAppClient168D6307EF1D6ED4",
    "EDGE_AUTH_CLIENT_ID":
        "ADTAgentCoreStack:ExportsOutputRefAdtEdgeAuthClientAF73B718FFC93DAF",
}


def test_the_app_sign_in_check_keeps_its_clients(assembly):
    name, path = assembly
    env = _app_task_env(_template(path, _COMPUTE))
    for key, export in _SIGN_IN_ENV.items():
        if key == "EDGE_AUTH_CLIENT_ID" and name == "edge-off":
            assert key not in env
            continue
        assert env[key] == {"Fn::ImportValue": export}, key
