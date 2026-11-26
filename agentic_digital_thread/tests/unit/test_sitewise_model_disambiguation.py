# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for SiteWise ambiguous-asset-model-name disambiguation.

Real customer estates carry DUPLICATE-NAMED asset models (hierarchy shells left
by upstream restructuring) that shadow the propertied model under a blind
first-name match. LIVE-CONFIRMED failure: onboarding discovered 'Machine' WITH
property ``act_mech_speed``, but the scalar resolver first-matched a
property-less 'Machine' shell and failed with
"no SiteWise property named 'act_mech_speed' on asset model 'Machine'".

The fix (``metric_resolver._select_sitewise_model``) disambiguates fail-loud:

1. a same-named model carrying every required property (preserving
   ``_match_by_name``'s case-insensitive exact-name, first-match semantics);
2. else the UNIQUE estate-wide model carrying the required property/ies, with a
   ``logger.warning`` naming the substitution (honest telemetry, not silent);
3. else an IMPROVED error listing candidate models and property counts.

Covers BOTH the scalar path (``metric_resolver._execute_sitewise``) and the
grouped path (``metric_dimension._execute_sitewise_grouped`` — which requires
measure AND dimension properties co-located when selecting). Fully offline —
catalog, neptune_query and the sitewise_read seam are stubbed (NO AWS/network),
mirroring ``test_metric_resolver_athena_sitewise.py``.
"""
from __future__ import annotations

import logging

import pytest

import agents.connection_types as ct
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric
from agents.metric_resolver import (
    AuthorityResolver,
    MetricResolverError,
    resolve_metric,
    resolve_metric_by_dimension,
)


# --- Test doubles (mirroring test_metric_resolver_athena_sitewise.py) --------

class _FakeAuthority(AuthorityResolver):
    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:
        return self._system_id


@pytest.fixture
def seed_catalog():
    """Seed the shared catalog with a SiteWise (Direct) system, then restore."""
    from agents.catalog import catalog

    saved = catalog._systems
    catalog._systems = [
        {
            "id": "sitewise-sys", "name": "SiteWise Direct",
            "connection_type": ct.SITEWISE_DIRECT, "schema": "",
            "status": "onboarded",
            "connection_config": {"region": "us-east-1"},
        },
    ]
    try:
        yield catalog
    finally:
        catalog._systems = saved


def _maps_to(table: str, column: str):
    """Canned neptune_query MAPS_TO row for the scalar path (event-time → none)."""
    def nq(cypher, params=None):
        from agents.metric_dimension import TIME_CONCEPT
        if (params or {}).get("concept") == TIME_CONCEPT:
            return []
        return [{"table": table, "column": column}]
    return nq


def _md_neptune_query(mapping_by_concept):
    """neptune_query double for the grouped path, keyed by params['concept']."""
    def nq(cypher, params=None):
        if "se.name AS table" in cypher:
            row = mapping_by_concept.get((params or {}).get("concept"))
            return [row] if row else []
        return []
    return nq


def _stub_sitewise(monkeypatch, *, models, values):
    """Stub the sitewise_read seam; ``values`` is keyed ``(asset_id, prop_id)``.

    ``list_assets`` records the model id it was called with (returned list) so
    tests can assert WHICH model the resolver selected."""
    calls = {"asset_model_ids": []}

    def fake_list_assets(connection_config, region=None, asset_model_id="", **k):
        calls["asset_model_ids"].append(asset_model_id)
        return [{"id": "a1"}, {"id": "a2"}]

    monkeypatch.setattr(
        mr.sitewise_read, "list_asset_models_with_properties",
        lambda connection_config, region=None, **k: models,
    )
    monkeypatch.setattr(mr.sitewise_read, "list_assets", fake_list_assets)
    monkeypatch.setattr(
        mr.sitewise_read, "batch_get_property_values",
        lambda connection_config, region, entries: {
            f"{e['assetId']}_{e['propertyId']}":
                values[(e["assetId"], e["propertyId"])]
            for e in entries
        },
    )
    return calls


def _speed_metric():
    return DirectMetric(
        id="m-speed", concept="mech-speed",
        aggregation=Aggregation.AVG, label="Mech Speed", unit="rpm",
    )


_SPEED_VALUES = {
    ("a1", "prop-speed"): {"value": 10, "timestamp": 100},
    ("a2", "prop-speed"): {"value": 30, "timestamp": 110},
}

# The live shape: a property-less 'Machine' hierarchy shell enumerated BEFORE
# the propertied 'Machine' model.
_SHELL_FIRST_MODELS = [
    {"id": "model-shell", "name": "Machine", "properties": []},
    {"id": "model-real", "name": "Machine",
     "properties": [{"id": "prop-speed", "name": "act_mech_speed"}]},
]


# --- (A) scalar path (metric_resolver._execute_sitewise) ---------------------

def test_scalar_duplicate_name_shell_first_picks_propertied_model(monkeypatch, seed_catalog):
    """Two 'Machine' models — shell (0 props) enumerated first, propertied second
    → the resolver selects the propertied one (the live failure, fixed)."""
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Machine", "act_mech_speed"))
    calls = _stub_sitewise(monkeypatch, models=_SHELL_FIRST_MODELS, values=_SPEED_VALUES)

    resolved = resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))

    assert resolved.value == 20  # (10 + 30) / 2 — real values, no fabrication
    assert calls["asset_model_ids"] == ["model-real"]


def test_scalar_duplicate_name_case_insensitive_step1(monkeypatch, seed_catalog):
    """Step (1) preserves _match_by_name semantics: the mapped name matches the
    duplicate-named models case-insensitively."""
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("machine", "ACT_MECH_SPEED"))
    calls = _stub_sitewise(monkeypatch, models=_SHELL_FIRST_MODELS, values=_SPEED_VALUES)

    resolved = resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))

    assert resolved.value == 20
    assert calls["asset_model_ids"] == ["model-real"]


def test_scalar_property_only_elsewhere_unique_model_substituted_with_warning(
    monkeypatch, seed_catalog, caplog
):
    """The mapped name matches only a shell; exactly ONE model in the estate
    carries the property → it is used, and the substitution is logged loud."""
    models = [
        {"id": "model-shell", "name": "Machine", "properties": []},
        {"id": "model-renamed", "name": "Machine_v2",
         "properties": [{"id": "prop-speed", "name": "act_mech_speed"}]},
    ]
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Machine", "act_mech_speed"))
    calls = _stub_sitewise(monkeypatch, models=models, values=_SPEED_VALUES)

    with caplog.at_level(logging.WARNING, logger="agents.metric_resolver"):
        resolved = resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))

    assert resolved.value == 20
    assert calls["asset_model_ids"] == ["model-renamed"]
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "disambiguation" in warning
    assert "'Machine'" in warning and "'Machine_v2'" in warning
    assert "model-renamed" in warning


def test_scalar_ambiguous_two_propertied_models_fails_with_candidates(
    monkeypatch, seed_catalog
):
    """Two models carry the property and neither name match does → fail loud with
    the candidate landscape (names, ids, property counts) — never a guess."""
    models = [
        {"id": "model-shell", "name": "Machine", "properties": []},
        {"id": "model-x", "name": "Machine_old",
         "properties": [{"id": "prop-speed", "name": "act_mech_speed"}]},
        {"id": "model-y", "name": "Machine_new",
         "properties": [{"id": "prop-speed", "name": "act_mech_speed"},
                        {"id": "prop-temp", "name": "temp"}]},
    ]
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Machine", "act_mech_speed"))
    _stub_sitewise(monkeypatch, models=models, values=_SPEED_VALUES)

    with pytest.raises(MetricResolverError) as exc:
        resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))

    msg = str(exc.value)
    assert "no SiteWise property named" in msg
    assert "'Machine' (id=model-shell, 0 properties)" in msg
    assert "'Machine_old' (id=model-x, 1 properties)" in msg
    assert "'Machine_new' (id=model-y, 2 properties)" in msg


def test_scalar_property_nowhere_fails_with_candidates(monkeypatch, seed_catalog):
    """Name-matched models exist but NO model in the estate carries the property
    → improved error lists the name-matched candidates and their property counts."""
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Machine", "no_such_prop"))
    _stub_sitewise(monkeypatch, models=_SHELL_FIRST_MODELS, values=_SPEED_VALUES)

    with pytest.raises(MetricResolverError) as exc:
        resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))

    msg = str(exc.value)
    assert "no SiteWise property named 'no_such_prop'" in msg
    assert "'Machine' (id=model-shell, 0 properties)" in msg
    assert "'Machine' (id=model-real, 1 properties)" in msg


def test_scalar_unresolved_model_name_still_fails_loud(monkeypatch, seed_catalog):
    """A mapped name matching NO model keeps the existing fail-loud error —
    a stale mapping is not a disambiguation problem."""
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([_speed_metric()]))
    monkeypatch.setattr(mr, "neptune_query", _maps_to("Compressor", "act_mech_speed"))
    _stub_sitewise(monkeypatch, models=_SHELL_FIRST_MODELS, values=_SPEED_VALUES)

    with pytest.raises(MetricResolverError, match="no SiteWise asset model named"):
        resolve_metric("m-speed", authority=_FakeAuthority("sitewise-sys"))


# --- (B) grouped path (metric_dimension._execute_sitewise_grouped) -----------

_GROUP_VALUES = {
    ("a1", "prop-state"): {"value": "RUNNING", "timestamp": 100},
    ("a1", "prop-amt"): {"value": 5, "timestamp": 100},
    ("a2", "prop-state"): {"value": "IDLE", "timestamp": 110},
    ("a2", "prop-amt"): {"value": 2, "timestamp": 110},
}


def _grouped_setup(monkeypatch, models):
    metric = DirectMetric(
        id="m-amount", concept="throughput",
        aggregation=Aggregation.SUM, label="Output", unit="units",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query({
        "throughput": {"table": "State_Event", "column": "amount_per_5s"},
        "machine-state": {"table": "State_Event", "column": "payload_toState"},
    }))
    return _stub_sitewise(monkeypatch, models=models, values=_GROUP_VALUES)


def test_grouped_duplicate_name_shell_first_picks_propertied_model(monkeypatch, seed_catalog):
    """Grouped path: the shell shadows a same-named model carrying BOTH the
    measure and the dimension property → the propertied model is selected."""
    models = [
        {"id": "model-shell", "name": "State_Event", "properties": []},
        {"id": "model-real", "name": "State_Event",
         "properties": [{"id": "prop-state", "name": "payload_toState"},
                        {"id": "prop-amt", "name": "amount_per_5s"}]},
    ]
    calls = _grouped_setup(monkeypatch, models)

    result = resolve_metric_by_dimension(
        "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
    )

    assert [(r.dimension_value, r.value) for r in result.rows] == [
        ("IDLE", 2), ("RUNNING", 5),
    ]
    assert calls["asset_model_ids"] == ["model-real"]


def test_grouped_selection_requires_both_measure_and_dimension_props(
    monkeypatch, seed_catalog, caplog
):
    """A same-named model with ONLY the dimension property does NOT satisfy the
    grouped contract; the unique estate model carrying BOTH properties is
    substituted with a warning."""
    models = [
        {"id": "model-partial", "name": "State_Event",
         "properties": [{"id": "prop-state", "name": "payload_toState"}]},
        {"id": "model-both", "name": "State_Event_v2",
         "properties": [{"id": "prop-state", "name": "payload_toState"},
                        {"id": "prop-amt", "name": "amount_per_5s"}]},
    ]
    calls = _grouped_setup(monkeypatch, models)

    with caplog.at_level(logging.WARNING, logger="agents.metric_resolver"):
        result = resolve_metric_by_dimension(
            "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
        )

    assert [(r.dimension_value, r.value) for r in result.rows] == [
        ("IDLE", 2), ("RUNNING", 5),
    ]
    assert calls["asset_model_ids"] == ["model-both"]
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "disambiguation" in warning
    assert "'payload_toState'" in warning and "'amount_per_5s'" in warning


def test_grouped_ambiguous_fails_with_candidates(monkeypatch, seed_catalog):
    """Two estate models carry both properties, neither name match does → fail
    loud with the candidate landscape."""
    models = [
        {"id": "model-shell", "name": "State_Event", "properties": []},
        {"id": "model-x", "name": "State_Event_old",
         "properties": [{"id": "prop-state", "name": "payload_toState"},
                        {"id": "prop-amt", "name": "amount_per_5s"}]},
        {"id": "model-y", "name": "State_Event_new",
         "properties": [{"id": "prop-state", "name": "payload_toState"},
                        {"id": "prop-amt", "name": "amount_per_5s"}]},
    ]
    _grouped_setup(monkeypatch, models)

    with pytest.raises(MetricResolverError) as exc:
        resolve_metric_by_dimension(
            "m-amount", "machine-state", authority=_FakeAuthority("sitewise-sys"),
        )

    msg = str(exc.value)
    assert "no SiteWise property named" in msg
    assert "'State_Event' (id=model-shell, 0 properties)" in msg
    assert "'State_Event_old' (id=model-x, 2 properties)" in msg
    assert "'State_Event_new' (id=model-y, 2 properties)" in msg


def test_grouped_same_property_measure_and_dimension_dedupes_requirement(
    monkeypatch, seed_catalog
):
    """COUNT-distribution shape (measure == dimension property): the duplicated
    requirement dedupes and the propertied duplicate-named model is selected."""
    metric = DirectMetric(
        id="m-state-dist", concept="machine-state",
        aggregation=Aggregation.COUNT, label="Machines by State", unit="machines",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: {"region": "us-east-1"})
    monkeypatch.setattr(mr, "neptune_query", _md_neptune_query(
        {"machine-state": {"table": "State_Event", "column": "payload_toState"}}
    ))
    models = [
        {"id": "model-shell", "name": "State_Event", "properties": []},
        {"id": "model-real", "name": "State_Event",
         "properties": [{"id": "prop-state", "name": "payload_toState"}]},
    ]
    calls = _stub_sitewise(monkeypatch, models=models, values={
        ("a1", "prop-state"): {"value": "RUNNING", "timestamp": 100},
        ("a2", "prop-state"): {"value": "IDLE", "timestamp": 110},
    })

    result = resolve_metric_by_dimension(
        "m-state-dist", "machine-state", authority=_FakeAuthority("sitewise-sys"),
    )

    assert [(r.dimension_value, r.value) for r in result.rows] == [
        ("IDLE", 1), ("RUNNING", 1),
    ]
    assert calls["asset_model_ids"] == ["model-real"]
