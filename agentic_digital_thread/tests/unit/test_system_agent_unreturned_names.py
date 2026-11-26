# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent's answer that names records with the schema's example values.

Live run on a rehearsal account (2026-10-18, Quick ask exact-6): a lookup was
refused, and the ERP system agent's call returned the asked batch's three
shipment rows with customer row ids and line values but no customer name. Its
answer named a customer for each row: the first two names were the first two
example values its baked schema shows for ``customers.company_name`` (next to
the first two example ``customer_id`` values, which are two of the three row
ids), and the third was in no result either. No tool result in the call held
any customer name; the orchestrator relayed the names as data and gave one
customer's line to another's total.

The answer is returned unchanged and the orchestrator gets a fixed engine
line naming what no result returned; there is no extra model turn.

The fixture is that call as the model saw it (``tests/fixtures/
system_agent_unreturned_names_exact6.json``), and every record name these
tests use comes from it. The agent is a real system agent built by
``create_system_agent`` from the fixture's customers schema entry. Only its
model turns are scripted: every invocation and tool call fires the agent's
own registered hooks (budget, capture, chart redaction), so the evidence the
code reads is what the capture hook really records.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents.response_integrity_capture import initialize_integrity_state  # noqa: E402

_FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                       / "system_agent_unreturned_names_exact6.json").read_text())
QUESTION = _FIXTURE["question"]
ANSWER = _FIXTURE["answer"]
COPIED = tuple(_FIXTURE["copied"])
MADE_UP = _FIXTURE["made_up"]
#: Names the lookup returns that the first answer did not write.
LOOKED_UP = tuple(name for name in _FIXTURE["looked_up"] if name not in COPIED)
#: The first answer with its customer list rebuilt from the lookup's names.
NAMED_FROM_LOOKUP = _FIXTURE["corrected_answer"]
BATCH = _FIXTURE["batch"]
LOOKUP = _FIXTURE["lookup"]
LOOKUP_ROWS = json.loads(LOOKUP["result"])["rows"]
MARKER = "[engine] Names in no result:"


def _agent(tables_desc: str = _FIXTURE["customers_entry"]):
    return sa.create_system_agent(
        system_id="erp", system_name="ERP", schema="erp_data", system_prompt="",
        tables_desc=tables_desc,
        conn_params={"host": "localhost", "port": 5432, "dbname": "erp",
                     "user": "reader", "password": "test-only"},  # nosec B106
        connection_type="PostgreSQL",
    )


def _tool_turns(agent, state: dict, calls: list, start: int) -> None:
    """Each call as the model and the agent's hooks see it."""
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
    for k, call in enumerate(calls, start=start):
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


def _reply(agent, text: str):
    agent.messages.append({"role": "assistant", "content": [{"text": text}]})
    return SimpleNamespace(message={"role": "assistant", "content": [{"text": text}]},
                           stop_reason="end_turn")


def _looks_up(agent, prompt, state):
    """What a second model turn could do if one ran: look the names up and
    rewrite the answer with them."""
    _tool_turns(agent, state, [LOOKUP], 100)
    return _reply(agent, NAMED_FROM_LOOKUP)


class _Script:
    """Scripted model turns: the first invocation of a call runs ``first_calls``
    and answers ``first_answer``; any later one runs ``later`` (by default it
    gives the same answer again). Each invocation first fires the agent's
    BeforeInvocationEvent hooks, which reset the captured evidence as they do
    live."""

    def __init__(self, first_calls=None, first_answer=ANSWER, later=None):
        self.prompts: list = []
        self.limits: list = []
        self.later = later
        self.first_calls = _FIXTURE["tool_calls"] if first_calls is None else first_calls
        self.first_answer = first_answer

    def __call__(self, agent, prompt=None, *, invocation_state=None, limits=None, **kwargs):
        from strands.hooks import BeforeInvocationEvent
        self.prompts.append(prompt)
        self.limits.append(limits)
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(
            agent=agent, invocation_state=invocation_state, messages=None))
        agent.messages.append({"role": "user", "content": [{"text": prompt}]})
        if len(self.prompts) == 1:
            _tool_turns(agent, invocation_state, self.first_calls, 1)
            return _reply(agent, self.first_answer)
        if self.later is None:
            return _reply(agent, self.first_answer)
        return self.later(agent, prompt, invocation_state)


def _use(monkeypatch, script) -> None:
    from strands import Agent
    monkeypatch.setattr(Agent, "__call__", lambda self, *a, **k: script(self, *a, **k))


def _run(monkeypatch, script, mode="report-only", agent=None, question=QUESTION):
    _use(monkeypatch, script)
    # These tests are about the unreturned-names line; the executed-filters
    # line (its own tests) would append to every fixture answer with a WHERE.
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")
    agent = agent or _agent()
    state: dict = {}
    initialize_integrity_state(state, question)
    answer = sa._legacy_mode_a_answer(agent, question, mode, state)
    return answer, state


def _tail(answer: str, state: dict) -> str:
    return sa._append_engine_facts(answer, state)[len(answer):]


def _query_result(sql: str, columns: list, rows: list) -> dict:
    """A tabular result as the query tool returns it, with its executed query."""
    return {"sql": sql, "result": json.dumps({
        "columns": columns, "rows": rows, "row_count": len(rows), "_adt_executed_query": sql})}


def _raw_result(sql: str, payload) -> dict:
    """A result of another shape (an API or MCP adapter), as the tool returns it."""
    return {"sql": sql, "result": json.dumps(payload)}


def test_the_fixture_is_the_failure():
    """None of the three names is in any tool result or in the question."""
    results = " ".join(call["result"] for call in _FIXTURE["tool_calls"])
    for name in (*COPIED, MADE_UP):
        assert name in ANSWER
        assert name not in results and name not in QUESTION
    for name in COPIED:
        assert name in _FIXTURE["customers_entry"]
    assert MADE_UP not in _FIXTURE["customers_entry"]


# --------------------------------------------------------------------------- #
# The answer is returned unchanged, with the engine line
# --------------------------------------------------------------------------- #

def test_the_answer_is_returned_unchanged_with_the_engine_line(monkeypatch):
    """The exact-6 answer goes to the orchestrator as written, in one model
    invocation, and the engine line names the two values it copied from the
    schema's examples. (A second model turn here would look the names up.)"""
    script = _Script(later=_looks_up)
    answer, state = _run(monkeypatch, script)
    assert len(script.prompts) == 1, "a second model turn ran"
    assert answer == ANSWER, "the answer was replaced"
    tail = _tail(answer, state)
    assert MARKER in tail
    for name in COPIED:
        assert f'"{name}"' in tail
    # The line hands the orchestrator no data: no name a result holds, nothing made up.
    for name in (*LOOKED_UP, MADE_UP):
        assert name not in tail


def test_a_name_an_earlier_call_returned_still_counts_as_unreturned(monkeypatch):
    """The system agent is a long-lived runtime singleton, so its history holds
    earlier, unrelated calls. A name only an earlier call returned is not this
    call's evidence."""
    agent = _agent()
    ids = ", ".join(row[0] for row in LOOKUP_ROWS)
    earlier = _Script(first_calls=[LOOKUP],
                      first_answer="\n".join(f"{row[0]}: {row[2]}" for row in LOOKUP_ROWS))
    _run(monkeypatch, earlier, agent=agent, question=f"Which customers have ids {ids}?")
    answer, state = _run(monkeypatch, _Script(), agent=agent)
    tail = _tail(answer, state)
    for name in COPIED:
        assert f'"{name}"' in tail, (
            f"{name} was counted as returned because an earlier call on this agent returned it")


def test_names_the_results_returned_are_left_alone(monkeypatch):
    """The same names, once a result in the call holds them, pass byte-identical."""
    script = _Script(first_calls=[*_FIXTURE["tool_calls"], LOOKUP], first_answer=NAMED_FROM_LOOKUP)
    answer, state = _run(monkeypatch, script)
    assert len(script.prompts) == 1
    assert sa._append_engine_facts(answer, state) == NAMED_FROM_LOOKUP


@pytest.mark.parametrize("question", [
    pytest.param(f"What did {COPIED[1]} receive from batch {BATCH}?", id="plain"),
    pytest.param(f"Show {COPIED[1]} 2026 shipments from batch {BATCH}", id="name-then-number"),
])
def test_a_name_the_question_gave_is_left_alone(monkeypatch, question):
    text = f"{COPIED[1]} shipped three orders from it."
    answer, state = _run(monkeypatch, _Script(first_answer=text), question=question)
    assert sa._append_engine_facts(answer, state) == text, (
        "a name the delegated question gave was reported as unreturned")


def test_a_lower_case_copy_of_a_schema_example_gets_the_line(monkeypatch):
    """Capitalisation is no boundary: the answer's own spelling is quoted."""
    lowered = ANSWER
    for name in COPIED:
        lowered = lowered.replace(name, name.lower())
    answer, state = _run(monkeypatch, _Script(first_answer=lowered))
    assert answer == lowered
    tail = _tail(answer, state)
    for name in COPIED:
        assert f'"{name.lower()}"' in tail


def test_an_answer_without_example_names_is_byte_identical(monkeypatch):
    ids = ", ".join(row[0] for row in LOOKUP_ROWS)
    plain = f"Batch {BATCH} shipped to three customers (ids {ids}); their names were not returned."
    script = _Script(first_answer=plain)
    answer, state = _run(monkeypatch, script)
    assert len(script.prompts) == 1
    assert sa._append_engine_facts(answer, state) == plain


def test_annotate_returns_the_answer_with_the_engine_line(monkeypatch):
    script = _Script(later=_looks_up)
    answer, state = _run(monkeypatch, script, mode="annotate")
    assert len(script.prompts) == 1, "a second model turn ran"
    assert answer == ANSWER, answer
    assert f'"{COPIED[0]}"' in _tail(answer, state)


def test_a_budget_stopped_answer_gets_the_line_like_any_other(monkeypatch):
    stopped = f"{sa._wall_partial_marker(60.0)}\n\n{ANSWER}"
    script = _Script(first_answer=stopped)
    answer, state = _run(monkeypatch, script)
    assert len(script.prompts) == 1
    assert answer == stopped
    tail = _tail(answer, state)
    assert f'"{COPIED[0]}"' in tail and f'"{COPIED[1]}"' in tail


# --------------------------------------------------------------------------- #
# What counts as returned
# --------------------------------------------------------------------------- #

def test_a_zero_row_lookup_by_the_example_names_is_still_reported(monkeypatch):
    """The agent looked the copied names up and found no row. The executed
    query in that result names them, but no row does."""
    quoted = ", ".join(f"'{name}'" for name in COPIED)
    empty = _query_result(
        f"SELECT customer_id, company_name FROM customers WHERE company_name IN ({quoted})",
        ["customer_id", "company_name"], [])
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], empty]))
    tail = _tail(answer, state)
    for name in COPIED:
        assert f'"{name}"' in tail, "the query echo of a lookup that found no row counted as data"


def test_a_name_only_in_an_error_result_is_reported(monkeypatch):
    """An error result returns nothing, whatever text it carries."""
    failed = {"sql": "lookup", "status": "error",
              "result": f"Error: no customer named {COPIED[0]} or {COPIED[1]}"}
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], failed]))
    tail = _tail(answer, state)
    for name in COPIED:
        assert f'"{name}"' in tail, "a name in an error result counted as returned"


def test_a_record_with_a_list_and_a_status_returns_its_values(monkeypatch):
    """A connected system's record with a list field and a status field (the
    review's probe) returns those values: the answer is byte-identical."""
    desc = ("items(item_id: uuid e.g. 5b81; status: character varying e.g. Ready for Review, "
            "Closed Out Pending; tags: text e.g. priority)")
    text = "Item k1 is Ready for Review."
    found = _raw_result("item", {"id": "k1", "status": "Ready for Review", "tags": ["priority"]})
    script = _Script(first_calls=[found], first_answer=text)
    answer, state = _run(monkeypatch, script, agent=_agent(desc),
                         question="What state is item k1 in?")
    assert sa._append_engine_facts(answer, state) == text, (
        "a returned record value was treated as metadata")
    assert len(script.prompts) == 1


def test_accepted_miss_a_wrapped_query_echo(monkeypatch):
    """A non-tabular wrapper that echoes the looked-up name outside an engine
    key proves it: no line for that name. The other copied name still gets one."""
    wrapped = _raw_result("lookup", {"result": {"query": COPIED[1], "results": []}})
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], wrapped]))
    tail = _tail(answer, state)
    assert f'"{COPIED[0]}"' in tail
    assert f'"{COPIED[1]}"' not in tail


def test_accepted_miss_a_longer_returned_name(monkeypatch):
    """A row naming ``<example> West`` (maybe another record) proves the example."""
    longer = _query_result("SELECT company_name FROM customers WHERE customer_code = 'C1'",
                           ["company_name"], [[f"{COPIED[1]} West"]])
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], longer]))
    tail = _tail(answer, state)
    assert f'"{COPIED[0]}"' in tail
    assert f'"{COPIED[1]}"' not in tail


@pytest.mark.parametrize("follow", ["3 shipments", "Received 3 shipments", "2026 Totals"])
def test_a_name_followed_by_a_number_or_capitalised_word_is_reported(monkeypatch, follow):
    """Whatever word follows it, an example name no result returned is reported."""
    text = f"{COPIED[0]} {follow}: $4,080.00"
    answer, state = _run(monkeypatch, _Script(first_answer=text))
    assert f'"{COPIED[0]}"' in _tail(answer, state), (
        f"the name was not checked because '{follow.split()[0]}' followed it")


def _big_result(k: int, size: int = 199_000) -> dict:
    """A maximum-size tabular result (the query result cap is 200,000 bytes)."""
    rows, used = [], 0
    while used < size:
        row = [f"R{k}-{len(rows):06d}", "padding value for a wide result row"]
        rows.append(row)
        used += len(json.dumps(row)) + 2
    return _query_result(f"SELECT code, note FROM t{k}", ["code", "note"], rows)


_BIG = [_big_result(k) for k in range(8)]


def test_many_maximum_size_results_keep_the_check_on(monkeypatch):
    """The system agent allows 40 tool calls, each result up to 200,000 bytes;
    eight such results must not switch the check off."""
    assert all(190_000 < len(big["result"]) < 200_000 for big in _BIG)
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], *_BIG]))
    tail = _tail(answer, state)
    for name in COPIED:
        assert f'"{name}"' in tail, "eight maximum-size results switched the check off"


def test_a_name_proven_before_many_large_results_stays_proven(monkeypatch):
    ship = json.loads(_FIXTURE["tool_calls"][3]["result"])
    row_id = ship["rows"][1][ship["columns"].index("customer_id")]
    proven = _query_result(f"SELECT company_name FROM customers WHERE customer_id = '{row_id}'",
                           ["company_name"], [[COPIED[1]]])
    answer, state = _run(monkeypatch, _Script(first_calls=[*_FIXTURE["tool_calls"], proven, *_BIG]))
    tail = _tail(answer, state)
    assert f'"{COPIED[0]}"' in tail
    assert f'"{COPIED[1]}"' not in tail, (
        "a name an earlier result of this call returned was forgotten")


# --------------------------------------------------------------------------- #
# The query entry point and the deployed mode
# --------------------------------------------------------------------------- #

def _set_mode(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    else:
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)


def _query(monkeypatch, script, mode) -> str:
    _set_mode(monkeypatch, mode)
    _use(monkeypatch, script)
    return asyncio.run(sa._run_mode_a_query(_agent(), "erp", QUESTION))


@pytest.mark.parametrize("mode", [None, "annotate"], ids=["deployed-default", "annotate"])
def test_the_query_entry_point_returns_the_answer_and_the_engine_line(monkeypatch, mode):
    script = _Script(later=_looks_up)
    answer = _query(monkeypatch, script, mode)
    assert len(script.prompts) == 1, "a second model turn ran"
    assert QUESTION in script.prompts[0]
    assert answer.startswith(ANSWER), "the answer was replaced"
    tail = answer[len(ANSWER):]
    assert MARKER in tail
    for name in COPIED:
        assert f'"{name}"' in tail


def test_the_deployed_mode_takes_this_path(monkeypatch):
    """What the stacks deploy: the CDK app's context default for the mode is
    empty, and the
    runtime env contract leaves the key out when it is empty, so the deployed
    runtimes read the code default, report-only, which runs this check (the
    entry point tests above run with the key unset). A deploy that opts into
    annotate takes the same path; enforce (the typed envelope) does not."""
    from agents import runtime_env_contract as contract
    from agents.config import response_integrity_mode
    repo = Path(__file__).resolve().parents[3]
    compute = (repo / "stacks" / "adt_compute_stack.py").read_text(encoding="utf-8")
    assert 'self.node.try_get_context("response_integrity_mode") or ""' in compute
    env = contract.build_env("system", contract.RuntimeEnvSources(region="us-east-1"),
                             system_id="erp", system_name="ERP", schema="erp_data",
                             connection_type="PostgreSQL", system_agent_model="m")
    assert "RESPONSE_INTEGRITY_MODE" not in env
    _set_mode(monkeypatch, None)
    assert response_integrity_mode() == "report-only"
    seen: list = []
    monkeypatch.setattr(sa, "_legacy_mode_a_answer",
                        lambda agent, question, mode, state: seen.append(mode) or "ok")
    assert sa._invoke_mode_a_agent_with_state(_agent(), QUESTION)[0] == "ok"
    assert seen == ["report-only"]
