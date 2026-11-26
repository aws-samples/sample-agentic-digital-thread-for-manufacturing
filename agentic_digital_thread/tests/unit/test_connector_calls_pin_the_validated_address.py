# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Credentialed connector calls connect only to an address the SSRF guard
validated.

Registration checks a target with
``connection_security.validate_connection_target``, but a client that later
connects by hostname resolves the name a second time, and that answer is
never checked: a name that was safe at registration can answer a link-local or
metadata address by the time discovery or a runtime read runs, and the stored
token goes there. Every connector client family is driven here against
offline fakes (``tests/unit/_pinned_wire.py``): a resolver that can change its
answer, and a wire that records what each request would connect to.

The rules pinned for every family (OpenAPI spec fetch, Tulip discovery and
runtime, MCP discovery and sampling, i3X discovery and runtime, the REST query
path, the MCP runtime client factory):

* the request connects to the validated IP, with the original ``Host`` header
  and TLS SNI name (so virtual hosting works and the certificate is checked
  against the original hostname);
* when the name re-resolves to a denied address after validation, nothing
  connects to that address or by name;
* redirects are not followed;
* private targets stay allowed (loopback, RFC1918, plain ``http`` to an
  internal load balancer), and the i3X self-signed mode still turns the
  certificate check off;
* the built-in HighByte MCP session still reopens after a hub restart, on the
  address the name answers then.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105 - offline test fixture

from agents import onboard_agent as oa  # noqa: E402
from agents import query_router as qr  # noqa: E402
from agents import system_agent as sa  # noqa: E402
from agents.connection_security import validate_connection_target  # noqa: E402
from agents.i3x_envelope import make_i3x_client  # noqa: E402
from agents.response_integrity_query import capped_mcp_http_client_factory  # noqa: E402
from tests.unit import _fake_mcp_hub as fh  # noqa: E402
from tests.unit._pinned_wire import CERT_NONE, CERT_REQUIRED, Resolver, Wire  # noqa: E402

PRIVATE = "10.0.12.34"        # an in-VPC target (RFC1918, allowed)
OTHER = "10.0.77.88"          # a second allowed address
DENIED = "169.254.169.254"    # the instance metadata address
TOKEN = "test-only-token-0123"  # nosec B105 - offline test fixture

_SPEC = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
         "paths": {"/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}}}


def _universal(request: httpx.Request) -> httpx.Response:
    """Answers every family's first requests with a well-formed body."""
    path = request.url.path
    if request.method == "POST" and request.content:
        message = json.loads(request.content)
        method, mid = message.get("method"), message.get("id")
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s1"}, json={
                "jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"}}})
        if "id" not in message:
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid,
                                             "result": {"tools": []}})
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "{}"}], "isError": False}})
        return httpx.Response(200, json={"success": True, "result": []})
    if path.endswith("openapi.json"):
        return httpx.Response(200, json=_SPEC)
    if path == "/api/v3/tables":
        return httpx.Response(200, json=[{"id": "t1", "label": "Orders"}])
    if path.startswith("/api/v3/tables/"):
        return httpx.Response(200, json={"columns": [{"name": "c", "dataType": "string"}]})
    if path.startswith("/v1/"):
        return httpx.Response(200, json={"success": True, "result": []})
    return httpx.Response(200, json={"ok": 1})


def _bearer() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
            "Accept": "application/json"}


def _openapi(host):
    return oa._discover_openapi({
        "connection_string": f"https://{host}/openapi.json",
        "connection_config": {"base_url": f"https://{host}", "token": TOKEN,
                              "auth_scheme": "bearer"}})


def _tulip_discovery(host):
    return oa._discover_tulip({"id": ""}, {"base_url": f"https://{host}",
                                           "token": TOKEN, "auth_scheme": "basic"})


def _mcp_discovery(host):
    return oa._mcp_list_tools(f"https://{host}/mcp", _bearer())


def _mcp_sample(host):
    return oa._sample_mcp_tool(f"https://{host}/mcp", _bearer(), "NameSpace_MCP")


def _i3x_discovery(host):
    get, _post = make_i3x_client(f"https://{host}", TOKEN, "bearer")
    return get("/namespaces")


def _i3x_runtime(host):
    return sa._I3xClient(f"https://{host}", "", TOKEN, "bearer").request("GET", "namespaces", {})


def _tulip_runtime(host):
    return sa._TulipClient(f"https://{host}", token=TOKEN, auth_scheme="basic").get("api/v3/tables")


def _rest_runtime(host):
    resolved = {"base_url": f"https://{host}", "token": TOKEN, "auth_scheme": "bearer"}
    with patch.object(qr, "resolve_connection", return_value=resolved):
        return qr._query_openapi("sys-rest", "GET /widgets")


def _mcp_runtime(host, scheme="https"):
    async def go():
        client = capped_mcp_http_client_factory(headers={"Authorization": f"Bearer {TOKEN}"})
        async with client:
            response = await client.post(f"{scheme}://{host}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return response.status_code
    return asyncio.run(go())


FAMILIES = {
    "openapi-spec-fetch": _openapi,
    "tulip-discovery": _tulip_discovery,
    "mcp-discovery": _mcp_discovery,
    "mcp-sampling": _mcp_sample,
    "i3x-discovery": _i3x_discovery,
    "i3x-runtime": _i3x_runtime,
    "tulip-runtime": _tulip_runtime,
    "rest-runtime": _rest_runtime,
    "mcp-runtime": _mcp_runtime,
}
HOST = "connector.example.com"


def _run(call, *args, **kwargs):
    """The family's outcome, or the exception it raised (some families raise,
    some return an error dict; the wire is what is asserted)."""
    try:
        return call(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the wire is the assertion
        return exc


@pytest.fixture
def resolver(monkeypatch):
    return Resolver().install(monkeypatch)


@pytest.fixture
def wire(monkeypatch):
    return Wire(_universal).install(monkeypatch)


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_each_request_connects_to_the_validated_address(family, resolver, wire):
    resolver.set(HOST, PRIVATE)
    _run(FAMILIES[family], HOST)
    assert wire.seen, "no request was sent"
    for seen in wire.seen:
        assert seen.connect_host == PRIVATE, seen
        assert seen.host_header == HOST, seen
        assert seen.sni == HOST, seen


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_a_name_that_rebinds_after_validation_never_reaches_the_new_address(
        family, resolver, wire):
    resolver.set(HOST, PRIVATE, DENIED)
    # The registration-time check sees the safe answer ...
    assert validate_connection_target(f"https://{HOST}/", connection_type="test") == PRIVATE
    # ... then the name answers the metadata address for every later lookup.
    _run(FAMILIES[family], HOST)
    reached = [seen.connect_host for seen in wire.seen]
    assert all(host == PRIVATE for host in reached), reached


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_a_redirect_is_not_followed(family, resolver, monkeypatch):
    resolver.set(HOST, PRIVATE)
    resolver.set("elsewhere.example.com", OTHER)

    def redirect(request):
        return httpx.Response(307, headers={"location": "https://elsewhere.example.com/x"})

    wire = Wire(redirect).install(monkeypatch)
    _run(FAMILIES[family], HOST)
    assert len(wire.seen) == 1, [seen.url for seen in wire.seen]


@pytest.mark.parametrize("family", ["i3x-discovery", "i3x-runtime"])
def test_the_i3x_self_signed_mode_still_skips_the_certificate_check(
        family, resolver, wire, monkeypatch):
    monkeypatch.setenv("I3X_TLS_VERIFY", "false")
    resolver.set(HOST, PRIVATE)
    _run(FAMILIES[family], HOST)
    assert wire.seen
    assert all(seen.verify_mode == CERT_NONE for seen in wire.seen), wire.seen
    assert all(seen.connect_host == PRIVATE and seen.sni == HOST for seen in wire.seen)


@pytest.mark.parametrize("family", ["i3x-discovery", "i3x-runtime", "tulip-runtime",
                                    "mcp-runtime"])
def test_the_certificate_is_checked_by_default(family, resolver, wire, monkeypatch):
    monkeypatch.delenv("I3X_TLS_VERIFY", raising=False)
    resolver.set(HOST, PRIVATE)
    _run(FAMILIES[family], HOST)
    assert wire.seen
    assert all(seen.verify_mode == CERT_REQUIRED for seen in wire.seen), wire.seen


def test_a_loopback_literal_target_stays_allowed(resolver, wire):
    _run(_tulip_runtime, "127.0.0.1:8443")
    assert [seen.connect_host for seen in wire.seen] == ["127.0.0.1"]
    assert wire.seen[0].host_header == "127.0.0.1:8443"


def test_plain_http_to_an_internal_load_balancer_stays_allowed(resolver, wire):
    resolver.set("internal-hub.example.com", PRIVATE)
    assert _run(_mcp_runtime, "internal-hub.example.com:45245", scheme="http") == 200
    assert [seen.connect_host for seen in wire.seen] == [PRIVATE]
    assert wire.seen[0].host_header == "internal-hub.example.com:45245"
    assert wire.seen[0].url.startswith(f"http://{PRIVATE}:45245/")


def test_the_credentials_ride_the_pinned_request(resolver, wire):
    resolver.set(HOST, PRIVATE)
    _run(_i3x_runtime, HOST)
    assert wire.seen[0].headers.get("authorization") == f"Bearer {TOKEN}"


# --------------------------------------------------------------------------- #
# The built-in HighByte MCP client and its session reopen
# --------------------------------------------------------------------------- #
HUB = "hb.example.com"


def _hub_tools(monkeypatch, hub):
    wire = Wire(hub.handle).install(monkeypatch)
    cfg = {"url": f"https://{HUB}/mcp", "token": fh.KEY, "auth_scheme": "bearer"}
    tools, _ = sa._system_tools("highbyte", "mcp", "", None, "MCP Server", cfg)
    return wire, {t.tool_name: t for t in tools}


def _call(tool, tool_use_id: str) -> dict:
    return asyncio.run(tool.mcp_client.call_tool_async(
        tool_use_id=tool_use_id, name=tool.mcp_tool.name, arguments={}))


def _stop(tools: dict) -> None:
    for tool in tools.values():
        try:
            tool.mcp_client.stop(None, None, None)
        except RuntimeError:
            pass  # stop() re-raises why a session closed, after cleaning up
        break


def test_the_highbyte_session_reopens_on_the_address_the_name_answers_then(
        monkeypatch, resolver):
    resolver.set(HUB, PRIVATE)
    hub = fh.FakeHub(accept="bearer", config_tools=False)
    wire, tools = _hub_tools(monkeypatch, hub)
    try:
        read = tools["NameSpace_MCP"]
        assert _call(read, "before")["status"] == "success"
        # The hub task is replaced: every session is gone and the name now
        # answers the new task's address.
        hub.restart()
        resolver.set(HUB, OTHER)
        after = _call(read, "after")
        assert after["status"] == "success", after
        assert hub.calls == ["NameSpace_MCP", "NameSpace_MCP"]
    finally:
        _stop(tools)
    reached = {seen.connect_host for seen in wire.seen}
    assert reached == {PRIVATE, OTHER}, reached
    assert all(seen.host_header == HUB and seen.sni == HUB for seen in wire.seen)


def test_a_highbyte_reopen_onto_a_denied_address_fails_closed(monkeypatch, resolver):
    resolver.set(HUB, PRIVATE)
    hub = fh.FakeHub(accept="bearer", config_tools=False)
    wire, tools = _hub_tools(monkeypatch, hub)
    try:
        read = tools["NameSpace_MCP"]
        assert _call(read, "before")["status"] == "success"
        hub.restart()
        resolver.set(HUB, DENIED)
        after = _call(read, "after")
        assert after["status"] == "error", after
    finally:
        _stop(tools)
    reached = [seen.connect_host for seen in wire.seen]
    assert all(host == PRIVATE for host in reached), reached
