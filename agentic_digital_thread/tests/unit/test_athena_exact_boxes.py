# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The box planner of the exact-total split, and the ``$files`` read behind it.

Measured on a real table: a 14-day count was refused with ``piece_cap``. The
table holds hundreds of files with no partition directory (an older
unpartitioned spec), and no file carries bounds for the partition source
column, so those files could not be placed on it. The one-column planner
counted them in every range, so every partition start became a cut (394
ranges). They do carry bounds for the column the query bounds.

Pinned here, pure and offline:

1. files that cannot be placed on a column never force a cut per partition
   start: a box whose floor (what no cut can remove) is over the target is
   left whole;
2. with the query's bounded column as a second dimension, those files are cut
   on their event bounds and the plan stays within the piece cap;
3. the leaves tile every row exactly once, NULLs on every dimension included
   (checked in SQLite with the rendered predicates);
4. a cut minimises the larger child; a refused box is halved by the estimate,
   else at a time midpoint;
5. the planning read (executed in SQLite) counts the bytes of every column a
   piece references (the split columns too), counts a delete file whole and
   places it on its partition value only, places unpartitioned files nowhere
   on the partition column, and clips event bounds to the query's range.

Synthetic names only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from agents import athena_exact_boxes as bx
from agents import athena_exact_plan as plan
from agents.athena_exact_shape import parse_shape

from ._athena_split_fakes import _register_functions

_D0 = datetime(2026, 9, 1)
PART = bx.Dim("created", "timestamp(6)", "partition")
EVENT = bx.Dim("completed", "timestamp(6)", "event", datetime(2026, 9, 28), datetime(2026, 10, 12))
#: The same column with no bound in the query (nothing to clip to).
EVENT_OPEN = bx.Dim("completed", "timestamp(6)", "event")
MB = 1_000_000


def _day(n: int) -> datetime:
    return _D0 + timedelta(days=n)


def _partitioned(days: int, est: int, event_lag: int = 2) -> list:
    """One group per partition day; its event bounds are its day plus a lag."""
    return [bx.Group((bx.Span(_day(d), _day(d + 1)), bx.Span(_day(d), _day(d + event_lag))), est)
            for d in range(days)]


# --------------------------------------------------------------------------- #
# 1-2. Unplaceable files
# --------------------------------------------------------------------------- #
def test_unplaceable_files_never_force_a_cut_per_partition_start():
    # The measured failure, one dimension: 394 small partition groups and one
    # big group with no partition value and no bounds on the column.
    groups = [bx.Group((bx.Span(_day(-d), _day(-d + 1)),), 840_000) for d in range(394)]
    groups.append(bx.Group((None,), 913 * MB))
    leaves = bx.plan_boxes((PART,), groups, 300 * MB, 16)
    assert leaves == [bx.full_box((PART,))]
    assert bx.floor(groups, leaves[0]) == 913 * MB


def test_unpartitioned_files_are_cut_on_the_event_column():
    # The same table with the query's bounded column as a second dimension:
    # the unpartitioned files carry event bounds (two days each, over 14 days).
    groups = _partitioned(30, 3 * MB)
    for n in range(165):
        start = datetime(2026, 9, 28) + timedelta(days=n % 14)
        groups.append(bx.Group((None, bx.Span(start, start + timedelta(days=2))), 5_500_000))
    dims = (PART, EVENT)
    total = bx.estimate(groups, bx.full_box(dims), dims)
    leaves = bx.plan_boxes(dims, groups, 300 * MB, 16)
    assert 2 <= len(leaves) <= 16
    assert all(bx.estimate(groups, b, dims) <= 300 * MB for b in leaves)
    assert any(b[1] != (None, None) for b in leaves)          # cut on the event column
    assert sum(bx.estimate(groups, b, dims) for b in leaves) >= total


def test_partitioned_files_with_wide_event_bounds_are_cut_on_the_partition():
    groups = [bx.Group((bx.Span(_day(d), _day(d + 1)), bx.Span(_day(-400), _day(400))), 50 * MB)
              for d in range(30)]
    leaves = bx.plan_boxes((PART, EVENT), groups, 300 * MB, 16)
    assert all(b[1] == (None, None) for b in leaves)          # event cuts cannot help
    assert all(bx.estimate(groups, b) <= 300 * MB for b in leaves) and len(leaves) <= 8


def test_a_plan_over_the_piece_cap_is_refused():
    groups = [bx.Group((bx.Span(_day(d), _day(d + 1)), None), 100 * MB) for d in range(40)]
    with pytest.raises(bx.TooManyPieces):
        bx.plan_boxes((PART, EVENT), groups, 150 * MB, 16)


# --------------------------------------------------------------------------- #
# 3. Exact tiling, NULLs on every dimension
# --------------------------------------------------------------------------- #
def _table_with_nulls() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE t (id INTEGER, "created" TEXT, "completed" TEXT)')
    rows = []
    for n in range(400):
        created = None if n % 23 == 0 else (_D0 + timedelta(hours=7 * n)).strftime("%Y-%m-%d %H:%M:%S")
        completed = None if n % 17 == 0 else (_D0 + timedelta(hours=5 * n + 3)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((n, created, completed))
    db.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return db


def _sqlite(predicate: str) -> str:
    return predicate.replace("TIMESTAMP '", "'")


@pytest.mark.parametrize("event", [EVENT_OPEN, EVENT], ids=["open", "clipped"])
@pytest.mark.parametrize("target", [40 * MB, 15 * MB, 6 * MB])
def test_leaves_tile_every_row_exactly_once_nulls_included(target, event):
    # Clipping to the query's bounds changes where cuts go, never the tiling:
    # the boxes stay open-ended, so rows outside the bounds still land once.
    groups = _partitioned(60, 2 * MB, event_lag=5)
    groups += [bx.Group((None, bx.Span(_day(n), _day(n + 3))), 1 * MB) for n in range(0, 60, 2)]
    groups.append(bx.Group((bx.Span(None, None, nulls=True), None), 1 * MB))
    leaves = bx.plan_boxes((PART, event), groups, target, 400)
    assert len(leaves) > (3 if event is EVENT_OPEN else 1)
    db = _table_with_nulls()
    seen: list[int] = []
    for box in leaves:
        pred = bx.box_predicate((PART, event), box, lambda c: f'"{c}"')
        ids = [r[0] for r in db.execute(f"SELECT id FROM t WHERE {_sqlite(pred)}")]  # nosec B608 - test SQL
        seen += ids
    assert sorted(seen) == list(range(400))


def test_box_predicates_and_labels():
    box = ((None, _day(4)), (_day(27), _day(29)))
    pred = bx.box_predicate((PART, EVENT), box, lambda c: f'"{c}"')
    assert pred == ("(\"created\" < TIMESTAMP '2026-09-05 00:00:00' OR \"created\" IS NULL) AND "
                    "(\"completed\" >= TIMESTAMP '2026-09-28 00:00:00' AND "
                    "\"completed\" < TIMESTAMP '2026-09-30 00:00:00')")
    assert bx.box_label((PART, EVENT), box) == ("created before 2026-09-05 or null; "
                                               "completed 2026-09-28 to 2026-09-30")
    zoned = bx.Dim("u", "timestamp(6) with time zone", "event")
    dated = bx.Dim("d", "date", "partition")
    assert bx.box_predicate((zoned, dated), ((_day(1), None), (None, None)), lambda c: c) == (
        "u >= TIMESTAMP '2026-09-02 00:00:00 UTC'")
    assert bx.box_predicate((dated,), ((_day(1), None),), lambda c: c) == "d >= DATE '2026-09-02'"
    assert bx.box_predicate((dated,), bx.full_box((dated,)), lambda c: c) is None
    assert bx.box_label((dated,), bx.full_box((dated,))) == "every row"


# --------------------------------------------------------------------------- #
# 4. Cuts, floors, runtime halving
# --------------------------------------------------------------------------- #
def test_best_cut_minimises_the_larger_child():
    groups = [bx.Group((bx.Span(_day(0), _day(1)),), 10), bx.Group((bx.Span(_day(1), _day(2)),), 10),
              bx.Group((bx.Span(_day(2), _day(3)),), 20), bx.Group((None,), 5)]
    score, dim, point = bx.best_cut(groups, bx.full_box((PART,)), (PART,))
    assert (score, dim, point) == (25, 0, _day(2))


def test_a_null_partition_and_maybe_null_files_stay_in_the_lowest_child():
    groups = [bx.Group((bx.Span(None, None, nulls=True),), 7),
              bx.Group((bx.Span(_day(5), _day(6), nulls=True),), 3)]
    left, right = bx.cut(bx.full_box((PART,)), 0, _day(5))
    assert bx.estimate(groups, left) == 10 and bx.estimate(groups, right) == 3


def test_covering_groups_make_the_floor():
    groups = [bx.Group((bx.Span(_day(0), _day(10)), None), 40), bx.Group((None, None), 2),
              bx.Group((bx.Span(_day(3), _day(4)), None), 9)]
    box = ((_day(2), _day(5)), (None, None))
    assert bx.floor(groups, box) == 42 and bx.estimate(groups, box) == 51


def test_split_box_uses_the_estimate_then_a_midpoint():
    groups = [bx.Group((bx.Span(_day(0), _day(1)), bx.Span(_day(0), _day(9))), 10),
              bx.Group((bx.Span(_day(1), _day(2)), bx.Span(_day(0), _day(9))), 10)]
    dims = (PART, EVENT_OPEN)
    left, right = bx.split_box(groups, bx.full_box(dims), dims)
    assert left[0] == (None, _day(1)) and right[0] == (_day(1), None)
    one = ((_day(0), _day(1)), (None, None))
    halves = bx.split_box(groups, one, dims)
    assert halves is not None and halves[0][1][1] is not None          # cut on the event column
    narrow = ((_day(0), _day(1)), (datetime(2026, 9, 28), datetime(2026, 9, 28, 1)))
    assert bx.split_box(groups, narrow, dims) is not None      # partition halved by hours
    tiny = ((_day(0), _day(0) + timedelta(hours=1)), (datetime(2026, 9, 28), datetime(2026, 9, 28, 1)))
    assert bx.split_box(groups, tiny, dims) is None


# --------------------------------------------------------------------------- #
# 5. The planning read, executed in SQLite over a hand-built "$files"
# --------------------------------------------------------------------------- #
_META = plan.TableMeta(
    columns={"state": ("state", "varchar"), "created": ("created", "timestamp(6)"),
             "completed": ("completed", "timestamp(6)"), "qty": ("qty", "bigint")},
    field_ids={"state": 1, "created": 2, "completed": 3, "qty": 4}, iceberg=True)
_CAND = plan.Candidate("created", "timestamp(6)", "day", "created_day", 1)
_SQL = ("SELECT COUNT(*) FROM t WHERE state = 'DONE' AND completed >= TIMESTAMP '2026-09-28 00:00:00' "
        "AND completed < TIMESTAMP '2026-10-12 00:00:00'")


def _files_db(files) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    _register_functions(db)
    db.execute('CREATE TABLE "t$files" (file_path TEXT, content INTEGER, file_size_in_bytes INTEGER, '
               "column_sizes TEXT, lower_bounds TEXT, upper_bounds TEXT)")
    for path, content, size, sizes, lower, upper in files:
        db.execute('INSERT INTO "t$files" VALUES (?,?,?,?,?,?)',
                   (path, content, size, json.dumps(sizes), json.dumps(lower), json.dumps(upper)))
    return db


def _plan_rows(files):
    shape = parse_shape(_SQL)
    dims = plan.split_dims(_CAND, shape, _META)
    sql, newest = plan.planning_sql('"t$files"', _CAND, dims, _META, shape)
    rows = [[None if v is None else str(v) for v in r]
            for r in _files_db(files).execute(sql.replace("CAST(NULL AS varchar)", "NULL"))]
    return dims, plan.parse_plan(rows, _CAND, dims, newest), sql


def test_planning_read_places_files_and_counts_the_split_columns_apart():
    sizes = {"1": 100, "2": 1000, "3": 10, "4": 99999}
    files = [
        ("s3://b/t/data/created_day=2026-09-29/f1.parquet", 0, 5000, sizes,
         {"3": "2026-09-29T01:00:00"}, {"3": "2026-10-01T05:00:00"}),
        ("s3://b/t/data/f2.parquet", 0, 7000, sizes,                     # unpartitioned, wide bounds
         {"3": "2026-08-01T00:00:00Z"}, {"3": "2026-12-01T00:00:00Z"}),
        ("s3://b/t/data/f3.parquet", 0, 7000, sizes, {}, {}),             # no bounds at all
        ("s3://b/t/data/created_day=2026-09-29/d1.parquet", 1, 300, {}, {}, {}),  # a delete file
        ("s3://b/t/data/created_day=2026-07-01/f4.parquet", 0, 9000, sizes,       # outside the window
         {"3": "2026-07-01T00:00:00"}, {"3": "2026-07-02T00:00:00"}),
    ]
    dims, layout, sql = _plan_rows(files)
    assert [d.column for d in dims] == ["created", "completed"]
    for fid in (1, 2, 3):                                                 # state, created, completed
        assert f"coalesce(element_at(column_sizes, {fid}), file_size_in_bytes / 4)" in sql
    assert "element_at(column_sizes, 4)" not in sql                       # qty is not read
    by_est = sorted(layout.groups, key=lambda g: (g.est, str(g.spans)))
    # The query reads state and completed; created (the partition split
    # column) counts only for a piece that filters on it.
    per_file = 100 + 10 + plan.FILE_OVERHEAD_BYTES
    assert [g.est for g in by_est] == [300, per_file, per_file, per_file]
    assert [g.extra for g in by_est] == [(0, 0), (1000, 0), (1000, 0), (1000, 0)]
    delete, *data = by_est
    assert delete.spans == (bx.Span(datetime(2026, 9, 29), datetime(2026, 9, 30)), None)
    spans = {g.spans for g in data}
    assert (bx.Span(datetime(2026, 9, 29), datetime(2026, 9, 30)),
            bx.Span(datetime(2026, 9, 29), datetime(2026, 10, 2))) in spans
    # Unpartitioned files with no bounds on the partition column sit at its
    # NULL end; the one with no event bounds is unplaceable on that column.
    assert (plan.LOW_END, bx.Span(datetime(2026, 9, 27), datetime(2026, 10, 14))) in spans  # clipped
    assert (plan.LOW_END, None) in spans
    assert layout.unplaced_files == 1 and layout.low_end_files == 2


def test_split_dims_take_two_sided_bounds_first_and_skip_the_partition_column():
    shape = parse_shape("SELECT COUNT(*) FROM t WHERE created >= TIMESTAMP '2026-09-01 00:00:00' "
                        "AND completed >= TIMESTAMP '2026-09-28 00:00:00' "
                        "AND completed < TIMESTAMP '2026-10-12 00:00:00'")
    dims = plan.split_dims(_CAND, shape, _META)
    assert [(d.column, d.kind) for d in dims] == [("created", "partition"), ("completed", "event")]
    assert dims[1].lo == datetime(2026, 9, 28) and dims[1].hi == datetime(2026, 10, 13)
    assert plan.split_dims(None, parse_shape("SELECT COUNT(*) FROM t"), _META) == ()


def test_planning_read_without_a_partition_column():
    shape = parse_shape(_SQL)
    dims = plan.split_dims(None, shape, _META)
    sql, newest = plan.planning_sql('"t$files"', None, dims, _META, shape)
    assert "regexp_extract" not in sql and newest == ["completed"]
    with pytest.raises(ValueError):
        plan.planning_sql('"t$files"', plan.Candidate("c", "date", "identity", "c'x", 1),
                          plan.split_dims(None, shape, _META), _META, shape)


def test_a_column_without_a_reported_size_counts_an_average_share():
    # A column with no field id, or a file that reports no size for it,
    # counts an average column's share of the file, not the whole file.
    meta = plan.TableMeta(_META.columns, {"completed": 3}, True)
    sql = plan._estimate_sql(parse_shape(_SQL), meta, ())
    assert sql == ("CASE WHEN content <> 0 THEN file_size_in_bytes ELSE file_size_in_bytes / 4 + "
                   "coalesce(element_at(column_sizes, 3), file_size_in_bytes / 4) + "
                   f"{plan.FILE_OVERHEAD_BYTES} END")
    files = [("s3://b/t/data/f1.parquet", 0, 8000, {"3": 10}, {}, {})]
    rows = [[None if v is None else str(v) for v in r]
            for r in _files_db(files).execute(f'SELECT {sql} FROM "t$files"')]  # nosec B608 - test SQL
    assert rows == [[str(2000 + 10 + plan.FILE_OVERHEAD_BYTES)]]


def test_files_at_the_null_end_over_the_target_are_not_cut_into_slices():
    # Files at the partition column's NULL end are in the piece holding the
    # low corner however that piece is cut: once they alone are over the
    # target, cutting it on the event column only copies them into every
    # slice, so it runs whole (the irreducible part, not just the floor).
    low_end = bx.Span(None, None, nulls=True)
    groups = [bx.Group((low_end, bx.Span(datetime(2026, 9, 27), datetime(2026, 10, 14))), 700 * MB)]
    groups += _partitioned(30, 5 * MB)
    dims = (PART, EVENT)
    box = ((None, _day(10)), (None, None))
    assert bx.floor(groups, box, dims) == 0 and bx.irreducible(groups, box, dims) == 700 * MB
    leaves = bx.plan_boxes(dims, groups, 500 * MB, 16)
    low = [b for b in leaves if b[0][0] is None]
    assert len(low) == 1 and low[0][1] == (None, None)
