# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for server._register_demo_systems (the startup registration hook).

Pins:
  * demo ENABLED  -> registers the 6 demo SQL systems plus the register-only
    plant systems (``workshop.DEMO_PLANT_SYSTEMS``) through connect_new_system,
    with the EXACT canonical ids/schemas passed VERBATIM as system_id (so the id is
    used verbatim, not slugified from the name), connection_type PostgreSQL, and a
    connection_string derived from demo.db_routing.demo_pg_dsn(schema);
  * demo DISABLED -> registers NOTHING (base mode: catalog stays empty, ADR-0030);
  * idempotent    -> a system already present in the catalog is skipped;
  * fail-soft     -> one system whose connect raises does not abort the others.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.demo.systems import DEMO_SYSTEMS  # noqa: E402
from agents.demo.workshop import DEMO_PLANT_SYSTEMS  # noqa: E402

#: What demo (non-workshop) mode registers: the demo six, then the plant systems.
_DEMO_MANIFEST = DEMO_SYSTEMS + DEMO_PLANT_SYSTEMS


@pytest.fixture(autouse=True)
def _demo_not_workshop(monkeypatch):
    """These pins cover demo mode; workshop mode is test_workshop_mode's job."""
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)


def _run(is_enabled, catalog_get=None, connect=None):
    """Invoke _register_demo_systems with demo gate, catalog, connect + demo_pg_dsn
    patched. Returns the connect MagicMock so the caller can assert calls."""
    cat = MagicMock()
    cat.get.side_effect = catalog_get if catalog_get else (lambda sid: None)
    connect = connect or MagicMock(return_value={"status": "created"})
    with patch("agents.demo.is_enabled", return_value=is_enabled), \
         patch.object(server, "catalog", cat), \
         patch("agents.onboard_agent.register_demo_system", connect), \
         patch("agents.demo.db_routing.demo_pg_dsn", side_effect=lambda s: f"dsn::{s}"):
        server._register_demo_systems()
    return connect


def test_demo_enabled_registers_demo_and_plant_systems_with_exact_ids_and_schemas():
    connect = _run(is_enabled=True)
    assert connect.call_count == 6 + len(DEMO_PLANT_SYSTEMS)
    by_id = {c.kwargs["system_id"]: c.kwargs for c in connect.call_args_list}
    # Exactly the 6 canonical ids plus the plant ids, passed VERBATIM as system_id.
    assert set(by_id) == {"erp", "mes-mil", "mes-det", "mes-cr", "cmms", "plm",
                          "mes-ind", "cmms-ind", "supplier-master"}
    expected = {s["id"]: s for s in _DEMO_MANIFEST}
    for sid, kw in by_id.items():
        assert kw["schema"] == expected[sid]["schema"]
        assert kw["name"] == expected[sid]["name"]
        assert kw["connection_type"] == "PostgreSQL"
        # connection_string derived from demo_pg_dsn(schema)
        assert kw["connection_string"] == f"dsn::{expected[sid]['schema']}"
        # id is passed verbatim (NOT slugified from the name)
        assert kw["system_id"] == sid


def test_demo_disabled_registers_nothing():
    connect = _run(is_enabled=False)
    assert connect.call_count == 0


def test_idempotent_skips_already_present_systems():
    # erp already present -> skipped; every other manifest system registered.
    connect = _run(is_enabled=True,
                   catalog_get=lambda sid: {"id": sid} if sid == "erp" else None)
    registered_ids = {c.kwargs["system_id"] for c in connect.call_args_list}
    assert "erp" not in registered_ids
    assert connect.call_count == len(_DEMO_MANIFEST) - 1


def test_fail_soft_one_bad_system_does_not_abort_others():
    calls = []

    def _connect(**kwargs):
        calls.append(kwargs["system_id"])
        if kwargs["system_id"] == "mes-det":
            raise RuntimeError("boom")
        return {"status": "created"}

    connect = _run(is_enabled=True, connect=MagicMock(side_effect=_connect))
    # Every manifest system was attempted despite mes-det raising.
    assert len(calls) == len(_DEMO_MANIFEST)
    assert "mes-det" in calls


def test_backfill_heals_degenerate_plant_and_name_in_place():
    # A demo MES system already present in the catalog (restored from a Neptune
    # System node an earlier provision wrote with no plant/name → name==id,
    # plant='') must be HEALED in place from the manifest, NOT re-registered.
    entries = {
        "mes-mil": {"id": "mes-mil", "name": "mes-mil", "plant": "",
                    "connection_type": "PostgreSQL", "status": "onboarded"},
    }
    connect = _run(is_enabled=True,
                   catalog_get=lambda sid: entries.get(sid))
    # mes-mil already present → NOT re-registered (heal, don't re-create).
    registered_ids = {c.kwargs["system_id"] for c in connect.call_args_list}
    assert "mes-mil" not in registered_ids
    # The live catalog entry was mutated with the manifest's canonical display name
    # + plant (Milwaukee), so per-plant KG nodes/Line State grouping resolve after
    # the next re-onboard.
    manifest = {s["id"]: s for s in DEMO_SYSTEMS}["mes-mil"]
    assert entries["mes-mil"]["name"] == manifest["name"]
    assert entries["mes-mil"]["plant"] == manifest["plant"]
    assert manifest["plant"]           # sanity: the manifest carries a real plant


def test_backfill_leaves_good_metadata_untouched():
    # A present system that ALREADY carries a real name + plant is left alone
    # (backfill only replaces degenerate name==id / empty plant).
    entries = {
        "erp": {"id": "erp", "name": "Custom ERP Name", "plant": "All Sites",
                "connection_type": "PostgreSQL", "status": "onboarded"},
    }
    connect = _run(is_enabled=True, catalog_get=lambda sid: entries.get(sid))
    assert "erp" not in {c.kwargs["system_id"] for c in connect.call_args_list}
    # untouched — a real (non-degenerate) name is never overwritten by the manifest
    assert entries["erp"]["name"] == "Custom ERP Name"
    assert entries["erp"]["plant"] == "All Sites"


# --------------------------------------------------------------------------- #
# Schema reconcile: one id, two schemas (frozen storyline vs demo/live MES).
# --------------------------------------------------------------------------- #

def _schema_split_entry(status):
    """A catalog entry for the shared plant-MES id registered with the OTHER
    story's schema (the frozen storyline), as a prior boot would leave it."""
    from agents.demo import workshop
    sid = workshop.LIVE_MES_SYSTEM["id"]
    other = next(s for s in workshop.WORKSHOP_SEEDED_SYSTEMS if s["id"] == sid)
    assert other["schema"] != workshop.LIVE_MES_SYSTEM["schema"]  # the split
    entry = {"id": sid, "name": other["name"], "plant": other["plant"],
             "connection_type": "PostgreSQL", "schema": other["schema"],
             "connection_string": f"dsn::{other['schema']}", "status": status}
    return sid, other["schema"], workshop.LIVE_MES_SYSTEM["schema"], entry


def test_schema_mismatch_on_registered_entry_is_corrected(caplog):
    sid, old, new, entry = _schema_split_entry("registered")
    with caplog.at_level("WARNING", logger=server.logger.name):
        connect = _run(is_enabled=True,
                       catalog_get=lambda s: entry if s == sid else None)
    assert sid not in {c.kwargs["system_id"] for c in connect.call_args_list}
    assert entry["schema"] == new
    assert entry["connection_string"] == f"dsn::{new}"
    warn = [r for r in caplog.records if r.levelname == "WARNING"
            and sid in r.getMessage()]
    assert warn and old in warn[0].getMessage() and new in warn[0].getMessage()


def test_schema_mismatch_on_onboarded_entry_is_left_and_logged_as_error(caplog):
    sid, old, new, entry = _schema_split_entry("onboarded")
    before = dict(entry)
    with caplog.at_level("WARNING", logger=server.logger.name):
        _run(is_enabled=True, catalog_get=lambda s: entry if s == sid else None)
    assert entry == before  # an onboarded system is never rewired silently
    err = [r for r in caplog.records if r.levelname == "ERROR"
           and sid in r.getMessage()]
    assert err, [r.getMessage() for r in caplog.records]
    msg = err[0].getMessage()
    assert old in msg and new in msg and f"scripts/reonboard.py {sid}" in msg


def test_matching_schema_is_not_reconciled(caplog):
    from agents.demo import workshop
    live = workshop.LIVE_MES_SYSTEM
    entry = {"id": live["id"], "name": live["name"], "plant": live["plant"],
             "schema": live["schema"], "connection_string": "kept",
             "status": "registered"}
    with caplog.at_level("WARNING", logger=server.logger.name):
        _run(is_enabled=True,
             catalog_get=lambda s: entry if s == live["id"] else None)
    assert entry["connection_string"] == "kept"
    assert not [r for r in caplog.records if live["id"] in r.getMessage()]


def test_live_mes_guidance_names_the_business_keys():
    """Event 23 (2026-10-02): the MES sign answer named a running work order by
    its row UUID. The guidance must steer the agent to the keys people use."""
    from agents.demo import workshop
    guidance = workshop.LIVE_MES_SYSTEM["query_guidance"]
    assert "work_order_number" in guidance and "run_number" in guidance
    assert "UUID" in guidance or "uuid" in guidance


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
