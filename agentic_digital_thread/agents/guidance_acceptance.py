# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure, opaque planning primitives for strict guidance acceptance."""
from __future__ import annotations

import hashlib
import re

from .query_intelligence import LEARNED_NOTE_PREFIX

MAX_GUIDANCE_LEN = 4000
_CONCEPT_MAPPED_SUFFIX = "use concept-mapped columns"
_NEGATIVE = re.compile(
    r"^(?P<key>.+?):\s*no column\s+['\"]?(?P<col>[^'\"\s]+)['\"]?",
    re.IGNORECASE,
)
_AVOID = re.compile(
    r"^(?P<key>.+?):\s*avoid columns\s*\[(?P<cols>[^\]]*)\]",
    re.IGNORECASE,
)


def opaque_digest(value: str) -> str:
    """Stable full digest; the source text never enters API responses or logs."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def split_guidance(guidance: str) -> tuple[str, list[str]]:
    from .guidance_scope import parse_document, scoped_entries
    document = parse_document(guidance)
    return document["base"], [text for _scope, text in scoped_entries(document)]


def assemble_guidance(base: str, notes: list[str]) -> str:
    additions = " ".join(f"{LEARNED_NOTE_PREFIX}{note}" for note in notes)
    return f"{base} {additions}".strip() if base and additions else (base or additions).strip()


def _negative_columns(note: str) -> tuple[str, list[str]] | None:
    match = _AVOID.match(note)
    if match:
        columns = [value.strip() for value in match.group("cols").split(",") if value.strip()]
        return match.group("key").strip(), columns
    match = _NEGATIVE.match(note)
    if match:
        return match.group("key").strip(), [match.group("col").strip()]
    return None


def consolidate_guidance_notes(notes: list[str]) -> list[str]:
    groups: dict[str, list[str]] = {}
    slots: list[tuple[str, str]] = []
    for raw in notes:
        note = (raw or "").strip()
        parsed = _negative_columns(note) if note else None
        if not parsed:
            if note:
                slots.append(("note", note))
            continue
        key, columns = parsed
        if key not in groups:
            groups[key] = []
            slots.append(("group", key))
        groups[key].extend(column for column in columns if column not in groups[key])
    rendered = [
        (f"{value}: avoid columns [{', '.join(groups[value])}]; {_CONCEPT_MAPPED_SUFFIX}"
         if kind == "group" else value)
        for kind, value in slots
    ]
    return list(dict.fromkeys(rendered))


def _note_represented(note: str, notes: list[str]) -> bool:
    if note in notes:
        return True
    parsed = _negative_columns(note)
    if not parsed:
        return False
    key, columns = parsed
    for candidate in notes:
        candidate_parsed = _negative_columns(candidate)
        if candidate_parsed and candidate_parsed[0] == key:
            return all(column in candidate_parsed[1] for column in columns)
    return False


def evolve_guidance(current: str, note: str, max_len: int,
                    scope: str = "query_hint") -> str | None:
    from .guidance_scope import evolve_document
    return evolve_document(current, note, scope, max_len)


def preview_guidance(current: str | None, note: str | None, *,
                     max_len: int = MAX_GUIDANCE_LEN,
                     scope: str = "query_hint") -> dict:
    """Plan one scoped append without I/O; return opaque metadata only."""
    from .guidance_scope import contains_note, evicted_entries, normalize_scope
    clean = note.strip() if isinstance(note, str) else ""
    resolved = normalize_scope(scope)
    note_meta = {"note_digest": opaque_digest(clean) if clean else None,
                 "note_length": len(clean) if clean else None,
                 "scope": resolved.value}
    if not isinstance(current, str) or not clean:
        return {"outcome": "unavailable", "current_digest": None,
                "current_length": None, **note_meta, "result_digest": None,
                "result_length": None, "evicted_note_digests": []}
    common = {"current_digest": opaque_digest(current),
              "current_length": len(current), **note_meta}
    if contains_note(current, clean, resolved):
        return {**common, "outcome": "duplicate",
                "result_digest": opaque_digest(current),
                "result_length": len(current), "evicted_note_digests": []}
    result = evolve_guidance(current, clean, max_len, resolved.value)
    if result is None:
        return {**common, "outcome": "no_room", "result_digest": None,
                "result_length": None, "evicted_note_digests": []}
    evicted = [opaque_digest(text) for _scope, text
               in evicted_entries(current, result)]
    return {**common, "outcome": "would_evict" if evicted else "append",
            "result_digest": opaque_digest(result), "result_length": len(result),
            "evicted_note_digests": evicted}


def _planned_result(current: str, note: str, max_len: int,
                    scope: str) -> tuple[str, str | None, list[str]]:
    from .guidance_scope import contains_note, evicted_entries
    clean = note.strip()
    if contains_note(current, clean, scope):
        return "duplicate", current, []
    result = evolve_guidance(current, clean, max_len, scope)
    if result is None:
        return "no_room", None, []
    evicted = [text for _scope, text in evicted_entries(current, result)]
    return ("would_evict" if evicted else "append"), result, evicted


def _confirmation_error(preview: dict, confirmation: dict | None) -> str | None:
    if preview["outcome"] != "would_evict":
        return None
    if not isinstance(confirmation, dict):
        return "eviction_confirmation_required"
    if set(confirmation) != {"current_digest", "evicted_note_digests"}:
        return "eviction_confirmation_mismatch"
    if confirmation.get("current_digest") != preview["current_digest"]:
        return "stale_preview"
    if confirmation.get("evicted_note_digests") != preview["evicted_note_digests"]:
        return "eviction_confirmation_mismatch"
    return None


def _pending(preview: dict, http_status: int = 503, **drift) -> dict:
    from .guidance_saga import pending_result
    result = pending_result(**drift)
    return {**result, "outcome": preview["outcome"],
            "http_status": http_status, "preview": preview}


def execute_guidance_acceptance(
    *, system_id: str, current: str, note: str,
    confirmation: dict | None, graph_compare_and_set, graph_read,
    delivery_before, delivery_compare_and_set, delivery_read,
    terminal_compare_and_set, terminal_read, terminal_restore=None,
    max_len: int = MAX_GUIDANCE_LEN, scope: str = "query_hint",
) -> dict:
    """Prove parity, synchronize graph/delivery, then commit proposal status."""
    preview = preview_guidance(current, note, max_len=max_len, scope=scope)
    if preview["outcome"] in {"unavailable", "no_room", "duplicate"}:
        code = 503 if preview["outcome"] == "unavailable" else 409
        return _pending(preview, code)
    if _confirmation_error(preview, confirmation):
        return _pending(preview, 409)
    _outcome, desired, _evicted = _planned_result(
        current, note, max_len, scope)
    if desired is None or not system_id:
        return _pending(preview)
    from .guidance_saga import replace_exact
    result = replace_exact(
        graph_before=current, graph_after=desired,
        delivery_before=delivery_before,
        graph_cas=graph_compare_and_set, graph_read=graph_read,
        delivery_cas=delivery_compare_and_set, delivery_read=delivery_read,
        terminal_cas=terminal_compare_and_set, terminal_read=terminal_read,
        terminal_restore=terminal_restore)
    if result["status"] != "synchronized":
        stale = (preview["outcome"] == "would_evict"
                 and result["drift"]["graph"])
        return {**result, "outcome": preview["outcome"],
                "http_status": 409 if stale else 503, "preview": preview}
    return {**result, "status": "delivered", "outcome": preview["outcome"],
            "preview": preview}
