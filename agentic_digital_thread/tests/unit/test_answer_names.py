# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``answer_names``: which schema example values an answer writes that no
returned data value of the call holds at all, and the fixed engine line.

The contract is conservative: a name counts as returned when any data string
of any result holds it as whole words. Every ambiguity resolves toward no
line, and the accepted misses are pinned here as such."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents import answer_names as an  # noqa: E402

_FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                       / "system_agent_unreturned_names_exact6.json").read_text())

NAME = "Example Corp Precision Parts"
OTHER = "Example Corp Glass Bottles"


def _names(tables_desc: str) -> tuple:
    return an.schema_names(sa._desc_entries(tables_desc))


def _result(payload, status: str = "success") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"toolUseId": "t", "status": status, "content": [{"text": text}]}


def _table(rows, columns=("value",), sql: str = "SELECT value FROM t", **extra) -> dict:
    return {"columns": list(columns), "rows": rows, "row_count": len(rows),
            "_adt_executed_query": sql, **extra}


def _state(names, *payloads) -> dict:
    """An invocation state holding ``payloads`` as this call's tool results."""
    state: dict = {}
    for payload in payloads:
        result = payload if isinstance(payload, dict) and "content" in payload else _result(payload)
        an.record_result(state, result, names)
    return state


def _unreturned(answer: str, names, *payloads, question: str = "") -> list:
    return an.unreturned_names(answer, names, _state(names, *payloads), question)


def test_schema_names_reads_name_shaped_text_examples_only():
    names = _names(_FIXTURE["customers_entry"])
    assert names[:3] == tuple(_FIXTURE["company_name_examples"])
    assert "Carlos Salazar" in names and "Any Town" in names
    # Single words, codes, enums, numbers, uuids, timestamps and redacted columns are not names.
    for value in ("CUST0002", "CUST-MCH-0001", "NET_30", "AUTOMOTIVE", "324437.00", "USA",
                  "Anytown", "ffd2422f-fa37-4727-bac8-21cb3adb49da", "2026-10-12 03:32:40.049537"):
        assert value not in names


def test_schema_names_shape_rules():
    desc = ("items(item_name: character varying e.g. Supplier lot, Purified Water, Bank of Example; "
            "note: text e.g. fully drawn, Cleared A Negative Balance From Earlier Rece\u2026; "
            "label: varchar(40) e.g. Line 2, Orange Soda 500ml; "
            "qty: numeric e.g. 10 20, 30; city: string e.g. Any Town)")
    names = _names(desc)
    assert names == ("Purified Water", "Bank of Example", "Orange Soda 500ml", "Any Town")


# --------------------------------------------------------------------------- #
# Still reported
# --------------------------------------------------------------------------- #

def test_a_name_in_no_result_is_reported_and_the_question_supplies_one():
    names = (NAME, OTHER)
    answer = f"Rows went to {NAME} and {OTHER}."
    assert _unreturned(answer, names) == [NAME, OTHER]
    assert _unreturned(answer, names, _table([[OTHER]])) == [NAME]
    assert _unreturned(answer, names, question=f"Did {NAME} and {OTHER} get any?") == []


def test_a_question_names_it_whatever_word_follows():
    assert _unreturned(f"{NAME} shipped three orders.", (NAME,),
                       question=f"Show {NAME} 2026 shipments") == []
    assert _unreturned(f"{NAME} shipped three orders.", (NAME,),
                       question=f"Did {NAME}West ship?") == [NAME]


def test_a_zero_row_tabular_lookup_by_the_name_is_reported():
    """A tabular result is its rows: the executed query and the engine notes
    beside the rows of a lookup that found nothing prove nothing."""
    sql = f"SELECT value FROM t WHERE name = '{NAME}'"
    empty = _table([], sql=sql, note=f"no rows for {NAME}",
                   identifier_check={"values": [NAME]}, row_id_note=f"[engine] {NAME}")
    assert _unreturned(f"{NAME}: $2,720.00", (NAME,), empty) == [NAME]
    nested = {"result": _table([], sql=sql)}
    assert _unreturned(NAME, (NAME,), nested) == [NAME]


def test_engine_keys_are_never_data():
    assert _unreturned(NAME, (NAME,), {"_adt_source": {"query": NAME}, "items": []}) == [NAME]
    assert _unreturned(NAME, (NAME,), {"data": {"_adt_echo": NAME}}) == [NAME]


def test_a_name_only_in_an_error_result_is_reported():
    assert _unreturned(NAME, (NAME,), _result(NAME, status="error")) == [NAME]
    assert _unreturned(NAME, (NAME,), _result({"message": NAME}, status="error")) == [NAME]
    state: dict = {}
    an.record_result(state, None, (NAME,))
    an.record_result(state, "text", (NAME,))
    assert an.unreturned_names(NAME, (NAME,), state) == [NAME]


# --------------------------------------------------------------------------- #
# What counts as returned: any data string holding the name as whole words
# --------------------------------------------------------------------------- #

def test_a_tabular_result_is_its_rows_at_any_depth():
    assert _unreturned(NAME, (NAME,), _table([[NAME]])) == []
    assert _unreturned(NAME, (NAME,), {"result": _table([[NAME]])}) == []
    # Inside the rows everything is data, a JSON cell's keys included.
    assert _unreturned(NAME, (NAME,), _table([[{"note": NAME}]])) == []


def test_every_key_and_value_of_other_json_is_data():
    """The review's probes of rounds 4 and 5: records with list fields,
    business statuses and message or note fields all return their values."""
    status = "Ready for Review"
    assert _unreturned(f"Item k1 is {status}.", (status,),
                       {"id": "k1", "status": status, "tags": ["priority"]}) == []
    assert _unreturned(NAME, (NAME,), {"id": "k1", "status": "failed", "message": NAME}) == []
    assert _unreturned(NAME, (NAME,), {"id": "k1", "status": "ok", "note": NAME}) == []
    assert _unreturned(NAME, (NAME,), [{"customer": {"name": NAME}}]) == []
    assert _unreturned(NAME, (NAME,), {"by_customer": {NAME: 6800}}) == []


def test_a_value_mentioning_the_name_returns_it():
    for value in (f"{NAME} #2", f"{NAME} (Ohio)", f"SHIPPED TO {NAME.upper()} ON 10/11",
                  f"Shipped to {NAME} on 10/11", f"{NAME} (CUST-100)", f"order for {NAME}, net 30"):
        assert _unreturned(f"{NAME}: $2,720.00", (NAME,), _table([[value]])) == [], value


def test_plain_text_is_read_whole():
    assert _unreturned(NAME, (NAME,), _result(f"Customer: {NAME}\nStatus: active")) == []
    assert _unreturned(NAME, (NAME,), _result(f"Customer record {NAME} is active")) == []
    assert _unreturned(NAME, (NAME,), _result(f"Customer {NAME}West is active")) == [NAME]


def test_held_match_is_whole_words_case_and_spacing_folded():
    assert _unreturned(NAME, (NAME,), _table([["EXAMPLE CORP   PRECISION PARTS"]])) == []
    assert _unreturned(NAME, (NAME,), _table([[NAME.replace(" ", "")]])) == [NAME]
    assert _unreturned(NAME, (NAME,), _table([[f"X{NAME}"]])) == [NAME]


def test_json_escaped_result_strings_are_read():
    name = "Caf\u00e9 Example Group"
    result = json.dumps(_table([[name]]))  # ensure_ascii escapes it
    assert "\\u00e9" in result
    assert _unreturned(f"{name} got 3.", (name,), _result(result)) == []


# --------------------------------------------------------------------------- #
# Accepted misses: no line where one might be right
# --------------------------------------------------------------------------- #

def test_accepted_miss_a_longer_returned_name_proves_the_shorter_one():
    """``<name> West`` may be another record; it still proves ``<name>``."""
    assert _unreturned(f"{NAME}: $2,720.00", (NAME,), _table([[f"{NAME} West"]])) == []


def test_accepted_miss_a_non_tabular_echo_outside_an_engine_key():
    """A wrapped search API echoing the looked-up name proves it, and so does
    a refusal whose note names it: only ``_adt`` keys are known not to be data."""
    assert _unreturned(NAME, (NAME,), {"result": {"query": NAME, "results": []}}) == []
    assert _unreturned(NAME, (NAME,), {"status": "not_run", "note": f"Not run: {NAME}"}) == []


# --------------------------------------------------------------------------- #
# Many and cut results
# --------------------------------------------------------------------------- #

def _big_table(k: int, size: int = 199_000) -> dict:
    """A maximum-size tabular result (the query result cap is 200,000 bytes)."""
    rows, used = [], 0
    while used < size:
        row = [f"R{k}-{len(rows):06d}", "padding value for a wide result row"]
        rows.append(row)
        used += len(json.dumps(row)) + 2
    return _table(rows, columns=("code", "note"), sql=f"SELECT code, note FROM t{k}")


def test_many_maximum_size_results_keep_the_check_on():
    names = (NAME, OTHER)
    big = [_big_table(k) for k in range(8)]
    assert all(190_000 < len(json.dumps(b)) < 200_000 for b in big)
    state = _state(names, _table([[OTHER]]), *big)
    assert an.unreturned_names(f"{NAME} and {OTHER}", names, state) == [NAME], (
        "large results switched the check off, or forgot a name an earlier result proved")


def test_the_record_keeps_names_only():
    names = (NAME, OTHER)
    state = _state(names, _table([[OTHER], [f"{NAME} West"]]), *[_big_table(k) for k in range(3)])
    record = state[an.RESULT_NAMES_STATE_KEY]
    assert set(record) == {"proven", "possible", "cut"}
    assert record["proven"] == {NAME.casefold(), OTHER.casefold()}
    assert len(json.dumps(record, default=sorted)) < 200


def test_a_cut_result_leaves_only_its_own_unread_names_unverified():
    """A result too large to read whole proves the names in the part read. A
    name its raw text holds anywhere else may be in the unread part, so it is
    not flagged; a name absent from that text, and one an earlier result
    proved, are judged as before."""
    third = "Example Corp Unread Name"
    nodes = [0] * (an.JSON_NODE_BUDGET + 10)
    cut = {"columns": ["n", "name"], "rows": [[nodes, third]],
           "_adt_executed_query": "SELECT n, name FROM t"}
    names = (NAME, OTHER, third)
    state = _state(names, _table([[OTHER]]), cut)
    answer = f"{NAME}, {OTHER} and {third}"
    assert an.unreturned_names(answer, names, state) == [NAME]


# --------------------------------------------------------------------------- #
# Matching the answer
# --------------------------------------------------------------------------- #

def test_answer_match_folds_case_and_spacing_and_keeps_whole_words():
    names = ("Any Town", "Purified Water")
    assert _unreturned("Many Towns were served; unpurified waters were used.", names) == []
    # The answer's own spelling is what the line quotes.
    assert _unreturned("Ship to any  TOWN and purified\nwater.", names) == [
        "any TOWN", "purified water"]


def test_a_name_followed_by_any_word_is_matched():
    for follow in ("3 shipments", "Received 3 shipments", "2026 Totals", "West"):
        assert _unreturned(f"{NAME} {follow}", (NAME,)) == [NAME], follow


def test_one_name_written_twice_is_listed_once():
    names = ("Orange Soda 500ml", "Orange soda 500ml")
    assert _unreturned("Orange Soda 500ml, again Orange Soda 500ml.", names) == [
        "Orange Soda 500ml"]


def test_at_most_max_names_in_answer_order():
    names = tuple(f"Example Name {chr(65 + k)}" for k in range(8))
    answer = ", ".join(reversed(names))
    assert _unreturned(answer, names) == list(reversed(names))[:an.MAX_NAMES]


def test_the_exact6_call_flags_the_two_copied_values():
    names = _names(_FIXTURE["customers_entry"])
    state: dict = {}
    for call in _FIXTURE["tool_calls"]:
        an.record_result(state, _result(call["result"]), names)
    found = an.unreturned_names(_FIXTURE["answer"], names, state, _FIXTURE["question"])
    assert found == _FIXTURE["copied"]
    assert _FIXTURE["made_up"] not in found  # not a schema example: never checked


# --------------------------------------------------------------------------- #
# Engine line
# --------------------------------------------------------------------------- #

def test_the_engine_line_is_fixed_and_carries_only_the_names():
    one = an.engine_line(["Example Name A"])
    two = an.engine_line(["Example Name A", "Example Name B"])
    for text in (one, two):
        assert text.startswith(an.MARKER)
        assert not re.search("[\u2013\u2014]", text)
        assert "give the record's id or key instead" in text
    assert '"Example Name A"' in one and "it is an example value" in one
    assert '"Example Name A", "Example Name B"' in two and "they are example values" in two
    assert "no result in this call returned" in two
