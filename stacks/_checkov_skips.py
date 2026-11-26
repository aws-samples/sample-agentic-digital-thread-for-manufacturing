# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
checkov skips for the Agentic Digital Thread sample.

The synthesized templates are scanned with checkov by adopters' pipelines.
checkov honors a per-resource ``Metadata.checkov.skip`` list, so the stock
checkov findings that are either false positives for this architecture or
deliberate demo-grade trade-offs are suppressed HERE, each with a ``comment``
an adopter can review — the same contract as ``_nag_suppressions.py`` for
cdk-nag.

Note: the Workshop Studio asset scanner (checkov-based) applies its own
high-severity rule subset (CKV_AWS_2, 59, 76, 103, 107, 109, 110, 111 and 161
seen so far) and reports, for those checks, every FAILED result AND every
SKIPPED one (ADR-0065). checkov records a listed skip without evaluating the
check, so a skip in that subset is a finding whether or not the check would
fail: it can never lower the scanner's count, and a skip for a check that
passes adds one. For those check ids, fix the resource or leave it failing;
add a skip only when stock checkov really fails and adopters need the reason.
The other skips below are outside that subset and are not reported by it.

Only findings with a documented reason belong in this module. A finding that
can be fixed by changing the resource is fixed in the stack instead.

Usage from ``app.py``::

    from stacks._checkov_skips import (
        apply_agentcore_checkov_skips,
        apply_adt_compute_checkov_skips,
        apply_onboarding_checkov_skips,
    )
"""
from __future__ import annotations

from typing import Iterable

import aws_cdk as cdk
from constructs import Construct, IConstruct


def checkov_skip(scope: IConstruct, skips: Iterable[tuple[str, str]]) -> None:
    """Attach ``Metadata.checkov.skip`` entries to the L1 behind ``scope``.

    ``scope`` may be an L2 construct (its ``default_child`` is used) or a
    ``CfnResource``. Entries accumulate, so several calls may target one
    resource.
    """
    cfn = scope if isinstance(scope, cdk.CfnResource) else scope.node.default_child
    if not isinstance(cfn, cdk.CfnResource):
        raise TypeError(f"{scope.node.path} has no CfnResource to annotate")
    existing = cfn.get_metadata("checkov") or {}
    entries = list(existing.get("skip", []))
    entries.extend({"id": check_id, "comment": comment} for check_id, comment in skips)
    cfn.add_metadata("checkov", {**existing, "skip": entries})


def _lambda_functions(stack: cdk.Stack) -> list[cdk.CfnResource]:
    return [
        c
        for c in stack.node.find_all()
        if isinstance(c, cdk.CfnResource) and c.cfn_resource_type == "AWS::Lambda::Function"
    ]


_LAMBDA_ENV_CMK = (
    "CKV_AWS_173",
    "Lambda environment variables hold only resource identifiers (table names, "
    "ARNs, endpoints) and no secrets; they are encrypted at rest with the "
    "AWS-managed key. A customer-managed KMS key adds a per-account key and "
    "policy surface with no benefit for this demo workload.",
)


# ---------------------------------------------------------------------------
# ADTAgentCoreStack
# ---------------------------------------------------------------------------
def apply_agentcore_checkov_skips(stack: cdk.Stack, agent_repo: Construct) -> None:
    checkov_skip(
        agent_repo,
        [
            (
                "CKV_AWS_51",
                "Images are pushed under content-addressed tags (the 64-hex asset "
                "tag of the source archive) and the workshop image-replication step "
                "skips any tag whose digest already matches, so tags are never "
                "silently re-pointed in practice. Enforcing immutability is deferred "
                "until that replication path is validated against it end to end.",
            ),
            (
                "CKV_AWS_136",
                "ECR default encryption (AES-256, AWS-owned key) protects a demo "
                "agent image that contains no secrets; a customer-managed key is "
                "not warranted for this sample.",
            ),
        ],
    )
    for fn in _lambda_functions(stack):
        checkov_skip(fn, [_LAMBDA_ENV_CMK])


# ---------------------------------------------------------------------------
# ADTComputeStack
# ---------------------------------------------------------------------------
def apply_adt_compute_checkov_skips(
    stack: cdk.Stack, distribution: Construct, alb: Construct
) -> None:
    checkov_skip(
        distribution,
        [
            (
                "CKV_AWS_174",
                "The distribution uses the default CloudFront certificate "
                "(*.cloudfront.net); CloudFront fixes the minimum viewer protocol "
                "for that certificate and does not allow raising it. A custom "
                "domain deployment (the optional custom-domain stack) supplies an "
                "ACM certificate with TLS 1.2 as its minimum.",
            ),
            (
                "CKV_AWS_86",
                "Access logging needs a dedicated log bucket that no one reads "
                "during a short-lived demo or workshop account; request-level "
                "diagnostics come from the ECS application logs in CloudWatch.",
            ),
        ],
    )
    checkov_skip(
        alb,
        [
            (
                "CKV_AWS_91",
                "The ALB is internal and reachable only through the CloudFront VPC "
                "origin; access logging would need a dedicated log bucket that is "
                "not consumed in a demo or workshop account. Request diagnostics "
                "come from the ECS application logs in CloudWatch.",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# ADTOnboardingStack
# ---------------------------------------------------------------------------
def apply_onboarding_checkov_skips(stack: cdk.Stack) -> None:
    for fn in _lambda_functions(stack):
        checkov_skip(fn, [_LAMBDA_ENV_CMK])
