# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""App-side handling of the runtime keepalive heartbeat frame.

The runtime emits ``data: {"adt_heartbeat": 1}`` lines while its event stream
is idle (see test_runtime_stream_heartbeat). On the app side the heartbeat must
be invisible: it produces NO ``SSEEvent``, does not count as a parse result,
error, progress event, or stream content (bytes / line_count / progress limit),
and every other reader of the raw runtime stream ignores it too.

These tests also pin the diagnostics upgrade: the two QUERY_FAILURE log lines in
``chat_client`` carry the exception CLASS NAME so a stream read-timeout is
distinguishable from a reset connection or an incomplete read.

Offline: no AWS, no network.
"""
from __future__ import annotations

import json
import logging

import pytest

from agents.chat_client import (
    ThinkingFilter,
    _CloudStreamState,
    _stream_cloud_response,
)
from agents.events import DoneEvent, ErrorEvent, MessageEvent


HEARTBEAT_LINE = 'data: ' + json.dumps({"adt_heartbeat": 1})


class _Log:
    def __init__(self):
        self.records = []

    def info(self, *a, **k):
        self.records.append(("info", a, k))

    def error(self, *a, **k):
        self.records.append(("error", a, k))

    def warning(self, *a, **k):
        self.records.append(("warning", a, k))

    def debug(self, *a, **k):
        self.records.append(("debug", a, k))


class _FakeStream:
    """Mimics the AgentCore SSE body: ``iter_lines()`` over raw byte lines."""

    def __init__(self, lines):
        self._lines = [ln.encode() if isinstance(ln, str) else ln for ln in lines]

    def iter_lines(self, chunk_size=1):
        yield from self._lines


def _new_state(integrity: bool = False):
    return _CloudStreamState(
        ThinkingFilter(), integrity_mode=integrity, log=_Log(),
        owner_id="u", session_id="s", runtime_session_id="r",
    )


def _render(events):
    return [(type(e).__name__, getattr(e, "content", getattr(e, "message", None)))
            for e in events]


REAL_LINES = [
    'data: ' + json.dumps({"data": "Answer "}),
    'data: ' + json.dumps({"data": "text."}),
    'data: ' + json.dumps({"intelligence_summary": {"total_tool_calls": 0}}),
]


# ---------------------------------------------------------------------------
# Heartbeat is invisible: identical SSEEvents with and without it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("integrity", [False, True])
def test_heartbeats_between_frames_yield_identical_sse_events(integrity):
    without = list(_new_state(integrity).iter_stream(_FakeStream(list(REAL_LINES))))

    interleaved = []
    for line in REAL_LINES:
        interleaved.append(HEARTBEAT_LINE)
        interleaved.append(line)
    interleaved.append(HEARTBEAT_LINE)
    with_hb = list(_new_state(integrity).iter_stream(_FakeStream(interleaved)))

    assert _render(with_hb) == _render(without)


def test_heartbeat_line_data_returns_none():
    state = _new_state()
    assert state._line_data(HEARTBEAT_LINE.encode()) is None


def test_heartbeat_does_not_count_as_bytes_lines_or_progress():
    state = _new_state(integrity=True)
    for _ in range(100):
        state._line_data(HEARTBEAT_LINE.encode())
    assert state.line_count == 0
    assert state.total_bytes == 0
    assert state.progress_events == 0
    assert state.progress_exceeded is False


def test_heartbeat_stamps_last_line_at_so_idle_s_measures_socket_silence():
    """The failure fingerprint's idle_s is time since the last line ARRIVED.
    A heartbeat is a line on the wire, so it resets that clock even though it
    is dropped before every other piece of accounting."""
    state = _new_state()
    state.last_line_at = 0.0
    state._line_data(HEARTBEAT_LINE.encode())
    assert state.last_line_at > 0.0
    assert state.line_count == 0
    assert state.last_frame == "none"


def test_many_heartbeats_do_not_trip_integrity_byte_cap():
    """A 30-min turn at 30s = ~60 tiny frames; even thousands must not trip the
    MAX_PUBLIC_STREAM_BYTES bound (heartbeats are dropped before accounting)."""
    state = _new_state(integrity=True)
    events = []
    for _ in range(5000):
        data = state._line_data(HEARTBEAT_LINE.encode())
        if data is not None:
            events.extend(state.events_for(data))
    assert events == []
    assert state.total_bytes == 0


def test_heartbeat_produces_no_event_via_events_for():
    state = _new_state()
    # Even if a heartbeat dict reaches events_for, it must yield nothing.
    assert state.events_for({"adt_heartbeat": 1}) == []


def test_heartbeat_not_written_to_capture(monkeypatch):
    captured = []

    class _Capture:
        capture_id = "cap"

        def write(self, line):
            captured.append(line)

        def close(self):
            pass

    monkeypatch.setattr(
        "agents.stream_capture.open_stream_capture",
        lambda **_k: _Capture(),
    )
    state = _new_state()
    state._line_data(HEARTBEAT_LINE.encode())
    assert captured == []


# ---------------------------------------------------------------------------
# QUERY_FAILURE log carries the exception class name
# ---------------------------------------------------------------------------

class _Boom(RuntimeError):
    pass


def test_stream_cloud_response_failure_logs_exception_class_name():
    log = _Log()
    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=False, log=log,
        owner_id="u", session_id="s", runtime_session_id="r",
    )

    class _BoomStream:
        def iter_lines(self, chunk_size=1):
            raise _Boom("read timed out")

    events = list(_stream_cloud_response({"response": _BoomStream()}, state))
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, DoneEvent) for e in events)
    failures = [r for r in log.records if r[0] == "error"
                and r[1] and "QUERY_FAILURE" in str(r[1][0])]
    assert failures, "expected a QUERY_FAILURE error log"
    rendered = failures[0][1][0] % failures[0][1][1:]
    assert "_Boom" in rendered


def test_stream_setup_failure_logs_exception_class_name(monkeypatch, caplog):
    from agents.chat_client import AgentCoreChatClient

    def boom_client(*_a, **_k):
        raise _Boom("connect reset")

    monkeypatch.setattr("agents.chat_client.boto3.client", boom_client)
    monkeypatch.setattr("agents.chat_client.get_config", lambda: __import__(
        "unittest.mock", fromlist=["MagicMock"]).MagicMock())
    with caplog.at_level(logging.ERROR, logger="agentcore_client"):
        events = list(AgentCoreChatClient(
            runtime_arn="arn:r", region="us-east-1").stream("p", "s", "u"))
    assert any(isinstance(e, ErrorEvent) for e in events)
    failure = [r for r in caplog.records if "QUERY_FAILURE" in r.getMessage()]
    assert failure, "expected a QUERY_FAILURE error log on setup failure"
    assert "_Boom" in failure[0].getMessage()


# ---------------------------------------------------------------------------
# Other raw-runtime readers ignore the heartbeat
# ---------------------------------------------------------------------------

def test_stream_capture_event_metadata_classifies_heartbeat_distinctly():
    """stream_capture never sees a heartbeat in production (dropped upstream),
    but if a raw line reaches _event_metadata it must not be mistaken for
    model content / tool / error — it is a benign metadata/heartbeat frame."""
    from agents.stream_capture import _event_metadata

    meta = json.loads(_event_metadata(HEARTBEAT_LINE))
    assert meta["event_category"] not in (
        "model_content_redacted", "tool_use", "tool_result", "error")


# ---------------------------------------------------------------------------
# Exact-match protocol (MED r1): only data == {HEARTBEAT_KEY: 1} is a
# heartbeat. A mixed-key frame, the reserved key with another value, and free
# text mentioning the token must all flow through as normal frames.
# ---------------------------------------------------------------------------

def test_is_heartbeat_requires_exact_single_key_frame():
    from agents.chat_client import _is_heartbeat
    assert _is_heartbeat({"adt_heartbeat": 1}) is True
    # Mixed-key / wrong-value / wrong-key / non-dict → NOT a heartbeat.
    assert _is_heartbeat({"adt_heartbeat": 1, "data": "REAL ANSWER"}) is False
    assert _is_heartbeat({"adt_heartbeat": 0}) is False
    assert _is_heartbeat({"adt_heartbeat": 2}) is False
    assert _is_heartbeat({"adt_heartbeat": "1"}) is False
    # Python equality treats True and 1.0 as 1; the protocol value is the int 1 only.
    assert _is_heartbeat({"adt_heartbeat": True}) is False
    assert _is_heartbeat({"adt_heartbeat": 1.0}) is False
    assert _is_heartbeat({"data": "adt_heartbeat"}) is False
    assert _is_heartbeat("adt_heartbeat") is False
    assert _is_heartbeat(None) is False


def test_mixed_key_frame_flows_through_and_is_counted():
    """A frame carrying the reserved key ALONGSIDE a real answer must NOT be
    dropped: the answer text is yielded and the frame is counted."""
    mixed = 'data: ' + json.dumps({"adt_heartbeat": 1, "data": "REAL ANSWER"})
    state = _new_state()
    data = state._line_data(mixed.encode())
    assert data is not None                     # not dropped as a heartbeat
    assert state.line_count == 1                # counted
    assert state.total_bytes > 0
    events = state.events_for(data)
    rendered = _render(events)
    assert ("MessageEvent", "REAL ANSWER") in rendered


def test_reserved_key_with_other_value_flows_through():
    """``{"adt_heartbeat": 2}`` is not the exact frame — treat as a real frame
    (not a heartbeat): it is counted, not silently dropped."""
    frame = 'data: ' + json.dumps({"adt_heartbeat": 2})
    state = _new_state()
    data = state._line_data(frame.encode())
    assert data == {"adt_heartbeat": 2}
    assert state.line_count == 1


def test_free_text_mentioning_token_is_not_a_heartbeat():
    """A real answer frame whose text merely mentions the token is a normal
    frame, counted, and its text yielded."""
    frame = 'data: ' + json.dumps({"data": "the adt_heartbeat key keeps sockets alive"})
    state = _new_state()
    data = state._line_data(frame.encode())
    assert data is not None
    assert state.line_count == 1
    rendered = _render(state.events_for(data))
    assert ("MessageEvent", "the adt_heartbeat key keeps sockets alive") in rendered
