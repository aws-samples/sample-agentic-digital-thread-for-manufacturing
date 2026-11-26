# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A record's own time column (a struct sibling), and a window on another time.

Measured on an analytics deployment, in a live dry run: asked for a daily
count of quality defects by a defect reason code over a window, a SQL system
agent defined its population by the defect code column (a flattened struct
field ``quality_data.defect_type``), pinned ``state = 'FAILED'``, and windowed
on a lifecycle time (``fail_date``) the question did not name. The defect
record carries its OWN time of its own, the one sibling timestamp under its
struct (``quality_data.defect_detected_date``); on that column the same defect
is tens of thousands, not three. The STATE TIME check does not fire (it counts
FAILED rows on FAILED's own column), and nothing paired the defect field with
its sibling timestamp.

The engine now enforces it in a Mode A call's guarded Athena tool:

* ``record_time.own_time_column`` is the schema fact: a flattened column
  ``a.b.c`` has an own time column when exactly one date/timestamp column is
  named ``a.b.<x>`` (one sibling under the same parent struct); a top-level
  column has none, and two sibling timestamps are ambiguous;
* a query that constrains a column with an own time column (``=``, ``IN``,
  ``LIKE``, ``IS NOT NULL``, ``<> ''``, ``<> '[]'``) and whose only bounded
  time columns are not that own time, whatever the question says, is refused
  before it runs with a remedy (window on the record's own time, or send it
  again for that window); the identical query sent again runs, and the answer then carries an engine line and a
  FINAL-ANSWER REQUIREMENTS block, as facts;
* the check reads the SQL that will run, after the scan guard adds its own
  partition predicate; under enforce the first send is refused instead of
  disclosed.

Not refused: the query windows on the record's own time, the population
column has no sibling time, no other time is bounded, more than one table,
anything uncertain. The question's wording never exempts a window. Fully
offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.orchestrator as orch
import agents.system_agent as sa
from agents import storage_bounds

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_TS = "timestamp(6) with time zone"
# A flattened struct: quality_data.defect_type (the population column) with one
# sibling timestamp (its own time), plus unrelated lifecycle times.
_COLUMNS = {"id": "varchar", "state": "varchar",
            "quality_data.defect_type": "varchar",
            "quality_data.defect_detected_date": _TS,
            "fail_date": _TS, "complete_date": _TS, "created": "timestamp(6)",
            "line": "varchar"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED; "
         "quality_data.defect_type: varchar e.g. TypeA, TypeB; "
         f"quality_data.defect_detected_date: {_TS}; "
         f"fail_date: {_TS}; complete_date: {_TS}; created: timestamp(6); line: varchar)")
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"


def _on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


# The live Q3 shape, neutral: population by the defect code, windowed on a
# lifecycle time the question did not name, state pinned on top.
_DEFECT_ON_FAIL = ("SELECT COUNT(*) AS n FROM events WHERE "
                   f"{_on('fail_date')} AND state = 'FAILED' "
                   "AND quality_data.defect_type IS NOT NULL "
                   "AND LOWER(quality_data.defect_type) LIKE '%typea%'")
_DEFECT_ON_OWN = ("SELECT COUNT(*) AS n FROM events WHERE "
                  f"{_on('quality_data.defect_detected_date')} "
                  "AND quality_data.defect_type IS NOT NULL "
                  "AND LOWER(quality_data.defect_type) LIKE '%typea%'")
_QUESTION = "What is the daily count of defects with reason code TypeA on 1/5?"


def _rt():
    return importlib.import_module("agents.record_time")


class _Lake:
    """One Iceberg table behind GetTableMetadata and a query engine that gives
    every data query one row (``$partitions`` answers the partition fields)."""

    def __init__(self, columns=None, fields=("created_day",)):
        self.columns = dict(columns or _COLUMNS)
        self.fields = fields
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events", "Columns": [{"Name": n, "Type": t} for n, t in self.columns.items()],
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
            names, rows = ["partition"], [["{" + ", ".join(f"{f}=1" for f in self.fields) + "}"]]
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in self.columns.items()]
        else:
            names, rows = ["n"], [["42"]]
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
    _rt().reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    _rt().reset()


def _ask(monkeypatch, lake, sqls, *, question=_QUESTION, kg_context="", desc=_DESC):
    """One Mode A call on a system baked with ``desc`` whose agent sends
    ``sqls`` to the guarded Athena tool the way strands does. Returns
    ``(tool payloads, final answer)``."""
    sa._remember_turn_time_facts("system-a", [], desc)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "The count is 42.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", question, kg_context))
    return payloads, answer


# --------------------------------------------------------------------------- #
# The helper: a flattened column's own sibling time column
# --------------------------------------------------------------------------- #
def test_a_unique_sibling_timestamp_is_the_own_time_column():
    rt = _rt()
    assert rt.own_time_column("quality_data.defect_type", _COLUMNS) \
        == "quality_data.defect_detected_date"


def test_two_sibling_timestamps_give_no_own_time_column():
    rt = _rt()
    columns = {**_COLUMNS, "quality_data.defect_cleared_date": _TS}
    assert rt.own_time_column("quality_data.defect_type", columns) is None


def test_a_top_level_column_has_no_own_time_column():
    assert _rt().own_time_column("state", _COLUMNS) is None


def test_a_sibling_that_is_not_a_timestamp_is_not_an_own_time_column():
    rt = _rt()
    columns = {"q.a": "varchar", "q.b": "varchar"}
    assert rt.own_time_column("q.a", columns) is None


def test_a_deeper_sibling_under_the_same_prefix_is_not_paired():
    """The own time is exactly one more segment, not a grandchild."""
    rt = _rt()
    columns = {"a.b.c": "varchar", "a.b.d.e": _TS, "a.b.t": _TS}
    assert rt.own_time_column("a.b.c", columns) == "a.b.t"


# --------------------------------------------------------------------------- #
# Refused before it runs
# --------------------------------------------------------------------------- #
def test_the_live_defect_shape_windowed_on_a_lifecycle_time_is_refused(monkeypatch):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_DEFECT_ON_FAIL])
    assert payload["error_type"] == "invalid_request"
    assert payload["reason"] == "window_not_on_record_time"
    assert payload["retryable"] is True
    assert payload["population_column"] == "quality_data.defect_type"
    assert payload["own_column"] == "quality_data.defect_detected_date"
    assert payload["window_column"] == "fail_date"
    guidance = payload["guidance"]
    assert "window on quality_data.defect_detected_date" in guidance
    assert "name the time you mean" not in guidance
    assert "send the same query again unchanged" in guidance


def test_a_self_join_of_the_record_time_table_is_now_refused(monkeypatch):
    # closed gap: a multi-base-table block (self-join) of a table
    # with a record-time rule can no longer bypass the rule on Athena.
    sql = ("SELECT COUNT(*) FROM events e JOIN events f ON e.id = f.id WHERE "
           f"e.{_on('fail_date')} AND e.quality_data.defect_type IS NOT NULL")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["error_type"] == "invalid_request"
    assert payload["error_name"] == "MULTI_RELATION_RULE_READ"
    assert "events" in payload.get("tables", [])


def test_the_remedy_reads_as_a_fixable_query_not_an_outage(monkeypatch):
    from agents.chat_client import classify_error_text
    from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_DEFECT_ON_FAIL])
    text = payload["guidance"].lower()
    for marker in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS):
        assert marker not in text
    assert classify_error_text(payload["guidance"]) != "timeout"
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


def test_an_equality_on_the_population_column_also_fires(monkeypatch):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_on('complete_date')} AND quality_data.defect_type = 'TypeA'")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["reason"] == "window_not_on_record_time"
    assert payload["window_column"] == "complete_date"


def test_an_empty_string_guard_on_the_population_column_fires(monkeypatch):
    sql = ("SELECT COUNT(*) AS n FROM events WHERE "
           f"{_on('fail_date')} AND quality_data.defect_type <> ''")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["reason"] == "window_not_on_record_time"


def test_under_enforce_the_first_send_is_refused(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_DEFECT_ON_FAIL])
    assert payload["reason"] == "window_not_on_record_time"
    assert lake.data_queries() == []


# --------------------------------------------------------------------------- #
# The repeat runs, and the answer discloses it
# --------------------------------------------------------------------------- #
def test_an_identical_repeat_runs_and_the_answer_discloses_it(monkeypatch):
    lake = _Lake()
    (first, second), answer = _ask(monkeypatch, lake,
                                   [_DEFECT_ON_FAIL, f"\n{_DEFECT_ON_FAIL}  "])
    assert first["reason"] == "window_not_on_record_time"
    assert second["rows"] == [["42"]]
    assert second["record_time_window"]["window_column"] == "fail_date"
    assert len(lake.data_queries()) == 1
    assert "other than record time" in answer
    assert ("counted by fail_date, not by the record's own time "
            "quality_data.defect_detected_date") in answer
    block = answer[answer.index("----- BEGIN FINAL-ANSWER REQUIREMENTS -----"):]
    assert "quality_data.defect_detected_date" in block
    assert answer.index("other than record time") < answer.index(
        "----- BEGIN FINAL-ANSWER REQUIREMENTS -----")
    for claim in ("wrong", "should", "instead", "leaves out"):
        assert claim not in answer[answer.index("[engine] Record time"):]


def test_a_call_with_no_refusal_carries_no_record_time_line(monkeypatch):
    (payload,), answer = _ask(monkeypatch, _Lake(), [_DEFECT_ON_OWN])
    assert payload["rows"] == [["42"]] and "record_time_window" not in payload
    assert "record time" not in answer and "FINAL-ANSWER REQUIREMENTS" not in answer


def test_model_text_that_reads_as_the_record_time_line_is_made_inert(monkeypatch):
    rt = _rt()
    forged = f"Done.\n{rt.ENGINE_PREFIX} the question did not name: anything."
    assert rt.ENGINE_PREFIX not in sa._rule_decline_of(forged)[0]


# --------------------------------------------------------------------------- #
# Not refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("sql", "question", "desc", "columns"), [
    # the window is the record's own sibling time
    (_DEFECT_ON_OWN, _QUESTION, _DESC, None),
    # the population column has no sibling timestamp (the sibling is text)
    (_DEFECT_ON_FAIL, _QUESTION,
     ("events(id: varchar; quality_data.defect_type: varchar e.g. TypeA; "
      f"quality_data.defect_note: varchar; fail_date: {_TS})"),
     {"id": "varchar", "quality_data.defect_type": "varchar",
      "quality_data.defect_note": "varchar", "fail_date": _TS}),
    # no other time column is bounded (only the population constraint, no window)
    ("SELECT COUNT(*) AS n FROM events WHERE quality_data.defect_type IS NOT NULL",
     _QUESTION, _DESC, None),
    # two sibling timestamps: ambiguous, no own time column
    (_DEFECT_ON_FAIL, _QUESTION,
     ("events(id: varchar; quality_data.defect_type: varchar e.g. TypeA; "
      f"quality_data.defect_detected_date: {_TS}; quality_data.defect_cleared_date: {_TS}; "
      f"fail_date: {_TS})"),
     {"id": "varchar", "quality_data.defect_type": "varchar",
      "quality_data.defect_detected_date": _TS, "quality_data.defect_cleared_date": _TS,
      "fail_date": _TS}),
    # the constraint on the population column is under an OR
    ("SELECT COUNT(*) AS n FROM events WHERE "
     f"{_on('fail_date')} AND (quality_data.defect_type = 'TypeA' OR line = 'l1')",
     _QUESTION, _DESC, None),
    # the population column itself is a top-level column with no struct parent
    (f"SELECT COUNT(*) AS n FROM events WHERE {_on('fail_date')} AND state = 'FAILED'",
     _QUESTION, _DESC, None),
])
def test_what_is_not_refused_runs(monkeypatch, sql, question, desc, columns):
    lake = _Lake(columns)
    (payload,), answer = _ask(monkeypatch, lake, [sql], question=question, desc=desc)
    assert "error" not in payload, payload
    assert payload["rows"] == [["42"]] and "record_time_window" not in payload
    assert lake.data_queries() and "other than record time" not in answer


# --------------------------------------------------------------------------- #
# The SQL that runs, after the scan guard's own partition predicate
# --------------------------------------------------------------------------- #
def test_an_auto_scoped_window_on_another_time_is_refused(monkeypatch):
    """The scan guard adds a recent-partition predicate on fail_date to an
    otherwise unwindowed query; the rule reads the scoped SQL."""
    recent = "\"fail_date\" IN (TIMESTAMP '2026-01-05 00:00:00 UTC')"
    monkeypatch.setattr(at, "list_partition_columns",
                        lambda *_a, **_k: [{"name": "fail_date", "data_type": _TS}])
    monkeypatch.setattr(at, "build_recent_partition_predicate", lambda *_a, **_k: recent)
    sql = ("SELECT COUNT(*) AS n FROM events WHERE quality_data.defect_type IS NOT NULL "
           "AND LOWER(quality_data.defect_type) LIKE '%typea%'")
    (payload,), _answer = _ask(monkeypatch, _Lake(), [sql])
    assert payload["reason"] == "window_not_on_record_time"
    assert payload["window_column"] == "fail_date"


# --------------------------------------------------------------------------- #
# The connector alone never applies the rule; the orchestrator registers it
# --------------------------------------------------------------------------- #
def test_the_connector_alone_never_applies_the_rule():
    sa._remember_turn_time_facts("system-a", [], _DESC)
    result = at.run_athena_query_exact(_DEFECT_ON_FAIL, _CFG, source_system="system-a",
                                       client=_Lake())
    assert result["rows"] == [["42"]]


def test_the_orchestrator_registers_the_record_time_marker():
    prompt = orch.ORCHESTRATOR_PROMPT
    rule = prompt[prompt.index("EXECUTED FILTERS:"):]
    rule = " ".join(rule[:rule.index("\n\n") if "\n\n" in rule else len(rule)].split())
    assert "other than record time" in rule
    assert "FINAL-ANSWER REQUIREMENTS block" in rule


# --------------------------------------------------------------------------- #
# The mapping gate, from the schema only
# --------------------------------------------------------------------------- #
def test_the_gate_is_true_only_when_a_sibling_pairing_exists():
    rt = _rt()
    rt.reset()
    rt.remember_tables_desc("system-a", _DESC)
    assert rt.has_types("system-a") is True
    rt.remember_tables_desc("system-b",
                            f"events(id: varchar; state: varchar; fail_date: {_TS})")
    assert rt.has_types("system-b") is False


# =========================================================================== #
# The production catalog shape, natural temporal phrasings, and the per-rule
# retry gate.
# =========================================================================== #
class _NestedLake(_Lake):
    """A table whose GetTableMetadata returns the struct as ONE top-level
    ``row(...)`` column, the way the live catalog does, NOT pre-flattened to
    dotted leaves. ``information_schema`` (the fallback) returns the same
    unflattened rows. Only ``flatten_athena_columns`` turns the ``row(...)``
    into ``quality_data.defect_type`` / ``quality_data.defect_detected_date``.
    """

    def __init__(self, fields=("created_day",)):
        super().__init__(fields=fields)
        self.meta_columns = {
            "id": "varchar", "state": "varchar",
            "entity_type_details": (
                "row(quality_data row(defect_type varchar, "
                f"defect_detected_date {_TS}), other varchar)"),
            "fail_date": _TS, "complete_date": _TS, "created": "timestamp(6)",
            "line": "varchar"}

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events",
            "Columns": [{"Name": n, "Type": t} for n, t in self.meta_columns.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in self.meta_columns.items()]
            header = {"Data": [{"VarCharValue": n} for n in names]}
            return {"ResultSet": {
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
                "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]}
                                    for row in rows]}}
        return super().get_query_results(QueryExecutionId, **_kwargs)


# The schema as the model sees it after onboarding flattens the struct: the
# baked desc and the SQL both spell the struct field with dots.
_NESTED_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED; "
                "entity_type_details.quality_data.defect_type: varchar e.g. TypeA, TypeB; "
                f"entity_type_details.quality_data.defect_detected_date: {_TS}; "
                f"fail_date: {_TS}; complete_date: {_TS}; created: timestamp(6); line: varchar)")
_NESTED_SQL = ("SELECT COUNT(*) AS n FROM events WHERE "
               f"{_on('fail_date')} AND state = 'FAILED' "
               "AND entity_type_details.quality_data.defect_type IS NOT NULL "
               "AND LOWER(entity_type_details.quality_data.defect_type) LIKE '%typea%'")


def test_a_production_nested_row_struct_fires(monkeypatch):
    """HIGH 1: the live Q3 trap arrives as ONE catalog ``row(...)`` column.
    The rule must still pair the struct field with its sibling timestamp and
    refuse, which it cannot do unless the catalog types are flattened before
    the decision reads them."""
    (payload,), _answer = _ask(monkeypatch, _NestedLake(), [_NESTED_SQL],
                               desc=_NESTED_DESC)
    assert payload["reason"] == "window_not_on_record_time"
    assert payload["population_column"] \
        == "entity_type_details.quality_data.defect_type"
    assert payload["own_column"] \
        == "entity_type_details.quality_data.defect_detected_date"
    assert payload["window_column"] == "fail_date"


# --------------------------------------------------------------------------- #
# The question's wording never exempts the window. Word rules could not tell
# naming a time from rejecting it (every negation or rejection vocabulary had
# another ordinary phrasing outside it), so the refusal fires on the query
# alone, and a question that does want the other window gets it on resend.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    # names the window column, in natural words or physically
    "How many TypeA defects by failure date on 1/5?",
    "How many TypeA defects by when they failed on 1/5?",
    "How many TypeA defects with a fail_date on 1/5?",
    # names the record's own time while the query windows on another
    "How many TypeA defects by defect detected date on 1/5?",
    "How many defects of reason TypeA by quality_data.defect_detected_date on 1/5?",
    # rejects the window, in forms no word list covered
    "How many defects excluding failure date on 1/5?",
    "How many defects while avoiding failure date on 1/5?",
    "How many defects using neither failure nor completion date on 1/5?",
    "How many defects? Ignore failure date and use detection date on 1/5.",
    "How many defects where failure date is not the intended window on 1/5?",
    "How many defects except those that were recorded in the original system "
    "by failure date on 1/5?",
])
def test_the_question_wording_never_exempts_the_window(monkeypatch, question):
    """Whatever the question says about time, a window on a time other than
    the record's own is refused on first send."""
    lake = _Lake()
    (payload,), _answer = _ask(monkeypatch, lake, [_DEFECT_ON_FAIL], question=question)
    assert payload["reason"] == "window_not_on_record_time"
    assert lake.data_queries() == []


@pytest.mark.parametrize("question", [
    "How many TypeA defects by failure date on 1/5?",
    "How many TypeA defects with a fail_date on 1/5?",
])
def test_a_question_that_wants_that_window_gets_it_on_resend(monkeypatch, question):
    """The cost when the question did want the other window is one refusal:
    the identical resend runs, and the answer says how the records were
    counted."""
    lake = _Lake()
    (first, second), answer = _ask(
        monkeypatch, lake, [_DEFECT_ON_FAIL, _DEFECT_ON_FAIL], question=question)
    assert first["reason"] == "window_not_on_record_time"
    assert "error" not in second, second
    assert second["rows"] == [["42"]]
    assert "other than record time" in answer
    assert "the question did not name" not in answer


def test_the_engine_line_does_not_claim_the_question_left_the_time_out():
    """The line is facts about the query, not a reading of the question."""
    from agents import record_time
    window = record_time.RecordTimeWindow(
        "events", "quality_data.defect_type", "quality_data.defect_detected_date",
        ("fail_date",), ("fail_date >= TIMESTAMP '2026-01-05'",), refuse=True)
    line = record_time.engine_line([window])
    assert "the question did not name" not in line
    assert "not asked for" not in line
    assert record_time.MARKER in line


# --------------------------------------------------------------------------- #
# The retry gate is per (rule, SQL). A query that trips two rules is
# refused once with BOTH remedies, and the retry runs disclosing both, with
# no rule ever bypassed and no silent run under enforce.
# --------------------------------------------------------------------------- #
# COMPLETED rows (state-time: windowed on FAILED's own time fail_date) whose
# population is the defect field (record-time: own time is the sibling). One
# query, two rules.
_BOTH_RULES_SQL = ("SELECT COUNT(*) AS n FROM events WHERE "
                   f"{_on('fail_date')} AND state = 'COMPLETED' "
                   "AND quality_data.defect_type IS NOT NULL "
                   "AND LOWER(quality_data.defect_type) LIKE '%typea%'")


@pytest.fixture
def _fresh_state():
    from agents import state_times
    state_times.reset()
    yield
    state_times.reset()


def test_one_first_send_refusal_carries_every_remedy(monkeypatch, _fresh_state):
    """The query trips state-time and record-time. The FIRST send is refused
    once with BOTH remedies (one round trip, not one per rule), and the
    identical resend runs. No rule is bypassed: both remedies were shown before
    the query ran."""
    lake = _Lake()
    (first, second), _answer = _ask(monkeypatch, lake, [_BOTH_RULES_SQL, _BOTH_RULES_SQL])
    assert "error" in first
    assert {r["reason"] for r in first["also_refused"]} | {first["reason"]} == {
        "window_on_other_state_time", "window_not_on_record_time"}
    # the record-own-time remedy (the sibling time) and the state-time remedy
    # (the state's own time) are both in the one refusal
    assert "quality_data.defect_detected_date" in first["guidance"]
    assert "fail_date" in first["guidance"]
    assert second["rows"] == [["42"]]
    assert len(lake.data_queries()) == 1


def test_both_remedies_are_disclosed_when_the_query_runs(monkeypatch, _fresh_state):
    lake = _Lake()
    payloads, answer = _ask(monkeypatch, lake, [_BOTH_RULES_SQL, _BOTH_RULES_SQL])
    assert payloads[-1]["rows"] == [["42"]]
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer
    assert "other than record time" in answer


def test_neither_rule_is_bypassed_or_silent_under_enforce(monkeypatch, _fresh_state):
    """Under enforce no engine lines are appended, so a rule that never showed
    its own remedy must not let the query run silently. The bug: one rule
    refuses on a shared key and the identical retry suppresses the other,
    running it without its remedy ever shown. Here the first send is refused
    with every applicable remedy, so the resend that runs has shown them all."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    lake = _Lake()
    (first, second), _answer = _ask(monkeypatch, lake, [_BOTH_RULES_SQL, _BOTH_RULES_SQL])
    assert "error" in first
    assert "quality_data.defect_detected_date" in first["guidance"]
    assert "fail_date" in first["guidance"]
    assert second.get("rows") == [["42"]]
    assert len(lake.data_queries()) == 1


def test_a_rule_new_on_the_resend_is_still_refused(monkeypatch, _fresh_state):
    """Rules already refused for this SQL are not refused again, but a rule
    that fires only now must still show its remedy before the query runs."""
    from agents import system_agent

    class _Call:
        def __init__(self):
            self.marked = set()

        def refused_rules(self, _sql):
            return frozenset(self.marked)

        def mark_refused(self, _sql, rule=""):
            self.marked.add(rule)

    class _Err(Exception):
        pass

    call = _Call()
    call.marked.add("state")
    try:
        system_agent._enforce_window_refusals("q", call, [("state", _Err("s")), ("record", _Err("r"))])
    except _Err as exc:
        assert str(exc) == "r"
    else:
        raise AssertionError("the new rule was not refused")
    assert call.marked == {"state", "record"}
    system_agent._enforce_window_refusals("q", call, [("state", _Err("s")), ("record", _Err("r"))])
