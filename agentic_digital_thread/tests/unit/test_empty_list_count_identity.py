# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which expansion a count of empty or NULL lists belongs to.

A count of the records whose list is empty or NULL gives an element expansion
its left-out records only when it counts exactly the records that expansion
drops. Pinned here:

* the field a reference reads is its whole name: ``actual.items`` and
  ``planned.items`` are fields of two row columns, so a count of one reaches
  no expansion of the other, and an earlier grouped result of one gives the
  other nothing; a leading name that is the query's own relation (its alias,
  or its table name when it has none) only qualifies the column, and the
  table's name under another alias is not read;
* a coalesced count (``COALESCE(json_array_length(<column>), 0) = 0``) also
  counts a value that is not a list (an object ``{}``, a scalar ``5``): its
  length is NULL. ``CAST`` to an ``ARRAY`` fails the query on such a value, so
  a ``CAST`` expansion that ran has none; ``TRY_CAST`` turns it into NULL with
  no element, so the count is not the expansion's empty or NULL lists and
  reaches no ``TRY_CAST`` expansion; nor one with a LIMIT and no ORDER BY,
  which can end before the ``CAST`` has read such a value;
* an expansion and its count processed at the same time still meet: the call
  pairs each with the other under one lock, whichever lands first.

Fully offline; synthetic names.
"""
from __future__ import annotations

import contextvars
import importlib
import threading
import types

import pytest

import agents.system_agent as sa

_TABLE = "bay_event_log"
_WHERE = "WHERE site = 'A'"


def _el():
    return importlib.import_module("agents.empty_lists")


def _expanded(column: str, *, cast: str = "CAST", element: str = "JSON",
              source: str = _TABLE) -> str:
    return (f"SELECT json_extract_scalar(e, '$.id') AS item_id, COUNT(*) AS n FROM {source} "
            f"CROSS JOIN UNNEST({cast}(json_parse({column}) AS ARRAY({element}))) AS u(e) "
            f"{_WHERE} GROUP BY json_extract_scalar(e, '$.id')")


def _count(column: str, *, source: str = _TABLE) -> str:
    return (f"SELECT COUNT(*) AS n FROM {source} {_WHERE} "
            f"AND COALESCE(json_array_length({column}), 0) = 0")


def _one(n: str = "7") -> dict:
    return {"columns": ["n"], "rows": [[n]], "row_count": 1}


def _rows() -> dict:
    return {"columns": ["item_id", "n"], "rows": [["I-1", "4"], ["I-2", "9"]], "row_count": 2}


def _process(call, sql: str, result: dict) -> dict:
    """One result of ``sql`` processed in the Mode A call ``call``."""
    def go():
        sa._MODE_A_CALL.set(call)
        sa._disclose_dropped_lists(result, sql)
    contextvars.copy_context().run(go)
    return result


def _counted(call) -> list:
    el = _el()
    return [w for w in call.state_windows() if isinstance(w, el.Dropped) and w.records]


# --------------------------------------------------------------------------- #
# Two row fields of the same name
# --------------------------------------------------------------------------- #
def test_two_row_fields_of_one_name_stay_apart():
    el = _el()
    actual, planned = _expanded("actual.items"), _expanded("planned.items")
    observed = el.counted(_count("planned.items"), _one())
    assert observed is not None and observed.records == 7
    waiting = [el.expansion(actual), el.expansion(planned)]
    assert [(d.column, d.records) for d in el.reached(observed, waiting)] == [("planned.items", 7)]
    assert el.dropped(actual, [observed]).records is None
    assert el.dropped(planned, [observed]).records == 7
    # a leading name that is the relation only qualifies the column
    aliased = _expanded("b.payload", source=f"{_TABLE} b")
    plain = el.counted(_count("payload"), _one())
    assert el.dropped(aliased, [plain]).records == 7
    assert el.dropped(_expanded(f"{_TABLE}.payload"), [plain]).records == 7


@pytest.mark.parametrize("count_first", [False, True], ids=["count_last", "count_first"])
def test_a_count_of_one_row_field_reaches_only_its_own_expansion(count_first):
    el = _el()
    call = sa._ModeACall("How many items per id?")
    actual, planned = _expanded("actual.items"), _expanded("planned.items")
    order = [(actual, _rows()), (planned, _rows())]
    count = (_count("planned.items"), _one())
    order = [count, *order] if count_first else [*order, count]
    shown = {sql: _process(call, sql, result) for sql, result in order}
    assert [(d.column, d.records) for d in _counted(call)] == [("planned.items", 7)]
    assert "empty_lists_dropped" not in shown[actual]
    line = el.engine_line(call.state_windows())
    assert "the list in planned.items into one row per element, so 7 records" in line
    assert "the list in items " not in line and "actual.items" not in line
    assert 'json_array_length("actual"."items")' in shown[actual]["note"]


def test_an_earlier_grouped_result_of_one_row_field_gives_the_other_nothing():
    el = _el()
    finding = types.SimpleNamespace(container="list", plain=True, table=_TABLE, column="items",
                                    records_known=True, empty_records=5, empty_rows=1,
                                    empty_table=())
    grouped = (f"SELECT planned.items AS items_json, COUNT(*) AS n FROM {_TABLE} {_WHERE} "
               "GROUP BY planned.items")
    observed = el.observe(grouped, finding)
    assert el.dropped(_expanded("planned.items"), [observed]).records == 5
    assert el.dropped(_expanded("actual.items"), [observed]).records is None
    # both row fields grouped: the finding's column could be either
    both = (f"SELECT actual.items, planned.items, COUNT(*) AS n FROM {_TABLE} {_WHERE} "
            "GROUP BY actual.items, planned.items")
    either = el.observe(both, finding)
    assert el.dropped(_expanded("planned.items"), [either]).records is None
    assert el.dropped(_expanded("actual.items"), [either]).records is None


@pytest.mark.parametrize(("expanded", "count"), [
    # the table's own name under another alias is not the relation
    (_expanded(f"{_TABLE}.payload", source=f"{_TABLE} b"), _count("payload")),
    (_expanded("payload"), _count(f"{_TABLE}.payload", source=f"{_TABLE} b")),
    # a row field is not the column of the same name
    (_expanded("items"), _count("planned.items")),
    (_expanded("planned.items"), _count("items")),
], ids=["expansion_table_name_aliased", "count_table_name_aliased",
        "column_vs_row_field", "row_field_vs_column"])
def test_a_reference_not_proved_to_read_the_same_field_gets_no_count(expanded, count):
    el = _el()
    observed = el.counted(count, _one())
    found = el.expansion(expanded)
    assert found is not None
    assert observed is None or el.reached(observed, [found]) == ()
    assert observed is None or el.dropped(expanded, [observed]).records is None


# --------------------------------------------------------------------------- #
# TRY_CAST: a coalesced count also counts values that are not lists
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("element", ["JSON", "VARCHAR", "MAP(VARCHAR, JSON)"])
def test_a_coalesced_count_reaches_no_try_cast_expansion(element):
    el = _el()
    lenient = _expanded("payload", cast="TRY_CAST", element=element)
    strict = _expanded("payload", element=element)
    observed = el.counted(_count("payload"), _one("4"))
    assert observed is not None and observed.records == 4
    assert el.expansion(lenient) is not None
    assert el.reached(observed, [el.expansion(lenient)]) == ()
    assert el.dropped(lenient, [observed]).records is None
    # a CAST expansion that ran has no value that is not a list
    assert [d.records for d in el.reached(observed, [el.expansion(strict)])] == [4]
    assert el.dropped(strict, [observed]).records == 4
    # an earlier grouped result's count is of empty or NULL lists only
    finding = types.SimpleNamespace(container="list", plain=True, table=_TABLE, column="payload",
                                    records_known=True, empty_records=3, empty_rows=1,
                                    empty_table=())
    grouped = f"SELECT payload, COUNT(*) AS n FROM {_TABLE} {_WHERE} GROUP BY payload"
    assert el.dropped(lenient, [el.observe(grouped, finding)]).records == 3


@pytest.mark.parametrize("count_first", [False, True], ids=["count_last", "count_first"])
def test_a_try_cast_expansion_and_a_coalesced_count_in_one_call_state_nothing(count_first):
    el = _el()
    call = sa._ModeACall("How many items per id?")
    lenient = (_expanded("payload", cast="TRY_CAST"), _rows())
    count = (_count("payload"), _one("4"))
    for sql, result in ([count, lenient] if count_first else [lenient, count]):
        _process(call, sql, result)
    assert _counted(call) == []
    assert el.engine_line(call.state_windows()) == ""
    assert "empty_lists_dropped" not in lenient[1]
    assert "count those records under the same filters" in lenient[1]["note"]


def test_a_coalesced_count_reaches_no_expansion_a_limit_can_end_early():
    """A LIMIT with no ORDER BY can end the expansion before the CAST has read
    a value that is not a list; with an ORDER BY every row is read first."""
    el = _el()
    observed = el.counted(_count("payload"), _one("4"))
    early = (f"SELECT e FROM {_TABLE} CROSS JOIN UNNEST(CAST(json_parse(payload) AS ARRAY(JSON))) "
             f"AS u(e) {_WHERE} LIMIT 50")
    assert el.expansion(early) is not None
    assert el.reached(observed, [el.expansion(early)]) == ()
    assert el.dropped(early, [observed]).records is None
    ordered = f"{_expanded('payload')} ORDER BY n DESC LIMIT 500"
    assert el.dropped(ordered, [observed]).records == 4


# --------------------------------------------------------------------------- #
# An expansion and its count processed at the same time
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("first", ["expansion", "count"])
def test_an_expansion_and_its_count_processed_at_once_still_meet(monkeypatch, first):
    """Both results read the call's records before either keeps its own (a
    barrier where a record is kept); the one named ``first`` reads first."""
    el = _el()
    call = sa._ModeACall("How many items per id?")
    results = {"expansion": (_expanded("payload"), _rows()), "count": (_count("payload"), _one())}
    entered = threading.Event()
    real_dropped = el.dropped

    def dropped(sql, observations):
        found = real_dropped(sql, observations)
        if sql == results[first][0]:
            entered.set()
        return found

    barrier = threading.Barrier(2, timeout=2)
    real_keep = sa._ModeACall.observe_list

    def keep(self, observation):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        real_keep(self, observation)

    monkeypatch.setattr(el, "dropped", dropped)
    monkeypatch.setattr(sa._ModeACall, "observe_list", keep)
    second = "count" if first == "expansion" else "expansion"

    def run(name, wait):
        if wait:
            entered.wait(timeout=2)
        _process(call, *results[name])

    threads = [threading.Thread(target=run, args=(first, False)),
               threading.Thread(target=run, args=(second, True))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert [(d.column, d.records) for d in _counted(call)] == [("payload", 7)]
    assert "so 7 records" in el.engine_line(call.state_windows())
    if first == "count":
        assert results["expansion"][1]["empty_lists_dropped"]["records"] == 7
