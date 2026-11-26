# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reply that stops for a tool but calls none is asked again (agents/empty_tool_turn.py).

The live failure (a rehearsal account, 2026-10-22, orchestrator on Claude Sonnet 5.5):
the first reply held only thinking and stop reason ``tool_use``. strands 1.42.0 ran an
empty tool step, the Bedrock formatter dropped the empty user message, and the next
request ended on the assistant reply, which a model without assistant-prefill support
rejects. These tests use the REAL ``BedrockModel`` request formatting with a stub client
that enforces that rule. Offline: no AWS, no network.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from agents.empty_tool_turn import (EMPTY_TOOL_TURN_LOG_MARKER, MAX_EMPTY_TOOL_TURN_RETRIES,
                                    EmptyToolTurnRetry, is_empty_tool_turn)

PREFILL_ERROR = ("This model does not support assistant message prefill. "
                 "The conversation must end with a user message.")


def _usage():
    return {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                         "metrics": {"latencyMs": 1}}}


def _thinking_only_tool_stop():
    """The live reply: signed thinking, then stop reason tool_use, no tool call."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "Deciding which system to ask"}}}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig-1"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(),
    ]


def _bare_tool_stop():
    """No content block at all, stop reason tool_use."""
    return [{"messageStart": {"role": "assistant"}},
            {"messageStop": {"stopReason": "tool_use"}}, _usage()]


def _text_tool_stop():
    """Answer text written, stop reason tool_use, no tool call."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Checking now."}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(),
    ]


def _tool_call(tool_id="t-1"):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_id, "name": "ask_system"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"question": "how many"}'}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(),
    ]


def _answer(text="Final answer."):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        _usage(),
    ]


class _NoPrefillClient:
    """Stub Bedrock runtime client: plays one scripted stream per call and, like a
    model without assistant-prefill support, rejects a request ending on the assistant."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.meta = SimpleNamespace(region_name="us-east-1")

    def converse_stream(self, **request):
        self.requests.append(copy.deepcopy(request))
        if request["messages"][-1]["role"] != "user":
            raise ClientError({"Error": {"Code": "ValidationException", "Message": PREFILL_ERROR}},
                              "ConverseStream")
        if not self.scripts:
            raise AssertionError("model called more times than scripted")
        return {"stream": iter(self.scripts.pop(0))}


@tool
def ask_system(question: str) -> str:
    """Ask a system a question."""
    return "3 records matched."


def _agent(scripts, hooks=()):
    model = BedrockModel(model_id="test-model", region_name="us-east-1")
    client = _NoPrefillClient(scripts)
    model.client = client
    agent = Agent(model=model, tools=[ask_system], callback_handler=None, hooks=list(hooks))
    return agent, client


def _run(agent, prompt="How many records?"):
    return asyncio.run(agent.invoke_async(prompt))


# --- the premise: without the hook the live reply ends the turn with the prefill error ---
def test_premise_thinking_only_tool_stop_fails_with_the_prefill_error():
    agent, client = _agent([_thinking_only_tool_stop(), _answer()])
    with pytest.raises(Exception) as caught:
        _run(agent)
    assert PREFILL_ERROR in repr(caught.value) + repr(caught.value.__cause__)
    assert client.requests[-1]["messages"][-1]["role"] == "assistant"


# --- with the hook the turn answers ---
@pytest.mark.parametrize("empty", [_thinking_only_tool_stop, _bare_tool_stop])
def test_empty_tool_stop_is_asked_again_and_the_turn_answers(empty):
    agent, client = _agent([empty(), _tool_call(), _answer("3 records.")], [EmptyToolTurnRetry()])
    result = _run(agent)
    assert "3 records." in str(result)
    assert len(client.requests) == 3
    # The retried request is the request the empty reply answered: nothing was added.
    assert client.requests[1]["messages"] == client.requests[0]["messages"]
    assert all(r["messages"][-1]["role"] == "user" for r in client.requests)
    # The empty reply never entered the conversation.
    assistant = [m for m in agent.messages if m["role"] == "assistant"]
    assert len(assistant) == 2
    assert "toolUse" in assistant[0]["content"][0]


def test_retries_are_bounded_then_the_reply_is_accepted_as_before(caplog):
    scripts = [_thinking_only_tool_stop()] * (MAX_EMPTY_TOOL_TURN_RETRIES + 1)
    agent, client = _agent(scripts, [EmptyToolTurnRetry()])
    with caplog.at_level(logging.WARNING, logger="agents.empty_tool_turn"):
        with pytest.raises(Exception) as caught:
            _run(agent)
    assert PREFILL_ERROR in repr(caught.value) + repr(caught.value.__cause__)
    # One first reply plus the bounded retries, then the old failing request.
    assert len(client.requests) == MAX_EMPTY_TOOL_TURN_RETRIES + 2
    lines = [r.getMessage() for r in caplog.records if EMPTY_TOOL_TURN_LOG_MARKER in r.getMessage()]
    assert len(lines) == MAX_EMPTY_TOOL_TURN_RETRIES + 1
    assert "not retrying" in lines[-1] and "test-model" in lines[-1]
    assert "Deciding" not in " ".join(lines)  # no message content in the log


def test_the_retry_allowance_covers_the_whole_invocation():
    # One retry before the tool call and one after it spend the allowance; an accepted
    # reply in between does not give a fresh one.
    scripts = [_thinking_only_tool_stop(), _tool_call("t-1"),
               _thinking_only_tool_stop(), _answer("Done.")]
    agent, client = _agent(scripts, [EmptyToolTurnRetry()])
    assert "Done." in str(_run(agent))
    assert len(client.requests) == 4

    scripts = [_thinking_only_tool_stop(), _tool_call("t-1"),
               _thinking_only_tool_stop(), _thinking_only_tool_stop()]
    agent, client = _agent(scripts, [EmptyToolTurnRetry()])
    with pytest.raises(Exception) as caught:
        _run(agent)
    assert PREFILL_ERROR in repr(caught.value) + repr(caught.value.__cause__)
    assert len(client.requests) == 5


class _PrefillClient:
    """Stub client for a provider that ACCEPTS a request ending on the assistant, and
    always answers with the empty tool stop."""

    def __init__(self):
        self.requests = []
        self.meta = SimpleNamespace(region_name="us-east-1")

    def converse_stream(self, **request):
        self.requests.append(request["messages"][-1]["role"])
        return {"stream": iter(_thinking_only_tool_stop())}


def _prefill_calls(hooks):
    model = BedrockModel(model_id="test-model", region_name="us-east-1")
    client = _PrefillClient()
    model.client = client
    agent = Agent(model=model, tools=[ask_system], callback_handler=None, hooks=list(hooks))
    result = asyncio.run(agent.invoke_async("How many records?", limits={"turns": 3}))
    assert result.stop_reason == "limit_turns"
    return len(client.requests)


def test_on_a_provider_that_accepts_prefill_the_hook_adds_only_its_allowance():
    # Past the allowance the loop is strands' own: the same model calls as with no hook.
    without = _prefill_calls([])
    assert _prefill_calls([EmptyToolTurnRetry()]) == without + MAX_EMPTY_TOOL_TURN_RETRIES


def test_a_discarded_reply_is_counted_in_usage_once():
    agent, client = _agent([_thinking_only_tool_stop(), _tool_call(), _answer()],
                           [EmptyToolTurnRetry()])
    result = _run(agent)
    assert len(client.requests) == 3
    usage = result.metrics.accumulated_usage
    assert (usage["inputTokens"], usage["outputTokens"], usage["totalTokens"]) == (30, 15, 45)
    assert result.metrics.accumulated_metrics["latencyMs"] == 3


def _empty_event(cancelled=False):
    import threading
    signal = threading.Event()
    if cancelled:
        signal.set()
    agent = SimpleNamespace(_cancel_signal=signal, event_loop_metrics=None,
                            model=SimpleNamespace(config={"model_id": "test-model"}))
    return SimpleNamespace(
        agent=agent, invocation_state={}, retry=False, exception=None,
        stop_response=SimpleNamespace(stop_reason="tool_use", message={"content": []}))


def test_no_retry_after_the_turn_was_cancelled(caplog):
    event = _empty_event(cancelled=True)
    with caplog.at_level(logging.WARNING, logger="agents.empty_tool_turn"):
        EmptyToolTurnRetry()._after_model_call(event)
    assert event.retry is False
    assert any("turn cancelled" in r.getMessage() for r in caplog.records)


def test_no_retry_when_the_callers_window_is_nearly_spent():
    from agents.model_stream_retry import STALL_RETRY_MIN_REMAINING_S, retry_window
    event = _empty_event()
    with retry_window(STALL_RETRY_MIN_REMAINING_S - 10):
        EmptyToolTurnRetry()._after_model_call(event)
    assert event.retry is False

    event = _empty_event()
    with retry_window(STALL_RETRY_MIN_REMAINING_S + 200):
        EmptyToolTurnRetry()._after_model_call(event)
    assert event.retry is True


def test_known_limit_reasoning_of_a_discarded_reply_was_already_streamed():
    # strands streams every event of a reply before the hook can discard it, so a
    # discarded reply's reasoning stays in the stream. Buffering reasoning until the reply
    # is accepted would hold back every turn's reasoning until the model finished it.
    agent, _ = _agent([_thinking_only_tool_stop(), _tool_call(), _answer()],
                      [EmptyToolTurnRetry()])

    async def collect():
        seen = []
        async for event in agent.stream_async("How many records?"):
            if isinstance(event, dict) and isinstance(event.get("reasoningText"), str):
                seen.append(event["reasoningText"])
        return seen

    assert "Deciding which system to ask" in "".join(asyncio.run(collect()))


def test_a_normal_tool_call_and_a_normal_answer_are_not_retried():
    agent, client = _agent([_tool_call(), _answer()], [EmptyToolTurnRetry()])
    _run(agent)
    assert len(client.requests) == 2


def test_known_limit_a_text_reply_that_calls_no_tool_is_not_retried():
    # Its text was already streamed to the user; a retry would show it twice.
    assert not is_empty_tool_turn("tool_use", {"content": [{"text": "Checking now."}]})
    agent, client = _agent([_text_tool_stop(), _answer()], [EmptyToolTurnRetry()])
    with pytest.raises(Exception):
        _run(agent)
    assert len(client.requests) == 2


def test_is_empty_tool_turn_shapes():
    assert is_empty_tool_turn("tool_use", {"content": []})
    assert is_empty_tool_turn("tool_use", {"content": [{"text": "  "}]})
    assert is_empty_tool_turn("tool_use", {"content": [{"reasoningContent": {}}]})
    assert not is_empty_tool_turn("end_turn", {"content": []})
    assert not is_empty_tool_turn("tool_use", {"content": [{"toolUse": {"name": "x"}}]})
    assert not is_empty_tool_turn("tool_use", None)


# --- every agent the app builds registers the hook ---
def test_orchestrator_kwargs_register_the_hook(monkeypatch):
    import agents.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(orchestrator, "_build_orchestrator_prompt", lambda: "p")
    monkeypatch.setattr(orchestrator, "_metamodel_prompt_suffix", lambda: "")
    hooks = orchestrator._base_agent_kwargs([], None)["hooks"]
    assert any(isinstance(h, EmptyToolTurnRetry) for h in hooks)


def test_system_agent_hooks_register_the_hook_last():
    from agents import system_agent
    hooks = system_agent._system_agent_hooks("system-a", "MCP Server", "", [])
    assert isinstance(hooks[-1], EmptyToolTurnRetry)
    assert not isinstance(hooks[0], EmptyToolTurnRetry)


def test_onboard_agent_registers_the_hook(monkeypatch):
    from agents import onboard_agent
    seen = {}

    class _Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(onboard_agent, "Agent", _Agent)
    monkeypatch.setattr(onboard_agent, "_onboard_agent_prompt", lambda: "p")
    monkeypatch.setattr(onboard_agent, "_get_onboard_tools", lambda token: [])
    onboard_agent.get_onboard_agent()
    assert any(isinstance(h, EmptyToolTurnRetry) for h in seen.get("hooks") or ())
