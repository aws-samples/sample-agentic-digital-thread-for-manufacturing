# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An ordered-set percentile the engine does not have is refused before it runs.

Measured on an analytics deployment, in four dry runs: asked for a discrete
percentile, the system agent wrote ``PERCENTILE_DISC(0.90) WITHIN GROUP
(ORDER BY x)``, ``PERCENTILE_CONT(...) WITHIN GROUP (...)`` or
``APPROX_PERCENTILE_DISC(x, 0.90)``. The engine (Trino) has none of them and
accepts ``WITHIN GROUP`` only after ``LISTAGG``; it rejected each with a
parser error at the ``(`` after ``WITHIN GROUP`` or a bare
FUNCTION_NOT_FOUND, and the agent got a sentence telling it to look up the
table's columns. Two runs then fell back to ``approx_percentile`` and
reported its estimates as if they were observed values.

Now the guarded Athena tool refuses those forms before anything is sent, with
a fixed ``invalid_request`` whose guidance gives the exact discrete form that
runs on this engine (ROW_NUMBER in value order, the row at CEIL(n * p)) and
says ``approx_percentile`` is an estimate to be reported as approximate.
``LISTAGG ... WITHIN GROUP``, a name in a string literal or inside a longer
identifier, a query that declares its own external function, and a query the
tokenizer cannot read with certainty are not refused. Fully offline;
synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import storage_bounds
from agents.chat_client import classify_error_text
from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_COLUMNS = {"id": "varchar", "line": "varchar", "duration_s": "bigint",
            "created": "timestamp(6)"}
_WINDOW = ("created >= TIMESTAMP '2026-01-05 00:00:00' "
           "AND created < TIMESTAMP '2026-01-06 00:00:00'")


def _approx():
    return importlib.import_module("agents.approximate_aggregates")


class _Lake:
    """One unpartitioned Iceberg table; every data query gets one row."""

    def __init__(self):
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events", "Columns": [{"Name": n, "Type": t} for n, t in _COLUMNS.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "$partitions" in sql:
            names, rows = ["partition"], []
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in _COLUMNS.items()]
        else:
            names, rows = ["line", "v"], [["l1", "7"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self) -> list[str]:
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()


def _run(monkeypatch, sqls) -> tuple[list[dict], _Lake]:
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    cfg = at._resolve_config(_CFG)
    payloads: list[dict] = []

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "Done.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    asyncio.run(sa._run_mode_a_query(object(), "system-a", "What is the p90 by line?"))
    return payloads, lake


_REFUSED = [
    (f"SELECT line, PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY duration_s) AS p90 "
     f"FROM events WHERE {_WINDOW} GROUP BY line", "percentile_disc"),
    (f"SELECT line, percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s) AS p90 "
     f"FROM events WHERE {_WINDOW} GROUP BY line", "percentile_cont"),
    (f"SELECT line, APPROX_PERCENTILE_DISC(duration_s, 0.9) AS p90 FROM events "
     f"WHERE {_WINDOW} GROUP BY line", "approx_percentile_disc"),
    (f"SELECT line, approx_percentile_cont(duration_s, 0.9) AS p90 FROM events "
     f"WHERE {_WINDOW} GROUP BY line", "approx_percentile_cont"),
    (f"SELECT line, MEDIAN(duration_s) AS p50 FROM events WHERE {_WINDOW} GROUP BY line",
     "median"),
    # The ordered-set form after another aggregate name.
    (f"SELECT line, mode() WITHIN GROUP (ORDER BY duration_s) AS m FROM events "
     f"WHERE {_WINDOW} GROUP BY line", "WITHIN GROUP"),
    # Over a CTE, as the second measured attempt was.
    (f"WITH d AS (SELECT line, duration_s FROM events WHERE {_WINDOW}) SELECT line, "
     "PERCENTILE_DISC(0.99) WITHIN GROUP (ORDER BY duration_s) AS p99 FROM d GROUP BY line",
     "percentile_disc"),
]


@pytest.mark.parametrize("sql, form", _REFUSED)
def test_a_percentile_form_the_engine_lacks_is_refused_before_it_runs(monkeypatch, sql, form):
    (payload,), lake = _run(monkeypatch, [sql])
    assert payload["error"] == payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "FUNCTION_NOT_FOUND"
    assert payload["diagnostic"] == "percentile_function_not_in_engine"
    assert payload["refused_form"] == form
    assert lake.queries == []


def test_the_guidance_gives_the_exact_form_and_says_the_estimate_is_approximate(monkeypatch):
    (payload,), _lake = _run(monkeypatch, [_REFUSED[0][0]])
    guidance = payload["guidance"]
    assert "ROW_NUMBER() OVER (PARTITION BY <group> ORDER BY <value>)" in guidance
    assert "GREATEST(1, CAST(CEIL(n * DECIMAL '<p>') AS BIGINT))" in guidance
    assert _approx().EXACT_GROUPED_FORM in guidance
    assert _approx().EXACT_UNGROUPED_FORM in guidance
    # The percentile is a decimal literal; integer arithmetic is named only to forbid it.
    assert "DECIMAL '0.90'" in guidance
    assert guidance.count("90 / 100") == 1
    assert "never write it as integer arithmetic such as 90 / 100" in guidance
    for form in (_approx().EXACT_GROUPED_FORM, _approx().EXACT_UNGROUPED_FORM):
        assert "/ 100" not in form and "DECIMAL '<p>'" in form
    assert "approx_percentile is an estimate" in guidance
    assert "approximate" in guidance.split("approx_percentile is an estimate", 1)[1]
    # The form that has not run on this engine is not offered.
    assert "array_agg" not in guidance and "element_at" not in guidance
    # No echo of the model's SQL.
    assert "duration_s" not in guidance and "events" not in guidance


def _offered(form: str, p: str) -> str:
    """The offered form with its placeholders filled for a local table. SQLite
    has no ``DECIMAL '<x>'`` literal, so it runs the same value as a plain
    numeric literal; the form's own text is pinned above."""
    import re
    filled = (form.replace("<group>", "g").replace("<value>", "v").replace("<table>", "t")
              .replace("<filters>", "1 = 1").replace("<p>", p))
    return re.sub(r"DECIMAL '([0-9.]+)'", r"\1", filled)


def _local_lake():
    import math
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.create_function("greatest", -1, lambda *args: max(args))
    db.create_function("ceil", 1, math.ceil)
    db.execute("CREATE TABLE t (g TEXT, v INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [
        ("a", 20), ("a", 10), ("a", None),
        ("b", 25), ("b", 5), ("b", 15)])
    return db


@pytest.mark.parametrize("p, grouped, overall", [
    ("0", {"a": 10, "b": 5}, 5),
    ("0.5", {"a": 10, "b": 15}, 15),
    # Group b is [5, 15, 25]: its p90 is 25 (rank CEIL(3 * 0.90) = 3).
    ("0.90", {"a": 20, "b": 25}, 25),
    ("1", {"a": 20, "b": 25}, 25),
])
def test_the_offered_exact_forms_give_the_observed_percentile(p, grouped, overall):
    """Nearest rank: the smallest value with at least a share p of the values
    at or below it, the first value at p = 0; NULLs are not counted."""
    db = _local_lake()
    rows = db.execute(_offered(_approx().EXACT_GROUPED_FORM, p) + " ORDER BY g").fetchall()
    assert {g: v for g, _n, v in rows} == grouped
    assert {g: n for g, n, _v in rows} == {"a": 2, "b": 3}
    assert db.execute(_offered(_approx().EXACT_UNGROUPED_FORM, p)).fetchall() == [(overall,)]


def test_integer_arithmetic_for_the_percentile_picks_the_wrong_row():
    """Why the guidance forbids it: n * 90 / 100 truncates to rank 2 of
    [5, 15, 25] and gives 15, not the p90 of 25."""
    db = _local_lake()
    form = _offered(_approx().EXACT_UNGROUPED_FORM, "0.90").replace("n * 0.90", "n * 90 / 100")
    assert db.execute(form.replace("1 = 1", "g = 'b'")).fetchall() == [(15,)]
    exact = _offered(_approx().EXACT_UNGROUPED_FORM, "0.90").replace("1 = 1", "g = 'b'")
    assert db.execute(exact).fetchall() == [(25,)]


def test_the_refusal_reads_as_a_fixable_query_not_an_outage(monkeypatch):
    (payload,), _lake = _run(monkeypatch, [_REFUSED[0][0]])
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert "retryable" not in payload
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


def test_an_identical_resend_is_refused_again(monkeypatch):
    sql = _REFUSED[2][0]
    (first, second), lake = _run(monkeypatch, [sql, sql])
    assert first["error"] == second["error"] == "invalid_request"
    assert lake.queries == []


@pytest.mark.parametrize("sql", [
    f"SELECT line, LISTAGG(id, ',') WITHIN GROUP (ORDER BY id) AS ids FROM events "
    f"WHERE {_WINDOW} GROUP BY line",
    f"SELECT line, 'percentile_disc(0.9)' AS how FROM events WHERE {_WINDOW}",
    f"SELECT line, MAX(duration_s) AS percentile_disc_hours FROM events WHERE {_WINDOW} "
    "GROUP BY line",
    f"SELECT line, approx_percentile(duration_s, 0.9) AS p90 FROM events WHERE {_WINDOW} "
    "GROUP BY line",
    f"SELECT line, COUNT(*) AS median FROM events WHERE {_WINDOW} GROUP BY line",
])
def test_what_the_engine_has_runs_unrefused(monkeypatch, sql):
    (payload,), lake = _run(monkeypatch, [sql])
    assert payload.get("error") is None, payload
    assert payload["rows"] == [["l1", "7"]]
    assert len(lake.data_queries()) == 1


@pytest.mark.parametrize("sql", [
    # A query the tokenizer cannot read with certainty.
    "SELECT PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY duration_s) FROM events /* open",
    # A query that declares its own function.
    "USING EXTERNAL FUNCTION median(x DOUBLE) RETURNS DOUBLE LAMBDA 'fn' "
    "SELECT median(duration_s) FROM events",
    # A qualified name is not the engine's built-in.
    "SELECT udf.percentile_disc(duration_s, 0.9) FROM events",
])
def test_what_cannot_be_read_as_the_engine_builtin_is_not_refused(sql):
    assert _approx().refusal(sql) is None


@pytest.mark.parametrize("sql, form", [
    # The declaration is for another function; median is still the missing built-in.
    ("USING EXTERNAL FUNCTION helper(x BIGINT) RETURNS BIGINT LAMBDA 'fn' "
     "SELECT helper(duration_s) AS h, median(duration_s) AS m FROM events", "median"),
    # A declared median is exempt, and another missing built-in beside it is not.
    ("USING EXTERNAL FUNCTION median(x DOUBLE) RETURNS DOUBLE LAMBDA 'fn' "
     "SELECT median(duration_s) AS m, percentile_disc(duration_s, 0.9) AS p FROM events",
     "percentile_disc"),
    # Two declarations, the second for median: percentile_cont is still refused.
    ("USING EXTERNAL FUNCTION helper(x BIGINT) RETURNS BIGINT LAMBDA 'fn', "
     "EXTERNAL FUNCTION median(x DOUBLE) RETURNS DOUBLE LAMBDA 'fn2' "
     "SELECT median(duration_s), percentile_cont(duration_s, 0.9) FROM events",
     "percentile_cont"),
])
def test_only_a_declared_function_name_is_exempt(sql, form):
    assert _approx().refused_form(sql) == form


def test_the_athena_prompt_carries_one_percentile_line():
    line = sa._percentile_prompt("Athena")
    assert "PERCENTILES (required)" in line
    assert "ROW_NUMBER" in line and "GREATEST(1, CEIL(n * DECIMAL '0.90'))" in line
    assert "never as integer arithmetic such as 90 / 100" in line
    assert "approx_percentile" in line and "approximate" in line
    assert "array_agg" not in line and "element_at" not in line
    assert "\u2014" not in line and "\u2013" not in line
    assert len(line) <= 600
    assert sa._policy_tail("Athena").count("PERCENTILES (required)") == 1
    for ct in ("PostgreSQL", "MCP Server", "SiteWise (Direct)", ""):
        assert sa._percentile_prompt(ct) == ""
        assert "PERCENTILES (required)" not in sa._policy_tail(ct)
