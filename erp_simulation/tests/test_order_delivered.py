# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shipment delivery step delivers non-story orders and never the story.

``advance_shipment_states`` (1) never advances a story-owned shipment through
the ladder, and (2) runs a bounded per-cycle catch-up that moves every SHIPPED,
NON-story order whose shipments are all delivered to DELIVERED - the current
cycle's AND any pre-existing backlog. Story-owned orders/shipments are excluded
by number (``utils.STORY_ORDER_NUMBER_RE`` / ``STORY_SHIPMENT_NUMBER_RE``), so
the live story's deliberately-SHIPPED order (ADR-0085: two DELIVERED, one
SHIPPED) stays exactly as written across cycles. The count is reported under
``shipping_orders_delivered``.
"""
from __future__ import annotations

import os
import re
import uuid

import pytest

from erp_simulation import state_advancer, story_history as sh, utils
from erp_simulation.state_advancer import StateAdvancer

_DSN = os.environ.get('ERP_SIM_TEST_PG_DSN')


# --------------------------------------------------------------------------
# Number-pattern coverage (runs in every lane; no DB).
# --------------------------------------------------------------------------
def test_story_patterns_match_every_planned_story_number_and_no_simulator_number():
    """``utils`` story patterns match every order/shipment story_history plans
    (``_orders`` and ``_run_order``) and no simulator number (ADR-0090: orders
    0..39,999,999 and shipments 40,000,000..79,999,999 keep the simulator's
    8-digit field at 0..7, below 80,000,000). The
    shared order pattern equals the one ``story_replan`` uses to retire the
    story's orders, so the ladder excludes exactly what the re-plan does."""
    from erp_simulation import story_replan
    assert story_replan.HISTORY_ORDER_PATTERN == utils.STORY_ORDER_NUMBER_RE
    order_re = re.compile(utils.STORY_ORDER_NUMBER_RE)
    ship_re = re.compile(utils.STORY_SHIPMENT_NUMBER_RE)
    planned = list(sh.plan(1_700_000_000.0, sh.MAX_DAYS).orders) + \
        list(sh.plan(1_700_000_000.0, sh.MAX_DAYS).run_orders)
    assert planned, 'the plan must write orders'
    for o in planned:
        assert order_re.match(o.order_number), o.order_number
        assert ship_re.match(o.shipment_number), o.shipment_number
    # The simulator's own numbers (8-digit field 0..7) never match.
    for n in ('ORD-20260101-00001234', 'ORD-20260101-39999999'):
        assert not order_re.match(n), n
    for n in ('SHIP-20260101-40000000', 'SHIP-20260101-79999999'):
        assert not ship_re.match(n), n


# --------------------------------------------------------------------------
# Real PostgreSQL: two databases (ERP + seed) for the story writer, reusing
# the story-replan suite's fixture.
# --------------------------------------------------------------------------
from erp_simulation.tests.test_story_replan import pg  # noqa: E402,F401  (fixture)
# One ERP database for the backlog-drain tests, reusing the routing suite's.
from erp_simulation.tests.test_fg_location_routing import pg_conn  # noqa: E402,F401  (fixture)


def _status(conn, order_number):
    with conn.cursor() as cur:
        cur.execute('SELECT status FROM customer_orders WHERE order_number = %s', (order_number,))
        return cur.fetchone()[0]


def _ship_row(conn, shipment_number):
    with conn.cursor() as cur:
        cur.execute('SELECT status, actual_delivery_date, updated_at FROM shipments '
                    'WHERE shipment_number = %s', (shipment_number,))
        return cur.fetchone()


def _age_all_shipments(conn, minutes=30):
    """Age every shipment so each ladder wait (<= 2 min) is satisfied."""
    with conn.cursor() as cur:
        cur.execute("UPDATE shipments SET shipment_date = NOW() - %s::interval, "
                    "updated_at = NOW() - %s::interval", (f'{minutes} minutes', f'{minutes} minutes'))
    conn.commit()


def test_story_shipped_order_and_shipment_survive_many_cycles(pg):
    """The live story's deliberately-SHIPPED order AND its shipment stay exactly
    as written through several cycles past every ladder wait; the two DELIVERED
    story orders are untouched too. The story shipments are written dated near
    the (2023) anchor, so they already satisfy every ladder wait - if the ladder
    did not exclude them by number it would walk the SHIPPED one to DELIVERED."""
    sp = sh.plan(1_700_000_000.0, 3)
    sh.write(pg['erp'], pg['seed'], sp)
    pg['erp'].commit()
    shipped = next(o for o in sp.orders if o.status == 'SHIPPED')
    delivered = [o for o in sp.orders if o.status == 'DELIVERED']
    before = _ship_row(pg['erp'], shipped.shipment_number)
    assert before[0] == 'SHIPPED' and before[1] is None
    advancer = StateAdvancer()
    for _ in range(4):
        advancer.advance_shipment_states(pg['erp'], 'cyc')
        pg['erp'].commit()
    # The SHIPPED story order and its shipment are byte-for-byte as written
    # (status, actual_delivery_date AND updated_at unchanged).
    assert _status(pg['erp'], shipped.order_number) == 'SHIPPED'
    assert _ship_row(pg['erp'], shipped.shipment_number) == before
    # The DELIVERED story orders are left as the story wrote them.
    for o in delivered:
        assert _status(pg['erp'], o.order_number) == 'DELIVERED'


def test_already_walked_story_shipment_leaves_the_order_shipped(pg):
    """An account where the pre-fix ladder already walked the story's SHIPPED
    shipment to DELIVERED (today's admin-account state): the story order still stays
    SHIPPED, because the catch-up excludes it by number."""
    sp = sh.plan(1_700_000_000.0, 3)
    sh.write(pg['erp'], pg['seed'], sp)
    shipped = next(o for o in sp.orders if o.status == 'SHIPPED')
    with pg['erp'].cursor() as cur:
        cur.execute("UPDATE shipments SET status = 'DELIVERED', actual_delivery_date = NOW() "
                    "WHERE shipment_number = %s", (shipped.shipment_number,))
    pg['erp'].commit()
    _age_all_shipments(pg['erp'])
    StateAdvancer().advance_shipment_states(pg['erp'], 'cyc')
    pg['erp'].commit()
    assert _status(pg['erp'], shipped.order_number) == 'SHIPPED'


# --------------------------------------------------------------------------
# Backlog drain (single ERP database).
# --------------------------------------------------------------------------
def _customer(cur):
    cur.execute('SELECT customer_id FROM customers ORDER BY customer_code LIMIT 1')
    return cur.fetchone()[0]


def _sim_order(cur, customer_id, i, status='SHIPPED'):
    """A simulator-numbered (non-story) order ORD-...-0NNNNNNN."""
    number = f'ORD-20260101-0000{i:04d}'
    cur.execute('INSERT INTO customer_orders (order_number, customer_id, status) '
                'VALUES (%s, %s, %s) RETURNING order_id', (number, customer_id, status))
    return number, cur.fetchone()[0]


def _sim_shipment(cur, order_id, customer_id, i, status='DELIVERED'):
    cur.execute('INSERT INTO shipments (shipment_number, order_id, customer_id, status, '
                'shipment_date, updated_at) VALUES (%s, %s, %s, %s, NOW(), NOW())',
                (f'SHIP-20260101-0000{i:04d}', order_id, customer_id, status))


def test_stale_backlog_of_delivered_orders_drains(pg_conn):
    """Pre-existing SHIPPED orders whose shipments are all DELIVERED (left
    SHIPPED before this step existed) are delivered - not only this cycle's."""
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE shipments SET status = 'DELIVERED' WHERE status <> 'DELIVERED'")
        customer_id = _customer(cur)
        number, oid = _sim_order(cur, customer_id, 1)
        _sim_shipment(cur, oid, customer_id, 1, 'DELIVERED')
        result = StateAdvancer().advance_shipment_states(pg_conn, 'cyc')
        assert _status(pg_conn, number) == 'DELIVERED'
        assert result['shipping_orders_delivered'] == 1


def test_backlog_drains_across_cycles_bounded_and_in_flight_does_not_block(pg_conn, monkeypatch):
    """With the per-cycle limit set to 2 and five fully-delivered SHIPPED orders
    plus one with an in-flight shipment: the five drain across cycles (<= 2 per
    cycle), the in-flight one is never moved and never blocks the others, and
    nothing is double-counted in a cycle."""
    monkeypatch.setattr(state_advancer, 'MAX_ROWS_PER_CYCLE', 2, raising=False)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE shipments SET status = 'DELIVERED' WHERE status <> 'DELIVERED'")
        customer_id = _customer(cur)
        ready = []
        for i in range(1, 6):
            number, oid = _sim_order(cur, customer_id, i)
            _sim_shipment(cur, oid, customer_id, i, 'DELIVERED')
            ready.append(number)
        # One order with a shipment still in flight: must never move.
        in_flight, oid = _sim_order(cur, customer_id, 99)
        _sim_shipment(cur, oid, customer_id, 991, 'DELIVERED')
        _sim_shipment(cur, oid, customer_id, 992, 'IN_TRANSIT')
    total = 0
    for _ in range(4):
        result = StateAdvancer().advance_shipment_states(pg_conn, 'cyc')
        moved = result['shipping_orders_delivered']
        assert moved <= 2                       # bounded per cycle
        total += moved
        pg_conn.commit()
    assert total == 5                            # all five drained, none double-counted
    assert all(_status(pg_conn, n) == 'DELIVERED' for n in ready)
    assert _status(pg_conn, in_flight) == 'SHIPPED'


def test_catch_up_takes_the_oldest_order_first_not_the_lowest_id(pg_conn, monkeypatch):
    """With one order per cycle, the order placed earliest moves first even when
    its id sorts last: order ids are random, so an id order would let new orders
    keep displacing an old one whenever the batch is full."""
    monkeypatch.setattr(state_advancer, 'MAX_ROWS_PER_CYCLE', 1, raising=False)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE shipments SET status = 'DELIVERED' WHERE status <> 'DELIVERED'")
        customer_id = _customer(cur)
        placed = (('ORD-20200101-00000001', 'ffffffff-ffff-4fff-bfff-ffffffffffff', '2020-01-01'),
                  ('ORD-20260101-00000002', '00000000-0000-4000-8000-000000000001', '2026-01-01'))
        for i, (number, order_id, order_date) in enumerate(placed, 1):
            cur.execute('INSERT INTO customer_orders (order_id, order_number, customer_id, status, '
                        'order_date) VALUES (%s, %s, %s, %s, %s)',
                        (order_id, number, customer_id, 'SHIPPED', order_date))
            _sim_shipment(cur, order_id, customer_id, 500 + i, 'DELIVERED')
    moved = []
    for _ in range(2):
        StateAdvancer().advance_shipment_states(pg_conn, 'cyc')
        pg_conn.commit()
        moved.append([n for n, _id, _d in placed if _status(pg_conn, n) == 'DELIVERED'])
    assert moved == [['ORD-20200101-00000001'],
                     ['ORD-20200101-00000001', 'ORD-20260101-00000002']]


def test_order_with_no_shipment_is_not_delivered(pg_conn):
    """A SHIPPED order with no shipment at all is never delivered (EXISTS guard)."""
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE shipments SET status = 'DELIVERED' WHERE status <> 'DELIVERED'")
        customer_id = _customer(cur)
        number, _oid = _sim_order(cur, customer_id, 7)   # no shipment
        result = StateAdvancer().advance_shipment_states(pg_conn, 'cyc')
        assert _status(pg_conn, number) == 'SHIPPED'
        assert result['shipping_orders_delivered'] == 0
