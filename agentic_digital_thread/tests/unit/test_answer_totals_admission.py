# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two harmless key spellings get engine totals.

Measured on an analytics deployment: a system agent's complete grouped results
got no engine total, so the orchestrator had nothing to set beside the totals
the agent added up by hand (and got wrong). Two key spellings made the totals
parser refuse a result whose COUNT columns add up exactly:

* a constant SELECT item that labels every row (``'<day>' AS day``), not in
  GROUP BY;
* ``COALESCE(k, <literal>) AS alias`` in the SELECT list over ``GROUP BY k``.

Pinned here: the totals parser (``answer_figures._shape``) reads both, alone
and beside a ``COUNT(DISTINCT)`` column (which gets no total), by spelling
GROUP BY to match before it asks the exact split's parser; that parser's own
contract is unchanged (it still refuses both as written). A COALESCE over a
column the query does not group by, a fallback that is not a literal, a CASE
key and a numeric constant stay refused. A wrong total beside such a result is
named with the engine total in the line.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents.athena_exact_shape import ShapeRefusal, parse_shape
from agents.response_integrity_query import bounded_query_result, strict_json_dumps

_LINES = ["pl_1", "pl_2", "pl_3", "pl_4"]
_COUNTS = ["1200", "3400", "560", "780"]                    # sums to 5,940
_WHERE = ("WHERE reel_state = 'SPLICED' AND spliced_at >= TIMESTAMP '2031-03-04 00:00:00 UTC' "
          "AND spliced_at < TIMESTAMP '2031-03-05 00:00:00 UTC'")
_CONSTANT = (f"SELECT '2031-03-04' AS run_day, press_line, COUNT(*) AS n_reels FROM reel_log "
             f"{_WHERE} GROUP BY press_line ORDER BY press_line LIMIT 500")
_COALESCE = (f"SELECT COALESCE(fault_code, 'NO_CODE') AS fault, COUNT(*) AS n_reels "
             f"FROM reel_log {_WHERE} GROUP BY fault_code ORDER BY n_reels DESC LIMIT 500")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    yield


def _af():
    return importlib.import_module("agents.answer_figures")


def _payload(columns, rows, sql) -> dict:
    return {**bounded_query_result(columns, rows, False), "_adt_executed_query": sql}


def _result(columns, rows, sql) -> dict:
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(_payload(columns, rows, sql))}]}


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


def _run(answer, state, question="How many reel_log reels were spliced per line?"):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _a, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", question))


def _totals(record) -> list[tuple]:
    return [tuple(item[:3]) for item in record["totals"]]


def _constant_rows():
    return [["2031-03-04", line, n] for line, n in zip(_LINES, _COUNTS)]


def _coalesce_rows():
    return [[code, n] for code, n in zip(["F_A", "F_B", "NO_CODE", "F_C"], _COUNTS)]


# --------------------------------------------------------------------------- #
# Admitted
# --------------------------------------------------------------------------- #
def test_a_constant_select_item_beside_the_keys_gets_its_total():
    record = _af().totals_record(_payload(["run_day", "press_line", "n_reels"],
                                          _constant_rows(), _CONSTANT))
    assert record is not None and record["rows"] == 4
    assert _totals(record) == [("n_reels", "5940", False)]


def test_a_constant_item_with_a_count_distinct_column_gets_the_count_total_only():
    sql = ("SELECT 'w09' AS run_week, press_line, COUNT(*) AS n_reels, "
           "COUNT(DISTINCT spool_id) AS n_spools FROM reel_log GROUP BY press_line")
    rows = [["w09", line, n, str(int(n) // 10)] for line, n in zip(_LINES, _COUNTS)]
    record = _af().totals_record(_payload(["run_week", "press_line", "n_reels", "n_spools"],
                                          rows, sql))
    assert _totals(record) == [("n_reels", "5940", False)]


def test_a_typed_constant_and_a_positional_group_by_get_a_total():
    sql = ("SELECT DATE '2031-03-04' AS run_day, press_line, COUNT(*) AS n_reels "
           "FROM reel_log GROUP BY 2")
    record = _af().totals_record(_payload(["run_day", "press_line", "n_reels"],
                                          _constant_rows(), sql))
    assert _totals(record) == [("n_reels", "5940", False)]


def test_a_coalesced_key_over_its_grouped_column_gets_its_total():
    record = _af().totals_record(_payload(["fault", "n_reels"], _coalesce_rows(), _COALESCE))
    assert record is not None and _totals(record) == [("n_reels", "5940", False)]


def test_several_coalesced_quoted_keys_get_their_total():
    sql = ('SELECT COALESCE("detail.press.line", \'NONE\') AS line_key, '
           'COALESCE("detail.fault.code", \'NO_CODE\') AS fault, '
           'COALESCE("detail.fault.note", \'NO_NOTE\') AS note, COUNT(*) AS n_reels '
           f'FROM reel_log {_WHERE} '
           'GROUP BY "detail.press.line", "detail.fault.code", "detail.fault.note" '
           'ORDER BY n_reels DESC LIMIT 500')
    rows = [[line, f"F_{i}", "NO_NOTE", n] for i, (line, n) in enumerate(zip(_LINES, _COUNTS))]
    record = _af().totals_record(_payload(["line_key", "fault", "note", "n_reels"], rows, sql))
    assert record is not None and _totals(record) == [("n_reels", "5940", False)]


def test_a_coalesced_key_beside_its_own_column_gets_its_total():
    sql = ("SELECT fault_code, COALESCE(fault_code, 'NO_CODE') AS fault, COUNT(*) AS n_reels "
           "FROM reel_log GROUP BY fault_code")
    rows = [[None if c == "NO_CODE" else c, c, n]
            for c, n in zip(["F_A", "F_B", "NO_CODE", "F_C"], _COUNTS)]
    record = _af().totals_record(_payload(["fault_code", "fault", "n_reels"], rows, sql))
    assert record is not None and _totals(record) == [("n_reels", "5940", False)]


# --------------------------------------------------------------------------- #
# Still refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    # COALESCE over a column the query does not group by
    "SELECT COALESCE(fault_note, 'x') AS fault, COUNT(*) AS n FROM reel_log GROUP BY fault_code",
    # a fallback that is not a literal
    "SELECT COALESCE(fault_code, fault_note) AS fault, COUNT(*) AS n FROM reel_log "
    "GROUP BY fault_code",
    # a CASE key
    "SELECT CASE WHEN fault_code IS NULL THEN 'x' ELSE fault_code END AS fault, COUNT(*) AS n "
    "FROM reel_log GROUP BY fault_code",
    # a numeric constant (in GROUP BY it would read as a position)
    "SELECT 7 AS fault, COUNT(*) AS n FROM reel_log GROUP BY fault_code",
])
def test_other_unmatched_keys_stay_refused(sql):
    rows = [[code, n] for code, n in zip(["F_A", "F_B", "F_C", "F_D"], _COUNTS)]
    assert _af().totals_record(_payload(["fault", "n"], rows, sql)) is None


def test_the_exact_split_parser_still_refuses_both_spellings_as_written():
    for sql in (_CONSTANT, _COALESCE):
        with pytest.raises(ShapeRefusal):
            parse_shape(sql)


# --------------------------------------------------------------------------- #
# The engine lines
# --------------------------------------------------------------------------- #
def test_a_correct_total_beside_a_constant_item_result_gets_the_totals_line():
    state = _state_after(_result(["run_day", "press_line", "n_reels"], _constant_rows(),
                                 _CONSTANT))
    answer = _run("Reels per line are listed.\n\nTotal reels: 5,940", state)
    assert "[engine] Result totals:" in answer and "5,940" in answer
    assert "Figures in no result" not in answer


@pytest.mark.parametrize("columns, rows, sql", [
    (["run_day", "press_line", "n_reels"], _constant_rows(), _CONSTANT),
    (["fault", "n_reels"], _coalesce_rows(), _COALESCE),
])
def test_a_wrong_total_is_named_with_the_engine_total(columns, rows, sql):
    answer = _run("Reels are listed.\n\nTotal reels: 6,010", _state_after(_result(columns, rows,
                                                                                 sql)))
    line = next(text for text in answer.split("\n\n")
                if text.startswith("[engine] Figures in no result"))
    assert "6,010" in line
    assert "Engine totals, in this turn's queries numbered as above: Q1 (" in line
    assert "5,940" in line
    assert "The engine computed no total" not in line
