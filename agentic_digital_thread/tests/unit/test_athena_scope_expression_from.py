# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A FROM that belongs to an expression is not a FROM clause.

Measured in an answer eval: asked for an hourly profile and for cycle-time
percentiles, the system agent wrote ``EXTRACT(HOUR FROM complete_date)`` and
``EXTRACT(EPOCH FROM complete_date - start_date)``. The scan guard read the
``FROM`` inside ``EXTRACT(`` as a FROM clause naming a table, could not find
that clause's SELECT, marked the whole query uncertain and refused it before
it ran (``scope_required``), on every table whatever its partitioning. All ten
refusals in the run were EXTRACT queries; the agent retried the same shape and
then said the figure could not be computed.

Trino has three special forms whose argument list holds a FROM of its own:
``EXTRACT(field FROM x)``, ``SUBSTRING(s FROM start [FOR length])`` and
``TRIM([spec] [chars] FROM s)``. The guard now skips exactly that FROM, so the
query gets the decision its twin written without it gets (``date_trunc``,
``substr``, ``ltrim``). Any other FROM keeps its old reading, so a scan the
guard could not read stays refused. ``a IS [NOT] DISTINCT FROM b`` keeps its
old reading on purpose: its FROM is at the query block's own depth, where the
state-time check and other readers take it for the block's FROM clause, so
admitting it would let a state-time breakdown run unrefused.

Because the guard now lets these queries run, the checks that read a query
after the guard must read them as they read the twin; the state-time check is
tested here end to end. Fully offline (a stubbed connector), neutral names.
"""
from __future__ import annotations

import asyncio

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import db, state_times, storage_bound_rule, storage_bounds
from agents.athena_scope import drop_comma_before_cross_join, relations, tokenize

_CFG = {"workgroup": "wg_a", "database": "db_a", "region": "us-east-1"}
_SHAPE = ("SELECT {expr} AS v, COUNT(*) AS n FROM table_x "
          "WHERE site = 'site-a'{extra} GROUP BY 1 ORDER BY 1")

#: (expression with an expression FROM, its twin without one).
_PAIRS = {
    "extract hour": ("EXTRACT(HOUR FROM complete_date)", "date_trunc('hour', complete_date)"),
    "extract, lower case, arithmetic": ("extract(epoch from complete_date - start_date)",
                                        "to_unixtime(complete_date) - to_unixtime(start_date)"),
    "extract of a parenthesised operand": ("EXTRACT(DAY FROM (complete_date - start_date))",
                                           "day(complete_date - start_date)"),
    "nested in an aggregate": ("APPROX_PERCENTILE(EXTRACT(MINUTE FROM complete_date), 0.9)",
                               "APPROX_PERCENTILE(minute(complete_date), 0.9)"),
    "substring from for": ("SUBSTRING(code FROM 1 FOR 3)", "substr(code, 1, 3)"),
    "trim leading chars from": ("TRIM(LEADING '0' FROM code)", "ltrim(code)"),
    "trim from": ("TRIM(FROM code)", "trim(code)"),
}


@pytest.fixture(autouse=True)
def _clear_caches():
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()
    yield
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()


@pytest.fixture
def ran(monkeypatch):
    calls: list[str] = []

    def connector(sql, cfg, source_system=""):
        calls.append(sql)
        return {"columns": ["v", "n"], "rows": [["a-1", "3"]], "row_count": 1}

    monkeypatch.setattr(at, "run_athena_query_exact", connector)
    # Partition discovery comes from the baked map only (no live lookup).
    monkeypatch.setattr(at, "list_partition_columns", lambda *a, **k: [])
    # An auto-scope rewrite would read the recent partitions: answered here so
    # a divergence shows as an applied scope, not as a lookup failure.
    monkeypatch.setattr(at, "build_recent_partition_predicate",
                        lambda *a, **k: "part_day >= '2026-09-01'")
    return calls


def _attempt(sql: str, baked: dict) -> dict:
    return sa._scoped_athena_attempt(sql, at._resolve_config(_CFG), baked, "system-a")


def _decision(sql: str, baked: dict) -> dict:
    return sa._scope_athena_sql(sql, at._resolve_config(_CFG), baked_partition_columns=baked)[1]


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


@pytest.mark.parametrize("pair", list(_PAIRS))
def test_the_relation_reader_finds_only_the_table(pair):
    sql = _SHAPE.format(expr=_PAIRS[pair][0], extra="")
    scans, uncertain = relations(tokenize(sql)[0])
    assert [s.parts for s in scans] == [("table_x",)]
    assert uncertain is False


@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.parametrize("pair", list(_PAIRS))
def test_an_expression_from_gets_its_twins_decision(ran, pair, kind):
    make, extra = _KINDS[kind]
    baked = make()
    with_from, twin = (_SHAPE.format(expr=e, extra=extra) for e in _PAIRS[pair])
    assert _decision(with_from, baked) == _decision(twin, baked)
    from_payload, twin_payload = _attempt(with_from, baked), _attempt(twin, baked)
    assert from_payload.get("error_type") == twin_payload.get("error_type"), (from_payload, twin_payload)
    assert from_payload.get("scope") == twin_payload.get("scope")
    assert len(ran) == (0 if "error" in twin_payload else 2)


@pytest.mark.parametrize("pair", ["extract hour", "substring from for", "trim leading chars from"])
def test_an_expression_from_in_the_where_clause_gets_its_twins_decision(ran, pair):
    baked = {"table_x": ["part_day"]}
    with_from, twin = (
        _SHAPE.format(expr="code", extra=f" AND part_day = '2026-09-01' AND {e} IS NOT NULL")
        for e in _PAIRS[pair])
    assert _decision(with_from, baked) == _decision(twin, baked)
    assert _decision(with_from, baked)["reason"] == "explicit_partition_predicate"
    assert "error" not in _attempt(with_from, baked)


def test_a_partition_column_inside_extract_is_not_a_partition_predicate(ran):
    baked = {"table_x": ["part_day"]}
    with_from = _SHAPE.format(expr="code", extra=" AND EXTRACT(YEAR FROM part_day) = 2026")
    twin = _SHAPE.format(expr="code", extra=" AND year(part_day) = 2026")
    assert _decision(with_from, baked) == _decision(twin, baked)
    assert _decision(with_from, baked)["reason"] != "explicit_partition_predicate"


@pytest.mark.parametrize("comparison", [
    "code IS DISTINCT FROM other_code",
    "code IS NOT DISTINCT FROM other_code",
    "code IS DISTINCT FROM 'c1'",
])
def test_is_distinct_from_keeps_its_old_reading(comparison):
    """Its FROM is at the block's own depth: still read as a FROM clause, so
    the query gets the reading (and refusal) it got before."""
    sql = _SHAPE.format(expr=comparison, extra="")
    scans, uncertain = relations(tokenize(sql)[0])
    assert uncertain or len(scans) > 1


def test_a_subquery_inside_extract_is_still_a_scan():
    sql = ("SELECT EXTRACT(HOUR FROM (SELECT MAX(complete_date) FROM table_y)) AS h "
           "FROM table_x WHERE site = 'site-a'")
    scans, uncertain = relations(tokenize(sql)[0])
    assert sorted(s.parts for s in scans) == [("table_x",), ("table_y",)]
    assert uncertain is False


@pytest.mark.parametrize("sql", [
    # A FROM inside any other call is no expression FROM: unchanged, uncertain.
    "SELECT some_function(code FROM table_y) AS v FROM table_x",
    # A quoted name is a user function, not the EXTRACT special form.
    'SELECT "extract"(HOUR FROM complete_date) AS v FROM table_x',
    # Only the first FROM in EXTRACT's argument list is its own.
    "SELECT EXTRACT(HOUR FROM complete_date FROM table_y) AS v FROM table_x",
])
def test_other_froms_keep_their_old_reading(sql):
    _scans, uncertain = relations(tokenize(sql)[0])
    assert uncertain is True


def test_distinct_from_without_is_is_still_read_as_a_from_clause():
    scans, _uncertain = relations(tokenize("SELECT code NOT DISTINCT FROM table_y FROM table_x")[0])
    assert ("table_y",) in [s.parts for s in scans]


def test_the_comma_repair_still_reads_a_query_with_extract():
    sql = ("SELECT EXTRACT(HOUR FROM complete_date) AS h, json_extract_scalar(el, '$.id') AS item "
           "FROM table_x, CROSS JOIN UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u(el)")
    repaired, removed = drop_comma_before_cross_join(sql)
    assert removed == 1
    assert "FROM table_x CROSS JOIN UNNEST" in repaired


# --------------------------------------------------------------------------- #
# After the guard: the state-time check reads the query as it reads its twin
# --------------------------------------------------------------------------- #
# A query the guard now lets run must also reach the checks
# that follow it as its twin does. The state-time check refuses counting every
# state on one state's own time column; an expression FROM before the state
# column must not hide the state from it.
_TS = "timestamp(6) with time zone"
_ST_COLUMNS = {"id": "varchar", "state": "varchar", "start_time": _TS, "complete_time": _TS,
               "fail_time": _TS, "created": "timestamp(6)", "cell": "varchar"}
_ST_DESC = ("events(id: varchar; state: varchar e.g. COMPLETED, FAILED, STARTED; "
            f"start_time: {_TS}; complete_time: {_TS}; fail_time: {_TS}; "
            "created: timestamp(6); cell: varchar e.g. c1, c2)")
_WINDOW = ("complete_time >= TIMESTAMP '2026-01-05 00:00:00 UTC' "
           "AND complete_time < TIMESTAMP '2026-01-06 00:00:00 UTC'")
_ST_Q = "How many segments per state and hour on 1/5?"
_ST_PAIRS = {
    "extract": ("EXTRACT(HOUR FROM complete_time)", "date_trunc('hour', complete_time)"),
    "substring": ("SUBSTRING(cell FROM 1 FOR 1)", "substr(cell, 1, 1)"),
    "trim": ("TRIM(LEADING 'c' FROM cell)", "ltrim(cell)"),
}
_ST_SHAPES = {
    "select list before state, group by ordinal":
        "SELECT {e} AS h, state, COUNT(*) AS n FROM events WHERE " + _WINDOW + " GROUP BY 1, 2",
    "group by expression":
        "SELECT state, COUNT(*) AS n FROM events WHERE " + _WINDOW + " GROUP BY state, {e}",
    "where predicate":
        "SELECT state, COUNT(*) AS n FROM events WHERE " + _WINDOW
        + " AND {e} IS NOT NULL GROUP BY state",
}


@pytest.fixture
def state_fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    storage_bounds.reset()
    state_times.reset()
    yield
    at.clear_schema_cache()
    storage_bounds.reset()
    state_times.reset()


def _state_parts(sql: str) -> list[dict]:
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    mapping = state_times.from_tables_desc(_ST_DESC)["events"]
    parts = state_times.decide_parts(sql, "events", bounded, _ST_COLUMNS, mapping, question=_ST_Q)
    return [(part.refuse, part.to_dict()) for part in parts]


@pytest.mark.parametrize("shape", list(_ST_SHAPES))
@pytest.mark.parametrize("pair", list(_ST_PAIRS))
def test_the_state_time_check_decides_an_expression_from_as_its_twin(state_fresh, pair, shape):
    with_from, twin = (_ST_SHAPES[shape].format(e=e) for e in _ST_PAIRS[pair])
    want = _state_parts(twin)
    assert want and any(refuse for refuse, _ in want), twin  # the twin is refused: not vacuous
    assert _state_parts(with_from) == want


class _Lake:
    """A stubbed Athena client: one hidden-partitioned ``events`` table."""

    def __init__(self):
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "events", "Columns": [{"Name": n, "Type": t} for n, t in _ST_COLUMNS.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in _ST_COLUMNS.items()]
        else:
            names, rows = ["n"], [["42"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self):
        return [q for q in self.queries if "$partitions" not in q and "information_schema" not in q]


def _ask_twice(monkeypatch, sql: str) -> tuple[list[dict], int]:
    """The same query sent twice through the guarded tool in one Mode A turn:
    the payloads and how many data queries reached the engine."""
    lake = _Lake()

    def graph_offline(*_a, **_k):
        raise RuntimeError("graph offline in this test")

    monkeypatch.setattr(db, "_neptune_execute_once", graph_offline)
    sa._remember_turn_time_facts("system-a", [], _ST_DESC)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    cfg = at._resolve_config(_CFG)
    payloads: list[dict] = []

    async def calls():
        for _ in range(2):
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return "The count is 42.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    asyncio.run(sa._run_mode_a_query(object(), "system-a", _ST_Q, ""))
    return payloads, len(lake.data_queries())


def _outcome(payload: dict) -> tuple:
    return payload.get("error"), payload.get("reason"), bool(payload.get("state_time_window"))


@pytest.mark.parametrize("pair", list(_ST_PAIRS))
def test_end_to_end_an_expression_from_is_refused_once_like_its_twin(monkeypatch, state_fresh, pair):
    shape = _ST_SHAPES["select list before state, group by ordinal"]
    with_from, twin = (shape.format(e=e) for e in _ST_PAIRS[pair])
    want_payloads, want_ran = _ask_twice(monkeypatch, twin)
    state_times.reset()
    storage_bounds.reset()
    got_payloads, got_ran = _ask_twice(monkeypatch, with_from)
    assert want_payloads[0].get("error") == "state_time_window"  # not vacuous
    assert "error" not in want_payloads[1] and want_ran == 1  # refused once, resend ran
    assert [_outcome(p) for p in got_payloads] == [_outcome(p) for p in want_payloads]
    assert got_payloads[1].get("state_time_window") == want_payloads[1].get("state_time_window")
    assert got_ran == want_ran


# --------------------------------------------------------------------------- #
# After the guard: the listed-key grain check reads the query as its twin
# --------------------------------------------------------------------------- #
# The listed-key check used to find a flat SELECT's list by the first
# ``from`` after SELECT, so the FROM inside EXTRACT/SUBSTRING/TRIM cut the list
# short, its COUNT was never seen, and the engine line that says a total adds
# up several listed keys was dropped. It now takes the first ``from`` outside
# the list's parentheses.
_KEY_SHAPE = ("SELECT {e} AS bucket, COUNT(*) AS n FROM events "
              "WHERE batch_id IN ('AB-1001', 'AB-1002') GROUP BY 1")
_KEY_PAIRS = {
    "extract": ("EXTRACT(HOUR FROM complete_time)", "date_trunc('hour', complete_time)"),
    "substring": ("SUBSTRING(code FROM 1 FOR 2)", "substr(code, 1, 2)"),
    "trim": ("TRIM(LEADING '0' FROM code)", "ltrim(code)"),
}


@pytest.mark.parametrize("pair", list(_KEY_PAIRS))
def test_the_listed_key_check_reads_an_expression_from_as_its_twin(pair):
    from agents import listed_keys
    with_from, twin = (_KEY_SHAPE.format(e=e) for e in _KEY_PAIRS[pair])
    assert listed_keys.statement_shape(with_from) == listed_keys.statement_shape(twin)
    assert listed_keys.collapsed_key_filters(with_from) == listed_keys.collapsed_key_filters(twin)
    want = listed_keys.grain_line([twin], "The total is 42.")
    assert want  # the twin's answer gets the engine line: not vacuous
    assert listed_keys.grain_line([with_from], "The total is 42.") == want


#: A parenthesis, or the word from, inside a double-quoted
#: name is data, not SQL structure. (quoted name, plain twin)
_QUOTED = {
    "open paren in an alias": ('x AS "hour (local"', "x AS hour_local"),
    "close paren in an alias": ('x AS "hour) local"', "x AS hour_local"),
    "balanced parens in an alias": ('x AS "hour (local)"', "x AS hour_local"),
    "doubled quote and paren": ('x AS "a""(b"', "x AS ab"),
    "from inside an alias": ('x AS "from here"', "x AS from_here"),
    "paren in a quoted column": ('"col (a" AS bucket', "col_a AS bucket"),
}
_QUOTED_SHAPE = ("SELECT {e}, COUNT(*) AS n FROM t "
                 "WHERE k IN ('AB-1001', 'AB-1002') GROUP BY 1")


@pytest.mark.parametrize("case", list(_QUOTED))
def test_a_quoted_name_does_not_move_the_select_lists_from(case):
    from agents import listed_keys
    quoted, plain = (_QUOTED_SHAPE.format(e=e) for e in _QUOTED[case])
    want = listed_keys.collapsed_key_filters(plain)
    assert want  # the plain twin is read as a collapse: not vacuous
    got = listed_keys.collapsed_key_filters(quoted)
    assert [(c.column, c.keys, c.operator, c.counts_rows) for c in got] == \
        [(c.column, c.keys, c.operator, c.counts_rows) for c in want]
    # The line rides both answers. Its wording names the group by the
    # select-list item, which for a quoted alias is not this change's concern.
    assert listed_keys.grain_line([quoted], "The total is 42.")
    assert listed_keys.grain_line([plain], "The total is 42.")


#: Text the listed-key mask does not blank (a PostgreSQL
#: dollar-quoted or E'' string, a backtick name) can hold a parenthesis. Where
#: no FROM sits at the list's own depth, the reader keeps the plain reading
#: (the first FROM), so such text can never cost the line. (literal, twin)
#: An ``E''`` string with a backslash-escaped quote is not read by the plain
#: reading either (the mask ends the string at the escaped quote).
_UNMASKED = {
    "dollar quote, open paren": ("$$($$ AS bucket", "'x' AS bucket"),
    "tagged dollar quote, open paren": ("$tag$($tag$ AS bucket", "'x' AS bucket"),
    "dollar quote, close paren": ("$$)$$ AS bucket", "'x' AS bucket"),
    "dollar quote, balanced": ("$$(a)$$ AS bucket", "'x' AS bucket"),
    "backtick name, open paren": ("`a (b` AS bucket", "x AS bucket"),
}


@pytest.mark.parametrize("case", list(_UNMASKED))
def test_unmasked_text_with_a_parenthesis_keeps_the_line(case):
    from agents import listed_keys
    literal, plain = (_QUOTED_SHAPE.format(e=e) for e in _UNMASKED[case])
    want = listed_keys.collapsed_key_filters(plain)
    assert want  # not vacuous
    got = listed_keys.collapsed_key_filters(literal)
    assert [(c.column, c.keys, c.operator, c.counts_rows) for c in got] == \
        [(c.column, c.keys, c.operator, c.counts_rows) for c in want]
    assert listed_keys.grain_line([literal], "The total is 42.")


def test_an_extract_after_unmasked_text_still_finds_the_list_end():
    """A dollar-quoted parenthesis before an EXTRACT: no FROM at the list's
    depth, so the first FROM (EXTRACT's) ends the list, as in the plain
    reading; the reader is never worse than the plain reading."""
    from agents import listed_keys
    sql = ("SELECT $$($$ AS tag, EXTRACT(HOUR FROM complete_time) AS h, COUNT(*) AS n FROM t "
           "WHERE k IN ('AB-1001', 'AB-1002') GROUP BY 1, 2")
    stmt = listed_keys._flat_select(sql)
    assert stmt is not None
    assert sql[stmt.select_list[1]:].upper().startswith("FROM COMPLETE_TIME")
