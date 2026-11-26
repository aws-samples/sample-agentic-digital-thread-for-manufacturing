# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1I — silent-stall backend: reasoning trace / heartbeat SSE.

Capture-first verdict (2026-07-15, strands==1.42.0 site-packages):

- Strands DOES define reasoning deltas: ``ReasoningTextStreamEvent`` emits
  ``{"reasoningText": str, "delta": ..., "reasoning": True}``
  (strands/types/_events.py:169–174), produced by
  ``handle_content_block_delta`` only when the model streams
  ``reasoningContent`` (extended thinking).
- The deployed orchestrator model has NO thinking config (plain model-id
  string, orchestrator.py:1499) AND the runtime_app stream filter dropped any
  such key → NO reasoning deltas on today's wire. The code-verified stall is
  the chat_client DEFERRAL seam: streaming ``current_tool_use`` events
  (partial JSON input) are deferred until the assistant message block, so a
  long input-composition window emits NOTHING, then N tool_starts land at once.

Implementation therefore does BOTH additive halves:
- the deferral-seam heartbeat (``preparing <tool> query…``, tool NAME only) —
  fixes today's stall; and
- ``reasoningText`` forwarding (runtime filter → ``event: reasoning``) — lights
  up automatically if extended thinking is ever enabled.

tool_start/token semantics must stay byte-identical (regression-pinned below).
Pattern: tests/unit/test_chat_client_no_text_dup.py.
"""
import json
import os
import queue
from unittest.mock import MagicMock, patch

from agents.chat_client import AgentCoreChatClient, _SSECallbackHandler, _dict_to_sse_event
from agents.events import (
    MessageEvent,
    ReasoningEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from agents.runtime_app import _reasoning_delta_text


# ── Harness (pattern: test_chat_client_no_text_dup.py) ───────────────────────

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
    with patch.dict(os.environ, {"SYSTEM_QUERY_MODE": "orchestrator"}), \
         patch("agents.chat_client.boto3.client", return_value=fake_client), \
         patch("agents.chat_client.get_config", return_value=MagicMock()):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            region="us-east-1",
        )
        return list(client.stream(prompt="hi", session_id="s", user_id="u"))


def _joined_text(emitted):
    return "".join(e.content for e in emitted if isinstance(e, MessageEvent))


# ── (c) ReasoningEvent SSE shape pinned ───────────────────────────────────────

def test_reasoning_event_to_sse_format_shape():
    ev = ReasoningEvent(text="preparing query_erp query…")
    sse = ev.to_sse_format()
    assert sse.startswith("event: reasoning\ndata: ")
    assert sse.endswith("\n\n")
    payload = json.loads(sse.split("\ndata: ", 1)[1])
    assert payload == {"text": "preparing query_erp query…"}


# ── (a) Forwarded reasoningText parses to ReasoningEvent (cloud path) ────────

def test_reasoning_text_between_tool_batches_yields_reasoning_events():
    # tool_result → reasoning deltas → next tool batch: the former silent gap.
    events = [
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "42 rows"}]}}
        ]}},
        {"reasoningText": "Now I should cross-check the MES"},
        {"reasoningText": " for the same work order"},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t2", "name": "query_mes", "input": {"query": "SELECT 1"}}},
        ]}},
    ]
    emitted = _run_stream(events)
    kinds = [type(e).__name__ for e in emitted]
    r_idx = [i for i, e in enumerate(emitted) if isinstance(e, ReasoningEvent)]
    tr_idx = kinds.index("ToolResultEvent")
    tu_idx = next(i for i, e in enumerate(emitted)
                  if isinstance(e, ToolUseEvent) and e.tool_use_id == "t2")
    assert len(r_idx) == 2
    assert all(tr_idx < i < tu_idx for i in r_idx), "reasoning must fill the gap between batches"
    texts = [e.text for e in emitted if isinstance(e, ReasoningEvent)]
    assert texts == ["Now I should cross-check the MES", " for the same work order"]


def test_reasoning_text_never_enters_token_stream():
    events = [
        {"reasoningText": "secret chain of thought"},
        {"data": "The answer is 42."},
    ]
    emitted = _run_stream(events)
    assert _joined_text(emitted) == "The answer is 42."
    assert any(isinstance(e, ReasoningEvent) for e in emitted)


# ── (d) Deferral-seam heartbeat ───────────────────────────────────────────────

def test_first_deferred_delta_emits_exactly_one_heartbeat_name_only():
    partial1 = '{"qu'
    partial2 = '{"query": "SELECT'
    events = [
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": partial1}},
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": partial2}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp",
                         "input": {"query": "SELECT 1"}}},
        ]}},
    ]
    emitted = _run_stream(events)
    heartbeats = [e for e in emitted if isinstance(e, ReasoningEvent)]
    assert len(heartbeats) == 1, "exactly ONE heartbeat per toolUseId"
    assert heartbeats[0].text == "preparing query_erp query…"
    # Never the partial JSON:
    assert partial1 not in heartbeats[0].text
    assert partial2 not in heartbeats[0].text


def test_parallel_tools_get_one_heartbeat_each():
    events = [
        {"current_tool_use": {"toolUseId": "a", "name": "query_erp", "input": '{"q'}},
        {"current_tool_use": {"toolUseId": "b", "name": "query_mes", "input": '{"q'}},
        {"current_tool_use": {"toolUseId": "a", "name": "query_erp", "input": '{"que'}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "a", "name": "query_erp", "input": {"query": "1"}}},
            {"toolUse": {"toolUseId": "b", "name": "query_mes", "input": {"query": "2"}}},
        ]}},
    ]
    emitted = _run_stream(events)
    texts = [e.text for e in emitted if isinstance(e, ReasoningEvent)]
    assert texts == ["preparing query_erp query…", "preparing query_mes query…"]


def test_synthetic_tool_use_without_input_gets_no_heartbeat():
    # Graph-first seed events carry no streaming input string → emitted
    # immediately as tool_start, NO heartbeat (nothing is deferred).
    events = [
        {"current_tool_use": {"toolUseId": "gf1", "name": "explore_graph"}},
    ]
    emitted = _run_stream(events)
    assert not any(isinstance(e, ReasoningEvent) for e in emitted)
    assert any(isinstance(e, ToolUseEvent) and e.tool_use_id == "gf1" for e in emitted)


# ── (b) tool_start deferral contract stays byte-identical ────────────────────

def test_deferral_still_emits_exactly_one_tool_start_with_complete_input():
    complete_input = {"query": "SELECT * FROM work_orders"}
    events = [
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": '{"qu'}},
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp",
                              "input": '{"query": "SELECT * FROM'}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp", "input": complete_input}},
        ]}},
    ]
    emitted = _run_stream(events)
    starts = [e for e in emitted if isinstance(e, ToolUseEvent) and e.tool_use_id == "t1"]
    assert len(starts) == 1, "exactly one tool_start per toolUseId"
    assert starts[0].tool_input == complete_input, "COMPLETE parsed input, not the partial string"


def test_heartbeat_does_not_break_tool_result_flow():
    events = [
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": '{"q'}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp", "input": {"query": "1"}}},
        ]}},
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "3 rows"}]}}
        ]}},
    ]
    emitted = _run_stream(events)
    assert any(isinstance(e, ToolResultEvent) and e.tool_use_id == "t1" for e in emitted)
    assert len([e for e in emitted if isinstance(e, ReasoningEvent)]) == 1


# ── Local path: _SSECallbackHandler + _dict_to_sse_event ─────────────────────

def test_local_callback_forwards_reasoning_text_on_reasoning_channel():
    q: queue.Queue = queue.Queue()
    handler = _SSECallbackHandler(q)
    handler(reasoningText="thinking about joins", reasoning=True,
            delta={"reasoningContent": {"text": "thinking about joins"}})
    item = q.get_nowait()
    assert item == {"event": "reasoning", "data": {"text": "thinking about joins"}}
    assert q.empty(), "reasoning kwarg must not also produce token/tool events"


def test_local_callback_token_and_tool_semantics_unchanged():
    q: queue.Queue = queue.Queue()
    handler = _SSECallbackHandler(q)
    handler(data="hello")
    handler(current_tool_use={"toolUseId": "t1", "name": "query_erp", "input": "{}"})
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert [i["event"] for i in items] == ["token", "tool_start"]
    assert items[0]["data"] == {"text": "hello"}


def test_local_integrity_callback_suppresses_raw_reasoning_and_tokens():
    q: queue.Queue = queue.Queue()
    handler = _SSECallbackHandler(q, emit_tokens=False)
    sensitive = "person@example.com password=do-not-stream"
    handler(
        reasoningText=sensitive, reasoning=True,
        delta={"reasoningContent": {"text": sensitive}}, data=sensitive,
    )
    assert q.empty()


def test_dict_to_sse_event_maps_reasoning_not_message_fallback():
    ev = _dict_to_sse_event({"event": "reasoning", "data": {"text": "hmm"}})
    assert isinstance(ev, ReasoningEvent)
    assert ev.text == "hmm"
    assert not isinstance(ev, MessageEvent), "must never fall through to the token stream"


# ── Runtime stream-filter decision (pure helper) ─────────────────────────────

def test_reasoning_delta_text_forwards_capped_text():
    assert _reasoning_delta_text({"reasoningText": "abc", "reasoning": True}) == "abc"
    long = "x" * 5000
    assert _reasoning_delta_text({"reasoningText": long}) == "x" * 2000


def test_reasoning_delta_text_skips_non_text_reasoning_events():
    # Signature / redacted-content reasoning events carry no forwardable text.
    assert _reasoning_delta_text({"reasoning_signature": "sig", "reasoning": True}) is None
    assert _reasoning_delta_text({"reasoningText": ""}) is None
    assert _reasoning_delta_text({"reasoningText": None}) is None
    assert _reasoning_delta_text({"reasoningText": b"redacted"}) is None
    assert _reasoning_delta_text({}) is None
