# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the i3X data-model discovery (Task A2 of the i3X port).

Covers:
- ``_is_i3x_scalar_type`` recognises bare scalars AND the property-URN form;
- ``_discover_i3x_data_model`` (mocked httpx, no live server):
  * scalar ObjectTypes filtered from the emitted tables,
  * one table per real ObjectType with the i3X core identifier columns,
  * structural container types skipped,
  * unitOfMeasure-aware schema-property columns + live-channel enrichment,
  * Hierarchy pseudo-table always present,
  * unreachable server ⇒ minimal Asset/AssetModel/Property fallback, no raise;
- routing: ``onboard_agent.discover()`` short-circuits i3X to the new
  function (and the result carries connection_type), and
  ``onboard.onboard_discover`` no longer sends i3X through
  ``_discover_openapi``.

All fixtures are synthetic — no dataset/demo literals.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402

_BASE = "https://i3x.example.internal"


# --------------------------------------------------------------------------- #
# _is_i3x_scalar_type
# --------------------------------------------------------------------------- #
def test_scalar_type_recognised_in_both_forms():
    for bare in ("DOUBLE", "INTEGER", "BOOLEAN", "STRING", "STRUCT"):
        assert oa._is_i3x_scalar_type(bare)
        assert oa._is_i3x_scalar_type(f"urn:aws:iotsitewise:property:{bare}")
    assert not oa._is_i3x_scalar_type("urn:type:equipmentmodel")
    assert not oa._is_i3x_scalar_type("EquipmentModel")
    assert not oa._is_i3x_scalar_type("")
    assert not oa._is_i3x_scalar_type(None)


# --------------------------------------------------------------------------- #
# Fake i3X server over monkeypatched httpx
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _envelope(results):
    return {"success": True, "results": results}


_NAMESPACES = [{"uri": "urn:example:ns", "displayName": "Example Namespace"}]

_OBJECT_TYPES = [
    # Scalar containers — MUST be filtered (bare and URN form).
    {"elementId": "DOUBLE", "displayName": "DOUBLE"},
    {"elementId": "urn:aws:iotsitewise:property:STRING", "displayName": "STRING"},
    # Structural container — MUST be skipped as a table.
    {"elementId": "urn:type:site", "displayName": "Site"},
    # Real asset model with a schema-declared property carrying a unit.
    {"elementId": "urn:type:equipmentmodel", "displayName": "EquipmentModel",
     "schema": {"properties": {
         "flow_rate": {"type": "number", "unit": "l/min",
                       "description": "process flow"},
         "elementId": {"type": "string"},  # identifier — not duplicated
     }}},
]

_INSTANCES = [{"elementId": "inst-1", "displayName": "UnitA",
               "typeElementId": "urn:type:equipmentmodel"}]

_RELATED_CHILDREN = _envelope([
    {"elementId": "inst-1", "result": [
        {"sourceRelationship": "HasChildren",
         "object": {"elementId": "prop-1", "displayName": "temperature",
                    "typeElementId": "urn:aws:iotsitewise:property:DOUBLE",
                    "isComposition": False}},
        {"sourceRelationship": "HasChildren",
         "object": {"elementId": "meta-1", "displayName": "_pathmeta",
                    "typeElementId": "urn:aws:iotsitewise:property:STRING",
                    "isComposition": False}},
    ]},
])


def _install_httpx(monkeypatch, *, unreachable=False):
    from agents import pinned_http

    def _get(url, headers=None, params=None, timeout=None, verify=None, **_kw):
        if unreachable:
            raise ConnectionError("server unreachable")
        params = params or {}
        if url.endswith("/v1/namespaces"):
            return _FakeResponse(_envelope(list(_NAMESPACES)))
        if url.endswith("/v1/objecttypes"):
            return _FakeResponse(_envelope(list(_OBJECT_TYPES)))
        if url.endswith("/v1/objects") and params.get("typeElementId"):
            return _FakeResponse(_envelope(list(_INSTANCES)))
        if url.endswith("/v1/objects") and params.get("root") == "true":
            return _FakeResponse(_envelope([{"elementId": "root-1"}]))
        return _FakeResponse(_envelope([]))

    def _post(url, headers=None, json=None, timeout=None, verify=None, **_kw):
        if unreachable:
            raise ConnectionError("server unreachable")
        if url.endswith("/v1/objects/related"):
            return _FakeResponse(_RELATED_CHILDREN)
        return _FakeResponse(_envelope([]))

    monkeypatch.setattr(pinned_http, "get", _get)
    monkeypatch.setattr(pinned_http, "post", _post)


def _entry():
    return {
        "connection_string": _BASE,
        "connection_config": {"base_url": _BASE, "token": "",
                              "auth_scheme": "bearer"},
        "connection_type": "SiteWise (i3X)",
    }


# --------------------------------------------------------------------------- #
# (1)+(2)+(3) table synthesis
# --------------------------------------------------------------------------- #
def test_scalar_and_structural_types_filtered_from_tables(monkeypatch):
    _install_httpx(monkeypatch)
    result = oa._discover_i3x_data_model(_entry())
    names = [t["name"] for t in result["tables"]]
    assert "DOUBLE" not in names
    assert "STRING" not in names
    assert "Site" not in names, "structural container types must not become tables"
    assert "EquipmentModel" in names


def test_object_type_table_has_core_identifier_columns(monkeypatch):
    _install_httpx(monkeypatch)
    result = oa._discover_i3x_data_model(_entry())
    table = next(t for t in result["tables"] if t["name"] == "EquipmentModel")
    cols = {c["name"]: c["data_type"] for c in table["columns"]}
    for ident in ("elementId", "displayName", "typeElementId", "namespaceUri",
                  "parentElementId", "path"):
        assert ident in cols, f"missing i3X core identifier column {ident}"
    # Schema-declared property with unitOfMeasure surfaced.
    assert "flow_rate" in cols
    assert "(l/min)" in cols["flow_rate"]
    # Live-channel enrichment from the sampled instance's property children.
    assert "temperature" in cols
    assert "live telemetry channel" in cols["temperature"]
    # Underscore metadata children never become columns.
    assert "_pathmeta" not in cols
    assert table["row_count"] == len(_INSTANCES)


def test_namespace_and_hierarchy_pseudo_tables_present(monkeypatch):
    _install_httpx(monkeypatch)
    result = oa._discover_i3x_data_model(_entry())
    names = [t["name"] for t in result["tables"]]
    assert "Namespace" in names
    assert "Hierarchy" in names
    hierarchy = next(t for t in result["tables"] if t["name"] == "Hierarchy")
    hcols = {c["name"] for c in hierarchy["columns"]}
    assert {"rootElementId", "parentElementId", "childElementId",
            "path", "depth", "relationshipType"} <= hcols


# --------------------------------------------------------------------------- #
# (4) unreachable server ⇒ fallback minimal schema, no raise
# --------------------------------------------------------------------------- #
def test_unreachable_server_falls_back_to_minimal_schema(monkeypatch):
    _install_httpx(monkeypatch, unreachable=True)
    result = oa._discover_i3x_data_model(_entry())
    assert "error" not in result
    names = [t["name"] for t in result["tables"]]
    assert names == ["Asset", "AssetModel", "Property"]


def test_missing_base_url_returns_error_not_raise():
    result = oa._discover_i3x_data_model({"connection_config": {}})
    assert "error" in result
    assert "base_url" in result["error"]


# --------------------------------------------------------------------------- #
# (5) onboard_agent.discover() routes i3X to the new function
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self):
        self.items = {}

    def get_session(self, system_id):
        from agents.onboarding_session import SessionNotFoundError
        if system_id not in self.items:
            raise SessionNotFoundError(system_id)
        return dict(self.items[system_id])

    def create_session(self, system_id, initiated_by, **fields):
        item = {"system_id": system_id, "status": "discovering",
                "initiated_by": initiated_by, **fields}
        self.items[system_id] = item
        return dict(item)

    def advance_status(self, system_id, from_status, to_status, **fields):
        self.items[system_id]["status"] = to_status
        self.items[system_id].update(fields)


def test_discover_tool_routes_i3x_to_data_model_discovery(monkeypatch):
    seen_entries = []

    def _fake_i3x(entry):
        seen_entries.append(entry)
        return {"tables": [{"name": "EquipmentModel", "columns": [], "row_count": 2}]}

    def _boom(_entry):
        raise AssertionError("_discover_openapi must NOT be called for i3X")

    fake_store = _FakeStore()
    monkeypatch.setattr(oa, "_discover_i3x_data_model", _fake_i3x)
    monkeypatch.setattr(oa, "_discover_openapi", _boom)
    monkeypatch.setattr(oa, "_store", lambda: fake_store)
    monkeypatch.setattr(oa.catalog, "get", lambda sid: {
        "id": sid, "name": "Example i3X", "connection_type": "SiteWise (i3X)",
        "schema": ""})
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "SiteWise (i3X)",
        "base_url": _BASE, "token": "tok-123", "auth_scheme": "bearer"})

    res = oa.discover("example-i3x")

    assert seen_entries, "discover() must call _discover_i3x_data_model"
    entry = seen_entries[0]
    # Connection sourced from the resolver (ADR 0031), and connection_type rides
    # on the entry.
    assert entry["connection_config"]["base_url"] == _BASE
    assert entry["connection_config"]["token"] == "tok-123"  # noqa: S105
    assert entry["connection_type"] == "SiteWise (i3X)"
    # Result carries connection_type + the discovered tables.
    assert res["connection_type"] == "SiteWise (i3X)"
    assert res["total_tables"] == 1
    assert fake_store.items["example-i3x"]["status"] == "discovered"


# --------------------------------------------------------------------------- #
# (6) onboard.onboard_discover i3X branch no longer calls _discover_openapi
# --------------------------------------------------------------------------- #
def test_onboard_discover_routes_i3x_away_from_openapi(monkeypatch):
    seen_entries = []

    def _fake_i3x(entry):
        seen_entries.append(entry)
        return {"tables": [{"name": "EquipmentModel",
                            "columns": [{"name": "elementId",
                                         "data_type": "string"}],
                            "row_count": 2}]}

    def _boom(_entry):
        raise AssertionError("_discover_openapi must NOT be called for i3X")

    monkeypatch.setattr(oa, "_discover_i3x_data_model", _fake_i3x)
    monkeypatch.setattr(oa, "_discover_openapi", _boom)
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "analysis")
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {
        "id": sid, "name": "Example i3X", "connection_type": "SiteWise (i3X)",
        "schema": ""})
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "SiteWise (i3X)",
        "base_url": _BASE, "token": "tok-123", "auth_scheme": "bearer"})

    body = DiscoverRequest(system_id="example-i3x",
                           connection_type="SiteWise (i3X)")
    res = asyncio.run(onboard.onboard_discover(body))

    assert "error" not in res, res
    assert seen_entries, "onboard_discover must call _discover_i3x_data_model"
    entry = seen_entries[0]
    assert entry["connection_config"]["base_url"] == _BASE
    assert entry["connection_type"] == "SiteWise (i3X)"
    assert res["connection_type"] == "SiteWise (i3X)"
    assert res["tables"][0]["name"] == "EquipmentModel"


def test_onboard_discover_rest_api_still_uses_openapi(monkeypatch):
    """Regression: the plain REST API branch keeps _discover_openapi."""
    openapi_calls = []

    def _fake_openapi(entry):
        openapi_calls.append(entry)
        return {"tables": [{"name": "GET /things",
                            "columns": [{"name": "id", "data_type": "string"}],
                            "row_count": 0}]}

    def _boom(_entry):
        raise AssertionError("_discover_i3x_data_model must NOT run for REST API")

    monkeypatch.setattr(oa, "_discover_openapi", _fake_openapi)
    monkeypatch.setattr(oa, "_discover_i3x_data_model", _boom)
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "analysis")
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {
        "id": sid, "name": "Example REST", "connection_type": "REST API",
        "schema": ""})
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "system_id": sid, "connection_type": "REST API",
        "connection_string": "https://api.example.internal/spec.json",
        "base_url": "https://api.example.internal"})

    body = DiscoverRequest(system_id="example-rest", connection_type="REST API")
    res = asyncio.run(onboard.onboard_discover(body))

    assert "error" not in res, res
    assert openapi_calls, "REST API discovery must still use _discover_openapi"
    assert res["connection_type"] == "REST API"
