# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the cross-account STS AssumeRole PREFLIGHT in ``probe_connection``.

Offline — the assume seam (``agents.aws_session.session_for``) and the
type-specific probe are both mocked; no live AWS / DB / network. Verifies:

  * AccessDenied on sts:AssumeRole -> status='fail', reason='assume_role_denied'
    with the actionable detail (and NO role-ARN echo); the type probe never runs.
  * Assume succeeds -> falls through to the normal type-specific probe unchanged.
  * No role_arn in the resolved config -> the preflight is skipped entirely
    (``session_for`` never called) — behavior identical to today.
  * Other STS errors (throttle, malformed ARN) -> existing generic failure
    semantics; no new reason values.

Follows the probe test pattern of ``test_admin_connection_test.py``.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_probes as cp  # noqa: E402
from agents.aws_session import AwsSessionError  # noqa: E402

# AWS documentation placeholder account id (never a real account).
_ROLE_ARN = "arn:aws:iam::111122223333:role/adt-data-read"

_EXPECTED_DENIED_DETAIL = (
    "ADT's deploy has not granted sts:AssumeRole for this role (or the "
    "data-account trust/ExternalId rejects it). Redeploy once with "
    "--cross-account-role convention, or add this role ARN to the explicit "
    "grant list. See docs/cross-account-onboarding.md"
    "#dynamic-onboarding-what-must-exist-ahead-of-time-deploy-once-checklist"
)


def _resolved(role_arn: str | None = _ROLE_ARN) -> dict:
    r = {
        "system_id": "erp",
        "connection_type": "PostgreSQL",
        "connection_string": "postgresql://user@db.example.com:5432/erp",
        "_secret_unresolved": False,
    }
    if role_arn:
        r["role_arn"] = role_arn
    return r


def _session_for_denied(cfg, default_region=None, service="adt"):
    """Fake assume seam: AwsSessionError caused by an STS AccessDenied (matches
    aws_session.session_for's fail-closed wrap: ``raise AwsSessionError(...) from e``)."""
    try:
        raise ClientError(
            {"Error": {"Code": "AccessDenied",
                       "Message": "User is not authorized to perform: sts:AssumeRole"}},
            "AssumeRole",
        )
    except ClientError as e:
        raise AwsSessionError(f"AssumeRole failed for role_arn={cfg['role_arn']}: {e}") from e


def _session_for_throttled(cfg, default_region=None, service="adt"):
    try:
        raise ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                          "AssumeRole")
    except ClientError as e:
        raise AwsSessionError(f"AssumeRole failed for role_arn={cfg['role_arn']}: {e}") from e


def test_denied_returns_assume_role_denied_with_actionable_detail():
    """AccessDenied on the preflight -> fail/assume_role_denied; type probe never runs."""
    probe_calls = []
    with patch("agents.aws_session.session_for", _session_for_denied), \
            patch.dict(cp._PROBES, {"postgres": lambda r: probe_calls.append(r) or "SELECT 1 ok"}):
        res = cp.probe_connection("erp", _resolved())

    assert res["status"] == "fail", res
    assert res["reason"] == "assume_role_denied", res
    assert res["detail"] == _EXPECTED_DENIED_DETAIL, res
    assert res["connection_type"] == "PostgreSQL", res
    assert isinstance(res["latency_ms"], int), res
    assert probe_calls == [], "type probe must NOT run when the preflight is denied"

    # No ARN / account / secret echo in the response.
    blob = json.dumps(res)
    assert "111122223333" not in blob, blob
    assert "arn:" not in blob.lower(), blob


def test_assume_success_falls_through_to_type_probe():
    """A successful preflight proceeds to the normal type-specific probe unchanged."""
    assume_calls = []

    def _session_for_ok(cfg, default_region=None, service="adt"):
        assume_calls.append(cfg)
        return object()  # session unused by the preflight

    with patch("agents.aws_session.session_for", _session_for_ok), \
            patch.dict(cp._PROBES, {"postgres": lambda r: "SELECT 1 ok"}):
        res = cp.probe_connection("erp", _resolved())

    assert assume_calls and assume_calls[0]["role_arn"] == _ROLE_ARN
    assert res["status"] == "pass", res
    assert res["reason"] == "ok", res
    assert res["detail"] == "SELECT 1 ok", res


def test_no_role_arn_skips_preflight():
    """No role_arn -> session_for is never called; behavior identical to today."""
    def _session_for_boom(*a, **k):
        raise AssertionError("session_for must not be called without a role_arn")

    with patch("agents.aws_session.session_for", _session_for_boom), \
            patch.dict(cp._PROBES, {"postgres": lambda r: "SELECT 1 ok"}):
        res = cp.probe_connection("erp", _resolved(role_arn=None))

    assert res["status"] == "pass", res
    assert res["reason"] == "ok", res
    assert res["detail"] == "SELECT 1 ok", res


def test_nested_connection_config_role_arn_triggers_preflight():
    """role_arn nested under connection_config (raw shape) also triggers the preflight."""
    resolved = _resolved(role_arn=None)
    resolved["connection_config"] = {"role_arn": _ROLE_ARN, "region": "us-east-1"}
    with patch("agents.aws_session.session_for", _session_for_denied), \
            patch.dict(cp._PROBES, {"postgres": lambda r: "SELECT 1 ok"}):
        res = cp.probe_connection("erp", resolved)
    assert res["reason"] == "assume_role_denied", res
    assert res["detail"] == _EXPECTED_DENIED_DETAIL, res


def test_other_sts_errors_keep_generic_semantics():
    """Throttle / malformed-ARN preflight failures use the EXISTING coarse reasons
    (no new reason values beyond assume_role_denied)."""
    # Throttling (ClientError cause with a non-AccessDenied code).
    with patch("agents.aws_session.session_for", _session_for_throttled), \
            patch.dict(cp._PROBES, {"postgres": lambda r: "SELECT 1 ok"}):
        res = cp.probe_connection("erp", _resolved())
    assert res["status"] == "fail", res
    assert res["reason"] != "assume_role_denied", res
    assert res["reason"] in cp._REASON_DETAIL, res

    # Malformed ARN (AwsSessionError raised with NO STS cause — pre-assume validation).
    def _session_for_malformed(cfg, default_region=None, service="adt"):
        raise AwsSessionError("connection_config.role_arn is not a valid IAM role ARN")

    with patch("agents.aws_session.session_for", _session_for_malformed), \
            patch.dict(cp._PROBES, {"postgres": lambda r: "SELECT 1 ok"}):
        res = cp.probe_connection("erp", _resolved())
    assert res["status"] == "fail", res
    assert res["reason"] != "assume_role_denied", res
    assert res["reason"] in cp._REASON_DETAIL, res


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("OK")
