# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Athena agent is told the accurate rule about partition columns.

Measured on an analytics deployment: the SQL system agent added a bound on
the table's partition column (a creation date) next to the question's own
time window, because its registration prompt said "ALWAYS include partition
predicates in WHERE clauses to keep scans small" (the raw run tool's
docstring said the same, and the QUERY EFFICIENCY directive said to always
filter on partition columns). That bound drops rows and changes counts, and
an over-limit total no longer needs one: the connector splits it exactly.

The accurate rule (``storage_bound_rule.PARTITION_RULE``) replaces it in the
provision prompt, the tool docstring, the discovery summary and the QUERY
EFFICIENCY directive. Agents provisioned before the change keep the old
sentence baked into their stored prompt, so the system agent replaces it when
it composes its prompt at start-up, the way the scan guard corrects a stale
"(no partition keys)" bake at run time, with no re-provision. Offline.
"""
from __future__ import annotations

import agents.athena_tools as at
import agents.system_agent as sa

_OLD = "ALWAYS include partition predicates in WHERE clauses to keep scans small."
_BAKED = ("You are the Amazon Athena agent for System A. Query its tables with "
          "athena_list_tables, athena_describe_table, and athena_run_query (read-only SQL, "
          f"Presto/Trino dialect). {_OLD} Cite specific values.")


def _rule() -> str:
    from agents.storage_bound_rule import PARTITION_RULE
    return PARTITION_RULE


def _compose(system_prompt: str, connection_type: str = "Athena", tables_desc: str = "") -> str:
    return sa._compose_system_prompt(
        system_id="system-a", system_name="System A", schema="lake_db",
        system_prompt=system_prompt, tables_desc=tables_desc,
        connection_type=connection_type)


def test_the_rule_says_to_bound_the_questions_own_time_column():
    rule = _rule()
    assert "question asks about" in rule
    assert "only when the question asks for that column's range" in rule
    assert "computed exactly by the tool" in rule
    assert "\u2014" not in rule and "\u2013" not in rule


def test_a_baked_registration_prompt_is_corrected_when_the_agent_starts():
    prompt = _compose(_BAKED)
    assert _OLD not in prompt
    assert _rule() in prompt
    assert prompt.startswith("You are the Amazon Athena agent for System A.")


def test_only_the_athena_baked_sentence_is_replaced():
    other = "You are the agent for System B. Cite specific values."
    corrected = _compose(_BAKED)
    assert corrected == _compose(_BAKED.replace(_OLD, _rule()))
    postgres = sa._compose_system_prompt(
        system_id="system-b", system_name="System B", schema="schema_b",
        system_prompt=f"{other} {_OLD}", tables_desc="t(a: int)", connection_type="PostgreSQL")
    assert _OLD in postgres and _rule() in corrected


def test_the_provision_prompt_carries_the_rule():
    from agents import onboard
    prompt = onboard._athena_system_prompt("System A")
    assert _OLD not in prompt and _rule() in prompt
    assert prompt.startswith("You are the Amazon Athena agent for System A.")


def test_the_raw_run_tool_docstring_carries_the_rule():
    tools = {t.tool_name: t for t in at.make_athena_tools(
        {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"})}
    description = " ".join(tools["athena_run_query"].tool_spec["description"].split())
    assert "Include partition predicates in WHERE clauses to keep scans small" not in description
    assert _rule() in description


def test_the_query_efficiency_directive_no_longer_says_always_filter_on_partitions():
    desc = "events(dt: date (partition key), qty: int)"
    efficiency = sa._athena_query_efficiency_prompt(desc)
    generic = sa._athena_query_efficiency_prompt("")
    for text in (efficiency, generic):
        assert "ALWAYS constrain the scan with a WHERE predicate on the table's PARTITION" not in text
        assert "to prune the scan" not in text
        assert _rule() in text
    assert "Known partition columns" in efficiency


def test_the_discovery_summary_carries_the_rule(monkeypatch):
    from agents import onboard_agent
    monkeypatch.setattr(at, "run_athena_query", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard_agent, "_athena_discover_cfg", lambda *_a, **_k: {
        "workgroup": "wg", "database": "lake_db", "region": "us-east-1"})
    out = onboard_agent._discover_athena_schema(
        "system-a", None, None, {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"})
    assert "always include partition predicates" not in out["ai_analysis"]
    assert _rule() in out["ai_analysis"]
