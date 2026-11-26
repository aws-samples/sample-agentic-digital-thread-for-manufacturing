# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure parts of the Athena exact-total split: shape, merge and planner.

``test_athena_exact_split.py`` drives the whole connector; these pin each
pure piece directly:

* shape: what is admitted (one relation, keys + COUNT/SUM/MIN/MAX, GROUP BY,
  ORDER BY over outputs, numeric LIMIT), each refusal, the day bounds the
  WHERE gives, and the piece text (only ``FOR VERSION AS OF`` and one conjunct
  added);
* merge: per typed key with one NULL key, SQL NULL rules for SUM/MIN/MAX,
  exact integers and decimals, floats not called exact, mixed time zones
  refused, ORDER BY / NULLS / LIMIT applied after the merge;
* planner metadata: the source column chosen from path coverage (day, hour,
  month, year, identity), partition values per transform, the coverage read
  and the catalog read (the box planner itself is in
  ``test_athena_exact_boxes.py``).

Pure and offline; synthetic names only.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from agents import athena_exact_merge as merge
from agents import athena_exact_plan as plan
from agents.athena_exact_shape import ShapeRefusal, parse_shape, piece_sql, pinned_sql


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_admitted_shape_reads_outputs_order_limit_and_bounds():
    shape = parse_shape(
        "SELECT site, COUNT(*) AS n, SUM(qty) total, MIN(ts), MAX(ts) FROM lake.events e "
        "WHERE ts BETWEEN DATE '2026-10-01' AND DATE '2026-10-03' AND state = 'DONE' "
        "AND done_at >= TIMESTAMP '2026-10-05 08:00:00' "
        "GROUP BY site ORDER BY n DESC NULLS FIRST, 1 LIMIT 10;")
    assert [(o.kind, o.func, o.alias) for o in shape.outputs] == [
        ("key", None, None), ("agg", "count", "n"), ("agg", "sum", "total"),
        ("agg", "min", None), ("agg", "max", None)]
    assert shape.table_parts == ("lake", "events") and shape.grouped and shape.limit == 10
    assert [(o.column, o.descending, o.nulls_first) for o in shape.order] == [
        (1, True, True), (0, False, False)]
    assert shape.bounds == {"ts": ("2026-10-01", "2026-10-03"), "done_at": ("2026-10-05", None)}


@pytest.mark.parametrize("sql, words", [
    ("SELECT AVG(x) FROM t", "AVG"),
    ("SELECT COUNT(DISTINCT x) FROM t", "DISTINCT"),
    ("SELECT k, COUNT(*) FROM t GROUP BY k HAVING COUNT(*) > 1", "HAVING"),
    ("SELECT k, SUM(x) OVER () FROM t", "window"),
    ("SELECT a.k, COUNT(*) FROM t a JOIN u b ON a.k = b.k GROUP BY a.k", "join"),
    ("WITH c AS (SELECT * FROM t) SELECT COUNT(*) FROM c", "common table"),
    ("SELECT COUNT(*) FROM t UNION ALL SELECT COUNT(*) FROM u", "set operation"),
    ("SELECT COUNT(*) FROM t WHERE k IN (SELECT k FROM u)", "subquery"),
    ("SELECT COUNT(*) * 2 FROM t", "COUNT"),
    ("SELECT k, SUM(a) / COUNT(*) FROM t GROUP BY k", "SUM"),
    ("SELECT SUM(a) + (b) FROM t", "SUM"),
    ("SELECT SUM(COUNT(x)) FROM t", "SUM"),
    ("SELECT COUNT(*) FROM t GROUP BY k", "GROUP BY"),
    ("SELECT k, COUNT(*) FROM t", "neither a key"),
    ("SELECT k, COUNT(*) FROM t GROUP BY k, j", "GROUP BY"),
    ("SELECT k, COUNT(*) FROM t GROUP BY GROUPING SETS ((k))", "grouping"),
    ("SELECT k, COUNT(*) FROM t GROUP BY k ORDER BY j", "ORDER BY"),
    ("SELECT COUNT(*) FROM t LIMIT 5 OFFSET 2", "OFFSET"),
    ("SELECT COUNT(*) FROM t FOR VERSION AS OF 1", "time-travel"),
    ("SELECT * FROM t", "SELECT *"),
    ("SELECT k FROM t GROUP BY k", "no COUNT"),
    ("SELECT COUNT(*) FROM t, u", "one table"),
    ("SELECT COUNT(*) FROM t; SELECT 1", "statement"),
    ("SELECT approx_distinct(x) FROM t", "APPROX_DISTINCT"),
])
def test_refused_shapes_name_why(sql, words):
    with pytest.raises(ShapeRefusal) as info:
        parse_shape(sql)
    assert info.value.reason in {"not_decomposable", "unparsable"}
    assert words.lower() in info.value.detail.lower()


@pytest.mark.parametrize("sql, function", [
    ("SELECT SUM(rand()) FROM t", "rand"),
    ("SELECT COUNT(*) FROM t WHERE random() < 0.1", "random"),
    ("SELECT k, COUNT(*) FROM t WHERE uuid() IS NOT NULL GROUP BY k", "uuid"),
    ("SELECT MAX(element_at(shuffle(tags), 1)) FROM t", "shuffle"),
    ("SELECT SUM(date_diff('second', ts, now())) FROM t", "now"),
    ("SELECT MIN(current_timestamp) FROM t", "current_timestamp"),
    ("SELECT k, COUNT(*) FROM t GROUP BY k ORDER BY RAND()", "rand"),
])
def test_volatile_functions_are_refused(sql, function):
    # Each piece would evaluate them on its own: the merged rows would not be
    # one query's. The clock is still admitted in WHERE (said per piece).
    with pytest.raises(ShapeRefusal) as info:
        parse_shape(sql)
    assert info.value.reason == "volatile" and function in info.value.detail.lower()


def test_the_clock_in_where_is_still_admitted():
    shape = parse_shape("SELECT COUNT(*) FROM t WHERE ts < current_timestamp - INTERVAL '1' DAY")
    assert shape.uses_clock is True


def test_count_of_a_number_and_quoted_names_are_admitted():
    shape = parse_shape('SELECT "k", count(1) FROM "db"."t" GROUP BY k ORDER BY 2')
    assert [o.func for o in shape.outputs] == [None, "count"]
    assert shape.order[0].column == 1


def test_piece_text_adds_only_the_snapshot_and_one_conjunct():
    sql = "SELECT site, COUNT(*) FROM events e WHERE state = 'DONE' GROUP BY site"
    shape = parse_shape(sql)
    assert pinned_sql(shape, 7) == (
        "SELECT site, COUNT(*) FROM events FOR VERSION AS OF 7 e WHERE state = 'DONE' GROUP BY site")
    assert piece_sql(shape, '"c" IS NULL', 7) == (
        "SELECT site, COUNT(*) FROM events FOR VERSION AS OF 7 e WHERE \"c\" IS NULL "
        "AND (state = 'DONE') GROUP BY site")
    assert piece_sql(shape, '"c" IS NULL') == (
        "SELECT site, COUNT(*) FROM events e WHERE \"c\" IS NULL AND (state = 'DONE') GROUP BY site")


def test_piece_text_without_where_inserts_one_before_group_by():
    shape = parse_shape("SELECT k, SUM(x) FROM t GROUP BY k ORDER BY k LIMIT 9")
    assert piece_sql(shape, '"c" < DATE \'2026-01-02\'') == (
        "SELECT k, SUM(x) FROM t WHERE \"c\" < DATE '2026-01-02' GROUP BY k ORDER BY k LIMIT 9")


def test_bounds_ignore_or_and_functions_on_the_column():
    shape = parse_shape(
        "SELECT COUNT(*) FROM t WHERE (a >= DATE '2026-01-01' OR b = 1) "
        "AND date(c) = DATE '2026-01-01' AND '2026-02-03' <= d AND e < from_iso8601_timestamp('2026-03-04T00:00:00Z')")
    assert shape.bounds == {"d": ("2026-02-03", None), "e": (None, "2026-03-04")}


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
def _merge(sql, pieces, types, max_rows=500):
    shape = parse_shape(sql)
    cols = [f"c{i}" for i in range(len(types))]
    return merge.merge_pieces(shape, [merge.PieceRows(cols, types, rows) for rows in pieces],
                              max_rows=max_rows)


def test_grouped_merge_adds_counts_and_sums_per_key_with_one_null_key():
    out = _merge("SELECT k, COUNT(*), SUM(x), MIN(x), MAX(x) FROM t GROUP BY k",
                 [[["a", "2", "5", "1", "4"], [None, "1", None, None, None]],
                  [["a", "3", "7", "0", "9"], [None, "4", "6", "6", "6"], ["b", "1", None, None, None]]],
                 ["varchar", "bigint", "bigint", "bigint", "bigint"])
    assert sorted(out.rows, key=str) == sorted([
        ["a", "5", "12", "0", "9"], [None, "5", "6", "6", "6"], ["b", "1", None, None, None]], key=str)
    assert out.exact and not out.cut


def test_global_merge_keeps_null_sum_when_every_piece_is_null():
    out = _merge("SELECT COUNT(*), SUM(x), MIN(x) FROM t",
                 [[["0", None, None]], [["0", None, None]]], ["bigint", "bigint", "bigint"])
    assert out.rows == [["0", None, None]]


def test_integers_and_decimals_stay_exact():
    # Within BIGINT (past it one query overflows: test_athena_exact_merge_types).
    big = str(2 ** 61)
    ints = _merge("SELECT SUM(x) FROM t", [[[big]], [[big]], [[big]]], ["bigint"])
    assert ints.rows == [[str(3 * 2 ** 61)]]
    dec = _merge("SELECT SUM(x) FROM t", [[["0.10"]], [["0.20"]], [["12345678901234567890.05"]]],
                 ["decimal(38,2)"])
    assert dec.rows == [["12345678901234567890.35"]] and Decimal(dec.rows[0][0])


def test_float_sum_is_not_called_exact():
    out = _merge("SELECT SUM(x), MIN(x) FROM t", [[["0.1", "0.5"]], [["0.2", "0.25"]]],
                 ["double", "double"])
    assert out.exact is False and out.rows[0][1] == "0.25"


def test_min_max_on_text_dates_and_timestamps_keep_the_engine_text():
    out = _merge("SELECT MIN(x), MAX(x) FROM t",
                 [[["2026-09-02 00:00:08.000", "2026-09-03 10:00:00.000"]],
                  [["2026-09-01 23:59:59.999", "2026-09-02 00:00:00.000"]]],
                 ["timestamp(3)", "timestamp(3)"])
    assert out.rows == [["2026-09-01 23:59:59.999", "2026-09-03 10:00:00.000"]]


def test_min_max_across_time_zones_is_refused():
    with pytest.raises(merge.MergeError):
        _merge("SELECT MIN(x) FROM t", [[["2026-09-02 00:00:00.000 UTC"]],
                                        [["2026-09-01 00:00:00.000 Europe/Paris"]]],
               ["timestamp(3) with time zone"])


def test_pieces_with_different_columns_or_types_are_refused():
    shape = parse_shape("SELECT COUNT(*) FROM t")
    with pytest.raises(merge.MergeError):
        merge.merge_pieces(shape, [merge.PieceRows(["a"], ["bigint"], [["1"]]),
                                   merge.PieceRows(["a"], ["integer"], [["1"]])], max_rows=5)


def test_order_by_nulls_and_limit_after_merge():
    rows = [[["a", "3"], ["b", "1"], [None, "2"]], [["c", "3"], ["b", "4"]]]
    types = ["varchar", "bigint"]
    out = _merge("SELECT k, COUNT(*) AS n FROM t GROUP BY k ORDER BY n DESC, k LIMIT 3", rows, types)
    assert out.rows == [["b", "5"], ["a", "3"], ["c", "3"]]
    nulls = _merge("SELECT k, COUNT(*) FROM t GROUP BY k ORDER BY k NULLS FIRST", rows, types)
    assert nulls.rows[0][0] is None
    last = _merge("SELECT k, COUNT(*) FROM t GROUP BY k ORDER BY k DESC", rows, types)
    assert last.rows[-1][0] is None and last.rows[0][0] == "c"


def test_merged_rows_over_the_row_cap_are_marked_cut():
    out = _merge("SELECT k, COUNT(*) FROM t GROUP BY k", [[["a", "1"]], [["b", "1"]], [["c", "1"]]],
                 ["varchar", "bigint"], max_rows=2)
    assert out.cut and len(out.rows) == 2


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
_META = plan.TableMeta(
    columns={"region": ("region", "varchar"), "created": ("created", "timestamp(6)"),
             "updated": ("updated", "timestamp(6) with time zone"), "dt": ("dt", "date"),
             "done_at": ("done_at", "timestamp(6)"), "qty": ("qty", "bigint"),
             "note": ("note", "varchar")},
    field_ids={"region": 1, "created": 2, "updated": 3, "dt": 4, "done_at": 5, "qty": 6},
    iceberg=True)


def test_candidates_choose_the_field_covering_most_bytes():
    found = plan.candidates({"region": 900, "created_day": 700, "updated_hour": 300,
                             "dt": 50, "dt_hour": 10, "qty_day": 999, "created_month": 5}, _META)
    assert [(c.column, c.transform) for c in found] == [
        ("created", "day"), ("updated", "hour"), ("dt", "identity"), ("created", "month")]


def _cand(transform="day", column="created", col_type="timestamp(6)"):
    return plan.Candidate(column, col_type, transform, f"{column}_{transform}", 1)


@pytest.mark.parametrize("transform, value, start, end", [
    ("day", "2026-09-02", datetime(2026, 9, 2), datetime(2026, 9, 3)),
    ("hour", "2026-09-02-13", datetime(2026, 9, 2, 13), datetime(2026, 9, 2, 14)),
    ("month", "2026-12", datetime(2026, 12, 1), datetime(2027, 1, 1)),
    ("year", "2026", datetime(2026, 1, 1), datetime(2027, 1, 1)),
])
def test_partition_values_parse_per_transform(transform, value, start, end):
    assert plan.partition_range(_cand(transform), value) == (start, end)
    assert plan.partition_range(_cand(transform), "garbage") is None


def test_coverage_sql_reads_field_names_from_paths():
    sql = plan.coverage_sql('"db"."t$files"')
    assert "regexp_extract_all(file_path, '/([A-Za-z_][A-Za-z0-9_]*)=[^/]*', 1)" in sql


def test_read_table_meta_follows_a_resource_link_and_reads_field_ids():
    class Glue:
        def __init__(self):
            self.calls = []

        def get_table(self, **kw):
            self.calls.append(kw)
            if kw["Name"] == "link":
                return {"Table": {"Name": "link", "TargetTable": {
                    "CatalogId": "111122223333", "DatabaseName": "src", "Name": "events"}}}
            return {"Table": {"Name": "events", "Parameters": {"table_type": "ICEBERG"},
                              "StorageDescriptor": {"Columns": [
                                  {"Name": "Created", "Type": "timestamp(6)",
                                   "Parameters": {"iceberg.field.id": "2"}},
                                  {"Name": "note", "Type": "string"}]}}}
    glue = Glue()
    meta = plan.read_table_meta(glue, None, "db", "link")
    assert meta.iceberg and meta.field_ids == {"created": 2}
    assert meta.columns["created"] == ("Created", "timestamp(6)")
    assert glue.calls[1] == {"DatabaseName": "src", "Name": "events", "CatalogId": "111122223333"}


# --------------------------------------------------------------------------- #
# Engine line
# --------------------------------------------------------------------------- #
def test_engine_line_names_a_split_result_once_with_its_piece_count():
    from agents import executed_filters
    line = executed_filters.executed_filters_line([
        {"query_text": 'SELECT snapshot_id FROM "db"."t$snapshots"', "row_count": 1},
        {"query_text": 'SELECT f FROM "db"."t$files" WHERE x = 1', "row_count": 3},
        {"query_text": "SELECT COUNT(*) FROM t WHERE a = 'b'", "row_count": 1, "pieces": 5},
        {"query_text": "SELECT COUNT(*) FROM t WHERE c = 1", "row_count": 2}])
    assert "Q1 (1 row, computed from 5 partition pieces): WHERE a = 'b'" in line
    assert "Q2 (2 rows): WHERE c = 1" in line and "$files" not in line


def test_an_impossible_bound_date_is_ignored_by_the_planner():
    shape = parse_shape("SELECT COUNT(*) FROM t WHERE done_at >= DATE '2026-13-45'")
    assert plan.bounded_columns(shape, _META) == []


# --------------------------------------------------------------------------- #
# Admin caps (per system, next to max_scanned_bytes)
# --------------------------------------------------------------------------- #
def test_caps_ride_the_resolved_config_and_fail_closed():
    import agents.athena_tools as at
    from agents.athena_exact_split import resolve_caps
    base = {"workgroup": "wg", "database": "db"}
    assert "exact_split_max_pieces" not in at._resolve_config(base)
    cfg = at._resolve_config({**base, "exact_split_max_pieces": "8",
                              "exact_split_max_total_bytes": 2048, "exact_split_max_seconds": "30"})
    caps = resolve_caps(cfg)
    assert (caps.max_pieces, caps.max_total_bytes, caps.max_seconds) == (8, 2048, 30.0)
    for bad in (0, -1, "abc", True, "1.5"):
        with pytest.raises(at.AthenaConfigError):
            at._resolve_config({**base, "exact_split_max_pieces": bad})


def test_caps_persist_with_the_node_config():
    from agents.connection_resolver import _NODE_CONFIG_KEYS
    assert {"exact_split_max_pieces", "exact_split_max_total_bytes",
            "exact_split_max_seconds"} <= set(_NODE_CONFIG_KEYS)
