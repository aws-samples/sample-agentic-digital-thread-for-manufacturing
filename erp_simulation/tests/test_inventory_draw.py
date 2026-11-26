# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inventory draws never leave a negative balance beside a positive one.

MES consumption drew every raw material from the legacy ``WH-RAW`` while the
site warehouse held the seeded stock, so a fresh account showed negative raw
balances within hours (7075 bar stock: IND-WH-RAW 2,400 kg, WH-RAW -530 kg).
``inventory_draw.plan_draw`` spends the preferred location, then the item's
others; ``plan_netting`` clears negatives earlier draws left. Totals hold.
"""
import os
import random
import uuid

import pytest

from erp_simulation import inventory_draw
from erp_simulation.inventory_draw import plan_draw, plan_netting
from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.production_simulator import RAW_LOCATION_CODES, SITE_RAW_LOCATION


def _apply(balances, plan):
    after = dict(balances)
    for location, take in plan:
        after[location] -= take
    return after


def test_the_preferred_location_covers_the_whole_draw():
    assert plan_draw([("site", 500), ("legacy", 900)], 300, "site") == [("site", 300)]


def test_a_short_preferred_location_spills_to_the_fullest_other():
    plan = plan_draw([("site", 100), ("legacy", 900), ("other", 50)], 300, "site")
    assert plan == [("site", 100), ("legacy", 200)]


def test_only_a_shortfall_of_the_total_goes_negative_and_only_on_the_preferred():
    balances = [("site", 100), ("legacy", 50)]
    after = _apply(balances, plan_draw(balances, 400, "site"))
    assert after == {"site": -250, "legacy": 0}


def test_without_a_preference_the_fullest_location_goes_first():
    assert plan_draw([("a", 10), ("b", 40)], 30) == [("b", 30)]
    assert plan_draw([("a", 10), ("b", 40)], 30, "absent") == [("b", 30)]


def test_a_negative_balance_is_never_drawn_from():
    balances = [("site", -80), ("legacy", 200)]
    after = _apply(balances, plan_draw(balances, 150, "site"))
    assert after == {"site": -80, "legacy": 50}


def test_nothing_to_draw_plans_nothing():
    assert plan_draw([("a", 10)], 0, "a") == []
    assert plan_draw([], 10, "a") == []


def test_random_draws_keep_the_total_and_never_split_the_sign():
    rng = random.Random(7)
    for _ in range(500):
        balances = [(f"l{i}", rng.randint(-50, 400)) for i in range(rng.randint(1, 4))]
        quantity = rng.randint(1, 900)
        preferred = rng.choice([b[0] for b in balances] + [None])
        plan = plan_draw(balances, quantity, preferred)
        assert sum(take for _loc, take in plan) == quantity
        after = _apply(balances, plan)
        before = dict(balances)
        for loc, value in after.items():          # a draw never pushes a non-negative stock negative...
            if before[loc] >= 0 and value < 0:     # ...unless the item's total ran short
                assert sum(max(v, 0) for v in before.values()) < quantity
                assert loc == (preferred if preferred in before else max(before, key=before.get))


def test_netting_tops_up_a_negative_from_the_fullest_positive():
    moves = plan_netting([("legacy", -530), ("site", 2400), ("other", 100)])
    assert moves == [("site", "legacy", 530)]


def test_netting_never_takes_a_donor_below_zero():
    balances = [("a", -300), ("b", 100), ("c", 50)]
    after = dict(balances)
    for source, target, quantity in plan_netting(balances):
        after[source] -= quantity
        after[target] += quantity
    assert after == {"a": -150, "b": 0, "c": 0}
    assert sum(after.values()) == sum(dict(balances).values())


def test_netting_with_nothing_negative_moves_nothing():
    assert plan_netting([("a", 0), ("b", 10)]) == []


# ------------------------------------------------------ real PostgreSQL ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture(scope="module")
def pg_erp():
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    with _fresh_erp_db() as conn:
        yield conn


def _split_item(cur, site_code):
    """(item_id, {location_code: on_hand}) for a raw item stocked at the site store,
    with a WH-RAW balance beside it (where PO receipts land; 400 when the seed had none)."""
    cur.execute("""
        SELECT ib.item_id FROM inventory_balances ib
        JOIN inventory_locations il ON il.location_id = ib.location_id
        WHERE il.location_code = %s AND ib.quantity_on_hand > 0
        ORDER BY ib.item_id LIMIT 1
    """, (SITE_RAW_LOCATION[site_code],))
    row = cur.fetchone()
    if not row:
        pytest.skip(f"seed has no raw item stocked at {site_code}")
    cur.execute("""
        INSERT INTO inventory_balances (item_id, location_id, quantity_on_hand)
        SELECT %s, location_id, 400 FROM inventory_locations WHERE location_code = 'WH-RAW'
        ON CONFLICT (item_id, location_id) DO NOTHING
    """, (row[0],))
    return row[0], _balances(cur, row[0])


def _balances(cur, item_id):
    cur.execute("""
        SELECT il.location_code, ib.quantity_on_hand FROM inventory_balances ib
        JOIN inventory_locations il ON il.location_id = ib.location_id
        WHERE ib.item_id = %s AND il.location_code = ANY(%s)
    """, (item_id, list(RAW_LOCATION_CODES)))
    return dict(cur.fetchall())


def test_pg_mes_consumption_draws_the_site_store_first_and_spills_without_a_negative(pg_erp):
    site_code = "INDIANAPOLIS_MACHINING"
    tag = uuid.uuid4().hex[:8].upper()
    with pg_erp.cursor() as cur:
        item_id, before = _split_item(cur, site_code)
        site_loc = SITE_RAW_LOCATION[site_code]
        quantity = before[site_loc] + max(before["WH-RAW"], 0) // 2   # more than the site store holds
        cur.execute("SELECT product_id FROM products ORDER BY product_id LIMIT 1")
        product_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site)
            VALUES (%s, %s, 1, 'IN_PROGRESS', %s) RETURNING run_id
        """, (f"RUN-D-{tag}", product_id, site_code))
        run_id = cur.fetchone()[0]
        cur.execute("INSERT INTO production_materials (run_id, item_id, planned_quantity) VALUES (%s, %s, %s)",
                    (run_id, item_id, quantity))
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id,
                                         planned_quantity, status)
            VALUES (%s, %s, %s, 1, 'RUNNING') RETURNING work_order_id
        """, (f"WO-D-{tag}", run_id, product_id))
        wo_id = cur.fetchone()[0]
        sim = MESSimulator.__new__(MESSimulator)
        sim._load_config_value = lambda *_args: 1.0          # no consumption variance
        sim._consume_materials_for_work_order(cur, wo_id, 1)
        after = _balances(cur, item_id)
        cur.execute("""SELECT count(*), COALESCE(SUM(quantity), 0) FROM inventory_transactions
                       WHERE reference_id = %s AND transaction_type = 'CONSUMPTION'""", (run_id,))
        transactions, posted = cur.fetchone()
    pg_erp.rollback()
    assert after[site_loc] == 0                                   # the site store went first
    assert after["WH-RAW"] == before["WH-RAW"] - (quantity - before[site_loc])
    assert min(after.values()) >= 0
    assert (transactions, posted) == (2, -quantity)               # one CONSUMPTION per location drawn


def test_pg_netting_clears_a_legacy_negative_and_keeps_the_total(pg_erp):
    with pg_erp.cursor() as cur:
        item_id, before = _split_item(cur, "INDIANAPOLIS_MACHINING")
        site_loc = SITE_RAW_LOCATION["INDIANAPOLIS_MACHINING"]
        cur.execute("""UPDATE inventory_balances SET quantity_on_hand = -30
                       WHERE item_id = %s AND location_id =
                             (SELECT location_id FROM inventory_locations WHERE location_code = 'WH-RAW')""",
                    (item_id,))
        broken = _balances(cur, item_id)
        assert inventory_draw.net_split_balances(cur, RAW_LOCATION_CODES) >= 1
        healed = _balances(cur, item_id)
        assert inventory_draw.net_split_balances(cur, RAW_LOCATION_CODES) == 0   # nothing left to net
    pg_erp.rollback()
    assert healed["WH-RAW"] == 0 and healed[site_loc] == broken[site_loc] - 30
    assert sum(healed.values()) == sum(broken.values())
