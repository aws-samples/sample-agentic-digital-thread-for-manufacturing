# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the i3X provision hierarchy walker (agents/onboard.py).

Drives the REAL ``_do_provision`` i3X branch against a mocked httpx transport
and captured ``neptune_write`` calls — no live server, no AWS, no Neptune.

Contract under test (Task A1 of the i3X port):
- double-envelope flatten yields children from BOTH the per-elementId-wrapped
  shape and the already-flat shape;
- underscore-prefixed property children (server metadata) are skipped;
- a property child becomes ``:Property`` + ``HAS_PROPERTY`` + ``FROM_SYSTEM``
  with the data type stripped from the URN;
- a composition child becomes ``:Asset`` with ONLY a ``HAS_PARENT`` hierarchy
  edge (the [F6] single-canonical-edge convention — NO ``HAS_CHILD``);
- levels come from the asset's TYPE (site/line/...) with depth-based
  equipment/component fallback;
- every asset (incl. structural-only) is stamped with ``property_count``;
- an i3X error envelope raised inside the walk is swallowed non-fatally;
- ``I3X_TLS_VERIFY=false`` reaches the httpx call;
- the post-schema MODELED_BY / LOCATED_AT bridge runs AFTER the SchemaEntry
  writes.

All fixtures are synthetic — no dataset/demo literals.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.i3x_envelope import flatten_related_entries, i3x_unwrap  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402

_BASE = "https://i3x.example.internal"
_SID = "example-i3x"


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


# ObjectTypes: structural tiers + one equipment model + one deep component model.
_OBJECT_TYPES = [
    {"elementId": "urn:type:enterprise", "displayName": "Enterprise"},
    {"elementId": "urn:type:site", "displayName": "Site"},
    {"elementId": "urn:type:line", "displayName": "Line"},
    {"elementId": "urn:type:equipmentmodel", "displayName": "EquipmentModel"},
]

_ROOTS = [
    {"elementId": "root-1", "displayName": "RootEnterprise",
     "typeElementId": "urn:type:enterprise"},
]

# HasChildren responses per parent elementId.
# root-1 -> site-1 via the DOUBLY-WRAPPED shape.
# site-1 -> line-1 via the ALREADY-FLAT shape.
# line-1 -> one property child, one underscore metadata child, one equipment child.
# eq-1   -> no children (leaf, structural-only: zero properties).
_RELATED = {
    "root-1": _envelope([
        {"elementId": "root-1", "result": [
            {"sourceRelationship": "HasChildren",
             "object": {"elementId": "site-1", "displayName": "SiteOne",
                        "typeElementId": "urn:type:site", "isComposition": True}},
        ]},
    ]),
    "site-1": _envelope([
        {"sourceRelationship": "HasChildren",
         "object": {"elementId": "line-1", "displayName": "LineOne",
                    "typeElementId": "urn:type:line", "isComposition": True}},
        # non-HasChildren entry must be dropped even when the server leaks it
        {"sourceRelationship": "HasParent",
         "object": {"elementId": "root-1", "displayName": "RootEnterprise",
                    "typeElementId": "urn:type:enterprise", "isComposition": True}},
    ]),
    "line-1": _envelope([
        {"elementId": "line-1", "result": [
            {"sourceRelationship": "HasChildren",
             "object": {"elementId": "prop-1", "displayName": "temperature",
                        "typeElementId": "urn:aws:iotsitewise:property:DOUBLE",
                        "isComposition": False}},
            {"sourceRelationship": "HasChildren",
             "object": {"elementId": "meta-1", "displayName": "_pathmeta",
                        "typeElementId": "urn:aws:iotsitewise:property:STRING",
                        "isComposition": False}},
            {"sourceRelationship": "HasChildren",
             "object": {"elementId": "eq-1", "displayName": "UnitA",
                        "typeElementId": "urn:type:equipmentmodel",
                        "isComposition": True}},
        ]},
    ]),
    "eq-1": _envelope([]),
}


def _install_httpx(monkeypatch, *, related=None, capture=None, fail_related=False):
    """Monkeypatch pinned_http.get/post to serve the fake i3X server."""
    from agents import pinned_http

    related_map = related if related is not None else _RELATED
    calls = capture if capture is not None else []

    def _get(url, headers=None, params=None, timeout=None, verify=None, **_kw):
        calls.append({"method": "GET", "url": url, "params": params or {},
                      "verify": verify})
        if url.endswith("/v1/objecttypes"):
            return _FakeResponse(_envelope(list(_OBJECT_TYPES)))
        if url.endswith("/v1/objects") and (params or {}).get("root") == "true":
            return _FakeResponse(_envelope(list(_ROOTS)))
        return _FakeResponse(_envelope([]))

    def _post(url, headers=None, json=None, timeout=None, verify=None, **_kw):
        calls.append({"method": "POST", "url": url, "json": json, "verify": verify})
        if url.endswith("/v1/objects/related"):
            if fail_related:
                return _FakeResponse({"success": False, "responseDetail": {
                    "title": "Internal", "status": 500, "detail": "walk exploded"}})
            eid = (json or {}).get("elementIds", [None])[0]
            return _FakeResponse(related_map.get(eid, _envelope([])))
        return _FakeResponse(_envelope([]))

    monkeypatch.setattr(pinned_http, "get", _get)
    monkeypatch.setattr(pinned_http, "post", _post)
    return calls


# --------------------------------------------------------------------------- #
# Provision harness (mirrors test_provision_schema_replace)
# --------------------------------------------------------------------------- #
class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _FakeCatalog:
    def __init__(self):
        self.entries = {
            _SID: {
                "id": _SID, "name": "Example i3X", "status": "registered",
                "connection_type": "SiteWise (i3X)",
                "connection_config": {"base_url": _BASE, "token": "",
                                      "auth_scheme": "bearer"},
                "secret_arn": "",
            },
        }

    def get(self, system_id):
        return self.entries.get(system_id)

    def update_status(self, *_a, **_k):
        return None

    def get_onboarded_ids(self):
        return set()

    def get_onboarded(self):
        return []


def _install_seams(monkeypatch):
    """Capture every neptune_write (base provision + i3X post-pass share ONE
    ordered list) and neutralize all impure seams."""
    writes: list[tuple[str, dict]] = []

    def _record(c, p=None):
        writes.append((c, p or {}))
        return True

    import agents.onboard_i3x as onboard_i3x
    monkeypatch.setattr(onboard, "neptune_write", _record)
    monkeypatch.setattr(onboard_i3x, "neptune_write", _record)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    monkeypatch.setattr(onboard, "_classify_table_entity",
                        lambda *_a, **_k: {"entity": None, "mint": None,
                                           "identity_fields": []})
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "accepted_user_concept_mappings",
                        lambda *_a, **_k: {})
    return writes


def _body():
    return ProvisionRequest(
        system_id=_SID,
        system_name="Example i3X",
        schema="",
        connection_type="SiteWise (i3X)",
        connection_string=_BASE,
        mappings=[
            {"table": "EquipmentModel", "field": "EquipmentModel.temperature",
             "concept": "sensor-reading",
             "mappings": [{"concept": "sensor-reading", "role": "measure",
                           "confidence": 0.9}]},
        ],
        rules=[],
    )


def _provision(monkeypatch, **httpx_kwargs):
    writes = _install_seams(monkeypatch)
    calls = _install_httpx(monkeypatch, **httpx_kwargs)
    onboard._do_provision(_body())
    return writes, calls


def _writes_matching(writes, fragment):
    return [(c, p) for c, p in writes if fragment in c]


# --------------------------------------------------------------------------- #
# (1) double-envelope flatten: wrapped AND flat shapes both yield children
# --------------------------------------------------------------------------- #
def test_walker_discovers_children_through_both_envelope_shapes(monkeypatch):
    writes, _ = _provision(monkeypatch)
    asset_merges = _writes_matching(writes, "MERGE (a:Asset {source_id: $sid})")
    merged_ids = {p["sid"] for _c, p in asset_merges}
    # site-1 arrived via the wrapped shape, line-1 via the flat shape.
    assert {"root-1", "site-1", "line-1", "eq-1"} <= merged_ids


def test_flatten_helper_handles_both_shapes_and_drops_noise():
    wrapped = [{"elementId": "p", "result": [
        {"sourceRelationship": "HasChildren", "object": {"elementId": "c1"}}]}]
    flat = [{"sourceRelationship": "HasChildren", "object": {"elementId": "c2"}}]
    noise = ["str", 42, {"unrelated": True}]
    entries = flatten_related_entries(wrapped + flat + noise)
    assert [e["object"]["elementId"] for e in entries] == ["c1", "c2"]
    assert flatten_related_entries({"not": "a list"}) == []


def test_non_haschildren_entries_are_dropped(monkeypatch):
    writes, _ = _provision(monkeypatch)
    # site-1's response leaks a HasParent entry pointing back at root-1; it
    # must NOT produce a hierarchy edge root-1 -> site-1's parent set.
    parent_edges = _writes_matching(writes, "MERGE (c)-[:HAS_PARENT]->(p)")
    pairs = {(p["cid"], p["pid"]) for _c, p in parent_edges}
    assert ("root-1", "site-1") not in pairs


# --------------------------------------------------------------------------- #
# (2) underscore-prefixed property children skipped
# --------------------------------------------------------------------------- #
def test_underscore_system_properties_are_skipped(monkeypatch):
    writes, _ = _provision(monkeypatch)
    prop_merges = _writes_matching(writes, "MERGE (p:Property {source_id: $sid})")
    names = {p["name"] for _c, p in prop_merges}
    assert "_pathmeta" not in names
    ids = {p["sid"] for _c, p in prop_merges}
    assert "meta-1" not in ids


# --------------------------------------------------------------------------- #
# (3) property child ⇒ :Property + HAS_PROPERTY + FROM_SYSTEM, URN-stripped type
# --------------------------------------------------------------------------- #
def test_property_child_written_with_property_layer(monkeypatch):
    writes, _ = _provision(monkeypatch)
    prop_merges = _writes_matching(writes, "MERGE (p:Property {source_id: $sid})")
    assert len(prop_merges) == 1
    _c, p = prop_merges[0]
    assert p == {"sid": "prop-1", "name": "temperature", "pid": "line-1",
                 "sys": _SID, "dtype": "DOUBLE",
                 "path": "RootEnterprise/SiteOne/LineOne/temperature"}

    has_prop = _writes_matching(writes, "MERGE (a)-[:HAS_PROPERTY]->(p)")
    assert [(p["pid"], p["sid"]) for _c, p in has_prop] == [("line-1", "prop-1")]

    from_sys = [(c, p) for c, p in writes
                if "Property {source_id: $sid}" in c and "FROM_SYSTEM" in c]
    assert [(p["sid"], p["sys"]) for _c, p in from_sys] == [("prop-1", _SID)]


# --------------------------------------------------------------------------- #
# (4) composition child ⇒ :Asset + HAS_PARENT ONLY (no HAS_CHILD anywhere)
# --------------------------------------------------------------------------- #
def test_hierarchy_edges_are_has_parent_only(monkeypatch):
    writes, _ = _provision(monkeypatch)
    assert not _writes_matching(writes, "HAS_CHILD"), \
        "the [F6] convention forbids HAS_CHILD — HAS_PARENT is the single edge"
    parent_edges = _writes_matching(writes, "MERGE (c)-[:HAS_PARENT]->(p)")
    pairs = {(p["cid"], p["pid"]) for _c, p in parent_edges}
    assert pairs == {("site-1", "root-1"), ("line-1", "site-1"),
                     ("eq-1", "line-1")}


def test_composition_child_gets_instance_of_and_from_system(monkeypatch):
    writes, _ = _provision(monkeypatch)
    inst = _writes_matching(writes, "MERGE (m:AssetModel {name: $mname})")
    by_asset = {p["sid"]: p["mname"] for _c, p in inst}
    assert by_asset["eq-1"] == "EquipmentModel"
    assert by_asset["site-1"] == "Site"
    assert by_asset["root-1"] == "Enterprise"


# --------------------------------------------------------------------------- #
# (5) type-based level mapping + depth fallback
# --------------------------------------------------------------------------- #
def test_levels_from_type_with_depth_fallback(monkeypatch):
    writes, _ = _provision(monkeypatch)
    asset_merges = _writes_matching(writes, "MERGE (a:Asset {source_id: $sid})")
    levels = {p["sid"]: p.get("level") for _c, p in asset_merges if "level" in p}
    assert levels["site-1"] == "site"
    assert levels["line-1"] == "line"
    # EquipmentModel is not a structural type → depth fallback (depth 2 ≤ 3).
    assert levels["eq-1"] == "equipment"
    # Root is merged with level 'enterprise' baked into the cypher.
    root_merge = [c for c, p in asset_merges if p["sid"] == "root-1"][0]
    assert "a.level = 'enterprise'" in root_merge


# --------------------------------------------------------------------------- #
# (6) property_count stamped per asset (0 for structural-only)
# --------------------------------------------------------------------------- #
def test_property_count_stamped_including_zero(monkeypatch):
    writes, _ = _provision(monkeypatch)
    stamps = _writes_matching(writes, "SET a.property_count = $n")
    counts = {p["sid"]: p["n"] for _c, p in stamps}
    assert counts == {"site-1": 0, "line-1": 1, "eq-1": 0, "root-1": 0}


# --------------------------------------------------------------------------- #
# (7) error envelope raises inside walk, swallowed non-fatally
# --------------------------------------------------------------------------- #
def test_error_envelope_in_walk_is_non_fatal(monkeypatch):
    writes, _ = _provision(monkeypatch, fail_related=True)
    # No children discovered — but the provision completed and the root asset
    # plus the schema write still landed.
    assert not _writes_matching(writes, "MERGE (p:Property {source_id: $sid})")
    root_merges = [p for _c, p in
                   _writes_matching(writes, "MERGE (a:Asset {source_id: $sid})")]
    assert {p["sid"] for p in root_merges} == {"root-1"}
    assert _writes_matching(writes, "MERGE (se:SchemaEntry"), \
        "provision must fall through to the common schema write"


def test_unwrap_raises_on_error_envelope():
    with pytest.raises(RuntimeError, match="i3X error"):
        i3x_unwrap({"success": False, "responseDetail": {
            "title": "Bad", "status": 400, "detail": "nope"}})
    assert i3x_unwrap({"success": True, "result": {"a": 1}}) == {"a": 1}
    assert i3x_unwrap({"success": True, "results": [1]}) == [1]
    assert i3x_unwrap([1, 2]) == [1, 2]
    assert i3x_unwrap({"plain": True}) == {"plain": True}


# --------------------------------------------------------------------------- #
# (8) I3X_TLS_VERIFY honored down to the httpx call
# --------------------------------------------------------------------------- #
def test_tls_verify_toggle_reaches_httpx(monkeypatch):
    monkeypatch.setenv("I3X_TLS_VERIFY", "false")
    _writes, calls = _provision(monkeypatch)
    walker_calls = [c for c in calls if "/v1/" in c["url"]]
    assert walker_calls, "sanity: the walker made HTTP calls"
    assert all(c["verify"] is False for c in walker_calls)


def test_tls_verify_defaults_on(monkeypatch):
    monkeypatch.delenv("I3X_TLS_VERIFY", raising=False)
    _writes, calls = _provision(monkeypatch)
    assert all(c["verify"] is True for c in calls if "/v1/" in c["url"])


# --------------------------------------------------------------------------- #
# post-schema bridge: MODELED_BY + LOCATED_AT after the SchemaEntry write
# --------------------------------------------------------------------------- #
def test_post_schema_bridge_runs_after_schema_write(monkeypatch):
    writes, _ = _provision(monkeypatch)
    cyphers = [c for c, _p in writes]
    modeled_by_idx = next(i for i, c in enumerate(cyphers)
                          if "MERGE (m)-[:MODELED_BY]->(se)" in c)
    located_at_idx = next(i for i, c in enumerate(cyphers)
                          if "MERGE (a)-[:LOCATED_AT]->(pl)" in c)
    schema_idxs = [i for i, c in enumerate(cyphers) if "MERGE (se:SchemaEntry" in c]
    assert schema_idxs, "sanity: schema entries were written"
    assert modeled_by_idx > max(schema_idxs), \
        "MODELED_BY bridge must run AFTER the SchemaEntry writes"
    assert located_at_idx > max(schema_idxs)
    # Parameterized on the system id only.
    assert writes[modeled_by_idx][1] == {"sys": _SID}
    assert writes[located_at_idx][1] == {"sys": _SID}


def test_bridge_absent_for_non_i3x_provision(monkeypatch):
    writes = _install_seams(monkeypatch)
    _install_httpx(monkeypatch)
    body = _body()
    object.__setattr__(body, "connection_type", "MCP Server")
    object.__setattr__(body, "connection_string", "https://mcp.example.internal/mcp")
    onboard._do_provision(body)
    assert not _writes_matching(writes, "MODELED_BY")
    assert not _writes_matching(writes, "MERGE (a)-[:LOCATED_AT]->(pl)")
