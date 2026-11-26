# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Simulation cycle isolation and bounding (ADR-0090).

Unit tests drive the REAL ``_run_cycle`` / ``_run_simulation_cycle`` with fake
simulators and a fake connection: the clock steps run first, each simulator
step is its own transaction, the time budget skips only simulator steps, the
bulk steps are bounded, and one summary line closes every cycle. The ``pg_*``
tests (ERP_SIM_TEST_PG_DSN) prove the same on a real PostgreSQL with a real
unique violation.
"""

import logging
import os
import types
import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from erp_simulation.pgdriver import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INERROR

from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import utils
from erp_simulation.finance_simulator import FinanceSimulator
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.production_simulator import ProductionSimulator
from erp_simulation.shipping_simulator import ShippingSimulator

_SIMULATOR_OWNERS = sorted({s.owner for s in orch_mod._SIMULATION_STEPS if s.owner != 'self'})


class FakeConn:
    """The pooled step connection. Every ``fetchone`` returns ``(ready,)``.

    The simulator lock lives on its own dedicated connection
    (``FakeDb.dedicated_connection`` -> ``FakeLockConn``): ``simulator_free``
    answers its probe; ``lock_connect_error`` / ``lock_probe_error`` /
    ``lock_rollback_error`` make the connect, the probe or the release fail;
    ``lock_closed`` counts its closes."""

    def __init__(self, log):
        self.log = log
        self.status = TRANSACTION_STATUS_IDLE
        self.autocommit = False
        self.ready = 0
        self.cursor_obj = MagicMock(name="cursor")
        self.cursor_obj.__enter__.return_value = self.cursor_obj
        self.cursor_obj.fetchone.side_effect = lambda: (self.ready,)
        self.simulator_free = True
        self.lock_connect_error = self.lock_probe_error = self.lock_rollback_error = None
        self.lock_closed = 0
        self.lock_cursor = MagicMock(name="lock_cursor")
        self.lock_cursor.__enter__.return_value = self.lock_cursor
        self.lock_cursor.execute.side_effect = self._lock_execute
        self.lock_cursor.fetchone.side_effect = lambda: (self.simulator_free,)

    def _lock_execute(self, *_args):
        if self.lock_probe_error is not None:
            raise self.lock_probe_error

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.log.append("commit")
        self.status = TRANSACTION_STATUS_IDLE

    def rollback(self):
        self.log.append("rollback")
        self.status = TRANSACTION_STATUS_IDLE

    def get_transaction_status(self):
        return self.status


class FakeLockConn:
    """The simulator lock's dedicated connection (state lives on FakeConn)."""

    def __init__(self, owner):
        self.owner = owner

    def cursor(self):
        return self.owner.lock_cursor

    def rollback(self):
        self.owner.log.append("lock-rollback")
        if self.owner.lock_rollback_error is not None:
            raise self.owner.lock_rollback_error

    def close(self):
        self.owner.lock_closed += 1
        self.owner.log.append("lock-close")

    def cancel(self):
        self.owner.log.append("lock-cancel")


class FakeDb:
    def __init__(self, conn):
        self.conn = conn
        self.lock_connections = []

    @contextmanager
    def connection(self):
        yield self.conn

    def dedicated_connection(self, **kwargs):
        if self.conn.lock_connect_error is not None:
            raise self.conn.lock_connect_error
        self.lock_connections.append(kwargs)
        return FakeLockConn(self.conn)


class Recorder:
    """A fake simulator: every method logs ``owner.method`` and returns ``{}``,
    unless ``behaviour[method]`` says otherwise."""

    def __init__(self, owner, log, behaviour):
        self._owner, self._log, self._behaviour = owner, log, behaviour

    def __getattr__(self, method):
        def call(*args):
            self._log.append(f"{self._owner}.{method}")
            action = self._behaviour.get(method)
            return action(*args) if callable(action) else (action or {})
        return call


def _orchestrator(behaviour=None, ready=0):
    log = []
    conn = FakeConn(log)
    conn.ready = ready
    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db = FakeDb(conn)
    orch.cycle_count = 0
    orch.config = types.SimpleNamespace(cloudwatch=types.SimpleNamespace(enabled=False))
    orch._cloudwatch_client = None
    behaviour = behaviour or {}
    for owner in _SIMULATOR_OWNERS:
        setattr(orch, owner, Recorder(owner, log, behaviour))
    orch._update_supplier_qualifications = lambda conn: log.append("self.suppliers") or {}
    orch._load_story_clock = lambda: log.append("clock")
    orch._run_story_step = lambda: log.append("story") or 0
    orch._run_story_anchor_mirror = lambda: log.append("anchor_mirror") or "off"
    orch._record_simulation_event = MagicMock(name="_record_simulation_event")
    return orch, log, conn


def _simulator_calls(log):
    return [entry for entry in log if "." in entry and not entry.startswith("mes_simulator.run_condition")]


# ------------------------------------------------------------- ordering ---

def test_the_clock_steps_run_first_then_every_simulator_step_in_order():
    orch, log, _ = _orchestrator()
    orch._run_cycle()
    assert log[:4] == ["clock", "story", "mes_simulator.run_condition_monitor", "anchor_mirror"]
    expected = [f"{s.owner}.{s.method}" if s.owner != 'self' else "self.suppliers"
                for s in orch_mod._SIMULATION_STEPS]
    assert _simulator_calls(log) == expected
    assert log.index("anchor_mirror") < log.index("order_simulator.generate_orders")


def test_the_monitor_step_never_raises():
    orch, log, _ = _orchestrator({"run_condition_monitor": MagicMock(side_effect=RuntimeError("seed db"))})
    orch._run_cycle()
    assert "order_simulator.generate_orders" in log


def test_the_downtime_step_reuses_the_cycle_monitor_result(monkeypatch):
    sim = MESSimulator.__new__(MESSimulator)
    calls = []
    monkeypatch.setattr(sim, "monitor_asset_condition", lambda cycle_id, now=None: calls.append(cycle_id) or 1)
    assert sim.run_condition_monitor("c-1") == 1
    assert sim._condition_monitor_for_cycle("c-1") == 1
    assert calls == ["c-1"]
    assert sim._condition_monitor_for_cycle("c-2") == 1   # a direct caller of another cycle runs it
    assert calls == ["c-1", "c-2"]


# ------------------------------------------------------------ isolation ---

def test_each_step_commits_alone_and_a_failure_rolls_back_only_itself(caplog):
    orch, log, _ = _orchestrator({
        "generate_orders": {"orders_created": 3},
        "generate_invoices": MagicMock(side_effect=RuntimeError("duplicate key invoices_invoice_number_key")),
        "create_shipments": {"shipments_created": 7},
    })
    with caplog.at_level(logging.INFO):
        orch._run_cycle()
    ship = log.index("shipping_simulator.create_shipments")
    inv = log.index("finance_simulator.generate_invoices")
    assert log[ship + 1] == "commit"            # shipments committed before invoicing ran
    assert log[inv + 1] == "rollback"           # only the invoice step rolled back
    assert log[log.index("finance_simulator.process_payments") + 1] == "commit"
    m = orch._cycle_metrics
    assert m["errors"] == 1 and m["shipments_created"] == 7 and m["orders_created"] == 3
    assert m["invoices_generated"] == 0
    assert any("Cycle step invoices failed and was rolled back alone" in r.getMessage()
               for r in caplog.records)


def test_a_step_that_swallowed_an_error_is_detected_and_rolled_back():
    """The per-row try/except in a simulator can leave the transaction aborted
    while returning normally: committing would silently roll it back."""
    def swallowed(conn, cycle_id):
        conn.status = TRANSACTION_STATUS_INERROR
        return {"invoices_generated": 5}

    orch, log, _ = _orchestrator({"generate_invoices": swallowed,
                                  "create_shipments": {"shipments_created": 2}})
    orch._run_cycle()
    assert log[log.index("finance_simulator.generate_invoices") + 1] == "rollback"
    assert orch._cycle_metrics["invoices_generated"] == 0   # nothing was committed
    assert orch._cycle_metrics["shipments_created"] == 2
    assert orch._cycle_metrics["errors"] == 1


# --------------------------------------------------------- time budget ---

def test_past_the_budget_the_remaining_simulator_steps_are_skipped(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(orch_mod, "_monotonic", lambda: clock[0])

    def slow(conn, cycle_id):
        clock[0] += orch_mod.CYCLE_TIME_BUDGET_SECONDS + 1   # a slow simulator
        return {}

    orch, log, _ = _orchestrator({"simulate_production_events": slow})
    with caplog.at_level(logging.INFO):
        orch._run_cycle()
    assert log[:4] == ["clock", "story", "mes_simulator.run_condition_monitor", "anchor_mirror"]
    calls = _simulator_calls(log)
    assert calls[-1] == "mes_simulator.simulate_production_events"
    names = [s.name for s in orch_mod._SIMULATION_STEPS]
    assert orch._cycle_skipped == names[names.index("mes_events") + 1:]
    summary = [r.getMessage() for r in caplog.records if " summary: " in r.getMessage()]
    assert len(summary) == 1 and "skipped=mes_quality," in summary[0]
    orch._record_simulation_event.assert_called_once()     # the cycle is still recorded


def test_the_budget_is_150_seconds_under_the_300_second_lambda():
    assert orch_mod.CYCLE_TIME_BUDGET_SECONDS == 150.0


# ------------------------------------------------------------ summary ---

def test_one_info_summary_line_with_step_timings(caplog):
    orch, _, _ = _orchestrator({"create_shipments": {"shipments_created": 4}})
    with caplog.at_level(logging.INFO, logger=orch_mod.logger.name):
        orch._run_cycle()
    lines = [r for r in caplog.records if " summary: " in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    text = lines[0].getMessage()
    assert "\n" not in text and "shipments_created=4" in text and "errors=0" in text
    for step in ("clock=", "story=", "monitor=", "anchor_mirror=", "shipments=", "invoices="):
        assert step in text


def test_the_summary_is_logged_even_when_the_connection_fails(caplog):
    orch, _, _ = _orchestrator()

    @contextmanager
    def broken():
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover

    orch._transaction_context = broken
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        orch._run_cycle()
    assert sum(" summary: " in r.getMessage() for r in caplog.records) == 1


def test_run_single_cycle_reports_success_through_run_cycle():
    orch, log, _ = _orchestrator()
    assert orch.run_single_cycle() == {"success": True, "cycle_count": 1, "simulator": "ran"}
    assert log[0] == "clock"


# ------------------------------------------------------ backlog / bounds ---

def test_a_large_backlog_is_logged_with_its_drain_plan(caplog):
    orch, _, _ = _orchestrator(ready=12_596)
    with caplog.at_level(logging.WARNING):
        orch._run_cycle()
    warnings = [r.getMessage() for r in caplog.records if "Shipping backlog" in r.getMessage()]
    assert warnings == ["Shipping backlog: 12596 orders ready to ship (> 2000); "
                        "draining up to 2 x 200 per cycle while above 2000 (~32 cycles)"]
    assert orch._cycle_metrics["ready_to_ship"] == 12_596


def test_a_small_backlog_is_not_a_warning(caplog):
    orch, _, _ = _orchestrator(ready=15)
    with caplog.at_level(logging.WARNING):
        orch._run_cycle()
    assert not any("Shipping backlog" in r.getMessage() for r in caplog.records)


class _BatchCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)

    def close(self):
        pass


def _first_select(cursor):
    return next(c for c in cursor.calls if c[0].startswith("SELECT"))


@pytest.mark.parametrize("sim_cls, method, table", [
    (ShippingSimulator, "create_shipments", "customer_orders"),
    (FinanceSimulator, "generate_invoices", "shipments"),
    (FinanceSimulator, "process_payments", "invoices"),
    (ProductionSimulator, "complete_production", "production_runs"),
])
def test_bulk_steps_read_a_bounded_oldest_first_batch(sim_cls, method, table):
    cursor = _BatchCursor()
    conn = types.SimpleNamespace(cursor=lambda: cursor)
    assert getattr(sim_cls.__new__(sim_cls), method)(conn, "c-1")
    sql, params = _first_select(cursor)
    assert f"FROM {table}" in sql and "ORDER BY" in sql and "LIMIT %s" in sql and "SKIP LOCKED" in sql
    assert params == (utils.MAX_ROWS_PER_CYCLE,) == (200,)


# ------------------------------------------------ real PostgreSQL ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg_erp():
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    utils._reset_sequence_cache()
    with _fresh_erp_db() as conn:
        yield conn
    utils._reset_sequence_cache()


def _shipped_orders(cur, count):
    """``count`` DELIVERED shipments of one-line orders, not yet invoiced."""
    cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    customer_id = cur.fetchone()[0]
    cur.execute("SELECT product_id FROM products ORDER BY product_id LIMIT 1")
    product_id = cur.fetchone()[0]
    ids = []
    for n in range(count):
        tag = uuid.uuid4().hex[:8]
        cur.execute("""INSERT INTO customer_orders (order_number, customer_id, order_date, status)
                       VALUES (%s, %s, NOW(), 'SHIPPED') RETURNING order_id""", (f"ORD-T85-{tag}", customer_id))
        order_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price)
                       VALUES (%s, 1, %s, 2, 10.00)""", (order_id, product_id))
        cur.execute("""INSERT INTO shipments (shipment_number, order_id, customer_id, shipment_date, status)
                       VALUES (%s, %s, %s, NOW(), 'DELIVERED') RETURNING shipment_id""",
                    (f"SHIP-T85-{tag}", order_id, customer_id))
        ids.append(cur.fetchone()[0])
    return ids


def _legacy_invoice(cur, number):
    """An invoice numbered by the old random generator, on today's date."""
    cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    cur.execute("""INSERT INTO invoices (invoice_number, customer_id, invoice_date, due_date, status)
                   VALUES (%s, %s, NOW(), NOW() + INTERVAL '30 days', 'PAID')""", (number, cur.fetchone()[0]))


def test_pg_an_invoice_number_conflict_regenerates_and_invoices_every_shipment(pg_erp):
    today = datetime.now().strftime("%Y%m%d")
    with pg_erp.cursor() as cur:
        shipments = _shipped_orders(cur, 3)
        _legacy_invoice(cur, f"INV-{today}-00002")
        utils.ensure_number_sequences(cur)
    pg_erp.commit()

    result = FinanceSimulator.__new__(FinanceSimulator).generate_invoices(pg_erp, "c-1")
    pg_erp.commit()

    assert result["invoices_generated"] == 3
    with pg_erp.cursor() as cur:
        cur.execute("SELECT shipment_id, invoice_number, total_amount FROM invoices "
                    "WHERE shipment_id = ANY(%s::uuid[]) ORDER BY shipment_id", ([str(i) for i in shipments],))
        rows = cur.fetchall()
    assert [r[1] for r in rows] == [f"INV-{today}-00001", f"INV-{today}-00003", f"INV-{today}-00004"]
    assert all(float(r[2]) == 21.6 for r in rows)          # line items + tax survived the retry


def test_pg_two_conflicts_skip_one_shipment_and_invoice_the_rest(pg_erp, caplog):
    today = datetime.now().strftime("%Y%m%d")
    with pg_erp.cursor() as cur:
        shipments = _shipped_orders(cur, 3)
        for n in ("00002", "00003"):
            _legacy_invoice(cur, f"INV-{today}-{n}")
        utils.ensure_number_sequences(cur)
    pg_erp.commit()

    with caplog.at_level(logging.WARNING):
        result = FinanceSimulator.__new__(FinanceSimulator).generate_invoices(pg_erp, "c-1")
    pg_erp.commit()

    assert result["invoices_generated"] == 2
    assert any("insert skipped" in r.getMessage() for r in caplog.records)
    with pg_erp.cursor() as cur:
        cur.execute("SELECT shipment_id FROM invoices WHERE shipment_id = ANY(%s::uuid[]) ORDER BY 1",
                    ([str(i) for i in shipments],))
        invoiced = [str(r[0]) for r in cur.fetchall()]
    in_order = sorted(str(i) for i in shipments)             # the batch reads ORDER BY shipment_id
    assert invoiced == [in_order[0], in_order[2]]            # the middle one hit 00002 then 00003
    # the skipped shipment is picked up next cycle
    assert FinanceSimulator.__new__(FinanceSimulator).generate_invoices(pg_erp, "c-2")["invoices_generated"] == 1


def test_pg_a_failed_step_never_rolls_back_a_sibling_step(pg_erp):
    """A real unique violation swallowed inside one step (the incident's shape)
    leaves only THAT step rolled back; the steps before and after commit."""
    today = datetime.now().strftime("%Y%m%d")
    with pg_erp.cursor() as cur:
        cur.execute("CREATE TABLE t85_marks (k TEXT)")
        _legacy_invoice(cur, f"INV-{today}-77777")
    pg_erp.commit()

    def step_insert(note):
        def run(conn, cycle_id):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO t85_marks (k) VALUES (%s)",
                            (f"t85_{note}",))
            return {}
        return run

    def step_swallows_duplicate(conn, cycle_id):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO t85_marks (k) VALUES ('t85_b')")
            try:
                _legacy_invoice(cur, f"INV-{today}-77777")     # 23505, swallowed like the old loops
            except Exception:
                pass
        return {}

    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch._cycle_timings = {}
    metrics = {"errors": 0}
    for name, fn in (("a", step_insert("a")), ("b", step_swallows_duplicate), ("c", step_insert("c"))):
        orch.fake = types.SimpleNamespace(run=fn)
        orch._run_step(pg_erp, orch_mod.CycleStep(name, "fake", "run"), "c-1", metrics)

    with pg_erp.cursor() as cur:
        cur.execute("SELECT k FROM t85_marks ORDER BY 1")
        assert [r[0] for r in cur.fetchall()] == ["t85_a", "t85_c"]
    assert metrics["errors"] == 1


def test_pg_the_shipping_backlog_drains_in_bounded_batches(pg_erp):
    """Backlog recovery is the bounded batch working: 450 ready orders ship as
    200 + 200 + 50 over three cycles, oldest first."""
    with pg_erp.cursor() as cur:
        cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
        customer_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO customer_orders (order_number, customer_id, order_date, status)
                       SELECT 'ORD-T85-' || g, %s, NOW() - (g || ' seconds')::interval, 'READY_TO_SHIP'
                       FROM generate_series(1, 450) g""", (customer_id,))
        utils.ensure_number_sequences(cur)
    pg_erp.commit()
    sim = ShippingSimulator.__new__(ShippingSimulator)
    sim.carriers = ["AnyCompany Freight", "AnyCompany Logistics", "AnyCompany Parcel"]
    shipped = []
    for cycle in range(3):
        shipped.append(sim.create_shipments(pg_erp, f"c-{cycle}")["shipments_created"])
        pg_erp.commit()
    assert shipped == [200, 200, 50]
    with pg_erp.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM customer_orders WHERE status = 'READY_TO_SHIP'")
        assert cur.fetchone()[0] == 0


def test_pg_a_po_number_conflict_regenerates_instead_of_aborting(pg_erp):
    """The frozen account's class: purchase_orders_po_number_key."""
    from erp_simulation.procurement_simulator import ProcurementSimulator
    today = datetime.now().strftime("%Y%m%d")
    with pg_erp.cursor() as cur:
        cur.execute("SELECT supplier_id, supplier_code FROM suppliers ORDER BY supplier_id LIMIT 1")
        supplier_id, supplier_code = cur.fetchone()
        cur.execute("SELECT item_id, item_code FROM inventory_items ORDER BY item_id LIMIT 1")
        item_id, item_code = cur.fetchone()
        cur.execute("""INSERT INTO purchase_orders (po_number, supplier_id, po_date, expected_delivery_date, status)
                       VALUES (%s, %s, NOW(), NOW(), 'RECEIVED')""", (f"PO-{today}-00001", supplier_id))
        utils.ensure_number_sequences(cur)
    pg_erp.commit()
    item = (item_id, item_code, None, None, None, 4.5, None, None, None, 10)
    with pg_erp.cursor() as cur:
        ok = ProcurementSimulator._create_supplier_po(cur, (supplier_id, supplier_code, 3), [item, item])
    pg_erp.commit()
    assert ok is True
    with pg_erp.cursor() as cur:
        cur.execute("""SELECT po.po_number, po.total_amount, COUNT(li.*) FROM purchase_orders po
                       JOIN po_line_items li ON li.po_id = po.po_id
                       WHERE po.status = 'SENT' GROUP BY po.po_number, po.total_amount""")
        assert cur.fetchall() == [(f"PO-{today}-00002", pytest.approx(90.0), 2)]
