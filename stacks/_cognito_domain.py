# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Account-unique Cognito hosted-UI domain prefixes for account-agnostic synth.

Cognito domain prefixes are **globally unique across all AWS accounts** in a
Region. The workshop packaging pipeline synthesizes without an account on
purpose (account-agnostic templates), so any prefix derived in Python from
``self.account`` is computed from an *unresolved CDK token string* and gets
BAKED into the packaged template as a literal — identical for every attendee
account. The first account to deploy wins the name; every other deploy fails.

Live history: ``adt-m2m-00000000`` (workshop deploy, 2026-08-24) and
``erp-dashboard-ccountid6`` — the ERP dashboard sliced the token
``${Token[AWS.AccountId.6]}`` and the regex cleanup left ``ccountid6``; test
event 11 (2026-09-14) failed at minute 20 with ``User pool domain
erp-dashboard-ccountid6 does not exist in this account … AlreadyExists``
because test event 10's account still owned it.

Every Cognito domain in this repo must go through :func:`pin_domain_to_account`
(``tests/test_cognito_domain_pinning.py`` scans the synthesized templates).
"""
from __future__ import annotations

import re

from aws_cdk import Aws, Fn, aws_cognito as cognito

# Synth-time stand-in for the account portion of a domain prefix when the
# account is an unresolved token. Deliberately NOT digit-shaped: a 12-digit
# literal reads like a real AWS account ID to content scanners, and this value
# never reaches a deployed template — :func:`pin_domain_to_account` replaces
# the suffix with ``AWS::AccountId`` at deploy time. Lowercase alphanumerics
# only, so it passes the L2 domain-prefix validation.
UNRESOLVED_ACCOUNT_PLACEHOLDER = "accountid"


def domain_suffix(account: str) -> str:
    """Stable, collision-safe 8-char suffix for Cognito domain prefixes.

    When CDK_DEFAULT_ACCOUNT is set (deploy time), ``account`` is a real numeric
    value and we use its last 8 digits. When it's unset (local synth), it's an
    unresolved CDK token that fails Cognito domain-prefix validation, so fall
    back to a placeholder — and callers MUST then repair the literal at deploy
    time via :func:`pin_domain_to_account` (see it for why).
    """
    return account[-8:] if re.fullmatch(r"\d+", account) else "00000000"


def account_is_concrete(account: str) -> bool:
    """True when synth had a real account (deploy.sh); False for the
    account-agnostic workshop packaging synth."""
    return bool(re.fullmatch(r"\d+", account))


def pin_domain_to_account(
    domain: cognito.UserPoolDomain, prefix: str, account: str
) -> str:
    """Make an account-agnostic synth still produce a UNIQUE domain prefix.

    When the account is concrete (deploy.sh), nothing changes: the literal
    prefix is kept verbatim so existing deployments are untouched and the
    domain is never replaced. When it is not, the L1's ``Domain`` property is
    overridden with a deploy-time ``Fn::Join`` on ``AWS::AccountId`` —
    resolved per attendee account, so every account gets its own prefix.
    ``prefix`` must be ``<base>-<suffix>``; the suffix is what gets replaced.

    Returns the EFFECTIVE domain value (the literal, or the deploy-time token)
    so outputs and downstream wiring never read the L2's stale literal.
    """
    if account_is_concrete(account):
        return prefix
    base = prefix.rsplit("-", 1)[0]  # "adt-m2m-00000000" -> "adt-m2m"
    effective = Fn.join("-", [base, Aws.ACCOUNT_ID])
    domain.node.default_child.add_property_override("Domain", effective)
    return effective
