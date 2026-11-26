# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 1 (Task 1.2) — in-process per-system schema cache keyed by schema_version.

Pure/offline. ``neptune_query`` is mocked for the version read; no DB is touched.
"""
from unittest.mock import patch

import pytest

import agents.schema_cache as sc


@pytest.fixture(autouse=True)
def _reset_cache():
    sc.reset()
    yield
    sc.reset()


def test_same_version_loads_once():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"v": calls["n"]}

    b1 = sc.get_or_load("sys-x", "v1", loader)
    b2 = sc.get_or_load("sys-x", "v1", loader)
    assert calls["n"] == 1
    assert b1 is b2


def test_bumped_version_reloads_and_evicts_prior():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"gen": calls["n"]}

    sc.get_or_load("sys-x", "v1", loader)
    b2 = sc.get_or_load("sys-x", "v2", loader)  # bumped version -> reload
    assert calls["n"] == 2
    assert b2 == {"gen": 2}
    # Only ONE entry per system id (old version evicted, not accumulated).
    assert len(sc._cache) == 1
    assert sc._cache["sys-x"][0] == "v2"


def test_invalidate_forces_reload():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"gen": calls["n"]}

    sc.get_or_load("sys-x", "v1", loader)
    sc.invalidate("sys-x")
    sc.get_or_load("sys-x", "v1", loader)
    assert calls["n"] == 2


def test_reset_clears_all():
    sc.get_or_load("a", "v1", lambda: 1)
    sc.get_or_load("b", "v1", lambda: 2)
    sc.reset()
    assert sc._cache == {}


def test_cheap_schema_version_returns_value():
    with patch("agents.db.neptune_query", return_value=[{"v": "1720000000000"}]):
        assert sc.cheap_schema_version("sys-x") == "1720000000000"


def test_cheap_schema_version_empty_sentinel_on_missing():
    with patch("agents.db.neptune_query", return_value=[]):
        assert sc.cheap_schema_version("sys-x") == ""
    with patch("agents.db.neptune_query", return_value=[{"v": None}]):
        assert sc.cheap_schema_version("sys-x") == ""


def test_cheap_schema_version_sentinel_on_exception():
    with patch("agents.db.neptune_query", side_effect=RuntimeError("neptune down")):
        assert sc.cheap_schema_version("sys-x") == ""
