# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chat trace shows the route context the system agent was sent.

The trace builds a call's input from the model's own tool-use arguments, which
are streamed before the gateway client runs, so a ROUTE block the gateway
merged in from the route seed (``route_facts``) never reached the trace. The
runtime now forwards each merged context with its tool result (a copy of the
forwarded message; the agent's own message is untouched), the chat client
puts it on the ``tool_result`` event as ``route_context``, and the trace's
Routing context shows it. ``raw args`` stays the model's own arguments.

The turn runs end to end offline: the route seed, the real gateway client,
Strands' ``MCPAgentTool.stream``, the runtime's stream wrapper and event
filter, and the chat client's frame parser. Made-up systems.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents import runtime_app
from agents.chat_client import ThinkingFilter, _CloudStreamState
from agents.events import ToolResultEvent, ToolUseEvent
from tests.unit._route_facts_world import (  # noqa: F401 - isolated_turn is a fixture
    WORLDS, Client, block, gateway, isolated_turn, peer_then_target, seed, stream,
)

WORLD = WORLDS[0]
LINE_TOOL = WORLD.tool(WORLD.target)
QUESTION = WORLD.target_question


@pytest.fixture(autouse=True)
def _fresh_turn(isolated_turn, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


class _Log:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


def _messages():
    use = {"role": "assistant", "content": [{"toolUse": {
        "toolUseId": "t-target", "name": LINE_TOOL, "input": {"question": QUESTION}}}]}
    result = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t-target", "status": "success",
        "content": [{"text": "Lot run R-20 failed two gauge checks."}]}}]}
    return use, result


def _runtime_frames(messages) -> list:
    """The runtime's forwarded frames for an Agent streaming ``messages``."""
    class _Agent:
        async def stream_async(self, _prompt, **_kwargs):
            for message in messages:
                yield {"message": message}

    agent = runtime_app._chart_injecting_build(lambda **_k: _Agent())(session_id="s",
                                                                      user_id="u")
    state = SimpleNamespace(skipped_count=0, tool_results={}, tool_inputs={},
                            seen_tool_use_ids=set(), tool_calls_log=[], response_text=[])

    async def _collect():
        return [runtime_app._filter_stream_event(event, state)
                async for event in agent.stream_async("p")]

    return [json.loads(json.dumps(frame, default=str))
            for frame in asyncio.run(_collect()) if frame]


def _client_events(frames, integrity_mode=False) -> list:
    state = _CloudStreamState(ThinkingFilter(), integrity_mode=integrity_mode, log=_Log(),
                              owner_id="u", session_id="", runtime_session_id="r")
    return [event for frame in frames for event in state.events_for(frame)]


def _turn(monkeypatch):
    """A turn whose second delegation the gateway merged the seed's facts into."""
    return peer_then_target(monkeypatch, WORLD, {"question": QUESTION})


def _sse(event) -> dict:
    return json.loads(event.to_sse_format().split("data: ", 1)[1])


def test_the_trace_shows_the_route_context_the_system_was_sent(monkeypatch):
    fake = _turn(monkeypatch)
    received = fake.calls[1][1]["kg_context"]
    use, result = _messages()

    events = _client_events(_runtime_frames([use, result]))
    start = next(e for e in events if isinstance(e, ToolUseEvent))
    done = next(e for e in events if isinstance(e, ToolResultEvent))

    routing = _sse(done).get("route_context")
    assert routing == block(received)
    assert f"\ncanonical_concept: {WORLD.concept}\n" in routing
    assert f"\n{WORLD.join_line}\n" in routing
    # raw args stay the model's own arguments.
    assert _sse(start)["input"] == {"question": QUESTION}
    # The message the agent keeps for its conversation is not changed.
    assert result["content"][0]["toolResult"].keys() == {"toolUseId", "status", "content"}


def test_a_call_the_merge_left_alone_carries_no_route_context(monkeypatch):
    seed(WORLD)
    fake = Client()
    client = gateway(monkeypatch, fake)
    model_route = (f"```ROUTE:\ntarget_system: {WORLD.target}\ntranslated_identifiers: R-20\n"
                   f"canonical_concept: {WORLD.concept}\nscope: site 2\njoin_rule: none\n```")
    stream(client, LINE_TOOL, {"question": QUESTION, "kg_context": model_route}, "t-target")

    done = next(e for e in _client_events(_runtime_frames(_messages()))
                if isinstance(e, ToolResultEvent))
    assert "route_context" not in _sse(done)


def test_integrity_mode_forwards_no_route_context(monkeypatch):
    _turn(monkeypatch)
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    frames = _runtime_frames(_messages())
    assert "adtRouteContext" not in json.dumps(frames)
    done = next(e for e in _client_events(frames, integrity_mode=True)
                if isinstance(e, ToolResultEvent))
    assert "route_context" not in _sse(done)


def test_the_chat_client_never_forwards_it_in_integrity_mode(monkeypatch):
    _turn(monkeypatch)
    frames = _runtime_frames(_messages())
    done = next(e for e in _client_events(frames, integrity_mode=True)
                if isinstance(e, ToolResultEvent))
    assert "route_context" not in _sse(done)
