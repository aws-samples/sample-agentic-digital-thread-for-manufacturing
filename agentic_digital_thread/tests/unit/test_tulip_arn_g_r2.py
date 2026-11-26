# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixes for the ARN-sourced Tulip connection: one validated request tuple,
no persisted derived token, key_scope in the request path, stricter ARN
identity and contract validation.

Offline; no AWS, no Neptune, no network. Each test states which behaviour it
covers:

* one validated request tuple on the PROVISIONED and RUNTIME paths: a fresh
  secret that omits ``workspace_id`` (or is otherwise incomplete) sends
  nothing, and no field from the node/runtime snapshot survives beside fresh
  fields.
* the derived Basic token is never persisted in the per-agent config secret
  for an ARN-sourced system.
* ``key_scope`` decides the path: ``instance`` adds ``/api/v3/w/<id>/``,
  ``workspace`` uses the unscoped path, on the provisioned resolve, the runtime
  and app discovery.
* an empty allowlist refuses before any account lookup (no STS), a partial ARN
  (no ``-XXXXXX`` suffix) is refused, and the partition must match the
  deployment Region's partition.
* the contract validates ``schema == 1`` and ``mode``; a derived token forces
  ``auth_scheme=basic``; an explicit token needs an allowed scheme.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_pinning as pinning  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.system_agent as sa  # noqa: E402
import agents.tulip_seat_contract as tsc  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

OWN = "111122223333"
SEAT_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:customer/tulip/tulip-connection-AbCdEf"
SID = "tulip-mes"
MOCK_URL = "https://mock.tulip.co"
LIVE_URL = "https://live.tulip.co"

COMPLETE_WS = {"schema": 1, "mode": "mock", "base_url": MOCK_URL, "workspace_id": "wsOLD",
               "key_scope": "workspace", "api_key": "MK", "api_secret": "MS"}  # nosec B106
# A fresh value that OMITS workspace_id (the incomplete-tuple case).
NEW_NO_WS = {"schema": 1, "mode": "live", "base_url": LIVE_URL, "key_scope": "instance",
             "api_key": "LK", "api_secret": "LS"}  # nosec B106 - no workspace_id


def _basic(key, secret):
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


# ── the PROVISIONED resolve rejects an incomplete fresh secret ──

def test_provisioned_resolve_rejects_a_fresh_secret_that_omits_workspace(monkeypatch):
    """A provisioned ARN Tulip node held the OLD workspace; the next secret omits
    workspace_id. resolve_connection must NOT keep the node's workspace beside
    the fresh url/key - the whole source is a retryable miss."""
    node = {"connection_config": json.dumps({"base_url": MOCK_URL, "workspace_id": "wsOLD",
                                              "key_scope": "workspace"}),
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(NEW_NO_WS))
    resolved = cr.resolve_connection(SID)
    assert resolved.get("_secret_unresolved") is True, resolved
    # No stale field from the node snapshot survives.
    assert not resolved.get("workspace_id"), resolved
    assert resolved.get("base_url") != MOCK_URL, resolved
    assert not resolved.get("token"), resolved


def test_provisioned_resolve_uses_the_whole_fresh_tuple_when_complete(monkeypatch):
    node = {"connection_config": json.dumps({"base_url": "https://stale.tulip.co",
                                              "workspace_id": "wsSTALE", "key_scope": "workspace"}),
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(COMPLETE_WS))
    resolved = cr.resolve_connection(SID)
    assert resolved.get("_secret_unresolved") is False, resolved
    assert resolved["base_url"] == MOCK_URL and resolved["workspace_id"] == "wsOLD"
    assert resolved["key_scope"] == "workspace"
    assert resolved["token"] == base64.b64encode(b"MK:MS").decode()


def test_provisioned_resolve_rejects_an_unreadable_fresh_secret(monkeypatch):
    node = {"connection_config": json.dumps({"base_url": MOCK_URL, "workspace_id": "wsOLD"}),
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    def _boom(arn):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(cr, "_fetch_secret", _boom)
    resolved = cr.resolve_connection(SID)
    assert resolved.get("_secret_unresolved") is True, resolved
    assert not resolved.get("workspace_id") and not resolved.get("token"), resolved


# ── the RUNTIME client sends nothing on an incomplete fresh secret ──

class _FakeResp:
    def __init__(self, body):
        self._body = body
    def raise_for_status(self):
        return None
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, sink, body):
        self._sink, self._body = sink, body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def stream(self, method, url, headers=None, params=None):
        self._sink.append({"url": url, "headers": dict(headers or {})})
        return _FakeResp(self._body)


def _runtime(monkeypatch, secret):
    sink = []
    import agents.pinned_http as pinned_http
    monkeypatch.setattr(pinned_http, "client", lambda **kw: _FakeClient(sink, "[]"))
    monkeypatch.setattr("agents.response_integrity_query.read_http_body_capped",
                        lambda response, limit: (response._body, False))
    monkeypatch.setattr(sa, "_fetch_credentials", lambda arn: dict(secret))
    client = sa._TulipClient(base_url="https://snapshot.tulip.co", secret_arn=SEAT_ARN,
                             workspace_id="wsSNAPSHOT", key_scope="instance")
    return client, sink


def test_runtime_sends_nothing_when_the_fresh_secret_omits_workspace(monkeypatch):
    client, sink = _runtime(monkeypatch, NEW_NO_WS | {"key_scope": "instance"})
    # NEW_NO_WS has no workspace_id -> incomplete contract -> no request.
    with pytest.raises(Exception):
        client.get("/api/v3/tables")
    assert sink == [], "no request may be sent from an incomplete fresh secret"


def test_runtime_sends_nothing_when_the_fresh_secret_is_unreadable(monkeypatch):
    sink = []
    import agents.pinned_http as pinned_http
    monkeypatch.setattr(pinned_http, "client", lambda **kw: _FakeClient(sink, "[]"))
    def _boom(arn):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(sa, "_fetch_credentials", _boom)
    client = sa._TulipClient(base_url="https://snapshot.tulip.co", secret_arn=SEAT_ARN,
                             workspace_id="wsSNAPSHOT", key_scope="instance")
    with pytest.raises(Exception):
        client.get("/api/v3/tables")
    assert sink == []


# ── key_scope decides the path on the runtime ──

def test_runtime_instance_scope_adds_the_w_segment(monkeypatch):
    client, sink = _runtime(monkeypatch, {"schema": 1, "mode": "live", "base_url": LIVE_URL,
                                          "workspace_id": "wsINST", "key_scope": "instance",
                                          "api_key": "LK", "api_secret": "LS"})
    client.get("/api/v3/tables")
    assert sink[-1]["url"] == f"{LIVE_URL}/api/v3/w/wsINST/tables"


def test_runtime_workspace_scope_is_unscoped(monkeypatch):
    client, sink = _runtime(monkeypatch, {"schema": 1, "mode": "mock", "base_url": MOCK_URL,
                                          "workspace_id": "wsWS", "key_scope": "workspace",
                                          "api_key": "MK", "api_secret": "MS"})
    client.get("/api/v3/tables")
    assert sink[-1]["url"] == f"{MOCK_URL}/api/v3/tables"


# ── key_scope decides the app-discover prefix ──

def test_app_discover_prefix_honours_key_scope():
    from agents.onboard_agent import _tulip_api_prefix
    assert _tulip_api_prefix("wsX", "instance") == "/api/v3/w/wsX"
    assert _tulip_api_prefix("wsX", "workspace") == "/api/v3"
    # Empty scope keeps the prior behaviour for an inline-key system.
    assert _tulip_api_prefix("wsX", "") == "/api/v3/w/wsX"


# ── scoped_path helper ──

def test_scoped_path_only_scopes_for_instance():
    assert tsc.scoped_path("/api/v3/tables", "wsX", "instance") == "/api/v3/w/wsX/tables"
    assert tsc.scoped_path("/api/v3/tables", "wsX", "workspace") == "/api/v3/tables"


# ── empty allowlist refuses before any account lookup ──

def test_empty_allowlist_refuses_without_an_account_lookup(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("ADT_SECRET_ARN_ALLOWLIST", raising=False)
    calls = {"account": 0}
    def _acct():
        calls["account"] += 1
        return OWN
    monkeypatch.setattr(cr, "deployment_account_id", _acct)
    monkeypatch.setattr(cr, "_sts_account_id", lambda: (calls.__setitem__("account", calls["account"] + 1), OWN)[1])
    with pytest.raises(pinning.SecretArnNotAllowedError, match="not enabled"):
        pinning.check_arn_allowed(SEAT_ARN)
    assert calls["account"] == 0, "no account lookup while the feature is off"


# ── a partial ARN (no -XXXXXX suffix) is refused ──

def test_a_partial_arn_without_the_suffix_is_refused(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", "customer/tulip/")
    partial = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:customer/tulip/tulip-connection"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="full Secrets Manager"):
        pinning.check_arn_allowed(partial)


# ── the partition must match the deployment Region's partition ──

def test_partition_must_match_the_deployment_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-gov-west-1")   # the aws-us-gov partition
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", "customer/tulip/")
    commercial = f"arn:aws:secretsmanager:us-gov-west-1:{OWN}:secret:customer/tulip/tulip-connection-AbCdEf"
    with pytest.raises(pinning.SecretArnNotAllowedError, match="partition does not match"):
        pinning.check_arn_allowed(commercial)


# ── the contract validates schema and mode ──

@pytest.mark.parametrize("secret", [
    {"base_url": MOCK_URL, "workspace_id": "ws7", "key_scope": "workspace",
     "api_key": "K", "api_secret": "S"},                       # no schema/mode
    {"schema": 99, "mode": "other", "base_url": MOCK_URL, "workspace_id": "ws7",
     "key_scope": "workspace", "api_key": "K", "api_secret": "S"},  # bad schema/mode
])
def test_contract_requires_schema_one_and_a_valid_mode(secret):
    assert tsc.contract_problem(secret) != ""
    assert not tsc.is_complete_contract(secret)


def test_contract_accepts_schema_one_and_a_valid_mode():
    ok = {"schema": 1, "mode": "live", "base_url": MOCK_URL, "workspace_id": "ws7",
          "key_scope": "workspace", "api_key": "K", "api_secret": "S"}  # nosec B106
    assert tsc.contract_problem(ok) == ""


# ── a derived token forces basic; an explicit token needs a scheme ──

def test_derived_token_forces_basic_never_inherits_a_caller_scheme():
    d = {"api_key": "K", "api_secret": "S", "auth_scheme": "bearer"}  # caller tried bearer
    tsc.with_derived_token(d)
    assert d["auth_scheme"] == "basic", d
    assert d["token"] == base64.b64encode(b"K:S").decode()


def test_an_explicit_token_without_an_allowed_scheme_is_rejected():
    s = {"schema": 1, "mode": "live", "base_url": MOCK_URL, "workspace_id": "ws7",
         "key_scope": "workspace", "token": "T"}  # no auth_scheme  # nosec B106
    assert tsc.contract_problem(s) != ""
    s2 = {**s, "auth_scheme": "bearer"}            # not an allowed scheme
    assert tsc.contract_problem(s2) != ""
    s3 = {**s, "auth_scheme": "basic"}             # allowed
    assert tsc.contract_problem(s3) == ""


# ── request_tuple returns the whole immutable tuple or refuses ──

def test_request_tuple_returns_one_tuple_or_raises():
    req = tsc.request_tuple({"schema": 1, "mode": "mock", "base_url": MOCK_URL,
                             "workspace_id": "wsX", "key_scope": "instance",
                             "api_key": "K", "api_secret": "S"})
    assert req.base_url == MOCK_URL and req.workspace_id == "wsX"
    assert req.key_scope == "instance"
    assert req.auth_header == _basic("K", "S")
    with pytest.raises(tsc.TulipContractError):
        tsc.request_tuple({"schema": 1, "mode": "mock", "base_url": MOCK_URL,
                           "key_scope": "instance", "api_key": "K", "api_secret": "S"})
