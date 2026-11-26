# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Local mode serves each system agent's MCP server from the app's own
process (``agents.local_agent_server``): its configuration, the database
password in ``conn_params`` included, is handed over as a Python object, so
no command line ever carries it, and there is no child output pipe to fill.

These start a real server on loopback; only the agent's ``FastMCP`` app is
a small stand-in for ``system_agent.create_mcp_server``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess  # nosec B404 - only replaced here, never run
import threading
import time

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

import agents.agent_manager as am
import agents.system_agent as sa
from agents import lease_guard

# These serve and call a real 127.0.0.1 server on purpose (that is the
# behavior under test), so they opt out of the lane-wide --disable-socket
# guard; no external network involved.
pytestmark = pytest.mark.enable_socket

SID = "mes_cr"
TOOL = "mes-cr___query_mes_cr"
PASSWORD = "pw-that-must-never-reach-argv"  # nosec B105 - test value
#: Set once the "hang" question's worker thread is blocked.
HANGING = threading.Event()
CONN = {"host": "127.0.0.1", "port": "5432", "dbname": "adt", "user": "adt",
        "password": PASSWORD}


@pytest.fixture
def spawned(monkeypatch):
    """Every process start is refused and recorded with its argv."""
    starts: list = []

    def _refuse(*args, **kwargs):
        starts.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"a process was started: {starts[-1]!r}")
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _refuse)
    for name in ("posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
        monkeypatch.setattr(os, name, _refuse, raising=False)
    return starts


@pytest.fixture
def built(monkeypatch):
    """``create_mcp_server`` replaced by a FastMCP app with the agent's query
    tool; returns the keyword arguments of each build and the context
    variables it saw."""
    builds: list = []
    import agents.business_context as bc
    monkeypatch.setattr(bc, "read_business_context", lambda _sid: None)

    def _create(**kwargs):
        builds.append({**kwargs, "_guard": lease_guard._WRITE_GUARD.get()})
        mcp = FastMCP(name=f"adt-{kwargs['system_id']}", host=kwargs["host"],
                      port=kwargs["port"], stateless_http=True)

        @mcp.tool(name=f"{kwargs['tool_name_prefix']}query_{SID}")
        async def query(question: str) -> str:
            if question == "hang":                     # a blocking call, as the
                import asyncio                         # connectors make

                def _block():
                    HANGING.set()
                    time.sleep(30)
                await asyncio.to_thread(_block)
            return f"answer: {question}"
        return mcp
    monkeypatch.setattr(sa, "create_mcp_server", _create)
    return builds


@pytest.fixture
def mgr():
    manager = am.AgentManager(mode="local")
    yield manager
    for sid in list(manager._agents):
        manager.stop_agent(sid)


def _start(manager, conn_params=None):
    return manager._start_local(SID, "Line MES", "mes", "prompt", "orders(id: int)",
                                dict(conn_params or CONN))


def _ask(url: str, question: str, timeout: float = 10) -> str:
    resp = httpx.post(url, timeout=timeout, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"question": question}}},
        headers={"accept": "application/json, text/event-stream"})
    resp.raise_for_status()
    body = resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    return json.loads(body)["result"]["content"][0]["text"]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_a_local_agent_is_served_in_process_and_no_argv_carries_its_password(
        spawned, built, mgr):
    url = _start(mgr)
    assert spawned == []
    port = mgr._agents[SID].port
    assert url == f"http://127.0.0.1:{port}/mcp"
    assert built[0]["conn_params"] == CONN and built[0]["host"] == "127.0.0.1"
    assert built[0]["port"] == port and built[0]["tool_name_prefix"] == "mes-cr___"
    assert _ask(url, "how many orders?") == "answer: how many orders?"
    assert not any(PASSWORD in " ".join(map(str, argv or ())) for argv in spawned)


def test_a_stopped_local_agent_frees_its_port_and_starts_again(spawned, built, mgr, caplog):
    _start(mgr)
    inst = mgr._agents[SID]
    with caplog.at_level("ERROR", logger="agents.local_agent_server"):
        mgr.stop_agent(SID)
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []
    assert inst.server._error is None
    assert not inst.server.is_running() and not _listening(inst.port)
    assert SID not in mgr._agents and mgr._used_ports == set()

    url = _start(mgr)
    assert mgr._agents[SID].port == inst.port              # the freed port again
    assert _ask(url, "again") == "answer: again"
    assert spawned == [] and len(built) == 2


def test_a_build_failure_fails_the_start_with_its_reason(spawned, built, mgr, monkeypatch):
    def _broken(**_kwargs):
        raise ValueError("schema mes not found")
    monkeypatch.setattr(sa, "create_mcp_server", _broken)
    with pytest.raises(RuntimeError, match="mes_cr failed to start: ValueError: schema mes"):
        _start(mgr)
    assert SID not in mgr._agents and mgr._used_ports == set()


def test_a_port_taken_after_the_probe_fails_the_start_with_the_bind_error(
        spawned, built, mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_port_is_free", lambda _port: True)
    with socket.create_server(("127.0.0.1", mgr.PORT_MIN)):
        with pytest.raises(RuntimeError, match="failed to start: OSError: .*in use"):
            _start(mgr)
    assert SID not in mgr._agents and mgr._used_ports == set() and built == []


def test_the_agent_does_not_inherit_the_starting_operations_lease(spawned, built, mgr):
    with lease_guard.write_guard(lambda: None):
        _start(mgr)
    assert built[0]["_guard"] is None


def test_a_stop_ends_a_server_whose_request_blocks_in_a_worker_thread(spawned, built, mgr):
    """A tool blocked in ``asyncio.to_thread`` cannot be interrupted; the
    stop still ends the server thread within its bound instead of waiting
    for that worker (asyncio.run would wait up to 300 s)."""
    mgr.STOP_WAIT_SECONDS = 0.5
    url = _start(mgr)
    inst = mgr._agents[SID]
    asking = threading.Thread(target=lambda: pytest.raises(Exception, _ask, url, "hang"),
                              daemon=True)
    HANGING.clear()
    asking.start()
    assert HANGING.wait(10)                                # its worker is blocked
    started = time.monotonic()
    mgr.stop_agent(SID)
    assert time.monotonic() - started < 2 * mgr.STOP_WAIT_SECONDS + 1
    assert inst.server._error is None
    assert not inst.server.is_running() and not _listening(inst.port)
    assert mgr._used_ports == set()
