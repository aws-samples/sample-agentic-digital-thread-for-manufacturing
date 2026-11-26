# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract guard: the streamed tool events carry an ``input`` field.

Feature 1 surfaces each tool call's input arguments in the reasoning trace. The
frontend reads ``data.input`` from the ``tool_start`` event (the call arguments)
and stashes them to show alongside the result. This offline guard locks the SSE
wire contract so a future edit that drops the ``input`` key from either tool
event fails fast in the unit lane — before the trace silently loses the inputs.

Fully offline: exercises the dataclass ``to_sse_format`` only. No AWS / network.
"""
from __future__ import annotations

import json

from agents.events import ToolResultEvent, ToolUseEvent


def _parse_sse(frame: str) -> dict:
    """Return the JSON payload from a single ``event: ...\\ndata: {...}`` frame."""
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no data line in SSE frame: {frame!r}")


def test_tool_start_emits_input_args() -> None:
    frame = ToolUseEvent(
        tool_name="query_system_x",
        tool_input={"query": "SELECT 1"},
        tool_use_id="abc",
    ).to_sse_format()
    assert frame.startswith("event: tool_start")
    payload = _parse_sse(frame)
    assert "input" in payload
    assert payload["input"] == {"query": "SELECT 1"}


def test_tool_result_emits_input_key() -> None:
    frame = ToolResultEvent(
        tool_name="query_system_x",
        tool_result={"query": "SELECT 1"},
        tool_use_id="abc",
        summary="1 record found",
    ).to_sse_format()
    assert frame.startswith("event: tool_result")
    payload = _parse_sse(frame)
    assert "input" in payload
    assert payload["input"] == {"query": "SELECT 1"}


def test_tool_start_input_defaults_to_none_not_missing() -> None:
    # Backward-compatible: the key is always present even with no args.
    payload = _parse_sse(ToolUseEvent(tool_name="t").to_sse_format())
    assert "input" in payload
    assert payload["input"] is None
