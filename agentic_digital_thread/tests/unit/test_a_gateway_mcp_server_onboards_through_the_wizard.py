# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A gateway MCP server onboards through the wizard, and through the chat.

The Quick gateway's query-bridge server has a tool, ``get_prompts``, that
takes no arguments, so discover calls it and reads its answer: the app's own
``GET /api/prompts`` catalog, ``{"prompts": [ ... ]}``. Every prompt carries
the same fields, so the answer describes one record shape: ``prompts.title``,
``prompts.text``, ... . Discover's columns are that shape, one column per
field whatever the number of prompts, with the type of its values; and the
tables discover answers pass the map's join to the provenance discover
stored for them. Before, a field came back once per prompt and the wizard's
``POST /map`` refused the discover's own tables (``schema_provenance_invalid``).

The fixture is the real thing as far as it can be offline: the app's prompt
catalog (``prompts.list_prompts``, the demo pack), the bridge's own
``get_prompts`` calling ``GET /api/prompts``, the gateway's JSON-RPC answers
and the connector's sampling (``onboard_agent._discover_mcp``). The graph,
the agent manager, Bedrock and the session table are the fakes of
test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import json
import time

import httpx
import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.models import DiscoverRequest, MapRequest, OnboardResolveRequest, ProvisionRequest
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    JOB_WAIT, SID, _agent_tools, _chat_provision_answer, _chat_until_resolved, world)

#: The real connector, captured before the fixture replaces it.
REAL_DISCOVER_MCP = oa._discover_mcp

PROMPTS_TOOL = "query-bridge___get_prompts"
RESULT_TOOL = "query-bridge___get_query_result"
SUBMIT_TOOL = "query-bridge___submit_query"
APP_URL = "http://adt-app.internal"


# ── the server: the bridge's tools behind the gateway, the app behind them ─

def _api_prompts(monkeypatch) -> list:
    """``GET /api/prompts`` as the app answers it with no stored prompts:
    the demo prompt pack (``prompts.list_prompts``)."""
    import agents.prompts as prompts

    class _NoStoredPrompts:
        def list_all(self, source=None):
            return []
    monkeypatch.setattr(prompts, "_prompt_store", _NoStoredPrompts())
    monkeypatch.setattr(prompts.demo, "is_enabled", lambda: True)
    catalog = prompts.list_prompts()
    assert len(catalog) > 1, "the prompt catalog is empty"
    return catalog


_TOOLS = [
    {"name": PROMPTS_TOOL, "inputSchema": {"type": "object", "properties": {}}},
    {"name": RESULT_TOOL, "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": SUBMIT_TOOL, "inputSchema": {"type": "object", "properties": {
        "question": {"type": "string"}, "plant": {"type": "string"}},
        "required": ["question"]}},
]


def _serve(monkeypatch, catalog: list, answers=None) -> list:
    """Route every ``httpx.Client`` request: ``GET <app>/api/prompts`` to the
    app's catalog, anything else to the gateway's JSON-RPC. A ``tools/call``
    of ``get_prompts`` runs the bridge's own tool (which calls the app);
    ``answers`` (tool name -> JSON value) replaces a tool's answer. Returns
    the list of the tools called."""
    import agents.query_bridge_server as bridge
    monkeypatch.setenv("ADT_APP_URL", APP_URL)
    monkeypatch.setenv("QUICK_GATEWAY_TOKEN", "test-only-gateway-token")  # nosec B105
    monkeypatch.delenv("QUICK_GATEWAY_TOKEN_REF", raising=False)
    called: list = []

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/prompts":
            return httpx.Response(200, json=catalog)
        body = json.loads(request.content)
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {}},
                                  headers={"mcp-session-id": "s-1"})
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                             "result": {"tools": tools}})
        name = body["params"]["name"]
        called.append(name)
        if answers and name in answers:
            text = json.dumps(answers[name])
        else:
            text = bridge.get_prompts()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}]}})

    tools = list(_TOOLS) + [{"name": n, "inputSchema": {"type": "object", "properties": {}}}
                            for n in (answers or {}) if n != PROMPTS_TOOL]
    real_client = httpx.Client

    class _Client(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(_handle)
            super().__init__(*a, **kw)
    monkeypatch.setattr(httpx, "Client", _Client)
    return called


def _connect_the_real_connector(world) -> None:
    """The real connector for both discovers. The chat's in-process discover
    reads the connection from the System node, which this fixture's system
    has none of before its first provision; it is given the catalog's URL."""
    url = world.catalog.get(SID)["connection_string"]

    def _discover(entry, system_id=""):
        return REAL_DISCOVER_MCP({**entry, "connection_string":
                                  entry.get("connection_string") or url},
                                 system_id=system_id)
    world.mp.setattr(oa, "_discover_mcp", _discover)


def _wizard_discover(world) -> dict:
    """``POST /api/onboard/discover`` with the body the wizard sends."""
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    assert "error" not in disc, disc
    return disc


def _fields_of(catalog: list) -> list:
    """The record shape of the catalog: its scalar fields in first-seen order."""
    return list(dict.fromkeys(k for p in catalog for k, v in p.items()
                              if not isinstance(v, (dict, list))))


def _poll(fn, *args) -> dict:
    end = time.monotonic() + JOB_WAIT
    while time.monotonic() < end:
        res = asyncio.run(fn(*args))
        if res.get("status") != "running":
            return res
        time.sleep(0.05)
    raise AssertionError("timed out")


# ── discover answers the record shape ─────────────────────────────────────

def test_discover_answers_one_column_per_field_of_the_catalog(world, monkeypatch):
    catalog = _api_prompts(monkeypatch)
    _serve(monkeypatch, catalog)
    _connect_the_real_connector(world)
    disc = _wizard_discover(world)
    tables = {t["name"]: t for t in disc["tables"]}
    assert list(tables) == [PROMPTS_TOOL, RESULT_TOOL, SUBMIT_TOOL]
    names = [c["name"] for c in tables[PROMPTS_TOOL]["columns"]]
    assert names == [f"prompts.{f}" for f in _fields_of(catalog)], names
    assert [c["name"] for c in tables[RESULT_TOOL]["columns"]] == ["job_id"]
    assert [c["name"] for c in tables[SUBMIT_TOOL]["columns"]] == ["question", "plant"]


def test_a_field_takes_the_type_of_its_values(world, monkeypatch):
    rows = [{"value": 1, "path": "Ent.SiteA.Good", "quality": None, "flag": True},
            {"value": 2.5, "path": "Ent.SiteA.Speed", "quality": "good", "flag": None},
            {"value": None, "path": "Ent.SiteA.Down", "quality": "bad", "flag": False},
            {"value": "n/a", "path": "Ent.SiteA.Mode", "quality": None, "flag": True}]
    _serve(monkeypatch, [], answers={"NameSpace_MCP": rows,
                                     "Counters": [{"n": 1}, {"n": 2.5}, {"n": None}]})
    _connect_the_real_connector(world)
    tables = {t["name"]: t for t in _wizard_discover(world)["tables"]}
    types = {c["name"]: c["data_type"] for c in tables["NameSpace_MCP"]["columns"]}
    assert types == {"value": "str", "path": "str", "quality": "str", "flag": "bool"}, types
    assert [(c["name"], c["data_type"]) for c in tables["Counters"]["columns"]] == [
        ("n", "float")]


def test_a_namespace_keyed_by_its_tag_paths_is_read_as_before(world, monkeypatch):
    """A namespace answered as one object keyed by its tag paths (each path
    once): the same columns as before, in the same order, with their types."""
    namespace = {"Ent": {"SiteA": {"Area1": {
        "StationA": {"Good": 120, "Down": False, "Speed": 41.5},
        "StationB": {"Good": 98, "Batch": "B-1"}}}, "SiteB": {"Area2": {
            "StationC": {"Rate": 1800.0, "_path": "Ent/SiteB/Area2/StationC"}}}}}
    _serve(monkeypatch, [], answers={"NameSpace_MCP": namespace})
    _connect_the_real_connector(world)
    tables = {t["name"]: t for t in _wizard_discover(world)["tables"]}
    assert [(c["name"], c["data_type"]) for c in tables["NameSpace_MCP"]["columns"]] == [
        ("Ent.SiteA.Area1.StationA.Good", "int"),
        ("Ent.SiteA.Area1.StationA.Down", "bool"),
        ("Ent.SiteA.Area1.StationA.Speed", "float"),
        ("Ent.SiteA.Area1.StationB.Good", "int"),
        ("Ent.SiteA.Area1.StationB.Batch", "str"),
        ("Ent.SiteB.Area2.StationC.Rate", "float"),
        ("Ent.SiteB.Area2.StationC._path", "str")]
    assert tables["NameSpace_MCP"]["row_count"] == 7


# ── what discover answers passes its own provenance join ───────────────────

def test_the_wizards_map_takes_the_tables_its_discover_answered(world, monkeypatch):
    catalog = _api_prompts(monkeypatch)
    _serve(monkeypatch, catalog)
    _connect_the_real_connector(world)
    disc = _wizard_discover(world)
    started = asyncio.run(onboard.onboard_map(MapRequest(system_id=SID,
                                                         tables=disc["tables"])))
    assert started.get("status") == "running" and started.get("job_id"), started
    mapped = _poll(onboard.onboard_map_status, started["job_id"])
    assert mapped.get("status") == "done", mapped
    fields = {m["field"] for m in mapped["mappings"]}
    discovered = {f"{t['name']}.{c['name']}" for t in disc["tables"] for c in t["columns"]}
    assert fields == discovered, fields ^ discovered


@pytest.mark.parametrize("columns", [
    [{"name": "a", "data_type": "str"}, {"name": "a", "data_type": "str"}],
    [{"name": "a", "data_type": "NoneType"}, {"name": "a", "data_type": "str"}],
    [{"name": "a", "data_type": "int"}, {"name": ""}, "not-a-column",
     {"name": "b"}, {"name": "a", "data_type": "float"}],
], ids=["repeated", "repeated-other-type", "repeated-nameless-and-not-a-column"])
def test_any_discover_answer_passes_its_own_provenance_join(columns, monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_schema_provenance", JobStore("schema-provenance", table=None))
    result = {"tables": [{"name": "t", "row_count": 1, "columns": copy.deepcopy(columns)}]}
    answered = onboard._store_discovered_provenance("prov-sys", result)
    trusted = onboard._trusted_map_tables("prov-sys", answered["tables"])
    names = [c["name"] for c in trusted[0]["columns"]]
    assert names == list(dict.fromkeys(c["name"] for c in columns
                                       if isinstance(c, dict) and c.get("name"))), names
    assert collections.Counter(names).most_common(1)[0][1] == 1


# ── onboarded end to end: the wizard, and the chat ─────────────────────────

def test_the_wizard_onboards_the_gateway_server(world, monkeypatch):
    catalog = _api_prompts(monkeypatch)
    _serve(monkeypatch, catalog)
    _connect_the_real_connector(world)
    entry = world.catalog.get(SID)
    disc = _wizard_discover(world)
    started = asyncio.run(onboard.onboard_map(MapRequest(system_id=SID,
                                                         tables=disc["tables"])))
    mapped = _poll(onboard.onboard_map_status, started["job_id"])
    assert mapped.get("status") == "done" and mapped["mappings"], mapped
    resolving = asyncio.run(onboard.onboard_resolve(OnboardResolveRequest(
        system_id=SID, mappings=mapped["mappings"])))
    resolved = _poll(onboard.onboard_resolve_status, resolving["job_id"])
    assert "error" not in resolved, resolved
    provisioning = asyncio.run(onboard.onboard_provision(ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or "", rules=[], mappings=mapped["mappings"],
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={})))
    done = _poll(onboard.onboard_provision_status, provisioning["job_id"])
    assert done.get("status") == "done", done
    assert world.catalog.get(SID)["status"] == "onboarded"
    assert len(world.manager.started) == 1
    body = world.job_bodies[0]
    assert {m["field"] for m in body.mappings} >= {f"{PROMPTS_TOOL}.prompts.title",
                                                   f"{SUBMIT_TOOL}.question"}


def test_the_chat_onboards_the_gateway_server(world, monkeypatch):
    """The chat's discover, map_fields and resolve_identities, then the app's
    provision, which reads the schema again with the wizard's discover and
    joins the session's mappings to the provenance that discover stored."""
    catalog = _api_prompts(monkeypatch)
    _serve(monkeypatch, catalog)
    _connect_the_real_connector(world)
    _chat_until_resolved()
    session = world.store.get_session(SID)
    columns = [c["name"] for t in session["discovered_tables"] if t["name"] == PROMPTS_TOOL
               for c in t["columns"]]
    assert columns == [f"prompts.{f}" for f in _fields_of(catalog)], columns
    finished = _chat_provision_answer()
    assert finished.get("status") == "onboarded", finished
    assert world.catalog.get(SID)["status"] == "onboarded"
    body = world.job_bodies[0]
    assert f"{PROMPTS_TOOL}.prompts.title" in {m["field"] for m in body.mappings}
