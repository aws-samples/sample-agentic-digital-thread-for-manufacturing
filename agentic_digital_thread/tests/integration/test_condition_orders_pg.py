# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simulator work orders and the facilitator answer key on real PostgreSQL.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (the
``test_story_lookup_pg.py`` convention). Each test builds a fresh account (a
db-init ERP database and a seed database with the site CMMS), writes the live
story history on a congruent anchor (or leaves the account frozen) and drives
the REAL ``MESSimulator.monitor_asset_condition`` against the seed database,
with the ERP's MES downtime read through ``MESSimulator.read_downtimes``,
then reads the answer key through ``story_lookup.lookup``:

* a fresh account's answer key is the same in every loop-0 phase as it was
  before any simulator order was ever completed (VIB-CMX-000000 and a bridge
  order raised during the alarm stay open through the repair and the healthy
  tail), and the first cycle of loop 1 completes both;
* a long-running account with thirteen stale condition orders and three stale
  MES-bridge orders is brought back by ONE cycle (each completed in its own
  repair or its own downtime, one log entry each), its answer key fits the row
  limit again, and a second cycle changes nothing;
* a long-running FROZEN account whose bridge orders pushed the seed's deferred
  PM off the end of the frozen key gets the seeded key back after one cycle;
* a placement afterwards still retires the old era (its completed condition
  orders and their log entries go together).

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_condition_orders_pg.py -q
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import db_routing, story_lookup as sl  # noqa: E402

from .story_pg_support import _build  # noqa: E402
from ._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_EMPTY_CUSTOMERS = "customers: no matching orders in the ERP yet"
_DOWNTIME_BY = "maint_02"


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _make_account(monkeypatch, *, live: bool):
    """A fresh account (live: history 20 loops before now), the lookup routed at it."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    from agents import pgdriver
    from agents.pgdriver import make_dsn, parse_dsn
    from erp_simulation import condition_monitor as cm, mes_simulator, story_history
    names = {"erp": "cmxerp_" + uuid.uuid4().hex[:8], "seed": "cmxseed_" + uuid.uuid4().hex[:8]}
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    dsns = {k: make_dsn(**{**parse_dsn(_DSN), "dbname": v}) for k, v in names.items()}
    conns = {k: pgdriver.connect(d) for k, d in dsns.items()}
    try:
        _build(conns)
        anchor = 0.0
        if live:
            anchor = float(cm.snap_anchor(time.time() - 20 * cm.TOTAL_SECONDS, "down"))
            story_history.write(conns["erp"], conns["seed"], story_history.plan(anchor))
        monkeypatch.setattr(db_routing, "demo_pg_dsn",
                            lambda schema: dsns["erp"] if db_routing.is_erp_schema(schema) else dsns["seed"])
        monkeypatch.setattr(mes_simulator, "get_config", lambda *a, **k: object())
        yield SimpleNamespace(erp=conns["erp"], seed=conns["seed"], anchor=anchor)
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


@pytest.fixture
def account(monkeypatch):
    yield from _make_account(monkeypatch, live=True)


@pytest.fixture
def frozen_account(monkeypatch):
    yield from _make_account(monkeypatch, live=False)


def _simulator(account):
    """The real MES simulator on the account's clock, seed database and ERP downtime."""
    from erp_simulation import story_live
    from erp_simulation.mes_simulator import MESSimulator

    def _stored_anchor():
        try:
            return story_live.read_anchor(account.erp)
        finally:
            account.erp.rollback()

    def _downtimes(ids):
        try:
            return MESSimulator.read_downtimes(account.erp, ids)
        finally:
            account.erp.rollback()
    sim = MESSimulator()
    sim.condition_anchor_epoch = account.anchor
    sim.condition_anchor_reader = _stored_anchor if account.anchor else None
    sim.condition_downtime_reader = _downtimes
    sim._get_seed_db_conn = lambda: account.seed
    return sim


def _bridge(account, reported: float, *, record: bool = True) -> SimpleNamespace:
    """A VIB-BEARING downtime in the ERP (35 to 25 minutes before ``reported``)
    and the bridge's own order for it in the seed, entered at ``reported``. The
    bridge is handed the stop's start, as the production caller hands it, and
    the open order already carries it as its actual start."""
    from erp_simulation.mes_simulator import MESSimulator
    from erp_simulation import condition_monitor as cm
    downtime_id = str(uuid.uuid4())
    entered = _utc(reported)
    start, end = entered - timedelta(minutes=35), entered - timedelta(minutes=25)
    if record:
        with account.erp.cursor() as cur:
            cur.execute("INSERT INTO mes_downtime_events (downtime_id, line_id, downtime_category, "
                        "downtime_reason, start_time, end_time, resolved_by, resolution_notes) "
                        "VALUES (%s, %s, 'MECHANICAL', 'VIB-BEARING', %s, %s, %s, 'Resolved after 10 min')",
                        (downtime_id, cm.STORY_MES_LINE, start, end, _DOWNTIME_BY))
        account.erp.commit()
    writer = MESSimulator.__new__(MESSimulator)          # the bridge uses class constants only
    writer._get_seed_db_conn = lambda: account.seed
    site = SimpleNamespace(execute=lambda sql, params=None: None,
                           fetchone=lambda: (MESSimulator.CONDITION_MONITOR_SITE,))
    writer._create_cmms_work_order_for_downtime(
        site, "mes-work-order", cm.STORY_MES_LINE, "MECHANICAL", "VIB-BEARING", downtime_id,
        downtime_start=start)
    wonum = f"IND-{downtime_id.replace('-', '')[:8].upper()}"
    with account.seed.cursor() as cur:
        cur.execute(f"UPDATE {sl.schema('cmms')}.work_orders SET reported_date = %s, target_start = %s, "  # nosec B608 - validated schema of a fixed role
                    "sched_start = %s WHERE wonum = %s", (entered, entered, entered, wonum))
        assert cur.rowcount == 1
    account.seed.commit()
    status, _reported, _sched, open_start, open_finish = _orders(account, wonum)[wonum]
    assert (status, open_start, open_finish) == ("WAPPR", start, None), wonum
    return SimpleNamespace(wonum=wonum, start=start, end=end, reported=entered)


def _at(account, loop: int, step: int) -> float:
    from erp_simulation import condition_monitor as cm
    return account.anchor + loop * cm.TOTAL_SECONDS + step * cm.INTERVAL_SEC + 1.0


def _phase_steps():
    from erp_simulation import condition_monitor as cm
    return {"degrading": cm.BPFO_ALARM_STEP // 2, "bpfo_alarm": cm.BPFO_ALARM_STEP + 120,
            "repair": cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS // 2,
            "healthy": (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS + cm.NUM_STEPS) // 2}


def _orders(account, pattern: str) -> dict:
    with account.seed.cursor() as cur:
        cur.execute(f"SELECT wonum, status, reported_date, sched_start, actual_start, actual_finish "
                    f"FROM {sl.schema('cmms')}.work_orders WHERE wonum LIKE %s ORDER BY wonum",  # nosec B608 - validated schema of a fixed role
                    (pattern,))
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    account.seed.rollback()
    return rows


def _condition_rows(account) -> dict:
    return _orders(account, sl.CONDITION_WONUM_PATTERN)


def _completion_log(account, pattern: str = sl.CONDITION_WONUM_PATTERN) -> list:
    with account.seed.cursor() as cur:
        cur.execute(f"SELECT wonum, log_date, author, log_type, entry FROM {sl.schema('cmms')}.work_order_log "
                    "WHERE wonum LIKE %s ORDER BY wonum, log_id",  # nosec B608 - validated schema of a fixed role
                    (pattern,))
        rows = cur.fetchall()
    account.seed.rollback()
    return rows


def _answer_key_maintenance(now: float, story: str = "live") -> tuple[dict, list]:
    key = sl.lookup(story, now=now)
    return {w["wonum"]: w["status"] for w in key["maintenance"]["work_orders"]}, key["warnings"]


def _assert_bridge_completed(account, b) -> None:
    from erp_simulation.mes_simulator import MESSimulator
    status, _reported, _sched, start, finish = _orders(account, b.wonum)[b.wonum]
    assert (status, start, finish) == ("COMP", b.start, b.end), b.wonum
    assert _completion_log(account, b.wonum) == [
        (b.wonum, max(b.end, b.reported), _DOWNTIME_BY, "COMPLETE", MESSimulator.BRIDGE_COMPLETION_ENTRY)]


def test_a_fresh_accounts_answer_key_is_unchanged_in_every_loop_zero_phase(account):
    from erp_simulation import condition_monitor as cm, story_history
    from erp_simulation.mes_simulator import MESSimulator
    sim = _simulator(account)
    history = {story_history.DEFERRED_PM_WONUM: "WAPPR", story_history.CONDITION_WONUM: "COMP",
               story_history.REPAIR_WONUM: "COMP"}
    order = MESSimulator._loop_wonum(0)
    raised_at = _at(account, 0, cm.BPFO_ALARM_STEP + 60)    # the bridge order, 5 min into the alarm
    bridge = None
    samples = {_at(account, 0, step): phase for phase, step in _phase_steps().items()}
    seen = {}
    for k in range(cm.TOTAL_SECONDS // 300):          # a cycle every five minutes through loop 0
        now = account.anchor + 300 * k + 7.0
        if bridge is None and now >= raised_at:
            bridge = _bridge(account, raised_at)
        sim.monitor_asset_condition(f"c{k}", now=now)
        for sample, phase in samples.items():
            if now <= sample < now + 300:
                seen[phase] = _answer_key_maintenance(sample)
    on_file = {**history, order: "WAPPR", bridge.wonum: "WAPPR"}
    assert seen == {"degrading": (history, []), "bpfo_alarm": (on_file, []),
                    "repair": (on_file, []), "healthy": (on_file, [])}
    assert _completion_log(account) == [] and _completion_log(account, bridge.wonum) == []

    # The first cycle of loop 1 completes both; the answer key no longer lists them.
    sim.monitor_asset_condition("loop-1", now=account.anchor + cm.TOTAL_SECONDS + 7.0)
    _assert_bridge_completed(account, bridge)
    assert _condition_rows(account)[order][0] == "COMP"
    assert _answer_key_maintenance(account.anchor + cm.TOTAL_SECONDS + 60.0) == (history, [])


def test_a_long_running_account_is_brought_back_by_one_cycle(account):
    from erp_simulation import condition_monitor as cm, story_history
    from erp_simulation.mes_simulator import MESSimulator
    # The measured rehearsal state, inside loop 13's alarm: fourteen open condition
    # orders (loops 0..13), three open MES-bridge bearing orders, the history's three.
    with account.seed.cursor() as cur:
        for loop in range(14):
            inside = _at(account, loop, cm.BPFO_ALARM_STEP)
            MESSimulator.insert_condition_work_order(
                cur, loop, cm.alarm_epoch(inside, account.anchor), cm.fail_epoch(inside, account.anchor))
    account.seed.commit()
    bridge = [_bridge(account, _at(account, 3 * i + 1, cm.BPFO_ALARM_STEP + 60)) for i in range(3)]
    now = _at(account, 13, cm.BPFO_ALARM_STEP + 10)        # loop 13, inside its alarm
    before, warnings = _answer_key_maintenance(now)
    assert len(before) == sl.ROW_LIMIT and warnings == [f"maintenance: list truncated at {sl.ROW_LIMIT}"]
    sim = _simulator(account)
    assert sim.monitor_asset_condition("first-cycle", now=now) == 0   # loop 13's own is on file

    rows = _condition_rows(account)
    assert {w for w, r in rows.items() if r[0] == "WAPPR"} == {MESSimulator._loop_wonum(13)}
    repair = timedelta(seconds=cm.BEARING_REPAIR_STEPS * cm.INTERVAL_SEC)
    for loop in range(13):
        status, _reported, sched, start, finish = rows[MESSimulator._loop_wonum(loop)]
        fail = _utc(account.anchor + loop * cm.TOTAL_SECONDS + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC)
        assert (status, sched, start, finish) == ("COMP", fail, fail, fail + repair), loop
    log = _completion_log(account)
    assert [(w, d, a, t, e) for w, d, a, t, e in log] == [
        (MESSimulator._loop_wonum(loop), rows[MESSimulator._loop_wonum(loop)][4],
         MESSimulator.CONDITION_COMPLETION_AUTHOR, "COMPLETE", MESSimulator.CONDITION_COMPLETION_ENTRY)
        for loop in range(13)]
    for b in bridge:
        _assert_bridge_completed(account, b)

    maintenance, warnings = _answer_key_maintenance(now)
    assert warnings == []
    assert maintenance == {story_history.DEFERRED_PM_WONUM: "WAPPR", story_history.CONDITION_WONUM: "COMP",
                           story_history.REPAIR_WONUM: "COMP", MESSimulator._loop_wonum(13): "WAPPR"}

    # A second cycle, and a cycle after a restart, change nothing.
    bridge_log = _completion_log(account, sl.BRIDGE_WONUM_PATTERN)
    assert sim.monitor_asset_condition("again", now=now + 60) == 0
    assert _simulator(account).monitor_asset_condition("restart", now=now + 120) == 0
    assert _condition_rows(account) == rows and _completion_log(account) == log
    assert _completion_log(account, sl.BRIDGE_WONUM_PATTERN) == bridge_log


def test_a_long_running_frozen_account_gets_its_seeded_answer_key_back(frozen_account):
    """The frozen key lists the story asset's open PM and its EM bearing orders
    newest first: once 19 bridge orders pile up, the seed's deferred PM 1182
    falls off the end. One wall-clock cycle completes them and the key is the
    fresh frozen key again."""
    from erp_simulation import condition_monitor as cm
    fresh = sl.lookup("frozen", now=time.time())
    assert fresh["warnings"] == [_EMPTY_CUSTOMERS]
    loop_start = cm.loop_start_epoch(time.time())
    bridge = [_bridge(frozen_account, loop_start - 1800.0 - 3600.0 * i) for i in range(19)]
    piled = sl.lookup("frozen", now=time.time())
    assert len(piled["maintenance"]["work_orders"]) == sl.ROW_LIMIT
    assert piled["maintenance"]["deferred_pm_wonum"] is None              # 1182 fell off the end
    assert f"maintenance: list truncated at {sl.ROW_LIMIT}" in piled["warnings"]

    _simulator(frozen_account).monitor_asset_condition("wall-clock", now=time.time())
    for b in bridge:
        _assert_bridge_completed(frozen_account, b)
    back = sl.lookup("frozen", now=time.time())
    assert back["warnings"] == [_EMPTY_CUSTOMERS]
    assert back["maintenance"] == fresh["maintenance"]


def test_a_placement_still_retires_the_old_era_after_completions(account):
    from erp_simulation import condition_monitor as cm, story_replan
    from erp_simulation.mes_simulator import MESSimulator
    sim = _simulator(account)
    for loop in range(3):                     # three loops of alarms, each closed after its loop
        sim.monitor_asset_condition(f"a{loop}", now=_at(account, loop, cm.BPFO_ALARM_STEP + 10))
    now = _at(account, 3, cm.BPFO_ALARM_STEP // 2)
    sim.monitor_asset_condition("close", now=now)
    assert {w: r[0] for w, r in _condition_rows(account).items()} == {
        MESSimulator._loop_wonum(loop): "COMP" for loop in range(3)}
    assert len(_completion_log(account)) == 3

    report = story_replan.replan(account.erp, account.seed, now - 3600, now=now, source="placed",
                                 expected_anchor=account.anchor)
    assert report["retired"]["condition_orders"] == 3
    assert _condition_rows(account) == {} and _completion_log(account) == []


def _rows_of(account, wonums) -> dict:
    with account.seed.cursor() as cur:
        cur.execute(stmt("SELECT * FROM {schema}.work_orders WHERE wonum = ANY(%s) ORDER BY wonum",
                         schema=sl.schema("cmms")), (list(wonums),))
        cols = [d[0] for d in cur.description]
        rows = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    account.seed.rollback()
    return rows


def test_ind_orders_the_bridge_cannot_prove_stay_open_and_a_persons_completed_one_stays_listed(account):
    """On the real SQL: only the proven bridge order is completed; a person's
    IND-* orders (story asset and another asset), a bridge order with an
    edited description and one with another reporter stay exactly as they are
    across two loops. The answer keys hide only the completed bridge order: a
    person's completed IND-* bearing order is maintenance history and stays."""
    from erp_simulation import condition_monitor as cm
    sim = _simulator(account)
    entered = _at(account, 1, cm.BPFO_ALARM_STEP + 60)
    proven, edited, other = (_bridge(account, entered) for _ in range(3))
    table = sl.schema("cmms")
    with account.seed.cursor() as cur:
        cur.execute(f"UPDATE {table}.work_orders SET description = description || ' Bearing on order.' "  # nosec B608 - validated schema of a fixed role
                    "WHERE wonum = %s", (edited.wonum,))
        cur.execute(stmt("UPDATE {schema}.work_orders SET reported_by = 'R. Roe' WHERE wonum = %s", schema=table),
                    (other.wonum,))
        for wonum, asset, work_type, status in (("IND-12345678", sl.STORY_CMMS_ASSET_NUM, "EM", "WAPPR"),
                                                ("IND-87654321", "1001-CGR-502", "CM", "WMATL"),
                                                ("IND-0000ABCD", sl.STORY_CMMS_ASSET_NUM, "EM", "COMP")):
            cur.execute(
                f"INSERT INTO {table}.work_orders (wonum, asset_num, work_type, description, status, priority, "  # nosec B608 - validated schema of a fixed role
                "reported_by, reported_date, failure_code) VALUES (%s, %s, %s, %s, %s, 2, 'R. Roe', %s, 'BEARING')",
                (wonum, asset, work_type, "Spindle coolant leak; waiting on parts", status, _utc(entered)))
        # An imported completed order with no reporter is not the bridge's either.
        cur.execute(
            f"INSERT INTO {table}.work_orders (wonum, asset_num, work_type, description, status, priority, "  # nosec B608 - validated schema of a fixed role
            "reported_by, reported_date, failure_code) VALUES (%s, %s, 'EM', %s, 'COMP', 2, NULL, %s, 'BEARING')",
            ("IND-0000BEEF", sl.STORY_CMMS_ASSET_NUM, "Spindle bearing replaced", _utc(entered)))
    account.seed.commit()
    # The story history's past stops are completed bridge orders already: the
    # close-out leaves them (and their COMPLETE entries) exactly as written.
    from erp_simulation import story_history as sh
    untouched = (edited.wonum, other.wonum, "IND-12345678", "IND-87654321", "IND-0000ABCD", "IND-0000BEEF",
                 *sh.PAST_STOP_WONUMS)
    before = _rows_of(account, untouched)
    history_log = [r for r in _completion_log(account, sl.BRIDGE_WONUM_PATTERN) if r[0] in sh.PAST_STOP_WONUMS]
    assert len(before) == len(untouched) and len(history_log) == len(sh.PAST_STOP_WONUMS)

    sim.monitor_asset_condition("next-loop", now=_at(account, 2, cm.BPFO_ALARM_STEP // 2))
    sim.monitor_asset_condition("loop-after", now=_at(account, 3, cm.BPFO_ALARM_STEP // 2))
    _assert_bridge_completed(account, proven)
    assert _rows_of(account, untouched) == before
    bridge_log = _completion_log(account, sl.BRIDGE_WONUM_PATTERN)
    assert [w for w, *_ in bridge_log if w not in sh.PAST_STOP_WONUMS] == [proven.wonum]
    assert [r for r in bridge_log if r[0] in sh.PAST_STOP_WONUMS] == history_log

    live, warnings = _answer_key_maintenance(_at(account, 3, cm.BPFO_ALARM_STEP // 2))
    assert warnings == []
    assert proven.wonum not in live
    assert {w: live.get(w) for w in (edited.wonum, other.wonum, "IND-12345678", "IND-0000ABCD",
                                     "IND-0000BEEF")} == {
        edited.wonum: "WAPPR", other.wonum: "WAPPR", "IND-12345678": "WAPPR", "IND-0000ABCD": "COMP",
        "IND-0000BEEF": "COMP"}
    frozen, _warnings = _answer_key_maintenance(time.time(), story="frozen")
    assert "IND-0000ABCD" in frozen and "IND-0000BEEF" in frozen and proven.wonum not in frozen


def test_a_frozen_order_with_no_status_is_not_taken_for_cancelled(frozen_account):
    """The frozen key counts every order line for the part that is not
    cancelled; an order whose status is NULL (allowed by the ERP's DDL) is
    not cancelled, so it counts. A cancelled one still does not."""
    fresh = sl.lookup("frozen", now=time.time())
    item = fresh["part"]["item_id"]
    erp = frozen_account.erp
    with erp.cursor() as cur:
        cur.execute("SELECT customer_id, customer_code FROM customers ORDER BY customer_code LIMIT 2")
        (open_id, open_code), (cancelled_id, _cancelled_code) = cur.fetchall()
        cur.execute("SELECT product_id FROM products WHERE engineering_part_number = %s LIMIT 1", (item,))
        (product_id,) = cur.fetchone()
        for number, customer, status in (("ORD-NULL-STATUS", open_id, None),
                                         ("ORD-CANCELLED", cancelled_id, sl.CANCELLED_ORDER_STATUS)):
            cur.execute("INSERT INTO customer_orders (order_number, customer_id, status) VALUES (%s, %s, %s) "
                        "RETURNING order_id", (number, customer, status))
            (order_id,) = cur.fetchone()
            cur.execute("INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price) "
                        "VALUES (%s, 1, %s, 4, 250.00)", (order_id, product_id))
    erp.commit()
    key = sl.lookup("frozen", now=time.time())
    assert [c["customer_code"] for c in key["customers"]] == [open_code]
    assert key["total_value"] == 1000.0
