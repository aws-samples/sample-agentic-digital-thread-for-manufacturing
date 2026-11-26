# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tulip-specific discovery — the Tulip Tables API short-circuit.

Tulip publishes no consolidated OpenAPI spec, and the generic path-as-table
parse would emit API-operation pseudo-tables, so a system whose connection type
is the first-class ``Tulip (Tables API)`` routes to ``_discover_tulip``
(enumerate ``/api/v3/tables`` then per-table columns). (routing is by
the declared connection type, NOT the host — see test_rest_branch_routes_tulip.)
Fully synthetic, no network: ``pinned_http.get`` and the registration-token resolver
are stubbed. Mirrors the live schema shapes observed against a Tulip instance.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105 - offline test fixture

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.pinned_http as pinned_http  # noqa: E402


def _use_get(monkeypatch, fake):
    """Route discovery reads through ``fake(url, headers=..., params=..., timeout=...)``,
    a URL-keyed stub returning a ``_Resp``-like object."""
    def _get(url, headers, params=None):
        resp = fake(url, headers=headers, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    monkeypatch.setattr(oa, "_tulip_discover_get", _get)
from agents.connection_types import TULIP  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# Realistic Tulip shapes: a nested {"type": "..."} descriptor and a flat dataType.
_TABLES = [
    {"id": "t1", "label": "(Requirements) Order Materials"},
    {"id": "t2", "label": "(Definition) Assets"},
]
_DEFS = {
    "t1": {"label": "(Requirements) Order Materials", "columns": [
        {"name": "lzwyu_order_id", "type": {"type": "string"}},
        {"name": "reavb_qty_required", "dataType": "float"},
        {"name": "link_col", "type": {"tableLinkID": "X", "type": "tableLink"}},
    ]},
    "t2": {"label": "(Definition) Assets", "columns": [
        {"name": "vebke_name", "type": {"type": "string"}},
    ]},
}


def _fake_get(url, headers=None, params=None, timeout=None, **_kw):
    if url.endswith("/api/v3/tables"):
        return _Resp(_TABLES)
    for tid, tdef in _DEFS.items():
        if url.endswith(f"/api/v3/tables/{tid}"):
            return _Resp(tdef)
    return _Resp({}, 404)


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    monkeypatch.setattr(cr, "resolve_registered_token",
                        lambda *a, **k: ("BASE64KEYSECRET", "basic"))


# --------------------------------------------------------------------------- #
# _is_tulip host/vendor-hint detection is GONE.
# Tulip identity is now the declared connection type ("Tulip (Tables API)"), so
# the former test_is_tulip_host[*] / test_is_tulip_vendor_hint cases were removed
# — routing-by-type is covered by test_rest_branch_routes_tulip below.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# _tulip_auth_header
# --------------------------------------------------------------------------- #

def test_tulip_auth_header_schemes():
    assert oa._tulip_auth_header("TOK", "basic") == "Basic TOK"
    assert oa._tulip_auth_header("TOK", "bearer_literal") == "TOK"
    assert oa._tulip_auth_header("TOK", "bearer") == "Bearer TOK"


# --------------------------------------------------------------------------- #
# _tulip_basic_token — encode from raw key+secret
# --------------------------------------------------------------------------- #

def test_tulip_basic_token_encodes_key_and_secret():
    import base64
    tok = oa._tulip_basic_token("myKey", "mySecret")
    assert tok == base64.b64encode(b"myKey:mySecret").decode()
    # composes with the header formatter → the on-the-wire Authorization value
    assert oa._tulip_auth_header(tok, "basic") == f"Basic {tok}"


def test_tulip_basic_token_missing_field_returns_empty():
    # either field missing ⇒ "" (caller treats as no-credential), never a
    # half-encoded "key:" / ":secret" token
    assert oa._tulip_basic_token("", "s") == ""
    assert oa._tulip_basic_token("k", "") == ""
    assert oa._tulip_basic_token("  ", "s") == ""  # whitespace-only is empty too


# --------------------------------------------------------------------------- #
# _discover_tulip
# --------------------------------------------------------------------------- #

def _resolved():
    return {"base_url": "https://your-instance.tulip.co",
            "token": "BASE64KEYSECRET", "auth_scheme": "basic"}


def test_discover_tulip_happy_path(monkeypatch):
    _use_get(monkeypatch, _fake_get)
    result = oa._discover_tulip({"id": "tulip"}, _resolved())
    assert "error" not in result
    tables = result["tables"]
    assert [t["name"] for t in tables] == [
        "(Requirements) Order Materials", "(Definition) Assets"]
    t1 = tables[0]
    assert t1["table_id"] == "t1"
    assert t1["row_count"] == 0
    cols = {c["name"]: c["data_type"] for c in t1["columns"]}
    assert cols["lzwyu_order_id"] == "string"     # nested {"type": ...}
    assert cols["reavb_qty_required"] == "float"   # flat dataType
    assert cols["link_col"] == "tableLink"


def test_discover_tulip_sends_basic_auth_header(monkeypatch):
    seen = {}

    def _capture(url, headers=None, params=None, timeout=None, **_kw):
        seen[url] = headers
        return _fake_get(url, headers, params, timeout)

    _use_get(monkeypatch, _capture)
    oa._discover_tulip({"id": "tulip"}, _resolved())
    assert seen["https://your-instance.tulip.co/api/v3/tables"]["Authorization"] == \
        "Basic BASE64KEYSECRET"


def test_discover_tulip_missing_base_url_errors(monkeypatch):
    _use_get(monkeypatch, _fake_get)
    result = oa._discover_tulip({"id": "tulip"}, {"token": "X", "auth_scheme": "basic"})
    assert "error" in result and "base_url" in result["error"]


def test_discover_tulip_missing_token_errors(monkeypatch):
    _use_get(monkeypatch, _fake_get)
    monkeypatch.setattr(cr, "resolve_registered_token", lambda *a, **k: ("", "basic"))
    result = oa._discover_tulip({"id": "tulip"}, {"base_url": "https://your-instance.tulip.co"})
    assert "error" in result and "credentials" in result["error"]


def test_discover_tulip_non_list_response_errors(monkeypatch):
    _use_get(monkeypatch, lambda *a, **k: _Resp({"errorCode": "BadRequestMissingWorkspace"}))
    result = oa._discover_tulip({"id": "tulip"}, _resolved())
    assert "error" in result and "expected a list" in result["error"]


def test_discover_tulip_http_error_is_caught(monkeypatch):
    _use_get(monkeypatch, lambda *a, **k: _Resp({}, 401))
    result = oa._discover_tulip({"id": "tulip"}, _resolved())
    assert "error" in result and "Failed to list Tulip tables" in result["error"]


# --------------------------------------------------------------------------- #
# routing: the discovery dispatch routes by the DECLARED connection type
# (type-string, not host) — a "Tulip (Tables API)" system
# goes to _discover_tulip on ANY host, and a plain "REST API" system goes to
# the generic OpenAPI parse even on a tulip.co host.
# --------------------------------------------------------------------------- #

def test_rest_branch_routes_tulip(monkeypatch):
    monkeypatch.setattr(oa, "_discover_tulip", lambda e, r: {"tables": ["TULIP"]})
    monkeypatch.setattr(oa, "_discover_openapi", lambda e: {"tables": ["OPENAPI"]})
    # Routed by the TULIP connection type, on an internal (non-tulip.co) host.
    res = oa._discover_backend_result(
        "tulip", {"connection_type": TULIP}, TULIP,
        {"base_url": "https://mes.internal.example.com"})
    assert res == {"tables": ["TULIP"]}


def test_rest_branch_generic_type_on_tulip_host_stays_openapi(monkeypatch):
    monkeypatch.setattr(oa, "_discover_tulip", lambda e, r: {"tables": ["TULIP"]})
    monkeypatch.setattr(oa, "_discover_openapi", lambda e: {"tables": ["OPENAPI"]})
    # A plain REST API system on a *.tulip.co host is NOT auto-detected as
    # Tulip anymore — the clean cut means only the declared type routes to Tulip.
    res = oa._discover_backend_result(
        "generic", {"connection_type": "REST API"}, "REST API",
        {"base_url": "https://your-instance.tulip.co",
         "connection_string": "https://your-instance.tulip.co/openapi.json"})
    assert res == {"tables": ["OPENAPI"]}


def test_rest_branch_routes_generic_openapi(monkeypatch):
    monkeypatch.setattr(oa, "_discover_tulip", lambda e, r: {"tables": ["TULIP"]})
    monkeypatch.setattr(oa, "_discover_openapi", lambda e: {"tables": ["OPENAPI"]})
    res = oa._discover_backend_result(
        "i3x-pub", {"connection_type": "REST API"}, "REST API",
        {"base_url": "https://api.example.com", "connection_string": "https://api.example.com/openapi.json"})
    assert res == {"tables": ["OPENAPI"]}


# --------------------------------------------------------------------------- #
# Optional workspace id (instance-wide Service Account) — item 1.
# When set, every Tulip call uses /api/v3/w/<id>/...; when empty, requests are
# byte-identical to today's /api/v3/... path.
# --------------------------------------------------------------------------- #

def test_tulip_api_prefix_scopes_only_with_a_valid_workspace():
    assert oa._tulip_api_prefix("") == "/api/v3"
    assert oa._tulip_api_prefix("  ") == "/api/v3"
    assert oa._tulip_api_prefix("mfSabc123") == "/api/v3/w/mfSabc123"
    # an invalid id is dropped (unscoped), never injected into the path
    assert oa._tulip_api_prefix("bad/segment") == "/api/v3"
    assert oa._tulip_api_prefix("..") == "/api/v3"


def test_valid_tulip_workspace_id():
    assert oa._valid_tulip_workspace_id("mfSabc_123-ZZ")
    assert not oa._valid_tulip_workspace_id("")
    assert not oa._valid_tulip_workspace_id("has space")
    assert not oa._valid_tulip_workspace_id("a/b")
    assert not oa._valid_tulip_workspace_id("..")
    assert not oa._valid_tulip_workspace_id("x" * 65)


def test_tulip_workspace_or_error():
    assert oa._tulip_workspace_or_error("") is None          # empty = no workspace = ok
    assert oa._tulip_workspace_or_error("mfSok") is None
    assert "path segment" in (oa._tulip_workspace_or_error("bad/seg") or "")


def test_discover_tulip_scopes_paths_with_workspace_id(monkeypatch):
    seen = []

    def _capture(url, headers=None, params=None, timeout=None, **_kw):
        seen.append(url)
        # strip the /w/<id> so the canned fixtures still match the table ids
        return _fake_get(url.replace("/api/v3/w/ws1", "/api/v3"),
                         headers, params, timeout)

    _use_get(monkeypatch, _capture)
    resolved = {**_resolved(), "workspace_id": "ws1"}
    result = oa._discover_tulip({"id": "tulip"}, resolved)
    assert "error" not in result
    # the list call AND every per-table columns call carry the workspace prefix
    assert "https://your-instance.tulip.co/api/v3/w/ws1/tables" in seen
    assert any(u == "https://your-instance.tulip.co/api/v3/w/ws1/tables/t1" for u in seen)
    # nothing hit the unscoped /api/v3/tables path
    assert all("/api/v3/tables" not in u for u in seen)


def test_discover_tulip_no_workspace_is_byte_identical(monkeypatch):
    seen = []

    def _capture(url, headers=None, params=None, timeout=None, **_kw):
        seen.append(url)
        return _fake_get(url, headers, params, timeout)

    _use_get(monkeypatch, _capture)
    oa._discover_tulip({"id": "tulip"}, _resolved())  # no workspace_id
    assert "https://your-instance.tulip.co/api/v3/tables" in seen
    assert all("/w/" not in u for u in seen)


def test_discover_tulip_rejects_malformed_workspace_id(monkeypatch):
    _use_get(monkeypatch, _fake_get)
    result = oa._discover_tulip({"id": "tulip"}, {**_resolved(), "workspace_id": "bad/seg"})
    assert "error" in result and "path segment" in result["error"]
