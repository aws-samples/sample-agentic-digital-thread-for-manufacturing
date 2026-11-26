# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Direct cloud Mode A stream-ordering regressions."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import agents.orchestrator as orchestrator
import agents.runtime_app as runtime_app
from agents.response_integrity import FinalAnswerDraft
from agents.runtime_integrity import BoundedTextBuffer, safe_tool_progress


class _FakeRuntimeApp:
    last = None

    def __init__(self):
        self.invoke = None
        _FakeRuntimeApp.last = self

    def entrypoint(self, fn):
        self.invoke = fn
        return fn

    def run(self):
        return None


class _FakeAgent:
    async def stream_async(self, _prompt, **kwargs):
        assert kwargs["structured_output_model"] is FinalAnswerDraft
        yield {"data": "RAW MODEL PII person@example.com password=hunter2"}
        yield {"message": {"role": "assistant", "content": [
            {"text": "RAW TERMINAL MESSAGE token=secret"},
        ]}}
        yield {"current_tool_use": {
            "toolUseId": "tool-1", "name": "system-a___query",
            "input": '{"identifier":"private-id"}',
        }}
        yield {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tool-1", "status": "success",
            "content": [{"text": json.dumps({
                "columns": ["measure"], "rows": [[999]], "row_count": 1,
                "password": "raw-secret",
            })}],
        }}]}}
        yield {"result": SimpleNamespace(structured_output=None)}


def test_cloud_mode_a_streams_only_tool_metadata_before_gate(monkeypatch):
    fake_module = SimpleNamespace(BedrockAgentCoreApp=_FakeRuntimeApp)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", fake_module)
    monkeypatch.setattr(orchestrator, "build_orchestrator", lambda **_kwargs: _FakeAgent())
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    # Enforce-path semantics (ADR-0046) — opt in per ADR-0047.
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setattr(runtime_app, "_record_rule_usage_hook", lambda *_a, **_k: None)

    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke

    async def collect_events():
        return [event async for event in invoke(
            {"prompt": "What is the current measure?", "sessionId": "s", "userId": "u"},
            SimpleNamespace(session_id="runtime"),
        )]

    events = asyncio.run(collect_events())

    first_answer = next(i for i, event in enumerate(events) if "data" in event)
    for event in events[:first_answer]:
        assert set(event).issubset({"current_tool_use", "tool_result_metadata"})
    serialized = json.dumps(events)
    for forbidden in (
        "person@example.com", "hunter2", "RAW TERMINAL", "private-id",
        "raw-secret", "[[999]]",
    ):
        assert forbidden not in serialized
    assert "No requested value passed deterministic" in events[first_answer]["data"]
    integrity = next(event["response_integrity"] for event in events
                     if "response_integrity" in event)
    assert integrity["verdict"] == "rejected"


def test_safe_tool_progress_drops_raw_messages_and_arguments():
    assert safe_tool_progress({"message": {"role": "assistant", "content": "raw"}}) is None
    event = safe_tool_progress({"current_tool_use": {
        "toolUseId": "id", "name": "system___query", "input": "private",
    }})
    assert event == {"current_tool_use": {
        "toolUseId": "id", "name": "system___query", "input": {},
    }}


def test_private_response_buffer_is_byte_bounded():
    buffer = BoundedTextBuffer(limit=8)
    buffer.append("1234")
    buffer.append("abcdefgh")
    buffer.append("never")
    assert len(buffer.text().encode()) <= 8
    assert buffer.truncated is True
