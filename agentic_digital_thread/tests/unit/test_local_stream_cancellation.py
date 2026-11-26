# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``LocalChatClient`` must HONOR cancellation and stay bounded.

Before this pass ``LocalChatClient.stream(cancel=…)`` documented the handle as
"ACCEPTED and unused" — accepted-and-ignored. Two consequences:

* The producer thread kept driving a full orchestrator turn (tools, model calls)
  for a consumer that had disconnected, and every callback token kept being
  enqueued.
* In the ordinary (non-integrity) mode the internal queue was built with
  ``maxsize=0`` — UNBOUNDED — and written with a blocking ``queue.put``. An
  abandoned turn therefore accumulated its whole remaining event stream in the
  app task's memory. In integrity mode the queue was bounded but the terminal
  frames were still written with a blocking ``put``, which on a bounded queue
  with no consumer blocks the producer thread forever.

The honest guarantee this pins (``strands`` is pinned ``==1.42.0``): the pinned
API DOES expose a cooperative seam — ``Agent.cancel()``, a thread-safe
``threading.Event`` honored at the model-streaming and pre-tool-execution
checkpoints — so cancellation is REQUESTED on the real seam. What cannot be
interrupted is work already inside a tool call or a provider read; that finishes
out-of-band. What IS guaranteed immediately is local: callback/hook production
stops, active production uses bounded back-pressure without losing tokens, and
the consumer returns without waiting out the model.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.chat_client as cc  # noqa: E402
from agents.chat_client import (  # noqa: E402
    LocalChatClient, StreamCancellation, _LocalCancelGate, _put_local_event,
    _SSECallbackHandler, _ToolResultHook,
)


_LOCAL_RELEASE_LIMIT = 1.0


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestLocalCancelGate:
    def test_close_is_idempotent_and_visible(self):
        gate = _LocalCancelGate()
        assert gate.cancelled is False
        gate.close()
        gate.close()
        assert gate.cancelled is True

    def test_a_shared_stream_cancellation_closes_the_gate(self):
        """ONE handle serves both clients — the local path arms it with a gate."""
        gate = _LocalCancelGate()
        handle = StreamCancellation()
        handle.arm(gate)
        handle.cancel()
        assert gate.cancelled is True

    def test_release_closes_the_gate_too(self):
        gate = _LocalCancelGate()
        handle = StreamCancellation()
        handle.arm(gate)
        handle.release()
        assert gate.cancelled is True

    def test_binding_an_agent_after_cancel_cancels_it_at_once(self):
        class _Agent:
            def __init__(self):
                self.cancels = 0

            def cancel(self):
                self.cancels += 1

        gate = _LocalCancelGate()
        gate.close()
        agent = _Agent()
        gate.bind_agent(agent)
        assert agent.cancels == 1

    def test_closing_the_gate_fires_the_bound_agents_cancel_once(self):
        class _Agent:
            def __init__(self):
                self.cancels = 0

            def cancel(self):
                self.cancels += 1

        gate = _LocalCancelGate()
        agent = _Agent()
        gate.bind_agent(agent)
        gate.close()
        gate.close()
        assert agent.cancels == 1

    def test_a_failing_agent_cancel_is_never_fatal(self):
        class _Hostile:
            def cancel(self):
                raise RuntimeError("agent already torn down")

        gate = _LocalCancelGate()
        gate.bind_agent(_Hostile())
        gate.close()          # must not raise
        assert gate.cancelled is True


class TestBoundedCancellationAwareEnqueue:
    def test_put_returns_false_once_the_gate_is_closed(self):
        q: queue.Queue = queue.Queue(maxsize=4)
        gate = _LocalCancelGate()
        gate.close()
        assert _put_local_event(q, {"event": "token", "data": {}}, gate=gate) is False
        assert q.qsize() == 0

    def test_a_full_queue_backpressures_without_losing_the_event(self):
        q: queue.Queue = queue.Queue(maxsize=1)
        q.put({"event": "token", "data": {"text": "first"}})
        gate = _LocalCancelGate()
        done = threading.Event()
        landed = []

        def _producer():
            landed.append(_put_local_event(
                q, {"event": "token", "data": {"text": "second"}}, gate=gate))
            done.set()

        threading.Thread(target=_producer, daemon=True).start()
        assert not done.wait(0.1), "a full queue silently dropped the event"
        assert q.get()["data"]["text"] == "first"
        assert done.wait(1), "producer did not resume after capacity opened"
        assert landed == [True]
        assert q.get()["data"]["text"] == "second"

    def test_cancel_releases_a_producer_waiting_on_a_full_queue(self):
        q: queue.Queue = queue.Queue(maxsize=1)
        q.put({"event": "token", "data": {}})
        gate = _LocalCancelGate()
        done = threading.Event()

        def _producer():
            assert _put_local_event(
                q, {"event": "token", "data": {}}, gate=gate) is False
            done.set()

        threading.Thread(target=_producer, daemon=True).start()
        assert not done.wait(0.1)
        gate.close()
        assert done.wait(_LOCAL_RELEASE_LIMIT), \
            "cancellation did not release the backpressured producer"

    def test_handler_and_hook_drop_events_after_cancel(self):
        q: queue.Queue = queue.Queue(maxsize=64)
        gate = _LocalCancelGate()
        handler = _SSECallbackHandler(q, gate=gate)
        hook = _ToolResultHook(q, gate=gate)
        handler(data="before")
        assert q.qsize() == 1

        gate.close()
        for i in range(500):
            handler(data=f"after-{i}")
        assert hook._emit({"event": "token", "data": {}}) is False

        assert q.qsize() == 1, "events were enqueued after cancellation"


class _Hooks:
    def __init__(self):
        self.added = []

    def add_hook(self, hook):
        self.added.append(hook)


class _LongRunningAgent:
    """Emits callback tokens until its cooperative cancel signal is set.

    ``CEILING`` keeps a regression a FAILURE rather than a hang."""

    CEILING = 20.0

    def __init__(self):
        self.callback_handler = None
        self.hooks = _Hooks()
        self.cancel_calls = 0
        self.emits = 0
        self.returned = threading.Event()
        self._cancelled = threading.Event()

    def cancel(self):
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


class TestStreamHonorsTheHandle:
    def test_queue_is_bounded_in_ordinary_mode(self):
        """``maxsize=0`` (unbounded) was the H3 memory amplifier."""
        agent = _LongRunningAgent()
        handle = StreamCancellation()
        with patch.object(cc, "build_orchestrator", lambda *a, **k: agent):
            gen = LocalChatClient().stream(prompt="q", session_id="s",
                                           user_id="u", cancel=handle)
            try:
                next(gen)
                assert _wait(lambda: agent.callback_handler is not None)
                q = agent.callback_handler._q
                assert q.maxsize > 0, "the local internal queue must be BOUNDED"
                assert _wait(lambda: q.qsize() <= q.maxsize)
            finally:
                handle.cancel()
                gen.close()
        assert agent.returned.wait(5)

    def test_cancel_fires_the_real_strands_seam_and_stops_production(self):
        agent = _LongRunningAgent()
        handle = StreamCancellation()
        with patch.object(cc, "build_orchestrator", lambda *a, **k: agent):
            gen = LocalChatClient().stream(prompt="q", session_id="s",
                                           user_id="u", cancel=handle)
            next(gen)

            handle.cancel()

            assert agent.returned.wait(5), "the producer never released"
            assert agent.cancel_calls >= 1, \
                "cancel was accepted and ignored — the strands seam never fired"
            q = agent.callback_handler._q
            settled = q.qsize()
            time.sleep(0.3)
            assert q.qsize() <= settled, "the queue grew after cancellation"
            gen.close()

    def test_generator_close_alone_cancels_the_local_producer(self):
        """No handle at all: closing the stream still stops the producer."""
        agent = _LongRunningAgent()
        with patch.object(cc, "build_orchestrator", lambda *a, **k: agent):
            gen = LocalChatClient().stream(prompt="q", session_id="s", user_id="u")
            next(gen)

            gen.close()

        assert agent.returned.wait(5)
        assert agent.cancel_calls >= 1

    def test_consumer_returns_without_waiting_out_the_model(self):
        agent = _LongRunningAgent()
        handle = StreamCancellation()
        with patch.object(cc, "build_orchestrator", lambda *a, **k: agent):
            gen = LocalChatClient().stream(prompt="q", session_id="s",
                                           user_id="u", cancel=handle)
            next(gen)
            handle.cancel()
            start = time.monotonic()
            rest = list(gen)
            elapsed = time.monotonic() - start

        assert elapsed < _LongRunningAgent.CEILING / 2, \
            f"the consumer waited out the model ({elapsed:.1f}s)"
        assert rest == [], "a cancelled turn must not emit terminal frames"



class _OutOfBandAgent:
    """Ignores cooperative cancel until released, like a blocked provider read."""

    def __init__(self):
        self.callback_handler = None
        self.hooks = _Hooks()
        self.cancel_calls = 0
        self.started = threading.Event()
        self.release_model = threading.Event()
        self.returned = threading.Event()

    def cancel(self):
        self.cancel_calls += 1

    def __call__(self, prompt, **kwargs):
        self.callback_handler(data="before-cancel")
        self.hooks.added[0]._emit(
            {"event": "token", "data": {"text": "hook-before-cancel"}})
        self.started.set()
        self.release_model.wait(5)
        self.returned.set()
        return "late answer"


class TestRealLocalClientRegression:
    def test_cancel_releases_consumer_and_stops_callback_and_hook_output(self):
        agent = _OutOfBandAgent()
        handle = StreamCancellation()
        with patch.object(cc, "build_orchestrator", lambda *a, **k: agent):
            stream = LocalChatClient().stream(
                prompt="q", session_id="s", user_id="u", cancel=handle)
            try:
                next(stream)
                assert agent.started.wait(1)
                q = agent.callback_handler._q
                hook = agent.hooks.added[0]
                assert q.maxsize == cc._local_queue_maxsize()
                assert q.maxsize > 0

                handle.cancel()
                started = time.monotonic()
                assert list(stream) == []
                assert time.monotonic() - started < 1.0
                assert agent.returned.is_set() is False

                settled = q.qsize()
                for i in range(q.maxsize * 2):
                    agent.callback_handler(data=f"late-{i}")
                    hook._emit({"event": "token", "data": {"text": f"late-{i}"}})
                assert q.qsize() == settled
                assert q.qsize() <= q.maxsize
                assert agent.cancel_calls == 1
            finally:
                agent.release_model.set()
                stream.close()
        assert agent.returned.wait(2)

    def test_tokens_and_terminal_survive_bounded_queue_pressure(self, monkeypatch):
        monkeypatch.setenv("LOCAL_STREAM_QUEUE_MAX", "2")

        class _BurstAgent:
            def __init__(self):
                self.callback_handler = None
                self.hooks = _Hooks()

            def cancel(self):
                pass

            def __call__(self, prompt, **kwargs):
                for i in range(2_000):
                    self.callback_handler(data=f"burst-{i}")
                return "complete"

        from agents.events import DoneEvent, MessageEvent, MetadataEvent
        with patch.object(cc, "build_orchestrator", lambda *a, **k: _BurstAgent()):
            events = list(LocalChatClient().stream(
                prompt="q", session_id="s", user_id="u"))

        messages = [event.content for event in events
                    if isinstance(event, MessageEvent)]
        assert messages == [f"burst-{i}" for i in range(2_000)]
        assert isinstance(events[-2], MetadataEvent)
        assert isinstance(events[-1], DoneEvent)

    def test_error_remains_terminal_after_partial_output(self):
        class _FailingAgent:
            def __init__(self):
                self.callback_handler = None
                self.hooks = _Hooks()

            def cancel(self):
                pass

            def __call__(self, prompt, **kwargs):
                self.callback_handler(data="partial")
                raise RuntimeError("provider failed")

        from agents.events import ErrorEvent, MessageEvent
        with patch.object(cc, "build_orchestrator", lambda *a, **k: _FailingAgent()):
            events = list(LocalChatClient().stream(
                prompt="q", session_id="s", user_id="u"))

        assert [type(event) for event in events] == [MessageEvent, ErrorEvent]
        assert events[0].content == "partial"


class TestNormalLocalPathUnchanged:
    def test_ordinary_turn_still_yields_summary_then_done(self):
        class _Quick:
            def __init__(self):
                self.callback_handler = None
                self.hooks = _Hooks()

            def cancel(self):
                pass

            def __call__(self, prompt, **kwargs):
                self.callback_handler(data="hello")
                return "hello"

        from agents.events import DoneEvent, MessageEvent, MetadataEvent
        with patch.object(cc, "build_orchestrator", lambda *a, **k: _Quick()):
            events = list(LocalChatClient().stream(
                prompt="q", session_id="s", user_id="u"))

        kinds = [type(e) for e in events]
        assert MessageEvent in kinds
        assert MetadataEvent in kinds
        assert kinds[-1] is DoneEvent
        assert kinds.index(MetadataEvent) < kinds.index(DoneEvent)
