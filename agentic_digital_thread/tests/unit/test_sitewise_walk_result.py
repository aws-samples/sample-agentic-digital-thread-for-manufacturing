# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SiteWise (Direct) hierarchy walk's counters must reach the provision
result AND the catalog entry — the walk is non-fatal, so without them a
zero-asset onboard is visible only as an ECS log line.

Live finding (2026-09-28, rehearsal R3): the workshop seed printed
``provision: OK`` while onboarding logged "0 assets walked (28 models, no
roots) — no Asset/Property nodes written". The seed polls the CATALOG (the
authoritative onboarded signal), so the counters ride the catalog entry the
same way the i3X walker's ``i3x_walk`` does, and the provision payload.

Pure in-process: the fake ``iotsitewise`` client from the graph-skeleton
suite stands in for boto3; graph writes are recorded, never sent.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import agents.onboard as onboard
from tests.unit.test_sitewise_direct_graph_skeleton import (
    ASSET_ID, MODEL_ID, PROPERTIES, SYSTEM_ID, FakeSiteWise,
)


@pytest.fixture
def writes(monkeypatch):
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: recorded.append((cypher, params or {})))
    return recorded


@pytest.fixture
def entry():
    """The catalog entry the walk annotates (patched in, never the global)."""
    catalog_entry = {"id": SYSTEM_ID, "status": "registered",
                     "connection_type": "SiteWise (Direct)"}
    with patch.object(onboard.catalog, "get",
                      side_effect=lambda sid: catalog_entry if sid == SYSTEM_ID else None):
        yield catalog_entry


def _ctx():
    return onboard._ProvisionState(
        body=None, system_id=SYSTEM_ID, system_name="OT Direct", plant="",
        schema="sitewise", rules=[], mappings=[], connection_type="SiteWise (Direct)",
        cm_items=[],
    )


def _walk(monkeypatch, fake):
    monkeypatch.setattr(onboard, "_dp_sitewise_client", lambda _cfg: fake)
    ctx = _ctx()
    onboard._dp_sitewise_hierarchy(ctx, {})
    return ctx


def test_walk_counters_land_on_ctx_and_catalog_entry(monkeypatch, writes, entry):
    ctx = _walk(monkeypatch, FakeSiteWise())
    expected = {"assets_walked": 1, "properties_written": len(PROPERTIES),
                "models": 1, "roots": 1}
    assert ctx.sitewise_walk == expected
    assert entry[onboard.SITEWISE_WALK_KEY] == expected


def test_child_assets_and_their_properties_are_counted(monkeypatch, writes, entry):
    child = {"id": "asset-0002", "name": "Machine-B", "assetModelId": MODEL_ID}
    fake = FakeSiteWise(described={
        ASSET_ID: {"assetId": ASSET_ID, "assetHierarchies": [{"id": "h-1"}],
                   "assetProperties": list(PROPERTIES)},
        "asset-0002": {"assetId": "asset-0002", "assetHierarchies": [],
                       "assetProperties": PROPERTIES[:1]},
    })
    real = fake.get_paginator

    def get_paginator(name):
        if name == "list_associated_assets":
            class _P:
                def paginate(self, **kw):
                    return [{"assetSummaries": [child] if kw["assetId"] == ASSET_ID else []}]
            return _P()
        return real(name)
    fake.get_paginator = get_paginator

    ctx = _walk(monkeypatch, fake)
    assert ctx.sitewise_walk["assets_walked"] == 2
    assert ctx.sitewise_walk["properties_written"] == len(PROPERTIES) + 1


def test_zero_asset_walk_is_recorded_as_zero_not_absent(monkeypatch, writes, entry):
    ctx = _walk(monkeypatch, FakeSiteWise(assets={MODEL_ID: []}))
    assert ctx.sitewise_walk == {"assets_walked": 0, "properties_written": 0,
                                 "models": 1, "roots": 0}
    assert entry[onboard.SITEWISE_WALK_KEY]["assets_walked"] == 0


def test_failed_walk_is_recorded_with_the_error_class(monkeypatch, writes, entry):
    def boom(_cfg):
        raise PermissionError("AccessDenied")
    monkeypatch.setattr(onboard, "_dp_sitewise_client", boom)
    ctx = _ctx()
    onboard._dp_sitewise_hierarchy(ctx, {})  # still non-fatal
    assert ctx.sitewise_walk["assets_walked"] == 0
    assert ctx.sitewise_walk["error"] == "PermissionError"
    assert entry[onboard.SITEWISE_WALK_KEY]["error"] == "PermissionError"


def test_walk_that_fails_partway_keeps_its_partial_count_and_the_error(monkeypatch, writes, entry):
    """A describe failure AFTER the root was written: the count is non-zero
    but the spine is partial — the error must survive next to the count so
    the seed gate can fail it (assets_walked alone reads healthy)."""
    fake = FakeSiteWise()

    def describe_asset(assetId):
        raise TimeoutError("describe timed out")
    fake.describe_asset = describe_asset
    ctx = _walk(monkeypatch, fake)
    assert ctx.sitewise_walk["error"] == "TimeoutError"
    assert ctx.sitewise_walk["roots"] == 1
    assert entry[onboard.SITEWISE_WALK_KEY]["error"] == "TimeoutError"


def test_skipped_listing_reaches_the_catalog_entry(monkeypatch, writes, entry):
    ctx = _walk(monkeypatch, FakeSiteWise(fail={None}))
    expected = [{"model": "(all models)", "filter": "TOP_LEVEL",
                 "error_class": "ConnectionError"}]
    assert ctx.sitewise_walk["listing_errors"] == expected
    assert entry[onboard.SITEWISE_WALK_KEY]["listing_errors"] == expected


def _result(ctx):
    with patch.object(onboard, "_dp_written_artifacts", return_value=([], [])), \
         patch.object(onboard, "_dp_reapply_self_learned", return_value=0):
        return onboard._dp_result(ctx)


def test_provision_result_carries_the_walk_for_sitewise_systems(monkeypatch, writes, entry):
    ctx = _walk(monkeypatch, FakeSiteWise())
    assert _result(ctx)[onboard.SITEWISE_WALK_KEY] == ctx.sitewise_walk


def test_provision_result_has_no_walk_key_for_non_sitewise_systems():
    assert onboard.SITEWISE_WALK_KEY not in _result(_ctx())
