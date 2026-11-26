# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An existing account's hot-path indexes are built concurrently, inside a budget.

db-init creates the hot-path indexes (hot_path_indexes.py) with the schema; an
existing account gets them from the orchestrator at cycle start. A plain
``CREATE INDEX`` there holds a SHARE lock on the table for the whole build, so
every writer of the table waits for it, and its lock timeout bounds only the
wait for that lock, never the build: on a large table one build can run past
the Lambda's timeout, roll back and start again the next cycle.

The pass now runs ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` on an autocommit
session (a concurrent build cannot run inside a transaction block) under a
statement timeout taken from one budget for the whole pass
(``INDEX_BUILD_BUDGET_SECONDS``). A build that fails or runs out of budget is
skipped for that cycle and the cycle's steps still run. A failed concurrent
build leaves an INVALID index behind (``pg_index.indisvalid``); the next pass
drops it, concurrently, and builds it again.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from erp_simulation import hot_path_indexes as hpi
from erp_simulation import pgdriver
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation.tests.test_cycle_isolation import (  # noqa: F401 - pg_erp is a fixture
    _orchestrator, _simulator_calls, pg_erp)
from erp_simulation.tests.test_simulator_single_flight import _PgDb, _test_db_params

_NAMES = [name for name, _table, _columns in hpi.HOT_PATH_INDEXES]


# --------------------------------------------------------------------------
# A scripted session (runs in every lane).
# --------------------------------------------------------------------------
class _Session:
    """The step connection. ``catalog`` maps index name -> ``indisvalid``;
    ``fail`` names the indexes whose CREATE is cancelled; ``on_create`` runs
    before each CREATE. Records ``(statement, params, autocommit)``."""

    def __init__(self, catalog=(), fail=(), on_create=None):
        self.catalog, self.fail, self.on_create = dict(catalog), set(fail), on_create
        self.autocommit = False
        self.calls = []

    def cursor(self):
        return _SessionCursor(self)


class _SessionCursor:
    def __init__(self, session):
        self.session, self._rows = session, []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        session = self.session
        session.calls.append((text, params, session.autocommit))
        self._rows = []
        if "FROM pg_index" in text:
            self._rows = [(name, valid) for name, valid in session.catalog.items() if name in params[0]]
        elif text.startswith("CREATE INDEX"):
            if session.on_create is not None:
                session.on_create()
            if any(f" {name} " in text for name in session.fail):
                raise pgdriver.QueryCanceledError("canceling statement due to statement timeout")

    def fetchall(self):
        return list(self._rows)


def _ddl(session):
    return [text for text, _params, _autocommit in session.calls if text.startswith(("CREATE", "DROP"))]


def _timeouts(session):
    return [params[0] for text, params, _a in session.calls
            if text == "SELECT set_config('statement_timeout', %s, false)"]


def test_missing_indexes_are_built_concurrently_outside_a_transaction_block():
    session = _Session(catalog={_NAMES[0]: True})
    assert hpi.ensure_hot_path_indexes(session) == _NAMES[1:]
    assert _ddl(session) == [hpi.concurrent_index_ddl(*spec) for spec in hpi.HOT_PATH_INDEXES[1:]]
    assert all(text.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS ") for text in _ddl(session))
    assert all(autocommit for _text, _params, autocommit in session.calls)
    assert session.autocommit is False                     # handed back as it came
    assert session.calls[-1][0] == "RESET statement_timeout"


def test_every_valid_index_is_one_catalog_read_and_nothing_else():
    session = _Session(catalog={name: True for name in _NAMES})
    assert hpi.ensure_hot_path_indexes(session) == []
    assert len(session.calls) == 1 and "FROM pg_index" in session.calls[0][0]
    assert session.autocommit is False


def test_an_invalid_index_is_dropped_concurrently_then_built_again():
    session = _Session(catalog={**{name: True for name in _NAMES}, _NAMES[2]: False})
    assert hpi.ensure_hot_path_indexes(session) == [_NAMES[2]]
    assert _ddl(session) == [f"DROP INDEX CONCURRENTLY IF EXISTS {_NAMES[2]}",
                             hpi.concurrent_index_ddl(*hpi.HOT_PATH_INDEXES[2])]


def test_a_failed_build_skips_the_rest_of_the_pass_without_raising(caplog):
    session = _Session(fail={_NAMES[1]})
    with caplog.at_level(logging.WARNING):
        assert hpi.ensure_hot_path_indexes(session) == _NAMES[:1]
    assert _ddl(session) == [hpi.concurrent_index_ddl(*spec) for spec in hpi.HOT_PATH_INDEXES[:2]]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and _NAMES[1] in warnings[0] and "next cycle" in warnings[0]
    assert session.calls[-1][0] == "RESET statement_timeout" and session.autocommit is False


def test_every_build_gets_what_is_left_of_one_budget_as_its_statement_timeout(monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr(hpi, "_monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(hpi, "INDEX_BUILD_BUDGET_SECONDS", 60.0, raising=False)
    session = _Session(on_create=lambda: clock.__setitem__(0, clock[0] + 25.0))
    with caplog.at_level(logging.WARNING):
        assert hpi.ensure_hot_path_indexes(session) == _NAMES[:3]
    # A bound value (never interpolated), in ms: 60 s, then 35 s, then 10 s; the fourth has none left.
    assert _timeouts(session) == ["60000", "35000", "10000"]
    assert len(_ddl(session)) == 3
    assert any(_NAMES[3] in r.getMessage() and "budget" in r.getMessage() for r in caplog.records)


def test_the_drop_and_the_build_of_an_invalid_index_share_what_is_left_of_the_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(hpi, "_monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(hpi, "INDEX_BUILD_BUDGET_SECONDS", 60.0, raising=False)
    session = _Session(catalog={**{name: True for name in _NAMES}, _NAMES[0]: False})
    original = _SessionCursor.execute

    def slow_drop(self, sql, params=None):
        original(self, sql, params)
        if sql.startswith("DROP INDEX"):
            clock[0] += 40.0
    monkeypatch.setattr(_SessionCursor, "execute", slow_drop)
    assert hpi.ensure_hot_path_indexes(session) == [_NAMES[0]]
    assert _timeouts(session) == ["60000", "20000"]        # the build gets what the drop left


def test_the_budget_leaves_the_required_steps_their_time():
    """The pass runs before the required steps; it must leave them most of the
    Lambda's 300 s even after slow clock steps."""
    assert 0 < hpi.INDEX_BUILD_BUDGET_SECONDS <= 60
    assert hpi.INDEX_BUILD_BUDGET_SECONDS < orch_mod.CYCLE_TIME_BUDGET_SECONDS / 2


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise). The pass runs inside
# a real cycle: the simulator lock is held on its own dedicated session (idle in
# its transaction) while the step connection builds the index.
# --------------------------------------------------------------------------
# 300 rows whose index expression sleeps 5 ms a row: one build scan takes ~1.5 s.
_SLOW = ("idx_hp_build_slow", "hp_build_scratch", "(hp_build_slow(v))")
_STATUS = ("idx_hp_build_status", "hp_build_scratch", "(status)")


@pytest.fixture
def scratch(pg_erp):
    with pg_erp.cursor() as cur:
        cur.execute("""
            CREATE TABLE hp_build_scratch (id serial PRIMARY KEY, v integer NOT NULL, status text NOT NULL);
            CREATE FUNCTION hp_build_slow(v integer) RETURNS integer LANGUAGE plpgsql IMMUTABLE
                AS $$ BEGIN PERFORM pg_sleep(0.005); RETURN v; END $$;
            INSERT INTO hp_build_scratch (v, status) SELECT g, 'S' || (g % 3) FROM generate_series(1, 300) g;
        """)
    pg_erp.commit()
    return pg_erp


def _index_state(conn, name):
    """``(indisvalid, indisunique)`` of index ``name``, or None when there is none."""
    with conn.cursor() as cur:
        cur.execute("""SELECT i.indisvalid, i.indisunique FROM pg_index i
                       JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s""", (name,))
        row = cur.fetchone()
    conn.rollback()
    return row


def _pg_cycle(conn):
    orch, log, _fake = _orchestrator()
    orch.db = _PgDb(conn)
    return orch, log


def _every_step():
    return [f"{s.owner}.{s.method}" if s.owner != 'self' else "self.suppliers"
            for s in orch_mod._SIMULATION_STEPS]


class _Writer(threading.Thread):
    """Another session inserting into the scratch table: one short transaction
    every 20 ms; records each insert's (start, end)."""

    def __init__(self, conn):
        super().__init__(daemon=True)
        self.params, self.stop, self.writes, self.error = _test_db_params(conn), threading.Event(), [], None

    def run(self):
        p = self.params
        conn = pgdriver.connect(host=p["host"], port=p["port"], dbname=p["database"],
                                user=p["user"], password=p["password"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                while not self.stop.is_set():
                    started = time.monotonic()
                    cur.execute("INSERT INTO hp_build_scratch (v, status) VALUES (0, 'W')")
                    self.writes.append((started, time.monotonic()))
                    time.sleep(0.02)
        except Exception as e:  # reported by the test
            self.error = e
        finally:
            conn.close()


def test_pg_a_cycle_builds_a_missing_index_while_another_session_keeps_writing(scratch, monkeypatch):
    monkeypatch.setattr(hpi, "HOT_PATH_INDEXES", (_SLOW,))
    window = {}
    build = orch_mod.ensure_hot_path_indexes

    def timed(*args):
        window["start"] = time.monotonic()
        try:
            return build(*args)
        finally:
            window["end"] = time.monotonic()

    monkeypatch.setattr(orch_mod, "ensure_hot_path_indexes", timed)
    orch, _log = _pg_cycle(scratch)
    writer = _Writer(scratch)
    writer.start()
    try:
        time.sleep(0.1)
        result = orch.run_single_cycle()
    finally:
        writer.stop.set()
        writer.join(timeout=10)
    assert writer.error is None
    assert result == {"success": True, "cycle_count": 1, "simulator": "ran"}
    assert _index_state(scratch, _SLOW[0]) == (True, False)
    during = [w for w in writer.writes if w[0] >= window["start"] and w[1] <= window["end"]]
    assert len(during) >= 5, (window, len(writer.writes))   # the writer never waited for the build


def test_pg_an_invalid_leftover_of_a_failed_concurrent_build_is_dropped_and_rebuilt(scratch, monkeypatch):
    monkeypatch.setattr(hpi, "HOT_PATH_INDEXES", (_STATUS,))
    scratch.autocommit = True
    try:
        with scratch.cursor() as cur, pytest.raises(pgdriver.IntegrityError) as caught:
            cur.execute("CREATE UNIQUE INDEX CONCURRENTLY idx_hp_build_status ON hp_build_scratch (status)")
        assert caught.value.pgcode == "23505"  # unique_violation
    finally:
        scratch.autocommit = False
    assert _index_state(scratch, _STATUS[0]) == (False, True)       # the leftover: INVALID
    orch, _log = _pg_cycle(scratch)
    assert orch.run_single_cycle()["simulator"] == "ran"
    assert _index_state(scratch, _STATUS[0]) == (True, False)        # dropped, then built as specified


def test_pg_a_build_past_the_budget_is_skipped_and_the_cycle_still_runs_its_steps(scratch, monkeypatch):
    monkeypatch.setattr(hpi, "HOT_PATH_INDEXES", (_SLOW,))
    monkeypatch.setattr(hpi, "INDEX_BUILD_BUDGET_SECONDS", 0.3, raising=False)   # the build needs ~1.5 s
    orch, log = _pg_cycle(scratch)
    assert orch.run_single_cycle() == {"success": True, "cycle_count": 1, "simulator": "ran"}
    assert orch._cycle_timings["indexes"] < 1.2, orch._cycle_timings   # stopped at the budget
    assert _simulator_calls(log) == _every_step()
    state = _index_state(scratch, _SLOW[0])
    assert state is None or state[0] is False                        # no usable index this cycle
    monkeypatch.setattr(hpi, "INDEX_BUILD_BUDGET_SECONDS", 60.0, raising=False)
    assert orch.run_single_cycle()["simulator"] == "ran"
    assert _index_state(scratch, _SLOW[0]) == (True, False)          # the next cycle repairs and builds it
    assert scratch.autocommit is False
