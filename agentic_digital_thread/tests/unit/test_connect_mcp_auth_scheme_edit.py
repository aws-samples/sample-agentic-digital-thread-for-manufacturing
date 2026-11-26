# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user MCP system's Authorization header can be chosen in its Edit form.

The Connect New System form takes an MCP token and the server sends it as
``Authorization: Bearer <token>`` (or as entered when it already starts with
``Bearer ``). A server that wants ``X-API-Key`` or the raw value had no way to
say so from the UI. The Edit form now offers "Authorization header" (Bearer
token / X-API-Key / token as entered), which writes the existing
``PUT /api/admin/connection`` ``auth_scheme`` field. Backend half:
``GET /api/admin/connection/{id}`` reports the scheme the system is sent
with (so the form shows it), the PUT accepts exactly those three for an MCP
Server, and the chosen scheme reaches discover. Offline.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    _admin_update, deployed)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402
    MCP_URL, USER_TOKEN, _register_mcp)


class _HeaderSpy:
    calls: list = []

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        _HeaderSpy.calls.append((url, dict(headers or {})))
        raise RuntimeError("stop after the first request")


@pytest.fixture
def headers_sent(monkeypatch):
    import httpx
    _HeaderSpy.calls = []
    monkeypatch.setattr(httpx, "Client", _HeaderSpy)
    return _HeaderSpy.calls


def _describe() -> dict:
    from agents.catalog import catalog
    return admin_routes._describe_connection_for(SID, catalog.get(SID))


def _discover():
    body = DiscoverRequest(system_id=SID, connection_type="MCP Server", connection_string="")
    return asyncio.run(onboard._discover_mcp_server(body, SID, "", "", "MCP Server"))


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


@pytest.mark.parametrize("token, scheme", [
    (USER_TOKEN, "bearer"), ("Bearer " + USER_TOKEN, "bearer_literal")])
def test_the_edit_form_reads_the_scheme_the_token_is_sent_with(cat, secrets, graph, token,
                                                               scheme):
    assert _register_mcp(token).get("status") == "created"
    assert _describe()["auth_scheme"] == scheme


@pytest.mark.parametrize("scheme, header", [
    ("api_key", ("X-API-Key", USER_TOKEN)),
    ("bearer_literal", ("Authorization", USER_TOKEN)),
    ("bearer", ("Authorization", f"Bearer {USER_TOKEN}")),
])
def test_an_edit_round_trips_the_scheme_to_the_form_and_to_discover(
        cat, secrets, graph, deployed, headers_sent, scheme, header):
    assert _register_mcp().get("status") == "created"
    res = _admin_update(SID, {"auth_scheme": scheme})
    assert _body(res).get("status") == "ok", _body(res)
    assert _describe()["auth_scheme"] == scheme
    _discover()
    assert headers_sent, "discover sent nothing"
    url, sent = headers_sent[0]
    assert url.startswith(MCP_URL.rsplit("/", 1)[0]), url
    name, value = header
    assert sent.get(name) == value, sent
    if name == "X-API-Key":
        assert "Authorization" not in sent, sent


def test_an_unknown_mcp_scheme_is_refused_and_nothing_changes(cat, secrets, graph, deployed):
    assert _register_mcp().get("status") == "created"
    res = _admin_update(SID, {"auth_scheme": "digest"})
    assert getattr(res, "status_code", None) == 400, _body(res)
    detail = _body(res)["detail"]
    assert "bearer" in detail and "api_key" in detail and "bearer_literal" in detail, detail
    assert SID not in graph
    assert _describe()["auth_scheme"] == "bearer"


def test_guard_a_demo_mcp_reports_its_node_scheme_only(cat, secrets, graph):
    cat.add({"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "registered", "connection_origin": "demo"})
    graph["highbyte"] = {"connection_config": json.dumps({"url": MCP_URL}), "secret_arn": "",
                         "connection_type": "MCP Server"}
    out = admin_routes._describe_connection_for("highbyte", cat.get("highbyte"))
    assert out.get("auth_scheme", "") == "", out


def test_guard_the_describe_never_carries_a_secret_value(cat, secrets, graph):
    _register_mcp()
    assert USER_TOKEN not in json.dumps(_describe())
