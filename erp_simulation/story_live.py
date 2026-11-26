# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live workshop story: per-cycle simulator step (ADR-0085).

The live story is keyed by DATA, not by a setting: it runs only when
``simulation_config.story_anchor_epoch`` exists, and only the deploy-time
story-history custom resource writes that row. Demo and frozen deploys never
have it, so every function here is a no-op for them.

Per cycle, while the story mill's condition-monitor loop (anchored at the
stored epoch) is in its ``bpfo_alarm`` phase, each RUNNING work order OF THE
STORY PART (``story_history.FAILED_SKU``) on the story line at the
condition-monitor site gains a failing ``DIMENSIONAL`` quality check on each
alarm-aligned slot ``alarm + n * DEFECT_CHECK_INTERVAL_MINUTES`` (ADR-0088 D6:
8 per window, the first AT the alarm instant, a slot before the order started
skipped), each stamped with its slot and failing ``defect_reject_fraction`` of
its sample at the slot's step, so the fail share climbs across the alarm.
Work-order completion already sums
every check into the sampled yield (``mes_simulator.derive_good_reject``), so
the batch's ``reject_quantity`` rises without touching the production-event
simulator. An order of ANOTHER product running on the line gets no check.

While the live story is on, the MES simulator gives the story line no work
of its own (``MESSimulator.reserved_lines``): the line runs only the
history's runs and this step's story run. So before the check the step
ENSURES a RUNNING story-line work order: when none is RUNNING
and this loop's story run has not been started yet, it starts ONE
(:func:`story_work_order`: the history's failed part, an ERP production run
``IN_PROGRESS`` on the line with a linked RUNNING MES work order and its
``START`` event, planned to end at the loop's bearing failure, for the whole
alarm window's quantity). Any RUNNING
order on the line - of any product - makes the story run wait; one of
another product still gets no check. It is an ordinary ERP-linked work
order, so the simulator's own completion step closes it at the first cycle
after ``planned_end_time``. No lot allocation: shipping only picks runs with
lot usage as a batch id, so the story run never becomes a shipped batch.

Which run is then the newest SHIPPED failure: the history's failed run. The
guards above still cover a simulator order on the line, which only an account
whose MES ran before the line was reserved can hold: one of the story part
RUNNING during an alarm window genuinely fails the check and, once shipped,
becomes the newer failed batch; one of another part never can.

One clock, one era (ADR-0089): the step writes only while the stored anchor
is still the one the cycle loaded. Each apply takes the SHARED story-clock
advisory lock (``story_replan.replan`` holds the exclusive one) and re-reads
the LIVE anchor (``story_live_anchor_epoch`` when the alarm was moved without
rewriting the history, ADR-0095, else ``story_anchor_epoch``) in the SAME
transaction before any insert; a moved,
cleared or malformed anchor skips the cycle (0, one log line), so a placement
never gains an old-era story run or check after it retired that era.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from . import condition_monitor
from . import story_history
from . import story_replan
from .logging_config import get_logger
from .mes_simulator import MESSimulator

logger = get_logger(__name__)

ANCHOR_KEY = "story_anchor_epoch"
DEFECT_CHECK_TYPE = "DIMENSIONAL"
DEFECT_INSPECTOR = "QC-SPC"
DEFECT_SAMPLE_SIZE = 20
STORY_SKU = story_history.FAILED_SKU              # the story part (the history's failed batch)
STORY_FG_LOCATION = story_history.FG_LOCATION
STORY_NUMBER_PREFIX = "8"                        # 8<loop>: the history owns 9xxxx
OPERATOR_COUNT = 5                               # MESSimulator.operators: OP-1..OP-5
STORY_RATE_PER_MINUTE = 1                        # integer column: see story_work_order
# The story run's planned quantity: the WHOLE alarm window's whole minutes at
# 1/min (floor of 107.17 = 107), whenever in the window the run starts, so the
# page states one number.
STORY_PLANNED_QUANTITY = math.floor(
    STORY_RATE_PER_MINUTE * (condition_monitor.BEARING_FAIL_STEP - condition_monitor.BPFO_ALARM_STEP)
    * condition_monitor.INTERVAL_SEC / 60)
# DIMENSIONAL checks land on alarm-aligned slots ``alarm + n * interval``
# (ADR-0088 D6): the first AT the alarm instant, each stamped with its slot and
# the BPFO of the slot's step. With a congruent anchor every slot is a
# telemetry grid point, so a check's BPFO is a published Spectrum_BPFO value.
DEFECT_CHECK_INTERVAL_MINUTES = 15
DEFECT_CHECK_INTERVAL_SECONDS = DEFECT_CHECK_INTERVAL_MINUTES * 60
DEFECT_CHECKS_PER_WINDOW = math.ceil(
    (condition_monitor.BEARING_FAIL_STEP - condition_monitor.BPFO_ALARM_STEP)
    * condition_monitor.INTERVAL_SEC / DEFECT_CHECK_INTERVAL_SECONDS)  # 8

# The last bad anchor value warned about (module-level: the Lambda builds a
# fresh orchestrator per invocation but reuses the warm module).
_NOT_WARNED = object()
_last_warned_value = _NOT_WARNED

# One DIMENSIONAL check per RUNNING story-line work order OF THE STORY PART
# per check slot (NOT EXISTS guard on a check at or after the slot), so the
# fail share climbs with defect_reject_fraction while re-running a cycle
# inside one slot inserts nothing new. A slot earlier than the order's
# actual_start_time is skipped (the order was not running then). The product
# filter keeps the defect on the story work order apply_cycle started (or a simulator order of the same part on the line): an
# order of another product running on LINE3 gets NO check, so a shipped
# simulator run of another part never becomes the failed batch.
_INSERT_DEFECT_CHECKS_SQL = """
    INSERT INTO mes_quality_checks (
        work_order_id, check_time, check_type,
        sample_size, pass_count, fail_count, inspector_id, notes
    )
    SELECT mwo.work_order_id, %s, %s, %s, %s, %s, %s, %s
    FROM mes_work_orders mwo
    JOIN products p ON p.product_id = mwo.product_id
    WHERE mwo.status = 'RUNNING'
      AND mwo.site = %s
      AND mwo.line = %s
      AND p.sku = %s
      AND mwo.actual_start_time <= %s
      AND NOT EXISTS (
          SELECT 1 FROM mes_quality_checks q
          WHERE q.work_order_id = mwo.work_order_id
            AND q.check_type = %s
            AND q.check_time >= %s
      )
"""

# The story run, in ONE statement (all or nothing): an ERP production run
# IN_PROGRESS on the story line, its RUNNING MES work order and the START
# event - the rows MESSimulator.simulate_production_events leaves when it
# starts a scheduled order. Guards: no work order RUNNING on the story line,
# and this loop's story work order not started before (its number is keyed
# by loop); the run number is the natural key the insert conflicts on. A
# named parameter is ONE bound parameter wherever it appears, so the ones a
# SELECT list also carries (typed text there) are cast ::text throughout.
_START_STORY_WORK_ORDER_SQL = """
    WITH run AS (
        INSERT INTO production_runs (
            run_number, product_id, order_id, planned_quantity, status,
            actual_start_time, planned_end_time, location_id, site, line, notes
        )
        SELECT %(run_number)s, p.product_id, NULL, %(quantity)s, 'IN_PROGRESS',
               %(start)s, %(planned_end)s,
               (SELECT location_id FROM inventory_locations
                WHERE location_code = %(fg_location)s AND is_active = TRUE LIMIT 1),
               %(site)s::text, %(line)s::text, %(notes)s
        FROM products p
        WHERE p.sku = %(sku)s
          AND NOT EXISTS (
              SELECT 1 FROM mes_work_orders mwo
              WHERE mwo.status = 'RUNNING' AND mwo.site = %(site)s AND mwo.line = %(line)s
          )
          AND NOT EXISTS (
              SELECT 1 FROM mes_work_orders mwo
              WHERE mwo.work_order_number = %(work_order_number)s
          )
        ON CONFLICT (run_number) DO NOTHING
        RETURNING run_id, product_id
    ), wo AS (
        INSERT INTO mes_work_orders (
            work_order_number, production_run_id, product_id, site, line,
            planned_quantity, status, scheduled_start_time, actual_start_time,
            target_rate_per_minute
        )
        SELECT %(work_order_number)s::text, run.run_id, run.product_id, %(site)s::text, %(line)s::text,
               %(quantity)s, 'RUNNING', %(start)s, %(start)s, %(rate)s
        FROM run
        RETURNING work_order_id
    )
    INSERT INTO mes_production_events (
        work_order_id, event_type, event_time, operator_id, quantity_at_event
    )
    SELECT wo.work_order_id, 'START', %(start)s, %(operator)s, 0 FROM wo
"""


@dataclass(frozen=True)
class StoryWorkOrder:
    run_number: str
    work_order_number: str
    start: datetime
    planned_end: datetime
    planned_quantity: int
    target_rate: int
    operator_id: str


def story_work_order(now: float, anchor: float) -> StoryWorkOrder:
    """The story run the step would start at ``now`` (pure).

    Numbers follow ``utils.generate_work_order_number`` / ``generate_run_number``
    (``WO-<YYYYMMDD>-<5 digits>``), dated on the loop's alarm instant and
    suffixed ``8`` + loop, so every cycle of one alarm window names the same
    rows. It starts at the current 5-min slot of the alarm-aligned grid
    (ADR-0088 D6): with a congruent anchor that is the telemetry grid slot
    ``grid_floor(now)``, and on the first alarm cycle it IS the alarm
    instant (the first check slot), also for a legacy off-grid anchor. It
    is planned to end at
    the loop's bearing failure, for :data:`STORY_PLANNED_QUANTITY` units (the
    whole alarm window) however late in the window it starts.

    ``target_rate_per_minute`` is an INTEGER column, so the history's machining
    rate (60 per 90 min = 0.67/min) cannot be stored; the run is planned at an
    honest :data:`STORY_RATE_PER_MINUTE` instead (one unit per alarm-window
    minute), so the completion step's OEE performance (actual / (rate x
    minutes)) reads what the run did rather than ~65 % for a rounded rate.
    """
    loop = condition_monitor.loop_number(condition_monitor.story_seconds(now, anchor))
    alarm = condition_monitor.alarm_epoch(now, anchor)
    fail = condition_monitor.fail_epoch(now, anchor)
    start = alarm + condition_monitor.grid_floor(now - alarm)
    suffix = f"{datetime.fromtimestamp(alarm):%Y%m%d}-{STORY_NUMBER_PREFIX}{loop % 10_000:04d}"
    return StoryWorkOrder(
        f"RUN-{suffix}", f"WO-{suffix}", datetime.fromtimestamp(start), datetime.fromtimestamp(fail),
        STORY_PLANNED_QUANTITY, STORY_RATE_PER_MINUTE, f"OP-{loop % OPERATOR_COUNT + 1}")


def _start_params(wo: StoryWorkOrder) -> dict:
    return {
        "run_number": wo.run_number, "work_order_number": wo.work_order_number,
        "sku": STORY_SKU, "quantity": wo.planned_quantity, "rate": wo.target_rate,
        "start": wo.start, "planned_end": wo.planned_end, "operator": wo.operator_id,
        "fg_location": STORY_FG_LOCATION, "site": MESSimulator.CONDITION_MONITOR_SITE,
        "line": condition_monitor.STORY_MES_LINE, "notes": f"Production batch for {STORY_SKU}",
    }


def _rows(cursor) -> int:
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


# The LIVE anchor (ADR-0095): ``story_live_anchor_epoch`` when the bearing
# alarm was moved without rewriting the history, else ``story_anchor_epoch``.
# One statement, the live key first, so a cycle never reads the two keys at
# different snapshots. Every live-side reader (the condition monitor, this
# step, the anchor mirror, the lookup's phase) goes through it.
LIVE_ANCHOR_KEY = story_history.LIVE_ANCHOR_KEY
_READ_ANCHOR_SQL = ("SELECT config_value FROM simulation_config WHERE config_key = ANY(%s) "
                    "ORDER BY config_key = %s DESC LIMIT 1")
_READ_ANCHOR_PARAMS = ([LIVE_ANCHOR_KEY, ANCHOR_KEY], LIVE_ANCHOR_KEY)


def _finite(raw) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def era_is_current(cursor, anchor: float) -> bool:
    """Take the shared story-clock lock, then re-read the stored LIVE anchor
    (a separate statement: its snapshot follows the lock); True only when it
    is still ``anchor``. The lock is held until the caller's transaction ends."""
    cursor.execute(story_replan.SHARED_LOCK_SQL, (story_replan.LOCK_KEY,))
    cursor.execute(_READ_ANCHOR_SQL, _READ_ANCHOR_PARAMS)
    row = cursor.fetchone()
    return row is not None and _finite(row[0]) == float(anchor)


def read_anchor(conn) -> Optional[float]:
    """The stored LIVE story anchor epoch (``story_live_anchor_epoch`` when
    present, else ``story_anchor_epoch``), or ``None`` when neither key
    exists (demo / frozen) or the value is not a finite positive number.

    Database errors propagate; the orchestrator's caller logs them.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(_READ_ANCHOR_SQL, _READ_ANCHOR_PARAMS)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        _warn_bad_anchor(row[0], "malformed")
        return None
    if not math.isfinite(value) or value <= 0:
        _warn_bad_anchor(row[0], "non-positive")
        return None
    return value


def _warn_bad_anchor(raw, kind: str) -> None:
    """One warning per bad stored VALUE (the anchor is re-read every cycle)."""
    global _last_warned_value
    if raw == _last_warned_value:
        return
    _last_warned_value = raw
    logger.warning(f"Ignoring {kind} {ANCHOR_KEY}: {raw!r}")


def defect_check_row(step: int, bpfo_mm_s: float) -> Tuple[int, int, int, str]:
    """``(sample_size, pass_count, fail_count, notes)`` of the dimensional
    check taken at loop ``step`` (pure). Outside the BPFO-alarm window the
    fail count is 0; inside it follows ``condition_monitor.defect_reject_fraction``.
    """
    fail = int(round(DEFECT_SAMPLE_SIZE * condition_monitor.defect_reject_fraction(step)))
    fail = max(0, min(DEFECT_SAMPLE_SIZE, fail))
    notes = (
        f"SPC bore diameter drift on {condition_monitor.STORY_MES_EQUIPMENT_ID}: "
        f"BPFO band {bpfo_mm_s:.2f} mm/s"
    )
    return DEFECT_SAMPLE_SIZE, DEFECT_SAMPLE_SIZE - fail, fail, notes


def defect_check_slot(now: float, anchor: float) -> Optional[float]:
    """The check slot the cycle at ``now`` fills (pure): the latest
    ``alarm + n * DEFECT_CHECK_INTERVAL_SECONDS`` at or before ``now`` in
    the anchored loop, or ``None`` outside its ``bpfo_alarm`` phase
    (n = 0 .. DEFECT_CHECKS_PER_WINDOW - 1)."""
    step = condition_monitor.loop_step(condition_monitor.story_seconds(now, anchor))
    if condition_monitor.phase(step) != "bpfo_alarm":
        return None
    alarm = condition_monitor.alarm_epoch(now, anchor)
    return alarm + DEFECT_CHECK_INTERVAL_SECONDS * int((now - alarm) // DEFECT_CHECK_INTERVAL_SECONDS)


def _check_params(slot: float, anchor: float) -> Tuple[int, int, tuple]:
    """``(step, failed, params)`` of the slot's check for _INSERT_DEFECT_CHECKS_SQL:
    stamped with the slot, failing and noting the BPFO at the slot's step."""
    step = condition_monitor.loop_step(condition_monitor.story_seconds(slot, anchor))
    bpfo = float(condition_monitor.spectral_features(step)["bpfo"])
    sample, passed, failed, notes = defect_check_row(step, bpfo)
    at = datetime.fromtimestamp(slot)
    return step, failed, (
        at, DEFECT_CHECK_TYPE, sample, passed, failed, DEFECT_INSPECTOR, notes,
        MESSimulator.CONDITION_MONITOR_SITE, condition_monitor.STORY_MES_LINE, STORY_SKU,
        at, DEFECT_CHECK_TYPE, at,
    )


def apply_cycle(conn, now: float, anchor: float) -> int:
    """Add the live defect for this cycle; returns check rows inserted.

    A no-op (0) outside the ``bpfo_alarm`` phase of the anchored loop. Inside
    it, first confirm the era (:func:`era_is_current`: a placement since the
    cycle loaded ``anchor`` skips the cycle), then ensure a RUNNING story-line
    work order (at most one story run per loop) and add the check of the
    current slot (:func:`defect_check_slot`; one per work order per slot,
    none for an order that started after the slot). Runs in its own
    transaction (the orchestrator's story step) and never raises: a failure
    is logged, rolled back and reported as 0.
    """
    slot = defect_check_slot(now, anchor)
    if slot is None:
        return 0
    step, failed, params = _check_params(slot, anchor)
    wo = story_work_order(now, anchor)
    try:
        started, inserted = _apply_in_era(conn, wo, params, anchor)
    except Exception as err:
        _rollback_quietly(conn)
        logger.warning(f"Live-story defect step FAILED at loop step {step}: {err}")
        return 0
    if started is None:
        _rollback_quietly(conn)
        logger.info(f"Live story: the story clock moved since this cycle loaded anchor {anchor!r}; "
                    f"skipping the cycle")
        return 0
    if started:
        logger.info(f"Live story: started {wo.work_order_number} ({wo.planned_quantity} x {STORY_SKU}) "
                    f"on {condition_monitor.STORY_MES_LINE} until {wo.planned_end:%H:%M}")
    if inserted:
        logger.info(f"Live story: {inserted} {DEFECT_CHECK_TYPE} check(s) failing {failed}/{DEFECT_SAMPLE_SIZE} "
                    f"at step {step}")
    return inserted


def _apply_in_era(conn, wo: StoryWorkOrder, params: tuple, anchor: float) -> Tuple[Optional[int], int]:
    """``(started, inserted)`` on one cursor, or ``(None, 0)`` with nothing
    written when the stored anchor is no longer ``anchor``."""
    cursor = conn.cursor()
    try:
        if not era_is_current(cursor, anchor):
            return None, 0
        cursor.execute(_START_STORY_WORK_ORDER_SQL, _start_params(wo))
        started = _rows(cursor)
        cursor.execute(_INSERT_DEFECT_CHECKS_SQL, params)
        return started, _rows(cursor)
    finally:
        cursor.close()


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # nosec B110 - best-effort rollback on a failing connection
        pass
