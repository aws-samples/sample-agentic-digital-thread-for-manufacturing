# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ALBs drop requests with invalid header fields outside the workshop.

The ADT app's own ALB sets the attribute on every profile; app.py applies it
to the ERP dashboard and HighByte ALBs through stacks/_sample_hardening.py
(tests/test_sample_hardening_mfa.py covers that wiring), which leaves a
workshop synth's load balancers unchanged. tests/test_highbyte_stack.py pins
the HighByte ALB in a real synth.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.assertions import Template

from stacks import _sample_hardening

ATTR = "routing.http.drop_invalid_header_fields.enabled"


def _attrs(context: dict) -> dict:
    app = cdk.App(context=context)
    stack = cdk.Stack(app, "AlbProbe")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2)
    alb = elbv2.ApplicationLoadBalancer(stack, "Alb", vpc=vpc, internet_facing=False)
    _sample_hardening.drop_invalid_header_fields(alb)
    (lb,) = Template.from_stack(stack).find_resources(
        "AWS::ElasticLoadBalancingV2::LoadBalancer").values()
    return {a["Key"]: a["Value"] for a in lb["Properties"].get("LoadBalancerAttributes", [])}


def test_non_workshop_alb_drops_invalid_headers():
    assert _sample_hardening.DROP_INVALID_HEADERS_ATTRIBUTE == ATTR
    assert _attrs({})[ATTR] == "true"


def test_workshop_alb_is_unchanged():
    assert ATTR not in _attrs({"with_workshop": "true"})
