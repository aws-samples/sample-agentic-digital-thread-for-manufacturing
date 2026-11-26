# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every system-active onboarding chat tool refuses a half-removed system.

The onboarding runtime's chat tools must not rebuild a system whose removal
did not finish (its :System node carries ``removal_incomplete``; its catalog
entry is ``removal_failed``). Two gaps remained after the first guard:

1. The guard read the node with the fail-soft ``neptune_query``, so a graph
   outage read as "not marked" and the tool ran (a stale ``onboarded``
   entry reached ``map_fields``' body). The node is now read with
   ``neptune_query_checked`` whenever the catalog does not already say
   ``removal_failed``, and a failed read refuses the call (retryable)
   before the tool runs.
2. Only discover / map_fields / provision_system were guarded.
   ``store_credentials`` wrote ``adt/systems/<id>`` for a ``removal_failed``
   entry, and ``test_connection``, ``assess_vocabulary_fit``,
   ``resolve_identities`` and ``verify_system`` could reach the source or
   the durable session. Every system-active tool in ONBOARD_TOOLS is now
   guarded; ``reset_system`` (which finishes the removal), the passive
   ``get_catalog`` / ``get_system_info``, ``connect_new_system`` (it
   registers a new id) and the zero-write vocabulary preview
   ``propose_concepts`` (no source, no session) are not.

Offline: every side door (source probe and resolution, PostgreSQL, the
DynamoDB session store, Secrets Manager, Bedrock, Step Functions, boto3) is
poisoned and recorded.
"""
from __future__ import annotations

import pytest

import agents.catalog as catalog_mod
import agents.connection_resolver as cr
import agents.db as db
import agents.onboard_agent as oa
from agents.catalog import REMOVAL_FAILED, SystemCatalog

SID = "half-removed"
MARKED = {"error": f"The removal of '{SID}' did not finish. Remove it again, then onboard it.",
          "reason": "removal_failed", "system_id": SID}

#: Every ONBOARD_TOOLS entry, classified: a new tool fails this test until it is.
GUARDED = {"store_credentials", "test_connection", "discover", "assess_vocabulary_fit",
           "map_fields", "resolve_identities", "provision_system", "verify_system"}
#: Guarded inside the tool, not by the decorator: connect_new_system takes an
#: explicit id and makes the same checked decision before it claims anything
#: (test_connect_new_system_removal_guard.py).
CUSTOM_GUARDED = {"connect_new_system"}
EXEMPT = {"get_catalog", "get_system_info", "reset_system", "propose_concepts"}
ARGS = {"store_credentials": {"credentials": '{"user": "u", "password": "p"}'}}


def _name(tool_obj) -> str:
    return getattr(tool_obj, "tool_name", None) or tool_obj.__name__


def _guarded_tools():
    return [t for t in oa.ONBOARD_TOOLS if _name(t) in GUARDED]


def test_every_onboarding_tool_is_classified():
    names = {_name(t) for t in oa.ONBOARD_TOOLS}
    assert names == GUARDED | CUSTOM_GUARDED | EXEMPT, names ^ (GUARDED | CUSTOM_GUARDED | EXEMPT)


@pytest.fixture
def reached(monkeypatch):
    """Poison every side door a tool body could use; record what was reached."""
    hits: list = []

    def _poison(label):
        def _hit(*_a, **_k):
            hits.append(label)
            raise AssertionError(f"reached {label}")
        return _hit
    monkeypatch.setattr(oa, "_store", _poison("dynamodb session store"))
    monkeypatch.setattr(oa, "_store_secret", _poison("secrets manager"))
    monkeypatch.setattr(oa, "bedrock_call", _poison("bedrock"))
    monkeypatch.setattr(oa, "pg", _poison("postgresql"))
    monkeypatch.setattr(oa, "_discover_backend_result", _poison("source discover"))
    monkeypatch.setattr(cr, "probe_connection", _poison("source probe"))
    monkeypatch.setattr(cr, "resolve_connection", _poison("source resolution"))
    import boto3
    monkeypatch.setattr(boto3, "client", _poison("boto3 (step functions / secrets)"))
    return hits


@pytest.fixture
def runtime_catalog(monkeypatch):
    cold = SystemCatalog([])
    for mod in (catalog_mod, oa):
        monkeypatch.setattr(mod, "catalog", cold)
    return cold


def _graph(monkeypatch, *, marked=None, down=False):
    """The node read. ``down``: every graph query fails (checked raises,
    fail-soft returns [])."""
    def _checked(cypher, parameters=None):
        if down:
            raise db.GraphQueryError("ThrottlingException", "graph unavailable")
        if "MATCH (s:System {id: $sid})" in cypher and "removal_incomplete" in cypher:
            return [] if marked is None else [{"removal_incomplete": marked, "name": "Half",
                                               "schema": "lims", "plant": "",
                                               "connection_type": "PostgreSQL",
                                               "system_type": "LIMS"}]
        if cypher.startswith("MATCH (s:System)"):
            return []
        return []
    monkeypatch.setattr(db, "neptune_query_checked", _checked)


def _call(tool_obj):
    try:
        return tool_obj(SID, **ARGS.get(_name(tool_obj), {}))
    except AssertionError as exc:
        return {"raised": str(exc)}


def _entry(status: str) -> dict:
    return {"id": SID, "name": "Half", "connection_type": "PostgreSQL", "schema": "lims",
            "status": status, "plant": "", "system_type": "LIMS"}


# ── a known removal_failed entry ───────────────────────────────────────────

@pytest.mark.parametrize("tool_obj", _guarded_tools(), ids=_name)
def test_a_removal_failed_system_is_refused_before_any_access(
        runtime_catalog, reached, monkeypatch, tool_obj):
    runtime_catalog.add(_entry(REMOVAL_FAILED))
    _graph(monkeypatch, down=True)                 # decided without reading the graph
    assert _call(tool_obj) == MARKED
    assert reached == []


@pytest.mark.parametrize("tool_obj", _guarded_tools(), ids=_name)
def test_a_marked_node_is_refused_even_with_a_stale_onboarded_entry(
        runtime_catalog, reached, monkeypatch, tool_obj):
    runtime_catalog.add(_entry("onboarded"))
    _graph(monkeypatch, marked=True)
    assert _call(tool_obj) == MARKED
    assert reached == []
    assert runtime_catalog.get(SID)["status"] == REMOVAL_FAILED


# ── graph outage: never "not marked" ───────────────────────────────────────

@pytest.mark.parametrize("tool_obj", _guarded_tools(), ids=_name)
@pytest.mark.parametrize("cold", [False, True], ids=["stale-entry", "cold-entry"])
def test_a_graph_outage_refuses_before_the_tool_runs(
        runtime_catalog, reached, monkeypatch, tool_obj, cold):
    if not cold:
        runtime_catalog.add(_entry("onboarded"))
    _graph(monkeypatch, down=True)
    res = _call(tool_obj)
    assert res.get("reason") == "graph_unavailable", res
    assert "try again" in res["error"].lower() and SID in res["error"]
    assert reached == []


# ── what stays as it was ───────────────────────────────────────────────────

def test_guard_a_healthy_system_still_reaches_the_tool(runtime_catalog, reached, monkeypatch):
    runtime_catalog.add(_entry("onboarded"))
    _graph(monkeypatch, marked=None)
    res = _call(oa.map_fields)
    assert res == {"raised": "reached dynamodb session store"}, res


def test_guard_the_passive_tools_still_show_a_removal_failed_system(
        runtime_catalog, reached, monkeypatch):
    runtime_catalog.add(_entry(REMOVAL_FAILED))
    _graph(monkeypatch, down=True)
    assert oa.get_system_info(SID)["status"] == REMOVAL_FAILED
    assert {s["id"]: s["status"] for s in oa.get_catalog()["systems"]} == {SID: REMOVAL_FAILED}
    assert reached == []


def test_guard_an_invalid_id_is_still_rejected_by_the_tool(runtime_catalog, monkeypatch):
    _graph(monkeypatch, down=True)
    with pytest.raises(ValueError):
        oa.store_credentials("../escape", '{"user": "u"}')


def test_guard_the_marker_query_is_the_one_the_offline_lane_answers():
    """tests/unit/conftest.py answers this checked read from the faked node."""
    assert oa._REMOVAL_MARKER_QUERY.startswith(
        "MATCH (s:System {id: $sid}) RETURN s.removal_incomplete AS removal_incomplete")
