# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A delegation that asked for the identifier-sources line and got a
successful answer stating values without it is logged once per turn.

The orchestrator's delegation wrapper asks each system for the line when the
turn's graph facts were read. A current system always answers the request:
the line, or an empty line when no key column of its result is stated. A
system that does not know the request drops it and answers without any line;
the guard then has no source for any value and lets every call through, which
is its designed behaviour and stays so. What changes: the turn, cloud or
local, logs one bounded line naming the systems and how many of their answers
came back without the line.

The schema is made up: a shop-floor system, a ledger and a drawing register.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from agents import identifier_provenance as ip
from agents import orchestrator as orch
from agents import runtime_app

PART = "AB-CD-1234"
DESIGN = "DS-1234-V3"
SID = "session-1800000000000-skew0002"
WRITE_TOKEN = "fedcba9876543210fedcba9876543210"
QUERY_REF = "q-0123456789ab"
QUESTION = "Which design version was job JOB-77001 built to?"
TOOL = {s: f"{s}___query_{s.replace('-', '_')}" for s in ("shop-floor", "ledger", "drawings")}
MARKER = "IDENTIFIER_SOURCES_MISSING"

JOINS = (
    {"id": "jp-1", "from_system": "ledger", "from_field": "ledger_db.items.design_code",
     "to_system": "drawings", "to_field": "drawings_db.records.drawing_no",
     "method": "value_exact_match"},
    {"id": "jp-2", "from_system": "shop-floor", "from_field": "shop_db.jobs.job_no",
     "to_system": "ledger", "to_field": "ledger_db.items.job_ref",
     "method": "value_exact_match"},
)


def _facts():
    rows = [{"system_id": s,
             "join_paths": [j for j in JOINS if s in (j["from_system"], j["to_system"])],
             "identity_rules": [], "value_mappings": []}
            for s in ("shop-floor", "ledger", "drawings")]
    return ip.facts_from(QUESTION, rows, cap=50)


ANSWERS = {TOOL["shop-floor"]: (f"Job JOB-77001 built part {PART} on line 4.",
                                [("jobs.part_code", [PART], ["part"])]),
           TOOL["ledger"]: (f"Item for job JOB-77001: design code {DESIGN}.",
                            [("items.design_code", [DESIGN], ["part"])]),
           TOOL["drawings"]: ("There are none.", [])}
#: An answer stating values that come from no key column (a timestamp and an
#: asset name): a current system acknowledges the request with an empty line.
NO_KEY_ANSWER = "Reading 2.80 at 2026-09-01T10:00:04 on Press-Line-07."


class _Edge:
    """A gateway whose system agents answer with or without the line: a
    current system writes it whenever asked (:data:`ip.ACK_LINE` when it has
    no key column to name), and ``knows_request=False`` is a system that
    predates the request and never writes it."""

    def __init__(self, *, knows_request: bool, status: str = "success"):
        self.knows_request, self.status, self.calls = knows_request, status, []

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        self.calls.append((name, dict(arguments or {})))
        text, records = ANSWERS[name]
        if self.knows_request and (arguments or {}).get(ip.RESERVED_ARG) == ip.REQUESTED:
            text += "\n\n" + (ip.line_for(text, records) or ip.ACK_LINE)
        return {"status": self.status, "toolUseId": tool_use_id, "content": [{"text": text}]}


def _args(question: str) -> dict:
    return {"question": question,
            "_adt_provenance": {"session_id": SID, "query_ref": QUERY_REF,
                                "write_token": WRITE_TOKEN}}


def _turn(monkeypatch, edge, calls, *, facts=True) -> list:
    """One runtime turn through the entrypoint seam: the route seed's facts,
    the delegation wrapper, ``calls`` (``[(system, question)]``) in order.
    Returns the results the model would read."""
    results: list = []

    async def body(*_invocation):
        ip._TURN_FACTS.set(_facts() if facts else None)
        client = orch._wrap_gateway_client_with_decline_guard(edge)
        for n, (system, question) in enumerate(calls):
            results.append(await client.call_tool_async(f"t{n}", TOOL[system], _args(question)))
        yield {"data": "done"}

    monkeypatch.setattr(runtime_app, "_invoke_integrity_runtime", body)
    selected = runtime_app._runtime_entrypoint(body, build_orchestrator=object())

    async def run():
        async for _event in selected({}, None):
            pass

    asyncio.run(run())
    return results


def _lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if MARKER in r.getMessage()]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    ip.reset_turn_facts()
    yield
    ip.reset_turn_facts()


def test_a_system_that_ignores_the_request_is_logged_once_per_turn(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    edge = _Edge(knows_request=False)
    _turn(monkeypatch, edge, [("shop-floor", "Which part did job JOB-77001 build?"),
                              ("shop-floor", "And on which line?"),
                              ("ledger", "Design code for job JOB-77001?")])
    assert all(args.get(ip.RESERVED_ARG) == ip.REQUESTED for _n, args in edge.calls)
    [line] = _lines(caplog)
    message = line.getMessage()
    assert "calls=3" in message
    assert "systems=ledger:1,shop-floor:2" in message


def test_the_line_carries_no_session_ref_token_value_or_question(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    question = f"List the design versions of part {PART}."
    _turn(monkeypatch, _Edge(knows_request=False), [("shop-floor", question)])
    [line] = _lines(caplog)
    message = line.getMessage()
    for secret in (SID, QUERY_REF, WRITE_TOKEN, PART, question, "JOB-77001"):
        assert secret not in message


def test_the_guard_still_fails_open_without_the_line(monkeypatch, caplog):
    edge = _Edge(knows_request=False)
    results = _turn(monkeypatch, edge, [("shop-floor", "Which part?"),
                                        ("drawings", f"Versions of {PART}?")])
    assert [r["status"] for r in results] == ["success", "success"]
    assert [name for name, _a in edge.calls] == [TOOL["shop-floor"], TOOL["drawings"]]


def test_no_line_when_the_system_answers_with_the_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    results = _turn(monkeypatch, _Edge(knows_request=True),
                    [("shop-floor", "Which part?"), ("ledger", "Design code?")])
    assert all(ip.LINE_PREFIX not in r["content"][0]["text"] for r in results)
    assert _lines(caplog) == []


def test_no_line_when_the_call_failed(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _turn(monkeypatch, _Edge(knows_request=False, status="error"), [("shop-floor", "Which part?")])
    assert _lines(caplog) == []


def test_no_line_when_the_request_was_not_sent(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    edge = _Edge(knows_request=False)
    _turn(monkeypatch, edge, [("shop-floor", "Which part?")], facts=False)
    assert all(ip.RESERVED_ARG not in args for _n, args in edge.calls)
    assert _lines(caplog) == []


def test_no_line_when_the_answer_states_no_value(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _turn(monkeypatch, _Edge(knows_request=False), [("drawings", "Any drawings this week?")])
    assert _lines(caplog) == []


def test_no_line_under_enforce_where_systems_never_write_it(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    _turn(monkeypatch, _Edge(knows_request=False), [("shop-floor", "Which part?")])
    assert _lines(caplog) == []


def test_with_no_turn_record_each_system_is_logged_on_its_first_miss(caplog):
    """The fallback only: a guard built where no entrypoint started a turn
    record (a direct caller of the gateway client). Each system's first
    answer without the line is logged as it happens, and a repeat is not."""
    caplog.set_level(logging.INFO)
    ip._TURN_UNANSWERED.set(None)
    ip._TURN_FACTS.set(_facts())
    client = orch._wrap_gateway_client_with_decline_guard(_Edge(knows_request=False))
    for n in range(2):
        asyncio.run(client.call_tool_async(f"t{n}", TOOL["shop-floor"], _args("Which part?")))
    [line] = _lines(caplog)
    assert "systems=shop-floor:1" in line.getMessage()


class _LocalAgent:
    """A local orchestrator stand-in: invoking it makes ``calls`` through
    the gateway client its build wrapped."""

    def __init__(self, client, calls):
        self.client, self.calls = client, calls
        self.callback_handler = None
        self.hooks = SimpleNamespace(add_hook=lambda _hook: None)

    def __call__(self, _prompt, **_kwargs):
        for n, (system, question) in enumerate(self.calls):
            asyncio.run(self.client.call_tool_async(f"t{n}", TOOL[system], _args(question)))
        return "final answer"


def _local_turn(monkeypatch, edge, calls) -> list:
    """One LOCAL chat turn (``LocalChatClient.stream``): the build, in the
    turn's own producer thread, wraps the gateway client with the turn's
    route-seed facts, and the agent then makes ``calls``. Returns once the
    producer thread has finished too: the turn's line is logged when it ends,
    after the stream's last event."""
    import threading

    from agents import chat_client
    from agents import self_learning

    def build(*_args, **_kwargs):
        ip._TURN_FACTS.set(_facts())
        return _LocalAgent(orch._wrap_gateway_client_with_decline_guard(edge), calls)

    finished = threading.Event()
    produce = chat_client._run_local_agent

    def run(*args):
        try:
            produce(*args)
        finally:
            finished.set()

    monkeypatch.setattr(chat_client, "build_orchestrator", build)
    monkeypatch.setattr(chat_client, "_run_local_agent", run)
    monkeypatch.setattr(chat_client, "_local_metric_layer_prompt", lambda p, _q, _s: p)
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda _ids: [])
    events = list(chat_client.LocalChatClient().stream(prompt="p", session_id=SID, user_id="u"))
    assert finished.wait(10), "the local producer thread did not finish"
    return events


def test_a_local_turn_logs_one_line_for_the_whole_turn(monkeypatch, caplog):
    """Reproduction on the local chat path: two answers without the line
    from one system and one from another give ONE line for the turn, with
    every call counted."""
    from agents.events import DoneEvent
    caplog.set_level(logging.INFO)
    edge = _Edge(knows_request=False)
    events = _local_turn(monkeypatch, edge, [("shop-floor", "Which part did job JOB-77001 build?"),
                                             ("ledger", "Design code for job JOB-77001?"),
                                             ("shop-floor", "And on which line?")])
    assert any(isinstance(e, DoneEvent) for e in events)
    assert len(edge.calls) == 3
    [line] = _lines(caplog)
    assert "calls=3 systems=ledger:1,shop-floor:2" in line.getMessage()


def test_a_local_turn_whose_systems_answer_the_request_logs_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _local_turn(monkeypatch, _Edge(knows_request=True),
                [("shop-floor", "Which part?"), ("ledger", "Design code?")])
    assert _lines(caplog) == []


# --------------------------------------------------------------------------- #
# A current system acknowledges a request it has no key column for
# --------------------------------------------------------------------------- #
def _system_answer(answer: str, *, requested: bool) -> str:
    """The Mode A answer of a system whose call returned no key column."""
    from agents import live_concept_mappings as lcm
    from agents import runtime_asgi
    from agents import system_agent as sa
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "query_shop_floor", "arguments": {
               "question": "Latest reading?",
               **({ip.RESERVED_ARG: ip.REQUESTED} if requested else {})}}}
    runtime_asgi._process_one_mcp_message(msg, "shop-floor")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, {}))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        mp.setattr(lcm, "_loaded", {})
        try:
            return asyncio.run(sa._run_mode_a_query(object(), "shop-floor", "Latest reading?"))
        finally:
            lcm.reset()


def test_a_requested_call_with_no_key_column_ends_with_the_empty_line():
    answer = _system_answer(NO_KEY_ANSWER, requested=True)
    assert answer == f"{NO_KEY_ANSWER}\n\n{ip.ACK_LINE}"
    assert ip.split_line(answer) == (NO_KEY_ANSWER, [])


def test_an_unrequested_call_still_gets_no_line():
    assert _system_answer(NO_KEY_ANSWER, requested=False) == NO_KEY_ANSWER


def test_the_empty_line_is_stripped_and_not_logged(caplog):
    """A current system's acknowledgement: never reaches the model, and an
    answer with values but no key column is not counted as missing."""
    caplog.set_level(logging.INFO)
    answer = _system_answer(NO_KEY_ANSWER, requested=True)
    result = {"status": "success", "content": [{"text": answer}]}
    stripped, records, text = ip.strip_result(result)
    assert stripped["content"][0]["text"] == NO_KEY_ANSWER and records == []
    assert ip.states_a_value(text)
    guard = ip.TurnGuard(_facts(), unanswered=ip.Unanswered(turn=False))
    seen = guard.observe(TOOL["shop-floor"], result, asked=True)
    assert ip.LINE_PREFIX not in str(seen)
    assert _lines(caplog) == []


def test_the_same_answer_from_a_system_that_ignores_the_request_is_logged(caplog):
    caplog.set_level(logging.INFO)
    result = {"status": "success", "content": [{"text": NO_KEY_ANSWER}]}
    guard = ip.TurnGuard(_facts(), unanswered=ip.Unanswered(turn=False))
    guard.observe(TOOL["shop-floor"], result, asked=True)
    [line] = _lines(caplog)
    assert "systems=shop-floor:1" in line.getMessage()
    assert "Press-Line-07" not in line.getMessage()
