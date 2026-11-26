# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Say which column names a record when a SQL result holds only its row id.

Test event, 2026-10-15: asked which batches drew one material lot, the ERP
system agent read the lot usage table and answered with two UUID run ids,
saying it had no business-key translation for them. Nothing in the agent's
result path or prompt told it that a row id is not a name, or where the
name is. (In that schema the run table itself is not exposed, so this line
does not fire there: tests/integration/test_row_id_names_pg.py pins both.)

A PostgreSQL result gets ONE fixed engine line, ``row_id_note``, when one of
its columns holds only row ids (UUIDs, or integers in an ``id``/``*_id`` key)
read from the FK side of a declared join key, and the table that key
references has a business key the result does not already carry::

    [engine] <col> is a row id; <table>.<business key> names that record;
    join on <declared key> and return it, with the row id labelled as the
    record's lookup key.

- Which column a result column comes from is read from the SELECT list
  (``select_lineage.output_sources``: aliases carried through, ``q.col`` by
  its FROM item, a bare ``col`` only when one FROM item has it). A ``*``, a
  WITH, a set operation, an expression, an ambiguous or unknown reference, a
  duplicated output name, or a projection that does not line up with the
  result gives no line: a column name or a table named somewhere in the SQL
  is never proof.
- Declared join keys are the ones onboarding baked after the table entries
  (``schema_format.declared_join_keys``), so the line only ever names a join
  the agent was already given. A row id no declared key resolves gets no
  line: another table linked through the same parent record is no proof of
  the same record, so nothing points at one.
- The row id stays in the answer beside the business key: it is the key
  another system resolves the record by.
- The business key is a text column of the referenced table other than its
  key: the one single-column UNIQUE text column (read from pg_catalog through
  the views, ``pg_keys.UNIQUE_COLUMNS_SQL``, once per database and
  ``CACHE_SECONDS``); with two or more, the one of them tagged with the
  record's identifier concept; with none, the one text column so tagged. The
  record's concept is any concept tagged on the key or the referencing
  column; when they carry none, any identifier tag counts. Anything else
  is ambiguous and gets no line.
- The catalog read runs only when a result has such a column, under its own
  short statement timeout. A failed read leaves the tags as the only source
  and is not retried for that database and schema for
  ``FAILURE_BACKOFF_SECONDS``.

The rows are never changed and any fault returns the result as it was. Pure
except :func:`unique_columns`; generic, no dataset knowledge.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

ROW_ID_NOTE_KEY = "row_id_note"
#: How long a database's unique columns are reused, how long a failed read
#: of them is not retried, and the catalog read's own statement timeout.
CACHE_SECONDS = 600
FAILURE_BACKOFF_SECONDS = 60
CHECK_TIMEOUT_MS = 5_000
#: Sets the session's statement_timeout (ms, a bound string); SET cannot bind.
_SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, false)"
#: Row-id columns named per result.
MAX_NOTES = 3

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                   re.IGNORECASE)
_TEXT_TYPE = re.compile(
    r"^(character varying|varchar|text|character|char|bpchar|citext|nvarchar|nchar|string)"
    r"(\s*\(\s*\d+\s*\))?$", re.IGNORECASE)
_IDENTIFIER_ROLE = "identifier"
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class RowIdContext:
    """What the system agent was given at build: the declared join keys
    ``(from_table, from_field, to_table, to_field)``, the baked column types
    ``{table: {column: type}}`` and the concept tags
    ``{(table, column): ((concept, role), ...)}``, all lowercased."""
    join_keys: tuple
    column_types: Mapping[str, Mapping[str, str]]
    tags: Mapping[tuple, tuple]


@dataclasses.dataclass(frozen=True)
class RowIdLink:
    column: str
    from_table: str
    from_field: str
    to_table: str
    to_field: str


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _concept_tags(items) -> dict:
    tags: dict[tuple, list] = {}
    for item in items or ():
        if not isinstance(item, dict):
            continue
        table, _dot, column = str(item.get("field") or "").lower().partition(".")
        concept = str(item.get("concept") or "").strip()
        if table and column and concept:
            tags.setdefault((table, column), []).append(
                (concept, str(item.get("role") or "").strip().lower()))
    return {key: tuple(value) for key, value in tags.items()}


def context_from(tables_desc: str, concept_items=None) -> RowIdContext | None:
    """The context for a baked PostgreSQL schema, or None when it declares no
    join key (no line is ever possible)."""
    from .response_integrity_sql import parse_physical_schema
    from .schema_format import declared_join_keys
    keys = tuple(declared_join_keys(tables_desc))
    if not keys:
        return None
    types = {table: dict(entry.get("columns") or {})
             for table, entry in parse_physical_schema(tables_desc).items()}
    return RowIdContext(join_keys=keys, column_types=types, tags=_concept_tags(concept_items))


def _row_id_values(values: list, to_field: str) -> bool:
    """Every value is a UUID, or every value is an integer and the key it
    references is named like a row id; at least one value."""
    from .surrogate_keys import is_surrogate_key
    present = [v for v in values if v is not None]
    if not present:
        return False
    if all(isinstance(v, str) and _UUID.match(v) for v in present):
        return True
    return (all(isinstance(v, int) and not isinstance(v, bool) for v in present)
            and is_surrogate_key(to_field, "integer"))


def _projected(columns, sql: str, context: RowIdContext, schema: str):
    """``[(result column, (relation, column) or None)]`` lined up with the
    result columns (``select_lineage.output_sources``), or None when the
    projection cannot be read or does not line up. A select item whose output
    name differs from its result column proves nothing about it."""
    from .select_lineage import output_sources
    names = [str(c).lower() for c in columns or ()]
    tables = {table: set(cols) for table, cols in context.column_types.items()}
    found = output_sources(sql, tables, schema)
    if found is None or len(found) != len(names):
        return None
    return [(name, source if out == name else None)
            for name, (out, source) in zip(names, found)]


def _links(projected, rows, context: RowIdContext) -> list[RowIdLink]:
    names = [name for name, _source in projected]
    links: list[RowIdLink] = []
    for index, (name, source) in enumerate(projected):
        if source is None or names.count(name) != 1:
            continue
        keys = [k for k in context.join_keys if k[:2] == source]
        if len({k[2:] for k in keys}) != 1:
            continue
        values = [row[index] for row in rows or ()
                  if isinstance(row, (list, tuple)) and len(row) > index]
        if _row_id_values(values, keys[0][3]):
            links.append(RowIdLink(name, *keys[0]))
    return links


def row_id_links(columns, rows, sql: str, context: RowIdContext,
                 schema: str = "") -> list[RowIdLink]:
    """Result columns whose proven source column (the SELECT list,
    ``_projected``) is the FK side of ONE declared key target and that hold
    only row ids. A duplicated output name gets none."""
    projected = _projected(columns, sql, context, schema)
    return _links(projected, rows, context) if projected else []


def _is_text(declared: str) -> bool:
    return bool(_TEXT_TYPE.match((declared or "").strip()))


def _identifier_tagged(link: RowIdLink, context: RowIdContext) -> set[str]:
    """Text columns of the referenced table tagged with the record's
    identifier concept (any identifier tag when the key columns carry none)."""
    types = context.column_types.get(link.to_table, {})
    record = {concept for key in ((link.to_table, link.to_field),
                                  (link.from_table, link.from_field))
              for concept, _role in context.tags.get(key, ())}
    tagged = set()
    for (table, column), tags in context.tags.items():
        if table != link.to_table or column == link.to_field or not _is_text(types.get(column)):
            continue
        identifiers = {concept for concept, role in tags if role == _IDENTIFIER_ROLE}
        if identifiers and (not record or identifiers & record):
            tagged.add(column)
    return tagged


def business_key(link: RowIdLink, context: RowIdContext,
                 unique: Mapping[tuple, str]) -> str:
    """The referenced table's business key column, or ``""`` (none, or
    ambiguous)."""
    types = context.column_types.get(link.to_table, {})
    unique_text = sorted(column for (table, column), declared in unique.items()
                         if table == link.to_table and column != link.to_field
                         and _is_text(declared or types.get(column, "")))
    if len(unique_text) == 1:
        return unique_text[0]
    tagged = _identifier_tagged(link, context)
    pool = [c for c in unique_text if c in tagged] if unique_text else sorted(tagged)
    return pool[0] if len(pool) == 1 else ""


def _sentence(link: RowIdLink, key: str) -> str:
    named = (link.column if link.column == link.from_field
             else f"{link.column} ({link.from_table}.{link.from_field})")
    return (f"{named} is a row id; {link.to_table}.{key} names that record; join on "
            f"{link.from_table}.{link.from_field} = {link.to_table}.{link.to_field} "
            "and return it, with the row id labelled as the record's lookup key.")


def row_id_note(columns, rows, sql: str, context: RowIdContext,
                unique: Mapping[tuple, str], schema: str = "") -> str:
    """The engine line for this result, ``""`` when no column needs one. A
    business key the result already carries (by name or by source) needs none."""
    projected = _projected(columns, sql, context, schema)
    if not projected:
        return ""
    names = {name for name, _source in projected}
    sources = {source for _name, source in projected if source}
    sentences: list[str] = []
    for link in _links(projected, rows, context):
        key = business_key(link, context, unique)
        if key and key not in names and (link.to_table, key) not in sources:
            sentences.append(_sentence(link, key))
    return "[engine] " + " ".join(sentences[:MAX_NOTES]) if sentences else ""


def _read_unique(cursor, schema: str) -> dict:
    from . import pg_keys
    cursor.execute(pg_keys.UNIQUE_COLUMNS_SQL, (schema, schema))
    rows = list(cursor.fetchall() or [])
    lineage: dict = {}
    if any(isinstance(r, (list, tuple)) and r and r[0] == "view" for r in rows):
        cursor.execute(pg_keys.ONE_TABLE_VIEW_DEFS_SQL, (schema,))
        lineage = pg_keys.lineage_from_rows(list(cursor.fetchall() or []))
    return pg_keys.unique_columns_from_rows(rows, lineage)


def unique_columns(cursor, schema: str, cache_key: Any = None,
                   restore_timeout_ms: int | None = None) -> dict:
    """``{(relation, column): type}`` for *schema*'s single-column unique
    columns, read on the caller's cursor once per ``cache_key`` and
    ``CACHE_SECONDS``. A failed read returns ``{}`` and is not retried for
    that database and schema for ``FAILURE_BACKOFF_SECONDS``, so a
    persistent failure costs one catalog attempt per backoff, not one per
    query."""
    marker, now = (cache_key, schema), time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(marker)
        if cached and cached[0] > now:
            return cached[1]
    try:
        if restore_timeout_ms is not None:
            cursor.execute(_SET_TIMEOUT_SQL, (str(CHECK_TIMEOUT_MS),))
        found, ttl = _read_unique(cursor, schema), CACHE_SECONDS
    except Exception as exc:  # noqa: BLE001 - the note is an addition, never required
        logger.warning("ROW_ID_NOTE unique-column read failed (%s); backing off %d s for "
                       "this schema", type(exc).__name__, FAILURE_BACKOFF_SECONDS)
        found, ttl = None, FAILURE_BACKOFF_SECONDS
    finally:
        if restore_timeout_ms is not None:
            try:
                cursor.execute(_SET_TIMEOUT_SQL, (str(restore_timeout_ms),))
            except Exception:  # noqa: BLE001,S110 - the connection closes right after  # nosec B110
                pass
    return _publish(marker, now + ttl, found)


def _publish(marker: tuple, expires: float, found: dict | None) -> dict:
    """Cache a read's result and return what callers should use. A failed
    read (``found is None``) caches ``{}`` for the backoff, unless another
    caller published an unexpired entry while this read was in flight; that
    entry wins."""
    with _CACHE_LOCK:
        current = _CACHE.get(marker)
        if found is None and current and current[0] > time.monotonic():
            return current[1]
        _CACHE[marker] = (expires, found or {})
        return found or {}


def with_row_id_note(cursor, schema: str, sql: str, result: dict,
                     context: RowIdContext | None, *, cache_key: Any = None,
                     restore_timeout_ms: int | None = None) -> dict:
    """*result* plus ``row_id_note`` when :func:`row_id_note` finds a column
    to name; otherwise *result* itself. Never raises."""
    if context is None or not isinstance(result, dict) or "error" in result:
        return result
    try:
        columns, rows = result.get("columns") or [], result.get("rows") or []
        if not row_id_links(columns, rows, sql, context, schema):
            return result
        unique = unique_columns(cursor, schema, cache_key, restore_timeout_ms)
        note = row_id_note(columns, rows, sql, context, unique, schema)
    except Exception as exc:  # noqa: BLE001 - the note is an addition, never required
        logger.warning("ROW_ID_NOTE failed (%s); result unchanged", type(exc).__name__)
        return result
    if not note:
        return result
    logger.info("ROW_ID_NOTE: named the business key for %d row-id column(s)",
                note.count(" is a row id;"))
    return {**result, ROW_ID_NOTE_KEY: note}
