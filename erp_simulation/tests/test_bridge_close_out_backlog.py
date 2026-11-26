# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The condition monitor's close-out of MES-bridge orders reads one bounded batch per cycle.

``MESSimulator.complete_bridge_work_orders`` completes the open ``IND-*``
orders of ended story loops that the MES bridge provably raised, each worked
in the downtime it was raised from. It used to read EVERY open ``IND-*`` order
of an ended loop, send every proven downtime id to the ERP in one lookup and
complete them all in one statement. A close-out that keeps failing (a downtime
reader that cannot reach the ERP) leaves those orders open while the bridge
keeps raising new ones, so the set grows with every stop, and the first cycle
that reaches it reads all of it: the unbounded-read shape that timed out the
simulator Lambda and ran it out of memory in the status steps.

The read now takes at most ``MAX_ROWS_PER_CYCLE`` orders per cycle, starting
at a fresh random bridge work-order number and wrapping round to the lowest
(the BACKORDERED and consumption reads do the same), so orders that can never
be proven (a person's ``IND-*`` order) cannot keep the proven ones behind them
waiting. The downtime lookup and the completion take only that batch. A set
smaller than the bound is read whole, in work-order-number order, as before.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import mes_simulator
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.tests._pg_sql import stmt

_SITE = MESSimulator.CONDITION_MONITOR_SITE
_SCHEMA = MESSimulator.CMMS_SITE_TARGET[_SITE][0]
_REPORTER = MESSimulator.CMMS_BRIDGE_REPORTED_BY
_OPEN = list(MESSimulator.CONDITION_OPEN_STATUSES)


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _downtime_id(k: int) -> str:
    """A canonical downtime id whose bridge order is ``IND-%08X`` of ``k``."""
    return f"{k:08x}-0000-4000-8000-{k:012x}"


def _bridge_row(k: int, reported: datetime) -> tuple:
    """One candidate row ``(wonum, description, reported_date, reported_by,
    actual_start)`` as the bridge's own writer enters it for downtime ``k``. The
    recorded stop start is not under test here, so it is left unset."""
    wonum, _type, _priority, description, _code = MESSimulator.indianapolis_bridge_order(
        _SITE, cm.STORY_MES_LINE, 'MECHANICAL', 'VIB-BEARING', _downtime_id(k))
    return wonum, description, reported, _REPORTER, None


# --------------------------------------------------------------------------
# Scripted seed cursor (runs in every lane).
# --------------------------------------------------------------------------
class _SeedCursor:
    """Open ``IND-*`` orders of ended loops, sorted by wonum. Serves the bounded
    start/wrap candidate reads (or, unbounded, all of them) and records each
    completion's work orders."""

    def __init__(self, rows):
        self.rows = sorted(rows)
        self.reads, self.completed = [], []
        self.rowcount, self._all = 0, []

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self._all = []
        if text.startswith("SELECT wonum"):
            self._all = self._candidates(text, params)
        elif text.startswith("WITH plan"):
            self.completed.append(list(params["wonums"]))
            self.rowcount = len(params["wonums"])

    def _candidates(self, text, params):
        if "LIMIT" not in text:
            self.reads.append(None)
            return list(self.rows)
        start, limit = params["start"], params["limit"]
        before = "wonum < %(start)s" in text
        self.reads.append(("before" if before else "from", start, limit))
        return [r for r in self.rows if (r[0] < start if before else r[0] >= start)][:limit]

    def fetchall(self):
        return list(self._all)


class _Downtimes:
    """The ERP downtime reader: every id it is asked for ended 25 minutes before ``reported``."""

    def __init__(self, reported):
        self.reported, self.calls = reported, []

    def __call__(self, ids):
        self.calls.append(list(ids))
        start, end = self.reported - timedelta(minutes=35), self.reported - timedelta(minutes=25)
        return {i: (start, end, 'maint_02') for i in ids}


def _close_out(cursor, reader, now):
    return MESSimulator.complete_bridge_work_orders(cursor, now - 3600.0, now, reader)


def test_one_cycle_reads_proves_and_completes_at_most_one_bounded_batch():
    bound = mes_simulator.MAX_ROWS_PER_CYCLE
    now = time.time()
    reported = _utc(now - 7200.0)
    cursor = _SeedCursor([_bridge_row(k, reported) for k in range(1, 5 * bound + 1)])
    reader = _Downtimes(reported)
    assert _close_out(cursor, reader, now) == bound
    assert cursor.reads and None not in cursor.reads               # every candidate read carries a LIMIT
    assert cursor.reads[0][2] == bound
    assert sum(read[2] for read in cursor.reads[1:]) <= bound       # the wrap read takes what is left
    assert len(reader.calls) == 1 and len(reader.calls[0]) == bound  # one bounded downtime lookup
    assert [len(batch) for batch in cursor.completed] == [bound]     # one bounded completion


def test_the_candidate_read_wraps_round_from_its_start(monkeypatch):
    monkeypatch.setattr(mes_simulator, 'MAX_ROWS_PER_CYCLE', 4)
    now = time.time()
    reported = _utc(now - 7200.0)
    rows = [_bridge_row(k, reported) for k in range(1, 7)]
    start = rows[4][0]
    monkeypatch.setattr(MESSimulator, '_bridge_scan_start', classmethod(lambda cls: start), raising=False)
    cursor = _SeedCursor(rows)
    assert _close_out(cursor, _Downtimes(reported), now) == 4
    assert cursor.reads == [('from', start, 4), ('before', start, 2)]
    # The batch is handled in work-order-number order: the wrapped-round lowest first.
    assert cursor.completed == [[rows[0][0], rows[1][0], rows[4][0], rows[5][0]]]


def test_the_scan_start_is_a_fresh_bridge_work_order_number_every_cycle():
    starts = {MESSimulator._bridge_scan_start() for _ in range(20)}
    assert len(starts) == 20
    assert all(re.fullmatch(r"IND-[0-9A-F]{8}", start) for start in starts)


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise): the seed's site
# CMMS and the ERP's MES downtime events.
# --------------------------------------------------------------------------
from erp_simulation.tests.test_story_history import pg_pair  # noqa: E402,F401  (fixture)

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_PERSON_ORDERS = [f"IND-{k:08X}" for k in range(1, 5)]   # below every bridge order the test adds
_BRIDGE_KS = list(range(0x10, 0x18))                    # IND-00000010 .. IND-00000017


def _seed_stalled_backlog(erp, seed, reported):
    """The backlog a stalled close-out leaves: eight bridge orders (the bridge's own
    writer, each with its ended MES downtime in the ERP) and four ``IND-*`` orders a
    person entered, all open and reported at ``reported`` (an ended loop)."""
    writer = MESSimulator.__new__(MESSimulator)          # the writer uses class constants only
    with seed.cursor() as scur, erp.cursor() as ecur:
        for k in _BRIDGE_KS:
            ecur.execute("""
                INSERT INTO mes_downtime_events (downtime_id, line_id, downtime_category, downtime_reason,
                                                 start_time, end_time, resolved_by, resolution_notes)
                VALUES (%s, %s, 'MECHANICAL', 'VIB-BEARING', %s, %s, 'maint_02', 'Resolved after 10 min')
            """, (_downtime_id(k), cm.STORY_MES_LINE,
                  reported - timedelta(minutes=35), reported - timedelta(minutes=25)))
            writer._insert_indianapolis_cmms_work_order(
                scur, _SITE, cm.STORY_MES_LINE, 'MECHANICAL', 'VIB-BEARING', _downtime_id(k))
        for wonum in _PERSON_ORDERS:
            scur.execute(stmt("""
                INSERT INTO {schema}.work_orders (wonum, asset_num, work_type, description, status,
                                                  priority, reported_by)
                VALUES (%s, %s, 'CM', 'Spindle coolant leak', 'WAPPR', 3, 'R. Roe')
            """, schema=_SCHEMA), (wonum, cm.STORY_CMMS_ASSET_NUM))
        scur.execute(stmt("UPDATE {schema}.work_orders SET reported_date = %s WHERE wonum LIKE 'IND-%%'",
                          schema=_SCHEMA), (reported,))
    erp.commit()
    seed.commit()


def _statuses(seed):
    with seed.cursor() as cur:
        cur.execute(stmt("SELECT wonum, status FROM {schema}.work_orders WHERE wonum LIKE 'IND-%%'",
                         schema=_SCHEMA))
        rows = dict(cur.fetchall())
    seed.rollback()
    return rows


def test_pg_a_stalled_close_out_backlog_drains_one_bounded_batch_per_cycle(pg_pair, monkeypatch):  # noqa: F811
    """Bound 3. The person's orders sit lowest, so a read taken from the start every
    cycle would re-read them and never reach the bridge orders behind them."""
    erp, seed = pg_pair["erp"], pg_pair["seed"]
    monkeypatch.setattr(mes_simulator, 'MAX_ROWS_PER_CYCLE', 3)
    monkeypatch.setattr(mes_simulator, '_unproven_bridge_warnings', {"loop_start": None})
    now = time.time()
    _seed_stalled_backlog(erp, seed, _utc(now - 7200.0))
    starts = iter(['IND-00000000', 'IND-00000004', 'IND-00000012', 'IND-00000015', 'IND-00000015'])
    monkeypatch.setattr(MESSimulator, '_bridge_scan_start', classmethod(lambda cls: next(starts)),
                        raising=False)
    lookups = []

    def read(ids):
        lookups.append(list(ids))
        try:
            return MESSimulator.read_downtimes(erp, ids)
        finally:
            erp.rollback()

    completed = []
    for _cycle in range(5):
        with seed.cursor() as cur:
            completed.append(MESSimulator.complete_bridge_work_orders(cur, now - 3600.0, now, read))
        seed.commit()
    assert completed == [0, 2, 3, 3, 0]
    assert all(len(ids) <= 3 for ids in lookups)
    bridge = {f"IND-{k:08X}" for k in _BRIDGE_KS}
    statuses = _statuses(seed)
    assert {w for w, s in statuses.items() if s == 'COMP'} == bridge
    assert {w for w, s in statuses.items() if s in _OPEN} == set(_PERSON_ORDERS)
