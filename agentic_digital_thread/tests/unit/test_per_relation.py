# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-physical-read splitting for the shared guard (agents.per_relation).

Covers the single-table-block reconstruction used by the record-key and
catalog window rules, and the CTE/derived-table-join detection that fails
closed when an outer block combines relation-producing CTEs over a rule table
Pure; no engine. Dataset-neutral names.
"""
from agents.per_relation import single_table_reads

T = "s3t.flow.segments"
O = "s3t.flow.other"


def _reads(sql):
    return single_table_reads(sql)


def test_single_scan_is_one_non_multi_read():
    reads = _reads(f"SELECT COUNT(*) FROM {T} WHERE observed_at >= TIMESTAMP '2026-10-05'")
    assert len(reads) == 1 and reads[0].multi_scan is False
    assert reads[0].terminal == "segments"
    assert reads[0].combines_relations is False


def test_scalar_subquery_isolates_each_read():
    reads = _reads(f"SELECT (SELECT COUNT(*) FROM {T} WHERE route_name <> '') AS n, o.id FROM {O} o")
    terms = {r.terminal: r.multi_scan for r in reads}
    assert terms.get("segments") is False
    assert all(not r.combines_relations for r in reads)


def test_join_of_two_base_tables_is_one_combined_read():
    # A join is not PROVEN: the prover returns one synthetic combining read
    # the caller refuses on, carrying both terminals.
    reads = _reads(f"SELECT COUNT(*) FROM {T} s JOIN {O} o ON s.id = o.id")
    assert len(reads) == 1 and reads[0].combines_relations is True
    assert set(reads[0].combined_terminals) == {"segments", "other"}


def test_cte_join_marks_the_first_read_combines_relations():
    sql = (f"WITH x AS (SELECT * FROM {T}), y AS (SELECT * FROM {O}) "
           "SELECT COUNT(*) FROM x JOIN y ON x.id = y.id WHERE x.observed_at >= TIMESTAMP '2026-10-05'")
    reads = _reads(sql)
    combined = [r for r in reads if r.combines_relations]
    assert len(combined) == 1
    assert "segments" in combined[0].combined_terminals


def test_single_cte_read_is_not_a_combined_join():
    sql = f"WITH x AS (SELECT * FROM {T}) SELECT COUNT(*) FROM x"
    reads = _reads(sql)
    assert all(not r.combines_relations for r in reads)


def test_multi_statement_is_unproven_not_proven():
    # Several statements are not a proven shape: one synthetic read the caller
    # refuses on, flagged unproven.
    reads = _reads(f"SELECT 1 FROM {T}; SELECT 2 FROM {O}")
    assert len(reads) == 1 and reads[0].combines_relations is True
    assert reads[0].unproven is True


def test_union_is_unproven():
    # A set operator is rejected anywhere.
    reads = _reads(f"SELECT id FROM {T} UNION ALL SELECT id FROM {O}")
    assert len(reads) == 1 and reads[0].unproven is True


def test_proven_single_scan_classification():
    from agents.per_relation import prove, PROVEN
    assert prove(f"SELECT COUNT(*) FROM {T} WHERE observed_at >= TIMESTAMP '2026-10-05'").verdict == PROVEN


def test_cte_chain_no_join_is_proven():
    from agents.per_relation import prove, PROVEN
    sql = f"WITH x AS (SELECT * FROM {T}), y AS (SELECT * FROM x) SELECT COUNT(*) FROM y"
    assert prove(sql).verdict == PROVEN


# --- R7-F1/F2: the prover is closed -- no data-reading PROVEN with zero reads

import pytest
from agents.per_relation import prove, PROVEN


def _proof(sql):
    return prove(sql)


R7_NOT_PROVEN = [
    # R7-F1: a data-bearing FROM that yields no scan must NOT prove.
    pytest.param(f"SELECT COUNT(*) FROM ((({T})))", id="nested-parentheses"),
    pytest.param(f"SELECT * FROM (PIVOT {T} ON route_name USING count(*))", id="pivot"),
    pytest.param(
        f"SELECT * FROM (UNPIVOT {T} ON observed_at, done_at INTO NAME m VALUE v)",
        id="unpivot"),
    # R7-F2: shadowing CTE and WITH RECURSIVE.
    pytest.param(f"WITH segments AS (SELECT * FROM {T}) SELECT COUNT(*) FROM segments",
                 id="cte-shadows-base-terminal"),
    pytest.param(f"WITH RECURSIVE x AS (SELECT * FROM {T}) SELECT COUNT(*) FROM x",
                 id="with-recursive"),
    # already non-proven families that must stay non-proven.
    pytest.param(f"FROM {T} SELECT COUNT(*)", id="duckdb-from-first"),
    pytest.param(f"SELECT * FROM {T} s ASOF JOIN {O} o ON s.id >= o.id", id="asof-join"),
    pytest.param(f"SELECT * FROM {T} s POSITIONAL JOIN {O} o", id="positional-join"),
]


@pytest.mark.parametrize("sql", R7_NOT_PROVEN)
def test_r7_shape_is_not_proven(sql):
    assert _proof(sql).verdict != PROVEN


def test_r7_no_proven_verdict_ever_has_zero_reads_for_a_data_read():
    # The core invariant: a PROVEN statement that reads a relation always has
    # at least one read.
    for sql in [p.values[0] for p in R7_NOT_PROVEN]:
        p = _proof(sql)
        if p.verdict == PROVEN:
            assert p.reads, f"{sql!r} proved with zero reads"


# R7-F2 (corrected brief): quoted / commented / literal-bearing forms must
# prove EXACTLY as their unquoted form, and run.
R7_PROVEN = [
    pytest.param('SELECT COUNT(*) FROM "s3t"."flow"."segments"', id="quoted-identifiers"),
    pytest.param(f"SELECT COUNT(*) /* JOIN UNION */ FROM {T}", id="sql-comment"),
    pytest.param(f"SELECT 'JOIN UNION', COUNT(*) FROM {T}", id="join-union-string-literal"),
    pytest.param(f"SELECT (SELECT COUNT(*) FROM {T}) AS n, o.id FROM {O} o", id="scalar-subquery"),
    pytest.param(f"SELECT o.id FROM {O} o WHERE EXISTS (SELECT 1 FROM {T} s WHERE s.id = o.id)",
                 id="exists-subquery"),
    pytest.param(f"SELECT o.id FROM {O} o WHERE o.id IN (SELECT id FROM {T})", id="in-subquery"),
    pytest.param(f"WITH x AS (SELECT * FROM {T}), y AS (SELECT * FROM x) SELECT COUNT(*) FROM y",
                 id="single-table-cte-chain"),
]


@pytest.mark.parametrize("sql", R7_PROVEN)
def test_r7_shape_is_proven(sql):
    assert _proof(sql).verdict == PROVEN


def test_r7_quoted_form_proves_as_unquoted_form():
    bare = prove(f"SELECT COUNT(*) FROM {T} WHERE observed_at >= TIMESTAMP '2026-10-05'")
    quoted = prove('SELECT COUNT(*) FROM "s3t"."flow"."segments" '
                   'WHERE observed_at >= TIMESTAMP \'2026-10-05\'')
    assert bare.verdict == quoted.verdict == PROVEN
    assert [r.terminal for r in quoted.reads] == [r.terminal for r in bare.reads] == ["segments"]


# --- R8-F1/F2: zero-read is UNPROVEN; the FROM source is a closed grammar ----

R8_NOT_PROVEN = [
    pytest.param(f"SELECT * FROM ({T})", id="parenthesized-source"),
    pytest.param(f"SELECT * FROM {T} AS x(a, b, c, d)", id="alias-column-list"),
    pytest.param(f"SELECT * FROM {T} TABLESAMPLE 50 PERCENT", id="tablesample"),
    pytest.param(f"SELECT * FROM {T} USING SAMPLE 10%", id="using-sample"),
    pytest.param(f"WITH x AS (SELECT * FROM {T}) SELECT COUNT(*) FROM x a JOIN x b ON a.id = b.id",
                 id="cte-referenced-twice"),
]


@pytest.mark.parametrize("sql", R8_NOT_PROVEN)
def test_r8_source_grammar_rejects_unmodeled_suffix(sql):
    assert _proof(sql).verdict != PROVEN


@pytest.mark.parametrize("sql", ["SELECT 1", "SHOW TABLES", f"DESCRIBE {T}", "SELECT 1 AS n"])
def test_r8_zero_read_statement_is_not_proven(sql):
    # A statement that reads no relation is UNPROVEN under the per-relation
    # contract (callers allow a no-data read separately), never PROVEN.
    proof = _proof(sql)
    assert proof.verdict != PROVEN
    assert not proof.reads


R8_PROVEN = [
    # The two closed source forms with an optional alias still prove.
    pytest.param(f"SELECT COUNT(*) FROM {T} x WHERE x.id > 0", id="bare-alias-no-as"),
    pytest.param(f"SELECT COUNT(*) FROM {T} AS x WHERE x.id > 0", id="as-alias"),
    pytest.param(f"SELECT COUNT(*) FROM (SELECT * FROM {T}) d", id="subquery-alias"),
    pytest.param('SELECT COUNT(*) FROM "s3t"."flow"."segments" AS "s" WHERE "s"."id" > 0',
                 id="quoted-with-alias"),
]


@pytest.mark.parametrize("sql", R8_PROVEN)
def test_r8_closed_source_forms_still_prove(sql):
    proof = _proof(sql)
    assert proof.verdict == PROVEN and proof.reads


# --- R9-F1: a clause introducer with no body is incomplete SQL, not proven ---

R9_INCOMPLETE = [
    pytest.param(f"SELECT * FROM {T} WHERE", id="incomplete-where"),
    pytest.param(f"SELECT * FROM {T} GROUP BY", id="empty-group-by"),
    pytest.param(f"SELECT * FROM {T} ORDER BY", id="empty-order-by"),
    pytest.param(f"SELECT COUNT(*) FROM {T} HAVING", id="empty-having"),
]


@pytest.mark.parametrize("sql", R9_INCOMPLETE)
def test_r9_incomplete_clause_is_not_proven(sql):
    assert _proof(sql).verdict != PROVEN


R9_COMPLETE = [
    pytest.param(f"SELECT * FROM {T} WHERE id > 0", id="where-with-body"),
    pytest.param(f"SELECT id FROM {T} GROUP BY id", id="group-by-with-body"),
    pytest.param(f"SELECT id FROM {T} ORDER BY id", id="order-by-with-body"),
]


@pytest.mark.parametrize("sql", R9_COMPLETE)
def test_r9_complete_clause_still_proves(sql):
    proof = _proof(sql)
    assert proof.verdict == PROVEN and proof.reads
