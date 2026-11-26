# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only, opaque reconciliation for accepted guidance delivery."""
from __future__ import annotations

from .guidance_acceptance import opaque_digest
from .guidance_scope import GuidanceScope, scoped_entries

MAX_SOURCE_ITEMS = 1000
MAX_MISMATCHES = 100

SourceEntries = dict[str, set[tuple[str, str]]]

_PAIR_KINDS = {
    ("accepted", "graph"): "accepted_not_graph",
    ("accepted", "delivery"): "accepted_not_delivered",
    ("graph", "accepted"): "graph_not_accepted",
    ("graph", "delivery"): "graph_not_delivery",
    ("delivery", "accepted"): "delivery_not_accepted",
    ("delivery", "graph"): "delivery_not_graph",
}


def _accepted_entries(rows: list[dict]) -> SourceEntries:
    entries: SourceEntries = {}
    for row in rows:
        if row.get("status") != "accepted" or row.get("proposed_type") != "guidance_note":
            continue
        system_id = str(row.get("from_system") or "")
        note = str(row.get("to_value") or "").strip()
        scope = str(row.get("guidance_scope") or GuidanceScope.LEGACY_UNSCOPED.value)
        if system_id and note:
            entries.setdefault(system_id, set()).add((scope, opaque_digest(note)))
    return entries


def _opaque_mismatch(kind: str, system_id: str,
                     note_digest: str | None) -> dict:
    return {"kind": kind, "system_digest": opaque_digest(system_id),
            "note_digest": note_digest}


def _scoped_digests(text: str, *, delivery: bool = False) -> set[tuple[str, str]]:
    return {(scope.value, opaque_digest(note))
            for scope, note in scoped_entries(text, plain_is_base=not delivery)}


def _document_entries(documents: dict[str, str], *, delivery: bool) -> SourceEntries:
    entries: SourceEntries = {}
    for system_id, text in documents.items():
        digests = _scoped_digests(text, delivery=delivery)
        if system_id and digests:
            entries[system_id] = digests
    return entries


def _pair_mismatches(left_name: str, right_name: str,
                     left: SourceEntries, right: SourceEntries) -> list[dict]:
    kind = _PAIR_KINDS[(left_name, right_name)]
    return [
        _opaque_mismatch(kind, system_id, note_digest)
        for system_id, values in left.items()
        for _scope, note_digest in values - right.get(system_id, set())
    ]


def _source_only_mismatches(sources: dict[str, SourceEntries]) -> list[dict]:
    mismatches = []
    names = tuple(sources)
    for name, entries in sources.items():
        other_ids = set().union(*(
            set(sources[other]) for other in names if other != name))
        for system_id in set(entries) - other_ids:
            mismatches.append(_opaque_mismatch(
                f"{name}_only_system", system_id, None))
    return mismatches


def _entry_count(entries: SourceEntries) -> int:
    return sum(len(values) for values in entries.values())


def _accepted_row_count(rows: list[dict]) -> int:
    return sum(
        1 for row in rows
        if row.get("status") == "accepted"
        and row.get("proposed_type") == "guidance_note"
        and str(row.get("from_system") or "")
        and str(row.get("to_value") or "").strip()
    )


def _all_mismatches(sources: dict[str, SourceEntries],
                    available: dict[str, bool]) -> list[dict]:
    mismatches = []
    for left in sources:
        for right in sources:
            if left != right and available[left] and available[right]:
                mismatches.extend(_pair_mismatches(
                    left, right, sources[left], sources[right]))
    if all(available.values()):
        mismatches.extend(_source_only_mismatches(sources))
    return mismatches


def _reconciliation_status(available: dict[str, bool], mismatch_count: int,
                           counts: dict[str, int | None]) -> str:
    if not all(available.values()):
        return "unavailable"
    count_mismatch = len(set(counts.values())) > 1
    return "mismatch" if mismatch_count or count_mismatch else "ok"


def build_reconciliation(accepted_rows: list[dict] | None,
                         graph_guidance: dict[str, str] | None,
                         delivered_guidance: dict[str, str] | None) -> dict:
    """Compare independently collected sources without exposing their text."""
    available = {
        "accepted": accepted_rows is not None,
        "graph": graph_guidance is not None,
        "delivery": delivered_guidance is not None,
    }
    sources = {
        "accepted": _accepted_entries(accepted_rows or []),
        "graph": _document_entries(graph_guidance or {}, delivery=False),
        "delivery": _document_entries(delivered_guidance or {}, delivery=True),
    }
    mismatches = _all_mismatches(sources, available)
    mismatches.sort(key=lambda item: (
        item["system_digest"], item["note_digest"] or "", item["kind"]))
    counts = {name: _entry_count(sources[name]) if available[name] else None
              for name in sources}
    if available["accepted"]:
        counts["accepted"] = _accepted_row_count(accepted_rows or [])
    return {
        "status": _reconciliation_status(available, len(mismatches), counts),
        "counts": {"accepted": counts["accepted"], "graph": counts["graph"],
                   "delivered": counts["delivery"]},
        "sources": {name: "ok" if ok else "unavailable"
                    for name, ok in available.items()},
        "mismatches": mismatches[:MAX_MISMATCHES],
        "mismatch_count": len(mismatches),
        "truncated": len(mismatches) > MAX_MISMATCHES,
    }


def _read_accepted() -> list[dict]:
    from . import self_learning
    rows = self_learning._query_by_status("accepted")
    if len(rows) > MAX_SOURCE_ITEMS:
        raise RuntimeError("accepted guidance reconciliation bound exceeded")
    return rows


def _read_graph() -> dict[str, str]:
    from . import self_learning
    rows = self_learning._neptune_query(
        "MATCH (s:System) WHERE s.query_guidance IS NOT NULL "
        "RETURN s.id AS id, s.query_guidance AS qg ORDER BY s.id "
        f"LIMIT {MAX_SOURCE_ITEMS + 1}", {})
    if len(rows) > MAX_SOURCE_ITEMS:
        raise RuntimeError("graph guidance reconciliation bound exceeded")
    return {str(row.get("id") or ""): str(row.get("qg") or "")
            for row in rows if row.get("id")}


def _read_delivery() -> dict[str, str]:
    from .system_guidance import list_snapshots
    snapshots = list_snapshots(max_items=MAX_SOURCE_ITEMS, max_pages=10)
    return {system_id: snapshot.text for system_id, snapshot in snapshots.items()}


def _available_or_none(reader):
    try:
        return reader()
    except Exception:  # noqa: BLE001 - each source reports honest unavailability
        return None


def collect_reconciliation() -> dict:
    """Read every source independently; one outage never erases another."""
    accepted = _available_or_none(_read_accepted)
    graph = _available_or_none(_read_graph)
    delivered = _available_or_none(_read_delivery)
    return build_reconciliation(accepted, graph, delivered)
