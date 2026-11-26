# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Indexes the bounded simulator hot paths need (ADR-0090 backlog drain).

A shipping batch of 200 orders once took 238 s on a 14k backlog: the
per-line ``ShippingSimulator._traceable_batch_id`` lookup ("latest COMPLETED
run of this product with lot usage") had only ``production_runs(product_id)``
to go on, so every call read ALL of the product's runs and sorted them
(O(table) per line item). ``idx_production_runs_traceable`` matches its
equality + ORDER BY exactly, so the lookup walks the index newest-first and
stops at the first traceable run. ``idx_customer_orders_status_order`` serves
the READY_TO_SHIP batch read (``WHERE status = ... ORDER BY order_id LIMIT``)
and the backlog COUNT without touching other statuses;
``idx_customer_orders_status_date`` serves the delivered-order catch-up
(SHIPPED, oldest order first). The bounded shipment
and invoice status steps (``state_advancer``) take their oldest rows first:
``idx_shipments_status_shipment_date`` (PREPARING, by shipment date),
``idx_shipments_status_updated`` (every later shipment status, by last update)
and ``idx_invoices_status_due`` (SENT, by due date) hand each batch over in
that order and stop at the bound, so no read sorts or scans a backlog.

db-init creates them with the schema (``HOT_PATH_INDEXES_SQL`` in
lambda-db-init/handler.py, mirrored in erp_simulation/schema/*.sql; a parity
test holds all three equal): plain ``CREATE INDEX`` on tables still empty. An
existing account gets them from the orchestrator, which calls
:func:`ensure_hot_path_indexes` at the start of every cycle: one catalog read
when they all exist and are valid, otherwise a concurrent build. Its tables can
be large, and a plain build holds a SHARE lock for the whole build (every
writer waits) with no bound on how long the build runs, so the orchestrator
builds with ``CREATE INDEX CONCURRENTLY`` under a statement timeout instead
(:func:`ensure_hot_path_indexes`).
"""

import time
from typing import List, Tuple

from .logging_config import get_logger

logger = get_logger(__name__)

# (index name, table, column list). Code constants: the only identifiers
# interpolated into DDL here.
HOT_PATH_INDEXES: Tuple[Tuple[str, str, str], ...] = (
    ('idx_production_runs_traceable', 'production_runs',
     '(product_id, status, actual_end_time DESC NULLS LAST, updated_at DESC NULLS LAST)'),
    ('idx_customer_orders_status_order', 'customer_orders', '(status, order_id)'),
    ('idx_customer_orders_status_date', 'customer_orders', '(status, order_date NULLS FIRST, order_id)'),
    ('idx_shipments_status_shipment_date', 'shipments', '(status, shipment_date)'),
    ('idx_shipments_status_updated', 'shipments', '(status, updated_at)'),
    ('idx_invoices_status_due', 'invoices', '(status, due_date)'),
)

# The whole build pass of one cycle, as a statement timeout: each DDL gets
# what is left. The pass runs before the required steps and the Lambda stops
# at 300 s, so the pass may use only a small part of it: 60 s still leaves the
# required steps most of the timeout, and it is under half of the 150 s after
# which the cycle skips its optional steps. It is ample for the backlogs seen
# live: on local PostgreSQL 16 the three shipment and invoice builds took 1 s
# together on 464,000 shipments and 236,000 invoices. A build that does not
# fit is cancelled and tried again next cycle.
INDEX_BUILD_BUDGET_SECONDS = 60.0

# Valid or not, per hot-path index that exists in this schema.
_INDEX_STATE_SQL = """
    SELECT c.relname, i.indisvalid FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
"""


def _monotonic() -> float:
    """The budget's clock (tests replace it)."""
    return time.monotonic()


def index_ddl(name: str, table: str, columns: str) -> str:
    """db-init's DDL for one hot-path index (empty tables; the schema files match)."""
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}"


def concurrent_index_ddl(name: str, table: str, columns: str) -> str:
    """The orchestrator's DDL for one hot-path index on an existing account."""
    return f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {columns}"


def ensure_hot_path_indexes(conn) -> List[str]:
    """Build the hot-path indexes that are missing or INVALID; return the names built.

    Reads the catalog first (a no-op when all exist and are valid: ``CREATE
    INDEX ... IF NOT EXISTS`` alone would still take a table lock). Each build
    is ``CREATE INDEX CONCURRENTLY``: writers of the table carry on while it
    runs. It cannot run inside a transaction block, so ``conn`` (idle, not in a
    transaction) is switched to autocommit for the pass and handed back as it
    came. Each statement runs under ``statement_timeout`` set to what is left
    of ``INDEX_BUILD_BUDGET_SECONDS``, which bounds the build and every lock
    wait inside it. A failed or cancelled concurrent build leaves an INVALID
    index; the next pass drops it (``DROP INDEX CONCURRENTLY``) and builds it
    again. Only one pass runs at a time (the orchestrator holds the simulator
    lock), so an INVALID index is never another pass's build in progress. A
    build that fails or runs out of budget ends the pass for this cycle: it
    is logged, never raised.
    """
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            work = _index_work(cursor)
            if not work:
                return []
            try:
                return _build(cursor, work, _monotonic() + INDEX_BUILD_BUDGET_SECONDS)
            finally:
                cursor.execute("RESET statement_timeout")
    finally:
        conn.autocommit = previous


def _index_work(cursor) -> List[Tuple[Tuple[str, str, str], bool]]:
    """``(spec, invalid)`` for each hot-path index that is missing (False) or
    INVALID (True), in ``HOT_PATH_INDEXES`` order."""
    cursor.execute(_INDEX_STATE_SQL, ([name for name, _table, _columns in HOT_PATH_INDEXES],))
    valid = dict(cursor.fetchall())
    return [(spec, spec[0] in valid) for spec in HOT_PATH_INDEXES if not valid.get(spec[0], False)]


class _BudgetSpent(Exception):
    """The pass's budget ran out before a statement could start."""


def _within_budget(cursor, sql: str, deadline: float) -> None:
    """Run ``sql`` under a ``statement_timeout`` of what is left before ``deadline``."""
    left_ms = int((deadline - _monotonic()) * 1000)
    if left_ms <= 0:
        raise _BudgetSpent()
    # SET cannot take a bound parameter; set_config(..., false) is a session SET.
    cursor.execute("SELECT set_config('statement_timeout', %s, false)", (str(left_ms),))
    cursor.execute(sql)


def _build(cursor, work, deadline: float) -> List[str]:
    """Drop (when INVALID) and build each index of ``work`` until one fails or
    ``deadline`` passes; the names built."""
    built: List[str] = []
    for (name, table, columns), invalid in work:
        try:
            if invalid:
                _within_budget(cursor, f"DROP INDEX CONCURRENTLY IF EXISTS {name}", deadline)  # nosec B608 - code constants above, never input
            _within_budget(cursor, concurrent_index_ddl(name, table, columns), deadline)  # nosec B608 - code constants above, never input
        except _BudgetSpent:
            logger.warning(f"Hot-path index {name} on {table} not built this cycle: the "
                           f"{INDEX_BUILD_BUDGET_SECONDS:g} s index budget is spent; next cycle tries again")
            break
        except Exception as e:
            logger.warning(f"Hot-path index {name} on {table} not built this cycle ({type(e).__name__}: {e}); "
                           "the next cycle drops what the build left and tries again")
            break
        built.append(name)
        logger.info(f"Built hot-path index {name} on {table} (concurrently)")
    return built
