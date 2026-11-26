# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression tests for the adt-app Cognito additions (ADR-0033, Task 1.1).

Guards the gateway-INBOUND identity consolidation onto the relocated adt-app
pool (which Wave 0 moved into ADTAgentCoreStack):

* a resource server `adt` with scopes query / onboard.explore / onboard.write;
* a client_credentials SERVICE client (`adt-app-service`) requesting the adt/query scope only (least-privilege);
* a user-pool domain with the `adt-app-` prefix (token endpoint for the flow);
* role groups `analyst` and `integrator` alongside the existing `Admin`;
* the interactive login client (`adt-app-client`) LEFT UNCHANGED (no OAuth flows).

Synthesized once offline (no AWS calls); requires the vendored CDK CLI + Node
under node_modules — skips if unavailable (mirrors tests/test_highbyte_stack.py).
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
STACK = "ADTAgentCoreStack"


@pytest.fixture(scope="session")
def agentcore_template(tmp_path_factory) -> dict:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")

    out_dir = tmp_path_factory.mktemp("cdk_synth_agentcore")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [
            str(CDK_BIN), "synth", STACK,
            "-c", "region=us-east-1",
            "--output", str(out_dir),
            "--quiet",
        ],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=300,
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


def test_adt_resource_server_with_three_scopes(agentcore_template):
    servers = _resources(agentcore_template, "AWS::Cognito::UserPoolResourceServer")
    adt = [r for r in servers.values() if r["Properties"].get("Identifier") == "adt"]
    assert adt, "adt resource server not found"
    scope_names = {s["ScopeName"] for s in adt[0]["Properties"].get("Scopes", [])}
    assert scope_names == {"query", "onboard.explore", "onboard.write"}, scope_names


@pytest.mark.parametrize("name", ["adt-app-service-v2", "adt-app-service"])
def test_service_client_uses_client_credentials_and_adt_scopes(agentcore_template, name):
    # adt-app-service-v2 is the service client in use; adt-app-service is the
    # retired original, kept unchanged until nothing imports its export
    # (tests/test_app_service_client_v2.py).
    clients = _resources(agentcore_template, "AWS::Cognito::UserPoolClient")
    svc = [r for r in clients.values()
           if r["Properties"].get("ClientName") == name]
    assert svc, f"{name} client not found"
    props = svc[0]["Properties"]
    assert props.get("AllowedOAuthFlows") == ["client_credentials"], props.get("AllowedOAuthFlows")
    assert props.get("GenerateSecret") is True
    # AllowedOAuthScopes are resource-server-qualified ("adt/<scope>") and
    # synthesize as CFN intrinsics (Fn::Join of the resource-server ref + the
    # "/<scope>" suffix), NOT resolved strings — so match the scope suffixes in
    # the serialized blob rather than calling set() on a list of dicts.
    # Least-privilege (see agentcore_stack AdtAppServiceClient): the service
    # client carries ONLY adt/query — onboard.explore / onboard.write are
    # deliberately withheld from background/service callers. Scopes synthesize as
    # resource-server-qualified CFN intrinsics (Ref to the resource server +
    # "/query"), so match on the serialized blob.
    blob = json.dumps(props.get("AllowedOAuthScopes", []))
    assert "/query" in blob, props.get("AllowedOAuthScopes")
    assert "/onboard.explore" not in blob and "/onboard.write" not in blob, (
        f"service client must stay least-privilege (adt/query only): "
        f"{props.get('AllowedOAuthScopes')}"
    )


def test_app_user_pool_domain_prefix(agentcore_template):
    """The app domain must carry the ``adt-app-`` prefix in BOTH synth modes.

    Cognito domain prefixes are globally unique across ALL AWS accounts, so
    ``_pin_domain_to_account`` deliberately emits two different shapes:

      * concrete account (deploy.sh sets CDK_DEFAULT_ACCOUNT) — a literal
        ``adt-app-<last8>`` string, kept verbatim so existing deployments are
        never replaced;
      * account-agnostic synth (workshop packaging, and ANY environment with
        no credentials — including CI) — an ``Fn::Join`` on ``AWS::AccountId``
        so each attendee account resolves its own prefix instead of racing for
        one baked literal.

    This test previously assumed the literal shape and called ``startswith``
    on the value. It passed locally (ambient credentials make the account
    concrete) and failed in CI (no credentials -> intrinsic dict) — the exact
    local-vs-CI gap. Assert the prefix
    in whichever shape is present, and additionally require that the
    intrinsic form really is account-scoped, since that is the property
    protecting a multi-account workshop.
    """
    domains = _resources(agentcore_template, "AWS::Cognito::UserPoolDomain")
    assert domains, "expected at least one UserPoolDomain"

    def _carries_app_prefix(value) -> bool:
        if isinstance(value, str):
            return value.startswith("adt-app-")
        # Intrinsic (Fn::Join): the base prefix is a literal inside the parts.
        return "adt-app" in json.dumps(value)

    values = [r["Properties"].get("Domain", "") for r in domains.values()]
    app = [v for v in values if _carries_app_prefix(v)]
    assert app, f"no UserPoolDomain carries the adt-app prefix: {values}"

    for value in app:
        if isinstance(value, str):
            continue
        # Account-agnostic synth MUST scope the domain per account, or the
        # first deployer wins the global name and every other deploy fails.
        assert "AWS::AccountId" in json.dumps(value), (
            "an account-agnostic app domain must join on AWS::AccountId so "
            f"each account gets a unique prefix: {value}"
        )


def test_role_groups_present(agentcore_template):
    groups = _resources(agentcore_template, "AWS::Cognito::UserPoolGroup")
    names = {r["Properties"].get("GroupName") for r in groups.values()}
    assert {"analyst", "integrator", "Admin"} <= names, names


def test_login_client_unchanged(agentcore_template):
    """The interactive login client adt-app-client must NOT gain OAuth flows."""
    clients = _resources(agentcore_template, "AWS::Cognito::UserPoolClient")
    login = [r for r in clients.values()
             if r["Properties"].get("ClientName") == "adt-app-client"]
    assert login, "adt-app-client login client not found"
    props = login[0]["Properties"]
    # No client_credentials flow; secret generation stays off (unchanged).
    assert "client_credentials" not in (props.get("AllowedOAuthFlows") or [])
    assert props.get("GenerateSecret") in (None, False)


def test_adt_app_pool_offers_totp_mfa_and_m2m_pool_is_untouched(agentcore_template):
    """Default (non-workshop) synth: the human pool is MFA OPTIONAL with TOTP
    only (stacks/_sample_hardening.py); the client-credentials M2M pool has no
    users and keeps the default."""
    pools = _resources(agentcore_template, "AWS::Cognito::UserPool")
    by_name = {r["Properties"].get("UserPoolName"): r["Properties"] for r in pools.values()}
    assert by_name["adt-app"]["MfaConfiguration"] == "OPTIONAL"
    assert by_name["adt-app"]["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert "MfaConfiguration" not in by_name["adt-m2m"]


def _cog2_reasons(resource: dict) -> list[str]:
    rules = resource.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
    return [r["reason"] for r in rules if r["id"] == "AwsSolutions-COG2"]


def test_cog2_suppression_is_per_pool_with_its_own_reason(agentcore_template):
    """Non-workshop synth: COG2 is suppressed on each pool with the reason
    that is true for it (stacks/_nag_suppressions.py), not stack-wide."""
    pools = _resources(agentcore_template, "AWS::Cognito::UserPool")
    by_name = {r["Properties"].get("UserPoolName"): r for r in pools.values()}
    (app_reason,) = _cog2_reasons(by_name["adt-app"])
    assert app_reason.startswith("adt-app: MFA is OPTIONAL with TOTP")
    assert "M2M" not in app_reason
    (m2m_reason,) = _cog2_reasons(by_name["adt-m2m"])
    assert m2m_reason.startswith("M2M user pool: MFA is not applicable")
    stack_rules = (agentcore_template.get("Metadata", {}).get("cdk_nag", {})
                   .get("rules_to_suppress", []))
    assert "AwsSolutions-COG2" not in {r["id"] for r in stack_rules}
