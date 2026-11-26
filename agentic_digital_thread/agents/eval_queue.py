# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Real-traffic review queue — COMPUTED ON READ — plus the steward decision
store (ADR-0062 Workstream A).

There is **no persisted queue**. :func:`compute_queue` joins its sources live
on every read; the only durable state this module writes is a STEWARD
DECISION, keyed by a stable source ref. That kills the queue-staleness class
outright (no refresh writer, no refresh job, no refresh route exists) at the
cost of one bounded read per call — cheap at demo volume against a TTL'd
feedback table.

Sources (both read-only):

* **Feedback** — ``storage.FeedbackStorage.get_by_time_range`` over a bounded
  UTC window (index-backed since the feedback-durability MR). Thumbs-down IS
  the flag, so negative rows are fetched (and ordered) FIRST; the remaining
  window is only read when negatives leave room under ``limit``. The stable
  id is the store's own ``feedback_id`` (``storage.feedback_id_for`` —
  a digest of session/message id), never a positional index.
* **Golden failures** — the ``verdict == "fail"`` rows of the newest
  ``_NEWEST_REPORTS`` persisted reports, read through ``eval_runs``' public
  read helpers (``list_runs`` / ``get_run``) so the report store stays owned
  by ONE module.

Pinned invariants (ADR-0062; tests in ``tests/unit/test_eval_queue.py``):

* **References only, never text.** Every persisted scalar path is in
  :data:`PERSISTED_DECISION_KEYS` / :data:`PERSISTED_INDEX_KEYS`. Question,
  answer and comment text is joined LIVE into the transient queue item and
  never reaches the store — pinned by a recursive leaf allowlist plus a
  sentinel scan over everything the store received (a renamed or nested leak
  fails both).
* **``compute_queue`` performs ZERO writes.** It is a pure read; the spy
  persistence seam in the tests asserts no put/delete during a read.
* **Machine pre-scores are triage-only.** :func:`machine_prescore` returns a
  deterministic signal-id list under the literal label
  :data:`PRESCORE_LABEL` (``"triage-only"``). No machine code path can set a
  decision: :func:`decide` is the ONLY writer in this module and it is
  reachable only from the steward route.
* **Synthetic traffic is excluded.** Feedback whose ``session_id`` matches ANY
  prefix in ``runtime_app._SYNTHETIC_SESSION_PREFIXES`` is dropped — the tuple
  is ITERATED at call time (never re-listed here), so a fourth prefix
  auto-extends the filter and its pin. Golden-failure rows are deliberately
  NOT filtered: those sessions are ``eval-run-`` by construction and the
  golden suite's own failures are a first-class queue source.
* **Steward notes are untrusted text.** ``untrusted_text.sanitize_untrusted``
  + ``validate_untrusted`` (bounded, secret-shape rejected); a rejection
  detail NEVER echoes the note.
* **Dangling sources are honest.** Feedback rows expire (90 d TTL) and reports
  age out of the newest-N window, so a decided item whose source no longer
  resolves is returned with ``source_expired: True`` and no text — never an
  error, never silently dropped.

Import-pure module (``catalog.py`` discipline): no boto3 at import; the stores
are lazy module-level accessors with a :func:`_reset_state` test seam
(``eval_runs.py`` idioms throughout).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from .untrusted_text import sanitize_untrusted, validate_untrusted

logger = logging.getLogger(__name__)

# ── source kinds + verdicts (pinned) ───────────────────────────────────────
SOURCE_FEEDBACK = "feedback"
SOURCE_GOLDEN_FAILURE = "golden-failure"

#: Steward verdict enum — the route pins the same three (422 otherwise).
VERDICTS = ("correct", "incorrect", "needs_context")

#: The literal label every machine pre-score carries. Machine signal lists are
#: TRIAGE HINTS, never verdicts (ADR-0062).
PRESCORE_LABEL = "triage-only"

# ── bounds (do not relax without an ADR) ───────────────────────────────────
DECISION_TTL_SECONDS = 180 * 24 * 3600  # steward decisions: 180 d
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90                    # the feedback row TTL horizon
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
NOTE_MAX_CHARS = 500                    # bounded steward note
MAX_TAGS = 8
TAG_MAX_CHARS = 40
DECIDED_BY_MAX_CHARS = 128
_NEWEST_REPORTS = 3                     # newest-N golden reports scanned
_MAX_INDEX_DECISIONS = 500              # bounded decided-ref index
_MAX_FAILURES = 3                       # transient failure lines per item
_FAILURE_CHARS = 200
_QUESTION_CHARS = 400                   # transient display excerpts
_ANSWER_CHARS = 600
_COMMENT_CHARS = 500
_MAX_CITED_SYSTEMS = 12

_DECISION_KEY_PREFIX = "dec-"
_INDEX_KEY = "index"                    # the replace-not-accrete ref index

#: RECURSIVE leaf allowlist for a persisted decision row. Anything not listed
#: here must never be written — the test walks every persisted scalar path
#: against this set, so a renamed or nested text leak fails regardless of what
#: it is called.
PERSISTED_DECISION_KEYS = frozenset({
    "source_ref", "source", "ref_sha", "verdict", "taxonomy_tags",
    "steward_note", "decided_at", "decided_by",
})

#: Same contract for the bounded index item (refs + provenance only).
PERSISTED_INDEX_KEYS = frozenset({
    "decisions", "source_ref", "source", "ref_sha", "decided_at",
})

# Source-ref shapes. These are the anti-smuggling gate: a client-supplied
# ``source_ref`` that is not one of these two shapes is REJECTED, so arbitrary
# customer text can never be persisted in the guise of a reference.
_FEEDBACK_REF_RE = re.compile(r"^feedback:fb-[0-9a-f]{32}$")
_GOLDEN_REF_RE = re.compile(
    r"^golden-failure:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_.-]{1,80}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,%d}$" % (TAG_MAX_CHARS - 1))
_DECIDED_BY_RE = re.compile(r"^[A-Za-z0-9_.:@|-]{1,%d}$" % DECIDED_BY_MAX_CHARS)

_DECISIONS = None  # lazy JobStore("eval-decision")
_FEEDBACK = None   # lazy FeedbackStorage


def _decisions():
    """The steward-decision store (lazy — ``job_store`` transitively imports
    boto3-importing modules). Tests inject ``JobStore(..., table=None)`` (or a
    spy wrapper) via the module attribute."""
    global _DECISIONS
    if _DECISIONS is None:
        from .job_store import JobStore
        _DECISIONS = JobStore("eval-decision",
                              ttl_seconds=DECISION_TTL_SECONDS)
    return _DECISIONS


def _feedback():
    """The feedback source store (lazy — ``storage`` imports boto3)."""
    global _FEEDBACK
    if _FEEDBACK is None:
        from .config import get_config
        from .storage import FeedbackStorage
        cfg = get_config()
        _FEEDBACK = FeedbackStorage(cfg.feedback_table_name, cfg.aws_region)
    return _FEEDBACK


def _reset_state() -> None:
    """Test seam: drop both handles (fresh stores per test)."""
    global _DECISIONS, _FEEDBACK
    _DECISIONS = None
    _FEEDBACK = None


# ── source refs (stable, opaque-hashable) ──────────────────────────────────

def feedback_source_ref(feedback_id) -> str:
    """``feedback:<feedback_id>`` — the store's own stable id (see
    ``storage.feedback_id_for``), never a positional queue index."""
    return f"{SOURCE_FEEDBACK}:{feedback_id}"


def golden_failure_source_ref(run_id, question_id) -> str:
    """``golden-failure:<run_id>:<question_id>``."""
    return f"{SOURCE_GOLDEN_FAILURE}:{run_id}:{question_id}"


def ref_sha(source_ref) -> str:
    """The sha-16 OPAQUE form of a source ref — the only form that may appear
    in an exported/downloaded artifact (the raw ref stays server-side)."""
    return hashlib.sha256(str(source_ref).encode("utf-8")).hexdigest()[:16]


def decision_key(source_ref) -> str:
    """JobStore key for one decided source (stable across recomputation)."""
    return f"{_DECISION_KEY_PREFIX}{ref_sha(source_ref)}"


def source_of(source_ref) -> "str | None":
    """The source kind for a WELL-SHAPED ref, else ``None``.

    Shape validation is the gate that keeps free text out of persistence: only
    the two pinned shapes above are recognized.
    """
    if not isinstance(source_ref, str):
        return None
    if _FEEDBACK_REF_RE.match(source_ref):
        return SOURCE_FEEDBACK
    if _GOLDEN_REF_RE.match(source_ref):
        return SOURCE_GOLDEN_FAILURE
    return None


# ── deterministic machine pre-score (triage-only) ──────────────────────────

def machine_prescore(source: str, facts: dict) -> dict:
    """Deterministic triage signal ids for one queue item.

    Signals are stable ids in sorted order — computed, never LLM-judged — and
    the block is labelled :data:`PRESCORE_LABEL`. It carries NO verdict and
    no score: the machine ranks nothing and decides nothing.
    """
    signals: list[str] = []
    if source == SOURCE_FEEDBACK:
        if facts.get("sentiment") == "negative":
            signals.append("user-flagged-negative")
        if facts.get("has_comment"):
            signals.append("user-comment-present")
        if not facts.get("cited_systems"):
            signals.append("no-cited-systems")
        if not facts.get("cited_rules"):
            signals.append("no-cited-rules")
    elif source == SOURCE_GOLDEN_FAILURE:
        signals.append("golden-verdict-fail")
        if facts.get("transport_failure"):
            signals.append("transport-failure")
        if facts.get("fabrication_flags"):
            signals.append("fabrication-flagged")
        if not facts.get("systems_queried"):
            signals.append("no-systems-queried")
        if not facts.get("neptune_queries"):
            signals.append("no-neptune-query")
    return {"label": PRESCORE_LABEL, "signals": sorted(signals)}


# ── read side: the computed queue ──────────────────────────────────────────

def _clamp(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _excerpt(value, limit: int) -> str:
    """Bounded TRANSIENT display text (never persisted)."""
    return str(value or "").strip()[:limit]


def _iso(value) -> "str | None":
    """ISO-8601 UTC for an epoch float or a passthrough for an ISO string."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    text = str(value or "").strip()
    return text or None


def _synthetic_prefixes() -> tuple:
    """The LIVE synthetic-session prefix tuple, read at call time.

    Imported lazily (``runtime_app`` is the AgentCore entry point — importing
    it at module scope would break import purity) and never re-listed here, so
    a new prefix extends this filter with no edit.
    """
    from . import runtime_app
    prefixes = getattr(runtime_app, "_SYNTHETIC_SESSION_PREFIXES", ())
    return tuple(prefixes or ())


def _is_synthetic(session_id) -> bool:
    return any(str(session_id or "").startswith(prefix)
               for prefix in _synthetic_prefixes())


def _feedback_id_for(session_id, message_id) -> str:
    from .storage import feedback_id_for
    return feedback_id_for(session_id, message_id)


def _feedback_rows(window_days: int, limit: int) -> list:
    """Bounded feedback window, negative rows FIRST (thumbs-down IS the flag).

    The remaining window is only read when the negatives leave room under
    ``limit`` — one extra index-backed read at most, skipped entirely on a
    busy window.
    """
    store = _feedback()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    def _newest_first(rows) -> list:
        return sorted([row for row in (rows or []) if isinstance(row, dict)],
                      key=lambda row: str(row.get("timestamp") or ""),
                      reverse=True)

    negatives = _newest_first(
        store.get_by_time_range(start_iso, end_iso, "negative"))
    rows = negatives[:limit]
    if len(rows) >= limit:
        return rows
    seen = {row.get("feedback_id") for row in rows}
    for row in _newest_first(store.get_by_time_range(start_iso, end_iso)):
        if row.get("feedback_id") in seen:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _feedback_items(window_days: int, limit: int) -> list:
    items = []
    for row in _feedback_rows(window_days, limit):
        session_id = row.get("session_id")
        if _is_synthetic(session_id):
            continue
        feedback_id = row.get("feedback_id") or _feedback_id_for(
            session_id, row.get("message_id"))
        source_ref = feedback_source_ref(feedback_id)
        if source_of(source_ref) is None:
            logger.debug("eval_queue_skipped_feedback_row: unrecognized id shape")
            continue
        cited = [str(system)[:120] for system in (row.get("cited_systems") or [])
                 if isinstance(system, str)][:_MAX_CITED_SYSTEMS]
        comment = _excerpt(row.get("user_comment"), _COMMENT_CHARS)
        items.append({
            "source_ref": source_ref,
            "ref_sha": ref_sha(source_ref),
            "source": SOURCE_FEEDBACK,
            "occurred_at": _iso(row.get("timestamp")),
            "sentiment": row.get("sentiment"),
            # Live-joined display text — TRANSIENT, never persisted.
            "question": _excerpt(row.get("user_message"), _QUESTION_CHARS),
            "answer_excerpt": _excerpt(row.get("assistant_response"),
                                       _ANSWER_CHARS),
            "user_comment": comment,
            "cited_systems": cited,
            "machine_prescore": machine_prescore(SOURCE_FEEDBACK, {
                "sentiment": row.get("sentiment"),
                "has_comment": bool(comment),
                "cited_systems": cited,
                "cited_rules": row.get("cited_rules") or [],
            }),
            "source_expired": False,
        })
    return items


def _golden_failure_item(run_id: str, row: dict, occurred_at,
                         run_path) -> dict:
    """One queue item from a failed golden row (display text TRANSIENT)."""
    source_ref = golden_failure_source_ref(run_id, row.get("id"))
    honesty = row.get("honesty") if isinstance(row.get("honesty"), dict) else {}
    return {
        "source_ref": source_ref,
        "ref_sha": ref_sha(source_ref),
        "source": SOURCE_GOLDEN_FAILURE,
        "occurred_at": occurred_at,
        "run_id": run_id,
        "question_id": row.get("id"),
        "run_path": run_path,
        "answer_excerpt": _excerpt(row.get("answer_excerpt"), _ANSWER_CHARS),
        "failures": [_excerpt(failure, _FAILURE_CHARS)
                     for failure in (row.get("failures") or [])
                     ][:_MAX_FAILURES],
        "machine_prescore": machine_prescore(SOURCE_GOLDEN_FAILURE, {
            "transport_failure": bool(row.get("transport_failure")),
            "fabrication_flags": honesty.get("fabrication_flags") or [],
            "systems_queried": row.get("systems_queried") or 0,
            "neptune_queries": row.get("neptune_queries") or 0,
        }),
        "source_expired": False,
    }


def _golden_failure_items(limit: int) -> list:
    """Failed rows from the newest-N persisted golden reports.

    Read through ``eval_runs``' public read helpers so the ``eval-report``
    store keeps exactly one owner; ``get_run`` still resolves a completed run
    whose short-TTL status record has expired.
    """
    from . import eval_runs
    items = []
    for summary in (eval_runs.list_runs().get("runs") or [])[:_NEWEST_REPORTS]:
        if not isinstance(summary, dict):
            continue
        run_id = summary.get("run_id")
        if not isinstance(run_id, str):
            continue
        report = (eval_runs.get_run(run_id) or {}).get("report")
        if not isinstance(report, dict):
            continue
        occurred_at = _iso(summary.get("finished_at")
                           or summary.get("started_at"))
        for row in report.get("questions") or []:
            if not isinstance(row, dict) or row.get("verdict") != "fail":
                continue
            item = _golden_failure_item(run_id, row, occurred_at,
                                        report.get("run_path"))
            if source_of(item["source_ref"]) is None:
                logger.debug(
                    "eval_queue_skipped_report_row: unrecognized ref shape")
                continue
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def _band(item: dict) -> int:
    """Deterministic band: flagged feedback, then the rest of the window,
    then golden failures."""
    if item.get("source") == SOURCE_FEEDBACK:
        return 0 if item.get("sentiment") == "negative" else 1
    return 2


def _sort_items(items: list) -> None:
    """Band ascending, newest first inside each band (stable two-pass)."""
    items.sort(key=lambda item: (str(item.get("occurred_at") or ""),
                                 item.get("ref_sha") or ""), reverse=True)
    items.sort(key=_band)


def _public_decision(row: dict) -> dict:
    """The decision block as the queue renders it (persisted keys only)."""
    return {key: row[key] for key in
            ("verdict", "taxonomy_tags", "steward_note", "decided_at",
             "decided_by", "ref_sha")
            if key in row}


def _expired_items(known_refs: set, limit: int) -> list:
    """Decided sources that no longer resolve in the current read.

    Feedback rows expire (90 d TTL) and reports age out of the newest-N
    window, so this is a NORMAL state, not an error: the item is returned with
    its steward decision, no text, and ``source_expired: True``.

    The decision rows are read in ONE batch (``_decision_rows``) — this used
    to be one remote read per index entry (up to 500) on the request path.
    """
    items = []
    entries = [entry for entry in list_decisions().get("decisions") or []
               if isinstance(entry, dict)
               and isinstance(entry.get("source_ref"), str)
               and entry["source_ref"] not in known_refs]
    rows = _decision_rows([entry["source_ref"] for entry in entries])
    for entry in entries:
        source_ref = entry["source_ref"]
        row = rows.get(source_ref)
        if not isinstance(row, dict):
            continue
        items.append({
            "source_ref": source_ref,
            "ref_sha": entry.get("ref_sha") or ref_sha(source_ref),
            "source": entry.get("source") or source_of(source_ref),
            "occurred_at": None,
            "question": None,
            "answer_excerpt": None,
            "machine_prescore": {"label": PRESCORE_LABEL, "signals": []},
            "decision": _public_decision(row),
            "source_expired": True,
        })
        if len(items) >= limit:
            break
    return items


def compute_queue(window_days: int = DEFAULT_WINDOW_DAYS,
                  limit: int = DEFAULT_LIMIT) -> dict:
    """The review queue, computed fresh on every call. PERFORMS NO WRITES.

    Returns ``{items, window_days, limit, counts}`` where ``counts`` carries
    ``{total, decided, undecided, source_expired}``. Display text is joined
    live from the sources into each (transient) item; only the steward
    decision block comes from storage.
    """
    window_days = _clamp(window_days, 1, MAX_WINDOW_DAYS, DEFAULT_WINDOW_DAYS)
    limit = _clamp(limit, 1, MAX_LIMIT, DEFAULT_LIMIT)
    items = _feedback_items(window_days, limit) + _golden_failure_items(limit)
    _sort_items(items)
    items = items[:limit]
    rows = _decision_rows([item["source_ref"] for item in items])
    for item in items:
        row = rows.get(item["source_ref"])
        item["decision"] = _public_decision(row) if isinstance(row, dict) \
            else None
    items.extend(_expired_items({item["source_ref"] for item in items}, limit))
    decided = sum(1 for item in items if item.get("decision"))
    return {
        "items": items,
        "window_days": window_days,
        "limit": limit,
        "counts": {
            "total": len(items),
            "decided": decided,
            "undecided": len(items) - decided,
            "source_expired": sum(1 for item in items
                                  if item.get("source_expired")),
        },
    }


# ── write side: steward decisions (the ONLY writer in this module) ─────────

def _clean_tags(tags) -> "tuple[list, tuple[str, str] | None]":
    """Bounded id-shaped taxonomy tags, or a pinned rejection reason."""
    if tags is None:
        return [], None
    if not isinstance(tags, (list, tuple)):
        return [], ("invalid_tags", "'taxonomy_tags' must be a list of tags")
    if len(tags) > MAX_TAGS:
        return [], ("invalid_tags",
                    f"'taxonomy_tags' accepts at most {MAX_TAGS} tags")
    cleaned = []
    for tag in tags:
        if not isinstance(tag, str) or not _TAG_RE.match(tag):
            return [], ("invalid_tags",
                        "each tag must be a lowercase id "
                        f"(letters, digits, '-', '_'; max {TAG_MAX_CHARS} chars)")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned, None


def _update_index(row: dict) -> None:
    """Replace-not-accrete bounded index of DECIDED refs (refs + provenance
    only — the shape ``JobStore`` cannot enumerate on its own, and what makes
    an expired source visible)."""
    store = _decisions()
    existing = store.get(_INDEX_KEY)
    entries = list((existing or {}).get("decisions") or []) \
        if isinstance(existing, dict) else []
    entry = {"source_ref": row["source_ref"], "source": row["source"],
             "ref_sha": row["ref_sha"], "decided_at": row["decided_at"]}
    entries = [item for item in entries if isinstance(item, dict)
               and item.get("source_ref") != entry["source_ref"]]
    entries.insert(0, entry)
    store[_INDEX_KEY] = {"decisions": entries[:_MAX_INDEX_DECISIONS]}


def _clean_note(note) -> "tuple[str, tuple[str, str] | None]":
    """The steward note under the ``untrusted_text`` treatment: sanitized,
    bounded, secret-shape and fence-delimiter rejected. A rejection detail
    NEVER echoes the note."""
    cleaned = sanitize_untrusted(note, max_len=NOTE_MAX_CHARS)
    problem = validate_untrusted(cleaned, max_len=NOTE_MAX_CHARS,
                                 field="'note'", label="steward note")
    if problem:
        return "", problem
    return cleaned, None


def _validated_decision_inputs(source_ref, verdict, taxonomy_tags, decided_by):
    """``(source, tags, None)`` for accepted inputs, else
    ``(None, [], (reason, detail))`` with a pinned reason."""
    source = source_of(source_ref)
    if source is None:
        return None, [], ("invalid_source_ref",
                          "'source_ref' is not a recognized source reference")
    if verdict not in VERDICTS:
        return None, [], ("invalid_verdict",
                          "'verdict' must be one of " + ", ".join(VERDICTS))
    tags, problem = _clean_tags(taxonomy_tags)
    if problem:
        return None, [], problem
    if decided_by and not _DECIDED_BY_RE.match(str(decided_by)):
        return None, [], ("invalid_decided_by",
                          "'decided_by' must be an id-shaped string")
    return source, tags, None


def decide(source_ref, verdict, taxonomy_tags=None, note=None,
           decided_by: str = "") -> "tuple[dict | None, tuple[str, str] | None]":
    """Record ONE steward decision; returns ``(row, None)`` or
    ``(None, (reason, detail))`` with pinned reasons.

    Reasons: ``invalid_source_ref`` | ``invalid_verdict`` | ``invalid_tags`` |
    ``invalid_decided_by`` plus the ``untrusted_text`` reasons for the note
    (``invalid_input`` | ``too_long`` | ``secret_like_content`` |
    ``fence_delimiter``). No ``detail`` ever echoes the note.

    This is the ONLY function in this module that writes. Nothing on the
    machine path (queue computation, pre-scoring) can reach it.
    """
    source, tags, problem = _validated_decision_inputs(
        source_ref, verdict, taxonomy_tags, decided_by)
    if problem:
        return None, problem
    row = {
        "source_ref": source_ref,
        "source": source,
        "ref_sha": ref_sha(source_ref),
        "verdict": verdict,
        "taxonomy_tags": tags,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by": str(decided_by or ""),
    }
    if note is not None:
        cleaned, problem = _clean_note(note)
        if problem:
            return None, problem
        if cleaned:
            row["steward_note"] = cleaned
    leaked = _unexpected_leaves(row, PERSISTED_DECISION_KEYS)
    if leaked:
        # Defensive: a future edit adding a field must extend the pinned
        # allowlist (and the ADR) rather than silently persisting it.
        raise ValueError(
            f"decision row carries non-allowlisted keys: {sorted(leaked)}")
    _decisions()[decision_key(source_ref)] = row
    _update_index(row)
    return row, None


def _unexpected_leaves(payload, allowlist) -> set:
    """Every leaf KEY reachable in ``payload`` that is not allowlisted
    (recursive — a nested or renamed field is caught regardless of depth)."""
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


# ── read helpers ───────────────────────────────────────────────────────────

def get_decision(source_ref) -> "dict | None":
    """The steward decision for one source ref, or ``None``."""
    if source_of(source_ref) is None:
        return None
    row = _decisions().get(decision_key(source_ref))
    return row if isinstance(row, dict) else None


def _decision_rows(source_refs: list) -> dict:
    """``{source_ref: row}`` for every ref that has a decision — ONE batched
    read (``JobStore.get_many``) instead of a remote read per ref. A store
    without ``get_many`` (a bare dict-like test double) falls back to
    per-ref ``get``."""
    refs = [ref for ref in dict.fromkeys(source_refs)
            if isinstance(ref, str) and source_of(ref) is not None]
    if not refs:
        return {}
    store = _decisions()
    keys = {decision_key(ref): ref for ref in refs}
    get_many = getattr(store, "get_many", None)
    if callable(get_many):
        found = get_many(list(keys))
    else:
        found = {key: store.get(key) for key in keys}
    return {keys[key]: row for key, row in found.items()
            if key in keys and isinstance(row, dict)}


def list_decisions() -> dict:
    """The bounded newest-first index of decided refs (refs only)."""
    index = _decisions().get(_INDEX_KEY)
    entries = list((index or {}).get("decisions") or []) \
        if isinstance(index, dict) else []
    return {"decisions": entries[:_MAX_INDEX_DECISIONS]}
