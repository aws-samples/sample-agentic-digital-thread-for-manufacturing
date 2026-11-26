# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression: AgentCoreChatClient must emit streamed assistant text exactly ONCE.

The cloud runtime forwards assistant text twice: incrementally as ``data`` /
``contentBlockDelta`` deltas AND, at the end of each turn, in full inside a
Strands ``message`` event. The client previously re-emitted the ``message``
event's ``text`` blocks as ``MessageEvent``s, so every reasoning block and the
final answer (including the ``---FOLLOWUPS---`` chips) rendered TWICE — the
"follow-up loop / weirdness" users reported.

These tests drive ``AgentCoreChatClient.stream`` with a mocked runtime stream and
assert the delta stream is the single source of streamed text, while ``message``
events still contribute tool use / tool result blocks.
"""
import json
from unittest.mock import MagicMock, patch

from agents.chat_client import AgentCoreChatClient
from agents.events import (
    MessageEvent,
    ToolResultEvent,
    ToolUseEvent,
)


class _FakeStream:
    """Mimics the AgentCore SSE response: ``iter_lines()`` over ``data: {...}``."""

    def __init__(self, events):
        self._lines = [b"data: " + json.dumps(e).encode() for e in events]

    def iter_lines(self):
        return iter(self._lines)


def _run_stream(events):
    """Run the client over a canned runtime event list; return emitted SSE events."""
    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": _FakeStream(events)}
    # Patch get_config too: AgentCoreChatClient.__init__ calls get_config(), which
    # runs live AgentCore discovery (config._discover_memory_id). Under the boto3
    # patch that discovery would hit a MagicMock — this is a unit test for stream
    # parsing, so stub the config out entirely (hermetic + fast; no AWS discovery).
    with patch("agents.chat_client.boto3.client", return_value=fake_client), \
         patch("agents.chat_client.get_config", return_value=MagicMock()):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            region="us-east-1",
        )
        return list(client.stream(prompt="hi", session_id="s", user_id="u"))


def _joined_text(emitted):
    return "".join(e.content for e in emitted if isinstance(e, MessageEvent))


def test_delta_text_not_duplicated_by_trailing_message_event():
    # Deltas stream "Hello world." then the terminal message repeats it verbatim.
    events = [
        {"data": "Hello "},
        {"data": "world."},
        {"message": {"role": "assistant", "content": [{"text": "Hello world."}]}},
    ]
    emitted = _run_stream(events)
    # Text must appear exactly ONCE — from the deltas, not re-emitted by the message.
    assert _joined_text(emitted) == "Hello world."


def test_followups_block_streamed_once():
    # A realistic final answer + FOLLOWUPS block, streamed as one delta then
    # repeated by the terminal message event.
    answer = "Bound 1200 books.\n---FOLLOWUPS---\nQ1: Break down by line?\nQ2: Waste rate?"
    events = [
        {"data": answer},
        {"message": {"role": "assistant", "content": [{"text": answer}]}},
    ]
    emitted = _run_stream(events)
    text = _joined_text(emitted)
    assert text == answer
    # Exactly one FOLLOWUPS marker / one Q1 → no duplicate chips downstream.
    assert text.count("---FOLLOWUPS---") == 1
    assert text.count("Q1:") == 1


def test_message_event_tool_result_still_emitted():
    # A message event carrying a single toolResult must still surface as a
    # ToolResultEvent (message events remain the carrier for tool use/results).
    events = [
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "42 rows"}]}}
        ]}},
    ]
    emitted = _run_stream(events)
    assert any(isinstance(e, ToolResultEvent) and e.tool_use_id == "t1" for e in emitted)


def test_message_event_with_text_and_tooluse_yields_tool_not_text():
    # Text is skipped (already streamed via deltas); the toolUse block is emitted.
    events = [
        {"message": {"role": "assistant", "content": [
            {"text": "let me query"},
            {"toolUse": {"toolUseId": "t9", "name": "explore_graph", "input": {}}},
        ]}},
    ]
    emitted = _run_stream(events)
    assert any(isinstance(e, ToolUseEvent) and e.tool_use_id == "t9" for e in emitted)
    assert _joined_text(emitted) == ""  # no duplicated reasoning text


def test_multiple_tool_results_in_one_message_all_emitted_no_text_dup():
    # Parallel batch: one message with >1 toolResult plus a stray text block.
    # All tool results emit; the text block is NOT re-emitted.
    events = [
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "a", "content": [{"text": "rows A"}]}},
            {"toolResult": {"toolUseId": "b", "content": [{"text": "rows B"}]}},
            {"text": "already streamed"},
        ]}},
    ]
    emitted = _run_stream(events)
    ids = {e.tool_use_id for e in emitted if isinstance(e, ToolResultEvent)}
    assert ids == {"a", "b"}
    assert _joined_text(emitted) == ""
