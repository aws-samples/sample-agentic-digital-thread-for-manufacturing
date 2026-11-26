# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PO receipts create material lots (lot traceability keeps flowing).

Live evidence (2026-09-24): only the seed ever created material lots, so they
ran out within weeks; production then allocated no lots and ERP lot tracking
stopped (~2026-08-08), breaking MES work order -> ERP supplier traceability.

    python -m pytest erp_simulation/tests -q
"""
from __future__ import annotations

from erp_simulation.procurement_simulator import create_receipt_lot


class FakeCursor:
    def __init__(self, location_row=("loc-mke-raw", "EA"), rowcount=1):
        self.location_row = location_row
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.location_row


def _call(cur, **overrides):
    kw = dict(po_id="po-1", po_number="PO-20260924-0007", supplier_id="sup-1",
              item_id="item-1", quantity=500, line_no=3)
    kw.update(overrides)
    return create_receipt_lot(cur, **kw)


def test_receipt_inserts_an_approved_lot_at_the_items_existing_location():
    cur = FakeCursor()
    assert _call(cur) is True
    (select_sql, select_params), (insert_sql, insert_params) = cur.calls
    assert "FROM material_lots" in select_sql and select_params == ("item-1",)
    assert insert_sql.startswith("INSERT INTO material_lots")
    assert "'APPROVED'" in insert_sql and "ON CONFLICT (lot_number) DO NOTHING" in insert_sql
    assert insert_params == ("LOT-PO-20260924-0007-03", "item-1", "sup-1", "po-1",
                             500, 500, "EA", "loc-mke-raw")


def test_item_without_a_known_lot_location_is_skipped_not_guessed():
    cur = FakeCursor(location_row=None)
    assert _call(cur) is False
    assert len(cur.calls) == 1  # only the location lookup, no INSERT


def test_re_receipt_is_idempotent():
    assert _call(FakeCursor(rowcount=0)) is False
