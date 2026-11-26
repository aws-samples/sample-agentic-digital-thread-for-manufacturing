# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Facilitator story lookup against real PostgreSQL (ADR-0085 decision 6).

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (the same
convention as ``erp_simulation/tests/test_story_history.py``). Builds TWO
databases the way a workshop account has them:

* ERP: ``lambda-db-init`` ``create_schema`` + ``seed_data`` (incl. the
  one-ERP machining site and the ``mes_<site>`` views);
* seed: ``lambda-adt-seed`` 001 / 003 / 005 / 011 (schemas, the frozen plant
  MES, PLM, the site CMMS).

Then (1) every ``SQL_*`` statement of ``agents.demo.story_lookup`` is
``EXPLAIN``ed with representative parameters through the lookup's own
read-only sessions, and (2) ``lookup('frozen')`` and — after
``erp_simulation.story_history.write`` — ``lookup('live')`` run end to end
with no warning except frozen's expected empty-customers one (a fresh ERP has
booked no order for the frozen part); then (3) ``story_live.apply_cycle`` at
the loop's BPFO alarm starts the RUNNING story run with a failing check, and
the lookup reports it as ``live_defect_run`` while ``failed_batch`` stays the
history's SHIPPED failed run; and (4) a steward placement
(``erp_simulation.story_replan.replan``, source ``placed``) moves the answer
key to the new era: ``anchor_source placed``, the new history failed run, the
same customers, a reset telemetry cursor and no ``live_defect_run``.

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_story_lookup_pg.py -q
"""
from __future__ import annotations

import importlib.util
import os
import string
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import db_routing, story_lookup as sl  # noqa: E402
from tests.integration._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
#: lambda-adt-seed files by number: schemas, frozen plant MES, PLM, site CMMS.
_SEED_SQL_NUMBERS = ("001", "003", "005", "011")
_EMPTY_CUSTOMERS = "customers: no matching orders in the ERP yet"

#: Representative bound parameters per statement (values are placeholders
#: of the right type; EXPLAIN only needs them to plan).
_EXPLAIN_PARAMS = {
    "SQL_FROZEN_BATCH": (sl.FROZEN_FAILED_STATUS, sl.STORY_EQUIPMENT_ID),
    "SQL_RELEASED_BY_DIGITS": (sl.RELEASED_STATE, "0000"),
    "SQL_PLM_ITEM": ("PN-0000",),
    "SQL_PART_CUSTOMERS": ("PN-0000", sl.CANCELLED_ORDER_STATUS, sl.CUSTOMER_LIMIT),
    "SQL_BATCH_CUSTOMERS": (["RUN-0"], sl.CUSTOMER_LIMIT),
    "SQL_BATCH_LOTS": ("RUN-0", sl.ROW_LIMIT),
    "SQL_LOT_SHIPPED_BATCHES": (["LOT-0"], sl.ROW_LIMIT),
    "SQL_STORY_CONFIG": (list(sl.CONFIG_KEYS), len(sl.CONFIG_KEYS)),
    "SQL_LIVE_BATCH": (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, "RUN-0"),
    "SQL_LIVE_DEFECT_RUN": (sl.STORY_MES_LINE, sl.DEFECT_CHECK_TYPE, ["RUN-0"]),
    "SQL_MAINTENANCE": (sl.STORY_CMMS_ASSET_NUM, sl.PM_WORK_TYPE, sl.OPEN_PM_STATUS,
                        sl.FROZEN_BEARING_WORK_TYPES, sl.BEARING_FAILURE_CODE, None,
                        *sl.SIMULATOR_COMPLETED_PARAMS, sl.ROW_LIMIT),
    "SQL_LIVE_MAINTENANCE": (sl.STORY_CMMS_ASSET_NUM, sl.PM_WORK_TYPE, sl.OPEN_PM_STATUS,
                             sl.LIVE_BEARING_WORK_TYPES, sl.BEARING_FAILURE_CODE,
                             sl.CONDITION_WONUM_PATTERN, *sl.SIMULATOR_COMPLETED_PARAMS,
                             datetime(2026, 1, 1),
                             list(sl.HISTORY_REPAIR_WONUMS), sl.ROW_LIMIT),
}


def _load_db_init():
    spec = importlib.util.spec_from_file_location(
        "db_init_handler_story_lookup_pg", _REPO / "lambda-db-init" / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(conns: dict) -> None:
    db_init = _load_db_init()
    db_init.create_schema(conns["erp"])
    db_init.seed_data(conns["erp"])
    with conns["seed"].cursor() as cur:
        for number in _SEED_SQL_NUMBERS:
            (path,) = (_REPO / "lambda-adt-seed").glob(f"{number}_*.sql")
            cur.execute(path.read_text(encoding="utf-8"))
    conns["seed"].commit()


@pytest.fixture(scope="module")
def pg_pair():
    """{erp|seed: (dsn, writable connection)} on two throwaway databases."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    from agents import pgdriver
    from agents.pgdriver import make_dsn, parse_dsn
    names = {"erp": "slerp_" + uuid.uuid4().hex[:8], "seed": "slseed_" + uuid.uuid4().hex[:8]}
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    dsns = {k: make_dsn(**{**parse_dsn(_DSN), "dbname": v}) for k, v in names.items()}
    conns = {k: pgdriver.connect(d) for k, d in dsns.items()}
    try:
        _build(conns)
        yield {k: (dsns[k], conns[k]) for k in names}
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=name))
        admin.close()


@pytest.fixture
def routed(pg_pair, monkeypatch):
    """Point the lookup's DSN builder at the two test databases (by the SAME
    ``is_erp_schema`` rule the production routing uses)."""
    erp_dsn, seed_dsn = pg_pair["erp"][0], pg_pair["seed"][0]
    monkeypatch.setattr(db_routing, "demo_pg_dsn",
                        lambda schema: erp_dsn if db_routing.is_erp_schema(schema) else seed_dsn)
    return pg_pair


def _roles(sql: str) -> set:
    return {f for _t, f, _s, _c in string.Formatter().parse(sql) if f}


def test_every_statement_explains_on_the_real_ddl(routed):
    statements = {n: v for n, v in vars(sl).items() if n.startswith("SQL_")}
    assert set(statements) == set(_EXPLAIN_PARAMS), "add EXPLAIN params for a new statement"
    db = sl.ReadOnlyDb()
    try:
        for name, sql in statements.items():
            schemas = {sl.schema(r) for r in _roles(sql)}
            assert len({db_routing.is_erp_schema(s) for s in schemas}) == 1, name  # one database
            plan = db.rows(next(iter(schemas)), "EXPLAIN " + sl.render(sql), _EXPLAIN_PARAMS[name])
            assert plan, name
    finally:
        db.close()


def test_frozen_then_live_end_to_end(routed):
    from erp_simulation import story_history

    frozen = sl.lookup("frozen", now=time.time())
    assert frozen["warnings"] == [_EMPTY_CUSTOMERS], frozen["warnings"]
    assert frozen["failed_batch"]["status"] == sl.FROZEN_FAILED_STATUS
    assert frozen["failed_batch"]["equipment_id"] == sl.STORY_EQUIPMENT_ID
    assert frozen["part"]["lifecycle_state"] == sl.RELEASED_STATE
    frozen_wos = {w["wonum"]: w for w in frozen["maintenance"]["work_orders"]}
    assert frozen["maintenance"]["deferred_pm_wonum"] in frozen_wos
    assert any(w["failure_code"] == sl.BEARING_FAILURE_CODE for w in frozen_wos.values())

    anchor = float(int(time.time()))
    story_plan = story_history.plan(anchor)
    story_history.write(routed["erp"][1], routed["seed"][1], story_plan)

    live = sl.lookup("live", now=anchor + 60)
    assert live["warnings"] == [], live["warnings"]
    assert live["clock"]["anchor_epoch"] == anchor
    assert live["clock"]["anchor_source"] == "deploy"
    assert live["clock"]["telemetry"] == {
        "anchor": anchor, "cursor_epoch": story_history.telemetry_cursor(anchor, story_plan.days),
        "backfill_complete": False}           # the minute step has not swept yet
    assert live["failed_batch"]["run_number"] == story_plan.failed_run.run_number
    assert live["failed_batch"]["is_history_run"] is True
    assert live["part"]["item_id"] == live["failed_batch"]["engineering_part_number"]
    assert {c["customer_code"] for c in live["customers"]} == set(story_history.STORY_CUSTOMER_CODES)
    assert live["total_value"] == 10200
    # Every other history run shipped too (one order each, ADR-0087): the
    # failed batch is still the newest SHIPPED run with a failing DIMENSIONAL
    # check, and the answer key (customers, value) does not widen.
    with routed["erp"][1].cursor() as cur:
        cur.execute("SELECT count(DISTINCT batch_id) FROM shipment_items WHERE batch_id = ANY(%s)",
                    ([r.run_number for r in (*story_plan.runs, story_plan.failed_run)],))
        assert cur.fetchone() == (len(story_plan.runs) + 1,)
    # THIS era only: the history's story orders, none of the frozen-era seed orders
    live_wos = {w["wonum"] for w in live["maintenance"]["work_orders"]}
    assert live_wos == set(story_history.STORY_ORDER_WONUMS)
    assert not live_wos & set(frozen_wos)
    # The PM's last completed cycle is in the CMMS on the schedule's last
    # completion date, but it is routine history: the answer key leaves it out.
    last, _due = story_history._pm_schedule_dates(story_plan)
    with routed["seed"][1].cursor() as cur:
        cur.execute(f"SELECT status, actual_finish::date FROM {story_history.CMMS_SCHEMA}.work_orders "  # nosec B608 - schema is a module constant, never input
                    "WHERE wonum = %s", (story_history.LAST_PM_WONUM,))
        assert cur.fetchone() == ("CLOSE", date.fromisoformat(last))
        cur.execute(f"SELECT last_completion_date FROM {story_history.CMMS_SCHEMA}.pm_schedules "  # nosec B608 - schema is a module constant, never input
                    "WHERE pm_num = %s", (story_history.STORY_PM_NUM,))
        assert cur.fetchone() == (date.fromisoformat(last),)
    assert story_history.LAST_PM_WONUM not in live_wos
    assert live["maintenance"]["deferred_pm_wonum"] == story_history.DEFERRED_PM_WONUM
    assert live["live_defect_run"] is None          # no BPFO alarm yet
    # The suspect lot: the failed batch's bar stock lot fed several shipped
    # batches of the part (not every one), reaching one customer more.
    lot = live["suspect_lot"]
    failed = story_plan.failed_run
    lot_runs = sorted(r.run_number for r in (*story_plan.runs, failed) if r.lot_number == failed.lot_number)
    assert [r["lot_number"] for r in lot["lots"]] == [failed.lot_number]
    assert lot["lots"][0]["supplier_code"] == story_history.HISTORY_LOT_SUPPLIER
    assert lot["shipped_batches"] == lot_runs
    assert {c["customer_code"] for c in lot["customers"]} > set(story_history.STORY_CUSTOMER_CODES)
    assert lot["total_value"] > live["total_value"]


def test_live_story_run_is_the_live_defect_run_not_the_failed_batch(routed):
    from erp_simulation import condition_monitor as cm, story_history, story_live

    erp = routed["erp"][1]
    anchor = story_live.read_anchor(erp)
    if anchor is None:          # run alone: write the history first
        anchor = float(int(time.time()))
        story_history.write(erp, routed["seed"][1], story_history.plan(anchor))
    history_run = story_history.plan(anchor).failed_run.run_number
    alarm_now = anchor + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC + 1.0
    assert story_live.apply_cycle(erp, alarm_now, anchor) == 1
    erp.commit()
    story_run = story_live.story_work_order(alarm_now, anchor).run_number

    live = sl.lookup("live", now=alarm_now)
    assert live["warnings"] == [], live["warnings"]
    assert live["failed_batch"]["run_number"] == history_run
    assert live["failed_batch"]["is_history_run"] is True
    assert live["live_defect_run"]["run_number"] == story_run
    assert live["live_defect_run"]["status"] == "RUNNING"
    assert live["live_defect_run"]["dimensional_fail_count"] > 0
    assert live["live_defect_run"]["is_history_run"] is False
    # the answer key does not widen: customers are still the history's shipments
    assert {c["customer_code"] for c in live["customers"]} == set(story_history.STORY_CUSTOMER_CODES)


def test_placed_replan_moves_the_answer_key_to_the_new_era(routed):
    from erp_simulation import condition_monitor as cm, story_history, story_live, story_replan

    erp, seed = routed["erp"][1], routed["seed"][1]
    anchor = story_live.read_anchor(erp)
    if anchor is None:          # run alone: write the history first
        anchor = float(int(time.time()))
        story_history.write(erp, seed, story_history.plan(anchor))
    now = anchor + cm.ALARM_OFFSET_SECONDS + 600     # past the old era's first alarm
    report = story_replan.replan(erp, seed, now - 3600, now=now, source="placed",
                                 expected_anchor=anchor)
    new_anchor = report["new_anchor"]
    assert new_anchor != anchor and report["source"] == "placed"

    live = sl.lookup("live", now=now + 60)
    assert live["warnings"] == [], live["warnings"]
    assert live["clock"]["anchor_epoch"] == new_anchor
    assert live["clock"]["anchor_source"] == "placed"
    assert live["clock"]["phase"] == "degrading"
    assert live["clock"]["telemetry"] == {
        "anchor": new_anchor, "cursor_epoch": report["telemetry"]["cursor_epoch"],
        "backfill_complete": False}
    assert live["failed_batch"]["run_number"] == report["failed_run_number"]
    assert live["failed_batch"]["is_history_run"] is True
    assert {c["customer_code"] for c in live["customers"]} == set(story_history.STORY_CUSTOMER_CODES)
    assert live["total_value"] == 10200
    assert live["live_defect_run"] is None      # the old era's story run was retired


def test_superseded_order_is_split_out_and_the_clock_carries_the_timeline(routed):
    """An open MES-bridge order on the story asset inside the live window,
    superseded by the history writer's OWN statement (-> CAN), is reported
    under ``superseded_work_orders``, never in ``work_orders``; the live clock
    carries ``phase_timeline`` agreeing with its phase."""
    from erp_simulation import story_history, story_live

    erp, seed = routed["erp"][1], routed["seed"][1]
    anchor = story_live.read_anchor(erp)
    if anchor is None:          # run alone: write the history first
        anchor = float(int(time.time()))
        story_history.write(erp, seed, story_history.plan(anchor))
    wonum = "IND-" + uuid.uuid4().hex[:8].upper()
    with seed.cursor() as cur:
        cur.execute(
            f"INSERT INTO {sl.schema('cmms')}.work_orders (wonum, asset_num, work_type, "
            "description, status, priority, reported_by, reported_date, failure_code) "
            "VALUES (%s, %s, 'EM', 'MES-triggered: bearing', 'WAPPR', 1, 'MES bridge', "
            "%s, %s)",  # nosec B608 - validated schema of a fixed role
            (wonum, sl.STORY_CMMS_ASSET_NUM, sl._naive_utc(anchor - 3600),
             sl.BEARING_FAILURE_CODE))
        assert story_history._supersede_frozen_orders(cur, story_history.plan(anchor)) >= 1
    seed.commit()

    live = sl.lookup("live", now=anchor + 60)
    maint = live["maintenance"]
    assert wonum not in {w["wonum"] for w in maint["work_orders"]}
    (superseded,) = [w for w in maint["superseded_work_orders"] if w["wonum"] == wonum]
    assert superseded["status"] == sl.CANCELLED_WO_STATUS
    assert superseded["description"].endswith(story_history.SUPERSEDED_DESCRIPTION_SUFFIX)
    assert all(w["status"] != sl.CANCELLED_WO_STATUS for w in maint["work_orders"])
    timeline = live["clock"]["phase_timeline"]
    assert timeline["current"] == live["clock"]["phase"]
    assert timeline["seconds_to_next_phase"] >= 0
