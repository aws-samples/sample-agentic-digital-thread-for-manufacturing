# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A top-level ``<string column> <> ''`` WHERE conjunct, written so the engine
can skip NULL pages.

On an analytics lake (Athena engine v3 over Iceberg), a week-long top-reason
count with a source owner's row exclusion scanned past the 5 GB per-query
limit when it held ``col IS NOT NULL AND col <> ''`` on a mostly-NULL column,
and 1.08 GB with the second conjunct written as ``length(col) > 0`` (same
rows). The engine folds ``IS NOT NULL`` into ``<> ''`` and then no longer
skips the pages where the column is NULL, so the exclusion's wide columns are
read for every row (measured 2026-10-27).

The connector now writes each such conjunct as ``(col IS NOT NULL AND
length(col) > 0)`` before the query runs. In a WHERE clause's top-level AND
chain the two are the same filter: a row is kept only when every conjunct is
TRUE, and both are TRUE exactly when ``col`` is neither NULL nor ``''``.
Anything else is left as written: an OR at the top level, NOT, parentheses,
an operand with an operator binding tighter, HAVING, ON, CASE, a column whose
catalogued type is not a string, and any reference not tied by SQL scope to
one base-table column. The result records the query as given
(``_adt_executed_query``); the statement the engine received is reported apart
(``athena_tools.engine_statements``). Offline: the Athena client is a fake whose catalog
read returns the types; synthetic names only.
"""
from __future__ import annotations

import pytest

from agents import sql_empty_string
from agents.athena_tools import run_athena_query

CFG = {"workgroup": "wg", "database": "db", "region": "us-east-1"}
DEF = '"details.quality.reason_code"'


class _Athena:
    """Catalog read plus a query path that records every started query."""

    def __init__(self, tables=None):
        default = [
            {"Name": "status", "Type": "varchar"},
            {"Name": "label", "Type": "string"},
            {"Name": "code", "Type": "char(4)"},
            {"Name": "n", "Type": "bigint"},
            {"Name": "ts_col", "Type": "timestamp(6)"},
            {"Name": "owner_ref", "Type": "varchar"},
            {"Name": "batch_ref", "Type": "varchar"},
            {"Name": "step_kind", "Type": "varchar"},
            {"Name": "area", "Type": "varchar"},
            {"Name": "details.quality.reason_code", "Type": "varchar"},
            {"Name": "details.quality.found_at", "Type": "timestamp(6) with time zone"},
        ]
        self.tables = tables or {"table_x": default, "table_y": default}
        self.started = []
        self.metadata_reads = 0

    def get_table_metadata(self, **kw):
        self.metadata_reads += 1
        if kw.get("TableName") not in self.tables:
            raise RuntimeError("EntityNotFoundException: table not found")
        return {"TableMetadata": {"Columns": self.tables[kw["TableName"]], "PartitionKeys": []}}

    def start_query_execution(self, **kw):
        self.started.append(kw["QueryString"])
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **_kw):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "n"}]},
                              "Rows": [{"Data": [{"VarCharValue": "n"}]},
                                       {"Data": [{"VarCharValue": "7"}]}]}}


@pytest.fixture(autouse=True)
def _no_storage_bound(monkeypatch):
    import agents.athena_tools as at
    monkeypatch.setattr(at, "_storage_bound_check", lambda *_a, **_k: None)


def _ran(sql, client=None):
    client = client or _Athena()
    result = run_athena_query(sql, CFG, client=client, source_system="sys-a")
    assert len(client.started) == 1
    return client.started[0], result


def _capped(sql: str) -> str:
    """``sql`` as the connector starts it, with its row cap."""
    from agents.sql_safety import MAX_ROWS, apply_row_cap
    return apply_row_cap(sql, MAX_ROWS)[0]


@pytest.mark.parametrize("written, expected", [
    ("SELECT count(*) FROM table_x WHERE status <> ''",
     "SELECT count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)"),
    ("SELECT count(*) FROM table_x WHERE status != ''",
     "SELECT count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)"),
    ("SELECT count(*) FROM table_x WHERE '' <> status",
     "SELECT count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)"),
    ("SELECT count(*) FROM table_x WHERE '' != label",
     "SELECT count(*) FROM table_x WHERE (label IS NOT NULL AND length(label) > 0)"),
    ("SELECT count(*) FROM table_x t WHERE t.n > 1 AND t.status <> '' AND t.n < 9",
     "SELECT count(*) FROM table_x t WHERE t.n > 1 AND (t.status IS NOT NULL AND "
     "length(t.status) > 0) AND t.n < 9"),
    ('SELECT count(*) FROM table_x WHERE "status" <> \'\'',
     'SELECT count(*) FROM table_x WHERE ("status" IS NOT NULL AND length("status") > 0)'),
    ("select count(*) from table_x where n > 1 and status <> ''",
     "select count(*) from table_x where n > 1 and (status IS NOT NULL AND length(status) > 0)"),
    ("SELECT count(*) FROM table_x WHERE n BETWEEN 1 AND 5 AND status <> ''",
     "SELECT count(*) FROM table_x WHERE n BETWEEN 1 AND 5 AND "
     "(status IS NOT NULL AND length(status) > 0)"),
    ("SELECT status, count(*) FROM table_x WHERE status <> '' GROUP BY status",
     "SELECT status, count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0) "
     "GROUP BY status"),
    ("SELECT count(*) FROM table_x WHERE status <> '' AND label <> ''",
     "SELECT count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0) AND "
     "(label IS NOT NULL AND length(label) > 0)"),
    ("SELECT count(*) FROM table_x WHERE status<>''",
     "SELECT count(*) FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)"),
    ("WITH c AS (SELECT status FROM table_x WHERE status <> '') SELECT count(*) FROM c",
     "WITH c AS (SELECT status FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)) "
     "SELECT count(*) FROM c"),
])
def test_a_top_level_not_empty_conjunct_is_written_to_skip_null_pages(written, expected):
    started, _result = _ran(written)
    assert started == _capped(expected)


def test_the_measured_query_shape_keeps_everything_else_byte_identical():
    """The shape of the measured query (2026-10-27): only the top-level
    `<> ''` conjunct changes; the row exclusion's predicate (inside NOT, with
    COALESCE) and every other conjunct stay exactly as written."""
    rule = ("NOT (COALESCE(owner_ref, '') <> '' AND COALESCE(batch_ref, '') <> '' AND "
            "COALESCE(step_kind, '') NOT IN ('STEP_A', 'STEP_B'))")
    written = (f"SELECT {DEF} AS reason_code, COUNT(*) AS record_count "
               f"FROM work_records WHERE area = 'A1' "
               f"AND {DEF} IS NOT NULL AND {DEF} <> '' AND {rule} "
               f"GROUP BY {DEF} ORDER BY record_count DESC LIMIT 5")
    client = _Athena(tables={"work_records": _Athena().tables["table_x"]})
    started, _ = _ran(written, client)
    assert started == _capped(
        written.replace(f"{DEF} <> ''", f"({DEF} IS NOT NULL AND length({DEF}) > 0)"))
    assert rule in started


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM table_x WHERE status <> '' OR n > 1",
    "SELECT count(*) FROM table_x WHERE n > 1 OR status <> ''",
    "SELECT count(*) FROM table_x WHERE NOT status <> ''",
    "SELECT count(*) FROM table_x WHERE (status <> '')",
    "SELECT count(*) FROM table_x WHERE n > 1 AND (status <> '' OR n < 0)",
    "SELECT count(*) FROM table_x WHERE COALESCE(status, '') <> ''",
    "SELECT count(*) FROM table_x WHERE trim(status) <> ''",
    "SELECT count(*) FROM table_x WHERE status <> 'x'",
    "SELECT count(*) FROM table_x WHERE status <> ' '",
    "SELECT count(*) FROM table_x WHERE status || 'a' <> ''",
    "SELECT count(*) FROM table_x WHERE status <> '' || 'a'",
    "SELECT count(*) FROM table_x WHERE flag = status <> ''",
    "SELECT count(*) FROM table_x WHERE code <> ''",
    "SELECT count(*) FROM table_x WHERE n <> ''",
    "SELECT count(*) FROM table_x WHERE ts_col <> ''",
    "SELECT count(*) FROM table_x WHERE missing_col <> ''",
    "SELECT count(*) FROM table_q WHERE status <> ''",
    "SELECT CASE WHEN status <> '' THEN 1 END FROM table_x",
    "SELECT status <> '' AS has_status FROM table_x",
    "SELECT status, count(*) FROM table_x GROUP BY status HAVING status <> ''",
    "SELECT count(*) FROM table_x a JOIN table_y b ON a.status <> '' AND a.n = b.n",
    "SELECT count(*) FROM table_x a JOIN table_y b ON a.n = b.n WHERE status <> ''",
    "WITH c AS (SELECT status FROM table_x) SELECT count(*) FROM c WHERE status <> ''",
    "SELECT count(*) FROM (SELECT status FROM table_x) d WHERE status <> ''",
    "SELECT count(*) FROM other_db.table_x WHERE status <> ''",
    "SELECT count(*) FROM table_x WHERE status <> '' AND n IN (SELECT n FROM table_y WHERE",
])
def test_anything_else_runs_as_written(sql):
    client = _Athena()
    try:
        started, _ = _ran(sql, client)
    except Exception:  # noqa: BLE001 - a statement the connector refuses runs nothing
        assert client.started == [] or client.started[0].startswith(sql)
        return
    assert started == _capped(sql)


def test_a_subquery_s_own_where_is_written_too():
    """Each query block's WHERE filters its own rows: the same equality holds
    in a subquery's top-level AND chain."""
    started, _ = _ran("SELECT count(*) FROM table_x x WHERE EXISTS "
                      "(SELECT 1 FROM table_y y WHERE y.n = x.n AND y.status <> '')")
    assert "(y.status IS NOT NULL AND length(y.status) > 0)" in started


def test_writing_is_idempotent():
    once = sql_empty_string.rewrite(
        "SELECT count(*) FROM table_x WHERE status <> ''",
        lambda parts: {"status": "varchar"} if parts[-1] == "table_x" else None)
    twice = sql_empty_string.rewrite(once, lambda parts: {"status": "varchar"})
    assert once == twice == ("SELECT count(*) FROM table_x WHERE "
                             "(status IS NOT NULL AND length(status) > 0)")


def test_uncertain_sql_is_left_as_written():
    sql = "SELECT count(*) FROM table_x WHERE status <> '' /* unterminated"
    assert sql_empty_string.rewrite(sql, lambda parts: {"status": "varchar"}) == sql


def test_a_query_without_a_candidate_reads_no_catalog():
    client = _Athena()
    _ran("SELECT count(*) FROM table_x WHERE status = 'x'", client)
    assert client.metadata_reads == 0


def test_internal_unattributed_reads_are_not_written():
    client = _Athena()
    sql = "SELECT count(*) FROM table_x WHERE status <> ''"
    run_athena_query(sql, CFG, client=client)
    assert client.started == [_capped(sql)]


def test_the_result_records_the_query_as_given_and_reports_the_engine_statement_apart():
    import agents.athena_tools as at
    model = "SELECT count(*) FROM table_x WHERE status <> ''"
    with at.engine_statements({}) as sink:
        started, result = _ran(model)
    assert started != _capped(model)
    assert result["_adt_executed_query"] == _capped(model)
    assert sink == {_capped(model): started}
    assert not [key for key in result if key.startswith("_adt_") and "engine" in key]


def test_a_query_with_nothing_written_reports_no_engine_statement():
    import agents.athena_tools as at
    model = "SELECT count(*) FROM table_x WHERE status = 'x'"
    with at.engine_statements({}) as sink:
        started, result = _ran(model)
    assert result["_adt_executed_query"] == started == _capped(model)
    assert sink == {}


def test_a_later_run_the_engine_receives_as_recorded_drops_the_earlier_report():
    """The same record run again unchanged (its column no longer catalogued as
    a string) supersedes the earlier report of it: no reader of the sink can
    take the first run's statement for the second."""
    import agents.athena_tools as at
    model = "SELECT count(*) FROM table_x WHERE status <> ''"
    char_status = [{"Name": "status", "Type": "char(4)"}]
    with at.engine_statements({}) as sink:
        first, _ = _ran(model)
        assert sink == {_capped(model): first}
        at.clear_schema_cache()
        second, result = _ran(model, _Athena(tables={"table_x": char_status}))
    at.clear_schema_cache()
    assert second == result["_adt_executed_query"] == _capped(model)
    assert sink == {}


def test_without_a_bound_sink_nothing_is_reported():
    _started, result = _ran("SELECT count(*) FROM table_x WHERE status <> ''")
    assert result["_adt_executed_query"] == _capped("SELECT count(*) FROM table_x WHERE status <> ''")


def test_the_split_reads_the_written_query(monkeypatch):
    """A query the engine still refuses for scan size is split from the
    written form, so its pieces skip NULL pages too."""
    import agents.athena_exact_split as xs
    import agents.athena_tools as at
    seen = {}

    def refuse(_sql, _cfg, _kwargs):
        raise at.AthenaEngineScanLimitError("Athena query cancelled: the engine scan limit was "
                                            "exceeded", limit_source="engine",
                                            scanned_bytes=5_000_000_000)

    def split(sql, _cfg, **_kw):
        seen["sql"] = sql
        raise xs.ExactSplitRefused("unsplittable", "no split fits")

    monkeypatch.setattr(at, "_run_once_retried", refuse)
    monkeypatch.setattr(xs, "compute_exact", split)
    with pytest.raises(at.AthenaEngineScanLimitError):
        at.run_athena_query_exact("SELECT count(*) FROM table_x WHERE status <> ''", CFG,
                                  source_system="sys-a", client=_Athena())
    assert seen["sql"] == ("SELECT count(*) FROM table_x WHERE "
                           "(status IS NOT NULL AND length(status) > 0)")


class _ZeroAthena(_Athena):
    """A COUNT that comes back 0 (and, for a second output, NULL)."""

    def get_query_results(self, **_kw):
        sql = self.started[-1]
        names = ["n", "flag"] if " AS flag" in sql else ["n"]
        cells = [{"VarCharValue": "0"}] + [{} for _ in names[1:]]
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": x} for x in names]},
                              "Rows": [{"Data": [{"VarCharValue": x} for x in names]},
                                       {"Data": cells}]}}


def _note_with_and_without_writing(model: str, monkeypatch) -> tuple:
    """The NULL-unsafe note (``system_agent._disclose_null_unsafe_filter``) on
    a zero COUNT of ``model``, with the connector writing its conjuncts and
    with it writing none; plus whether anything was written."""
    import agents.athena_tools as at
    from agents import system_agent
    with at.engine_statements({}) as sink:
        _sql, written = _ran(model, _ZeroAthena())
    with monkeypatch.context() as patch:
        patch.setattr(at, "_written_for_null_pages", lambda _a, _c, sql: sql)
        _sql, plain = _ran(model, _ZeroAthena())
    for result in (written, plain):
        system_agent._disclose_null_unsafe_filter(result, model)
    return written.get("note"), plain.get("note"), bool(sink)


@pytest.mark.parametrize("model, noted", [
    ("SELECT count(*) AS n FROM table_x WHERE status <> ''", True),
    ("SELECT count(*) AS n FROM table_x WHERE status = 'x' AND label <> ''", True),
    ("SELECT count(*) AS n FROM table_x WHERE n > 1 AND status != ''", True),
    ("SELECT count(*) AS n FROM table_x WHERE status <> '' AND label <> ''", True),
    ("SELECT count(*) AS n FROM table_x WHERE status IS NOT NULL AND status <> ''", False),
    ("SELECT count(*) AS n FROM table_x WHERE status <> '' AND status = 'x'", False),
    # The agent's own form of the written conjunct, alone, beside a written
    # one, and in the SELECT list (where it once consumed the read-back).
    ("SELECT count(*) AS n FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)",
     False),
    ("SELECT count(*) AS n FROM table_x WHERE (label IS NOT NULL AND length(label) > 0) "
     "AND status <> ''", True),
    ("SELECT count(*) AS n, max(CASE WHEN (status IS NOT NULL AND length(status) > 0) "
     "THEN 1 ELSE 0 END) AS flag FROM table_x WHERE status <> ''", True),
    ("SELECT count(*) AS n FROM table_x WHERE status <> '' AND label NOT IN ('a', 'b')", True),
    ("SELECT count(*) AS n FROM table_x WHERE status <> '' AND label NOT LIKE 'a%'", True),
    ("SELECT count(*) AS n FROM table_x WHERE status <> '' OR label <> ''", False),
])
def test_a_zero_result_s_note_is_the_note_it_had_without_the_writing(model, noted, monkeypatch):
    """Every reader of ``_adt_executed_query`` reads the query as given, so
    the note on an empty or zero-count result is the one it was before the
    connector wrote anything, whatever the agent wrote around it."""
    written, plain, wrote = _note_with_and_without_writing(model, monkeypatch)
    assert written == plain
    assert (written is not None) is noted
    if "<> ''" in model.split("WHERE", 1)[1] and " OR " not in model:
        assert wrote  # not vacuous: the engine did get the written form


def test_the_select_list_case_still_gets_its_note(monkeypatch):
    """The agent's own `(col IS NOT NULL AND length(col) > 0)` in the SELECT
    list does not hide that the WHERE's `col <> ''` drops NULL rows."""
    written, _plain, wrote = _note_with_and_without_writing(
        "SELECT count(*) AS n, max(CASE WHEN (status IS NOT NULL AND length(status) > 0) "
        "THEN 1 ELSE 0 END) AS flag FROM table_x WHERE status <> ''", monkeypatch)
    assert wrote and written and "status" in written


def test_the_form_written_by_the_agent_itself_gets_no_note(monkeypatch):
    """A model that writes the null-safe form itself already states the NULL
    exclusion; its zero result gets no note, as before."""
    written, plain, _wrote = _note_with_and_without_writing(
        "SELECT count(*) AS n FROM table_x WHERE (status IS NOT NULL AND length(status) > 0)",
        monkeypatch)
    assert written is None and plain is None
