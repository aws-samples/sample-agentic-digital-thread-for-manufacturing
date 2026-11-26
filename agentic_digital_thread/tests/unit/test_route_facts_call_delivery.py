# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route facts record a delegation by how it was delivered, never by its result's words.

The route-facts wrapper keeps each delegation's merged ROUTE context so the
trace and the reopened chat show what the system was sent
(``route_facts.note_call``). Whether the call was sent comes from the layer
that knows it (``call_delivery``): the decline guard and the
identifier-provenance guard refuse a call without sending it, the session
keeper says whether a call on a closed session went out, and the wrapper
reads that for its own call only. A call no layer reported sending counts as
not sent. A system's own error was sent, whatever its words.

Made-up systems on two differently named schemas (``_route_facts_world``),
through the real per-turn gateway client with a fake MCP client at the
network edge. Offline.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.types import Tool as MCPTool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.types.exceptions import MCPClientInitializationError

import agents.orchestrator as orch
from agents import delegation_decline as dd
from agents import route_facts as rf
from tests.unit._route_facts_world import (  # noqa: F401 - isolated_turn is a fixture
    _SCHEMA, WORLDS, gateway, isolated_turn, result, seed, stream,
)

_IDS = [w.name for w in WORLDS]


@pytest.fixture(autouse=True)
def _fresh_turn(isolated_turn):
    rf.reset_turn()
    yield
    rf.reset_turn()


class _Edge:
    """The MCP client at the network edge: answers each tool from its own
    queue, records each call's tool use id, can hold a tool's calls until
    released, and has a session that can close (a call on a closed session is
    refused before sending, as strands refuses it)."""

    def __init__(self, answers):
        self.answers = {name: list(queue) for name, queue in answers.items()}
        self.calls = []
        self.held = {}
        self.alive = True
        self._tool_provider_started = True

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            if not self.alive:
                raise MCPClientInitializationError("the client session is not running.")
            self.calls.append((name, tool_use_id))
            if name in self.held:
                await self.held[name].wait()
            queue = self.answers.get(name) or []
            return queue.pop(0) if queue else result("Here are the rows.")

        self.call_tool_async = _call
        self.list_tools_sync = lambda *a, **k: []

    def _is_session_active(self):
        return self.alive

    def stop(self, *_args):
        self.alive = False

    def start(self):
        raise RuntimeError("the session could not be opened")


def _error(*texts):
    return {"status": "error", "toolUseId": "x", "content": [{"text": t} for t in texts]}


def _adds_context(tool_name, question) -> bool:
    args = {"question": question}
    return rf.merge(rf.turn_facts(), tool_name, args) is not args


def _declined(concept):
    return result(f"I cannot give that.\n\n[engine] declined: reason_kind=not_connected; "
                  f"concept={concept}")


async def _run_tool(client, name, tool_input, tool_use_id):
    """One delegation through Strands' own MCP tool adapter, on the running loop."""
    tool = MCPAgentTool(MCPTool(name=name, inputSchema=_SCHEMA), client)
    return [event async for event in tool.stream(
        {"toolUseId": tool_use_id, "name": name, "input": tool_input}, {})]


def _peer_then(monkeypatch, world, target_results):
    """The peer answered with its run ids; the target's calls answer
    ``target_results`` in order. Returns (the edge, the client)."""
    seed(world)
    edge = _Edge({world.tool(world.peer): [result(world.peer_answer)],
                  world.tool(world.target): list(target_results)})
    client = gateway(monkeypatch, edge)
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t-peer")
    return edge, client


def _refusal_words(kind: str, world) -> str:
    """A remote error's first block, opening with one guard's refusal words."""
    if kind == "decline":
        return f"{dd.REFUSAL_PREFIX} the source holds no such rows."
    return f"[engine] Not sent to {world.target}: the source is read only today."


@pytest.mark.parametrize("words", ["decline", "identifier"])
@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
@pytest.mark.parametrize("blocks", [1, 2], ids=["one-block", "multi-block"])
def test_a_remote_error_in_the_refusal_words_keeps_its_context(monkeypatch, world, words,
                                                               blocks):
    """Reproduction (fails before the change): the target system was sent the
    call and answered with an error of its own whose first block opens with
    the guards' refusal words. The old text check read it as a refusal and
    dropped the context the system was sent."""
    text = _refusal_words(words, world)
    edge, client = _peer_then(monkeypatch, world,
                              [_error(text, *(["Details follow."] * (blocks - 1)))])
    question = {"question": world.target_question}
    assert _adds_context(world.tool(world.target), question["question"])

    stream(client, world.tool(world.target), question, "t-target")

    assert edge.calls[-1] == (world.tool(world.target), "t-target")
    received = rf.turn_facts().received.get("t-target", "")
    assert received.startswith("```ROUTE:") and f"target_system: {world.target}" in received
    assert len(rf.turn_facts().results) == 1  # the peer's; the error carries no values


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_successful_answer_in_the_refusal_words_keeps_its_context(monkeypatch, world):
    """Guard (passes before the change too): a successful answer was sent,
    whatever its words."""
    edge, client = _peer_then(monkeypatch, world,
                              [result(f"{dd.REFUSAL_PREFIX} quoted from the source's notes.")])
    stream(client, world.tool(world.target), {"question": world.target_question}, "t-target")
    assert "t-target" in rf.turn_facts().received


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_decline_refused_repeat_records_nothing(monkeypatch, world):
    """Guard (passes before the change too): the decline guard refuses the
    repeat, which never reaches the edge; nothing of it is recorded."""
    edge, client = _peer_then(monkeypatch, world, [_declined(world.concept)])
    stream(client, world.tool(world.target), {"question": world.target_question}, "t-decl")
    repeat = f"And {world.run_ids[0]} on its own?"
    assert _adds_context(world.tool(world.target), repeat)
    results = list(rf.turn_facts().results)

    events = stream(client, world.tool(world.target), {"question": repeat}, "t-repeat")

    assert dd.REFUSAL_PREFIX in str(events[-1])
    assert [tid for _name, tid in edge.calls] == ["t-peer", "t-decl"]
    assert "t-repeat" not in rf.turn_facts().received
    assert rf.turn_facts().results == results


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_call_the_closed_session_refused_records_nothing(monkeypatch, world):
    """Reproduction (fails before the change): the session closed and could
    not be reopened, so the keeper answers "could not be reopened" and the
    call never went out. The old wrapper kept the context the merge built
    for it."""
    edge, client = _peer_then(monkeypatch, world, [])
    edge.alive = False

    events = stream(client, world.tool(world.target), {"question": world.target_question},
                    "t-target")

    assert "could not be reopened" in str(events[-1])
    assert [tid for _name, tid in edge.calls] == ["t-peer"]
    assert "t-target" not in rf.turn_facts().received


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_sent_attempt_is_not_undone_by_an_unsent_retry(monkeypatch, world):
    """Guard (passes before the change too): the first attempt reached the
    gateway and got a retryable error; the session closed during the backoff
    and could not be reopened, so the retry was not sent. The first attempt
    carried the context, which stays; the error carries no values."""
    edge, client = _peer_then(monkeypatch, world, [_error("Service Unavailable")])

    async def _close_during_backoff(*_args, **_kwargs):
        edge.alive = False

    monkeypatch.setattr(asyncio, "sleep", _close_during_backoff)
    events = stream(client, world.tool(world.target), {"question": world.target_question},
                    "t-target")

    assert "could not be reopened" in str(events[-1])
    assert [tid for _name, tid in edge.calls] == ["t-peer", "t-target"]
    assert rf.turn_facts().received["t-target"].startswith("```ROUTE:")
    assert len(rf.turn_facts().results) == 1


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_parallel_refused_and_sent_pair_each_get_their_own_delivery(monkeypatch, world):
    """Guard (passes before the change too): two delegations in flight at
    once, each its own asyncio task as Strands runs them. The peer's call is
    held at the edge while the decline guard refuses a repeat to the target;
    the refusal is recorded as not sent while the peer's call is still open,
    and the peer's call, released after, is recorded as sent."""
    edge, client = _peer_then(monkeypatch, world, [_declined(world.concept)])
    stream(client, world.tool(world.target), {"question": world.target_question}, "t-decl")
    repeat = {"question": f"And {world.run_ids[0]} on its own?"}
    peer = {"question": world.peer_question}
    assert _adds_context(world.tool(world.target), repeat["question"])
    assert _adds_context(world.tool(world.peer), peer["question"])

    async def _pair():
        edge.held[world.tool(world.peer)] = asyncio.Event()
        sent = asyncio.create_task(_run_tool(client, world.tool(world.peer), peer, "t-sent"))
        for _ in range(1000):
            if (world.tool(world.peer), "t-sent") in edge.calls:
                break
            await asyncio.sleep(0)
        assert (world.tool(world.peer), "t-sent") in edge.calls, "the peer's call is held"
        refused = await asyncio.create_task(
            _run_tool(client, world.tool(world.target), repeat, "t-refused"))
        assert not sent.done()
        assert "t-refused" not in rf.turn_facts().received
        edge.held[world.tool(world.peer)].set()
        return refused, await sent

    refused, _sent = asyncio.run(_pair())

    assert dd.REFUSAL_PREFIX in str(refused[-1])
    assert "t-refused" not in rf.turn_facts().received
    assert rf.turn_facts().received["t-sent"].startswith("```ROUTE:")
    assert (world.tool(world.target), "t-refused") not in edge.calls


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_a_call_no_layer_reported_sending_records_nothing(world):
    """New rule (fails before the change, where every returned call was
    recorded): with no layer below the wrapper reporting that it sent the
    call, the call counts as not sent and nothing of it is recorded."""
    seed(world)
    facts = rf.turn_facts()

    class _Silent:
        async def call_tool_async(self, *_args, **_kwargs):
            return result("Here are the rows.")

    client = orch._wrap_gateway_client_with_route_facts(_Silent(), facts)
    assert _adds_context(world.tool(world.peer), world.peer_question)

    asyncio.run(client.call_tool_async("t-peer", world.tool(world.peer),
                                       {"question": world.peer_question}))

    assert facts.received == {} and facts.results == []


@pytest.mark.parametrize("world", WORLDS, ids=_IDS)
def test_note_call_records_by_delivery(world):
    """Unit test of the typed delivery (new in this change): nothing for a
    call not sent; the context but no values for one that may have acted or
    that its system answered with an error; both for a sent answer. A direct
    call without a delivery records as sent."""
    from agents.call_delivery import CallDelivery
    seed(world)
    facts = rf.turn_facts()
    tool, answer = world.tool(world.peer), result(world.peer_answer)

    rf.note_call(facts, tool, "t-not", "ctx-not", answer, delivery=CallDelivery.NOT_SENT)
    rf.note_call(facts, tool, "t-may", "ctx-may", answer,
                 delivery=CallDelivery.MAY_HAVE_ACTED)
    rf.note_call(facts, tool, "t-err", "ctx-err", _error("The query failed."),
                 delivery=CallDelivery.SENT)
    assert facts.received == {"t-may": "ctx-may", "t-err": "ctx-err"}
    assert facts.results == []

    rf.note_call(facts, tool, "t-ok", "ctx-ok", answer)
    assert facts.received["t-ok"] == "ctx-ok"
    assert [r.system for r in facts.results] == [world.peer]


def test_an_attempt_is_recorded_into_the_open_call_only():
    """Unit test of ``call_delivery`` (new in this change): nothing happens
    outside an open call; an unsent attempt does not undo an earlier delivered
    one; a write from a task the call started, or from a thread it awaits
    through ``asyncio.to_thread``, reaches the call; a call with no write
    reads as not sent."""
    from agents.call_delivery import CallDelivery, outcome, record, tracking
    record(CallDelivery.SENT)  # no open call: nothing to write into

    with tracking() as call:
        assert outcome(call) is CallDelivery.NOT_SENT
        record(CallDelivery.SENT)
        record(CallDelivery.NOT_SENT)
    assert outcome(call) is CallDelivery.SENT

    with tracking() as call:
        record(CallDelivery.NOT_SENT)
        record(CallDelivery.MAY_HAVE_ACTED)
    assert outcome(call) is CallDelivery.MAY_HAVE_ACTED

    # The writes below record SENT, never the not-sent default, so a write
    # that a child task or thread lost would fail the assertion.
    async def _inner():
        record(CallDelivery.SENT)

    async def _from_child_task():
        with tracking() as opened:
            await asyncio.create_task(_inner())
        return opened

    async def _from_thread():
        with tracking() as opened:
            await asyncio.to_thread(record, CallDelivery.SENT)
        return opened

    assert outcome(asyncio.run(_from_child_task())) is CallDelivery.SENT
    assert outcome(asyncio.run(_from_thread())) is CallDelivery.SENT
