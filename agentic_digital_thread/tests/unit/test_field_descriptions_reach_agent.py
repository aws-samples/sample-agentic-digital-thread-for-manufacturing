# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a connector recorded each field as holding reaches the system agent.

Discovery records a field's type and, for some connectors, what the field
holds after the type (``text (when the ticket was logged)``). Provision gets
that text with every field it maps, from the connector-owned schema record,
and the graph keeps it on the field. An agent whose baked schema is prose (an
API system) sees no column types, so the described fields reach it in their
own section, and two times on one record (when it was raised, when the stop
it records began) reach it each with its own meaning.

Pinned here, on a schema named for nothing in the product:

1. ``schema_format.type_description`` reads the recorded meaning, and only
   that (a bare type, a precision or a nested type is no description);
2. provision keeps every described field (``onboard._field_description_items``)
   on its state, bounded, and EVERY agent start in provision attaches them to
   the agent's connection config;
3. the agent's prompt lists each described field with its description, and
   the key is popped so it never reaches a connection;
4. every connection type's prompt carries the FIELD MEANINGS rule: a field is
   read by its own description, and one time of a record is never given as
   another.

Fully offline: model and Agent are stubbed, no graph, no AWS.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import agents.onboard as onboard
import agents.system_agent as sa
from agents import field_descriptions, schema_format
from agents.models import ProvisionRequest

_LOGGED = "when the ticket was logged"
_HALTED = "when the press stopped running"

_MAPPINGS = [
    {"table": "Ticket", "field": "Ticket.opened_on", "concept": "event-time",
     "data_type": f"text ({_LOGGED})"},
    {"table": "Ticket", "field": "Ticket.halt_began", "concept": "skip",
     "data_type": f"text ({_HALTED})"},
    {"table": "Ticket", "field": "Ticket.rank", "concept": "skip", "data_type": "integer"},
    {"table": "Press", "field": "Press.bore", "concept": "skip", "data_type": "numeric(10,2)"},
]
#: In the canonical (table, column) order the record is kept in.
_ITEMS = [{"field": "Ticket.halt_began", "description": _HALTED},
          {"field": "Ticket.opened_on", "description": _LOGGED}]

_ALL_TYPES = ("PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)", "MCP Server",
              "REST API", "")
_RULE_HEADER = "FIELD MEANINGS (required):"
_SECTION_HEADER = "\n\nFIELD DESCRIPTIONS ("


# --------------------------------------------------------------------------- #
# 1. The recorded meaning of a field's type text
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("data_type, meaning", [
    (f"text ({_LOGGED})", _LOGGED),
    ("string or integer (the crew the plan is assigned to)", "the crew the plan is assigned to"),
    ("integer (state code: 0 = open, 7 = done)", "state code: 0 = open, 7 = done"),
    ("string \u2014 holds the line name", "holds the line name"),
    ("integer", ""),
    ("timestamp without time zone", ""),
    ("numeric(10,2)", ""),
    ("decimal (12, 4)", ""),
    ("timestamp(3) with time zone", ""),
    ("array(varchar)", ""),
    ("map(varchar, array(integer))", ""),
    ("string (partition key)", ""),
    ("", ""),
    (None, ""),
])
def test_type_description_reads_only_a_recorded_meaning(data_type, meaning):
    assert schema_format.type_description(data_type) == meaning


def test_type_description_is_one_line():
    assert schema_format.type_description("text (when it\nwas\tlogged)") == "when it was logged"


# --------------------------------------------------------------------------- #
# 2. Provision keeps them and every agent start attaches them
# --------------------------------------------------------------------------- #
def test_provision_keeps_every_described_field_and_only_those():
    assert onboard._field_description_items(_MAPPINGS) == _ITEMS


def test_provision_keeps_a_field_once_and_stays_bounded():
    many = [{"table": "T", "field": f"T.c{i}", "data_type": f"text (meaning number {i} " + "x" * 400 + ")"}
            for i in range(400)]
    items = onboard._field_description_items(many + many)
    assert len({it["field"] for it in items}) == len(items)
    assert all(len(it["description"]) <= field_descriptions.MAX_CHARS for it in items)
    assert sum(len(it["field"]) + len(it["description"]) for it in items) \
        <= field_descriptions.TOTAL_CHARS
    assert len(items) <= field_descriptions.MAX_ITEMS


def test_provision_state_carries_them(monkeypatch):
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda sid: [])
    body = ProvisionRequest(system_id="help-desk", system_name="Help desk",
                            connection_type="REST API", mappings=_MAPPINGS)
    ctx = onboard._make_provision_state(body)
    assert ctx.field_descriptions == _ITEMS
    cfg = onboard._with_concept_mappings({"base_url": "https://h.example"}, ctx.cm_items,
                                         ctx.field_descriptions)
    assert cfg["field_descriptions"] == _ITEMS and cfg["base_url"] == "https://h.example"
    assert onboard._with_concept_mappings(None, [], []) is None


def _call_args(source: str, name: str) -> list[str]:
    """The argument text of every call of ``name`` in ``source`` (its def excluded)."""
    out = []
    for match in re.finditer(rf"(?<!def ){re.escape(name)}\(", source):
        depth, i = 1, match.end()
        while depth and i < len(source):
            depth += {"(": 1, ")": -1}.get(source[i], 0)
            i += 1
        out.append(source[match.end():i - 1])
    return out


def test_every_agent_start_in_provision_attaches_them():
    """Each provision branch attaches the agent's records through
    ``_with_concept_mappings``; every call passes the described fields."""
    source = Path(onboard.__file__).read_text(encoding="utf-8")
    calls = _call_args(source, "_with_concept_mappings")
    assert len(calls) >= 6, calls
    for call in calls:
        assert "ctx.field_descriptions" in call, call


# --------------------------------------------------------------------------- #
# 3. The agent's prompt lists them with their descriptions
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


def _agent(monkeypatch, cfg):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    return sa.create_system_agent(
        system_id="help-desk", system_name="Help desk", schema="desk",
        system_prompt="You are the help desk agent.",
        tables_desc="Ticket(opened_on: text, halt_began: text, rank: integer)",
        conn_params={"host": "localhost", "dbname": "d", "user": "u", "password": "p"},
        connection_type="PostgreSQL", connection_config=cfg)


def test_both_times_reach_the_agent_each_with_its_own_description(monkeypatch):
    cfg = {"secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:conn",
           "field_descriptions": list(_ITEMS)}
    prompt = _agent(monkeypatch, cfg).system_prompt
    lines = prompt.splitlines()
    assert f"Ticket.opened_on: {_LOGGED}" in lines
    assert f"Ticket.halt_began: {_HALTED}" in lines
    assert prompt.count(_SECTION_HEADER) == 1 and prompt.count(_RULE_HEADER) == 1
    assert prompt.index(_SECTION_HEADER) < prompt.index(_RULE_HEADER)
    assert "field_descriptions" not in cfg     # popped: never a connection key


def test_no_described_field_means_no_section(monkeypatch):
    prompt = _agent(monkeypatch, {}).system_prompt
    assert _SECTION_HEADER not in prompt
    assert prompt.count(_RULE_HEADER) == 1


def test_the_section_keeps_each_line_to_its_own_field():
    section = sa._field_descriptions_prompt([
        {"field": "Ticket.opened_on", "description": "when it was\nlogged"},
        {"field": "Ticket.bad field", "description": "dropped: not one field token"},
        {"field": "Ticket.halt_began", "description": ""},
        "not a record",
    ])
    body = section.split(":\n", 1)[1].splitlines()
    assert body == ["Ticket.opened_on: when it was logged"]
    assert sa._field_descriptions_prompt([]) == ""
    assert sa._field_descriptions_prompt(None) == ""


def test_the_section_is_bounded_and_says_what_it_left_out():
    items = [{"field": f"T.c{i}", "description": "y" * 150} for i in range(400)]
    section = sa._field_descriptions_prompt(items)
    assert len(section) <= sa._FIELD_DESCRIPTIONS_PROMPT_CAP + 200
    assert "more fields not shown" in section


# --------------------------------------------------------------------------- #
# 4. The rule, on every connection type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_every_connection_type_reads_a_field_by_its_own_description(connection_type):
    prompt = sa._compose_system_prompt("help-desk", "Help desk", "desk", "", "", connection_type)
    assert prompt.count(_RULE_HEADER) == 1, connection_type
    rule = sa._field_meanings_prompt()
    assert rule in sa._policy_tail(connection_type)
    flat = " ".join(rule.split())
    assert "never give one as another" in flat
    assert "created" in flat and "started" in flat


def test_the_rule_names_no_field():
    rule = sa._field_meanings_prompt()
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", rule)
    assert not re.search(r"\d", rule)
