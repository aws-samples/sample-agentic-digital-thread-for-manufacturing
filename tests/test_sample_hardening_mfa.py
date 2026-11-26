# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cognito MFA on the pools that sign in people: OPTIONAL + TOTP outside the
workshop, unchanged (CloudFormation default, MFA off) on a workshop synth.

The full-app synths cover the real stacks (tests/test_agentcore_app_cognito.py
for adt-app, tests/test_highbyte_stack.py for highbyte-ui-users, the gates'
simulator synth for erp-dashboard-users); these tests pin the helpers.
"""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.assertions import Template
from cdk_nag import NagPackSuppression

from stacks import _nag_suppressions, _sample_hardening

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pool_props(context: dict) -> dict:
    app = cdk.App(context=context)
    stack = cdk.Stack(app, "PoolProbe")
    _sample_hardening.offer_totp_mfa(cognito.UserPool(stack, "Pool"))
    (pool,) = Template.from_stack(stack).find_resources("AWS::Cognito::UserPool").values()
    return pool["Properties"]


def _l2_props() -> dict:
    """What the L2 options would render, for comparison."""
    stack = cdk.Stack(cdk.App(), "L2Probe")
    cognito.UserPool(stack, "Pool", mfa=cognito.Mfa.OPTIONAL,
                     mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True))
    (pool,) = Template.from_stack(stack).find_resources("AWS::Cognito::UserPool").values()
    return pool["Properties"]


@pytest.mark.parametrize("context", [{}, {"with_workshop": "false"}, {"with_workshop": "no"}])
def test_non_workshop_pool_has_optional_totp_mfa(context):
    props = _pool_props(context)
    assert props["MfaConfiguration"] == "OPTIONAL"
    assert props["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert "SmsConfiguration" not in props
    assert props == _l2_props()


@pytest.mark.parametrize("value", ["true", "True", "TRUE"])
def test_workshop_pool_keeps_the_default(value):
    props = _pool_props({"with_workshop": value})
    assert "MfaConfiguration" not in props
    assert "EnabledMfas" not in props


class _Stacks:
    """Stand-ins with the attributes harden() reads."""

    def __init__(self, app: cdk.App, *, with_hub: bool = True):
        core = cdk.Stack(app, "Core")
        self.agentcore = type("Core", (), {"adt_user_pool": cognito.UserPool(core, "AdtAppUserPool")})()
        erp = cdk.Stack(app, "Erp")
        self.erp_dashboard = type("Erp", (), {
            "user_pool": cognito.UserPool(erp, "DashboardUserPool"),
            "alb": elbv2.ApplicationLoadBalancer(erp, "Lb", vpc=ec2.Vpc(erp, "Vpc", max_azs=2)),
        })()
        hub = cdk.Stack(app, "Hub")
        attrs = {}
        if with_hub:
            attrs = {"user_pool": cognito.UserPool(hub, "HighByteUiUserPool"),
                     "highbyte_service": type("Svc", (), {"load_balancer": elbv2.ApplicationLoadBalancer(
                         hub, "Lb", vpc=ec2.Vpc(hub, "Vpc", max_azs=2))})()}
        self.highbyte = type("Hub", (), attrs)()
        self.stacks = (core, erp, hub)


def _rendered(stack: cdk.Stack) -> tuple[list, list]:
    t = Template.from_stack(stack)
    pools = [p["Properties"] for p in t.find_resources("AWS::Cognito::UserPool").values()]
    lbs = [{a["Key"]: a["Value"] for a in lb["Properties"].get("LoadBalancerAttributes", [])}
           for lb in t.find_resources("AWS::ElasticLoadBalancingV2::LoadBalancer").values()]
    return pools, lbs


@pytest.mark.parametrize("workshop", [False, True])
def test_harden_sets_every_human_pool_and_alb(workshop):
    app = cdk.App(context={"with_workshop": "true"} if workshop else {})
    s = _Stacks(app)
    _sample_hardening.harden(agentcore=s.agentcore, erp_dashboard=s.erp_dashboard, highbyte=s.highbyte)
    attr = _sample_hardening.DROP_INVALID_HEADERS_ATTRIBUTE
    for stack in s.stacks:
        pools, lbs = _rendered(stack)
        assert pools
        for props in pools:
            assert ("MfaConfiguration" in props) is not workshop
        for attrs in lbs:
            assert (attrs.get(attr) == "true") is not workshop


def test_harden_without_erp_or_hub():
    app = cdk.App()
    s = _Stacks(app, with_hub=False)
    _sample_hardening.harden(agentcore=s.agentcore, highbyte=s.highbyte)
    _sample_hardening.harden(agentcore=s.agentcore)
    (pool,), _ = _rendered(s.stacks[0])
    assert pool["MfaConfiguration"] == "OPTIONAL"
    erp_pools, _ = _rendered(s.stacks[1])
    assert "MfaConfiguration" not in erp_pools[0]


def test_stack_files_are_not_edited_for_hardening():
    """The hardening lives in app.py + stacks/_sample_hardening.py: the
    HighByte stack file feeds the OPC UA server fingerprint the workshop's SFC
    task carries, and the other stack files stay as the workshop rehearsed."""
    for path in ("stacks/highbyte_opcua_stack.py", "stacks/agentcore_stack.py",
                 "stacks/erp_dashboard_stack.py"):
        assert "_sample_hardening" not in (REPO_ROOT / path).read_text(), path
    assert "_sample_hardening.harden(" in (REPO_ROOT / "app.py").read_text()


def _cog2(stack_context: dict) -> NagPackSuppression:
    app = cdk.App(context=stack_context)
    stack = cdk.Stack(app, "NagProbe")
    original = [NagPackSuppression(id="AwsSolutions-COG1", reason="r1 " * 4),
                NagPackSuppression(id="AwsSolutions-COG2", reason="workshop reason text")]
    out = _nag_suppressions._with_cog2_reason(stack, "pool-x", original)
    assert [s.id for s in out] == ["AwsSolutions-COG1", "AwsSolutions-COG2"]
    assert out[0] is original[0]
    return out[1]


def test_cog2_reason_is_verbatim_on_the_workshop():
    assert _cog2({"with_workshop": "true"}).reason == "workshop reason text"


def test_cog2_reason_describes_optional_mfa_elsewhere():
    reason = _cog2({}).reason
    assert reason.startswith("pool-x: MFA is OPTIONAL with TOTP")
    assert "offer_totp_mfa" in reason
