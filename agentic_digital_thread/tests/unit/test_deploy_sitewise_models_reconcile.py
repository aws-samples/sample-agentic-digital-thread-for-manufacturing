# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Existing SiteWise models gain the properties they lack
(``highbyte/DeploySitewiseModels.py`` ``reconcile_model``).

Rehearsal defect (2026-10-02): an account that kept its SiteWise models
after a stack delete had a June ``CNC`` model without the vibration-analysis
channels (``Spectrum_*``, ``BPFO_*``, ``CrestFactor``, ``Kurtosis``). The
asset-tree build found the model by name and reused it unchanged, and SFC
dropped every one of those channels ("property ... not found"), so the
CMX-441 bearing story never reached SiteWise. Find-by-name now ADDS missing
properties and hierarchies; it never removes or alters existing ones.

Fully offline: the deploy script is loaded by path; the client is a fake.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODELS_PATH = _REPO_ROOT / "highbyte" / "DeploySitewiseModels.py"


@pytest.fixture(scope="module")
def models():
    spec = importlib.util.spec_from_file_location(
        "deploy_sitewise_models_reconcile_under_test", str(_MODELS_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Client:
    """In-memory SiteWise: one existing model, records every update."""

    def __init__(self, props, hiers=(), composites=None):
        self.model = {
            "assetModelId": "m-1", "assetModelName": "CNC",
            "assetModelDescription": "old",
            "assetModelProperties": [dict(p) for p in props],
            "assetModelHierarchies": [dict(h) for h in hiers],
            "assetModelStatus": {"state": "ACTIVE"},
        }
        if composites:
            self.model["assetModelCompositeModels"] = composites
        self.updates = []
        self.created = []

    def get_paginator(self, _op):
        model = self.model

        class _P:
            def paginate(self):
                return [{"assetModelSummaries": [
                    {"name": model["assetModelName"], "id": model["assetModelId"]}]}]
        return _P()

    def describe_asset_model(self, assetModelId):
        return self.model

    def update_asset_model(self, **kwargs):
        self.updates.append(kwargs)

    def create_asset_model(self, **kwargs):
        self.created.append(kwargs)
        return {"assetModelId": "new"}


_OLD = [
    {"id": "p1", "name": "Vibration_X", "dataType": "DOUBLE", "unit": "mm/s",
     "type": {"measurement": {}}, "path": [{"id": "p1", "name": "Vibration_X"}]},
    {"id": "p2", "name": "CoolantTemp", "dataType": "DOUBLE",
     "type": {"measurement": {}}},
]


def test_missing_properties_are_added_and_existing_ones_sent_back(models):
    client = _Client(_OLD)
    declared = [models.meas("Vibration_X", "DOUBLE", unit="mm/s"),
                models.meas("Spectrum_BPFO", "DOUBLE", unit="mm/s"),
                models.meas("BPFO_Alarm", "BOOLEAN")]
    added = models.reconcile_model(client, "m-1", "CNC", properties=declared)
    assert added == ["Spectrum_BPFO", "BPFO_Alarm"]
    (update,) = client.updates
    sent = update["assetModelProperties"]
    assert [p["name"] for p in sent] == [
        "Vibration_X", "CoolantTemp", "Spectrum_BPFO", "BPFO_Alarm"]
    # Existing properties keep their ids; read-only keys (path) are stripped.
    assert sent[0] == {"id": "p1", "name": "Vibration_X", "dataType": "DOUBLE",
                       "unit": "mm/s", "type": {"measurement": {}}}
    assert "id" not in sent[2]
    assert update["assetModelName"] == "CNC"
    assert update["assetModelDescription"] == "old"


def test_a_complete_model_is_not_updated(models):
    client = _Client(_OLD)
    declared = [models.meas("Vibration_X", "DOUBLE"), models.meas("CoolantTemp", "DOUBLE")]
    assert models.reconcile_model(client, "m-1", "CNC", properties=declared) == []
    assert client.updates == []


def test_extra_existing_properties_are_never_removed(models):
    client = _Client(_OLD)
    declared = [models.meas("Spectrum_1X", "DOUBLE")]
    models.reconcile_model(client, "m-1", "CNC", properties=declared)
    names = [p["name"] for p in client.updates[0]["assetModelProperties"]]
    assert {"Vibration_X", "CoolantTemp"} <= set(names)


def test_missing_hierarchies_are_added(models):
    client = _Client([], hiers=[{"id": "h1", "name": "Processes_CNC",
                                 "childAssetModelId": "c1"}])
    declared = [{"name": "Processes_CNC", "childAssetModelId": "c1"},
                {"name": "Processes_Oven", "childAssetModelId": "c2"}]
    assert models.reconcile_model(client, "m-1", "Line", hierarchies=declared) == [
        "Processes_Oven"]
    sent = client.updates[0]["assetModelHierarchies"]
    assert sent == [{"id": "h1", "name": "Processes_CNC", "childAssetModelId": "c1"},
                    {"name": "Processes_Oven", "childAssetModelId": "c2"}]


def test_a_model_with_composite_models_is_left_alone(models):
    client = _Client(_OLD, composites=[{"name": "alarm", "type": "AWS/ALARM"}])
    declared = [models.meas("Spectrum_1X", "DOUBLE")]
    assert models.reconcile_model(client, "m-1", "CNC", properties=declared) == []
    assert client.updates == []


def test_find_by_name_reconciles_instead_of_returning_the_stale_model(models):
    client = _Client(_OLD)
    model_id = models.create_or_get_model(
        client, "CNC", properties=models.PROCESS_MEASUREMENTS["CNC"])
    assert model_id == "m-1"
    assert client.created == []
    added = {p["name"] for p in client.updates[0]["assetModelProperties"]
             if "id" not in p}
    assert {"Spectrum_BPFO", "BPFO_Alarm", "CrestFactor", "Kurtosis"} <= added


def test_find_by_name_without_reconcile_never_updates(models):
    """The workshop seed's role has no UpdateAssetModel: reconcile=False must
    return the found model without describing or updating it."""
    client = _Client(_OLD)
    model_id = models.create_or_get_model(
        client, "CNC", properties=models.PROCESS_MEASUREMENTS["CNC"],
        reconcile=False)
    assert model_id == "m-1"
    assert client.updates == [] and client.created == []


def test_a_declared_unit_is_backfilled_onto_an_existing_property(models):
    """A model created before units were declared kept unit-less vibration
    channels, and answers guessed g or mm/s. Reconcile sends the property
    back with its id and the declared unit."""
    client = _Client(_OLD)
    declared = [models.meas("Vibration_X", "DOUBLE", unit="mm/s"),
                models.meas("CoolantTemp", "DOUBLE", unit="degC")]
    assert models.reconcile_model(client, "m-1", "CNC", properties=declared) == ["CoolantTemp"]
    (update,) = client.updates
    sent = {p["name"]: p for p in update["assetModelProperties"]}
    assert sent["CoolantTemp"] == {"id": "p2", "name": "CoolantTemp", "dataType": "DOUBLE",
                                   "unit": "degC", "type": {"measurement": {}}}
    assert sent["Vibration_X"]["unit"] == "mm/s"
    assert len(sent) == 2
