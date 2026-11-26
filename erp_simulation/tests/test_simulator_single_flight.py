# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One simulator at a time, and production receipts that drain a backlog.

Found live on a demo account (2026-10-22): the schedule fires every minute and
a cycle may run up to its 150 s budget, so cycles overlapped (about five at a
time once each hit the 300 s Lambda timeout). The production-receipt step read
its WHOLE backlog (236,810 completed runs) every cycle; the overlapping cycles
deadlocked on the same inventory rows, one deadlock aborted the step's
transaction, every later row failed with "current transaction is aborted", the
step rolled back and nothing was ever received. The cycle then ran out of time
before the shipment step, so no shipment was delivered for three months.

The lock lives on its OWN connection per cycle (never pooled, never reused),
as a transaction-level advisory lock in a transaction held open for the
steps, with a server-side idle bound: it cannot be re-entered by a reused
session, cannot outlive the connection, and an invocation that abandons it
(killed or frozen with its socket open) loses it when the server ends the
idle session.

Unit tests drive the REAL ``_run_cycle`` with the fake connection of
test_cycle_isolation; the ``pg_*`` tests (ERP_SIM_TEST_PG_DSN) prove the lock,
the abandoned-lock bound, the bounded batch and the per-row isolation on a
real PostgreSQL.
"""

import json
import logging
import threading
import time
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from erp_simulation import pgdriver
from erp_simulation.pgdriver import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INERROR

from erp_simulation import cycle_result
from erp_simulation import production_simulator as prod_mod
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation.database import DatabaseManager
from erp_simulation.production_simulator import ProductionSimulator
from erp_simulation.tests.test_cycle_isolation import (
    _DSN, _orchestrator, _simulator_calls, pg_erp,  # noqa: F401 - pg_erp is a fixture
)


def _executed(conn):
    return [" ".join(str(c.args[0]).split()) for c in conn.cursor_obj.execute.call_args_list]


def _lock_sql(conn):
    return [" ".join(str(c.args[0]).split()) for c in conn.lock_cursor.execute.call_args_list]


# ------------------------------------------------------------- unit ---

def test_a_busy_simulator_runs_only_the_clock_steps(caplog):
    orch, log, conn = _orchestrator()
    conn.simulator_free = False
    with caplog.at_level(logging.INFO):
        result = orch.run_single_cycle()
    assert result == {"success": True, "cycle_count": 0, "simulator": "busy"}
    assert log[:4] == ["clock", "story", "mes_simulator.run_condition_monitor", "anchor_mirror"]
    assert _simulator_calls(log) == []
    orch._record_simulation_event.assert_not_called()   # no all-zero sparkline row
    summary = next(r.getMessage() for r in caplog.records if "summary" in r.getMessage())
    assert "skipped=simulator:busy" in summary
    assert conn.lock_closed == 1                         # the probe's own session is closed


def test_the_lock_is_taken_on_its_own_connection_and_closed_after_the_steps():
    orch, log, conn = _orchestrator()
    assert orch.run_single_cycle()["simulator"] == "ran"
    sql = _lock_sql(conn)
    assert sql[0] == "SELECT pg_try_advisory_xact_lock(%s)"
    assert conn.lock_cursor.execute.call_args_list[0].args[1] == (orch_mod.SIMULATOR_LOCK_KEY,)
    assert sql[1:] == ["SELECT 1"] * len(orch_mod._SIMULATION_STEPS)   # one heartbeat per step
    assert not any("advisory" in s for s in _executed(conn))   # the pooled steps' session never holds it
    assert len(_simulator_calls(log)) == len(orch_mod._SIMULATION_STEPS)
    assert log[-2:] == ["lock-rollback", "lock-close"]           # released only after every step
    (kwargs,) = orch.db.lock_connections
    assert kwargs["idle_in_transaction_timeout_s"] == orch_mod.SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS
    assert not _heartbeat_threads()


def _heartbeat_threads():
    return [t for t in threading.enumerate()
            if t.name == "erp-simulator-lock-heartbeat" and t.is_alive()]


def test_the_lock_is_released_when_the_steps_raise(monkeypatch):
    orch, log, conn = _orchestrator()
    monkeypatch.setattr(orch, "_simulation_steps", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        orch._run_cycle()
    assert conn.lock_closed == 1 and log[-1] == "lock-close"
    assert not _heartbeat_threads()


@pytest.mark.parametrize("fault", ["connect", "probe"])
def test_a_failed_lock_check_fails_the_cycle_it_is_never_busy(fault, caplog):
    """A database fault must not read as healthy contention (HTTP 200 "busy"
    while the simulation stalls). The simulator steps are still skipped, but
    the cycle FAILS and ErrorCount is published."""
    orch, log, conn = _orchestrator()
    orch._publish_metric = MagicMock(name="_publish_metric")
    error = pgdriver.OperationalError("remaining connection slots are reserved")
    if fault == "connect":
        conn.lock_connect_error = error
    else:
        conn.lock_probe_error = error
    with caplog.at_level(logging.INFO):
        result = orch.run_single_cycle()
    assert result["success"] is False and result["simulator"] == "unavailable"
    assert "could not check the simulator lock" in result["error"]
    assert _simulator_calls(log) == []
    orch._publish_metric.assert_any_call("ErrorCount", 1)
    assert conn.lock_closed == (1 if fault == "probe" else 0)
    summary = next(r.getMessage() for r in caplog.records if "summary" in r.getMessage())
    assert "skipped=simulator:unavailable" in summary


def test_a_lock_lost_before_a_step_stops_the_cycle_there():
    """The heartbeat finds the lock session gone (its idle bound fired):
    another cycle may already be running, so this one stops before the next
    step instead of overlapping it. Committed steps stand; the cycle fails."""
    orch, log, conn = _orchestrator()
    orch._publish_metric = MagicMock(name="_publish_metric")
    calls = []

    def lock_execute(sql, *_args):
        calls.append(sql)
        if sql == "SELECT 1" and calls.count("SELECT 1") == 3:
            raise pgdriver.OperationalError("terminating connection due to idle-in-transaction timeout")
    conn.lock_cursor.execute.side_effect = lock_execute
    result = orch.run_single_cycle()
    assert result["success"] is False and result["simulator"] == "unavailable"
    assert "ended before step" in result["error"]
    assert len(_simulator_calls(log)) == 2                         # the two steps before the lost heartbeat
    assert log[log.index("lock-close") - 1] != "lock-rollback"     # a lost session is not rolled back
    orch._publish_metric.assert_any_call("ErrorCount", 1)
    orch._record_simulation_event.assert_not_called()


def test_a_lock_session_lost_mid_cycle_is_reported_not_raised(caplog):
    """The lock session ended before release (its idle bound fired, or the
    connection dropped): the committed steps stand and the summary still
    closes the cycle; the loss is said, never raised."""
    orch, _log, conn = _orchestrator()
    conn.lock_rollback_error = pgdriver.OperationalError(
        "terminating connection due to idle-in-transaction timeout")
    with caplog.at_level(logging.WARNING):
        assert orch.run_single_cycle() == {"success": True, "cycle_count": 1, "simulator": "ran"}
    assert any("simulator lock session ended before the cycle finished" in r.getMessage()
               for r in caplog.records)
    assert conn.lock_closed == 1


def test_the_lock_key_differs_from_the_story_clock_lock():
    from erp_simulation import story_replan
    assert orch_mod.SIMULATOR_LOCK_KEY != story_replan.LOCK_KEY


def test_the_idle_bound_outlasts_the_lambda_timeout():
    """The lock session is touched before every step, so its server-side idle
    bound must exceed the longest step (no step outlives the Lambda's 300 s
    timeout); it is also the bound on a lock an invocation abandons."""
    assert 300 < orch_mod.SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS <= 600


def test_the_dedicated_connection_carries_the_idle_bound_and_keepalives(monkeypatch):
    seen = {}
    monkeypatch.setattr(pgdriver, "connect", lambda **kw: seen.update(kw) or "conn")
    db = DatabaseManager()
    monkeypatch.setattr(db, "get_connection_params", lambda: {
        "host": "h", "port": 5432, "database": "d", "user": "u", "password": "p"})  # nosec B105 - fake
    assert db.dedicated_connection(idle_in_transaction_timeout_s=330,
                                   application_name="erp-simulator-lock") == "conn"
    assert seen["options"] == "-c idle_in_transaction_session_timeout=330000"
    assert seen["keepalives"] == 1 and seen["connect_timeout"] == 10
    assert seen["application_name"] == "erp-simulator-lock" and seen["dbname"] == "d"


# ----------------------------------------- synchronous callers (busy) ---

def _reply(simulator=None, status=200):
    body = {"success": True}
    if simulator:
        body["simulator"] = simulator
    return {"statusCode": status, "body": json.dumps(body)}


@pytest.mark.parametrize("error, payload, outcome", [
    (None, _reply("ran"), cycle_result.RAN),
    (None, _reply("busy"), cycle_result.BUSY),
    (None, _reply(), cycle_result.RAN),                 # an image built before the lock
    (None, {"ok": True}, cycle_result.RAN),
    (None, _reply(status=500), cycle_result.FAILED),
    ("Unhandled", {"errorMessage": "x"}, cycle_result.FAILED),
])
def test_cycle_outcome_reads_the_lambda_reply(error, payload, outcome):
    assert cycle_result.cycle_outcome(error, payload) == outcome


def test_run_cycles_waits_out_a_busy_simulator_and_counts_only_cycles_that_ran():
    replies = iter([(None, _reply("busy")), (None, _reply("busy")), (None, _reply("ran")),
                    ("Unhandled", {}), (None, _reply("ran"))])
    slept, seen = [], []
    result = cycle_result.run_cycles(lambda: next(replies), 3, sleep=slept.append,
                                     on_cycle=lambda n, outcome: seen.append((n, outcome)))
    assert result == {"ran": 2, "failed": 1, "waited_seconds": 20, "gave_up": False}
    assert slept == [cycle_result.BUSY_RETRY_SECONDS] * 2
    assert seen == [(1, "ran"), (2, "failed"), (3, "ran")]


def test_run_cycles_gives_up_after_its_busy_budget():
    slept = []
    result = cycle_result.run_cycles(lambda: (None, _reply("busy")), 10, sleep=slept.append,
                                     retry_seconds=10, budget_seconds=30)
    assert result == {"ran": 0, "failed": 0, "waited_seconds": 30, "gave_up": True}
    assert slept == [10, 10, 10]


def test_the_lambda_says_busy_in_its_reply(monkeypatch):
    from lambda_ import lambda_function
    fake = types.SimpleNamespace(run_single_cycle=lambda: {"success": True, "cycle_count": 0,
                                                          "simulator": "busy"})
    monkeypatch.setattr(orch_mod, "SimulationOrchestrator", lambda: fake)
    monkeypatch.setattr("erp_simulation.config.get_config",
                        lambda **_k: types.SimpleNamespace(database=types.SimpleNamespace(host="h")))
    reply = lambda_function.lambda_handler({"action": "single_cycle"}, None)
    assert reply["statusCode"] == 200
    assert cycle_result.cycle_outcome(None, reply) == cycle_result.BUSY
    assert "no simulator step ran" in json.loads(reply["body"])["message"]


def test_the_cleanup_warmup_counts_only_cycles_that_ran(monkeypatch):
    from lambda_ import lambda_cleanup_old_data as cleanup
    replies = iter([_reply("busy")] + [_reply("ran")] * 3)

    class _Client:
        def invoke(self, **_kw):
            payload = json.dumps(next(replies)).encode()
            return {"StatusCode": 200, "Payload": types.SimpleNamespace(read=lambda: payload)}

    monkeypatch.setattr(cleanup.boto3, "client", lambda *_a, **_k: _Client())
    monkeypatch.setattr(cycle_result.time, "sleep", lambda _s: None)
    assert cleanup.run_warmup_cycles(3) == {"warmup_cycles": 3, "warmup_failed": 0,
                                            "warmup_gave_up": False}


# ------------------------------------------------ real PostgreSQL ---

def _fg_product_and_site(cur):
    """A product with a FINISHED_GOOD item, and a site whose FG location is active."""
    cur.execute("""SELECT p.product_id FROM products p
                   JOIN inventory_items ii ON ii.product_id = p.product_id
                                          AND ii.item_type = 'FINISHED_GOOD'
                   ORDER BY p.sku LIMIT 1""")
    product_id = cur.fetchone()[0]
    cur.execute("SELECT location_code FROM inventory_locations WHERE is_active = TRUE")
    active = {row[0] for row in cur.fetchall()}
    site = next(s for s, code in sorted(prod_mod.SITE_FG_LOCATION.items()) if code in active)
    return product_id, site


def _completed_runs(cur, product_id, site, count, tag, oldest_minutes=10_000):
    """``count`` COMPLETED runs awaiting their receipt; run ``tag``-1 is the oldest."""
    cur.execute("""
        INSERT INTO production_runs (run_number, product_id, planned_quantity, actual_quantity,
                                     status, actual_end_time, site)
        SELECT %s || '-' || g, %s, 10, 10, 'COMPLETED',
               NOW() - ((%s - g) || ' minutes')::interval, %s
        FROM generate_series(1, %s) g
        RETURNING run_number""", (tag, product_id, oldest_minutes, site, count))
    return [row[0] for row in cur.fetchall()]


def _received(cur, tag):
    cur.execute("""SELECT pr.run_number FROM production_runs pr
                   JOIN inventory_transactions it ON it.reference_id = pr.run_id
                    AND it.reference_type = 'PRODUCTION_RUN' AND it.transaction_type = 'RECEIPT'
                   WHERE pr.run_number LIKE %s""", (f"{tag}-%",))
    return {row[0] for row in cur.fetchall()}


def _sim():
    return ProductionSimulator.__new__(ProductionSimulator)


def test_pg_completed_runs_are_received_in_bounded_batches_oldest_first(pg_erp):
    with pg_erp.cursor() as cur:
        product_id, site = _fg_product_and_site(cur)
        runs = _completed_runs(cur, product_id, site, 450, "RUN-T22B")
    pg_erp.commit()

    received = []
    for cycle in range(3):
        received.append(_sim().complete_production(pg_erp, f"c-{cycle}")["runs_completed"])
        pg_erp.commit()
        if cycle == 0:
            with pg_erp.cursor() as cur:
                assert _received(cur, "RUN-T22B") == set(runs[:200])   # the 200 oldest
    assert received == [200, 200, 50]
    with pg_erp.cursor() as cur:
        assert _received(cur, "RUN-T22B") == set(runs)


def test_pg_one_failing_receipt_is_rolled_back_alone(pg_erp, caplog):
    """The incident's shape: one row fails. Before, the step's transaction was
    left aborted and every later row failed with it; now that row alone rolls
    back, the rest commit, and the failure is logged once with a count."""
    with pg_erp.cursor() as cur:
        product_id, site = _fg_product_and_site(cur)
        runs = _completed_runs(cur, product_id, site, 5, "RUN-T22P")
        cur.execute("""
            CREATE FUNCTION t22_poison() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.reference_id = (SELECT run_id FROM production_runs WHERE run_number = 'RUN-T22P-1')
              THEN RAISE EXCEPTION 'deadlock detected (simulated)'; END IF;
              RETURN NEW;
            END $$""")
        cur.execute("""CREATE TRIGGER t22_poison BEFORE INSERT ON inventory_transactions
                       FOR EACH ROW EXECUTE FUNCTION t22_poison()""")
    pg_erp.commit()
    try:
        with caplog.at_level(logging.WARNING):
            result = _sim().complete_production(pg_erp, "c-1")
        assert pg_erp.get_transaction_status() != TRANSACTION_STATUS_INERROR
        pg_erp.commit()
        assert result["runs_completed"] == 4
        with pg_erp.cursor() as cur:
            assert _received(cur, "RUN-T22P") == set(runs[1:])
        failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert sum("1 of 5 production receipts failed" in r.getMessage() for r in failures) == 1
        assert sum("Failed to produce inventory" in r.getMessage() for r in failures) == 1
    finally:
        pg_erp.rollback()
        with pg_erp.cursor() as cur:
            cur.execute("DROP TRIGGER IF EXISTS t22_poison ON inventory_transactions")
            cur.execute("DROP FUNCTION IF EXISTS t22_poison()")
        pg_erp.commit()


def test_pg_runs_that_cannot_be_received_never_fill_the_batch(pg_erp, monkeypatch):
    """A site whose FG location is inactive was skipped row by row before;
    with a bounded batch it must not occupy the batch and starve newer runs."""
    monkeypatch.setattr(prod_mod, "MAX_ROWS_PER_CYCLE", 3)
    with pg_erp.cursor() as cur:
        product_id, site = _fg_product_and_site(cur)
        dead_site = next(s for s in sorted(prod_mod.SITE_FG_LOCATION) if s != site)
        cur.execute("UPDATE inventory_locations SET is_active = FALSE WHERE location_code = %s",
                    (prod_mod.SITE_FG_LOCATION[dead_site],))
        _completed_runs(cur, product_id, dead_site, 5, "RUN-T22D", oldest_minutes=20_000)
        live = _completed_runs(cur, product_id, site, 2, "RUN-T22L")
    pg_erp.commit()
    try:
        assert _sim().complete_production(pg_erp, "c-1")["runs_completed"] == 2
        pg_erp.commit()
        with pg_erp.cursor() as cur:
            assert _received(cur, "RUN-T22L") == set(live)
            assert _received(cur, "RUN-T22D") == set()
    finally:
        with pg_erp.cursor() as cur:
            cur.execute("UPDATE inventory_locations SET is_active = TRUE WHERE location_code = %s",
                        (prod_mod.SITE_FG_LOCATION[dead_site],))
        pg_erp.commit()


def _test_db_params(conn):
    dsn = pgdriver.parse_dsn(_DSN)
    return {"host": dsn["host"], "port": int(dsn.get("port", 5432)),
            "database": conn.get_dsn_parameters()["dbname"],
            "user": dsn["user"], "password": dsn.get("password", "")}


class _PgDb(DatabaseManager):
    """The real DatabaseManager (its dedicated lock connections included) on
    the test database; ``connection()`` hands out the fixture's connection."""

    def __init__(self, conn):
        super().__init__()
        self._conn, self._test_params = conn, _test_db_params(conn)

    def get_connection_params(self):
        return dict(self._test_params)

    @contextmanager
    def connection(self):
        yield self._conn


@contextmanager
def _other_session(conn):
    params = _test_db_params(conn)
    other = pgdriver.connect(host=params["host"], port=params["port"], dbname=params["database"],
                             user=params["user"], password=params["password"])
    other.autocommit = True
    try:
        yield other
    finally:
        other.close()


def _free(session):
    """True when nobody holds the simulator lock (probes in its own transaction)."""
    with session.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (orch_mod.SIMULATOR_LOCK_KEY,))
        free = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    return free


def _pg_orchestrator(pg_erp):
    orch, log, _fake = _orchestrator()
    orch.db = _PgDb(pg_erp)
    return orch, log


def test_pg_a_cycle_holds_the_lock_only_while_its_steps_run(pg_erp):
    orch, _log = _pg_orchestrator(pg_erp)
    states = []
    with _other_session(pg_erp) as other:
        orch._update_supplier_qualifications = lambda conn: states.append(_free(other)) or {}
        assert orch.run_single_cycle()["simulator"] == "ran"
        assert states == [False]                                   # held while the steps ran
        assert _free(other) is True                                # released after them
        assert orch.run_single_cycle()["simulator"] == "ran"       # again: never re-entered, never leaked
        assert states == [False, False] and _free(other) is True


def test_pg_a_second_cycle_skips_the_simulator_while_one_holds_it(pg_erp):
    orch, log = _pg_orchestrator(pg_erp)
    with _other_session(pg_erp) as other:
        with other.cursor() as cur:                                # a cycle elsewhere holds the simulator
            cur.execute("BEGIN")
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (orch_mod.SIMULATOR_LOCK_KEY,))
            assert cur.fetchone()[0] is True
        assert orch.run_single_cycle()["simulator"] == "busy"
        assert _simulator_calls(log) == []
        with other.cursor() as cur:
            cur.execute("ROLLBACK")
        assert orch.run_single_cycle()["simulator"] == "ran"
    assert pg_erp.get_transaction_status() == TRANSACTION_STATUS_IDLE


def test_pg_a_cycle_longer_than_the_idle_bound_keeps_the_lock(pg_erp, monkeypatch):
    """A cycle that outlasts the idle bound (a local run), with no single gap
    as long, keeps its lock: the heartbeat before each step resets the
    server's idle clock, so a second session stays excluded."""
    monkeypatch.setattr(orch_mod, "SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS", 2)
    slow = {method: (lambda *_a: time.sleep(0.8) or {})
            for method in ("generate_invoices", "process_payments", "generate_orders",
                           "check_and_create_pos", "receive_pos")}
    orch, log, _fake = _orchestrator(slow)
    orch.db = _PgDb(pg_erp)
    states = []
    with _other_session(pg_erp) as other:
        orch._update_supplier_qualifications = lambda conn: states.append(_free(other)) or {}
        started = time.monotonic()
        result = orch.run_single_cycle()
        assert time.monotonic() - started > 3.5                    # longer than the 2 s bound
        assert result == {"success": True, "cycle_count": 1, "simulator": "ran"}
        assert states == [False]                                   # still held at the last step
        assert _free(other) is True


def test_pg_one_step_longer_than_the_idle_bound_keeps_the_lock(pg_erp, monkeypatch):
    """A single step that outlasts the idle bound (a local run blocked on the
    database): the background heartbeat keeps the lock session active while
    the step runs, so a second session probing DURING that step is excluded."""
    monkeypatch.setattr(orch_mod, "SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(orch_mod, "SIMULATOR_LOCK_HEARTBEAT_SECONDS", 0.5, raising=False)
    states = []
    with _other_session(pg_erp) as other:
        def long_step(*_args):
            time.sleep(3.0)                                        # past the 2 s bound, in one step
            states.append(_free(other))
            return {}
        orch, _log, _fake = _orchestrator({"generate_invoices": long_step})
        orch.db = _PgDb(pg_erp)
        assert orch.run_single_cycle() == {"success": True, "cycle_count": 1, "simulator": "ran"}
        assert states == [False]                                   # held during the long step
        assert _free(other) is True                                # released after the cycle


class _ThreadSpy:
    """A real connection that records which thread tears it down."""

    def __init__(self, conn):
        self.conn, self.teardown_threads = conn, []

    def cursor(self):
        return self.conn.cursor()

    def rollback(self):
        self.teardown_threads.append(("rollback", threading.current_thread().name))
        self.conn.rollback()

    def close(self):
        self.teardown_threads.append(("close", threading.current_thread().name))
        self.conn.close()

    def cancel(self):
        self.conn.cancel()


def test_pg_a_stalled_heartbeat_is_cancelled_and_never_raced_by_the_release(pg_erp, monkeypatch):
    """A background beat stuck on a stalled statement (a network or Aurora
    stall): stop() cancels it, the keeper thread itself releases the lock and
    closes the connection, nothing else touches it, and no thread survives."""
    params = _test_db_params(pg_erp)
    raw = pgdriver.connect(host=params["host"], port=params["port"], dbname=params["database"],
                           user=params["user"], password=params["password"])
    with raw.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (orch_mod.SIMULATOR_LOCK_KEY,))
        assert cur.fetchone()[0] is True
    spy = _ThreadSpy(raw)
    monkeypatch.setattr(orch_mod._LockKeeper, "STOP_WAIT_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(orch_mod._LockKeeper, "BEAT_SQL", "SELECT pg_sleep(30)", raising=False)
    keeper = orch_mod._LockKeeper(spy, 0.1)
    time.sleep(0.5)                                          # the background beat is now stalled
    started = time.monotonic()
    assert keeper.stop() is True
    assert time.monotonic() - started < 3
    assert spy.teardown_threads == [("close", "erp-simulator-lock-heartbeat")]   # cancelled: no rollback
    assert raw.closed and not _heartbeat_threads()
    with _other_session(pg_erp) as other:
        assert _free(other) is True                          # closing the session released the lock


def test_pg_an_abandoned_lock_is_dropped_by_the_server(pg_erp, monkeypatch):
    """An invocation takes the lock and never releases it
    (killed or frozen with its socket open). The lock session's server-side
    idle bound ends it, so a later cycle runs; nothing here has to act."""
    monkeypatch.setattr(orch_mod, "SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS", 2)
    abandoned, _ = _pg_orchestrator(pg_erp)
    lock = abandoned._claim_simulator()
    assert lock is not None                                        # taken, never released
    orch, _log = _pg_orchestrator(pg_erp)
    assert orch.run_single_cycle()["simulator"] == "busy"
    time.sleep(3.5)
    assert orch.run_single_cycle()["simulator"] == "ran"
    lock.close()
