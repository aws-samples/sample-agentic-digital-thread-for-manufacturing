# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Timestamp-versus-date SQL hygiene in the Athena connector.

A TIMESTAMP column compared to a DATE literal (or a date-only string) with
``=``, ``<=``, ``>`` or ``BETWEEN`` compares at midnight: ``ts = DATE 'd'``
matches only rows stamped exactly 00:00:00 and ``ts <= DATE 'd'`` drops the
whole last day, while the query still claims the asked day. Measured on a
rehearsal account, a count written that way returned a small fraction of the true total.
The connector now refuses such a query before it runs, with a message that
gives the correct half-open form; ``>=`` and ``<`` with a date stay allowed
(they are the correct half-open bounds), and a column whose type is unknown
is never refused.

Offline: the Athena client is a fake whose catalog read returns the types.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from agents.athena_tools import _tool_error, make_athena_tools, run_athena_query

CFG = {"workgroup": "wg", "database": "db", "region": "us-east-1"}


class _Athena:
    """Catalog read plus a query path that records every started query."""

    def __init__(self, columns=None, tables=None):
        self.columns = columns if columns is not None else [
            {"Name": "ts_col", "Type": "timestamp(6)"},
            {"Name": "tz_col", "Type": "timestamp(3) with time zone"},
            {"Name": "day_col", "Type": "date"},
            {"Name": "status", "Type": "varchar"},
        ]
        #: ``{table: columns}``; ``None`` gives every table the default columns.
        self.tables = tables
        self.started = []
        self.metadata_reads = 0

    def get_table_metadata(self, **kw):
        self.metadata_reads += 1
        columns = self.columns
        if self.tables is not None:
            if kw.get("TableName") not in self.tables:
                raise RuntimeError("EntityNotFoundException: table not found")
            columns = self.tables[kw["TableName"]]
        return {"TableMetadata": {"Columns": columns, "PartitionKeys": []}}

    def start_query_execution(self, **kw):
        self.started.append(kw["QueryString"])
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **_kw):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "n"}]},
                              "Rows": [{"Data": [{"VarCharValue": "n"}]},
                                       {"Data": [{"VarCharValue": "7"}]}]}}


def _run(sql, client):
    return run_athena_query(sql, CFG, client=client, source_system="sys-a")


@pytest.mark.parametrize("predicate", [
    "ts_col = DATE '2026-08-20'",
    "ts_col <= DATE '2026-08-20'",
    "ts_col > DATE '2026-08-20'",
    "ts_col BETWEEN DATE '2026-08-19' AND DATE '2026-08-20'",
    "ts_col NOT BETWEEN DATE '2026-08-19' AND DATE '2026-08-20'",
    "t.ts_col = DATE '2026-08-20'",
    '"ts_col" <= DATE \'2026-08-20\'',
    "tz_col = DATE '2026-08-20'",
    "ts_col = '2026-08-20'",
    "DATE '2026-08-20' >= ts_col",
    "DATE '2026-08-20' = ts_col",
    "DATE '2026-08-20' < ts_col",
    "status = 'x' AND ts_col <= date '2026-08-20'",
])
def test_a_midnight_comparison_is_refused_before_it_runs(predicate):
    client = _Athena()
    with pytest.raises(Exception) as err:
        _run(f"SELECT count(*) FROM table_x t WHERE {predicate}", client)
    assert client.started == []
    payload = _tool_error(err.value)
    assert payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "TIMESTAMP_COMPARED_TO_DATE"
    guidance = payload["guidance"]
    assert "midnight" in guidance and "RESOLVED TIME WINDOWS" in guidance
    assert "DATE '2026-08-20'" in guidance and "DATE '2026-08-21'" in guidance


@pytest.mark.parametrize("predicate", [
    "ts_col >= DATE '2026-08-20' AND ts_col < DATE '2026-08-21'",
    "ts_col >= DATE '2026-08-20'",
    "ts_col < DATE '2026-08-21'",
    "DATE '2026-08-20' <= ts_col",
    "DATE '2026-08-21' > ts_col",
    "day_col = DATE '2026-08-20'",
    "day_col <= DATE '2026-08-20'",
    "ts_col = TIMESTAMP '2026-08-20 00:00:00'",
    "ts_col BETWEEN DATE '2026-08-20' AND TIMESTAMP '2026-08-20 23:59:59'",
    "date(ts_col) = DATE '2026-08-20'",
    "CAST(ts_col AS DATE) = DATE '2026-08-20'",
    "ts_col <= DATE '2026-08-20' + INTERVAL '1' DAY",
    "status = '2026-08-20'",
    "unknown_col = DATE '2026-08-20'",
])
def test_allowed_comparisons_run(predicate):
    client = _Athena()
    out = _run(f"SELECT count(*) FROM table_x WHERE {predicate}", client)
    assert out["rows"] == [["7"]] and len(client.started) == 1


def test_an_unknown_column_type_is_never_refused():
    client = _Athena()
    client.get_table_metadata = None  # the catalog read is unavailable
    out = _run("SELECT count(*) FROM table_x WHERE ts_col = DATE '2026-08-20'", client)
    assert len(client.started) == 1 and out["rows"] == [["7"]]


def test_a_select_alias_of_the_same_name_is_not_read_as_the_column():
    client = _Athena()
    sql = ("SELECT * FROM (SELECT CAST(ts_col AS DATE) AS ts_col FROM table_x) s "
           "WHERE ts_col = DATE '2026-08-20'")
    _run(sql, client)
    assert len(client.started) == 1


TS = {"Name": "ts_col", "Type": "timestamp(6)"}
ID = {"Name": "id", "Type": "bigint"}
TWO_TABLES = {"table_x": [TS, ID], "table_y": [ID, {"Name": "status", "Type": "varchar"}],
              "table_z": [TS, ID]}
DERIVED = "(SELECT CAST(ts_col AS DATE) ts_col, id FROM table_x)"


@pytest.mark.parametrize("sql", [
    # an implicit projection alias in a derived table (no AS)
    f"SELECT count(*) FROM {DERIVED} s WHERE ts_col = DATE '2026-08-20'",
    f"SELECT count(*) FROM {DERIVED} s WHERE s.ts_col = DATE '2026-08-20'",
    f"SELECT count(*) FROM {DERIVED} AS s WHERE s.ts_col <= DATE '2026-08-20'",
    # a CTE, a CTE with a column list, and a CTE that shadows a base table name
    "WITH d AS (SELECT CAST(ts_col AS DATE) ts_col FROM table_x) "
    "SELECT count(*) FROM d WHERE ts_col = DATE '2026-08-20'",
    "WITH d (ts_col) AS (SELECT CAST(ts_col AS DATE) FROM table_x) "
    "SELECT count(*) FROM d WHERE d.ts_col = DATE '2026-08-20'",
    "WITH table_x AS (SELECT CAST(ts_col AS DATE) ts_col FROM table_x) "
    "SELECT count(*) FROM table_x WHERE ts_col = DATE '2026-08-20'",
    # a derived table next to a base table: an unqualified name is not tied to either
    f"SELECT count(*) FROM table_y y JOIN {DERIVED} s ON y.id = s.id "
    "WHERE ts_col = DATE '2026-08-20'",
    # a column list on a table alias renames the columns
    "SELECT count(*) FROM table_x AS t (ts_col, id) WHERE t.ts_col = DATE '2026-08-20'",
    # another database's table: its columns are not this catalog read's
    "SELECT count(*) FROM otherdb.table_x WHERE ts_col = DATE '2026-08-20'",
    # two base tables both carry the column: the reference is ambiguous
    "SELECT count(*) FROM table_x, table_z WHERE ts_col = DATE '2026-08-20'",
    # a qualifier from an outer scope (correlated) is not resolved here
    "SELECT count(*) FROM table_x t WHERE EXISTS (SELECT 1 FROM table_y u "
    "WHERE u.id = t.id AND t.ts_col = DATE '2026-08-20')",
    # a qualifier that names no relation in scope
    "SELECT count(*) FROM table_x t WHERE q.ts_col = DATE '2026-08-20'",
])
def test_a_reference_not_tied_to_a_base_timestamp_column_passes(sql):
    client = _Athena(tables=TWO_TABLES)
    out = _run(sql, client)
    assert len(client.started) == 1 and out["rows"] == [["7"]]


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM table_x t JOIN table_y u ON t.id = u.id "
    "WHERE t.ts_col = DATE '2026-08-20'",
    "SELECT count(*) FROM table_x t JOIN table_y u ON t.id = u.id "
    "WHERE ts_col = DATE '2026-08-20'",
    "SELECT count(*) FROM db.table_x WHERE ts_col <= DATE '2026-08-20'",
    "SELECT count(*) FROM (SELECT id FROM table_x WHERE ts_col = DATE '2026-08-20') s",
    "WITH d AS (SELECT id FROM table_x WHERE ts_col <= DATE '2026-08-20') "
    "SELECT count(*) FROM d",
    "SELECT count(*) FROM table_y WHERE id IN (SELECT id FROM table_x x "
    "WHERE x.ts_col > DATE '2026-08-20')",
    "SELECT count(*) FROM table_x WHERE (id = 1 OR ts_col = DATE '2026-08-20')",
    "SELECT count(*) FROM table_y UNION ALL "
    "SELECT count(*) FROM table_x WHERE ts_col = DATE '2026-08-20'",
])
def test_a_reference_tied_to_a_base_timestamp_column_is_refused(sql):
    client = _Athena(tables=TWO_TABLES)
    with pytest.raises(Exception) as err:
        _run(sql, client)
    assert _tool_error(err.value)["error_name"] == "TIMESTAMP_COMPARED_TO_DATE"
    assert client.started == []


def test_a_query_without_a_date_comparison_reads_no_catalog(monkeypatch):
    """The midnight check reads no catalog for a query with no date
    comparison. (The storage-bound check, which reads every bounded column,
    is its own check and is pinned in test_storage_bound_refusal.)"""
    import agents.athena_tools as at
    monkeypatch.setattr(at, "_storage_bound_check", lambda *_a, **_k: None)
    client = _Athena()
    _run("SELECT count(*) FROM table_x WHERE status = 'x'", client)
    assert client.metadata_reads == 0 and len(client.started) == 1


def test_internal_unattributed_reads_are_not_checked():
    client = _Athena()
    run_athena_query("SELECT count(*) FROM table_x WHERE ts_col = DATE '2026-08-20'",
                     CFG, client=client)
    assert len(client.started) == 1


def test_the_agent_tool_returns_the_refusal_as_its_result(monkeypatch):
    client = _Athena()
    monkeypatch.setattr("agents.athena_tools.boto3_client_for", lambda *a, **k: client)
    run_query = make_athena_tools(CFG, source_system="sys-a")[2]
    payload = json.loads(run_query._tool_func(
        sql="SELECT count(*) FROM table_x WHERE ts_col <= DATE '2026-08-20'"))
    assert payload["error_name"] == "TIMESTAMP_COMPARED_TO_DATE"
    assert client.started == []


# --------------------------------------------------------------------------- #
# The module on its own
# --------------------------------------------------------------------------- #
def test_the_finding_names_the_column_operator_and_day():
    from agents import sql_time_hygiene as hygiene

    [finding] = hygiene.find_midnight_comparisons(
        "SELECT 1 FROM t WHERE ts_col <= DATE '2026-12-31'", _one_table)
    assert (finding.column, finding.operator, finding.day) == ("ts_col", "<=", date(2026, 12, 31))
    message = hygiene.TimestampDateComparisonError(finding).to_dict()["guidance"]
    assert "DATE '2027-01-01'" in message


def test_uncertain_sql_is_not_refused():
    from agents import sql_time_hygiene as hygiene

    assert hygiene.find_midnight_comparisons(
        "SELECT 1 FROM t WHERE ts_col = DATE '2026-08-20' /* open", _one_table) == []


def test_the_finding_names_a_qualified_reference_as_written():
    from agents import sql_time_hygiene as hygiene

    [finding] = hygiene.find_midnight_comparisons(
        "SELECT 1 FROM t AS x WHERE x.ts_col = DATE '2026-08-20'", _one_table)
    assert finding.column == "x.ts_col"


@pytest.mark.parametrize("sql", [
    "SELECT ts_col FROM t ORDER BY ts_col = DATE '2026-08-20'",
    "SELECT count(*) FROM t GROUP BY ts_col HAVING ts_col = DATE '2026-08-20'",
    "SELECT f(ts_col = DATE '2026-08-20') FROM t",
    "SELECT 1 FROM t, UNNEST(arr) AS u (ts_col) WHERE ts_col = DATE '2026-08-20'",
    "SELECT 1 FROM t WHERE ts_col = DATE '2026-08-20' AND id IN (SELECT id FROM t x WHERE",
])
def test_references_outside_a_resolvable_clause_pass(sql):
    from agents import sql_time_hygiene as hygiene

    assert hygiene.find_midnight_comparisons(sql, _one_table) == []


def _one_table(parts):
    return {"ts_col": "timestamp(6)", "id": "bigint"} if parts[-1] == "t" else None
