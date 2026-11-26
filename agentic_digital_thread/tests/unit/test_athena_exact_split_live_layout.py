# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The planner on a real table's file statistics (``_athena_split_live_layout``).

A live check on 14 days of a real table: the plain COUNT was computed exactly
in 5 pieces, while the same window grouped by one more column was refused at
planning with ``byte_cap`` (needing more than the 12 GiB cap), although its
estimate was only 1.3x the count's. Cause: 21 groups of files with no
partition directory and no bounds on the partition column (written under an
older, unpartitioned spec; 502 MB of the count's estimate, 690 MB of the
grouped one's) were taken to be read by EVERY piece cut on the partition
column. A grouped piece then had about 840 MB of a 900 MB target fixed before
its own data, so the plan needed 53 pieces that summed to 46.7 GB. The live
count's own pieces show the engine does not read those files above the
partition column's low end (it prunes them by the files' row-group
statistics): pieces there scanned 0.2 to 0.4 of an estimate that counted them.

Such files are now placed at the partition column's NULL end (they hold older
rows, or NULL when the column was added after them). Pinned here:

1. both live layouts plan within the piece and byte caps at the first
   calibration, and the grouped plan lands near the count's piece count;
2. under that placement the five live count pieces' measured bytes are a
   steady share of their estimates, where before they ranged 0.2 to 0.8;
3. the planning rows parse to that placement;
4. a first plan over the byte cap goes to the probe instead of refusing.

Pure and offline (planning rows only; no engine).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from agents import athena_exact_boxes as bx
from agents import athena_exact_plan as plan
from agents import athena_exact_split as sp

from . import _athena_split_live_layout as live

GB = 1_000_000_000
_CAND = plan.Candidate("part_col", "timestamp", "day", "part_col_day", 1)
_DIMS = (bx.Dim("part_col", "timestamp", "partition"),
         bx.Dim("event_col", "timestamp", "event", datetime(2026, 9, 28), datetime(2026, 10, 13)))


def _rows(groups) -> list[list]:
    """The planning-read rows (``planning_sql`` columns) for the captured groups."""
    return [["true", pv, None, None, elo, ehi, str(files), str(est), str(est), None, str(x0), str(x1)]
            for pv, elo, ehi, files, est, x0, x1 in groups]


def _layout(groups) -> plan.Layout:
    return plan.parse_plan(_rows(groups), _CAND, _DIMS, ["event_col"])


def _planned(layout: plan.Layout) -> tuple[sp._Run, list]:
    """The first plan exactly as a split makes it after a refused 1 GB query."""
    class Refusal:
        scanned_bytes = GB
    run = sp._Run({}, None, sp.Caps(sp.DEFAULT_MAX_PIECES, sp.DEFAULT_MAX_TOTAL_BYTES, 150.0), 0.0)
    return run, sp._plan(run, layout, GB, Refusal)


@pytest.mark.parametrize("groups", [live.COUNT, live.GROUPED], ids=["count", "grouped"])
def test_the_live_layouts_plan_within_the_caps(groups):
    layout = _layout(groups)
    run, leaves = _planned(layout)
    planned = sum(bx.estimate(layout.groups, b, layout.dims) for b in leaves) * run.calibration
    assert 2 <= len(leaves) <= run.caps.max_pieces // 3
    assert planned <= run.caps.max_total_bytes / 2
    assert not run.over_cap_plan


def test_the_grouped_plan_lands_near_the_counts():
    count = len(_planned(_layout(live.COUNT))[1])
    grouped = len(_planned(_layout(live.GROUPED))[1])
    assert grouped <= count + 2


def test_unpartitioned_files_parse_to_the_partition_columns_null_end():
    layout = _layout(live.GROUPED)
    low = [g for g in layout.groups if g.spans[0] == plan.LOW_END]
    assert len(low) == sum(1 for g in live.GROUPED if g[0] is None) == 21
    assert layout.low_end_files == sum(g[3] for g in live.GROUPED if g[0] is None)
    every = layout.without_low_end_guess()
    assert sum(1 for g in every.groups if g.spans[0] is None) == 21 and every.low_end_files == 0
    # The pieces holding the partition column's low end always read them.
    low_box = ((None, datetime(2026, 9, 1)), (None, None))
    assert bx.irreducible(layout.groups, low_box, layout.dims) >= sum(
        g.weight((0,)) for g in low if bx.covers(g.spans[1], (datetime(2026, 9, 28), datetime(2026, 10, 13))))
    assert bx.floor(layout.groups, low_box, layout.dims) < bx.irreducible(layout.groups, low_box, layout.dims)


def _d(text: str) -> datetime:
    return datetime.fromisoformat(text)


#: The five pieces of the live 14-day count and the bytes each scanned.
_LIVE_PIECES = (
    (((None, _d("2026-09-21")), (None, _d("2026-10-08"))), 771_080_910),
    (((None, _d("2026-10-01")), (_d("2026-10-08"), None)), 968_340_574),
    (((_d("2026-09-21"), _d("2026-09-26")), (None, _d("2026-10-08"))), 181_556_218),
    (((_d("2026-09-26"), None), (None, _d("2026-10-08"))), 488_365_538),
    (((_d("2026-10-01"), None), (_d("2026-10-08"), None)), 474_884_279),
)


def test_the_live_count_pieces_read_a_steady_share_of_their_estimates():
    layout = _layout(live.COUNT)
    ratios = [scanned / bx.estimate(layout.groups, box, layout.dims) for box, scanned in _LIVE_PIECES]
    assert 0.6 <= min(ratios) and max(ratios) <= 0.9
    every = layout.without_low_end_guess()
    before = [scanned / bx.estimate(every.groups, box, every.dims) for box, scanned in _LIVE_PIECES]
    assert min(before) < 0.25 and max(before) > 0.7          # every piece charged with them


def test_a_first_plan_over_the_byte_cap_goes_to_the_probe_instead_of_refusing():
    # With the files charged to every partition piece (the placement the live
    # refusal was planned with), the first plan is far over the byte cap.
    # It is no longer refused before any evidence: it is kept for the probe.
    layout = _layout(live.GROUPED).without_low_end_guess()
    run, leaves = _planned(layout)
    planned = sum(bx.estimate(layout.groups, b, layout.dims) for b in leaves) * run.calibration
    assert planned > run.caps.max_total_bytes and run.over_cap_plan
