# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Transport-only keepalive contracts for the ECS ``/api/ask`` queue drain."""

from __future__ import annotations

import asyncio
import queue
import threading
import time

import pytest

import agents.server as server
from agents.events import (
    DoneEvent,
    MessageEvent,
    MetadataEvent,
    ToolResultEvent,
    ToolUseEvent,
)

_KEEPALIVE = ": keepalive\n\n"
_TEST_INTERVAL = 0.03
_OVERALL_SAFETY_TIMEOUT = 1.0


@pytest.fixture
def short_keepalive(monkeypatch):
    assert server._ASK_STREAM_KEEPALIVE_SECONDS == 15.0
    assert server._STREAM_STOP_TICK == 0.25
    monkeypatch.setattr(server, "_ASK_STREAM_KEEPALIVE_SECONDS", _TEST_INTERVAL)
    monkeypatch.setattr(server, "_STREAM_STOP_TICK", 0.005)


def _stats() -> dict:
    return {"token_count": 0, "tools_used": []}


def _drain(worker, stats=None):
    return server._drain_event_queue(
        worker, stats if stats is not None else _stats(), time.monotonic()
    )


class _IdleSequenceClient:
    """Emit a real tool start, stay semantically silent, then finish normally."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.waiting = threading.Event()
        self.iterator_closed = False
        self.client_closed = False
        self.events = [
            ToolUseEvent(
                tool_name="secret_tool",
                tool_input={"credential": "never-log-me"},
                tool_use_id="tool-secret-id",
            ),
            ToolResultEvent(
                tool_name="secret_tool",
                tool_result="secret-result",
                tool_use_id="tool-secret-id",
                summary="bounded result summary",
            ),
            MessageEvent(content="final answer marker"),
            MetadataEvent(
                data={
                    "intelligence_summary": {"total_tool_calls": 1},
                    "response_integrity": {"verdict": "accepted"},
                }
            ),
            DoneEvent(text="final answer marker"),
        ]

    def close(self) -> None:  # pragma: no cover - must never be called
        self.client_closed = True

    def stream(self, **kwargs):
        try:
            yield self.events[0]
            self.waiting.set()
            assert self.release.wait(2), "test did not release the idle stream"
            yield from self.events[1:]
        finally:
            self.iterator_closed = True


def _worker(client):
    return server._start_stream_worker(
        client, "question-secret", "session-secret", "user-secret", False, None
    )


def test_semantic_silence_emits_repeated_exact_payload_free_comments(
    short_keepalive, monkeypatch
):
    client = _IdleSequenceClient()
    worker = _worker(client)
    stats = _stats()
    calls = []
    real_accumulate = server._accumulate_and_frame

    def _spy(event, target):
        calls.append(event)
        yield from real_accumulate(event, target)

    monkeypatch.setattr(server, "_accumulate_and_frame", _spy)
    stream = _drain(worker, stats)
    first = next(stream)
    assert first == client.events[0].to_sse_format()
    assert client.waiting.wait(1)

    started = time.monotonic()
    first_comment = next(stream)
    first_comment_at = time.monotonic()
    second_comment = next(stream)
    second_comment_at = time.monotonic()
    comments = [first_comment, second_comment]
    assert second_comment_at - first_comment_at >= _TEST_INTERVAL * 0.8
    assert second_comment_at - started <= _OVERALL_SAFETY_TIMEOUT
    assert comments == [_KEEPALIVE, _KEEPALIVE]
    assert all(len(comment.encode()) == 13 for comment in comments)
    assert all(
        "event:" not in comment and "data:" not in comment for comment in comments
    )
    forbidden = [
        "question-secret",
        "session-secret",
        "user-secret",
        "secret_tool",
        "tool-secret-id",
        "never-log-me",
        "secret-result",
        "final answer marker",
        "credential",
        "exception",
        "reasoning",
    ]
    assert all(needle not in "".join(comments) for needle in forbidden)
    assert calls == [client.events[0]]
    assert stats == {"token_count": 0, "tools_used": ["secret_tool"]}

    client.release.set()
    frames = [first, *comments, *list(stream)]
    semantic = [frame for frame in frames if frame != _KEEPALIVE]
    assert semantic == [event.to_sse_format() for event in client.events]
    assert calls == client.events
    assert stats == {"token_count": 3, "tools_used": ["secret_tool"]}
    assert client.iterator_closed is True
    assert client.client_closed is False


def test_queued_semantic_event_wins_when_heartbeat_deadline_is_due():
    worker = server._StreamWorker(maxsize=2)
    event = MessageEvent(content="semantic")
    worker.queue.put(event)
    try:
        assert server._next_stream_event(worker, timeout=0) is event
    finally:
        server._cancel_worker(worker)


class _ScriptedClock:
    """``time.monotonic`` that only moves forward: real time plus a skew the
    scripted queue advances instead of sleeping, so no wall-time race exists."""

    def __init__(self) -> None:
        self._real = time.monotonic
        self.skew = 0.0

    def __call__(self) -> float:
        return self._real() + self.skew


class _ScriptedQueue:
    """Queue stand-in: a blocking ``get`` fast-forwards the clock to the
    scheduled arrival (inside the wait) or to the end of the timeout."""

    def __init__(self, clock: _ScriptedClock) -> None:
        self.clock = clock
        self.scheduled = []  # (due, item)
        self.blocking_deliveries = []

    def put(self, item, timeout=None) -> None:
        self.scheduled.append((self.clock(), item))

    def put_at(self, due: float, item) -> None:
        self.scheduled.append((due, item))

    def _pop_due(self, now: float):
        for index, (due, item) in enumerate(self.scheduled):
            if due <= now:
                return self.scheduled.pop(index)[1]
        raise queue.Empty

    def get_nowait(self):
        return self._pop_due(self.clock())

    def get(self, timeout=None):
        now = self.clock()
        due_in = [due - now for due, _ in self.scheduled if due - now <= timeout]
        self.clock.skew += max(0.0, min(due_in)) if due_in else timeout
        if not due_in:
            raise queue.Empty
        item = self._pop_due(self.clock())
        self.blocking_deliveries.append(item)
        return item


def test_real_frame_resets_idle_deadline(short_keepalive, monkeypatch):
    clock = _ScriptedClock()
    monkeypatch.setattr(time, "monotonic", clock)
    worker = server._StreamWorker(maxsize=2)
    scripted = _ScriptedQueue(clock)
    worker.queue = scripted
    stream = _drain(worker)
    try:
        assert next(stream) == _KEEPALIVE
        real = MessageEvent("real")
        # Arrives halfway through the next idle wait, never before it starts.
        scripted.put_at(clock() + _TEST_INTERVAL / 2, real)
        assert next(stream) == real.to_sse_format()
        assert scripted.blocking_deliveries == [real], "not a mid-wait arrival"
        after_real = clock()
        assert next(stream) == _KEEPALIVE
        # A full interval after the real frame, not the old deadline's half.
        assert clock() - after_real >= _TEST_INTERVAL
    finally:
        stream.close()


def test_eof_and_stop_never_emit_a_late_keepalive(short_keepalive):
    eof_worker = server._StreamWorker(maxsize=1)
    eof_worker.queue.put(None)
    assert list(_drain(eof_worker)) == []

    stopped_worker = server._StreamWorker(maxsize=1)
    stopped_worker.stop.set()
    assert list(_drain(stopped_worker)) == []


def test_stop_polling_is_independent_of_keepalive_cadence(monkeypatch):
    worker = server._StreamWorker(maxsize=1)
    monkeypatch.setattr(server, "_STREAM_STOP_TICK", 0.01)
    result = []
    done = threading.Event()

    def _wait():
        result.append(server._next_stream_event(worker, timeout=5.0))
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    time.sleep(0.03)
    worker.stop.set()
    assert done.wait(0.25), "stop waited for the heartbeat deadline"
    assert result == [server._STOPPED]
    server._cancel_worker(worker)


def test_public_error_filter_passes_comments_and_semantic_frames_byte_exact():
    expected = [
        _KEEPALIVE,
        MessageEvent("answer").to_sse_format(),
        _KEEPALIVE,
        DoneEvent("answer").to_sse_format(),
    ]

    async def _source():
        for frame in expected:
            yield frame

    async def _collect():
        return [frame async for frame in server._fixed_public_sse(_source())]

    assert asyncio.run(_collect()) == expected
