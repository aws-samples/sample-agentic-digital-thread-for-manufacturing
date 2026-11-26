# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit coverage for the audit script's i3X property-layer checks.

Drives ``tests/audit/audit_onboarding_graph.py``'s fact-family (d) functions
against a monkeypatched ``neptune_query`` (no graph, no AWS): ownership
(every :Property has an owning :Asset via HAS_PROPERTY), per-asset
``property_count`` vs HAS_PROPERTY out-degree, the graph-wide orphaned-
Property invariant, and the run_audit wiring (skip when no i3X-style system
is onboarded; fold family (d) into the hard pass otherwise).

The cypher constants asserted here are the SOURCE OF TRUTH copied verbatim
into docs/graph-shape-contract.md — a drift in either place should fail here.
"""

from __future__ import annotations

from tests.audit import audit_onboarding_graph as mod


def _route(responses: dict):
    """A fake ``neptune_query`` routing on the module's cypher constants."""
    def fake(cypher, params=None):
        for key, value in responses.items():
            if cypher == key:
                return value(params) if callable(value) else value
        return []
    return fake


# ---------------------------------------------------------------------------
# pick_i3x_target_system
# ---------------------------------------------------------------------------

def test_pick_i3x_target_none_when_no_property_systems(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({}))
    assert mod.pick_i3x_target_system() is None


def test_pick_i3x_target_returns_deterministic_first(monkeypatch):
    rows = [{"s.id": "zeta"}, {"s.id": "alpha"}]
    monkeypatch.setattr(
        mod, "neptune_query",
        _route({mod.CYPHER_SYSTEMS_WITH_PROPERTIES: rows}),
    )
    assert mod.pick_i3x_target_system() == "alpha"


# ---------------------------------------------------------------------------
# (d1) ownership
# ---------------------------------------------------------------------------

def test_property_ownership_passes_when_all_owned(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_PROPERTY_TOTAL: [{"n": 5}],
        mod.CYPHER_PROPERTY_WITHOUT_OWNER: [{"n": 0}],
    }))
    report = mod.assert_property_ownership("sysa")
    assert report["pass"] is True
    assert report["property_count"] == 5
    assert report["unowned_count"] == 0


def test_property_ownership_fails_on_unowned(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_PROPERTY_TOTAL: [{"n": 5}],
        mod.CYPHER_PROPERTY_WITHOUT_OWNER: [{"n": 2}],
    }))
    report = mod.assert_property_ownership("sysa")
    assert report["pass"] is False
    assert report["unowned_count"] == 2


def test_property_ownership_fails_on_zero_properties(monkeypatch):
    """The target is only picked because it owns Properties — zero means the
    layer vanished between pick and check; fail loud, not vacuous-pass."""
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_PROPERTY_TOTAL: [{"n": 0}],
        mod.CYPHER_PROPERTY_WITHOUT_OWNER: [{"n": 0}],
    }))
    assert mod.assert_property_ownership("sysa")["pass"] is False


def test_ownership_cypher_requires_asset_label():
    """The owner must be an :Asset specifically, not any HAS_PROPERTY source."""
    assert "<-[:HAS_PROPERTY]-(:Asset)" in mod.CYPHER_PROPERTY_WITHOUT_OWNER


# ---------------------------------------------------------------------------
# (d2) property_count vs HAS_PROPERTY out-degree
# ---------------------------------------------------------------------------

def _degree_rows(rows):
    return _route({mod.CYPHER_ASSET_PROPERTY_DEGREE: rows})


def test_count_degree_passes_when_all_match(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _degree_rows([
        {"source_id": "a1", "name": "n1", "property_count": 3, "degree": 3},
        # 0 = structural-only asset: stamped 0 with no HAS_PROPERTY edges is OK
        {"source_id": "a2", "name": "n2", "property_count": 0, "degree": 0},
    ]))
    report = mod.assert_property_count_degree("sysa")
    assert report["pass"] is True
    assert report["asset_count"] == 2
    assert report["mismatch_count"] == 0


def test_count_degree_detects_mismatch(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _degree_rows([
        {"source_id": "a1", "name": "n1", "property_count": 3, "degree": 2},
    ]))
    report = mod.assert_property_count_degree("sysa")
    assert report["pass"] is False
    assert report["mismatch_count"] == 1
    assert report["mismatches"][0]["property_count"] == 3
    assert report["mismatches"][0]["has_property_degree"] == 2


def test_count_degree_missing_stamp_is_mismatch(monkeypatch):
    """The walker stamps EVERY asset (0 = structural-only), so a missing
    stamp is a mismatch even at degree 0 — the orchestrator's
    property_count=0 guard reads the stamp, not the edges."""
    monkeypatch.setattr(mod, "neptune_query", _degree_rows([
        {"source_id": "a1", "name": "n1", "property_count": None, "degree": 0},
    ]))
    report = mod.assert_property_count_degree("sysa")
    assert report["pass"] is False
    assert report["assets_missing_stamp"] == 1


def test_count_degree_fails_on_no_assets(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _degree_rows([]))
    assert mod.assert_property_count_degree("sysa")["pass"] is False


# ---------------------------------------------------------------------------
# (d3) orphaned Property (graph-wide, after-reset invariant)
# ---------------------------------------------------------------------------

def test_no_orphans_passes_on_zero(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 0}],
    }))
    report = mod.assert_no_orphaned_properties()
    assert report["pass"] is True
    assert report["orphaned_property_count"] == 0


def test_orphans_fail(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 4}],
    }))
    report = mod.assert_no_orphaned_properties()
    assert report["pass"] is False
    assert report["orphaned_property_count"] == 4


# ---------------------------------------------------------------------------
# run_audit wiring
# ---------------------------------------------------------------------------

def test_audit_property_layer_skips_without_i3x_system(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({}))
    section = mod.audit_property_layer()
    assert section["status"] == "skipped"
    assert section["pass"] is True  # relational-only deployments stay green


def test_run_audit_includes_property_layer_and_orphan_check(monkeypatch, tmp_path):
    """A relational system audit stays green with the (d) section skipped,
    and the graph-wide orphan check is included in the report + hard pass."""
    monkeypatch.setattr(mod, "REPORT_PATH", str(tmp_path / "report.json"))
    monkeypatch.setattr(mod, "read_one_concept_value", lambda sid: None)
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_SYSTEMS_WITH_MAPPINGS: [{"s.id": "sysa"}],
        mod.CYPHER_FIELD_CONCEPT: [
            {"table": "t", "column": "c", "concept": "k", "role": "measure", "confidence": 0.9},
        ],
        mod.CYPHER_AUTHORITY: [
            {"ar.concept": "k", "ar.system_id": "sysa", "ar.rule": "r", "ar.priority": 1},
        ],
        mod.CYPHER_JOINS: [
            {"jp.from_system": "sysa", "jp.from_table": "t", "jp.from_field": "c",
             "jp.to_system": "sysa", "jp.to_table": "t2", "jp.to_field": "c2"},
        ],
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 0}],
    }))
    report = mod.run_audit()
    assert report["status"] == "pass"
    assert report["i3x_property_layer"]["status"] == "skipped"
    assert report["orphaned_properties"]["pass"] is True


def test_run_audit_fails_hard_on_orphaned_properties(monkeypatch, tmp_path):
    """Orphaned Properties flip the whole audit to fail even when the
    relational families pass — the after-reset invariant is a hard gate."""
    monkeypatch.setattr(mod, "REPORT_PATH", str(tmp_path / "report.json"))
    monkeypatch.setattr(mod, "read_one_concept_value", lambda sid: None)
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_SYSTEMS_WITH_MAPPINGS: [{"s.id": "sysa"}],
        mod.CYPHER_FIELD_CONCEPT: [
            {"table": "t", "column": "c", "concept": "k", "role": "measure", "confidence": 0.9},
        ],
        mod.CYPHER_AUTHORITY: [
            {"ar.concept": "k", "ar.system_id": "sysa", "ar.rule": "r", "ar.priority": 1},
        ],
        mod.CYPHER_JOINS: [
            {"jp.from_system": "sysa", "jp.from_table": "t", "jp.from_field": "c",
             "jp.to_system": "sysa", "jp.to_table": "t2", "jp.to_field": "c2"},
        ],
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 2}],
    }))
    report = mod.run_audit()
    assert report["status"] == "fail"
    assert report["orphaned_properties"]["orphaned_property_count"] == 2


def test_run_audit_checks_property_layer_for_i3x_system(monkeypatch, tmp_path):
    """With an i3X-style system present, family (d) is checked per-system and
    a per-asset count/degree mismatch fails the audit."""
    monkeypatch.setattr(mod, "REPORT_PATH", str(tmp_path / "report.json"))
    monkeypatch.setattr(mod, "read_one_concept_value", lambda sid: None)
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_SYSTEMS_WITH_MAPPINGS: [{"s.id": "sysa"}],
        mod.CYPHER_FIELD_CONCEPT: [
            {"table": "t", "column": "c", "concept": "k", "role": "measure", "confidence": 0.9},
        ],
        mod.CYPHER_AUTHORITY: [
            {"ar.concept": "k", "ar.system_id": "sysa", "ar.rule": "r", "ar.priority": 1},
        ],
        mod.CYPHER_JOINS: [
            {"jp.from_system": "sysa", "jp.from_table": "t", "jp.from_field": "c",
             "jp.to_system": "sysa", "jp.to_table": "t2", "jp.to_field": "c2"},
        ],
        mod.CYPHER_SYSTEMS_WITH_PROPERTIES: [{"s.id": "sysa"}],
        mod.CYPHER_PROPERTY_TOTAL: [{"n": 2}],
        mod.CYPHER_PROPERTY_WITHOUT_OWNER: [{"n": 0}],
        mod.CYPHER_ASSET_PROPERTY_DEGREE: [
            {"source_id": "a1", "name": "n1", "property_count": 2, "degree": 1},
        ],
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 0}],
    }))
    report = mod.run_audit()
    assert report["i3x_property_layer"]["status"] == "checked"
    assert report["i3x_property_layer"]["ownership"]["pass"] is True
    assert report["i3x_property_layer"]["count_degree"]["pass"] is False
    assert report["status"] == "fail"
