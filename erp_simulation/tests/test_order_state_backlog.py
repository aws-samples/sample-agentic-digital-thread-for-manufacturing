# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The order-state steps read one bounded batch per cycle.

``advance_production_states`` (CONFIRMED and BACKORDERED orders) and
``advance_backorder_states`` used to read every order in that status at once.
A demo account whose cycles had been timing out for months held hundreds of
thousands of CONFIRMED orders, so the first cycle that reached the step read
them all and ran the 512 MB Lambda out of memory every time. Each read now
takes at most ``MAX_ROWS_PER_CYCLE`` orders, in ``order_id`` order (the
``(status, order_id)`` hot-path index), and the rest wait for later cycles.
The BACKORDERED read starts at a fresh random point each cycle and wraps
round, so backorders that stay short cannot hide the ones behind them.
"""
from __future__ import annotations

import uuid

import pytest

from erp_simulation import state_advancer, utils
from erp_simulation.state_advancer import StateAdvancer
from erp_simulation.tests.test_fg_location_routing import pg_conn  # noqa: F401  (fixture)
from erp_simulation.tests.test_fg_order_flow import _IND_FG, _IND_SITE, _fg_balance, _site_product


# --------------------------------------------------------------------------
# Scripted cursor (runs in every lane): a status read returns `backlog` rows,
# or only as many as a bound LIMIT asks for.
# --------------------------------------------------------------------------
class _BacklogCursor:
    def __init__(self, backlog):
        self.backlog = backlog
        self.status_reads = []
        self.availability_reads = 0
        self._all = []

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self._all = []
        for status in ('CONFIRMED', 'BACKORDERED'):
            if f"co.status = '{status}'" in sql and sql.startswith('SELECT'):
                limit = params[-1] if 'LIMIT %s' in sql else None
                self.status_reads.append((status, limit))
                n = self.backlog if limit is None else min(self.backlog, limit)
                self._all = [(f'{status[:1]}-{i}', f'ORD-{i}') for i in range(n)]
                return
        if 'as available_inventory' in sql:
            self.availability_reads += 1
            self._all = [('p-1', 1, 0)]   # short: CONFIRMED -> BACKORDERED, BACKORDERED stays

    def fetchall(self):
        return self._all

    def fetchone(self):
        return None

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _advancer():
    adv = StateAdvancer.__new__(StateAdvancer)        # skip get_config()
    adv._create_production_for_backorder = lambda *a: None
    return adv


def test_production_states_reads_one_bounded_batch_per_status():
    cursor = _BacklogCursor(backlog=utils.MAX_ROWS_PER_CYCLE * 5)
    result = _advancer().advance_production_states(_Conn(cursor), 'cycle-1')
    assert cursor.status_reads == [('CONFIRMED', utils.MAX_ROWS_PER_CYCLE),
                                   ('BACKORDERED', utils.MAX_ROWS_PER_CYCLE)]
    assert cursor.availability_reads == 2 * utils.MAX_ROWS_PER_CYCLE
    assert result['orders_confirmed_to_backordered'] == utils.MAX_ROWS_PER_CYCLE


def test_backorder_states_reads_one_bounded_batch():
    cursor = _BacklogCursor(backlog=utils.MAX_ROWS_PER_CYCLE * 5)
    _advancer().advance_backorder_states(_Conn(cursor), 'cycle-1')
    assert cursor.status_reads == [('BACKORDERED', utils.MAX_ROWS_PER_CYCLE)]
    assert cursor.availability_reads == utils.MAX_ROWS_PER_CYCLE


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise).
# --------------------------------------------------------------------------
_LIMIT = 3


def _stocked_orders(cur, status, n):
    """Retire every seeded order in ``status``, then add ``n`` one-unit orders of a stocked product."""
    cur.execute("UPDATE customer_orders SET status = 'SHIPPED' WHERE status IN ('CONFIRMED', 'BACKORDERED')")
    product_id = _site_product(cur, _IND_SITE)
    assert (_fg_balance(cur, _IND_FG, product_id) or 0) >= n
    cur.execute("SELECT customer_id FROM customers ORDER BY customer_code LIMIT 1")
    customer_id = cur.fetchone()[0]
    for _ in range(n):
        cur.execute("""
            INSERT INTO customer_orders (order_number, customer_id, status, updated_at)
            VALUES (%s, %s, %s, NOW() - INTERVAL '10 minutes') RETURNING order_id
        """, ('ORD-BKLG-' + uuid.uuid4().hex[:8], customer_id, status))
        order_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price)
            VALUES (%s, 1, %s, 1, 10)
        """, (order_id, product_id))


def _count(cur, status):
    cur.execute("SELECT COUNT(*) FROM customer_orders WHERE order_number LIKE 'ORD-BKLG-%%' "
                "AND status = %s", (status,))
    return cur.fetchone()[0]


@pytest.fixture
def small_batch(monkeypatch):
    # raising=False: the module only gains the name with the bounded reads.
    monkeypatch.setattr(state_advancer, 'MAX_ROWS_PER_CYCLE', _LIMIT, raising=False)


def test_pg_confirmed_backlog_drains_one_batch_per_cycle(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        _stocked_orders(cur, 'CONFIRMED', 8)
    first = _advancer().advance_production_states(pg_conn, 'cycle-pg')
    with pg_conn.cursor() as cur:
        assert (_count(cur, 'READY_TO_SHIP'), _count(cur, 'CONFIRMED')) == (3, 5)
    assert first['orders_confirmed_to_ready'] == 3
    _advancer().advance_production_states(pg_conn, 'cycle-pg')
    with pg_conn.cursor() as cur:
        assert (_count(cur, 'READY_TO_SHIP'), _count(cur, 'CONFIRMED')) == (6, 2)


def test_pg_backordered_backlog_drains_one_batch_per_cycle(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        _stocked_orders(cur, 'BACKORDERED', 8)
    _advancer().advance_backorder_states(pg_conn, 'cycle-pg')
    with pg_conn.cursor() as cur:
        assert (_count(cur, 'READY_TO_SHIP'), _count(cur, 'BACKORDERED')) == (3, 5)
    result = _advancer().advance_production_states(pg_conn, 'cycle-pg')
    with pg_conn.cursor() as cur:
        assert (_count(cur, 'READY_TO_SHIP'), _count(cur, 'BACKORDERED')) == (6, 2)
    assert result['orders_backordered_to_ready'] == 3


# --------------------------------------------------------------------------
# A backorder that stays short must not hide the ones behind it.
# --------------------------------------------------------------------------
_ZERO_UUID = '00000000-0000-0000-0000-000000000000'


def test_backorder_scan_start_is_a_fresh_uuid_every_cycle():
    adv = _advancer()
    starts = {adv._backorder_scan_start() for _ in range(20)}
    assert len(starts) == 20
    assert all(str(uuid.UUID(s)) == s for s in starts)


class _OrderedBackorders:
    """Scripted cursor over sorted BACKORDERED ids; serves the start/wrap reads."""

    def __init__(self, ids):
        self.ids = sorted(ids)
        self.reads = []
        self.checked = []
        self._all = []

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self._all = []
        if "co.status = 'BACKORDERED'" in sql:
            start, limit = params
            if 'co.order_id >= %s::uuid' in sql:
                hit = [i for i in self.ids if i >= start]
            else:
                hit = [i for i in self.ids if i < start]
            self.reads.append(('from' if '>=' in sql else 'before', start, limit))
            self._all = [(i, i.upper()) for i in hit[:limit]]
        elif 'as available_inventory' in sql:
            self.checked.append(params[0])
            self._all = [('p-1', 1, 0)]

    def fetchall(self):
        return self._all

    def close(self):
        pass


def test_backordered_read_wraps_round_from_its_start(monkeypatch):
    monkeypatch.setattr(state_advancer, 'MAX_ROWS_PER_CYCLE', 4, raising=False)
    ids = [f'{n:08x}-0000-0000-0000-000000000000' for n in range(1, 7)]   # 6 backorders
    cursor = _OrderedBackorders(ids)
    adv = _advancer()
    adv._backorder_scan_start = lambda: ids[4]
    adv.advance_backorder_states(_Conn(cursor), 'cycle-1')
    assert cursor.reads == [('from', ids[4], 4), ('before', ids[4], 2)]
    assert cursor.checked == [ids[4], ids[5], ids[0], ids[1]]


def test_pg_short_backorders_do_not_starve_the_ones_behind_them(pg_conn, small_batch):  # noqa: F811
    with pg_conn.cursor() as cur:
        _stocked_orders(cur, 'BACKORDERED', 8)
        cur.execute("SELECT order_id::text FROM customer_orders WHERE order_number LIKE 'ORD-BKLG-%%' "
                    "ORDER BY order_id")
        ids = [row[0] for row in cur.fetchall()]
        # The three lowest ids can never be filled.
        cur.execute("UPDATE order_line_items SET quantity = 1000000000 WHERE order_id = ANY(%s::uuid[])",
                    (ids[:_LIMIT],))
    starts = iter([_ZERO_UUID, ids[_LIMIT]])
    adv = _advancer()
    adv._backorder_scan_start = lambda: next(starts)
    adv.advance_backorder_states(pg_conn, 'cycle-pg')       # reads the three short ones
    with pg_conn.cursor() as cur:
        assert _count(cur, 'READY_TO_SHIP') == 0
    adv.advance_backorder_states(pg_conn, 'cycle-pg')       # a later cycle reaches the rest
    with pg_conn.cursor() as cur:
        assert (_count(cur, 'READY_TO_SHIP'), _count(cur, 'BACKORDERED')) == (3, 5)
