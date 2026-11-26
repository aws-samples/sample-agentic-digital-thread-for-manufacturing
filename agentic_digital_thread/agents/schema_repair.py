# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Additive schema repair (ADR-0077 D1/D2) — sync, deterministic, no LLM.

``materialise_drift`` is the write side of the steward's accept of a
``column_added``/``table_added`` ``schema_drift`` row: validate identifiers
(fail closed) → RE-READ the source (the row never carries a schema payload)
→ MERGE through the ONE writer home ``schema_fields`` (shared with
onboarding b2a) → exact readback. It never writes ``MAPS_TO``, authority,
identity/join rules or runtime state; the caller passes its own graph seams.

``repair_schema`` is the body of the steward-gated
``POST /api/neptune/graph/repair-schema``: it re-runs the drift check and
re-applies — through the EXISTING ``accept_mapping`` — only the pending rows
the check re-filed after a prior accept of the SAME addition (the
``REDETECTED_DESCRIPTION`` marker, any generation). Never-
reviewed drift is never touched (no auto-accept). Bounded and idempotent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MATERIALISE_COLUMN_CAP = 1000   # table_added fail-closed bound
REPAIR_ACCEPT_CAP = 200         # accepts per repair-schema call
_FAILED_DETAIL_CAP = 20


def split_from_field(from_field: str) -> tuple[str, str]:
    """``"table.col.leaf"`` → ``("table", "col.leaf")``; ``"table."`` →
    ``("table", "")`` (the table-level grain). ``ValueError`` without a dot
    or with an empty table."""
    text = str(from_field or "")
    if "." not in text:
        raise ValueError(f"Invalid drift subject: {text!r}")
    table, column = text.split(".", 1)
    if not table:
        raise ValueError(f"Invalid drift subject: {text!r}")
    return table, column


def _athena_config(system_id: str, entry: dict) -> dict:
    """The SAME config precedence + cross-account merge discovery uses."""
    from .onboard_agent import _athena_discover_cfg
    resolved: dict = {}
    if not entry.get("connection_config"):
        from .connection_resolver import resolve_connection
        resolved = resolve_connection(system_id)
    return _athena_discover_cfg(system_id, entry, resolved, None)


def _fresh_columns(system_id: str, table: str) -> tuple[list, bool]:
    """Non-Athena source read: the drift check's own read-only discover."""
    from . import schema_drift
    fresh = schema_drift._discover_fresh(system_id)
    if not isinstance(fresh, dict) or fresh.get("error"):
        raise RuntimeError("source discovery failed: DiscoveryError")
    meta = schema_drift._match_fresh_table(
        schema_drift._fresh_table_map(fresh.get("tables") or []), table)
    if meta is None:
        raise ValueError(f"table '{table}' is not visible in the source")
    return [{"name": c, "data_type": meta["types"].get(c, ""), "is_partition": False}
            for c in meta["columns"]], True


def _source_columns(system_id: str, table: str) -> tuple[list, bool]:
    """The CURRENT physical columns of ``table`` + an affirmative completeness
    flag. Athena: the metadata-API describe (flattened, partition-flagged)."""
    from .catalog import catalog
    from .connection_types import ATHENA
    entry = catalog.get(system_id) or {}
    if entry.get("connection_type") == ATHENA:
        from .athena_tools import describe_physical_columns_bounded
        # refresh=True: bypass the schema-cache READ — accept writes the
        # CURRENT metadata, never a describe warmed by the drift check.
        cols, complete = describe_physical_columns_bounded(
            table, _athena_config(system_id, entry), refresh=True)
        return list(cols or []), bool(complete)
    return _fresh_columns(system_id, table)


def _select_columns(cols: list, column: str) -> list:
    """The source columns a ``column_added`` subject names: the exact column
    or every flattened leaf under a struct parent (case-insensitive)."""
    wanted = column.lower()
    chosen = [c for c in cols
              if str(c.get("name") or "").lower() == wanted
              or str(c.get("name") or "").lower().startswith(wanted + ".")]
    return sorted(chosen, key=lambda c: str(c.get("name")))


def _require_schema_entry(system_id: str, table: str, *, query) -> None:
    """A ``column_added`` lands on an EXISTING SchemaEntry. The System probe
    is the ``_repoint_maps_to`` honesty guard: the read seam may swallow a
    backend error into ``[]``, so "no System either" means graph unreachable."""
    from .schema_fields import schema_entry_exists
    if schema_entry_exists(system_id, table, query=query):
        return
    rows = query("MATCH (s:System {id: $sid}) RETURN count(s) AS n", {"sid": system_id})
    if not rows or int((rows[0] or {}).get("n") or 0) < 1:
        raise RuntimeError("graph unavailable — schema drift not materialised")
    raise ValueError(f"table '{table}' is not in the onboarded schema for "
                     f"system '{system_id}'")


def _readback_all(system_id: str, table: str, cols: list, *, query,
                  table_created: bool = False) -> None:
    """EXACT readback — every written property compared, never existence
    only: the SchemaEntry (name/system_id through System-HAS_SCHEMA) when the
    table was created, then each Field's full MERGEd shape through the
    System-HAS_SCHEMA-HAS_FIELD path. Any mismatch raises (row stays pending)."""
    from .schema_fields import (expected_field, field_id, field_mismatches,
                                read_field, read_schema_entry, schema_entry_id)
    if table_created:
        want = {"name": table, "system_id": system_id}
        bad = field_mismatches(want, read_schema_entry(system_id, table, query=query))
        if bad:
            raise RuntimeError("materialisation readback mismatch for "
                               f"{schema_entry_id(system_id, table)}: {bad}")
    for col in cols:
        bad = field_mismatches(expected_field(system_id, table, col),
                               read_field(system_id, table, col["name"], query=query))
        if bad:
            raise RuntimeError("materialisation readback mismatch for "
                               f"{field_id(system_id, table, col['name'])}: {bad}")


def _refresh_completeness(system_id: str, table: str, source_cols: list,
                          complete: bool, *, write, query) -> None:
    """Rewrite the D3 flag from graph-vs-source: complete ⇔ the source read
    was complete AND every described column is now a Field."""
    from .schema_fields import table_field_names, write_discovery_completeness
    names = table_field_names(system_id, table, query=query)
    now_complete = complete and {str(c.get("name")) for c in source_cols} <= names
    write_discovery_completeness(system_id, table, now_complete, len(names), write=write)


def _validated_subject(item: dict) -> tuple[str, str, str, str]:
    """``(system_id, table, column, reason)`` — every identifier fail-closed."""
    from .db import validate_system_id
    from .metric_binding import validate_column_name
    from .schema_drift import MATERIALISABLE_KINDS
    from .schema_fields import validate_table_name
    reason = str(item.get("reason") or "")
    if reason not in MATERIALISABLE_KINDS:
        raise ValueError(f"schema drift kind '{reason}' is not materialisable")
    system_id = validate_system_id(str(item.get("from_system") or ""))
    table, column = split_from_field(item.get("from_field"))
    validate_table_name(table)
    if reason == "column_added":
        validate_column_name(column)
    return system_id, table, column, reason


def _chosen_columns(system_id: str, table: str, column: str, reason: str,
                    cols: list, *, query) -> list:
    """The validated columns to materialise for this drift kind."""
    from .schema_fields import validate_physical_column
    if reason == "table_added":
        if len(cols) > MATERIALISE_COLUMN_CAP:
            raise ValueError(f"table '{table}' has {len(cols)} columns — over the "
                             f"{MATERIALISE_COLUMN_CAP}-column materialisation cap")
        chosen = cols
    else:
        _require_schema_entry(system_id, table, query=query)
        chosen = _select_columns(cols, column)
    if not chosen:
        raise ValueError(f"'{table}.{column}' is not visible in the source")
    return [validate_physical_column(c) for c in chosen]


def materialise_drift(item: dict, *, write, query) -> dict:
    """Validate → re-read → MERGE → readback one additive drift row.

    ``ValueError`` (bad identifier, not in source, table not onboarded)
    precedes EVERY write. ``RuntimeError`` (graph unreachable, readback
    miss) is fail-loud; the caller leaves the row pending."""
    from .schema_fields import (field_id, schema_entry_id, write_physical_field,
                                write_schema_entry)
    system_id, table, column, reason = _validated_subject(item)
    cols, complete = _source_columns(system_id, table)
    chosen = _chosen_columns(system_id, table, column, reason, cols, query=query)
    seid = schema_entry_id(system_id, table)
    if reason == "table_added":
        write_schema_entry(system_id, table, write=write)
    for col in chosen:
        write_physical_field(system_id, table, col, write=write)
    _refresh_completeness(system_id, table, cols, complete, write=write, query=query)
    _readback_all(system_id, table, chosen, query=query,
                  table_created=reason == "table_added")
    # The applied artifact: the one Field, else the SchemaEntry owning them
    # (a table, or a struct parent flattened to several leaves).
    node_id = (field_id(system_id, table, chosen[0]["name"])
               if reason == "column_added" and len(chosen) == 1 else seid)
    return {"node_id": node_id, "schema_entry": seid,
            "fields": [c["name"] for c in chosen], "field_count": len(chosen),
            "table_created": reason == "table_added"}


def _drift_rows(system_id: str, pending: list) -> list:
    """The system's pending ``schema_drift`` rows."""
    return [r for r in pending or []
            if r.get("proposed_type") == "schema_drift"
            and str(r.get("from_system") or "") == system_id]


def _marked_rows(system_id: str, pending: list) -> list:
    """Pending additive drift the check RE-FILED after a prior steward accept.

    Eligibility is PROVEN from stored lineage, never from marker text: the row
    must be generated drift (``source='agent'``, ``method='schema-drift'``),
    carry the generation marker, AND the subject's most recent accept must be
    the SAME additive reason (``schema_drift._accept_generation > 0``). A
    misfiled or hand-crafted row with a marker but no accepted predecessor
    stays in the ordinary review queue (ADR-0077 D2)."""
    from .schema_drift import (MATERIALISABLE_KINDS, _accept_generation,
                               redetected_generation)
    rows = []
    for r in _drift_rows(system_id, pending):
        if (r.get("reason") in MATERIALISABLE_KINDS
                and r.get("source") == "agent" and r.get("method") == "schema-drift"
                and redetected_generation(r.get("to_description"))
                and _accept_generation(system_id, str(r.get("from_field") or ""),
                                       str(r.get("reason") or "")) > 0):
            rows.append(r)
    return sorted(rows, key=lambda r: str(r.get("from_field") or ""))


def _accept_rows(rows: list, actor: str) -> tuple[int, list]:
    """The EXISTING accept per row; a failure is reported, never fatal."""
    from . import self_learning
    materialised, failed = 0, []
    for row in rows:
        mapping_id = str(row.get("mapping_id") or "")
        try:
            result = self_learning.accept_mapping(mapping_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 - class only; the row stays pending
            failed.append({"mapping_id": mapping_id, "error": type(exc).__name__})
            continue
        if "error" in result or not result.get("materialised"):
            failed.append({"mapping_id": mapping_id,
                           "error": str(result.get("error") or "not materialised")})
            continue
        materialised += 1
    return materialised, failed


def repair_schema(system_id: str, actor: str = "", detect: bool = True) -> dict:
    """Re-apply already-accepted additive drift for ONE system (counts only)."""
    from . import schema_drift, self_learning
    from .db import validate_system_id
    validate_system_id(system_id)
    detection = schema_drift.run_drift_check(system_id) if detect else None
    report = {"system_id": system_id, "detection": detection, "candidates": 0,
              "materialised": 0, "failed": [], "failed_count": 0, "remaining": 0,
              "pending_unreviewed": 0}
    if detection and str(detection.get("status") or "").startswith("skipped"):
        return report
    pending = self_learning.get_pending_mappings()
    rows = _marked_rows(system_id, pending)
    batch = rows[:REPAIR_ACCEPT_CAP]
    materialised, failed = _accept_rows(batch, actor)
    marked_ids = {r.get("mapping_id") for r in rows}
    report.update(candidates=len(rows), materialised=materialised,
                  failed=failed[:_FAILED_DETAIL_CAP], failed_count=len(failed),
                  remaining=len(rows) - len(batch),
                  pending_unreviewed=sum(1 for r in _drift_rows(system_id, pending)
                                         if r.get("mapping_id") not in marked_ids))
    logger.info("schema repair for %s: %d candidate(s), %d materialised, %d failed",
                system_id, len(rows), materialised, len(failed))
    return report
