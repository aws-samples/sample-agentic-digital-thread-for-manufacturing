# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The startup registration hook restores a manifest system's routing prose.

A manifest system (demo plant or workshop) is REGISTERED at first boot with its
``capabilities`` / ``query_guidance`` (guidance-through-registration); provision
copies that prose onto the :System node. After an app restart, ``_restore``
re-adds the system to the catalog from Neptune WITHOUT that prose, and the
idempotent hook used to skip it. That was harmless while the :System node kept
the prose (provision's second precedence), but a reset deletes the node, so a
re-onboard after any restart fell through to the generic per-connection-type
default. Observed live (2026-10-12): after a stack update restarted the app,
removing and re-adding HighByte left it described as "Live data via a Model
Context Protocol (MCP) server's exposed tools.", and the orchestrator stopped
sending "right now" sensor questions to it.

Pins:
  * an already-present manifest system with NO prose gets the manifest's
    capabilities + query_guidance back (outcome ``backfilled``);
  * prose already on the entry is never overwritten;
  * a manifest entry that carries no prose adds nothing to the entry;
  * only an entry on the manifest's schema gets it (an entry onboarded from the
    other story's schema is not this manifest system);
  * after the heal, provision with the :System node gone (a reset) resolves the
    manifest prose, not the generic default.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
from agents.demo import workshop  # noqa: E402
from agents.onboard import _resolve_provision_guidance  # noqa: E402


def _restored(sid: str, sysdef: dict) -> dict:
    """A catalog entry as ``_restore`` rebuilds it from a Neptune System node:
    identity + connection only, no routing prose."""
    return {"id": sid, "name": sysdef["name"], "plant": sysdef.get("plant", ""),
            "connection_type": sysdef.get("connection_type", "PostgreSQL"),
            "schema": sysdef.get("schema", ""), "status": "onboarded"}


def _run_hook(entries: dict, *, workshop_on: bool, highbyte: bool = False):
    cat = MagicMock()
    cat.get.side_effect = lambda sid: entries.get(sid)
    connect = MagicMock(return_value={"status": "created"})
    with patch("agents.demo.is_enabled", return_value=True), \
         patch.object(server, "catalog", cat), \
         patch("agents.onboard_agent.register_demo_system", connect), \
         patch("agents.demo.db_routing.demo_pg_dsn", side_effect=lambda s: f"dsn::{s}"), \
         patch.object(workshop, "_highbyte_shipped", return_value=highbyte):
        server._register_demo_systems()
    return connect


@pytest.fixture
def live_workshop(monkeypatch):
    monkeypatch.setenv("ADT_WORKSHOP", "1")
    monkeypatch.setenv("ADT_WORKSHOP_STORY", "live")


@pytest.fixture
def demo_mode(monkeypatch):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)


def _manifest(highbyte: bool) -> dict:
    return {s["id"]: s for s in workshop.registered_systems("live", highbyte_shipped=highbyte)}


def test_restored_highbyte_gets_its_registration_prose_back(live_workshop):
    want = _manifest(highbyte=True)["highbyte"]
    entries = {"highbyte": _restored("highbyte", want)}
    connect = _run_hook(entries, workshop_on=True, highbyte=True)
    assert "highbyte" not in {c.kwargs["system_id"] for c in connect.call_args_list}
    assert entries["highbyte"]["capabilities"] == want["capabilities"]
    assert entries["highbyte"]["query_guidance"] == want["query_guidance"]


def test_restored_sitewise_gets_the_with_highbyte_guidance(live_workshop):
    want = _manifest(highbyte=True)["sitewise-direct"]
    assert "TREND and HISTORY" in want["query_guidance"]  # the rewritten form
    entries = {"sitewise-direct": _restored("sitewise-direct", want)}
    _run_hook(entries, workshop_on=True, highbyte=True)
    assert entries["sitewise-direct"]["query_guidance"] == want["query_guidance"]
    assert entries["sitewise-direct"]["capabilities"] == want["capabilities"]


def test_restored_plant_cmms_gets_its_prose_back_in_demo_mode(demo_mode):
    want = {s["id"]: s for s in workshop.DEMO_PLANT_SYSTEMS}["cmms-ind"]
    entries = {"cmms-ind": _restored("cmms-ind", want)}
    _run_hook(entries, workshop_on=False)
    assert entries["cmms-ind"]["capabilities"] == want["capabilities"]
    assert entries["cmms-ind"]["query_guidance"] == want["query_guidance"]


def test_heal_reports_backfilled_for_prose():
    want = {s["id"]: s for s in workshop.DEMO_PLANT_SYSTEMS}["cmms-ind"]
    entry = _restored("cmms-ind", want)
    assert server._heal_existing_demo_system(entry, want, lambda s: f"dsn::{s}") == "backfilled"


def test_prose_already_on_the_entry_is_never_overwritten(live_workshop):
    want = _manifest(highbyte=True)["highbyte"]
    entry = _restored("highbyte", want)
    entry["capabilities"] = "Operator-edited capabilities."
    entry["query_guidance"] = "Operator-edited guidance."
    entries = {"highbyte": entry}
    _run_hook(entries, workshop_on=True, highbyte=True)
    assert entry["capabilities"] == "Operator-edited capabilities."
    assert entry["query_guidance"] == "Operator-edited guidance."


def test_manifest_entry_without_prose_adds_nothing(demo_mode):
    from agents.demo.systems import DEMO_SYSTEMS
    erp = next(s for s in DEMO_SYSTEMS if s["id"] == "erp")
    assert not erp.get("capabilities") and not erp.get("query_guidance")
    entries = {"erp": _restored("erp", erp)}
    _run_hook(entries, workshop_on=False)
    assert "capabilities" not in entries["erp"]
    assert "query_guidance" not in entries["erp"]


def test_reonboard_after_reset_resolves_the_manifest_prose_not_the_default(live_workshop):
    """The live failure end to end: restart (entry restored without prose), hook,
    reset (the :System node is gone, so node_guidance is empty), re-provision."""
    want = _manifest(highbyte=True)["highbyte"]
    entries = {"highbyte": _restored("highbyte", want)}
    _run_hook(entries, workshop_on=True, highbyte=True)
    guidance, capabilities, _ = _resolve_provision_guidance(
        entries["highbyte"], {}, "MCP Server", "highbyte")
    assert capabilities == want["capabilities"]
    assert want["query_guidance"] in guidance


def test_onboarded_entry_from_another_storys_schema_gets_no_prose(live_workshop):
    """An entry onboarded from the OTHER story's schema is not this manifest
    system, so it gets neither the schema rewire nor this story's prose."""
    sid = workshop.LIVE_MES_SYSTEM["id"]
    other = next(s for s in workshop.WORKSHOP_SEEDED_SYSTEMS if s["id"] == sid)
    entry = _restored(sid, other)
    assert entry["schema"] != workshop.LIVE_MES_SYSTEM["schema"]
    _run_hook({sid: entry}, workshop_on=True, highbyte=True)
    assert "capabilities" not in entry and "query_guidance" not in entry


def test_registered_entry_rewired_to_the_manifest_schema_gets_its_prose(live_workshop):
    sid = workshop.LIVE_MES_SYSTEM["id"]
    other = next(s for s in workshop.WORKSHOP_SEEDED_SYSTEMS if s["id"] == sid)
    entry = {**_restored(sid, other), "status": "registered"}
    _run_hook({sid: entry}, workshop_on=True, highbyte=True)
    assert entry["schema"] == workshop.LIVE_MES_SYSTEM["schema"]
    assert entry["query_guidance"] == workshop.LIVE_MES_SYSTEM["query_guidance"]


def test_user_registration_under_a_manifest_id_gets_no_manifest_prose(live_workshop):
    """An attendee can fully remove a register-only manifest system and register a
    replacement through Connect New System under the same id and (for an MCP source,
    blank) schema, onboarded with its own prose. ``_restore`` rebuilds that entry
    without prose but re-marks it ``connection_origin=user``. The manifest backfill
    must leave it alone: provision then keeps the :System node's own prose (second
    precedence), not the manifest's."""
    want = _manifest(highbyte=True)["highbyte"]
    entry = {**_restored("highbyte", want), "connection_origin": "user"}
    _run_hook({"highbyte": entry}, workshop_on=True, highbyte=True)
    assert "capabilities" not in entry and "query_guidance" not in entry
    own = {"query_guidance": "Our own hub: ask it for the current reading.", "capabilities": "Our own UNS.",
           "tool_prefix": ""}
    guidance, capabilities, _ = _resolve_provision_guidance(entry, own, "MCP Server", "highbyte")
    assert capabilities == own["capabilities"]
    assert guidance.startswith(own["query_guidance"])
    assert want["query_guidance"] not in guidance


def test_heal_still_backfills_a_manifest_registration_without_the_user_marker(live_workshop):
    """The bundled HighByte restore carries no user-origin marker (demo/workshop,
    admin-edited and legacy entries are left as the restore built them), so the
    guard above must not switch the fix off for the case it exists for."""
    want = _manifest(highbyte=True)["highbyte"]
    for origin in ({}, {"connection_origin": "demo"}, {"connection_origin": ""}):
        entry = {**_restored("highbyte", want), **origin}
        _run_hook({"highbyte": entry}, workshop_on=True, highbyte=True)
        assert entry["query_guidance"] == want["query_guidance"], origin
        assert entry["capabilities"] == want["capabilities"], origin
