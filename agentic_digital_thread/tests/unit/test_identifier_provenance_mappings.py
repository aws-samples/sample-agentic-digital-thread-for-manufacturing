# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier provenance on the measured concept mappings of real onboarded systems.

Onboarding's mapper gives a business key the ``identifier`` role in one
system and ``correlate`` in another (the fixture holds both for the same
columns). A system answer must carry the source column of a value whenever
the column is one the app treats as a cross-system join key, whatever role
the mapper picked. The fixture is the graph's own mapping payload; the table
and column names come from it, and the values are made up.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import identifier_provenance as ip
from agents import live_concept_mappings as lcm
from agents import orchestrator as orch
from agents import runtime_asgi
from agents import system_agent as sa
from agents import tool_result_capture as trc

FIXTURE = Path(__file__).parent / "fixtures" / "concept_mappings_workshop_systems.json"
VALUE = "AB-CD-1234"


def _systems() -> dict:
    return json.loads(FIXTURE.read_text())["systems"]


def _correlate_fields(system_id: str, concept: str) -> list:
    """The fields the measured mapping gives ``concept`` with the correlate role."""
    return [i["field"] for i in _systems()[system_id]
            if i["concept"] == concept and i["role"] == "correlate"]


def _answer_for(system_id: str, field: str) -> str:
    """The Mode A answer for one SQL result reading ``field``, asked by the
    orchestrator, with the system's measured mappings as its snapshot."""
    table, column = field.split(".", 1)
    columns = sorted({i["field"].split(".", 1)[1] for i in _systems()[system_id]
                      if i["field"].startswith(table + ".")})
    schema = {table: {"columns": {c: "character varying" for c in columns}}}
    sql = f"SELECT t.{column} FROM {table} t LIMIT 5"
    payload = {"columns": [column], "rows": [[VALUE]], "row_count": 1, "_adt_executed_query": sql}
    capture = trc.InvocationToolResultCapture(system_id=system_id, physical_schema=schema)
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None, tool_use={"toolUseId": "u1", "name": "query_public",
                                      "input": {"sql": sql}},
        invocation_state=state, exception=None, retry=False, cancel_message=None,
        result={"toolUseId": "u1", "status": "success",
                "content": [{"text": json.dumps(payload)}]},
        agent=SimpleNamespace(messages=[])))
    runtime_asgi._process_one_mcp_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "q", "arguments": {"question": "q", ip.RESERVED_ARG: ip.REQUESTED}}},
        system_id)
    lcm.reset()
    lcm.remember_snapshot(system_id, _systems()[system_id])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state",
                   lambda _a, _q: (f"The record holds {VALUE}.", state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        mp.setattr(lcm, "_loaded", {})
        try:
            return asyncio.run(sa._run_mode_a_query(object(), system_id, "q"))
        finally:
            lcm.reset()


_PRODUCT_KEYS = [(s, f) for s in sorted(_systems()) for f in _correlate_fields(s, "product")]


@pytest.mark.parametrize(("system_id", "field"), _PRODUCT_KEYS)
def test_a_correlate_role_product_key_carries_its_source_column(system_id, field):
    """Reproduction: every column the measured mapping ties to the product
    concept with the correlate role, a business key by its name, names its
    value's source column in the answer the orchestrator receives."""
    _body, records = ip.split_line(_answer_for(system_id, field))
    assert records, f"{system_id} {field}: no identifier-sources line"
    table, column = field.split(".", 1)
    assert {(r["table"], r["column"]): r["values"] for r in records} == {(table, column): [VALUE]}


def test_every_measured_system_with_a_product_key_is_covered():
    assert len({s for s, _f in _PRODUCT_KEYS}) >= 4


# --------------------------------------------------------------------------- #
# The guard on the workshop graph's recorded joins, with the measured mappings
# --------------------------------------------------------------------------- #
def _tool(system_id: str) -> str:
    return f"{system_id}___query_{system_id.replace('-', '_')}"


def _concepts(system_id: str, field: str) -> list:
    """The concepts the measured mapping gives ``field`` with a key role."""
    return sorted({i["concept"] for i in _systems()[system_id]
                   if i["field"] == field and i["role"] in ("identifier", "correlate")})


def _line_text(text: str, field: str, value: str, concepts: list) -> str:
    start = text.index(value)
    entry = {"f": field, "c": concepts,
             "at": [[start, start + len(value), ip.token_digest(value)]]}
    return f"{text}\n\n{ip.LINE_PREFIX}{json.dumps([entry], separators=(',', ':'))}"


class _Gateway:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        self.calls.append(name)
        return {"status": "success", "toolUseId": tool_use_id,
                "content": [{"text": self.answers.get(name, "No rows.")}]}


def _send(source: str, answer: str, target: str, value: str) -> dict:
    """The target's result when ``value``, in ``source``'s answer, is sent to
    it, on the workshop graph a module 6 account holds."""
    from tests.unit import _route_seed_workshop_fixture as wf
    ip.reset_turn_facts()
    orch.build_route_seed(query=wf.graph(wf.MODULE_6),
                          question=f"Which revision was production run {wf.RUN_NUMBER} built to?")
    client = orch._wrap_gateway_client_with_decline_guard(_Gateway({_tool(source): answer}))
    try:
        asyncio.run(client.call_tool_async("t1", _tool(source), {"question": "First hop."}))
        return asyncio.run(client.call_tool_async("t2", _tool(target),
                                                  {"question": f"Records for {value}?"}))
    finally:
        ip.reset_turn_facts()


def test_the_measured_product_key_sent_to_the_parts_system_is_refused_through_its_join():
    """The plant MES answer carries the measured mapping's product key; the
    parts system holds parts under another field and the graph joins it to
    the ERP's part number. The refusal names that join and no other."""
    (field,) = _correlate_fields("mes-ind", "product")
    result = _send("mes-ind", _answer_for("mes-ind", field), "plm", VALUE)
    text = result["content"][0]["text"]
    assert result["status"] == "error"
    assert "erp" in text and "products.engineering_part_number" in text
    assert "batch_id" not in text


def test_a_maintenance_equipment_key_sent_to_the_erp_is_not_refused():
    """The ERP's recorded joins are batch and part joins; a machine key is
    neither, so the call goes through."""
    text = _line_text("Asset 1001-ABC-441 has one open order.", "assets.asset_num",
                      "1001-ABC-441", ["equipment-id"])
    assert _send("cmms-ind", text, "erp", "1001-ABC-441")["status"] == "success"


_REQUIRED_PATH = [
    ("erp", "shipment_items.batch_id", ["batch"], "RUN-20261008-90002", "mes-ind"),
    ("mes-ind", "material_consumption.lot_number", None, "LOT-IND-AB-H1", "erp"),
    ("erp", "lot_mes_tracking.batch_id", ["batch"], "WO-20261008-10002", "mes-ind"),
    ("mes-ind", "production_runs.run_number", None, "RUN-20261008-90003", "erp"),
    ("mes-ind", "production_runs.sku", None, VALUE, "erp"),
    ("cmms-ind", "assets.asset_num", ["equipment-id"], "1001-ABC-441", "mes-ind"),
]


@pytest.mark.parametrize(("source", "field", "concepts", "value", "target"), _REQUIRED_PATH)
def test_the_required_path_delegation_shapes_go_through(source, field, concepts, value, target):
    """Every cross-system delegation shape of the required path that carries
    a value an earlier system returned, with the measured concepts where the
    mapping fixture holds the source system."""
    concepts = concepts if concepts is not None else _concepts(source, field)
    text = _line_text(f"The record holds {value}.", field, value, concepts)
    assert _send(source, text, target, value)["status"] == "success"
