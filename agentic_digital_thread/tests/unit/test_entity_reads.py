# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which of a connector's own read tools reads which of its entities.

The onboarding agent test proves a PostgreSQL read from the SQL capture: the
captured statement names the table. The API connectors write no statement,
and their schema entries are entities (a Tulip table, a
SiteWise asset model) that their tools read under other names
(``tulip_query_records``, ``sitewise_list_assets``), so
the agent test could only read the answer text. Each connector now declares
which of its read tools reads which entity, next to the tool list it owns
(``system_agent``), and the agent test reads the call's trace against it.

Pinned here: every entity each connector's discovery exposes maps to at least
one tool that connector registers, every tool a declaration names is one the
connector registers, an entity the source names itself needs its id in an
argument the tool takes, and the trace check reads only the tool call and its
own parse of the source's response.

Names and ids are made up and differ from the seed data. Offline.
"""
from __future__ import annotations

import json

import pytest

import agents.system_agent as sa
from agents import connection_types as ct
from agents import entity_reads as er


# ── what each connector registers and exposes ─────────────────────────────────

def _registered(toolset: list) -> set:
    return {t.tool_name for t in toolset}


def _tulip_entities(monkeypatch) -> list:
    import agents.onboard_agent as oa
    monkeypatch.setattr(oa, "_tulip_table_columns", lambda *a, **k: [])
    listing = [{"id": "kb7Q2", "label": "Kiln Batches"}, {"id": "sl9", "label": "Shift Log"}]
    return [t["name"] for t in oa._tulip_tables_from_list(listing, "https://tulip.invalid",
                                                           {}, "")]


def _sitewise_entities(monkeypatch) -> list:
    import agents.onboard_agent as oa
    import agents.sitewise_read as swr
    monkeypatch.setattr(oa, "_sitewise_discover_cfg", lambda *a, **k: {})
    models = [{"id": "m-press", "name": "Press Line Model",
               "properties": [{"id": "p1", "name": "Ram Force", "dataType": "DOUBLE"}]},
              {"id": "m-oven", "name": "Curing Oven", "properties": []}]
    monkeypatch.setattr(swr, "list_asset_models_with_properties", lambda *a, **k: models)
    monkeypatch.setattr(swr, "list_assets", lambda *a, **k: [])
    found = oa._discover_sitewise_direct_model("press-telemetry", {}, None, {"region": "x"})
    names = [t["name"] for t in found["tables"]]
    assert "Press Line Model" in names and oa.ASSET_HIERARCHY_TABLE in names
    return names + [t["name"] for t in oa._sitewise_fallback_tables()]


def _i3x_entities(monkeypatch) -> list:
    import agents.onboard_i3x as oi

    def _get(path, **_params):
        return {"/namespaces": [{"uri": "urn:foundry", "displayName": "Foundry"}],
                "/objecttypes": [{"elementId": "furnace-type", "displayName": "Furnace"},
                                 {"elementId": "site-type", "displayName": "Site"}],
                }.get(path, [])

    monkeypatch.setattr(oi, "make_i3x_client", lambda *a, **k: (_get, lambda *a, **k: []))
    found = oi.discover_i3x_data_model({"connection_config": {"base_url": "https://i3x.invalid"}})
    names = [t["name"] for t in found["tables"]]
    assert "Furnace" in names
    return names + [t["name"] for t in oi._fallback_tables()]


def _connectors() -> dict:
    """``{connector: (declaration, its toolset, its discovery's entity names)}``."""
    return {
        "tulip": (lambda: sa.entity_reads_for(ct.TULIP),
                  lambda: sa._make_tulip_tools("https://tulip.invalid", token="unused"),
                  _tulip_entities),
        "sitewise-direct": (lambda: sa.entity_reads_for(ct.SITEWISE_DIRECT),
                            lambda: sa._make_sitewise_direct_tool(
                                region="us-east-1", connection_config={},
                                system_id="press-telemetry"),
                            _sitewise_entities),
        "i3x": (lambda: sa.entity_reads_for(ct.SITEWISE_I3X),
                lambda: sa._make_i3x_tools("https://i3x.invalid", token="unused"),
                _i3x_entities),
    }


CONNECTORS = sorted(_connectors())


@pytest.mark.parametrize("connector", CONNECTORS)
def test_every_entity_a_connector_exposes_maps_to_a_tool_it_registers(connector, monkeypatch):
    declaration, toolset, entities = _connectors()[connector]
    reads, registered = declaration(), _registered(toolset())
    names = entities(monkeypatch)
    assert names, connector
    for name in names:
        rule = reads.rule(name)
        assert rule is not None, f"{connector}: no read tool for {name!r}"
        assert rule.tools and set(rule.tools) <= registered, (connector, name, rule)


@pytest.mark.parametrize("connector", CONNECTORS)
def test_every_tool_a_declaration_names_is_registered_by_that_connector(connector):
    declaration, toolset, _ = _connectors()[connector]
    reads = declaration()
    rules = list(reads.named.values()) + ([reads.source_named] if reads.source_named else [])
    assert {name for rule in rules for name in rule.tools} <= _registered(toolset())


@pytest.mark.parametrize("connector", CONNECTORS)
def test_an_entity_the_source_names_is_read_with_its_id_in_an_argument_the_tool_takes(
        connector):
    """A Tulip table, a SiteWise asset model and an i3X object type are named
    by the source, so a call of the read tool reads that entity only when the
    tool's id argument holds the entity's id."""
    declaration, toolset, _ = _connectors()[connector]
    rule = declaration().source_named
    if rule is None:
        return
    assert rule.id_arg, connector
    specs = {t.tool_name: t.tool_spec["inputSchema"]["json"]["properties"] for t in toolset()}
    for name in rule.tools:
        assert rule.id_arg in specs[name], (connector, name)


# ── whether a table's name tells which rule reads it ─────────────────────────
# A connector whose discovery can return a source-named table under one of its
# fixed names cannot tell the two apart from the name (no table origin is
# kept), so it declares so, and no trace proves its reads.

def _sitewise_accepts(name, _monkeypatch) -> bool:
    """Whether SiteWise (Direct) discovery returns an asset model named
    ``name`` as a table (it takes every model name from the source)."""
    import agents.onboard_agent as oa
    tables = oa._sitewise_model_tables([{"id": "m-any", "name": name, "properties": []}],
                                       {}, "us-east-1")
    return name in [t["name"] for t in tables]


def _i3x_accepts(name, _monkeypatch) -> bool:
    """Whether i3X discovery returns an object type named ``name`` as a table
    (it refuses only structural container names)."""
    import agents.onboard_i3x as oi
    table = oi._object_type_table(lambda *_a, **_k: [], lambda *_a, **_k: [],
                                  {"elementId": "type-any", "displayName": name})
    return table is not None and table["name"] == name


def _tulip_accepts(name, monkeypatch) -> bool:
    import agents.onboard_agent as oa
    monkeypatch.setattr(oa, "_tulip_table_columns", lambda *a, **k: [])
    tables = oa._tulip_tables_from_list([{"id": "t1", "label": name}],
                                        "https://tulip.invalid", {}, "")
    return name in [t["name"] for t in tables]


_ACCEPTS = {"tulip": _tulip_accepts, "sitewise-direct": _sitewise_accepts, "i3x": _i3x_accepts}


def source_names_taking_a_fixed_name(connector: str, monkeypatch) -> set:
    """The fixed rule names that ``connector``'s discovery also accepts from
    the source as a table name."""
    declaration = _connectors()[connector][0]()
    return {name for name in declaration.named if _ACCEPTS[connector](name, monkeypatch)}


@pytest.mark.parametrize("connector", CONNECTORS)
def test_a_connector_declares_whether_a_tables_name_tells_its_origin(connector, monkeypatch):
    declaration = _connectors()[connector][0]()
    collisions = source_names_taking_a_fixed_name(connector, monkeypatch)
    assert declaration.names_tell_origin is (not collisions), (connector, collisions)


@pytest.mark.parametrize("ctype", [ct.POSTGRESQL, ct.ATHENA, ct.MCP_SERVER, ct.REST_API])
def test_a_type_with_no_declaration_has_no_rules(ctype):
    assert sa.entity_reads_for(ctype) is None


# ── the trace check ───────────────────────────────────────────────────────────

def _step(tool, args=None, *, status="success", records_read=3, raw_input=None):
    step = {"seq": 1, "tool_name": tool, "status": status,
            "tool_input": raw_input if raw_input is not None else json.dumps(args or {})}
    if records_read is not None:
        step["records_read"] = records_read
    return step


FIXED = er.EntityRead(("vendor_list_lots",))
BY_ID = er.EntityRead(("vendor_read_table",), id_arg="table_ref")


@pytest.mark.parametrize("count", [3, 1, 0], ids=["records", "one", "empty"])
def test_a_successful_call_whose_tool_parsed_the_response_proves_a_fixed_entity(count):
    """A count of zero is a successful read of an empty entity: the tool
    parsed the source's response and found no records."""
    calls = er.traced_reads([_step("vendor_list_lots", records_read=count)], FIXED)
    assert er.proves_read(calls, FIXED)


@pytest.mark.parametrize("step", [
    _step("vendor_list_lots", status="error"),
    _step("vendor_list_lots", status="exception"),
    _step("vendor_list_lots", status="unknown"),
    _step("vendor_list_lots", status="error", records_read=0),
    _step("vendor_list_lots", records_read=None),
    _step("vendor_list_lots", records_read=True),
    _step("vendor_list_lots", records_read=False),
    _step("vendor_list_lots", records_read="3"),
    _step("vendor_list_lots", records_read=-1),
    _step("vendor_list_lots", records_read=0.0),
    _step("vendor_read_table"),
    _step("query_vendor_lots"),
], ids=["error", "exception", "unknown", "error-zero", "no-count", "true", "false", "text",
        "negative", "float", "other-tool", "unmapped"])
def test_no_other_step_proves_a_read(step):
    """A call that failed, one whose tool reported no count (it did not parse
    a successful response), and a call of a tool not mapped to the entity
    prove nothing. A missing count is not zero."""
    assert not er.proves_read(er.traced_reads([step], FIXED), FIXED)


def test_an_empty_read_of_a_source_named_entity_needs_its_id_too():
    calls = er.traced_reads([_step("vendor_read_table", {"table_ref": "tb-9"},
                                   records_read=0)], BY_ID)
    assert er.proves_read(calls, BY_ID, "tb-9")
    assert not er.proves_read(calls, BY_ID, "tb-1")


def test_an_entity_named_by_the_source_needs_its_id_in_the_call():
    steps = [_step("vendor_read_table", {"table_ref": "tb-9", "limit": 100})]
    calls = er.traced_reads(steps, BY_ID)
    assert er.proves_read(calls, BY_ID, "tb-9")
    assert not er.proves_read(calls, BY_ID, "tb-1")
    assert not er.proves_read(calls, BY_ID, "")


def test_an_unknown_id_is_never_matched_by_an_empty_one():
    calls = er.traced_reads([_step("vendor_read_table", {"table_ref": ""})], BY_ID)
    assert not er.proves_read(calls, BY_ID, "")


def test_the_id_must_be_the_same_string():
    """An id passed as a number is not the table's id string (fail closed)."""
    calls = er.traced_reads([_step("vendor_read_table", {"table_ref": 9})], BY_ID)
    assert not er.proves_read(calls, BY_ID, "9")


@pytest.mark.parametrize("step", [
    _step("vendor_read_table", {"limit": 100}),
    _step("vendor_read_table", {"table_ref": 9}),
    _step("vendor_read_table", raw_input='{"table_ref": "tb-9", "filters": "[{'),
    _step("vendor_read_table", raw_input="not json"),
], ids=["no-id", "number", "truncated", "text"])
def test_a_call_whose_input_does_not_carry_the_id_proves_nothing(step):
    assert not er.proves_read(er.traced_reads([step], BY_ID), BY_ID, "tb-9")


def test_any_one_proving_call_among_failed_ones_is_enough():
    steps = [_step("vendor_read_table", {"table_ref": "tb-9"}, status="error"),
             _step("vendor_read_table", {"table_ref": "tb-9"}, records_read=None),
             _step("vendor_list_lots", records_read=0),
             _step("vendor_read_table", {"table_ref": "tb-1"}),
             _step("vendor_read_table", {"table_ref": "tb-9"}, records_read=12)]
    assert er.proves_read(er.traced_reads(steps, BY_ID), BY_ID, "tb-9")


@pytest.mark.parametrize("steps", [None, "steps", [None, 3, "x"], []])
def test_a_foreign_trace_shape_proves_nothing(steps):
    assert er.traced_reads(steps, FIXED) == []
    assert not er.proves_read([], FIXED)
