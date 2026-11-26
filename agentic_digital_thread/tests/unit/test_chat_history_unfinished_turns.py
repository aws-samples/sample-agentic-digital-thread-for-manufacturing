# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chat history a turn reads back leaves out attempts the user never saw.

Seen on a test event (2026-10-15): a turn the app gave up on in the middle of
a system call was stored up to its last tool call. "Ask again" re-sent the
question into the same chat session; the runtime loaded that history, strands
added a "Tool was interrupted." result, and the model answered from the failed
attempt's tool results without calling a system again, so the answer had no
Sources. When the server finished a turn the browser never showed, the retry
read the whole answer and said it had "just answered" the question.

Pins:

* the pure read rule: a turn starts at a user message with text; it finished
  when its last message is an assistant reply with text and no tool call, or a
  successful result of the structured-answer tool (the enforce path's final
  answer); every unfinished turn is dropped wherever it sits; a leading
  fragment that does not start with a question is left alone; tool calls and
  results stay paired; a history with a finished turn is never emptied;
* the retry rule: with the re-asked question set, the trailing turns whose
  opening question EXACTLY equals it (preambles peeled, whitespace collapsed,
  raw or as stored with PII redacted) are dropped as well, never a longer or
  different question;
* the real read path: AgentCore session manager + orchestrator wrapper over
  an in-memory memory plane, through the real runtime entrypoint. The rule
  runs ONLY on a retry turn; an ordinary turn reads the history as listed, so
  a turn the user stopped after seeing output stays in context;
* a retry turn does not read the same-session summaries, which can carry the
  attempt the history read leaves out;
* the RESERVED ``retryUndeliveredTurn`` payload key is popped at the runtime
  seam and never reaches the prompt or the model.

Offline: no AWS, no network. Fixtures are domain-neutral.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orchestrator
import agents.runtime_app as runtime_app
from tests.unit.test_orchestrator_output_cap_recovery import (  # noqa: F401 - harness
    _FakeMemoryPlane,
    _FakeRuntimeApp,
    _answer,
    _errors,
    _tool_call,
    _wire,
    _wire_memory,
)


def _history_turns():
    from agents import history_turns
    return history_turns


# --------------------------------------------------------------------------- #
# Message builders (strands message shapes, domain-neutral content)
# --------------------------------------------------------------------------- #
def _user(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def _reply(text: str) -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def _call(tool_id: str, name: str = "plant1___query_records") -> dict:
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}]}


def _says_and_calls(tool_id: str) -> dict:
    return {"role": "assistant", "content": [
        {"text": "Checking the records system."},
        {"toolUse": {"toolUseId": tool_id, "name": "plant1___query_records", "input": {}}}]}


def _result(tool_id: str, status: str = "success", text: str = "3 rows") -> dict:
    return {"role": "user", "content": [
        {"toolResult": {"toolUseId": tool_id, "status": status,
                        "content": [{"text": text}]}}]}


def _interrupted(tool_id: str) -> dict:
    """What strands appends before the next prompt to a history ending in a call."""
    return _result(tool_id, status="error", text="Tool was interrupted.")


def _structured_call(tool_id: str = "t-final") -> dict:
    return _call(tool_id, name="FinalAnswerDraft")


def _structured_ok(tool_id: str = "t-final") -> dict:
    return _result(tool_id, text="Successfully validated FinalAnswerDraft structured output")


def _filter(messages, **kwargs):
    return _history_turns().without_unfinished_turns(messages, **kwargs)


def _assert_paired(messages) -> None:
    """Every toolUse is answered by the NEXT message, and every toolResult
    answers a toolUse in the message right before it (what Bedrock requires)."""
    for index, message in enumerate(messages):
        uses = [b["toolUse"]["toolUseId"] for b in message["content"] if "toolUse" in b]
        if uses:
            assert index + 1 < len(messages), f"call {uses} left without a result"
            nxt = messages[index + 1]
            answered = {b["toolResult"]["toolUseId"] for b in nxt["content"]
                        if "toolResult" in b}
            assert set(uses) <= answered, f"call {uses} left without a result"
        results = [b["toolResult"]["toolUseId"] for b in message["content"]
                   if "toolResult" in b]
        if results and index > 0:
            prev = messages[index - 1]
            asked = {b["toolUse"]["toolUseId"] for b in prev["content"] if "toolUse" in b}
            assert set(results) <= asked, f"result {results} without its call"


# --------------------------------------------------------------------------- #
# 1. The pure read rule
# --------------------------------------------------------------------------- #
def test_a_turn_cut_after_a_tool_call_is_dropped():
    history = [_user("How many records failed?"), _reply("Three."),
               _user("Which of them are still open?"), _call("t1"), _result("t1"),
               _call("t2")]
    assert _filter(history) == history[:2]


def test_a_finished_turn_with_tool_calls_is_kept_as_is():
    history = [_user("How many records failed?"), _call("t1"), _result("t1"),
               _says_and_calls("t2"), _result("t2"), _reply("Three of them.")]
    kept = _filter(history)
    assert kept == history
    assert all(a is b for a, b in zip(kept, history))  # the same objects


def test_an_unfinished_turn_in_the_middle_is_dropped_too():
    history = [_user("First question?"), _call("t1"), _result("t1"), _call("t2"),
               _interrupted("t2"),
               _user("Second question?"), _reply("Second answer."),
               _user("Third question?"), _call("t3"),
               _user("Fourth question?"), _reply("Fourth answer.")]
    assert _filter(history) == [_user("Second question?"), _reply("Second answer."),
                                _user("Fourth question?"), _reply("Fourth answer.")]


def test_a_turn_cut_before_any_reply_is_dropped():
    history = [_user("First question?"), _reply("First answer."), _user("Second question?")]
    assert _filter(history) == history[:2]


def test_a_reply_that_still_calls_a_tool_is_not_an_answer():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _says_and_calls("t1")]
    assert _filter(history) == history[:2]


def test_a_leading_fragment_without_a_question_is_left_alone():
    # The window offset can start the listed history mid-turn.
    history = [_result("t0"), _reply("Earlier answer."),
               _user("Next question?"), _call("t1")]
    assert _filter(history) == history[:2]


def test_the_structured_answer_counts_as_finished():
    history = [_user("How many records failed?"), _call("t1"), _result("t1"),
               _structured_call(), _structured_ok()]
    assert _filter(history) == history


def test_a_failed_structured_answer_is_not_finished():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _structured_call(),
               _result("t-final", status="error", text="validation failed")]
    assert _filter(history) == history[:2]


def test_a_result_of_an_ordinary_tool_is_not_finished():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _call("t1"), _result("t1")]
    assert _filter(history) == history[:2]


def test_the_output_limit_notice_continues_the_same_turn():
    from agents.output_cap_recovery import RECOVERY_NOTICE
    cut = [_user("Long question?"), _reply("Here is the first part"),
           _user(RECOVERY_NOTICE), _call("t1")]
    assert _filter(cut) == []
    answered = [_user("Long question?"), _reply("Here is the first part"),
                _user(RECOVERY_NOTICE), _reply("and the rest.")]
    assert _filter(answered) == answered


def test_a_history_with_a_finished_turn_is_never_emptied():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _call("t1"), _result("t1"), _call("t2")]
    assert _filter(history) != []


@pytest.mark.parametrize("history", [
    [_user("A?"), _call("t1"), _result("t1"), _call("t2")],
    [_user("A?"), _call("t1"), _interrupted("t1"), _user("B?"), _reply("b")],
    [_user("A?"), _reply("a"), _user("B?"), _call("t1"), _result("t1"), _call("t2"),
     _interrupted("t2"), _user("C?"), _call("t3"), _result("t3"), _reply("c")],
    [_result("t0"), _reply("x"), _user("A?"), _says_and_calls("t1"), _result("t1"),
     _reply("a"), _user("B?"), _says_and_calls("t2")],
    [_user("A?"), _call("t1"), _result("t1"), _structured_call(), _structured_ok(),
     _user("B?"), _call("t2")],
])
def test_tool_calls_and_results_stay_paired(history):
    _assert_paired(_filter(history))


def test_empty_and_malformed_items_do_not_raise():
    assert _filter([]) == []
    odd = [None, {"role": "user"}, {"role": "assistant", "content": "text"}]
    assert isinstance(_filter(odd), list)


def test_session_message_objects_are_filtered_by_their_message():
    from strands.types.session import SessionMessage
    items = [SessionMessage.from_message(m, i) for i, m in enumerate(
        [_user("First question?"), _reply("First answer."),
         _user("Second question?"), _call("t1")])]
    kept = _filter(items)
    assert kept == items[:2]
    assert kept[0] is items[0]


def test_the_structured_answer_tool_name_matches_the_integrity_model():
    from agents.response_integrity import FinalAnswerDraft
    assert _history_turns().STRUCTURED_ANSWER_TOOLS == frozenset({FinalAnswerDraft.__name__})


# --------------------------------------------------------------------------- #
# 2. The retry rule
# --------------------------------------------------------------------------- #
_STORED = "[Temporal context: now is 2026-01-01 10:00 UTC] {q}"


def test_retry_drops_the_finished_attempt_the_user_never_saw():
    history = [_user(_STORED.format(q="First question?")), _reply("First answer."),
               _user(_STORED.format(q="Second question?")), _call("t1"), _result("t1"),
               _reply("Second answer.")]
    assert _filter(history, retry_question="Second question?") == history[:2]


def test_retry_drops_every_trailing_attempt_at_the_same_question():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _reply("Second answer."),
               _user("Second question?"), _call("t1")]
    assert _filter(history, retry_question="Second question?") == history[:2]


def test_retry_keeps_the_answer_before_an_unfinished_attempt():
    history = [_user("First question?"), _reply("First answer."),
               _user("Second question?"), _call("t1"), _result("t1")]
    assert _filter(history, retry_question="Second question?") == history[:2]


def test_retry_never_drops_a_turn_that_asked_something_else():
    # The failed attempt was never stored (it failed before the runtime ran).
    history = [_user("First question?"), _reply("First answer.")]
    assert _filter(history, retry_question="Second question?") == history


def test_retry_matches_a_question_stored_with_pii_redacted():
    from agents.pii import redact_pii
    question = "Which orders did ops@example.com reopen?"
    stored = redact_pii(_STORED.format(q=question))
    assert question not in stored  # the stored copy really is redacted
    history = [_user("First question?"), _reply("First answer."),
               _user(stored), _reply("Two orders.")]
    assert _filter(history, retry_question=question) == history[:2]


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_retry_question_drops_nothing_extra(blank):
    history = [_user("First question?"), _reply("First answer.")]
    assert _filter(history, retry_question=blank) == history


# The retried attempt is identified by an EXACT opening question
# (whitespace-normalised, preambles peeled), never by containment.
def test_retry_never_drops_a_longer_question_that_starts_with_it():
    # The retried attempt never reached the runtime; the visible turn before it
    # asked a longer question that merely starts with the retried one.
    history = [_user("Which orders failed and which customers did they hit?"),
               _reply("Two orders, one customer.")]
    assert _filter(history, retry_question="Which orders failed") == history


def test_retry_never_drops_a_redacted_question_that_only_contains_it():
    from agents.pii import redact_pii
    stored = redact_pii(_STORED.format(q="Which orders did ops@example.com reopen and close?"))
    history = [_user("First question?"), _reply("First answer."),
               _user(stored), _reply("Two orders.")]
    assert _filter(history, retry_question="Which orders did ops@example.com reopen") == history


def test_retry_keeps_an_earlier_question_that_redacts_the_same_when_it_is_not_trailing():
    from agents.pii import redact_pii
    earlier = redact_pii(_STORED.format(q="Which orders did ops@example.com reopen?"))
    history = [_user(earlier), _reply("Two orders."),
               _user("Something else?"), _reply("Something else answered.")]
    retry = "Which orders did sales@example.com reopen?"  # redacts to the same text
    assert redact_pii(retry) in redact_pii(earlier)
    assert _filter(history, retry_question=retry) == history


def test_retry_matches_after_whitespace_normalisation():
    history = [_user("First question?"), _reply("First answer."),
               _user(_STORED.format(q="Second   question?\n")), _reply("Second answer.")]
    assert _filter(history, retry_question=" Second question? ") == history[:2]


def test_retry_matches_the_question_inside_a_composed_turn():
    composed = ("[ROUTE SEED] graph routing notes\n\n---\n\n"
                + _STORED.format(q="Second question?"))
    history = [_user("First question?"), _reply("First answer."),
               _user(composed), _reply("Second answer.")]
    assert _filter(history, retry_question="Second question?") == history[:2]


# --------------------------------------------------------------------------- #
# 3. The real read path: session manager + wrapper, through the runtime
# --------------------------------------------------------------------------- #
def _store(monkeypatch, plane, messages, session_id: str = "s-mem") -> None:
    """Write *messages* the way the runtime stores them during a turn."""
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager)
    from strands.types.session import SessionMessage
    _wire_memory(monkeypatch, plane)
    manager = AgentCoreMemorySessionManager(AgentCoreMemoryConfig(
        memory_id="mem-1", session_id=session_id, actor_id="u-1"), region_name="us-east-1")
    for message in messages:
        manager.create_message(session_id, "default", SessionMessage.from_message(message, 0))
    monkeypatch.undo()


def _turn(monkeypatch, plane, scripts, prompt: str, *, extra: dict | None = None,
          session_id: str = "s-mem"):
    """One remembered turn through the REAL runtime entrypoint (``selected``)."""
    from tests.unit.test_orchestrator_output_cap_recovery import _ScriptedModel
    model = _ScriptedModel(scripts)
    _wire(monkeypatch, model)
    _wire_memory(monkeypatch, plane)
    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke
    payload = {"prompt": prompt, "rawPrompt": prompt, "sessionId": session_id,
               "userId": "u-1", "writeMemory": True, **(extra or {})}

    async def collect():
        return [e async for e in invoke(payload, SimpleNamespace(session_id="rt"))]

    events = asyncio.run(collect())
    monkeypatch.undo()
    return events, model, payload


def _texts(messages) -> list[str]:
    return [b.get("text") or json.dumps(b, sort_keys=True)
            for m in messages for b in m["content"]]


def test_a_retry_leaves_a_cut_attempt_out_of_the_history(monkeypatch):
    plane = _FakeMemoryPlane()
    _turn(monkeypatch, plane, [_tool_call("t-1", "count"), _answer("First answer.")],
          "First question?")
    # The second turn was cut while the next system call ran: stored up to it.
    _store(monkeypatch, plane, [_user("Second question?"), _call("t-2"),
                                _result("t-2"), _call("t-3")])
    events, model, _payload = _turn(monkeypatch, plane, [_answer("Second answer.")],
                                    "Second question?", extra={"retryUndeliveredTurn": True})
    assert _errors(events) == []
    sent = model.calls[0]
    assert _texts(sent)[0] == "First question?"
    assert "First answer." in _texts(sent)
    assert sent[-1] == _user("Second question?")
    flat = json.dumps(sent[:-1])
    assert "Second question?" not in flat
    assert "t-2" not in flat and "t-3" not in flat
    assert "Tool was interrupted." not in flat
    _assert_paired(sent)


def test_an_ordinary_follow_up_keeps_a_turn_the_user_stopped(monkeypatch):
    """The user saw partial text and tool progress,
    pressed Stop, then asked a follow-up. Without a retry the history is read
    exactly as before: that turn stays (strands closes its open call)."""
    plane = _FakeMemoryPlane()
    _turn(monkeypatch, plane, [_answer("First answer.")], "First question?")
    _store(monkeypatch, plane, [_user("Second question?"), _says_and_calls("t-2"),
                                _result("t-2"), _call("t-3")])
    events, model, _payload = _turn(monkeypatch, plane, [_answer("Third answer.")],
                                    "And the open ones?")
    assert _errors(events) == []
    flat = json.dumps(model.calls[0])
    assert "Second question?" in flat
    assert "Checking the records system." in flat
    assert "Tool was interrupted." in flat  # strands' own handling, as on main
    _assert_paired(model.calls[0])


def test_retry_leaves_out_the_finished_attempt_the_user_never_saw(monkeypatch):
    plane = _FakeMemoryPlane()
    _turn(monkeypatch, plane, [_answer("First answer.")], "First question?")
    _turn(monkeypatch, plane, [_tool_call("t-1", "count"), _answer("Undelivered answer.")],
          "Second question?")
    events, model, payload = _turn(
        monkeypatch, plane, [_tool_call("t-9", "count"), _answer("Fresh answer.")],
        "Second question?", extra={"retryUndeliveredTurn": True})
    assert _errors(events) == []
    sent = model.calls[0]
    assert _texts(sent) == ["First question?", "First answer.", "Second question?"]
    assert "Undelivered answer." not in json.dumps(model.calls)
    # The reserved key never reached the model, and was taken off the payload.
    assert "retryUndeliveredTurn" not in json.dumps(model.calls)
    assert "retryUndeliveredTurn" not in payload


def test_an_ordinary_follow_up_still_reads_the_whole_finished_history(monkeypatch):
    plane = _FakeMemoryPlane()
    _turn(monkeypatch, plane, [_answer("First answer.")], "First question?")
    _turn(monkeypatch, plane, [_answer("Second answer.")], "Second question?")
    _events, model, _payload = _turn(monkeypatch, plane, [_answer("Third answer.")],
                                     "Second question?")
    assert _texts(model.calls[0]) == ["First question?", "First answer.",
                                      "Second question?", "Second answer.",
                                      "Second question?"]


# --------------------------------------------------------------------------- #
# 4. The reserved payload key at the runtime seam
# --------------------------------------------------------------------------- #
def _run_seam(monkeypatch, payload: dict) -> dict:
    """Drive ``_runtime_entrypoint(...).selected`` with a capturing legacy body."""
    seen: dict = {}

    async def legacy(inner_payload, context):
        seen["payload"] = dict(inner_payload)
        try:
            seen["question"] = _history_turns().retried_question()
        except ImportError:  # before the fix: no history rule exists at all
            seen["question"] = "<no history rule>"
        yield {"data": "ok"}

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())

    async def run():
        return [e async for e in selected(payload, None)]

    asyncio.run(run())
    return seen


def test_the_retry_key_is_popped_and_handed_to_the_history_read(monkeypatch):
    seen = _run_seam(monkeypatch, {"prompt": "[ctx] Second question?",
                                   "rawPrompt": "Second question?",
                                   "retryUndeliveredTurn": True})
    assert "retryUndeliveredTurn" not in seen["payload"]
    assert seen["question"] == "Second question?"
    assert "retryUndeliveredTurn" not in json.dumps(seen["payload"])


@pytest.mark.parametrize("value", [False, None, "true", 1, {"x": 1}])
def test_anything_but_true_is_popped_and_ignored(monkeypatch, value):
    seen = _run_seam(monkeypatch, {"prompt": "q", "rawPrompt": "q",
                                   "retryUndeliveredTurn": value})
    assert "retryUndeliveredTurn" not in seen["payload"]
    assert seen["question"] is None


def test_an_ordinary_turn_sets_no_retried_question(monkeypatch):
    seen = _run_seam(monkeypatch, {"prompt": "q", "rawPrompt": "q"})
    assert seen["question"] is None


def test_the_retry_falls_back_to_the_prompt_without_a_raw_question(monkeypatch):
    seen = _run_seam(monkeypatch, {"prompt": "Second question?",
                                   "retryUndeliveredTurn": True})
    assert seen["question"] == "Second question?"


def test_the_payload_key_has_one_spelling():
    assert _history_turns().RETRY_PAYLOAD_KEY == "retryUndeliveredTurn"


def test_the_read_wrapper_logs_counts_only(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    history = [_user("Private question about ops@example.com?"), _call("t1")]
    _filter(history)
    lines = [r.getMessage() for r in caplog.records if "CHAT_HISTORY_TURNS" in r.getMessage()]
    assert len(lines) == 1
    assert "dropped_unfinished=1" in lines[0]
    assert "example.com" not in lines[0] and "Private" not in lines[0]


def test_orchestrator_read_wrapper_applies_the_rule_on_a_retry():
    from strands.types.session import SessionMessage
    stored = [SessionMessage.from_message(m, i) for i, m in enumerate(
        [_user("First question?"), _reply("First answer."),
         _user("Second question?"), _call("t1")])]
    manager = SimpleNamespace(list_messages=lambda *a, **k: list(stored))
    orchestrator._wrap_list_messages(manager)
    token = _history_turns().RETRY_QUESTION_VAR.set("Second question?")
    try:
        assert manager.list_messages("s", "default") == stored[:2]
    finally:
        _history_turns().RETRY_QUESTION_VAR.reset(token)


def test_without_a_retry_the_read_wrapper_returns_the_history_as_main_does():
    """The unfinished-turn rule runs ONLY on a retry."""
    from strands.types.session import SessionMessage
    stored = [SessionMessage.from_message(m, i) for i, m in enumerate(
        [_user("First question?"), _reply("First answer."),
         _user("Second question?"), _call("t1"), _result("t1"), _call("t2")])]
    manager = SimpleNamespace(list_messages=lambda *a, **k: list(stored))
    orchestrator._wrap_list_messages(manager)
    read = manager.list_messages("s", "default")
    assert read == stored
    assert all(a is b for a, b in zip(read, stored))


# --------------------------------------------------------------------------- #
# 5. A retry does not read same-session summaries
# --------------------------------------------------------------------------- #
_HIDDEN_SUMMARY = "Summary of the hidden attempt: the records system reported three."


class _SummaryPlane(_FakeMemoryPlane):
    """The memory plane, plus a session summary of the attempt the user never saw."""

    def __init__(self):
        super().__init__()
        self.namespaces: list[str] = []

    def retrieve_memory_records(self, **params):
        # bedrock-agentcore 1.9.1+ retrieves by path prefix (namespacePath).
        namespace = params.get("namespace") or params.get("namespacePath", "")
        self.namespaces.append(namespace)
        if namespace.startswith("/summaries/"):
            return {"memoryRecordSummaries": [{"content": {"text": _HIDDEN_SUMMARY}}]}
        return {"memoryRecordSummaries": []}


def _summary_plane_after_hidden_attempt(monkeypatch) -> "_SummaryPlane":
    plane = _SummaryPlane()
    _turn(monkeypatch, plane, [_answer("First answer.")], "First question?")
    _turn(monkeypatch, plane, [_answer("Undelivered answer.")], "Second question?")
    plane.namespaces.clear()
    return plane


def test_a_retry_never_reads_the_summary_of_the_hidden_attempt(monkeypatch):
    plane = _summary_plane_after_hidden_attempt(monkeypatch)
    events, model, _payload = _turn(monkeypatch, plane, [_answer("Fresh answer.")],
                                    "Second question?", extra={"retryUndeliveredTurn": True})
    assert _errors(events) == []
    assert _HIDDEN_SUMMARY not in json.dumps(model.calls)
    assert not [n for n in plane.namespaces if n.startswith("/summaries/")]


def test_an_ordinary_turn_still_reads_session_summaries(monkeypatch):
    # Control: proves the fake summary does reach the prompt when it is read.
    plane = _summary_plane_after_hidden_attempt(monkeypatch)
    _events, model, _payload = _turn(monkeypatch, plane, [_answer("Third answer.")],
                                     "Third question?")
    assert _HIDDEN_SUMMARY in json.dumps(model.calls[0])
    assert [n for n in plane.namespaces if n.startswith("/summaries/")]


def test_the_retry_retrieval_config_drops_only_the_same_session_namespace():
    from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig
    ordinary = orchestrator._turn_retrieval_config(RetrievalConfig, "s-1")
    token = _history_turns().RETRY_QUESTION_VAR.set("Second question?")
    try:
        retry = orchestrator._turn_retrieval_config(RetrievalConfig, "s-1")
    finally:
        _history_turns().RETRY_QUESTION_VAR.reset(token)
    assert ordinary == orchestrator._ltm_retrieval_config(RetrievalConfig, "s-1")
    assert set(retry) == {ns for ns in ordinary if "{sessionId}" not in ns}
