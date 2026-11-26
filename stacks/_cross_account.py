# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure helper for the ``crossAccountDataRoleArn`` deploy-time context value.

Parses the single context string into the list of IAM ``Resource`` ARNs the
ADT app task role (``stacks/adt_compute_stack.py``) and the AgentCore runtime
role (``stacks/agentcore_stack.py``) are granted ``sts:AssumeRole`` on. The
same key supports three modes (backward compatible with the original
single-ARN semantics):

* **empty / absent** → ``[]`` — NO grant. The base sample stays wildcard-free
  and byte-identical to a deployment that never heard of cross-account data.
* **the literal keyword** ``convention`` (case-insensitive) → the
  naming-convention wildcard ``arn:aws:iam::*:role/ADTCrossAccount*``.
  Self-service mode: any data account that deploys the standard trust-pinned
  role template becomes onboardable with no ADT redeploy.
* **anything else** → a comma-separated list of explicit role ARNs, granted
  exactly as given (a single ARN behaves exactly as before). Malformed
  entries raise ``ValueError`` rather than silently granting the wrong thing.

SECURITY JUSTIFICATION
======================
The caller-side ``sts:AssumeRole`` grant produced here is **defense-in-depth,
not the primary access control**. The primary gate lives in the DATA account:

1. the data-account role's **trust policy** is pinned to THIS (demo) account's
   principal, and
2. the trust policy requires the customer's ``sts:ExternalId``
   (see docs/cross-account-onboarding.md).

An ``sts:AssumeRole`` attempt — even under the ``convention`` wildcard —
against a role that does not trust this account (or without the correct
ExternalId) **fails at STS**, regardless of what the caller-side policy
allows. The wildcard therefore only widens which *already-trusting* roles the
app may assume; it cannot reach into accounts that have not explicitly
deployed the trust-pinned role template. ``convention`` mode is **opt-in
only, never the default**: it trades enumerated resource scoping for
self-service onboarding, and stricter environments should keep the explicit
ARN list instead.
"""
from __future__ import annotations

#: Case-insensitive keyword that selects the naming-convention wildcard mode.
CONVENTION_KEYWORD = "convention"

#: Grant resource used in convention mode — matches any account's role that
#: follows the standard ``ADTCrossAccount*`` naming convention.
CONVENTION_WILDCARD_ARN = "arn:aws:iam::*:role/ADTCrossAccount*"

#: cdk-nag AwsSolutions-IAM5 justification for the convention-mode wildcard.
CONVENTION_NAG_JUSTIFICATION = (
    "Opt-in cross-account 'convention' mode: the sts:AssumeRole resource "
    "wildcard arn:aws:iam::*:role/ADTCrossAccount* is defense-in-depth only. "
    "The real gate is in each DATA account: the target role's trust policy is "
    "pinned to this account AND requires the customer's sts:ExternalId, so "
    "an assume attempt against a role that does not trust this account fails "
    "at STS. Never the default (empty context = no grant); stricter "
    "environments should pass an explicit comma-separated ARN list instead."
)


def cross_account_grant_resources(value: str | None) -> list[str]:
    """Return the sts:AssumeRole ``Resource`` list for a context value.

    Modes: empty/absent → ``[]`` (no grant); the literal ``convention``
    (case-insensitive) → ``[CONVENTION_WILDCARD_ARN]``; otherwise a
    comma-separated list of explicit IAM role ARNs (whitespace around
    entries is stripped, empty segments are dropped).

    :raises ValueError: if any explicit entry does not look like an IAM role
        ARN (must start with ``arn:aws:iam::`` and contain ``:role/``) —
        fail loudly rather than silently granting the wrong resource.
    """
    if value is None:
        return []
    text = value.strip()
    if not text:
        return []
    if text.lower() == CONVENTION_KEYWORD:
        return [CONVENTION_WILDCARD_ARN]

    resources: list[str] = []
    for segment in text.split(","):
        entry = segment.strip()
        if not entry:
            continue
        if not entry.startswith("arn:aws:iam::") or ":role/" not in entry:
            raise ValueError(
                "crossAccountDataRoleArn: malformed role ARN entry "
                f"{entry!r} — each comma-separated entry must be an IAM role "
                "ARN (arn:aws:iam::<account>:role/<name>), or use the single "
                f"keyword {CONVENTION_KEYWORD!r} for the naming-convention "
                "wildcard."
            )
        resources.append(entry)
    return resources


def grant_cross_account_assume(stack, role) -> list[str]:
    """Grant *role* ``sts:AssumeRole`` on the deploy's cross-account data role(s).

    The ONE caller-side grant (ADR-0069 follow-up 4): the ECS app task role,
    the AgentCore runtime role and the onboarding runtime role each run code
    that assumes the data account's read-only role (query time, resolve
    evidence, and — on the onboarding runtime — Athena/SiteWise DISCOVERY).
    Two of the three used to hand-roll this block; the third had none, so
    chat-driven discovery on a cross-account system failed with AccessDenied
    while the wizard (app process) succeeded. Reads the same
    ``crossAccountDataRoleArn`` context; a no-op when unset. Returns the
    granted resources (empty when none) so callers/tests can pin them.
    """
    from aws_cdk import aws_iam as iam
    from cdk_nag import NagPackSuppression, NagSuppressions
    resources = cross_account_grant_resources(
        stack.node.try_get_context("crossAccountDataRoleArn"))
    if not resources:
        return []
    role.add_to_policy(iam.PolicyStatement(
        actions=["sts:AssumeRole"], resources=resources))
    if CONVENTION_WILDCARD_ARN in resources:
        # Opt-in convention mode produces a resource wildcard — suppress
        # AwsSolutions-IAM5 with the documented justification (defense-in-depth;
        # the STS trust policy + ExternalId are the real gate).
        NagSuppressions.add_resource_suppressions(
            role, [NagPackSuppression(id="AwsSolutions-IAM5",
                                      reason=CONVENTION_NAG_JUSTIFICATION)],
            apply_to_children=True)
    return resources
