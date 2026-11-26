# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Quick API prod stage writes access logs (not on a workshop synth).

Outside the workshop the stage sends one JSON record per request to a log
group of its own (30-day retention). The account's API Gateway CloudWatch
role it needs is owned by ADTNetworkStack (stacks/_apigw_log_role.py,
tests/test_apigw_account_log_role.py), so this stack carries no setter and no
AWS::ApiGateway::Account. A workshop synth keeps the stage as it was. The stack is built directly against a throwaway VPC, so no app synth is
needed.
"""
from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.assertions import Template

from stacks.quick_apigateway_stack import ACCESS_LOG_FORMAT, QuickApiGatewayStack


def _template(context: dict) -> dict:
    app = cdk.App(context=context)
    env = cdk.Environment(account="111122223333", region="us-east-1")
    net = cdk.Stack(app, "Net", env=env)
    vpc = ec2.Vpc(net, "Vpc", max_azs=2)
    token = secretsmanager.Secret(net, "Token")
    stack = QuickApiGatewayStack(app, "QuickApiGatewayStack", env=env, vpc=vpc,
                                 alb_dns_name="internal-alb.example.com",
                                 alb_arn="arn:aws:elasticloadbalancing:us-east-1:111122223333:"
                                         "loadbalancer/app/adt/0123456789abcdef",
                                 alb_security_group_id="sg-0123456789abcdef0",
                                 gateway_token_secret=token)
    return Template.from_stack(stack).to_json()


def _of_type(template: dict, cfn_type: str) -> dict:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == cfn_type}


@pytest.fixture(scope="module")
def logged() -> dict:
    return _template({})


@pytest.fixture(scope="module")
def workshop() -> dict:
    return _template({"with_workshop": "true"})


def test_stage_writes_json_access_logs_to_its_own_log_group(logged):
    (stage,) = _of_type(logged, "AWS::ApiGateway::Stage").values()
    setting = stage["Properties"]["AccessLogSetting"]
    assert setting["Format"] == ACCESS_LOG_FORMAT
    json.loads(ACCESS_LOG_FORMAT)  # one JSON object per request
    assert "$context.identity.caller" not in ACCESS_LOG_FORMAT
    assert "$context.identity.user" not in ACCESS_LOG_FORMAT
    group_id = setting["DestinationArn"]["Fn::GetAtt"][0]
    group = logged["Resources"][group_id]
    assert group["Type"] == "AWS::Logs::LogGroup"
    assert group["Properties"]["RetentionInDays"] == 30


def test_no_account_resource_and_no_setter_of_its_own(logged):
    assert _of_type(logged, "AWS::ApiGateway::Account") == {}
    assert _of_type(logged, "AWS::CloudFormation::CustomResource") == {}
    assert _of_type(logged, "AWS::Lambda::Function") == {}


def test_workshop_stage_is_unchanged(workshop):
    (stage,) = _of_type(workshop, "AWS::ApiGateway::Stage").values()
    assert "AccessLogSetting" not in stage["Properties"]
    assert _of_type(workshop, "AWS::Logs::LogGroup") == {}
    assert _of_type(workshop, "AWS::CloudFormation::CustomResource") == {}
