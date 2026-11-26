# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Strict mode for sticky SSM reads during a real deploy.

The fail-soft read (any SSM error -> "") is right for CI, unit tests and
offline synth, but wrong for a deploy: on 2026-11-16 a deploy whose CDK app
could not reach SSM silently resolved every sticky setting to blank, so the
synthesized stack dropped the external OIDC identity provider and
CloudFormation tried to delete it. Only a cross-stack export stopped it, and
other settings (e.g. the response-integrity mode) have no such guard.

deploy.sh exports ADT_STICKY_STRICT=1 for its CDK runs. In strict mode:
  * a parameter that genuinely does not exist still resolves to the default;
  * any other failure (no credentials, access denied, throttling, no region)
    raises StickyReadError naming the parameter, so the deploy stops before
    CloudFormation sees a stack missing settings.
Without the variable, behavior is unchanged (fail-soft).

Run:  PYTHONPATH=. python -m pytest tests/test_sticky_context_strict.py -v
"""
from __future__ import annotations

import importlib

import pytest

import stacks._sticky_context as sticky

SSM_NAME = "/adt/oidc-issuer-url"


class _ClientError(Exception):
    """Shape of botocore's ClientError: .response['Error']['Code']."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class _NoCredentialsError(Exception):
    """Stands in for botocore.exceptions.NoCredentialsError (no .response)."""


class RaisingSsm:
    def __init__(self, exc: Exception):
        self._exc = exc

    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        raise self._exc


class FakeSsm:
    def __init__(self, value: str):
        self._value = value

    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        return {"Parameter": {"Value": self._value}}


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setenv("ADT_STICKY_STRICT", "1")


@pytest.fixture
def lenient(monkeypatch):
    monkeypatch.delenv("ADT_STICKY_STRICT", raising=False)


def _error_cls():
    return getattr(sticky, "StickyReadError", None)


class TestStrictMode:
    @pytest.mark.parametrize(
        "exc",
        [
            _NoCredentialsError("Unable to locate credentials"),
            _ClientError("AccessDeniedException"),
            _ClientError("ThrottlingException"),
            RuntimeError("endpoint unreachable"),
        ],
    )
    def test_read_failure_raises_and_names_the_parameter(self, strict, exc):
        err = _error_cls()
        assert err is not None, "stacks._sticky_context must define StickyReadError"
        with pytest.raises(err) as info:
            sticky.resolve_sticky(None, SSM_NAME, region="us-east-1",
                                  ssm_client=RaisingSsm(exc))
        assert SSM_NAME in str(info.value)

    def test_bool_wrapper_also_raises(self, strict):
        err = _error_cls()
        assert err is not None
        with pytest.raises(err):
            sticky.resolve_sticky_bool(None, "/adt/edge-auth-enabled", region="us-east-1",
                                       ssm_client=RaisingSsm(_NoCredentialsError("x")))

    def test_missing_parameter_still_returns_default(self, strict):
        assert sticky.resolve_sticky(
            None, SSM_NAME, region="us-east-1", default="dflt",
            ssm_client=RaisingSsm(_ClientError("ParameterNotFound"))) == "dflt"

    def test_value_still_read_normally(self, strict):
        assert sticky.resolve_sticky(None, SSM_NAME, region="us-east-1",
                                     ssm_client=FakeSsm(" https://idp ")) == "https://idp"

    def test_explicit_context_still_skips_ssm(self, strict):
        assert sticky.resolve_sticky("explicit", SSM_NAME, region="us-east-1",
                                     ssm_client=RaisingSsm(_NoCredentialsError("x"))) == "explicit"

    def test_no_region_raises(self, strict):
        err = _error_cls()
        assert err is not None
        with pytest.raises(err):
            sticky.resolve_sticky(None, SSM_NAME, region=None)


class TestLenientUnchanged:
    @pytest.mark.parametrize(
        "exc",
        [_NoCredentialsError("x"), _ClientError("AccessDeniedException"),
         _ClientError("ParameterNotFound"), RuntimeError("x")],
    )
    def test_any_failure_returns_default(self, lenient, exc):
        assert sticky.resolve_sticky(None, SSM_NAME, region="us-east-1", default="d",
                                     ssm_client=RaisingSsm(exc)) == "d"

    def test_falsy_strict_values_are_lenient(self, monkeypatch):
        for v in ("", "0", "false"):
            monkeypatch.setenv("ADT_STICKY_STRICT", v)
            importlib.reload(sticky)
            assert sticky.resolve_sticky(None, SSM_NAME, region="us-east-1", default="d",
                                         ssm_client=RaisingSsm(RuntimeError("x"))) == "d"


def test_deploy_sh_turns_strict_mode_on():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent
            / "agentic_digital_thread" / "deploy.sh").read_text()
    assert "export ADT_STICKY_STRICT=1" in text


def test_app_highbyte_reader_uses_the_shared_sticky_reader():
    """app.py's HighByte enable read must go through resolve_sticky_bool, so
    it inherits strict mode (incl. the no-region case) instead of carrying its
    own fail-soft copy."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_highbyte_enabled_in_ssm")
    calls = {c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "resolve_sticky_bool" in calls
    src = ast.get_source_segment((Path(__file__).resolve().parent.parent / "app.py").read_text(), fn)
    assert "get_parameter" not in src and "except" not in src
