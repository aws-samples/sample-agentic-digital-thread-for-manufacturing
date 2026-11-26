# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DuckDB dialect forms the shared readers must recognise (review F2).

DuckDB writes a timestamp/date bound as ``'2026-01-01'::TIMESTAMP`` (postfix
cast) as well as the ``TIMESTAMP '...'`` form Athena uses, and de-duplicates
with ``QUALIFY ROW_NUMBER() OVER (PARTITION BY <subset>) = 1`` instead of a
latest-row CTE. The storage/window readers must see the ``::TYPE`` bound and
the record-key reader must see the QUALIFY de-dup, on either engine. Pure.
"""
import duckdb

from agents import record_key
from agents.storage_bound_rule import bounded_columns

T = "s3t.flow.segments"


def _runs_in_duckdb(sql):
    """The QUALIFY/de-dup SQL must be executable DuckDB (every
    positive fixture runs in a real engine). Uses a bare table ``segments`` so
    the fixture needs no catalog; the rule reads terminal names the same way."""
    con = duckdb.connect()
    con.execute("CREATE TABLE segments(id BIGINT, segment_type VARCHAR, revision BIGINT)")
    con.execute("INSERT INTO segments VALUES (1,'A',1),(1,'A',2),(2,'B',1)")
    con.execute(sql)   # raises if the statement is not valid DuckDB
    con.close()


def _bound(sql):
    read = bounded_columns(sql, include_same_column_or=True)
    return {} if read is None else read[1]


# --- ::TYPE postfix cast is a literal bound ---------------------------------

def test_timestamp_postfix_cast_is_a_bound():
    assert "observed_at" in _bound(
        f"SELECT 1 FROM {T} WHERE observed_at >= '2026-10-05'::TIMESTAMP")


def test_date_postfix_cast_is_a_bound():
    assert "observed_at" in _bound(
        f"SELECT 1 FROM {T} WHERE observed_at >= '2026-10-05'::DATE")


def test_timestamptz_postfix_cast_is_a_bound():
    assert "observed_at" in _bound(
        f"SELECT 1 FROM {T} WHERE observed_at >= '2026-10-05 00:00:00'::TIMESTAMPTZ")


def test_postfix_cast_on_the_left_side_is_a_bound():
    assert "observed_at" in _bound(
        f"SELECT 1 FROM {T} WHERE '2026-10-05'::TIMESTAMP <= observed_at")


def test_iso_literal_still_a_bound():
    assert "observed_at" in _bound(
        f"SELECT 1 FROM {T} WHERE observed_at >= TIMESTAMP '2026-10-05'")


# --- QUALIFY ROW_NUMBER de-dup is a record-key de-duplication ---------------

_KEY = {"id": None, "segment_type": None}


def _derived_qualify(subset, table="segments"):
    # The EXECUTABLE DuckDB de-dup shape: QUALIFY inside a derived table, then
    # COUNT it outside (a bare SELECT COUNT(*) ... QUALIFY is rejected by
    # DuckDB because the non-aggregated columns are not grouped).
    return (f"SELECT COUNT(*) AS n FROM (SELECT * FROM {table} "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {subset} ORDER BY revision DESC) = 1) d")


def test_qualify_derived_table_subset_is_a_strict_subset_dedup():
    sql = _derived_qualify("id", table=T)
    found = record_key.decide(sql, "segments", _KEY)
    assert found is not None and found.refuse is True
    _runs_in_duckdb(_derived_qualify("id"))          # the shape is executable


def test_qualify_derived_table_full_key_is_not_refused():
    sql = _derived_qualify("id, segment_type", table=T)
    found = record_key.decide(sql, "segments", _KEY)
    assert found is None or found.refuse is False
    _runs_in_duckdb(_derived_qualify("id, segment_type"))


def test_bare_qualify_without_outer_count_is_not_read():
    # Not a record de-dup the figure depends on, and not executable DuckDB
    # (so it must not be classified as one). record_key.decide -> None.
    sql = (f"SELECT COUNT(*) AS n FROM {T} "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) = 1")
    assert record_key.decide(sql, "segments", _KEY) is None


def test_grouped_query_with_qualify_is_not_record_dedup():
    # A GROUP BY block with QUALIFY filters already-aggregated GROUPS, not
    # records; it must NOT be classified as subset record de-duplication.
    sql = ("SELECT id, COUNT(*) AS n FROM segments GROUP BY id "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY MAX(revision) DESC) = 1")
    assert record_key.decide(sql.replace("segments", T), "segments", _KEY) is None
    _runs_in_duckdb(sql)                             # the grouped shape is executable


# --- R4-F4: a QUALIFY inside a CTE, counted outside, is record de-dup -------

def _cte_qualify(subset, outer="COUNT(*)", table="segments"):
    return (f"WITH d AS (SELECT * FROM {table} "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {subset} ORDER BY revision DESC) = 1) "
            f"SELECT {outer} AS n FROM d")


def test_cte_qualify_subset_count_is_a_strict_subset_dedup():
    sql = _cte_qualify("id", table=T)
    found = record_key.decide(sql, "segments", _KEY)
    assert found is not None and found.refuse is True
    _runs_in_duckdb(_cte_qualify("id"))


def test_cte_qualify_subset_sum_is_a_strict_subset_dedup():
    sql = _cte_qualify("id", outer="SUM(revision)", table=T)
    found = record_key.decide(sql, "segments", _KEY)
    assert found is not None and found.refuse is True
    _runs_in_duckdb(_cte_qualify("id", outer="SUM(revision)"))


def test_cte_qualify_full_key_is_not_refused():
    sql = _cte_qualify("id, segment_type", table=T)
    found = record_key.decide(sql, "segments", _KEY)
    assert found is None or found.refuse is False
    _runs_in_duckdb(_cte_qualify("id, segment_type"))


def test_cte_qualify_passthrough_consumer_is_not_read():
    # The outer selects the kept rows (no multiplicity aggregate) -> not read.
    sql = _cte_qualify("id", outer="*", table=T).replace("SELECT * AS n", "SELECT *")
    assert record_key.decide(sql, "segments", _KEY) is None


def test_cte_qualify_grouped_body_is_not_record_dedup():
    sql = ("WITH d AS (SELECT id, COUNT(*) c FROM " + T + " GROUP BY id "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY MAX(revision) DESC) = 1) "
           "SELECT COUNT(*) AS n FROM d")
    assert record_key.decide(sql, "segments", _KEY) is None
    _runs_in_duckdb(sql.replace(T, "segments"))
