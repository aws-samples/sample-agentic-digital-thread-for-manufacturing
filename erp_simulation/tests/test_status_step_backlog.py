# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-cycle status steps move one bounded batch per cycle.

``advance_shipment_states`` (five shipment transitions),
``advance_invoice_states`` (SENT -> OVERDUE), ``advance_order_states``
(PENDING -> CONFIRMED) and the MES consumption pass
(``track_material_consumption``) used to read every row of their status in one
statement. A stalled cycle leaves that backlog growing, and the first cycle
that reaches the step then reads all of it (on a live account, unbounded
backlog reads made every cycle time out at 236,000 rows and ran the 512 MB
Lambda out of memory at 464,000). Each step now moves at most
``MAX_ROWS_PER_CYCLE`` rows per status per cycle and the rest wait for later
cycles. A backlog smaller than the bound is read whole: the status steps move
it exactly as before, and the consumption pass works it in ``work_order_id``
order (the unbounded read had no order at all), so which random quantities a
work order draws does not depend on where the cycle's scan started.

The shipment and invoice steps take the oldest rows first, by the timestamp
the step waits on; every row a batch reads leaves its status, so nothing can
starve. The PENDING read takes ``order_id`` order like the other
``customer_orders`` status reads. A completed work order whose product has no
active BOM never gets a consumption row and stays pending for good, so the
consumption read starts at a fresh random point each cycle and wraps round
(the BACKORDERED read does the same).
"""
from __future__ import annotations

import random
import re
import uuid

import pytest

from erp_simulation import mes_simulator, state_advancer, utils
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.state_advancer import StateAdvancer
from erp_simulation.tests._pg_sql import stmt
from erp_simulation.tests.test_fg_location_routing import pg_conn  # noqa: F401  (fixture)

_SHIPMENT_SOURCES = ('PREPARING', 'PICKED', 'PACKED', 'SHIPPED', 'IN_TRANSIT')
_SHIPMENT_KEYS = ('shipping_preparing_to_picked', 'shipping_picked_to_packed',
                  'shipping_packed_to_shipped', 'shipping_shipped_to_in_transit',
                  'shipping_in_transit_to_delivered')
_WHERE_STATUS = re.compile(r"\bWHERE status = '(\w+)'")


# --------------------------------------------------------------------------
# Scripted cursors (run in every lane).
# --------------------------------------------------------------------------
class _StatusCursor:
    """Every status UPDATE matches ``backlog`` rows; a bound LIMIT caps how many it moves."""

    def __init__(self, backlog):
        self.backlog = backlog
        self.reads = []
        self._all = []

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self._all = []
        match = _WHERE_STATUS.search(sql)
        if sql.startswith('UPDATE') and match:
            limit = params[-1] if 'LIMIT %s' in sql else None
            self.reads.append((match.group(1), limit))
            moved = self.backlog if limit is None else min(self.backlog, limit)
            self._all = [(f'id-{i}', f'N-{i}') for i in range(moved)]

    def fetchall(self):
        return self._all

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _advancer():
    return StateAdvancer.__new__(StateAdvancer)        # skip get_config()


def test_shipment_states_move_one_bounded_batch_per_status():
    bound = utils.MAX_ROWS_PER_CYCLE
    cursor = _StatusCursor(backlog=bound * 5)
    result = _advancer().advance_shipment_states(_Conn(cursor), 'cycle-1')
    # The order-delivery UPDATE uses the alias ``co.status``, which _WHERE_STATUS
    # (``WHERE status = ...``) does not match, so it records no read here.
    assert cursor.reads == [(status, bound) for status in _SHIPMENT_SOURCES]
    assert [result[key] for key in _SHIPMENT_KEYS] == [bound] * 5
    assert result['shipping_orders_delivered'] == 0
    assert result['transitions'] == 5 * bound


def test_invoice_states_move_one_bounded_batch():
    bound = utils.MAX_ROWS_PER_CYCLE
    cursor = _StatusCursor(backlog=bound * 5)
    result = _advancer().advance_invoice_states(_Conn(cursor), 'cycle-1')
    assert cursor.reads == [('SENT', bound)]
    assert result['finance_sent_to_overdue'] == result['transitions'] == bound


def test_order_states_move_one_bounded_batch():
    bound = utils.MAX_ROWS_PER_CYCLE
    cursor = _StatusCursor(backlog=bound * 5)
    result = _advancer().advance_order_states(_Conn(cursor), 'cycle-1')
    assert cursor.reads == [('PENDING', bound)]
    assert result['orders_pending_to_confirmed'] == result['transitions'] == bound


def test_a_backlog_below_the_bound_moves_every_row_with_the_same_result():
    cursor = _StatusCursor(backlog=7)
    advancer = _advancer()
    assert advancer.advance_shipment_states(_Conn(cursor), 'c') == {
        'transitions': 35, **{key: 7 for key in _SHIPMENT_KEYS}, 'shipping_orders_delivered': 0,
        'cycle_id': 'c'}
    assert advancer.advance_invoice_states(_Conn(cursor), 'c') == {
        'transitions': 7, 'finance_sent_to_overdue': 7, 'cycle_id': 'c'}
    assert advancer.advance_order_states(_Conn(cursor), 'c') == {
        'transitions': 7, 'orders_pending_to_confirmed': 7, 'cycle_id': 'c'}


class _PendingConsumption:
    """Sorted COMPLETED work orders with no consumption rows; serves the start/wrap
    reads (or, unbounded, every one of them) and records each BOM lookup."""

    def __init__(self, ids):
        self.ids = sorted(ids)
        self.reads = []
        self.processed = []
        self._all = []

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self._all = []
        if 'FROM mes_work_orders mwo' in sql:
            self._all = [(i, i.upper(), 'p-1', 10, 10, None) for i in self._pending(sql, params)]
        elif 'FROM bill_of_materials bom' in sql:
            self.processed.append(params[0])

    def _pending(self, sql, params):
        if 'LIMIT %s' not in sql:
            self.reads.append(None)
            return self.ids
        start, limit = params
        before = 'mwo.work_order_id < %s' in sql
        self.reads.append(('before' if before else 'from', start, limit))
        hit = [i for i in self.ids if (i < start if before else i >= start)]
        return hit[:limit]

    def fetchall(self):
        return self._all

    def fetchone(self):
        return None

    def close(self):
        pass


def _mes():
    sim = MESSimulator.__new__(MESSimulator)          # skip __init__ (config, seed DB)
    sim._track_work_order_consumption = lambda cursor, wo: 0
    return sim


def _ids(n):
    return [f'{k:08x}-0000-0000-0000-000000000000' for k in range(1, n + 1)]


def test_consumption_reads_one_bounded_batch():
    bound = utils.MAX_ROWS_PER_CYCLE
    sim = _mes()
    seen = []
    sim._track_work_order_consumption = lambda cursor, wo: seen.append(wo[0]) or 0
    cursor = _PendingConsumption(_ids(bound * 5))
    sim.track_material_consumption(_Conn(cursor), 'cycle-1')
    assert len(seen) == bound
    assert cursor.reads and None not in cursor.reads           # every read carries a LIMIT
    assert cursor.reads[0][2] == bound
    assert sum(read[2] for read in cursor.reads[1:]) <= bound   # the wrap read takes what is left


def test_consumption_read_wraps_round_from_its_start(monkeypatch):
    monkeypatch.setattr(mes_simulator, 'MAX_ROWS_PER_CYCLE', 4, raising=False)
    ids = _ids(6)
    seen = []
    sim = _mes()
    sim._track_work_order_consumption = lambda cursor, wo: seen.append(wo[0]) or 0
    sim._consumption_scan_start = lambda: ids[4]
    cursor = _PendingConsumption(ids)
    sim.track_material_consumption(_Conn(cursor), 'cycle-1')
    assert cursor.reads == [('from', ids[4], 4), ('before', ids[4], 2)]
    assert seen == [ids[0], ids[1], ids[4], ids[5]]          # worked in work_order_id order


def test_consumption_scan_start_is_a_fresh_uuid_every_cycle():
    sim = _mes()
    starts = {sim._consumption_scan_start() for _ in range(20)}
    assert len(starts) == 20
    assert all(str(uuid.UUID(s)) == s for s in starts)


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise).
# --------------------------------------------------------------------------
_LIMIT = 3
_ZERO_UUID = '00000000-0000-0000-0000-000000000000'


@pytest.fixture
def small_batch(monkeypatch):
    # raising=False: mes_simulator only gains the name with the bounded read.
    monkeypatch.setattr(state_advancer, 'MAX_ROWS_PER_CYCLE', _LIMIT, raising=False)
    monkeypatch.setattr(mes_simulator, 'MAX_ROWS_PER_CYCLE', _LIMIT, raising=False)


def _customer_and_order(cur):
    cur.execute("SELECT customer_id FROM customers ORDER BY customer_code LIMIT 1")
    customer_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO customer_orders (order_number, customer_id, status)
        VALUES (%s, %s, 'SHIPPED') RETURNING order_id
    """, ('ORD-SBKLG-' + uuid.uuid4().hex[:8], customer_id))
    return customer_id, cur.fetchone()[0]


# (source status, target status, the timestamp the step waits on)
_SHIPMENT_STEPS = [
    ('PREPARING', 'PICKED', 'shipment_date'),
    ('PICKED', 'PACKED', 'updated_at'),
    ('PACKED', 'SHIPPED', 'updated_at'),
    ('SHIPPED', 'IN_TRANSIT', 'updated_at'),
    ('IN_TRANSIT', 'DELIVERED', 'updated_at'),
]


def _shipment_backlog(cur, source, own_clock, n=8):
    """Retire every seeded open shipment, then add ``n`` in ``source``: shipment
    ``i`` is ``10 + i`` minutes old on the step's own timestamp and the newest
    on the other one, so a batch taken by the wrong column picks different rows."""
    cur.execute("UPDATE shipments SET status = 'DELIVERED' WHERE status <> 'DELIVERED'")
    customer_id, order_id = _customer_and_order(cur)
    for i in range(n):
        own, other = f'{10 + i} minutes', f'{10 + n - i} minutes'
        shipment_date, updated_at = (own, other) if own_clock == 'shipment_date' else (other, own)
        cur.execute("""
            INSERT INTO shipments (shipment_number, order_id, customer_id, status,
                                   shipment_date, updated_at)
            VALUES (%s, %s, %s, %s, NOW() - %s::interval, NOW() - %s::interval)
        """, (f'SHIP-SBKLG-{i}', order_id, customer_id, source, shipment_date, updated_at))


def _numbers_in(cur, table, column, prefix, status):
    cur.execute(stmt("SELECT {col} FROM {tbl} WHERE {col} LIKE %s AND status = %s",
                     col=column, tbl=table), (prefix + '%', status))
    return {row[0] for row in cur.fetchall()}


@pytest.mark.parametrize('source, target, own_clock', _SHIPMENT_STEPS)
def test_pg_shipment_backlog_drains_oldest_first(pg_conn, small_batch,  # noqa: F811
                                                 source, target, own_clock):
    with pg_conn.cursor() as cur:
        _shipment_backlog(cur, source, own_clock)
    moved = []
    for _ in range(3):
        _advancer().advance_shipment_states(pg_conn, 'cycle-pg')
        with pg_conn.cursor() as cur:
            moved.append(_numbers_in(cur, 'shipments', 'shipment_number', 'SHIP-SBKLG-', target))
    oldest = [f'SHIP-SBKLG-{i}' for i in range(7, -1, -1)]
    assert moved == [set(oldest[:3]), set(oldest[:6]), set(oldest)]


def test_pg_overdue_backlog_drains_earliest_due_first(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE invoices SET status = 'PAID' WHERE status = 'SENT'")
        customer_id, order_id = _customer_and_order(cur)
        for i in range(8):
            cur.execute("""
                INSERT INTO invoices (invoice_number, customer_id, order_id, status,
                                      invoice_date, due_date, total_amount)
                VALUES (%s, %s, %s, 'SENT', NOW() - %s::interval, NOW() - %s::interval, 100)
            """, (f'INV-SBKLG-{i}', customer_id, order_id, f'{20 - i} days', f'{1 + i} days'))
    overdue = []
    for _ in range(3):
        _advancer().advance_invoice_states(pg_conn, 'cycle-pg')
        with pg_conn.cursor() as cur:
            overdue.append(_numbers_in(cur, 'invoices', 'invoice_number', 'INV-SBKLG-', 'OVERDUE'))
    earliest = [f'INV-SBKLG-{i}' for i in range(7, -1, -1)]
    assert overdue == [set(earliest[:3]), set(earliest[:6]), set(earliest)]


def test_pg_pending_backlog_drains_one_batch_per_cycle(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE customer_orders SET status = 'SHIPPED' WHERE status = 'PENDING'")
        cur.execute("SELECT customer_id FROM customers ORDER BY customer_code LIMIT 1")
        customer_id = cur.fetchone()[0]
        for i in range(8):
            cur.execute("""
                INSERT INTO customer_orders (order_number, customer_id, status, order_date)
                VALUES (%s, %s, 'PENDING', NOW() - %s::interval)
            """, (f'ORD-PBKLG-{i}', customer_id, f'{10 + i} minutes'))
    confirmed = []
    for _ in range(3):
        result = _advancer().advance_order_states(pg_conn, 'cycle-pg')
        with pg_conn.cursor() as cur:
            confirmed.append(len(_numbers_in(cur, 'customer_orders', 'order_number', 'ORD-PBKLG-',
                                             'CONFIRMED')))
    assert confirmed == [3, 6, 8]
    assert result['orders_pending_to_confirmed'] == 2


def _completed_work_orders(cur, n=8):
    """Retire every seeded COMPLETED work order, then add ``n`` COMPLETED ones (no
    consumption rows) of a product with an active BOM; returns their ids sorted
    and a second BOM product."""
    cur.execute("UPDATE mes_work_orders SET status = 'CANCELLED' WHERE status = 'COMPLETED'")
    cur.execute("SELECT product_id FROM bill_of_materials WHERE is_active "
                "GROUP BY product_id ORDER BY product_id LIMIT 2")
    product_id, other_product = (row[0] for row in cur.fetchall())
    cur.execute("""
        INSERT INTO production_runs (run_number, product_id, planned_quantity, status)
        VALUES (%s, %s, 10, 'COMPLETED') RETURNING run_id
    """, ('RUN-CBKLG-' + uuid.uuid4().hex[:8], product_id))
    run_id = cur.fetchone()[0]
    for i in range(n):
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id,
                                         planned_quantity, actual_quantity, status)
            VALUES (%s, %s, %s, 10, 10, 'COMPLETED')
        """, (f'WO-CBKLG-{i}', run_id, product_id))
    cur.execute("SELECT work_order_id::text FROM mes_work_orders "
                "WHERE work_order_number LIKE 'WO-CBKLG-%%' ORDER BY work_order_id")
    return [row[0] for row in cur.fetchall()], other_product


def _consumed(cur):
    cur.execute("""
        SELECT COUNT(DISTINCT mmc.work_order_id) FROM mes_material_consumption mmc
        JOIN mes_work_orders w ON w.work_order_id = mmc.work_order_id
        WHERE w.work_order_number LIKE 'WO-CBKLG-%%'
    """)
    return cur.fetchone()[0]


def test_pg_consumption_backlog_drains_one_batch_per_cycle(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        _completed_work_orders(cur)
    consumed = []
    for _ in range(3):
        MESSimulator.__new__(MESSimulator).track_material_consumption(pg_conn, 'cycle-pg')
        with pg_conn.cursor() as cur:
            consumed.append(_consumed(cur))
    assert consumed == [3, 6, 8]


def test_pg_work_orders_without_a_bom_do_not_starve_the_ones_behind_them(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        ids, no_bom_product = _completed_work_orders(cur)
        # The three lowest ids belong to a product with no active BOM: they never get a row.
        cur.execute("UPDATE bill_of_materials SET is_active = FALSE WHERE product_id = %s",
                    (no_bom_product,))
        cur.execute("UPDATE mes_work_orders SET product_id = %s WHERE work_order_id = ANY(%s::uuid[])",
                    (no_bom_product, ids[:_LIMIT]))
    starts = iter([_ZERO_UUID, ids[_LIMIT]])
    sim = MESSimulator.__new__(MESSimulator)
    sim._consumption_scan_start = lambda: next(starts)
    sim.track_material_consumption(pg_conn, 'cycle-pg')      # reads the three without a BOM
    with pg_conn.cursor() as cur:
        assert _consumed(cur) == 0
    sim.track_material_consumption(pg_conn, 'cycle-pg')      # a later cycle reaches the rest
    with pg_conn.cursor() as cur:
        assert _consumed(cur) == _LIMIT


_CONSUMPTION_ROWS_SQL = """
    SELECT w.work_order_number, ii.item_code, mmc.lot_number, mmc.planned_quantity, mmc.actual_quantity
    FROM mes_material_consumption mmc
    JOIN mes_work_orders w ON w.work_order_id = mmc.work_order_id
    JOIN inventory_items ii ON ii.item_id = mmc.item_id
    WHERE w.work_order_number LIKE 'WO-CBKLG-%%'
    ORDER BY 1, 2, 3
"""


def test_pg_below_the_bound_each_work_order_gets_the_same_consumption_from_any_scan_start(pg_conn):  # noqa: F811
    """Characterization: a backlog below the bound is read whole in one batch,
    and the batch is worked in ``work_order_id`` order whatever the random start,
    so each work order gets the same consumption (its random quantities) for the
    same random state: where the scan started never moves a draw from one work
    order to another."""
    with pg_conn.cursor() as cur:
        ids, _other = _completed_work_orders(cur)
        cur.execute("SAVEPOINT before_consumption")
    per_start = []
    for start in (_ZERO_UUID, ids[3], ids[6]):
        random.seed(1023)
        sim = MESSimulator.__new__(MESSimulator)
        sim._consumption_scan_start = lambda start=start: start
        assert sim.track_material_consumption(pg_conn, 'cycle-pg')['consumption_records_created'] > 0
        with pg_conn.cursor() as cur:
            cur.execute(_CONSUMPTION_ROWS_SQL)
            per_start.append(cur.fetchall())
            cur.execute("ROLLBACK TO SAVEPOINT before_consumption")
    assert len({row[0] for row in per_start[0]}) == len(ids)
    assert per_start[1] == per_start[0] and per_start[2] == per_start[0]


# --------------------------------------------------------------------------
# Each bounded read is served in order by an index: no sort over the backlog.
# --------------------------------------------------------------------------
def _statement(name):
    if name.startswith('shipment:'):
        return (state_advancer._SHIPMENT_STEP_SQL[name.split(':', 1)[1]],
                (utils.STORY_SHIPMENT_NUMBER_RE, utils.MAX_ROWS_PER_CYCLE))
    if name == 'consumption':
        return mes_simulator._CONSUMPTION_PENDING_FROM_SQL, (_ZERO_UUID, utils.MAX_ROWS_PER_CYCLE)
    if name == '_SHIPPED_ORDERS_TO_DELIVERED_SQL':
        return state_advancer._SHIPPED_ORDERS_TO_DELIVERED_SQL, (utils.STORY_ORDER_NUMBER_RE,
                                                                 utils.MAX_ROWS_PER_CYCLE)
    return getattr(state_advancer, name), (utils.MAX_ROWS_PER_CYCLE,)


@pytest.mark.parametrize('name, index', [
    ('shipment:PREPARING', 'idx_shipments_status_shipment_date'),
    ('shipment:PICKED', 'idx_shipments_status_updated'),
    ('shipment:IN_TRANSIT', 'idx_shipments_status_updated'),
    ('_SENT_TO_OVERDUE_SQL', 'idx_invoices_status_due'),
    ('_PENDING_TO_CONFIRMED_SQL', 'idx_customer_orders_status_order'),
    ('_SHIPPED_ORDERS_TO_DELIVERED_SQL', 'idx_customer_orders_status_date'),
    ('consumption', 'mes_work_orders_pkey'),
])
def test_pg_the_bounded_read_walks_an_index_without_a_sort(pg_conn, name, index):  # noqa: F811
    from erp_simulation.pgdriver import sql as psql

    sql, params = _statement(name)
    try:
        with pg_conn.cursor() as cur:
            # Sorting and seq scans priced out: a plan still sorting has no index to walk.
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("SET LOCAL enable_sort = off")
            cur.execute(stmt("EXPLAIN {query}", query=psql.SQL(sql)), params)
            plan = "\n".join(row[0] for row in cur.fetchall())
    finally:
        pg_conn.rollback()
    assert index in plan and 'Sort' not in plan, plan
