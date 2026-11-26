# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One spelling of a field from the chat's resolve to the provision.

The chat's mapping rows carry ``field`` already table-qualified
(``table.column``). resolve_identities lists them to the model, and the model
answers each rule's ``source_field`` / ``source_table`` in the spelling it was
shown. The provision then checks every rule's source field against the schema
it reads again, with the provision writer's own normalization of a rule's
source field (``onboard._rule_source_column``: the last dotted segment). So
the prompt lists each field once (``table.column``, never the table twice),
and whatever spelling the model echoes, an unchanged schema provisions while a
removed field still refuses. These tests run the real resolve_identities with
a model that echoes the prompt it got, then the chat's provision. Offline, on
the fixture of test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import copy
import json

import pytest

import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, TABLES, _agent_tools, _chat_map_answer, _chat_provision_answer, _wait_for, world)

TARGET = "plant-mes"
DOWN = "Ent/Site/Line1/StationA/Down"


def _peer(world) -> None:
    """Another onboarded system with a field mapped to the same concept, so
    resolve_identities asks the model for rules."""
    world.catalog.add({"id": TARGET, "name": "Plant MES", "status": "onboarded",
                       "connection_type": "REST API", "schema": "", "system_type": "MES"})
    rows = [{"sys_id": TARGET, "sys_name": "Plant MES", "sys_schema": "",
             "field_name": "stop_code", "concept": "downtime", "table_name": "stops"}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: copy.deepcopy(rows))


def _echoing_model(world, prompts: list):
    """The chat's model: the map answer for map_fields; for resolve, one rule
    on the downtime field, in exactly the spelling the prompt listed it."""
    def _answer(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        prompts.append(prompt)
        block = prompt.split("has these concept mappings:\n", 1)[1].split("\n\n", 1)[0]
        listed = next(line.strip().split(" -> ")[0] for line in block.splitlines()
                      if line.strip().endswith("-> downtime"))
        return json.dumps({"rules": [{
            "concept": "downtime", "source_field": listed, "source_table": listed.split(".")[0],
            "source_pattern": "", "target_system": TARGET, "target_field": "stops.stop_code",
            "target_table": "stops", "target_pattern": "", "confidence": 0.8,
            "reasoning": "same stop"}], "value_mappings": []})
    world.mp.setattr(oa, "bedrock_call", _answer)


def _chat_until_resolved_with_a_rule(world) -> list:
    _peer(world)
    world.share_sampled_values()
    prompts: list = []
    _echoing_model(world, prompts)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert resolved.get("total_rules") == 1, resolved
    return prompts


def test_the_resolve_prompt_lists_each_field_once(world):
    prompts = _chat_until_resolved_with_a_rule(world)
    listed = prompts[0].split("has these concept mappings:\n", 1)[1].split("\n\n", 1)[0]
    lines = [line.strip().split(" -> ")[0] for line in listed.splitlines() if line.strip()]
    assert lines == [f"get_tags.{c['name']}" for c in TABLES[0]["columns"]], lines
    assert "get_tags.get_tags." not in prompts[0]


def test_a_rule_in_the_prompts_spelling_provisions_an_unchanged_schema(world):
    _chat_until_resolved_with_a_rule(world)
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    written = [p for c, p in world.writes if "MERGE (ir:IdentityRule" in c]
    assert [p["src_field"] for p in written] == [DOWN]
    assert written[0]["tgt_sys"] == TARGET


def test_a_rule_on_a_field_removed_since_still_refuses(world):
    _chat_until_resolved_with_a_rule(world)
    tables = copy.deepcopy(TABLES)
    tables[0]["columns"] = [c for c in tables[0]["columns"] if c["name"] != DOWN]
    world.discover_tables = tables
    before = len(world.writes)
    res = _chat_provision_answer()
    assert res.get("reason") == "schema_changed", res
    assert res["rules_not_found"] == [f"get_tags.{DOWN}"]
    assert len(world.writes) == before and world.job_bodies == []


@pytest.mark.parametrize("spelling", [
    f"get_tags.get_tags.{DOWN}", f"get_tags.{DOWN}", DOWN,
], ids=["table-twice", "qualified", "column"])
def test_the_rule_check_reads_a_field_as_the_writer_does(spelling, monkeypatch):
    from types import SimpleNamespace
    from agents.onboard_chat_lifecycle import _rule_sources_missing
    rule = {"concept": "downtime", "source_field": spelling, "source_table": "get_tags",
            "target_system": TARGET, "target_field": "stop_code"}
    assert _rule_sources_missing([rule], TABLES) == []
    gone = [{**TABLES[0], "columns": [c for c in TABLES[0]["columns"] if c["name"] != DOWN]}]
    assert _rule_sources_missing([rule], gone) == [spelling]
    # The provision writer stores the same column for that rule.
    written: list = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: written.append(params) or True)
    ctx = SimpleNamespace(system_id=SID, committable=set(), rules_written=0, nodes=0)
    onboard._dp_write_identity_rule(ctx, dict(rule), "", sample_check_passed=False, fanout_k=1)
    assert written[0]["src_field"] == DOWN
