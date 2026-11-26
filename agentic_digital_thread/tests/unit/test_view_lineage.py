# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A view column counts as a base column only when its lineage is proven (offline).

An earlier version of onboarding read foreign keys and primary keys
through one-table views by matching a view column's NAME to the base column's.
A same-named view column is not proof: ``other AS pid`` (aliased),
``other AS pid, pid AS other`` (swapped), ``pid + 0 AS pid`` (computed) or
``pid::bigint AS pid`` (cast) all keep the name with different or changed
values, and each would have become a false key in the agent's prompt and a
``foreign_key_match`` JoinPath in the graph.

``view_lineage.pass_through_columns`` reads the view's own definition
(``pg_get_viewdef``) and proves a column only when its select-list entry is a
bare reference to the same-named column of the one base table, in a query of
the simple shape ``SELECT <list> FROM <table> [WHERE ...]``. Anything it
cannot read proves nothing.

A projection can also change the NUMBER of rows: a set-returning function in
the select list (``generate_series(1, 3) AS n``, ``unnest(...)``, any
user-defined ``RETURNS SETOF`` function, or an operator backed by one) repeats
every base row, so a primary key copied unchanged next to it is no longer
unique. A view whose select list holds anything but column references,
renames and constants therefore proves NOTHING, whatever its other columns:
that is how the reader stays sure every view row is one base row. The strings below are PostgreSQL's own renderings
(captured from PostgreSQL 16 on 2026-10-14; earlier releases qualify each
column with the relation name, which is accepted too).
"""
from __future__ import annotations

import pytest

from agents.view_lineage import pass_through_columns

_COLS = ["cid", "pid", "other", "site"]


def _proved(view_def, cols=_COLS, schema="base", table="c"):
    return pass_through_columns(view_def, cols, schema, table)


# --------------------------------------------------------------------------- #
# Proven: plain copies of the base columns
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("view_def", [
    # SELECT * FROM base.c (PostgreSQL 16 renders single-table columns bare)
    " SELECT cid,\n    pid,\n    other,\n    site\n   FROM base.c;",
    # earlier releases qualify each column with the relation name
    " SELECT c.cid,\n    c.pid,\n    c.other,\n    c.site\n   FROM base.c;",
    # unqualified relation (visible on the search path when rendered)
    " SELECT cid,\n    pid,\n    other,\n    site\n   FROM c;",
    # a row filter keeps every column a copy
    " SELECT cid,\n    pid,\n    other,\n    site\n   FROM base.c\n  WHERE (site = 'X'::text);",
    # an alias on the relation, columns qualified by it
    " SELECT q.cid,\n    q.pid,\n    q.other,\n    q.site\n   FROM base.c q\n"
    "  WHERE (q.site IN ( SELECT c_1.site\n           FROM base.c c_1));",
    " SELECT q.cid, q.pid, q.other, q.site FROM ONLY base.c AS q",
])
def test_a_pass_through_view_proves_every_column(view_def):
    assert _proved(view_def) == set(_COLS)


def test_quoted_names_are_compared_unquoted():
    view_def = ' SELECT "Cid",\n    pid\n   FROM base."Child";'
    assert pass_through_columns(view_def, ["Cid", "pid"], "base", "Child") == {"Cid", "pid"}


# --------------------------------------------------------------------------- #
# Not proven: hostile projections that keep a base column's name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("view_def,cols,proved", [
    # aliased: the view's pid holds the base column other
    (" SELECT cid,\n    other AS pid\n   FROM base.c;", ["cid", "pid"], {"cid"}),
    # swapped
    (" SELECT cid,\n    other AS pid,\n    pid AS other\n   FROM base.c;",
     ["cid", "pid", "other"], {"cid"}),
    # renamed on the way out
    (' SELECT cid AS "Cid",\n    pid\n   FROM base.c;', ["Cid", "pid"], {"pid"}),
    # a constant under a base column's name (one row per base row still)
    (" SELECT cid,\n    1 AS pid\n   FROM base.c;", ["cid", "pid"], {"cid"}),
    (" SELECT cid,\n    'x'::text AS pid\n   FROM base.c;", ["cid", "pid"], {"cid"}),
    (" SELECT cid,\n    NULL::integer AS pid\n   FROM base.c;", ["cid", "pid"], {"cid"}),
])
def test_an_aliased_swapped_or_constant_column_is_not_proven(view_def, cols, proved):
    assert pass_through_columns(view_def, cols, "base", "c") == proved


@pytest.mark.parametrize("view_def,cols", [
    # computed and cast keep the name, not the value or type
    (" SELECT cid,\n    (pid + 0) AS pid\n   FROM base.c;", ["cid", "pid"]),
    (" SELECT cid,\n    (pid)::bigint AS pid\n   FROM base.c;", ["cid", "pid"]),
    (" SELECT cid,\n    abs(pid) AS pid\n   FROM base.c;", ["cid", "pid"]),
    # an expression next to an unchanged key: the key is not proven either
    (" SELECT cid,\n    pid,\n    (now() + '1 day'::interval) AS due\n   FROM base.c;",
     ["cid", "pid", "due"]),
    (" SELECT cid,\n    pid,\n        CASE\n            WHEN (pid > 0) THEN 'a'::text\n"
     "            ELSE 'b'::text\n        END AS kind\n   FROM base.c;", ["cid", "pid", "kind"]),
])
def test_any_expression_in_the_select_list_proves_nothing(view_def, cols):
    assert pass_through_columns(view_def, cols, "base", "c") == set()


@pytest.mark.parametrize("view_def,cols,schema,table", [
    # the review's case: every base row repeats three times
    (" SELECT id,\n    generate_series(1, 3) AS n\n   FROM base.t;", ["id", "n"], "base", "t"),
    (" SELECT t.id,\n    generate_series(1, 3) AS n\n   FROM base.t;", ["id", "n"], "base", "t"),
    # a user-defined set-returning function, qualified and not, quoted and not
    (" SELECT cid,\n    pid,\n    base.child_rows(cid) AS n\n   FROM base.c;",
     ["cid", "pid", "n"], "base", "c"),
    (" SELECT cid,\n    pid,\n    child_rows(cid) AS n\n   FROM base.c;",
     ["cid", "pid", "n"], "base", "c"),
    (' SELECT cid,\n    pid,\n    "ChildRows"(cid) AS n\n   FROM base.c;',
     ["cid", "pid", "n"], "base", "c"),
    (" SELECT cid,\n    pid,\n    unnest(ARRAY[1, 2]) AS n\n   FROM base.c;",
     ["cid", "pid", "n"], "base", "c"),
    # a set-returning call with no output name of its own
    (" SELECT cid,\n    generate_series(1, 2)\n   FROM base.c;",
     ["cid", "generate_series"], "base", "c"),
])
def test_a_set_returning_projection_proves_nothing(view_def, cols, schema, table):
    assert pass_through_columns(view_def, cols, schema, table) == set()


@pytest.mark.parametrize("view_def", [
    # the same table twice: every row repeats, no column keeps its key
    " SELECT c.cid,\n    c.pid\n   FROM base.c\nUNION ALL\n SELECT c.cid,\n    c.pid\n   FROM base.c;",
    " SELECT DISTINCT cid,\n    pid\n   FROM base.c;",
    " SELECT cid,\n    pid\n   FROM base.c\n  GROUP BY cid, pid;",
    " SELECT cid,\n    pid\n   FROM base.c\n LIMIT 10;",
    " SELECT cid,\n    pid\n   FROM base.c\n  ORDER BY cid;",
    " SELECT c.cid,\n    c.pid\n   FROM (base.c\n     JOIN base.p ON ((p.pid = c.pid)));",
    " SELECT c.cid,\n    c.pid\n   FROM base.c, base.p;",
    " WITH x AS (SELECT cid, pid FROM base.c) SELECT cid, pid FROM x;",
    " SELECT x.cid,\n    x.pid\n   FROM ( SELECT c.cid, c.pid FROM base.c) x;",
    " SELECT cid,\n    pid\n   FROM base.other_table;",           # a different relation
    " SELECT cid,\n    pid\n   FROM other_schema.c;",              # a different schema
    " SELECT z.cid,\n    z.pid\n   FROM base.c q;",                # the wrong qualifier
    " SELECT *\n   FROM base.c;",
    " SELECT cid,\n    pid\n   FROM base.c; SELECT 1;",
    " SELECT cid,\n    pid\n   FROM base.c WHERE (cid = 'unterminated);",
    "", "not sql at all", None, 7,
])
def test_a_view_of_any_other_shape_proves_nothing(view_def):
    assert pass_through_columns(view_def, ["cid", "pid"], "base", "c") == set()


@pytest.mark.parametrize("cols", [["cid"], ["cid", "pid", "extra"], ["pid", "cid"], [],
                                  None, "cid,pid", ["cid", 7]])
def test_the_view_columns_must_line_up_with_the_select_list(cols):
    view_def = " SELECT cid,\n    pid\n   FROM base.c;"
    assert pass_through_columns(view_def, cols, "base", "c") == set()
