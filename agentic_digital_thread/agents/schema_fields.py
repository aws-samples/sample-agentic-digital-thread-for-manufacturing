# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ONE SchemaEntry/Field physical-schema writer home (ADR-0077 D1).

Shared by onboarding's b2a step (``onboard._dp_athena_physical_fields``) and
the steward's accept of an additive ``schema_drift`` proposal
(``schema_repair.materialise_drift``) so both write byte-identical graph
shape. The Cypher text moved here UNCHANGED from the pre-ADR-0077 b2a writer
(pinned by ``tests/unit/test_schema_fields.py``).

Import-pure: stdlib + the pure ``metric_binding`` column-identifier home.
The graph seams are REQUIRED keyword arguments (``write=`` / ``query=``) —
this module never imports ``db``, so each caller keeps its own monkeypatch
target (``onboard.neptune_write`` for provision; ``self_learning``'s loud
seams for the accept).
"""
from __future__ import annotations

import re
from typing import Callable

from .metric_binding import validate_column_name

# Byte-identical to the pre-ADR-0077 onboard._dp_athena_physical_fields Cypher.
SCHEMA_ENTRY_MERGE = (
    "MERGE (se:SchemaEntry {id: $seid}) SET se.name = $name, se.system_id = $sid "
    "WITH se MERGE (s:System {id: $sid}) ON CREATE SET s.name = $sid, s.status = 'onboarded' "
    "MERGE (s)-[:HAS_SCHEMA]->(se)")
FIELD_MERGE = (
    "MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
    "f.system_id = $sid, f.is_partition = $isp, f.data_type = $dtype")
HAS_FIELD_MERGE = (
    "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) MERGE (se)-[:HAS_FIELD]->(f)")

# ADR-0077 D3 — per-table discovery completeness (absent = unknown).
DISCOVERY_COMPLETENESS_SET = (
    "MATCH (se:SchemaEntry {id: $seid}) "
    "SET se.discovery_complete = $complete, se.discovered_columns = $count")

# Exact readback / existence probes (validate → MERGE → readback).
FIELD_READBACK = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid})"
    "-[:HAS_FIELD]->(f:Field {id: $fid}) "
    "RETURN f.name AS name, f.schema_id AS schema_id, f.system_id AS system_id, "
    "f.data_type AS data_type, f.is_partition AS is_partition")
SCHEMA_ENTRY_READBACK = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "RETURN se.name AS name, se.system_id AS system_id")
SCHEMA_ENTRY_EXISTS = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "RETURN count(se) AS n")
TABLE_FIELD_NAMES = (
    "MATCH (se:SchemaEntry {id: $seid})-[:HAS_FIELD]->(f:Field) RETURN f.name AS name")
SYSTEM_SCHEMA_ENTRY_NAMES = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry) RETURN se.name AS name")

# One table token: the proposal_scoring._FIELD_SEGMENT_PATTERN register
# (letters, digits, ``_``, ``$``, ``-``) with its 128-char cap. No dot — a
# dotted table would make ``schema_entry_id``/``field_id`` ambiguous.
_TABLE_PATTERN = re.compile(r"[A-Za-z0-9_$-]{1,128}")

Write = Callable[..., object]
Query = Callable[..., list]


def schema_entry_id(system_id: str, table: str) -> str:
    """The SchemaEntry id onboarding writes (``<system>.<table>``)."""
    return f"{system_id}.{table}"


def field_id(system_id: str, table: str, column: str) -> str:
    """The Field id onboarding writes (``<system>.<table>.<column>``)."""
    return f"{system_id}.{table}.{column}"


def validate_table_name(name) -> str:
    """Return ``name`` when it is ONE table token; ``ValueError`` otherwise."""
    if not isinstance(name, str) or not _TABLE_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid table name: {name!r}")
    return name


def validate_physical_column(col) -> dict:
    """A NEW ``{name, data_type, is_partition}`` dict from a source column.

    The name goes through the ONE column-identifier home (ADR-0076 D4 —
    dotted struct leaves pass, anything else fails closed)."""
    if not isinstance(col, dict):
        raise ValueError(f"Invalid physical column: {col!r}")
    return {"name": validate_column_name(col.get("name")),
            "data_type": str(col.get("data_type") or ""),
            "is_partition": bool(col.get("is_partition"))}


def write_schema_entry(system_id: str, table: str, *, write: Write) -> str:
    """MERGE the SchemaEntry + its System/HAS_SCHEMA; returns the entry id."""
    seid = schema_entry_id(system_id, table)
    write(SCHEMA_ENTRY_MERGE, {"seid": seid, "name": table, "sid": system_id})
    return seid


def write_physical_field(system_id: str, table: str, col: dict, *,
                         write: Write) -> str:
    """MERGE one Field + its HAS_FIELD edge; returns the Field id."""
    cname = col["name"]
    seid = schema_entry_id(system_id, table)
    fid = field_id(system_id, table, cname)
    write(FIELD_MERGE,
          {"fid": fid, "name": cname, "schema_id": seid, "sid": system_id,
           "isp": bool(col.get("is_partition")), "dtype": col.get("data_type", "")})
    write(HAS_FIELD_MERGE, {"seid": seid, "fid": fid})
    return fid


def write_discovery_completeness(system_id: str, table: str, complete: bool,
                                 discovered: int, *, write: Write) -> None:
    """SET ``se.discovery_complete`` / ``se.discovered_columns`` (ADR-0077 D3)."""
    write(DISCOVERY_COMPLETENESS_SET,
          {"seid": schema_entry_id(system_id, table),
           "complete": bool(complete), "count": int(discovered)})


def read_field(system_id: str, table: str, column: str, *,
               query: Query) -> dict | None:
    """Exact readback of one Field through its System/SchemaEntry path."""
    rows = query(FIELD_READBACK, {"sid": system_id,
                                  "seid": schema_entry_id(system_id, table),
                                  "fid": field_id(system_id, table, column)})
    return rows[0] if rows else None


def expected_field(system_id: str, table: str, col: dict) -> dict:
    """The EXACT Field property shape ``write_physical_field`` MERGEs."""
    return {"name": col["name"], "schema_id": schema_entry_id(system_id, table),
            "system_id": system_id, "data_type": str(col.get("data_type") or ""),
            "is_partition": bool(col.get("is_partition"))}


def field_mismatches(expected: dict, row: dict | None) -> list:
    """The property names whose read-back value differs from ``expected``
    (every key when the row is missing). ``is_partition`` compares by
    identity to ``True``/``False`` so a non-boolean read-back never passes."""
    if not row:
        return sorted(expected)
    out = []
    for key, want in expected.items():
        got = row.get(key)
        same = (got is want) if isinstance(want, bool) else (got == want)
        if not same:
            out.append(key)
    return sorted(out)


def read_schema_entry(system_id: str, table: str, *, query: Query) -> dict | None:
    """Exact readback of the SchemaEntry through its System/HAS_SCHEMA path."""
    rows = query(SCHEMA_ENTRY_READBACK, {"sid": system_id,
                                         "seid": schema_entry_id(system_id, table)})
    return rows[0] if rows else None


def schema_entry_exists(system_id: str, table: str, *, query: Query) -> bool:
    """True when the system owns the table's SchemaEntry in the graph."""
    rows = query(SCHEMA_ENTRY_EXISTS, {"sid": system_id,
                                       "seid": schema_entry_id(system_id, table)})
    return bool(rows) and int((rows[0] or {}).get("n") or 0) >= 1


def table_field_names(system_id: str, table: str, *, query: Query) -> set:
    """The Field names currently under the table's SchemaEntry."""
    rows = query(TABLE_FIELD_NAMES, {"seid": schema_entry_id(system_id, table)})
    return {str(r.get("name")) for r in rows or [] if r.get("name")}


def list_schema_entry_names(system_id: str, *, query: Query) -> list[str]:
    """Every SchemaEntry name the system owns, in a stable order. A SchemaEntry
    ``name`` is what onboarding wrote: a single table token for Athena, the
    namespace-qualified name for S3 Tables (``<namespace>.<table>``)."""
    rows = query(SYSTEM_SCHEMA_ENTRY_NAMES, {"sid": system_id})
    names = [str(r.get("name")) for r in rows or [] if r and r.get("name")]
    return sorted(dict.fromkeys(names))


def terminal_table_name(name: str) -> str:
    """The guard-facing terminal name of a SchemaEntry name: the last dotted
    part (``a.b.c`` -> ``c``), the name unchanged when it has no dot. This is
    the key the steward-declared table facts are remembered under and the name
    a guarded query references, so a qualified SchemaEntry and the terminal a
    query names resolve to the same fact (the collision rule)."""
    return str(name or "").rpartition(".")[2] or str(name or "")


class TableNameAmbiguous(ValueError):
    """A submitted table name matched more than one of the system's SchemaEntry
    names by its terminal part, so it cannot be resolved to one entry."""


def resolve_schema_entry_name(system_id: str, submitted: str, *, query: Query) -> str | None:
    """Resolve a steward-submitted table name to the system's actual SchemaEntry
    name, accepting either the terminal or the fully-qualified form.

    An EXACT match to a SchemaEntry name wins. A qualified (dotted) name must
    match exactly, so a wrong namespace is an unknown table, never another
    namespace's entry. A terminal-only name is matched against every entry's
    terminal; exactly one match resolves, more than one raises
    :class:`TableNameAmbiguous`, none returns ``None`` (an unknown table). For a
    single-part (Athena) name the exact match is the only path, so Athena
    resolution is unchanged."""
    names = list_schema_entry_names(system_id, query=query)
    if submitted in names:
        return submitted
    if "." in submitted:
        return None
    want = terminal_table_name(submitted)
    matches = [n for n in names if terminal_table_name(n) == want]
    if len(matches) > 1:
        raise TableNameAmbiguous(
            f"table name {submitted!r} matches more than one schema entry: "
            f"{', '.join(matches)}")
    return matches[0] if matches else None
