# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An in-process fake of the HighByte 4.4.2 MCP server, for offline tests.

Speaks the subset of streamable-HTTP MCP the app uses (``initialize``,
``notifications/initialized``, ``tools/list``, ``tools/call``; GET answers 405,
so no server-sent stream) through ``httpx.MockTransport``, so no socket opens.

``accept`` names the Authorization form it takes: ``"bearer"`` (only
``Authorization: Bearer <key>``), ``"raw"`` (only the key), ``"both"`` (what a
4.4.2 hub takes); any other value refuses every request. A refused request is
401. The tool list is the one
a local 4.4.2 hub returns for this repo's project with Configuration Tools on
(``NameSpace_MCP`` plus the five ``config_pipeline_*`` tools), or only
``NameSpace_MCP`` with ``config_tools=False``. ``restart()`` forgets every
session, as a hub restart or an expired-then-reset trial does; a request on a
forgotten session gets ``stale_status`` (the 4.4.2 hub answers 400). After a
restart, ``tools/call`` answers "Unknown Tool" until some session runs
``tools/list`` (measured on a local 4.4.2 hub: 55 s of "Unknown Tool" after
``docker restart``, then calls worked once one session listed the tools).

Failure modes for the session-reopen tests: ``down_status`` answers every
request with that status (a hub that is not serving); ``drop_after_call``
records a ``tools/call`` and then answers that status instead of the result
(the call took effect but its response was lost); ``error_text`` answers every
``tools/call`` with a tool error carrying that text (an error the server
writes, while the session stays open).
"""
from __future__ import annotations

import itertools
import json

import httpx

KEY = "hb-test-api-key-0123456789"  # nosec B105 - test-only fake credential

NAMESPACE_TOOL = {"name": "NameSpace_MCP", "description": "Read the namespace.",
                  "inputSchema": {"type": "object", "properties": {}}}
CONFIG_TOOLS = [
    {"name": "config_pipeline_get", "description": "Get a pipeline.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "config_pipeline_create", "description": "Create a pipeline.",
     "inputSchema": {"type": "object", "properties": {"pipeline": {"type": "object"}}}},
    {"name": "config_pipeline_edit", "description": "Edit a pipeline.",
     "inputSchema": {"type": "object", "properties": {"pipeline": {"type": "object"}}}},
    {"name": "config_pipeline_schema", "description": "The pipeline schema.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "config_pipeline_examples", "description": "Pipeline examples.",
     "inputSchema": {"type": "object", "properties": {}}},
]
CONFIG_TOOL_NAMES = frozenset(t["name"] for t in CONFIG_TOOLS)


class FakeHub:
    def __init__(self, *, accept: str = "bearer", config_tools: bool = True,
                 namespace: object = None, stale_status: int = 400):
        self.down_status: int | None = None
        self.drop_after_call: int | None = None
        self.error_text: str | None = None
        self.accept = accept
        self.config_tools = config_tools
        self.namespace = namespace if namespace is not None else [{"_name": "Site", "value": 1}]
        self.stale_status = stale_status
        self.sessions: set[str] = set()
        self.listed = False
        self._ids = itertools.count(1)
        self.auth_seen: list[str] = []
        self.calls: list[str] = []

    # --- auth -------------------------------------------------------------
    def _authorized(self, request: httpx.Request) -> bool:
        value = request.headers.get("authorization", "")
        self.auth_seen.append("bearer" if value == f"Bearer {KEY}"
                              else "raw" if value == KEY else "other" if value else "none")
        accepted = {"bearer": {f"Bearer {KEY}"}, "raw": {KEY},
                    "both": {KEY, f"Bearer {KEY}"}}.get(self.accept, set())
        return value in accepted

    def restart(self) -> None:
        self.sessions.clear()
        self.listed = False

    @property
    def tools(self) -> list[dict]:
        return [NAMESPACE_TOOL, *(CONFIG_TOOLS if self.config_tools else [])]

    # --- transport --------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down_status:
            return httpx.Response(self.down_status)
        if request.method != "POST":
            return httpx.Response(405)
        if not self._authorized(request):
            return httpx.Response(401, json={"error": "unauthorized"})
        message = json.loads(request.content or b"{}")
        method = message.get("method")
        if method == "initialize":
            sid = f"s{next(self._ids)}"
            self.sessions.add(sid)
            return httpx.Response(200, headers={"mcp-session-id": sid}, json={
                "jsonrpc": "2.0", "id": message.get("id"),
                "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake-hub", "version": "4.4.2"}}})
        sid = request.headers.get("mcp-session-id", "")
        if sid and sid not in self.sessions:
            return httpx.Response(self.stale_status)
        if "id" not in message:
            return httpx.Response(202)
        if method == "tools/list":
            self.listed = True
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                             "result": {"tools": self.tools}})
        if method == "tools/call":
            name = (message.get("params") or {}).get("name", "")
            if not self.listed:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {
                    "content": [{"type": "text", "text": "Unknown Tool"}], "isError": True}})
            self.calls.append(name)
            if self.drop_after_call:
                return httpx.Response(self.drop_after_call)
            if self.error_text is not None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {
                    "content": [{"type": "text", "text": self.error_text}], "isError": True}})
            text = json.dumps(self.namespace) if name == "NameSpace_MCP" else "{}"
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                             "result": {"content": [{"type": "text", "text": text}],
                                                        "isError": False}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"),
                                         "error": {"code": -32601, "message": "no method"}})


def install(monkeypatch, hub: FakeHub) -> None:
    """Route every sync ``httpx.Client`` and the runtime's MCP client factory to
    ``hub`` (no socket opens)."""
    real_client = httpx.Client

    class _Client(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(hub.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 transport=httpx.MockTransport(hub.handle))

    from agents import response_integrity_query
    monkeypatch.setattr(response_integrity_query, "capped_mcp_http_client_factory", factory)
