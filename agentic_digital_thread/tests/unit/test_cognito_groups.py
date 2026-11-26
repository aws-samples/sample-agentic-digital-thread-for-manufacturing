# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1.3 (ADR-0033): `cognito:groups` surfaced on UserInfo via validate_token.

Offline — patches the JWKS fetch and jwt.decode so no network/keys are needed.
"""
from unittest.mock import patch

import agents.auth.cognito as cognito_mod
from agents.auth.cognito import CognitoAuth


def _auth() -> CognitoAuth:
    return CognitoAuth(
        user_pool_id="us-east-1_pool",
        client_id="client123",
        client_secret=None,
        region="us-east-1",
    )


def _validate_with_claims(claims: dict):
    auth = _auth()
    with patch.object(CognitoAuth, "_get_jwks", return_value=[]), \
         patch.object(cognito_mod, "_signing_key", return_value="key"), \
         patch.object(cognito_mod.jwt, "decode", return_value=claims):
        return auth.validate_token("dummy.token.value")


def _base_claims(**extra) -> dict:
    return {
        "sub": "u-1",
        "token_use": "access",
        "client_id": "client123",
        "username": "alice",
        "email": "alice@example.com",
        **extra,
    }


def test_groups_surfaced_from_claim():
    info = _validate_with_claims(_base_claims(**{"cognito:groups": ["integrator", "analyst"]}))
    assert info.groups == ["integrator", "analyst"]
    assert info.user_id == "u-1"


def test_groups_absent_is_empty_list():
    info = _validate_with_claims(_base_claims())
    assert info.groups == []


def test_is_admin_semantics_preserved():
    # is_admin is a static helper over a group list — unchanged by Task 1.3.
    assert CognitoAuth.is_admin(["Admin"]) is True
    assert CognitoAuth.is_admin(["analyst"]) is False
    info = _validate_with_claims(_base_claims(**{"cognito:groups": ["Admin"]}))
    assert "Admin" in info.groups
    assert CognitoAuth.is_admin(info.groups) is True
