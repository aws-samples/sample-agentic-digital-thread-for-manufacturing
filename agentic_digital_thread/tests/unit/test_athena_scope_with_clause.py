# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A WITH clause opens only where a query can begin; ``with time zone`` is a type.

Found while replaying the row exclusion over logged queries: the shared CTE
reader (``athena_scope._cte_bindings``) read EVERY ``WITH`` word as the start
of a WITH clause, so the ``with`` of a type such as ``timestamp(6) with time
zone`` (or of ``UNNEST(...) WITH ORDINALITY``) made it report an unreadable
WITH chain, ``relations`` reported the query uncertain, and every rule that
reads relations gave no decision on a plain single-table read.

The reader now opens a WITH clause only where a statement or a parenthesised
query can begin: the first token, after ``(`` or ``;``, or after a word a
query follows (``AS`` of CREATE TABLE / VIEW, ``EXPLAIN [ANALYZE]
[VERBOSE]``); a ``WITH [LOCAL] TIME ZONE`` is part of a type wherever it is.
A real WITH chain, a nested one, and a CTE named like a type word are read as
before.

Fully offline; synthetic names.
"""
from __future__ import annotations

import pytest

from agents import athena_scope, record_key, row_exclusion, storage_bound_rule
from agents.athena_scope import _cte_bindings, relations, tokenize

_WIN = ("finished_at >= TIMESTAMP '2026-10-01 00:00:00 UTC' "
        "AND finished_at < TIMESTAMP '2026-10-02 00:00:00 UTC'")


def _read(sql: str):
    tokens, uncertain = tokenize(sql)
    assert not uncertain
    bindings, cte_uncertain = _cte_bindings(tokens)
    scans, scan_uncertain = relations(tokens)
    return [b.name for b in bindings], cte_uncertain, [s.table for s in scans], scan_uncertain


@pytest.mark.parametrize("cast", [
    "CAST(finished_at AS timestamp(6) with time zone)",
    "CAST(finished_at AS timestamp with time zone)",
    "CAST(finished_at AS TIMESTAMP(3) WITH TIME ZONE)",
    "CAST(finished_at AS timestamp(6) with local time zone)",
    "CAST(begun_time AS time(3) with time zone)",
    "date_diff('hour', begun_at, CAST(TIMESTAMP '2026-10-01 00:00:00' AS timestamp(6) "
    "with time zone))",
])
def test_a_type_with_a_time_zone_is_not_a_with_clause(cast):
    sql = f"SELECT state, {cast} AS t FROM job_events WHERE state = 'FAILED' AND {_WIN}"
    assert _read(sql) == ([], False, ["job_events"], False)


def test_a_typed_cast_in_the_where_clause_is_read_too():
    sql = ("SELECT COUNT(*) AS n FROM job_events WHERE finished_at >= "
           "CAST(TIMESTAMP '2026-10-01 00:00:00' AS timestamp(6) with time zone)")
    assert _read(sql) == ([], False, ["job_events"], False)


def test_a_cte_query_that_also_uses_the_type_reads_its_chain():
    sql = ("WITH x AS (SELECT id, CAST(finished_at AS timestamp(6) with time zone) AS t "
           "FROM job_events WHERE state = 'DONE'), "
           "y AS (SELECT id FROM x WHERE t >= CAST(TIMESTAMP '2026-10-01 00:00:00' AS "
           "timestamp(6) with time zone)) SELECT COUNT(*) AS n FROM y")
    assert _read(sql) == (["x", "y"], False, ["job_events"], False)


def test_a_nested_with_in_a_derived_table_is_still_a_clause():
    sql = ("SELECT COUNT(*) AS n FROM (WITH y AS (SELECT CAST(finished_at AS timestamp "
           "with time zone) AS t FROM job_events) SELECT t FROM y) z")
    assert _read(sql) == (["y"], False, ["job_events"], False)


def test_a_with_in_an_in_subquery_is_still_a_clause():
    sql = ("SELECT COUNT(*) AS n FROM job_events WHERE run_ref IN "
           "(WITH k AS (SELECT batch_tag FROM ledger_lines) SELECT batch_tag FROM k)")
    assert _read(sql) == (["k"], False, ["job_events", "ledger_lines"], False)


def test_unnest_with_ordinality_is_not_a_with_clause():
    sql = ("SELECT v, n FROM job_events CROSS JOIN UNNEST(split(tags, ',')) "
           "WITH ORDINALITY AS u(v, n) WHERE state = 'DONE'")
    assert _read(sql) == ([], False, ["job_events"], False)


def test_a_statement_start_and_a_second_statement_still_open_a_clause():
    assert _read("WITH zone AS (SELECT 1 AS n FROM job_events) SELECT n FROM zone") == (
        ["zone"], False, ["job_events"], False)
    names, cte_uncertain, _scans, _u = _read(
        "SELECT 1 AS n; WITH x AS (SELECT id FROM job_events) SELECT id FROM x")
    assert names == ["x"] and cte_uncertain is False


@pytest.mark.parametrize("prefix", ["EXPLAIN", "EXPLAIN ANALYZE", "EXPLAIN ANALYZE VERBOSE",
                                    "CREATE TABLE out_t AS", "CREATE VIEW out_v AS"])
def test_a_query_after_a_statement_prefix_still_opens_a_clause(prefix):
    names, cte_uncertain, scans, _u = _read(
        f"{prefix} WITH x AS (SELECT id FROM job_events) SELECT id FROM x")
    assert names == ["x"] and cte_uncertain is False and scans == ["job_events"]


def test_a_malformed_real_with_chain_is_still_uncertain():
    tokens, _u = tokenize("WITH x (SELECT id FROM job_events) SELECT id FROM x")
    assert _cte_bindings(tokens)[1] is True


# --------------------------------------------------------------------------- #
# The rules that read relations now decide on these queries.
# --------------------------------------------------------------------------- #
_TYPED_READ = ("SELECT step_kind, COUNT(*) AS n, MAX(date_diff('hour', begun_at, "
               "CAST(TIMESTAMP '2026-10-01 00:00:00' AS timestamp(6) with time zone))) AS h "
               f"FROM job_events WHERE state = 'FAILED' AND {_WIN} GROUP BY step_kind")


def test_the_bounded_column_reader_reads_a_typed_single_table_read():
    read = storage_bound_rule.bounded_columns(_TYPED_READ)
    assert read is not None and set(read[1]) == {"state", "finished_at"}


def test_the_row_exclusion_check_decides_a_typed_single_table_read():
    fact = row_exclusion.exclusion_from("shadow copies", [
        {"column": "pipeline", "op": "not_empty"}, {"column": "run_ref", "op": "not_empty"}])
    assert row_exclusion.classify(_TYPED_READ, "job_events", fact).kind == "missing"
    with_pred = f"{_TYPED_READ.split(' GROUP BY')[0]} AND {row_exclusion.predicate_sql(fact)}"
    assert row_exclusion.classify(with_pred, "job_events", fact).kind == "applied"


def test_the_record_key_check_decides_a_dedupe_whose_cte_uses_the_type():
    sql = ("WITH latest AS (SELECT id, CAST(finished_at AS timestamp(6) with time zone) AS t, "
           "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn FROM job_events) "
           "SELECT COUNT(*) AS n FROM latest WHERE rn = 1")
    found = record_key.decide(sql, "job_events", ["id", "revision", "run_ref"])
    assert found is not None and list(found.subset) == ["id"]


def test_the_scope_reader_sees_one_certain_scan():
    tokens, _u = tokenize(_TYPED_READ)
    scans, uncertain = athena_scope.relations(tokens)
    assert [s.identity for s in scans] == ["job_events"] and uncertain is False
