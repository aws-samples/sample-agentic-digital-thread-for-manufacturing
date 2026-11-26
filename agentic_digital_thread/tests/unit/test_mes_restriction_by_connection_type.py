# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PostgreSQL MES table restriction follows the connection type, not the id.

The restriction names the seeded PostgreSQL MES tables and forbids querying ERP
tables. It describes a PostgreSQL schema, so a system reached any other way
(Tulip Tables API, REST, MCP, Athena, SiteWise) must not get it just
because its id contains "mes": such a system was told to use tables it does
not have and refused a confirmed join because the join's far end was an ERP
field. PostgreSQL MES systems keep the restriction exactly as before.
"""
import pytest

import agents.system_agent as sa
from agents import connection_types as ct
from agents.demo import systems as demo_systems
from agents.demo import workshop as ws

_TABLES = "production_runs(run_id: uuid, run_number: varchar)"
_NON_POSTGRES = (ct.TULIP, ct.REST_API, ct.MCP_SERVER, ct.ATHENA,
                 ct.SITEWISE_DIRECT, ct.SITEWISE_I3X)
_ERP_BAN = "NEVER query ERP tables"
_TABLE_LIST = "Your MES tables are:"


class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


def _compose(system_id, connection_type, tables_desc=_TABLES):
    return sa._compose_system_prompt(
        system_id=system_id, system_name="Cell Line", schema="line_x",
        system_prompt="", tables_desc=tables_desc, connection_type=connection_type)


def test_a_tulip_mes_agent_gets_no_postgres_table_list_or_erp_ban(monkeypatch):
    """The agent that runs for a Tables API system registered under a mes id."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    agent = sa.create_system_agent(
        system_id="mes-x", system_name="Cell Line", schema="",
        system_prompt="Base system prompt.", tables_desc="",
        conn_params=None, connection_type=ct.TULIP,
        connection_config={"base_url": "https://your-instance.tulip.co",
                           "auth_scheme": "basic"})
    prompt = agent.system_prompt
    assert _ERP_BAN not in prompt
    assert _TABLE_LIST not in prompt
    assert sa._mes_restriction_prompt() not in prompt


@pytest.mark.parametrize("connection_type", _NON_POSTGRES)
@pytest.mark.parametrize("tables_desc", ["", _TABLES])
def test_a_non_postgres_mes_prompt_has_no_postgres_mes_restriction(connection_type,
                                                                   tables_desc):
    prompt = _compose("mes-x", connection_type, tables_desc)
    assert _ERP_BAN not in prompt, connection_type
    assert _TABLE_LIST not in prompt, connection_type
    assert sa._type_guidance("mes-x", "line_x", tables_desc, connection_type) == (
        sa._type_guidance("cell-x", "line_x", tables_desc, connection_type))


@pytest.mark.parametrize("connection_type", _NON_POSTGRES)
def test_a_non_postgres_prompt_does_not_depend_on_mes_in_the_id(connection_type):
    assert _compose("mes-x", connection_type) == _compose("cell-x", connection_type)


def _postgres_mes_manifest_systems():
    """Every PostgreSQL MES the demo and workshop manifests can register."""
    lists = [demo_systems.DEMO_SYSTEMS, ws.DEMO_PLANT_SYSTEMS]
    lists += [ws.registered_systems(story, highbyte_shipped=shipped)
              for story in ws.STORIES for shipped in (False, True)]
    found = {(s["id"], s.get("schema") or "") for entries in lists for s in entries
             if s.get("system_type") == "MES" and s.get("connection_type") == ct.POSTGRESQL}
    return sorted(found)


def test_the_manifests_hold_postgres_mes_systems_to_check():
    assert len(_postgres_mes_manifest_systems()) >= 3


@pytest.mark.parametrize("system_id,schema", _postgres_mes_manifest_systems())
@pytest.mark.parametrize("tables_desc", ["", _TABLES])
def test_every_postgres_mes_keeps_its_restriction_exactly(system_id, schema, tables_desc):
    guidance = sa._type_guidance(system_id, schema, tables_desc, ct.POSTGRESQL)
    assert guidance == sa._mes_restriction_prompt()


def test_a_postgres_mes_restriction_sits_before_the_policy_tail(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose("mes-x", ct.POSTGRESQL)
    restriction = sa._mes_restriction_prompt()
    assert prompt.count(restriction) == 1
    assert restriction + sa._policy_tail(ct.POSTGRESQL) in prompt


def test_a_postgres_system_without_mes_in_its_id_keeps_the_discovery_fallback():
    guidance = sa._type_guidance("cell-x", "line_x", "", ct.POSTGRESQL)
    assert _ERP_BAN not in guidance
    assert "information_schema.tables" in guidance
