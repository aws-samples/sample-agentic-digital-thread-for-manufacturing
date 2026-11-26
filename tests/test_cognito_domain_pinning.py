# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cognito hosted-UI domains must resolve uniquely per deploying account."""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"


def _domain_resolves_from_account(domain_value) -> bool:
    """True when the intrinsic ``Domain`` references ``AWS::AccountId``."""
    text = json.dumps(domain_value)
    return isinstance(domain_value, dict) and "AWS::AccountId" in text


def _user_pool_domains(template: dict) -> dict:
    return {
        key: value
        for key, value in template.get("Resources", {}).items()
        if value.get("Type") == "AWS::Cognito::UserPoolDomain"
    }


def _synth_template(
    tmp_path_factory,
    *,
    stack: str,
    context: dict[str, str],
    output_slug: str,
) -> dict:
    """Credential-blind synth (no AWS credentials, no lookups)."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(output_slug)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
    }
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    # Credential-blind for real: a developer's [default] profile or ambient
    # keys let the CDK CLI re-resolve a concrete account, which flips the
    # stacks onto the deploy-time literal-prefix branch and fails the intrinsic
    # assertion.
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(key, None)
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    args = [str(CDK_BIN), "synth", stack]
    for key, value in context.items():
        args.extend(["-c", f"{key}={value}"])
    args.extend(["--output", str(out_dir), "--quiet"])
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    template_path = out_dir / f"{stack}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


@pytest.fixture(scope="session")
def erp_dashboard_template(tmp_path_factory) -> dict:
    return _synth_template(
        tmp_path_factory,
        stack="ADTERPDashboardStack",
        context={"region": "us-east-1", "with_simulator": "true"},
        output_slug="cdk_synth_erp_dashboard",
    )


def test_erp_dashboard_domain_is_pinned_to_the_deploying_account(
    erp_dashboard_template,
):
    domains = _user_pool_domains(erp_dashboard_template)
    assert domains, "ERP dashboard must define its Cognito hosted-UI domain"
    for logical, resource in domains.items():
        value = resource["Properties"]["Domain"]
        assert _domain_resolves_from_account(value), (logical, value)
        assert "erp-dashboard" in json.dumps(value)
    output = erp_dashboard_template["Outputs"]["CognitoUserPoolDomain"]["Value"]
    assert _domain_resolves_from_account(output), output


@pytest.fixture(scope="session")
def adt_compute_edge_auth_template(tmp_path_factory) -> dict:
    """Credential-blind ADT compute synth with edge auth enabled."""
    return _synth_template(
        tmp_path_factory,
        stack="ADTComputeStack",
        context={"region": "us-east-1", "edge_auth": "true"},
        output_slug="cdk_synth_adt_compute",
    )


def test_adt_app_edge_auth_reuses_the_upstream_domain(
    adt_compute_edge_auth_template,
):
    """Edge auth must not grow a second pool domain — or ANY Cognito client —
    in ADTComputeStack: the adt-app pool, its domain AND the edge OAuth client
    all live upstream in ADTAgentCoreStack (client relocated 2026-09-23 so the
    gateway CUSTOM_JWT authorizers can allow it; ADR-0070 amendment)."""
    assert _user_pool_domains(adt_compute_edge_auth_template) == {}, (
        "ADTComputeStack must not create a UserPoolDomain — the adt-app pool's "
        "domain is owned by ADTAgentCoreStack and a pool allows exactly one"
    )
    clients = {
        logical: resource
        for logical, resource in adt_compute_edge_auth_template["Resources"].items()
        if resource["Type"] == "AWS::Cognito::UserPoolClient"
    }
    assert clients == {}, (
        "ADTComputeStack must not own any Cognito UserPoolClient — the edge "
        "client (AdtEdgeAuthClient) lives in ADTAgentCoreStack so the query/"
        f"onboarding gateways can allow it: {sorted(clients)}"
    )


