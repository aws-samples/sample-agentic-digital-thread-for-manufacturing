# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retire the steward queue's machine proposals for an offboarded system.

Offboarding deletes the system's graph artifacts, runtime and gateway target,
but the proposals store is separate (DynamoDB). Without this step every PENDING
machine proposal naming the removed system stays in the steward queue — live
2026-09-27: 24 pending ``join_path``/``insight`` rows pointed at a system that no
longer existed, and accepting one would write a JoinPath to nowhere.

The same supersede serves onboarding (``retire_pending_ids``): a join or
crosswalk proposal its discovery filed against a peer whose removal started
meanwhile is superseded at the end of the resolve job.

Contract (ADR-0052/0053):
- Only ``pending`` rows move, and only to ``superseded`` — the machine NEVER
  writes ``rejected`` (a reject is a steward tombstone).
- Steward-authored rows (``source`` user/steward) are left untouched: steward
  rows always survive.
- Accepted/rejected/expired rows are history and are never touched.
- Each write is CONDITIONAL on the row still being ``pending``, so a steward
  decision landing mid-retirement wins; that row is simply not counted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Sources whose rows are steward claims — never retired by the machine.
STEWARD_SOURCES = frozenset({"user", "steward"})
OFFBOARDED_REASON = "system_offboarded"
#: A proposal onboarding filed against a peer whose removal started meanwhile.
SYSTEM_BEING_REMOVED_REASON = "system_being_removed"


def _names_system(row: dict, system_id: str) -> bool:
    """Exact system id, case-insensitive. Deliberately NOT the short-name
    ("mes-mil" -> "mes") match the graph step uses: a short name is shared by
    every site of that system type, so retiring by it would supersede OTHER,
    still-onboarded systems' proposals."""
    wanted = system_id.casefold()
    return any(str(row.get(k) or "").casefold() == wanted
               for k in ("from_system", "to_system"))


def retirable_rows(rows: list[dict], system_id: str) -> list[dict]:
    """PENDING machine rows that name ``system_id`` on either side."""
    return [
        r for r in rows
        if r.get("status") == "pending"
        and _names_system(r, system_id)
        and str(r.get("source") or "") not in STEWARD_SOURCES
    ]


def _is_condition_race(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "ConditionalCheckFailedException"


def retire_pending_for_offboarded_system(system_id: str) -> int:
    """Supersede one offboarded system's pending machine proposals."""
    return retire_pending_for_offboarded_systems([system_id])


def retire_pending_for_offboarded_systems(system_ids: list[str]) -> int:
    """Supersede the pending machine proposals naming ANY of ``system_ids``
    (one pending-queue read). Returns rows superseded. Raises on a store
    failure (the caller decides how to report it); a lost conditional race is
    not an error."""
    ids = [s for s in (system_ids or []) if s]
    if not ids:
        return 0
    from .self_learning import _query_by_status

    pending = _query_by_status("pending")
    rows = {r["mapping_id"]: r for sid in ids for r in retirable_rows(pending, sid)}
    return _supersede(rows.values(), OFFBOARDED_REASON)


def retire_pending_ids(mapping_ids, reason: str) -> int:
    """Supersede the pending MACHINE proposals among ``mapping_ids`` (one
    pending-queue read), stamped with ``reason``: onboarding's join discovery
    uses it for proposals it filed against a system whose removal started
    meanwhile. Steward rows and decided rows are left as they are. Returns
    rows superseded; raises on a store failure like the offboard path."""
    wanted = {m for m in (mapping_ids or []) if m}
    if not wanted:
        return 0
    from .self_learning import _query_by_status

    rows = [r for r in _query_by_status("pending")
            if r.get("mapping_id") in wanted and r.get("status") == "pending"
            and str(r.get("source") or "") not in STEWARD_SOURCES]
    return _supersede(rows, reason)


def _supersede(rows, reason: str) -> int:
    """Move each row ``pending`` -> ``superseded`` with ``reason``, each write
    conditional on the row still being pending (a steward decision landing
    first wins and is not counted)."""
    from .self_learning import _get_table, _update_status

    table = _get_table()
    stamp = datetime.now(timezone.utc).isoformat()
    extra = {"superseded_at": stamp, "superseded_reason": reason}
    retired = 0
    for row in rows:
        try:
            _update_status(table, row["mapping_id"], row["discovered_at"],
                           "superseded", extra, expect_status="pending")
            retired += 1
        except Exception as exc:  # noqa: BLE001 - only the race is tolerated
            if not _is_condition_race(exc):
                raise
            logger.info("proposal retirement: %s was decided concurrently; left as-is",
                        row["mapping_id"])
    return retired
