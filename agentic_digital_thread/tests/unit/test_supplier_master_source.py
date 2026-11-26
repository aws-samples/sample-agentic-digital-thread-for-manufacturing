# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Supplier-master demo source for the ValueMapping crosswalk (piece 2).

Pins:
* the supplier master is a DEMO-ONLY system — registered when workshop is
  DISABLED, and NEVER in any workshop system list or bulk-onboard default
  (so the ../adt-workshop content is untouched);
* its schema routes to the SEED database (no erp_/mes_ prefix);
* the ERP carries deterministic ANCHOR_SUPPLIERS whose names match the
  supplier master's vendors under DIFFERENT ids (the crosswalk's real match).

    PYTHONPATH=. python -m pytest tests/unit/test_supplier_master_source.py -q
"""
from __future__ import annotations

from agents.demo import workshop
from agents.demo import db_routing


def _supplier_master():
    return workshop.SUPPLIER_MASTER_SYSTEM


def test_supplier_master_is_registered_in_demo_mode():
    ids = {s["id"] for s in workshop.DEMO_PLANT_SYSTEMS}
    assert "supplier-master" in ids


def test_supplier_master_is_not_in_any_workshop_system_list():
    # Never seeded, never register-only in the workshop, never bulk-onboarded:
    # the workshop content cannot see it.
    for story in ("frozen", "live"):
        seeded = {s["id"] for s in workshop.seeded_systems(story)}
        registered = {s["id"] for s in workshop.registered_systems(story)}
        assert "supplier-master" not in seeded
        assert "supplier-master" not in registered
    assert "supplier-master" not in {s["id"] for s in workshop.WORKSHOP_SYSTEMS}
    assert "supplier-master" not in workshop.seeded_system_ids("frozen")
    assert "supplier-master" not in workshop.seeded_system_ids("live")


def test_supplier_master_schema_routes_to_seed_db():
    # supplier_master has no erp_/mes_ prefix ⇒ NOT an ERP-family schema ⇒ seed DB.
    assert db_routing.is_erp_schema("supplier_master") is False


def test_supplier_master_identity_shape():
    sm = _supplier_master()
    assert sm["schema"] == "supplier_master"
    assert sm["connection_type"] == "PostgreSQL"
    # A tool-name-safe id (no re-slugify from the display name).
    assert sm["id"] == "supplier-master"


def test_manifest_registers_supplier_master_only_when_workshop_disabled(monkeypatch):
    # workshop disabled ⇒ DEMO_PLANT_SYSTEMS (incl. supplier-master) is appended.
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    ids = {s["id"] for s in workshop.DEMO_PLANT_SYSTEMS}
    assert "supplier-master" in ids
