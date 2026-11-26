# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An MCP registration carries one URL beside its token.

A user MCP registration could carry ``connection_string`` A and
``connection_config.url`` B (or ``base_url``) with a token: only A (or, when
A was empty, B) passed the target guard, both were kept, and discover /
provision prefer ``url``, so the stored token went to B. Already possible before
this change (same validation, same preference). Now every non-empty URL field is
guarded and they must all be the same URL after normalisation, or the
registration is refused before anything is claimed or stored. Offline.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, _register, cat, secrets)

A = "https://mcp.example.com/mcp"
B = "https://other.example.net/mcp"
BLOCKED = "http://169.254.169.254/latest/meta-data"
TOKEN = "tok-123"  # nosec B105 - fixture


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    checked: list = []
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def _validate(target, *a, **kw):
        checked.append(str(target))
        if "169.254.169.254" in str(target):
            raise cr.ConnectionTargetError("connection target is a cloud metadata endpoint")
        return "203.0.113.10"
    monkeypatch.setattr(cr, "validate_connection_target", _validate)
    return checked


def _mcp(connection_string: str, **cfg) -> dict:
    return _register({"name": "Quality Lab", "connection_type": "MCP Server",
                      "connection_string": connection_string,
                      "connection_config": {"token": TOKEN, **cfg}})


def _nothing_stored(cat, secrets):
    assert cat.get(SID) is None, cat.get(SID)
    assert secrets == {}, secrets


@pytest.mark.parametrize("key", ["url", "base_url"])
def test_an_allowed_url_beside_a_blocked_one_is_refused(cat, secrets, guard, key):
    res = _mcp(A, **{key: BLOCKED})
    assert "error" in res and "rejected" in res["error"], res
    assert BLOCKED in guard
    _nothing_stored(cat, secrets)


@pytest.mark.parametrize("key", ["url", "base_url"])
def test_two_different_allowed_urls_are_refused(cat, secrets, key):
    res = _mcp(A, **{key: B})
    assert "error" in res and "one URL" in res["error"], res
    _nothing_stored(cat, secrets)


def test_a_url_only_in_connection_config_is_still_guarded(cat, secrets, guard):
    res = _mcp("", url=BLOCKED)
    assert "error" in res, res
    _nothing_stored(cat, secrets)


def test_guard_the_same_url_in_both_fields_after_normalisation_registers(cat, secrets):
    # Case and the explicit default port normalise; a trailing slash does not
    # (a different resource: test_connect_endpoint_identity.py).
    res = _mcp(A, url="HTTPS://MCP.example.com:443/mcp")
    assert res.get("status") == "created", res


def test_guard_one_url_registers(cat, secrets):
    assert _mcp(A).get("status") == "created"
