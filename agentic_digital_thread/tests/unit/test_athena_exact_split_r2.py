# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Round 2: the independent review's 9 findings on the exact-split extension.

Each test encodes the reviewer's concrete input from ``s40/review-split-r1.md``.
Driven against ``FakeLake`` (SQLite behind boto3-shaped clients) or the pure
modules directly. Offline, synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
import agents.athena_exact_or as or_mod
import agents.athena_exact_percentile as pct
import agents.athena_exact_shape as shape_mod

from ._athena_split_fakes import FakeLake

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
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


def _refused(lake, sql, cfg=None):
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return at._tool_error(info.value)["exact_split"]


def _not_branched(lake, sql, cfg=None):
    try:
        split = _exact(lake, sql, cfg).get("exact_split", {})
        assert "branches" not in split, split
    except at.AthenaEngineScanLimitError as info:
        assert "branches" not in at._tool_error(info).get("exact_split", {})


def _win(col, lo="2026-09-04", hi="2026-09-12"):
    return f"{col} >= TIMESTAMP '{lo} 00:00:00' AND {col} < TIMESTAMP '{hi} 00:00:00'"


# --------------------------------------------------------------------------- #
# Finding 1: branch-level ORDER BY / LIMIT must not pre-truncate the combine.
# State A holds site X=6 and Z=5; state B holds site Y=6 and Z=5. Grouped by
# site, ORDER BY n DESC, site LIMIT 1: the whole query returns Z,10 (Z=5+5);
# the branch path must too, not X,6.
# --------------------------------------------------------------------------- #
_STRING_COLS = {"state": "varchar", "site": "varchar", "done_at": "timestamp(6)",
                "failed_at": "timestamp(6)", "created": "timestamp(6)"}


def _xyz_rows():
    rows, n = [], 0
    #: state A: X x6, Z x5 (windowed on done_at); state B: Y x6, Z x5 (failed_at)
    for site, count in (("X", 6), ("Z", 5)):
        for _ in range(count):
            created = _START + timedelta(days=5, minutes=n)
            rows.append(("A", site, created.strftime("%Y-%m-%d %H:%M:%S"), None,
                         created.strftime("%Y-%m-%d %H:%M:%S")))
            n += 1
    for site, count in (("Y", 6), ("Z", 5)):
        for _ in range(count):
            created = _START + timedelta(days=6, minutes=n)
            rows.append(("B", site, None, created.strftime("%Y-%m-%d %H:%M:%S"),
                         created.strftime("%Y-%m-%d %H:%M:%S")))
            n += 1
    # bulk padding OUTSIDE the window (window is 2026-09-04..2026-09-12, i.e.
    # days 3-10) so the whole query is over the cap but F is not counted.
    for day in range(15, 20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=i)
            rows.append(("A", "F", created.strftime("%Y-%m-%d %H:%M:%S"), None,
                         created.strftime("%Y-%m-%d %H:%M:%S")))
    return rows


_XYZ_SQL = ("SELECT site, COUNT(*) AS n FROM events WHERE "
            "((state = 'A' AND " + _win("done_at") + ") "
            "OR (state = 'B' AND " + _win("failed_at") + ")) "
            "GROUP BY site ORDER BY n DESC, site LIMIT 1")


def test_f1_branch_order_limit_applied_only_in_the_final_combine():
    lake = FakeLake(columns=_STRING_COLS, rows=_xyz_rows(), part_col="created", cutoff=_CUTOFF)
    result = _exact(lake, _XYZ_SQL)
    assert result["exact_split"]["branches"] == 2
    assert result["rows"] == [["Z", "10"]]


# --------------------------------------------------------------------------- #
# Finding 2: disjointness by the pinned column's type, not source text. An
# integer state column with branches pinned by state = 1 and state = 01 is NOT
# split (1 and 01 are the same integer; the branches overlap).
# --------------------------------------------------------------------------- #
_INT_COLS = {"state": "integer", "a_at": "timestamp(6)", "b_at": "timestamp(6)",
             "created": "timestamp(6)"}


def _int_rows():
    rows = []
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            rows.append((1, created.strftime("%Y-%m-%d %H:%M:%S"), None,
                         created.strftime("%Y-%m-%d %H:%M:%S")))
    return rows


def test_f2_integer_pins_1_and_01_are_not_split():
    lake = FakeLake(columns=_INT_COLS, rows=_int_rows(), part_col="created", cutoff=_CUTOFF)
    sql = ("SELECT state, COUNT(*) FROM events WHERE "
           "((state = 1 AND " + _win("a_at") + ") "
           "OR (state = 01 AND " + _win("b_at") + ")) GROUP BY state")
    _not_branched(lake, sql)


def test_f2_the_parser_rejects_a_numeric_pin_column():
    # A column-type lookup that reports integer -> the OR parser declines.
    sql = ("SELECT state, COUNT(*) FROM events WHERE "
           "((state = 1 AND " + _win("a_at") + ") "
           "OR (state = 2 AND " + _win("b_at") + ")) GROUP BY state")
    assert or_mod.parse_or_branches(sql, column_type=lambda c: "integer") is None
    # A string column with string-literal pins is accepted.
    sql_s = ("SELECT state, COUNT(*) FROM events WHERE "
             "((state = 'A' AND " + _win("a_at") + ") "
             "OR (state = 'B' AND " + _win("b_at") + ")) GROUP BY state")
    assert or_mod.parse_or_branches(sql_s, column_type=lambda c: "varchar") is not None
    # A numeric literal pin on an unknown-type column is rejected.
    assert or_mod.parse_or_branches(sql, column_type=lambda c: None) is None


# --------------------------------------------------------------------------- #
# Finding 3: OR plus percentile is refused by the OR parser (digests are not
# kept across branches yet).
# --------------------------------------------------------------------------- #
def test_f3_or_plus_percentile_is_not_split():
    sql = ("SELECT state, COUNT(*), approx_percentile(qty, 0.5) FROM events WHERE "
           "((state = 'A' AND " + _win("a_at") + ") "
           "OR (state = 'B' AND " + _win("b_at") + ")) GROUP BY state")
    assert or_mod.parse_or_branches(sql, column_type=lambda c: "varchar") is None


# --------------------------------------------------------------------------- #
# Finding 4: one budget across all branches. The OR fixture must refuse under a
# tight piece cap and a tight total-byte cap.
# --------------------------------------------------------------------------- #
_OR_COLS = {"state": "varchar", "done_at": "timestamp(6)", "failed_at": "timestamp(6)",
            "begun_at": "timestamp(6)", "created": "timestamp(6)"}
_OR_STATES = ("DONE", "FAILED", "BEGUN")
_OR_TIME = {"DONE": "done_at", "FAILED": "failed_at", "BEGUN": "begun_at"}


def _or_rows():
    rows = []
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            state = _OR_STATES[(day + i) % 3]
            stamp = created.strftime("%Y-%m-%d %H:%M:%S")
            rows.append((state,
                         stamp if state == "DONE" else None,
                         stamp if state == "FAILED" else None,
                         stamp if state == "BEGUN" else None, stamp))
    return rows


def _or_sql():
    branches = " OR ".join(f"(state = '{s}' AND " + _win(_OR_TIME[s]) + ")" for s in _OR_STATES)
    return f"SELECT state, COUNT(*) FROM events WHERE ({branches}) GROUP BY state"


def _or_lake():
    # At this cutoff each branch is 9 pieces / ~3.2M bytes (under the caps
    # below), but the three branches combine to 27 pieces / ~9.6M bytes; a
    # SHARED budget must refuse, a per-branch one wrongly succeeds.
    return FakeLake(columns=_OR_COLS, rows=_or_rows(), part_col="created", cutoff=500_000)


def test_f4_a_tight_piece_cap_refuses_across_branches():
    split = _refused(_or_lake(), _or_sql(), {"exact_split_max_pieces": 15})
    assert split["applied"] is False and split["reason"] == "piece_cap"


def test_f4_a_tight_total_byte_cap_refuses_across_branches():
    split = _refused(_or_lake(), _or_sql(), {"exact_split_max_total_bytes": 6_000_000})
    assert split["applied"] is False and split["reason"] == "byte_cap"


# --------------------------------------------------------------------------- #
# Finding 5: value type. qdigest is built over the value expression's own
# supported type (BIGINT/DOUBLE/REAL); two BIGINT values beyond 2^53 stay
# distinct; a date_diff result stays BIGINT; an unprovable type refuses.
# --------------------------------------------------------------------------- #
def test_f5_bigint_value_type_is_preserved():
    use = shape_mod.PercentileUse(0, 0, 0, 0, 0, 0, False)
    # A bigint value expression builds a bigint qdigest, not a double one.
    expr = shape_mod.digest_piece_expr_typed("col", "bigint")
    assert "double" not in expr.lower() and "bigint" in expr.lower()


def test_f5_an_unprovable_value_type_refuses():
    with pytest.raises(shape_mod.ShapeRefusal):
        shape_mod.resolve_value_type("some_expr", {})  # no type known


def test_f5_a_bigint_grouped_percentile_keeps_distinct_large_values():
    cols = {"line": "varchar", "big": "bigint", "created": "timestamp(6)"}
    rows = []
    # Two bigint values one apart, both beyond 2^53, in one group.
    base = 9007199254740992
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            val = base if (day + i) % 2 == 0 else base + 1
            rows.append(("L1", val, created.strftime("%Y-%m-%d %H:%M:%S")))
    lake = FakeLake(columns=cols, rows=rows, part_col="created", cutoff=_CUTOFF)
    sql = ("SELECT line, approx_percentile(big, 1.0) AS p FROM events WHERE "
           + _win("created") + " GROUP BY line")
    result = _exact(lake, sql)
    # The max (quantile 1.0) is the larger bigint, exact (double would collapse them).
    assert result["rows"] == lake.reference(sql)
    assert result["rows"][0][1] == str(base + 1)


# --------------------------------------------------------------------------- #
# Finding 6: percentile group keys use the typed engine-equivalent key.
# DECIMAL 1.5 and 1.50 are one group.
# --------------------------------------------------------------------------- #
def test_f6_decimal_keys_1_5_and_1_50_are_one_group():
    from agents.athena_exact_merge import PieceRows
    shape = shape_mod.parse_shape(
        "SELECT k, approx_percentile(v, 0.9) AS p FROM events WHERE v > 0 GROUP BY k")
    # Two pieces whose DECIMAL key prints as 1.5 and 1.50: one engine group.
    pieces = [
        PieceRows(["k", "p"], ["decimal(10,2)", "varchar"], [["1.5", _digest([10.0])]]),
        PieceRows(["k", "p"], ["decimal(10,2)", "varchar"], [["1.50", _digest([20.0])]]),
    ]
    order, digests, _accs, _display = pct.collect(shape, pieces)
    assert len(order) == 1, order


# --------------------------------------------------------------------------- #
# Finding 7: ORDER BY a percentile output is refused; ORDER BY a key works.
# --------------------------------------------------------------------------- #
def test_f7_order_by_a_percentile_is_refused():
    with pytest.raises(shape_mod.ShapeRefusal):
        shape_mod.parse_shape(
            "SELECT line, approx_percentile(dur, 0.9) AS p FROM events "
            "WHERE dur > 0 GROUP BY line ORDER BY p DESC")


def test_f7_order_by_a_key_still_parses():
    shape = shape_mod.parse_shape(
        "SELECT line, approx_percentile(dur, 0.9) AS p FROM events "
        "WHERE dur > 0 GROUP BY line ORDER BY line")
    assert shape.has_percentile and shape.order


# --------------------------------------------------------------------------- #
# Finding 8: the merge query must return exactly one well-formed row per gid
# that had digests; a missing/duplicate/unknown gid or wrong width refuses.
# --------------------------------------------------------------------------- #
def test_f8_a_missing_gid_refuses():
    shape = _percentile_shape()
    order = [("L1",), ("L2",)]
    digests = {("L1",): [_digest([1.0])], ("L2",): [_digest([2.0])]}
    with pytest.raises(pct.PercentileMergeError):
        pct.quantiles_by_group(lambda sql: [["0", "1.0"]], shape, order, digests)  # L2 (gid 1) missing


def test_f8_a_duplicate_gid_refuses():
    shape = _percentile_shape()
    order = [("L1",)]
    digests = {("L1",): [_digest([1.0])]}
    with pytest.raises(pct.PercentileMergeError):
        pct.quantiles_by_group(lambda sql: [["0", "1.0"], ["0", "2.0"]], shape, order, digests)


def test_f8_an_unknown_gid_refuses():
    shape = _percentile_shape()
    order = [("L1",)]
    digests = {("L1",): [_digest([1.0])]}
    with pytest.raises(pct.PercentileMergeError):
        pct.quantiles_by_group(lambda sql: [["0", "1.0"], ["7", "9.0"]], shape, order, digests)


def test_f8_a_group_with_no_digest_input_is_null_without_a_query():
    shape = _percentile_shape()
    order = [("L1",)]
    digests = {("L1",): []}  # no digest for the only group
    out = pct.quantiles_by_group(lambda sql: (_ for _ in ()).throw(AssertionError("no query")),
                                 shape, order, digests)
    assert out == {("L1",): None}


# --------------------------------------------------------------------------- #
# Finding 9: a stated accuracy constant passed to qdigest_agg, in the lineage
# and note.
# --------------------------------------------------------------------------- #
def test_f9_the_digest_expression_passes_the_accuracy_constant():
    expr = shape_mod.digest_piece_expr_typed("col", "double")
    assert str(shape_mod.QDIGEST_ACCURACY) in expr


def test_f9_the_lineage_and_note_state_the_accuracy():
    cols = {"line": "varchar", "dur": "bigint", "created": "timestamp(6)"}
    rows = [("L1", 60 * (i % 50 + 1), (_START + timedelta(days=d, minutes=i))
             .strftime("%Y-%m-%d %H:%M:%S")) for d in range(20) for i in range(12)]
    lake = FakeLake(columns=cols, rows=rows, part_col="created", cutoff=_CUTOFF)
    sql = ("SELECT line, approx_percentile(dur, ARRAY[0.9, 0.99]) AS p FROM events WHERE "
           + _win("created") + " GROUP BY line")
    result = _exact(lake, sql)
    assert result["exact_split"].get("percentile_accuracy") == shape_mod.QDIGEST_ACCURACY
    assert str(shape_mod.QDIGEST_ACCURACY) in result["note"]


# --------------------------------------------------------------------------- #
# helpers for the pure-module tests
# --------------------------------------------------------------------------- #
def _percentile_shape():
    """A parsed grouped percentile shape with one key column."""
    return shape_mod.parse_shape(
        "SELECT k, approx_percentile(v, 0.9) AS p FROM events WHERE v > 0 GROUP BY k")


def _digest(values):
    """The fake's hex-encoded digest for ``values`` (a JSON list)."""
    import json
    return json.dumps([float(v) for v in values]).encode("utf-8").hex().upper()
