# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the sticky observability enable flag (resolve_sticky_bool).

The `--observability` deploy flag is opt-in and default-OFF (CloudWatch
Transaction Search span ingestion has continuous cost). It follows the
HighByte sticky discipline: deploy.sh persists ``true`` in SSM
(``/adt/observability-enabled``); app.py re-reads it fail-soft
via :func:`stacks._sticky_context.resolve_sticky_bool` and passes the boolean
into ``ADTComputeStack`` (AGENTS_OBSERVABILITY_DRILLDOWN env + span-read IAM).

These tests guard:
1. precedence — explicit ``-c observability=true`` > SSM sticky value > off;
2. the default-off contract — nothing set means DISABLED;
3. fail-soft — an SSM failure of any kind must resolve to disabled, never
   raise (offline synth / CI must not break).

All tests are fully offline: the SSM client is injected as a fake.

Run:  PYTHONPATH=. python -m pytest tests/test_sticky_context_observability.py -v
"""
from __future__ import annotations

import pytest

from stacks._sticky_context import resolve_sticky_bool

SSM_NAME = "/adt/observability-enabled"


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


class TestDefaultOff:
    """The opt-in contract: with nothing set, observability is DISABLED."""

    def test_absent_everywhere_is_disabled(self):
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm(""))
            is False
        )

    def test_no_region_offline_synth_is_disabled(self):
        # Offline synth path: region undeterminable — no boto3, no network.
        assert resolve_sticky_bool(None, SSM_NAME, region=None) is False

    def test_ssm_false_is_disabled(self):
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm("false"))
            is False
        )

    def test_ssm_junk_is_disabled(self):
        # Only a literal "true" enables — junk never silently enables a
        # cost-bearing feature.
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm("enabled"))
            is False
        )


class TestExplicitWins:
    def test_explicit_true_enables_without_ssm_read(self):
        ssm = FakeSsm("false")
        assert (
            resolve_sticky_bool("true", SSM_NAME, region="us-east-1", ssm_client=ssm)
            is True
        )
        assert ssm.calls == []  # explicit -c value skips the SSM read entirely

    def test_explicit_false_beats_ssm_true(self):
        # An explicit -c observability=false wins over the sticky value.
        assert (
            resolve_sticky_bool("false", SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm("true"))
            is False
        )

    def test_explicit_value_is_case_insensitive(self):
        assert (
            resolve_sticky_bool("TRUE", SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm(""))
            is True
        )


class TestStickyFromSsm:
    def test_ssm_true_enables(self):
        ssm = FakeSsm("true")
        assert resolve_sticky_bool(None, SSM_NAME, region="us-east-1", ssm_client=ssm) is True
        assert ssm.calls == [SSM_NAME]

    def test_ssm_value_is_stripped_and_case_insensitive(self):
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm(" True \n"))
            is True
        )

    def test_empty_context_falls_through_to_ssm(self):
        # cdk context may carry "" — empty must NOT count as explicit.
        assert (
            resolve_sticky_bool("", SSM_NAME, region="us-east-1",
                                ssm_client=FakeSsm("true"))
            is True
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
    def test_any_ssm_exception_resolves_disabled(self, exc):
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1",
                                ssm_client=RaisingSsm(exc))
            is False
        )

    def test_failed_read_honors_caller_default(self):
        assert (
            resolve_sticky_bool(None, SSM_NAME, region="us-east-1", default=True,
                                ssm_client=RaisingSsm(Exception("boom")))
            is True
        )
