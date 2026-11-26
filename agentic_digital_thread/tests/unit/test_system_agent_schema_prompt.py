# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schema baking + stop-re-discovery acceptance (highest-value Mode A latency fix).

The nested system agents dominated multi-system latency by RE-DISCOVERING their
schema on EVERY delegated sub-question (list_tables -> describe_table ->
run_query, repeated). The fix bakes the FULL per-table schema (column name + data
type) into the agent prompt and tells the agent NOT to re-discover it up front.

These are pure/offline assertions on the prompt-composition helpers — no Bedrock
model, AWS client, or DB connection is constructed.
"""
import pytest

import agents.system_agent as sa


# --------------------------------------------------------------------------- #
# format_tables_desc — the columns+types renderer used by provision + restore
# --------------------------------------------------------------------------- #
def test_format_tables_desc_emits_columns_and_types():
    out = sa.format_tables_desc(
        {
            "work_orders": [("wo_id", "varchar"), ("qty", "integer")],
            "runs": [("run_id", "uuid"), ("started_at", "timestamp")],
        }
    )
    assert "work_orders(wo_id: varchar, qty: integer)" in out
    assert "runs(run_id: uuid, started_at: timestamp)" in out
    # Tables are separated by ". "
    assert ". " in out


def test_format_tables_desc_names_only_when_type_blank():
    # A column with no data type (map-derived fallback) renders the bare name.
    out = sa.format_tables_desc({"t": [("a", ""), ("b", None), "c"]})
    assert out == "t(a, b, c)"


def test_format_tables_desc_marks_partition_via_type_suffix():
    # Athena partition keys are flagged by appending to the data type — generic,
    # no dataset literals.
    out = sa.format_tables_desc({"events": [("dt", "string (partition key)")]})
    assert "events(dt: string (partition key))" == out


# --------------------------------------------------------------------------- #
# (a) populated tables_desc -> schema baked in AND 'do not re-discover' directive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "connection_type,discovery_marker,schema_header",
    [
        ("Athena", "athena_list_tables", "Available tables:"),
        ("PostgreSQL", "information_schema", "Available tables:"),
        # The baked SiteWise schema is model/property shaped, so it gets its own
        # header + directive (see test_sitewise_schema_seed.py for the full
        # seeding acceptance) — but the bake+forbid contract is the same.
        ("SiteWise (Direct)", "sitewise_list_assets", "Onboarded asset models and properties:"),
    ],
)
def test_populated_schema_bakes_and_forbids_rediscovery(connection_type, discovery_marker, schema_header):
    tables_desc = "work_orders(wo_id: varchar, qty: integer)"
    prompt = sa._compose_system_prompt(
        system_id="erp-x",
        system_name="ERP X",
        schema="erp_data",
        system_prompt="You are ERP X.",
        tables_desc=tables_desc,
        connection_type=connection_type,
    )
    # The schema itself is present.
    assert schema_header in prompt
    assert tables_desc in prompt
    # The explicit 'schema already provided — do NOT re-discover' directive is present
    assert "SCHEMA ALREADY PROVIDED" in prompt
    assert "Do NOT call" in prompt
    # It names this connection type's discovery tools/queries as what NOT to call.
    assert discovery_marker in prompt
    # Reconciled with SELF-RECOVERY (re-introspect only on a real error) — no contradiction.
    assert "SELF-RECOVERY" in prompt
    assert "EFFICIENCY" in prompt


def test_populated_schema_postgres_omits_the_discovery_fallback_nudge():
    """With a schema baked in, the PostgreSQL info_schema DISCOVERY nudge must NOT be
    emitted — otherwise it would contradict 'schema already provided'."""
    prompt = sa._compose_system_prompt(
        system_id="cmms-x",
        system_name="CMMS X",
        schema="cmms",
        system_prompt="",
        tables_desc="assets(asset_id: varchar)",
        connection_type="PostgreSQL",
    )
    assert "If you are unsure which tables exist, run:" not in prompt


# --------------------------------------------------------------------------- #
# (b) empty tables_desc -> the runtime-discovery fallback guidance survives
# --------------------------------------------------------------------------- #
def test_empty_schema_keeps_postgres_discovery_fallback():
    prompt = sa._compose_system_prompt(
        system_id="cmms-x",
        system_name="CMMS X",
        schema="cmms",
        system_prompt="",
        tables_desc="",
        connection_type="PostgreSQL",
    )
    # No baked schema -> the fallback that tells the agent HOW to discover is present.
    assert "SCHEMA ALREADY PROVIDED" not in prompt
    assert "information_schema.tables" in prompt
    assert "discover tables before saying a table does not exist" in prompt
    # Self-recovery / efficiency still apply.
    assert "SELF-RECOVERY" in prompt
    assert "EFFICIENCY" in prompt


def test_empty_schema_athena_relies_on_self_recovery_not_a_bogus_pg_nudge():
    """A non-PostgreSQL type with no baked schema must NOT get the PostgreSQL
    information_schema nudge (it has no such catalog) — it relies on SELF-RECOVERY."""
    prompt = sa._compose_system_prompt(
        system_id="lake-x",
        system_name="Lake X",
        schema="athena",
        system_prompt="Athena agent.",
        tables_desc="",
        connection_type="Athena",
    )
    assert "information_schema.tables" not in prompt
    assert "SCHEMA ALREADY PROVIDED" not in prompt
    assert "SELF-RECOVERY" in prompt


def test_mes_restriction_survives_with_baked_schema():
    """MES systems keep their table-restriction guidance alongside the baked schema."""
    prompt = sa._compose_system_prompt(
        system_id="mes-mil",
        system_name="MES Milwaukee",
        schema="mes_milwaukee",
        system_prompt="",
        tables_desc="production_runs(run_id: uuid)",
        connection_type="PostgreSQL",
    )
    assert "Available tables:" in prompt
    assert "SCHEMA ALREADY PROVIDED" in prompt
    assert "MES (Manufacturing Execution System) agent" in prompt
    # MES is PostgreSQL but must NOT get the generic info_schema discovery nudge.
    assert "If you are unsure which tables exist, run:" not in prompt
