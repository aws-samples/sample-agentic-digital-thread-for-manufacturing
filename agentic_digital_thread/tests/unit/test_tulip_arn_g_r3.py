# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Backend fixes for the ARN-sourced Tulip connection re-save and the single
request-tuple source. Offline; no AWS, no Neptune, no network.

Covers:
* the current-connection summary reports the non-secret ``credential_source``
  marker (plus ``workspace_id`` / ``key_scope``) so the admin edit form can
  render a Secrets Manager connection read-only. It never exposes a secret.
* the pre-provision registration path (``_resolve_arn_source``) and the
  provisioned path (``_resolve_tulip_seat``) both get the credential from the
  ONE ``tulip_seat_contract.request_tuple`` constructor (no re-derivation).

These fail on ``87aa6125`` (the summary carried no credential_source, and
_resolve_arn_source re-derived the token) and pass after.
"""
from __future__ import annotations

import base64
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.tulip_seat_contract as tsc  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

OWN = "111122223333"
SEAT_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:customer/tulip/tulip-connection-AbCdEf"
SID = "tulip-mes"
URL = "https://acme.tulip.co"
COMPLETE = {"schema": 1, "mode": "mock", "base_url": URL, "workspace_id": "wsX",
            "key_scope": "instance", "api_key": "K", "api_secret": "S"}  # nosec B106


@pytest.fixture
def arn_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", "customer/tulip/")


def _arn_entry():
    return {"id": SID, "name": "Tulip MES", "connection_type": TULIP, "schema": "",
            "status": "registered", "connection_origin": "user", "secret_arn": SEAT_ARN,
            "connection_config": {"credential_source": "secret_arn"}}


# ── the current-connection summary exposes the non-secret markers ──

def test_summary_reports_credential_source_and_routing_no_secret(monkeypatch, arn_env):
    """describe_connection exposes credential_source=secret_arn plus the
    non-secret workspace_id/key_scope and the ARN pointer, and NO secret
    value (no token/api_key/api_secret)."""
    node = {"connection_config": '{"base_url": "https://stale", "workspace_id": "wsOLD"}',
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(COMPLETE))
    summary = cr.describe_connection(SID)
    assert summary["credential_source"] == "secret_arn"
    assert summary["workspace_id"] == "wsX"
    assert summary["key_scope"] == "instance"
    assert summary["secret_arn"] == SEAT_ARN
    # No secret VALUE leaks: the base64 token value and the raw key/secret are
    # absent. (secret_fields legitimately NAMES 'token' as a presence indicator,
    # never its value.)
    import json
    blob = json.dumps(summary)
    assert base64.b64encode(b"K:S").decode() not in blob
    assert "api_secret" not in blob
    assert summary.get("token") is None and summary.get("api_key") is None


# ── one request-tuple source: registration and provisioned both via request_tuple ──

def test_registration_resolves_through_the_tuple_constructor(monkeypatch, arn_env):
    """_resolve_arn_source (pre-provision) sets base_url/workspace/scope/token
    from the ONE request_tuple, not an independent derivation."""
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(COMPLETE))
    resolved = cr.registration_connection(SID, _arn_entry())
    req = tsc.request_tuple(dict(COMPLETE))
    assert resolved["base_url"] == req.base_url == URL
    assert resolved["workspace_id"] == req.workspace_id == "wsX"
    assert resolved["key_scope"] == req.key_scope == "instance"
    assert resolved["token"] == req.token == base64.b64encode(b"K:S").decode()
    assert resolved["auth_scheme"] == req.auth_scheme == "basic"


def test_registration_rejects_an_incomplete_secret_via_the_tuple(monkeypatch, arn_env):
    """An incomplete secret makes request_tuple raise, so the registration is a
    retryable miss with no routing kept."""
    bad = {k: v for k, v in COMPLETE.items() if k != "workspace_id"}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(bad))
    resolved = cr.registration_connection(SID, _arn_entry())
    assert resolved.get("_secret_unresolved") is True, resolved
    assert not resolved.get("workspace_id") and not resolved.get("token")


def test_provisioned_resolve_uses_the_tuple_token(monkeypatch, arn_env):
    """_resolve_tulip_seat (provisioned node) takes token/scheme from the tuple
    (the single source), not a separate basic_token derivation."""
    node = {"connection_config": '{"base_url": "https://stale", "workspace_id": "wsOLD"}',
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(COMPLETE))
    resolved = cr.resolve_connection(SID)
    req = tsc.request_tuple(dict(COMPLETE))
    assert resolved["token"] == req.token
    assert resolved["auth_scheme"] == req.auth_scheme == "basic"


def test_tuple_carries_token_and_header_from_one_place():
    """The tuple is the single source of the credential AND the header."""
    req = tsc.request_tuple(dict(COMPLETE))
    assert req.token == base64.b64encode(b"K:S").decode()
    assert req.auth_scheme == "basic"
    assert req.auth_header == "Basic " + req.token
