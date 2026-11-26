# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline pins for the ERP↔PLM engineering-part cross-reference seed.

The live ERP products (lambda-db-init seed_products) carry their PLM part
number in products.engineering_part_number; the PLM items live in
lambda-adt-seed/005_plm_engineering.sql (plm_engineering.products.item_id).

Pins:
* every seeded cross-reference points at a PLM item that actually exists;
* every machined (MACH-*) product the fourth site seeds maps to a Released
  PLM item (the one-ERP customer-impact leg joins ERP products to PLM);
* exactly ONE live AUTO-* product is deliberately left without a cross-ref
  (AUTO-ELEC-ECU — honest imperfection, per the demo-realism requirement);
* the MEASURED value overlap (agents.resolve_evidence.value_overlap — the
  same function onboarding uses) between the ERP field values and the PLM
  item ids is ≥ 0.8, so the cross-system join lights up organically;
* both DDL homes for the ERP products table define the column (including the
  idempotent ALTER for pre-existing databases).

Fully offline: the handler module is parsed with ast (never imported), the
SQL seed is parsed as text.
"""

import ast
import re
import sys
from pathlib import Path

# Repo root (for lambda-db-init/, lambda-adt-seed/, erp_simulation/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.resolve_evidence import value_overlap  # noqa: E402

_HANDLER_PATH = _REPO_ROOT / "lambda-db-init" / "handler.py"
_PLM_SEED_PATH = _REPO_ROOT / "lambda-adt-seed" / "005_plm_engineering.sql"
_ERP_SCHEMA_PATH = _REPO_ROOT / "erp_simulation" / "schema" / "02_products_inventory.sql"


def _load_xref() -> dict:
    """Extract the PLM_PART_XREF dict literal from the handler via ast."""
    tree = ast.parse(_HANDLER_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PLM_PART_XREF":
                    return ast.literal_eval(node.value)
    raise AssertionError("PLM_PART_XREF not found in lambda-db-init/handler.py")


def _plm_item_ids() -> set:
    """All plm_engineering.products item_ids from the seed SQL."""
    sql = _PLM_SEED_PATH.read_text()
    ids = set()
    for block in re.findall(r"INSERT INTO products VALUES\s*(.*?);", sql, re.S):
        ids.update(re.findall(r"\('(PN-[A-Za-z0-9-]+)'", block))
    assert ids, "no PLM product item_ids parsed — seed layout changed?"
    return ids


def _released_plm_item_ids() -> set:
    """item_ids of PLM product rows whose lifecycle state is Released."""
    rows = re.findall(r"\('(PN-[A-Za-z0-9-]+)',(.*?)\)", _PLM_SEED_PATH.read_text())
    return {item_id for item_id, rest in rows if "'Released'" in rest}


def _machined_skus() -> list:
    """The MACH-* ERP product SKUs seeded by the handler's machining site."""
    return sorted(set(re.findall(r"\('(MACH-[A-Z0-9-]+)', '", _HANDLER_PATH.read_text())))


def _live_auto_skus() -> list:
    """The live AUTO-* ERP product SKUs from the handler's seed_products."""
    skus = re.findall(r"\('(AUTO-[A-Z0-9-]+)','", _HANDLER_PATH.read_text())
    return sorted(set(skus))


def test_every_xref_targets_an_existing_plm_item():
    xref = _load_xref()
    plm_ids = _plm_item_ids()
    missing = {sku: pn for sku, pn in xref.items() if pn not in plm_ids}
    assert not missing, f"cross-refs point at non-existent PLM items: {missing}"


def test_every_machined_product_maps_to_a_released_plm_item():
    xref = _load_xref()
    released = _released_plm_item_ids()
    machined = _machined_skus()
    assert len(machined) == 4, f"expected the 4 machined products, got {machined}"
    unmapped = [sku for sku in machined if xref.get(sku) not in released]
    assert not unmapped, f"machined products without a Released PLM item: {unmapped}"


def test_exactly_one_auto_product_deliberately_unlinked():
    xref = _load_xref()
    auto_skus = _live_auto_skus()
    assert len(auto_skus) == 5, f"expected the 5 live AUTO-* products, got {auto_skus}"
    unlinked = [sku for sku in auto_skus if sku not in xref]
    assert unlinked == ["AUTO-ELEC-ECU"], (
        "exactly AUTO-ELEC-ECU must stay without a cross-ref (honest imperfection)"
    )


def test_value_overlap_at_least_point_eight():
    """Measured with the SAME function onboarding uses (organic overlap)."""
    erp_values = list(_load_xref().values())  # what sampling reads (NULLs excluded)
    plm_values = sorted(_plm_item_ids())
    result = value_overlap(erp_values, plm_values)
    assert result["ratio"] >= 0.8, f"ERP↔PLM value overlap too weak: {result}"


def test_both_ddl_homes_define_the_column():
    for path in (_ERP_SCHEMA_PATH, _HANDLER_PATH):
        text = path.read_text()
        assert "engineering_part_number VARCHAR(50)" in text, path
        assert (
            "ADD COLUMN IF NOT EXISTS engineering_part_number" in text
        ), f"{path} must carry the idempotent migration for existing databases"
