# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live story's machine runs only story work (ADR-0085 amendment 2026-11-04).

While the live story is on (``STORY_LINE_RESERVED=1`` on the simulation
Lambda, or this cycle's story anchor stored), the MES simulator gives the
story line at the condition-monitor site no work. Its only runs are then the
history's and ``story_live``'s alarm run, so no simulator run can sit inside a
history run a placement re-times, and the alarm run always finds the line
idle. Off the live story nothing changes.

Offline: ``get_next_available_line`` on a recording cursor. PostgreSQL
(skipped unless ``ERP_SIM_TEST_PG_DSN`` is set): the 2026-11-04 deploy (three
RELEASED runs at the story site in one cycle, the third one landing on LINE3),
then the history written onto an anchor whose last line-3 run covers that
cycle, as a placement re-times it.

    python -m pytest erp_simulation/tests/test_story_line_reserved.py -q
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import mes_simulator as mes_mod
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import story_history as sh
from erp_simulation import story_live
from erp_simulation import utils
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.tests.test_cycle_isolation import _orchestrator as _fake_orchestrator
from erp_simulation.tests.test_cycle_isolation import _simulator_calls as _step_calls
from stacks import _workshop_story

_SITE = MESSimulator.CONDITION_MONITOR_SITE
_LINE = cm.STORY_MES_LINE
_ENV = MESSimulator.STORY_LINE_ENV
_OTHER_SITE = "SOME_OTHER_SITE"
_ANCHOR = 1_900_000_123.0


class _BusyCursor:
    """Answers get_next_available_line's busy-line query with ``busy``."""

    def __init__(self, busy):
        self._busy = busy
        self.params = None

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return [(line,) for line in self._busy]


@pytest.fixture
def sim(monkeypatch):
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    monkeypatch.delenv(_ENV, raising=False)
    return MESSimulator()


def _next(sim, site=_SITE, busy=("LINE1", "LINE2")):
    return sim.get_next_available_line(_BusyCursor(busy), site)


# --------------------------------------------------------------------------
# Off the live story: exactly as before
# --------------------------------------------------------------------------

def test_off_the_live_story_the_story_line_takes_work_as_before(sim):
    assert sim.condition_anchor_epoch == 0.0
    assert not sim.story_line_reserved()
    assert sim.reserved_lines(_SITE) == frozenset()
    assert _next(sim) == _LINE
    assert _next(sim, busy=()) == "LINE1"
    assert _next(sim, busy=("LINE1", "LINE2", "LINE3")) is None


def test_an_unknown_clock_alone_reserves_nothing(sim):
    """The anchor read failed this cycle (None): no evidence of a live story,
    so a demo or frozen plant keeps its third line."""
    sim.condition_anchor_epoch = None
    assert not sim.story_line_reserved()
    assert _next(sim) == _LINE


@pytest.mark.parametrize("raw", ["", "0", "true", "yes"])
def test_only_the_packaged_value_1_reserves_the_line(sim, monkeypatch, raw):
    monkeypatch.setenv(_ENV, raw)
    assert not sim.story_line_reserved()
    assert _next(sim) == _LINE


# --------------------------------------------------------------------------
# On the live story: the story line is never the next free line
# --------------------------------------------------------------------------

def _live_by_flag(sim, monkeypatch):
    monkeypatch.setenv(_ENV, " 1 ")


def _live_by_anchor(sim, monkeypatch):
    sim.condition_anchor_epoch = _ANCHOR      # what _load_story_clock sets on a stored anchor


@pytest.mark.parametrize("live", [_live_by_flag, _live_by_anchor], ids=["flag", "stored-anchor"])
def test_the_live_story_keeps_the_story_line_for_its_own_runs(sim, monkeypatch, live):
    live(sim, monkeypatch)
    assert sim.story_line_reserved()
    assert sim.reserved_lines(_SITE) == frozenset({_LINE})
    assert _next(sim) is None                           # LINE1 and LINE2 busy: the run waits
    assert _next(sim, busy=("LINE1",)) == "LINE2"
    assert _next(sim, busy=()) == "LINE1"


@pytest.mark.parametrize("live", [_live_by_flag, _live_by_anchor], ids=["flag", "stored-anchor"])
def test_every_other_site_keeps_its_third_line(sim, monkeypatch, live):
    live(sim, monkeypatch)
    assert sim.reserved_lines(_OTHER_SITE) == frozenset()
    assert _next(sim, site=_OTHER_SITE) == "LINE3"


def test_the_flag_and_line_match_the_stack_and_the_story():
    """The env name is the one the live synth sets; the reserved line is the
    line the history's failed batch and the story run use."""
    assert MESSimulator.STORY_LINE_ENV == _workshop_story.STORY_LINE_ENV_NAME
    assert sh.plan(_ANCHOR).failed_run.line == _LINE
    params = story_live._start_params(story_live.story_work_order(_ANCHOR, _ANCHOR))
    assert (params["site"], params["line"]) == (_SITE, _LINE)


def test_no_history_line3_run_overlaps_another_and_every_alarm_run_follows_the_history():
    """With only story runs on the line, the line is free of overlaps: the
    plan's own line-3 runs never overlap one another, and every story run
    (one per loop, starting at or after its alarm) starts after the history
    ends, also after a two-clock move (its alarm lies after the history)."""
    sp = sh.plan(_ANCHOR)
    line3 = sorted((r for r in (*sp.runs, sp.failed_run) if r.line == _LINE), key=lambda r: r.start)
    assert all(a.end <= b.start for a, b in zip(line3, line3[1:]))
    history_end = max(r.end for r in line3)
    for anchor in (_ANCHOR, _ANCHOR + 3 * cm.TOTAL_SECONDS):
        for loop in range(3):
            alarm = anchor + loop * cm.TOTAL_SECONDS + cm.ALARM_OFFSET_SECONDS
            run = story_live.story_work_order(alarm, anchor)
            assert sh.ts(run.start.timestamp()) >= history_end


# --------------------------------------------------------------------------
# Real PostgreSQL: the 2026-11-04 deploy, then a placement
# --------------------------------------------------------------------------

@pytest.fixture
def utc_host():
    """The Lambda's zone (UTC): the MES's naive local stamps and the
    history's naive UTC stamps are the same literal. Listed BEFORE the
    database fixture so its session zone follows."""
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
def pg(utc_host, monkeypatch):
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    monkeypatch.delenv(_ENV, raising=False)
    utils._reset_sequence_cache()
    with _fresh_erp_db() as conn:
        with conn.cursor() as cur:
            utils.ensure_number_sequences(cur)
        conn.commit()
        yield conn
    utils._reset_sequence_cache()


def _released_runs(conn, count: int = 3) -> list:
    """``count`` RELEASED runs at the story site, one per machining part, as a
    deploy's first cycles leave them; returns their run numbers."""
    numbers = []
    with conn.cursor() as cur:
        for k in range(count):
            sku = sh.PRODUCTS[k % len(sh.PRODUCTS)][0]
            cur.execute("SELECT product_id FROM products WHERE sku = %s", (sku,))
            product_id = cur.fetchone()[0]
            number = f"RUN-T-{uuid.uuid4().hex[:8]}"
            cur.execute("""
                INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site)
                VALUES (%s, %s, 10, 'RELEASED', %s)""", (number, product_id, _SITE))
            numbers.append(number)
    conn.commit()
    return numbers


def _mes_cycle(conn, sim, cycle_id: str) -> None:
    """The cycle's MES steps through ``mes_events``, in the orchestrator's own
    order (the reservation step, create work orders, start and complete
    them), each committed alone as the orchestrator does."""
    steps = orch_mod._SIMULATION_STEPS
    for step in steps[:[s.name for s in steps].index("mes_events") + 1]:
        if step.owner == "mes_simulator":
            getattr(sim, step.method)(conn, cycle_id)
            conn.commit()


_deploy_cycle = _mes_cycle


def _work_order_lines(conn, run_numbers) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pr.run_number, w.line FROM production_runs pr
            LEFT JOIN mes_work_orders w ON w.production_run_id = pr.run_id
            WHERE pr.run_number = ANY(%s)""", (list(run_numbers),))
        return dict(cur.fetchall())


# A non-plan story-line run that started inside a history story-line run.
_OVERLAPS_SQL = """
    SELECT live.work_order_number, hist.work_order_number
    FROM mes_work_orders live
    JOIN mes_work_orders hist
      ON hist.site = live.site AND hist.line = live.line
     AND live.actual_start_time >= hist.actual_start_time
     AND live.actual_start_time < hist.actual_end_time
    WHERE live.site = %s AND live.line = %s
      AND hist.work_order_number = ANY(%s) AND NOT (live.work_order_number = ANY(%s))
"""


def _place_over_now(conn) -> sh.StoryPlan:
    """Write the history onto an anchor whose last line-3 run (``-91005``)
    covers the deploy cycle, as the first placement re-times it."""
    anchor = float(cm.snap_anchor(time.time() + 4 * 3600))
    sp = sh.plan(anchor)
    last = max((r for r in sp.runs if r.line == _LINE), key=lambda r: r.start)
    assert last.run_number.endswith("-91005") and last.start <= datetime.now() < last.end
    with conn.cursor() as cur:
        sh._write_erp(cur, sp)
    conn.commit()
    return sp


def _overlaps(conn, sp: sh.StoryPlan) -> list:
    plan_wos = [r.work_order_number for r in (*sp.runs, sp.failed_run)]
    with conn.cursor() as cur:
        cur.execute(_OVERLAPS_SQL, (_SITE, _LINE, plan_wos, plan_wos))
        return cur.fetchall()


def test_pg_without_the_reservation_the_deploy_run_sits_inside_the_placed_history(pg):
    """Today's simulator on a live deploy (no flag, no anchor yet): the third
    RELEASED run takes LINE3, and a placement over that time puts it inside
    history run -91005 on the story machine. The 2026-11-04 defect."""
    sim = MESSimulator()
    runs = _released_runs(pg)
    _deploy_cycle(pg, sim, "deploy-1")
    assert sorted(_work_order_lines(pg, runs).values()) == ["LINE1", "LINE2", _LINE]
    sp = _place_over_now(pg)
    (pair,) = _overlaps(pg, sp)
    assert pair[1].endswith("-91005")


def test_pg_with_the_flag_no_deploy_run_reaches_the_story_line_and_nothing_overlaps(pg, monkeypatch):
    """A live synth's simulator (flag set, before the anchor exists): LINE1
    and LINE2 take two runs, the third waits RELEASED; after the placement no
    run on the story machine overlaps the history, and the runs the machine
    made after the failed batch are exactly the plan's (-91002, -91005)."""
    monkeypatch.setenv(_ENV, "1")
    sim = MESSimulator()
    runs = _released_runs(pg)
    _deploy_cycle(pg, sim, "deploy-1")
    lines = _work_order_lines(pg, runs)
    assert sorted(v for v in lines.values() if v) == ["LINE1", "LINE2"]
    (waiting,) = [run for run, line in lines.items() if line is None]
    _deploy_cycle(pg, sim, "deploy-2")                  # both lines still busy: it keeps waiting
    assert _work_order_lines(pg, [waiting]) == {waiting: None}
    sp = _place_over_now(pg)
    assert _overlaps(pg, sp) == []
    with pg.cursor() as cur:
        cur.execute("""
            SELECT pr.run_number FROM production_runs pr
            JOIN mes_work_orders w ON w.production_run_id = pr.run_id
            WHERE w.site = %s AND w.line = %s AND w.actual_start_time > %s
            ORDER BY w.actual_start_time""", (_SITE, _LINE, sp.failed_run.end))
        after = [r[0] for r in cur.fetchall()]
    assert [run.rsplit("-", 1)[1] for run in after] == ["91002", "91005"]


def test_pg_a_stored_anchor_keeps_the_line_without_the_flag(pg):
    """The row-keyed path (a live story without the packaged flag): once the
    orchestrator loaded a stored anchor, LINE3 takes no simulator work."""
    sim = MESSimulator()
    sim.condition_anchor_epoch = _ANCHOR
    runs = _released_runs(pg)
    _deploy_cycle(pg, sim, "placed-1")
    assert sorted(v for v in _work_order_lines(pg, runs).values() if v) == ["LINE1", "LINE2"]


def test_pg_the_alarm_run_starts_on_the_kept_line_while_simulator_work_waits(pg, monkeypatch):
    """Flag set, simulator work waiting at the site: the cycle before the
    alarm leaves LINE3 idle, the first alarm cycle starts the story run there
    (the orchestrator's story step runs before the MES steps), and the MES
    steps after it still put nothing else on the line."""
    monkeypatch.setenv(_ENV, "1")
    sim = MESSimulator()
    anchor = time.time() - cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC - 1.0
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO simulation_config (config_key, config_value, data_type, category, default_value)
            VALUES (%s, %s, 'float', 'story', '0')""", (story_live.ANCHOR_KEY, repr(float(anchor))))
    pg.commit()
    runs = _released_runs(pg)
    _deploy_cycle(pg, sim, "before-alarm")
    assert _LINE not in _work_order_lines(pg, runs).values()
    assert story_live.apply_cycle(pg, time.time(), anchor) == 1
    pg.commit()
    _deploy_cycle(pg, sim, "alarm-1")
    story = story_live.story_work_order(time.time(), anchor)
    with pg.cursor() as cur:
        cur.execute("SELECT work_order_number, status FROM mes_work_orders WHERE site = %s AND line = %s",
                    (_SITE, _LINE))
        assert cur.fetchall() == [(story.work_order_number, "RUNNING")]


# --------------------------------------------------------------------------
# An account updated in place: SCHEDULED orders an older build put on LINE3
# --------------------------------------------------------------------------

def _scheduled_by_an_older_build(conn) -> dict:
    """Three RELEASED runs scheduled by a simulator without the reservation
    (LINE1, LINE2, LINE3, all SCHEDULED, with their lot tracking); returns
    ``{line: work_order_id}``."""
    _released_runs(conn)
    MESSimulator().create_mes_work_orders(conn, "older-build")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT line, work_order_id FROM mes_work_orders WHERE site = %s AND status = 'SCHEDULED'",
                    (_SITE,))
        by_line = dict(cur.fetchall())
    assert sorted(by_line) == ["LINE1", "LINE2", _LINE]
    return by_line


def _order(conn, work_order_id) -> tuple:
    """(status, line, target rate, its run's line and status, its lot tracking lines)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT w.status, w.line, w.target_rate_per_minute, pr.line, pr.status
            FROM mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
            WHERE w.work_order_id = %s""", (work_order_id,))
        row = cur.fetchone()
        cur.execute("SELECT DISTINCT line FROM lot_mes_tracking WHERE work_order_id = %s", (work_order_id,))
        return (*row, sorted(r[0] for r in cur.fetchall()))


def _running_on_the_story_line(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT work_order_id FROM mes_work_orders WHERE site = %s AND line = %s "
                    "AND status = 'RUNNING'", (_SITE, _LINE))
        return [r[0] for r in cur.fetchall()]


_events = _mes_cycle


def _waits_past_now(conn, work_order_id) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT scheduled_start_time > LOCALTIMESTAMP FROM mes_work_orders WHERE work_order_id = %s",
                    (work_order_id,))
        return cur.fetchone()[0]


def test_the_reservation_step_runs_ahead_of_every_mes_step_that_assigns_or_starts_work():
    """Its own optional step, ahead of mes_work_orders (an older build's order
    takes a freed line before new work) and mes_events (nothing due on the
    kept line when orders start); past the time budget a skipped step skips
    every later optional step too, so it is never skipped while mes_events runs."""
    steps = {s.name: s for s in orch_mod._SIMULATION_STEPS}
    names = list(steps)
    step = steps["mes_reserved_lines"]
    assert (step.owner, step.method, step.copies, step.required) == (
        "mes_simulator", "clear_reserved_lines", (), False)
    assert names.index("mes_reserved_lines") + 1 == names.index("mes_work_orders") < names.index("mes_events")
    assert not steps["mes_work_orders"].required and not steps["mes_events"].required


def test_off_the_live_story_the_reservation_step_runs_no_sql(sim):
    class _NoSql:
        def cursor(self):
            raise AssertionError("no SQL off the live story")

    assert sim.clear_reserved_lines(_NoSql(), "c-1") == {
        "reserved_line_orders_moved": 0, "reserved_line_orders_waiting": 0}


def test_pg_a_stored_scheduled_story_line_order_moves_to_a_free_line_with_its_lot_tracking(pg, monkeypatch):
    """The upgraded build starts an older build's SCHEDULED LINE3 order on a
    free line: the work order, its run and its lot tracking all name the new
    line, with that line's target rate; nothing runs on LINE3."""
    by_line = _scheduled_by_an_older_build(pg)
    with pg.cursor() as cur:
        cur.execute("UPDATE mes_work_orders SET status = 'CANCELLED' WHERE work_order_id = ANY(%s::uuid[])",
                    ([str(by_line["LINE1"]), str(by_line["LINE2"])],))
    pg.commit()
    status, line, rate, _run_line, run_status, tracked = _order(pg, by_line[_LINE])
    assert (status, line, rate, run_status) == ("SCHEDULED", _LINE, 100, "RELEASED") and tracked == [_LINE]
    monkeypatch.setenv(_ENV, "1")
    _events(pg, MESSimulator(), "upgraded-1")
    assert _order(pg, by_line[_LINE]) == ("RUNNING", "LINE1", 120, "LINE1", "IN_PROGRESS", ["LINE1"])
    assert _running_on_the_story_line(pg) == []


def test_pg_a_stored_scheduled_story_line_order_waits_until_another_line_frees(pg, monkeypatch):
    """With LINE1 and LINE2 busy it stays SCHEDULED on LINE3 (its run still
    RELEASED) and is not due, so even the event step alone does not start it;
    it waits through the cycle a line completes and starts on that line in
    the next cycle."""
    by_line = _scheduled_by_an_older_build(pg)
    monkeypatch.setenv(_ENV, "1")
    sim = MESSimulator()
    _events(pg, sim, "upgraded-1")                     # LINE1 and LINE2 start; LINE3's order waits
    assert _order(pg, by_line[_LINE])[:2] == ("SCHEDULED", _LINE)
    assert _order(pg, by_line["LINE1"])[0] == "RUNNING"
    assert _waits_past_now(pg, by_line[_LINE])
    sim.simulate_production_events(pg, "events-alone")
    pg.commit()
    assert _order(pg, by_line[_LINE])[:2] == ("SCHEDULED", _LINE)
    with pg.cursor() as cur:
        cur.execute("""UPDATE production_runs SET planned_end_time = NOW() - INTERVAL '1 minute'
                       WHERE run_id = (SELECT production_run_id FROM mes_work_orders WHERE work_order_id = %s)""",
                    (by_line["LINE1"],))
    pg.commit()
    _events(pg, sim, "upgraded-2")                     # LINE1 completes after the start step
    assert _order(pg, by_line["LINE1"])[0] == "COMPLETED"
    assert _order(pg, by_line[_LINE])[:2] == ("SCHEDULED", _LINE)
    _events(pg, sim, "upgraded-3")
    assert _order(pg, by_line[_LINE])[:2] == ("RUNNING", "LINE1")
    assert _running_on_the_story_line(pg) == []


def test_pg_the_waiting_order_takes_a_freed_line_ahead_of_new_work(pg, monkeypatch):
    """Full-cycle order: a new RELEASED run waiting at the site does not take
    the line that frees; the older build's order does, and the new run keeps
    waiting for the next free line."""
    by_line = _scheduled_by_an_older_build(pg)
    monkeypatch.setenv(_ENV, "1")
    sim = MESSimulator()
    _mes_cycle(pg, sim, "upgraded-1")                  # LINE1 and LINE2 start; LINE3's order waits
    (newer,) = _released_runs(pg, count=1)
    with pg.cursor() as cur:
        cur.execute("""UPDATE production_runs SET planned_end_time = NOW() - INTERVAL '1 minute'
                       WHERE run_id = (SELECT production_run_id FROM mes_work_orders WHERE work_order_id = %s)""",
                    (by_line["LINE1"],))
    pg.commit()
    _mes_cycle(pg, sim, "upgraded-2")                  # no free line for either; LINE1 completes
    assert _work_order_lines(pg, [newer]) == {newer: None}
    _mes_cycle(pg, sim, "upgraded-3")
    assert _order(pg, by_line[_LINE])[:2] == ("RUNNING", "LINE1")
    assert _work_order_lines(pg, [newer]) == {newer: None}
    assert _running_on_the_story_line(pg) == []


def test_pg_off_the_live_story_a_stored_scheduled_line3_order_starts_on_line3(pg):
    by_line = _scheduled_by_an_older_build(pg)
    _events(pg, MESSimulator(), "frozen-1")
    assert _order(pg, by_line[_LINE])[:2] == ("RUNNING", _LINE)


def test_pg_another_sites_scheduled_line3_order_starts_on_line3_on_the_live_story(pg, monkeypatch):
    with pg.cursor() as cur:
        cur.execute("SELECT site_code FROM sites WHERE is_active AND site_code <> %s ORDER BY site_code LIMIT 1",
                    (_SITE,))
        other = cur.fetchone()[0]
        cur.execute("SELECT product_id FROM products ORDER BY sku LIMIT 1")
        product_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site)
                       VALUES (%s, %s, 10, 'RELEASED', %s) RETURNING run_id""",
                    (f"RUN-T-{uuid.uuid4().hex[:8]}", product_id, other))
        run_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                           planned_quantity, status, scheduled_start_time, target_rate_per_minute)
                       VALUES (%s, %s, %s, %s, 'LINE3', 10, 'SCHEDULED', NOW() - INTERVAL '1 minute', 100)
                       RETURNING work_order_id""", (f"WO-T-{uuid.uuid4().hex[:8]}", run_id, product_id, other))
        wo_id = cur.fetchone()[0]
    pg.commit()
    monkeypatch.setenv(_ENV, "1")
    _events(pg, MESSimulator(), "live-1")
    assert _order(pg, wo_id)[:2] == ("RUNNING", "LINE3")


# --------------------------------------------------------------------------
# The reservation step fails closed: no MES work order is assigned or
# started in a cycle whose reservation step failed
# --------------------------------------------------------------------------

_BLOCKED = ("mes_simulator.create_mes_work_orders", "mes_simulator.simulate_production_events")


def _every_step_call() -> list:
    """Every simulator step's call, in cycle order, as the fake orchestrator logs it."""
    return [f"{s.owner}.{s.method}" if s.owner != "self" else "self.suppliers"
            for s in orch_mod._SIMULATION_STEPS]


def test_only_the_reservation_step_blocks_later_steps_and_only_mes_steps_after_it():
    steps = orch_mod._SIMULATION_STEPS
    names = [s.name for s in steps]
    blocking = {s.name: s.blocks for s in steps if s.blocks}
    assert blocking == {"mes_reserved_lines": ("mes_work_orders", "mes_events")}
    assert all(names.index(b) > names.index("mes_reserved_lines") for b in blocking["mes_reserved_lines"])


def test_a_failed_reservation_step_skips_assigning_and_starting_work_this_cycle_only():
    """The step fails (rolled back, counted): this cycle runs neither
    mes_work_orders nor mes_events, every other step runs as before, and the
    next cycle, with the step succeeding, runs every step again."""
    behaviour = {"clear_reserved_lines": MagicMock(side_effect=RuntimeError("reservation step failed"))}
    orch, log, _conn = _fake_orchestrator(behaviour)
    orch._run_cycle()
    calls = _step_calls(log)
    assert [c for c in _every_step_call() if c not in _BLOCKED] == calls
    assert log[log.index("mes_simulator.clear_reserved_lines") + 1] == "rollback"
    assert orch._cycle_metrics["errors"] == 1
    assert orch._cycle_skipped == ["mes_work_orders", "mes_events"]
    del behaviour["clear_reserved_lines"]                # the next cycle's step succeeds
    log.clear()
    orch._run_cycle()
    assert _step_calls(log) == _every_step_call()
    assert orch._cycle_metrics["errors"] == 0 and orch._cycle_skipped == []


@pytest.mark.parametrize("method", ["create_mes_work_orders", "simulate_production_events",
                                    "simulate_quality_checks"])
def test_any_other_steps_failure_is_handled_as_before(method):
    orch, log, _conn = _fake_orchestrator({method: MagicMock(side_effect=RuntimeError("boom"))})
    orch._run_cycle()
    assert _step_calls(log) == _every_step_call()
    assert orch._cycle_metrics["errors"] == 1 and orch._cycle_skipped == []


class _Beat:
    def beat(self):
        return None


def _real_mes_cycle(conn, sim) -> list:
    """The orchestrator's REAL step loop (``_run_simulation_steps``: one
    transaction per step, failures rolled back and counted) over its MES steps
    through mes_events, on ``conn``; returns the steps it skipped."""
    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.mes_simulator, orch.cycle_count, orch._current_cycle_id = sim, 0, f"c-{uuid.uuid4().hex[:6]}"
    orch._cycle_metrics, orch._cycle_timings, orch._cycle_skipped = {"errors": 0}, {}, []
    orch._cycle_started = orch_mod._monotonic()
    for name in ("_ensure_number_sequences", "_ensure_hot_path_indexes", "_check_backlog"):
        setattr(orch, name, lambda _conn: None)
    names = [s.name for s in orch_mod._SIMULATION_STEPS]
    mes = orch_mod._SIMULATION_STEPS[:names.index("mes_events") + 1]
    orch._simulation_steps = lambda: tuple(s for s in mes if s.owner == "mes_simulator")
    orch._run_simulation_steps(conn, _Beat())
    return orch._cycle_skipped


def test_pg_a_failed_reservation_step_starts_nothing_on_the_story_line(pg, monkeypatch):
    """An older build's due SCHEDULED LINE3 order and a free LINE1: the
    reservation step fails after writing part of the move (rolled back), so
    that cycle starts nothing at all and the order stays SCHEDULED on LINE3.
    The next cycle's step succeeds: the order moves to LINE1 and starts there,
    LINE2's order starts, and nothing runs on LINE3."""
    by_line = _scheduled_by_an_older_build(pg)
    with pg.cursor() as cur:
        cur.execute("UPDATE mes_work_orders SET status = 'CANCELLED' WHERE work_order_id = %s",
                    (by_line["LINE1"],))
    pg.commit()
    monkeypatch.setenv(_ENV, "1")
    sim = MESSimulator()
    move = MESSimulator._move_off_reserved_line

    def move_then_fail(self, cursor, *args):
        move(self, cursor, *args)
        raise RuntimeError("the reservation step failed after a partial move")

    monkeypatch.setattr(MESSimulator, "_move_off_reserved_line", move_then_fail)
    skipped = _real_mes_cycle(pg, sim)
    assert _running_on_the_story_line(pg) == []
    assert _order(pg, by_line[_LINE]) == ("SCHEDULED", _LINE, 100, None, "RELEASED", [_LINE])
    assert _order(pg, by_line["LINE2"])[0] == "SCHEDULED"
    assert skipped == ["mes_work_orders", "mes_events"]
    monkeypatch.setattr(MESSimulator, "_move_off_reserved_line", move)
    assert _real_mes_cycle(pg, sim) == []
    assert _order(pg, by_line[_LINE]) == ("RUNNING", "LINE1", 120, "LINE1", "IN_PROGRESS", ["LINE1"])
    assert _order(pg, by_line["LINE2"])[0] == "RUNNING"
    assert _running_on_the_story_line(pg) == []
