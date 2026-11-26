# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime-side deterministic chart injection.

ChartRedactionHook (agents/orchestrator.py) queues extracted chart tags on
``agent._pending_chart_blocks``; the legacy stream loop in
``runtime_app._run_orchestrator`` drains them after every yielded event and
injects them as plain ``{"data": …}`` events — exactly once per rendered
chart (title + content-hash dedup). Tags queued AT the terminal
event are injected right BEFORE the result event.
``RESPONSE_INTEGRITY_MODE=enforce`` still drains (the hook keeps the
model context clean in every mode) but never injects.

Pinned here:

* pending tags are yielded once as a data event; a byte-identical re-queue
  is skipped, while a same-title different-window tag and a new title pass;
* enforce mode (monkeypatched env) ⇒ the stream yields NO chart data event,
  and the queue is still drained;
* the drain helper is fail-soft — an error yields no chart, never raises;
* the result event stays LAST; terminal-event charts surface before it;
* end-to-end through the captured legacy entrypoint: tags queued mid-stream
  by the (simulated) hook arrive as data events in stream order.

All payloads are SYNTHETIC (no dataset literals); everything runs offline.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import agents.orchestrator as orchestrator
import agents.runtime_app as runtime_app

TAG_A = '<chart-block type="line" title="series-a" labels="[1,2]" data="[10,20]"/>'
TAG_B = '<chart-block type="line" title="series-b" labels="[1,2]" data="[30,40]"/>'
# Same TITLE as TAG_A, different data — must dedup against TAG_A.
TAG_A_ALT = '<chart-block type="line" title="series-a" labels="[3,4]" data="[50,60]"/>'
TAG_C = '<chart-block type="line" title="series-c" labels="[1]" data="[9]"/>'


# --------------------------------------------------------------------------- #
# _drain_pending_chart_text — the injection-text helper
# --------------------------------------------------------------------------- #
def test_drain_text_yields_once_then_dedups(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A, TAG_B])
    text = runtime_app._drain_pending_chart_text(agent)
    assert TAG_A in text and TAG_B in text
    assert text.startswith("\n\n") and text.endswith("\n\n")
    # Queue is cleared; nothing further to inject.
    assert runtime_app._drain_pending_chart_text(agent) == ""
    # Byte-identical re-queue (guaranteed-charts overlap) is skipped; a
    # same-title different-window tag AND a new title both pass (H2).
    agent._pending_chart_blocks.extend([TAG_A, TAG_A_ALT, TAG_C])
    text2 = runtime_app._drain_pending_chart_text(agent)
    assert text2.count("series-a") == 1  # TAG_A_ALT only — TAG_A deduped
    assert TAG_A_ALT in text2
    assert TAG_C in text2


def test_drain_text_empty_agent(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert runtime_app._drain_pending_chart_text(SimpleNamespace()) == ""


def test_enforce_mode_drains_but_never_injects(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A])
    assert runtime_app._drain_pending_chart_text(agent) == ""
    # The hook queue was still drained — enforce drops, never accumulates.
    assert agent._pending_chart_blocks == []


def test_drain_text_failure_is_fail_soft(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)

    class _Hostile:
        @property
        def _pending_chart_blocks(self):
            raise RuntimeError("boom")

    assert runtime_app._drain_pending_chart_text(_Hostile()) == ""


# --------------------------------------------------------------------------- #
# _wrap_stream_with_chart_injection — the stream-side seam
# --------------------------------------------------------------------------- #
def test_stream_wrapper_never_yields_past_the_result_event(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = SimpleNamespace()

    async def stream(_prompt, **_kwargs):
        yield {"data": "text"}
        agent._pending_chart_blocks = [TAG_A]
        # Tags queued right before the terminal event must NOT surface after
        # it: Agent.__call__/invoke_async read the LAST event's result key.
        yield {"result": "final"}

    wrapped = runtime_app._wrap_stream_with_chart_injection(agent, stream)

    async def collect():
        return [event async for event in wrapped("p")]

    events = asyncio.run(collect())
    # The result event stays LAST (nothing is ever yielded past it)…
    assert events[-1] == {"result": "final"}
    # …and the terminal-event charts are injected BEFORE it, not dropped
    # (the stub already told the model these charts rendered).
    assert any("data" in e and TAG_A in str(e.get("data", "")) for e in events)
    assert TAG_A in str(events[-2].get("data", ""))


def test_chart_injecting_build_wraps_stream(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)

    class _Agent:
        async def stream_async(self, _prompt, **_kwargs):
            self._pending_chart_blocks = [TAG_A]
            yield {"data": "text"}

    build = runtime_app._chart_injecting_build(lambda **_k: _Agent())
    agent = build(session_id="s", user_id="u")

    async def collect():
        return [event async for event in agent.stream_async("p")]

    events = asyncio.run(collect())
    assert {"data": "text"} in events
    assert any("data" in e and TAG_A in e["data"] for e in events)


# --------------------------------------------------------------------------- #
# End-to-end through the captured legacy entrypoint
# --------------------------------------------------------------------------- #
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


class _ChartAgent:
    """Simulates the hook's queueing between stream events: the real hook
    fires on MessageAddedEvent (a by-reference append to agent.messages)
    BEFORE the same event reaches the stream loop."""

    async def stream_async(self, _prompt, **_kwargs):
        yield {"data": "intro "}
        self._pending_chart_blocks = [TAG_A, TAG_B]
        yield {"data": "analysis"}
        # Guaranteed-charts overlap: same title again + one new title.
        self._pending_chart_blocks.extend([TAG_A_ALT, TAG_C])
        yield {"data": " conclusion"}


def _collect_legacy_events(monkeypatch, agent):
    fake_module = SimpleNamespace(BedrockAgentCoreApp=_FakeRuntimeApp)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", fake_module)
    monkeypatch.setattr(orchestrator, "build_orchestrator", lambda **_k: agent)
    monkeypatch.setattr(runtime_app, "_graph_first_prelude", lambda p: (p, []))
    monkeypatch.setattr(runtime_app, "_record_rule_usage_hook",
                        lambda *_a, **_k: None)
    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke

    async def collect():
        return [event async for event in invoke(
            {"prompt": "chart the telemetry", "sessionId": "s", "userId": "u"},
            SimpleNamespace(session_id="runtime"),
        )]

    return asyncio.run(collect())


def test_legacy_loop_injects_pending_tags_exactly_once(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    events = _collect_legacy_events(monkeypatch, _ChartAgent())
    datas = [e["data"] for e in events if isinstance(e, dict) and "data" in e]
    joined = "".join(datas)
    # Each distinct rendered chart arrives exactly once, as data events.
    assert joined.count(TAG_A) == 1
    assert joined.count(TAG_B) == 1
    assert joined.count(TAG_C) == 1
    # H2 content-hash dedup: the same-title DIFFERENT-DATA tag is a distinct
    # window and renders too (title-only dedup dropped it).
    assert joined.count(TAG_A_ALT) == 1
    # Injection rides in dedicated chart chunks: one for the first queueing,
    # one for the second batch.
    chart_chunks = [d for d in datas if "<chart-block" in d]
    assert len(chart_chunks) == 2
    # Streamed answer tokens are untouched around the injection.
    assert "intro " in joined and "analysis" in joined and " conclusion" in joined
    # Charts arrive AFTER the event that queued them (stream order).
    assert datas.index(chart_chunks[0]) > datas.index("analysis")


def test_legacy_loop_enforce_mode_yields_no_chart_event(monkeypatch):
    # Mode B + enforce routes through the LEGACY loop (the integrity runtime
    # is Mode A only) — the suppression branch must hold there too.
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    agent = _ChartAgent()
    events = _collect_legacy_events(monkeypatch, agent)
    joined = "".join(e["data"] for e in events
                     if isinstance(e, dict) and "data" in e)
    assert "<chart-block" not in joined
    # The hook queue was drained (dropped), not left to accumulate.
    assert agent._pending_chart_blocks == []
