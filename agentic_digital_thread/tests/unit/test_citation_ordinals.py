# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CitationOrdinalHook: the number stamped on a tool result is the number the
Sources chip carries.

The client numbers Sources chips by counting ``tool_start`` events whose tool
is not the bare ``explore_graph`` (provenance D2). The hook runs the same
predicate over the model's toolUse blocks and stamps each toolResult with
its ordinal, so the model copies the chip number instead of counting system
agents itself. Pinned here:

* ordinals follow call order across model rounds and skip ``explore_graph``;
* a repeated call to the same system gets its own number (two chips);
* a non-system tool (metric proposal, gateway graph copy) is counted, as
  the client counts it;
* the stamp matches ``agents.provenance.citation_ordinals`` byte for byte
  on the same call list;
* string and list toolResult content shapes are both stamped, other shapes
  left alone, and the counter restarts per invocation;
* the hook never raises into the agent loop;
* the hook is registered beside ChartRedactionHook on both constructions.

All payloads are synthetic.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from strands.hooks import BeforeInvocationEvent, MessageAddedEvent

from agents import orchestrator
from agents.citation_ordinals import (
    CitationOrdinalHook, assign_ordinals, citation_stamp, stamp_tool_result,
)
from agents.provenance import citation_ordinals


def _use(tool_id, name):
    return {"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}


def _result(tool_id, content):
    return {"toolResult": {"toolUseId": tool_id, "status": "success", "content": content}}


def _add(hook, content):
    message = {"role": "assistant" if "toolUse" in content[0] else "user", "content": content}
    hook._on_message_added(SimpleNamespace(message=message, agent=SimpleNamespace()))
    return message


def _texts(message):
    out = []
    for block in message["content"]:
        content = block["toolResult"]["content"]
        out.append(content if isinstance(content, str)
                   else " ".join(p.get("text", "") for p in content))
    return out


def test_ordinals_skip_the_graph_and_follow_call_order_across_rounds():
    hook = CitationOrdinalHook()
    _add(hook, [_use("g1", "explore_graph"), _use("a1", "sys-a___query_sys_a")])
    first = _add(hook, [_result("g1", [{"text": "graph"}]), _result("a1", [{"text": "rows"}])])
    _add(hook, [_use("b1", "sys-b___query_sys_b"), _use("a2", "sys-a___query_sys_a")])
    second = _add(hook, [_result("b1", [{"text": "rows"}]), _result("a2", [{"text": "rows"}])])
    assert _texts(first) == ["graph", "rows " + citation_stamp(1)]
    # The repeat call to sys-a is the THIRD chip, not a merge into [1].
    assert _texts(second) == ["rows " + citation_stamp(2), "rows " + citation_stamp(3)]


def test_non_system_tools_are_counted_because_the_client_counts_them():
    hook = CitationOrdinalHook()
    _add(hook, [_use("m1", "propose_metric_definition"),
                _use("g2", "digital-thread___explore_graph"),
                _use("a1", "sys-a___query_sys_a")])
    message = _add(hook, [_result("m1", [{"text": "filed"}]),
                          _result("g2", [{"text": "graph"}]),
                          _result("a1", [{"text": "rows"}])])
    assert _texts(message) == ["filed " + citation_stamp(1),
                               "graph " + citation_stamp(2),
                               "rows " + citation_stamp(3)]


def test_assign_ordinals_matches_the_provenance_counter_exactly():
    calls = [{"name": "explore_graph", "toolUseId": "g"},
             {"name": "x___query_x", "toolUseId": "x1"},
             {"name": "propose_metric_definition", "toolUseId": "m"},
             {"name": "x___query_x", "toolUseId": "x2"},
             {"name": "y___query_y", "toolUseId": ""}]
    ordinals, counter = assign_ordinals(calls, 0)
    assert ordinals == citation_ordinals(calls)
    assert counter == 4


def test_string_content_is_stamped_and_other_shapes_left_alone():
    plain = {"content": "rows here\n"}
    assert stamp_tool_result(plain, 2) is True
    assert plain["content"] == "rows here\n\n" + citation_stamp(2)
    empty = {"content": "   "}
    assert stamp_tool_result(empty, 1) is True
    assert empty["content"] == citation_stamp(1)
    other = {"content": {"json": {"rows": 1}}}
    assert stamp_tool_result(other, 1) is False
    assert other == {"content": {"json": {"rows": 1}}}


def test_the_counter_restarts_on_every_invocation():
    hook = CitationOrdinalHook()
    _add(hook, [_use("a1", "sys-a___query_sys_a")])
    hook._on_invocation_start(SimpleNamespace())
    _add(hook, [_use("a2", "sys-a___query_sys_a")])
    message = _add(hook, [_result("a2", [{"text": "rows"}])])
    assert _texts(message) == ["rows " + citation_stamp(1)]


def test_an_unknown_result_id_and_a_bad_message_never_raise():
    hook = CitationOrdinalHook()
    message = _add(hook, [_result("never-seen", [{"text": "rows"}])])
    assert _texts(message) == ["rows"]
    hook._on_message_added(SimpleNamespace(message="not a dict", agent=None))
    hook._on_message_added(SimpleNamespace(message={"content": [None, 3]}, agent=None))


def test_registers_both_events_and_is_wired_beside_the_chart_hook():
    registry = MagicMock()
    hook = CitationOrdinalHook()
    hook.register_hooks(registry)
    registered = {call.args[0] for call in registry.add_callback.call_args_list}
    assert registered == {BeforeInvocationEvent, MessageAddedEvent}
    assert "CitationOrdinalHook()" in inspect.getsource(orchestrator._base_agent_kwargs)


def test_prompt_tells_the_model_to_copy_the_stamp(monkeypatch):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    for mode in ("agent", "orchestrator"):
        prompt = orchestrator._build_orchestrator_prompt(mode)
        assert 'engine line "Cite this result as [N]"' in prompt, mode
        assert "Two results from the same system keep their own numbers." in prompt, mode
        # The anchor the scoped-guidance rewrite keys on is still intact.
        assert "Each system agent you call gets a number in order." in prompt, mode
