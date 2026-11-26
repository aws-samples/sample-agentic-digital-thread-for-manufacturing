# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How big a piece may be is learnt from evidence, not fixed.

A live re-run on a real table refused a 14-day count with ``piece_cap``
although the engine showed two pieces were enough (each 7-day half ran as one
query). Three causes, each pinned here:

1. open-ended boxes on a column the query bounds were never clipped to the
   query's bounds, so their floor (what no cut can remove) was always 0 and
   the planner kept cutting; every piece also carries the query's own WHERE,
   so a box is planned clipped (``athena_exact_boxes.effective``);
2. a box whose floor is at the target was cut anyway: it now runs whole, and
   only the engine's refusal (then a split) or a floor over the per-query
   limit (refused before running) decides;
3. a fixed calibration of 2.0 multiplied an estimate that was already too
   high. The calibration now starts at the estimate's word unless the
   refused query proves it low, a plan that would need more than a third of
   the piece cap is planned closer to the limit, and every finished piece
   recalibrates (the highest measured ratio; a refused piece gives a lower
   bound and is re-planned at it). Refused pieces count toward the piece cap.

Also: a split column the query does not read costs bytes only in the pieces
that filter on it (the live estimate counted the partition column in every
piece).

The live-shaped layout: three old unpartitioned files with wide completion
ranges (about 420 MB estimated) next to about 140 partitioned files, the
estimate 1.4x what the engine reads, a 1 GB per-query limit. Fully offline;
synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
from agents import athena_exact_boxes as bx

from ._athena_split_fakes import FakeLake

_COLUMNS = {"cell": "varchar", "state": "varchar", "qty": "bigint",
            "created": "timestamp(6)", "completed": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_STAMP = "%Y-%m-%d %H:%M:%S"
MB, GB = 1_000_000, 1_000_000_000
_SQL = ("SELECT COUNT(*) FROM events WHERE state = 'DONE' "
        "AND completed >= TIMESTAMP '2026-09-28 00:00:00' "
        "AND completed < TIMESTAMP '2026-10-12 00:00:00'")
#: Old unpartitioned files: their completion ranges (wide) and row counts.
_OLD_FILES = {"u1": ("2026-09-27 00:00:00", "2026-10-13 23:00:00", 70),
              "u2": ("2026-09-27 00:00:00", "2026-10-09 23:00:00", 60),
              "u3": ("2026-09-27 00:00:00", "2026-10-13 23:00:00", 20)}


def _live_rows() -> list[tuple]:
    rows = []
    start = datetime(2026, 8, 25)
    for day in range(49):
        for i in range(36):
            created = start + timedelta(days=day, minutes=40 * i)
            completed = created + timedelta(hours=(i * 7) % 60)
            rows.append(("cell-a" if i % 3 else "cell-b", ("DONE", "OPEN")[i % 2], i % 17,
                         created.strftime(_STAMP), completed.strftime(_STAMP)))
    for key, (lo, hi, count) in _OLD_FILES.items():
        first, last = datetime.strptime(lo, _STAMP), datetime.strptime(hi, _STAMP)
        step = (last - first) / (count - 1)
        for n in range(count):
            rows.append((key, ("DONE", "OPEN")[n % 2], n % 17,
                         (datetime(2026, 7, 1) + timedelta(hours=n)).strftime(_STAMP),
                         (first + step * n).strftime(_STAMP)))
    return rows


def _lake(*, factor: float, cutoff: int = GB) -> FakeLake:
    """One column of one row is 1 MB: the old files hold ~300 MB of the
    query's columns, the partitioned files the window overlaps ~1.2 GB."""
    return FakeLake(columns=_COLUMNS, rows=_live_rows(), part_col="created", cutoff=cutoff,
                    bytes_per_row=5 * MB, rows_per_file=12, estimate_factor=factor,
                    unpartitioned=lambda r: r[0].startswith("u"), unpart_key=lambda r: r[0])


def _exact(lake: FakeLake, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(_SQL, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


# --------------------------------------------------------------------------- #
# The live shape, both ways wrong
# --------------------------------------------------------------------------- #
def test_the_live_shape_finishes_in_few_pieces_when_the_estimate_is_high():
    lake = _lake(factor=1.4)
    assert lake.scan_bytes(_SQL) > lake.cutoff
    old = [f for f in lake.files if f["partition"] is None]
    assert len(old) == 3 and len([f for f in lake.files if f["partition"]]) >= 140
    result = _exact(lake)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(_SQL)
    assert split["pieces"] <= 4 and split["refused_pieces"] <= 1
    assert len(_pieces(lake)) == split["pieces"] + split["refused_pieces"]
    assert split["calibration"] < 1.0                     # learnt from the pieces that ran


def test_the_live_shape_recovers_when_the_estimate_is_low():
    # The estimate is half what the engine reads, and the whole query is far
    # over the limit, so the refusal's own lower bound says little: pieces
    # are refused, re-planned at the raised calibration, and the total is
    # still exact within the default caps.
    lake = _lake(factor=0.5, cutoff=800 * MB)
    result = _exact(lake)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(_SQL)
    assert split["refused_pieces"] >= 1 and split["calibration"] > 1.5
    pieces = _pieces(lake)
    assert len(pieces) == split["pieces"] + split["refused_pieces"] <= 16
    assert sum(lake.scan_bytes(p) <= lake.cutoff for p in pieces) == split["pieces"]


def test_a_split_column_the_query_does_not_read_costs_only_where_it_is_filtered():
    lake = _lake(factor=1.0)
    result = _exact(lake)
    # Every piece of the plan filters on the completion column only: the
    # partition column was never worth its bytes here.
    assert result["exact_split"]["pieces"] >= 2
    assert all('"created"' not in p for p in _pieces(lake))


# --------------------------------------------------------------------------- #
# Clipping to the query's bounds, and boxes that run whole
# --------------------------------------------------------------------------- #
_PART = bx.Dim("created", "timestamp(6)", "partition")
_EVENT = bx.Dim("completed", "timestamp(6)", "event", datetime(2026, 9, 28), datetime(2026, 10, 13))
_WIDE = bx.Span(datetime(2026, 9, 27), datetime(2026, 10, 14))


def test_an_open_ended_event_box_is_clipped_to_the_query_bounds():
    box = ((None, None), (None, None))
    eff = bx.effective(box, (_PART, _EVENT))
    assert eff == ((None, None), (datetime(2026, 9, 28), datetime(2026, 10, 13)))
    assert bx.covers(_WIDE, eff[1]) and not bx.covers(_WIDE, box[1])
    lower = ((None, None), (None, datetime(2026, 10, 5)))
    assert bx.effective(lower, (_PART, _EVENT))[1] == (datetime(2026, 9, 28), datetime(2026, 10, 5))
    outside = bx.Span(datetime(2026, 8, 1), datetime(2026, 8, 3))
    assert not bx.overlaps(outside, bx.effective(lower, (_PART, _EVENT))[1])
    assert bx.overlaps(outside, lower[1])                  # unclipped it would count


def test_floors_and_estimates_of_open_boxes_count_what_the_query_reads():
    groups = [bx.Group((None, _WIDE), 400 * MB),
              bx.Group((bx.Span(datetime(2026, 8, 1), datetime(2026, 8, 2)),
                        bx.Span(datetime(2026, 8, 1), datetime(2026, 8, 3))), 900 * MB)]
    dims, full = (_PART, _EVENT), bx.full_box((_PART, _EVENT))
    assert bx.floor(groups, full) == 0                       # read without the query's bounds
    assert bx.floor(groups, full, dims) == 400 * MB
    assert bx.estimate(groups, full, dims) == 400 * MB       # the old group is outside the query
    assert bx.estimate(groups, full) == 1300 * MB


def test_a_box_whose_floor_is_over_the_target_but_under_the_cap_runs_whole():
    groups = [bx.Group((None, _WIDE), 420 * MB)]
    groups += [bx.Group((bx.Span(datetime(2026, 9, 28) + timedelta(days=d), datetime(2026, 9, 29) + timedelta(days=d)),
                         bx.Span(datetime(2026, 9, 28) + timedelta(days=d), datetime(2026, 9, 30) + timedelta(days=d))),
                        10 * MB) for d in range(14)]
    leaves = bx.plan_boxes((_PART, _EVENT), groups, 300 * MB, 2)
    assert leaves == [bx.full_box((_PART, _EVENT))]          # never cut toward an unreachable target
    assert bx.floor(groups, leaves[0], (_PART, _EVENT)) == 420 * MB


def test_cut_points_stay_inside_the_query_bounds():
    groups = [bx.Group((None, bx.Span(datetime(2026, 9, 1) + timedelta(days=d),
                                      datetime(2026, 9, 2) + timedelta(days=d))), 50 * MB)
              for d in range(60)]
    leaves = bx.plan_boxes((_PART, _EVENT), groups, 120 * MB, 40)
    cuts = {r for b in leaves for r in b[1] if r is not None}
    assert cuts and all(datetime(2026, 9, 28) < c < datetime(2026, 10, 13) for c in cuts)


@pytest.mark.parametrize("seen, total, expected", [(GB, 2_330 * MB, 1.0), (GB, 400 * MB, 2.5)])
def test_the_first_calibration_is_the_estimate_unless_the_refusal_proves_it_low(seen, total, expected):
    from agents import athena_exact_split as sp
    run = sp._Run({}, None, sp.Caps(16, 10 ** 12, 100.0), 0.0)
    run.bounds.append(seen / total)
    run.recalibrate()
    assert run.calibration == pytest.approx(expected)
    run.measured.append(0.7)
    run.recalibrate()
    assert run.calibration == pytest.approx(max(0.7, seen / total))
