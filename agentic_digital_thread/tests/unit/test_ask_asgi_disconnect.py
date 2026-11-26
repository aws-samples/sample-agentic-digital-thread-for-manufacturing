# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A REAL ``http.disconnect`` must tear the /api/ask turn down.

The earlier cancellation work proved the contract by calling ``gen.close()`` on
the drain generator directly — which is a TEST-ONLY entry point. In production
nothing calls it:

* ``ask`` hands a SYNC generator to ``StreamingResponse``, so Starlette wraps it
  in ``iterate_in_threadpool`` — and that wrapper has **no** ``finally`` that
  closes the sync iterator.
* ``_ask_integrity`` then replaces ``body_iterator`` with ``_fixed_public_sse``.
* Starlette 0.51.0's ``StreamingResponse.__call__`` reacts to ``http.disconnect``
  by cancelling ``stream_response`` inside its own task group and returning. It
  never ``aclose()``es ``body_iterator``.

Net effect before the fix: on a real client disconnect the worker's stop flag was
never set, its cancel handle never fired, and the armed per-invocation cloud body
was never closed — exactly the abandoned-but-still-billing turn the load incident
was about. The sync generator was left to the garbage collector.

Pins (all through the PRODUCTION response wrapper — ``server.ask`` is
``_ask_integrity``):

1. Driving the production response with a real ``http.disconnect`` stops the
   worker and closes the armed cloud body.
2. The teardown happens BEFORE ``StreamingResponse.__call__`` returns — probed
   from inside the production ``finally`` itself.
3. The ASGI-spec >= 2.4 path (no disconnect listener; the disconnect surfaces as
   ``OSError`` from ``send``) tears down too.
4. Public SSE framing survives: ``http.response.start`` then body chunks, and a
   normal (undisconnected) turn still yields its terminal frames.
5. The admission token is released on the disconnect path — no leak.
6. The LOCAL client path is torn down by the same disconnect: its bounded queue
   stops growing, the cooperative strands seam is fired, and teardown does not
   wait out the model call.

Offline: the chat clients are stubs; nothing streams over a socket.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.events import MessageEvent  # noqa: E402
from agents.models import AskRequest  # noqa: E402
from agents.query_admission import get_admission  # noqa: E402


# ── ASGI harness ─────────────────────────────────────────────────────────────

def _scope(spec_version: str = "2.1") -> dict:
    return {"type": "http", "method": "POST", "path": "/api/ask",
            "headers": [], "asgi": {"version": "3.0", "spec_version": spec_version}}


class _AsgiRun:
    """Drives one ASGI response, disconnecting after the first body chunk."""

    def __init__(self, *, spec_version: str = "2.1", send_raises: bool = False):
        self.spec_version = spec_version
        self.send_raises = send_raises
        self.messages: list[dict] = []
        self.elapsed = 0.0
        self._first_body = asyncio.Event()

    async def _send(self, message: dict) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self._first_body.set()
            if self.send_raises:
                raise OSError("peer closed the connection")

    async def _receive(self) -> dict:
        await self._first_body.wait()
        return {"type": "http.disconnect"}

    async def __call__(self, response) -> None:
        start = time.monotonic()
        try:
            await response(_scope(self.spec_version), self._receive, self._send)
        finally:
            self.elapsed = time.monotonic() - start

    @property
    def body_chunks(self) -> list[str]:
        return [m["body"].decode() if isinstance(m["body"], bytes) else str(m["body"])
                for m in self.messages
                if m["type"] == "http.response.body" and m.get("body")]


class _FakeRequestState:
    user = None
    session = None


class _FakeRequest:
    def __init__(self):
        self.state = _FakeRequestState()


def _ask(client, question: str = "why did lot 9 fail?", session_id: str = "s-asgi"):
    """Build the PRODUCTION /api/ask response (``server.ask`` is
    ``_ask_integrity`` — the public wrapper, framing filter included)."""
    body = AskRequest(question=question, session_id=session_id)
    with patch.object(server, "get_chat_client", lambda: client):
        return asyncio.run(server.ask(body, _FakeRequest()))


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── Cloud-shaped stub: armed body, blocked inside the read ───────────────────

class _BlockingBody:
    """A per-invocation response body whose read only ``close()`` releases."""

    def __init__(self) -> None:
        self._released = threading.Event()
        self.closed = False
        self.close_calls = 0

    def read_block(self) -> None:
        # Generous ceiling: with cancellation working this returns at once, and
        # the assertions fail long before it elapses if it does not.
        self._released.wait(30)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._released.set()


class _BlockedInReadClient:
    """Arms the cancel handle with a body, then parks inside its read."""

    def __init__(self) -> None:
        self.body = _BlockingBody()
        self.iterator_closed = False
        self.client_closed = False

    def close(self) -> None:  # pragma: no cover - must never be called
        self.client_closed = True

    def stream(self, cancel=None, **kwargs):
        if cancel is not None:
            cancel.arm(self.body)
        try:
            yield MessageEvent(content="tok-1")
            self.body.read_block()
            yield MessageEvent(content="tok-2")
        finally:
            self.iterator_closed = True


class _CapturedWorkers:
    """Records every worker ``ask`` starts, without touching the handler."""

    def __init__(self) -> None:
        self.workers: list = []

    def __enter__(self):
        self._real = server._start_stream_worker

        def _spy(*args, **kwargs):
            worker = self._real(*args, **kwargs)
            self.workers.append(worker)
            return worker

        server._start_stream_worker = _spy
        return self

    def __exit__(self, *exc):
        server._start_stream_worker = self._real
        return False

    @property
    def only(self):
        assert len(self.workers) == 1, f"expected one worker, got {len(self.workers)}"
        return self.workers[0]


class TestRealAsgiDisconnectTearsTheTurnDown:
    def test_disconnect_stops_the_worker_and_closes_the_armed_body(self):
        """THE H1 repro: no ``gen.close()`` anywhere — only ``http.disconnect``."""
        client = _BlockedInReadClient()
        with _CapturedWorkers() as spy:
            response = _ask(client)
            run = _AsgiRun()
            asyncio.run(run(response))
            worker = spy.only

        assert worker.stop.is_set() is True, "the producer was never stopped"
        assert worker.cancel.cancelled is True, "the cancel handle never fired"
        assert client.body.closed is True, \
            "the armed per-invocation cloud body was never closed"
        assert client.client_closed is False, \
            "the GLOBAL chat client must never be closed"
        assert _wait(lambda: client.iterator_closed), \
            "the event iterator must be closed"
        assert inspect.getgeneratorstate(response._source_iterator) == inspect.GEN_CLOSED, \
            "the ASGI response left its underlying sync iterator open"

    def test_teardown_happens_before_call_returns(self):
        """Probed from INSIDE the production ``finally``.

        ``_aclose_body_iterator`` is the last statement of the response's own
        teardown, so a snapshot taken there is strictly before
        ``StreamingResponse.__call__`` returns — and strictly after the
        ownership cancel."""
        client = _BlockedInReadClient()
        snapshots: list[tuple[bool, bool]] = []
        real_aclose = server._aclose_body_iterator

        async def _probe(iterator):
            snapshots.append((worker_box[0].stop.is_set(), client.body.closed))
            await real_aclose(iterator)

        with _CapturedWorkers() as spy:
            response = _ask(client)
            worker_box = spy.workers
            with patch.object(server, "_aclose_body_iterator", _probe):
                asyncio.run(_AsgiRun()(response))

        assert snapshots, "the response never ran its own teardown"
        stopped, body_closed = snapshots[0]
        assert stopped is True, "worker still running when __call__ was returning"
        assert body_closed is True, "cloud body still open when __call__ was returning"

    def test_spec_24_send_failure_also_tears_down(self):
        """ASGI >= 2.4 never listens for disconnect — it surfaces on ``send``."""
        from starlette.requests import ClientDisconnect
        client = _BlockedInReadClient()
        with _CapturedWorkers() as spy:
            response = _ask(client)
            run = _AsgiRun(spec_version="2.4", send_raises=True)
            with pytest.raises(ClientDisconnect):
                asyncio.run(run(response))
            worker = spy.only

        assert worker.stop.is_set() is True
        assert worker.cancel.cancelled is True
        assert client.body.closed is True

    def test_disconnect_releases_the_admission_token(self):
        client = _BlockedInReadClient()
        response = _ask(client)
        asyncio.run(_AsgiRun()(response))

        assert get_admission().busy is False, "the admission token leaked"

    def test_public_framing_survives_the_disconnect(self):
        client = _BlockedInReadClient()
        response = _ask(client)
        run = _AsgiRun()
        asyncio.run(run(response))

        assert run.messages[0]["type"] == "http.response.start"
        assert run.messages[0]["status"] == 200
        assert run.body_chunks, "no body chunk was sent"
        assert "tok-1" in run.body_chunks[0]


class TestUndisconnectedTurnIsUnchanged:
    def test_normal_turn_streams_its_terminal_frames(self):
        from agents.events import DoneEvent

        class _FiniteDone:
            def stream(self, cancel=None, **kwargs):
                yield MessageEvent(content="answer")
                yield DoneEvent(text="answer")

        response = _ask(_FiniteDone())
        run = _AsgiRun()
        # No disconnect: the receive channel stays silent until the body ends.
        run._receive = _never  # type: ignore[method-assign]
        asyncio.run(run(response))

        joined = "".join(run.body_chunks)
        assert "answer" in joined
        assert "event: done" in joined
        assert run.messages[-1] == {"type": "http.response.body", "body": b"",
                                    "more_body": False}
        assert get_admission().busy is False


async def _never() -> dict:
    await asyncio.Event().wait()          # pragma: no cover - never resolves
    raise AssertionError("unreachable")   # pragma: no cover


# ── H3: the LOCAL client path, through the same real disconnect ──────────────

class _Hooks:
    def __init__(self) -> None:
        self.added: list = []

    def add_hook(self, hook) -> None:
        self.added.append(hook)


class _FakeStrandsAgent:
    """Stands in for the strands ``Agent`` built by ``build_orchestrator``.

    Emits callback tokens until its cooperative cancel signal is set — the REAL
    pinned seam (``strands`` 1.42.0 ``Agent.cancel()``, honored at the model
    streaming / pre-tool checkpoints). The hard ceiling keeps a regression a
    FAILURE rather than a hang."""

    CEILING = 20.0

    def __init__(self) -> None:
        self.callback_handler = None
        self.hooks = _Hooks()
        self.cancel_calls = 0
        self.emits = 0
        self.returned = threading.Event()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._cancelled.set()

    def __call__(self, prompt, **kwargs):
        deadline = time.monotonic() + self.CEILING
        while not self._cancelled.wait(0.002):
            if time.monotonic() > deadline:
                break
            self.emits += 1
            self.callback_handler(data=f"t{self.emits}")
        self.returned.set()
        return "local answer"


def _local_client():
    from agents.chat_client import LocalChatClient
    return LocalChatClient()


def _drive_local(agent, spy: '_CapturedWorkers | None' = None) -> _AsgiRun:
    """One PRODUCTION /api/ask turn on the local client, disconnected mid-stream.

    ``build_orchestrator`` stays patched for the whole run — the local producer
    thread calls it, and that thread outlives the response construction."""
    with patch("agents.chat_client.build_orchestrator", lambda *a, **k: agent):
        response = _ask(_local_client(), session_id="s-local")
        run = _AsgiRun()
        asyncio.run(run(response))
        if spy is not None:
            assert spy.workers, "no worker was started"
        return run


class TestLocalClientPathIsCancelledByTheDisconnect:
    def test_disconnect_cancels_the_local_producer_and_bounds_the_queue(self):
        agent = _FakeStrandsAgent()
        with _CapturedWorkers() as spy:
            run = _drive_local(agent, spy)
            worker = spy.only

        assert worker.stop.is_set() is True
        assert run.elapsed < _FakeStrandsAgent.CEILING / 2, (
            "response teardown waited out the model call "
            f"({run.elapsed:.1f}s)")
        assert agent.cancel_calls >= 1, \
            "the cooperative strands cancel seam was never fired"
        assert agent.returned.wait(5), "the local producer never released"

        handler = agent.callback_handler
        assert handler is not None
        q = handler._q
        assert q.maxsize > 0, "the local internal queue must be BOUNDED"
        assert q.qsize() <= q.maxsize

    def test_callback_events_are_dropped_after_cancel(self):
        agent = _FakeStrandsAgent()
        _drive_local(agent)

        handler = agent.callback_handler
        q = handler._q
        settled = q.qsize()
        for i in range(2000):
            handler(data=f"post-{i}")           # must neither block nor grow

        assert q.qsize() <= max(settled, q.maxsize)
        assert q.qsize() <= q.maxsize

    def test_local_disconnect_releases_the_admission_token(self):
        _drive_local(_FakeStrandsAgent())

        assert get_admission().busy is False


class _SilentBlockedClient:
    """Arm a per-invocation body and block before any semantic event."""

    def __init__(self) -> None:
        self.body = _BlockingBody()
        self.iterator_closed = False
        self.client_closed = False

    def close(self) -> None:  # pragma: no cover - must never be called
        self.client_closed = True

    def stream(self, cancel=None, **kwargs):
        if cancel is not None:
            cancel.arm(self.body)
        try:
            self.body.read_block()
            yield MessageEvent(content="must-not-be-sent")
        finally:
            self.iterator_closed = True


def test_disconnect_after_idle_keepalive_tears_down_without_late_bytes():
    """The production response stays 250 ms-stop-aware despite a 15 s cadence."""
    client = _SilentBlockedClient()
    with patch.object(server, "_ASK_STREAM_KEEPALIVE_SECONDS", 0.03), \
            patch.object(server, "_STREAM_STOP_TICK", 0.005), \
            _CapturedWorkers() as spy:
        response = _ask(client, question="private question", session_id="private-session")
        run = _AsgiRun()
        asyncio.run(run(response))
        worker = spy.only

    assert run.elapsed < 1.0
    assert run.body_chunks == [": keepalive\n\n"]
    assert worker.stop.is_set() is True
    assert worker.cancel.cancelled is True
    assert client.body.closed is True
    assert _wait(lambda: client.iterator_closed)
    assert client.client_closed is False
    assert inspect.getgeneratorstate(response._source_iterator) == inspect.GEN_CLOSED
    assert get_admission().busy is False
    time.sleep(0.08)
    assert run.body_chunks == [": keepalive\n\n"], "heartbeat continued after disconnect"
