# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/generalization_review.py — the ADR-0060 generalization-review lane.

Pure/offline — the CLI module's top level is stdlib-only, loaded via importlib
(the test_graph_snapshot_diff.py pattern). Pins the ADR-0060 verification
contract:

- classification table: dialect note ⇒ dialect-generic; a note quoting a
  system id or sampled value ⇒ customer-specific; ambiguous ⇒ unclear, never
  generic; customer markers win over dialect vocabulary;
- the marker set is DERIVED from input, never hardcoded (empty in ⇒ empty out);
- report renders BOTH verdict columns (heuristic always; LLM suggestion only
  when provided) and the human-decides header; business context is hash-only
  by default;
- without --llm-assist the run imports ZERO model code (meta-path import
  hook — the pinned gate);
- snapshot-mode CLI run against a fixture export produces the report file and
  exits 0; malformed inputs exit 2.

All fixture literals are placeholder-shaped (neutrality ratchet scans tests).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CLI_PATH = (Path(__file__).resolve().parents[2]
             / "scripts" / "generalization_review.py")


def _load_cli():
    if "adt_generalization_review_cli" in sys.modules:
        return sys.modules["adt_generalization_review_cli"]
    spec = importlib.util.spec_from_file_location(
        "adt_generalization_review_cli", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["adt_generalization_review_cli"] = module
    return module


cli = _load_cli()


# ── fixture snapshot (placeholder-shaped, format adt-graph-snapshot/1) ───────

_GUIDANCE_ERP = (
    "Base guidance for the connector.\n"
    "LEARNED: Cast temporal literals with an explicit TIMESTAMP format when "
    "filtering by date.\n"
    "LEARNED: Values in station_id match cmms asset codes after stripping "
    "the STN- prefix."
)


def _node(labels, props):
    return {"labels": list(labels), "props": dict(props)}


def _snapshot(nodes):
    return {"format": "adt-graph-snapshot/1",
            "exported_at": "2026-08-07T00:00:00+00:00",
            "node_count": len(nodes), "edge_count": 0,
            "nodes": list(nodes), "edges": []}


def _fixture_snapshot():
    return _snapshot([
        _node(["System"], {"id": "erp", "name": "Example ERP",
                           "query_guidance": _GUIDANCE_ERP,
                           "business_context": "We run three shifts at "
                                               "north-plant.",
                           "business_context_enabled": True}),
        _node(["System"], {"id": "cmms", "name": "Example CMMS",
                           "query_guidance": "Base guidance only."}),
        _node(["Plant"], {"id": "north-plant", "name": "North Plant"}),
        _node(["ValueMapping"], {
            "id": "vm-1", "method": "user_confirmed",
            "from_system": "erp", "from_field": "station_id",
            "from_value": "STN-4400", "to_system": "cmms",
            "to_field": "asset_code", "to_value": "ASSET-4400",
            "concept": "equipment", "confidence": 0.9}),
        _node(["IdentityRule"], {
            "id": "ir-1", "method": "crosswalk_confirmed",
            "source_system": "erp", "target_system": "cmms",
            "prefix": "STN-", "concept": "equipment"}),
        # NOT steward-confirmed — must be excluded from the rules table.
        _node(["JoinPath"], {
            "id": "jp-1", "method": "onboarding-llm",
            "from_system": "erp", "from_field": "order_id",
            "to_system": "cmms", "to_field": "ref_id"}),
        _node(["Concept"], {"id": "equipment", "name": "equipment"}),
    ])


@pytest.fixture()
def snapshot_file(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    return str(path)


def _loaded():
    return {
        "systems": [
            {"system_id": "erp", "name": "Example ERP",
             "query_guidance": _GUIDANCE_ERP, "business_context": "bc",
             "business_context_enabled": True},
        ],
        "rules": [
            {"kind": "ValueMapping",
             "props": {"id": "vm-1", "method": "user_confirmed",
                       "from_system": "erp", "from_value": "STN-4400",
                       "to_system": "cmms", "to_value": "ASSET-4400"}},
        ],
        "sites": [{"id": "north-plant", "name": "North Plant"}],
    }


def _markers():
    loaded = _loaded()
    return cli.deployment_marker_set(
        loaded["systems"], loaded["rules"], loaded["sites"])


# ── classification table (the ADR-0060 verification contract) ────────────────

@pytest.mark.parametrize("text,expected", [
    # generic SQL-dialect note ⇒ dialect-generic
    ("Cast temporal literals with an explicit TIMESTAMP format.",
     "dialect-generic"),
    ("Always quote identifiers and paginate large result sets.",
     "dialect-generic"),
    ("Use INTERVAL arithmetic; ORDER BY needs an explicit direction.",
     "dialect-generic"),
    # a note quoting a system id or sampled value ⇒ customer-specific
    ("The erp order numbers join on work order ids.", "customer-specific"),
    ("Translate STN-4400 to ASSET-4400 when bridging.", "customer-specific"),
    ("Ask about north-plant capacity first.", "customer-specific"),
    # customer markers WIN over dialect vocabulary
    ("In erp, cast temporal literals with TIMESTAMP.", "customer-specific"),
    # ambiguous falls to unclear, never generic
    ("Prefer smaller result sets when possible.", "unclear"),
    ("", "unclear"),
])
def test_classification_table(text, expected):
    result = cli.classify_note(text, _markers())
    assert result["verdict"] == expected
    if expected == "unclear":
        assert result["matched"] == []
    else:
        assert result["matched"], "non-unclear verdicts must show evidence"


def test_connector_type_mention_is_generic_evidence():
    connector_types = cli.connector_type_markers()
    assert "Athena" in connector_types          # engine vocabulary loaded
    assert "PostgreSQL" in connector_types
    result = cli.classify_note(
        "Athena requires the workgroup to be set explicitly.",
        set(), connector_types)
    assert result["verdict"] == "dialect-generic"
    assert "connector-type:Athena" in result["matched"]


def test_marker_matching_is_word_bounded():
    # 'erp' embedded in a longer token must NOT match (alnum lookarounds).
    result = cli.classify_note("The interpolation step is unrelated.",
                               {"erp"})
    assert result["verdict"] == "unclear"


# ── marker derivation: from input, never hardcoded ───────────────────────────

def test_marker_set_derived_from_input_never_hardcoded():
    assert cli.deployment_marker_set([], [], []) == set()
    markers = _markers()
    for expected in ("erp", "cmms", "Example ERP", "north-plant",
                     "North Plant", "STN-4400", "ASSET-4400"):
        assert expected in markers


def test_marker_set_drops_noise():
    systems = [{"system_id": "ab", "name": "42"}]   # too short / digit-only
    markers = cli.deployment_marker_set(systems, [], [])
    assert markers == set()


# ── snapshot loading ─────────────────────────────────────────────────────────

def test_load_from_snapshot_projects_and_filters(snapshot_file):
    loaded = cli.load_from_snapshot(snapshot_file)
    assert [s["system_id"] for s in loaded["systems"]] == ["cmms", "erp"]
    # only steward-confirmed methods survive; the onboarding-llm JoinPath is out
    kinds = [(r["kind"], r["props"]["method"]) for r in loaded["rules"]]
    assert kinds == [("IdentityRule", "crosswalk_confirmed"),
                     ("ValueMapping", "user_confirmed")]
    assert loaded["sites"][0]["id"] == "north-plant"
    assert "snapshot" in loaded["source"]


def test_load_from_snapshot_refuses_wrong_format(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": "other/1", "nodes": []}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported snapshot format"):
        cli.load_from_snapshot(str(path))


# ── report rendering ─────────────────────────────────────────────────────────

def _rows(snapshot_file, include_text=False):
    loaded = cli.load_from_snapshot(snapshot_file)
    return cli.build_rows(loaded, include_text=include_text), loaded


def test_report_renders_hash_only_by_default(snapshot_file):
    rows, loaded = _rows(snapshot_file)
    report = cli.render_report(rows, loaded["source"])
    assert "# Generalization review" in report
    assert "## Learned query guidance" in report
    assert "## Steward-confirmed rules" in report
    assert "## Business context" in report
    assert "## Promotion checklist" in report
    assert "The human decides" in report
    # heuristic verdict column always present, with both verdict values
    assert "heuristic verdict" in report
    assert "dialect-generic" in report            # the TIMESTAMP note
    assert "customer-specific" in report          # the cmms/STN- note
    # business context: hash-only — the prose never appears
    assert "We run three shifts" not in report
    assert "not-classified (hash-only)" in report
    # the excluded (non-confirmed) JoinPath never appears
    assert "jp-1" not in report and "onboarding-llm" not in report


def test_report_renders_llm_suggestion_column_when_provided(snapshot_file):
    rows, loaded = _rows(snapshot_file)
    suggestions = ["dialect-generic"] * len(rows["guidance"])
    report = cli.render_report(rows, loaded["source"],
                               llm_suggestions=suggestions)
    assert "| LLM suggestion |" in report         # beside, never instead of
    assert "heuristic verdict" in report
    assert "ADVISORY" in report or "advisory" in report.lower()


def test_report_omits_llm_column_without_suggestions(snapshot_file):
    rows, loaded = _rows(snapshot_file)
    report = cli.render_report(rows, loaded["source"])
    assert "| LLM suggestion |" not in report


def test_report_include_text_classifies_business_context(snapshot_file):
    rows, loaded = _rows(snapshot_file, include_text=True)
    report = cli.render_report(rows, loaded["source"], include_text=True)
    assert "We run three shifts" in report
    # the BC quotes the derived site literal ⇒ customer-specific
    bc_rows = rows["context"]
    assert bc_rows and bc_rows[0]["verdict"] == "customer-specific"


def test_markdown_cells_escape_pipes_and_truncate():
    assert cli._cell("a|b") == "a\\|b"
    long = "x" * 500
    assert len(cli._cell(long)) <= cli._CELL_MAX_LEN


# ── LLM label parsing (pure — advisory column never invents a verdict) ──────

@pytest.mark.parametrize("reply,expected", [
    ('{"0": "dialect-generic", "1": "customer-specific"}',
     ["dialect-generic", "customer-specific"]),
    ('Labels follow.\n{"0": "unclear", "1": "bogus-label"}\nDone.',
     ["unclear", ""]),
    ("no json at all", ["", ""]),
    ('{"7": "unclear"}', ["", ""]),               # out-of-range index dropped
])
def test_parse_llm_labels(reply, expected):
    assert cli.parse_llm_labels(reply, 2) == expected


# ── the pinned gate: no model import without --llm-assist ────────────────────

_MODEL_MODULES = ("strands", "agents.model_provider", "agents.model_catalog")


class _ForbidModelImports:
    """Meta-path hook that fails the test if any model module is imported."""

    def find_spec(self, name, path=None, target=None):
        if name in _MODEL_MODULES or name.startswith("strands."):
            raise AssertionError(
                f"model code imported without --llm-assist: {name}")
        return None


def test_no_model_import_without_llm_assist(snapshot_file, tmp_path):
    out = tmp_path / "report.md"
    saved = {name: sys.modules.pop(name) for name in list(sys.modules)
             if name in _MODEL_MODULES or name.startswith("strands.")}
    hook = _ForbidModelImports()
    sys.meta_path.insert(0, hook)
    try:
        rc = cli.main(["--snapshot", snapshot_file, "--out", str(out)])
    finally:
        sys.meta_path.remove(hook)
        sys.modules.update(saved)
    assert rc == 0
    assert out.exists() and "# Generalization review" in out.read_text(
        encoding="utf-8")


def test_llm_assist_calls_classifier_and_renders_column(
        snapshot_file, tmp_path, monkeypatch, capsys):
    out = tmp_path / "report.md"
    seen = {}

    def _fake_classify(texts, model_id=""):
        seen["texts"] = list(texts)
        seen["model_id"] = model_id
        return ["unclear"] * len(texts)

    monkeypatch.setattr(cli, "classify_with_llm", _fake_classify)
    rc = cli.main(["--snapshot", snapshot_file, "--out", str(out),
                   "--llm-assist", "--llm-model", "some-model"])
    assert rc == 0
    assert seen["model_id"] == "some-model"
    assert len(seen["texts"]) == 2                # the two LEARNED notes
    assert "| LLM suggestion |" in out.read_text(encoding="utf-8")


def test_llm_assist_failure_degrades_to_no_column(
        snapshot_file, tmp_path, monkeypatch, capsys):
    out = tmp_path / "report.md"

    def _boom(texts, model_id=""):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(cli, "classify_with_llm", _boom)
    rc = cli.main(["--snapshot", snapshot_file, "--out", str(out),
                   "--llm-assist"])
    assert rc == 0
    assert "WARNING: --llm-assist failed" in capsys.readouterr().out
    assert "| LLM suggestion |" not in out.read_text(encoding="utf-8")


# ── CLI exit-code contract ───────────────────────────────────────────────────

def test_cli_snapshot_mode_end_to_end(snapshot_file, tmp_path, capsys):
    out = tmp_path / "review.md"
    rc = cli.main(["--snapshot", snapshot_file, "--out", str(out)])
    assert rc == 0
    assert "Report written to" in capsys.readouterr().out
    report = out.read_text(encoding="utf-8")
    assert "STN-4400" in report                   # rule row rendered
    assert "## Promotion checklist" in report


def test_cli_missing_file_exits_2(tmp_path, capsys):
    rc = cli.main(["--snapshot", str(tmp_path / "absent.json")])
    assert rc == 2
    assert "ERROR:" in capsys.readouterr().out


def test_cli_wrong_format_exits_2(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": "nope"}), encoding="utf-8")
    rc = cli.main(["--snapshot", str(path)])
    assert rc == 2
    assert "unsupported snapshot format" in capsys.readouterr().out


# ── maintainability: the task's ≤50-line-function bound, pinned ──────────────

def test_all_functions_at_most_50_lines():
    import ast
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span > 50:
                offenders.append(f"{node.name} ({span} lines)")
    assert not offenders, f"functions over 50 lines: {offenders}"
