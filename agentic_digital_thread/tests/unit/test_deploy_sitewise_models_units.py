# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Measurement units on the SiteWise models (``highbyte/DeploySitewiseModels.py``).

Rehearsal defect: ``meas()`` never set ``unit``, so every SiteWise property
reached discovery and the model with no unit and the answer had to guess
what a vibration or temperature number meant. These tests pin the units on
the story channels and that a unit-less measurement stays unit-less (no
empty-string ``unit`` — SiteWise rejects a zero-length ``PropertyUnit``).

Fully offline: the deploy script is loaded by path; no client is built.
"""

import importlib.util
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODELS_PATH = _REPO_ROOT / "highbyte" / "DeploySitewiseModels.py"
# botocore iotsitewise PropertyUnit: min 1, max 256, no control characters.
_PROPERTY_UNIT = re.compile(r"[^\u0000-\u001F\u007F]{1,256}")


def _load_models():
    spec = importlib.util.spec_from_file_location("deploy_sitewise_models_under_test",
                                                  str(_MODELS_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def models():
    return _load_models()


def _cnc(models) -> dict:
    return {p["name"]: p for p in models.PROCESS_MEASUREMENTS["CNC"]}


def test_meas_emits_unit_only_when_given(models):
    assert models.meas("A", "DOUBLE") == {
        "name": "A", "dataType": "DOUBLE", "type": {"measurement": {}}}
    assert models.meas("A", "DOUBLE", unit="mm/s") == {
        "name": "A", "dataType": "DOUBLE", "unit": "mm/s", "type": {"measurement": {}}}
    assert "unit" not in models.meas("A", "DOUBLE", unit="")


@pytest.mark.parametrize("name,unit", [
    ("Vibration_X", "mm/s"), ("Vibration_Y", "mm/s"), ("Vibration_Z", "mm/s"),
    ("Spectrum_1X", "mm/s"), ("Spectrum_BPFO", "mm/s"), ("Spectrum_BPFI", "mm/s"),
    ("BPFO_Hz", "Hz"), ("BPFI_Hz", "Hz"),
    ("CoolantTemp", "°C"),
])
def test_cnc_story_channels_carry_units(models, name, unit):
    assert _cnc(models)[name]["unit"] == unit


@pytest.mark.parametrize("name", ["CrestFactor", "Kurtosis", "BPFO_Alarm", "Status"])
def test_dimensionless_cnc_channels_have_no_unit(models, name):
    assert "unit" not in _cnc(models)[name]


def test_every_declared_unit_satisfies_the_sitewise_property_unit_shape(models):
    declared = [p["unit"] for props in models.PROCESS_MEASUREMENTS.values()
                for p in props if "unit" in p]
    assert declared, "the story channels declare units"
    for unit in declared:
        assert _PROPERTY_UNIT.fullmatch(unit), unit


def test_feed_properties_all_have_units(models):
    """The degradation feed writes these four; each must be unit-labelled."""
    feed_path = _REPO_ROOT / "lambda-sitewise-feed" / "handler.py"
    spec = importlib.util.spec_from_file_location("sitewise_feed_units_under_test", str(feed_path))
    feed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feed)
    cnc = _cnc(models)
    for name in [*feed.VIBRATION_PROPERTIES, feed.TEMP_PROPERTY]:
        assert cnc[name].get("unit"), name
