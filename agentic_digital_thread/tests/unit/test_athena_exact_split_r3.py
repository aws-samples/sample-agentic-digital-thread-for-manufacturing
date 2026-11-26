# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 3: the round 2 review's 4 narrowing findings on the exact-split extension.

Each test encodes the reviewer's concrete input from ``s40/review-split-r2.md``.
This round NARROWS what is supported and refuses the rest. Driven against
``FakeLake`` (SQLite behind boto3-shaped clients) or the pure modules directly.
Offline, synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
import agents.athena_exact_percentile as pct
import agents.athena_exact_shape as shape_mod

from ._athena_split_fakes import FakeLake

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 64}
_CUTOFF = 900_000
_START = datetime(2026, 9, 1)


@pytest.fixture(autouse=True)
def _fresh_caches():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


def _exact(lake, sql, cfg=None):
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _win(col, lo="2026-09-04", hi="2026-09-12"):
    return f"{col} >= TIMESTAMP '{lo} 00:00:00' AND {col} < TIMESTAMP '{hi} 00:00:00'"


# --------------------------------------------------------------------------- #
# Finding 1: intermediate branch merges must keep ALL groups; MAX_ROWS,
# ORDER BY and LIMIT apply once, after the combine. Two disjoint string-pinned
# branches with the SAME 600 site groups, GROUP BY site ORDER BY site DESC
# LIMIT 1: the whole query returns g599,2; the branch path must too (or refuse),
# never g596 (a per-branch 500-row cut).
# --------------------------------------------------------------------------- #
_F1_COLS = {"state": "varchar", "site": "varchar", "done_at": "timestamp(6)",
            "failed_at": "timestamp(6)", "created": "timestamp(6)"}
_F1_CUTOFF = 40_000_000


def _f1_rows():
    rows = []
    # 600 sites g000..g599, each appearing on three created-day partitions
    # (5, 6, 7) in BOTH state A (done_at) and state B (failed_at), inside the
    # window, so a branch splits on created and merges to 600 groups. Per site
    # the combined count across both branches is 6.
    for n in range(1800):
        site = f"g{n % 600:03d}"
        day = n % 3 + 5
        stamp = (_START + timedelta(days=day, minutes=n % 120)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(("A", site, stamp, None, stamp))
        rows.append(("B", site, None, stamp, stamp))
    return rows


_F1_SQL = ("SELECT site, COUNT(*) AS n FROM events WHERE "
           "((state = 'A' AND " + _win("done_at") + ") "
           "OR (state = 'B' AND " + _win("failed_at") + ")) "
           "GROUP BY site ORDER BY site DESC LIMIT 1")


def test_f1_each_branch_is_merged_without_the_model_facing_max_rows_cut(monkeypatch):
    """Each branch's intermediate merge must keep ALL its groups: before this
    fix the branch merge was capped at the model-facing MAX_ROWS (500) before the
    combine, so a 600-group branch lost 100 groups and a top-k across branches
    could land on g596 instead of g599. The fix passes a large intermediate
    bound (the grouped-row byte budget) to each branch; the real MAX_ROWS and
    ORDER BY/LIMIT apply once after the combine. Capture the bound per branch."""
    import agents.athena_exact_split as sp
    from agents.sql_safety import MAX_ROWS
    seen: list = []
    real = sp._compute_against

    def spy(run, shape, *, refusal, glue_client, snapshot, max_rows=None, **more):
        seen.append(max_rows)
        return real(run, shape, refusal=refusal, glue_client=glue_client, snapshot=snapshot,
                    max_rows=max_rows, **more)
    monkeypatch.setattr(sp, "_compute_against", spy)
    lake = FakeLake(columns=_F1_COLS, rows=_f1_rows(), part_col="created", cutoff=_F1_CUTOFF)
    try:
        _exact(lake, _F1_SQL, {"exact_split_max_total_bytes": 4_000_000_000})
    except at.AthenaEngineScanLimitError:
        pass  # a cap refusal is fine; the point is the per-branch bound below
    assert seen, "the OR path did not run any branch"
    # Every branch was merged with a bound strictly ABOVE the model-facing cap,
    # so no branch is pre-cut to 500 groups before the cross-branch combine.
    assert all(m is not None and m > MAX_ROWS for m in seen), seen


def test_f1_the_combine_applies_order_and_limit_over_all_branch_groups():
    """The combine over two branches that each kept all 600 groups returns the
    correct ORDER BY site DESC LIMIT 1 top row (g599), never a pre-cut g596."""
    import agents.athena_exact_or as or_mod
    import agents.athena_exact_shape as shape_mod
    from agents.athena_exact_merge import Merged
    from agents.sql_safety import MAX_ROWS
    shape = shape_mod.parse_shape(
        "SELECT site, COUNT(*) AS n FROM events WHERE state = 'A' "
        "GROUP BY site ORDER BY site DESC LIMIT 1")
    rows_a = [[f"g{n:03d}", "1"] for n in range(600)]
    rows_b = [[f"g{n:03d}", "1"] for n in range(600)]
    branch_a = Merged(["site", "n"], ["varchar", "bigint"], rows_a, True, False, 600)
    branch_b = Merged(["site", "n"], ["varchar", "bigint"], rows_b, True, False, 600)
    merged = or_mod.combine([branch_a, branch_b], shape, max_rows=MAX_ROWS)
    assert merged.rows == [["g599", "2"]], merged.rows


# --------------------------------------------------------------------------- #
# Finding 2: the percentile value expression is supported ONLY as a bare
# bigint/double/real column or a plain date_diff('unit', col, col). Arithmetic
# on either (date_diff(...) + weight) refuses.
# --------------------------------------------------------------------------- #
def test_f2_date_diff_plus_column_refuses():
    types = {"started": "timestamp(6)", "completed": "timestamp(6)", "weight": "double"}
    with pytest.raises(shape_mod.ShapeRefusal):
        shape_mod.resolve_value_type("date_diff('second', started, completed) + weight", types)


def test_f2_a_bare_supported_column_and_plain_date_diff_are_proved():
    types = {"big": "bigint", "ratio": "double", "r": "real", "started": "timestamp(6)",
             "completed": "timestamp(6)", "name": "varchar"}
    assert shape_mod.resolve_value_type("big", types) == "bigint"
    assert shape_mod.resolve_value_type("ratio", types) == "double"
    assert shape_mod.resolve_value_type("r", types) == "real"
    assert shape_mod.resolve_value_type("date_diff('second', started, completed)", types) == "bigint"


def test_f2_other_value_expressions_refuse():
    types = {"big": "bigint", "started": "timestamp(6)", "completed": "timestamp(6)",
             "name": "varchar", "ratio": "double"}
    for arg in ("big / 2",                                   # arithmetic on a column
                "CAST(big AS double)",                       # a cast
                "GREATEST(0, date_diff('second', started, completed))",  # a function wrapper
                "date_diff('second', started, completed) / 3600.0",      # arithmetic on date_diff
                "name",                                      # unsupported column type
                "abs(big)",                                  # another function
                "date_diff(started, completed)"):            # not the 3-arg unit form
        with pytest.raises(shape_mod.ShapeRefusal):
            shape_mod.resolve_value_type(arg, types)


# --------------------------------------------------------------------------- #
# Finding 3: ordering in the percentile path uses the ordinary merge's
# implementation, so its NaN refusal applies. A DOUBLE group key holding NaN
# under ORDER BY k refuses.
# --------------------------------------------------------------------------- #
def test_f3_order_by_a_nan_double_key_refuses():
    shape = shape_mod.parse_shape(
        "SELECT k, approx_percentile(v, 0.9) AS p FROM events WHERE v > 0 GROUP BY k ORDER BY k")
    rows = [["NaN", "1.0"], ["1.0", "2.0"], ["2.0", "3.0"]]
    with pytest.raises(pct.PercentileMergeError):
        pct._ordered(shape, rows, ["double", "varchar"])


def test_f3_order_by_a_plain_double_key_still_sorts():
    shape = shape_mod.parse_shape(
        "SELECT k, approx_percentile(v, 0.9) AS p FROM events WHERE v > 0 GROUP BY k ORDER BY k")
    rows = [["2.0", "a"], ["10.0", "b"]]
    out = pct._ordered(shape, rows, ["double", "varchar"])
    assert [r[0] for r in out] == ["2.0", "10.0"]


# --------------------------------------------------------------------------- #
# Finding 4: a NULL quantile for a gid that HAD digest input refuses; only a
# gid with no digest input may be NULL.
# --------------------------------------------------------------------------- #
def _shape():
    return shape_mod.parse_shape(
        "SELECT k, approx_percentile(v, 0.9) AS p FROM events WHERE v > 0 GROUP BY k")


def test_f4_a_null_quantile_for_a_group_with_digest_input_refuses():
    shape = _shape()
    with pytest.raises(pct.PercentileMergeError):
        pct.quantiles_by_group(lambda sql: [["0", None]], shape, [0], {0: ["AA"]})


def test_f4_a_group_with_no_digest_input_is_null_without_a_query():
    shape = _shape()
    out = pct.quantiles_by_group(
        lambda sql: (_ for _ in ()).throw(AssertionError("no query")), shape, [0], {0: []})
    assert out == {0: None}


# --------------------------------------------------------------------------- #
# Round 4: a dotted percentile value reference does not prove its type.
# --------------------------------------------------------------------------- #

def test_r4_a_dotted_percentile_value_refuses():
    """``u.x`` may be an UNNEST alias (DOUBLE) shadowing a base column ``x``
    (BIGINT); the Glue type of ``x`` proves nothing about it."""
    types = {"x": "bigint", "vals": "varchar", "started": "timestamp(6)",
             "completed": "timestamp(6)"}
    for arg in ("u.x", "events.x", "date_diff('second', t.started, t.completed)"):
        with pytest.raises(shape_mod.ShapeRefusal):
            shape_mod.resolve_value_type(arg, types)


def test_r4_a_percentile_over_an_unnest_expansion_refuses():
    sql = ("SELECT approx_percentile(x, 0.5) FROM events "
           "CROSS JOIN UNNEST(CAST(json_parse(vals) AS ARRAY(DOUBLE))) AS u(x)")
    try:
        shape = shape_mod.parse_shape(sql)
    except shape_mod.ShapeRefusal:
        return  # refused at parse: also correct
    with pytest.raises(shape_mod.ShapeRefusal):
        shape_mod.with_percentile_types(shape, {"x": "bigint", "vals": "varchar"})


def test_r4_a_quoted_dotted_column_name_is_one_token_and_stays_supported():
    """A lake column whose NAME contains dots is one quoted identifier."""
    types = {"a.b.duration": "double"}
    assert shape_mod.resolve_value_type('"a.b.duration"', types) == "double"
