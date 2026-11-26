# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The totals line of a list expansion says how many records it leaves out.

A result whose rows are the elements of a serialized list (an inner UNNEST of
a JSON list column) has no row for a record whose list is empty or NULL. When
the result carries that count (``empty_lists_dropped``), the entry that says
what its rows count states it in the list-elements phrase: ``so N records
with an empty or NULL list add no row`` in place of ``so a record whose list
is empty adds no row``.

Pinned here:

* the counted phrase, with the singular for one record;
* it is the first label detail to give way, at the per-entry cap and under
  the whole-line budget: past either, the entry reads as it would without the
  count, so the count never costs the rest of the label, and a figure is never
  cut;
* a later total over the same FROM and WHERE points at the earlier entry only
  when both leave out the same records;
* a result that is not a list expansion never states the count.

Neutral fixtures, fully offline.
"""
from __future__ import annotations

import importlib
import re

from agents.executed_filters import MAX_LINE_CHARS

_TABLE = "bin_audit"
_LIST = '"routing.hops"'
_EXPANSION = f"CROSS JOIN UNNEST(CAST(json_parse({_LIST}) AS ARRAY(JSON))) AS t(e)"
_WHERE = ("WHERE aisle = 'A7' AND audited_at >= TIMESTAMP '2031-05-01 00:00:00 UTC' "
          "AND audited_at < TIMESTAMP '2031-05-08 00:00:00 UTC'")
_KEYS = ["H-1", "H-2", "H-3"]
_COUNTS = ["40", "25", "10"]                                  # sums to 75
_ELEMENTS = ('of list elements: one row per element of UNNEST(CAST(json_parse("routing.hops") AS '
             f"ARRAY(JSON))) in each {_TABLE} row, so ")
_PHRASE = f"{_ELEMENTS}a record whose list is empty adds no row"
_COUNTED = f"{_ELEMENTS}15 records with an empty or NULL list add no row"
_LEFT_OUT = re.compile(r"so (\d[\d,]*) records? with an empty or NULL list adds? no row")


def _af():
    return importlib.import_module("agents.answer_figures")


def _expanded(where: str = _WHERE, key: str = "json_extract_scalar(e, '$.id')") -> str:
    return (f"SELECT {key} AS hop, COUNT(*) AS bins FROM {_TABLE} {_EXPANSION} {where} "
            f"GROUP BY {key}")


def _near_cap(path: str, width: int = 48) -> str:
    """An expansion whose entry, without the count, is a few characters under
    its cap: a long key path and three long-column filters."""
    column = "zone_attribute_" + "x" * width
    predicates = " AND ".join(f"{column}_{i} = 'v'" for i in range(3))
    return _expanded(f"{_WHERE} AND {predicates}", f"json_extract_scalar(e, '$.{path}')")


def _statement(sql: str, dropped: int | None = None, counts=_COUNTS) -> dict:
    payload = {"columns": ["hop", "bins"], "rows": [[k, n] for k, n in zip(_KEYS, counts)],
               "row_count": len(counts), "_adt_executed_query": sql}
    if dropped is not None:
        payload["empty_lists_dropped"] = {"column": "routing.hops", "records": dropped}
    record = _af().totals_record(payload)
    assert record is not None, sql
    return {"query_text": sql, "totals": record}


def _entries(line: str) -> list[str]:
    body = line.split("numbered as above: ", 1)[1]
    return re.split(r"; (?=Q\d+ \()", body)


def test_an_expansion_states_the_records_its_empty_lists_leave_out():
    line = _af().totals_line([_statement(_expanded(), dropped=15)])
    assert (f"Q1 (3 rows, one per value of json_extract_scalar(e, '$.id'), {_COUNTED}, "
            "with audited_at in [2031-05-01 00:00:00 UTC, 2031-05-08 00:00:00 UTC), where "
            "aisle = 'A7'): bins = COUNT(*) 75.") in line


def test_one_left_out_record_is_singular():
    line = _af().totals_line([_statement(_expanded(), dropped=1)])
    assert f"{_ELEMENTS}1 record with an empty or NULL list adds no row, with audited_at" in line


def test_a_large_count_is_grouped():
    line = _af().totals_line([_statement(_expanded(), dropped=12345)])
    assert f"{_ELEMENTS}12,345 records with an empty or NULL list add no row," in line


def test_without_the_count_the_label_is_unchanged():
    line = _af().totals_line([_statement(_expanded())])
    assert f"{_PHRASE}, with audited_at in" in line
    assert not _LEFT_OUT.search(line)


def test_a_result_that_is_not_an_expansion_never_states_the_count():
    sql = f"SELECT dock AS hop, COUNT(*) AS bins FROM {_TABLE} {_WHERE} GROUP BY dock"
    line = _af().totals_line([_statement(sql, dropped=15)])
    assert "bins = COUNT(*) 75" in line
    assert not _LEFT_OUT.search(line) and "empty or NULL" not in line


def test_the_count_fits_beside_a_label_close_to_its_cap():
    af = _af()
    sql = _near_cap("location.reference_code_value")
    (plain,) = _entries(af.totals_line([_statement(sql)]))
    (entry,) = _entries(af.totals_line([_statement(sql, dropped=15)]))
    assert af._MAX_ENTRY_CHARS - 20 < len(plain) <= af._MAX_ENTRY_CHARS - 8
    assert len(entry) <= af._MAX_ENTRY_CHARS
    assert "so 15 records with an empty or NULL list add no row, with audited_at" in entry
    assert entry.endswith("bins = COUNT(*) 75.")


def test_a_count_past_the_entry_cap_gives_way_and_the_label_stays():
    af = _af()
    sql = _near_cap("location.reference_code_value_backup")
    (plain,) = _entries(af.totals_line([_statement(sql)]))
    (entry,) = _entries(af.totals_line([_statement(sql, dropped=15)]))
    assert af._MAX_ENTRY_CHARS - 8 < len(plain) <= af._MAX_ENTRY_CHARS
    assert entry == plain
    assert "so a record whose list is empty adds no row" in entry
    assert entry.endswith("bins = COUNT(*) 75.")


def test_past_the_cap_the_count_gives_way_before_the_aggregates():
    af = _af()
    aggregate = ("SUM(CASE WHEN json_extract_scalar(e, '$.kind') IN ('transfer', 'restock', "
                 "'return') THEN 1 ELSE 0 END)")
    sql = _near_cap("location.reference_code_value").replace("COUNT(*) AS bins",
                                                              f"{aggregate} AS bins")
    (plain,) = _entries(af.totals_line([_statement(sql)]))
    (entry,) = _entries(af.totals_line([_statement(sql, dropped=15)]))
    assert entry == plain and len(entry) <= af._MAX_ENTRY_CHARS
    assert entry.endswith("): bins 75.") and "so a record whose list is empty adds no row" in entry
    assert not _LEFT_OUT.search(entry)


def test_six_long_expansions_lose_their_counts_before_anything_else():
    af = _af()
    sqls = []
    for i in range(6):
        predicates = " AND ".join(f"zone_attribute_{i}{j:02d}.long_attribute_name = 'v_{j}'"
                                  for j in range(6))
        sqls.append(_expanded(f"{_WHERE} AND {predicates}"))
    counts = [[str(40 + i), "25", "10"] for i in range(6)]       # sums to 75 + i
    plain = af.totals_line([_statement(sql, counts=c) for sql, c in zip(sqls, counts)])
    line = af.totals_line([_statement(sql, dropped=15 + i, counts=c)
                           for i, (sql, c) in enumerate(zip(sqls, counts))])
    assert len(line) <= MAX_LINE_CHARS
    assert line == plain
    entries = _entries(line)
    assert [e.rstrip(".").rsplit(" ", 1)[1] for e in entries] == [str(75 + i) for i in range(6)]


def test_under_the_line_budget_a_count_gives_way_before_any_aggregate():
    af = _af()
    first, second = _near_cap("location.reference", 29), _near_cap("location.reference", 30)
    plain = af.totals_line([_statement(first), _statement(second, counts=["5", "6", "7"])])
    assert len(plain) <= MAX_LINE_CHARS < len(plain) + 16
    assert plain.count(" = COUNT(*) ") == 2
    line = af.totals_line([_statement(first, dropped=15),
                           _statement(second, dropped=16, counts=["5", "6", "7"])])
    assert len(line) <= MAX_LINE_CHARS
    assert line.count(" = COUNT(*) ") == 2
    shorter, longer = _entries(line)
    assert "so 15 records with an empty or NULL list add no row" in shorter
    assert "so a record whose list is empty adds no row" in longer
    assert not _LEFT_OUT.search(longer)


def test_the_figure_line_keeps_the_count_within_its_budget():
    af = _af()
    found = [("4,321", [])]
    line = af.figure_line(found, [_statement(_expanded(), dropped=15)])
    assert len(line) <= MAX_LINE_CHARS
    assert "so 15 records with an empty or NULL list add no row" in line
    assert line.endswith("bins = COUNT(*) 75.")


def test_a_later_total_over_the_same_rows_and_count_points_at_the_earlier_one():
    other = _expanded(key="json_extract_scalar(e, '$.name')")
    line = _af().totals_line([_statement(_expanded(), dropped=15),
                              _statement(other, dropped=15, counts=["5", "6", "7"])])
    first, second = _entries(line)
    assert "so 15 records with an empty or NULL list add no row" in first
    assert "of the same list elements as Q1" in second and not _LEFT_OUT.search(second)


def test_the_same_rows_with_another_count_state_their_own():
    other = _expanded(key="json_extract_scalar(e, '$.name')")
    line = _af().totals_line([_statement(_expanded()),
                              _statement(other, dropped=15, counts=["5", "6", "7"])])
    first, second = _entries(line)
    assert not _LEFT_OUT.search(first)
    assert "the same list elements" not in second
    assert "so 15 records with an empty or NULL list add no row" in second
