# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Facilitator story lookup (ADR-0085) — what THIS account's workshop story is.

Backs ``GET /api/admin/workshop/story`` (``agents/workshop_story_routes.py``):
a steward-only, READ-ONLY answer key so a facilitator can check an attendee's
answer against the data actually in the account.

* ``frozen``: the seeded B-2847 chain — the Stopped run on the story machine
  in the seeded plant MES, the ONE Released PLM item sharing its product-code
  digit core, the corporate-ERP customers with (non-cancelled) order lines
  for that item, and
  the site CMMS's open PM + emergency bearing order on the story asset.
  ``clock`` is ``None`` (nothing moves).
* ``live``: the anchored story clock computed from the ``story_*`` rows the
  live-story history wrote to ``simulation_config`` (phase math on those
  numbers — no ``erp_simulation`` import; parity with
  ``erp_simulation.condition_monitor.phase`` is pinned by test) plus its
  ``anchor_source`` (deploy | packaged | placed) and the sensor-feed state
  ``telemetry {anchor, cursor_epoch, backfill_complete}`` (ADR-0092) and the
  ``phase_timeline`` (the current loop's phase boundaries and countdowns, so
  a UI does no clock math; ``None`` plus one ``clock`` warning when the stored
  geometry does not tile the loop — the rest of the clock still renders), the failed
  batch = the most recent story-line work order with failing DIMENSIONAL
  checks whose run was SHIPPED (``shipment_items.batch_id``) or is the
  history's failed run, the customers shipped from those runs, the separate
  ``live_defect_run`` (the newest failing story-line run that is neither the
  failed batch nor the history's failed run — the RUNNING story work order of a BPFO window; ``None``
  before the first alarm and in frozen), and the deferred PM + condition-monitor /
  bearing orders of THIS era only (reported inside the ``story_history_days``
  window before the anchor, or one of the history's own work orders — the
  frozen-era seed orders on the same asset stay out; a condition order or an
  MES-bridge order the simulator completed after its loop ended is routine
  history and stays out too, in both stories, so the list carries at most one
  condition order, the current loop's). Orders the history
  superseded (status ``CAN``) are listed apart as
  ``maintenance.superseded_work_orders``, never as work (live only; the
  frozen ``maintenance`` shape is unchanged).
  The live clock also carries the reset failsafe's stored default
  (``default_anchor_epoch`` / ``default_anchor_iso`` / ``default_source``,
  all ``None`` when absent) and the auto-place POLICY ``auto_place {enabled,
  minutes}`` read from env ``STORY_AUTO_PLACE_MINUTES`` (ADR-0091 Design C:
  the backend only publishes it; the UI decides when to fire).

Every source is optional: a missing or failing source adds a ``warnings``
entry and the rest of the answer still assembles — never a 500; a list cut at
its LIMIT says so. Connections are read-only sessions with a statement
timeout, opened at most once per database per lookup, every query is
parameterized and LIMITed, and schema names come from the workshop/demo
system constants — resolved lazily per query (never at import) and
identifier-validated.
"""
from __future__ import annotations

import logging
import math
import os
import re
import string
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from . import db_routing, systems, workshop

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 5000
CONNECT_TIMEOUT_S = 5
ROW_LIMIT = 20
CUSTOMER_LIMIT = 50

# The story machine in each system (the same identities the seed and the
# simulator use; drift is pinned by tests/unit/test_workshop_story_lookup.py).
STORY_EQUIPMENT_ID = "L3-M1"            # seeded plant MES production_runs.equipment_id
STORY_MES_LINE = "LINE3"                # live MES work_orders.line
STORY_CMMS_ASSET_NUM = "1001-CMX-441"   # site CMMS assets.asset_num
FROZEN_FAILED_STATUS = "Stopped"
RELEASED_STATE = "Released"
DEFECT_CHECK_TYPE = "DIMENSIONAL"
BEARING_FAILURE_CODE = "BEARING"
CONDITION_WONUM_PATTERN = "VIB-CMX-%"
PM_WORK_TYPE = "PM"
OPEN_PM_STATUS = "WAPPR"
FROZEN_BEARING_WORK_TYPES = ["EM"]
LIVE_BEARING_WORK_TYPES = ["CM", "EM"]
CANCELLED_ORDER_STATUS = "CANCELLED"    # customer_orders.status excluded from frozen totals
# The history's condition-monitor + emergency repair orders (mirrors
# erp_simulation/story_history.py CONDITION_WONUM / REPAIR_WONUM; pinned by test).
# Its deferred PM comes from story_deferred_pm_wonum in simulation_config.
HISTORY_REPAIR_WONUMS = ("3107", "3108")
# The CMMS status the history writer sets on an order the live plant history
# supersedes (erp_simulation/story_history.py _SUPERSEDE_FROZEN_ORDERS_SQL;
# pinned by test). Live only: such rows are reported apart, never as work.
CANCELLED_WO_STATUS = "CAN"
SUPERSEDED_WO_FIELDS = ("wonum", "status", "work_type", "description", "reported_date")
# The status the simulator completes its own orders to once their loop has
# ended (MESSimulator.CONDITION_COMPLETED_STATUS; pinned by test): the
# condition monitor's VIB-CMX-<loop> orders (a number space only the monitor
# writes) and the orders the MES downtime bridge raised (IND-<hex>,
# MESSimulator.CMMS_BRIDGE_WONUM_PREFIX, reported by
# MESSimulator.CMMS_BRIDGE_REPORTED_BY). Such an order is routine history like
# the PM's last completed cycle: both keys leave it out, so neither grows with
# the age of the account. A completed IND-* order someone else reported is
# that person's maintenance record and stays listed, as do the seed's and the
# history's completed orders (1195, 3107, 3108).
COMPLETED_WO_STATUS = "COMP"
BRIDGE_WONUM_PATTERN = "IND-%"
BRIDGE_REPORTED_BY = "MES bridge"
SIMULATOR_COMPLETED_PARAMS = (COMPLETED_WO_STATUS, CONDITION_WONUM_PATTERN, BRIDGE_WONUM_PATTERN,
                              BRIDGE_REPORTED_BY)

# The loop's phases in order (the names phase() returns) and the alarm phase.
PHASE_ORDER = ("degrading", "bpfo_alarm", "repair", "healthy")
ALARM_PHASE = "bpfo_alarm"

# The simulation_config keys the live-story history writes (mirrors
# erp_simulation/story_history.py CONFIG_KEYS; parity pinned by test).
CONFIG_KEYS = (
    "story_anchor_epoch", "story_loop_seconds", "story_interval_seconds",
    "story_bpfo_alarm_step", "story_fail_step", "story_repair_steps",
    "story_history_days", "story_failed_run_number", "story_deferred_pm_wonum",
    "story_anchor_source", "story_telemetry_anchor", "story_telemetry_cursor_epoch",
)
# The reset failsafe's default anchor (mirrors story_history.DEFAULT_KEYS and
# story_replan.DEFAULT_SOURCES; parity pinned by test). READ_KEYS: what the
# lookup reads.
DEFAULT_KEYS = ("story_default_anchor", "story_default_source")
DEFAULT_SOURCES = ("deploy", "packaged")
# The second clock (ADR-0095; story_history.LIVE_ANCHOR_KEY): the anchor the
# live side follows when the alarm was moved without rewriting the history.
LIVE_ANCHOR_KEY = "story_live_anchor_epoch"
LIVE_ANCHOR_SOURCE = "placed-live"
READ_KEYS = CONFIG_KEYS + DEFAULT_KEYS + (LIVE_ANCHOR_KEY,)
# The auto-place policy (ADR-0091 Design C; stacks/_workshop_story.py): whole
# minutes, 0 / unset = off, else 5 up to one (stored) loop.
AUTO_PLACE_ENV = "STORY_AUTO_PLACE_MINUTES"
AUTO_PLACE_MIN_MINUTES = 5
# The app's loop env (story_clock.LOOP_ENV, which caps the place route's
# alarm_in_minutes). Mirrored here, not imported: story_clock pulls fastapi.
LOOP_ENV = "STORY_LOOP_SECONDS"
_ENV_LOOP_DEFAULT, _ENV_LOOP_RANGE, _ENV_LOOP_GRID = 28800, (3600, 28800), 300
_GEOMETRY_KEYS = {
    "loop_seconds": "story_loop_seconds",
    "interval_seconds": "story_interval_seconds",
    "bpfo_alarm_step": "story_bpfo_alarm_step",
    "fail_step": "story_fail_step",
    "repair_steps": "story_repair_steps",
}

# The SiteWise telemetry grid (mirrors erp_simulation.condition_monitor
# TELEMETRY_GRID_SECONDS; pinned by test): the sweep publishes one point per step.
TELEMETRY_GRID_SECONDS = 300

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class _Missing(Exception):
    """A source answered but holds nothing usable (reported as a warning)."""


def _ident(name: str) -> str:
    """A schema name safe to interpolate (constants only — validated anyway)."""
    if not _IDENT.match(name or ""):
        raise ValueError(f"unsafe schema identifier {name!r}")
    return name


def _schema_of(entries: list[dict], system_id: str) -> str:
    for entry in entries:
        if entry.get("id") == system_id:
            return entry["schema"]
    raise ValueError(f"no system {system_id!r} in the manifest")


ERP_SCHEMA = "public"   # the corporate ERP's base tables (ERP database)

# Schema roles -> resolver, evaluated LAZILY per query (never at import, so a
# manifest change can never crash server boot; a failure is one section's
# warning). ``{role}`` placeholders in the SQL_* templates below name them.
_SCHEMA_SOURCES: dict[str, Callable[[], str]] = {
    "erp": lambda: ERP_SCHEMA,
    "frozen_mes": lambda: _schema_of(workshop.WORKSHOP_SEEDED_SYSTEMS, "mes-ind"),
    "live_mes": lambda: workshop.LIVE_MES_SYSTEM["schema"],
    "cmms": lambda: workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["schema"],
    "plm": lambda: _schema_of(systems.DEMO_SYSTEMS, "plm"),
}


def schema(role: str) -> str:
    """The validated schema for *role*; an unresolvable one is a :class:`_Missing`."""
    try:
        return _ident(_SCHEMA_SOURCES[role]())
    except Exception as exc:
        raise _Missing(f"{role} schema unresolvable ({type(exc).__name__})") from None


def render(sql: str) -> str:
    """*sql* with its ``{role}`` placeholders replaced by :func:`schema`."""
    roles = {field for _t, field, _s, _c in string.Formatter().parse(sql) if field}
    return sql.format(**{role: schema(role) for role in roles})


# nosec B608 on every template below: the only interpolation is the validated
# schema of a fixed role (:func:`render`), never input; every VALUE is a bound
# parameter.
SQL_FROZEN_BATCH = """
    SELECT batch_number, work_order_ref, line_id, equipment_id, product_code, planned_qty,
           good_qty, reject_qty, scrap_qty, start_time, end_time, status, notes
    FROM {frozen_mes}.production_runs
    WHERE status = %s AND equipment_id = %s
    ORDER BY start_time DESC LIMIT 1"""  # nosec B608
SQL_RELEASED_BY_DIGITS = """
    SELECT item_id, item_name, revision, lifecycle_state, release_date
    FROM {plm}.products
    WHERE lifecycle_state = %s AND regexp_replace(item_id, '[^0-9]', '', 'g') = %s
    ORDER BY item_id LIMIT 2"""  # nosec B608
SQL_PLM_ITEM = """
    SELECT item_id, item_name, revision, lifecycle_state, release_date
    FROM {plm}.products WHERE item_id = %s LIMIT 1"""  # nosec B608
_CUSTOMER_SELECT = """
    SELECT c.customer_code, c.company_name, COUNT(DISTINCT o.order_id) AS orders,
           SUM(li.line_total) AS value
    FROM {erp}.order_line_items li
    JOIN {erp}.customer_orders o ON o.order_id = li.order_id
    JOIN {erp}.customers c ON c.customer_id = o.customer_id"""  # nosec B608
_CUSTOMER_TAIL = """
    GROUP BY c.customer_code, c.company_name
    ORDER BY value DESC, c.customer_code LIMIT %s"""
SQL_PART_CUSTOMERS = _CUSTOMER_SELECT + """
    JOIN {erp}.products p ON p.product_id = li.product_id
    WHERE p.engineering_part_number = %s AND o.status IS DISTINCT FROM %s""" + _CUSTOMER_TAIL  # nosec B608
SQL_BATCH_CUSTOMERS = _CUSTOMER_SELECT + """
    WHERE li.line_item_id IN (SELECT si.line_item_id FROM {erp}.shipment_items si
                              WHERE si.batch_id = ANY(%s))""" + _CUSTOMER_TAIL  # nosec B608
# The supplier lot of the failed batch's main material (the component it drew
# the most of) and every SHIPPED batch that drew that lot: the containment
# scope a suspect supplier lot implies. Customers use the batch query's shape.
SQL_BATCH_LOTS = """
    WITH drawn AS (
        SELECT ml.item_id, ml.lot_number, s.supplier_code, s.company_name AS supplier_name,
               ml.received_date, u.quantity_used
        FROM {erp}.production_runs pr
        JOIN {erp}.lot_production_usage u ON u.production_run_id = pr.run_id
        JOIN {erp}.material_lots ml ON ml.lot_id = u.lot_id
        JOIN {erp}.suppliers s ON s.supplier_id = ml.supplier_id
        WHERE pr.run_number = %s)
    SELECT lot_number, supplier_code, supplier_name, received_date FROM drawn
    WHERE item_id = (SELECT item_id FROM drawn GROUP BY item_id
                     ORDER BY SUM(quantity_used) DESC, item_id LIMIT 1)
    ORDER BY lot_number LIMIT %s"""  # nosec B608
SQL_LOT_SHIPPED_BATCHES = """
    SELECT DISTINCT pr.run_number
    FROM {erp}.lot_production_usage u
    JOIN {erp}.material_lots ml ON ml.lot_id = u.lot_id
    JOIN {erp}.production_runs pr ON pr.run_id = u.production_run_id
    WHERE ml.lot_number = ANY(%s)
      AND EXISTS (SELECT 1 FROM {erp}.shipment_items si WHERE si.batch_id = pr.run_number)
    ORDER BY pr.run_number LIMIT %s"""  # nosec B608
SQL_STORY_CONFIG = """
    SELECT config_key, config_value FROM {erp}.simulation_config
    WHERE config_key = ANY(%s) LIMIT %s"""  # nosec B608
_LIVE_RUN_SELECT = """
    SELECT w.work_order_number, pr.run_number, w.line, w.status, w.planned_quantity,
           w.good_quantity, w.reject_quantity, w.actual_start_time, w.actual_end_time,
           p.sku, p.product_name, p.engineering_part_number,
           SUM(q.fail_count) AS dimensional_fail_count, MAX(q.check_time) AS last_failed_check
    FROM {live_mes}.work_orders w
    JOIN {live_mes}.quality_checks q ON q.work_order_id = w.work_order_id
    JOIN {erp}.production_runs pr ON pr.run_id = w.production_run_id
    JOIN {erp}.products p ON p.product_id = w.product_id
    WHERE w.line = %s AND q.check_type = %s AND q.fail_count > 0"""  # nosec B608
_LIVE_RUN_TAIL = """
    GROUP BY w.work_order_id, w.work_order_number, pr.run_number, w.line, w.status,
             w.planned_quantity, w.good_quantity, w.reject_quantity, w.actual_start_time,
             w.actual_end_time, p.sku, p.product_name, p.engineering_part_number
    ORDER BY MAX(q.check_time) DESC LIMIT 1"""
# The failed batch: the newest failing story-line run that was SHIPPED —
# ``shipment_items.batch_id`` carries ``production_runs.run_number``
# (``ShippingSimulator._traceable_batch_id``; the history writes the same for
# its failed run) — or the history's own failed run (bound; None = none).
SQL_LIVE_BATCH = _LIVE_RUN_SELECT + """
      AND (pr.run_number = %s OR EXISTS (SELECT 1 FROM {erp}.shipment_items si
                                         WHERE si.batch_id = pr.run_number))""" + _LIVE_RUN_TAIL  # nosec B608
# The live defect run: the newest failing story-line run that is neither the
# failed batch nor the history's failed run (bound list) — the story run
# story_live starts in each BPFO window.
SQL_LIVE_DEFECT_RUN = _LIVE_RUN_SELECT + """
      AND NOT (pr.run_number = ANY(%s))""" + _LIVE_RUN_TAIL  # nosec B608
_MAINTENANCE_SELECT = """
    SELECT wonum, work_type, status, description, failure_code, reported_date,
           target_start, sched_start, actual_start, actual_finish
    FROM {cmms}.work_orders
    WHERE asset_num = %s
      AND ((work_type = %s AND status = %s)
           OR (work_type = ANY(%s) AND failure_code = %s)
           OR wonum LIKE %s)"""  # nosec B608
_MAINTENANCE_TAIL = """
    ORDER BY reported_date DESC NULLS LAST, wonum LIMIT %s"""
# Both keys: no order the simulator completed after its loop ended
# (:data:`SIMULATOR_COMPLETED_PARAMS`). ``reported_by`` is nullable, and a
# NULL reporter is "not the bridge": IS NOT DISTINCT FROM keeps the inner
# test FALSE for it (a plain ``=`` would make it UNKNOWN, and the NOT would
# drop the row).
_NOT_COMPLETED_BY_SIMULATOR = """
      AND NOT (status = %s AND (wonum LIKE %s OR (wonum LIKE %s AND reported_by IS NOT DISTINCT FROM %s)))"""
SQL_MAINTENANCE = _MAINTENANCE_SELECT + _NOT_COMPLETED_BY_SIMULATOR + _MAINTENANCE_TAIL
# Live: only THIS era's orders — reported inside the history window, or one of
# the history's own work orders (its deferred PM predates the window).
SQL_LIVE_MAINTENANCE = _MAINTENANCE_SELECT + _NOT_COMPLETED_BY_SIMULATOR + """
      AND (reported_date >= %s OR wonum = ANY(%s))""" + _MAINTENANCE_TAIL


def _plain(value):
    """JSON-plain scalar: datetimes/dates → ISO strings, Decimal → float."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _connect(schema: str):
    """ONE read-only, statement-timeout-bounded connection to *schema*'s database."""
    from .. import pgdriver  # lazy: this package stays import-pure (stdlib only)
    conn = pgdriver.connect(db_routing.demo_pg_dsn(schema), connect_timeout=CONNECT_TIMEOUT_S)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (str(STATEMENT_TIMEOUT_MS),))
    except Exception:
        conn.close()
        raise
    return conn


class ReadOnlyDb:
    """Lazily opened read-only connections, at most ONE attempt per database
    (ERP vs seed, by :func:`db_routing.is_erp_schema`) per lookup — a down
    database costs one connect timeout, not one per section."""

    def __init__(self, connect: Callable = _connect):
        self._connect = connect
        self._conns: dict = {}

    def _conn(self, schema: str):
        key = db_routing.is_erp_schema(schema)
        if key not in self._conns:
            try:
                self._conns[key] = self._connect(schema)
            except Exception as exc:  # cached so the next section fails fast
                self._conns[key] = exc
        got = self._conns[key]
        if isinstance(got, Exception):
            raise got
        return got

    def rows(self, schema: str, sql: str, params: tuple) -> list[dict]:
        with self._conn(schema).cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]

    def close(self) -> None:
        for conn in self._conns.values():
            if not isinstance(conn, Exception):
                try:
                    conn.close()
                except Exception:  # nosec B110 - best-effort close of a read-only session
                    pass
        self._conns.clear()


def _section(warnings: list, name: str, fn: Callable, *args, requires=True):
    """Run one source; on a miss or failure record a warning and return None.

    A skipped section whose name already carries a warning (the live clock:
    config miss, then the dependent clock math) adds nothing more — one
    warning per section name. A failure logs the digit-masked message
    server-side; the client only ever sees the exception type name.
    """
    if not requires:
        if not any(w.startswith(f"{name}:") for w in warnings):
            warnings.append(f"{name}: skipped (depends on a source that returned nothing)")
        return None
    try:
        return fn(*args)
    except _Missing as miss:
        warnings.append(f"{name}: {miss}")
    except Exception as exc:
        logger.warning("workshop story lookup: %s unavailable (%s: %s)", name,
                       type(exc).__name__, re.sub(r"\d", "#", str(exc)))
        warnings.append(f"{name}: source unavailable ({type(exc).__name__})")
    return None


def _rows(db, role: str, sql: str, params: tuple) -> list[dict]:
    """*sql* (a ``{role}`` template) rendered and run on *role*'s database."""
    return db.rows(schema(role), render(sql), params)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _naive_utc(epoch: float) -> datetime:
    """Naive UTC timestamp — the CMMS columns are ``TIMESTAMP`` in UTC."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


# ── Story clock (pure) ────────────────────────────────────────────────────────

def phase(step: int, bpfo_alarm_step: int, fail_step: int, repair_steps: int) -> str:
    """Story phase at loop *step* from the stored geometry (same rule as
    ``erp_simulation.condition_monitor.phase``)."""
    if step < bpfo_alarm_step:
        return "degrading"
    if step < fail_step:
        return "bpfo_alarm"
    if step < fail_step + repair_steps:
        return "repair"
    return "healthy"


def _geometry(config: dict) -> dict:
    """Positive integer loop geometry from the ``story_*`` rows (else _Missing)."""
    geo = {}
    for name, key in _GEOMETRY_KEYS.items():
        try:
            value = int(float(config[key]))
        except (KeyError, TypeError, ValueError, OverflowError):
            raise _Missing(f"config key {key} missing or malformed") from None
        if value <= 0:
            raise _Missing(f"config key {key} must be positive")
        geo[name] = value
    return geo


def _anchor(config: dict) -> float:
    """The stored HISTORY anchor: a finite epoch > 0 (the history only writes
    one). The answer key, the maintenance window and the full-placement CAS
    are on it."""
    try:
        anchor = float(config["story_anchor_epoch"])
    except (KeyError, TypeError, ValueError):
        raise _Missing("config key story_anchor_epoch missing or malformed") from None
    if not math.isfinite(anchor) or anchor <= 0:
        raise _Missing("config key story_anchor_epoch must be a finite positive epoch")
    return anchor


def _live_anchor(config: dict) -> tuple[float, Optional[str]]:
    """``(anchor, source)`` the LIVE side follows (ADR-0095): the live key
    when present and well-formed (source ``placed-live``), else the history
    anchor (source None: one clock). The phase, the alarm and the chip are on it."""
    live = _finite(config.get(LIVE_ANCHOR_KEY))
    if live is not None and live > 0:
        return live, LIVE_ANCHOR_SOURCE
    return _anchor(config), None


def _finite(value) -> Optional[float]:
    """*value* as a finite float, else ``None`` (absent or malformed)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _telemetry_status(config: dict, now: float) -> dict:
    """Whether the story's sensor feed follows the clock (``story_telemetry_*``),
    null-safe. The simulator's anchor mirror (ADR-0092) records the anchor it
    copied for the story OPC UA server and the grid point it confirmed it at;
    ``backfill_complete`` (name kept for the API) is true once that
    confirmation is within one grid step of the last grid point at or before
    *now* (``floor(now/300)*300``), i.e. the feed follows the current anchor."""
    cursor = _finite(config.get("story_telemetry_cursor_epoch"))
    last_grid = math.floor(now / TELEMETRY_GRID_SECONDS) * TELEMETRY_GRID_SECONDS
    return {"anchor": _finite(config.get("story_telemetry_anchor")),
            "cursor_epoch": None if cursor is None else int(cursor),
            "backfill_complete": None if cursor is None else cursor >= last_grid - TELEMETRY_GRID_SECONDS}


def _phase_spans(geo: dict) -> list[tuple[str, int, int]]:
    """``(phase, start_offset_s, end_offset_s)`` in loop order from the stored
    geometry; a geometry whose phases do not tile ``[0, loop)`` with non-empty
    spans is a :class:`_Missing` (no honest timeline exists for it)."""
    interval = geo["interval_seconds"]
    cuts = [0, geo["bpfo_alarm_step"] * interval, geo["fail_step"] * interval,
            (geo["fail_step"] + geo["repair_steps"]) * interval, geo["loop_seconds"]]
    if any(lo >= hi for lo, hi in zip(cuts, cuts[1:])):
        raise _Missing("story geometry phases do not tile the loop")
    return list(zip(PHASE_ORDER, cuts, cuts[1:]))


def phase_timeline(anchor: float, geo: dict, now: float) -> dict:
    """The current loop's phase boundaries and countdowns at *now*, so a UI
    renders the clock without any geometry math. Every value comes from the
    stored geometry (*geo*, :func:`_geometry`); offsets are seconds from the
    loop start, countdowns round UP (never 0 before the transition) and the
    elapsed time rounds DOWN. ``next_alarm_at`` is this loop's BPFO alarm while
    it is still ahead, else the next loop's."""
    spans = _phase_spans(geo)
    loop = geo["loop_seconds"]
    t = now - anchor
    start = anchor + int(t // loop) * loop
    offset = t % loop          # the same split story_clock's loop_step uses
    index = next((i for i, (_p, _lo, hi) in enumerate(spans) if offset < hi), len(spans) - 1)
    _name, lo, hi = spans[index]
    alarm = spans[PHASE_ORDER.index(ALARM_PHASE)][1]
    alarm_at = start + alarm + (0 if offset < alarm else loop)
    return {
        "loop_started_at": _iso(start), "loop_ends_at": _iso(start + loop),
        "phases": [{"phase": p, "start_at": _iso(start + a), "end_at": _iso(start + b),
                    "start_offset_s": a, "end_offset_s": b} for p, a, b in spans],
        "current": PHASE_ORDER[index], "seconds_into_phase": int(math.floor(offset - lo)),
        "seconds_to_next_phase": int(math.ceil(hi - offset)),
        "next_phase": PHASE_ORDER[(index + 1) % len(PHASE_ORDER)],
        "next_phase_at": _iso(start + hi),
        "next_alarm_at": _iso(alarm_at), "seconds_to_alarm": int(math.ceil(alarm_at - now)),
        "alarm_active": PHASE_ORDER[index] == ALARM_PHASE,
        "loop_fraction": round(offset / loop, 6),
    }


def _timeline_or_none(anchor: float, geo: dict, now: float,
                      warnings: Optional[list]) -> Optional[dict]:
    """:func:`phase_timeline`, or ``None`` plus ONE ``clock:`` warning when the
    stored geometry does not tile the loop — the rest of the clock (the tab
    and the chip, the UI path for re-placing the clock) still renders."""
    try:
        return phase_timeline(anchor, geo, now)
    except _Missing as miss:
        if warnings is not None:
            warnings.append(f"clock: {miss}; phase_timeline withheld (re-place the story clock)")
        return None


def _legacy_phase(timeline: Optional[dict], step: int, geo: dict) -> tuple[str, str]:
    """``(phase, next_phase)``: the timeline's own (the SAME spans, so the two
    cannot drift); only a non-tiling geometry falls back to :func:`phase`."""
    if timeline is not None:
        return timeline["current"], timeline["next_phase"]
    current = phase(step, geo["bpfo_alarm_step"], geo["fail_step"], geo["repair_steps"])
    return current, PHASE_ORDER[(PHASE_ORDER.index(current) + 1) % len(PHASE_ORDER)]


def parse_default(config: dict) -> Optional[tuple[int, str]]:
    """Mirror of ``story_replan.parse_default``: the stored ``(anchor, source)``
    or ``None`` when absent or malformed."""
    source = config.get(DEFAULT_KEYS[1])
    anchor = _finite(config.get(DEFAULT_KEYS[0]))
    if source not in DEFAULT_SOURCES or anchor is None or anchor <= 0:
        return None
    return int(anchor), source


def env_loop_seconds(raw: Optional[str]) -> Optional[int]:
    """``story_clock.loop_seconds_from`` semantics without raising: unset /
    blank -> the 8 h default; a multiple of 300 in [3600, 28800] -> itself;
    anything else -> ``None`` (unusable, so it bounds nothing here)."""
    if raw is None or not raw.strip():
        return _ENV_LOOP_DEFAULT
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text, 10)
    low, high = _ENV_LOOP_RANGE
    return value if low <= value <= high and value % _ENV_LOOP_GRID == 0 else None


def auto_place_policy(raw: Optional[str], loop_seconds: int, warnings: Optional[list] = None) -> dict:
    """``{enabled, minutes}`` from the env text: unset / blank / ``0`` is off;
    a whole number in 5..min(stored loop, env loop)/60 is on (the place route
    caps ``alarm_in_minutes`` at the env loop); anything else is off plus ONE
    ``clock`` warning (a bad env never breaks the answer key)."""
    text = (raw or "").strip()
    off = {"enabled": False, "minutes": 0}
    if not text or text == "0":
        return off
    high = min(loop_seconds, env_loop_seconds(os.environ.get(LOOP_ENV)) or loop_seconds) // 60
    if text.isascii() and text.isdigit() and AUTO_PLACE_MIN_MINUTES <= int(text) <= high:
        return {"enabled": True, "minutes": int(text)}
    if warnings is not None:
        warnings.append(f"clock: {AUTO_PLACE_ENV}={text!r} ignored (0 or {AUTO_PLACE_MIN_MINUTES}..{high} "
                        f"minutes); auto-place is off")
    return off


def _failsafe(config: dict, loop_seconds: int, warnings: Optional[list]) -> dict:
    """The clock's Design C fields: the stored reset default and the auto-place policy."""
    default = parse_default(config)
    anchor, source = default if default is not None else (None, None)
    return {"default_anchor_epoch": anchor, "default_anchor_iso": None if anchor is None else _iso(anchor),
            "default_source": source,
            "auto_place": auto_place_policy(os.environ.get(AUTO_PLACE_ENV), loop_seconds, warnings)}


def story_clock(config: dict, now: float, warnings: Optional[list] = None) -> dict:
    """Where the anchored loop is at *now* (epoch seconds), with the anchor's
    provenance (``anchor_source``), the telemetry sweep state and the
    :func:`phase_timeline` (``None`` + one *warnings* entry for a non-tiling
    stored geometry). The phase, the alarm and the timeline are on the LIVE
    anchor (``live_anchor_epoch``, ADR-0095); ``anchor_epoch`` stays the
    history anchor the answer key and the full-placement CAS are on."""
    history = _anchor(config)
    anchor, live_source = _live_anchor(config)
    geo = _geometry(config)
    loop, interval = geo["loop_seconds"], geo["interval_seconds"]
    t = now - anchor
    loop_number = int(t // loop)
    step = int((t % loop) // interval)
    start = anchor + loop_number * loop
    at = {
        "bpfo_alarm": start + geo["bpfo_alarm_step"] * interval,
        "repair": start + geo["fail_step"] * interval,
        "healthy": start + (geo["fail_step"] + geo["repair_steps"]) * interval,
        "degrading": start + loop,
    }
    timeline = _timeline_or_none(anchor, geo, now, warnings)
    current, following = _legacy_phase(timeline, step, geo)
    next_at = timeline["next_phase_at"] if timeline else _iso(at[following])
    return {
        "anchor_epoch": history, "anchor_at": _iso(history), "now": _iso(now),
        "live_anchor_epoch": anchor, "live_anchor_at": _iso(anchor), "live_anchor_source": live_source,
        **geo, "loop_number": loop_number, "loop_step": step, "phase": current,
        "loop_started_at": _iso(start),
        "bpfo_alarm_at": _iso(at["bpfo_alarm"]), "bearing_fail_at": _iso(at["repair"]),
        "repair_ends_at": _iso(at["healthy"]), "next_loop_at": _iso(at["degrading"]),
        "next_phase": following, "next_phase_at": next_at,
        "anchor_source": config.get("story_anchor_source") or None,
        "telemetry": _telemetry_status(config, now),
        "phase_timeline": timeline,
        **_failsafe(config, loop, warnings),
    }


def live_window(config: dict) -> tuple[datetime, list[str]]:
    """This era's maintenance window: (reported-since, the history's own wonums).

    Since = anchor - ``story_history_days`` days; the wonums are the history's
    deferred PM (``story_deferred_pm_wonum``) and repair orders — the PM is
    reported well before the window, so it is admitted by number.
    """
    anchor = _anchor(config)
    try:
        days = int(float(config["story_history_days"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _Missing("config key story_history_days missing or malformed") from None
    if days <= 0:
        raise _Missing("config key story_history_days must be positive")
    pm = config.get("story_deferred_pm_wonum")
    return _naive_utc(anchor - days * 86400), [w for w in (pm, *HISTORY_REPAIR_WONUMS) if w]


# ── Sources ───────────────────────────────────────────────────────────────────

def _one(rows: list[dict], what: str) -> dict:
    if not rows:
        raise _Missing(f"no {what} found")
    return rows[0]


def _frozen_batch(db) -> dict:
    row = _one(_rows(db, "frozen_mes", SQL_FROZEN_BATCH,
                     (FROZEN_FAILED_STATUS, STORY_EQUIPMENT_ID)), "stopped run on the story machine")
    return {"source_schema": schema("frozen_mes"), **row}


def _released_part_for(db, product_code: str) -> dict:
    digits = re.sub(r"\D", "", product_code or "")
    if not digits:
        raise _Missing("failed batch carries no product code")
    rows = _rows(db, "plm", SQL_RELEASED_BY_DIGITS, (RELEASED_STATE, digits))
    if len(rows) != 1:
        raise _Missing(f"expected exactly one {RELEASED_STATE} item for product code "
                       f"{product_code}, found {len(rows)}")
    return {"source_schema": schema("plm"), **rows[0]}


def _plm_item(db, item_id: Optional[str]) -> dict:
    if not item_id:
        raise _Missing("failed batch's product has no engineering part number")
    row = _one(_rows(db, "plm", SQL_PLM_ITEM, (item_id,)), f"PLM item {item_id}")
    return {"source_schema": schema("plm"), **row}


def _truncation(warnings: list, name: str, rows: list, limit: int) -> None:
    if len(rows) >= limit:
        warnings.append(f"{name}: list truncated at {limit}")


def _customers(db, warnings: list, sql: str, params: tuple) -> list[dict]:
    rows = _rows(db, "erp", sql, (*params, CUSTOMER_LIMIT))
    if not rows:
        # A real (empty) answer, but a facilitator must not read it as "no impact":
        # a fresh account's simulator may not have booked matching orders yet.
        warnings.append("customers: no matching orders in the ERP yet")
    _truncation(warnings, "customers", rows, CUSTOMER_LIMIT)
    return rows


def _suspect_lot(db, warnings: list, run_number: Optional[str]) -> dict:
    """The failed batch's main-material lot, the shipped batches that drew it,
    their customers and value: the containment scope of a suspect lot."""
    lots = _rows(db, "erp", SQL_BATCH_LOTS, (run_number, ROW_LIMIT))
    if not lots:
        raise _Missing(f"no supplier lot recorded for {run_number}")
    numbers = [r["lot_number"] for r in lots]
    batches = [r["run_number"] for r in _rows(db, "erp", SQL_LOT_SHIPPED_BATCHES, (numbers, ROW_LIMIT))]
    _truncation(warnings, "suspect_lot batches", batches, ROW_LIMIT)
    customers = _customers(db, warnings, SQL_BATCH_CUSTOMERS, (batches,)) if batches else []
    total = round(sum(float(c.get("value") or 0) for c in customers), 2)
    return {"source_schema": schema("erp"), "lots": lots, "shipped_batches": batches,
            "customers": customers, "total_value": total}


def stored_default(db=None) -> Optional[tuple[int, str]]:
    """The reset failsafe's stored ``(anchor, source)`` (:func:`parse_default`),
    or ``None``; reads only :data:`DEFAULT_KEYS`. *db* is a test seam (the
    placement dry run's Lambda-free read; a failure raises to the caller)."""
    own = db is None
    db = ReadOnlyDb() if own else db
    try:
        rows = _rows(db, "erp", SQL_STORY_CONFIG, (list(DEFAULT_KEYS), len(DEFAULT_KEYS)))
    finally:
        if own:
            db.close()
    return parse_default({r["config_key"]: r["config_value"] for r in rows})


def _story_config(db) -> dict:
    rows = _rows(db, "erp", SQL_STORY_CONFIG, (list(READ_KEYS), len(READ_KEYS)))
    config = {r["config_key"]: r["config_value"] for r in rows}
    if "story_anchor_epoch" not in config:
        raise _Missing("no story_anchor_epoch in simulation_config (the live-story "
                       "history has not run)")
    return config


def _live_run_entry(row: dict, history_run: Optional[str]) -> dict:
    return {"source_schema": schema("live_mes"), **row, "history_run_number": history_run,
            "is_history_run": bool(history_run) and row.get("run_number") == history_run}


def _live_batch(db, history_run: Optional[str]) -> dict:
    row = _one(_rows(db, "live_mes", SQL_LIVE_BATCH,
                     (STORY_MES_LINE, DEFECT_CHECK_TYPE, history_run)),
               f"shipped {STORY_MES_LINE} work order with failing {DEFECT_CHECK_TYPE} checks")
    return _live_run_entry(row, history_run)


def _live_defect_run(db, history_run: Optional[str], failed_run: Optional[str]) -> Optional[dict]:
    """The newest failing story-line run that is neither the failed batch nor
    the history's failed run, or ``None`` — none yet (before the first BPFO
    alarm) is not a miss. Excluding the history run too matters once a newer
    shipped run is the failed batch: the history run is then still a failing
    LINE3 run, but it is not live."""
    excluded = sorted({r for r in (failed_run, history_run) if r})
    rows = _rows(db, "live_mes", SQL_LIVE_DEFECT_RUN,
                 (STORY_MES_LINE, DEFECT_CHECK_TYPE, excluded))
    return _live_run_entry(rows[0], history_run) if rows else None


def _maintenance_rows(db, warnings: list, bearing_types: list, wonum_pattern: Optional[str],
                      window: Optional[tuple]) -> list[dict]:
    """Frozen (*window* None): the seed predicate. Live: the same predicate
    restricted to :func:`live_window`. Neither lists an order the simulator
    completed after its loop ended."""
    params = (STORY_CMMS_ASSET_NUM, PM_WORK_TYPE, OPEN_PM_STATUS, bearing_types,
              BEARING_FAILURE_CODE, wonum_pattern, *SIMULATOR_COMPLETED_PARAMS)
    if window is None:
        rows = _rows(db, "cmms", SQL_MAINTENANCE, (*params, ROW_LIMIT))
    else:
        rows = _rows(db, "cmms", SQL_LIVE_MAINTENANCE, (*params, *window, ROW_LIMIT))
    _truncation(warnings, "maintenance", rows, ROW_LIMIT)
    return rows


def _maintenance(db, warnings: list, bearing_types: list, wonum_pattern: Optional[str],
                 deferred_pm: Optional[str], window: Optional[tuple] = None) -> dict:
    rows = _maintenance_rows(db, warnings, bearing_types, wonum_pattern, window)
    if not deferred_pm:
        open_pms = [r["wonum"] for r in rows
                    if r.get("work_type") == PM_WORK_TYPE and r.get("status") == OPEN_PM_STATUS]
        # Rows are newest-first, so [-1] is the OLDEST open PM: the original
        # deferral, not a later re-raised PM on the same schedule.
        deferred_pm = open_pms[-1] if open_pms else None
    return {"source_schema": schema("cmms"), "asset_num": STORY_CMMS_ASSET_NUM,
            "deferred_pm_wonum": deferred_pm, "work_orders": rows}


# ── Assembly ──────────────────────────────────────────────────────────────────

def _body(clock, batch, part, maintenance, customers, live_defect_run=None) -> dict:
    total = None if customers is None else round(sum(float(c.get("value") or 0) for c in customers), 2)
    return {"clock": clock, "failed_batch": batch, "part": part, "maintenance": maintenance,
            "customers": customers or [], "total_value": total,
            "live_defect_run": live_defect_run}


def _frozen(db, now: float, warnings: list) -> dict:
    batch = _section(warnings, "failed_batch", _frozen_batch, db)
    part = _section(warnings, "part", _released_part_for, db,
                    (batch or {}).get("product_code"), requires=batch)
    customers = _section(warnings, "customers", _customers, db, warnings, SQL_PART_CUSTOMERS,
                         ((part or {}).get("item_id"), CANCELLED_ORDER_STATUS), requires=part)
    maintenance = _section(warnings, "maintenance", _maintenance, db, warnings,
                           FROZEN_BEARING_WORK_TYPES, None, None)
    return _body(None, batch, part, maintenance, customers)


def _split_superseded(maintenance: dict) -> dict:
    """Move :data:`CANCELLED_WO_STATUS` rows out of ``work_orders`` into
    ``superseded_work_orders`` (:data:`SUPERSEDED_WO_FIELDS` only)."""
    rows = maintenance["work_orders"]
    return {**maintenance,
            "work_orders": [r for r in rows if r.get("status") != CANCELLED_WO_STATUS],
            "superseded_work_orders": [{k: r.get(k) for k in SUPERSEDED_WO_FIELDS}
                                       for r in rows if r.get("status") == CANCELLED_WO_STATUS]}


def _live_maintenance(db, warnings: list, config: Optional[dict]) -> dict:
    """Live maintenance; without a story window, unrestricted and SAID so.
    Superseded (cancelled) orders are reported apart (:func:`_split_superseded`)."""
    try:
        window = live_window(config or {})
    except _Missing as miss:
        warnings.append(f"maintenance: {miss}; list not restricted to the live era")
        window = (_naive_utc(0), [])
    return _split_superseded(_maintenance(
        db, warnings, LIVE_BEARING_WORK_TYPES, CONDITION_WONUM_PATTERN,
        (config or {}).get("story_deferred_pm_wonum"), window))


def _live(db, now: float, warnings: list) -> dict:
    config = _section(warnings, "clock", _story_config, db)
    clock = _section(warnings, "clock", story_clock, config, now, warnings, requires=config)
    history_run = (config or {}).get("story_failed_run_number")
    batch = _section(warnings, "failed_batch", _live_batch, db, history_run)
    live_run = _section(warnings, "live_defect_run", _live_defect_run, db, history_run,
                        (batch or {}).get("run_number"))
    part = _section(warnings, "part", _plm_item, db,
                    (batch or {}).get("engineering_part_number"), requires=batch)
    runs = sorted({r for r in ((batch or {}).get("run_number"), history_run) if r})
    customers = _section(warnings, "customers", _customers, db, warnings,
                         SQL_BATCH_CUSTOMERS, (runs,), requires=runs)
    lot = _section(warnings, "suspect_lot", _suspect_lot, db, warnings,
                   (batch or {}).get("run_number"), requires=batch)
    maintenance = _section(warnings, "maintenance", _live_maintenance, db, warnings, config)
    return {**_body(clock, batch, part, maintenance, customers, live_run), "suspect_lot": lot}


_BUILDERS = {"frozen": _frozen, "live": _live}


def lookup(story_name: str, db=None, now: Optional[float] = None) -> dict:
    """The facilitator answer key for *story_name* (``frozen`` | ``live``).

    Shape: ``{story, generated_at, clock|None, failed_batch, part, maintenance,
    customers[], total_value, live_defect_run|None, warnings[]}``, and for the live story
    ``suspect_lot``: ``{lots[], shipped_batches[], customers[], total_value}`` (None when the
    batch carries no lot). Read-only; never raises for a
    missing or failing source (see ``warnings``). *db* (anything with
    ``rows(schema, sql, params)``) and *now* are test seams.
    """
    if story_name not in _BUILDERS:
        raise ValueError(f"unknown workshop story {story_name!r}")
    now = time.time() if now is None else now
    own = db is None
    db = ReadOnlyDb() if own else db
    warnings: list[str] = []
    try:
        body = _BUILDERS[story_name](db, now, warnings)
    finally:
        if own:
            db.close()
    return {"story": story_name, "generated_at": _iso(now), **body, "warnings": warnings}
