# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unbuffered SSE read contract (2026-07-16 buffering incident).

botocore's default ``iter_lines()`` drains the HTTP body in 1KB chunks; during
the tool-composition phase the runtime emits only sparse, tiny SSE lines, so a
chunk took 25–30s to FILL and tool events reached the browser that late
(MEASURED). ``AgentCoreChatClient.stream`` now requests ``chunk_size=1`` and
falls back to the default reader for streams without the kwarg.

Three guards, fully offline:

1. the client REQUESTS ``chunk_size=1`` (the unbuffered read cannot silently
   regress to the 1KB default);
2. the fallback path (no ``chunk_size`` support) still streams the same
   events — the existing test doubles across the suite rely on this;
3. the SSE line grammar survives a REAL ``botocore.response.StreamingBody``
   read byte-by-byte: multi-event, split-mid-line chunks from the raw socket,
   and CRLF line endings.
"""
import io
import json
from unittest.mock import MagicMock, patch

from agents.chat_client import AgentCoreChatClient
from agents.events import MessageEvent, ToolResultEvent, ToolUseEvent


def _run_stream(fake_stream):
    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": fake_stream}
    with patch("agents.chat_client.boto3.client", return_value=fake_client), \
         patch("agents.chat_client.get_config", return_value=MagicMock()):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x",
            region="us-east-1",
        )
        return list(client.stream(prompt="hi", session_id="s", user_id="u"))


# ── 1. the client requests chunk_size=1 ────────────────────────────────────

class _RecordingStream:
    """Fake stream that records the chunk_size the client asked for."""

    def __init__(self, lines):
        self._lines = lines
        self.requested_chunk_size = None

    def iter_lines(self, chunk_size=1024):
        self.requested_chunk_size = chunk_size
        return iter(self._lines)


def test_client_requests_unbuffered_chunk_size_1():
    stream = _RecordingStream([b'data: {"data": "hello"}'])
    emitted = _run_stream(stream)
    assert stream.requested_chunk_size == 1, (
        "unbuffered read regressed: iter_lines must be called with chunk_size=1 "
        "(default 1KB chunking delayed sparse tool events 25-30s)"
    )
    assert any(isinstance(e, MessageEvent) and e.content == "hello" for e in emitted)


# ── 2. fallback for streams without chunk_size support ─────────────────────

class _LegacyStream:
    """Stream whose iter_lines takes NO kwargs (test doubles / old botocore)."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


def test_fallback_to_default_iter_lines_still_streams():
    emitted = _run_stream(_LegacyStream([b'data: {"data": "hi"}']))
    assert any(isinstance(e, MessageEvent) and e.content == "hi" for e in emitted)


# ── 3. grammar over a REAL botocore StreamingBody, byte-by-byte ────────────

class _ChunkyRaw(io.RawIOBase):
    """File-like whose read(n) returns at most 7 bytes — forces every SSE line
    to be split mid-line across many reads, like a slow socket."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        if n is None or n < 0:
            return self._buf.read()
        return self._buf.read(min(n, 7))

    def readable(self):
        return True


def _streaming_body(data: bytes):
    from botocore.response import StreamingBody
    return StreamingBody(_ChunkyRaw(data), content_length=len(data))


def test_grammar_multi_event_split_mid_line_crlf():
    # Multi-event SSE wire with CRLF endings and lines far longer than the
    # 7-byte raw-read chunks (split-mid-line). Grammar must be preserved.
    tool_start = {"current_tool_use": {"toolUseId": "gf1", "name": "explore_graph"}}
    result = {"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": "gf1", "content": [{"text": "42 rows from erp.parts"}]}},
    ]}}
    wire = (
        b"data: " + json.dumps({"data": "Working"}).encode() + b"\r\n\r\n"
        b"data: " + json.dumps(tool_start).encode() + b"\r\n\r\n"
        b": keepalive comment line\r\n"
        b"data: " + json.dumps(result).encode() + b"\r\n\r\n"
        b"data: " + json.dumps({"data": " on it"}).encode() + b"\n\n"
    )
    emitted = _run_stream(_streaming_body(wire))

    texts = [e.content for e in emitted if isinstance(e, MessageEvent)]
    assert texts == ["Working", " on it"]
    starts = [e for e in emitted if isinstance(e, ToolUseEvent) and e.tool_use_id == "gf1"]
    assert len(starts) == 1 and starts[0].tool_name == "explore_graph"
    results = [e for e in emitted if isinstance(e, ToolResultEvent) and e.tool_use_id == "gf1"]
    assert len(results) == 1
    assert results[0].tool_name == "explore_graph"


def test_streaming_body_honors_chunk_size_1_directly():
    # Pin the botocore contract the client relies on: iter_lines(chunk_size=1)
    # yields each line without waiting for a 1KB chunk to fill.
    body = _streaming_body(b"data: {\"a\": 1}\r\ndata: {\"b\": 2}\n")
    lines = list(body.iter_lines(chunk_size=1))
    assert lines == [b'data: {"a": 1}', b'data: {"b": 2}']
