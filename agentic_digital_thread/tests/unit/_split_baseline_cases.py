# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exact-split cases run the same way on two commits.

``fixtures/exact_split_baseline.json`` holds what each case returned and which
queries it started on dedb4d03, the commit before the split kept time back
for its caller; ``test_athena_exact_split_time_left`` runs the same cases on
the current code and compares. This module uses only names both commits have,
so the same code ran on both sides (regenerate the fixture by copying this
module into a checkout of that commit and running :func:`capture` there).

The card case is a dashboard refresh (``metric_athena_scope.read_scope``, its
4 pieces and 30 s) of a grouped COUNT whose file statistics overestimate it
three times: the first plan is over the card's piece cap and its probe shows
three pieces fit. On the split's clock each metadata read takes
:data:`META_SECONDS` and each piece :data:`PIECE_SECONDS`, as measured on a
rehearsal account (2026-10-23: snapshot, coverage and planning reads 2.8-3.5 s,
pieces 2.3-6.6 s), so the probe runs with about 19.5 s of the 30 s left.

Offline (``FakeLake``); synthetic names only.
"""
from __future__ import annotations

import json
import time
import types
from contextlib import contextmanager, nullcontext
from pathlib import Path

import agents.athena_tools as at
from agents import athena_exact_split as sp
from agents import metric_athena_scope as mas

from . import test_athena_exact_split as base
from . import test_athena_exact_split_or_states as ors
from . import test_athena_exact_split_retry as retry
from ._athena_split_fakes import FakeLake

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "exact_split_baseline.json"
META_SECONDS = 3.5
PIECE_SECONDS = 3.0
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
CARD_SQL = ("SELECT site, COUNT(*) AS n FROM events WHERE state = 'DONE' "
            "AND created >= TIMESTAMP '2026-09-05 00:00:00' "
            "AND created < TIMESTAMP '2026-09-25 00:00:00' GROUP BY site ORDER BY n DESC")


def _lake(**kwargs) -> FakeLake:
    return FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created", **kwargs)


#: name -> (lake factory, SQL, connection config, a dashboard card refresh)
CASES = {
    "count": (lambda: _lake(cutoff=base._CUTOFF), base._P1, _CFG, False),
    "over-cap probe": (lambda: _lake(cutoff=4_000_000, estimate_factor=2.0), retry._GROUPED,
                       _CFG, False),
    "or of states": (ors._lake, ors._or_of_states(), ors._CFG, False),
    "card, over-cap probe": (lambda: _lake(cutoff=6_000_000, estimate_factor=3.0), CARD_SQL,
                             _CFG, True),
}


def _clear() -> None:
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    sp.clear_layout_cache()


@contextmanager
def split_clock(lake: FakeLake):
    """The split's clock, moved on by :data:`META_SECONDS` when a metadata
    read starts and :data:`PIECE_SECONDS` when a piece starts; yields a dict
    whose ``offset`` is the time added so far."""
    real = time.monotonic
    clock = {"offset": 0.0}
    start = lake.start_query_execution

    def starting(QueryString, **kwargs):  # noqa: N803 - boto3 shape
        if "$snapshots" in QueryString or "$files" in QueryString:
            clock["offset"] += META_SECONDS
        elif "FOR VERSION AS OF" in QueryString:
            clock["offset"] += PIECE_SECONDS
        return start(QueryString=QueryString, **kwargs)

    lake.start_query_execution = starting
    saved = sp.time
    sp.time = types.SimpleNamespace(monotonic=lambda: real() + clock["offset"])
    try:
        yield clock
    finally:
        sp.time = saved


def _normal(result: dict) -> dict:
    """The result as JSON, with the query ids (their numbering follows the
    order threads start pieces in) replaced by how many there are."""
    out = json.loads(json.dumps(result))
    split = out.get("exact_split")
    if isinstance(split, dict) and isinstance(split.get("query_ids"), list):
        split["query_ids"] = len(split["query_ids"])
    return out


def run_case(name: str, limit=None) -> dict:
    """What case ``name`` returns (or why it was refused) and the sorted
    queries it started, under ``limit`` (a card case uses its own scope)."""
    make_lake, sql, cfg, card = CASES[name]
    saved_interval = at._DEFAULT_POLL_INTERVAL
    at._DEFAULT_POLL_INTERVAL = 0.01
    _clear()
    lake = make_lake()
    clock = None
    try:
        scope = mas.read_scope("sys-lake", "lineage") if card else sp.limited(limit)
        timing = split_clock(lake) if card else nullcontext()
        with scope, timing as clock:
            try:
                out = {"result": _normal(at.run_athena_query_exact(
                    sql, dict(cfg), client=lake, glue_client=lake.glue))}
            except at.AthenaEngineScanLimitError as exc:
                out = {"refused": exc.exact_split}
    finally:
        at._DEFAULT_POLL_INTERVAL = saved_interval
        _clear()
    out["queries"] = sorted(lake.started)
    if clock is not None:
        out["split_clock_seconds"] = clock["offset"]
    return out


def capture() -> dict:
    """Every case with no caller limit, for the fixture."""
    return {name: run_case(name) for name in sorted(CASES)}


if __name__ == "__main__":  # pragma: no cover - run on the baseline commit
    print(json.dumps(capture(), indent=1, sort_keys=True))
