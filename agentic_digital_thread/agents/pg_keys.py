# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single-column primary keys of a PostgreSQL schema, read through its views.

Shared by onboarding (internal JoinPaths and the declared join keys it bakes)
and the system agent's aggregate check (``aggregate_guard``), so both read a
relation's own key the same way. The system agent's row-id note
(``row_id_names``) reads single-column UNIQUE columns the same way. A view schema (one pass-through view per
table) carries no constraints of its own, so a view's key is the key of the
one table it reads, and only when the view's definition proves that column
is an unchanged copy in a view with one row per base row
(``view_lineage.pass_through_columns``). A same-named column is not proof.

Read from pg_catalog, which every login can read: information_schema's
``view_table_usage`` and ``view_column_usage`` list only the tables the
current role owns. The SQL takes the schema as a bound parameter. The pure
functions read the rows back and never raise on a malformed row.
"""
from __future__ import annotations

from typing import Any, Iterable

#: The views of one schema that each read exactly ONE table, with that table's
#: oid: a pass-through view (``SELECT * FROM public.t``, the shape of a
#: view-per-table schema) or a row-filtered one. A view over a join or a
#: subquery on another table reads two or more relations and is left out.
#: Which of a one-table view's columns copy a base column unchanged is then
#: proven from the view's own definition (``lineage_from_rows``), never from a
#: matching name.
ONE_TABLE_VIEWS_CTE = """
    WITH one_table_views AS (
        SELECT v.oid AS view_oid, v.relname AS view_name, min(d.refobjid) AS base_oid
        FROM pg_catalog.pg_class v
        JOIN pg_catalog.pg_namespace n ON n.oid = v.relnamespace
        JOIN pg_catalog.pg_rewrite r ON r.ev_class = v.oid
        JOIN pg_catalog.pg_depend d ON d.objid = r.oid
            AND d.classid = 'pg_catalog.pg_rewrite'::regclass
            AND d.refclassid = 'pg_catalog.pg_class'::regclass
            AND d.refobjid <> v.oid
        JOIN pg_catalog.pg_class b ON b.oid = d.refobjid
        WHERE n.nspname = %s AND v.relkind IN ('v', 'm')
        GROUP BY v.oid, v.relname
        HAVING count(DISTINCT d.refobjid) = 1 AND bool_and(b.relkind IN ('r', 'p'))
    )
"""

#: Each one-table view with its base table, its definition as PostgreSQL
#: renders it, and its output columns in order (``lineage_from_rows``).
#: Params: schema.
ONE_TABLE_VIEW_DEFS_SQL = ONE_TABLE_VIEWS_CTE + """
    SELECT o.view_name, bn.nspname, b.relname, pg_catalog.pg_get_viewdef(o.view_oid),
           ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                 WHERE a.attrelid = o.view_oid AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY a.attnum)
    FROM one_table_views o
    JOIN pg_catalog.pg_class b ON b.oid = o.base_oid
    JOIN pg_catalog.pg_namespace bn ON bn.oid = b.relnamespace
    ORDER BY 1
"""  # nosec B608 - two string constants joined; the schema is a %s parameter

#: Single-column primary keys of the schema's own tables (``table``) and of the
#: base tables behind its one-table views (``view``, named by the view and the
#: base column), each with the oid of the table that holds the key. Params:
#: schema twice.
PRIMARY_KEY_COLUMNS_SQL = ONE_TABLE_VIEWS_CTE + """
    SELECT 'view', vw.view_name, a.attname, c.conrelid::text
    FROM pg_catalog.pg_constraint c
    JOIN one_table_views vw ON vw.base_oid = c.conrelid
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'p' AND cardinality(c.conkey) = 1
    UNION
    SELECT 'table', t.relname, a.attname, c.conrelid::text
    FROM pg_catalog.pg_constraint c
    JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE n.nspname = %s AND c.contype = 'p' AND cardinality(c.conkey) = 1
"""  # nosec B608 - two string constants joined; the schema is a %s parameter

#: The single-column, non-primary UNIQUE indexes (a UNIQUE constraint makes
#: one) of the schema's own tables and of the base tables behind its one-table
#: views, as ``(kind, relation, column, declared type)``. A partial or
#: expression index proves no column unique on every row and is left out.
#: Params: schema twice.
_UNIQUE_INDEX = """
    i.indisunique AND NOT i.indisprimary AND i.indisvalid AND i.indnkeyatts = 1
    AND i.indkey[0] > 0 AND i.indpred IS NULL AND i.indexprs IS NULL"""
UNIQUE_COLUMNS_SQL = (
    ONE_TABLE_VIEWS_CTE  # nosec B608 - only string constants are joined; the schema is a %s parameter
    + """
    SELECT 'view', vw.view_name, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
    FROM pg_catalog.pg_index i
    JOIN one_table_views vw ON vw.base_oid = i.indrelid
    JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
    WHERE"""
    + _UNIQUE_INDEX
    + """
    UNION
    SELECT 'table', t.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
    WHERE n.nspname = %s AND"""
    + _UNIQUE_INDEX
    + """
"""
)


def names(row: Any, count: int) -> tuple | None:
    """The first ``count`` cells of a catalog row when each is a non-empty name."""
    if not isinstance(row, (list, tuple)) or len(row) < count:
        return None
    cells = tuple(row[:count])
    return cells if all(isinstance(c, str) and c for c in cells) else None


def lineage_from_rows(rows: Iterable[Any]) -> dict[str, set[str]]:
    """``{view: columns}`` from ``ONE_TABLE_VIEW_DEFS_SQL`` rows: the columns
    each view's definition proves are unchanged copies of the same-named base
    column. A row that cannot be read proves nothing."""
    from .view_lineage import pass_through_columns
    lineage: dict[str, set[str]] = {}
    for row in rows or ():
        found = names(row, 4)
        columns = row[4] if found and len(row) > 4 else None
        if found and isinstance(columns, list):
            lineage[found[0]] = pass_through_columns(found[3], columns, found[1], found[2])
    return lineage


def primary_keys_from_rows(rows: Iterable[Any],
                           lineage: dict[str, set[str]]) -> dict[tuple[str, str], str]:
    """``{(relation, column): key table oid}`` from ``PRIMARY_KEY_COLUMNS_SQL``
    rows: a table's own key, and a view's key where ``lineage`` proves the view
    column is a copy."""
    keyed = [found for found in (names(row, 4) for row in rows or ()) if found]
    return {(table, column): oid for kind, table, column, oid in keyed
            if kind == "table" or (kind == "view" and column in lineage.get(table, ()))}


def relation_keys(primary_keys: dict[tuple[str, str], str]) -> dict[str, str]:
    """``{relation: key column}`` for each relation with exactly one
    single-column primary key in ``primary_keys``."""
    found: dict[str, set[str]] = {}
    for table, column in primary_keys:
        found.setdefault(table, set()).add(column)
    return {table: next(iter(cols)) for table, cols in found.items() if len(cols) == 1}


def unique_columns_from_rows(rows: Iterable[Any],
                             lineage: dict[str, set[str]]) -> dict[tuple[str, str], str]:
    """``{(relation, column): declared type}`` from ``UNIQUE_COLUMNS_SQL``
    rows: a table's own unique column, and a view's where ``lineage`` proves
    the view column is a copy."""
    found: dict[tuple[str, str], str] = {}
    for row in rows or ():
        cells = names(row, 4)
        if cells and (cells[0] == "table"
                      or (cells[0] == "view" and cells[2] in lineage.get(cells[1], ()))):
            found[(cells[1], cells[2])] = cells[3]
    return found
