# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one HTTP path for credentialed connector calls: every request connects
to an address the SSRF guard validated for it.

:func:`connection_security.validate_connection_target` resolves a host and
refuses denied addresses, but a client that then connects by hostname resolves
the name again, and that second answer is never checked (DNS rebinding). The
transports here close the gap for every request they send:

* the request's host is resolved and validated immediately before the
  connection, with the registration allow rules (loopback, RFC1918 and global
  addresses pass; link-local, the instance metadata addresses and other
  non-global ranges are refused, and a name that does not resolve is refused);
* the request is sent to the validated IP with the original ``Host`` header and
  TLS SNI name, so virtual hosting works and the server certificate is checked
  against the original hostname (``verify=False`` still turns the check off,
  for the i3X self-signed mode);
* the clients built here never follow redirects, and a redirect a caller
  follows by hand passes through the same check.

Plain ``http`` stays allowed (an internal load balancer target); any other
scheme is refused. Each original host gets its own connection pool, because a
pooled TLS connection keeps the SNI name it was opened with. When the
connection to the first validated address fails (refused or timed out), the
next validated address is tried, as a connect by name would; a failure after
connecting is never retried elsewhere. The cost is one ``getaddrinfo`` per
request, usually answered by the resolver cache.

The connection probes pin their own requests the same way and keep doing so.
"""
from __future__ import annotations

import ipaddress
import threading

import httpx

from .connection_security import ConnectionTargetError, validated_addresses

_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: Raised before any byte of the request was sent: safe to try the next address.
_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of the original URL, the pool key."""
    return url.scheme, url.raw_host.decode("ascii").lower(), url.port or _DEFAULT_PORTS.get(url.scheme, 0)


def pin_request(request: httpx.Request, connection_type: str) -> list[str]:
    """Resolve and validate ``request``'s host now and point the request at the
    first validated IP, keeping the original ``Host`` header and TLS SNI name.
    Returns every validated IP in order; raises :class:`ConnectionTargetError`
    (nothing is sent)."""
    url = request.url
    if url.scheme not in _SCHEMES:
        raise ConnectionTargetError(f"scheme '{url.scheme}' is not allowed")
    host = url.raw_host.decode("ascii")  # IDNA form; an IPv6 literal unbracketed
    port = url.port or _DEFAULT_PORTS[url.scheme]
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    ips = validated_addresses(target, connection_type=connection_type)
    if _is_ip_literal(host):
        return [host]
    if "host" not in request.headers:  # httpx sets it from the URL; keep it original
        request.headers["Host"] = url.netloc.decode("ascii")
    request.extensions = {**request.extensions, "sni_hostname": host}
    request.url = url.copy_with(host=ips[0])
    return ips


def _next_address(request: httpx.Request, original: httpx.URL, ip: str) -> None:
    if not _is_ip_literal(original.raw_host.decode("ascii")):
        request.url = original.copy_with(host=ip)


class PinnedTransport(httpx.BaseTransport):
    """Sync transport: :func:`pin_request`, then one ``httpx.HTTPTransport``
    per original host."""

    def __init__(self, connection_type: str, *, verify=True):
        self._connection_type = connection_type
        self._verify = verify
        self._pools: dict = {}
        self._lock = threading.Lock()

    def _pool(self, key) -> httpx.HTTPTransport:
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pools[key] = httpx.HTTPTransport(verify=self._verify)
            return pool

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key, original = _origin(request.url), request.url
        ips = pin_request(request, self._connection_type)
        pool = self._pool(key)
        for index, ip in enumerate(ips):
            if index:
                _next_address(request, original, ip)
            try:
                return pool.handle_request(request)
            except _CONNECT_FAILURES:
                if index == len(ips) - 1:
                    raise
        raise ConnectionTargetError("host did not resolve")  # unreachable: ips is never empty

    def close(self) -> None:
        with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            pool.close()


class AsyncPinnedTransport(httpx.AsyncBaseTransport):
    """Async transport: :func:`pin_request` in a worker thread (the lookup
    blocks), then one ``httpx.AsyncHTTPTransport`` per original host."""

    def __init__(self, connection_type: str, *, verify=True):
        self._connection_type = connection_type
        self._verify = verify
        self._pools: dict = {}
        self._lock = threading.Lock()

    def _pool(self, key) -> httpx.AsyncHTTPTransport:
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pools[key] = httpx.AsyncHTTPTransport(verify=self._verify)
            return pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import anyio
        key, original = _origin(request.url), request.url
        ips = await anyio.to_thread.run_sync(pin_request, request, self._connection_type)
        pool = self._pool(key)
        for index, ip in enumerate(ips):
            if index:
                _next_address(request, original, ip)
            try:
                return await pool.handle_async_request(request)
            except _CONNECT_FAILURES:
                if index == len(ips) - 1:
                    raise
        raise ConnectionTargetError("host did not resolve")  # unreachable: ips is never empty

    async def aclose(self) -> None:
        with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.aclose()


def client(*, connection_type: str, timeout, verify=True, **kwargs) -> "httpx.Client":
    """A sync client on :class:`PinnedTransport` that never follows redirects.
    ``httpx`` is looked up when called, like the call sites' own lazy imports."""
    import httpx as _httpx
    return _httpx.Client(transport=PinnedTransport(connection_type, verify=verify),
                         follow_redirects=False, timeout=timeout, verify=verify, **kwargs)


def async_client(*, connection_type: str, timeout, verify=True, **kwargs) -> "httpx.AsyncClient":
    """An async client on :class:`AsyncPinnedTransport` that never follows
    redirects."""
    import httpx as _httpx
    return _httpx.AsyncClient(transport=AsyncPinnedTransport(connection_type, verify=verify),
                              follow_redirects=False, timeout=timeout, verify=verify, **kwargs)


def request(method: str, url: str, *, connection_type: str, timeout, verify=True,
            **kwargs) -> "httpx.Response":
    """One pinned request on a client of its own; the body is read before the
    client closes."""
    with client(connection_type=connection_type, timeout=timeout, verify=verify) as c:
        return c.request(method, url, **kwargs)


def get(url: str, *, connection_type: str, timeout, verify=True, **kwargs) -> "httpx.Response":
    return request("GET", url, connection_type=connection_type, timeout=timeout,
                   verify=verify, **kwargs)


def post(url: str, *, connection_type: str, timeout, verify=True, **kwargs) -> "httpx.Response":
    return request("POST", url, connection_type=connection_type, timeout=timeout,
                   verify=verify, **kwargs)
