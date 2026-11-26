# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GET /api/auth/me carries the GENERIC secret_arn_source_enabled UI capability
(item 2): true when this deployment's allowlist is non-empty, so the Connect
New System form can offer the "Secrets Manager secret" credential choice. The
capability is generic; the server still enforces the allowlist on write.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.auth.routes as routes  # noqa: E402
from agents.auth.cognito import UserInfo  # noqa: E402


def _request(is_admin=False):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(is_admin=is_admin)
    return req


def _call_me(monkeypatch, *, allowlist):
    user = UserInfo(user_id="u1", email="u@example.com", username="u")
    monkeypatch.setattr(routes, "get_current_user", lambda request: user)
    if allowlist is None:
        monkeypatch.delenv("ADT_SECRET_ARN_ALLOWLIST", raising=False)
    else:
        monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", allowlist)
    return asyncio.run(routes.me(_request()))


def test_me_reports_arn_source_disabled_when_the_allowlist_is_empty(monkeypatch):
    out = _call_me(monkeypatch, allowlist=None)
    assert out["secret_arn_source_enabled"] is False


def test_me_reports_arn_source_enabled_when_the_allowlist_is_set(monkeypatch):
    out = _call_me(monkeypatch, allowlist="customer/tulip/")
    assert out["secret_arn_source_enabled"] is True


def test_me_still_carries_the_identity_fields(monkeypatch):
    out = _call_me(monkeypatch, allowlist=None)
    assert out["user_id"] == "u1" and out["email"] == "u@example.com"
    assert out["username"] == "u" and out["is_admin"] is False
