# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The corporate CMMS's MES-bridge orders are dated by their stop and closed when it is.

The MES downtime bridge raises a ``WO-MES-<site>-<id>`` corrective order in the
corporate CMMS (``cmms_maintenance``: the plants other than the one with its
own CMMS) for every mechanical or electrical stop. It used to write each one
``open`` with a created offset of zero and never touch it again, so every such
order stayed open for good and the CMMS view dated all of them "now": on one
demo account 15,818 open orders, against 7 seeded ones, all created at the
moment of the query.

Now the order records its stop's start (``created_ts``), and the cycle step
``close_corporate_bridge_orders`` closes the open ones the bridge provably
raised, at most ``MAX_ROWS_PER_CYCLE`` per cycle, each from the MES downtime it
was raised from: a stop that has ended completes its order with the stop's
start, end and resolver; a stop still running leaves it open; a stop the ERP no
longer holds (a Reset All or a db-init re-run wiped it) cancels it, with no
date the CMMS does not know. Orders the bridge did not provably raise, and the
seeded orders, are never touched.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from erp_simulation import mes_simulator
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation.mes_simulator import MESSimulator

_SITES = {code: prefix for code, prefix in MESSimulator.CMMS_SITE_PREFIX.items()
          if code not in MESSimulator.CMMS_SITE_TARGET}
_SITE, _PREFIX = sorted(_SITES.items())[0]


def _naive(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


def _bridge(downtime_id: str, prefix: str = _PREFIX, site: str = _SITE) -> tuple:
    """``(wo_number, description)`` exactly as the corporate bridge writes them."""
    seed_cursor = MagicMock()
    seed_cursor.fetchone.return_value = (1, f"{prefix}-FILL-001")
    MESSimulator.__new__(MESSimulator)._insert_cmms_work_order(
        seed_cursor, prefix, site, "LINE1", "MECHANICAL", "MOTOR-OVL", downtime_id)
    params = seed_cursor.execute.call_args_list[-1].args[1]
    return params[0], params[3]


# --------------------------------------------------------------------------
# Scripted seed cursor (runs in every lane).
# --------------------------------------------------------------------------
class _SeedCursor:
    """Open corporate bridge orders ``(wo_number, description)``, sorted by number.
    Serves the bounded start/wrap candidate reads and records each close-out plan."""

    def __init__(self, rows):
        self.rows = sorted(rows)
        self.reads, self.plans = [], []
        self._result = []

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self._result = []
        if text.startswith("SELECT wo_number"):
            assert "LIMIT" in text, "every candidate read is bounded"
            start, limit = params["start"], params["limit"]
            before = "wo_number < %(start)s" in text
            self.reads.append(("before" if before else "from", start, limit))
            self._result = [r for r in self.rows if (r[0] < start if before else r[0] >= start)][:limit]
        elif text.startswith("WITH plan"):
            plan = list(zip(params["wo_numbers"], params["statuses"], params["starts"], params["finishes"],
                            params["assignees"], params["hours"]))
            self.plans.append(plan)
            self._result = [(status,) for _wo, status, *_rest in plan]
        else:  # pragma: no cover - a statement the close-out must not send
            raise AssertionError(f"unexpected statement: {text[:80]}")

    def fetchall(self):
        return list(self._result)


def _close_out(cursor, read, now):
    return MESSimulator.close_out_corporate_bridge_orders(cursor, read, now)


def test_an_order_whose_stop_has_ended_is_completed_from_its_stop():
    now = time.time()
    downtime = str(uuid.uuid4())
    start, end = _naive(now - 1800), _naive(now - 1200)
    cursor = _SeedCursor([_bridge(downtime)])
    assert _close_out(cursor, lambda ids: {downtime: (start, end, "maint_02")}, now) == (1, 0)
    ((wo, status, created, finished, assignee, hours),) = cursor.plans[0]
    assert wo == cursor.rows[0][0] and status == MESSimulator.CORP_COMPLETED_STATUS
    assert created == _aware(start) and finished == _aware(end)    # the stop's own times, in UTC
    assert created.tzinfo is not None and finished.tzinfo is not None
    assert assignee == "maint_02" and hours == pytest.approx(600 / 3600, abs=0.01)


@pytest.mark.parametrize("ends_in", [None, 300.0])
def test_an_order_whose_stop_is_still_running_stays_open(ends_in):
    now = time.time()
    downtime = str(uuid.uuid4())
    end = None if ends_in is None else _naive(now + ends_in)
    cursor = _SeedCursor([_bridge(downtime)])
    assert _close_out(cursor, lambda ids: {downtime: (_naive(now - 60), end, None)}, now) == (0, 0)
    assert cursor.plans == []


def test_an_order_whose_stop_is_gone_is_cancelled_without_dates():
    now = time.time()
    cursor = _SeedCursor([_bridge(str(uuid.uuid4()))])
    assert _close_out(cursor, lambda ids: {}, now) == (0, 1)
    ((_wo, status, created, finished, assignee, hours),) = cursor.plans[0]
    assert status == MESSimulator.CORP_CANCELLED_STATUS
    assert (created, finished, assignee, hours) == (None, None, None, None)


def test_only_orders_the_bridge_provably_raised_are_touched():
    downtime, other = "abcdef12-" + str(uuid.uuid4())[9:], str(uuid.uuid4())   # letters: case matters
    wo, description = _bridge(downtime)
    rows = [
        (wo, description.replace(downtime, other)),                      # the id does not give its number
        (f"WO-MES-{_PREFIX}-{downtime[:8]}", description),               # lower-case number: not the writer's
        ("WO-MES-" + "ZZ" + wo[len("WO-MES-") + len(_PREFIX):], description),  # not a bridge site prefix
        (wo + "X", description),
        (wo, "Seal leak on the filler, raised by the planner"),
    ]
    seen = []
    cursor = _SeedCursor(rows)
    assert _close_out(cursor, lambda ids: seen.append(list(ids)) or {}, time.time()) == (0, 0)
    assert cursor.plans == [] and seen in ([], [[]])


def test_every_bridge_site_prefix_is_proven():
    """Every prefix the bridge has written into this CMMS, including the site that
    moved to its own CMMS later (its older orders are still here)."""
    for site, prefix in MESSimulator.CMMS_SITE_PREFIX.items():
        downtime = str(uuid.uuid4())
        wo, description = _bridge(downtime, prefix, site)
        assert MESSimulator._proven_corp_bridge_downtime_id(wo, description) == downtime


def test_one_cycle_reads_and_closes_at_most_one_bounded_batch():
    bound = mes_simulator.MAX_ROWS_PER_CYCLE
    rows = [_bridge(str(uuid.uuid4())) for _ in range(3 * bound)]
    lookups = []
    cursor = _SeedCursor(rows)
    completed, cancelled = _close_out(cursor, lambda ids: lookups.append(list(ids)) or {}, time.time())
    assert (completed, cancelled) == (0, bound)
    assert cursor.reads[0][2] == bound and sum(r[2] for r in cursor.reads[1:]) <= bound
    assert len(lookups) == 1 and len(lookups[0]) == bound
    assert [len(plan) for plan in cursor.plans] == [bound]


def test_the_candidate_read_wraps_round_from_a_fresh_start(monkeypatch):
    monkeypatch.setattr(mes_simulator, "MAX_ROWS_PER_CYCLE", 4)
    rows = sorted(_bridge(str(uuid.uuid4())) for _ in range(6))
    start = rows[4][0]
    monkeypatch.setattr(MESSimulator, "_corp_bridge_scan_start", classmethod(lambda cls: start))
    cursor = _SeedCursor(rows)
    assert _close_out(cursor, lambda ids: {}, time.time()) == (0, 4)
    assert cursor.reads == [("from", start, 4), ("before", start, 2)]
    assert [wo for wo, *_rest in cursor.plans[0]] == [rows[0][0], rows[1][0], rows[4][0], rows[5][0]]


def test_the_scan_start_is_a_fresh_corporate_bridge_number():
    starts = {MESSimulator._corp_bridge_scan_start() for _ in range(30)}
    assert len(starts) == 30
    prefixes = "|".join(sorted(MESSimulator.CMMS_SITE_PREFIX.values()))
    assert all(re.fullmatch(rf"WO-MES-({prefixes})-[0-9A-F]{{8}}", s) for s in starts)


def test_the_bridge_records_its_stop_start_on_the_corporate_order():
    seed_cursor = MagicMock()
    seed_cursor.fetchone.return_value = (9, f"{_PREFIX}-MIX-001")
    stop = datetime(2031, 4, 2, 7, 12, 30)
    MESSimulator.__new__(MESSimulator)._insert_cmms_work_order(
        seed_cursor, _PREFIX, _SITE, "LINE1", "MECHANICAL", "MOTOR-OVL", uuid.uuid4(), downtime_start=stop)
    sql, params = seed_cursor.execute.call_args_list[-1].args
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert columns[-1] == "created_ts"
    assert params[-1] == _aware(stop) and params[-1].tzinfo is not None


def test_the_corporate_bridge_hands_its_stop_start_over(monkeypatch):
    sim = MESSimulator.__new__(MESSimulator)
    handed = []
    monkeypatch.setattr(sim, "_insert_cmms_work_order",
                        lambda *a, **kw: handed.append(kw.get("downtime_start")), raising=False)
    stop = datetime(2031, 4, 2, 7, 12, 30)
    sim._write_bridge_order(MagicMock(), _SITE, "LINE1", "MECHANICAL", "MOTOR-OVL", uuid.uuid4(), stop)
    assert handed == [stop]


# --------------------------------------------------------------------------
# The cycle step.
# --------------------------------------------------------------------------
def _step():
    (step,) = [s for s in orch_mod._SIMULATION_STEPS if s.method == "close_corporate_bridge_orders"]
    return step


def test_the_close_out_is_its_own_step_right_after_the_downtime_step():
    names = [s.name for s in orch_mod._SIMULATION_STEPS]
    step = _step()
    assert step.owner == "mes_simulator" and not step.required
    assert names.index(step.name) == names.index("mes_downtime") + 1
    assert dict(step.copies) == {"cmms_bridge_orders_completed": "completed",
                                 "cmms_bridge_orders_cancelled": "cancelled"}


def _sim_with_seed(seed_conn):
    sim = MESSimulator.__new__(MESSimulator)
    sim._get_seed_db_conn = lambda: seed_conn
    return sim


def test_the_step_reads_the_erp_on_its_own_connection_and_commits_the_seed(monkeypatch):
    seed_conn = MagicMock()
    erp_conn = MagicMock()
    calls = []

    def close_out(cursor, read, now):
        calls.append(read(["dt-1"]))
        return 2, 1

    monkeypatch.setattr(MESSimulator, "close_out_corporate_bridge_orders", staticmethod(close_out))
    monkeypatch.setattr(MESSimulator, "read_downtimes",
                        staticmethod(lambda conn, ids: {"conn": conn, "ids": list(ids)}))
    result = _sim_with_seed(seed_conn).close_corporate_bridge_orders(erp_conn, "cycle-1")
    assert result["completed"] == 2 and result["cancelled"] == 1
    assert calls == [{"conn": erp_conn, "ids": ["dt-1"]}]
    seed_conn.commit.assert_called_once()


def test_the_step_without_a_seed_database_does_nothing():
    erp_conn = MagicMock()
    result = _sim_with_seed(None).close_corporate_bridge_orders(erp_conn, "cycle-1")
    assert (result["completed"], result["cancelled"]) == (0, 0)
    erp_conn.cursor.assert_not_called()


def test_a_failing_close_out_rolls_the_seed_back_and_does_not_raise(monkeypatch, caplog):
    seed_conn = MagicMock()

    def boom(cursor, read, now):
        raise RuntimeError("seed db down")

    monkeypatch.setattr(MESSimulator, "close_out_corporate_bridge_orders", staticmethod(boom))
    with caplog.at_level("WARNING"):
        result = _sim_with_seed(seed_conn).close_corporate_bridge_orders(MagicMock(), "cycle-1")
    assert (result["completed"], result["cancelled"]) == (0, 0)
    seed_conn.rollback.assert_called_once()
    seed_conn.commit.assert_not_called()
    assert any("seed db down" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise): the seed's
# corporate CMMS (004) and the ERP's MES downtime events.
# --------------------------------------------------------------------------
from erp_simulation.tests._pg_sql import stmt  # noqa: E402
from erp_simulation.tests.test_story_history import _REPO, _load_db_init  # noqa: E402

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_CORP_SQL = ("001_schemas.sql", "004_cmms_maintenance.sql")


def _apply_corp_seed(seed):
    with seed.cursor() as cur:
        for name in _CORP_SQL:
            cur.execute((_REPO / "lambda-adt-seed" / name).read_text(encoding="utf-8"))
    seed.commit()


@pytest.fixture
def corp_pair():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    names = {"erp": "tcerp_" + uuid.uuid4().hex[:8], "seed": "tcseed_" + uuid.uuid4().hex[:8]}
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for name in names.values():
            cur.execute(stmt("CREATE DATABASE {db}", db=name))
    conns = {k: pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": v})) for k, v in names.items()}
    try:
        _load_db_init().create_schema(conns["erp"])
        _apply_corp_seed(conns["seed"])
        yield conns
    finally:
        for conn in conns.values():
            conn.close()
        with admin.cursor() as cur:
            for name in names.values():
                cur.execute(stmt("DROP DATABASE {db}", db=name))
        admin.close()


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _stop(erp, downtime_id, start, end, resolver="maint_03"):
    with erp.cursor() as cur:
        cur.execute("""
            INSERT INTO mes_downtime_events (downtime_id, line_id, downtime_category, downtime_reason,
                                             start_time, end_time, resolved_by, resolution_notes)
            VALUES (%s, 'LINE1', 'MECHANICAL', 'MOTOR-OVL', %s, %s, %s, 'Resolved')
        """, (downtime_id, start, end, resolver))
    erp.commit()


def _bridge_order(seed, downtime_id, start=None):
    """The real corporate bridge writer, committed."""
    writer = MESSimulator.__new__(MESSimulator)
    with seed.cursor() as cur:
        writer._insert_cmms_work_order(cur, _PREFIX, _SITE, "LINE1", "MECHANICAL", "MOTOR-OVL",
                                       downtime_id, downtime_start=start)
    seed.commit()
    return f"WO-MES-{_PREFIX}-{downtime_id[:8].upper()}"


def _cycle(erp, seed, now):
    with seed.cursor() as cur:
        result = MESSimulator.close_out_corporate_bridge_orders(
            cur, lambda ids: MESSimulator.read_downtimes(erp, ids), now)
    seed.commit()
    erp.rollback()
    return result


def _view(seed, wo):
    return _rows(seed, "SELECT status, created_at, completed_at, assigned_to, actual_hours "
                       "FROM cmms_maintenance.work_orders WHERE wo_number = %s", (wo,))[0]


def _seeded(seed):
    return _rows(seed, "SELECT wo_number, status, assigned_to, completed_at IS NULL, "
                       "date_trunc('minute', NOW() - created_at) FROM cmms_maintenance.work_orders "
                       "WHERE wo_number NOT LIKE 'WO-MES-%%' ORDER BY wo_number")


def test_pg_bridge_orders_are_dated_by_their_stop_and_closed_with_it(corp_pair):
    erp, seed = corp_pair["erp"], corp_pair["seed"]
    now = time.time()
    seeded_before = _seeded(seed)
    ended, running, gone = (str(uuid.uuid4()) for _ in range(3))
    ended_at = (_naive(now - 3600), _naive(now - 3000))
    _stop(erp, ended, *ended_at)
    _stop(erp, running, _naive(now - 120), _naive(now + 600))
    wo_ended = _bridge_order(seed, ended, ended_at[0])
    wo_running = _bridge_order(seed, running, _naive(now - 120))
    wo_gone = _bridge_order(seed, gone)                       # an order from before the fix: no stop start

    assert _view(seed, wo_running)[1] == _aware(_naive(now - 120))   # dated by its stop, not "now"
    assert _cycle(erp, seed, now) == (1, 1)

    status, created, completed, assignee, hours = _view(seed, wo_ended)
    assert status == MESSimulator.CORP_COMPLETED_STATUS
    assert (created, completed) == (_aware(ended_at[0]), _aware(ended_at[1]))
    assert assignee == "maint_03" and float(hours) == pytest.approx(0.17, abs=0.01)
    assert _view(seed, wo_running)[:3] == ("open", _aware(_naive(now - 120)), None)
    assert _view(seed, wo_gone)[:3] == (MESSimulator.CORP_CANCELLED_STATUS, None, None)

    assert _cycle(erp, seed, now) == (0, 0)                  # a rerun changes nothing
    assert _cycle(erp, seed, now + 900) == (1, 0)            # the running stop has ended since
    assert _view(seed, wo_running)[0] == MESSimulator.CORP_COMPLETED_STATUS
    assert _seeded(seed) == seeded_before                    # the seeded orders are untouched


def test_pg_a_backlog_of_orphaned_orders_drains_one_bounded_batch_per_cycle(corp_pair, monkeypatch):
    erp, seed = corp_pair["erp"], corp_pair["seed"]
    monkeypatch.setattr(mes_simulator, "MAX_ROWS_PER_CYCLE", 3)
    orders = sorted(_bridge_order(seed, str(uuid.uuid4())) for _ in range(8))
    starts = iter([orders[0], orders[3], orders[6], orders[0]])
    monkeypatch.setattr(MESSimulator, "_corp_bridge_scan_start", classmethod(lambda cls: next(starts)))
    assert [_cycle(erp, seed, time.time())[1] for _ in range(4)] == [3, 3, 2, 0]
    statuses = dict(_rows(seed, "SELECT wo_number, status FROM cmms_maintenance.work_orders "
                                "WHERE wo_number LIKE 'WO-MES-%%'"))
    assert set(statuses.values()) == {MESSimulator.CORP_CANCELLED_STATUS}


def test_pg_the_candidate_read_uses_the_open_bridge_index(corp_pair):
    seed = corp_pair["seed"]
    for _ in range(50):
        _bridge_order(seed, str(uuid.uuid4()))
    with seed.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("EXPLAIN " + mes_simulator._CORP_BRIDGE_CANDIDATES_FROM_SQL,
                    {"start": "WO-MES-", "limit": 200})
        plan = "\n".join(row[0] for row in cur.fetchall())
    seed.rollback()
    assert "idx_work_orders_raw_open_bridge" in plan


def test_pg_the_seed_file_migrates_an_existing_table_and_keeps_its_rows(corp_pair):
    """An account seeded before this change: no stop-time columns, created_offset
    NOT NULL, a bridge order already in it. Re-running the seed adds the columns,
    keeps the order and its "now" dating, and is idempotent."""
    seed = corp_pair["seed"]
    with seed.cursor() as cur:
        cur.execute("DROP VIEW cmms_maintenance.work_orders")
        cur.execute("DROP INDEX cmms_maintenance.idx_work_orders_raw_open_bridge")
        cur.execute("ALTER TABLE cmms_maintenance.work_orders_raw DROP COLUMN created_ts, "
                    "DROP COLUMN completed_ts, ALTER COLUMN created_offset SET NOT NULL")
        cur.execute("""INSERT INTO cmms_maintenance.work_orders_raw
                           (wo_number, asset_id, wo_type, priority, description, status_hint, created_offset)
                       VALUES ('WO-MES-OLD-00000001', 1, 'corrective', 'high', 'old bridge order', 'open',
                               INTERVAL '0 minutes')""")
    seed.commit()
    _apply_corp_seed(seed)
    _apply_corp_seed(seed)
    ((status, age),) = _rows(seed, "SELECT status, NOW() - created_at FROM cmms_maintenance.work_orders "
                                   "WHERE wo_number = 'WO-MES-OLD-00000001'")
    assert status == "open" and age == timedelta(0)
    ((nullable,),) = _rows(seed, "SELECT is_nullable FROM information_schema.columns WHERE table_schema = "
                                 "'cmms_maintenance' AND table_name = 'work_orders_raw' "
                                 "AND column_name = 'created_offset'")
    assert nullable == "YES"
