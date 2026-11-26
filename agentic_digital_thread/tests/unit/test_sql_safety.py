# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the shared sql_safety helpers (row cap + read-only guard)."""
import pytest

from agents.sql_safety import apply_row_cap, ensure_select_only


def test_apply_row_cap_injects_limit_when_absent():
    sql, capped = apply_row_cap("SELECT * FROM t")
    assert sql == "SELECT * FROM t LIMIT 500"
    assert capped is True


def test_apply_row_cap_lowers_excessive_limit():
    sql, capped = apply_row_cap("SELECT * FROM t LIMIT 9999")
    assert sql.upper().endswith("LIMIT 500")
    assert capped is True


def test_apply_row_cap_keeps_small_limit():
    sql, capped = apply_row_cap("select a from t limit 10")
    assert capped is False
    assert "limit 10" in sql


def test_apply_row_cap_custom_max():
    sql, capped = apply_row_cap("SELECT 1", max_rows=5)
    assert sql.endswith("LIMIT 5")
    assert capped is True


@pytest.mark.parametrize("good", [
    "SELECT 1",
    "  select * from t",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SHOW TABLES",
    "DESCRIBE t",
    "-- a comment\nSELECT 1",
    "/* block */ SELECT 1",
    "SELECT 1;",
])
def test_ensure_select_only_allows_read_only(good):
    assert ensure_select_only(good) == good


@pytest.mark.parametrize("bad", [
    "DROP TABLE t",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x=1",
    "DELETE FROM t",
    "MERGE INTO t ...",
    "CREATE TABLE t (a int)",
    "SELECT 1; DROP TABLE t",
    "EXPLAIN SELECT 1",              # EXPLAIN not allowed (see below)
    "EXPLAIN ANALYZE DELETE FROM t",  # CRITICAL: EXPLAIN ANALYZE executes DML on Trino
    "  ",
    "-- only a comment",
])
def test_ensure_select_only_rejects_mutations_and_injection(bad):
    with pytest.raises(ValueError):
        ensure_select_only(bad)


@pytest.mark.parametrize("bad", [
    # SELECT ... INTO creates a table: a write in a single SELECT statement.
    "SELECT * INTO new_t FROM t",
    "select a, b into temp new_t from t",
    "SELECT * INTO UNLOGGED new_t FROM t",
    "(SELECT 1 INTO new_t)",
    "WITH x AS (SELECT 1) SELECT * INTO new_t FROM x",
    "SELECT * /* c */ INTO new_t FROM t",
])
def test_ensure_select_only_rejects_select_into(bad):
    with pytest.raises(ValueError, match="INTO"):
        ensure_select_only(bad)


@pytest.mark.parametrize("good", [
    "SELECT 'insert into t' AS note FROM t",
    'SELECT "into" FROM t',
    "SELECT 1 -- into\n",
    "SELECT intolerance, into_count FROM t",
])
def test_ensure_select_only_allows_into_outside_code(good):
    assert ensure_select_only(good) == good


@pytest.mark.parametrize("good", [
    # INTO is a valid column label after AS and a valid field name after a
    # dot (PostgreSQL and DuckDB both run these); neither creates a table.
    "SELECT 1 AS into",
    "SELECT 1 AS INTO FROM t",
    "SELECT t.into FROM (SELECT 1 AS into) AS t",
    'SELECT s."t".into FROM s',
])
def test_ensure_select_only_allows_into_as_identifier(good):
    assert ensure_select_only(good) == good


@pytest.mark.parametrize("bad", [
    # The SELECT ... INTO clause stays refused however it is spelled.
    "SELECT * INTO TEMPORARY TABLE new_t FROM t",
    "SELECT * InTo new_t FROM t",
    "SELECT * --c\nINTO new_t FROM t",
    "SELECT 1 AS into INTO new_t FROM t",
    "SELECT t.into INTO new_t FROM t",
    "WITH x AS (SELECT 1 INTO new_t) SELECT * FROM x",
])
def test_ensure_select_only_still_rejects_select_into_clause(bad):
    with pytest.raises(ValueError, match="INTO"):
        ensure_select_only(bad)
