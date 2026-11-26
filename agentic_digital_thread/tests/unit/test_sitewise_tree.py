# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the SiteWise asset tree SFC writes into
(``agents/demo/sitewise_tree.py``, ADR-0082 decision 9).

A small in-memory SiteWise stands in for the service. The load-bearing test
drives onboarding's REAL hierarchy walk (``onboard._dp_sitewise_roots`` /
``_dp_sitewise_walk``) over the built tree: that walk is what turns SiteWise
into graph ``Asset`` / ``Property`` nodes, so if it reaches every process asset
by its site/area/line path, the graph consumers see the shape they already
read. No site, city or equipment literal appears here (ADR-0044 ratchet): every
expected name is derived from the generator.

Run:
  cd agentic_digital_thread && PYTHONPATH=. python3 -m pytest \\
    tests/unit/test_sitewise_tree.py -q --disable-socket --allow-unix-socket
"""
from __future__ import annotations

import itertools

import pytest

from agents.demo import sfc_config, sitewise_tree

_MODELS = sfc_config._load_by_path("sw_models_for_tree_test", sfc_config._MODELS_PATH,
                                   stub_modules=("boto3",))


class _Err(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeSiteWise:
    """Just enough of iotsitewise for the tree builder and the onboarding walk."""

    def __init__(self, with_models: bool = True) -> None:
        self._ids = (f"id-{n}" for n in itertools.count())
        self.models: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}
        self.parent: dict[str, str] = {}
        self.calls: list[str] = []
        if with_models:
            self._seed_models()

    def _seed_models(self) -> None:
        self._model("Enterprise", "Sites", "Site")
        self._model("Site", "Areas", "Area")
        self._model("Area", "Lines", "Line")
        line = self._model("Line")
        for proc in _MODELS.PROCESS_MODEL_NAMES:
            props = list(_MODELS.PROCESS_MEASUREMENTS[proc]) + list(_MODELS.HB_ROUTING)
            self._model(proc, props=props)
            self.models[line]["hier"].append(
                {"id": next(self._ids), "name": f"Processes_{proc}",
                 "childAssetModelId": self.model_id(proc)})

    def _model(self, name, hier=None, child=None, props=()):
        mid = next(self._ids)
        self.models[mid] = {"name": name, "props": [p["name"] for p in props], "hier": []}
        if hier:
            self.models[mid]["hier"].append(
                {"id": next(self._ids), "name": hier, "childAssetModelId": child})
        return mid

    def model_id(self, name):
        return next(m for m, v in self.models.items() if v["name"] == name)

    # -- API ----------------------------------------------------------------

    def get_paginator(self, op):
        fake = self

        class _P:
            def paginate(self, **kw):
                yield getattr(fake, op)(**kw)
        return _P()

    def list_asset_models(self, **_):
        return {"assetModelSummaries": [{"id": m, "name": v["name"]}
                                        for m, v in self.models.items()]}

    def describe_asset_model(self, assetModelId):
        model = self.models[assetModelId]
        return {"assetModelName": model["name"],
                "assetModelProperties": [{"name": n} for n in model["props"]],
                "assetModelHierarchies": model["hier"],
                "assetModelStatus": {"state": "ACTIVE"}}

    def update_asset_model(self, assetModelId, assetModelName,
                           assetModelProperties, assetModelHierarchies=(),
                           assetModelDescription=None):
        self.calls.append("update_model")
        self.models[assetModelId]["props"] = [p["name"] for p in assetModelProperties]
        self.models[assetModelId]["hier"] = [
            {"id": h.get("id") or next(self._ids), **h} for h in assetModelHierarchies]

    def create_asset_model(self, assetModelName, assetModelProperties,
                           assetModelHierarchies=()):
        self.calls.append("create_model")
        mid = next(self._ids)
        self.models[mid] = {
            "name": assetModelName, "props": [p["name"] for p in assetModelProperties],
            "hier": [{"id": next(self._ids), **h} for h in assetModelHierarchies]}
        return {"assetModelId": mid}

    def _summary(self, aid):
        a = self.assets[aid]
        return {"id": aid, "name": a["name"], "assetModelId": a["model"]}

    def list_assets(self, assetModelId, filter="ALL"):
        return {"assetSummaries": [
            self._summary(a) for a, v in self.assets.items()
            if v["model"] == assetModelId and (filter != "TOP_LEVEL" or a not in self.parent)]}

    def list_associated_assets(self, assetId, hierarchyId, traversalDirection):
        return {"assetSummaries": [
            self._summary(c) for c, p in self.parent.items()
            if p == assetId and self.assets[c]["via"] == hierarchyId]}

    def describe_asset(self, assetId):
        if assetId.startswith("externalId:"):
            ext = assetId.split(":", 1)[1]
            assetId = next((a for a, v in self.assets.items() if v.get("ext") == ext), None)
            if assetId is None:
                raise _Err("ResourceNotFoundException")
        a = self.assets[assetId]
        model = self.models[a["model"]]
        return {
            "assetId": assetId, "assetName": a["name"], "assetModelId": a["model"],
            "assetExternalId": a.get("ext"), "assetStatus": {"state": "ACTIVE"},
            "assetHierarchies": [{"id": h["id"], "name": h["name"]} for h in model["hier"]],
            "assetProperties": [{"id": f"{assetId}/{p}", "name": p} for p in model["props"]],
        }

    def create_asset(self, assetName, assetModelId, assetExternalId=None):
        self.calls.append("create")
        aid = next(self._ids)
        self.assets[aid] = {"name": assetName, "model": assetModelId, "ext": assetExternalId}
        return {"assetId": aid}

    def update_asset(self, assetId, assetName, assetExternalId):
        self.calls.append("update")
        self.assets[assetId].update(name=assetName, ext=assetExternalId)

    def associate_assets(self, assetId, hierarchyId, childAssetId):
        self.calls.append("associate")
        assert childAssetId not in self.parent, "child already has a parent"
        self.parent[childAssetId] = assetId
        self.assets[childAssetId]["via"] = hierarchyId


@pytest.fixture(scope="module")
def specs():
    return sfc_config.asset_tree()


def _build(client, specs):
    return sitewise_tree.ensure_tree(client, specs, sleep=lambda _s: None, log=lambda _m: None)


def test_fresh_account_builds_the_whole_tree(specs):
    sw = FakeSiteWise()
    stats = _build(sw, specs)
    assert stats["created"] == len(specs) == len(sw.assets)
    assert stats["associated"] == len(specs) - 1          # everything but the root
    exts = [a["ext"] for a in sw.assets.values()]
    assert len(set(exts)) == len(exts) == len(specs)
    (root,) = [a for a in sw.assets if a not in sw.parent]
    assert sw.models[sw.assets[root]["model"]]["name"] == "Enterprise"


def test_rerun_changes_nothing(specs):
    sw = FakeSiteWise()
    _build(sw, specs)
    sw.calls.clear()
    stats = _build(sw, specs)
    assert sw.calls == []
    assert stats["kept"] == len(specs)


def test_story_mill_reuses_the_existing_asset(specs):
    """The feed Lambda / workshop seed create the story asset top-level; the
    tree adopts that exact asset under its line instead of making a second."""
    (story,) = [s for s in specs if s.adopt_by_name]
    sw = FakeSiteWise()
    existing = sw.create_asset(story.name, sw.model_id(story.model))["assetId"]
    _build(sw, specs)
    same_name = [a for a, v in sw.assets.items() if v["name"] == story.name]
    assert same_name == [existing]
    assert sw.assets[existing]["ext"] == story.external_id
    line = next(s for s in specs if s.external_id == story.parent)
    assert sw.assets[sw.parent[existing]]["name"] == line.name


def test_a_highbyte_built_tree_is_kept_not_duplicated(specs):
    """Assets HighByte created (names only, no external ids) are reused."""
    seeded = FakeSiteWise()
    _build(seeded, specs)
    for a in seeded.assets.values():
        a["ext"] = None
    seeded.calls.clear()
    stats = _build(seeded, specs)
    assert "create" not in seeded.calls and "associate" not in seeded.calls
    assert stats["adopted"] == len(specs)
    assert {a["ext"] for a in seeded.assets.values()} == {s.external_id for s in specs}


class SlowSiteWise(FakeSiteWise):
    """SiteWise as it behaves live: a created or (re)associated asset sits in
    UPDATING for ``latency`` ticks, a tick being one ``sleep`` by the builder,
    and ``associate_assets`` on a busy parent raises the conflict error."""

    def __init__(self, latency: int = 3) -> None:
        super().__init__()
        self.latency, self.tick, self.busy_until = latency, 0, {}
        self.sleeps = 0

    def sleep(self, _s: float) -> None:
        self.sleeps += 1
        self.tick += 1

    def _busy(self, aid: str) -> bool:
        return self.busy_until.get(aid, -1) > self.tick

    def _touch(self, *aids: str) -> None:
        for aid in aids:
            self.busy_until[aid] = self.tick + self.latency

    def describe_asset(self, assetId):
        desc = super().describe_asset(assetId)
        if self._busy(desc["assetId"]):
            desc["assetStatus"] = {"state": "UPDATING"}
        return desc

    def create_asset(self, assetName, assetModelId, assetExternalId=None):
        out = super().create_asset(assetName, assetModelId, assetExternalId)
        self._touch(out["assetId"])
        return out

    def associate_assets(self, assetId, hierarchyId, childAssetId):
        if self._busy(assetId) or self._busy(childAssetId):
            raise _Err("ConflictingOperationException")
        super().associate_assets(assetId, hierarchyId, childAssetId)
        self._touch(assetId, childAssetId)


def _shape(specs) -> tuple[int, int]:
    """(levels, sum over levels of the largest fan-out under one parent)."""
    by = {s.external_id: s for s in specs}

    def depth(s):
        d = 0
        while s.parent:
            s, d = by[s.parent], d + 1
        return d
    rounds = 0
    for level in range(max(map(depth, specs)) + 1):
        fan: dict[str | None, int] = {}
        for s in specs:
            if depth(s) == level and s.parent:
                fan[s.parent] = fan.get(s.parent, 0) + 1
        rounds += max(fan.values(), default=0)
    return max(map(depth, specs)) + 1, rounds


def test_fresh_build_waits_per_level_not_per_asset(specs):
    """Live, the custom resource timed out at the 15-minute Lambda ceiling
    because every asset was created, polled to ACTIVE, associated and polled
    again one at a time. The walk must settle a whole level at once, so the
    polls grow with depth and fan-out, never with the asset count."""
    sw = SlowSiteWise(latency=3)
    stats = sitewise_tree.ensure_tree(sw, specs, sleep=sw.sleep, log=lambda _m: None)
    assert stats["created"] == len(specs) and stats["associated"] == len(specs) - 1
    levels, rounds = _shape(specs)
    # one settle per level for the creates, one per association round, each
    # ``latency`` ticks long; serial would be ~2 * latency * len(specs).
    assert sw.sleeps <= (levels + rounds) * sw.latency + levels + rounds
    assert sw.sleeps < len(specs) * sw.latency


def test_missing_models_fail_loudly(specs):
    sw = FakeSiteWise()
    del sw.models[sw.model_id("Line")]
    with pytest.raises(sitewise_tree.TreeError, match="DeploySitewiseModels"):
        _build(sw, specs)


def test_onboarding_walk_reaches_every_process_asset_by_its_path(specs, monkeypatch):
    """Onboarding's own walk (rooted at the Site assets) must find every asset
    SFC writes to, at the site/area/line/asset path HighByte's tree gave it."""
    onboard = pytest.importorskip("agents.onboard")
    sw = FakeSiteWise()
    _build(sw, specs)
    writes: list[dict] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda _q, params: writes.append(params))

    sw_models = {m: v["name"] for m, v in sw.models.items()}
    visited = 0
    roots = onboard._dp_sitewise_roots(sw, sw_models)
    for asset, model_name in roots:
        visited += onboard._dp_sitewise_walk(sw, sw_models, "sys", asset["id"], asset["name"])
    assert len(roots) == len(sfc_config.SITES)
    assert visited == len(specs) - 1                       # every asset below the root

    paths = {p["path"] for p in writes if "path" in p and "sid" in p and "level" not in p}
    by_ext = {s.external_id: s for s in specs}

    def path(spec):
        chain = []
        while spec is not None and spec.parent is not None:
            chain.append(spec.name)
            spec = by_ext.get(spec.parent)
        return "/".join(reversed(chain))

    asset_paths = {p["path"] for p in writes if "level" in p}
    for ch in sfc_config.resolve_channels():
        spec = by_ext[ch.asset_external_id]
        assert path(spec) in asset_paths, path(spec)
        assert f"{path(spec)}/{ch.prop}" in paths


def test_generator_addresses_exactly_the_tree_assets(specs):
    cfg = sfc_config.generate_sfc_config(
        sfc_config.placeholder_endpoints(), region="r", table_bucket="b",
        namespace="n", table_name="t")
    assets = cfg["Targets"]["sitewise-target"]["Assets"]
    tree = {s.external_id: s for s in specs}
    for entry in assets:
        assert set(entry) == {"AssetExternalId", "Properties"}
        spec = tree[entry["AssetExternalId"]]
        types = sfc_config.MODEL_PROPERTY_TYPES[spec.model]
        for prop in entry["Properties"]:
            assert prop["DataType"] == types[prop["PropertyName"]].lower()
    written = {e["AssetExternalId"] for e in assets}
    assert written == {s.external_id for s in specs
                       if s.model not in ("Enterprise", "Site", "Area", "Line")}


def test_stack_custom_resource_creates_models_then_tree(specs, monkeypatch):
    """The Lambda the SFC stack deploys (lambda-sfc-sitewise-tree/handler.py,
    with its two vendored files) builds everything from an EMPTY account — the
    Workshop Studio case, where deploy.sh never ran — and Delete is a no-op."""
    import dataclasses
    import importlib
    import json
    import sys
    from pathlib import Path

    repo = Path(sitewise_tree.__file__).resolve().parents[3]
    for d in (repo / "lambda-sfc-sitewise-tree", repo / "highbyte",
              repo / "agentic_digital_thread" / "agents" / "demo"):
        monkeypatch.syspath_prepend(str(d))
    for name in ("handler", "DeploySitewiseModels", "sitewise_tree"):
        sys.modules.pop(name, None)
    handler = importlib.import_module("handler")
    monkeypatch.setattr(handler.DeploySitewiseModels.time, "sleep", lambda _s: None)
    monkeypatch.setattr(handler.sitewise_tree.time, "sleep", lambda _s: None)
    sw = FakeSiteWise(with_models=False)
    monkeypatch.setattr(handler.boto3, "client", lambda *_a, **_k: sw)

    event = {"RequestType": "Create", "ResourceProperties": {
        "Specs": json.dumps([dataclasses.asdict(s) for s in specs])}}
    out = handler.handler(event, None)
    assert out["PhysicalResourceId"] == handler.PHYSICAL_ID
    assert sw.calls.count("create_model") == len(_MODELS.PROCESS_MODEL_NAMES) + 4
    assert len(sw.assets) == len(specs)

    sw.calls.clear()
    handler.handler({**event, "RequestType": "Update"}, None)
    assert "create" not in sw.calls and "create_model" not in sw.calls
    handler.handler({"RequestType": "Delete", "PhysicalResourceId": out["PhysicalResourceId"]}, None)
    assert len(sw.assets) == len(specs)
    for name in ("handler", "DeploySitewiseModels", "sitewise_tree"):
        sys.modules.pop(name, None)
