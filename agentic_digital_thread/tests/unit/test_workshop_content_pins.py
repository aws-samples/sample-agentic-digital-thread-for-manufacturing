# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The workshop-content-pin fixture is LOAD-BEARING, offline.

The fixture (``tests/fixtures/workshop_content_pins.json``) records the exact
values the ``../adt-workshop`` content modules present as expected answers for
the frozen story. A live frozen-story check asserts the deployed app still
returns them; these OFFLINE tests assert that the demo-data upgrades on this
branch (PO delivery variability + the supplier-master crosswalk) *cannot*
move those values by construction -- so the fixture protects a real invariant
even without a running stack, which is what makes it worth keeping.

    python -m pytest tests/unit/test_workshop_content_pins.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

_FIXTURE = (Path(__file__).resolve().parents[1]
            / "fixtures" / "workshop_content_pins.json")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _pins() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_loads_and_pins_the_frozen_lots():
    pins = _pins()
    assert pins["story"] == "frozen"
    assert pins["lot_numbers"] == [
        "LOT-IND-BARAL7075150-H2240", "LOT-IND-BARAL7075150-H2231"]


def _erp_sim_dir() -> Path:
    return _REPO_ROOT / "erp_simulation"


def test_demo_replenishment_lot_namespace_cannot_collide_with_protected_lots():
    # The OTD feature shapes NEW replenishment-PO receipt lots, numbered
    # f"LOT-{po_number}-{line_no:02d}" where po_number is "PO-YYYYMMDD-#####".
    # The protected frozen lots are "LOT-IND-...". Prove the demo lot shape
    # can never produce a protected lot number, so a demo PO can't overwrite
    # the story lot the workshop content pins. (Read by path -- the app unit
    # lane cannot import erp_simulation.) Rather than ASSUME the 'PO-' prefix,
    # verify it at its real source.
    ps = (_erp_sim_dir() / "procurement_simulator.py").read_text(encoding="utf-8")
    utils = (_erp_sim_dir() / "utils.py").read_text(encoding="utf-8")
    # The receipt-lot format string is the one source of demo lot numbers.
    assert 'f"LOT-{po_number}-{int(line_no):02d}"' in ps, \
        "receipt-lot format changed; re-verify it cannot produce a LOT-IND-... number"
    # po_number comes from insert_numbered(cursor, 'PO', ...) -> next_business_number,
    # which formats "{prefix}-YYYYMMDD-digits". Pin both so a prefix change breaks this.
    assert "insert_numbered(cursor, 'PO'," in ps, \
        "PO insert prefix changed; re-verify the demo lot prefix"
    assert 'f"{prefix}-{(now or datetime.now()):%Y%m%d}-{value:0{digits}d}"' in utils, \
        "business-number format changed; re-verify the demo lot prefix"
    # Therefore every demo lot begins 'LOT-PO-'. The protected lots do not.
    protected = _pins()["lot_numbers"]
    assert protected, "fixture has no protected lot numbers"
    for lot in protected:
        assert lot.startswith("LOT-IND-")
        assert not lot.startswith("LOT-PO-")


def test_crosswalk_anchor_names_do_not_touch_the_frozen_chain():
    # The supplier-master crosswalk anchors are generic placeholder company
    # names. None of them is the frozen story's material code, batch, product,
    # equipment or work order, so adding them cannot shadow a pinned answer.
    # (Read the anchor company_name literals from seed_data.py by path.)
    import re
    src = (_erp_sim_dir() / "seed_data.py").read_text(encoding="utf-8")
    # Anchor on the ASSIGNMENT, not the first mention (which is a comment), and
    # stop at the closing paren on its own line.
    anchor_block = src.split("ANCHOR_SUPPLIERS: tuple[dict, ...] = (", 1)[1].split("\n)", 1)[0]
    anchor_values = re.findall(r"'([^']+)'|\"([^\"]+)\"", anchor_block)
    anchor_values = [a or b for a, b in anchor_values]
    assert anchor_values, "found no anchor values to check -- extraction broke"
    anchor_text = " ".join(anchor_values)
    pins = _pins()
    reserved = {
        pins["material_code"], pins["batch_defect"], pins["product"],
        *pins["equipment"], *pins["work_orders"], *pins["lot_numbers"],
    }
    for token in reserved:
        assert token not in anchor_text, \
            f"anchor supplier text collides with protected token {token!r}"


def test_supplier_master_vendors_do_not_reuse_protected_identifiers():
    # The supplier-master seed must not reuse any protected frozen-chain id.
    seed = (_REPO_ROOT / "lambda-adt-seed" / "012_supplier_master.sql").read_text(
        encoding="utf-8")
    pins = _pins()
    reserved = {
        pins["material_code"], pins["batch_defect"], pins["product"],
        *pins["equipment"], *pins["work_orders"], *pins["lot_numbers"],
    }
    for token in reserved:
        assert token not in seed, \
            f"supplier-master seed reuses protected token {token!r}"
