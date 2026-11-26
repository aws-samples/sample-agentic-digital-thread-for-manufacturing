# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test-only: the corporate CMMS (schema ``cmms_maintenance``) as the mock reads it.

Built from the REAL sources, so it cannot drift from what an account holds:

* the corporate seed file's INSERT rows (``004_cmms_maintenance.sql``), parsed
  from the file; ``assets.asset_id`` is the SERIAL, so it is the row's position
  in the file's insert order;
* the ``work_orders`` / ``maintenance_schedules`` VIEWS over the ``*_raw``
  tables, computed at a fixed :data:`NOW` the way the views compute them
  (``NOW() + offset``, status from the hint or the due date);
* MES downtime-bridge orders built by calling the simulator's own bridge
  writer for a corporate site.

Every view timestamp is timezone-aware (UTC), as pgdriver returns a
``timestamptz``.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation.mes_simulator import MESSimulator  # noqa: E402

SCHEMA = "cmms_maintenance"
SEED_SQL = _REPO_ROOT / "lambda-adt-seed" / f"004_{SCHEMA}.sql"
#: The fixed "database clock" the views are computed at.
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)

ASSET_COLUMNS = ("asset_tag", "asset_name", "asset_type", "facility", "area", "manufacturer",
                 "model_number", "serial_number", "install_date", "operational_status", "criticality")
WO_RAW_COLUMNS = ("wo_number", "asset_id", "wo_type", "priority", "description", "assigned_to",
                  "status_hint", "estimated_hours", "failure_code", "created_offset",
                  "completed_offset")
SCHEDULE_RAW_COLUMNS = ("asset_id", "maintenance_type", "frequency_days", "assigned_team",
                        "last_performed_offset", "next_due_offset")

_TOKEN = re.compile(r"INTERVAL '([^']*)'|'((?:[^']|'')*)'|(NULL)|(-?\d+(?:\.\d+)?)")
_INTERVAL_PART = re.compile(r"([+-]?\d+)\s*(day|hour|minute)s?")


def interval(text: str) -> timedelta:
    """A PostgreSQL interval literal like ``-89 days 18 hours`` (each field
    carries its own sign, as PostgreSQL reads it)."""
    parts = _INTERVAL_PART.findall(text)
    assert parts, text
    unit = {"day": "days", "hour": "hours", "minute": "minutes"}
    return sum((timedelta(**{unit[u]: int(n)}) for n, u in parts), timedelta())


def _tokens(line: str) -> list:
    out = []
    for iv, text, null, number in _TOKEN.findall(line):
        if iv:
            out.append(interval(iv))
        elif null:
            out.append(None)
        elif number:
            out.append(float(number) if "." in number else int(number))
        else:
            out.append(text.replace("''", "'"))
    return out


def _blocks(prefix: str) -> list[list[list]]:
    """The token rows of every ``INSERT INTO <prefix> ... VALUES`` block."""
    sql = SEED_SQL.read_text(encoding="utf-8")
    blocks = []
    for chunk in sql.split(f"INSERT INTO {prefix}")[1:]:
        body = chunk.split("VALUES", 1)[1].split("ON CONFLICT", 1)[0]
        blocks.append([_tokens(line) for line in body.splitlines() if line.strip().startswith("(")])
    return blocks


def seed_assets() -> list[dict]:
    """The ``assets`` rows in insert order; ``asset_id`` is the SERIAL."""
    rows = [row for block in _blocks("assets (") for row in block]
    out = []
    for serial, tokens in enumerate(rows, start=1):
        row = dict(zip(ASSET_COLUMNS, tokens, strict=True))
        row["install_date"] = date.fromisoformat(row["install_date"])
        out.append({"asset_id": serial, **row, "created_at": NOW})
    return out


def _asset_ids() -> dict[str, int]:
    return {a["asset_tag"]: a["asset_id"] for a in seed_assets()}


def view_work_order(raw: dict, work_order_id: int, now: datetime = NOW) -> dict:
    """One ``work_orders_raw`` row as the ``work_orders`` view shows it."""
    done = raw.get("completed_offset")
    return {
        "work_order_id": work_order_id, "wo_number": raw["wo_number"], "asset_id": raw["asset_id"],
        "wo_type": raw["wo_type"], "priority": raw.get("priority", "medium"),
        "description": raw["description"], "assigned_to": raw.get("assigned_to"),
        "status": raw.get("status_hint", "open"), "estimated_hours": raw.get("estimated_hours"),
        "actual_hours": raw.get("actual_hours"), "failure_code": raw.get("failure_code"),
        "created_at": now + raw.get("created_offset", timedelta(days=-1)),
        "completed_at": now + done if done is not None else None,
    }


def seed_work_orders(now: datetime = NOW) -> list[dict]:
    ids = _asset_ids()
    (block,) = _blocks("work_orders_raw (")
    out = []
    for serial, tokens in enumerate(block, start=1):
        raw = dict(zip(WO_RAW_COLUMNS, tokens, strict=True))
        raw["asset_id"] = ids[raw["asset_id"]]          # the sub-select names the asset tag
        out.append(view_work_order(raw, serial, now))
    return out


def seed_schedules(now: datetime = NOW) -> list[dict]:
    ids = _asset_ids()
    (block,) = _blocks("maintenance_schedules_raw (")
    out = []
    for serial, tokens in enumerate(block, start=1):
        raw = dict(zip(SCHEDULE_RAW_COLUMNS, tokens, strict=True))
        next_due = now + raw["next_due_offset"]
        status = ("overdue" if next_due < now else
                  "due_soon" if next_due < now + timedelta(hours=24) else "scheduled")
        out.append({"schedule_id": serial, "asset_id": ids[raw["asset_id"]],
                    "maintenance_type": raw["maintenance_type"],
                    "frequency_days": raw["frequency_days"], "assigned_team": raw["assigned_team"],
                    "last_performed": now + raw["last_performed_offset"], "next_due": next_due,
                    "status": status})
    return out


def corp_tables(now: datetime = NOW) -> dict[str, list[dict]]:
    """The three views the mock reads, as column dicts (the reader's order:
    newest order first)."""
    wos = sorted(seed_work_orders(now), key=lambda w: (-w["created_at"].timestamp(), w["wo_number"]))
    return {"assets": seed_assets(), "work_orders": wos, "maintenance_schedules": seed_schedules(now)}


class _BridgeCursor:
    """Answers the bridge writer's asset lookup the way the database would
    (preferred asset type first, then by tag) and records its INSERT."""

    def __init__(self, assets: list[dict]):
        self._assets, self.inserts, self._row = assets, [], None

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            prefix, preferred = params[0].rstrip("%"), params[1]
            matches = sorted((a for a in self._assets if a["asset_tag"].startswith(prefix)),
                             key=lambda a: (a["asset_type"] != preferred, a["asset_tag"]))
            self._row = (matches[0]["asset_id"], matches[0]["asset_tag"]) if matches else None
        else:
            self.inserts.append((sql, params))

    def fetchone(self):
        return self._row

    def close(self):
        pass


def _insert_raw(sql: str, params: tuple) -> dict:
    """The ``work_orders_raw`` row an ``INSERT ... (cols) VALUES (...)`` writes."""
    assert f"INSERT INTO {SCHEMA}.work_orders_raw" in sql, sql
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    values = sql.split("VALUES", 1)[1].split("(", 1)[1].rsplit("ON CONFLICT", 1)[0].strip().rstrip(")")
    remaining, row = list(params), {}
    for col, token in zip(cols, (t.strip() for t in values.split(",")), strict=True):
        if token == "%s":  # nosec B105 - SQL placeholder token, not a credential
            row[col] = remaining.pop(0)
        elif token.startswith("INTERVAL"):
            row[col] = interval(token.split("'")[1])
        else:
            row[col] = token.strip("'")
    assert not remaining, (cols, values)
    return row


def bridge_order(assets: list[dict], site: str, category: str, reason: str, downtime_id: str,
                 work_order_id: int = 9001, now: datetime = NOW) -> dict:
    """One corporate work order exactly as the MES downtime bridge writes it
    (through its own entry point), as the ``work_orders`` view shows it."""
    simulator = MESSimulator.__new__(MESSimulator)   # the bridge uses class constants only
    cur = _BridgeCursor(assets)
    seed_conn = SimpleNamespace(cursor=lambda: cur, commit=lambda: None, rollback=lambda: None)
    simulator._get_seed_db_conn = lambda: seed_conn
    erp_cursor = SimpleNamespace(execute=lambda sql, params=None: None, fetchone=lambda: (site,))
    simulator._create_cmms_work_order_for_downtime(
        erp_cursor, "mes-work-order", "Line1", category, reason, downtime_id)
    ((sql, params),) = cur.inserts                    # the writer's one INSERT (a failure is swallowed)
    return view_work_order(_insert_raw(sql, params), work_order_id, now)


#: The reader's per-transaction statement timeout (``SET LOCAL`` cannot bind a
#: parameter on the server, so it is ``set_config(..., true)`` with a str).
SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, true)"


class FakeCursor:
    """A pgdriver-like cursor answering ``SELECT <cols> FROM <schema>.<table> ...``
    from ``tables``; records every statement in ``log``. A column selected
    ``AS <alias>`` is named by its alias; ``source_rows`` (the reader's
    ``count(*) OVER ()``) is the table's full row count."""

    _FROM = re.compile(r"FROM\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s")

    def __init__(self, tables: dict[str, list[dict]], log: list):
        self._tables, self._log, self._rows, self.description = tables, log, [], None

    def execute(self, query, params=None):
        self._log.append((query, params))
        if query == SET_TIMEOUT_SQL:
            return
        table = self._FROM.search(query).group(2)
        cols = [c.strip().rsplit(" AS ", 1)[-1].strip()
                for c in query.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        self.description = [(c,) for c in cols]
        limit = params[-1] if params else None
        rows = self._tables[table]
        self._rows = [tuple(len(rows) if c == "source_rows" else r.get(c) for c in cols)
                      for r in rows][:limit]

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sqlite_value(value):
    """A value SQLite can store; timestamps and dates as ISO text (UTC
    throughout, so they sort as the database sorts them)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class _SqliteCursor:
    def __init__(self, db):
        self._db, self._cur, self.description = db, None, None

    def execute(self, query, params=None):
        if query == SET_TIMEOUT_SQL:
            return
        self._cur = self._db.execute(query.replace("%s", "?"), tuple(params or ()))
        self.description = self._cur.description

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SqliteConn:
    """A pgdriver-shaped connection over an in-memory SQLite copy of
    ``tables`` (name -> column dicts): ``%s`` placeholders become ``?`` and
    ``SET`` statements do nothing, so real queries run unchanged. With
    ``schema`` the tables live in that schema, as the seed database's views
    do; without it they are unqualified, as the Daily Briefing's SQL names
    them. Text compares byte-wise (SQLite's BINARY collation)."""

    def __init__(self, tables: dict[str, list[dict]], schema: str | None = None):
        import sqlite3

        self._db = sqlite3.connect(":memory:")
        prefix = ""
        if schema:
            self._db.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
            prefix = f"{schema}."
        for name, rows in tables.items():
            assert rows, name
            cols = list(dict.fromkeys(k for r in rows for k in r))
            self._db.execute(f"CREATE TABLE {prefix}{name} ({', '.join(cols)})")
            self._db.executemany(
                f"INSERT INTO {prefix}{name} VALUES ({', '.join('?' * len(cols))})",
                [tuple(_sqlite_value(r.get(c)) for c in cols) for r in rows])

    def cursor(self):
        return _SqliteCursor(self._db)

    def set_session(self, **_kwargs):
        pass

    def rollback(self):
        pass

    def close(self):
        self._db.close()
