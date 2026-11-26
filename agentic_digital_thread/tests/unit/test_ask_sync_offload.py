# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 D1 prerequisite — ``/api/ask/sync``'s cloud branch is offloaded.

The cloud stream is sync I/O (the boto3 AgentCore invoke and, since ADR-0078,
the per-turn metric-layer resolve). The handler used to iterate it INLINE in
``async def ask_sync``, freezing the single uvicorn event loop for the whole
turn. The AST tripwire in ``test_event_loop_offload.py`` could not see it
(``stream`` is also the name of Starlette's async ``request.stream()``), so
this file pins the handler specifically. Offline.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.chat_client as chat_client
import agents.server as server
from agents.events import DoneEvent, MessageEvent


def _called_names(fn) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute)
                      else getattr(func, "id", ""))
    return names


def test_ask_sync_cloud_branch_is_offloaded():
    names = _called_names(server.ask_sync)
    assert "stream" not in names and "AgentCoreChatClient" not in names
    assert "offload" in names
    assert "stream" in _called_names(server._ask_sync_cloud)


def test_ask_sync_cloud_answer_shape_unchanged(monkeypatch):
    seen: dict = {}

    class _FakeClient:
        def stream(self, **kwargs):
            seen.update(kwargs, thread=threading.current_thread().name)
            yield MessageEvent(content="widget ")
            yield MessageEvent(content="answer")
            yield DoneEvent()

    monkeypatch.setattr(server, "get_config", lambda: SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client, "AgentCoreChatClient", _FakeClient)
    app = FastAPI()
    app.post("/api/ask/sync")(server.ask_sync)
    with TestClient(app) as client:
        response = client.post("/api/ask/sync", json={
            "question": "widget units today", "session_id": "sync-w",
            "user_id": "widget-user"})
    assert response.status_code == 200
    assert response.json() == {"answer": "widget answer"}
    assert seen["session_id"] == "sync-w" and seen["write_memory"] is False
    assert seen["thread"] != threading.main_thread().name
