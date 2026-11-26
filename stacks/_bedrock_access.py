# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock model-access helpers shared by every role that invokes a model.

Why this exists (found live, Workshop Studio the 2026-09-10 test event):
the FIRST invocation of a third-party (Anthropic) foundation model in an AWS
account makes Bedrock open an AWS Marketplace subscription on the caller's
behalf. If the calling role lacks the Marketplace actions, that automatic
subscription fails and the invoke -- and every one after it -- returns
``AccessDeniedException``. The orchestrator runtime hit exactly this in a
freshly vended account: MCP gateway handshake fine, tools fine, then
``strands/models/bedrock.py stream -> AccessDeniedException``.

deploy.sh deployments never saw it because their accounts had already
subscribed to Claude once (by a human in the console). Every Workshop Studio
team account is brand new, so the auto-subscribe path is the only path.

Reference: https://repost.aws/knowledge-center/bedrock-resolve-marketplace-permission
Bedrock says Amazon/Nova, Mistral, Meta, DeepSeek, Qwen and OpenAI models do not
need this; Anthropic models do.
"""

from __future__ import annotations

from aws_cdk import Stack, aws_iam as iam


def bedrock_marketplace_auto_subscribe_statement() -> iam.PolicyStatement:
    """Allow Bedrock to auto-subscribe a Marketplace-listed model on first invoke.

    Resource must be ``*`` (Marketplace subscription actions are not resource
    scoped). The blast radius is bounded by ``aws:CalledViaLast``: the actions
    are only usable when Bedrock itself is the caller, i.e. from inside a model
    invocation, never by the role directly.
    """
    return iam.PolicyStatement(
        sid="BedrockMarketplaceAutoSubscribe",
        actions=[
            "aws-marketplace:ViewSubscriptions",
            "aws-marketplace:Subscribe",
        ],
        resources=["*"],
        conditions={
            "StringEquals": {"aws:CalledViaLast": "bedrock.amazonaws.com"},
        },
    )


def mantle_inference_statements(stack: Stack) -> list[iam.PolicyStatement]:
    """Bedrock MANTLE inference (ADR-0055), the OpenAI-compatible surface
    that serves the GPT-5.x family. Distinct service prefix from ``bedrock:``.

    ``CreateInference`` is authorized against a Mantle project; a request
    that names no project uses the account's ``default`` project, so the
    grant covers this account's projects in any Region (the shape of the AWS
    managed policy AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy).
    ``CallWithBearerToken`` covers the short-lived token the model provider
    mints per turn; it has no resource type and must stay on ``*``.
    """
    return [
        iam.PolicyStatement(
            actions=["bedrock-mantle:CreateInference"],
            resources=[stack.format_arn(service="bedrock-mantle", region="*",
                                        resource="project", resource_name="*")],
        ),
        iam.PolicyStatement(
            actions=["bedrock-mantle:CallWithBearerToken"],
            resources=["*"],
        ),
    ]
