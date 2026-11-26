# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.query_router`` connection sourcing.

Asserts that ``_query_openapi`` and ``_query_mcp`` obtain their endpoint URL
(and auth) from ``resolve_connection(system_id)`` rather than from the catalog
snapshot. All I/O is faked — NO real network, AWS, or MCP server.

The module under test is imported with the REAL sibling modules
(``agents.catalog``/``agents.db``/``agents.connection_resolver``); they import
cleanly and the catalog is never read by the rerouted code, so the real import
is harmless. Each test patches ``qr.resolve_connection`` directly, and the
network clients (``httpx``/``mcp``) — which ``query_router`` imports lazily
INSIDE the functions under test — are faked per-test via a
``patch.dict(sys.modules, ...)`` CONTEXT MANAGER that auto-restores. Nothing is
mutated in ``sys.modules`` (or any global) at import/collection time, so this
module leaves no residue for the rest of the test session.

Runnable two ways (matches ``test_connection_resolver.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_query_router_resolver.py
  * standalone:  python agentic_digital_thread/tests/unit/test_query_router_resolver.py
"""
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

# Import the module under test with the REAL siblings. ``query_router`` imports
# ``resolve_connection`` (patched per-test below) and ``catalog``/``pg`` at the
# module level, but ``_query_openapi``/``_query_mcp`` never touch the catalog —
# they source the connection from ``resolve_connection`` — so the real import is
# inert for these tests. No ``sys.modules`` mutation happens here.
import agents.query_router as qr  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes for the network clients. ``query_router`` imports ``httpx`` and ``mcp``
# LAZILY inside the functions under test, so swapping them into ``sys.modules``
# for the duration of the call (via ``patch.dict``) is sufficient — and fully
# reverts on exit, leaving zero residue.
# --------------------------------------------------------------------------- #
class _FakeHttpResp:
    headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield b'{"ok":1}'


@contextmanager
def _fake_httpx(captured: dict):
    """Install a fake ``httpx`` in ``sys.modules`` for the duration of the block."""
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            assert method == "GET"
            captured["url"] = url
            captured["headers"] = headers or {}
            return _FakeHttpResp()

    mod = types.ModuleType("httpx")
    mod.Client = _FakeClient
    with patch.dict(sys.modules, {"httpx": mod}):
        yield


@contextmanager
def _fake_mcp(captured: dict):
    """Install a fake ``mcp`` package tree for the duration of the block."""
    class _FakeACM:
        async def __aenter__(self):
            return (None, None, None)

        async def __aexit__(self, *a):
            return False

    def _streamablehttp_client(url, **kwargs):
        captured["url"] = url
        captured["httpx_client_factory"] = kwargs.get("httpx_client_factory")
        return _FakeACM()

    class _FakeSession:
        def __init__(self, rs, ws):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])  # no tools -> early empty return

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = _FakeSession

    client_pkg = types.ModuleType("mcp.client")

    sh_mod = types.ModuleType("mcp.client.streamable_http")
    sh_mod.streamablehttp_client = _streamablehttp_client

    fakes = {
        "mcp": mcp_mod,
        "mcp.client": client_pkg,
        "mcp.client.streamable_http": sh_mod,
    }
    with patch.dict(sys.modules, fakes):
        yield


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_query_openapi_uses_resolve_connection_not_catalog():
    """_query_openapi builds the URL + auth from resolve_connection(system_id)."""
    captured = {}
    resolved = {
        "system_id": "sys-rest",
        "connection_type": "REST API",
        "connection_string": "https://resolved.example.com/api/",
        "auth_type": "bearer",
        "token": "TKN-123",
    }
    with _fake_httpx(captured):
        with patch.object(qr, "resolve_connection", return_value=resolved) as mock_resolve:
            out = qr._query_openapi("sys-rest", "GET /widgets")

    # URL was resolved via the resolver, with system_id threaded through.
    mock_resolve.assert_called_once_with("sys-rest")
    assert captured["url"] == "https://resolved.example.com/api/widgets"
    # The catalog trap value must NOT appear.
    assert "CATALOG-TRAP" not in captured["url"]
    # Auth came from the merged secret on the resolved dict.
    assert captured["headers"].get("Authorization") == "Bearer TKN-123"
    assert out["row_count"] == 1


def test_query_openapi_prefers_base_url_then_url():
    """base_url wins over url/connection_string when present in the resolved dict."""
    captured = {}
    resolved = {
        "connection_string": "https://conn-string.example.com",
        "url": "https://url-field.example.com",
        "base_url": "https://base-url.example.com/v1",
    }
    with _fake_httpx(captured):
        with patch.object(qr, "resolve_connection", return_value=resolved):
            qr._query_openapi("sys-rest", "/things")

    assert captured["url"] == "https://base-url.example.com/v1/things"


def test_query_mcp_uses_resolve_connection_not_catalog():
    """_query_mcp obtains the MCP endpoint from resolve_connection(system_id)."""
    captured = {}
    resolved = {
        "system_id": "sys-mcp",
        "connection_type": "MCP Server",
        "connection_string": "https://resolved-mcp.example.com/mcp",
    }
    with _fake_mcp(captured):
        with patch.object(qr, "resolve_connection", return_value=resolved) as mock_resolve:
            out = qr._query_mcp("sys-mcp", "find stuff")

    mock_resolve.assert_called_once_with("sys-mcp")
    assert captured["url"] == "https://resolved-mcp.example.com/mcp"
    assert "CATALOG-TRAP" not in captured["url"]
    # No tools advertised -> empty normalized result (behavior preserved).
    assert out == {"columns": [], "rows": [], "row_count": 0}


def test_query_openapi_basic_auth_scheme():
    """auth_scheme='basic' → 'Authorization: Basic <token>' (HTTP Basic; e.g. Tulip)."""
    captured = {}
    resolved = {
        "connection_string": "https://tulip.example.com/api/v3",
        "auth_scheme": "basic",
        "token": "QVBJX0tFWTpTRUNSRVQ=",  # base64("API_KEY:SECRET")
    }
    with _fake_httpx(captured):
        with patch.object(qr, "resolve_connection", return_value=resolved):
            qr._query_openapi("sys-tulip", "GET /tables/abc/records")
    assert captured["url"] == "https://tulip.example.com/api/v3/tables/abc/records"
    assert captured["headers"].get("Authorization") == "Basic QVBJX0tFWTpTRUNSRVQ="


def test_query_openapi_bearer_literal_and_api_key_schemes():
    """auth_scheme bearer_literal sends the token verbatim; api_key uses X-API-Key."""
    cap1 = {}
    with _fake_httpx(cap1):
        with patch.object(qr, "resolve_connection", return_value={
                "base_url": "https://x.example.com", "auth_scheme": "bearer_literal",
                "token": "Basic ABC=="}):
            qr._query_openapi("s", "/p")
    assert cap1["headers"].get("Authorization") == "Basic ABC=="

    cap2 = {}
    with _fake_httpx(cap2):
        with patch.object(qr, "resolve_connection", return_value={
                "base_url": "https://x.example.com", "auth_scheme": "api_key",
                "token": "K123"}):
            qr._query_openapi("s", "/p")
    assert cap2["headers"].get("X-API-Key") == "K123"


def _run_standalone():
    tests = [
        test_query_openapi_uses_resolve_connection_not_catalog,
        test_query_openapi_prefers_base_url_then_url,
        test_query_mcp_uses_resolve_connection_not_catalog,
        test_query_openapi_basic_auth_scheme,
        test_query_openapi_bearer_literal_and_api_key_schemes,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - standalone reporter
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_standalone() else 0)
