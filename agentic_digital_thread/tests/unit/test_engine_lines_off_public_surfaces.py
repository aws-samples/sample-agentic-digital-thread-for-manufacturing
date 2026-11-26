# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Orchestrator-only engine lines reach the orchestrator and no surface a person reads.

A system agent's short answer that ends with the identifiers line
(``answer_ids``) is, as text, also the tool activity summary (the local
hook's ``tool_result`` event and the runtime stream's ``ToolResultEvent``
summary and preview), the stored investigation trace, and, when the
orchestrator copies the line, part of the public answer (the web stream and
the external answer the Quick bridge relays). Each surface must drop the line
while the orchestrator's copy keeps it. The answer is short on purpose: a
long one can hide the line behind the summary's 2,000-character cap.

The system agent is a real one built from the recorded call's schema
(``tests/fixtures/system_agent_unreturned_ids_sku.json``); only its model
turns are scripted. The names line (``answer_names``) is covered the same way.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents import answer_ids, answer_names  # noqa: E402

_FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                       / "system_agent_unreturned_ids_sku.json").read_text())
QUESTION = _FIXTURE["question"]
MADE_UP = _FIXTURE["made_up"]
CALLS = _FIXTURE["tool_calls"]
#: A short answer: one sentence naming the record's id and the made-up SKU.
SHORT = f"Batch RUN-20261022-90001 shipped product Precision Shaft 7742 (SKU: {MADE_UP})."
MARKER = answer_ids.MARKER


def _agent():
    return sa.create_system_agent(
        system_id="erp", system_name="ERP", schema="erp_data", system_prompt="",
        tables_desc=_FIXTURE["tables_desc"],
        conn_params={"host": "localhost", "port": 5432, "dbname": "erp",
                     "user": "reader", "password": "test-only"},  # nosec B106
        connection_type="PostgreSQL",
    )


def _script(answer: str):
    from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent

    def call(agent, prompt=None, *, invocation_state=None, limits=None, **kwargs):
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(
            agent=agent, invocation_state=invocation_state, messages=None))
        for k, step in enumerate(CALLS, start=1):
            use = {"toolUseId": f"t{k}", "name": "query_erp_data", "input": {"sql": step["sql"]}}
            agent.hooks.invoke_callbacks(BeforeToolCallEvent(
                agent=agent, selected_tool=None, tool_use=use,
                invocation_state=invocation_state, cancel_tool=False))
            result = {"toolUseId": use["toolUseId"], "status": "success",
                      "content": [{"text": step["result"]}]}
            agent.hooks.invoke_callbacks(AfterToolCallEvent(
                agent=agent, selected_tool=None, tool_use=use,
                invocation_state=invocation_state, result=result, exception=None))
        return SimpleNamespace(message={"role": "assistant", "content": [{"text": answer}]},
                               stop_reason="end_turn")
    return call


@pytest.fixture
def delegated(monkeypatch):
    """What the ERP system agent returns for the short answer, through the
    runtime's own entry point, with its investigation trace captured."""
    from strands import Agent
    from agents import investigation_trace_store
    from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(Agent, "__call__", lambda self, *a, **k: _script(SHORT)(self, *a, **k))
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")
    stored: list = []
    monkeypatch.setattr(investigation_trace_store, "put_trace",
                        lambda session, ref, entry, **_kw: stored.append(entry) or True)
    token = provenance_context_var.set(("session-1", "query-1"))
    wtoken = provenance_write_token_var.set("0123456789abcdef0123456789abcdef")
    try:
        answer = asyncio.run(sa._run_mode_a_query(_agent(), "erp", QUESTION))
    finally:
        provenance_context_var.reset(token)
        provenance_write_token_var.reset(wtoken)
    return answer, stored


def test_the_orchestrator_receives_the_line(delegated):
    answer, _stored = delegated
    assert answer.startswith(SHORT)
    assert MARKER in answer and f'"{MADE_UP}"' in answer
    assert len(answer) < 2_000, "the answer must be short enough for the summary to show whole"


def test_the_local_tool_activity_summary_drops_the_line(delegated):
    from agents.chat_client import _ToolResultHook
    answer, _stored = delegated
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    event = SimpleNamespace(
        tool_use={"name": "erp___query_erp", "toolUseId": "tool-1", "input": {"question": "q"}},
        result={"toolUseId": "tool-1", "status": "success", "content": [{"text": answer}]})
    hook._on_tool_done(event)
    emitted = []
    while not q.empty():
        emitted.append(q.get_nowait())
    results = [item for item in emitted
               if isinstance(item, dict) and item.get("event") == "tool_result"]
    assert results, emitted
    summary = results[-1]["data"]["summary"]
    assert SHORT in summary
    assert MARKER not in summary and "Do not state" not in summary


def test_the_runtime_stream_summary_and_preview_drop_the_line(delegated):
    from agents.chat_client import _tool_result_from_payload
    answer, _stored = delegated
    event = _tool_result_from_payload({"toolUseId": "tool-1", "content": [{"text": answer}]})
    assert SHORT in event.summary and SHORT in (event.tool_result or "")
    for text in (event.summary, event.tool_result or ""):
        assert MARKER not in text and "Do not state" not in text


def test_the_stored_investigation_trace_drops_the_line(delegated):
    """The entry keeps every other line of the answer, other engine lines included."""
    answer, stored = delegated
    assert len(stored) == 1
    final = stored[0]["final_answer"]
    start = answer.index(MARKER)
    end = answer.find("\n", start)
    line = answer[start:] if end < 0 else answer[start:end]
    assert MARKER not in final
    assert final == answer.replace(f"\n\n{line}", "", 1), final
    assert final.startswith(SHORT)
    assert MARKER in answer, "the orchestrator's copy lost the line"


def _orchestrator_answer(answer: str) -> str:
    """An orchestrator answer that copies the system agent's engine line."""
    line = answer[answer.index(MARKER):]
    return f"The batch shipped to three customers.\n\n{line}\n\nTotal value: $10,200.00."


def test_the_external_answer_drops_a_copied_line(delegated):
    from agents.public_answer import external_answer_text
    answer, _stored = delegated
    public = external_answer_text(_orchestrator_answer(answer))
    assert public == "The batch shipped to three customers.\n\nTotal value: $10,200.00."


@pytest.mark.parametrize("size", [1, 3, 17, 4096])
def test_the_streamed_web_answer_drops_a_copied_line(delegated, size):
    """Streamed in chunks of any size, split anywhere in the line."""
    from agents.public_answer import InternalTelemetryFilter
    answer, _stored = delegated
    text = _orchestrator_answer(answer)
    filter_ = InternalTelemetryFilter()
    out = "".join(filter_.feed(text[i:i + size]) for i in range(0, len(text), size))
    out += filter_.flush()
    assert MARKER not in out and "Do not state" not in out
    assert out.startswith("The batch shipped to three customers.\n\n")
    assert out.rstrip().endswith("Total value: $10,200.00.")


def test_the_done_event_drops_a_copied_line(delegated):
    from agents.events import DoneEvent
    from agents.public_answer import InternalTelemetryFilter
    from agents.server import _public_answer_events
    answer, _stored = delegated
    events = _public_answer_events(DoneEvent(_orchestrator_answer(answer)),
                                   InternalTelemetryFilter())
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done and MARKER not in done[0].text


def test_the_names_line_is_covered_the_same_way(monkeypatch):
    """The names line on the same surfaces: the activity summary, the trace
    entry and the external answer."""
    from agents import investigation_trace_store
    from agents.public_answer import external_answer_text
    from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
    from agents.tool_result_summary import summarize_tool_result
    line = answer_names.engine_line(["Harbor Tile Works"])
    text = f"Two kiln lots shipped.\n\n{line}"
    summary = summarize_tool_result("erp___query_erp", {"content": [{"text": text}]})
    assert summary == "Two kiln lots shipped."
    stored: list = []
    monkeypatch.setattr(investigation_trace_store, "put_trace",
                        lambda session, ref, entry, **_kw: stored.append(entry) or True)
    token = provenance_context_var.set(("session-1", "query-1"))
    wtoken = provenance_write_token_var.set("0123456789abcdef0123456789abcdef")
    try:
        sa._write_investigation_trace({}, "erp", "q", text)
    finally:
        provenance_context_var.reset(token)
        provenance_write_token_var.reset(wtoken)
    assert stored and stored[0]["final_answer"] == "Two kiln lots shipped."
    assert external_answer_text(f"{text}\n\nDone.") == "Two kiln lots shipped.\n\nDone."
