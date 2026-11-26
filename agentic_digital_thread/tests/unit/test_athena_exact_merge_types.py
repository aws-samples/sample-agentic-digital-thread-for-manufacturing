# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The exact-split merge keeps the engine's numeric semantics, or refuses.

The merge (``athena_exact_merge``) used Python's default 28-digit Decimal
context, unbounded integers, the cells' raw text as GROUP BY keys and Python
float ordering. Against what the engine (Trino) does with ONE query:

* a ``decimal(38,2)`` SUM past 28 digits raised (or would round); one query
  returns it exactly, and raises on a sum past the declared precision;
* a BIGINT SUM past 2^63 - 1 came back as an unbounded Python int marked
  exact; one query raises a numeric overflow;
* ``0.0`` and ``-0.0`` (and NaN in two pieces) were separate DOUBLE groups;
  one query's GROUP BY makes each one group;
* MIN/MAX over a DOUBLE holding NaN followed Python's comparison of NaN,
  which depends on the order the pieces came in.

Where the engine's result is known the merge now reproduces it (each case
below is checked against one query's answer, written out); where it is an
error or not certain, the merge refuses with a named reason and no total is
returned. Pure and offline.
"""
from __future__ import annotations

from decimal import Context, Decimal

import pytest

from agents import athena_exact_merge as merge
from agents.athena_exact_shape import parse_shape

BIGINT_MAX = 2 ** 63 - 1


def _merge(sql: str, pieces: list, types: list[str]) -> merge.Merged:
    shape = parse_shape(sql)
    columns = [f"c{i}" for i in range(len(types))]
    return merge.merge_pieces(shape, [merge.PieceRows(columns, types, rows) for rows in pieces],
                              max_rows=500)


def _refused(sql: str, pieces: list, types: list[str]) -> str:
    with pytest.raises(merge.MergeError) as info:
        _merge(sql, pieces, types)
    return info.value.reason


# --------------------------------------------------------------------------- #
# DECIMAL
# --------------------------------------------------------------------------- #
def test_a_decimal_sum_past_28_digits_is_exact():
    values = ["123456789012345678901234567890.15", "876543210987654321098765432109.90",
              "0.01"]
    wide = Context(prec=80)                                          # the engine's exact sum
    one_query = wide.add(wide.add(Decimal(values[0]), Decimal(values[1])), Decimal(values[2]))
    assert len(one_query.as_tuple().digits) > 28
    out = _merge("SELECT SUM(x) FROM t", [[[v]] for v in values], ["decimal(38,2)"])
    assert out.rows == [[format(one_query, "f")]] and out.exact


def test_a_decimal_sum_past_the_declared_precision_is_refused():
    # decimal(38,2) holds 36 integer digits; one query raises on the sum.
    near = "9" * 36 + ".00"
    assert Context(prec=40).add(Decimal(near), Decimal(near)) >= Decimal(10) ** 36
    assert _refused("SELECT SUM(x) FROM t", [[[near]], [[near]]], ["decimal(38,2)"]) == "numeric_overflow"


def test_a_decimal_sum_reads_precision_and_scale_from_the_column_type():
    out = _merge("SELECT SUM(x) FROM t", [[["1.50"]], [["2.25"]]], ["decimal(38,2)"])
    assert out.rows == [["3.75"]]
    # The bound is the result column's declared precision, whatever it is.
    assert _refused("SELECT SUM(x) FROM t", [[["9.5"]], [["0.6"]]], ["decimal(2,1)"]) == "numeric_overflow"


def test_the_connector_reports_a_decimal_columns_precision_and_scale():
    import agents.athena_tools as at

    class Results:
        def get_query_results(self, **_kwargs):
            info = [{"Name": "s", "Type": "decimal", "Precision": 38, "Scale": 2},
                    {"Name": "n", "Type": "bigint", "Precision": 19, "Scale": 0}]
            rows = [{"Data": [{"VarCharValue": "s"}, {"VarCharValue": "n"}]},
                    {"Data": [{"VarCharValue": "1.25"}, {"VarCharValue": "3"}]}]
            return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": info}, "Rows": rows}}
    types: list = []
    at._collect_results(Results(), "q1", 10, types)
    assert types == ["decimal(38,2)", "bigint"]


# --------------------------------------------------------------------------- #
# BIGINT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pieces", [[[[str(BIGINT_MAX)]], [["1"]]],
                                    [[[str(-BIGINT_MAX - 1)]], [["-1"]]]],
                         ids=["above", "below"])
def test_a_bigint_sum_past_64_bits_is_refused_not_returned_unbounded(pieces):
    assert _refused("SELECT SUM(x) FROM t", pieces, ["bigint"]) == "numeric_overflow"


def test_a_bigint_sum_at_the_edge_is_exact():
    out = _merge("SELECT SUM(x) FROM t", [[[str(BIGINT_MAX - 1)]], [["1"]]], ["bigint"])
    assert out.rows == [[str(BIGINT_MAX)]] and out.exact


# --------------------------------------------------------------------------- #
# GROUP BY keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("data_type", ["double", "real"])
def test_signed_zero_and_nan_keys_are_one_group_each(data_type):
    pieces = [[["0.0", "2"], ["NaN", "1"]], [["-0.0", "3"], ["NaN", "4"], ["1.5", "1"]]]
    out = _merge("SELECT k, COUNT(*) FROM t GROUP BY k", pieces, [data_type, "bigint"])
    counts = {row[0]: row[1] for row in out.rows}
    # One query: three groups, zero (5 rows), NaN (5 rows) and 1.5 (1 row).
    assert len(out.rows) == 3 and counts["NaN"] == "5" and counts["1.5"] == "1"
    assert counts.get("0.0", counts.get("-0.0")) == "5"


def test_numeric_keys_group_by_value_not_by_text():
    out = _merge("SELECT k, COUNT(*) FROM t GROUP BY k", [[["1.50", "1"]], [["1.5", "2"]]],
                 ["decimal(10,2)", "bigint"])
    assert len(out.rows) == 1 and out.rows[0][1] == "3"


@pytest.mark.parametrize("data_type", ["", "array(varchar)", "timestamp(3) with time zone"])
def test_a_key_whose_grouping_cannot_be_established_is_refused(data_type):
    reason = _refused("SELECT k, COUNT(*) FROM t GROUP BY k", [[["a", "1"]], [["a", "1"]]],
                      [data_type, "bigint"])
    assert reason == "unmergeable_type"


# --------------------------------------------------------------------------- #
# MIN / MAX over floating point
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("func", ["MIN", "MAX"])
@pytest.mark.parametrize("pieces", [[[["NaN"]], [["1.0"]]], [[["1.0"]], [["NaN"]]]],
                         ids=["nan first", "nan last"])
def test_min_max_over_a_double_holding_nan_is_refused(func, pieces):
    assert _refused(f"SELECT {func}(x) FROM t", pieces, ["double"]) == "float_nan"


def test_min_max_tied_on_signed_zero_is_refused():
    # Which zero one query prints at a tie is not certain: refused.
    reason = _refused("SELECT MIN(x) FROM t", [[["0.0"]], [["-0.0"]]], ["double"])
    assert reason == "float_signed_zero"


def test_order_by_a_double_holding_nan_is_refused():
    reason = _refused("SELECT k, COUNT(*) FROM t GROUP BY k ORDER BY k",
                      [[["NaN", "1"]], [["1.0", "2"]]], ["double", "bigint"])
    assert reason == "float_nan"


def test_min_max_over_finite_doubles_still_merges():
    out = _merge("SELECT MIN(x), MAX(x) FROM t", [[["-1.5", "2.0"]], [["0.25", "Infinity"]]],
                 ["double", "double"])
    assert out.rows == [["-1.5", "Infinity"]]
