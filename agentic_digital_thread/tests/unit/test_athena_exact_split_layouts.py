# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exact totals on table layouts a partition-only split cannot handle.

A live check on a real table refused a 14-day count with ``piece_cap``:
hundreds of files sat outside any partition directory and carried no bounds
for the partition source column, so every partition piece had to read them.
They did carry bounds for the column the query bounds. The split now cuts on
two dimensions (the partition source column and the query's bounded temporal
column), so these layouts are computed exactly, end to end through the
connector and the file-level ``FakeLake`` (Iceberg-style pruning by partition
values and bounds, the planning ``$files`` read executed in SQLite):

* unpartitioned files with narrow event bounds next to partitioned ones (the
  live failure): cut on the event column;
* partitioned files with wide event bounds: cut on the partition only;
* both at once: cut on both;
* no partition field at all: cut on the event column only;
* delete files, the NULL partition and NULL event values: still exact.

Also pinned: a row-level expression inside COUNT/SUM/MIN/MAX is computed
exactly, a query that reads the current time says it was evaluated per piece,
pieces still running when a split is abandoned are stopped, and the query's
referenced columns hold no SQL words.

Fully offline; synthetic names only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
from agents.athena_exact_shape import parse_shape

from ._athena_split_fakes import FakeLake, RunsUntilStopped

_COLUMNS = {"site": "varchar", "state": "varchar", "qty": "bigint",
            "created": "timestamp(6)", "completed": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_D0 = datetime(2026, 8, 1)
_STAMP = "%Y-%m-%d %H:%M:%S"


def _rows(days: int, per_day: int, *, null_every: int = 0) -> list[tuple]:
    rows = []
    for day in range(days):
        for i in range(per_day):
            created = _D0 + timedelta(days=day, minutes=47 * i)
            completed = created + timedelta(hours=(i * 5) % 72)
            n = day * per_day + i
            rows.append((("site-a", "site-b", None)[n % 3], ("DONE", "OPEN")[n % 2], n % 23,
                         None if null_every and n % null_every == 0 else created.strftime(_STAMP),
                         None if null_every and n % null_every == 1 else completed.strftime(_STAMP)))
    return rows


def _window(lo: str, hi: str, select: str = "COUNT(*)", tail: str = "") -> str:
    return (f"SELECT {select} FROM events WHERE state = 'DONE' "  # nosec B608 - test SQL
            f"AND completed >= TIMESTAMP '{lo} 00:00:00' AND completed < TIMESTAMP '{hi} 00:00:00'{tail}")


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _same(got, want) -> bool:
    key = lambda r: json.dumps(r)  # noqa: E731 - a sort key for rows holding None
    return sorted(got, key=key) == sorted(want, key=key)


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #
def test_unpartitioned_files_next_to_partitioned_ones_are_cut_on_the_event_column():
    rows = _rows(60, 30)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=8_000_000,
                    unpartitioned=lambda r: r[3] < "2026-08-25", unpart_sort="completed")
    sql = _window("2026-08-15", "2026-08-29")
    unpartitioned = [f for f in lake.files if f["partition"] is None]
    # Every partition-only piece would read all the unpartitioned files the
    # window overlaps, and those alone are over the cutoff.
    overlap = [f for f in unpartitioned if f["bounds"]["completed"][1] >= "2026-08-15"
               and f["bounds"]["completed"][0] < "2026-08-29"]
    assert len(overlap) * 6 * lake.col_bytes * 3 > lake.cutoff
    assert lake.scan_bytes(sql) > lake.cutoff
    result = _exact(lake, sql)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(sql)
    assert split["split_columns"] == ["created", "completed"] and split["pieces"] <= 16
    assert any('"completed"' in p for p in _pieces(lake))


def test_partitioned_files_with_wide_event_bounds_are_cut_on_the_partition():
    rows = _rows(30, 20)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=7_000_000,
                    widen={"completed": ("2026-01-01 00:00:00", "2026-12-31 00:00:00")})
    sql = _window("2026-08-10", "2026-08-24")
    result = _exact(lake, sql)
    assert result["rows"] == lake.reference(sql) and result["exact_split"]["pieces"] <= 16
    assert all('"completed"' not in p for p in _pieces(lake))
    assert all('"created"' in p for p in _pieces(lake))


def test_a_layout_that_needs_both_columns_is_cut_on_both():
    # Unpartitioned files (narrow event bounds) sit at the partition column's
    # low end; partitioned files (wide event bounds) are in every event piece:
    # the low piece is cut on the event column, the rest on the partition.
    # The engine prunes by the files' own row values (footer statistics).
    rows = _rows(30, 15)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=8_000_000,
                    unpartitioned=lambda r: r[3] < "2026-08-12", unpart_sort="completed",
                    widen={"completed": ("2026-01-01 00:00:00", "2026-12-31 00:00:00")},
                    footer_stats=True)
    sql = _window("2026-08-05", "2026-08-19", "site, COUNT(*), SUM(qty)", " GROUP BY site")
    result = _exact(lake, sql)
    assert result["exact_split"]["pieces"] <= 16
    assert _same(result["rows"], lake.reference(sql))
    pieces = _pieces(lake)
    assert any('"created"' in p and '"completed"' in p for p in pieces)
    over = sum(lake.scan_bytes(p) > lake.cutoff for p in pieces)
    assert over == result["exact_split"]["refused_pieces"]


def test_a_table_with_no_partition_field_is_cut_on_the_event_column_only():
    rows = _rows(40, 20)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=5_000_000,
                    unpartitioned=lambda r: True, unpart_sort="created")
    sql = _window("2026-08-05", "2026-08-25")
    result = _exact(lake, sql)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(sql)
    assert split["split_columns"] == ["completed"] and split["partition_column"] is None
    assert all('"created"' not in p for p in _pieces(lake))


def test_delete_files_null_partitions_and_null_event_values_stay_exact():
    rows = _rows(30, 20, null_every=13)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=9_000_000,
                    delete_files=("2026-08-03", "2026-08-11", "null", None))
    for sql in (_window("2026-08-02", "2026-08-28", "state, COUNT(*), SUM(qty), MIN(completed)",
                        " GROUP BY state"),
                "SELECT COUNT(*), COUNT(completed), COUNT(created), SUM(qty) FROM events"):
        result = _exact(lake, sql)
        assert _same(result["rows"], lake.reference(sql)), sql
        assert result["exact_split"]["pieces"] > 1


def test_the_live_failure_layout_no_longer_needs_a_piece_per_partition_start():
    # Partitioned files of 60 days, every one of them small, next to big
    # unpartitioned files the query's window overlaps: the old plan cut at
    # every partition start and went over the piece cap.
    rows = _rows(60, 30)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=8_000_000,
                    unpartitioned=lambda r: r[3] < "2026-08-25", unpart_sort="completed",
                    unpart_rows_per_file=12)
    sql = _window("2026-08-11", "2026-08-25")
    result = _exact(lake, sql, {"exact_split_max_pieces": 12})
    assert result["rows"] == lake.reference(sql)


# --------------------------------------------------------------------------- #
# Row-level expressions, the clock, stopping, referenced columns
# --------------------------------------------------------------------------- #
@pytest.fixture
def lake():
    return FakeLake(columns=_COLUMNS, rows=_rows(30, 20), part_col="created", cutoff=8_000_000)


def test_row_level_expressions_inside_aggregates_are_exact(lake):
    sql = _window("2026-08-02", "2026-08-28",
                  "site, SUM(CASE WHEN qty > 10 THEN 1 ELSE 0 END) AS big, "
                  "COUNT(CASE WHEN state = 'DONE' THEN 1 END), "
                  "MAX(CASE WHEN qty > 3 THEN completed END)", " GROUP BY site")
    result = _exact(lake, sql)
    assert _same(result["rows"], lake.reference(sql))
    assert result["exact_split"]["pieces"] > 1


def test_a_query_reading_the_current_time_reads_one_frozen_instant(lake):
    # Every piece reads the same instant (test_athena_exact_split_clock).
    sql = _window("2026-08-02", "2026-08-28", tail=" AND completed < current_timestamp")
    result = _exact(lake, sql)
    assert result["rows"] == lake.reference(sql)
    assert result["exact_split"]["clock_frozen_at"].endswith("Z")
    assert "clock_per_piece" not in result["exact_split"] and "each piece" not in result["note"]
    plain = _exact(lake, _window("2026-08-02", "2026-08-28"))
    assert "clock_frozen_at" not in plain["exact_split"] and "current time" not in plain["note"]


def test_pieces_still_running_when_the_split_is_abandoned_are_stopped(lake, monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)
    # A piece keeps running until it is stopped, so it cannot end on its own
    # poll window at the split's deadline: only the split stops it.
    athena = RunsUntilStopped(lake)
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(athena, _window("2026-08-02", "2026-08-28"), {"exact_split_max_seconds": 3})
    assert info.value.exact_split["reason"] == "wall_time"
    slow = [qid for qid, q in lake._queries.items() if "FOR VERSION AS OF" in q]
    assert slow and set(slow) <= set(lake.stopped)


def test_referenced_columns_hold_no_sql_words():
    shape = parse_shape("SELECT site, COUNT(*) FROM events WHERE state = 'DONE' AND qty IS NOT NULL "
                        "AND completed < current_timestamp GROUP BY site ORDER BY site DESC LIMIT 5")
    assert shape.referenced == {"site", "events", "state", "qty", "completed"}
    assert shape.uses_clock is True
    assert not re.search(r"\b(select|and|from|count|limit)\b", " ".join(shape.referenced))


def test_a_callers_tighter_caps_and_listener_reach_the_split(lake):
    from agents import athena_exact_split as sp
    started = []
    with sp.limited(sp.CapsLimit(max_pieces=2), lambda: started.append(True)):
        with pytest.raises(at.AthenaEngineScanLimitError) as info:
            _exact(lake, _window("2026-08-02", "2026-08-28"))
    assert info.value.exact_split["reason"] == "piece_cap" and started == [True]
    assert _exact(lake, _window("2026-08-02", "2026-08-28"))["exact_split"]["pieces"] > 2


def test_unpartitioned_files_the_engine_reads_everywhere_drop_the_low_end_guess():
    # The engine cannot prune the unpartitioned files from any partition
    # piece here (no footer statistics in the fake): the first piece above
    # the low end reads far more than its estimate, the placement guess is
    # dropped, the queue is re-planned with them in every piece, and the
    # total is still exact.
    rows = _rows(30, 15)
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=14_000_000,
                    unpartitioned=lambda r: r[3] < "2026-08-12", unpart_sort="completed",
                    widen={"completed": ("2026-01-01 00:00:00", "2026-12-31 00:00:00")})
    sql = _window("2026-08-05", "2026-08-19", "site, COUNT(*), SUM(qty)", " GROUP BY site")
    result = _exact(lake, sql)
    split = result["exact_split"]
    assert _same(result["rows"], lake.reference(sql))
    assert split["partition_low_end_guess_dropped"] is True
    assert split["pieces"] + split["refused_pieces"] <= 16
