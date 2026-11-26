# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shipping-backlog drain: the hot-path indexes, the per-batch traceable-run
lookup, the required-first step order, and the double shipping batch.

Production symptom: on an account with a 14k 'ready to ship' backlog one
bounded shipping batch (200 orders) took 238 s. EXPLAIN ANALYZE showed the
per-line-item ``_traceable_batch_id`` lookup reading EVERY run of the product
(bitmap heap scan of ``production_runs`` + sort, ~240 ms per line on 1.5M
runs): the only index was ``product_id`` and the ORDER BY needed the whole
set. The composite index turns it into an index walk that stops at the first
traceable run, and the batch memoises it per product. PostgreSQL tests
(ERP_SIM_TEST_PG_DSN) pin the plan, the index presence and the time bound.
"""
from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path

import pytest

from erp_simulation import hot_path_indexes as hpi
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import utils
from erp_simulation.shipping_simulator import ShippingSimulator
from erp_simulation.tests._pg_sql import stmt
from erp_simulation.tests.test_cycle_isolation import _orchestrator, _simulator_calls

_REPO = Path(__file__).resolve().parents[2]
_HANDLER = _REPO / "lambda-db-init" / "handler.py"
_SCHEMA = _REPO / "erp_simulation" / "schema"

REQUIRED = ("shipments", "invoices", "payments")


# ------------------------------------------------------------ step order ---

def test_the_bounded_bulk_steps_run_first_and_are_the_only_required_steps():
    names = [s.name for s in orch_mod._SIMULATION_STEPS]
    assert tuple(names[:3]) == REQUIRED
    assert {s.name for s in orch_mod._SIMULATION_STEPS if s.required} == set(REQUIRED)


def test_after_the_clock_steps_shipments_invoices_payments_run_before_the_heavy_simulators():
    orch, log, _ = _orchestrator()
    orch._run_cycle()
    assert log[:4] == ["clock", "story", "mes_simulator.run_condition_monitor", "anchor_mirror"]
    assert _simulator_calls(log)[:4] == ["shipping_simulator.create_shipments",
                                         "finance_simulator.generate_invoices",
                                         "finance_simulator.process_payments",
                                         "order_simulator.generate_orders"]


def test_past_the_budget_only_the_optional_steps_are_skipped(monkeypatch):
    """Even when the clock steps alone used the whole budget, the three
    required steps each still get their one bounded batch."""
    clock = [1000.0]
    monkeypatch.setattr(orch_mod, "_monotonic", lambda: clock[0])
    orch, log, _ = _orchestrator()

    def slow_mirror():
        log.append("anchor_mirror")
        clock[0] += orch_mod.CYCLE_TIME_BUDGET_SECONDS + 1
        return {}

    orch._run_story_anchor_mirror = slow_mirror
    orch._run_cycle()
    assert _simulator_calls(log) == ["shipping_simulator.create_shipments",
                                     "finance_simulator.generate_invoices",
                                     "finance_simulator.process_payments"]
    optional = [s.name for s in orch_mod._SIMULATION_STEPS if not s.required]
    assert orch._cycle_skipped == optional


# ---------------------------------------------------------- double batch ---

def _full_batch(*_args):
    return {"shipments_created": utils.MAX_ROWS_PER_CYCLE}


def test_above_the_backlog_threshold_shipping_takes_a_second_bounded_batch():
    orch, log, _ = _orchestrator({"create_shipments": _full_batch}, ready=14_107)
    orch._run_cycle()
    assert log.count("shipping_simulator.create_shipments") == 2
    ship = [i for i, e in enumerate(log) if e == "shipping_simulator.create_shipments"]
    assert all(log[i + 1] == "commit" for i in ship)          # each batch its own transaction
    assert orch._cycle_metrics["shipments_created"] == 2 * utils.MAX_ROWS_PER_CYCLE
    assert "shipments" in orch._cycle_timings and "shipments_2" in orch._cycle_timings


def test_at_or_below_the_threshold_shipping_takes_one_batch():
    orch, log, _ = _orchestrator({"create_shipments": _full_batch},
                                 ready=orch_mod.BACKLOG_WARN_THRESHOLD)
    orch._run_cycle()
    assert log.count("shipping_simulator.create_shipments") == 1
    assert orch._cycle_metrics["shipments_created"] == utils.MAX_ROWS_PER_CYCLE


def test_never_more_than_two_shipping_batches_per_cycle():
    assert orch_mod.BACKLOG_SHIPMENT_BATCHES == 2
    step = next(s for s in orch_mod._SIMULATION_STEPS if s.name == "shipments")
    assert step.backlog_batches == orch_mod.BACKLOG_SHIPMENT_BATCHES
    assert all(s.backlog_batches == 1 for s in orch_mod._SIMULATION_STEPS if s.name != "shipments")


def test_the_second_batch_runs_only_if_another_batch_fits_in_the_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(orch_mod, "_monotonic", lambda: clock[0])

    def slow_batch(*_args):
        clock[0] += orch_mod.CYCLE_TIME_BUDGET_SECONDS * 0.6   # a second one would overrun
        return _full_batch()

    orch, log, _ = _orchestrator({"create_shipments": slow_batch}, ready=14_107)
    orch._run_cycle()
    assert log.count("shipping_simulator.create_shipments") == 1
    assert "generate_invoices" in " ".join(log)                  # required steps still ran


@pytest.mark.parametrize("first", [{"shipments_created": 0}, RuntimeError("boom")])
def test_an_empty_or_failed_first_batch_is_not_repeated(first):
    behaviour = first if isinstance(first, dict) else _raise(first)
    orch, log, _ = _orchestrator({"create_shipments": behaviour}, ready=14_107)
    orch._run_cycle()
    assert log.count("shipping_simulator.create_shipments") == 1


def _raise(error):
    def run(*_args):
        raise error
    return run


def test_a_14k_backlog_drains_in_under_40_cycles():
    per_cycle = orch_mod.BACKLOG_SHIPMENT_BATCHES * utils.MAX_ROWS_PER_CYCLE
    assert -(-14_107 // per_cycle) < 40


def test_the_backlog_warning_states_the_double_batch_drain_plan(caplog):
    orch, _, _ = _orchestrator(ready=14_107)
    with caplog.at_level(logging.WARNING):
        orch._run_cycle()
    warnings = [r.getMessage() for r in caplog.records if "Shipping backlog" in r.getMessage()]
    assert warnings == ["Shipping backlog: 14107 orders ready to ship (> 2000); "
                        "draining up to 2 x 200 per cycle while above 2000 (~36 cycles)"]


# ------------------------------------------------------- hot-path indexes ---
# The concurrent build on an existing account (budget, INVALID repair, a failed
# build skipped) is pinned in test_hot_path_index_build.py.

def test_the_orchestrator_ensures_the_indexes_before_the_first_step(monkeypatch):
    orch, log, _ = _orchestrator()
    monkeypatch.setattr(orch_mod, "ensure_hot_path_indexes", lambda conn: log.append("indexes") or [])
    orch._run_cycle()
    assert log.index("indexes") < log.index("shipping_simulator.create_shipments")
    assert "indexes" in orch._cycle_timings


def test_an_index_failure_never_stops_the_cycle(monkeypatch, caplog):
    orch, log, _ = _orchestrator()

    def fail(conn):
        raise RuntimeError("catalog read failed")

    monkeypatch.setattr(orch_mod, "ensure_hot_path_indexes", fail)
    with caplog.at_level(logging.WARNING):
        orch._run_cycle()
    assert "shipping_simulator.create_shipments" in log
    assert any("Hot-path indexes not ensured" in r.getMessage() for r in caplog.records)


def _db_init_hot_path_sql() -> str:
    tree = ast.parse(_HANDLER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HOT_PATH_INDEXES_SQL" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"HOT_PATH_INDEXES_SQL literal not found in {_HANDLER}")


def _ddl_lines(text: str) -> set:
    return {" ".join(line.split()).rstrip(";") for line in text.splitlines()
            if line.strip().upper().startswith("CREATE INDEX")}


def test_db_init_and_the_schema_files_carry_the_same_index_ddl():
    expected = {hpi.index_ddl(*spec) for spec in hpi.HOT_PATH_INDEXES}
    assert _ddl_lines(_db_init_hot_path_sql()) == expected
    schema = set().union(*(_ddl_lines(p.read_text(encoding="utf-8")) for p in _SCHEMA.glob("*.sql")))
    assert expected <= schema
    tree = ast.parse(_HANDLER.read_text(encoding="utf-8"))
    create_schema = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "create_schema")
    assert any(isinstance(n, ast.Name) and n.id == "HOT_PATH_INDEXES_SQL"
               for n in ast.walk(create_schema))                    # create_schema applies it


# -------------------------------------------- per-batch traceable lookup ---

class _ShipCursor:
    """Two orders, four lines over two products; counts traceable lookups."""
    LINES = {"o-1": [("l-1", "p-a", 1, "A"), ("l-2", "p-b", 1, "B")],
             "o-2": [("l-3", "p-a", 1, "A"), ("l-4", "p-b", 1, "B")]}

    def __init__(self):
        self.lookups, self._one, self._all = [], None, []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self._one, self._all = (1,), []
        if "READY_TO_SHIP" in sql:
            self._all = [("o-1", "ORD-1", "c"), ("o-2", "ORD-2", "c")]
        elif sql.startswith("SELECT oli.line_item_id"):
            self._all = self.LINES[params[0]]
        elif sql.startswith("SELECT pr.run_number"):
            self.lookups.append(params[0])
            self._one = ("RUN-" + params[0],)
        elif sql.startswith("SELECT item_id FROM inventory_items"):
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def close(self):
        pass


def _sim():
    sim = ShippingSimulator.__new__(ShippingSimulator)
    sim.carriers = ["AnyCompany Freight", "AnyCompany Logistics", "AnyCompany Parcel"]
    return sim


def test_the_traceable_run_is_looked_up_once_per_product_per_batch(monkeypatch):
    monkeypatch.setattr(utils, "next_business_number", lambda cursor, prefix, now=None: f"{prefix}-1")
    sim = _sim()
    for batch in range(2):
        cursor = _ShipCursor()
        conn = type("C", (), {"cursor": lambda self: cursor})()
        assert sim.create_shipments(conn, f"c-{batch}")["shipments_created"] == 2
        assert sorted(cursor.lookups) == ["p-a", "p-b"]            # not once per line (4)


# ------------------------------------------------------ real PostgreSQL ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_BACKLOG = 14_000
_RUNS = 300_000


@pytest.fixture(scope="module")
def pg_backlog():
    """db-init schema + seed, then a synthetic backlog: 14,000 READY orders
    (3 lines each), 300,000 COMPLETED runs with lot usage."""
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    utils._reset_sequence_cache()
    with _fresh_erp_db() as conn:
        _seed_backlog(conn)
        yield conn
    utils._reset_sequence_cache()


def _seed_backlog(conn):
    params = {"backlog": _BACKLOG, "runs": _RUNS}
    with conn.cursor() as cur:
        # One statement per execute: a statement with bound parameters is one statement.
        cur.execute("""
            CREATE TEMP TABLE p AS SELECT product_id, row_number() OVER (ORDER BY product_id) - 1 AS rn
            FROM products
        """)
        cur.execute("""
            INSERT INTO customer_orders (order_number, customer_id, order_date, status)
            SELECT 'ORD-PERF-' || g, (SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1),
                   NOW() - (g || ' minutes')::interval, 'READY_TO_SHIP'
            FROM generate_series(1, %(backlog)s) g
        """, params)
        cur.execute("""
            INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price)
            SELECT co.order_id, ln, p.product_id, 2, 10
            FROM customer_orders co CROSS JOIN generate_series(1, 3) ln
            JOIN p ON p.rn = (abs(hashtext(co.order_number)) + ln) %% (SELECT COUNT(*) FROM p)
            WHERE co.order_number LIKE 'ORD-PERF-%%'
        """, params)
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, actual_quantity, status,
                                         actual_start_time, actual_end_time, updated_at)
            SELECT 'RUN-PERF-' || g, p.product_id, 100, 100, 'COMPLETED',
                   NOW() - (g || ' minutes')::interval - INTERVAL '1 hour',
                   NOW() - (g || ' minutes')::interval, NOW()
            FROM generate_series(1, %(runs)s) g JOIN p ON p.rn = g %% (SELECT COUNT(*) FROM p)
        """, params)
        cur.execute("""
            INSERT INTO lot_production_usage (lot_id, production_run_id, quantity_used, unit_of_measure)
            SELECT (SELECT lot_id FROM material_lots ORDER BY lot_id LIMIT 1), pr.run_id, 1, 'EA'
            FROM production_runs pr WHERE pr.run_number LIKE 'RUN-PERF-%%'
        """, params)
        utils.ensure_number_sequences(cur)
    conn.commit()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ANALYZE")
    conn.autocommit = False


def _index_names(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        return {row[0] for row in cur.fetchall()}


def _plan(conn, sql, params):
    from erp_simulation.pgdriver import sql as psql

    try:
        with conn.cursor() as cur:
            cur.execute(stmt("EXPLAIN {query}", query=psql.SQL(sql)), params)
            return "\n".join(row[0] for row in cur.fetchall())
    finally:
        conn.rollback()   # read-only; never leave the shared module connection aborted


def test_pg_the_ready_batch_read_uses_the_status_order_index(pg_backlog):
    sql = ("SELECT co.order_id, co.order_number, co.customer_id FROM customer_orders co "
           "WHERE co.status = 'READY_TO_SHIP' ORDER BY co.order_id LIMIT %s FOR UPDATE SKIP LOCKED")
    plan = _plan(pg_backlog, sql, (utils.MAX_ROWS_PER_CYCLE,))
    assert "idx_customer_orders_status_order" in plan and "Sort" not in plan, plan


def test_pg_db_init_creates_the_hot_path_indexes(pg_backlog):
    assert {name for name, _t, _c in hpi.HOT_PATH_INDEXES} <= _index_names(pg_backlog)


def test_pg_the_orchestrator_ensure_restores_a_missing_index(pg_backlog):
    name = hpi.HOT_PATH_INDEXES[0][0]
    with pg_backlog.cursor() as cur:
        cur.execute(stmt("DROP INDEX {index}", index=name))
    pg_backlog.commit()
    assert hpi.ensure_hot_path_indexes(pg_backlog) == [name]   # concurrently, on 300,000 runs
    assert hpi.ensure_hot_path_indexes(pg_backlog) == []
    assert name in _index_names(pg_backlog)
    assert pg_backlog.autocommit is False


def test_pg_the_traceable_lookup_walks_the_index_instead_of_every_run(pg_backlog):
    import inspect
    import re
    source = inspect.getsource(ShippingSimulator._traceable_batch_id)
    sql = re.search(r'"""\s*(SELECT .*?)"""', source, re.S).group(1)
    with pg_backlog.cursor() as cur:
        cur.execute("SELECT product_id FROM products ORDER BY product_id LIMIT 1")
        product_id = cur.fetchone()[0]
    plan = _plan(pg_backlog, sql, (product_id, utils.HISTORY_RUN_NOTE))
    assert "idx_production_runs_traceable" in plan, plan
    assert "Sort" not in plan and "Seq Scan on production_runs" not in plan, plan


def test_the_traceable_lookup_never_picks_a_history_run():
    """The live shipper must not allocate a shipment to a live-story history
    run: the history plan already records where that batch went (a
    rehearsal run, 2026-10-02: the first live shipments of MACH-HSG-7742 landed on
    the newest history run and grew the story lot's customer total)."""
    import inspect
    source = inspect.getsource(ShippingSimulator._traceable_batch_id)
    assert "pr.notes IS DISTINCT FROM %s" in source
    assert "HISTORY_RUN_NOTE" in source
    from erp_simulation import story_history
    assert story_history.HISTORY_RUN_NOTE is utils.HISTORY_RUN_NOTE


def test_pg_a_history_run_is_skipped_for_the_newest_live_run(pg_backlog):
    with pg_backlog.cursor() as cur:
        cur.execute("SELECT product_id FROM products ORDER BY product_id DESC LIMIT 1")
        product_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, actual_quantity,
                status, actual_start_time, actual_end_time, updated_at, notes)
            VALUES ('RUN-HIST-NEWEST', %s, 20, 20, 'COMPLETED',
                    NOW() + INTERVAL '1 day', NOW() + INTERVAL '2 days', NOW(), %s)
            RETURNING run_id""", (product_id, utils.HISTORY_RUN_NOTE))
        run_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO lot_production_usage (lot_id, production_run_id, quantity_used,
                       unit_of_measure) SELECT lot_id, %s, 1, 'EA' FROM material_lots
                       ORDER BY lot_id LIMIT 1""", (run_id,))
        picked = _sim()._traceable_batch_id(cur, product_id, "SKU")
    pg_backlog.rollback()
    assert picked is not None and picked != "RUN-HIST-NEWEST"


def test_pg_200_backlog_orders_ship_in_under_10_seconds(pg_backlog):
    sim = _sim()
    started = time.monotonic()
    result = sim.create_shipments(pg_backlog, "perf")
    elapsed = time.monotonic() - started
    pg_backlog.commit()
    assert result["shipments_created"] == utils.MAX_ROWS_PER_CYCLE
    assert elapsed < 10.0, f"200 orders took {elapsed:.1f}s"
    with pg_backlog.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FROM shipment_items si JOIN shipments s USING (shipment_id)
                       JOIN customer_orders co ON co.order_id = s.order_id
                       WHERE co.order_number LIKE 'ORD-PERF-%%' AND si.batch_id LIKE 'RUN-PERF-%%'""")
        assert cur.fetchone()[0] == 3 * utils.MAX_ROWS_PER_CYCLE   # every line carries its traceable run
        cur.execute("SELECT COUNT(*) FROM customer_orders WHERE status = 'READY_TO_SHIP'")
        assert cur.fetchone()[0] == _BACKLOG - utils.MAX_ROWS_PER_CYCLE
