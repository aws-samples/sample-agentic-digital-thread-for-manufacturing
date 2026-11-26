# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An Iceberg snapshot clause after a table name is part of the read, not its alias.

``FROM table_x FOR VERSION AS OF 7 x`` reads one snapshot of ``table_x`` under
the alias ``x``. The shared relation reader (``athena_scope.relations``) must
consume ``FOR VERSION AS OF <expr>`` and ``FOR TIMESTAMP AS OF <expr>`` before
it looks for the alias; otherwise the word ``FOR`` is taken as the alias and
every reader that binds a qualifier to a read (the scan guard's partition
proof, the row-exclusion proof, the record key) reads the wrong alias. A
``FOR`` after a table name that is not a readable snapshot clause makes the
statement uncertain (callers fail closed). Neutral names; offline.
"""
from __future__ import annotations

import pytest

from agents.athena_scope import (_where_expression, predicate_guards, relations,
                                 tokenize)


def _rels(sql: str):
    tokens, uncertain = tokenize(sql)
    assert not uncertain
    return tokens, *relations(tokens)


@pytest.mark.parametrize("clause", [
    "FOR VERSION AS OF 7",
    "FOR TIMESTAMP AS OF TIMESTAMP '2026-07-20 00:00:00 UTC'",
    "FOR TIMESTAMP AS OF '2026-07-20 00:00:00'",
    "FOR VERSION AS OF (7)",
    "FOR TIMESTAMP AS OF from_iso8601_timestamp('2026-07-20T00:00:00Z')",
    "FOR TIMESTAMP AS OF current_timestamp - INTERVAL '1' DAY",
    "for version as of 12345678901234",
])
@pytest.mark.parametrize("alias, expected", [
    (" x", "x"), (" AS x", "x"), (' AS "Q"', "Q"), ("", "table_x")])
def test_the_alias_after_a_snapshot_clause_is_the_reads_alias(clause, alias, expected):
    _tokens, found, uncertain = _rels(f"SELECT COUNT(*) FROM table_x {clause}{alias} WHERE 1 = 1")
    assert not uncertain
    (rel,) = found
    assert rel.alias == expected and rel.parts == ("table_x",)


def test_a_snapshot_read_in_a_join_keeps_both_aliases():
    sql = ("SELECT COUNT(*) FROM table_x FOR VERSION AS OF 7 x "
           "JOIN table_y FOR TIMESTAMP AS OF TIMESTAMP '2026-07-20 00:00:00 UTC' AS y "
           "ON x.id = y.id")
    _tokens, found, uncertain = _rels(sql)
    assert not uncertain and [r.alias for r in found] == ["x", "y"]


def test_a_snapshot_read_in_a_set_operation_cte_and_derived_table():
    sql = ("WITH c AS (SELECT * FROM table_x FOR VERSION AS OF 7 a WHERE 1 = 1) "
           "SELECT * FROM c UNION ALL "
           "SELECT * FROM (SELECT * FROM table_x FOR VERSION AS OF 8 b) d")
    _tokens, found, uncertain = _rels(sql)
    assert not uncertain
    assert [(r.parts[-1], r.alias) for r in found] == [("table_x", "a"), ("table_x", "b")]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM table_x FOR VERSION AS 7 x",
    "SELECT * FROM table_x FOR VERSION AS OF x",
    "SELECT * FROM table_x FOR SYSTEM_TIME AS OF TIMESTAMP '2026-07-20 00:00:00 UTC'",
    "SELECT * FROM table_x FOR",
])
def test_an_unreadable_clause_after_a_table_is_uncertain_never_an_alias(sql):
    tokens, _u = tokenize(sql)
    found, uncertain = relations(tokens)
    assert uncertain
    assert all(r.alias.upper() != "FOR" for r in found)


def test_the_scan_guard_binds_a_partition_bound_to_the_snapshot_reads_alias():
    """Another rule than the row exclusion: the partition-scope proof reads a
    qualified bound on a joined read by that read's alias."""
    sql = ("SELECT COUNT(*) FROM table_x FOR VERSION AS OF 7 x JOIN table_y y "
           "ON x.id = y.id WHERE x.day_part = DATE '2026-07-20'")
    tokens, found, uncertain = _rels(sql)
    assert not uncertain
    rel = found[0]
    assert predicate_guards(_where_expression(tokens, rel), rel, ["day_part"], True)


# --------------------------------------------------------------------------- #
# The snapshot expression is certain only where its end is proven.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clause", [
    "FOR TIMESTAMP AS OF TIMESTAMP '2026-07-20 00:00:00' AT TIME ZONE 'UTC'",
    "FOR VERSION AS OF 7 * 3",
    "FOR TIMESTAMP AS OF current_timestamp - INTERVAL '1 02:03:04' DAY TO SECOND",
    "FOR VERSION AS OF 21 / 3",
])
@pytest.mark.parametrize("alias", [" x", ""])
def test_an_expression_whose_end_cannot_be_proven_is_uncertain(clause, alias):
    sql = f"SELECT COUNT(*) FROM table_x {clause}{alias} JOIN table_y y ON 1 = 1"
    tokens, _u = tokenize(sql)
    found, uncertain = relations(tokens)
    assert uncertain
    assert all(r.alias.upper() not in {"AT", "TO", "FOR", "TIME", "ZONE"}
               for r in found if r.parts == ("table_x",))


@pytest.mark.parametrize("clause", [
    "FOR TIMESTAMP AS OF (TIMESTAMP '2026-07-20 00:00:00' AT TIME ZONE 'UTC')",
    "FOR VERSION AS OF (7 * 3)",
])
def test_a_parenthesised_expression_ends_at_its_close(clause):
    _tokens, found, uncertain = _rels(f"SELECT COUNT(*) FROM table_x {clause} x JOIN table_y y "
                                      "ON x.id = y.id")
    assert not uncertain and [r.alias for r in found] == ["x", "y"]


@pytest.mark.parametrize("tail", [
    "", ")", " WHERE 1 = 1", " x", " AS x", ' "x"', " x WHERE 1 = 1", " x, table_y",
    " x CROSS JOIN UNNEST(a) AS u(v)", " x GROUP BY 1", " x UNION ALL SELECT 1",
])
def test_an_expression_followed_by_a_boundary_or_one_alias_is_certain(tail):
    body = f"SELECT * FROM table_x FOR VERSION AS OF 7{tail}"
    sql = f"SELECT * FROM ({body}" if tail == ")" else body
    tokens, uncertain_tokens = tokenize(sql)
    assert not uncertain_tokens
    found, _uncertain = relations(tokens)
    (rel,) = [r for r in found if r.parts == ("table_x",)]
    assert rel.alias in {"x", "table_x"}


def test_two_words_after_the_expression_are_uncertain():
    tokens, _u = tokenize("SELECT * FROM table_x FOR VERSION AS OF 7 x y WHERE 1 = 1")
    assert relations(tokens)[1]


def test_a_read_without_a_snapshot_clause_keeps_its_alias_and_certainty():
    for sql, alias in (("SELECT * FROM table_x x JOIN table_y y ON x.id = y.id", "x"),
                       ("SELECT * FROM table_x AS x WHERE 1 = 1", "x"),
                       ("SELECT * FROM table_x WHERE 1 = 1", "table_x")):
        _tokens, found, uncertain = _rels(sql)
        assert not uncertain and found[0].alias == alias


@pytest.mark.parametrize("alias", ['"FOR"', 'AS "FOR"'])
def test_a_quoted_reserved_word_is_an_alias(alias):
    _tokens, found, uncertain = _rels(f"SELECT * FROM table_x {alias} JOIN table_y y "
                                      'ON "FOR".id = y.id')
    assert not uncertain and [r.alias for r in found] == ["FOR", "y"]
