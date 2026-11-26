# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep measure-style concept tags off integer surrogate-key columns.

A surrogate key is an integer row id a database invents to number its own
rows (``<thing>_id`` / ``id`` holding 1, 2, 3 ...). It carries no business
meaning and no identifier another system shares. The map prompt already says
"never map a quality concept to check_id or any *_id" (``catalog.MAPPING_DISAMBIGUATION``),
yet live onboards of a maintenance schema still came back with two integer id
columns tagged as a quality measure. Prompt text is advisory; this is the
deterministic backstop.

Rule, applied to each mapping row before concept selection: when the column
is named ``id`` or ends in ``_id`` AND is integer-typed (the discovered data
type, or, when the type is unknown, every sampled value is a plain integer),
every role mapping whose role is not ``identifier`` is removed. Identifier
proposals survive so the reviewer still decides them. A row left with no
mappings becomes the ordinary ``needs_review`` row ("Skip All Unmapped"). The
row records what was removed, so the review can show why.

Pure: no I/O, no model, no graph. Generic by construction: no concept or
column literal decides anything except the ``_id`` naming convention.

Sibling rule (``drop_identifier_tags_on_temporal_columns``): a date/time
column never carries an identifier tag or an identifier-kind concept (one the
vocabulary declares as naming a record, :func:`is_identifier_kind_concept`). A
point in time is not an identifier another system shares, and an
identifier-tagged date column feeds the equipment ValueMapping bridge a date as
the value to teach. Nothing is chosen in its place: which of a table's dates is
the time windows filter on and which is a plan cannot be read from a column's
type, so a column left with no tag is unmapped, for review.
"""
from __future__ import annotations

import re

_ID_NAME = re.compile(r"(^|_)id$", re.IGNORECASE)
_INTEGER_TYPE = re.compile(
    r"^(tiny|small|medium|big)?int(eger)?[248]?$|^(small|big)?serial[248]?$"
    r"|^(number|numeric|decimal)\(\s*\d+\s*(,\s*0\s*)?\)$|^long$",
    re.IGNORECASE)
_INTEGER_VALUE = re.compile(r"^-?\d+$")
KEPT_ROLE = "identifier"
NOTE = ("Integer row id (surrogate key): non-identifier concept tags removed; "
        "it carries no business meaning another system shares.")


def _column_facts(tables) -> "dict[tuple[str, str], tuple[str, list]]":
    facts: dict[tuple[str, str], tuple[str, list]] = {}
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        tname = str(table.get("name") or "")
        samples = table.get("sample_values") or {}
        for column in table.get("columns") or []:
            if isinstance(column, dict) and column.get("name"):
                cname = str(column["name"])
                facts[(tname, cname)] = (str(column.get("data_type") or ""),
                                         list(samples.get(cname) or []))
    return facts


def is_surrogate_key(column: str, data_type: str, samples=()) -> bool:
    """True for an ``id``/``*_id`` column that is integer-typed.

    An unknown/empty type falls back to the samples: every one must be a
    plain integer, and there must be at least one.
    """
    if not _ID_NAME.search(column or ""):
        return False
    dtype = (data_type or "").strip()
    if dtype and dtype.lower() != "unknown":
        return bool(_INTEGER_TYPE.match(dtype))
    values = [str(v).strip() for v in samples or () if v is not None]
    return bool(values) and all(_INTEGER_VALUE.match(v) for v in values)


def _split_field(row: dict) -> "tuple[str, str]":
    field = str(row.get("field") or "")
    table = str(row.get("table") or "")
    if table and field.startswith(table + "."):
        return table, field[len(table) + 1:]
    head, _, tail = field.partition(".")
    return (table or head, tail) if tail else (table, field)


def drop_non_identifier_tags_on_surrogate_keys(mappings: list, tables) -> int:
    """Strip non-identifier role mappings from surrogate-key rows, in place.

    Returns the number of rows changed. Rows for columns the discovered
    ``tables`` do not describe are left alone (nothing to judge them by).
    """
    facts = _column_facts(tables)
    changed = 0
    for row in mappings or []:
        if not isinstance(row, dict):
            continue
        fact = facts.get(_split_field(row))
        if fact is None or not is_surrogate_key(_split_field(row)[1], *fact):
            continue
        roles = [r for r in row.get("mappings") or [] if isinstance(r, dict)]
        kept = [r for r in roles if r.get("role") == KEPT_ROLE]
        if len(kept) == len(roles):
            continue
        row["mappings"] = kept
        row["surrogate_key_removed"] = sorted({str(r.get("concept") or "")
                                               for r in roles if r not in kept})
        row["reasoning"] = f"{NOTE} {row.get('reasoning') or ''}".strip()
        changed += 1
    return changed


_TEMPORAL_TYPE = re.compile(
    r"^(date|time|datetime|datetime2|smalldatetime|datetimeoffset|timestamp|timestamptz)"
    r"(\s*\(\s*\d+\s*\))?(\s+with(out)?\s+time\s+zone)?$",
    re.IGNORECASE)
# ISO-8601 date, optionally followed by a time part. A bare number (20220615)
# is deliberately NOT a date: nothing separates its fields.
_TEMPORAL_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
#: A declared type that DESCRIBES its values the way a REST or MCP connector
#: writes them (``"string (ISO-8601 date the order was created)"``,
#: ``"string (date-time)"``): naming ISO-8601 or date-time makes it a date.
#: Other words (``date`` alone, ``updated``) are not read: a description can
#: mention a date without the column holding one.
_NAMES_TEMPORAL_FORMAT = re.compile(r"\bISO[- ]?8601\b|\bdate[- ]?time\b", re.IGNORECASE)
TEMPORAL_NOTE = ("Date/time column: identifier concept tags removed; a point in time "
                 "is never the identifier another system shares.")


def is_temporal_column(data_type: str, samples=()) -> bool:
    """True for a date/time/timestamp-typed column, or one whose declared
    type names an ISO-8601 or date-time format (``_NAMES_TEMPORAL_FORMAT``).

    An unknown/empty type falls back to the samples: every one must be an
    ISO-8601 date or timestamp, and there must be at least one.
    """
    dtype = (data_type or "").strip()
    if dtype and dtype.lower() != "unknown":
        return bool(_TEMPORAL_TYPE.match(dtype) or _NAMES_TEMPORAL_FORMAT.search(dtype))
    values = [str(v).strip() for v in samples or () if v is not None]
    return bool(values) and all(_TEMPORAL_VALUE.match(v) for v in values)


#: A concept whose id names an identifier (``equipment-id``, ``site-id``) is one
#: whatever role the proposer wrote beside it.
_IDENTIFIER_CONCEPT_RE = re.compile(r"(^|-)id$", re.IGNORECASE)


def is_identifier_kind_concept(concept) -> bool:
    """Whether ``concept`` names one record or an identifier, by the
    vocabulary's metadata: an id named as an identifier (``*-id``), or an
    entity concept (``concept_tagging.ENTITY_CONCEPT_IDS``: a work order, a
    batch, a maintenance schedule). Membership decides, never the name: the
    field concepts for a date (``event-time``, ``schedule``) are not entity
    concepts. Case-insensitive."""
    from .concept_tagging import ENTITY_CONCEPT_IDS
    key = str(concept or "").strip().lower()
    if not key:
        return False
    return bool(_IDENTIFIER_CONCEPT_RE.search(key)) or key in ENTITY_CONCEPT_IDS


def _is_identifier_tag(tag: dict) -> bool:
    """Identifier role, or an identifier-kind concept in any role. Live
    2026-10-03: ``install_date -> equipment-id`` arrived as ``correlate`` and
    the equipment registry then read the date column as the asset tag; walks
    2026-10-21/22: ``work-order`` arrived as ``measure`` on a work order's
    report and creation dates."""
    return tag.get("role") == KEPT_ROLE or is_identifier_kind_concept(tag.get("concept"))


def drop_identifier_tags_on_temporal_columns(mappings: list, tables) -> int:
    """Strip identifier mappings from date/time rows, in place.

    Live 2026-09-26: the proposer tagged ``install_date`` / ``installed_date``
    as the equipment identifier on two maintenance schemas, and the equipment
    bridge then taught ``2022-06-15 -> <asset>``. An identifier is a tag in the
    identifier role or an identifier-kind concept in any role
    (:func:`_is_identifier_tag`). Non-identifier tags (a measure or a time
    concept) survive for the reviewer; a row left with none is unmapped, and
    no concept or confidence is put in its place. The result is the same
    whatever the order of the rows. Returns the number of rows changed;
    undescribed columns are left alone.
    """
    facts = _column_facts(tables)
    changed = 0
    for row in mappings or []:
        if not isinstance(row, dict):
            continue
        fact = facts.get(_split_field(row))
        if fact is None or not is_temporal_column(*fact):
            continue
        roles = [r for r in row.get("mappings") or [] if isinstance(r, dict)]
        kept = [r for r in roles if not _is_identifier_tag(r)]
        if len(kept) == len(roles):
            continue
        row["mappings"] = kept
        row["temporal_identifier_removed"] = sorted({str(r.get("concept") or "")
                                                     for r in roles if r not in kept})
        row["reasoning"] = f"{TEMPORAL_NOTE} {row.get('reasoning') or ''}".strip()
        changed += 1
    return changed


def _surrogate_rows(mappings: list, tables) -> "list[dict]":
    facts = _column_facts(tables)
    rows = []
    for row in mappings or []:
        if not isinstance(row, dict):
            continue
        fact = facts.get(_split_field(row))
        if fact is not None and is_surrogate_key(_split_field(row)[1], *fact):
            rows.append(row)
    return rows


def withhold_value_overlap_on_surrogate_keys(mappings: list, tables, suggest) -> int:
    """Run ``suggest(mappings)`` but undo its value-overlap upgrades on row ids.

    The value-overlap pass measures shared values; a surrogate key's 1, 2, 3
    ... coincide with any small-integer population elsewhere (live
    2026-09-25: a log row id "matched" scrap counts, asset row ids "matched"
    BOM ids). That overlap is not evidence, so an upgraded surrogate row is
    restored to exactly what it was before the pass and records the concept
    it declined. Returns the number of rows restored.
    """
    import copy
    before = {id(row): copy.deepcopy(row) for row in _surrogate_rows(mappings, tables)}
    suggest(mappings)
    restored = 0
    for row in mappings or []:
        if id(row) not in before or row.get("suggestion_basis") != "value_overlap":
            continue
        declined = str(row.get("concept") or "")
        snapshot = before.pop(id(row))
        row.clear()
        row.update(snapshot)
        row["surrogate_key_removed"] = sorted(
            set(row.get("surrogate_key_removed") or []) | {declined})
        if NOTE not in str(row.get("reasoning") or ""):
            row["reasoning"] = f"{NOTE} {row.get('reasoning') or ''}".strip()
        restored += 1
    return restored


def drop_untrustworthy_tags(mappings: list, tables) -> int:
    """Run both deterministic pre-review strips: measure tags off integer
    surrogate keys, identifier tags off date/time columns. Returns the total
    number of rows changed. One call for the two mapping pipelines (wizard map
    job and chat ``map_fields``) so neither can drift from the other."""
    return (drop_non_identifier_tags_on_surrogate_keys(mappings, tables)
            + drop_identifier_tags_on_temporal_columns(mappings, tables))
