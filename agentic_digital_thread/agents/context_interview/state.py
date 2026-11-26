# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guided-onboarding state: first-launch detection, dismissal, answer store (T-C).

Two jobs, both deliberately boring:

**First-launch detection.** A fresh deployment is one where NOTHING has been
established yet — all three of: no system reports ``onboarded``, no system has
business context enabled, and the graph holds zero ``MAPS_TO`` edges. All three,
not any, because each alone has false positives: a steward mid-onboarding has no
`MAPS_TO` yet but is clearly not on day 0; an operator who cleared business
context has not reset their deployment. Computed server-side in ONE place so the
frontend never re-derives the rule (and cannot drift from it).

**Dismissal.** A steward who declines the guided flow must not be asked again, so
dismissal persists through the SAME duck-typed settings-store seam the demo and
live-updates toggles use (``storage.SsmSettingsStore`` — ``get_flag``/
``set_flag``). Reusing it means the ``ADT_SETTINGS_STORE=none`` local escape hatch
already applies: in local mode dismissal is in-memory for the process, which is
the right trade (a laptop should not write deployment state to a shared account).

The answer/draft store is in-memory-with-graph-fallback-free by design: interview
answers become proposals immediately (``landing.land_answer``), so the only thing
held here is the un-confirmed business-context DRAFT and the set of answered
topics. Losing that to a restart costs a steward a re-answer, never data
integrity — which is why it does not justify a new persistence surface.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

DISMISS_PARAM = "/adt/guided-onboarding-dismissed"

_lock = threading.Lock()
_dismissed_memo: bool | None = None
# system_id ("" = deployment scope) -> {"topics": set[str], "draft": str}
_answers: dict[str, dict] = {}


def _store():
    """Return the settings store, or ``None`` when persistence is opted out.

    Mirrors ``server.lifespan``'s ADT_SETTINGS_STORE=none contract so local mode
    never reads or writes a deployed account's parameters.
    """
    if os.environ.get("ADT_SETTINGS_STORE", "").strip().lower() == "none":
        return None
    try:
        from ..storage import SsmSettingsStore
        return SsmSettingsStore(DISMISS_PARAM)
    except Exception as exc:  # nosec B110 - persistence is optional, never fatal
        logger.warning("guided-onboarding store unavailable: %s", exc)
        return None


def is_dismissed() -> bool:
    """True when a steward has declined the guided flow. Read-through cached."""
    global _dismissed_memo
    with _lock:
        if _dismissed_memo is not None:
            return _dismissed_memo
    store = _store()
    value = False
    if store is not None:
        try:
            value = bool(store.get_flag())
        except Exception as exc:  # nosec B110 - a settings read never breaks the flow
            logger.warning("guided dismissal read failed: %s", exc)
            value = False
    with _lock:
        _dismissed_memo = value
    return value


def set_dismissed(value: bool) -> None:
    """Persist the dismissal. Write failures PROPAGATE — a steward who dismisses
    and is asked again on the next load has been silently failed."""
    global _dismissed_memo
    store = _store()
    if store is not None:
        store.set_flag(bool(value))
    with _lock:
        _dismissed_memo = bool(value)


def _has_onboarded_system() -> bool:
    try:
        from ..catalog import catalog
        return bool(catalog.get_onboarded())
    except Exception:  # nosec B110 - absence of catalog reads as "nothing onboarded"
        return False


def _has_enabled_business_context() -> bool:
    try:
        from ..business_context import read_business_context
        from ..catalog import catalog
        for entry in catalog.get_all():
            row = read_business_context(entry.get("id") or "")
            if row and row.get("enabled") and (row.get("business_context") or "").strip():
                return True
    except Exception:  # nosec B110 - unreadable context reads as "none set"
        return False
    return False


def _has_mappings() -> bool:
    try:
        from ..db import neptune_query
        rows = neptune_query(
            "MATCH ()-[r:MAPS_TO]->() RETURN count(r) AS n LIMIT 1", {})
    except Exception:  # nosec B110 - graph unavailable reads as "no mappings"
        return False
    for row in rows or []:
        try:
            if int(row.get("n") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def is_first_launch() -> bool:
    """True only when NOTHING has been established yet (all three signals).

    Short-circuits on the cheapest signal first: the catalog is in-memory, the
    business-context read hits the graph per system, the edge count hits it once.
    """
    if _has_onboarded_system():
        return False
    if _has_enabled_business_context():
        return False
    return not _has_mappings()


def answered_topics(scope_id: str = "") -> set[str]:
    with _lock:
        return set(_answers.get(scope_id, {}).get("topics") or set())


def record_topic(topic: str, scope_id: str = "") -> None:
    if not topic:
        return
    with _lock:
        slot = _answers.setdefault(scope_id, {"topics": set(), "draft": ""})
        slot["topics"].add(topic)


def draft_context(scope_id: str = "") -> str:
    with _lock:
        return _answers.get(scope_id, {}).get("draft") or ""


def append_draft(text: str, scope_id: str = "", *, max_len: int = 2000) -> str:
    """Merge one held answer into the business-context draft (bounded)."""
    from .landing import merge_draft_context

    with _lock:
        slot = _answers.setdefault(scope_id, {"topics": set(), "draft": ""})
        slot["draft"] = merge_draft_context(slot["draft"], [text], max_len=max_len)
        return slot["draft"]


def clear_draft(scope_id: str = "") -> None:
    with _lock:
        slot = _answers.get(scope_id)
        if slot:
            slot["draft"] = ""


def clear_confirmed_draft(confirmed: str, scope_id: str = "") -> None:
    """Remove the CONFIRMED draft text and keep anything appended after it.

    The confirm reads the draft, then writes and rolls off the event loop, so
    a guided answer can be appended meanwhile. The draft only ever grows at
    the end (``merge_draft_context``), so the confirmed text is a prefix of
    the current draft and the remainder is what landed mid-confirm. If it is
    not a prefix (the draft was replaced), nothing is cleared: keeping text
    the steward can review beats discarding text that was never written."""
    with _lock:
        slot = _answers.get(scope_id)
        if not slot:
            return
        current = slot["draft"] or ""
        if current.startswith(confirmed):
            slot["draft"] = current[len(confirmed):].strip()


# Bounded manifest of landings this interview session produced, for the final
# review ("here is everything about to be written, by destination"). In-memory
# like the draft: losing it to a restart loses only the SUMMARY view — the
# proposals themselves are already durable in the lifecycle store.
MAX_LANDINGS = 200
_landings: list[dict] = []


def record_landing(entry: dict) -> None:
    """Record one non-rejected landing for the review manifest (bounded)."""
    if not isinstance(entry, dict) or not entry.get("question_id"):
        return
    with _lock:
        if len(_landings) < MAX_LANDINGS:
            _landings.append(dict(entry))


def landings() -> list[dict]:
    with _lock:
        return [dict(e) for e in _landings]


def clear_landings() -> None:
    with _lock:
        _landings.clear()


def landing_count() -> int:
    with _lock:
        return len(_landings)


def clear_landings_before(count: int) -> None:
    """Drop the first ``count`` landings (the ones a confirm reviewed) and keep
    any recorded after its snapshot. Landings are only ever appended."""
    with _lock:
        del _landings[:max(0, count)]


def reset_for_tests() -> None:
    """Test seam only — clears the memo, the answer store, and the manifest."""
    global _dismissed_memo
    with _lock:
        _dismissed_memo = None
        _answers.clear()
        _landings.clear()


def guided_state() -> dict:
    """The ONE payload the frontend consumes; it never re-derives the rule.

    ``first_launch`` stays true while an UNCONFIRMED draft exists even after a
    system onboards. Without that, the moment the first system finished mapping
    the flow would vanish and strand the steward's un-saved answers — the day-0
    interview would lose exactly the work it was created to capture.
    """
    dismissed = is_dismissed()
    first = False if dismissed else (is_first_launch() or bool(draft_context()))
    return {
        "first_launch": first,
        "dismissed": dismissed,
        "answered_topics": sorted(answered_topics()),
        "draft_business_context": draft_context(),
    }
