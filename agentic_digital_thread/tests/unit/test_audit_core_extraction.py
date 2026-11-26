# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extraction pins for agents/audit_core.py (evals-phase2 Task 1.1).

The pure answer-audit core moved from scripts/audit_answer.py into
agents/audit_core.py (the container image copies only agents/ — the server
cannot import repo-root scripts/). The ADR-0061 D2 precedent is
agents/eval_core.py; its bar applies here: ALL pre-existing
tests/unit/test_audit_answer*.py pass with ZERO edits.

Pins here:

- **Import purity** — agents/audit_core.py is stdlib-only at MODULE scope
  (AST scan) and imports cleanly with boto3/botocore/strands/requests
  blocked, so importing it never constructs a client and never touches the
  network. ``boto3`` appears ONLY inside the converse seam function (lazy).
- **Seam ownership (both directions)** — the converse seam OBJECT is owned by
  the core: ``audit_answer._converse_factory is audit_core._converse_factory``
  and BOTH the legacy script attribute (what every pre-existing test
  monkeypatches) and the core attribute (what the future server worker will
  patch) control the call the core makes. This kills the "patched the
  re-export, real seam unpatched" failure class.
- **Byte parity** — a fixture run through the CLI produces the EXACT bytes
  the pre-extraction lane produced (captured before the move), and the seam
  receives the same kwargs with the same system-prompt / bundle payload.

Same repo-root-script import mechanism as test_audit_answer.py.
"""
import ast
import builtins
import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import audit_answer  # noqa: E402

from agents import audit_core  # noqa: E402

# Every name the plan moves into the core (public + private).
_MOVED_NAMES = (
    "CLAIM_EXCERPT_CHARS", "TAXONOMY", "SUGGESTED_MAPPING_KEYS",
    "FINDINGS_VERSION", "_default_converse", "_converse_factory",
    "report_sha", "report_sha_bytes", "_walk_ids", "collect_known_ids",
    "_response_text", "parse_findings_json", "_bracket_slice",
    "_measured_usage", "summarize_usage", "audit_bundle",
    "_validate_finding", "validate_findings", "build_findings_doc",
)

# Wrapped (NOT identical) names: the CLI layers scripts/-only behavior on top
# — build_bundle injects the smoke error-label classifier, which lives in
# scripts/ (the container does not ship it).
_WRAPPED_NAMES = ("build_bundle",)

# Names that deliberately STAY in scripts/ (CLI parsing + file I/O
# orchestration + the model-catalog default, which is not stdlib).
_SCRIPT_ONLY_NAMES = ("build_parser", "main", "load_json_file",
                      "_load_inputs", "_audit_rows", "default_model_id",
                      "failed_or_flagged_rows", "_row_flagged",
                      "DEFAULT_OUT", "DEFAULT_MAX_TOKENS")


# ── fixtures (shared by the parity pins) ────────────────────────────────────

def _report_doc() -> dict:
    """The parity fixture report — byte-stable (insertion-ordered dumps)."""
    return {
        "base_url": "https://example.test",
        "run_id": "r1",
        "run_path": "cli",
        "suite": {"pass": 0, "fail": 1, "skipped": 0, "total": 1},
        "questions": [{
            "id": "q-parity", "session_id": "eval-r1-1", "verdict": "fail",
            "failures": ["expected system_queries >= 2 (got 0)"],
            "answer_excerpt": "No batch data found",
            "honesty": {"absence_markers_matched": [],
                        "fabrication_flags": []},
        }],
    }


def _evidence_doc() -> dict:
    return {"trace": {"session_id": "eval-r1-1", "steps": [
        {"id": "trace-step-7", "summary": "queried MES for batch"},
        {"rule_id": "ir-mes-0123456789abcdef"},
    ]}}


_FIXTURE_FINDINGS = [{
    "taxonomy": "mapping_gap",
    "question_id": "q-parity",
    "claim_excerpt": "No batch data found",
    "evidence_refs": ["trace-step-7"],
    "rationale": "the MES trace shows the batch join was never attempted",
    "suggested_mapping": {"from_system": "erp", "to_system": "mes"},
}, {
    "taxonomy": "performance",          # outside the pinned 7 ⇒ discarded
    "question_id": "q-parity",
    "evidence_refs": ["trace-step-7"],
}]

# Captured from scripts/audit_answer.py BEFORE the extraction (the parity
# baseline). json.dump(indent=2) ⇒ no trailing newline.
_EXPECTED_FINDINGS_BYTES = """
{
  "version": 1,
  "source_report_sha": "4c18c64c2d3aac2c4f1d661ca1a1869c532c3e94d9e566aff5440a109f3a15a9",
  "run_path": "cli",
  "findings": [
    {
      "id": "finding-001",
      "taxonomy": "mapping_gap",
      "question_id": "q-parity",
      "claim_excerpt": "No batch data found",
      "evidence_refs": [
        "trace-step-7"
      ],
      "rationale": "the MES trace shows the batch join was never attempted",
      "suggested_mapping": {
        "from_system": "erp",
        "to_system": "mes"
      }
    }
  ],
  "discarded_invalid": 1,
  "usage": {
    "input_tokens": 111,
    "output_tokens": 22
  }
}
""".strip("\n").encode("utf-8")

# sha256 of the pre-extraction system prompt and of the serialized bundle
# payload — the seam's inputs must be byte-identical after the move.
_EXPECTED_PROMPT_SHA = (
    "ce40764fb4159175431259d7c209958c45bfebb397fd222dc96693df1ce2faee")
_EXPECTED_USER_MESSAGE_SHA = (
    "5be89e9c706b9133a2499a59bb3541f520c6bdaca04011a1f738345a71e854da")


def _write_fixture(tmp_path) -> tuple:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report_doc()), encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence_doc()), encoding="utf-8")
    return report_path, evidence_path, tmp_path / "findings.json"


def _fake_converse(calls, findings=None, usage=None):
    def fake(**kwargs):
        calls.append(kwargs)
        response = {"output": {"message": {"content": [
            {"text": json.dumps(findings if findings is not None
                                else _FIXTURE_FINDINGS)}]}}}
        if usage is not None:
            response["usage"] = dict(usage)
        return response
    return fake


# ── import purity ───────────────────────────────────────────────────────────

def test_audit_core_module_scope_imports_are_stdlib_only():
    """AST-scan the MODULE-LEVEL imports of agents/audit_core.py.

    Kills: a boto3/bedrock/strands/model_catalog import creeping to module
    scope — the core must import cleanly inside the container with no client
    construction and no network at import time. ``boto3`` is allowed ONLY
    lazily inside the converse seam (asserted below).
    """
    tree = ast.parse(Path(audit_core.__file__).read_text(encoding="utf-8"))
    roots, relative = set(), set()
    for node in tree.body:                      # module scope ONLY
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                relative.add((node.level, node.module))
            else:
                roots.add(node.module.split(".")[0])
    assert roots, "audit_core parsed but no module-scope imports found?"
    non_stdlib = roots - set(sys.stdlib_module_names)
    assert not non_stdlib, f"non-stdlib imports in audit_core: {non_stdlib}"
    assert not relative, f"unexpected intra-package imports: {relative}"


def test_boto3_is_imported_lazily_inside_the_converse_seam_only():
    tree = ast.parse(Path(audit_core.__file__).read_text(encoding="utf-8"))
    homes = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import) and any(
                    alias.name.split(".")[0] == "boto3"
                    for alias in inner.names):
                homes.add(node.name)
    assert homes == {"_default_converse"}, homes


def test_audit_core_imports_with_boto3_and_network_clients_blocked():
    """Execute a FRESH copy of the module with the cloud/network roots
    blocked. Proves import purity behaviorally (not just by AST) without
    disturbing the already-imported module object.
    """
    blocked = ("boto3", "botocore", "strands", "requests", "urllib3",
               "http.client", "socket")
    real_import = builtins.__import__
    attempted: list = []

    def _blocking_import(name, *args, **kwargs):
        if name in blocked or name.split(".")[0] in blocked:
            attempted.append(name)
            raise ImportError(f"blocked at import time: {name}")
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location(
        "_audit_core_purity_probe", audit_core.__file__)
    module = importlib.util.module_from_spec(spec)
    builtins.__import__ = _blocking_import
    try:
        spec.loader.exec_module(module)          # must NOT raise
    finally:
        builtins.__import__ = real_import
    assert attempted == [], f"import-time cloud imports: {attempted}"
    assert module.TAXONOMY == audit_core.TAXONOMY
    assert module._converse_factory is module._default_converse


# ── re-export identity ──────────────────────────────────────────────────────

def test_script_binds_the_same_core_module_the_agents_side_imports():
    """ONE module instance — the seam is a module-level object, so a second
    instance (the namespace-package double import) would silently split the
    seam between the CLI and the server worker."""
    assert audit_answer.audit_core is audit_core
    assert Path(audit_answer.audit_core.__file__) == Path(audit_core.__file__)


def test_every_moved_name_reexported_as_same_object():
    for name in _MOVED_NAMES:
        assert hasattr(audit_answer, name), f"missing re-export: {name}"
        assert getattr(audit_answer, name) is getattr(audit_core, name), name


def test_system_prompt_reexport_is_the_core_object():
    assert audit_answer._SYSTEM_PROMPT is audit_core.SYSTEM_PROMPT


def test_wrapped_and_script_only_names_stay_callable_in_scripts():
    for name in _WRAPPED_NAMES:
        assert callable(getattr(audit_answer, name)), name
        assert callable(getattr(audit_core, name)), name
    for name in _SCRIPT_ONLY_NAMES:
        assert hasattr(audit_answer, name), name
        assert not hasattr(audit_core, name), (
            f"{name} must stay in scripts/ (CLI layer)")


def test_import_audit_core_script_context_fallback(monkeypatch):
    """Subprocess-free coverage of the except-ImportError branch: when a bare
    ``agents`` import is unavailable (the `python3 scripts/audit_answer.py`
    context), the fallback appends agentic_digital_thread/ and resolves the
    same file."""
    real_import = builtins.__import__
    calls: list = []

    def _blocked_once(name, *args, **kwargs):
        if name == "agents" and not calls:
            calls.append(name)
            raise ImportError("blocked for fallback test: agents")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_once)
    module = audit_answer._import_audit_core()
    assert calls, "the fallback branch was never exercised"
    assert Path(module.__file__) == Path(audit_core.__file__)
    assert module.TAXONOMY == audit_core.TAXONOMY


# ── seam ownership: patching EITHER side controls the call ───────────────────

def test_converse_seam_object_is_owned_by_the_core():
    assert audit_answer._converse_factory is audit_core._converse_factory
    assert audit_core._converse_factory is audit_core._default_converse


def test_patching_the_legacy_script_seam_controls_the_core_call(
        tmp_path, monkeypatch):
    """THE regression pin: every pre-existing test monkeypatches
    ``scripts.audit_answer._converse_factory``. After the move the CALL is
    made inside agents/audit_core.py — the legacy patch must still be the
    callable that runs, and the core's own (unpatched) seam must NOT run."""
    report_path, evidence_path, out_path = _write_fixture(tmp_path)
    calls: list = []
    core_calls: list = []
    monkeypatch.setattr(audit_answer, "_converse_factory",
                        _fake_converse(calls))
    monkeypatch.setattr(audit_core, "_converse_factory",
                        _fake_converse(core_calls))
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    assert len(calls) == 1, "the legacy script seam did not control the call"
    assert core_calls == [], "the explicit override must win over the core"


def test_patching_the_core_seam_controls_the_script_path(tmp_path,
                                                        monkeypatch):
    """The other direction: the future server worker patches the CORE seam.
    With the script attribute untouched (still the pristine default), the
    core's seam is what runs — a re-export can never strand the real seam."""
    report_path, evidence_path, out_path = _write_fixture(tmp_path)
    core_calls: list = []
    monkeypatch.setattr(audit_core, "_converse_factory",
                        _fake_converse(core_calls))
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    assert len(core_calls) == 1


def test_resolve_converse_precedence_is_explicit(monkeypatch):
    fake = _fake_converse([])
    # pristine default (what an unpatched re-export carries) ⇒ defer to core
    assert audit_core.resolve_converse(
        audit_core._default_converse) is audit_core._converse_factory
    assert audit_core.resolve_converse(None) is audit_core._converse_factory
    # a genuine override wins
    assert audit_core.resolve_converse(fake) is fake
    # and a patched core seam is what "defer to core" resolves to
    monkeypatch.setattr(audit_core, "_converse_factory", fake)
    assert audit_core.resolve_converse(None) is fake
    assert audit_core.resolve_converse(audit_core._default_converse) is fake


def test_core_audit_bundle_uses_the_seam_and_returns_measured_usage():
    calls: list = []
    raw, usage = audit_core.audit_bundle(
        {"question_id": "q-1"}, model_id="test-model", max_tokens=99,
        converse=_fake_converse(calls, usage={"inputTokens": 7,
                                              "outputTokens": 3}))
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 99
    assert calls[0]["system_prompt"] is audit_core.SYSTEM_PROMPT
    assert raw == _FIXTURE_FINDINGS
    assert usage == (7, 3)


# ── measured-or-absent usage summation ──────────────────────────────────────

def test_summarize_usage_is_measured_or_absent():
    assert audit_core.summarize_usage([(1, 2), (10, 20)], 2) == {
        "input_tokens": 11, "output_tokens": 22}
    assert audit_core.summarize_usage([(1, 2)], 2) is None   # partial ⇒ absent
    assert audit_core.summarize_usage([], 0) is None         # no rows audited


# ── byte parity with the pre-extraction lane ────────────────────────────────

def test_findings_file_bytes_match_the_pre_extraction_baseline(tmp_path,
                                                               monkeypatch):
    report_path, evidence_path, out_path = _write_fixture(tmp_path)
    calls: list = []
    monkeypatch.setattr(audit_answer, "_converse_factory", _fake_converse(
        calls, usage={"inputTokens": 111, "outputTokens": 22}))
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    assert out_path.read_bytes() == _EXPECTED_FINDINGS_BYTES
    assert hashlib.sha256(out_path.read_bytes()).hexdigest() == (
        "a395f9bf315dccae75c32982d5edcf5d2efe329e615bd2182d4507a00431cf8f")


def test_seam_payload_matches_the_pre_extraction_baseline(tmp_path,
                                                          monkeypatch):
    """The prompt text and the serialized bundle reaching the seam are
    byte-identical to the pre-extraction lane (kwargs, prompt, payload)."""
    report_path, evidence_path, out_path = _write_fixture(tmp_path)
    calls: list = []
    monkeypatch.setattr(audit_answer, "_converse_factory",
                        _fake_converse(calls))
    assert audit_answer.main([
        "--report", str(report_path), "--evidence", str(evidence_path),
        "--out", str(out_path), "--model", "test-model"]) == 0
    assert len(calls) == 1
    call = calls[0]
    assert sorted(call) == ["max_tokens", "model_id", "region",
                            "system_prompt", "user_message"]
    assert hashlib.sha256(
        call["system_prompt"].encode("utf-8")).hexdigest() == (
        _EXPECTED_PROMPT_SHA)
    assert hashlib.sha256(
        call["user_message"].encode("utf-8")).hexdigest() == (
        _EXPECTED_USER_MESSAGE_SHA)


# ── the CLI wrapper keeps the shared error-taxonomy injection ───────────────

def test_build_bundle_wrapper_injects_the_smoke_classifier(monkeypatch):
    seen: list = []

    def _classify(texts):
        seen.append(list(texts))
        return ["FAKE_LABEL"]

    monkeypatch.setattr(audit_answer.smoke, "classify_error_labels",
                        _classify)
    bundle = audit_answer.build_bundle(
        {"id": "q-1", "failures": ["boom"], "errors": ["timeout"]}, [])
    assert seen == [["boom", "timeout"]]
    assert bundle["error_labels"] == ["FAKE_LABEL"]


def test_core_build_bundle_without_a_classifier_yields_no_labels():
    """The container ships no smoke_prompts: without the injected classifier
    a row gets no error labels (labels annotate, they never gate)."""
    bundle = audit_core.build_bundle(
        {"id": "q-1", "failures": ["boom"]}, [])
    assert bundle["error_labels"] == []
    precomputed = audit_core.build_bundle(
        {"id": "q-2", "err_labels": ["TIMEOUT"], "failures": ["boom"]}, [])
    assert precomputed["error_labels"] == ["TIMEOUT"]


# ── the annotate-never-gate posture survives the move ───────────────────────

def test_core_carries_no_exit_code_or_verdict_path():
    src = inspect.getsource(audit_core)
    assert "sys.exit" not in src
    assert "argparse" not in src
    for forbidden in ("anthropic.", "openai."):
        assert forbidden not in src        # no literal model ids in the core
