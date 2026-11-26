# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent's answer that states a record identifier no result returned,
in the form of the schema's example values.

The fixture (``tests/fixtures/system_agent_unreturned_ids_sku.json``) is one
ERP call as the model saw it: four queries over shipment lines whose results
carry each product's row id and name but no SKU, and an answer that states a
SKU built in the form of the schema's example SKUs. The orchestrator relayed
that SKU as the product's identifier.

The answer is returned unchanged and the orchestrator gets a fixed engine line
naming the identifier no result returned; there is no extra model turn. The
agent is a real system agent built by ``create_system_agent`` from the
fixture's schema entries; only its model turns are scripted, so the evidence
the code reads is what the agent's own capture hook records.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents.response_integrity_capture import initialize_integrity_state  # noqa: E402

_FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                       / "system_agent_unreturned_ids_sku.json").read_text())
QUESTION = _FIXTURE["question"]
ANSWER = _FIXTURE["answer"]
MADE_UP = _FIXTURE["made_up"]
CALLS = _FIXTURE["tool_calls"]
MARKER = "[engine] Identifiers in no result:"


def _agent(tables_desc: str = _FIXTURE["tables_desc"]):
    return sa.create_system_agent(
        system_id="erp", system_name="ERP", schema="erp_data", system_prompt="",
        tables_desc=tables_desc,
        conn_params={"host": "localhost", "port": 5432, "dbname": "erp",
                     "user": "reader", "password": "test-only"},  # nosec B106
        connection_type="PostgreSQL",
    )


def _tool_turns(agent, state: dict, calls: list) -> None:
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
    for k, call in enumerate(calls, start=1):
        use = {"toolUseId": f"tooluse_{k}", "name": "query_erp_data",
               "input": {"sql": call["sql"]}}
        agent.messages.append({"role": "assistant", "content": [{"toolUse": use}]})
        agent.hooks.invoke_callbacks(BeforeToolCallEvent(
            agent=agent, selected_tool=None, tool_use=use, invocation_state=state,
            cancel_tool=False))
        result = {"toolUseId": use["toolUseId"], "status": call.get("status", "success"),
                  "content": [{"text": call["result"]}]}
        agent.hooks.invoke_callbacks(AfterToolCallEvent(
            agent=agent, selected_tool=None, tool_use=use, invocation_state=state,
            result=result, exception=None))
        agent.messages.append({"role": "user", "content": [{"toolResult": result}]})


class _Script:
    """One scripted model invocation: run ``calls``, then answer ``answer``."""

    def __init__(self, calls=None, answer=ANSWER):
        self.calls = CALLS if calls is None else calls
        self.answer = answer
        self.prompts: list = []

    def __call__(self, agent, prompt=None, *, invocation_state=None, limits=None, **kwargs):
        from strands.hooks import BeforeInvocationEvent
        self.prompts.append(prompt)
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(
            agent=agent, invocation_state=invocation_state, messages=None))
        agent.messages.append({"role": "user", "content": [{"text": prompt}]})
        _tool_turns(agent, invocation_state, self.calls)
        agent.messages.append({"role": "assistant", "content": [{"text": self.answer}]})
        return SimpleNamespace(message={"role": "assistant",
                                        "content": [{"text": self.answer}]},
                               stop_reason="end_turn")


def _use(monkeypatch, script) -> None:
    from strands import Agent
    monkeypatch.setattr(Agent, "__call__", lambda self, *a, **k: script(self, *a, **k))
    # The executed-filters line (its own tests) appends to every answer with a WHERE.
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")


def _run(monkeypatch, script, question=QUESTION, mode="report-only", agent=None):
    _use(monkeypatch, script)
    state: dict = {}
    initialize_integrity_state(state, question)
    answer = sa._legacy_mode_a_answer(agent or _agent(), question, mode, state)
    return answer, sa._append_engine_facts(answer, state)[len(answer):]


def _sku_result(sku: str) -> dict:
    sql = "SELECT sku, product_name FROM products WHERE product_name = 'P'"
    return {"sql": sql, "result": json.dumps({
        "columns": ["sku", "product_name"], "rows": [[sku, "P"]], "row_count": 1,
        "_adt_executed_query": sql})}


def test_the_fixture_is_the_failure():
    """The identifier is in the answer, in no result, not in the question and
    not a schema example; the schema's examples for that column share its
    leading segment."""
    results = " ".join(call["result"] for call in CALLS)
    assert MADE_UP in ANSWER
    assert MADE_UP not in results and MADE_UP not in QUESTION
    assert MADE_UP not in _FIXTURE["tables_desc"]
    lead = MADE_UP.split("-")[0]
    for example in _FIXTURE["sku_examples"]:
        assert example in _FIXTURE["tables_desc"] and example.startswith(lead + "-")


def test_the_answer_is_returned_unchanged_with_the_engine_line(monkeypatch):
    script = _Script()
    answer, tail = _run(monkeypatch, script)
    assert len(script.prompts) == 1, "a second model turn ran"
    assert answer == ANSWER, "the answer was replaced"
    assert MARKER in tail
    assert f'"{MADE_UP}"' in tail


def test_the_line_names_only_what_no_result_returned(monkeypatch):
    """Every other identifier in the answer (batches, customer codes) is in
    a result, so the line quotes the made-up one alone."""
    _answer, tail = _run(monkeypatch, _Script())
    assert MARKER in tail
    line = tail.split(MARKER, 1)[1]
    assert re.findall(r'"([^"]+)"', line) == [MADE_UP]


def test_an_identifier_a_result_returned_is_left_alone(monkeypatch):
    answer, tail = _run(monkeypatch, _Script(calls=[*CALLS, _sku_result(MADE_UP)]))
    assert answer == ANSWER
    assert MARKER not in tail


def test_an_identifier_only_an_earlier_call_returned_is_still_reported(monkeypatch):
    """The system agent is a long-lived singleton: an earlier, unrelated call
    on it is not this call's evidence."""
    agent = _agent()
    _run(monkeypatch, _Script(calls=[_sku_result(MADE_UP)], answer=f"The SKU is {MADE_UP}."),
         question="Which SKU is product P?", agent=agent)
    _answer, tail = _run(monkeypatch, _Script(), agent=agent)
    assert f'"{MADE_UP}"' in tail


def test_an_identifier_the_question_gave_is_left_alone(monkeypatch):
    question = f"{QUESTION}\nThe product is {MADE_UP}."
    _answer, tail = _run(monkeypatch, _Script(), question=question)
    assert MARKER not in tail


def test_an_answer_without_the_identifier_is_byte_identical(monkeypatch):
    plain = ANSWER.replace(f" (SKU: {MADE_UP})", "")
    answer, tail = _run(monkeypatch, _Script(answer=plain))
    assert answer == plain
    assert tail == ""


def test_annotate_returns_the_answer_with_the_engine_line(monkeypatch):
    answer, tail = _run(monkeypatch, _Script(), mode="annotate")
    assert MADE_UP in answer
    assert f'"{MADE_UP}"' in tail


def test_the_query_entry_point_returns_the_answer_and_the_engine_line(monkeypatch):
    """The deployed default (report-only) through the runtime's own entry point."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    script = _Script()
    _use(monkeypatch, script)
    answer = asyncio.run(sa._run_mode_a_query(_agent(), "erp", QUESTION))
    assert len(script.prompts) == 1
    assert answer.startswith(ANSWER), "the answer was replaced"
    tail = answer[len(ANSWER):]
    assert MARKER in tail and f'"{MADE_UP}"' in tail
