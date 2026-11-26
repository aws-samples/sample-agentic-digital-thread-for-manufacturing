# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit coverage for the audit script's equipment-bridge checks:
fact family (e) — the equipment ValueMapping / MAPS_TO_ASSET layer — and the
report-only metamodel reconciliation probe.

Drives ``tests/audit/audit_onboarding_graph.py`` against a monkeypatched
``neptune_query`` (no graph, no AWS), following the
``test_audit_i3x_property_layer.py`` pattern: link integrity (every equipment
VM with ``to_element_id`` has exactly one MAPS_TO_ASSET edge whose target
Asset ``source_id`` matches), the bare-VM writer guard, the skip(pass)
semantics for relational-only graphs, the run_audit wiring (family (e) folds
into the hard verdict; the metamodel probe never does), and the
INFO-not-FAIL drift reporting both directions.

The cypher constants asserted here are the SOURCE OF TRUTH copied verbatim
into docs/graph-shape-contract.md §4(e) — a drift in either place should fail
``test_graph_contract_metamodel_pin.py``.
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


def _vm_row(vm_id: str, teid: str, edge_count: int, targets: list) -> dict:
    return {"vm_id": vm_id, "to_element_id": teid,
            "edge_count": edge_count, "asset_source_ids": targets}


# ---------------------------------------------------------------------------
# (e1/e2) link integrity
# ---------------------------------------------------------------------------

def test_links_pass_when_exactly_one_edge_matching_source_id():
    rows = [
        _vm_row("vm-equip-a", "uuid-a", 1, ["uuid-a"]),
        _vm_row("vm-equip-b", "uuid-b", 1, ["uuid-b"]),
    ]
    report = mod.assert_equipment_vm_asset_links(rows)
    assert report["pass"] is True
    assert report["vm_count"] == 2
    assert report["mismatch_count"] == 0


def test_links_fail_on_missing_edge():
    rows = [_vm_row("vm-equip-a", "uuid-a", 0, [])]
    report = mod.assert_equipment_vm_asset_links(rows)
    assert report["pass"] is False
    assert report["mismatch_count"] == 1
    assert report["mismatches"][0]["vm_id"] == "vm-equip-a"


def test_links_fail_on_duplicate_edges():
    rows = [_vm_row("vm-equip-a", "uuid-a", 2, ["uuid-a", "uuid-a"])]
    report = mod.assert_equipment_vm_asset_links(rows)
    assert report["pass"] is False
    assert report["mismatch_count"] == 1


def test_links_fail_when_target_source_id_differs():
    """Exactly one edge but pointing at the WRONG Asset — must fail."""
    rows = [_vm_row("vm-equip-a", "uuid-a", 1, ["uuid-other"])]
    report = mod.assert_equipment_vm_asset_links(rows)
    assert report["pass"] is False
    assert report["mismatches"][0]["asset_source_ids"] == ["uuid-other"]


def test_links_vacuous_pass_on_zero_rows():
    """Skip semantics live in the wrapper — the assertion itself is vacuous."""
    report = mod.assert_equipment_vm_asset_links([])
    assert report["pass"] is True
    assert report["vm_count"] == 0


# ---------------------------------------------------------------------------
# (e3) bare-VM writer guard
# ---------------------------------------------------------------------------

def test_bare_vm_guard_passes_on_zero(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 0}],
    }))
    report = mod.assert_no_bare_vm_asset_edges()
    assert report["pass"] is True
    assert report["bare_vm_edge_count"] == 0


def test_bare_vm_guard_fails_on_edge_from_vm_missing_element_id(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 3}],
    }))
    report = mod.assert_no_bare_vm_asset_edges()
    assert report["pass"] is False
    assert report["bare_vm_edge_count"] == 3


# ---------------------------------------------------------------------------
# audit_equipment_vm_layer wrapper (skip / checked semantics)
# ---------------------------------------------------------------------------

def test_layer_skips_pass_when_no_vms_and_no_edges(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES: [],
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 0}],
    }))
    report = mod.audit_equipment_vm_layer()
    assert report["status"] == "skipped"
    assert report["pass"] is True


def test_layer_checked_and_fails_on_bare_edge_even_without_vm_rows(monkeypatch):
    """A MAPS_TO_ASSET edge from a bare VM must fail even when NO equipment VM
    carries to_element_id (a foreign writer violated the guard)."""
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES: [],
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 1}],
    }))
    report = mod.audit_equipment_vm_layer()
    assert report["status"] == "checked"
    assert report["pass"] is False


def test_layer_checked_pass_when_all_links_good(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES: [
            _vm_row("vm-equip-a", "uuid-a", 1, ["uuid-a"]),
        ],
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 0}],
    }))
    report = mod.audit_equipment_vm_layer()
    assert report["status"] == "checked"
    assert report["pass"] is True
    assert report["links"]["vm_count"] == 1


def test_layer_query_is_parameterized_on_concept(monkeypatch):
    """The equipment-VM query must be called with the concept as a PARAMETER
    (no literal interpolated into the cypher text)."""
    seen: list[tuple[str, dict | None]] = []

    def fake(cypher, params=None):
        seen.append((cypher, params))
        return [{"n": 0}] if cypher == mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM else []

    monkeypatch.setattr(mod, "neptune_query", fake)
    mod.audit_equipment_vm_layer()
    vm_calls = [(c, p) for c, p in seen if c == mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES]
    assert vm_calls and vm_calls[0][1] == {"concept": "equipment"}
    assert "$concept" in mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES
    assert "'equipment'" not in mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES


# ---------------------------------------------------------------------------
# probe_metamodel_edges — report-only, both drift directions
# ---------------------------------------------------------------------------

def test_probe_reports_documented_but_absent_as_info(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_META_EDGE_NAMES: [{"name": "HAS_SCHEMA"}, {"name": "MAPS_TO_ASSET"}],
        mod.CYPHER_LIVE_EDGE_TYPES: [{"rel": "HAS_SCHEMA"}],
    }))
    report = mod.probe_metamodel_edges()
    assert report["status"] == "checked"
    assert report["info_only"] is True
    assert "pass" not in report  # never contributes to the hard verdict
    assert report["documented_but_absent"] == ["MAPS_TO_ASSET"]
    assert report["live_but_undocumented"] == []


def test_probe_reports_live_but_undocumented(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_META_EDGE_NAMES: [{"name": "HAS_SCHEMA"}],
        mod.CYPHER_LIVE_EDGE_TYPES: [{"rel": "HAS_SCHEMA"}, {"rel": "SOME_NEW_EDGE"}],
    }))
    report = mod.probe_metamodel_edges()
    assert report["live_but_undocumented"] == ["SOME_NEW_EDGE"]
    assert report["documented_but_absent"] == []


def test_probe_flags_missing_metamodel(monkeypatch):
    monkeypatch.setattr(mod, "neptune_query", _route({
        mod.CYPHER_META_EDGE_NAMES: [],
        mod.CYPHER_LIVE_EDGE_TYPES: [{"rel": "HAS_SCHEMA"}],
    }))
    report = mod.probe_metamodel_edges()
    assert report["status"] == "no_metamodel"
    assert report["documented_count"] == 0


# ---------------------------------------------------------------------------
# run_audit wiring
# ---------------------------------------------------------------------------

def _minimal_green_graph(extra: dict | None = None):
    """Responses for a relational-only green audit (families d/e skip)."""
    responses = {
        mod.CYPHER_SYSTEMS_WITH_MAPPINGS: [{"s.id": "sysa"}],
        mod.CYPHER_FIELD_CONCEPT: [
            {"table": "t", "column": "c", "concept": "equipment-id",
             "role": "identifier", "confidence": 0.9},
        ],
        mod.CYPHER_AUTHORITY: [
            {"ar.concept": "equipment-id", "ar.system_id": "sysa",
             "ar.rule": "r", "ar.priority": 1},
        ],
        mod.CYPHER_JOINS: [
            {"jp.from_system": "sysa", "jp.from_table": "a", "jp.from_field": "x",
             "jp.to_system": "sysa", "jp.to_table": "b", "jp.to_field": "y"},
        ],
        mod.CYPHER_JOINS_FROM_SYSTEM_EDGE: [{"n": 1}],
        mod.CYPHER_JOINS_TO_SYSTEM_EDGE: [{"n": 1}],
        mod.CYPHER_SYSTEMS_WITH_PROPERTIES: [],
        mod.CYPHER_ORPHANED_PROPERTIES: [{"n": 0}],
        mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES: [],
        mod.CYPHER_MAPS_TO_ASSET_FROM_BARE_VM: [{"n": 0}],
        mod.CYPHER_META_EDGE_NAMES: [],
        mod.CYPHER_LIVE_EDGE_TYPES: [],
        mod.CYPHER_IDENTITY_RESOLVES: [{"n": 0}],
        mod.CYPHER_IDENTITY_LINKS_CONCEPT: [{"n": 0}],
    }
    responses.update(extra or {})
    return responses


def test_run_audit_folds_equipment_vm_layer_into_hard_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "REPORT_PATH", str(tmp_path / "report.json"))
    monkeypatch.setattr(mod, "read_one_concept_value", lambda sid: None)
    monkeypatch.setattr(mod, "neptune_query", _route(_minimal_green_graph({
        mod.CYPHER_EQUIPMENT_VM_ASSET_EDGES: [
            _vm_row("vm-equip-a", "uuid-a", 0, []),  # missing edge -> hard fail
        ],
    })))
    report = mod.run_audit("sysa")
    assert report["equipment_vm_layer"]["pass"] is False
    assert report["status"] == "fail"


def test_run_audit_green_with_skipped_families_and_probe_sections(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "REPORT_PATH", str(tmp_path / "report.json"))
    monkeypatch.setattr(mod, "read_one_concept_value", lambda sid: None)
    monkeypatch.setattr(mod, "neptune_query", _route(_minimal_green_graph()))
    report = mod.run_audit("sysa")
    assert report["status"] == "pass"
    assert report["equipment_vm_layer"]["status"] == "skipped"
    assert report["metamodel_edges"]["status"] == "no_metamodel"
    # Report-only sections present but never gate the verdict.
    assert report["metamodel_edges"]["info_only"] is True
