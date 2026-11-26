# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runtime-side keepalive heartbeat at the ``_runtime_entrypoint`` seam.

When the orchestrator's event stream is idle (e.g. a system-agent call parked
on a slow MCP tool result for ~5 min), the runtime now emits a tiny heartbeat
frame every N seconds so the app's ``Config(read_timeout=300)`` socket read
does not time out and discard an answer the runtime would have finished.

The wrapper lives at the SANCTIONED seam — ``_runtime_entrypoint(...).selected``
wraps ``implementation(*args)`` for both the legacy and the integrity path —
OUTSIDE the gate-frozen legacy invoke body. These tests pin:

* a tiny-interval wrapper yields heartbeats while the inner is idle, passes
  real events through unchanged and in order, never runs two ``__anext__`` at
  once, closes the inner generator on early consumer exit, and propagates
  inner exceptions;
* ``selected()`` emits heartbeats on BOTH the legacy and the integrity path and
  still appends the A-lite marker on the legacy path;
* the interval coercion is fail-closed (finite, >0, floor/ceiling).

Offline: no AWS, no network.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest

from agents import runtime_app


HEARTBEAT_KEY = "adt_heartbeat"

#: Module-level ContextVar used to prove the inner generator keeps ONE
#: consistent context across its own steps when driven through the wrapper.
_PROBE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "adt_heartbeat_probe", default="unset")


def test_heartbeat_key_is_the_reserved_single_key():
    assert runtime_app.HEARTBEAT_KEY == HEARTBEAT_KEY
    frame = runtime_app.HEARTBEAT_FRAME
    assert isinstance(frame, dict) and list(frame.keys()) == [HEARTBEAT_KEY]
    # No text, no timestamps/data — one reserved key only.
    assert frame[HEARTBEAT_KEY] in (1, True)


# ---------------------------------------------------------------------------
# Wrapper: heartbeats while idle, pass-through unchanged + in order
# ---------------------------------------------------------------------------

def _drain(agen) -> list:
    out: list = []

    async def run():
        async for event in agen:
            out.append(event)

    asyncio.run(run())
    return out


def test_wrapper_emits_heartbeat_while_inner_idle_then_passes_event():
    """One slow event preceded by idle gaps longer than the interval yields at
    least one heartbeat, and the real event survives unchanged and last."""
    real = {"data": "late answer"}

    async def slow():
        await asyncio.sleep(0.25)   # idle >> interval below
        yield real

    out = _drain(runtime_app._with_stream_heartbeat(slow(), interval=0.02))
    assert out[-1] == real
    heartbeats = [e for e in out if e == runtime_app.HEARTBEAT_FRAME]
    assert heartbeats, "expected at least one heartbeat during the idle gap"
    # Only heartbeats and the one real event — nothing else invented.
    assert all(e == runtime_app.HEARTBEAT_FRAME or e == real for e in out)


def test_wrapper_passes_events_through_unchanged_and_in_order_when_fast():
    events = [{"a": 1}, {"b": 2}, {"c": 3}]

    async def fast():
        for event in events:
            yield event

    # Large interval ⇒ never idle long enough to fire a heartbeat.
    out = _drain(runtime_app._with_stream_heartbeat(fast(), interval=1000))
    assert out == events


def test_wrapper_never_runs_two_anext_on_inner_at_once():
    """A single pending ``__anext__`` is re-awaited after each heartbeat — the
    inner must never see a second concurrent ``__anext__`` call."""
    concurrent = 0
    max_concurrent = 0

    class Inner:
        def __init__(self):
            self._n = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            try:
                await asyncio.sleep(0.2)
                self._n += 1
                if self._n > 1:
                    raise StopAsyncIteration
                return {"only": "event"}
            finally:
                concurrent -= 1

    out = _drain(runtime_app._with_stream_heartbeat(Inner(), interval=0.02))
    assert max_concurrent == 1
    assert {"only": "event"} in out
    assert any(e == runtime_app.HEARTBEAT_FRAME for e in out)


def test_wrapper_closes_inner_on_early_consumer_exit():
    closed = {"flag": False}

    class Inner:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0)
            return {"tick": 1}

        async def aclose(self):
            closed["flag"] = True

    async def run():
        agen = runtime_app._with_stream_heartbeat(Inner(), interval=1000)
        got = []
        async for event in agen:
            got.append(event)
            break               # consumer leaves early
        await agen.aclose()     # triggers the wrapper's finally / GeneratorExit
        return got

    got = asyncio.run(run())
    assert got == [{"tick": 1}]
    assert closed["flag"] is True


def test_wrapper_propagates_inner_exception():
    class Boom(RuntimeError):
        pass

    async def inner():
        if False:               # make it an async generator
            yield {}
        raise Boom("inner blew up")

    with pytest.raises(Boom):
        _drain(runtime_app._with_stream_heartbeat(inner(), interval=1000))


# ---------------------------------------------------------------------------
# selected(): heartbeats on BOTH paths, A-lite marker preserved on legacy
# ---------------------------------------------------------------------------

LEGACY_EVENTS = [{"data": "hello"}, {"intelligence_summary": {"total_tool_calls": 1}}]
INTEGRITY_EVENTS = [
    {"data": "gated"},
    {"response_integrity": {"verdict": "accepted", "mode": "enforce"}},
]


def _run_selected(monkeypatch, *, integrity: bool) -> list:
    async def legacy(payload, context):
        yield LEGACY_EVENTS[0]
        await asyncio.sleep(0.25)        # idle gap >> interval
        yield LEGACY_EVENTS[1]

    async def integrity_impl(payload, context, build_orchestrator):
        yield INTEGRITY_EVENTS[0]
        await asyncio.sleep(0.25)        # idle gap >> interval
        yield INTEGRITY_EVENTS[1]

    monkeypatch.setattr(runtime_app, "_invoke_integrity_runtime", integrity_impl)
    # Bypass the fail-closed floor (1 s) for a fast test: patch the interval
    # source the wrapper reads, so the 0.25 s idle gap fires a heartbeat.
    monkeypatch.setattr(runtime_app, "_heartbeat_interval_seconds", lambda: 0.02)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    if integrity:
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    else:
        monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())
    events: list = []

    async def run():
        async for event in selected({}, None):
            events.append(event)

    asyncio.run(run())
    return events


def test_selected_legacy_path_emits_heartbeats_and_keeps_marker(monkeypatch):
    events = _run_selected(monkeypatch, integrity=False)
    non_heartbeat = [e for e in events if e != runtime_app.HEARTBEAT_FRAME]
    # Real stream is byte-identical: legacy events, then the trailing marker.
    assert non_heartbeat == LEGACY_EVENTS + [{"response_integrity": {"mode": "report-only"}}]
    assert any(e == runtime_app.HEARTBEAT_FRAME for e in events)


def test_selected_integrity_path_emits_heartbeats_no_extra_marker(monkeypatch):
    events = _run_selected(monkeypatch, integrity=True)
    non_heartbeat = [e for e in events if e != runtime_app.HEARTBEAT_FRAME]
    assert non_heartbeat == INTEGRITY_EVENTS
    assert any(e == runtime_app.HEARTBEAT_FRAME for e in events)


# ---------------------------------------------------------------------------
# Interval coercion: fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["0", "-5", "", "abc", "nan", "inf"])
def test_interval_coercion_rejects_bad_values_uses_default(monkeypatch, raw):
    monkeypatch.setenv("ADT_RUNTIME_STREAM_HEARTBEAT_SECONDS", raw)
    interval = runtime_app._heartbeat_interval_seconds()
    assert isinstance(interval, (int, float))
    assert 0 < interval < float("inf")


def test_interval_coercion_default_when_unset(monkeypatch):
    monkeypatch.delenv("ADT_RUNTIME_STREAM_HEARTBEAT_SECONDS", raising=False)
    assert runtime_app._heartbeat_interval_seconds() == 30


def test_interval_coercion_clamps_to_ceiling(monkeypatch):
    from agents.chat_client import RUNTIME_STREAM_READ_TIMEOUT_S
    # A huge AND a merely-too-large override both resolve below the reader's
    # socket read timeout, not merely below some larger ceiling (HIGH r1).
    for raw in ("100000", "150", "299", "600"):
        monkeypatch.setenv("ADT_RUNTIME_STREAM_HEARTBEAT_SECONDS", raw)
        interval = runtime_app._heartbeat_interval_seconds()
        assert interval < RUNTIME_STREAM_READ_TIMEOUT_S


def test_heartbeat_ceiling_is_safely_below_client_read_timeout():
    """The runtime's idle-heartbeat ceiling MUST be strictly below the app's
    botocore read timeout, or an override in [timeout, ceiling] would let the
    socket time out before the first heartbeat — the exact lost-answer bug."""
    from agents.chat_client import RUNTIME_STREAM_READ_TIMEOUT_S
    assert runtime_app._HEARTBEAT_CEILING_SECONDS < RUNTIME_STREAM_READ_TIMEOUT_S
    # Keep the documented default/floor intact.
    assert runtime_app._HEARTBEAT_DEFAULT_SECONDS == 30
    assert runtime_app._HEARTBEAT_FLOOR_SECONDS == 1


# ---------------------------------------------------------------------------
# Context propagation: the inner turn must keep ONE consistent context across
# its own steps. The pre-r2 wrapper ran each ``__anext__`` in a fresh task
# (ensure_future per event), which snapshots the context at task creation, so
# a contextvar SET in one step was invisible to the next and a token from one
# step could not be reset in another ("was created in a different Context").
# ---------------------------------------------------------------------------

def _drain_in_fresh_context(agen) -> list:
    """Run the wrapper inside a dedicated copied context (what the real outer
    ``async for`` in BedrockAgentCoreApp does — the consumer owns one context)."""
    out: list = []

    async def run():
        async for event in agen:
            out.append(event)

    def go():
        asyncio.run(run())

    ctx = contextvars.copy_context()
    ctx.run(go)
    return out


def test_inner_contextvar_set_in_step_one_is_visible_in_step_two():
    """A ContextVar the inner generator SETS during step 1 must be readable by
    the same generator in step 2 — proving the whole turn runs in one context."""
    async def inner():
        _PROBE_VAR.set("from-step-1")
        yield {"step": 1}
        # Second step reads what the first step set — only works if both steps
        # execute in the SAME context (one producer task, not one-per-anext).
        yield {"step": 2, "seen": _PROBE_VAR.get()}

    out = _drain_in_fresh_context(
        runtime_app._with_stream_heartbeat(inner(), interval=1000))
    steps = [e for e in out if "step" in e]
    assert steps == [{"step": 1}, {"step": 2, "seen": "from-step-1"}]


def test_inner_contextvar_token_set_then_reset_across_a_yield():
    """``token = var.set(x)`` before a yield and ``var.reset(token)`` after it
    must not raise — the set and the reset must happen in the SAME context."""
    error: dict = {}

    async def inner():
        token = _PROBE_VAR.set("scoped")
        try:
            yield {"phase": "during", "value": _PROBE_VAR.get()}
        finally:
            # Resetting a token created in a different context raises
            # ValueError: <Token> was created in a different Context.
            _PROBE_VAR.reset(token)

    async def run():
        got = []
        try:
            async for event in runtime_app._with_stream_heartbeat(
                    inner(), interval=1000):
                got.append(event)
        except ValueError as exc:   # token-context mismatch surfaces here
            error["exc"] = exc
        return got

    def go():
        return asyncio.run(run())

    got = contextvars.copy_context().run(go)
    assert error == {}, f"token reset raised across contexts: {error.get('exc')}"
    during = [e for e in got if e.get("phase") == "during"]
    assert during == [{"phase": "during", "value": "scoped"}]
    # After the generator's finally, the outer context's var is back to default
    # (the inner scope's set/reset stayed inside the producer's own context).
    assert _PROBE_VAR.get() == "unset"


# ---------------------------------------------------------------------------
# The inner generator's OWN finally must run — the legacy invoke body does
# turn-complete logging and learning capture there — both on a normal end and
# on an EARLY consumer close.
# ---------------------------------------------------------------------------

def test_inner_finally_runs_on_normal_end():
    ran: dict = {"finally": False}

    async def inner():
        try:
            yield {"n": 1}
            yield {"n": 2}
        finally:
            ran["finally"] = True

    out = _drain(runtime_app._with_stream_heartbeat(inner(), interval=1000))
    assert [e for e in out if "n" in e] == [{"n": 1}, {"n": 2}]
    assert ran["finally"] is True


def test_inner_finally_runs_on_early_consumer_close():
    ran: dict = {"finally": False}

    async def inner():
        try:
            while True:
                yield {"tick": 1}
        finally:
            ran["finally"] = True

    async def run():
        agen = runtime_app._with_stream_heartbeat(inner(), interval=1000)
        got = []
        async for event in agen:
            got.append(event)
            break                 # consumer leaves after the first event
        await agen.aclose()       # must drive the inner generator's finally
        return got

    got = asyncio.run(run())
    assert got == [{"tick": 1}]
    assert ran["finally"] is True


# ---------------------------------------------------------------------------
# Pacing (MED r1): direct-iteration lead bound. After the consumer takes event
# N and PAUSES before resuming past its yield, the producer must not have
# pulled N+1 from the inner generator — a strict one-event lead, not two
# (one queued + one already pulled). The producer only advances the inner
# generator when the consumer acknowledges it resumed.
# ---------------------------------------------------------------------------

def test_producer_does_not_pull_next_until_consumer_resumes_past_yield():
    produced: list[int] = []
    resume = asyncio.Event()      # the test releases the consumer when it says so

    async def inner():
        n = 0
        while n < 5:
            produced.append(n)    # record each pull from the inner generator
            yield {"n": n}
            n += 1

    async def run():
        agen = runtime_app._with_stream_heartbeat(inner(), interval=1000)
        first = await agen.__anext__()       # take event 0
        # Consumer has event 0 but has NOT resumed past the wrapper's `yield 0`
        # yet (we have not asked for the next item). Give the loop ample turns:
        # a correct one-event-lead producer is now blocked on the resume ack and
        # must NOT have pulled event 1 from the inner generator.
        for _ in range(50):
            await asyncio.sleep(0)
        ahead_snapshot = list(produced)
        # Now resume: pull the rest so the generator can finish/clean up.
        rest = [first]
        async for event in agen:
            rest.append(event)
        return first, ahead_snapshot, rest

    first, ahead_snapshot, rest = asyncio.run(run())
    assert first == {"n": 0}
    # Only event 0 had been pulled from the inner generator while the consumer
    # was paused on event 0 — never event 1 (which would be a 2-event lead).
    assert ahead_snapshot == [0], f"inner ran ahead: produced={ahead_snapshot}"
    # And nothing was lost or reordered once the consumer resumed.
    assert [e for e in rest if "n" in e] == [{"n": i} for i in range(5)]


def test_heartbeat_still_fires_while_producer_blocked_in_slow_inner_step():
    """Pacing must not suppress heartbeats: while the producer is parked inside
    a slow inner step (no event available), the consumer still times out and
    emits a heartbeat."""
    real = {"data": "after a long inner step"}

    async def inner():
        await asyncio.sleep(0.25)     # slow first inner step, idle >> interval
        yield real

    out = _drain(runtime_app._with_stream_heartbeat(inner(), interval=0.02))
    assert out[-1] == real
    assert any(e == runtime_app.HEARTBEAT_FRAME for e in out)
