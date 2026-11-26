# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A column qualifier must resolve to the read's OWN relation, never to a lone
catalog or namespace component of its dotted name (row-exclusion review F1).

For `s3t.flow.segments`, `s3t` (catalog) and `flow` (namespace) are NOT valid
short qualifiers for the `segments` columns; only the alias, the terminal table
name `segments`, or the full `flow.segments` / `s3t.flow.segments` chain are. A
correlated outer query whose alias collides with `s3t` or `flow` must not be
able to make an unapplied exclusion read as applied. The gap is in the shared
reader, so a 2-part `db.table` with an outer alias equal to `db` is covered too
(Athena benefits as well). Pure/offline.
"""
from agents import row_exclusion as rx
from agents import row_exclusion_read as r

_WANTED = [(c.column, c.op, frozenset(c.values))
           for c in rx.exclusion_from("x", [{"column": "status", "op": "empty"}]).conditions]


def _pred(qual=""):
    return f"NOT (COALESCE({qual}status, '') = '')"


def _classify(sql, table="segments"):
    return r.classify(sql, table, _WANTED).kind


# --- a lone catalog / namespace component is NOT a valid qualifier ----------

def test_terminal_name_qualifier_is_applied():
    assert _classify(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {_pred('segments.')}") == "applied"


def test_full_namespace_table_qualifier_is_applied():
    assert _classify(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {_pred('flow.segments.')}") == "applied"


def test_full_three_part_qualifier_is_applied():
    assert _classify(
        f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {_pred('s3t.flow.segments.')}") == "applied"


def test_lone_namespace_qualifier_is_not_applied():
    # `flow` alone is a namespace, not a valid column qualifier for segments.
    assert _classify(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {_pred('flow.')}") == "missing"


def test_lone_catalog_qualifier_is_not_applied():
    assert _classify(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {_pred('s3t.')}") == "missing"


def test_correlated_outer_alias_cannot_fake_applied():
    # Outer query aliased s3t over flow.other; the subquery over segments is bare
    # and the predicate on the OUTER row (s3t.status). segments is read bare.
    sql = ("SELECT * FROM s3t.flow.other s3t WHERE (SELECT COUNT(*) FROM s3t.flow.segments "
           f"WHERE {_pred('s3t.')}) > 0")
    assert _classify(sql) == "missing"


# --- Athena 2-part parity: outer alias == db name ---------------------------

def test_athena_two_part_lone_db_qualifier_is_not_applied():
    # lake_db is the database (a lone namespace component), not a valid qualifier.
    assert _classify(
        f"SELECT COUNT(*) FROM lake_db.segments WHERE {_pred('lake_db.')}") == "missing"


def test_athena_two_part_terminal_qualifier_is_applied():
    assert _classify(
        f"SELECT COUNT(*) FROM lake_db.segments WHERE {_pred('segments.')}") == "applied"
