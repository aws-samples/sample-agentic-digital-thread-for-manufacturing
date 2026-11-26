# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which connection types a deployment lets take a named Secrets Manager secret.

The allowlist prefix (``ADT_SECRET_ARN_ALLOWLIST``) turns the credential source
on; ``ADT_SECRET_ARN_SOURCE_TYPES`` says for which connection types (``tulip``,
``tulip``), so a build only offers it where its read grants reach. With the
allowlist set and the type list unset the source is Tulip's alone, its
original meaning. ``GET /api/auth/me`` reports the list
(``secret_arn_source_types``) beside the flag (``secret_arn_source_enabled``,
true when the list is not empty), and registration refuses a named secret for
a type the deployment has not enabled. Offline: no AWS, no Neptune.
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.auth.routes as routes  # noqa: E402
import agents.connection_pinning as cp  # noqa: E402
from agents.auth.cognito import UserInfo  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    _register, cat, no_dns, secrets)

ALLOWLIST = "customer/tulip/"
TULIP_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf"


def _env(allowlist=ALLOWLIST, types_value=None) -> dict:
    env = {"AWS_REGION": "us-east-1"}
    if allowlist is not None:
        env["ADT_SECRET_ARN_ALLOWLIST"] = allowlist
    if types_value is not None:
        env["ADT_SECRET_ARN_SOURCE_TYPES"] = types_value
    return env


@pytest.fixture
def build(monkeypatch):
    """Set a deployment's two variables (``None`` = unset)."""
    def _set(allowlist=ALLOWLIST, types_value=None):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        for name, value in (("ADT_SECRET_ARN_ALLOWLIST", allowlist),
                            ("ADT_SECRET_ARN_SOURCE_TYPES", types_value)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
    return _set


@pytest.mark.parametrize("allowlist, types_value, expected", [
    (None, None, ()),
    (None, "tulip", ()),
    (ALLOWLIST, None, (TULIP,)),
    (ALLOWLIST, "", (TULIP,)),
    (ALLOWLIST, "tulip", (TULIP,)),
    (ALLOWLIST, "tulip, unknown", (TULIP,)),
    (ALLOWLIST, "unknown", ()),
])
def test_the_enabled_source_types(allowlist, types_value, expected):
    env = _env(allowlist, types_value)
    assert cp.source_types(env) == expected
    assert cp.arn_source_enabled(env) is bool(expected)
    for connection_type in (TULIP, "MCP Server"):
        assert cp.source_type_enabled(connection_type, env) is (connection_type in expected)


def _me(monkeypatch):
    user = UserInfo(user_id="u1", email="u@example.com", username="u")
    monkeypatch.setattr(routes, "get_current_user", lambda request: user)
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=False))
    return asyncio.run(routes.me(request))


@pytest.mark.parametrize("types_value, expected", [
    (None, [TULIP]), ("tulip", [TULIP])])
def test_me_reports_the_enabled_types(monkeypatch, build, types_value, expected):
    build(types_value=types_value)
    out = _me(monkeypatch)
    assert out["secret_arn_source_types"] == expected
    assert out["secret_arn_source_enabled"] is True


def _register_named(connection_type: str, arn: str) -> dict:
    return _register({"name": "Named Seat", "system_type": "MES", "connection_type": connection_type,
                      "connection_config": {"secret_arn": arn}})


