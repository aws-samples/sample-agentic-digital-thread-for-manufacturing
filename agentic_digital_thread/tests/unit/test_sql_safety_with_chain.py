# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The read-only guard must validate the statement AFTER a leading WITH chain.

`ensure_select_only` accepts a statement when its first keyword is a read-only
prefix. `WITH` is one of those prefixes, so a write hidden behind a CTE chain
(`WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x`) once slipped through.
The guard must look past the whole chain at the statement it actually governs,
and refuse a data-modifying CTE body too (PostgreSQL runs those).
"""
import pytest

from agents.sql_safety import ensure_select_only


# --- writes after a leading WITH chain are rejected -------------------------

@pytest.mark.parametrize("bad", [
    # The four spelled out in the brief.
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "WITH x AS (SELECT 1) DELETE FROM t WHERE id IN (SELECT * FROM x)",
    "WITH x AS (SELECT 1) CREATE TABLE t AS SELECT * FROM x",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    # Case and comment variants of the leading keyword.
    "with x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "/* c */ WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "-- c\nWITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    # A write after several CTEs.
    "WITH a AS (SELECT 1), b AS (SELECT 2) UPDATE t SET x = 1",
    # A write after a parenthesised SELECT inside the chain.
    "WITH a AS ((SELECT 1)) DELETE FROM t",
    # RECURSIVE / MATERIALIZED chains still guarding the tail.
    "WITH RECURSIVE a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
    "WITH a AS MATERIALIZED (SELECT 1) UPDATE t SET x = 1",
    "WITH a AS NOT MATERIALIZED (SELECT 1) MERGE INTO t USING a ON true",
    # Column list before AS.
    "WITH a(c) AS (SELECT 1) DELETE FROM t",
    # Other write kinds.
    "WITH a AS (SELECT 1) MERGE INTO t USING a ON true WHEN MATCHED THEN DELETE",
    "WITH a AS (SELECT 1) DROP TABLE t",
    "WITH a AS (SELECT 1) ALTER TABLE t ADD COLUMN c int",
    "WITH a AS (SELECT 1) COPY t FROM 'f'",
    "WITH a AS (SELECT 1) CALL p()",
    "WITH a AS (SELECT 1) EXECUTE s",
    # An unknown word after the chain.
    "WITH a AS (SELECT 1) FROBNICATE t",
])
def test_write_after_with_chain_is_rejected(bad):
    with pytest.raises(ValueError):
        ensure_select_only(bad)


# --- data-modifying CTE bodies are rejected ---------------------------------

@pytest.mark.parametrize("bad", [
    "WITH d AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM d",
    "WITH d AS (UPDATE t SET x = 1 RETURNING *) SELECT * FROM d",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH d AS (MERGE INTO t USING s ON true WHEN MATCHED THEN DELETE) SELECT 1",
    # Nested one level: the inner CTE body modifies.
    "WITH a AS (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d) SELECT * FROM a",
    # One good CTE then a modifying one.
    "WITH a AS (SELECT 1), d AS (DELETE FROM t RETURNING *) SELECT * FROM a",
])
def test_data_modifying_cte_body_is_rejected(bad):
    with pytest.raises(ValueError):
        ensure_select_only(bad)


# --- genuine reads after a WITH chain stay accepted -------------------------

@pytest.mark.parametrize("good", [
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b",
    "WITH RECURSIVE a(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM a WHERE n < 5) SELECT * FROM a",
    "WITH a AS MATERIALIZED (SELECT 1) SELECT * FROM a",
    "WITH a AS NOT MATERIALIZED (SELECT 1) SELECT * FROM a",
    "WITH a(c1, c2) AS (SELECT 1, 2) SELECT * FROM a",
    # VALUES and TABLE after a chain.
    "WITH a AS (SELECT 1) VALUES (1), (2)",
    "WITH a AS (SELECT 1) TABLE a",
    # Opening parenthesis leading to a SELECT.
    "WITH a AS (SELECT 1) (SELECT * FROM a)",
    "WITH a AS (SELECT 1) ((SELECT * FROM a))",
    # A CTE whose body contains the word insert inside a string / quoted name.
    "WITH a AS (SELECT 'insert into t' AS msg) SELECT * FROM a",
    'WITH a AS (SELECT 1 AS "insert into t") SELECT * FROM a',
    # Nested WITH in a body, both reads.
    "WITH a AS (WITH b AS (SELECT 1) SELECT * FROM b) SELECT * FROM a",
    # Comments between CTEs and before the body keyword.
    "WITH a AS (SELECT 1) /* c */ SELECT * FROM a",
    "WITH a AS ( /* c */ SELECT 1) SELECT * FROM a",
    "WITH a AS (-- c\nSELECT 1) SELECT * FROM a",
    # Case variants of the body keyword.
    "WITH a AS (select 1) select * from a",
])
def test_read_after_with_chain_is_accepted(good):
    assert ensure_select_only(good) == good


# --- a recursive CTE's SEARCH / CYCLE clauses (PostgreSQL 14+) ---------------

_REC = "WITH RECURSIVE t(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM t WHERE n < 3)"


@pytest.mark.parametrize("good", [
    f"{_REC} SEARCH DEPTH FIRST BY n SET ord SELECT * FROM t ORDER BY ord",
    f"{_REC} search breadth first by n, m set ord SELECT * FROM t",
    f"{_REC} CYCLE n SET is_cycle USING path SELECT * FROM t",
    f"{_REC} CYCLE n, m SET c TO 'Y' DEFAULT 'N' USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO true DEFAULT false USING p SELECT * FROM t",
    f"{_REC} SEARCH DEPTH FIRST BY n SET ord CYCLE n SET c USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c USING p, u AS (SELECT 2) SELECT * FROM t, u",
    f"{_REC} CYCLE n SET c TO DATE '2024-01-01' DEFAULT DATE '2024-01-02' USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO TIMESTAMP '2024-01-01 00:00' DEFAULT TIMESTAMP '2024-01-02 00:00' USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO INTERVAL '1' DAY DEFAULT INTERVAL '2' DAY USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO 'Y'::text DEFAULT 'N'::text USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO INTERVAL '1-2' YEAR TO MONTH DEFAULT INTERVAL '0-0' YEAR TO MONTH "
    "USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO INTERVAL '1 02:03:04' DAY TO SECOND DEFAULT INTERVAL '0' SECOND "
    "USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO 'Y'::pg_catalog.text DEFAULT 'N'::pg_catalog.text USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO -1 DEFAULT 0 USING p SELECT * FROM t",
])
def test_search_and_cycle_clauses_are_reads(good):
    assert ensure_select_only(good) == good


@pytest.mark.parametrize("bad", [
    f"{_REC} CYCLE n SET c USING p INSERT INTO x SELECT * FROM t",
    f"{_REC} SEARCH DEPTH FIRST BY n SET ord DELETE FROM x",
    f"{_REC} SEARCH DEPTH BY n SET ord SELECT * FROM t",      # FIRST missing
    f"{_REC} CYCLE n SET c TO 'Y' USING p SELECT * FROM t",    # DEFAULT missing
    f"{_REC} CYCLE SET c USING p SELECT * FROM t",             # no column
    f"{_REC} CYCLE n SET c USING p SEARCH DEPTH FIRST BY n SET o SELECT * FROM t",
    # SEARCH / CYCLE apply only to a recursive chain.
    "WITH t(n) AS (VALUES (1)) SEARCH DEPTH FIRST BY n SET ord SELECT * FROM t",
    "WITH t(n) AS (VALUES (1)) CYCLE n SET c USING p SELECT * FROM t",
    # A CYCLE mark value cannot hold parentheses or a statement.
    f"{_REC} CYCLE n SET c TO (SELECT 1) DEFAULT 'N' USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO DELETE FROM x RETURNING DEFAULT false USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO true DEFAULT UPDATE x SET y USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO INSERT DEFAULT false USING p SELECT * FROM t",
    f"{_REC} CYCLE n SET c TO 'Y'; DEFAULT 'N' USING p SELECT * FROM t",
])
def test_a_write_or_unreadable_clause_after_search_cycle_is_rejected(bad):
    with pytest.raises(ValueError):
        ensure_select_only(bad)
