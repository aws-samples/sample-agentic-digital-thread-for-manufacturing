# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ValueMapping graph hygiene for accounts onboarded before the replay fix.

Two classes of bad ``:ValueMapping`` row survive on graphs provisioned before
the §i replay became ValueMapping-only and equipment mappings became
identity-keyed (2026-09-28):

* **malformed learned** — the old ``onboard._dp_reapply_self_learned`` loop
  replayed EVERY accepted self-learning item as a ValueMapping, including a
  delivered guidance note (ADR-0063 — never a graph rule). The row carries the
  item's ``neptune_node_id`` (``guidance-subj-*``) or the ``vm-learned-*``
  fallback, an empty concept, one empty side and the note sentence on the
  other. Only that SHAPE is classified: a learned row with a concept and both
  values is a real mapping and is left alone.
* **duplicate generated** — ``vm-equip-*`` rows sharing the equipment identity
  (``onboard._EQUIPMENT_VM_IDENTITY``) with another ``vm-equip-*`` row (older
  index-/field-keyed ids). The survivor is the row whose id equals the current
  ``onboard._equipment_vm_id`` key, else the newest by any timestamp, else the
  lexically first id — replace-not-accrete (ADR-0052).

``subj-*`` steward rows ALWAYS survive (the planner never classifies them and
the applier refuses them), and any row that is not classified is never
touched. The planner is pure; the applier takes the graph read/write seams so
it runs through the steward-gated ``POST /api/neptune/graph/repair`` body.

A failed read is never an empty result: the read seam (``neptune_query``)
degrades to ``[]``, so the scan is controlled by a one-row count (the scan
must return exactly that many rows) and the readback answers one row per
requested id. A failed control reports ``scan_failed``/``readback_failed``
and ``verified: false``; a failed scan plans and writes nothing. Id lists are
capped at :data:`REPORT_ID_CAP` with ``<list>_truncated`` flags.
"""
from __future__ import annotations

from typing import Callable, Iterable

from .stored_value_mapping_memo import mutates_value_mappings

# Id prefixes the OLD replay minted for a learned item: the accepted item's
# ``neptune_node_id`` for a guidance note (``self_learning`` stamps
# ``guidance-<subject id>``) and the ``vm-learned-`` fallback
# (``onboard._dp_replay_learned_vm``).
LEARNED_VM_PREFIXES = ("guidance-", "vm-learned-")
# Steward-accepted rows (``self_learning._subject_id``) — never touched.
STEWARD_VM_PREFIX = "subj-"
_TIMESTAMP_FIELDS = ("updated_at", "created_at")

SCAN_VALUE_MAPPINGS_CYPHER = "MATCH (vm:ValueMapping) RETURN " + ", ".join(
    f"vm.{f} AS {f}" for f in ("id", "from_system", "from_value", "to_system",
                               "to_value", "concept", *_TIMESTAMP_FIELDS))
# Positive control for the scan: a count ALWAYS answers exactly one row, so an
# empty answer is a failed read (``neptune_query`` degrades to ``[]``), never
# an empty graph; the scan must then return exactly ``n`` rows.
COUNT_VALUE_MAPPINGS_CYPHER = "MATCH (vm:ValueMapping) RETURN count(vm) AS n"
RETIRE_VALUE_MAPPING_CYPHER = "MATCH (vm:ValueMapping {id: $vmid}) DETACH DELETE vm"
# Self-controlled readback: ONE row per requested id (``found`` null when the
# row is gone), so fewer rows than ids means the read failed.
READBACK_VALUE_MAPPINGS_CYPHER = (
    "UNWIND $ids AS i OPTIONAL MATCH (vm:ValueMapping {id: i}) "
    "RETURN i AS id, vm.id AS found")
# Id lists in a report are capped (counts stay exact); ``<list>_truncated``
# says when one was cut.
REPORT_ID_CAP = 200

Read = Callable[..., list]
Write = Callable[..., object]


def _blank(value) -> bool:
    return not str(value or "").strip()


def _is_malformed_learned(row: dict) -> bool:
    """The replayed-note shape: learned id, blank concept, a blank side."""
    rid = str(row.get("id") or "")
    return (rid.startswith(LEARNED_VM_PREFIXES) and _blank(row.get("concept"))
            and (_blank(row.get("from_value")) or _blank(row.get("to_value"))))


def _newest_timestamp(row: dict) -> str:
    return max((str(row.get(f) or "") for f in _TIMESTAMP_FIELDS), default="")


def _winner(group: list[dict], current_id: str) -> dict:
    """Current-key row, else the newest timestamp, else the lexically first id."""
    for row in group:
        if row["id"] == current_id:
            return row
    newest = max(_newest_timestamp(r) for r in group)
    return min((r for r in group if _newest_timestamp(r) == newest),
               key=lambda r: r["id"])


def _duplicate_groups(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Group generated equipment rows by their CURRENT identity key."""
    from .onboard import _EQUIPMENT_VM_PREFIX, _equipment_vm_id
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("id") or "").startswith(_EQUIPMENT_VM_PREFIX):
            groups.setdefault(_equipment_vm_id(row), []).append(row)
    return {k: g for k, g in groups.items() if len(g) > 1}


def plan_value_mapping_hygiene(rows) -> dict:
    """Pure plan over scanned ValueMapping rows (no graph access).

    Returns sorted id lists: ``malformed_learned`` and ``duplicate_generated``
    to retire, and ``kept`` — the survivor of each duplicate group."""
    usable = [r for r in (rows or []) if isinstance(r, dict) and r.get("id")
              and not str(r["id"]).startswith(STEWARD_VM_PREFIX)]
    malformed = sorted(r["id"] for r in usable if _is_malformed_learned(r))
    retire, kept = [], []
    for current_id, group in _duplicate_groups(usable).items():
        keep = _winner(group, current_id)
        kept.append(keep["id"])
        retire.extend(r["id"] for r in group if r is not keep)
    return {"malformed_learned": malformed, "duplicate_generated": sorted(retire),
            "kept": sorted(kept)}


def _capped(report: dict, key: str, ids) -> None:
    ids = list(ids)
    report[key] = ids[:REPORT_ID_CAP]
    report[f"{key}_truncated"] = len(ids) > REPORT_ID_CAP


def _summary(plan: dict, *, dry_run: bool, retired: set | None = None) -> dict:
    malformed, duplicates = plan["malformed_learned"], plan["duplicate_generated"]
    confirmed = (lambda ids: len(ids)) if retired is None else (
        lambda ids: sum(1 for i in ids if i in retired))
    report = {
        "dry_run": dry_run,
        "malformed_learned_retired": confirmed(malformed),
        "duplicate_generated_retired": confirmed(duplicates),
        "kept": len(plan["kept"]),
    }
    for key, ids in (("malformed_learned_ids", malformed),
                     ("duplicate_generated_ids", duplicates), ("kept_ids", plan["kept"])):
        _capped(report, key, ids)
    return report


def _readback_rows(ids: list[str], read: Read) -> dict | None:
    """``{id: found}`` from the one-row-per-id readback, or ``None`` when the
    read failed (not exactly one row for each requested id)."""
    rows = read(READBACK_VALUE_MAPPINGS_CYPHER, {"ids": ids}) or []
    answered = {str(r.get("id")): r.get("found") for r in rows if isinstance(r, dict)}
    if len(rows) != len(ids) or set(answered) != set(ids):
        return None
    return answered


def _readback(plan: dict, targets: list[str], read: Read) -> tuple[dict, set]:
    """Exact readback of every retired AND kept id in one parameterised read.
    Returns the (capped) report and the FULL still-present set."""
    ids = targets + list(plan["kept"])
    answered = _readback_rows(ids, read) if ids else {}
    failed = answered is None
    present = set() if failed else {i for i, found in answered.items() if found is not None}
    still = [i for i in targets if i in present]
    missing = [] if failed else [i for i in plan["kept"] if i not in present]
    report = {"verified": not failed and not still and not missing, "readback_failed": failed}
    _capped(report, "still_present", still)
    _capped(report, "kept_missing", missing)
    return report, set(still)


@mutates_value_mappings
def apply_value_mapping_hygiene(plan: dict, *, write: Write, read: Read) -> dict:
    """DETACH DELETE each planned id (parameterised, one per id), then read
    back. A retirement COUNTS only when its write succeeded AND the readback
    no longer sees the row; a surviving kept row is part of verification.
    A ``subj-*`` id in the plan is refused before any write. When the
    readback itself failed, ``verified`` is false and the counts stay as
    attempted (successful writes) — never confirmed, never invented."""
    targets = list(plan["malformed_learned"]) + list(plan["duplicate_generated"])
    steward = [i for i in targets if str(i).startswith(STEWARD_VM_PREFIX)]
    if steward:
        raise ValueError(f"refusing to retire steward ValueMapping rows: {steward}")
    written = {i for i in targets
               if write(RETIRE_VALUE_MAPPING_CYPHER, {"vmid": i}) is not False}
    readback, still = _readback(plan, targets, read)
    return {**_summary(plan, dry_run=False, retired=written - still), "readback": readback,
            "scan_failed": False, "verified": readback["verified"]}


def scan_value_mappings(read: Read) -> list | None:
    """The scanned rows, or ``None`` when the scan cannot be trusted: the
    count control did not answer one integer row, or the scan's row count
    differs from it (a failed read degrades to ``[]``)."""
    control = read(COUNT_VALUE_MAPPINGS_CYPHER) or []
    n = control[0].get("n") if len(control) == 1 and isinstance(control[0], dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    rows = read(SCAN_VALUE_MAPPINGS_CYPHER) or []
    return rows if len(rows) == n else None


def run_value_mapping_hygiene(read: Read, write: Write, *, dry_run: bool) -> dict:
    """Scan → plan → apply (or, under ``dry_run``, report the plan only —
    no write is ever issued). A failed scan plans nothing, writes nothing and
    reports ``scan_failed: true`` / ``verified: false``."""
    rows = scan_value_mappings(read)
    if rows is None:
        empty = plan_value_mapping_hygiene([])
        return {**_summary(empty, dry_run=dry_run), "scan_failed": True, "verified": False}
    plan = plan_value_mapping_hygiene(rows)
    if dry_run:
        return {**_summary(plan, dry_run=True), "scan_failed": False, "verified": True}
    return apply_value_mapping_hygiene(plan, write=write, read=read)
