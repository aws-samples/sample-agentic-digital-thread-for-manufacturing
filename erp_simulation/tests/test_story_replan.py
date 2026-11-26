# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Placeable story clock (erp_simulation/story_replan.py, ADR-0089).

Offline: the pure anchor arithmetic, the retire enumeration's FK coverage
against the db-init DDL, and the seed catch-up through a STUBBED
``MESSimulator.insert_condition_work_order`` (Task G's contract). The PG
variant (``ERP_SIM_TEST_PG_DSN``) runs retire / replan against the real
db-init DDL and the seed SQL in two throwaway databases, and then the
facilitator story lookup (``agentic_digital_thread/agents/demo/story_lookup.py``).
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import story_history as sh
from erp_simulation import story_replan as sr
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.tests._pg_sql import stmt

_REPO = Path(__file__).resolve().parents[2]
LOOP = cm.TOTAL_SECONDS


def _utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def _in_last_loop(anchor: int, now: float) -> bool:
    return now - LOOP < anchor <= now


# ---------------------------------------------------------------------------
# Pure anchor arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset", [-7 * 86400 - 1234.5, -LOOP, -1, 0, 149, 151, 3600, 5 * 86400 + 77])
def test_normalize_is_congruent_in_the_last_loop_and_keeps_the_phase(offset):
    now = _utc("2026-10-14T12:03:17")
    anchor = sr.normalize_anchor(now + offset, now)
    assert isinstance(anchor, int)
    assert anchor % cm.TELEMETRY_GRID_SECONDS == cm.ANCHOR_RESIDUE
    assert _in_last_loop(anchor, now)
    assert (cm.snap_anchor(now + offset, "nearest") - anchor) % LOOP == 0
    assert sr.loops_shifted(now + offset, now) * LOOP == cm.snap_anchor(now + offset, "nearest") - anchor


def test_normalize_keeps_a_reference_already_in_the_last_loop():
    now = _utc("2026-10-14T12:00:00")
    ref = cm.snap_anchor(now - 3600, "down")
    assert sr.normalize_anchor(ref, now) == ref and sr.loops_shifted(ref, now) == 0


def test_normalize_boundary_now_minus_a_loop_moves_up_to_now():
    now = float(cm.snap_anchor(_utc("2026-10-14T12:00:00"), "down"))
    assert sr.normalize_anchor(now - LOOP, now) == now
    assert sr.normalize_anchor(now, now) == now


def test_alarm_at_14_10z_places_the_anchor_at_08_45_10z():
    alarm = _utc("2026-10-14T14:10:00")
    ref = sr.alarm_to_anchor(alarm)
    assert ref == _utc("2026-10-14T08:45:10")
    assert ref + cm.ALARM_OFFSET_SECONDS == alarm
    now = _utc("2026-10-14T14:30:00")
    assert sr.normalize_anchor(ref, now) == ref and sr.loops_shifted(ref, now) == 0
    assert [loop for loop, _a, _f in sr.era_alarms(ref, now)] == [0]


def test_an_alarm_more_than_5h25m_ahead_shifts_one_loop_and_catches_up_loop_0():
    now = _utc("2026-10-14T08:00:00")
    ref = sr.alarm_to_anchor(_utc("2026-10-14T14:10:00"))
    anchor = sr.normalize_anchor(ref, now)
    assert anchor == _utc("2026-10-14T00:45:10") and sr.loops_shifted(ref, now) == 1
    ((loop, alarm, fail),) = sr.era_alarms(anchor, now)
    assert (loop, alarm) == (0, _utc("2026-10-14T06:10:00"))
    assert fail == cm.fail_epoch(anchor, anchor)
    # the phase is kept: every 8 h after, the alarm is again at 14:10Z
    assert (anchor + LOOP + cm.ALARM_OFFSET_SECONDS) == _utc("2026-10-14T14:10:00")


@pytest.mark.parametrize("alarm,expected", [
    ("2026-10-14T14:12:29", "2026-10-14T14:10:00"),
    ("2026-10-14T14:12:31", "2026-10-14T14:15:00"),
    ("2026-10-14T14:10:00", "2026-10-14T14:10:00"),
])
def test_alarm_to_anchor_rounds_the_alarm_to_the_nearest_grid_point(alarm, expected):
    anchor = sr.alarm_to_anchor(_utc(alarm))
    assert anchor + cm.ALARM_OFFSET_SECONDS == _utc(expected)
    assert cm.alarm_epoch(anchor, anchor) % cm.TELEMETRY_GRID_SECONDS == 0


@pytest.mark.parametrize("phase", list(sr.PHASE_STEPS))
def test_phase_offset_puts_now_that_far_into_the_phase(phase):
    now = _utc("2026-10-14T12:01:40")
    start, end = sr.PHASE_STEPS[phase]
    elapsed = (end - start) * cm.INTERVAL_SEC / 60 / 2
    anchor = sr.phase_offset_to_anchor(phase, elapsed, now)
    assert anchor % cm.TELEMETRY_GRID_SECONDS == cm.ANCHOR_RESIDUE and _in_last_loop(anchor, now)
    position = (now - anchor) % LOOP
    assert abs(position - (start * cm.INTERVAL_SEC + elapsed * 60)) <= cm.TELEMETRY_GRID_SECONDS / 2
    assert cm.phase(cm.loop_step(cm.story_seconds(now, anchor))) == phase


@pytest.mark.parametrize("phase,elapsed", [("repair", 20), ("repair", -1), ("healthy", 28), ("bogus", 0)])
def test_phase_offset_rejects_an_elapsed_time_outside_the_phase(phase, elapsed):
    with pytest.raises(ValueError):
        sr.phase_offset_to_anchor(phase, elapsed, _utc("2026-10-14T12:00:00"))


def test_story_run_numbers_are_the_minute_steps_for_every_loop_alarmed_by_now():
    story_live = pytest.importorskip("erp_simulation.story_live")
    anchor = float(cm.snap_anchor(_utc("2026-10-12T00:00:00"), "down"))
    assert sr.story_run_numbers(anchor, anchor + cm.ALARM_OFFSET_SECONDS - 1) == []
    now = anchor + 2 * LOOP + cm.ALARM_OFFSET_SECONDS
    numbers = sr.story_run_numbers(anchor, now)
    assert len(numbers) == 3
    for loop, (run, wo) in enumerate(numbers):
        expected = story_live.story_work_order(anchor + loop * LOOP + cm.ALARM_OFFSET_SECONDS + 60, anchor)
        assert (run, wo) == (expected.run_number, expected.work_order_number)
        assert re.fullmatch(rf"RUN-\d{{8}}-8{loop:04d}", run)


def test_lock_key_is_a_signed_bigint():
    assert -(2 ** 63) <= sr.LOCK_KEY < 2 ** 63


def test_replan_rejects_an_unknown_source():
    with pytest.raises(ValueError, match="source"):
        sr.replan(None, None, 1.0, now=2.0, source="manual")


@pytest.mark.parametrize("stored,expected,stale", [
    (100.0, None, False), (100.0, 100, False), (100.0, 101.0, True), (None, 100.0, True), (None, None, False)])
def test_cas(stored, expected, stale):
    if stale:
        with pytest.raises(sr.StaleAnchor):
            sr._check_cas(stored, expected)
    else:
        sr._check_cas(stored, expected)


def _fk_children(parent: str) -> set:
    """Tables whose DDL REFERENCES ``parent`` (db-init)."""
    text = (_REPO / "lambda-db-init" / "handler.py").read_text(encoding="utf-8")
    children = set()
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", text, re.S):
        if re.search(rf"REFERENCES {parent}\(", body):
            children.add(table)
    return children


def test_retire_steps_cover_every_fk_child_of_a_retired_parent():
    """A new FK child of a retired table must join the retire (else the
    delete fails, or worse, a later writer change leaks rows)."""
    tables = {re.search(r"DELETE FROM (\w+)", sql).group(1) for _k, _i, sql in sr._RETIRE_STEPS}
    for parent in ("production_runs", "mes_work_orders", "customer_orders", "shipments",
                   "order_line_items", "invoices"):
        assert parent in tables
        assert _fk_children(parent) <= tables, (parent, _fk_children(parent) - tables)


class _SeedCursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.statements.append(sql)


def test_catch_up_uses_the_monitors_writer_per_alarmed_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(MESSimulator, "insert_condition_work_order",
                        classmethod(lambda cls, cur, loop, alarm, fail: calls.append((loop, alarm, fail)) or 1),
                        raising=False)
    anchor = float(cm.snap_anchor(_utc("2026-10-14T00:00:00"), "down"))
    now = anchor + LOOP + cm.ALARM_OFFSET_SECONDS
    assert sr._catch_up_condition_orders(_SeedCursor(), anchor, now) == 2
    assert calls == [(loop, anchor + loop * LOOP + cm.ALARM_OFFSET_SECONDS,
                      anchor + loop * LOOP + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC) for loop in (0, 1)]


def test_catch_up_before_the_first_alarm_needs_no_writer(monkeypatch):
    monkeypatch.delattr(MESSimulator, "insert_condition_work_order", raising=False)
    anchor = float(cm.snap_anchor(_utc("2026-10-14T00:00:00"), "down"))
    assert sr._catch_up_condition_orders(_SeedCursor(), anchor, anchor + 60) == 0


def test_module_is_small_and_its_functions_short():
    import ast
    src = (_REPO / "erp_simulation" / "story_replan.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) < 800
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno + 1 <= 50, node.name


# ---------------------------------------------------------------------------
# Real PostgreSQL (throwaway container): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# ---------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_SEED_SQL = ("001", "003", "005", "011")   # schemas, frozen MES, PLM, site CMMS
_ERP_TABLES = ("production_runs", "mes_work_orders", "mes_quality_checks", "mes_production_events",
               "mes_downtime_events", "lot_production_usage", "lot_mes_tracking", "customer_orders",
               "order_line_items", "shipments", "shipment_items", "invoices", "invoice_line_items", "payments")
_needs_g = pytest.mark.xfail(
    not hasattr(MESSimulator, "insert_condition_work_order"),
    reason="Task G's MESSimulator.insert_condition_work_order not merged yet", strict=True)


def _load_db_init():
    spec = importlib.util.spec_from_file_location("db_init_handler_replan", _REPO / "lambda-db-init" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pg():
    """{erp, seed: connection, dsn: {erp, seed}} on two throwaway databases."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    pytest.importorskip("boto3")
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    names = {"erp": "rperp_" + uuid.uuid4().hex[:8], "seed": "rpseed_" + uuid.uuid4().hex[:8]}
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    dsns = {k: make_dsn(**{**parse_dsn(_DSN), "dbname": v}) for k, v in names.items()}
    conns = {k: pgdriver.connect(d) for k, d in dsns.items()}
    try:
        db_init = _load_db_init()
        db_init.create_schema(conns["erp"])
        db_init.seed_data(conns["erp"])
        with conns["seed"].cursor() as cur:
            for number in _SEED_SQL:
                (path,) = (_REPO / "lambda-adt-seed").glob(f"{number}_*.sql")
                cur.execute(path.read_text(encoding="utf-8"))
        conns["seed"].commit()
        yield {**conns, "dsn": dsns}
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _counts(conn):
    return {t: _all(conn, f"SELECT count(*) FROM {t}")[0][0] for t in _ERP_TABLES}  # nosec B608 - test constants


def _config(conn):
    return dict(_all(conn, "SELECT config_key, config_value FROM simulation_config WHERE config_key LIKE 'story%%'"))


def _era(pg, days_ago: float = 2.0):
    """Write the history on anchor A1, start the loop-0 story run (RUNNING,
    one failing check), and return ``(A1, story run number)``."""
    story_live = pytest.importorskip("erp_simulation.story_live")
    a1 = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp() - days_ago * sh.DAY, "down"))
    sh.write(pg["erp"], pg["seed"], sh.plan(a1))
    alarm = a1 + cm.ALARM_OFFSET_SECONDS + 60
    assert story_live.apply_cycle(pg["erp"], alarm, a1) == 1
    pg["erp"].commit()
    return a1, story_live.story_work_order(alarm, a1).run_number


def _simulator_rows(conn):
    """Rows the simulator owns: a LINE3 story-part run with a random number
    (RUNNING, a failing check) and an order + shipment of its own."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH run AS (
                INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
                SELECT 'RUN-20260101-12345', product_id, 10, 'IN_PROGRESS', %s, %s FROM products WHERE sku = %s
                RETURNING run_id, product_id)
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                planned_quantity, status) SELECT 'WO-20260101-12345', run_id, product_id, %s, %s, 10, 'RUNNING'
            FROM run RETURNING work_order_id""",
                    (sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU, sh.SITE, cm.STORY_MES_LINE))
        (wo_id,) = cur.fetchone()
        cur.execute("INSERT INTO mes_quality_checks (work_order_id, check_type, sample_size, pass_count, "
                    "fail_count) VALUES (%s, 'DIMENSIONAL', 20, 15, 5)", (wo_id,))
        cur.execute("""
            WITH o AS (
                INSERT INTO customer_orders (order_number, customer_id, status, total_amount)
                SELECT 'ORD-20260101-55555555', customer_id, 'SHIPPED', 1 FROM customers
                WHERE customer_code = %s RETURNING order_id, customer_id)
            INSERT INTO shipments (shipment_number, order_id, customer_id, status)
            SELECT 'SHIP-20260101-55555555', order_id, customer_id, 'SHIPPED' FROM o""",
                    (sh.STORY_CUSTOMER_CODES[0],))
    conn.commit()


def _invoice_a_history_shipment(conn, sp):
    """What the finance simulator does to a history shipment (invoice, line, payment)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH inv AS (
                INSERT INTO invoices (invoice_number, customer_id, order_id, shipment_id, status, total_amount)
                SELECT 'INV-T-1', s.customer_id, s.order_id, s.shipment_id, 'PAID', 10
                FROM shipments s WHERE s.shipment_number = %s RETURNING invoice_id, customer_id),
            line AS (
                INSERT INTO invoice_line_items (invoice_id, line_number, description, quantity, unit_price)
                SELECT invoice_id, 1, 'x', 1, 10 FROM inv)
            INSERT INTO payments (payment_number, customer_id, invoice_id, amount)
            SELECT 'PAY-T-1', customer_id, invoice_id, 10 FROM inv""", (sp.run_orders[0].shipment_number,))
    conn.commit()


def _expected_retire(sp, story_runs: int, story_checks: int, story_events: int, finance: int):
    runs = (*sp.runs, sp.failed_run)
    orders = len(sp.orders) + len(sp.run_orders)
    return {**dict.fromkeys(sr.RETIRE_COUNT_KEYS, 0),
            "runs": len(runs), "story_runs": story_runs, "work_orders": len(runs) + story_runs,
            "orders": orders, "order_lines": orders, "shipments": orders, "shipment_items": orders,
            "quality_checks": sum(len(r.checks) for r in runs) + story_checks,
            "production_events": 2 * len(runs) + story_events, "downtime_events": 1 + len(sp.past_stops),
            "lot_production_usage": len(runs), "lot_mes_tracking": len(runs),
            "material_consumption": len(runs),
            "invoices": finance, "invoice_line_items": finance, "payments": finance}


def test_pg_retire_deletes_exactly_the_plans_rows(pg):
    erp = pg["erp"]
    a1, story_run = _era(pg)
    sp = sh.plan(a1)
    _simulator_rows(erp)
    _invoice_a_history_shipment(erp, sp)
    before = _counts(erp)
    with erp.cursor() as cur:
        retired = sr.retire_era(cur, a1, now=a1 + 2 * sh.DAY)
    erp.commit()
    assert retired == _expected_retire(sp, story_runs=1, story_checks=1, story_events=1, finance=1)
    after = _counts(erp)
    table_of = {"runs": "production_runs", "story_runs": "production_runs", "work_orders": "mes_work_orders",
                "quality_checks": "mes_quality_checks", "production_events": "mes_production_events",
                "downtime_events": "mes_downtime_events", "orders": "customer_orders",
                "order_lines": "order_line_items"}
    removed = {t: 0 for t in _ERP_TABLES}
    for key, n in retired.items():
        table = table_of.get(key, key)
        if table in removed:
            removed[table] += n
    assert {t: before[t] - after[t] for t in _ERP_TABLES} == removed
    numbers = [r.run_number for r in (*sp.runs, sp.failed_run)] + [story_run]
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = ANY(%s)", (numbers,)) == [(0,)]
    orders = [o.order_number for o in (*sp.orders, *sp.run_orders)]
    assert _all(erp, "SELECT count(*) FROM customer_orders WHERE order_number = ANY(%s)", (orders,)) == [(0,)]
    # simulator-owned rows are untouched
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = 'RUN-20260101-12345'") == [(1,)]
    assert _all(erp, "SELECT count(*) FROM mes_quality_checks q JOIN mes_work_orders w USING (work_order_id) "
                     "WHERE w.work_order_number = 'WO-20260101-12345'") == [(1,)]
    assert _all(erp, "SELECT count(*) FROM shipments WHERE shipment_number = 'SHIP-20260101-55555555'") == [(1,)]
    with erp.cursor() as cur:
        assert sr.retire_era(cur, a1, now=a1 + 2 * sh.DAY) == dict.fromkeys(sr.RETIRE_COUNT_KEYS, 0)
    erp.commit()


def _place(pg, a1, now, alarm, **kw):
    return sr.replan(pg["erp"], pg["seed"], sr.alarm_to_anchor(alarm), now=now, source="placed",
                     expected_anchor=kw.pop("expected", a1), **kw)


def test_pg_replan_retires_the_old_era_and_writes_the_new_one(pg):
    erp = pg["erp"]
    a1, story_run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    out = _place(pg, a1, now, now + 3600)
    a2 = out["new_anchor"]
    assert a2 == sr.alarm_to_anchor(now + 3600) and out["loops_shifted"] == 0
    assert out["alarm_at"] == cm.grid_floor(now + 3600 + 150)
    assert (out["old_anchor"], out["source"]) == (a1, "placed")
    assert out["retired"] == {**_expected_retire(sh.plan(a1), 1, 1, 1, 0), "condition_orders": 0}
    assert (out["written"]["runs"], out["written"]["orders"]) == (19, 21)
    assert out["written"]["condition_orders"] == 0            # loop 0's alarm is still ahead
    sp2 = sh.plan(a2)
    config = _config(erp)
    assert float(config["story_anchor_epoch"]) == a2 and config["story_anchor_source"] == "placed"
    assert config["story_failed_run_number"] == sp2.failed_run.run_number == out["failed_run_number"]
    assert float(config["story_telemetry_anchor"]) == a2
    assert int(config["story_telemetry_cursor_epoch"]) == sh.telemetry_cursor(a2, 3)
    assert out["telemetry"]["cursor_epoch"] == sh.telemetry_cursor(a2, 3)
    old = [r.run_number for r in (*sh.plan(a1).runs, sh.plan(a1).failed_run)] + [story_run]
    new = [r.run_number for r in (*sp2.runs, sp2.failed_run)]
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = ANY(%s) "
                     "AND NOT run_number = ANY(%s)", (old, new)) == [(0,)]
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = ANY(%s)", (new,)) == [(19,)]
    assert _all(erp, """
        SELECT DISTINCT pr.run_number FROM production_runs pr
        JOIN mes_work_orders w ON w.production_run_id = pr.run_id
        JOIN mes_quality_checks q ON q.work_order_id = w.work_order_id
        JOIN shipment_items si ON si.batch_id = pr.run_number
        WHERE q.check_type = 'DIMENSIONAL' AND q.fail_count > 0""") == [(sp2.failed_run.run_number,)]
    # the seed side follows the new plan
    for wo in sp2.cmms_work_orders:
        assert _all(pg["seed"], "SELECT reported_date, actual_finish FROM cmms_indianapolis.work_orders "
                                "WHERE wonum = %s", (wo.wonum,)) == [(wo.reported_date, wo.actual_finish)]


def test_pg_replan_to_the_same_anchor_retires_and_writes_nothing(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    a2 = _place(pg, a1, now, now + 3600)["new_anchor"]
    with erp.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = '1' WHERE config_key = 'story_telemetry_cursor_epoch'")
    erp.commit()
    rows = _counts(erp)
    again = _place(pg, a1, now + 60, now + 3600, expected=a2)
    assert again["new_anchor"] == a2 and again["old_anchor"] == a2
    assert set(again["retired"].values()) == {0} and set(again["written"].values()) == {0}
    assert set(again["seed"].values()) == {0}
    assert _counts(erp) == rows
    assert int(_config(erp)["story_telemetry_cursor_epoch"]) == sh.telemetry_cursor(a2, 3)   # re-sweep
    # one supersede entry per frozen open order, one prior-era prefix, after two re-plans
    seed = pg["seed"]
    for wonum in ("1182", "1201"):
        assert _all(seed, "SELECT count(*) FROM cmms_indianapolis.work_order_log WHERE wonum = %s "
                          "AND entry LIKE %s", (wonum, sh.SUPERSEDED_LOG_ENTRY + "%")) == [(1,)]
    (desc,) = _all(seed, "SELECT description FROM cmms_indianapolis.work_orders WHERE wonum = '1187'")[0]
    assert desc.startswith(sh.PRIOR_ERA_PREFIX) and desc.count(sh.PRIOR_ERA_PREFIX.strip()) == 1



def _legacy_run_holding(conn, run_number: str) -> None:
    """A pre-ADR-0090 simulator run that holds a history run number."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
            SELECT %s, product_id, 10, 'COMPLETED', 'detroit', 'line1' FROM products ORDER BY sku LIMIT 1""",
                    (run_number,))
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, planned_quantity)
            SELECT 'WO-20000101-31337', run_id, product_id, 10 FROM production_runs WHERE run_number = %s""",
                    (run_number,))
    conn.commit()


def _stop_in_both_systems(pg, stop) -> tuple:
    """(MES downtime rows, CMMS orders, CMMS log rows) of one past stop."""
    return (_all(pg["erp"], "SELECT count(*) FROM mes_downtime_events WHERE downtime_id = %s::uuid",
                 (stop.downtime_id,))[0][0],
            _all(pg["seed"], "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum = %s",
                 (stop.order.wonum,))[0][0],
            _all(pg["seed"], "SELECT count(*) FROM cmms_indianapolis.work_order_log WHERE wonum = %s",
                 (stop.order.wonum,))[0][0])


def test_pg_a_placement_keeps_a_held_runs_stop_out_of_both_systems(pg):
    """A placement onto an era where another run holds a past stop's run
    number writes neither the MES stop nor its bridge order: the CMMS never
    holds a completed bridge order with no MES downtime behind it."""
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    a2 = sr.alarm_to_anchor(now + 3600)
    held = sh.plan(a2).past_stops[1]
    _legacy_run_holding(pg["erp"], held.run_number)
    assert _place(pg, a1, now, now + 3600)["new_anchor"] == a2
    assert _stop_in_both_systems(pg, held) == (0, 0, 0)
    for stop in sh.plan(a2).past_stops:
        if stop.run_number != held.run_number:
            assert _stop_in_both_systems(pg, stop) == (1, 1, 1)


def test_pg_a_same_anchor_replan_keeps_a_held_runs_stop_out_of_the_cmms(pg):
    """The same-anchor branch (a Reset onto the stored anchor) rewrites only
    the seed side; it must leave out the stop the history writer left out."""
    now = float(int(datetime.now(timezone.utc).timestamp()))
    anchor = float(sr.normalize_anchor(now - 2 * sh.DAY, now))
    sp = sh.plan(anchor)
    held = sp.past_stops[2]
    _legacy_run_holding(pg["erp"], held.run_number)
    sh.write(pg["erp"], pg["seed"], sp)
    assert _stop_in_both_systems(pg, held) == (0, 0, 0)
    out = sr.replan(pg["erp"], pg["seed"], anchor, now=now, source="placed", expected_anchor=anchor)
    assert out["new_anchor"] == out["old_anchor"] == anchor
    assert _stop_in_both_systems(pg, held) == (0, 0, 0)


def test_pg_a_moved_anchor_records_the_servers_catch_up_and_a_same_anchor_replan_keeps_it(pg):
    """ADR-0092 amendment: a placement records where the OPC UA server
    climbs from (the previous anchor and, for decision 8, that era's source),
    when it starts (2 min later) and where it ends (the new anchor's first
    alarm after that); the anchor mirror reads it back. A re-plan onto the
    same anchor leaves it as it was, and a later move records the placed era."""
    from erp_simulation import story_anchor_mirror as mirror
    erp = pg["erp"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    out = _place(pg, a1, now, now + 3600)
    a2 = out["new_anchor"]
    assert out["written"]["catch_up"] == 1
    start, end = int(now) + sr.CATCH_UP_LEAD_SECONDS, a2 + cm.ALARM_OFFSET_SECONDS
    assert _config(erp)[sr.CATCH_UP_KEY] == f"{int(a1)};{start};{end};deploy"
    assert mirror.read_catch_up(erp) == (int(a1), start, end, "deploy")
    again = _place(pg, a1, now + 60, now + 3600, expected=a2)
    assert "catch_up" not in again["written"]
    assert _config(erp)[sr.CATCH_UP_KEY] == f"{int(a1)};{start};{end};deploy"
    later = _place(pg, a1, now + 120, now + 7200, expected=a2)
    assert mirror.read_catch_up(erp)[::3] == (int(a2), "placed")
    assert later["old_anchor"] == a2


def test_the_catch_up_row_records_only_a_known_previous_source():
    row = lambda source: sr.catch_up_row(1_790_000_010, 1_790_003_610, 1_790_000_100, source).value  # noqa: E731
    start, end = sr.catch_up_window(1_790_003_610, 1_790_000_100)
    assert row("deploy") == f"1790000010;{start};{end};deploy"
    assert row("packaged").endswith(";packaged") and row("placed").endswith(";placed")
    assert row(None) == row("garbage") == f"1790000010;{start};{end}"

def test_pg_replan_then_restore_gives_back_the_frozen_seed(pg):
    seed = pg["seed"]
    snapshot = lambda: (_all(seed, "SELECT * FROM cmms_indianapolis.work_orders ORDER BY wonum"),  # noqa: E731
                        _all(seed, "SELECT wonum, log_date, author, log_type, entry "
                                   "FROM cmms_indianapolis.work_order_log ORDER BY log_id"))
    frozen = snapshot()
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    _place(pg, a1, now, now + 3600)
    sh.restore_frozen_seed(seed)
    assert snapshot() == frozen


def test_pg_replan_cas_mismatch_raises_and_changes_nothing(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    rows, config = _counts(erp), _config(erp)
    now = a1 + 2 * sh.DAY
    with pytest.raises(sr.StaleAnchor):
        _place(pg, a1, now, now + 3600, expected=a1 + 300)
    assert (_counts(erp), _config(erp)) == (rows, config)


def test_pg_concurrent_replans_are_serialised_and_one_is_stale(pg):
    from erp_simulation import pgdriver
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    results, barrier = [], threading.Barrier(2)

    def place(alarm):
        erp, seed = pgdriver.connect(pg["dsn"]["erp"]), pgdriver.connect(pg["dsn"]["seed"])
        try:
            barrier.wait()
            results.append(sr.replan(erp, seed, sr.alarm_to_anchor(alarm), now=now, source="placed",
                                     expected_anchor=a1)["new_anchor"])
        except sr.StaleAnchor:
            results.append("stale")
        finally:
            erp.close()
            seed.close()

    threads = [threading.Thread(target=place, args=(now + d,)) for d in (3600, 5400)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(results, key=str).count("stale") == 1, results
    (winner,) = [r for r in results if r != "stale"]
    assert float(_config(pg["erp"])["story_anchor_epoch"]) == winner
    assert _all(pg["erp"], "SELECT count(*) FROM production_runs WHERE notes = 'Live-story history run'") == [(19,)]


def _condition_order(seed, wonum, reported):
    with seed.cursor() as cur:
        cur.execute("INSERT INTO cmms_indianapolis.work_orders (wonum, asset_num, work_type, description, "
                    "status, priority, reported_by, reported_date) VALUES "
                    "(%s, %s, 'CM', 'old era', 'WAPPR', 1, 'Condition monitor', %s)",
                    (wonum, cm.STORY_CMMS_ASSET_NUM, sh.ts(reported)))
    seed.commit()


def test_pg_replan_purges_every_condition_order_reported_before_now(pg):
    """D11: an old-era VIB-CMX reported AFTER the new anchor still goes."""
    seed = pg["seed"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    _condition_order(seed, "VIB-CMX-000000", a1 + cm.ALARM_OFFSET_SECONDS)
    _condition_order(seed, "VIB-CMX-000005", now - 600)
    out = _place(pg, a1, now, now + 3600)
    assert out["retired"]["condition_orders"] == 2 and out["written"]["condition_orders"] == 0
    assert _all(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum LIKE 'VIB-CMX-%%'") == [(0,)]


@_needs_g
def test_pg_replan_catches_up_the_new_eras_alarmed_loop(pg):
    seed = pg["seed"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    _condition_order(seed, "VIB-CMX-000000", a1 + cm.ALARM_OFFSET_SECONDS)   # old era, same wonum
    out = _place(pg, a1, now, now - 1200)
    alarm = out["new_anchor"] + cm.ALARM_OFFSET_SECONDS
    assert alarm <= now and out["written"]["condition_orders"] == 1
    assert out["retired"]["condition_orders"] == 1
    assert _all(seed, "SELECT wonum, reported_date FROM cmms_indianapolis.work_orders "
                      "WHERE wonum LIKE 'VIB-CMX-%%'") == [("VIB-CMX-000000", sh.ts(alarm))]
    again = _place(pg, a1, now + 60, now - 1200, expected=out["new_anchor"])
    assert again["retired"]["condition_orders"] == 0 and again["written"]["condition_orders"] == 0


def test_pg_story_lookup_after_a_replan_reports_the_new_failed_batch(pg, monkeypatch):
    app = str(_REPO / "agentic_digital_thread")
    if app not in sys.path:
        monkeypatch.syspath_prepend(app)
    sl = pytest.importorskip("agents.demo.story_lookup")
    db_routing = pytest.importorskip("agents.demo.db_routing")
    monkeypatch.setattr(db_routing, "demo_pg_dsn", lambda schema: (
        pg["dsn"]["erp"] if db_routing.is_erp_schema(schema) else pg["dsn"]["seed"]))
    a1, _run = _era(pg)
    old_failed = sh.plan(a1).failed_run.run_number
    now = a1 + 2 * sh.DAY + 1800
    out = _place(pg, a1, now, now + 3600)
    live = sl.lookup("live", now=now + 60)
    assert live["warnings"] == [], live["warnings"]
    assert live["clock"]["anchor_epoch"] == out["new_anchor"]
    assert live["failed_batch"]["run_number"] == out["failed_run_number"] != old_failed
    assert live["failed_batch"]["is_history_run"] is True
    assert {c["customer_code"] for c in live["customers"]} == set(sh.STORY_CUSTOMER_CODES)
    assert len(live["customers"]) == 3 and live["total_value"] == 10200
    assert live["live_defect_run"] is None                    # the old era's story run is gone
    assert {w["wonum"] for w in live["maintenance"]["work_orders"]} == set(sh.STORY_ORDER_WONUMS)
    assert old_failed not in {c.get("batch_id") for c in live["customers"]}


# ---------------------------------------------------------------------------
# One era at a time: the per-cycle writers vs a placement (ADR-0089)
# ---------------------------------------------------------------------------

def _alarm_cycle(a1: float, loop: int = 5) -> float:
    """A cycle instant inside ``loop``'s BPFO-alarm window of ``a1``."""
    return a1 + loop * LOOP + cm.ALARM_OFFSET_SECONDS + 60


def _vib_orders(seed):
    return _all(seed, "SELECT wonum, reported_date FROM cmms_indianapolis.work_orders "
                      "WHERE wonum LIKE 'VIB-CMX-%%' ORDER BY wonum")


def test_pg_retire_never_matches_a_simulator_run_with_a_story_run_number(pg):
    """A simulator run whose random number is an old-era loop's story run
    number (same site/line/part, no order) has its OWN work-order number: it
    is never retired."""
    erp = pg["erp"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY
    (loop1_run, loop1_wo) = sr.story_run_numbers(a1, now)[1]
    with erp.cursor() as cur:
        cur.execute("""
            WITH run AS (
                INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
                SELECT %s, product_id, 10, 'IN_PROGRESS', %s, %s FROM products WHERE sku = %s
                RETURNING run_id, product_id)
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                planned_quantity, status) SELECT 'WO-20260101-77777', run_id, product_id, %s, %s, 10, 'RUNNING'
            FROM run""", (loop1_run, sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU, sh.SITE, cm.STORY_MES_LINE))
    erp.commit()
    assert loop1_wo != "WO-20260101-77777"
    with erp.cursor() as cur:
        retired = sr.retire_era(cur, a1, now=now)
    erp.commit()
    assert retired["story_runs"] == 1                        # loop 0's story run only
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s", (loop1_run,)) == [(1,)]
    assert _all(erp, "SELECT count(*) FROM mes_work_orders WHERE work_order_number = 'WO-20260101-77777'") \
        == [(1,)]


@_needs_g
def test_pg_a_cycle_that_loaded_the_old_anchor_writes_nothing_after_a_placement(pg):
    """The cycle loaded A1, a placement then moved the clock: neither the
    story step nor the condition monitor writes an old-era row."""
    story_live = pytest.importorskip("erp_simulation.story_live")
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    t = _alarm_cycle(a1)
    cached = story_live.read_anchor(erp)                     # the cycle's load
    erp.commit()
    a2 = _place(pg, a1, t, t + 3600)["new_anchor"]
    assert cached == a1 != a2
    assert story_live.apply_cycle(erp, t, cached) == 0
    erp.commit()
    old_run = story_live.story_work_order(t, a1).run_number
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s", (old_run,)) == [(0,)]
    sim = MESSimulator.__new__(MESSimulator)
    sim.condition_anchor_epoch = cached
    sim.condition_anchor_reader = lambda: story_live.read_anchor(erp)
    sim._get_seed_db_conn = lambda: seed
    assert sim.monitor_asset_condition("race", now=t) == 0
    erp.commit()
    assert _vib_orders(seed) == []                            # no stray VIB-CMX-000005
    # the same cycle on the NEW anchor still writes (the guard is the era, not a switch-off)
    sim.condition_anchor_epoch = float(a2)
    new_alarm = a2 + cm.ALARM_OFFSET_SECONDS
    assert sim.monitor_asset_condition("new-era", now=new_alarm + 60) == 1
    erp.commit()
    assert _vib_orders(seed) == [("VIB-CMX-000000", sh.ts(new_alarm))]


def test_pg_apply_cycle_waits_for_a_placement_in_flight_then_skips(pg):
    """The shared lock waits for the exclusive one, and the anchor is re-read
    AFTER it (a snapshot taken before the wait would still see A1)."""
    from erp_simulation import pgdriver
    story_live = pytest.importorskip("erp_simulation.story_live")
    a1, _run = _era(pg)
    t = _alarm_cycle(a1)
    holder, worker = pgdriver.connect(pg["dsn"]["erp"]), pgdriver.connect(pg["dsn"]["erp"])
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (sr.LOCK_KEY,))   # a placement in flight
        result = []
        thread = threading.Thread(target=lambda: result.append(story_live.apply_cycle(worker, t, a1)))
        thread.start()
        thread.join(1.0)
        assert thread.is_alive() and result == []             # waiting on the exclusive lock
        with holder.cursor() as cur:
            cur.execute("UPDATE simulation_config SET config_value = %s WHERE config_key = %s",
                        (repr(a1 + LOOP), sr.ANCHOR_KEY))
        holder.commit()                                       # the placement commits, lock released
        thread.join(30)
        worker.commit()
        assert result == [0]
        old_run = story_live.story_work_order(t, a1).run_number
        assert _all(pg["erp"], "SELECT count(*) FROM production_runs WHERE run_number = %s", (old_run,)) == [(0,)]
    finally:
        holder.close()
        worker.close()


def _cmms_dates(seed, sp):
    return [_all(seed, "SELECT reported_date FROM cmms_indianapolis.work_orders WHERE wonum = %s", (wo.wonum,))
            for wo in sp.cmms_work_orders]


def test_pg_an_interleaved_earlier_placement_never_writes_its_seed_era(pg):
    """A's ERP half, then all of B, then A's seed half: A's seed half sees the
    committed ERP anchor is B's and raises StaleAnchor with the seed untouched,
    so ERP and seed stay in ONE era (B's)."""
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    target_a = sr.normalize_anchor(sr.alarm_to_anchor(now + 3600), now)
    erp_a = sh._in_transaction(erp, sr._replan_erp, {"target": target_a, "now": now, "source": "placed",
                                                     "expected": a1})
    out_b = _place(pg, a1, now, now + 5400, expected=target_a)
    sp_b = sh.plan(out_b["new_anchor"])
    before = (_all(seed, "SELECT * FROM cmms_indianapolis.work_orders ORDER BY wonum"),
              _all(seed, "SELECT * FROM cmms_indianapolis.work_order_log ORDER BY log_id"))
    with pytest.raises(sr.StaleAnchor, match="re-placed"):
        sh._in_transaction(seed, sr._replan_seed, {"plan": erp_a["plan"], "now": now, "erp_conn": erp})
    after = (_all(seed, "SELECT * FROM cmms_indianapolis.work_orders ORDER BY wonum"),
             _all(seed, "SELECT * FROM cmms_indianapolis.work_order_log ORDER BY log_id"))
    assert after == before
    assert _cmms_dates(seed, sp_b) == [[(wo.reported_date,)] for wo in sp_b.cmms_work_orders]
    assert float(_config(erp)["story_anchor_epoch"]) == out_b["new_anchor"]


def test_pg_the_seed_half_waits_for_the_monitors_shared_lock(pg):
    """The re-plan's seed half takes the story-clock lock EXCLUSIVE on the seed
    database: a monitor holding it shared makes it wait (ERP already moved)."""
    from erp_simulation import pgdriver
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    monitor = pgdriver.connect(pg["dsn"]["seed"])
    erp, seed = pgdriver.connect(pg["dsn"]["erp"]), pgdriver.connect(pg["dsn"]["seed"])
    try:
        with monitor.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", (sr.LOCK_KEY,))
        result = []
        thread = threading.Thread(target=lambda: result.append(sr.replan(
            erp, seed, sr.alarm_to_anchor(now + 3600), now=now, source="placed", expected_anchor=a1)))
        thread.start()
        thread.join(2.0)
        assert thread.is_alive() and result == []
        new_anchor = sr.normalize_anchor(sr.alarm_to_anchor(now + 3600), now)
        assert float(_config(pg["erp"])["story_anchor_epoch"]) == new_anchor     # the ERP half committed
        pg["erp"].commit()
        monitor.commit()                                      # the monitor's transaction ends
        thread.join(30)
        assert [r["new_anchor"] for r in result] == [new_anchor]
    finally:
        for conn in (monitor, erp, seed):
            conn.close()


# ---------------------------------------------------------------------------
# Packaged loop length (ADR-0091): pure anchor arithmetic on a 3 h loop
# ---------------------------------------------------------------------------

from erp_simulation.tests._loop_env import run_under_loop  # noqa: E402

_REPLAN_DUMP = """\
import random
from erp_simulation import condition_monitor as cm, story_replan as sr
rng = random.Random(10800)
now = 1.8e9
norm = [sr.normalize_anchor(rng.uniform(1.7e9, 1.9e9), now) for _ in range(100)]
phases = {p: sr.phase_offset_to_anchor(p, (e - s) * 5 / 120, now) for p, (s, e) in sr.PHASE_STEPS.items()}  # mid-phase
placed = sr.alarm_to_anchor(now + 5400)
eras = sr.era_alarms(sr.normalize_anchor(placed, now) - 8 * cm.TOTAL_SECONDS, now)
emit({"loop": sr.LOOP_SECONDS, "norm": norm, "phase_steps": sr.PHASE_STEPS,
      "phases": {p: [a, cm.phase(cm.loop_step(cm.story_seconds(now, a)))] for p, a in phases.items()},
      "placed": [placed, placed + cm.ALARM_OFFSET_SECONDS], "eras": eras,
      "runs": [r for r, _w in sr.story_run_numbers(eras and eras[0][1] - cm.ALARM_OFFSET_SECONDS or now, now)]})
"""


def test_replan_constants_are_the_shipped_ones_when_unset():
    assert sr.LOOP_SECONDS == 28800
    assert sr.PHASE_STEPS == {"degrading": (0, 3898), "bpfo_alarm": (3898, 5184),
                              "repair": (5184, 5424), "healthy": (5424, 5760)}


def test_a_three_hour_loop_normalises_to_the_new_congruence():
    out = run_under_loop("10800", _REPLAN_DUMP)
    now = 1.8e9
    assert out["loop"] == 10800
    assert all(a % 300 == 190 and now - 10800 < a <= now for a in out["norm"])
    assert out["phase_steps"] == {"degrading": [0, 1462], "bpfo_alarm": [1462, 1944],
                                  "repair": [1944, 2034], "healthy": [2034, 2160]}
    for phase, (anchor, at_now) in out["phases"].items():
        assert anchor % 300 == 190 and at_now == phase
    placed, alarm = out["placed"]
    assert placed % 300 == 190 and alarm % 300 == 0 and abs(alarm - (now + 5400)) <= 150
    assert len(out["eras"]) == 8  # eight 3 h loops back from the placed anchor
    for _loop, alarm, fail in out["eras"]:
        assert alarm % 300 == 0 and fail - alarm == 2410 and alarm <= now
    assert len(set(out["runs"])) == len(out["runs"]) == 8


# ---------------------------------------------------------------------------
# A changed loop geometry (ADR-0091): retire the history NAMESPACE, whatever
# geometry wrote it, and always rewrite the geometry config keys.
# ---------------------------------------------------------------------------

def test_the_namespace_patterns_match_every_number_the_plan_and_the_story_step_write():
    a1 = float(cm.snap_anchor(_utc("2026-09-20T00:00:00"), "down"))
    sp = sh.plan(a1)
    for run in (*sp.runs, sp.failed_run):
        assert re.match(sr.HISTORY_RUN_PATTERN, run.run_number), run.run_number
        assert run.work_order_number == "WO-" + run.run_number[len("RUN-"):]
    for order in (*sp.orders, *sp.run_orders):
        assert re.match(sr.HISTORY_ORDER_PATTERN, order.order_number), order.order_number
        assert order.shipment_number.rsplit("-", 1)[1] == order.order_number.rsplit("-", 1)[1]
    for run, wo in sr.story_run_numbers(a1, a1 + 3 * sh.DAY):
        assert re.match(sr.STORY_RUN_PATTERN, run) and wo == "WO-" + run[len("RUN-"):]
    # simulator numbers (ADR-0090: 5-digit RUN 0-39999, 8-digit ORD below 40000000) never match
    for sim in ("RUN-20260101-12345", "RUN-20260101-39999"):
        assert not re.match(sr.HISTORY_RUN_PATTERN, sim) and not re.match(sr.STORY_RUN_PATTERN, sim)
    assert not re.match(sr.HISTORY_ORDER_PATTERN, "ORD-20260101-39999999")


def test_geometry_drift_compares_the_stored_geometry_keys_with_the_plan():
    sp = sh.plan(float(cm.snap_anchor(_utc("2026-09-20T00:00:00"), "down")))
    same = {row.key: row.value for row in sp.config}
    assert sr.GEOMETRY_KEYS == ("story_loop_seconds", "story_interval_seconds", "story_bpfo_alarm_step",
                                "story_fail_step", "story_repair_steps")
    assert sr._geometry_drift(same, sp) is False
    for key in sr.GEOMETRY_KEYS:
        assert sr._geometry_drift({**same, key: "1"}, sp) is True
        assert sr._geometry_drift({k: v for k, v in same.items() if k != key}, sp) is True
    assert sr._geometry_drift({**same, "story_anchor_epoch": "1.0"}, sp) is False


_LOOP_CHANGE_CHILD = """\
import importlib.util
from erp_simulation import pgdriver
from erp_simulation import condition_monitor as cm, story_history as sh, story_replan as sr
dsns, now = {dsns!r}, {now!r}
spec = importlib.util.spec_from_file_location("lsh_loop", "lambda_/lambda_story_history.py")
lam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lam)
lam._connect = lambda env: pgdriver.connect(dsns["erp" if env == "DB_SECRET_NAME" else "seed"])
lam._now = lambda: now
out = lam.lambda_handler({{"RequestType": "Update", "PhysicalResourceId": "adt-story-history",
    "ResourceProperties": {{"Days": "3", "Anchor": "", "LoopSeconds": "{loop}"}},
    "OldResourceProperties": {{"Days": "3", "Anchor": ""}}}}, None)
anchor = float(out["Data"]["AnchorEpoch"])
sp = sh.plan(anchor)
emit({{"data": out["Data"], "geometry": cm.story_geometry(), "residue": cm.ANCHOR_RESIDUE,
      "runs": sorted(r.run_number for r in (*sp.runs, sp.failed_run)),
      "work_orders": sorted(r.work_order_number for r in (*sp.runs, sp.failed_run)),
      "orders": sorted(o.order_number for o in (*sp.orders, *sp.run_orders)),
      "shipments": sorted(o.shipment_number for o in (*sp.orders, *sp.run_orders)),
      "failed": sp.failed_run.run_number,
      "cmms": {{wo.wonum: str(wo.reported_date) for wo in sp.cmms_work_orders}},
      "alarms": [[sr._condition_wonum(loop), str(sh.ts(alarm))] for loop, alarm, _f in sr.era_alarms(anchor, now)]}})
"""


def _namespace_rows(erp):
    return {
        "runs": [r for (r,) in _all(erp, "SELECT run_number FROM production_runs WHERE site = %s "
                                          "AND run_number ~ %s ORDER BY 1", (sh.SITE, sr.HISTORY_RUN_PATTERN))],
        "story_runs": _all(erp, "SELECT run_number FROM production_runs WHERE run_number ~ %s",
                           (sr.STORY_RUN_PATTERN,)),
        "work_orders": [w for (w,) in _all(erp, "SELECT work_order_number FROM mes_work_orders "
                                                "WHERE work_order_number ~ '^WO-[0-9]{8}-[89][0-9]{4}$' "
                                                "ORDER BY 1")],
        "orders": [o for (o,) in _all(erp, "SELECT order_number FROM customer_orders WHERE order_number ~ %s "
                                            "ORDER BY 1", (sr.HISTORY_ORDER_PATTERN,))],
        "shipments": [s for (s,) in _all(erp, "SELECT shipment_number FROM shipments "
                                               "WHERE shipment_number ~ '^SHIP-[0-9]{8}-9[0-9]{7}$' ORDER BY 1")],
    }


@pytest.mark.parametrize("loop,now_after", [
    (10800, 2 * sh.DAY + 1800),   # a different congruence: the anchor moves
    (18600, 3600),                # residue 10 like 8 h, anchor already in the last loop: target == stored
])
def test_pg_a_loop_change_replans_the_stored_anchor_with_no_old_era_row_left(pg, loop, now_after):
    """Written under 8 h (this process), re-planned by the Lambda's LoopSeconds
    Update under ``loop`` in a FRESH interpreter: one failed batch shipped to
    three customers, the stored geometry is the new loop's, and no row of the
    old era's namespace (runs, work orders, orders, shipments, story runs,
    CMMS condition orders) survives."""
    erp, seed = pg["erp"], pg["seed"]
    a1, old_story_run = _era(pg)
    now = a1 + now_after
    old_failed = sh.plan(a1).failed_run.run_number
    with erp.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = 'placed' WHERE config_key = %s", (sr.SOURCE_KEY,))
    erp.commit()
    out = run_under_loop(str(loop), _LOOP_CHANGE_CHILD.format(dsns=pg["dsn"], now=now, loop=loop))
    assert out["geometry"]["loop_seconds"] == loop
    anchor = float(out["data"]["AnchorEpoch"])
    assert out["data"]["AnchorSource"] == "placed" and out["data"]["FailedRunNumber"] == out["failed"]
    assert anchor % 300 == out["residue"] and now - loop < anchor <= now
    if loop == 18600:
        assert anchor == a1                                   # the idempotent branch is NOT taken
    config = _config(erp)
    geo = out["geometry"]
    assert {k: config[k] for k in sr.GEOMETRY_KEYS} == {
        "story_loop_seconds": str(loop), "story_interval_seconds": str(geo["interval_seconds"]),
        "story_bpfo_alarm_step": str(geo["bpfo_alarm_step"]), "story_fail_step": str(geo["fail_step"]),
        "story_repair_steps": str(geo["repair_steps"])}
    assert float(config["story_anchor_epoch"]) == anchor and config["story_failed_run_number"] == out["failed"]
    assert _all(erp, """
        SELECT DISTINCT pr.run_number FROM production_runs pr
        JOIN mes_work_orders w ON w.production_run_id = pr.run_id
        JOIN mes_quality_checks q ON q.work_order_id = w.work_order_id
        WHERE q.check_type = 'DIMENSIONAL' AND q.fail_count > 0""") == [(out["failed"],)]
    assert _all(erp, """
        SELECT count(DISTINCT c.customer_code) FROM shipment_items si
        JOIN shipments s ON s.shipment_id = si.shipment_id JOIN customers c ON c.customer_id = s.customer_id
        WHERE si.batch_id = %s""", (out["failed"],)) == [(3,)]
    assert _all(erp, "SELECT count(*) FROM mes_downtime_events") == [(1 + len(sh.PAST_STOPS),)]
    rows = _namespace_rows(erp)
    assert rows == {"runs": out["runs"], "story_runs": [], "work_orders": out["work_orders"],
                    "orders": out["orders"], "shipments": out["shipments"]}
    assert old_story_run not in rows["runs"] and (old_failed == out["failed"] or old_failed not in rows["runs"])
    assert [[w, str(d)] for w, d in _vib_orders(seed)] == out["alarms"]
    for wonum, reported in out["cmms"].items():
        assert _all(seed, "SELECT reported_date::text FROM cmms_indianapolis.work_orders WHERE wonum = %s",
                    (wonum,)) == [(reported,)]


def _pre_adr_simulator_rows(conn):
    """Random (pre-ADR-0090) simulator numbers that land in the story-owned
    9xxxx / 8xxxx / 9xxxxxxx spaces, each with an UNRELATED paired number."""
    with conn.cursor() as cur:
        for run, wo in (("RUN-20260101-91234", "WO-20260101-55555"), ("RUN-20260101-81234", "WO-20260101-66666")):
            cur.execute("""
                WITH run AS (
                    INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
                    SELECT %s, product_id, 10, 'COMPLETED', %s, %s FROM products WHERE sku = %s
                    RETURNING run_id, product_id)
                INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                    planned_quantity, status) SELECT %s, run_id, product_id, %s, %s, 10, 'COMPLETED' FROM run""",
                        (run, sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU, wo, sh.SITE, cm.STORY_MES_LINE))
        cur.execute("""
            WITH o AS (
                INSERT INTO customer_orders (order_number, customer_id, status, total_amount)
                SELECT 'ORD-20260101-95555555', customer_id, 'SHIPPED', 1 FROM customers
                WHERE customer_code = %s RETURNING order_id, customer_id)
            INSERT INTO shipments (shipment_number, order_id, customer_id, status)
            SELECT 'SHIP-20260101-91111111', order_id, customer_id, 'SHIPPED' FROM o""",
                    (sh.STORY_CUSTOMER_CODES[0],))
    conn.commit()


def test_pg_a_geometry_rewrite_never_retires_a_simulator_row(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    _simulator_rows(erp)
    _pre_adr_simulator_rows(erp)
    with erp.cursor() as cur:
        cur.execute("UPDATE simulation_config SET config_value = '10800' WHERE config_key = 'story_loop_seconds'")
    erp.commit()
    now = a1 + 2 * sh.DAY + 1800
    out = sr.replan(erp, pg["seed"], a1, now=now, source="deploy", expected_anchor=a1)
    assert out["new_anchor"] == sr.normalize_anchor(a1, now) and out["retired"]["runs"] == 19
    assert out["retired"]["story_runs"] == 1 and out["retired"]["orders"] == 21
    assert _config(erp)["story_loop_seconds"] == str(cm.TOTAL_SECONDS)
    assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = 'RUN-20260101-12345'") == [(1,)]
    assert _all(erp, "SELECT count(*) FROM shipments WHERE shipment_number = 'SHIP-20260101-55555555'") == [(1,)]
    for number in ("RUN-20260101-91234", "RUN-20260101-81234"):
        assert _all(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s", (number,)) == [(1,)]
    assert _all(erp, "SELECT count(*) FROM customer_orders WHERE order_number = 'ORD-20260101-95555555'") == [(1,)]


# ---------------------------------------------------------------------------
# Reset failsafe (ADR-0091 Design C): the default anchor the story-history CR
# planned onto, and ``reset`` onto it through the SAME replan.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("config,expected", [
    ({"story_default_anchor": "1789990010", "story_default_source": "deploy"}, (1789990010, "deploy")),
    ({"story_default_anchor": "1789990010.0", "story_default_source": "packaged"}, (1789990010, "packaged")),
    ({}, None),
    ({"story_default_anchor": "1789990010"}, None),                                  # no source
    ({"story_default_source": "deploy"}, None),                                      # no anchor
    ({"story_default_anchor": "1789990010", "story_default_source": "placed"}, None),  # never a default
    ({"story_default_anchor": "nan", "story_default_source": "deploy"}, None),
    ({"story_default_anchor": "-5", "story_default_source": "deploy"}, None),
    ({"story_default_anchor": "x", "story_default_source": "deploy"}, None),
])
def test_parse_default_is_null_safe(config, expected):
    assert sr.parse_default(config) == expected


def test_default_contract_constants():
    assert (sr.DEFAULT_ANCHOR_KEY, sr.DEFAULT_SOURCE_KEY) == sh.DEFAULT_KEYS
    assert sr.DEFAULT_SOURCES == ("deploy", "packaged")
    assert issubclass(sr.NoDefaultAnchor, RuntimeError)


def test_write_default_refuses_a_placed_source_before_touching_the_db():
    with pytest.raises(ValueError, match="source"):
        sr.write_default_anchor(object(), 1789990010, "placed")


def test_pg_default_anchor_round_trips_and_a_placement_never_moves_it(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    assert sr.read_default_anchor(erp) is None
    assert sr.write_default_anchor(erp, a1, "deploy") == 2
    assert sr.read_default_anchor(erp) == (int(a1), "deploy")
    assert sr.write_default_anchor(erp, a1, "deploy") == 0          # unchanged rows stay put
    now = a1 + 2 * sh.DAY + 1800
    _place(pg, a1, now, now + 3600)
    assert sr.read_default_anchor(erp) == (int(a1), "deploy")
    assert sr.write_default_anchor(erp, a1 + 300, "packaged") == 2
    assert sr.read_default_anchor(erp) == (int(a1) + 300, "packaged")
    assert erp.get_transaction_status() == 0                          # no transaction left open


def test_pg_reset_replans_onto_the_stored_default_with_its_source(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    sr.write_default_anchor(erp, a1, "deploy")
    now = a1 + 2 * sh.DAY + 1800
    a2 = _place(pg, a1, now, now + 3600)["new_anchor"]
    out = sr.reset(erp, pg["seed"], now=now + 60, expected_anchor=a2)
    target = sr.normalize_anchor(a1, now + 60)
    assert out["reset"] is True and (out["default_anchor"], out["default_source"]) == (int(a1), "deploy")
    assert (out["old_anchor"], out["new_anchor"], out["source"]) == (a2, target, "deploy")
    assert out["loops_shifted"] == 0 and out["alarm_at"] == target + cm.ALARM_OFFSET_SECONDS
    config = _config(erp)
    assert float(config["story_anchor_epoch"]) == target and config["story_anchor_source"] == "deploy"
    assert (config["story_default_anchor"], config["story_default_source"]) == (str(int(a1)), "deploy")


def test_pg_reset_is_cas_guarded(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    sr.write_default_anchor(erp, a1, "packaged")
    rows, config = _counts(erp), _config(erp)
    with pytest.raises(sr.StaleAnchor):
        sr.reset(erp, pg["seed"], now=a1 + 2 * sh.DAY, expected_anchor=a1 + 300)
    assert (_counts(erp), _config(erp)) == (rows, config)


def test_pg_reset_without_a_default_raises_and_changes_nothing(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    rows, config = _counts(erp), _config(erp)
    with pytest.raises(sr.NoDefaultAnchor):
        sr.reset(erp, pg["seed"], now=a1 + 2 * sh.DAY, expected_anchor=None)
    assert (_counts(erp), _config(erp)) == (rows, config)


def _story_lambda(pg, now: int):
    """The story-history Lambda module wired to the throwaway databases at ``now``."""
    from erp_simulation import pgdriver

    spec = importlib.util.spec_from_file_location("lsh_backfill", _REPO / "lambda_" / "lambda_story_history.py")
    lam = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lam)
    lam._connect = lambda env: pgdriver.connect(pg["dsn"]["erp" if env == "DB_SECRET_NAME" else "seed"])
    lam._now = lambda: now
    return lam


@pytest.mark.parametrize("placed", [False, True])
def test_pg_an_unchanged_update_backfills_the_default_so_reset_succeeds(pg, placed):
    """An account upgraded in place: the history was written by a code path that
    records no default (sh.write / replan only), the deploy's CR runs as an
    UNCHANGED Update, and the reset failsafe then works."""
    erp = pg["erp"]
    a1, _run = _era(pg)
    now = int(a1 + 2 * sh.DAY + 1800)
    stored = _place(pg, a1, now, now + 3600)["new_anchor"] if placed else a1
    assert sr.read_default_anchor(erp) is None
    lam = _story_lambda(pg, now)
    out = lam.lambda_handler({"RequestType": "Update", "PhysicalResourceId": "adt-story-history",
                              "ResourceProperties": {"Days": "3", "Anchor": ""},
                              "OldResourceProperties": {"Days": "3", "Anchor": ""}}, None)
    assert float(out["Data"]["AnchorEpoch"]) == stored            # the unchanged Update never moves the clock
    default = (cm.snap_anchor(now, "down"), "deploy") if placed else (int(a1), "deploy")
    assert sr.read_default_anchor(erp) == default
    report = lam.lambda_handler({"action": "reset", "expected_anchor": stored}, None)
    assert "error" not in report, report
    assert report["reset"] is True and (report["default_anchor"], report["default_source"]) == default
    target = sr.normalize_anchor(default[0], now)
    config = _config(erp)
    assert float(config["story_anchor_epoch"]) == target == report["new_anchor"]
    assert config["story_anchor_source"] == "deploy"

def test_pg_clear_story_config_also_clears_the_default(pg):
    erp = pg["erp"]
    a1, _run = _era(pg)
    sr.write_default_anchor(erp, a1, "deploy")
    sh.clear_story_config(erp)
    assert not set(sh.DEFAULT_KEYS) & set(_config(erp)) and sr.read_default_anchor(erp) is None


# ---------------------------------------------------------------------------
# Two clocks (ADR-0095): replan_live moves the alarm and leaves the history
# ---------------------------------------------------------------------------

def test_live_alarm_allowed_is_the_one_invariant():
    """The new live loop-0 alarm must fall AFTER the written history ends."""
    history = 1_790_000_000
    assert sr.live_alarm_allowed(history, history) is True                        # one clock
    assert sr.live_alarm_allowed(history, history + 60) is True
    assert sr.live_alarm_allowed(history, history - cm.ALARM_OFFSET_SECONDS + 1) is True
    assert sr.live_alarm_allowed(history, history - cm.ALARM_OFFSET_SECONDS) is False
    assert sr.live_alarm_allowed(history, history - 2 * cm.ALARM_OFFSET_SECONDS) is False


def test_live_clock_refused_is_a_value_error_and_stale_anchor_is_not():
    assert issubclass(sr.LiveClockRefused, ValueError)
    assert not issubclass(sr.StaleAnchor, ValueError)
    assert sr.LIVE_ANCHOR_KEY == sh.LIVE_ANCHOR_KEY == "story_live_anchor_epoch"


def _live_read(erp):
    story_live = pytest.importorskip("erp_simulation.story_live")
    value = story_live.read_anchor(erp)
    erp.rollback()
    return value


def _history_snapshot(erp, seed):
    """Everything the history owns: its ERP rows (counts), the 9xxxx run
    numbers and the history CMMS rows; a live move must leave all of it."""
    return (_counts(erp),
            _all(erp, "SELECT run_number FROM production_runs WHERE run_number ~ %s ORDER BY 1",
                 (sr.HISTORY_RUN_PATTERN,)),
            _all(seed, "SELECT wonum, status, reported_date FROM cmms_indianapolis.work_orders "
                       "WHERE wonum NOT LIKE 'VIB-CMX-%%' ORDER BY wonum"))


def test_pg_replan_live_moves_the_alarm_and_keeps_the_history_as_written(pg):
    """Loop-0 story run and VIB order exist on A1; a live move 10 minutes ahead
    retires only those, writes the live key, and the history is byte-identical."""
    erp, seed = pg["erp"], pg["seed"]
    story_live = pytest.importorskip("erp_simulation.story_live")
    a1 = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp() - 2 * sh.DAY, "down"))
    sh.write(erp, seed, sh.plan(a1))
    history_before = _history_snapshot(erp, seed)            # before any live-era row exists
    alarm = a1 + cm.ALARM_OFFSET_SECONDS + 60
    assert story_live.apply_cycle(erp, alarm, a1) == 1
    erp.commit()
    story_run = story_live.story_work_order(alarm, a1).run_number
    now = a1 + cm.ALARM_OFFSET_SECONDS + 3600            # an hour into the alarm phase
    with seed.cursor() as cur:
        assert sr._catch_up_condition_orders(cur, a1, now) == 1
    seed.commit()
    reference = sr.alarm_to_anchor(now + 600)
    out = sr.replan_live(erp, seed, reference, now=now, expected_live_anchor=a1)
    target = sr.normalize_anchor(reference, now)
    assert out["keep_history"] is True and out["one_clock"] is False
    assert out["history_anchor"] == int(a1) and out["old_live_anchor"] == a1 and out["new_live_anchor"] == target
    assert out["failed_run_number"] == sh.plan(a1).failed_run.run_number
    assert out["retired"] == {"story_runs": 1, "condition_orders": 1}
    assert out["written"]["config"] == 1 and out["written"]["catch_up"] == 1 and out["written"]["condition_orders"] == 0
    config = _config(erp)
    assert float(config["story_anchor_epoch"]) == a1, "the history anchor never moves on a live move"
    assert float(config[sr.LIVE_ANCHOR_KEY]) == target
    assert config["story_anchor_source"] == "deploy" and config["story_failed_run_number"] == out["failed_run_number"]
    assert float(config["story_telemetry_anchor"]) == target
    assert _live_read(erp) == target, "the live side now follows the live key"
    assert sh.read_stored_anchor(erp) == a1, "the history reader still sees the history anchor"
    assert _history_snapshot(erp, seed) == history_before
    assert _all(erp, "SELECT run_number FROM production_runs WHERE run_number = %s", (story_run,)) == []
    assert _vib_orders(seed) == []
    from erp_simulation import story_anchor_mirror as mirror
    assert mirror.read_catch_up(erp)[::3] == (int(a1), "deploy")
    assert mirror.read_feed_state(erp).source == "placed"


def test_pg_replan_live_refuses_an_alarm_inside_the_history_and_writes_nothing(pg):
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + 600
    before = (_config(erp), _counts(erp), _vib_orders(seed))
    with pytest.raises(sr.LiveClockRefused, match="inside the written history"):
        sr.replan_live(erp, seed, sr.alarm_to_anchor(a1 - 3600), now=now, expected_live_anchor=a1)
    assert (_config(erp), _counts(erp), _vib_orders(seed)) == before


def test_pg_replan_live_cas_is_on_the_live_anchor(pg):
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    out = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)
    live = out["new_live_anchor"]
    with pytest.raises(sr.StaleAnchor):
        sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1800), now=now, expected_live_anchor=a1)
    again = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1800), now=now, expected_live_anchor=live)
    assert again["old_live_anchor"] == live and float(_config(erp)[sr.LIVE_ANCHOR_KEY]) == again["new_live_anchor"]


def test_pg_replan_live_back_onto_the_history_anchor_is_one_clock_again(pg):
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)["new_live_anchor"]
    out = sr.replan_live(erp, seed, a1, now=now, expected_live_anchor=live)
    assert out["one_clock"] is True and out["new_live_anchor"] == int(a1)
    assert sr.LIVE_ANCHOR_KEY not in _config(erp) and _live_read(erp) == a1
    assert out["written"]["condition_orders"] == 1, "A1's loop-0 alarm has passed: its VIB order is raised again"


def _held(erp, now) -> bool:
    """Would a policy package's anchor mirror hold the feed (publish nothing)?"""
    from erp_simulation import story_anchor_mirror as mirror
    feed = mirror.read_feed_state(erp)
    value = mirror.mirror_value("present", _live_read(erp), feed.catch_up, now, feed.source, hold=True)
    return value.endswith(mirror.HELD_SUFFIX)


def test_pg_replan_live_back_onto_a_default_history_records_it_placed(pg):
    """The cleared live key was the only record of a placement: without the
    ``placed`` source a policy package would hold the feed again and the next
    page load would auto-place over the history."""
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    assert _config(erp)["story_anchor_source"] == "deploy" and _held(erp, now)
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)
    assert live["written"]["source"] == 0 and _config(erp)["story_anchor_source"] == "deploy"
    assert not _held(erp, now), "a stored live key is a placement for the feed"
    out = sr.replan_live(erp, seed, a1, now=now, expected_live_anchor=live["new_live_anchor"])
    assert out["one_clock"] is True and out["written"]["source"] == 1
    config = _config(erp)
    assert sr.LIVE_ANCHOR_KEY not in config and config["story_anchor_source"] == "placed"
    assert float(config["story_anchor_epoch"]) == a1, "the history anchor never moves on a live move"
    assert not _held(erp, now), "the feed stays on the placed clock"


def test_pg_replan_live_back_onto_a_placed_history_writes_no_source(pg):
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + 2 * sh.DAY + 1800
    a2 = _place(pg, a1, now, now + 3600)["new_anchor"]
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 7200), now=now, expected_live_anchor=a2)
    out = sr.replan_live(erp, seed, a2, now=now, expected_live_anchor=live["new_live_anchor"])
    assert out["one_clock"] is True and out["written"]["source"] == 0
    assert _config(erp)["story_anchor_source"] == "placed"


@pytest.mark.parametrize("stored", [None, "manual"])
def test_pg_replan_live_back_onto_an_absent_or_unknown_source_records_it_placed(pg, stored):
    """A policy mirror leaves the parameter alone for an absent or unknown
    source, so without ``placed`` the feed would stay on the old live anchor."""
    from erp_simulation import story_anchor_mirror as mirror
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    with erp.cursor() as cur:
        if stored is None:
            cur.execute("DELETE FROM simulation_config WHERE config_key = 'story_anchor_source'")
        else:
            cur.execute("UPDATE simulation_config SET config_value = %s WHERE config_key = 'story_anchor_source'",
                        (stored,))
    erp.commit()
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)
    out = sr.replan_live(erp, seed, a1, now=now, expected_live_anchor=live["new_live_anchor"])
    assert out["one_clock"] is True and out["written"]["source"] == 1
    assert _config(erp)["story_anchor_source"] == "placed"
    feed = mirror.read_feed_state(erp)
    value = mirror.mirror_value("present", _live_read(erp), feed.catch_up, now, feed.source, hold=True)
    assert value is not None and value.split(";")[0] == str(int(a1)), "the feed follows the history anchor"


def test_pg_reset_onto_the_stored_anchor_restores_the_default_source(pg):
    """A placement onto the stored anchor keeps the stored source; the reset
    writes the default one, so it always leaves an unplaced clock."""
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    sr.write_default_anchor(erp, a1, "packaged")
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)
    sr.replan_live(erp, seed, a1, now=now, expected_live_anchor=live["new_live_anchor"])
    same = sr.replan(erp, seed, a1, now=now, source="packaged", expected_anchor=a1)
    assert same["source"] == "placed" and same["written"]["config"] == 0, "a placement keeps the stored source"
    rows = _counts(erp)
    out = sr.reset(erp, seed, now=now, expected_anchor=a1)
    assert (out["old_anchor"], out["new_anchor"], out["source"]) == (a1, int(a1), "packaged")
    assert out["written"]["config"] == 1 and _counts(erp) == rows, "only the source row changes"
    config = _config(erp)
    assert config["story_anchor_source"] == "packaged" and float(config["story_anchor_epoch"]) == a1
    assert _held(erp, now), "a reset leaves an unplaced clock: a policy package holds the feed again"


def test_pg_a_full_replan_clears_the_live_key_and_catches_up_from_the_live_anchor(pg):
    erp, seed = pg["erp"], pg["seed"]
    a1, _run = _era(pg)
    now = a1 + cm.ALARM_OFFSET_SECONDS + 600
    live = sr.replan_live(erp, seed, sr.alarm_to_anchor(now + 1200), now=now, expected_live_anchor=a1)["new_live_anchor"]
    a2 = sr.normalize_anchor(a1 + 3600, now)
    out = sr.replan(erp, seed, a2, now=now, source="placed", expected_anchor=a1)
    assert out["written"]["live_anchor_cleared"] == 1
    config = _config(erp)
    assert sr.LIVE_ANCHOR_KEY not in config and float(config["story_anchor_epoch"]) == a2
    assert _live_read(erp) == a2
    from erp_simulation import story_anchor_mirror as mirror
    assert mirror.read_catch_up(erp)[::3] == (int(live), "placed"), "the feed climbs from the anchor it followed"


def test_pg_replan_live_without_a_story_clock_is_refused(pg):
    erp, seed = pg["erp"], pg["seed"]
    with pytest.raises(sr.LiveClockRefused, match="no story clock"):
        sr.replan_live(erp, seed, 1_790_000_000, now=1_790_000_100)


# ---------------------------------------------------------------------------
# The one-time history replay across a Reset (the replay ADR): a Reset ends
# the replay, so the next placement records a fresh window with a fresh
# identity and the server replays it from point 0.
# ---------------------------------------------------------------------------

class _ReplaySsm:
    def __init__(self):
        self.value = "pending"

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.value = Value


class _OneConnDb:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        import contextlib
        return contextlib.nullcontext(self.conn)


def _mirror_cycle(erp, ssm, now):
    """One simulation cycle of the replay mirror: the stored window -> the
    parameter, recording the identity once (the orchestrator's own code)."""
    from erp_simulation import story_anchor_mirror as mirror
    from erp_simulation.simulation_orchestrator import SimulationOrchestrator
    fact, since = mirror.read_backfill_state(erp)
    outcome = mirror.sync_backfill(fact, since, now, lambda: ssm)
    _value, record = mirror.backfill_value(fact, since, now)
    if outcome == "written" and record is not None and since is None:
        holder = type("Holder", (), {"db": _OneConnDb(erp)})()
        SimulationOrchestrator._persist_backfill_live_since(holder, record)
    return outcome


def _first_served(value, now):
    """What the story server serves on its history nodes once it first sees
    ``value`` with SFC subscribed: ``(index, gridEpoch)`` or None. A pass opens
    with one pace step of Bad history nodes, so point 0 goes out one pace after
    pickup; read it there."""
    import json as _json
    import subprocess as _sp
    clock = (_REPO / "highbyte" / "components" / "opc-server" / "clock.mjs").as_uri()
    opts = f"{{loopSeconds: {cm.TOTAL_SECONDS}, dataSize: {cm.NUM_STEPS}}}"
    script = (f"import * as c from {_json.dumps(clock)};\n"
              f"const s = c.replayOnFact(c.REPLAY_IDLE, c.parseBackfill({_json.dumps(value)}), {now * 1000}, "
              f"true, {opts}); const p = c.replayPoint(s, {now * 1000} + c.BACKFILL_PACE_MS, {opts});\n"
              f"console.log(JSON.stringify(p && [p.index, p.gridEpoch, s.order.find((e) => e !== null)]));")
    out = _sp.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                  timeout=60, check=True)  # nosec B603 B607 - fixed argv, test-only
    return _json.loads(out.stdout)


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node is not installed")
def test_pg_a_reset_ends_the_replay_and_the_next_placement_replays_afresh(pg, monkeypatch):
    from erp_simulation import story_anchor_mirror as mirror
    monkeypatch.setenv(mirror.BACKFILL_PARAMETER_ENV, "/adt/workshop/story-feed-backfill")
    erp, ssm = pg["erp"], _ReplaySsm()
    a1, _run = _era(pg)
    sr.write_default_anchor(erp, a1, "deploy")
    now = a1 + 2 * sh.DAY + 1800
    a2 = _place(pg, a1, now, now + 3600)["new_anchor"]          # out of the HOLD: a window
    assert _mirror_cycle(erp, ssm, now + 60) == "written"
    first = ssm.value
    assert first.endswith(f";{int(now + 60)}") and _config(erp)[sh.LIVE_SINCE_KEY] == str(int(now + 60))
    ssm.value = first + ";done"                                 # the server finished it
    assert _mirror_cycle(erp, ssm, now + 3600) == "same" and ssm.value == first + ";done"

    reset = sr.reset(erp, pg["seed"], now=now + 4000, expected_anchor=a2)
    assert reset["written"]["backfill_cleared"] == 2
    config = _config(erp)
    assert sh.BACKFILL_KEY not in config and sh.LIVE_SINCE_KEY not in config
    assert _mirror_cycle(erp, ssm, now + 4060) == "written" and ssm.value == "pending"

    later = now + 5000
    _place(pg, reset["new_anchor"], later, later + 3600)       # out of the HOLD again
    assert _mirror_cycle(erp, ssm, later + 60) == "written"
    second = ssm.value
    assert second != first and second.endswith(f";{int(later + 60)}"), "a fresh window and identity"
    index, epoch, first_in_order = _first_served(second, int(later + 90))
    assert index == 0 and epoch == first_in_order, "the server replays the new window from point 0"
