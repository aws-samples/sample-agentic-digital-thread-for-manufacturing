# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator's own output cap: one bounded recovery, then an honest error.

The observed failure (a live account, 2026-10-13): three system agents each
returned a long list; on the next model turn the orchestrator model ran into
its 16,384-token output cap while still writing tool calls. strands 1.42.0
replaced the cut-off tool uses with text, appended that message and raised
``MaxTokensReachedException``; the runtime turned it into
``{"error": "Agent invocation failed"}`` and the user saw the generic
"Query failed. Retry in a moment ..." after a 394 s wait.

These tests drive a fake strands ``Model`` through the REAL path the app
uses: ``runtime_app._run_orchestrator`` (the entrypoint interception, chart
wrapper and keepalive) around the REAL ``orchestrator.build_orchestrator``
(the real Agent kwargs, conversation manager and hooks). They pin:

* the premise: the strands state left behind after the raise;
* a cut while writing tool calls, and a cut while writing the answer, each
  recover with exactly ONE more model call that continues the SAME
  conversation, and the turn answers instead of failing;
* a second cut ends with a fixed, honest "answer too long" public error
  (never the generic one), and still exactly two model calls;
* a cut inside unsigned thinking is sent back without the reasoning block;
* the enforce (integrity) path gets the same recovery;
* one bounded, value-free log line records that recovery fired and how it
  ended; other failures are not retried;
* the chat client maps the runtime's fixed code to its own public category,
  and memory display drops the runtime notice turn.

Offline: no AWS, no network.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import sys
from types import SimpleNamespace

import boto3
import bedrock_agentcore as _REAL_AGENTCORE
import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.types.exceptions import MaxTokensReachedException

import agents.orchestrator as orchestrator
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


# --------------------------------------------------------------------------- #
# A scripted fake model: each stream() call plays the next script
# --------------------------------------------------------------------------- #
def _usage(output_tokens: int) -> dict:
    return {"metadata": {
        "usage": {"inputTokens": 100, "outputTokens": output_tokens,
                  "totalTokens": 100 + output_tokens},
        "metrics": {"latencyMs": 5},
    }}


def _cut_in_tool_use(tool_id: str = "t-cut") -> list[dict]:
    """A reply cut off while it was still writing a tool call's input."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Asking each system now."}}},
        {"contentBlockStop": {}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": tool_id, "name": "ask_system"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {
            "input": '{"question": "list every record with ids '}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "max_tokens"}},
        _usage(16384),
    ]


def _cut_in_text(text: str = "Here is the first part of the answer") -> list[dict]:
    """A reply cut off while it was writing the final answer text."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "max_tokens"}},
        _usage(16384),
    ]


def _cut_in_thinking() -> list[dict]:
    """A reply cut off inside its thinking: no signature, no text, no tool."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {
            "text": "Working through the returned rows one by one"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "max_tokens"}},
        _usage(16384),
    ]


def _answer(text: str = "Final answer.") -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        _usage(200),
    ]


def _signed_thinking(text: str, signature: str) -> list[dict]:
    """A finished thinking block: its text, then its signature."""
    return [
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": text}}}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": signature}}}},
        {"contentBlockStop": {}},
    ]


def _cut_after_signed_thinking() -> list[dict]:
    """The live case: thinking finished (signed), then a tool call was cut."""
    return [
        {"messageStart": {"role": "assistant"}},
        *_signed_thinking("Planning the calls", "sig-cut-1"),
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "t-cut", "name": "ask_system"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {
            "input": '{"question": "list every record with ids '}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "max_tokens"}},
        _usage(16384),
    ]


def _signed_answer(text: str = "Answer.", signature: str = "sig-ok-1") -> list[dict]:
    """A normal turn: signed thinking, then the answer."""
    return [
        {"messageStart": {"role": "assistant"}},
        *_signed_thinking("Thinking it through", signature),
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        _usage(200),
    ]


def _tool_call(tool_id: str, question: str) -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": tool_id, "name": "ask_system"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {
            "input": '{"question": "%s"}' % question}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(50),
    ]


def _structured_answer(tool_id: str = "t-final") -> list[dict]:
    """The enforce path's answer: a call to the structured-output tool."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": tool_id, "name": "FinalAnswerDraft"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(50),
    ]


class _ScriptedModel(Model):
    """Plays one scripted reply per model call and records what it was sent."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[list[dict]] = []

    def update_config(self, **model_config):
        return None

    def get_config(self):
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        if not self.scripts:
            raise AssertionError("model called more times than scripted")
        for event in self.scripts.pop(0):
            yield event


@tool
def ask_system(question: str) -> str:
    """Ask a system a question."""
    return "3 records matched."


# --------------------------------------------------------------------------- #
# 1. The premise: what strands 1.42.0 leaves behind when it raises
# --------------------------------------------------------------------------- #
def _drain(agent, prompt):
    async def run():
        return [e async for e in agent.stream_async(prompt)]
    return asyncio.run(run())


def test_premise_cut_tool_use_is_replaced_and_the_message_is_kept():
    agent = Agent(model=_ScriptedModel([_cut_in_tool_use()]), tools=[ask_system],
                  callback_handler=None)
    with pytest.raises(MaxTokensReachedException):
        _drain(agent, "question")
    last = agent.messages[-1]
    assert last["role"] == "assistant"
    # No toolUse survives, so no tool ran and no toolResult is owed.
    assert not any("toolUse" in block for block in last["content"])
    assert [b["text"] for b in last["content"]][0] == "Asking each system now."
    assert len(last["content"]) == 2  # the cut tool use became a text block


def test_premise_cut_text_is_kept_as_the_last_assistant_message():
    agent = Agent(model=_ScriptedModel([_cut_in_text("partial")]),
                  tools=[ask_system], callback_handler=None)
    with pytest.raises(MaxTokensReachedException):
        _drain(agent, "question")
    last = agent.messages[-1]
    # strands 1.48 also stamps a tracking_id on each message; only the role and
    # the kept text are ours to pin.
    assert (last["role"], last["content"]) == ("assistant", [{"text": "partial"}])
    assert "metadata" in last


def test_premise_cut_thinking_leaves_an_unsigned_reasoning_block():
    agent = Agent(model=_ScriptedModel([_cut_in_thinking()]),
                  tools=[ask_system], callback_handler=None)
    with pytest.raises(MaxTokensReachedException):
        _drain(agent, "question")
    [block] = agent.messages[-1]["content"]
    assert "signature" not in block["reasoningContent"]["reasoningText"]


# --------------------------------------------------------------------------- #
# 2. The real runtime path (default report-only mode)
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


def _fake_config():
    return SimpleNamespace(has_memory=False, has_gateway=False,
                           agentcore_gateway_url=None, memory_id=None,
                           aws_region="us-east-1")


def _wire(monkeypatch, model, *, mode=None):
    """Real build_orchestrator + real runtime entrypoint around *model*."""
    if mode is None:
        monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    else:
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore",
                        SimpleNamespace(BedrockAgentCoreApp=_FakeRuntimeApp))
    monkeypatch.setattr(orchestrator, "build_model", lambda *_a, **_k: model)
    monkeypatch.setattr(orchestrator, "get_config", _fake_config)
    monkeypatch.setattr(orchestrator, "_get_orchestrator_tools",
                        lambda **_k: [ask_system])
    monkeypatch.setattr(orchestrator, "_metamodel_prompt_suffix", lambda: "")
    monkeypatch.setattr(runtime_app, "_graph_first_prelude", lambda p: (p, []))
    for hook in ("_record_rule_usage_hook", "_check_for_discoveries",
                 "_check_for_reason_code_mappings",
                 "_persist_column_not_found_guidance",
                 "_record_empty_join_signal", "_record_metric_term_miss_signal",
                 "_record_context_not_reflected_signal", "_bump_turn_cost_hook",
                 "_capture_correlation_insights",
                 "_capture_value_mapping_proposals",
                 "_integrity_post_turn_learning"):
        monkeypatch.setattr(runtime_app, hook, lambda *_a, **_k: None)
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda _ids: [])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError(f"no AWS client in this test: {a}")))


class _FakeMemoryPlane:
    """An in-memory AgentCore Memory data plane: only the calls the real
    ``AgentCoreMemorySessionManager`` and ``MemoryClient`` make. Events are
    kept oldest first and listed newest first, like the service."""

    def __init__(self):
        self.events: list[dict] = []
        # MemoryClient's constructor logs its clients' Region.
        self.meta = SimpleNamespace(region_name="us-east-1")

    def create_event(self, **params):
        event = {"memoryId": params["memoryId"], "actorId": params["actorId"],
                 "sessionId": params["sessionId"],
                 "payload": copy.deepcopy(params["payload"]),
                 "eventId": f"{len(self.events):013d}#evt",
                 "eventTimestamp": params.get("eventTimestamp")}
        if params.get("metadata"):
            event["metadata"] = copy.deepcopy(params["metadata"])
        self.events.append(event)
        return {"event": event}

    @staticmethod
    def _matches(event, expressions) -> bool:
        """``filter.eventMetadata``: every expression must hold (the SDK's
        session/agent state lookups filter on ``stateType`` and ``agentId``)."""
        metadata = event.get("metadata") or {}
        for expr in expressions:
            key, op = expr["left"]["metadataKey"], expr["operator"]
            if op == "EXISTS" and key not in metadata:
                return False
            if op == "NOT_EXISTS" and key in metadata:
                return False
            if op == "EQUALS_TO" and metadata.get(key) != expr["right"]["metadataValue"]:
                return False
        return True

    def list_events(self, **params):
        expressions = (params.get("filter") or {}).get("eventMetadata") or []
        rows = [e for e in reversed(self.events)
                if e["actorId"] == params["actorId"]
                and e["sessionId"] == params["sessionId"]
                and self._matches(e, expressions)]
        return {"events": copy.deepcopy(rows[:params.get("maxResults", 100)])}

    def retrieve_memory_records(self, **_params):
        return {"memoryRecordSummaries": []}

    def stored_messages(self, actor_id: str = "u-1") -> list[dict]:
        """The persisted strands messages for *actor_id*, oldest first."""
        import json
        out = []
        for event in self.events:
            if event["actorId"] != actor_id:
                continue
            for item in event["payload"]:
                if "conversational" in item:
                    text = item["conversational"]["content"]["text"]
                    out.append(json.loads(text)["message"])
        return out


def _wire_memory(monkeypatch, plane):
    """Interactive memory ON, with the real session manager on *plane*."""
    class _Session:
        region_name = "us-east-1"

        def __init__(self, *_a, **_k):
            pass

        def client(self, *_a, **_k):
            return plane

    # The real package (its memory integration is under test), with only the
    # runtime app class swapped for the capturing fake.
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", _REAL_AGENTCORE)
    monkeypatch.setattr(_REAL_AGENTCORE, "BedrockAgentCoreApp", _FakeRuntimeApp)
    monkeypatch.setattr(boto3, "Session", _Session)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: plane)
    monkeypatch.setattr(orchestrator, "get_config", lambda: SimpleNamespace(
        has_memory=True, has_gateway=False, agentcore_gateway_url=None,
        memory_id="mem-1", aws_region="us-east-1"))


def _unsigned_reasoning_blocks(messages) -> list:
    return [b for m in messages for b in m.get("content") or []
            if isinstance(b, dict) and "reasoningContent" in b
            and not (b["reasoningContent"].get("reasoningText") or {}).get("signature")
            and not b["reasoningContent"].get("redactedContent")]


def _bedrock_formatter():
    """The real Converse request formatter (built before any boto3 patch)."""
    from strands.models.bedrock import BedrockModel
    return BedrockModel(model_id="us.anthropic.claude-sonnet-5", region_name="us-east-1")


def _run(monkeypatch, scripts, *, mode=None, prompt="which records failed?",
         session_id="s-1", plane=None, after_wire=None):
    """One turn through the real runtime. ``plane`` turns interactive memory
    on, backed by an in-memory AgentCore Memory data plane (round 2);
    ``after_wire`` patches more seams once the standard wiring is in place."""
    model = _ScriptedModel(scripts)
    _wire(monkeypatch, model, mode=mode)
    if plane is not None:
        _wire_memory(monkeypatch, plane)
    if after_wire is not None:
        after_wire(monkeypatch)
    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke
    payload = {"prompt": prompt, "sessionId": session_id, "userId": "u-1"}
    if plane is not None:
        payload["writeMemory"] = True

    async def collect():
        return [e async for e in invoke(payload, SimpleNamespace(session_id="rt"))]

    return asyncio.run(collect()), model


def _errors(events):
    return [e["error"] for e in events if isinstance(e, dict) and "error" in e]


def _text(events):
    return "".join(e["data"] for e in events
                   if isinstance(e, dict) and isinstance(e.get("data"), str))


def _recovery():
    from agents import output_cap_recovery
    return output_cap_recovery


def _notice_turn(messages) -> dict:
    last = messages[-1]
    assert last["role"] == "user"
    return last


def test_cut_in_tool_use_recovers_with_one_more_call(monkeypatch):
    events, model = _run(monkeypatch, [
        _cut_in_tool_use(),
        _tool_call("t-ok", "count failed records"),
        _answer("Three records failed."),
    ])
    assert _errors(events) == []
    assert "Three records failed." in _text(events)
    # The cut call, then the retry turn (a tool call + the answer). The retry
    # is ONE extra invocation; its own tool loop is the model's normal work.
    assert len(model.calls) == 3
    retry_sent = model.calls[1]
    # Same conversation: the user question, the cut reply, then the notice.
    assert retry_sent[0]["role"] == "user"
    assert retry_sent[-2]["role"] == "assistant"
    assert not any("toolUse" in b for b in retry_sent[-2]["content"])
    notice = _notice_turn(retry_sent)
    assert notice["content"] == [{"text": _recovery().RECOVERY_NOTICE}]
    assert sum(1 for m in retry_sent if m["role"] == "user") == 2


def test_cut_in_answer_text_continues_the_same_conversation(monkeypatch):
    events, model = _run(monkeypatch, [
        _cut_in_text("The first half of the answer"),
        _answer(" and the rest of it."),
    ])
    assert _errors(events) == []
    assert _text(events) == "The first half of the answer and the rest of it."
    assert len(model.calls) == 2
    # The model sees what it already wrote, so it can continue from there.
    assert model.calls[1][-2]["content"] == [{"text": "The first half of the answer"}]


def test_a_second_cut_ends_with_the_honest_too_long_error(monkeypatch):
    events, model = _run(monkeypatch, [_cut_in_tool_use(), _cut_in_text()])
    assert _errors(events) == [_recovery().OUTPUT_LIMIT_ERROR_CODE]
    assert "Agent invocation failed" not in _errors(events)
    assert len(model.calls) == 2  # bounded: exactly one retry


def test_cut_inside_unsigned_thinking_is_sent_back_without_it(monkeypatch):
    events, model = _run(monkeypatch, [_cut_in_thinking(), _answer()])
    assert _errors(events) == []
    cut_reply = model.calls[1][-2]
    assert cut_reply["role"] == "assistant"
    assert not any("reasoningContent" in b for b in cut_reply["content"])
    # An assistant turn must carry content: the fixed placeholder fills it.
    assert cut_reply["content"] == [{"text": _recovery().CUT_REPLY_PLACEHOLDER}]


def test_no_cut_means_no_retry_and_no_notice(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        events, model = _run(monkeypatch, [_answer("Done.")])
    assert _errors(events) == []
    assert len(model.calls) == 1
    assert "OUTPUT_LIMIT_RECOVERY" not in caplog.text


def test_other_failures_are_not_retried(monkeypatch):
    class _Broken(_ScriptedModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
            self.calls.append(messages)
            raise RuntimeError("boom 4242")
            yield  # pragma: no cover

    model = _Broken([])
    _wire(monkeypatch, model)
    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke

    async def collect():
        return [e async for e in invoke({"prompt": "q", "sessionId": "s"},
                                        SimpleNamespace(session_id="rt"))]

    events = asyncio.run(collect())
    assert _errors(events) == ["Agent invocation failed"]
    assert len(model.calls) == 1


def test_measured_cost_counts_both_attempts(monkeypatch):
    events, _model = _run(monkeypatch, [_cut_in_text(), _answer()])
    [summary] = [e["intelligence_summary"] for e in events
                 if isinstance(e, dict) and "intelligence_summary" in e]
    assert summary["cost"]["output_tokens"] == 16384 + 200


# --------------------------------------------------------------------------- #
# 3. The log line: one per recovery, bounded, no data values
# --------------------------------------------------------------------------- #
def _recovery_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "OUTPUT_LIMIT_RECOVERY" in r.getMessage()]


def test_one_log_line_records_a_recovery_that_answered(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run(monkeypatch, [_cut_in_tool_use(), _answer("All good.")])
    [line] = _recovery_lines(caplog)
    assert "outcome=answered" in line
    assert "cut_tool_calls=1" in line
    assert "session=s-1" in line
    assert "list every record" not in line and "All good" not in line
    assert len(line) < 400


def test_one_log_line_records_a_second_cut(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run(monkeypatch, [_cut_in_text(), _cut_in_text()])
    [line] = _recovery_lines(caplog)
    assert "outcome=cut_again" in line
    assert "first part of the answer" not in line


def test_a_retry_that_fails_otherwise_is_logged_with_its_trace(monkeypatch, caplog):
    class _CutThenBreak(_ScriptedModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
            if self.calls:
                self.calls.append(messages)
                raise RuntimeError("model gone 4242")
            async for event in super().stream(messages, tool_specs, system_prompt, **kw):
                yield event

    model = _CutThenBreak([_cut_in_text()])
    _wire(monkeypatch, model)
    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke

    async def collect():
        return [e async for e in invoke({"prompt": "q", "sessionId": "s-2"},
                                        SimpleNamespace(session_id="rt"))]

    with caplog.at_level(logging.INFO):
        events = asyncio.run(collect())
    assert _errors(events) == ["Agent invocation failed"]
    [line] = _recovery_lines(caplog)
    assert "outcome=error" in line and "type=RuntimeError" in line
    assert "trace=" in line and "Traceback" in line
    assert "4242" not in line  # digits masked
    assert len(model.calls) == 2


# --------------------------------------------------------------------------- #
# 4. The enforce (integrity) path gets the same recovery
# --------------------------------------------------------------------------- #
def test_enforce_path_recovers_from_a_cut(monkeypatch):
    events, model = _run(monkeypatch, [_cut_in_text(), _structured_answer()],
                         mode="enforce")
    assert _errors(events) == []
    # The gated final answer and the integrity verdict both arrive.
    assert "## Answer" in _text(events)
    assert any(isinstance(e, dict) and "response_integrity" in e for e in events)
    assert len(model.calls) == 2
    assert _notice_turn(model.calls[1])["content"] == [
        {"text": _recovery().RECOVERY_NOTICE}]


def test_enforce_path_second_cut_is_the_honest_error(monkeypatch):
    events, model = _run(monkeypatch, [_cut_in_text(), _cut_in_text()],
                         mode="enforce")
    assert _errors(events) == [_recovery().OUTPUT_LIMIT_ERROR_CODE]
    assert len(model.calls) == 2


# --------------------------------------------------------------------------- #
# 5. The notice and the public error text
# --------------------------------------------------------------------------- #
def test_notice_is_generic_and_bounded():
    notice = _recovery().RECOVERY_NOTICE
    assert notice.startswith(_recovery().RECOVERY_NOTICE_PREFIX)
    assert len(notice) <= 700
    lowered = notice.lower()
    for word in ("cut off", "tool call", "continue", "concise"):
        assert word in lowered, word


def test_chat_client_maps_the_code_to_the_output_limit_category():
    from agents.chat_client import classify_error_text
    assert classify_error_text(_recovery().OUTPUT_LIMIT_ERROR_CODE) == "output_limit"
    # The raw framework text keeps its old classification.
    assert classify_error_text("MaxTokensReachedException mid tool-use") == "runtime_error"
    # A data value that merely contains the words never classifies.
    assert classify_error_text("value orchestrator_output_limit_x") == "runtime_error"


def test_output_limit_public_text_is_honest_and_not_the_generic_one():
    from agents.events import ERROR_CATEGORIES, public_error_message
    assert "output_limit" in ERROR_CATEGORIES
    text = public_error_message("output_limit")
    assert text != public_error_message("runtime_error")
    assert "too long" in text
    assert "Retry in a moment" not in text


def test_output_limit_text_trips_no_outage_or_timeout_classifier():
    from pathlib import Path

    from agents.chat_client import _ERROR_TEXT_CLASSIFIERS
    from agents.events import public_error_message
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts import smoke_prompts as smoke

    for text in (public_error_message("output_limit"), _recovery().RECOVERY_NOTICE):
        assert smoke.classify_error_labels([text]) == [], text
        assert smoke.detect_outage_fallback_signals(text) == [], text
        assert [c for c, p in _ERROR_TEXT_CLASSIFIERS
                if c != "output_limit" and p.search(text)] == [], text


def test_runtime_error_event_carries_the_output_limit_category():
    from agents.chat_client import _CloudStreamState
    from agents.events import public_error_message
    state = _CloudStreamState.__new__(_CloudStreamState)
    state.runtime_error_seen = False
    state.session_id = "s-1"
    [event] = state._runtime_error_events({"error": _recovery().OUTPUT_LIMIT_ERROR_CODE})
    assert event.category == "output_limit"
    assert event.message == public_error_message("output_limit")


def test_memory_display_drops_the_notice_turn():
    from agents.memory_labels import label_from_turns, strip_turn_preambles
    notice = _recovery().RECOVERY_NOTICE
    assert strip_turn_preambles(notice) == ""
    turns = [SimpleNamespace(role="user", content=notice, timestamp=1),
             SimpleNamespace(role="user", content="which records failed?",
                             timestamp=2)]
    assert label_from_turns(turns) == "which records failed?"


# --------------------------------------------------------------------------- #
# 6. Seams and size
# --------------------------------------------------------------------------- #
def test_both_runtime_stream_sites_use_the_recovery_seam():
    import inspect
    assert "stream_with_output_cap_recovery" in inspect.getsource(
        runtime_app._filtered_stream)
    assert "stream_with_output_cap_recovery" in inspect.getsource(
        runtime_app._integrity_model_events)
    # The integrity body no longer streams the agent directly.
    assert "agent.stream_async(" not in inspect.getsource(
        runtime_app._invoke_integrity_runtime)


def test_new_functions_stay_under_the_50_line_gate():
    import ast
    import inspect
    module = _recovery()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno + 1 <= 50, node.name
    for func in (runtime_app._filtered_stream, runtime_app._integrity_model_events,
                 runtime_app._invoke_integrity_runtime):
        assert len(inspect.getsource(func).splitlines()) <= 50


# --------------------------------------------------------------------------- #
# 7. Round 2 (HIGH): the stored copy of a cut reply must not poison the next
#    turn. Real AgentCoreMemorySessionManager, real wrapper, in-memory plane.
# --------------------------------------------------------------------------- #
def _two_turns(monkeypatch, first_scripts, *, second_scripts=None,
               first_after_wire=None):
    """Turn 1 then turn 2 of one remembered chat; returns (plane, turn-2 model)."""
    plane = _FakeMemoryPlane()
    _run(monkeypatch, first_scripts, session_id="s-mem", plane=plane,
         after_wire=first_after_wire)
    monkeypatch.undo()
    events, second = _run(monkeypatch, second_scripts or [_answer("Second answer.")],
                          session_id="s-mem", plane=plane,
                          prompt="and which of them are still open?")
    assert _errors(events) == []
    return plane, second


def test_cut_inside_thinking_is_stored_without_the_unsigned_block(monkeypatch):
    plane, _second = _two_turns(monkeypatch, [_cut_in_thinking(), _answer("Recovered.")])
    stored = plane.stored_messages()
    assert _unsigned_reasoning_blocks(stored) == []
    cut_reply = stored[1]
    assert cut_reply["role"] == "assistant"
    assert cut_reply["content"] == [{"text": _recovery().CUT_REPLY_PLACEHOLDER}]


def test_next_turn_after_a_cut_sends_no_unsigned_reasoning_to_bedrock(monkeypatch):
    formatter = _bedrock_formatter()
    _plane, second = _two_turns(monkeypatch, [_cut_in_thinking(), _answer("Recovered.")])
    rehydrated = second.calls[0]
    # The whole remembered conversation came back, then the new question.
    assert [m["role"] for m in rehydrated] == ["user", "assistant", "user",
                                              "assistant", "user"]
    assert _unsigned_reasoning_blocks(rehydrated) == []
    request = formatter._format_bedrock_messages(rehydrated)
    for message in request:
        for block in message["content"]:
            if "reasoningContent" in block:
                assert block["reasoningContent"]["reasoningText"].get("signature")
    roles = [m["role"] for m in request]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles


def test_signed_thinking_in_a_cut_reply_is_kept_byte_for_byte(monkeypatch):
    plane, second = _two_turns(monkeypatch,
                               [_cut_after_signed_thinking(), _answer("Recovered.")])
    signed = {"reasoningContent": {"reasoningText": {
        "text": "Planning the calls", "signature": "sig-cut-1"}}}
    stored_cut = plane.stored_messages()[1]["content"]
    assert stored_cut[0] == signed
    assert second.calls[0][1]["content"][0] == signed


def test_retry_request_keeps_signed_thinking_and_matches_what_is_stored(monkeypatch):
    plane = _FakeMemoryPlane()
    _events, model = _run(monkeypatch, [_cut_after_signed_thinking(), _answer("Done.")],
                          session_id="s-mem", plane=plane)
    live_cut = model.calls[1][-2]
    assert live_cut["content"][0]["reasoningContent"]["reasoningText"]["signature"] \
        == "sig-cut-1"
    # One rule for both copies: the retry saw what the next turn rehydrates.
    assert live_cut["content"] == plane.stored_messages()[1]["content"]


def test_a_normal_signed_turn_is_stored_exactly_as_produced(monkeypatch):
    plane, second = _two_turns(monkeypatch, [_signed_answer("Answer.", "sig-ok-1")])
    stored_reply = plane.stored_messages()[1]
    assert stored_reply["content"] == [
        {"reasoningContent": {"reasoningText": {
            "text": "Thinking it through", "signature": "sig-ok-1"}}},
        {"text": "Answer."}]
    assert second.calls[0][1]["content"] == stored_reply["content"]


def test_a_session_already_poisoned_on_main_heals_on_rehydration(monkeypatch):
    """Turn 1 stored WITHOUT the write-side clean-up (what main stored after
    a cut inside thinking); turn 2 must still send Bedrock a valid request."""
    def raw_storage(mp):
        mp.setattr(orchestrator, "_wrap_session_manager", lambda sm: sm)

    plane, second = _two_turns(monkeypatch, [_cut_in_thinking(), _answer("Recovered.")],
                               first_after_wire=raw_storage)
    assert _unsigned_reasoning_blocks(plane.stored_messages()) != []  # poisoned
    assert _unsigned_reasoning_blocks(second.calls[0]) == []


def test_storage_clean_up_returns_untouched_messages_unchanged():
    from strands.types.session import SessionMessage
    signed = SessionMessage.from_message({"role": "assistant", "content": [
        {"reasoningContent": {"reasoningText": {"text": "t", "signature": "s"}}},
        {"text": ""},
        {"text": "a"}]}, 0)
    assert orchestrator._persistable_session_message(signed) is signed
    redacted = SessionMessage.from_message({"role": "assistant", "content": [
        {"reasoningContent": {"redactedContent": b"opaque"}}, {"text": "a"}]}, 1)
    assert orchestrator._persistable_session_message(redacted) is redacted


def test_storage_clean_up_drops_only_unsigned_blocks_and_empties():
    from strands.types.session import SessionMessage
    original = {"role": "assistant", "content": [
        {"reasoningContent": {"reasoningText": {"text": "cut"}}},
        {"reasoningContent": {"reasoningText": {"text": "t", "signature": "s"}}},
        {"text": ""},
        {"text": "kept"}]}
    message = SessionMessage.from_message(copy.deepcopy(original), 0)
    cleaned = orchestrator._persistable_session_message(message)
    assert cleaned is not message
    assert cleaned.message["content"] == [
        {"reasoningContent": {"reasoningText": {"text": "t", "signature": "s"}}},
        {"text": "kept"}]
    assert message.message == original  # the live copy is never touched


# --------------------------------------------------------------------------- #
# 8. Round 2 (MED): a cut tool call that never ran must not count anywhere
# --------------------------------------------------------------------------- #
def _summary(events) -> dict:
    [summary] = [e["intelligence_summary"] for e in events
                 if isinstance(e, dict) and "intelligence_summary" in e]
    return summary


def _capture_learning_inputs(sink: dict):
    """Record what the post-turn learning seams receive (both paths)."""
    def patch(mp):
        mp.setattr(runtime_app, "_check_for_discoveries",
                   lambda calls, inputs, results, *_a, **_k: sink.update(
                       calls=list(calls), inputs=dict(inputs), results=dict(results)))
        mp.setattr(runtime_app, "_integrity_post_turn_learning",
                   lambda state, *_a, **_k: sink.update(
                       names=dict(state.tool_names), calls=state.tool_calls,
                       inputs=dict(state.tool_query_inputs),
                       metadata=dict(state.tool_metadata)))
    return patch


def test_cut_tool_call_is_not_counted_when_the_retry_answers(monkeypatch):
    sink: dict = {}
    events, _model = _run(monkeypatch, [_cut_in_tool_use(), _answer("Done.")],
                          after_wire=_capture_learning_inputs(sink))
    summary = _summary(events)
    assert summary["total_tool_calls"] == 0
    assert summary["system_queries"] == 0
    assert sink["calls"] == []
    assert "t-cut" not in sink["inputs"] and "t-cut" not in sink["results"]


def test_only_tool_calls_with_results_count_after_recovery(monkeypatch):
    sink: dict = {}
    events, _model = _run(monkeypatch, [
        _cut_in_tool_use(), _tool_call("t-ok", "count failed records"),
        _answer("Three records failed.")], after_wire=_capture_learning_inputs(sink))
    summary = _summary(events)
    assert summary["total_tool_calls"] == 1
    assert summary["system_queries"] == 1
    assert [c["toolUseId"] for c in sink["calls"]] == ["t-ok"]


def test_enforce_path_does_not_count_the_cut_tool_call(monkeypatch):
    sink: dict = {}
    events, _model = _run(monkeypatch, [_cut_in_tool_use(), _structured_answer()],
                          mode="enforce", after_wire=_capture_learning_inputs(sink))
    summary = _summary(events)
    assert summary["total_tool_calls"] == 0
    assert summary["system_queries"] == 0
    assert sink["names"] == {} and sink["calls"] == 0
    assert "t-cut" not in sink["inputs"] and "t-cut" not in sink["metadata"]


def test_enforce_path_counts_only_the_retry_tool_call(monkeypatch):
    sink: dict = {}
    events, _model = _run(monkeypatch, [
        _cut_in_tool_use(), _tool_call("t-ok", "count failed records"),
        _structured_answer()], mode="enforce",
        after_wire=_capture_learning_inputs(sink))
    summary = _summary(events)
    assert summary["total_tool_calls"] == 1
    assert sink["names"] == {"t-ok": "ask_system"} and sink["calls"] == 1


def test_cut_tool_call_cannot_satisfy_a_golden_systems_min(monkeypatch):
    from agents import eval_core
    events, _model = _run(monkeypatch, [_cut_in_tool_use(), _answer("Three records failed.")])
    verdict, failures = eval_core.score_question(
        {"systems_min": 1}, _summary(events), _text(events), _errors(events),
        question_text="which records failed?")
    assert verdict == "fail"
    assert "system_queries 0 < 1" in failures


def test_recovery_hands_the_exact_cut_ids_to_the_caller():
    model = _ScriptedModel([_tool_call("t-ran", "a"), _cut_in_tool_use("t-cut"),
                            _answer("ok")])
    agent = Agent(model=model, tools=[ask_system], callback_handler=None)
    seen: list = []

    async def run():
        async for _event in _recovery().stream_with_output_cap_recovery(
                agent, "q", session_id="s", on_cut=seen.append):
            pass

    asyncio.run(run())
    assert seen == [frozenset({"t-cut"})]


def test_a_second_cut_also_drops_its_own_unsent_tool_calls(monkeypatch):
    events, _model = _run(monkeypatch, [_cut_in_tool_use("t-a"), _cut_in_tool_use("t-b")])
    assert _errors(events) == [_recovery().OUTPUT_LIMIT_ERROR_CODE]
    summary = _summary(events)
    assert summary["total_tool_calls"] == 0
    assert summary["system_queries"] == 0


def test_round_two_functions_stay_under_the_50_line_gate():
    import inspect
    for func in (orchestrator._persistable_session_message,
                 orchestrator._wrap_session_manager,
                 orchestrator._wrap_list_messages,
                 runtime_app._forget_cut_tool_calls,
                 runtime_app._RuntimeIntegrityInvocation.forget_tool_calls):
        assert len(inspect.getsource(func).splitlines()) <= 50
