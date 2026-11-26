# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every FG location — including the machining site's IND-FG — gates orders and shipping.

HEAD inlined a four-code allowlist that omitted IND-FG, so a Machined Components
order skipped its stock check (straight to READY_TO_SHIP) and shipping never drew
IND-FG down. The allowlist is now the derived ``FG_LOCATION_CODES`` bound as
``= ANY(%s)``.
"""
from __future__ import annotations

import random
import re
import uuid
from pathlib import Path

import pytest

from erp_simulation import production_simulator
from erp_simulation.production_simulator import (
    DEFAULT_FG_LOCATION, FG_LOCATION_CODES, SITE_FG_LOCATION,
)
from erp_simulation.shipping_simulator import ShippingSimulator
from erp_simulation.state_advancer import StateAdvancer
from erp_simulation.tests import test_fg_availability_characterization as ch
from erp_simulation.tests.test_fg_location_routing import pg_conn  # noqa: F401  (fixture)

_IND_SITE = 'INDIANAPOLIS_MACHINING'
_IND_FG = SITE_FG_LOCATION[_IND_SITE]
_PKG = Path(production_simulator.__file__).resolve().parent


def test_fg_location_codes_is_derived_from_the_one_map():
    assert FG_LOCATION_CODES == tuple(sorted({*SITE_FG_LOCATION.values(), DEFAULT_FG_LOCATION}))
    assert _IND_FG in FG_LOCATION_CODES
    assert ch.LEGACY_FG < set(FG_LOCATION_CODES)


@pytest.mark.parametrize('module', ['state_advancer.py', 'shipping_simulator.py'])
def test_no_inline_fg_location_allowlist(module):
    source = (_PKG / module).read_text()
    assert not re.search(r"location_code\s+IN\s*\(", source)
    assert 'location_code = ANY(%s)' in source


# --------------------------------------------------------------------------
# Scripted cursor (the characterization fixtures plus one IND product).
# --------------------------------------------------------------------------
@pytest.fixture
def ind_fixtures(monkeypatch):
    monkeypatch.setitem(ch._BALANCES, 'p-ind', [(_IND_FG, 50)])
    monkeypatch.setitem(ch._LINES, 'o-ind-short', [('li-i1', 'p-ind', 100, 'IND-SKU')])
    monkeypatch.setitem(ch._LINES, 'o-ind-ok', [('li-i2', 'p-ind', 40, 'IND-SKU')])
    monkeypatch.setattr(ch, '_CONFIRMED', [('o-ind-short', 'O-IND-SHORT'), ('o-ind-ok', 'O-IND-OK')])
    monkeypatch.setattr(ch, '_BACKORDERED', [('o-ind-short', 'O-IND-SHORT')])
    monkeypatch.setattr(ch, '_READY', [('o-ind-ok', 'O-IND-OK', 'cust-1')])


def _statuses(cursor):
    return [(p[0], sql.split("status = '")[1].split("'")[0])
            for sql, p in cursor.calls if sql.startswith('UPDATE customer_orders')]


def test_ind_order_is_stock_checked(ind_fixtures):
    result, backorder_calls, cursor = ch.run_production_states()
    assert _statuses(cursor) == [('o-ind-short', 'BACKORDERED'), ('o-ind-ok', 'READY_TO_SHIP')]
    assert backorder_calls == [('o-ind-short', 'O-IND-SHORT', [('p-ind', 100, 50)])]
    assert result['orders_backordered_to_ready'] == 0
    assert all(s == frozenset(FG_LOCATION_CODES) for s in cursor.fg_sets)


def test_ind_backorder_stays_until_stocked(ind_fixtures):
    result, _ = ch.run_backorder_states()
    assert result['transitions'] == 0


def test_shipping_ind_order_draws_ind_fg(ind_fixtures, monkeypatch):
    result, cursor = ch.run_shipments(monkeypatch)
    assert result['shipments_created'] == 1
    update = next(p for sql, p in cursor.calls if sql.startswith('UPDATE inventory_balances'))
    assert (update[0], update[2:]) == (40, ('item-p-ind', 'loc-' + _IND_FG))


# --------------------------------------------------------------------------
# Real PostgreSQL (ERP_SIM_TEST_PG_DSN; skipped otherwise).
# --------------------------------------------------------------------------
_FG_BALANCE_SQL = """
    SELECT ii.product_id, ib.quantity_on_hand
    FROM inventory_balances ib
    JOIN inventory_items ii ON ii.item_id = ib.item_id AND ii.item_type = 'FINISHED_GOOD'
    JOIN inventory_locations il ON il.location_id = ib.location_id
    WHERE il.location_code = %s
    ORDER BY ii.product_id
"""


def _fg_balance(cur, location_code, product_id):
    cur.execute(_FG_BALANCE_SQL, (location_code,))
    return {pid: float(q) for pid, q in cur.fetchall()}.get(product_id)


def _total_fg(cur, product_id):
    return sum(_fg_balance(cur, code, product_id) or 0 for code in FG_LOCATION_CODES)


def _site_product(cur, site_code):
    cur.execute("""
        SELECT p.product_id FROM products p
        JOIN inventory_items ii ON ii.product_id = p.product_id AND ii.item_type = 'FINISHED_GOOD'
        JOIN product_site_routing psr ON psr.product_category = p.category AND psr.is_active = TRUE
        JOIN sites s ON s.site_id = psr.site_id AND s.site_code = %s
        WHERE p.is_active = TRUE ORDER BY p.sku LIMIT 1
    """, (site_code,))
    row = cur.fetchone()
    assert row, f'seed must route a finished good to {site_code}'
    return row[0]


def _confirmed_order(cur, product_id, quantity, status='CONFIRMED'):
    """Insert a single-line order already past the 2-minute confirmation window."""
    cur.execute("SELECT customer_id FROM customers ORDER BY customer_code LIMIT 1")
    customer_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO customer_orders (order_number, customer_id, status, updated_at)
        VALUES (%s, %s, %s, NOW() - INTERVAL '10 minutes') RETURNING order_id
    """, ('ORD-FIXA-' + uuid.uuid4().hex[:8], customer_id, status))
    order_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO order_line_items (order_id, line_number, product_id, quantity, unit_price)
        VALUES (%s, 1, %s, %s, 10)
    """, (order_id, product_id, quantity))
    cur.execute("UPDATE production_runs SET status = 'COMPLETED' WHERE product_id = %s", (product_id,))
    return order_id


def _order_status(cur, order_id):
    cur.execute("SELECT status FROM customer_orders WHERE order_id = %s", (order_id,))
    return cur.fetchone()[0]


def _order_runs(cur, order_id):
    cur.execute("""
        SELECT il.location_code, pr.site FROM production_runs pr
        JOIN inventory_locations il ON il.location_id = pr.location_id
        WHERE pr.order_id = %s AND pr.status = 'PLANNED'
    """, (order_id,))
    return cur.fetchall()


def _advance(conn):
    StateAdvancer.__new__(StateAdvancer).advance_production_states(conn, 'cycle-pg')


@pytest.mark.parametrize('site_code', [_IND_SITE, 'MILWAUKEE_BOTTLING', 'DETROIT_AUTOPARTS',
                                       'CEDARRAPIDS_FOOD'])
def test_pg_short_order_backorders_and_triggers_production(pg_conn, site_code):  # noqa: F811
    with pg_conn.cursor() as cur:
        product_id = _site_product(cur, site_code)
        order_id = _confirmed_order(cur, product_id, int(_total_fg(cur, product_id)) + 50)
    _advance(pg_conn)
    with pg_conn.cursor() as cur:
        assert _order_status(cur, order_id) == 'BACKORDERED'
        assert _order_runs(cur, order_id) == [(SITE_FG_LOCATION[site_code], site_code)]


def test_pg_stocked_ind_order_is_ready_to_ship(pg_conn):  # noqa: F811
    with pg_conn.cursor() as cur:
        product_id = _site_product(cur, _IND_SITE)
        on_hand = _fg_balance(cur, _IND_FG, product_id)
        assert on_hand and on_hand >= 2
        order_id = _confirmed_order(cur, product_id, int(on_hand) // 2)
    _advance(pg_conn)
    with pg_conn.cursor() as cur:
        assert _order_status(cur, order_id) == 'READY_TO_SHIP'
        assert _order_runs(cur, order_id) == []


def test_pg_shipping_ind_order_reduces_ind_fg(pg_conn):  # noqa: F811
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE customer_orders SET status = 'SHIPPED' WHERE status = 'READY_TO_SHIP'")
        product_id = _site_product(cur, _IND_SITE)
        before = _fg_balance(cur, _IND_FG, product_id)
        order_id = _confirmed_order(cur, product_id, 7, status='READY_TO_SHIP')
    pg_conn.commit()
    random.seed(3)
    sim = ShippingSimulator.__new__(ShippingSimulator)
    sim.carriers = ['AnyCompany Freight', 'AnyCompany Logistics', 'AnyCompany Parcel']
    assert sim.create_shipments(pg_conn, 'cycle-pg')['shipments_created'] == 1
    with pg_conn.cursor() as cur:
        assert _order_status(cur, order_id) == 'SHIPPED'
        assert _fg_balance(cur, _IND_FG, product_id) == before - 7
        cur.execute("""
            SELECT il.location_code, it.quantity FROM inventory_transactions it
            JOIN inventory_locations il ON il.location_id = it.location_id
            WHERE it.reference_type = 'SHIPMENT' AND it.item_id IN (
                SELECT item_id FROM inventory_items WHERE product_id = %s)
        """, (product_id,))
        assert [(code, float(q)) for code, q in cur.fetchall()] == [(_IND_FG, -7.0)]
