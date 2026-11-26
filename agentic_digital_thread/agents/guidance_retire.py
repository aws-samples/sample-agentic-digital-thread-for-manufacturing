# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward retire-a-note-by-digest over the EXISTING prune saga (ADR-0063 D7).

READ path — :func:`list_digests`: every learned note on a system's graph
``query_guidance`` as ``{digest, scope, source, preview, created_at,
delivered}`` plus the opaque ``current_digest`` authority token the retire
must echo, and ``retirements_30d`` (the ADR-0052 read path of the ledger
telemetry below). Read-only: graph read, delivery snapshot, accepted-row
provenance read, usage-ledger read.

WRITE path — :func:`retire_by_digest`: validates the 12-hex
``guidance_scope.content_digest``, refuses a stale ``expected_current_digest``
(409), resolves the digest to exactly ONE note text (0 → 404, distinct texts
sharing a digest → 409), then DELEGATES to
``self_learning.prune_query_guidance_note`` — the SAME graph CAS + delivery
``replace_exact`` saga the text-keyed prune uses, so there is no second
writer; the saga owns compensation and reports ``pending`` + drift on
failure (passed through as 409). A ``pruned`` result is then proven by an
exact readback of BOTH the graph and the delivery store; anything but
``absent`` on both is a 409 ``readback_mismatch`` with no retry.

Telemetry: exactly one bounded ``[GUIDANCE] retire`` log line per call
(digests and bounded status words only — NEVER note text) and one
day-bucketed usage-ledger bump per success (followed by a drop of the
ledger's 60 s in-process read cache, so the steward's next GET counts it).
Telemetry proves the retirement
fired; it says nothing about model behaviour. Generic: no dataset knowledge.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DIGEST_PATTERN = r"^[0-9a-f]{12}$"
CURRENT_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
PREVIEW_CHARS = 80
USAGE_KIND = "guidance_retire"
USAGE_EVIDENCE = "guidance-retire"
_LOG_FIELD_CAP = 64
_DIGEST_RE = re.compile(DIGEST_PATTERN)


def usage_artifact_id(system_id: str) -> str:
    """Ledger artifact id for one system's retirements (kind-qualified)."""
    return f"{USAGE_KIND}:{system_id}"


def _refusal(http_status: int, error: str, **extra) -> dict:
    return {"http_status": http_status, "error": error, **extra}


def _graph_text(system_id: str) -> str | None:
    from . import self_learning
    return self_learning._read_query_guidance_strict(system_id)


def _note_digests(document, *, plain_is_base: bool = True) -> set[str]:
    from . import guidance_scope
    return {guidance_scope.content_digest(text) for _scope, text
            in guidance_scope.scoped_entries(document, plain_is_base=plain_is_base)}


def _delivered_digests(system_id: str) -> set[str] | None:
    """Digests in the agent delivery store; ``None`` when it cannot be read
    (logged at warning with the exception TYPE only — never its message)."""
    from . import system_guidance
    try:
        snap = system_guidance.snapshot(system_id)
    except Exception as exc:  # noqa: BLE001 - honest unavailable, never a guess
        logger.warning("guidance retire: delivery store unreadable (%s)",
                       type(exc).__name__)
        return None
    return _note_digests(snap.text, plain_is_base=False)


def _provenance(system_id: str) -> dict[str, dict]:
    """Accepted ``guidance_note`` rows keyed by the note's content digest."""
    from . import guidance_scope, self_learning
    out: dict[str, dict] = {}
    for row in self_learning.get_accepted_for_system(system_id) or []:
        text = str(row.get("to_value") or "").strip()
        if row.get("proposed_type") == "guidance_note" and text:
            out.setdefault(guidance_scope.content_digest(text), row)
    return out


def _retirements_30d(system_id: str) -> int:
    from . import artifact_usage
    entry = (artifact_usage.read_artifact_usage(30) or {}).get(
        usage_artifact_id(system_id)) or {}
    try:
        return int(entry.get("usage_30d", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _note_entry(scope, text: str, delivered: set[str] | None,
                provenance: dict[str, dict]) -> dict:
    from . import guidance_scope
    digest = guidance_scope.content_digest(text)
    row = provenance.get(digest) or {}
    return {"digest": digest, "scope": scope.value,
            "source": row.get("source") or None,
            "preview": text[:PREVIEW_CHARS],
            "created_at": row.get("accepted_at") or row.get("discovered_at") or None,
            "delivered": None if delivered is None else digest in delivered}


def list_digests(system_id: str) -> dict:
    """READ-ONLY per-note digest listing for one system (404 when absent)."""
    from . import guidance_scope
    from .guidance_acceptance import opaque_digest
    current = _graph_text(system_id)
    if current is None:
        return _refusal(404, "system not found")
    delivered = _delivered_digests(system_id)
    provenance = _provenance(system_id)
    notes = [_note_entry(scope, text, delivered, provenance)
             for scope, text in guidance_scope.scoped_entries(current)]
    return {"system_id": system_id, "current_digest": opaque_digest(current),
            "notes": notes, "count": len(notes),
            "retirements_30d": _retirements_30d(system_id)}


def _resolve_text(current: str, digest: str) -> tuple[str | None, dict | None]:
    from . import guidance_scope
    texts = {text for _scope, text in guidance_scope.scoped_entries(current)
             if guidance_scope.content_digest(text) == digest}
    if not texts:
        return None, _refusal(404, "note not found")
    if len(texts) > 1:
        return None, _refusal(409, "ambiguous_digest")
    return texts.pop(), None


def _saga_failure(system_id: str, saga: dict) -> dict:
    """HTTP status from the saga's EXPLICIT reply fields, never an error-text
    substring: ``status: pending`` ⇒ 409; the note-not-found reply (an
    explicit ``removed: 0``) or the exact system-not-found reply ⇒ 404 (a
    race with a concurrent writer after our own read); anything else ⇒ 400."""
    if saga.get("status") == "pending":
        return {"http_status": 409, **saga}
    vanished = saga.get("removed", None) == 0 or (
        saga.get("error") == f"System {system_id} not found")
    return {"http_status": 404 if vanished else 400, **saga}


def _readback(system_id: str, digest: str) -> tuple[dict, str | None]:
    graph = _graph_text(system_id)
    delivered = _delivered_digests(system_id)
    state = {
        "graph": ("unavailable" if graph is None else
                  "present" if digest in _note_digests(graph) else "absent"),
        "delivery": ("unavailable" if delivered is None else
                     "present" if digest in delivered else "absent"),
    }
    return state, graph


def _prove(system_id: str, digest: str, saga: dict) -> dict:
    from .guidance_acceptance import opaque_digest
    state, graph = _readback(system_id, digest)
    if state != {"graph": "absent", "delivery": "absent"}:
        return {"http_status": 409, "status": "readback_mismatch",
                "error": "readback_mismatch", "digest": digest,
                "readback": state}
    return {"status": "retired", "digest": digest,
            "removed": int(saga.get("removed", 0) or 0),
            "remaining": int(saga.get("remaining", 0) or 0),
            "current_digest": opaque_digest(graph), "readback": state}


def _retire(system_id: str, digest: str, expected_current_digest: str) -> dict:
    """Validate, check freshness, resolve, then delegate to the prune saga.

    The ``stale_digest`` check is BEST-EFFORT: it compares against this read
    of the graph, and the saga re-reads the graph itself, so a concurrent
    write landing between the two is not refused here. Correctness does not
    rest on it — the saga removes only the exact resolved note text under its
    own graph CAS + delivery ``replace_exact`` (a vanished note is a 404, a
    racing writer a ``pending`` 409), and :func:`_prove` reads both back."""
    from . import self_learning
    from .guidance_acceptance import opaque_digest
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return _refusal(422, "invalid_digest")
    current = _graph_text(system_id)
    if current is None:
        return _refusal(404, "system not found")
    if expected_current_digest != opaque_digest(current):
        return _refusal(409, "stale_digest", current_digest=opaque_digest(current))
    text, refusal = _resolve_text(current, digest)
    if refusal:
        return refusal
    saga = self_learning.prune_query_guidance_note(system_id, text)
    if saga.get("status") != "pruned":
        return _saga_failure(system_id, saga)
    return _prove(system_id, digest, saga)


def _bounded(value) -> str:
    return "".join(ch if ch.isprintable() else "?"
                   for ch in str(value or ""))[:_LOG_FIELD_CAP]


def _record(system_id: str, digest, result: dict) -> None:
    status = result.get("status") or result.get("error") or "unknown"
    removed = int(result.get("removed", 0) or 0) if status == "retired" else 0
    shown = digest if isinstance(digest, str) and _DIGEST_RE.match(digest) else "invalid"
    logger.info("[GUIDANCE] retire system=%s digest=%s status=%s removed=%d",
                _bounded(system_id), shown, _bounded(status), removed)
    if status != "retired":
        return
    try:
        from . import artifact_usage
        bumped = artifact_usage.bump_artifact_usage(
            {(USAGE_KIND, usage_artifact_id(system_id))}, evidence=USAGE_EVIDENCE)
        if bumped:
            # The ledger read is a 60 s in-process cache; the steward's GET
            # that fetched ``current_digest`` for THIS retire primed it, so
            # without this the follow-up GET reports the pre-retire count.
            artifact_usage._reset_read_cache()
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the retire
        logger.warning("guidance retire usage bump failed: %s", type(exc).__name__)


def retire_by_digest(system_id: str, digest: str,
                     expected_current_digest: str) -> dict:
    """Retire ONE note by digest through the existing prune saga.

    Returns the response body; a non-200 outcome carries ``http_status``.
    """
    result = _retire(system_id, digest, expected_current_digest)
    _record(system_id, digest, result)
    return result
