# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The total-byte cap of an exact split counts what is already in flight.

Each launch was checked against the bytes of FINISHED queries plus that one
piece's estimate, so a split could start several pieces at once whose real
bytes together went past ``exact_split_max_total_bytes`` before any of them
reported; stopping after the first finished could not unscan the rest. And
the timed-out first attempt of a plain query that was retried, then refused
for size, was missing from the split's bytes.

Now, before a piece starts:

* every running piece's calibrated estimate is reserved against the cap, and
  a piece that does not fit beside them waits (refused only when nothing is
  running);
* no more pieces run at once than the remaining budget could pay for if each
  read the full per-query limit (at least one), so concurrency falls as the
  budget tightens;
* the bytes of every timed-out attempt (a piece's or the plain query's) are in
  the split's total and its lineage.

The cap stays predictive: a running scan cannot be stopped at the exact byte.
Fully offline (``FakeLake``); synthetic names only.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
from agents import athena_exact_split as sp

from . import test_athena_exact_split as base
from ._athena_split_fakes import FakeLake, RunsUntilStopped

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_FAST = {"poll_timeout_seconds": 0.5}
#: Twenty days: over the 3 MB per-query limit, planned as two pieces.
_WINDOW = ("SELECT COUNT(*) FROM events WHERE created >= TIMESTAMP '2026-09-05 00:00:00' "
           "AND created < TIMESTAMP '2026-09-25 00:00:00'")


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)


def _lake(**kwargs) -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=base._CUTOFF, **kwargs)


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _engine_bytes(lake: FakeLake) -> int:
    """Bytes of every finished query the split counts (the connector's own
    schema read is not part of it)."""
    return sum(b for qid, b in lake.scanned.items() if "information_schema" not in lake._queries[qid])


def test_a_piece_that_does_not_fit_beside_a_running_one_is_not_started():
    # Real bytes are twice the estimate. Each piece fits the cap alone; the
    # two together do not, so the second waits for the first, whose bytes
    # then show the queue is over the cap.
    lake = _lake(estimate_factor=0.5)
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, _WINDOW, {"exact_split_max_total_bytes": 5_000_000})
    assert info.value.exact_split["reason"] == "byte_cap"
    assert len(_pieces(lake)) == 1


def test_concurrency_falls_as_the_remaining_budget_tightens():
    # The remaining budget (5.9 MB) could pay for one piece reading the full
    # 3 MB limit, not two: one piece runs at a time. A piece keeps running
    # until it is stopped, so it cannot end on its own poll window at the
    # same deadline: only the split's 2 s deadline ends the run.
    lake = _lake(estimate_factor=0.5)
    athena = RunsUntilStopped(lake)
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(athena, _WINDOW, {"exact_split_max_total_bytes": 8_900_000, "exact_split_max_seconds": 2})
    assert info.value.exact_split["reason"] == "wall_time"
    assert len(_pieces(lake)) == 1


def test_with_room_for_both_the_pieces_run_together_and_the_total_is_exact():
    lake = _lake(estimate_factor=0.5)
    result = _exact(lake, _WINDOW, {"exact_split_max_total_bytes": 12_000_000})
    assert result["rows"] == lake.reference(_WINDOW)
    assert result["exact_split"]["total_scanned_bytes"] <= 12_000_000


def _run(spent: int, *, total: int = 10_000_000, cap: int = 2_000_000) -> sp._Run:
    run = sp._Run({}, None, sp.Caps(16, total, 150.0), 0.0)
    run.spent, run.cap, run.calibration = spent, cap, 1.0
    return run


def test_admission_reserves_running_pieces():
    running = {object(): sp._Piece(((None, None),), 1_500_000)}
    run = _run(5_000_000)
    assert sp._may_start(run, running, sp._Piece(((None, None),), 1_000_000)) is True
    assert sp._may_start(run, running, sp._Piece(((None, None),), 4_000_000)) is False
    with pytest.raises(sp.ExactSplitRefused) as info:
        sp._may_start(run, {}, sp._Piece(((None, None),), 6_000_000))
    assert info.value.reason == "byte_cap"


def test_admission_lowers_concurrency_to_what_the_limit_could_cost():
    one = sp._Piece(((None, None),), 100_000)
    run = _run(5_000_000)                    # 5 MB left: two full-limit pieces
    running = {object(): one}
    assert sp._may_start(run, running, one) is True
    running[object()] = one
    assert sp._may_start(run, running, one) is False
    assert sp._may_start(_run(9_500_000), {}, one) is True      # always at least one


def test_the_room_check_counts_running_pieces():
    run = _run(5_000_000)
    queued = [sp._Piece(((None, None),), 2_000_000)]
    sp._check_room(run, queued, {})
    with pytest.raises(sp.ExactSplitRefused) as info:
        sp._check_room(run, queued, {object(): sp._Piece(((None, None),), 3_500_000)})
    assert info.value.reason == "byte_cap"


def test_a_timed_out_first_attempt_of_the_plain_query_is_in_the_splits_bytes():
    lake = _lake()
    lake.stall_first_plain, lake.stall_bytes = 1, 400_000
    result = _exact(lake, base._P1, _FAST)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(base._P1)
    assert split["timed_out_attempt_bytes"] == 400_000
    assert split["total_scanned_bytes"] == _engine_bytes(lake) + 400_000


def test_a_timed_out_first_attempt_of_a_piece_is_in_the_splits_bytes():
    lake = _lake()
    lake.stall_first_pieces, lake.stall_bytes = 1, 250_000
    result = _exact(lake, base._P1, _FAST)
    split = result["exact_split"]
    assert split["retried_pieces"] == 1 and split["timed_out_attempt_bytes"] == 250_000
    assert split["total_scanned_bytes"] == _engine_bytes(lake) + 250_000
