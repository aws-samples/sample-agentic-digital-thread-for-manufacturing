# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Notification feed — READ-ONLY aggregation of what needs steward attention.

One endpoint, ``GET /api/notifications``, consumed by the header bell in the
main app. v1 scope is read-aggregation only: NO new writers, no per-user
read-state, no persistence — every item is derived from EXISTING state:

* pending learning proposals (the same ``self_learning.get_all_mappings``
  rows the Vocabulary tab's review reads — counted, never mutated);
* pending runtime steward questions (``steward_question`` rows filed by the
  failure-signal sweep, ADR-0059 D5 — counted as ONE item, disjoint from the
  proposals count so a question never notifies twice);
* in-flight async jobs: per-system removals (``onboard._RESET_JOBS``) and
  graph snapshot import / reconcile-repair jobs
  (``graph_snapshot_routes._IMPORT_JOBS`` / ``_REPAIR_JOBS``);
* recently finished jobs (``done`` / ``error`` entries still in those
  stores) so "it finished" is visible without watching a page.

Auth follows the sibling non-admin routes (e.g. ``/api/proposed-mappings``):
the app-level AuthMiddleware authenticates every ``/api`` request; there is
deliberately NO admin gate — any signed-in user may see the feed. Each
``href`` points at the EXISTING page that handles the item; this module adds
no new surface for acting on anything.

Fail-soft per source: one source raising must never empty the whole feed —
the failed source contributes a ``{"kind", "error"}`` item and the other
sources' items are returned intact.

Job-store enumeration note: ``JobStore`` exposes no listing surface, so the
removal scan enumerates the process-local ``_mem`` cache plus the
cross-replica ``__active_reset__`` marker. Recently-finished entries written
by ANOTHER replica are therefore best-effort in v1; running removals are
always visible through the marker.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from .public_errors import public_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["notifications"])

#: href targets — the EXISTING pages that handle each item kind.
_HREF_PROPOSALS = "/admin.html#tab-vocabulary"
_HREF_ONBOARD = "/#onboard"
_HREF_ADMIN = "/admin.html"
#: The runtime-question answering surface (ADR-0059 D5 — Wave 3A frontend).
_HREF_QUESTIONS = "/#onboard/questions"

#: Job states the feed surfaces (anything else is ignored, e.g. markers).
_SURFACED_STATES = ("running", "done", "error")


def _store_keys(store) -> set:
    """Enumerable keys of a JobStore (its ``_mem`` cache) or a plain dict."""
    mem = getattr(store, "_mem", None)
    if isinstance(mem, dict):
        return set(mem)
    if isinstance(store, dict):
        return set(store)
    return set()


def _proposal_items() -> list[dict]:
    """One 'proposals' item with the pending-review count (absent when 0).

    ``steward_question`` rows are EXCLUDED: they are queue entries the
    ``questions`` source counts separately — the bell must distinguish "the
    system has questions for you" from "proposals await review" (ADR-0059
    D3/D5), and one row must never notify twice."""
    from .self_learning import get_all_mappings
    rows = get_all_mappings() or []
    pending = sum(1 for r in rows
                  if isinstance(r, dict) and r.get("status") == "pending"
                  and r.get("proposed_type") != "steward_question")
    if pending == 0:
        return []
    noun = "proposal" if pending == 1 else "proposals"
    return [{
        "kind": "proposals",
        "label": f"{pending} learning {noun} awaiting review",
        "count": pending,
        "href": _HREF_PROPOSALS,
    }]


def _question_items() -> list[dict]:
    """ONE 'questions' item counting pending runtime steward questions
    (ADR-0059 D5): one notification regardless of N, absent when 0, linking
    to the answering surface — never to the vocabulary review queue."""
    from .self_learning import get_all_mappings
    rows = get_all_mappings() or []
    pending = sum(1 for r in rows
                  if isinstance(r, dict) and r.get("status") == "pending"
                  and r.get("proposed_type") == "steward_question")
    if pending == 0:
        return []
    return [{
        "kind": "questions",
        "label": f"The system has {pending} question(s) for you",
        "count": pending,
        "href": _HREF_QUESTIONS,
    }]


def _removal_items() -> list[dict]:
    """Per-system removal (offboard reset) jobs — running and finished."""
    from . import onboard
    store = onboard._RESET_JOBS
    marker_key = onboard._ACTIVE_RESET_KEY
    keys = _store_keys(store)
    # The cross-replica marker names the system whose removal is in flight —
    # include it so a removal started on another replica is still visible.
    marker = store.get(marker_key) or {}
    if isinstance(marker, dict) and marker.get("system_id"):
        keys.add(marker["system_id"])
    keys.discard(marker_key)
    items: list[dict] = []
    for sid in sorted(keys):
        job = store.get(sid) or {}
        state = job.get("state") if isinstance(job, dict) else None
        if state not in _SURFACED_STATES:
            continue
        label = {
            "running": f"Removing system {sid}",
            "done": f"Removal of {sid} finished",
            "error": f"Removal of {sid} failed",
        }[state]
        item = {"kind": "removal", "label": label, "state": state,
                "system_id": sid, "href": _HREF_ONBOARD}
        if job.get("started_at"):
            item["started_at"] = job["started_at"]
        items.append(item)
    return items


def _snapshot_job_items(kind: str, attr: str, noun: str) -> list[dict]:
    """One graph-snapshot job slot (import or repair) as a feed item."""
    from . import graph_snapshot_routes as gsr
    store = getattr(gsr, attr, None)
    if store is None:
        return []
    job = store.get("global") or {}
    state = job.get("state") if isinstance(job, dict) else None
    if state not in _SURFACED_STATES:
        return []
    label = {
        "running": f"{noun} running",
        "done": f"{noun} finished",
        "error": f"{noun} failed",
    }[state]
    item = {"kind": kind, "label": label, "state": state, "href": _HREF_ADMIN}
    if job.get("started_at"):
        item["started_at"] = job["started_at"]
    return [item]


def _import_items() -> list[dict]:
    return _snapshot_job_items("import", "_IMPORT_JOBS", "Graph snapshot import")


def _repair_items() -> list[dict]:
    return _snapshot_job_items("repair", "_REPAIR_JOBS", "Graph reconcile repair")


#: (kind, source) pairs — resolved per request so tests can monkeypatch the
#: underlying modules; each source is independently fail-soft.
_SOURCES: tuple[tuple[str, str], ...] = (
    ("proposals", "_proposal_items"),
    ("questions", "_question_items"),
    ("removal", "_removal_items"),
    ("import", "_import_items"),
    ("repair", "_repair_items"),
)


@router.get("/notifications")
def get_notifications() -> dict:
    """Aggregate the attention feed. Fail-soft: a raising source yields a
    ``{"kind", "error"}`` item; the other sources' items survive."""
    import sys
    module = sys.modules[__name__]
    items: list[dict] = []
    for kind, fn_name in _SOURCES:
        try:
            items.extend(getattr(module, fn_name)())
        except Exception as exc:  # noqa: BLE001 - per-source fail-soft by contract
            logger.warning("notifications source %s failed: %s", kind, type(exc).__name__)
            items.append({"kind": kind, **public_error(exc, f"notifications.{kind}")})
    return {"items": items,
            "generated_at": datetime.now(timezone.utc).isoformat()}
