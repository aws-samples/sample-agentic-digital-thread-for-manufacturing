# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A query that is one statement ending in semicolons is read as that statement.

Measured in two answer evals: the system agent's first call wrote several
correct queries, each filtered to the rows the question counts, and each
ended with ``;``. The scan guard (``athena_scope.scope_sql``) reads any
semicolon token as uncertain, so every one came back ``scope_required``
("simplify the query to a single plain SELECT ... no multiple statements").
On the re-ask the agent dropped a filter together with the semicolon and
counted the wrong rows.

A statement followed only by semicolons (whitespace and comments around them)
is that one statement. Mode A removes the trailing semicolons before the
guard, the connector and the executed-SQL record see the query, and the
result's note says so (the pattern of ``drop_comma_before_cross_join``).
Mode B never rewrites the SQL it is given, so it refuses that query before
anything is submitted and says to send the same statement without them. Any
other semicolon is refused as before. Fully offline (a stubbed connector),
neutral names.
"""
from __future__ import annotations

import json

import pytest

import agents.athena_tools as at
import agents.system_agent as sa


def drop(sql: str) -> tuple[str, int]:
    from agents.athena_scope import drop_trailing_semicolons
    return drop_trailing_semicolons(sql)


_CFG = {"workgroup": "wg_a", "database": "db_a", "region": "us-east-1"}
#: One statement, filtered to the rows it counts.
_STMT = ("SELECT kind, COUNT(*) AS n FROM table_x "
         "WHERE site = 'site-a' AND kind LIKE '%_LINE'{extra} GROUP BY kind ORDER BY n DESC")
_NOTE = "removed the semicolon at the end of the query"
_NOTE_MANY = "removed the semicolons at the end of the query"


@pytest.fixture(autouse=True)
def _clear_caches():
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()
    yield
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sent, statement, removed", [
    ("SELECT a FROM table_x;", "SELECT a FROM table_x", 1),
    ("SELECT a FROM table_x ;", "SELECT a FROM table_x", 1),
    ("SELECT a FROM table_x;;", "SELECT a FROM table_x", 2),
    ("SELECT a FROM table_x ; ; ;\n", "SELECT a FROM table_x", 3),
    ("\nSELECT a\nFROM table_x\nORDER BY a DESC;\n", "\nSELECT a\nFROM table_x\nORDER BY a DESC", 1),
    # Comments after the semicolons, or between them, go with them.
    ("SELECT a FROM table_x; -- done", "SELECT a FROM table_x", 1),
    ("SELECT a FROM table_x; /* end */ ;", "SELECT a FROM table_x", 2),
    ("SELECT a FROM table_x -- last line\n;", "SELECT a FROM table_x", 1),
    # A semicolon in a string literal, quoted name or comment is not a terminator.
    ("SELECT 'a;b' AS s FROM table_x;", "SELECT 'a;b' AS s FROM table_x", 1),
    ('SELECT "col;x" FROM table_x;', 'SELECT "col;x" FROM table_x', 1),
    ("SELECT a /* ; */ FROM table_x -- ;\n;", "SELECT a /* ; */ FROM table_x", 1),
    ("WITH d AS (SELECT a FROM table_x) SELECT COUNT(*) FROM d;",
     "WITH d AS (SELECT a FROM table_x) SELECT COUNT(*) FROM d", 1),
])
def test_one_statement_and_its_trailing_semicolons_is_that_statement(sent, statement, removed):
    assert drop(sent) == (statement, removed)


@pytest.mark.parametrize("sql", [
    "SELECT a FROM table_x",                                    # no semicolon
    "SELECT a FROM table_x -- ;",                               # only in a comment
    "SELECT 'x;' FROM table_x",                                 # only in a literal
    "SELECT a FROM table_x; SELECT b FROM table_y",             # two statements
    "SELECT a FROM table_x; SELECT b FROM table_y;",            # two, each ended
    "SELECT a FROM table_x;; SELECT 1",                         # more SQL after them
    "SELECT a FROM table_x; DROP TABLE table_x",                # more SQL after it
    "SELECT a FROM table_x WHERE b = 1; AND c = 2;",            # inside a statement
    "SELECT (a;) FROM table_x;",                                # inside parentheses
    ";", " ; ; ", "-- only a comment\n;",                       # no statement
    "SELECT a FROM table_x; /* unterminated",                   # uncertain comment
    "SELECT 'unterminated FROM table_x;",                       # uncertain literal
    "SELECT a FROM table_x; -- bare\rcarriage return",          # uncertain line comment
])
def test_any_other_semicolon_leaves_the_sql_as_it_is(sql):
    assert drop(sql) == (sql, 0)


# --------------------------------------------------------------------------- #
# Mode A: the system agent's guarded athena_run_query
# --------------------------------------------------------------------------- #
def _hidden():
    at._record_table_format(at._resolve_config(_CFG), "table_x",
                            {"Parameters": {"table_type": "ICEBERG"}})
    return {"table_x": []}


#: (baked partition map, the predicate the query adds) per kind of table.
_KINDS = {
    "unpartitioned (live discovery)": (lambda: {}, ""),
    "known unpartitioned (baked)": (lambda: {"table_x": []}, ""),
    "hidden partitioned": (_hidden, ""),
    "partitioned, with its predicate": (lambda: {"table_x": ["part_day"]},
                                        " AND part_day = '2026-09-01'"),
    "partitioned, no predicate": (lambda: {"table_x": ["part_day"]}, ""),
}


@pytest.fixture
def connector(monkeypatch):
    """The plain connector, stubbed: every SQL it is given, and a result
    whose executed-SQL record is that SQL (as the real one sets it)."""
    calls: list[str] = []

    def plain(sql, cfg, **_kwargs):
        calls.append(sql)
        return {"columns": ["kind", "n"], "rows": [["A_LINE", "3"]], "row_count": 1,
                "_adt_executed_query": sql}

    monkeypatch.setattr(at, "run_athena_query", plain)
    monkeypatch.setattr(at, "list_partition_columns", lambda *a, **k: [])
    # An auto-scope rewrite would read the recent partitions: answered here
    # so a divergence shows as an applied scope, not as a lookup failure.
    monkeypatch.setattr(at, "build_recent_partition_predicate",
                        lambda *a, **k: "part_day >= '2026-09-01'")
    return calls


def _attempt(sql: str, baked: dict) -> dict:
    return sa._scoped_athena_attempt(sql, at._resolve_config(_CFG), baked, "system-a")


@pytest.mark.parametrize("ending", [";", " ;\n", ";;", "; -- done\n"])
@pytest.mark.parametrize("kind", list(_KINDS))
def test_mode_a_runs_the_statement_without_its_semicolons_and_says_so(connector, kind, ending):
    make, extra = _KINDS[kind]
    baked = make()
    statement = _STMT.format(extra=extra)
    payload = _attempt(statement + ending, baked)
    expected = _attempt(statement, baked)
    # The decision is the one the statement without semicolons gets.
    assert payload.get("error_type") == expected.get("error_type")
    assert payload.get("scope") == expected.get("scope")
    assert (_NOTE_MANY if ending.count(";") > 1 else _NOTE) in payload["note"]
    assert "semicolon" not in str(expected.get("note", ""))
    if "error" in expected:
        assert connector == []
    else:
        # The SQL that ran, and the executed-SQL record, is the statement.
        assert len(connector) == 2 and connector[0] == connector[1]
        assert ";" not in connector[0]
        assert payload["_adt_executed_query"] == expected["_adt_executed_query"]
        assert payload["rows"] == expected["rows"]


def test_mode_a_the_filtered_statement_runs_where_it_was_refused(connector):
    """The measured case: a known-unpartitioned table, one filtered statement
    ending in ``;``. Before, ``scope_required``; now it runs with its filter."""
    sent = _STMT.format(extra="") + ";"
    payload = _attempt(sent, {"table_x": []})
    assert "error" not in payload, payload
    assert connector == [_STMT.format(extra="")]
    assert "kind LIKE '%_LINE'" in payload["_adt_executed_query"]
    assert _NOTE in payload["note"]


def test_mode_a_the_tool_entry_returns_the_run_with_its_note(connector):
    out = json.loads(sa._guarded_athena_call(
        _STMT.format(extra="") + ";", at._resolve_config(_CFG), {"table_x": []},
        "system-a", None))
    assert "error" not in out, out
    assert out["rows"] == [["A_LINE", "3"]]
    assert _NOTE in out["note"]


def test_mode_a_keeps_a_comma_fix_note_beside_it(connector):
    sql = ("SELECT json_extract_scalar(el, '$.id') AS item, COUNT(*) AS n FROM table_x, "
           "CROSS JOIN UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u(el) GROUP BY 1;")
    payload = _attempt(sql, {"table_x": []})
    assert "error" not in payload, payload
    assert _NOTE in payload["note"]
    assert "removed a comma before CROSS JOIN" in payload["note"]
    assert connector == [sql[:-1].replace("table_x, CROSS", "table_x CROSS")]


@pytest.mark.parametrize("sql", [
    "SELECT a FROM table_x; SELECT b FROM table_x",
    "SELECT a FROM table_x WHERE b = 1; AND c = 2;",
    "SELECT a FROM table_x;; SELECT 1",
    "SELECT a FROM table_x; /* unterminated",
])
def test_mode_a_any_other_semicolon_is_refused_as_before(connector, sql):
    payload = _attempt(sql, {"table_x": []})
    assert payload["error_type"] == "scope_required"
    assert "semicolon" not in str(payload.get("note", ""))
    assert connector == []


def test_mode_a_a_statement_without_semicolons_has_no_note(connector):
    payload = _attempt(_STMT.format(extra=""), {"table_x": []})
    assert "error" not in payload
    assert "note" not in payload or "semicolon" not in payload["note"]


# --------------------------------------------------------------------------- #
# Mode B: the orchestrator's own run_query never rewrites the SQL it is given
# (the sidecar stores the tool input as the query that ran), so a statement
# ending in semicolons is refused before anything is submitted, with the fix.
# --------------------------------------------------------------------------- #
_UNPARTITIONED_DESC = "table_x(kind: varchar, site: varchar) (no partition keys)"
_PARTITIONED_DESC = "table_x(kind: varchar, site: varchar, part_day: varchar (partition key))"


def _mode_b(sql: str, tables_desc: str) -> dict:
    return sa._mode_b_athena_query(sql, "db_a", None, dict(_CFG), tables_desc)


@pytest.fixture
def mode_b_ran(monkeypatch):
    calls: list[str] = []

    def exact(sql, cfg, **_kwargs):
        calls.append(sql)
        return {"columns": ["kind", "n"], "rows": [["A_LINE", "3"]], "row_count": 1}

    monkeypatch.setattr(at, "run_athena_query_exact", exact)
    monkeypatch.setattr(at, "list_partition_columns", lambda *a, **k: [])
    return calls


@pytest.mark.parametrize("desc", [_UNPARTITIONED_DESC, _PARTITIONED_DESC])
@pytest.mark.parametrize("ending", [";", " ;; -- done"])
def test_mode_b_refuses_a_statement_ending_in_semicolons_with_the_fix(mode_b_ran, desc, ending):
    payload = _mode_b(_STMT.format(extra=" AND part_day = '2026-09-01'") + ending, desc)
    assert mode_b_ran == []                          # nothing was submitted
    assert payload["error"] == "invalid_request" and payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "TRAILING_SEMICOLON"
    assert "without the semicolon at the end" in payload["guidance"]
    assert "nothing else in it needs to change" in payload["guidance"]
    assert "note" not in payload                     # no rewrite to disclose
    # The guidance holds no SQL of the query.
    blob = json.dumps(payload)
    for absent in ("table_x", "site-a", "_LINE", "GROUP BY", "done"):
        assert absent not in blob, absent


def test_mode_b_runs_the_statement_as_it_was_given(mode_b_ran):
    statement = _STMT.format(extra="")
    payload = _mode_b(statement, _UNPARTITIONED_DESC)
    assert mode_b_ran == [statement] and payload["rows"] == [["A_LINE", "3"]]
    assert "note" not in payload


@pytest.mark.parametrize("sql", [
    "SELECT a FROM table_x; SELECT b FROM table_x",
    "SELECT a FROM table_x WHERE b = 1; AND c = 2;",
])
def test_mode_b_any_other_semicolon_is_refused_as_before(mode_b_ran, sql):
    payload = _mode_b(sql, _UNPARTITIONED_DESC)
    assert payload["error_type"] == "scope_required"
    assert "error_name" not in payload
    assert mode_b_ran == []


def test_only_mode_a_removes_the_semicolons():
    import inspect
    assert "drop_trailing_semicolons" in inspect.getsource(sa._scoped_athena_attempt)
    assert "drop_trailing_semicolons" not in inspect.getsource(sa._mode_b_athena_query)
