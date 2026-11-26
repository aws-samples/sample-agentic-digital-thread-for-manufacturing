# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 2.1 — richer SiteWise (Direct) discovery: one pseudo-table PER asset model
whose columns are that model's properties (ADR-0037), PLUS the backward-compatible
``asset_hierarchy`` table.

Fully synthetic: ``sitewise_read`` is stubbed with ARBITRARY model/property names
(never a live customer's names). No AWS, no network, no dataset literals.
"""
from __future__ import annotations

import asyncio

import agents.onboard as onboard
import agents.sitewise_read as sitewise_read
from agents.models import DiscoverRequest


class _FakeCatalog:
    def get(self, system_id):
        return None


# SYNTHETIC discovery fixture — arbitrary model/property names.
_SYNTH_MODELS = [
    {"id": "mdl-evt", "name": "evt_model", "description": "", "status": "ACTIVE",
     "properties": [
         {"id": "p-dur", "name": "dur", "dataType": "DOUBLE", "unit": "s"},
         {"id": "p-from", "name": "from_st", "dataType": "STRING", "unit": ""},
         {"id": "p-to", "name": "to_st", "dataType": "STRING", "unit": ""},
     ]},
    {"id": "mdl-met", "name": "metric_model", "description": "", "status": "ACTIVE",
     "properties": [
         {"id": "p-amt", "name": "amt", "dataType": "DOUBLE", "unit": "ea"},
         {"id": "p-waste", "name": "waste", "dataType": "DOUBLE", "unit": "ea"},
     ]},
]


def _patch(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    monkeypatch.setattr(
        sitewise_read, "list_asset_models_with_properties",
        lambda cfg, region, max_models=None: [dict(m) for m in _SYNTH_MODELS],
    )
    # No live assets in this test — sampling/hierarchy gracefully degrade.
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])


def _discover(monkeypatch):
    _patch(monkeypatch)
    body = DiscoverRequest(system_id="", connection_type="SiteWise (Direct)")
    return asyncio.run(onboard.onboard_discover(body))


def test_discover_emits_one_pseudo_table_per_model(monkeypatch):
    result = _discover(monkeypatch)
    assert result["schema_name"] == "sitewise-direct"
    assert result["connection_type"] == "SiteWise (Direct)"
    names = {t["name"] for t in result["tables"]}
    assert "evt_model" in names
    assert "metric_model" in names
    assert "asset_hierarchy" in names  # backward-compatible table retained


def test_model_columns_are_properties_with_types(monkeypatch):
    result = _discover(monkeypatch)
    evt = next(t for t in result["tables"] if t["name"] == "evt_model")
    col_by_name = {c["name"]: c["data_type"] for c in evt["columns"]}
    assert set(col_by_name) == {"dur", "from_st", "to_st"}
    # dataType + unit carried in the type string; tagged as a sitewise property.
    assert "DOUBLE" in col_by_name["dur"]
    assert "(s)" in col_by_name["dur"]
    assert "sitewise property" in col_by_name["dur"]
    assert "STRING" in col_by_name["from_st"]


def test_ai_analysis_has_no_model_literals(monkeypatch):
    """The ai_analysis summary must not hardcode any specific model name."""
    result = _discover(monkeypatch)
    analysis = result["ai_analysis"].lower()
    assert "2 asset model" in analysis
    # generic concept vocabulary is fine; customer/dataset literals are not.
    for banned in ("state_event", "site1", "site2", "example-sitewise", "federated_process_db"):
        assert banned not in analysis


def test_discover_falls_back_on_api_failure(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())

    def _boom(cfg, region, max_models=None):
        raise Exception("AccessDenied")

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _boom)
    body = DiscoverRequest(system_id="", connection_type="SiteWise (Direct)")
    result = asyncio.run(onboard.onboard_discover(body))
    # Static fallback tool-table list is returned; branch still identifies as SiteWise.
    assert result["connection_type"] == "SiteWise (Direct)"
    names = {t["name"] for t in result["tables"]}
    assert "sitewise_get_asset_property_history" in names


def test_discover_threads_connection_config_region(monkeypatch):
    """The SiteWise (Direct) discover branch must honor an explicit
    connection_config.region (from the connect registration / modal) rather than
    only the ambient AWS_REGION."""
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["region"] = region
        return [dict(m) for m in _SYNTH_MODELS]

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    body = DiscoverRequest(system_id="", connection_type="SiteWise (Direct)",
                           connection_config={"region": "ap-southeast-2"})
    asyncio.run(onboard.onboard_discover(body))
    assert seen["region"] == "ap-southeast-2"


def test_discover_region_defaults_to_env_when_config_absent(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["region"] = region
        return [dict(m) for m in _SYNTH_MODELS]

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    body = DiscoverRequest(system_id="", connection_type="SiteWise (Direct)")
    asyncio.run(onboard.onboard_discover(body))
    assert seen["region"] == "ca-central-1"


# ---------------------------------------------------------------------------
# Cross-account (assumed-role) discovery — the SiteWise (Direct) discover branch
# must thread connection_config.role_arn/external_id into the iotsitewise client
# (via aws_session.boto3_client_for inside sitewise_read) EXACTLY like Athena, so
# a SiteWise source in ANOTHER account can be DISCOVERED, not just queried. All
# synthetic — no AWS, no network, no dataset literals.
# ---------------------------------------------------------------------------

def test_discover_threads_cross_account_role_from_body(monkeypatch):
    """When the connect form / body supplies role_arn + external_id, discovery
    passes BOTH verbatim into sitewise_read (which builds the iotsitewise client via
    aws_session.boto3_client_for → STS assume-role). A complete config is used
    as-is — no node/secret re-resolution."""
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    # A complete cross-account config must NOT trigger any node resolution.
    def _boom(_sid):  # pragma: no cover - must not be called
        raise AssertionError("resolve_connection must not be called for a complete config")
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", _boom)
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["cfg"] = dict(cfg)
        return [dict(m) for m in _SYNTH_MODELS]

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    body = DiscoverRequest(
        system_id="", connection_type="SiteWise (Direct)",
        connection_config={
            "region": "us-west-2",
            "role_arn": "arn:aws:iam::444455556666:role/CrossAcctReadOnly",
            "external_id": "ext-secret-xyz",
        })
    result = asyncio.run(onboard.onboard_discover(body))
    assert result["connection_type"] == "SiteWise (Direct)"
    # The cross-account credentials reached the sitewise_read client config, so the
    # boto3 iotsitewise client STS-AssumeRoles into the SOURCE account rather than
    # falling back to ambient deploy-account credentials.
    assert seen["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/CrossAcctReadOnly"
    assert seen["cfg"]["external_id"] == "ext-secret-xyz"


def test_discover_merges_external_id_from_node_on_reonboard(monkeypatch):
    """On re-onboard/restore the body/catalog snapshot carries only the NON-SECRET
    role_arn (external_id lives in Secrets Manager, not on the node). Discovery must
    merge the SECRET external_id (and region) from the resolved node the SAME way
    Athena does — via _with_cross_account_credentials — so the iotsitewise client
    still assumes the cross-account role instead of ambient deploy-account creds."""
    # A registered system must exist so discover passes its system_id guard.
    class _CatWithEntry:
        def get(self, system_id):
            if system_id == "sys-sw":
                return {"id": "sys-sw", "connection_type": "SiteWise (Direct)"}
            return None

        def set_connection_config(self, system_id, connection_config):
            # discover persists the SANITIZED non-secret subset onto the entry —
            # accepted here; this test asserts the credential merge, not storage.
            return True
    monkeypatch.setattr(onboard, "catalog", _CatWithEntry())
    resolved = {
        "system_id": "sys-sw", "connection_type": "SiteWise (Direct)",
        "role_arn": "arn:aws:iam::444455556666:role/CrossAcctReadOnly",
        "external_id": "ext-secret-from-node", "region": "us-west-2",
    }
    monkeypatch.setattr("agents.connection_resolver.resolve_connection",
                        lambda _sid: resolved)
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["cfg"] = dict(cfg)
        seen["region"] = region
        return [dict(m) for m in _SYNTH_MODELS]

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    # Body carries ONLY the non-secret role_arn (external_id was stripped to a secret).
    body = DiscoverRequest(
        system_id="sys-sw", connection_type="SiteWise (Direct)",
        connection_config={"role_arn": "arn:aws:iam::444455556666:role/CrossAcctReadOnly"})
    asyncio.run(onboard.onboard_discover(body))
    # external_id (secret) + region were merged from the resolved node.
    assert seen["cfg"]["external_id"] == "ext-secret-from-node"
    assert seen["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/CrossAcctReadOnly"
    # A persisted region merged from the node also flows through to the client.
    assert seen["region"] == "us-west-2"


def test_discover_blank_system_id_no_xacct_leaves_config_ambient(monkeypatch):
    """Pre-registration Mode-B discovery (blank system_id, no cross-account keys)
    must NOT resolve a node and must leave the config credential-free (ambient
    deploy-account credentials) — unchanged behavior."""
    monkeypatch.setattr(onboard, "catalog", _FakeCatalog())
    def _boom(_sid):  # pragma: no cover - must not be called for blank system_id
        raise AssertionError("resolve_connection must not be called for blank system_id")
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", _boom)
    seen = {}

    def _capture(cfg, region, max_models=None):
        seen["cfg"] = dict(cfg)
        return [dict(m) for m in _SYNTH_MODELS]

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    body = DiscoverRequest(system_id="", connection_type="SiteWise (Direct)")
    asyncio.run(onboard.onboard_discover(body))
    assert not seen["cfg"].get("role_arn")
    assert not seen["cfg"].get("external_id")
