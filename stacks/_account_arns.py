# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Account-and-Region ARN patterns for grants whose exact resource is named
at run time.

A role whose ENIs or log group are created by a service at run time cannot
name them at synth. Granting the writes on these patterns instead of ``"*"``
keeps them inside this account and Region, which is all ``"*"`` reaches in a
single-account deployment. Actions with no resource type (the describes) must
stay on ``"*"``: IAM evaluates them against ``"*"``, which these patterns do
not match.
"""
from __future__ import annotations

from aws_cdk import ArnFormat, Stack


def account_ec2_arn(stack: Stack) -> str:
    """Every EC2 resource (network interfaces, subnets, security groups) in
    this stack's account and Region."""
    return stack.format_arn(service="ec2", resource="*")


def account_log_group_arn(stack: Stack) -> str:
    """Every CloudWatch Logs log group, and the streams under it, in this
    stack's account and Region."""
    return stack.format_arn(service="logs", resource="log-group", resource_name="*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME)


def account_log_stream_arn(stack: Stack) -> str:
    """Every log stream of every log group in this stack's account and Region.

    ``log-group:*`` already matches a stream ARN at run time, but the IAM
    policy simulator evaluates ``CreateLogStream``/``PutLogEvents`` against the
    explicit ``log-group:<g>:log-stream:<s>`` form; granting both makes the
    simulator agree with what the runtime does."""
    return stack.format_arn(service="logs", resource="log-group",
                            resource_name="*:log-stream:*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME)


def agentcore_runtime_log_group_arn(stack: Stack) -> str:
    """The log groups AgentCore Runtime creates for this account's runtimes
    (``/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>``)."""
    return stack.format_arn(service="logs", resource="log-group",
                            resource_name="/aws/bedrock-agentcore/runtimes/*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME)
