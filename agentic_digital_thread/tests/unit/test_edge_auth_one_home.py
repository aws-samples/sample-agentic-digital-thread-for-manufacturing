# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Source-level guard for the single Authorization@Edge infrastructure home."""
from pathlib import Path


STACKS = Path(__file__).resolve().parents[3] / "stacks"
ONE_HOME = STACKS / "_edge_auth.py"
FORBIDDEN = (
    "cloudfront-authorization-at-edge",
    "Outputs.{",
    "wafv2.CfnWebACL(",
    "wafv2.CfnWebACL.RuleProperty(",
    "wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(",
    # External OIDC identity provider (ADR-0072): the provider resource and the
    # client-side provider list are built ONLY by the facade, so every edge
    # client gets the same scopes/mapping/dynamic-reference secret handling.
    "UserPoolIdentityProviderOidc(",
    "UserPoolClientIdentityProvider.custom(",
    "supported_identity_providers=",
)


def test_edge_auth_plumbing_has_one_home():
    offenders = []
    for path in sorted(STACKS.glob("*.py")):
        if path == ONE_HOME:
            continue
        source = path.read_text()
        for needle in FORBIDDEN:
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def test_one_home_contains_every_guarded_primitive():
    source = ONE_HOME.read_text()
    missing = [needle for needle in FORBIDDEN if needle not in source]
    assert missing == [], missing
