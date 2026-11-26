# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deploy-time live-story history (erp_simulation/story_history.py) and its
CloudFormation provider handler (lambda_/lambda_story_history.py).

Offline: the pure planner plus the writer against a fake DB that honours the
natural keys (ON CONFLICT / NOT EXISTS) the real SQL relies on. The PG variant
(``ERP_SIM_TEST_PG_DSN``) runs the writer against the real db-init DDL and the
seed CMMS DDL in two throwaway databases.
"""
from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import story_history as shist

_REPO = Path(__file__).resolve().parents[2]
ANCHOR = 1_790_000_000.0
CONTRACT_KEYS = {
    "story_anchor_epoch", "story_loop_seconds", "story_interval_seconds",
    "story_bpfo_alarm_step", "story_fail_step", "story_repair_steps",
    "story_history_days", "story_failed_run_number", "story_deferred_pm_wonum",
    "story_anchor_source", "story_telemetry_anchor", "story_telemetry_cursor_epoch",
}


def _epoch(naive_utc: datetime) -> float:
    return naive_utc.replace(tzinfo=timezone.utc).timestamp()


def _phase_at(when: datetime, anchor: float = ANCHOR) -> str:
    return cm.phase(cm.loop_step(cm.story_seconds(_epoch(when), anchor)))


def _db_init_literal(name: str):
    tree = ast.parse((_REPO / "lambda-db-init" / "handler.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in db-init handler")


# ---------------------------------------------------------------------------
# Pure plan
# ---------------------------------------------------------------------------

def test_plan_is_deterministic():
    assert shist.plan(ANCHOR) == shist.plan(ANCHOR)
    assert shist.plan(ANCHOR) != shist.plan(ANCHOR + 60)


# Golden digests of plan(ANCHOR, days) taken BEFORE the one-clock config keys
# were added: every original config row stays byte-identical (new keys only
# ever APPEND config rows). The row digests were re-taken when the history
# gained its lot trace: two supplier lots for the failed part and a passing
# DIMENSIONAL check on every normal run, again when the failed batch's
# lot became a history lot of its own, and again when the normal runs became
# wholly clean (VISUAL 50 of 50, 60 good of 60; the config digests did not
# move), and again when the story PM's last completed cycle became a history
# work order (only that row was added; the config digests did not move), and
# again when the live story gained its non-story plant PM alignment
# (``plant_pm``: the shifted schedules and their CLOSE work orders, a NEW field
# appended to the body; every pre-existing row stays byte-identical, so the
# body WITHOUT ``plant_pm`` still hashes to the pre-plant_pm digest — see
# ``test_the_body_without_plant_pm_is_the_pre_plant_pm_golden`` — and the
# config digests did not move), and again when the deferred PM's log gained
# its CREATE entry (dated at the order's reported_date; the body WITHOUT that
# entry still hashes to the previous digest, see
# ``test_the_body_without_the_create_entry_is_the_previous_golden``; the config
# digests did not move). The plant's past stops (``past_stops``) are a NEW field
# outside the body, so neither digest moved when they were added: every row
# the body already held is byte-identical.
_GOLDEN_KEYS = (
    "story_anchor_epoch", "story_loop_seconds", "story_interval_seconds",
    "story_bpfo_alarm_step", "story_fail_step", "story_repair_steps",
    "story_history_days", "story_failed_run_number", "story_deferred_pm_wonum",
)
# The body digest BEFORE plant_pm was appended (every other row byte-identical).
_GOLDEN_PRE_PLANT_PM_BODY = {
    3: "bdb36f41edf7c58c4187dad310586c23bdacd72d071dc08f4cf568f9a7a9c444",
    2: "7536cac5e241319dcded2d155094c68ee6fa158544bb0309aa8d5a5b4f3b9293",
}
# The body digest BEFORE the deferred PM's CREATE log entry was added.
_GOLDEN_PRE_CREATE_LOG_BODY = {
    3: "3596e49dda58163752041b42d1fe16cdf53424dba441451234d892a229af4a5a",
    2: "1465043ae0b7c4658de10ec46c3c91b3deb9b5c52ef48ecdc60a11e414aa7f01",
}
_GOLDEN = {
    3: ("d60c8c965e11d347c1646253693b46ec28954c4720907e8af89eadb0b9267db6",
        "557cc2d4d46756a06f2f9366a3b524326701f8bff129714051550ca14992a68b"),
    2: ("7f31d94610799e807ae0aafd54bdf92ba6ec0720589cca6ae76bcc060129f9b3",
        "4d3ed479a9790e22d57c90ff04eef4a0eec9fd3594b632fa919df7a68103230e"),
}


@pytest.mark.parametrize("days", sorted(_GOLDEN))
def test_plan_rows_are_byte_identical_to_the_golden(days):
    import hashlib
    sp = shist.plan(ANCHOR, days)
    body = repr((sp.anchor_epoch, sp.days, sp.runs, sp.failed_run, sp.downtime, sp.orders,
                 sp.run_orders, sp.cmms_work_orders, sp.cmms_log, sp.plant_pm))
    config = repr(tuple(r for r in sp.config if r.key in _GOLDEN_KEYS))
    assert tuple(r.key for r in sp.config)[:len(_GOLDEN_KEYS)] == _GOLDEN_KEYS
    assert (hashlib.sha256(body.encode()).hexdigest(),
            hashlib.sha256(config.encode()).hexdigest()) == _GOLDEN[days]


@pytest.mark.parametrize("days", sorted(_GOLDEN))
def test_the_body_without_plant_pm_is_the_pre_plant_pm_golden(days):
    """The two goldens change ONLY by the added ``plant_pm`` rows: the body
    taken WITHOUT ``plant_pm`` still hashes to the pre-plant_pm digest, the way
    the 3064 commit proved the plan without its new row hashed to the old
    value. So nothing else in the plan moved when plant_pm was added."""
    import hashlib
    sp = shist.plan(ANCHOR, days)
    body_without = repr((sp.anchor_epoch, sp.days, sp.runs, sp.failed_run, sp.downtime, sp.orders,
                         sp.run_orders, sp.cmms_work_orders, _without_create_entry(sp)))
    assert hashlib.sha256(body_without.encode()).hexdigest() == _GOLDEN_PRE_PLANT_PM_BODY[days]


def _without_create_entry(sp):
    return tuple(log for log in sp.cmms_log if log.log_type != "CREATE")


@pytest.mark.parametrize("days", sorted(_GOLDEN))
def test_the_body_without_the_create_entry_is_the_previous_golden(days):
    """The goldens change ONLY by the deferred PM's CREATE log entry: without
    it the body hashes to the previous digest, so no other row moved."""
    import hashlib
    sp = shist.plan(ANCHOR, days)
    body = repr((sp.anchor_epoch, sp.days, sp.runs, sp.failed_run, sp.downtime, sp.orders,
                 sp.run_orders, sp.cmms_work_orders, _without_create_entry(sp), sp.plant_pm))
    assert hashlib.sha256(body.encode()).hexdigest() == _GOLDEN_PRE_CREATE_LOG_BODY[days]
    assert len(sp.cmms_log) - len(_without_create_entry(sp)) == 1


@pytest.mark.parametrize("anchor,days", [(0, 3), (-5, 3), (ANCHOR, 0), (ANCHOR, 10)])
def test_plan_rejects_bad_input(anchor, days):
    with pytest.raises(ValueError):
        shist.plan(anchor, days)


def test_failed_batch_lies_inside_the_bpfo_window():
    failed = shist.plan(ANCHOR).failed_run
    assert failed.line == cm.STORY_MES_LINE and failed.sku == shist.FAILED_SKU
    for when in (failed.start, failed.end, *(c.check_time for c in failed.checks)):
        assert _phase_at(when) == "bpfo_alarm", when
    # ... in the loop that contains anchor - 1 day
    assert shist.story_windows(ANCHOR)["loop_start"] == ANCHOR - shist.DAY < _epoch(failed.start) < ANCHOR


def test_failed_batch_rejects_follow_the_live_defect_rule():
    failed = shist.plan(ANCHOR).failed_run
    assert [c.check_type for c in failed.checks] == ["DIMENSIONAL"] * 3
    win = shist.story_windows(ANCHOR)
    for check in failed.checks:
        step = int((_epoch(check.check_time) - win["loop_start"]) // cm.INTERVAL_SEC)
        assert check.fail_count == round(check.sample_size * cm.defect_reject_fraction(step))
        assert 0.25 <= check.fail_count / check.sample_size <= 0.40
        assert check.pass_count + check.fail_count == check.sample_size
    assert 0.25 <= failed.reject / failed.planned <= 0.35
    assert failed.good + failed.reject == failed.planned


def test_normal_runs_are_normal_and_never_on_the_story_line_in_an_alarm():
    sp = shist.plan(ANCHOR)
    assert len(sp.runs) == sp.days * shist.RUNS_PER_DAY
    assert {r.line for r in sp.runs} == set(shist.LINES)
    for run in sp.runs:
        assert run.reject / run.planned <= 0.02 and run.stop_reason is None
        dimensional = [c for c in run.checks if c.check_type == "DIMENSIONAL"]
        assert len(dimensional) == 1 and dimensional[0].fail_count == 0
        # Only the failed batch fails a check or rejects a part: a reject on
        # every normal run made the workshop's answers call every batch failed.
        assert all(c.fail_count == 0 and c.pass_count == c.sample_size for c in run.checks)
        assert (run.good, run.reject) == (run.planned, 0)
        if run.line == cm.STORY_MES_LINE:
            for when in (run.start, run.end):
                assert _phase_at(when) != "bpfo_alarm"
    numbers = [r.run_number for r in (*sp.runs, sp.failed_run)]
    assert len(set(numbers)) == len(numbers)
    assert all(re.fullmatch(r"RUN-\d{8}-9\d{4}", n) for n in numbers)


def test_history_is_all_before_the_anchor():
    sp = shist.plan(ANCHOR)
    stamps = [t for r in (*sp.runs, sp.failed_run) for t in (r.start, r.end)]
    stamps += [sp.downtime.start, sp.downtime.end]
    stamps += [t for o in (*sp.orders, *sp.run_orders)
               for t in (o.order_date, o.ship_date, o.delivered_date) if t]
    assert max(_epoch(t) for t in stamps) < ANCHOR
    assert min(_epoch(r.start) for r in sp.runs) >= ANCHOR - sp.days * shist.DAY


def test_maintenance_timeline():
    sp = shist.plan(ANCHOR)
    done, pm, condition, repair = sp.cmms_work_orders
    assert done.wonum == shist.LAST_PM_WONUM and done.actual_finish < pm.reported_date
    failed = sp.failed_run
    assert (pm.wonum, pm.status, pm.work_type, pm.pm_num) == ("3101", "WAPPR", "PM", shist.STORY_PM_NUM)
    assert "DEFERRED" in pm.description
    assert _epoch(pm.target_start) == _epoch(failed.start) - 5 * shist.DAY
    assert _epoch(pm.sched_start) == ANCHOR + 2 * shist.DAY
    assert pm.target_start < failed.start < repair.actual_start < repair.actual_finish
    assert (condition.wonum, condition.work_type, condition.status, condition.failure_code) == ("3107", "CM", "COMP", "BEARING")
    assert condition.reported_by == "Condition monitor"
    assert _phase_at(condition.reported_date) == "bpfo_alarm"
    assert (repair.wonum, repair.work_type, repair.status, repair.failure_code) == ("3108", "EM", "COMP", "BEARING")
    assert _phase_at(repair.actual_start) == "repair"
    assert failed.end <= sp.downtime.start == repair.actual_start
    assert sp.cmms_log[-1].wonum == pm.wonum and sp.cmms_log[-1].log_type == "DEFERRAL"
    for wo in sp.cmms_work_orders:
        assert len(wo.wonum) <= 15 and len(wo.description) <= 250


def test_deferred_pm_log_dates_its_creation_and_its_deferral_apart():
    """A test event's answers dated the PM's deferral by the day the order was
    raised (``reported_date``): the history wrote only the DEFERRAL entry, on a
    row whose description already says DEFERRED. The log now dates creation
    (CREATE at ``reported_date``, the 011 seed's own deferred PM has one too)
    and deferral (DEFERRAL two days before the original due date) apart."""
    sp = shist.plan(ANCHOR)
    pm = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.DEFERRED_PM_WONUM)
    done = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.LAST_PM_WONUM)
    create, deferral = sp.cmms_log
    assert (create.wonum, create.log_type, create.log_date, create.author) == (
        pm.wonum, "CREATE", pm.reported_date, pm.reported_by)
    assert create.entry == (
        f"PM work order generated from {pm.pm_num}. Due {pm.target_start:%Y-%m-%d} per "
        f"{shist.PM_FREQUENCY_DAYS}-day cycle (last completed {done.actual_finish:%Y-%m-%d}).")
    assert (deferral.wonum, deferral.log_type) == (pm.wonum, "DEFERRAL")
    assert _epoch(deferral.log_date) == _epoch(pm.target_start) - 2 * shist.DAY
    assert create.log_date < deferral.log_date < pm.target_start
    assert len(create.entry) <= 250


def test_write_logs_creation_and_deferral_once_and_restore_removes_both():
    erp, seed = FakeDB(), FakeDB()
    shist.write(erp, seed, shist.plan(ANCHOR))
    rows = [r for r in seed.log if r[0] == shist.DEFERRED_PM_WONUM]
    assert [r[3] for r in rows] == ["CREATE", "DEFERRAL"]
    assert [r for r in seed.log if r[0] == shist.DEFERRED_PM_WONUM] == [
        (log.wonum, log.log_date, log.author, log.log_type, log.entry)
        for log in shist.plan(ANCHOR).cmms_log]
    again = shist.write(erp, seed, shist.plan(ANCHOR))
    assert again["seed"]["cmms_log"] == 0
    assert [r for r in seed.log if r[0] == shist.DEFERRED_PM_WONUM] == rows
    shist.restore_frozen_seed(seed)
    assert [r for r in seed.log if r[0] == shist.DEFERRED_PM_WONUM] == []


def test_three_customers_received_the_failed_batch():
    sp = shist.plan(ANCHOR)
    assert len({o.customer_code for o in sp.orders}) == 3
    assert all(o.batch_id == sp.failed_run.run_number and o.sku == sp.failed_run.sku for o in sp.orders)
    assert sum(o.quantity for o in sp.orders) <= sp.failed_run.good
    assert all(o.order_date < sp.failed_run.start and o.ship_date > sp.failed_run.end for o in sp.orders)
    assert {o.status for o in sp.orders} <= {"SHIPPED", "DELIVERED"}


def test_every_completed_history_run_ships_once():
    """A plant ships its completed runs: each normal history run has ONE
    DELIVERED order/shipment to one site customer (rotating), carrying the
    run number as the shipment line's batch id, shipped after the run."""
    sp = shist.plan(ANCHOR)
    assert len(sp.run_orders) == len(sp.runs)
    for run, order in zip(sp.runs, sp.run_orders):
        assert (order.batch_id, order.sku, order.status) == (run.run_number, run.sku, "DELIVERED")
        assert 0 < order.quantity <= run.good
        assert order.order_date < run.start and order.ship_date > run.end
        assert order.ship_date < order.delivered_date < shist.ts(ANCHOR)
    codes = [o.customer_code for o in sp.run_orders]
    assert codes[:len(shist.SITE_CUSTOMER_CODES)] == list(shist.SITE_CUSTOMER_CODES)
    assert set(codes) == set(shist.SITE_CUSTOMER_CODES)
    every = (*sp.orders, *sp.run_orders)
    for attr in ("order_number", "shipment_number"):
        assert len({getattr(o, attr) for o in every}) == len(every)
    assert sp.failed_run.run_number not in {o.batch_id for o in sp.run_orders}


def test_only_the_failed_run_has_a_failing_dimensional_check():
    """The story lookup's failed batch = the newest SHIPPED run with a failing
    DIMENSIONAL check; shipping every run must not make another one qualify."""
    sp = shist.plan(ANCHOR)
    failing = [r.run_number for r in (*sp.runs, sp.failed_run)
               if any(c.check_type == "DIMENSIONAL" and c.fail_count > 0 for c in r.checks)]
    assert failing == [sp.failed_run.run_number]


def test_reference_data_matches_the_db_init_seed():
    """Customers, SKUs and lots the history joins to are the ones db-init seeds."""
    customers = {row[0] for row in _db_init_literal("MACHINING_CUSTOMERS")}
    assert set(shist.STORY_CUSTOMER_CODES) <= customers
    assert set(shist.SITE_CUSTOMER_CODES) == customers
    lots = {f"LOT-IND-{row[0].replace('-', '')}-SEED" for row in _db_init_literal("INDIANAPOLIS_RAW_ITEMS")}
    assert {lot for _sku, lot, _kg in shist.PRODUCTS} <= lots
    assert shist.PRODUCTS[0][1] == f"LOT-IND-{shist.FAILED_MATERIAL_CODE.replace('-', '')}-SEED"
    assert shist.EARLIER_LOT_NUMBER not in lots and shist.SUSPECT_LOT_NUMBER not in lots
    assert "DIMENSIONAL" in _db_init_literal("MES_QUALITY_CHECK_TYPES")


def test_frozen_pm_dates_are_the_011_seed_row():
    """The Delete restores exactly the seed's PM-1001-0007 dates and frequency."""
    row = re.search(r"\('PM-1001-0007', '1001-CMX-441', '[^']*', (\d+), 'DAYS', '[^']*', "
                    r"'([\d-]+)', '([\d-]+)'", _SEED_011.read_text(encoding="utf-8"))
    assert (int(row.group(1)), (row.group(2), row.group(3))) == (shist.PM_FREQUENCY_DAYS, shist.FROZEN_PM_DATES)


def test_the_pm_schedule_follows_the_deferred_pm():
    sp = shist.plan(ANCHOR)
    pm = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.DEFERRED_PM_WONUM)
    last, due = shist._pm_schedule_dates(sp)
    assert due == pm.target_start.date().isoformat()
    assert (date.fromisoformat(due) - date.fromisoformat(last)).days == shist.PM_FREQUENCY_DAYS
    assert date.fromisoformat(due) < sp.failed_run.start.date()


def test_the_schedules_last_completion_is_a_completed_work_order():
    """The PM schedule's last completion is a completed work order of the story
    PM, so a reader of the schedule and a reader of the work-order history see
    the same last-done date. Without it the newest completed order of the PM
    was the 011 seed's 2025-10-01 one, months before the schedule's date."""
    sp = shist.plan(ANCHOR)
    last, due = shist._pm_schedule_dates(sp)
    done = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.LAST_PM_WONUM)
    deferred = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.DEFERRED_PM_WONUM)
    assert (done.work_type, done.status, done.pm_num, done.failure_code) == ("PM", "CLOSE", shist.STORY_PM_NUM, None)
    assert "DEFERRED" not in done.description
    assert done.target_start.date().isoformat() == done.actual_finish.date().isoformat() == last
    assert done.reported_date < done.target_start == done.sched_start <= done.actual_start < done.actual_finish
    assert done.actual_finish < deferred.reported_date and int(done.wonum) < int(deferred.wonum)
    assert len(done.wonum) <= 15 and len(done.description) <= 250
    seed_wonums = set(re.findall(r"^\('(\w+)', '1001-", _SEED_011.read_text(encoding="utf-8"), re.M))
    assert done.wonum not in seed_wonums
    completed_seed_pm = re.findall(
        rf"^\('\w+', '{cm.STORY_CMMS_ASSET_NUM}', 'PM', '[^']*', 'CLOSE', \d, '{shist.STORY_PM_NUM}', "
        r"'[^']*', +'[^']*', '[^']*', '[^']*', '[^']*', '([\d-]+)",
        _SEED_011.read_text(encoding="utf-8"), re.M)
    assert completed_seed_pm and max(completed_seed_pm) < last       # it is the newest completion
    assert shist.LAST_PM_WONUM in shist.HISTORY_WONUMS                      # the Delete removes it
    assert shist.LAST_PM_WONUM not in shist.STORY_ORDER_WONUMS              # not a story order (answer key)
    assert set(shist.HISTORY_WONUMS) == {wo.wonum for wo in sp.cmms_work_orders}


# ---------------------------------------------------------------------------
# The non-story plant PM schedules: shifted onto the live run (this fix)
# ---------------------------------------------------------------------------

def _seed_pm_schedule_rows():
    """The 011 ``pm_schedules`` rows as (pm_num -> (asset, freq, crew, desc, last, due))."""
    body = _SEED_011.read_text(encoding="utf-8").split("INSERT INTO pm_schedules VALUES", 1)[1]
    body = body.split("ON CONFLICT", 1)[0]
    rows = {}
    for m in re.finditer(
            r"\('(PM-[\w-]+)',\s*'([\w-]+)',\s*'((?:[^']|'')*)',\s*(\d+),\s*'DAYS',\s*'([\w-]+)',"
            r"\s*'([\d-]+)',\s*'([\d-]+)'",
            body):
        pm, asset, desc, freq, crew, last, due = m.groups()
        rows[pm] = (asset, int(freq), crew, desc.replace("''", "'"), last, due)
    return rows


# Plans with NO seed CLOSE work order in 011 (so no seed priority to inherit):
# the live close-order policy is to carry the plan's standing priority. 0008 is
# on the critical story asset 1001-CMX-441 (priority-1 machine) and takes the
# story PM's own close priority (2, as work orders 1004/1096); 0015 is on the
# low-criticality tool presetter and takes 3 (the plant-cadence close priority).
_PLANT_PM_PRIORITY_POLICY = {"PM-1001-0008": 2, "PM-1001-0015": 3}


def _seed_close_order_priority():
    """pm_num -> priority of its CLOSE work order in the 011 ``work_orders``
    seed (the source the live CLOSE orders inherit their priority from). Plans
    with no seed CLOSE order are absent."""
    body = _SEED_011.read_text(encoding="utf-8").split("INSERT INTO work_orders VALUES", 1)[1]
    body = body.split("ON CONFLICT", 1)[0]
    priority = {}
    for m in re.finditer(
            r"\('\w+',\s*'[\w-]+',\s*'PM',\s*'(?:[^']|'')*',\s*'CLOSE',\s*(\d),\s*'(PM-[\w-]+)'", body):
        prio, pm = int(m.group(1)), m.group(2)
        priority.setdefault(pm, prio)
    return priority


def test_plant_pm_baseline_is_the_011_seed_rows_other_than_the_story_pm():
    """PLANT_PM_BASELINE is exactly the 011 pm_schedules rows (asset, frequency,
    crew, DESCRIPTION, frozen dates) OTHER than the story PM — so the alignment
    data (which the live CLOSE orders copy their text from) cannot drift from
    the seed it restores to."""
    seed_rows = _seed_pm_schedule_rows()
    assert shist.STORY_PM_NUM in seed_rows
    assert {b.pm_num for b in shist.PLANT_PM_BASELINE} == set(seed_rows) - {shist.STORY_PM_NUM}
    for b in shist.PLANT_PM_BASELINE:
        asset, freq, crew, desc, last, due = seed_rows[b.pm_num]
        assert (b.asset_num, b.frequency, b.crew, b.last_completion, b.next_due) == (
            asset, freq, crew, last, due)
        # the baseline description is the 011 seed PM's own description verbatim
        # (the live CLOSE order's maintenance text), so a seed-text change cannot
        # leave an attendee a schedule and its generated work order disagreeing
        assert b.description == desc, b.pm_num
        # the frozen interval the shift preserves
        assert date.fromisoformat(due) - date.fromisoformat(last) == timedelta(days=freq)


def test_plant_pm_baseline_priority_comes_from_the_seed_close_order():
    """``priority`` is not a pm_schedules column; each live CLOSE order inherits
    the priority of the plan's OWN CLOSE work order in the 011 seed. Plans with
    no seed CLOSE order follow the documented _PLANT_PM_PRIORITY_POLICY."""
    seed_close_priority = _seed_close_order_priority()
    for b in shist.PLANT_PM_BASELINE:
        if b.pm_num in seed_close_priority:
            assert b.priority == seed_close_priority[b.pm_num], b.pm_num
        else:
            assert b.pm_num in _PLANT_PM_PRIORITY_POLICY, b.pm_num
            assert b.priority == _PLANT_PM_PRIORITY_POLICY[b.pm_num], b.pm_num
    # the two policy plans are exactly the ones the seed has no CLOSE order for
    assert set(_PLANT_PM_PRIORITY_POLICY) == {b.pm_num for b in shist.PLANT_PM_BASELINE} - set(seed_close_priority)


def test_frozen_story_run_date_is_the_011_defect_scenario_day():
    """The shift anchor is the 011 seed's B-2847 run day (nowhere hard-coded as
    a bare literal elsewhere): read it from the seed header, where every
    non-story row's frozen dates straddle it."""
    text = _SEED_011.read_text(encoding="utf-8")
    assert "2026-02-10" in text and "batch B-2847, 2026-02-10" in text
    assert shist.FROZEN_STORY_RUN_DATE == date(2026, 2, 10)
    for b in shist.PLANT_PM_BASELINE:
        assert date.fromisoformat(b.last_completion) < shist.FROZEN_STORY_RUN_DATE < date.fromisoformat(b.next_due)


def test_plant_pm_rows_are_the_frozen_rows_shifted_onto_the_live_run():
    sp = shist.plan(ANCHOR)
    shift = sp.failed_run.start.date() - shist.FROZEN_STORY_RUN_DATE
    assert {p.pm_num for p in sp.plant_pm} == {b.pm_num for b in shist.PLANT_PM_BASELINE}
    for p, b in zip(sp.plant_pm, shist.PLANT_PM_BASELINE):
        assert p.pm_num == b.pm_num and p.asset_num == b.asset_num
        assert p.last_completion == date.fromisoformat(b.last_completion) + shift
        assert p.next_due == date.fromisoformat(b.next_due) + shift
        # the shift preserves the frozen interval exactly
        assert (p.next_due - p.last_completion).days == b.frequency


def test_the_story_pm_is_never_a_plant_pm_row():
    """The story PM (PM-1001-0007) has its OWN placement (3064/3101); it is not
    shifted by the plant alignment and never appears in plant_pm."""
    sp = shist.plan(ANCHOR)
    assert shist.STORY_PM_NUM not in {p.pm_num for p in sp.plant_pm}
    assert shist.STORY_PM_NUM not in {b.pm_num for b in shist.PLANT_PM_BASELINE}


_SWEEP_ANCHORS = [
    ANCHOR, ANCHOR + 1, ANCHOR - 1, ANCHOR + 12 * 3600, ANCHOR + shist.DAY - 1,
    ANCHOR + shist.DAY, ANCHOR + 7 * shist.DAY + 123, 1_800_000_000.0, 1_777_777_777.0,
]


@pytest.mark.parametrize("anchor", _SWEEP_ANCHORS)
@pytest.mark.parametrize("days", [1, 2, 3, 5, 9])
def test_every_aligned_plant_plan_straddles_the_run_for_any_anchor_and_history(anchor, days):
    """The same sweep the 3064 review used (day-boundary anchors x history
    lengths): every aligned non-story plan has its last completion BEFORE the
    live run and its next due AFTER it, so none reads months overdue, whatever
    the anchor, loop or history length. The CLOSE order's finish is the
    schedule's last completion (schedule and work order agree)."""
    sp = shist.plan(anchor, days)
    run_day = sp.failed_run.start.date()
    assert sp.plant_pm, "every live plan carries the non-story alignment"
    for p in sp.plant_pm:
        assert p.last_completion < run_day < p.next_due, (p.pm_num, p.last_completion, p.next_due)
        assert p.close.actual_finish.date() == p.last_completion
        assert p.close.target_start.date() == p.last_completion


@pytest.mark.parametrize("loop", ["3600", "10800", "28800"])
def test_plant_pm_straddles_the_run_under_a_packaged_loop(loop):
    """A packaged condition-monitor loop moves the run day; every aligned plan
    still straddles it (the shift tracks the run day, whatever the loop)."""
    out = run_under_loop(loop, """
from erp_simulation import condition_monitor as cm, story_history as shist
anchor = cm.snap_anchor(1.8e9, "down")
sp = shist.plan(anchor)
run_day = sp.failed_run.start.date().isoformat()
emit({"loop": cm.TOTAL_SECONDS, "run_day": run_day,
      "plant": [[p.pm_num, p.last_completion.isoformat(), p.next_due.isoformat()] for p in sp.plant_pm]})
""")
    assert out["loop"] == int(loop)
    run_day = date.fromisoformat(out["run_day"])
    assert len(out["plant"]) == len(shist.PLANT_PM_BASELINE)
    for _pm, last, due in out["plant"]:
        assert date.fromisoformat(last) < run_day < date.fromisoformat(due)


def test_plant_close_orders_follow_the_3064_pattern():
    """Each aligned plan's CLOSE work order is in the SAME shape as the story
    PM's last cycle (work order 3064): PM/CLOSE on the plan's pm_num, raised a
    week ahead, worked on its target day, the plan's own crew/priority/description."""
    sp = shist.plan(ANCHOR)
    done = next(wo for wo in sp.cmms_work_orders if wo.wonum == shist.LAST_PM_WONUM)  # 3064
    for p, b in zip(sp.plant_pm, shist.PLANT_PM_BASELINE):
        c = p.close
        assert (c.work_type, c.status, c.pm_num, c.failure_code) == ("PM", "CLOSE", b.pm_num, None)
        assert c.priority == b.priority and c.description == b.description
        assert "DEFERRED" not in c.description
        # the 3064 cadence: reported a week before target, worked and finished on the target day
        assert c.reported_date < c.target_start == c.sched_start <= c.actual_start < c.actual_finish
        assert (c.target_start - c.reported_date) == (done.target_start - done.reported_date)
        assert (c.actual_finish - c.target_start) == (done.actual_finish - done.target_start)
        assert len(c.wonum) <= 15 and len(c.description) <= 250


def test_plant_close_wonums_are_a_reserved_range_no_other_writer_produces():
    """The reserved CLOSE wonums collide with nothing: not the 011 seed
    (1004..1204), not the story history (3064/3101/3107/3108), not the live
    condition monitor (VIB-CMX-<loop>), not the MES bridge (IND-<hex>), and MES
    work orders are WO-<date>-<5 digits> (simulator 40000-79999, story 8xxxx /
    9xxxx), never site CMMS orders."""
    reserved = set(shist.PLANT_PM_CLOSE_WONUMS)
    assert len(reserved) == len(shist.PLANT_PM_CLOSE_WONUMS) == len(shist.PLANT_PM_BASELINE)
    seed_wonums = set(re.findall(r"^\('(\w+)',", _SEED_011.read_text(encoding="utf-8"), re.M))
    assert not reserved & seed_wonums
    assert not reserved & set(shist.HISTORY_WONUMS)
    assert not reserved & {shist.DEFERRED_PM_WONUM, shist.CONDITION_WONUM, shist.REPAIR_WONUM, shist.LAST_PM_WONUM}
    assert all(not w.startswith(shist.CONDITION_WONUM_PREFIX) for w in reserved)
    # every reserved wonum fits the Maximo WONUM VARCHAR(15) and is its own plan's
    assert all(len(w) <= 15 for w in reserved)
    assert [p.close.wonum for p in shist.plan(ANCHOR).plant_pm] == list(shist.PLANT_PM_CLOSE_WONUMS)


def test_plant_pm_close_orders_are_not_in_the_facilitator_answer_key():
    """The answer key stays the story orders only (3101/3107/3108): the plant
    CLOSE orders are routine history, never listed."""
    assert shist.STORY_ORDER_WONUMS == ("3101", "3107", "3108")
    assert not set(shist.PLANT_PM_CLOSE_WONUMS) & set(shist.STORY_ORDER_WONUMS)
    assert not set(shist.PLANT_PM_CLOSE_WONUMS) & set(shist.HISTORY_WONUMS)


def _lot_scope(sp):
    """(batches that drew the failed batch's lot, customers they shipped to)."""
    lot = sp.failed_run.lot_number
    runs = [r.run_number for r in (*sp.runs, sp.failed_run) if r.lot_number == lot]
    customers = {o.customer_code for o in (*sp.orders, *sp.run_orders) if o.batch_id in runs}
    return runs, customers


def test_the_failed_batch_lot_feeds_several_batches_but_not_every_batch_of_the_part():
    sp = shist.plan(ANCHOR)
    runs, customers = _lot_scope(sp)
    part = [r for r in (*sp.runs, sp.failed_run) if r.sku == shist.FAILED_SKU]
    assert sp.failed_run.lot_number == shist.SUSPECT_LOT_NUMBER
    assert all(r.lot_number != shist.PRODUCTS[0][1] for r in part)  # the db-init lot stays the simulator's
    assert 1 < len(runs) < len(part)
    assert set(shist.STORY_CUSTOMER_CODES) < customers      # the lot reaches one more customer


def test_each_history_lot_is_sized_to_its_runs_and_received_before_them():
    sp = shist.plan(ANCHOR)
    lots = {lot.lot_number: lot for lot in sp.lots}
    assert list(lots) == [shist.EARLIER_LOT_NUMBER, shist.SUSPECT_LOT_NUMBER]
    for number, lot in lots.items():
        drawn = [r for r in (*sp.runs, sp.failed_run) if r.lot_number == number]
        assert drawn and all(r.sku == shist.FAILED_SKU for r in drawn)
        assert (lot.item_code, lot.supplier_code) == (shist.FAILED_MATERIAL_CODE, shist.HISTORY_LOT_SUPPLIER)
        assert lot.quantity == round(sum(r.lot_quantity for r in drawn), 2)
        assert lot.received < min(r.start for r in drawn)


def test_the_suspect_lot_arrives_after_the_earlier_lot_is_used_up():
    sp = shist.plan(ANCHOR)
    earlier = [r for r in sp.runs if r.lot_number == shist.EARLIER_LOT_NUMBER]
    suspect = next(lot for lot in sp.lots if lot.lot_number == shist.SUSPECT_LOT_NUMBER)
    assert max(r.end for r in earlier) < suspect.received


def test_a_one_day_history_stays_on_one_lot():
    sp = shist.plan(ANCHOR, days=1)
    assert [lot.lot_number for lot in sp.lots] == [shist.SUSPECT_LOT_NUMBER]
    assert all(r.lot_number == shist.SUSPECT_LOT_NUMBER
               for r in (*sp.runs, sp.failed_run) if r.sku == shist.FAILED_SKU)


def test_config_rows_are_the_contract_keys():
    sp = shist.plan(ANCHOR, days=2)
    rows = {r.key: r for r in sp.config}
    assert set(rows) == CONTRACT_KEYS
    assert float(rows["story_anchor_epoch"].value) == ANCHOR
    geo = cm.story_geometry()
    assert int(rows["story_loop_seconds"].value) == geo["loop_seconds"]
    assert int(rows["story_bpfo_alarm_step"].value) == geo["bpfo_alarm_step"]
    assert int(rows["story_history_days"].value) == 2
    assert rows["story_failed_run_number"].value == sp.failed_run.run_number
    assert rows["story_deferred_pm_wonum"].value == shist.DEFERRED_PM_WONUM


def test_one_clock_config_rows_default_to_a_deploy_anchor_and_a_full_sweep():
    """The one-clock keys: provenance (``deploy`` unless told otherwise) and
    the telemetry sweep state (the minute step publishes from the grid point
    after the cursor, so the first sweep covers the whole history)."""
    rows = {r.key: r for r in shist.plan(ANCHOR, days=2).config}
    assert rows["story_anchor_source"].value == "deploy"
    assert float(rows["story_telemetry_anchor"].value) == ANCHOR
    cursor = int(rows["story_telemetry_cursor_epoch"].value)
    assert cursor == cm.grid_floor(ANCHOR - 2 * shist.DAY) - cm.TELEMETRY_GRID_SECONDS
    assert cursor % cm.TELEMETRY_GRID_SECONDS == 0
    assert rows["story_telemetry_anchor"].data_type == "float"


def test_telemetry_cursor_is_clamped_into_the_sitewise_window():
    """With the writer's ``now``, a history older than SiteWise's 7-day window
    starts the sweep 300 s inside it (never a point BatchPut rejects); a
    fresh era keeps its unclamped cursor; ``now=None`` stays pure."""
    grid = cm.TELEMETRY_GRID_SECONDS
    unclamped = cm.grid_floor(ANCHOR - 3 * shist.DAY) - grid
    assert shist.telemetry_cursor(ANCHOR, 3) == unclamped
    assert shist.telemetry_cursor(ANCHOR, 3, now=ANCHOR + 60) == unclamped      # inside the window
    for now in (ANCHOR + 5 * shist.DAY, ANCHOR + 5 * shist.DAY + 123.4, ANCHOR + 6.99 * shist.DAY):
        cursor = shist.telemetry_cursor(ANCHOR, 3, now=now)
        assert cursor == cm.grid_floor(now - 7 * shist.DAY + grid) > unclamped
        assert cursor % grid == 0 and cursor + grid >= now - 7 * shist.DAY + grid
        assert cursor > now - 7 * shist.DAY
    assert shist.SITEWISE_WINDOW_SECONDS == 7 * shist.DAY
    rows = {r.key: r.value for r in shist.plan(ANCHOR, 3, now=ANCHOR + 5 * shist.DAY).config}
    assert int(rows["story_telemetry_cursor_epoch"]) == shist.telemetry_cursor(ANCHOR, 3, now=ANCHOR + 5 * shist.DAY)


@pytest.mark.parametrize("source", shist.ANCHOR_SOURCES)
def test_plan_records_the_anchor_source(source):
    rows = {r.key: r.value for r in shist.plan(ANCHOR, source=source).config}
    assert rows["story_anchor_source"] == source


def test_plan_rejects_an_unknown_anchor_source():
    assert shist.ANCHOR_SOURCES == ("deploy", "packaged", "placed")
    with pytest.raises(ValueError, match="source"):
        shist.plan(ANCHOR, source="manual")


def test_default_keys_are_the_reset_failsafe_and_never_planned():
    """ADR-0091 Design C: the default anchor rides its own keys, outside the
    plan's config rows, so a placement (which upserts the plan) never moves it."""
    assert shist.DEFAULT_KEYS == ("story_default_anchor", "story_default_source")
    assert not set(shist.DEFAULT_KEYS) & {r.key for r in shist.plan(ANCHOR).config}


def test_config_keys_constant_is_the_planned_key_list():
    """``CONFIG_KEYS`` (what Delete clears) is exactly what the plan writes."""
    assert tuple(r.key for r in shist.plan(ANCHOR).config) == shist.CONFIG_KEYS
    assert set(shist.CONFIG_KEYS) == CONTRACT_KEYS


# ---------------------------------------------------------------------------
# Writer against a key-honouring fake DB
# ---------------------------------------------------------------------------

_KEYED = {"simulation_config", "production_runs", "mes_work_orders", "customer_orders", "material_lots"}
_WO_COLS = ("wonum", "asset_num", "work_type", "description", "status", "priority", "pm_num",
            "reported_by", "reported_date", "target_start", "sched_start", "actual_start",
            "actual_finish", "failure_code", "crew")
_PLAN_DATES = ("reported_date", "target_start", "sched_start", "actual_start", "actual_finish")


_SEED_011 = _REPO / "lambda-adt-seed" / "011_cmms_indianapolis.sql"
_SEED_WO_ROW = re.compile(
    r"\('(\w+)',\s*'([\w-]+)',\s*'(\w+)',\s*'((?:[^']|'')*)',\s*'(\w+)',\s*\d,\s*('[^']*'|NULL),"
    r"\s*'(?:[^']|'')*',\s*'([\d-]+ [\d:]+)'")


def _seed_011_work_orders():
    """The 011 ``work_orders`` rows as FakeDB rows (wonum -> row)."""
    body = _SEED_011.read_text(encoding="utf-8").split("INSERT INTO work_orders VALUES", 1)[1]
    body = body.split("ON CONFLICT", 1)[0]
    rows = {}
    for wonum, asset, work_type, description, status, pm_num, reported in _SEED_WO_ROW.findall(body):
        rows[wonum] = {"wonum": wonum, "asset_num": asset, "work_type": work_type,
                       "description": description.replace("''", "'"), "status": status,
                       "pm_num": None if pm_num == "NULL" else pm_num.strip("'"),
                       "reported_date": datetime.strptime(reported, "%Y-%m-%d %H:%M")}
    assert len(rows) == body.count("\n('"), "every 011 work order row parsed"
    return rows


def _seed_011_assets():
    """``(asset_num, asset_type)`` of every 011 ``assets`` row."""
    body = _SEED_011.read_text(encoding="utf-8").split("INSERT INTO assets VALUES", 1)[1]
    rows = re.findall(r"\('([\w-]+)',\s*'[^']*',\s*'\d+',\s*'[^']*',\s*'(\w+)'", body.split("ON CONFLICT", 1)[0])
    assert rows
    return rows


class FakeDB:
    """ERP rows by natural key; seed CMMS work orders / log modelled as rows."""

    def __init__(self, missing=(), fail_on=None, wappr_pms=(), work_orders=None, held=(), no_assets=False):
        self.keys, self.config, self.inserts = set(), {}, []
        self.rows = []  # (table, normalised INSERT text, params) of every row written
        self.missing, self.fail_on, self.held = set(missing), fail_on, set(held)
        self.no_assets = no_assets  # a site CMMS without an asset row
        self.work_orders = {w: {"wonum": w, "asset_num": cm.STORY_CMMS_ASSET_NUM, "work_type": "PM",
                                "description": f"frozen PM {w}", "status": "WAPPR",
                                "reported_date": datetime(2025, 12, 22, 7)} for w in wappr_pms}
        self.work_orders.update(work_orders or {})
        self.log = []  # (wonum, log_date, author, log_type, entry)
        self.superseded, self.deleted_keys = [], []
        self.commits = self.rollbacks = 0
        # 011 seed: the story PM's (last completion, next due) and every
        # non-story plant PM row's frozen dates (what the alignment moves).
        self.pm_schedules = {shist.STORY_PM_NUM: shist.FROZEN_PM_DATES}
        self.pm_schedules.update({b.pm_num: (b.last_completion, b.next_due)
                                  for b in shist.PLANT_PM_BASELINE})

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, db):
        self.db, self.rowcount, self._rows = db, 0, []
        self._seed_sql = {
            shist._UPSERT_CMMS_WORK_ORDER_SQL: self._upsert_wo,
            shist._DELETE_STALE_LOG_SQL: self._delete_stale_log,
            shist._INSERT_LOG_SQL: self._insert_log,
            shist._PURGE_CONDITION_LOG_SQL: self._purge_condition_log,
            shist._PURGE_CONDITION_WO_SQL: self._purge_condition_wo,
            shist._SUPERSEDE_FROZEN_ORDERS_SQL: self._supersede,
            shist._MARK_SUPERSEDED_SQL: self._mark_superseded,
            shist._MARK_PRIOR_ERA_SQL: self._mark_prior_era,
            shist._RESTORE_SUPERSEDED_SQL: self._restore,
            shist._RESTORE_PRIOR_ERA_SQL: self._restore_prior_era,
            shist._DELETE_HISTORY_LOG_SQL: self._delete_history_log,
            shist._DELETE_HISTORY_WO_SQL: self._delete_history_wo,
            shist._ALIGN_PM_SCHEDULE_SQL: self._align_pm_schedule,
            shist._SUPERSEDE_PLANT_PM_ORDERS_SQL: self._supersede_plant_pm,
            shist._MARK_PLANT_PM_SUPERSEDED_SQL: self._mark_plant_pm_superseded,
            shist._DELETE_PLANT_PM_LOG_SQL: self._delete_plant_pm_log,
            shist._DELETE_PLANT_PM_WO_SQL: self._delete_plant_pm_wo,
            shist._DELETE_PAST_STOP_LOG_SQL: self._delete_past_stop_log,
            shist._DELETE_PAST_STOP_WO_SQL: self._delete_past_stop_wo,
            shist._FOREIGN_PAST_STOP_ORDERS_SQL: self._foreign_past_stop_orders,
        }

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if self.db.fail_on and self.db.fail_on in text:
            raise RuntimeError("boom")
        self.rowcount = 0
        if sql in self._seed_sql:
            self._seed_sql[sql](params)
        elif sql == shist._HELD_ELSEWHERE_SQL:
            # ``held``: run numbers another run holds (number or work-order number)
            self._rows = [(wo, run) for wo, run in zip(*params) if run in self.db.held]
        elif sql == shist._PAST_STOP_HOSTS_SQL:
            # the fake keeps no MES work-order ids; the OEE alignment runs on PostgreSQL
            self._rows = []
        elif sql == shist._DELETE_OMITTED_PAST_STOPS_SQL:
            # nor MES downtime rows: the omitted-stop delete runs on PostgreSQL
            self._rows = []
        elif text.startswith("SELECT config_value FROM simulation_config"):
            value = self.db.config.get(params[0])
            self._rows = [(value,)] if value is not None else []
        elif text.startswith(f"SELECT asset_num FROM {shist.CMMS_SCHEMA}.assets WHERE asset_num LIKE"):
            # the bridge's asset pick over the 011 assets: preferred type first, then by number
            prefix, preferred = params[0].rstrip("%"), params[1]
            picks = sorted((a for a in _seed_011_assets() if a[0].startswith(prefix)),
                           key=lambda a: (a[1] != preferred, a[0]))
            self._rows = [(picks[0][0],)] if picks and not self.db.no_assets else []
        elif m := re.match(r"SELECT (\w+) FROM \w+ WHERE \1 = ANY", text):
            self._rows = [(k,) for k in params[0] if k not in self.db.missing]
        elif m := re.match(r"INSERT INTO ([\w.]+)", text):
            self._insert(m.group(1), text, params)
        elif text.startswith("DELETE FROM simulation_config WHERE config_key = ANY"):
            gone = [k for k in params[0] if k in self.db.config]
            for k in gone:
                del self.db.config[k]
            self.db.deleted_keys.append(tuple(params[0]))
            self.rowcount = len(gone)
        else:
            raise AssertionError(f"unexpected SQL: {text[:80]}")

    def _insert(self, table, text, params):
        # An ON CONFLICT insert is keyed on its first value (every such
        # statement the writer issues leads with its natural key).
        keyed = table in _KEYED or "ON CONFLICT" in text
        key = (table, params[0]) if keyed else (table, params)
        new = not keyed or key not in self.db.keys
        if new:
            self.db.keys.add(key)
            self.db.inserts.append(table)
            self.db.rows.append((table, text, tuple(params)))
            if table == "simulation_config":
                self.db.config[params[0]] = params[1]
        self.rowcount = 1 if new else 0
        self._rows = [(str(uuid.uuid4()),)] if new and "RETURNING" in text else []

    # -- seed CMMS ----------------------------------------------------------
    def _upsert_wo(self, params):
        row = dict(zip(_WO_COLS, params))
        old = self.db.work_orders.get(row["wonum"])
        if old is None:
            self.db.work_orders[row["wonum"]] = row
            self.db.inserts.append("work_orders")
            self.rowcount = 1
        elif any(old.get(c) != row[c] for c in _PLAN_DATES):
            old.update({c: row[c] for c in ("status", *_PLAN_DATES)})
            self.rowcount = 1

    def _delete_stale_log(self, params):
        wonum, log_type, *current = params
        keep = [r for r in self.db.log
                if not (r[0] == wonum and r[3] == log_type and [r[1], r[2], r[4]] != current)]
        self.rowcount, self.db.log = len(self.db.log) - len(keep), keep

    def _insert_log(self, params):
        if not any(r[0] == params[5] and r[3] == params[6] for r in self.db.log):
            self.db.log.append(tuple(params[:5]))
            self.db.inserts.append("work_order_log")
            self.rowcount = 1

    def _condition_scope(self, params):
        asset, like, before = params
        assert like == "VIB-CMX-%"
        return {w for w, r in self.db.work_orders.items()
                if r["asset_num"] == asset and w.startswith(like[:-1]) and r["reported_date"] < before}

    def _purge_condition_log(self, params):
        self._drop_log(self._condition_scope(params))

    def _purge_condition_wo(self, params):
        self._drop_wo(self._condition_scope(params))

    def _frozen_scope(self, p):
        return {w for w, r in self.db.work_orders.items()
                if r["asset_num"] == p["asset"] and r["reported_date"] is not None
                and r["reported_date"] < p["anchor"] and w not in p["history"]
                and not w.startswith(p["condition"].rstrip("%"))}

    def _supersede(self, p):
        prior_entry = dict(zip(p["priors"], p["entries"]))
        hit = sorted(w for w in self._frozen_scope(p) if self.db.work_orders[w]["status"] in prior_entry)
        for wonum in hit:
            row = self.db.work_orders[wonum]
            entry = prior_entry[row["status"]]
            row["status"] = "CAN"
            self.db.log.append((wonum, p["log_date"], p["author"], "UPDATE", entry))
            self.db.superseded.append((wonum, p["log_date"], p["author"], entry))
        self.rowcount = len(hit)

    def _mark_superseded(self, p):
        logged = {r[0] for r in self.db.log if r[4] in p["entries"]}
        for wonum in logged:
            row = self.db.work_orders.get(wonum)
            if not row or row["asset_num"] != p["asset"] or row["status"] != "CAN":
                continue
            text = row["description"] + p["suffix"]
            if not row["description"].endswith(p["suffix"]) and len(text) <= p["width"]:
                row["description"] = text
                self.rowcount += 1

    def _mark_prior_era(self, p):
        for wonum in self._frozen_scope(p):
            row = self.db.work_orders[wonum]
            text = p["prefix"] + row["description"]
            if (row["status"] == "COMP" and not row["description"].startswith(p["prefix"])
                    and len(text) <= p["width"]):
                row["description"] = text
                self.rowcount += 1

    def _restore(self, p):
        entry_prior = dict(zip(p["entries"], p["priors"]))
        logged = {r[0]: entry_prior[r[4]] for r in self.db.log if r[4] in entry_prior}
        restored = {w for w in logged if self.db.work_orders.get(w, {}).get("status") == "CAN"}
        for wonum in restored:
            row = self.db.work_orders[wonum]
            row["status"] = logged[wonum]
            if row["description"].endswith(p["suffix"]):
                row["description"] = row["description"][:-len(p["suffix"])]
        before = len(self.db.log)
        self.db.log = [r for r in self.db.log if not (r[4] in entry_prior and r[0] in restored)]
        self.rowcount = before - len(self.db.log)

    def _restore_prior_era(self, p):
        for row in self.db.work_orders.values():
            if row["asset_num"] == p["asset"] and row["description"].startswith(p["prefix"]):
                row["description"] = row["description"][len(p["prefix"]):]
                self.rowcount += 1

    def _history_scope(self, params):
        wonums, asset = set(params[0]), params[1]
        return {w for w, r in self.db.work_orders.items() if w in wonums and r["asset_num"] == asset}

    def _delete_history_log(self, params):
        self._drop_log(self._history_scope(params))

    def _delete_history_wo(self, params):
        self._drop_wo(self._history_scope(params))

    def _align_pm_schedule(self, params):
        last, due, pm_num, asset = params[:4]
        if pm_num in self.db.pm_schedules and self.db.pm_schedules[pm_num] != (last, due):
            self.db.pm_schedules[pm_num] = (last, due)
            self.rowcount = 1

    def _plant_pm_open_scope(self, p):
        """CAN candidates: open rows whose pm_num is an aligned plant plan's,
        reported before the anchor, not a reserved plant CLOSE order."""
        return {w for w, r in self.db.work_orders.items()
                if r.get("pm_num") in set(p["pm_nums"]) and r["status"] in shist.OPEN_STATUSES
                and r.get("reported_date") is not None and r["reported_date"] < p["anchor"]
                and w not in set(p["close_wonums"])}

    def _supersede_plant_pm(self, p):
        prior_entry = dict(zip(p["priors"], p["entries"]))
        hit = sorted(w for w in self._plant_pm_open_scope(p)
                     if self.db.work_orders[w]["status"] in prior_entry)
        for wonum in hit:
            row = self.db.work_orders[wonum]
            entry = prior_entry[row["status"]]
            row["status"] = "CAN"
            self.db.log.append((wonum, p["log_date"], p["author"], "UPDATE", entry))
            self.db.superseded.append((wonum, p["log_date"], p["author"], entry))
        self.rowcount = len(hit)

    def _mark_plant_pm_superseded(self, p):
        logged = {r[0] for r in self.db.log if r[4] in p["entries"]}
        for wonum in logged:
            row = self.db.work_orders.get(wonum)
            if (not row or row.get("pm_num") not in set(p["pm_nums"]) or row["status"] != "CAN"
                    or wonum in set(p["close_wonums"])):
                continue
            text = row["description"] + p["suffix"]
            if not row["description"].endswith(p["suffix"]) and len(text) <= p["width"]:
                row["description"] = text
                self.rowcount += 1

    def _delete_plant_pm_log(self, params):
        self._drop_log(set(params[0]) & set(self.db.work_orders))

    def _delete_plant_pm_wo(self, params):
        self._drop_wo({w for w in params[0] if w in self.db.work_orders})

    def _past_stop_orders(self, p, owned):
        """The past-stop numbers in ``p`` whose order the history owns (the
        bridge reported it and its description ends in that stop's id), or,
        ``owned=False``, whose order it does not."""
        ids = dict(zip(p["wonums"], p["ids"]))
        return {w for w, r in self.db.work_orders.items() if w in ids
                and (r.get("reported_by") == p["reporter"]
                     and str(r.get("description") or "").endswith(ids[w])) == owned}

    def _delete_past_stop_log(self, p):
        self._drop_log(self._past_stop_orders(p, owned=True))

    def _delete_past_stop_wo(self, p):
        self._drop_wo(self._past_stop_orders(p, owned=True))

    def _foreign_past_stop_orders(self, p):
        self._rows = [(w,) for w in sorted(self._past_stop_orders(p, owned=False))]

    def _drop_log(self, wonums):
        before = len(self.db.log)
        self.db.log = [r for r in self.db.log if r[0] not in wonums]
        self.rowcount = before - len(self.db.log)

    def _drop_wo(self, wonums):
        assert not any(r[0] in wonums for r in self.db.log), "FK: log rows first"
        for wonum in wonums:
            del self.db.work_orders[wonum]
        self.rowcount = len(wonums)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _condition_order(wonum, reported):
    return {"wonum": wonum, "asset_num": cm.STORY_CMMS_ASSET_NUM, "work_type": "CM",
            "description": "condition order", "status": "WAPPR", "reported_date": reported}


_SEED_COUNTS_NOOP = {"cmms_work_orders": 0, "cmms_log": 0, "superseded_orders": 0,
                     "prior_era_marked": 0, "purged_condition_orders": 0, "pm_schedule": 0,
                     "plant_pm_work_orders": 0, "plant_pm_schedule": 0, "superseded_plant_orders": 0,
                     "past_stop_orders": 0, "past_stop_log": 0}


def test_write_inserts_everything_then_nothing():
    erp, seed = FakeDB(), FakeDB()
    first = shist.write(erp, seed, shist.plan(ANCHOR))
    sp = shist.plan(ANCHOR)
    assert first["anchor_epoch"] == ANCHOR and first["failed_run_number"] == sp.failed_run.run_number
    assert first["erp"] == {"config": len(shist.CONFIG_KEYS), "runs": len(sp.runs) + 1, "orders": 3 + len(sp.runs),
                            "past_stops": len(shist.PAST_STOPS)}
    assert first["seed"] == {**_SEED_COUNTS_NOOP, "cmms_work_orders": 4, "cmms_log": 2,
                             "pm_schedule": 1, "plant_pm_work_orders": len(shist.PLANT_PM_BASELINE),
                             "plant_pm_schedule": len(shist.PLANT_PM_BASELINE),
                             "past_stop_orders": len(shist.PAST_STOPS), "past_stop_log": len(shist.PAST_STOPS)}
    assert seed.pm_schedules[shist.STORY_PM_NUM] == shist._pm_schedule_dates(sp)
    # every non-story plant PM row moved onto the live run (shifted off its 011 dates)
    for p in sp.plant_pm:
        assert seed.pm_schedules[p.pm_num] == (p.last_completion.isoformat(), p.next_due.isoformat())
        assert seed.work_orders[p.close.wonum]["status"] == "CLOSE"
    assert erp.inserts.count("mes_quality_checks") == 2 * len(sp.runs) + 3
    assert erp.inserts.count("mes_material_consumption") == len(sp.runs) + 1
    assert erp.inserts.count("material_lots") == len(sp.lots) == 2
    assert erp.inserts.count("mes_downtime_events") == 1 + len(shist.PAST_STOPS)
    assert erp.inserts.count("shipment_items") == 3 + len(sp.runs)
    before = (len(erp.inserts), len(seed.inserts))
    second = shist.write(erp, seed, shist.plan(ANCHOR))
    assert second["erp"] == {"config": 0, "runs": 0, "orders": 0, "past_stops": 0}
    assert second["seed"] == _SEED_COUNTS_NOOP
    assert (len(erp.inserts), len(seed.inserts)) == before
    assert erp.rollbacks == seed.rollbacks == 0


# The Resolve step's evidence bar for a measured identity rule
# (agentic_digital_thread/agents/resolve_gates.py MIN_SHARED_VALUES,
# MIN_OVERLAP_RATIO): distinct shared values, and their share of the smaller
# side's distinct values (resolve_evidence.value_overlap).
_RESOLVE_MIN_SHARED, _RESOLVE_MIN_RATIO = 3, 0.5


def _split_top_level(exprs: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in exprs:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        cur += ch
    return out + [cur.strip()]


_INSERT_FORM = re.compile(r"INSERT INTO [\w.]+(?: AS \w+)? \(([^)]*)\) "
                          r"(?:VALUES \((.*?)\)(?= ON CONFLICT| RETURNING|$)|SELECT (.*?) FROM )")


def _written(db, table: str, column: str) -> list:
    """Every value the writer put in ``table.column`` through a VALUES or an
    INSERT ... SELECT statement (a ``%s`` expression in that column's place)."""
    values = []
    for written_table, text, params in db.rows:
        if written_table != table:
            continue
        m = _INSERT_FORM.match(text)
        assert m, f"unparsed insert: {text[:100]}"
        columns = [c.strip() for c in m.group(1).split(",")]
        at = 0
        for col, expr in zip(columns, _split_top_level(m.group(2) or m.group(3))):
            if col == column and expr == "%s":
                values.append(params[at])
            at += expr.count("%s")
    return values


def _seed_011_failure_codes():
    body = _SEED_011.read_text(encoding="utf-8").split("INSERT INTO work_orders VALUES", 1)[1]
    body = body.split("ON CONFLICT", 1)[0]
    return set(re.findall(r",\s*'([\w-]+)',\s*'[\w-]+'\),?\s*$", body, re.M))


def _overlap(a: set, b: set):
    shared = a & b
    return shared, len(shared) / (min(len(a), len(b)) or 1)


def _fresh_account(days: int = 3):
    """ERP + site CMMS right after the history ran on a fresh account (story
    loop 0: the simulator has completed no order yet)."""
    erp = FakeDB()
    seed = FakeDB(work_orders={w: dict(r) for w, r in _seed_011_work_orders().items()})
    shist.write(erp, seed, shist.plan(ANCHOR, days))
    return erp, seed


# The deployed history is three days (the CR's Days); one day carries four
# stops (its four runs of the other parts on lines 1 and 2), longer ones six.
_FRESH_DAYS = [1, 2, 3, 9]


@pytest.mark.parametrize("days", _FRESH_DAYS)
def test_a_fresh_account_shares_technician_logins_between_the_cmms_log_and_the_mes(days):
    """Module 4's operator rule (CMMS ``work_order_log.author`` to the
    Indianapolis MES ``downtime_events.resolved_by``) needs the logins of the
    technicians who resolved past stops in BOTH systems from the first minute:
    the simulator writes them to the CMMS log only when it completes a bridge
    order after its loop ended, and an event account reaches module 4 in loop 0."""
    erp, seed = _fresh_account(days)
    authors = {r[2] for r in seed.log}
    resolved = set(_written(erp, "mes_downtime_events", "resolved_by"))
    shared, ratio = _overlap(authors, resolved)
    assert len(shared) >= _RESOLVE_MIN_SHARED and ratio >= _RESOLVE_MIN_RATIO, (sorted(authors), sorted(resolved))


@pytest.mark.parametrize("days", _FRESH_DAYS)
def test_a_fresh_account_shares_failure_codes_between_the_cmms_and_the_mes(days):
    """The failure-mode rule (CMMS ``work_orders.failure_code`` to MES
    ``downtime_events.downtime_reason``) measures the reason codes the MES
    bridge copies into the orders it raises; a fresh account must already hold
    the plant's past bridged stops, not only the story's BEARING/VIB-BEARING."""
    erp, seed = _fresh_account(days)
    codes = {r.get("failure_code") for r in seed.work_orders.values()} - {None} | _seed_011_failure_codes()
    reasons = set(_written(erp, "mes_downtime_events", "downtime_reason"))
    shared, ratio = _overlap(codes, reasons)
    assert len(shared) >= _RESOLVE_MIN_SHARED and ratio >= _RESOLVE_MIN_RATIO, (sorted(codes), sorted(reasons))


def _sim():
    from erp_simulation.mes_simulator import MESSimulator
    return MESSimulator


@pytest.mark.parametrize("days", [1, 2, 3, 9])
def test_past_stops_are_routine_stops_on_the_earliest_runs_off_the_story_line(days):
    """Each past stop sits inside one of the earliest history runs on line 1
    or 2 of a part other than the failed one, one per run (so on no batch the
    story traces); its reason is one the random downtime step draws at the
    site for its category (never the story mill's), and the bridged
    categories only."""
    sim, sp = _sim(), shist.plan(ANCHOR, days)
    hosts = [r for r in sp.runs if r.line != cm.STORY_MES_LINE
             and r.sku != shist.FAILED_SKU][:len(shist.PAST_STOPS)]
    assert [s.run_number for s in sp.past_stops] == [r.run_number for r in hosts]
    assert len(sp.past_stops) == (4 if days == 1 else len(shist.PAST_STOPS))
    traced = {sp.failed_run.run_number} | {r.run_number for r in sp.runs if r.sku == shist.FAILED_SKU}
    assert traced.isdisjoint(s.run_number for s in sp.past_stops)
    reasons = sim._build_downtime_reasons()
    for stop, run in zip(sp.past_stops, hosts):
        assert (stop.work_order_number, stop.line) == (run.work_order_number, run.line)
        assert run.start < stop.start < stop.end < run.end and _epoch(stop.end) < ANCHOR
        assert stop.category in ("MECHANICAL", "ELECTRICAL")
        assert stop.reason in reasons[stop.category] and stop.reason not in sim.STORY_FAILURE_REASONS
        assert stop.resolved_by in sim.DOWNTIME_RESOLVERS
        assert stop.notes == f"Resolved after {shist.downtime_length_text(int(_epoch(stop.end) - _epoch(stop.start)))}"
        assert stop.start.microsecond == stop.end.microsecond == 0
    assert len({s.reason for s in sp.past_stops}) == len(sp.past_stops)


def test_past_stops_name_every_technician_and_every_bridged_reason_the_site_draws():
    sim, sp = _sim(), shist.plan(ANCHOR)
    assert {s.resolved_by for s in sp.past_stops} == set(sim.DOWNTIME_RESOLVERS)
    drawn = {r for c in ("MECHANICAL", "ELECTRICAL") for r in sim._build_downtime_reasons()[c]}
    assert {s.reason for s in sp.past_stops} == drawn - sim.STORY_FAILURE_REASONS


def test_the_random_downtime_step_draws_its_resolver_from_the_same_logins(monkeypatch):
    from erp_simulation import mes_simulator as ms
    seen = []
    monkeypatch.setattr(ms.random, "choice", lambda seq: seen.append(tuple(seq)) or seq[0])

    class _Cursor:
        def execute(self, sql, params):
            self.params = params

        def fetchone(self):
            return ("downtime-id",)
    cur = _Cursor()
    ms.MESSimulator.__new__(ms.MESSimulator)._insert_downtime_event(
        cur, "wo", "LINE1", "MECHANICAL", "MECH-JAM", None, None, 600)
    assert seen == [ms.MESSimulator.DOWNTIME_RESOLVERS] and cur.params[6] == ms.MESSimulator.DOWNTIME_RESOLVERS[0]


def test_past_stop_ids_and_order_numbers_do_not_move_with_the_anchor():
    """A rewrite on a new anchor upserts the same bridge orders (their dates
    move); the ids are UUIDs no other writer issues (version 5, the
    simulator's gen_random_uuid is version 4)."""
    first, later = shist.plan(ANCHOR), shist.plan(NEW_ANCHOR + 3600)
    assert [s.downtime_id for s in first.past_stops] == [s.downtime_id for s in later.past_stops]
    assert [s.order.wonum for s in first.past_stops] == list(shist.PAST_STOP_WONUMS)
    assert [s.order.wonum for s in later.past_stops] == list(shist.PAST_STOP_WONUMS)
    assert [s.start for s in first.past_stops] != [s.start for s in later.past_stops]
    assert all(uuid.UUID(i).version == 5 for i in shist.PAST_STOP_DOWNTIME_IDS)
    assert len(set(shist.PAST_STOP_WONUMS)) == len(shist.PAST_STOPS)
    for wonum in shist.PAST_STOP_WONUMS:
        assert len(wonum) <= 15 and wonum not in (*shist.HISTORY_WONUMS, *shist.PLANT_PM_CLOSE_WONUMS)


def test_each_past_stop_order_is_what_the_simulator_completes_from_its_stop():
    """The pair is the one ``complete_bridge_work_orders`` makes once a loop
    has ended: the order is a PROVEN bridge order of its stop, and completing
    it from the stop gives exactly the planned dates and the resolver as the
    COMPLETE entry's author."""
    sim, sp = _sim(), shist.plan(ANCHOR)
    for stop in sp.past_stops:
        wo, log = stop.order, stop.log
        assert sim._proven_bridge_downtime_id(wo.wonum, wo.description, wo.reported_by) == stop.downtime_id
        assert wo.description == sim.indianapolis_bridge_order(
            shist.SITE, stop.line, stop.category, stop.reason, stop.downtime_id)[3]
        plan, unproven = sim._bridge_completions(
            [(wo.wonum, wo.description, wo.reported_date, wo.reported_by, wo.actual_start)],
            lambda ids: {stop.downtime_id: (stop.start, stop.end, stop.resolved_by)}, ANCHOR)
        assert unproven == [] and plan == [(wo.wonum, wo.actual_start, wo.actual_finish, log.author,
                                            wo.description)]
        assert (wo.status, wo.pm_num, wo.failure_code) == (sim.CONDITION_COMPLETED_STATUS, None, stop.reason)
        assert (wo.work_type, wo.priority) == (("EM", 1) if stop.category == "MECHANICAL" else ("CM", 2))
        # The bridge stamps the order when it sees the stop (its NOW()), and the
        # completion dates the log at GREATEST(actual_finish, reported_date).
        assert wo.reported_date == wo.target_start == wo.sched_start == stop.observed
        assert (wo.actual_start, wo.actual_finish) == (stop.start, stop.end)
        assert (log.wonum, log.log_date, log.log_type, log.entry) == (
            wo.wonum, max(wo.actual_finish, wo.reported_date), "COMPLETE", sim.BRIDGE_COMPLETION_ENTRY)


@pytest.mark.parametrize("days", [1, 3])
def test_past_stops_are_seen_by_the_bridge_after_they_start_and_inside_their_run(days):
    """The live step draws a stop's start between its run's start and the
    cycle that inserts it, and the bridge raises the order in that cycle: so
    the order is reported at or after the stop's start, sometimes before the
    stop ends and sometimes after, and while its run is still running."""
    sp = shist.plan(ANCHOR, days)
    runs = {r.run_number: r for r in sp.runs}
    for stop in sp.past_stops:
        assert stop.start <= stop.observed < runs[stop.run_number].end
        assert stop.observed.microsecond == 0
    seen = {stop.observed < stop.end for stop in shist.plan(ANCHOR).past_stops}
    assert seen == {True, False}


def test_history_oee_keeps_the_inserted_figures_without_downtime():
    """With no downtime the figures are exactly what ``_write_run`` inserts:
    availability 100.00 and OEE 0.95 x quality."""
    for run in shist.plan(ANCHOR).runs:
        quality = 100.0 * run.good / run.planned
        assert shist._history_oee(shist.RUN_SECONDS / 60, 0.0, 95.0, run.good, run.planned) == (
            100.0, round(0.95 * quality, 2))


def test_history_oee_takes_the_simulators_availability_for_a_stop():
    """A stop costs the run availability by the simulator's own formula
    (``compute_oee_components``), and OEE moves with it."""
    from erp_simulation.mes_simulator import compute_oee_components
    run, minutes = shist.plan(ANCHOR).runs[0], 1310 / 60
    avail = compute_oee_components(shist.RUN_SECONDS / 60, minutes, 0, 0, 0, 0)[0]
    assert shist._history_oee(shist.RUN_SECONDS / 60, minutes, 95.0, run.good, run.planned) == (
        round(avail * 100, 2), round(avail * 95.0 * run.good / run.planned, 2))
    assert shist._history_oee(shist.RUN_SECONDS / 60, 500.0, 95.0, run.good, run.planned) == (0.0, 0.0)


def test_without_held_stops_drops_only_the_stop_whose_run_another_run_holds():
    sp = shist.plan(ANCHOR)
    held = sp.past_stops[2].run_number
    out = shist.without_held_stops(FakeDB(held={held}).cursor(), sp)
    assert [s.run_number for s in out.past_stops] == [s.run_number for s in sp.past_stops if s.run_number != held]
    assert out.runs == sp.runs and out.cmms_work_orders == sp.cmms_work_orders
    assert shist.without_held_stops(FakeDB().cursor(), sp) is sp


def test_a_seed_write_removes_the_past_stop_orders_its_plan_no_longer_holds():
    """The CMMS side mirrors the plan: an order an earlier write left for a
    stop this plan does not carry (its run is now held elsewhere, or the
    history is shorter) goes, with its log; the others stay."""
    sp, seed = shist.plan(ANCHOR), FakeDB()
    shist.write(FakeDB(), seed, sp)
    gone = sp.past_stops[1]
    shist.write(FakeDB(held={gone.run_number}), seed, sp)
    assert gone.order.wonum not in seed.work_orders
    assert not any(r[0] == gone.order.wonum for r in seed.log)
    for stop in sp.past_stops:
        if stop is not gone:
            assert stop.order.wonum in seed.work_orders
    short = shist.plan(ANCHOR, 1)
    shist.write(FakeDB(), seed, short)
    assert {w for w in shist.PAST_STOP_WONUMS if w in seed.work_orders} == {s.order.wonum for s in short.past_stops}


def _colliding_live_bridge_order(i: int):
    """A live bridge order (another MES stop, a version-4 id) whose number
    collides with past stop ``i``'s: the bridge keeps 8 hex digits of the id."""
    sim, own = _sim(), shist.PAST_STOP_DOWNTIME_IDS[i]
    other = own[:9] + ("0" if own[9] != "0" else "1") + own[10:]
    wonum, work_type, priority, description, code = sim.indianapolis_bridge_order(
        shist.SITE, "LINE1", "MECHANICAL", "MECH-JAM", other)
    assert wonum == shist.PAST_STOP_WONUMS[i] and other != own
    return {"wonum": wonum, "asset_num": "1001-CGR-502", "work_type": work_type, "description": description,
            "status": "WAPPR", "priority": priority, "pm_num": None,
            "reported_by": sim.CMMS_BRIDGE_REPORTED_BY, "reported_date": datetime(2026, 9, 1, 8),
            "target_start": datetime(2026, 9, 1, 8), "sched_start": datetime(2026, 9, 1, 8),
            "actual_start": None, "actual_finish": None, "failure_code": code, "crew": shist.CREW}


def test_a_live_bridge_order_with_a_colliding_number_survives_the_stale_cleanup_and_restore():
    """The cleanup removes only the history's own bridge orders (their
    description proves the past stop's full id), never a live order that
    shares the 8-digit number."""
    live = _colliding_live_bridge_order(5)
    seed = FakeDB(work_orders={live["wonum"]: dict(live)})
    seed.log.append((live["wonum"], datetime(2026, 9, 1, 9), "maint_01", "UPDATE", "live entry"))
    shist.write(FakeDB(), seed, shist.plan(ANCHOR, 1))          # stop 5 is not in a one-day history
    assert seed.work_orders[live["wonum"]] == live
    assert (live["wonum"], datetime(2026, 9, 1, 9), "maint_01", "UPDATE", "live entry") in seed.log
    shist.restore_frozen_seed(seed)
    assert seed.work_orders[live["wonum"]] == live
    assert any(r[0] == live["wonum"] for r in seed.log)
    assert not any(w in seed.work_orders for w in shist.PAST_STOP_WONUMS[:4])


def test_a_live_bridge_order_holding_a_planned_stops_number_fails_the_seed_write_loudly():
    """A planned past stop whose number a live order holds is never written
    over it (that would rewrite the live order's dates and status)."""
    live = _colliding_live_bridge_order(2)
    erp, seed = FakeDB(), FakeDB(work_orders={live["wonum"]: dict(live)})
    with pytest.raises(RuntimeError, match="another order"):
        shist.write(erp, seed, shist.plan(ANCHOR))
    assert seed.rollbacks == 1 and seed.work_orders[live["wonum"]] == live


def test_past_stop_orders_land_on_the_bridges_asset_never_the_story_mill():
    erp, seed = _fresh_account()
    for wonum in shist.PAST_STOP_WONUMS:
        row = seed.work_orders[wonum]
        assert row["asset_num"] != cm.STORY_CMMS_ASSET_NUM and row["crew"] == shist.CREW
        assert row["reported_by"] == _sim().CMMS_BRIDGE_REPORTED_BY and row["status"] == "COMP"
        assert not row["description"].startswith(shist.PRIOR_ERA_PREFIX)


def test_a_run_another_run_holds_keeps_its_stop_out_of_both_systems():
    sp = shist.plan(ANCHOR)
    held = sp.past_stops[1].run_number
    erp, seed = FakeDB(held={held}), FakeDB()
    out = shist.write(erp, seed, sp)
    assert out["erp"]["past_stops"] == out["seed"]["past_stop_orders"] == len(sp.past_stops) - 1
    assert sp.past_stops[1].downtime_id not in _written(erp, "mes_downtime_events", "downtime_id")
    assert sp.past_stops[1].order.wonum not in seed.work_orders
    assert not any(r[0] == sp.past_stops[1].order.wonum for r in seed.log)


def test_a_site_cmms_without_assets_fails_the_seed_write_loudly():
    erp, seed = FakeDB(), FakeDB(no_assets=True)
    with pytest.raises(RuntimeError, match="no asset"):
        shist.write(erp, seed, shist.plan(ANCHOR))
    assert seed.rollbacks == 1 and erp.commits == 1


def test_rewrite_on_a_new_anchor_moves_the_past_stop_orders():
    seed = FakeDB()
    shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    shist.write(FakeDB(), seed, shist.plan(NEW_ANCHOR))
    later = shist.plan(NEW_ANCHOR)
    for stop in later.past_stops:
        row = seed.work_orders[stop.order.wonum]
        assert (row["status"], *(row[c] for c in _PLAN_DATES)) == (
            stop.order.status, *(getattr(stop.order, c) for c in _PLAN_DATES))
        assert [r for r in seed.log if r[0] == stop.order.wonum] == [
            (stop.log.wonum, stop.log.log_date, stop.log.author, stop.log.log_type, stop.log.entry)]


def test_write_retires_the_frozen_era_on_the_story_asset_once():
    """011 on the story asset: open orders (1182 WAPPR, 1201 APPR) -> CAN with
    a prior-status log entry and the superseded suffix; completed orders
    (1187, 1195) keep their status under the prior-era prefix; CLOSE'd cadence
    PMs, other assets and the history's own PM (3101) are untouched; a second
    run changes nothing."""
    frozen = _seed_011_work_orders()
    erp, seed = FakeDB(), FakeDB(work_orders={w: dict(r) for w, r in frozen.items()})
    first = shist.write(erp, seed, shist.plan(ANCHOR))
    assert (first["seed"]["superseded_orders"], first["seed"]["prior_era_marked"]) == (2, 2)
    assert first["seed"]["superseded_plant_orders"] == 1          # 011's PM 1204 (1001-CVA-100)
    wos = seed.work_orders
    story_superseded = [(w, e) for w, _d, _a, e in seed.superseded if w in ("1182", "1201")]
    assert story_superseded == [
        ("1182", "Superseded by the live plant history (status was WAPPR)"),
        ("1201", "Superseded by the live plant history (status was APPR)")]
    # 1204 (a NON-story-asset open PM for an aligned plan) is retired the same way
    assert ("1204", "Superseded by the live plant history (status was WAPPR)") in [
        (w, e) for w, _d, _a, e in seed.superseded]
    assert wos["1204"]["status"] == "CAN"
    assert wos["1204"]["description"] == frozen["1204"]["description"] + shist.SUPERSEDED_DESCRIPTION_SUFFIX
    assert all(d == shist.ts(ANCHOR) and a == shist.SUPERSEDED_LOG_AUTHOR for _w, d, a, _e in seed.superseded)
    for wonum in ("1182", "1201"):
        assert wos[wonum]["status"] == "CAN"
        assert wos[wonum]["description"] == frozen[wonum]["description"] + shist.SUPERSEDED_DESCRIPTION_SUFFIX
    assert wos["1182"]["description"].endswith(
        "DEFERRED at production planning request (Line 3 at full loading) [superseded by the live plant history]")
    for wonum in ("1187", "1195"):
        assert wos[wonum]["status"] == "COMP"
        assert wos[wonum]["description"] == shist.PRIOR_ERA_PREFIX + frozen[wonum]["description"]
    # 1204 is superseded, and 1198 (an unrelated CAL order, no aligned pm_num)
    # is NOT: the plant supersede is scoped by the aligned plans' pm_num.
    assert wos["1198"] == frozen["1198"]
    untouched = set(frozen) - {"1182", "1201", "1187", "1195", "1204"}
    assert {w: wos[w] for w in untouched} == {w: frozen[w] for w in untouched}
    open_on_asset = {w for w, r in wos.items()
                     if r["asset_num"] == cm.STORY_CMMS_ASSET_NUM and r["status"] in shist.OPEN_STATUSES}
    assert open_on_asset == {shist.DEFERRED_PM_WONUM}
    snapshot = ({w: dict(r) for w, r in wos.items()}, list(seed.log))
    assert shist.write(erp, seed, shist.plan(ANCHOR))["seed"] == _SEED_COUNTS_NOOP
    assert ({w: dict(r) for w, r in wos.items()}, list(seed.log)) == snapshot


def test_frozen_era_marks_fit_the_011_description_column():
    """The marks never overflow VARCHAR(250): the DDL width is the constant,
    and every 011 story-asset row the live write marks fits with its mark."""
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS work_orders \((.*?)\);",
                    _SEED_011.read_text(encoding="utf-8"), re.S).group(1)
    assert re.search(r"description\s+VARCHAR\((\d+)\)", ddl).group(1) == str(shist.DESCRIPTION_WIDTH)
    story = [r for r in _seed_011_work_orders().values() if r["asset_num"] == cm.STORY_CMMS_ASSET_NUM]
    for row in story:
        if row["status"] == "COMP":
            assert len(shist.PRIOR_ERA_PREFIX + row["description"]) <= shist.DESCRIPTION_WIDTH, row["wonum"]
        if row["status"] in shist.OPEN_STATUSES:
            assert len(row["description"] + shist.SUPERSEDED_DESCRIPTION_SUFFIX) <= shist.DESCRIPTION_WIDTH
        assert not row["description"].startswith(shist.PRIOR_ERA_PREFIX)
        assert not row["description"].endswith(shist.SUPERSEDED_DESCRIPTION_SUFFIX)


def test_frozen_era_sql_scopes_to_the_story_asset_before_the_anchor():
    scope = " ".join(shist._FROZEN_ERA_SCOPE.split())
    assert "wo.asset_num = %(asset)s" in scope and "wo.reported_date < %(anchor)s" in scope
    assert "wo.wonum <> ALL(%(history)s)" in scope and "wo.wonum NOT LIKE %(condition)s" in scope
    supersede = " ".join(shist._SUPERSEDE_FROZEN_ORDERS_SQL.split())
    assert f"UPDATE {shist.CMMS_SCHEMA}.work_orders AS wo SET status = 'CAN'" in supersede  # nosec B608 - expected text
    assert "wo.status = m.prior" in supersede and scope in supersede
    assert f"INSERT INTO {shist.CMMS_SCHEMA}.work_order_log" in supersede
    prior_era = " ".join(shist._MARK_PRIOR_ERA_SQL.split())
    assert "wo.status = 'COMP'" in prior_era and scope in prior_era
    assert shist.superseded_log_entry("APPR") == "Superseded by the live plant history (status was APPR)"


def test_write_marks_a_legacy_superseded_pm_without_resuperseding_it():
    """An env written before the status-recording entry: 1182 is already CAN
    with the bare entry. A rerun adds the description suffix only; Delete
    reopens it to WAPPR (the only status the bare entry ever superseded)."""
    frozen = _seed_011_work_orders()
    seed = FakeDB(work_orders={w: dict(r) for w, r in frozen.items()})
    seed.work_orders["1182"]["status"] = "CAN"
    seed.log.append(("1182", shist.ts(ANCHOR), shist.SUPERSEDED_LOG_AUTHOR, "UPDATE", shist.SUPERSEDED_LOG_ENTRY))
    out = shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    assert out["seed"]["superseded_orders"] == 1  # 1201 only (1182 already CAN)
    assert out["seed"]["superseded_plant_orders"] == 1  # 1204 (1001-CVA-100)
    assert seed.work_orders["1182"]["description"].endswith(shist.SUPERSEDED_DESCRIPTION_SUFFIX)
    assert [r[4] for r in seed.log if r[0] == "1182"] == [shist.SUPERSEDED_LOG_ENTRY]
    assert shist.restore_frozen_seed(seed)["restored_orders"] == 3  # 1182, 1201, 1204
    assert seed.work_orders == frozen and seed.log == []


def test_clear_story_config_deletes_every_contract_key():
    erp, seed = FakeDB(), FakeDB()
    shist.write(erp, seed, shist.plan(ANCHOR))
    erp.config["unrelated_key"] = "1"
    erp.config.update({"story_default_anchor": "1789990010", "story_default_source": "deploy",
                       shist.CATCH_UP_KEY: "1789990010;1790000130;1790019610"})
    assert shist.clear_story_config(erp) == len(shist.CONFIG_KEYS) + len(shist.DEFAULT_KEYS) + 1
    assert erp.deleted_keys == [shist.CONFIG_KEYS + shist.DEFAULT_KEYS
                                + (shist.CATCH_UP_KEY, shist.LIVE_ANCHOR_KEY, shist.BACKFILL_KEY, shist.LIVE_SINCE_KEY)]
    assert erp.config == {"unrelated_key": "1"}
    assert erp.commits == 2  # write + clear


def test_clear_story_config_rolls_back_and_raises():
    erp = FakeDB(fail_on="DELETE FROM simulation_config")
    with pytest.raises(RuntimeError):
        shist.clear_story_config(erp)
    assert erp.rollbacks == 1 and erp.commits == 0


def test_write_reuses_the_stored_anchor():
    erp, seed = FakeDB(), FakeDB()
    shist.write(erp, seed, shist.plan(ANCHOR))
    later = shist.write(erp, seed, shist.plan(ANCHOR + 7 * 3600))
    assert later["anchor_epoch"] == ANCHOR
    assert later["failed_run_number"] == shist.plan(ANCHOR).failed_run.run_number
    assert later["erp"]["runs"] == 0


def test_write_fails_loudly_on_missing_reference_data():
    erp, seed = FakeDB(missing={"CUST-MCH-0002"}), FakeDB()
    with pytest.raises(RuntimeError, match="CUST-MCH-0002"):
        shist.write(erp, seed, shist.plan(ANCHOR))
    assert erp.rollbacks == 1 and erp.commits == 0 and not seed.inserts


def test_write_skips_a_normal_run_whose_number_another_run_holds():
    """Pre-ADR-0090 data: another run holds a planned run's number. That run
    and the order shipping it are left out; everything else is written."""
    sp = shist.plan(ANCHOR)
    taken = sp.runs[4]
    erp, seed = FakeDB(held={taken.run_number}), FakeDB()
    out = shist.write(erp, seed, sp)
    assert out["erp"]["runs"] == len(sp.runs) and out["erp"]["skipped_runs"] == 1
    assert out["erp"]["orders"] == 3 + len(sp.runs) - 1
    assert ("production_runs", taken.run_number) not in erp.keys
    assert ("mes_work_orders", taken.work_order_number) not in erp.keys
    assert not any(o.batch_id == taken.run_number and ("customer_orders", o.order_number) in erp.keys
                   for o in sp.run_orders)
    assert ("production_runs", sp.failed_run.run_number) in erp.keys and erp.rollbacks == 0


def test_write_refuses_when_the_failed_batch_number_is_held():
    sp = shist.plan(ANCHOR)
    erp, seed = FakeDB(held={sp.failed_run.run_number}), FakeDB()
    with pytest.raises(shist.NumberCollision, match=sp.failed_run.run_number):
        shist.write(erp, seed, sp)
    assert erp.rollbacks == 1 and erp.commits == 0 and not seed.inserts


def test_write_rolls_back_and_raises_on_a_failing_seed_db():
    erp, seed = FakeDB(), FakeDB(fail_on="work_order_log")
    with pytest.raises(RuntimeError, match="boom"):
        shist.write(erp, seed, shist.plan(ANCHOR))
    assert erp.commits == 1 and seed.rollbacks == 1 and seed.commits == 0


NEW_ANCHOR = ANCHOR + 5 * shist.DAY  # a db-init re-run wiped the ERP side: next write, new clock


def _plan_dates(sp):
    return {wo.wonum: (wo.status, *(getattr(wo, c) for c in _PLAN_DATES)) for wo in sp.cmms_work_orders}


def _seed_dates(seed):
    return {w: (r["status"], *(r[c] for c in _PLAN_DATES))
            for w, r in seed.work_orders.items() if w in shist.HISTORY_WONUMS}


def test_rewrite_on_a_new_anchor_moves_the_seed_rows_and_keeps_one_log_per_type():
    seed = FakeDB()
    shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    assert _seed_dates(seed) == _plan_dates(shist.plan(ANCHOR))
    seed.work_orders[shist.CONDITION_WONUM]["description"] = "steward edit"  # not plan-owned
    rewrite = shist.write(FakeDB(), seed, shist.plan(NEW_ANCHOR))
    assert rewrite["anchor_epoch"] == NEW_ANCHOR
    assert rewrite["seed"]["cmms_work_orders"] == 4 and rewrite["seed"]["cmms_log"] == 2
    assert _seed_dates(seed) == _plan_dates(shist.plan(NEW_ANCHOR)) != _plan_dates(shist.plan(ANCHOR))
    assert seed.work_orders[shist.CONDITION_WONUM]["description"] == "steward edit"
    assert [r for r in seed.log if r[0] == shist.DEFERRED_PM_WONUM] == [
        (log.wonum, log.log_date, log.author, log.log_type, log.entry)
        for log in shist.plan(NEW_ANCHOR).cmms_log]
    again = shist.write(FakeDB(), seed, shist.plan(NEW_ANCHOR))
    assert again["seed"] == _SEED_COUNTS_NOOP


def test_condition_wonum_prefix_is_the_live_monitors():
    from erp_simulation.mes_simulator import MESSimulator
    assert shist.CONDITION_WONUM_PREFIX == "VIB-CMX-"
    alarm = cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0
    # loop 0 of EVERY anchor has the same wonum: the collision the purge removes
    for anchor in (ANCHOR, NEW_ANCHOR):
        assert MESSimulator._condition_wonum(cm.story_seconds(anchor + alarm, anchor)) == "VIB-CMX-000000"


def test_rewrite_purges_earlier_era_condition_orders_only():
    seed = FakeDB()
    shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    old_era = _condition_order("VIB-CMX-000000", shist.ts(ANCHOR + 3600))
    this_era = _condition_order("VIB-CMX-000001", shist.ts(NEW_ANCHOR + 3600))
    other_asset = {**_condition_order("VIB-CMX-000002", shist.ts(ANCHOR)), "asset_num": "1001-HYP-402"}
    for row in (old_era, this_era, other_asset):
        seed.work_orders[row["wonum"]] = row
    seed.log.append(("VIB-CMX-000000", shist.ts(ANCHOR + 3700), "tech", "UPDATE", "note"))
    first = shist.write(FakeDB(), seed, shist.plan(NEW_ANCHOR))
    assert first["seed"]["purged_condition_orders"] == 1
    assert "VIB-CMX-000000" not in seed.work_orders
    assert {"VIB-CMX-000001", "VIB-CMX-000002"} <= set(seed.work_orders)
    assert not [r for r in seed.log if r[0] == "VIB-CMX-000000"]
    assert shist.write(FakeDB(), seed, shist.plan(NEW_ANCHOR))["seed"]["purged_condition_orders"] == 0


def test_same_anchor_rerun_keeps_this_eras_condition_orders():
    seed = FakeDB()
    shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    live = _condition_order("VIB-CMX-000000", shist.ts(ANCHOR + 3600))
    seed.work_orders[live["wonum"]] = live
    erp = FakeDB()
    shist.write(erp, seed, shist.plan(ANCHOR))
    assert shist.write(erp, seed, shist.plan(ANCHOR))["seed"]["purged_condition_orders"] == 0
    assert "VIB-CMX-000000" in seed.work_orders


def test_restore_frozen_seed_reopens_the_superseded_pm_and_drops_history_rows():
    erp, seed = FakeDB(), FakeDB(wappr_pms={"1182"})
    seed.work_orders["1190"] = {**_condition_order("1190", None), "work_type": "PM", "status": "CAN"}
    seed.log.append(("1182", shist.ts(ANCHOR - shist.DAY), "R. Roe", "DEFERRAL", "frozen-era entry"))
    shist.write(erp, seed, shist.plan(ANCHOR))
    assert seed.work_orders["1182"]["status"] == "CAN"
    assert shist.restore_frozen_seed(seed) == {"restored_orders": 1, "prior_era_restored": 0,
                                            "history_work_orders": 4, "pm_schedule": 1,
                                            "plant_pm_work_orders": len(shist.PLANT_PM_BASELINE),
                                            "plant_pm_schedule": len(shist.PLANT_PM_BASELINE),
                                            "past_stop_orders": len(shist.PAST_STOPS)}
    assert seed.pm_schedules[shist.STORY_PM_NUM] == shist.FROZEN_PM_DATES
    # every non-story plant PM schedule is back to its 011 frozen dates and the
    # reserved CLOSE orders are gone
    for b in shist.PLANT_PM_BASELINE:
        assert seed.pm_schedules[b.pm_num] == (b.last_completion, b.next_due)
    assert not set(shist.PLANT_PM_CLOSE_WONUMS) & set(seed.work_orders)
    assert seed.work_orders["1182"]["status"] == "WAPPR"
    assert seed.work_orders["1182"]["description"] == "frozen PM 1182"
    assert seed.work_orders["1190"]["status"] == "CAN"  # cancelled without the supersede entry
    assert not set(shist.HISTORY_WONUMS) & set(seed.work_orders)
    assert seed.log == [("1182", shist.ts(ANCHOR - shist.DAY), "R. Roe", "DEFERRAL", "frozen-era entry")]
    assert seed.commits == 2 and seed.rollbacks == 0
    assert shist.restore_frozen_seed(seed) == {"restored_orders": 0, "prior_era_restored": 0,
                                            "history_work_orders": 0, "pm_schedule": 0,
                                            "plant_pm_work_orders": 0, "plant_pm_schedule": 0,
                                            "past_stop_orders": 0}


def test_live_then_restore_gives_back_exactly_the_011_rows():
    frozen = _seed_011_work_orders()
    seed = FakeDB(work_orders={w: dict(r) for w, r in frozen.items()})
    shist.write(FakeDB(), seed, shist.plan(ANCHOR))
    assert shist.restore_frozen_seed(seed) == {"restored_orders": 3, "prior_era_restored": 2,
                                            "history_work_orders": 4, "pm_schedule": 1,
                                            "plant_pm_work_orders": len(shist.PLANT_PM_BASELINE),
                                            "plant_pm_schedule": len(shist.PLANT_PM_BASELINE),
                                            "past_stop_orders": len(shist.PAST_STOPS)}
    assert seed.work_orders == frozen and seed.log == []
    assert {w: seed.work_orders[w]["status"] for w in ("1182", "1201", "1204")} == {
        "1182": "WAPPR", "1201": "APPR", "1204": "WAPPR"}
    # the non-story plant schedules are back to their 011 frozen dates
    for b in shist.PLANT_PM_BASELINE:
        assert seed.pm_schedules[b.pm_num] == (b.last_completion, b.next_due)


def test_restore_frozen_seed_rolls_back_and_raises():
    seed = FakeDB(fail_on="DELETE FROM cmms_indianapolis.work_orders")
    with pytest.raises(RuntimeError):
        shist.restore_frozen_seed(seed)
    assert seed.rollbacks == 1 and seed.commits == 0


# ---------------------------------------------------------------------------
# CloudFormation provider handler
# ---------------------------------------------------------------------------

@pytest.fixture
def cfn_handler(monkeypatch):
    path = _REPO / "lambda_" / "lambda_story_history.py"
    spec = importlib.util.spec_from_file_location("lambda_story_history_t7", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dbs = {"DB_SECRET_NAME": FakeDB(), "SEED_DB_SECRET_NAME": FakeDB()}
    for db in dbs.values():
        db.close = lambda: None
    monkeypatch.setattr(module, "_connect", lambda env: dbs[env])
    clock = itertools.count(int(ANCHOR), 3600)
    monkeypatch.setattr(module, "_now", lambda: next(clock))
    return module, dbs


def test_handler_create_then_update_keeps_the_anchor(cfn_handler):
    module, dbs = cfn_handler
    props = {"ResourceProperties": {"Days": "3", "Version": "1"}}
    created = module.lambda_handler({"RequestType": "Create", **props}, None)
    assert created["PhysicalResourceId"] == module.PHYSICAL_ID
    deploy = cm.snap_anchor(ANCHOR, "down")  # the deploy anchor is on the telemetry grid (D15)
    assert float(created["Data"]["AnchorEpoch"]) == deploy
    assert created["Data"]["FailedRunNumber"] == shist.plan(deploy).failed_run.run_number
    updated = module.lambda_handler(
        {"RequestType": "Update", "PhysicalResourceId": module.PHYSICAL_ID, **props}, None)
    assert updated["Data"] == created["Data"]
    # Create: the history + the reset-failsafe default (ADR-0091 Design C); an
    # unchanged Update: the (idempotent) history only, the default untouched.
    assert dbs["DB_SECRET_NAME"].commits == 3
    assert dbs["DB_SECRET_NAME"].config["story_default_anchor"] == str(int(deploy))
    assert dbs["DB_SECRET_NAME"].config["story_default_source"] == "deploy"


def test_handler_delete_clears_the_story_config_and_restores_the_frozen_seed(cfn_handler):
    module, dbs = cfn_handler
    erp, seed = dbs["DB_SECRET_NAME"], dbs["SEED_DB_SECRET_NAME"]
    seed.work_orders.update(FakeDB(wappr_pms={"1182"}).work_orders)
    props = {"ResourceProperties": {"Days": "3", "Version": "1"}}
    module.lambda_handler({"RequestType": "Create", **props}, None)
    assert seed.work_orders["1182"]["status"] == "CAN"
    inserts_before = list(erp.inserts)
    out = module.lambda_handler({"RequestType": "Delete", "PhysicalResourceId": "x"}, None)
    assert out == {"PhysicalResourceId": "x"}
    assert erp.deleted_keys == [shist.CONFIG_KEYS + shist.DEFAULT_KEYS
                                + (shist.CATCH_UP_KEY, shist.LIVE_ANCHOR_KEY, shist.BACKFILL_KEY, shist.LIVE_SINCE_KEY)]
    assert not any(k.startswith("story_") for k in erp.config)
    assert erp.inserts == inserts_before  # ERP history rows stay
    assert seed.work_orders["1182"]["status"] == "WAPPR"
    assert not set(shist.HISTORY_WONUMS) & set(seed.work_orders) and seed.log == []


def test_handler_delete_never_raises(cfn_handler, monkeypatch, caplog):
    module, _dbs = cfn_handler

    def unreachable(env):
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(module, "_connect", unreachable)
    with caplog.at_level("WARNING"):
        out = module.lambda_handler({"RequestType": "Delete", "PhysicalResourceId": "x"}, None)
    assert out == {"PhysicalResourceId": "x"}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("story config clear FAILED" in m and "RuntimeError" in m for m in msgs)
    assert any("frozen seed restore FAILED" in m and "RuntimeError" in m for m in msgs)


def test_handler_delete_steps_are_independent(cfn_handler, caplog):
    """A failing ERP clear still restores the seed; a failing seed restore
    still reports the resource deleted."""
    module, dbs = cfn_handler
    erp, seed = dbs["DB_SECRET_NAME"], dbs["SEED_DB_SECRET_NAME"]
    module.lambda_handler({"RequestType": "Create", "ResourceProperties": {}}, None)
    erp.fail_on = "DELETE FROM simulation_config"
    with caplog.at_level("WARNING"):
        out = module.lambda_handler({"RequestType": "Delete"}, None)
    assert out == {"PhysicalResourceId": module.PHYSICAL_ID}
    assert erp.rollbacks == 1 and "story_anchor_epoch" in erp.config
    assert not set(shist.HISTORY_WONUMS) & set(seed.work_orders)
    assert any("story config clear FAILED" in r.getMessage() for r in caplog.records)
    erp.fail_on, seed.fail_on = None, "work_order_log"
    caplog.clear()
    with caplog.at_level("WARNING"):
        module.lambda_handler({"RequestType": "Delete"}, None)
    assert "story_anchor_epoch" not in erp.config and seed.rollbacks == 1
    assert any("frozen seed restore FAILED" in r.getMessage() for r in caplog.records)


def test_handler_raises_on_errors(cfn_handler):
    module, dbs = cfn_handler
    dbs["DB_SECRET_NAME"].missing.add(shist.FAILED_SKU)
    with pytest.raises(RuntimeError):
        module.lambda_handler({"RequestType": "Create", "ResourceProperties": {}}, None)
    with pytest.raises(ValueError):
        module.lambda_handler({"RequestType": "Bogus"}, None)


def test_handler_connect_requires_the_secret_env(monkeypatch):
    path = _REPO / "lambda_" / "lambda_story_history.py"
    spec = importlib.util.spec_from_file_location("lambda_story_history_t7_env", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("SEED_DB_SECRET_NAME", raising=False)
    with pytest.raises(RuntimeError, match="SEED_DB_SECRET_NAME"):
        module._connect("SEED_DB_SECRET_NAME")


def test_handler_connections_carry_a_statement_timeout(monkeypatch):
    import sys
    import types
    from erp_simulation.config import DatabaseConfig
    path = _REPO / "lambda_" / "lambda_story_history.py"
    spec = importlib.util.spec_from_file_location("lambda_story_history_t7_timeout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from erp_simulation import pgdriver
    seen = {}
    monkeypatch.setattr(pgdriver, "connect", lambda **kw: seen.update(kw) or "conn")
    cfg = types.SimpleNamespace(host="h", port=5432, database="d", user="u", password="p",  # nosec B106 - fake test credentials, not real ones
                                connection_timeout=5)
    monkeypatch.setattr(DatabaseConfig, "from_secrets_manager", staticmethod(lambda *_a: cfg))
    monkeypatch.setenv("DB_SECRET_NAME", "adt/erp")
    assert module._connect("DB_SECRET_NAME") == "conn"
    assert seen["options"] == "-c statement_timeout=15000" == f"-c statement_timeout={module.STATEMENT_TIMEOUT_MS}"
    assert seen["connect_timeout"] == 5


# ---------------------------------------------------------------------------
# Real PostgreSQL (throwaway container): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# ---------------------------------------------------------------------------
from erp_simulation.tests._pg_sql import stmt  # noqa: E402

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


def _load_db_init():
    spec = importlib.util.spec_from_file_location(
        "db_init_handler_t7", _REPO / "lambda-db-init" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pg_pair():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    pytest.importorskip("boto3")
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    names = {"erp": "t7erp_" + uuid.uuid4().hex[:8], "seed": "t7seed_" + uuid.uuid4().hex[:8]}
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    conns = {k: pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": v})) for k, v in names.items()}
    try:
        db_init = _load_db_init()
        db_init.create_schema(conns["erp"])
        db_init.seed_data(conns["erp"])
        with conns["seed"].cursor() as cur:
            for sql in ("001_schemas.sql", "011_cmms_indianapolis.sql"):
                cur.execute((_REPO / "lambda-adt-seed" / sql).read_text(encoding="utf-8"))
        conns["seed"].commit()
        yield conns
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE {db}", db=name))
        admin.close()


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_pg_write_is_idempotent_and_joins_up(pg_pair):
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    first = shist.write(erp, seed, shist.plan(anchor))
    run_number = first["failed_run_number"]
    assert first["erp"]["runs"] == 19 and first["seed"]["cmms_work_orders"] == 4
    assert first["seed"]["superseded_orders"] == 2  # 011's frozen-era open PMs 1182, 1201
    assert first["seed"]["prior_era_marked"] == 2  # 011's frozen-era completed 1187, 1195
    again = shist.write(erp, seed, shist.plan(anchor + 3600))
    assert again["anchor_epoch"] == anchor
    assert again["erp"] == {"config": 0, "runs": 0, "orders": 0, "past_stops": 0}
    assert again["seed"] == _SEED_COUNTS_NOOP
    # the failed run, its work order, rejects and DIMENSIONAL failures
    rows = _one(erp, """
        SELECT w.reject_quantity, w.line, w.site, count(q.check_id), sum(q.fail_count)
        FROM production_runs pr JOIN mes_work_orders w ON w.production_run_id = pr.run_id
        JOIN mes_quality_checks q ON q.work_order_id = w.work_order_id AND q.check_type = 'DIMENSIONAL'
        WHERE pr.run_number = %s GROUP BY 1, 2, 3""", (run_number,))
    assert rows == [(18, cm.STORY_MES_LINE, shist.SITE, 3, 18)]
    # the three customers that received the batch
    customers = _one(erp, """
        SELECT DISTINCT c.customer_code FROM shipment_items si
        JOIN shipments s ON s.shipment_id = si.shipment_id JOIN customers c ON c.customer_id = s.customer_id
        WHERE si.batch_id = %s ORDER BY 1""", (run_number,))
    assert [c for (c,) in customers] == list(shist.STORY_CUSTOMER_CODES)
    # the failed batch's three orders are worth $10,200; every history run shipped once
    assert _one(erp, """
        SELECT sum(o.total_amount) FROM customer_orders o WHERE o.order_id IN (
            SELECT li.order_id FROM order_line_items li JOIN shipment_items si
            ON si.line_item_id = li.line_item_id WHERE si.batch_id = %s)""", (run_number,)) == [(10200,)]
    assert _one(erp, "SELECT count(DISTINCT si.batch_id) FROM shipment_items si JOIN production_runs pr "
                     "ON pr.run_number = si.batch_id WHERE pr.notes = 'Live-story history run'") == [(19,)]
    assert _one(erp, "SELECT count(*) FROM shipment_items si JOIN mes_indianapolis.production_runs pr "
                     "ON pr.run_number = si.batch_id") == [(3 + 18,)]
    # the lot trace and the site MES view
    assert _one(erp, "SELECT count(*) FROM lot_mes_tracking t JOIN mes_work_orders w "
                     "ON w.work_order_id = t.work_order_id JOIN production_runs pr "
                     "ON pr.run_id = w.production_run_id WHERE pr.run_number = %s", (run_number,)) == [(1,)]
    assert _one(erp, "SELECT count(*) FROM mes_indianapolis.downtime_events "
                     "WHERE downtime_reason = 'VIB-BEARING'") == [(1,)]
    # the seed CMMS side
    assert _one(seed, "SELECT status, pm_num FROM cmms_indianapolis.work_orders WHERE wonum = '3101'") == [("WAPPR", shist.STORY_PM_NUM)]
    assert _one(seed, "SELECT log_type FROM cmms_indianapolis.work_order_log WHERE wonum = '3101' "
                      "ORDER BY log_date") == [("CREATE",), ("DEFERRAL",)]
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum IN ('3107', '3108') "
                      "AND status = 'COMP' AND failure_code = 'BEARING'") == [(2,)]


def test_pg_direct_reset_step_rewrites_the_cleared_story(pg_pair, monkeypatch):
    """The story step a direct ERP reset runs (stacks/_direct_reset.py): the
    no-change Update event CloudFormation sends the history CR. A direct reset
    drops simulation_config (the story_* anchor keys with it); on that event
    the handler finds no stored anchor, takes a fresh one and rewrites the
    whole story, so a Reset All ends with the story rebuilt, not gone."""
    _spec = importlib.util.spec_from_file_location(
        "story_history_handler_reset", _REPO / "lambda_" / "lambda_story_history.py")
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    # the handler's connections are the throwaway ERP and seed databases
    monkeypatch.setattr(handler, "_with_connections", lambda fn: fn(erp, seed))
    monkeypatch.setattr(handler, "_on_connection",
                        lambda secret_env, fn: fn(erp if secret_env == "DB_SECRET_NAME" else seed))

    # Create writes the story: the anchor key is present.
    created = handler._create_or_update({"ResourceProperties": {"Days": "3", "Anchor": ""}}, "Create")
    assert shist.read_stored_anchor(erp) == float(created["AnchorEpoch"])
    assert _one(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s",
                (created["FailedRunNumber"],)) == [(1,)]

    # The db-init full reset itself: drop every ERP table, then create and
    # seed the schema again. The story_* keys and the history rows go with it.
    monkeypatch.setenv("DB_NAME", erp.get_dsn_parameters()["dbname"])
    db_init = _load_db_init()
    db_init.drop_all_tables(erp)
    db_init.create_schema(erp)
    db_init.seed_data(erp)
    assert shist.read_stored_anchor(erp) is None
    assert _one(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s",
                (created["FailedRunNumber"],)) == [(0,)]

    # The step's event: the deployed properties as both new and old (so the
    # writer does not re-plan, it writes); no stored anchor -> a fresh anchor.
    props = {"Days": "3", "Anchor": "", "Version": "v", "ErpDbInit": "t|db", "ErpDbSecretArn": "arn"}
    event = {"RequestType": "Update", "ResourceProperties": props, "OldResourceProperties": props}
    assert handler._anchor_changed(event) is False and handler._loop_changed(event) is False
    data = handler._create_or_update(event, "Update")

    # the story is back: the anchor and the full set of story_* keys
    assert shist.read_stored_anchor(erp) == float(data["AnchorEpoch"])
    keys = {k for (k,) in _one(erp,
            "SELECT config_key FROM simulation_config WHERE config_key LIKE %s", ("story\\_%",))}
    assert set(shist.CONFIG_KEYS) <= keys
    # the failed batch the lookup answers with is written again
    assert _one(erp, "SELECT count(*) FROM production_runs WHERE run_number = %s",
                (data["FailedRunNumber"],)) == [(1,)]


def test_pg_a_fresh_account_shares_logins_and_failure_codes_through_the_site_views(pg_pair):
    """What module 4's Resolve samples on a fresh account, read through the
    site MES view and the site CMMS: the technicians' logins and the failure
    codes are shared from the first minute, the past stops' bridge orders sit
    off the story mill, and the simulator's close-out finds nothing of them
    left to complete (they are already worked)."""
    from erp_simulation.mes_simulator import MESSimulator
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp(), "down"))
    shist.write(erp, seed, shist.plan(anchor))
    resolved = {v for (v,) in _one(erp, "SELECT DISTINCT resolved_by FROM mes_indianapolis.downtime_events")}
    reasons = {v for (v,) in _one(erp, "SELECT DISTINCT downtime_reason FROM mes_indianapolis.downtime_events")}
    authors = {v for (v,) in _one(seed, "SELECT DISTINCT author FROM cmms_indianapolis.work_order_log")}
    codes = {v for (v,) in _one(seed, "SELECT DISTINCT failure_code FROM cmms_indianapolis.work_orders "
                                      "WHERE failure_code IS NOT NULL")}
    for a, b in ((authors, resolved), (codes, reasons)):
        shared, ratio = _overlap(a, b)
        assert len(shared) >= _RESOLVE_MIN_SHARED and ratio >= _RESOLVE_MIN_RATIO, (sorted(a), sorted(b))
    assert set(MESSimulator.DOWNTIME_RESOLVERS) <= authors & resolved
    assert _one(seed, "SELECT DISTINCT asset_num, status, reported_by FROM cmms_indianapolis.work_orders "
                      "WHERE wonum = ANY(%s)", (list(shist.PAST_STOP_WONUMS),)) == [
        ("1001-CGR-502", "COMP", MESSimulator.CMMS_BRIDGE_REPORTED_BY)]

    def read(ids):
        return MESSimulator.read_downtimes(erp, ids)
    with seed.cursor() as cur:
        loop_start = anchor + cm.TOTAL_SECONDS
        assert MESSimulator.complete_bridge_work_orders(cur, loop_start, loop_start + 60, read) == 0
    seed.rollback()
    # one MES stop per bridge order, its id in the order's description
    assert _one(erp, "SELECT count(*) FROM mes_indianapolis.downtime_events WHERE downtime_id::text = ANY(%s)",
                (list(shist.PAST_STOP_DOWNTIME_IDS),)) == [(len(shist.PAST_STOPS),)]


_PG_HISTORY_OEE_SQL = """
    SELECT pr.run_number, w.availability, w.oee_percentage, w.performance, w.good_quantity,
           w.planned_quantity,
           EXTRACT(EPOCH FROM (w.actual_end_time - w.actual_start_time)) / 60,
           COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (d.end_time - d.start_time))) / 60
                     FROM mes_downtime_events d WHERE d.work_order_id = w.work_order_id), 0)
    FROM mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
    WHERE pr.run_number = ANY(%s)
"""


def test_pg_a_past_stops_run_carries_its_downtime_in_availability_and_oee(pg_pair):
    """The simulator derives a work order's availability from its downtime
    rows; a history run with a past stop must not read 100% available. Runs
    without one keep the figures they were inserted with, and a rerun moves
    nothing."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp(), "down"))
    sp = shist.plan(anchor)
    shist.write(erp, seed, sp)
    hosts = {s.run_number for s in sp.past_stops}
    rows = _one(erp, _PG_HISTORY_OEE_SQL, ([r.run_number for r in sp.runs],))
    assert len(rows) == len(sp.runs)
    for run_number, avail, oee, perf, good, planned, run_minutes, downtime in rows:
        expected = shist._history_oee(float(run_minutes), float(downtime), float(perf), good, planned)
        assert (float(avail), float(oee)) == expected, run_number
        assert (float(avail) < 100.0) == (run_number in hosts), run_number
    shist.write(erp, seed, sp)
    assert _one(erp, _PG_HISTORY_OEE_SQL, ([r.run_number for r in sp.runs],)) == rows


def test_pg_a_stop_moved_back_onto_its_run_gives_the_run_it_left_its_figures_back(pg_pair):
    """A stop an earlier era hung on another run (a live -> frozen -> live
    redeploy keeps the ERP rows) moves onto this plan's run on the next write;
    the run it left reads as it was inserted again."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp(), "down"))
    sp = shist.plan(anchor)
    shist.write(erp, seed, sp)
    stop = sp.past_stops[0]
    other = next(r for r in sp.runs if r.run_number not in {s.run_number for s in sp.past_stops})
    clean = _one(erp, _PG_HISTORY_OEE_SQL, ([other.run_number],))
    with erp.cursor() as cur:
        cur.execute("""
            UPDATE mes_downtime_events SET work_order_id =
                (SELECT work_order_id FROM mes_work_orders WHERE work_order_number = %s)
            WHERE downtime_id = %s::uuid""", (other.work_order_number, stop.downtime_id))
        cur.execute("UPDATE mes_work_orders SET availability = 80.00, oee_percentage = 70.00 "
                    "WHERE work_order_number = %s", (other.work_order_number,))
    erp.commit()
    shist.write(erp, seed, sp)
    assert _one(erp, _PG_HISTORY_OEE_SQL, ([other.run_number],)) == clean
    ((_rn, avail, *_rest),) = _one(erp, _PG_HISTORY_OEE_SQL, ([stop.run_number],))
    assert float(avail) < 100.0


def test_pg_a_shorter_history_keeps_exactly_its_stops_in_both_systems(pg_pair):
    """A three-day history then a one-day one on the same clock: both systems
    hold exactly the one-day plan's four stops, and every history run reads
    the availability and OEE of the downtime it holds now (the runs the two
    dropped stops sat on get their inserted figures back)."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp(), "down"))
    long_, short = shist.plan(anchor, 3), shist.plan(anchor, 1)
    shist.write(erp, seed, long_)
    shist.write(erp, seed, short)
    planned = {s.downtime_id for s in short.past_stops}
    assert len(planned) == 4
    assert {v for (v,) in _one(erp, "SELECT downtime_id::text FROM mes_downtime_events "
                                    "WHERE downtime_id::text = ANY(%s)", (list(shist.PAST_STOP_DOWNTIME_IDS),))} == planned
    assert {v for (v,) in _one(seed, "SELECT wonum FROM cmms_indianapolis.work_orders WHERE wonum = ANY(%s)",
                               (list(shist.PAST_STOP_WONUMS),))} == {s.order.wonum for s in short.past_stops}
    runs = sorted({r.run_number for r in (*long_.runs, *short.runs)})
    for run_number, avail, oee, perf, good, planned_qty, run_minutes, downtime in _one(erp, _PG_HISTORY_OEE_SQL,
                                                                                         (runs,)):
        assert (float(avail), float(oee)) == shist._history_oee(
            float(run_minutes), float(downtime), float(perf), good, planned_qty), run_number


def test_pg_the_cleanup_never_deletes_downtime_on_a_run_the_history_does_not_own(pg_pair):
    """A past stop's id on a non-history work order (it cannot come from the
    history writer) is left alone: only history runs' stops are the plan's."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(cm.snap_anchor(datetime.now(timezone.utc).timestamp(), "down"))
    shist.write(erp, seed, shist.plan(anchor, 3))
    stop = shist.plan(anchor, 3).past_stops[5]
    with erp.cursor() as cur:
        cur.execute("UPDATE production_runs SET notes = 'simulator' WHERE run_id = "
                    "(SELECT production_run_id FROM mes_work_orders WHERE work_order_number = %s)",
                    (stop.work_order_number,))
    erp.commit()
    shist.write(erp, seed, shist.plan(anchor, 1))
    assert _one(erp, "SELECT count(*) FROM mes_downtime_events WHERE downtime_id = %s::uuid",
                (stop.downtime_id,)) == [(1,)]


def test_pg_lot_trace_fans_out_from_the_failed_batch(pg_pair):
    """MES consumption names the ERP lot the run drew; that lot (a history
    lot) fed several shipped batches of the part but not all of them, and
    their customers include one the failed batch never reached. Both history
    lots are fully drawn, so the simulator's FIFO never picks them."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    sp = shist.plan(anchor)
    shist.write(erp, seed, sp)
    failed = sp.failed_run
    # MES side: the failed work order consumed the lot ERP recorded, from a supplier
    assert _one(erp, """
        SELECT m.lot_number, s.supplier_code FROM mes_indianapolis.material_consumption m
        JOIN mes_indianapolis.work_orders w ON w.work_order_id = m.work_order_id
        JOIN material_lots ml ON ml.lot_number = m.lot_number
        JOIN suppliers s ON s.supplier_id = ml.supplier_id
        WHERE w.work_order_number = %s""", (failed.work_order_number,)) == [
        (shist.SUSPECT_LOT_NUMBER, shist.HISTORY_LOT_SUPPLIER)]
    lot_runs = [r for (r,) in _one(erp, """
        SELECT pr.run_number FROM lot_production_usage u
        JOIN material_lots ml ON ml.lot_id = u.lot_id
        JOIN production_runs pr ON pr.run_id = u.production_run_id
        WHERE ml.lot_number = %s ORDER BY 1""", (failed.lot_number,))]
    part_runs = [r.run_number for r in (*sp.runs, failed) if r.sku == shist.FAILED_SKU]
    assert failed.run_number in lot_runs and 1 < len(lot_runs) < len(part_runs)
    customers = {c for (c,) in _one(erp, """
        SELECT DISTINCT c.customer_code FROM shipment_items si
        JOIN shipments s ON s.shipment_id = si.shipment_id
        JOIN customers c ON c.customer_id = s.customer_id
        WHERE si.batch_id = ANY(%s)""", (lot_runs,))}
    assert set(shist.STORY_CUSTOMER_CODES) < customers
    # the lot's other batches passed their DIMENSIONAL checks
    assert _one(erp, """
        SELECT count(*), sum(q.fail_count) FROM mes_quality_checks q
        JOIN mes_work_orders w ON w.work_order_id = q.work_order_id
        JOIN production_runs pr ON pr.run_id = w.production_run_id
        WHERE q.check_type = 'DIMENSIONAL' AND pr.run_number = ANY(%s)""",
                [[r for r in lot_runs if r != failed.run_number]]) == [(len(lot_runs) - 1, 0)]
    # both history lots: received before their runs, fully drawn
    for lot in sp.lots:
        (received, quantity, remaining), = _one(erp, """
            SELECT received_date, received_quantity, remaining_quantity FROM material_lots
            WHERE lot_number = %s""", (lot.lot_number,))
        assert remaining == 0 and float(quantity) == lot.quantity
        assert received < min(r.start for r in (*sp.runs, failed) if r.lot_number == lot.lot_number)
    # the PM schedule row tells the same story as the deferred PM
    assert _one(seed, """SELECT last_completion_date::text, next_due_date::text
        FROM cmms_indianapolis.pm_schedules WHERE pm_num = %s""", (shist.STORY_PM_NUM,)) == [
        shist._pm_schedule_dates(sp)]
    # so the simulator's FIFO (production_simulator._allocate_lots_fifo) draws
    # the db-init lot, never a story lot: the lot scope cannot grow
    assert _one(erp, """
        SELECT ml.lot_number FROM material_lots ml
        JOIN suppliers s ON ml.supplier_id = s.supplier_id
        JOIN inventory_items i ON i.item_id = ml.item_id
        WHERE i.item_code = %s AND ml.quality_status = 'APPROVED'
          AND s.qualification_status = 'APPROVED' AND ml.remaining_quantity > 0
          AND ml.current_location_id IN (SELECT location_id FROM inventory_locations
                                         WHERE location_code = %s AND is_active = TRUE)
        ORDER BY ml.received_date ASC""", (shist.FAILED_MATERIAL_CODE, shist.RAW_LOCATION)) == [
        (shist.PRODUCTS[0][1],)]


def _pg_legacy_run(erp, run_number: str, work_order_number: str) -> None:
    """A pre-ADR-0090 simulator run: its random numbers land in the story range."""
    with erp.cursor() as cur:
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
            SELECT %s, product_id, 10, 'COMPLETED', 'detroit', 'line1' FROM products ORDER BY sku LIMIT 1""",
                    (run_number,))
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, planned_quantity)
            SELECT %s, run_id, product_id, 10 FROM production_runs WHERE run_number = %s""",
                    (work_order_number, run_number))
    erp.commit()


def test_pg_write_skips_runs_whose_numbers_legacy_simulator_rows_hold(pg_pair):
    """The failure seen on a rehearsal account: a legacy run holds a history
    run's WORK-ORDER number (and another legacy run holds a history RUN
    number). Before the guard, the first raised NotNullViolation and the
    second silently shipped the legacy run as the story's batch. Now both are
    skipped with their orders, the legacy rows are untouched, and the failed
    batch is intact."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    sp = shist.plan(anchor)
    wo_taken, run_taken = sp.runs[4], sp.runs[7]
    _pg_legacy_run(erp, "RUN-20000101-26053", wo_taken.work_order_number)
    _pg_legacy_run(erp, run_taken.run_number, "WO-20000101-31337")
    out = shist.write(erp, seed, sp)
    assert out["erp"]["runs"] == 19 - 2 and out["erp"]["skipped_runs"] == 2
    assert _one(erp, "SELECT site FROM production_runs WHERE run_number = ANY(%s) ORDER BY 1",
                ([wo_taken.run_number, run_taken.run_number],)) == [("detroit",)]
    assert _one(erp, "SELECT count(*) FROM shipment_items WHERE batch_id = ANY(%s)",
                ([wo_taken.run_number, run_taken.run_number],)) == [(0,)]
    assert _one(erp, "SELECT count(*) FROM shipment_items WHERE batch_id = %s",
                (sp.failed_run.run_number,)) == [(3,)]
    assert shist.write(erp, seed, sp)["erp"] == {"config": 0, "runs": 0, "orders": 0, "past_stops": 0,
                                                 "skipped_runs": 2}


def _pg_story_rows(seed):
    return {w: (s, d) for w, s, d in _one(
        seed, "SELECT wonum, status, description FROM cmms_indianapolis.work_orders "
              "WHERE asset_num = %s ORDER BY wonum", (cm.STORY_CMMS_ASSET_NUM,))}


def test_pg_live_history_leaves_one_open_deferred_pm(pg_pair):
    """Only the history's PM stays open on the story asset: 011's frozen-era
    open PMs (1182 WAPPR, 1201 APPR) are cancelled ONCE with a prior-status
    log entry and the superseded suffix; its completed orders (1187, 1195)
    carry the prior-era prefix; other rows are untouched."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    frozen = _pg_story_rows(seed)
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    shist.write(erp, seed, shist.plan(anchor))
    shist.write(erp, seed, shist.plan(anchor))
    live = _pg_story_rows(seed)
    assert sorted(w for w, (s, _d) in live.items() if s in shist.OPEN_STATUSES) == [shist.DEFERRED_PM_WONUM]
    for wonum, prior in (("1182", "WAPPR"), ("1201", "APPR")):
        assert live[wonum] == ("CAN", frozen[wonum][1] + shist.SUPERSEDED_DESCRIPTION_SUFFIX)
        assert _one(seed, "SELECT log_date, author, log_type FROM cmms_indianapolis.work_order_log "
                          "WHERE wonum = %s AND entry = %s", (wonum, shist.superseded_log_entry(prior))) == [
            (shist.ts(anchor), shist.SUPERSEDED_LOG_AUTHOR, "UPDATE")]
    assert "DEFERRED" in live["1182"][1] and live["1182"][1].endswith("[superseded by the live plant history]")
    for wonum in ("1187", "1195"):
        assert live[wonum] == ("COMP", shist.PRIOR_ERA_PREFIX + frozen[wonum][1])
    assert live["1187"][1].startswith("[Prior CMMS era, before the live history] SPC diameter drift on batch B-2847")
    assert {w: live[w] for w in ("1004", "1096")} == {w: frozen[w] for w in ("1004", "1096")}
    # 1204 (an aligned plan's own open PM, on another asset) is superseded;
    # 1198 (an unrelated CAL order, no aligned pm_num) is untouched.
    assert _one(seed, "SELECT wonum, status FROM cmms_indianapolis.work_orders "
                      "WHERE wonum IN ('1198', '1204') ORDER BY 1") == [("1198", "APPR"), ("1204", "CAN")]


def test_pg_clear_story_config_removes_the_anchor(pg_pair):
    story_live = pytest.importorskip("erp_simulation.story_live")
    erp = pg_pair["erp"]
    shist.write(erp, pg_pair["seed"], shist.plan(float(int(datetime.now(timezone.utc).timestamp()))))
    assert shist.clear_story_config(erp) == len(shist.CONFIG_KEYS)
    assert story_live.read_anchor(erp) is None
    assert _one(erp, "SELECT count(*) FROM simulation_config WHERE config_key LIKE 'story\\_%%'") == [(0,)]
    assert _one(erp, "SELECT count(*) FROM production_runs WHERE notes = 'Live-story history run'")[0][0] > 0


def test_pg_live_simulator_reads_the_anchor(pg_pair):
    story_live = pytest.importorskip("erp_simulation.story_live")
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    shist.write(pg_pair["erp"], pg_pair["seed"], shist.plan(anchor))
    assert story_live.read_anchor(pg_pair["erp"]) == anchor


def _seed_snapshot(seed):
    return (_one(seed, "SELECT * FROM cmms_indianapolis.work_orders ORDER BY wonum"),
            _one(seed, "SELECT wonum, log_date, author, log_type, entry "
                       "FROM cmms_indianapolis.work_order_log ORDER BY log_id"))


def test_pg_rewrite_on_a_new_anchor_moves_the_seed_rows(pg_pair):
    """db-init re-run: the ERP story config is gone, the next write takes a
    NEW anchor; the seed CMMS rows follow it (one CREATE and one DEFERRAL entry) and the
    previous era's loop-0 condition order no longer blocks this era's."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    old = float(int(datetime.now(timezone.utc).timestamp())) - 5 * shist.DAY
    shist.write(erp, seed, shist.plan(old))
    with seed.cursor() as cur:  # what the live monitor wrote in the old era (loop 0)
        cur.execute("INSERT INTO cmms_indianapolis.work_orders (wonum, asset_num, work_type, description, "
                    "status, priority, reported_by, reported_date) VALUES "
                    "('VIB-CMX-000000', %s, 'CM', 'old era', 'WAPPR', 1, 'Condition monitor', %s)",
                    (cm.STORY_CMMS_ASSET_NUM, shist.ts(old + 3600)))
    seed.commit()
    shist.clear_story_config(erp)
    new = old + 5 * shist.DAY
    out = shist.write(erp, seed, shist.plan(new))
    assert out["anchor_epoch"] == new
    assert out["seed"] == {**_SEED_COUNTS_NOOP, "cmms_work_orders": 4, "cmms_log": 2,
                           "purged_condition_orders": 1, "pm_schedule": 1,
                           "plant_pm_work_orders": len(shist.PLANT_PM_BASELINE),
                           "plant_pm_schedule": len(shist.PLANT_PM_BASELINE),
                           "past_stop_orders": len(shist.PAST_STOPS), "past_stop_log": len(shist.PAST_STOPS)}
    for wo in shist.plan(new).cmms_work_orders:
        assert _one(seed, "SELECT status, reported_date, target_start, sched_start, actual_start, "
                          "actual_finish FROM cmms_indianapolis.work_orders WHERE wonum = %s", (wo.wonum,)) == [
            (wo.status, wo.reported_date, wo.target_start, wo.sched_start, wo.actual_start, wo.actual_finish)]
    for log in shist.plan(new).cmms_log:
        assert _one(seed, "SELECT log_date, entry FROM cmms_indianapolis.work_order_log "
                          "WHERE wonum = %s AND log_type = %s",
                    (shist.DEFERRED_PM_WONUM, log.log_type)) == [(log.log_date, log.entry)]
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum LIKE 'VIB-CMX-%%'") == [(0,)]
    # each past stop and its bridge order moved to the new era together (the
    # old era's runs are still in the ERP: only the story config was cleared)
    for stop in shist.plan(new).past_stops:
        assert _one(erp, "SELECT w.work_order_number, d.start_time, d.end_time, d.resolved_by "
                         "FROM mes_downtime_events d JOIN mes_work_orders w USING (work_order_id) "
                         "WHERE d.downtime_id = %s", (stop.downtime_id,)) == [
            (stop.work_order_number, stop.start, stop.end, stop.resolved_by)]
        assert _one(seed, "SELECT actual_start, actual_finish FROM cmms_indianapolis.work_orders "
                          "WHERE wonum = %s", (stop.order.wonum,)) == [(stop.start, stop.end)]
        assert _one(seed, "SELECT log_date, author FROM cmms_indianapolis.work_order_log "
                          "WHERE wonum = %s", (stop.order.wonum,)) == [(stop.log.log_date, stop.resolved_by)]
    again = shist.write(erp, seed, shist.plan(new))
    assert again["seed"] == _SEED_COUNTS_NOOP and again["erp"]["past_stops"] == 0


def test_pg_live_then_delete_restores_the_frozen_seed(pg_pair):
    """live write -> Delete: 1182 back to WAPPR, 1201 back to APPR, 1204 back
    to WAPPR, the description marks and supersede entries gone, 3101/3107/3108
    and the plant CLOSE orders gone, and the seed CMMS tables equal a fresh 011
    load again."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    frozen = _seed_snapshot(seed)
    shist.write(erp, seed, shist.plan(float(int(datetime.now(timezone.utc).timestamp()))))
    live = _pg_story_rows(seed)
    assert {w: live[w][0] for w in ("1182", "1201", "1187", "1195", shist.DEFERRED_PM_WONUM)} == {
        "1182": "CAN", "1201": "CAN", "1187": "COMP", "1195": "COMP", shist.DEFERRED_PM_WONUM: "WAPPR"}
    assert live["1182"][1].endswith(shist.SUPERSEDED_DESCRIPTION_SUFFIX)
    assert live["1187"][1].startswith(shist.PRIOR_ERA_PREFIX) and live["1195"][1].startswith(shist.PRIOR_ERA_PREFIX)
    # the plant open order 1204 (1001-CVA-100) is superseded, and the reserved
    # plant CLOSE orders exist, before the Delete
    assert _one(seed, "SELECT status FROM cmms_indianapolis.work_orders WHERE wonum = '1204'") == [("CAN",)]
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum = ANY(%s)",
                (list(shist.PLANT_PM_CLOSE_WONUMS),)) == [(len(shist.PLANT_PM_CLOSE_WONUMS),)]
    assert shist.restore_frozen_seed(seed) == {"restored_orders": 3, "prior_era_restored": 2,
                                            "history_work_orders": 4, "pm_schedule": 1,
                                            "plant_pm_work_orders": len(shist.PLANT_PM_BASELINE),
                                            "plant_pm_schedule": len(shist.PLANT_PM_BASELINE),
                                            "past_stop_orders": len(shist.PAST_STOPS)}
    assert _one(seed, "SELECT wonum, status FROM cmms_indianapolis.work_orders "
                      "WHERE wonum IN ('1182', '1201', '1204') ORDER BY 1") == [
        ("1182", "WAPPR"), ("1201", "APPR"), ("1204", "WAPPR")]
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum = '3101'") == [(0,)]
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum = ANY(%s)",
                (list(shist.PLANT_PM_CLOSE_WONUMS),)) == [(0,)]
    assert _seed_snapshot(seed) == frozen
    assert shist.restore_frozen_seed(seed) == {"restored_orders": 0, "prior_era_restored": 0,
                                            "history_work_orders": 0, "pm_schedule": 0,
                                            "plant_pm_work_orders": 0, "plant_pm_schedule": 0,
                                            "past_stop_orders": 0}


def test_pg_plant_pm_schedules_follow_the_live_run_and_reset_restores_them(pg_pair):
    """The fix, end to end on real PostgreSQL: before the live write every
    non-story Indianapolis pm_schedules row carries its 011 frozen dates (months
    overdue against a live run); after the write each row straddles the live run
    (last done before it, next due after it) and carries a CLOSE work order on
    its shifted last-completion date; a same-anchor rerun changes nothing; the
    Delete restores the seed rows exactly."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    frozen_schedules = {pm: (last, due) for pm, last, due in _one(
        seed, "SELECT pm_num, last_completion_date::text, next_due_date::text "
              "FROM cmms_indianapolis.pm_schedules WHERE pm_num = ANY(%s)",
        ([b.pm_num for b in shist.PLANT_PM_BASELINE],))}
    assert frozen_schedules == {b.pm_num: (b.last_completion, b.next_due) for b in shist.PLANT_PM_BASELINE}

    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    sp = shist.plan(anchor)
    out = shist.write(erp, seed, sp)
    assert out["seed"]["plant_pm_work_orders"] == len(shist.PLANT_PM_BASELINE)
    assert out["seed"]["plant_pm_schedule"] == len(shist.PLANT_PM_BASELINE)
    assert out["seed"]["superseded_plant_orders"] == 1          # 1204
    run_day = sp.failed_run.start.date()
    for p in sp.plant_pm:
        (last, due), = _one(seed, "SELECT last_completion_date, next_due_date "
                                  "FROM cmms_indianapolis.pm_schedules WHERE pm_num = %s", (p.pm_num,))
        assert last == p.last_completion and due == p.next_due
        assert last < run_day < due                            # no longer months overdue
        (status, asset, fin), = _one(
            seed, "SELECT status, asset_num, actual_finish::date FROM cmms_indianapolis.work_orders "
                  "WHERE wonum = %s", (p.close.wonum,))
        assert (status, asset, fin) == ("CLOSE", p.asset_num, p.last_completion)
    # a same-anchor rerun changes nothing on the seed side
    assert shist.write(erp, seed, shist.plan(anchor + 3600))["seed"] == _SEED_COUNTS_NOOP
    # the plant CLOSE orders are NOT in the facilitator answer key (story orders only)
    assert not set(shist.PLANT_PM_CLOSE_WONUMS) & set(shist.STORY_ORDER_WONUMS)
    # Delete restores every non-story schedule to its 011 frozen dates and drops the CLOSE orders
    shist.restore_frozen_seed(seed)
    after = {pm: (last, due) for pm, last, due in _one(
        seed, "SELECT pm_num, last_completion_date::text, next_due_date::text "
              "FROM cmms_indianapolis.pm_schedules WHERE pm_num = ANY(%s)",
        ([b.pm_num for b in shist.PLANT_PM_BASELINE],))}
    assert after == frozen_schedules
    assert _one(seed, "SELECT count(*) FROM cmms_indianapolis.work_orders WHERE wonum = ANY(%s)",
                (list(shist.PLANT_PM_CLOSE_WONUMS),)) == [(0,)]


def test_pg_legacy_superseded_pm_is_marked_then_restored_exactly(pg_pair):
    """An env written by the pre-status supersede (1182 CAN + the bare entry):
    the next write marks it and supersedes 1201; Delete still gives back
    exactly the fresh 011 rows."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    frozen = _seed_snapshot(seed)
    anchor = float(int(datetime.now(timezone.utc).timestamp()))
    with seed.cursor() as cur:
        cur.execute("UPDATE cmms_indianapolis.work_orders SET status = 'CAN' WHERE wonum = '1182'")
        cur.execute("INSERT INTO cmms_indianapolis.work_order_log (wonum, log_date, author, log_type, entry) "
                    "VALUES ('1182', %s, %s, 'UPDATE', %s)",
                    (shist.ts(anchor), shist.SUPERSEDED_LOG_AUTHOR, shist.SUPERSEDED_LOG_ENTRY))
    seed.commit()
    out = shist.write(erp, seed, shist.plan(anchor))
    assert (out["seed"]["superseded_orders"], out["seed"]["prior_era_marked"]) == (1, 2)
    assert out["seed"]["superseded_plant_orders"] == 1          # 1204 on 1001-CVA-100
    assert _pg_story_rows(seed)["1182"][1].endswith(shist.SUPERSEDED_DESCRIPTION_SUFFIX)
    assert shist.restore_frozen_seed(seed)["restored_orders"] == 3  # 1182, 1201, 1204
    assert _seed_snapshot(seed) == frozen


# ---------------------------------------------------------------------------
# Packaged loop length (ADR-0091): the failed batch scales with the window
# ---------------------------------------------------------------------------

from erp_simulation.tests._loop_env import run_under_loop  # noqa: E402

_HISTORY_DUMP = """\
from erp_simulation import condition_monitor as cm, story_history as shist
anchor = cm.snap_anchor(1.8e9)
p = shist.plan(anchor, 3)
w = shist.story_windows(anchor)
f = p.failed_run
e = lambda d: shist._epoch(d)
line3 = [(e(r.start), e(r.end)) for r in p.runs if r.line == f.line]
emit({"anchor": anchor, "w": w, "loop": cm.TOTAL_SECONDS,
      "consts": [shist.FAILED_OFFSET, list(shist.FAILED_CHECK_OFFSETS), shist.FAILED_RUN_SECONDS,
                 shist.ALARM_WINDOW_SECONDS, shist.SHIPPED_ALARM_WINDOW_SECONDS],
      "failed": [e(f.start), e(f.end), f.reject, f.planned, [c.fail_count for c in f.checks],
                 [e(c.check_time) for c in f.checks]],
      "downtime": [e(p.downtime.start), e(p.downtime.end)],
      "line3": line3, "numbers": [r.run_number for r in (*p.runs, f)],
      "config": {c.key: c.value for c in p.config}})
"""


def test_failed_batch_constants_are_the_shipped_ones_when_unset():
    assert (shist.FAILED_OFFSET, shist.FAILED_CHECK_OFFSETS, shist.FAILED_RUN_SECONDS) == (600, (1800, 3600, 5100), 5400)
    assert shist.ALARM_WINDOW_SECONDS == shist.SHIPPED_ALARM_WINDOW_SECONDS == 6430
    assert shist.FAILED_RUN_SECONDS == shist.RUN_SECONDS


def test_a_three_hour_loop_plans_a_congruent_history():
    out = run_under_loop("10800", _HISTORY_DUMP)
    w, (start, end, reject, planned, fails, check_times) = out["w"], out["failed"]
    assert out["loop"] == 10800 and out["anchor"] % 300 == 190
    assert out["consts"] == [180, [660, 1320, 1860], 1980, 2410, 6430]
    # the loop containing anchor - 1 d starts exactly a day back (86400 = 8 x 3 h)
    assert w["loop_start"] == out["anchor"] - 86400
    assert (w["alarm"] - w["loop_start"], w["fail"] - w["alarm"], w["repaired"] - w["fail"]) == (7310, 2410, 450)
    assert w["alarm"] % 300 == 0 and w["repaired"] < w["loop_start"] + 10800
    # the failed batch and its three checks sit inside the alarm window ...
    assert w["alarm"] < start and end < w["fail"]
    assert all(start < t < w["fail"] for t in check_times)
    # ... with the shipped batch's shape: 5 / 6 / 7 of 20 fail, 30 % rejects
    assert fails == [5, 6, 7] and (reject, planned) == (18, 60)
    assert out["downtime"] == [w["fail"], w["repaired"]]
    assert all(e <= start or s >= end for s, e in out["line3"]), "no line-3 overlap"
    assert len(set(out["numbers"])) == len(out["numbers"]) == 19
    cfg = out["config"]
    assert (cfg["story_loop_seconds"], cfg["story_bpfo_alarm_step"], cfg["story_fail_step"],
            cfg["story_repair_steps"]) == ("10800", "1462", "1944", "90")


def test_the_unset_history_is_byte_identical_in_a_fresh_import():
    out = run_under_loop(None, _HISTORY_DUMP)
    p = shist.plan(out["anchor"], 3)
    assert out["numbers"] == [r.run_number for r in (*p.runs, p.failed_run)]
    assert out["config"] == {c.key: c.value for c in p.config}
    assert out["failed"][2:5] == [p.failed_run.reject, p.failed_run.planned,
                                  [c.fail_count for c in p.failed_run.checks]]


# ---------------------------------------------------------------------------
# Every accepted loop (a multiple of 300 in [3600, 28800]): the failed batch
# never overlaps a normal line-3 run, and every failed-run check sits inside
# the BPFO-alarm window. Above 8 h it did (42600..51300 s, 75600..86400 s),
# which is why the range is capped at the shipped default.
# ---------------------------------------------------------------------------

_ACCEPTED_LOOPS = tuple(range(cm.LOOP_SECONDS_RANGE[0], cm.LOOP_SECONDS_RANGE[1] + 1, cm.LOOP_SECONDS_MULTIPLE))
_SWEEP_DUMP = """\
from erp_simulation import condition_monitor as cm, story_history as shist
anchor = float(cm.snap_anchor(1.8e9, "down"))
sp, w = shist.plan(anchor), shist.story_windows(anchor)
failed = sp.failed_run
start, end = shist._epoch(failed.start), shist._epoch(failed.end)
line3 = [[shist._epoch(r.start), shist._epoch(r.end)] for r in sp.runs if r.line == cm.STORY_MES_LINE]
emit({"loop": cm.TOTAL_SECONDS, "failed": [start, end], "line3": line3,
      "alarm": w["alarm"], "fail": w["fail"], "checks": [shist._epoch(c.check_time) for c in failed.checks],
      "line": failed.line, "stops": len(sp.past_stops)})
"""


@pytest.fixture(scope="module")
def loop_sweep():
    """``{loop: dump}`` for every accepted loop, one fresh interpreter each (run concurrently)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        dumps = pool.map(lambda loop: run_under_loop(str(loop), _SWEEP_DUMP), _ACCEPTED_LOOPS)
        return dict(zip(_ACCEPTED_LOOPS, dumps))


def test_the_sweep_covers_the_whole_capped_range():
    assert _ACCEPTED_LOOPS[0] == 3600 and _ACCEPTED_LOOPS[-1] == 28800 and len(_ACCEPTED_LOOPS) == 85


@pytest.mark.parametrize("loop", _ACCEPTED_LOOPS)
def test_every_accepted_loop_keeps_the_failed_batch_clear_of_line_3_and_its_checks_in_the_window(
        loop_sweep, loop):
    out = loop_sweep[loop]
    assert out["loop"] == loop and out["line"] == cm.STORY_MES_LINE
    start, end = out["failed"]
    assert out["alarm"] <= start < end <= out["fail"]
    assert [r for r in out["line3"] if r[0] < end and start < r[1]] == []
    assert len(out["checks"]) == 3
    assert all(out["alarm"] <= t < out["fail"] and start <= t <= end for t in out["checks"])
    assert out["stops"] == len(shist.PAST_STOPS)   # every loop length keeps all six stops
