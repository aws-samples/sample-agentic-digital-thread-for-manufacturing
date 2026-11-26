# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-row total columns and a few more shapes of a stated count are read.

Measured on an analytics deployment: a system agent ran one count grouped by
day and by group, then wrote a table with one column per group and a last
column headed "Daily Total" holding each day's sum. It misadded most of the
days, and wrote "The highest daily count was N" with one of the misadded
figures. The figure check never read either: a header that reduces to a
period word ("daily") is not a count header, and "count was N" is not a read
shape. With the column headed "Count" the same wrong days were named. In
another call it wrote "contains N failed segments across M cells" and
"<value>: N total across all cells", neither of which was read.

Pinned here, each on its own:

* a header made of a period word (hourly, daily, weekly, monthly, quarterly,
  yearly, annual) and a total word or one count noun is a count header; a
  period word alone, or with any other word (an average, a rate, a unit), is
  not;
* the misadded cells of such a column are named and its right cells are
  not, as under a "Count" header;
* ``<label> count is/was N`` reads N like ``<label> total was N``; a right
  figure is held and a wrong one named; a label about an average or a rate
  is still not read;
* ``<label>: N total`` (and ``N in total``, ``N overall``) reads N as the
  label's total, through the same label test as ``<label> total: N``;
* ``N <count noun> across ...`` reads N as a total, unless the segment
  scopes it to part of the result.

What counts as support is unchanged. Neutral fixtures, fully offline.
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_DAYS = [f"2031-03-0{d}" for d in range(1, 7)]
_GROUPS = ["grp_a", "grp_b", "grp_c"]
_CELLS = [[410, 233, 167], [388, 251, 140], [402, 219, 171],
          [377, 262, 149], [425, 207, 163], [391, 244, 152]]
_DAY_SUMS = [sum(row) for row in _CELLS]                     # 810, 779, 792, 788, 795, 787
_BY_DAY_GROUP = ("SELECT d, grp, COUNT(*) AS n FROM table_x GROUP BY d, grp "
                 "ORDER BY d, grp LIMIT 500")
#: Days 2 and 5 misadded by 600, as the measured table was.
_WRONG = {1: _DAY_SUMS[1] + 600, 4: _DAY_SUMS[4] + 600}      # 1,379 and 1,395


def _af():
    return importlib.import_module("agents.answer_figures")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


def _result(columns, rows, sql) -> dict:
    body = {**bounded_query_result(columns, rows, False), "_adt_executed_query": sql}
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


def _run(answer, state, question="How many table_x rows were there per day and group?"):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", question))


def _by_day_group():
    rows = [[d, g, str(n)] for d, row in zip(_DAYS, _CELLS) for g, n in zip(_GROUPS, row)]
    return _state_after(_result(["d", "grp", "n"], rows, _BY_DAY_GROUP))


def _figures(out: str) -> str:
    """The figure line, or ``""`` when the answer carries none."""
    if "[engine] Figures in no result" not in out:
        return ""
    start = out.index("[engine] Figures in no result")
    return out[start:].split("Engine totals", 1)[0]


def _pivot(last_header: str, wrong=_WRONG) -> str:
    head = "| Date | " + " | ".join(_GROUPS) + f" | **{last_header}** |"
    rule = "|---" * (len(_GROUPS) + 2) + "|"
    rows = [f"| {d} | " + " | ".join(str(n) for n in row) + f" | **{wrong.get(i, s):,}** |"
            for i, (d, row, s) in enumerate(zip(_DAYS, _CELLS, _DAY_SUMS))]
    return "\n".join([head, rule, *rows]) + "\n"


# --------------------------------------------------------------------------- #
# 1. Header vocabulary
# --------------------------------------------------------------------------- #
def test_a_period_word_with_a_total_word_or_a_count_noun_is_a_count_header():
    af = _af()
    for header in ("Daily Total", "Weekly Total", "Hourly Count",
                   "Daily Count", "Quarterly Records", "Yearly Total Count",
                   "Annual Total", "Total Daily Count", "**Daily Total**"):
        assert af._header_counts(header), header


def test_a_period_word_alone_or_with_another_measure_is_not_a_count_header():
    af = _af()
    for header in ("Daily", "Weekly", "Daily Average", "Daily Rate", "Daily Total (kWh)",
                   "Daily Total Cost", "Monthly Revenue", "Daily %", "Daily Date",
                   "Weekly Avg Count", "Daily Cycle Time"):
        assert not af._header_counts(header), header


# --------------------------------------------------------------------------- #
# 2. A per-row total column of a pivot table
# --------------------------------------------------------------------------- #
def test_the_fixture_names_the_misadded_days_under_a_count_header():
    figures = _figures(_run(_pivot("Count"), _by_day_group()))
    assert "1,379" in figures and "1,395" in figures
    for right in (810, 792, 788, 787):
        assert f"{right}" not in figures, right


def test_the_misadded_cells_of_a_daily_total_column_are_named():
    figures = _figures(_run(_pivot("Daily Total"), _by_day_group()))
    assert "1,379" in figures and "1,395" in figures
    for right in (810, 792, 788, 787):
        assert f"{right}" not in figures, right


def test_a_right_daily_total_column_is_not_named():
    answer = _pivot("Daily Total", wrong={})
    assert _figures(_run(answer, _by_day_group())) == ""


def test_a_daily_average_column_is_still_not_read():
    answer = _pivot("Daily Average")
    assert _figures(_run(answer, _by_day_group())) == ""


# --------------------------------------------------------------------------- #
# 3. "<label> count was N"
# --------------------------------------------------------------------------- #
def test_a_count_was_shape_is_read():
    af = _af()
    assert af.stated_figures("The highest daily count was 1,395 on 2031-03-05.") == ["1,395"]
    assert af.stated_figures("The lowest daily count is 779.") == ["779"]
    assert af.stated_figures("The record count was 412.") == ["412"]


def test_a_wrong_highest_count_is_named_and_a_right_one_is_not():
    wrong = _run("The highest daily count was 1,395 on 2031-03-05.", _by_day_group())
    assert "1,395" in _figures(wrong)
    right = "The highest daily count was 810 on 2031-03-01."
    assert _figures(_run(right, _by_day_group())) == ""


def test_a_count_was_shape_about_an_average_or_rate_is_not_read():
    af = _af()
    for answer in ("The average daily count was 1,395.", "The daily count rate was 1,395.",
                   "The count was 12 percent higher.",
                   "The difference from the earlier 1,000 count is 395 more rows."):
        assert af.stated_figures(answer) == [], answer


# --------------------------------------------------------------------------- #
# 4. "<label>: N total"
# --------------------------------------------------------------------------- #
_BY_GROUP_DAY = ("SELECT grp, d, COUNT(*) AS n FROM table_x GROUP BY grp, d "
                 "ORDER BY grp, d LIMIT 500")


def _by_group_day():
    rows = [[g, d, str(row[i])] for i, g in enumerate(_GROUPS) for d, row in zip(_DAYS, _CELLS)]
    return _state_after(_result(["grp", "d", "n"], rows, _BY_GROUP_DAY))


def test_a_figure_with_total_after_it_is_read_as_its_labels_total():
    af = _af()
    assert af.stated_figures("- grp_a: 2,393 total across all days") == ["2,393"]
    assert af.stated_figures("grp_b: 1,416 in total") == ["1,416"]
    assert af.stated_figures("grp_c: 942 overall") == ["942"]


def test_a_figure_with_total_after_it_still_passes_the_label_test():
    af = _af()
    for answer in ("Average: 2,393 total", "Order ID: 2,393 total", "Share: 12 total",
                   "grp_a: 2,393 (highest)", "grp_a: 2,393"):
        assert af.stated_figures(answer) == [], answer


def test_a_wrong_group_total_after_its_label_is_named_and_a_right_one_is_not():
    wrong = _run("- grp_a: 2,993 total across all days", _by_group_day())
    assert "2,993" in _figures(wrong)
    right = "- grp_a: 2,393 total across all days\n- grp_c: 942 total across all days"
    assert _figures(_run(right, _by_group_day())) == ""


# --------------------------------------------------------------------------- #
# 5. "N <count noun> across ..."
# --------------------------------------------------------------------------- #
def test_a_count_across_the_whole_result_is_read():
    af = _af()
    assert af.stated_figures("The window contains 5,351 failed records across all cells.") \
        == ["5,351"]
    assert af.stated_figures("4,751 rows across all days") == ["4,751"]
    assert af.stated_figures("4,751 rows across every cell") == ["4,751"]


def test_a_count_across_some_number_of_groups_is_not_read():
    """"across 5 sites" may be a subset of the returned sites; the checker
    derives no arbitrary subset sum past a few groups, so a right subtotal
    would be named. Only an all-of-the-result phrase is read."""
    af = _af()
    for answer in ("The window contains 5,351 failed records across 3 cells.",
                   "There were 11,111 records across 5 sites.",
                   "CANCELLED (8,776 segments across multiple segment types)"):
        assert af.stated_figures(answer) == [], answer


def test_a_count_across_part_of_the_result_is_not_read():
    af = _af()
    for answer in ("For grp_a: 2,993 rows across 6 days.",
                   "Only 2,993 rows across the first days.",
                   "On average 793 rows across groups."):
        assert af.stated_figures(answer) == [], answer


def test_a_wrong_count_across_groups_is_named_and_a_right_one_is_not():
    wrong = _run("The window contains 5,351 records across all cells.", _by_day_group())
    assert "5,351" in _figures(wrong)
    right = "The window contains 4,751 records across all cells."
    assert _figures(_run(right, _by_day_group())) == ""


def test_a_right_subtotal_across_some_of_many_groups_is_not_named():
    """Ten groups (past the subset-sum bound), and a
    correct subtotal of five of them written as "across 5 sites"."""
    counts = [10 ** k for k in range(10)]
    rows = [[f"site_{k}", str(n)] for k, n in enumerate(counts)]
    state = _state_after(_result(["site", "n"], rows,
                                 "SELECT site, COUNT(*) AS n FROM table_x GROUP BY site "
                                 "ORDER BY site"))
    out = _run("There were 11,111 records across 5 sites.", state,
               question="How many table_x rows were there per site?")
    assert _figures(out) == ""


# --------------------------------------------------------------------------- #
# 6. Which named figures are shown first
# --------------------------------------------------------------------------- #
def test_a_named_figure_the_answer_states_twice_is_shown_first():
    """Past the shown cap the line names a few figures and counts the rest. A
    figure the answer states more than once (a table cell it then quotes in
    its prose) is the one most likely to be carried on, so it is shown, and
    required, first; the others keep their order."""
    af = _af()
    found = [(s, ()) for s in ("101", "202", "303", "404", "505", "606", "707", "808")]
    answer = ("| Group | Count |\n|---|---|\n| a | 101 |\n| b | 202 |\n| h | 808 |\n"
              "The highest count was 808.")
    ordered = af._shown_first(found, answer)
    assert [s for s, _ in ordered] == ["808", "101", "202", "303", "404", "505", "606", "707"]
    # Order is unchanged when no figure repeats, and a repeated digit run inside
    # a longer number does not count.
    plain = "| Group | Count |\n|---|---|\n| a | 101 |\n| b | 1,101 |"
    assert af._shown_first(found, plain) == found


def test_a_date_or_identifier_holding_the_digits_is_not_a_second_statement():
    """Small day counts equal to parts of the dates beside them (``10`` in
    ``2031-03-10``) are not stated twice; the one figure the prose copies is
    still shown first, past the shown cap."""
    af = _af()
    found = [(s, ()) for s in ("10", "11", "12", "13", "14", "15", "808")]
    rows = "\n".join(f"| 2031-03-{d} | {d} |" for d in range(10, 16))
    answer = (f"| Date | Daily Total |\n|---|---|\n{rows}\n| 2031-03-16 | 808 |\n"
              "The highest count was 808. Batch A10B and v11 ran on 10/12/2031.")
    assert [s for s, _ in af._shown_first(found, answer)][0] == "808"
    assert [s for s, _ in af._shown_first(found, answer)][1:] == ["10", "11", "12", "13", "14", "15"]


@pytest.mark.parametrize("prose", ["The highest count was 808.", "Highest count:808."])
def test_a_numeric_id_column_is_not_a_second_statement_but_a_compact_label_is(prose):
    """A repeat is a second place the figure reader reads the number as a
    count: not an identifier column holding the same digits, and yes a
    labelled count written with no space after its colon."""
    af = _af()
    found = [(s, ()) for s in ("10", "11", "12", "13", "14", "15", "808")]
    rows = "\n".join(f"| {d} | {d} |" for d in range(10, 16))
    answer = f"| Order ID | Daily Total |\n|---|---|\n{rows}\n| 99 | 808 |\n{prose}"
    assert [s for s, _ in af._shown_first(found, answer)][0] == "808"


@pytest.mark.parametrize("repeat", [
    "Highest count: 808. Confirmed count: 808.",
    "808 records total. 808 records total.",
    "Highest count: 808. 808 records total.",
])
def test_two_count_statements_on_one_line_are_two_and_the_figure_is_required(repeat):
    """Two sentences on one line can each state the figure as a count, by the
    same reader or by two; that is a repeat, so past the shown cap it is still
    named and required."""
    af = _af()
    state = _state_after(_result(["grp", "n"], [["a", "1"], ["b", "2"], ["c", "3"]],
                                 "SELECT grp, COUNT(*) AS n FROM table_x GROUP BY grp"))
    answer = "\n".join(f"Count: {n}." for n in range(20, 26)) + "\n" + repeat
    lines = af.engine_lines(trc.collect_sidecar_statements(state), af.result_values(state), answer)
    figure = next(line for line in lines if "Figures in no result" in line)
    required = next(line for line in lines if "REQUIRED" in line)
    assert "808" in figure.split(" in this answer")[0]
    assert "808" in required.split(" as data")[0]


def test_one_statement_both_readers_read_counts_once():
    """A statement the labelled and the phrased reader both read is one
    statement, not a repeat."""
    af = _af()
    lines = af._lines("Total: 808 records total.")
    labelled = {s for s, _span in af._labelled_matches(lines[0])}
    phrased = {s for s, _b, _span in af._total_phrase_matches(lines[0])}
    assert "808" in labelled | phrased
    assert af._statement_counts(lines)["808"] == 1
