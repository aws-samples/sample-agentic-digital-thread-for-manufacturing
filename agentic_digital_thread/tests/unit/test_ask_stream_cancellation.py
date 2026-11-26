# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The /api/ask producer thread must be CANCELLATION-AWARE (P0-A).

Incident evidence (2026-08-24 Management full-e2e, twice): 41 pass / 5 varying
network failures where every exact failure passed when run targeted; a single
healthy app task (cpu=512 / memory=1024, ONE Uvicorn process) pinned
~99.9-100% CPU for ~45 minutes while memory sat at ~43%; ALB averages 3-6 s
with maxima of 18-31 s; a static Chart.js asset timed out and one CloudFront
502. That is application workload amplification on a single-core budget.

One amplifier lives here. ``_start_stream_worker`` spawned a daemon thread that
pumped ``client.stream`` into an UNBOUNDED queue and had NO idea the consumer
had gone away. A client disconnect (or an aborted briefing) raises
``GeneratorExit`` in the drain generator, but the producer kept driving the
orchestrator turn to completion and kept ``put``-ing into a queue nobody would
ever read — so every abandoned turn stayed a full-cost, full-CPU turn, and its
events accumulated in memory until the queue was garbage.

Pins:
1. ``_start_stream_worker`` returns a handle carrying the queue AND a stop
   event, and the queue is BOUNDED.
2. Closing the drain generator (the disconnect) SETS the stop event.
3. The producer notices the stop event, stops pulling, and CLOSES the event
   iterator in a ``finally`` (the chat client itself is global and is NEVER
   closed).
4. Nothing is produced or queued after the disconnect — no post-disconnect
   queue growth.
5. The normal (sentinel-terminated) path still stops its worker cleanly.

Offline: the chat client is a local stub; nothing streams over a socket.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.events import MessageEvent  # noqa: E402


class _EndlessClient:
    """A chat-client stub whose stream never ends on its own.

    ``produced`` counts events pulled from the generator; ``closed`` records
    that the ITERATOR (not the client) was closed."""

    def __init__(self, delay: float = 0.005) -> None:
        self.produced = 0
        self.closed = False
        self.client_closed = False
        self._delay = delay

    def close(self) -> None:  # pragma: no cover - must never be called
        self.client_closed = True

    def stream(self, **kwargs):
        try:
            while True:
                self.produced += 1
                yield MessageEvent(content=f"tok-{self.produced}")
                time.sleep(self._delay)
        finally:
            self.closed = True


def _worker(client):
    return server._start_stream_worker(client, "q", "s", "u", False, None)


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestWorkerHandleShape:
    def test_handle_carries_a_bounded_queue_and_a_stop_event(self):
        client = _EndlessClient()
        worker = _worker(client)
        try:
            assert isinstance(worker.queue, queue.Queue)
            assert worker.queue.maxsize > 0, "the queue must be BOUNDED"
            assert isinstance(worker.stop, threading.Event)
            assert worker.stop.is_set() is False
        finally:
            worker.stop.set()
        assert _wait(lambda: client.closed)


class TestDisconnectCancelsTheProducer:
    def _drain(self, worker):
        stats = {"token_count": 0, "tools_used": []}
        return server._drain_event_queue(worker, stats, time.monotonic())

    def test_generator_close_sets_stop_and_closes_the_iterator(self):
        client = _EndlessClient()
        worker = _worker(client)
        gen = self._drain(worker)
        assert "tok-1" in next(gen)

        gen.close()  # client disconnect

        assert worker.stop.is_set() is True
        assert _wait(lambda: client.closed), "the event iterator must be closed"
        assert client.client_closed is False, "the GLOBAL chat client must not be closed"

    def test_no_production_after_disconnect(self):
        client = _EndlessClient()
        worker = _worker(client)
        gen = self._drain(worker)
        next(gen)

        gen.close()
        assert _wait(lambda: client.closed)
        settled = client.produced
        settled_qsize = worker.queue.qsize()
        time.sleep(0.4)

        assert client.produced == settled, "producer kept driving after disconnect"
        assert worker.queue.qsize() <= settled_qsize, "queue grew after disconnect"
        assert worker.queue.qsize() <= worker.queue.maxsize

    def test_queue_never_exceeds_its_bound_while_abandoned(self):
        """A never-read queue must plateau at the bound, not grow unbounded."""
        client = _EndlessClient(delay=0.0)
        worker = _worker(client)
        try:
            time.sleep(0.4)
            assert worker.queue.qsize() <= worker.queue.maxsize
        finally:
            worker.stop.set()
        assert _wait(lambda: client.closed)


class _BlockingBody:
    """A response body that BLOCKS in its read until ``close()`` releases it.

    This is the shape that matters: the real cloud producer sits inside
    ``response["response"].iter_lines()``, so a stop flag checked between events
    is never reached and the iterator's ``finally`` cannot run. Only closing the
    per-invocation body from OUTSIDE the blocked read unblocks it."""

    def __init__(self) -> None:
        self._released = threading.Event()
        self.closed = False
        self.close_calls = 0

    def read_block(self) -> None:
        # Generous ceiling: if cancellation works this returns immediately, and
        # the assertions below fail long before it elapses if it does not.
        self._released.wait(30)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._released.set()


class _BlockedInReadClient:
    """A chat-client stub that arms the cancel handle, then blocks mid-stream."""

    def __init__(self) -> None:
        self.body = _BlockingBody()
        self.closed = False
        self.client_closed = False
        self.produced = 0

    def close(self) -> None:  # pragma: no cover - must never be called
        self.client_closed = True

    def stream(self, cancel=None, **kwargs):
        if cancel is not None:
            cancel.arm(self.body)
        try:
            self.produced += 1
            yield MessageEvent(content="tok-1")
            self.body.read_block()  # blocked INSIDE the read, like iter_lines
            self.produced += 1
            yield MessageEvent(content="tok-2")
        finally:
            self.closed = True


class TestDisconnectCancelsAProducerBlockedInsideTheRead:
    """The primary load-closure contract: an abandoned turn must END.

    A producer parked inside its read holds the AgentCore turn alive until the
    300 s read timeout, so a stop flag alone closes nothing."""

    def _drain(self, worker):
        stats = {"token_count": 0, "tools_used": []}
        return server._drain_event_queue(worker, stats, time.monotonic())

    def test_worker_exposes_a_cancel_handle(self):
        client = _BlockedInReadClient()
        worker = _worker(client)
        try:
            assert _wait(lambda: client.body.closed is False or True)
            assert worker.cancel is not None
            assert worker.cancel.cancelled is False
        finally:
            worker.stop.set()
            worker.cancel.cancel()
        assert _wait(lambda: client.closed)

    def test_disconnect_closes_the_body_and_unblocks_the_producer(self):
        client = _BlockedInReadClient()
        worker = _worker(client)
        gen = self._drain(worker)
        assert "tok-1" in next(gen)
        assert _wait(lambda: client.body is not None)

        gen.close()  # client disconnect while the producer is blocked in read

        assert worker.stop.is_set() is True
        assert worker.cancel.cancelled is True
        assert _wait(lambda: client.body.closed), \
            "the per-invocation response body must be closed from outside"
        assert _wait(lambda: client.closed), "the event iterator must be closed"
        assert client.client_closed is False, \
            "the GLOBAL chat client must never be closed"

    def test_no_post_disconnect_production_or_queue_growth(self):
        client = _BlockedInReadClient()
        worker = _worker(client)
        gen = self._drain(worker)
        next(gen)

        gen.close()
        assert _wait(lambda: client.closed)
        settled_produced, settled_qsize = client.produced, worker.queue.qsize()
        time.sleep(0.3)

        assert client.produced == settled_produced
        assert worker.queue.qsize() <= settled_qsize
        assert worker.queue.qsize() <= worker.queue.maxsize

    def test_normal_completion_releases_the_body_too(self):
        """No leak on the happy path: the body closes without a cancel."""
        class _Finite:
            def __init__(self):
                self.body = _BlockingBody()

            def stream(self, cancel=None, **kwargs):
                if cancel is not None:
                    cancel.arm(self.body)
                yield MessageEvent(content="hello")

        client = _Finite()
        worker = _worker(client)
        stats = {"token_count": 0, "tools_used": []}
        frames = list(server._drain_event_queue(worker, stats, time.monotonic()))

        assert len(frames) == 1 and "hello" in frames[0]
        assert _wait(lambda: client.body.closed)


class TestStreamCancellationHandle:
    """The handle itself: idempotent, race-safe, never fatal."""

    def test_arm_then_cancel_closes_the_body_once(self):
        from agents.chat_client import StreamCancellation
        body = _BlockingBody()
        handle = StreamCancellation()
        handle.arm(body)

        handle.cancel()
        handle.cancel()

        assert body.close_calls == 1
        assert handle.cancelled is True

    def test_cancel_before_arm_closes_immediately(self):
        """The disconnect can land between invoke and the first read."""
        from agents.chat_client import StreamCancellation
        handle = StreamCancellation()
        handle.cancel()
        body = _BlockingBody()

        handle.arm(body)

        assert body.closed is True

    def test_release_closes_without_marking_cancelled(self):
        from agents.chat_client import StreamCancellation
        body = _BlockingBody()
        handle = StreamCancellation()
        handle.arm(body)

        handle.release()

        assert body.close_calls == 1
        assert handle.cancelled is False

    def test_a_failing_close_is_never_fatal(self):
        from agents.chat_client import StreamCancellation

        class _Hostile:
            def close(self):
                raise RuntimeError("body already torn down")

        handle = StreamCancellation()
        handle.arm(_Hostile())
        handle.cancel()  # must not raise
        assert handle.cancelled is True

    def test_arm_none_is_a_no_op(self):
        from agents.chat_client import StreamCancellation
        handle = StreamCancellation()
        handle.arm(None)
        handle.cancel()
        assert handle.cancelled is True


class TestCloudPathArmsThePerInvocationBody:
    """``_stream_cloud_response`` must arm/release the RESPONSE BODY.

    Previously only ``_CloudStreamState`` (the capture file) was closed in the
    ``finally`` — never the body — so nothing could unblock ``iter_lines``."""

    def _state(self):
        from agents.chat_client import ThinkingFilter, _CloudStreamState
        import logging as _log
        return _CloudStreamState(
            ThinkingFilter(), integrity_mode=False,
            log=_log.getLogger("test_agentcore_client"),
            owner_id="u", session_id="s", runtime_session_id="rs-1",
        )

    def test_body_is_armed_and_released_on_the_normal_path(self):
        from agents.chat_client import StreamCancellation, _stream_cloud_response

        class _Body:
            def __init__(self):
                self.closed = False

            def iter_lines(self, chunk_size=None):
                return iter([])

            def close(self):
                self.closed = True

        body = _Body()
        handle = StreamCancellation()
        list(_stream_cloud_response({"response": body}, self._state(), handle))

        assert body.closed is True, "the per-invocation body must be closed"
        assert handle.cancelled is False

    def test_cancel_closes_the_body_while_iter_lines_is_blocked(self):
        from agents.chat_client import StreamCancellation, _stream_cloud_response

        body = _BlockingBody()
        body.iter_lines = lambda chunk_size=None: iter(
            _blocking_lines(body))
        handle = StreamCancellation()
        gen = _stream_cloud_response({"response": body}, self._state(), handle)
        done = threading.Event()

        def _consume():
            try:
                list(gen)
            finally:
                done.set()

        threading.Thread(target=_consume, daemon=True).start()
        assert _wait(lambda: body.close_calls == 0 or True)
        time.sleep(0.1)

        handle.cancel()

        assert done.wait(5), "cancel must unblock the blocked cloud iterator"
        assert body.closed is True

    def test_body_is_released_when_the_stream_raises(self):
        from agents.chat_client import StreamCancellation, _stream_cloud_response

        class _Boom:
            def __init__(self):
                self.closed = False

            def iter_lines(self, chunk_size=None):
                raise RuntimeError("stream torn down")

            def close(self):
                self.closed = True

        body = _Boom()
        handle = StreamCancellation()
        events = list(_stream_cloud_response({"response": body}, self._state(),
                                             handle))

        assert body.closed is True
        errors = [e for e in events if getattr(e, "category", None) is not None
                  and type(e).__name__ == "ErrorEvent"]
        assert errors and errors[0].category == "stream_error"
        assert errors[0].message.startswith("Query failed")
        assert errors[0].correlation_id == "s"


def _blocking_lines(body):
    """One yielded line, then a block that only ``body.close()`` releases."""
    yield b""
    body.read_block()


class TestNormalPathUnchanged:
    def test_sentinel_terminated_stream_yields_then_clears_the_flag(self):

        class _Finite:
            def stream(self, **kwargs):
                yield MessageEvent(content="hello")

        worker = _worker(_Finite())
        stats = {"token_count": 0, "tools_used": []}
        frames = list(server._drain_event_queue(worker, stats, time.monotonic()))

        assert len(frames) == 1 and "hello" in frames[0]
        assert stats["token_count"] == 1
        assert worker.stop.is_set() is True
