# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Placeable story clock: re-plan the live story on a new anchor (ADR-0089).

The stored ``simulation_config.story_anchor_epoch`` is the ONE story clock
(ADR-0088). Moving it is a RETIRE + REWRITE, never an in-place date shift
(run numbers embed their dates): :func:`replan`

1. takes ``pg_advisory_xact_lock`` in ONE ERP transaction, re-reads the
   stored anchor and checks it against the caller's ``expected_anchor``
   (compare-and-swap: :class:`StaleAnchor`);
2. deletes EXACTLY the rows the deterministic ``story_history.plan(old)``
   wrote plus the story runs of the old anchor's loops (:func:`retire_era`,
   enumerated from the plan and ``story_live.story_work_order``; there is no
   marker column, and a simulator-owned row never matches). When the stored
   loop geometry (:data:`GEOMETRY_KEYS`) is not the one this process's
   ``condition_monitor`` read (a packaged ``LoopSeconds`` change, ADR-0091),
   ``plan(old)`` cannot rebuild the old numbers, so it deletes the story
   number NAMESPACE instead (:func:`retire_namespace`) and always rewrites,
   the geometry config keys included;
3. writes ``plan(new)`` through the existing insert-only writer, upserts the
   story config (anchor, provenance, failed run) and resets the telemetry
   sweep state so the minute step republishes the whole history;
4. then, in the SEED transaction (ERP first, ADR-0085 order), under the
   same advisory lock on the seed database and after confirming the
   committed ERP anchor is still this placement's (else :class:`StaleAnchor`:
   a later placement owns the seed side): purges every
   story-asset condition order reported before ``now`` that is not this
   era's, re-applies the history CMMS rows (``story_history._write_seed``:
   the upsert moves 3101/3107/3108, supersede / prior-era marks are
   idempotent) and catches up ``VIB-CMX-<loop>`` for every new-era loop whose
   alarm is at or before ``now``.

A re-plan onto the STORED anchor retires and writes nothing: it only resets
the telemetry sweep and re-applies the (idempotent) seed side, so a retry
after a seed failure converges.

The anchor is normalised first (:func:`normalize_anchor`): congruent with the
telemetry grid (``condition_monitor.snap_anchor``) and inside the last loop
before ``now``, because the history must end before now and SiteWise rejects
future values. The loop is periodic, so the phase is preserved.

Two clocks (ADR-0095): :func:`replan_live` moves ONLY the live side (the
anchor the condition monitor, the story step, the sensor feed and the phase
chip follow) by writing ``simulation_config.story_live_anchor_epoch`` and
retiring just the old live era's story runs and condition orders; the
history, its failed batch and every run number stay as written. The one
invariant is that the new loop-0 alarm falls after the history ends
(:func:`live_alarm_allowed`). Every full :func:`replan` clears the live key,
so the two clocks become one again.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from . import condition_monitor as cm
from . import story_history as sh

LOOP_SECONDS = cm.TOTAL_SECONDS
DEFAULT_DAYS = 3
# pg_advisory_xact_lock key: b"STORYCLK" as a signed 64-bit integer.
LOCK_KEY = int.from_bytes(b"STORYCLK", "big", signed=True)
# The per-cycle writers (story_live.apply_cycle on the ERP, the MES condition
# monitor on the seed) take it SHARED; replan takes it EXCLUSIVE on both.
SHARED_LOCK_SQL = "SELECT pg_advisory_xact_lock_shared(%s)"
ANCHOR_KEY = "story_anchor_epoch"
# The second clock (ADR-0095): the live side's anchor when the alarm was moved
# WITHOUT rewriting the history (:func:`replan_live`); absent = one clock.
# Every full re-plan clears it, so the two clocks become one again.
LIVE_ANCHOR_KEY = sh.LIVE_ANCHOR_KEY
DAYS_KEY = "story_history_days"
SOURCE_KEY = "story_anchor_source"
TELEMETRY_KEYS = ("story_telemetry_anchor", "story_telemetry_cursor_epoch")
# The stored loop geometry (story_history._config). When it differs from the
# geometry condition_monitor read at import, plan(old) would rebuild the OLD
# era with the NEW geometry, so the retire goes by namespace instead.
GEOMETRY_KEYS = ("story_loop_seconds", "story_interval_seconds", "story_bpfo_alarm_step", "story_fail_step", "story_repair_steps")
# The story-owned number spaces (ADR-0090: simulator RUN 0-39999, WO 40000-79999, ORD
# below 40000000, SHIP 40000000-79999999): story_history's 9xxxx runs and 9xxxxxxx
# orders, story_live's 8xxxx story runs.
HISTORY_RUN_PATTERN = r"^RUN-[0-9]{8}-9[0-9]{4}$"
STORY_RUN_PATTERN = r"^RUN-[0-9]{8}-8[0-9]{4}$"
HISTORY_ORDER_PATTERN = r"^ORD-[0-9]{8}-9[0-9]{7}$"
# A placement cannot rewrite what the story OPC UA server already sent to
# SiteWise, so the server CATCHES UP instead of jumping (ADR-0092 amendment
# 2026-10-02): from CATCH_UP_LEAD_SECONDS after the placement (time for the
# mirror and the server's poll to carry the new value) it plays from the
# previous anchor's row to the new anchor's next alarm row, reaching it at
# that alarm. Nothing in the ERP or the CMMS reads the sensor before the
# alarm, so the records are unaffected.
CATCH_UP_KEY = sh.CATCH_UP_KEY
CATCH_UP_LEAD_SECONDS = 120

# Loop steps at which each story phase starts, and the step after it ends.
PHASE_STEPS: Dict[str, Tuple[int, int]] = {
    "degrading": (0, cm.BPFO_ALARM_STEP),
    "bpfo_alarm": (cm.BPFO_ALARM_STEP, cm.BEARING_FAIL_STEP),
    "repair": (cm.BEARING_FAIL_STEP, cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS),
    "healthy": (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS, cm.NUM_STEPS),
}


class StaleAnchor(RuntimeError):
    """The stored anchor is not the one the caller placed against (CAS)."""


class LiveClockRefused(ValueError):
    """A live-only move (:func:`replan_live`) cannot be made: no story clock
    is stored, the stored loop geometry is not this process's, or the new
    alarm would land inside the written history."""


# ---------------------------------------------------------------------------
# Pure anchor arithmetic
# ---------------------------------------------------------------------------

def normalize_anchor(reference_epoch: float, now: float) -> int:
    """The congruent anchor with ``reference_epoch``'s phase inside
    ``(now - LOOP_SECONDS, now]`` (ADR-0089 D12): snap to the grid congruence (nearest),
    then shift by whole loops."""
    snapped = cm.snap_anchor(reference_epoch, "nearest")
    return snapped - math.ceil((snapped - now) / LOOP_SECONDS) * LOOP_SECONDS


def loops_shifted(reference_epoch: float, now: float) -> int:
    """Whole loops :func:`normalize_anchor` moved the reference BACK
    (negative: forward, for a reference earlier than the last loop)."""
    snapped = cm.snap_anchor(reference_epoch, "nearest")
    return (snapped - normalize_anchor(reference_epoch, now)) // LOOP_SECONDS


def alarm_to_anchor(alarm_epoch: float) -> int:
    """The congruent reference anchor whose loop-0 BPFO alarm is
    ``alarm_epoch`` rounded to the nearest telemetry grid point (NOT
    normalised: pass it through :func:`normalize_anchor` / :func:`replan`)."""
    return cm.snap_anchor(alarm_epoch - cm.ALARM_OFFSET_SECONDS, "nearest")


def phase_offset_to_anchor(phase: str, elapsed_minutes: float, now: float) -> int:
    """The normalised anchor that puts ``now`` ``elapsed_minutes`` into
    ``phase`` (``degrading|bpfo_alarm|repair|healthy``), to the grid (+-150 s).
    An unknown phase or an elapsed time outside the phase raises ValueError."""
    if phase not in PHASE_STEPS:
        raise ValueError(f"phase must be one of {tuple(PHASE_STEPS)}, got {phase!r}")
    start, end = (step * cm.INTERVAL_SEC for step in PHASE_STEPS[phase])
    offset = float(elapsed_minutes) * 60
    if not 0 <= offset < end - start:
        raise ValueError(f"elapsed_minutes must be in [0, {(end - start) / 60:g}) for {phase!r}, "
                         f"got {elapsed_minutes!r}")
    return normalize_anchor(now - start - offset, now)


def era_alarms(anchor: float, now: float) -> List[Tuple[int, float, float]]:
    """``(loop, alarm_epoch, fail_epoch)`` of every loop >= 0 of ``anchor``
    whose BPFO alarm is at or before ``now``."""
    out = []
    for loop in range(0, max(cm.loop_number(now - anchor), -1) + 1):
        start = anchor + loop * LOOP_SECONDS
        alarm = start + cm.ALARM_OFFSET_SECONDS
        if alarm <= now:
            out.append((loop, alarm, cm.fail_epoch(start, anchor)))
    return out


def story_run_numbers(anchor: float, now: float) -> List[Tuple[str, str]]:
    """``(run_number, work_order_number)`` of the story run the minute step
    would have started in each loop of ``anchor`` whose alarm is <= ``now``
    (the SAME numbering function the step uses)."""
    from . import story_live  # lazy: story_live imports story_history
    return [(wo.run_number, wo.work_order_number)
            for wo in (story_live.story_work_order(alarm, anchor) for _l, alarm, _f in era_alarms(anchor, now))]


# ---------------------------------------------------------------------------
# Retire: exactly the rows plan(old) and the old era's story step wrote
# ---------------------------------------------------------------------------

# Every statement below is a constant; every value is a bound parameter.
_HISTORY_RUN_IDS_SQL = """
    SELECT pr.run_id FROM production_runs pr
    JOIN products p ON p.product_id = pr.product_id
    JOIN unnest(%s::varchar[], %s::varchar[], %s::varchar[], %s::timestamp[])
        AS m(run_number, sku, line, started)
      ON pr.run_number = m.run_number AND p.sku = m.sku AND pr.line = m.line
     AND pr.actual_start_time = m.started
    WHERE pr.site = %s
"""
# A story run matches on its run number AND its MES work-order number (the
# step inserts both, one statement), both on the story site / line / part,
# with no order: a simulator run whose random number lands in the 8NNNN
# space has its own work-order number and never matches.
_STORY_RUN_IDS_SQL = """
    SELECT DISTINCT pr.run_id FROM production_runs pr
    JOIN products p ON p.product_id = pr.product_id
    JOIN mes_work_orders w ON w.production_run_id = pr.run_id
    JOIN unnest(%s::varchar[], %s::varchar[]) AS m(run_number, work_order_number)
      ON pr.run_number = m.run_number AND w.work_order_number = m.work_order_number
    WHERE pr.order_id IS NULL AND pr.site = %s AND pr.line = %s AND p.sku = %s
      AND w.site = pr.site AND w.line = pr.line AND w.product_id = pr.product_id
"""
_ORDER_IDS_SQL = """
    SELECT o.order_id FROM customer_orders o
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN unnest(%s::varchar[], %s::varchar[], %s::timestamp[]) AS m(order_number, customer_code, ordered)
      ON o.order_number = m.order_number AND c.customer_code = m.customer_code
     AND o.order_date = m.ordered
"""
_WORK_ORDER_IDS_SQL = "SELECT work_order_id FROM mes_work_orders WHERE production_run_id = ANY(%s::uuid[])"
_SHIPMENT_IDS_SQL = "SELECT shipment_id FROM shipments WHERE order_id = ANY(%s::uuid[])"
_INVOICE_IDS_SQL = ("SELECT invoice_id FROM invoices "
                    "WHERE order_id = ANY(%s::uuid[]) OR shipment_id = ANY(%s::uuid[])")

# Geometry-agnostic scope (:func:`retire_namespace`): a story-owned number on
# the story site AND its paired number (the writers issue both from one
# suffix), so a pre-ADR-0090 random simulator number in the same space,
# which has an unrelated paired number, never matches.
_NAMESPACE_HISTORY_RUN_IDS_SQL = """
    SELECT DISTINCT pr.run_id FROM production_runs pr
    JOIN mes_work_orders w ON w.production_run_id = pr.run_id
    WHERE pr.site = %s AND pr.run_number ~ %s AND w.work_order_number = 'WO-' || substr(pr.run_number, 5)
"""
_NAMESPACE_STORY_RUN_IDS_SQL = """
    SELECT DISTINCT pr.run_id FROM production_runs pr
    JOIN products p ON p.product_id = pr.product_id
    JOIN mes_work_orders w ON w.production_run_id = pr.run_id
    WHERE pr.run_number ~ %s AND w.work_order_number = 'WO-' || substr(pr.run_number, 5)
      AND pr.order_id IS NULL AND pr.site = %s AND pr.line = %s AND p.sku = %s
      AND w.site = pr.site AND w.line = pr.line AND w.product_id = pr.product_id
"""
_NAMESPACE_ORDER_IDS_SQL = """
    SELECT DISTINCT o.order_id FROM customer_orders o
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN shipments s ON s.order_id = o.order_id
    WHERE o.order_number ~ %s AND c.customer_code = ANY(%s)
      AND split_part(s.shipment_number, '-', 3) = split_part(o.order_number, '-', 3)
"""

# Children first (FK order). (count key, id set, statement). The invoices /
# payments are the finance simulator's rows OF the retired shipments: they
# cannot outlive their order (FK) and are counted separately.
_RETIRE_STEPS: Tuple[Tuple[str, str, str], ...] = (
    ("payments", "invoices", "DELETE FROM payments WHERE invoice_id = ANY(%s::uuid[])"),
    ("invoice_line_items", "invoices", "DELETE FROM invoice_line_items WHERE invoice_id = ANY(%s::uuid[])"),
    ("invoices", "invoices", "DELETE FROM invoices WHERE invoice_id = ANY(%s::uuid[])"),
    ("shipment_items", "shipments", "DELETE FROM shipment_items WHERE shipment_id = ANY(%s::uuid[])"),
    ("shipments", "shipments", "DELETE FROM shipments WHERE shipment_id = ANY(%s::uuid[])"),
    ("order_lines", "orders", "DELETE FROM order_line_items WHERE order_id = ANY(%s::uuid[])"),
    ("orders", "orders", "DELETE FROM customer_orders WHERE order_id = ANY(%s::uuid[])"),
    ("quality_checks", "work_orders", "DELETE FROM mes_quality_checks WHERE work_order_id = ANY(%s::uuid[])"),
    ("production_events", "work_orders",
     "DELETE FROM mes_production_events WHERE work_order_id = ANY(%s::uuid[])"),
    ("downtime_events", "work_orders", "DELETE FROM mes_downtime_events WHERE work_order_id = ANY(%s::uuid[])"),
    ("material_consumption", "work_orders",
     "DELETE FROM mes_material_consumption WHERE work_order_id = ANY(%s::uuid[])"),
    ("lot_mes_tracking", "work_orders", "DELETE FROM lot_mes_tracking WHERE work_order_id = ANY(%s::uuid[])"),
    ("work_orders", "work_orders", "DELETE FROM mes_work_orders WHERE work_order_id = ANY(%s::uuid[])"),
    ("lot_production_usage", "runs", "DELETE FROM lot_production_usage WHERE production_run_id = ANY(%s::uuid[])"),
    ("production_materials", "runs", "DELETE FROM production_materials WHERE run_id = ANY(%s::uuid[])"),
    ("runs", "history_runs", "DELETE FROM production_runs WHERE run_id = ANY(%s::uuid[])"),
    ("story_runs", "story_runs", "DELETE FROM production_runs WHERE run_id = ANY(%s::uuid[])"),
)
RETIRE_COUNT_KEYS = tuple(key for key, _ids, _sql in _RETIRE_STEPS)


def _ids(cur, sql: str, params: tuple) -> List[str]:
    cur.execute(sql, params)
    return [str(row[0]) for row in cur.fetchall()]


def _history_run_ids(cur, sp: sh.StoryPlan) -> List[str]:
    runs = (*sp.runs, sp.failed_run)
    return _ids(cur, _HISTORY_RUN_IDS_SQL, (
        [r.run_number for r in runs], [r.sku for r in runs], [r.line for r in runs],
        [r.start for r in runs], sh.SITE))


def _order_ids(cur, sp: sh.StoryPlan) -> List[str]:
    orders = (*sp.orders, *sp.run_orders)
    return _ids(cur, _ORDER_IDS_SQL, (
        [o.order_number for o in orders], [o.customer_code for o in orders],
        [o.order_date for o in orders]))


def _retire_scope(cur, sp: sh.StoryPlan, story_runs: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    """The ids of every row to retire, by id set (see :data:`_RETIRE_STEPS`);
    ``story_runs`` are ``(run_number, work_order_number)`` pairs."""
    story = _ids(cur, _STORY_RUN_IDS_SQL, (
        [run for run, _wo in story_runs], [wo for _run, wo in story_runs],
        sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU)) if story_runs else []
    return _scope_of(cur, _history_run_ids(cur, sp), story, _order_ids(cur, sp))


def _namespace_scope(cur) -> Dict[str, List[str]]:
    """:func:`_retire_scope` by number space instead of by plan."""
    history = _ids(cur, _NAMESPACE_HISTORY_RUN_IDS_SQL, (sh.SITE, HISTORY_RUN_PATTERN))
    story = _ids(cur, _NAMESPACE_STORY_RUN_IDS_SQL, (
        STORY_RUN_PATTERN, sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU))
    orders = _ids(cur, _NAMESPACE_ORDER_IDS_SQL, (HISTORY_ORDER_PATTERN, list(sh.SITE_CUSTOMER_CODES)))
    return _scope_of(cur, history, story, orders)


def _scope_of(cur, history: List[str], story: List[str], orders: List[str]) -> Dict[str, List[str]]:
    """Every id set of :data:`_RETIRE_STEPS` from the run and order ids."""
    runs = history + story
    shipments = _ids(cur, _SHIPMENT_IDS_SQL, (orders,))
    return {
        "history_runs": history, "story_runs": story, "runs": runs,
        "work_orders": _ids(cur, _WORK_ORDER_IDS_SQL, (runs,)),
        "orders": orders, "shipments": shipments,
        "invoices": _ids(cur, _INVOICE_IDS_SQL, (orders, shipments)),
    }


def retire_era(cur, old_anchor: float, *, now: float, days: int = DEFAULT_DAYS) -> Dict[str, int]:
    """Delete exactly what ``plan(old_anchor, days)`` wrote to the ERP and the
    story runs of ``old_anchor``'s loops whose alarm is <= ``now`` (with every
    child row, FK order); returns rows deleted per :data:`RETIRE_COUNT_KEYS`.
    Runs inside the caller's transaction; a simulator-owned row never matches
    (history rows match on number AND sku/line/start, orders on number AND
    customer/date, story runs on run AND work-order number AND no
    order/site/line/story part)."""
    sp = sh.plan(old_anchor, days)
    return _retire(cur, _retire_scope(cur, sp, story_run_numbers(old_anchor, now)))


def retire_namespace(cur) -> Dict[str, int]:
    """Delete EVERY story history and story-step row, whatever loop geometry
    wrote it: the story site's 9xxxx history runs and 8xxxx story runs, the
    9xxxxxxx orders of the site's customers, and every child row (FK order);
    returns rows deleted per :data:`RETIRE_COUNT_KEYS`. Used when the stored
    geometry is not this process's (:func:`retire_era` would rebuild the old
    era's numbers with the new geometry and miss them). A row matches only
    with its paired number (run AND work order, order AND shipment), so a
    simulator row never does. Runs inside the caller's transaction."""
    return _retire(cur, _namespace_scope(cur))


def _retire(cur, scope: Dict[str, List[str]]) -> Dict[str, int]:
    counts = dict.fromkeys(RETIRE_COUNT_KEYS, 0)
    for key, id_set, sql in _RETIRE_STEPS:
        if scope[id_set]:
            cur.execute(sql, (scope[id_set],))
            counts[key] = sh._count(cur)
    return counts


def _retire_live_runs(cur, anchor: float, now: float) -> int:
    """Delete the story runs (and their MES rows) the minute step started in
    ``anchor``'s loops whose alarm is <= ``now``, and NOTHING of the history:
    the live era's own rows (ADR-0095). Returns story runs deleted."""
    numbers = story_run_numbers(anchor, now)
    if not numbers:
        return 0
    story = _ids(cur, _STORY_RUN_IDS_SQL, (
        [run for run, _wo in numbers], [wo for _run, wo in numbers], sh.SITE, cm.STORY_MES_LINE, sh.FAILED_SKU))
    return _retire(cur, _scope_of(cur, [], story, []))["story_runs"]


# ---------------------------------------------------------------------------
# Replan: one ERP transaction (lock, CAS, retire, write, config), then seed
# ---------------------------------------------------------------------------

_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"
_READ_CONFIG_SQL = "SELECT config_key, config_value FROM simulation_config WHERE config_key = ANY(%s)"
# The plan owns every story config value: a re-plan overwrites (the
# insert-only writer would keep the old anchor). Unchanged rows stay put.
_UPSERT_CONFIG_SQL = """
    INSERT INTO simulation_config (config_key, config_value, data_type, category, description, default_value)
    VALUES (%s, %s, %s, 'story', %s, %s)
    ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value, updated_at = NOW()
    WHERE simulation_config.config_value IS DISTINCT FROM EXCLUDED.config_value
"""


def _read_config(cur) -> Dict[str, str]:
    cur.execute(_READ_CONFIG_SQL, (list(sh.CONFIG_KEYS) + [LIVE_ANCHOR_KEY],))
    return {key: value for key, value in cur.fetchall()}


def _stored_anchors(config: Dict[str, str]) -> Tuple[Optional[float], Optional[float]]:
    """``(history, live)`` anchors as stored: ``live`` is the live key's
    value, or the history anchor when no live key exists (one clock), or
    None when there is no clock at all."""
    history = float(config[ANCHOR_KEY]) if ANCHOR_KEY in config else None
    live = float(config[LIVE_ANCHOR_KEY]) if LIVE_ANCHOR_KEY in config else history
    return history, live


_DELETE_LIVE_SQL = "DELETE FROM simulation_config WHERE config_key = %s"


def _clear_live_anchor(cur) -> int:
    cur.execute(_DELETE_LIVE_SQL, (LIVE_ANCHOR_KEY,))
    return sh._count(cur)


def _upsert_rows(cur, rows) -> int:
    changed = 0
    for row in rows:
        cur.execute(_UPSERT_CONFIG_SQL, (row.key, row.value, row.data_type, row.description, row.value))
        changed += sh._count(cur)
    return changed


def _reset_telemetry(cur, sp: sh.StoryPlan) -> Dict[str, object]:
    """Reset the feed confirmation to ``sp``'s anchor, unconfirmed: the
    simulator's anchor mirror confirms it within a cycle (ADR-0092)."""
    _upsert_rows(cur, [row for row in sp.config if row.key in TELEMETRY_KEYS])
    cursor = next(int(row.value) for row in sp.config if row.key == TELEMETRY_KEYS[1])
    return {"anchor": sp.anchor_epoch, "cursor_epoch": cursor, "backfill": "pending"}


def _check_cas(stored: Optional[float], expected: Optional[float]) -> None:
    if expected is not None and (stored is None or float(expected) != stored):
        raise StaleAnchor(f"stored anchor is {stored!r}, the placement expected {expected!r}")


def _geometry_drift(config: Dict[str, str], sp: sh.StoryPlan) -> bool:
    """The stored geometry (:data:`GEOMETRY_KEYS`; a missing key counts)
    is not ``sp``'s, i.e. not the one condition_monitor read at import."""
    planned = {row.key: row.value for row in sp.config if row.key in GEOMETRY_KEYS}
    return any(config.get(key) != planned.get(key) for key in GEOMETRY_KEYS)


def _retire_prior(cur, stored: Optional[float], drift: bool, now: float, days: int) -> Dict[str, int]:
    """Retire the stored era: by namespace after a geometry change, else by plan."""
    if stored is None:
        return dict.fromkeys(RETIRE_COUNT_KEYS, 0)
    return retire_namespace(cur) if drift else retire_era(cur, stored, now=now, days=days)


def _replan_erp(cur, args: dict) -> Dict[str, object]:
    """The ERP half of :func:`replan` (inside its transaction). The same anchor under the
    same geometry retires and writes nothing (a reset still restores the default source,
    and always ends the history replay); a changed geometry always retires (by namespace)
    and rewrites, config included. A stored live anchor (ADR-0095) is cleared: after a full
    re-plan the two clocks are one, and the feed catches up from the anchor it was following."""
    cur.execute(_LOCK_SQL, (LOCK_KEY,))
    config = _read_config(cur)
    stored, live = _stored_anchors(config)
    _check_cas(stored, args["expected"])
    days = int(config.get(DAYS_KEY, DEFAULT_DAYS))
    target = args["target"]
    sp = sh.plan(target, days, source=args["source"], now=args["now"])
    drift = stored is not None and _geometry_drift(config, sp)
    retired = dict.fromkeys(RETIRE_COUNT_KEYS, 0)
    written = {"config": 0, "runs": 0, "orders": 0}
    if stored == target and not drift:
        # Same anchor: nothing rewritten; a reset still restores the default source.
        restore = bool(args.get("restore_source"))
        source = args["source"] if restore else config.get(SOURCE_KEY, args["source"])
        sp = sh.plan(target, days, source=source, now=args["now"])
        if restore and source != config.get(SOURCE_KEY):
            written["config"] = _upsert_rows(cur, [row for row in sp.config if row.key == SOURCE_KEY])
    else:
        retired = _retire_prior(cur, stored, drift, args["now"], days)
        config_rows = _upsert_rows(cur, [row for row in sp.config if row.key not in TELEMETRY_KEYS])
        written = {**sh._write_erp(cur, sp), "config": config_rows}
    if live is not None and live != stored:
        retired["story_runs"] += _retire_live_runs(cur, live, args["now"])
    if live is not None and live != target:
        source = "placed" if live != stored else config.get(SOURCE_KEY)
        written["catch_up"] = _upsert_rows(cur, [catch_up_row(live, target, args["now"], source)])
    if stored != target and config.get(SOURCE_KEY) in DEFAULT_SOURCES:
        written["backfill"] = _upsert_rows(cur, [sh.backfill_row(target, args["now"])])  # replay ADR
    written["backfill_cleared"] = sh.clear_backfill(cur) if args.get("restore_source") else 0  # a Reset
    written["live_anchor_cleared"] = _clear_live_anchor(cur)
    return {"old_anchor": stored, "plan": sh.without_held_stops(cur, sp), "retired": retired, "written": written,
            "telemetry": _reset_telemetry(cur, sp), "geometry_changed": drift}


def catch_up_window(new_anchor: float, now: float) -> Tuple[int, int]:
    """``(start, end)`` of the server's catch-up after a placement at ``now``:
    it starts :data:`CATCH_UP_LEAD_SECONDS` later and ends at the first BPFO
    alarm of ``new_anchor`` after that start."""
    start = int(now) + CATCH_UP_LEAD_SECONDS
    end = cm.alarm_epoch(start, new_anchor)
    if end <= start:
        end += LOOP_SECONDS
    return start, int(end)


def catch_up_row(old_anchor: float, new_anchor: float, now: float,
                 old_source: Optional[str] = None) -> sh.ConfigRow:
    """The :data:`CATCH_UP_KEY` row for a move from ``old_anchor``, with the
    era's ``story_anchor_source`` when it is a known one: the anchor mirror
    reads it to climb from the healthy row when that era was held unplaced
    (ADR-0092 decision 8). This module only records the fact."""
    start, end = catch_up_window(new_anchor, now)
    source = f";{old_source}" if old_source in sh.ANCHOR_SOURCES else ""
    return sh.ConfigRow(CATCH_UP_KEY, f"{int(old_anchor)};{start};{end}{source}", "string",
                        "OPC UA catch-up after the last placement: previous anchor;start;end;previous source")


# Story-asset condition orders reported before ``now`` that are NOT this
# era's (wonum AND alarm-instant reported_date): an earlier era's order
# (even with a new-era wonum) goes with its log rows; this era's stay.
_STALE_CONDITION_SCOPE = sh.sql_text("""
    SELECT wo.wonum FROM {cmms}.work_orders wo
    WHERE wo.asset_num = %(asset)s AND wo.wonum LIKE %(prefix)s AND wo.reported_date < %(now)s
      AND NOT EXISTS (
          SELECT 1 FROM unnest(%(keep_wonums)s::varchar[], %(keep_dates)s::timestamp[]) AS k(wonum, reported)
          WHERE k.wonum = wo.wonum AND k.reported = wo.reported_date)
""", cmms=sh.CMMS_SCHEMA)
_PURGE_STALE_CONDITION_LOG_SQL = sh.sql_text(
    "DELETE FROM {cmms}.work_order_log WHERE wonum IN ({scope})", cmms=sh.CMMS_SCHEMA, scope=_STALE_CONDITION_SCOPE)
_PURGE_STALE_CONDITION_WO_SQL = sh.sql_text(
    "DELETE FROM {cmms}.work_orders WHERE wonum IN ({scope})", cmms=sh.CMMS_SCHEMA, scope=_STALE_CONDITION_SCOPE)


def _condition_wonum(loop: int) -> str:
    return sh.MESSimulator._condition_wonum(float(loop * LOOP_SECONDS))


def _purge_condition_orders_before(cur, anchor: float, now: float) -> int:
    """ADR-0089 D11: delete the story asset's condition orders reported before ``now``
    except this era's own; returns work orders deleted. Idempotent."""
    alarms = era_alarms(anchor, now)
    params = {"asset": cm.STORY_CMMS_ASSET_NUM, "prefix": sh.CONDITION_WONUM_PREFIX + "%",
              "now": sh.ts(now), "keep_wonums": [_condition_wonum(loop) for loop, _a, _f in alarms],
              "keep_dates": [sh.ts(alarm) for _l, alarm, _f in alarms]}
    cur.execute(_PURGE_STALE_CONDITION_LOG_SQL, params)
    cur.execute(_PURGE_STALE_CONDITION_WO_SQL, params)
    return sh._count(cur)


def _catch_up_condition_orders(cur, anchor: float, now: float) -> int:
    """Raise ``VIB-CMX-<loop>`` for every new-era loop whose alarm is <= now,
    through the monitor's own writer (ON CONFLICT DO NOTHING)."""
    alarms = era_alarms(anchor, now)
    if not alarms:
        return 0
    insert = getattr(sh.MESSimulator, "insert_condition_work_order")  # Task G contract, resolved per call
    return sum(insert(cur, loop, alarm, fail) for loop, alarm, fail in alarms)


def _erp_anchor_now(erp_conn) -> Optional[float]:
    """The COMMITTED ERP anchor, read in its own short transaction."""
    try:
        return sh.read_stored_anchor(erp_conn)
    finally:
        erp_conn.rollback()


def _claim_seed_era(cur, sp: sh.StoryPlan, erp_conn) -> None:
    """Take the EXCLUSIVE story-clock lock on the seed transaction (the MES
    condition monitor takes it shared), then confirm the committed ERP anchor
    is still this plan's: a later placement that committed its ERP half owns
    the seed side, so an interleaved earlier one raises :class:`StaleAnchor`
    with nothing written to the seed."""
    cur.execute(_LOCK_SQL, (LOCK_KEY,))
    stored = _erp_anchor_now(erp_conn)
    if stored != float(sp.anchor_epoch):
        raise StaleAnchor(f"the story clock was re-placed during this placement: the stored anchor is "
                          f"{stored!r}, this placement wrote {sp.anchor_epoch!r}")


def _replan_seed(cur, args: dict) -> Dict[str, int]:
    """The seed half of :func:`replan` (inside its transaction, under the
    seed-side story-clock lock: :func:`_claim_seed_era`)."""
    sp, now = args["plan"], args["now"]
    _claim_seed_era(cur, sp, args["erp_conn"])
    purged = _purge_condition_orders_before(cur, sp.anchor_epoch, now)
    counts = sh._write_seed(cur, sp)
    counts["purged_condition_orders"] += purged
    counts["caught_up_condition_orders"] = _catch_up_condition_orders(cur, sp.anchor_epoch, now)
    return counts


def replan(erp_conn, seed_conn, new_anchor: float, *, now: float, source: str,
           expected_anchor: Optional[float] = None, restore_source: bool = False) -> Dict[str, object]:
    """Move the story clock to ``normalize_anchor(new_anchor, now)``.

    ``source`` is recorded as ``story_anchor_source`` (``placed`` |
    ``packaged`` | ``deploy``); onto the stored anchor the stored source is
    kept unless ``restore_source`` (the reset). ``expected_anchor`` (None: no check) must
    equal the stored anchor or :class:`StaleAnchor` is raised with nothing
    written. ERP transaction first, then seed; either failure rolls that
    database back and re-raises. Both halves hold the EXCLUSIVE story-clock
    advisory lock on their own database; the seed half also raises
    :class:`StaleAnchor` (seed untouched) when a later placement's ERP half
    committed in between (that placement's seed half owns the seed). Returns ``{old_anchor, new_anchor, source,
    retired{...}, written{...}, seed{...}, telemetry{...}, alarm_at,
    loops_shifted, geometry_changed}`` (epochs in seconds; ``alarm_at`` is the
    placed alarm; ``geometry_changed``: the namespace retire ran).
    """
    if source not in sh.ANCHOR_SOURCES:
        raise ValueError(f"source must be one of {sh.ANCHOR_SOURCES}, got {source!r}")
    target = normalize_anchor(new_anchor, now)
    erp = sh._in_transaction(erp_conn, _replan_erp, {
        "target": target, "now": now, "source": source, "expected": expected_anchor,
        "restore_source": restore_source})
    seed = sh._in_transaction(seed_conn, _replan_seed, {"plan": erp["plan"], "now": now, "erp_conn": erp_conn})
    return _report(erp, seed, new_anchor, now)


def _report(erp: dict, seed: dict, new_anchor: float, now: float) -> Dict[str, object]:
    sp: sh.StoryPlan = erp["plan"]
    source = next(row.value for row in sp.config if row.key == SOURCE_KEY)
    retired = {**erp["retired"], "condition_orders": seed["purged_condition_orders"]}
    written = {**erp["written"], "cmms_work_orders": seed["cmms_work_orders"], "cmms_log": seed["cmms_log"],
               "condition_orders": seed["caught_up_condition_orders"]}
    return {
        "old_anchor": erp["old_anchor"], "new_anchor": int(sp.anchor_epoch), "source": source,
        "retired": retired, "written": written,
        "seed": {"superseded_orders": seed["superseded_orders"], "prior_era_marked": seed["prior_era_marked"]},
        "telemetry": erp["telemetry"], "failed_run_number": sp.failed_run.run_number,
        "geometry_changed": erp["geometry_changed"],
        "alarm_at": cm.snap_anchor(new_anchor, "nearest") + cm.ALARM_OFFSET_SECONDS,
        "loops_shifted": loops_shifted(new_anchor, now),
    }


# ---------------------------------------------------------------------------
# Live-only move (ADR-0095): the alarm moves, the written history stays
# ---------------------------------------------------------------------------

def live_alarm_allowed(history_anchor: float, target: float) -> bool:
    """True when ``target``'s loop-0 BPFO alarm falls AFTER the written history
    ends (``history_anchor``): the one invariant of the two-clock model, so a
    live condition order never lands among the history's own orders."""
    return target + cm.ALARM_OFFSET_SECONDS > history_anchor


def _live_anchor_row(target: float) -> sh.ConfigRow:
    return sh.ConfigRow(LIVE_ANCHOR_KEY, repr(float(target)), "float",
                        "Live story clock anchor, moved without rewriting the history")


def _live_anchor_now(erp_conn) -> Optional[float]:
    """The COMMITTED live anchor (live key, else history), own short transaction."""
    from . import story_live  # lazy: story_live imports this module
    try:
        return story_live.read_anchor(erp_conn)
    finally:
        erp_conn.rollback()


def _check_live_move(config: Dict[str, str], history: Optional[float], target: float) -> None:
    """The three refusals of a live-only move (:class:`LiveClockRefused`)."""
    if history is None:
        raise LiveClockRefused("no story clock is stored: the live-story history has not run")
    days = int(config.get(DAYS_KEY, DEFAULT_DAYS))
    if _geometry_drift(config, sh.plan(history, days)):
        raise LiveClockRefused("the stored loop geometry is not this deployment's; place the whole clock instead")
    if not live_alarm_allowed(history, target):
        raise LiveClockRefused(f"the new alarm at {int(target + cm.ALARM_OFFSET_SECONDS)} would land inside "
                               f"the written history (which ends at {int(history)}); place the whole clock instead")


_PLACED_ROW = sh.ConfigRow(SOURCE_KEY, "placed", "string", "How the story anchor was placed")


def _replan_live_erp(cur, args: dict) -> Dict[str, object]:
    """The ERP half of :func:`replan_live` (inside its transaction): lock,
    CAS on the LIVE anchor, the invariant, then retire only the old live
    era's story runs and write the live key (or clear it when the new live
    anchor IS the history anchor: one clock again). The cleared key was the
    only record of a placement (the mirror and the UI read it so), so any
    other history source (default, absent or unknown) becomes ``placed``.
    History rows, the history anchor and the failed run number are untouched."""
    cur.execute(_LOCK_SQL, (LOCK_KEY,))
    config = _read_config(cur)
    history, live = _stored_anchors(config)
    _check_cas(live, args["expected"])
    target, now = args["target"], args["now"]
    _check_live_move(config, history, target)
    retired, written = 0, {"config": 0, "source": 0, "catch_up": 0, "telemetry": 0}
    if target != live:
        retired = _retire_live_runs(cur, live, now)
        if target == history:
            written["config"] = _clear_live_anchor(cur)
            if config.get(SOURCE_KEY) != "placed":   # default, absent or unknown
                written["source"] = _upsert_rows(cur, [_PLACED_ROW])
        else:
            written["config"] = _upsert_rows(cur, [_live_anchor_row(target)])
        source = "placed" if live != history else config.get(SOURCE_KEY)
        written["catch_up"] = _upsert_rows(cur, [catch_up_row(live, target, now, source)])
        written["telemetry"] = _upsert_rows(cur, [sh.ConfigRow(
            TELEMETRY_KEYS[0], repr(float(target)), "float", "Anchor the story sensor feed follows")])
    return {"history": history, "old_live": live, "target": target, "retired_story_runs": retired,
            "written": written, "failed_run_number": config.get("story_failed_run_number")}


def _replan_live_seed(cur, args: dict) -> Dict[str, int]:
    """The seed half of :func:`replan_live`: under the exclusive seed-side
    lock, confirm the committed live anchor is still this move's, then purge
    the story asset's condition orders that are not this live era's and raise
    the ones whose alarm has passed. The history's CMMS rows are not touched."""
    target, now = args["target"], args["now"]
    cur.execute(_LOCK_SQL, (LOCK_KEY,))
    current = _live_anchor_now(args["erp_conn"])
    if current != float(target):
        raise StaleAnchor(f"the live clock was re-placed during this move: the stored live anchor is "
                          f"{current!r}, this move wrote {target!r}")
    return {"purged_condition_orders": _purge_condition_orders_before(cur, target, now),
            "caught_up_condition_orders": _catch_up_condition_orders(cur, target, now)}


def replan_live(erp_conn, seed_conn, new_anchor: float, *, now: float,
                expected_live_anchor: Optional[float] = None) -> Dict[str, object]:
    """Move ONLY the live clock to ``normalize_anchor(new_anchor, now)``: the
    sensor feed, the condition orders, the story runs and the phase chip
    follow the new anchor; the three-day history, its failed batch and every
    run number stay as written (ADR-0095). ``expected_live_anchor`` (None: no
    check) must equal the stored LIVE anchor (the live key, else the history
    anchor) or :class:`StaleAnchor` is raised with nothing written. Raises
    :class:`LiveClockRefused` (nothing written) when no clock is stored, the
    geometry drifted, or the new alarm would land inside the history. Moving
    the live clock back onto the history anchor clears the live key (one
    clock again) and records the history source as ``placed``. ERP
    transaction first, then seed, as :func:`replan`."""
    target = normalize_anchor(new_anchor, now)
    erp = sh._in_transaction(erp_conn, _replan_live_erp, {"target": target, "now": now,
                                                          "expected": expected_live_anchor})
    seed = sh._in_transaction(seed_conn, _replan_live_seed, {"target": target, "now": now, "erp_conn": erp_conn})
    return {
        "keep_history": True, "history_anchor": int(erp["history"]),
        "old_live_anchor": erp["old_live"], "new_live_anchor": int(target),
        "one_clock": target == erp["history"], "failed_run_number": erp["failed_run_number"],
        "retired": {"story_runs": erp["retired_story_runs"], "condition_orders": seed["purged_condition_orders"]},
        "written": {**erp["written"], "condition_orders": seed["caught_up_condition_orders"]},
        "alarm_at": cm.snap_anchor(new_anchor, "nearest") + cm.ALARM_OFFSET_SECONDS,
        "loops_shifted": loops_shifted(new_anchor, now),
    }


# ---------------------------------------------------------------------------
# Reset failsafe (ADR-0091 Design C): the default anchor and reset onto it
# ---------------------------------------------------------------------------

DEFAULT_ANCHOR_KEY, DEFAULT_SOURCE_KEY = sh.DEFAULT_KEYS
# A default is what the CR planned onto; a steward placement is never one.
DEFAULT_SOURCES = ("deploy", "packaged")


class NoDefaultAnchor(RuntimeError):
    """No (well-formed) default anchor is stored: nothing to reset onto."""


def parse_default(config: Dict[str, str]) -> Optional[Tuple[int, str]]:
    """``(anchor, source)`` of the stored default, or None when either key is
    absent or malformed (a non-finite / non-positive epoch, a non-default source)."""
    source = config.get(DEFAULT_SOURCE_KEY)
    try:
        anchor = float(config[DEFAULT_ANCHOR_KEY])
    except (KeyError, TypeError, ValueError):
        return None
    if source not in DEFAULT_SOURCES or not math.isfinite(anchor) or anchor <= 0:
        return None
    return int(anchor), source


def read_default_anchor(erp_conn) -> Optional[Tuple[int, str]]:
    """The stored default (:func:`parse_default`), read in its own short transaction."""
    try:
        with erp_conn.cursor() as cur:
            cur.execute(_READ_CONFIG_SQL, (list(sh.DEFAULT_KEYS),))
            config = {key: value for key, value in cur.fetchall()}
    finally:
        erp_conn.rollback()
    return parse_default(config)


def write_default_anchor(erp_conn, anchor: float, source: str) -> int:
    """Upsert the default ``(anchor, source)`` in one ERP transaction; returns
    config rows changed (0: already stored). ``source`` must be a default one."""
    if source not in DEFAULT_SOURCES:
        raise ValueError(f"source must be one of {DEFAULT_SOURCES}, got {source!r}")
    rows = (sh.ConfigRow(DEFAULT_ANCHOR_KEY, str(int(anchor)), "int",
                         "Default story anchor the reset failsafe restores"),
            sh.ConfigRow(DEFAULT_SOURCE_KEY, source, "string", "Source of the default story anchor"))
    return sh._in_transaction(erp_conn, _upsert_rows, rows)


def reset(erp_conn, seed_conn, *, now: float, expected_anchor: Optional[float] = None) -> Dict[str, object]:
    """Re-plan onto the stored default anchor WITH its stored source, through
    :func:`replan` (lock, CAS on ``expected_anchor``, era guard). The default is
    normalised first, so the report's ``alarm_at`` is this loop's alarm and
    ``loops_shifted`` is 0. The default source is written even onto the stored
    anchor, so a reset always leaves an unplaced clock. Raises :class:`NoDefaultAnchor`
    (nothing written) when no default is stored. The report adds ``reset: true``,
    ``default_anchor`` and ``default_source``."""
    default = read_default_anchor(erp_conn)
    if default is None:
        raise NoDefaultAnchor("no default story anchor is stored (the story-history resource "
                              "records one on Create and back-fills it on every Update)")
    anchor, source = default
    report = replan(erp_conn, seed_conn, normalize_anchor(anchor, now), now=now, source=source,
                    expected_anchor=expected_anchor, restore_source=True)
    return {**report, "reset": True, "default_anchor": anchor, "default_source": source}


__all__ = [
    "StaleAnchor", "LiveClockRefused", "normalize_anchor", "loops_shifted", "alarm_to_anchor",
    "phase_offset_to_anchor", "era_alarms", "story_run_numbers", "retire_era", "retire_namespace", "replan",
    "RETIRE_COUNT_KEYS", "LOCK_KEY", "GEOMETRY_KEYS", "LIVE_ANCHOR_KEY", "live_alarm_allowed", "replan_live",
    "NoDefaultAnchor", "DEFAULT_SOURCES", "parse_default", "read_default_anchor", "write_default_anchor", "reset"]
