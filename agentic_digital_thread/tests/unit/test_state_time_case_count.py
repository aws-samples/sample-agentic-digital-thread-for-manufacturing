# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A state counted inside a conditional aggregate is read as a counted state.

Measured on an analytics deployment, in dry run 13 Q2: a query counted FAILED
rows with ``COUNT(CASE WHEN state = 'FAILED' THEN 1 END)`` beside ``COUNT(*)``,
grouped by another column, windowed on ``complete_date`` (COMPLETED's own time
column). The state column was in neither the WHERE nor the GROUP BY, so the
state-time check read no counted state and did not refuse; the failed figure
came back on the wrong time column.

The engine now reads a state counted inside a conditional aggregate of the
counting block's SELECT list as a counted state, and decides it with the
existing rules:

* ``COUNT(CASE WHEN c THEN x [ELSE NULL] END)``,
  ``COUNT(DISTINCT CASE WHEN c THEN x END)``,
  ``SUM(CASE WHEN c THEN <positive number> [ELSE 0 | ELSE NULL] END)``,
  ``COUNT(...) FILTER (WHERE c)``, ``COUNT_IF(c)`` and the Trino ``IF(c, ...)``
  forms each count the states the ``=`` / ``IN`` pins of the state column in
  the top-level AND of ``c`` name; a non-count aggregate (MAX, MIN, AVG of a
  CASE) is never read;
* a counting branch that does not pin the state column, an ELSE that counts,
  or the state column named in ``c`` in any other form (OR, NOT, ``<>``,
  computed) leaves that aggregate unread (no decision from it);
* Q2's shape (an unconditional ``COUNT(*)`` of the window's own state beside a
  conditional count of another state) is a breakdown: refused once even when
  the question names the window's state word; the resend runs disclosed;
* a window on a column no state owns now counts conditionally counted states
  too: two or more such states are refused once, one is a cohort and runs.

Fully offline; synthetic names, on the logged table's shape and on a
differently named ``job_runs`` table to prove it generalizes.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import storage_bounds

_TS = "timestamp(6) with time zone"
_COLUMNS = {"id": "varchar", "state": "varchar", "start_time": _TS, "complete_time": _TS,
            "fail_time": _TS, "cancel_time": _TS, "created": "timestamp(6)",
            "cell": "varchar", "duration_s": "bigint"}
_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
         f"start_time: {_TS}; complete_time: {_TS}; fail_time: {_TS}; cancel_time: {_TS}; "
         "created: timestamp(6); cell: varchar e.g. c1, c2; duration_s: bigint)")
_DAY = "TIMESTAMP '2026-01-05 00:00:00 UTC'"
_NEXT = "TIMESTAMP '2026-01-06 00:00:00 UTC'"
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}


def _on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


# The logged Q2 shape: COUNT(*) plus COUNT(CASE WHEN state = 'FAILED' ...),
# grouped by another column, windowed on COMPLETED's own time column.
_Q2 = (f"SELECT COUNT(*) AS segment_count, "
       f"COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS failed_segments, cell "
       f"FROM events WHERE {_on('complete_time')} GROUP BY cell")
_COMPLETED_Q = "How many completed segments by cell on 1/5, and the failed count?"

# A differently named schema: job_runs(status FINISHED/ABORTED/QUEUED).
_JR_COLUMNS = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "started_time": _TS, "updated_time": _TS,
               "line": "varchar", "elapsed_s": "bigint"}
_JR_DESC = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED; "
            f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; started_time: {_TS}; "
            f"updated_time: {_TS}; line: varchar e.g. a, b; elapsed_s: bigint)")


def _jr_on(column: str) -> str:
    return f"{column} >= {_DAY} AND {column} < {_NEXT}"


# ABORTED counted in a CASE, windowed on FINISHED's own time column.
_JR_CASE = (f"SELECT COUNT(*) AS n, "
            f"COUNT(CASE WHEN status = 'ABORTED' THEN 1 END) AS aborted, line "
            f"FROM job_runs WHERE {_jr_on('finish_time')} GROUP BY line")


def _st():
    return importlib.import_module("agents.state_times")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    try:
        _st().reset()
    except ModuleNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# A decide()-level helper: fast, exact, no Mode A machinery.
# --------------------------------------------------------------------------- #
def _decide(sql, *, question, table="events", desc=_DESC, columns=_COLUMNS):
    from agents import storage_bound_rule
    st = _st()
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    mapping = st.from_tables_desc(desc)[table]
    return st.decide(sql, table, bounded, columns, mapping, question=question)


def _record(found) -> dict:
    return found.to_dict()


# --------------------------------------------------------------------------- #
# Q2's shape: a breakdown, refused once even when the question says "completed"
# --------------------------------------------------------------------------- #
def test_q2_shape_counts_the_conditional_state_and_is_a_breakdown():
    found = _decide(_Q2, question=_COMPLETED_Q)
    assert found is not None
    rec = _record(found)
    assert rec["window_column"] == "complete_time"
    assert rec["own_columns"] == {"FAILED": "fail_time"}
    # COUNT(*) counts COMPLETED's own rows beside the conditional FAILED count:
    # a breakdown, refused once even though the question names "completed".
    assert found.refuse is True


def test_q2_shape_generalizes_to_a_differently_named_schema():
    found = _decide(_JR_CASE, question="How many finished runs by line on 1/5, and aborted?",
                    table="job_runs", desc=_JR_DESC, columns=_JR_COLUMNS)
    assert found is not None
    rec = _record(found)
    assert rec["window_column"] == "finish_time"
    assert rec["own_columns"] == {"ABORTED": "abort_time"}
    assert found.refuse is True


_FORMS = [
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS c",
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 ELSE NULL END) AS c",
    "COUNT(CASE WHEN state = 'FAILED' THEN id END) AS c",
    "COUNT(DISTINCT CASE WHEN state = 'FAILED' THEN id END) AS c",
    "SUM(CASE WHEN state = 'FAILED' THEN 1 END) AS c",
    "SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS c",
    "COUNT(*) FILTER (WHERE state = 'FAILED') AS c",
    "COUNT_IF(state = 'FAILED') AS c",
    "COUNT(IF(state = 'FAILED', 1)) AS c",
    "SUM(IF(state = 'FAILED', 1, 0)) AS c",
    "COUNT(CASE WHEN state IN ('FAILED') THEN 1 END) AS c",
]


@pytest.mark.parametrize("item", _FORMS)
def test_every_counting_form_reads_the_conditional_state(item):
    """Each supported conditional count of FAILED, beside COUNT(*), on
    COMPLETED's own column, is a breakdown refused once."""
    sql = f"SELECT COUNT(*) AS n, {item}, cell FROM events WHERE {_on('complete_time')} GROUP BY cell"
    found = _decide(sql, question=_COMPLETED_Q)
    assert found is not None, item
    assert _record(found)["own_columns"] == {"FAILED": "fail_time"}, item
    assert found.refuse is True, item


_NON_COUNTS = [
    "MAX(CASE WHEN state = 'FAILED' THEN duration_s END) AS c",
    "MIN(CASE WHEN state = 'FAILED' THEN duration_s END) AS c",
    "AVG(CASE WHEN state = 'FAILED' THEN duration_s END) AS c",
]


@pytest.mark.parametrize("item", _NON_COUNTS)
def test_a_non_count_aggregate_of_a_case_is_not_read(item):
    """MAX/MIN/AVG of a CASE is not a count of rows, so the state inside it is
    never read. With only COUNT(*) of COMPLETED's own rows on COMPLETED's own
    column, there is no window on another state's time: it runs."""
    sql = f"SELECT COUNT(*) AS n, {item}, cell FROM events WHERE {_on('complete_time')} GROUP BY cell"
    assert _decide(sql, question=_COMPLETED_Q) is None, item


# --------------------------------------------------------------------------- #
# The window's own state only, pinned away, OR/NOT/<>, WHERE names own column
# --------------------------------------------------------------------------- #
def test_a_conditional_count_of_the_windows_own_state_only_runs():
    """A conditional count of COMPLETED on COMPLETED's own column, with no
    other state counted, is a cohort of the window's own rows: no window on
    another state's time, so it runs (no decision)."""
    sql = (f"SELECT COUNT(CASE WHEN state = 'COMPLETED' THEN 1 END) AS c, cell "
           f"FROM events WHERE {_on('complete_time')} GROUP BY cell")
    assert _decide(sql, question=_COMPLETED_Q) is None


def test_a_conditional_count_pinned_away_by_the_where_adds_nothing():
    """The WHERE pins the state column to COMPLETED; the conditional FAILED
    count is intersected away by that pin, so the only counted state is
    COMPLETED, on its own column: it runs."""
    sql = (f"SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS c, cell FROM events "
           f"WHERE state = 'COMPLETED' AND {_on('complete_time')} GROUP BY cell")
    assert _decide(sql, question=_COMPLETED_Q) is None


def test_a_multi_branch_case_that_both_pins_is_a_union():
    """Both WHEN branches pin the state column: the aggregate counts the union
    of their states. FAILED and STARTED both have their own time columns other
    than COMPLETED's, so the breakdown is refused once."""
    sql = ("SELECT COUNT(*) AS n, "
           "COUNT(CASE WHEN state = 'FAILED' THEN 1 WHEN state = 'STARTED' THEN 1 END) AS c, cell "
           f"FROM events WHERE {_on('complete_time')} GROUP BY cell")
    found = _decide(sql, question=_COMPLETED_Q)
    assert found is not None
    assert _record(found)["own_columns"] == {"FAILED": "fail_time", "STARTED": "start_time"}
    assert found.refuse is True


@pytest.mark.parametrize("branch", [
    # a counting branch that does not pin the state column
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 WHEN duration_s > 0 THEN 1 END) AS c",
    # a counting ELSE
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 ELSE 1 END) AS c",
    # the state column under NOT
    "COUNT(CASE WHEN NOT (state = 'COMPLETED') THEN 1 END) AS c",
    # the state column compared with <>
    "COUNT(CASE WHEN state <> 'COMPLETED' THEN 1 END) AS c",
    # a computed state value
    "COUNT(CASE WHEN lower(state) = 'failed' THEN 1 END) AS c",
])
def test_a_conditional_aggregate_the_reader_cannot_read_is_not_read(branch):
    """When a conditional aggregate does not pin the state column cleanly, it
    adds no counted state. With only COUNT(*) of COMPLETED's own rows on
    COMPLETED's own column, the query runs."""
    sql = f"SELECT COUNT(*) AS n, {branch}, cell FROM events WHERE {_on('complete_time')} GROUP BY cell"
    assert _decide(sql, question=_COMPLETED_Q) is None, branch


@pytest.mark.parametrize("branch", [
    "COUNT(CASE WHEN state = 'FAILED' OR state = 'STARTED' THEN 1 END)",
    "COUNT_IF(state = 'FAILED' OR state = 'STARTED')",
])
def test_round2_a_state_column_or_inside_the_condition_reads_like_its_or_pinned(branch):
    """Round 2 (reported change from r1): an OR of state pins inside the
    condition is now read, by parity with its OR-pinned equivalent (the same
    OR carried into the WHERE, which `_decide_or` decides). r1 left it unread.
    Compared as the lone aggregate. (The base engine's `_decide_or` treats an
    OR of state pins differently from an `IN` list, a pre-existing asymmetry,
    so the faithful parity partner is the OR-pinned form, not the `IN` form.)
    """
    cond = f"SELECT {branch} AS c FROM events WHERE {_on('complete_time')}"
    or_pinned = (f"SELECT COUNT(*) FROM events WHERE (state = 'FAILED' OR state = 'STARTED') "
                 f"AND {_on('complete_time')}")
    got = _flag(_decide(cond, question=_COMPLETED_Q))
    assert got is not None and got == _flag(_decide(or_pinned, question=_COMPLETED_Q))


def test_the_where_naming_a_counted_states_own_column_is_not_refused():
    """The WHERE mentions FAILED's own column (any form): as today, the query
    is not refused for the conditional FAILED count either."""
    sql = (f"SELECT COUNT(*) AS n, COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS c, cell "
           f"FROM events WHERE {_on('complete_time')} AND fail_time IS NOT NULL GROUP BY cell")
    assert _decide(sql, question=_COMPLETED_Q) is None


# --------------------------------------------------------------------------- #
# A window on a column no state owns: conditional states count too
# --------------------------------------------------------------------------- #
_UNOWNED_COLUMNS = {**_COLUMNS, "updated_time": _TS}
_UNOWNED_DESC = _DESC.replace("created: timestamp(6); ",
                              f"created: timestamp(6); updated_time: {_TS}; ")


def test_two_single_state_conditional_counts_on_an_unowned_column_are_a_breakdown():
    """R3-2 (follow-up): two single-state conditional counts on a column no
    state owns are a query-level breakdown of those two states on one column
    belonging to neither. The governing parity partner is the IN-pin / GROUP
    BY of the same two states (one aggregate over both), which is refused, not
    the per-aggregate single-state cohort. A prior round asserted the pair ran
    (per-aggregate parity only); R3-2 corrected that to query-level parity, so
    the conditional pair is now refused exactly as its IN-pin equivalent is."""
    cond = ("SELECT COUNT(CASE WHEN state = 'COMPLETED' THEN 1 END) AS a, "
            "COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS b, cell "
            f"FROM events WHERE {_on('updated_time')} GROUP BY cell")
    in_pin = ("SELECT COUNT(*) AS n, cell FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_on('updated_time')} GROUP BY cell")
    got = _decide(cond, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                  columns=_UNOWNED_COLUMNS)
    want = _decide(in_pin, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                   columns=_UNOWNED_COLUMNS)
    assert got is not None and got.refuse is True
    assert _record(got)["window_column"] == "updated_time"
    assert sorted(_record(got)["states"]) == ["COMPLETED", "FAILED"]
    assert _flag(got) == _flag(want)


def test_a_group_by_the_state_column_on_an_unowned_column_is_still_refused():
    """GROUP BY the state column over an unowned time column is one aggregate
    over every state and stays refused (unchanged by the conditional reader)."""
    sql = (f"SELECT state, COUNT(*) AS n FROM events WHERE {_on('updated_time')} GROUP BY state")
    found = _decide(sql, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                    columns=_UNOWNED_COLUMNS)
    assert found is not None and found.refuse is True
    assert _record(found)["window_column"] == "updated_time"


def test_one_conditionally_counted_state_on_an_unowned_column_runs():
    """One state counted inside a conditional aggregate on a column no state
    owns is a cohort of that state's rows: it runs."""
    sql = ("SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS b, cell "
           f"FROM events WHERE {_on('updated_time')} GROUP BY cell")
    assert _decide(sql, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                   columns=_UNOWNED_COLUMNS) is None


@pytest.mark.parametrize("items", [
    ("SUM(CASE WHEN state = 'COMPLETED' THEN 1 END) AS a, "
     "SUM(CASE WHEN state = 'FAILED' THEN 1 END) AS b"),
    "COUNT_IF(state = 'COMPLETED') AS a, COUNT_IF(state = 'FAILED') AS b",
])
def test_two_single_state_non_count_forms_on_an_unowned_column_are_a_breakdown(items):
    """R3-2 (follow-up): SUM(CASE ...) and COUNT_IF(...) of two single states
    on a column no state owns are a query-level breakdown, refused exactly as
    their IN-pin equivalent is. A prior round asserted the pair ran (per-
    aggregate parity); R3-2 corrected that to query-level parity."""
    sql = f"SELECT {items}, cell FROM events WHERE {_on('updated_time')} GROUP BY cell"
    in_pin = ("SELECT COUNT(*) AS n, cell FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_on('updated_time')} GROUP BY cell")
    got = _decide(sql, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                  columns=_UNOWNED_COLUMNS)
    want = _decide(in_pin, question="How many by state on 1/5?", desc=_UNOWNED_DESC,
                   columns=_UNOWNED_COLUMNS)
    assert got is not None and got.refuse is True, items
    assert _flag(got) == _flag(want), items


# --------------------------------------------------------------------------- #
# End to end through the guarded Athena tool: refused once, then disclosed
# --------------------------------------------------------------------------- #
class _Lake:
    def __init__(self, columns):
        self.columns = dict(columns)
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        name = "events" if "state" in self.columns else "job_runs"
        return {"TableMetadata": {
            "Name": name, "Columns": [{"Name": n, "Type": t} for n, t in self.columns.items()],
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
            names, rows = ["partition"], [["{created_day=1}"]]
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

    def data_queries(self):
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


def _ask(monkeypatch, lake, sqls, *, question, desc):
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
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", question, ""))
    return payloads, answer


def test_q2_shape_is_refused_once_then_the_resend_runs_disclosed(monkeypatch):
    lake = _Lake(_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [_Q2, _Q2],
                                   question=_COMPLETED_Q, desc=_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert first["window_column"] == "complete_time"
    assert first["own_columns"] == {"FAILED": "fail_time"}
    assert "one query per state" in first["guidance"]
    assert second["rows"] == [["42"]]
    assert second["state_time_window"]["window_column"] == "complete_time"
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer


def test_the_differently_named_schema_is_refused_once_end_to_end(monkeypatch):
    lake = _Lake(_JR_COLUMNS)
    (payload,), _answer = _ask(monkeypatch, lake, [_JR_CASE],
                               question="How many finished runs by line on 1/5, and aborted?",
                               desc=_JR_DESC)
    assert payload["reason"] == "window_on_other_state_time"
    assert payload["window_column"] == "finish_time"
    assert payload["own_columns"] == {"ABORTED": "abort_time"}
    assert lake.data_queries() == []


# =========================================================================== #
# Round 1 (review s68/review-stcase-r1.md): a conditional aggregate decides
# exactly like its WHERE-pinned or GROUP BY equivalent.
# =========================================================================== #
# A schema with a numeric column, so a SUM of a column can be tested.
_P_COLUMNS = {"id": "varchar", "state": "varchar", "complete_time": _TS, "fail_time": _TS,
              "start_time": _TS, "updated_time": _TS, "qty": "double", "cell": "varchar"}
_P_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
           f"complete_time: {_TS}; fail_time: {_TS}; start_time: {_TS}; updated_time: {_TS}; "
           "qty: double; cell: varchar e.g. c1, c2)")
_WIN = _on("complete_time")
# The reviewer's cohort question: names the window's state word ("completed").
_COHORT_Q = "Of the records completed today, how many are now failed?"


def _p(sql, *, question=_COHORT_Q):
    return _decide(sql, question=question, desc=_P_DESC, columns=_P_COLUMNS)


def _flag(found):
    """``(refuse, sorted states)`` or ``None`` for a decision, to compare
    parity between a conditional aggregate and its pinned/grouped equivalent."""
    if found is None:
        return None
    rec = found.to_dict()
    return (found.refuse, tuple(sorted(rec["states"])))


# The governing equivalents, measured on f5399bf5 (s68/parity/probe_base_parity.py):
# GROUP BY state -> refused breakdown (FAILED, STARTED); IN pin -> refused (FAILED);
# SUM(qty)/COUNT(*) WHERE state='FAILED' -> decision, disclosed (FAILED).
def test_the_governing_equivalents_decide_as_on_the_base():
    assert _flag(_p(f"SELECT state, COUNT(*) AS n FROM events WHERE {_WIN} GROUP BY state")) == (
        True, ("FAILED", "STARTED"))
    assert _flag(_p(f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', "
                    f"'FAILED') AND {_WIN} GROUP BY state")) == (True, ("FAILED",))
    assert _flag(_p(f"SELECT SUM(qty) AS q FROM events WHERE state = 'FAILED' AND {_WIN}")) == (
        False, ("FAILED",))
    assert _flag(_p(f"SELECT COUNT(*) AS n FROM events WHERE state = 'FAILED' AND {_WIN}")) == (
        False, ("FAILED",))


# Finding 1: a conditional count under a wrapper or parentheses or arithmetic,
# beside COUNT(*), decides like the GROUP BY breakdown (refused once, FAILED).
_WRAPPED_FORMS = [
    "COALESCE(COUNT(CASE WHEN state = 'FAILED' THEN 1 END), 0) AS c",
    "(COUNT(CASE WHEN state = 'FAILED' THEN 1 END)) AS c",
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 END) * 100.0 / COUNT(*) AS c",
    "CAST(SUM(CASE WHEN state = 'FAILED' THEN 1 END) AS DOUBLE) AS c",
]


@pytest.mark.parametrize("item", _WRAPPED_FORMS)
def test_a_wrapped_conditional_count_is_read_as_a_breakdown(item):
    sql = f"SELECT COUNT(*) AS n, {item} FROM events WHERE {_WIN}"
    assert _flag(_p(sql)) == (True, ("FAILED",)), item


# Finding 2: a simple CASE on the state column is read like the searched CASE.
_SIMPLE_CASE_FORMS = [
    "COUNT(CASE state WHEN 'FAILED' THEN 1 END) AS c",
    "COUNT(CASE state WHEN 'FAILED' THEN 1 WHEN 'STARTED' THEN 1 END) AS c",
    "COUNT(CASE e.state WHEN 'FAILED' THEN 1 END) AS c",
]


@pytest.mark.parametrize("item", _SIMPLE_CASE_FORMS)
def test_a_simple_case_on_the_state_column_is_read(item):
    states = ("FAILED", "STARTED") if "STARTED" in item else ("FAILED",)
    alias = " e " if "e.state" in item else " "
    sql = f"SELECT COUNT(*) AS n, {item} FROM events{alias}WHERE {_WIN}"
    assert _flag(_p(sql)) == (True, states), item


@pytest.mark.parametrize("operand", ["lower(state)", "state || ''", "id"])
def test_a_simple_case_on_a_non_state_operand_is_not_read(operand):
    """A simple CASE whose operand is not exactly the state column is unread,
    so only COUNT(*) of COMPLETED's own rows on its own column remains: runs."""
    sql = (f"SELECT COUNT(*) AS n, COUNT(CASE {operand} WHEN 'FAILED' THEN 1 END) AS c "
           f"FROM events WHERE {_WIN}")
    assert _p(sql) is None, operand


# Finding 3: the cohort query decides exactly like its GROUP BY / IN equivalents
# (refused once), and the identical resend then runs. Text cannot tell a cohort
# from a breakdown; this is the same cost the GROUP BY breakdown already pays.
def test_the_cohort_query_matches_its_group_by_and_in_pin_equivalents():
    cohort = (f"SELECT COUNT(*) AS completed_cohort, "
              f"COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS now_failed FROM events WHERE {_WIN}")
    in_pin = (f"SELECT state, COUNT(*) AS n FROM events WHERE state IN ('COMPLETED', 'FAILED') "
              f"AND {_WIN} GROUP BY state")
    assert _flag(_p(cohort)) == _flag(_p(in_pin)) == (True, ("FAILED",))


def test_the_cohort_query_refused_once_then_the_resend_runs(monkeypatch):
    cohort = (f"SELECT COUNT(*) AS completed_cohort, "
              f"COUNT(CASE WHEN state = 'FAILED' THEN 1 END) AS now_failed FROM events WHERE {_WIN}")
    lake = _Lake(_P_COLUMNS)
    (first, second), answer = _ask(monkeypatch, lake, [cohort, cohort],
                                   question=_COHORT_Q, desc=_P_DESC)
    assert first["reason"] == "window_on_other_state_time"
    assert second["rows"] == [["42"]] and "state_time_window" in second
    assert len(lake.data_queries()) == 1
    assert "state time, not asked for" in answer


# Finding 4: SUM is read like its WHERE-pinned equivalent. A branch contributes
# unless THEN is literal NULL (COUNT and SUM) or literal 0 (SUM only); a SUM of
# any non-zero/non-null expression (a column, a negative, a fraction) is read; a
# contributing ELSE or counting-of-the-zeros leaves the aggregate unread.
_READ_SUM_FORMS = [
    "SUM(qty) FILTER (WHERE state = 'FAILED') AS c",
    "SUM(CASE WHEN state = 'FAILED' THEN qty END) AS c",
    "SUM(CASE WHEN state = 'FAILED' THEN -1 END) AS c",
    "SUM(CASE WHEN state = 'FAILED' THEN 1.5 END) AS c",
]


@pytest.mark.parametrize("item", _READ_SUM_FORMS)
def test_a_sum_of_a_states_rows_is_read_like_its_pinned_equivalent(item):
    """Beside COUNT(*), a read SUM of FAILED is a breakdown (parity with GROUP
    BY state); the window state's own rows are counted by the COUNT(*)."""
    sql = f"SELECT COUNT(*) AS n, {item} FROM events WHERE {_WIN}"
    assert _flag(_p(sql)) == (True, ("FAILED",)), item


def test_a_sum_of_a_states_rows_alone_is_disclosed_like_its_pinned_equivalent():
    """A read SUM of FAILED with no unconditional count of the window state
    decides like `SUM(qty) WHERE state = 'FAILED'`: a decision, disclosed
    (the question names the window's state word)."""
    sql = f"SELECT SUM(qty) FILTER (WHERE state = 'FAILED') AS c FROM events WHERE {_WIN}"
    assert _flag(_p(sql)) == (False, ("FAILED",))


_UNREAD_BRANCH_FORMS = [
    # COUNT counts the 0s: a contributing ELSE
    "COUNT(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS c",
    # SUM's ELSE 1 contributes
    "SUM(CASE WHEN state = 'FAILED' THEN 0 ELSE 1 END) AS c",
    # SUM THEN 0 is not a contributing branch, and nothing else pins -> unread
    "SUM(CASE WHEN state = 'FAILED' THEN 0 END) AS c",
]


@pytest.mark.parametrize("item", _UNREAD_BRANCH_FORMS)
def test_a_contributing_else_or_zero_branch_leaves_the_aggregate_unread(item):
    sql = f"SELECT COUNT(*) AS n, {item} FROM events WHERE {_WIN}"
    assert _p(sql) is None, item


def test_max_min_avg_of_a_case_stay_unread():
    for agg in ("MAX", "MIN", "AVG"):
        sql = (f"SELECT COUNT(*) AS n, {agg}(CASE WHEN state = 'FAILED' THEN qty END) AS c "
               f"FROM events WHERE {_WIN}")
        assert _p(sql) is None, agg


# Finding 5: a conditional aggregate followed by OVER is a window over the
# result rows, not a count of the table's rows: skipped, as _counts_rows does.
def test_a_conditional_filter_followed_by_over_is_skipped():
    sql = (f"SELECT COUNT(*) FILTER (WHERE state = 'FAILED') OVER () AS c "
           f"FROM events WHERE {_WIN}")
    assert _p(sql) is None


def test_a_conditional_case_count_followed_by_over_is_skipped():
    sql = (f"SELECT COUNT(CASE WHEN state = 'FAILED' THEN 1 END) OVER () AS c "
           f"FROM events WHERE {_WIN}")
    assert _p(sql) is None


# Generic parity on a differently named schema: each conditional form gives the
# same (refuse, states) as its pinned or GROUP BY equivalent, for an unnamed
# question and one naming the window's state word.
_JR_WIN = _jr_on("finish_time")
_JR_NAMED_Q = "Of the finished runs today, how many aborted?"
_JR_UNNAMED_Q = "How many runs by line today?"


@pytest.mark.parametrize("question", [_JR_NAMED_Q, _JR_UNNAMED_Q])
@pytest.mark.parametrize("item", [
    "COUNT(CASE WHEN status = 'ABORTED' THEN 1 END)",
    "COUNT(CASE status WHEN 'ABORTED' THEN 1 END)",
    "COUNT(*) FILTER (WHERE status = 'ABORTED')",
    "COUNT_IF(status = 'ABORTED')",
    "COUNT(IF(status = 'ABORTED', 1))",
    "SUM(CASE WHEN status = 'ABORTED' THEN elapsed_s END)",
    "COALESCE(COUNT(CASE WHEN status = 'ABORTED' THEN 1 END), 0)",
    "(SUM(CASE WHEN status = 'ABORTED' THEN 1 END))",
])
def test_every_conditional_form_matches_its_in_pin_equivalent_on_job_runs(item, question):
    """Beside COUNT(*), each form counts ABORTED; its decision equals the
    GROUP BY / IN-pin breakdown of FINISHED and ABORTED on finish_time."""
    jr_cols = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "elapsed_s": "double", "line": "varchar"}
    jr_desc = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED; "
               f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; elapsed_s: double; "
               "line: varchar e.g. a, b)")
    cond = f"SELECT COUNT(*) AS n, {item} AS c FROM job_runs WHERE {_JR_WIN}"
    in_pin = (f"SELECT status, COUNT(*) AS n FROM job_runs WHERE status IN ('FINISHED', "
              f"'ABORTED') AND {_JR_WIN} GROUP BY status")
    got = _decide(cond, question=question, table="job_runs", desc=jr_desc, columns=jr_cols)
    want = _decide(in_pin, question=question, table="job_runs", desc=jr_desc, columns=jr_cols)
    assert _flag(got) == _flag(want), item


# =========================================================================== #
# Round 2 (review s68/review-stcase-r2.md): a conditional aggregate decides
# exactly like the filtered COUNT(*) query it is equivalent to. Parity by
# construction: each test pairs a conditional form with its hand-written
# pinned equivalent and asserts the same (refuse, states).
# =========================================================================== #
_FAIL_WIN = f"fail_time >= {_DAY} AND fail_time < {_NEXT}"


def _parity(conditional_sql, pinned_sql, *, question=_COHORT_Q):
    """Assert a conditional aggregate decides the same as its pinned COUNT(*)
    equivalent (both on the qty-bearing parity schema)."""
    got = _p(conditional_sql, question=question)
    want = _p(pinned_sql, question=question)
    assert _flag(got) == _flag(want), (conditional_sql, _flag(got), _flag(want))
    return _flag(got)


# N1: a time predicate inside the branch is kept, so the equivalent has two
# bounded time columns (no decision) or names the state's own column (no
# decision), exactly as the pinned form.
def test_n1_a_fail_time_range_inside_the_branch_matches_its_pinned_equivalent():
    cond = (f"SELECT COUNT(*) AS c, "
            f"COUNT(CASE WHEN state = 'FAILED' AND {_FAIL_WIN} THEN 1 END) AS f "
            f"FROM events WHERE {_WIN}")
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_FAIL_WIN} AND {_WIN}"
    assert _parity(cond, pinned) is None  # two bounded time columns: no decision


def test_n1_a_fail_time_not_null_inside_the_branch_matches_its_pinned_equivalent():
    cond = (f"SELECT COUNT(*) AS c, "
            f"COUNT(CASE WHEN state = 'FAILED' AND fail_time IS NOT NULL THEN 1 END) AS f "
            f"FROM events WHERE {_WIN}")
    pinned = (f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND fail_time IS NOT NULL "
              f"AND {_WIN}")
    assert _parity(cond, pinned) is None  # the WHERE names the own column: no decision


@pytest.mark.parametrize("form", [
    "COUNT(*) FILTER (WHERE state = 'FAILED' AND fail_time IS NOT NULL)",
    "COUNT_IF(state = 'FAILED' AND fail_time IS NOT NULL)",
    "COUNT(IF(state = 'FAILED' AND fail_time IS NOT NULL, 1))",
])
def test_n1_own_time_predicate_in_filter_count_if_and_if_matches_pinned(form):
    cond = f"SELECT COUNT(*) AS c, {form} AS f FROM events WHERE {_WIN}"
    pinned = (f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND fail_time IS NOT NULL "
              f"AND {_WIN}")
    assert _parity(cond, pinned) is None


def test_n1_the_reviewer_cohort_with_a_fail_time_range_is_not_refused():
    """The exact reviewer N1 repro: the equivalent has two bounded time
    columns, so no decision, and the cohort is NOT a breakdown refusal."""
    cohort = (f"SELECT COUNT(*) AS completed, "
              f"COUNT(CASE WHEN state = 'FAILED' AND {_FAIL_WIN} THEN 1 END) AS failed_same_day "
              f"FROM events WHERE {_WIN}")
    assert _p(cohort) is None


# N2: a nested CASE in a THEN is read (one CASE unit by CASE/END nesting), and
# decides like the flattened single CASE of the conjoined conditions.
def test_n2_a_nested_case_in_the_then_matches_the_flattened_single_case():
    nested = (f"SELECT COUNT(*) AS c, "
              f"COUNT(CASE WHEN state = 'FAILED' THEN CASE WHEN qty > 0 THEN 1 END END) AS f "
              f"FROM events WHERE {_WIN}")
    flat = (f"SELECT COUNT(*) AS c, "
            f"COUNT(CASE WHEN state = 'FAILED' AND qty > 0 THEN 1 END) AS f "
            f"FROM events WHERE {_WIN}")
    # Both are a breakdown over FAILED on complete_time (question names completed).
    assert _parity(nested, flat) == (True, ("FAILED",))


def test_n2_a_nested_case_whose_inner_bounds_the_own_time_matches_pinned():
    """A nested CASE whose inner condition bounds the counted state's own time
    is read and gets no decision, like its pinned equivalent (two windows)."""
    nested = (f"SELECT COUNT(*) AS c, "
              f"COUNT(CASE WHEN state = 'FAILED' THEN CASE WHEN {_FAIL_WIN} THEN 1 END END) AS f "
              f"FROM events WHERE {_WIN}")
    pinned = f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' AND {_FAIL_WIN} AND {_WIN}"
    assert _parity(nested, pinned) is None


def test_n2_an_inner_only_state_condition_under_a_non_state_outer_is_read():
    """`CASE WHEN qty > 0 THEN CASE WHEN state = 'FAILED' THEN 1 END END` counts
    FAILED rows with qty > 0, like `state = 'FAILED' AND qty > 0`: a breakdown."""
    inner = (f"SELECT COUNT(*) AS c, "
             f"COUNT(CASE WHEN qty > 0 THEN CASE WHEN state = 'FAILED' THEN 1 END END) AS f "
             f"FROM events WHERE {_WIN}")
    flat = (f"SELECT COUNT(*) AS c, "
            f"COUNT(CASE WHEN qty > 0 AND state = 'FAILED' THEN 1 END) AS f "
            f"FROM events WHERE {_WIN}")
    assert _parity(inner, flat) == (True, ("FAILED",))


# Earlier-branch negation: an earlier NULL branch on fail_time negates into the
# later branch's path, which then names fail_time, so no decision (parity with
# the pinned `state = 'FAILED' AND NOT (fail_time IS NULL)`).
def test_earlier_branch_negation_names_the_own_time_and_matches_pinned():
    cond = (f"SELECT COUNT(*) AS c, "
            f"COUNT(CASE WHEN fail_time IS NULL THEN NULL WHEN state = 'FAILED' THEN 1 END) AS f "
            f"FROM events WHERE {_WIN}")
    pinned = (f"SELECT COUNT(*) FROM events WHERE state = 'FAILED' "
              f"AND NOT (fail_time IS NULL) AND {_WIN}")
    assert _parity(cond, pinned) is None


# A generic parity table of every readable form against its built equivalent,
# for an unnamed question and one naming the window's state word, on the
# events-shaped schema and on job_runs.
_EVENTS_FORMS = [
    ("COUNT(CASE WHEN state = 'FAILED' THEN 1 END)", "state = 'FAILED'"),
    ("COUNT(CASE state WHEN 'FAILED' THEN 1 END)", "state = 'FAILED'"),
    ("COUNT(*) FILTER (WHERE state = 'FAILED')", "state = 'FAILED'"),
    ("COUNT_IF(state = 'FAILED')", "state = 'FAILED'"),
    ("COUNT(IF(state = 'FAILED', 1))", "state = 'FAILED'"),
    ("SUM(CASE WHEN state = 'FAILED' THEN qty END)", "state = 'FAILED'"),
    ("COALESCE(COUNT(CASE WHEN state = 'FAILED' THEN 1 END), 0)", "state = 'FAILED'"),
    ("COUNT(CASE WHEN state = 'FAILED' AND qty > 0 THEN 1 END)", "state = 'FAILED' AND qty > 0"),
    ("COUNT(CASE WHEN state = 'FAILED' AND fail_time IS NOT NULL THEN 1 END)",
     "state = 'FAILED' AND fail_time IS NOT NULL"),
]


@pytest.mark.parametrize("question", [_COHORT_Q, "How many records on 1/5?"])
@pytest.mark.parametrize(("form", "pin"), _EVENTS_FORMS)
def test_events_parity_table_each_form_matches_its_pinned_equivalent(form, pin, question):
    # The aggregate alone (no cohort COUNT(*), which would add the separately
    # accepted breakdown escalation): its decision must equal its pinned form.
    cond = f"SELECT {form} AS f FROM events WHERE {_WIN}"
    pinned = f"SELECT COUNT(*) FROM events WHERE {pin} AND {_WIN}"
    _parity(cond, pinned, question=question)


_JR_FORMS = [
    ("COUNT(CASE WHEN status = 'ABORTED' THEN 1 END)", "status = 'ABORTED'"),
    ("COUNT(CASE status WHEN 'ABORTED' THEN 1 END)", "status = 'ABORTED'"),
    ("COUNT(*) FILTER (WHERE status = 'ABORTED')", "status = 'ABORTED'"),
    ("SUM(CASE WHEN status = 'ABORTED' THEN elapsed_s END)", "status = 'ABORTED'"),
    ("COUNT(CASE WHEN status = 'ABORTED' AND abort_time IS NOT NULL THEN 1 END)",
     "status = 'ABORTED' AND abort_time IS NOT NULL"),
    ("COUNT(CASE WHEN status = 'ABORTED' THEN CASE WHEN elapsed_s > 0 THEN 1 END END)",
     "status = 'ABORTED' AND elapsed_s > 0"),
]


@pytest.mark.parametrize("question", ["Of finished runs today, how many aborted?",
                                      "How many runs today?"])
@pytest.mark.parametrize(("form", "pin"), _JR_FORMS)
def test_job_runs_parity_table_each_form_matches_its_pinned_equivalent(form, pin, question):
    jr_cols = {"id": "varchar", "status": "varchar", "finish_time": _TS, "abort_time": _TS,
               "queue_time": _TS, "elapsed_s": "double", "line": "varchar"}
    jr_desc = ("job_runs(id: varchar; status: varchar e.g. FINISHED, ABORTED, QUEUED; "
               f"finish_time: {_TS}; abort_time: {_TS}; queue_time: {_TS}; elapsed_s: double; "
               "line: varchar e.g. a, b)")
    win = _jr_on("finish_time")
    cond = f"SELECT {form} AS f FROM job_runs WHERE {win}"
    pinned = f"SELECT COUNT(*) FROM job_runs WHERE {pin} AND {win}"
    got = _decide(cond, question=question, table="job_runs", desc=jr_desc, columns=jr_cols)
    want = _decide(pinned, question=question, table="job_runs", desc=jr_desc, columns=jr_cols)
    assert _flag(got) == _flag(want), (form, _flag(got), _flag(want))


# Round 3 (R3-5): an IF whose THEN cannot be read leaves the whole aggregate
# unread. Its THEN counts rows the reader cannot place by state, so a readable
# ELSE alone must not make the aggregate look like a count of one state.
_UNREAD_THEN_IF = [
    "COUNT(IF(duration_s > 0, CASE cell WHEN 'c1' THEN 1 END, IF(state = 'FAILED', 1, NULL)))",
    "COUNT(IF(duration_s > 0, CASE cell WHEN 'c1' THEN 1 END, "
    "CASE WHEN state = 'FAILED' THEN 1 END))",
]


@pytest.mark.parametrize("form", _UNREAD_THEN_IF)
def test_an_if_with_an_unread_then_is_not_read_from_its_else(form):
    sql = f"SELECT {form} AS c FROM events WHERE {_on('complete_time')}"
    assert _decide(sql, question="How many rows on 1/5?") is None


def test_an_if_with_a_readable_then_and_else_is_still_read():
    sql = (f"SELECT COUNT(*) AS n, COUNT(IF(state = 'FAILED', 1, NULL)) AS f "
           f"FROM events WHERE {_on('complete_time')}")
    found = _decide(sql, question="How many rows on 1/5?")
    assert found is not None and found.refuse
    assert _record(found)["states"] == ["FAILED"]
