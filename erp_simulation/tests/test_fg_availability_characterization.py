# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization of the FG-availability order transitions and shipping.

Pins the SQL statement/param sequence and results of
``StateAdvancer.advance_production_states``, ``StateAdvancer.advance_backorder_states``
and ``ShippingSimulator.create_shipments`` as they were BEFORE decomposition, for
orders on the three legacy sites (MKE / DET / CR) plus the FG-WAREHOUSE default.

The FG-location predicate is the one intentional difference: HEAD inlined a
literal ``IN (...)`` list, the decomposed code binds ``= ANY(%s)``. Both forms are
normalised to ``<FG_LOCS>`` (the bound list is dropped from params) and the
scripted cursor evaluates whichever form it sees, so the legacy scenarios below
must produce identical traces either way. This file must stay runnable against
the HEAD sources (it imports nothing added by the fix).
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime

import pytest

from erp_simulation import shipping_simulator, utils
from erp_simulation.shipping_simulator import ShippingSimulator
from erp_simulation.state_advancer import StateAdvancer

LEGACY_FG = {'FG-WAREHOUSE', 'MKE-FG', 'DET-FG', 'CR-FG'}

# product_id -> [(location_code, quantity_on_hand)]; 'p-nofg' has no FG item.
_BALANCES = {
    'p-mke': [('MKE-FG', 300)],
    'p-det': [('DET-FG', 100), ('FG-WAREHOUSE', 50)],
    'p-cr': [],                                     # FG item, no balance rows
    'p-raw': [('MKE-WH-RAW', 999)],                 # only a non-FG balance
    'p-fgw': [('FG-WAREHOUSE', 40), ('CR-FG', 25)],
    'p-nofg': None,
}

# order_id -> [(line_item_id, product_id, quantity, sku)]
_LINES = {
    'o-ready': [('li-1', 'p-mke', 100, 'MKE-SKU')],
    'o-short': [('li-2', 'p-det', 500, 'DET-SKU')],
    'o-empty-bal': [('li-3', 'p-cr', 10, 'CR-SKU')],
    'o-mixed': [('li-4', 'p-mke', 50, 'MKE-SKU'), ('li-5', 'p-det', 200, 'DET-SKU')],
    'o-raw-only': [('li-6', 'p-raw', 10, 'RAW-SKU')],
    'o-no-lines': [],
    'o-nofg': [('li-7', 'p-nofg', 5, 'NOFG-SKU')],
    'b-now-ok': [('li-8', 'p-det', 120, 'DET-SKU')],
    'b-still-short': [('li-9', 'p-fgw', 100, 'FGW-SKU')],
    'b-no-lines': [],
    's-split': [('li-10', 'p-fgw', 30, 'FGW-SKU'), ('li-11', 'p-det', 120, 'DET-SKU')],
    's-fail': [('li-12', 'p-mke', 1, 'MKE-SKU')],
    's-misc': [('li-13', 'p-nofg', 2, 'NOFG-SKU'), ('li-14', 'p-cr', 3, 'CR-SKU'),
               ('li-15', 'p-raw', 4, 'RAW-SKU')],
}

_CONFIRMED = [(o, o.upper()) for o in ('o-ready', 'o-short', 'o-empty-bal', 'o-mixed',
                                       'o-raw-only', 'o-no-lines', 'o-nofg')]
_BACKORDERED = [(o, o.upper()) for o in ('b-now-ok', 'b-still-short', 'b-no-lines')]
_READY = [(o, o.upper(), 'cust-' + o) for o in ('s-split', 's-fail', 's-misc', 'o-no-lines')]
_TRACEABLE = {'p-det': 'RUN-DET-1'}

_IN_RE = re.compile(r"il\.location_code IN \(((?:'[A-Z-]+'(?:, )?)+)\)")
_ANY = 'il.location_code = ANY(%s)'


def _fg_set(sql, params):
    """The FG-location allowlist a statement filters on (literal or bound), else None."""
    m = _IN_RE.search(sql)
    if m:
        return set(re.findall(r"'([A-Z-]+)'", m.group(1)))
    if _ANY in sql:
        return set(next(p for p in params if isinstance(p, (list, tuple))))
    return None


class _Cursor:
    """Scripted cursor over the fixtures above; records every call."""

    def __init__(self, fail_orders=()):
        self.fail_orders = set(fail_orders)
        self.calls = []
        self.fg_sets = []
        self._one, self._all = None, []
        self._n = 0

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self.calls.append((sql, params))
        self._one, self._all = None, []
        locs = _fg_set(sql, params)
        if locs is not None:
            self.fg_sets.append(frozenset(locs))
        if "co.status = 'CONFIRMED'" in sql:
            self._all = list(_CONFIRMED)
        elif "co.status = 'BACKORDERED'" in sql and 'co.order_id < %s' in sql:
            self._all = []               # the wrap-round read: nothing sorts below the pinned start
        elif "co.status = 'BACKORDERED'" in sql:
            self._all = list(_BACKORDERED)
        elif "co.status = 'READY_TO_SHIP'" in sql:
            self._all = list(_READY)
        elif 'as available_inventory' in sql:
            self._all = self._availability(params[0], locs)
        elif sql.startswith('SELECT oli.line_item_id'):
            self._all = [tuple(li) for li in _LINES[params[0]]]
        elif sql.startswith('INSERT INTO shipments'):
            self._n += 1
            self._one = None if params[1] in self.fail_orders else (f'ship-{self._n}',)
        elif sql.startswith('SELECT pr.run_number'):
            run = _TRACEABLE.get(params[0])
            self._one = None if run is None else (run,)
        elif sql.startswith('SELECT item_id FROM inventory_items'):
            self._one = None if _BALANCES[params[0]] is None else ('item-' + params[0],)
        elif sql.startswith('SELECT ib.location_id, il.location_code'):
            self._one = self._fg_balance(params[0], locs, 'quantity_on_hand >=' in sql and params[-1])

    @staticmethod
    def _availability(order_id, locs):
        rows = []
        for _, pid, qty, _ in _LINES[order_id]:
            balances = _BALANCES[pid]
            if balances is None:
                continue                      # inner join to the FG item drops the line
            if not balances:
                rows.append((pid, qty, 0))    # LEFT JOIN null row survives the OR
                continue
            kept = [q for code, q in balances if code in locs]
            if kept:
                rows.append((pid, qty, sum(kept)))
        return rows

    @staticmethod
    def _fg_balance(item_id, locs, minimum):
        pid = item_id[len('item-'):]
        rows = [('loc-' + code, code, q) for code, q in _BALANCES[pid] if code in locs]
        if minimum is not False:
            rows = [r for r in rows if r[2] >= minimum]
        return max(rows, key=lambda r: r[2]) if rows else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _mask(sql, params):
    sql = _IN_RE.sub('<FG_LOCS>', sql).replace(_ANY, '<FG_LOCS>')
    if params is not None:
        params = tuple('<DT>' if isinstance(p, datetime) else p
                       for p in params if not isinstance(p, (list, tuple)))
    return (' '.join(sql.split()[:3]), hashlib.sha1(sql.encode()).hexdigest()[:10], params)  # nosec B324 - a short fingerprint for grouping statements, not a security hash


def _advancer(cursor):
    adv = StateAdvancer.__new__(StateAdvancer)  # skip get_config()
    adv.backorder_calls = []
    adv._create_production_for_backorder = (
        lambda cur, oid, onum, items: adv.backorder_calls.append((oid, onum, items)))
    # The backorder read starts at a random point each cycle; pin it to the lowest id.
    adv._backorder_scan_start = lambda: '00000000-0000-0000-0000-000000000000'
    return adv


def run_production_states():
    cursor = _Cursor()
    adv = _advancer(cursor)
    result = adv.advance_production_states(_Conn(cursor), 'cycle-1')
    return result, adv.backorder_calls, cursor


def run_backorder_states():
    cursor = _Cursor()
    result = _advancer(cursor).advance_backorder_states(_Conn(cursor), 'cycle-1')
    return result, cursor


def run_shipments(monkeypatch):
    counter = iter(range(1, 100))
    monkeypatch.setattr(utils, 'next_business_number', lambda cursor, prefix, now=None: f'{prefix}-{next(counter)}')
    monkeypatch.setattr(shipping_simulator, 'generate_tracking_number', lambda: 'TRK')
    random.seed(11)
    cursor = _Cursor(fail_orders={'s-fail'})
    sim = ShippingSimulator.__new__(ShippingSimulator)  # skip get_config()
    sim.carriers = ['AnyCompany Freight', 'AnyCompany Logistics', 'AnyCompany Parcel']
    result = sim.create_shipments(_Conn(cursor), 'cycle-1')
    return result, cursor


def _assert_legacy_allowlist(cursor, expected_count):
    assert len(cursor.fg_sets) == expected_count
    assert all(LEGACY_FG <= s for s in cursor.fg_sets)


# Captured from HEAD (pre-decomposition). Since then each order-status read
# takes one bounded batch per cycle (ORDER BY order_id LIMIT 200), and the
# BACKORDERED read starts at a per-cycle point (pinned in ``_advancer``) and
# wraps round with a second read for the rest of the batch.
_GOLDEN_PRODUCTION_RESULT = {'transitions': 9, 'orders_confirmed_to_ready': 4, 'orders_confirmed_to_backordered': 3, 'orders_backordered_to_ready': 2, 'cycle_id': 'cycle-1'}
_GOLDEN_BACKORDER_CALLS = [('o-short', 'O-SHORT', [('p-det', 500, 150)]), ('o-empty-bal', 'O-EMPTY-BAL', [('p-cr', 10, 0)]), ('o-mixed', 'O-MIXED', [('p-mke', 50, 300), ('p-det', 200, 150)])]
_GOLDEN_PRODUCTION = [   ('SELECT co.order_id, co.order_number', '5cb7c44a03', (200,)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-ready',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('o-ready',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-short',)),
    ('UPDATE customer_orders SET', '8af04a63f2', ('o-short',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-empty-bal',)),
    ('UPDATE customer_orders SET', '8af04a63f2', ('o-empty-bal',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-mixed',)),
    ('UPDATE customer_orders SET', '8af04a63f2', ('o-mixed',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-raw-only',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('o-raw-only',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-no-lines',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('o-no-lines',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('o-nofg',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('o-nofg',)),
    ('SELECT co.order_id, co.order_number', '075a6b8bbe', ('00000000-0000-0000-0000-000000000000', 200)),
    ('SELECT co.order_id, co.order_number', '1da3c5e3c7', ('00000000-0000-0000-0000-000000000000', 197)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-now-ok',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('b-now-ok',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-still-short',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-no-lines',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('b-no-lines',))]
_GOLDEN_BACKORDER_RESULT = {'transitions': 1, 'cycle_id': 'cycle-1'}
_GOLDEN_BACKORDER = [   ('SELECT co.order_id, co.order_number', '075a6b8bbe', ('00000000-0000-0000-0000-000000000000', 200)),
    ('SELECT co.order_id, co.order_number', '1da3c5e3c7', ('00000000-0000-0000-0000-000000000000', 197)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-now-ok',)),
    ('UPDATE customer_orders SET', '47b09e5db9', ('b-now-ok',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-still-short',)),
    ('SELECT oli.product_id, oli.quantity,', '099e230fab', ('b-no-lines',))]
_GOLDEN_SHIP_RESULT = {'shipments_created': 3, 'cycle_id': 'cycle-1'}
# ADR-0090: the READY_TO_SHIP read is bounded (LIMIT 200, SKIP LOCKED) and each
# shipment INSERT runs under the numbered-insert savepoint.
_GOLDEN_SHIP = [   ('SELECT co.order_id, co.order_number,', '6458a209e3', (200,)),
    ('SAVEPOINT ship_order', '1330e96b4e', None),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_freight_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_logistics_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_parcel_weight',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_min',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_max',)),
    ('SAVEPOINT adt_numbered_insert', 'cc7ae06e38', None),
    (   'INSERT INTO shipments',
        'b2bd3d6143',
        (   'SHIP-1',
            's-split',
            'cust-s-split',
            '<DT>',
            'AnyCompany Logistics',
            'TRK',
            '<DT>',
            'PREPARING')),
    ('RELEASE SAVEPOINT adt_numbered_insert', '179d53738e', None),
    ('SELECT oli.line_item_id, oli.product_id,', '7fbc4f4772', ('s-split',)),
    ('SELECT pr.run_number FROM', '1fe234ad34', ('p-fgw', 'Live-story history run')),
    (   'INSERT INTO shipment_items',
        '0a884b9790',
        ('ship-1', 'li-10', 'p-fgw', 30, None)),
    ('SELECT item_id FROM', 'a78e7aaf54', ('p-fgw',)),
    (   'SELECT ib.location_id, il.location_code,',
        '3ceaf5aa08',
        ('item-p-fgw', 30)),
    (   'INSERT INTO inventory_transactions',
        'e9e4413db7',
        (   'item-p-fgw',
            'loc-FG-WAREHOUSE',
            'CONSUMPTION',
            -30,
            'SHIPMENT',
            'ship-1',
            '<DT>')),
    (   'UPDATE inventory_balances SET',
        '2a841054ba',
        (30, '<DT>', 'item-p-fgw', 'loc-FG-WAREHOUSE')),
    ('SELECT pr.run_number FROM', '1fe234ad34', ('p-det', 'Live-story history run')),
    (   'INSERT INTO shipment_items',
        '0a884b9790',
        ('ship-1', 'li-11', 'p-det', 120, 'RUN-DET-1')),
    ('SELECT item_id FROM', 'a78e7aaf54', ('p-det',)),
    (   'SELECT ib.location_id, il.location_code,',
        '3ceaf5aa08',
        ('item-p-det', 120)),
    ('SELECT ib.location_id, il.location_code,', '5922cd69c4', ('item-p-det',)),
    (   'INSERT INTO inventory_transactions',
        'e9e4413db7',
        (   'item-p-det',
            'loc-DET-FG',
            'CONSUMPTION',
            -120,
            'SHIPMENT',
            'ship-1',
            '<DT>')),
    (   'UPDATE inventory_balances SET',
        '2a841054ba',
        (120, '<DT>', 'item-p-det', 'loc-DET-FG')),
    ('UPDATE customer_orders SET', '1040a3aa7c', ('s-split',)),
    ('SAVEPOINT ship_order', '1330e96b4e', None),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_freight_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_logistics_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_parcel_weight',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_min',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_max',)),
    ('SAVEPOINT adt_numbered_insert', 'cc7ae06e38', None),
    (   'INSERT INTO shipments',
        'b2bd3d6143',
        (   'SHIP-2',
            's-fail',
            'cust-s-fail',
            '<DT>',
            'AnyCompany Parcel',
            'TRK',
            '<DT>',
            'PREPARING')),
    ('RELEASE SAVEPOINT adt_numbered_insert', '179d53738e', None),
    ('ROLLBACK TO SAVEPOINT', 'e35dcf7d13', None),
    ('RELEASE SAVEPOINT ship_order', '254cc4610f', None),
    ('SAVEPOINT ship_order', '1330e96b4e', None),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_freight_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_logistics_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_parcel_weight',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_min',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_max',)),
    ('SAVEPOINT adt_numbered_insert', 'cc7ae06e38', None),
    (   'INSERT INTO shipments',
        'b2bd3d6143',
        (   'SHIP-3',
            's-misc',
            'cust-s-misc',
            '<DT>',
            'AnyCompany Logistics',
            'TRK',
            '<DT>',
            'PREPARING')),
    ('RELEASE SAVEPOINT adt_numbered_insert', '179d53738e', None),
    ('SELECT oli.line_item_id, oli.product_id,', '7fbc4f4772', ('s-misc',)),
    ('SELECT pr.run_number FROM', '1fe234ad34', ('p-nofg', 'Live-story history run')),
    (   'INSERT INTO shipment_items',
        '0a884b9790',
        ('ship-3', 'li-13', 'p-nofg', 2, None)),
    ('SELECT item_id FROM', 'a78e7aaf54', ('p-nofg',)),
    ('SELECT pr.run_number FROM', '1fe234ad34', ('p-cr', 'Live-story history run')),
    (   'INSERT INTO shipment_items',
        '0a884b9790',
        ('ship-3', 'li-14', 'p-cr', 3, None)),
    ('SELECT item_id FROM', 'a78e7aaf54', ('p-cr',)),
    (   'SELECT ib.location_id, il.location_code,',
        '3ceaf5aa08',
        ('item-p-cr', 3)),
    ('SELECT ib.location_id, il.location_code,', '5922cd69c4', ('item-p-cr',)),
    ('SELECT pr.run_number FROM', '1fe234ad34', ('p-raw', 'Live-story history run')),
    (   'INSERT INTO shipment_items',
        '0a884b9790',
        ('ship-3', 'li-15', 'p-raw', 4, None)),
    ('SELECT item_id FROM', 'a78e7aaf54', ('p-raw',)),
    (   'SELECT ib.location_id, il.location_code,',
        '3ceaf5aa08',
        ('item-p-raw', 4)),
    ('SELECT ib.location_id, il.location_code,', '5922cd69c4', ('item-p-raw',)),
    ('UPDATE customer_orders SET', '1040a3aa7c', ('s-misc',)),
    ('SAVEPOINT ship_order', '1330e96b4e', None),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_freight_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_logistics_weight',)),
    (   'SELECT config_value FROM',
        'dc941df510',
        ('shipping_carrier_parcel_weight',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_min',)),
    ('SELECT config_value FROM', 'dc941df510', ('shipping_delay_max',)),
    ('SAVEPOINT adt_numbered_insert', 'cc7ae06e38', None),
    (   'INSERT INTO shipments',
        'b2bd3d6143',
        (   'SHIP-4',
            'o-no-lines',
            'cust-o-no-lines',
            '<DT>',
            'AnyCompany Freight',
            'TRK',
            '<DT>',
            'PREPARING')),
    ('RELEASE SAVEPOINT adt_numbered_insert', '179d53738e', None),
    ('SELECT oli.line_item_id, oli.product_id,', '7fbc4f4772', ('o-no-lines',)),
    ('UPDATE customer_orders SET', '1040a3aa7c', ('o-no-lines',))]


def test_advance_production_states_characterization():
    result, backorder_calls, cursor = run_production_states()
    assert result == _GOLDEN_PRODUCTION_RESULT
    assert backorder_calls == _GOLDEN_BACKORDER_CALLS
    assert [_mask(*c) for c in cursor.calls] == _GOLDEN_PRODUCTION
    _assert_legacy_allowlist(cursor, len(_CONFIRMED) + len(_BACKORDERED))


def test_advance_backorder_states_characterization():
    result, cursor = run_backorder_states()
    assert result == _GOLDEN_BACKORDER_RESULT
    assert [_mask(*c) for c in cursor.calls] == _GOLDEN_BACKORDER
    _assert_legacy_allowlist(cursor, len(_BACKORDERED))


def test_create_shipments_characterization(monkeypatch):
    result, cursor = run_shipments(monkeypatch)
    assert result == _GOLDEN_SHIP_RESULT
    assert [_mask(*c) for c in cursor.calls] == _GOLDEN_SHIP
    assert all(LEGACY_FG <= s for s in cursor.fg_sets) and cursor.fg_sets


def test_production_state_errors_propagate():
    cursor = _Cursor()
    adv = _advancer(cursor)
    cursor.execute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db down'))
    with pytest.raises(RuntimeError):
        adv.advance_production_states(_Conn(cursor), 'c')
    with pytest.raises(RuntimeError):
        adv.advance_backorder_states(_Conn(cursor), 'c')
