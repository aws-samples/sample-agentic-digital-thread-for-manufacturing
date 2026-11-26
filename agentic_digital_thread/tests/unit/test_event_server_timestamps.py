# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side event timestamps + envelope logging (streaming forensics wave).

* ``ToolUseEvent``/``ToolResultEvent`` carry an optional server ``ts`` (epoch
  ms) and the result an optional ``elapsed_ms`` — OMITTED from the SSE payload
  when None so old frames stay byte-identical.
* ``AgentCoreChatClient`` stamps ts at tool_start composition-emit and
  tool_result receipt, pairing ``elapsed_ms`` by toolUseId at the emitter.
* ``log_tool_result_envelope`` logs toolUseId/status/bounded structural shape,
  NEVER raw payload text, and NEVER raises.

Fully offline (mocked runtime stream, no AWS / network).
"""
import json
import logging
import os
from unittest.mock import MagicMock, patch

from agents.chat_client import AgentCoreChatClient, _dict_to_sse_event
from agents.events import (
    ENVELOPE_PREVIEW_CHARS, ToolResultEvent, ToolUseEvent,
    log_tool_result_envelope,
)


def _payload(frame: str) -> dict:
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no data line in {frame!r}")


# ── dataclass / SSE payload contract ────────────────────────────────────────

def test_ts_omitted_when_none_backcompat():
    start = _payload(ToolUseEvent(tool_name="t").to_sse_format())
    result = _payload(ToolResultEvent(tool_name="t").to_sse_format())
    assert "ts" not in start
    assert "ts" not in result and "elapsed_ms" not in result


def test_ts_and_elapsed_present_when_set():
    start = _payload(ToolUseEvent(tool_name="t", ts=1752600000123).to_sse_format())
    assert start["ts"] == 1752600000123
    result = _payload(ToolResultEvent(tool_name="t", ts=1752600004123,
                                      elapsed_ms=4000).to_sse_format())
    assert result["ts"] == 1752600004123
    assert result["elapsed_ms"] == 4000


# ── cloud path: stamping + pairing by toolUseId ─────────────────────────────

class _FakeStream:
    def __init__(self, events):
        self._lines = [b"data: " + json.dumps(e).encode() for e in events]

    def iter_lines(self, chunk_size=1024):
        return iter(self._lines)


def _run_stream(events):
    fake_client = MagicMock()
    fake_client.invoke_agent_runtime.return_value = {"response": _FakeStream(events)}
    with patch.dict(os.environ, {"SYSTEM_QUERY_MODE": "orchestrator"}), \
         patch("agents.chat_client.boto3.client", return_value=fake_client), \
         patch("agents.chat_client.get_config", return_value=MagicMock()):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x",
            region="us-east-1",
        )
        return list(client.stream(prompt="hi", session_id="s", user_id="u"))


def test_cloud_stream_stamps_ts_and_pairs_elapsed():
    events = [
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp",
                         "input": {"query": "SELECT 1"}}},
        ]}},
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "status": "success",
                            "content": [{"text": "3 rows"}]}},
        ]}},
    ]
    emitted = _run_stream(events)
    starts = [e for e in emitted if isinstance(e, ToolUseEvent) and e.tool_use_id == "t1"]
    results = [e for e in emitted if isinstance(e, ToolResultEvent) and e.tool_use_id == "t1"]
    assert len(starts) == 1 and len(results) == 1
    assert isinstance(starts[0].ts, int) and starts[0].ts > 1_600_000_000_000
    assert isinstance(results[0].ts, int) and results[0].ts >= starts[0].ts
    assert isinstance(results[0].elapsed_ms, int)
    assert results[0].elapsed_ms == results[0].ts - starts[0].ts


def test_synthetic_graph_first_event_gets_ts():
    # The runtime's synthetic graph-first seed flows through the immediate-emit
    # branch (no input key) and must be stamped like every other tool_start.
    emitted = _run_stream([
        {"current_tool_use": {"toolUseId": "gf1", "name": "explore_graph"}},
    ])
    starts = [e for e in emitted if isinstance(e, ToolUseEvent) and e.tool_use_id == "gf1"]
    assert len(starts) == 1
    assert isinstance(starts[0].ts, int)


def test_parallel_results_each_pair_their_own_elapsed():
    events = [
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "a", "name": "query_erp", "input": {"q": 1}}},
            {"toolUse": {"toolUseId": "b", "name": "query_mes", "input": {"q": 2}}},
        ]}},
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "a", "content": [{"text": "1 row"}]}},
            {"toolResult": {"toolUseId": "b", "content": [{"text": "2 rows"}]}},
        ]}},
    ]
    emitted = _run_stream(events)
    results = {e.tool_use_id: e for e in emitted if isinstance(e, ToolResultEvent)}
    assert set(results) >= {"a", "b"}
    for tid in ("a", "b"):
        assert isinstance(results[tid].ts, int)
        assert isinstance(results[tid].elapsed_ms, int)
        assert results[tid].elapsed_ms >= 0


# ── local path: queue dicts carry ts through _dict_to_sse_event ─────────────

def test_dict_to_sse_event_passes_ts_and_elapsed_through():
    start = _dict_to_sse_event({"event": "tool_start", "data": {
        "tool": "t", "tool_id": "x", "input": {}, "ts": 123,
    }})
    assert isinstance(start, ToolUseEvent) and start.ts == 123
    result = _dict_to_sse_event({"event": "tool_result", "data": {
        "tool": "t", "tool_id": "x", "summary": "", "input": {},
        "ts": 200, "elapsed_ms": 77,
    }})
    assert isinstance(result, ToolResultEvent)
    assert result.ts == 200 and result.elapsed_ms == 77


def test_local_hook_stamps_ts_on_tool_result():
    import queue as _queue
    from agents.chat_client import _ToolResultHook
    q: _queue.Queue = _queue.Queue()
    hook = _ToolResultHook(q)
    fake_event = MagicMock()
    fake_event.tool_use = {"name": "query_erp", "toolUseId": "t9", "input": {}}
    fake_event.result = {"status": "success", "content": [{"text": "ok"}]}
    hook._on_tool_done(fake_event)
    item = q.get_nowait()
    assert item["event"] == "tool_result"
    assert isinstance(item["data"]["ts"], int)


# ── envelope logging: capped + never raises ─────────────────────────────────

def test_envelope_log_is_shape_only_and_bounded(caplog):
    log = logging.getLogger("test_envelope")
    huge = "raw-secret-payload-" * 600
    with caplog.at_level(logging.INFO, logger="test_envelope"):
        log_tool_result_envelope(log, "t1", "success", huge)
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "id_hash=" in msg and "status=success" in msg
    assert "id=t1" not in msg
    assert f"str({len(huge)})" in msg
    assert "raw-secret-payload" not in msg
    assert len(caplog.records[0].args[-1]) <= ENVELOPE_PREVIEW_CHARS


def test_envelope_log_shapes(caplog):
    log = logging.getLogger("test_envelope_shapes")
    with caplog.at_level(logging.INFO, logger="test_envelope_shapes"):
        log_tool_result_envelope(log, "a", "success", [{"text": "hi"}])
        log_tool_result_envelope(log, "b", "error", {"content": [], "status": "error"})
        log_tool_result_envelope(log, "c", "", None)
    msgs = [r.getMessage() for r in caplog.records]
    assert "shape=list[1]" in msgs[0]
    assert "shape=dict(" in msgs[1]
    assert "shape=none" in msgs[2]


def test_envelope_log_never_raises():
    class _Nasty:
        def __str__(self):
            raise RuntimeError("boom")

    log = logging.getLogger("test_envelope_nasty")
    # Must swallow — a forensics line can never break a live stream.
    log_tool_result_envelope(log, "t1", "success", _Nasty())
    log_tool_result_envelope(None, "t1", "success", "ok")  # even a broken logger


def test_cloud_stream_emits_envelope_log(caplog):
    events = [
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_erp", "input": {}}},
        ]}},
        {"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "status": "success",
                            "content": [{"text": "3 rows"}]}},
        ]}},
    ]
    with caplog.at_level(logging.INFO, logger="agentcore_client"):
        _run_stream(events)
    envelope = [r for r in caplog.records if "TOOL_RESULT_ENVELOPE" in r.getMessage()]
    assert envelope, "expected a TOOL_RESULT_ENVELOPE line at ToolResultEvent creation"
    assert "id_hash=" in envelope[0].getMessage()
    assert "id=t1" not in envelope[0].getMessage()
