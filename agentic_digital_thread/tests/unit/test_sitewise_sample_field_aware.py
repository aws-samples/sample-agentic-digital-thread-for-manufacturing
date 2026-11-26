# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SiteWise sampler returns only the values of the field it was asked for,
dispatching on each connector's field contract rather than guessing.

A SiteWise system keeps its join evidence as graph Asset nodes: an asset carries
``name`` (instance name / i3X displayName), ``path`` (hierarchy path),
``source_id`` (i3X elementId / Direct asset id), ``asset_type`` (the model),
``level`` (enterprise/site/area/line/equipment/component), and a HAS_PARENT edge
to its parent. Discovery exposes each asset model as a pseudo-table and, for
Direct, a single ``asset_hierarchy`` table.

The sampler maps each (connector pseudo-table, column) to a fixed graph
projection (``resolve_evidence._SW_MODEL_FIELD_PROJECTIONS`` /
``_SW_HIERARCHY_FIELD_PROJECTIONS``). Model tables scope by the exact model;
``asset_hierarchy`` name columns scope by the exact hierarchy level. Property
columns, and contract columns with no graph source yet, return []. No demo model
or site names: the models here are renamed and carry spaces and mixed case.
"""

from unittest.mock import patch

from agents import resolve_evidence as rev


# --------------------------------------------------------------------------- #
# A tiny graph engine over a node list, reading the sampler's own query shape:
# the MATCH's inline {level:/asset_type:} selectors, the HAS_PARENT traversal,
# and the projected attribute. This exercises the real Cypher the sampler
# builds rather than re-encoding the contract in the fake.
# --------------------------------------------------------------------------- #

def _make_fake_neptune(nodes):
    by_sid = {n["source_id"]: n for n in nodes}

    def fake(cypher, params=None):
        params = params or {}
        sel = [n for n in nodes if n.get("system") == params.get("sid")]
        if "asset_type: $model" in cypher:
            sel = [n for n in sel if n.get("asset_type") == params.get("model")]
        if "level: $level" in cypher:
            sel = [n for n in sel if n.get("level") == params.get("level")]
        out = []
        for n in sel:
            if "p.source_id" in cypher:  # HAS_PARENT traversal
                parent = by_sid.get(n.get("parent_sid"))
                val = parent.get("source_id") if parent else None
            elif "a.path" in cypher:
                val = n.get("path")
            elif "a.source_id" in cypher:
                val = n.get("source_id")
            elif "a.asset_type" in cypher:
                val = n.get("asset_type")
            else:
                val = n.get("name")
            if val is not None:
                out.append({"name": val})
        # DISTINCT, order-preserving.
        seen, dedup = set(), []
        for r in out:
            if r["name"] not in seen:
                seen.add(r["name"])
                dedup.append(r)
        return dedup

    return fake


# Direct per-model graph: two renamed models, disjoint assets, plus a hierarchy
# spine (site/area/line) and a parent edge, so the asset_hierarchy level columns
# and parent projection have data.
_DIRECT_NODES = [
    {"source_id": "s1", "system": "sys-sw", "name": "North Site", "path": "North Site",
     "asset_type": "Facility", "level": "site", "parent_sid": None},
    {"source_id": "a1", "system": "sys-sw", "name": "Press Area", "path": "North Site/Press Area",
     "asset_type": "Zone", "level": "area", "parent_sid": "s1"},
    {"source_id": "l1", "system": "sys-sw", "name": "Line 1", "path": "North Site/Press Area/Line 1",
     "asset_type": "Flow", "level": "line", "parent_sid": "a1"},
    {"source_id": "aa1", "system": "sys-sw", "name": "unit-aa-01",
     "path": "North Site/Press Area/Line 1/unit-aa-01", "asset_type": "WidgetPress",
     "level": "equipment", "parent_sid": "l1"},
    {"source_id": "aa2", "system": "sys-sw", "name": "unit-aa-02",
     "path": "North Site/Press Area/Line 1/unit-aa-02", "asset_type": "WidgetPress",
     "level": "equipment", "parent_sid": "l1"},
    {"source_id": "bb7", "system": "sys-sw", "name": "unit-bb-07",
     "path": "North Site/Press Area/Line 1/unit-bb-07", "asset_type": "CrateFiller",
     "level": "equipment", "parent_sid": "l1"},
]


def _sample(**kw):
    kw.setdefault("system_id", "sys-sw")
    kw.setdefault("connection_type", "SiteWise (Direct)")
    kw.setdefault("use_memo", False)
    return rev.sample_field_values(**kw)


# --------------------------------------------------------------------------- #
# Direct model tables: _hierarchyName / _path (kept behaviour), property empty.
# --------------------------------------------------------------------------- #

def test_direct_hierarchy_name_scoped_to_model():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="WidgetPress", field="WidgetPress._hierarchyName")
    assert sorted(vals) == ["unit-aa-01", "unit-aa-02"]
    assert "unit-bb-07" not in vals


def test_direct_two_models_do_not_return_the_same_sample():
    """The cross-model noise case: the two models' samples stay disjoint."""
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        a = _sample(table="WidgetPress", field="WidgetPress._hierarchyName")
        b = _sample(table="CrateFiller", field="CrateFiller._hierarchyName")
    assert sorted(a) == ["unit-aa-01", "unit-aa-02"]
    assert sorted(b) == ["unit-bb-07"]
    assert set(a).isdisjoint(set(b))


def test_direct_path_scoped_to_model():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="WidgetPress", field="WidgetPress._path")
    assert sorted(vals) == ["North Site/Press Area/Line 1/unit-aa-01",
                            "North Site/Press Area/Line 1/unit-aa-02"]


def test_direct_bare_hierarchy_field_uses_table_as_model():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="CrateFiller", field="_hierarchyName")
    assert sorted(vals) == ["unit-bb-07"]


def test_direct_property_column_returns_empty():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="WidgetPress", field="WidgetPress.TorqueValue")
    assert vals == []


# --------------------------------------------------------------------------- #
# Direct asset_hierarchy table (NOT a model). FAILS on 58437dee: that build
# treats asset_hierarchy as a model and rejects all six columns -> [].
# --------------------------------------------------------------------------- #

def test_asset_hierarchy_asset_id_gives_source_ids():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="asset_hierarchy", field="asset_hierarchy.asset_id")
    assert sorted(vals) == ["a1", "aa1", "aa2", "bb7", "l1", "s1"]


def test_asset_hierarchy_asset_model_gives_asset_types():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="asset_hierarchy", field="asset_hierarchy.asset_model")
    assert sorted(vals) == ["CrateFiller", "Facility", "Flow", "WidgetPress", "Zone"]


def test_asset_hierarchy_line_name_scoped_to_line_level():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="asset_hierarchy", field="asset_hierarchy.line_name")
    assert vals == ["Line 1"]


def test_asset_hierarchy_site_and_area_scoped_to_their_levels():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        site = _sample(table="asset_hierarchy", field="asset_hierarchy.site_name")
        area = _sample(table="asset_hierarchy", field="asset_hierarchy.area_name")
    assert site == ["North Site"]
    assert area == ["Press Area"]


def test_asset_hierarchy_process_name_scoped_to_equipment_level():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample(table="asset_hierarchy", field="asset_hierarchy.process_name")
    assert sorted(vals) == ["unit-aa-01", "unit-aa-02", "unit-bb-07"]
    # Never a site/area/line name: process_name is the equipment level only.
    assert "North Site" not in vals and "Line 1" not in vals


# --------------------------------------------------------------------------- #
# Guards (pass on old and new).
# --------------------------------------------------------------------------- #

def test_no_table_returns_all_asset_names_guard():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_DIRECT_NODES)):
        vals = _sample()
    assert "unit-aa-01" in vals and "unit-bb-07" in vals and "North Site" in vals


def test_sampler_is_best_effort_on_graph_error_guard():
    with patch("agents.db.neptune_query", side_effect=RuntimeError("boom")):
        assert _sample(table="WidgetPress", field="WidgetPress._hierarchyName") == []


# --------------------------------------------------------------------------- #
# SiteWise (i3X) model tables: displayName/path/elementId/parentElementId, model
# names with spaces and mixed case. elementId and parentElementId FAIL on
# 58437dee (not in the allowlist -> []); displayName/path were fixed in round 2.
# --------------------------------------------------------------------------- #

_I3X_NODES = [
    {"source_id": "e-plant", "system": "sys-i3x", "name": "Plant", "path": "Plant",
     "asset_type": "Site", "level": "site", "parent_sid": None},
    {"source_id": "e-cellA", "system": "sys-i3x", "name": "Cell A", "path": "Plant/Cell A",
     "asset_type": "Packaging Cell", "level": "equipment", "parent_sid": "e-plant"},
    {"source_id": "e-cellB", "system": "sys-i3x", "name": "Cell B", "path": "Plant/Cell B",
     "asset_type": "Packaging Cell", "level": "equipment", "parent_sid": "e-plant"},
    {"source_id": "e-robot7", "system": "sys-i3x", "name": "Robot 7", "path": "Plant/Robot 7",
     "asset_type": "Weld Robot", "level": "equipment", "parent_sid": "e-plant"},
]


def _sample_i3x(**kw):
    kw.setdefault("system_id", "sys-i3x")
    kw.setdefault("connection_type", "SiteWise (i3X)")
    kw.setdefault("use_memo", False)
    return rev.sample_field_values(**kw)


def test_i3x_displayname_qualified_and_bare():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        q = _sample_i3x(table="Packaging Cell", field="Packaging Cell.displayName")
        b = _sample_i3x(table="Weld Robot", field="displayName")
    assert sorted(q) == ["Cell A", "Cell B"]
    assert b == ["Robot 7"]


def test_i3x_path_qualified_and_bare():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        q = _sample_i3x(table="Packaging Cell", field="Packaging Cell.path")
        b = _sample_i3x(table="Weld Robot", field="path")
    assert sorted(q) == ["Plant/Cell A", "Plant/Cell B"]
    assert b == ["Plant/Robot 7"]


def test_i3x_elementid_gives_model_scoped_source_ids():
    """FAILS on 58437dee: elementId was not in the allowlist, so []."""
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        q = _sample_i3x(table="Packaging Cell", field="Packaging Cell.elementId")
        b = _sample_i3x(table="Weld Robot", field="elementId")
    assert sorted(q) == ["e-cellA", "e-cellB"]
    assert b == ["e-robot7"]
    assert "e-robot7" not in q  # exact model scope


def test_i3x_parentelementid_gives_parent_source_id_via_has_parent():
    """FAILS on 58437dee: parentElementId was not in the allowlist, so []."""
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        q = _sample_i3x(table="Packaging Cell", field="Packaging Cell.parentElementId")
    assert q == ["e-plant"]


def test_i3x_typeelementid_and_namespaceuri_are_empty():
    """These columns are not persisted on the Asset node; the contract marks
    them as no-graph-source and the sampler returns []."""
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        assert _sample_i3x(table="Packaging Cell", field="typeElementId") == []
        assert _sample_i3x(table="Packaging Cell", field="namespaceUri") == []


def test_i3x_property_column_returns_empty():
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        vals = _sample_i3x(table="Packaging Cell", field="Packaging Cell.Temperature")
    assert vals == []


def test_i3x_unknown_model_returns_empty_not_all_names():
    """Type-id fallback: when the i3X walker's object-type lookup fails it stores
    the type ID in asset_type while discovery names the table by display name, so
    the exact filter matches nothing. The sampler returns [] rather than all
    names, which would re-create the cross-model crosswalks this fix removes.
    No graph property ties the display-named table to that type ID."""
    with patch("agents.db.neptune_query", side_effect=_make_fake_neptune(_I3X_NODES)):
        assert _sample_i3x(table="No Such Model", field="displayName") == []


# --------------------------------------------------------------------------- #
# Contract completeness: every column each connector's discovery emits must have
# an explicit projection OR an explicit "no graph source" (None) entry, so a new
# discovery column can never silently fall through to the property-empty branch.
# --------------------------------------------------------------------------- #

def test_i3x_identifier_columns_are_all_in_the_model_contract():
    from agents.onboard_i3x import _IDENTIFIER_COLUMN_NAMES
    missing = [c for c in _IDENTIFIER_COLUMN_NAMES
               if c.lower() not in rev._SW_MODEL_FIELD_PROJECTIONS]
    assert missing == [], f"i3X identifier columns with no contract entry: {missing}"


def test_asset_hierarchy_columns_are_all_in_the_hierarchy_contract():
    from agents.onboard_agent import ASSET_HIERARCHY_COLUMNS
    missing = [c for c in ASSET_HIERARCHY_COLUMNS
               if c.lower() not in rev._SW_HIERARCHY_FIELD_PROJECTIONS]
    assert missing == [], f"asset_hierarchy columns with no contract entry: {missing}"


def test_every_contract_projection_key_is_resolvable():
    """Each non-None spec names a projection the sampler can resolve (an
    attribute key or the parent traversal). Catches a typo'd projection key."""
    for contract in (rev._SW_MODEL_FIELD_PROJECTIONS, rev._SW_HIERARCHY_FIELD_PROJECTIONS):
        for col, spec in contract.items():
            if spec is None:
                continue
            proj, _level = spec
            assert proj == "parent" or proj in rev._SW_ATTR_PROJECTIONS, \
                f"{col}: projection {proj!r} is not resolvable"


def test_emitted_asset_hierarchy_table_matches_the_contract(monkeypatch):
    """The columns and table name discovery actually emits are the ones the
    sampler contract and ASSET_HIERARCHY_COLUMNS name."""
    from agents import onboard_agent, sitewise_read

    monkeypatch.setattr(sitewise_read, "list_assets", lambda *a, **k: [])
    table = onboard_agent._sitewise_hierarchy_table([{"id": "m-1", "name": "Model A"}], {}, "us-east-1")
    emitted = tuple(c["name"] for c in table["columns"])
    assert emitted == onboard_agent.ASSET_HIERARCHY_COLUMNS
    assert table["name"] == onboard_agent.ASSET_HIERARCHY_TABLE == rev._SW_HIERARCHY_TABLE
    assert set(c.lower() for c in emitted) == set(rev._SW_HIERARCHY_FIELD_PROJECTIONS)
