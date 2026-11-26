# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's provision provisions what was confirmed, or nothing.

The chat's provision reads the system's schema again (the wizard's discover)
and joins the session's mappings to it. A mapped field that is no longer in
the schema would be left out by that join, and an identity rule naming it
would still be written, so the provision would commit less than the user
confirmed at map and resolve and keep a rule on a field that is gone. Any
such loss refuses the provision before anything is claimed or started, names
the fields, and tells the model to start the system over and map it again.
A rule whose source is not in the schema the system was mapped with is
another system's: it is left out, not written as this system's.
Offline, on the fixture of test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import copy
import json

import pytest

import agents.onboard as onboard
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, TABLES, _agent_tools, _chat_provision_answer, _chat_until_resolved, _wait_for, world)

GONE = "Ent/Site/Line1/StationA/Good"
KEPT = "Ent/Site/Line1/StationA/Down"


def _without(column: str) -> list:
    tables = copy.deepcopy(TABLES)
    tables[0]["columns"] = [c for c in tables[0]["columns"] if c["name"] != column]
    return tables


def _rule(field: str, table: str = "get_tags") -> dict:
    return {"concept": "quality-metric", "source_field": f"{table}.{field}",
            "source_table": table, "source_pattern": "", "target_system": "plant-mes",
            "target_field": "good_count", "target_table": "runs", "target_pattern": "",
            "confidence": 0.8, "reasoning": "shared counter"}


def _set_rules(world, rules: list) -> None:
    world.table.items[SID]["rules"] = json.dumps(rules)


def _refused_with_nothing_started(world, res: dict, writes_before: int) -> None:
    assert res.get("reason") == "schema_changed", res
    assert "status" not in res, res
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == [] and world.manager.started == []
    assert len(world.writes) == writes_before, "the refused provision wrote to the graph"
    assert not onboard._provision_running(SID)
    assert "reset_system" in res["error"] and "map_fields" in res["error"]


def test_a_mapped_field_gone_from_the_schema_refuses_the_provision(world):
    _chat_until_resolved()
    world.discover_tables = _without(GONE)
    before = len(world.writes)
    res = _chat_provision_answer()
    _refused_with_nothing_started(world, res, before)
    assert res["fields_not_found"] == [f"get_tags.{GONE}"]
    assert GONE in res["error"]


def test_a_rule_on_the_field_that_is_gone_refuses_and_writes_nothing(world):
    _chat_until_resolved()
    _set_rules(world, [_rule(GONE), _rule(KEPT)])
    world.discover_tables = _without(GONE)
    before = len(world.writes)
    res = _chat_provision_answer()
    _refused_with_nothing_started(world, res, before)
    assert res["fields_not_found"] == [f"get_tags.{GONE}"]
    assert res["rules_not_found"] == [f"get_tags.{GONE}"]
    assert not any("IdentityRule" in c for c in world.cyphers())


@pytest.mark.parametrize("rule", [
    _rule("Ent/Site/Line1/StationA/Retired"),                        # never mapped, not in it
    {**_rule("Ent/Site/Line1/StationA/Retired"), "source_system": SID},
], ids=["missing-field", "missing-field-of-this-system"])
def test_a_rule_on_a_source_field_not_in_the_schema_refuses(world, rule):
    _chat_until_resolved()
    _set_rules(world, [rule])
    before = len(world.writes)
    res = _chat_provision_answer()
    _refused_with_nothing_started(world, res, before)
    assert res["rules_not_found"] == [rule["source_field"]]
    assert "fields_not_found" not in res


def test_a_rule_on_a_table_it_had_when_mapped_and_no_longer_has_refuses(world):
    world.discover_tables = copy.deepcopy(TABLES) + [
        {"name": "get_alarms", "row_count": 1,
         "columns": [{"name": "Ent/Site/Line1/StationA/Alarm", "data_type": "bool"}]}]
    _chat_until_resolved()
    _set_rules(world, [_rule("Ent/Site/Line1/StationA/Alarm", table="get_alarms")])
    world.table.items[SID]["mappings"] = json.dumps(
        [m for m in world.store.get_session(SID)["mappings"] if m["table"] == "get_tags"])
    world.discover_tables = copy.deepcopy(TABLES)
    before = len(world.writes)
    res = _chat_provision_answer()
    _refused_with_nothing_started(world, res, before)
    assert res["rules_not_found"] == ["get_alarms.Ent/Site/Line1/StationA/Alarm"]


def test_a_rule_on_a_table_of_another_system_is_left_out(world):
    """A table the system did not have when it was mapped is another
    system's: its rule is not provisioned with this one."""
    _chat_until_resolved()
    other = _rule(KEPT, table="get_alarms")
    _set_rules(world, [other])
    finished = _chat_provision_answer()
    assert finished.get("status") == "onboarded" and finished["rules_left_out"] == 1, finished
    assert world.job_bodies[0].rules == []


@pytest.mark.parametrize("rule", [
    _rule(KEPT),
    {**_rule(KEPT), "source_field": KEPT},                            # the column alone
    {**_rule(KEPT), "source_table": "", "source_field": f"get_tags.{KEPT}"},
    {**_rule(KEPT), "source_field": f"get_tags.get_tags.{KEPT}"},     # the table twice
    {**_rule(KEPT), "source_table": "", "source_field": f"get_tags.get_tags.{KEPT}"},
], ids=["qualified", "column-only", "table-in-field", "table-twice", "table-twice-no-table"])
def test_a_stable_schema_with_a_rule_on_a_mapped_field_provisions_as_before(world, rule):
    _chat_until_resolved()
    _set_rules(world, [rule])
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    assert "fields_not_found" not in started
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    body = world.job_bodies[0]
    assert body.rules == [rule]
    assert {m["field"] for m in body.mappings} == {
        f"get_tags.{c['name']}" for c in TABLES[0]["columns"]}


# ── a dotted column is matched whole ────────────────────────────────────────

DOTTED = [{"name": "orders", "row_count": 2, "columns": [
    {"name": "header.code", "data_type": "string"},
    {"name": "line.code", "data_type": "string"}]}]


@pytest.mark.parametrize("field, table", [
    ("orders.header.code", "orders"), ("orders.orders.header.code", "orders"),
    ("header.code", "orders"), ("orders.header.code", ""), ("code", "orders"),
], ids=["qualified", "table-twice", "column", "no-table", "leaf"])
def test_a_rule_on_a_dotted_column_that_exists_is_present(field, table):
    from agents.onboard_chat_lifecycle import _rule_sources_missing
    rule = {"source_field": field, "source_table": table}
    assert _rule_sources_missing([rule], DOTTED) == []


@pytest.mark.parametrize("field", ["orders.status.code", "status.code", "orders.orders.status.code"],
                         ids=["qualified", "column", "table-twice"])
def test_a_rule_on_a_dotted_column_that_is_gone_is_missing_while_its_leaf_lives_on(field):
    """``status.code`` is not a column even though other columns end in
    ``code``."""
    from agents.onboard_chat_lifecycle import _rule_sources_missing
    assert _rule_sources_missing([{"source_field": field, "source_table": "orders"}],
                                 DOTTED) == [field]


def test_a_rule_on_a_dotted_column_not_in_the_schema_refuses_the_provision(world):
    world.discover_tables = copy.deepcopy(DOTTED)
    _chat_until_resolved()
    _set_rules(world, [_rule("status.code", table="orders")])
    before = len(world.writes)
    res = _chat_provision_answer()
    _refused_with_nothing_started(world, res, before)
    assert res["rules_not_found"] == ["orders.status.code"]
