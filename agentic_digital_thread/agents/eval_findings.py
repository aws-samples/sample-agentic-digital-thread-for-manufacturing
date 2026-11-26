# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Findings ledger — a MINIMIZED durable row plus a SHORT-TTL customer-derived
detail row (ADR-0062 Workstream C).

``scripts/audit_answer.py`` findings are NOT reference-only: each one carries a
``claim_excerpt`` (a quote of the suspect claim, ≤300 chars) and a free-text
``rationale``. Both are derived from a customer answer, so this module splits
what an audit produces into three scopes with three different lifetimes:

======================  ====  ==========================================
Scope                   TTL   Contents
======================  ====  ==========================================
``eval-finding``        90 d  MINIMIZED: taxonomy, OPAQUE source/question
                              refs, status, timestamps, measured usage.
                              Nothing else, at any nesting depth.
``eval-finding-detail``  7 d  CUSTOMER-DERIVED: ``claim_excerpt``,
                              ``rationale``, ``suggested_mapping`` —
                              labelled :data:`CUSTOMER_DERIVED_LABEL` and
                              kept only long enough for a steward to
                              review. Expiry is NORMAL: the durable row
                              then reads ``detail_expired``.
``eval-finding-steward`` 180 d Steward annotations under a SEPARATE
                              ``steward-<finding_id>`` key. Regeneration
                              never touches them.
======================  ====  ==========================================

Pinned invariants (ADR-0062; tests in ``tests/unit/test_eval_findings.py``):

* **The durable row is minimized, enforced at WRITE time.** Every persisted
  scalar path must be in :data:`PERSISTED_FINDING_KEYS` /
  :data:`PERSISTED_INDEX_KEYS`; a non-allowlisted leaf RAISES *before* any
  write reaches the store (a renamed or nested excerpt leak cannot land).
  The detail and steward scopes carry their own allowlists.
* **LLM output is UNTRUSTED.** ``claim_excerpt`` / ``rationale`` /
  ``suggested_mapping`` values are type-checked, length-capped and
  secret-shape rejected through ``untrusted_text`` (the same pinned patterns
  as steward text — never copied here) BEFORE any persistence. A rejected
  value is DROPPED and COUNTED; it is never echoed in a return value, an
  error, or a log record.
* **``suggested_mapping`` VALUES stay out of durable storage.** They live in
  the 7 d detail row; the only door into durable storage is a steward
  explicitly filing a proposal (a later task's route), never an import.
* **Replacement DELETES.** :func:`replace_findings` removes every prior
  GENERATED row in the ``source_report_sha`` scope (durable *and* detail)
  before writing anew — replace-not-accrete (ADR-0052), proven by deletion,
  not by dedupe. Steward rows survive byte-identical, which is why
  :func:`finding_id_for` is DETERMINISTIC in ``(source_report_sha, local id)``.
* **The import BINDS its source.** :func:`import_findings` takes the findings
  document and the source report BYTES together, verifies
  ``sha256(report) == findings.source_report_sha`` and re-checks referential
  integrity through ``audit_core`` — then persists minimized rows only. The
  uploaded report is NEVER handed to a store. (The HTTP ingress size cap
  belongs to the Wave-2 route, which reads a capped stream BEFORE parsing;
  this module bounds the findings COUNT it will accept.)
* **The digest is computed ON READ.** :func:`weekly_digest` aggregates the
  bounded index (taxonomy counts + a bounded ux_gap REF list, unresolved) and
  performs ZERO writes. There is no digest writer and no clustering.
* **``usage`` is measured or ABSENT** — exactly ``{input_tokens,
  output_tokens}`` ints or no key at all. Never zero-as-unknown, never
  dollars (ADR-0053).
* **Telemetry rides the REAL ledger** — ``artifact_usage.bump_artifact_usage``
  with ``("eval-finding", <finding_id>)`` refs (ADR-0052), never an invented
  counter on the index item.
* **Every writer ships its READ path** (ADR-0052 / ADR-0062 D10).
  :func:`writer_telemetry` reads the SAME ledger back per writer kind —
  ``eval-finding`` (import, here), ``proposed-mapping`` (steward-filed, in
  ``eval_findings_routes``) and ``audit-run`` (the Wave-4 annotator's slot,
  which reports unfired until that writer lands). It surfaces REFS ONLY, so
  "counters exist" can never stand in for "this writer fired".

Import-pure module (``catalog.py`` discipline): no boto3 at import; the stores
are lazy module-level accessors with a :func:`_reset_state` test seam.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .audit_core import (CLAIM_EXCERPT_CHARS, FINDINGS_VERSION,
                         SUGGESTED_MAPPING_KEYS, TAXONOMY, collect_known_ids,
                         report_sha_bytes, validate_findings)
from .untrusted_text import sanitize_untrusted, validate_untrusted

logger = logging.getLogger(__name__)

# ── lifetimes (the data-classification contract — do not relax) ────────────
FINDING_TTL_SECONDS = 90 * 24 * 3600     # minimized durable row
DETAIL_TTL_SECONDS = 7 * 24 * 3600       # CUSTOMER-DERIVED detail
STEWARD_TTL_SECONDS = 180 * 24 * 3600    # steward annotations

#: The label every detail row carries, so no reader can mistake it for
#: reference-only metadata.
CUSTOMER_DERIVED_LABEL = "customer-derived"

#: Finding lifecycle. Generated rows are always :data:`GENERATED_STATUS`; only
#: a steward moves a finding, and the move lands in the STEWARD scope.
STATUSES = ("open", "annotated", "filed", "dismissed")
GENERATED_STATUS = "open"

#: The only ``run_path`` values a report can carry (``eval_core`` stamps one of
#: these). Anything else is dropped rather than persisted — a filesystem path
#: is not a reference.
RUN_PATHS = ("server", "cli")

# ── bounds ─────────────────────────────────────────────────────────────────
RATIONALE_MAX_CHARS = 1000
MAPPING_VALUE_MAX_CHARS = 200
NOTE_MAX_CHARS = 500
ANNOTATED_BY_MAX_CHARS = 128
MAX_FINDINGS_PER_IMPORT = 200
MAX_INDEX_FINDINGS = 500
DEFAULT_DIGEST_WINDOW_DAYS = 7
MAX_DIGEST_WINDOW_DAYS = 90
UX_GAP_REF_LIMIT = 20

_INDEX_KEY = "index"
_FINDING_ID_PREFIX = "f-"
_STEWARD_KEY_PREFIX = "steward-"

# ── writer telemetry: the ledger ref kinds this surface reads back ──────────
#: The findings-IMPORT writer (fired in :func:`_bump_findings_usage` below).
WRITER_KIND_IMPORT = "eval-finding"
#: The steward-triggered PROPOSAL-FILED writer (fired by the file-proposal
#: route — the only door mapping values enter durable storage through).
WRITER_KIND_PROPOSAL_FILED = "proposed-mapping"
#: The Wave-4 bounded-triage-annotator SLOT. Nothing fires this ref kind yet;
#: the annotator (``audit_scheduler._run_audit``) will bump it with its
#: ``run_id`` and needs NO change here — the slot already reads back, so the
#: writer arrives with its read path already in place (ADR-0052).
WRITER_KIND_AUDIT_RUN = "audit-run"

#: The kinds :func:`writer_telemetry` reports, in render order. Every other
#: ledger kind (rules, concepts, other subsystems' writers) is ignored — a
#: concept ref is a vocabulary word, and this surface publishes refs only.
TELEMETRY_WRITER_KINDS = (WRITER_KIND_IMPORT, WRITER_KIND_PROPOSAL_FILED,
                          WRITER_KIND_AUDIT_RUN)
TELEMETRY_WINDOW_DAYS = 30
MAX_TELEMETRY_WINDOW_DAYS = 90
TELEMETRY_REF_LIMIT = 20

#: RECURSIVE leaf allowlist for a persisted DURABLE row. Extending a durable
#: row means editing this literal set (and the ADR) — the write-time guard
#: raises otherwise, so a nested or renamed customer-derived field cannot land.
PERSISTED_FINDING_KEYS = frozenset({
    "finding_id", "taxonomy", "source_report_sha", "source_ref_sha",
    "question_ref", "run_path", "status", "created_at",
    "usage", "input_tokens", "output_tokens",
})

#: Same contract for the ONE bounded replace-not-accrete index item.
PERSISTED_INDEX_KEYS = frozenset({
    "findings", "finding_id", "taxonomy", "source_report_sha", "question_ref",
    "created_at",
})

#: The CUSTOMER-DERIVED scope: the only place generated free text may live.
PERSISTED_DETAIL_KEYS = frozenset({
    "finding_id", "data_class", "claim_excerpt", "rationale",
    "suggested_mapping",
}) | frozenset(SUGGESTED_MAPPING_KEYS)

#: The steward scope (survives regeneration byte-identical).
PERSISTED_STEWARD_KEYS = frozenset({
    "finding_id", "status", "steward_note", "annotated_at", "annotated_by",
})

_FINDING_ID_RE = re.compile(r"^f-[0-9a-f]{16}$")
_REPORT_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ANNOTATED_BY_RE = re.compile(r"^[A-Za-z0-9_.:@|-]{1,%d}$" % ANNOTATED_BY_MAX_CHARS)

_FINDINGS = None   # lazy JobStore("eval-finding")
_DETAILS = None    # lazy JobStore("eval-finding-detail")
_STEWARD = None    # lazy JobStore("eval-finding-steward")


def _store(kind: str, ttl_seconds: int):
    """One lazily constructed JobStore (``job_store`` transitively imports
    boto3-importing modules, so the import stays inside the accessor)."""
    from .job_store import JobStore
    return JobStore(kind, ttl_seconds=ttl_seconds)


def _findings():
    """The MINIMIZED durable scope (90 d)."""
    global _FINDINGS
    if _FINDINGS is None:
        _FINDINGS = _store("eval-finding", FINDING_TTL_SECONDS)
    return _FINDINGS


def _details():
    """The CUSTOMER-DERIVED detail scope (7 d)."""
    global _DETAILS
    if _DETAILS is None:
        _DETAILS = _store("eval-finding-detail", DETAIL_TTL_SECONDS)
    return _DETAILS


def _steward():
    """The steward-annotation scope (180 d) — never written by a generator."""
    global _STEWARD
    if _STEWARD is None:
        _STEWARD = _store("eval-finding-steward", STEWARD_TTL_SECONDS)
    return _STEWARD


def _reset_state() -> None:
    """Test seam: drop all three handles (fresh stores per test)."""
    global _FINDINGS, _DETAILS, _STEWARD
    _FINDINGS = _DETAILS = _STEWARD = None


# ── ids + opaque refs ──────────────────────────────────────────────────────

def _sha16(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def finding_id_for(source_report_sha: str, local_id: str) -> str:
    """DETERMINISTIC finding id for one ``(report scope, model-assigned id)``.

    Determinism is what makes steward annotations survive a re-import: the
    same finding in the same report keeps the same key, so
    ``steward-<finding_id>`` still points at it after the generated rows are
    replaced.
    """
    return f"{_FINDING_ID_PREFIX}{_sha16(f'{source_report_sha}:{local_id}')}"


def question_ref(question_id) -> str:
    """The OPAQUE question reference — a sha-16 of the question id, so nothing
    question-derived is stored in the clear (question ids are golden-set slugs
    today and arbitrary strings tomorrow)."""
    return _sha16(question_id)


def steward_key(finding_id: str) -> str:
    """The separate steward-scoped key for one finding."""
    return f"{_STEWARD_KEY_PREFIX}{finding_id}"


def _source_ref_sha(run_id, question_id) -> "str | None":
    """The opaque queue source ref for this finding, when the report identifies
    its run — the join key back to ``eval_queue`` items and steward decisions.
    Absent (never invented) when the ref would not be well-shaped."""
    from . import eval_queue
    if not run_id or not question_id:
        return None
    ref = eval_queue.golden_failure_source_ref(run_id, question_id)
    if eval_queue.source_of(ref) is None:
        return None
    return eval_queue.ref_sha(ref)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── untrusted LLM output (capped, secret-shape rejected, never echoed) ──────

def _bounded_field(value, limit: int) -> "tuple[str | None, str | None]":
    """``(accepted_text | None, rejection_reason | None)`` for one LLM string.

    Length is a CAP (machine output is bounded, not refused); type and content
    are REJECTIONS reusing the pinned ``untrusted_text`` patterns. The check
    runs on the capped text — exactly what would be persisted. The value is
    never returned, logged or echoed on rejection.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "invalid_input"
    text = sanitize_untrusted(value)[:limit].strip()
    if not text:
        return None, None
    problem = validate_untrusted(text, max_len=limit, field="llm_field")
    return (None, problem[0]) if problem else (text, None)


def _bounded_mapping(mapping) -> "tuple[dict, dict]":
    """``({key: value}, {path: reason})`` over :data:`SUGGESTED_MAPPING_KEYS`.

    The ALLOWLIST is iterated (not the input), so an unexpected key cannot be
    copied through; it is counted as ``unknown_keys``.
    """
    if mapping is None:
        return {}, {}
    if not isinstance(mapping, dict):
        return {}, {"suggested_mapping": "invalid_input"}
    clean: dict = {}
    rejected: dict = {}
    for key in SUGGESTED_MAPPING_KEYS:
        if key not in mapping:
            continue
        text, reason = _bounded_field(mapping[key], MAPPING_VALUE_MAX_CHARS)
        if reason:
            rejected[f"suggested_mapping.{key}"] = reason
        elif text:
            clean[key] = text
    if any(key not in SUGGESTED_MAPPING_KEYS for key in mapping):
        rejected["suggested_mapping"] = "unknown_keys"
    return clean, rejected


def sanitize_llm_output(finding) -> "tuple[dict, dict]":
    """``(detail, rejected)`` for ONE finding's customer-derived detail.

    ``detail`` carries only accepted, capped values (``claim_excerpt`` ≤
    :data:`~agents.audit_core.CLAIM_EXCERPT_CHARS`, ``rationale`` ≤
    :data:`RATIONALE_MAX_CHARS`, mapping values ≤
    :data:`MAPPING_VALUE_MAX_CHARS`). ``rejected`` maps field path → pinned
    reason (``invalid_input`` | ``too_long`` | ``secret_like_content`` |
    ``fence_delimiter`` | ``unknown_keys``) and contains NO values, so a
    caller can count and report rejections without echoing one.
    """
    if not isinstance(finding, dict):
        return {}, {"finding": "invalid_input"}
    detail: dict = {}
    rejected: dict = {}
    for field, limit in (("claim_excerpt", CLAIM_EXCERPT_CHARS),
                         ("rationale", RATIONALE_MAX_CHARS)):
        text, reason = _bounded_field(finding.get(field), limit)
        if reason:
            rejected[field] = reason
        elif text:
            detail[field] = text
    mapping, mapping_rejected = _bounded_mapping(finding.get("suggested_mapping"))
    if mapping:
        detail["suggested_mapping"] = mapping
    rejected.update(mapping_rejected)
    if rejected:
        # FIELD PATHS ONLY — a rejected value must not reach a log record.
        logger.debug("eval_findings_llm_field_rejected: fields=%s",
                     sorted(rejected))
    return detail, rejected


# ── write-time minimization guard ──────────────────────────────────────────

def _unexpected_leaves(payload, allowlist) -> set:
    """Every leaf KEY reachable in ``payload`` that is not allowlisted
    (recursive — a nested or renamed field is caught at any depth)."""
    found: set = set()

    def _walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key not in allowlist:
                    found.add(str(key))
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(payload)
    return found


def _guard(payload, allowlist, scope: str) -> None:
    """RAISE before any write when ``payload`` carries a non-allowlisted leaf.

    Defensive by design: a future edit that grows a row must grow the pinned
    allowlist (and the ADR) with it rather than silently persisting a field.
    Only KEY names appear in the message — never a value.
    """
    leaked = _unexpected_leaves(payload, allowlist)
    if leaked:
        raise ValueError(
            f"{scope} row carries non-allowlisted keys: {sorted(leaked)}")


# ── generated rows ─────────────────────────────────────────────────────────

def _measured_usage(usage) -> "dict | None":
    """The measured usage block or ``None`` (⇒ the key is ABSENT). Exactly
    ``{input_tokens, output_tokens}`` ints — never estimated, never dollars,
    never zero-as-unknown."""
    if not isinstance(usage, dict) or set(usage) != {"input_tokens",
                                                     "output_tokens"}:
        return None
    values = [usage["input_tokens"], usage["output_tokens"]]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
               for v in values):
        return None
    return {"input_tokens": values[0], "output_tokens": values[1]}


def _durable_row(source_report_sha: str, finding: dict, *, run_path,
                 usage, run_id, created_at: str) -> dict:
    """ONE minimized durable row. Refs only — the excerpt, the rationale and
    the mapping go to the 7 d customer-derived scope, never here."""
    question_id = finding.get("question_id")
    row = {
        "finding_id": finding_id_for(source_report_sha,
                                     str(finding.get("id") or "")),
        "taxonomy": finding.get("taxonomy"),
        "source_report_sha": source_report_sha,
        "question_ref": question_ref(question_id),
        "status": GENERATED_STATUS,
        "created_at": created_at,
    }
    ref_sha = _source_ref_sha(run_id, question_id)
    if ref_sha:
        row["source_ref_sha"] = ref_sha
    if run_path in RUN_PATHS:
        row["run_path"] = run_path
    if usage is not None:
        # The IMPORT's measured usage, stamped on each row of the scope.
        row["usage"] = dict(usage)
    return row


def _detail_row(finding_id: str, detail: dict) -> dict:
    """The customer-derived detail row, explicitly labelled."""
    return {"finding_id": finding_id, "data_class": CUSTOMER_DERIVED_LABEL,
            **detail}


def _index_entries() -> list:
    index = _findings().get(_INDEX_KEY)
    entries = list((index or {}).get("findings") or []) \
        if isinstance(index, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def _index_entry(row: dict) -> dict:
    return {"finding_id": row["finding_id"], "taxonomy": row["taxonomy"],
            "source_report_sha": row["source_report_sha"],
            "question_ref": row["question_ref"],
            "created_at": row["created_at"]}


def _write_index(source_report_sha: str, rows: list) -> None:
    """Replace-not-accrete: the scope's entries are rebuilt, other scopes'
    entries are preserved, and the whole index stays bounded."""
    kept = [entry for entry in _index_entries()
            if entry.get("source_report_sha") != source_report_sha]
    payload = {"findings": ([_index_entry(row) for row in rows]
                            + kept)[:MAX_INDEX_FINDINGS]}
    _guard(payload, PERSISTED_INDEX_KEYS, "index")
    _findings()[_INDEX_KEY] = payload


def _delete_scope(source_report_sha: str) -> int:
    """DELETE every generated row in the scope — durable AND detail.

    Deletion (not dedupe) is the contract: a re-import that no longer contains
    a finding must leave no trace of it. Steward rows are in a different store
    and are never touched here.

    The sweep is INDEX-DRIVEN, so it is bounded by
    :data:`MAX_INDEX_FINDINGS`: a row old enough to have been evicted from the
    index is left to its own 90 d TTL rather than found by a scan (``JobStore``
    cannot enumerate, and a table scan for this is not worth it).
    """
    findings, details = _findings(), _details()
    deleted = 0
    for entry in _index_entries():
        if entry.get("source_report_sha") != source_report_sha:
            continue
        finding_id = entry.get("finding_id")
        if not isinstance(finding_id, str):
            continue
        findings.pop(finding_id, None)
        details.pop(finding_id, None)
        deleted += 1
    return deleted


def _bump_findings_usage(rows: list) -> int:
    """Fire the REAL usage ledger for the findings-import writer (ADR-0052).

    Lazy import (``artifact_usage`` pulls the learning store) and best-effort
    by design — ``bump_artifact_usage`` swallows its own backend failures.
    """
    if not rows:
        return 0
    from .artifact_usage import bump_artifact_usage
    return bump_artifact_usage([("eval-finding", row["finding_id"])
                                for row in rows])


def replace_findings(source_report_sha: str, findings: list, *,
                     run_path=None, usage=None, run_id=None) -> dict:
    """Replace ALL generated rows for one report scope; returns a summary.

    The prior scope is DELETED first (durable + detail), then the new
    minimized rows and their 7 d customer-derived detail rows are written and
    the bounded index is rebuilt. Steward annotations are untouched.

    Returns ``{source_report_sha, written, deleted, rejected_fields,
    taxonomy_counts, usage_bumped}``. ``rejected_fields`` counts DROPPED LLM
    values by reason and carries no values.
    """
    if not isinstance(source_report_sha, str) or \
            not _REPORT_SHA_RE.match(source_report_sha):
        raise ValueError("source_report_sha must be a sha256 hex digest")
    rows, details, rejected = _build_rows(
        source_report_sha, findings or [], run_path=run_path,
        usage=_measured_usage(usage), run_id=run_id)
    deleted = _delete_scope(source_report_sha)
    findings_store, details_store = _findings(), _details()
    for row in rows:
        findings_store[row["finding_id"]] = row
    for finding_id, detail in details.items():
        details_store[finding_id] = detail
    _write_index(source_report_sha, rows)
    counts: dict = {}
    for row in rows:
        counts[row["taxonomy"]] = counts.get(row["taxonomy"], 0) + 1
    return {"source_report_sha": source_report_sha, "written": len(rows),
            "deleted": deleted, "rejected_fields": rejected,
            "taxonomy_counts": counts,
            "usage_bumped": _bump_findings_usage(rows)}


def _build_rows(source_report_sha: str, findings: list, *, run_path, usage,
                run_id) -> "tuple[list, dict, dict]":
    """``(durable rows, {finding_id: detail row}, {reason: count})``.

    Every row is guarded against its allowlist HERE — before the first write —
    so a non-allowlisted leaf aborts the whole import rather than persisting
    half of it.
    """
    created_at = _now_iso()
    rows: list = []
    details: dict = {}
    rejected: dict = {}
    for finding in findings[:MAX_FINDINGS_PER_IMPORT]:
        if not isinstance(finding, dict) or finding.get("taxonomy") not in TAXONOMY:
            rejected["invalid_finding"] = rejected.get("invalid_finding", 0) + 1
            continue
        row = _durable_row(source_report_sha, finding, run_path=run_path,
                           usage=usage, run_id=run_id, created_at=created_at)
        _guard(row, PERSISTED_FINDING_KEYS, "finding")
        detail, field_rejections = sanitize_llm_output(finding)
        for reason in field_rejections.values():
            rejected[reason] = rejected.get(reason, 0) + 1
        rows.append(row)
        if detail:
            detail_row = _detail_row(row["finding_id"], detail)
            _guard(detail_row, PERSISTED_DETAIL_KEYS, "detail")
            details[row["finding_id"]] = detail_row
    return rows, details, rejected


# ── bound import (findings + their source report, together) ────────────────

def _parse_report(report_bytes) -> "tuple[dict | None, bytes | None]":
    """``(report, raw bytes)`` for a transiently validated upload, else
    ``(None, None)``. The parsed report is used for the sha binding and
    referential integrity ONLY — it is never handed to a store."""
    if isinstance(report_bytes, str):
        raw = report_bytes.encode("utf-8")
    elif isinstance(report_bytes, (bytes, bytearray)):
        raw = bytes(report_bytes)
    else:
        return None, None
    try:
        report = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None
    return (report, raw) if isinstance(report, dict) else (None, None)


def _check_doc(findings_doc) -> "tuple[str, str] | None":
    """Pinned shape problems for the findings document, or ``None``."""
    if not isinstance(findings_doc, dict):
        return ("invalid_findings_doc", "'findings_file' must be an object")
    if findings_doc.get("version") != FINDINGS_VERSION:
        return ("unsupported_version",
                f"'findings_file.version' must be {FINDINGS_VERSION}")
    if not isinstance(findings_doc.get("findings"), list):
        return ("invalid_findings_doc",
                "'findings_file.findings' must be a list")
    if len(findings_doc["findings"]) > MAX_FINDINGS_PER_IMPORT:
        return ("too_many_findings",
                f"at most {MAX_FINDINGS_PER_IMPORT} findings per import")
    return None


def import_findings(findings_doc, report_bytes, *,
                    evidence_docs=None) -> "tuple[dict | None, tuple[str, str] | None]":
    """Import one findings document TOGETHER WITH its source report bytes.

    The binding is FAIL-CLOSED: ``sha256(report_bytes)`` must equal
    ``findings_doc["source_report_sha"]`` (there is no "resolve the referenced
    report if we happen to have it" path), and every ``evidence_refs`` id is
    re-checked against the report/evidence bundle through
    ``audit_core.validate_findings`` — invalid findings are DISCARDED and
    counted, never repaired. The uploaded report is parsed transiently and
    NEVER persisted.

    ``evidence_docs`` is the OPTIONAL transient bundle the auditor ran with
    (``audit_answer.py --evidence <path>...``). It goes into the SAME
    ``audit_core.collect_known_ids`` call as the report, so referential
    integrity resolves against report ids UNION evidence ids: a finding citing
    an id that exists only in an evidence document is kept instead of
    discarded, while a ref resolving in NEITHER half is still discarded and
    counted in ``discarded_invalid``. It is customer-derived (traces,
    intelligence summaries), so — exactly like the report — it is used for
    validation and then dropped: nothing from it is persisted or logged.

    Returns ``(summary, None)`` — the :func:`replace_findings` summary plus
    ``discarded_invalid`` — or ``(None, (reason, detail))``.
    """
    problem = _check_doc(findings_doc)
    if problem:
        return None, problem
    report, raw = _parse_report(report_bytes)
    if report is None:
        return None, ("invalid_source_report",
                      "'source_report' must be a JSON object")
    computed = report_sha_bytes(raw)
    if computed != findings_doc.get("source_report_sha"):
        return None, ("source_report_mismatch",
                      "'source_report' does not match "
                      "'findings_file.source_report_sha'")
    valid, discarded = validate_findings(
        findings_doc["findings"],
        collect_known_ids(report, list(evidence_docs or [])))
    summary = replace_findings(computed, valid,
                               run_path=findings_doc.get("run_path"),
                               usage=findings_doc.get("usage"),
                               run_id=report.get("run_id"))
    summary["discarded_invalid"] = discarded
    return summary, None


# ── steward annotations (separate scope, survive regeneration) ──────────────

def _check_annotation(status, annotated_by) -> "tuple[str, str] | None":
    if status is not None and status not in STATUSES:
        return ("invalid_status", "'status' must be one of "
                + ", ".join(STATUSES))
    if annotated_by and not _ANNOTATED_BY_RE.match(str(annotated_by)):
        return ("invalid_annotated_by",
                "'annotated_by' must be an id-shaped string")
    return None


def annotate(finding_id, *, status=None, note=None,
             annotated_by: str = "") -> "tuple[dict | None, tuple[str, str] | None]":
    """Record ONE steward annotation under the separate steward-scoped key.

    Reasons: ``unknown_finding`` | ``invalid_status`` | ``invalid_annotated_by``
    plus the ``untrusted_text`` reasons for the note (``invalid_input`` |
    ``too_long`` | ``secret_like_content`` | ``fence_delimiter``). No detail
    ever echoes the note. The generated scopes are not touched, so a later
    re-import leaves this row byte-identical.
    """
    if not isinstance(finding_id, str) or not _FINDING_ID_RE.match(finding_id):
        return None, ("unknown_finding", "no such finding")
    if not isinstance(_findings().get(finding_id), dict):
        return None, ("unknown_finding", "no such finding")
    problem = _check_annotation(status, annotated_by)
    if problem:
        return None, problem
    row = {"finding_id": finding_id, "annotated_at": _now_iso(),
           "annotated_by": str(annotated_by or ""),
           "status": status or "annotated"}
    if note is not None:
        cleaned = sanitize_untrusted(note, max_len=NOTE_MAX_CHARS)
        problem = validate_untrusted(cleaned, max_len=NOTE_MAX_CHARS,
                                     field="'note'", label="steward note")
        if problem:
            return None, problem
        if cleaned:
            row["steward_note"] = cleaned
    _guard(row, PERSISTED_STEWARD_KEYS, "steward")
    _steward()[steward_key(finding_id)] = row
    return row, None


def get_annotation(finding_id) -> "dict | None":
    """The steward annotation for one finding, or ``None``."""
    row = _steward().get(steward_key(finding_id))
    return row if isinstance(row, dict) else None


# ── read side (zero writes) ────────────────────────────────────────────────

def get_finding(finding_id) -> "dict | None":
    """One finding: the minimized row, its detail row IF still unexpired
    (``detail_expired`` otherwise — a NORMAL 7 d state, never an error), and
    the steward annotation. This is the ONLY place a ref-bearing row is
    joined with generated detail.
    """
    if not isinstance(finding_id, str) or not _FINDING_ID_RE.match(finding_id):
        return None      # never let a caller read the index item as a finding
    row = _findings().get(finding_id)
    if not isinstance(row, dict):
        return None
    out = dict(row)
    detail = _details().get(finding_id)
    out["detail"] = ({key: value for key, value in detail.items()
                      if key != "finding_id"}
                     if isinstance(detail, dict) else None)
    out["detail_expired"] = out["detail"] is None
    annotation = get_annotation(finding_id)
    out["steward"] = annotation
    if annotation and annotation.get("status") in STATUSES:
        # The steward's status wins; the generated row keeps saying "open" so a
        # re-import cannot clobber a steward decision.
        out["status"] = annotation["status"]
    return out


def list_findings() -> dict:
    """The ledger grouped by taxonomy — MINIMIZED index entries only, nothing
    resolved (refs stay refs until :func:`get_finding`). PERFORMS NO WRITES."""
    groups: dict = {taxonomy: [] for taxonomy in TAXONOMY}
    entries = _index_entries()
    for entry in entries:
        groups.setdefault(entry.get("taxonomy"), []).append(entry)
    return {"total": len(entries),
            "groups": {taxonomy: rows for taxonomy, rows in groups.items()
                       if taxonomy in TAXONOMY},
            "counts": {taxonomy: len(groups.get(taxonomy) or [])
                       for taxonomy in TAXONOMY}}


def _in_window(entry: dict, cutoff: str) -> bool:
    return str(entry.get("created_at") or "") >= cutoff


def weekly_digest(window_days: int = DEFAULT_DIGEST_WINDOW_DAYS) -> dict:
    """The "this week" digest, COMPUTED ON READ over the bounded index.

    Taxonomy counts plus a BOUNDED list of ux_gap REFS (≤
    :data:`UX_GAP_REF_LIMIT`, unresolved — resolution happens only when a
    steward expands one). There is deliberately NO clustering and NO digest
    writer: this function performs ZERO writes.
    """
    try:
        days = max(1, min(int(window_days), MAX_DIGEST_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_DIGEST_WINDOW_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    counts: dict = {}
    ux_gap_refs: list = []
    total = 0
    for entry in _index_entries():
        if not _in_window(entry, cutoff):
            continue
        total += 1
        taxonomy = entry.get("taxonomy")
        counts[taxonomy] = counts.get(taxonomy, 0) + 1
        if taxonomy == "ux_gap" and len(ux_gap_refs) < UX_GAP_REF_LIMIT:
            ux_gap_refs.append({"finding_id": entry.get("finding_id"),
                                "question_ref": entry.get("question_ref")})
    return {"window_days": days, "total": total,
            "taxonomy_counts": {taxonomy: counts.get(taxonomy, 0)
                                for taxonomy in TAXONOMY},
            "ux_gap_refs": ux_gap_refs,
            "ux_gap_refs_truncated": counts.get("ux_gap", 0) > len(ux_gap_refs)}


# ── writer telemetry read path (ADR-0052 / ADR-0062 D10) ───────────────────

def _telemetry_window(days) -> int:
    """The clamped trailing window (1 … :data:`MAX_TELEMETRY_WINDOW_DAYS`)."""
    try:
        return max(1, min(int(days), MAX_TELEMETRY_WINDOW_DAYS))
    except (TypeError, ValueError):
        return TELEMETRY_WINDOW_DAYS


def _ledger_entries(days: int) -> dict:
    """The REAL ledger, read through ``artifact_usage.read_artifact_usage``.

    The SAME entry point ``learning_health.usage_ledger_stats`` wraps (its
    60 s cache included) — there is deliberately no second aggregation
    contract. Lazy import (the ledger pulls the learning store) and
    swallowing: a telemetry read is evidence, never a dependency.
    """
    try:
        from .artifact_usage import read_artifact_usage
        return read_artifact_usage(days) or {}
    except Exception as exc:  # noqa: BLE001 - a telemetry read never raises
        logger.warning("writer telemetry read failed (non-critical): %s",
                       type(exc).__name__)
        return {}


def _empty_writer_slot(kind: str) -> dict:
    """The honest unfired slot: ``fired`` False, zero counters, no refs.

    A slot is always PRESENT (a writer that has not fired is a fact worth
    showing) and carries no measurement it does not have — no dollars, no
    estimates, no rates: the ledger counts uses.
    """
    return {"kind": kind, "fired": False, "artifacts": 0, "uses": 0,
            "last_used_at": None, "refs": [], "refs_truncated": False,
            "evidence_classes": []}


def _fold_writer_entry(slot: dict, ref: str, entry: dict) -> None:
    """Fold ONE ledger entry into its writer slot (in place). Refs only."""
    try:
        uses = int(entry.get("usage_30d", 0) or 0)
    except (TypeError, ValueError):
        uses = 0
    last_used = entry.get("last_used_at")
    slot["artifacts"] += 1
    slot["uses"] += uses
    slot["refs"].append({"ref": ref, "uses": uses,
                         "last_used_at": str(last_used) if last_used else None})
    if last_used and (slot["last_used_at"] is None
                      or str(last_used) > str(slot["last_used_at"])):
        slot["last_used_at"] = str(last_used)
    for evidence_class in (entry.get("evidence_classes") or []):
        if str(evidence_class) not in slot["evidence_classes"]:
            slot["evidence_classes"].append(str(evidence_class))


def _finish_writer_slot(slot: dict) -> None:
    """Order, bound and flag one folded slot (busiest ref first, id tiebreak)."""
    slot["refs"].sort(key=lambda item: (-item["uses"], item["ref"]))
    slot["refs_truncated"] = len(slot["refs"]) > TELEMETRY_REF_LIMIT
    slot["refs"] = slot["refs"][:TELEMETRY_REF_LIMIT]
    slot["evidence_classes"].sort()
    slot["fired"] = slot["artifacts"] > 0


def writer_telemetry(days: int = TELEMETRY_WINDOW_DAYS) -> dict:
    """Per-writer ledger evidence for the Evals surface — ZERO writes.

    THE ADR-0052 read path for this subsystem's writers: for every kind in
    :data:`TELEMETRY_WRITER_KINDS` it reports the recorded REFS (bounded to
    :data:`TELEMETRY_REF_LIMIT`, busiest first), how many uses the ledger
    counted, when the writer last fired, and the ledger's evidence classes.
    Refs are opaque ids (a sha-16 finding id, a mapping id, a run id) — no
    excerpt, rationale, mapping value or question text can reach this shape.

    An unfired writer is reported, not hidden: its slot reads ``fired:
    false`` with an empty ``refs`` list, which is exactly what makes
    "counters exist somewhere" unable to pass for "this writer fired".
    Counts are measured USES; there is no dollar, price or estimated figure
    in the shape (ADR-0053).

    Entries whose ledger row carries no ``evidence`` attribute fold under
    ``"tool-text"`` inside ``read_artifact_usage`` — the ADR-0052 (b) legacy
    default, surfaced as-is rather than reinterpreted here.
    """
    window = _telemetry_window(days)
    slots = {kind: _empty_writer_slot(kind) for kind in TELEMETRY_WRITER_KINDS}
    for artifact_id, entry in _ledger_entries(window).items():
        if not isinstance(entry, dict):
            continue
        slot = slots.get(str(entry.get("kind") or ""))
        if slot is None:
            continue          # another subsystem's ref kind — not ours to show
        _fold_writer_entry(slot, str(entry.get("artifact_id") or artifact_id),
                           entry)
    for slot in slots.values():
        _finish_writer_slot(slot)
    return {"window_days": window, "writers": slots}
