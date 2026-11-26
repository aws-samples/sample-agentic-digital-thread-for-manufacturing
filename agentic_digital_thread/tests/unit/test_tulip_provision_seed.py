# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tulip provision seed — known table ids/columns into the agent tables_desc,
the softened guidance, and the resolved workspace id (items 1 & 3).

The provisioned Tulip agent used to get only table NAMES and guidance that told
it to list tables + read columns before EVERY query. Here it gets the discovered
table IDS and bounded column names so it can query records directly; the guidance
makes the list/column calls conditional on an unknown id or a failed call.
Synthetic — the onboarding-session store and connection resolver are stubbed.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.connection_types import TULIP, _TULIP_GUIDANCE  # noqa: E402


def _ctx(system_id="tulip", cm_items=None):
    return types.SimpleNamespace(
        system_id=system_id, system_name="Plant Tulip", schema="",
        cm_items=cm_items or [])


_DISCOVERED = [
    {"name": "Orders", "table_id": "t1",
     "columns": [{"name": "run_number"}, {"name": "qty"}]},
    {"name": "Assets", "table_id": "t2",
     "columns": [{"name": "asset_tag"}]},
]


@pytest.fixture
def _stub_session(monkeypatch):
    class _FakeStore:
        def __init__(self, tables):
            self._tables = tables
        def get_session(self, system_id):
            return {"discovered_tables": self._tables}
    monkeypatch.setattr("agents.onboard_agent._store",
                        lambda: _FakeStore(_DISCOVERED))


# --------------------------------------------------------------------------- #
# _tulip_known_tables_desc — table ids + bounded columns
# --------------------------------------------------------------------------- #

def test_known_tables_desc_includes_ids_and_columns(_stub_session):
    desc = onboard._tulip_known_tables_desc(_ctx())
    assert "Orders (id=t1)" in desc
    assert "run_number" in desc and "qty" in desc
    assert "Assets (id=t2)" in desc
    # tells the agent to query directly with the id
    assert "tulip_query_records" in desc


def test_known_tables_desc_falls_back_to_cm_item_names(monkeypatch):
    # No discovery session ⇒ degrade to concept-mapping field prefixes (names).
    class _Empty:
        def get_session(self, system_id):
            from agents.onboarding_session import SessionNotFoundError
            raise SessionNotFoundError(system_id)
    monkeypatch.setattr("agents.onboard_agent._store", lambda: _Empty())
    desc = onboard._tulip_known_tables_desc(
        _ctx(cm_items=[{"field": "work_orders.wo_number"},
                       {"field": "defects.defect_id"}]))
    assert "Known business tables (names read from the system):" in desc
    assert "work_orders" in desc and "defects" in desc
    assert "id=" not in desc   # no ids available in the fallback


def test_known_tables_desc_empty_when_nothing_known(monkeypatch):
    class _Empty:
        def get_session(self, system_id):
            from agents.onboarding_session import SessionNotFoundError
            raise SessionNotFoundError(system_id)
    monkeypatch.setattr("agents.onboard_agent._store", lambda: _Empty())
    assert onboard._tulip_known_tables_desc(_ctx(cm_items=[])) == ""


def test_known_tables_desc_bounds_columns(monkeypatch):
    many_cols = [{"name": f"c{i}"} for i in range(30)]
    tables = [{"name": "Wide", "table_id": "tW", "columns": many_cols}]

    class _Store:
        def get_session(self, system_id):
            return {"discovered_tables": tables}
    monkeypatch.setattr("agents.onboard_agent._store", lambda: _Store())
    desc = onboard._tulip_known_tables_desc(_ctx())
    # only the first N columns shown, with an ellipsis marker
    assert "c0" in desc and "…" in desc
    assert "c29" not in desc


# --------------------------------------------------------------------------- #
# _tulip_tables_from_discovery — defensive parsing
# --------------------------------------------------------------------------- #

def test_tables_from_discovery_shapes_rows(_stub_session):
    rows = onboard._tulip_tables_from_discovery(_ctx())
    assert rows[0] == ("Orders", "t1", ["run_number", "qty"])
    assert rows[1] == ("Assets", "t2", ["asset_tag"])


# --------------------------------------------------------------------------- #
# _dp_tulip_workspace_id — node-first then catalog, malformed dropped (item 1)
# --------------------------------------------------------------------------- #

def test_workspace_id_from_node(monkeypatch):
    monkeypatch.setattr("agents.connection_resolver.resolve_connection",
                        lambda sid: {"workspace_id": "wsNode"})
    assert onboard._dp_tulip_workspace_id(_ctx()) == "wsNode"


def test_workspace_id_from_catalog_when_node_empty(monkeypatch):
    monkeypatch.setattr("agents.connection_resolver.resolve_connection",
                        lambda sid: {})
    monkeypatch.setattr(onboard.catalog, "get",
                        lambda sid: {"connection_config": {"workspace_id": "wsCat"}})
    assert onboard._dp_tulip_workspace_id(_ctx()) == "wsCat"


def test_workspace_id_malformed_is_dropped(monkeypatch):
    monkeypatch.setattr("agents.connection_resolver.resolve_connection",
                        lambda sid: {"workspace_id": "bad/seg"})
    assert onboard._dp_tulip_workspace_id(_ctx()) == ""


# --------------------------------------------------------------------------- #
# _TULIP_GUIDANCE softened (item 3)
# --------------------------------------------------------------------------- #

def test_tulip_guidance_makes_listing_conditional():
    g = _TULIP_GUIDANCE
    # the agent is told to query records directly when it already knows the id
    assert "query records DIRECTLY" in g or "query records directly" in g.lower()
    # and to list / read columns only for an unknown id or a failed call
    assert "only to find a table whose id you do not already know" in g
    # still names the tools and stays read-only
    assert "tulip_query_records" in g and "Read-only." in g
