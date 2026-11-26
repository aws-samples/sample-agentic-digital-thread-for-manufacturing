# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the shared demo-system manifest (agents.demo.systems).

Pins the manifest's contract: the SIX demo SQL systems with their EXACT canonical
ids/names/schemas (the ids the onboarding harness onboards BY), all PostgreSQL, and
tool-name-safe ids. Also pins that the test fixture ``_demo_catalog`` re-uses the
real manifest (DRY) rather than duplicating it.
"""
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.demo.systems import DEMO_SYSTEMS  # noqa: E402
from agents.connection_types import sanitize_system_id  # noqa: E402

# The EXACT canonical identity contract (id -> (name, schema, plant)).
_EXPECTED = {
    "erp": ("ERP", "erp_data", "All Sites"),
    "mes-mil": ("Milwaukee MES", "mes_milwaukee", "Milwaukee"),
    "mes-det": ("Detroit MES", "mes_detroit", "Detroit"),
    "mes-cr": ("Cedar Rapids MES", "mes_cedarrapids", "Cedar Rapids"),
    "cmms": ("CMMS", "cmms_maintenance", "All Sites"),
    "plm": ("PLM", "plm_engineering", "All Sites"),
}


def test_manifest_has_exactly_the_six_sql_systems():
    ids = [s["id"] for s in DEMO_SYSTEMS]
    assert ids == list(_EXPECTED.keys()), ids
    assert len(DEMO_SYSTEMS) == 6


def test_manifest_stays_six_without_the_plant_systems():
    """The demo addon REGISTERS the plant systems too (``manifest_systems``),
    but they are not demo systems: DEMO_SYSTEMS — the bulk-onboard set — stays
    exactly the six."""
    from agents.demo.workshop import DEMO_PLANT_SYSTEMS
    assert DEMO_PLANT_SYSTEMS
    assert not {s["id"] for s in DEMO_PLANT_SYSTEMS} & set(_EXPECTED)
    assert len(DEMO_SYSTEMS) == 6


def test_manifest_identities_are_exact():
    for s in DEMO_SYSTEMS:
        name, schema, plant = _EXPECTED[s["id"]]
        assert s["name"] == name, s
        assert s["schema"] == schema, s
        assert s["plant"] == plant, s
        assert s["connection_type"] == "PostgreSQL", s
        # Each entry must carry the fields the registration hook consumes.
        for key in ("id", "name", "system_type", "plant", "connection_type", "schema"):
            assert key in s, (key, s)


def test_manifest_ids_are_tool_name_safe_and_verbatim():
    """Every id must pass sanitize_system_id UNCHANGED — otherwise the id-passthrough
    at registration would rewrite it and break the harness's adt_<id> assertions."""
    for s in DEMO_SYSTEMS:
        assert sanitize_system_id(s["id"]) == s["id"], s["id"]


def test_manifest_carries_no_status_field():
    """Status is a live-catalog concern, not a manifest identity — the manifest must
    not bake one in (the registration hook sets status='registered' via connect)."""
    for s in DEMO_SYSTEMS:
        assert "status" not in s, s


def test_demo_catalog_fixture_reuses_real_manifest():
    """The offline fixture must be DRY with the real manifest: the 6 SQL identities
    it exposes are exactly the manifest's (plus test-only extras)."""
    try:
        from unit._demo_catalog import DEMO_SYSTEMS as FIXTURE
    except ImportError:
        from _demo_catalog import DEMO_SYSTEMS as FIXTURE
    fixture_by_id = {s["id"]: s for s in FIXTURE}
    for s in DEMO_SYSTEMS:
        f = fixture_by_id[s["id"]]
        assert f["name"] == s["name"]
        assert f["schema"] == s["schema"]
        assert f["status"] == "registered"
    # The extra test-only identities remain present for the resolver/status tests.
    for extra in ("cmms2", "sitewise-direct", "highbyte"):
        assert extra in fixture_by_id, extra


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
