# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two ways an exact split gave up before it had to.

A live check on a real table found:

1. a piece's poll saw QUEUED for its whole 60 s window although the engine
   ran the query in 4.5 s (a stalled poll; the same happened once to a plain
   query). The split gave no total (``piece_timed_out``). Now a piece that
   times out is run once more as a fresh query when the wall budget leaves
   room; only a second timeout of the same piece gives no total;
2. a GROUP BY over a window whose plain COUNT finished in 5 pieces was
   refused at PLANNING time with ``piece_cap``: its estimate (more columns
   read) was high and nothing had run to say so. Now a first plan over the
   piece cap is probed: its biggest piece runs, the calibration is taken
   from it, the queue is re-planned (split and merged), and the split stops
   with ``piece_cap`` only when the calibrated queue still does not fit.

Also: a first plan over the byte cap gets the same probe before it can be
refused.

Fully offline (the file-level ``FakeLake``); synthetic names only.
"""
from __future__ import annotations

from collections import Counter

import pytest

import agents.athena_tools as at
from agents import athena_exact_split as sp

from . import test_athena_exact_split as base
from ._athena_split_fakes import FakeLake

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
#: Polls give up after half a second in these tests.
_FAST = {"poll_timeout_seconds": 0.5}
_GROUPED = ("SELECT site, COUNT(*) AS n, MIN(completed) AS first_done, MAX(completed) AS last_done "
            "FROM events WHERE state = 'DONE' AND completed >= TIMESTAMP '2026-09-02 00:00:00' "
            "AND completed < TIMESTAMP '2026-09-29 00:00:00' GROUP BY site ORDER BY n DESC LIMIT 200")


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _refusal(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return info.value.exact_split


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _lake(**kwargs) -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created", **kwargs)


# --------------------------------------------------------------------------- #
# 1. A stalled poll
# --------------------------------------------------------------------------- #
def test_a_piece_whose_poll_stalls_is_retried_once_and_the_total_is_exact():
    lake = _lake(cutoff=base._CUTOFF)
    lake.stall_first_pieces = 1                       # the probe's poll never moves
    result = _exact(lake, base._P1, _FAST)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(base._P1)
    assert split["retried_pieces"] == 1
    starts = Counter(_pieces(lake))
    assert max(starts.values()) == 2                  # the probe box, run twice
    stalled = [qid for qid, sql in lake._queries.items() if qid in lake._stalled]
    assert stalled and set(stalled) <= set(lake.stopped)


def test_a_second_timeout_of_the_same_piece_gives_no_total():
    lake = _lake(cutoff=base._CUTOFF)
    lake.slow_piece_containing = "FOR VERSION AS OF"  # every piece stays running
    split = _refusal(lake, base._P1, _FAST)
    assert split["reason"] == "piece_timed_out"
    starts = Counter(_pieces(lake))
    assert max(starts.values()) == 2                  # one retry, never a third run


def test_no_retry_when_the_wall_budget_has_no_room(monkeypatch):
    monkeypatch.setattr(sp, "RETRY_MIN_SECONDS", 3600.0)
    lake = _lake(cutoff=base._CUTOFF)
    lake.stall_first_pieces = 1
    split = _refusal(lake, base._P1, _FAST)
    assert split["reason"] == "piece_timed_out"
    assert max(Counter(_pieces(lake)).values()) == 1


# --------------------------------------------------------------------------- #
# 2. A first plan over the piece cap
# --------------------------------------------------------------------------- #
def test_a_grouped_query_whose_estimate_is_high_is_probed_not_refused_at_planning():
    # The estimate is about twice what the engine reads: at its word the
    # plan needs more than 16 pieces; measured, it needs a handful.
    lake = _lake(cutoff=4_000_000, estimate_factor=2.0)
    assert lake.scan_bytes(_GROUPED) > lake.cutoff
    result = _exact(lake, _GROUPED)
    split = result["exact_split"]
    assert result["rows"] == lake.reference(_GROUPED)
    assert split["first_plan_over_cap"] is True
    assert split["calibration"] < 1.0
    assert len(_pieces(lake)) == split["pieces"] + split["refused_pieces"] <= 16


def test_an_over_cap_plan_the_probe_confirms_stops_after_the_probe():
    # The estimate is right: the probe measures it, the queue still needs
    # more pieces than are left, and nothing more is read.
    lake = _lake(cutoff=3_000_000, estimate_factor=1.0)
    split = _refusal(lake, _GROUPED, {"exact_split_max_pieces": 6})
    assert split["reason"] == "piece_cap"
    assert len(_pieces(lake)) == 1


def test_a_plan_beyond_what_a_probe_could_rescue_is_refused_at_planning(monkeypatch):
    monkeypatch.setattr(sp, "PROBE_PLAN_FACTOR", 1)
    lake = _lake(cutoff=4_000_000, estimate_factor=2.0)
    split = _refusal(lake, _GROUPED)
    assert split["reason"] == "piece_cap"
    assert _pieces(lake) == []


# --------------------------------------------------------------------------- #
# 3. A first plan over the byte cap
# --------------------------------------------------------------------------- #
def test_a_first_plan_over_the_byte_cap_is_probed_not_refused_at_planning():
    # The estimate is twice what the engine reads: at its word the plan is
    # over the total byte cap; measured, it fits.
    lake = _lake(cutoff=base._CUTOFF, estimate_factor=2.0)
    result = _exact(lake, base._P1, {"exact_split_max_total_bytes": 15_000_000})
    split = result["exact_split"]
    assert result["rows"] == lake.reference(base._P1)
    assert split["first_plan_over_cap"] is True
    assert split["total_scanned_bytes"] <= 15_000_000


def test_an_over_byte_cap_plan_the_probe_confirms_stops_after_the_probe():
    lake = _lake(cutoff=base._CUTOFF, estimate_factor=1.0)
    split = _refusal(lake, base._P1, {"exact_split_max_total_bytes": 7_000_000})
    assert split["reason"] == "byte_cap"
    assert len(_pieces(lake)) == 1
