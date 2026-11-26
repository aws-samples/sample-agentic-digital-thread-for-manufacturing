# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reproductions of the turn shape the identifier-provenance guard fixes.

Kept apart from ``test_identifier_provenance.py`` and written against
module seams that exist without the feature, so they run, and fail on their
assertions, on code that has no provenance channel. Made-up schema: a
shop-floor system keeps a part code on its jobs, a ledger keeps a design code
on its items, a drawing register keys its records by drawing number, and the
graph records one join into the register, from the ledger's design code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from agents import live_concept_mappings as lcm
from agents import orchestrator as orch
from agents import runtime_asgi
from agents import system_agent as sa
from agents import tool_result_capture as trc

#: The wire constants, spelled out so this file needs no import of the feature.
REQUEST_ARG, REQUESTED = "_adt_identifier_sources", "requested"
LINE_PREFIX = "[engine] identifier-sources v1: "


def _span(text: str, value: str) -> list:
    """Where ``value`` is in ``text``, bound to the exact token by its digest."""
    start = text.index(value)
    return [start, start + len(value), hashlib.sha256(value.encode()).hexdigest()[:12]]

PART, DESIGN = "AB-CD-1234", "DS-1234-V3"
QUESTION = "Which design version was job JOB-77001 built to, and is it approved?"
TOOL = {s: f"{s}___query_{s.replace('-', '_')}" for s in ("shop-floor", "ledger", "drawings")}
#: An exact join's recorded reasoning names its shape and concept.
_REASONING = ("5 exact values of a.c exist in b b.c (membership-probed); both columns shape "
              "AA-9999-A9; concept {}; 20 and 20 distinct values sampled")
JOINS = [{"id": "jp-1", "from_system": "ledger", "from_field": "ledger_db.items.design_code",
          "to_system": "drawings", "to_field": "drawings_db.records.drawing_no",
          "method": "value_exact_match", "provenance": _REASONING.format("part")},
         {"id": "jp-2", "from_system": "shop-floor", "from_field": "shop_db.jobs.job_no",
          "to_system": "ledger", "to_field": "ledger_db.items.job_ref",
          "method": "value_exact_match", "provenance": _REASONING.format("job")}]


def _seed_query():
    rows = [{"system_id": s, "join_paths": [j for j in JOINS
                                            if s in (j["from_system"], j["to_system"])],
             "identity_rules": [], "value_mappings": []}
            for s in ("shop-floor", "ledger", "drawings")]
    inventory = [{"system_id": r["system_id"], "system_name": r["system_id"]} for r in rows]

    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        return rows if cypher == orch._ROUTE_SEED_CYPHER else []
    return query


class _SystemAgents:
    """Gateway stand-in: each system answers, and adds its identifier-sources
    line when the call asks for one, as the system runtime does."""

    def __init__(self):
        self.calls = []
        self.answers = {
            TOOL["shop-floor"]: (f"Job JOB-77001 built part {PART}.", "jobs.part_code", PART),
            TOOL["ledger"]: (f"Design code {DESIGN}.", "items.design_code", DESIGN)}

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        self.calls.append(name)
        text, column, value = self.answers.get(name, ("No rows.", "", ""))
        if (arguments or {}).get(REQUEST_ARG) == REQUESTED and column:
            at = [_span(text, value)]
            text += "\n\n" + LINE_PREFIX + json.dumps([{"f": column, "c": ["part"], "at": at}],
                                                      separators=(",", ":"))
        return {"status": "success", "toolUseId": tool_use_id, "content": [{"text": text}]}


def test_a_source_value_sent_beside_its_joined_system_is_not_sent():
    """A parallel cross-system turn: the shop-floor answer hands back the
    part code, then the ledger (for the design code) and the drawing
    register (with the part code) are asked in one parallel batch. The
    register must not receive the part code: the refusal names the recorded
    join instead."""
    orch.build_route_seed(query=_seed_query(), question=QUESTION)
    agents = _SystemAgents()
    client = orch._wrap_gateway_client_with_decline_guard(agents)

    def ask(system, question):
        return asyncio.run(client.call_tool_async(f"t-{system}", TOOL[system],
                                                  {"question": question}))
    first = ask("shop-floor", "For job JOB-77001, which part was built?")
    register = ask("drawings", f"List the design versions of part \"{PART}\".")
    ask("ledger", "For job JOB-77001, give the item's design code.")
    assert TOOL["drawings"] not in agents.calls
    assert "ledger_db.items.design_code" in register["content"][0]["text"]
    assert LINE_PREFIX not in first["content"][0]["text"]


SCHEMA = {"jobs": {"columns": {"job_no": "text", "part_code": "text"}}}
SQL = "SELECT j.job_no, j.part_code FROM jobs j WHERE j.job_no = 'JOB-77001' LIMIT 500"


def test_a_requested_system_answer_names_where_its_identifiers_came_from(monkeypatch):
    """The system side: a call the orchestrator asked ends with the line
    naming the identifier-role columns its values came from, and the
    reserved argument never reaches the tool."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "query_shop_floor", "arguments": {"question": "Which part?",
                                                  REQUEST_ARG: REQUESTED}}}
    runtime_asgi._process_one_mcp_message(msg, "shop-floor")
    capture = trc.InvocationToolResultCapture(system_id="shop-floor", physical_schema=SCHEMA)
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    payload = {"columns": ["job_no", "part_code"], "rows": [["JOB-77001", PART]],
               "row_count": 1, "_adt_executed_query": SQL}
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None, tool_use={"toolUseId": "u1", "name": "query_public",
                                      "input": {"sql": SQL}},
        invocation_state=state, exception=None, retry=False, cancel_message=None,
        result={"toolUseId": "u1", "status": "success",
                "content": [{"text": json.dumps(payload)}]},
        agent=SimpleNamespace(messages=[])))
    lcm.reset()
    lcm.remember_snapshot("shop-floor", [
        {"concept": "part", "field": "jobs.part_code", "role": "identifier"}])
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state",
                        lambda _a, _q: (f"Job JOB-77001 built {PART}.", state))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    monkeypatch.setattr(lcm, "_loaded", {})
    answer = asyncio.run(sa._run_mode_a_query(object(), "shop-floor", "Which part?"))
    lcm.reset()
    stated = f"Job JOB-77001 built {PART}."
    at = [_span(stated, PART)]
    assert answer.endswith(LINE_PREFIX + json.dumps([{"f": "jobs.part_code", "c": ["part"],
                                                      "at": at}],
                                                    separators=(",", ":")))
    assert REQUEST_ARG not in msg["params"]["arguments"]


@pytest.fixture(autouse=True)
def _no_turn_state():
    yield
    try:
        from agents import identifier_provenance
        identifier_provenance.reset_turn_facts()
    except ImportError:
        pass
