# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user-registered MCP token goes out as a Bearer token by default.

A Connect New System MCP registration stores its token in its own
``adt/conn/<id>`` secret and no auth scheme. Every reader then defaulted the
scheme to ``bearer_literal`` (the header value is the stored text verbatim),
so a bare token arrived as ``Authorization: <token>`` with no ``Bearer ``.
Now, for a USER-registered MCP system with a token and no auth scheme of its
own, the registration's connection (``registration_connection``: discover,
preflight, provision, the admin Edit) carries ``bearer``, or ``bearer_literal``
when the token already starts with ``Bearer `` (never ``Bearer Bearer``).
Provision persists that scheme on the node as it always persisted one.
Demo systems and any auth scheme already set (a node's, an Edit's) are
untouched; the HighByte bootstrap pair is sent as Bearer
(``agents/builtin_highbyte.py``). Offline.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
from agents.connection_probes import _mcp_auth_headers  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    HB_TOKEN, HB_URL, MCP_URL, _add_demo_mcp, _mcp_discover, _provision_mcp,
    _register_mcp, highbyte, httpx_spy)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402

BARE = "bare-token-123"  # nosec B105 - fixture
PREFIXED = "Bearer jwt.abc.def"  # nosec B105 - fixture


def _auth_sent(httpx_spy) -> str:
    assert httpx_spy.calls, "no request was sent"
    return httpx_spy.calls[0][1]


# ── discover ───────────────────────────────────────────────────────────────

def test_a_bare_user_token_is_sent_as_a_bearer_token(cat, secrets, httpx_spy):
    _register_mcp(token=BARE)
    _mcp_discover(SID, "")
    assert _auth_sent(httpx_spy) == f"Bearer {BARE}"


def test_a_token_that_already_says_bearer_is_not_prefixed_again(cat, secrets, httpx_spy):
    _register_mcp(token=PREFIXED)
    _mcp_discover(SID, "")
    assert _auth_sent(httpx_spy) == PREFIXED


@pytest.mark.parametrize("token", ["bearer lower.case", "BEARER UPPER"])
def test_the_bearer_check_ignores_case(cat, secrets, httpx_spy, token):
    _register_mcp(token=token)
    _mcp_discover(SID, "")
    assert _auth_sent(httpx_spy) == token


# ── registration connection (preflight, Edit) and provision ───────────────

def test_the_registration_connection_carries_the_default(cat, secrets):
    _register_mcp(token=BARE)
    reg = cr.registration_connection(SID, cat.get(SID))
    assert reg["auth_scheme"] == "bearer", reg
    assert _mcp_auth_headers(reg["token"], reg["auth_scheme"])["Authorization"] == \
        f"Bearer {BARE}"


def test_provision_gives_the_runtime_and_the_node_the_bearer_scheme(cat, secrets, monkeypatch):
    _register_mcp(token=BARE)
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert result["status"] == "provisioned", result
    cfg = started[-1]["connection_config"]
    assert (cfg["token"], cfg["auth_scheme"]) == (BARE, "bearer"), cfg
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["non_secret"]["auth_scheme"] == "bearer", node


def test_guard_a_tokenless_user_mcp_gets_no_scheme_and_no_header(cat, secrets, httpx_spy):
    _register_mcp(token=None)
    assert "auth_scheme" not in cr.registration_connection(SID, cat.get(SID))
    _mcp_discover(SID, "")
    assert _auth_sent(httpx_spy) == ""


# ── guards: anything already set, HighByte, demo ───────────────────────────

@pytest.mark.parametrize("scheme, header", [
    ("bearer_literal", ("Authorization", BARE)),
    ("api_key", ("X-API-Key", BARE)),
    ("bearer", ("Authorization", f"Bearer {BARE}")),
])
def test_guard_a_scheme_on_the_node_is_kept(cat, secrets, monkeypatch, httpx_spy, scheme, header):
    """A provisioned (or admin-edited) user MCP: the node's scheme wins, as
    before — including ``bearer_literal`` that provision wrote before this
    change."""
    _register_mcp(token=BARE)
    node = _node_row({"url": MCP_URL, "auth_scheme": scheme}, CONN_ARN, "MCP Server")
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node if sid == SID else None)
    reg = cr.registration_connection(SID, cat.get(SID))
    assert reg["auth_scheme"] == scheme
    key, value = header
    assert _mcp_auth_headers(reg["token"], reg["auth_scheme"])[key] == value


def test_guard_an_explicit_bearer_with_a_prefixed_token_is_byte_identical(cat, secrets,
                                                                          monkeypatch):
    _register_mcp(token=PREFIXED)
    node = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node if sid == SID else None)
    reg = cr.registration_connection(SID, cat.get(SID))
    assert _mcp_auth_headers(reg["token"], reg["auth_scheme"])["Authorization"] == \
        f"Bearer {PREFIXED}"


def test_guard_highbyte_discover_is_unchanged(cat, secrets, highbyte, httpx_spy):
    _add_demo_mcp(cat)
    from agents import onboard
    entry = onboard._mcp_discover_entry("highbyte", "")
    # The HighByte bootstrap pair is sent as Bearer (agents/builtin_highbyte.py).
    assert entry["connection_config"] == {"url": f"{HB_URL}/mcp", "token": HB_TOKEN,
                                          "auth_scheme": "bearer"}


def test_guard_a_demo_mcp_provision_is_unchanged(cat, secrets, monkeypatch):
    from tests.unit import test_onboard_provision_node_write as h
    started: list = []
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: started.append(kw) or "mcp://test-agent")
    entry = {"id": "demo-mcp", "name": "Demo", "connection_type": "MCP Server", "schema": "",
             "status": "registered", "connection_origin": "demo",
             "connection_config": {"url": "https://demo.example.com/mcp", "token": "t"},
             "_provision": {"system_id": "demo-mcp", "system_name": "Demo", "plant": "",
                            "connection_type": "MCP Server",
                            "connection_string": "https://demo.example.com/mcp"}}
    result, calls = h._run_provision(entry)
    cfg = started[-1]["connection_config"]
    assert "auth_scheme" not in cfg or cfg["auth_scheme"] != "bearer", cfg
    assert json.dumps(calls[-1]["non_secret"], sort_keys=True) == json.dumps(
        {"auth_scheme": "bearer", "connection_type": "MCP Server",
         "url": "https://demo.example.com/mcp"}, sort_keys=True)
