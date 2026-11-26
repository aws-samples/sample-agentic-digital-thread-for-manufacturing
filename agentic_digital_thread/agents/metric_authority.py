# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ONE steward writer of ``AuthorityRule`` (ADR 0008 seam, ADR-0076 D2).

``set_authority`` upserts the per-``(system, concept)`` rule (mirrors
onboard.py c4); with ``field`` it ALSO records the steward's binding choice
on that SAME node (``ar.field`` / ``ar.table`` + audit stamps) and re-reads it
— a readback mismatch reports failure (validate → MERGE → exact readback).
:func:`set_authority_payload` is the sync body of the steward-gated
``POST /api/metrics/set-authority``: a ``field`` must name EXACTLY one of the
concept's current binding candidates (ADR-0076 D1) or nothing is written.

The machine NEVER writes ``field``: onboarding, drift, proposal accept and the
sweeps have no path here. Graph I/O goes through ``metric_resolver``'s
``neptune_query`` / ``neptune_write`` seams (looked up lazily, so the offline
lane's monkeypatches apply); ``metric_resolver`` re-exports ``set_authority``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from . import metric_binding as mb
from .metric_turn_invalidation import (  # noqa: F401 - re-exported helper
    invalidate_metric_turn_results, invalidates_metric_turn_results,
)

_FIELD_WRITE = (
    "MATCH (ar:AuthorityRule {id: $arid}) "
    "SET ar.field = $field, ar.table = $table, "
    "ar.field_set_by = $actor, ar.field_set_at = $at"
)
_FIELD_READBACK = (
    "MATCH (ar:AuthorityRule {id: $arid}) "
    "RETURN ar.field AS field, ar.table AS table, ar.status AS status LIMIT 1"
)


def _mr():
    from . import metric_resolver
    return metric_resolver


@invalidates_metric_turn_results
def set_authority(concept: str, system_id: str, *, field: str | None = None,
                  table: str | None = None, actor: str = "") -> bool:
    """Upsert an ``AuthorityRule`` + ``APPLIES_TO`` edge (mirrors onboard.py c4).

    PRECEDENCE NOTE (single source of truth): concept precedence — which system is
    authoritative for a given concept — is driven by these PER-CONCEPT
    ``AuthorityRule`` nodes (resolved in ``_resolve_authority``), NOT by a
    system's ``system_type`` label. ``system_type`` (e.g. 'MES'/'Historian', set via
    ``PUT /api/admin/system-type``) is DESCRIPTIVE metadata for the UI and onboarding
    affinity suggestions only; it never overrides or short-circuits an AuthorityRule.

    ``field`` absent ⇒ exactly the two historical writes. ``field`` present
    (ADR-0076 D2; ``table`` required) ⇒ one more SET plus an exact readback.

    ADR-0078: the per-turn metric-layer result cache is dropped after EVERY
    call (``invalidates_metric_turn_results``) — success, readback mismatch
    or raise (a partial write may already have changed which system is
    authoritative).
    """
    mr = _mr()
    ar_id = f"ar-{system_id}-{concept}"
    ok = mr.neptune_write(
        "MERGE (ar:AuthorityRule {id: $arid}) "
        "SET ar.rule = $rule, ar.system_id = $sid, ar.concept = $concept, ar.priority = 1",
        {
            "arid": ar_id,
            "rule": f"{system_id} is the authoritative source for {concept}",
            "sid": system_id,
            "concept": concept,
        },
    )
    mr.neptune_write(
        "MATCH (ar:AuthorityRule {id: $arid}), (s:System {id: $sid}) "
        "MERGE (ar)-[:APPLIES_TO]->(s)",
        {"arid": ar_id, "sid": system_id},
    )
    if field is None:
        return ok
    return bool(ok) and _write_field(ar_id, field, table, actor) is not None


def _write_field(ar_id: str, field: str, table: str | None, actor: str) -> dict | None:
    """SET the steward's choice, then re-read it: the readback row when it
    matches byte-for-byte, else ``None`` (never a silent partial write)."""
    if not table:
        raise ValueError("a binding field is recorded with its table")
    mr = _mr()
    mr.neptune_write(_FIELD_WRITE, {
        "arid": ar_id, "field": field, "table": table, "actor": actor or "unknown",
        "at": datetime.now(timezone.utc).isoformat(),
    })
    rows = mr.neptune_query(_FIELD_READBACK, {"arid": ar_id}) or []
    row = rows[0] if rows else {}
    if row.get("field") == field and row.get("table") == table:
        return row
    return None


def _actor(request) -> str:
    user = getattr(getattr(request, "state", None), "user", None)
    return getattr(user, "email", None) or "unknown"


def _reject(message: str, candidates) -> JSONResponse:
    shown = candidates[:mb.BINDING_CANDIDATE_CAP]
    return JSONResponse(status_code=400, content={
        "error": message,
        "candidates": [{"table": c.table, "column": c.column} for c in shown],
    })


def _pick(body, candidates) -> mb.BindingCandidate | JSONResponse:
    """Exactly one candidate for ``body.field`` (+ ``body.table``), else a 400."""
    where = f"concept {body.concept!r} on system {body.system_id!r}"
    if not candidates:
        return _reject(f"no MAPS_TO field for {where}", candidates)
    if mb.is_truncated(candidates):
        return _reject(
            f"{where} has more than {mb.BINDING_CANDIDATE_CAP} binding "
            "candidates; the set is not exhaustive, so no field can be "
            "validated against it", candidates)
    matches = mb.match_candidate(candidates, body.field, body.table)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        at = f" on table {body.table!r}" if body.table else ""
        return _reject(f"field {body.field!r}{at} is not a binding of {where}", candidates)
    return _reject(f"field {body.field!r} is bound on {len(matches)} tables for "
                   f"{where}; pass 'table' to choose one", candidates)


def _field_payload(body, request) -> dict | JSONResponse:
    mr = _mr()
    rows = mr.neptune_query(mr._MEASURE_BINDING_CYPHER,
                            {"sid": body.system_id, "concept": body.concept})
    candidates = mb.candidates_from_rows(rows or [])
    chosen = _pick(body, candidates)
    if isinstance(chosen, JSONResponse):
        return chosen
    ok = mr.set_authority(body.concept, body.system_id, field=chosen.column,
                          table=chosen.table, actor=_actor(request))
    return {
        "status": "ok" if ok else "failed", "concept": body.concept,
        "system_id": body.system_id, "field": chosen.column,
        "table": chosen.table, "readback": bool(ok),
        "candidates": [{"table": c.table, "column": c.column} for c in candidates],
    }


def set_authority_payload(body, request=None) -> dict | JSONResponse:
    """Sync body of ``POST /api/metrics/set-authority`` (offloaded; steward-gated
    at registration). Without ``field`` the response is byte-identical to the
    historical route; with it, only an exact candidate reaches the write."""
    if body.field is None:
        ok = _mr().set_authority(body.concept, body.system_id)
        return {"status": "ok" if ok else "failed", "concept": body.concept,
                "system_id": body.system_id}
    return _field_payload(body, request)
