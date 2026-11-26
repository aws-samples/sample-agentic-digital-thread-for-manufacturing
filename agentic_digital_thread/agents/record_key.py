# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A table's declared RECORD KEY, and the de-duplication of records it refuses.

Measured on an analytics deployment, in a live dry run ("how many
segments did cell C1 complete yesterday?"): a SQL system agent first ran the right
COUNT(*) of COMPLETED rows on the working day, then ran

    WITH latest AS (SELECT id, revision, state, complete_date,
      ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn
      FROM records WHERE cell = 'C1' AND complete_date >= ... AND complete_date < ...)
    SELECT COUNT(DISTINCT id) AS n FROM latest WHERE rn = 1 AND state = 'COMPLETED'

and answered a smaller number, calling the row count "revisions, not distinct
segments".
That is wrong: the source owner gave the record key as ``id, segment_type,
entity_type, revision, cell, COALESCE(manufacturing_attempt_id, ''),
COALESCE(lifecycle_id, '')`` -- each revision row is its own record, and ``id``
is a strict subset of the key. An Athena/Iceberg table has no primary-key
constraint, so onboarding cannot read one; the owner's statement is the only
source.

This module is three things, each the analog of ``record_time`` /
``storage_bound_rule`` / ``business_context``:

1. The steward-set table fact (:func:`set_record_key` / :func:`read_record_key`
   / :func:`clear_record_key`): an ordered list of physical columns (each a
   ``{column, coalesced}``), stored on the table's ``SchemaEntry`` with
   provenance (who, when, source text). Validation: every column is a ``Field``
   of that table, no duplicates, 1..12 columns. The key applies to a backend
   that has no catalog key of its own (Athena today); a backend whose keys come
   from its catalog (PostgreSQL) is refused at the route.

2. The per-system registry the system agent reads (:func:`remember` /
   :func:`columns_for` / :func:`keys_for`), filled at bake time from the graph
   for each table of the baked table list (:func:`remember_from_graph`, which
   reads the list through ``system_agent.desc_table_names``, the reader the row
   exclusion shares), and the schema
   line it bakes (:func:`schema_lines`), a FACT the agent sees, not an
   instruction. The registry keeps the ``coalesced`` flag, and the line shows a
   coalesced column as ``COALESCE(col, '')`` the way the owner wrote the key. No
   key -> no line.

3. The engine check (:func:`decide`), refuse-once like ``record_time``, over
   ONE deliberately small grammar: a named CTE that reads exactly the one
   remembered table and DIRECTLY projects
   ``ROW_NUMBER() OVER (PARTITION BY <subset>) AS <alias>`` with ``<subset>`` a
   STRICT SUBSET of the key (each term a bare / sole-relation-qualified column
   or ``COALESCE(<key column>, '')``); one outer query that reads that CTE
   DIRECTLY in its FROM; that outer query's WHERE an AND-only predicate one
   of whose conjuncts is exactly ``<alias> = 1`` or ``<cte>.<alias> = 1`` (after
   stripping balanced parentheses); and that outer query aggregating the kept
   rows by their multiplicity (``COUNT`` in any form, ``SUM``, ``AVG``, a
   percentile, ``array_agg`` ...) in its SELECT list or HAVING, at its own
   level. NOT checked (no decision): the latest row per group read for its
   values (no aggregate, or only ``MIN`` / ``MAX``), an aggregate under
   ``OVER`` or in a nested query block, ``RANK`` /
   ``DENSE_RANK`` (ties keep more than one row), ``COUNT(DISTINCT x)``,
   ``SELECT DISTINCT`` / ``GROUP BY`` of a subset, a partition by the FULL key,
   a derived-table owner, ``QUALIFY``, a pass-through CTE, any ``OR`` / ``NOT``
   in that WHERE, and any uncertain tokenisation. The refusal states the key and
   that rows sharing the subset are separate records; the identical resend runs
   and is disclosed (:func:`engine_line` / :func:`requirement_block`), silently
   refused instead under ``RESPONSE_INTEGRITY_MODE=enforce``. A query that
   aggregates the kept rows without counting them is not said to have
   counted.

Names are printed only through ``engine_names``. Generic: no deployment names.
Pure apart from the per-system registry the system agent fills at construction,
and the injected graph seams (``write=`` / ``query=``), the ``schema_fields``
convention.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from .athena_scope import _strip_outer, relations, tokenize
from .storage_bound_rule import StorageBound, StorageBoundError

ERROR_NAME = "RECORD_KEY_DEDUPE"
REASON_RECORD_KEY = "dedupe_subset_of_record_key"
#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] Record key"
MARKER = "a subset of the record key"
#: The most columns a steward may declare as one record key.
MAX_KEY_COLUMNS = 12
#: The steward's source text is kept, length-capped, never printed to the agent.
_MAX_SOURCE_LEN = 2000
#: The ONE window function whose row number filtered to ``= 1`` keeps exactly
#: one row per partition group. RANK / DENSE_RANK keep ties, so they are not
#: read: a refusal would falsely say rows were collapsed to one.
_RANK_FUNC = "row_number"

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_keys: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# The per-system registry (parallels state_times / storage_bounds / record_time)
# --------------------------------------------------------------------------- #
def _terms(columns) -> list:
    """Normalize a remembered key to a list of ``{column, coalesced}`` terms:
    a bare string is a non-coalesced column, a dict carries its flag."""
    out = []
    for item in columns or ():
        if isinstance(item, str):
            out.append({"column": item.strip().lower(), "coalesced": False})
        elif isinstance(item, dict):
            name = str(item.get("column") or item.get("name") or "").strip().lower()
            if name:
                out.append({"column": name, "coalesced": bool(item.get("coalesced"))})
    return out


def remember(system_id: str, table_columns: dict) -> None:
    """Keep ``system_id``'s declared record keys, ``{table: [term, ...]}`` where
    a term is a column string or a ``{column, coalesced}`` dict. An empty map
    forgets the system."""
    if not system_id:
        return
    with _lock:
        cleaned = {str(t).lower(): _terms(cols) for t, cols in (table_columns or {}).items()}
        cleaned = {t: terms for t, terms in cleaned.items() if terms}
        if cleaned:
            _keys[system_id] = cleaned
        else:
            _keys.pop(system_id, None)


def columns_for(system_id: str, table: str, *, quoted: bool = False) -> list:
    """The key's column NAMES for ``table`` (the subset check compares names).

    Table names are remembered as the physical (lowercased) graph names. An
    UNQUOTED table reference is case-insensitive in the engine, so it is looked
    up case-folded; a QUOTED reference is case-sensitive, so it is matched
    exactly (a quoted name of a different case does not match)."""
    lookup = str(table) if quoted else str(table).lower()
    with _lock:
        return [t["column"] for t in _keys.get(system_id, {}).get(lookup, [])]


def keys_for(system_id: str) -> dict:
    """``{table: [term, ...]}`` for the system (terms keep the coalesced flag)."""
    with _lock:
        return {t: [dict(term) for term in terms]
                for t, terms in _keys.get(system_id, {}).items()}


def has_keys(system_id: str) -> bool:
    with _lock:
        return bool(_keys.get(system_id))


def reset() -> None:
    """Forget every system's record keys (tests)."""
    with _lock:
        _keys.clear()


def _graph_query():
    """The graph read seam (lazy import, patchable in tests). Raises on an
    import failure so the caller treats it as a bake fault."""
    from .db import neptune_query
    return neptune_query


def _described_tables(tables_desc: str) -> list:
    """The table names of the baked table list, read by the ONE reader the
    steward-declared table facts share (``system_agent.desc_table_names``,
    which drops a relation-kind marker such as ``[base_table]`` and never
    reads a type's parentheses as a table)."""
    from .system_agent import desc_table_names
    return desc_table_names(tables_desc)


def remember_from_graph(system_id: str, tables_desc: str) -> None:
    """Read each table in ``tables_desc`` for a steward-declared record key and
    replace the system's remembered keys in ONE assignment.

    Isolated from the sibling registries and atomic: the map is built locally
    and only then handed to :func:`remember`, so EVERY failure path -- the
    table list, the graph import, the query, a parse -- leaves the system with
    NO key, never the prior one. Only the DECLARED key is baked (an
    Athena/Iceberg table has no catalog key; a PostgreSQL system never carries
    a declared key, the route refuses it). Fail-soft: a fault clears the
    system's key."""
    try:
        tables = _described_tables(tables_desc)
    except Exception:  # noqa: BLE001 - an unreadable list, no key (never the prior one)
        remember(system_id, {})
        return
    if not tables:
        remember(system_id, {})
        return
    try:
        query = _graph_query()
    except Exception:  # noqa: BLE001 - no graph seam, no key (never the prior one)
        remember(system_id, {})
        return
    out: dict = {}
    try:
        from . import schema_fields
        for table in tables:
            # ``tables`` are the guard-facing TERMINAL names (desc_table_names).
            # The SchemaEntry is stored under its real name, which for S3 Tables
            # is the namespace-qualified form; resolve to it for the READ, but
            # remember the key under the terminal name the guard looks up. For
            # an Athena single-part name the resolved name is the same.
            real = schema_fields.resolve_schema_entry_name(
                system_id, table, query=query) or table
            terms = read_record_key(system_id, real, query=query).get("declared_key", [])
            if terms:
                out[table] = terms
    except Exception:  # noqa: BLE001 - any read/parse fault clears the system's key
        remember(system_id, {})
        return
    remember(system_id, out)


# --------------------------------------------------------------------------- #
# The steward-set table fact (SchemaEntry provenance)
# --------------------------------------------------------------------------- #
def _normalize_key(record_key) -> list | None:
    """``record_key`` as a list of ``{name, coalesced}`` (a bare string element
    means ``coalesced=False``), else ``None`` for a malformed element."""
    if not isinstance(record_key, (list, tuple)):
        return None
    out = []
    for item in record_key:
        if isinstance(item, str):
            out.append({"column": item.strip().lower(), "coalesced": False})
            continue
        if isinstance(item, dict):
            name = str(item.get("column") or item.get("name") or "").strip().lower()
            if name:
                out.append({"column": name, "coalesced": bool(item.get("coalesced"))})
                continue
        return None
    return out


def _validation_error(key: list, columns: set) -> tuple | None:
    """``(reason, detail)`` for an invalid key, else ``None``. ``columns`` are
    the table's Field names (lowercased)."""
    if not key:
        return "empty_key", "a record key needs at least one column"
    if len(key) > MAX_KEY_COLUMNS:
        return "too_many_columns", f"a record key has at most {MAX_KEY_COLUMNS} columns"
    names = [c["column"] for c in key]
    if len(set(names)) != len(names):
        return "duplicate_column", "a record key lists each column at most once"
    missing = [n for n in names if n not in columns]
    if missing:
        return "unknown_column", f"not columns of this table: {', '.join(sorted(missing))}"
    return None


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SET_RECORD_KEY = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "SET se.record_key = $record_key, se.record_key_by = $by, "
    "se.record_key_at = $at, se.record_key_source = $source")
READ_RECORD_KEY = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "RETURN se.record_key AS record_key, se.record_key_by AS record_key_by, "
    "se.record_key_at AS record_key_at, se.record_key_source AS record_key_source")


def set_record_key(system_id: str, table: str, record_key, *, actor: str = "",
                   source: str = "", write, query) -> dict:
    """Validate and store a table's record key on its ``SchemaEntry`` with
    provenance. Returns ``{"status": "ok", ...}`` or ``{"status":
    "validation_failed", "reason": ..., "detail": ...}``; nothing is written on
    a validation failure. ``write`` / ``query`` are the graph seams."""
    from . import schema_fields
    normalized = _normalize_key(record_key)
    if normalized is None:
        return {"status": "validation_failed", "reason": "invalid_input",
                "detail": "record_key must be a list of columns"}
    if not schema_fields.schema_entry_exists(system_id, table, query=query):
        return {"status": "validation_failed", "reason": "unknown_table",
                "detail": "this system has no such table"}
    columns = {c.lower() for c in schema_fields.table_field_names(system_id, table, query=query)}
    err = _validation_error(normalized, columns)
    if err:
        return {"status": "validation_failed", "reason": err[0], "detail": err[1]}
    write(SET_RECORD_KEY, {"sid": system_id,
                           "seid": schema_fields.schema_entry_id(system_id, table),
                           "record_key": json.dumps(normalized),
                           "by": str(actor or ""), "at": _utc_now_iso(),
                           "source": str(source or "")[:_MAX_SOURCE_LEN]})
    return {"status": "ok", "system_id": system_id, "table": table,
            "record_key": normalized}


def clear_record_key(system_id: str, table: str, *, actor: str = "", write, query) -> dict:
    """Remove a table's declared record key (its provenance with it)."""
    from . import schema_fields
    if not schema_fields.schema_entry_exists(system_id, table, query=query):
        return {"status": "validation_failed", "reason": "unknown_table",
                "detail": "this system has no such table"}
    write(SET_RECORD_KEY, {"sid": system_id,
                           "seid": schema_fields.schema_entry_id(system_id, table),
                           "record_key": None, "by": str(actor or ""),
                           "at": _utc_now_iso(), "source": None})
    return {"status": "ok", "system_id": system_id, "table": table}


def read_record_key(system_id: str, table: str, *, query) -> dict:
    """The table's declared key (a list of ``{column, coalesced}`` terms) and
    its provenance. The declared key applies to a backend with no catalog key
    of its own; a catalog-keyed backend is refused at the route, so there is no
    measured-key precedence here."""
    from . import schema_fields
    rows = query(READ_RECORD_KEY, {"sid": system_id,
                                   "seid": schema_fields.schema_entry_id(system_id, table)})
    row = (rows[0] if rows else {}) or {}
    return {"system_id": system_id, "table": table,
            "declared_key": _parse_stored(row.get("record_key")),
            "record_key_by": row.get("record_key_by"),
            "record_key_at": row.get("record_key_at"),
            "record_key_source": row.get("record_key_source")}


def _parse_stored(value) -> list:
    """A stored ``record_key`` JSON string back to a ``[{column, coalesced}]``
    list, ``[]`` for unset or malformed."""
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return _normalize_key(parsed) or []


# --------------------------------------------------------------------------- #
# Carry a declared record key across a re-provision's schema rewrite
# --------------------------------------------------------------------------- #
# The record key is a steward fact stored on the table's SchemaEntry
# (``se.record_key``). A re-provision DELETEs the schema subgraph and MERGEs it
# fresh, so without a carry the key is lost (both engines). ``take_for_rewrite``
# snapshots the RAW stored value and its provenance before the delete;
# ``restore_after_rewrite`` writes each one back VERBATIM after the replacement
# SchemaEntries and Fields exist, keyed by the SchemaEntry name (stable across
# the rewrite), preserving the original ``by``/``at``/``source``.
_READ_RAW_RECORD_KEY = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "RETURN se.record_key AS record_key, se.record_key_by AS record_key_by, "
    "se.record_key_at AS record_key_at, se.record_key_source AS record_key_source")
# The DURABLE carry: a snapshot of the record keys a re-provision is carrying
# across its schema rewrite, stored on the System node (which the schema delete
# never touches) so a FAILED provision's retry recovers it after the
# SchemaEntries are gone, matching the row-exclusion rewrite marker (review
# live-carry-r2 F1). JSON ``{table: {record_key, record_key_by, record_key_at,
# record_key_source}}``; absent/null = no carry in flight.
_READ_REWRITE_CARRY = (
    "MATCH (s:System {id: $sid}) RETURN s.record_key_rewrite AS record_key_rewrite")
_WRITE_REWRITE_CARRY = (
    "MATCH (s:System {id: $sid}) SET s.record_key_rewrite = $carry")


def _parse_carry(raw) -> dict:
    """A stored rewrite-carry JSON string back to ``{table: record}``; ``{}``
    for unset or malformed."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def take_for_rewrite(system_id: str, tables, *, query) -> dict:
    """A snapshot of each table's RAW stored record key and provenance, taken
    BEFORE a schema rewrite deletes the SchemaEntries it lives on. Returns
    ``{table: {record_key, record_key_by, record_key_at, record_key_source}}``
    for every table that has a stored key; tables with none are omitted.

    A DURABLE carry a prior (failed) provision persisted on the System node
    (:func:`persist_rewrite_carry`) seeds the snapshot: a retry after the schema
    was deleted recovers the keys from it, not from the now-empty SchemaEntries.
    The CURRENT SchemaEntry, read CHECKED, takes precedence over the carry per
    table:

    * a non-empty current key SUPERSEDES the carried one (a steward edit between
      the first attempt and the retry is not lost to a stale carry);
    * a current row that EXISTS with no key is a steward CLEAR -- that table is
      dropped from the carry so the next rewrite cannot resurrect the old key;
    * only when the SchemaEntry row is ABSENT (the interrupted rewrite already
      deleted it) is the carried entry preserved.

    A faulting checked read raises rather than silently dropping a key. The
    merged snapshot is what the caller re-persists, replacing the stale carry
    before the delete. The graph read seam is injected so the provision keeps
    its own monkeypatch target."""
    from . import schema_fields
    out = dict(_parse_carry(
        ((query(_READ_REWRITE_CARRY, {"sid": system_id}) or [{}])[0] or {}).get("record_key_rewrite")))
    for table in tables or ():
        rows = query(_READ_RAW_RECORD_KEY,
                     {"sid": system_id, "seid": schema_fields.schema_entry_id(system_id, table)})
        if not rows:
            continue  # SchemaEntry already gone: keep the carried entry
        row = rows[0] or {}
        if row.get("record_key"):
            out[table] = {"record_key": row.get("record_key"),
                          "record_key_by": row.get("record_key_by"),
                          "record_key_at": row.get("record_key_at"),
                          "record_key_source": row.get("record_key_source")}
        else:
            out.pop(table, None)  # steward cleared the key: drop any stale carry
    return out


def persist_rewrite_carry(system_id: str, carried: dict, *, write) -> None:
    """Store the carried record keys durably on the System node, so a failed
    re-provision's retry can restore them after the SchemaEntries were deleted
    (review live-carry-r2 F1). A no-op for an empty carry."""
    if carried:
        write(_WRITE_REWRITE_CARRY, {"sid": system_id, "carry": json.dumps(carried)})


def clear_rewrite_carry(system_id: str, *, write) -> None:
    """Remove the durable rewrite carry once every table was restored, so the
    next re-provision snapshots fresh (review live-carry-r2 F1)."""
    write(_WRITE_REWRITE_CARRY, {"sid": system_id, "carry": None})


def restore_after_rewrite(system_id: str, carried: dict, *, write, query) -> dict:
    """Write each snapshotted record key back onto its recreated SchemaEntry
    after a re-provision's schema rewrite, so the steward's declared key
    survives. Returns ``{restored, dropped, missing}`` -> sorted table names.

    Each table is checked against the GRAPH, not this run's memory (review
    live-carry F2):

    * ``missing``: the table has no SchemaEntry in the new schema. Nothing is
      written; its key is not carried.
    * ``dropped``: at least one key column no longer exists among the table's
      recreated Fields. Nothing is written (a stored key with a non-existent
      column is invalid); the drop is logged and recorded as not carried.
    * ``restored``: every key column still exists, so the raw value and its
      original provenance are written back VERBATIM and the write is CONFIRMED
      (the ``MATCH`` updated the SchemaEntry). Not re-stamped: it is the
      rewrite's own write.

    Never overwrites a NEWER steward value: a table whose recreated SchemaEntry
    already carries a record key (a steward set one after the recreate) is left
    alone (``skipped``). With the record-key PUT refused while the rewrite
    marker stands this is belt-and-suspenders, but it also makes the restore
    CAS-safe when called on its own (review live-carry F3).

    A table NOT in ``restored`` is never counted as carried, so a caller must
    not report the carry complete when any table is missing, dropped or held.

    ``query`` MUST be a CHECKED read (``neptune_query_checked``): every restore
    decision reads the graph, and an unreadable existence, current-key or Field
    read must leave the table ``held`` and write nothing, never silently treat a
    transient ``[]`` as "no newer key" and overwrite a present steward key
    (review live-carry-r2 F2)."""
    from . import schema_fields
    out: dict = {"restored": [], "dropped": [], "missing": [], "skipped": [], "held": []}
    for table, record in (carried or {}).items():
        bucket = _record_key_restore_bucket(system_id, table, record, query=query)
        if bucket != "restored":
            out[bucket].append(table)
            continue
        confirmed = write(SET_RECORD_KEY,       # the resubmit-safe write (pinned)
                          {"sid": system_id,
                           "seid": schema_fields.schema_entry_id(system_id, table),
                           "record_key": record.get("record_key"),
                           "by": record.get("record_key_by") or "",
                           "at": record.get("record_key_at") or _utc_now_iso(),
                           "source": record.get("record_key_source")})
        out["restored" if confirmed is not False else "missing"].append(table)
    return {k: sorted(v) for k, v in out.items() if v}


def _record_key_restore_bucket(system_id: str, table: str, record: dict, *, query) -> str:
    """Whether a carried record key should be written back: ``missing`` (no
    recreated SchemaEntry), ``skipped`` (a newer steward key already present),
    ``dropped`` (a key column no longer exists), ``held`` (a graph read was
    unavailable, so the decision cannot be made safely), or ``restored`` (write
    it). Read-only, and every read is CHECKED: a read fault returns ``held`` so
    nothing is written and the carry is left for the next attempt (review
    live-carry-r2 F2)."""
    from . import schema_fields
    from .db import GraphQueryError
    try:
        if not schema_fields.schema_entry_exists(system_id, table, query=query):
            return "missing"
        if read_record_key(system_id, table, query=query).get("declared_key"):
            logger.info("record key of %s.%s not carried: a newer steward key is already "
                        "on the recreated table", system_id, table)
            return "skipped"                  # a newer steward key: never overwrite it
        columns = {c.lower()
                   for c in schema_fields.table_field_names(system_id, table, query=query)}
    except GraphQueryError:
        logger.warning("record key of %s.%s not carried yet: a graph read was unavailable; "
                       "left for the next re-provision", system_id, table)
        return "held"
    key = _parse_stored(record.get("record_key"))
    missing_cols = [c["column"] for c in key if c["column"] not in columns]
    if not key or missing_cols:
        logger.warning("record key of %s.%s not carried across the schema rewrite: its "
                       "column(s) no longer exist in the recreated table (%d column(s) gone)",
                       system_id, table, len(missing_cols))
        return "dropped"
    return "restored"


# --------------------------------------------------------------------------- #
# The schema fact line the system agent bakes
# --------------------------------------------------------------------------- #
def schema_lines(table_columns: dict) -> str:
    """One fact line per table with a record key, ``""`` for none. ``table_columns``
    is ``{table: [term, ...]}`` (a term is a column string or a ``{column,
    coalesced}`` dict). The line states the key -- a coalesced column shown as
    ``COALESCE(col, '')`` the way the owner wrote it -- and that a COUNT(*) of
    the table's rows counts records. Names print only through
    ``engine_names.printable``; a withheld name is counted."""
    from .engine_names import Withheld
    entries = {t: _terms(cols) for t, cols in (table_columns or {}).items()}
    entries = {t: terms for t, terms in entries.items() if terms}
    if not entries:
        return ""
    names = Withheld()
    parts = []
    for table, terms in entries.items():
        shown = ", ".join(_render_term(names, term) for term in terms)
        parts.append(f"{names.show(table, 'table')}: record key {shown}")
    body = "; ".join(parts)
    return ("\n\nRECORD KEYS (facts): each row of these tables is its own record, "
            f"identified by the table's record key, so a COUNT(*) of the table's rows "
            f"counts records -- {body}. Rows that share only a subset of a record key "
            f"are separate records, not duplicates.{names.sentence()}")


def _render_term(names, term: dict) -> str:
    """A key term for the schema line: ``COALESCE(col, '')`` when coalesced,
    else the bare column, each printed through the withheld-name path."""
    shown = names.show(term["column"])
    return f"COALESCE({shown}, '')" if term.get("coalesced") else shown


# --------------------------------------------------------------------------- #
# Reading the query -- the one shape: ROW_NUMBER() OVER (PARTITION BY <subset>)
# whose alias the consuming query filters to = 1.
# --------------------------------------------------------------------------- #
def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _matching(tokens, open_index: int):
    """The index of the ``)`` that closes the ``(`` at ``open_index``."""
    depth = 0
    for i in range(open_index, len(tokens)):
        if tokens[i].text == "(":
            depth += 1
        elif tokens[i].text == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _relation_names(scans) -> set:
    """The lowercased names a column qualifier may legitimately use for the
    sole relation: its alias and each part of its (possibly qualified) table
    name, so ``r.id`` over ``FROM records r`` and ``lake.records`` both bind."""
    relation = scans[0]
    names = {str(relation.alias).lower()} if relation.alias else set()
    names.update(str(p).lower() for p in relation.parts)
    names.discard("")
    return names


def _resolve_column(spelling: str, relation_names: set) -> str | None:
    """A partition term spelled as a column, resolved to its terminal column
    name (lowercased) when a qualifier binds to the sole relation, else
    ``None``. ``a.b`` resolves to ``b`` only when ``a`` is the relation's alias
    or a part of its table name; a qualifier that binds to nothing is not
    read."""
    name = str(spelling or "").lower()
    if "." not in name:
        return name or None
    qualifier, _dot, column = name.rpartition(".")
    head = qualifier.split(".")[0]
    return column if head in relation_names and column else None


def _partition_subset(over_inner, relation_names: set) -> list | None:
    """The key-eligible columns of ``PARTITION BY <cols> [ORDER BY ...]``: each
    item is a bare/qualified column OR ``COALESCE(<column>, '')``; a qualifier
    must bind to the sole relation. ``None`` when there is no PARTITION BY or
    any item is some other expression (no refusal on an unreadable item)."""
    from .athena_exact_shape import _split_commas
    base = min((t.depth for t in over_inner), default=0)
    at = next((i for i, t in enumerate(over_inner)
               if t.depth == base and _upper(t) == "PARTITION"), None)
    if at is None or at + 1 >= len(over_inner) or _upper(over_inner[at + 1]) != "BY":
        return None
    order = next((i for i in range(at + 2, len(over_inner))
                  if over_inner[i].depth == base and _upper(over_inner[i]) == "ORDER"),
                 len(over_inner))
    out = []
    for item in _split_commas(over_inner[at + 2:order]):
        spelling = _partition_item(item)
        resolved = _resolve_column(spelling, relation_names) if spelling else None
        if not resolved:
            return None
        out.append(resolved)
    return out or None


def _partition_item(tokens) -> str | None:
    """One PARTITION BY item as a column spelling: a bare/qualified column, or
    ``COALESCE(<column>, '')`` (read as that column). ``None`` for any other
    expression."""
    from .record_time import _chain
    stripped = _strip_outer(tokens)
    chain = _chain(stripped)
    if chain:
        return chain
    return _coalesce_column(stripped)


def _coalesce_column(tokens) -> str | None:
    """The column of ``COALESCE(<column>, '')`` (case-insensitive), else
    ``None``. Only the two-argument empty-string form is read."""
    from .athena_exact_shape import _split_commas
    from .record_time import _chain
    if (len(tokens) < 4 or tokens[0].kind != "word"
            or tokens[0].value.lower() != "coalesce" or tokens[1].text != "("):
        return None
    if _matching(tokens, 1) != len(tokens) - 1:
        return None
    args = _split_commas(tokens[2:-1])
    if len(args) != 2:
        return None
    second = args[1]
    if not (len(second) == 1 and second[0].kind == "literal"
            and _empty_string_literal(second[0])):
        return None
    return _chain(args[0])


def _empty_string_literal(token) -> bool:
    """True when the string literal ``token`` decodes to exactly the empty
    string. The tokenizer stores ``"literal"`` in ``value``, so the decoded
    value is read from the raw source text: a single-quoted literal whose body
    (quotes stripped) is empty."""
    text = str(token.text)
    if len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return False
    return text[1:-1] == ""


def _window_in_cte_body(tokens, body_open: int, body_close: int, relation_names: set):
    """``(partition subset, alias)`` when the CTE body between ``body_open`` and
    ``body_close`` DIRECTLY projects a single
    ``ROW_NUMBER() OVER (PARTITION BY <subset>) AS <alias>`` at the body's own
    select, else ``None``. The window must be at the body's top depth (not in a
    nested subquery), the subset a readable list of key-eligible columns."""
    body_depth = tokens[body_open].depth + 1
    for i in range(body_open + 1, body_close):
        token = tokens[i]
        if (token.depth == body_depth and token.kind == "word"
                and token.value.lower() == _RANK_FUNC):
            found = _row_number(tokens, i, body_close, relation_names)
            if found is not None:
                return found
    return None


def _row_number(tokens, func_index: int, limit: int, relation_names: set):
    """``(partition subset, alias)`` for a ``ROW_NUMBER() OVER (PARTITION BY
    ...) AS <alias>`` at ``func_index`` (its OVER and alias within ``limit``),
    else ``None``."""
    n = len(tokens)
    if func_index + 1 >= n or tokens[func_index + 1].text != "(":
        return None
    close = _matching(tokens, func_index + 1)
    if close is None or close + 2 >= n or close + 2 >= limit \
            or _upper(tokens[close + 1]) != "OVER" or tokens[close + 2].text != "(":
        return None
    over_close = _matching(tokens, close + 2)
    if over_close is None or over_close >= limit:
        return None
    subset = _partition_subset(tokens[close + 3:over_close], relation_names)
    alias = _rank_alias(tokens, over_close)
    if subset is None or not alias:
        return None
    return subset, alias


def _rank_alias(tokens, over_close: int) -> str:
    """The alias the windowed expression is given (``... ) AS rn`` or ``... )
    rn``), else ``""``."""
    nxt = over_close + 1
    if nxt < len(tokens) and _upper(tokens[nxt]) == "AS":
        nxt += 1
    if nxt < len(tokens) and tokens[nxt].kind in {"word", "identifier"}:
        return str(tokens[nxt].value).lower()
    return ""


def _outer_reads_cte(tokens, binding) -> int | None:
    """The index of the SELECT of the one outer query whose FROM reads ONLY the
    CTE ``binding`` directly (no join, no derived table), else ``None``. The
    outer query is at the CTE's scope depth, after the CTE body and within the
    CTE's visibility scope."""
    depth = binding.scope_depth
    for i in range(binding.body_close + 1, binding.scope_end):
        if tokens[i].depth != depth or _upper(tokens[i]) != "SELECT":
            continue
        frm = _from_index(tokens, i, depth)
        if frm is not None and _from_is_only_cte(tokens, frm, depth, binding):
            return i
    return None


def _from_index(tokens, select_index: int, depth: int) -> int | None:
    """The FROM of the query opened at ``select_index``, at ``depth``, before
    its WHERE / GROUP / ... or the end of its scope."""
    for j in range(select_index + 1, len(tokens)):
        if tokens[j].depth < depth:
            return None
        if tokens[j].depth == depth:
            word = _upper(tokens[j])
            if word == "FROM":
                return j
            if word in {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
                        "QUALIFY", "UNION", "INTERSECT", "EXCEPT", "SELECT"}:
                return None
    return None


def _from_is_only_cte(tokens, frm: int, depth: int, binding) -> bool:
    """True when the FROM at ``frm`` names exactly the CTE and nothing else (no
    join, no comma, no derived table) before the next clause."""
    end = _clause_end(tokens, frm, depth)
    items = [tokens[j] for j in range(frm + 1, end) if tokens[j].depth == depth]
    names = [t for t in items if t.kind in {"word", "identifier"}]
    if not names or not _name_matches_binding(names[0], binding):
        return False
    rest = [t for t in items[items.index(names[0]) + 1:]
            if _upper(t) not in {"AS"} and t.kind in {"word", "identifier"}]
    joins = any(t.text == "," or _upper(t) in {"JOIN", "CROSS", "INNER", "LEFT",
                "RIGHT", "FULL", "UNION"} for t in items)
    return not joins and len(rest) <= 1  # the CTE and at most its alias


def _name_matches_binding(token, binding) -> bool:
    if binding.quoted or token.kind == "identifier":
        return (binding.quoted == (token.kind == "identifier")
                and binding.name == token.value)
    return binding.name.lower() == str(token.value).lower()


def _clause_end(tokens, start: int, depth: int) -> int:
    """The index at which the clause opened at ``start`` ends: the next clause
    keyword at ``depth`` or a drop below ``depth``."""
    enders = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "WINDOW",
              "QUALIFY", "UNION", "INTERSECT", "EXCEPT", "FETCH", "FROM", "SELECT"}
    for j in range(start + 1, len(tokens)):
        if tokens[j].depth < depth:
            return j
        if tokens[j].depth == depth and _upper(tokens[j]) in enders:
            return j
    return len(tokens)


def _outer_filters_alias(tokens, select_index: int, depth: int, alias: str,
                         cte: str) -> bool:
    """True when the outer query's WHERE is an AND-only predicate one of whose
    conjuncts is exactly ``<alias> = 1`` or ``<cte>.<alias> = 1`` (balanced
    parentheses stripped from the whole WHERE and each conjunct). Any OR or NOT
    anywhere in the WHERE, or no WHERE, returns False."""
    where = _where_tokens(tokens, select_index, depth)
    if where is None:
        return False
    from .athena_scope import _split_boolean
    inner = _strip_outer(where)
    if any(_upper(t) in {"OR", "NOT"} for t in inner):
        return False
    for conjunct in _split_boolean(inner, "AND"):
        if _is_alias_eq_one(_strip_outer(conjunct), alias, cte):
            return True
    return False


def _where_tokens(tokens, select_index: int, depth: int):
    """The outer query's WHERE predicate tokens (balanced outer parentheses
    stripped), else ``None`` when there is no WHERE at this depth."""
    end = _clause_end(tokens, select_index, depth)   # stops at the first clause
    # _clause_end stops at FROM/WHERE/...; walk forward to the WHERE itself.
    where = next((j for j in range(select_index + 1, len(tokens))
                  if tokens[j].depth == depth and _upper(tokens[j]) == "WHERE"
                  and j >= end), None)
    if where is None:
        return None
    stop = _clause_end(tokens, where, depth)
    return tokens[where + 1:stop]


def _is_alias_eq_one(conjunct, alias: str, cte: str) -> bool:
    """True when ``conjunct`` is exactly ``<alias> = 1`` or ``<cte>.<alias> =
    1`` (either side), with no other tokens."""
    eq = [j for j, t in enumerate(conjunct) if t.kind == "operator" and t.text == "="]
    if len(eq) != 1:
        return False
    at = eq[0]
    left, right = conjunct[:at], conjunct[at + 1:]
    for ref, other in ((left, right), (right, left)):
        if len(other) == 1 and _is_one(other[0]) and _ref_is_alias(ref, alias, cte):
            return True
    return False


def _ref_is_alias(ref, alias: str, cte: str) -> bool:
    """``ref`` is exactly ``<alias>`` or ``<cte>.<alias>`` and nothing else."""
    if len(ref) == 1:
        return ref[0].kind in {"word", "identifier"} and str(ref[0].value).lower() == alias
    if (len(ref) == 3 and ref[1].text == "."
            and ref[0].kind in {"word", "identifier"} and ref[2].kind in {"word", "identifier"}):
        return str(ref[0].value).lower() == cte and str(ref[2].value).lower() == alias
    return False


def _is_one(token) -> bool:
    return bool(token) and token.kind == "number" and token.text.strip() == "1"


#: Aggregates whose value changes with how many rows there are: over the rows a
#: de-duplication kept, each gives a different result than over every record.
#: MIN / MAX (and their ``_by`` forms, ``arbitrary``, ``bool_and`` ...) do not,
#: so they are not here: a query whose only aggregates are those is not read.
#: By the engine's families: general (``count`` ... ``checksum``), bitwise
#: (``bitwise_xor_agg``: a repeated value cancels; AND / OR do not change),
#: map (``histogram``, ``map_agg``, ``multimap_agg``), set (``set_agg``),
#: lambda (``reduce_agg``), approximate (sketches and estimates, ``merge`` of
#: them included), statistical and regression.
_MULTIPLICITY_AGGREGATES = frozenset({
    "count", "count_if", "approx_distinct", "sum", "avg", "approx_percentile",
    "percentile_cont", "percentile_disc", "median", "array_agg", "listagg", "string_agg",
    "checksum", "bitwise_xor_agg",
    "histogram", "map_agg", "multimap_agg", "set_agg", "reduce_agg",
    "numeric_histogram", "approx_most_frequent", "approx_set", "merge",
    "tdigest_agg", "qdigest_agg",
    "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
    "geometric_mean", "corr", "covar_pop", "covar_samp", "kurtosis", "skewness", "entropy",
    "regr_intercept", "regr_slope", "regr_count", "regr_avgx", "regr_avgy", "regr_r2",
    "regr_sxx", "regr_sxy", "regr_syy"})
#: Of those, the ones that count rows or values (the "counted" texts apply).
_COUNT_AGGREGATES = frozenset({"count", "count_if", "approx_distinct"})
#: A parenthesis opened before one of these words is a nested query block.
_NESTED_BLOCK = frozenset({"SELECT", "WITH", "VALUES"})


def _outer_aggregates(tokens, select_index: int, depth: int) -> set:
    """The multiplicity aggregates (:data:`_MULTIPLICITY_AGGREGATES`) the
    outer query computes over the CTE's rows at its own level: called in its
    SELECT list or its HAVING, alone or inside an expression (``ROUND(AVG(x),
    1)``), with or without ``FILTER``. Not read: a call followed by ``OVER``
    (a window over the result rows, its arguments and its window spec with
    it) and anything in a nested query block. ``set()`` for none or for a
    clause the reader cannot bound."""
    frm = _from_index(tokens, select_index, depth)
    if frm is None:
        return set()
    spans = [(select_index + 1, frm)]
    end = _scope_end(tokens, frm, depth)
    having = next((j for j in range(frm + 1, end)
                   if tokens[j].depth == depth and _upper(tokens[j]) == "HAVING"), None)
    if having is not None:
        spans.append((having + 1, _clause_end(tokens, having, depth)))
    found: set = set()
    for start, stop in spans:
        names = _calls_in(tokens, start, stop)
        if names is None:
            return set()
        found |= names
    return found


def _scope_end(tokens, start: int, depth: int) -> int:
    """The index where the query block at ``depth`` that holds ``start``
    ends: the first token below ``depth``, else the end."""
    return next((j for j in range(start, len(tokens)) if tokens[j].depth < depth), len(tokens))


def _calls_in(tokens, start: int, stop: int) -> set | None:
    """The multiplicity aggregate names called in ``tokens[start:stop]``
    outside nested query blocks and windowed calls, ``None`` when a
    parenthesis does not close inside the span."""
    found: set = set()
    i = start
    while i < stop:
        token = tokens[i]
        if token.text == "(" and i + 1 < stop and _upper(tokens[i + 1]) in _NESTED_BLOCK:
            close = _matching(tokens, i)
            if close is None or close >= stop:
                return None
            i = close + 1
            continue
        if (token.kind == "word" and i + 1 < stop and tokens[i + 1].text == "("
                and not (i > start and tokens[i - 1].text == ".")):
            close = _matching(tokens, i + 1)
            if close is None or close >= stop:
                return None
            after = close + 1
            if (after + 1 < stop and _upper(tokens[after]) == "FILTER"
                    and tokens[after + 1].text == "("):
                filter_close = _matching(tokens, after + 1)
                if filter_close is None or filter_close >= stop:
                    return None
                after = filter_close + 1
            if after < stop and _upper(tokens[after]) == "OVER":
                i = _past_over(tokens, after, stop)
                if i is None:
                    return None
                continue
            if token.value.lower() in _MULTIPLICITY_AGGREGATES:
                found.add(token.value.lower())
        i += 1
    return found


def _past_over(tokens, over: int, stop: int) -> int | None:
    """The index just past the window of ``OVER`` at ``over``: its
    parenthesised spec, or a named window."""
    if over + 1 < stop and tokens[over + 1].text == "(":
        close = _matching(tokens, over + 1)
        return None if close is None or close >= stop else close + 1
    return over + 2 if over + 1 < stop else None


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubsetDedupe:
    """A single-table query that keeps one row per ``subset``, a STRICT SUBSET
    of ``record_key``."""

    table: str
    subset: tuple
    record_key: tuple
    #: Always refuse-once: the rule reads the query and the key alone.
    refuse: bool = True
    #: True when the outer query counts the kept rows (``COUNT``,
    #: ``count_if``, ``approx_distinct``); False when it only aggregates them
    #: otherwise (``SUM``, ``AVG``, a percentile), so the texts do not say
    #: "counted".
    counts: bool = True

    def to_dict(self, names=None) -> dict:
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        out = {"table": names.show(self.table, "table"),
               "subset": [names.show(c) for c in self.subset],
               "record_key": [names.show(c) for c in self.record_key]}
        if names.count:
            out["names_withheld"] = names.count
        return out


def decide(sql: str, table: str, record_key) -> SubsetDedupe | None:
    """The subset-of-record-key de-duplication this query runs, else ``None``.

    Refuses exactly ONE grammar (nothing else):

    1. a named CTE reads exactly the one physical ``table`` and DIRECTLY
       projects ``ROW_NUMBER() OVER (PARTITION BY <subset>) AS <alias>`` with
       ``<subset>`` a STRICT, non-empty SUBSET of ``record_key`` (each term a
       bare/sole-relation-qualified column or ``COALESCE(<key column>, '')``);
    2. one outer query reads that CTE DIRECTLY in its FROM (no join, no derived
       table, no CTE in between);
    3. that outer query's WHERE is an AND-only predicate one of whose conjuncts
       is exactly ``<alias> = 1`` or ``<cte>.<alias> = 1`` (balanced parentheses
       stripped);
    4. that outer query's result depends on how many rows were kept: its
       SELECT list or HAVING calls, at its own level, an aggregate whose value
       changes with row multiplicity (:data:`_MULTIPLICITY_AGGREGATES`:
       ``COUNT`` in any form, ``SUM``, ``AVG``, a percentile, ``array_agg``
       ...). A call under ``OVER`` and one in a nested query block do not
       count. A plain selection of the kept rows (the latest row of each
       group, read for its values) and an outer query whose only aggregates
       are ``MIN`` / ``MAX`` are not read.

    Any OR or NOT in that WHERE, a derived table, QUALIFY, a pass-through CTE,
    a nested or recursive WITH, a CTE name repeated in the chain, a top-level
    set operation, a statement that does not start at WITH, a second
    statement, or any uncertain tokenisation returns ``None``."""
    key = [str(c).lower() for c in record_key or ()]
    if not key:
        return None
    tokens, uncertain = tokenize(_one_statement(sql))
    if uncertain or not tokens:
        return None
    from .athena_scope import _cte_bindings
    bindings, cte_uncertain = _cte_bindings(tokens)
    if cte_uncertain:
        return None
    keyset = set(key)
    qualify = _qualify_dedupe(tokens, table, keyset)
    if qualify is not None:
        return _subset_dedupe(table, key, qualify)
    for binding in _top_level_chain(tokens, bindings):
        found = _cte_dedupes(tokens, binding, table, keyset)
        if found is not None:
            return _subset_dedupe(table, key, found)
    return None


def _subset_dedupe(table: str, key: list, found) -> "SubsetDedupe":
    """A :class:`SubsetDedupe` from one rule branch's ``(subset, counts)``,
    with the subset ordered as the declared key is."""
    subset, counts = found
    ordered = tuple(c for c in key if c in set(subset))
    return SubsetDedupe(table, ordered, tuple(key), refuse=True, counts=counts)


def _qualify_dedupe(tokens, table: str, keyset: set):
    """``(strict-subset partition, counts)`` when the statement de-duplicates
    ``table`` with DuckDB's ``QUALIFY ROW_NUMBER() OVER (PARTITION BY <subset>)
    = 1`` and an OUTER query counts the kept rows, else ``None``. The DuckDB
    analogue of the latest-row CTE grammar (:func:`decide`).

    The executable shape is a DERIVED TABLE with the QUALIFY, counted outside:
    ``SELECT COUNT(*) FROM (SELECT * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION
    BY <subset>) = 1) d``. The QUALIFY is traced from the block that OWNS the
    base-table scan; a QUALIFY in a GROUPED block is NOT record
    de-duplication (it filters already-aggregated groups), so a block with a
    GROUP BY at the scan's depth is rejected. The outer query must read only
    that derived table and compute a multiplicity aggregate."""
    if _upper(tokens[0]) in {"WITH"} or any(t.kind == "semicolon" for t in tokens):
        return None
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1 or scans[0].table.lower() != str(table).lower():
        return None
    scan = scans[0]
    block_end = _scope_end(tokens, scan.select_index, scan.depth)
    if _clause_at(tokens, scan.select_index, block_end, scan.depth, "GROUP") is not None:
        return None  # grouped QUALIFY filters groups, not records
    qualify = _clause_at(tokens, scan.select_index, block_end, scan.depth, "QUALIFY")
    if qualify is None:
        return None
    window = _qualify_window(tokens, qualify, block_end, _relation_names(scans))
    if window is None or not _is_strict_subset(window, keyset):
        return None
    counts = _qualify_outer_counts(tokens, scan)
    if counts is None:
        return None
    return window, counts


def _clause_at(tokens, start: int, end: int, depth: int, word: str):
    """The index of clause ``word`` at ``depth`` within ``tokens[start:end]``,
    else ``None`` (``GROUP`` / ``QUALIFY`` of the scan's own block)."""
    for i in range(start + 1, end):
        if tokens[i].depth == depth and _upper(tokens[i]) == word:
            return i
    return None


def _qualify_outer_counts(tokens, scan):
    """Whether the block holding ``scan`` is a DERIVED TABLE read by an outer
    query that computes a multiplicity aggregate over it: ``True`` when that
    aggregate counts rows (``COUNT`` ...), ``False`` when it aggregates them
    otherwise (``SUM`` ...), ``None`` when there is no such outer count (so the
    QUALIFY is not a record de-duplication the figure depends on). The whole
    statement being the scan's own block (no enclosing derived table) is also
    ``None``: a bare ``SELECT COUNT(*) ... QUALIFY`` is not executable DuckDB."""
    if scan.depth == 0:
        return None
    opened = _enclosing_paren(tokens, scan.select_index)
    if opened is None:
        return None
    close = _matching(tokens, opened)
    if close is None:
        return None
    outer_depth = scan.depth - 1
    outer = next((i for i in range(close + 1, len(tokens))
                  if tokens[i].depth == outer_depth and _upper(tokens[i]) == "SELECT"), None)
    outer = outer if outer is not None else _outer_before(tokens, opened, outer_depth)
    if outer is None:
        return None
    aggregates = _outer_aggregates(tokens, outer, outer_depth)
    if not aggregates:
        return None
    return bool(aggregates & _COUNT_AGGREGATES)


def _enclosing_paren(tokens, index: int):
    """The index of the ``(`` that encloses ``tokens[index]`` one level out."""
    depth = tokens[index].depth
    for j in range(index - 1, -1, -1):
        if tokens[j].text == "(" and tokens[j].depth == depth - 1:
            return j
    return None


def _outer_before(tokens, opened: int, outer_depth: int):
    """The SELECT of the outer query whose FROM opens at ``opened`` (the derived
    table), scanning back to the ``SELECT`` at ``outer_depth``."""
    for j in range(opened - 1, -1, -1):
        if tokens[j].depth == outer_depth and _upper(tokens[j]) == "SELECT":
            return j
    return None


def _qualify_window(tokens, qualify: int, block_end: int, relation_names: set):
    """The strict-subset PARTITION BY of a ``QUALIFY ROW_NUMBER() OVER
    (PARTITION BY <subset> ...) = 1`` clause at ``qualify``, whose predicate
    within the block is exactly that, balanced; else ``None``. Reuses the
    latest-row window reader's partition parse and requires the ``= 1``
    filter. ``block_end`` bounds the clause to the scan's own block."""
    n = len(tokens)
    qdepth = tokens[qualify].depth
    i = qualify + 1
    if (i >= n or tokens[i].kind != "word" or tokens[i].value.lower() != _RANK_FUNC
            or i + 1 >= n or tokens[i + 1].text != "("):
        return None
    close = _matching(tokens, i + 1)
    if close is None or close + 2 >= n or _upper(tokens[close + 1]) != "OVER" \
            or tokens[close + 2].text != "(":
        return None
    over_close = _matching(tokens, close + 2)
    if over_close is None:
        return None
    subset = _partition_subset(tokens[close + 3:over_close], relation_names)
    rest = [t for t in tokens[over_close + 1:block_end] if t.depth == qdepth]
    if subset is None or len(rest) != 2 or rest[0].kind != "operator" \
            or rest[0].text != "=" or not _is_one(rest[1]):
        return None
    return subset


_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT"})


def _one_statement(sql) -> str:
    """``sql`` without one trailing semicolon (an ordinary statement end); any
    other semicolon stays and makes the statement unreadable."""
    text = sql.strip() if isinstance(sql, str) else ""
    return text[:-1] if text.endswith(";") else text


def _top_level_chain(tokens, bindings) -> list:
    """The statement's own WITH chain (the bindings at depth zero), or ``[]``
    when the statement is not one non-recursive WITH chain followed by exactly
    one top-level query: a statement that does not start at WITH (EXPLAIN, a
    parenthesised statement), a chain not followed directly by SELECT (INSERT,
    CREATE TABLE AS, DELETE), a second statement, a recursive WITH, a CTE name
    repeated in the chain, a set operation at the top level, or more than one
    top-level SELECT. Nested WITH bindings are never returned, so a
    de-duplication inside another CTE, or shadowed by one, is not read."""
    if not tokens or _upper(tokens[0]) != "WITH":
        return []
    if any(t.kind == "semicolon" for t in tokens):
        return []
    chain = [b for b in bindings if b.scope_depth == 0]
    if not chain or any(b.recursive for b in chain):
        return []
    query = max(b.body_close for b in chain) + 1
    if query >= len(tokens) or _upper(tokens[query]) != "SELECT":
        return []
    names = [b.name if b.quoted else str(b.name).lower() for b in chain]
    if len(set(names)) != len(names):
        return []
    top = [t for t in tokens if t.depth == 0]
    if any(_upper(t) in _SET_OPERATORS for t in top):
        return []
    if sum(1 for t in top if _upper(t) == "SELECT") != 1:
        return []
    return chain


def _cte_dedupes(tokens, binding, table: str, keyset: set):
    """``(strict-subset partition, counts)`` of ``binding`` when it is the
    direct-CTE de-duplication of ``table`` consumed by an AND-only ``<alias> =
    1`` outer WHERE whose query aggregates the kept rows by their multiplicity
    (see :func:`decide`), else ``None``. ``counts`` is True when one of those
    aggregates counts (``COUNT``, ``count_if``, ``approx_distinct``)."""
    body = tokens[binding.body_open + 1:binding.body_close]
    scans, scan_uncertain = relations(body)
    if scan_uncertain or len(scans) != 1 or scans[0].table.lower() != str(table).lower():
        return None
    subset = _cte_dedupe_subset(tokens, binding, scans, keyset)
    if subset is None:
        return None
    select = _outer_reads_cte(tokens, binding)
    if select is None:
        return None
    aggregates = _outer_aggregates(tokens, select, binding.scope_depth)
    if not aggregates:
        return None
    return subset, bool(aggregates & _COUNT_AGGREGATES)


def _cte_dedupe_subset(tokens, binding, scans, keyset: set):
    """The strict-subset partition of a CTE body that de-duplicates the table,
    whether it does so with ``ROW_NUMBER() OVER (...) AS <alias>`` consumed by
    an outer ``<alias> = 1`` (the original grammar), or with a body-level
    ``QUALIFY ROW_NUMBER() OVER (...) = 1``; else ``None``. A
    GROUPED CTE body (a GROUP BY at the body's own depth) is not record
    de-duplication and is rejected."""
    names = _relation_names(scans)
    body_depth = tokens[binding.body_open].depth + 1
    if _clause_at(tokens, binding.body_open, binding.body_close, body_depth, "GROUP") is not None:
        return None
    qualify = _clause_at(tokens, binding.body_open, binding.body_close, body_depth, "QUALIFY")
    if qualify is not None:
        subset = _qualify_window(tokens, qualify, binding.body_close, names)
        return subset if subset is not None and _is_strict_subset(subset, keyset) else None
    window = _window_in_cte_body(tokens, binding.body_open, binding.body_close, names)
    if window is None:
        return None
    subset, alias = window
    if not _is_strict_subset(subset, keyset):
        return None
    cte = str(binding.name).lower()
    select = _outer_reads_cte(tokens, binding)
    if select is None or not _outer_filters_alias(tokens, select, binding.scope_depth,
                                                  alias, cte):
        return None
    return subset


def _is_strict_subset(subset, keyset: set) -> bool:
    """True when ``subset`` is a non-empty set of key columns that does not
    cover the whole key (so the grouped rows that share it are still separate
    records)."""
    cols = {c for c in subset if c}
    return bool(cols) and cols <= keyset and cols != keyset


# --------------------------------------------------------------------------- #
# The refusal and the disclosure: facts, names only when printable
# --------------------------------------------------------------------------- #
def _subset_fact(names, found: "SubsetDedupe", *, ran: bool) -> str:
    key = ", ".join(names.show(c) for c in found.record_key)
    subset = ", ".join(names.show(c) for c in found.subset)
    were = "were" if ran else "would be"
    return (f"rows that share {subset} {were} collapsed to one, but {subset} is only part "
            f"of the record key ({key}), so rows that share it are separate records, each "
            "its own record")


class RecordKeyError(StorageBoundError):
    """A query refused before it ran for de-duplicating records of a table by a
    column set that is a strict subset of its declared record key
    (reason :data:`REASON_RECORD_KEY`)."""

    def __init__(self, found: SubsetDedupe):
        super().__init__(StorageBound(found.table, (), (), ()), (), (),
                         reason=REASON_RECORD_KEY)
        self.found = found

    def to_dict(self) -> dict:
        from .engine_names import Withheld
        f, names = self.found, Withheld()
        table = names.show(f.table, "table")
        fact = _subset_fact(names, f, ran=False)
        key = ", ".join(names.show(c) for c in f.record_key)
        what = (f"Refused before running: this query de-duplicates {table} by a subset of its "
                f"record key, so {fact}. The table's record key is {key}; a COUNT(*) of its rows "
                "counts records.")
        if f.counts:
            remedy = ("Count the rows as they are (a COUNT of the records, no de-duplication "
                      "by part of the key). If you mean to count distinct values of those "
                      "columns, send the same query again unchanged: it runs, and the answer "
                      "says how the records were counted.")
        else:
            remedy = ("Aggregate the rows as they are (no de-duplication by part of the key). "
                      "If you mean one row per distinct value of those columns, send the same "
                      "query again unchanged: it runs, and the answer says which rows were "
                      "aggregated.")
        guidance = f"{what} {remedy}"
        out = {
            "error": "record_key_dedupe",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": REASON_RECORD_KEY,
            "retryable": True,
            **f.to_dict(names),
            "guidance": guidance + names.sentence(),
        }
        if names.count:
            out["names_withheld"] = names.count
        return out


def _distinct(windows) -> list:
    out = []
    for window in windows or ():
        if isinstance(window, SubsetDedupe) and window not in out:
            out.append(window)
    return out[:6]


def engine_line(windows) -> str:
    """One bounded engine line, as facts, for the subset de-duplications this
    call's queries ran with, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    names = Withheld()
    parts = [f"in {names.show(w.table, 'table')}, {_subset_fact(names, w, ran=True)}"
             for w in shown]
    return (f"{ENGINE_PREFIX} check, in this turn's queries: {'; '.join(parts)}. "
            f"Each de-duplicated by {MARKER}.{names.sentence()}")


def requirement_block(windows) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these de-duplications, ``""`` for
    none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes = []
    for w in shown:
        subset = ", ".join(names.show(c) for c in w.subset)
        key = ", ".join(names.show(c) for c in w.record_key)
        if w.counts:
            said = f"it counted distinct values of {subset}, not records"
        else:
            said = (f"its figures were computed over one row per distinct value of {subset}, "
                    "not over every record")
        notes.append(f"REQUIRED: the answer must say that {said}; the record key is {key}, so "
                     f"rows that share {subset} are separate records.")
    if notes and names.count:
        notes[-1] += names.sentence()
    document = empty_document()
    for note in notes:
        document = append_note(document, note, GuidanceScope.ANSWER_REQUIREMENT)
    return render_orchestrator_sections(document)["answer_requirements"]


def neutralize(text: str) -> str:
    """``text`` with a copy of the engine line's prefix made inert."""
    if not isinstance(text, str) or ENGINE_PREFIX not in text:
        return text
    return text.replace(ENGINE_PREFIX, ENGINE_PREFIX[len("[engine] "):])
