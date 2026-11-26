# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The storage consumers do not read a same-column OR window.

The window rules (state_times, record_time) read a date window written as an OR
of ranges on one column; the storage consumers must NOT. A union of two ranges
is not one contiguous range, and storage_bound_rule.decide / decide_window and
the exact-split planner were not measured for it. This pins that
bounded_columns' DEFAULT return, and therefore every storage consumer that
reads it, ignores the OR conjunct exactly as before. Fully offline; synthetic
names.
"""
from __future__ import annotations

import importlib

import pytest

_TS = "timestamp(6) with time zone"
_R1 = ("done_at >= TIMESTAMP '2026-07-20 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-21 00:00:00 UTC'")
_R2 = ("done_at >= TIMESTAMP '2026-07-27 00:00:00 UTC' "
       "AND done_at < TIMESTAMP '2026-07-28 00:00:00 UTC'")
_OR_WINDOW = f"(({_R1}) OR ({_R2}))"

# Several single-table OR shapes on one column, with and without a top-level
# single-range bound alongside.
_SHAPES = [
    f"SELECT COUNT(*) AS n FROM events WHERE status = 'DONE' AND {_OR_WINDOW}",
    f"SELECT line, COUNT(*) AS n FROM events WHERE line IN ('l1','l2') AND {_OR_WINDOW} GROUP BY line",
    (f"SELECT COUNT(*) AS n FROM events WHERE {_OR_WINDOW} "
     "AND failed_at >= TIMESTAMP '2026-07-20 00:00:00 UTC'"),
]
_TYPES = {"done_at": _TS, "failed_at": _TS, "status": "varchar", "line": "varchar"}


def _sbr():
    return importlib.import_module("agents.storage_bound_rule")


@pytest.mark.parametrize("sql", _SHAPES)
def test_default_bounded_columns_does_not_read_the_or_window(sql):
    _parts, bounded = _sbr().bounded_columns(sql)
    assert "done_at" not in bounded


@pytest.mark.parametrize("sql", _SHAPES)
def test_decide_never_refuses_or_discloses_an_or_only_storage_bound(sql):
    """storage_bound_rule.decide reads the default bounds, so an OR-only bound
    on a storage column is invisible to it: no refusal, no disclosed bound."""
    sbr = _sbr()
    _parts, bounded = sbr.bounded_columns(sql)
    # done_at is the Iceberg storage source; the OR bound on it is not read.
    bound = sbr.decide("events", bounded, {"done_at"}, _TYPES, iceberg=True)
    # Only a top-level single-range bound on a storage column would be returned.
    if "done_at" in bounded:  # never, per the pin above
        raise AssertionError("the OR bound leaked into decide")
    assert bound is None or "done_at" not in bound.storage


def test_decide_window_ignores_an_or_only_storage_window():
    """With the window written only as an OR on the storage column done_at,
    decide_window sees no bounded time column and returns None (no refusal)."""
    sbr = _sbr()
    sql = f"SELECT COUNT(*) AS n FROM events WHERE {_OR_WINDOW}"
    _parts, bounded = sbr.bounded_columns(sql)
    result = sbr.decide_window("events", bounded, {"done_at"}, _TYPES, iceberg=True,
                               sql=sql, question="How many on 7/20 and 7/27?")
    assert result is None
