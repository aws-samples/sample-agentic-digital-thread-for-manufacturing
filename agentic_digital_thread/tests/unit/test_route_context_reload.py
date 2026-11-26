# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reopened chat shows the route context its delegations were sent.

The gateway merges the route seed's facts into a delegation's ``kg_context``
after the model wrote its arguments, and the stored turn the Recent sessions
reload rebuilds from holds only the model's arguments. So a chat reopened
from Recent sessions lost the Routing context the live trace showed. The live
chat now keeps each forwarded route context app-side
(``route_context_store``, keyed by session and tool use id, with the owner),
and ``GET /api/memory/events`` replays it as the call's ``route_context``.
Never under the response-integrity gate, never for another user.

Offline: the live frame goes through the chat client's own parser, the reload
through the events route with AWS faked (``test_memory_events_rehydrate``).
"""
from __future__ import annotations

import pytest

from agents import route_context_store as store
from agents.chat_client import ThinkingFilter, _CloudStreamState
from agents.job_store import JobStore
from tests.unit.test_memory_events_rehydrate import _event, _route

TOOL = "line-log___query_line_log"
QUESTION = "For runs (run_code) R-20261022-0001: which line made it?"
ROUTE = ("```ROUTE:\ntarget_system: line-log\ntranslated_identifiers: not given\n"
         "canonical_concept: batch-lot\nscope: not given\n"
         "join_rule: jp-lr-1 line-log.floor.runs.run_code -> order-book.books.lines.lot_ref\n```")


@pytest.fixture(autouse=True)
def _memory_store():
    store._job_store = JobStore(kind="route-context", table=None)
    yield
    store._job_store = None


class _Log:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


def _live_turn(integrity_mode=False, session_id="s1", owner="u1"):
    """The live chat reads the runtime's frames for one delegation whose
    model arguments had no kg_context; the runtime forwarded the merged one."""
    state = _CloudStreamState(ThinkingFilter(), integrity_mode=integrity_mode, log=_Log(),
                              owner_id=owner, session_id=session_id, runtime_session_id="r")
    use = {"role": "assistant", "content": [{"toolUse": {
        "toolUseId": "t2", "name": TOOL, "input": {"question": QUESTION}}}]}
    result = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t2", "status": "success", "adtRouteContext": ROUTE,
        "content": [{"text": "Run R-20261022-0001 ran on line 3."}]}}]}
    return [e for frame in ({"message": use}, {"message": result})
            for e in state.events_for(frame)]


def _stored_turn():
    """The same turn as Recent sessions reads it back: the model's arguments."""
    return [
        _event("e1", 0, "user", [{"text": "Which batches failed?"}]),
        _event("e2", 1, "assistant", [
            {"toolUse": {"toolUseId": "t2", "name": TOOL, "input": {"question": QUESTION}}}]),
        _event("e3", 2, "user", [
            {"toolResult": {"toolUseId": "t2", "status": "success",
                            "content": [{"text": "Run R-20261022-0001 ran on line 3."}]}}]),
        _event("e4", 3, "assistant", [{"text": "Run R-20261022-0001 passed."}]),
    ]


def _blocks(resp, kind):
    return [b for m in resp["messages"] for b in m["blocks"] if b["type"] == kind]


def test_a_reopened_chat_replays_the_route_context_the_model_left_out():
    _live_turn()
    resp, _ = _route(_stored_turn())
    (use,) = _blocks(resp, "tool_use")
    (result,) = _blocks(resp, "tool_result")
    assert use["input"] == {"question": QUESTION}
    assert result["route_context"] == ROUTE


def test_the_store_is_scoped_to_the_owner_and_bounded():
    assert store.put("u1", "s1", "t9", "x" * (store.MAX_CHARS + 50))
    assert store.for_session("u1", "s1", ["t9"]) == {"t9": "x" * store.MAX_CHARS}
    assert store.for_session("u2", "s1", ["t9"]) == {}
    assert not store.put("u1", "bad session id!", "t9", ROUTE)
    assert not store.put("u1", "s1", "", ROUTE)
    assert not store.put("u1", "s1", "t8", "   ")


def test_a_call_without_a_forwarded_context_reloads_without_one():
    resp, _ = _route(_stored_turn())
    (result,) = _blocks(resp, "tool_result")
    assert "route_context" not in result


def test_integrity_mode_neither_stores_nor_replays_it():
    _live_turn(integrity_mode=True)
    assert store.for_session("u1", "s1", ["t2"]) == {}
    store.put("u1", "s1", "t2", ROUTE)
    resp, _ = _route(_stored_turn(), integrity="enforce")
    assert "route_context" not in str(resp)
    assert "ROUTE:" not in str(resp)
