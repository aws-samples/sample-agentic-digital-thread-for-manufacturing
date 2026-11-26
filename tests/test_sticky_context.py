# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the SSM-backed sticky context helper (stacks/_sticky_context.py).

These guard the sticky-flag precedence and — critically — the fail-soft
contract: `resolve_sticky` must NEVER raise or block offline synth/CI, no
matter how the SSM read fails (no credentials, no region, parameter absent,
offline). All tests are fully offline: the SSM client is injected as a fake
(no boto3 sessions, no network).

Run:  PYTHONPATH=. python -m pytest tests/test_sticky_context.py -v
"""
from __future__ import annotations

import pytest

from stacks._sticky_context import resolve_sticky

SSM_NAME = "/adt/system-query-mode"


class FakeSsm:
    """Minimal SSM client double: returns a canned value and records calls."""

    def __init__(self, value: str):
        self._value = value
        self.calls = []

    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        self.calls.append(Name)
        return {"Parameter": {"Value": self._value}}


class RaisingSsm:
    """SSM client double that fails like a real client would offline/credless."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        raise self._exc


class TestExplicitWins:
    def test_explicit_context_value_wins_over_ssm(self):
        ssm = FakeSsm("agent")
        assert (
            resolve_sticky("orchestrator", SSM_NAME, region="us-east-1", ssm_client=ssm)
            == "orchestrator"
        )

    def test_explicit_value_skips_the_ssm_read_entirely(self):
        ssm = FakeSsm("agent")
        resolve_sticky("orchestrator", SSM_NAME, region="us-east-1", ssm_client=ssm)
        assert ssm.calls == []

    def test_explicit_value_is_stripped(self):
        assert (
            resolve_sticky("  agent  ", SSM_NAME, region="us-east-1",
                           ssm_client=FakeSsm("orchestrator"))
            == "agent"
        )


class TestSsmFallback:
    def test_ssm_value_used_when_context_is_none(self):
        ssm = FakeSsm("agent")
        assert resolve_sticky(None, SSM_NAME, region="us-east-1", ssm_client=ssm) == "agent"
        assert ssm.calls == [SSM_NAME]

    def test_ssm_value_used_when_context_is_empty_string(self):
        # cdk.json ships crossAccountDataRoleArn: "" — empty must NOT count as explicit.
        ssm = FakeSsm("arn:aws:iam::111122223333:role/adt-data-read")
        assert (
            resolve_sticky("", SSM_NAME, region="us-east-1", ssm_client=ssm)
            == "arn:aws:iam::111122223333:role/adt-data-read"
        )

    def test_whitespace_only_context_falls_through_to_ssm(self):
        assert (
            resolve_sticky("   ", SSM_NAME, region="us-east-1", ssm_client=FakeSsm("agent"))
            == "agent"
        )

    def test_ssm_value_is_stripped(self):
        assert (
            resolve_sticky(None, SSM_NAME, region="us-east-1", ssm_client=FakeSsm(" agent \n"))
            == "agent"
        )


class TestFailSoft:
    @pytest.mark.parametrize(
        "exc",
        [
            Exception("ParameterNotFound"),
            RuntimeError("Unable to locate credentials"),
            ConnectionError("offline"),
            KeyError("Parameter"),
        ],
        ids=["param-missing", "no-credentials", "offline", "malformed-response"],
    )
    def test_any_ssm_exception_returns_default(self, exc):
        assert (
            resolve_sticky(None, SSM_NAME, region="us-east-1",
                           default="fallback", ssm_client=RaisingSsm(exc))
            == "fallback"
        )

    def test_no_region_and_no_client_skips_ssm_and_returns_default(self):
        # Offline synth path: region undeterminable — must not touch boto3/network.
        assert resolve_sticky(None, SSM_NAME, region=None, default="dflt") == "dflt"

    def test_no_region_default_is_empty_string(self):
        # Empty default => key stays absent, so stacks' `try_get_context(...) or
        # "orchestrator"` default logic keeps working.
        assert resolve_sticky(None, SSM_NAME, region=None) == ""


class TestDefaults:
    def test_default_when_context_and_ssm_both_empty(self):
        assert (
            resolve_sticky(None, SSM_NAME, region="us-east-1",
                           default="orchestrator", ssm_client=FakeSsm(""))
            == "orchestrator"
        )

    def test_empty_default_when_everything_empty(self):
        assert resolve_sticky("", SSM_NAME, region="us-east-1", ssm_client=FakeSsm("")) == ""
