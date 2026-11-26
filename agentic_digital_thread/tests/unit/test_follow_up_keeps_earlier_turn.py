# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A follow-up question keeps the previous turn's results in the model's window.

Walk 2026-10-19 (Team 2, follow-up after a containment answer): the answer
reused the earlier answer, made one graph call, cited a bare "[5]" copied
from the earlier answer, and showed NO Sources row. On another run of the
same question (Team 3) the answer cited "[E2] ... earlier in this chat".

Cause: the orchestrator's sliding window (20 messages, trimmed before every
model call) trims to the first plain user question after the overflow
point. When the previous turn alone fills the window (its question is the
first message), adding the follow-up question pushes it one message over, and
the only plain question left to trim to is the NEW one: every earlier tool
result and the earlier answer leave the model's context before it reads the
follow-up. ``CitationOrdinalHook`` had already labelled those results
``[E<k>]`` and the runtime had already sent the labels to the client, but the
model never saw them, so it answered from the long-term-memory summary text
(which carries the earlier answer's own citation numbers) and the client had
no ``[E<k>]`` citation to show.

The window now trims at turn boundaries: it keeps the previous turn and the
current one from their questions while that stays within a hard cap
(``max_kept_messages``, twice the window), and only past the cap falls back
to the stock trim. Its stored state keeps the stock class name, because
strands refuses to restore a state whose name differs and existing chats
would otherwise fail on their next turn. Messages, tool names and questions
here are synthetic and domain-neutral; the unit tests run under two unrelated
naming schemes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from strands.agent.conversation_manager import SlidingWindowConversationManager

import agents.orchestrator as orch
from agents import citation_ordinals as co
from agents.conversation_window import TurnKeepingSlidingWindow
from agents.output_cap_recovery import RECOVERY_NOTICE
from tests.unit.test_orchestrator_output_cap_recovery import (
    _FakeMemoryPlane, _answer, _errors, _run, _tool_call,
)


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
#: Two unrelated naming schemes: the window rule may not depend on either.
_SCHEMES = [
    {"tool": "plant_x___query_plant_x", "q": "question {n}", "a": "answer {n}"},
    {"tool": "vendor_ledger_7___lookup", "q": "Wie steht es um Los {n}?", "a": "Los {n}: ok"},
]


def _question(text):
    return {"role": "user", "content": [{"text": text}]}


def _pair(tool, tool_id):
    return [{"role": "assistant",
             "content": [{"toolUse": {"toolUseId": tool_id, "name": tool, "input": {}}}]},
            {"role": "user",
             "content": [{"toolResult": {"toolUseId": tool_id, "status": "success",
                                         "content": [{"text": f"rows for {tool_id}"}]}}]}]


def _turn(scheme, n, cycles, *, answered=True):
    msgs = [_question(scheme["q"].format(n=n))]
    for k in range(cycles):
        msgs += _pair(scheme["tool"], f"t{n}-{k}")
    if answered:
        msgs.append({"role": "assistant", "content": [{"text": scheme["a"].format(n=n)}]})
    return msgs


def _apply(manager, messages):
    agent = SimpleNamespace(messages=messages)
    manager.apply_management(agent)
    return agent.messages


def _result_ids(messages):
    return [b["toolResult"]["toolUseId"] for m in messages for b in m["content"]
            if isinstance(b, dict) and "toolResult" in b]


def _starts_with_question(messages):
    first = messages[0]
    return first["role"] == "user" and all("toolResult" not in b for b in first["content"])


# --------------------------------------------------------------------------- #
# 1. The premise: the stock window drops the whole previous turn
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scheme", _SCHEMES)
def test_premise_the_stock_window_drops_a_full_previous_turn(scheme):
    stock = SlidingWindowConversationManager(window_size=20, should_truncate_results=False,
                                             per_turn=True)
    history = _turn(scheme, 1, 9) + [_question(scheme["q"].format(n=2))]
    assert len(history) == 21
    kept = _apply(stock, history)
    assert kept == [_question(scheme["q"].format(n=2))]


# --------------------------------------------------------------------------- #
# 2. The turn-keeping window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("cycles", [8, 9, 12, 17])
def test_a_follow_up_keeps_the_previous_turn_from_its_question(scheme, cycles):
    window = TurnKeepingSlidingWindow(window_size=20)
    previous = _turn(scheme, 1, cycles)
    follow_up = [_question(scheme["q"].format(n=2))] + _pair(scheme["tool"], "now-0")
    kept = _apply(window, previous + follow_up)
    assert kept == previous + follow_up
    assert window.removed_message_count == 0


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_an_older_turn_goes_first_and_the_count_matches_what_was_removed(scheme):
    window = TurnKeepingSlidingWindow(window_size=20)
    oldest, previous = _turn(scheme, 1, 6), _turn(scheme, 2, 9)
    current = [_question(scheme["q"].format(n=3))]
    kept = _apply(window, oldest + previous + current)
    assert kept == previous + current
    # The session manager restores with this offset: it must equal exactly
    # the number of messages dropped from the front.
    assert window.removed_message_count == len(oldest)


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_a_long_turn_keeps_its_own_question_within_the_cap(scheme):
    """Mid-turn: the stock window trimmed a long turn's own question away
    and the history then began with an assistant tool call. Within the cap
    the turn now stays whole, starting at its question."""
    window = TurnKeepingSlidingWindow(window_size=20)
    turn = _turn(scheme, 1, 14, answered=False)
    kept = _apply(window, turn)
    assert kept == turn
    assert _starts_with_question(kept)


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_past_the_cap_the_window_stays_bounded(scheme):
    window = TurnKeepingSlidingWindow(window_size=20)
    assert window.max_kept_messages == 40
    previous = _turn(scheme, 1, 15)                       # 32 messages
    current = _turn(scheme, 2, 6, answered=False)          # 13 messages
    kept = _apply(window, previous + current)
    # The previous turn no longer fits beside this one: the current turn is
    # kept from its question.
    assert kept == current
    huge = _turn(scheme, 3, 30, answered=False)
    kept = _apply(window, list(huge))
    assert len(kept) <= window.max_kept_messages


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_the_output_limit_notice_is_not_a_turn_start(scheme):
    """The runtime's output-cap notice continues the turn it belongs to, so
    it is never the boundary the window keeps from."""
    window = TurnKeepingSlidingWindow(window_size=20)
    previous = _turn(scheme, 1, 9)
    current = ([_question(scheme["q"].format(n=2))] + _pair(scheme["tool"], "c-0")
               + [{"role": "assistant", "content": [{"text": "partial"}]},
                  _question(RECOVERY_NOTICE)])
    kept = _apply(window, previous + current)
    assert kept == previous + current


def test_a_context_overflow_still_uses_the_stock_reduction():
    """A reactive overflow (the model said the context is too long) must
    reduce: the turn-keeping rule never applies there."""
    window = TurnKeepingSlidingWindow(window_size=20)
    scheme = _SCHEMES[0]
    history = _turn(scheme, 1, 9) + [_question("next")]
    agent = SimpleNamespace(messages=list(history))
    window.reduce_context(agent, e=RuntimeError("context overflow"))
    assert len(agent.messages) < len(history)


def test_the_orchestrator_builds_the_turn_keeping_window():
    manager = orch._make_conversation_manager()
    assert isinstance(manager, TurnKeepingSlidingWindow)
    assert (manager.window_size, manager.should_truncate_results, manager.per_turn) == (
        20, False, True)


# --------------------------------------------------------------------------- #
# 3. The real runtime with the real AgentCore Memory session manager
# --------------------------------------------------------------------------- #
def _summary(events):
    return next(e["intelligence_summary"] for e in events
                if isinstance(e, dict) and "intelligence_summary" in e)


def _labels_seen(messages):
    """``{toolUseId: "E<k>"}`` for every earlier stamp the model was sent."""
    out = {}
    for message in messages:
        for block in message["content"]:
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not result:
                continue
            for part in result["content"]:
                text = part.get("text", "") if isinstance(part, dict) else ""
                if text.startswith(co.EARLIER_STAMP_PREFIX):
                    out[result["toolUseId"]] = text[len(co.EARLIER_STAMP_PREFIX):].strip("[].")
    return out


@pytest.mark.parametrize("cycles", [8, 9, 12])
def test_a_follow_up_after_a_long_turn_is_answered_with_the_earlier_results(
        monkeypatch, cycles):
    plane = _FakeMemoryPlane()
    first, _ = _run(monkeypatch,
                    [_tool_call(f"t-{k}", f"part {k}") for k in range(cycles)]
                    + [_answer("Found it [1].")],
                    session_id="s-follow", plane=plane)
    assert _errors(first) == []
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_tool_call("n-1", "one more look"),
                                       _answer("Still open [E2].")],
                         session_id="s-follow", plane=plane, prompt="which are open?")
    assert _errors(second) == []
    sent = _summary(second)["earlier_sources"]
    assert sent, "the client is told about earlier results"
    for call in model.calls:
        seen = _labels_seen(call)
        # Every model call of the follow-up, including the one that writes
        # the answer, still sees the earlier results it was told it may cite.
        assert sorted(seen.values()) == sorted(e["label"] for e in sent), call
        assert all(m["role"] in ("user", "assistant") for m in call)
        assert _starts_with_question(call)


def _stock_manager():
    return SlidingWindowConversationManager(window_size=20, should_truncate_results=False,
                                            per_turn=True)


@pytest.mark.parametrize("first_manager,second_manager", [
    (_stock_manager, None),     # a chat stored before this window shipped
    (None, _stock_manager),     # this window's chat read by an image without it
])
def test_a_stored_chat_restores_across_the_window_change(monkeypatch, first_manager,
                                                         second_manager):
    """strands refuses to restore a stored conversation-manager state whose
    class name differs, which would fail every later turn of an existing chat.
    The window writes and accepts the stock name."""
    plane = _FakeMemoryPlane()

    def patch(factory):
        if factory is not None:
            return lambda mp: mp.setattr(orch, "_make_conversation_manager", factory)
        return None

    first, _ = _run(monkeypatch, [_tool_call("t-1", "part 1"), _answer("Found it [1].")],
                    session_id="s-swap", plane=plane, after_wire=patch(first_manager))
    assert _errors(first) == []
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_answer("Still open [E1].")],
                         session_id="s-swap", plane=plane, prompt="which are open?",
                         after_wire=patch(second_manager))
    assert _errors(second) == []
    assert list(_labels_seen(model.calls[0]).values()) == ["E1"]


def test_the_stored_state_carries_the_stock_name():
    window = TurnKeepingSlidingWindow(window_size=20)
    window.removed_message_count = 7
    state = window.get_state()
    assert state["__name__"] == "SlidingWindowConversationManager"
    restored = TurnKeepingSlidingWindow(window_size=20)
    restored.restore_from_session(state)
    assert restored.removed_message_count == 7
    stock = _stock_manager()
    stock.restore_from_session(state)
    assert stock.removed_message_count == 7


# --------------------------------------------------------------------------- #
# 4. A chat the stock window already trimmed
# --------------------------------------------------------------------------- #
def _fragment(scheme, n, cycles, *, answered=True):
    """What a session the stock window trimmed restores to: the turn without
    its question, starting at an assistant tool call and its result."""
    return _turn(scheme, n, cycles, answered=answered)[1:]


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_a_leading_tool_call_fragment_is_kept_as_the_previous_turn(scheme):
    window = TurnKeepingSlidingWindow(window_size=20)
    window.removed_message_count = 1
    previous = _fragment(scheme, 1, 10)                    # 21 messages, no question
    current = [_question(scheme["q"].format(n=2))] + _pair(scheme["tool"], "now-0")
    kept = _apply(window, previous + current)
    assert kept == previous + current
    # Nothing was cut, so the stored offset is exactly what it was.
    assert window.removed_message_count == 1


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_a_leading_fragment_past_the_cap_takes_the_current_turn(scheme):
    window = TurnKeepingSlidingWindow(window_size=20)
    window.removed_message_count = 3
    previous = _fragment(scheme, 1, 19)                    # 39 messages
    current = [_question(scheme["q"].format(n=2))] + _pair(scheme["tool"], "now-0")
    assert len(previous + current) > window.max_kept_messages
    kept = _apply(window, previous + current)
    assert kept == current
    assert window.removed_message_count == 3 + len(previous)


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_a_leading_fragment_that_is_not_a_tool_pair_is_not_a_turn(scheme):
    """Only the shape the stock window leaves behind (an assistant tool call
    whose result follows) counts as a restored previous turn."""
    window = TurnKeepingSlidingWindow(window_size=20)
    lone_answer = [{"role": "assistant", "content": [{"text": "an old answer"}]}]
    previous = lone_answer + _fragment(scheme, 1, 10)[1:]  # starts with a toolResult
    current = [_question(scheme["q"].format(n=2))] + _pair(scheme["tool"], "now-0")
    stock = _stock_manager()
    expected = _apply(stock, list(previous + current))
    assert _apply(window, previous + current) == expected


def _persisted_removed_count(plane) -> int:
    """The newest conversation-manager offset the session manager stored."""
    import json
    counts = []
    for event in plane.events:
        for item in event["payload"]:
            if "blob" not in item:
                continue
            data = json.loads(item["blob"])
            state = data.get("conversation_manager_state") if isinstance(data, dict) else None
            if isinstance(state, dict):
                counts.append(state["removed_message_count"])
    assert counts, "no stored agent state"
    return counts[-1]


@pytest.mark.parametrize("cycles", [10, 12])
def test_a_chat_the_stock_window_trimmed_keeps_its_earlier_results(monkeypatch, cycles):
    """A chat stored by the stock window restores past its
    question (nonzero offset), starting at a tool call and its result. The
    follow-up still sees every earlier result it is told it may cite."""
    plane = _FakeMemoryPlane()
    stock_wire = lambda mp: mp.setattr(orch, "_make_conversation_manager", _stock_manager)  # noqa: E731
    first, _ = _run(monkeypatch,
                    [_tool_call(f"t-{k}", f"part {k}") for k in range(cycles)]
                    + [_answer("Found it [1].")],
                    session_id="s-trimmed", plane=plane, after_wire=stock_wire)
    assert _errors(first) == []
    stored_offset = _persisted_removed_count(plane)
    assert stored_offset > 0, "the premise: the stock window trimmed the stored turn"
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_tool_call("n-1", "one more look"),
                                       _answer("Still open [E2].")],
                         session_id="s-trimmed", plane=plane, prompt="which are open?")
    assert _errors(second) == []
    sent = _summary(second)["earlier_sources"]
    assert sent
    assert len(model.calls) == 2
    for call in model.calls:
        assert sorted(_labels_seen(call).values()) == sorted(e["label"] for e in sent), call
    # The window cut nothing more, so the stored offset is the restored one.
    assert _persisted_removed_count(plane) == stored_offset
