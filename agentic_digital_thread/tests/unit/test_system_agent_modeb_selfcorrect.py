# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic Mode B self-correction: when ``_mode_b_run_query`` fails with a
column/table NAME-resolution error (Athena/Presto or PostgreSQL), the returned error
dict is ENRICHED with the exact live schema (``error_type == "schema_mismatch"`` +
``schema_hint``) so the orchestrator's retry is grounded. Non-schema errors (syntax,
timeout) and the success path stay byte-identical (they never carry a schema_hint).

Fully offline: pgdriver cursor mocked; ``run_athena_query``/``_mode_b_describe_schema``
monkeypatched. Synthetic data only — no dataset literals.
"""
import pytest

import agents.system_agent as sa


# --------------------------------------------------------------------------- #
# Classifier — narrow: only real column/table name-resolution errors match.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Athena query failed: COLUMN_NOT_FOUND: line 1:8: Column 'x' cannot be resolved"),
        RuntimeError("Athena query failed: TABLE_NOT_FOUND"),
        RuntimeError("Table awsdatacatalog.db.tbl does not exist"),
        Exception('column "widget_code" does not exist'),
        Exception('relation "widgets" does not exist'),
        type("UndefinedColumn", (Exception,), {})("boom"),
        type("UndefinedTable", (Exception,), {})("boom"),
    ],
)
def test_classifier_matches_schema_errors(exc):
    assert sa._is_schema_resolution_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Athena query failed: line 1:1: mismatched input 'SELET'"),
        TimeoutError("Athena query did not finish within 60s (last state RUNNING)"),
        Exception("permission denied for table widgets"),
        RuntimeError("scanned-bytes budget exceeded; query stopped"),
        Exception("division by zero"),
    ],
)
def test_classifier_rejects_non_schema_errors(exc):
    assert sa._is_schema_resolution_error(exc) is False


# --------------------------------------------------------------------------- #
# PostgreSQL branch — mocked cursor raising on the user query.
# --------------------------------------------------------------------------- #
class _PgCursor:
    """Raises ``exc`` when the user SELECT executes; serves information_schema for
    the describe_schema reuse. SET statements are no-ops."""

    def __init__(self, exc):
        self._exc = exc
        self._last = ""

    def execute(self, sql, params=None):
        s = str(sql)
        self._last = s
        if s.strip().upper().startswith("SET"):
            return
        if "information_schema" in s:
            return
        # The user query (capped SELECT) fails with the injected backend error.
        raise self._exc

    def fetchall(self):
        if "information_schema.tables" in self._last:
            return [("widgets",)]
        if "information_schema.columns" in self._last:
            return [("widgets", "widget_code", "varchar")]
        return []

    @property
    def description(self):
        return [("c",)]


class _PgConn:
    def __init__(self, exc):
        self._exc = exc

    def cursor(self):
        return _PgCursor(self._exc)

    def close(self):
        pass


def _run_pg(monkeypatch, exc):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")  # skip sample DISTINCT queries
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _PgConn(exc))
    # schema="" avoids the psql.SQL search_path SET (kept simple/offline).
    return sa._mode_b_run_query(
        "PostgreSQL", "SELECT widget_code FROM widgets", "", {"host": "h", "dbname": "d"}, None
    )


def test_pg_column_error_is_enriched_with_schema_hint(monkeypatch):
    res = _run_pg(monkeypatch, Exception('column "widget_code" does not exist'))
    assert res["error_type"] == "schema_mismatch"
    assert res["error"] == "schema_mismatch"
    assert "widget_code" not in res["error"]
    # schema_hint reuses the real describe_schema (tables -> columns+types).
    assert "widget_code: varchar" in res["schema_hint"]["rendered"]
    assert {"name": "widget_code", "data_type": "varchar"} in res["schema_hint"]["columns"]["widgets"]
    assert "schema_hint" in res["guidance"]


def test_pg_syntax_error_not_enriched(monkeypatch):
    with pytest.raises(Exception) as ei:
        _run_pg(monkeypatch, Exception('syntax error at or near "SELET"'))
    assert 'syntax error' in str(ei.value)
    # It re-raised the ORIGINAL error — no enriched dict.
    assert not isinstance(ei.value, dict)


def test_pg_best_effort_reraises_when_describe_fails(monkeypatch):
    """If the schema fetch itself fails, the ORIGINAL error is re-raised unchanged
    (no regression, no half-enriched dict)."""
    def _boom(*a, **k):
        raise RuntimeError("describe unavailable")

    monkeypatch.setattr(sa, "_mode_b_describe_schema", _boom)
    with pytest.raises(Exception) as ei:
        _run_pg(monkeypatch, Exception('column "widget_code" does not exist'))
    assert 'column "widget_code" does not exist' in str(ei.value)


# --------------------------------------------------------------------------- #
# PostgreSQL success path — byte-identical (never carries a schema_hint).
# --------------------------------------------------------------------------- #
class _PgOkCursor:
    def __init__(self):
        self._rows = iter(((1,), (2,)))

    def execute(self, sql, params=None):
        self._last = str(sql)

    def fetchone(self):
        return next(self._rows, None)

    @property
    def description(self):
        return [("n",)]


class _PgOkConn:
    def cursor(self):
        return _PgOkCursor()

    def close(self):
        pass


def test_pg_success_unchanged(monkeypatch):
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _PgOkConn())
    res = sa._mode_b_run_query(
        "PostgreSQL", "SELECT n FROM widgets", "", {"host": "h", "dbname": "d"}, None
    )
    assert res == {"columns": ["n"], "rows": [[1], [2]], "row_count": 2}
    assert "error_type" not in res
    assert "schema_hint" not in res


# --------------------------------------------------------------------------- #
# Athena branch — run_athena_query raises; describe_schema reused for the hint.
# --------------------------------------------------------------------------- #
def test_athena_column_error_is_enriched(monkeypatch):
    def _raise(sql, cfg):
        raise RuntimeError(
            "Athena query failed: COLUMN_NOT_FOUND: line 1:8: Column 'bogus' cannot be resolved"
        )

    monkeypatch.setattr("agents.athena_tools.run_athena_query", _raise)
    monkeypatch.setattr("agents.athena_tools.list_partition_columns", lambda table, cfg: [])
    monkeypatch.setattr(
        sa, "_mode_b_describe_schema",
        lambda *a, **k: {"rendered": "widgets\n  widget_code: varchar", "columns": {}},
    )
    res = sa._mode_b_run_query(
        "Athena", "SELECT bogus FROM widgets", "", None, {"workgroup": "w", "database": "d"}
    )
    assert res["error_type"] == "schema_mismatch"
    assert res["error"] == "schema_mismatch"
    assert "COLUMN_NOT_FOUND" not in str(res)
    assert res["schema_hint"]["rendered"].startswith("widgets")


def test_athena_timeout_not_enriched(monkeypatch):
    def _raise(sql, cfg):
        raise TimeoutError("Athena query did not finish within 60s (last state RUNNING)")

    monkeypatch.setattr("agents.athena_tools.run_athena_query", _raise)
    monkeypatch.setattr("agents.athena_tools.list_partition_columns", lambda table, cfg: [])
    with pytest.raises(TimeoutError):
        sa._mode_b_run_query(
            "Athena", "SELECT n FROM widgets", "", None, {"workgroup": "w", "database": "d"}
        )


def test_athena_success_unchanged(monkeypatch):
    ok = {"columns": ["n"], "rows": [[1]], "row_count": 1}
    monkeypatch.setattr("agents.athena_tools.run_athena_query", lambda sql, cfg: ok)
    monkeypatch.setattr("agents.athena_tools.list_partition_columns", lambda table, cfg: [])
    res = sa._mode_b_run_query(
        "Athena", "SELECT n FROM widgets", "", None, {"workgroup": "w", "database": "d"}
    )
    assert res == ok
    assert "schema_hint" not in res


# --------------------------------------------------------------------------- #
# Mode B Athena uses the same partition-scope decision as Mode A
# --------------------------------------------------------------------------- #
def test_mode_b_athena_unscoped_cte_is_blocked_before_data_execution(monkeypatch):
    calls = []

    def fake_run(sql, cfg):
        calls.append(sql)
        return {"columns": ["n"], "rows": [[1]], "row_count": 1}

    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    monkeypatch.setattr(
        "agents.athena_tools.list_partition_columns",
        lambda table, cfg: [{"name": "event_day", "data_type": "date"}],
    )
    out = sa._mode_b_run_query(
        "Athena",
        "WITH x AS (SELECT * FROM readings) SELECT count(*) FROM x",
        "",
        None,
        {"workgroup": "w", "database": "d"},
    )
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert out["scope"]["partition_columns"] == ["event_day"]
    assert calls == []


def _install_mode_b_partition_guard(monkeypatch, *, metadata="partitioned"):
    data_calls = []

    def list_columns(table, cfg):
        if metadata == "failure":
            raise RuntimeError("metadata unavailable")
        if metadata == "empty":
            return []
        return [{"name": "event_day", "data_type": "date"}]

    def fake_run(sql, cfg):
        data_calls.append(sql)
        return {"columns": ["n"], "rows": [[1]], "row_count": 1}

    monkeypatch.setattr("agents.athena_tools.list_partition_columns", list_columns)
    monkeypatch.setattr(
        "agents.athena_tools.build_recent_partition_predicate",
        lambda table, cfg, qualifier="": f'{qualifier}"event_day" IN (DATE \'2026-07-15\')',
    )
    monkeypatch.setattr("agents.athena_tools.run_athena_query", fake_run)
    return data_calls


@pytest.mark.parametrize(
    "where_clause",
    ["event_day = event_day", "event_day = DATE '2026-07-15' OR TRUE"],
)
def test_mode_b_athena_tautology_cannot_authorize_original_sql(monkeypatch, where_clause):
    data_calls = _install_mode_b_partition_guard(monkeypatch)
    original = f"SELECT count(*) FROM readings WHERE {where_clause}"  # nosec B608 - test fixture composing expected SQL from module constants
    out = sa._mode_b_run_query("Athena", original, "", None, {"database": "d"})
    assert out["row_count"] == 1
    assert len(data_calls) == 1
    assert data_calls[0] != original
    assert '"event_day" IN' in data_calls[0]
    assert out["note"].startswith("auto-scoped")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT * FROM readings) SELECT count(*) FROM x",
        "SELECT count(*) FROM (SELECT * FROM readings) x",
        "SELECT count(*) FROM readings r JOIN events e ON r.id = e.id",
        "SELECT count(*) FROM readings r, events e WHERE r.id = e.id",
        'SELECT count(*) FROM "lake_db"."readings" AS "r"',
    ],
)
def test_mode_b_athena_unsafe_or_uncertain_shapes_execute_zero_data_queries(monkeypatch, sql):
    data_calls = _install_mode_b_partition_guard(monkeypatch)
    out = sa._mode_b_run_query("Athena", sql, "", None, {"database": "d"})
    assert out["error_type"] == "scope_required"
    assert out["scope"]["reason"] == "scope_required"
    assert data_calls == []


def test_mode_b_athena_metadata_failure_executes_zero_data_queries(monkeypatch):
    data_calls = _install_mode_b_partition_guard(monkeypatch, metadata="failure")
    out = sa._mode_b_run_query(
        "Athena", "SELECT count(*) FROM readings", "", None, {"database": "d"}
    )
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert data_calls == []


def test_mode_b_athena_baked_hint_survives_metadata_miss(monkeypatch):
    data_calls = _install_mode_b_partition_guard(monkeypatch, metadata="empty")
    out = sa._mode_b_run_query(
        "Athena",
        "WITH x AS (SELECT * FROM readings) SELECT count(*) FROM x",
        "",
        None,
        {"database": "d"},
        "readings(event_day: date (partition key), value: double)",
    )
    assert out["error_type"] == "scope_required"
    assert out["scope"]["partition_columns"] == ["event_day"]
    assert data_calls == []


def test_mode_b_athena_safe_scoped_query_executes_verbatim(monkeypatch):
    data_calls = _install_mode_b_partition_guard(monkeypatch)
    original = (
        "SELECT count(*) FROM readings "
        "WHERE event_day BETWEEN DATE '2026-07-01' AND DATE '2026-07-15'"
    )
    out = sa._mode_b_run_query("Athena", original, "", None, {"database": "d"})
    assert out["row_count"] == 1
    assert data_calls == [original]
