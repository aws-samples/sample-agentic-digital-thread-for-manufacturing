# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A total across listed record keys keeps a figure per key, or says it has none.

Measured on a rehearsal account (2026-10-15, five fresh four-question chats).
Q1 asked which batches one customer's shipments came from: its rows were
filtered to that customer, so they paired the failed batch with that one
customer only. Q4 asked which customers got parts from any of the five
batches made from the same lot. On two of the five runs the orchestrator's
delegated ERP question asked only for a total per customer across the five
listed batch keys. The ERP agent filtered on the five keys
(``batch_id IN (...)``), grouped by customer alone and answered with four
customer totals. Nothing in that result said which batch a total came from,
so the orchestrator took the batch-to-customer pairing from Q1's
customer-filtered rows and wrote that only the complaining customer received
the failed batch, when three customers did. On the three runs whose
delegated question asked per batch and per customer, the caveat was right.

The fix is on the system agent's side of the query boundary, both halves
deterministic and generic:

1. A delegated question that asks for a total, sum, count or value and lists
   two or more record keys of one namespace (the same letters around the
   digits, e.g. two ``RUN-`` numbers) carries ONE fixed engine line asking
   that every total across them also be given per key. Keys of different
   namespaces (a customer id and an order id), a question with no aggregate
   word, and keys that appear only in guidance or graph context do not count.
2. When a query this call aggregated rows across two or more listed record
   keys without grouping by their column, the answer carries ONE fixed engine
   line unless it gives, for every one of those keys, a figure that the
   call's own rows prove (the key's own aggregate, or each of its rows on a
   line of its own). When that query grouped by other columns (totals per
   customer), the figure must be per key AND group: a line naming the key
   and the customer carrying that pair's figure, from rows that hold both.
   Anything not proven counts as missing. The wording follows the operator
   and names the group column.

The four-turn regression runs the real orchestrator hooks over a strands
agent with Q1 to Q3 in its memory, and checks what the orchestrator's model
is sent for Q4, for the Q4 shapes measured on 2026-10-15 (a table per
customer plus a table per batch: flagged; the batch-by-customer pairs: not
flagged) and that its system prompt carries the ENGINE FACTS and RECIPIENTS
rules.

Customer and product names are neutral placeholders; the batch keys, the SQL
and the answer shapes are the runs' own. Fully offline.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.chat_client as chat_client
import agents.orchestrator as orch
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import eval_core, query_intelligence as qi
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import smoke_prompts as smoke  # noqa: E402

_GOLDENS = (Path(__file__).resolve().parents[2] / "agents" / "demo"
            / "golden_evals.json")

_KEYS = ["RUN-20261010-92001", "RUN-20261010-92004", "RUN-20261011-90001",
         "RUN-20261011-91002", "RUN-20261011-91005"]
_IN_LIST = ",\n  ".join(f"'{key}'" for key in _KEYS)

# ---------------------------------------------------------------------------
# Run 4 (chat w9-8a9db55c8e, Q4): the delegated question, the three ERP
# statements as executed, and the ERP agent's answer.
# ---------------------------------------------------------------------------
RUN4_QUESTION = (
    "For production batches with the following identifiers (same batches, possibly "
    "stored as either work-order or run-number style IDs): WO-20261010-92001 / "
    "RUN-20261010-92001, WO-20261010-92004 / RUN-20261010-92004, WO-20261011-90001 / "
    "RUN-20261011-90001, WO-20261011-91002 / RUN-20261011-91002, WO-20261011-91005 / "
    "RUN-20261011-91005: find all shipments across ALL customers made from these "
    "batches. For each customer, give the total units shipped and the total value "
    "(price \u00d7 quantity) of those shipments. Then give the grand total units and "
    "grand total value across all customers and all five batches.")

RUN4_STATEMENTS = [
    ("SELECT \n  DISTINCT batch_id\nFROM shipment_items\nWHERE batch_id IN (\n"
     "  'WO-20261010-92001', 'RUN-20261010-92001',\n  'WO-20261010-92004', "
     "'RUN-20261010-92004',\n  'WO-20261011-90001', 'RUN-20261011-90001',\n  "
     "'WO-20261011-91002', 'RUN-20261011-91002',\n  'WO-20261011-91005', "
     "'RUN-20261011-91005'\n)\nORDER BY batch_id LIMIT 500"),
    ("SELECT\n  s.shipment_id,\n  s.customer_id,\n  c.company_name,\n  si.batch_id,\n"
     "  si.product_id,\n  si.quantity_shipped,\n  oli.unit_price,\n"
     "  (si.quantity_shipped * oli.unit_price) AS line_value\nFROM shipment_items si\n"
     "JOIN shipments s ON si.shipment_id = s.shipment_id\n"
     "JOIN customers c ON s.customer_id = c.customer_id\n"
     "JOIN order_line_items oli ON si.line_item_id = oli.line_item_id\n"
     f"WHERE si.batch_id IN (\n  {_IN_LIST}\n)\n"
     "ORDER BY si.batch_id, s.customer_id, s.shipment_id LIMIT 500"),
    ("SELECT\n  c.company_name,\n  SUM(si.quantity_shipped) AS total_units_shipped,\n"
     "  SUM(si.quantity_shipped * oli.unit_price) AS total_value\n"
     "FROM shipment_items si\nJOIN shipments s ON si.shipment_id = s.shipment_id\n"
     "JOIN customers c ON s.customer_id = c.customer_id\n"
     "JOIN order_line_items oli ON si.line_item_id = oli.line_item_id\n"
     f"WHERE si.batch_id IN (\n  {_IN_LIST}\n)\n"
     "GROUP BY c.customer_id, c.company_name\nORDER BY total_value DESC LIMIT 500"),
]

RUN4_ANSWER = (
    "Excellent! Now I have all the data needed. Here's the complete summary:\n\n"
    "## Shipments from Production Batches (" + ", ".join(_KEYS) + ")\n\n"
    "### Shipments by Customer\n\n"
    "| Customer | Total Units Shipped | Total Value |\n"
    "|----------|---------------------|-------------|\n"
    "| Example Corp Precision | 48 units | $16,320.00 |\n"
    "| Example Corp Aerospace | 30 units | $10,200.00 |\n"
    "| Example Corp Gear | 20 units | $6,800.00 |\n"
    "| Example Corp Drivetrain | 12 units | $4,080.00 |\n\n"
    "### Grand Totals (All Customers, All 5 Batches)\n"
    "- **Total Units Shipped: 110 units**\n"
    "- **Total Value: $37,400.00**\n\n"
    "The shipments span 4 distinct customers across the 5 production batches.")

# ---------------------------------------------------------------------------
# Run 5 (chat w9-2d2bd66698, Q4): question, the two aggregate statements that
# followed its detail reads, and the answer.
# ---------------------------------------------------------------------------
RUN5_QUESTION = (
    "For these production batch IDs: WO-20261010-92001, WO-20261010-92004, "
    "WO-20261011-90001, WO-20261011-91002, WO-20261011-91005: find all shipments "
    "made from these batches. For each customer that received parts from any of "
    "these batches, give the customer name, total units received, and total "
    "monetary value of those shipments. Also give the grand total units and grand "
    "total value across all customers.")

_RUN5_FROM = (
    "FROM shipment_items si\nJOIN shipments s ON si.shipment_id = s.shipment_id\n"
    "JOIN customers c ON s.customer_id = c.customer_id\n"
    "LEFT JOIN invoices inv ON s.shipment_id = inv.shipment_id\n"
    "LEFT JOIN invoice_line_items ili ON inv.invoice_id = ili.invoice_id "
    "AND si.product_id = ili.product_id\n"
    "WHERE si.batch_id IN (" + ", ".join(f"'{key}'" for key in _KEYS) + ")\n")

RUN5_STATEMENTS = [
    ("SELECT \n  si.batch_id,\n  si.shipment_id,\n  si.quantity_shipped,\n"
     "  s.customer_id,\n  c.company_name\n" + _RUN5_FROM
     + "ORDER BY si.batch_id, s.shipment_date, s.customer_id LIMIT 500"),
    ("SELECT \n  c.company_name,\n  SUM(si.quantity_shipped) AS total_units_received,\n"
     "  SUM(si.quantity_shipped * ili.unit_price) AS total_value\n" + _RUN5_FROM
     + "GROUP BY c.company_name\nORDER BY total_value DESC LIMIT 500"),
    ("SELECT \n  SUM(si.quantity_shipped) AS grand_total_units,\n"
     "  SUM(si.quantity_shipped * ili.unit_price) AS grand_total_value\n"
     + _RUN5_FROM + "LIMIT 500"),
]

RUN5_ANSWER = (
    "Perfect! I have all the information needed. Here's the summary:\n\n"
    "## Shipments from Production Batches\n\n"
    "**Note:** The batch IDs you provided use the \"WO-\" prefix, but the system "
    "stores them with \"RUN-\" prefix in shipment records. I've queried for "
    + ", ".join(_KEYS[:-1]) + ", and " + _KEYS[-1] + ".\n\n"
    "### Customer Summary\n\n"
    "| Customer Name | Total Units Received | Total Value |\n|---|---:|---:|\n"
    "| Example Corp Precision | 48 | $16,320.00 |\n"
    "| Example Corp Aerospace | 30 | $10,200.00 |\n"
    "| Example Corp Gear | 20 | $6,800.00 |\n"
    "| Example Corp Drivetrain | 12 | $4,080.00 |\n\n"
    "### Grand Totals\n\n- **Grand Total Units:** 110 units\n"
    "- **Grand Total Value:** $37,400.00")

# Runs 1 and 3 asked per batch and per customer; their answers carry the
# per-batch rows the caveat needs.
SPLIT_ANSWER = (
    "| Batch | Customer | Units | Value |\n|---|---|---|---|\n"
    "| RUN-20261010-92001 | Example Corp Gear | 20 | $6,800.00 |\n"
    "| RUN-20261010-92004 | Example Corp Precision | 20 | $6,800.00 |\n"
    "| RUN-20261011-90001 | Example Corp Aerospace | 10 | $3,400.00 |\n"
    "| RUN-20261011-90001 | Example Corp Precision | 8 | $2,720.00 |\n"
    "| RUN-20261011-90001 | Example Corp Drivetrain | 12 | $4,080.00 |\n"
    "| RUN-20261011-91002 | Example Corp Precision | 20 | $6,800.00 |\n"
    "| RUN-20261011-91005 | Example Corp Aerospace | 20 | $6,800.00 |\n\n"
    "Total: 110 units, $37,400.00.")

# Q1 and Q2 of run 4: a date window, and one key.
RUN4_Q1_QUESTION = (
    "List all shipments of product \"Example Shaft 1000\" to customer \"Example "
    "Corp Drivetrain\" with ship dates between 2026-10-08 and 2026-10-15 (past 7 "
    "days). For each shipment, give the batch_id, ship date, and quantity.")
RUN4_Q2_QUESTION = (
    "For production batch_id \"RUN-20261011-90001\" (Example Shaft 1000), which "
    "supplier raw-material lot(s) were consumed in making this batch? For each lot, "
    "give the lot number, supplier name, and the date/time the lot arrived "
    "(received). Also list any OTHER batch_ids that consumed material from the same "
    "supplier lot(s).")


# ---------------------------------------------------------------------------
# The rows each statement returned (run 4 and run 5 traces; uuids and names
# replaced). Customer totals carry no batch key.
# ---------------------------------------------------------------------------
_PRICE = "340.00"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


_SPLIT = [  # (batch key, customer, units)
    ("RUN-20261010-92001", "Example Corp Gear", 20),
    ("RUN-20261010-92004", "Example Corp Precision", 20),
    ("RUN-20261011-90001", "Example Corp Aerospace", 10),
    ("RUN-20261011-90001", "Example Corp Precision", 8),
    ("RUN-20261011-90001", "Example Corp Drivetrain", 12),
    ("RUN-20261011-91002", "Example Corp Precision", 20),
    ("RUN-20261011-91005", "Example Corp Aerospace", 20),
]
_CUSTOMER_TOTALS = [["Example Corp Precision", 48, "16320.00"],
                    ["Example Corp Aerospace", 30, "10200.00"],
                    ["Example Corp Gear", 20, "6800.00"],
                    ["Example Corp Drivetrain", 12, "4080.00"]]

RUN4_RESULTS = [
    (RUN4_STATEMENTS[0], ["batch_id"], [[key] for key in _KEYS]),
    (RUN4_STATEMENTS[1],
     ["shipment_id", "customer_id", "company_name", "batch_id", "product_id",
      "quantity_shipped", "unit_price", "line_value"],
     [[_uuid(i), _uuid(100 + i), customer, key, _uuid(999), units, _PRICE,
       f"{units * 340}.00"] for i, (key, customer, units) in enumerate(_SPLIT)]),
    (RUN4_STATEMENTS[2], ["company_name", "total_units_shipped", "total_value"],
     _CUSTOMER_TOTALS),
]
RUN5_RESULTS = [
    (RUN5_STATEMENTS[0],
     ["batch_id", "shipment_id", "quantity_shipped", "customer_id", "company_name"],
     [[key, _uuid(i), units, _uuid(100 + i), customer]
      for i, (key, customer, units) in enumerate(_SPLIT)]),
    (RUN5_STATEMENTS[1], ["company_name", "total_units_received", "total_value"],
     _CUSTOMER_TOTALS),
    (RUN5_STATEMENTS[2], ["grand_total_units", "grand_total_value"], [[110, "37400.00"]]),
]
# Customer totals only: the Q4 shape whose caveat went wrong.
TOTALS_ONLY = [RUN4_RESULTS[2]]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _tool_result(sql: str, columns, rows) -> dict:
    body = {**bounded_query_result(list(columns), [list(r) for r in rows], truncated=False),
            "_adt_executed_query": sql}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _event(state, result, *, exception=None, retry=False):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "query_public", "input": {"sql": "SELECT 1"}},
        invocation_state=state, result=result, exception=exception, retry=retry,
        agent=SimpleNamespace(messages=[]))


def _state_after(*results) -> dict:
    """The invocation state after the capture hook saw ``(sql, columns, rows)``
    results, or bare SQL strings (one dummy row each)."""
    capture = trc.InvocationToolResultCapture(system_id="sys-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for item in results:
        sql, columns, rows = (item, ["c"], [[1]]) if isinstance(item, str) else item
        capture._on_after_tool_call(_event(state, _tool_result(sql, columns, rows)))
    return state


def _run_query(answer: str, state: dict, question: str = "question",
               seen: dict | None = None) -> str:
    seen = {} if seen is None else seen

    def _invoke(_agent, augmented):
        seen["augmented"] = augmented
        return answer, state

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", _invoke)
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_write_investigation_trace",
                   lambda _st, _id, q, _a, **_k: seen.setdefault("trace_question", q))
        # These tests pin the grain line; the executed-filters line that also
        # rides these answers is pinned in test_executed_filters_line.py.
        mp.setattr(sa, "_executed_filters_fact", lambda _state: "")
        return asyncio.run(sa._run_mode_a_query(object(), "sys-a", question))


def _keys():
    from agents import listed_keys
    return listed_keys


# The engine texts, spelled out so the reproduction tests fail on the
# assertion itself (not on an import) against code without the fix.
NOTE = (
    "[engine] This question lists several record keys of one kind. Wherever your "
    "answer gives a total, sum or count across them, also give that figure for "
    "each listed key. When the question also groups the figures (for example per "
    "recipient or per site), give one row for each pair of a listed key and a group "
    "in a single table, never one table per key and another per group, so the "
    "reader can see which group each key's figure belongs to.")


def _grain_line(column: str, count: int, proven: int = 0, operator: str = "total",
                group: str = "") -> str:
    if group:
        noun = {"total": "totals", "average": "averages",
                "extreme": "smallest or largest values"}[operator]
        verb = {"total": "add up rows for", "average": "are taken over rows for",
                "extreme": "are picked from rows for"}[operator]
        if proven:
            return (f"[engine] The {group} {noun} in this answer {verb} {count} listed "
                    f"{column} values, and the answer pairs only {proven} of them with "
                    f"their {group} values, so it does not show which {group} values the "
                    f"other {count - proven} have rows for.")
        return (f"[engine] The {group} {noun} in this answer {verb} {count} listed "
                f"{column} values without pairing any {column} with its {group} values, "
                f"so it does not show which {group} values a single {column} has rows "
                f"for, or which {column} a {group} figure came from.")
    head = {
        "total": "[engine] A total in this answer adds up rows for",
        "average": "[engine] An average in this answer is taken over rows for",
        "extreme": "[engine] A smallest or largest value in this answer is picked from rows for",
    }[operator]
    if proven:
        rest = {"total": f"what the other {count - proven} contributed",
                "average": f"the average for the other {count - proven}",
                "extreme": f"the value for the other {count - proven}"}[operator]
        return (f"{head} {count} listed {column} values, but the answer gives a figure "
                f"of its own for only {proven} of them, so it does not show {rest}.")
    rest = {"total": f"which {column} a total came from",
            "average": f"the average for any one {column}",
            "extreme": f"which {column} that value came from"}[operator]
    return (f"{head} {count} listed {column} values with no figure for any single "
            f"one of them, so it does not show {rest}.")


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


# --------------------------------------------------------------------------- #
# 1. Runs 4 and 5 through the Mode A query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [RUN4_QUESTION, RUN5_QUESTION], ids=["run4", "run5"])
def test_the_q4_question_asks_for_a_figure_per_listed_key(question):
    seen: dict = {}
    _run_query("Answer.", {}, question=question, seen=seen)
    # Time context, then the question, then the one engine line.
    assert seen["augmented"].endswith("\n" + question + "\n\n" + NOTE)
    assert seen["augmented"].count("[engine]") == 1
    # The trace keeps the question the orchestrator wrote.
    assert seen["trace_question"] == question


@pytest.mark.parametrize("results,answer", [
    (RUN4_RESULTS, RUN4_ANSWER), (RUN5_RESULTS, RUN5_ANSWER)], ids=["run4", "run5"])
def test_customer_totals_over_listed_batches_carry_the_grain_line(results, answer):
    out = _run_query(answer, _state_after(*results))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")
    assert out.count("[engine]") == 1


@pytest.mark.parametrize("results", [RUN4_RESULTS, RUN5_RESULTS], ids=["run4", "run5"])
def test_an_answer_whose_per_batch_rows_the_call_proves_is_untouched(results):
    assert _run_query(SPLIT_ANSWER, _state_after(*results)) == SPLIT_ANSWER


@pytest.mark.parametrize("line", [
    "Note: RUN-20261011-90001 failed 3 dimensional checks on 2026-10-11.",
    "- RUN-20261011-90001 (the failed batch): Example Corp Drivetrain, 12 units",
    "RUN-20261011-90001 shipped 12 units to Example Corp Drivetrain.",
], ids=["unrelated-number", "number-from-memory", "sentence-from-memory"])
def test_a_one_key_line_the_rows_do_not_prove_still_gets_the_line(line):
    # Customer totals only this call: the 12 is the earlier turn's figure.
    answer = RUN4_ANSWER + "\n\n" + line
    out = _run_query(answer, _state_after(*TOTALS_ONLY))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")


def test_an_unrelated_number_beside_a_key_the_rows_hold_is_not_a_figure():
    # The detail rows are in this call, but 3 is no value they hold for the key.
    answer = RUN4_ANSWER + "\n\nNote: RUN-20261011-90001 failed 3 dimensional checks."
    out = _run_query(answer, _state_after(*RUN4_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")


def test_a_figure_for_one_key_names_how_many_are_left():
    # The collapsed query grouped by customer, so a key counts only once its
    # figure is given per customer: 90001's three rows, each with its customer.
    answer = RUN4_ANSWER + "\n\n" + "\n".join(
        f"| {key} | {customer} | {units} |" for key, customer, units in _SPLIT
        if key == "RUN-20261011-90001")
    out = _run_query(answer, _state_after(*RUN4_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, proven=1, group="company_name")


def test_a_keys_total_without_its_groups_is_not_a_figure_for_a_grouped_total():
    # 30 is 90001's own sum (10 + 8 + 12), but the totals were per customer:
    # nothing on the line says which customers the 30 units went to.
    answer = RUN4_ANSWER + "\n\nRUN-20261011-90001 went to three customers: 30 units in all."
    out = _run_query(answer, _state_after(*RUN4_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")


@pytest.mark.parametrize("question", [RUN4_Q1_QUESTION, RUN4_Q2_QUESTION, "question"],
                         ids=["run4-q1-dates", "run4-q2-one-key", "plain"])
def test_a_question_without_a_key_list_reaches_the_agent_unchanged(question):
    seen: dict = {}
    _run_query("Answer.", {}, question=question, seen=seen)
    assert seen["augmented"].endswith(question)
    assert "[engine]" not in seen["augmented"]


def test_keys_in_the_guidance_or_graph_context_do_not_count(monkeypatch):
    seen: dict = {}

    def _invoke(_agent, augmented):
        seen["augmented"] = augmented
        return "Answer.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", _invoke)
    monkeypatch.setattr(sa, "_augment_question_with_guidance",
                        lambda _s, q: q + "\n\nGuidance: total for AB-1001 and AB-1002.")
    asyncio.run(sa._run_mode_a_query(object(), "sys-a", RUN4_Q1_QUESTION,
                                     kg_context="total for CD-2001, CD-2002"))
    assert "[engine]" not in seen["augmented"]


# --------------------------------------------------------------------------- #
# 2. The question note: one namespace, and a question that asks for a figure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    (RUN5_QUESTION, ["WO-20261010-92001", "WO-20261010-92004", "WO-20261011-90001",
                     "WO-20261011-91002", "WO-20261011-91005"]),
    ("Compare L3-M1 and L3-M2 downtime.", ["L3-M1", "L3-M2"]),
    ("Totals for AB-1001, ab-1001 and AB-1002.", ["AB-1001", "AB-1002"]),
    ("Totals for RUN-20261010-92001, WO-20261010-92001, RUN-20261011-90001 and "
     "WO-20261011-90001.",
     ["RUN-20261010-92001", "RUN-20261011-90001", "WO-20261010-92001", "WO-20261011-90001"]),
], ids=["run5", "machine-codes", "case-repeat", "two-namespaces-two-lists"])
def test_listed_keys_are_two_or_more_keys_of_one_namespace(text, expected):
    assert sorted(_keys().listed_keys(text)) == sorted(expected)


@pytest.mark.parametrize("text", [
    RUN4_Q1_QUESTION,                          # dates are not keys
    RUN4_Q2_QUESTION,                          # one key
    "Total value for customer C-100 and order O-200.",   # two namespaces
    "Total for RUN-20261011-90001 and WO-20261011-90002.",
    "Runs AB-1001 and 1001-AB-7.",             # two shapes
    "Rows for a49e3824-7429-44d6-a698-a0b754d2d0d0 and "
    "92160089-74b4-4948-8d16-1944aa3cef47.",   # row ids, not record keys
    "Statuses OPEN and IN_PROGRESS for P1, P2.",  # plain words and short codes
    "Columns shipment_items.batch_id and order_line_items.line_item_id.",
    "", None,
], ids=["dates", "one-key", "customer-and-order", "run-and-work-order", "two-shapes",
        "uuids", "words-and-short-codes", "column-names", "empty", "none"])
def test_text_without_a_key_list_lists_nothing(text):
    assert _keys().listed_keys(text) == []
    assert _keys().with_listed_keys_note(text) == text


@pytest.mark.parametrize("text", [
    "Compare L3-M1 and L3-M2 downtime.",
    "List the inspections for RUN-20261011-91002 and RUN-20261011-91005.",
    "Which customers received RUN-20261011-91002 or RUN-20261011-91005?",
], ids=["compare", "list", "which"])
def test_a_key_list_without_an_aggregate_word_gets_no_note(text):
    assert _keys().listed_keys(text)
    assert _keys().with_listed_keys_note(text) == text


@pytest.mark.parametrize("text", [
    "How many stops did L3-M1 and L3-M2 have?",
    "Sum the scrap for L3-M1 and L3-M2.",
    "Give the count of orders for RUN-20261011-91002 and RUN-20261011-91005.",
    "What is the value shipped from RUN-20261011-91002 and RUN-20261011-91005?",
    "Give the total for RUN-20261011-91002, RUN-20261011-91005.",
], ids=["how-many", "sum", "count", "value", "total"])
def test_a_key_list_with_an_aggregate_word_gets_the_note(text):
    assert _keys().with_listed_keys_note(text) == text + "\n\n" + NOTE


def test_the_note_is_appended_once_after_the_question():
    noted = _keys().with_listed_keys_note(RUN5_QUESTION + "  \n")
    assert noted == RUN5_QUESTION + "\n\n" + NOTE


def test_the_note_names_no_dataset_word_or_literal():
    low = NOTE.lower()
    for word in ("shipment", "customer", "supplier", "machine", "batch", "lot", "run"):
        assert not re.search(rf"\b{word}s?\b", low), word
    assert not re.search(r"\d", NOTE)
    assert not re.search(r"\b[A-Za-z0-9]+[-_][A-Za-z0-9]+\b", NOTE)
    # No current-state word: the integrity layer reads the question's intent.
    assert not re.search(r"\b(current|latest|live|now|today|open|available)\b", low)


# --------------------------------------------------------------------------- #
# 3. Which queries collapse listed keys
# --------------------------------------------------------------------------- #
def _collapsed(sql):
    return [(c.column, c.keys, c.operator, set(c.measures))
            for c in _keys().collapsed_key_filters(sql)]


def test_run4_and_run5_aggregates_collapse_the_batch_keys():
    units_and_price = {"quantity_shipped", "unit_price"}
    for sql in (RUN4_STATEMENTS[2], RUN5_STATEMENTS[1], RUN5_STATEMENTS[2]):
        assert _collapsed(sql) == [("batch_id", tuple(_KEYS), "total", units_and_price)]


@pytest.mark.parametrize("sql,operator,measures", [
    ("SELECT c, AVG(q) FROM t WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
     "average", {"q"}),
    ("SELECT c, MAX(t.q) AS top FROM t WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
     "extreme", {"q"}),
    ("SELECT c, MIN(q), MAX(q) FROM t WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
     "extreme", {"q"}),
    ("SELECT c, COUNT(*) FROM t WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
     "total", set()),
    ("SELECT c, AVG(q), SUM(q) FROM t WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
     "total", {"q"}),
    ("SELECT c, SUM(CASE WHEN s = 'x' THEN t.q ELSE 0 END) FROM t "
     "WHERE k IN ('AB-1001','AB-1002') GROUP BY c", "total", {"s", "q"}),
], ids=["avg", "max", "min-max", "count", "avg-and-sum", "case"])
def test_the_operator_and_measures_are_read_from_the_select_list(sql, operator, measures):
    assert _collapsed(sql) == [("k", ("AB-1001", "AB-1002"), operator, measures)]


@pytest.mark.parametrize("sql", [
    RUN4_STATEMENTS[0], RUN4_STATEMENTS[1], RUN5_STATEMENTS[0],   # no aggregate
    # grouped by the key column
    "SELECT si.batch_id, c.name, SUM(si.qty) FROM t si JOIN c ON 1=1 "
    "WHERE si.batch_id IN ('AB-1001','AB-1002') GROUP BY si.batch_id, c.name",
    # the keys survive inside a list aggregate
    "SELECT c.name, STRING_AGG(DISTINCT si.batch_id, ',') AS keys, SUM(si.qty) "
    "FROM t si WHERE si.batch_id IN ('AB-1001','AB-1002') GROUP BY c.name",
    # NOT IN, one key, plain words, mixed shapes, mixed namespaces
    "SELECT c, SUM(q) FROM t WHERE k NOT IN ('AB-1001','AB-1002') GROUP BY c",
    "SELECT c, SUM(q) FROM t WHERE k IN ('AB-1001') GROUP BY c",
    "SELECT c, COUNT(*) FROM t WHERE status IN ('OPEN','CLOSED') GROUP BY c",
    "SELECT c, SUM(q) FROM t WHERE k IN ('AB-1001','1001-AB-7') GROUP BY c",
    "SELECT c, SUM(q) FROM t WHERE k IN ('C-100','O-200') GROUP BY c",
    # an IN list outside WHERE, a list of numbers, a subquery
    "SELECT c, SUM(CASE WHEN k IN ('AB-1001','AB-1002') THEN q END) FROM t GROUP BY c",
    "SELECT c, SUM(q) FROM t WHERE k IN (1001, 1002) GROUP BY c",
    "SELECT c, SUM(q) FROM t WHERE k IN ('AB-1001','AB-1002') "
    "AND o = (SELECT MAX(o) FROM u) GROUP BY c",
    "WITH x AS (SELECT * FROM t) SELECT c, SUM(q) FROM x "
    "WHERE k IN ('AB-1001','AB-1002') GROUP BY c",
    # the IN list sits inside a string or a comment
    "SELECT c, SUM(q) FROM t WHERE note = 'k IN (''AB-1001'',''AB-1002'')' GROUP BY c",
    "SELECT c, SUM(q) FROM t -- WHERE k IN ('AB-1001','AB-1002')\nGROUP BY c",
    "", "not sql at all", None,
], ids=["run4-distinct", "run4-detail", "run5-detail", "grouped-by-key",
        "list-aggregate", "not-in", "one-key", "plain-words", "two-shapes",
        "two-namespaces", "in-select-list", "numbers", "subquery", "cte",
        "in-a-string", "in-a-comment", "empty", "not-sql", "none"])
def test_queries_that_keep_or_lack_a_key_list_do_not_collapse(sql):
    assert _collapsed(sql) == []


def test_a_quoted_key_column_is_read_bare():
    sql = ('SELECT "c", COUNT(*) FROM t WHERE "T"."Batch_Id" IN '
           "('AB-1001', 'AB-1002', 'AB-1003') GROUP BY \"c\"")
    assert _collapsed(sql) == [("batch_id", ("AB-1001", "AB-1002", "AB-1003"), "total", set())]


# --------------------------------------------------------------------------- #
# 4. The answer line: wording, proof from rows, placement
# --------------------------------------------------------------------------- #
def test_the_engine_texts_are_exact():
    assert _keys().LISTED_KEYS_NOTE == NOTE
    for operator in ("total", "average", "extreme"):
        for proven in (0, 2):
            for group in ("", "company_name"):
                assert (_keys().grain_line_text("batch_id", 5, operator=operator,
                                                proven=proven, group=group)
                        == _grain_line("batch_id", 5, proven=proven, operator=operator,
                                       group=group))


def test_an_unsafe_group_name_is_not_echoed():
    assert _keys().grain_line_text("batch_id", 2, group="x; drop") == _grain_line(
        "batch_id", 2, group="group")


def test_an_unsafe_column_name_is_not_echoed():
    assert _keys().grain_line_text("x; drop", 2) == _grain_line("key", 2)


def _avg_results():
    sql = ("SELECT c.name, AVG(si.cycle_time) AS avg_cycle FROM t si "
           "WHERE si.run IN ('AB-1001','AB-1002','AB-1003') GROUP BY c.name")
    detail = ("SELECT si.run, si.cycle_time FROM t si "
              "WHERE si.run IN ('AB-1001','AB-1002','AB-1003')")
    return [(detail, ["run", "cycle_time"],
             [["AB-1001", 41], ["AB-1001", 43], ["AB-1002", 50], ["AB-1003", 61]]),
            (sql, ["name", "avg_cycle"], [["Cell A", 49]])]


def test_an_average_over_listed_keys_gets_average_wording():
    out = _run_query("Average cycle time: 49 s.", _state_after(*_avg_results()))
    assert out.endswith("\n\n" + _grain_line("run", 3, operator="average", group="name"))


def test_a_max_over_listed_keys_gets_its_own_wording():
    sql = ("SELECT c.name, MAX(si.cycle_time) AS worst FROM t si "
           "WHERE si.run IN ('AB-1001','AB-1002') GROUP BY c.name")
    out = _run_query("Worst cycle: 61 s.", _state_after((sql, ["name", "worst"], [["A", 61]])))
    assert out.endswith("\n\n" + _grain_line("run", 2, operator="extreme", group="name"))


def test_a_header_or_list_naming_every_key_is_not_a_per_key_figure():
    answer = "\n".join(["Batches: " + ", ".join(_KEYS), "1. RUN-20261010-92001",
                        "- RUN-20261010-92004", "Totals: 110 units."])
    out = _run_query(answer, _state_after(*RUN4_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")


@pytest.mark.parametrize("answer", [
    SPLIT_ANSWER,
    json.dumps({"rows": [{"batch": key, "customer": customer, "units": units}
                         for key, customer, units in _SPLIT]}),
    json.dumps([[key, customer, units] for key, customer, units in _SPLIT]),
    json.dumps({"answer": "Per batch:\n" + "\n".join(
        f"- {key} to {customer}: {units} units" for key, customer, units in _SPLIT)}),
], ids=["table", "json-records", "json-rows", "json-prose"])
def test_per_key_figures_the_rows_prove_count_in_prose_and_json(answer):
    assert _run_query(answer, _state_after(*RUN4_RESULTS)) == answer


_PER_KEY_SQL = ("SELECT si.batch_id, SUM(si.quantity_shipped) AS units FROM shipment_items si "
                f"WHERE si.batch_id IN ({_IN_LIST}) GROUP BY si.batch_id")
_PER_KEY_ROWS = [["RUN-20261010-92001", 20], ["RUN-20261010-92004", 20],
                 ["RUN-20261011-90001", 30], ["RUN-20261011-91002", 20],
                 ["RUN-20261011-91005", 20]]
_GRAND_SQL = ("SELECT SUM(si.quantity_shipped) AS total_units FROM shipment_items si "
              f"WHERE si.batch_id IN ({_IN_LIST})")
_GRAND = (_GRAND_SQL, ["total_units"], [[110]])


#: The engine's own totals line for the per-key result (``answer_figures``):
#: it is a complete grouped SUM, so the call states its total.
_PER_KEY_TOTALS = ("[engine] Result totals: the engine added up every returned row of each "
                   "complete grouped result, in this turn's queries numbered as above: "
                   "Q1 (5 rows, one per value of batch_id, of shipment_items rows where "
                   "si.batch_id IN (<5 values>)): units = SUM(si.quantity_shipped) 110.")


def test_a_per_key_aggregate_alias_proves_a_figure():
    # A grand total over the keys (no other grouping): per-key totals are its
    # breakdown.
    answer = "\n".join(f"{key}: {units} units" for key, units in _PER_KEY_ROWS)
    state = _state_after((_PER_KEY_SQL, ["batch_id", "units"], _PER_KEY_ROWS), _GRAND)
    assert _run_query(answer, state) == answer + "\n\n" + _PER_KEY_TOTALS


def test_per_key_totals_beside_group_totals_do_not_pair_them():
    # The run 1 and run 3 shape: a table per customer and a separate table per
    # batch. Neither says which customers a batch went to.
    answer = "\n".join(f"{key}: {units} units" for key, units in _PER_KEY_ROWS)
    state = _state_after((_PER_KEY_SQL, ["batch_id", "units"], _PER_KEY_ROWS),
                         RUN4_RESULTS[2])
    assert _run_query(answer, state) == answer + "\n\n" + _grain_line(
        "batch_id", 5, group="company_name") + "\n\n" + _PER_KEY_TOTALS


def test_a_count_figure_is_proven_by_the_rows_per_key():
    count_sql = ("SELECT COUNT(*) AS shipments FROM shipment_items si "
                 f"WHERE si.batch_id IN ({_IN_LIST})")
    answer = "\n".join(f"{key}: {n} shipments" for key, n in (
        ("RUN-20261010-92001", 1), ("RUN-20261010-92004", 1), ("RUN-20261011-90001", 3),
        ("RUN-20261011-91002", 1), ("RUN-20261011-91005", 1)))
    state = _state_after(RUN4_RESULTS[1], (count_sql, ["shipments"], [[7]]))
    assert _run_query(answer, state) == answer


def test_a_json_answer_of_customer_totals_carries_the_line_as_the_reserved_member():
    answer = json.dumps({"answer": "Totals by customer.",
                         "rows": [{"customer": "Example Corp Gear", "units": 20}]})
    out = json.loads(_run_query(answer, _state_after(*RUN4_RESULTS)))
    assert out[trc.RESULT_CUT_NOTE_KEY] == _grain_line("batch_id", 5, group="company_name")


def test_a_cut_and_a_collapse_carry_both_lines_in_order():
    cut = {"toolUseId": "t-2", "status": "success", "content": [{"text": strict_json_dumps({
        **bounded_query_result(["a"], [[i] for i in range(500)], truncated=True),
        "_adt_executed_query": "SELECT a FROM t"})}]}
    state = _state_after(*RUN4_RESULTS)
    trc.record_result_cut(state, cut)
    out = _run_query(RUN4_ANSWER, state)
    cut_line = trc.result_cut_line(state)
    assert cut_line
    assert out == RUN4_ANSWER + "\n\n" + cut_line + "\n\n" + _grain_line(
        "batch_id", 5, group="company_name")


def test_enforce_mode_leaves_the_envelope_alone(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    envelope = json.dumps({"answer": "gated", "claims": [], "evidence": []})
    assert _run_query(envelope, _state_after(*RUN4_RESULTS)) == envelope


def test_a_detection_fault_returns_the_answer_unchanged(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_keys(), "grain_line", _boom)
    assert _run_query(RUN4_ANSWER, _state_after(*RUN4_RESULTS)) == RUN4_ANSWER


def test_a_note_fault_sends_the_question_unchanged(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_keys(), "with_listed_keys_note", _boom)
    seen: dict = {}
    _run_query("Answer.", {}, question=RUN5_QUESTION, seen=seen)
    assert seen["augmented"].endswith(RUN5_QUESTION)


# --------------------------------------------------------------------------- #
# 5. The rows the proof reads: this call's own, bounded
# --------------------------------------------------------------------------- #
def test_only_rows_that_hold_a_record_key_are_kept():
    state = _state_after(*RUN4_RESULTS)
    rows = _keys().key_rows(state)
    assert rows and all(row["keys"] for row in rows)
    # Customer-total rows hold no record key, so they are not kept.
    assert len(rows) == len(_KEYS) + len(_SPLIT)


def test_a_failed_or_retried_call_records_no_rows():
    capture = trc.InvocationToolResultCapture(system_id="sys-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    result = _tool_result(*RUN4_RESULTS[1])
    capture._on_after_tool_call(_event(state, result, exception=RuntimeError("x")))
    capture._on_after_tool_call(_event(state, result, retry=True))
    assert _keys().key_rows(state) == []


def test_hostile_row_state_is_tolerated():
    state = _state_after(*RUN4_RESULTS)
    state[_keys().KEY_ROWS_STATE_KEY] = "not a list"
    out = _run_query(RUN4_ANSWER, state)
    assert out == RUN4_ANSWER + "\n\n" + _grain_line("batch_id", 5, group="company_name")


# --------------------------------------------------------------------------- #
# 6. The line trips no classifier, golden or failure reading
# --------------------------------------------------------------------------- #
_ALL_LINES = [_grain_line("batch_id", n, proven=p, operator=o, group=g)
              for n, p in ((2, 0), (5, 0), (5, 1), (13, 4))
              for o in ("total", "average", "extreme")
              for g in ("", "company_name")]


@pytest.mark.parametrize("line", _ALL_LINES)
def test_the_line_is_neither_failed_nor_an_outage_or_fabrication_signal(line):
    text = "Four customer totals, 110 units in all.\n\n" + line
    assert qi.failure_marker(text) is None and not qi.is_failed_result(text)
    assert qi.failure_marker(line) is None
    low = line.lower()
    hits = [m for m in orch._TRANSIENT_TOOL_ERROR_MARKERS + orch._AUTH_TOOL_ERROR_MARKERS
            if m in low]
    hits += [c for c, pattern in chat_client._ERROR_TEXT_CLASSIFIERS
             if pattern.search(line)]
    hits += smoke.classify_error_labels([line])
    hits += smoke.detect_outage_fallback_signals(line)
    hits += smoke.detect_memory_reference_signals(line)
    hits += smoke.detect_fabrication_signals(line)
    hits += [w for w in ("unavailable", "outage", "error", "failed", "timed out")
             if w in low]
    assert hits == []


def test_the_lines_and_note_match_no_golden_must_not():
    goldens = json.loads(_GOLDENS.read_text(encoding="utf-8"))
    questions = goldens["questions"] if isinstance(goldens, dict) else goldens
    assert questions
    for text in _ALL_LINES + [NOTE]:
        for question in questions:
            expect = question.get("expect") or {}
            for pattern in expect.get("answer_must_not_match") or []:
                assert not re.search(pattern, text), (question["id"], pattern)
            assert eval_core.fabrication_flags(
                expect, question.get("text", ""), "Totals.\n\n" + text) == [], question["id"]


# --------------------------------------------------------------------------- #
# 7. Four turns: Q1 to Q3 in the orchestrator's memory, then Q4
# --------------------------------------------------------------------------- #
# What each system call of the chat returned. Q1's rows are filtered to one
# customer, so they pair the failed batch with that customer only. Q4's rows
# are customer totals only; its answer restates the Q1 pairing, the line an
# agent writes from an earlier call, not from this call's rows.
_Q1_SQL = ("SELECT si.batch_id, SUM(si.quantity_shipped) AS total_quantity, COUNT(*) AS "
           "shipments FROM shipment_items si JOIN shipments s ON si.shipment_id = "
           "s.shipment_id WHERE s.customer_id = '" + _uuid(104) + "' AND s.shipment_date "
           ">= '2026-10-08' GROUP BY si.batch_id ORDER BY si.batch_id LIMIT 500")
_Q1_ROWS = [["RUN-20261009-93000", 20, 1], ["RUN-20261011-90001", 12, 1],
            ["RUN-20261012-00003", 409, 96]]
_Q1_ANSWER = ("| Batch | Units | Shipments |\n|---|---|---|\n"
              + "".join(f"| {k} | {u} | {n} |\n" for k, u, n in _Q1_ROWS))
_Q2_SQL = ("SELECT l.lot_number, l.supplier_name, t.batch_id FROM lot_mes_tracking t JOIN "
           "material_lots l ON l.lot_id = t.lot_id WHERE t.batch_id = 'WO-20261011-90001' "
           "LIMIT 500")
_Q2_ANSWER = "Lot LOT-EX-0001 from Example Metals; four other batches drew from it."
_Q3_QUESTION = (
    "For work orders WO-20261010-92001, WO-20261010-92004, WO-20261011-91002 and "
    "WO-20261011-91005: did these runs pass or fail their dimensional quality checks? "
    "Give me the check type, pass/fail counts, and any failure reasons for each.")
_Q3_SQL = ("SELECT work_order, check_type, pass_count, fail_count FROM quality_checks "
           "WHERE work_order IN ('WO-20261010-92001','WO-20261010-92004',"
           "'WO-20261011-91002','WO-20261011-91005') LIMIT 500")
_Q3_ANSWER = "All four work orders passed: 20/20 dimensional, 50/50 visual."
_Q4_ANSWER = (RUN4_ANSWER + "\n\nRUN-20261011-90001 (the failed batch) went to "
              "Example Corp Drivetrain: 12 units.")

_TURNS = [  # (user prompt, tool, delegated question, results, system answer)
    ("Which batches did the customer's shipments come from?", "erp___query_erp",
     RUN4_Q1_QUESTION, [(_Q1_SQL, ["batch_id", "total_quantity", "shipments"], _Q1_ROWS)],
     _Q1_ANSWER),
    ("Which supplier lot did that batch consume?", "erp___query_erp", RUN4_Q2_QUESTION,
     [(_Q2_SQL, ["lot_number", "supplier_name", "batch_id"],
       [["LOT-EX-0001", "Example Metals", "WO-20261011-90001"]])], _Q2_ANSWER),
    ("Did the other batches from that lot pass inspection?", "mes-ind___query_mes_ind",
     _Q3_QUESTION, [(_Q3_SQL, ["work_order", "check_type", "pass_count", "fail_count"],
                     [["WO-20261010-92001", "DIMENSIONAL", 20, 0]])], _Q3_ANSWER),
    ("Which customers received parts from any batch made with that lot?",
     "erp___query_erp", RUN4_QUESTION, TOTALS_ONLY, _Q4_ANSWER),
]


def _result_texts(message: dict) -> list[str]:
    out = []
    for block in message.get("content") or []:
        result = block.get("toolResult") if isinstance(block, dict) else None
        if result:
            out += [part.get("text", "") for part in result.get("content") or []
                    if isinstance(part, dict)]
    return out


def _run_four_turns(monkeypatch, turns, *, real_prompt: bool = False):
    """Run ``turns`` through the production orchestrator construction with a
    scripted model; return the model (its ``calls`` hold every message list it
    was sent, ``system_prompts`` the system prompt of each call)."""
    import copy

    from strands import Agent, tool
    from strands.models.model import Model

    def _tool_call(tool_id, name, question):
        return [{"messageStart": {"role": "assistant"}},
                {"contentBlockStart": {"start": {"toolUse": {
                    "toolUseId": tool_id, "name": name}}}},
                {"contentBlockDelta": {"delta": {"toolUse": {
                    "input": json.dumps({"question": question})}}}},
                {"contentBlockStop": {}},
                {"messageStop": {"stopReason": "tool_use"}}]

    def _final(text):
        return [{"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"delta": {"text": text}}},
                {"contentBlockStop": {}},
                {"messageStop": {"stopReason": "end_turn"}}]

    class _ScriptedModel(Model):
        def __init__(self, scripts):
            self.scripts = list(scripts)
            self.calls: list[list[dict]] = []
            self.system_prompts: list = []

        def update_config(self, **_config):
            return None

        def get_config(self):
            return {}

        def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            self.calls.append(copy.deepcopy(messages))
            self.system_prompts.append(system_prompt)
            for event in self.scripts.pop(0):
                yield event

    scripts = []
    for n, (_prompt, name, question, _results, _answer) in enumerate(turns, start=1):
        scripts += [_tool_call(f"q{n}", name, question), _final(f"Turn {n} answer.")]
    model = _ScriptedModel(scripts)

    by_question = {question: (results, answer)
                   for _p, _n, question, results, answer in turns}

    def _invoke(_agent, augmented):
        # The delegated question arrives with the time line before it and any
        # engine note after it; the state holds this call's own rows only.
        question = next(q for q in by_question if q in augmented)
        results, answer = by_question[question]
        return answer, _state_after(*results)

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", _invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)

    @tool(name="erp___query_erp")
    async def erp_query(question: str) -> str:
        """Ask ERP."""
        return await sa._run_mode_a_query(object(), "erp", question)

    @tool(name="mes-ind___query_mes_ind")
    async def mes_query(question: str) -> str:
        """Ask the plant MES."""
        return await sa._run_mode_a_query(object(), "mes-ind", question)

    # The production orchestrator construction (model, prompt and hooks),
    # with the model replaced by the script.
    monkeypatch.setattr(orch, "build_model", lambda *_a, **_k: model)
    if not real_prompt:
        monkeypatch.setattr(orch, "_build_orchestrator_prompt", lambda: "prompt")
    monkeypatch.setattr(orch, "_metamodel_prompt_suffix", lambda: "")
    kwargs = orch._base_agent_kwargs([erp_query, mes_query], None)
    kwargs.pop("conversation_manager")
    agent = Agent(callback_handler=None, **kwargs)
    for prompt, *_rest in turns:
        agent(prompt)
    return model


def test_four_turns_the_q4_fact_reaches_the_orchestrator_model(monkeypatch):
    model = _run_four_turns(monkeypatch, _TURNS)

    sent = model.calls[-1]           # what the model read before answering Q4
    q4_text = "\n".join(_result_texts(sent[-1]))
    assert "Example Corp Drivetrain | 12 units" in q4_text
    assert _grain_line("batch_id", 5, group="company_name") in q4_text
    # Q1's customer-filtered pairing is in the same context (memory) ...
    earlier = [text for message in sent[:-1] for text in _result_texts(message)]
    assert any("| RUN-20261011-90001 | 12 | 1 |" in text for text in earlier)
    # ... and no earlier result carries a grain line.
    assert not any("listed batch_id values" in text for text in earlier)


# --------------------------------------------------------------------------- #
# 8. A raw aggregate input is not a per-key figure
# --------------------------------------------------------------------------- #
# SUM(qty * price) across the listed keys: each key's unit price is an input
# to the total, not the key's total, even when the key has one row. A grand
# value total (no other grouping), so only the measure is under test.
_VALUE_SQL = ("SELECT SUM(si.quantity_shipped * oli.unit_price) AS total_value "
              "FROM shipment_items si "
              "JOIN order_line_items oli ON si.line_item_id = oli.line_item_id "
              f"WHERE si.batch_id IN ({_IN_LIST})")
_VALUE_RESULT = (_VALUE_SQL, ["total_value"], [["37400.00"]])
_ONE_ROW_PER_KEY = (
    "SELECT si.batch_id, si.quantity_shipped, oli.unit_price FROM shipment_items si "
    "JOIN order_line_items oli ON si.line_item_id = oli.line_item_id "
    f"WHERE si.batch_id IN ({_IN_LIST})",
    ["batch_id", "quantity_shipped", "unit_price"],
    [[key, 20, _PRICE] for key in _KEYS])


@pytest.mark.parametrize("price", ["$340", "$340.00"])
@pytest.mark.parametrize("detail", [RUN4_RESULTS[1], _ONE_ROW_PER_KEY],
                         ids=["run4-detail", "one-row-per-key"])
def test_each_keys_unit_price_does_not_prove_its_value_total(detail, price):
    answer = ("Value by customer: see the table above.\n"
              + "\n".join(f"- {key}: unit price {price}" for key in _KEYS))
    out = _run_query(answer, _state_after(detail, _VALUE_RESULT))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5)


def test_each_keys_value_total_proves_it():
    totals = {"RUN-20261010-92001": "6,800.00", "RUN-20261010-92004": "6,800.00",
              "RUN-20261011-90001": "10,200.00", "RUN-20261011-91002": "6,800.00",
              "RUN-20261011-91005": "6,800.00"}
    answer = "\n".join(f"- {key}: ${value}" for key, value in totals.items())
    assert _run_query(answer, _state_after(RUN4_RESULTS[1], _VALUE_RESULT)) == answer


def test_one_row_out_of_several_is_not_the_keys_figure():
    # This call holds 90001's three rows (10, 8, 12 units); the answer names
    # only the 12 that went to one customer, the Q4 failure mode.
    lines = [f"| {key} | {customer} | {units} |" for key, customer, units in _SPLIT
             if key != "RUN-20261011-90001"]
    answer = "\n".join(lines + ["RUN-20261011-90001 went to Example Corp Drivetrain: 12 units."])
    out = _run_query(answer, _state_after(*RUN4_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, proven=4, group="company_name")


def test_every_row_of_a_key_on_its_own_line_proves_it():
    # 90001's three rows each on a line naming only 90001: the breakdown
    # shows what the key contributed.
    lines = [f"| {key} | {customer} | {units} |" for key, customer, units in _SPLIT]
    answer = "\n".join(lines)
    assert _run_query(answer, _state_after(*RUN4_RESULTS)) == answer


def test_two_rows_on_one_line_do_not_prove_both():
    answer = "\n".join(
        [f"| {key} | {units} |" for key, _c, units in _SPLIT if key != "RUN-20261011-90001"]
        + ["RUN-20261011-90001: 10 and 12 units, then 8 more"])
    # 10, 8 and 12 all sit on ONE line: each row needs its own line. (A grand
    # total over the keys, so a per-key line needs no other group.)
    out = _run_query(answer, _state_after(RUN4_RESULTS[1], _GRAND))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, proven=4)


def test_a_grouped_aggregate_output_finer_than_the_key_proves_by_its_rows():
    finer = ("SELECT si.batch_id, c.company_name, SUM(si.quantity_shipped) AS units "
             "FROM shipment_items si JOIN shipments s ON si.shipment_id = s.shipment_id "
             "JOIN customers c ON s.customer_id = c.customer_id "
             f"WHERE si.batch_id IN ({_IN_LIST}) GROUP BY si.batch_id, c.company_name")
    result = (finer, ["batch_id", "company_name", "units"],
              [[key, customer, units] for key, customer, units in _SPLIT])
    full = "\n".join(f"| {key} | {customer} | {units} |" for key, customer, units in _SPLIT)
    assert _run_query(full, _state_after(result, RUN4_RESULTS[2])) == full
    total = "\n".join([f"| {k} | {u} |" for k, _c, u in _SPLIT if k != "RUN-20261011-90001"]
                      + ["RUN-20261011-90001: 30 units"])
    assert _run_query(total, _state_after(result, _GRAND)) == total


def test_an_output_of_another_aggregate_proves_nothing():
    # A per-key MAX of the unit price is not the per-key SUM of units.
    other = ("SELECT si.batch_id, MAX(oli.unit_price) AS top_price FROM shipment_items si "
             "JOIN order_line_items oli ON si.line_item_id = oli.line_item_id "
             f"WHERE si.batch_id IN ({_IN_LIST}) GROUP BY si.batch_id")
    result = (other, ["batch_id", "top_price"], [[key, 340] for key in _KEYS])
    collapse = ("SELECT c.name, SUM(si.unit_price) AS s FROM t si "
                f"WHERE si.batch_id IN ({_IN_LIST}) GROUP BY c.name")
    answer = "\n".join(f"{key}: 340" for key in _KEYS)
    out = _run_query(answer, _state_after(result, (collapse, ["name", "s"], [["A", 1]])))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="name")


# --------------------------------------------------------------------------- #
# 9. A total per group across listed keys needs the key-by-group pairs
#    (rehearsal, 2026-10-15, five four-question chats before and after this change)
# --------------------------------------------------------------------------- #
# Runs 1 and 3: the ERP agent answered with a table per customer AND a
# separate table per batch. Its own rows held the batch-by-customer split
# (run 1 grouped by both; run 3 read the shipment lines), but the answer did
# not, so the orchestrator took the customer of the failed batch from Q1.
# Run 5: the answer gave the split; the orchestrator's caveat then narrowed
# it to the complaining customer. Customer names are neutral placeholders.
_CUSTOMER_IDS = {name: _uuid(200 + i) for i, name in enumerate(
    sorted({customer for _k, customer, _u in _SPLIT}))}
_BY_BATCH = {}
for _key, _customer, _units in _SPLIT:
    _BY_BATCH[_key] = _BY_BATCH.get(_key, 0) + _units
_FROM = ("FROM erp_data.shipment_items si\n"
         "JOIN erp_data.shipments s ON si.shipment_id = s.shipment_id\n"
         "JOIN erp_data.order_line_items oli ON si.line_item_id = oli.line_item_id\n"
         "JOIN erp_data.customers c ON s.customer_id = c.customer_id\n"
         "WHERE si.batch_id IN (" + ", ".join(f"'{key}'" for key in _KEYS) + ")\n")
_CROSS = (
    "SELECT si.batch_id, s.customer_id, c.company_name,\n"
    "  SUM(si.quantity_shipped) as total_units,\n"
    "  SUM(si.quantity_shipped * oli.unit_price) as total_value\n" + _FROM
    + "GROUP BY si.batch_id, s.customer_id, c.company_name\n"
    "ORDER BY si.batch_id, c.company_name LIMIT 500",
    ["batch_id", "customer_id", "company_name", "total_units", "total_value"],
    [[key, _CUSTOMER_IDS[customer], customer, units, f"{units * 340}.00"]
     for key, customer, units in _SPLIT])
_BY_CUSTOMER = (
    "SELECT s.customer_id, c.company_name,\n"
    "  SUM(si.quantity_shipped) as total_units,\n"
    "  SUM(si.quantity_shipped * oli.unit_price) as total_value\n" + _FROM
    + "GROUP BY s.customer_id, c.company_name\nORDER BY c.company_name LIMIT 500",
    ["customer_id", "company_name", "total_units", "total_value"],
    [[_CUSTOMER_IDS[name], name, units, value] for name, units, value in _CUSTOMER_TOTALS])
_PER_BATCH = (
    "SELECT si.batch_id,\n  SUM(si.quantity_shipped) as total_units,\n"
    "  SUM(si.quantity_shipped * oli.unit_price) as total_value\n"
    "FROM erp_data.shipment_items si\n"
    "JOIN erp_data.shipments s ON si.shipment_id = s.shipment_id\n"
    "JOIN erp_data.order_line_items oli ON si.line_item_id = oli.line_item_id\n"
    "WHERE si.batch_id IN (" + ", ".join(f"'{key}'" for key in _KEYS) + ")\n"
    "GROUP BY si.batch_id\nORDER BY si.batch_id LIMIT 500",
    ["batch_id", "total_units", "total_value"],
    [[key, units, f"{units * 340}.00"] for key, units in _BY_BATCH.items()])
_RAW_LINES = (
    "SELECT si.batch_id, s.shipment_id, s.customer_id, c.company_name, c.customer_code,\n"
    "  si.quantity_shipped, oli.unit_price,\n"
    "  (si.quantity_shipped * oli.unit_price) AS line_value\n" + _FROM
    + "ORDER BY si.batch_id, s.customer_id, si.shipment_id LIMIT 500",
    ["batch_id", "shipment_id", "customer_id", "company_name", "customer_code",
     "quantity_shipped", "unit_price", "line_value"],
    [[key, _uuid(300 + i), _CUSTOMER_IDS[customer], customer, f"CUST-EX-{i:04d}",
      units, _PRICE, f"{units * 340}.00"] for i, (key, customer, units) in enumerate(_SPLIT)])
_BY_CUSTOMER_CODE = (
    "SELECT s.customer_id, c.company_name, c.customer_code,\n"
    "  SUM(si.quantity_shipped) AS total_units,\n"
    "  SUM(si.quantity_shipped * oli.unit_price) AS total_value\n" + _FROM
    + "GROUP BY s.customer_id, c.company_name, c.customer_code\nORDER BY c.company_name",
    ["customer_id", "company_name", "customer_code", "total_units", "total_value"],
    [[_CUSTOMER_IDS[name], name, f"CUST-EX-{i:04d}", units, value]
     for i, (name, units, value) in enumerate(_CUSTOMER_TOTALS)])

_CUSTOMER_TABLE = ("| Customer | Total Units | Total Value |\n|---|---|---|\n"
                   + "".join(f"| {name} | {units} | ${float(value):,.2f} |\n"
                             for name, units, value in _CUSTOMER_TOTALS))
_BATCH_TABLE = ("| Batch ID | Total Units | Total Value |\n|---|---|---|\n"
                + "".join(f"| {key} ({key.replace('RUN-', 'WO-')}) | {units} | "
                          f"${units * 340:,.2f} |\n" for key, units in _BY_BATCH.items()))
RUN1_QUESTION = (
    "For these production batch_ids: WO-20261010-92001, WO-20261010-92004, "
    "WO-20261011-90001, WO-20261011-91002, WO-20261011-91005: which customers received "
    "shipments from any of these batches? For each customer, give the total units "
    "received and the total dollar value of those shipments, and also give me the grand "
    "total units and grand total value across all customers and all five batches.")
RUN1_ANSWER = ("## Results\n\n### Customers Receiving Shipments from These Five Batches:\n\n"
               + _CUSTOMER_TABLE + "\n### Breakdown by Batch ID:\n\n" + _BATCH_TABLE
               + "\n### Grand Totals:\n- **Total Units: 110**\n- **Total Value: $37,400.00**")
RUN1_RESULTS = [_CROSS, _BY_CUSTOMER, _PER_BATCH]
RUN3_ANSWER = ("## Shipment Summary\n\n" + _CUSTOMER_TABLE
               + "\n**Per-Batch Totals** (showing contribution from each batch):\n\n"
               + _BATCH_TABLE).rstrip()
RUN3_RESULTS = [_RAW_LINES, _BY_CUSTOMER_CODE, _PER_BATCH]
RUN5_SPLIT_ANSWER = (
    "### Per Batch Totals:\n\n" + _BATCH_TABLE
    + "\n### Per Customer Breakdown (All Five Batches):\n\n"
    "| Batch ID | Customer | Total Units | Total Value |\n|---|---|---|---|\n"
    + "".join(f"| {key} | {customer} | {units} | ${units * 340:,.2f} |\n"
              for key, customer, units in _SPLIT)).rstrip()
RUN5_SPLIT_RESULTS = [_CROSS, _PER_BATCH]


@pytest.mark.parametrize("results,answer", [
    (RUN1_RESULTS, RUN1_ANSWER), (RUN3_RESULTS, RUN3_ANSWER)], ids=["run1", "run3"])
def test_a_table_per_customer_and_a_table_per_batch_get_the_line(results, answer):
    out = _run_query(answer, _state_after(*results), question=RUN1_QUESTION)
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, group="company_name")


def test_an_answer_with_the_key_by_group_pairs_is_untouched():
    assert (_run_query(RUN5_SPLIT_ANSWER, _state_after(*RUN5_SPLIT_RESULTS))
            == RUN5_SPLIT_ANSWER)
    # The same pairs answer the run 1 shape, whose rows held them.
    assert _run_query(RUN5_SPLIT_ANSWER, _state_after(*RUN1_RESULTS)) == RUN5_SPLIT_ANSWER


def test_a_pair_line_must_carry_that_pairs_own_figure():
    # 90001 named with each customer, but every line carries the batch's 30.
    answer = RUN5_SPLIT_ANSWER.replace("| 12 | $4,080.00 |", "| 30 | $10,200.00 |")
    out = _run_query(answer, _state_after(*RUN1_RESULTS))
    assert out == answer + "\n\n" + _grain_line("batch_id", 5, proven=4, group="company_name")


@pytest.mark.parametrize("sql,groups", [
    (_BY_CUSTOMER[0], [("customer_id",), ("company_name",)]),
    (_BY_CUSTOMER_CODE[0], [("customer_id",), ("company_name",), ("customer_code",)]),
    ("SELECT c.company_name AS customer, SUM(q) AS u FROM t WHERE k IN "
     "('AB-1001','AB-1002') GROUP BY c.company_name", [("company_name", "customer")]),
    ("SELECT c.company_name AS customer, SUM(q) AS u FROM t WHERE k IN "
     "('AB-1001','AB-1002') GROUP BY 1", [("customer", "company_name")]),
    ("SELECT DATE_TRUNC('day', d), SUM(q) FROM t WHERE k IN ('AB-1001','AB-1002') "
     "GROUP BY DATE_TRUNC('day', d)", [()]),
    (_GRAND_SQL, []),
], ids=["two-columns", "three-columns", "aliased", "ordinal", "expression", "none"])
def test_the_group_columns_are_read_from_the_group_by(sql, groups):
    (collapse,) = _keys().collapsed_key_filters(sql)
    assert [tuple(names) for names in collapse.groups] == groups


def test_an_expression_group_can_never_be_paired_so_the_line_stays():
    sql = ("SELECT DATE_TRUNC('day', si.d), SUM(si.quantity_shipped) AS u FROM t si "
           f"WHERE si.batch_id IN ({_IN_LIST}) GROUP BY DATE_TRUNC('day', si.d)")
    answer = "\n".join(f"{key}: {units} units" for key, units in _PER_KEY_ROWS)
    state = _state_after((_PER_KEY_SQL, ["batch_id", "units"], _PER_KEY_ROWS),
                         (sql, ["d", "u"], [["2026-10-11", 110]]))
    assert _run_query(answer, state) == answer + "\n\n" + _grain_line(
        "batch_id", 5, group="group") + "\n\n" + _PER_KEY_TOTALS


_Q4_SHAPES = {  # name -> (Q4 results, ERP answer, grain line expected)
    "phase1-run4": (TOTALS_ONLY, _Q4_ANSWER, True),
    "run1": (RUN1_RESULTS, RUN1_ANSWER, True),
    "run3": (RUN3_RESULTS, RUN3_ANSWER, True),
    "run5": (RUN5_SPLIT_RESULTS, RUN5_SPLIT_ANSWER, False),
}


@pytest.mark.parametrize("shape", sorted(_Q4_SHAPES))
def test_four_turns_each_q4_shape_reaches_the_orchestrator_model(monkeypatch, shape):
    results, answer, flagged = _Q4_SHAPES[shape]
    turns = _TURNS[:3] + [(_TURNS[3][0], "erp___query_erp", RUN1_QUESTION, results, answer)]
    model = _run_four_turns(monkeypatch, turns, real_prompt=True)
    sent = model.calls[-1]
    q4_text = "\n".join(_result_texts(sent[-1]))
    assert answer.splitlines()[0] in q4_text
    line = _grain_line("batch_id", 5, group="company_name")
    assert (line in q4_text) is flagged
    # Q1's customer-filtered pairing is in memory in every shape.
    earlier = [text for message in sent[:-1] for text in _result_texts(message)]
    assert any("| RUN-20261011-90001 | 12 | 1 |" in text for text in earlier)
    # The model answering Q4 is told what an engine line means for a pairing
    # and that a record's caveat covers every recipient the rows show.
    system = model.system_prompts[-1]
    system = system if isinstance(system, str) else json.dumps(system)
    assert "ENGINE FACTS:" in system and "RECIPIENTS:" in system


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_the_engine_fact_and_recipient_rules_are_in_both_modes(mode):
    prompt = orch._build_orchestrator_prompt(mode)
    assert prompt.count("ENGINE FACTS:") == 1 and prompt.count("RECIPIENTS:") == 1
    # They ride the same seam as the consistency rule, right after it.
    assert prompt.index("CONSISTENCY:") < prompt.index("ENGINE FACTS:") < prompt.index(
        "RECIPIENTS:")


# --------------------------------------------------------------------------- #
# 10. An earlier result may supply the pairing only when it matches
# --------------------------------------------------------------------------- #
# The engine line describes only the current result's rows. A complete
# earlier key-by-group result (same records, group, measure and scope) is
# still valid evidence; Q1's rows, filtered to one customer, are not.
def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_engine_fact_rule_lets_a_matching_earlier_result_supply_the_pairing():
    rule = _flat(orch._ENGINE_FACT_RULE)
    # No blanket ban on earlier results.
    assert "or from an earlier turn's results" not in rule
    for clause in ("the same records", "the same group", "the same measure",
                   "the same scope", "say which result it came from"):
        assert clause in rule, clause
    # A narrower earlier result does not count, and the re-ask stays.
    assert "filtered to fewer records or groups, or to another window, does not" in rule
    assert "ask that system once more for one row per record and group" in rule


_Q3_PAIRS_QUESTION = (
    "For batch_ids " + ", ".join(_KEYS) + ": give the units shipped for each batch and "
    "customer, one row per batch and customer.")
_Q3_PAIRS_ANSWER = (
    "| Batch ID | Customer | Total Units | Total Value |\n|---|---|---|---|\n"
    + "".join(f"| {key} | {customer} | {units} | ${units * 340:,.2f} |\n"
              for key, customer, units in _SPLIT)).rstrip()


@pytest.mark.parametrize("earlier", ["complete-pairs", "customer-filtered"])
def test_four_turns_an_earlier_pair_result_is_in_context_with_the_rule(monkeypatch, earlier):
    if earlier == "complete-pairs":
        # Q3 already returned every batch-by-customer pair for the same five
        # batches, measure and window: the model may reuse it, saying so.
        q3 = (_TURNS[2][0], "erp___query_erp", _Q3_PAIRS_QUESTION, [_CROSS], _Q3_PAIRS_ANSWER)
        turns = _TURNS[:2] + [q3]
        expected = "| RUN-20261011-90001 | Example Corp Drivetrain | 12 | $4,080.00 |"
    else:
        # Only Q1's rows, filtered to one customer: they do not count.
        turns = _TURNS[:3]
        expected = "| RUN-20261011-90001 | 12 | 1 |"
    turns = turns + [(_TURNS[3][0], "erp___query_erp", RUN1_QUESTION, TOTALS_ONLY, _Q4_ANSWER)]
    model = _run_four_turns(monkeypatch, turns, real_prompt=True)
    sent = model.calls[-1]
    q4_text = "\n".join(_result_texts(sent[-1]))
    assert _grain_line("batch_id", 5, group="company_name") in q4_text
    earlier_texts = [text for message in sent[:-1] for text in _result_texts(message)]
    assert any(expected in text for text in earlier_texts)
    system = model.system_prompts[-1]
    system = _flat(system if isinstance(system, str) else json.dumps(system))
    # The rule the model reads permits the first case and refuses the second.
    assert "An earlier result may supply it only when that result covers the same records" in system
    assert "filtered to fewer records or groups, or to another window, does not" in system


# --------------------------------------------------------------------------- #
# A FROM inside EXTRACT / SUBSTRING / TRIM is not the query's FROM clause
# --------------------------------------------------------------------------- #
# The scan guard lets these queries run on Athena; the engine line must ride
# the answer exactly as it does for the same query written without them.
_EXPRESSION_FROM = [
    ("EXTRACT(HOUR FROM complete_time)", "date_trunc('hour', complete_time)"),
    ("SUBSTRING(code FROM 1 FOR 2)", "substr(code, 1, 2)"),
    ("TRIM(LEADING '0' FROM code)", "ltrim(code)"),
]


@pytest.mark.parametrize("with_from, twin", _EXPRESSION_FROM, ids=["extract", "substring", "trim"])
def test_the_engine_line_rides_an_expression_from_query_as_its_twin(with_from, twin):
    shape = ("SELECT {e} AS bucket, COUNT(*) AS n FROM events "
             "WHERE batch_id IN ('AB-1001', 'AB-1002') GROUP BY 1")
    answers = []
    for expr in (twin, with_from):
        state = _state_after((shape.format(e=expr), ["bucket", "n"], [[1, 42]]))
        answers.append(_run_query("The total is 42.", state))
    line = _grain_line("batch_id", 2, group="bucket")
    assert line in _flat(answers[0])  # the twin gets the line: not vacuous
    assert answers[1] == answers[0]


@pytest.mark.parametrize("alias", ['"hour (local"', '"hour) local"', '"a""(b"'],
                         ids=["open-paren", "close-paren", "doubled-quote"])
def test_the_engine_line_rides_a_query_with_a_parenthesis_in_a_quoted_alias(alias):
    shape = ("SELECT x AS {a}, COUNT(*) AS n FROM t "
             "WHERE k IN ('AB-1001', 'AB-1002') GROUP BY x")
    answers = []
    for name in ("hour_local", alias):
        state = _state_after((shape.format(a=name), ["bucket", "n"], [[1, 42]]))
        answers.append(_run_query("The total is 42.", state))
    assert "[engine]" in answers[0]  # the plain twin gets the line: not vacuous
    assert "[engine] The " in answers[1] and "2 listed k values" in _flat(answers[1])


@pytest.mark.parametrize("literal", ["$$($$", "$tag$($tag$", "$$)$$"],
                         ids=["dollar-open", "tagged-dollar-open", "dollar-close"])
def test_the_engine_line_rides_a_query_with_a_parenthesis_in_a_dollar_quoted_literal(literal):
    shape = ("SELECT {v} AS bucket, COUNT(*) AS n FROM t "
             "WHERE k IN ('AB-1001', 'AB-1002') GROUP BY 1")
    answers = []
    for value in ("'x'", literal):
        state = _state_after((shape.format(v=value), ["bucket", "n"], [[1, 42]]))
        answers.append(_run_query("The total is 42.", state))
    assert "[engine]" in answers[0]  # the plain twin gets the line: not vacuous
    # The line rides; its wording names the group as the plain reading does
    # for this text (the list split counts the literal's parenthesis).
    assert "[engine] The " in answers[1] and "2 listed k values" in _flat(answers[1])
