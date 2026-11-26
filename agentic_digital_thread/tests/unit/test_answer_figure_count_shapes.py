# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The in-app figure check reads the count shapes the evaluator reads.

The engine's in-answer check (``answer_figures.stated_figures`` /
``unfound_figures``, run inside each system agent over its own query results)
read a figure only when the answer labelled it a total or count, or placed it
under a one-word count header. Real answers mostly state counts without that
label: a count noun after the number (``54,288 segment records``), a label
that names a count noun with no total word (``FAILED segments: 13,656``), or a
cell under a multi-word count header (``Record Count``). The AgentCore
evaluator (``eval_figure_check``) grew its own reader for those shapes; this
pins that the ONE reader now lives in ``answer_figures`` and the in-app check
reads them too, through the SAME support tests.

Pinned here (neutral fixtures, fully offline):

* each of the three shapes, when the figure is in no result, is NAMED by the
  in-app check (``stated_figures`` reads it; ``unfound_figures`` keeps it);
* a RIGHT figure in each shape (a value a result returned) is NOT named;
* every existing skip still holds for the new shapes: a percentage, a decimal,
  a date, a duration, an average/rate/share word, an identifier label, and a
  plural subject in the gap.

These tests FAIL on dedb4d03, where the count-shape reader lived only in
``eval_figure_check`` and the in-app ``stated_figures``/``unfound_figures``
never saw these shapes.
"""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

import agents.tool_result_capture as trc
from agents.response_integrity_query import bounded_query_result, strict_json_dumps


def _af():
    return importlib.import_module("agents.answer_figures")


def _result(columns, rows, sql, *, truncated=False) -> dict:
    body = {**bounded_query_result(columns, rows, truncated), "_adt_executed_query": sql}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _state_after(*results) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result in results:
        capture._on_after_tool_call(SimpleNamespace(
            selected_tool=None,
            tool_use={"toolUseId": "t-1", "name": "query_public", "input": {"sql": "SELECT 1"}},
            invocation_state=state, result=result, exception=None, retry=False,
            agent=SimpleNamespace(messages=[])))
    return state


# A grouped result whose rows sum to 12,000 (so 12,000 is a RIGHT whole-column
# sum) and whose cells are 2,000 and 2,500 (RIGHT values). 13,656 is in none.
_GROUPED = "SELECT grp, COUNT(*) AS n FROM table_x GROUP BY grp ORDER BY grp LIMIT 500"
_ROWS = [["grp_a", "2000"], ["grp_b", "2500"], ["grp_c", "3000"], ["grp_d", "4500"]]


def _result_sum_12000() -> dict:
    return _result(["grp", "n"], _ROWS, _GROUPED)


def _named(answer: str, state: dict, question: str = "How many table_x rows?") -> list[str]:
    af = _af()
    stmts = trc.collect_sidecar_statements(state)
    values = af.result_values(state)
    return [s for s, _sums in af.unfound_figures(
        answer, values, af.engine_totals(stmts), question, stmts)]


# --------------------------------------------------------------------------- #
# 1. stated_figures reads each shape
# --------------------------------------------------------------------------- #
def test_stated_reads_count_noun_after_the_number():
    # Shape: N <count noun>. No total word, no colon.
    assert "13,656" in _af().stated_figures("The run produced 13,656 segment records.")


def test_stated_reads_a_label_naming_a_count_noun():
    # Shape: <label naming a count noun>: N. No total word in the label.
    assert "13,656" in _af().stated_figures("FAILED segments: 13,656")


def test_a_cell_under_a_multi_word_count_header_is_read_by_the_evaluator_only():
    # Shape: a cell under a header that names a count noun the way a label
    # does (Record Count), which is not a one-word count header. Which rows a
    # table's count is for (a picked set of groups, a rank, a code) cannot be
    # told from the row text reliably, so the in-app check leaves these cells
    # to the evaluator, as before.
    answer = "| State | Record Count |\n| --- | --- |\n| FAILED | 13,656 |"
    assert "13,656" not in _af().stated_figures(answer)
    efc = importlib.import_module("agents.eval_figure_check")
    assert "13,656" in efc.read_figures(answer)


# --------------------------------------------------------------------------- #
# 2. unfound_figures NAMES a wrong figure in each shape
# --------------------------------------------------------------------------- #
def test_wrong_count_noun_figure_is_named():
    answer = "Across the window the system found 13,656 segment records."
    assert _named(answer, _state_after(_result_sum_12000())) == ["13,656"]


def test_wrong_labelled_count_noun_figure_is_named():
    answer = "FAILED segments: 13,656"
    assert _named(answer, _state_after(_result_sum_12000())) == ["13,656"]


def test_a_wrong_count_header_cell_is_not_named_in_app():
    answer = "| State | Record Count |\n| --- | --- |\n| FAILED | 13,656 |"
    assert _named(answer, _state_after(_result_sum_12000())) == []


def test_the_engine_line_names_a_wrong_count_shape_figure():
    answer = "The system counted 13,656 segment records in the window."
    state = _state_after(_result_sum_12000())
    af = _af()
    stmts = trc.collect_sidecar_statements(state)
    lines = af.engine_lines(stmts, af.result_values(state), answer, "How many?")
    assert any("13,656" in line and af.FIGURE_PREFIX in line for line in lines)


# --------------------------------------------------------------------------- #
# 3. a RIGHT figure in each shape is NOT named
# --------------------------------------------------------------------------- #
def test_a_right_count_noun_figure_is_not_named():
    # 12,000 is the whole-column sum of the returned rows.
    answer = "The system found 12,000 segment records in total across the groups."
    assert _named(answer, _state_after(_result_sum_12000())) == []


def test_a_right_labelled_count_noun_figure_is_not_named():
    # 2,500 is a returned cell.
    answer = "Segments in grp_b: 2,500"
    assert _named(answer, _state_after(_result_sum_12000())) == []


def test_a_right_count_header_cell_is_not_named():
    # Every cell shown is a returned value.
    answer = ("| Group | Record Count |\n| --- | --- |\n"
              "| grp_a | 2,000 |\n| grp_b | 2,500 |")
    assert _named(answer, _state_after(_result_sum_12000())) == []


# --------------------------------------------------------------------------- #
# 4. the existing skips still hold for the new shapes
# --------------------------------------------------------------------------- #
def test_percentage_in_count_noun_shape_is_not_read():
    assert _af().stated_figures("Scrap was 42% segments of the run.") == []


def test_decimal_in_count_noun_shape_is_not_read():
    assert _af().stated_figures("The run logged 13.6 records per minute.") == []


def test_a_date_label_is_not_read_as_a_count():
    # An identifier/date label is not a count label even with a colon.
    assert "20260115" not in _af().stated_figures("Run date key: 20260115")


def test_a_rate_label_is_not_read_as_a_count():
    assert _af().stated_figures("Average records: 13,656") == []


def test_a_plural_subject_in_the_gap_is_not_a_count_of_the_noun():
    # "3 sites reported ... records" counts sites, not records: the noun is
    # not the head of the number's phrase.
    assert "3" not in _af().stated_figures("In the window 3 sites reported segment records.")


def test_a_duration_in_count_noun_shape_is_not_read():
    assert _af().stated_figures("The job ran 13 minutes of records.") == []


# --------------------------------------------------------------------------- #
# 5. A scale word makes the figure approximate (shared reader)
# --------------------------------------------------------------------------- #
# "109 million records" rounds 108,929,936; read as the bare 109 it was named.
_SCALED = ("- FAILED, segments that failed (109 million records)",
           "The window held 95 M records.",
           "About the backlog: 3 thousand units waited.",
           "It logged 12 bn records.",
           "It logged 4 mn rows.",
           "It logged 7 K records.",
           "It logged 2 Billion records.",
           "FAILED records: 109 million")


@pytest.mark.parametrize("answer", _SCALED)
def test_a_figure_with_a_scale_word_is_not_read_in_app(answer):
    assert _af().stated_figures(answer) == [], answer


@pytest.mark.parametrize("answer", _SCALED)
def test_a_figure_with_a_scale_word_is_not_read_by_the_evaluator(answer):
    efc = importlib.import_module("agents.eval_figure_check")
    assert efc.read_figures(answer) == [], answer


def test_a_scale_word_figure_is_not_named():
    answer = "- FAILED, segments that failed (109 million records)"
    assert _named(answer, _state_after(_result_sum_12000())) == []


def test_a_plain_count_beside_a_scaled_one_is_still_read():
    answer = "It logged 13,656 records and 2 million rows."
    assert _af().stated_figures(answer) == ["13,656"]


# The labelled and phrased TOTAL readers share the same rule: "Total records:
# 109 million" rounds a larger count, so it is not read as the bare 109.
_SCALED_TOTALS = ("Total records: 109 million",
                  "The total is 95 million records.",
                  "Grand total: 12 bn",
                  "| Total | 3 thousand |")


@pytest.mark.parametrize("answer", _SCALED_TOTALS)
def test_a_labelled_total_with_a_scale_word_is_not_read(answer):
    assert _af().stated_figures(answer) == [], answer


def test_a_labelled_total_with_a_scale_word_is_not_named():
    assert _named("Total records: 109 million", _state_after(_result_sum_12000())) == []


def test_a_labelled_total_written_in_full_is_still_read_and_named():
    answer = "Total records: 109,000,000"
    assert _af().stated_figures(answer) == ["109,000,000"]
    assert _named(answer, _state_after(_result_sum_12000())) == ["109,000,000"]


def test_a_labelled_total_followed_by_a_word_starting_like_a_scale_is_still_read():
    # "more", "both", "kept" start with m / b / k: not a scale word.
    answer = "Total records: 13,656 more than last week."
    assert _af().stated_figures(answer) == ["13,656"]


def test_a_letter_joined_by_an_ampersand_is_not_a_scale_word():
    # "7 B&W" is seven black-and-white items, not seven billion.
    assert _af().stated_figures("Total units: 7 B&W") == ["7"]


# A one-letter K / M / B is also a unit (metres, Kelvin, bytes). The total
# readers read a figure before one exactly as they did before; only the count
# shapes, which name what is counted, take it as a scale.
_UNIT_TOTALS = ("Total travel distance: 13,656 m",
                "Overall operating temperature: 13,656 K",
                "Total storage: 13,656 B",
                "The total is 13,656 m.",
                "For the 12,000 records, total travel distance: 13,656 m.",
                "Total travel distance: 13,656 m for archived records.")


@pytest.mark.parametrize("answer", _UNIT_TOTALS)
def test_a_one_letter_unit_after_a_total_is_still_read(answer):
    assert "13,656" in _af().stated_figures(answer), answer


@pytest.mark.parametrize("answer", _UNIT_TOTALS)
def test_a_wrong_total_with_a_one_letter_unit_is_still_named(answer):
    assert _named(answer, _state_after(_result_sum_12000())) == ["13,656"], answer


# "bn" / "mn" are scales only in lower case: "mN" and "MN" are millinewtons and
# meganewtons.
@pytest.mark.parametrize("answer", ("Total force: 13,656 mN", "Total force: 13,656 MN",
                                    "Total load: 13,656 BN"))
def test_a_unit_spelled_like_bn_or_mn_is_still_read_and_named(answer):
    assert _af().stated_figures(answer) == ["13,656"], answer
    assert _named(answer, _state_after(_result_sum_12000())) == ["13,656"], answer


# --------------------------------------------------------------------------- #
# 6. A label naming two or more groups is a picked-subset scope
# --------------------------------------------------------------------------- #
# Ten groups, past the subset-sum bound, so a correct sum of three named groups
# is no derived value: read, it would be named.
_TEN = "SELECT code, COUNT(*) AS n FROM table_x GROUP BY code ORDER BY code"


def _ten_codes() -> dict:
    rows = [[f"code_{k}", str(10 ** k)] for k in range(10)]
    return _result(["code", "n"], rows, _TEN)


_PICKED = ("- Cancellations (code_1, code_2, code_3): 1,110 segments (9.9%)",
           "Codes code_1, code_2 and code_3: 1,110 records",
           "Codes code_1 and code_2 and code_3: 1,110 records",
           "Cancellations (code_1; code_2 or code_3) logged 1,110 segments.",
           "The codes code_1, code_2, and code_3 logged 1,110 records.",
           "There are 1,110 records in code_1, code_2, and code_3.",
           "The run logged 1,110 records for code_1, code_2 or code_3 combined.")


@pytest.mark.parametrize("answer", _PICKED)
def test_a_count_for_a_listed_set_of_groups_is_not_read_in_app(answer):
    assert "1,110" not in _af().stated_figures(answer), answer


@pytest.mark.parametrize("answer", _PICKED)
def test_a_right_sum_of_listed_groups_is_not_named(answer):
    assert _named(answer, _state_after(_ten_codes())) == [], answer


def test_the_evaluator_still_reads_a_listed_group_count():
    # The picked-list scope is the in-app reader's; the evaluator reads every
    # shape and lets its own value view decide.
    efc = importlib.import_module("agents.eval_figure_check")
    assert "1,110" in efc.read_figures(_PICKED[0])


def test_a_single_named_group_is_still_read_and_named_when_wrong():
    answer = "Widgets (blue): 40 units"
    assert _af().stated_figures(answer) == ["40"]
    assert _named(answer, _state_after(_result_sum_12000())) == ["40"]


def test_a_single_named_group_with_a_right_figure_is_not_named():
    assert _named("Widgets (grp_b): 2,500 units", _state_after(_result_sum_12000())) == []


# Only a serial list after the noun (a, b and c) scopes the count: one group, or
# a later clause joined by "and", does not.
@pytest.mark.parametrize("answer", ("There are 13,656 records in code_1.",
                                    "There are 13,656 records in the window, and most failed.",
                                    "There are 13,656 records in rework and scrap."))
def test_a_count_without_a_serial_list_after_it_is_still_named(answer):
    assert _named(answer, _state_after(_ten_codes())) == ["13,656"], answer


def test_a_word_joined_by_underscores_is_not_a_list():
    # "A_AND_B" is one name, not "A and B".
    answer = "SHADE_AND_TONE (13,656 failed records)"
    assert _af().stated_figures(answer) == ["13,656"]


# A table count is never read in-app under a multi-word count header, so no
# arrangement of the row (a picked group list after a rank, a code or a numeric
# id; a description with a parenthetical) can turn a right subtotal into a
# named figure.
_PICKED_ROWS = ("| Groups | Record Count |\n| --- | --- |\n| code_1, code_2, and code_3 | 1,110 |",
                "| Groups | Record Count |\n| --- | --- |\n| (code_1, code_2, code_3) | 1,110 |",
                "| Status | Groups | Record Count |\n| --- | --- | --- |\n"
                "| selected | code_1, code_2, and code_3 | 1,110 |",
                "| Rank | Groups | Record Count |\n| --- | --- | --- |\n"
                "| 1 | code_1, code_2, and code_3 | 1,110 |",
                "| Id | Groups | Record Count |\n| --- | --- | --- |\n"
                "| 4017 | code_1, code_2 and code_3 | 1,110 |")


@pytest.mark.parametrize("answer", _PICKED_ROWS)
def test_a_right_sum_of_groups_in_a_table_row_is_not_named(answer):
    assert _named(answer, _state_after(_ten_codes())) == [], answer


# --------------------------------------------------------------------------- #
# 7. Skip when unsure: shapes whose figure is not an exact count of the noun
# --------------------------------------------------------------------------- #
def _result_total_12() -> dict:
    return _result(["n"], [["12"]], "SELECT COUNT(*) AS n FROM table_x")


def test_a_quoted_name_starting_with_a_scale_word_does_not_hide_a_total():
    # "Thousand Pines" is a name, not a scale: base reads and names the 13.
    answer = 'Total sites: 13 "Thousand Pines" locations.'
    assert _named(answer, _state_after(_result_total_12())) == ["13"]


@pytest.mark.parametrize("answer", ("Version 2 records remain compatible.",
                                    "Type 2 records are kept apart.",
                                    "Line 2 production records were checked.",
                                    "Phase 3 records closed."))
def test_a_classifier_number_is_not_read_as_a_count(answer):
    assert _af().stated_figures(answer) == [], answer


@pytest.mark.parametrize("answer", ("There are between 100 and 200 records.",
                                    "A minimum of 100 records matched.",
                                    "A maximum of 200 records matched.",
                                    "Rounded to the nearest hundred: 100 records matched.",
                                    "An estimate of 100 records matched."))
def test_a_bound_or_rounded_count_is_not_read(answer):
    assert _af().stated_figures(answer) == [], answer


@pytest.mark.parametrize("answer", ("Dividing 2,400 by 24 gives 100 records.",
                                    "The line averaged 100 records each hour.",
                                    "Thirty batches at 3 each produce 90 records.",
                                    "That is 4 times 25, or 100 records.",
                                    "The line logs 100 records daily."))
def test_a_calculated_or_rate_count_is_not_read(answer):
    assert _af().stated_figures(answer) == [], answer


@pytest.mark.parametrize("answer", ("There are 1,110 records (code_1, code_2, and code_3).",
                                    "There are 1,110 records in code_1, code_2, code_3.",
                                    "There are 1,110 records in code_1; code_2; and code_3.",
                                    "Code_1 and code_2 and code_3 yield 1,110 records."))
def test_more_picked_list_spellings_do_not_name_a_right_subtotal(answer):
    assert _named(answer, _state_after(_ten_codes())) == [], answer


@pytest.mark.parametrize("answer", (
        "There are 13,656 records in the window, up on last week, and most failed.",
        "In the window 13,656 records failed and were retried."))
def test_a_long_clause_with_commas_is_not_a_picked_list(answer):
    assert _named(answer, _state_after(_ten_codes())) == ["13,656"], answer


def test_a_figure_the_old_readers_name_keeps_its_visible_slot():
    # Six new count-shape figures before an old labelled total must not push
    # the old figure out of the visible engine line.
    answer = "\n".join([f"FAILED records: 20,00{k}" for k in range(6)] + ["Total widgets: 30,000"])
    state = _state_after(_result_sum_12000())
    af = _af()
    stmts = trc.collect_sidecar_statements(state)
    found = af.unfound_figures(answer, af.result_values(state), af.engine_totals(stmts),
                               "How many?", stmts)
    assert [row[0] for row in found][0] == "30,000"
    lines = af.engine_lines(stmts, af.result_values(state), answer, "How many?")
    assert any("30,000" in line and af.FIGURE_PREFIX in line for line in lines)
