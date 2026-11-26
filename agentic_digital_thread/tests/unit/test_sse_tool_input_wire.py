# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wire contract: the streamed ``tool_start`` carries the tool's FULL input args.

Feature 1 surfaces each tool call's input arguments in the reasoning trace. The
frontend stashes ``data.input`` from the ``tool_start`` event. Strands streams
``current_tool_use`` with the input as a PARTIAL JSON *string* that accumulates
over deltas, while the COMPLETE parsed input dict arrives in the assistant
``message`` toolUse block. The client must:

  * DEFER the partial-string ``current_tool_use`` events (not pre-claim the id
    with empty args), and
  * emit exactly ONE ``tool_start`` per tool carrying the complete input dict
    from the message toolUse block,

so the earlier bug — every ``tool_start`` streamed with ``input=null`` because
the null ``current_tool_use`` claimed the id and the input-bearing block was
deduped away — cannot regress.

Drives ``AgentCoreChatClient.stream`` with a mocked runtime stream (fully
offline; no AWS / network).
"""
import json
import os
from unittest.mock import MagicMock, patch

from agents.chat_client import AgentCoreChatClient
from agents.events import ToolUseEvent


class _FakeStream:
    """Mimics the AgentCore SSE response: ``iter_lines()`` over ``data: {...}``."""

    def __init__(self, events):
        self._lines = [b"data: " + json.dumps(e).encode() for e in events]

    def iter_lines(self):
        return iter(self._lines)


def _run_stream(events):
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


def _tool_starts(emitted):
    return [e for e in emitted if isinstance(e, ToolUseEvent)]


def test_tool_start_carries_full_input_from_message_block():
    # Realistic ordering: an empty current_tool_use (input="") arrives first, then
    # partial-string accumulation, THEN the assistant message toolUse block with
    # the complete parsed input dict, then the tool result.
    full_input = {"query": "SELECT part_id FROM erp.parts WHERE status='OPEN'"}
    events = [
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": ""}},
        {"current_tool_use": {"toolUseId": "t1", "name": "query_erp", "input": '{"query":"SELECT'}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp", "input": full_input}},
        ]}},
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "3 rows"}]}},
        ]}},
    ]
    starts = _tool_starts(_run_stream(events))
    # Exactly ONE tool_start for t1 — no duplicate pill.
    t1_starts = [s for s in starts if s.tool_use_id == "t1"]
    assert len(t1_starts) == 1, f"expected 1 tool_start for t1, got {len(t1_starts)}"
    # ...and it carries the COMPLETE parsed input dict (not None / not the "" string).
    assert t1_starts[0].tool_input == full_input


def test_tool_start_input_present_on_sse_wire():
    # End-to-end wire check: the tool_start SSE frame includes a non-null "input".
    full_input = {"asset_id": "PUMP-7", "metric": "vibration"}
    events = [
        {"current_tool_use": {"toolUseId": "t2", "name": "query_sitewise", "input": ""}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t2", "name": "query_sitewise", "input": full_input}},
        ]}},
    ]
    starts = [s for s in _tool_starts(_run_stream(events)) if s.tool_use_id == "t2"]
    assert len(starts) == 1
    frame = starts[0].to_sse_format()
    assert frame.startswith("event: tool_start")
    payload = json.loads(frame.splitlines()[1][len("data: "):])
    assert payload["input"] == full_input
    assert payload["input"] is not None


def test_synthetic_current_tool_use_without_input_still_emits_immediately():
    # A synthetic event (e.g. the deterministic graph-first seed) carries NO input
    # key and has no following message toolUse block; it must still emit a
    # tool_start so its pill renders.
    events = [
        {"current_tool_use": {"toolUseId": "gf1", "name": "explore_graph"}},
    ]
    starts = [s for s in _tool_starts(_run_stream(events)) if s.tool_use_id == "gf1"]
    assert len(starts) == 1
    assert starts[0].tool_name == "explore_graph"


def test_parallel_tooluse_blocks_each_get_full_input():
    # Parallel batch: two tools stream empty current_tool_use events, then a single
    # assistant message carries both toolUse blocks with their full inputs.
    in_a = {"query": "SELECT 1"}
    in_b = {"query": "SELECT 2"}
    events = [
        {"current_tool_use": {"toolUseId": "a", "name": "query_erp", "input": ""}},
        {"current_tool_use": {"toolUseId": "b", "name": "query_mes", "input": ""}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "a", "name": "query_erp", "input": in_a}},
            {"toolUse": {"toolUseId": "b", "name": "query_mes", "input": in_b}},
        ]}},
    ]
    starts = _tool_starts(_run_stream(events))
    by_id = {s.tool_use_id: s.tool_input for s in starts if s.tool_use_id in ("a", "b")}
    assert by_id == {"a": in_a, "b": in_b}
