# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pinned HTTP path (``agents/pinned_http.py``) on its own: per-request
resolution and validation, the rewrite to the validated IP with the original
``Host`` and TLS SNI, one connection pool per original host, no redirects, and
the MCP client factory built on it. Offline: ``tests/unit/_pinned_wire.py``.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from agents import pinned_http
from agents.connection_security import ConnectionTargetError
from agents.response_integrity_query import (
    MAX_QUERY_RESULT_BYTES,
    capped_mcp_http_client_factory,
)
from tests.unit._pinned_wire import CERT_NONE, CERT_REQUIRED, Resolver, Wire

A, B = "10.1.0.10", "10.1.0.20"
V6 = "2600:1f14::10"
PUBLIC = "52.94.236.248"


@pytest.fixture
def resolver(monkeypatch):
    return Resolver().install(monkeypatch)


@pytest.fixture
def wire(monkeypatch):
    return Wire().install(monkeypatch)


def _get(url, **kwargs):
    return pinned_http.get(url, connection_type="test", timeout=5.0, **kwargs)


def test_the_request_goes_to_the_validated_ip_with_the_original_host_and_sni(resolver, wire):
    resolver.set("api.example.com", A)
    assert _get("https://api.example.com:8443/v1/x?q=1").status_code == 200
    (seen,) = wire.seen
    assert seen.url == f"https://{A}:8443/v1/x?q=1"
    assert seen.host_header == "api.example.com:8443"
    assert seen.sni == "api.example.com"
    assert seen.verify_mode == CERT_REQUIRED


def test_a_public_address_is_allowed(resolver, wire):
    resolver.set("api.example.com", PUBLIC)
    _get("https://api.example.com/")
    assert wire.seen[0].connect_host == PUBLIC


def test_an_ipv6_answer_is_bracketed_in_the_url(resolver, wire):
    resolver.set("api.example.com", V6)
    _get("https://api.example.com/x")
    assert wire.seen[0].url == f"https://[{V6}]/x"
    assert wire.seen[0].host_header == "api.example.com"


def test_an_ip_literal_target_is_validated_and_sent_unchanged(resolver, wire):
    _get(f"http://{A}:8080/x")
    (seen,) = wire.seen
    assert seen.url == f"http://{A}:8080/x"
    assert seen.sni is None
    with pytest.raises(ConnectionTargetError):
        _get("http://169.254.169.254/latest/meta-data/")
    assert len(wire.seen) == 1


@pytest.mark.parametrize("denied", ["169.254.169.254", "fe80::1", "fd00:ec2::254",
                                    "::ffff:169.254.169.254", "100.64.0.1"])
def test_a_denied_answer_at_request_time_fails_closed(denied, resolver, wire):
    resolver.set("api.example.com", denied)
    with pytest.raises(ConnectionTargetError):
        _get("https://api.example.com/")
    assert wire.seen == []


def test_a_name_that_does_not_resolve_fails_closed(resolver, wire):
    with pytest.raises(ConnectionTargetError):
        _get("https://nowhere.example.com/")
    assert wire.seen == []


def test_each_request_resolves_again(resolver, wire):
    resolver.set("api.example.com", A, B)
    with pinned_http.client(connection_type="test", timeout=5.0) as client:
        client.get("https://api.example.com/1")
        client.get("https://api.example.com/2")
    assert [seen.connect_host for seen in wire.seen] == [A, B]
    assert resolver.asked.count("api.example.com") == 2


def test_two_names_on_one_address_never_share_a_connection_pool(resolver, wire):
    """A pooled TLS connection carries the SNI name it was opened with, so two
    hostnames behind one IP get separate pools."""
    resolver.set("one.example.com", A)
    resolver.set("two.example.com", A)
    with pinned_http.client(connection_type="test", timeout=5.0) as client:
        client.get("https://one.example.com/")
        client.get("https://two.example.com/")
        client.get("https://one.example.com/again")
    first, second, third = wire.seen
    assert first.transport_id != second.transport_id
    assert first.transport_id == third.transport_id
    assert (first.sni, second.sni) == ("one.example.com", "two.example.com")


def test_redirects_are_never_followed(resolver, monkeypatch):
    resolver.set("api.example.com", A)
    resolver.set("elsewhere.example.com", B)
    wire = Wire(lambda request: httpx.Response(
        302, headers={"location": "https://elsewhere.example.com/"})).install(monkeypatch)
    with pinned_http.client(connection_type="test", timeout=5.0) as client:
        assert client.follow_redirects is False
        assert client.get("https://api.example.com/").status_code == 302
    assert [seen.connect_host for seen in wire.seen] == [A]


def test_verify_false_turns_the_certificate_check_off(resolver, wire):
    resolver.set("i3x.example.com", A)
    _get("https://i3x.example.com/", verify=False)
    assert wire.seen[0].verify_mode == CERT_NONE
    assert wire.seen[0].sni == "i3x.example.com"


def test_a_scheme_other_than_http_or_https_is_refused(resolver, wire):
    transport = pinned_http.PinnedTransport("test")
    with pytest.raises(ConnectionTargetError):
        transport.handle_request(httpx.Request("GET", "ftp://api.example.com/x"))
    assert wire.seen == []


def test_every_validated_address_is_returned_in_resolver_order(monkeypatch):
    from agents.connection_security import validated_addresses
    _answers(monkeypatch, A, A, B)
    assert validated_addresses("api.example.com:443", connection_type="test") == [A, B]
    _answers(monkeypatch, A, "169.254.169.254")
    with pytest.raises(ConnectionTargetError):
        validated_addresses("api.example.com:443", connection_type="test")


def _answers(monkeypatch, *ips):
    import socket

    def answers(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port or 0))) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", answers)


def test_the_next_validated_address_is_tried_when_the_first_refuses(monkeypatch):
    _answers(monkeypatch, A, B)

    def handler(request):
        if request.url.host == A:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    wire = Wire(handler).install(monkeypatch)
    assert _get("https://api.example.com/x").status_code == 200
    assert [seen.connect_host for seen in wire.seen] == [A, B]
    assert all(seen.sni == "api.example.com" and seen.host_header == "api.example.com"
               for seen in wire.seen)


def test_a_failure_after_connecting_is_never_retried_on_another_address(monkeypatch):
    _answers(monkeypatch, A, B)

    def handler(request):
        raise httpx.ReadTimeout("no answer", request=request)

    wire = Wire(handler).install(monkeypatch)
    with pytest.raises(httpx.ReadTimeout):
        _get("https://api.example.com/x")
    assert [seen.connect_host for seen in wire.seen] == [A]


def test_the_last_connect_error_surfaces_when_every_address_refuses(monkeypatch):
    _answers(monkeypatch, A, B)

    def handler(request):
        raise httpx.ConnectError(f"refused by {request.url.host}", request=request)

    wire = Wire(handler).install(monkeypatch)
    with pytest.raises(httpx.ConnectError, match=B):
        _get("https://api.example.com/x")
    assert [seen.connect_host for seen in wire.seen] == [A, B]


def test_the_async_transport_also_tries_the_next_validated_address(monkeypatch):
    _answers(monkeypatch, A, B)

    def handler(request):
        if request.url.host == A:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(200)

    wire = Wire(handler).install(monkeypatch)

    async def go():
        async with pinned_http.async_client(connection_type="test", timeout=5.0) as client:
            return (await client.get("https://mcp.example.com/mcp")).status_code

    assert asyncio.run(go()) == 200
    assert [seen.connect_host for seen in wire.seen] == [A, B]


def test_the_async_client_pins_each_request(resolver, wire):
    resolver.set("mcp.example.com", A, B)

    async def go():
        async with pinned_http.async_client(connection_type="test", timeout=5.0) as client:
            assert client.follow_redirects is False
            await client.post("https://mcp.example.com/mcp", json={})
            await client.post("https://mcp.example.com/mcp", json={})

    asyncio.run(go())
    assert [seen.connect_host for seen in wire.seen] == [A, B]
    assert all(seen.sni == "mcp.example.com" for seen in wire.seen)


def test_the_async_client_fails_closed_on_a_denied_answer(resolver, wire):
    resolver.set("mcp.example.com", "169.254.169.254")

    async def go():
        async with pinned_http.async_client(connection_type="test", timeout=5.0) as client:
            await client.post("https://mcp.example.com/mcp", json={})

    with pytest.raises(ConnectionTargetError):
        asyncio.run(go())
    assert wire.seen == []


def test_the_mcp_factory_is_pinned_unredirected_and_still_byte_capped(resolver, monkeypatch):
    resolver.set("mcp.example.com", A)
    wire = Wire(lambda request: httpx.Response(
        200, content=b"x" * (MAX_QUERY_RESULT_BYTES + 1))).install(monkeypatch)

    async def go():
        client = capped_mcp_http_client_factory(headers={"Authorization": "Bearer t"})
        async with client:
            assert client.follow_redirects is False
            async with client.stream("POST", "https://mcp.example.com/mcp", json={}) as response:
                async for _chunk in response.aiter_bytes():
                    pass

    with pytest.raises(ValueError, match="exceeds connector limit"):
        asyncio.run(go())
    assert [seen.connect_host for seen in wire.seen] == [A]
    assert wire.seen[0].headers["authorization"] == "Bearer t"
