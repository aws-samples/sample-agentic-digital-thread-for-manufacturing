# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic REST API / i3X discover: one stored URL per token.

The wizard's REST-family discover (``onboard._rest_family_discover``, generic
REST API and SiteWise (i3X)) took the stored token from the System node but the
spec URL from the request. ``_discover_openapi`` attached the token whenever
the spec URL ``startswith`` the base URL, so a request spec URL on a look-alike
host (``https://api.example.com.attacker.example/...``) received the token, and
a request ``base_url`` filled in where the node had none re-pointed it. Now, as
for MCP / Tulip / i3X provision: when the node holds a token, a request
URL that differs from the node's stored URL is refused before any request; a
token with no stored URL runs nothing authenticated; and the OpenAPI fetch
sends a token only to the base URL's own origin and path. Offline.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import cat, no_dns  # noqa: E402,F401

SID = "legacy-rest"
TOKEN = "stored-rest-token-777"  # nosec B105 - fixture
BASE = "https://api.example.com"
SPEC = "https://api.example.com/openapi.json"
LOOKALIKE = "https://api.example.com.attacker.example/openapi.json"
ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{SID}-AbCdEf"
SPEC_BODY = json.dumps({"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
                        "paths": {"/orders": {"get": {"responses": {"200": {"description": "ok"}}}}}})


@pytest.fixture
def http_get(monkeypatch):
    calls: list = []

    class _Resp:
        text = SPEC_BODY

        def raise_for_status(self):
            return None

    def _get(url, headers=None, timeout=None, **kw):
        calls.append((url, (headers or {}).get("Authorization", "")))
        return _Resp()
    from agents import pinned_http
    monkeypatch.setattr(pinned_http, "get", _get)
    return calls


def _legacy_node(cat, monkeypatch, cfg: dict, ctype: str = "REST API"):
    cat.add({"id": SID, "name": "Legacy REST", "connection_type": ctype, "schema": "",
             "status": "onboarded"})
    node = {"connection_config": json.dumps(cfg), "secret_arn": ARN, "connection_type": ctype}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node if sid == SID else None)
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: {"token": TOKEN})


def _discover(url: str, ctype: str = "REST API", body_cfg: dict | None = None):
    return onboard._rest_family_discover(SID, url, "", ctype, body_cfg or {})


def _token_went_to(calls, prefix: str) -> bool:
    return any(u.startswith(prefix) and TOKEN in auth for u, auth in calls)


@pytest.mark.parametrize("ctype", ["REST API", "SiteWise (i3X)"])
def test_a_look_alike_request_spec_url_never_gets_the_stored_token(
        cat, monkeypatch, http_get, ctype):
    _legacy_node(cat, monkeypatch, {"base_url": BASE}, ctype)
    result, _ = _discover(LOOKALIKE, ctype)
    assert not _token_went_to(http_get, "https://api.example.com.attacker.example"), http_get
    assert "error" in result and "does not match" in result["error"], result
    assert http_get == [], "refused before any request"


def test_a_request_base_url_never_re_points_the_stored_token(cat, monkeypatch, http_get):
    """Node token, no node URL: the request's base_url / spec URL run tokenless."""
    _legacy_node(cat, monkeypatch, {"auth_scheme": "bearer"})
    _discover("https://attacker.example/openapi.json",
              body_cfg={"base_url": "https://attacker.example"})
    assert http_get and not any(TOKEN in auth for _, auth in http_get), http_get


def test_guard_the_stored_url_still_gets_its_token(cat, monkeypatch, http_get):
    _legacy_node(cat, monkeypatch, {"base_url": BASE, "connection_string": SPEC})
    result, _ = _discover(SPEC)
    assert "error" not in result, result
    assert http_get == [(SPEC, f"Bearer {TOKEN}")], http_get


def test_guard_the_base_url_as_the_request_url_still_works(cat, monkeypatch, http_get):
    _legacy_node(cat, monkeypatch, {"base_url": BASE})
    result, _ = _discover(BASE + "/")
    assert "error" not in result, result
    assert http_get and http_get[0][1] == f"Bearer {TOKEN}", http_get


def test_guard_a_spec_url_on_the_stored_server_still_gets_its_token(cat, monkeypatch, http_get):
    """A legacy node that stores only base_url, with the catalog's spec URL on
    that server as the request URL (what the wizard sends)."""
    _legacy_node(cat, monkeypatch, {"base_url": BASE})
    result, _ = _discover(SPEC)
    assert "error" not in result, result
    assert http_get == [(SPEC, f"Bearer {TOKEN}")], http_get


def test_guard_a_tokenless_system_keeps_the_request_url(cat, monkeypatch, http_get):
    cat.add({"id": SID, "name": "Legacy REST", "connection_type": "REST API", "schema": "",
             "status": "onboarded"})
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    result, _ = _discover("https://public.example.org/openapi.json")
    assert "error" not in result, result
    assert http_get == [("https://public.example.org/openapi.json", "")], http_get


@pytest.mark.parametrize("spec, sent", [
    (SPEC, True),
    (BASE, True),
    (LOOKALIKE, False),
    ("https://api.example.com:8443/openapi.json", False),
    ("http://api.example.com/openapi.json", False),
    ("https://api.example.com/other/openapi.json", True),
])
def test_the_openapi_fetch_sends_the_token_only_to_its_base_origin(http_get, spec, sent):
    onboard_agent._discover_openapi({"connection_string": spec, "connection_config": {
        "base_url": BASE, "token": TOKEN, "auth_scheme": "bearer"}})
    assert (TOKEN in http_get[0][1]) is sent, (spec, http_get)


def test_the_openapi_fetch_respects_the_base_path():
    """A base URL with a path only covers that path's subtree."""
    assert onboard_agent._token_target_matches("https://api.example.com/v1",
                                               "https://api.example.com/v1/openapi.json")
    assert not onboard_agent._token_target_matches("https://api.example.com/v1",
                                                   "https://api.example.com/v10/openapi.json")
