# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Order segments: one category map, Machined Components orders.

The characterization test pins the SQL statement/param sequence that
``OrderSimulator.generate_orders`` executed BEFORE it was decomposed, for
the three legacy segments and the unknown-segment fallback (fixed
``random.seed``; generated order numbers and wall-clock dates masked).
"""
from __future__ import annotations

import random
import re

from erp_simulation import segments
import pytest

from erp_simulation.order_simulator import OrderSimulator
from erp_simulation.tests._numbering import fake_business_numbers, without_isolation
from erp_simulation.seed_data import SeedDataGenerator

_CONFIG = {'orders_per_cycle_min': '6', 'orders_per_cycle_max': '6'}

_LEGACY_CUSTOMERS = [
    ('c-bev', 'CUST-0001', 'Bev Co', 'NET30', 'BEVERAGE'),
    ('c-auto', 'CUST-0002', 'Auto Co', 'NET30', 'AUTOMOTIVE'),
    ('c-food', 'CUST-0003', 'Food Co', 'NET30', 'FOOD'),
    ('c-other', 'CUST-0004', 'Other Co', 'NET30', 'RETAIL'),
]

_PRODUCTS = [
    ('p-cola5', 'COLA-500ML', 'Cola 500ml', 1.25, 'Carbonated Soft Drinks'),
    ('p-oj1', 'ORANGE-1L', 'Orange 1L', 2.50, 'Juices'),
    ('p-eng', 'AUTO-ENG-001', 'Piston', 40.00, 'Engine Components'),
    ('p-brk', 'AUTO-BRK-002', 'Brake Pad', 22.00, 'Brake Components'),
    ('p-cer', 'FOOD-CER-001', 'Cereal', 3.10, 'Cereals'),
    ('p-hsg', 'MACH-HSG-7742', 'Housing', 340.00, 'Machined Components'),
    ('p-shft', 'MACH-SHFT-8810', 'Coupling', 180.00, 'Machined Components'),
    ('p-parent', 'BEV-PARENT', 'Parent', 9.99, 'Beverages'),
]


@pytest.fixture(autouse=True)
def _sequence_numbers(monkeypatch):
    """ADR-0090 numbers without a database sequence."""
    fake_business_numbers(monkeypatch)


class _FakeCursor:
    """Records every statement; answers config, customer, product and RETURNING reads."""

    def __init__(self, customers, products, config=None, duplicate_inserts=()):
        self.customers = customers
        self.products = products
        self.config = dict(_CONFIG if config is None else config)
        self.duplicate_inserts = set(duplicate_inserts)
        self.calls = []
        self._pending = None
        self._inserts = 0

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))
        if 'simulation_config' in sql:
            value = self.config.get(params[0])
            self._pending = None if value is None else (value,)
        elif 'RETURNING order_id' in sql:
            self._inserts += 1
            dup = self._inserts in self.duplicate_inserts
            self._pending = None if dup else (f'order-{self._inserts}',)

    def fetchone(self):
        return self._pending

    def fetchall(self):
        sql = self.calls[-1][0]
        return list(self.customers if 'FROM customers' in sql else self.products)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _mask(sql, params):
    """Replace generated order numbers and wall-clock dates with stable tokens."""
    if params is None:
        return (sql, None)
    if sql.startswith('INSERT INTO customer_orders'):
        number, customer, ordered, requested, status = params
        assert re.fullmatch(r'ORD-\d{8}-\d{8}', number)
        return (sql, ('<ORDER>', customer, (requested - ordered).days, status))
    return (sql, tuple(round(p, 2) if isinstance(p, float) else p for p in params))


def _run(customers=_LEGACY_CUSTOMERS, products=_PRODUCTS, seed=7, **kw):
    random.seed(seed)
    cursor = _FakeCursor(customers, products, **kw)
    # __new__ skips __init__'s get_config() (Secrets Manager); generate_orders never reads it.
    result = OrderSimulator.__new__(OrderSimulator).generate_orders(_FakeConn(cursor), 'cycle-1')
    return result, [_mask(sql, params) for sql, params in without_isolation(cursor.calls)]


def _kind(sql):
    return ' '.join(sql.split()[:3])


# Captured from HEAD (pre-decomposition) with random.seed(7) / seed(11).
_GOLDEN_LEGACY = [
    ('SELECT config_value FROM', ('orders_per_cycle_min',)),
    ('SELECT config_value FROM', ('orders_per_cycle_max',)),
    ('SELECT customer_id, customer_code,', None),
    ('SELECT product_id, sku,', None),
    ('SELECT config_value FROM', ('product_cola_weight',)),
    ('SELECT config_value FROM', ('product_orange_weight',)),
    ('SELECT config_value FROM', ('product_lemon_weight',)),
    ('SELECT config_value FROM', ('product_grape_weight',)),
    ('SELECT config_value FROM', ('order_quantity_min',)),
    ('SELECT config_value FROM', ('order_quantity_max',)),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-auto', 13, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-1', 1, 'p-eng', 95, 40.0)),
    ('UPDATE customer_orders SET', (3800.0, 304.0, 50.0, 4154.0, 'order-1')),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-other', 10, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-2', 1, 'p-oj1', 42, 2.5)),
    ('UPDATE customer_orders SET', (105.0, 8.4, 50.0, 163.4, 'order-2')),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-food', 8, 'PENDING')),
    ('ROLLBACK TO SAVEPOINT', None),
    ('RELEASE SAVEPOINT order_insert', None),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-bev', 10, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-4', 1, 'p-oj1', 56, 2.5)),
    ('INSERT INTO order_line_items', ('order-4', 2, 'p-cola5', 126, 1.25)),
    ('UPDATE customer_orders SET', (297.5, 23.8, 50.0, 371.3, 'order-4')),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-other', 14, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-5', 1, 'p-cola5', 69, 1.25)),
    ('INSERT INTO order_line_items', ('order-5', 2, 'p-oj1', 98, 2.5)),
    ('INSERT INTO order_line_items', ('order-5', 3, 'p-oj1', 65, 2.5)),
    ('INSERT INTO order_line_items', ('order-5', 4, 'p-cola5', 139, 1.25)),
    ('UPDATE customer_orders SET', (667.5, 53.4, 50.0, 770.9, 'order-5')),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-other', 17, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-6', 1, 'p-oj1', 82, 2.5)),
    ('INSERT INTO order_line_items', ('order-6', 2, 'p-cola5', 148, 1.25)),
    ('INSERT INTO order_line_items', ('order-6', 3, 'p-oj1', 69, 2.5)),
    ('INSERT INTO order_line_items', ('order-6', 4, 'p-cola5', 168, 1.25)),
    ('INSERT INTO order_line_items', ('order-6', 5, 'p-cola5', 79, 1.25)),
    ('UPDATE customer_orders SET', (871.25, 69.7, 50.0, 990.95, 'order-6')),
]

_GOLDEN_EMPTY_SEGMENT = [
    ('SELECT config_value FROM', ('orders_per_cycle_min',)),
    ('SELECT config_value FROM', ('orders_per_cycle_max',)),
    ('SELECT customer_id, customer_code,', None),
    ('SELECT product_id, sku,', None),
    ('SELECT config_value FROM', ('product_cola_weight',)),
    ('SELECT config_value FROM', ('product_orange_weight',)),
    ('SELECT config_value FROM', ('product_lemon_weight',)),
    ('SELECT config_value FROM', ('product_grape_weight',)),
    ('SELECT config_value FROM', ('order_quantity_min',)),
    ('SELECT config_value FROM', ('order_quantity_max',)),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-food', 9, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-1', 1, 'p-eng', 30, 40.0)),
    ('UPDATE customer_orders SET', (1200.0, 96.0, 50.0, 1346.0, 'order-1')),
    ('SAVEPOINT order_insert', None),
    ('INSERT INTO customer_orders', ('<ORDER>', 'c-food', 10, 'PENDING')),
    ('RELEASE SAVEPOINT order_insert', None),
    ('INSERT INTO order_line_items', ('order-2', 1, 'p-cola5', 168, 1.25)),
    ('INSERT INTO order_line_items', ('order-2', 2, 'p-oj1', 100, 2.5)),
    ('INSERT INTO order_line_items', ('order-2', 3, 'p-parent', 91, 9.99)),
    ('INSERT INTO order_line_items', ('order-2', 4, 'p-oj1', 62, 2.5)),
    ('INSERT INTO order_line_items', ('order-2', 5, 'p-eng', 35, 40.0)),
    ('UPDATE customer_orders SET', (2924.09, 233.93, 50.0, 3208.02, 'order-2')),
]

_GOLDEN_SQL = {
    "INSERT INTO customer_orders ( order_number, customer_id, order_date, requested_delivery_date, status ) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (order_number) DO NOTHING RETURNING order_id",
    'INSERT INTO order_line_items ( order_id, line_number, product_id, quantity, unit_price ) VALUES (%s, %s, %s, %s, %s)',
    'RELEASE SAVEPOINT order_insert',
    'ROLLBACK TO SAVEPOINT order_insert',
    'SAVEPOINT order_insert',
    'SELECT config_value FROM simulation_config WHERE config_key = %s',
    "SELECT customer_id, customer_code, company_name, payment_terms, COALESCE(customer_segment, 'BEVERAGE') AS customer_segment FROM customers WHERE is_active = TRUE",
    'SELECT product_id, sku, product_name, list_price, category FROM products WHERE is_active = TRUE ORDER BY product_id',
    'UPDATE customer_orders SET subtotal = %s, tax_amount = %s, shipping_amount = %s, total_amount = %s WHERE order_id = %s',
}

_FOOD_ONLY = [c for c in _LEGACY_CUSTOMERS if c[4] == 'FOOD']
_NO_FOOD_PRODUCTS = [p for p in _PRODUCTS if p[4] not in ('Cereals', 'Machined Components')]
_TWO_ORDERS = {'orders_per_cycle_min': '2', 'orders_per_cycle_max': '2'}


def test_characterization_legacy_segments_and_fallback():
    result, trace = _run(duplicate_inserts={3})
    assert result == {'orders_created': 5, 'cycle_id': 'cycle-1'}
    assert [(_kind(s), p) for s, p in trace] == _GOLDEN_LEGACY


def test_characterization_segment_without_products_uses_all_products():
    result, trace = _run(_FOOD_ONLY, _NO_FOOD_PRODUCTS, seed=11, config=_TWO_ORDERS)
    assert result == {'orders_created': 2, 'cycle_id': 'cycle-1'}
    assert [(_kind(s), p) for s, p in trace] == _GOLDEN_EMPTY_SEGMENT


def test_characterization_sql_text_unchanged():
    _, trace = _run(duplicate_inserts={3})
    _, trace2 = _run(_FOOD_ONLY, _NO_FOOD_PRODUCTS, seed=11, config=_TWO_ORDERS)
    assert {s for s, _ in trace + trace2} == _GOLDEN_SQL


def test_characterization_early_returns():
    zero = {'orders_per_cycle_min': '0', 'orders_per_cycle_max': '0'}
    assert _run(config=zero)[0] == {'orders_created': 0}
    assert len(_run(config=zero)[1]) == 2
    assert _run(customers=[])[0] == {'orders_created': 0}
    assert len(_run(customers=[])[1]) == 3
    assert _run(products=[])[0] == {'orders_created': 0}
    assert len(_run(products=[])[1]) == 4


# --- behaviour after the single-source refactor ------------------------------

_MACHINING = 'MACHINING'
_MACHINED = 'Machined Components'
_BY_ID = {p[0]: p for p in _PRODUCTS}


def _line_items(trace):
    return [params for sql, params in trace if sql.startswith('INSERT INTO order_line_items')]


def test_machining_segment_maps_to_machined_components():
    assert segments.SEGMENT_CATEGORIES[_MACHINING] == {_MACHINED}
    assert segments.allowed_categories(_MACHINING) == {_MACHINED}


def test_unknown_segment_falls_back_to_beverage():
    beverage = segments.SEGMENT_CATEGORIES[segments.FALLBACK_SEGMENT]
    assert segments.FALLBACK_SEGMENT == 'BEVERAGE'
    assert segments.allowed_categories('NO-SUCH-SEGMENT') == beverage
    assert segments.allowed_categories('') == beverage


def test_machining_customer_orders_only_machined_components():
    customer = [('c-mach', 'CUST-MCH-0001', 'Machining Co', 'NET30', _MACHINING)]
    result, trace = _run(customer, seed=3)
    items = _line_items(trace)
    assert result['orders_created'] == 6
    assert items
    assert {_BY_ID[pid][4] for _, _, pid, _, _ in items} == {_MACHINED}


def test_machined_order_quantities_are_tens_of_units():
    customer = [('c-mach', 'CUST-MCH-0001', 'Machining Co', 'NET30', _MACHINING)]
    _, trace = _run(customer, seed=5)
    quantities = [qty for _, _, _, qty, _ in _line_items(trace)]
    assert quantities and all(5 <= q <= 20 for q in quantities)  # default range 50..200


def test_unknown_segment_customer_orders_beverages():
    customer = [('c-x', 'CUST-X', 'X Co', 'NET30', 'NO-SUCH-SEGMENT')]
    _, trace = _run(customer, seed=9)
    beverage = segments.SEGMENT_CATEGORIES[segments.FALLBACK_SEGMENT]
    assert {_BY_ID[pid][4] for _, _, pid, _, _ in _line_items(trace)} <= beverage


def test_quantity_range_rules():
    assert segments.quantity_range('COLA-500ML', 50, 200) == (50, 200)
    assert segments.quantity_range('cola-500ml', 50, 200) == (50, 200)
    assert segments.quantity_range('ORANGE-1L', 50, 200) == (25, 100)
    assert segments.quantity_range('MACH-HSG-7742', 50, 200) == (5, 20)
    assert segments.quantity_range('mach-brkt-9901', 50, 200) == (5, 20)
    assert segments.quantity_range('MACH-SHFT-8810', 5, 9) == (1, 2)


def test_seed_generator_segments_derive_from_single_source():
    legacy = {k: set(v) for k, v in segments.SEGMENT_CATEGORIES.items() if k != _MACHINING}
    assert {k: set(v) for k, v in SeedDataGenerator.SEGMENT_CATEGORIES.items()} == legacy
    assert _MACHINING not in SeedDataGenerator.SEGMENT_CATEGORIES


#: SeedDataGenerator.SEGMENT_CATEGORIES exactly as the pre-refactor literal
#: (key AND list order), verified against ``git show HEAD:erp_simulation/seed_data.py``.
_PRE_REFACTOR_SEED_SEGMENTS = {
    'BEVERAGE': ['Carbonated Soft Drinks', 'Juices', 'Water'],
    'AUTOMOTIVE': ['Engine Components', 'Transmission Components', 'Brake Components',
                   'Electrical Systems'],
    'FOOD': ['Cereals', 'Snack Foods', 'Grain Products'],
}


def test_seed_generator_segments_keep_pre_refactor_order():
    derived = SeedDataGenerator.SEGMENT_CATEGORIES
    assert list(derived) == list(_PRE_REFACTOR_SEED_SEGMENTS)
    assert derived == _PRE_REFACTOR_SEED_SEGMENTS


def test_generate_orders_return_keys_unchanged():
    result, _ = _run()
    assert set(result) == {'orders_created', 'cycle_id'}
