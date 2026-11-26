# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Simulation Orchestrator for ERP Simulation System.

Coordinates the execution of all simulation modules in a scheduled cycle.
Manages database transactions, error handling, and CloudWatch metrics.
"""

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, Tuple
from contextlib import ExitStack, contextmanager

from erp_simulation import pgdriver

from . import story_live
from . import story_anchor_mirror
from .config import get_config
from .database import get_database
from .hot_path_indexes import ensure_hot_path_indexes
from .logging_config import get_logger
from .sites import SITE_EVENT_PREFIX, SITE_EVENT_STATES  # noqa: F401 - re-exported for compatibility
from .sites import optional_event_column
from .utils import MAX_ROWS_PER_CYCLE, ensure_number_sequences

logger = get_logger(__name__)

# Live-story clock states for one cycle (ADR-0085; see _load_story_clock).
STORY_CLOCK_ABSENT = "absent"
STORY_CLOCK_PRESENT = "present"
STORY_CLOCK_UNKNOWN = "unknown"

# Cycle bounding (ADR-0090). The Lambda runs every minute with a 300 s
# timeout: past this budget the remaining simulator steps are skipped (the
# clock steps already ran first); they pick up next cycle.
CYCLE_TIME_BUDGET_SECONDS = 150.0
# Above this many 'ready to ship' orders the cycle logs the backlog and how
# the bounded shipping step (MAX_ROWS_PER_CYCLE per batch) drains it.
BACKLOG_WARN_THRESHOLD = 2000
# Above the threshold the shipping step takes up to this many bounded batches
# per cycle (each its own transaction, each only if another fits in the
# budget): a 14k backlog drains in ~36 cycles instead of ~71.
BACKLOG_SHIPMENT_BATCHES = 2

# One simulator at a time. The schedule fires every minute and a cycle may run
# up to the budget above, so invocations overlap; overlapping simulator steps
# deadlocked on the same inventory rows (found live 2026-10-22: every cycle
# aborted its receipt step and timed out). The simulator steps therefore run
# only while this invocation holds a transaction-level advisory lock, taken
# WITHOUT waiting on its OWN connection (DatabaseManager.dedicated_connection,
# never pooled) whose transaction stays open for the steps: no reused session
# can re-enter it, and it ends with that connection. An invocation that finds
# it held runs only the clock steps (which carry their own locks). Its own
# key: the live-story clock uses ``story_replan.LOCK_KEY``.
SIMULATOR_LOCK_KEY = int.from_bytes(b"ERPCYCLE", "big", signed=True)
SIMULATOR_BUSY = "simulator:busy"
SIMULATOR_UNAVAILABLE = "simulator:unavailable"
SIMULATOR_LOCK_LOST = "simulator:lock-lost"
# The lock session is touched before every simulator step, and the server ends
# it once it has sat idle in its transaction this long: past the Lambda's
# 300 s timeout (no step, and no cycle, runs longer there), and the bound on a
# lock an invocation abandons (killed or frozen with its socket open).
SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS = 330
# While a cycle runs, a background thread touches the lock session this often,
# so even one step longer than the idle bound (a local run blocked on the
# database) keeps the lock. A frozen or killed invocation freezes or kills the
# thread with it, so the server's idle bound still drops an abandoned lock.
SIMULATOR_LOCK_HEARTBEAT_SECONDS = 60


class SimulatorLockUnavailable(RuntimeError):
    """The simulator lock could not be checked, or its session ended
    mid-cycle: the cycle fails (``ErrorCount``), it is never reported as a
    busy simulator."""


class _LockKeeper:
    """Owns one cycle's lock connection from claim to close.

    :meth:`beat` touches the session now (``SELECT 1``, which resets its
    idle-in-transaction clock) and returns the error if it has ended; a
    daemon thread does the same every ``interval`` seconds. One guard
    serialises the two, and a background failure is kept for the next
    :meth:`beat` to report. The THREAD tears the connection down when it
    stops (rollback, which releases the lock, then close), so no other thread
    ever touches the connection after :meth:`stop`: a beat stuck on a stalled
    network can never race the release."""

    STOP_WAIT_SECONDS = 5.0
    BEAT_SQL = "SELECT 1"

    def __init__(self, conn, interval: float):
        self._conn, self._interval = conn, interval
        self._guard = threading.Lock()
        self._stopped = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="erp-simulator-lock-heartbeat",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stopped.wait(self._interval):
                if self.beat() is not None:
                    return
        finally:
            self._teardown()

    def beat(self):
        """Touch the session; the error that ended it, or None."""
        with self._guard:
            if self._error is None and not self._stopped.is_set():
                try:
                    with self._conn.cursor() as cursor:
                        cursor.execute(self.BEAT_SQL)
                except Exception as e:
                    self._error = e
            return self._error

    def _teardown(self) -> None:
        """Release the lock (end its transaction) and close the connection.
        A session that already ended means the lock was lost: said, never
        raised. Runs on the keeper thread only."""
        with self._guard:
            try:
                if self._error is None:
                    self._conn.rollback()
            except Exception as e:
                logger.warning(f"The simulator lock session ended before the cycle finished "
                               f"({type(e).__name__}: {e}); another cycle may have started meanwhile")
            finally:
                try:
                    self._conn.close()
                except Exception as e:  # nosec B110 - the session ends either way
                    logger.warning(f"Closing the simulator lock connection failed: {e}")

    def stop(self) -> bool:
        """Stop the heartbeat and wait for the thread to release the lock.

        A beat still in flight after the wait (a stalled network) is cancelled
        with ``connection.cancel()``, which is safe from another thread and
        does not wait for the stalled statement. True once the thread has
        released and exited; False when it is still blocked, in which case it
        releases when the statement returns, and the server ends the session
        within ``SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS`` at the latest."""
        self._stopped.set()
        self._thread.join(timeout=self.STOP_WAIT_SECONDS)
        if self._thread.is_alive():
            try:
                self._conn.cancel()
            except Exception as e:  # nosec B110 - best effort; the server bound still applies
                logger.warning(f"Cancelling the simulator lock heartbeat failed: {e}")
            self._thread.join(timeout=self.STOP_WAIT_SECONDS)
        return not self._thread.is_alive()


def _monotonic() -> float:
    """Cycle clock (tests replace it to fake a slow step)."""
    return time.monotonic()


@dataclass(frozen=True)
class CycleStep:
    """One simulator step: ``getattr(orchestrator, owner).method(conn, cycle_id)``
    (``owner == 'self'``: ``method(conn)``). ``copies`` maps metric <- result
    key; ``transitions`` adds the result's ``transitions`` to state_transitions.
    A ``required`` step runs every cycle even past the time budget (one
    bounded batch); ``backlog_batches`` > 1 lets it take that many batches
    while the shipping backlog is above ``BACKLOG_WARN_THRESHOLD``. ``blocks``
    names later steps that do NOT run this cycle when this step fails (fail
    closed); a step without it fails alone, the later steps run as usual."""
    name: str
    owner: str
    method: str
    copies: Tuple[Tuple[str, str], ...] = ()
    transitions: bool = False
    required: bool = False
    backlog_batches: int = 1
    blocks: Tuple[str, ...] = ()


def _same(*keys: str) -> Tuple[Tuple[str, str], ...]:
    return tuple((key, key) for key in keys)


_SIMULATION_STEPS: Tuple[CycleStep, ...] = (
    # REQUIRED first (ADR-0090): the three bounded bulk steps run right after
    # the clock steps, before the heavy simulators, and are never skipped by
    # the budget. Each works on rows earlier cycles made ready.
    CycleStep('shipments', 'shipping_simulator', 'create_shipments', _same('shipments_created'),
              required=True, backlog_batches=BACKLOG_SHIPMENT_BATCHES),
    CycleStep('invoices', 'finance_simulator', 'generate_invoices',
              _same('invoices_generated') + (('finance_revenue', 'total_revenue'),), required=True),
    CycleStep('payments', 'finance_simulator', 'process_payments',
              _same('payments_recorded') + (('finance_collected', 'total_collected'),), required=True),
    CycleStep('orders', 'order_simulator', 'generate_orders', _same('orders_created')),
    CycleStep('procurement', 'procurement_simulator', 'check_and_create_pos', _same('pos_created')),
    CycleStep('po_receipt', 'procurement_simulator', 'receive_pos', _same('pos_received', 'pos_in_transit')),
    CycleStep('order_states', 'state_advancer', 'advance_order_states',
              _same('orders_pending_to_confirmed'), transitions=True),
    CycleStep('production_runs', 'production_simulator', 'create_production_runs',
              (('production_runs_created', 'runs_created'),)),
    CycleStep('production_release', 'state_advancer', 'release_production_runs',
              _same('production_planned_to_released'), transitions=True),
    # Live story (ADR-0085 amendment 2026-11-04): before any MES step, no
    # SCHEDULED order is due on the story line (off the live story a no-op).
    # Before mes_work_orders, so an older build's order takes a freed line
    # ahead of new work; optional like mes_events, so past the budget it is
    # never skipped while mes_events runs. Fails closed: if it fails, this
    # cycle neither assigns nor starts MES work orders.
    CycleStep('mes_reserved_lines', 'mes_simulator', 'clear_reserved_lines',
              blocks=('mes_work_orders', 'mes_events')),
    CycleStep('mes_work_orders', 'mes_simulator', 'create_mes_work_orders',
              (('mes_work_orders_created', 'work_orders_created'),)),
    CycleStep('mes_events', 'mes_simulator', 'simulate_production_events',
              (('mes_production_events_created', 'events_created'),)),
    CycleStep('mes_quality', 'mes_simulator', 'simulate_quality_checks',
              (('mes_quality_checks_created', 'quality_checks_created'),)),
    CycleStep('mes_consumption', 'mes_simulator', 'track_material_consumption',
              (('mes_material_consumption_records', 'consumption_records_created'),)),
    CycleStep('mes_downtime', 'mes_simulator', 'simulate_downtime_events',
              (('mes_downtime_events_created', 'downtime_events_created'),)),
    # Closes the corporate CMMS's MES-bridge orders whose stop has ended (or no
    # longer exists), right after the stops it reads were committed.
    CycleStep('cmms_close_out', 'mes_simulator', 'close_corporate_bridge_orders',
              (('cmms_bridge_orders_completed', 'completed'),
               ('cmms_bridge_orders_cancelled', 'cancelled'))),
    # After downtime/changeover so the derived machine_state is current.
    CycleStep('mes_line_status', 'mes_simulator', 'update_line_status',
              (('mes_line_status_updated', 'line_status_updated'),)),
    CycleStep('production_complete', 'production_simulator', 'complete_production',
              (('production_runs_completed', 'runs_completed'),)),
    CycleStep('production_states', 'state_advancer', 'advance_production_states',
              _same('orders_confirmed_to_ready', 'orders_confirmed_to_backordered',
                    'orders_backordered_to_ready'), transitions=True),
    CycleStep('shipment_states', 'state_advancer', 'advance_shipment_states',
              _same('shipping_preparing_to_picked', 'shipping_picked_to_packed', 'shipping_packed_to_shipped',
                    'shipping_shipped_to_in_transit', 'shipping_in_transit_to_delivered',
                    'shipping_orders_delivered'), transitions=True),
    CycleStep('invoice_states', 'state_advancer', 'advance_invoice_states',
              _same('finance_sent_to_overdue'), transitions=True),
    CycleStep('backorders', 'state_advancer', 'advance_backorder_states', transitions=True),
    # Clears a negative balance beside a positive one for the same item (inventory_draw.py).
    CycleStep('inventory_netting', 'state_advancer', 'net_split_inventory'),
    # Suppliers degrade on elevated lot rejection rates and recover after a cooldown.
    CycleStep('supplier_qualification', 'self', '_update_supplier_qualifications',
              (('supplier_status_changes', 'changes'),)),
)

_CYCLE_METRIC_KEYS = tuple(dict.fromkeys(
    ['state_transitions'] + [metric for step in _SIMULATION_STEPS for metric, _key in step.copies]))

_SUMMARY_COUNT_KEYS = (
    'ready_to_ship', 'orders_created', 'pos_created', 'pos_received', 'production_runs_created',
    'production_runs_completed', 'mes_work_orders_created', 'shipments_created',
    'invoices_generated', 'payments_recorded', 'state_transitions',
)


def _apply_step_result(step: CycleStep, result: Dict[str, Any], metrics: Dict[str, Any]) -> None:
    for metric, key in step.copies:  # summed: a backlog batch adds to the first
        metrics[metric] = metrics.get(metric, 0) + result.get(key, 0)
    if step.transitions:
        metrics['state_transitions'] += result.get('transitions', 0)


def _transaction_aborted(conn) -> bool:
    """True when the connection's transaction is in the failed state."""
    status = getattr(conn, 'get_transaction_status', None)
    return callable(status) and status() == pgdriver.TRANSACTION_STATUS_INERROR


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception as e:  # nosec B110 - the step already failed; the next step reports a dead connection
        logger.warning(f"Rollback failed: {type(e).__name__}: {e}")


# simulation_events columns present in the live table, read once per process
# (None = not read yet). An upgraded environment whose db-init did not re-run
# lacks newer columns (e.g. mes_ind_*); the INSERT skips them instead of
# failing every cycle.
_event_table_columns = None


def _reset_event_columns_cache(columns=None):
    """Forget (or, for tests, set) the cached simulation_events column set."""
    global _event_table_columns
    _event_table_columns = None if columns is None else frozenset(columns)


def _existing_event_columns(cursor, wanted):
    """Cached simulation_events columns; None when the read finds none (unknown)."""
    global _event_table_columns
    if _event_table_columns is None:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'simulation_events'
        """)
        found = frozenset(row[0] for row in cursor.fetchall())
        if not found:
            return None
        _event_table_columns = found
        # An optional plant's columns exist only where it is enabled: their
        # absence is expected and skipped quietly.
        missing = [column for column in wanted
                   if column not in found and not optional_event_column(column)]
        if missing:
            logger.warning("simulation_events lacks columns %s (db-init not re-run?); "
                           "recording cycles without them", ", ".join(missing))
    return _event_table_columns


class SimulationOrchestrator:
    """
    Main coordinator for the ERP simulation engine.
    
    Orchestrates the execution of all simulation modules in a scheduled cycle,
    manages database transactions, handles errors with retry logic, and publishes
    CloudWatch metrics.
    """
    
    def __init__(self):
        """Initialize the simulation orchestrator."""
        self.config = get_config()
        self.db = get_database()
        self.cycle_count = 0
        self.running = False
        self._story_anchor = None  # live-story anchor epoch (ADR-0085); used only while the clock is present
        self._story_clock = STORY_CLOCK_ABSENT
        
        # CloudWatch client (lazy initialization)
        self._cloudwatch_client = None
        
        # Initialize simulation modules
        from .order_simulator import OrderSimulator
        from .procurement_simulator import ProcurementSimulator
        from .production_simulator import ProductionSimulator
        from .mes_simulator import MESSimulator
        from .shipping_simulator import ShippingSimulator
        from .finance_simulator import FinanceSimulator
        from .state_advancer import StateAdvancer
        
        self.order_simulator = OrderSimulator()
        self.procurement_simulator = ProcurementSimulator()
        self.production_simulator = ProductionSimulator()
        self.mes_simulator = MESSimulator()
        self.shipping_simulator = ShippingSimulator()
        self.finance_simulator = FinanceSimulator()
        self.state_advancer = StateAdvancer()
        
        logger.info("Simulation orchestrator initialized with all modules")
    
    @property
    def cloudwatch_client(self):
        """Lazy initialization of CloudWatch client."""
        if self._cloudwatch_client is None and self.config.cloudwatch.enabled:
            try:
                import boto3
                self._cloudwatch_client = boto3.client(
                    'cloudwatch',
                    region_name=self.config.cloudwatch.region
                )
                logger.info("CloudWatch client initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize CloudWatch client: {e}")
        return self._cloudwatch_client
    
    def start(self) -> None:
        """
        Start the simulation engine.
        
        Runs the simulation in a continuous loop with the configured cycle interval.
        """
        logger.info("Starting ERP simulation engine...")
        logger.info(f"Cycle interval: {self.config.simulation.cycle_interval_seconds}s")
        
        self.running = True
        
        try:
            while self.running:
                cycle_start_time = time.time()
                
                try:
                    self._run_cycle()  # publishes ErrorCount itself on failure
                except Exception as e:
                    logger.error(f"Simulation cycle failed: {e}", exc_info=True)
                
                # Calculate sleep time to maintain cycle interval
                cycle_duration = time.time() - cycle_start_time
                sleep_time = max(0, self.config.simulation.cycle_interval_seconds - cycle_duration)
                
                if sleep_time > 0:
                    logger.debug(f"Sleeping for {sleep_time:.2f}s until next cycle")
                    time.sleep(sleep_time)
                else:
                    logger.warning(
                        f"Cycle took {cycle_duration:.2f}s, "
                        f"exceeding interval of {self.config.simulation.cycle_interval_seconds}s"
                    )
        
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, stopping simulation...")
        finally:
            self.stop()
    
    def stop(self) -> None:
        """Stop the simulation engine."""
        logger.info("Stopping ERP simulation engine...")
        self.running = False
        self.db.close()
        logger.info("Simulation engine stopped")
    
    def _run_cycle(self) -> None:
        """One full cycle (ADR-0090). The workshop's clock steps run FIRST, each
        on its own best-effort connection, so a slow or failing simulator can
        never starve them: load the story clock, the live-story step, the
        condition monitor, the story-anchor mirror. Then the simulator
        steps (:meth:`_run_simulation_cycle`), each in its own transaction and
        under the cycle time budget. One INFO summary line always closes it."""
        self._cycle_started = _monotonic()
        self._cycle_timings = {}
        self._cycle_skipped = []
        self._cycle_metrics = {'errors': 0}
        self._simulator_outcome = None
        self._current_cycle_id = f"cycle-{self.cycle_count + 1}-{int(time.time())}"
        try:
            self._timed('clock', self._load_story_clock)
            self._timed('story', self._run_story_step)
            self._timed('monitor', self._run_condition_monitor)
            self._timed('anchor_mirror', self._run_story_anchor_mirror)
            self._run_simulation_cycle()
        except Exception:
            self._publish_metric('ErrorCount', 1)
            raise
        finally:
            self._log_cycle_summary()

    def _timed(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` and record its wall time under ``name``."""
        start = _monotonic()
        try:
            return fn()
        finally:
            self._cycle_timings[name] = _monotonic() - start

    def _cycle_elapsed(self) -> float:
        return _monotonic() - self._cycle_started

    def _run_condition_monitor(self) -> int:
        """The MES condition monitor ahead of the simulators; never raises. The
        downtime step reuses this cycle's result instead of running it again."""
        try:
            return self.mes_simulator.run_condition_monitor(self._cycle_id())
        except Exception as e:
            logger.warning(f"Condition monitor failed: {type(e).__name__}: {e}")
            return 0

    def _cycle_id(self) -> str:
        """This cycle's id (set by :meth:`_run_cycle`)."""
        if getattr(self, '_current_cycle_id', None) is None:
            self._current_cycle_id = f"cycle-{self.cycle_count + 1}-{int(time.time())}"
        return self._current_cycle_id

    def _run_simulation_cycle(self) -> None:
        """
        The simulator steps of one cycle, in order, on ONE pooled connection.

        Each step is its own transaction (committed alone, rolled back alone on
        failure, counted in ``errors``); a step that swallowed an error and
        left the transaction aborted is detected and rolled back too, so one
        failure never rolls back its siblings. Once the cycle has used
        ``CYCLE_TIME_BUDGET_SECONDS`` the remaining OPTIONAL steps are skipped
        (named in the summary) and pick up next cycle; a required step always
        gets its bounded batch. The steps run only while this invocation holds
        ``SIMULATOR_LOCK_KEY`` (on its own connection, :meth:`_claim_simulator`):
        when another cycle holds it nothing here runs (summary
        ``skipped=simulator:busy``) and no cycle event is recorded. When the
        lock cannot be checked, or its session ends mid-cycle, the cycle FAILS
        (``SimulatorLockUnavailable``): the steps that committed stand, the
        rest do not run, and ``_run_cycle`` publishes ``ErrorCount``.
        """
        try:
            lock = self._claim_simulator()
        except SimulatorLockUnavailable:
            self._simulator_outcome = "unavailable"
            self._cycle_skipped.append(SIMULATOR_UNAVAILABLE)
            raise
        if lock is None:
            self._simulator_outcome = "busy"
            self._cycle_skipped.append(SIMULATOR_BUSY)
            return
        self._simulator_outcome = "ran"
        keeper = _LockKeeper(lock, SIMULATOR_LOCK_HEARTBEAT_SECONDS)
        try:
            with self._transaction_context() as conn:
                cycle_duration_ms = self._run_simulation_steps(conn, keeper)
        finally:
            if not keeper.stop():
                logger.warning("The simulator lock heartbeat is still blocked after it was cancelled; "
                               "it releases the lock when that statement returns, and the server "
                               f"ends the session within {SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS} s "
                               "at the latest")
        metrics = self._cycle_metrics
        self._timed('events', lambda: self._record_simulation_event(self.cycle_count, metrics))
        self._publish_cycle_metrics(metrics, cycle_duration_ms)

    def _run_simulation_steps(self, conn, keeper) -> float:
        """The steps of :meth:`_run_simulation_cycle` on ``conn`` while the
        lock is held; returns their duration in ms. ``keeper`` heartbeats the
        lock session in the background, during a step too, and is checked
        before every step (:meth:`_heartbeat`)."""
        cycle_id = self._cycle_id()
        self.cycle_count += 1
        metrics = self._cycle_metrics
        metrics.update({key: 0 for key in _CYCLE_METRIC_KEYS})
        start = _monotonic()
        self._timed('sequences', lambda: self._ensure_number_sequences(conn))
        self._timed('indexes', lambda: self._ensure_hot_path_indexes(conn))
        self._timed('backlog', lambda: self._check_backlog(conn))
        blocked: set = set()
        for step in self._simulation_steps():
            if step.name in blocked or (not step.required
                                        and self._cycle_elapsed() > CYCLE_TIME_BUDGET_SECONDS):
                self._cycle_skipped.append(step.name)
                continue
            self._heartbeat(keeper, step.name)
            errors = metrics['errors']
            result = self._run_step(conn, step, cycle_id, metrics)
            if step.blocks and metrics['errors'] > errors:   # fail closed (CycleStep.blocks)
                blocked.update(step.blocks)
                logger.warning(f"Cycle step {step.name} failed: not running "
                               f"{', '.join(step.blocks)} this cycle")
            self._run_backlog_batches(conn, step, result, cycle_id, metrics)
        return (_monotonic() - start) * 1000

    def _heartbeat(self, keeper, next_step: str) -> None:
        """Touch the lock session before ``next_step``. If the session has
        ended (here, or in a background beat during the last step), the lock
        is gone and another cycle may already run: stop rather than overlap."""
        error = keeper.beat()
        if error is not None:
            self._cycle_skipped.append(SIMULATOR_LOCK_LOST)
            raise SimulatorLockUnavailable(
                f"the simulator lock session ended before step {next_step} "
                f"({type(error).__name__}: {error}); the remaining steps were not run") from error

    def _claim_simulator(self):
        """The simulator lock, taken without waiting on a dedicated connection
        whose transaction stays open while the steps run; returns that
        connection, or None when another cycle holds the lock. A failed
        connect or probe raises ``SimulatorLockUnavailable``: the simulator is
        skipped (overlapping is what this lock prevents) and the cycle is
        reported FAILED, never as ordinary contention."""
        try:
            lock = self.db.dedicated_connection(
                idle_in_transaction_timeout_s=SIMULATOR_LOCK_IDLE_TIMEOUT_SECONDS,
                application_name="erp-simulator-lock")
        except Exception as e:
            raise SimulatorLockUnavailable(
                f"could not check the simulator lock ({type(e).__name__}: {e})") from e
        try:
            with lock.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", (SIMULATOR_LOCK_KEY,))
                held = bool(cursor.fetchone()[0])
        except Exception as e:
            self._close_quietly(lock)
            raise SimulatorLockUnavailable(
                f"could not check the simulator lock ({type(e).__name__}: {e})") from e
        if not held:
            self._close_quietly(lock)
            logger.info("Skipping the simulator steps: another cycle is still running them "
                        "(this invocation ran the clock steps only)")
            return None
        return lock

    @staticmethod
    def _close_quietly(lock) -> None:
        try:
            lock.close()
        except Exception as e:  # nosec B110 - the session ends either way
            logger.warning(f"Closing the simulator lock connection failed: {e}")

    def _simulation_steps(self) -> Tuple['CycleStep', ...]:
        """The simulator steps in cycle order (a missing simulator is skipped)."""
        return tuple(step for step in _SIMULATION_STEPS
                     if step.owner == 'self' or getattr(self, step.owner, None) is not None)

    def _run_step(self, conn, step: 'CycleStep', cycle_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Run one simulator step as its own transaction; metrics count only
        what was committed. Returns the committed result ({} on failure)."""
        start = _monotonic()
        result: Dict[str, Any] = {}
        try:
            if step.owner == 'self':
                result = getattr(self, step.method)(conn) or {}
            else:
                result = getattr(getattr(self, step.owner), step.method)(conn, cycle_id) or {}
            if _transaction_aborted(conn):
                raise RuntimeError("an error inside the step left its transaction aborted")
            conn.commit()
            _apply_step_result(step, result, metrics)
        except Exception as e:
            result = {}
            _rollback_quietly(conn)
            metrics['errors'] += 1
            logger.error(f"Cycle step {step.name} failed and was rolled back alone: "
                         f"{type(e).__name__}: {e}")  # deliberate: logged, step isolated; message carries the cause  # nosemgrep: logging-error-without-handling
        finally:
            self._cycle_timings[step.name] = _monotonic() - start
        return result

    def _run_backlog_batches(self, conn, step: 'CycleStep', result: Dict[str, Any],
                             cycle_id: str, metrics: Dict[str, Any]) -> None:
        """While the backlog is above ``BACKLOG_WARN_THRESHOLD``, run up to
        ``step.backlog_batches - 1`` more bounded batches of ``step`` (timed as
        ``<name>_<n>``), each only after a batch that made progress and only
        if another batch of the last one's duration still fits in the budget."""
        if self._cycle_metrics.get('ready_to_ship', 0) <= BACKLOG_WARN_THRESHOLD:
            return
        last = step.name
        for batch in range(2, step.backlog_batches + 1):
            progressed = bool(step.copies) and result.get(step.copies[0][1], 0) > 0
            fits = self._cycle_elapsed() + self._cycle_timings.get(last, 0.0) <= CYCLE_TIME_BUDGET_SECONDS
            if not (progressed and fits):
                return
            last = f"{step.name}_{batch}"
            result = self._run_step(conn, replace(step, name=last), cycle_id, metrics)

    def _ensure_hot_path_indexes(self, conn) -> None:
        """Build the missing hot-path indexes concurrently on the step connection
        (``ensure_hot_path_indexes``: autocommit, inside its own time budget).
        Best effort: a failed or timed-out build is skipped for this cycle and
        the steps still run. The simulator lock's session holds no table lock
        and no snapshot between its heartbeats, so the build does not wait for
        it (a heartbeat caught mid-statement when the build checks for older
        snapshots would make it wait, within the build's budget)."""
        try:
            ensure_hot_path_indexes(conn)
        except Exception as e:
            _rollback_quietly(conn)
            logger.warning(f"Hot-path indexes not ensured this cycle: {type(e).__name__}: {e}")

    def _ensure_number_sequences(self, conn) -> None:
        """Create the business-number sequences if missing (own commit; best effort)."""
        try:
            with conn.cursor() as cursor:
                ensure_number_sequences(cursor)
            conn.commit()
        except Exception as e:
            _rollback_quietly(conn)
            logger.warning(f"Business-number sequences not ensured this cycle: {type(e).__name__}: {e}")

    def _check_backlog(self, conn) -> None:
        """Count 'ready to ship' orders; above the threshold, say how the bounded
        shipping step drains it (backlog recovery is just the bounded batch)."""
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM customer_orders WHERE status = 'READY_TO_SHIP'")
                ready = int(cursor.fetchone()[0])
            conn.commit()
        except Exception as e:
            _rollback_quietly(conn)
            logger.warning(f"Backlog check failed: {type(e).__name__}: {e}")
            return
        self._cycle_metrics['ready_to_ship'] = ready
        if ready > BACKLOG_WARN_THRESHOLD:
            logger.warning(f"Shipping backlog: {ready} orders ready to ship (> {BACKLOG_WARN_THRESHOLD}); "
                           f"draining up to {BACKLOG_SHIPMENT_BATCHES} x {MAX_ROWS_PER_CYCLE} per cycle "
                           f"while above {BACKLOG_WARN_THRESHOLD} "
                           f"(~{-(-ready // (BACKLOG_SHIPMENT_BATCHES * MAX_ROWS_PER_CYCLE))} cycles)")

    def _log_cycle_summary(self) -> None:
        """ONE INFO line per cycle: totals, budget, skipped steps, step timings."""
        m = self._cycle_metrics
        timings = ' '.join(f"{name}={secs:.2f}s" for name, secs in self._cycle_timings.items())
        counts = ' '.join(f"{key}={m.get(key, 0)}" for key in _SUMMARY_COUNT_KEYS if key in m)
        skipped = ','.join(self._cycle_skipped) or '-'
        logger.info(f"Cycle {self.cycle_count} summary: total={self._cycle_elapsed():.2f}s "
                    f"budget={CYCLE_TIME_BUDGET_SECONDS:.0f}s skipped={skipped} errors={m.get('errors', 0)} "
                    f"{counts} | {timings}")

    @contextmanager
    def _transaction_context(self):
        """One pooled connection for the simulator steps.

        Only the checkout is retried (up to 3 attempts, exponential backoff).
        An exception from the body propagates unchanged: a generator context
        manager cannot run its body twice, and retrying here once turned every
        body error into "generator didn't stop after throw()".
        """
        with ExitStack() as stack:
            conn = self._checkout_with_retry(stack)
            yield conn

    def _checkout_with_retry(self, stack: ExitStack):
        """Enter ``self.db.connection()`` on ``stack``, retrying a failed checkout."""
        max_retries = 3
        retry_delay = 1  # seconds
        for attempt in range(1, max_retries + 1):
            try:
                return stack.enter_context(self.db.connection())
            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"Connection checkout failed after {max_retries} attempts: {e}")  # deliberate: logged for ops visibility then re-raised; message carries context  # nosemgrep: logging-error-without-handling
                    raise
                logger.warning(f"Connection checkout failed (attempt {attempt}/{max_retries}): {e}. "
                               f"Retrying in {retry_delay}s...")
                time.sleep(retry_delay)  # deliberate pacing/backoff delay in a bounded retry loop  # nosemgrep: arbitrary-sleep
                retry_delay *= 2  # Exponential backoff

    def _record_simulation_event(self, cycle_number: int, metrics: Dict[str, int]) -> None:
        """
        Record simulation event counts to database for sparkline visualization.
        
        This creates one row per Lambda cycle with counts for all metrics.
        All sparklines read from this table to ensure perfect alignment.
        
        Args:
            cycle_number: Sequential cycle number (ignored - we calculate from database).
            metrics: Dictionary of metric counts from this cycle.
        """
        try:
            with self.db.connection() as conn:
                with conn.cursor() as cursor:
                    # Get the next cycle number from database (persistent across Lambda invocations)
                    cursor.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM simulation_events")
                    next_cycle_number = cursor.fetchone()[0]
                    row = {'cycle_number': next_cycle_number,
                           **self._cycle_counts(metrics), **self._site_mes_counts(cursor)}
                    self._insert_event_row(cursor, row)
                    
                    # Clean up old events (keep last 24 hours)
                    cursor.execute("""
                        DELETE FROM simulation_events 
                        WHERE event_timestamp < NOW() - INTERVAL '24 hours'
                    """)
                    
                    conn.commit()
                    logger.info(f"Recorded simulation event for cycle {next_cycle_number}")
        
        except Exception as e:
            logger.error(f"Failed to record simulation event: {e}", exc_info=True)
            # Don't raise - this is non-critical

    @staticmethod
    def _cycle_counts(metrics: Dict[str, int]) -> Dict[str, int]:
        """simulation_events column -> count, from the transition counts captured this cycle."""
        m = metrics.get
        return {
            # Orders - all new orders start in PENDING
            'orders_new': m('orders_created', 0),
            'orders_pending': m('orders_pending_to_confirmed', 0),  # Transitions TO confirmed
            'orders_confirmed': m('orders_confirmed_to_ready', 0) + m('orders_confirmed_to_backordered', 0),  # FROM confirmed
            'orders_backordered': m('orders_confirmed_to_backordered', 0),  # Transitions TO backordered
            'orders_shipped': m('shipments_created', 0),  # Shipments created = orders shipped
            'orders_in_production': m('production_runs_created', 0),
            # Production - all new runs start in PLANNED
            'production_new': m('production_runs_created', 0),
            'production_planned': m('production_runs_created', 0),
            'production_released': m('production_planned_to_released', 0),
            'production_in_progress': m('mes_work_orders_created', 0),  # MES work orders = runs IN_PROGRESS
            'production_completed': m('production_runs_completed', 0),
            # Procurement
            'procurement_sent': m('pos_created', 0),
            'procurement_in_transit': m('pos_in_transit', 0),
            'procurement_received': m('pos_received', 0),
            # Shipping - all new shipments start in PREPARING
            'shipping_preparing': m('shipments_created', 0),
            'shipping_picked': m('shipping_preparing_to_picked', 0),
            'shipping_packed': m('shipping_picked_to_packed', 0),
            'shipping_shipped': m('shipping_packed_to_shipped', 0),
            'shipping_in_transit': m('shipping_shipped_to_in_transit', 0),
            'shipping_delivered': m('shipping_in_transit_to_delivered', 0),
            # Finance - creation counts and dollar amounts
            'finance_invoices': m('invoices_generated', 0),
            'finance_paid': m('payments_recorded', 0),
            'finance_revenue': m('finance_revenue', 0),
            'finance_collected': m('finance_collected', 0),
            # MES (aggregate)
            'mes_scheduled': m('mes_work_orders_created', 0),
            'mes_running': m('mes_production_events_created', 0),
            'mes_completed': m('mes_quality_checks_created', 0),
            # Inventory: POs received + production completed this cycle
            'inventory_transactions': m('pos_received', 0) + m('production_runs_completed', 0),
        }

    @staticmethod
    def _site_mes_counts(cursor) -> Dict[str, int]:
        """Per-site MES work-order counts (last 2 minutes) as mes_<prefix>_<state> columns."""
        cursor.execute("""
            SELECT site,
                   COUNT(*) FILTER (WHERE status = 'SCHEDULED') as scheduled,
                   COUNT(*) FILTER (WHERE status = 'RUNNING') as running,
                   COUNT(*) FILTER (WHERE status = 'COMPLETED') as completed
            FROM mes_work_orders
            WHERE created_at > NOW() - INTERVAL '2 minutes'
            GROUP BY site
        """)
        site_mes = {row[0]: row[1:4] for row in cursor.fetchall()}
        return {
            f'mes_{prefix}_{state}': count
            for site, prefix in SITE_EVENT_PREFIX.items()
            for state, count in zip(SITE_EVENT_STATES, site_mes.get(site, (0, 0, 0)))
        }

    @staticmethod
    def _insert_event_row(cursor, row: Dict[str, int]) -> None:
        """INSERT one simulation_events row; column names come only from code constants.

        Columns the live table lacks are skipped (see _existing_event_columns).
        """
        existing = _existing_event_columns(cursor, row)
        if existing is not None:
            row = {column: value for column, value in row.items() if column in existing}
        columns = ', '.join(row)
        placeholders = ', '.join(['%s'] * len(row))
        cursor.execute(
            f"INSERT INTO simulation_events (event_timestamp, {columns}) "
            f"VALUES (NOW(), {placeholders})",  # nosec B608 - column names are code constants, values are bound parameters
            tuple(row.values()),
        )

    def _update_supplier_qualifications(self, conn) -> dict:
        """Update supplier qualification_status based on MES quality data.
        
        Dynamic threshold logic:
        - For each item, count how many APPROVED suppliers exist
        - Only degrade a supplier if the item will still have >= 2 APPROVED suppliers after
        - Always maintain 2-3 suppliers in non-APPROVED states across the system
          (DISQUALIFIED, UNDER_EVALUATION, CONDITIONAL) to keep the demo realistic
        - If fewer than 2 suppliers are non-APPROVED, lower the degradation threshold
          to push borderline suppliers into CONDITIONAL/UNDER_EVALUATION
        - If more than 3 suppliers are non-APPROVED, raise the recovery threshold
          to let some recover faster
        
        Degradation: APPROVED → CONDITIONAL → UNDER_EVALUATION → DISQUALIFIED
        Recovery: DISQUALIFIED → UNDER_EVALUATION → CONDITIONAL → APPROVED
        """
        changes = 0
        degraded = []
        improved = []
        
        cursor = conn.cursor()
        try:
            # Count current non-APPROVED suppliers globally
            cursor.execute("""
                SELECT COUNT(*) FROM suppliers 
                WHERE is_active = TRUE AND qualification_status != 'APPROVED'
            """)
            non_approved_count = cursor.fetchone()[0]
            
            # Dynamic thresholds based on how many are already non-APPROVED
            # Target: always 2-3 non-APPROVED suppliers
            if non_approved_count < 2:
                # Too few non-APPROVED — be more aggressive about degrading
                degrade_threshold = 0.05  # 5% rejection rate triggers degradation
                improve_threshold = 0.005  # Very low bar to improve (keep them degraded)
            elif non_approved_count > 3:
                # Too many non-APPROVED — be more aggressive about recovery
                degrade_threshold = 0.20  # 20% rejection rate needed to degrade further
                improve_threshold = 0.05  # 5% is good enough to improve
            else:
                # In the sweet spot (2-3 non-APPROVED) — normal thresholds
                degrade_threshold = 0.10  # 10% rejection rate
                improve_threshold = 0.02  # 2% to improve
            
            # Get quality data per supplier with item-level approved counts
            cursor.execute("""
                WITH supplier_quality AS (
                    SELECT 
                        s.supplier_id,
                        s.supplier_code,
                        s.qualification_status,
                        COALESCE(SUM(qc.fail_count), 0) AS total_bad,
                        COALESCE(SUM(qc.pass_count + qc.fail_count), 0) AS total_checked
                    FROM suppliers s
                    LEFT JOIN material_lots ml ON ml.supplier_id = s.supplier_id
                    LEFT JOIN lot_production_usage lpu ON lpu.lot_id = ml.lot_id
                    LEFT JOIN production_runs pr ON pr.run_id = lpu.production_run_id
                    LEFT JOIN mes_work_orders mwo ON mwo.production_run_id = pr.run_id
                    LEFT JOIN mes_quality_checks qc ON qc.work_order_id = mwo.work_order_id
                        AND qc.check_time > NOW() - INTERVAL '7 days'
                    WHERE s.is_active = TRUE
                    GROUP BY s.supplier_id, s.supplier_code, s.qualification_status
                )
                SELECT supplier_id, supplier_code, qualification_status, total_bad, total_checked,
                    CASE WHEN total_checked > 0 THEN total_bad::float / total_checked ELSE 0 END AS rejection_rate
                FROM supplier_quality
                WHERE total_checked > 0
            """)
            
            rows = cursor.fetchall()
            
            # Status ladders
            degrade_map = {
                'APPROVED': 'CONDITIONAL',
                'CONDITIONAL': 'UNDER_EVALUATION',
                'UNDER_EVALUATION': 'DISQUALIFIED',
            }
            improve_map = {
                'DISQUALIFIED': 'UNDER_EVALUATION',
                'UNDER_EVALUATION': 'CONDITIONAL',
                'CONDITIONAL': 'APPROVED',
            }
            
            for row in rows:
                supplier_id, supplier_code, current_status, total_bad, total_checked, rejection_rate = row
                
                # DEGRADE: only if rejection rate exceeds dynamic threshold
                # AND the supplier's items will still have enough APPROVED suppliers
                if rejection_rate > degrade_threshold and current_status in degrade_map:
                    # Check: will each item this supplier provides still have >= 2 APPROVED sources?
                    cursor.execute("""
                        SELECT isup.item_id, COUNT(*) AS approved_count
                        FROM item_suppliers isup
                        JOIN suppliers s2 ON s2.supplier_id = isup.supplier_id
                        WHERE isup.item_id IN (
                            SELECT item_id FROM item_suppliers WHERE supplier_id = %s
                        )
                        AND s2.qualification_status = 'APPROVED'
                        AND s2.supplier_id != %s
                        GROUP BY isup.item_id
                    """, (supplier_id, supplier_id))
                    item_counts = cursor.fetchall()
                    
                    # Only degrade if ALL items will still have >= 2 approved suppliers
                    safe_to_degrade = all(count >= 2 for _, count in item_counts) if item_counts else False
                    
                    if safe_to_degrade:
                        new_status = degrade_map[current_status]
                        cursor.execute(
                            "UPDATE suppliers SET qualification_status = %s WHERE supplier_id = %s",
                            (new_status, supplier_id)
                        )
                        changes += 1
                        non_approved_count += 1 if current_status == 'APPROVED' else 0
                        degraded.append(f"{supplier_code}: {current_status}→{new_status} (reject={rejection_rate:.1%})")
                
                # IMPROVE: if rejection rate below dynamic threshold and currently degraded
                elif rejection_rate < improve_threshold and current_status in improve_map:
                    # Don't improve if we'd drop below 2 non-APPROVED
                    if non_approved_count > 2 or current_status == 'DISQUALIFIED':
                        new_status = improve_map[current_status]
                        cursor.execute(
                            "UPDATE suppliers SET qualification_status = %s WHERE supplier_id = %s",
                            (new_status, supplier_id)
                        )
                        changes += 1
                        non_approved_count -= 1 if new_status == 'APPROVED' else 0
                        improved.append(f"{supplier_code}: {current_status}→{new_status} (reject={rejection_rate:.1%})")
            
            # Cooldown recovery: suppliers with no recent lot usage improve one step
            # But only if we still have enough non-APPROVED (> 2)
            if non_approved_count > 2:
                cursor.execute("""
                    SELECT s.supplier_id, s.supplier_code, s.qualification_status
                    FROM suppliers s
                    WHERE s.qualification_status IN ('DISQUALIFIED', 'UNDER_EVALUATION')
                      AND s.is_active = TRUE
                      AND s.supplier_id NOT IN (
                          SELECT DISTINCT ml.supplier_id
                          FROM material_lots ml
                          JOIN lot_production_usage lpu ON lpu.lot_id = ml.lot_id
                          WHERE lpu.usage_date > NOW() - INTERVAL '5 days'
                      )
                    LIMIT 1
                """)
                cooldown_row = cursor.fetchone()
                if cooldown_row:
                    supplier_id, supplier_code, current_status = cooldown_row
                    new_status = improve_map[current_status]
                    cursor.execute(
                        "UPDATE suppliers SET qualification_status = %s WHERE supplier_id = %s",
                        (new_status, supplier_id)
                    )
                    changes += 1
                    improved.append(f"{supplier_code}: {current_status}→{new_status} (cooldown)")
            
            conn.commit()
        except Exception as e:
            logger.warning(f"Supplier qualification update error (non-fatal): {e}")
            try:
                conn.rollback()
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
        finally:
            cursor.close()
        
        return {"changes": changes, "degraded": degraded, "improved": improved}

    def _publish_cycle_metrics(self, metrics: Dict[str, int], cycle_duration_ms: float) -> None:
        """
        Publish cycle metrics to CloudWatch.
        
        Args:
            metrics: Dictionary of metric names and values.
            cycle_duration_ms: Cycle execution time in milliseconds.
        """
        if not self.config.cloudwatch.enabled or not self.cloudwatch_client:
            return
        
        try:
            metric_data = []
            
            # Add all metrics
            for metric_name, value in metrics.items():
                metric_data.append({
                    'MetricName': metric_name.replace('_', ' ').title().replace(' ', ''),
                    'Value': value,
                    'Unit': 'Count',
                    'Timestamp': datetime.utcnow()
                })
            
            # Add cycle duration metric
            metric_data.append({
                'MetricName': 'CycleExecutionTime',
                'Value': cycle_duration_ms,
                'Unit': 'Milliseconds',
                'Timestamp': datetime.utcnow()
            })
            
            # Publish to CloudWatch
            self.cloudwatch_client.put_metric_data(
                Namespace=self.config.cloudwatch.metrics_namespace,
                MetricData=metric_data
            )
            
            logger.debug(f"Published {len(metric_data)} metrics to CloudWatch")
        
        except Exception as e:
            logger.warning(f"Failed to publish metrics to CloudWatch: {e}")
    
    def _publish_metric(self, metric_name: str, value: float, unit: str = 'Count') -> None:
        """
        Publish a single metric to CloudWatch.
        
        Args:
            metric_name: Name of the metric.
            value: Metric value.
            unit: Metric unit (default: Count).
        """
        if not self.config.cloudwatch.enabled or not self.cloudwatch_client:
            return
        
        try:
            self.cloudwatch_client.put_metric_data(
                Namespace=self.config.cloudwatch.metrics_namespace,
                MetricData=[{
                    'MetricName': metric_name,
                    'Value': value,
                    'Unit': unit,
                    'Timestamp': datetime.utcnow()
                }]
            )
        except Exception as e:
            logger.warning(f"Failed to publish metric {metric_name}: {e}")
    
    def run_single_cycle(self) -> Dict[str, Any]:
        """
        Run a single simulation cycle (for testing).
        
        Returns:
            Dictionary with cycle metrics: ``simulator`` is ``ran``, or
            ``busy`` when another cycle held the simulator and only the clock
            steps ran (``cycle_result`` reads it for synchronous callers). A
            lock that could not be checked or was lost mid-cycle is a FAILED
            cycle (``success`` False, ``simulator`` ``unavailable``).
        """
        logger.info("Running single simulation cycle...")
        
        try:
            self._run_cycle()
            return {'success': True, 'cycle_count': self.cycle_count,
                    'simulator': getattr(self, '_simulator_outcome', None) or 'ran'}
        except Exception as e:
            logger.error(f"Single cycle execution failed: {e}", exc_info=True)
            failed = {'success': False, 'error': str(e)}
            if isinstance(e, SimulatorLockUnavailable):
                failed['simulator'] = 'unavailable'
            return failed

    def _load_story_clock(self) -> None:
        """Read the live-story anchor (``simulation_config.story_anchor_epoch``)
        into one of three states (ADR-0085):

        * ``absent``  - no key (demo / frozen): no story step, and the MES
          condition monitor stays on the wall clock (anchor 0.0);
        * ``present`` - the monitor and the story step run on the anchor;
        * ``unknown`` - the read failed: skip BOTH the condition monitor
          (``condition_anchor_epoch = None``) and the story step THIS cycle.

        One plain best-effort connection, no retry (``_transaction_context``'s
        retry cannot re-run a failed body). The Lambda builds a fresh
        orchestrator per invocation, so there is no previous clock to keep:
        guessing the wall clock could raise a stray condition work order.
        """
        try:
            with self.db.connection() as conn:
                anchor = story_live.read_anchor(conn)
        except Exception as e:
            logger.warning(f"Story clock unknown this cycle (anchor read failed: {e}); "
                           f"skipping the condition monitor and the story step")
            self._story_clock, self._story_anchor = STORY_CLOCK_UNKNOWN, None
            self.mes_simulator.condition_anchor_epoch = None
            return
        self._story_clock = STORY_CLOCK_ABSENT if anchor is None else STORY_CLOCK_PRESENT
        self._story_anchor = anchor
        self.mes_simulator.condition_anchor_epoch = anchor or 0.0
        self.mes_simulator.condition_anchor_reader = self._read_story_anchor
        self.mes_simulator.condition_downtime_reader = self._read_downtimes

    def _read_story_anchor(self):
        """A fresh read of the stored anchor (own pooled connection): the MES
        condition monitor confirms the era with it under the story-clock lock."""
        with self.db.connection() as conn:
            return story_live.read_anchor(conn)

    def _read_downtimes(self, downtime_ids):
        """The MES downtime events ``downtime_ids`` (own pooled connection):
        the condition monitor completes the bridge orders raised from them."""
        from .mes_simulator import MESSimulator
        with self.db.connection() as conn:
            return MESSimulator.read_downtimes(conn, downtime_ids)

    def _run_story_step(self) -> int:
        """Live-story defect step, BEFORE the simulator steps (ADR-0090); a
        no-op unless the clock is ``present``. Own best-effort connection;
        never raises."""
        if self._story_clock != STORY_CLOCK_PRESENT or self._story_anchor is None:
            return 0
        try:
            with self.db.connection() as conn:
                return story_live.apply_cycle(conn, time.time(), self._story_anchor)
        except Exception as e:
            logger.warning(f"Story step failed: {e}")
            return 0

    def _run_story_anchor_mirror(self) -> str:
        """Copy this cycle's story clock into the SSM parameter the story
        site's OPC UA server follows (ADR-0092), after the story step and the
        condition monitor, BEFORE the simulator steps (ADR-0090). A no-op
        unless a live synth set ``STORY_ANCHOR_PARAMETER``; never raises.
        When the parameter carries the anchor, the telemetry rows record it
        (own best-effort connection, compare-and-set on the stored anchor).
        A placement's catch-up rides along until it ends; the stored anchor
        source tells the mirror whether to hold the feed until the first placement."""
        now = time.time()
        feed = (self._story_feed_state() if self._story_clock == STORY_CLOCK_PRESENT
                else story_anchor_mirror.FeedState())
        outcome = story_anchor_mirror.sync(self._story_clock, self._story_anchor,
                                           catch_up=feed.catch_up, now=now, source=feed.source)
        if outcome in ("written", "same") and self._story_clock == STORY_CLOCK_PRESENT:
            try:
                with self.db.connection() as conn:
                    story_anchor_mirror.confirm(conn, self._story_anchor, now)
            except Exception as e:
                logger.warning(f"Story anchor mirror confirmation failed: {e}")
        self._run_story_backfill_mirror(now)
        return outcome

    def _run_story_backfill_mirror(self, now: float) -> str:
        """Copy the stored history-replay window into the SECOND SSM parameter
        the story server polls (the replay ADR); records ``story_feed_live_since``
        (the replay's identity) once so the value is stable across cycles. A no-op
        unless a live synth set ``STORY_FEED_BACKFILL_PARAMETER``; never raises."""
        if not story_anchor_mirror.backfill_enabled():
            return "off"
        fact, live_since = None, None
        try:
            with self.db.connection() as conn:
                fact, live_since = story_anchor_mirror.read_backfill_state(conn)
        except Exception as e:
            logger.warning(f"Story backfill mirror: no connection for the state: {e}")
        outcome = story_anchor_mirror.sync_backfill(fact, live_since, now)
        _value, record_since = story_anchor_mirror.backfill_value(fact, live_since, now)
        if outcome == "written" and record_since is not None and live_since is None:
            self._persist_backfill_live_since(record_since)
        return outcome

    def _persist_backfill_live_since(self, since: int) -> None:
        """Record ``story_feed_live_since`` the first time the replay starts."""
        try:
            with self.db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO simulation_config (config_key, config_value, default_value, "
                        "data_type, category, description) VALUES (%s, %s, %s, 'string', 'story', %s) "
                        "ON CONFLICT (config_key) DO NOTHING",
                        (story_anchor_mirror.LIVE_SINCE_KEY, str(int(since)), str(int(since)),
                         "Wall epoch the one-time history replay was first published (its identity)"))
                conn.commit()
        except Exception as e:
            logger.warning(f"Story backfill live-since not recorded this cycle: {e}")

    def _story_feed_state(self):
        """The stored placement catch-up and anchor source (both None: no
        parameter, or not read)."""
        if not story_anchor_mirror.enabled():
            return story_anchor_mirror.FeedState()
        try:
            with self.db.connection() as conn:
                return story_anchor_mirror.read_feed_state(conn)
        except Exception as e:
            logger.warning(f"Story anchor mirror: no connection for the feed state: {e}")
            return story_anchor_mirror.FeedState()
