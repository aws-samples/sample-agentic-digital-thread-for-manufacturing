# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test-only: the LIVE-story state of the site CMMS schema, as column dicts.

Built from the REAL sources, so it cannot drift from what an account holds:

* the site-CMMS seed file's INSERT rows, parsed from the file;
* what the live plant history does to them: the frozen-era open orders on the
  story asset become ``CAN`` with the supersede suffix, its completed ones get
  the prior-era prefix, and the story PM's schedule moves to the deferred PM's
  dates; every non-story plant PM plan is shifted onto the live run too
  (its schedule dates moved, its own completed CLOSE cycle written, and its
  one open frozen-era order superseded to ``CAN``);
* the history's own orders (the plan's CMMS work orders: 3101, 3107, 3108),
  the six aligned-plant-PM CLOSE orders (the reserved 32xx band), and
  condition-monitor (``VIB-CMX-<loop>``) and MES downtime-bridge
  (``IND-*``) orders built by calling those writers themselves.
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402
from erp_simulation import story_history as sh  # noqa: E402
from erp_simulation.mes_simulator import MESSimulator  # noqa: E402

SEED_SQL = _REPO_ROOT / "lambda-adt-seed" / f"011_{sh.CMMS_SCHEMA}.sql"
#: A fixed story anchor (2026-10-03, snapped onto the story grid).
ANCHOR = float(cm.snap_anchor(1791000000, "down"))
CONDITION_LOOP = 1
CONDITION_WONUM = MESSimulator._loop_wonum(CONDITION_LOOP)

COLUMNS = {
    "assets": ("asset_num", "description", "site_id", "location", "asset_type", "status",
               "manufacturer", "model_number", "serial_number", "install_date", "priority"),
    "pm_schedules": ("pm_num", "asset_num", "description", "frequency", "frequency_units",
                     "assigned_crew", "last_completion_date", "next_due_date", "pm_status"),
    "work_orders": ("wonum", "asset_num", "work_type", "description", "status", "priority",
                    "pm_num", "reported_by", "reported_date", "target_start", "sched_start",
                    "actual_start", "actual_finish", "failure_code", "crew"),
}
_DATES = {"install_date", "last_completion_date", "next_due_date"}
_TIMESTAMPS = {"reported_date", "target_start", "sched_start", "actual_start", "actual_finish"}
_TOKEN = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?\d+)")


def _value(column: str, token: tuple) -> object:
    text, null, number = token
    if null:
        return None
    if number:
        return int(number)
    text = text.replace("''", "'")
    if column in _DATES:
        return date.fromisoformat(text)
    if column in _TIMESTAMPS:
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    return text


def seed_rows(table: str) -> list[dict]:
    """The ``INSERT INTO <table> VALUES`` rows of the 011 seed file."""
    sql = SEED_SQL.read_text(encoding="utf-8")
    block = sql.split(f"INSERT INTO {table} VALUES", 1)[1].split("ON CONFLICT", 1)[0]
    rows = []
    for line in block.splitlines():
        if not line.strip().startswith("("):
            continue
        tokens = _TOKEN.findall(line)
        rows.append({c: _value(c, t) for c, t in zip(COLUMNS[table], tokens)})
    return rows


def _live_frozen_era(wo: dict, plant_pm_nums: frozenset[str]) -> dict:
    """One 011 row as the live story leaves it. On the story asset: open orders
    become ``CAN`` with the supersede suffix and completed ones get the
    prior-era prefix. Off the story asset: an open order whose ``pm_num`` is one
    of the aligned plant PM plans is also superseded (``CAN`` + suffix), exactly
    as the live history cancels it; an open order on no aligned plan (e.g. the
    CAL on 1001-TPS-100) stays as it is."""
    if wo["asset_num"] != cm.STORY_CMMS_ASSET_NUM:
        if wo["status"] in sh.OPEN_STATUSES and wo["pm_num"] in plant_pm_nums \
                and wo["wonum"] not in sh.PLANT_PM_CLOSE_WONUMS:
            return {**wo, "status": "CAN",
                    "description": wo["description"] + sh.SUPERSEDED_DESCRIPTION_SUFFIX}
        return wo
    if wo["status"] in sh.OPEN_STATUSES:
        return {**wo, "status": "CAN",
                "description": wo["description"] + sh.SUPERSEDED_DESCRIPTION_SUFFIX}
    if wo["status"] == "COMP":
        return {**wo, "description": sh.PRIOR_ERA_PREFIX + wo["description"]}
    return wo


def _plant_crew(pm_num: str) -> str:
    """The crew the 011 seed assigned the aligned plant plan (the writer's
    ``story_history._plant_crew`` reads the same baseline)."""
    return next(b.crew for b in sh.PLANT_PM_BASELINE if b.pm_num == pm_num)


def _plant_pm_close_orders(plan: sh.StoryPlan) -> list[dict]:
    """The CLOSE work order the live history writes for each aligned plant PM
    plan's shifted last-completion cycle — its own asset and crew, built from
    the plan's ``close`` order (mirrors ``story_history._write_plant_pm``)."""
    return [{**{c: None for c in COLUMNS["work_orders"]}, **asdict(p.close),
             "asset_num": p.asset_num, "crew": _plant_crew(p.pm_num)}
            for p in plan.plant_pm]


def _history_orders(plan: sh.StoryPlan) -> list[dict]:
    return [{**asdict(wo), "asset_num": cm.STORY_CMMS_ASSET_NUM, "crew": sh.CREW}
            for wo in plan.cmms_work_orders]


def condition_order(loop: int = CONDITION_LOOP) -> dict:
    """Loop ``loop`` after the anchor's condition order (``VIB-CMX-<loop>``),
    exactly as the condition monitor's writer inserts it."""
    inside = ANCHOR + loop * cm.TOTAL_SECONDS + cm.ALARM_OFFSET_SECONDS + 60
    assert cm.loop_number(cm.story_seconds(inside, ANCHOR)) == loop
    cur = _BridgeCursor([])
    MESSimulator.insert_condition_work_order(
        cur, loop, cm.alarm_epoch(inside, ANCHOR), cm.fail_epoch(inside, ANCHOR))
    ((sql, params),) = cur.inserts
    return {**{c: None for c in COLUMNS["work_orders"]}, **_insert_row(sql, params)}


def _live_pm_schedules(plan: sh.StoryPlan) -> list[dict]:
    """Every ``pm_schedules`` row with its live dates: the story PM moves to the
    deferred PM's dates, and each aligned plant PM moves to its shifted
    last-completion / next-due (mirrors ``story_history._write_plant_pm``)."""
    last, due = (date.fromisoformat(d) for d in sh._pm_schedule_dates(plan))
    plant = {p.pm_num: p for p in plan.plant_pm}
    out = []
    for pm in seed_rows("pm_schedules"):
        if pm["pm_num"] == sh.STORY_PM_NUM:
            out.append({**pm, "last_completion_date": last, "next_due_date": due})
        elif pm["pm_num"] in plant:
            p = plant[pm["pm_num"]]
            out.append({**pm, "last_completion_date": p.last_completion,
                        "next_due_date": p.next_due})
        else:
            out.append(pm)
    return out


def live_tables() -> dict[str, list[dict]]:
    """``assets`` / ``work_orders`` / ``pm_schedules`` of a live-story account."""
    plan = sh.plan(ANCHOR)
    plant_pm_nums = frozenset(b.pm_num for b in sh.PLANT_PM_BASELINE)
    work_orders = [_live_frozen_era(wo, plant_pm_nums) for wo in seed_rows("work_orders")]
    work_orders += _history_orders(plan) + _plant_pm_close_orders(plan) + [condition_order()]
    return {"assets": seed_rows("assets"), "work_orders": work_orders,
            "pm_schedules": _live_pm_schedules(plan)}


# ---------------------------------------------------------------------------
# The MES downtime bridge's orders, built from its own writer
# ---------------------------------------------------------------------------

#: What ``NOW()`` stands for in a bridge order built here.
BRIDGE_NOW = datetime(2026, 10, 3, 14, 5)


class _BridgeCursor:
    """Records the bridge writer's statements and answers its asset lookup the
    way the database would (preferred asset type first, then by number)."""

    def __init__(self, assets: list[dict]):
        self._assets, self.inserts, self._row, self.rowcount = assets, [], None, 1

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            prefix, preferred = params[0].rstrip("%"), params[1]
            matches = sorted((a for a in self._assets if a["asset_num"].startswith(prefix)),
                             key=lambda a: (a["asset_type"] != preferred, a["asset_num"]))
            self._row = (matches[0]["asset_num"],) if matches else None
        else:
            self.inserts.append((sql, params))

    def fetchone(self):
        return self._row

    def close(self):
        pass


def _insert_row(sql: str, params: tuple) -> dict:
    """The row an ``INSERT INTO ...(cols) VALUES (...)`` writes: ``%s`` takes the
    next parameter, a quoted literal is itself, a bare integer is an int and
    ``NOW()`` is :data:`BRIDGE_NOW`."""
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    values = sql.split("VALUES", 1)[1].split("(", 1)[1].rsplit("ON CONFLICT", 1)[0]
    values = values.strip().rstrip(")").replace("NOW()", "NOW")
    remaining, row = list(params), {}
    for col, token in zip(cols, (t.strip() for t in values.split(","))):
        row[col] = (remaining.pop(0) if token == "%s" else BRIDGE_NOW if token == "NOW"  # nosec B105 - SQL placeholder tokens, not credentials
                    else int(token) if token.isdigit() else token.strip("'"))
    assert not remaining and len(row) == len(cols), (cols, values)
    return row


def bridge_order(assets: list[dict], category: str, reason: str, downtime_id: str,
                 downtime_start=None) -> dict:
    """One work order exactly as the MES downtime bridge writes it, through the
    bridge's own entry point (it routes the site to its own CMMS writer).
    ``downtime_start`` is the stop's start the bridge is handed, when given."""
    simulator = MESSimulator.__new__(MESSimulator)   # the bridge uses class constants only
    cur = _BridgeCursor(assets)
    seed_conn = SimpleNamespace(cursor=lambda: cur, commit=lambda: None, rollback=lambda: None)
    simulator._get_seed_db_conn = lambda: seed_conn
    site_row = (MESSimulator.CONDITION_MONITOR_SITE,)
    erp_cursor = SimpleNamespace(execute=lambda sql, params=None: None, fetchone=lambda: site_row)
    started = {"downtime_start": downtime_start} if downtime_start is not None else {}
    simulator._create_cmms_work_order_for_downtime(
        erp_cursor, "mes-work-order", cm.STORY_MES_LINE, category, reason, downtime_id, **started)
    ((sql, params),) = cur.inserts                    # the writer's one INSERT (a failure is swallowed)
    return {**{c: None for c in COLUMNS["work_orders"]}, **_insert_row(sql, params)}

#: The reader's per-transaction statement timeout (``SET LOCAL`` cannot bind a
#: parameter on the server, so it is ``set_config(..., true)`` with a str).
SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, true)"


class FakeCursor:
    """A pgdriver-like cursor answering the site-CMMS reader's three SELECTs."""

    def __init__(self, tables: dict[str, list[dict]], log: list):
        self._tables, self._log, self._rows, self.description = tables, log, [], None

    def execute(self, query, params=None):
        self._log.append((query, params))
        if query == SET_TIMEOUT_SQL:
            return
        table = next(t for t in COLUMNS if f".{t} " in query)
        cols = [c.strip() for c in query.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        self.description = [(c,) for c in cols]
        limit = params[-1] if params else None
        self._rows = [tuple(r.get(c) for c in cols) for r in self._tables[table]][:limit]

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
