# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The engine's form crossing on the recorded workshop shapes.

The crossing itself is pinned on a neutral schema in ``test_form_crossing``.
This file replays the recorded shapes it must handle and the ones it must
leave alone, with the forms facts of ``test_identifier_forms`` (the two exact
batch rules and the source's work-order to run key join) and of
``test_measured_reference_joins`` (the same key join as onboarding writes it,
measured or a bare name guess), and the ERP call recorded in
``fixtures/system_agent_unreturned_names_exact6.json``:

* the holder's first answer is the recorded ERP answer with the engine line
  and block that today's ``unresolved_miss`` writes for the recorded calls
  (the shape guard's own refusal of the run value in the lot-tracking table,
  then the rows that never pair it). The last recorded call is left out: it
  is a refusal kind an older guard wrote, which today's guard does not;
* the MES answers from its rows, one pair of which shares no digits, and the
  ERP answers the re-ask from its shipment lines for the run values the
  question carries. The crossing path then gives four customers, 110 units and
  $37,400, which only the MES rows' pairs reach (a pairing made from digits
  would miss one batch: 90 units, $30,600);
* left byte-identical: the recorded answer with no crossing signal, the
  module 2 step 4 answer that already reached every customer, an ERP answer
  to work-order values that came back as run rows (module 7), a lookup of
  another system's run values (8c), and a key join that is only a name guess;
  a run value the MES does not hold leaves the ERP answer as it was;
* the pairing system's concept check on the four MES columns' recorded
  mappings (``fixtures/concept_mappings_workshop_systems.json``): all four
  seats pass, the one where onboarding left a key column unmapped by its
  join partner's mapping, and so do a fresh seat's ``mes-ind`` columns
  (test event 2026-11-11), where the same key column was unmapped.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

import agents.identifier_provenance as ip
import agents.orchestrator as orch
import agents.route_facts as rf
import agents.unresolved_miss as um
from agents import form_pairing as fp
from agents import identifier_shape_guard as g
from agents import schema_format
from agents.call_delivery import CallDelivery
from agents.call_delivery import record as record_delivery
from tests.unit import test_identifier_forms as tif

RESERVED = fp.RESERVED_ARG
ERP, MES = "erp-a", "mes-a"
ERP_TOOL, MES_TOOL = "erp-a___query_erp_a", "mes-a___query_mes_a"
FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                      / "system_agent_unreturned_names_exact6.json").read_text())
RULES = [tif.RUN_RULE, tif.WO_RULE]
MES_FIELDS = ["work_orders.work_order_number", "production_runs.run_number"]

#: The ERP view tables the recorded calls read, and the measured pair the
#: shape guard holds for them (onboarding's measurement, as the schema notes it).
ERP_TABLES = {
    "lot_mes_tracking": [("batch_id", "character varying"), ("lot_id", "uuid"),
                         ("site", "character varying"), ("line", "character varying"),
                         ("quantity_consumed", "numeric"), ("unit_of_measure", "text"),
                         ("consumption_date", "timestamp")],
    "shipment_items": [("shipment_item_id", "uuid"), ("shipment_id", "uuid"),
                       ("batch_id", "character varying"), ("line_item_id", "uuid"),
                       ("product_id", "uuid"), ("quantity_shipped", "integer")],
    "shipments": [("shipment_id", "uuid"), ("shipment_date", "timestamp"), ("order_id", "uuid"),
                  ("customer_id", "uuid"), ("shipment_number", "text")],
    "order_line_items": [("line_item_id", "uuid"), ("line_total", "numeric"),
                         ("unit_price", "numeric")],
    "material_lots": [("lot_id", "uuid"), ("lot_number", "text"), ("item_id", "uuid"),
                      ("supplier_id", "uuid"), ("received_quantity", "numeric"),
                      ("remaining_quantity", "numeric"), ("unit_of_measure", "text"),
                      ("current_location_id", "uuid")],
    "inventory_items": [("item_id", "uuid"), ("item_code", "text"), ("item_name", "text")],
    "suppliers": [("supplier_id", "uuid"), ("company_name", "text")],
    "customers": [("customer_id", "uuid"), ("company_name", "text")],
}
MEASURED_PAIR = {"concept": "batch", "column": "batch_id", "table_a": "lot_mes_tracking",
                 "table_b": "shipment_items", "read_a": 939, "read_b": 500,
                 "shape_a": "WO-99999999-99999", "shape_b": "RUN-99999999-99999"}


def _first_answer() -> str:
    """The recorded ERP answer with the engine line and block today's
    ``unresolved_miss`` carries for the recorded calls."""
    from agents.response_integrity_sql import parse_physical_schema
    desc = schema_format.with_distinct_identifiers(
        schema_format.format_tables_desc(ERP_TABLES), [MEASURED_PAIR])
    context, physical, state = g.context_from(desc), parse_physical_schema(desc), {}
    for call in FIXTURE["tool_calls"][:-1]:
        um.record_result(state, {"status": "success", "content": [{"text": call["result"]}]},
                         context, physical)
    fact = um.engine_fact(FIXTURE["answer"], state, FIXTURE["question"])
    assert fact.startswith(um.MARKER) and "token: RUN-20261011-90001" in fact
    return f"{FIXTURE['answer']}\n\n{fact}"


#: The MES pairs per item. One pair's numbers share no digits: only the rows
#: say which run a work order built.
MES_ROWS = [("WO-20261010-92001", "RUN-20261010-90777"),
            ("WO-20261010-92004", "RUN-20261010-92004"),
            ("WO-20261011-90001", "RUN-20261011-90001"),
            ("WO-20261011-91002", "RUN-20261011-91002"),
            ("WO-20261011-91005", "RUN-20261011-91005")]
#: The ERP's shipment lines per run (customer, units); $340 a unit.
SHIPMENTS = {"RUN-20261011-90001": [("Drivetrain Customer", 12), ("Aerospace Customer", 10),
                                    ("Precision Customer", 8)],
             "RUN-20261010-90777": [("Precision Customer", 20)],
             "RUN-20261010-92004": [("Precision Customer", 20)],
             "RUN-20261011-91002": [("Aerospace Customer", 20)],
             "RUN-20261011-91005": [("Gear Customer", 20)]}
PRICE = 340
_PAIR_LINE = re.compile(r"^- (WO-\S+) = (RUN-\S+)$", re.MULTILINE)


def erp_reask(question: str) -> dict:
    """The ERP's answer to a re-ask: the shipment lines of the run values the
    question pairs, by customer, with the total."""
    runs = [run for _wo, run in _PAIR_LINE.findall(question)]
    units: dict = {}
    for run in runs:
        for customer, n in SHIPMENTS.get(run, []):
            units[customer] = units.get(customer, 0) + n
    rows = "\n".join(f"| {c} | {n} | ${n * PRICE:,} |"
                     for c, n in sorted(units.items(), key=lambda kv: -kv[1]))
    total = sum(units.values())
    text = (f"Shipments for the {len(runs)} batches that drew the lot, by customer:\n\n"
            f"| Customer | Units | Value |\n|---|---|---|\n{rows}\n\n"
            f"Total: {total} units, ${total * PRICE:,}.")
    return _result(text)


def closure(rows, values):
    asked = set(values)
    matched = [r for r in rows if r[0] in asked or r[1] in asked]
    left, right = {r[0] for r in matched}, {r[1] for r in matched}
    return [list(r) for r in rows if r[0] in left or r[1] in right]


def _result(text, status="success"):
    return {"status": status, "toolUseId": "t", "content": [{"text": text}]}


class _Gateway:
    """The ERP answers its first call from ``first`` and a re-ask from its
    shipment lines; the MES answers pairing requests from its rows."""

    def __init__(self, first, rows=MES_ROWS):
        self.first, self.rows = first, list(rows)
        self.calls, self.lock = [], threading.Lock()
        self.list_tools_sync = lambda *a, **k: []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            record_delivery(CallDelivery.SENT)
            with self.lock:
                self.calls.append((name, dict(arguments or {})))
                erp_calls = sum(1 for n, _a in self.calls if n == ERP_TOOL)
            if RESERVED in (arguments or {}):
                values = json.loads(arguments[RESERVED])["values"]
                return _result(json.dumps({
                    "engine": "pair_forms", "v": 2, "status": "ok", "fields": MES_FIELDS,
                    "pairs": closure(self.rows, values), "cut": False, "dropped": 0}))
            if name == ERP_TOOL:
                return self.first if erp_calls == 1 else erp_reask(arguments["question"])
            return _result("No further rows.")

        self.call_tool_async = _call


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    rf.reset_turn()
    ip.reset_turn_facts()
    yield
    rf.reset_turn()
    ip.reset_turn_facts()


def _turn(jps=tif.JPS, question=None):
    rows = [{"system_id": s,
             "identity_rules": [r for r in RULES if s in (r["source_system"], r["target_system"])],
             "join_paths": [j for j in jps if s in (j["from_system"], j["to_system"])],
             "value_mappings": []} for s in (ERP, MES)]
    ip.remember_turn_facts(question or FIXTURE["question"], rows, orch._ROUTE_SEED_RULE_CAP)


def _delegate(first, rows=MES_ROWS, question=None, **turn):
    _turn(**turn)
    gateway = _Gateway(first, rows)
    client = orch._wrap_gateway_client_with_delegation_context(gateway)
    result = asyncio.run(client.call_tool_async(
        "t1", ERP_TOOL, {"question": question or FIXTURE["question"]}))
    return gateway, result


def _names(gateway):
    return [name for name, _ in gateway.calls]


def _text(result):
    return "\n".join(b["text"] for b in result["content"])


def test_the_recorded_erp_answer_is_crossed_at_the_mes_to_every_customer():
    gateway, result = _delegate(_result(_first_answer()))
    assert _names(gateway) == [ERP_TOOL, MES_TOOL, ERP_TOOL]
    request = json.loads(next(a for _n, a in gateway.calls if RESERVED in a)[RESERVED])
    assert request["join"] == tif.PAIR_JP["id"]
    assert sorted(request["values"]) == sorted([wo for wo, _run in MES_ROWS]
                                               + [FIXTURE["batch"]])
    reask = [a for n, a in gateway.calls if n == ERP_TOOL][-1]["question"]
    assert "lot_mes_tracking.batch_id = shipment_items.batch_id:" in reask
    for wo, run in MES_ROWS:
        assert f"- {wo} = {run}" in reask
    assert "RUN-20261010-92001" not in reask              # never made from digits
    text = _text(result)
    for row in ("| Precision Customer | 48 | $16,320 |", "| Aerospace Customer | 30 | $10,200 |",
                "| Gear Customer | 20 | $6,800 |", "| Drivetrain Customer | 12 | $4,080 |"):
        assert row in text
    assert "Total: 110 units, $37,400." in text
    assert "[engine] Identifier forms crossed:" in text


def test_the_recorded_erp_answer_without_a_crossing_signal_is_byte_identical():
    original = _result(FIXTURE["answer"])
    gateway, result = _delegate(original)
    assert _names(gateway) == [ERP_TOOL] and result is original


#: Module 2 step 4 on a passing run: every batch's customers came back.
STEP4_PASS = ("Four customers received parts from batches made with that lot:\n\n"
              "| Customer | Units | Value |\n|---|---|---|\n"
              "| Customer A | 48 | $16,320 |\n| Customer B | 30 | $10,200 |\n"
              "| Customer C | 20 | $6,800 |\n| Customer D | 12 | $4,080 |\n\n"
              "That is 110 units and $37,400 in total, from batches RUN-20261010-92001, "
              "RUN-20261010-92004, RUN-20261011-90001, RUN-20261011-91002 and RUN-20261011-91005.")


@pytest.mark.parametrize("question, answer", [
    (tif.QUESTION, STEP4_PASS),
    # Module 7: work-order values sent to the ERP come back as run rows.
    (("Which shipment lines hold the batches of work orders WO-20261010-92001 and "
      "WO-20261010-92004?"),
     "| batch_id | quantity |\n|---|---|\n| RUN-20261010-92001 | 12 |\n| RUN-20261010-92004 | 20 |"),
    # 8c: another system's run values looked up in the ERP shipment lines.
    ("Do batches RUN-20261106-00311 and RUN-20261106-00319 have ERP shipment lines?",
     ("RUN-20261106-00311 has 3 shipment lines. No record of RUN-20261106-00319 in "
      "shipment_items.batch_id yet.")),
])
def test_answers_that_already_pass_are_byte_identical(question, answer):
    original = _result(answer)
    gateway, result = _delegate(original, question=question)
    assert _names(gateway) == [ERP_TOOL] and result is original


def test_a_run_value_the_mes_does_not_hold_leaves_the_erp_answer_as_it_was():
    """A lookup of values the MES rows do not hold (another system's runs):
    the pairing finds no counterpart, so the ERP answer goes back unchanged."""
    answer = _result("No lot record for RUN-20261106-00311.\n\n[engine] declined: "
                     "reason_kind=not_connected; concept=batch")
    gateway, result = _delegate(answer, question="Which lot did batch RUN-20261106-00311 draw?")
    assert _names(gateway) == [ERP_TOOL, MES_TOOL] and result is answer


def _onboarded_joins(monkeypatch, measured: bool):
    from tests.unit import test_measured_reference_joins as tmr
    row = (lambda sql: tmr._row()) if measured else (
        lambda sql: tmr._row(missing=3) if "production_runs" in sql else tmr._row())
    return tmr._written_mes_joins(monkeypatch, row)


def test_the_key_join_as_onboarding_measures_it_is_crossed(monkeypatch):
    joins = _onboarded_joins(monkeypatch, measured=True)
    run_join = next(j for j in joins if j["to_field"].endswith("production_runs.production_run_id"))
    gateway, result = _delegate(_result(_first_answer()), jps=[*tif.JPS[:2], *joins])
    joins_named = {json.loads(a[RESERVED])["join"] for _n, a in gateway.calls if RESERVED in a}
    assert joins_named == {run_join["id"]}
    assert "Total: 110 units, $37,400." in _text(result)


def test_a_key_join_the_data_contradicts_is_never_crossed(monkeypatch):
    joins = _onboarded_joins(monkeypatch, measured=False)
    original = _result(_first_answer())
    gateway, result = _delegate(original, jps=[*tif.JPS[:2], *joins])
    assert _names(gateway) == [ERP_TOOL] and result is original


# --------------------------------------------------------------------------- #
# The four MES columns' recorded concept mappings (the pairing system's check)
# --------------------------------------------------------------------------- #
MAPPINGS = json.loads((Path(__file__).resolve().parent / "fixtures"
                       / "concept_mappings_workshop_systems.json").read_text())["systems"]
#: The pairing read's two form endpoints on the recorded MES schema, with the
#: concepts their exact joins to the ERP's batch columns record (the exact
#: pass agrees them through catalog-related pairs, ``test_exact_affinity_pg``),
#: and the two columns of the work-order to run key join.
MES_ENDPOINTS = (
    fp.Endpoint("work_orders", "work_order_number", "AA-99999999-99999", "batch", "work-order"),
    fp.Endpoint("production_runs", "run_number", "AAA-99999999-99999", "batch",
                "production-run"))
MES_KEYS = (("work_orders", "production_run_id"), ("production_runs", "production_run_id"))
MES_COLUMNS = (*((e.table, e.column) for e in MES_ENDPOINTS), *MES_KEYS)


def _stored(system_id):
    """The recorded mappings as ``form_pairing.CONCEPTS_CYPHER`` returns them."""
    return [{"field": f"{system_id}.{m['field']}".lower(), "concept": m["concept"],
             "role": m["role"], "retired": False} for m in MAPPINGS[system_id]
            if tuple(m["field"].split(".")) in MES_COLUMNS]


@pytest.mark.parametrize("system_id", ["mes-ind", "mes-mil", "mes-cr"])
def test_the_recorded_mes_columns_pass_the_concept_check(system_id):
    """On the allowlist, and each endpoint mapped exactly to its form's concept."""
    from agents import catalog
    assert fp.concept_check(_stored(system_id), system_id, MES_ENDPOINTS, MES_KEYS) is None
    assert not any(catalog.person_valued_field(c) for _t, c in MES_COLUMNS)


def test_a_seat_where_onboarding_left_a_key_column_unmapped_passes_by_its_partner():
    """The recorded ``mes-det`` seat stored no mapping for
    ``production_runs.production_run_id``; the key column takes the mapping of
    its join partner ``work_orders.production_run_id``, so the check passes."""
    stored = _stored("mes-det")
    assert "mes-det.production_runs.production_run_id" not in {r["field"] for r in stored}
    assert fp.concept_check(stored, "mes-det", MES_ENDPOINTS, MES_KEYS) is None


#: The four MES columns' mappings as test event 2026-11-11 Team 1's graph held
#: them (a fresh Create account, read with the module 7 query shape): the
#: ``mes-ind`` surrogate ``production_runs.production_run_id`` has no mapping.
#: Every pairing read on that seat refused ``concept_unknown`` before this
#: inheritance, so the crossing never ran (9 of 9 rounds void).
FRESH_IND = [{"field": f"mes-ind.{f}", "concept": c, "role": "identifier", "retired": False}
             for f, c in (("work_orders.work_order_number", "work-order"),
                          ("production_runs.run_number", "production-run"),
                          ("work_orders.production_run_id", "production-run"))]


def test_a_fresh_seats_mes_columns_pass_the_concept_check():
    assert fp.concept_check(FRESH_IND, "mes-ind", MES_ENDPOINTS, MES_KEYS) is None
