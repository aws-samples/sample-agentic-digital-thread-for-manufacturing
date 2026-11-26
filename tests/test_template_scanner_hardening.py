# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time regression for the template-scanner hardening (2026-09-11).

The Workshop Studio asset scanner (checkov underneath) flagged three of the
engine's child templates. Fixable findings were fixed in the stacks; the rest
carry a documented ``Metadata.checkov.skip`` from ``stacks/_checkov_skips.py``.
This test pins both halves so a refactor cannot silently drop them.

Synthesized offline (no AWS calls); skips if the vendored CDK CLI is missing,
mirroring tests/test_bedrock_marketplace_access.py.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import pytest
import _role_policies

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"


def _synth(tmp_path_factory, stack: str, context: dict[str, str] | None = None) -> dict:
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(f"cdk_synth_{stack}")
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    extra = [arg for key, value in (context or {}).items() for arg in ("-c", f"{key}={value}")]
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [str(CDK_BIN), "synth", stack, "-c", "region=us-east-1", *extra,
         "--output", str(out_dir), "--quiet"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    template_path = out_dir / f"{stack}.template.json"
    if not template_path.exists():
        pytest.skip(f"synth did not produce a template:\n{result.stderr[-2000:]}")
    return json.loads(template_path.read_text())


@pytest.fixture(scope="module")
def compute(tmp_path_factory):
    return _synth(tmp_path_factory, "ADTComputeStack")


@pytest.fixture(scope="module")
def agentcore(tmp_path_factory):
    return _synth(tmp_path_factory, "ADTAgentCoreStack")


@pytest.fixture(scope="module")
def onboarding(tmp_path_factory):
    return _synth(tmp_path_factory, "ADTOnboardingStack")


@pytest.fixture(scope="module")
def erp_database(tmp_path_factory):
    return _synth(tmp_path_factory, "ADTERPDatabaseStack", {"with_simulator": "true"})


@pytest.fixture(scope="module")
def erp_dashboard(tmp_path_factory):
    return _synth(tmp_path_factory, "ADTERPDashboardStack", {"with_simulator": "true"})


def _by_type(template: dict, rtype: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == rtype]


def _skips(res: dict) -> dict[str, str]:
    entries = res.get("Metadata", {}).get("checkov", {}).get("skip", [])
    return {e["id"]: e["comment"] for e in entries}


# ---------------------------------------------------------------------------
# Behavior fixes
# ---------------------------------------------------------------------------
def test_alb_drops_invalid_header_fields(compute):
    (alb,) = _by_type(compute, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    attrs = {a["Key"]: a["Value"] for a in alb["Properties"]["LoadBalancerAttributes"]}
    assert attrs.get("routing.http.drop_invalid_header_fields.enabled") == "true"


def test_buckets_versioned_with_noncurrent_expiry(compute):
    buckets = _by_type(compute, "AWS::S3::Bucket")
    assert len(buckets) == 2
    for b in buckets:
        props = b["Properties"]
        assert props["VersioningConfiguration"]["Status"] == "Enabled"
        rules = props["LifecycleConfiguration"]["Rules"]
        assert any("NoncurrentVersionExpiration" in r for r in rules), (
            "versioning without a noncurrent-version expiry would accumulate "
            "every overwritten stream capture forever"
        )


def test_ecr_scans_on_push(agentcore):
    (repo,) = _by_type(agentcore, "AWS::ECR::Repository")
    assert repo["Properties"]["ImageScanningConfiguration"]["ScanOnPush"] is True


def test_aurora_cluster_has_iam_database_authentication(erp_database):
    """Password logins keep working; IAM database auth is turned on as well."""
    (cluster,) = _by_type(erp_database, "AWS::RDS::DBCluster")
    assert cluster["Properties"].get("EnableIAMDatabaseAuthentication") is True


def test_no_rds6_suppression_now_that_iam_auth_is_on():
    """A leftover cdk-nag RDS6 suppression would say IAM auth is off and hide
    a regression that turned it off again."""
    import aws_cdk as cdk
    from stacks._nag_suppressions import apply_erp_database_suppressions

    stack = cdk.Stack(cdk.App(), "NagProbe")
    apply_erp_database_suppressions(stack)
    rules = stack.template_options.metadata.get("cdk_nag", {}).get("rules_to_suppress", []) \
        if stack.template_options.metadata else []
    assert "AwsSolutions-RDS6" not in {r["id"] for r in rules}


def test_task_role_slr_grant_is_confined_to_agentcore(compute):
    found = []
    for _, doc in _role_policies.named_documents(compute):
        for st in doc["Statement"]:
            actions = st.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "iam:CreateServiceLinkedRole" in actions:
                found.append(st)
    assert found, "task role lost its CreateServiceLinkedRole grant"
    for st in found:
        cond = st.get("Condition", {})
        names = cond.get("StringLike", {}).get("iam:AWSServiceName") or cond.get(
            "StringEquals", {}
        ).get("iam:AWSServiceName")
        assert names, f"unconditioned iam:CreateServiceLinkedRole on *: {st}"
        names = [names] if isinstance(names, str) else names
        assert all("bedrock-agentcore.amazonaws.com" in n for n in names)


# ---------------------------------------------------------------------------
# Documented skips (checkov Metadata) — every skip must carry a reason
# ---------------------------------------------------------------------------
def _assert_documented(res: dict, expected_ids: set[str]) -> None:
    skips = _skips(res)
    assert expected_ids <= set(skips), f"missing skips: {expected_ids - set(skips)}"
    for check_id in expected_ids:
        assert len(skips[check_id]) > 40, f"{check_id} skip has no real justification"


def test_cloudfront_skips_documented(compute):
    (dist,) = _by_type(compute, "AWS::CloudFront::Distribution")
    # Default certificate — the TLS finding is structurally unfixable here.
    assert "ViewerCertificate" not in dist["Properties"]["DistributionConfig"]
    _assert_documented(dist, {"CKV_AWS_174", "CKV_AWS_86"})


def test_alb_access_log_skip_documented(compute):
    (alb,) = _by_type(compute, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    _assert_documented(alb, {"CKV_AWS_91"})


def test_ecr_skips_documented(agentcore):
    (repo,) = _by_type(agentcore, "AWS::ECR::Repository")
    _assert_documented(repo, {"CKV_AWS_51", "CKV_AWS_136"})


def test_dashboard_listener_skips_documented(erp_dashboard):
    """Same reason as the app's listener: an internal ALB behind CloudFront."""
    (alb,) = _by_type(erp_dashboard, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert alb["Properties"]["Scheme"] == "internal"
    (listener,) = _by_type(erp_dashboard, "AWS::ElasticLoadBalancingV2::Listener")
    assert listener["Properties"]["Protocol"] == "HTTP"
    _assert_documented(listener, {"CKV_AWS_2", "CKV_AWS_103"})


def _task_role(compute: dict) -> dict:
    (role,) = [r for lid, r in compute["Resources"].items()
               if r["Type"] == "AWS::IAM::Role" and lid.startswith("AdtTaskRole")]
    return role


def _role(template: dict, prefix: str) -> dict:
    (role,) = [r for lid, r in template["Resources"].items()
               if r["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)]
    return role


def _role_statements(template: dict, prefix: str) -> list[dict]:
    """Every statement attached to the role: inline, AWS::IAM::Policy or managed."""
    (role_id,) = [lid for lid, r in template["Resources"].items()
                  if r["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)]
    return _role_policies.statements(template, role_id)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


#: Writes a runtime or task role used to hold on '*' and now scopes to this
#: account (ADR-0065, amended 2026-10-19).
_ACCOUNT_SCOPED_WRITES = {
    "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
    "bedrock-mantle:CreateInference",
}


def test_task_role_carries_no_scanner_counted_skip(compute):
    """checkov passes CKV_AWS_109 on the task role (its one permissions-
    management grant on '*' is conditioned), and the Workshop Studio scanner
    counts any skip in its rule set as a finding, so the role has none."""
    assert "CKV_AWS_109" not in _skips(_task_role(compute))


@pytest.mark.parametrize("stack_fixture,prefix", [
    ("compute", "AdtTaskRole"),
    ("agentcore", "RuntimeRole"),
    ("onboarding", "OnboardingRuntimeRole"),
])
def test_runtime_and_task_role_writes_are_account_scoped(stack_fixture, prefix, request):
    """The ENI, log and Mantle-inference writes are never granted on '*', and
    the roles carry no CKV_AWS_111 skip (it would be a scanner finding)."""
    template = request.getfixturevalue(stack_fixture)
    role = _role(template, prefix)
    for st in _role_statements(template, prefix):
        if "*" in _as_list(st["Resource"]):
            assert not set(_as_list(st["Action"])) & _ACCOUNT_SCOPED_WRITES, st
    assert "CKV_AWS_111" not in _skips(role)


def _actions_resources(template: dict, prefix: str) -> dict[str, str]:
    """action -> JSON of every resource it is granted on (merged across statements)."""
    out: dict[str, list] = {}
    for st in _role_statements(template, prefix):
        for action in _as_list(st["Action"]):
            out.setdefault(action, []).extend(_as_list(st["Resource"]))
    return {a: json.dumps(r) for a, r in out.items()}


@pytest.mark.parametrize("stack_fixture,prefix", [
    ("agentcore", "RuntimeRole"), ("onboarding", "OnboardingRuntimeRole"),
])
def test_runtime_roles_carry_the_agentcore_execution_role_log_grants(stack_fixture, prefix, request):
    """AgentCore puts a resource policy on each runtime's log group with the
    runtime role (denied 16 times per fresh deploy before this grant); it is
    confined to the runtimes' log groups. Stream writes name the explicit
    log-stream ARN too, so the IAM policy simulator agrees with the runtime."""
    grants = _actions_resources(request.getfixturevalue(stack_fixture), prefix)
    assert "log-group:/aws/bedrock-agentcore/runtimes/*" in grants["logs:PutResourcePolicy"]
    assert '"*"' not in grants["logs:PutResourcePolicy"]
    for action in ("logs:CreateLogStream", "logs:PutLogEvents"):
        assert "log-group:*:log-stream:*" in grants[action], action


def test_runtime_role_reads_only_the_branding_parameters(agentcore):
    """agents/branding.py reads two SSM overrides at import in every runtime
    (about 1,000 denials per parameter in a 5-hour rehearsal without this)."""
    grant = _actions_resources(agentcore, "RuntimeRole")["ssm:GetParameter"]
    assert "parameter/adt/branding-company" in grant
    assert "parameter/adt/branding-tagline" in grant
    assert "parameter/adt/*" not in grant and '"*"' not in grant


def test_mantle_inference_is_scoped_to_the_accounts_projects(compute):
    """CreateInference is authorized against a Mantle project (the default
    project when a request names none); only the bearer-token call has no
    resource type and stays on '*'."""
    statements = [st for st in _role_statements(compute, "AdtTaskRole")
                  if any(a.startswith("bedrock-mantle:") for a in _as_list(st["Action"]))]
    by_action = {a: st["Resource"] for st in statements for a in _as_list(st["Action"])}
    assert by_action["bedrock-mantle:CallWithBearerToken"] == "*"
    scoped = json.dumps(by_action["bedrock-mantle:CreateInference"])
    assert ":bedrock-mantle:*:" in scoped and ":project/*" in scoped and "AWS::AccountId" in scoped


# The onboarding stack has no Lambda since its provisioner was removed
# (2026-10-29); tests/test_onboarding_runtime_stack.py pins that.
@pytest.mark.parametrize("stack_fixture", ["agentcore"])
def test_lambda_env_cmk_skip_on_every_function(stack_fixture, request):
    template = request.getfixturevalue(stack_fixture)
    fns = _by_type(template, "AWS::Lambda::Function")
    assert fns
    for fn in fns:
        _assert_documented(fn, {"CKV_AWS_173"})
