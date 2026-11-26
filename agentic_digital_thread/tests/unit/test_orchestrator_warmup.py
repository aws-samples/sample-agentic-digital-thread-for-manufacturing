# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator warmup ping is answered without a model turn.

The app pings the orchestrator runtime every 180 s (server._warmup_loop). Each
ping used to run a full Claude Sonnet turn (~37,000 input tokens, ~480 turns a
day with no users, measured in CloudWatch AWS/Bedrock). The runtime seam
``_runtime_entrypoint`` now answers the ping itself (agents/warmup.py).

Offline: no AWS, no network.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from agents import runtime_app
from agents.warmup import WARMUP_PROMPT, WARMUP_USER_ID, is_warmup_payload


def _drain(payload: dict) -> tuple[list[dict], list[dict]]:
    """Run the selected entrypoint; return (events, payloads the legacy turn saw)."""
    seen: list[dict] = []

    async def legacy(p, context):
        seen.append(p)
        yield {"data": "model answer"}

    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())
    events: list[dict] = []

    async def run():
        async for event in selected(payload, None):
            events.append(event)

    asyncio.run(run())
    return events, seen


def test_warmup_ping_is_answered_without_a_model_turn():
    events, seen = _drain({"prompt": WARMUP_PROMPT, "userId": WARMUP_USER_ID,
                           "sessionId": "s-1"})
    assert events == [{"warmup": "ok"}]
    assert seen == []  # the orchestrator turn (and its model call) never ran


def test_a_user_typing_ping_still_gets_a_normal_turn(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    events, seen = _drain({"prompt": "ping", "userId": "user-123", "sessionId": "s-2"})
    assert len(seen) == 1
    assert {"data": "model answer"} in events


def test_is_warmup_payload_needs_both_fields():
    ping = {"prompt": "ping", "userId": "warmup", "sessionId": "s"}
    assert is_warmup_payload(ping)
    assert not is_warmup_payload({**ping, "userId": "someone"})
    assert not is_warmup_payload({**ping, "prompt": "hello"})
    assert not is_warmup_payload({"prompt": "ping", "userId": "warmup"})
    assert not is_warmup_payload(None)


def test_server_sends_exactly_the_payload_the_runtime_recognizes(monkeypatch):
    """The sender and the receiver share one contract (agents/warmup.py)."""
    import boto3

    from agents import server

    sent: list[bytes] = []

    class _FakeClient:
        def invoke_agent_runtime(self, **kwargs):
            sent.append(kwargs["payload"].read())
            return {}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeClient())
    cfg = SimpleNamespace(agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:"
                          "111122223333:runtime/adt_orchestrator-x", aws_region="us-east-1")
    server._invoke_orchestrator_warmup(cfg)
    assert len(sent) == 1
    assert is_warmup_payload(json.loads(sent[0]))


def test_cutting_the_stream_closes_the_turn_at_once():
    """A client that hangs up mid-answer closes the turn, not the GC later."""
    closed: list[bool] = []

    async def legacy(p, context):
        try:
            yield {"data": "first"}
            yield {"data": "second"}
        finally:
            closed.append(True)

    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())

    async def run():
        stream = selected({"prompt": "q", "userId": "user-123", "sessionId": "s-3"}, None)
        assert await stream.__anext__() == {"data": "first"}
        await stream.aclose()

    asyncio.run(run())
    assert closed == [True]


def test_a_user_turn_with_the_warmup_user_id_is_not_a_warmup():
    """/api/ask/sync takes user_id from the request body, so a caller can send
    userId "warmup". A real turn's payload carries more fields than the ping
    (plant, writeMemory, rawPrompt), so it is never answered as a warmup."""
    user_turn = {"prompt": "ping", "userId": "warmup", "sessionId": "s-4",
                 "plant": "", "writeMemory": False, "rawPrompt": "ping"}
    assert not is_warmup_payload(user_turn)
    events, seen = _drain(user_turn)
    assert len(seen) == 1
