# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""audit_core — the pure answer-audit core (findings schema + validation).

Extracted from scripts/audit_answer.py so the SERVER can run the same
annotate-never-gate audit lane (the container image copies only agents/ — the
server cannot import repo-root scripts/). scripts/audit_answer.py re-exports
every name here at module scope, so the CLI lane and its tests are unchanged;
this module COMPLEMENTS the CLI lane, never replaces it (ADR-0061 D2, the
agents/eval_core.py precedent).

Contract (pinned by tests/unit/test_audit_core_extraction.py):
- STDLIB-ONLY at import: no boto3/bedrock/strands/model_catalog import at
  module scope. ``boto3`` is imported LAZILY inside the converse seam, so
  importing this module never constructs a client and never touches the
  network.
- The converse seam OBJECT is owned HERE. ``scripts.audit_answer`` binds the
  same object, and ``resolve_converse`` makes monkeypatching EITHER side
  control the call (see its docstring) — a re-export must never leave a
  "patched the alias, real seam unpatched" hole.
- Findings validation is DETERMINISTIC and computed: the LLM proposes,
  ``validate_findings`` decides, invalid findings are DISCARDED and counted.
  The LLM never produces a verdict and never influences an exit code
  (ADR-0053 posture, ADR-0061 annotate-never-gate).
- ``usage`` is MEASURED tokens summed across responses or ABSENT — never
  estimated, never dollars.
- CLI argument parsing, report/evidence file loading and findings-file
  writing stay in scripts/audit_answer.py.
"""
import hashlib
import json
import re
from pathlib import Path

FINDINGS_VERSION = 1
CLAIM_EXCERPT_CHARS = 300

# The pinned finding taxonomy — exactly these 7, validated deterministically.
TAXONOMY = ("data_gap", "mapping_gap", "schema_drift", "prompt_gap",
            "code_bug", "ux_gap", "golden_candidate")

# suggested_mapping keys — every key MUST be a kwarg of
# agents/self_learning.py::log_proposed_mapping (pinned by signature
# introspection in tests/unit/test_audit_answer_findings.py) so findings can
# route through the existing proposal plumbing unchanged.
SUGGESTED_MAPPING_KEYS = ("from_system", "from_field", "from_value",
                          "to_system", "to_field", "to_value",
                          "concept", "transform", "proposed_type")

SYSTEM_PROMPT = (
    "You are an answer auditor for a manufacturing data platform. You receive "
    "ONE failed or flagged question from an evaluation report, with its "
    "deterministic scoring failures and captured evidence documents. Propose "
    "root-cause findings as a JSON array (and NOTHING else — no prose, no "
    "markdown fences). Each finding is an object: "
    '{"taxonomy": one of ' + "|".join(TAXONOMY) + ", "
    '"question_id": the bundle\'s question_id, '
    '"claim_excerpt": short quote of the suspect claim or failure, '
    '"evidence_refs": ids copied VERBATIM from the bundle (report/evidence '
    'ids only — never invent ids), '
    '"rationale": one or two sentences, '
    'optionally "suggested_mapping": an object using ONLY the keys '
    + ", ".join(SUGGESTED_MAPPING_KEYS) + " with string values}. "
    "Cite only ids present in the bundle. Return [] when no defensible "
    "finding exists. You explain; you never decide pass/fail."
)


# ── LLM seam ─────────────────────────────────────────────────────────────────

def _default_converse(*, model_id: str, max_tokens: int, system_prompt: str,
                      user_message: str, region: str | None = None) -> dict:
    """One Bedrock converse call. ``maxTokens`` is ALWAYS set explicitly —
    the MaxTokens invariant applies in spirit (a verbose model must not die
    silently mid-response)."""
    import boto3  # lazy: import purity — unit tests never construct a client
    kwargs = {"region_name": region} if region else {}
    client = boto3.client("bedrock-runtime", **kwargs)
    return client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )


# Module-level seam (the ``rederive`` idiom from feedback_exploration.py):
# tests script the response by assigning a fake; production uses the default.
# This module OWNS the object; scripts/audit_answer.py re-exports it.
_converse_factory = _default_converse


def resolve_converse(override=None):
    """Resolve the converse seam for one call — seam-ownership rule.

    An ``override`` that is still the PRISTINE default (the value a re-export
    carries when nobody patched it) defers to this module's ``_converse_factory``
    global, so patching ``agents.audit_core._converse_factory`` controls the
    CLI path too. A genuinely overridden callable (a test's fake assigned to
    ``scripts.audit_answer._converse_factory``, which the CLI passes in) wins.
    Patching either side therefore controls the call; patching both gives the
    explicit override precedence.
    """
    if override is None or override is _default_converse:
        return _converse_factory
    return override


# ── Pure helpers (offline-testable; no network) ──────────────────────────────

def report_sha_bytes(data: bytes) -> str:
    """sha256 of report bytes (provenance for the findings file)."""
    return hashlib.sha256(data).hexdigest()


def report_sha(path: str) -> str:
    """sha256 of the report file bytes (provenance for the findings file)."""
    return report_sha_bytes(Path(path).read_bytes())


def _walk_ids(node, out: set) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value and (
                    key == "id" or key.endswith("_id")):
                out.add(value)
            _walk_ids(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_ids(item, out)


def collect_known_ids(report: dict, evidence_docs: list) -> set:
    """Every id present in the report + evidence bundle: values of ``id`` and
    ``*_id`` keys, recursively (session ids, trace step ids, rule ids, ...).
    evidence_refs must resolve into this set (referential integrity)."""
    ids: set = set()
    _walk_ids(report, ids)
    for doc in evidence_docs:
        _walk_ids(doc, ids)
    return ids


def build_bundle(row: dict, evidence_docs: list, classify=None) -> dict:
    """The claim/evidence bundle for one flagged question.

    ``classify`` is the SHARED word-boundary error taxonomy
    (scripts/smoke_prompts.py::classify_error_labels — injected by the CLI,
    never duplicated here: it lives in scripts/, which the container does not
    ship). Without it a row that carries no precomputed ``err_labels`` simply
    gets none — error labels annotate, they never gate.
    """
    texts = ([str(f) for f in row.get("failures") or []]
             + [str(e) for e in row.get("errors") or []])
    labels = row.get("err_labels")
    if not labels:
        labels = classify(texts) if classify is not None else []
    return {
        "question_id": row.get("id") or row.get("title") or "",
        "session_id": row.get("session_id"),
        "verdict": row.get("verdict") or row.get("grounding"),
        "failures": row.get("failures") or [],
        "error_labels": labels,
        "honesty": row.get("honesty"),
        "answer_excerpt": row.get("answer_excerpt", ""),
        "evidence": evidence_docs,
    }


def _response_text(response: dict) -> str:
    message = ((response.get("output") or {}).get("message") or {})
    return "".join(part.get("text", "") for part in message.get("content") or []
                   if isinstance(part, dict))


def parse_findings_json(text: str) -> list:
    """Parse the model's findings array; tolerate surrounding prose/fences.
    Anything unparsable yields [] — the deterministic validator is the gate
    on content, this only extracts candidates."""
    for candidate in (text, _bracket_slice(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def _bracket_slice(text: str) -> str:
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    return match.group(0) if match else ""


def _measured_usage(response: dict):
    """(input_tokens, output_tokens) iff the response carries MEASURED
    counts; None otherwise. Never estimated."""
    usage = response.get("usage") or {}
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens, output_tokens
    return None


def summarize_usage(measured: list, expected: int):
    """The measured-or-absent usage block: the summed token counts iff EVERY
    one of ``expected`` responses carried measured counts, else None (⇒ the
    key is ABSENT from the findings document). Never estimated, never
    dollars (ADR-0053)."""
    if not expected or len(measured) != expected:
        return None
    return {"input_tokens": sum(pair[0] for pair in measured),
            "output_tokens": sum(pair[1] for pair in measured)}


def audit_bundle(bundle: dict, *, model_id: str, max_tokens: int,
                 region: str | None = None, converse=None) -> tuple:
    """ONE converse call for ONE bundle → (raw candidate findings, measured
    usage pair or None). Exceptions propagate to the caller: every failure
    class here is the caller's model-error class (CLI exit 2). Raw findings
    are UNVALIDATED — ``validate_findings`` is the gate."""
    factory = resolve_converse(converse)
    response = factory(model_id=model_id, max_tokens=max_tokens,
                       system_prompt=SYSTEM_PROMPT,
                       user_message=json.dumps(bundle, default=str),
                       region=region)
    return (parse_findings_json(_response_text(response)),
            _measured_usage(response))


# ── Deterministic post-LLM validation ───────────────────────────────────────

def _validate_finding(item, known_ids: set):
    """Deterministic post-LLM validation of ONE candidate finding.

    Returns the normalized finding dict, or None (⇒ discarded + counted):
    taxonomy must be one of the pinned 7; question_id must be a non-empty
    string; evidence_refs must be a non-empty list of strings that ALL
    resolve into the report+evidence bundle; suggested_mapping (optional)
    must be a non-empty dict using ONLY SUGGESTED_MAPPING_KEYS with string
    values.
    """
    if not isinstance(item, dict):
        return None
    if item.get("taxonomy") not in TAXONOMY:
        return None
    question_id = item.get("question_id")
    if not isinstance(question_id, str) or not question_id:
        return None
    refs = item.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return None
    if not all(isinstance(ref, str) and ref in known_ids for ref in refs):
        return None
    finding = {
        "id": str(item.get("id") or ""),
        "taxonomy": item["taxonomy"],
        "question_id": question_id,
        "claim_excerpt": str(item.get("claim_excerpt") or "")[:CLAIM_EXCERPT_CHARS],
        "evidence_refs": list(refs),
        "rationale": str(item.get("rationale") or ""),
    }
    mapping = item.get("suggested_mapping")
    if mapping is not None:
        if (not isinstance(mapping, dict) or not mapping
                or not set(mapping).issubset(SUGGESTED_MAPPING_KEYS)
                or not all(isinstance(v, str) for v in mapping.values())):
            return None
        finding["suggested_mapping"] = dict(mapping)
    return finding


def validate_findings(raw_findings: list, known_ids: set) -> tuple:
    """(valid findings, discarded_invalid count). Invalid findings are
    DISCARDED, never repaired; surviving findings get deterministic
    sequential ids when the model supplied none."""
    valid, discarded = [], 0
    for item in raw_findings or []:
        finding = _validate_finding(item, known_ids)
        if finding is None:
            discarded += 1
        else:
            valid.append(finding)
    for index, finding in enumerate(valid, start=1):
        if not finding["id"]:
            finding["id"] = f"finding-{index:03d}"
    return valid, discarded


def build_findings_doc(*, source_report_sha: str, run_path,
                       findings: list, discarded_invalid: int,
                       usage: "dict | None" = None) -> dict:
    """The pinned version-1 findings document. ``usage`` is included only
    when measured (never estimated, never dollars)."""
    doc = {
        "version": FINDINGS_VERSION,
        "source_report_sha": source_report_sha,
        "run_path": run_path,
        "findings": findings,
        "discarded_invalid": discarded_invalid,
    }
    if usage is not None:
        doc["usage"] = usage
    return doc
