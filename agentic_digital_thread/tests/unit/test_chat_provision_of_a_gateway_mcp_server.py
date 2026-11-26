# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's provision of an MCP server behind an AgentCore gateway.

The shapes are the real ones for the Quick query-bridge gateway server,
built by the real connector (``onboard_agent._discover_mcp``) from the
server's JSON-RPC answers:

* tools are named with the gateway's target prefix
  (``query-bridge___get_prompts``);
* a tool with parameters has one column per parameter (``job_id``;
  ``question``, ``plant``);
* a tool without parameters is called once and its answer flattened, so
  ``get_prompts`` (``{"prompts": [ ... ]}``) has nested columns
  ``prompts.<field>``, one per field of a prompt.

The chat's map_fields keeps the rows its model returns as they are; the
model names a nested column by its leaf under the tool
(``query-bridge___get_prompts.schedule``). And resolve_identities proposes
the rules it finds anywhere in the graph, here between the MES systems and
the CMMS, none of them sourced in this system. Neither is a change to the
schema. The provision still refuses for a mapped field or a rule source of
this system that the schema read again no longer has. Offline, on the
fixture of test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging

import httpx

import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.models import DiscoverRequest
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, _agent_tools, _chat_provision_answer, _chat_until_resolved, _wait_for, world)

#: The real connector, captured before the fixture replaces it.
REAL_DISCOVER_MCP = oa._discover_mcp

PROMPTS_TOOL = "query-bridge___get_prompts"
RESULT_TOOL = "query-bridge___get_query_result"
SUBMIT_TOOL = "query-bridge___submit_query"
PROMPT_FIELDS = ["template_id", "source", "created", "pinned", "schedule", "title", "text",
                 "persona", "category"]


def _prompts() -> list:
    """GET /api/prompts as the bridge returns it: 20 prompts, four of them
    video briefings (one more field), lists of strings dropped by the
    flatten."""
    out = []
    for i in range(20):
        prompt = {"template_id": f"reg-{i:02d}", "source": "registry",
                  "created": "2026-10-10T11:00:00Z", "pinned": i < 3, "schedule": None,
                  "title": f"Prompt {i}", "text": f"What happened on line {i}?",
                  "persona": "plant-manager", "category": "operations",
                  "required_systems": ["mes-mil"]}
        if i < 4:
            prompt["type"] = "video_briefing"
        out.append(prompt)
    return out


_TOOLS = [
    {"name": PROMPTS_TOOL, "inputSchema": {"type": "object", "properties": {}}},
    {"name": RESULT_TOOL, "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}}}},
    {"name": SUBMIT_TOOL, "inputSchema": {"type": "object", "properties": {
        "question": {"type": "string"}, "plant": {"type": "string"}}}},
]


def _gateway(prompts_answer=None, tools=None):
    """The gateway's JSON-RPC answers (initialize, tools/list, tools/call)."""
    def _handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                  headers={"mcp-session-id": "s-1"})
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": tools or _TOOLS}})
        answer = prompts_answer() if prompts_answer else {"prompts": _prompts()}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(answer)}]}})
    return _handle


def _gateway_tables(monkeypatch, **kwargs) -> list:
    """The tables the connector discovers on that gateway."""
    real_client = httpx.Client
    handler = _gateway(**kwargs)

    class _Client(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    monkeypatch.setattr(httpx, "Client", _Client)
    try:
        result = REAL_DISCOVER_MCP({"connection_string": "https://gw.example.com/mcp",
                                    "connection_config": {"token": "t",
                                                          "auth_scheme": "bearer"}})
    finally:
        monkeypatch.setattr(httpx, "Client", real_client)
    assert "error" not in result, result
    return result["tables"]


def _row(table: str, field: str, concept: str = "needs_review") -> dict:
    return {"field": f"{table}.{field}", "table": table, "concept": concept,
            "confidence": 0.3, "mappings": [{"concept": concept, "role": "correlate",
                                             "confidence": 0.3}],
            "reasoning": "prompt catalog / query job"}


#: The chat map_fields rows as the session held them: the prompt fields by
#: their leaf under the tool, the parameters as discovered.
LEAF_ROWS = ([_row(PROMPTS_TOOL, f) for f in PROMPT_FIELDS]
             + [_row(RESULT_TOOL, "job_id", "sensor-reading"),
                _row(SUBMIT_TOOL, "question", "sensor-reading"),
                _row(SUBMIT_TOOL, "plant", "equipment-id")])

PHYSICAL_FIELDS = ({f"{PROMPTS_TOOL}.prompts.{f}" for f in PROMPT_FIELDS}
                   | {f"{RESULT_TOOL}.job_id", f"{SUBMIT_TOOL}.question",
                      f"{SUBMIT_TOOL}.plant"})


def _cross_system_rule(source_field: str, source_table: str, **extra) -> dict:
    return {"concept": "production-line", "source_field": source_field,
            "source_table": source_table, "source_pattern": "LINE[0-9]+",
            "target_system": "cmms", "target_field": "area", "target_table": "assets",
            "target_pattern": "<Area>-L[0-9]+", "confidence": 0.9,
            "reasoning": "MES line ids match the CMMS asset area", **extra}


#: The six rules resolve_identities returned for it: production lines of the
#: MES systems linked to the CMMS asset area, none sourced in this system.
OTHER_SYSTEMS_RULES = [
    _cross_system_rule("line", "production_runs", source_system="mes-mil"),
    _cross_system_rule("line_id", "work_orders", source_system="mes-cr"),
    _cross_system_rule("line", "production_orders"),
    _cross_system_rule("line_id", "batches"),
    _cross_system_rule("mes-ind.line", ""),
    _cross_system_rule("line_id", ""),
]

OWN_RULE = {"concept": "equipment-id", "source_field": f"{SUBMIT_TOOL}.plant",
            "source_table": SUBMIT_TOOL, "source_pattern": "", "target_system": "cmms",
            "target_field": "plant", "target_table": "assets", "target_pattern": "",
            "confidence": 0.8, "reasoning": "plant names"}


def _resolved_as_in_the_chat(world, tables, rows=None, rules=None) -> None:
    """The chat's discover, map_fields and resolve_identities ran on these
    tables; the session holds the rows and rules the chat's tools kept."""
    world.discover_tables = copy.deepcopy(tables)
    _chat_until_resolved()
    world.table.items[SID]["mappings"] = json.dumps(rows if rows is not None else LEAF_ROWS)
    world.table.items[SID]["rules"] = json.dumps(
        rules if rules is not None else OTHER_SYSTEMS_RULES)


def _without_column(tables: list, table: str, column: str) -> list:
    out = copy.deepcopy(tables)
    for t in out:
        if t["name"] == table:
            t["columns"] = [c for c in t["columns"] if c["name"] != column]
    return out


def _onboarded(world) -> dict:
    return _wait_for(lambda: (lambda r: r if r.get("status") != "provisioning" else None)(
        _agent_tools().provision_system(SID)), timeout=60)


# ── the schema did not change: the provision runs ──────────────────────────

def test_a_chat_provision_of_the_gateway_server_provisions_every_mapped_field(world,
                                                                              monkeypatch):
    tables = _gateway_tables(monkeypatch)
    assert [t["name"] for t in tables] == [PROMPTS_TOOL, RESULT_TOOL, SUBMIT_TOOL]
    assert [c["name"] for c in tables[0]["columns"]] == [
        f"prompts.{f}" for f in PROMPT_FIELDS + ["type"]]
    _resolved_as_in_the_chat(world, tables)
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    finished = _onboarded(world)
    assert finished["status"] == "onboarded", finished
    body = world.job_bodies[0]
    assert {m["field"] for m in body.mappings} == PHYSICAL_FIELDS
    assert {m["table"] for m in body.mappings} == {PROMPTS_TOOL, RESULT_TOOL, SUBMIT_TOOL}
    assert all(m["data_type"] and m["relation_kind"] for m in body.mappings)


def test_identity_rules_between_other_systems_are_left_out_not_written_under_it(world,
                                                                                monkeypatch):
    _resolved_as_in_the_chat(world, _gateway_tables(monkeypatch),
                             rules=OTHER_SYSTEMS_RULES + [OWN_RULE])
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    finished = _onboarded(world)
    assert finished.get("rules_left_out") == len(OTHER_SYSTEMS_RULES), finished
    assert "sourced in other systems" in finished["message"], finished
    assert world.job_bodies[0].rules == [OWN_RULE]
    written = [p for c, p in world.writes if "IdentityRule" in c and "src_field" in p]
    assert {p["src_field"] for p in written} <= {"plant"}, written


def test_a_gateway_tool_named_without_its_prefix_is_the_discovered_tool(world, monkeypatch):
    rows = [_row("get_prompts", "schedule"), _row("get_query_result", "job_id")]
    _resolved_as_in_the_chat(world, _gateway_tables(monkeypatch), rows=rows, rules=[])
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    _onboarded(world)
    assert {m["field"] for m in world.job_bodies[0].mappings} == {
        f"{PROMPTS_TOOL}.prompts.schedule", f"{RESULT_TOOL}.job_id"}


def test_a_row_that_names_no_column_of_the_schema_is_left_out_as_the_wizard_leaves_it(
        world, monkeypatch, caplog):
    tables = _gateway_tables(monkeypatch)
    tables[2]["columns"].append({"name": "header.code", "data_type": "string"})
    tables[2]["columns"].append({"name": "status.code", "data_type": "string"})
    rows = LEAF_ROWS + [_row(PROMPTS_TOOL, "no_such_field"),   # not a column
                        _row(SUBMIT_TOOL, "code")]             # two columns end in it
    _resolved_as_in_the_chat(world, tables, rows=rows, rules=[])
    caplog.set_level(logging.WARNING, logger="agents.onboard_chat_lifecycle")
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    finished = _onboarded(world)
    assert sorted(finished.get("fields_left_out") or []) == [
        f"{PROMPTS_TOOL}.no_such_field", f"{SUBMIT_TOOL}.code"]
    assert "left out" in finished["message"]
    assert {m["field"] for m in world.job_bodies[0].mappings} == PHYSICAL_FIELDS
    assert any(f"{PROMPTS_TOOL}.no_such_field" in r.getMessage() for r in caplog.records)


# ── a field or rule source of this system that is gone still refuses ──────

def _refused_unchanged(world, res: dict) -> None:
    assert res.get("reason") == "schema_changed", res
    assert "status" not in res, res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == [] and world.manager.started == []
    assert not onboard._provision_running(SID)


def test_a_mapped_nested_field_the_server_no_longer_returns_refuses(world, monkeypatch):
    tables = _gateway_tables(monkeypatch)
    _resolved_as_in_the_chat(world, tables)
    world.discover_tables = _without_column(tables, PROMPTS_TOOL, "prompts.persona")
    res = _chat_provision_answer()
    _refused_unchanged(world, res)
    assert res["fields_not_found"] == [f"{PROMPTS_TOOL}.prompts.persona"], res
    assert "rules_not_found" not in res, res


def test_a_rule_on_a_parameter_the_tool_no_longer_takes_refuses(world, monkeypatch):
    tables = _gateway_tables(monkeypatch)
    _resolved_as_in_the_chat(world, tables, rules=OTHER_SYSTEMS_RULES + [OWN_RULE])
    world.discover_tables = _without_column(tables, SUBMIT_TOOL, "plant")
    res = _chat_provision_answer()
    _refused_unchanged(world, res)
    assert res["rules_not_found"] == [f"{SUBMIT_TOOL}.plant"], res
    assert res["fields_not_found"] == [f"{SUBMIT_TOOL}.plant"], res


def test_a_refusal_is_logged_with_its_reason_and_the_missing_fields(world, monkeypatch,
                                                                     caplog):
    tables = _gateway_tables(monkeypatch)
    _resolved_as_in_the_chat(world, tables, rules=[OWN_RULE])
    world.discover_tables = _without_column(tables, SUBMIT_TOOL, "plant")
    caplog.set_level(logging.WARNING, logger="agents.onboard_chat_lifecycle")
    res = _chat_provision_answer()
    assert res.get("reason") == "schema_changed", res
    refusals = [r for r in caplog.records
                if r.levelno == logging.WARNING and "refused" in r.getMessage()]
    assert len(refusals) == 1, [r.getMessage() for r in caplog.records]
    text = refusals[0].getMessage()
    assert SID in text and "schema_changed" in text and f"{SUBMIT_TOOL}.plant" in text, text


# ── the wizard's discover of a server that calls back into the app ─────────

def test_the_wizards_discover_leaves_the_app_free_to_answer_the_servers_call(world):
    """``get_prompts`` answers from the app's own ``GET /api/prompts``. The
    discover samples it while the app serves the call, so it must not hold
    the app's event loop."""
    loop_of_the_route: dict = {}

    async def _api_prompts():
        return {"prompts": _prompts()}

    def _connector(_entry, system_id=""):
        call = asyncio.run_coroutine_threadsafe(_api_prompts(), loop_of_the_route["loop"])
        try:
            answer = call.result(timeout=2)
        except TimeoutError:
            call.cancel()
            return {"tables": [{"name": PROMPTS_TOOL, "columns": [], "row_count": 0}]}
        columns = [{"name": f"prompts.{k}", "data_type": "str"}
                   for p in answer["prompts"] for k in p if k in PROMPT_FIELDS]
        return {"tables": [{"name": PROMPTS_TOOL, "columns": columns,
                            "row_count": len(columns)}]}
    world.mp.setattr(oa, "_discover_mcp", _connector)
    entry = world.catalog.get(SID)

    async def _route():
        loop_of_the_route["loop"] = asyncio.get_running_loop()
        return await onboard.onboard_discover(DiscoverRequest(
            system_id=SID, connection_string=entry["connection_string"],
            connection_type=entry["connection_type"], connection_config={}))
    disc = asyncio.run(_route())
    assert "error" not in disc, disc
    assert [c["name"] for c in disc["tables"][0]["columns"]] == [
        f"prompts.{f}" for f in PROMPT_FIELDS], "the server's call into the app timed out"
