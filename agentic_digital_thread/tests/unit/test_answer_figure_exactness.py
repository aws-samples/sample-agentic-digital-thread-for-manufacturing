# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The figure check stays exact when a call holds a huge number.

Measured on an analytics deployment: a system agent's call ended on its tool
budget after one of its queries timed out. The timeout result carried the
engine's query id, a hex UUID with a digit-e-digit run inside it, which was
read as a number of 642 digits. The budget hook cancelled the next tool call;
strands delivers that as a plain-text result, so it was recorded like a data
result and every number read so far became a data value. The pair test then
subtracted each stated figure from that value at 28-digit precision, the
difference rounded to the negated value, which is in the pair set, and every
figure the answer stated was held. A total that was not the sum of its own
rows went unnamed.

Pinned here, each on its own:

* the pair test holds a figure only when the subtraction is exact
  (``answer_figures._held``);
* a cancelled tool call's message is not recorded as returned values
  (``tool_result_capture``), while the conversation snapshot is still kept;
* a number written inside a longer token (an id, a hash) is not read as a
  number, and a number at the end of a sentence or beside punctuation still
  is (``answer_figures._numbers_in``);
* a number with a short unit glued to its digits (a time unit or a k/M
  magnitude, as a data cell like ``12h`` holds it) still reads as the number
  written, so a correct figure taken from such a cell is held;
* a grouped number (``40,425``) is read once, whole, so its pieces never
  pair into a wrong figure (40 + 425), while a compact list's numbers
  (``[40,425]``) are still read one by one;

and, end to end, the measured shape: a wrong total beside a timed-out query
and a cancelled call is named.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_SQL = ("SELECT plant_zone, step_kind, COUNT(*) AS n_steps FROM batch_log "
        "WHERE run_day = DATE '2031-03-04' GROUP BY plant_zone, step_kind")
_ROWS = [["zone_n", "kind_p", "1200"], ["zone_n", "kind_q", "3400"],
         ["zone_s", "kind_p", "560"], ["zone_s", "kind_q", "780"]]       # sums to 5,940
#: A query id shaped like the engine's: a hex UUID whose last group holds a
#: digit-e-digit run (``9e999``).
_QUERY_ID = "0f1e2d3c-4b5a-4697-8a8b-c9e999fa0b1c"
_CANCEL = ("TOOL BUDGET EXHAUSTED (5 tool calls used of 40, 62s elapsed of 60s): do NOT call "
           "any more tools. Answer NOW using ONLY the data already returned.")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


def _af():
    return importlib.import_module("agents.answer_figures")


def _grouped() -> dict:
    body = {**bounded_query_result(["plant_zone", "step_kind", "n_steps"], _ROWS, False),
            "_adt_executed_query": _SQL}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _timeout() -> dict:
    body = {"error": f"Athena query {_QUERY_ID} did not finish within 3s (last state RUNNING)",
            "error_type": "query_timeout", "query_id": _QUERY_ID, "last_state": "RUNNING",
            "scanned_bytes": None}
    return {"toolUseId": "t-2", "status": "success", "content": [{"text": json.dumps(body)}]}


def _cancelled() -> dict:
    return {"toolUseId": "t-3", "status": "error", "content": [{"text": _CANCEL}]}


def _event(state, result, *, cancel=None, messages=()):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": result["toolUseId"], "name": "query_public",
                  "input": {"sql": "SELECT 1"}},
        invocation_state=state, result=result, exception=None, retry=False,
        cancel_message=cancel, agent=SimpleNamespace(messages=list(messages)))


def _state_after(*calls) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result, cancel in calls:
        capture._on_after_tool_call(_event(state, result, cancel=cancel))
    return state


def _run(answer, state, question="How many batch_log steps ran per zone and kind?"):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", question))


# --------------------------------------------------------------------------- #
# The pair test is exact
# --------------------------------------------------------------------------- #
def test_the_pair_test_holds_a_true_sum_and_not_a_figure_lost_in_rounding():
    af = _af()
    huge = Decimal("9E+999")
    pairs = {huge, -huge, Decimal(100), Decimal(200), Decimal(-100), Decimal(-200)}
    assert af._held(Decimal(300), set(), pairs, [])
    assert af._held(Decimal(100), set(), pairs, [])
    assert not af._held(Decimal(12_345), set(), pairs, [])


def test_the_pair_test_is_exact_for_long_integers_too():
    af = _af()
    big = Decimal("123456789012345678901234567890123")          # 33 digits
    pairs = {big, big.copy_negate(), Decimal(7), Decimal(-7)}
    assert af._held(Decimal("123456789012345678901234567890130"), set(), pairs, [])
    assert not af._held(Decimal("123456789012345678901234567890131"), set(), pairs, [])


def test_the_held_pair_set_negates_long_values_exactly():
    af = _af()
    big = Decimal("123456789012345678901234567890123")
    values = {"raw": set(), "data": {big, Decimal(7)}, "derived": set(), "rows": 0}
    checks = af._figure_checks(values, (), "")
    assert big.copy_negate() in checks["pairs"]
    assert af._held(Decimal("123456789012345678901234567890116"), checks["single"],
                    checks["pairs"], checks["rough"])


# --------------------------------------------------------------------------- #
# A cancelled tool call returned nothing
# --------------------------------------------------------------------------- #
def test_a_cancelled_call_adds_nothing_to_the_value_record():
    af = _af()
    state = _state_after((_cancelled(), _CANCEL))
    values = af.result_values(state)
    assert values is None or (values["results"] == 0 and not values["raw"]
                              and not values["data"] and not values["derived"])


def test_a_cancelled_call_does_not_make_earlier_numbers_data_values():
    af = _af()
    error = {"toolUseId": "t-5", "status": "success",
             "content": [{"text": json.dumps({"error": "stopped after 3 polls",
                                              "error_type": "query_timeout"})}]}
    state = _state_after((error, None), (_cancelled(), _CANCEL))
    values = af.result_values(state)
    assert values["results"] == 1
    assert Decimal(3) in values["raw"] and Decimal(3) not in values["data"]
    assert Decimal(40) not in values["raw"] | values["data"]


def test_a_cancelled_call_still_keeps_the_conversation_snapshot():
    af = _af()
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    conversation = [{"role": "user", "content": [{"text": "Count steps; the plan said 6,010."}]}]
    capture._on_after_tool_call(_event(state, _cancelled(), cancel=_CANCEL,
                                       messages=conversation))
    assert af.result_values(state)["context"][0] == conversation


def test_an_uncancelled_error_text_is_still_recorded():
    af = _af()
    state = _state_after(({"toolUseId": "t-4", "status": "error",
                           "content": [{"text": "query failed after 17 retries"}]}, None))
    assert Decimal(17) in af.result_values(state)["data"]


# --------------------------------------------------------------------------- #
# A number inside a longer token is not a number
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    f"query id {_QUERY_ID}",
    "evidence ref-310625e241ab5d07 attached",
    "checksum 3a7e88b1c9",
    "build 1.2.3 of the reader",
])
def test_no_huge_or_partial_number_is_read_inside_a_token(text):
    values = set(_af()._numbers_in(text))
    assert all(abs(v) < 10 ** 12 for v in values), values
    assert Decimal("9E+999") not in values and Decimal("3E+88") not in values


def test_an_id_with_a_digit_e_digit_run_reads_no_number_from_that_run():
    values = set(_af()._numbers_in(_QUERY_ID))
    assert Decimal("9E+999") not in values and Decimal(9) not in values


@pytest.mark.parametrize("text, expected", [
    ("The total is 40,425.", {Decimal(40425)}),
    ("returned 5940 rows; 12 cut", {Decimal(5940), Decimal(12)}),
    ("ratio 3.5, (56) and x=7", {Decimal("3.5"), Decimal(56), Decimal(7)}),
    ('{"n": 14, "m": [1,2]}', {Decimal(14), Decimal(1), Decimal(2)}),
    ("day 2031-03-04 at 00:00:00", {Decimal(2031), Decimal(3), Decimal(4), Decimal(0)}),
    ("value 1.25E7 seen", {Decimal("1.25E7")}),
])
def test_a_standalone_number_is_still_read(text, expected):
    values = {abs(v) for v in _af()._numbers_in(text)}
    assert expected <= values, values


# --------------------------------------------------------------------------- #
# A number with a short unit suffix is still that number
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, expected", [
    ('{"span": "12h"}', {Decimal(12)}),
    ('["5m", "40ms", "90s"]', {Decimal(5), Decimal(40), Decimal(90)}),
    ('{"volume": "3.5k"}', {Decimal("3.5")}),
    ("ran for 12h and 30min", {Decimal(12), Decimal(30)}),
    ("about 2M rows", {Decimal(2)}),
])
def test_a_number_with_a_unit_suffix_is_read_as_that_number(text, expected):
    assert set(_af()._numbers_in(text)) == expected


@pytest.mark.parametrize("text", [
    "ab7e641cd", "ref x12h", "12hq", "id 5mx9", "ab3.5k", "3.5k2",
])
def test_a_token_that_only_contains_a_unit_shaped_run_is_still_no_number(text):
    assert set(_af()._numbers_in(text)) == set()


def test_a_unit_suffixed_cell_supports_the_figure_it_states():
    sql = "SELECT shift_key, MAX(span) AS span FROM shift_log GROUP BY shift_key"
    body = {**bounded_query_result(["shift_key", "span"], [["sh_a", "12h"], ["sh_b", "9h"]],
                                   False), "_adt_executed_query": sql}
    result = {"toolUseId": "t-6", "status": "success",
              "content": [{"text": strict_json_dumps(body)}]}
    state = _state_after((result, None))
    answer = _run("The longest shift is listed.\n\nTotal hours: 12", state,
                  question="How long was the longest shift_log span?")
    assert "Figures in no result" not in answer


# --------------------------------------------------------------------------- #
# A grouped number is one number, never also its comma-separated pieces
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, expected", [
    ("formatted total: 40,425", {Decimal(40425)}),
    ("balance -40,425 left", {Decimal(-40425)}),
    ("moved 1,234,567.5 units", {Decimal("1234567.5")}),
    ("about 2M rows, 1,500k bytes", {Decimal(2), Decimal(1500)}),
    ("40,425 and 12 and 7,001.", {Decimal(40425), Decimal(12), Decimal(7001)}),
    ('{"a": "1,200", "b": [3, 4]}', {Decimal(1200), Decimal(3), Decimal(4)}),
])
def test_a_grouped_number_reads_as_exactly_one_number(text, expected):
    assert set(_af()._numbers_in(text)) == expected


def test_a_compact_list_of_numbers_still_reads_each_one():
    af = _af()
    assert {Decimal(40), Decimal(425)} <= set(af._numbers_in("[40,425]"))
    assert {Decimal(1), Decimal(200), Decimal(3)} <= set(af._numbers_in('{"v":[1,200,3]}'))
    assert set(af._numbers_in("[1,2345]")) == {Decimal(1), Decimal(2345)}


def _text_result(text: str) -> dict:
    return {"toolUseId": "t-7", "status": "success", "content": [{"text": text}]}


def test_a_sum_of_a_grouped_numbers_pieces_is_named():
    state = _state_after((_text_result("formatted total: 40,425"), None))
    answer = _run("The records are counted.\n\nTotal records: 465", state,
                  question="How many batch_log records are there?")
    assert "[engine] Figures in no result: 465" in answer


def test_the_grouped_number_itself_is_still_held():
    state = _state_after((_text_result("formatted total: 40,425"), None))
    answer = _run("The records are counted.\n\nTotal records: 40,425", state,
                  question="How many batch_log records are there?")
    assert "Figures in no result" not in answer


# --------------------------------------------------------------------------- #
# End to end: the measured shape
# --------------------------------------------------------------------------- #
def test_a_wrong_total_beside_a_timeout_and_a_cancelled_call_is_named():
    state = _state_after((_grouped(), None), (_timeout(), None), (_cancelled(), _CANCEL))
    answer = _run("Steps per zone and kind are listed.\n\nTotal steps: 6,010", state)
    assert "[engine] Figures in no result: 6,010" in answer
    assert "REQUIRED: the answer must not state 6,010" in answer


def test_the_correct_total_in_the_same_shape_is_not_named():
    state = _state_after((_grouped(), None), (_timeout(), None), (_cancelled(), _CANCEL))
    answer = _run("Steps per zone and kind are listed.\n\nTotal steps: 5,940", state)
    assert "Figures in no result" not in answer
    assert "[engine] Result totals:" in answer
