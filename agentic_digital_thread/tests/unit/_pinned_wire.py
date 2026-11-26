# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline wire and resolver fakes for the connector address-pinning tests.

:class:`Resolver` stands in for ``socket.getaddrinfo``: each host answers from
its own list of addresses, one per lookup, the last one repeating, so a test
can make a name answer one address when it is validated and another one later
(DNS rebinding). An IP literal answers itself; an unknown name does not
resolve.

:class:`Wire` replaces the request handler of httpx's own socket transports
(``httpx.HTTPTransport`` and ``httpx.AsyncHTTPTransport``), the layer that
would open the socket. It records what each request would connect to (the URL
host), the ``Host`` header, the TLS SNI name and the transport's certificate
check, then answers through ``handler``. No socket opens.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from typing import Callable

import httpx


class Resolver:
    def __init__(self, answers: dict[str, list[str]] | None = None):
        self.answers = {host: list(ips) for host, ips in (answers or {}).items()}
        self.asked: list[str] = []

    def set(self, host: str, *ips: str) -> None:
        self.answers[host] = list(ips)

    def __call__(self, host, port=None, *args, **kwargs):
        host = host.decode() if isinstance(host, bytes) else str(host)
        self.asked.append(host)
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            ips = self.answers.get(host)
            if not ips:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            ip = ips.pop(0) if len(ips) > 1 else ips[0]
        port = int(port or 0)
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def install(self, monkeypatch) -> "Resolver":
        monkeypatch.setattr(socket, "getaddrinfo", self)
        return self


@dataclass
class Seen:
    method: str
    url: str
    connect_host: str
    host_header: str
    sni: str | None
    verify_mode: object
    transport_id: int
    headers: dict = field(default_factory=dict)


def _verify_mode(transport) -> object:
    pool = getattr(transport, "_pool", None)
    context = getattr(pool, "_ssl_context", None)
    return getattr(context, "verify_mode", None)


class Wire:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response] | None = None):
        self.handler = handler or (lambda request: httpx.Response(200, json={}))
        self.seen: list[Seen] = []

    def _record(self, transport, request: httpx.Request) -> httpx.Response:
        self.seen.append(Seen(
            method=request.method, url=str(request.url),
            connect_host=request.url.host,
            host_header=request.headers.get("host", ""),
            sni=request.extensions.get("sni_hostname"),
            verify_mode=_verify_mode(transport), transport_id=id(transport),
            headers=dict(request.headers)))
        return self.handler(request)

    def install(self, monkeypatch) -> "Wire":
        wire = self

        def handle_request(transport, request):
            return wire._record(transport, request)

        async def handle_async_request(transport, request):
            return wire._record(transport, request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                            handle_async_request)
        return self


CERT_NONE = ssl.CERT_NONE
CERT_REQUIRED = ssl.CERT_REQUIRED
