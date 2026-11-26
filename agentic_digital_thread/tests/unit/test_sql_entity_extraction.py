# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave-5 writer-seam SQL entity extraction (``provenance.sql_statement_entity``).

Offline pins for the bounded, regex-free tokenizer that fixes the live
defect where sidecar entries carried entity ALWAYS ``{'table':'','columns':[]}``:

* table = the primary FROM relation of the FINAL top-level SELECT (schema
  qualifier + quotes stripped; JOINs ⇒ primary relation only);
* columns = SELECT-list names only when EVERY item is enumerable
  (identifiers or ``AS`` aliases) — ``*`` / unaliased expressions ⇒ ``[]``
  honestly;
* discovery statements (information_schema/pg_catalog/bare ``pg_*``) ⇒
  honest empty, and ``is_discovery_statement`` flags them for the writer's
  last-data-bearing-statement pick;
* anything unparseable, non-SELECT, or over-cap ⇒ today's honest ``("", [])``
  — never estimated, never guessed.

Stdlib-pure: the module's exact import pin
(test_provenance_builder.test_module_imports_are_stdlib_plus_query_intelligence_only)
already forbids ``re``/boto3 — these tests only pin behavior.
"""
from __future__ import annotations

import pytest

from agents.provenance import (
    MAX_ENTITY_SQL_CHARS,
    is_discovery_statement,
    sql_statement_entity,
)


# --------------------------------------------------------------------------- #
# Happy-path extraction
# --------------------------------------------------------------------------- #
def test_single_select():
    assert sql_statement_entity(
        "SELECT part_no, qty FROM orders WHERE qty > 5"
    ) == ("orders", ["part_no", "qty"])


def test_with_cte_uses_final_select_relation():
    sql = ("WITH recent AS (SELECT * FROM orders WHERE qty > 1) "
           "SELECT part_no FROM recent ORDER BY part_no")
    assert sql_statement_entity(sql) == ("recent", ["part_no"])


def test_join_yields_primary_table_only():
    sql = ("SELECT o.part_no, l.qty FROM orders o "
           "JOIN order_lines l ON o.id = l.order_id")
    assert sql_statement_entity(sql) == ("orders", ["part_no", "qty"])


def test_schema_qualified_and_quoted_identifiers():
    assert sql_statement_entity(
        'SELECT "Part No" FROM "erp"."Order Details"'
    ) == ("Order Details", ["Part No"])
    assert sql_statement_entity(
        "SELECT a\nFROM sched.orders\nWHERE d > '2020-01-01'"
    ) == ("orders", ["a"])


def test_as_alias_names_expression_columns():
    assert sql_statement_entity(
        "SELECT count(*) AS total FROM orders"
    ) == ("orders", ["total"])


def test_distinct_prefix_and_dedup():
    assert sql_statement_entity(
        "SELECT DISTINCT a, b, a FROM t"
    ) == ("t", ["a", "b"])


def test_subquery_in_where_never_steals_the_relation():
    assert sql_statement_entity(
        "SELECT a FROM t WHERE x IN (SELECT y FROM other)"
    ) == ("t", ["a"])


def test_string_literals_and_comments_are_masked():
    assert sql_statement_entity(
        "SELECT a AS label FROM t WHERE note = 'FROM x'"
    ) == ("t", ["label"])
    assert sql_statement_entity(
        "SELECT a FROM t -- trailing comment FROM z\nWHERE b = 1"
    ) == ("t", ["a"])


# --------------------------------------------------------------------------- #
# Honest-empty paths
# --------------------------------------------------------------------------- #
def test_star_projection_is_honest_empty_columns():
    assert sql_statement_entity("SELECT * FROM orders") == ("orders", [])
    assert sql_statement_entity("SELECT o.* FROM orders o") == ("orders", [])


def test_unaliased_expression_makes_columns_honest_empty():
    assert sql_statement_entity("SELECT qty * 2 FROM orders") == ("orders", [])
    assert sql_statement_entity("SELECT count(*) FROM orders") == ("orders", [])


@pytest.mark.parametrize("sql", [
    "totally not sql ;;;",
    "",
    "   ",
    None,
    123,
    "UPDATE orders SET qty = 1",
    "EXPLAIN SELECT 1",
    "SELECT 1",  # no FROM
    "S" * (MAX_ENTITY_SQL_CHARS + 1),
])
def test_garbage_non_select_and_over_cap_are_honest_empty(sql):
    assert sql_statement_entity(sql) == ("", [])


# --------------------------------------------------------------------------- #
# Discovery statements (information_schema / pg_catalog)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT table_name FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'x'",
    "SELECT relname FROM pg_catalog.pg_class",
    "SELECT relname FROM pg_class",
    "SELECT tablename FROM pg_tables",
])
def test_discovery_statements_flagged_and_honest_empty(sql):
    assert is_discovery_statement(sql) is True
    assert sql_statement_entity(sql) == ("", [])


@pytest.mark.parametrize("sql", [
    "SELECT part_no FROM orders",
    "SELECT a FROM sched.pg_style_named_table",  # qualified user table
    "not sql at all",
    None,
])
def test_data_statements_and_garbage_are_not_discovery(sql):
    assert is_discovery_statement(sql) is False
