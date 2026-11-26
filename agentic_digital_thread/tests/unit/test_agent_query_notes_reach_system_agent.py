# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Column meanings reach the system agent that writes the SQL.

A test event's answers valued goods received from a set of batches with the
invoice header total (tax included) instead of the order line value, and
dated a work order's deferral by the day the order was raised. Both are
column meanings, and both matter to the agent that writes the SQL, not to
the orchestrator (which never sends column names to a system agent).

Where registration ``query_guidance`` goes: the System node carries it and
the route seed delivers it to the ORCHESTRATOR as a fenced QUERY HINTS
section. The system agent never sees it: its per-question guidance store
holds only steward-delivered notes (``guidance_scope.delivery_document``
strips the base), and a PostgreSQL agent's provision prompt is generated from
its table list alone. The workshop's route seed also has no room left: a
sentence added to a workshop system's ``query_guidance`` cuts the plant
systems' own guidance and the identifier owners block
(``test_route_seed_workshop_budget``).

So a manifest entry carries ``agent_query_notes``, and provisioning a
PostgreSQL system appends them to that agent's prompt, fenced as the same
advisory QUERY HINTS block the agent reads for delivered notes. A user's own
registration under a manifest id, or the same id on another schema, gets
none; a system without notes keeps its prompt byte-identical. Offline: no
AWS, no graph, no database.
"""
from __future__ import annotations

import importlib.util
import re
import types
from pathlib import Path

import pytest

import agents.onboard as onboard
import agents.system_agent as sa
from agents.connection_resolver import CONNECTION_ORIGIN_KEY, ORIGIN_DEMO, ORIGIN_USER
from agents.demo import systems as demo_systems
from agents.demo import workshop
from agents.models import ProvisionRequest

_ERP = next(s for s in demo_systems.DEMO_SYSTEMS if s["id"] == "erp")
_SITE_CMMS = next(s for s in workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS
                  if s["system_type"] == "CMMS")
_PLAIN = next(s for s in demo_systems.DEMO_SYSTEMS if s["id"] == "plm")

_ERP_MONEY = (
    "A shipment has no amount of its own",
    "A whole order line is worth its line_total (quantity x unit_price, before tax)",
    "are whole-document figures: subtotal plus tax (an order's also shipping)",
    "invoices.total_amount for an amount billed or invoiced",
    "read subtotal and tax_amount before describing what a header figure includes",
)


def test_erp_note_does_not_claim_every_header_total_adds_tax():
    # Header totals are plain stored columns; some rows carry no tax or shipping,
    # so the note states how a total is made up, not that every row was taxed.
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP.get("schema", "")))
    assert "total_amount adds tax" not in notes
    assert "figures that include tax" not in notes


def test_erp_note_names_only_columns_each_header_has():
    # invoices carry subtotal and tax_amount; only customer_orders carry
    # shipping_amount, so the note must not send an invoice query to it.
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP.get("schema", "")))
    assert "read its subtotal, tax_amount and shipping_amount" not in notes
    assert "invoices.shipping_amount" not in notes
    assert "read subtotal and tax_amount" in notes


# Replayed at the logged decision point (16 samples per arm, the runtime's own
# model), naming both header totals by column and saying not to use them as
# the value of goods shipped or received was observed to move the agent to
# the order-line value more often than the note's earlier "may include tax ...
# not the value of the goods" wording. The replay is a signal, not proof; the
# live run is the check.
_HEADER_TOTALS = (
    "invoices.total_amount and customer_orders.total_amount are whole-document figures",
    "Never use them as the value of goods shipped or received",
)


def test_erp_note_names_each_header_total_and_never_values_goods_with_it():
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP.get("schema", "")))
    for fragment in _HEADER_TOTALS:
        assert fragment in notes, fragment


# A header total is the right figure when the question asks for an order's
# total or an amount billed: the open-orders golden asks for each order's
# "total amount", and the Daily Briefing's open-orders section ranks orders by
# "highest total value" with a Total column. The note must not ban those.
_HEADER_TOTAL_USES = (
    "Use customer_orders.total_amount for an order's total or total value",
    "invoices.total_amount for an amount billed or invoiced",
)


def test_erp_note_keeps_header_totals_for_order_totals_and_amounts_billed():
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP.get("schema", "")))
    for fragment in _HEADER_TOTAL_USES:
        assert fragment in notes, fragment
    assert "received or ordered" not in notes
    assert "total_amount only for" not in notes


# Rehearsal account, 2026-10-17: the orchestrator asked the ERP agent for "the
# monetary value (price/amount) of that shipment", and the agent summed each
# shipment's invoice total_amount (tax included). Replayed offline at that
# decision point, the agent chose a header total 8 of 8 times with the note's
# first wording and 8 of 8 without it: "the value of goods ordered or shipped"
# did not reach a question about a shipment's value. Every delegation in that
# run that asked for the value of the parts was answered from the order line.
_SHIPMENT_VALUE = (
    "A shipment has no amount of its own",
    "The value of a shipment, or of the parts a customer received in it",
    "the sum of shipment_items.quantity_shipped x order_line_items.unit_price",
    "joining each shipment item to its order line on line_item_id",
)


def test_erp_note_values_a_shipment_from_its_order_lines():
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP.get("schema", "")))
    for fragment in _SHIPMENT_VALUE:
        assert fragment in notes, fragment


_REPO_ROOT = Path(__file__).resolve().parents[3]
_ERP_DDL = _REPO_ROOT / "lambda-db-init" / "handler.py"


def _load_db_init_handler():
    """lambda-db-init has a dash in its directory name, so load it by path."""
    spec = importlib.util.spec_from_file_location("adt_db_init_handler_qnotes", _ERP_DDL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


erp_db_init = _load_db_init_handler()


def _ddl_body(table: str) -> str:
    """The column list of ``table`` in the ERP database the demo provisions."""
    text = _ERP_DDL.read_text()
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", text, re.S)
    assert match, table
    return match.group(1)


def _ddl_column_line(table: str, column: str) -> str:
    """The one DDL line that declares ``column`` of ``table``, whitespace folded."""
    lines = [line.strip() for line in _ddl_body(table).splitlines()
             if line.strip().split(" ", 1)[0] == column]
    assert len(lines) == 1, (table, column, lines)
    return " ".join(lines[0].split())


def _ddl_columns(table: str) -> set[str]:
    """Column names of ``table`` in the ERP database the demo provisions."""
    cols = set()
    for line in _ddl_body(table).splitlines():
        word = line.strip().split(" ", 1)[0]
        if word and word.isidentifier() and word.upper() not in ("UNIQUE", "PRIMARY", "CHECK"):
            cols.add(word)
    return cols


def test_erp_note_columns_are_the_erp_schema_columns():
    # The note states column meanings; each one it names must be a column of the
    # table it names, and "a shipment has no amount" must stay true.
    shipments = _ddl_columns("shipments")
    assert not {c for c in shipments if re.search(r"amount|total|value|price", c)}
    assert {"line_item_id", "quantity_shipped"} <= _ddl_columns("shipment_items")
    assert {"line_item_id", "quantity", "unit_price", "line_total"} <= _ddl_columns("order_line_items")
    assert {"subtotal", "tax_amount", "total_amount"} <= _ddl_columns("invoices")
    assert "shipping_amount" not in _ddl_columns("invoices")
    assert {"subtotal", "tax_amount", "shipping_amount", "total_amount"} <= _ddl_columns("customer_orders")


def test_erp_note_join_and_line_value_hold_in_the_erp_schema():
    # The note joins a shipment item to its order line on line_item_id and
    # reads quantity x unit_price as the line's value. Both must stay true of
    # the schema the agent queries, not only the column names.
    assert "REFERENCES order_line_items(line_item_id)" in _ddl_column_line(
        "shipment_items", "line_item_id")
    assert "GENERATED ALWAYS AS (quantity * unit_price)" in _ddl_column_line(
        "order_line_items", "line_total")
    assert "unit_price" not in _ddl_columns("shipment_items")
    # The agent queries erp_data views: each of these tables must be exposed
    # one to one, so the view carries exactly the table's columns.
    views_sql = erp_db_init._erp_data_views_sql()
    for table in ("shipments", "shipment_items", "order_line_items", "invoices",
                  "customer_orders"):
        assert table in erp_db_init._ERP_DATA_VIEW_TABLES, table
        assert f"CREATE OR REPLACE VIEW erp_data.{table} AS SELECT * FROM public.{table};" \
            in views_sql, table


def test_erp_note_reaches_the_agent_whole():
    # The delivery cap shortens a long value; the whole note must survive it.
    entry = {**_ERP, CONNECTION_ORIGIN_KEY: ORIGIN_DEMO}
    block = _flat(onboard._agent_query_notes_block(entry, _ERP["id"]))
    notes = _flat(demo_systems.agent_query_notes(_ERP["id"], _ERP["schema"]))
    assert notes in block
_CMMS_DATES = (
    "A work order's reported_date is when it was raised",
    "later changes such as a deferral are dated by their work_order_log entry",
    "target_start is the original due date and sched_start the current schedule",
)
_CASES = [(_ERP, _ERP_MONEY), (_SITE_CMMS, _CMMS_DATES)]
_CASE_IDS = ["erp", "site-cmms"]


def _flat(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# The manifest texts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entry,fragments", _CASES, ids=_CASE_IDS)
def test_manifest_entry_states_the_column_meanings(entry, fragments):
    notes = _flat(demo_systems.agent_query_notes(entry["id"], entry["schema"]))
    for fragment in fragments:
        assert fragment in notes, fragment
    assert not any(ch.isdigit() for ch in notes) and "$" not in notes


@pytest.mark.parametrize("entry,fragments", _CASES, ids=_CASE_IDS)
def test_the_notes_stay_out_of_the_route_seed_guidance(entry, fragments):
    # query_guidance rides the route seed to the orchestrator; these must not.
    for fragment in fragments:
        assert fragment not in _flat(entry.get("query_guidance", ""))


def test_notes_follow_the_manifest_id_and_schema_only():
    assert demo_systems.agent_query_notes(_ERP["id"], "another_schema") == ""
    assert demo_systems.agent_query_notes("not-a-manifest-system", _ERP["schema"]) == ""
    assert demo_systems.agent_query_notes(_PLAIN["id"], _PLAIN["schema"]) == ""


# --------------------------------------------------------------------------- #
# The provision prompt carries them
# --------------------------------------------------------------------------- #
@pytest.fixture
def pg_start(monkeypatch):
    """Everything _dp_start_pg_agent does besides building the prompt."""
    started: list = []

    class _Mgr:
        _agents: dict = {}

        def start_agent(self, **kw):
            started.append(kw)
            return "mcp://agent"

    entries: dict = {}
    monkeypatch.setattr(onboard, "catalog", types.SimpleNamespace(get=entries.get))
    monkeypatch.setattr(onboard, "_agent_manager", _Mgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: None)
    monkeypatch.setattr(onboard, "bedrock_call", lambda *a, **k: "Generated prompt.")
    monkeypatch.setattr(onboard, "_dp_pg_tables_desc", lambda ctx: "tables")
    monkeypatch.setattr(onboard, "_pg_agent_conn_params", lambda ctx, schema: {"host": "h"})
    return types.SimpleNamespace(started=started, entries=entries)


def _ctx(entry: dict) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=entry["id"], system_name=entry["name"],
                            plant=entry.get("plant", ""), connection_type="PostgreSQL",
                            schema=entry["schema"])
    return onboard._ProvisionState(
        body=body, system_id=entry["id"], system_name=entry["name"],
        plant=entry.get("plant", ""), schema=entry["schema"], rules=[], mappings=[],
        connection_type="PostgreSQL", cm_items=[])


def _start(pg_start, entry: dict) -> str:
    pg_start.entries[entry["id"]] = {**entry, CONNECTION_ORIGIN_KEY: ORIGIN_DEMO}
    assert onboard._dp_start_pg_agent(_ctx(entry)) is None
    return pg_start.started[-1]["system_prompt"]


@pytest.mark.parametrize("entry,fragments", _CASES, ids=_CASE_IDS)
def test_provision_prompt_carries_the_notes(pg_start, entry, fragments):
    prompt = _start(pg_start, entry)
    assert prompt.startswith("Generated prompt.")
    flat = _flat(prompt)
    for fragment in fragments:
        assert flat.count(fragment) == 1, fragment
    # The same advisory fence the agent reads for steward-delivered notes.
    assert "----- BEGIN QUERY HINTS -----" in prompt
    assert prompt.rstrip().endswith("----- END QUERY HINTS -----")


def test_erp_agent_prompt_carries_the_money_sentence_ahead_of_the_engine_rules(pg_start):
    system_prompt = _start(pg_start, _ERP)
    composed = sa._compose_system_prompt(
        "erp", _ERP["name"], _ERP["schema"], system_prompt, "", "PostgreSQL")
    flat = _flat(composed)
    for fragment in _ERP_MONEY:
        assert flat.count(fragment) == 1, fragment
    # Engine rules come after the notes, so the notes cannot displace them.
    assert flat.index(_ERP_MONEY[0]) < flat.index("BUDGET (required)")


def test_a_system_without_notes_keeps_the_prompt_as_it_was(pg_start):
    assert _start(pg_start, _PLAIN) == "Generated prompt."


def test_a_user_registration_under_a_manifest_id_gets_no_notes():
    entry = {**_ERP, CONNECTION_ORIGIN_KEY: ORIGIN_USER}
    assert onboard._agent_query_notes_block(entry, _ERP["id"]) == ""
    assert onboard._agent_query_notes_block(None, _ERP["id"]) == ""
    demo = {**_ERP, CONNECTION_ORIGIN_KEY: ORIGIN_DEMO}
    assert _ERP_MONEY[0] in _flat(onboard._agent_query_notes_block(demo, _ERP["id"]))
