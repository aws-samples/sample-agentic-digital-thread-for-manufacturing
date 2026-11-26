#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
audit_answer.py — dev-lane deep-context answer auditor (annotate, never gate).

Fourth sibling of smoke_prompts.py, audit_learning.py, and eval_answers.py —
it COMPLEMENTS all three, never replaces any of them. Where those lanes GATE
(deterministic verdicts over a live env), this lane EXPLAINS: given a
finished eval/smoke report plus captured evidence files, it asks an LLM to
propose root-cause findings for each failed or flagged question, then applies
DETERMINISTIC post-LLM validation before anything is written. The LLM never
produces a verdict and never influences an exit code (ADR-0053 posture);
findings are annotations for a human steward.

The pure core — findings schema, deterministic validation, bundle building,
the measured-or-absent usage summation and the converse seam — lives in
``agents/audit_core.py`` so the SERVER can run the same lane (the container
image copies only agents/; it cannot import repo-root scripts/). Every moved
name is re-exported below at module scope, so this CLI and its tests are
unchanged. CLI parsing, report/evidence loading and findings-file writing
stay HERE.

v1 is strictly file-in/file-out: the report and evidence are JSON files on
disk — NO live fetch, NO raw production data. Evidence files are curated
captures (investigation traces, intelligence summaries, graph rules,
onboarded schema) exported by their owning tools.

Analysis: per failed/flagged question a claim/evidence bundle is built
(failure texts are annotated with the shared word-boundary error taxonomy —
smoke_prompts.classify_error_labels, imported, never duplicated) and sent to
Bedrock ``converse`` (boto3 ``bedrock-runtime``) with ``maxTokens`` set
EXPLICITLY. The default model id comes from ``agents/model_catalog.py``
(single source — this module carries NO literal model ids).

Deterministic validation (computed, post-LLM): ``taxonomy`` must be one of
the pinned 7 — data_gap, mapping_gap, schema_drift, prompt_gap, code_bug,
ux_gap, golden_candidate; every ``evidence_refs`` entry must resolve to an id
present in the report+evidence bundle (referential integrity); an optional
``suggested_mapping`` may only use keys that are kwargs of
``agents/self_learning.py::log_proposed_mapping`` (so later phases can route
findings through the existing proposal plumbing — v1 ONLY writes the file).
Invalid findings are DISCARDED and counted in ``discarded_invalid``.

Output findings JSON (pinned schema, version 1):
  {version: 1, source_report_sha, run_path (copied from the report),
   findings: [{id, taxonomy, question_id, claim_excerpt,
               evidence_refs: [...], rationale,
               suggested_mapping?: {from_system, from_field, from_value,
               to_system, to_field, to_value, concept, transform,
               proposed_type}}],
   discarded_invalid, usage?: {input_tokens, output_tokens}}

``usage`` carries MEASURED token counts summed from the converse responses,
and is present only when EVERY response carried usage — otherwise the key is
ABSENT. Never estimated, never dollars (ADR-0053 rule).

Usage:
  python3 scripts/audit_answer.py --report /tmp/eval_answers_report.json \
      [--evidence <trace.json> --evidence <rules.json> ...] \
      [--out /tmp/audit_answer_findings.json] [--model <bedrock-model-id>] \
      [--max-tokens 2000] [--region <aws-region>]

Env fallbacks: AUDIT_ANSWER_REPORT, AUDIT_ANSWER_MODEL, AUDIT_ANSWER_OUT.
Exit 0 = audit completed, REGARDLESS of findings (findings annotate, never
gate — no other success/failure code exists by design); 2 = usage/input/model
error.
"""
import argparse
import json
import os
import sys
from pathlib import Path


def _import_smoke():
    """Import the sibling smoke_prompts module (package or script context)."""
    try:
        from scripts import smoke_prompts  # unit tests: repo root on sys.path
        return smoke_prompts
    except ImportError:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import smoke_prompts  # script context: python3 scripts/audit_answer.py
        return smoke_prompts


smoke = _import_smoke()


def _import_model_catalog():
    """Import agents/model_catalog (single model-id source) with a sys.path
    fallback into agentic_digital_thread/ — mirror of the _import_smoke idiom."""
    try:
        from agentic_digital_thread.agents import model_catalog
        return model_catalog
    except ImportError:
        adt_dir = str(Path(__file__).resolve().parents[1]
                      / "agentic_digital_thread")
        if adt_dir not in sys.path:
            sys.path.insert(0, adt_dir)
        from agents import model_catalog
        return model_catalog


_model_catalog = _import_model_catalog()


def _import_audit_core():
    """Import agents/audit_core (the pure audit core).

    Deliberately ``agents`` FIRST (unlike _import_eval_core, which tries the
    ``agentic_digital_thread.agents`` package context first): the converse
    seam is a MODULE-LEVEL object, and the two spellings resolve to two
    distinct module instances when the repo root is also on sys.path (the
    namespace-package double import). Binding the same ``agents.audit_core``
    instance the server/agents side imports keeps ONE seam object, so
    patching either side controls the call. The fallback appends
    agentic_digital_thread/ for the `python3 scripts/audit_answer.py` context
    (where a bare ``agents`` import cannot resolve).
    """
    try:
        from agents import audit_core  # in-container / PYTHONPATH=. lane
        return audit_core
    except ImportError:
        adt_dir = str(Path(__file__).resolve().parents[1]
                      / "agentic_digital_thread")
        if adt_dir not in sys.path:
            sys.path.insert(0, adt_dir)
        from agents import audit_core
        return audit_core


audit_core = _import_audit_core()

FINDINGS_VERSION = audit_core.FINDINGS_VERSION
DEFAULT_OUT = "/tmp/audit_answer_findings.json"  # nosec B108 - fixed report path documented in --help; findings contain no secrets
DEFAULT_MAX_TOKENS = 2000

# ── Re-exports from agents/audit_core.py (extraction, behavior-preserving) ───
# Every moved name is re-exported at module scope as the SAME object so
# importers of THIS module (the pre-existing unit tests, which monkeypatch
# `audit_answer._converse_factory`) are unchanged. The seam object is OWNED by
# the core: audit_core.resolve_converse makes patching EITHER side control the
# call, so the re-export can never leave the real seam unpatched.
CLAIM_EXCERPT_CHARS = audit_core.CLAIM_EXCERPT_CHARS
TAXONOMY = audit_core.TAXONOMY
SUGGESTED_MAPPING_KEYS = audit_core.SUGGESTED_MAPPING_KEYS
_SYSTEM_PROMPT = audit_core.SYSTEM_PROMPT
_default_converse = audit_core._default_converse
_converse_factory = audit_core._converse_factory
report_sha = audit_core.report_sha
report_sha_bytes = audit_core.report_sha_bytes
_walk_ids = audit_core._walk_ids
collect_known_ids = audit_core.collect_known_ids
_response_text = audit_core._response_text
parse_findings_json = audit_core.parse_findings_json
_bracket_slice = audit_core._bracket_slice
_measured_usage = audit_core._measured_usage
summarize_usage = audit_core.summarize_usage
audit_bundle = audit_core.audit_bundle
_validate_finding = audit_core._validate_finding
validate_findings = audit_core.validate_findings
build_findings_doc = audit_core.build_findings_doc


def default_model_id(region: str | None = None) -> str:
    """Default auditor model — the catalog's orchestrator default (single
    source; no literal model ids in this module)."""
    return _model_catalog.default_model(_model_catalog.ORCHESTRATOR, region)


def build_bundle(row: dict, evidence_docs: list) -> dict:
    """CLI wrapper over ``audit_core.build_bundle``: injects the SHARED
    word-boundary error taxonomy (smoke_prompts.classify_error_labels —
    imported, never duplicated; it lives in scripts/, which the container
    image does not ship, so the core takes it as an argument)."""
    return audit_core.build_bundle(row, evidence_docs,
                                   classify=smoke.classify_error_labels)


# ── Report-row selection + file I/O (CLI layer) ─────────────────────────────

def load_json_file(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _row_flagged(row: dict) -> bool:
    """A report row that warrants auditing — failed OR flagged.

    Eval-shaped rows (verdict present): verdict == "fail", or a passing row
    whose honesty block carries fabrication flags. Smoke-shaped rows: not
    done, ungrounded, or any error/fallback signal.
    """
    verdict = row.get("verdict")
    if verdict is not None:
        if verdict == "fail":
            return True
        honesty = row.get("honesty") or {}
        return bool(honesty.get("fabrication_flags"))
    if row.get("done") is False:
        return True
    if row.get("grounding") == "ungrounded":
        return True
    return bool(row.get("err_labels") or row.get("fallback_signals"))


def failed_or_flagged_rows(report: dict) -> list:
    """The report rows to audit (eval and smoke reports both carry
    ``questions``); skipped rows never audit."""
    return [row for row in report.get("questions") or []
            if isinstance(row, dict) and row.get("verdict") != "skipped"
            and _row_flagged(row)]


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser(environ=None) -> argparse.ArgumentParser:
    """Build the CLI parser, accepting an environment mapping for pure tests."""
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        description="Deep-context answer auditor (annotate-never-gate; "
                    "file-in/file-out)")
    ap.add_argument("--report", default=env.get("AUDIT_ANSWER_REPORT", ""),
                    help="eval_answers.py or smoke_prompts.py report JSON "
                         "(AUDIT_ANSWER_REPORT); required")
    ap.add_argument("--evidence", action="append", default=[],
                    help="captured evidence JSON file (repeatable): "
                         "investigation traces, intelligence summaries, "
                         "graph rules, onboarded schema — NO live fetch")
    ap.add_argument("--out", default=env.get("AUDIT_ANSWER_OUT", ""),
                    help=f"findings JSON output path (AUDIT_ANSWER_OUT; "
                         f"default {DEFAULT_OUT})")
    ap.add_argument("--model", default=env.get("AUDIT_ANSWER_MODEL", ""),
                    help="Bedrock model id (AUDIT_ANSWER_MODEL; default: the "
                         "catalog's orchestrator default — single source, "
                         "agents/model_catalog.py)")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    help="explicit maxTokens for every converse call "
                         f"(default {DEFAULT_MAX_TOKENS})")
    ap.add_argument("--region", default=env.get("AWS_REGION", ""),
                    help="AWS region for bedrock-runtime and model-id "
                         "resolution (AWS_REGION)")
    return ap


def _load_inputs(args):
    """(report, evidence_docs) or None after printing the input error."""
    try:
        report = load_json_file(args.report)
    except (OSError, ValueError) as exc:
        print(f"input error: cannot read report {args.report}: "
              f"{type(exc).__name__}", flush=True)
        return None
    if not isinstance(report, dict) or not isinstance(
            report.get("questions"), list):
        print("input error: report carries no questions[] — not an "
              "eval/smoke report", flush=True)
        return None
    evidence_docs = []
    for path in args.evidence:
        try:
            evidence_docs.append(load_json_file(path))
        except (OSError, ValueError) as exc:
            print(f"input error: cannot read evidence {path}: "
                  f"{type(exc).__name__}", flush=True)
            return None
    return report, evidence_docs


def _audit_rows(rows, evidence_docs, args, model_id):
    """(raw findings, usage-or-None) across all flagged rows, or None after
    printing the model error. ONE core converse call per bundle; usage is
    present only when EVERY response carried measured counts."""
    raw_findings, measured = [], []
    for row in rows:
        bundle = build_bundle(row, evidence_docs)
        try:
            findings, usage = audit_core.audit_bundle(
                bundle, model_id=model_id, max_tokens=args.max_tokens,
                region=args.region or None, converse=_converse_factory)
        except Exception as exc:  # noqa: BLE001 - boto3 raises many types; all are the exit-2 model-error class
            print(f"model error on {bundle['question_id']}: "
                  f"{type(exc).__name__}", flush=True)
            return None
        if usage is not None:
            measured.append(usage)
        raw_findings.extend(findings)
    return raw_findings, audit_core.summarize_usage(measured, len(rows))


def main(argv=None) -> int:
    """Audit a finished report; exit 0 = audit completed (findings annotate,
    never gate), 2 = usage/input/model error. No other code exists."""
    args = build_parser().parse_args(argv)
    if not args.report:
        print("usage error: --report is required (or AUDIT_ANSWER_REPORT)",
              flush=True)
        return 2
    loaded = _load_inputs(args)
    if loaded is None:
        return 2
    report, evidence_docs = loaded
    model_id = args.model or default_model_id(args.region or None)
    if not model_id:
        print("model error: no model id (catalog empty and --model unset)",
              flush=True)
        return 2
    rows = failed_or_flagged_rows(report)
    print(f"== audit: {len(rows)} failed/flagged of "
          f"{len(report['questions'])} questions; model={model_id} ==",
          flush=True)
    audited = _audit_rows(rows, evidence_docs, args, model_id)
    if audited is None:
        return 2
    raw_findings, usage_block = audited
    known_ids = collect_known_ids(report, evidence_docs)
    findings, discarded = validate_findings(raw_findings, known_ids)
    doc = build_findings_doc(
        source_report_sha=report_sha(args.report),
        run_path=report.get("run_path"), findings=findings,
        discarded_invalid=discarded, usage=usage_block)
    out_path = args.out or DEFAULT_OUT
    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
    except OSError as exc:
        print(f"output error: cannot write {out_path}: "
              f"{type(exc).__name__}", flush=True)
        return 2
    print(f"== findings written: {out_path} — {len(findings)} finding(s), "
          f"{discarded} discarded_invalid ==", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
