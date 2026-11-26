# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The engine never states a sum of per-group distinct counts.

Measured in a rehearsal dry run: a grouped result returned COUNT(*) and
COUNT(DISTINCT <id>) per code. The engine's totals line added the distinct
column's per-code counts and printed the sum, labelled as per-group distinct
counts added up. One id sat under several codes, so the sum counted nothing,
and the orchestrator stated it as a count of distinct ids anyway: the label
beside the figure did not stop it. On another question the same label made a
correct answer hedge its total.

Pinned here:

* a grouped result whose only count columns are COUNT(DISTINCT) gets no
  totals record, no totals line and no engine total;
* a result with COUNT(*) and COUNT(DISTINCT) states the COUNT(*) total only,
  with no distinct figure and no note about one, and its line is the line the
  same result printed without the distinct column;
* a totals record that still marks a total as a COUNT(DISTINCT) one is read
  as no total;
* the line, the engine totals and the evaluator's reading of a line of plain
  COUNT and SUM totals are byte-identical to what the code printed before
  (recorded from it, not from this code);
* a figure line for a call whose only grouped result is distinct-only says
  the engine computed no total, and its requirement says to use a returned
  figure or say the total was not computed.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

import importlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

import agents.tool_result_capture as trc
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_WINDOW = ("packed_at >= TIMESTAMP '2031-05-01 00:00:00 UTC' "
           "AND packed_at < TIMESTAMP '2031-05-02 00:00:00 UTC'")
_BAYS = ["bay_1", "bay_2", "bay_3", "bay_4"]
_COUNTS = ["1200", "3400", "560", "780"]                    # sums to 5,940
_CARRIERS = ["3", "5", "2", "4"]                             # sums to 14
_HEAD = ("[engine] Result totals: the engine added up every returned row of each complete "
         "grouped result, in this turn's queries numbered as above: ")

#: Recorded by running these calls through the code as it was before
#: COUNT(DISTINCT) columns lost their totals: the whole engine lines, the
#: engine totals and the evaluator's reading of each line, for calls of plain
#: COUNT and SUM totals.
_OLD_ONE_COUNT = (
    _HEAD + "Q1 (4 rows, one per value of bay, of crate_log rows with packed_at in "
    "[2031-05-01 00:00:00 UTC, 2031-05-02 00:00:00 UTC)): n = COUNT(*) 5,940.")
_OLD_ONE_SUM = (
    _HEAD + "Q1 (4 rows, one per combination of bay and shift, of crate_log rows): units = "
    "SUM(qty) 91, late_n = SUM(CASE WHEN outcome = 'late' THEN 1 ELSE 0 END) 8.")
_OLD_TWO_SAME_SCOPE = (
    _HEAD + "Q1 (4 rows, one per value of bay, of crate_log rows with packed_at in "
    "[2031-05-01 00:00:00 UTC, 2031-05-02 00:00:00 UTC)): n = COUNT(*) 5,940; Q2 (2 rows, one "
    "per value of shift, of the same crate_log rows as Q1): n = COUNT(*) 5,940.")
_OLD_PLAIN_COUNT = (
    _HEAD + "Q1 (4 rows, one per value of bay, of crate_log rows): n = COUNT(*) 5,940.")
_OLD_FIGURE_LINE = (
    "[engine] Figures in no result: 6,010 in this answer is stated as a total or count, but "
    "none of this call's results holds it, and it is not an engine total or a sum or "
    "difference of two returned values. Engine totals, in this turn's queries numbered as "
    "above: Q1 (4 rows, one per value of bay, of crate_log rows with packed_at in "
    "[2031-05-01 00:00:00 UTC, 2031-05-02 00:00:00 UTC)): n = COUNT(*) 5,940; Q2 (4 rows, one "
    "per combination of bay and shift, of crate_log rows): units = SUM(qty) 91, late_n = "
    "SUM(CASE WHEN outcome = 'late' THEN 1 ELSE 0 END) 8.")
_OLD_FIGURE_REQUIREMENT = (
    "----- BEGIN FINAL-ANSWER REQUIREMENTS -----\nApply only during orchestrator final-answer "
    "composition. Always subordinate to evidence, honest absence, safety, and tool errors; do "
    "not use these requirements for query construction.\n[\"REQUIRED: the answer must not "
    "state 6,010 as data from this system, because none of its results this turn holds it. "
    "Use a figure its results give or an engine total instead.\"]\n"
    "----- END FINAL-ANSWER REQUIREMENTS -----")

_COUNT_BY_BAY = (["bay", "n"], [[b, n] for b, n in zip(_BAYS, _COUNTS)],
                 f"SELECT bay, COUNT(*) AS n FROM crate_log WHERE {_WINDOW} GROUP BY bay")
_SUMS = (["bay", "shift", "units", "late_n"],
         [["bay_1", "s1", "40", "3"], ["bay_1", "s2", "25", "0"], ["bay_2", "s1", "17", "4"],
          ["bay_3", "s2", "9", "1"]],
         "SELECT bay, shift, SUM(qty) AS units, SUM(CASE WHEN outcome = 'late' THEN 1 ELSE 0 "
         "END) AS late_n FROM crate_log GROUP BY bay, shift")
_COUNT_BY_SHIFT = (["shift", "n"], [["s1", "2100"], ["s2", "3840"]],
                   f"SELECT shift, COUNT(*) AS n FROM crate_log WHERE {_WINDOW} GROUP BY shift")
_DISTINCT_ONLY = (["bay", "carriers"], [[b, c] for b, c in zip(_BAYS, _CARRIERS)],
                  "SELECT bay, COUNT(DISTINCT carrier_id) AS carriers FROM crate_log "
                  "GROUP BY bay")
_MIXED = (["bay", "n", "carriers"],
          [[b, n, c] for b, n, c in zip(_BAYS, _COUNTS, _CARRIERS)],
          "SELECT bay, COUNT(*) AS n, COUNT(DISTINCT carrier_id) AS carriers FROM crate_log "
          "GROUP BY bay")
_DISTINCT_FIRST = (["bay", "carriers", "n"],
                   [[b, c, n] for b, n, c in zip(_BAYS, _COUNTS, _CARRIERS)],
                   "SELECT bay, COUNT(DISTINCT carrier_id) AS carriers, COUNT(*) AS n "
                   "FROM crate_log GROUP BY bay")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


def _af():
    return importlib.import_module("agents.answer_figures")


def _payload(fixture) -> dict:
    columns, rows, sql = fixture
    return {**bounded_query_result(columns, rows, False), "_adt_executed_query": sql}


def _result(fixture) -> dict:
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(_payload(fixture))}]}


def _state_after(*fixtures) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for fixture in fixtures:
        capture._on_after_tool_call(SimpleNamespace(
            selected_tool=None,
            tool_use={"toolUseId": "t-1", "name": "query_public", "input": {"sql": "SELECT 1"}},
            invocation_state=state, result=_result(fixture), exception=None, retry=False,
            agent=SimpleNamespace(messages=[])))
    return state


def _call(*fixtures, answer="Rows are listed above."):
    """``(engine lines, engine totals, sidecar statements)`` for one call."""
    af = _af()
    state = _state_after(*fixtures)
    statements = trc.collect_sidecar_statements(state)
    lines = af.engine_lines(statements, af.result_values(state), answer, "")
    return lines, af.engine_totals(statements), statements


# --------------------------------------------------------------------------- #
# A COUNT(DISTINCT) column gets no total
# --------------------------------------------------------------------------- #
def test_a_distinct_only_grouped_result_gets_no_totals_record():
    assert _af().totals_record(_payload(_DISTINCT_ONLY)) is None


def test_a_distinct_only_grouped_result_gets_no_line_and_no_engine_total():
    lines, totals, statements = _call(_DISTINCT_ONLY)
    assert lines == [] and totals == []
    assert _af().totals_line(statements) == ""
    assert not any("totals" in statement for statement in statements)


def test_a_count_and_a_distinct_count_state_the_count_total_only():
    record = _af().totals_record(_payload(_MIXED))
    assert [tuple(item[:3]) for item in record["totals"]] == [("n", "5940", False)]
    lines, totals, _statements = _call(_MIXED)
    assert totals == [Decimal(5940)]
    (line,) = lines
    assert "carriers" not in line and "14" not in line and "distinct" not in line.lower()
    assert line == _OLD_PLAIN_COUNT


def test_a_distinct_column_before_the_count_keeps_the_count_cells_in_line():
    af = _af()
    record = af.totals_record(_payload(_DISTINCT_FIRST))
    assert [tuple(item[:3]) for item in record["totals"]] == [("n", "5940", False)]
    assert record["cells"] == [["1200", "3400", "560", "780"]]
    _lines, _totals, statements = _call(_DISTINCT_FIRST)
    ((cells, total, _keys, ordinal),) = af._total_columns(statements)
    assert cells == [Decimal(c) for c in _COUNTS] and total == Decimal(5940) and ordinal == 1


def test_a_record_that_still_marks_a_distinct_total_is_read_as_no_total():
    af = _af()
    sql = _MIXED[2]
    mixed = {"rows": 4, "keys": ["bay"],
             "totals": [("n", "5940", False, "COUNT(*)"),
                        ("carriers", "14", True, "COUNT(DISTINCT carrier_id)")]}
    statements = [{"query_text": sql, "totals": mixed}]
    assert af.engine_totals(statements) == [Decimal(5940)]
    line = af.totals_line(statements)
    assert "carriers" not in line and "14" not in line and line.endswith("n = COUNT(*) 5,940.")
    only = {"rows": 4, "keys": ["bay"],
            "totals": [("carriers", "14", True, "COUNT(DISTINCT carrier_id)")]}
    statements = [{"query_text": _DISTINCT_ONLY[2], "totals": only}]
    assert af.totals_line(statements) == "" and af.engine_totals(statements) == []


def test_no_distinct_note_is_left_on_the_module():
    assert not hasattr(_af(), "DISTINCT_NOTE")


# --------------------------------------------------------------------------- #
# A distinct-only call's figure line and requirement
# --------------------------------------------------------------------------- #
def test_a_figure_beside_a_distinct_only_result_is_told_no_total_was_computed():
    lines, _totals, _statements = _call(_DISTINCT_ONLY, answer="Per bay.\n\nTotal crates: 6,010")
    line, block = lines
    assert line.startswith("[engine] Figures in no result: 6,010 in this answer")
    assert line.endswith(" The engine computed no total from this call's results.")
    assert "14" not in line and "carriers" not in line
    assert ("Use a figure its results give instead, or say that total was not computed."
            in block)


def test_a_distinct_sum_stated_as_a_count_is_not_vouched_by_an_engine_total():
    # The figure check still holds a whole-column sum of a returned column (not
    # changed here); what changes is that no engine line states the sum.
    lines, totals, _statements = _call(_MIXED, answer="5,940 crates, covering 14 carriers.")
    assert Decimal(14) not in totals
    assert all("14" not in line for line in lines)


# --------------------------------------------------------------------------- #
# Plain COUNT and SUM totals are unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixtures, line, totals", [
    ((_COUNT_BY_BAY,), _OLD_ONE_COUNT, ["5940"]),
    ((_SUMS,), _OLD_ONE_SUM, ["91", "8"]),
    ((_COUNT_BY_BAY, _COUNT_BY_SHIFT), _OLD_TWO_SAME_SCOPE, ["5940", "5940"]),
], ids=["one_count", "one_sum", "two_same_scope"])
def test_plain_totals_print_as_before(fixtures, line, totals):
    lines, engine, _statements = _call(*fixtures)
    assert lines == [line]
    assert engine == [Decimal(t) for t in totals]


def test_a_figure_line_over_plain_totals_prints_as_before():
    lines, engine, _statements = _call(_COUNT_BY_BAY, _SUMS,
                                       answer="Per bay.\n\nTotal crates: 6,010")
    assert lines == [_OLD_FIGURE_LINE, _OLD_FIGURE_REQUIREMENT]
    assert engine == [Decimal(5940), Decimal(91), Decimal(8)]


@pytest.mark.parametrize("line, read", [
    (_OLD_ONE_COUNT, {"5940"}),
    (_OLD_ONE_SUM, {"0", "1", "8", "91"}),
    (_OLD_TWO_SAME_SCOPE, {"5940"}),
    (_OLD_PLAIN_COUNT, {"5940"}),
    (_OLD_FIGURE_LINE, {"0", "1", "5940", "8", "91"}),
    (_OLD_FIGURE_REQUIREMENT, set()),
], ids=["one_count", "one_sum", "two_same_scope", "plain_count", "figure_line", "requirement"])
def test_the_evaluator_reads_a_line_of_plain_totals_as_before(line, read):
    efc = importlib.import_module("agents.eval_figure_check")
    assert {str(v) for v in efc._engine_totals(line)} == read
