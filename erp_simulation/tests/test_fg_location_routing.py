# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""FG-location routing for replenishment and backorder production.

The characterization tests pin the SQL statement/param sequence that
``ProductionSimulator.create_production_runs`` and
``StateAdvancer._create_production_for_backorder`` executed BEFORE they were
decomposed (fixed ``random.seed``; generated run numbers masked; the
replenishment FG-location CASE compared separately because it now carries
every mapped site).
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import random
import re
import uuid
from pathlib import Path

import pytest

from erp_simulation import production_simulator
from erp_simulation.tests._numbering import fake_business_numbers, without_isolation
from erp_simulation.production_simulator import ProductionSimulator
from erp_simulation.state_advancer import StateAdvancer

# product_id -> (sku, name, routed site, bom[(component, qty_required, available, item_code, uom)])
_PRODUCTS = {
    'p-mke1': ('BEV-COLA', 'Cola', 'MILWAUKEE_BOTTLING', [('c-syrup', 0.5, 10000, 'SYRUP', 'L')]),
    'p-mke2': ('BEV-OJ', 'Orange', 'MILWAUKEE_BOTTLING', [('c-conc', 1.0, 400, 'CONC', 'L')]),
    'p-mke3': ('BEV-LEM', 'Lemon', 'MILWAUKEE_BOTTLING', [('c-syrup', 0.5, 10000, 'SYRUP', 'L')]),
    'p-mke4': ('BEV-GRP', 'Grape', 'MILWAUKEE_BOTTLING', [('c-syrup', 0.5, 10000, 'SYRUP', 'L')]),
    'p-det1': ('AUTO-ENG', 'Piston', 'DETROIT_AUTOPARTS', [('c-steel', 2.0, 10, 'STEEL', 'KG')]),
    'p-det2': ('AUTO-BRK', 'Brake', 'DETROIT_AUTOPARTS', []),
    'p-cr1': ('FOOD-CER', 'Cereal', 'CEDARRAPIDS_FOOD', [('c-oats', 0.25, 100000, 'OATS', 'KG'),
                                                         ('c-box', 1.0, 100000, 'BOX', 'EA')]),
    'p-cr2': ('FOOD-SNK', 'Snack', 'CEDARRAPIDS_FOOD', [('c-oats', 0.25, 100000, 'OATS', 'KG')]),
    'p-ind1': ('MACH-HSG', 'Housing', 'INDIANAPOLIS_MACHINING', [('c-bar', 1.0, 100000, 'BAR', 'EA')]),
    'p-boom': ('BEV-BOOM', 'Boom', 'MILWAUKEE_BOTTLING', [('c-syrup', 0.5, 10000, 'SYRUP', 'L')]),
}

# Every mapped FG location resolves except CR (exercises the missing-location skip).
_LOCATIONS = {'MKE-FG': 'loc-mke', 'DET-FG': 'loc-det', 'IND-FG': 'loc-ind', 'FG-WAREHOUSE': 'loc-fgw'}

_LOTS = {
    'c-syrup': [('lot-s1', 200.0), ('lot-s2', 5000.0)],
    'c-oats': [('lot-o1', 10.0)],
    'c-bar': [('lot-b1', 1000.0)],
}

_CONFIG = {'production_batch_size_min': '500', 'production_batch_size_max': '600',
           'fg_reorder_point': '1000'}

_CASE_RE = re.compile(r"CASE s\.site_code (?:WHEN '[A-Z_]+' THEN '[A-Z-]+' )+ELSE 'FG-WAREHOUSE' END")


@pytest.fixture(autouse=True)
def _sequence_numbers(monkeypatch):
    """ADR-0090 numbers without a database sequence."""
    fake_business_numbers(monkeypatch)


class _FakeCursor:
    """Scripted cursor: answers by statement shape, records every call."""

    def __init__(self, candidates=(), existing_runs=(), locations=None, no_return=()):
        self.candidates = list(candidates)
        self.existing_runs = set(existing_runs)
        self.locations = dict(_LOCATIONS if locations is None else locations)
        self.no_return = set(no_return)
        self.calls = []
        self.rowcount = 0
        self._one = None
        self._all = []
        self._runs = 0

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self.calls.append((sql, params))
        self._one, self._all = None, []
        if sql.startswith('UPDATE production_runs'):
            self.rowcount = 2
        elif 'simulation_config' in sql:
            value = _CONFIG.get(params[0])
            self._one = None if value is None else (value,)
        elif sql.startswith('WITH pipeline_production'):
            self._all = self.candidates
        elif 'as available' in sql:
            self._all = [(c, q, a, code) for c, q, a, code, _ in _PRODUCTS[params[0]][3]]
        elif 'FROM material_lots' in sql:
            self._all = list(_LOTS.get(params[0], []))
        elif 'FROM inventory_locations' in sql:
            loc = self.locations.get(params[0])
            self._one = None if loc is None else (loc,)
        elif sql.startswith('INSERT INTO production_runs'):
            self._runs += 1
            self._one = None if params[1] in self.no_return else (f'run-{self._runs}',)
        elif 'ii.unit_of_measure' in sql:
            self._all = [(c, q, code, uom) for c, q, _, code, uom in _PRODUCTS[params[0]][3]]
        elif sql.startswith('SELECT component_item_id, quantity_required'):
            self._all = [(c, q) for c, q, *_ in _PRODUCTS[params[0]][3]]
        elif sql.startswith('SELECT run_id, planned_quantity'):
            self._one = ('run-x', 10) if params[0] in self.existing_runs else None
        elif sql.startswith('SELECT sku, product_name'):
            info = _PRODUCTS.get(params[0])
            self._one = None if info is None else info[:2]
        elif sql.startswith('SELECT s.site_code'):
            info = _PRODUCTS.get(params[0])
            self._one = None if info is None or info[2] is None else (info[2],)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _mask(sql, params):
    sql = _CASE_RE.sub('<FG_CASE>', sql)
    if sql.startswith('INSERT INTO production_runs'):
        assert re.fullmatch(r'RUN-\d{8}-\d{5}', params[0])
        params = ('<RUN>',) + tuple(params[1:])
    if params is not None:
        params = tuple(round(p, 2) if isinstance(p, float) else p for p in params)
    return (' '.join(sql.split()[:3]), hashlib.sha1(sql.encode()).hexdigest()[:10], params)  # nosec B324 - a short fingerprint for grouping statements, not a security hash


def _candidate(pid, inventory=50, pipeline=0):
    sku, name, site, _ = _PRODUCTS[pid]
    return (pid, sku, name, inventory, pipeline, site)


# 4 MKE candidates (4th hits the per-site line cap), a material-capped MKE run,
# a material-starved DET skip, a no-BOM DET run, a missing CR location, and a
# RETURNING failure caught per candidate.
_REPLENISH = [_candidate(p) for p in ('p-mke1', 'p-mke2', 'p-boom', 'p-mke3', 'p-det1',
                                        'p-det2', 'p-cr1', 'p-mke4')]


def _replenish(candidates=_REPLENISH, seed=7, **kw):
    random.seed(seed)
    cursor = _FakeCursor(candidates=candidates, **kw)
    sim = ProductionSimulator.__new__(ProductionSimulator)  # skip get_config()
    sim.production_lines = ['LINE1', 'LINE2', 'LINE3']
    result = sim.create_production_runs(_FakeConn(cursor), 'cycle-1')
    return result, cursor


def _backorder(line_items, seed=7, **kw):
    random.seed(seed)
    cursor = _FakeCursor(**kw)
    StateAdvancer.__new__(StateAdvancer)._create_production_for_backorder(
        cursor, 'order-1', 'ORD-1', line_items)
    return cursor


# Captured from HEAD (pre-decomposition), random.seed(7).
# The first statement is the once-per-cycle claims-table probe (no table
# here, so the stale-run UPDATE after it is the unchanged statement).
_GOLDEN_REPLENISH = [   ('SELECT to_regclass(\'public.external_mes_runs\')', '3421e48897', None),
    ('UPDATE production_runs SET', 'cadf8d6c6a', None),
    ('SELECT config_value FROM', 'dc941df510', ('production_batch_size_min',)),
    ('SELECT config_value FROM', 'dc941df510', ('production_batch_size_max',)),
    ('SELECT config_value FROM', 'dc941df510', ('fg_reorder_point',)),
    ('WITH pipeline_production AS', 'bc3903be92', (1000,)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-mke1',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '2dc8938644',
        (   '<RUN>',
            'p-mke1',
            541,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Inventory replenishment batch for BEV-COLA (current: 50)')),
    ('SELECT bom.component_item_id, bom.quantity_required,', '89227cc184', ('p-mke1',)),
    ('SELECT ml.lot_id, ml.remaining_quantity', '80b44b2a75', ('c-syrup', 'MKE-WH-RAW')),
    ('INSERT INTO lot_production_usage', '39cd744b03', ('lot-s1', 'run-1', 200.0, 'L')),
    ('UPDATE material_lots SET', 'e7762e0bdc', (200.0, 'lot-s1')),
    ('INSERT INTO lot_production_usage', '39cd744b03', ('lot-s2', 'run-1', 70.5, 'L')),
    ('UPDATE material_lots SET', 'e7762e0bdc', (70.5, 'lot-s2')),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-mke1',)),
    ('INSERT INTO production_materials', 'c67ddaad8c', ('run-1', 'c-syrup', 270.5)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-mke2',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '2dc8938644',
        (   '<RUN>',
            'p-mke2',
            400,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Inventory replenishment batch for BEV-OJ (current: 50)')),
    ('SELECT bom.component_item_id, bom.quantity_required,', '89227cc184', ('p-mke2',)),
    ('SELECT ml.lot_id, ml.remaining_quantity', '80b44b2a75', ('c-conc', 'MKE-WH-RAW')),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-mke2',)),
    ('INSERT INTO production_materials', 'c67ddaad8c', ('run-2', 'c-conc', 400.0)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-boom',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '2dc8938644',
        (   '<RUN>',
            'p-boom',
            511,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Inventory replenishment batch for BEV-BOOM (current: 50)')),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-mke3',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '2dc8938644',
        (   '<RUN>',
            'p-mke3',
            574,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Inventory replenishment batch for BEV-LEM (current: 50)')),
    ('SELECT bom.component_item_id, bom.quantity_required,', '89227cc184', ('p-mke3',)),
    ('SELECT ml.lot_id, ml.remaining_quantity', '80b44b2a75', ('c-syrup', 'MKE-WH-RAW')),
    ('INSERT INTO lot_production_usage', '39cd744b03', ('lot-s1', 'run-4', 200.0, 'L')),
    ('UPDATE material_lots SET', 'e7762e0bdc', (200.0, 'lot-s1')),
    ('INSERT INTO lot_production_usage', '39cd744b03', ('lot-s2', 'run-4', 87.0, 'L')),
    ('UPDATE material_lots SET', 'e7762e0bdc', (87.0, 'lot-s2')),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-mke3',)),
    ('INSERT INTO production_materials', 'c67ddaad8c', ('run-4', 'c-syrup', 287.0)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-det1',)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-det2',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('DET-FG',)),
    (   'INSERT INTO production_runs',
        '2dc8938644',
        (   '<RUN>',
            'p-det2',
            537,
            'PLANNED',
            'loc-det',
            'DETROIT_AUTOPARTS',
            'Inventory replenishment batch for AUTO-BRK (current: 50)')),
    ('SELECT bom.component_item_id, bom.quantity_required,', '89227cc184', ('p-det2',)),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-det2',)),
    ('SELECT bom.component_item_id, bom.quantity_required,', '71abc76472', ('p-cr1',)),
    ('SELECT location_id FROM', 'db8e8da1a8', ('CR-FG',))]
_GOLDEN_BACKORDER = [   ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-mke1',)),
    ('SELECT sku, product_name', '63822ae6ac', ('p-mke1',)),
    ('SELECT s.site_code FROM', '873bd66553', ('p-mke1',)),
    ('SELECT location_id FROM', 'b59518cc51', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '9fe4eac4a6',
        (   '<RUN>',
            'p-mke1',
            'order-1',
            800,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Order-driven production for ORD-1 - BEV-COLA (shortage: 800)')),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-mke1',)),
    ('INSERT INTO production_materials', 'c67ddaad8c', ('run-1', 'c-syrup', 400.0)),
    ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-det1',)),
    ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-gone',)),
    ('SELECT sku, product_name', '63822ae6ac', ('p-gone',)),
    ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-cr1',)),
    ('SELECT sku, product_name', '63822ae6ac', ('p-cr1',)),
    ('SELECT s.site_code FROM', '873bd66553', ('p-cr1',)),
    ('SELECT location_id FROM', 'b59518cc51', ('CR-FG',)),
    ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-det2',)),
    ('SELECT sku, product_name', '63822ae6ac', ('p-det2',)),
    ('SELECT s.site_code FROM', '873bd66553', ('p-det2',)),
    ('SELECT location_id FROM', 'b59518cc51', ('DET-FG',)),
    (   'INSERT INTO production_runs',
        '9fe4eac4a6',
        (   '<RUN>',
            'p-det2',
            'order-1',
            100,
            'PLANNED',
            'loc-det',
            'DETROIT_AUTOPARTS',
            'Order-driven production for ORD-1 - AUTO-BRK (shortage: 15)')),
    ('SELECT component_item_id, quantity_required', '90c5f852a7', ('p-det2',)),
    ('SELECT run_id, planned_quantity', 'dbab8bca1e', ('p-boom',)),
    ('SELECT sku, product_name', '63822ae6ac', ('p-boom',)),
    ('SELECT s.site_code FROM', '873bd66553', ('p-boom',)),
    ('SELECT location_id FROM', 'b59518cc51', ('MKE-FG',)),
    (   'INSERT INTO production_runs',
        '9fe4eac4a6',
        (   '<RUN>',
            'p-boom',
            'order-1',
            100,
            'PLANNED',
            'loc-mke',
            'MILWAUKEE_BOTTLING',
            'Order-driven production for ORD-1 - BEV-BOOM (shortage: 40)'))]


def test_replenishment_characterization():
    result, cursor = _replenish(no_return={'p-boom'})
    assert result == {'runs_created': 4, 'cycle_id': 'cycle-1'}
    assert [_mask(*c) for c in without_isolation(cursor.calls)] == _GOLDEN_REPLENISH


_BACKORDER_ITEMS = [
    ('p-mke1', 900, 100),   # shortage -> run at MKE
    ('p-mke2', 50, 80),     # no shortage
    ('p-det1', 30, 0),      # existing run -> skip
    ('p-gone', 30, 0),      # unknown product -> skip
    ('p-cr1', 30, 0),       # CR location missing -> skip
    ('p-det2', 20, 5),      # shortage below minimum batch -> 100
    ('p-boom', 40, 0),      # RETURNING failure aborts the remaining items
    ('p-cr2', 40, 0),
]


def test_backorder_characterization():
    cursor = _backorder(_BACKORDER_ITEMS, existing_runs={'p-det1'}, no_return={'p-boom'})
    assert [_mask(*c) for c in without_isolation(cursor.calls)] == _GOLDEN_BACKORDER


# --------------------------------------------------------------------------
# The shared FG-location map now reaches replenishment and backorder planning.
# --------------------------------------------------------------------------
_HEAD_CASE = ("CASE s.site_code WHEN 'MILWAUKEE_BOTTLING' THEN 'MKE-FG' WHEN 'DETROIT_AUTOPARTS' "
              "THEN 'DET-FG' WHEN 'CEDARRAPIDS_FOOD' THEN 'CR-FG' WHEN 'BOTTLING_PLANT' "
              "THEN 'FG-WAREHOUSE' ELSE 'FG-WAREHOUSE' END")
_IND_SITE = _PRODUCTS['p-ind1'][2]
_IND_FG = production_simulator.SITE_FG_LOCATION[_IND_SITE]


def _branches(case_sql):
    return re.findall(r"WHEN '([A-Z_]+)' THEN '([A-Z-]+)'", case_sql)


def test_fg_case_renders_the_one_map():
    case = production_simulator.fg_location_case_sql('s.site_code')
    assert _CASE_RE.fullmatch(case)
    assert dict(_branches(case)) == production_simulator.SITE_FG_LOCATION
    # every HEAD branch survives unchanged; the addition is the machining
    # site (ADR-0072).
    assert set(_branches(_HEAD_CASE)) < set(_branches(case))
    assert set(_branches(case)) - set(_branches(_HEAD_CASE)) == {
        (_IND_SITE, _IND_FG)}


def test_one_shared_map_object():
    from erp_simulation import state_advancer
    assert ProductionSimulator.SITE_FG_LOCATION is production_simulator.SITE_FG_LOCATION
    assert state_advancer.SITE_FG_LOCATION is production_simulator.SITE_FG_LOCATION


def test_replenishment_sql_resolves_ind_fg():
    _, cursor = _replenish(candidates=[_candidate('p-ind1')])
    replenish_sql = next(sql for sql, _ in cursor.calls if sql.startswith('WITH pipeline_production'))
    assert f"WHEN '{_IND_SITE}' THEN '{_IND_FG}'" in replenish_sql
    assert ('SELECT location_id FROM', (_IND_FG,)) in [
        (' '.join(sql.split()[:3]), p) for sql, p in cursor.calls]
    insert = next(p for sql, p in cursor.calls if sql.startswith('INSERT INTO production_runs'))
    assert insert[1] == 'p-ind1' and insert[4] == _LOCATIONS[_IND_FG] and insert[5] == _IND_SITE


def test_backorder_run_for_ind_product_deposits_to_ind_fg():
    cursor = _backorder([('p-ind1', 30, 0)])
    assert [p for sql, p in cursor.calls if 'FROM inventory_locations' in sql] == [(_IND_FG,)]
    insert = next(p for sql, p in cursor.calls if sql.startswith('INSERT INTO production_runs'))
    assert insert[1:3] == ('p-ind1', 'order-1')
    assert insert[5] == _LOCATIONS[_IND_FG] and insert[6] == _IND_SITE


def test_unmapped_site_falls_back_to_default_location():
    loc = production_simulator.DEFAULT_FG_LOCATION
    sim = ProductionSimulator.__new__(ProductionSimulator)
    cursor = _FakeCursor()
    assert sim._active_fg_location_id(cursor, 'UNMAPPED_SITE', 'SKU') == _LOCATIONS[loc]
    assert cursor.calls[-1][1] == (loc,)


# --------------------------------------------------------------------------
# Real PostgreSQL (throwaway database): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# --------------------------------------------------------------------------
from erp_simulation.tests._pg_sql import stmt  # noqa: E402

_HANDLER_PATH = Path(__file__).resolve().parents[2] / 'lambda-db-init' / 'handler.py'
_DSN = os.environ.get('ERP_SIM_TEST_PG_DSN')


def _load_db_init():
    spec = importlib.util.spec_from_file_location('db_init_handler_t4b', _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pg_conn():
    if not _DSN:
        pytest.skip('set ERP_SIM_TEST_PG_DSN to run against PostgreSQL')
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    db_name = 't4b_' + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt('CREATE DATABASE {db}', db=db_name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), 'dbname': db_name}))
    try:
        handler = _load_db_init()
        handler.create_schema(conn)
        handler.seed_data(conn)
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt('DROP DATABASE {db}', db=db_name))
        admin.close()


_IND_FG_BALANCES_SQL = """
    SELECT ii.product_id, ib.quantity_on_hand
    FROM inventory_balances ib
    JOIN inventory_items ii ON ii.item_id = ib.item_id AND ii.item_type = 'FINISHED_GOOD'
    JOIN inventory_locations il ON il.location_id = ib.location_id
    WHERE il.location_code = %s
"""


def _pg_simulator():
    sim = ProductionSimulator.__new__(ProductionSimulator)
    sim.production_lines = ['LINE1', 'LINE2', 'LINE3']
    return sim


def test_pg_replenishment_reads_ind_fg_stock(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_IND_FG_BALANCES_SQL, (_IND_FG,))
        seeded = {str(pid): float(qty) for pid, qty in cur.fetchall()}
        assert seeded, 'seed must stock the machining FG location'
        rows = _pg_simulator()._replenishment_candidates(cur, 10 ** 9)
    ind = {str(r[0]): float(r[3]) for r in rows if r[5] == _IND_SITE}
    assert ind == seeded  # current_inventory is the IND-FG balance, not FG-WAREHOUSE's


def test_pg_ind_stock_above_reorder_point_plans_no_ind_run(pg_conn):
    sim = _pg_simulator()
    with pg_conn.cursor() as cur:
        reorder_point = sim._load_replenishment_params(cur)[2]
        cur.execute("""
            UPDATE inventory_balances SET quantity_on_hand = %s
            WHERE location_id = (SELECT location_id FROM inventory_locations WHERE location_code = %s)
        """, (reorder_point + 1000, _IND_FG))
        assert cur.rowcount > 0
        cur.execute("SELECT count(*) FROM production_runs WHERE site = %s", (_IND_SITE,))
        before = cur.fetchone()[0]
    pg_conn.commit()
    sim.create_production_runs(pg_conn, 'cycle-pg')
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM production_runs WHERE site = %s", (_IND_SITE,))
        assert cur.fetchone()[0] == before


def test_pg_backorder_run_for_ind_product_deposits_to_ind_fg(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_IND_FG_BALANCES_SQL, (_IND_FG,))
        product_id = cur.fetchall()[0][0]
        cur.execute("UPDATE production_runs SET status = 'COMPLETED' WHERE product_id = %s", (product_id,))
        StateAdvancer.__new__(StateAdvancer)._create_production_for_backorder(
            cur, None, 'ORD-PG', [(product_id, 500, 0)])
        cur.execute("""
            SELECT il.location_code, pr.site FROM production_runs pr
            JOIN inventory_locations il ON il.location_id = pr.location_id
            WHERE pr.product_id = %s AND pr.status = 'PLANNED'
        """, (product_id,))
        assert cur.fetchall() == [(_IND_FG, _IND_SITE)]
