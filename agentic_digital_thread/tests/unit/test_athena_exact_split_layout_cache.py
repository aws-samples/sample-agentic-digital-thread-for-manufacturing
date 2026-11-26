# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A second refusal on the same table snapshot does not read the layout again.

Measured in an answer eval: the exact split refused the same query
(``unsplittable``) five times in fifteen minutes on one table, and each
refusal read the table's ``$files`` partition fields and layout statistics
again, about 11 s of a 60 s question each time. The split now keeps those
rows per (table, pinned snapshot, layout SQL) in a small bounded cache, so a
refusal the split already decided costs only the refused query and the
snapshot read; a new snapshot reads the layout again.

Fully offline (``FakeLake``); synthetic names only.
"""
from __future__ import annotations

import pytest

import agents.athena_exact_split as split
import agents.athena_tools as at

from . import test_athena_exact_split as base
from ._athena_split_fakes import FakeLake

_REFUSE = {"exact_split_max_pieces": 1}


@pytest.fixture
def lake():
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=base._CUTOFF)


def _files_reads(lake: FakeLake) -> list[str]:
    return [q for q in lake.started if "$files" in q]


def _refusal(lake: FakeLake, sql: str) -> str:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        base._exact(lake, sql, _REFUSE)
    return info.value.exact_split["reason"]


def test_a_second_refusal_on_the_same_snapshot_reads_the_layout_once(lake):
    assert _refusal(lake, base._P1) == "piece_cap"
    first = len(_files_reads(lake))
    assert first == 2                      # the partition fields and the layout rows
    assert _refusal(lake, base._P1) == "piece_cap"
    assert len(_files_reads(lake)) == first
    # The snapshot is still read every time: it is what the cache is keyed on.
    assert sum("$snapshots" in q for q in lake.started) == 2


def test_a_new_snapshot_reads_the_layout_again(lake):
    assert _refusal(lake, base._P1) == "piece_cap"
    first = len(_files_reads(lake))
    lake.snapshot += 1
    assert _refusal(lake, base._P1) == "piece_cap"
    assert len(_files_reads(lake)) == 2 * first


def test_another_window_reads_its_own_layout_rows(lake):
    assert _refusal(lake, base._P1) == "piece_cap"
    other = base._P1.replace("2026-09-20", "2026-09-25")
    assert _refusal(lake, other) == "piece_cap"
    # Same partition-field read (cached), its own layout statistics.
    assert len(_files_reads(lake)) == 3


def test_the_cache_is_bounded():
    for n in range(split.LAYOUT_CACHE_SIZE + 5):
        split._layout_cache_put(("k", n), [["row"]])
    assert len(split._LAYOUT_CACHE) == split.LAYOUT_CACHE_SIZE
    assert split._layout_cache_get(("k", 0)) is None
    assert split._layout_cache_get(("k", split.LAYOUT_CACHE_SIZE + 4)) == [["row"]]
    split._layout_cache_put(("big",), [["r"]] * (split.LAYOUT_CACHE_MAX_ROWS + 1))
    assert split._layout_cache_get(("big",)) is None


def test_a_cached_layout_is_not_shared_with_the_caller():
    split._layout_cache_put(("k",), [["a", "1"]])
    rows = split._layout_cache_get(("k",))
    rows[0][0] = "changed"
    rows.append(["b"])
    assert split._layout_cache_get(("k",)) == [["a", "1"]]
