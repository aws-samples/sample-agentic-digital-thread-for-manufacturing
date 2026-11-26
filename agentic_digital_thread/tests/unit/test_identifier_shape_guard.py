# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PostgreSQL query tool refuses lookups that onboarding measured cannot match (offline).

Workshop rehearsal, 2026-10-18 (Quick lot-total lane): one
system keeps a concept under a same-named column in two tables, each column
holding its own identifier (another shape, no shared value). Onboarding
measures the pair and bakes it into the schema, yet in 8 of 10 asks the system
agent still looked values of one column up in the other (no rows), joined the
two columns to each other (no rows), or typed the other column's form of a
value it had read and queried with that (once it picked the wrong record and
the answer's total was wrong).

The tool now reads the measured pairs and their shapes back from the baked
schema and, before running a SELECT, refuses two things deterministically,
both of which can only find no rows:

1. a string literal compared (``=`` or ``IN``) to a measured column when the
   value has the paired column's shape and not this column's;
2. an equality between the two columns of a measured pair.

Everything else runs exactly as before: no pair or no shapes measured, a
column with no pair, anything the guard cannot parse, LIKE, ranges,
functions, negations, any error inside the guard, and a value in the
column's own shape, whether or not the request or an earlier result held it.

Synthetic forms (``KX-9999`` / ``QZ-9999``); the same behavior on real
PostgreSQL: tests/integration/test_identifier_shape_guard_pg.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from agents import distinct_identifiers as di
from agents import identifier_shape_guard as g
from agents import schema_format
from agents import system_agent as sa

_DETAILS = {
    "consumption": [("consumption_id", "uuid"), ("lot_id", "uuid"),
                    ("ref_code", "character varying")],
    "dispatch_lines": [("line_id", "uuid"), ("ref_code", "character varying"),
                       ("order_id", "uuid"), ("qty", "integer")],
    "orders": [("order_id", "uuid"), ("customer", "text")],
}
_PAIR = {"concept": "unit", "column": "ref_code", "table_a": "consumption",
         "table_b": "dispatch_lines", "read_a": 40, "read_b": 37,
         "shape_a": "KX-9999", "shape_b": "QZ-9999"}
_LEGACY_PAIR = {k: v for k, v in _PAIR.items() if not k.startswith("shape_")}
_BASE = schema_format.format_tables_desc(_DETAILS)
_DESC = schema_format.with_distinct_identifiers(_BASE, [_PAIR])
_LEGACY_DESC = schema_format.with_distinct_identifiers(_BASE, [_LEGACY_PAIR])
_CTX = g.context_from(_DESC)

_WRONG = "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042'"
_RIGHT = "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'QZ-0042'"


def _check(sql):
    return g.check(sql, _CTX)


def _kind(sql):
    out = _check(sql)
    return out and out["identifier_check"]["kind"]


# --------------------------------------------------------------------------- #
# The measured shapes reach the runtime through the baked schema
# --------------------------------------------------------------------------- #

def test_the_baked_entry_carries_the_measured_shapes():
    assert _DESC.endswith(
        "unit: consumption.ref_code and dispatch_lines.ref_code, 40 and 37 values read, "
        "shaped like KX-9999 and QZ-9999.")


def test_the_shapes_parse_back_from_the_baked_schema():
    assert schema_format.distinct_identifier_pairs(_DESC) == [{
        "concept": "unit", "column": "ref_code", "table_a": "consumption",
        "table_b": "dispatch_lines", "shape_a": "KX-9999", "shape_b": "QZ-9999"}]


def test_a_schema_baked_before_shapes_were_measured_gets_no_guard():
    assert schema_format.distinct_identifier_pairs(_LEGACY_DESC) == []
    assert g.context_from(_LEGACY_DESC) is None
    assert g.context_from(_BASE) is None
    assert g.context_from("") is None


@pytest.mark.parametrize("bad", ["KX 9999", "KX-9999(", "e.g.-99", "", None, "KX-9999."])
def test_an_unusable_shape_is_not_rendered(bad):
    out = schema_format.with_distinct_identifiers(_BASE, [{**_PAIR, "shape_b": bad}])
    assert out == _LEGACY_DESC


@pytest.mark.parametrize("value,example", [
    ("WO-20261015-01947", "WO-99999999-99999"),
    ("KX-0042", "KX-9999"),
    ("1f5e047a-fbef-4143-a568-b89be688c163", None),   # rendered only if it round-trips
    ("Z1Y2", "X999"),
])
def test_the_example_has_the_measured_shape(value, example):
    sig = sa._shape_signature(value)
    got = di.shape_example(sig)
    if example is not None:
        assert got == example
    assert got is None or sa._signatures_match(sa._shape_signature(got), sig)


def test_a_shape_with_a_space_or_a_one_character_symbol_has_no_example():
    assert di.shape_example(sa._shape_signature("KX 0042")) is None
    assert di.shape_example(sa._shape_signature("KX-#")) is None


def test_measure_records_each_sides_shape():
    rows = ([["a", f"KX-{i:04d}", False] for i in range(1, 41)]
            + [["b", f"QZ-{i:04d}", False] for i in range(1, 38)])
    cm = [{"concept": "unit", "field": "consumption.ref_code", "role": "identifier"},
          {"concept": "unit", "field": "dispatch_lines.ref_code", "role": "identifier"}]
    out = di.measure(lambda *_a, **_k: {"rows": rows}, "app", cm, _DETAILS)
    assert out["distinct"] == [{**_PAIR, "signature_a": '[["-"],[["prefix","kx"],[4,"digit"]]]',
                                "signature_b": '[["-"],[["prefix","qz"],[4,"digit"]]]'}]


# --------------------------------------------------------------------------- #
# 1. Wrong-form lookup
# --------------------------------------------------------------------------- #

def test_a_value_of_the_paired_form_is_not_looked_up():
    out = _check(_WRONG)
    assert out["status"] == "not_run"
    assert out["identifier_check"] == {
        "kind": "wrong_form", "concept": "unit", "column": "dispatch_lines.ref_code",
        "paired_column": "consumption.ref_code", "values": ["KX-0042"]}
    note = out["note"]
    assert "consumption.ref_code" in note and "dispatch_lines.ref_code" in note
    assert "QZ-9999" in note and "KX-9999" in note


def test_the_result_carries_an_identifier_miss_block_the_agent_protocol_reads():
    block = _check(_WRONG)["identifier_miss"][0]
    token, samples = sa._parse_identifier_miss_block(block)
    assert token == "KX-0042"
    assert "dispatch_lines.ref_code" in block.split("looked_in:", 1)[1].split("\n", 1)[0]
    assert samples, block
    fenced = f"```\n{block}\n```"
    assert sa._strip_false_identifier_miss(fenced) == fenced       # a real miss: kept


@pytest.mark.parametrize("sql", [
    "SELECT qty FROM dispatch_lines WHERE ref_code = 'KX-0042'",
    "SELECT d.qty FROM dispatch_lines AS d WHERE 'KX-0042' = d.ref_code",
    "SELECT dispatch_lines.qty FROM dispatch_lines WHERE dispatch_lines.ref_code = 'KX-0042'",
    "SELECT d.qty FROM app.dispatch_lines d WHERE d.ref_code = 'KX-0042' LIMIT 5",
    'SELECT "d"."qty" FROM "dispatch_lines" "d" WHERE "d"."ref_code" = \'KX-0042\'',
    "SELECT d.qty FROM dispatch_lines d WHERE (d.qty > 1 AND (d.ref_code = 'KX-0042'))",
    "SELECT o.customer FROM orders o WHERE o.order_id IN "
    "(SELECT order_id FROM dispatch_lines WHERE ref_code = 'KX-0042')",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042' AND d.qty > 100",
])
def test_aliased_bare_qualified_and_quoted_lookups_are_caught(sql):
    out = _check(sql)
    assert out["identifier_check"]["kind"] == "wrong_form", sql
    assert out["identifier_check"]["column"] == "dispatch_lines.ref_code"


def test_an_in_list_of_the_paired_form_is_not_looked_up():
    out = _check("SELECT d.qty FROM dispatch_lines d WHERE d.ref_code IN ('KX-0001', 'KX-0002')")
    assert out["identifier_check"]["values"] == ["KX-0001", "KX-0002"]
    assert len(out["identifier_miss"]) == 2


def test_a_mixed_in_list_names_only_the_values_of_the_other_form():
    out = _check("SELECT d.qty FROM dispatch_lines d WHERE d.ref_code IN ('QZ-0001', 'KX-0002')")
    assert out["identifier_check"]["kind"] == "wrong_form"
    assert out["identifier_check"]["values"] == ["KX-0002"]


def test_the_other_direction_is_caught_too():
    out = _check("SELECT c.lot_id FROM consumption c WHERE c.ref_code = 'QZ-0042'")
    assert out["identifier_check"]["column"] == "consumption.ref_code"
    assert out["identifier_check"]["paired_column"] == "dispatch_lines.ref_code"


# --------------------------------------------------------------------------- #
# 2. Join of the pair
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    "SELECT c.lot_id FROM consumption c JOIN dispatch_lines d ON c.ref_code = d.ref_code",
    "SELECT c.lot_id FROM consumption c JOIN dispatch_lines d ON d.ref_code = c.ref_code "
    "WHERE c.ref_code = 'KX-0001'",
    "SELECT c.lot_id FROM consumption c JOIN dispatch_lines d ON d.order_id = c.lot_id "
    "WHERE c.ref_code = d.ref_code",
    "SELECT c.lot_id FROM consumption c WHERE EXISTS "
    "(SELECT 1 FROM dispatch_lines d WHERE d.ref_code = c.ref_code)",
])
def test_an_equality_between_the_two_columns_is_not_run(sql):
    out = _check(sql)
    assert out["identifier_check"]["kind"] == "paired_join", sql
    assert {out["identifier_check"]["column"], out["identifier_check"]["paired_column"]} == {
        "consumption.ref_code", "dispatch_lines.ref_code"}
    assert out["identifier_miss"] == []


def test_a_measured_column_compared_with_an_unmeasured_one_runs():
    assert _check("SELECT 1 FROM dispatch_lines d JOIN orders o ON d.ref_code = o.customer") is None


# --------------------------------------------------------------------------- #
# Only a required conjunct counts
# --------------------------------------------------------------------------- #
# A judged comparison stops the query only when, if it is false, no row can
# come back: a WHERE, HAVING or inner-join ON joined to its clause by AND
# alone, in the statement itself or in an IN / EXISTS subquery that is
# itself such a conjunct. Anything else can still return correct rows.

_DEAD = "d.ref_code = 'KX-0042'"
_PAIR_EQ = "c.ref_code = d.ref_code"


@pytest.mark.parametrize("sql", [
    f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} OR d.qty > 100",
    f"SELECT d.qty FROM dispatch_lines d WHERE d.qty > 100 OR {_DEAD}",
    "SELECT 1 FROM consumption WHERE ref_code = 'KX-0042' "
    "UNION ALL SELECT 2 FROM dispatch_lines WHERE ref_code = 'KX-0042'",
    "SELECT 1 FROM consumption WHERE ref_code = 'QZ-0042' "
    "UNION SELECT 2 FROM dispatch_lines WHERE ref_code = 'KX-0042'",
    f"SELECT c.lot_id FROM consumption c LEFT JOIN dispatch_lines d ON {_PAIR_EQ}",
    f"SELECT c.lot_id FROM consumption c LEFT OUTER JOIN dispatch_lines d ON {_PAIR_EQ} "
    "WHERE d.order_id IS NULL",
    f"SELECT d.qty FROM consumption c RIGHT JOIN dispatch_lines d ON {_PAIR_EQ}",
    f"SELECT d.qty FROM consumption c FULL OUTER JOIN dispatch_lines d ON {_PAIR_EQ}",
    f"SELECT d.qty FROM consumption c JOIN dispatch_lines d ON {_PAIR_EQ} "
    "RIGHT JOIN orders o ON o.order_id = d.order_id",
    f"SELECT CASE WHEN {_DEAD} THEN 1 ELSE 0 END FROM dispatch_lines d",
    f"SELECT d.qty FROM dispatch_lines d WHERE d.qty > 1 AND ({_DEAD} OR d.qty < 0)",
    f"SELECT d.qty FROM dispatch_lines d WHERE NOT {_DEAD}",
    f"SELECT d.qty FROM dispatch_lines d WHERE d.qty > 1 AND NOT ({_DEAD} AND d.qty > 2)",
    "SELECT o.customer FROM orders o WHERE o.order_id NOT IN "
    "(SELECT order_id FROM dispatch_lines WHERE ref_code = 'KX-0042')",
    "SELECT o.customer FROM orders o WHERE NOT EXISTS "
    f"(SELECT 1 FROM dispatch_lines d WHERE {_DEAD})",
    "SELECT o.customer, (SELECT MAX(d.qty) FROM dispatch_lines d WHERE "
    f"{_DEAD}) AS m FROM orders o",
    "SELECT o.customer FROM orders o WHERE o.order_id IN "
    f"(SELECT order_id FROM dispatch_lines d WHERE {_DEAD}) OR o.customer = 'x'",
    "SELECT o.customer FROM orders o WHERE o.order_id IN "
    f"(SELECT order_id FROM dispatch_lines d WHERE {_DEAD} OR d.qty > 1)",
    "SELECT o.customer FROM orders o WHERE o.order_id = ANY "
    f"(SELECT order_id FROM dispatch_lines d WHERE {_DEAD})",
    f"SELECT x.qty FROM (SELECT qty FROM dispatch_lines d WHERE {_DEAD}) x",
    f"WITH x AS (SELECT qty FROM dispatch_lines d WHERE {_DEAD}) SELECT qty FROM x",
    f"SELECT d.qty, {_DEAD} AS hit FROM dispatch_lines d",
    f"SELECT d.qty FROM dispatch_lines d WHERE d.qty > 1 OR d.qty < 0 AND {_DEAD}",
    f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} AND left(d.ref_code, 2) = 'QZ' "
    "OR d.qty > 1",
    f"SELECT d.qty FROM dispatch_lines d WHERE CASE WHEN d.qty > 1 THEN {_DEAD} END",
    f"SELECT d.qty FROM dispatch_lines d WHERE FALSE BETWEEN FALSE AND ({_DEAD})",
    f"SELECT d.qty FROM dispatch_lines d WHERE FALSE BETWEEN SYMMETRIC FALSE AND ({_DEAD})",
    f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} AND CASE WHEN d.qty > 1 THEN TRUE END "
    "OR d.qty > 100",
])
def test_a_comparison_that_is_not_a_required_conjunct_runs(sql):
    assert _check(sql) is None, sql


@pytest.mark.parametrize("sql,kind", [
    (f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} AND (d.qty > 1 OR d.qty < 0)",
     "wrong_form"),
    (f"SELECT d.qty FROM dispatch_lines d WHERE ((d.qty > 1) AND ({_DEAD}))", "wrong_form"),
    (f"SELECT c.lot_id FROM consumption c INNER JOIN dispatch_lines d ON {_PAIR_EQ}",
     "paired_join"),
    (f"SELECT c.lot_id FROM consumption c JOIN dispatch_lines d ON d.qty > 1 AND {_PAIR_EQ} "
     "LEFT JOIN orders o ON o.order_id = d.order_id", "paired_join"),
    ("SELECT c.lot_id FROM consumption c LEFT JOIN dispatch_lines d ON c.lot_id = d.order_id "
     f"WHERE {_PAIR_EQ}", "paired_join"),
    ("SELECT o.customer FROM orders o WHERE o.customer = 'x' AND o.order_id IN "
     f"(SELECT order_id FROM dispatch_lines d WHERE d.qty > 1 AND {_DEAD})", "wrong_form"),
    ("SELECT o.customer FROM orders o WHERE EXISTS "
     f"(SELECT 1 FROM dispatch_lines d WHERE d.order_id = o.order_id AND {_DEAD})",
     "wrong_form"),
    ("SELECT o.customer FROM orders o WHERE o.order_id IN (SELECT d.order_id FROM "
     "dispatch_lines d WHERE EXISTS (SELECT 1 FROM consumption c WHERE "
     f"{_PAIR_EQ}))", "paired_join"),
    (f"SELECT d.ref_code, SUM(d.qty) FROM dispatch_lines d GROUP BY d.ref_code "
     f"HAVING {_DEAD}", "wrong_form"),
    (f"WITH x AS (SELECT 1 AS n) SELECT d.qty FROM dispatch_lines d WHERE {_DEAD}",
     "wrong_form"),
    # An OR or AND inside a CASE beside the comparison is not on its path to the clause.
    (f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} AND CASE WHEN d.qty > 100 "
     "OR d.qty < 0 THEN TRUE ELSE FALSE END", "wrong_form"),
    ("SELECT d.qty FROM dispatch_lines d WHERE CASE WHEN d.qty > 100 OR d.qty < 0 "
     f"THEN TRUE ELSE FALSE END AND {_DEAD}", "wrong_form"),
    ("SELECT d.qty FROM dispatch_lines d WHERE CASE WHEN d.qty > 1 THEN CASE WHEN "
     f"d.qty > 2 OR d.qty < 0 THEN TRUE END END AND {_DEAD}", "wrong_form"),
    # The AND that closes a BETWEEN leaves the AND around it boolean.
    (f"SELECT d.qty FROM dispatch_lines d WHERE {_DEAD} AND d.qty BETWEEN 1 AND 5",
     "wrong_form"),
    (f"SELECT d.qty FROM dispatch_lines d WHERE d.qty BETWEEN 1 AND 5 AND {_DEAD}",
     "wrong_form"),
    ("SELECT d.qty FROM dispatch_lines d WHERE d.qty BETWEEN CASE WHEN d.qty > 1 AND "
     f"d.qty < 9 THEN 1 ELSE 0 END AND 5 AND {_DEAD}", "wrong_form"),
])
def test_a_required_conjunct_still_stops_the_query(sql, kind):
    out = _check(sql)
    assert out and out["identifier_check"]["kind"] == kind, sql


# --------------------------------------------------------------------------- #
# Everything else runs as today
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    _RIGHT,
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code LIKE 'KX-%'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code > 'KX-0001'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code BETWEEN 'KX-0001' AND 'KX-0009'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code <> 'KX-0001'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code NOT IN ('KX-0001')",
    "SELECT d.qty FROM dispatch_lines d WHERE NOT d.ref_code = 'KX-0001'",
    "SELECT d.qty FROM dispatch_lines d WHERE NOT (d.qty > 1 OR d.ref_code = 'KX-0001')",
    "SELECT d.qty FROM dispatch_lines d WHERE upper(d.ref_code) = 'KX-0001'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-' || 'X'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0001'::text",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = E'KX-0001'",
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code IN ('KX-0001', d.ref_code)",
    "SELECT SUM(CASE WHEN d.ref_code = 'KX-0001' THEN 1 END) FROM dispatch_lines d",
    "SELECT o.customer FROM orders o WHERE o.customer = 'KX-0042'",
    "SELECT c.lot_id FROM consumption c WHERE c.lot_id = 'KX-0042'",
    'SELECT 1 FROM dispatch_lines d WHERE d."Ref_Code" = \'KX-0042\'',
    # bare column, both tables in scope: PostgreSQL would refuse it as ambiguous
    "SELECT 1 FROM consumption c JOIN dispatch_lines d ON d.order_id = c.lot_id "
    "WHERE ref_code = 'KX-0042'",
    # bare column beside a derived table the guard cannot see into
    "SELECT 1 FROM dispatch_lines JOIN (SELECT order_id FROM orders) o USING (order_id) "
    "WHERE ref_code = 'KX-0042'",
    # comma join: the second table is not read, so the bare column is not resolved
    "SELECT 1 FROM orders o, dispatch_lines WHERE ref_code = 'KX-0042'",
    # an alias that names two different tables in two scopes is not resolved
    "SELECT 1 FROM consumption x WHERE x.ref_code = 'KX-1' "
    "UNION SELECT 1 FROM dispatch_lines x WHERE x.ref_code = 'KX-1'",
])
def test_queries_the_guard_does_not_judge_run(sql):
    assert _check(sql) is None, sql


@pytest.mark.parametrize("sql", [
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042",      # open literal
    "SELECT d.qty FROM dispatch_lines d WHERE (d.ref_code = 'KX-0042'",    # open paren
    "SELECT d.qty FROM dispatch_lines d WHERE d.ref_code = 'KX-0042' /* x",
    "",
])
def test_sql_the_guard_cannot_parse_runs(sql):
    assert _check(sql) is None


def test_no_shape_data_means_no_guard():
    assert g.guard_query(_WRONG, None) is None


def test_a_guard_error_is_logged_and_never_blocks(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(g, "check", boom)
    with caplog.at_level(logging.WARNING, logger=g.logger.name):
        assert g.guard_query(_WRONG, _CTX) is None
    assert "IDENTIFIER_SHAPE_GUARD" in caplog.text


def test_the_engine_names_no_dataset():
    source = Path(g.__file__).read_text(encoding="utf-8")
    for word in ("batch", "batch_id", "shipment", "shipments", "shipment_items",
                 "lot_mes_tracking", "work_order", "erp", "mes"):
        assert not re.search(rf"\b{word}\b", source, re.IGNORECASE), word
    assert not re.search(r"\b(WO|RUN)-", source)


# --------------------------------------------------------------------------- #
# Wiring: the system agent's PostgreSQL tool
# --------------------------------------------------------------------------- #

class _Cursor:
    def __init__(self, columns, rows):
        self.columns, self.rows, self.statements = columns, rows, []
        self.description = None
        self._it = iter(())

    def execute(self, sql, *_a, **_k):
        self.statements.append(str(sql))
        if str(sql).lstrip().upper().startswith("SELECT"):
            self.description = [(c,) for c in self.columns]
            self._it = iter([tuple(r) for r in self.rows])

    def fetchone(self):
        return next(self._it, None)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def set_session(self, **_k):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


def _tool(monkeypatch, desc=_DESC, columns=("ref_code",), rows=(("KX-0042",),)):
    cursor = _Cursor(list(columns), [list(r) for r in rows])
    connects = []

    def connect(**kw):
        connects.append(kw)
        return _Conn(cursor)

    monkeypatch.setattr(sa.pgdriver, "connect", connect)
    tools, _desc = sa._system_tools("sys-a", "app", desc, {"host": "h"}, "PostgreSQL", None,
                                    concept_items=[])
    return tools[0], connects


def _call(tool, sql, invocation_state=None):
    """Call ``tool`` the way a Strands agent does: ``stream`` with the
    invocation's state (a context is injected only if the tool asks for one)."""
    state = invocation_state if invocation_state is not None else {}
    state.setdefault("agent", None)
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": {"sql": sql}}

    async def run():
        last = None
        async for event in tool.stream(use, state):
            last = event
        return last

    result = asyncio.run(run()).tool_result
    assert result["status"] == "success", result
    return json.loads(result["content"][0]["text"])


class _Stop(Exception):
    pass


def _in_one_invocation(monkeypatch, tool, question, *sqls):
    """Run ``sqls`` through ``tool`` inside one Mode A invocation of
    ``question``: the runtime builds the invocation's state and every tool
    call gets that same state, as in a live ask."""
    outs = []

    def agent(_prompt, invocation_state=None, **_kw):
        for sql in sqls:
            outs.append(_call(tool, sql, invocation_state))
        raise _Stop

    monkeypatch.setattr("agents.config.response_integrity_mode", lambda: "report-only")
    with pytest.raises(_Stop):
        sa._invoke_mode_a_agent_with_state(agent, question)
    return outs


def test_the_tool_does_not_run_a_wrong_form_lookup(monkeypatch):
    tool, connects = _tool(monkeypatch)
    out = _call(tool, _WRONG, {})
    assert out["status"] == "not_run" and out["identifier_check"]["kind"] == "wrong_form"
    assert connects == []


def test_the_tool_runs_a_correct_query(monkeypatch):
    tool, connects = _tool(monkeypatch)
    out = _call(tool, _RIGHT, {})
    assert out["rows"] == [["KX-0042"]] and len(connects) == 1


def test_the_tool_without_shapes_runs_every_query(monkeypatch):
    tool, connects = _tool(monkeypatch, desc=_LEGACY_DESC)
    out = _call(tool, _WRONG, {})
    assert out["rows"] == [["KX-0042"]] and len(connects) == 1


def test_the_tool_called_without_an_agent_runs_both_checks(monkeypatch):
    tool, connects = _tool(monkeypatch)
    assert json.loads(tool._tool_func(sql=_WRONG))["status"] == "not_run"
    assert json.loads(tool._tool_func(sql=_RIGHT))["rows"] == [["KX-0042"]]
    assert len(connects) == 1


def test_a_guard_error_runs_the_query(monkeypatch):
    tool, connects = _tool(monkeypatch)
    monkeypatch.setattr(g, "check", lambda *_a, **_k: 1 / 0)
    assert _call(tool, _WRONG, {})["rows"] == [["KX-0042"]] and len(connects) == 1


def test_the_tool_schema_still_shows_only_the_sql_argument(monkeypatch):
    tool, _connects = _tool(monkeypatch)
    assert set(tool.tool_spec["inputSchema"]["json"]["properties"]) == {"sql"}


# --------------------------------------------------------------------------- #
# A value in the column's own shape runs, whoever wrote it
# --------------------------------------------------------------------------- #
# A value of the column's own shape can match, so the guard never refuses it,
# also when neither the request nor an earlier result held it (the agent may
# have edited a value of the paired form into it). A live check found such
# edits were often right: a query refused as an edit returned rows for the
# same values in a later query, and the refusal also dropped the record the
# question asked about and its join.

_READ = ("SELECT ref_code FROM consumption WHERE lot_id = "
         "'00000000-0000-0000-0000-000000000001'")


def test_a_value_edited_from_the_paired_form_runs(monkeypatch):
    tool, connects = _tool(monkeypatch)
    outs = _in_one_invocation(monkeypatch, tool, "Which orders took unit QZ-0007?",
                              _READ, _RIGHT)
    assert outs[1].get("status") != "not_run", outs[1]
    assert outs[1]["rows"] == [["KX-0042"]] and len(connects) == 2


def test_an_in_list_with_an_edited_value_runs(monkeypatch):
    tool, connects = _tool(monkeypatch)
    sql = "SELECT qty FROM dispatch_lines WHERE ref_code IN ('QZ-0007', 'QZ-0042')"
    outs = _in_one_invocation(monkeypatch, tool, "Units QZ-0007 and QZ-0008", _READ, sql)
    assert outs[1].get("status") != "not_run", outs[1]
    assert len(connects) == 2


def test_an_edited_value_runs_in_an_or_branch_and_alone(monkeypatch):
    tool, connects = _tool(monkeypatch)
    outs = _in_one_invocation(monkeypatch, tool, "Which orders took unit QZ-0007?", _READ,
                              f"{_RIGHT} OR d.qty > 100", _RIGHT)
    assert [o.get("status") for o in outs] == [None, None, None], outs
    assert len(connects) == 3


@pytest.mark.parametrize("built", [
    "REPLACE(c.ref_code, 'KX-', 'QZ-')",
    "CASE WHEN c.ref_code LIKE 'KX-%' THEN replace(c.ref_code, 'KX-', 'QZ-') END",
    "'QZ-' || split_part(c.ref_code, '-', 2)",
    "concat('QZ-', substring(c.ref_code FROM 4))",
])
def test_a_value_the_query_built_by_rewriting_text_runs(monkeypatch, built):
    """A rehearsal ask had the database run the edit itself (a REPLACE in the
    select list) and then used the output: that lookup runs."""
    tool, connects = _tool(monkeypatch, columns=("ref_code", "other_form"),
                           rows=(("KX-0042", "QZ-0042"),))
    outs = _in_one_invocation(monkeypatch, tool, "Which orders took unit QZ-0007?",
                              f"SELECT c.ref_code, {built} AS other_form FROM consumption c",
                              _RIGHT)
    assert outs[1].get("status") != "not_run", outs[1]
    assert len(connects) == 2


@pytest.mark.parametrize("question,rows", [
    ("Which orders took unit QZ-0042?", (("KX-0042",),)),             # given in the request
    ("Which orders took KX-0042?", (("KX-0042",), ("QZ-0042",))),     # returned earlier
    ("Which orders?", (("KX-0041",),)),                               # no paired source
])
def test_a_value_of_the_columns_own_form_runs(monkeypatch, question, rows):
    tool, connects = _tool(monkeypatch, rows=rows)
    outs = _in_one_invocation(monkeypatch, tool, question, _READ, _RIGHT)
    assert outs[1]["rows"] == [list(r) for r in rows] and len(connects) == 2


def test_a_wrong_form_lookup_is_still_refused_inside_an_invocation(monkeypatch):
    tool, connects = _tool(monkeypatch)
    outs = _in_one_invocation(monkeypatch, tool, "Which orders took unit QZ-0007?",
                              _READ, _WRONG)
    assert outs[1]["identifier_check"]["kind"] == "wrong_form"
    assert len(connects) == 1
