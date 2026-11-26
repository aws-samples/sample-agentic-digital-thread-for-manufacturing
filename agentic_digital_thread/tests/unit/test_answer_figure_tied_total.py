# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A total written beside a result's rows is that result's total.

Measured on an analytics deployment: a system agent ran one grouped count per
day, then wrote a day's total that was not the sum of that day's rows (the
engine total) and listed that day's leading rows under it. The figure check
let it pass, because the wrong figure happened to be the sum of two values
returned elsewhere in the call (two cells of the other day's result, or the
first day's total plus one of the other day's cells).

When the rows a prose total is written beside (the list or table the
listed-sum test already reads) reproduce one complete grouped result's count
or sum column, its leading rows in order or all of them, the figure is stated
as that result's total. If it differs from that total, only that result's own
rows and row count, the engine's exact totals and the question's own figures
can still hold it; a value, a sum or a difference that brings in values of
other results does not.

Pinned here, each on its own:

* the measured shape (a heading with the day's total, then its leading rows)
  is named, and so is a total line under a table of all of a result's rows;
* one key's sum across both days (a vouch sum) does not pair with a listed
  row to hold such a figure;
* still held: the result's own total; a sum of two engine totals (both days
  together); a sum of two of the listed result's own rows; a figure written
  beside no result's rows; a figure scoped to part of the result; a table
  count cell (a merged row), which is never tied to a result;
* a listing of two rows, or rows out of order, ties nothing;
* a listing ties only a result whose keys it writes beside each count: an
  unrelated result with equal leading counts neither ties nor holds the
  figure, a listing with a result's counts but other keys ties nothing, and
  rows sharing a count may be listed in any order among themselves;
* when several results return the listed rows, one of them alone must hold
  the figure: a cell of each does not;
* a running, partial or filtered total (correct for some of the rows of a
  result with more rows than every subset sum is kept for) is never tied,
  read from the figure's own clause, so a clause past it that only says
  where the rows are leaves it tied;
* a tied figure is checked against its result alone: another result's
  value, rounding, the call's row count, the question's range and dates do
  not hold it; another result's engine total and the listed result's own
  row count still do;
* nor does a sum or difference of the result's total and one of its own
  values, or a difference of two of its cells: its total less or plus one
  of its rows, or less two of them, is named, even when another result has
  the same total; a sum of two of its cells, a sum with another result's
  engine total (one of its cells equal to such a total is read as it), a
  running subtotal and a figure beside no result's rows are held as before;
* past a cut result, a tied figure the cut result cannot be the total of is
  named, and one it may be the total of is not;
* the figure line names the query whose rows a figure named this way is
  written beside and its engine total, never says no result holds it or
  that it is no sum of two returned values, and keeps to its bound; its
  requirement says not to state it as that result's total;
* a result's cells and row keys are kept on its totals record only up to
  the listing bound, and keeping them changes no totals line.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_CODES = [f"code_{c}" for c in "abcdefghij"]
_DAY_ONE = [41, 23, 9, 7, 6, 5, 4, 3, 2, 1]                     # sums to 101
_DAY_TWO = [512, 230, 98, 77, 60, 44, 38, 26, 17, 11]           # sums to 1,113


def _sql(day: str) -> str:
    return ("SELECT fault_code, COUNT(*) AS n_events FROM fault_log "
            f"WHERE logged_at >= TIMESTAMP '2031-03-{day} 00:00:00' "
            f"AND logged_at < TIMESTAMP '2031-03-{int(day) + 1:02d} 00:00:00' "
            "GROUP BY fault_code ORDER BY n_events DESC LIMIT 500")


def _af():
    return importlib.import_module("agents.answer_figures")


def _grouped(tool_id: str, day: str, counts) -> dict:
    rows = [[code, str(n)] for code, n in zip(_CODES, counts)]
    body = {**bounded_query_result(["fault_code", "n_events"], rows, False),
            "_adt_executed_query": _sql(day)}
    return {"toolUseId": tool_id, "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _event(state, result):
    from types import SimpleNamespace
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": result["toolUseId"], "name": "query_public",
                  "input": {"sql": "SELECT 1"}},
        invocation_state=state, result=result, exception=None, retry=False,
        cancel_message=None, agent=SimpleNamespace(messages=[]))


def _state_after(*results) -> dict:
    from types import SimpleNamespace
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result in results:
        capture._on_after_tool_call(_event(state, result))
    return state


def _two_days() -> dict:
    return _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO))


_QUESTION = "How many fault_log events were there per fault code on each of the two days?"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


def _run(answer, state, question=_QUESTION):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", question))


def _named(answer, state, question=_QUESTION) -> list[str]:
    af = _af()
    statements = trc.collect_sidecar_statements(state)
    found = af.unfound_figures(answer, af.result_values(state), af.engine_totals(statements),
                               question, statements)
    return [spelling for spelling, _sums in found]


def _leading(counts, count=5) -> str:
    return "\n".join(f"- {code}: {n}" for code, n in zip(_CODES, counts[:count]))


def _table(counts) -> str:
    rows = "\n".join(f"| {code} | {n} |" for code, n in zip(_CODES, counts))
    return f"| Fault code | Events |\n|---|---|\n{rows}"


# --------------------------------------------------------------------------- #
# A wrong total beside a result's rows is named
# --------------------------------------------------------------------------- #
#: 118 is day one's total (101) plus a day-two cell (17), and a day-two cell
#: plus a day-one cell (77 + 41): a pair of returned values, not day one's total.
_WRONG = "118"


def _measured(figure: str = _WRONG) -> str:
    return (f"### Day one (10 distinct fault codes, {figure} total events):\n"
            f"**Leading faults:**\n{_leading(_DAY_ONE)}\n\n"
            f"### Day two (10 distinct fault codes, 1,113 total events):\n"
            f"**Leading faults:**\n{_leading(_DAY_TWO)}\n")


def test_the_measured_heading_total_beside_leading_rows_is_named():
    state = _two_days()
    assert _named(_measured(), state) == [_WRONG]
    answer = _run(_measured(), state)
    assert f"[engine] Figures in no result as stated: {_WRONG} in this answer" in answer


def test_a_total_line_under_a_table_of_all_rows_is_named():
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n**Total events, day one: {_WRONG}**"
    assert _named(answer, _two_days()) == [_WRONG]


def test_one_keys_sum_across_both_days_does_not_pair_with_a_listed_row():
    """112 is code_c's sum over both days (9 + 98, a vouch sum) plus a day-one
    cell (5), and day one's total plus a day-two cell (101 + 11)."""
    from decimal import Decimal
    af = _af()
    state = _two_days()
    assert Decimal(107) in af.cross_result_vouch_sums(trc.collect_sidecar_statements(state))
    assert _named(_measured("112"), state) == ["112"]


def test_the_figure_line_does_not_call_a_tied_figure_no_sum_of_two_values():
    answer = _run(_measured(), _two_days())
    line = next(text for text in answer.splitlines()
                if text.startswith("[engine] Figures in no result as stated: "))
    assert "a sum or difference of two returned values" not in line
    assert (f"{_WRONG} in this answer is written as a total beside rows Q1 returns, and "
            "Q1's engine total is 101.") in line


def test_an_untied_figure_keeps_the_figure_line_wording():
    answer = _run("Day one is counted.\n\nTotal events: 9,999", _two_days())
    assert ("but none of this call's results holds it, and it is not an engine total or a "
            "sum or difference of two returned values.") in answer
    assert "written beside the rows of a result" not in answer


# --------------------------------------------------------------------------- #
# What still holds a figure
# --------------------------------------------------------------------------- #
def test_the_results_own_total_is_held():
    assert _named(_measured("101"), _two_days()) == []


def test_a_sum_of_two_engine_totals_beside_one_days_rows_is_held():
    answer = (f"Both days together: 1,214 total events.\n\n### Day one leading codes\n"
              f"{_leading(_DAY_ONE, 3)}\n")
    assert _named(answer, _two_days()) == []


def test_a_sum_of_two_of_the_listed_results_own_rows_is_held():
    answer = f"Codes code_a and code_b: 64 total events\n{_leading(_DAY_ONE)}\n"
    assert _named(answer, _two_days()) == []


def test_a_pair_sum_beside_no_results_rows_is_still_held():
    answer = f"Day one is counted.\n\n**Total events: {_WRONG}**"
    assert _named(answer, _two_days()) == []


def test_a_figure_scoped_to_part_of_the_result_is_not_tied():
    answer = f"Total events for the codes below: {_WRONG}\n{_leading(_DAY_ONE)}\n"
    assert _named(answer, _two_days()) == []


def test_a_table_count_cell_is_never_tied_to_a_result():
    rows = "\n".join(f"| {code} | {n} |" for code, n in zip(_CODES[:3], _DAY_ONE[:3]))
    answer = (f"| Fault code | Events |\n|---|---|\n{rows}\n"
              f"| code_x (two spellings) | {_WRONG} |\n")
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("listing", [
    _leading(_DAY_ONE, 2),
    "\n".join(f"- {code}: {n}" for code, n in zip(_CODES, [23, 41, 9, 7, 6])),
])
def test_two_rows_or_rows_out_of_order_tie_nothing(listing):
    answer = f"### Day one ({_WRONG} total events):\n{listing}\n"
    assert _named(answer, _two_days()) == []


# --------------------------------------------------------------------------- #
# A listing ties only a result whose keys and counts it reproduces
# --------------------------------------------------------------------------- #
_OWN_KEYS = ["code_a", "code_b", "code_c", "code_d"]
_OTHER_KEYS = ["code_w", "code_x", "code_y", "code_z"]


def _keyed(tool_id: str, line: int, keys, counts) -> dict:
    sql = ("SELECT fault_code, COUNT(*) AS n_events FROM fault_log "
           f"WHERE line_id = {line} GROUP BY fault_code ORDER BY n_events DESC LIMIT 500")
    rows = [[key, str(n)] for key, n in zip(keys, counts)]
    body = {**bounded_query_result(["fault_code", "n_events"], rows, False),
            "_adt_executed_query": sql}
    return {"toolUseId": tool_id, "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def test_another_results_equal_leading_counts_do_not_hold_a_wrong_total():
    """106 is 99 + 7: a cell of the other line's result plus one of the listed
    result's rows, not the listed result's total (80). The other result's
    leading counts equal the listed ones in the first state and not in the
    second; its keys are different in both, so it is tied in neither."""
    listing = "\n".join(f"- {key}: {n}" for key, n in zip(_OWN_KEYS, [41, 23, 9]))
    answer = f"Line one:\n{listing}\n\nTotal events: 106"
    for first in (41, 42):
        state = _state_after(_keyed("t-1", 1, _OWN_KEYS, [41, 23, 9, 7]),
                             _keyed("t-2", 2, _OTHER_KEYS, [first, 23, 9, 99]))
        assert _named(answer, state) == ["106"], first


def test_a_listing_with_a_results_counts_but_not_its_keys_ties_nothing():
    listing = "\n".join(f"- station_{i}: {n}" for i, n in enumerate(_DAY_ONE[:5]))
    answer = f"### Day one ({_WRONG} total events):\n{listing}\n"
    assert _named(answer, _two_days()) == []


def test_equal_counts_listed_in_another_order_still_tie():
    """Rows that share a count may be listed in any order among themselves.
    Preserved behavior: this also holds before the listing's keys are read."""
    counts = [41, 23, 9, 9, 9, 6, 5, 4, 3, 2]
    state = _state_after(_grouped("t-1", "04", counts), _grouped("t-2", "11", _DAY_TWO))
    listing = "\n".join(f"- {_CODES[i]}: {counts[i]}" for i in (0, 1, 4, 2))
    answer = f"### Day one ({_WRONG} total events):\n{listing}\n"
    assert _named(answer, state) == [_WRONG]


# --------------------------------------------------------------------------- #
# A running, partial or filtered total is not the result's total
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label", [
    "Running total after fourth row: 80",
    "Cumulative total through the fourth row: 80",
    "Total so far: 80",
    "Sub-total: 80",
    "Total events with more than 5 each: 86",
])
def test_a_correct_running_or_filtered_subtotal_beside_all_rows_is_held(label):
    """80 is the sum of the leading four rows and 86 of the rows above 5: true
    subtotals of a result with more rows than every subset sum is kept for."""
    af = _af()
    assert len(_DAY_ONE) > af.MAX_SUBSET_VALUES
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n{label}"
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("label", [
    "Total events: 118, with the full breakdown shown below:",
    "Total events: 118. The full breakdown, with each code, is shown below:",
    "Total events: 118 (shown below)",
])
def test_a_clause_that_only_presents_the_listing_leaves_the_total_tied(label):
    """118 is no total of day one (101); the words after it say where its rows
    are, not that it totals part of them."""
    answer = f"Day one.\n\n{label}\n{_leading(_DAY_ONE)}\n"
    assert _named(answer, _two_days()) == [_WRONG]


@pytest.mark.parametrize("label", [
    "Total events: 80 after the fourth row",
    "Codes with more than 5 events, total: 86",
])
def test_a_running_or_filtered_qualifier_on_either_side_of_the_figure_is_held(label):
    """Preserved behavior: the qualifier is in the figure's own clause."""
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n{label}"
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("label", [
    "Total events: 80, after the fourth row",
    "Total events: 80 - after the fourth row",
    "Total events: 80, through the fourth row",
    "Total events: 80, so far",
    "Total events: 80 \u2014 up to the fourth row",
    "Total events: 80, after the fourth row shown below",
    "Total events: 80 - through the fourth row listed below",
])
def test_a_qualifier_right_after_a_comma_or_dash_still_scopes_the_figure(label):
    """80 is the sum of day one's first four rows: a running qualifier that
    begins the next clause still makes it a subtotal."""
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n{label}"
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("label", [
    "Total events: 118, with the full breakdown shown below",
    "Total events: 118 - with each code listed below",
])
def test_a_later_clause_that_does_not_qualify_the_figure_leaves_it_tied(label):
    """118 is no total of day one; a clause after a comma or dash that only
    presents the listing does not scope it to part of the rows."""
    answer = f"Day one.\n\n{label}\n{_leading(_DAY_ONE)}\n"
    assert _named(answer, _two_days()) == [_WRONG]


@pytest.mark.parametrize("label", [
    "Total events: 118, only the leading codes are shown below",
    "Total events: 118 \u2014 only the leading codes are listed below",
    "Total events: 118 - only the top codes appear above",
])
def test_a_segment_with_a_subset_or_rank_word_is_scoped_as_before(label):
    """Preserved behavior, not this branch's: a segment holding "only" or a
    ranking word ("leading", "top") is read as scoped to part of a result
    (``_scoped_spellings``), so its figure is never stated as a total. Known
    limit: a wrong full total in such a segment is held."""
    af = _af()
    assert "118" in af._scoped_spellings([label])
    answer = f"Day one.\n\n{label}\n{_leading(_DAY_ONE)}\n"
    assert _named(answer, _two_days()) == []


# --------------------------------------------------------------------------- #
# A tied total is checked against its own result only
# --------------------------------------------------------------------------- #
def _beside_day_one(figure: str) -> str:
    return f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\nTotal events: {figure}"


def _mean(value: str) -> dict:
    body = {**bounded_query_result(["mean_events"], [[value]], False),
            "_adt_executed_query": "SELECT AVG(n_events) AS mean_events FROM fault_daily"}
    return {"toolUseId": "t-3", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def test_a_value_another_result_returns_does_not_hold_a_total_beside_a_results_rows():
    """512 is a cell of the second day's result, not day one's total (101)."""
    assert _named(_beside_day_one("512"), _two_days()) == ["512"]


def test_another_results_rounded_value_does_not_hold_a_total_beside_a_results_rows():
    """250 rounds from 250.4, a value a third result returns."""
    state = _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO),
                         _mean("250.4"))
    assert _named(_beside_day_one("250"), state) == ["250"]


def test_the_questions_range_does_not_hold_a_total_beside_a_results_rows():
    question = "Were there about 1.2k fault_log events per fault code on each of the two days?"
    assert _named(_beside_day_one("1,180"), _two_days(), question) == ["1,180"]


def test_a_date_does_not_hold_a_total_beside_a_results_rows():
    question = "How many fault_log events were there per fault code on 2031-03-04?"
    assert _named(_beside_day_one("20310304"), _two_days(), question) == ["20310304"]


def test_the_calls_row_count_does_not_hold_a_total_beside_a_results_rows():
    """50 is no more than the 63 rows the call returned, but more than the
    three rows of the result it is written beside, whose total is 900."""
    stations = [f"station_{i:02d}" for i in range(60)]
    state = _state_after(_keyed("t-1", 1, _OWN_KEYS[:3], [400, 300, 200]),
                         _keyed("t-2", 2, stations, [5000] * 60))
    listing = "\n".join(f"- {key}: {n}" for key, n in zip(_OWN_KEYS, [400, 300, 200]))
    assert _named(f"Line one:\n{listing}\n\nTotal events: 50", state) == ["50"]


def test_another_results_engine_total_beside_a_results_rows_stays_held():
    """Day two's heading follows day one's rows and gives day two's engine
    total. Preserved behavior."""
    answer = (f"### Day one (101 total events):\n{_leading(_DAY_ONE)}\n\n"
              "### Day two: 1,113 total events\n")
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("label", ["Day one", "Day two"])
def test_a_figure_equal_to_another_querys_engine_total_is_held_whatever_its_label(label):
    """A total that equals another query's engine total exactly is held as on
    the base, whichever day the prose names. The check reads figures, not
    which result a label refers to: naming it would flag the correct
    "Day two" line as well as the wrong "Day one" line. Known limit."""
    answer = f"Day one, by fault code:\n{_leading(_DAY_ONE)}\n\n{label} total events: 1,113\n"
    assert _named(answer, _two_days()) == []


def test_the_listed_results_own_row_count_stays_held():
    """Preserved behavior: 10 is the number of rows day one returns."""
    af = _af()
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\nTotal faults: 10"
    assert "10" in af._cut_candidates(af._lines(answer))
    assert _named(answer, _two_days()) == []


def _figure_lines(answer: str) -> tuple[str, str]:
    line = next(text for text in answer.splitlines() if text.startswith("[engine] Figures "))
    return line, answer[answer.index("BEGIN FINAL-ANSWER REQUIREMENTS"):]


def test_the_figure_line_names_the_result_a_returned_value_is_written_beside():
    line, block = _figure_lines(_run(_beside_day_one("512"), _two_days()))
    assert line.startswith("[engine] Figures in no result as stated: 512 in this answer is "
                           "written as a total beside rows Q1 returns, and Q1's engine total "
                           "is 101.")
    assert "none of this call's results holds" not in line
    assert "a sum or difference of two returned values" not in line
    assert "none of its results this turn holds" not in block
    assert ("must not state 512 as the total of the result rows it is written beside, "
            "because it is not that result's engine total") in block


def test_a_line_with_both_kinds_of_figure_gives_each_its_own_sentence():
    answer = f"{_beside_day_one('512')}\n\nGrand total events: 9,999"
    line, block = _figure_lines(_run(answer, _two_days()))
    assert line.startswith("[engine] Figures in no result as stated: 9,999 in this answer is "
                           "stated as a total or count, but none of this call's results holds "
                           "it, and it is not an engine total or a sum or difference of two "
                           "returned values. 512 in this answer is written as a total beside "
                           "rows Q1 returns, and Q1's engine total is 101.")
    assert "must not state 9,999 as data from this system" in block
    assert "must not state 512 as the total of the result rows" in block
    assert "512 as data from this system" not in block


def test_a_line_naming_many_tied_figures_keeps_to_the_line_bound():
    from decimal import Decimal

    from agents.executed_filters import MAX_LINE_CHARS
    af = _af()
    statements = trc.collect_sidecar_statements(_two_days())
    figures = [f"{9_000_000 + i:,}" for i in range(af.MAX_FIGURES)]
    tied = {f: tuple((q, Decimal(10 ** 15 + q)) for q in range(1, 40)) for f in figures}
    line = af.figure_line([(f, []) for f in figures], statements, tied)
    assert line.startswith(af.TIED_PREFIX) and len(line) <= MAX_LINE_CHARS
    assert "none of this call's results holds" not in line
    assert f"{len(figures)} figures in this answer are written as totals beside rows" in line


def test_a_copied_tied_prefix_is_made_inert():
    af = _af()
    assert af.TIED_PREFIX not in af.neutralize(f"Note: {af.TIED_PREFIX}118 in this answer")


# --------------------------------------------------------------------------- #
# Results that return the same listed rows are each checked alone
# --------------------------------------------------------------------------- #
def _same_rows() -> dict:
    return _state_after(_keyed("t-1", 1, _OWN_KEYS, [41, 23, 9, 7]),
                        _keyed("t-2", 2, _OWN_KEYS, [41, 23, 9, 99]))


def _beside_same_rows(figure: str) -> str:
    listing = "\n".join(f"- {key}: {n}" for key, n in zip(_OWN_KEYS, [41, 23, 9]))
    return f"Line one:\n{listing}\n\nTotal events: {figure}"


def test_a_pair_across_two_results_with_the_same_listed_rows_is_named():
    """106 is 99 + 7, one cell of each result; neither result alone, its
    cells and totals (80 and 172), holds it."""
    assert _named(_beside_same_rows("106"), _same_rows()) == ["106"]
    line, _block = _figure_lines(_run(_beside_same_rows("106"), _same_rows()))
    assert ("106 in this answer is written as a total beside rows Q1 and Q2 return, and "
            "their engine totals are 80 (Q1) and 172 (Q2).") in line


@pytest.mark.parametrize("figure", ["80", "172", "48", "140"])
def test_a_figure_one_result_alone_holds_beside_rows_two_return_is_held(figure):
    """Each result's own total, or a pair of one result's own cells (41 + 7,
    41 + 99). Preserved behavior."""
    assert _named(_beside_same_rows(figure), _same_rows()) == []


# --------------------------------------------------------------------------- #
# A tied total is no sum or difference of its result's own total and values
# --------------------------------------------------------------------------- #
def _under_table(figure: str) -> str:
    """The measured shape: every row of a result, then a total line under it."""
    return (f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n"
            f"**Total events: {figure}** across 10 distinct fault codes.")


@pytest.mark.parametrize("shape", [_under_table, _beside_day_one, _measured])
@pytest.mark.parametrize("figure", ["100", "92", "60"])
def test_a_total_less_one_of_its_own_rows_is_named(shape, figure):
    """100, 92 and 60 are day one's total (101) less its last, third and
    first row: a sum of the total and one negated cell of the very result
    they are written beside, which no longer holds them."""
    assert _named(shape(figure), _two_days()) == [figure]


@pytest.mark.parametrize("figure", ["142", "202"])
def test_a_total_plus_one_of_its_own_values_is_named(figure):
    """142 counts day one's first row twice (101 + 41); 202 is its total twice."""
    assert _named(_under_table(figure), _two_days()) == [figure]


@pytest.mark.parametrize("figure", ["98", "95", "37"])
def test_a_total_less_two_of_its_own_rows_is_named(figure):
    """98 and 95 leave out two rows (2 + 1, 5 + 1) whose sum is another of
    day one's cells (3, 6); 37 leaves out the first two (41 + 23) and is a
    difference of two of its cells (41 - 4)."""
    assert _named(_under_table(figure), _two_days()) == [figure]


def test_the_figure_line_gives_the_total_a_figure_less_one_row_is_written_beside():
    line, block = _figure_lines(_run(_under_table("100"), _two_days()))
    assert line.startswith("[engine] Figures in no result as stated: 100 in this answer is "
                           "written as a total beside rows Q1 returns, and Q1's engine total "
                           "is 101.")
    assert "must not state 100 as the total of the result rows it is written beside" in block


@pytest.mark.parametrize("label", [
    "Running total through the ninth row: 100",
    "Sub-total: 100",
    "Total events: 100, through the ninth row",
])
def test_a_running_subtotal_equal_to_the_total_less_a_row_is_held(label):
    """100 is the sum of day one's first nine rows: a running subtotal is never
    tied, so it is checked as any figure, as before."""
    answer = f"Day one, by fault code:\n\n{_table(_DAY_ONE)}\n\n{label}"
    assert _named(answer, _two_days()) == []


@pytest.mark.parametrize("figure", ["100", "37"])
def test_an_untied_difference_of_two_returned_values_is_held(figure):
    """Beside no listing, or beside two rows (too few to tie), 101 - 1 and
    41 - 4 still hold the figure as a difference of two returned values."""
    assert _named(f"Day one is counted.\n\n**Total events: {figure}**", _two_days()) == []
    answer = f"### Day one ({figure} total events):\n{_leading(_DAY_ONE, 2)}\n"
    assert _named(answer, _two_days()) == []


def test_a_figure_equal_to_another_results_total_is_still_held():
    """Known limit, unchanged: 100 is day one's total less its last row, and
    also the engine total of a third result, so it is held. 92, its total
    less its third row, is no other result's total and is named."""
    state = _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO),
                         _keyed("t-3", 3, _OTHER_KEYS[:2], [60, 40]))
    assert _named(_under_table("100"), state) == []
    assert _named(_under_table("92"), state) == ["92"]


def test_a_total_less_one_row_is_named_when_another_result_has_the_same_total():
    """A second breakdown of the same rows (60 + 41) has day one's total too:
    a value equal to the listed result's total is its own, whichever query
    also returns it, so 100 is still day one's total less its last row."""
    state = _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO),
                         _keyed("t-3", 3, _OTHER_KEYS[:2], [60, 41]))
    assert _named(_under_table("100"), state) == ["100"]


def test_its_total_with_another_results_total_equal_to_one_of_its_cells_is_held():
    """Both lines together (101 + 23): the other result's engine total, 23, is
    also one of day one's cells. A cell equal to another result's engine total
    is read as that total, as a figure equal to it is, so the sum still holds."""
    state = _state_after(_grouped("t-1", "04", _DAY_ONE),
                         _keyed("t-3", 3, _OTHER_KEYS[:2], [20, 3]))
    answer = (f"Both lines together: 124 total events.\n\n### Day one leading codes\n"
              f"{_leading(_DAY_ONE, 3)}\n")
    assert _named(answer, state) == []
    assert _named(answer.replace("124", "125"), state) == ["125"]


@pytest.mark.parametrize("figure", ["64", "1,214", "1,012"])
def test_two_of_its_cells_or_its_total_with_another_results_total_still_hold(figure):
    """64 is a sum of two of day one's cells (41 + 23): such a sum can be the
    correct total of two rows the prose names, which this check cannot tell
    from a full total, so it still holds. 1,214 is both days' totals together
    and 1,012 their difference. Each is held as before."""
    assert _named(_under_table(figure), _two_days()) == []


def test_a_total_less_two_rows_that_equals_the_other_two_rows_is_held():
    """Known limit: of four rows (80), the total less the first two (41 + 23)
    is 16, the sum of the other two (9 + 7). A sum of two of a result's cells
    still holds a figure written beside it, so this one is held."""
    state = _state_after(_keyed("t-1", 1, _OWN_KEYS, [41, 23, 9, 7]),
                         _keyed("t-2", 2, _OTHER_KEYS, [500, 300]))
    listing = "\n".join(f"- {key}: {n}" for key, n in zip(_OWN_KEYS, [41, 23, 9, 7]))
    assert _named(f"Line one:\n{listing}\n\nTotal events: 17", state) == ["17"]
    assert _named(f"Line one:\n{listing}\n\nTotal events: 16", state) == []


# --------------------------------------------------------------------------- #
# Past a cut result
# --------------------------------------------------------------------------- #
def _cut(counts) -> dict:
    sql = ("SELECT station, COUNT(*) AS n_events FROM fault_log "
           "GROUP BY station ORDER BY n_events DESC LIMIT 2")
    rows = [[f"st_{i}", str(n)] for i, n in enumerate(counts)]
    body = {**bounded_query_result(["station", "n_events"], rows, False),
            "_adt_executed_query": sql}
    return {"toolUseId": "t-9", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def test_past_a_cut_a_tied_figure_no_cut_result_can_total_is_named():
    state = _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO),
                         _cut([700, 650]))
    assert _named(_measured(), state) == [_WRONG]


def test_past_a_cut_a_tied_figure_a_cut_result_may_total_is_not_named():
    state = _state_after(_grouped("t-1", "04", _DAY_ONE), _grouped("t-2", "11", _DAY_TWO),
                         _cut([60, 50]))
    assert _named(_measured(), state) == []


# --------------------------------------------------------------------------- #
# The cells on the totals record
# --------------------------------------------------------------------------- #
def _payload(counts) -> dict:
    rows = [[f"k_{i}", str(n)] for i, n in enumerate(counts)]
    return {"columns": ["k", "n"], "rows": rows, "row_count": len(rows),
            "_adt_executed_query": "SELECT k, COUNT(*) AS n FROM fault_log GROUP BY k"}


def test_a_results_cells_are_kept_up_to_the_listing_bound():
    af = _af()
    record = af.totals_record(_payload(_DAY_ONE))
    assert record["cells"] == [[str(n) for n in _DAY_ONE]]
    assert record["row_keys"] == [[f"k_{i}"] for i in range(len(_DAY_ONE))]
    wide = af.totals_record(_payload([1] * (af._MAX_LIST_LINES + 1)))
    assert wide is not None and "cells" not in wide and "row_keys" not in wide


def test_keeping_the_cells_changes_no_totals_line():
    af = _af()
    first = {"query_text": "SELECT k, COUNT(*) AS n FROM fault_log WHERE z = 1 GROUP BY k",
             "totals": af.totals_record(_payload([3, 5]))}
    second = {"query_text": "SELECT k, COUNT(*) AS n FROM fault_log WHERE z = 2 GROUP BY k",
              "totals": af.totals_record(_payload([5, 3]))}
    line = af.totals_line([first, second])
    assert "Q1 (" in line and "Q2 (" not in line


# --------------------------------------------------------------------------- #
# Two results with the same total, and a coded subtotal beside an uncoded row
# --------------------------------------------------------------------------- #
def _grouped_keys(tool_id: str, day: str, keys, counts) -> dict:
    rows = [[key, str(n)] for key, n in zip(keys, counts)]
    body = {**bounded_query_result(["fault_code", "n_events"], rows, False),
            "_adt_executed_query": _sql(day)}
    return {"toolUseId": tool_id, "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


_FOUR = [41, 23, 20, 17]                                          # sums to 101


def _both_days(other) -> str:
    return (f"**Both days together: 202 total events.** Day one's faults:\n"
            f"{_leading(_FOUR, 4)}\n")


def test_both_results_together_is_held_when_they_share_a_total():
    state = _state_after(_grouped("t-1", "04", _FOUR), _grouped("t-2", "11", [60, 41]))
    assert _named(_both_days([60, 41]), state) == []


def test_both_results_together_is_named_when_only_one_has_that_total():
    state = _state_after(_grouped("t-1", "04", _FOUR), _grouped("t-2", "11", [60, 40]))
    assert _named(_both_days([60, 40]), state) == ["202"]


#: The uncoded row is not the last one, so the coded total (89) is no running
#: sum of the listed rows: only the label says it leaves that row out.
_CODED_KEYS = ["code_a", "code_b", "uncoded", "code_c"]
_CODED = [50, 30, 12, 9]                                          # sums to 101


def _coded_table() -> str:
    rows = "\n".join(f"| {key} | {n} |" for key, n in zip(_CODED_KEYS, _CODED))
    return f"| Fault code | Events |\n|---|---|\n{rows}"


@pytest.mark.parametrize("label", ["Total coded events", "Coded events in total",
                                   "Total events with a code"])
def test_a_coded_subtotal_beside_an_uncoded_row_is_held(label):
    state = _state_after(_grouped_keys("t-1", "04", _CODED_KEYS, _CODED))
    answer = f"Day one, by fault code:\n\n{_coded_table()}\n\n**{label}: 89**"
    assert _named(answer, state) == []


def test_a_full_total_beside_the_same_rows_less_the_uncoded_row_is_named():
    state = _state_after(_grouped_keys("t-1", "04", _CODED_KEYS, _CODED))
    answer = f"Day one, by fault code:\n\n{_coded_table()}\n\n**Total events: 89**"
    assert _named(answer, state) == ["89"]
