# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A time window applied ONLY to a storage column, on a table whose business
time (a state's own time) the rules know, is refused before it runs.

Live accuracy bug: on an Iceberg table partitioned by a write/creation date,
the SQL agent answered 'how many completed on 7/20' by bounding only the
write date. Rows are written on earlier days than they complete, so that
counts the rows written that day, not the day's completions, and the count was
badly wrong. The existing window-on-storage rule
(``storage_bound_rule.decide_window``) only runs when the turn carries a
RESOLVED TIME WINDOWS block; a 'compare 7/20 vs 7/27' phrasing carries none,
so the query ran. The state-time mapping is known regardless of the block, so
the refusal now fires whenever a table has a known business-time column and
the query's only time bound is on a storage column the question does not name:
the agent is told to filter on the business time column, by name, and retries.

Athena and S3 Tables (DuckDB) share ``storage_bound_rule`` and the guard, so
the rule covers both. Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import storage_bound_rule, storage_bounds

from .test_state_time_window import _CFG, _DESC, _Lake, _st

# The storage column is `created` (partition `created_day`); the business time
# columns are the states' own times (complete_time / fail_time / start_time).
_DAY = "TIMESTAMP '2026-07-20 00:00:00'"
_NEXT = "TIMESTAMP '2026-07-21 00:00:00'"
_STORAGE_ONLY = (f"SELECT COUNT(*) AS n FROM events WHERE created >= {_DAY} AND created < {_NEXT}")
_COMPLETED_QUESTION = "How many rows completed on 7/20?"
# No RESOLVED TIME WINDOWS block in the turn (the live 'compare' phrasing).
_NO_WINDOW_KG = "Routing note for this question."


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _st().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _st().reset()


def _ask(monkeypatch, lake, sqls, *, question=_COMPLETED_QUESTION, kg_context=_NO_WINDOW_KG,
         desc=_DESC, time_basis="business"):
    sa._remember_turn_time_facts("system-a", [], desc)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    def _one(sql):
        # The run-query tool sets this per call from its time_basis arg; the
        # harness drives _scoped_athena_attempt directly, so it sets it here.
        token = sa._TIME_BASIS.set(sa._clean_time_basis(time_basis))
        try:
            return sa._scoped_athena_attempt(sql, cfg, {}, "system-a")
        finally:
            sa._TIME_BASIS.reset(token)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(_one, sql))

    def invoke(_agent, _question):
        run_async(calls)
        return "Answer.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", question, kg_context))
    return payloads, answer


def test_storage_only_window_without_a_resolved_block_is_refused(monkeypatch):
    # The live slip: no RESOLVED TIME WINDOWS block, so the old decide_window
    # never ran and this query (bounding only the write/storage column) ran.
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_STORAGE_ONLY])
    assert payload["error"] == "storage_bound"
    assert payload["error_name"] == "STORAGE_BOUND"
    assert payload["reason"] == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE
    assert payload["retryable"] is True
    assert payload["storage_columns"] == ["created"]
    guidance = payload["guidance"]
    # Names a business time column to filter on, and says the storage column
    # records when rows were written.
    assert ("complete_time" in guidance or "fail_time" in guidance
            or "start_time" in guidance)
    assert "written" in guidance.lower() or "stored" in guidance.lower()
    assert lake.data_queries() == []


def test_a_bound_on_a_business_time_column_is_not_refused(monkeypatch):
    # Bounding the state's own time column is exactly what the rule wants: runs.
    lake = _Lake()
    good = (f"SELECT COUNT(*) AS n FROM events WHERE complete_time >= {_DAY} "
            f"AND complete_time < {_NEXT}")
    (payload,), _answer = _ask(monkeypatch, lake, [good])
    assert "error" not in payload
    assert payload["rows"] == [["42"]]


def test_the_question_naming_the_storage_column_is_not_refused(monkeypatch):
    # If the question is genuinely about the write/creation date, allow it.
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_STORAGE_ONLY],
                               question="How many rows were created on 7/20?")
    assert "error" not in payload
    assert payload["rows"] == [["42"]]


def test_no_mapping_means_no_business_time_refusal(monkeypatch):
    # A system with no state-time mapping has no known business time, so this
    # rule does not fire (the structural decide_window still governs windowed
    # turns elsewhere).
    lake = _Lake()
    plain_desc = "events(id: varchar; state: varchar; created: timestamp(6))"
    (payload,), _answer = _ask(monkeypatch, lake, [_STORAGE_ONLY], desc=plain_desc)
    assert "error" not in payload
    assert payload["rows"] == [["42"]]


def test_the_remedy_reads_as_fixable_not_an_outage(monkeypatch):
    from agents.chat_client import classify_error_text
    from agents.orchestrator import (_AUTH_TOOL_ERROR_MARKERS,
                                      _TRANSIENT_TOOL_ERROR_MARKERS)
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_STORAGE_ONLY])
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


@pytest.mark.parametrize("sql", [
    # a direct day-bound nested in a CTE / derived table
    f"WITH w AS (SELECT * FROM events WHERE created >= {_DAY} AND created < {_NEXT}) "
    "SELECT COUNT(*) FROM w",
    f"SELECT COUNT(*) FROM (SELECT * FROM events WHERE created >= {_DAY} AND created < {_NEXT}) w",
    # BETWEEN on the storage column
    f"SELECT COUNT(*) FROM events WHERE created BETWEEN {_DAY} AND {_NEXT}",
])
def test_direct_storage_bounds_in_nested_and_between_forms_are_refused(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("sql", [
    # CAST(storage_col AS DATE) compared / grouped
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events WHERE CAST(created AS DATE) "
    "IN (DATE '2026-07-20', DATE '2026-07-27') GROUP BY 1",
    # date_trunc('day', storage_col)
    f"SELECT date_trunc('day', created) AS d, COUNT(*) FROM events WHERE "
    f"date_trunc('day', created) IN ({_DAY}, {_NEXT}) GROUP BY 1",
    # a storage-day projection + GROUP BY with no business-time bound
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events GROUP BY 1",
    # DuckDB postfix cast of the storage column to a day
    f"SELECT COUNT(*) FROM events WHERE created::date BETWEEN DATE '2026-07-20' "
    "AND DATE '2026-07-27'",
])
def test_storage_day_grouping_and_wrapped_bounds_are_refused(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("question", [
    "How many rows were ingested on 7/20?",
    "How fresh is the data?",
    "When was the data last loaded?",
])
def test_an_explicit_ingestion_time_basis_lets_the_storage_window_run(monkeypatch, question):
    # Exemption B: the caller sets time_basis='ingestion' (the tool arg) for a
    # write/ingestion/freshness question, so a window on the storage column is
    # the declared basis and the query runs. The intent comes from the arg, not
    # from matching words in the question text (the old word list is gone).
    sql = f"SELECT COUNT(*) FROM events WHERE created >= {_DAY} AND created < {_NEXT}"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question=question,
                               time_basis="ingestion")
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]]


@pytest.mark.parametrize("sql", [
    # A direct WHERE bound: the connector also labels this one.
    f"SELECT COUNT(*) AS n FROM events WHERE created >= {_DAY} AND created < {_NEXT}",
    # A grouped projection of the storage day: the connector labels nothing, so
    # the rule itself must emit the storage-bound disclosure under ingestion.
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events GROUP BY 1",
    # A wrapped bucket of the storage day, grouped.
    "SELECT date_trunc('day', created) AS d, COUNT(*) FROM events GROUP BY 1",
])
def test_every_ingestion_run_is_labelled_with_the_storage_bound(monkeypatch, sql):
    # Exemption B must not run silently, whatever the shape: an ingestion run
    # that references the storage column carries the connector's `storage_bound`
    # label (the storage column it used). That label is what the answer path
    # turns into the storage-bound disclosure.
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql],
                               question="How many rows by load day?", time_basis="ingestion")
    assert "error" not in payload, payload
    assert payload.get("storage_bound", {}).get("columns") == ["created"], payload


def test_the_ingestion_disclosure_reaches_the_final_system_answer(monkeypatch):
    # End to end: an ingestion run that bounds the storage column is labelled by
    # the rule (the grouped/wrapped forms are labelled too, above), and that
    # label reaches the system answer's `[engine] Storage bound ran:` line
    # through the provenance path, so the reader learns the figures are dated by
    # write time. The result is captured as a sidecar statement and the answer
    # is composed from it.
    import json
    from types import SimpleNamespace

    import agents.tool_result_capture as trc

    sql = f"SELECT COUNT(*) AS n FROM events WHERE created >= {_DAY} AND created < {_NEXT}"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql],
                               question="How many rows by load window?", time_basis="ingestion")
    assert payload.get("storage_bound", {}).get("columns") == ["created"], payload

    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "athena_run_query", "input": {"sql": sql}},
        invocation_state=state,
        result={"toolUseId": "t-1", "status": "success",
                "content": [{"text": json.dumps(payload)}]},
        exception=None, retry=False, agent=SimpleNamespace(messages=[])))

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state",
                        lambda _agent, _q: ("That load window held 42 rows.", state))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    composed = asyncio.run(sa._run_mode_a_query(
        object(), "system-a", "How many rows by load window?"))
    assert "[engine] Storage bound ran:" in composed
    assert "created" in composed


@pytest.mark.parametrize("sql", [
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events GROUP BY 1",
    "SELECT id, created FROM events ORDER BY created DESC LIMIT 5",
])
def test_a_grouped_or_ordered_ingestion_run_discloses_its_write_time_basis(monkeypatch, sql):
    # A grouping or ordering on the storage column has no WHERE bound, so the
    # bound line cannot describe it; the write-time basis line and its answer
    # requirement must still reach the system answer.
    import json
    from types import SimpleNamespace

    import agents.tool_result_capture as trc

    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql],
                               question="How many completed by day?", time_basis="ingestion")
    assert payload.get("storage_bound", {}).get("basis") == "ingestion", payload

    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "athena_run_query", "input": {"sql": sql}},
        invocation_state=state,
        result={"toolUseId": "t-1", "status": "success",
                "content": [{"text": json.dumps(payload)}]},
        exception=None, retry=False, agent=SimpleNamespace(messages=[])))

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state",
                        lambda _agent, _q: ("42 completed rows.", state))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    composed = asyncio.run(sa._run_mode_a_query(
        object(), "system-a", "How many completed by day?"))
    assert "[engine] Write-time basis:" in composed
    assert "created" in composed
    assert "dated by when rows were written" in composed


def test_a_business_basis_run_carries_no_write_time_basis_record():
    bound = storage_bound_rule.StorageBound("events", ("created",), (), ())
    assert "basis" not in bound.to_dict()
    assert storage_bounds.ingestion_basis_columns([{"storage_bound": bound.to_dict()}]) == []


def test_without_the_ingestion_basis_the_same_storage_window_is_refused(monkeypatch):
    # The default basis is business time: with a state-time mapping a storage-
    # only window is refused, whatever the question says (the word list is gone,
    # so "recently" or any phrasing no longer opens the storage window).
    sql = f"SELECT COUNT(*) FROM events WHERE created >= {_DAY} AND created < {_NEXT}"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question="How many rows recently?")
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("sql", [
    # MIN/MAX of the storage column in the SELECT list, nowhere else: a
    # freshness read of the earliest/latest stored value, which drops no rows.
    "SELECT MAX(created) FROM events",
    "SELECT MIN(created), MAX(created) FROM events",
    "SELECT state, MAX(created) FROM events GROUP BY 1",
])
def test_a_minmax_freshness_read_of_the_storage_column_is_not_refused(monkeypatch, sql):
    # Exemption A, structural: a freshness read ("when were rows last written")
    # is a legitimate storage-column read, so it runs without the ingestion arg.
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert "error" not in payload, payload


@pytest.mark.parametrize("sql", [
    # ORDER BY the storage column is no longer exempt: it dates the output by
    # write time, and with LIMIT it also drops all but the latest-written rows.
    # A 'latest rows' listing uses time_basis='ingestion' instead.
    "SELECT id FROM events ORDER BY CAST(created AS DATE)",
    "SELECT id FROM events ORDER BY created DESC LIMIT 10",
    # the statement aggregates as well: still the wrong basis.
    "SELECT COUNT(*) FROM events ORDER BY CAST(created AS DATE)",
    # MIN/MAX in SELECT is a freshness read, but the storage column ALSO appears
    # in a WHERE bound: that bound still drops the day's rows, so refuse.
    f"SELECT MAX(created) FROM events WHERE created >= {_DAY} AND created < {_NEXT}",
])
def test_order_by_and_mixed_storage_shapes_are_refused(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("sql", [
    # An alias equal to the storage column name is a relation alias, not a
    # column reference: the statement never reads the storage column.
    "SELECT COUNT(*) FROM events AS created",
    # A prefix-distinct column (created_extra) is a different column.
    "SELECT created_extra FROM events",
    # The storage name only in a string literal / comment is not a reference.
    "SELECT COUNT(*) FROM events WHERE state = 'created' /* created */",
])
def test_alias_prefix_literal_and_comment_are_not_storage_references(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert "error" not in payload, payload


@pytest.mark.parametrize("sql", [
    # A table-qualified and a quoted reference to the storage column are read.
    f"SELECT COUNT(*) FROM events e WHERE e.created >= {_DAY}",
    f'SELECT COUNT(*) FROM events WHERE "created" >= {_DAY}',
])
def test_qualified_and_quoted_storage_references_are_refused(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


def test_max_storage_in_a_cte_then_filtered_is_not_a_freshness_exemption(monkeypatch):
    # A MIN/MAX of the storage column inside a CTE whose value then filters the
    # outer result is not a pure freshness read: the aggregate controls which
    # groups survive, so the storage basis drives the answer and it refuses.
    sql = ("WITH w AS (SELECT state, MAX(created) AS latest, COUNT(*) AS n "
           "FROM events GROUP BY state) "
           f"SELECT * FROM w WHERE latest >= {_DAY}")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("sql", [
    # strftime / date_format / extract wrap the storage column into a day, so
    # with no wrapper allowlist each reads as a storage-time basis and refuses.
    "SELECT strftime(created, '%Y-%m-%d') AS d, COUNT(*) FROM events GROUP BY 1",
    "SELECT date_format(created, '%Y-%m-%d') AS d, COUNT(*) FROM events GROUP BY 1",
    "SELECT extract(year FROM created) AS y, COUNT(*) FROM events GROUP BY 1",
])
def test_any_wrapper_over_the_storage_column_is_a_storage_basis_and_refused(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("sql", [
    # A business-time column mentioned elsewhere does NOT disable the rule: a
    # storage-day projection/grouping with MAX/HAVING/OVER of a business column
    # is still dated by the storage day, so it refuses.
    "SELECT CAST(created AS DATE) AS d, MAX(complete_time), COUNT(*) FROM events GROUP BY 1",
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events GROUP BY 1 ORDER BY MAX(complete_time)",
    "SELECT CAST(created AS DATE) AS d, COUNT(*) FROM events "
    "GROUP BY 1 HAVING MAX(complete_time) IS NOT NULL",
    "SELECT CAST(created AS DATE) AS d, MAX(complete_time) OVER () FROM events",
])
def test_mentioning_a_business_column_elsewhere_does_not_exempt_a_storage_day(monkeypatch, sql):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


@pytest.mark.parametrize("question", [
    "How many completed orders arrived on 7/20?",
    "How many stale work orders completed on 7/20?",
    "How many completed jobs were written off on 7/20?",
])
def test_business_words_that_overlap_storage_vocabulary_do_not_exempt(monkeypatch, question):
    # With the word list gone, business questions whose wording brushes against
    # ingestion vocabulary ("arrived", "stale", "written off") no longer open
    # the storage window: only the explicit time_basis arg does.
    sql = f"SELECT COUNT(*) FROM events WHERE created >= {_DAY} AND created < {_NEXT}"
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql], question=question)
    assert payload.get("reason") == storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE


def test_both_business_and_storage_bounds_are_refused_by_the_partition_rule(monkeypatch):
    # A dual bound (business time AND storage column) is the established
    # partition rule's case: a storage bound the question did not ask for can
    # drop completions written on other days, so the competing-bound refusal is
    # intended. The business-time rule defers; the competing rule refuses.
    sql = (f"SELECT COUNT(*) FROM events WHERE created >= {_DAY} AND created < {_NEXT} "
           f"AND complete_time >= {_DAY} AND complete_time < {_NEXT}")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload.get("error_name") == "STORAGE_BOUND"
    assert payload.get("reason") == storage_bound_rule.REASON_COMPETING


def test_the_capture_keeps_every_write_time_basis_column_of_a_multi_read_result():
    import agents.tool_result_capture as trc

    result = {"storage_bound": [
        {"table": "a", "columns": ["created"], "conjuncts": [], "basis": "ingestion"},
        {"table": "b", "columns": ["loaded_at"], "conjuncts": [], "basis": "ingestion"},
        {"table": "c", "columns": ["created"], "conjuncts": [], "basis": "ingestion"},
    ]}
    assert trc.ingestion_basis_record(result) == {"columns": ["created", "loaded_at"]}


def test_an_s3tables_ingestion_run_carries_one_storage_record_marked_with_its_basis():
    # The business-time rule's ingestion record and the DuckDB storage check
    # describe the same bound: the result keeps one, marked with its basis.
    import json

    from . import test_s3tables_guard_e2e as s3

    s3.storage_bounds.remember(
        s3.SYS, {"segments": ["observed_at"]},
        {"segments": ["id", "observed_at", "done_at", "route_name"]})
    s3.state_times.remember(s3.SYS, {"segments": (
        s3.state_times.StateTimes(
            "segments", "state", (("COMPLETED", "done_at"),),
            ("observed_at", "done_at")),)})
    s3.row_exclusion.remember(s3.SYS, {})
    run = s3._run_tool()
    token = sa._MODE_A_CALL.set(sa._ModeACall("How many rows were ingested?"))
    try:
        out = json.loads(run(
            "SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'",
            time_basis="ingestion"))
    finally:
        sa._MODE_A_CALL.reset(token)
    record = out.get("storage_bound")
    assert isinstance(record, dict), out
    assert record.get("basis") == "ingestion"
    assert record.get("columns") == ["observed_at"]


def test_business_time_refusal_counts_every_withheld_name_once():
    """A business-time refusal whose business column is not a plain identifier
    withholds it, counts it in names_withheld, and says so in ONE sentence."""
    bound = storage_bound_rule.StorageBound(
        table="segments", storage=("data_creation_date",),
        other=("bad name;--",), conjuncts=())
    err = storage_bound_rule.StorageBoundError(
        bound, ("data_creation_date",), ("bad name;--",),
        reason=storage_bound_rule.REASON_BUSINESS_TIME_ON_STORAGE)
    payload = err.to_dict()
    assert "bad name;--" not in payload["guidance"]
    assert payload["names_withheld"] == 1
    assert payload["guidance"].count("withheld because") == 1
