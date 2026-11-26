# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Authorization@Edge session cookies (SAR cloudfront-authorization-at-edge).

When the CloudFront distribution runs the Cognito Authorization@Edge gate
(deploy-time toggle — ``EDGE_AUTH_CLIENT_ID``/``EDGE_AUTH_ENABLED`` on the app
env), humans arrive already authenticated by the Cognito HOSTED UI; the SAR's
Lambda@Edge functions store their tokens as
``CognitoIdentityServiceProvider.<clientId>.*`` cookies (the Amplify cookie
convention). These helpers parse those cookies so ``AuthMiddleware`` can accept
the edge session WITHOUT a second /login.html form login. (Sign-out leaves
them to the SAR SignOut handler at ``/signout``.) Pure functions over the request cookie
mapping plus two env reads — no I/O, unit-testable offline
(tests/unit/test_edge_session.py).

Both env vars are absent unless the deploy enabled edge auth
(stacks/adt_compute_stack.py) — with them unset every helper is inert and app
behavior is byte-identical to before.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

_COOKIE_PREFIX = "CognitoIdentityServiceProvider"


@dataclass(frozen=True)
class EdgeTokens:
    """The token triple the SAR stores for the signed-in user."""

    username: str
    access_token: str
    id_token: Optional[str] = None


def edge_client_id() -> Optional[str]:
    """The edge-auth Cognito app client id, or None when edge auth is off.

    This is the DISTINCT authorization_code_grant client the CDK stack creates
    for the hosted-UI redirect — never the app's USER_PASSWORD_AUTH client.
    """
    value = (os.getenv("EDGE_AUTH_CLIENT_ID") or "").strip()
    return value or None


def edge_auth_enabled() -> bool:
    """True when the deploy runs the Authorization@Edge gate."""
    return (os.getenv("EDGE_AUTH_ENABLED") or "").strip().lower() == "true"


def parse_edge_cookies(
    cookies: Mapping[str, str], client_id: str
) -> Optional[EdgeTokens]:
    """Extract the SAR cookie triple for ``client_id``; None when incomplete.

    Cookie layout (set by the SAR's ParseAuth/RefreshAuth Lambda@Edge):
      CognitoIdentityServiceProvider.<clientId>.LastAuthUser          -> username
      CognitoIdentityServiceProvider.<clientId>.<username>.accessToken
      CognitoIdentityServiceProvider.<clientId>.<username>.idToken

    Only presence/shape is checked here — the caller MUST still validate the
    access token cryptographically (CognitoAuth.validate_token pinned to the
    edge client id).
    """
    if not client_id:
        return None
    username = (cookies.get(f"{_COOKIE_PREFIX}.{client_id}.LastAuthUser") or "").strip()
    if not username:
        return None
    base = f"{_COOKIE_PREFIX}.{client_id}.{username}"
    access_token = (cookies.get(f"{base}.accessToken") or "").strip()
    if not access_token:
        return None
    id_token = (cookies.get(f"{base}.idToken") or "").strip() or None
    return EdgeTokens(username=username, access_token=access_token, id_token=id_token)
