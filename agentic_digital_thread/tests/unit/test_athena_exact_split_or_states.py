# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A COUNT across several states, each on its own time column, written as one OR.

A measured over-cap query asks for a count of rows in several lifecycle states,
each windowed on that state's own time column, written as one OR of per-state
branches:

    ... WHERE <common> AND ((state = 'A' AND a_time IN W)
                            OR (state = 'B' AND b_time IN W)
                            OR (state = 'C' AND c_time IN W)) GROUP BY state

The single-query exact split refuses this (``unsplittable``): the time bounds
sit under the OR, not a top-level AND, so the box planner can clip no box to
the window and every partition box must read each state column's whole range,
over the per-query cap. Each BRANCH alone, though, is an ordinary single-window
COUNT the splitter can plan.

So the connector now runs each branch as its own query (``<common> AND Bi``),
splits each the existing exact way, and combines. The branches pin disjoint
values of one shared column, so the combined groups are exact. The answer says
how many queries and pieces it was computed from. Anything else under an OR
(a branch pinning a different column, an overlapping pin, a non-literal bound,
NOT, a nested OR) stays unsplit, exactly as today.

Driven against ``FakeLake`` (SQLite behind boto3-shaped clients). Offline,
synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at

from ._athena_split_fakes import FakeLake

# A table with a state column and one own time column per state value.
_COLUMNS = {"site": "varchar", "state": "varchar", "done_at": "timestamp(6)",
            "failed_at": "timestamp(6)", "begun_at": "timestamp(6)",
            "created": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
#: Any whole-table column read is over this cutoff; any one day fits. The
#: window below spans several days, so a query reading a whole state column
#: over that window is over the cutoff too (forcing the split). Sized so the
#: whole OR (three branches sharing ONE piece budget after Round 2) fits well
#: under exact_split_max_pieces.
_CUTOFF = 1_500_000
_START = datetime(2026, 9, 1)
_STATES = ("DONE", "FAILED", "BEGUN")
_TIME_COL = {"DONE": "done_at", "FAILED": "failed_at", "BEGUN": "begun_at"}


def _rows() -> list[tuple]:
    """20 days; each row is in exactly one state and carries only that state's
    own time column (the others NULL), equal to its ``created`` instant."""
    rows = []
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            state = _STATES[(day + i) % 3]
            stamp = created.strftime("%Y-%m-%d %H:%M:%S")
            done = stamp if state == "DONE" else None
            failed = stamp if state == "FAILED" else None
            begun = stamp if state == "BEGUN" else None
            rows.append(("SITE-A" if i % 2 else "SITE-B", state, done, failed, begun, stamp))
    return rows


@pytest.fixture(autouse=True)
def _fresh_caches():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


def _lake() -> FakeLake:
    return FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF)


@pytest.fixture
def lake():
    return _lake()


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _refused(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return at._tool_error(info.value)["exact_split"]


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _same_rows(got, want) -> bool:
    """Order-insensitive row comparison (the OR queries have no ORDER BY, so
    the engine's group order is unspecified)."""
    import json
    key = lambda r: json.dumps(r)  # noqa: E731
    return sorted(got, key=key) == sorted(want, key=key)


def _not_branched(lake: FakeLake, sql: str) -> None:
    """Assert the OR combine did NOT claim this query: it is refused, runs as a
    plain query, or is handled by the ordinary single-query split -- never with
    the ``branches`` accounting. Anything not of the recognised shape lands
    here."""
    try:
        split = _exact(lake, sql).get("exact_split", {})
        assert "branches" not in split, split
    except at.AthenaEngineScanLimitError as info:
        split = at._tool_error(info).get("exact_split", {})
        assert "branches" not in split, split


def _window(lo: str, hi: str, time_col: str) -> str:
    return (f"{time_col} >= TIMESTAMP '{lo} 00:00:00' AND {time_col} < TIMESTAMP '{hi} 00:00:00'")


def _or_of_states(states=_STATES, lo="2026-09-04", hi="2026-09-12", common="site = 'SITE-A'",
                  grouped=True) -> str:
    branches = " OR ".join(
        f"(state = '{s}' AND {_window(lo, hi, _TIME_COL[s])})" for s in states)
    select = "SELECT state, COUNT(*)" if grouped else "SELECT COUNT(*)"
    tail = " GROUP BY state" if grouped else ""
    return f"{select} FROM events WHERE {common} AND ({branches}){tail}"


# --------------------------------------------------------------------------- #
# The OR of per-state branches is computed exactly
# --------------------------------------------------------------------------- #
def test_the_or_shape_routes_to_the_branch_path():
    """Red before the branch path existed: there is no branch path, so the result (whether the
    single split happens to manage it or refuses) never carries the
    ``branches`` accounting the OR combine adds. With the branch path, the OR
    shape is always computed per branch and says so."""
    lake = _lake()
    sql = _or_of_states()
    try:
        split = _exact(lake, sql)["exact_split"]
    except at.AthenaEngineScanLimitError as info:
        split = at._tool_error(info)["exact_split"]
    assert split.get("branches") == 3, split


def test_or_of_states_grouped_count_is_combined_exactly(lake):
    sql = _or_of_states()
    result = _exact(lake, sql)
    assert _same_rows(result["rows"], lake.reference(sql))
    split = result["exact_split"]
    assert split["applied"] is True
    # One query per branch, each split into its own pieces.
    assert split["branches"] == 3
    assert split["branch_queries"] == 3
    assert split["pieces"] >= split["branches"]
    assert "3 branch queries" in result["note"]


def test_combined_groups_equal_the_per_branch_results(lake):
    """Each branch run alone gives that state's group; the combined result is
    those groups together."""
    sql = _or_of_states()
    combined = {r[0]: r[1] for r in _exact(lake, sql)["rows"]}
    for state in _STATES:
        one = _or_of_states(states=(state,))
        branch_rows = _lake().reference(one)
        assert len(branch_rows) == 1 and branch_rows[0][0] == state
        assert combined[state] == branch_rows[0][1], state


def test_or_of_states_ungrouped_count_sums_disjoint_branches():
    lake = _lake()
    sql = _or_of_states(grouped=False)
    result = _exact(lake, sql)
    assert _same_rows(result["rows"], lake.reference(sql))
    assert result["exact_split"]["branches"] == 3


def test_each_branch_query_is_the_common_where_and_one_branch(lake):
    _exact(lake, _or_of_states())
    pieces = _pieces(lake)
    assert pieces
    # The query's state OR is gone: each piece names exactly one state value
    # (a box predicate may itself use OR for the partition NULL end, which is
    # not the query's OR of branches).
    for p in pieces:
        named = [s for s in _STATES if f"'{s}'" in p]
        assert len(named) == 1, p


def test_a_subset_of_states_is_computed_exactly(lake):
    sql = _or_of_states(states=("DONE", "FAILED"))
    result = _exact(lake, sql)
    assert _same_rows(result["rows"], lake.reference(sql))
    assert result["exact_split"]["branches"] == 2


def test_the_result_is_one_logical_result_not_added_by_the_model(lake):
    result = _exact(lake, _or_of_states())
    assert "do not add" in result["note"].lower()
    # A single result record, not a list the model must sum.
    assert isinstance(result["rows"], list)


# --------------------------------------------------------------------------- #
# Anything else under an OR stays unsplit (handled by the single split, never
# the branch combine)
# --------------------------------------------------------------------------- #
def test_a_branch_pinning_a_different_column_is_not_combined(lake):
    sql = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
           "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-12", "done_at") + ") "
           "OR (site = 'SITE-B' AND " + _window("2026-09-04", "2026-09-12", "failed_at") + ")) "
           "GROUP BY state")
    _not_branched(lake, sql)


def test_an_overlapping_pin_is_not_combined(lake):
    # Two branches pin the SAME value: not disjoint, so not an exact combine.
    sql = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
           "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-08", "done_at") + ") "
           "OR (state = 'DONE' AND " + _window("2026-09-08", "2026-09-12", "done_at") + ")) "
           "GROUP BY state")
    _not_branched(lake, sql)


def test_a_branch_with_a_non_literal_bound_is_not_combined(lake):
    sql = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
           "((state = 'DONE' AND done_at >= TIMESTAMP '2026-09-04 00:00:00' "
           "AND done_at < current_timestamp) "
           "OR (state = 'FAILED' AND " + _window("2026-09-04", "2026-09-12", "failed_at") + ")) "
           "GROUP BY state")
    _not_branched(lake, sql)


def test_a_negated_branch_is_not_combined(lake):
    sql = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
           "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-12", "done_at") + ") "
           "OR (NOT state = 'FAILED' AND " + _window("2026-09-04", "2026-09-12", "failed_at") + ")) "
           "GROUP BY state")
    _not_branched(lake, sql)


def test_a_nested_or_branch_is_not_combined(lake):
    sql = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
           "((state = 'DONE' AND (done_at >= TIMESTAMP '2026-09-04 00:00:00' "
           "OR done_at < TIMESTAMP '2026-09-01 00:00:00')) "
           "OR (state = 'FAILED' AND " + _window("2026-09-04", "2026-09-12", "failed_at") + ")) "
           "GROUP BY state")
    _not_branched(lake, sql)


def test_the_or_parser_rejects_the_non_combinable_shapes():
    """The parser itself declines each non-recognised OR shape (so it never
    reaches the branch combine)."""
    import agents.athena_exact_or as or_mod
    diff_col = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
                "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-12", "done_at") + ") "
                "OR (site = 'SITE-B' AND " + _window("2026-09-04", "2026-09-12", "failed_at") + ")) "
                "GROUP BY state")
    overlap = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
               "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-08", "done_at") + ") "
               "OR (state = 'DONE' AND " + _window("2026-09-08", "2026-09-12", "done_at") + ")) "
               "GROUP BY state")
    non_literal = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
                   "((state = 'DONE' AND done_at >= TIMESTAMP '2026-09-04 00:00:00' "
                   "AND done_at < current_timestamp) "
                   "OR (state = 'FAILED' AND " + _window("2026-09-04", "2026-09-12", "failed_at")
                   + ")) GROUP BY state")
    negated = ("SELECT state, COUNT(*) FROM events WHERE site = 'SITE-A' AND "
               "((state = 'DONE' AND " + _window("2026-09-04", "2026-09-12", "done_at") + ") "
               "OR (NOT state = 'FAILED' AND " + _window("2026-09-04", "2026-09-12", "failed_at")
               + ")) GROUP BY state")
    varchar = lambda _col: "varchar"  # noqa: E731 - every column reads as a string here
    for sql in (diff_col, overlap, non_literal, negated):
        assert or_mod.parse_or_branches(sql, column_type=varchar) is None, sql
    # The recognised shape parses to three branches on the shared column.
    good = or_mod.parse_or_branches(_or_of_states(), column_type=varchar)
    assert good is not None and good.pinned_column == "state" and len(good.branch_sqls) == 3


def test_a_branch_whose_count_cannot_split_fails_the_whole_combine():
    """If one branch cannot be computed exactly, there is NO total (every
    existing guarantee stays: no partial sum). Only the FAILED branch's pieces
    fail -- the whole multi-branch query still refuses for scan first."""
    lake = _lake()
    lake.fail_piece_all_of = ("FOR VERSION AS OF", "'FAILED'")
    sql = _or_of_states()
    split = _refused(lake, sql)
    assert split["applied"] is False
    assert split["reason"] == "piece_failed"


def test_one_snapshot_across_all_branches(lake):
    _exact(lake, _or_of_states())
    pieces = _pieces(lake)
    # Every branch's pieces pin the same single snapshot.
    snaps = {p.split("FOR VERSION AS OF ")[1].split()[0].rstrip(",") for p in pieces}
    assert snaps == {str(lake.snapshot)}
