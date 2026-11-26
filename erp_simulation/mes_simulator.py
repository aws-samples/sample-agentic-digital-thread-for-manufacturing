# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
MES (Manufacturing Execution System) Simulator for ERP Simulation System.

Manages MES work orders, production events, quality checks, material consumption,
downtime events, and OEE calculations for bottling line operations.
"""

import math
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any, Callable, Dict, Optional

from . import condition_monitor, inventory_draw
from .config import get_config, DatabaseConfig
from .utils import MAX_ROWS_PER_CYCLE, SqlText, insert_numbered, sql_text
from .logging_config import get_logger
from .base_simulator import BaseSimulator
from .production_simulator import RAW_LOCATION_CODES, SITE_RAW_LOCATION
from .external_mes import (  # the per-site MES backend switch and run ownership
    MES_BACKEND_BUILTIN,
    MES_BACKEND_CONFIG_PREFIX,  # noqa: F401 - re-exported for callers of this module
    MES_BACKEND_EXTERNAL,
    claims_table_present,
    mes_backend_config_key,
    resolve_mes_backend,
    unclaimed,
)

logger = get_logger(__name__)

# A bridge order's description as its writer builds it
# (_insert_indianapolis_cmms_work_order), ending in the MES downtime event it
# was raised from in canonical form (lower-case, hyphenated: PostgreSQL's
# uuid text). Part of the proof that an IND-* order is the bridge's own.
_BRIDGE_DESCRIPTION = re.compile(
    r"MES-triggered: .+ \u2014 MES downtime event "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# The open IND-* orders of ended loops, and the completion of the proven ones
# (one statement that also writes the log entry; it re-checks the reporter
# and the exact proven text). ``{schema}`` is the site CMMS class constant.
# At most ``MAX_ROWS_PER_CYCLE`` candidates per cycle: a close-out that keeps
# failing leaves them open while the bridge keeps raising more, and the cycle
# that reaches the set must not read all of it. Orders that can never be
# proven (a person's IND-* order) stay in the set for good, so a batch taken
# from the start every cycle could keep re-reading them; each read instead
# starts at a fresh random bridge number, walking the wonum primary key, and
# wraps round to the lowest (the BACKORDERED read in state_advancer does the same).
# Each candidate carries the stop start the order already records
# (``actual_start``), which completion keeps when its downtime row is gone.
_BRIDGE_CANDIDATES_FROM_SQL = """
    SELECT wonum, description, reported_date, reported_by, actual_start FROM {schema}.work_orders
    WHERE wonum LIKE %(prefix)s AND status = ANY(%(open_statuses)s) AND reported_date < %(loop_start)s
      AND wonum >= %(start)s
    ORDER BY wonum
    LIMIT %(limit)s
"""
_BRIDGE_CANDIDATES_BEFORE_SQL = """
    SELECT wonum, description, reported_date, reported_by, actual_start FROM {schema}.work_orders
    WHERE wonum LIKE %(prefix)s AND status = ANY(%(open_statuses)s) AND reported_date < %(loop_start)s
      AND wonum < %(start)s
    ORDER BY wonum
    LIMIT %(limit)s
"""
_COMPLETE_BRIDGE_SQL = """
    WITH plan AS (
        SELECT * FROM unnest(%(wonums)s::varchar[], %(starts)s::timestamp[], %(finishes)s::timestamp[],
                             %(authors)s::varchar[], %(descriptions)s::varchar[])
            AS p(wonum, actual_start, actual_finish, author, description)
    ), completed AS (
        UPDATE {schema}.work_orders AS wo
        SET status = %(status)s, actual_start = p.actual_start, actual_finish = p.actual_finish
        FROM plan p
        WHERE wo.wonum = p.wonum AND wo.wonum LIKE %(prefix)s
          AND wo.status = ANY(%(open_statuses)s) AND wo.reported_date < %(loop_start)s
          AND wo.reported_by = %(reported_by)s AND wo.description = p.description
        RETURNING wo.wonum, GREATEST(p.actual_finish, wo.reported_date) AS log_date,
                  COALESCE(p.author, %(default_author)s) AS author
    )
    INSERT INTO {schema}.work_order_log (wonum, log_date, author, log_type, entry)
    SELECT wonum, log_date, left(author, 60), 'COMPLETE', %(entry)s FROM completed
"""
_READ_DOWNTIMES_SQL = """
    SELECT downtime_id::text, start_time, end_time, resolved_by FROM mes_downtime_events
    WHERE downtime_id = ANY(%s::uuid[])
"""

# A corporate CMMS bridge order's number as its writer builds it
# (_insert_cmms_work_order): the site's asset-tag prefix and the first 8 hex
# chars of the downtime id, upper case. With _BRIDGE_DESCRIPTION, the proof
# that a WO-MES-* order is the bridge's own.
_CORP_BRIDGE_WO_NUMBER = re.compile(r"WO-MES-([A-Z]+)-([0-9A-F]{8})")
# The open corporate bridge orders, at most ``MAX_ROWS_PER_CYCLE`` per cycle,
# from a fresh random bridge number in wo_number order, wrapping round to the
# lowest (the site CMMS close-out above does the same, for the same reason:
# orders that can never be proven stay open, and a read from the start every
# cycle could keep re-reading them). The WHERE repeats the predicate of the
# partial index idx_work_orders_raw_open_bridge (lambda-adt-seed/
# 004_cmms_maintenance.sql) verbatim, so the read walks that index and never
# the closed orders, which only grow.
_CORP_BRIDGE_OPEN = SqlText("status_hint IN ('open', 'in_progress') AND wo_number LIKE 'WO-MES-%%'")
_CORP_BRIDGE_CANDIDATES_FROM_SQL = sql_text("""
    SELECT wo_number, description FROM cmms_maintenance.work_orders_raw
    WHERE {bridge_open} AND wo_number >= %(start)s
    ORDER BY wo_number
    LIMIT %(limit)s
""", bridge_open=_CORP_BRIDGE_OPEN)
_CORP_BRIDGE_CANDIDATES_BEFORE_SQL = sql_text("""
    SELECT wo_number, description FROM cmms_maintenance.work_orders_raw
    WHERE {bridge_open} AND wo_number < %(start)s
    ORDER BY wo_number
    LIMIT %(limit)s
""", bridge_open=_CORP_BRIDGE_OPEN)
# Close the planned orders in one statement; it re-checks that each is still
# open and still carries the proven text, so an edit in between or a rerun
# changes nothing. A cancelled order whose stop time was never recorded loses
# its offset too: the view then shows no creation time instead of "now".
_CORP_CLOSE_BRIDGE_SQL = """
    WITH plan AS (
        SELECT * FROM unnest(%(wo_numbers)s::varchar[], %(descriptions)s::text[], %(statuses)s::varchar[],
                             %(starts)s::timestamptz[], %(finishes)s::timestamptz[],
                             %(assignees)s::varchar[], %(hours)s::numeric[])
            AS p(wo_number, description, status, created_ts, completed_ts, assigned_to, actual_hours)
    )
    UPDATE cmms_maintenance.work_orders_raw AS w
    SET status_hint = p.status,
        created_ts = COALESCE(p.created_ts, w.created_ts),
        created_offset = CASE WHEN p.status = %(cancelled)s THEN NULL ELSE w.created_offset END,
        completed_ts = p.completed_ts,
        assigned_to = COALESCE(w.assigned_to, p.assigned_to),
        actual_hours = COALESCE(p.actual_hours, w.actual_hours)
    FROM plan p
    WHERE w.wo_number = p.wo_number AND w.description = p.description
      AND w.status_hint IN ('open', 'in_progress') AND w.wo_number LIKE 'WO-MES-%%'
    RETURNING w.status_hint
"""
# actual_hours is DECIMAL(5,2): a stop longer than this is recorded at the cap.
_CORP_MAX_ACTUAL_HOURS = 999.99
# Whether this process has reported corporate bridge orders it cannot prove.
_unproven_corp_bridge_warned: Dict[str, bool] = {"done": False}

# Completed work orders still without consumption rows: at most
# ``MAX_ROWS_PER_CYCLE`` per cycle, the rest wait for later cycles (a stalled
# cycle lets this set grow with every work order MES completes). A work order
# whose product has no active BOM never gets a row, so it stays in the set for
# good, and a batch taken from the start every cycle could keep re-reading those
# and never reach the others. Each read instead starts at a fresh random point in
# ``work_order_id`` (UUID) order, walking the primary key, and wraps round to the
# start (the BACKORDERED read in state_advancer does the same).
_CONSUMPTION_PENDING_FROM_SQL = """
    SELECT mwo.work_order_id, mwo.work_order_number, mwo.product_id,
           mwo.planned_quantity, mwo.actual_quantity, mwo.production_run_id
    FROM mes_work_orders mwo
    WHERE mwo.status = 'COMPLETED'
      AND mwo.work_order_id >= %s::uuid
      AND NOT EXISTS (
          SELECT 1 FROM mes_material_consumption mmc
          WHERE mmc.work_order_id = mwo.work_order_id
      )
    ORDER BY mwo.work_order_id
    LIMIT %s
"""

_CONSUMPTION_PENDING_BEFORE_SQL = """
    SELECT mwo.work_order_id, mwo.work_order_number, mwo.product_id,
           mwo.planned_quantity, mwo.actual_quantity, mwo.production_run_id
    FROM mes_work_orders mwo
    WHERE mwo.status = 'COMPLETED'
      AND mwo.work_order_id < %s::uuid
      AND NOT EXISTS (
          SELECT 1 FROM mes_material_consumption mmc
          WHERE mmc.work_order_id = mwo.work_order_id
      )
    ORDER BY mwo.work_order_id
    LIMIT %s
"""
# The loop whose unproven IND-* orders were last reported (once per loop per
# process: a warm Lambda container keeps it across invocations).
_unproven_bridge_warnings: Dict[str, Any] = {"loop_start": None}


def _naive_utc(epoch: float) -> datetime:
    """Naive UTC timestamp for ``epoch`` (the CMMS columns are TIMESTAMP)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _aware_utc(value: Optional[datetime]) -> Optional[datetime]:
    """``value`` as an aware UTC datetime (None stays None). The ERP's MES
    timestamps are naive UTC; the corporate CMMS's fixed dates are
    TIMESTAMPTZ, so they are written with their zone and never depend on the
    seed session's TimeZone."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _warn_unproven_bridge_orders(loop_start_epoch: float, wonums: list) -> None:
    """One warning per loop naming the IND-* orders of ended loops that the
    close-out leaves open because nothing proves the MES bridge raised them
    (those in the cycle's bounded candidate batch)."""
    if not wonums or _unproven_bridge_warnings.get("loop_start") == loop_start_epoch:
        return
    _unproven_bridge_warnings["loop_start"] = loop_start_epoch
    shown = ", ".join(wonums[:10]) + (f" and {len(wonums) - 10} more" if len(wonums) > 10 else "")
    logger.warning(f"Condition monitor: {len(wonums)} open IND-* work order(s) of earlier loops read "
                   f"this cycle are not provably MES-bridge orders and stay open: {shown}")


def compute_oee_components(run_minutes, downtime_minutes, actual_quantity,
                           target_rate, total_pass, total_sample):
    """Compute OEE components (Availability × Performance × Quality).

    Pure/import-safe (no I/O) so it is unit-testable offline.

    Returns:
        (availability, performance, quality, oee_pct) where availability,
        performance and quality are 0-1 fractions and oee_pct is a 0-100 percentage
        rounded to 2 decimals.
    """
    run_minutes = max(float(run_minutes), 0.1)
    avail = max(0.0, min(1.0, (run_minutes - downtime_minutes) / run_minutes))
    perf = (min(1.0, actual_quantity / (target_rate * run_minutes))
            if target_rate and target_rate > 0 else 1.0)
    qual = (total_pass / total_sample) if total_sample and total_sample > 0 else 1.0
    oee_pct = round(avail * perf * qual * 100, 2)
    return avail, perf, qual, oee_pct


def derive_machine_state(has_running_wo, has_open_downtime, in_changeover):
    """Derive the current line machine state with deterministic precedence.

    Precedence: DOWN > CHANGEOVER > RUNNING > IDLE. Pure/import-safe.
    """
    if has_open_downtime:
        return 'DOWN'
    if in_changeover:
        return 'CHANGEOVER'
    if has_running_wo:
        return 'RUNNING'
    return 'IDLE'


def derive_good_reject(actual_quantity, total_pass, total_sample, fallback_yield):
    """Scale the sampled first-pass yield to the FULL produced quantity.

    Pure/import-safe (no I/O) so it is unit-testable offline.

    The MES quality check is a small SAMPLE — ``total_pass``/``total_sample``
    (e.g. 20 passed of a 20-unit sample) is a first-pass yield RATE, not the batch
    good count. Writing the raw sample counts into ``good_quantity`` made
    ``good_quantity`` the sample size (e.g. 20 of an actual 512), so any
    good/actual yield read ~4-9%. This scales the sampled yield across
    ``actual_quantity`` so ``good ≈ actual − reject`` with a realistic small
    reject count. When there is no sample, fall back to ``fallback_yield`` (a
    realistic ~0.97-0.995).

    Returns ``(good_quantity, reject_quantity)`` with
    ``good + reject == actual_quantity``.
    """
    actual_quantity = max(0, int(actual_quantity))
    if total_sample and total_sample > 0:
        yield_rate = total_pass / total_sample
    else:
        yield_rate = fallback_yield
    yield_rate = max(0.0, min(1.0, float(yield_rate)))
    reject = int(round(actual_quantity * (1.0 - yield_rate)))
    reject = max(0, min(actual_quantity, reject))
    good = actual_quantity - reject
    return good, reject


# Default range of the expected pass rate of a routine quality check's units
# (simulation_config mes_quality_pass_rate_min / _max, seeded by lambda-db-init
# seed_simulation_config): 0 to 2,000 rejects per million, so most checks pass
# clean and an occasional one finds a reject. The old 0.90-0.99 default put a
# reject on nearly every check. A deployed work order runs 2 to 4 minutes and
# gets one check, so about 3-4 % of batches carry a routine reject. The
# story's bore failure stays the only DIMENSIONAL failure: no routine list
# has that type (SITE_QUALITY_CHECK_TYPES).
DEFAULT_QUALITY_PASS_RATE_MIN = 0.998
DEFAULT_QUALITY_PASS_RATE_MAX = 1.0


def sampled_rejects(sample_size: int, reject_probability: float, rng=random) -> int:
    """Rejects found in a check of ``sample_size`` units when each unit fails
    independently with ``reject_probability`` (a binomial draw).

    Pure apart from ``rng`` (pass a seeded ``random.Random`` in tests). It
    replaces ``sample_size - int(sample_size * pass_rate)``: the truncation
    rounded every fractional reject up to a whole one, so a check at a 99 %
    pass rate on 20 to 50 units always recorded at least one reject and every
    batch looked failed. Here a clean check is the common outcome at a high
    pass rate, and a low one (the Quality Issues scenario) still fails often.
    """
    p = min(1.0, max(0.0, float(reject_probability)))
    return sum(1 for _ in range(max(0, int(sample_size))) if rng.random() < p)  # nosec B311 - synthetic data generation, not security context


# The length of a random MES downtime, by category: (median minutes, sigma of
# the natural log), drawn in whole seconds between the configured floor and
# cap (5 and 60 minutes as seeded). Repair times are classically lognormal
# (the maintainability model of MIL-HDBK-472), so each category draws from
# its own lognormal, truncated to that range. Mechanical and electrical stops
# run longest and vary most (a jam clears in minutes, a failed drive or
# bearing takes most of the hour): about 1 in 7 mechanical and 1 in 12
# electrical stops run past half an hour, and about 1 in 300 reaches 55
# minutes. Material and quality stops sit in between, and changeovers and
# operator stops (breaks, safety checks) are short and regular (nine in ten
# under 1.4 times their median). The tails are kept thin enough that the
# longest stops spread out below the cap instead of crowding against it:
# mechanical and electrical medians of 22 and 18 minutes at a sigma of 0.6
# put two of the five longest of 5,000 stops on the same second in about 1
# run in 7, this shape in about 1 in 20. The draw used to be a whole number of minutes uniform
# between the floor and the cap (5 to 30), so a few weeks of cycles put
# dozens of events at exactly 30 minutes and every "longest downtime"
# ranking was a tie at the cap.
DOWNTIME_DURATION_PROFILES = {
    'MECHANICAL': (18.0, 0.5),
    'ELECTRICAL': (15.0, 0.5),
    'MATERIAL_SHORTAGE': (12.0, 0.45),
    'QUALITY_ISSUE': (10.0, 0.4),
    'CHANGEOVER': (12.0, 0.2),
    'OPERATOR': (8.0, 0.25),
}
# The shape of a category the table does not list (OTHER: the random draw
# never picks it).
DEFAULT_DOWNTIME_PROFILE = (10.0, 0.4)
# Default floor and cap of that length in minutes (simulation_config
# mes_downtime_duration_min / _max, seeded by lambda-db-init
# seed_simulation_config, moved on existing accounts by its
# migrate_moved_config_defaults). A stop is written with its end time when it
# starts, so the cap is also how far ahead of the cycle a stop can end; it
# stays at an hour. The floor stays 5 minutes, longer than a deployed work
# order runs (2 to 4 minutes), as before.
DEFAULT_DOWNTIME_DURATION_MIN = 5
DEFAULT_DOWNTIME_DURATION_MAX = 60
# inv_cdf takes the open interval (0, 1); float rounding can land a draw on
# either end of it.
_OPEN_UNIT_LOW, _OPEN_UNIT_HIGH = math.nextafter(0.0, 1.0), math.nextafter(1.0, 0.0)


def draw_downtime_seconds(category: Optional[str], floor_minutes: int, cap_minutes: int,
                          rng=random) -> int:
    """The length in whole seconds of one random ``category`` downtime, from
    ``floor_minutes`` to ``cap_minutes`` inclusive (taken the other way round
    when the floor is above the cap; a bound below 1 minute counts as 1).

    The category's lognormal (``DOWNTIME_DURATION_PROFILES``) is truncated to
    that range, not clamped: one uniform draw goes through the inverse CDF of
    the part of the distribution that rounds into the range, so each length
    in it keeps its own probability and none piles up on the floor or the
    cap. A range so far into a tail that the float CDF has no mass left there
    gives the bound nearer the shape (the floor of a range above it, the cap
    of one below it). Pure apart from ``rng``: exactly one ``rng.random()``
    call (pass a seeded ``random.Random`` in tests).
    """
    lo, hi = sorted((max(1, int(floor_minutes)) * 60, max(1, int(cap_minutes)) * 60))
    median, sigma = DOWNTIME_DURATION_PROFILES.get(category, DEFAULT_DOWNTIME_PROFILE)
    dist = NormalDist(math.log(median * 60), sigma)
    r = rng.random()  # nosec B311 - synthetic data generation, not security context
    # The lengths that round to lo..hi run from lo - 0.5 to hi + 0.5 seconds.
    a, b = dist.cdf(math.log(lo - 0.5)), dist.cdf(math.log(hi + 0.5))
    if b <= a:
        return lo if lo >= median * 60 else hi
    u = min(max(a + (b - a) * r, _OPEN_UNIT_LOW), _OPEN_UNIT_HIGH)
    return min(hi, max(lo, round(math.exp(dist.inv_cdf(u)))))


def downtime_length_text(seconds: int) -> str:
    """``seconds`` as the resolution note states it: ``"37 min 25 s"``, or
    ``"37 min"`` on a whole minute (the form every earlier note has). Minutes
    and seconds, not decimal minutes: a note of ``37.42 min`` reads as 37
    minutes 42 seconds, and this form is exact."""
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


# ---------------------------------------------------------------------------
# Per-site MES backend switch
# ---------------------------------------------------------------------------
# A site's MES backend is read EVERY cycle from a simulation_config row keyed
# ``mes_backend:<SITE_CODE>`` (so flipping it needs no redeploy). ``builtin``
# (the default; an absent or unrecognised value also means builtin) runs the
# in-built MES simulation for that site. ``external`` steps the built-in MES
# aside for NEW work: it creates no work orders for that site, and an external
# integrator claims the site's RELEASED runs (erp_simulation/external_mes.py).
# Ownership is per run: a claimed run never gets a built-in work order, and a
# built-in work order created before a flip runs to completion. See
# docs/tulip-external-mes-switch.md. The constants and the key builder live
# in external_mes.py and are importable from here as before.


# RELEASED runs at a site that have no MES work order yet (the built-in MES
# takes them). The claims form adds the external-MES ownership rule and locks
# the rows it returns, so an integrator's claim and a work order never land on
# the same run.
_RELEASED_RUNS_SQL = """
            SELECT pr.run_id, pr.run_number, pr.product_id, pr.planned_quantity,
                   p.sku, p.category
            FROM production_runs pr
            JOIN products p ON pr.product_id = p.product_id
            WHERE pr.status = 'RELEASED'
              AND pr.site = %s
              AND NOT EXISTS (
                  SELECT 1 FROM mes_work_orders mwo
                  WHERE mwo.production_run_id = pr.run_id
              )
            ORDER BY pr.created_at ASC
        """
_RELEASED_RUNS_CLAIMS_SQL = _RELEASED_RUNS_SQL.replace(
    "            ORDER BY pr.created_at ASC\n",
    f"              AND {unclaimed('pr.run_id')}\n"
    "            ORDER BY pr.created_at ASC\n"
    "            FOR UPDATE OF pr SKIP LOCKED\n")


class MESSimulator(BaseSimulator):
    """
    Simulates Manufacturing Execution System operations.
    
    Creates MES work orders from production runs, simulates bottling line operations,
    tracks quality checks, material consumption, downtime events, and calculates OEE.
    """
    
    # Site code → CMMS asset-tag prefix — the ONE source of truth for the
    # MES-downtime → CMMS work-order bridge. Must match the asset tags
    # (modeled after IBM Maximo) seeded by lambda-adt-seed/004_cmms_maintenance.sql
    # (MKE-*, DET-*, CRR-*). Note Cedar Rapids is CRR, not CR.
    # Indianapolis (ADR-0083) is the IND site of the simulator; its CMMS is
    # the SEPARATE cmms_indianapolis schema (see CMMS_SITE_TARGET).
    CMMS_SITE_PREFIX = {
        'MILWAUKEE_BOTTLING': 'MKE',
        'DETROIT_AUTOPARTS': 'DET',
        'CEDARRAPIDS_FOOD': 'CRR',
        'INDIANAPOLIS_MACHINING': 'IND',
    }

    # Site code → (schema, asset-number prefix) for sites whose CMMS is NOT
    # cmms_maintenance. Indianapolis maintenance must land in
    # cmms_indianapolis (lambda-adt-seed/011_cmms_indianapolis.sql, IBM Maximo
    # ASSETNUM 1001-*): routing it into the already-onboarded cmms_maintenance
    # would make Indianapolis maintenance visible before the workshop's
    # module-4 onboarding and erase the module-3 wall. Sites absent here use
    # cmms_maintenance.work_orders_raw with CMMS_SITE_PREFIX.
    CMMS_SITE_TARGET = {
        'INDIANAPOLIS_MACHINING': ('cmms_indianapolis', '1001'),
    }
    # Indianapolis (IBM Maximo) failure codes: VIB-BEARING is the CMX-441 story
    # (emergency work order 1195, failure code BEARING).
    CMMS_IND_FAILURE_CODE = {
        'VIB-BEARING': 'BEARING', 'INSTR-DRIFT': 'DIM-DRIFT',
    }
    CMMS_IND_ASSET_TYPE = {'VIB-BEARING': 'CNC_MILL', 'INSTR-DRIFT': 'CNC_MILL'}
    # The story mill's own failure modes among the downtime reasons: the ones
    # the site CMMS codes as the story's failures above (BEARING, the
    # condition monitor's code; DIM-DRIFT, the drift order 1187) and the
    # bridge aims at the mill. At the condition-monitor site only the story
    # raises them (the condition monitor and the story history), so the
    # random downtime draw never picks them there (random_downtime_reasons).
    # That holds on every line: the bridge picks the site's asset by reason,
    # not by line, so a draw on any line reaches the mill the same way.
    STORY_FAILURE_REASONS = frozenset(CMMS_IND_FAILURE_CODE)
    # The MES logins of the technicians who resolve a downtime stop (the
    # stop's resolved_by; the bridge order's completion entry names the same
    # login). One list for the random downtime step and the story history.
    DOWNTIME_RESOLVERS = ('maint_01', 'maint_02', 'maint_03', 'supervisor')

    # Site code → the routine quality checks its lines run. Sites absent here
    # use the bottling list in __init__ (quality_check_types). Before this map
    # every site drew from that list, so precision shafts at the machining
    # site carried FILL_LEVEL and CAP_TORQUE checks (seen in the workshop's
    # first question, 2026-10-03). DIMENSIONAL is deliberately absent: it is
    # the machining site's defect signal (story_live / story_history), and a
    # routine DIMENSIONAL check on every other run would blur which batch
    # failed bore inspection. Every value must be allowed by the
    # mes_quality_checks check_type constraint (lambda-db-init
    # MES_QUALITY_CHECK_TYPES).
    SITE_QUALITY_CHECK_TYPES = {
        'INDIANAPOLIS_MACHINING': ('VISUAL', 'WEIGHT'),
        'DETROIT_AUTOPARTS': ('VISUAL', 'WEIGHT', 'LEAK_TEST'),
    }

    # Downtime reason → preferred CMMS asset_type for the bridge's asset pick.
    # Best-effort: types exist per site (fillers in MKE, CNCs in DET, mixers in
    # CRR); when the preferred type is absent at the site, the pick falls back
    # to any site asset. Reasons without a clear equipment type are omitted.
    CMMS_REASON_ASSET_TYPE = {
        'VIB-BEARING': 'cnc_machine',   # spindle/bearing vibration (DET precedent)
        'PUMP-FAIL': 'filler',          # filler pump failures (MKE precedent)
        'MECH-JAM': 'packager',
        'BELT-SLIP': 'checkweigher',    # conveyor-belt-equipped (spare-parts precedent)
        'INSTR-DRIFT': 'carbonator',    # transducer drift (MKE precedent)
        'MOTOR-OVL': 'mixer',
    }
    
    def __init__(self):
        """Initialize the MES simulator."""
        self.config = get_config()

        # Seed-DB connection for the MES-downtime → CMMS work-order bridge.
        # cmms_maintenance lives in the SEED database (separate host in cloud),
        # so the bridge must never write through the ERP connection.
        self._seed_db_conn = None
        self._seed_db_warned = False

        # Production lines configuration (3 lines total)
        self.production_lines = ['LINE1', 'LINE2', 'LINE3']

        # Target production rates will be loaded from config at runtime
        # Keeping defaults here for reference only
        self._default_target_rates = {
            'LINE1': 120,
            'LINE2': 120,
            'LINE3': 100
        }

        # Quality check types for bottling operations
        self.quality_check_types = [
            'FILL_LEVEL',
            'CAP_TORQUE',
            'LABEL_PLACEMENT',
            'LEAK_TEST',
            'WEIGHT',
            'VISUAL'
        ]

        # Downtime categories
        self.downtime_categories = [
            'MECHANICAL',
            'ELECTRICAL',
            'MATERIAL_SHORTAGE',
            'QUALITY_ISSUE',
            'CHANGEOVER',
            'OPERATOR'
        ]

        self.downtime_reasons = self._build_downtime_reasons()

        # Operator IDs
        self.operators = [f"OP-{i}" for i in range(1, 6)]
        self.inspectors = [f"QC-{i}" for i in range(1, 4)]
        self.technicians = [f"TECH-{i}" for i in range(1, 4)]

        logger.info("MES simulator initialized")

    @staticmethod
    def _build_downtime_reasons() -> Dict[str, list]:
        """Downtime reasons by category — uses CMMS failure codes for
        cross-system correlation (Neptune IdentityRules map CMMS asset tags
        to MES line IDs)."""
        return {
            'MECHANICAL': [
                'VIB-BEARING',    # Vibration/bearing fault — matches CMMS DET-CNC-001
                'MECH-JAM',       # Mechanical jam
                'BELT-SLIP',      # Conveyor belt slippage
                'PUMP-FAIL',      # Pump failure
            ],
            'ELECTRICAL': [
                'INSTR-DRIFT',    # Instrument/sensor drift — matches CMMS MKE-CARB-002
                'PLC-COMM',       # PLC communication error
                'MOTOR-OVL',      # Motor overload
                'POWER-FLUCT',    # Power fluctuation
            ],
            'MATERIAL_SHORTAGE': [
                'MAT-SHORT',      # Material shortage
                'TANK-EMPTY',     # Tank empty
                'SUPPLY-DELAY',   # Supply delay
            ],
            'QUALITY_ISSUE': [
                'TEMP-HEATER',    # Temperature/heater issue — matches CMMS CRR-EXT-001
                'FILL-SPEC',      # Fill level out of spec
                'CONTAM',         # Contamination detected
            ],
            'CHANGEOVER': [
                'CHANGEOVER',     # Product changeover
                'LABEL-CHANGE',   # Label roll replacement
            ],
            'OPERATOR': [
                'OP-BREAK',       # Operator break
                'SAFETY-INSP',    # Safety inspection
            ]
        }
    
    def get_active_sites(self, cursor) -> list:
        """Query active sites from the database, excluding legacy BOTTLING_PLANT."""
        cursor.execute("""
            SELECT site_code FROM sites WHERE is_active = TRUE
            AND site_code != 'BOTTLING_PLANT'
        """)
        return [row[0] for row in cursor.fetchall()]

    def get_next_available_line(self, cursor, site_code: str, product_id: str = None) -> Optional[str]:
        """
        Get the next available production line scoped to a specific site.
        
        A line is busy if it has:
        - A RUNNING MES work order, OR
        - A SCHEDULED MES work order (reserved for upcoming production)
        A line in :meth:`reserved_lines` (the live story's machine) is never
        available.
        
        Args:
            cursor: Database cursor.
            site_code: Site code to scope line availability check.
            product_id: Optional product ID to check for same-product line preference.
            
        Returns:
            Available line name or None if all lines are busy.
        """
        # Check which lines are currently busy with RUNNING or SCHEDULED MES work orders
        # Scoped per site so line availability at one site doesn't affect another
        cursor.execute("""
            SELECT DISTINCT mwo.line
            FROM mes_work_orders mwo
            WHERE mwo.status IN ('RUNNING', 'SCHEDULED')
              AND mwo.site = %s AND mwo.line IS NOT NULL
        """, (site_code,))
        
        busy_lines = {row[0] for row in cursor.fetchall()}
        reserved = self.reserved_lines(site_code)
        
        # Find first available line
        for line in self.production_lines:
            if line not in busy_lines and line not in reserved:
                return line
        
        if reserved - busy_lines:
            logger.debug(f"{site_code}: {sorted(reserved)} runs only live-story work; no free line")
        return None  # All lines busy

    def story_line_reserved(self) -> bool:
        """True while the live workshop story is on: the package says so
        (:attr:`STORY_LINE_ENV` is ``1``, set on a live synth only, so it holds
        from the first cycle, before the story-history custom resource writes
        the anchor, and through a reset that wipes it) or this cycle's story
        anchor is stored (:attr:`condition_anchor_epoch` > 0). An unknown clock
        (``None``, the read failed) is not evidence: without the flag a demo or
        frozen plant stays exactly as before."""
        if os.environ.get(self.STORY_LINE_ENV, '').strip() == '1':
            return True
        anchor = self.condition_anchor_epoch
        return anchor is not None and anchor > 0

    def reserved_lines(self, site_code: str) -> frozenset:
        """Lines at ``site_code`` the simulator gives no work to: the story
        machine's line (:data:`condition_monitor.STORY_MES_LINE` at
        :attr:`CONDITION_MONITOR_SITE`) while :meth:`story_line_reserved`.
        That line then runs only story work: the history's runs and
        ``story_live``'s alarm run. So no simulator run can sit inside a
        history run a placement re-times, the alarm run always finds the line
        idle, and every account gives the same answer to which runs the
        machine made after the failed batch. Empty everywhere else."""
        if site_code == self.CONDITION_MONITOR_SITE and self.story_line_reserved():
            return frozenset((condition_monitor.STORY_MES_LINE,))
        return frozenset()
    
    def create_mes_work_orders(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Generate MES work orders from production runs.
        
        Iterates over all active sites, creates MES work orders for released
        production runs at each site. Assigns lines based on per-site capacity.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with work order creation statistics.
        """
        cursor = conn.cursor()
        
        try:
            # Check if MES is in outage mode (NEW FEATURE)
            outage_enabled = self._load_config_value(cursor, 'mes_outage_enabled', False, 'boolean')
            if outage_enabled:
                logger.info("MES outage active - skipping work order creation")
                return {'work_orders_created': 0, 'outage_active': True}
            
            # Get all active sites (excludes legacy BOTTLING_PLANT)
            active_sites = self.get_active_sites(cursor)
            
            if not active_sites:
                logger.info("No active sites found for MES work order creation")
                return {'work_orders_created': 0}
            
            logger.info(f"MES checking {len(active_sites)} active sites: {active_sites}")
            self._log_released_runs(cursor)
            
            work_orders_created = 0
            external_sites = []
            # Once per cycle: with the claims table, claimed runs are skipped;
            # without it, the work-order query is exactly the one it always was.
            claims = claims_table_present(cursor)
            for site_code in active_sites:
                if self._site_mes_backend(cursor, site_code) == MES_BACKEND_EXTERNAL:
                    # An external MES (e.g. Tulip) takes this site's new builds:
                    # the built-in MES creates no work orders here, and the
                    # integrator claims the RELEASED runs (external_mes.py).
                    external_sites.append(site_code)
                    continue
                work_orders_created += self._create_site_work_orders(cursor, site_code, claims=claims)

            if external_sites:
                logger.info("MES backend 'external' for %s - built-in MES stepping aside "
                            "(no work orders; runs await the external integrator)",
                            ", ".join(external_sites))
            logger.info(f"Created {work_orders_created} MES work orders (cycle: {cycle_id})")
            
            return {
                'work_orders_created': work_orders_created,
                'cycle_id': cycle_id,
                'external_sites': external_sites,
            }
        
        except Exception as e:
            logger.error(f"MES work order creation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _site_mes_backend(self, cursor, site_code: str) -> str:
        """``site_code``'s MES backend, read fresh from simulation_config every
        cycle (``mes_backend:<SITE>``): ``external`` only for the exact value,
        otherwise ``builtin`` (absent, blank or unrecognised ⇒ builtin, so an
        account that never set the key behaves exactly as before)."""
        return resolve_mes_backend(self._load_config_value(
            cursor, mes_backend_config_key(site_code), MES_BACKEND_BUILTIN, 'string'))

    def _log_released_runs(self, cursor) -> None:
        """Debug: log which RELEASED runs exist and their site values."""
        cursor.execute("SELECT run_number, site, status FROM production_runs WHERE status = 'RELEASED' LIMIT 10")
        released_runs = cursor.fetchall()
        if released_runs:
            logger.info(f"RELEASED production runs: {[(r[0], r[1]) for r in released_runs]}")
        else:
            logger.info("No RELEASED production runs found in database")

    def _create_site_work_orders(self, cursor, site_code, claims: bool = False) -> int:
        """Create MES work orders for ``site_code``'s RELEASED runs that lack
        one; returns how many were created. A per-run failure is logged and
        the next run continues. ``claims``: the database has the external-MES
        claims table, so runs an external MES claimed are skipped and the
        selected runs are locked against a concurrent claim."""
        # Find RELEASED production runs without MES work orders for this site
        cursor.execute(_RELEASED_RUNS_CLAIMS_SQL if claims else _RELEASED_RUNS_SQL, (site_code,))
        
        runs_needing_work_orders = cursor.fetchall()
        
        if not runs_needing_work_orders:
            logger.debug(f"No production runs need MES work orders at {site_code}")
            return 0
        
        logger.info(f"Found {len(runs_needing_work_orders)} production runs needing MES work orders at {site_code}")
        
        created = 0
        for run_id, run_number, product_id, planned_quantity, sku, category in runs_needing_work_orders:
            try:
                inserted = self._insert_run_work_order(
                    cursor, site_code, run_id, run_number, product_id, planned_quantity, sku, category)
                if inserted is None:
                    continue
                work_order_number, work_order_id, available_line = inserted
                created += 1
                self._log_lot_tracking(cursor, work_order_id, work_order_number, run_id, run_number,
                                       site_code, available_line)
                logger.debug(
                    f"Created MES work order {work_order_number} for production run {run_number}: "
                    f"site {site_code}, product {sku}, quantity {planned_quantity}, assigned to {available_line}"
                )
            except Exception as e:
                logger.error(f"Failed to create MES work order for run {run_number}: {e}", exc_info=True)
                # Continue with next run
        return created

    def _insert_run_work_order(self, cursor, site_code, run_id, run_number,
                               product_id, planned_quantity, sku, category):
        """Insert one run's MES work order; returns (work_order_number,
        work_order_id, line), or None when skipped (no routing, no free line,
        or number skipped — retried next cycle)."""
        # Verify routing exists for this product's category
        cursor.execute("""
            SELECT 1 FROM product_site_routing psr
            JOIN sites s ON psr.site_id = s.site_id
            WHERE psr.product_category = %s
              AND psr.is_active = TRUE
            LIMIT 1
        """, (category,))
        
        if not cursor.fetchone():
            logger.warning(f"No routing entry for category '{category}' (product {sku}), skipping work order creation")
            return None
        
        # Check if a line is available at this site
        available_line = self.get_next_available_line(cursor, site_code)
        
        if not available_line:
            # All lines busy at this site, skip creating work order for now
            logger.debug(f"All lines busy at {site_code}, skipping work order creation for {run_number}")
            return None
        
        created = self._insert_mes_work_order(
            cursor, run_id, product_id, site_code, available_line, planned_quantity)
        if created is None:  # number skipped (logged): the run is retried next cycle
            return None
        return created[0], created[1], available_line

    def _log_lot_tracking(self, cursor, work_order_id, work_order_number, run_id, run_number,
                          site_code, line) -> None:
        """Create lot_mes_tracking rows for this production run's lots,
        allocating them first when the run was planned without any."""
        self._ensure_run_lots(cursor, run_id, site_code)
        tracking_rows = self._create_lot_mes_tracking(
            cursor,
            work_order_id,
            work_order_number,
            run_id,
            site_code,
            line,
        )
        if tracking_rows > 0:
            logger.debug(
                f"Created {tracking_rows} lot_mes_tracking rows for "
                f"work order {work_order_number} (run {run_number})"
            )

    @staticmethod
    def _ensure_run_lots(cursor, run_id, site_code: str) -> int:
        """An order-driven (backorder) run is planned without lots. Allocate them
        FIFO when its MES work order is created, so lot_mes_tracking and the MES
        consumption rows carry the real ERP lots (a replenishment run already
        has its allocation and is left alone). Returns rows allocated."""
        cursor.execute("SELECT 1 FROM lot_production_usage WHERE production_run_id = %s LIMIT 1", (run_id,))
        if cursor.fetchone():
            return 0
        cursor.execute("SELECT product_id, planned_quantity FROM production_runs WHERE run_id = %s", (run_id,))
        row = cursor.fetchone()
        if not row or not row[1]:
            return 0
        from .production_simulator import ProductionSimulator  # deferred: avoids an import cycle
        return ProductionSimulator.allocate_run_lots(cursor, run_id, row[0], int(row[1]), site_code)

    def _insert_mes_work_order(self, cursor, run_id, product_id, site_code, line, planned_quantity):
        """Insert a SCHEDULED work order on ``line`` (configurable target rate);
        returns (work_order_number, work_order_id), or None when the number was
        skipped (ADR-0090)."""
        target_rate = self._line_target_rate(cursor, line)
        scheduled_start_time = datetime.now()
        inserted = insert_numbered(cursor, 'WO', """
            INSERT INTO mes_work_orders (
                work_order_number, production_run_id, product_id,
                site, line, planned_quantity, status,
                scheduled_start_time, target_rate_per_minute
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING work_order_id
        """, lambda number: (number, run_id, product_id, site_code, line, planned_quantity,
                             'SCHEDULED', scheduled_start_time, target_rate),
            what=f"MES work order for run {run_id}")
        return None if inserted is None else (inserted[0], inserted[1][0])

    def _line_target_rate(self, cursor, line: str) -> int:
        """``line``'s target rate per minute (``mes_<line>_rate``, else the default)."""
        return self._load_config_value(
            cursor, f'mes_{line.lower()}_rate', self._default_target_rates.get(line, 100), 'int')

    # A SCHEDULED order still on a reserved line that cannot move yet is put
    # this far ahead, so this cycle's event step does not start it; every
    # cycle tries the move again first, so while the live story is on it
    # never starts there, and a few failed cycles in a row do not let it.
    RESERVED_LINE_RETRY = timedelta(minutes=10)

    def clear_reserved_lines(self, conn, cycle_id: str) -> Dict[str, Any]:
        """Cycle step, ahead of :meth:`create_mes_work_orders` and
        :meth:`simulate_production_events`: no SCHEDULED work order is due on a
        line :meth:`reserved_lines` keeps.

        Only an account whose MES ran before the line was reserved holds such
        an order. It has not started, so its line is only an assignment: each
        one moves to the next free line (:meth:`_move_off_reserved_line`), or,
        with no free line, is rescheduled :attr:`RESERVED_LINE_RETRY` ahead and
        tried again next cycle, the way a new run waits in RELEASED. Off the
        live story nothing is reserved and no SQL runs."""
        site = self.CONDITION_MONITOR_SITE
        lines = sorted(self.reserved_lines(site))
        if not lines:
            return {'reserved_line_orders_moved': 0, 'reserved_line_orders_waiting': 0}
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT work_order_id, work_order_number, line FROM mes_work_orders
                WHERE status = 'SCHEDULED' AND site = %s AND line = ANY(%s)
                ORDER BY scheduled_start_time, work_order_number
            """, (site, lines))
            moved = waiting = 0
            for work_order_id, work_order_number, line in cursor.fetchall():
                if self._move_off_reserved_line(cursor, work_order_id, work_order_number, site, line):
                    moved += 1
                else:
                    self._wait_on_reserved_line(cursor, work_order_id, work_order_number, site, line)
                    waiting += 1
            return {'reserved_line_orders_moved': moved, 'reserved_line_orders_waiting': waiting,
                    'cycle_id': cycle_id}
        finally:
            cursor.close()

    def _move_off_reserved_line(self, cursor, work_order_id, work_order_number,
                                site_code: str, line: str) -> Optional[str]:
        """Move a SCHEDULED work order off reserved ``line`` onto the next free
        line (:meth:`get_next_available_line`, never a reserved one) with that
        line's target rate and its ``lot_mes_tracking`` rows, the only rows
        that record the line before a start; due now if an earlier cycle had
        put it ahead. Returns the new line, or None when no line is free."""
        new_line = self.get_next_available_line(cursor, site_code)
        if new_line is None:
            return None
        cursor.execute("""
            UPDATE mes_work_orders
            SET line = %s, target_rate_per_minute = %s,
                scheduled_start_time = LEAST(scheduled_start_time, LOCALTIMESTAMP), updated_at = NOW()
            WHERE work_order_id = %s AND status = 'SCHEDULED'
        """, (new_line, self._line_target_rate(cursor, new_line), work_order_id))
        cursor.execute("UPDATE lot_mes_tracking SET line = %s WHERE work_order_id = %s",
                       (new_line, work_order_id))
        logger.info(f"Moved SCHEDULED work order {work_order_number} at {site_code} from {line} to "
                    f"{new_line}: {line} runs only live-story work")
        return new_line

    def _wait_on_reserved_line(self, cursor, work_order_id, work_order_number,
                               site_code: str, line: str) -> None:
        """Keep a SCHEDULED work order on reserved ``line`` from being due this
        cycle: its start goes :attr:`RESERVED_LINE_RETRY` ahead (never earlier)."""
        cursor.execute("""
            UPDATE mes_work_orders
            SET scheduled_start_time = GREATEST(scheduled_start_time, LOCALTIMESTAMP + %s), updated_at = NOW()
            WHERE work_order_id = %s AND status = 'SCHEDULED'
        """, (self.RESERVED_LINE_RETRY, work_order_id))
        logger.debug(f"{work_order_number} waits: {line} at {site_code} runs only live-story work "
                     f"and no other line is free")

    def simulate_production_events(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Simulate production events for MES work orders.
        
        Generates START events when work orders begin, STOP events when they complete,
        and CHANGEOVER events between different products.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with production event statistics.
        """
        cursor = conn.cursor()
        
        try:
            events_created = 0
            
            # 1. Start scheduled work orders
            cursor.execute("""
                SELECT mwo.work_order_id, mwo.work_order_number, mwo.product_id, mwo.line,
                       mwo.planned_quantity, mwo.target_rate_per_minute, mwo.production_run_id
                FROM mes_work_orders mwo
                WHERE mwo.status = 'SCHEDULED'
                  AND mwo.scheduled_start_time <= NOW()
            """)
            
            work_orders_to_start = cursor.fetchall()
            
            for work_order_id, work_order_number, product_id, line_id, planned_quantity, target_rate, production_run_id in work_orders_to_start:
                try:
                    # Update work order status to RUNNING
                    actual_start_time = datetime.now()
                    
                    # Calculate planned_end_time based on production rate
                    # For demo: configurable production time with varying lengths
                    # This means with 3 lines, a line finishes every 1-2 minutes on average
                    production_min = self._load_config_value(cursor, 'mes_production_duration_min', 2, 'int')
                    production_max = self._load_config_value(cursor, 'mes_production_duration_max', 4, 'int')
                    production_minutes = random.randint(production_min, production_max)  # nosec B311 - synthetic data generation, not security context
                    planned_end_time = actual_start_time + timedelta(minutes=production_minutes)
                    
                    cursor.execute("""
                        UPDATE mes_work_orders
                        SET status = 'RUNNING',
                            actual_start_time = %s,
                            updated_at = NOW()
                        WHERE work_order_id = %s
                    """, (actual_start_time, work_order_id))
                    
                    # MES starting work order updates Production run (PLANNED → IN_PROGRESS)
                    # and assigns the line from the work order, and sets planned_end_time
                    cursor.execute("""
                        UPDATE production_runs
                        SET status = 'IN_PROGRESS',
                            actual_start_time = %s,
                            planned_end_time = %s,
                            line = %s,
                            updated_at = NOW()
                        WHERE run_id = %s
                    """, (actual_start_time, planned_end_time, line_id, production_run_id))
                    
                    # Verify the production run was updated
                    if cursor.rowcount == 0:
                        logger.warning(f"Failed to update production run to IN_PROGRESS for work order {work_order_id}")
                    else:
                        logger.debug(f"Updated production run to IN_PROGRESS for work order {work_order_id}")
                        
                        # Consume materials when MES starts production
                        self._consume_materials_for_work_order(cursor, work_order_id, planned_quantity)
                    
                    # Create START event
                    operator_id = random.choice(self.operators)  # nosec B311 - synthetic data generation, not security context
                    
                    cursor.execute("""
                        INSERT INTO mes_production_events (
                            work_order_id, event_type, event_time,
                            operator_id, quantity_at_event
                        )
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        work_order_id,
                        'START',
                        actual_start_time,
                        operator_id,
                        0
                    ))
                    
                    events_created += 1
                    
                    logger.debug(
                        f"Started work order {work_order_number} on {line_id} "
                        f"with operator {operator_id}"
                    )
                
                except Exception as e:
                    logger.error(f"Failed to start work order {work_order_number}: {e}", exc_info=True)
            
            # 2. Complete running work orders (based on production run planned duration)
            cursor.execute("""
                SELECT mwo.work_order_id, mwo.work_order_number, mwo.product_id,
                       mwo.line, mwo.planned_quantity, mwo.actual_start_time,
                       mwo.target_rate_per_minute, pr.planned_end_time
                FROM mes_work_orders mwo
                JOIN production_runs pr ON mwo.production_run_id = pr.run_id
                WHERE mwo.status = 'RUNNING'
                  AND mwo.actual_start_time IS NOT NULL
                  AND pr.planned_end_time <= NOW()
            """)
            
            work_orders_to_complete = cursor.fetchall()
            
            for work_order_id, work_order_number, product_id, line_id, planned_quantity, actual_start_time, target_rate, planned_end_time in work_orders_to_complete:
                try:
                    # Calculate actual production (use planned end time from production run)
                    actual_end_time = datetime.now()
                    production_minutes = (actual_end_time - actual_start_time).total_seconds() / 60
                    
                    # Actual quantity with small variance (95-100% of planned)
                    actual_quantity = int(planned_quantity * random.uniform(0.95, 1.0))  # nosec B311 - synthetic data generation, not security context
                    
                    # Update work order status to COMPLETED
                    cursor.execute("""
                        UPDATE mes_work_orders
                        SET status = 'COMPLETED',
                            actual_end_time = %s,
                            actual_quantity = %s,
                            updated_at = NOW()
                        WHERE work_order_id = %s
                    """, (actual_end_time, actual_quantity, work_order_id))

                    # Roll up quality checks + compute full OEE (Availability × Performance × Quality)
                    # Availability: (run_time - downtime) / run_time  — uses mes_downtime_events
                    # Performance:  actual_qty / (target_rate × run_minutes)
                    # Quality:      pass_count / sample_size from mes_quality_checks

                    # Downtime for this work order
                    cursor.execute("""
                        SELECT COALESCE(SUM(
                            EXTRACT(EPOCH FROM (COALESCE(end_time, NOW()) - start_time)) / 60
                        ), 0)
                        FROM mes_downtime_events
                        WHERE work_order_id = %s
                    """, (work_order_id,))
                    downtime_minutes = float(cursor.fetchone()[0] or 0)

                    # Quality check totals
                    cursor.execute("""
                        SELECT COALESCE(SUM(pass_count), 0),
                               COALESCE(SUM(fail_count), 0),
                               COALESCE(SUM(sample_size), 0)
                        FROM mes_quality_checks
                        WHERE work_order_id = %s
                    """, (work_order_id,))
                    qc_row = cursor.fetchone()
                    total_pass = int(qc_row[0])
                    total_fail = int(qc_row[1])
                    total_sample = int(qc_row[2])

                    # Compute OEE components in Python — no UPDATE...FROM, no missing columns
                    run_minutes = max(production_minutes, 0.1)
                    avail, perf, qual, oee_pct = compute_oee_components(
                        run_minutes, downtime_minutes, actual_quantity,
                        target_rate, total_pass, total_sample,
                    )
                    good_qty, reject_qty = derive_good_reject(
                        actual_quantity, total_pass, total_sample,
                        fallback_yield=random.uniform(0.97, 0.995),  # nosec B311 - synthetic data generation, not security context
                    )
                    # Store Availability/Performance as 0-100 percentages so they display
                    # consistently with pass_rate and oee_percentage on the OEE gauge.
                    avail_pct = round(avail * 100, 2)
                    perf_pct = round(perf * 100, 2)

                    cursor.execute("""
                        UPDATE mes_work_orders
                        SET good_quantity = %s,
                            reject_quantity = %s,
                            availability = %s,
                            performance = %s,
                            oee_percentage = %s,
                            updated_at = NOW()
                        WHERE work_order_id = %s
                    """, (good_qty, reject_qty, avail_pct, perf_pct, oee_pct, work_order_id))
                    
                    # Update production run status to COMPLETED (MES updates Production)
                    cursor.execute("""
                        UPDATE production_runs
                        SET status = 'COMPLETED',
                            actual_end_time = %s,
                            actual_quantity = %s,
                            updated_at = NOW()
                        WHERE run_id = (
                            SELECT production_run_id FROM mes_work_orders WHERE work_order_id = %s
                        )
                    """, (actual_end_time, actual_quantity, work_order_id))
                    
                    # Verify the production run was updated
                    if cursor.rowcount == 0:
                        logger.warning(f"Failed to update production run for work order {work_order_id}")
                    else:
                        logger.debug(f"Updated production run to COMPLETED for work order {work_order_id}")
                    
                    # Create STOP event
                    cursor.execute("""
                        INSERT INTO mes_production_events (
                            work_order_id, event_type, event_time,
                            quantity_at_event
                        )
                        VALUES (%s, %s, %s, %s)
                    """, (
                        work_order_id,
                        'STOP',
                        actual_end_time,
                        actual_quantity
                    ))
                    
                    events_created += 1
                    
                    logger.debug(
                        f"Completed work order {work_order_number} on {line_id}: "
                        f"produced {actual_quantity} units in {production_minutes:.1f} minutes"
                    )
                
                except Exception as e:
                    logger.error(f"Failed to complete work order {work_order_number}: {e}", exc_info=True)
            
            # 3. Generate CHANGEOVER events (when switching products on a line)
            # Check for consecutive work orders on the same line with different products
            cursor.execute("""
                WITH consecutive_orders AS (
                    SELECT 
                        mwo1.work_order_id as current_wo_id,
                        mwo1.work_order_number as current_wo_number,
                        mwo1.line,
                        mwo1.product_id as current_product_id,
                        mwo2.product_id as previous_product_id,
                        mwo1.actual_start_time
                    FROM mes_work_orders mwo1
                    JOIN mes_work_orders mwo2 ON mwo1.line = mwo2.line
                    WHERE mwo1.status = 'RUNNING'
                      AND mwo2.status = 'COMPLETED'
                      AND mwo1.product_id != mwo2.product_id
                      AND mwo1.actual_start_time > mwo2.actual_end_time
                      AND NOT EXISTS (
                          SELECT 1 FROM mes_production_events mpe
                          WHERE mpe.work_order_id = mwo1.work_order_id
                            AND mpe.event_type = 'CHANGEOVER'
                      )
                )
                SELECT * FROM consecutive_orders
                LIMIT 5
            """)
            
            changeover_candidates = cursor.fetchall()
            
            for current_wo_id, current_wo_number, line_id, current_product_id, previous_product_id, actual_start_time in changeover_candidates:
                try:
                    # Create CHANGEOVER event
                    changeover_time = actual_start_time - timedelta(minutes=random.randint(10, 30))  # nosec B311 - synthetic data generation, not security context
                    changeover_duration = random.randint(10, 30)  # minutes  # nosec B311 - synthetic data generation, not security context
                    
                    cursor.execute("""
                        INSERT INTO mes_production_events (
                            work_order_id, event_type, event_time,
                            duration_seconds, reason_description
                        )
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        current_wo_id,
                        'CHANGEOVER',
                        changeover_time,
                        changeover_duration * 60,
                        'Product changeover in progress'
                    ))
                    
                    events_created += 1
                    
                    logger.debug(
                        f"Created CHANGEOVER event for work order {current_wo_number} "
                        f"on {line_id}: {changeover_duration} minutes"
                    )
                
                except Exception as e:
                    logger.error(f"Failed to create changeover event: {e}", exc_info=True)
            
            logger.info(f"Created {events_created} production events (cycle: {cycle_id})")
            
            return {
                'events_created': events_created,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Production event simulation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def update_line_status(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Upsert current per-line status into mes_line_status.

        For every (site, line) that has MES work orders, derive the current
        machine_state (DOWN > CHANGEOVER > RUNNING > IDLE), accumulate cumulative
        runtime_hours for RUNNING lines, and record current line_speed / throughput.
        One row per (site, line), upserted via ON CONFLICT so the OEE / Line State
        cards always have a current snapshot.

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with line status update statistics.
        """
        cursor = conn.cursor()

        try:
            rows_updated = 0

            # All (site, line) pairs the MES knows about.
            cursor.execute("""
                SELECT DISTINCT site, line
                FROM mes_work_orders
                WHERE line IS NOT NULL
            """)
            site_lines = cursor.fetchall()

            for site, line in site_lines:
                # Open (unresolved) downtime on this line → DOWN.
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM mes_downtime_events d
                        JOIN mes_work_orders w ON d.work_order_id = w.work_order_id
                        WHERE w.site = %s AND w.line = %s AND d.end_time IS NULL
                    )
                """, (site, line))
                has_open_downtime = bool(cursor.fetchone()[0])

                # A RUNNING work order on this line → RUNNING (unless down).
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM mes_work_orders
                        WHERE site = %s AND line = %s AND status = 'RUNNING'
                    )
                """, (site, line))
                has_running_wo = bool(cursor.fetchone()[0])

                # A recent CHANGEOVER event with no later STOP/START → in changeover.
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM mes_production_events e
                        JOIN mes_work_orders w ON e.work_order_id = w.work_order_id
                        WHERE w.site = %s AND w.line = %s
                          AND e.event_type = 'CHANGEOVER'
                          AND e.event_time > NOW() - INTERVAL '30 minutes'
                    )
                """, (site, line))
                in_changeover = bool(cursor.fetchone()[0])

                machine_state = derive_machine_state(
                    has_running_wo, has_open_downtime, in_changeover)

                # Current line speed / throughput from recent RUNNING work orders.
                cursor.execute("""
                    SELECT COALESCE(AVG(actual_rate_per_minute), 0),
                           COALESCE(SUM(good_quantity), 0)
                    FROM mes_work_orders
                    WHERE site = %s AND line = %s
                      AND updated_at > NOW() - INTERVAL '1 hour'
                """, (site, line))
                rate_row = cursor.fetchone()
                line_speed = round(float(rate_row[0] or 0), 2)
                throughput = round(float(rate_row[1] or 0), 2)

                # Cumulative runtime: prior value + minutes since last update while RUNNING.
                cursor.execute("""
                    SELECT runtime_hours, updated_at
                    FROM mes_line_status
                    WHERE site = %s AND line = %s
                """, (site, line))
                prev = cursor.fetchone()
                runtime_hours = float(prev[0]) if prev and prev[0] is not None else 0.0
                if machine_state == 'RUNNING' and prev and prev[1] is not None:
                    cursor.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s)) / 3600",
                                   (prev[1],))
                    elapsed_hours = float(cursor.fetchone()[0] or 0)
                    runtime_hours += max(0.0, elapsed_hours)
                runtime_hours = round(runtime_hours, 2)

                cursor.execute("""
                    INSERT INTO mes_line_status (
                        site, line, machine_state, runtime_hours,
                        line_speed, throughput, state_since, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (site, line) DO UPDATE SET
                        machine_state = EXCLUDED.machine_state,
                        runtime_hours = EXCLUDED.runtime_hours,
                        line_speed = EXCLUDED.line_speed,
                        throughput = EXCLUDED.throughput,
                        state_since = CASE
                            WHEN mes_line_status.machine_state != EXCLUDED.machine_state
                            THEN NOW() ELSE mes_line_status.state_since END,
                        updated_at = NOW()
                """, (site, line, machine_state, runtime_hours, line_speed, throughput))
                rows_updated += 1

            conn.commit()
            logger.info(f"Updated {rows_updated} line status rows (cycle: {cycle_id})")

            return {
                'line_status_updated': rows_updated,
                'cycle_id': cycle_id
            }

        except Exception as e:
            logger.error(f"Line status update failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def simulate_quality_checks(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Simulate quality checks during production.

        Generates periodic quality checks for running work orders, using the
        check types of the site the work order runs at
        (:meth:`quality_check_types_for`). Each sampled unit passes at the
        configured rate (default 99.8-100 %), so most checks pass clean.

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with quality check statistics.
        """
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT mwo.work_order_id, mwo.work_order_number, mwo.line,
                       mwo.actual_start_time, mwo.planned_quantity, mwo.site
                FROM mes_work_orders mwo
                WHERE mwo.status = 'RUNNING'
                  AND mwo.actual_start_time IS NOT NULL
            """)
            running_work_orders = cursor.fetchall()
            if not running_work_orders:
                logger.debug("No running work orders for quality checks")
                return {'quality_checks_created': 0}

            checks_created = 0
            for work_order in running_work_orders:
                try:
                    checks_created += self._create_work_order_quality_checks(cursor, work_order)
                except Exception as e:
                    logger.error(f"Failed to create quality checks for work order {work_order[1]}: {e}", exc_info=True)

            logger.info(f"Created {checks_created} quality checks (cycle: {cycle_id})")
            return {
                'quality_checks_created': checks_created,
                'cycle_id': cycle_id
            }
        except Exception as e:
            logger.error(f"Quality check simulation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def quality_check_types_for(self, site_code) -> tuple:
        """The routine check types for ``site_code`` (SITE_QUALITY_CHECK_TYPES),
        or the bottling default for a site without its own list."""
        return tuple(self.SITE_QUALITY_CHECK_TYPES.get(site_code)
                     or self.quality_check_types)

    def _create_work_order_quality_checks(self, cursor, work_order) -> int:
        """Create the checks a running work order is due and return how many
        were created: one check as soon as the order is seen, then one every
        30 minutes after the latest recorded check.

        No check is dated later than now. The first check lands at start +
        30 min, or now if the order has run less than that (every deployed
        order, which finishes in 2-4 minutes; it used to be stamped 30 minutes
        after its start, a time that had not happened yet). Later checks
        follow the latest recorded one, so an early first check does not
        swallow the next half-hour slot."""
        work_order_id, work_order_number, _line, actual_start_time, _qty, site_code = work_order
        now = datetime.now()
        step = timedelta(minutes=30)
        # Count and latest time span checks of EVERY type on purpose: the story
        # step runs earlier in the cycle and writes DIMENSIONAL checks on the
        # story run, which keeps routine checks off it. Filtering this to
        # routine types would add routine checks to the story run and shift
        # its quality figure.
        cursor.execute("""
            SELECT COUNT(*), MAX(check_time)
            FROM mes_quality_checks
            WHERE work_order_id = %s
        """, (work_order_id,))
        existing_checks, latest = cursor.fetchone()
        due = []
        if not existing_checks or latest is None:
            due.append(min(actual_start_time + step, now))
            latest = due[0]
        while latest + step <= now:
            latest += step
            due.append(latest)
        check_types = self.quality_check_types_for(site_code)
        created = 0
        for check_time in due:
            check_type = random.choice(check_types)  # nosec B311 - synthetic data generation, not security context
            self._insert_quality_check(cursor, work_order_id, work_order_number,
                                       check_type, check_time)
            created += 1
        return created

    def _insert_quality_check(self, cursor, work_order_id, work_order_number,
                              check_type: str, check_time) -> None:
        """Insert one check with a sampled size, pass rate and inspector: each
        sampled unit passes at a rate drawn from the configured range, so the
        reject count is a binomial draw (:func:`sampled_rejects`)."""
        sample_size = random.randint(20, 50)  # nosec B311 - synthetic data generation, not security context
        pass_rate_min = self._load_config_value(
            cursor, 'mes_quality_pass_rate_min', DEFAULT_QUALITY_PASS_RATE_MIN, 'float')
        pass_rate_max = self._load_config_value(
            cursor, 'mes_quality_pass_rate_max', DEFAULT_QUALITY_PASS_RATE_MAX, 'float')
        pass_rate_target = random.uniform(pass_rate_min, pass_rate_max)  # nosec B311 - synthetic data generation, not security context
        fail_count = sampled_rejects(sample_size, 1.0 - pass_rate_target)
        pass_count = sample_size - fail_count
        inspector_id = random.choice(self.inspectors)  # nosec B311 - synthetic data generation, not security context
        cursor.execute("""
            INSERT INTO mes_quality_checks (
                work_order_id, check_time, check_type,
                sample_size, pass_count, fail_count,
                inspector_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (work_order_id, check_time, check_type, sample_size,
              pass_count, fail_count, inspector_id))
        logger.debug(
            f"Created {check_type} quality check for work order {work_order_number}: "
            f"{pass_count}/{sample_size} passed ({pass_count/sample_size*100:.1f}%)"
        )

    def track_material_consumption(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Track material consumption for completed MES work orders.

        Each consumption row records the ERP material lot the production run
        actually drew (``lot_production_usage``), so MES and ERP carry the same
        lot number. A component with no allocated lot is recorded with no lot
        number rather than an invented one. At most ``MAX_ROWS_PER_CYCLE`` work
        orders per cycle (``_work_orders_needing_consumption``).

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with material consumption statistics.
        """
        cursor = conn.cursor()
        try:
            pending = self._work_orders_needing_consumption(cursor, self._consumption_scan_start())
            if not pending:
                logger.debug("No work orders need material consumption tracking")
                return {'consumption_records_created': 0}
            logger.info(f"Found {len(pending)} work orders needing material consumption tracking")
            records_created = sum(
                self._track_work_order_consumption(cursor, work_order) for work_order in pending
            )
            logger.info(f"Created {records_created} material consumption records (cycle: {cycle_id})")
            return {'consumption_records_created': records_created, 'cycle_id': cycle_id}
        except Exception as e:
            logger.error(f"Material consumption tracking failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _consumption_scan_start(self) -> str:
        """A fresh random point in ``work_order_id`` (UUID) order where this
        cycle's consumption read starts (``_work_orders_needing_consumption``)."""
        return str(uuid.uuid4())

    @staticmethod
    def _work_orders_needing_consumption(cursor, start: str) -> list:
        """At most ``MAX_ROWS_PER_CYCLE`` completed MES work orders that have no
        consumption rows yet, from ``start`` in ``work_order_id`` order, wrapping
        round to the lowest ids (``_CONSUMPTION_PENDING_FROM_SQL``). Returned in
        ``work_order_id`` order: a backlog below the bound comes back whole and
        in the same order whatever the start, so the random quantities each
        work order draws do not depend on where the scan started."""
        cursor.execute(_CONSUMPTION_PENDING_FROM_SQL, (start, MAX_ROWS_PER_CYCLE))
        rows = list(cursor.fetchall())
        if len(rows) < MAX_ROWS_PER_CYCLE:
            cursor.execute(_CONSUMPTION_PENDING_BEFORE_SQL, (start, MAX_ROWS_PER_CYCLE - len(rows)))
            rows = list(cursor.fetchall()) + rows
        return rows

    def _track_work_order_consumption(self, cursor, work_order) -> int:
        """Consumption rows for one work order's BOM, one row per ERP lot drawn."""
        work_order_id, work_order_number, product_id, planned_quantity, _, run_id = work_order
        try:
            cursor.execute("""
                SELECT bom.component_item_id, bom.quantity_required, ii.item_code
                FROM bill_of_materials bom
                JOIN inventory_items ii ON bom.component_item_id = ii.item_id
                WHERE bom.product_id = %s
                  AND bom.is_active = TRUE
            """, (product_id,))
            bom_items = cursor.fetchall()
            if not bom_items:
                logger.warning(f"No BOM found for product in work order {work_order_number}")
                return 0
            variance_min = self._load_config_value(cursor, 'mes_material_variance_min', 1.02, 'float')
            variance_max = self._load_config_value(cursor, 'mes_material_variance_max', 1.05, 'float')
            lots = self._run_lots_by_item(cursor, run_id)
            created = 0
            for item_id, quantity_required, item_code in bom_items:
                planned = float(quantity_required) * float(planned_quantity)
                actual = planned * random.uniform(variance_min, variance_max)  # nosec B311 - synthetic data generation, not security context
                created += self._insert_consumption_rows(
                    cursor, work_order_id, item_id, planned, actual, lots.get(item_id))
                logger.debug(f"Tracked consumption for {work_order_number}: {item_code} "
                             f"planned {planned:.2f}, actual {actual:.2f}")
            return created
        except Exception as e:
            logger.error(f"Failed to track material consumption for work order {work_order_number}: {e}",
                         exc_info=True)
            return 0

    @staticmethod
    def _run_lots_by_item(cursor, production_run_id) -> Dict[Any, list]:
        """ERP lots the production run drew, keyed by item: ``[(lot_number, quantity_used)]``."""
        if production_run_id is None:
            return {}
        cursor.execute("""
            SELECT ml.item_id, ml.lot_number, lpu.quantity_used
            FROM lot_production_usage lpu
            JOIN material_lots ml ON ml.lot_id = lpu.lot_id
            WHERE lpu.production_run_id = %s
            ORDER BY lpu.usage_date, ml.lot_number
        """, (production_run_id,))
        lots: Dict[Any, list] = {}
        for item_id, lot_number, quantity_used in cursor.fetchall():
            lots.setdefault(item_id, []).append((lot_number, float(quantity_used or 0)))
        return lots

    @staticmethod
    def _insert_consumption_rows(cursor, work_order_id, item_id, planned, actual, lots) -> int:
        """Split one component's consumption across the lots drawn (no lot: one NULL-lot row)."""
        draws = lots or [(None, 1.0)]
        total = sum(quantity for _, quantity in draws)
        for lot_number, quantity in draws:
            share = quantity / total if total > 0 else 1.0 / len(draws)
            cursor.execute("""
                INSERT INTO mes_material_consumption (
                    work_order_id, item_id, planned_quantity,
                    actual_quantity, consumption_time, lot_number
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (work_order_id, item_id, planned * share, actual * share, datetime.now(), lot_number))
        return len(draws)

    def simulate_downtime_events(self, conn, cycle_id: str) -> Dict[str, Any]:
        """Simulate downtime events for production lines.

        Generates random downtime events (mechanical, material shortage,
        changeover) with realistic durations and resolution tracking; the
        MECHANICAL/ELECTRICAL events also bridge a corrective work order
        into the CMMS seed database. Also runs the condition monitor
        (predictive work order from the story mill's vibration spectrum).

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns a dictionary with downtime event statistics.
        """
        condition_wos = self._condition_monitor_for_cycle(cycle_id)
        cursor = conn.cursor()
        try:
            # Find running work orders that might experience downtime
            cursor.execute("""
                SELECT mwo.work_order_id, mwo.work_order_number, mwo.line,
                       mwo.actual_start_time, mwo.site
                FROM mes_work_orders mwo
                WHERE mwo.status = 'RUNNING'
                  AND mwo.actual_start_time IS NOT NULL
            """)
            running_work_orders = cursor.fetchall()
            if not running_work_orders:
                logger.debug("No running work orders for downtime simulation")
                return {'downtime_events_created': 0, 'condition_work_orders_created': condition_wos}

            events_created = 0
            self._resolve_stale_downtime_events(cursor)

            for work_order_id, work_order_number, line_id, actual_start_time, site_code in running_work_orders:
                try:
                    events_created += self._maybe_create_downtime_event(
                        cursor, work_order_id, work_order_number, line_id, actual_start_time, site_code)
                except Exception as e:
                    logger.error(f"Failed to create downtime event for work order {work_order_number}: {e}", exc_info=True)

            logger.info(f"Created {events_created} downtime events (cycle: {cycle_id})")
            return {'downtime_events_created': events_created, 'cycle_id': cycle_id,
                    'condition_work_orders_created': condition_wos}
        except Exception as e:
            logger.error(f"Downtime event simulation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _resolve_stale_downtime_events(self, cursor) -> None:
        """Resolve any active downtime events that have run long enough."""
        cursor.execute("""
            UPDATE mes_downtime_events
            SET end_time = NOW(),
                resolved_by = 'maint_auto',
                resolution_notes = 'Auto-resolved after downtime window'
            WHERE end_time IS NULL
              AND start_time <= NOW() - INTERVAL '20 minutes'
        """)
        if cursor.rowcount > 0:
            logger.info(f"Auto-resolved {cursor.rowcount} active downtime events")

    def _maybe_create_downtime_event(
        self, cursor, work_order_id, work_order_number: str, line_id: str, actual_start_time,
        site_code: Optional[str] = None,
    ) -> int:
        """Roll for and create ONE downtime event for a running work order.

        Applies the probability/dedupe gates, inserts the mes_downtime_events
        row, and (for MECHANICAL/ELECTRICAL) bridges a corrective work order
        into the CMMS seed database. The reason comes from
        :meth:`random_downtime_reasons` for the work order's ``site_code``.
        Returns 1 if an event was created, else 0.
        """
        if not self._downtime_gates_pass(cursor, work_order_id, line_id):
            return 0

        # Select random downtime category and a category-appropriate reason
        downtime_category = random.choice(self.downtime_categories)  # nosec B311 - synthetic data generation, not security context
        downtime_reason = random.choice(self.random_downtime_reasons(site_code, downtime_category))  # nosec B311 - synthetic data generation, not security context

        event = self._build_downtime_window(cursor, actual_start_time, downtime_category)
        if event is None:
            return 0
        start_time, end_time, duration_seconds = event

        downtime_id = self._insert_downtime_event(
            cursor, work_order_id, line_id, downtime_category, downtime_reason,
            start_time, end_time, duration_seconds,
        )

        # For MECHANICAL or ELECTRICAL downtime, create a CMMS work order in
        # the SEED database — this links MES downtime to the maintenance system.
        if downtime_category in ('MECHANICAL', 'ELECTRICAL'):
            self._create_cmms_work_order_for_downtime(
                cursor, work_order_id, line_id, downtime_category,
                downtime_reason, downtime_id, downtime_start=start_time,
            )

        logger.debug(
            f"Created downtime event for work order {work_order_number} on {line_id}: "
            f"{downtime_category} - {downtime_reason} ({downtime_length_text(duration_seconds)})"
        )
        return 1

    def random_downtime_reasons(self, site_code: Optional[str], category: str) -> list:
        """The reasons a random ``category`` downtime at ``site_code`` draws from.

        The category's list, except at the condition-monitor site, where the
        story mill's own failure modes (``STORY_FAILURE_REASONS``) are left
        out on every line: there the condition monitor and the story history
        raise them on the story clock, and a random draw would raise the same
        bearing order on the mill at a moment no story chose. Every category
        keeps at least one reason there (pinned by test)."""
        reasons = self.downtime_reasons[category]
        if site_code != self.CONDITION_MONITOR_SITE:
            return reasons
        return [reason for reason in reasons if reason not in self.STORY_FAILURE_REASONS]

    def _insert_downtime_event(
        self, cursor, work_order_id, line_id: str, downtime_category: str,
        downtime_reason: str, start_time, end_time, duration_seconds: int,
    ):
        """Insert the mes_downtime_events row and return its downtime_id.

        The resolution note states the stop's length in minutes and seconds
        (:func:`downtime_length_text`), the same length ``end_time -
        start_time`` gives and ``duration_minutes`` stores to two decimals."""
        # Select technician who resolved the issue
        resolved_by = random.choice(self.DOWNTIME_RESOLVERS)  # nosec B311 - synthetic data generation, not security context
        cursor.execute("""
            INSERT INTO mes_downtime_events (
                work_order_id, line_id, downtime_category,
                downtime_reason, start_time, end_time,
                resolved_by, resolution_notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING downtime_id
        """, (
            work_order_id, line_id, downtime_category, downtime_reason,
            start_time, end_time, resolved_by,
            f'Resolved after {downtime_length_text(duration_seconds)}',
        ))
        return cursor.fetchone()[0]

    def _downtime_gates_pass(self, cursor, work_order_id, line_id: str) -> bool:
        """Probability + dedupe gates for starting a new downtime event."""
        # Configurable downtime probability
        downtime_prob = self._load_config_value(cursor, 'mes_downtime_probability', 0.20, 'float')
        if random.random() > downtime_prob:  # nosec B311 - synthetic data generation, not security context
            return False

        # Don't start new downtime if this line already has active downtime
        cursor.execute("""
            SELECT COUNT(*) FROM mes_downtime_events d
            JOIN mes_work_orders mwo ON d.work_order_id = mwo.work_order_id
            WHERE mwo.line = %s AND d.end_time IS NULL
        """, (line_id,))
        if cursor.fetchone()[0] > 0:
            return False

        # Limit to 2 downtime events per work order
        cursor.execute("""
            SELECT COUNT(*)
            FROM mes_downtime_events
            WHERE work_order_id = %s
        """, (work_order_id,))
        return cursor.fetchone()[0] < 2

    def _build_downtime_window(self, cursor, actual_start_time, downtime_category=None):
        """Compute (start_time, end_time, duration_seconds) for a downtime event.

        The length is :func:`draw_downtime_seconds` for ``downtime_category``
        between the configured floor and cap. Both timestamps fall on a whole
        second (readers of them, such as the CMMS bridge that copies them onto
        its orders, take no sub-second part), so ``end_time - start_time`` is
        the drawn length exactly. Returns None when the work order started too
        recently (< 1 minute ago).
        """
        duration_min = self._load_config_value(
            cursor, 'mes_downtime_duration_min', DEFAULT_DOWNTIME_DURATION_MIN, 'int')
        duration_max = self._load_config_value(
            cursor, 'mes_downtime_duration_max', DEFAULT_DOWNTIME_DURATION_MAX, 'int')
        duration_seconds = draw_downtime_seconds(downtime_category, duration_min, duration_max, random)

        # Downtime start time (sometime during production)
        running_minutes = (datetime.now() - actual_start_time).total_seconds() / 60
        if running_minutes < 1:
            return None

        downtime_offset = random.randint(1, max(1, int(running_minutes)))  # nosec B311 - synthetic data generation, not security context
        start_time = (actual_start_time + timedelta(minutes=downtime_offset)).replace(microsecond=0)
        # Write end_time immediately so duration_minutes (generated column) is populated
        end_time = start_time + timedelta(seconds=duration_seconds)
        return start_time, end_time, duration_seconds

    def _create_cmms_work_order_for_downtime(
        self, erp_cursor, work_order_id, line_id: str,
        downtime_category: str, downtime_reason: str, downtime_id,
        downtime_start=None,
    ) -> None:
        """Bridge a MES downtime event to a corrective CMMS work order.

        ``downtime_start`` is the stop's own start (naive UTC, the downtime
        row's ``start_time``). A site CMMS order records it as its actual
        start, the time the machine went down for it; a corporate CMMS order
        records it as its creation time (``created_ts``).

        cmms_maintenance lives in the SEED database (a different host in cloud
        deploys), so the write goes through the seed-DB connection — NEVER the
        ERP connection. Failures are logged loudly but never break the MES
        cycle; if the seed-DB environment is absent the bridge is skipped
        (one loud warning from _get_seed_db_conn).
        """
        seed_conn = self._get_seed_db_conn()
        if seed_conn is None:
            return

        # Resolve the MES site (ERP-side lookup) to the CMMS asset-tag prefix
        erp_cursor.execute(
            "SELECT site FROM mes_work_orders WHERE work_order_id = %s",
            (work_order_id,),
        )
        site_row = erp_cursor.fetchone()
        site_code = site_row[0] if site_row else ''
        try:
            self._write_bridge_order(seed_conn, site_code, line_id, downtime_category,
                                     downtime_reason, downtime_id, downtime_start)
        except Exception as cmms_err:
            try:
                seed_conn.rollback()
            except Exception:  # nosec B110 - best-effort rollback on a failing connection
                pass
            logger.warning(
                f"MES→CMMS bridge FAILED for downtime {downtime_id}: {cmms_err}"
            )

    def _write_bridge_order(self, seed_conn, site_code: str, line_id: str,
                            downtime_category: str, downtime_reason: str, downtime_id,
                            downtime_start) -> None:
        """Write and commit the bridge's one order in the seed database: into
        the site's own CMMS for a site that has one, else into the corporate
        CMMS under the site's asset-tag prefix. Errors propagate."""
        seed_cursor = seed_conn.cursor()
        try:
            if site_code in self.CMMS_SITE_TARGET:
                self._insert_indianapolis_cmms_work_order(
                    seed_cursor, site_code, line_id,
                    downtime_category, downtime_reason, downtime_id, downtime_start,
                )
            else:
                self._insert_cmms_work_order(
                    seed_cursor, self.CMMS_SITE_PREFIX.get(site_code, 'MKE'), site_code,
                    line_id, downtime_category, downtime_reason, downtime_id,
                    downtime_start=downtime_start,
                )
            seed_conn.commit()
        finally:
            seed_cursor.close()

    def _insert_indianapolis_cmms_work_order(
        self, seed_cursor, site_code: str, line_id: str,
        downtime_category: str, downtime_reason: str, downtime_id,
        downtime_start=None,
    ) -> None:
        """Insert a work order in the IBM Maximo shape into the site's OWN CMMS schema.

        cmms_indianapolis.work_orders (not work_orders_raw): wonum is
        VARCHAR(15), work_type EM/CM, status WAPPR, priority 1-4, and the
        asset is picked by IBM Maximo ASSETNUM prefix (1001-*), preferring the
        CNC mill for vibration/drift reasons. The random downtime draw never
        offers those reasons at the condition-monitor site
        (STORY_FAILURE_REASONS): the condition monitor raises the mill's
        bearing order on the story clock. ``actual_start`` is the stop's own
        start (``downtime_start``; NULL when none is handed over): the order
        is worked in that stop, as its completion later records.
        """
        schema, asset_prefix = self.CMMS_SITE_TARGET[site_code]
        asset_num = self.indianapolis_bridge_asset(seed_cursor, site_code, downtime_reason)
        if asset_num is None:
            logger.warning(
                f"MES→CMMS bridge: no {schema} asset with prefix {asset_prefix}- "
                f"for downtime {downtime_id} — work order not created"
            )
            return
        wonum, work_type, priority, description, failure_code = self.indianapolis_bridge_order(
            site_code, line_id, downtime_category, downtime_reason, downtime_id)
        # Only the schema (a class constant) is in the text; every value is bound.
        seed_cursor.execute(sql_text("""
            INSERT INTO {schema}.work_orders
                (wonum, asset_num, work_type, description, status, priority,
                 reported_by, reported_date, target_start, sched_start, failure_code, crew,
                 actual_start)
            VALUES (%s, %s, %s, %s, 'WAPPR', %s, 'MES bridge', NOW(), NOW(), NOW(), %s, 'IND-MECH-A', %s)
            ON CONFLICT (wonum) DO NOTHING
        """, schema=schema), (wonum, asset_num, work_type, description, priority, failure_code, downtime_start))
        logger.info(
            f"Created {schema} WO {wonum} on asset {asset_num} "
            f"for MES downtime {downtime_id} ({line_id})"
        )

    @classmethod
    def indianapolis_bridge_asset(cls, seed_cursor, site_code: str, downtime_reason: str) -> Optional[str]:
        """The site CMMS asset the bridge raises a ``downtime_reason`` order on:
        the site's IBM Maximo ASSETNUM prefix, the reason's preferred asset
        type first (CMMS_IND_ASSET_TYPE), then by number; None when the site
        CMMS has no asset. One SELECT on ``seed_cursor``. The story history's
        past stops are raised on the asset this picks too."""
        schema, asset_prefix = cls.CMMS_SITE_TARGET[site_code]
        preferred_type = cls.CMMS_IND_ASSET_TYPE.get(downtime_reason, '')
        # Only the schema (a class constant) is in the text; every value is bound.
        seed_cursor.execute(sql_text("""
            SELECT asset_num FROM {schema}.assets
            WHERE asset_num LIKE %s
            ORDER BY (asset_type = %s) DESC, asset_num
            LIMIT 1
        """, schema=schema), (f'{asset_prefix}-%', preferred_type))
        asset_row = seed_cursor.fetchone()
        return asset_row[0] if asset_row else None

    @classmethod
    def indianapolis_bridge_order(cls, site_code: str, line_id: str, downtime_category: str,
                                  downtime_reason: str, downtime_id) -> tuple:
        """``(wonum, work_type, priority, description, failure_code)`` of the
        order the bridge raises for one stop: EM priority 1 for a MECHANICAL
        stop, else CM priority 2; the description names the MES downtime event
        it was raised from (what :meth:`_proven_bridge_downtime_id` reads back);
        the failure code is the site CMMS's code for the reason, else the
        reason itself. Pure; shared with the story history's past stops."""
        is_mechanical = downtime_category == 'MECHANICAL'
        description = (
            f"MES-triggered: {downtime_reason} on {line_id} "
            f"(site {site_code}) — MES downtime event {downtime_id}"
        )[:250]
        failure_code = cls.CMMS_IND_FAILURE_CODE.get(downtime_reason, downtime_reason)[:20]
        return (cls._bridge_wonum(downtime_id), 'EM' if is_mechanical else 'CM',
                1 if is_mechanical else 2, description, failure_code)

    # Condition monitoring (ADR-0073): the story mill's OPC UA server publishes
    # edge-FFT features of its spindle vibration; the rule below evaluates the
    # SAME curve at the SAME wall-clock loop step (condition_monitor.loop_step,
    # mirrored by components/opc-server/server.mjs) and raises a predictive
    # work order in the site's own CMMS the moment the BPFO band crosses its
    # threshold — while the mill is still running. One work order per loop.
    # The order stays open (waiting for approval) through its own loop: the
    # alarm, the bearing failure, the repair stop and the healthy tail. Once
    # that loop has ended the next cycle completes it, worked in the bearing
    # replacement its loop played, so a story account that runs for days has
    # at most one open condition order (complete_condition_work_orders).
    # The MES downtime bridge's orders follow the same rule in the same
    # transaction: once their loop has ended they are completed, worked in
    # the downtime they were raised from (complete_bridge_work_orders).
    CONDITION_MONITOR_SITE = 'INDIANAPOLIS_MACHINING'
    CONDITION_MONITOR_CREW = 'IND-MECH-A'
    # The live story owns the story machine's line (reserved_lines): a live
    # synth sets this env to "1" on the simulation Lambda
    # (stacks/_workshop_story.STORY_LINE_ENV_NAME).
    STORY_LINE_ENV = 'STORY_LINE_RESERVED'
    # The statuses a condition order is still open in (the site CMMS's open
    # IBM Maximo statuses, as story_history.OPEN_STATUSES) and the one it is
    # completed to, with the completion's log entry.
    CONDITION_OPEN_STATUSES = ('WAPPR', 'APPR', 'INPRG', 'WMATL')
    CONDITION_COMPLETED_STATUS = 'COMP'
    CONDITION_COMPLETION_AUTHOR = 'Maintenance planner'
    CONDITION_COMPLETION_ENTRY = (
        'The order was still open when the bearing failed. Bearing set '
        'replaced in the repair stop; machine returned to service.')
    # The bridge's work-order numbers ("IND-" + 8 hex chars of the downtime
    # id, wonum VARCHAR(15)) and its orders' completion log entry.
    CMMS_BRIDGE_WONUM_PREFIX = 'IND-'
    CMMS_BRIDGE_REPORTED_BY = 'MES bridge'   # the writer's reported_by (pinned by test)
    BRIDGE_COMPLETION_ENTRY = (
        'MES downtime resolved; repair completed and the line returned to production.')
    # Live workshop story (ADR-0085): the orchestrator sets this from
    # simulation_config.story_anchor_epoch each cycle. 0.0 = wall-clock loop;
    # None = story clock unknown this cycle (anchor read failed): skip.
    condition_anchor_epoch = 0.0
    # ...and this: a callable returning the STORED anchor (a fresh ERP read),
    # so the monitor can confirm the era under the story-clock lock (ADR-0089).
    # None on a story anchor = the era cannot be confirmed: skip (fail closed).
    condition_anchor_reader: Optional[Callable[[], Optional[float]]] = None
    # ...and this: a callable reading the ERP's MES downtime events by id
    # (read_downtimes on its own connection), so the bridge orders of ended
    # loops can be completed in the downtime they were raised from. None
    # (a direct caller) = leave the bridge orders open.
    condition_downtime_reader: Optional[Callable[[list], Dict[str, tuple]]] = None

    def run_condition_monitor(self, cycle_id: str) -> int:
        """The cycle's condition monitor, run by the orchestrator BEFORE the
        heavy simulator steps (ADR-0090); the downtime step then reuses it."""
        self._condition_monitor_cycle = cycle_id
        self._condition_monitor_result = self.monitor_asset_condition(cycle_id)
        return self._condition_monitor_result

    def _condition_monitor_for_cycle(self, cycle_id: str) -> int:
        """This cycle's monitor result when the orchestrator already ran it;
        otherwise run it now (direct callers keep the old behaviour)."""
        if getattr(self, '_condition_monitor_cycle', None) == cycle_id:
            return self._condition_monitor_result
        return self.monitor_asset_condition(cycle_id)

    def monitor_asset_condition(self, cycle_id: str, now: Optional[float] = None) -> int:
        """Raise the story mill's condition-based work order when the outer-race
        band alarm is set, and complete the condition orders of loops that have
        ended. Returns rows inserted (0 when healthy, when the spindle is
        stopped, when this loop's order already exists, when the story clock
        is unknown, or when the seed DB is unavailable). Never raises: the MES
        cycle must not depend on the CMMS bridge.

        The order is stamped with the loop's alarm instant (ADR-0088 D5), not the
        cycle that noticed it, so it agrees to the second with the telemetry.
        From loop 1 on, every cycle (any phase) also runs
        :meth:`complete_condition_work_orders` and, with a downtime reader,
        :meth:`complete_bridge_work_orders` in the same transaction, so an
        account that piled up open orders is brought back on its next cycle.
        Loop 0 of a story clock has no earlier loop (the story history and every
        placement delete the condition orders and supersede the open orders
        reported before the anchor), so a fresh account's loop 0 makes no
        extra statement or seed round trip.
        """
        anchor = self.condition_anchor_epoch
        if anchor is None:
            return 0
        now = time.time() if now is None else now
        t = condition_monitor.story_seconds(now, anchor)
        alarm = condition_monitor.spectral_features(condition_monitor.loop_step(t))['bpfo_alarm']
        loop_no = condition_monitor.loop_number(t)
        close_out = loop_no >= 1
        if not (alarm or close_out):
            return 0
        seed_conn = self._get_seed_db_conn()
        if seed_conn is None:
            return 0
        try:
            completed, inserted = self._condition_orders_in_transaction(
                seed_conn, anchor, now, loop_no, alarm, close_out)
        except Exception as cm_err:
            self._condition_monitor_failed(seed_conn, loop_no, cycle_id, cm_err)
            return 0
        if completed:
            logger.info(f"Condition monitor completed {completed} work order(s) of earlier "
                        f"loops (cycle {cycle_id})")
        if inserted:
            logger.info(f"Condition monitor raised WO {self._loop_wonum(loop_no)}: BPFO "
                        f"{condition_monitor.spectral_features(condition_monitor.BPFO_ALARM_STEP)['bpfo']} "
                        f"mm/s (cycle {cycle_id})")
        return inserted

    def _condition_orders_in_transaction(self, seed_conn, anchor: float, now: float, loop_no: int,
                                         alarm: bool, close_out: bool) -> tuple:
        """``(completed, inserted)`` for one cycle in ONE seed transaction,
        under :meth:`_condition_era_current`: the close-out of earlier loops'
        condition orders and, with a downtime reader, bridge orders
        (``close_out``), then this loop's order (``alarm``). A moved story
        clock rolls back and returns ``(0, 0)``; errors propagate."""
        seed_cursor = seed_conn.cursor()
        try:
            if not self._condition_era_current(seed_cursor, anchor):
                seed_conn.rollback()
                return 0, 0
            completed = 0
            if close_out:
                loop_start = condition_monitor.loop_start_epoch(now, anchor)
                completed = self.complete_condition_work_orders(seed_cursor, loop_start, now)
                reader = self.condition_downtime_reader
                if reader is not None:
                    completed += self.complete_bridge_work_orders(seed_cursor, loop_start, now, reader)
            inserted = self.insert_condition_work_order(
                seed_cursor, loop_no, condition_monitor.alarm_epoch(now, anchor),
                condition_monitor.fail_epoch(now, anchor)) if alarm else 0
            seed_conn.commit()
            return completed, inserted
        finally:
            seed_cursor.close()

    def _condition_era_current(self, seed_cursor, anchor: float) -> bool:
        """On the story clock: take the SHARED story-clock lock on the seed
        transaction (the re-plan's seed half holds it exclusive), then re-read
        the stored anchor; False (one log line) when a placement moved or
        cleared it since the cycle loaded ``anchor``. The wall-clock loop
        (anchor 0.0: demo / frozen) has no story clock and takes no lock."""
        if not anchor:
            return True
        from . import story_replan  # lazy: story_replan -> story_history -> this module
        seed_cursor.execute(story_replan.SHARED_LOCK_SQL, (story_replan.LOCK_KEY,))
        reader = self.condition_anchor_reader
        current = reader() if reader is not None else None
        if current is not None and float(current) == float(anchor):
            return True
        logger.info(f"Condition monitor: the story clock moved since this cycle loaded anchor {anchor!r} "
                    f"(stored {current!r}); skipping the cycle")
        return False

    def _condition_monitor_failed(self, seed_conn, loop_no: int, cycle_id: str, err: Exception) -> None:
        """Roll back and warn once per loop (not per cycle) about a failed order."""
        try:
            seed_conn.rollback()
        except Exception:  # nosec B110 - best-effort rollback on a failing connection
            pass
        if getattr(self, '_condition_monitor_failed_loop', None) != loop_no:
            self._condition_monitor_failed_loop = loop_no
            logger.warning(f"Condition monitor work order FAILED (cycle {cycle_id}): {err}")

    @classmethod
    def insert_condition_work_order(cls, seed_cursor, loop_no: int, alarm_epoch: float,
                                    fail_epoch: float) -> int:
        """Insert loop ``loop_no``'s condition order into the site's CMMS; rows inserted.

        wonum ``VIB-CMX-<loop_no % 1e6, 6 digits>`` with ``ON CONFLICT (wonum)
        DO NOTHING`` (0 when it exists); reported_date = target_start = the
        alarm instant and sched_start = the bearing failure, both naive UTC;
        the description carries the features at BPFO_ALARM_STEP. Executes one
        statement on ``seed_cursor``: no commit, errors propagate (the caller
        owns the transaction). Also the story re-plan's catch-up path.
        """
        schema, _prefix = cls.CMMS_SITE_TARGET[cls.CONDITION_MONITOR_SITE]
        reported = _naive_utc(alarm_epoch)
        description = cls._condition_work_order_description(
            condition_monitor.spectral_features(condition_monitor.BPFO_ALARM_STEP))
        # Only the schema (a class constant) is in the text; every value is bound.
        seed_cursor.execute(sql_text("""
            INSERT INTO {schema}.work_orders
                (wonum, asset_num, work_type, description, status, priority,
                 reported_by, reported_date, target_start, sched_start, failure_code, crew)
            VALUES (%s, %s, 'CM', %s, 'WAPPR', 1, 'Condition monitor', %s, %s, %s, 'BEARING', %s)
            ON CONFLICT (wonum) DO NOTHING
        """, schema=schema), (cls._loop_wonum(loop_no), condition_monitor.STORY_CMMS_ASSET_NUM, description,
                              reported, reported, _naive_utc(fail_epoch), cls.CONDITION_MONITOR_CREW))
        return seed_cursor.rowcount if seed_cursor.rowcount and seed_cursor.rowcount > 0 else 0

    @classmethod
    def complete_condition_work_orders(cls, seed_cursor, loop_start_epoch: float, now: float) -> int:
        """Complete the story asset's open condition orders reported before
        ``loop_start_epoch`` (the current loop's start: every such order
        belongs to a loop that has ended); rows completed.

        Each becomes ``CONDITION_COMPLETED_STATUS``, worked in the bearing
        replacement of its own loop: ``actual_start`` = its ``sched_start``
        (the bearing failure the writer scheduled it for), ``actual_finish`` =
        the end of the repair stop (never later than ``now``), plus one
        ``COMPLETE`` log entry dated ``actual_finish``. Every value comes from
        the order itself, so the result does not depend on which cycle runs
        it; a completed order no longer matches, so a rerun changes nothing.
        The current loop's order (reported at its alarm, after the loop
        start) is never touched. One statement on ``seed_cursor``: no commit,
        errors propagate (the caller owns the transaction).
        """
        schema, _prefix = cls.CMMS_SITE_TARGET[cls.CONDITION_MONITOR_SITE]
        # Only the schema (a class constant) is in the text; every value is bound.
        seed_cursor.execute(sql_text("""
            WITH completed AS (
                UPDATE {schema}.work_orders AS wo
                SET status = %(status)s,
                    actual_start = COALESCE(wo.sched_start, wo.reported_date),
                    actual_finish = LEAST(COALESCE(wo.sched_start, wo.reported_date)
                                          + %(repair_seconds)s * INTERVAL '1 second', %(now)s)
                WHERE wo.asset_num = %(asset)s AND wo.wonum LIKE %(prefix)s
                  AND wo.status = ANY(%(open_statuses)s) AND wo.reported_date < %(loop_start)s
                RETURNING wo.wonum, wo.actual_finish
            )
            INSERT INTO {schema}.work_order_log (wonum, log_date, author, log_type, entry)
            SELECT wonum, actual_finish, %(author)s, 'COMPLETE', %(entry)s FROM completed
        """, schema=schema), {
            'status': cls.CONDITION_COMPLETED_STATUS,
            'repair_seconds': condition_monitor.BEARING_REPAIR_STEPS * condition_monitor.INTERVAL_SEC,
            'now': _naive_utc(now), 'asset': condition_monitor.STORY_CMMS_ASSET_NUM,
            'prefix': cls._loop_wonum(0).rstrip('0') + '%',
            'open_statuses': list(cls.CONDITION_OPEN_STATUSES),
            'loop_start': _naive_utc(loop_start_epoch),
            'author': cls.CONDITION_COMPLETION_AUTHOR, 'entry': cls.CONDITION_COMPLETION_ENTRY,
        })
        return seed_cursor.rowcount if seed_cursor.rowcount and seed_cursor.rowcount > 0 else 0

    @classmethod
    def complete_bridge_work_orders(cls, seed_cursor, loop_start_epoch: float, now: float,
                                    read_downtimes: Callable[[list], Dict[str, tuple]]) -> int:
        """Complete the open MES-bridge orders (``IND-*``, any asset of the
        site) reported before ``loop_start_epoch``, i.e. of loops that have
        ended; rows completed.

        Only a PROVEN bridge order is touched (:meth:`_proven_bridge_downtime_id`:
        reported by the bridge, and its description ends in the canonical id of
        the downtime that reproduces its number). Each is worked in that
        downtime (``read_downtimes``, the ERP): status
        ``CONDITION_COMPLETED_STATUS``, ``actual_start`` / ``actual_finish`` =
        the downtime's start and end, one ``COMPLETE`` log entry by whoever
        resolved it, dated at its end or, when the order was entered after
        that, at the order's own report. An order whose downtime is still open
        waits; a proven order whose downtime record no longer exists (a
        placement retired its run, a db-init re-run wiped the ERP) is finished
        at the moment it was entered, by the planner, and keeps the stop start
        it records (none when it records none: its report time is never given
        as one). Every other open ``IND-*``
        order of an ended loop stays as it is and is reported once per loop
        (a warning). The completion re-checks the reporter and the exact
        proven text, and a completed order no longer matches, so an edit in
        between or a rerun changes nothing. At most ``MAX_ROWS_PER_CYCLE``
        candidates per call (:meth:`_bridge_candidates`), so the downtime
        lookup and the completion take at most that many too; the rest wait
        for later cycles. Up to three statements on ``seed_cursor`` and no
        commit (the caller owns the transaction); errors propagate.
        """
        schema, _prefix = cls.CMMS_SITE_TARGET[cls.CONDITION_MONITOR_SITE]
        scope = {'prefix': cls.CMMS_BRIDGE_WONUM_PREFIX + '%',
                 'open_statuses': list(cls.CONDITION_OPEN_STATUSES),
                 'loop_start': _naive_utc(loop_start_epoch)}
        rows = cls._bridge_candidates(seed_cursor, schema, scope, cls._bridge_scan_start())
        plan, unproven = cls._bridge_completions(rows, read_downtimes, now)
        _warn_unproven_bridge_orders(loop_start_epoch, unproven)
        if not plan:
            return 0
        wonums, starts, finishes, authors, descriptions = (list(column) for column in zip(*plan))
        seed_cursor.execute(sql_text(_COMPLETE_BRIDGE_SQL, schema=schema), {  # every value is bound
            **scope, 'status': cls.CONDITION_COMPLETED_STATUS, 'wonums': wonums, 'starts': starts,
            'finishes': finishes, 'authors': authors, 'descriptions': descriptions,
            'reported_by': cls.CMMS_BRIDGE_REPORTED_BY, 'default_author': cls.CONDITION_COMPLETION_AUTHOR,
            'entry': cls.BRIDGE_COMPLETION_ENTRY,
        })
        return seed_cursor.rowcount if seed_cursor.rowcount and seed_cursor.rowcount > 0 else 0

    @classmethod
    def _bridge_scan_start(cls) -> str:
        """A fresh random bridge work-order number (the prefix and 8 random
        upper-case hex chars, the shape of :meth:`_bridge_wonum`) where this
        cycle's candidate read starts (:meth:`_bridge_candidates`)."""
        return cls._bridge_wonum(uuid.uuid4())

    @staticmethod
    def _bridge_candidates(seed_cursor, schema: str, scope: Dict[str, Any], start: str) -> list:
        """At most ``MAX_ROWS_PER_CYCLE`` candidate rows ``(wonum, description,
        reported_date, reported_by)``: from ``start`` in wonum order, wrapping
        round to the lowest (``_BRIDGE_CANDIDATES_FROM_SQL``). Returned in wonum
        order, so a set below the bound comes back whole and in the order one
        unbounded read gave."""
        params = {**scope, 'start': start, 'limit': MAX_ROWS_PER_CYCLE}
        seed_cursor.execute(sql_text(_BRIDGE_CANDIDATES_FROM_SQL, schema=schema), params)
        rows = list(seed_cursor.fetchall())
        if len(rows) >= MAX_ROWS_PER_CYCLE:
            return rows
        seed_cursor.execute(sql_text(_BRIDGE_CANDIDATES_BEFORE_SQL, schema=schema),
                            {**params, 'limit': MAX_ROWS_PER_CYCLE - len(rows)})
        return list(seed_cursor.fetchall()) + rows

    @classmethod
    def _bridge_completions(cls, rows, read_downtimes, now: float) -> tuple:
        """``(plan, unproven)`` for the candidates ``(wonum, description,
        reported_date, reported_by, actual_start)``: ``plan`` = ``(wonum,
        actual_start, actual_finish, author, description)`` of each proven
        order that can be completed now (see :meth:`complete_bridge_work_orders`),
        ``unproven`` = the wonums whose provenance is not proven. One downtime
        read for all."""
        ids, unproven = {}, []
        for wonum, description, _reported, reported_by, _start in rows:
            downtime_id = cls._proven_bridge_downtime_id(wonum, description, reported_by)
            if downtime_id is None:
                unproven.append(wonum)
            else:
                ids[wonum] = downtime_id
        downtimes = read_downtimes(sorted(set(ids.values()))) if ids else {}
        cutoff = _naive_utc(now)
        plan = []
        for wonum, description, reported, _reported_by, recorded_start in rows:
            if wonum not in ids:
                continue
            record = downtimes.get(ids[wonum])
            if record is None:
                plan.append((wonum, recorded_start, reported, None, description))
            elif record[1] is not None and record[1] <= cutoff:
                plan.append((wonum, record[0], record[1], record[2], description))
        return plan, unproven

    @classmethod
    def _proven_bridge_downtime_id(cls, wonum: str, description: Optional[str],
                                   reported_by: Optional[str]) -> Optional[str]:
        """The MES downtime id ``wonum`` was raised from, when the row proves it
        is the bridge's own order: reported by ``CMMS_BRIDGE_REPORTED_BY``, its
        description in the writer's form ending in a canonical downtime id, and
        that id reproducing ``wonum``. ``None`` otherwise."""
        if reported_by != cls.CMMS_BRIDGE_REPORTED_BY:
            return None
        match = _BRIDGE_DESCRIPTION.fullmatch(description or '')
        if match is None or cls._bridge_wonum(match.group(1)) != wonum:
            return None
        return match.group(1)

    @staticmethod
    def read_downtimes(erp_conn, downtime_ids) -> Dict[str, tuple]:
        """``{downtime id: (start_time, end_time, resolved_by)}`` of the ERP's
        MES downtime events ``downtime_ids`` (lower-case UUID text); an id the
        ERP no longer holds is absent. One statement, no commit."""
        if not downtime_ids:
            return {}
        cursor = erp_conn.cursor()
        try:
            cursor.execute(_READ_DOWNTIMES_SQL, (list(downtime_ids),))
            return {str(row[0]).lower(): (row[1], row[2], row[3]) for row in cursor.fetchall()}
        finally:
            cursor.close()

    @classmethod
    def _bridge_wonum(cls, downtime_id) -> str:
        """The bridge's wonum for ``downtime_id`` (VARCHAR(15)): the prefix and
        the first 8 hex chars of the (unique) downtime id, upper case."""
        return f"{cls.CMMS_BRIDGE_WONUM_PREFIX}{str(downtime_id).replace('-', '')[:8].upper()}"

    @staticmethod
    def _condition_work_order_description(features: Dict[str, Any]) -> str:
        """What the technician reads: a spindle VIBRATION alarm (the word a
        CMMS query for a vibration work order matches), the offending band,
        its frequency, the threshold it crossed and the supporting features
        (<= 250 chars)."""
        return (
            f"Condition monitor: spindle vibration alarm. BPFO band {features['bpfo']:.2f} mm/s at "
            f"{condition_monitor.BPFO_HZ:.0f} Hz exceeded {condition_monitor.BPFO_ALARM_MM_S:.2f} mm/s on "
            f"{condition_monitor.STORY_SITEWISE_ASSET} (spindle {condition_monitor.SPINDLE_RPM:.0f} rpm); "
            f"1x {features['one_x']:.2f} mm/s, crest factor {features['crest']:.1f}, "
            f"kurtosis {features['kurtosis']:.1f}. Outer-race bearing defect developing; "
            f"replace spindle bearing."
        )[:250]

    @staticmethod
    def _loop_wonum(loop_no: int) -> str:
        """wonum VARCHAR(15): one order per 8 h loop, idempotent across cycles."""
        return f"VIB-CMX-{loop_no % 1_000_000:06d}"

    @classmethod
    def _condition_wonum(cls, t: float) -> str:
        """The wonum of the loop containing story time ``t``."""
        return cls._loop_wonum(condition_monitor.loop_number(t))

    @staticmethod
    def _condition_repair_time(now: float, anchor: float = 0.0) -> datetime:
        """Scheduled start = the bearing replacement the OPC UA loop plays at
        BEARING_FAIL_STEP of the loop containing ``now`` (loops start at
        ``anchor`` when it is positive, else they are epoch-aligned), naive UTC."""
        return _naive_utc(condition_monitor.fail_epoch(now, anchor))

    def _insert_cmms_work_order(
        self, seed_cursor, site_prefix: str, site_code: str, line_id: str,
        downtime_category: str, downtime_reason: str, downtime_id,
        downtime_start=None,
    ) -> None:
        """Pick the site asset and insert the CMMS work_orders_raw row, dated by
        its stop: ``created_ts`` is ``downtime_start`` (NULL when none is handed
        over, and the view then dates the order by its zero offset, "now", as
        before). :meth:`close_corporate_bridge_orders` closes it with the stop."""
        asset_row = self._pick_cmms_asset(seed_cursor, site_prefix, downtime_reason)
        if not asset_row:
            logger.warning(
                f"MES→CMMS bridge: no CMMS asset with prefix {site_prefix}- "
                f"for downtime {downtime_id} — work order not created"
            )
            return
        priority = 'high' if downtime_category == 'MECHANICAL' else 'medium'
        # wo_number is VARCHAR(30) — key on the (unique) downtime event
        wo_num = f"WO-MES-{site_prefix}-{str(downtime_id)[:8].upper()}"
        # description carries the MES downtime event id so agents can trace
        # the CMMS work order back to the MES event (free-text field; the
        # cmms schema has no MES reference column).
        description = (
            f"MES-triggered: {downtime_reason} on {line_id} "
            f"(site {site_code}) — MES downtime event {downtime_id}"
        )
        seed_cursor.execute("""
            INSERT INTO cmms_maintenance.work_orders_raw
                (wo_number, asset_id, wo_type, priority, description,
                 status_hint, failure_code, created_offset, created_ts)
            VALUES (%s, %s, 'corrective', %s, %s, 'open', %s, INTERVAL '0 minutes', %s)
            ON CONFLICT (wo_number) DO NOTHING
        """, (wo_num, asset_row[0], priority, description, downtime_reason, _aware_utc(downtime_start)))
        logger.info(
            f"Created CMMS WO {wo_num} on asset {asset_row[1]} "
            f"for MES downtime {downtime_id} ({line_id})"
        )

    # The corporate CMMS's statuses (004_cmms_maintenance.sql): the bridge's
    # orders are open until their stop ends, then completed; an order whose
    # stop the ERP no longer holds is cancelled.
    CORP_COMPLETED_STATUS = 'completed'
    CORP_CANCELLED_STATUS = 'cancelled'

    def close_corporate_bridge_orders(self, conn, cycle_id: str) -> Dict[str, Any]:
        """Cycle step: close the corporate CMMS's open MES-bridge orders whose
        stop has ended or no longer exists (:meth:`close_out_corporate_bridge_orders`),
        reading the downtimes on the step's ERP connection ``conn`` and
        committing the seed database. Without a seed database it does nothing;
        a failure is logged loudly and rolled back, never raised (the MES cycle
        must not depend on the CMMS bridge). Returns ``completed`` and
        ``cancelled`` counts."""
        seed_conn = self._get_seed_db_conn()
        if seed_conn is None:
            return {'completed': 0, 'cancelled': 0}
        try:
            seed_cursor = seed_conn.cursor()
            try:
                completed, cancelled = self.close_out_corporate_bridge_orders(
                    seed_cursor, lambda ids: self.read_downtimes(conn, ids), time.time())
            finally:
                seed_cursor.close()
            seed_conn.commit()
        except Exception as err:
            try:
                seed_conn.rollback()
            except Exception:  # nosec B110 - best-effort rollback on a failing connection
                pass
            logger.warning(f"Corporate CMMS bridge close-out FAILED (cycle {cycle_id}): {err}")
            return {'completed': 0, 'cancelled': 0}
        if completed or cancelled:
            logger.info(f"Corporate CMMS bridge close-out: {completed} completed, "
                        f"{cancelled} cancelled (cycle {cycle_id})")
        return {'completed': completed, 'cancelled': cancelled, 'cycle_id': cycle_id}

    @classmethod
    def close_out_corporate_bridge_orders(cls, seed_cursor,
                                          read_downtimes: Callable[[list], Dict[str, tuple]],
                                          now: float) -> tuple:
        """``(completed, cancelled)``: one bounded batch of the corporate CMMS's
        open ``WO-MES-*`` orders, each closed from the MES downtime it was raised
        from.

        Only a PROVEN bridge order is touched
        (:meth:`_proven_corp_bridge_downtime_id`). Its downtime is read from the
        ERP (``read_downtimes``, one call for the batch): a stop that has ended
        (``end_time`` at or before ``now``) completes it, ``created_ts`` /
        ``completed_ts`` = the stop's start / end, ``assigned_to`` = whoever
        resolved it (when the order names nobody), ``actual_hours`` = its
        length; a stop still running leaves it open; a stop the ERP no longer
        holds (a Reset All or a db-init re-run wiped it) cancels it, keeping
        only a creation date it already had. Any other open ``WO-MES-*`` order
        stays as it is and is reported once per process. At most
        ``MAX_ROWS_PER_CYCLE`` candidates (from :meth:`_corp_bridge_scan_start`,
        wrapping round), so the downtime lookup and the update take at most
        that many too; the rest wait for later cycles. No commit (the caller
        owns the transaction); errors propagate.
        """
        rows = cls._corp_bridge_candidates(seed_cursor, cls._corp_bridge_scan_start())
        ids, unproven = {}, []
        for wo_number, description in rows:
            downtime_id = cls._proven_corp_bridge_downtime_id(wo_number, description)
            if downtime_id is None:
                unproven.append(wo_number)
            else:
                ids[wo_number] = downtime_id
        if unproven and not _unproven_corp_bridge_warned["done"]:
            _unproven_corp_bridge_warned["done"] = True
            logger.warning(f"Corporate CMMS: {len(unproven)} open WO-MES order(s) the MES bridge did not "
                           f"provably raise are left open, e.g. {', '.join(unproven[:5])}")
        if not ids:
            return 0, 0
        downtimes = read_downtimes(sorted(set(ids.values())))
        plan = cls._corp_bridge_plan(rows, ids, downtimes, now)
        if not plan:
            return 0, 0
        columns = list(zip(*plan))
        seed_cursor.execute(_CORP_CLOSE_BRIDGE_SQL, {
            'wo_numbers': list(columns[0]), 'descriptions': list(columns[1]), 'statuses': list(columns[2]),
            'starts': list(columns[3]), 'finishes': list(columns[4]), 'assignees': list(columns[5]),
            'hours': list(columns[6]), 'cancelled': cls.CORP_CANCELLED_STATUS,
        })
        statuses = [row[0] for row in seed_cursor.fetchall()]
        return statuses.count(cls.CORP_COMPLETED_STATUS), statuses.count(cls.CORP_CANCELLED_STATUS)

    @classmethod
    def _corp_bridge_plan(cls, rows, ids: Dict[str, str], downtimes: Dict[str, tuple], now: float) -> list:
        """``(wo_number, description, status, created_ts, completed_ts,
        assigned_to, actual_hours)`` for each proven order that closes now, in
        wo_number order (see :meth:`close_out_corporate_bridge_orders`)."""
        cutoff = _aware_utc(_naive_utc(now))
        plan = []
        for wo_number, description in rows:
            if wo_number not in ids:
                continue
            record = downtimes.get(ids[wo_number])
            if record is None:
                plan.append((wo_number, description, cls.CORP_CANCELLED_STATUS, None, None, None, None))
                continue
            start, end, resolved_by = _aware_utc(record[0]), _aware_utc(record[1]), record[2]
            if end is None or end > cutoff:
                continue
            hours = round(min(max((end - start).total_seconds(), 0.0) / 3600.0, _CORP_MAX_ACTUAL_HOURS), 2)
            plan.append((wo_number, description, cls.CORP_COMPLETED_STATUS, start, end,
                         resolved_by, hours))
        return plan

    @classmethod
    def _proven_corp_bridge_downtime_id(cls, wo_number: str, description: Optional[str]) -> Optional[str]:
        """The MES downtime id ``wo_number`` was raised from, when the row proves
        it is the corporate bridge's own order: its number in the writer's form
        (a site prefix of ``CMMS_SITE_PREFIX``, the id's first 8 hex chars,
        upper case), its description in the writer's form ending in that id in
        canonical form. ``None`` otherwise."""
        number = _CORP_BRIDGE_WO_NUMBER.fullmatch(wo_number or '')
        match = _BRIDGE_DESCRIPTION.fullmatch(description or '')
        if number is None or match is None or number.group(1) not in cls.CMMS_SITE_PREFIX.values():
            return None
        downtime_id = match.group(1)
        if downtime_id[:8].upper() != number.group(2):
            return None
        return downtime_id

    @staticmethod
    def _corp_bridge_candidates(seed_cursor, start: str) -> list:
        """At most ``MAX_ROWS_PER_CYCLE`` open corporate bridge orders
        ``(wo_number, description)`` from ``start`` in wo_number order, wrapping
        round to the lowest; returned in wo_number order."""
        params = {'start': start, 'limit': MAX_ROWS_PER_CYCLE}
        seed_cursor.execute(_CORP_BRIDGE_CANDIDATES_FROM_SQL, params)  # start and limit are bound
        rows = [tuple(row) for row in seed_cursor.fetchall()]
        if len(rows) >= MAX_ROWS_PER_CYCLE:
            return rows
        seed_cursor.execute(_CORP_BRIDGE_CANDIDATES_BEFORE_SQL,
                            {**params, 'limit': MAX_ROWS_PER_CYCLE - len(rows)})
        return [tuple(row) for row in seed_cursor.fetchall()] + rows

    @classmethod
    def _corp_bridge_scan_start(cls) -> str:
        """A fresh random corporate bridge number (a site prefix of
        ``CMMS_SITE_PREFIX`` and 8 random upper-case hex chars, the shape of
        the writer's numbers) where this cycle's candidate read starts."""
        prefix = random.choice(sorted(set(cls.CMMS_SITE_PREFIX.values())))  # nosec B311 - a scan position, not security
        return f"WO-MES-{prefix}-{uuid.uuid4().hex[:8].upper()}"

    def _pick_cmms_asset(self, seed_cursor, site_prefix: str, downtime_reason: str):
        """Pick the CMMS asset for a downtime event — deterministic-ish.

        Matches the site prefix and prefers an asset whose type matches the
        downtime reason (CMMS_REASON_ASSET_TYPE) when derivable; otherwise
        falls back to the first site asset by tag order.
        """
        preferred_type = self.CMMS_REASON_ASSET_TYPE.get(downtime_reason, '')
        seed_cursor.execute("""
            SELECT asset_id, asset_tag FROM cmms_maintenance.assets
            WHERE asset_tag LIKE %s
            ORDER BY (asset_type = %s) DESC, asset_tag
            LIMIT 1
        """, (f'{site_prefix}-%', preferred_type))
        return seed_cursor.fetchone()

    def _get_seed_db_conn(self):
        """Return a (cached) connection to the SEED database, or None.

        When the seed-DB environment is absent or unreachable, log LOUDLY once
        and return None so callers skip the bridge — never a silent swallow.
        """
        if self._seed_db_conn is not None and not getattr(self._seed_db_conn, 'closed', 0):
            return self._seed_db_conn
        try:
            params = self._seed_db_params()
        except Exception as e:
            self._warn_seed_db_once(f"seed-DB credentials unavailable: {e}")
            return None
        if params is None:
            self._warn_seed_db_once(
                "SEED_DB_SECRET_NAME / SEED_DB_HOST not set — MES downtime events "
                "will NOT create CMMS work orders (cross-system bridge disabled)"
            )
            return None
        try:
            from erp_simulation import pgdriver
            self._seed_db_conn = pgdriver.connect(**params)
            return self._seed_db_conn
        except Exception as e:
            self._warn_seed_db_once(f"seed-DB connection failed: {e}")
            return None

    def _seed_db_params(self) -> Optional[Dict[str, Any]]:
        """Resolve seed-DB (cmms_maintenance) connection params from the env.

        Mirrors the simulator's own DB config pattern: SEED_DB_SECRET_NAME
        (AWS Secrets Manager, same secret shape as DB_SECRET_NAME) wins, then
        SEED_DB_HOST + SEED_DB_PORT/SEED_DB_NAME/SEED_DB_USER/SEED_DB_PASSWORD.
        Returns None when neither is configured.
        """
        secret_name = os.getenv('SEED_DB_SECRET_NAME')
        if secret_name:
            cfg = DatabaseConfig.from_secrets_manager(
                secret_name, os.getenv('AWS_REGION', 'us-east-1')
            )
            return {'host': cfg.host, 'port': cfg.port, 'database': cfg.database,
                    'user': cfg.user, 'password': cfg.password}
        host = os.getenv('SEED_DB_HOST')
        if host:
            return {'host': host,
                    'port': int(os.getenv('SEED_DB_PORT', '5432')),
                    'database': os.getenv('SEED_DB_NAME', 'adt_demo'),
                    'user': os.getenv('SEED_DB_USER', 'postgres'),
                    'password': os.getenv('SEED_DB_PASSWORD', '')}
        return None

    def _warn_seed_db_once(self, message: str) -> None:
        """Warn loudly ONCE about a disabled/failing MES→CMMS bridge."""
        if not self._seed_db_warned:
            logger.warning(f"MES→CMMS bridge disabled: {message}")
            self._seed_db_warned = True
    
    def _create_lot_mes_tracking(
        self,
        cursor,
        work_order_id,
        work_order_number: str,
        production_run_id,
        site_code: str,
        line: str,
    ) -> int:
        """
        Create lot_mes_tracking rows for a newly created MES work order.

        Finds all lot_production_usage rows for the given production_run_id and
        inserts a corresponding lot_mes_tracking row for each, using the
        work_order_number as the batch_id.

        Returns the number of tracking rows inserted.
        Returns 0 silently if no lot_production_usage rows exist for the run.

        Args:
            cursor: Database cursor (same transaction as the work order INSERT).
            work_order_id: UUID of the newly created mes_work_orders row.
            work_order_number: Human-readable work order number (used as batch_id).
            production_run_id: UUID of the ERP production_runs row.
            site_code: Site code (e.g. 'MILWAUKEE_BOTTLING').
            line: Production line (e.g. 'LINE1').

        Returns:
            Count of lot_mes_tracking rows inserted.
        """
        try:
            # Find all lot_production_usage rows for this production run
            cursor.execute("""
                SELECT lpu.lot_id, lpu.quantity_used, lpu.unit_of_measure
                FROM lot_production_usage lpu
                WHERE lpu.production_run_id = %s
            """, (production_run_id,))

            usage_rows = cursor.fetchall()

            if not usage_rows:
                return 0

            rows_inserted = 0
            for lot_id, quantity_used, unit_of_measure in usage_rows:
                cursor.execute("""
                    INSERT INTO lot_mes_tracking (
                        lot_id, work_order_id, batch_id, site, line,
                        quantity_consumed, unit_of_measure
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    lot_id,
                    work_order_id,
                    work_order_number,
                    site_code,
                    line,
                    quantity_used,
                    unit_of_measure,
                ))
                rows_inserted += cursor.rowcount

            return rows_inserted

        except Exception as e:
            logger.error(
                f"Failed to create lot_mes_tracking for work order {work_order_number}: {e}",
                exc_info=True,
            )
            return 0

    def _consume_materials_for_work_order(self, cursor, work_order_id: int, planned_quantity: int) -> None:
        """
        Consume materials when MES starts a work order.

        Each planned material is drawn from the run's site raw-material
        warehouse first, then the item's other raw locations
        (``inventory_draw.draw``), so no location goes negative while another
        still holds the stock.

        Args:
            cursor: Database cursor.
            work_order_id: MES work order ID.
            planned_quantity: Planned production quantity.
        """
        try:
            cursor.execute("""
                SELECT pr.run_id, pr.site
                FROM mes_work_orders mwo
                JOIN production_runs pr ON mwo.production_run_id = pr.run_id
                WHERE mwo.work_order_id = %s
            """, (work_order_id,))
            run_info = cursor.fetchone()
            if not run_info:
                logger.warning(f"No production run found for work order {work_order_id}")
                return
            run_id, site_code = run_info
            cursor.execute("""
                SELECT pm.material_id, pm.item_id, pm.planned_quantity, ii.item_code
                FROM production_materials pm
                JOIN inventory_items ii ON pm.item_id = ii.item_id
                WHERE pm.run_id = %s
            """, (run_id,))
            for material in cursor.fetchall():
                self._consume_material(cursor, run_id, site_code, *material)
        except Exception as e:
            logger.error(f"Failed to consume materials for work order {work_order_id}: {e}", exc_info=True)

    def _consume_material(self, cursor, run_id, site_code, material_id, item_id,
                          planned_quantity_material, item_code) -> None:
        """Draw one planned material (configured variance) and record its actual use."""
        variance_min = self._load_config_value(cursor, 'mes_material_variance_min', 1.0, 'float')
        variance_max = self._load_config_value(cursor, 'mes_material_variance_max', 1.05, 'float')
        actual_quantity = float(planned_quantity_material) * random.uniform(variance_min, variance_max)  # nosec B311 - synthetic data generation, not security context
        inventory_draw.draw(
            cursor, item_id, int(actual_quantity), RAW_LOCATION_CODES,
            reference_type='PRODUCTION_RUN', reference_id=run_id,
            preferred_code=SITE_RAW_LOCATION.get(site_code))
        cursor.execute("""
            UPDATE production_materials
            SET actual_quantity = %s
            WHERE material_id = %s
        """, (actual_quantity, material_id))
        logger.debug(f"Consumed material {item_code}: {actual_quantity:.2f} units for run {run_id}")

