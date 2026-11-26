# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise (Direct) provisioning must write the Asset / AssetModel / Property
graph skeleton whether or not the account has a model literally named ``Site``.

Live finding (2026-09-18 workshop test): the graph held Field/JoinPath/System
nodes but ZERO Asset, AssetModel or Property nodes after a SiteWise (Direct)
provision, because ``_dp_sitewise_hierarchy`` only walked when a ``Site`` model
existed and the Direct branch never wrote ``:Property`` nodes at all (only the
i3X walker did). Without the Asset node the equipment ValueMapping bridge
(``MAPS_TO_ASSET``) has nothing to anchor to.

Pure in-process: a fake ``iotsitewise`` client stands in for boto3 and the
graph writer is a recorder, so the emitted cypher can be asserted exactly.
"""
from __future__ import annotations

import logging

import pytest

import agents.onboard as onboard

SYSTEM_ID = "ot-direct"
MODEL_ID = "model-0001"
MODEL_NAME = "CNC"
ASSET_ID = "asset-0001"
ASSET_NAME = "Machine-A"
PROPERTIES = [
    {"id": "prop-1", "name": "Spindle Speed", "dataType": "DOUBLE", "unit": "rpm"},
    {"id": "prop-2", "name": "Feed Rate", "dataType": "DOUBLE", "unit": "mm/s"},
    {"id": "prop-3", "name": "Vibration", "dataType": "DOUBLE", "unit": "mm/s"},
    {"id": "prop-4", "name": "Run State", "dataType": "BOOLEAN"},
]


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages(kwargs)


class FakeSiteWise:
    """Minimal ``iotsitewise`` client: one model, one TOP_LEVEL asset, no
    hierarchies, four properties. Records the ``list_assets`` kwargs used.

    ``list_assets`` follows the botocore ListAssets contract the walk relies
    on: ``assetModelId`` is optional ONLY with ``filter='TOP_LEVEL'`` (which
    then lists every model's top-level assets); the default ``ALL`` filter
    requires it. Every summary carries ``assetModelId``. An asset associated
    under a parent is NOT top-level (``_child`` marks such fixtures).
    ``children`` maps a parent asset id to its CHILD summaries; ``fail`` is a
    set of ``assetModelId`` values (``None`` = the model-less TOP_LEVEL call)
    whose listing raises."""

    def __init__(self, models=None, assets=None, described=None, children=None, fail=()):
        self.models = models if models is not None else {MODEL_ID: MODEL_NAME}
        self.assets = assets if assets is not None else {
            MODEL_ID: [{"id": ASSET_ID, "name": ASSET_NAME, "assetModelId": MODEL_ID}],
        }
        self.described = described if described is not None else {
            ASSET_ID: {"assetId": ASSET_ID, "assetHierarchies": [],
                       "assetProperties": list(PROPERTIES)},
        }
        self.children = children or {}
        self.fail = set(fail)
        self.list_assets_calls: list[dict] = []

    def _list_assets(self, kw):
        self.list_assets_calls.append(dict(kw))
        model_id, top_level = kw.get("assetModelId"), kw.get("filter") == "TOP_LEVEL"
        if model_id in self.fail:
            raise ConnectionError(f"listing failed for {model_id}")
        if model_id is None and not top_level:
            raise AssertionError("ListAssets: assetModelId is required with filter ALL")
        listed = [a for mid, rows in self.assets.items() if model_id in (None, mid) for a in rows]
        if top_level:
            listed = [a for a in listed if not a.get("_child")]
        return [{"assetSummaries": listed}]

    def get_paginator(self, name):
        if name == "list_asset_models":
            return _Paginator(lambda kw: [{"assetModelSummaries": [
                {"id": mid, "name": mname} for mid, mname in self.models.items()]}])
        if name == "list_assets":
            return _Paginator(self._list_assets)
        if name == "list_associated_assets":
            return _Paginator(lambda kw: [{"assetSummaries": list(
                self.children.get(kw.get("assetId"), []))}])
        raise AssertionError(f"unexpected paginator {name}")

    def describe_asset(self, assetId):
        return self.described.get(assetId, {"assetId": assetId, "assetHierarchies": [],
                                             "assetProperties": []})


@pytest.fixture
def writes(monkeypatch):
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: recorded.append((cypher, params or {})))
    return recorded


def _ctx():
    return onboard._ProvisionState(
        body=None, system_id=SYSTEM_ID, system_name="OT Direct", plant="",
        schema="sitewise", rules=[], mappings=[], connection_type="SiteWise (Direct)",
        cm_items=[],
    )


def _run(monkeypatch, fake):
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(_ctx(), {})


def _matching(writes, *needles):
    return [(c, p) for c, p in writes if all(n in c for n in needles)]


def test_no_site_model_roots_walk_at_top_level_assets_and_writes_asset_skeleton(monkeypatch, writes):
    fake = FakeSiteWise()
    _run(monkeypatch, fake)

    # ONE model-less TOP_LEVEL listing (botocore: assetModelId is optional
    # with TOP_LEVEL), never a per-model loop.
    assert fake.list_assets_calls == [{"filter": "TOP_LEVEL"}]

    asset_nodes = _matching(writes, "MERGE (a:Asset {source_id: $sid})")
    assert [p["sid"] for _, p in asset_nodes] == [ASSET_ID]
    props = asset_nodes[0][1]
    assert props["name"] == ASSET_NAME and props["sys"] == SYSTEM_ID
    assert props["atype"] == MODEL_NAME and props["level"] == "equipment"

    instance_of = _matching(writes, "MERGE (m:AssetModel {name: $mname})", "INSTANCE_OF")
    assert [(p["mname"], p["sid"]) for _, p in instance_of] == [(MODEL_NAME, ASSET_ID)]

    from_system = _matching(writes, "(a:Asset {source_id: $sid}), (s:System {id: $sys})", "FROM_SYSTEM")
    assert [(p["sid"], p["sys"]) for _, p in from_system] == [(ASSET_ID, SYSTEM_ID)]

    # A top-level root has no parent: no HAS_PARENT edge for it.
    assert _matching(writes, "HAS_PARENT") == []


def test_direct_branch_writes_property_nodes_with_has_property_and_from_system(monkeypatch, writes):
    _run(monkeypatch, FakeSiteWise())

    prop_nodes = _matching(writes, "MERGE (p:Property {source_id: $sid})")
    assert sorted(p["sid"] for _, p in prop_nodes) == sorted(p["id"] for p in PROPERTIES)
    by_id = {p["sid"]: p for _, p in prop_nodes}
    spindle = by_id["prop-1"]
    assert spindle["name"] == "Spindle Speed"
    assert spindle["dtype"] == "DOUBLE"
    assert spindle["unit"] == "rpm"
    assert spindle["pid"] == ASSET_ID
    assert spindle["sys"] == SYSTEM_ID
    assert spindle["path"] == f"{ASSET_NAME}/Spindle Speed"
    assert by_id["prop-4"]["unit"] == ""          # absent unit is an empty string, not None

    has_property = _matching(writes, "HAS_PROPERTY")
    assert sorted((p["pid"], p["sid"]) for _, p in has_property) == sorted(
        (ASSET_ID, p["id"]) for p in PROPERTIES)

    prop_from_system = _matching(writes, "(p:Property {source_id: $sid}), (s:System {id: $sys})", "FROM_SYSTEM")
    assert sorted(p["sid"] for _, p in prop_from_system) == sorted(p["id"] for p in PROPERTIES)
    assert {p["sys"] for _, p in prop_from_system} == {SYSTEM_ID}

    stamps = _matching(writes, "SET a.property_count = $n")
    assert [(p["sid"], p["n"]) for _, p in stamps] == [(ASSET_ID, len(PROPERTIES))]


def test_site_model_still_roots_at_site_assets(monkeypatch, writes):
    site_id, site_asset = "model-site", "asset-site"
    fake = FakeSiteWise(
        models={"model-ent": "Enterprise", site_id: "Site", MODEL_ID: MODEL_NAME},
        assets={"model-ent": [{"id": "asset-ent", "name": "Corp", "assetModelId": "model-ent"}],
                site_id: [{"id": site_asset, "name": "Plant", "assetModelId": site_id}],
                MODEL_ID: [{"id": ASSET_ID, "name": ASSET_NAME, "assetModelId": MODEL_ID}]},
        described={site_asset: {"assetHierarchies": [{"id": "h1"}], "assetProperties": []},
                   ASSET_ID: {"assetHierarchies": [], "assetProperties": list(PROPERTIES)}},
    )
    _run(monkeypatch, fake)

    # Rooted at the Site asset; a CNC asset that is itself TOP_LEVEL (not
    # associated under the Site) is an orphan root too — never silently dropped.
    # The Enterprise model is never listed as a root: its subtree IS the Sites.
    assert fake.list_assets_calls[0] == {"assetModelId": site_id}
    assert all(call.get("assetModelId") != "model-ent" for call in fake.list_assets_calls)
    asset_nodes = _matching(writes, "MERGE (a:Asset {source_id: $sid})")
    assert [(p["sid"], p["level"]) for _, p in asset_nodes] == [
        (site_asset, "site"), (ASSET_ID, "equipment")]


def test_site_model_child_asset_is_reached_through_hierarchy_not_as_a_root(monkeypatch, writes):
    """An equipment asset associated UNDER a Site is written once, as a child
    (HAS_PARENT), never re-rooted by the TOP_LEVEL orphan pass."""
    site_id, site_asset = "model-site", "asset-site"
    child = {"id": ASSET_ID, "name": ASSET_NAME, "assetModelId": MODEL_ID, "_child": True}
    fake = FakeSiteWise(
        models={site_id: "Site", MODEL_ID: MODEL_NAME},
        assets={site_id: [{"id": site_asset, "name": "Plant", "assetModelId": site_id}],
                MODEL_ID: [child]},
        described={site_asset: {"assetHierarchies": [{"id": "h1"}], "assetProperties": []},
                   ASSET_ID: {"assetHierarchies": [], "assetProperties": list(PROPERTIES)}},
    )
    fake.get_paginator = _with_children(fake.get_paginator, {site_asset: [child]})
    _run(monkeypatch, fake)

    asset_nodes = _matching(writes, "MERGE (a:Asset {source_id: $sid})")
    assert [p["sid"] for _, p in asset_nodes] == [site_asset, ASSET_ID]
    assert [(p["cid"], p["pid"]) for _, p in _matching(writes, "HAS_PARENT")] == [(ASSET_ID, site_asset)]


def test_empty_site_model_falls_back_to_top_level_roots(monkeypatch, writes):
    """Live finding 2026-09-28 (rehearsal R3): an account carrying the 28
    HighByte models has a ``Site`` model with ZERO assets. The Site-only
    rooting returned no roots ("0 assets walked (28 models, no roots)") and the
    workshop's TOP_LEVEL CNC asset never reached the graph — no Asset,
    AssetModel, Property nodes and no equipment ValueMapping."""
    site_id, enterprise_id = "model-site", "model-enterprise"
    fake = FakeSiteWise(
        models={site_id: "Site", enterprise_id: "Enterprise", MODEL_ID: MODEL_NAME},
        assets={site_id: [], enterprise_id: [],
                MODEL_ID: [{"id": ASSET_ID, "name": ASSET_NAME, "assetModelId": MODEL_ID}]},
    )
    _run(monkeypatch, fake)

    asset_nodes = _matching(writes, "MERGE (a:Asset {source_id: $sid})")
    assert [(p["sid"], p["level"]) for _, p in asset_nodes] == [(ASSET_ID, "equipment")]
    assert len(_matching(writes, "MERGE (p:Property {source_id: $sid})")) == len(PROPERTIES)
    instance_of = _matching(writes, "MERGE (m:AssetModel {name: $mname})", "INSTANCE_OF")
    assert [p["mname"] for _, p in instance_of] == [MODEL_NAME]


def _with_children(get_paginator, children_by_parent):
    """Wrap a fake's ``get_paginator`` so ``list_associated_assets`` returns
    the given CHILD summaries per parent asset id."""
    def wrapped(name):
        if name == "list_associated_assets":
            return _Paginator(lambda kw: [{"assetSummaries": list(
                children_by_parent.get(kw.get("assetId"), []))}])
        return get_paginator(name)
    return wrapped


def test_zero_assets_walked_is_logged_as_a_warning(monkeypatch, writes, caplog):
    fake = FakeSiteWise(assets={MODEL_ID: []})
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        _run(monkeypatch, fake)
    assert any(r.levelno == logging.WARNING and "0 assets" in r.getMessage() and SYSTEM_ID in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert _matching(writes, "MERGE (a:Asset") == []


# ── Hardening: type-based levels, per-listing isolation,
#    one TOP_LEVEL listing, no asset written or counted twice ────────────────

def _asset(aid, name, model_id, child=False):
    return {"id": aid, "name": name, "assetModelId": model_id, **({"_child": True} if child else {})}


def _hierarchical(ids):
    """describe_asset rows: every id gets one hierarchy (so its CHILD list is
    queried) and one property named after it."""
    return {aid: {"assetId": aid, "assetHierarchies": [{"id": f"h-{aid}"}],
                  "assetProperties": [{"id": f"p-{aid}", "name": f"P {aid}"}]}
            for aid in ids}


def _asset_levels(writes):
    return [(p["sid"], p["level"]) for _, p in _matching(writes, "MERGE (a:Asset {source_id: $sid})")]


def _parents(writes):
    return [(p["cid"], p["pid"]) for _, p in _matching(writes, "HAS_PARENT")]


def _sequence(writes):
    """Every Asset-layer write in order, reduced to (kind, key) — pins the
    exact walk order, not just the final set."""
    kinds = (("MERGE (a:Asset {source_id: $sid})", "asset", "sid"),
             ("HAS_PARENT", "parent", "cid"),
             ("INSTANCE_OF", "instance_of", "sid"),
             ("(a:Asset {source_id: $sid}), (s:System", "asset_system", "sid"),
             ("MERGE (p:Property {source_id: $sid})", "property", "sid"),
             ("SET a.property_count", "property_count", "sid"))
    seq = []
    for cypher, params in writes:
        for needle, kind, key in kinds:
            if needle in cypher:
                seq.append((kind, params[key]))
                break
    return seq


_HB_MODELS = {"m-ent": "Enterprise", "m-site": "Site", "m-area": "Area",
              "m-line": "Line", "m-proc": "ProcessUnit", "m-part": "Drive"}


def _canonical_fake():
    """HighByte canonical tree: Enterprise > Site > Area > Line > process >
    part (the part sits at depth 4 from the Site root)."""
    ent, site = _asset("ent", "Corp", "m-ent"), _asset("site", "Plant", "m-site", child=True)
    area, line = _asset("area", "Pack", "m-area", child=True), _asset("line", "L1", "m-line", child=True)
    proc, part = _asset("proc", "Unit1", "m-proc", child=True), _asset("part", "M1", "m-part", child=True)
    return FakeSiteWise(
        models=dict(_HB_MODELS),
        assets={"m-ent": [ent], "m-site": [site], "m-area": [area], "m-line": [line],
                "m-proc": [proc], "m-part": [part]},
        described=_hierarchical(["ent", "site", "area", "line", "proc", "part"]),
        children={"ent": [site], "site": [area], "area": [line], "line": [proc], "proc": [part]},
    )


def test_canonical_highbyte_tree_levels_and_write_sequence_are_unchanged(monkeypatch, writes):
    """M1 must be output-identical for the canonical tree: type-derived levels
    agree with the old depth table at every tier (Site root; the Enterprise
    root is still skipped because its subtree IS the Site)."""
    _run(monkeypatch, _canonical_fake())

    assert _asset_levels(writes) == [("site", "site"), ("area", "area"), ("line", "line"),
                                     ("proc", "equipment"), ("part", "component")]
    assert _parents(writes) == [("area", "site"), ("line", "area"), ("proc", "line"), ("part", "proc")]
    root = [("asset", "site"), ("instance_of", "site"), ("asset_system", "site")]

    def child(aid):
        return [("asset", aid), ("parent", aid), ("instance_of", aid), ("asset_system", aid)]

    def own(aid):
        return [("property", f"p-{aid}"), ("property_count", aid)]
    assert _sequence(writes) == (root + own("site") + child("area") + own("area")
                                 + child("line") + own("line") + child("proc") + own("proc")
                                 + child("part") + own("part"))


def test_call_shape_is_site_listing_then_one_model_less_top_level_listing(monkeypatch, writes):
    fake = _canonical_fake()
    _run(monkeypatch, fake)
    assert fake.list_assets_calls == [{"assetModelId": "m-site"}, {"filter": "TOP_LEVEL"}]


def test_orphan_line_root_labels_its_subtree_by_type_not_depth(monkeypatch, writes):
    """An orphan Line root (TOP_LEVEL, beside a Site) must not relabel its
    process child as 'area' (the old depth table's depth-1 slot). The orphan
    root has no HAS_PARENT to any Site."""
    site, line = _asset("site", "Plant", "m-site"), _asset("line9", "L9", "m-line")
    pump = _asset("pump9", "Unit9", "m-proc", child=True)
    fake = FakeSiteWise(
        models={"m-site": "Site", "m-line": "Line", "m-proc": "ProcessUnit"},
        assets={"m-site": [site], "m-line": [line], "m-proc": [pump]},
        described=_hierarchical(["site", "line9", "pump9"]),
        children={"line9": [pump]},
    )
    _run(monkeypatch, fake)
    assert _asset_levels(writes) == [("site", "site"), ("line9", "line"), ("pump9", "equipment")]
    assert _parents(writes) == [("pump9", "line9")]


def test_orphan_area_root_keeps_line_and_process_levels(monkeypatch, writes):
    area = _asset("area9", "A9", "m-area")
    line = _asset("line9", "L9", "m-line", child=True)
    proc = _asset("proc9", "U9", "m-proc", child=True)
    fake = FakeSiteWise(
        models={"m-area": "Area", "m-line": "Line", "m-proc": "ProcessUnit"},
        assets={"m-area": [area], "m-line": [line], "m-proc": [proc]},
        described=_hierarchical(["area9", "line9", "proc9"]),
        children={"area9": [line], "line9": [proc]},
    )
    _run(monkeypatch, fake)
    assert _asset_levels(writes) == [("area9", "area"), ("line9", "line"), ("proc9", "equipment")]


def test_top_level_site_asset_is_rooted_once_not_twice(monkeypatch, writes):
    """A Site asset is both in the Site listing AND top-level: one root."""
    site = _asset("site", "Plant", "m-site")
    area = _asset("area", "Pack", "m-area", child=True)
    fake = FakeSiteWise(models={"m-site": "Site", "m-area": "Area"},
                        assets={"m-site": [site], "m-area": [area]},
                        described=_hierarchical(["site", "area"]), children={"site": [area]})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(ctx, {})
    assert _asset_levels(writes) == [("site", "site"), ("area", "area")]
    assert ctx.sitewise_walk["roots"] == 1
    assert ctx.sitewise_walk["assets_walked"] == 2
    assert ctx.sitewise_walk["properties_written"] == 2


def test_asset_reached_twice_in_one_walk_is_written_and_counted_once(monkeypatch, writes):
    """The same child listed under two hierarchies (or two parents) is
    visited once — no second write, no double count."""
    site = _asset("site", "Plant", "m-site")
    area_a, area_b = _asset("aa", "A", "m-area", child=True), _asset("ab", "B", "m-area", child=True)
    line = _asset("line", "L1", "m-line", child=True)
    described = _hierarchical(["site", "aa", "ab", "line"])
    described["site"]["assetHierarchies"] = [{"id": "h1"}, {"id": "h2"}]
    fake = FakeSiteWise(models={"m-site": "Site", "m-area": "Area", "m-line": "Line"},
                        assets={"m-site": [site], "m-area": [area_a, area_b], "m-line": [line]},
                        described=described,
                        children={"site": [area_a, area_b], "aa": [line], "ab": [line]})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(ctx, {})
    sids = [sid for sid, _ in _asset_levels(writes)]
    # Both hierarchies of ``site`` list aa+ab; ``line`` is under aa AND ab.
    assert sids == ["site", "aa", "line", "ab"]
    assert _parents(writes) == [("aa", "site"), ("line", "aa"), ("ab", "site")]
    assert ctx.sitewise_walk["assets_walked"] == 4
    assert ctx.sitewise_walk["properties_written"] == 4


def test_non_enterprise_ancestor_of_sites_does_not_rewalk_or_relabel_them(monkeypatch, writes):
    """A ``Region`` TOP_LEVEL asset parenting Site assets is an extra root,
    but the Sites (already rooted) are neither re-written nor relabelled, and
    their subtrees are not walked a second time."""
    region = _asset("reg", "West", "m-reg")
    s1, s2 = _asset("s1", "P1", "m-site", child=True), _asset("s2", "P2", "m-site", child=True)
    area = _asset("a1", "Pack", "m-area", child=True)
    fake = FakeSiteWise(models={"m-reg": "Region", "m-site": "Site", "m-area": "Area"},
                        assets={"m-reg": [region], "m-site": [s1, s2], "m-area": [area]},
                        described=_hierarchical(["reg", "s1", "s2", "a1"]),
                        children={"reg": [s1, s2], "s1": [area]})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(ctx, {})
    assert _asset_levels(writes) == [("s1", "site"), ("a1", "area"), ("s2", "site"),
                                     ("reg", "equipment")]
    assert _parents(writes) == [("a1", "s1")]
    assert [p["sid"] for _, p in _matching(writes, "SET a.property_count")] == ["s1", "a1", "s2", "reg"]
    assert ctx.sitewise_walk["assets_walked"] == 4


def test_failed_site_listing_is_isolated_and_top_level_roots_still_walk(monkeypatch, writes, caplog):
    """M2: one failing model listing no longer aborts the whole hierarchy —
    it is logged (model id + exception class) and the walk continues."""
    fake = FakeSiteWise(models={"m-site": "Site", MODEL_ID: MODEL_NAME},
                        assets={"m-site": [], MODEL_ID: [_asset(ASSET_ID, ASSET_NAME, MODEL_ID)]},
                        fail={"m-site"})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        onboard._dp_sitewise_hierarchy(ctx, {})
    assert _asset_levels(writes) == [(ASSET_ID, "equipment")]
    assert "error" not in ctx.sitewise_walk
    assert any("m-site" in r.getMessage() and "ConnectionError" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING), \
        [r.getMessage() for r in caplog.records]


def test_failed_top_level_listing_keeps_the_site_roots(monkeypatch, writes, caplog):
    site = _asset("site", "Plant", "m-site")
    fake = FakeSiteWise(models={"m-site": "Site"}, assets={"m-site": [site]},
                        described=_hierarchical(["site"]), fail={None})
    with caplog.at_level(logging.WARNING, logger=onboard.logger.name):
        _run(monkeypatch, fake)
    assert _asset_levels(writes) == [("site", "site")]
    assert any("TOP_LEVEL" in r.getMessage() and "ConnectionError" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_skipped_site_listing_is_recorded_in_the_walk_counters(monkeypatch, writes):
    """A skipped listing is invisible to the seed gate unless it rides the
    counters: the walk still reaches assets, so ``assets_walked`` alone
    reads healthy over a partial spine."""
    fake = FakeSiteWise(models={"m-site": "Site", MODEL_ID: MODEL_NAME},
                        assets={"m-site": [], MODEL_ID: [_asset(ASSET_ID, ASSET_NAME, MODEL_ID)]},
                        fail={"m-site"})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(ctx, {})
    assert ctx.sitewise_walk["assets_walked"] == 1
    assert ctx.sitewise_walk["listing_errors"] == [
        {"model": "m-site", "filter": "ALL", "error_class": "ConnectionError"}]


def test_skipped_top_level_listing_is_recorded_as_all_models(monkeypatch, writes):
    site = _asset("site", "Plant", "m-site")
    fake = FakeSiteWise(models={"m-site": "Site"}, assets={"m-site": [site]},
                        described=_hierarchical(["site"]), fail={None})
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    onboard._dp_sitewise_hierarchy(ctx, {})
    assert ctx.sitewise_walk["listing_errors"] == [
        {"model": "(all models)", "filter": "TOP_LEVEL", "error_class": "ConnectionError"}]


def test_clean_walk_carries_no_listing_errors_key(monkeypatch, writes):
    ctx = _ctx()
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: FakeSiteWise())
    onboard._dp_sitewise_hierarchy(ctx, {})
    assert "listing_errors" not in ctx.sitewise_walk


def test_assets_below_an_extra_tier_keep_their_children(monkeypatch, writes):
    """Site > Area > Line > Station > Machine > Event: the machine sits at
    depth 4. The walk used to stop there, so the machine kept its own
    properties but its child assets (the event streams) were never written."""
    models = {"m-site": "Site", "m-area": "Area", "m-line": "Line",
              "m-st": "Station", "m-mc": "Machine", "m-ev": "Event"}
    ids = ["site", "area", "line", "st", "mc", "ev"]
    rows = {"site": _asset("site", "Plant", "m-site"),
            "area": _asset("area", "A1", "m-area", child=True),
            "line": _asset("line", "L1", "m-line", child=True),
            "st": _asset("st", "S1", "m-st", child=True),
            "mc": _asset("mc", "M1", "m-mc", child=True),
            "ev": _asset("ev", "M1-events", "m-ev", child=True)}
    fake = FakeSiteWise(
        models=models,
        assets={mid: [rows[aid]] for mid, aid in zip(models, ids)},
        described=_hierarchical(ids),
        children={p: [rows[c]] for p, c in zip(ids, ids[1:])},
    )
    _run(monkeypatch, fake)
    assert [sid for sid, _ in _asset_levels(writes)] == ids
    assert ("ev", "mc") in _parents(writes)
    assert {p["sid"] for _, p in _matching(writes, "MERGE (p:Property {source_id: $sid})")} == \
        {f"p-{aid}" for aid in ids}


def test_walk_cap_still_bounds_a_malformed_chain(monkeypatch, writes):
    """Every asset has a fresh child: the cap stops the walk, the seen set
    is not what ends it."""
    depth = onboard._SW_WALK_MAX_PARENT_DEPTH + 5
    ids = [f"a{i}" for i in range(depth)]
    models = {"m-site": "Site", "m-x": "Node"}
    rows = {aid: _asset(aid, aid, "m-site" if i == 0 else "m-x", child=i > 0)
            for i, aid in enumerate(ids)}
    fake = FakeSiteWise(models=models,
                        assets={"m-site": [rows["a0"]], "m-x": [rows[a] for a in ids[1:]]},
                        described=_hierarchical(ids),
                        children={p: [rows[c]] for p, c in zip(ids, ids[1:])})
    _run(monkeypatch, fake)
    written = [sid for sid, _ in _asset_levels(writes)]
    assert written == ids[:onboard._SW_WALK_MAX_PARENT_DEPTH + 2]
