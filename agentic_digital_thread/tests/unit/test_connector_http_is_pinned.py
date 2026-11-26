# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No connector HTTP call bypasses the pinned path (``agents/pinned_http.py``).

An AST scan of every module under ``agents/``: a direct ``httpx`` request,
client or transport (``httpx.get``, ``httpx.Client(...)``, the same names
imported with ``from httpx import`` or through an ``import httpx as`` alias),
the MCP SDK's default client (``create_mcp_http_client``), or an MCP
streamable-HTTP client without the capped, pinned client factory, fails the
scan unless its module is on :data:`ALLOWED` with the reason it is not a
connector call or pins the address itself. An allowlisted module that no
longer makes such a call fails too, so the list only shrinks.
"""
from __future__ import annotations

import ast
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "agents"

#: The httpx entry points that send a request or build something that does.
_HTTPX_NAMES = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "request", "stream",
    "Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport",
})
_MCP_CLIENTS = frozenset({"streamablehttp_client", "streamable_http_client", "sse_client"})
_PINNED_FACTORY = "capped_mcp_http_client_factory"

#: Module (relative to ``agents/``) -> why it may call httpx directly.
ALLOWED = {
    "pinned_http.py": "the one pinned path itself",
    "connection_probes.py": "pins each probe to the address it validates, Host and SNI kept",
    "query_bridge_server.py": "calls this deployment's own application URL, not a connector",
    "auth/cognito.py": "reads the deployment's own Amazon Cognito signing keys",
    "gateway_auth.py": "connects to the deployment's own Amazon Bedrock AgentCore gateway",
    "orchestrator.py": "connects to the deployment's own Amazon Bedrock AgentCore gateway",
}


def _offences(tree: ast.AST) -> list[str]:
    modules, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    modules.add(alias.asname or "httpx")
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for alias in node.names:
                if alias.name in _HTTPX_NAMES:
                    names.add(alias.asname or alias.name)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id in modules and func.attr in _HTTPX_NAMES):
            found.append(f"line {node.lineno}: {func.value.id}.{func.attr}(...)")
        elif isinstance(func, ast.Name) and func.id in names:
            found.append(f"line {node.lineno}: {func.id}(...) from httpx")
        elif callee == "create_mcp_http_client":
            found.append(f"line {node.lineno}: create_mcp_http_client(...)")
        elif callee in _MCP_CLIENTS:
            factory = next((kw.value for kw in node.keywords
                            if kw.arg in {"httpx_client_factory", "http_client"}), None)
            if not (isinstance(factory, ast.Name) and factory.id == _PINNED_FACTORY):
                found.append(f"line {node.lineno}: {callee}(...) without {_PINNED_FACTORY}")
    return found


def _scan() -> dict[str, list[str]]:
    out = {}
    for path in sorted(AGENTS.rglob("*.py")):
        rel = path.relative_to(AGENTS).as_posix()
        found = _offences(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if found:
            out[rel] = found
    return out


def test_no_connector_module_calls_httpx_outside_the_pinned_path():
    unpinned = {rel: found for rel, found in _scan().items() if rel not in ALLOWED}
    assert unpinned == {}, (
        "Connector HTTP must go through agents/pinned_http.py (it connects to the "
        f"validated address and never follows redirects): {unpinned}")


def test_every_allowlisted_module_still_needs_its_entry():
    hits = _scan()
    stale = sorted(rel for rel in ALLOWED if rel not in hits)
    assert stale == [], f"remove these entries, they make no direct call any more: {stale}"


def test_the_scan_catches_each_spelling():
    source = (
        "import httpx\n"
        "import httpx as h\n"
        "from httpx import Client as C\n"
        "from mcp.shared._httpx_utils import create_mcp_http_client\n"
        "httpx.get('u')\n"
        "h.post('u')\n"
        "C()\n"
        "create_mcp_http_client()\n"
        "streamablehttp_client('u')\n"
        "streamablehttp_client('u', httpx_client_factory=other)\n"
        "streamablehttp_client('u', httpx_client_factory=capped_mcp_http_client_factory)\n"
    )
    found = _offences(ast.parse(source))
    assert len(found) == 6, found
