# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the OFFLINE semantic diff in scripts/graph_snapshot.py.

Pure/offline — the CLI module's top level is stdlib-only, loaded via importlib
(same pattern as test_provision_runtimes.py). Golden fixture pairs exercise:
missing-in-B, extra-in-B, changed (property-level), normalized volatile
properties (timestamps / created_at / updated_at / ARNs / runtime & gateway
target ids), float tolerance 1e-6, and the exit-code contract (0 identical
after normalization, 1 otherwise) plus the optional markdown report.
"""
import importlib.util
import json
import sys
from pathlib import Path

_CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "graph_snapshot.py"


def _load_cli():
    if "adt_graph_snapshot_cli" in sys.modules:
        return sys.modules["adt_graph_snapshot_cli"]
    spec = importlib.util.spec_from_file_location("adt_graph_snapshot_cli",
                                                  _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["adt_graph_snapshot_cli"] = module
    return module


cli = _load_cli()


def _snap(nodes, edges=()):
    return {"format": "adt-graph-snapshot/1",
            "exported_at": "2026-08-06T00:00:00+00:00",
            "node_count": len(nodes), "edge_count": len(edges),
            "nodes": list(nodes), "edges": list(edges)}


def _node(labels, props):
    return {"labels": labels, "props": props}


# ── Golden pair: missing / extra / changed, grouped by label & type ──────────

_A = _snap(
    nodes=[
        _node(["System"], {"id": "erp", "name": "ERP"}),
        _node(["Concept"], {"id": "work_order", "name": "work order"}),
        _node(["Field"], {"id": "f1", "name": "col_a"}),
    ],
    edges=[
        {"type": "MAPS_TO", "from": 2, "to": 1,
         "props": {"confidence": 0.9, "role": "identifier"}},
        {"type": "HAS_SCHEMA", "from": 0, "to": 2, "props": {}},
    ],
)

_B = _snap(
    nodes=[
        _node(["System"], {"id": "erp", "name": "ERP"}),
        _node(["Concept"], {"id": "work_order", "name": "RENAMED"}),
        _node(["Asset"], {"source_id": "a-9", "name": "surprise"}),
        _node(["Field"], {"id": "f1", "name": "col_a"}),
    ],
    edges=[
        {"type": "HAS_SCHEMA", "from": 0, "to": 3, "props": {}},
    ],
)


def test_golden_pair_missing_extra_changed():
    diff = cli.diff_snapshots(_A, _B)
    assert diff["identical"] is False
    system = diff["nodes"]["System"]
    assert not (system["missing_in_b"] or system["extra_in_b"]
                or system["changed"])
    changed = diff["nodes"]["Concept"]["changed"]
    assert changed == [{"item": "id=work_order", "properties": ["name"]}]
    assert diff["nodes"]["Asset"]["extra_in_b"] == ["source_id=a-9"]
    assert diff["nodes"]["Asset"]["a_count"] == 0
    assert diff["nodes"]["Asset"]["b_count"] == 1
    maps_to = diff["edges"]["MAPS_TO"]
    assert maps_to["a_count"] == 1 and maps_to["b_count"] == 0
    assert maps_to["missing_in_b"] == ["id=f1 -> id=work_order"]
    assert not diff["edges"]["HAS_SCHEMA"]["missing_in_b"]


# ── Volatile-property normalization + float tolerance ────────────────────────

def _volatile_pair():
    a = _snap([_node(["System"], {
        "id": "erp", "name": "ERP",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "last_seen_timestamp": "2026-01-03 10:00:00",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-AAA",
        "runtime_id": "adt_erp-AAA",
        "gateway_target_id": "TGT-OLD",
        "confidence": 0.5,
    })])
    b = _snap([_node(["System"], {
        "id": "erp", "name": "ERP",
        "created_at": "2026-08-06T09:00:00+00:00",
        "updated_at": "2026-08-06T09:05:00+00:00",
        "last_seen_timestamp": "2026-08-06 11:00:00",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_erp-ZZZ",
        "runtime_id": "adt_erp-ZZZ",
        "gateway_target_id": "TGT-NEW",
        "confidence": 0.5000004,  # inside 1e-6 tolerance
    })])
    return a, b


def test_volatile_properties_normalize_to_identical():
    a, b = _volatile_pair()
    diff = cli.diff_snapshots(a, b)
    assert diff["identical"] is True, json.dumps(diff, indent=1, default=str)


def test_float_beyond_tolerance_is_changed():
    a, b = _volatile_pair()
    b["nodes"][0]["props"]["confidence"] = 0.51
    diff = cli.diff_snapshots(a, b)
    assert diff["identical"] is False
    assert diff["nodes"]["System"]["changed"][0]["properties"] == ["confidence"]


def test_non_volatile_string_still_compares():
    a, b = _volatile_pair()
    b["nodes"][0]["props"]["name"] = "ERP-2"
    diff = cli.diff_snapshots(a, b)
    assert diff["nodes"]["System"]["changed"][0]["properties"] == ["name"]


def test_created_by_is_not_treated_as_volatile():
    a = _snap([_node(["ConceptOverlay"], {"id": "o1", "created_by": "alice"})])
    b = _snap([_node(["ConceptOverlay"], {"id": "o1", "created_by": "bob"})])
    assert cli.diff_snapshots(a, b)["identical"] is False


# ── Identity does not require an id property; order-independence ────────────

def test_id_free_nodes_match_by_fingerprint_regardless_of_order():
    x = _node(["Field"], {"name": "col_a", "schema_id": "s1"})
    y = _node(["Field"], {"name": "col_b", "schema_id": "s1"})
    diff = cli.diff_snapshots(_snap([x, y]), _snap([y, x]))
    assert diff["identical"] is True


def test_duplicate_identity_multiset_compares():
    x = _node(["Field"], {"name": "col_a"})
    diff = cli.diff_snapshots(_snap([x, x]), _snap([x, x]))
    assert diff["identical"] is True
    diff = cli.diff_snapshots(_snap([x, x]), _snap([x]))
    assert diff["identical"] is False
    assert len(diff["nodes"]["Field"]["missing_in_b"]) == 1
    assert diff["nodes"]["Field"]["extra_in_b"] == []


# ── Exit codes + markdown report through main() ──────────────────────────────

def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_diff_exit_zero_when_identical_after_normalization(tmp_path, capsys):
    a, b = _volatile_pair()
    rc = cli.main(["diff", _write(tmp_path, "a.json", a),
                   _write(tmp_path, "b.json", b)])
    assert rc == 0
    assert "IDENTICAL" in capsys.readouterr().out


def test_diff_exit_one_and_markdown_report(tmp_path, capsys):
    report = tmp_path / "report.md"
    rc = cli.main(["diff", _write(tmp_path, "a.json", _A),
                   _write(tmp_path, "b.json", _B),
                   "--report", str(report)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "DIFFERENT" in out
    text = report.read_text(encoding="utf-8")
    assert "| MAPS_TO | 1 | 0 |" in text
    assert "Verdict: **DIFFERENT**" in text


def test_import_without_yes_refuses(tmp_path, capsys):
    a, _ = _volatile_pair()
    rc = cli.main(["import", "--base-url", "http://localhost:0",
                   "--password", "x",
                   "--file", _write(tmp_path, "a.json", a)])
    assert rc == 2
    assert "--yes" in capsys.readouterr().out
