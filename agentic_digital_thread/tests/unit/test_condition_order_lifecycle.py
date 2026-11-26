# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The condition monitor's work orders, and the MES downtime bridge's, follow a CMMS lifecycle.

``MESSimulator.monitor_asset_condition`` raises one ``VIB-CMX-<loop>`` order
per story loop at the BPFO alarm. The order stays open through its own loop
(the alarm, the bearing failure, the repair stop and the healthy tail: the
four phases the workshop's facilitator table describes, where it is "still
on file, waiting for approval"), and once that loop has ended the next cycle
completes it: status ``COMP``, worked in the bearing replacement its own loop
played (``actual_start`` = its scheduled bearing failure, ``actual_finish``
= the end of the repair stop) and one ``COMPLETE`` log entry. So a story
account that runs for days carries at most ONE open condition order, and an
account that already piled them up is brought back on its next cycle.

The MES downtime bridge's ``IND-*`` orders follow the same rule in the same
transaction: open through the loop they were raised in, completed once it
has ended, worked in the downtime they were raised from (read from the ERP),
or at the moment they were entered when that downtime no longer exists,
keeping the stop start they record (none when they record none).

Offline: the seed database is a small in-memory model of the site CMMS's
``work_orders`` / ``work_order_log`` that applies the statements the monitor
issues (the insert and the close-outs) by their bound parameters; the ERP's
downtime events are a dict behind the monitor's reader. The real SQL runs
against PostgreSQL in ``tests/integration/test_condition_orders_pg.py``.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import mes_simulator as mes_mod  # noqa: E402
from erp_simulation import story_history as sh  # noqa: E402
from erp_simulation.mes_simulator import MESSimulator  # noqa: E402
from erp_simulation.utils import MAX_ROWS_PER_CYCLE as BOUND  # noqa: E402

#: A congruent story anchor (2026-10-03, on the telemetry grid).
ANCHOR = float(cm.snap_anchor(1791000000, "down"))
LOOP = cm.TOTAL_SECONDS
REPAIR = timedelta(seconds=cm.BEARING_REPAIR_STEPS * cm.INTERVAL_SEC)
PREFIX = MESSimulator._loop_wonum(0).rstrip("0")
OPEN = ("WAPPR", "APPR", "INPRG", "WMATL")


def _bridge_insert(downtime_id: str, asset: str, category: str, reason: str) -> tuple:
    """What the MES downtime bridge writes for ``downtime_id`` through its own
    entry point (asset pick answered with ``asset``):
    ``(wonum, asset, work_type, description, priority, failure_code, reported_by)``."""
    from . import _site_cmms_rows as rows
    row = rows.bridge_order([{"asset_num": asset, "asset_type": "CNC_MILL"}], category, reason, downtime_id)
    return (row["wonum"], row["asset_num"], row["work_type"], row["description"], row["priority"],
            row["failure_code"], row["reported_by"])


BRIDGE_PREFIX = _bridge_insert("0822d3e2-0000-4000-8000-000000000000", cm.STORY_CMMS_ASSET_NUM,
                               "MECHANICAL", "VIB-BEARING")[0][:4]


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _at(loop: int, step: int, anchor: float = ANCHOR) -> float:
    """An instant inside story loop ``loop`` at ``step`` (a second past the step)."""
    return anchor + loop * LOOP + step * cm.INTERVAL_SEC + 1.0


# Phase -> a step inside it.
PHASE_STEP = {
    "degrading": cm.BPFO_ALARM_STEP // 2,
    "bpfo_alarm": cm.BPFO_ALARM_STEP + 10,
    "repair": cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS // 2,
    "healthy": (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS + cm.NUM_STEPS) // 2,
}


class SeedCmms:
    """The site CMMS work orders and log, as the monitor's statements leave them."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.log: list[dict] = []
        self.statements: list[str] = []
        self.tx: list[str] = []
        self.after_candidates: list = []

    def add(self, wonum, *, status="WAPPR", reported=None, sched=None, asset=cm.STORY_CMMS_ASSET_NUM,
            work_type="CM", failure_code="BEARING", description="", reported_by="Condition monitor"):
        self.orders[wonum] = {"wonum": wonum, "asset_num": asset, "work_type": work_type,
                              "status": status, "reported_date": reported, "sched_start": sched,
                              "actual_start": None, "actual_finish": None,
                              "failure_code": failure_code, "description": description,
                              "reported_by": reported_by}

    def add_bridge(self, downtime_id: str, *, reported: datetime, status="WAPPR",
                   asset=cm.STORY_CMMS_ASSET_NUM, reason="VIB-BEARING", category="MECHANICAL",
                   start: datetime | None = None) -> str:
        """One MES-bridge order exactly as the bridge's own writer builds it
        (wonum, work type, description, failure code, and ``start``, the stop
        start it was handed, as its actual start), reported at ``reported``
        (the writer stamps the database's NOW())."""
        wonum, _asset, work_type, description, _priority, failure_code, reported_by = _bridge_insert(
            downtime_id, asset, category, reason)
        self.add(wonum, status=status, reported=reported, sched=reported, asset=asset,
                 work_type=work_type, failure_code=failure_code, description=description,
                 reported_by=reported_by)
        self.orders[wonum]["actual_start"] = start
        return wonum

    def open_condition(self) -> set[str]:
        return {w for w, o in self.orders.items()
                if w.startswith(PREFIX) and o["status"] in OPEN}

    def open_bridge(self) -> set[str]:
        return {w for w, o in self.orders.items()
                if w.startswith(BRIDGE_PREFIX) and o["status"] in OPEN}

    def connection(self) -> SimpleNamespace:
        cmms = self

        class _Cursor:
            rowcount = 0
            _rows: list = []

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                cmms.statements.append(text)
                if text.startswith("SELECT pg_advisory_xact_lock_shared"):
                    return
                if text.startswith("INSERT INTO") and "ON CONFLICT (wonum) DO NOTHING" in text:
                    wonum, asset, _desc, reported, _target, sched, _crew = params
                    if wonum in cmms.orders:
                        self.rowcount = 0
                    else:
                        cmms.add(wonum, reported=reported, sched=sched, asset=asset)
                        self.rowcount = 1
                    return
                if text.startswith("SELECT wonum, description, reported_date") and isinstance(params, dict):
                    columns = [c.strip() for c in text[len("SELECT"):].split(" FROM ", 1)[0].split(",")]
                    self._rows = cmms._bridge_candidates(params, columns, text)
                    for hook in cmms.after_candidates:
                        hook()
                    return
                if "UPDATE" in text and "work_orders" in text and isinstance(params, dict):
                    if "wonums" in params:
                        self.rowcount = cmms._complete_bridge(params)
                    else:
                        self.rowcount = cmms._close_out(params)
                    return
                raise AssertionError(f"unexpected statement: {text[:120]}")

            def fetchall(self):
                return list(self._rows)

            def close(self):
                pass

        return SimpleNamespace(cursor=lambda: _Cursor(), commit=lambda: cmms.tx.append("commit"),
                               rollback=lambda: cmms.tx.append("rollback"), closed=0)

    def _bridge_candidates(self, p: dict, columns: list, text: str) -> list:
        """The candidate rows, with the columns the statement selects: those at
        or after ``start`` (or, for the wrap-round read, before it), in wonum
        order, at most ``limit``."""
        before = "wonum < %(start)s" in text
        rows = sorted(tuple(o[c] for c in columns) for o in self.orders.values()
                      if o["wonum"].startswith(p["prefix"].rstrip("%")) and o["status"] in p["open_statuses"]
                      and o["reported_date"] is not None and o["reported_date"] < p["loop_start"]
                      and ((o["wonum"] < p["start"]) if before else (o["wonum"] >= p["start"])))
        return rows[:p["limit"]]

    def _complete_bridge(self, p: dict) -> int:
        done = 0
        descriptions = p.get("descriptions") or [None] * len(p["wonums"])
        for wonum, start, finish, author, description in zip(
                p["wonums"], p["starts"], p["finishes"], p["authors"], descriptions):
            order = self.orders.get(wonum)
            if (order is None or not wonum.startswith(p["prefix"].rstrip("%"))
                    or order["status"] not in p["open_statuses"]
                    or not order["reported_date"] < p["loop_start"]
                    or ("reported_by" in p and order["reported_by"] != p["reported_by"])
                    or (description is not None and order["description"] != description)):
                continue
            order.update(status=p["status"], actual_start=start, actual_finish=finish)
            self.log.append({"wonum": wonum, "log_date": max(finish, order["reported_date"]),
                             "author": (author or p["default_author"])[:60], "log_type": "COMPLETE",
                             "entry": p["entry"]})
            done += 1
        return done

    def _close_out(self, p: dict) -> int:
        done = 0
        for order in self.orders.values():
            if (order["asset_num"] == p["asset"] and order["wonum"].startswith(p["prefix"].rstrip("%"))
                    and order["status"] in p["open_statuses"]
                    and order["reported_date"] is not None and order["reported_date"] < p["loop_start"]):
                start = order["sched_start"] or order["reported_date"]
                order.update(status=p["status"], actual_start=start,
                             actual_finish=min(start + timedelta(seconds=p["repair_seconds"]), p["now"]))
                self.log.append({"wonum": order["wonum"], "log_date": order["actual_finish"],
                                 "author": p["author"], "log_type": "COMPLETE", "entry": p["entry"]})
                done += 1
        return done


@pytest.fixture()
def make_sim(monkeypatch):
    """A MESSimulator (config stubbed) wired to ``cmms`` on the story clock
    ``anchor``; ``downtimes`` (an :class:`ErpDowntimes`) is its ERP downtime reader."""
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    monkeypatch.setattr(mes_mod, "_unproven_bridge_warnings", {"loop_start": None}, raising=False)
    for var in ("SEED_DB_SECRET_NAME", "SEED_DB_HOST"):
        monkeypatch.delenv(var, raising=False)

    def _make(cmms: SeedCmms, anchor: float = ANCHOR, downtimes=None) -> MESSimulator:
        sim = MESSimulator()
        sim.condition_anchor_epoch = anchor
        sim.condition_anchor_reader = (lambda: anchor) if anchor else None
        if downtimes is not None:
            sim.condition_downtime_reader = downtimes.read
        conn = cmms.connection()
        sim._get_seed_db_conn = lambda: conn
        return sim
    return _make


class ErpDowntimes:
    """The ERP's MES downtime events as the monitor's reader sees them:
    downtime id -> ``(start_time, end_time, resolved_by)``."""

    def __init__(self):
        self.rows: dict[str, tuple] = {}
        self.calls: list[list[str]] = []
        self._issued = 0

    def new_id(self) -> str:
        self._issued += 1
        n = self._issued
        return f"{0x0822d3e2 + 7919 * n:08x}-1f2e-4a5b-8c6d-{n:012x}"

    def read(self, ids):
        self.calls.append(list(ids))
        return {i: self.rows[i] for i in ids if i in self.rows}


def _stale_orders(cmms: SeedCmms, loops: range, anchor: float = ANCHOR) -> None:
    """Loops' condition orders exactly as the monitor's writer inserts them, all still open."""
    cur = cmms.connection().cursor()
    for loop in loops:
        inside = _at(loop, cm.BPFO_ALARM_STEP, anchor)
        MESSimulator.insert_condition_work_order(
            cur, loop, cm.alarm_epoch(inside, anchor), cm.fail_epoch(inside, anchor))
    cmms.statements.clear()


def _assert_completed_in_its_own_repair(cmms: SeedCmms, loop: int, anchor: float = ANCHOR) -> None:
    order = cmms.orders[MESSimulator._loop_wonum(loop)]
    fail = _utc(anchor + loop * LOOP + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC)
    assert order["status"] == "COMP"
    assert order["sched_start"] == fail
    assert (order["actual_start"], order["actual_finish"]) == (fail, fail + REPAIR)
    entries = [e for e in cmms.log if e["wonum"] == order["wonum"]]
    assert len(entries) == 1 and entries[0]["log_type"] == "COMPLETE"
    assert entries[0]["log_date"] == fail + REPAIR


# ---------------------------------------------------------------------------
# A long-running story account
# ---------------------------------------------------------------------------

def test_thirteen_loops_leave_at_most_the_current_loops_order_open(make_sim):
    """A cycle every five minutes for thirteen loops: in every cycle at most
    one condition order is open and it is the current loop's; before a loop's
    alarm none is; every earlier loop's order is completed in its own repair."""
    cmms = SeedCmms()
    sim = make_sim(cmms)
    for k in range(13 * LOOP // 300):
        now = ANCHOR + 300 * k + 7.0
        sim.monitor_asset_condition(f"c{k}", now=now)
        t = cm.story_seconds(now, ANCHOR)
        loop, step = cm.loop_number(t), cm.loop_step(t)
        expected = set() if step < cm.BPFO_ALARM_STEP else {MESSimulator._loop_wonum(loop)}
        assert cmms.open_condition() == expected, (loop, cm.phase(step))
    assert len(cmms.orders) == 13
    for loop in range(12):
        _assert_completed_in_its_own_repair(cmms, loop)
    assert cmms.orders[MESSimulator._loop_wonum(12)]["status"] == "WAPPR"


@pytest.mark.parametrize("phase", list(PHASE_STEP))
def test_thirteen_stale_open_orders_are_brought_back_on_the_next_cycle(make_sim, phase):
    """The measured rehearsal state: thirteen open orders from loops 0..12.
    One cycle in loop 13, whatever its phase, leaves only what the rule
    allows open: none before loop 13's alarm, loop 13's own from the alarm on."""
    cmms = SeedCmms()
    _stale_orders(cmms, range(13))
    assert len(cmms.open_condition()) == 13
    if phase != "degrading":
        _stale_orders(cmms, range(13, 14))       # loop 13's own, raised at its alarm
    make_sim(cmms).monitor_asset_condition("first-cycle", now=_at(13, PHASE_STEP[phase]))
    expected = set() if phase == "degrading" else {MESSimulator._loop_wonum(13)}
    assert cmms.open_condition() == expected
    for loop in range(13):
        _assert_completed_in_its_own_repair(cmms, loop)


def test_the_close_out_is_idempotent_across_cycles_and_restarts(make_sim):
    cmms = SeedCmms()
    _stale_orders(cmms, range(5))
    now = _at(5, PHASE_STEP["degrading"])
    make_sim(cmms).monitor_asset_condition("c1", now=now)
    snapshot = ({w: dict(o) for w, o in cmms.orders.items()}, list(cmms.log))
    sim = make_sim(cmms)                       # the same cycle again ...
    sim.monitor_asset_condition("c1", now=now)
    make_sim(cmms).monitor_asset_condition("c2", now=now + 60)   # ... and after a restart
    assert ({w: dict(o) for w, o in cmms.orders.items()}, list(cmms.log)) == snapshot
    assert len(cmms.log) == 5


def test_the_close_out_touches_only_open_condition_orders_on_the_story_asset(make_sim):
    cmms = SeedCmms()
    old = _utc(ANCHOR + 3600)
    _stale_orders(cmms, range(2))
    cmms.add("IND-0822D3E2", reported=old, work_type="EM")              # MES-bridge order
    cmms.add(sh.DEFERRED_PM_WONUM, reported=old, work_type="PM", failure_code=None)
    cmms.add(sh.CONDITION_WONUM, status="COMP", reported=old)           # the history's own
    cmms.add(MESSimulator._loop_wonum(900), reported=old, asset="1001-HYP-402")
    cmms.add(MESSimulator._loop_wonum(901), status="CAN", reported=old)
    make_sim(cmms).monitor_asset_condition("c", now=_at(2, PHASE_STEP["degrading"]))
    assert {w for w, o in cmms.orders.items() if o["status"] == "COMP"} == {
        sh.CONDITION_WONUM, MESSimulator._loop_wonum(0), MESSimulator._loop_wonum(1)}
    for wonum in ("IND-0822D3E2", sh.DEFERRED_PM_WONUM, MESSimulator._loop_wonum(900)):
        assert cmms.orders[wonum]["status"] == "WAPPR", wonum
    assert cmms.orders[MESSimulator._loop_wonum(901)]["status"] == "CAN"
    assert {e["wonum"] for e in cmms.log} == {MESSimulator._loop_wonum(0), MESSimulator._loop_wonum(1)}


# ---------------------------------------------------------------------------
# A fresh workshop account: loop 0 is exactly what it was
# ---------------------------------------------------------------------------

def test_loop_zero_never_closes_anything_and_only_the_alarm_touches_the_cmms(make_sim):
    """Loop 0 of a story clock has no earlier loop (the history and every
    placement delete the condition orders before the anchor): a cycle in any
    loop-0 phase issues no close-out, outside the alarm no statement at all,
    and VIB-CMX-000000 stays open through the repair and the healthy tail."""
    cmms = SeedCmms()
    sim = make_sim(cmms)
    for k in range(LOOP // 300):
        now = ANCHOR + 300 * k + 7.0
        before = len(cmms.statements)
        sim.monitor_asset_condition(f"c{k}", now=now)
        issued = cmms.statements[before:]
        assert not [s for s in issued if "UPDATE" in s]
        if not cm.spectral_features(cm.loop_step(cm.story_seconds(now, ANCHOR)))["bpfo_alarm"]:
            assert issued == []
    assert cmms.open_condition() == {MESSimulator._loop_wonum(0)}
    assert cmms.log == []


# ---------------------------------------------------------------------------
# Demo / frozen accounts (wall-clock loop): the same rule, no story lock
# ---------------------------------------------------------------------------

def test_wall_clock_loops_keep_one_open_order_and_take_no_story_lock(make_sim):
    cmms = SeedCmms()
    sim = make_sim(cmms, anchor=0.0)
    start = 4242 * LOOP
    for k in range(3 * LOOP // 300):
        sim.monitor_asset_condition(f"w{k}", now=start + 300 * k + 7.0)
    assert cmms.open_condition() == {MESSimulator._loop_wonum(4244)}
    for loop in (4242, 4243):
        _assert_completed_in_its_own_repair(cmms, loop, anchor=0.0)
    assert not [s for s in cmms.statements if "pg_advisory" in s]


# ---------------------------------------------------------------------------
# Era safety and failures
# ---------------------------------------------------------------------------

def test_a_moved_story_clock_skips_the_close_out_too(make_sim):
    """A placement between the cycle's anchor load and the monitor: the
    loop arithmetic is on the OLD anchor, so nothing is completed either."""
    cmms = SeedCmms()
    _stale_orders(cmms, range(3))
    sim = make_sim(cmms)
    sim.condition_anchor_reader = lambda: ANCHOR + 300.0
    assert sim.monitor_asset_condition("c", now=_at(3, PHASE_STEP["degrading"])) == 0
    assert len(cmms.open_condition()) == 3 and cmms.log == []


def test_a_failing_close_out_rolls_back_and_warns_once_per_loop(make_sim, caplog):
    rolled = []
    sim = make_sim(SeedCmms())

    def _boom(sql, params=None):
        raise RuntimeError("relation does not exist")
    cursor = SimpleNamespace(execute=_boom, close=lambda: None)
    sim._get_seed_db_conn = lambda: SimpleNamespace(
        cursor=lambda: cursor, commit=lambda: None, rollback=lambda: rolled.append(1), closed=0)
    with caplog.at_level("WARNING"):
        for k in range(3):
            assert sim.monitor_asset_condition("c", now=_at(4, PHASE_STEP["healthy"]) + k) == 0
    assert len(rolled) == 3
    assert sum("Condition monitor work order FAILED" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# The writer's contract (the PostgreSQL lane runs the statement itself)
# ---------------------------------------------------------------------------

def test_complete_condition_work_orders_contract():
    from unittest.mock import MagicMock
    cursor = MagicMock(name="seed_cursor")
    cursor.rowcount = 3
    loop_start, now = ANCHOR + 7 * LOOP, ANCHOR + 7 * LOOP + 600.0
    assert MESSimulator.complete_condition_work_orders(cursor, loop_start, now) == 3
    cursor.execute.assert_called_once()     # one statement, no commit: the caller owns the transaction
    sql, params = cursor.execute.call_args.args
    schema, _prefix = MESSimulator.CMMS_SITE_TARGET[MESSimulator.CONDITION_MONITOR_SITE]
    assert f"UPDATE {schema}.work_orders" in sql and f"INSERT INTO {schema}.work_order_log" in sql
    assert "'COMPLETE'" in sql and "NOW()" not in sql
    assert params == {
        "status": "COMP", "repair_seconds": cm.BEARING_REPAIR_STEPS * cm.INTERVAL_SEC,
        "now": _utc(now), "asset": cm.STORY_CMMS_ASSET_NUM, "prefix": PREFIX + "%",
        "open_statuses": list(OPEN), "loop_start": _utc(loop_start),
        "author": MESSimulator.CONDITION_COMPLETION_AUTHOR,
        "entry": MESSimulator.CONDITION_COMPLETION_ENTRY}


@pytest.mark.parametrize("rowcount", [0, -1, None])
def test_complete_condition_work_orders_reports_zero_when_nothing_matched(rowcount):
    from unittest.mock import MagicMock
    cursor = MagicMock(name="seed_cursor")
    cursor.rowcount = rowcount
    assert MESSimulator.complete_condition_work_orders(cursor, ANCHOR, ANCHOR) == 0


def test_open_statuses_are_the_history_writers_and_the_rows_fit_the_ddl():
    assert MESSimulator.CONDITION_OPEN_STATUSES == sh.OPEN_STATUSES
    ddl = (_REPO_ROOT / "lambda-adt-seed" / f"011_{sh.CMMS_SCHEMA}.sql").read_text(encoding="utf-8")
    allowed = ddl.split("status          VARCHAR(10)  NOT NULL CHECK (status IN (", 1)[1].split(")", 1)[0]
    statuses = {s.strip().strip("'") for s in allowed.split(",")}
    assert {MESSimulator.CONDITION_COMPLETED_STATUS, *MESSimulator.CONDITION_OPEN_STATUSES} <= statuses
    assert "author          VARCHAR(60)" in ddl and "entry           TEXT" in ddl
    assert len(MESSimulator.CONDITION_COMPLETION_AUTHOR) <= 60


# ---------------------------------------------------------------------------
# The completion note is true for every open status
# ---------------------------------------------------------------------------

def test_the_condition_completion_note_holds_for_every_open_status(make_sim):
    """An order still in any of the four open statuses when its loop ends is
    completed, and its note makes no claim about approval it cannot know."""
    cmms = SeedCmms()
    _stale_orders(cmms, range(4))
    for loop, status in enumerate(OPEN):
        cmms.orders[MESSimulator._loop_wonum(loop)]["status"] = status
    make_sim(cmms).monitor_asset_condition("c", now=_at(4, PHASE_STEP["degrading"]))
    for loop in range(4):
        _assert_completed_in_its_own_repair(cmms, loop)
    entries = {e["entry"] for e in cmms.log}
    assert len(entries) == 1
    (entry,) = entries
    assert "approv" not in entry.lower()
    assert "still open when the bearing failed" in entry


# ---------------------------------------------------------------------------
# The MES downtime bridge's orders: completed once their loop has ended,
# worked in the downtime they were raised from
# ---------------------------------------------------------------------------

DOWNTIME_BY = "maint_02"


def _bridge(cmms: SeedCmms, erp: ErpDowntimes, loop: int, *, step: int = cm.BPFO_ALARM_STEP + 60,
            anchor: float = ANCHOR, status: str = "WAPPR", asset: str = cm.STORY_CMMS_ASSET_NUM,
            ended: bool = True, record: bool = True, category: str = "MECHANICAL",
            reason: str = "VIB-BEARING", stamped: bool = True) -> SimpleNamespace:
    """A bridge order raised in ``loop`` for a downtime the MES logged 35 to 25
    minutes before the order was entered (the shape a rehearsal account showed),
    with that downtime in the ERP unless ``record`` is False. The order carries
    the stop's start as its actual start, as the bridge writes it, unless
    ``stamped`` is False (an order written before the bridge recorded it)."""
    reported = _utc(_at(loop, step, anchor))
    start, end = reported - timedelta(minutes=35), reported - timedelta(minutes=25)
    downtime_id = erp.new_id()
    if record:
        erp.rows[downtime_id] = (start, end if ended else None, DOWNTIME_BY)
    wonum = cmms.add_bridge(downtime_id, reported=reported, status=status, asset=asset,
                            category=category, reason=reason, start=start if stamped else None)
    return SimpleNamespace(wonum=wonum, id=downtime_id, start=start, end=end, reported=reported)


_UNSET = object()


def _assert_bridge_completed(cmms: SeedCmms, b, *, start=_UNSET, finish=None, author=DOWNTIME_BY) -> None:
    order = cmms.orders[b.wonum]
    start = b.start if start is _UNSET else start
    finish = b.end if finish is None else finish
    assert (order["status"], order["actual_start"], order["actual_finish"]) == ("COMP", start, finish), b.wonum
    (entry,) = [e for e in cmms.log if e["wonum"] == b.wonum]
    assert (entry["log_type"], entry["author"]) == ("COMPLETE", author)
    assert entry["log_date"] == max(finish, b.reported)      # never before the order was entered
    assert entry["entry"] and "approv" not in entry["entry"].lower()


@pytest.mark.parametrize("phase", list(PHASE_STEP))
def test_bridge_orders_of_ended_loops_are_completed_from_their_downtime(make_sim, phase):
    """The rehearsal account: 13 open condition orders and three open bridge
    orders from loops 1, 4 and 7. One cycle in loop 13, whatever its phase,
    completes the three in the downtime they were raised for; a bridge order
    raised in loop 13 itself stays open (as the facilitator table says)."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    _stale_orders(cmms, range(13))
    earlier = [_bridge(cmms, erp, loop) for loop in (1, 4, 7)]
    current = None
    if phase != "degrading":
        _stale_orders(cmms, range(13, 14))
        current = _bridge(cmms, erp, 13, step=cm.BPFO_ALARM_STEP + 5)
    sim = make_sim(cmms, downtimes=erp)
    sim.monitor_asset_condition("first-cycle", now=_at(13, PHASE_STEP[phase]))
    for b in earlier:
        _assert_bridge_completed(cmms, b)
    assert cmms.open_bridge() == (set() if current is None else {current.wonum})
    assert erp.calls == [sorted(b.id for b in earlier)]
    snapshot = ({w: dict(o) for w, o in cmms.orders.items()}, list(cmms.log))
    sim.monitor_asset_condition("second-cycle", now=_at(13, PHASE_STEP[phase]) + 60)
    assert ({w: dict(o) for w, o in cmms.orders.items()}, list(cmms.log)) == snapshot
    assert len(erp.calls) == 1                  # nothing left to look up


def test_a_bridge_order_whose_downtime_is_still_open_waits_for_it(make_sim):
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 2, ended=False)
    sim = make_sim(cmms, downtimes=erp)
    sim.monitor_asset_condition("c1", now=_at(3, PHASE_STEP["degrading"]))
    assert cmms.open_bridge() == {b.wonum} and cmms.log == []
    erp.rows[b.id] = (b.start, b.end, DOWNTIME_BY)            # the MES resolves the downtime
    sim.monitor_asset_condition("c2", now=_at(3, PHASE_STEP["degrading"]) + 60)
    _assert_bridge_completed(cmms, b)


def test_a_bridge_order_whose_downtime_record_is_gone_is_completed_on_its_own_dates(make_sim):
    """A placement retires the old era's story runs with their downtime rows,
    and a db-init re-run wipes the ERP: such an order has no downtime left to
    read. It belongs to an ended loop, so it is completed anyway, at the moment
    it was entered, by the planner. It keeps the stop start it records; its
    report time never takes that place."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 2, record=False)
    assert cmms.orders[b.wonum]["actual_start"] == b.start != b.reported
    make_sim(cmms, downtimes=erp).monitor_asset_condition("c", now=_at(3, PHASE_STEP["degrading"]))
    _assert_bridge_completed(cmms, b, start=b.start, finish=b.reported,
                             author=MESSimulator.CONDITION_COMPLETION_AUTHOR)


def test_a_gone_downtime_with_no_recorded_stop_start_leaves_the_start_empty(make_sim):
    """An order written before the bridge recorded the stop start has none to
    keep: with its downtime row gone, the completion leaves its actual start
    empty rather than giving its report time as one."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 2, record=False, stamped=False)
    make_sim(cmms, downtimes=erp).monitor_asset_condition("c", now=_at(3, PHASE_STEP["degrading"]))
    _assert_bridge_completed(cmms, b, start=None, finish=b.reported,
                             author=MESSimulator.CONDITION_COMPLETION_AUTHOR)


def test_a_loop_zero_bridge_order_stays_open_all_of_loop_zero(make_sim):
    """Loop 0 of a fresh account is unchanged: a bridge order raised during
    the alarm stays open through the repair and the healthy tail (no lookup,
    no close-out), and the first cycle of loop 1 completes it."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    sim = make_sim(cmms, downtimes=erp)
    b = None
    for k in range(LOOP // 300):
        now = ANCHOR + 300 * k + 7.0
        if b is None and cm.loop_step(cm.story_seconds(now, ANCHOR)) > cm.BPFO_ALARM_STEP + 60:
            b = _bridge(cmms, erp, 0)
        sim.monitor_asset_condition(f"c{k}", now=now)
    assert b is not None and cmms.open_bridge() == {b.wonum}
    assert erp.calls == [] and cmms.log == []
    assert not [s for s in cmms.statements if s.startswith("SELECT wonum, description") or "UPDATE" in s]
    sim.monitor_asset_condition("loop-1", now=ANCHOR + LOOP + 7.0)
    _assert_bridge_completed(cmms, b)
    _assert_completed_in_its_own_repair(cmms, 0)


def test_only_open_bridge_orders_of_ended_loops_are_completed(make_sim):
    """Every open bridge order of an ended loop goes, whatever its asset or
    work type; a cancelled or already completed one, an order of the current
    loop, the history's orders and the seed's are left as they are."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    story = _bridge(cmms, erp, 1)
    other = _bridge(cmms, erp, 1, asset="1001-HYP-402", category="ELECTRICAL", reason="PLC-COMM")
    cancelled = _bridge(cmms, erp, 1, status="CAN")
    done = _bridge(cmms, erp, 1, status="COMP")
    current = _bridge(cmms, erp, 2, step=10)
    cmms.add(sh.DEFERRED_PM_WONUM, reported=_utc(ANCHOR - 86400), work_type="PM", failure_code=None)
    cmms.add("1201", status="APPR", reported=_utc(ANCHOR - 86400), work_type="PM", failure_code=None)
    make_sim(cmms, downtimes=erp).monitor_asset_condition("c", now=_at(2, cm.BPFO_ALARM_STEP // 2))
    _assert_bridge_completed(cmms, story)
    _assert_bridge_completed(cmms, other)
    assert cmms.orders[other.wonum]["work_type"] == "CM"
    assert cmms.orders[cancelled.wonum]["status"] == "CAN"
    assert cmms.orders[done.wonum]["actual_finish"] is None    # not rewritten
    assert cmms.open_bridge() == {current.wonum}
    assert (cmms.orders[sh.DEFERRED_PM_WONUM]["status"], cmms.orders["1201"]["status"]) == ("WAPPR", "APPR")
    assert {e["wonum"] for e in cmms.log} == {story.wonum, other.wonum}


def test_wall_clock_bridge_orders_are_completed_after_their_loop(make_sim):
    """Demo / frozen: the same rule on the epoch-aligned loop, no story lock."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    earlier = [_bridge(cmms, erp, loop, anchor=0.0) for loop in (4242, 4243)]
    current = _bridge(cmms, erp, 4244, step=10, anchor=0.0)
    make_sim(cmms, anchor=0.0, downtimes=erp).monitor_asset_condition(
        "w", now=_at(4244, cm.BPFO_ALARM_STEP // 2, anchor=0.0))
    for b in earlier:
        _assert_bridge_completed(cmms, b)
    assert cmms.open_bridge() == {current.wonum}
    assert not [s for s in cmms.statements if "pg_advisory" in s]


def test_a_moved_story_clock_leaves_bridge_orders_until_the_next_cycle(make_sim):
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 1)
    sim = make_sim(cmms, downtimes=erp)
    sim.condition_anchor_reader = lambda: ANCHOR + 300.0
    assert sim.monitor_asset_condition("moved", now=_at(2, PHASE_STEP["degrading"])) == 0
    assert cmms.open_bridge() == {b.wonum} and erp.calls == []
    sim.condition_anchor_reader = lambda: ANCHOR
    sim.monitor_asset_condition("next", now=_at(2, PHASE_STEP["degrading"]) + 60)
    _assert_bridge_completed(cmms, b)


def test_without_a_downtime_reader_bridge_orders_wait_for_one(make_sim):
    """A direct caller with no ERP reader completes the condition orders but
    cannot tell an ended downtime from a missing one, so it leaves the bridge
    orders open; the first cycle that has the reader completes them."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    _stale_orders(cmms, range(1))
    b = _bridge(cmms, erp, 1)
    sim = make_sim(cmms)
    sim.monitor_asset_condition("no-reader", now=_at(2, PHASE_STEP["degrading"]))
    _assert_completed_in_its_own_repair(cmms, 0)
    assert cmms.open_bridge() == {b.wonum}
    sim.condition_downtime_reader = erp.read
    sim.monitor_asset_condition("reader", now=_at(2, PHASE_STEP["degrading"]) + 60)
    _assert_bridge_completed(cmms, b)


def test_a_failing_downtime_read_rolls_the_whole_cycle_back(make_sim, caplog):
    cmms, erp = SeedCmms(), ErpDowntimes()
    _stale_orders(cmms, range(1))
    _bridge(cmms, erp, 1)

    def _down(ids):
        raise RuntimeError("ERP unavailable")
    sim = make_sim(cmms)
    sim.condition_downtime_reader = _down
    with caplog.at_level("WARNING"):
        for k in range(3):
            assert sim.monitor_asset_condition("c", now=_at(2, PHASE_STEP["degrading"]) + k) == 0
    assert "commit" not in cmms.tx and cmms.tx.count("rollback") == 3
    assert sum("Condition monitor work order FAILED" in r.message for r in caplog.records) == 1


def test_complete_bridge_work_orders_contract(monkeypatch):
    """On the caller's cursor: the bounded candidate read from a random start,
    its wrap-round read for what is left of the bound, then one completion that
    also writes the log; no commit. Nothing to complete issues no completion and
    reads no downtime."""
    from unittest.mock import MagicMock
    schema, _prefix = MESSimulator.CMMS_SITE_TARGET[MESSimulator.CONDITION_MONITOR_SITE]
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 6)
    loop_start, now = ANCHOR + 7 * LOOP, ANCHOR + 7 * LOOP + 600.0
    start = BRIDGE_PREFIX + "0" * 8                      # the lowest bridge number: one read finds the order
    monkeypatch.setattr(MESSimulator, "_bridge_scan_start", classmethod(lambda cls: start))
    cursor = MagicMock(name="seed_cursor")
    description = cmms.orders[b.wonum]["description"]
    cursor.fetchall.side_effect = [[(b.wonum, description, b.reported, MESSimulator.CMMS_BRIDGE_REPORTED_BY,
                                     b.start)], []]
    cursor.rowcount = 1
    assert MESSimulator.complete_bridge_work_orders(cursor, loop_start, now, erp.read) == 1
    (from_sql, from_params), (before_sql, before_params), (update_sql, update_params) = [
        c.args for c in cursor.execute.call_args_list]
    scope = {"prefix": BRIDGE_PREFIX + "%", "open_statuses": list(OPEN), "loop_start": _utc(loop_start)}
    assert f"FROM {schema}.work_orders" in from_sql and "wonum >= %(start)s" in from_sql
    assert "wonum < %(start)s" in before_sql and "LIMIT %(limit)s" in from_sql and "LIMIT %(limit)s" in before_sql
    assert from_params == {**scope, "start": start, "limit": BOUND}
    assert before_params == {**scope, "start": start, "limit": BOUND - 1}
    assert f"UPDATE {schema}.work_orders" in update_sql and f"INSERT INTO {schema}.work_order_log" in update_sql
    assert "'COMPLETE'" in update_sql and "NOW()" not in update_sql
    assert "reported_by" in from_sql.split("FROM", 1)[0]
    for read_sql in (from_sql, before_sql):                       # the stop start it already records
        assert "actual_start" in read_sql.split("FROM", 1)[0]
    assert "wo.reported_by = %(reported_by)s" in update_sql and "wo.description = p.description" in update_sql
    assert update_params == {
        "status": "COMP", "wonums": [b.wonum], "starts": [b.start], "finishes": [b.end],
        "authors": [DOWNTIME_BY], "descriptions": [description],
        "reported_by": MESSimulator.CMMS_BRIDGE_REPORTED_BY, **scope,
        "default_author": MESSimulator.CONDITION_COMPLETION_AUTHOR,
        "entry": MESSimulator.BRIDGE_COMPLETION_ENTRY}
    empty = MagicMock(name="seed_cursor")
    empty.fetchall.return_value = []
    assert MESSimulator.complete_bridge_work_orders(empty, loop_start, now, erp.read) == 0
    assert empty.execute.call_count == 2 and erp.calls == [[b.id]]


def test_read_downtimes_reads_the_erp_by_id():
    from unittest.mock import MagicMock
    cursor = MagicMock(name="erp_cursor")
    start, end = datetime(2026, 10, 3, 5, 15), datetime(2026, 10, 3, 5, 25)
    ident = "0822D3E2-1F2E-4A5B-8C6D-000000000001"
    cursor.fetchall.return_value = [(ident.lower(), start, end, DOWNTIME_BY)]
    conn = MagicMock(name="erp_conn")
    conn.cursor.return_value = cursor
    assert MESSimulator.read_downtimes(conn, [ident.lower()]) == {ident.lower(): (start, end, DOWNTIME_BY)}
    cursor.close.assert_called_once()
    sql, params = cursor.execute.call_args.args
    assert "FROM mes_downtime_events" in sql and "= ANY(%s::uuid[])" in sql
    assert params == ([ident.lower()],)
    assert MESSimulator.read_downtimes(conn, []) == {} and cursor.execute.call_count == 1


# ---------------------------------------------------------------------------
# Only a proven MES-bridge order is completed: reported by the bridge AND its
# description carries the canonical downtime id that reproduces its number.
# Any other IND-* row stays open, unchanged, and is reported once per loop.
# ---------------------------------------------------------------------------

def _snapshot(cmms: SeedCmms, wonum: str) -> tuple:
    return dict(cmms.orders[wonum]), [e for e in cmms.log if e["wonum"] == wonum]


def _two_cycles(sim, loop: int) -> None:
    """A cycle in the next loop and one in the loop after (an IND-* row of
    ``loop`` is a candidate in both)."""
    sim.monitor_asset_condition("next-loop", now=_at(loop + 1, PHASE_STEP["degrading"]))
    sim.monitor_asset_condition("loop-after", now=_at(loop + 2, PHASE_STEP["healthy"]))


@pytest.mark.parametrize("asset", [cm.STORY_CMMS_ASSET_NUM, "1001-CGR-502"], ids=["story-asset", "other-asset"])
@pytest.mark.parametrize("status", ["WAPPR", "WMATL"])
def test_an_ind_order_the_bridge_did_not_raise_stays_open(make_sim, asset, status):
    """A person's IND-* order (another reporter, no downtime id) is not the
    simulator's to complete, whatever its asset; a proven bridge order beside
    it is completed as before."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    entered = _utc(_at(1, cm.BPFO_ALARM_STEP + 60))
    cmms.add("IND-12345678", status=status, reported=entered, sched=entered, asset=asset, work_type="CM",
             failure_code="BEARING", description="Spindle coolant leak; waiting on parts", reported_by="R. Roe")
    bridge = _bridge(cmms, erp, 1)
    before = _snapshot(cmms, "IND-12345678")
    _two_cycles(make_sim(cmms, downtimes=erp), 1)
    assert _snapshot(cmms, "IND-12345678") == before
    _assert_bridge_completed(cmms, bridge)


def _replace_id(description: str, new: str) -> str:
    head, _sep, _old = description.rpartition("MES downtime event ")
    return f"{head}MES downtime event {new}"


@pytest.mark.parametrize("edit", [
    lambda d, i: _replace_id(d, "11111111-2222-4333-8444-555555555555"),   # another downtime's id
    lambda d, i: _replace_id(d, i.upper()),                                # not the canonical form
    lambda d, i: d + " Bearing on order, see note.",                      # text after the id
    lambda d, i: "Spindle bearing noise reported by the operator",        # rewritten by hand
], ids=["other-id", "upper-case-id", "appended-note", "rewritten"])
def test_a_bridge_shaped_order_with_an_edited_description_stays_open(make_sim, edit):
    """The downtime the order was raised from is still in the ERP, but the
    order's own text no longer proves it was: it is left as it is."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 1)
    cmms.orders[b.wonum]["description"] = edit(cmms.orders[b.wonum]["description"], b.id)
    before = _snapshot(cmms, b.wonum)
    _two_cycles(make_sim(cmms, downtimes=erp), 1)
    assert _snapshot(cmms, b.wonum) == before


def test_a_bridge_shaped_order_reported_by_someone_else_stays_open(make_sim):
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 1)
    cmms.orders[b.wonum]["reported_by"] = "R. Roe"
    before = _snapshot(cmms, b.wonum)
    _two_cycles(make_sim(cmms, downtimes=erp), 1)
    assert _snapshot(cmms, b.wonum) == before


def test_an_unproven_order_without_its_downtime_does_not_take_the_entry_time_fallback(make_sim):
    """The entry-time fallback is for a proven bridge order whose downtime row
    is gone; an unproven one with no downtime row stays open."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    gone = _bridge(cmms, erp, 1, record=False)
    unproven = _bridge(cmms, erp, 1, record=False)
    cmms.orders[unproven.wonum]["reported_by"] = "R. Roe"
    _two_cycles(make_sim(cmms, downtimes=erp), 1)
    _assert_bridge_completed(cmms, gone, start=gone.start, finish=gone.reported,
                             author=MESSimulator.CONDITION_COMPLETION_AUTHOR)
    assert cmms.orders[unproven.wonum]["status"] == "WAPPR"
    assert not [e for e in cmms.log if e["wonum"] == unproven.wonum]


def test_unproven_orders_are_reported_once_per_loop(make_sim, caplog):
    """One warning naming the rows left open, in the first cycle of a loop
    that sees them; not again in that loop (the same simulator, or a new one
    in the same process), and once more in the next loop."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    entered = _utc(_at(1, cm.BPFO_ALARM_STEP + 60))
    cmms.add("IND-12345678", reported=entered, sched=entered, description="Spindle coolant leak",
             reported_by="R. Roe")

    def warnings():
        return [r for r in caplog.records if r.levelname == "WARNING" and "IND-12345678" in r.getMessage()]
    with caplog.at_level("WARNING"):
        sim = make_sim(cmms, downtimes=erp)
        for k in range(5):
            sim.monitor_asset_condition("c", now=_at(2, PHASE_STEP["degrading"]) + 60 * k)
        make_sim(cmms, downtimes=erp).monitor_asset_condition("restart", now=_at(2, PHASE_STEP["healthy"]))
        assert len(warnings()) == 1
        sim.monitor_asset_condition("next-loop", now=_at(3, PHASE_STEP["degrading"]))
        sim.monitor_asset_condition("next-loop-2", now=_at(3, PHASE_STEP["degrading"]) + 60)
    assert len(warnings()) == 2
    assert cmms.orders["IND-12345678"]["status"] == "WAPPR"


def test_a_bridge_order_edited_after_the_candidate_read_is_left_open(make_sim):
    """The completion re-checks the reporter and the exact proven text, so an
    edit landing between the read and the update leaves the order open."""
    cmms, erp = SeedCmms(), ErpDowntimes()
    b = _bridge(cmms, erp, 1)
    cmms.after_candidates.append(lambda: cmms.orders[b.wonum].update(description="Edited by the planner"))
    make_sim(cmms, downtimes=erp).monitor_asset_condition("c", now=_at(2, PHASE_STEP["degrading"]))
    assert cmms.orders[b.wonum]["status"] == "WAPPR"
    assert not [e for e in cmms.log if e["wonum"] == b.wonum]


def test_the_bridge_writer_reports_its_orders_as_the_mes_bridge():
    """The proof's reporter is the writer's own (the row its entry point writes)."""
    wonum, _asset, _type, description, _prio, _code, reported_by = _bridge_insert(
        "0822d3e2-1f2e-4a5b-8c6d-000000000001", cm.STORY_CMMS_ASSET_NUM, "MECHANICAL", "VIB-BEARING")
    assert reported_by == MESSimulator.CMMS_BRIDGE_REPORTED_BY
    assert MESSimulator._proven_bridge_downtime_id(wonum, description, reported_by) == \
        "0822d3e2-1f2e-4a5b-8c6d-000000000001"
    assert MESSimulator._proven_bridge_downtime_id(wonum, description, "R. Roe") is None
