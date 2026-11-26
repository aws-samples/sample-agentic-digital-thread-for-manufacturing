# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deploy-time history for the LIVE workshop story (ADR-0085).

A live-story deploy runs this once (``Custom::ADTStoryHistory``, ADTERPLambdaStack)
so a fresh account already carries a few simulated days before the story clock
starts: normal machining runs, each shipped once to one site customer (the
way a plant ships what it makes: ``shipment_items.batch_id`` = the run
number), ONE failed line-3 batch shipped to three customers, the spindle-bearing PM that was deferred before it
(and its previous cycle, done on time: the PM schedule's last completion), and the
condition-monitor / emergency work orders of the repair. A few routine
mechanical / electrical stops on runs of the other parts on lines 1 and 2
(:data:`PAST_STOPS`) carry the
completed MES-bridge order each raised, so the MES and the site CMMS already
name the same technicians and failure codes. Every run carries
its lot trace on both sides (ERP lot usage, MES material consumption), and the
failed part's material comes from two supplier lots, so the failed batch's
lot fed several batches but not every batch of the part.

Two halves:

* :func:`plan` is PURE (no I/O, no RNG, no wall clock): the same anchor always
  yields the same rows, numbered with fixed ``9xxxx`` suffixes.
* :func:`write` is the one writer. Every ERP row is keyed by a natural key
  (``ON CONFLICT DO NOTHING``) and child rows are written only when their
  parent row was new, so a second run inserts nothing. The anchor is read back
  first: when one is stored, the plan is rebuilt from the STORED anchor, so a
  stack Update never moves the story clock. The seed-side CMMS rows are
  UPSERTED instead (the plan owns their dates and status; the log entry is
  keyed by ``(wonum, log_type)``): a db-init re-run wipes the ERP side, the
  next write takes a NEW anchor, and the seed rows must follow it.

``simulation_config.story_anchor_epoch`` is what switches the simulator's
live-story steps on (``erp_simulation/story_live.py``); only this writer
creates it, inside the same ERP transaction as the history rows.

Commit order: the ERP transaction (history rows AND the anchor) commits
BEFORE the seed-database transaction (site CMMS rows). The two databases
cannot share one transaction, so a seed failure leaves the ERP side committed
and the custom resource fails. That order is deliberate: the stored anchor is
what keeps a retry on the SAME clock (the rerun rebuilds the plan from it,
the ERP inserts are all no-ops and only the missing seed rows are written),
whereas seed-first would let a failed ERP write leave CMMS rows dated for an
anchor that was never stored.

On the seed side the live story also retires the frozen era on the story
asset (``011_cmms_indianapolis.sql`` rows reported before the anchor, which
the FROZEN story needs as they are). Its OPEN orders (PM 1182 ``WAPPR``,
PM 1201 ``APPR``) are superseded: status ``CAN``, one ``work_order_log``
entry that records the prior status (:func:`superseded_log_entry`) and
:data:`SUPERSEDED_DESCRIPTION_SUFFIX` on the description. Its COMPLETED
orders (CM 1187, EM 1195, whose descriptions cite frozen-story ids) stay as
history with :data:`PRIOR_ERA_PREFIX` on the description. The history's own
deferred PM (``3101``) is then the only open order. Condition-monitor orders
(``VIB-CMX-<loop>``) on the story asset reported before the anchor are an
earlier era's and are deleted, so they never suppress this era's
loop-numbered orders. Every step is idempotent; the seed SQL itself is never
edited.

The custom resource's Delete (live -> frozen) runs :func:`clear_story_config`
(every ``story_*`` key, :data:`CONFIG_KEYS`: the live clock and defect turn
off) and :func:`restore_frozen_seed` (each superseded order reopens to its
recorded status, suffix, prefix and supersede entries go, and the
history-owned CMMS rows go, so the frozen story sees exactly its own seed).
The ERP history rows stay (ordinary demo data).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from . import condition_monitor as cm
from .logging_config import get_logger
from .mes_simulator import MESSimulator, compute_oee_components, downtime_length_text
from .utils import HISTORY_RUN_NOTE, SqlText, sql_text

logger = get_logger(__name__)

SITE = MESSimulator.CONDITION_MONITOR_SITE
CMMS_SCHEMA = MESSimulator.CMMS_SITE_TARGET[SITE][0]
CREW = MESSimulator.CONDITION_MONITOR_CREW
LINES = ("LINE1", "LINE2", cm.STORY_MES_LINE)
FG_LOCATION = "IND-FG"
# (sku, main raw-material lot, kg of that material per unit) — the one-ERP
# machining products and the LOT-IND-*-SEED lots db-init seeds for them.
PRODUCTS = (
    ("MACH-HSG-7742", "LOT-IND-BARAL7075150-SEED", 4.2),
    ("MACH-SHFT-8810", "LOT-IND-RODST4140100-SEED", 6.0),
    ("MACH-BRKT-9901", "LOT-IND-SHTTI64125-SEED", 1.4),
)
FAILED_SKU = PRODUCTS[0][0]
RAW_LOCATION = "IND-WH-RAW"
# The failed part's main material arrives in TWO supplier lots, both written
# by this history and both fully drawn by its runs, so the simulator's FIFO
# never picks either (it keeps drawing the db-init lot, which is not part of
# the story). The runs of the earliest simulated day finish the earlier lot;
# every later run of the part, the failed batch included, draws the suspect
# lot. One lot then feeds several batches, but not every batch of the part,
# and the suspect lot's batches are a fixed containment scope however long
# the account runs.
FAILED_MATERIAL_CODE = "BAR-AL7075-150"       # PRODUCTS[0]'s LOT-IND-...-SEED item
EARLIER_LOT_NUMBER = "LOT-IND-BARAL7075150-H2231"
HISTORY_LOT_SUPPLIER = "SUP-IND-METALS"
EARLIER_LOT_LEAD_DAYS = 3                     # received three days before its first run
SUSPECT_LOT_NUMBER = "LOT-IND-BARAL7075150-H2240"
SUSPECT_LOT_LEAD_SECONDS = 12 * 3600          # received 12 h before its first run
STORY_CUSTOMER_CODES = ("CUST-MCH-0001", "CUST-MCH-0002", "CUST-MCH-0003")
# Every MACHINING customer of the site (db-init MACHINING_CUSTOMERS): the
# completed history runs ship to them in rotation.
SITE_CUSTOMER_CODES = (*STORY_CUSTOMER_CODES, "CUST-MCH-0004")
DEFERRED_PM_WONUM = "3101"
CONDITION_WONUM = "3107"
REPAIR_WONUM = "3108"
# The story PM's last completed cycle (one frequency before the deferred PM's
# target): the work order behind the PM schedule's last completion date, so
# the schedule and the work-order history agree on when the PM was last done.
# Routine history, not a story order: the answer key does not list it.
LAST_PM_WONUM = "3064"
STORY_PM_NUM = "PM-1001-0007"
PM_FREQUENCY_DAYS = 90                  # 011_cmms_indianapolis.sql: PM-1001-0007 frequency
# The PM's dates in the 011 seed (the frozen story's: last completed, next
# due). The live story moves them to its own deferred PM (3101) so the PM
# schedule and the work order tell the same story; the Delete restores these.
FROZEN_PM_DATES = ("2025-10-01", "2025-12-30")
# The frozen story's run day (011_cmms_indianapolis.sql KEY DEFECT SCENARIO:
# batch B-2847, 2026-02-10). Every non-story Indianapolis PM schedule row in
# the 011 seed straddles this day (last completion before it, next due after
# it); the live story shifts them by (live run day - this) so they keep that
# standing against the live run instead of reading months overdue. A deploy-
# time data constant, like FROZEN_PM_DATES above; the engine agents never see
# it. The Delete restores the seed rows from PLANT_PM_BASELINE below.
FROZEN_STORY_RUN_DATE = date(2026, 2, 10)


@dataclass(frozen=True)
class PlantPmBaseline:
    """A non-story Indianapolis ``pm_schedules`` row as the 011 seed writes it
    (the frozen story's dates), plus the reserved work-order number the live
    story closes its shifted last-completion cycle under. The story PM
    (PM-1001-0007) is NOT here: it has its own placement (:func:`_pm_schedule_dates`,
    work orders 3064/3101)."""
    pm_num: str
    asset_num: str
    description: str
    frequency: int
    crew: str
    priority: int
    last_completion: str          # 011 seed last_completion_date (ISO)
    next_due: str                 # 011 seed next_due_date (ISO)
    close_wonum: str              # reserved CLOSE work order for the shifted cycle


# The 011_cmms_indianapolis.sql pm_schedules rows OTHER than the story PM, with
# the CLOSE work order each live-aligned cycle is written under. The close
# wonums are a reserved 32xx band that no other writer produces: the seed uses
# 1004..1204, the story history uses 3064/3101/3107/3108, the condition monitor
# uses VIB-CMX-<loop>, and MES work orders are WO-<date>-<5 digits> (the
# simulator's 40000-79999, the story's 8xxxx live and 9xxxx history runs), not
# site CMMS orders. Priorities and crews are the 011 seed's for each plan.
PLANT_PM_BASELINE = (
    PlantPmBaseline("PM-1001-0008", "1001-CMX-441",
                    "180-day way cover and axis lubrication service", 180, "IND-MECH-A", 2,
                    "2025-11-12", "2026-05-11", "3208"),
    PlantPmBaseline("PM-1001-0011", "1001-HYP-402",
                    "90-day hydraulic fluid analysis and filter change", 90, "IND-MECH-B", 3,
                    "2026-01-08", "2026-04-08", "3211"),
    PlantPmBaseline("PM-1001-0012", "1001-SGR-501",
                    "60-day wheel dress and spindle runout check", 60, "IND-MECH-B", 3,
                    "2026-01-20", "2026-03-21", "3212"),
    PlantPmBaseline("PM-1001-0013", "1001-CGR-502",
                    "60-day wheel dress and spindle runout check", 60, "IND-MECH-B", 3,
                    "2026-01-22", "2026-03-23", "3213"),
    PlantPmBaseline("PM-1001-0014", "1001-CVA-100",
                    "30-day belt tension and roller inspection", 30, "IND-MECH-B", 3,
                    "2026-02-02", "2026-03-04", "3214"),
    PlantPmBaseline("PM-1001-0015", "1001-TPS-100",
                    "180-day probe calibration and qualification", 180, "IND-INST-A", 3,
                    "2025-12-10", "2026-06-08", "3215"),
)
# The reserved CLOSE work orders the live story adds for the aligned non-story
# plans (what the Delete removes). Also what the collision test proves unique.
PLANT_PM_CLOSE_WONUMS = tuple(b.close_wonum for b in PLANT_PM_BASELINE)
# The plant's routine stops in the history (the MES downtime bridge's
# ordinary traffic): MECHANICAL / ELECTRICAL stops on the earliest history
# runs of the OTHER parts on lines 1 and 2 (no batch the story traces), each
# resolved by one of the MES technicians
# (MESSimulator.DOWNTIME_RESOLVERS), and the order the bridge raised for each,
# already completed the way the simulator completes one once its loop has
# ended (MESSimulator.complete_bridge_work_orders): worked in the stop, its
# COMPLETE log entry by whoever resolved it. A long-running account collects
# such pairs from story loop 1 on; an event account works in loop 0, so
# without these the site CMMS log and the MES share no technician login and no
# failure code. Never the story line, part, asset or failure reasons
# (MESSimulator.STORY_FAILURE_REASONS); only completed orders.
# (category, reason, minutes into its run the stop starts, its length in
# seconds, seconds after its start the bridge saw it). The live step draws a
# stop's start between its run's start and the cycle that inserts it, and the
# bridge raises the order in that cycle (its NOW()): so an order is reported
# after its stop started, before the stop ended for some and after for others.
PAST_STOPS = (
    ("MECHANICAL", "MECH-JAM", 23, 700, 240),
    ("ELECTRICAL", "PLC-COMM", 41, 495, 600),
    ("MECHANICAL", "BELT-SLIP", 17, 1145, 95),
    ("ELECTRICAL", "MOTOR-OVL", 52, 1310, 1500),
    ("MECHANICAL", "PUMP-FAIL", 34, 1650, 410),
    ("ELECTRICAL", "POWER-FLUCT", 28, 800, 900),
)
# Each stop's MES downtime id is fixed (a name-based UUID), so its bridge
# order's number (IND-<id>) and description are the same for every anchor: a
# rewrite on a new anchor moves the order's dates (the CMMS upsert), and the
# retire of the history runs takes the old era's stops with them.
_PAST_STOP_NAMESPACE = uuid.UUID("5640df4c-1d4a-4bdb-b122-b0c38609a3b4")
PAST_STOP_DOWNTIME_IDS = tuple(str(uuid.uuid5(_PAST_STOP_NAMESPACE, f"stop-{i}"))
                               for i in range(len(PAST_STOPS)))
# Their bridge orders in the site CMMS (what the Delete removes).
PAST_STOP_WONUMS = tuple(MESSimulator._bridge_wonum(i) for i in PAST_STOP_DOWNTIME_IDS)
SUPERSEDED_LOG_ENTRY = "Superseded by the live plant history"
SUPERSEDED_LOG_AUTHOR = "Maintenance planner"
# Frozen-era orders on the story asset in these (open) statuses are
# superseded; the log entry records the prior status so Delete reopens it
# EXACTLY. The bare SUPERSEDED_LOG_ENTRY is the pre-status form (WAPPR only).
OPEN_STATUSES = ("WAPPR", "APPR", "INPRG", "WMATL")
SUPERSEDED_DESCRIPTION_SUFFIX = " [superseded by the live plant history]"
# Frozen-era COMPLETED orders on the story asset cite frozen-story ids the
# live plant does not have; they stay (history) but are marked prior-era.
PRIOR_ERA_PREFIX = "[Prior CMMS era, before the live history] "
DESCRIPTION_WIDTH = 250                 # cmms_indianapolis.work_orders.description VARCHAR(250)


def superseded_log_entry(prior_status: str) -> str:
    """The supersede log entry for an order that was ``prior_status``."""
    return f"{SUPERSEDED_LOG_ENTRY} (status was {prior_status})"
# The simulation_config contract keys this writer owns (and Delete clears).
CONFIG_KEYS = (
    "story_anchor_epoch", "story_loop_seconds", "story_interval_seconds",
    "story_bpfo_alarm_step", "story_fail_step", "story_repair_steps",
    "story_history_days", "story_failed_run_number", "story_deferred_pm_wonum",
    # one clock (ADR-0089): anchor provenance, and the anchor the story OPC UA
    # server's feed follows as last confirmed by the simulator's mirror
    # (erp_simulation/story_anchor_mirror.py, ADR-0092)
    "story_anchor_source", "story_telemetry_anchor", "story_telemetry_cursor_epoch",
)
# The reset failsafe (ADR-0091 Design C): the anchor the story-history CR
# planned onto (epoch) and its source (packaged | deploy). NOT plan rows (a
# placement upserts the plan, so it never moves them); written by the CR
# through ``story_replan.write_default_anchor`` and cleared with the story.
DEFAULT_KEYS = ("story_default_anchor", "story_default_source")
# The catch-up of the story OPC UA server after a placement (ADR-0092
# amendment 2026-10-02): "<previous anchor>;<start epoch>;<end epoch>",
# written by story_replan on every anchor move and copied by the anchor
# mirror. NOT a plan row; cleared with the story.
CATCH_UP_KEY = "story_feed_catch_up"
# The one-time history replay (the replay ADR): the story OPC UA server
# replays the history window once on its history nodes at the first placement
# out of the HOLD. ``story_replan`` records the WINDOW as a fact
# ("<H>;<from>;<to>") on a full re-plan whose previous era was unplaced, the
# same way it records the catch-up; the anchor mirror copies it to the second
# SSM parameter with the replay's identity appended, and records it once as
# ``story_feed_live_since``. NOT plan rows; a Reset clears both
# (:func:`clear_backfill`), and they are cleared with the story.
BACKFILL_KEY = "story_feed_backfill"
LIVE_SINCE_KEY = "story_feed_live_since"
# The second clock (ADR-0095): the anchor the LIVE side (condition monitor,
# story step, sensor feed, phase chip) follows when the bearing alarm was
# moved WITHOUT rewriting the history. Absent = one clock (the live side
# follows ``story_anchor_epoch``). Written only by ``story_replan.replan_live``,
# cleared by every full re-plan and with the story. NOT a plan row.
LIVE_ANCHOR_KEY = "story_live_anchor_epoch"
# How the stored anchor was placed: the Create time, a packaged
# ``--story-anchor``, or a steward placement (erp_simulation/story_replan.py).
ANCHOR_SOURCES = ("deploy", "packaged", "placed")

DAY = 86400
SITEWISE_WINDOW_SECONDS = 7 * DAY       # BatchPut rejects values older than 7 days
RUNS_PER_DAY = 6
RUN_SECONDS = 5400                      # 90 min per machining run
RUN_SPACING = 13500                     # a run every 3 h 45 min, from 00:30 into the day
BATCH = 60                              # units per run
# The failed batch sits INSIDE the BPFO-alarm window. Its offsets are authored
# against the shipped 8 h window (6430 s) and scale with a packaged loop
# (ADR-0091), floored to whole minutes: identical with STORY_LOOP_SECONDS unset.
_SHIPPED_FAIL_STEP = int(cm.DEFAULT_LOOP_SECONDS // cm.INTERVAL_SEC * cm.FAIL_FRACTION)
SHIPPED_ALARM_WINDOW_SECONDS = (_SHIPPED_FAIL_STEP - cm.bpfo_alarm_step((_SHIPPED_FAIL_STEP, 0))) * cm.INTERVAL_SEC
ALARM_WINDOW_SECONDS = (cm.BEARING_FAIL_STEP - cm.BPFO_ALARM_STEP) * cm.INTERVAL_SEC


def _window_scaled(seconds: int) -> int:
    """``seconds`` of the shipped alarm window, scaled to this loop's (minutes)."""
    return seconds * ALARM_WINDOW_SECONDS // SHIPPED_ALARM_WINDOW_SECONDS // 60 * 60


FAILED_OFFSET = _window_scaled(600)     # failed batch starts 10 min into the BPFO window (8 h)
FAILED_CHECK_OFFSETS = tuple(_window_scaled(s) for s in (1800, 3600, 5100))
FAILED_RUN_SECONDS = _window_scaled(RUN_SECONDS)  # 90 min (8 h): ends before the bearing stops
CHECK_SAMPLE = 20
ORDER_QUANTITIES = (12, 10, 8)
ORDER_STATUSES = ("DELIVERED", "DELIVERED", "SHIPPED")
RUN_ORDER_QUANTITY = BATCH // 3         # one order per completed history run
RUN_ORDER_LEAD = DAY                    # ordered a day before the run started
RUN_SHIP_DELAY = 3600                   # shipped an hour after the run ended
RUN_DELIVERY_DELAY = 12 * 3600          # delivered half a day later ...
DELIVERY_MARGIN = 600                   # ... but always before the anchor
MAX_DAYS = 9                            # one suffix digit per simulated day


@dataclass(frozen=True)
class CheckPlan:
    check_type: str
    check_time: datetime
    sample_size: int
    pass_count: int
    fail_count: int
    notes: str


@dataclass(frozen=True)
class RunPlan:
    run_number: str
    work_order_number: str
    sku: str
    line: str
    start: datetime
    end: datetime
    planned: int
    good: int
    reject: int
    checks: Tuple[CheckPlan, ...]
    lot_number: str
    lot_quantity: float
    stop_reason: Optional[str] = None


@dataclass(frozen=True)
class DowntimePlan:
    work_order_number: str
    line: str
    category: str
    reason: str
    start: datetime
    end: datetime
    notes: str


@dataclass(frozen=True)
class OrderPlan:
    order_number: str
    shipment_number: str
    customer_code: str
    sku: str
    quantity: int
    status: str
    order_date: datetime
    ship_date: datetime
    delivered_date: Optional[datetime]
    batch_id: str


@dataclass(frozen=True)
class CmmsWorkOrderPlan:
    wonum: str
    work_type: str
    description: str
    status: str
    priority: int
    pm_num: Optional[str]
    reported_by: str
    reported_date: datetime
    target_start: datetime
    sched_start: datetime
    actual_start: Optional[datetime]
    actual_finish: Optional[datetime]
    failure_code: Optional[str]


@dataclass(frozen=True)
class CmmsLogPlan:
    wonum: str
    log_date: datetime
    author: str
    log_type: str
    entry: str


@dataclass(frozen=True)
class ConfigRow:
    key: str
    value: str
    data_type: str
    description: str


@dataclass(frozen=True)
class LotPlan:
    """A supplier lot the history itself writes (fully drawn by its runs)."""
    lot_number: str
    item_code: str
    supplier_code: str
    received: datetime
    quantity: float


@dataclass(frozen=True)
class PlantPmPlan:
    """A non-story Indianapolis PM schedule aligned to the live story run: its
    frozen 011 dates shifted so the plan keeps straddling the run (last done
    before it, next due after it), plus the CLOSE work order for that shifted
    last-completion cycle. Built from a :class:`PlantPmBaseline`."""
    pm_num: str
    asset_num: str
    last_completion: date
    next_due: date
    close: CmmsWorkOrderPlan


@dataclass(frozen=True)
class PastStopPlan:
    """One of the plant's routine stops (:data:`PAST_STOPS`) on a history run,
    and the MES-bridge order it raised (``order``, completed) with that
    order's COMPLETE log entry (``log``, by whoever resolved the stop)."""
    downtime_id: str
    run_number: str
    work_order_number: str
    line: str
    category: str
    reason: str
    start: datetime
    end: datetime
    resolved_by: str
    notes: str
    order: CmmsWorkOrderPlan
    log: CmmsLogPlan
    observed: datetime


@dataclass(frozen=True)
class StoryPlan:
    anchor_epoch: float
    days: int
    config: Tuple[ConfigRow, ...]
    runs: Tuple[RunPlan, ...]
    failed_run: RunPlan
    downtime: DowntimePlan
    orders: Tuple[OrderPlan, ...]
    run_orders: Tuple[OrderPlan, ...]
    cmms_work_orders: Tuple[CmmsWorkOrderPlan, ...]
    cmms_log: Tuple[CmmsLogPlan, ...]
    lots: Tuple[LotPlan, ...] = ()
    plant_pm: Tuple[PlantPmPlan, ...] = ()
    past_stops: Tuple[PastStopPlan, ...] = ()


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------

def ts(epoch: float) -> datetime:
    """Naive UTC timestamp (the ERP and seed databases run in UTC)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def story_windows(anchor: float) -> Dict[str, float]:
    """Epochs of the story loop that contains ``anchor - 1 day``: its start,
    the BPFO alarm, the bearing failure and the end of the repair."""
    loop_start = cm.loop_start_epoch(anchor - DAY, anchor)
    return {
        "loop_start": loop_start,
        "alarm": loop_start + cm.BPFO_ALARM_STEP * cm.INTERVAL_SEC,
        "fail": loop_start + cm.BEARING_FAIL_STEP * cm.INTERVAL_SEC,
        "repaired": loop_start + (cm.BEARING_FAIL_STEP + cm.BEARING_REPAIR_STEPS) * cm.INTERVAL_SEC,
    }


# One-time history replay window (the replay ADR). On the first placement out
# of the HOLD the story OPC UA server replays the history ONCE on its history
# nodes; story_replan records this window as the fact "<H>;<from>;<to>" (like
# the catch-up), and the anchor mirror copies it to the second SSM parameter.
BACKFILL_DAYS = 3  # the shipped history depth (plan's default days)
# The deepest window, measured back from the placement: the history depth plus
# one loop, since a placement's anchor is normalised into the loop before it
# (story_replan.normalize_anchor). An older anchor is cut to this depth rather
# than widening the replay (at most 961 points, about 32 min at a 2 s pace).
BACKFILL_MAX_DEPTH_SECONDS = BACKFILL_DAYS * DAY + cm.TOTAL_SECONDS
# How long after the placement every replayed point is still inside SiteWise's
# ingest window ([-7 d, +10 min], inclusive) when it is sent: a day covers the
# server's pickup, the replay, the done grace and repeated restarts many times.
REPLAY_HORIZON_SECONDS = DAY


def backfill_window(anchor: float, now: float) -> Tuple[int, int]:
    """``(from, to)`` of the history replay for a placement at ``now``. ``to``
    is ``grid_floor(now)``; ``from`` is the first epoch on the anchor's grid at
    or after the latest of: :data:`BACKFILL_DAYS` days before the anchor (the
    story's depth), :data:`BACKFILL_MAX_DEPTH_SECONDS` before ``now`` (never
    deeper), and the oldest time SiteWise still accepts
    :data:`REPLAY_HORIZON_SECONDS` after ``now``."""
    oldest = max(anchor - BACKFILL_DAYS * DAY, now - BACKFILL_MAX_DEPTH_SECONDS,
                 now + REPLAY_HORIZON_SECONDS - SITEWISE_WINDOW_SECONDS)
    grid = cm.TELEMETRY_GRID_SECONDS
    return int(anchor - ((anchor - oldest) // grid) * grid), cm.grid_floor(now)


def backfill_row(anchor: float, now: float) -> "ConfigRow":
    """The :data:`BACKFILL_KEY` row for a placement at ``now`` (:func:`backfill_window`);
    the anchor mirror turns it into the second SSM parameter's value."""
    frm, to = backfill_window(anchor, now)
    return ConfigRow(BACKFILL_KEY, f"{int(anchor)};{frm};{to}", "string",
                     "OPC UA one-time history replay window: anchor;from;to")


def clear_backfill(cur) -> int:
    """Delete the replay window and its identity (:data:`BACKFILL_KEY`,
    :data:`LIVE_SINCE_KEY`) in the caller's transaction; returns rows deleted.
    A Reset calls it, so the mirror sets the parameter back to ``pending`` and
    the next placement records a fresh window with a fresh identity."""
    cur.execute("DELETE FROM simulation_config WHERE config_key = ANY(%s)", ([BACKFILL_KEY, LIVE_SINCE_KEY],))
    return max(cur.rowcount or 0, 0)


def _numbers(start: datetime, suffix: str) -> Tuple[str, str]:
    day = start.strftime("%Y%m%d")
    return f"RUN-{day}-{suffix}", f"WO-{day}-{suffix}"


def _normal_run(anchor: float, day: int, k: int) -> RunPlan:
    """Run ``k`` of simulated day ``day`` (1 = the day before the anchor)."""
    start_epoch = anchor - day * DAY + 1800 + k * RUN_SPACING
    start, end = ts(start_epoch), ts(start_epoch + RUN_SECONDS)
    sku, lot, kg = PRODUCTS[(k + day) % len(PRODUCTS)]
    run_number, wo_number = _numbers(start, f"9{day}{k:03d}")
    visual = CheckPlan("VISUAL", ts(start_epoch + RUN_SECONDS // 2), 50, 50, 0,
                       "Routine visual and surface-finish check")
    dimensional = CheckPlan("DIMENSIONAL", ts(start_epoch + RUN_SECONDS // 3), CHECK_SAMPLE,
                            CHECK_SAMPLE, 0, "SPC bore diameter and runout within tolerance")
    return RunPlan(run_number, wo_number, sku, LINES[k % len(LINES)], start, end,
                   BATCH, BATCH, 0, (dimensional, visual), lot, round(kg * BATCH, 2))


def _split_lots(runs: Tuple[RunPlan, ...], days: int) -> Tuple[RunPlan, ...]:
    """The failed part's runs on the earliest simulated day draw
    :data:`EARLIER_LOT_NUMBER` and its later runs :data:`SUSPECT_LOT_NUMBER`;
    the other parts keep their db-init lots. A one-day history has no earlier
    day, so all of its runs of the part draw the suspect lot."""
    def lot_of(i: int, r: RunPlan) -> RunPlan:
        if r.sku != FAILED_SKU:
            return r
        earlier = days >= 2 and i < RUNS_PER_DAY
        return replace(r, lot_number=EARLIER_LOT_NUMBER if earlier else SUSPECT_LOT_NUMBER)
    return tuple(lot_of(i, r) for i, r in enumerate(runs))


def _history_lots(runs: Tuple[RunPlan, ...]) -> Tuple[LotPlan, ...]:
    """The history's own lots, each sized to exactly what its runs drew:
    the earlier lot (when the history has an earlier day) and the suspect lot,
    received :data:`SUSPECT_LOT_LEAD_SECONDS` before its first run."""
    lots = []
    for number, lead in ((EARLIER_LOT_NUMBER, EARLIER_LOT_LEAD_DAYS * DAY),
                         (SUSPECT_LOT_NUMBER, SUSPECT_LOT_LEAD_SECONDS)):
        drawn = sorted((r for r in runs if r.lot_number == number), key=lambda r: r.start)
        if drawn:
            lots.append(LotPlan(number, FAILED_MATERIAL_CODE, HISTORY_LOT_SUPPLIER,
                                ts(_epoch(drawn[0].start) - lead),
                                round(sum(r.lot_quantity for r in drawn), 2)))
    return tuple(lots)


def _failed_check(start_epoch: float, window_start: float, offset: int) -> CheckPlan:
    """A DIMENSIONAL check whose fail share follows the live defect rule."""
    step = int((start_epoch + offset - window_start) // cm.INTERVAL_SEC)
    failed = round(CHECK_SAMPLE * cm.defect_reject_fraction(step))
    bpfo = float(cm.spectral_features(step)["bpfo"])
    notes = f"SPC bore diameter drift on {cm.STORY_MES_EQUIPMENT_ID}: BPFO band {bpfo:.2f} mm/s"
    return CheckPlan("DIMENSIONAL", ts(start_epoch + offset), CHECK_SAMPLE,
                     CHECK_SAMPLE - failed, failed, notes)


def _failed_run(anchor: float) -> RunPlan:
    """THE failed batch: line 3, inside the BPFO-alarm window, ~30% rejects."""
    win = story_windows(anchor)
    start_epoch = win["alarm"] + FAILED_OFFSET
    checks = tuple(_failed_check(start_epoch, win["loop_start"], off) for off in FAILED_CHECK_OFFSETS)
    failed = sum(c.fail_count for c in checks)
    sampled = sum(c.sample_size for c in checks)
    reject = round(BATCH * failed / sampled)
    start = ts(start_epoch)
    run_number, wo_number = _numbers(start, "90001")
    kg = PRODUCTS[0][2]
    return RunPlan(run_number, wo_number, FAILED_SKU, cm.STORY_MES_LINE, start,
                   ts(start_epoch + FAILED_RUN_SECONDS), BATCH, BATCH - reject, reject, checks,
                   SUSPECT_LOT_NUMBER, round(kg * BATCH, 2),
                   stop_reason=f"SPC bore diameter drift on {cm.STORY_MES_EQUIPMENT_ID}")


def _downtime(anchor: float, failed: RunPlan) -> DowntimePlan:
    win = story_windows(anchor)
    return DowntimePlan(
        failed.work_order_number, cm.STORY_MES_LINE, "MECHANICAL", "VIB-BEARING",
        ts(win["fail"]), ts(win["repaired"]),
        f"Spindle bearing failed on {cm.STORY_MES_EQUIPMENT_ID}; bearing set replaced")


def _epoch(naive_utc: datetime) -> float:
    return naive_utc.replace(tzinfo=timezone.utc).timestamp()


def _orders(failed: RunPlan) -> Tuple[OrderPlan, ...]:
    """Three MACHINING customers ordered the failed part and got that batch:
    ordered 1.5 / 1 / 0.5 days before it ran, shipped 1 / 2 / 3 h after it."""
    start, end = _epoch(failed.start), _epoch(failed.end)
    out = []
    for i, (code, qty, status) in enumerate(zip(STORY_CUSTOMER_CODES, ORDER_QUANTITIES, ORDER_STATUSES), 1):
        ordered, ship = start - (4 - i) * DAY / 2, end + i * 3600
        suffix = f"{90000000 + i}"
        out.append(OrderPlan(
            f"ORD-{ts(ordered):%Y%m%d}-{suffix}", f"SHIP-{ts(ship):%Y%m%d}-{suffix}",
            code, failed.sku, qty, status, ts(ordered), ts(ship),
            ts(ship + 12 * 3600) if status == "DELIVERED" else None, failed.run_number))
    return tuple(out)


def _run_order(anchor: float, index: int, run: RunPlan) -> OrderPlan:
    """The ONE DELIVERED order a completed history run shipped on: the site's
    customers in rotation, the run number as the shipment line's batch id."""
    ordered, ship = _epoch(run.start) - RUN_ORDER_LEAD, _epoch(run.end) + RUN_SHIP_DELAY
    delivered = min(ship + RUN_DELIVERY_DELAY, anchor - DELIVERY_MARGIN)
    suffix = run.run_number.rsplit("-", 1)[1] + "001"
    return OrderPlan(
        f"ORD-{ts(ordered):%Y%m%d}-{suffix}", f"SHIP-{ts(ship):%Y%m%d}-{suffix}",
        SITE_CUSTOMER_CODES[index % len(SITE_CUSTOMER_CODES)], run.sku, RUN_ORDER_QUANTITY,
        "DELIVERED", ts(ordered), ts(ship), ts(delivered), run.run_number)


def _run_orders(anchor: float, runs: Tuple[RunPlan, ...]) -> Tuple[OrderPlan, ...]:
    """A plant ships what it makes: every completed history run, once."""
    return tuple(_run_order(anchor, i, run) for i, run in enumerate(runs))


def _deferred_pm(anchor: float, failed: RunPlan) -> CmmsWorkOrderPlan:
    target = _epoch(failed.start) - 5 * DAY
    return CmmsWorkOrderPlan(
        DEFERRED_PM_WONUM, "PM",
        "90-day spindle bearing inspection, lubrication and preload check - DEFERRED at "
        "production planning request (Line 3 at full loading)",
        "WAPPR", 2, STORY_PM_NUM, "Maintenance planner", ts(target - 8 * DAY), ts(target),
        ts(anchor + 2 * DAY), None, None, None)


def _last_completed_pm(deferred: CmmsWorkOrderPlan) -> CmmsWorkOrderPlan:
    """The story PM's previous cycle, done on time one frequency before the
    deferred PM's target (the PM schedule's last completion date): raised a
    week ahead, worked on its target day like the 011 seed's cadence PMs."""
    day = datetime.combine(deferred.target_start.date() - timedelta(days=PM_FREQUENCY_DAYS),
                           datetime.min.time())
    hour = timedelta(hours=1)
    return CmmsWorkOrderPlan(
        LAST_PM_WONUM, "PM", "90-day spindle bearing inspection, lubrication and preload check",
        "CLOSE", 2, STORY_PM_NUM, "Maintenance planner", day - timedelta(days=7) + 7 * hour,
        day + 6 * hour, day + 6 * hour, day + 6.25 * hour, day + 14.5 * hour, None)


def plant_pm_shift(failed: RunPlan) -> timedelta:
    """How far the non-story plant PM schedules move: the live story run day
    minus the frozen B-2847 run day. Every 011 non-story row straddles the
    frozen run (last done before it, next due after it), so shifting by this
    keeps that straddle against the live run for any anchor, loop or history
    length (the run day is the only thing that moves)."""
    return failed.start.date() - FROZEN_STORY_RUN_DATE


def _plant_close_order(base: PlantPmBaseline, last: date) -> CmmsWorkOrderPlan:
    """The CLOSE work order for a non-story plan's shifted last-completion
    cycle, in the same shape as the story PM's last cycle (work order 3064,
    :func:`_last_completed_pm`): raised a week ahead, worked on its target day
    (``actual_finish`` on ``last``), the plan's own description/crew/priority."""
    day = datetime.combine(last, datetime.min.time())
    hour = timedelta(hours=1)
    return CmmsWorkOrderPlan(
        base.close_wonum, "PM", base.description, "CLOSE", base.priority, base.pm_num,
        "Maintenance planner", day - timedelta(days=7) + 7 * hour, day + 6 * hour,
        day + 6 * hour, day + 6.25 * hour, day + 14.5 * hour, None)


def _plant_pm(failed: RunPlan) -> Tuple[PlantPmPlan, ...]:
    """Every non-story Indianapolis PM schedule, shifted onto the live run and
    carrying the CLOSE work order of its shifted last-completion cycle."""
    shift = plant_pm_shift(failed)
    out = []
    for base in PLANT_PM_BASELINE:
        last = date.fromisoformat(base.last_completion) + shift
        due = date.fromisoformat(base.next_due) + shift
        out.append(PlantPmPlan(base.pm_num, base.asset_num, last, due,
                               _plant_close_order(base, last)))
    return tuple(out)


def _repair_orders(anchor: float) -> Tuple[CmmsWorkOrderPlan, ...]:
    win = story_windows(anchor)
    alarm_features = cm.spectral_features(cm.BPFO_ALARM_STEP)
    condition = CmmsWorkOrderPlan(
        CONDITION_WONUM, "CM", MESSimulator._condition_work_order_description(alarm_features),
        "COMP", 1, None, "Condition monitor", ts(win["alarm"]), ts(win["alarm"]), ts(win["fail"]),
        ts(win["fail"]), ts(win["repaired"]), "BEARING")
    repair = CmmsWorkOrderPlan(
        REPAIR_WONUM, "EM",
        f"Spindle bearing set failed on {cm.STORY_CMMS_ASSET_NUM} after the BPFO alarm (work order "
        f"{CONDITION_WONUM}). Replace bearing set, realign spindle, verify runout. Root cause: "
        f"deferred 90-day bearing PM (work order {DEFERRED_PM_WONUM}).",
        "COMP", 1, None, "Line 3 operator", ts(win["fail"]), ts(win["fail"]), ts(win["fail"]),
        ts(win["fail"]), ts(win["repaired"]), "BEARING")
    return condition, repair


def _create_log(pm: CmmsWorkOrderPlan) -> CmmsLogPlan:
    """The deferred PM's creation entry, dated when it was raised (its
    ``reported_date``), in the 011 seed's CREATE wording: the log then dates
    creation and deferral apart, as the frozen seed's own deferred PM does.
    The last completion is the previous cycle's (:func:`_last_completed_pm`)."""
    last = pm.target_start.date() - timedelta(days=PM_FREQUENCY_DAYS)
    return CmmsLogPlan(
        pm.wonum, pm.reported_date, pm.reported_by, "CREATE",
        f"PM work order generated from {pm.pm_num}. Due {pm.target_start:%Y-%m-%d} per "
        f"{PM_FREQUENCY_DAYS}-day cycle (last completed {last:%Y-%m-%d}).")


def _cmms_log(pm: CmmsWorkOrderPlan) -> Tuple[CmmsLogPlan, ...]:
    return (_create_log(pm), CmmsLogPlan(
        pm.wonum, ts(_epoch(pm.target_start) - 2 * DAY), "Production planning", "DEFERRAL",
        f"Deferred at production planning request - Line 3 at full loading; approval withheld. "
        f"Rescheduled to {pm.sched_start:%Y-%m-%d}. Risk noted: spindle bearing PM will exceed "
        f"the 90-day interval."))


def _past_stop(i: int, run: RunPlan) -> PastStopPlan:
    """Stop ``i`` of :data:`PAST_STOPS` on ``run``, resolved by the ``i``-th
    technician in turn, and its bridge order exactly as the bridge raises it
    (``MESSimulator.indianapolis_bridge_order``, reported when the bridge saw
    the stop) and as the simulator completes it (worked from the stop's start
    to its end, one COMPLETE entry by the resolver dated
    ``GREATEST(actual_finish, reported_date)``, the bridge's completion text)."""
    category, reason, minutes, seconds, seen_after = PAST_STOPS[i]
    downtime_id = PAST_STOP_DOWNTIME_IDS[i]
    start = ts(_epoch(run.start) + minutes * 60)
    end = ts(_epoch(start) + seconds)
    observed = ts(_epoch(start) + seen_after)
    resolver = MESSimulator.DOWNTIME_RESOLVERS[i % len(MESSimulator.DOWNTIME_RESOLVERS)]
    wonum, work_type, priority, description, failure_code = MESSimulator.indianapolis_bridge_order(
        SITE, run.line, category, reason, downtime_id)
    order = CmmsWorkOrderPlan(
        wonum, work_type, description, MESSimulator.CONDITION_COMPLETED_STATUS, priority, None,
        MESSimulator.CMMS_BRIDGE_REPORTED_BY, observed, observed, observed, start, end, failure_code)
    log = CmmsLogPlan(wonum, max(end, observed), resolver, "COMPLETE", MESSimulator.BRIDGE_COMPLETION_ENTRY)
    return PastStopPlan(downtime_id, run.run_number, run.work_order_number, run.line, category, reason,
                        start, end, resolver, f"Resolved after {downtime_length_text(seconds)}", order, log,
                        observed)


def _past_stops(runs: Tuple[RunPlan, ...]) -> Tuple[PastStopPlan, ...]:
    """The plant's routine stops, one per run, on the earliest history runs
    (``runs`` is in time order) that are neither on the story line nor of the
    failed part: no stop sits on a batch the story traces (the failed part's
    batches, its lots, the shipments of it). A short history keeps the first
    stops."""
    hosts = [r for r in runs if r.line != cm.STORY_MES_LINE and r.sku != FAILED_SKU][:len(PAST_STOPS)]
    return tuple(_past_stop(i, run) for i, run in enumerate(hosts))


def telemetry_cursor(anchor: float, days: int, now: Optional[float] = None) -> int:
    """The feed-confirmation cursor of a fresh era: one grid point BEFORE the
    history's first grid point, so the lookup reads "not confirmed yet" until
    the simulator's anchor mirror confirms the new anchor (ADR-0092).

    With ``now`` it is clamped to no earlier than ``grid_floor(now - 7 d +
    300)`` (the bound the retired SiteWise sweep needed; kept so stored
    values stay comparable). ``now=None``: unclamped (pure)."""
    cursor = cm.grid_floor(anchor - days * DAY) - cm.TELEMETRY_GRID_SECONDS
    if now is None:
        return cursor
    return max(cursor, cm.grid_floor(now - SITEWISE_WINDOW_SECONDS + cm.TELEMETRY_GRID_SECONDS))


def _clock_rows(anchor: float, days: int, source: str,
                now: Optional[float] = None) -> Tuple[Tuple[str, str, str, str], ...]:
    """The one-clock config rows: provenance and the feed-confirmation state
    (reset per era; the simulator's anchor mirror confirms it, ADR-0092)."""
    return (
        ("story_anchor_source", source, "string", "How the story anchor was placed"),
        ("story_telemetry_anchor", repr(float(anchor)), "float", "Anchor the story sensor feed follows"),
        ("story_telemetry_cursor_epoch", str(telemetry_cursor(anchor, days, now)), "int",
         "Last grid point at which the sensor feed confirmed the anchor"),
    )


def _config(anchor: float, days: int, failed: RunPlan, source: str = "deploy",
            now: Optional[float] = None) -> Tuple[ConfigRow, ...]:
    geo = cm.story_geometry()
    rows = [
        ("story_anchor_epoch", repr(float(anchor)), "float", "Live story clock anchor (epoch seconds)"),
        ("story_loop_seconds", str(geo["loop_seconds"]), "int", "Condition-monitor loop length"),
        ("story_interval_seconds", str(geo["interval_seconds"]), "int", "Condition-monitor step"),
        ("story_bpfo_alarm_step", str(geo["bpfo_alarm_step"]), "int", "Loop step of the BPFO alarm"),
        ("story_fail_step", str(geo["fail_step"]), "int", "Loop step of the bearing failure"),
        ("story_repair_steps", str(geo["repair_steps"]), "int", "Steps of the bearing repair"),
        ("story_history_days", str(days), "int", "Simulated days written before the anchor"),
        ("story_failed_run_number", failed.run_number, "string", "History failed batch (run number)"),
        ("story_deferred_pm_wonum", DEFERRED_PM_WONUM, "string", "History deferred PM work order"),
        *_clock_rows(anchor, days, source, now),
    ]
    return tuple(ConfigRow(*row) for row in rows)


def plan(anchor_epoch: float, days: int = 3, *, source: str = "deploy",
         now: Optional[float] = None) -> StoryPlan:
    """The full deterministic history for ``anchor_epoch`` (epoch seconds > 0);
    ``source`` (:data:`ANCHOR_SOURCES`) is recorded as ``story_anchor_source``;
    ``now`` (the writer's clock) clamps the telemetry cursor into SiteWise's
    window (:func:`telemetry_cursor`; None: unclamped)."""
    anchor = float(anchor_epoch)
    if anchor <= 0:
        raise ValueError(f"anchor_epoch must be positive, got {anchor_epoch!r}")
    if not 1 <= int(days) <= MAX_DAYS:
        raise ValueError(f"days must be 1..{MAX_DAYS}, got {days!r}")
    if source not in ANCHOR_SOURCES:
        raise ValueError(f"source must be one of {ANCHOR_SOURCES}, got {source!r}")
    days = int(days)
    runs = _split_lots(tuple(_normal_run(anchor, d, k) for d in range(days, 0, -1)
                             for k in range(RUNS_PER_DAY)), days)
    failed = _failed_run(anchor)
    pm = _deferred_pm(anchor, failed)
    return StoryPlan(
        anchor, days, _config(anchor, days, failed, source, now), runs, failed, _downtime(anchor, failed),
        _orders(failed), _run_orders(anchor, runs), (_last_completed_pm(pm), pm, *_repair_orders(anchor)),
        _cmms_log(pm), _history_lots((*runs, failed)), _plant_pm(failed), _past_stops(runs))


# ---------------------------------------------------------------------------
# Idempotent writer
# ---------------------------------------------------------------------------

def read_stored_anchor(erp_conn) -> Optional[float]:
    """The stored ``story_anchor_epoch`` or None. A malformed value raises."""
    with erp_conn.cursor() as cur:
        cur.execute("SELECT config_value FROM simulation_config WHERE config_key = %s",
                    ("story_anchor_epoch",))
        row = cur.fetchone()
    return float(row[0]) if row else None


def write(erp_conn, seed_conn, story_plan: StoryPlan) -> Dict[str, object]:
    """Write ``story_plan`` (or its rebuild from a STORED anchor) to both DBs.

    One transaction per database, ERP first (see the module docstring for
    why); any error rolls that database back and re-raises. Returns the
    anchor, the failed run and per-table insert counts.
    """
    stored = read_stored_anchor(erp_conn)
    if stored is not None and stored != story_plan.anchor_epoch:
        story_plan = plan(stored, story_plan.days)
    skipped: list = []
    erp = _in_transaction(erp_conn, lambda cur, sp: _write_erp(cur, sp, skipped), story_plan)
    # A past stop whose run another run holds was not written to the MES, so
    # its bridge order is not written to the CMMS either.
    seed_plan = replace(story_plan, past_stops=tuple(
        s for s in story_plan.past_stops if s.run_number not in skipped))
    seed = _in_transaction(seed_conn, _write_seed, seed_plan)
    return {"anchor_epoch": story_plan.anchor_epoch,
            "failed_run_number": story_plan.failed_run.run_number,
            "erp": erp, "seed": seed}


def without_held_stops(cur, sp: StoryPlan) -> StoryPlan:
    """``sp`` without the past stops whose run another run holds (the ERP
    writer leaves those runs and their stops out), read on the ERP cursor
    ``cur``: what the seed side may write. ``sp`` itself when none is held."""
    if not sp.past_stops:
        return sp
    cur.execute(_HELD_ELSEWHERE_SQL, ([s.work_order_number for s in sp.past_stops],
                                      [s.run_number for s in sp.past_stops]))
    held = {run_number for _wo, run_number in cur.fetchall()}
    if not held:
        return sp
    return replace(sp, past_stops=tuple(s for s in sp.past_stops if s.run_number not in held))


def _in_transaction(conn, fn, arg) -> Dict[str, int]:
    try:
        with conn.cursor() as cur:
            counts = fn(cur, arg)
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise


def clear_story_config(erp_conn) -> int:
    """Delete every ``story_*`` contract key (:data:`CONFIG_KEYS`, the
    reset-failsafe :data:`DEFAULT_KEYS`, :data:`CATCH_UP_KEY`, the backfill
    fact :data:`BACKFILL_KEY` / :data:`LIVE_SINCE_KEY` and the live
    anchor :data:`LIVE_ANCHOR_KEY`); returns
    rows deleted. The history rows stay. Rolls back and re-raises on error."""
    try:
        with erp_conn.cursor() as cur:
            cur.execute("DELETE FROM simulation_config WHERE config_key = ANY(%s)",
                        (list(CONFIG_KEYS + DEFAULT_KEYS
                              + (CATCH_UP_KEY, LIVE_ANCHOR_KEY, BACKFILL_KEY, LIVE_SINCE_KEY)),))
            deleted = max(cur.rowcount or 0, 0)
        erp_conn.commit()
        return deleted
    except Exception:
        erp_conn.rollback()
        raise


def _write_erp(cur, sp: StoryPlan, skipped_out: Optional[list] = None) -> Dict[str, int]:
    """The ERP rows of ``sp``; ``skipped_out`` (when given) receives the run
    numbers left out because another run holds them."""
    _require_reference(cur, sp)
    skipped = _runs_held_elsewhere(cur, sp)
    if skipped_out is not None:
        skipped_out.extend(sorted(skipped))
    counts = {"config": 0, "runs": 0, "orders": 0}
    for row in sp.config:
        cur.execute(
            "INSERT INTO simulation_config (config_key, config_value, data_type, category, "
            "description, default_value) VALUES (%s, %s, %s, 'story', %s, %s) "
            "ON CONFLICT (config_key) DO NOTHING",
            (row.key, row.value, row.data_type, row.description, row.value))
        counts["config"] += max(cur.rowcount or 0, 0)
    for lot in sp.lots:
        _write_lot(cur, lot)
    for run in (*sp.runs, sp.failed_run):
        if run.run_number not in skipped:
            counts["runs"] += _write_run(cur, run, sp.downtime if run is sp.failed_run else None)
    prior_hosts = _past_stop_hosts(cur)
    stops = [stop for stop in sp.past_stops if stop.run_number not in skipped]
    _delete_omitted_past_stops(cur, stops)
    counts["past_stops"] = sum(_write_past_stop(cur, stop) for stop in stops)
    _align_past_stop_oee(cur, prior_hosts)
    for order in (*sp.orders, *sp.run_orders):
        if order.batch_id not in skipped:
            counts["orders"] += _write_order(cur, order)
    if skipped:
        counts["skipped_runs"] = len(skipped)
    return counts


class NumberCollision(RuntimeError):
    """The failed batch's MES work-order number is held by another run."""


# A planned run whose number, or whose MES work-order number, already belongs
# to ANOTHER run cannot be written: ON CONFLICT would skip it and its order
# would ship someone else's batch, or its children would have no parent work
# order. Only data from before ADR-0090 does this: the old random simulator
# numbering could land in the story's 9xxxx space. A fresh account never
# matches, and neither does this plan's own run on a same-anchor rerun.
_HELD_ELSEWHERE_SQL = """
    SELECT m.work_order_number, m.run_number
    FROM unnest(%s::varchar[], %s::varchar[]) AS m(work_order_number, run_number)
    WHERE EXISTS (
            SELECT 1 FROM mes_work_orders w
            LEFT JOIN production_runs pr ON pr.run_id = w.production_run_id
            WHERE w.work_order_number = m.work_order_number
              AND pr.run_number IS DISTINCT FROM m.run_number)
       OR EXISTS (
            SELECT 1 FROM production_runs pr
            WHERE pr.run_number = m.run_number
              AND NOT EXISTS (SELECT 1 FROM mes_work_orders w
                              WHERE w.production_run_id = pr.run_id
                                AND w.work_order_number = m.work_order_number))
"""


def _runs_held_elsewhere(cur, sp: StoryPlan) -> set:
    """Run numbers to leave out because another run holds their number or
    their work-order number. A normal run is skipped (with the order that
    ships it); the failed batch never is: :class:`NumberCollision` (nothing
    written)."""
    runs = (*sp.runs, sp.failed_run)
    cur.execute(_HELD_ELSEWHERE_SQL, ([r.work_order_number for r in runs], [r.run_number for r in runs]))
    held = {run_number: wo for wo, run_number in cur.fetchall()}
    if sp.failed_run.run_number in held:
        raise NumberCollision(f"story history: {sp.failed_run.run_number} / {held[sp.failed_run.run_number]} "
                              "(the failed batch) already belongs to another run; this account carries "
                              "pre-ADR-0090 simulator numbers in the story's range")
    if held:
        logger.warning(f"Story history: skipped {len(held)} run(s) whose number another run holds "
                       f"(pre-ADR-0090 data): {sorted(held)}")
    return set(held)


def _require_reference(cur, sp: StoryPlan) -> None:
    """Fail loudly when the one-ERP reference data the history joins to is absent."""
    planned_lots = {lot.lot_number for lot in sp.lots}
    checks = (
        ("products", "sku", sorted({r.sku for r in (*sp.runs, sp.failed_run)})),
        ("customers", "customer_code",
         sorted({o.customer_code for o in (*sp.orders, *sp.run_orders)})),
        ("material_lots", "lot_number",
         sorted({r.lot_number for r in (*sp.runs, sp.failed_run)} - planned_lots)),
        ("inventory_items", "item_code", sorted({lot.item_code for lot in sp.lots})),
        ("suppliers", "supplier_code", sorted({lot.supplier_code for lot in sp.lots})),
        ("inventory_locations", "location_code", [FG_LOCATION, RAW_LOCATION]),
    )
    for table, column, keys in checks:
        cur.execute(sql_text("SELECT {column} FROM {table} WHERE {column} = ANY(%s)", column=column, table=table),
                    (keys,))
        missing = set(keys) - {r[0] for r in cur.fetchall()}
        if missing:
            raise RuntimeError(f"story history: {table}.{column} missing {sorted(missing)}")


def _returning(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _write_run(cur, run: RunPlan, downtime: Optional[DowntimePlan]) -> int:
    """One completed run + its MES work order and children. 0 when it existed."""
    run_id = _returning(cur, """
        INSERT INTO production_runs (run_number, product_id, planned_quantity, actual_quantity, status,
            planned_start_time, actual_start_time, planned_end_time, actual_end_time, location_id,
            site, line, notes)
        SELECT %s, p.product_id, %s, %s, 'COMPLETED', %s, %s, %s, %s,
            (SELECT location_id FROM inventory_locations WHERE location_code = %s), %s, %s, %s
        FROM products p WHERE p.sku = %s
        ON CONFLICT (run_number) DO NOTHING RETURNING run_id
    """, (run.run_number, run.planned, run.planned, run.start, run.start, run.end, run.end,
          FG_LOCATION, SITE, run.line, HISTORY_RUN_NOTE, run.sku))
    if run_id is None:
        return 0
    quality = 100.0 * run.good / run.planned
    wo_id = _returning(cur, """
        INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
            planned_quantity, actual_quantity, good_quantity, reject_quantity, status,
            scheduled_start_time, actual_start_time, actual_end_time, target_rate_per_minute,
            actual_rate_per_minute, availability, performance, oee_percentage)
        SELECT %s, pr.run_id, pr.product_id, %s, %s, %s, %s, %s, %s, 'COMPLETED', %s, %s, %s, 1, %s,
            100.00, 95.00, %s
        FROM production_runs pr WHERE pr.run_id = %s
        ON CONFLICT (work_order_number) DO NOTHING RETURNING work_order_id
    """, (run.work_order_number, SITE, run.line, run.planned, run.planned, run.good, run.reject,
          run.start, run.start, run.end, round(run.planned / (RUN_SECONDS / 60), 2),
          round(0.95 * quality, 2), run_id))
    _write_work_order_children(cur, run, run_id, wo_id)
    if downtime is not None:
        cur.execute("""
            INSERT INTO mes_downtime_events (work_order_id, line_id, downtime_category, downtime_reason,
                start_time, end_time, resolved_by, resolution_notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (wo_id, downtime.line, downtime.category, downtime.reason, downtime.start,
              downtime.end, CREW, downtime.notes))
    return 1


# A past stop on its history run's MES work order, keyed by its fixed
# downtime id and owned by the plan: written whether or not the run itself
# was new (a stack Update gives an account written by an earlier history
# version its stops), and moved onto this era's run when an earlier era's
# runs still hold it (a live -> frozen -> live redeploy keeps the ERP history
# rows); a same-anchor rerun changes nothing. Nothing is written when the run
# is not this history's.
_INSERT_PAST_STOP_SQL = """
    INSERT INTO mes_downtime_events AS d (downtime_id, work_order_id, line_id, downtime_category,
        downtime_reason, start_time, end_time, resolved_by, resolution_notes)
    SELECT %s, w.work_order_id, %s, %s, %s, %s, %s, %s, %s
    FROM mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
    WHERE w.work_order_number = %s AND pr.run_number = %s AND w.site = %s
    ON CONFLICT (downtime_id) DO UPDATE SET work_order_id = EXCLUDED.work_order_id,
        line_id = EXCLUDED.line_id, downtime_category = EXCLUDED.downtime_category,
        downtime_reason = EXCLUDED.downtime_reason, start_time = EXCLUDED.start_time,
        end_time = EXCLUDED.end_time, resolved_by = EXCLUDED.resolved_by,
        resolution_notes = EXCLUDED.resolution_notes
    WHERE (d.work_order_id, d.line_id, d.downtime_category, d.downtime_reason, d.start_time,
           d.end_time, d.resolved_by, d.resolution_notes)
        IS DISTINCT FROM (EXCLUDED.work_order_id, EXCLUDED.line_id, EXCLUDED.downtime_category,
                          EXCLUDED.downtime_reason, EXCLUDED.start_time, EXCLUDED.end_time,
                          EXCLUDED.resolved_by, EXCLUDED.resolution_notes)
"""


def _write_past_stop(cur, stop: PastStopPlan) -> int:
    """One routine stop (:class:`PastStopPlan`); 0 when it was already there."""
    cur.execute(_INSERT_PAST_STOP_SQL, (
        stop.downtime_id, stop.line, stop.category, stop.reason, stop.start, stop.end,
        stop.resolved_by, stop.notes, stop.work_order_number, stop.run_number, SITE))
    return _count(cur)


# A past stop this write does not carry (a shorter history, or its run is
# held elsewhere now) goes from the MES, as its bridge order goes from the
# CMMS (_write_past_stop_orders); only off a history run (its notes mark it).
_DELETE_OMITTED_PAST_STOPS_SQL = """
    DELETE FROM mes_downtime_events d
    USING mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
    WHERE d.work_order_id = w.work_order_id AND d.downtime_id = ANY(%s::uuid[]) AND pr.notes = %s
"""
# The MES work orders that hold a past stop now.
_PAST_STOP_HOSTS_SQL = """
    SELECT DISTINCT work_order_id::text FROM mes_downtime_events WHERE downtime_id = ANY(%s::uuid[])
"""
# A history work order's run time, the OEE inputs it was inserted with, and
# its downtime (only history runs: their notes mark them).
_HISTORY_OEE_INPUTS_SQL = """
    SELECT w.work_order_id::text, EXTRACT(EPOCH FROM (w.actual_end_time - w.actual_start_time)) / 60,
           w.performance, w.good_quantity, w.planned_quantity, w.availability, w.oee_percentage,
           COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (d.end_time - d.start_time))) / 60
                     FROM mes_downtime_events d WHERE d.work_order_id = w.work_order_id), 0)
    FROM mes_work_orders w JOIN production_runs pr ON pr.run_id = w.production_run_id
    WHERE w.work_order_id = ANY(%s::uuid[]) AND pr.notes = %s
"""
_SET_HISTORY_OEE_SQL = """
    UPDATE mes_work_orders SET availability = %s, oee_percentage = %s WHERE work_order_id = %s::uuid
"""


def _past_stop_hosts(cur) -> set:
    cur.execute(_PAST_STOP_HOSTS_SQL, (list(PAST_STOP_DOWNTIME_IDS),))
    return {row[0] for row in cur.fetchall()}


def _delete_omitted_past_stops(cur, stops) -> int:
    """Delete the past stops not in ``stops`` (what this write carries) from
    the history runs; the OEE alignment then gives those runs their inserted
    figures back. Returns the rows deleted; a rerun deletes none."""
    carried = {stop.downtime_id for stop in stops}
    omitted = [i for i in PAST_STOP_DOWNTIME_IDS if i not in carried]
    if not omitted:
        return 0
    cur.execute(_DELETE_OMITTED_PAST_STOPS_SQL, (omitted, HISTORY_RUN_NOTE))
    return _count(cur)


def _history_oee(run_minutes: float, downtime_minutes: float, performance: float,
                 good: int, planned: int) -> Tuple[float, float]:
    """(availability %, OEE %) of a history work order: the simulator's
    availability (:func:`compute_oee_components`, run time less downtime) and
    the history's own performance and quality (good of planned), so a run with
    no downtime keeps exactly the figures ``_write_run`` inserts."""
    avail = compute_oee_components(run_minutes, downtime_minutes, 0, 0, 0, 0)[0]
    quality = good / planned if planned else 1.0
    return round(avail * 100, 2), round(avail * performance * quality, 2)


def _align_past_stop_oee(cur, prior_hosts: set) -> int:
    """Availability and OEE of every history work order that holds a past
    stop now or held one before this write (a stop that moved onto this era's
    run gives the run it left its inserted figures back). Returns the rows
    changed; a rerun changes none."""
    hosts = sorted(prior_hosts | _past_stop_hosts(cur))
    if not hosts:
        return 0
    cur.execute(_HISTORY_OEE_INPUTS_SQL, (hosts, HISTORY_RUN_NOTE))
    changed = 0
    for wo_id, run_minutes, performance, good, planned, avail, oee, downtime in cur.fetchall():
        want = _history_oee(float(run_minutes), float(downtime), float(performance), good, planned)
        if (avail is None or oee is None) or (float(avail), float(oee)) != want:
            cur.execute(_SET_HISTORY_OEE_SQL, (*want, wo_id))
            changed += 1
    return changed


def _write_work_order_children(cur, run: RunPlan, run_id, wo_id) -> None:
    """START/STOP events, quality checks and the lot trace of one work order."""
    events = (("START", run.start, 0, None, None),
              ("STOP", run.end, run.planned, "SPC-DRIFT" if run.stop_reason else "COMPLETE",
               run.stop_reason or "Run completed"))
    for event_type, when, qty, code, text in events:
        cur.execute("""
            INSERT INTO mes_production_events (work_order_id, event_type, event_time, reason_code,
                reason_description, quantity_at_event) VALUES (%s, %s, %s, %s, %s, %s)
        """, (wo_id, event_type, when, code, text, qty))
    for check in run.checks:
        cur.execute("""
            INSERT INTO mes_quality_checks (work_order_id, check_time, check_type, sample_size,
                pass_count, fail_count, inspector_id, notes) VALUES (%s, %s, %s, %s, %s, %s, 'QC-SPC', %s)
        """, (wo_id, check.check_time, check.check_type, check.sample_size, check.pass_count,
              check.fail_count, check.notes))
    cur.execute("""
        INSERT INTO lot_production_usage (lot_id, production_run_id, quantity_used, unit_of_measure, usage_date)
        SELECT lot_id, %s, %s, unit_of_measure, %s FROM material_lots WHERE lot_number = %s
    """, (run_id, run.lot_quantity, run.start, run.lot_number))
    cur.execute("""
        INSERT INTO lot_mes_tracking (lot_id, work_order_id, batch_id, site, line, quantity_consumed,
            unit_of_measure, consumption_date)
        SELECT lot_id, %s, %s, %s, %s, %s, unit_of_measure, %s FROM material_lots WHERE lot_number = %s
    """, (wo_id, run.work_order_number, SITE, run.line, run.lot_quantity, run.start, run.lot_number))
    _write_consumption(cur, run, wo_id)


def _write_consumption(cur, run: RunPlan, wo_id) -> None:
    """The MES side of the lot trace: the work order consumed the lot's material."""
    cur.execute("""
        INSERT INTO mes_material_consumption (work_order_id, item_id, planned_quantity,
            actual_quantity, consumption_time, lot_number)
        SELECT %s, item_id, %s, %s, %s, lot_number FROM material_lots WHERE lot_number = %s
    """, (wo_id, run.lot_quantity, run.lot_quantity, run.start, run.lot_number))


def _write_lot(cur, lot: LotPlan) -> None:
    """A fully drawn supplier lot; a rewrite on a new anchor moves its receipt."""
    cur.execute("""
        INSERT INTO material_lots (lot_number, item_id, supplier_id, received_date, received_quantity,
            remaining_quantity, unit_of_measure, quality_status, current_location_id, notes)
        SELECT %s, i.item_id, s.supplier_id, %s, %s, 0, i.unit_of_measure, 'APPROVED',
            (SELECT location_id FROM inventory_locations WHERE location_code = %s), %s
        FROM inventory_items i, suppliers s WHERE i.item_code = %s AND s.supplier_code = %s
        ON CONFLICT (lot_number) DO UPDATE SET received_date = EXCLUDED.received_date,
            received_quantity = EXCLUDED.received_quantity, remaining_quantity = 0
    """, (lot.lot_number, lot.received, lot.quantity, RAW_LOCATION, "Supplier lot, fully drawn",
          lot.item_code, lot.supplier_code))


def _write_order(cur, order: OrderPlan) -> int:
    """Order + line + shipment (+ item carrying the batch). 0 when it existed."""
    order_id = _returning(cur, """
        INSERT INTO customer_orders (order_number, customer_id, order_date, requested_delivery_date,
            status, subtotal, tax_amount, shipping_amount, total_amount, notes)
        SELECT %s, c.customer_id, %s, %s, %s, p.list_price * %s, 0, 0, p.list_price * %s, %s
        FROM customers c, products p WHERE c.customer_code = %s AND p.sku = %s
        ON CONFLICT (order_number) DO NOTHING RETURNING order_id
    """, (order.order_number, order.order_date, order.ship_date, order.status, order.quantity,
          order.quantity, "Live-story history order", order.customer_code, order.sku))
    if order_id is None:
        return 0
    line_id = _returning(cur, """
        INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price)
        SELECT %s, 1, p.product_id, %s, p.list_price FROM products p WHERE p.sku = %s
        RETURNING line_item_id
    """, (order_id, order.quantity, order.sku))
    shipment_id = _returning(cur, """
        INSERT INTO shipments (shipment_number, order_id, customer_id, shipment_date, carrier,
            estimated_delivery_date, actual_delivery_date, status)
        SELECT %s, o.order_id, o.customer_id, %s, 'AnyCompany Freight', %s, %s, %s
        FROM customer_orders o WHERE o.order_id = %s
        RETURNING shipment_id
    """, (order.shipment_number, order.ship_date, order.ship_date + timedelta(days=1),
          order.delivered_date, order.status, order_id))
    cur.execute("""
        INSERT INTO shipment_items (shipment_id, line_item_id, product_id, quantity_shipped, batch_id)
        SELECT %s, %s, p.product_id, %s, %s FROM products p WHERE p.sku = %s
    """, (shipment_id, line_id, order.quantity, order.batch_id, order.sku))
    return 1


def _count(cur) -> int:
    return max(cur.rowcount or 0, 0)


# The plan OWNS the dates and status of its CMMS work orders: a history
# rewrite on a NEW anchor (db-init re-run) moves them; a same-anchor rerun
# matches the WHERE nothing and changes nothing (rowcount 0).
_UPSERT_CMMS_WORK_ORDER_SQL = sql_text("""
    INSERT INTO {cmms}.work_orders AS wo (wonum, asset_num, work_type, description, status,
        priority, pm_num, reported_by, reported_date, target_start, sched_start,
        actual_start, actual_finish, failure_code, crew)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (wonum) DO UPDATE SET status = EXCLUDED.status,
        reported_date = EXCLUDED.reported_date, target_start = EXCLUDED.target_start,
        sched_start = EXCLUDED.sched_start, actual_start = EXCLUDED.actual_start,
        actual_finish = EXCLUDED.actual_finish
    WHERE (wo.reported_date, wo.target_start, wo.sched_start, wo.actual_start, wo.actual_finish)
        IS DISTINCT FROM (EXCLUDED.reported_date, EXCLUDED.target_start, EXCLUDED.sched_start,
                          EXCLUDED.actual_start, EXCLUDED.actual_finish)
""", cmms=CMMS_SCHEMA)

# A history log entry is keyed by (wonum, log_type): a stale one (older
# anchor) is replaced, so a rewrite never accrues a second entry.
_DELETE_STALE_LOG_SQL = sql_text("""
    DELETE FROM {cmms}.work_order_log
    WHERE wonum = %s AND log_type = %s
      AND (log_date, author, entry) IS DISTINCT FROM (%s::timestamp, %s::varchar, %s::text)
""", cmms=CMMS_SCHEMA)
_INSERT_LOG_SQL = sql_text("""
    INSERT INTO {cmms}.work_order_log (wonum, log_date, author, log_type, entry)
    SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS (
        SELECT 1 FROM {cmms}.work_order_log WHERE wonum = %s AND log_type = %s)
""", cmms=CMMS_SCHEMA)

# The live condition monitor numbers its orders by story loop
# (MESSimulator._condition_wonum: VIB-CMX-<loop>), and loop 0 starts AT the
# anchor. Orders on the story asset reported BEFORE the current anchor belong
# to an earlier story era; left in place, their wonums would silently
# suppress this era's loop-0.. orders (ON CONFLICT DO NOTHING).
CONDITION_WONUM_PREFIX = MESSimulator._condition_wonum(0.0).rstrip("0")
_PURGE_CONDITION_SCOPE = sql_text(
    "SELECT wonum FROM {cmms}.work_orders "
    "WHERE asset_num = %s AND wonum LIKE %s AND reported_date < %s", cmms=CMMS_SCHEMA)
_PURGE_CONDITION_LOG_SQL = sql_text(
    "DELETE FROM {cmms}.work_order_log WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_PURGE_CONDITION_SCOPE)
_PURGE_CONDITION_WO_SQL = sql_text(
    "DELETE FROM {cmms}.work_orders WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_PURGE_CONDITION_SCOPE)


def _write_seed(cur, sp: StoryPlan) -> Dict[str, int]:
    """The site CMMS rows (seed database): the PM's last completed cycle, the
    deferred PM, its log, the repair, the past stops' completed bridge orders;
    earlier-era condition orders go, the frozen-era open orders are
    superseded and its completed orders are marked prior-era."""
    counts = {"cmms_work_orders": 0, "cmms_log": 0}
    for wo in sp.cmms_work_orders:
        cur.execute(_UPSERT_CMMS_WORK_ORDER_SQL, (
            wo.wonum, cm.STORY_CMMS_ASSET_NUM, wo.work_type, wo.description, wo.status,
            wo.priority, wo.pm_num, wo.reported_by, wo.reported_date, wo.target_start,
            wo.sched_start, wo.actual_start, wo.actual_finish, wo.failure_code, CREW))
        counts["cmms_work_orders"] += _count(cur)
    for log in sp.cmms_log:
        counts["cmms_log"] += _write_log_entry(cur, log)
    counts["purged_condition_orders"] = _purge_earlier_condition_orders(cur, sp)
    counts["superseded_orders"] = _supersede_frozen_orders(cur, sp)
    counts["prior_era_marked"] = _mark_prior_era_orders(cur, sp)
    counts["pm_schedule"] = _align_pm_schedule(cur, *_pm_schedule_dates(sp))
    counts["plant_pm_work_orders"], counts["plant_pm_schedule"] = _write_plant_pm(cur, sp)
    counts["superseded_plant_orders"] = _supersede_plant_pm_orders(cur, sp)
    counts["past_stop_orders"], counts["past_stop_log"] = _write_past_stop_orders(cur, sp)
    return counts


def _write_log_entry(cur, log: CmmsLogPlan) -> int:
    """One history log entry, keyed by ``(wonum, log_type)``; 1 when written."""
    cur.execute(_DELETE_STALE_LOG_SQL, (log.wonum, log.log_type, log.log_date, log.author, log.entry))
    cur.execute(_INSERT_LOG_SQL, (log.wonum, log.log_date, log.author, log.log_type, log.entry,
                                  log.wonum, log.log_type))
    return _count(cur)


# The past stops' bridge orders the history owns: the number, the bridge as
# reporter and a description ending in THAT stop's downtime id (the bridge's
# text). A bridge number keeps 8 hex digits of its stop's id, so a live order
# for another stop can share one; it is never the history's to move or delete.
_PAST_STOP_PROOF = SqlText("JOIN unnest(%(wonums)s::varchar[], %(ids)s::text[]) AS p(wonum, downtime_id) "
                           "ON p.wonum = wo.wonum")
_OWNED_PAST_STOP_SCOPE = sql_text(
    "SELECT wo.wonum FROM {cmms}.work_orders wo {proof} "
    "WHERE wo.reported_by = %(reporter)s AND right(wo.description, length(p.downtime_id)) = p.downtime_id",
    cmms=CMMS_SCHEMA, proof=_PAST_STOP_PROOF)
_DELETE_PAST_STOP_LOG_SQL = sql_text(
    "DELETE FROM {cmms}.work_order_log WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_OWNED_PAST_STOP_SCOPE)
_DELETE_PAST_STOP_WO_SQL = sql_text(
    "DELETE FROM {cmms}.work_orders WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_OWNED_PAST_STOP_SCOPE)
# A planned stop's number that an order the history does not own holds.
_FOREIGN_PAST_STOP_ORDERS_SQL = sql_text(
    "SELECT wo.wonum FROM {cmms}.work_orders wo {proof} "
    "WHERE wo.reported_by IS DISTINCT FROM %(reporter)s "
    "OR right(wo.description, length(p.downtime_id)) IS DISTINCT FROM p.downtime_id "
    "ORDER BY wo.wonum", cmms=CMMS_SCHEMA, proof=_PAST_STOP_PROOF)


def _past_stop_scope(wonums) -> Dict[str, object]:
    """Parameters of the owned / foreign past-stop order statements for
    ``wonums`` (past stops' bridge numbers), each with its stop's id."""
    ids = dict(zip(PAST_STOP_WONUMS, PAST_STOP_DOWNTIME_IDS))
    wonums = list(wonums)
    return {"wonums": wonums, "ids": [ids[w] for w in wonums],
            "reporter": MESSimulator.CMMS_BRIDGE_REPORTED_BY}


def _delete_owned_past_stop_orders(cur, wonums) -> int:
    """Delete the history's own bridge orders among ``wonums`` and their log
    (log rows first); returns the work orders deleted."""
    if not wonums:
        return 0
    scope = _past_stop_scope(wonums)
    cur.execute(_DELETE_PAST_STOP_LOG_SQL, scope)
    cur.execute(_DELETE_PAST_STOP_WO_SQL, scope)
    return _count(cur)


def _write_past_stop_orders(cur, sp: StoryPlan) -> Tuple[int, int]:
    """Each past stop's completed bridge order, on the asset the bridge picks
    for its reason (``MESSimulator.indianapolis_bridge_asset``), and its
    COMPLETE entry; an order an earlier write left for a stop ``sp`` does not
    carry (its run is held elsewhere now, or the history is shorter) goes with
    its log, so the CMMS holds no bridge order without its MES stop. Only the
    history's own orders are moved or deleted: a planned stop whose number
    another order holds raises (the seed write rolls back). Returns (work
    orders written, log entries written); a same-anchor rerun writes neither."""
    planned = [stop.order.wonum for stop in sp.past_stops]
    if planned:
        cur.execute(_FOREIGN_PAST_STOP_ORDERS_SQL, _past_stop_scope(planned))
        foreign = [row[0] for row in cur.fetchall()]
        if foreign:
            raise RuntimeError(f"story history: {CMMS_SCHEMA}.work_orders {foreign} belong to another order "
                               "(a live bridge order whose number matches a past stop's); not written over")
    _delete_owned_past_stop_orders(cur, [wonum for wonum in PAST_STOP_WONUMS if wonum not in planned])
    orders = log = 0
    for stop in sp.past_stops:
        asset = MESSimulator.indianapolis_bridge_asset(cur, SITE, stop.reason)
        if asset is None:
            raise RuntimeError(f"story history: {CMMS_SCHEMA}.assets holds no asset for the {stop.reason} stop")
        wo = stop.order
        cur.execute(_UPSERT_CMMS_WORK_ORDER_SQL, (
            wo.wonum, asset, wo.work_type, wo.description, wo.status, wo.priority, wo.pm_num,
            wo.reported_by, wo.reported_date, wo.target_start, wo.sched_start, wo.actual_start,
            wo.actual_finish, wo.failure_code, CREW))
        orders += _count(cur)
        log += _write_log_entry(cur, stop.log)
    return orders, log


# The story PM's schedule row: last completion and next due, changed only
# when they differ (a rerun on the same clock changes nothing).
_ALIGN_PM_SCHEDULE_SQL = sql_text("""
    UPDATE {cmms}.pm_schedules SET last_completion_date = %s::date, next_due_date = %s::date
    WHERE pm_num = %s AND asset_num = %s
      AND (last_completion_date IS DISTINCT FROM %s::date OR next_due_date IS DISTINCT FROM %s::date)
""", cmms=CMMS_SCHEMA)


def _pm_schedule_dates(sp: StoryPlan) -> Tuple[str, str]:
    """(last completion, next due) of the story PM: due on the deferred PM's
    target start, last completed one frequency before it."""
    pm = next(wo for wo in sp.cmms_work_orders if wo.wonum == DEFERRED_PM_WONUM)
    due = pm.target_start.date()
    return (due - timedelta(days=PM_FREQUENCY_DAYS)).isoformat(), due.isoformat()


def _align_pm_schedule(cur, last: str, due: str,
                       pm_num: str = STORY_PM_NUM, asset: str = cm.STORY_CMMS_ASSET_NUM) -> int:
    cur.execute(_ALIGN_PM_SCHEDULE_SQL, (last, due, pm_num, asset, last, due))
    return _count(cur)


def _write_plant_pm(cur, sp: StoryPlan) -> Tuple[int, int]:
    """The non-story Indianapolis plans: upsert each shifted last-completion
    CLOSE work order (its own asset/crew) and move its pm_schedules row onto
    the live run. Returns (work orders written, schedules moved). A same-anchor
    rerun matches the WHERE nothing and changes neither (counts 0)."""
    work_orders = schedule = 0
    for p in sp.plant_pm:
        wo = p.close
        cur.execute(_UPSERT_CMMS_WORK_ORDER_SQL, (
            wo.wonum, p.asset_num, wo.work_type, wo.description, wo.status, wo.priority,
            wo.pm_num, wo.reported_by, wo.reported_date, wo.target_start, wo.sched_start,
            wo.actual_start, wo.actual_finish, wo.failure_code, _plant_crew(p)))
        work_orders += _count(cur)
        schedule += _align_pm_schedule(cur, p.last_completion.isoformat(),
                                       p.next_due.isoformat(), p.pm_num, p.asset_num)
    return work_orders, schedule


def _plant_crew(p: PlantPmPlan) -> str:
    """The crew the 011 seed assigned the plan (carried on the baseline)."""
    return next(b.crew for b in PLANT_PM_BASELINE if b.pm_num == p.pm_num)


def _purge_earlier_condition_orders(cur, sp: StoryPlan) -> int:
    """Delete the story asset's condition orders reported before the anchor
    (and their log rows); returns work orders deleted. Idempotent."""
    scope = (cm.STORY_CMMS_ASSET_NUM, CONDITION_WONUM_PREFIX + "%", ts(sp.anchor_epoch))
    cur.execute(_PURGE_CONDITION_LOG_SQL, scope)
    cur.execute(_PURGE_CONDITION_WO_SQL, scope)
    return _count(cur)


# The frozen era on the story asset: rows reported before the anchor that are
# neither the history's own work orders nor condition-monitor orders (the
# 011 seed rows, and anything an earlier story era left behind).
_FROZEN_ERA_SCOPE = SqlText(
    "wo.asset_num = %(asset)s AND wo.reported_date < %(anchor)s "
    "AND wo.wonum <> ALL(%(history)s) AND wo.wonum NOT LIKE %(condition)s")

# Frozen-era OPEN orders (011: PM 1182 WAPPR, PM 1201 APPR) -> CAN, with one
# log entry each that records the prior status, in ONE statement. A cancelled
# row no longer matches, so a rerun changes nothing.
_SUPERSEDE_FROZEN_ORDERS_SQL = sql_text("""
    WITH superseded AS (
        UPDATE {cmms}.work_orders AS wo SET status = 'CAN'
        FROM unnest(%(priors)s::varchar[], %(entries)s::text[]) AS m(prior, entry)
        WHERE wo.status = m.prior AND {frozen_era}
        RETURNING wo.wonum, m.entry
    )
    INSERT INTO {cmms}.work_order_log (wonum, log_date, author, log_type, entry)
    SELECT wonum, %(log_date)s, %(author)s, 'UPDATE', entry FROM superseded
""", cmms=CMMS_SCHEMA, frozen_era=_FROZEN_ERA_SCOPE)

# Every CAN order on the story asset that carries a supersede entry (either
# form) says so in its description, once; a row the suffix would overflow
# stays as it is (the entry still records the supersede).
_MARK_SUPERSEDED_SQL = sql_text("""
    UPDATE {cmms}.work_orders AS wo SET description = wo.description || %(suffix)s
    WHERE wo.asset_num = %(asset)s AND wo.status = 'CAN'
      AND right(wo.description, length(%(suffix)s)) <> %(suffix)s
      AND length(wo.description || %(suffix)s) <= %(width)s
      AND wo.wonum IN (SELECT wonum FROM {cmms}.work_order_log WHERE entry = ANY(%(entries)s))
""", cmms=CMMS_SCHEMA)

# Frozen-era COMPLETED orders (011: CM 1187, EM 1195) stay as history, marked
# prior-era once (their descriptions cite frozen-story ids the live plant
# does not have). CLOSE'd cadence PMs are left as they are.
_MARK_PRIOR_ERA_SQL = sql_text("""
    UPDATE {cmms}.work_orders AS wo SET description = %(prefix)s || wo.description
    WHERE wo.status = 'COMP' AND {frozen_era}
      AND left(wo.description, length(%(prefix)s)) <> %(prefix)s
      AND length(%(prefix)s || wo.description) <= %(width)s
""", cmms=CMMS_SCHEMA, frozen_era=_FROZEN_ERA_SCOPE)


def _supersede_mapping(with_legacy: bool) -> Dict[str, list]:
    """Parallel (prior status, supersede entry) arrays; ``with_legacy`` adds
    the pre-status entry, which only ever superseded a WAPPR PM."""
    priors = list(OPEN_STATUSES)
    entries = [superseded_log_entry(s) for s in priors]
    if with_legacy:
        priors, entries = priors + ["WAPPR"], entries + [SUPERSEDED_LOG_ENTRY]
    return {"priors": priors, "entries": entries}


def _frozen_era_params(sp: StoryPlan) -> Dict[str, object]:
    return {"asset": cm.STORY_CMMS_ASSET_NUM, "anchor": ts(sp.anchor_epoch),
            "history": ([wo.wonum for wo in sp.cmms_work_orders] + list(PLANT_PM_CLOSE_WONUMS)
                        + list(PAST_STOP_WONUMS)),
            "condition": CONDITION_WONUM_PREFIX + "%", "width": DESCRIPTION_WIDTH}


def _supersede_frozen_orders(cur, sp: StoryPlan) -> int:
    """Cancel the frozen-era open orders on the story asset and mark every
    superseded description; returns how many were cancelled now."""
    params = {**_frozen_era_params(sp), **_supersede_mapping(with_legacy=False),
              "log_date": ts(sp.anchor_epoch), "author": SUPERSEDED_LOG_AUTHOR}
    cur.execute(_SUPERSEDE_FROZEN_ORDERS_SQL, params)
    superseded = _count(cur)
    cur.execute(_MARK_SUPERSEDED_SQL, {
        "asset": cm.STORY_CMMS_ASSET_NUM, "suffix": SUPERSEDED_DESCRIPTION_SUFFIX,
        "width": DESCRIPTION_WIDTH, "entries": _supersede_mapping(with_legacy=True)["entries"]})
    return superseded


def _mark_prior_era_orders(cur, sp: StoryPlan) -> int:
    """Prefix the frozen-era completed orders on the story asset; returns how many."""
    cur.execute(_MARK_PRIOR_ERA_SQL, {**_frozen_era_params(sp), "prefix": PRIOR_ERA_PREFIX})
    return _count(cur)


# The non-story plant PM plans' OWN open seed orders (011: PM 1204 WAPPR on
# 1001-CVA-100, due the day PM-1001-0014 was next due). These sit on OTHER
# assets than the story asset, so the story-asset supersede above misses them;
# left open, a plant-wide "what work is open?" question reads them as live
# backlog from the frozen era. Scoped by the aligned plans' own pm_num (never
# by asset), so an unrelated open order on the same asset is untouched. Marked
# with the SAME supersede entry + suffix as the story asset's, so the shared
# restore (keyed by the log entry, not the asset) reopens them on Delete. The
# cancel + prior-status log entry is ONE statement (as the story-asset
# supersede is): a cancelled row no longer matches, so a rerun changes nothing.
_PLANT_PM_OPEN_SCOPE = SqlText(
    "wo.pm_num = ANY(%(pm_nums)s) AND wo.status = m.prior "
    "AND wo.reported_date < %(anchor)s AND wo.wonum <> ALL(%(close_wonums)s)")
_SUPERSEDE_PLANT_PM_ORDERS_SQL = sql_text("""
    WITH superseded AS (
        UPDATE {cmms}.work_orders AS wo SET status = 'CAN'
        FROM unnest(%(priors)s::varchar[], %(entries)s::text[]) AS m(prior, entry)
        WHERE {plant_pm_open}
        RETURNING wo.wonum, m.entry
    )
    INSERT INTO {cmms}.work_order_log (wonum, log_date, author, log_type, entry)
    SELECT wonum, %(log_date)s, %(author)s, 'UPDATE', entry FROM superseded
""", cmms=CMMS_SCHEMA, plant_pm_open=_PLANT_PM_OPEN_SCOPE)
# Every CAN plant-PM order that carries a supersede entry says so in its
# description once (scoped by pm_num, the plant counterpart of _MARK_SUPERSEDED_SQL).
_MARK_PLANT_PM_SUPERSEDED_SQL = sql_text("""
    UPDATE {cmms}.work_orders AS wo SET description = wo.description || %(suffix)s
    WHERE wo.pm_num = ANY(%(pm_nums)s) AND wo.status = 'CAN'
      AND wo.wonum <> ALL(%(close_wonums)s)
      AND right(wo.description, length(%(suffix)s)) <> %(suffix)s
      AND length(wo.description || %(suffix)s) <= %(width)s
      AND wo.wonum IN (SELECT wonum FROM {cmms}.work_order_log WHERE entry = ANY(%(entries)s))
""", cmms=CMMS_SCHEMA)


def _plant_pm_open_params(sp: StoryPlan) -> Dict[str, object]:
    return {"pm_nums": [b.pm_num for b in PLANT_PM_BASELINE], "anchor": ts(sp.anchor_epoch),
            "close_wonums": list(PLANT_PM_CLOSE_WONUMS)}


def _supersede_plant_pm_orders(cur, sp: StoryPlan) -> int:
    """Cancel each aligned plant plan's own frozen-era open order (its prior
    status recorded in a supersede log entry, the suffix on its description),
    so the shared Delete reopens it exactly. Returns how many were cancelled now."""
    params = {**_plant_pm_open_params(sp), **_supersede_mapping(with_legacy=False),
              "log_date": ts(sp.anchor_epoch), "author": SUPERSEDED_LOG_AUTHOR}
    cur.execute(_SUPERSEDE_PLANT_PM_ORDERS_SQL, params)
    superseded = _count(cur)
    cur.execute(_MARK_PLANT_PM_SUPERSEDED_SQL, {
        **_plant_pm_open_params(sp), "suffix": SUPERSEDED_DESCRIPTION_SUFFIX,
        "width": DESCRIPTION_WIDTH, "entries": _supersede_mapping(with_legacy=True)["entries"]})
    return superseded


# ---------------------------------------------------------------------------
# Delete (live -> frozen): give the frozen story back its own seed
# ---------------------------------------------------------------------------

# The story's own orders (the deferred PM and the repair; what the answer key
# lists) and every history-owned CMMS row (what the Delete removes).
STORY_ORDER_WONUMS = (DEFERRED_PM_WONUM, CONDITION_WONUM, REPAIR_WONUM)
HISTORY_WONUMS = (LAST_PM_WONUM, *STORY_ORDER_WONUMS)

# Undo the supersede: only a CAN row that carries a supersede log entry is
# reopened, to the status that entry records (the pre-status entry: WAPPR);
# the description suffix and the entry go with it.
_RESTORE_SUPERSEDED_SQL = sql_text("""
    WITH logged AS (
        SELECT l.wonum, m.prior FROM {cmms}.work_order_log l
        JOIN unnest(%(priors)s::varchar[], %(entries)s::text[]) AS m(prior, entry) ON l.entry = m.entry
    ), restored AS (
        UPDATE {cmms}.work_orders AS wo SET status = logged.prior,
            description = CASE WHEN right(wo.description, length(%(suffix)s)) = %(suffix)s
                THEN left(wo.description, length(wo.description) - length(%(suffix)s))
                ELSE wo.description END
        FROM logged WHERE wo.wonum = logged.wonum AND wo.status = 'CAN'
        RETURNING wo.wonum
    )
    DELETE FROM {cmms}.work_order_log
    WHERE entry = ANY(%(entries)s) AND wonum IN (SELECT wonum FROM restored)
""", cmms=CMMS_SCHEMA)
# Undo the prior-era mark (no 011 description starts with the prefix).
_RESTORE_PRIOR_ERA_SQL = sql_text("""
    UPDATE {cmms}.work_orders SET description = substr(description, length(%(prefix)s) + 1)
    WHERE asset_num = %(asset)s AND left(description, length(%(prefix)s)) = %(prefix)s
""", cmms=CMMS_SCHEMA)
_HISTORY_SCOPE = sql_text("SELECT wonum FROM {cmms}.work_orders "
                          "WHERE wonum = ANY(%s) AND asset_num = %s", cmms=CMMS_SCHEMA)
_DELETE_HISTORY_LOG_SQL = sql_text(
    "DELETE FROM {cmms}.work_order_log WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_HISTORY_SCOPE)
_DELETE_HISTORY_WO_SQL = sql_text(
    "DELETE FROM {cmms}.work_orders WHERE wonum IN ({scope})", cmms=CMMS_SCHEMA, scope=_HISTORY_SCOPE)
# The reserved plant-PM CLOSE orders the live story added (on their own
# assets, so NOT caught by the story-asset _HISTORY_SCOPE above): dropped by
# wonum (the reserved band is unique to the live story), log rows first.
_DELETE_PLANT_PM_LOG_SQL = sql_text("DELETE FROM {cmms}.work_order_log WHERE wonum = ANY(%s)", cmms=CMMS_SCHEMA)
_DELETE_PLANT_PM_WO_SQL = sql_text("DELETE FROM {cmms}.work_orders WHERE wonum = ANY(%s)", cmms=CMMS_SCHEMA)


def restore_frozen_seed(seed_conn) -> Dict[str, int]:
    """Reopen the superseded frozen-era orders (CAN -> their recorded prior
    status; supersede entry and description suffix removed), strip the
    prior-era prefix, and delete the history-owned CMMS rows
    (:data:`HISTORY_WONUMS`, the plant PM CLOSE orders, the past stops'
    bridge orders it owns, and their log). One transaction; rolls back and
    re-raises on error; a second run changes nothing."""
    return _in_transaction(seed_conn, _restore_seed, None)


def _restore_seed(cur, _unused) -> Dict[str, int]:
    cur.execute(_RESTORE_SUPERSEDED_SQL, {**_supersede_mapping(with_legacy=True),
                                          "suffix": SUPERSEDED_DESCRIPTION_SUFFIX})
    restored = _count(cur)
    cur.execute(_RESTORE_PRIOR_ERA_SQL, {"prefix": PRIOR_ERA_PREFIX, "asset": cm.STORY_CMMS_ASSET_NUM})
    prior_era = _count(cur)
    scope = (list(HISTORY_WONUMS), cm.STORY_CMMS_ASSET_NUM)
    cur.execute(_DELETE_HISTORY_LOG_SQL, scope)
    cur.execute(_DELETE_HISTORY_WO_SQL, scope)
    history = _count(cur)
    cur.execute(_DELETE_PLANT_PM_LOG_SQL, (list(PLANT_PM_CLOSE_WONUMS),))
    cur.execute(_DELETE_PLANT_PM_WO_SQL, (list(PLANT_PM_CLOSE_WONUMS),))
    plant_history = _count(cur)
    # The past stops' bridge orders the history owns (never a live order
    # that shares a number: _OWNED_PAST_STOP_SCOPE).
    past_stops = _delete_owned_past_stop_orders(cur, PAST_STOP_WONUMS)
    plant_schedule = 0
    for b in PLANT_PM_BASELINE:
        plant_schedule += _align_pm_schedule(cur, b.last_completion, b.next_due, b.pm_num, b.asset_num)
    return {"restored_orders": restored, "prior_era_restored": prior_era,
            "history_work_orders": history, "pm_schedule": _align_pm_schedule(cur, *FROZEN_PM_DATES),
            "plant_pm_work_orders": plant_history, "plant_pm_schedule": plant_schedule,
            "past_stop_orders": past_stops}
