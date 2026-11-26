# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The premise of fresh smoke scoring: a fresh smoke turn has no AgentCore Memory.

Strict smoke scoring treats a fresh turn as having no earlier conversation
(``scripts/smoke_prompts.strict_failure_reasons``): memory-sounding wording is
reported, and only a claim of a prior conversation fails. That holds because
the turn can't read or write AgentCore Memory: the lane sends
``write_memory: false``, the app resolves that to False, the stream worker
hands it to the chat client, the AgentCore payload and both runtime
entrypoints carry it through, and the orchestrator attaches
the memory session manager (retrieval and writes) only when it is True. These
tests pin each link, so a change that gives smoke turns memory fails here
instead of silently making the scoring wrong.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.smoke_prompts import ask_body  # noqa: E402


def test_the_lane_asks_for_a_fresh_turn_without_memory():
    assert ask_body("q", "smoke-run-1", followup_mode=False)["write_memory"] is False


def test_the_app_resolves_a_false_write_memory_to_false():
    from agents import server
    body = SimpleNamespace(write_memory=False, user_id="smoke")
    request = SimpleNamespace(state=SimpleNamespace(user=None, session=None))
    with patch("agents.auth.forwarding.user_access_token_from_session",
               return_value=None):
        _user, user_id, write_memory, _token = server._ask_stream_identity(
            body, request, is_briefing=False)
    assert user_id == "smoke"
    assert write_memory is False


def test_the_ask_path_hands_write_memory_false_to_the_chat_client(monkeypatch):
    # The composed server-to-client handoff: an /api/ask
    # body with write_memory=False goes through _admitted_ask_response
    # (_ask_stream_identity, then _start_stream_worker) and must reach
    # client.stream as False, so a stream worker that forces memory on fails.
    from agents import server
    from agents.models import AskRequest
    from agents.query_admission import get_admission
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    seen = []

    class _Client:
        def stream(self, **kwargs):
            seen.append(kwargs)
            return iter(())

    body = AskRequest(question="q", session_id="smoke-run-1", user_id="smoke",
                      write_memory=False)
    request = SimpleNamespace(state=SimpleNamespace(user=None, session=None))
    owner = server._StreamOwner(get_admission().acquire("query"))
    try:
        with patch.object(server, "get_chat_client", lambda: _Client()), \
             patch.object(server, "_store_ask_usage", lambda *a, **k: None), \
             patch("agents.auth.forwarding.user_access_token_from_session",
                   return_value=None):
            response = server._admitted_ask_response(
                body, request, "q", False, owner)
            list(response._source_iterator)
    finally:
        owner.cancel()
    assert len(seen) == 1
    assert seen[0]["session_id"] == "smoke-run-1"
    assert seen[0]["write_memory"] is False


def test_no_memory_session_manager_without_write_memory_even_when_configured():
    import agents.orchestrator as orch
    captured = []

    def _agent(**kwargs):
        captured.append(kwargs)
        return MagicMock(name="Agent")

    config = MagicMock(has_memory=True, memory_id="mem-1")
    with patch.object(orch, "get_config", return_value=config), \
         patch.object(orch, "_get_orchestrator_tools", return_value=[]), \
         patch.object(orch, "_make_session_manager",
                      side_effect=AssertionError("memory attached")), \
         patch.object(orch, "Agent", side_effect=_agent):
        orch.build_orchestrator("smoke-run-1", "smoke", write_memory=False)
    assert "session_manager" not in captured[0]


# The cloud path between the app and build_orchestrator: the AgentCore payload
# and both runtime entrypoints must carry write_memory=False through.

class _EmptyStream:
    def iter_lines(self):
        return iter([])


def test_the_runtime_payload_carries_write_memory_false(monkeypatch):
    import json
    from agents.chat_client import AgentCoreChatClient
    monkeypatch.setenv("METRIC_TURN_NOTICE", "off")
    captured = {}
    fake = MagicMock()

    def _invoke(**kwargs):
        captured["payload"] = json.loads(kwargs["payload"].read().decode())
        return {"response": _EmptyStream()}

    fake.invoke_agent_runtime.side_effect = _invoke
    with patch("agents.chat_client.boto3.client", return_value=fake):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x",
            region="us-east-1")
        list(client.stream(prompt="q", session_id="smoke-run-1", user_id="smoke",
                           write_memory=False))
    assert captured["payload"]["writeMemory"] is False


def _no_prelude(monkeypatch):
    from agents import runtime_app
    monkeypatch.setattr(runtime_app, "_prelude_with_planned",
                        lambda raw, cand, **_routing: (raw, [], None))
    return runtime_app


def test_the_legacy_runtime_turn_keeps_write_memory_false(monkeypatch):
    runtime_app = _no_prelude(monkeypatch)
    for payload in ({"prompt": "q", "sessionId": "smoke-run-1", "writeMemory": False},
                    {"prompt": "q", "sessionId": "smoke-run-1"}):
        assert runtime_app._new_turn_state(payload, None).write_memory is False


def test_the_integrity_runtime_builds_the_orchestrator_with_write_memory_false(monkeypatch):
    import asyncio
    runtime_app = _no_prelude(monkeypatch)
    seen = []

    class _Stop(Exception):
        pass

    def _build(**kwargs):
        seen.append(kwargs)
        raise _Stop()

    async def _drain():
        async for _ in runtime_app._invoke_integrity_runtime(
                {"prompt": "q", "sessionId": "smoke-run-1", "userId": "smoke",
                 "writeMemory": False}, None, _build):
            pass

    asyncio.run(_drain())
    assert seen and seen[0]["write_memory"] is False
