# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every read of a table with a row exclusion is decided, one read at a time.

A query can read a rule-bearing table more than once: a UNION ALL of per-window
reads, a join or self-join, a ratio to a total in a scalar subquery, an
``IN (SELECT ...)`` or ``EXISTS`` filter. Each of those reads counts rows on its
own, so each must carry the source owner's predicate in its OWN WHERE, bound to
its own alias when it is joined. A predicate in another branch, on another
alias or only in the outer query does not apply to it.

* every read carries it: the query runs and the answer says, once per table,
  that it excluded the owner's rows;
* any read does not: refused once, naming each read that lacks it and the exact
  predicate to AND into that read's WHERE; the identical resend runs and the
  answer says its figures may include those rows;
* a statement that may read the table but cannot be read with certainty fails
  closed the same way (refused once, then disclosed); one that certainly does
  not read it is untouched.

Neutral schema: ``table_x`` (``status``, ``kind``, ``amount``, ``ts``) with a
rule, ``table_y`` (``grade``, ``note``) with its own rule or none. Offline.
"""
from __future__ import annotations

import importlib

import pytest


def _rx():
    return importlib.import_module("agents.row_exclusion")


_LABEL = "draft rows"
_X_CONDITIONS = [{"column": "status", "op": "in", "values": ["draft", "void"]},
                 {"column": "kind", "op": "empty"}]
_Y_CONDITIONS = [{"column": "grade", "op": "not_in", "values": ["a"]}]


def _x():
    return _rx().exclusion_from(_LABEL, _X_CONDITIONS)


def _y():
    return _rx().exclusion_from("old rows", _Y_CONDITIONS)


def _p(qualifier: str = "") -> str:
    return _rx().predicate_sql(_x(), qualifier=qualifier)


def _branch(day: str, pred: bool) -> str:
    where = f"ts >= DATE '{day}' AND amount > 0" + (f" AND {_p()}" if pred else "")
    return f"SELECT '{day}' AS w, COUNT(*) AS n FROM table_x WHERE {where}"


def _decide(sql: str):
    return _rx().decide(sql, "table_x", _x())


# --------------------------------------------------------------------------- #
# The qualified predicate is the one canonical rendering, qualified.
# --------------------------------------------------------------------------- #
def test_the_qualified_predicate_prefixes_each_column_with_the_alias():
    assert _p() == "NOT (COALESCE(status, '') IN ('draft', 'void') AND COALESCE(kind, '') = '')"
    assert _p("b") == ("NOT (COALESCE(b.status, '') IN ('draft', 'void') AND "
                       "COALESCE(b.kind, '') = '')")


# --------------------------------------------------------------------------- #
# A UNION ALL of per-window reads.
# --------------------------------------------------------------------------- #
def test_a_union_of_reads_that_all_carry_the_predicate_runs_with_it():
    sql = " UNION ALL ".join(_branch(d, True) for d in ("2026-07-20", "2026-07-27"))
    found = _decide(sql)
    assert found is not None and found.applied is True


def test_a_union_with_one_branch_missing_is_refused_naming_that_branch():
    sql = f"{_branch('2026-07-20', True)} UNION ALL {_branch('2026-07-27', False)}"
    found = _decide(sql)
    assert found is not None and found.refuse is True
    payload = _rx().RowExclusionError(found).to_dict()
    (read,) = payload["reads"]
    assert "branch 2 of the UNION ALL" in read["read"]
    assert read["predicate"] == _p()
    assert "branch 2 of the UNION ALL" in payload["guidance"]
    assert f"AND {_p()}" in payload["guidance"]
    assert "send the same query again unchanged" in payload["guidance"]


def test_a_predicate_in_another_branch_does_not_count_for_this_one():
    sql = (f"{_branch('2026-07-20', True)} UNION ALL "
           f"SELECT 'b' AS w, COUNT(*) AS n FROM table_x WHERE amount > 0")
    assert _decide(sql).refuse is True


def test_an_intersect_or_except_of_reads_is_decided_per_branch():
    sql = f"{_branch('2026-07-20', True)} EXCEPT {_branch('2026-07-27', True)}"
    assert _decide(sql).applied is True
    sql = f"{_branch('2026-07-20', True)} INTERSECT {_branch('2026-07-27', False)}"
    assert _decide(sql).refuse is True


def test_a_union_of_one_read_and_a_literal_row_is_decided_on_the_read():
    assert _decide(f"{_branch('2026-07-20', True)} UNION ALL SELECT 'y', 0").applied is True
    assert _decide(f"{_branch('2026-07-20', False)} UNION ALL SELECT 'y', 0").refuse is True


# --------------------------------------------------------------------------- #
# Joins.
# --------------------------------------------------------------------------- #
_JOIN = "SELECT COUNT(*) AS n FROM table_x x JOIN table_y y ON x.status = y.grade"


def _yp(qualifier: str = "") -> str:
    return _rx().predicate_sql(_y(), qualifier=qualifier)


def test_a_join_of_two_rule_tables_is_decided_for_each_table():
    both = f"{_JOIN} WHERE {_p('x')} AND {_yp('y')}"
    assert _rx().decide(both, "table_x", _x()).applied is True
    assert _rx().decide(both, "table_y", _y()).applied is True
    one = f"{_JOIN} WHERE {_p('x')}"
    assert _rx().decide(one, "table_x", _x()).applied is True
    found = _rx().decide(one, "table_y", _y())
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert "alias y" in read["read"] and read["predicate"] == _yp("y")


def test_a_join_to_a_table_without_a_rule_needs_the_qualified_predicate():
    sql = "SELECT COUNT(*) AS n FROM table_x x JOIN table_y y ON x.status = y.grade"
    assert _decide(f"{sql} WHERE {_p('x')}").applied is True
    # unqualified in a joined read: not bound to that read, so not proven
    found = _decide(f"{sql} WHERE {_p()}")
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert read["predicate"] == _p("x")
    # a qualifier on the other alias does not count
    assert _decide(f"{sql} WHERE {_p('y')}").refuse is True


def test_a_join_without_an_alias_binds_to_the_table_name():
    sql = ("SELECT COUNT(*) AS n FROM table_x JOIN table_y ON table_x.status = table_y.grade "
           f"WHERE {_p('table_x')}")
    assert _decide(sql).applied is True


def test_a_self_join_needs_the_predicate_on_each_alias():
    sql = "SELECT COUNT(*) AS n FROM table_x a JOIN table_x b ON a.status = b.status"
    assert _decide(f"{sql} WHERE {_p('a')} AND {_p('b')}").applied is True
    found = _decide(f"{sql} WHERE {_p('a')}")
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert "alias b" in read["read"] and read["predicate"] == _p("b")


def test_a_join_to_a_cte_is_a_joined_read():
    sql = ("WITH k AS (SELECT 'draft' AS c) SELECT COUNT(*) AS n FROM table_x t "
           "JOIN k ON t.status = k.c")
    assert _decide(f"{sql} WHERE {_p('t')}").applied is True
    assert _decide(sql).refuse is True


# --------------------------------------------------------------------------- #
# Expression subqueries.
# --------------------------------------------------------------------------- #
def test_a_ratio_to_a_total_in_a_scalar_subquery_needs_it_in_both_reads():
    total = "(SELECT COUNT(*) FROM table_x WHERE amount > 0{})"
    outer = "SELECT kind, COUNT(*) * 1.0 / {} AS share FROM table_x WHERE amount > 0{} GROUP BY 1"
    both = outer.format(total.format(f" AND {_p()}"), f" AND {_p()}")
    assert _decide(both).applied is True
    inner_missing = outer.format(total.format(""), f" AND {_p()}")
    found = _decide(inner_missing)
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert "subquery" in read["read"]
    assert _decide(outer.format(total.format(f" AND {_p()}"), "")).refuse is True


def test_an_in_subquery_read_is_decided_on_its_own_where():
    sql = ("SELECT COUNT(*) AS n FROM table_y WHERE grade IN "
           "(SELECT status FROM table_x WHERE amount > 0{})")
    assert _decide(sql.format(f" AND {_p()}")).applied is True
    found = _decide(sql.format(""))
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert "IN subquery" in read["read"]


def test_an_exists_subquery_read_is_decided_on_its_own_where():
    sql = ("SELECT COUNT(*) AS n FROM table_y y WHERE EXISTS "
           "(SELECT 1 FROM table_x x WHERE x.status = y.grade{})")
    assert _decide(sql.format(f" AND {_p()}")).applied is True
    assert _decide(sql.format("")).refuse is True
    # a correlated predicate on the OUTER alias is not the inner read's
    assert _decide(sql.format(f" AND {_p('y')}")).refuse is True


# --------------------------------------------------------------------------- #
# Predicates outside the read's own WHERE never count.
# --------------------------------------------------------------------------- #
def test_a_pass_through_cte_filtered_later_is_refused_not_silent():
    sql = f"WITH c AS (SELECT * FROM table_x) SELECT COUNT(*) AS n FROM c WHERE {_p()}"
    found = _decide(sql)
    assert found is not None and found.refuse is True
    assert _rx().classify(sql, "table_x", _x()).reason == "conditions_elsewhere"


def test_a_predicate_only_in_the_outer_query_of_a_derived_table_does_not_count():
    sql = f"SELECT COUNT(*) AS n FROM (SELECT * FROM table_x) d WHERE {_p('d')}"
    assert _decide(sql).refuse is True


def test_a_predicate_on_an_alias_of_no_read_does_not_count():
    sql = f"SELECT COUNT(*) AS n FROM table_x t WHERE {_p('other')}"
    assert _decide(sql).refuse is True


def test_the_conditions_measured_in_the_select_list_are_refused_not_silent():
    sql = ("SELECT COUNT(*), SUM(CASE WHEN COALESCE(kind, '') = '' THEN 1 ELSE 0 END) "
           "FROM table_x")
    assert _decide(sql).refuse is True


# --------------------------------------------------------------------------- #
# List expansions are not a second read.
# --------------------------------------------------------------------------- #
def test_a_cross_join_values_reads_no_table_and_is_not_a_join():
    sql = ("SELECT v.w, COUNT(*) AS n FROM table_x CROSS JOIN (VALUES ('a'), ('b')) AS v(w) "
           "WHERE amount > 0{} GROUP BY 1")
    assert _decide(sql.format(f" AND {_p()}")).applied is True
    found = _decide(sql.format(""))
    assert found.refuse is True
    assert _rx().classify(sql.format(""), "table_x", _x()).kind == "missing"


def test_a_cross_join_unnest_stays_decided_as_before():
    sql = ("SELECT e.v, COUNT(*) FROM table_x CROSS JOIN UNNEST(split(kind, ',')) AS e(v) "
           f"WHERE amount > 0 AND {_p()} GROUP BY 1")
    assert _decide(sql).applied is True


# --------------------------------------------------------------------------- #
# A type name holding WITH is not a WITH clause.
# --------------------------------------------------------------------------- #
def test_a_timestamp_with_time_zone_cast_is_a_certain_single_read():
    sql = ("SELECT COUNT(*) FROM table_x WHERE CAST(ts AS timestamp(6) with time zone) "
           "> TIMESTAMP '2026-07-20 00:00:00 UTC'{}")
    assert _rx().classify(sql.format(""), "table_x", _x()).kind == "missing"
    assert _decide(sql.format(f" AND {_p()}")).applied is True


# --------------------------------------------------------------------------- #
# Uncertain statements that may read the table fail closed, once.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM table_x WHERE status = 'open AND {P}",
    "SELECT COUNT(*) FROM table_x /* a /* nested */ comment */",
    "SELECT COUNT(*) FROM table_x WHERE {P}; SELECT 1",
    "SELECT COUNT(*) FROM table_x, table_y",
    "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT * FROM table_x",
])
def test_a_statement_that_may_read_the_table_but_cannot_be_read_is_refused_once(sql):
    found = _decide(sql.replace("{P}", _p()))
    assert found is not None and found.refuse is True
    payload = _rx().RowExclusionError(found).to_dict()
    assert f"AND {_p()}" in payload["guidance"]
    assert "send the same query again unchanged" in payload["guidance"]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM table_y WHERE grade = 'open",
    "SELECT COUNT(*) FROM table_y; SELECT 1",
    "SELECT COUNT(*) FROM table_y",
    "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'table_y'",
])
def test_a_statement_that_certainly_does_not_read_the_table_is_untouched(sql):
    assert _decide(sql) is None
    assert _rx().classify(sql, "table_x", _x()).kind == "not_read"


# --------------------------------------------------------------------------- #
# An Iceberg snapshot read is decided on its real alias, never on "FOR".
# --------------------------------------------------------------------------- #
_SNAP = ("FOR VERSION AS OF 7", "FOR TIMESTAMP AS OF TIMESTAMP '2026-07-20 00:00:00 UTC'")


@pytest.mark.parametrize("snap", _SNAP)
def test_a_joined_snapshot_read_carrying_its_alias_predicate_is_applied(snap):
    sql = (f"SELECT COUNT(*) FROM table_x {snap} x JOIN table_y y ON x.id = y.id "
           f"WHERE {_p('x')}")
    assert _decide(sql).applied is True


@pytest.mark.parametrize("snap", _SNAP)
@pytest.mark.parametrize("alias, qualifier", [(" x", "x"), (" AS x", "x"), ("", "table_x")])
def test_a_joined_snapshot_read_missing_it_is_told_its_real_qualifier(snap, alias, qualifier):
    sql = f"SELECT COUNT(*) FROM table_x {snap}{alias} JOIN table_y y ON {qualifier}.id = y.id"
    found = _decide(sql)
    assert found.refuse is True
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert read["predicate"] == _p(qualifier)
    assert "FOR" not in read["read"] and '"FOR".' not in read["predicate"]
    assert _decide(f"{sql} WHERE {_p(qualifier)}").applied is True


@pytest.mark.parametrize("snap", _SNAP)
def test_a_lone_snapshot_read_takes_a_bare_or_alias_qualified_predicate(snap):
    assert _decide(f"SELECT COUNT(*) FROM table_x {snap} WHERE {_p()}").applied is True
    assert _decide(f"SELECT COUNT(*) FROM table_x {snap} t WHERE {_p('t')}").applied is True
    assert _decide(f"SELECT COUNT(*) FROM table_x {snap} t").refuse is True


@pytest.mark.parametrize("snap", _SNAP)
def test_snapshot_reads_in_union_branches_cte_and_derived_table_are_each_decided(snap):
    branch = f"SELECT COUNT(*) AS n FROM table_x {snap} b WHERE {_p('b')}"
    assert _decide(f"{branch} UNION ALL {branch}").applied is True
    found = _decide(f"{branch} UNION ALL SELECT COUNT(*) AS n FROM table_x {snap} b")
    (read,) = _rx().RowExclusionError(found).to_dict()["reads"]
    assert "branch 2 of the UNION ALL" in read["read"] and "alias b" in read["read"]
    cte = f"WITH c AS (SELECT * FROM table_x {snap} AS s WHERE {_p('s')}) SELECT COUNT(*) FROM c"
    assert _decide(cte).applied is True
    derived = f"SELECT COUNT(*) FROM (SELECT * FROM table_x {snap} s WHERE {_p()}) d"
    assert _decide(derived).applied is True


@pytest.mark.parametrize("snap", [
    "FOR TIMESTAMP AS OF TIMESTAMP '2026-07-20 00:00:00' AT TIME ZONE 'UTC'",
    "FOR VERSION AS OF 7 * 3",
    "FOR TIMESTAMP AS OF current_timestamp - INTERVAL '1 02:03:04' DAY TO SECOND",
])
def test_a_snapshot_expression_it_cannot_bound_fails_closed_never_on_a_guessed_alias(snap):
    sql = (f"SELECT COUNT(*) FROM table_x {snap} x JOIN table_y y ON x.id = y.id "
           f"WHERE {_p('x')}")
    found = _decide(sql)
    assert found is not None and found.refuse is True
    assert found.reason == "uncertain_relations" and not found.sites


@pytest.mark.parametrize("snap", [
    "FOR TIMESTAMP AS OF (TIMESTAMP '2026-07-20 00:00:00' AT TIME ZONE 'UTC')",
    "FOR VERSION AS OF (7 * 3)",
])
def test_a_parenthesised_snapshot_expression_is_read_on_its_alias(snap):
    sql = (f"SELECT COUNT(*) FROM table_x {snap} x JOIN table_y y ON x.id = y.id "
           f"WHERE {_p('x')}")
    assert _decide(sql).applied is True


@pytest.mark.parametrize("alias", ['"FOR"', 'AS "FOR"'])
def test_a_quoted_reserved_alias_carries_its_qualified_predicate(alias):
    sql = (f'SELECT COUNT(*) FROM table_x {alias} JOIN table_y y ON "FOR".id = y.id '
           f'WHERE {_p("FOR")}')
    assert _decide(sql).applied is True


def test_the_apps_own_pinned_piece_is_read_on_its_alias():
    """The exact split pins each piece with ``FOR VERSION AS OF <id>`` right
    after the table name, before the model's alias (``athena_exact_shape``)."""
    from agents.athena_exact_shape import parse_shape, pinned_sql
    sql = f"SELECT kind, COUNT(*) FROM table_x e WHERE amount > 0 AND {_p('e')} GROUP BY kind"
    pinned = pinned_sql(parse_shape(sql), 7)
    assert "table_x FOR VERSION AS OF 7 e" in pinned
    assert _decide(sql).applied is True
    assert _decide(pinned).applied is True


# --------------------------------------------------------------------------- #
# The system-level decision covers every rule-bearing table.
# --------------------------------------------------------------------------- #
def test_decide_all_for_returns_one_read_per_rule_bearing_table():
    rx = _rx()
    rx.reset()
    try:
        rx.remember("sys", {"table_x": _x(), "table_y": _y()})
        sql = f"{_JOIN} WHERE {_p('x')}"
        found = {r.table: r for r in rx.decide_all_for("sys", sql)}
        assert set(found) == {"table_x", "table_y"}
        assert found["table_x"].applied is True and found["table_y"].refuse is True
        assert rx.decide_all_for("sys", "SELECT 1 FROM other_table") == ()
    finally:
        rx.reset()


# --------------------------------------------------------------------------- #
# The disclosure is once per table, and the prompt fact names the per-read rule.
# --------------------------------------------------------------------------- #
def test_the_engine_line_names_each_table_once_however_many_reads():
    sql = " UNION ALL ".join(_branch(d, True) for d in ("2026-07-20", "2026-07-27", "2026-08-03"))
    line = _rx().engine_line([_decide(sql)])
    assert line.count("table_x was read with the source owner's row exclusion") == 1


def test_the_schema_fact_says_every_read_carries_its_own_predicate():
    line = _rx().schema_lines({"table_x": _x()})
    assert "every read" in line and "own WHERE" in line and "alias" in line
