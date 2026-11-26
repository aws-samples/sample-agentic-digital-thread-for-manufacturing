# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OTD delivery-time variability (erp_simulation.delivery_timing).

Reproduces the pre-feature gap: with no opt-in config the offset is 0.0 and the
receipt records actual_delivery_date = NOW() (at or after the expected date, so
every supplier shows the same OTD), and pins the opt-in, per-supplier spread.

    python -m pytest erp_simulation/tests/test_delivery_timing.py -q
"""
from __future__ import annotations

import statistics

from erp_simulation.delivery_timing import (
    DELIVERY_VARIABILITY_KEY,
    delivery_offset_minutes,
)


def test_default_zero_variability_is_byte_identical_offset_is_zero():
    # The GAP: unset/zero lever ⇒ offset 0.0, so the receipt records NOW()
    # (today's behavior, and the workshop's). This is what makes the feature
    # safe-by-default — see test_receive_one_po_zero_offset_uses_now_sql for the
    # SQL branch that consumes a 0.0 offset.
    for po in ("PO-20260101-00001", "PO-20991231-99999", "", "PO-X"):
        assert delivery_offset_minutes(po, 0.0) == 0.0
        assert delivery_offset_minutes(po, 0) == 0.0


def test_bad_config_values_fail_safe_to_zero():
    for bad in (-5.0, float("nan"), float("inf"), None, "abc"):
        assert delivery_offset_minutes("PO-20260101-00001", bad) == 0.0


def test_offset_is_deterministic_per_po_number():
    a = delivery_offset_minutes("PO-20260101-00042", 120.0)
    b = delivery_offset_minutes("PO-20260101-00042", 120.0)
    assert a == b  # same PO ⇒ same offset (idempotent re-receipt)


def test_offset_stays_within_the_window_and_both_signs_occur():
    window = 120.0
    offsets = [delivery_offset_minutes(f"PO-2026-{i:05d}", window) for i in range(500)]
    # Bounded: never outside [-0.5*window, +window].
    assert min(offsets) >= -window * 0.5 - 0.001
    assert max(offsets) <= window + 0.001
    # A believable mix: some early (negative), some late (positive).
    assert any(o < 0 for o in offsets)
    assert any(o > 0 for o in offsets)


def test_spread_without_a_supplier_uses_the_default_share():
    # No supplier key ⇒ ~85% on or before the expected date.
    offsets = [delivery_offset_minutes(f"PO-2026-{i:05d}", 120.0) for i in range(2000)]
    on_time = sum(1 for o in offsets if o <= 0)
    assert 0.80 < on_time / len(offsets) < 0.90


def test_config_key_name_is_stable():
    # Pinned: scenarios.py / demo config write this exact key.
    assert DELIVERY_VARIABILITY_KEY == "procurement_delivery_variability_minutes"


def test_huge_finite_value_is_clamped_to_the_declared_max():
    # A huge finite config value must NOT pass through — *60 and make_interval
    # would overflow and abort PO receipt. It is clamped to the declared max
    # (1440 min), so the offset stays within that window's bounds.
    from erp_simulation.delivery_timing import _MAX_VARIABILITY_MINUTES
    huge = 1e18
    offsets = [delivery_offset_minutes(f"PO-2026-{i:05d}", huge) for i in range(200)]
    assert max(offsets) <= _MAX_VARIABILITY_MINUTES + 0.001
    assert min(offsets) >= -_MAX_VARIABILITY_MINUTES * 0.5 - 0.001
    # Clamped exactly to the max window: identical to passing the max directly.
    assert delivery_offset_minutes("PO-2026-00042", huge) == \
        delivery_offset_minutes("PO-2026-00042", _MAX_VARIABILITY_MINUTES)


class _FakeCursor:
    """Records (sql, params) for each execute; no line items to receive."""
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return []  # no PO line items, so _receive_po_line is never called

    def close(self):
        pass


def test_receive_one_po_zero_offset_uses_now_sql():
    # Offset 0 (default) must take the legacy branch: actual_delivery_date is a
    # passed timestamp (NOW()-equivalent), NOT expected + make_interval.
    from erp_simulation.procurement_simulator import ProcurementSimulator
    cur = _FakeCursor()
    ProcurementSimulator._receive_one_po(
        cur, po_id=1, po_number="PO-20260101-00001", supplier_id=1,
        location_id=1, variability_minutes=0.0)
    update = next(sql for sql, _ in cur.calls if "status = 'RECEIVED'" in sql)
    assert "make_interval" not in update
    assert "actual_delivery_date = %s" in update


def test_receive_one_po_positive_offset_uses_make_interval_sql():
    # A positive spread must take the OTD branch: expected + make_interval.
    from erp_simulation.procurement_simulator import ProcurementSimulator
    cur = _FakeCursor()
    ProcurementSimulator._receive_one_po(
        cur, po_id=1, po_number="PO-20260101-00001", supplier_id=1,
        location_id=1, variability_minutes=120.0)
    update, params = next((sql, p) for sql, p in cur.calls
                          if "status = 'RECEIVED'" in sql)
    assert "expected_delivery_date + make_interval(secs => %s)" in update
    # The seconds param is the PO's offset * 60 (no supplier code: default share).
    assert params[0] == delivery_offset_minutes(
        "PO-20260101-00001", 120.0, supplier_key=None) * 60.0


def _on_time_share(supplier_key, n=2000, window=120.0):
    offsets = [delivery_offset_minutes(f"PO-2026-{i:05d}", window, supplier_key=supplier_key)
               for i in range(n)]
    return sum(1 for o in offsets if o <= 0) / n


def test_suppliers_differ_in_on_time_share():
    # The demo needs an OTD-by-supplier view with something to show: reliable
    # suppliers and poor ones, not the same rate for everyone.
    shares = [_on_time_share(str(s), n=1000) for s in range(1, 41)]
    assert max(shares) - min(shares) >= 0.20
    assert max(shares) >= 0.90
    assert min(shares) <= 0.75


def test_overall_on_time_share_leans_on_time():
    shares = [_on_time_share(str(s), n=500) for s in range(1, 201)]
    assert 0.78 <= statistics.mean(shares) <= 0.92


def test_supplier_share_is_stable_and_bounded():
    from erp_simulation.delivery_timing import supplier_on_time_share
    for s in range(1, 300):
        p = supplier_on_time_share(str(s))
        assert 0.60 <= p <= 0.97
        assert p == supplier_on_time_share(str(s))


def test_on_time_and_late_offsets_have_the_right_sign_and_bounds():
    window = 120.0
    for i in range(3000):
        o = delivery_offset_minutes(f"PO-2026-{i:05d}", window, supplier_key="7")
        assert -window * 0.5 - 0.001 <= o <= window + 0.001


def test_receive_one_po_offsets_by_its_supplier():
    from erp_simulation.procurement_simulator import ProcurementSimulator
    cur = _FakeCursor()
    ProcurementSimulator._receive_one_po(
        cur, po_id=1, po_number="PO-20260101-00001", supplier_id=17,
        supplier_code="SUPP-17", location_id=1, variability_minutes=120.0)
    _, params = next((sql, p) for sql, p in cur.calls if "status = 'RECEIVED'" in sql)
    assert params[0] == delivery_offset_minutes(
        "PO-20260101-00001", 120.0, supplier_key="SUPP-17") * 60.0


def test_positive_spread_never_falls_back_to_now(monkeypatch):
    # With a spread configured, a PO whose offset happens to be 0.0 is still
    # dated from the plan, not stamped with the receipt time.
    from erp_simulation import procurement_simulator as ps
    monkeypatch.setattr(ps, "delivery_offset_minutes", lambda *a, **k: 0.0)
    cur = _FakeCursor()
    ps.ProcurementSimulator._receive_one_po(
        cur, po_id=1, po_number="PO-20260101-00001", supplier_id=17,
        location_id=1, variability_minutes=120.0)
    update = next(sql for sql, _ in cur.calls if "status = 'RECEIVED'" in sql)
    assert "make_interval" in update


def test_tiny_positive_window_stays_within_bounds():
    # A late PO's floor must not push the offset past a sub-0.001 window.
    window = 0.0005
    for i in range(500):
        o = delivery_offset_minutes(f"PO-2026-{i:05d}", window, supplier_key="x")
        assert -window * 0.5 - 1e-9 <= o <= window + 1e-9


class _ReadyCursor(_FakeCursor):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def fetchall(self):
        return self._rows


def test_ready_pos_carry_the_stable_supplier_code():
    # supplier_id is a fresh random UUID on every full reset, so a supplier's
    # reliability must come from its stable supplier_code.
    from erp_simulation.procurement_simulator import ProcurementSimulator
    cur = _ReadyCursor([(1, "PO-1", "uuid-a", "SUPP-VM-01")])
    rows = ProcurementSimulator._pos_ready_to_receive(cur)
    sql = cur.calls[0][0]
    assert "supplier_code" in sql and "JOIN suppliers" in sql
    assert rows == [(1, "PO-1", "uuid-a", "SUPP-VM-01")]


def test_receive_one_po_keys_reliability_by_supplier_code_not_uuid():
    from erp_simulation.procurement_simulator import ProcurementSimulator
    seconds = []
    for uuid_after_reset in ("6f1c-old", "9a2e-new"):
        cur = _FakeCursor()
        ProcurementSimulator._receive_one_po(
            cur, po_id=1, po_number="PO-20260101-00001", supplier_id=uuid_after_reset,
            supplier_code="SUPP-VM-01", location_id=1, variability_minutes=120.0)
        _, params = next((sql, p) for sql, p in cur.calls if "status = 'RECEIVED'" in sql)
        seconds.append(params[0])
    assert seconds[0] == seconds[1] == delivery_offset_minutes(
        "PO-20260101-00001", 120.0, supplier_key="SUPP-VM-01") * 60.0


def test_small_windows_keep_both_ends_in_bounds():
    # Rounding to 3 decimals must not push either end past the window.
    for window in (0.0005, 0.001, 0.0011, 0.0015, 0.0019, 0.002):
        for i in range(1000):
            o = delivery_offset_minutes(f"PO-2026-{i:05d}", window, supplier_key="x")
            assert -window * 0.5 - 1e-12 <= o <= window + 1e-12, (window, o)


class _ArrivalCursor(_FakeCursor):
    """Answers the arrival check: (True,) means the late PO has not arrived."""
    def __init__(self, not_arrived):
        super().__init__()
        self._not_arrived = not_arrived

    def fetchone(self):
        return (self._not_arrived,)


def _late_po_number(supplier_code="SUPP-17"):
    for i in range(1000):
        po = f"PO-2026-{i:05d}"
        if delivery_offset_minutes(po, 120.0, supplier_key=supplier_code) > 0:
            return po
    raise AssertionError("no late PO in range")


def _early_po_number(supplier_code="SUPP-17"):
    for i in range(1000):
        po = f"PO-2026-{i:05d}"
        if delivery_offset_minutes(po, 120.0, supplier_key=supplier_code) <= 0:
            return po
    raise AssertionError("no early PO in range")


def test_a_late_po_stays_in_transit_until_its_delivery_time_has_passed():
    # A late PO must not be received (and stamped with a delivery time still in
    # the future) as soon as it is due; it waits until expected + offset.
    from erp_simulation.procurement_simulator import ProcurementSimulator as PS
    po = _late_po_number()
    cur = _ArrivalCursor(not_arrived=True)
    assert PS._skip_this_cycle(cur, po_id=1, po_number=po, supplier_code="SUPP-17",
                               reliability=1.0, variability_minutes=120.0) is True
    sql, params = cur.calls[-1]
    assert "make_interval(secs => %s) > NOW()" in sql
    assert params[0] == delivery_offset_minutes(po, 120.0, supplier_key="SUPP-17") * 60.0
    cur = _ArrivalCursor(not_arrived=False)
    assert PS._skip_this_cycle(cur, po_id=1, po_number=po, supplier_code="SUPP-17",
                               reliability=1.0, variability_minutes=120.0) is False


def test_an_early_po_is_received_when_due_without_an_arrival_check():
    from erp_simulation.procurement_simulator import ProcurementSimulator as PS
    cur = _ArrivalCursor(not_arrived=True)
    assert PS._skip_this_cycle(cur, po_id=1, po_number=_early_po_number(), supplier_code="SUPP-17",
                               reliability=1.0, variability_minutes=120.0) is False
    assert cur.calls == []


def test_no_spread_never_runs_an_arrival_check():
    # Default / workshop: byte-identical — only the reliability draw decides.
    from erp_simulation.procurement_simulator import ProcurementSimulator as PS
    cur = _ArrivalCursor(not_arrived=True)
    assert PS._skip_this_cycle(cur, po_id=1, po_number=_late_po_number(), supplier_code="SUPP-17",
                               reliability=1.0, variability_minutes=0.0) is False
    assert cur.calls == []


def test_receive_pos_skips_a_late_po_that_has_not_arrived(monkeypatch):
    from erp_simulation import procurement_simulator as ps
    received = []
    monkeypatch.setattr(ps.ProcurementSimulator, "_advance_to_in_transit", staticmethod(lambda c: 0))
    monkeypatch.setattr(ps.ProcurementSimulator, "_pos_ready_to_receive",
                        staticmethod(lambda c: [(1, "PO-A", "u1", "SUPP-1"), (2, "PO-B", "u2", "SUPP-2")]))
    monkeypatch.setattr(ps.ProcurementSimulator, "_raw_materials_location", staticmethod(lambda c: 9))
    monkeypatch.setattr(ps.ProcurementSimulator, "_skip_this_cycle",
                        staticmethod(lambda c, **k: k["po_number"] == "PO-A"))
    monkeypatch.setattr(ps.ProcurementSimulator, "_receive_one_po",
                        staticmethod(lambda c, **k: received.append(k["po_number"]) or 0))
    sim = ps.ProcurementSimulator.__new__(ps.ProcurementSimulator)
    sim._load_config_value = lambda cur, key, default, kind: 120.0 if "variability" in key else 1.0

    class _Conn:
        def cursor(self):
            return _FakeCursor()
    result = sim.receive_pos(_Conn(), "cycle-1")
    assert received == ["PO-B"] and result["pos_received"] == 1


def test_a_waiting_late_po_makes_no_reliability_draw_until_it_arrives(monkeypatch):
    # Re-drawing reliability every cycle while a late PO waits compounds it
    # (0.5 reliability over ~60 cycles never passes) and each failure pushes the
    # expected date out again. One draw, after arrival.
    from erp_simulation.procurement_simulator import ProcurementSimulator as PS
    draws = []
    monkeypatch.setattr(PS, "_maybe_delay_delivery",
                        staticmethod(lambda *a: draws.append(a) or False))
    po = _late_po_number()
    for _ in range(5):
        assert PS._skip_this_cycle(_ArrivalCursor(not_arrived=True), po_id=1, po_number=po,
                                   supplier_code="SUPP-17", reliability=0.5,
                                   variability_minutes=120.0) is True
    assert draws == []
    assert PS._skip_this_cycle(_ArrivalCursor(not_arrived=False), po_id=1, po_number=po,
                               supplier_code="SUPP-17", reliability=0.5,
                               variability_minutes=120.0) is False
    assert len(draws) == 1


from erp_simulation.tests.test_fg_location_routing import pg_conn  # noqa: E402,F401  (fixture)


def test_pg_a_delayed_delivery_moves_the_expected_date_by_whole_days(pg_conn, monkeypatch):  # noqa: F811
    """The delay is a bound day count (``make_interval(days => %s)``): a
    placeholder inside the literal ``INTERVAL '%s days'`` cannot be bound."""
    from datetime import datetime, timedelta

    from erp_simulation import procurement_simulator
    from erp_simulation.procurement_simulator import ProcurementSimulator
    monkeypatch.setattr(procurement_simulator.random, "random", lambda: 1.0)   # always delayed
    monkeypatch.setattr(procurement_simulator.random, "randint", lambda a, b: 3)
    expected = datetime(2026, 3, 1, 8, 30)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT supplier_id FROM suppliers ORDER BY supplier_id LIMIT 1")
        supplier_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO purchase_orders (po_number, supplier_id, po_date, expected_delivery_date, status)
                       VALUES ('PO-20260301-99001', %s, NOW(), %s, 'IN_TRANSIT') RETURNING po_id""",
                    (supplier_id, expected))
        po_id = cur.fetchone()[0]
        assert ProcurementSimulator._maybe_delay_delivery(cur, po_id, "PO-20260301-99001", 0.5) is True
        cur.execute("SELECT expected_delivery_date FROM purchase_orders WHERE po_id = %s", (po_id,))
        assert cur.fetchone()[0] == expected + timedelta(days=3)
    pg_conn.rollback()
