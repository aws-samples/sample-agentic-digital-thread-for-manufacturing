# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tests.harness._guard — fully offline (STS is monkeypatched).

These exercise the destructive-op safety guards without any live AWS call:
``assert_dev_account`` is driven by a fake boto3 Session/STS, and the env-based
guards are driven by monkeypatched environment variables. Run from
``agentic_digital_thread/``::

    python -m pytest tests/harness/test_guard.py
"""

from __future__ import annotations

import pytest

from tests.harness import _guard
from tests.harness._guard import (
    GuardError,
    assert_dev_account,
    assert_target_graph,
    require_opt_in,
)


# ---------------------------------------------------------------------------
# Fakes — stand in for boto3.Session / the STS client (no network).
# ---------------------------------------------------------------------------
class _FakeSTS:
    def __init__(self, account: str) -> None:
        self._account = account

    def get_caller_identity(self) -> dict:
        return {"Account": self._account}


class _FakeSession:
    def __init__(self, account: str, region: str | None) -> None:
        self.region_name = region
        self._account = account

    def client(self, name: str, region_name: str | None = None):
        assert name == "sts"
        return _FakeSTS(self._account)


def _patch_session(monkeypatch, account: str, region: str | None) -> None:
    """Replace boto3.Session in the guard module with a fake returning account/region."""
    monkeypatch.setattr(
        _guard.boto3, "Session", lambda *a, **k: _FakeSession(account, region)
    )


# ---------------------------------------------------------------------------
# assert_dev_account
# ---------------------------------------------------------------------------
def test_assert_dev_account_passes_on_dev(monkeypatch):
    _patch_session(monkeypatch, _guard.DEV_ACCOUNT, _guard.DEV_REGION)
    assert assert_dev_account() == _guard.DEV_ACCOUNT


def test_assert_dev_account_raises_on_wrong_account(monkeypatch):
    _patch_session(monkeypatch, "999999999999", _guard.DEV_REGION)
    with pytest.raises(GuardError, match="dev account"):
        assert_dev_account()


def test_assert_dev_account_raises_on_wrong_region(monkeypatch):
    _patch_session(monkeypatch, _guard.DEV_ACCOUNT, "us-west-2")
    with pytest.raises(GuardError, match="dev region"):
        assert_dev_account()


# ---------------------------------------------------------------------------
# assert_target_graph
# ---------------------------------------------------------------------------
def test_assert_target_graph_raises_when_unset(monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    with pytest.raises(GuardError, match="unset"):
        assert_target_graph()


def test_assert_target_graph_raises_on_placeholder(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "adt")
    with pytest.raises(GuardError, match="placeholder"):
        assert_target_graph()


def test_assert_target_graph_raises_on_mismatch(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abc123")
    with pytest.raises(GuardError, match="expected"):
        assert_target_graph(expected_graph_id="g-different")


def test_assert_target_graph_passes_on_real_id(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abc123")
    assert assert_target_graph() == "g-abc123"
    assert assert_target_graph(expected_graph_id="g-abc123") == "g-abc123"


# ---------------------------------------------------------------------------
# require_opt_in
# ---------------------------------------------------------------------------
def test_require_opt_in_raises_without_optin(monkeypatch):
    monkeypatch.delenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", raising=False)
    with pytest.raises(GuardError, match="opt-in"):
        require_opt_in()


def test_require_opt_in_passes_with_yes(monkeypatch):
    monkeypatch.delenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", raising=False)
    assert require_opt_in(yes=True) is None


def test_require_opt_in_passes_with_env(monkeypatch):
    monkeypatch.setenv("ADT_HARNESS_ALLOW_DESTRUCTIVE", "1")
    assert require_opt_in() is None
