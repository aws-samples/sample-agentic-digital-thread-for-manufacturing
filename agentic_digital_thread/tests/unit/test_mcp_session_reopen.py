# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent's MCP client survives its server restarting, without
replaying a call that may have taken effect.

The runtime loads an MCP Server system's tools once and keeps the client. A
HighByte hub drops every session when ECS replaces its task, and when an
expired trial is reset (the hub stops and restarts its HTTP server). Measured
on a local 4.4.2 hub (``docker restart``): the next call through the kept
client failed with "Connection to the MCP server was closed" and every call
after it raised "the client session is not running", while a new client worked
at once. The fake hub answers a forgotten session with 400, as 4.4.2 does.

The rules pinned here:

* the session is judged closed by the client's own state, never by text a tool
  result carries;
* a call whose session closed under it is called again only when it is on the
  replay-safe list (the built-in HighByte ``NameSpace_MCP`` read); any other
  call gets a retryable "outcome unknown" error and is not sent again;
* a call that never reached the server (the session was already closed) is
  sent once the session is reopened;
* a 401 or 403 is reported as such: no reopen, no replay.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from agents import system_agent  # noqa: E402
from tests.unit import _fake_mcp_hub as fh  # noqa: E402

CFG = {"url": "https://hb.example.com/mcp", "token": fh.KEY, "auth_scheme": "bearer"}
CLOSED = "connection to the mcp server was closed"


@pytest.fixture
def hub(monkeypatch):
    hub = fh.FakeHub(accept="bearer", config_tools=True)
    fh.install(monkeypatch, hub)
    return hub


@pytest.fixture
def tools_for(hub):
    loaded = []

    def load(system_id: str) -> dict:
        tools, _ = system_agent._system_tools(system_id, "mcp", "", None, "MCP Server", CFG)
        loaded.append(tools[0].mcp_client)
        return {t.tool_name: t for t in tools}
    yield load
    for client in loaded:
        try:
            client.stop(None, None, None)
        except RuntimeError:
            pass  # stop() re-raises why a session closed, after cleaning up


def _call(tool, tool_use_id: str, arguments=None) -> dict:
    """The call as the agent makes it (``MCPAgentTool.stream``: keyword args)."""
    return asyncio.run(tool.mcp_client.call_tool_async(
        tool_use_id=tool_use_id, name=tool.mcp_tool.name, arguments=arguments or {}))


def _text(result: dict) -> str:
    return " ".join(b.get("text", "") for b in result.get("content") or []).lower()


def test_the_highbyte_read_recovers_once_after_a_restart(hub, tools_for):
    read = tools_for("highbyte")["NameSpace_MCP"]
    assert _call(read, "before")["status"] == "success"
    hub.restart()
    after = _call(read, "after")
    assert after["status"] == "success", after
    assert _call(read, "again")["status"] == "success"
    assert hub.calls == ["NameSpace_MCP"] * 3


def test_a_mutating_tool_is_not_replayed_when_its_response_is_lost(hub, tools_for):
    create = tools_for("plant-uns")["config_pipeline_create"]
    hub.drop_after_call = 502  # the hub acts, then the response is lost
    result = _call(create, "create", {"pipeline": {"name": "p"}})
    assert hub.calls == ["config_pipeline_create"]  # sent once, never again
    assert result["status"] == "error"
    assert "unknown" in _text(result) and CLOSED not in _text(result)
    hub.drop_after_call = None
    assert _call(create, "next", {"pipeline": {"name": "q"}})["status"] == "success"
    assert hub.calls == ["config_pipeline_create"] * 2


def test_a_non_allowlisted_read_is_not_replayed_either(hub, tools_for):
    read = tools_for("plant-uns")["NameSpace_MCP"]
    assert _call(read, "before")["status"] == "success"
    hub.restart()
    result = _call(read, "after")
    assert result["status"] == "error" and "unknown" in _text(result)
    assert hub.calls == ["NameSpace_MCP"]
    assert _call(read, "next")["status"] == "success"  # the session was reopened


def test_a_call_that_never_reached_the_server_is_sent_after_the_reopen(hub, tools_for):
    """The session closed earlier (the server was down); the next call is
    refused by the client before anything is sent, so sending it is not a
    replay, for any tool."""
    create = tools_for("plant-uns")["config_pipeline_create"]
    hub.down_status = 503
    assert _call(create, "down")["status"] == "error"
    hub.down_status = None
    assert hub.calls == []
    assert _call(create, "back", {"pipeline": {"name": "p"}})["status"] == "success"
    assert hub.calls == ["config_pipeline_create"]


def test_a_server_error_text_never_reopens_the_session(hub, tools_for):
    read = tools_for("highbyte")["NameSpace_MCP"]
    hub.error_text = "Connection to the MCP server was closed"
    result = _call(read, "error")
    assert result["status"] == "error"
    assert hub.calls == ["NameSpace_MCP"]  # returned as is, not called again
    assert len(hub.sessions) == 1  # the load's session, never reopened


def test_a_401_surfaces_its_cause_without_a_reopen_or_a_replay(hub, tools_for):
    read = tools_for("highbyte")["NameSpace_MCP"]
    sessions = len(hub.sessions)
    hub.accept = "nobody"  # the hub stops taking this key
    result = _call(read, "refused")
    assert result["status"] == "error"
    assert "401" in _text(result), result
    assert len(hub.sessions) == sessions  # no initialize was accepted
    assert hub.calls == []


def test_a_server_still_down_reports_why_the_reopen_failed(hub, tools_for):
    read = tools_for("highbyte")["NameSpace_MCP"]
    hub.down_status = 503
    result = _call(read, "down")
    assert result["status"] == "error"
    assert "503" in _text(result), result
    hub.down_status = None
    assert _call(read, "back")["status"] == "success"
