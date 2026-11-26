# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for SiteWise renderer/property-view PARITY and the
graph-asset section fallback.

LIVE-CONFIRMED problems (both generic, no dataset literals):

* **Parity gap** — an asset model can return ZERO ``assetModelProperties`` from
  ``describe_asset_model`` while its values live as composite/path-based
  properties (data streams). Discovery and the read paths (section renderer,
  scalar/grouped resolvers, Mode A/B) ALL enumerate through the ONE shared seam
  ``sitewise_read.list_asset_models_with_properties`` — so the fix enriches that
  seam (composite-model properties folded into describe; a
  ``ListAssetModelProperties(filter='ALL')`` fallback for empty models) and
  parity holds by construction.

* **Non-model pseudo-table** — discovery intentionally retains a
  backward-compatible hierarchy pseudo-table that is NOT an asset model; the
  renderer now falls back to the graph's onboarded Asset nodes (one row per
  asset) instead of erroring, and keeps the honest error when the graph holds
  no assets.

Fully synthetic (no dataset names), no AWS/network.
"""
from __future__ import annotations

import pytest

import agents.aws_session as aws_session
import agents.entity_render as er
import agents.metric_resolver as mr
import agents.sitewise_read as sw
from agents.entity_render import SectionSpec
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric


# ---------------------------------------------------------------------------
# Fake iotsitewise client: one model whose describe returns ZERO top-level
# properties — the values are enumerable only via ListAssetModelProperties.
# ---------------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class _StreamModelClient:
    """A model with NO describe-properties but a rich stream enumeration."""

    def __init__(self):
        self.list_props_calls: list[str] = []

    def get_paginator(self, name):
        if name == "list_asset_models":
            return _FakePaginator([{"assetModelSummaries": [
                {"id": "mdl-stream", "name": "stream_model",
                 "status": {"state": "ACTIVE"}, "description": ""},
            ]}])
        if name == "list_asset_model_properties":
            self.list_props_calls.append("mdl-stream")
            return _FakePaginator([{"assetModelPropertySummaries": [
                {"id": "p-flow", "name": "flow_rate", "dataType": "DOUBLE", "unit": ""},
                {"id": "p-dev", "name": "device_ref", "dataType": "STRING", "unit": ""},
                {"id": "p-cnt", "name": "count_idx", "dataType": "INTEGER", "unit": "ea"},
            ]}])
        if name == "list_assets":
            return _FakePaginator([{"assetSummaries": [
                {"id": "asset-1", "name": "Unit-1", "assetModelId": "mdl-stream",
                 "status": {"state": "ACTIVE"}, "hierarchies": []},
            ]}])
        raise AssertionError(name)

    def describe_asset_model(self, assetModelId):  # noqa: N803 (boto3 kwarg)
        return {"assetModelId": assetModelId, "assetModelName": "stream_model",
                "assetModelDescription": "",
                "assetModelProperties": []}          # ZERO top-level properties

    def get_asset_property_value(self, assetId, propertyId):  # noqa: N803
        vals = {"p-flow": {"doubleValue": 7.5},
                "p-cnt": {"integerValue": 12}}
        if propertyId not in vals:
            raise RuntimeError("property value not readable for this kind")
        return {"propertyValue": {"value": vals[propertyId],
                                  "timestamp": {"timeInSeconds": 1700000000},
                                  "quality": "GOOD"}}

    def batch_get_asset_property_value(self, entries, nextToken=None):  # noqa: N803
        """Batch analogue of get_asset_property_value (the scalar/grouped
        resolvers read via the batch seam — event-loop fix): per-entry success
        or errorEntry, mirroring the real API's tolerance."""
        success, errors = [], []
        for e in entries:
            try:
                resp = self.get_asset_property_value(e["assetId"], e["propertyId"])
                success.append({"entryId": e["entryId"],
                                "assetPropertyValue": resp["propertyValue"]})
            except Exception as exc:  # noqa: BLE001 - fake mirrors API error entries
                errors.append({"entryId": e["entryId"],
                               "errorCode": "InternalFailureException",
                               "errorMessage": str(exc)})
        return {"successEntries": success, "errorEntries": errors,
                "skippedEntries": []}


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)


# ===========================================================================
# (A) The shared seam enumerates the RICHER property set
# ===========================================================================

def test_seam_falls_back_to_list_asset_model_properties_for_empty_model(monkeypatch):
    client = _StreamModelClient()
    _patch_client(monkeypatch, client)
    out = sw.list_asset_models_with_properties({}, "us-east-1")
    assert [m["name"] for m in out] == ["stream_model"]
    assert [p["name"] for p in out[0]["properties"]] == \
        ["flow_rate", "device_ref", "count_idx"]
    assert client.list_props_calls == ["mdl-stream"]


def test_seam_does_not_call_richer_enumeration_when_describe_has_properties(monkeypatch):
    client = _StreamModelClient()
    client.describe_asset_model = lambda assetModelId: {
        "assetModelId": assetModelId, "assetModelName": "stream_model",
        "assetModelDescription": "",
        "assetModelProperties": [
            {"id": "p-top", "name": "top_prop", "dataType": "DOUBLE", "unit": ""}],
    }
    _patch_client(monkeypatch, client)
    out = sw.list_asset_models_with_properties({}, "us-east-1")
    assert [p["name"] for p in out[0]["properties"]] == ["top_prop"]
    assert client.list_props_calls == []            # describe was sufficient


def test_describe_asset_model_folds_in_composite_model_properties(monkeypatch):
    client = _StreamModelClient()
    client.describe_asset_model = lambda assetModelId: {
        "assetModelId": assetModelId, "assetModelName": "stream_model",
        "assetModelDescription": "",
        "assetModelProperties": [
            {"id": "p-top", "name": "top_prop", "dataType": "DOUBLE", "unit": ""}],
        "assetModelCompositeModels": [{
            "name": "composite",
            "properties": [
                {"id": "p-comp", "name": "comp_prop", "dataType": "STRING", "unit": ""},
                {"id": "p-top", "name": "top_prop", "dataType": "DOUBLE", "unit": ""},
            ],
        }],
    }
    _patch_client(monkeypatch, client)
    detail = sw.describe_asset_model({}, "us-east-1", "mdl-stream")
    # Composite properties are included; duplicates de-duplicated by id.
    assert [p["name"] for p in detail["properties"]] == ["top_prop", "comp_prop"]


def test_seam_richer_enumeration_failure_is_empty_not_fatal(monkeypatch):
    client = _StreamModelClient()

    def boom(name):
        if name == "list_asset_model_properties":
            raise RuntimeError("AccessDeniedException")
        return _StreamModelClient.get_paginator(client, name)

    client.get_paginator = boom
    _patch_client(monkeypatch, client)
    out = sw.list_asset_models_with_properties({}, "us-east-1")
    assert out[0]["properties"] == []               # degraded, whole enumeration alive


# ===========================================================================
# (B) Renderer parity — a zero-describe model renders values through the seam
# ===========================================================================

_SW_SYSTEM = {"id": "sys-ot", "name": "OT System", "schema": "sys-ot",
              "connection_type": "SiteWise (Direct)", "system_type": "Historian"}

_STREAM_SPEC = SectionSpec(
    table="stream_model", entity="asset", entity_label="Asset",
    columns=[
        {"column": "flow_rate", "label": "Flow Rate", "field_concept": "machine-speed"},
        {"column": "device_ref", "label": "Device Ref", "field_concept": None},
        {"column": "count_idx", "label": "Count", "field_concept": "throughput"},
    ])


def test_renderer_renders_values_for_zero_describe_model(monkeypatch):
    _patch_client(monkeypatch, _StreamModelClient())
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"region": "us-east-1"})
    sec = er.render_section(_SW_SYSTEM, _STREAM_SPEC,
                            query_fn=lambda s, q: pytest.fail("no SQL for SiteWise"))
    assert sec["status"] == "ok"
    # Columns matched against the RICHER enumeration (asset + 3 props + timestamp).
    assert [c["column"] for c in sec["columns"]] == \
        ["asset", "flow_rate", "device_ref", "count_idx", "timestamp"]
    row = sec["rows"][0]
    assert row[0] == "Unit-1"
    assert row[1] == 7.5                            # readable value
    assert row[2] is None                           # unreadable kind → empty cell
    assert row[3] == 12
    # Never a whole-section error when at least one column resolved.
    assert "error" not in sec


# ===========================================================================
# (C) Scalar resolver parity — _execute_sitewise matches via the same seam
# ===========================================================================

def test_scalar_resolver_resolves_property_on_zero_describe_model(monkeypatch):
    from agents.catalog import catalog

    saved = catalog._systems
    catalog._systems = [{
        "id": "sys-ot", "name": "OT System",
        "connection_type": "SiteWise (Direct)", "schema": "",
        "status": "onboarded", "connection_config": {"region": "us-east-1"},
    }]
    try:
        _patch_client(monkeypatch, _StreamModelClient())
        monkeypatch.setattr(mr, "_full_connection_config",
                            lambda sid: {"region": "us-east-1"})
        metric = DirectMetric(id="m-flow", concept="machine-speed",
                              aggregation=Aggregation.MAX, label="Flow", unit="u")
        monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
        monkeypatch.setattr(
            mr, "neptune_query",
            lambda cypher, params=None: [
                {"table": "stream_model", "column": "flow_rate"}],
        )

        class _Auth(mr.AuthorityResolver):
            def authoritative_system(self, concept):
                return "sys-ot"

        resolved = mr.resolve_metric("m-flow", authority=_Auth())
        assert resolved.value == 7.5
        assert resolved.lineage.sql == "sitewise:max(stream_model.flow_rate)"
    finally:
        catalog._systems = saved


# ===========================================================================
# (D) Non-model pseudo-table → graph Asset rows (FIX C)
# ===========================================================================

_HIER_SPEC = SectionSpec(
    table="asset_hierarchy", entity="site", entity_label="Site",
    columns=[
        {"column": "site_name", "label": "Site", "field_concept": "site"},
        {"column": "asset_id", "label": "Asset Id", "field_concept": "equipment-id"},
    ])


def _patch_no_matching_model(monkeypatch):
    monkeypatch.setattr(
        "agents.sitewise_read.list_asset_models_with_properties",
        lambda cfg, region=None, **k: [
            {"id": "mdl-1", "name": "line_model", "properties": []}],
    )
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"region": "us-east-1"})


def test_non_model_table_with_graph_assets_renders_asset_rows(monkeypatch):
    _patch_no_matching_model(monkeypatch)
    captured = {}

    def fake_nq(cypher, params=None):
        captured["cypher"], captured["params"] = cypher, params
        return [
            {"name": "Plant-1", "asset_type": "Site", "path": "Plant-1", "level": "site"},
            {"name": "Cell-A", "asset_type": "line_model",
             "path": "Plant-1/Cell-A", "level": "line"},
        ]

    monkeypatch.setattr(er, "neptune_query", fake_nq)
    sec = er.render_section(_SW_SYSTEM, _HIER_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert [c["column"] for c in sec["columns"]] == \
        ["name", "asset_type", "path", "level"]
    assert sec["rows"] == [
        ["Plant-1", "Site", "Plant-1", "site"],
        ["Cell-A", "line_model", "Plant-1/Cell-A", "line"],
    ]
    assert sec["row_count"] == 2
    # The graph read is scoped to THIS system and bounded.
    assert captured["params"] == {"sid": "sys-ot"}
    assert f"LIMIT {er.MAX_ROWS}" in captured["cypher"]
    # Envelope contract intact.
    assert sec["kind"] == "table"
    assert len(sec["lineage"]) == len(sec["columns"])


def test_non_model_table_without_graph_assets_keeps_honest_error(monkeypatch):
    _patch_no_matching_model(monkeypatch)
    monkeypatch.setattr(er, "neptune_query", lambda cypher, params=None: [])
    sec = er.render_section(_SW_SYSTEM, _HIER_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0
    assert "SiteWise (Direct)" in sec["error"]


def test_matching_model_never_consults_graph_assets(monkeypatch):
    # The fallback keys off 'table matches NO asset model' — a matching model
    # renders natively and never reads Asset nodes.
    _patch_client(monkeypatch, _StreamModelClient())
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(
        er, "neptune_query",
        lambda *a, **k: pytest.fail("graph Assets must NOT be read for a real model"))
    sec = er.render_section(_SW_SYSTEM, _STREAM_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "ok"


def test_graph_asset_rows_capped_at_max_rows(monkeypatch):
    _patch_no_matching_model(monkeypatch)
    many = [{"name": f"A{i}", "asset_type": "t", "path": f"P/A{i}", "level": "equipment"}
            for i in range(er.MAX_ROWS + 50)]
    monkeypatch.setattr(er, "neptune_query", lambda cypher, params=None: many)
    sec = er.render_section(_SW_SYSTEM, _HIER_SPEC, query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert sec["row_count"] == er.MAX_ROWS
