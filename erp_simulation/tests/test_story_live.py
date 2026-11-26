# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live workshop story: the per-cycle simulator step (erp_simulation/story_live.py)
and its orchestrator wiring (ADR-0085).

Offline tests use recording fakes. The PostgreSQL acceptance (skipped unless
ERP_SIM_TEST_PG_DSN is set) builds the real ERP schema with lambda-db-init,
puts a RUNNING story-line work order at the condition-monitor site, runs
``apply_cycle`` at a BPFO-alarm instant and then the MES completion path, and
sees ``reject_quantity > 0``.

    python -m pytest erp_simulation/tests/test_story_live.py -q
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
import time
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import mes_simulator as mes_mod
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import story_history, story_live
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.tests._pg_sql import stmt

_SITE = MESSimulator.CONDITION_MONITOR_SITE
_LINE = cm.STORY_MES_LINE
_ANCHOR = 1_900_000_123.0  # arbitrary positive epoch, not loop-aligned (nor congruent)
_CONGRUENT = float(cm.snap_anchor(_ANCHOR))  # the one-clock anchor: its alarm is a grid point
_SLOT = story_live.DEFECT_CHECK_INTERVAL_MINUTES * 60


def _at_step(step: int, anchor: float = _ANCHOR) -> float:
    return anchor + step * cm.INTERVAL_SEC + 1.0


def _conn(fetchone=None, rowcount=1):
    conn = MagicMock(name="conn")
    cursor = MagicMock(name="cursor")
    cursor.fetchone.return_value = fetchone
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


def _live_conn(stored_anchor: float = _ANCHOR, rowcount=1):
    """A fake connection whose (re-read) stored anchor is ``stored_anchor``."""
    return _conn(fetchone=(repr(float(stored_anchor)),), rowcount=rowcount)


# --------------------------------------------------------------------------
# read_anchor
# --------------------------------------------------------------------------

def test_read_anchor_is_none_when_the_key_is_absent():
    conn, cursor = _conn(fetchone=None)
    assert story_live.read_anchor(conn) is None
    sql, params = cursor.execute.call_args.args
    assert "simulation_config" in sql
    # ONE statement reads both clocks, the live key first (ADR-0095).
    assert params == ([story_live.LIVE_ANCHOR_KEY, story_live.ANCHOR_KEY], story_live.LIVE_ANCHOR_KEY)
    assert "ORDER BY config_key = %s DESC LIMIT 1" in sql
    cursor.close.assert_called_once()


def test_live_anchor_key_matches_the_history_contract():
    assert story_live.LIVE_ANCHOR_KEY == "story_live_anchor_epoch" == story_history.LIVE_ANCHOR_KEY


def test_era_is_current_rereads_the_live_anchor_after_the_shared_lock():
    conn, cursor = _live_conn(_ANCHOR)
    assert story_live.era_is_current(cursor, _ANCHOR) is True
    lock, read = cursor.execute.call_args_list[-2:]
    assert "pg_advisory_xact_lock_shared" in lock.args[0]
    assert read.args[1] == ([story_live.LIVE_ANCHOR_KEY, story_live.ANCHOR_KEY], story_live.LIVE_ANCHOR_KEY)


def test_read_anchor_returns_the_stored_epoch():
    conn, _ = _conn(fetchone=(str(_ANCHOR),))
    assert story_live.read_anchor(conn) == _ANCHOR


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nan", "inf", None])
def test_read_anchor_ignores_unusable_values(raw):
    conn, _ = _conn(fetchone=(raw,))
    assert story_live.read_anchor(conn) is None


def test_read_anchor_warns_once_per_bad_value(monkeypatch, caplog):
    """Lambda re-reads the anchor every minute: one warning per bad VALUE,
    not one per cycle; a different bad value warns again."""
    monkeypatch.setattr(story_live, "_last_warned_value", story_live._NOT_WARNED)
    with caplog.at_level("WARNING"):
        for raw in ("abc", "abc", "abc", "-5", "-5", "abc"):
            assert story_live.read_anchor(_conn(fetchone=(raw,))[0]) is None
    warned = [r.getMessage() for r in caplog.records if story_live.ANCHOR_KEY in r.getMessage()]
    assert len(warned) == 3, warned
    assert "'abc'" in warned[0] and "'-5'" in warned[1] and "'abc'" in warned[2]


def test_anchor_key_matches_the_history_contract():
    assert story_live.ANCHOR_KEY == "story_anchor_epoch"


# --------------------------------------------------------------------------
# defect_check_row (pure)
# --------------------------------------------------------------------------

def test_defect_row_has_no_failures_outside_the_window():
    for step in (0, cm.BPFO_ALARM_STEP - 1, cm.BEARING_FAIL_STEP, cm.NUM_STEPS - 1):
        sample, passed, failed, _notes = story_live.defect_check_row(step, 0.5)
        assert (sample, passed, failed) == (20, 20, 0)


def test_defect_row_fails_inside_the_window_and_climbs():
    fails = [story_live.defect_check_row(s, 1.0)[2]
             for s in range(cm.BPFO_ALARM_STEP, cm.BEARING_FAIL_STEP)]
    assert fails[0] == round(20 * cm.DEFECT_REJECT_MIN) > 0
    assert fails[-1] == round(20 * cm.DEFECT_REJECT_MAX)
    assert fails == sorted(fails)
    sample, passed, failed, _ = story_live.defect_check_row(cm.BPFO_ALARM_STEP, 1.0)
    assert passed + failed == sample == 20


def test_defect_row_notes():
    notes = story_live.defect_check_row(cm.BPFO_ALARM_STEP, 0.7123)[3]
    assert notes == f"SPC bore diameter drift on {cm.STORY_MES_EQUIPMENT_ID}: BPFO band 0.71 mm/s"


# --------------------------------------------------------------------------
# defect_check_slot (pure, ADR-0088 D6): alarm-aligned 15-min slots
# --------------------------------------------------------------------------

def test_defect_check_slot_is_none_outside_the_alarm_window():
    for step in (0, cm.BPFO_ALARM_STEP - 1, cm.BEARING_FAIL_STEP, cm.NUM_STEPS - 1):
        assert story_live.defect_check_slot(_at_step(step, _CONGRUENT), _CONGRUENT) is None


def test_defect_check_slot_is_the_latest_alarm_aligned_slot():
    alarm = cm.alarm_epoch(_CONGRUENT, _CONGRUENT)
    fail = cm.fail_epoch(_CONGRUENT, _CONGRUENT)
    assert story_live.defect_check_slot(alarm, _CONGRUENT) == alarm
    assert story_live.defect_check_slot(alarm + _SLOT - 1, _CONGRUENT) == alarm
    assert story_live.defect_check_slot(alarm + _SLOT, _CONGRUENT) == alarm + _SLOT
    slots = {story_live.defect_check_slot(t, _CONGRUENT) for t in range(int(alarm), int(fail), 60)}
    assert sorted(slots) == [alarm + _SLOT * n for n in range(8)]  # exactly 8 per window
    assert story_live.DEFECT_CHECKS_PER_WINDOW == 8


def test_every_check_slot_is_a_published_telemetry_point():
    """With a congruent anchor each slot is on the 5-min SiteWise grid, so a
    check's BPFO is exactly a published ``Spectrum_BPFO`` value."""
    alarm = cm.alarm_epoch(_CONGRUENT, _CONGRUENT)
    for n in range(8):
        slot = alarm + _SLOT * n
        assert slot % cm.TELEMETRY_GRID_SECONDS == 0
        assert story_live.defect_check_slot(slot + 59, _CONGRUENT) == slot


def test_defect_check_slot_follows_the_loop():
    later = _CONGRUENT + 3 * cm.TOTAL_SECONDS
    alarm = cm.alarm_epoch(later, _CONGRUENT)
    assert story_live.defect_check_slot(alarm + 2 * _SLOT + 5, _CONGRUENT) == alarm + 2 * _SLOT


# --------------------------------------------------------------------------
# apply_cycle (fake connection)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("step", [0, cm.BPFO_ALARM_STEP - 1, cm.BEARING_FAIL_STEP,
                                  cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS])
def test_apply_cycle_is_a_noop_outside_the_window(step):
    conn, _ = _conn()
    assert story_live.apply_cycle(conn, _at_step(step), _ANCHOR) == 0
    conn.cursor.assert_not_called()


def test_apply_cycle_inserts_one_guarded_check_for_the_story_line():
    conn, cursor = _live_conn(rowcount=2)
    now = _at_step(cm.BPFO_ALARM_STEP)
    assert story_live.apply_cycle(conn, now, _ANCHOR) == 2
    sql, params = cursor.execute.call_args.args
    assert "INSERT INTO mes_quality_checks" in sql and "NOT EXISTS" in sql
    assert "mwo.status = 'RUNNING'" in sql
    assert "JOIN products p ON p.product_id = mwo.product_id" in sql and "p.sku = %s" in sql
    assert "mwo.actual_start_time <= %s" in sql  # a slot before the order started is skipped
    assert "q.check_time >= %s" in sql  # one check per slot, not once per work order
    (check_time, check_type, sample, passed, failed, inspector, notes,
     site, line, sku, started_by, guard_type, guard_slot) = params
    slot = datetime.fromtimestamp(cm.alarm_epoch(now, _ANCHOR))  # the first slot IS the alarm
    assert check_time == started_by == guard_slot == slot
    assert check_type == guard_type == "DIMENSIONAL"
    assert (site, line) == (_SITE, _LINE)
    assert sku == story_live.STORY_SKU  # never an order of another product
    assert inspector == "QC-SPC" and sample == 20 and failed > 0
    assert notes.startswith("SPC bore diameter drift")
    cursor.close.assert_called_once()


def test_apply_cycle_stamps_the_slot_and_its_bpfo_not_the_cycle():
    """A cycle 100 s past slot 1 writes the check AT slot 1 with the fail
    count and the condition model's BPFO band at the slot's step."""
    conn, cursor = _live_conn(_CONGRUENT)
    slot = cm.alarm_epoch(_CONGRUENT, _CONGRUENT) + _SLOT
    assert story_live.apply_cycle(conn, slot + 100.0, _CONGRUENT) == 1
    params = cursor.execute.call_args.args[1]
    step = cm.loop_step(cm.story_seconds(slot, _CONGRUENT))
    bpfo = cm.spectral_features(step)["bpfo"]
    sample, passed, failed, notes = story_live.defect_check_row(step, bpfo)
    assert params[:7] == (datetime.fromtimestamp(slot), "DIMENSIONAL", sample, passed, failed,
                          "QC-SPC", notes)
    assert notes.endswith(f"BPFO band {bpfo:.2f} mm/s")
    assert params[10] == params[12] == datetime.fromtimestamp(slot)


def test_apply_cycle_first_ensures_the_story_work_order_then_adds_the_check():
    """Line 3 is idle most of the time: the step starts the story run (one
    guarded statement) BEFORE the check insert, on the same cursor."""
    conn, cursor = _live_conn()
    now = _at_step(cm.BPFO_ALARM_STEP)
    assert story_live.apply_cycle(conn, now, _ANCHOR) == 1
    (lock_sql, _l), (_read, _r), (start_sql, start_params), (check_sql, _) = [
        c.args for c in cursor.execute.call_args_list]
    assert "pg_advisory_xact_lock_shared" in lock_sql
    assert "INSERT INTO production_runs" in start_sql and "INSERT INTO mes_work_orders" in start_sql
    assert "INSERT INTO mes_production_events" in start_sql and "'START'" in start_sql
    assert "'IN_PROGRESS'" in start_sql and "'RUNNING'" in start_sql
    assert start_sql.count("NOT EXISTS") == 2 and "ON CONFLICT (run_number) DO NOTHING" in start_sql
    assert "INSERT INTO mes_quality_checks" in check_sql
    wo = story_live.story_work_order(now, _ANCHOR)
    assert start_params == {
        "run_number": wo.run_number, "work_order_number": wo.work_order_number,
        "sku": story_live.STORY_SKU, "quantity": wo.planned_quantity, "rate": wo.target_rate,
        "start": wo.start, "planned_end": wo.planned_end, "operator": wo.operator_id,
        "fg_location": story_live.STORY_FG_LOCATION, "site": _SITE, "line": _LINE,
        "notes": f"Production batch for {story_live.STORY_SKU}",
    }
    cursor.close.assert_called_once()


def test_apply_cycle_logs_a_started_story_work_order(caplog):
    conn, _ = _live_conn()
    now = _at_step(cm.BPFO_ALARM_STEP)
    with caplog.at_level("INFO"):
        story_live.apply_cycle(conn, now, _ANCHOR)
    number = story_live.story_work_order(now, _ANCHOR).work_order_number
    assert any(f"started {number}" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# story_work_order (pure)
# --------------------------------------------------------------------------

def _loop_epoch(step: int, loop: int = 0) -> float:
    return _ANCHOR + loop * cm.TOTAL_SECONDS + step * cm.INTERVAL_SEC


def test_story_work_order_is_one_per_loop_across_the_whole_window():
    """Every cycle of one loop's alarm window names the SAME run / work order
    (the natural keys that make the start idempotent); the next loop's differ."""
    steps = (cm.BPFO_ALARM_STEP, cm.BPFO_ALARM_STEP + 700, cm.BEARING_FAIL_STEP - 1)
    plans = [story_live.story_work_order(_loop_epoch(s) + 1.0, _ANCHOR) for s in steps]
    assert len({(p.run_number, p.work_order_number) for p in plans}) == 1
    later = story_live.story_work_order(_loop_epoch(cm.BPFO_ALARM_STEP, loop=1) + 1.0, _ANCHOR)
    assert later.work_order_number != plans[0].work_order_number


def test_story_work_order_numbers_look_like_the_simulators():
    """``WO-<YYYYMMDD>-<5 digits>`` / ``RUN-<same>`` (utils.generate_*), dated on
    the loop's alarm instant, suffix ``8`` + loop (the history uses ``9xxxx``)."""
    import re
    wo = story_live.story_work_order(_loop_epoch(cm.BPFO_ALARM_STEP, loop=3) + 1.0, _ANCHOR)
    alarm_day = datetime.fromtimestamp(_loop_epoch(cm.BPFO_ALARM_STEP, loop=3)).strftime("%Y%m%d")
    assert re.fullmatch(r"WO-\d{8}-\d{5}", wo.work_order_number)
    assert wo.work_order_number == f"WO-{alarm_day}-80003"
    assert wo.run_number == f"RUN-{alarm_day}-80003"
    assert re.fullmatch(r"OP-[1-5]", wo.operator_id)


def test_story_run_starts_at_the_current_grid_slot():
    """Plan D6: the run starts at the current 5-min grid slot, which on the
    first alarm cycle IS the alarm instant (the first check slot)."""
    alarm = cm.alarm_epoch(_CONGRUENT, _CONGRUENT)
    assert story_live.story_work_order(alarm + 24.0, _CONGRUENT).start == datetime.fromtimestamp(alarm)
    late = story_live.story_work_order(alarm + 1000.0, _CONGRUENT)
    assert late.start == datetime.fromtimestamp(cm.grid_floor(alarm + 1000.0)) == datetime.fromtimestamp(alarm + 900)


def test_story_run_on_a_legacy_anchor_uses_the_alarm_aligned_grid():
    """A non-congruent (pre-one-clock) anchor puts the alarm off the epoch
    grid; the run still starts AT the alarm on the first alarm cycle (never
    at the grid point before it, nor at one between the alarm and the cycle,
    which would skip check 0), and on 5-min steps from the alarm after."""
    alarm = cm.alarm_epoch(_ANCHOR, _ANCHOR)
    assert alarm % cm.TELEMETRY_GRID_SECONDS != 0
    boundary = cm.grid_floor(alarm) + cm.TELEMETRY_GRID_SECONDS  # an epoch grid point after the alarm
    for now in (alarm + 1.0, boundary + 1.0, alarm + 299.0):
        assert story_live.story_work_order(now, _ANCHOR).start == datetime.fromtimestamp(alarm)
    assert story_live.story_work_order(alarm + 1000.0, _ANCHOR).start == datetime.fromtimestamp(alarm + 900)


def test_story_work_order_runs_until_the_bearing_fails():
    """Starts now, planned to end at the loop's bearing failure (so the line
    stays RUNNING through the whole alarm window), for the whole window.

    ``target_rate_per_minute`` is an INTEGER column, so the history's 60 per
    90 min (0.67/min) cannot be stored; rounding it up to 1/min while keeping
    the 0.67/min quantity would make the completion step's OEE performance
    (actual / (rate x minutes)) read ~65 % for a healthy run. The run is
    planned at an honest 1/min instead: one unit per alarm-window minute."""
    now = _loop_epoch(cm.BPFO_ALARM_STEP) + 1.0
    wo = story_live.story_work_order(now, _ANCHOR)
    fail = _loop_epoch(cm.BEARING_FAIL_STEP)
    assert wo.start == datetime.fromtimestamp(_loop_epoch(cm.BPFO_ALARM_STEP))
    assert wo.planned_end == datetime.fromtimestamp(fail)
    assert wo.target_rate == story_live.STORY_RATE_PER_MINUTE == 1
    alarm_minutes = (cm.BEARING_FAIL_STEP - cm.BPFO_ALARM_STEP) * cm.INTERVAL_SEC / 60
    assert wo.planned_quantity == story_live.STORY_PLANNED_QUANTITY == math.floor(alarm_minutes) == 107


def test_story_work_order_quantity_is_the_whole_alarm_whenever_it_starts():
    """ONE number for the page: the run is planned for the whole alarm window
    (107 min x 1/min) whether it starts on the alarm step, 24 s late (the
    live walk's 106) or late in the window; only ``planned_end`` is fixed
    at the bearing failure."""
    fail = datetime.fromtimestamp(_loop_epoch(cm.BEARING_FAIL_STEP))
    for now in (_loop_epoch(cm.BPFO_ALARM_STEP), _loop_epoch(cm.BPFO_ALARM_STEP) + 24.0,
                _loop_epoch(cm.BEARING_FAIL_STEP - 1) + 4.0):
        wo = story_live.story_work_order(now, _ANCHOR)
        assert (wo.planned_quantity, wo.target_rate, wo.planned_end) == (107, 1, fail)


def test_story_work_order_performance_is_honest_on_completion():
    """The simulator's completion step scores performance as actual quantity
    over target rate x run minutes; a full-length story run at 95-100 % of
    plan must read ~95-100 %, not the ~65 % a rounded-up rate would give."""
    now = _loop_epoch(cm.BPFO_ALARM_STEP) + 1.0
    wo = story_live.story_work_order(now, _ANCHOR)
    minutes = (wo.planned_end - wo.start).total_seconds() / 60
    for share in (0.95, 1.0):
        _a, perf, _q, _oee = mes_mod.compute_oee_components(
            minutes, 0, int(wo.planned_quantity * share), wo.target_rate, 20, 20)
        assert share - 0.02 <= perf <= 1.0


def test_story_product_is_the_history_failed_part():
    from erp_simulation import story_history as sh
    assert story_live.STORY_SKU == sh.FAILED_SKU
    assert story_live.STORY_FG_LOCATION == sh.FG_LOCATION


def test_apply_cycle_uses_the_anchor_not_the_wall_clock():
    """The same instant is in the window for one anchor and not for another."""
    now = _at_step(cm.BPFO_ALARM_STEP)
    conn, _ = _live_conn()
    assert story_live.apply_cycle(conn, now, _ANCHOR) == 1
    shifted, _ = _live_conn(_ANCHOR + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC)
    assert story_live.apply_cycle(shifted, now, _ANCHOR + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC) == 0


def test_apply_cycle_reports_zero_when_nothing_new():
    conn, _ = _live_conn(rowcount=0)
    assert story_live.apply_cycle(conn, _at_step(cm.BPFO_ALARM_STEP + 5), _ANCHOR) == 0


def test_apply_cycle_never_raises_on_a_failing_cursor(caplog):
    conn, cursor = _conn()
    cursor.execute.side_effect = RuntimeError("relation does not exist")
    with caplog.at_level("WARNING"):
        assert story_live.apply_cycle(conn, _at_step(cm.BPFO_ALARM_STEP), _ANCHOR) == 0
    conn.rollback.assert_called_once()
    assert any("Live-story defect step FAILED" in r.message for r in caplog.records)


def test_apply_cycle_survives_a_failing_rollback():
    conn, cursor = _conn()
    cursor.execute.side_effect = RuntimeError("down")
    conn.rollback.side_effect = RuntimeError("still down")
    assert story_live.apply_cycle(conn, _at_step(cm.BPFO_ALARM_STEP), _ANCHOR) == 0


def test_apply_cycle_takes_the_shared_story_lock_then_rereads_the_anchor():
    """ADR-0089: the SHARED story-clock lock (story_replan.replan holds the
    exclusive one) comes first, then the anchor is re-read in a SEPARATE
    statement (its READ COMMITTED snapshot is taken after the lock)."""
    from erp_simulation import story_replan
    conn, cursor = _live_conn()
    assert story_live.apply_cycle(conn, _at_step(cm.BPFO_ALARM_STEP), _ANCHOR) == 1
    (lock_sql, lock_params), (read_sql, read_params) = [c.args for c in cursor.execute.call_args_list[:2]]
    assert lock_sql == "SELECT pg_advisory_xact_lock_shared(%s)" and lock_params == (story_replan.LOCK_KEY,)
    assert "simulation_config" in read_sql
    assert read_params == ([story_live.LIVE_ANCHOR_KEY, story_live.ANCHOR_KEY], story_live.LIVE_ANCHOR_KEY)


@pytest.mark.parametrize("stored", [(repr(_ANCHOR + 300.0),), None, ("garbage",)],
                         ids=["moved", "cleared", "malformed"])
def test_apply_cycle_skips_the_cycle_when_the_anchor_moved(caplog, stored):
    """A placement between the cycle's anchor load and this step: nothing of
    the old era is written (no story run, no check), one log line, 0."""
    conn, cursor = _conn(fetchone=stored)
    with caplog.at_level("INFO"):
        assert story_live.apply_cycle(conn, _at_step(cm.BPFO_ALARM_STEP), _ANCHOR) == 0
    sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert len(sqls) == 2 and not any("INSERT" in sql for sql in sqls)
    conn.rollback.assert_called_once()
    moved = [r for r in caplog.records if "story clock moved" in r.getMessage()]
    assert len(moved) == 1


# --------------------------------------------------------------------------
# Orchestrator wiring (no config / DB: built with __new__ and fakes)
# --------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, conn, fail=None):
        self.conn, self.fail, self.opened = conn, fail, 0

    @contextmanager
    def connection(self):
        self.opened += 1
        if self.fail is not None:
            raise self.fail
        yield self.conn


def _no_transaction_context():
    raise AssertionError("the story clock/step must not use the retrying _transaction_context")


def _orchestrator(conn, fail=None):
    """A FRESH orchestrator, as the Lambda builds one per invocation."""
    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db = _FakeDb(conn, fail)
    orch.cycle_count = 0
    orch._story_anchor = None
    orch._story_clock = orch_mod.STORY_CLOCK_ABSENT
    orch.mes_simulator = types.SimpleNamespace(condition_anchor_epoch=0.0,
                                               run_condition_monitor=lambda cycle_id: 0)
    orch._run_simulation_cycle = MagicMock(name="_run_simulation_cycle")
    orch._transaction_context = _no_transaction_context
    return orch


def test_run_single_cycle_without_anchor_runs_no_story_sql(monkeypatch):
    conn, cursor = _conn(fetchone=None)
    orch = _orchestrator(conn)
    apply = MagicMock(name="apply_cycle")
    monkeypatch.setattr(story_live, "apply_cycle", apply)

    assert orch.run_single_cycle() == {"success": True, "cycle_count": 0, "simulator": "ran"}

    orch._run_simulation_cycle.assert_called_once_with()
    apply.assert_not_called()
    sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert len(sqls) == 1 and "simulation_config" in sqls[0]  # the anchor read only
    assert orch._story_clock == orch_mod.STORY_CLOCK_ABSENT
    assert orch.mes_simulator.condition_anchor_epoch == 0.0  # wall-clock monitor (demo/frozen)


def test_the_monitor_gets_a_fresh_stored_anchor_reader():
    """The monitor confirms the era with a FRESH read (own connection), not
    the anchor the cycle cached (ADR-0089)."""
    conn, cursor = _conn(fetchone=(str(_ANCHOR),))
    orch = _orchestrator(conn)
    orch._load_story_clock()
    opened = orch.db.opened
    cursor.fetchone.return_value = (repr(_ANCHOR + 300.0),)   # a placement since the load
    assert orch.mes_simulator.condition_anchor_epoch == _ANCHOR
    assert orch.mes_simulator.condition_anchor_reader() == _ANCHOR + 300.0
    assert orch.db.opened == opened + 1


@pytest.mark.parametrize("stored", [(str(_ANCHOR),), None], ids=["story-clock", "wall-clock"])
def test_the_monitor_gets_a_fresh_erp_downtime_reader(stored):
    """On either clock the monitor reads the MES downtime its bridge orders
    were raised from with a FRESH read (own connection), by downtime id."""
    conn, cursor = _conn(fetchone=stored)
    orch = _orchestrator(conn)
    orch._load_story_clock()
    opened = orch.db.opened
    start, end = datetime(2026, 10, 3, 5, 15), datetime(2026, 10, 3, 5, 25)
    ident = "0822d3e2-1f2e-4a5b-8c6d-000000000001"
    cursor.fetchall.return_value = [(ident, start, end, "maint_02")]
    assert orch.mes_simulator.condition_downtime_reader([ident]) == {ident: (start, end, "maint_02")}
    assert orch.db.opened == opened + 1
    sql, params = cursor.execute.call_args.args
    assert "FROM mes_downtime_events" in sql and params == ([ident],)


def test_run_single_cycle_with_anchor_anchors_the_monitor_and_runs_the_step(monkeypatch):
    conn, _ = _conn(fetchone=(str(_ANCHOR),))
    orch = _orchestrator(conn)
    order = []
    orch._run_simulation_cycle.side_effect = lambda: order.append(
        ("cycle", orch.mes_simulator.condition_anchor_epoch))
    monkeypatch.setattr(story_live, "apply_cycle",
                        lambda c, now, anchor: order.append(("step", anchor)) or 1)

    assert orch.run_single_cycle()["success"] is True
    assert orch._story_clock == orch_mod.STORY_CLOCK_PRESENT
    # ADR-0090: the story step runs BEFORE the simulator steps (the clock first)
    assert order == [("step", _ANCHOR), ("cycle", _ANCHOR)]


@pytest.mark.parametrize("failure", ["connect", "query"])
def test_unknown_story_clock_skips_the_monitor_and_the_step(monkeypatch, failure):
    """Per-invocation reality: a fresh orchestrator whose anchor read fails
    has NO previous clock to keep. The clock is UNKNOWN for this cycle: the
    condition monitor is skipped (never a stray wall-clock work order) and so
    is the story step; the rest of the cycle still runs, with no retry."""
    conn, cursor = _conn()
    if failure == "connect":
        orch = _orchestrator(conn, fail=RuntimeError("pool exhausted"))
    else:
        orch = _orchestrator(conn)
        cursor.execute.side_effect = RuntimeError("statement timeout")
    apply = MagicMock(name="apply_cycle")
    monkeypatch.setattr(story_live, "apply_cycle", apply)
    monkeypatch.setattr(orch_mod.time, "sleep", MagicMock(side_effect=AssertionError("no retry")))
    seen = []
    orch._run_simulation_cycle.side_effect = lambda: seen.append(orch.mes_simulator.condition_anchor_epoch)

    assert orch.run_single_cycle() == {"success": True, "cycle_count": 0, "simulator": "ran"}

    assert orch.db.opened == 1  # one best-effort attempt, no retry
    assert orch._story_clock == orch_mod.STORY_CLOCK_UNKNOWN
    assert seen == [None]  # the monitor sees "unknown" during the cycle
    apply.assert_not_called()


def test_monitor_is_skipped_while_the_story_clock_is_unknown():
    """At a wall-clock BPFO-alarm instant a wall-clock monitor WOULD raise a
    work order; with the clock unknown it returns 0 without touching the seed DB."""
    sim = MESSimulator.__new__(MESSimulator)
    sim.condition_anchor_epoch = None
    sim._get_seed_db_conn = MagicMock(side_effect=AssertionError("seed DB touched"))
    alarm = cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0  # wall-clock loop 0, BPFO alarm
    assert cm.spectral_features(cm.loop_step(alarm))["bpfo_alarm"]
    assert sim.monitor_asset_condition("cycle", now=alarm) == 0


@pytest.mark.parametrize("clock", [orch_mod.STORY_CLOCK_ABSENT, orch_mod.STORY_CLOCK_UNKNOWN])
def test_story_step_runs_only_while_the_clock_is_present(monkeypatch, clock):
    """The clock STATE gates the step: a leftover anchor never runs it."""
    conn, _ = _conn()
    orch = _orchestrator(conn)
    orch._story_clock, orch._story_anchor = clock, _ANCHOR
    apply = MagicMock(name="apply_cycle", return_value=1)
    monkeypatch.setattr(story_live, "apply_cycle", apply)
    assert orch._run_story_step() == 0 and orch.db.opened == 0
    apply.assert_not_called()
    orch._story_clock = orch_mod.STORY_CLOCK_PRESENT
    assert orch._run_story_step() == 1
    apply.assert_called_once()
    assert apply.call_args.args[2] == _ANCHOR


def test_story_step_failure_is_swallowed(monkeypatch):
    conn, _ = _conn()
    orch = _orchestrator(conn)
    orch._story_clock, orch._story_anchor = orch_mod.STORY_CLOCK_PRESENT, _ANCHOR

    def boom(*_a):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(story_live, "apply_cycle", boom)
    assert orch._run_story_step() == 0


def test_story_step_connection_failure_is_swallowed():
    conn, _ = _conn()
    orch = _orchestrator(conn, fail=RuntimeError("db down"))
    orch._story_clock, orch._story_anchor = orch_mod.STORY_CLOCK_PRESENT, _ANCHOR
    assert orch._run_story_step() == 0 and orch.db.opened == 1


# --------------------------------------------------------------------------
# Real PostgreSQL acceptance (throwaway container): ERP_SIM_TEST_PG_DSN
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"


def _load_db_init_handler():
    spec = importlib.util.spec_from_file_location("db_init_handler_t6", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    stand_in = "boto3" not in sys.modules
    if stand_in:
        sys.modules["boto3"] = types.ModuleType("boto3")
    try:
        spec.loader.exec_module(module)
    finally:
        if stand_in:
            del sys.modules["boto3"]
    return module


def _align_session_time_zone(conn) -> None:
    """The simulator writes naive LOCAL timestamps and compares them with the
    database's NOW(); in the cloud both are UTC. Give the test session the
    host's offset so a laptop in any zone sees the same comparisons."""
    from erp_simulation.pgdriver import sql as psql

    offset = time.localtime().tm_gmtoff // 60
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset), 60)
    with conn.cursor() as cur:
        cur.execute(stmt("SET TIME ZONE INTERVAL {tz} HOUR TO MINUTE",
                         tz=psql.Literal(f"{sign}{hours:02d}:{minutes:02d}")))
    conn.commit()


@contextmanager
def _fresh_erp_db():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    handler = _load_db_init_handler()
    db_name = "t6_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name}))
    try:
        handler.create_schema(conn)
        handler.seed_data(conn)
        _align_session_time_zone(conn)
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db}", db=db_name))
        admin.close()


@pytest.fixture(scope="module")
def pg_erp():
    with _fresh_erp_db() as conn:
        yield conn


@pytest.fixture
def pg_fresh(monkeypatch):
    """One empty ERP per test (the story line starts idle), and a config-free
    MESSimulator for the simulator's own completion / line-status steps."""
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    with _fresh_erp_db() as conn:
        yield conn


_OTHER_SKU = "MACH-BRKT-9901"  # a one-ERP machining product that is NOT the story part


def _running_work_order(cur, site: str, line: str, sku: str, started: datetime = None) -> str:
    """A RUNNING work order of ``sku`` whose production run is due to complete."""
    cur.execute("SELECT product_id FROM products WHERE sku = %s", (sku,))
    product_id = cur.fetchone()[0]
    tag = uuid.uuid4().hex[:8]
    started = datetime.now() - timedelta(minutes=10) if started is None else started
    cur.execute("""
        INSERT INTO production_runs (run_number, product_id, planned_quantity, status,
                                     actual_start_time, planned_end_time, site, line)
        VALUES (%s, %s, 100, 'IN_PROGRESS', %s, %s, %s, %s) RETURNING run_id
    """, (f"RUN-T6-{tag}", product_id, started, datetime.now() - timedelta(minutes=1), site, line))
    run_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                                     planned_quantity, status, scheduled_start_time,
                                     actual_start_time, target_rate_per_minute)
        VALUES (%s, %s, %s, %s, %s, 100, 'RUNNING', %s, %s, 10) RETURNING work_order_id
    """, (f"WO-T6-{tag}", run_id, product_id, site, line, started, started))
    return cur.fetchone()[0]


def _dimensional_checks(cur, work_order_id) -> list:
    cur.execute("SELECT fail_count FROM mes_quality_checks "
                "WHERE work_order_id = %s AND check_type = 'DIMENSIONAL'", (work_order_id,))
    return [r[0] for r in cur.fetchall()]


def test_pg_live_defect_raises_rejects_on_completion(pg_erp, monkeypatch):
    conn = pg_erp
    other_line = "LINE1"
    assert other_line != _LINE
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO simulation_config (config_key, config_value, data_type, category, default_value)
            VALUES (%s, %s, 'float', 'story', '0')
        """, (story_live.ANCHOR_KEY, repr(_ANCHOR)))
        story_wo = _running_work_order(cur, _SITE, _LINE, story_live.STORY_SKU)
        off_line_wo = _running_work_order(cur, _SITE, other_line, story_live.STORY_SKU)
        other_sku_wo = _running_work_order(cur, _SITE, _LINE, _OTHER_SKU)
    conn.commit()

    anchor = story_live.read_anchor(conn)
    assert anchor == _ANCHOR
    alarm_now = _at_step(cm.BPFO_ALARM_STEP, anchor)
    assert story_live.apply_cycle(conn, alarm_now, anchor) == 1
    assert story_live.apply_cycle(conn, alarm_now + 60, anchor) == 0  # NOT EXISTS guard
    conn.commit()
    with conn.cursor() as cur:
        fails = _dimensional_checks(cur, story_wo)
        assert len(fails) == 1 and fails[0] > 0
        assert _dimensional_checks(cur, off_line_wo) == []
        assert _dimensional_checks(cur, other_sku_wo) == []  # another product: never checked

    # Completion path (the MES cycle's STOP step) sums every check.
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    MESSimulator().simulate_production_events(conn, "t6-cycle")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, reject_quantity, good_quantity, actual_quantity "
                    "FROM mes_work_orders WHERE work_order_id = %s", (story_wo,))
        status, reject, good, actual = cur.fetchone()
    assert status == "COMPLETED"
    assert reject > 0 and good + reject == actual
    assert reject / actual >= cm.DEFECT_REJECT_MIN - 0.02


# --------------------------------------------------------------------------
# The story line is idle (R2b): the step starts the story run itself
# --------------------------------------------------------------------------

def _store_anchor(conn, anchor: float) -> None:
    """The stored story clock the step re-reads under the story-clock lock."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO simulation_config (config_key, config_value, data_type, category, default_value)
            VALUES (%s, %s, 'float', 'story', '0')
            ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value
        """, (story_live.ANCHOR_KEY, repr(float(anchor))))
    conn.commit()


def _anchor_for(step: int, seconds_into_step: float = 1.0) -> float:
    """An anchor that puts the REAL now at ``step`` of loop 0."""
    return time.time() - step * cm.INTERVAL_SEC - seconds_into_step


def _story_rows(cur, site: str = _SITE, line: str = _LINE) -> list:
    cur.execute("""
        SELECT w.work_order_number, w.status, w.planned_quantity, w.actual_start_time,
               w.target_rate_per_minute, p.sku, pr.run_number, pr.status, pr.line,
               pr.planned_end_time, pr.order_id
        FROM mes_work_orders w
        JOIN production_runs pr ON pr.run_id = w.production_run_id
        JOIN products p ON p.product_id = w.product_id
        WHERE w.site = %s AND w.line = %s ORDER BY w.actual_start_time""", (site, line))
    return cur.fetchall()


def _one_value(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def test_pg_alarm_entry_on_an_idle_line_starts_the_story_run(pg_fresh):
    """First alarm cycle, LINE3 idle: ONE RUNNING story work order appears
    (ERP run IN_PROGRESS on LINE3, START event, story part) carrying the
    failing DIMENSIONAL check; later cycles add nothing; the simulator's own
    line-status step shows LINE3 RUNNING and does not complete it early."""
    conn = pg_fresh
    anchor = _anchor_for(cm.BPFO_ALARM_STEP)
    _store_anchor(conn, anchor)
    now = time.time()
    with conn.cursor() as cur:
        assert _story_rows(cur) == []
    assert story_live.apply_cycle(conn, now, anchor) == 1
    conn.commit()
    plan = story_live.story_work_order(now, anchor)
    with conn.cursor() as cur:
        rows = _story_rows(cur)
        assert len(rows) == 1
        number, status, qty, started, rate, sku, run_number, run_status, run_line, end, order = rows[0]
        assert (number, status, sku, run_number) == (
            plan.work_order_number, "RUNNING", story_live.STORY_SKU, plan.run_number)
        assert (run_status, run_line, order) == ("IN_PROGRESS", _LINE, None)
        assert (qty, rate, started, end) == (plan.planned_quantity, plan.target_rate, plan.start, plan.planned_end)
        wo_id = _one_value(cur, "SELECT work_order_id FROM mes_work_orders WHERE work_order_number = %s", (number,))
        assert _dimensional_checks(cur, wo_id)[0] == round(20 * cm.DEFECT_REJECT_MIN)
        cur.execute("SELECT event_type, quantity_at_event FROM mes_production_events WHERE work_order_id = %s",
                    (wo_id,))
        assert cur.fetchall() == [("START", 0)]
        # no lot allocation: never a traceable (shippable) batch id
        assert _one_value(cur, "SELECT count(*) FROM lot_production_usage lpu JOIN production_runs pr "
                               "ON pr.run_id = lpu.production_run_id WHERE pr.run_number = %s",
                          (run_number,)) == 0

    for later in (now + 60, now + 600):
        assert story_live.apply_cycle(conn, later, anchor) == 0
    conn.commit()
    sim = MESSimulator()
    sim.simulate_production_events(conn, "t-idle")
    sim.update_line_status(conn, "t-idle")
    conn.commit()
    with conn.cursor() as cur:
        assert [r[1] for r in _story_rows(cur)] == ["RUNNING"]
        assert _one_value(cur, "SELECT machine_state FROM mes_line_status WHERE site = %s AND line = %s",
                          (_SITE, _LINE)) == "RUNNING"
        assert _one_value(cur, "SELECT count(*) FROM mes_quality_checks q JOIN mes_work_orders w "
                               "ON w.work_order_id = q.work_order_id WHERE w.line = %s "
                               "AND q.check_type = 'DIMENSIONAL'", (_LINE,)) == 1


def test_pg_story_run_completes_normally_after_the_bearing_fails(pg_fresh):
    """The story run is an ordinary ERP-linked work order: the simulator's
    completion step (planned_end_time <= NOW()) closes it at the first cycle
    after the bearing failure with the check summed into the yield, and the
    same loop never starts a second one."""
    conn = pg_fresh
    anchor = _anchor_for(cm.BEARING_FAIL_STEP, seconds_into_step=120.0)  # failed 2 min ago
    _store_anchor(conn, anchor)
    alarm_now = anchor + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0
    assert story_live.apply_cycle(conn, alarm_now, anchor) == 1
    conn.commit()
    MESSimulator().simulate_production_events(conn, "t-complete")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT w.status, w.reject_quantity, w.good_quantity, w.actual_quantity, pr.status
            FROM mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
            WHERE w.work_order_number = %s""", (story_live.story_work_order(alarm_now, anchor).work_order_number,))
        status, reject, good, actual, run_status = cur.fetchone()
    assert (status, run_status) == ("COMPLETED", "COMPLETED")
    assert reject > 0 and good + reject == actual
    # later in the same alarm window: line idle again, but one story run per loop
    assert story_live.apply_cycle(conn, alarm_now + 1800, anchor) == 0
    conn.commit()
    with conn.cursor() as cur:
        assert len(_story_rows(cur)) == 1


def _alarm_cycles(alarm_now: float, anchor: float) -> list:
    """The Lambda's one-minute cycle instants inside the loop-0 alarm window."""
    fail = anchor + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC
    return [alarm_now + 60 * k for k in range(int((fail - alarm_now) // 60) + 1)
            if alarm_now + 60 * k < fail]


def test_pg_the_check_recurs_every_interval_and_the_fail_share_climbs(pg_fresh):
    """Every minute across the whole alarm window: a new DIMENSIONAL check
    lands once per DEFECT_CHECK_INTERVAL_MINUTES (never twice inside one
    interval), each failing ``defect_check_row(step)`` at its own step, so
    the fail share climbs; completion then sums EVERY check into the yield."""
    conn = pg_fresh
    anchor = _anchor_for(cm.BEARING_FAIL_STEP, seconds_into_step=120.0)  # failed 2 min ago
    _store_anchor(conn, anchor)
    alarm_now = anchor + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0
    cycles = _alarm_cycles(alarm_now, anchor)
    inserted = [story_live.apply_cycle(conn, t, anchor) for t in cycles]
    conn.commit()
    interval = story_live.DEFECT_CHECK_INTERVAL_MINUTES
    assert [k for k, n in enumerate(inserted) if n] == list(range(0, len(cycles), interval))
    assert len(cycles) == 108 and sum(inserted) == 8
    expected = [story_live.defect_check_row(
        cm.loop_step(cm.story_seconds(cycles[k], anchor)), 0.0)[2] for k in range(0, len(cycles), interval)]
    number = story_live.story_work_order(alarm_now, anchor).work_order_number
    with conn.cursor() as cur:
        wo_id = _one_value(cur, "SELECT work_order_id FROM mes_work_orders WHERE work_order_number = %s",
                           (number,))
        cur.execute("SELECT fail_count FROM mes_quality_checks WHERE work_order_id = %s "
                    "AND check_type = 'DIMENSIONAL' ORDER BY check_time", (wo_id,))
        fails = [r[0] for r in cur.fetchall()]
    assert fails == expected and fails == sorted(fails) and fails[-1] > fails[0]
    MESSimulator().simulate_production_events(conn, "t-climb")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, actual_quantity, good_quantity, reject_quantity "
                    "FROM mes_work_orders WHERE work_order_id = %s", (wo_id,))
        status, actual, good, reject = cur.fetchone()
        cur.execute("SELECT SUM(pass_count), SUM(sample_size) FROM mes_quality_checks "
                    "WHERE work_order_id = %s", (wo_id,))
        total_pass, total_sample = cur.fetchone()
    assert status == "COMPLETED" and total_sample == 20 * len(fails)
    assert (good, reject) == mes_mod.derive_good_reject(actual, total_pass, total_sample, 1.0)
    assert reject / actual > cm.DEFECT_REJECT_MIN


def test_pg_a_running_story_part_simulator_order_takes_the_check_first(pg_fresh):
    """A simulator work order OF THE STORY PART already RUNNING on LINE3 gets
    the check and no story run starts; once it completes, the next alarm
    cycle starts ONE."""
    conn = pg_fresh
    anchor = _anchor_for(cm.BPFO_ALARM_STEP)
    _store_anchor(conn, anchor)
    now = time.time()
    with conn.cursor() as cur:
        sim_wo = _running_work_order(cur, _SITE, _LINE, story_live.STORY_SKU)
    conn.commit()
    assert story_live.apply_cycle(conn, now, anchor) == 1
    conn.commit()
    with conn.cursor() as cur:
        assert len(_story_rows(cur)) == 1 and len(_dimensional_checks(cur, sim_wo)) == 1
    MESSimulator().simulate_production_events(conn, "t-sim")  # the simulator's run was due
    conn.commit()
    assert story_live.apply_cycle(conn, now + 60, anchor) == 1
    assert story_live.apply_cycle(conn, now + 120, anchor) == 0
    conn.commit()
    with conn.cursor() as cur:
        statuses = [(r[0], r[1]) for r in _story_rows(cur)]
    assert statuses[-1] == (story_live.story_work_order(now, anchor).work_order_number, "RUNNING")
    assert len(statuses) == 2


def test_pg_a_running_order_of_another_part_gets_no_check_and_the_story_run_waits(pg_fresh):
    """A simulator order of ANOTHER product RUNNING on LINE3: the story run
    waits (the line is busy, as for any RUNNING order), but that order gets
    NO check - a shipped run of another part must never become the failed
    batch. Once it completes, the next alarm cycle starts the story run and
    only the story run carries the check."""
    conn = pg_fresh
    anchor = _anchor_for(cm.BPFO_ALARM_STEP)
    _store_anchor(conn, anchor)
    now = time.time()
    with conn.cursor() as cur:
        other_wo = _running_work_order(cur, _SITE, _LINE, _OTHER_SKU)
    conn.commit()
    assert story_live.apply_cycle(conn, now, anchor) == 0
    conn.commit()
    with conn.cursor() as cur:
        assert len(_story_rows(cur)) == 1  # the other order only: the story run waits
        assert _dimensional_checks(cur, other_wo) == []
    MESSimulator().simulate_production_events(conn, "t-other")  # the other order was due
    conn.commit()
    assert story_live.apply_cycle(conn, now + 60, anchor) == 1
    conn.commit()
    story_number = story_live.story_work_order(now, anchor).work_order_number
    with conn.cursor() as cur:
        rows = _story_rows(cur)
        assert [(r[0], r[1], r[5]) for r in rows][-1] == (story_number, "RUNNING", story_live.STORY_SKU)
        assert _dimensional_checks(cur, other_wo) == []
        assert _one_value(cur, "SELECT count(*) FROM mes_quality_checks q JOIN mes_work_orders w "
                               "ON w.work_order_id = q.work_order_id JOIN products p "
                               "ON p.product_id = w.product_id WHERE w.line = %s "
                               "AND q.check_type = 'DIMENSIONAL' AND p.sku <> %s",
                          (_LINE, story_live.STORY_SKU)) == 0


def test_pg_history_batch_stays_the_newest_shipped_failure(pg_fresh):
    """Module 2: with no simulator order of the story part in the alarm
    window, the newest SHIPPED batch with failed DIMENSIONAL checks (the
    story lookup's rule) is still the history's run, even after the live story
    run has completed with rejects and every other history run has shipped
    too (their routine VISUAL checks never count); shipping never picks the
    live run as a batch id (no lot allocation)."""
    from erp_simulation import story_history as sh
    from erp_simulation.shipping_simulator import ShippingSimulator
    conn = pg_fresh
    anchor = _anchor_for(cm.BEARING_FAIL_STEP, seconds_into_step=120.0)
    _store_anchor(conn, anchor)
    with conn.cursor() as cur:
        sh._write_erp(cur, sh.plan(anchor))
    conn.commit()
    alarm_now = anchor + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0
    assert story_live.apply_cycle(conn, alarm_now, anchor) == 1
    conn.commit()
    MESSimulator().simulate_production_events(conn, "t-m2")
    conn.commit()
    live_run = story_live.story_work_order(alarm_now, anchor).run_number
    with conn.cursor() as cur:
        assert _one_value(cur, "SELECT w.reject_quantity FROM mes_work_orders w JOIN production_runs pr "
                               "ON pr.run_id = w.production_run_id WHERE pr.run_number = %s", (live_run,)) > 0
        newest = _one_value(cur, """
            SELECT si.batch_id FROM shipment_items si
            JOIN production_runs pr ON pr.run_number = si.batch_id
            JOIN mes_work_orders w ON w.production_run_id = pr.run_id
            WHERE EXISTS (SELECT 1 FROM mes_quality_checks q
                          WHERE q.work_order_id = w.work_order_id AND q.fail_count > 0
                            AND q.check_type = 'DIMENSIONAL')
            ORDER BY pr.actual_end_time DESC LIMIT 1""")
        assert newest == sh.plan(anchor).failed_run.run_number != live_run
        product_id = _one_value(cur, "SELECT product_id FROM products WHERE sku = %s", (story_live.STORY_SKU,))
        picked = ShippingSimulator.__new__(ShippingSimulator)._traceable_batch_id(
            cur, product_id, story_live.STORY_SKU)
        assert picked != live_run


# --------------------------------------------------------------------------
# One clock (ADR-0088 D5 / D6): the minute walk against the ERP AND the CMMS
# --------------------------------------------------------------------------
_SEED_SQL = ("001_schemas.sql", "011_cmms_indianapolis.sql")
_SEED_DIR = Path(__file__).resolve().parents[2] / "lambda-adt-seed"


@pytest.fixture
def utc_host():
    """The Lambda's zone: the ERP's naive LOCAL stamps and the CMMS's naive
    UTC stamps are then the same literal, as in the cloud. Listed BEFORE the
    database fixtures so their session zone follows."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def pg_seed():
    """A throwaway seed database with the fourth site's CMMS (001 + 011)."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    name = "t6seed_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": name}))
    try:
        with conn.cursor() as cur:
            for sql in _SEED_SQL:
                cur.execute((_SEED_DIR / sql).read_text(encoding="utf-8"))
        conn.commit()
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db}", db=name))
        admin.close()


def _congruent_anchor_failed_ago(seconds: float = 120.0) -> float:
    """A one-clock (congruent) anchor whose loop-0 bearing failed ``seconds`` ago."""
    return float(cm.snap_anchor(time.time() - cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC - seconds))


def _walk(erp, seed, anchor: float, offset: float = 7.0) -> None:
    """The Lambda's every-minute cycle across loop 0's alarm window: the MES
    condition monitor (CMMS) then the story step (MES), as the orchestrator runs them."""
    _store_anchor(erp, anchor)
    sim = MESSimulator()
    sim.condition_anchor_epoch = anchor
    sim.condition_anchor_reader = lambda: story_live.read_anchor(erp)
    sim._get_seed_db_conn = lambda: seed
    alarm, fail = cm.alarm_epoch(anchor, anchor), cm.fail_epoch(anchor, anchor)
    for t in range(int(alarm), int(fail), 60):
        sim.monitor_asset_condition("walk", now=t + offset)
        story_live.apply_cycle(erp, t + offset, anchor)
        erp.commit()


def _checks(cur, line: str = _LINE) -> list:
    cur.execute("""
        SELECT w.work_order_number, q.check_time, q.fail_count, q.notes
        FROM mes_quality_checks q JOIN mes_work_orders w ON w.work_order_id = q.work_order_id
        WHERE w.line = %s AND q.check_type = 'DIMENSIONAL' ORDER BY q.check_time""", (line,))
    return cur.fetchall()


def test_pg_one_clock_walk_lands_eight_checks_on_the_alarm_slots(utc_host, pg_fresh, pg_seed):
    """8 checks at exactly alarm + 900 n; check 0 == the VIB-CMX order's
    reported_date == the alarm instant; each check notes the published
    Spectrum_BPFO of its slot; the story run starts AT the alarm slot."""
    anchor = _congruent_anchor_failed_ago()
    alarm, fail = cm.alarm_epoch(anchor, anchor), cm.fail_epoch(anchor, anchor)
    assert alarm % cm.TELEMETRY_GRID_SECONDS == 0
    _walk(pg_fresh, pg_seed, anchor)
    slots = [alarm + _SLOT * n for n in range(8)]
    plan = story_live.story_work_order(alarm, anchor)
    with pg_fresh.cursor() as cur:
        checks = _checks(cur)
        rows = _story_rows(cur)
    assert [c[1] for c in checks] == [datetime.fromtimestamp(t) for t in slots]
    assert {c[0] for c in checks} == {plan.work_order_number}
    for (_wo, _at, fail_count, notes), slot in zip(checks, slots):
        step = cm.loop_step(cm.story_seconds(slot, anchor))
        assert notes.endswith(f"BPFO band {cm.spectral_features(step)['bpfo']:.2f} mm/s")
        assert fail_count == story_live.defect_check_row(step, 0.0)[2]
    assert len(rows) == 1
    assert rows[0][3] == datetime.fromtimestamp(alarm)  # story run actual_start_time
    assert rows[0][2] == story_live.STORY_PLANNED_QUANTITY == 107
    with pg_seed.cursor() as cur:
        cur.execute("SELECT wonum, reported_date, target_start, sched_start "
                    "FROM cmms_indianapolis.work_orders WHERE wonum LIKE 'VIB-CMX-%%'")
        orders = cur.fetchall()
    assert orders == [("VIB-CMX-000000", checks[0][1], checks[0][1], datetime.fromtimestamp(fail))]
    assert checks[0][1] == datetime.fromtimestamp(alarm)


def test_pg_a_late_starting_story_part_order_gets_only_later_slots(utc_host, pg_fresh, pg_seed):
    """A simulator order of the story part that started between slot 1 and
    slot 2 is checked from slot 2 on (6 checks); the story run waits (the
    line is busy) and the earlier slots are skipped, never back-filled."""
    anchor = _congruent_anchor_failed_ago()
    alarm = cm.alarm_epoch(anchor, anchor)
    started = datetime.fromtimestamp(alarm + _SLOT + 100)
    with pg_fresh.cursor() as cur:
        sim_wo = _running_work_order(cur, _SITE, _LINE, story_live.STORY_SKU, started=started)
        sim_number = _one_value(cur, "SELECT work_order_number FROM mes_work_orders WHERE work_order_id = %s",
                                (sim_wo,))
    pg_fresh.commit()
    _walk(pg_fresh, pg_seed, anchor)
    with pg_fresh.cursor() as cur:
        checks = _checks(cur)
        rows = _story_rows(cur)
    assert [c[1] for c in checks] == [datetime.fromtimestamp(alarm + _SLOT * n) for n in range(2, 8)]
    assert all(c[1] >= started for c in checks)
    assert {c[0] for c in checks} == {sim_number}
    assert [r[0] for r in rows] == [sim_number]  # no story run while the line was busy


# ---------------------------------------------------------------------------
# Packaged loop length (ADR-0091): the live step on a 3 h loop
# ---------------------------------------------------------------------------

from erp_simulation.tests._loop_env import run_under_loop  # noqa: E402

_LIVE_DUMP = """\
from erp_simulation import condition_monitor as cm, story_live as sl
anchor = cm.snap_anchor(1.8e9)
t0 = anchor + 5 * cm.TOTAL_SECONDS + 1
alarm, fail = cm.alarm_epoch(t0, anchor), cm.fail_epoch(t0, anchor)
slots = sorted({sl.defect_check_slot(t, anchor) for t in range(int(alarm), int(fail), 60)})
rows = [sl._check_params(s, anchor)[:2]
        + (cm.spectral_features(cm.loop_step(cm.story_seconds(s, anchor)))["bpfo"],) for s in slots]
wo = sl.story_work_order(alarm, anchor)
emit({"consts": [sl.DEFECT_CHECKS_PER_WINDOW, sl.STORY_PLANNED_QUANTITY],
      "alarm": alarm, "fail": fail, "slots": slots, "rows": rows,
      "outside": [sl.defect_check_slot(alarm - 1, anchor), sl.defect_check_slot(fail, anchor)],
      "wo": [wo.start.timestamp(), wo.planned_end.timestamp(), wo.planned_quantity, wo.run_number]})
"""


def test_live_constants_are_the_shipped_ones_when_unset():
    assert (story_live.DEFECT_CHECKS_PER_WINDOW, story_live.STORY_PLANNED_QUANTITY) == (8, 107)


def test_a_three_hour_loop_runs_three_checks_on_the_grid():
    out = run_under_loop("10800", _LIVE_DUMP)
    assert out["consts"] == [3, 40]
    alarm, fail = out["alarm"], out["fail"]
    assert out["slots"] == [alarm, alarm + 900, alarm + 1800]  # 3 slots in the 2410 s window
    assert all(s % 300 == 0 for s in out["slots"]) and out["outside"] == [None, None]
    fails = [failed for _step, failed, _bpfo in out["rows"]]
    assert fails == sorted(fails) and fails[0] == round(20 * cm.DEFECT_REJECT_MIN)
    for step, _failed, bpfo in out["rows"]:
        assert 1462 <= step < 1944 and bpfo >= cm.BPFO_ALARM_MM_S
    start, end, qty, run = out["wo"]
    assert (start, end - start, qty) == (alarm, 2410, 40) and run.startswith("RUN-") and run.endswith("-80005")
