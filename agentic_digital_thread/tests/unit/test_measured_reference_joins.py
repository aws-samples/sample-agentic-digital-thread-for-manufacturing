# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision measures each ``*_id`` name-pattern join against the data.

Workshop rehearsal, 2026-10-17: the route seed of every lot-trace
question had no IDENTIFIER FORMS block. A plant MES's work-order to run key
join (``work_orders.production_run_id -> production_runs.production_run_id``)
is the only join that pairs a work-order number with a run number per item,
and provision could only record it as a guess from the column names
(``basis = column_name_pattern``): the run view joins another table and
renames its key, so no declared foreign key can be read through it. Since
the guess no longer counts as per-item pairing evidence, the forms block was
empty and the orchestrator converted between the two forms itself.

Provision step b3 now reads each such join once, bounded: every non-null
value of the referencing column must be found in the target column, and the
target column must hold one distinct, non-null value per row. When both hold,
and no other table the column name could mean holds the values too, the join
is recorded as measured (``basis = measured_reference``) with the counts and
when they were read; otherwise, or when the read is too large, times out,
fails, compares types PostgreSQL cannot compare, or is past the provision's
cap or time budget (connecting included), it stays ``column_name_pattern``
and says why. A bare guess still proves nothing.

A measured reference is evidence for ONE reader: the identifier-forms
pairing. Everywhere else, the metric layer's mirror collapse included, it
counts exactly as the naming guess does.

Offline: the database and the graph are fakes. The SQL itself is proven on
PostgreSQL in ``tests/integration/test_measured_reference_joins_pg.py``.
"""
from __future__ import annotations

import datetime
import time

import pytest

import agents.connection_evidence as ce
import agents.db as db
from agents import pgdriver
import agents.onboard as onboard
from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch
from agents.metric_resolver import MetricResolverError, _select_join_path
from agents.models import ProvisionRequest

SID = "sys-a"
SCHEMA = "schema_a"
MEASURED = "measured_reference"
GUESS = "column_name_pattern"
#: The connect time b3 holds back for each read (2 s: libpq's own floor).
CONNECT_S = getattr(onboard, "REFERENCE_CONNECT_TIMEOUT_S", 2)


def _connect_cap():
    """The connect-timeout cap in force for a read (None without one)."""
    return getattr(db, "current_connect_timeout_cap", lambda: None)()


def _ctx(system_id=SID, schema=SCHEMA, **overrides) -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=system_id, system_name="System A", schema=schema,
                            connection_type="PostgreSQL")
    ctx = onboard._ProvisionState(
        body=body, system_id=system_id, system_name="System A", plant="", schema=schema,
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[])
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _guess(ft, ff, tt, tf=None):
    return {"from_table": ft, "from_field": ff, "to_table": tt, "to_field": tf or ff,
            "basis": GUESS}


def _set_value(cypher: str, params: dict, prop: str):
    """The value a JoinPath MERGE SETs on ``jp.<prop>``, or KeyError."""
    import re
    m = re.search(rf"jp\.{prop} = (\$\w+|'[^']*')", cypher)
    if not m:
        raise KeyError(prop)
    token = m.group(1)
    return params[token[1:]] if token.startswith("$") else token[1:-1]


def _props(cypher: str, params: dict) -> dict:
    """Every ``jp.<prop>`` the statement SETs, with its value."""
    import re
    return {p: _set_value(cypher, params, p) for p in re.findall(r"jp\.(\w+) = ", cypher)}


def _row(*, from_rows=40, to_rows=12, from_values=12, missing=0, to_values=None,
         to_distinct=None):
    """The measurement read's one row: rows read from the referencing table and
    the target table, distinct non-null referencing values, how many of those
    the target column lacks, non-null target values, distinct target values."""
    to_values = to_rows if to_values is None else to_values
    to_distinct = to_rows if to_distinct is None else to_distinct
    return [from_rows, to_rows, from_values, missing, to_values, to_distinct]


def _provision(monkeypatch, joins, answer, ctx=None):
    """Run b3 over ``joins`` with ``answer(sql, params, kwargs)`` as the
    database (a dict result, or an exception to raise). Returns ``(merges,
    reads, ctx)``: the JoinPath MERGEs as ``{prop: value}`` and every read."""
    writes: list = []
    reads: list = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [dict(j) for j in joins])

    def pg(schema, sql, params=None, **kwargs):
        reads.append({"schema": schema, "sql": sql, "params": params, "kwargs": kwargs,
                      "connect_cap": _connect_cap()})
        result = answer(sql, params, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(onboard, "pg", pg)
    ctx = ctx or _ctx()
    onboard._dp_internal_joinpaths(ctx)
    merges = [{**_props(c, p), "jpid": p.get("jpid")}
              for c, p in writes if c.startswith("MERGE (jp:JoinPath")]
    return merges, reads, ctx


def _rows(row):
    return lambda *_a: {"rows": [list(row)]}


# --------------------------------------------------------------------------- #
# (a) the measurement decides the basis
# --------------------------------------------------------------------------- #
def test_a_guess_whose_values_are_all_in_a_unique_target_column_is_recorded_as_measured(
        monkeypatch):
    (merge,), reads, _ctx_ = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()))
    assert merge["method"] == "foreign_key_match"           # readers key on method: unchanged
    assert merge["basis"] == MEASURED
    assert merge["reference_count"] == 12 and merge["key_count"] == 12
    assert merge["constraint_name"] == ""
    assert "reference_check" not in merge
    assert len(reads) == 1                                 # one read per join


def test_a_measured_reference_records_when_it_was_read_in_utc(monkeypatch):
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    (merge,), _reads, _c = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()))
    measured_at = datetime.datetime.fromisoformat(merge["measured_at"])
    assert measured_at.utcoffset() == datetime.timedelta(0)
    assert before <= measured_at <= datetime.datetime.now(datetime.timezone.utc)
    # A guess carries no read time: nothing about it was proven then.
    (guess,), _r, _c2 = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row(missing=1)))
    assert "measured_at" not in guess


@pytest.mark.parametrize("row, reason", [
    (_row(missing=1), "values_missing"),
    (_row(to_distinct=11), "target_not_unique"),
    (_row(to_values=11, to_distinct=11), "target_not_unique"),
    (_row(from_values=2), "too_few_values"),
    (_row(from_values=0, missing=0), "too_few_values"),
], ids=["a value is not in the target", "duplicate target values", "a null target value",
        "two values", "no values"])
def test_a_guess_the_data_does_not_bear_out_stays_a_guess_and_says_why(monkeypatch, row, reason):
    (merge,), _reads, _c = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(row))
    assert merge["basis"] == GUESS
    assert merge["reference_check"] == reason
    assert "reference_count" not in merge and "key_count" not in merge


@pytest.mark.parametrize("side", ["from", "to"])
def test_a_table_over_the_row_cap_is_not_measured_and_the_reason_is_recorded(monkeypatch, side):
    limit = onboard.REFERENCE_MEASURE_ROW_LIMIT
    row = _row(from_rows=limit + 1) if side == "from" else _row(to_rows=limit + 1, to_values=12,
                                                               to_distinct=12)
    (merge,), reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(row))
    assert merge["basis"] == GUESS and merge["reference_check"] == "too_large"
    # The read itself stops one row past the cap on each side.
    assert reads[0]["params"] == (limit + 1, limit + 1)
    assert ctx.reference_not_measured == [
        {"from_table": "table_x", "from_field": "item_id", "to_table": "items",
         "to_field": "item_id", "reason": "too_large"}]


@pytest.mark.parametrize("answer, reason", [
    (lambda *_a: {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                  "error_category": "OperationalError"}, "failed"),
    (lambda *_a: {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                  "error_category": "QueryCanceled"}, "timeout"),
    (lambda *_a: RuntimeError("boom"), "failed"),
    (lambda *_a: {"rows": []}, "failed"),
    (lambda *_a: {"rows": [[1, 2, 3]]}, "failed"),
], ids=["unavailable", "statement deadline", "raised", "no row", "short row"])
def test_a_read_that_fails_or_times_out_stays_a_guess_with_the_reason(monkeypatch, answer, reason):
    (merge,), _reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], answer)
    assert merge["basis"] == GUESS and merge["reference_check"] == reason
    assert [m["reason"] for m in ctx.reference_not_measured] == [reason]


def test_each_read_carries_a_statement_deadline_no_longer_than_the_per_read_limit(monkeypatch):
    _m, reads, _c = _provision(monkeypatch, [_guess("table_x", "item_id", "items"),
                                             _guess("table_y", "item_id", "items")],
                               _rows(_row()))
    assert len(reads) == 2
    for read in reads:
        assert 0 < read["kwargs"]["statement_timeout_ms"] <= onboard.REFERENCE_READ_TIMEOUT_MS


def test_each_read_waits_to_connect_at_most_the_connect_time(monkeypatch):
    _m, reads, _c = _provision(monkeypatch, [_guess("table_x", "item_id", "items"),
                                             _guess("table_y", "item_id", "items")],
                               _rows(_row()))
    assert [read["connect_cap"] for read in reads] == [CONNECT_S, CONNECT_S]
    assert _connect_cap() is None                          # and only for those reads


def test_joins_past_the_cap_are_not_read_and_are_reported(monkeypatch):
    monkeypatch.setattr(onboard, "REFERENCE_MAX_MEASURED_JOINS", 1)
    merges, reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items"), _guess("table_y", "item_id", "items")],
        _rows(_row()))
    assert len(reads) == 1
    assert [(m["basis"], m.get("reference_check")) for m in merges] == [
        (MEASURED, None), (GUESS, "cap")]
    assert [(m["from_table"], m["reason"]) for m in ctx.reference_not_measured] == [
        ("table_y", "cap")]


def test_no_read_starts_once_the_provision_time_budget_is_spent(monkeypatch):
    spent = _ctx(reference_started_at=time.monotonic() - onboard.REFERENCE_TOTAL_BUDGET_S - 1)
    (merge,), reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()), ctx=spent)
    assert reads == []
    assert merge["basis"] == GUESS and merge["reference_check"] == "budget"
    assert [m["reason"] for m in ctx.reference_not_measured] == ["budget"]


def test_a_read_never_runs_past_what_is_left_of_the_budget(monkeypatch):
    """Connect and statement together fit in what is left."""
    left_s = 3.2
    ctx = _ctx(reference_started_at=time.monotonic() - onboard.REFERENCE_TOTAL_BUDGET_S + left_s)
    _m, reads, _c = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()), ctx=ctx)
    (read,) = reads
    assert read["connect_cap"] is not None
    assert read["connect_cap"] * 1000 + read["kwargs"]["statement_timeout_ms"] <= left_s * 1000


def test_no_read_starts_without_room_to_connect_and_run_a_statement(monkeypatch):
    left_s = CONNECT_S + onboard.REFERENCE_MIN_READ_MS / 1000 - 0.2
    ctx = _ctx(reference_started_at=time.monotonic() - onboard.REFERENCE_TOTAL_BUDGET_S + left_s)
    (merge,), reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()), ctx=ctx)
    assert reads == []
    assert merge["basis"] == GUESS and merge["reference_check"] == "budget"


def test_a_connection_that_never_answers_cannot_hold_b3_past_its_budget(monkeypatch):
    """The real ``db.pg`` over a connect that hangs as a black-holed endpoint
    does: libpq gives up after ``connect_timeout`` (at least 2 s), and without
    one it would wait far longer (``hang_s`` here)."""
    budget_s, hang_s = 3.0, 6.0
    monkeypatch.setattr(onboard, "REFERENCE_TOTAL_BUDGET_S", budget_s)
    monkeypatch.setattr(db, "_SCHEMA_CONN_CACHE", {})
    monkeypatch.setattr(db, "_records_conn_params_for_schema",
                        lambda _schema: {"host": "192.0.2.1", "port": "5432", "dbname": "d",
                                         "user": "u", "password": "p"})
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    waits: list = []

    def hanging_connect(**kwargs):
        timeout = int(kwargs.get("connect_timeout") or 0)
        waits.append(kwargs.get("connect_timeout"))
        time.sleep(max(2, timeout) if timeout > 0 else hang_s)
        raise pgdriver.OperationalError("timeout expired")
    monkeypatch.setattr(db.pgdriver, "connect", hanging_connect)
    monkeypatch.setattr(onboard, "pg", db.pg)
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [
        _guess("table_x", "item_id", "items"), _guess("table_y", "item_id", "items"),
        _guess("table_z", "item_id", "items")])
    ctx = _ctx()
    started = time.monotonic()
    onboard._dp_internal_joinpaths(ctx)
    elapsed = time.monotonic() - started
    assert elapsed <= budget_s + 0.5, elapsed
    assert waits == [CONNECT_S]                            # one capped attempt, then the budget
    merges = [_props(c, p) for c, p in writes if c.startswith("MERGE (jp:JoinPath")]
    assert [(m["basis"], m["reference_check"]) for m in merges] == [
        (GUESS, "failed"), (GUESS, "budget"), (GUESS, "budget")]
    assert [m["reason"] for m in ctx.reference_not_measured] == ["failed", "budget", "budget"]


def test_a_connect_slower_than_its_cap_overruns_by_that_one_connect_only(monkeypatch):
    """libpq applies ``connect_timeout`` to each address a host resolves to, so
    one connect can outlast the cap (two addresses here). The budget is read
    from the wall clock before every read, so the reads after it don't start:
    b3 can pass its budget by that one connect, not by one per join."""
    budget_s, per_address_s, addresses = 3.0, 1.0, 3
    monkeypatch.setattr(onboard, "REFERENCE_TOTAL_BUDGET_S", budget_s)
    monkeypatch.setattr(db, "_SCHEMA_CONN_CACHE", {})
    monkeypatch.setattr(db, "_records_conn_params_for_schema",
                        lambda _schema: {"host": "db.example.com", "port": "5432", "dbname": "d",
                                         "user": "u", "password": "p"})
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    connects: list = []

    def slow_connect(**kwargs):
        connects.append(kwargs.get("connect_timeout"))
        time.sleep(per_address_s * addresses)   # every address times out in turn
        raise pgdriver.OperationalError("timeout expired")
    monkeypatch.setattr(db.pgdriver, "connect", slow_connect)
    monkeypatch.setattr(onboard, "pg", db.pg)
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a: [
        _guess(f"table_{n}", "item_id", "items") for n in "vwxyz"])
    ctx = _ctx()
    started = time.monotonic()
    onboard._dp_internal_joinpaths(ctx)
    elapsed = time.monotonic() - started
    assert len(connects) == 1, connects
    assert elapsed <= max(budget_s, per_address_s * addresses) + 0.5, elapsed
    merges = [_props(c, p) for c, p in writes if c.startswith("MERGE (jp:JoinPath")]
    assert [m["reference_check"] for m in merges] == ["failed"] + ["budget"] * 4


def test_a_declared_key_is_not_read_and_gets_no_measurement(monkeypatch):
    declared = {"from_table": "table_x", "from_field": "key_b_id", "to_table": "table_b",
                "to_field": "key_b_id", "basis": "declared_foreign_key", "constraint_name": "fk_x"}
    merges, reads, ctx = _provision(
        monkeypatch, [declared, _guess("table_x", "item_id", "items")], _rows(_row()))
    assert len(reads) == 1 and "table_b" not in reads[0]["sql"]
    assert merges[0]["basis"] == "declared_foreign_key"
    assert not {"reference_count", "key_count", "reference_check"} & set(merges[0])
    assert merges[1]["basis"] == MEASURED


def test_the_read_names_both_columns_and_is_scoped_to_the_system_schema(monkeypatch):
    _m, reads, _c = _provision(
        monkeypatch, [_guess("work_orders", "production_run_id", "production_runs")],
        _rows(_row()))
    (read,) = reads
    assert read["schema"] == SCHEMA
    for name in (f'"{SCHEMA}"."work_orders"', f'"{SCHEMA}"."production_runs"',
                 '"production_run_id"'):
        assert name in read["sql"]


def test_the_read_compares_the_columns_as_they_are(monkeypatch):
    """No cast: the read uses the equality a join on the two columns would."""
    _m, reads, _c = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], _rows(_row()))
    sql = reads[0]["sql"].lower()
    assert "cast(" not in sql and "::" not in sql


@pytest.mark.parametrize("answer", [
    lambda *_a: {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                 "error_category": "UndefinedFunction"},
    # What pgdriver raises for SQLSTATE 42883 (undefined_function).
    lambda *_a: pgdriver.errors.UndefinedFunction("operator does not exist: uuid = text",
                                                  pgcode="42883"),
    lambda *_a: {"columns": [], "rows": [], "row_count": 0, "unavailable": True,
                 "error_category": "DatatypeMismatch"},
], ids=["no operator (db.pg)", "no operator (raised)", "datatype mismatch"])
def test_columns_postgres_cannot_compare_stay_a_guess(monkeypatch, answer):
    (merge,), _reads, ctx = _provision(
        monkeypatch, [_guess("table_x", "item_id", "items")], answer)
    assert merge["basis"] == GUESS and merge["reference_check"] == "type_mismatch"
    # The read ran and settled it: the join as written cannot execute.
    assert ctx.reference_not_measured == []


# --------------------------------------------------------------------------- #
# Other tables the column name could mean (``alternatives``)
# --------------------------------------------------------------------------- #
def _with_alternatives(join: dict, *tables) -> dict:
    return {**join, "alternatives": list(tables)}


def _by_target(rows: dict, default=None):
    """Answer each read with the row of the first target table its SQL names."""
    def answer(sql, *_a):
        for table, row in rows.items():
            if f'."{table}"' in sql:
                return row if isinstance(row, (dict, BaseException)) else {"rows": [list(row)]}
        return {"rows": [list(default or _row())]}
    return answer


def test_two_target_tables_whose_keys_hold_the_same_values_promote_neither(monkeypatch):
    join = _with_alternatives(_guess("events", "line_id", "assembly_lines"), "production_lines")
    (merge,), reads, ctx = _provision(monkeypatch, [join], _rows(_row()))
    assert merge["basis"] == GUESS and merge["reference_check"] == "ambiguous_target"
    assert merge["to_table"] == f"{SCHEMA}.assembly_lines"
    assert "reference_count" not in merge and "measured_at" not in merge
    assert ['"production_lines"' in r["sql"] for r in reads] == [False, True]
    assert ctx.reference_not_measured == []                # read, and settled


def test_only_the_written_target_holding_the_values_is_measured(monkeypatch):
    join = _with_alternatives(_guess("events", "line_id", "assembly_lines"),
                              "production_lines", "test_lines")
    (merge,), reads, _ctx_ = _provision(monkeypatch, [join], _by_target({
        "production_lines": _row(missing=4), "test_lines": _row(to_distinct=3)}))
    assert merge["basis"] == MEASURED
    assert len(reads) == 3                                 # every candidate was read


def test_a_candidate_whose_data_could_not_be_read_keeps_the_guess(monkeypatch):
    join = _with_alternatives(_guess("events", "line_id", "assembly_lines"), "production_lines")
    (merge,), _reads, ctx = _provision(monkeypatch, [join], _by_target({
        "production_lines": {"rows": [], "unavailable": True, "error_category": "QueryCanceled"}}))
    assert merge["basis"] == GUESS and merge["reference_check"] == "timeout"
    assert ctx.reference_not_measured == [
        {"from_table": "events", "from_field": "line_id", "to_table": "assembly_lines",
         "to_field": "line_id", "reason": "timeout", "candidate": "production_lines"}]


def test_a_written_target_the_data_contradicts_reads_no_other_candidate(monkeypatch):
    join = _with_alternatives(_guess("events", "line_id", "assembly_lines"), "production_lines")
    (merge,), reads, _ctx_ = _provision(monkeypatch, [join], _rows(_row(missing=1)))
    assert merge["basis"] == GUESS and merge["reference_check"] == "values_missing"
    assert len(reads) == 1                                 # the outcome cannot change


def _catalog_pg(*, tables, id_cols, column_rows=lambda _t: {"rows": [[1]]}, truncated=False):
    """A ``pg`` stand-in answering the name heuristic's catalog reads."""
    def pg(_schema, sql, params=None, **_k):
        if "information_schema.tables" in sql:
            return {"rows": [[t] for t in tables]}
        if "column_name LIKE" in sql:
            res = {"rows": [list(r) for r in id_cols]}
            return {**res, "truncated": True} if truncated else res
        if "SELECT 1" in sql:
            return column_rows(params[1])
        return {"rows": []}
    return pg


def _detect(monkeypatch, pg, keys=None):
    monkeypatch.setattr(onboard, "pg", pg)
    monkeypatch.setattr(onboard, "_measured_foreign_keys", lambda _s: [])
    monkeypatch.setattr(onboard, "_primary_key_columns", lambda _s: dict(keys or {}))
    return onboard._detect_internal_joinpaths(SCHEMA, SID)


_LINES = ["events", "assembly_lines", "production_lines"]
_LINE_KEYS = {("assembly_lines", "line_id"): "11", ("production_lines", "line_id"): "12"}


def test_the_heuristic_records_every_other_table_the_name_could_mean(monkeypatch):
    joins = _detect(monkeypatch, _catalog_pg(
        tables=_LINES, id_cols=[(t, "line_id") for t in _LINES]), _LINE_KEYS)
    assert joins == [{"from_table": "events", "from_field": "line_id",
                      "to_table": "assembly_lines", "to_field": "line_id", "basis": GUESS,
                      "alternatives": ["production_lines"]}]


def test_a_table_without_the_column_is_no_alternative(monkeypatch):
    joins = _detect(monkeypatch, _catalog_pg(
        tables=_LINES, id_cols=[("events", "line_id"), ("assembly_lines", "line_id")]),
        _LINE_KEYS)
    assert [j.get("alternatives") for j in joins] == [None]


def test_a_cut_short_column_read_checks_each_candidate_and_keeps_an_unread_one(monkeypatch):
    unread = {"rows": [], "unavailable": True, "error_category": "OperationalError"}
    joins = _detect(monkeypatch, _catalog_pg(
        tables=_LINES + ["test_lines"], id_cols=[("events", "line_id")], truncated=True,
        column_rows=lambda t: unread if t == "test_lines" else {"rows": [[1]]}),
        {**_LINE_KEYS, ("test_lines", "line_id"): "13"})
    assert joins[0]["alternatives"] == ["production_lines", "test_lines"]


def test_the_provision_result_reports_the_joins_it_could_not_measure(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    skipped = {"from_table": "table_x", "from_field": "item_id", "to_table": "items",
               "to_field": "item_id", "reason": "timeout"}
    ctx = _ctx(mcp_url="https://gateway.example/mcp", tool_name="query_sys_a",
               reference_not_measured=[skipped])
    assert onboard._dp_result(ctx)["reference_joins_not_measured"] == [skipped]
    assert "reference_joins_not_measured" not in onboard._dp_result(_ctx(
        mcp_url="https://gateway.example/mcp", tool_name="query_sys_a"))


def test_the_measured_basis_and_its_numbers_are_returned_with_every_join_path():
    assert MEASURED in ce.JOIN_BASES
    for field in ("reference_count", "key_count", "measured_at", "reference_check"):
        assert field in ce.JOIN_PATH_EVIDENCE_FIELDS, field
        assert field in onboard.JOIN_PATH_RESULT_FIELDS, field


# --------------------------------------------------------------------------- #
# (c) a measured MES run join pairs the forms per item (the repro's rows)
# --------------------------------------------------------------------------- #
#: The six name-pattern joins every plant MES view schema gets (the 2026-10-17
#: rehearsal inventory, system and schema names neutral): only the work-order
#: to run join pairs the two forms.
_MES, _ERP, _MES_SCHEMA, _ERP_SCHEMA = "mes-a", "erp-a", "sch_m", "sch_e"
_MES_GUESSES = [
    _guess("downtime_events", "line_id", "production_lines"),
    _guess("downtime_events", "work_order_id", "work_orders"),
    _guess("material_consumption", "work_order_id", "work_orders"),
    _guess("production_events", "work_order_id", "work_orders"),
    _guess("quality_checks", "work_order_id", "work_orders"),
    _guess("work_orders", "production_run_id", "production_runs"),
]


def _exact_rule(rid, source_field, target_field):
    return {"id": rid, "concept": "batch", "source_system": _MES,
            "source_field": source_field, "target_system": _ERP, "target_field": target_field,
            "source_pattern": "", "target_pattern": "", "method": ia.EXACT_METHOD,
            "confidence": 0.85}


#: The two exact batch rules and their exact join paths, shaped as the
#: rehearsal's route seed carried them (rules on leaf columns, join paths
#: table-qualified, no shape in the provenance).
_WO_RULE = _exact_rule("ir-mes-a-7cb2528a7e89299f", "work_order_number", "batch_id")
_RUN_RULE = _exact_rule("ir-mes-a-9e31bdb89260cbc2", "run_number", "batch_id")
_EXACT_JPS = [
    {"id": "jp-mes-a-7cb2528a7e89299f", "from_system": _MES,
     "from_field": f"{_MES_SCHEMA}.work_orders.work_order_number", "to_system": _ERP,
     "to_field": f"{_ERP_SCHEMA}.lot_mes_tracking.batch_id", "transform": "identity",
     "method": ia.EXACT_METHOD},
    {"id": "jp-mes-a-9e31bdb89260cbc2", "from_system": _MES,
     "from_field": f"{_MES_SCHEMA}.production_runs.run_number", "to_system": _ERP,
     "to_field": f"{_ERP_SCHEMA}.shipment_items.batch_id", "transform": "identity",
     "method": ia.EXACT_METHOD},
]
_QUESTION = ("Which customers received parts from any batch that drew the same supplier lot "
             "as batch <your run number>, and what is the value of the parts each of them "
             "received, and the total?")
_FORMS_LINE = (
    f"- {_ERP} batch, from {_MES}: {_ERP_SCHEMA}.lot_mes_tracking.batch_id holds "
    f"work_orders.work_order_number values; {_ERP_SCHEMA}.shipment_items.batch_id holds "
    "production_runs.run_number values; per item the source pairs them by "
    "production_runs.production_run_id = work_orders.production_run_id, so ask it for the "
    "other form")


def _seed_rows(merges, system):
    """The written JoinPaths as the route-seed read returns them: table-qualified
    endpoints, method and basis (orchestrator ``_ROUTE_SEED_CYPHER``)."""
    return [{"id": m["jpid"], "from_system": system,
             "from_field": f"{m['from_table']}.{m['from_field']}", "to_system": system,
             "to_field": f"{m['to_table']}.{m['to_field']}", "transform": m["transform"],
             "method": m["method"], "basis": m["basis"]} for m in merges]


def _written_mes_joins(monkeypatch, row_for):
    """b3 over the six MES guesses; ``row_for(sql)`` answers each read."""
    merges, _reads, _ctx_ = _provision(
        monkeypatch, _MES_GUESSES, lambda sql, *_a: {"rows": [list(row_for(sql))]},
        ctx=_ctx(_MES, _MES_SCHEMA))
    return _seed_rows(merges, _MES)


def test_a_measured_run_join_puts_the_identifier_forms_line_in_the_seed(monkeypatch):
    joins = _written_mes_joins(monkeypatch, lambda _sql: _row())
    run_join = next(j for j in joins if j["to_field"].endswith("production_runs.production_run_id"))
    assert run_join["basis"] == MEASURED
    rules = [_WO_RULE, _RUN_RULE]
    lines = it.find_field_forms(rules, _EXACT_JPS + joins, _QUESTION)
    assert [(line["pairer"], line["pairing"]) for line in lines] == [
        ("source", ("production_runs.production_run_id = work_orders.production_run_id",))]
    assert it.render_forms(lines).splitlines()[1:] == [_FORMS_LINE]

    # The same rows through the route seed (the graph read is faked).
    inventory = [{"system_id": s, "system_name": s.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None} for s in (_ERP, _MES)]
    seed_rows = [{**inv, "mapped_concepts": [], "shared_concepts": [], "value_mappings": [],
                  "join_paths": [j for j in _EXACT_JPS + joins
                                 if inv["system_id"] in (j["from_system"], j["to_system"])],
                  "identity_rules": rules} for inv in inventory]

    def graph(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        return seed_rows if cypher == orch._ROUTE_SEED_CYPHER else []
    seed = orch.build_route_seed(query=graph, question=_QUESTION)
    assert it.FORMS_HEADER in seed and _FORMS_LINE in seed


def test_a_run_join_the_data_contradicts_still_pairs_nothing(monkeypatch):
    """A measured guess that fails the check is still a bare guess: no line."""
    joins = _written_mes_joins(
        monkeypatch, lambda sql: _row(missing=3) if "production_runs" in sql else _row())
    run_join = next(j for j in joins if j["to_field"].endswith("production_runs.production_run_id"))
    assert run_join["basis"] == GUESS
    assert it.find_field_forms([_WO_RULE, _RUN_RULE], _EXACT_JPS + joins, _QUESTION) == []
    # ...while the other measured joins are recorded as measured.
    assert {j["basis"] for j in joins if j is not run_join} == {MEASURED}


# --------------------------------------------------------------------------- #
# The metric layer: a measured reference counts exactly as the naming guess
# --------------------------------------------------------------------------- #
_SUPPLY = ("supply-system", "supply_data")


def _supply_edge(join_id, ft, tt, method, basis=None):
    edge = {"join_id": join_id, "from_table": f"supply_data.{ft}", "from_field": "supplier_id",
            "to_table": f"supply_data.{tt}", "to_field": "supplier_id",
            "transform": "direct", "method": method}
    return {**edge, "basis": basis} if basis else edge


_REVERSES = {
    "alone": [],
    "value overlap reverse": [_supply_edge("reverse", "suppliers", "item_suppliers",
                                           "value_overlap", "value_overlap")],
    "steward-confirmed reverse": [_supply_edge("reverse", "suppliers", "item_suppliers",
                                               "user_confirmed")],
    "crosswalk-confirmed reverse": [_supply_edge("reverse", "suppliers", "item_suppliers",
                                                 "crosswalk_confirmed")],
}


def _outcome(edges, measure, dimension):
    try:
        join = _select_join_path(_SUPPLY[0], measure, dimension, query=lambda c, p=None: edges)
    except MetricResolverError as exc:
        return ("error", str(exc))
    return ("join", join.join_id, join.from_table, join.to_table)


@pytest.mark.parametrize("reverse", list(_REVERSES), ids=list(_REVERSES))
@pytest.mark.parametrize("measure, dimension", [("item_suppliers", "suppliers"),
                                                ("suppliers", "item_suppliers")],
                         ids=["measure on the many side", "measure on the one side"])
def test_the_metric_layer_treats_a_measured_reference_exactly_as_the_naming_guess(
        monkeypatch, reverse, measure, dimension):
    (merge,), _reads, _c = _provision(
        monkeypatch, [_guess("item_suppliers", "supplier_id", "suppliers")], _rows(_row()),
        ctx=_ctx(*_SUPPLY))
    assert merge["basis"] == MEASURED
    measured = {"join_id": merge["jpid"], "from_table": merge["from_table"],
                "from_field": merge["from_field"], "to_table": merge["to_table"],
                "to_field": merge["to_field"], "transform": merge["transform"],
                "method": merge["method"], "basis": merge["basis"]}
    guess = {**measured, "basis": GUESS}
    others = _REVERSES[reverse]
    assert (_outcome([*others, measured], measure, dimension)
            == _outcome([*others, guess], measure, dimension))


def test_a_measured_reference_does_not_settle_a_mirror_the_metric_layer_refuses(monkeypatch):
    """The pinned shape: a measured forward edge and a reverse edge of
    another method stay ambiguous, as they are for a guess on main."""
    (merge,), _reads, _c = _provision(
        monkeypatch, [_guess("item_suppliers", "supplier_id", "suppliers")], _rows(_row()),
        ctx=_ctx(*_SUPPLY))
    assert merge["basis"] == MEASURED
    measured = _supply_edge(merge["jpid"], "item_suppliers", "suppliers", merge["method"],
                            merge["basis"])
    for reverse in ("value overlap reverse", "steward-confirmed reverse"):
        with pytest.raises(MetricResolverError, match="ambiguous"):
            _select_join_path(_SUPPLY[0], "item_suppliers", "suppliers",
                              query=lambda c, p=None, r=reverse: [*_REVERSES[r], measured])


def test_a_mirrored_guess_the_data_contradicts_still_fails_loud(monkeypatch):
    (merge,), _reads, _c = _provision(
        monkeypatch, [_guess("item_suppliers", "supplier_id", "suppliers")],
        _rows(_row(to_distinct=5)), ctx=_ctx(*_SUPPLY))
    assert merge["basis"] == GUESS and merge["reference_check"] == "target_not_unique"
    guess = _supply_edge("guess", "item_suppliers", "suppliers", merge["method"], merge["basis"])
    reverse = _supply_edge("reverse", "suppliers", "item_suppliers", "value_overlap")
    with pytest.raises(MetricResolverError, match="ambiguous"):
        _select_join_path(_SUPPLY[0], "item_suppliers", "suppliers",
                          query=lambda c, p=None: [guess, reverse])
