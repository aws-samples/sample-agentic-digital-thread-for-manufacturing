# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's mapping rows and rules give the wizard's provision what the
wizard's own rows give it, field by field.

The chat (``map_fields`` / ``resolve_identities``) and the wizard (the map job /
``_do_resolve``) are two pipelines over one row shape. The app's chat
provision joins the chat's rows to the wizard's discover with the wizard map
job's own step (``_attach_map_provenance``) and hands them to the wizard's
provision, so every field provision reads must come out the same. One shape
difference was real: ``map_fields`` batches MCP tags by equipment station and
the model echoes the station as the row's table, which is not a discovered
table, so the rows could not be joined to the schema. ``map_fields`` now puts
each row back on the tool its tag came from, as the map job does for its own
split tables.
"""
from __future__ import annotations

import copy
import json
import types

import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa

CONCEPTS = {"work-order", "equipment-id", "downtime", "quality-metric", "sensor-reading"}
PG_TABLES = [{"name": "work_orders", "relation_kind": "base_table", "schema_revision": "rev-1",
              "columns": [{"name": "wo_number", "data_type": "text"},
                          {"name": "asset_tag", "data_type": "text"},
                          {"name": "opened_at", "data_type": "timestamp"}]}]
MODEL_ROWS = [
    {"field": "work_orders.wo_number", "table": "work_orders", "reasoning": "id",
     "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.95}]},
    {"field": "work_orders.asset_tag", "table": "work_orders", "reasoning": "tag",
     "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9},
                  {"concept": "work-order", "role": "correlate", "confidence": 0.6}]},
    {"field": "work_orders.opened_at", "table": "work_orders", "reasoning": "time",
     "mappings": [{"concept": "work-order", "role": "measure", "confidence": 0.8}]},
]


def _metadata(tables):
    return onboard._mapping_schema_metadata(tables)


def _provision_reads(row: dict) -> tuple:
    """Every value the wizard's provision derives from one mapping row."""
    return (onboard._mapping_table_field(row), onboard._confirmed_role_mappings(row),
            row.get("relation_kind"), row.get("data_type"), row.get("schema_revision"),
            row.get("concept"), row.get("confidence"))


def test_chat_and_wizard_rows_give_the_provision_the_same_fields():
    chat_rows = copy.deepcopy(MODEL_ROWS)
    oa._annotate_best_concepts(chat_rows)                       # map_fields
    wizard_rows = copy.deepcopy(MODEL_ROWS)
    onboard._select_field_concepts(wizard_rows, False, CONCEPTS)  # the map job
    chat = onboard._attach_map_provenance({"mappings": chat_rows}, _metadata(PG_TABLES))
    wizard = onboard._attach_map_provenance({"mappings": wizard_rows}, _metadata(PG_TABLES))
    assert len(chat["mappings"]) == len(wizard["mappings"]) == 3
    for c, w in zip(chat["mappings"], wizard["mappings"]):
        assert _provision_reads(c) == _provision_reads(w), c["field"]
    assert onboard._concept_mapping_items(chat["mappings"], committable=CONCEPTS) == \
        onboard._concept_mapping_items(wizard["mappings"], committable=CONCEPTS)


def test_chat_rows_pass_the_wizards_provision_provenance_check(monkeypatch):
    monkeypatch.setattr(onboard, "_schema_provenance", {"rev-1": {
        "status": "ready", "system_id": "plant-cmms",
        "tables": {"work_orders": {"relation_kind": "base_table", "columns": {
            "wo_number": "text", "asset_tag": "text", "opened_at": "timestamp"}}}}})
    rows = copy.deepcopy(MODEL_ROWS)
    oa._annotate_best_concepts(rows)
    rows = onboard._attach_map_provenance({"mappings": rows}, _metadata(PG_TABLES))["mappings"]
    trusted = onboard._trusted_provision_body(onboard.ProvisionRequest(
        system_id="plant-cmms", mappings=rows))
    assert [m["field"] for m in trusted.mappings] == [m["field"] for m in MODEL_ROWS]


def test_chat_and_wizard_rules_name_the_same_join(monkeypatch):
    """The chat's rule names the tables in source_table/target_table; the
    wizard's qualifies the fields. Provision reads the same join from both."""
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [{"tbl": "assets"}])
    ctx = types.SimpleNamespace(mappings=[{"field": "work_orders.asset_tag",
                                           "table": "work_orders"}])
    chat_rule = {"concept": "equipment-id", "source_field": "asset_tag",
                 "source_table": "work_orders", "target_system": "plant-erp",
                 "target_field": "tag", "target_table": "assets"}
    wizard_rule = {"concept": "equipment-id", "source_field": "work_orders.asset_tag",
                   "target_system": "plant-erp", "target_field": "assets.tag"}
    for rule in (chat_rule, wizard_rule):
        assert onboard._dp_rule_join_tables(ctx, rule) == ("work_orders", "assets")
        assert rule["source_field"].split(".")[-1] == "asset_tag"
        assert rule["target_field"].split(".")[-1] == "tag"


# ── MCP: the rows go back on the discovered tool ────────────────────────────

MCP_TABLES = [
    {"name": "get_tags", "row_count": 3, "columns": [
        {"name": "Ent/Site/Line1/StationA/Down", "data_type": "bool"},
        {"name": "Ent/Site/Line1/StationA/Good", "data_type": "int"},
        {"name": "Ent/Site/Line1/StationB/Speed", "data_type": "float"}]},
    {"name": "get_alarms", "row_count": 1, "columns": [
        {"name": "Ent/Site/Line1/StationA/Alarm", "data_type": "bool"}]},
]


class _Session:
    def __init__(self, tables):
        self.item = {"system_id": "line-mcp", "status": "discovered",
                     "connection_type": "MCP Server", "discovered_tables": tables}

    def get_session(self, _sid):
        return copy.deepcopy(self.item)

    def advance_status(self, _sid, _from, to, **fields):
        self.item.update(status=to, **fields)


def _station_model(prompt: str) -> str:
    block = prompt.split("Tables and columns:\n", 1)[1].split("\n\n", 1)[0]
    rows = []
    for line in block.splitlines():
        table, _, cols = line.strip().partition(": ")
        rows += [{"field": f"{table}.{c}", "table": table, "reasoning": "",
                  "mappings": [{"concept": "sensor-reading", "role": "measure",
                                "confidence": 0.9}]} for c in cols.split(", ") if c]
    return json.dumps(rows)


@pytest.fixture
def mcp_session(monkeypatch):
    from agents.catalog import SystemCatalog
    session = _Session(copy.deepcopy(MCP_TABLES))
    monkeypatch.setattr(oa, "_store", lambda: session)
    monkeypatch.setattr(oa, "catalog", SystemCatalog([{
        "id": "line-mcp", "name": "Line MCP", "connection_type": "MCP Server",
        "status": "registered"}]))
    monkeypatch.setattr(oa, "bedrock_call", _station_model)
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "concepts_prompt_block", lambda: "- sensor-reading: x")
    monkeypatch.setattr(catalog_mod, "concept_hints_block", lambda: "  sensor-reading: x")
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: {})
    return session


def test_map_fields_puts_each_mcp_tag_back_on_its_tool(mcp_session):
    result = oa.map_fields("line-mcp")
    assert "error" not in result, result
    got = {(m["table"], m["field"]) for m in mcp_session.item["mappings"]}
    assert got == {(t["name"], f"{t['name']}.{c['name']}")
                   for t in MCP_TABLES for c in t["columns"]}


def test_the_chats_mcp_rows_survive_the_wizards_provenance_join(mcp_session):
    oa.map_fields("line-mcp")
    tables = copy.deepcopy(MCP_TABLES)
    for t in tables:
        t["schema_revision"] = "rev-2"
    joined = onboard._attach_map_provenance(
        {"mappings": copy.deepcopy(mcp_session.item["mappings"])}, _metadata(tables))
    assert len(joined["mappings"]) == 4
