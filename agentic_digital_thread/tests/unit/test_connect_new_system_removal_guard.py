# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user registration never reclaims the id of a half-removed system.

``connect_new_system`` (the chat tool and the HTTP route) can register an
explicit ``system_id``. Its only uniqueness check was the fail-soft
``catalog.lookup``: on a cold onboarding-runtime container with the graph
unavailable, hydration found nothing, so a system whose removal did not
finish (its node carries ``removal_incomplete``) registered as new, and with
credentials it could restore and overwrite its pending-deletion
``adt/conn/<id>`` secret. A user registration now makes the same checked
decision as the chat tools' removal guard before anything is claimed: a
marked node refuses (``removal_failed``), a graph read failure refuses
(``graph_unavailable``), an existing node or entry is the existing
"already exists" error, and only a checked no-row registers. The demo hook
(its own origin, run at app start-up after the restore) is unchanged.
Offline: the graph read is faked; Secrets Manager writes are recorded.
"""
from __future__ import annotations

import pytest

import agents.catalog as catalog_mod
import agents.connection_resolver as cr
import agents.db as db
import agents.onboard_agent as oa
from agents.catalog import REMOVAL_FAILED, SystemCatalog
from tests.unit.test_connect_new_system_custom_types import no_dns  # noqa: F401 - autouse

SID = "half-removed"
URL = "https://mcp.example.com/mcp"
TOKEN = "tok-SECRET-9"  # nosec B105 - fixture


@pytest.fixture
def cold(monkeypatch):
    """A cold runtime catalog, and every secret write recorded."""
    fresh = SystemCatalog([])
    for mod in (catalog_mod, oa):
        monkeypatch.setattr(mod, "catalog", fresh)
    writes: list = []

    def _store(system_id, secret):
        writes.append(system_id)
        return f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{system_id}-AbCdEf"
    monkeypatch.setattr(cr, "_store_connection_secret", _store)
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    monkeypatch.setattr(oa, "_store_secret", lambda *a, **k: writes.append("adt/systems")
                        or "arn:x")
    return type("Cold", (), {"catalog": fresh, "writes": writes})


def _graph(monkeypatch, *, node=None, down=False):
    """Every checked graph read: down raises; otherwise ``node`` is the row
    for SID (None: no node). The fail-soft reads (hydration) see the same."""
    def _checked(cypher, parameters=None):
        if down:
            raise db.GraphQueryError("ThrottlingException", "graph unavailable")
        if "MATCH (s:System {id: $sid})" in cypher:
            sid = (parameters or {}).get("sid")
            return [dict(node)] if (node is not None and sid == SID) else []
        if cypher.startswith("MATCH (s:System)"):
            return [{"id": SID, **node}] if node is not None else []
        return []
    monkeypatch.setattr(db, "neptune_query_checked", _checked)


def _register(**extra) -> dict:
    return oa.connect_new_system(name="Half Removed", system_type="Custom", plant="",
                                 connection_type="MCP Server", connection_string=URL,
                                 connection_config={"token": TOKEN}, system_id=SID, **extra)


def _nothing_written(cold):
    assert cold.catalog.get_all() == [], cold.catalog.get_all()
    assert cold.writes == [], cold.writes


def test_a_graph_outage_refuses_on_a_cold_catalog(cold, monkeypatch):
    _graph(monkeypatch, down=True)
    res = _register()
    assert res.get("reason") == "graph_unavailable", res
    assert "try again" in res["error"].lower()
    _nothing_written(cold)


def test_a_marked_explicit_id_refuses_on_a_cold_catalog(cold, monkeypatch):
    _graph(monkeypatch, node={"removal_incomplete": True, "name": "Half", "schema": "",
                              "connection_type": "MCP Server"})
    # Hydration would see the node too; make it fail so only the checked read decides.
    monkeypatch.setattr(cold.catalog, "_graph_rows", lambda: [])
    res = _register()
    assert res == {"error": f"The removal of '{SID}' did not finish. Remove it again, "
                            "then onboard it.",
                   "reason": "removal_failed", "system_id": SID}, res
    _nothing_written(cold)


def test_a_known_removal_failed_entry_refuses_without_a_graph_read(cold, monkeypatch):
    cold.catalog.add({"id": SID, "name": "Half", "connection_type": "MCP Server",
                      "schema": "", "status": REMOVAL_FAILED})
    _graph(monkeypatch, down=True)
    assert _register()["reason"] == "removal_failed"
    assert cold.writes == []


def test_an_existing_unmarked_node_is_already_registered(cold, monkeypatch):
    """A cold catalog that missed a live node (hydration failed) must not
    register over it either."""
    _graph(monkeypatch, node={"removal_incomplete": None, "name": "Half", "schema": "",
                              "connection_type": "MCP Server", "status": "onboarded"})
    monkeypatch.setattr(cold.catalog, "_graph_rows", lambda: [])
    assert _register() == {"error": f"System {SID} already exists"}
    _nothing_written(cold)


def test_guard_a_checked_no_row_registers(cold, monkeypatch):
    _graph(monkeypatch, node=None)
    res = _register()
    assert res.get("status") == "created", res
    assert cold.catalog.get(SID)["connection_origin"] == "user"
    assert cold.writes == [SID]


def test_guard_a_live_entry_is_still_already_exists(cold, monkeypatch):
    cold.catalog.add({"id": SID, "name": "Half", "connection_type": "MCP Server",
                      "schema": "", "status": "registered"})
    _graph(monkeypatch, node=None)
    assert _register() == {"error": f"System {SID} already exists"}
    assert cold.writes == []


def test_guard_the_http_route_takes_the_same_decision(cold, monkeypatch):
    import agents.onboard as onboard
    from agents.models import ConnectRequest
    monkeypatch.setattr(onboard, "catalog", cold.catalog)
    _graph(monkeypatch, down=True)
    res = onboard.onboard_connect(ConnectRequest(
        name="Half Removed", system_type="Custom", connection_type="MCP Server",
        connection_string=URL, connection_config={"token": TOKEN}, system_id=SID))
    assert res.get("reason") == "graph_unavailable", res
    _nothing_written(cold)


def test_guard_the_demo_hook_is_unchanged(cold, monkeypatch):
    """The demo registration hook (its own origin, run at app start-up after
    the restore has rebuilt any removal_failed entry) keeps its behaviour."""
    _graph(monkeypatch, down=True)
    res = oa.register_demo_system(name="HighByte", system_type="UNS", plant="",
                                  connection_type="MCP Server", connection_string=URL,
                                  schema="", system_id="highbyte")
    assert res.get("status") == "created", res
