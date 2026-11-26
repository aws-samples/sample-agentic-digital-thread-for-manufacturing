# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ERP anchor suppliers overlap the supplier-master vendors by NAME under
different ids — the real match the ValueMapping crosswalk miner derives.

    python -m pytest erp_simulation/tests/test_supplier_crosswalk_anchors.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

from erp_simulation.seed_data import ANCHOR_SUPPLIERS


def _supplier_master_vendors() -> dict[str, str]:
    """{vendor_number: legal_name} parsed from the supplier-master seed SQL."""
    seed = (Path(__file__).resolve().parents[2]
            / "lambda-adt-seed" / "012_supplier_master.sql").read_text()
    return dict(re.findall(r"'(VM-\d+)',\s*'([^']+)'", seed))


def _db_init_anchor_names() -> set[str]:
    """Anchor supplier company_names the DEPLOYED ERP seed inserts
    (lambda-db-init ERP_CROSSWALK_ANCHOR_SUPPLIERS), parsed by path — the app
    unit lane / this suite cannot import the db-init Lambda module."""
    src = (Path(__file__).resolve().parents[2]
           / "lambda-db-init" / "handler.py").read_text()
    block = src.split("ERP_CROSSWALK_ANCHOR_SUPPLIERS = (", 1)[1].split("\n)", 1)[0]
    # Each tuple: ('CODE', 'Company Name', 'TERMS', N) -> take the 2nd string.
    return {m[1] for m in re.findall(r"\('([^']+)',\s*'([^']+)'", block)}


def test_deployed_erp_seed_carries_the_anchor_names():
    # The crosswalk only works if the DEPLOYED ERP (db-init seed_suppliers),
    # not just erp_simulation's generator, actually inserts the anchor company
    # names the supplier-master overlaps. Pin all three sources together.
    anchor_names = {a["company_name"] for a in ANCHOR_SUPPLIERS}
    db_init_names = _db_init_anchor_names()
    vendor_names = set(_supplier_master_vendors().values())
    assert anchor_names == db_init_names, (
        "erp_simulation ANCHOR_SUPPLIERS and db-init ERP_CROSSWALK_ANCHOR_SUPPLIERS "
        f"drifted: {anchor_names ^ db_init_names}")
    assert anchor_names <= vendor_names, (
        f"anchor names absent from the deployed supplier master: {anchor_names - vendor_names}")


def test_every_erp_anchor_name_appears_in_the_supplier_master():
    vendor_names = set(_supplier_master_vendors().values())
    anchor_names = {a["company_name"] for a in ANCHOR_SUPPLIERS}
    missing = anchor_names - vendor_names
    assert not missing, f"anchor names absent from supplier master: {missing}"


def test_crosswalk_is_a_real_overlap_not_a_total_join():
    # The supplier master also carries vendors with NO ERP anchor match, so the
    # miner must DISCOVER the overlap rather than assume both sides are equal.
    vendor_names = set(_supplier_master_vendors().values())
    anchor_names = {a["company_name"] for a in ANCHOR_SUPPLIERS}
    assert vendor_names - anchor_names, "supplier master should have non-matching vendors too"


def test_ids_differ_in_shape_same_company():
    # Same company, different id shape across systems (SUPP-VM-## vs VM-1000##).
    vendors = _supplier_master_vendors()
    by_name = {name: num for num, name in vendors.items()}
    for a in ANCHOR_SUPPLIERS:
        assert a["supplier_code"].startswith("SUPP-VM-")
        vendor_number = by_name[a["company_name"]]
        assert vendor_number.startswith("VM-")
        assert vendor_number != a["supplier_code"]


def test_anchor_suppliers_are_three_stable_rows():
    assert len(ANCHOR_SUPPLIERS) == 3
    assert [a["supplier_code"] for a in ANCHOR_SUPPLIERS] == [
        "SUPP-VM-01", "SUPP-VM-02", "SUPP-VM-03"]


def _gen():
    # generate_suppliers is pure (uses only random + module constants, never
    # self.db), so a sentinel db is enough to exercise the count contract.
    from erp_simulation.seed_data import SeedDataGenerator
    return SeedDataGenerator(db=object())


def test_generate_suppliers_returns_exactly_count_at_boundaries():
    gen = _gen()
    # count below the anchor count: exactly count rows, all anchors (clamped).
    for n in (0, 1, 2, 3):
        rows = gen.generate_suppliers(count=n)
        assert len(rows) == n, f"count={n} returned {len(rows)}"
        codes = [r["supplier_code"] for r in rows]
        assert codes == [a["supplier_code"] for a in ANCHOR_SUPPLIERS[:n]]


def test_generate_suppliers_returns_exactly_count_when_large():
    # A count far larger than one pass of the type list must still return
    # exactly count (the old finite loop returned ~10-15 regardless).
    rows = _gen().generate_suppliers(count=200)
    assert len(rows) == 200
    # The three anchors come first; the rest are generated.
    assert [r["supplier_code"] for r in rows[:3]] == [
        a["supplier_code"] for a in ANCHOR_SUPPLIERS]
    assert all(r["supplier_code"].startswith("SUPP") for r in rows[3:])
