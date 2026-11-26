# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two endpoint URLs are the same only when they name the same endpoint.

``connection_resolver.normalize_endpoint_url`` rebuilt the host without its
IPv6 brackets, so ``https://[2606:4700:4700::1111:8443]/mcp`` (one IPv6 host,
default port) and ``https://[2606:4700:4700::1111]:8443/mcp`` (another host,
port 8443) compared equal, and an MCP registration could carry both beside
one token. It also dropped an explicit port 0 (``if port``) and treated
``/mcp`` and ``/mcp/`` as one resource. ``onboard_agent._token_target_matches``
(the REST OpenAPI token scope) compared the same broken string. Now both go
through ``endpoint_identity``: scheme, host (IPv6 kept bracketed, IDNA),
effective port (``port is not None``), the exact path (an empty path is
``/``) and the query; only the fragment is ignored. Offline.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
from tests.unit.test_connect_mcp_one_url_registration import (  # noqa: E402,F401
    _mcp, _nothing_stored, guard)
from tests.unit.test_connect_new_system_custom_types import cat, secrets  # noqa: E402,F401

V6_DEFAULT = "https://[2606:4700:4700::1111:8443]/mcp"
V6_8443 = "https://[2606:4700:4700::1111]:8443/mcp"

DIFFERENT = [
    (V6_DEFAULT, V6_8443, "IPv6 host vs another IPv6 host on a non-default port"),
    ("https://mcp.example.com:0/mcp", "https://mcp.example.com/mcp", "explicit port 0"),
    ("http://mcp.example.com/mcp", "https://mcp.example.com/mcp", "scheme"),
    ("https://mcp.example.com:8443/mcp", "https://mcp.example.com:9443/mcp", "non-default port"),
    ("https://mcp.example.com/mcp", "https://mcp.example.com/api", "path"),
    ("https://mcp.example.com/mcp", "https://mcp.example.com/mcp/", "trailing slash"),
    ("https://mcp.example.com/mcp?x=1", "https://mcp.example.com/mcp", "query"),
    ("http://mcp.example.com:443/mcp", "https://mcp.example.com/mcp", "port 443 on http"),
]
SAME = [
    ("HTTPS://MCP.Example.COM/mcp", "https://mcp.example.com/mcp", "case of scheme and host"),
    ("https://mcp.example.com:443/mcp", "https://mcp.example.com/mcp", "explicit default port"),
    ("https://mcp.example.com/mcp#frag", "https://mcp.example.com/mcp", "fragment"),
    ("https://mcp.example.com", "https://mcp.example.com/", "empty path is /"),
    ("https://[2606:4700:4700::1111]:443/mcp", "https://[2606:4700:4700::1111]/mcp",
     "IPv6 with explicit default port"),
    ("https://bücher.example/mcp", "https://xn--bcher-kva.example/mcp", "IDNA host"),
]


@pytest.mark.parametrize("a, b, why", DIFFERENT, ids=[c[2] for c in DIFFERENT])
def test_different_endpoints_never_compare_equal(a, b, why):
    assert cr.endpoint_identity(a) != cr.endpoint_identity(b), why
    assert cr.normalize_endpoint_url(a) != cr.normalize_endpoint_url(b), why


@pytest.mark.parametrize("a, b, why", SAME, ids=[c[2] for c in SAME])
def test_the_same_endpoint_compares_equal(a, b, why):
    assert cr.endpoint_identity(a) == cr.endpoint_identity(b), why
    assert cr.normalize_endpoint_url(a) == cr.normalize_endpoint_url(b), why


def test_the_normalised_form_keeps_ipv6_brackets_and_port_zero():
    assert cr.normalize_endpoint_url(V6_8443) == V6_8443
    assert cr.normalize_endpoint_url(V6_DEFAULT) == V6_DEFAULT
    assert cr.normalize_endpoint_url("https://h.example:0/x") == "https://h.example:0/x"


@pytest.mark.parametrize("a, b, why", DIFFERENT, ids=[c[2] for c in DIFFERENT])
def test_an_mcp_registration_with_two_different_endpoints_is_refused(cat, secrets, a, b, why):
    res = _mcp(a, url=b)
    assert "error" in res and "one URL" in res["error"], (why, res)
    _nothing_stored(cat, secrets)


def test_guard_the_same_ipv6_endpoint_in_both_fields_registers(cat, secrets):
    res = _mcp("https://[2606:4700:4700::1111]:443/mcp", url="https://[2606:4700:4700::1111]/mcp")
    assert res.get("status") == "created", res


@pytest.mark.parametrize("base, spec, why", [
    ("https://[2606:4700:4700::1111]:8443", "https://[2606:4700:4700::1111:8443]/openapi.json",
     "IPv6 host collision"),
    ("https://api.example.com:0", "https://api.example.com/openapi.json", "explicit port 0"),
    ("https://api.example.com/v1/", "https://api.example.com/v10/openapi.json", "segment"),
])
def test_the_rest_token_scope_never_matches_another_endpoint(base, spec, why):
    assert not onboard_agent._token_target_matches(base, spec), why


@pytest.mark.parametrize("base, spec", [
    ("https://[2606:4700:4700::1111]:8443", "https://[2606:4700:4700::1111]:8443/openapi.json"),
    ("https://API.example.com:443/v1", "https://api.example.com/v1/openapi.json"),
    ("https://api.example.com/v1/", "https://api.example.com/v1/openapi.json"),
])
def test_guard_the_rest_token_scope_still_matches_its_own_server(base, spec):
    assert onboard_agent._token_target_matches(base, spec)
