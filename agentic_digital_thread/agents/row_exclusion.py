# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A table's declared ROW EXCLUSION: rows its source owner says are not records.

Measured on an analytics deployment: the source owner of a table said that a
temporary dual-id mode writes a second, "shadow" copy of some rows, and gave the
rule that tells them apart (keep a row when one column is empty, or a second is
empty, or a third is one of a list of values; every other row is a shadow
copy). Nothing applied it, so answers about failures at two sites over-counted
(one failure code lost exactly a third of its rows once the rule was applied).
A prose hint is not the lever: hints reach the agent as advisory query guidance
and were measured not to hold. An Athena/Iceberg table has no catalog place for
such a rule, so, like the record key (``record_key``), the owner's statement is
the only source and is stored as a steward-set table fact.

This module is four things, each the analog of ``record_key``:

1. The steward-set table fact (:func:`set_row_exclusion` /
   :func:`read_row_exclusion` / :func:`clear_row_exclusion`): an ordered list
   of 1 to :data:`MAX_CONDITIONS` structured conditions, ALL of which hold for
   a row that is not a record, a short plain label the owner gives those rows
   ("shadow copies") and the source text, stored on the table's
   ``SchemaEntry`` with provenance (who, when, source). No SQL is taken from
   the steward: each condition is ``{column, op}`` or ``{column, op, values}``
   with ``op`` one of :data:`OPS`, the column a string-typed ``Field`` of that
   table, no column+op twice, ``values`` 1 to :data:`MAX_VALUES` non-empty
   bounded strings with no quote, backslash or control character (rejected,
   never escaped). Every reject has a reason; nothing is written on one.
   Athena only (the admin route refuses any other backend).

2. ONE canonical SQL rendering (:func:`predicate_sql`),
   ``NOT (<c1> AND <c2> ...)`` with ``empty`` as ``COALESCE(col, '') = ''``,
   ``not_empty`` as ``COALESCE(col, '') <> ''`` and ``in`` / ``not_in`` as
   ``COALESCE(col, '') [NOT] IN ('v1', ...)``, used by the schema line, the
   refusal and the check alike. A column that is not a plain lowercase
   identifier (a flattened ``a.b`` column, a reserved word) is double-quoted.

3. The per-system registry the system agent reads (:func:`remember` /
   :func:`exclusion_for` / :func:`exclusions_for`), filled at bake time from
   the graph (:func:`remember_from_graph`) and the schema line it bakes
   (:func:`schema_lines`): a FACT, not an instruction, that per the source
   owner the table's records are the rows that satisfy the predicate and the
   others are <label>, not records, with the predicate shown once. No
   exclusion, no line. Which tables carry an exclusion comes from the
   system's MANIFEST (``row_exclusion_manifest``, one read of the System
   node), never from the baked table list, so a system whose manifest is
   confirmed empty reads no fact at all. Each listed table is PRESENT (its
   fact, re-validated) or UNAVAILABLE (:class:`Unavailable`: the read
   failed, answered no SchemaEntry row, found no fact stored or a fact that
   no longer fits, or a re-provision is carrying it across a schema
   rewrite); a table is ABSENT only when the manifest does not list it, and
   a system whose manifest cannot be read is UNKNOWN. An unknown fact is never
   taken for no fact. A read that may reach an unavailable table, or any data
   read of an unknown system, is refused before it runs, every time
   (:func:`unavailable_for`, :class:`RowExclusionUnavailableError`). Before
   each guarded query the manifest is read again (:func:`refresh`) and only
   the listed tables that are new, changed, pending or unavailable are read,
   in one batch; every other state is kept as last known, so a steward's
   change takes effect on the next query, a transient graph error heals
   without a rebuild, and a graph fault never touches a system whose
   manifest lists nothing. A table is taken as carrying no exclusion, or as
   carrying the one last read for it, only on a manifest read made for that
   query: when the manifest cannot be read, every data read of the system is
   held, and a fact last read only names what the hold could not confirm.

4. The engine check (:func:`decide`, :func:`decide_all_for` for every
   rule-bearing table of a system), refuse-once like ``record_key``; its SQL
   reader is ``row_exclusion_read``. For a statement the shared tokenizer
   (``athena_scope.tokenize``) reads with certainty, it decides EVERY
   physical read of the remembered table (``athena_scope.relations``): each
   set-operation branch, each side of a join or self-join, each CTE body
   (``athena_scope._cte_bindings``) or derived table, each expression
   subquery (``IN (SELECT ...)``, ``EXISTS``, a scalar subquery). The WHERE
   of each read (``athena_scope._where_expression``, that read's own query
   block) is split into its AND conjuncts, nested ANDs flattened (an OR at a
   level makes that level one conjunct); the read CARRIES the exclusion when
   one conjunct is ``NOT (<the conditions>)`` compared normalized: keyword and
   unquoted-name case, whitespace, balanced parentheses around the whole and
   around each condition, identifier quoting, ``<>`` or ``!=``, and the
   conditions (and the values of an IN list) in any order. A value's case is
   part of the value. A read alone in its block binds a bare column or a
   qualifier naming its alias or table; a read whose block JOINs anything but
   a list expansion (``CROSS JOIN UNNEST``, ``CROSS JOIN (VALUES ...)``,
   neither of which reads a table) binds only a column qualified with its own
   alias (or its table name when it has none): a bare column there may be
   another relation's. Nothing scoped to another read counts for it (a
   predicate in another branch, on another alias or only in the outer query).
   Then, once per table:

   * every read carries it: the query runs, and the answer says (one short
     clause, once per table) that it excluded the owner's <label>
     (:func:`engine_line` / :func:`requirement_block`);
   * a read does not (no such conjunct: none at all, one missing a condition
     or a value, the inner conjunction without its NOT, the predicate OR-ed
     in, a bare column in a joined read): refused once with the label, each
     read that lacks it (which read, its alias, branch, CTE or subquery) and
     the exact predicate to AND into that read's WHERE, bound to its alias
     when joined (:class:`RowExclusionError`); the identical resend runs and
     is disclosed, the answer saying its figures may include rows the owner
     marks as <label>. Under ``RESPONSE_INTEGRITY_MODE=enforce``, which
     appends no engine lines, the first send is refused the same way and the
     resend runs without them, as for the other refuse-once rules;
   * cannot be proven but may read the table (:func:`classify` names why):
     uncertain tokenisation, a second statement, a relation it cannot read, a
     recursive or unreadable WITH, or one of the owner's conditions written
     OUTSIDE the WHERE of every read (a pass-through CTE filtered later, a
     CASE that measures the rows): failed closed, refused once and disclosed
     on the resend exactly as a read without it. A statement whose text an
     uncertain read cannot trust is taken to read the table when it names it.
     Only a statement that certainly does not read the table is untouched.

Names are printed only through ``engine_names``. Generic: no deployment
names. Pure apart from the per-system registry the system agent fills at
construction and the injected graph seams (``write=`` / ``query=``), the
``schema_fields`` convention.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field, replace

from .row_exclusion_read import Reading
from .row_exclusion_read import classify as _read
from .row_exclusion_read import may_read as _may_read
from .row_exclusion_read import may_read_data as _may_read_data
from .storage_bound_rule import StorageBound, StorageBoundError

ERROR_NAME = "ROW_EXCLUSION"
REASON_ROW_EXCLUSION = "row_exclusion_not_applied"
#: A read held back because the table's row exclusion is unavailable.
ERROR_NAME_UNAVAILABLE = "ROW_EXCLUSION_UNAVAILABLE"
REASON_UNAVAILABLE = "row_exclusion_unavailable"
#: Why a table's row exclusion is unavailable: its read failed, or the stored
#: value no longer validates. A column that no longer fits the table is named
#: by ``_column_error``'s reason (``unknown_column``, ``column_not_string``,
#: ``unprintable_column``).
READ_FAILED = "read_failed"
INVALID_STORED = "invalid_stored"
#: A listed table whose fact read answered no SchemaEntry row: not absent.
ENTRY_MISSING = "entry_missing"
#: A table the manifest lists as settled whose stored fact is gone (a write
#: outside the steward route removed it, such as a schema rewrite).
FACT_MISSING = "fact_missing"
#: A table the manifest lists as pending with no fact stored: a steward's set
#: or clear is in progress, or was interrupted, so its rule is not known.
WRITE_PENDING = "write_pending"
#: A listed table while a re-provision carries the system's row exclusions
#: across its schema rewrite (the manifest's rewrite marker stands).
REWRITE_IN_PROGRESS = "rewrite_in_progress"
#: The unavailable entry for a system whose row-exclusion manifest could not be
#: read (``row_exclusion_manifest``): every data read of it is held.
ALL_TABLES = "*"
#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] Row exclusion"
#: The phrases the engine line uses for a read without / with the exclusion.
MARKER_MISSING = "without the source owner's row exclusion"
MARKER_APPLIED = "with the source owner's row exclusion"
#: The condition operators a steward may use (see the module docstring).
OPS = ("empty", "not_empty", "in", "not_in")
MAX_CONDITIONS = 8
MAX_VALUES = 32
MAX_VALUE_CHARS = 128
MAX_LABEL_CHARS = 80
#: The steward's source text is kept, length-capped, never printed to the agent.
_MAX_SOURCE_LEN = 2000
#: Glue/Trino string column types (as ``athena_exact_or``): ``COALESCE(col, '')``
#: compares a string, so only a string column can carry a condition.
_STRING_TYPES = ("varchar", "char", "string")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _./,()-]*")
#: Characters a value may not hold: quotes, backslash, control characters.
_VALUE_FORBIDDEN = re.compile(r"['\"\\\x00-\x1f\x7f]")
_PLAIN_COLUMN = re.compile(r"[a-z_][a-z0-9_]*")
#: Trino's reserved words: a column named one is double-quoted when rendered.
_RESERVED = frozenset("""
    ALTER AND AS BETWEEN BY CASE CAST CONSTRAINT CREATE CROSS CUBE CURRENT_CATALOG
    CURRENT_DATE CURRENT_PATH CURRENT_ROLE CURRENT_SCHEMA CURRENT_TIME CURRENT_TIMESTAMP
    CURRENT_USER DEALLOCATE DELETE DESCRIBE DISTINCT DROP ELSE END ESCAPE EXCEPT EXECUTE
    EXISTS EXTRACT FALSE FOR FROM FULL GROUP GROUPING HAVING IN INNER INSERT INTERSECT INTO
    IS JOIN JSON_ARRAY JSON_EXISTS JSON_OBJECT JSON_QUERY JSON_TABLE JSON_VALUE LEFT LIKE
    LISTAGG LOCALTIME LOCALTIMESTAMP NATURAL NORMALIZE NOT NULL ON OR ORDER OUTER PREPARE
    RECURSIVE RIGHT ROLLUP SELECT SKIP TABLE THEN TRIM TRUE UESCAPE UNION UNNEST USING
    VALUES WHEN WHERE WITH""".split())

_lock = threading.Lock()
_facts: dict[str, dict] = {}
#: ``{system: {table: Unavailable}}``: tables whose row exclusion is unknown.
_unavailable: dict[str, dict] = {}
#: ``{system: {table: manifest Entry}}``: the system's row-exclusion manifest
#: as last read (``row_exclusion_manifest``); a system missing here has an
#: UNKNOWN manifest.
_listed: dict[str, dict] = {}
#: Systems whose state was set directly (:func:`remember`), never re-read.
_pinned: set = set()
#: ``{system: {table: token}}``: the manifest token each listed table's state
#: was last read at, for a settled entry; a table here is not read again until
#: its token changes, it turns pending, or it is unavailable.
_settled: dict[str, dict] = {}
#: Serializes the re-reads of one system (``{system: Lock}``).
_refresh_locks: dict[str, threading.Lock] = {}
#: ``{system: {table}}``: the tables whose rule this process has stated to the
#: system agent (in a question's block, or in a refusal or disclosure of a
#: query). The agent is a long-lived runtime whose conversation keeps earlier
#: questions, so every later question says what became of each of them
#: (:func:`current_schema_lines`).
_stated: dict[str, set] = {}
#: Manifest read attempts before a guarded query is decided without one: a
#: single failed read is tried once more at once, so one transient error
#: neither holds a system's reads nor leaves them to an old manifest.
MANIFEST_READ_ATTEMPTS = 2


# --------------------------------------------------------------------------- #
# The fact
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Condition:
    """One condition of an exclusion: ``column`` (lowercased), ``op`` (one of
    :data:`OPS`) and, for ``in`` / ``not_in``, its ``values`` in the steward's
    order."""

    column: str
    op: str
    values: tuple = ()

    def key(self) -> tuple:
        """What makes two conditions the same (an IN list's order does not)."""
        return self.column, self.op, frozenset(self.values)

    def to_dict(self) -> dict:
        out = {"column": self.column, "op": self.op}
        if self.values:
            out["values"] = list(self.values)
        return out


@dataclass(frozen=True)
class Exclusion:
    """A table's exclusion: its ``label`` and its ordered ``conditions``, ALL
    of which hold for a row that is not a record. ``at`` / ``source`` are
    when it was set and from what source, read with the fact in the same
    statement (empty when unknown); they say where the rule came from and are
    not part of the rule, so two exclusions compare by label and conditions
    only. Who set it is not read: it stays in the steward audit trail."""

    label: str
    conditions: tuple
    at: str = field(default="", compare=False)
    source: str = field(default="", compare=False)


@dataclass(frozen=True)
class Unavailable:
    """A table whose row exclusion is not known: ``kind`` says why
    (:data:`READ_FAILED`, the read failed; :data:`ENTRY_MISSING`, the read
    answered no SchemaEntry row; :data:`FACT_MISSING`, the manifest lists the
    table as settled but no fact is stored; :data:`WRITE_PENDING`, the
    manifest lists it as pending and no fact is stored;
    :data:`REWRITE_IN_PROGRESS`, a
    re-provision is carrying it across a schema rewrite;
    :data:`INVALID_STORED`, the stored
    value no longer validates; or ``_column_error``'s reason, its condition
    ``columns`` no longer fit the table, with their current ``types``). The
    table :data:`ALL_TABLES` stands for every data table of a system whose
    manifest could not be read for this query; ``last_read`` names the
    tables whose fact was read before, which only explains the hold (that
    fact may have been replaced since). A read of it is refused before it
    runs, every time (:class:`RowExclusionUnavailableError`): its figures
    cannot be confirmed."""

    table: str
    kind: str
    columns: tuple = ()
    types: tuple = ()
    last_read: tuple = ()


def _normalize_conditions(raw) -> list | None:
    """``raw`` as a list of :class:`Condition` (column and op case-folded and
    stripped, values as given), else ``None`` for a malformed shape: not a
    list, an element that is not an object of ``column`` / ``op`` /
    ``values``, a column or op that is not a string, or values that are not a
    list of strings. The VALUES of the fields are checked by
    :func:`_shape_error`."""
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict) or not set(item).issubset({"column", "op", "values"}):
            return None
        column, op = item.get("column"), item.get("op")
        if not isinstance(column, str) or not column.strip() or not isinstance(op, str):
            return None
        values = item.get("values", ())
        if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
            return None
        out.append(Condition(column.strip().lower(), op.strip().lower(), tuple(values)))
    return out


def exclusion_from(label, conditions) -> Exclusion:
    """An :class:`Exclusion` from a label and a list of condition objects
    (``{column, op[, values]}``), shape-normalized; ``ValueError`` for a
    malformed list. Validation of the values is :func:`set_row_exclusion`'s."""
    normalized = _normalize_conditions(conditions)
    if normalized is None:
        raise ValueError("conditions must be a list of {column, op[, values]} objects")
    return Exclusion(str(label or ""), tuple(normalized))


def _value_error(value: str) -> bool:
    from .untrusted_text import validate_untrusted
    return (not value or value != value.strip() or len(value) > MAX_VALUE_CHARS
            or bool(_VALUE_FORBIDDEN.search(value))
            or validate_untrusted(value, max_len=MAX_VALUE_CHARS, field="value") is not None)


def _label_error(label) -> bool:
    from .untrusted_text import validate_untrusted
    return (not isinstance(label, str) or not label or label != label.strip()
            or len(label) > MAX_LABEL_CHARS or not _LABEL_RE.fullmatch(label)
            or validate_untrusted(label, max_len=MAX_LABEL_CHARS, field="label") is not None)


def _shape_error(conditions: list, label) -> tuple | None:
    """``(reason, detail)`` for an exclusion whose label, count, ops or values
    are invalid, else ``None``. Columns are checked by :func:`_column_error`.
    ``detail`` never echoes a rejected value."""
    if not conditions:
        return "empty_conditions", "a row exclusion needs at least one condition"
    if len(conditions) > MAX_CONDITIONS:
        return "too_many_conditions", f"a row exclusion has at most {MAX_CONDITIONS} conditions"
    if _label_error(label):
        return "invalid_label", (f"the label is 1 to {MAX_LABEL_CHARS} characters of letters, "
                                 "digits, spaces and _ . / , ( ) -")
    seen = set()
    for condition in conditions:
        if condition.op not in OPS:
            return "invalid_op", f"op is one of {', '.join(OPS)}"
        listed = condition.op in {"in", "not_in"}
        if listed != bool(condition.values) or len(condition.values) > MAX_VALUES:
            return "invalid_values", (f"in and not_in take 1 to {MAX_VALUES} values; empty and "
                                      "not_empty take none")
        if any(_value_error(v) for v in condition.values):
            return "invalid_value", (f"each value is 1 to {MAX_VALUE_CHARS} characters with no "
                                     "quote, backslash, control character or edge whitespace")
        if (condition.column, condition.op) in seen:
            return "duplicate_condition", "a column and op appear at most once"
        seen.add((condition.column, condition.op))
    return None


def _string_type(data_type) -> bool:
    return str(data_type or "").strip().lower().startswith(_STRING_TYPES)


def _column_error(conditions: list, column_types: dict) -> tuple | None:
    """``(reason, detail, columns)`` when a condition's column is not a Field
    of the table, not string-typed, or not printable in engine text, else
    ``None``. ``columns`` are the offending columns, in condition order; the
    route's ``detail`` never echoes them, the bake's unavailable state does
    (through ``engine_names``). The route and the bake share this check."""
    from .engine_names import printable

    def offending(test) -> tuple:
        return tuple(dict.fromkeys(c.column for c in conditions if test(c.column)))
    missing = offending(lambda col: col not in column_types)
    if missing:
        return "unknown_column", f"not columns of this table: {len(missing)}", missing
    wrong = offending(lambda col: not _string_type(column_types[col]))
    if wrong:
        return ("column_not_string", "every condition's column must be a string column", wrong)
    plain = offending(lambda col: not printable(col))
    if plain:
        return ("unprintable_column", "every condition's column must be a plain identifier",
                plain)
    return None


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Write (or clear) a table's stored fact with its provenance, only while the
#: System's manifest is still ``$expected``, the value the writer read with
#: its own token on that table (``_write_fact``): manifest and fact are
#: compared and written in ONE statement, so a writer whose token a
#: concurrent write of the same table took writes nothing.
SET_ROW_EXCLUSION = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "WHERE coalesce(s.row_exclusion_manifest, '') = $expected "
    "SET se.row_exclusion = $row_exclusion, se.row_exclusion_by = $by, "
    "se.row_exclusion_at = $at, se.row_exclusion_source = $source RETURN se.id AS seid")
READ_ROW_EXCLUSION = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
    "RETURN se.row_exclusion AS row_exclusion, se.row_exclusion_by AS row_exclusion_by, "
    "se.row_exclusion_at AS row_exclusion_at, se.row_exclusion_source AS row_exclusion_source")
FIELD_TYPES = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid})"
    "-[:HAS_FIELD]->(f:Field) RETURN f.name AS name, f.data_type AS data_type")
#: The stored facts of several listed tables in ONE read (a table with no row
#: here was not found, which is not absent), each with when it was set and
#: from what source (``set_at`` / ``set_source``), read in the same statement
#: so they belong to that fact.
READ_ROW_EXCLUSIONS = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry) WHERE se.id IN $seids "
    "RETURN se.id AS seid, se.row_exclusion AS row_exclusion, "
    "se.row_exclusion_at AS set_at, se.row_exclusion_source AS set_source")
#: The current Fields of several tables in ONE read.
FIELD_TYPES_BATCH = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
    "WHERE se.id IN $seids RETURN se.id AS seid, f.name AS name, f.data_type AS data_type")
#: The stored facts of several listed tables WITH their provenance, in ONE
#: read: what a re-provision carries across its schema rewrite
#: (:func:`take_for_rewrite`).
READ_ROW_EXCLUSION_RECORDS = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry) WHERE se.id IN $seids "
    "RETURN se.id AS seid, se.row_exclusion AS row_exclusion, "
    "se.row_exclusion_by AS row_exclusion_by, se.row_exclusion_at AS row_exclusion_at, "
    "se.row_exclusion_source AS row_exclusion_source")


class RowExclusionWriteFailed(RuntimeError):
    """A steward write of a table's row exclusion did not land; the manifest
    keeps the table listed, so nothing stored is ever left unlisted."""


class RowExclusionEntryMissing(RowExclusionWriteFailed):
    """A fact write matched no SchemaEntry of the table (the table is not in
    the system's schema)."""


class RowExclusionContended(RowExclusionWriteFailed):
    """A fact write kept losing to concurrent writers (the manifest moved, or
    the graph aborted the write on a concurrent modification, on every
    attempt); the table stays listed, so nothing stored is left unlisted."""


def _field_types(system_id: str, table: str, query) -> dict:
    from . import schema_fields
    rows = query(FIELD_TYPES, {"sid": system_id,
                               "seid": schema_fields.schema_entry_id(system_id, table)})
    return {str(r.get("name")).lower(): r.get("data_type") for r in rows or [] if r.get("name")}


def _failed(reason: str, detail: str) -> dict:
    return {"status": "validation_failed", "reason": reason, "detail": detail}


def _write_fact(system_id: str, table: str, token: str, value, actor, source, *, write,
                query, at=None, during_rewrite: bool = False, owner=None) -> None:
    """Write (``value``) or clear (``None``) the table's stored fact with its
    provenance, only while the caller still holds ``token`` on the table in
    the system's manifest. The write is conditional, in the same statement,
    on the manifest being the value read here (:data:`SET_ROW_EXCLUSION`),
    so once a concurrent write of the same table has taken the entry, or a
    re-provision has written its rewrite marker, no fact is written. A write
    that matches nothing is sent again on a fresh read when the manifest
    moved (another table's writer). Raises
    ``row_exclusion_manifest.RewriteInProgress`` while a rewrite marker
    stands (unless this is the re-provision's own restore,
    ``during_rewrite``, whose marker ``owner`` must still own),
    ``TokenLost`` when the token is gone, and
    :class:`RowExclusionWriteFailed` when the graph refused the write or the
    SchemaEntry is not there."""
    from . import row_exclusion_manifest as manifest
    from . import schema_fields
    params = {"sid": system_id, "seid": schema_fields.schema_entry_id(system_id, table),
              "row_exclusion": value, "by": str(actor or ""), "at": at or _utc_now_iso(),
              "source": None if value is None else str(source or "")[:_MAX_SOURCE_LEN]}
    missed = None
    for attempt in range(manifest.SWAP_ATTEMPTS):
        raw, entries = manifest.read(system_id, query=query)
        manifest.check_rewrite(raw, during_rewrite=during_rewrite, owner=owner)
        entry = entries.get(table)
        if entry is None or entry.token != token:
            raise manifest.TokenLost("another write of this table's row exclusion took its "
                                     "manifest entry")
        if missed is not None and raw == missed:
            raise RowExclusionEntryMissing("the row exclusion write matched no SchemaEntry")
        try:
            rows = write(SET_ROW_EXCLUSION, {**params, "expected": raw or ""})
        except Exception as exc:  # noqa: BLE001 - only a conflict is read again
            if not manifest.is_write_conflict(exc):
                raise
            manifest._conflict_pause(attempt)
            continue                 # it did not run: read again and resend
        if rows is None or rows is False:
            raise RowExclusionWriteFailed("the row exclusion write was refused")
        if rows:
            return
        missed = raw
    raise RowExclusionContended("the manifest kept changing under a concurrent writer")


def set_row_exclusion(system_id: str, table: str, conditions, *, label, actor: str = "",
                      source: str = "", write, query) -> dict:
    """Validate and store a table's row exclusion on its ``SchemaEntry`` with
    provenance. Returns ``{"status": "ok", ..., "predicate": ...}`` or
    ``{"status": "validation_failed", "reason": ..., "detail": ...}``; nothing
    is written on a validation failure. ``write`` / ``query`` are the graph
    seams (``write`` answers the rows it wrote).

    Crash-safe order (``row_exclusion_manifest``): the table is listed in the
    system's manifest, pending, with a new token BEFORE the fact is written,
    and settled after, so a write interrupted at any point never leaves a
    stored fact unlisted. The fact write and the settle hold only while the
    entry keeps that token: a concurrent set or clear of the same table that
    takes it makes this call raise ``TokenLost`` with no fact written after
    that point. While a re-provision's rewrite marker stands, the call raises
    ``RewriteInProgress`` before it writes anything (and a call in flight
    when the marker lands writes nothing more). A write that does not land
    raises (``ManifestUnknown``, ``ManifestWriteFailed``,
    :class:`RowExclusionWriteFailed`)."""
    from . import row_exclusion_manifest as manifest
    from . import schema_fields
    normalized = _normalize_conditions(conditions)
    if normalized is None:
        return _failed("invalid_input", "conditions must be a list of {column, op[, values]}")
    manifest.check_rewrite(manifest.read(system_id, query=query)[0])
    if not schema_fields.schema_entry_exists(system_id, table, query=query):
        return _failed("unknown_table", "this system has no such table")
    err = (_shape_error(normalized, label)
           or _column_error(normalized, _field_types(system_id, table, query)))
    if err:
        return _failed(*err[:2])
    fact = Exclusion(label, tuple(normalized))
    token = manifest.new_token()
    manifest.update(system_id, manifest.listing(table, token, pending=True),
                    query=query, write=write)
    _write_fact(system_id, table, token, _serialize(fact), actor, source, write=write,
                query=query)
    manifest.update(system_id, manifest.settling(table, token), query=query, write=write)
    return {"status": "ok", "system_id": system_id, "table": table, "label": label,
            "conditions": [c.to_dict() for c in normalized], "predicate": predicate_sql(fact)}


def clear_row_exclusion(system_id: str, table: str, *, actor: str = "", write, query) -> dict:
    """Remove a table's row exclusion (its provenance with it).

    Crash-safe order (``row_exclusion_manifest``): the table is listed,
    pending, with a new token (whether or not it was listed), its fact is
    cleared, and only then is it removed from the manifest, so an interrupted
    clear never leaves a stored fact unlisted. A table whose SchemaEntry is
    gone but which the manifest still lists is only removed from the manifest
    (there is no fact to clear). As :func:`set_row_exclusion`, the fact write
    and the removal hold only while the entry keeps this call's token
    (``TokenLost`` otherwise), nothing is written while a re-provision's
    rewrite marker stands (``RewriteInProgress``), and a write that does not
    land raises."""
    from . import row_exclusion_manifest as manifest
    from . import schema_fields
    raw, entries = manifest.read(system_id, query=query)
    manifest.check_rewrite(raw)
    exists = schema_fields.schema_entry_exists(system_id, table, query=query)
    if not exists and table not in entries:
        return _failed("unknown_table", "this system has no such table")
    token = manifest.new_token()
    manifest.update(system_id, manifest.listing(table, token, pending=True),
                    query=query, write=write)
    if exists:
        _write_fact(system_id, table, token, None, actor, None, write=write, query=query)
    manifest.update(system_id, manifest.removing(table, token), query=query, write=write)
    return {"status": "ok", "system_id": system_id, "table": table}


def read_row_exclusion(system_id: str, table: str, *, query) -> dict:
    """The table's row exclusion (label, conditions, canonical predicate) and
    its provenance; empty when unset or when the stored value no longer
    validates (:func:`_parse_stored`)."""
    from . import schema_fields
    rows = query(READ_ROW_EXCLUSION, {"sid": system_id,
                                      "seid": schema_fields.schema_entry_id(system_id, table)})
    row = (rows[0] if rows else {}) or {}
    fact = _parse_stored(row.get("row_exclusion"))
    return {"system_id": system_id, "table": table,
            "label": fact.label if fact else "",
            "conditions": [c.to_dict() for c in fact.conditions] if fact else [],
            "predicate": predicate_sql(fact) if fact else "",
            "row_exclusion_by": row.get("row_exclusion_by"),
            "row_exclusion_at": row.get("row_exclusion_at"),
            "row_exclusion_source": row.get("row_exclusion_source")}


# --------------------------------------------------------------------------- #
# Carrying the facts across a re-provision's schema rewrite
# --------------------------------------------------------------------------- #
def _provision_graph():
    """The graph seams ``(write, query)`` a re-provision uses to carry the
    row exclusions across its schema rewrite (lazy, patchable in tests).
    Both are CHECKED, as the steward route's: the write answers the rows it
    wrote (``None`` when refused, ``db.GraphWriteConflict`` when the graph
    aborted it on a concurrent modification) and the read raises on a
    failure."""
    from .db import neptune_query_checked, neptune_write_returning_cas
    return neptune_write_returning_cas, neptune_query_checked


def begin_rewrite(system_id: str, *, owner: str, query, write) -> dict:
    """Before a schema rewrite deletes the system's SchemaEntries (and the
    facts stored on them): mark the rewrite on the System node, in ONE
    compare-and-set of the manifest (``row_exclusion_manifest.begin_rewrite``),
    owned by this run's ``owner`` token, with a copy of every listed table's
    stored fact and provenance, ``{table: {"pending", "record"}}`` (``record``
    ``None`` when nothing is stored), settled and pending alike. From then on
    a system agent holds every read of a listed table and a steward write is
    refused, until :func:`restore_after_rewrite` removes the marker. A marker
    a crashed or failed run left is taken over by this run and its stored
    copy returned, rather than copying the replaced schema. A confirmed empty
    manifest is marked too (with nothing to carry), so a steward's first set
    cannot land on a SchemaEntry the delete removes. Raises when the manifest
    or the facts cannot be read or the marker is not written: the caller must
    not delete the schema then, or the owner's rules would be lost with it."""
    from . import row_exclusion_manifest as manifest
    from . import schema_fields

    def carry(entries: dict) -> dict:
        if not entries:
            return {}
        seids = {schema_fields.schema_entry_id(system_id, t): t for t in entries}
        rows = query(READ_ROW_EXCLUSION_RECORDS, {"sid": system_id, "seids": list(seids)})
        records = {seids[r["seid"]]: {k: None if r.get(k) is None else str(r.get(k))
                                      for k in manifest.RECORD_KEYS}
                   for r in rows or []
                   if isinstance(r, dict) and r.get("seid") in seids and r.get("row_exclusion")}
        return {t: {"pending": e.pending, "record": records.get(t)} for t, e in entries.items()}
    return manifest.begin_rewrite(system_id, carry, owner=owner, query=query, write=write)


def restore_after_rewrite(system_id: str, *, owner: str, written=(), query, write) -> dict:
    """After the rewrite has written the replacement SchemaEntries and Fields,
    carry back the copy :func:`begin_rewrite` STORED on the System node (read
    from the graph, never from the provision's memory), while the marker is
    still the one this run's ``owner`` token owns:

    1. each listed table with a carried fact gets it back, with its
       provenance, under its current token (``_write_fact``, made as the
       rewrite's own write: no steward write lands while the marker stands);
    2. each fact written back is checked against the table's NEW Fields with
       the steward route's own check (``_column_error``). One that no longer
       fits stays stored for the steward to repair; an agent holds its reads;
    3. LAST, one compare-and-set gives every listed table a NEW token (its
       pending flag kept) and removes the marker
       (``row_exclusion_manifest.end_rewrite``), so a system agent that
       cached a fact (the cloud fast path keeps a running runtime) reads it
       again.

    ``written`` names the tables this provision writes. A carried fact whose
    table is one of them but has no SchemaEntry (a schema step that failed
    before it wrote the table) is NOT given up: step 3 is skipped, so the
    marker, the only stored copy, stays, every listed table stays held, and
    the next re-provision restores from it. A carried table this provision
    does not write is gone from the system: the marker is removed, the table
    stays listed and its reads held (no SchemaEntry), and a steward can clear
    it. A crash at any step also leaves the marker, with every listed table
    held, until the next re-provision restores from the same stored copy.

    Returns the tables by outcome, only the non-empty lists: ``kept`` (fits
    the new columns), ``unfit`` (no longer fits, or no longer validates),
    ``missing`` (the table has no SchemaEntry in the new schema) and, when
    the marker stays, ``held`` (every listed table). Raises when the manifest
    cannot be read, a write is refused, or the marker is gone or owned by
    another run (that run restores and removes it)."""
    from . import row_exclusion_manifest as manifest
    raw, entries = manifest.read(system_id, query=query)
    carried = manifest.check_rewrite(raw, during_rewrite=True, owner=owner)
    outcome = _carry_all(system_id, carried, entries, owner=owner, query=query, write=write)
    if any(t in set(written or ()) for t in outcome["missing"]):
        outcome["held"] = list(entries)
        return {k: sorted(v) for k, v in outcome.items() if v}

    def retoken(listed: dict) -> dict:
        return {t: manifest.Entry(manifest.new_token(), e.pending) for t, e in listed.items()}
    manifest.end_rewrite(system_id, retoken, owner=owner, query=query, write=write)
    return {k: sorted(v) for k, v in outcome.items() if v}


def _carry_all(system_id: str, carried: dict, entries: dict, *, owner: str, query,
               write) -> dict:
    """Steps 1 and 2 of :func:`restore_after_rewrite`: each listed table's
    carried fact written back and checked against the new columns;
    ``{"kept", "unfit", "missing", "held"}`` -> tables (``held`` left empty)."""
    outcome: dict = {"kept": [], "unfit": [], "missing": [], "held": []}
    written_back: dict = {}
    for table, before in sorted(carried.items()):
        entry = entries.get(table)
        if entry is None or before["record"] is None:
            continue
        result = _carry_fact(system_id, table, entry.token, before["record"], owner=owner,
                             query=query, write=write)
        if result == "missing":
            outcome["missing"].append(table)
        else:
            written_back[table] = result
    present = {t: f for t, f in written_back.items() if f is not None}
    outcome["unfit"].extend(t for t, f in written_back.items() if f is None)
    for table, fits in _fits_new_columns(system_id, present, query=query).items():
        outcome["kept" if fits else "unfit"].append(table)
    return outcome


def _carry_fact(system_id: str, table: str, token: str, record: dict, *, owner: str, query,
                write):
    """Write one table's carried fact back with its provenance under its
    ``token``, as the rewrite's own write under the marker ``owner`` owns:
    the parsed fact (``None`` when the stored value no longer validates), or
    the outcome name ``missing`` (the table has no SchemaEntry)."""
    try:
        _write_fact(system_id, table, token, record["row_exclusion"],
                    record.get("row_exclusion_by"), record.get("row_exclusion_source"),
                    write=write, query=query, at=record.get("row_exclusion_at"),
                    during_rewrite=True, owner=owner)
    except RowExclusionEntryMissing:
        return "missing"
    return _parse_stored(record["row_exclusion"])


def _fits_new_columns(system_id: str, present: dict, *, query) -> dict:
    """``{table: fits}``: each fact checked against its table's current
    Fields, read in ONE batch, with the steward route's check
    (``_column_error``)."""
    from . import schema_fields
    if not present:
        return {}
    seids = {schema_fields.schema_entry_id(system_id, t): t for t in present}
    types: dict = {t: {} for t in present}
    for row in query(FIELD_TYPES_BATCH, {"sid": system_id, "seids": list(seids)}) or []:
        table = seids.get(row.get("seid")) if isinstance(row, dict) else None
        if table is not None and row.get("name"):
            types[table][str(row.get("name")).lower()] = row.get("data_type")
    return {t: _column_error(list(f.conditions), types[t]) is None for t, f in present.items()}


def _serialize(fact: Exclusion) -> str:
    return json.dumps({"label": fact.label, "conditions": [c.to_dict() for c in fact.conditions]})


def _parse_stored(value) -> Exclusion | None:
    """A stored ``row_exclusion`` JSON string back to an :class:`Exclusion`,
    re-validated (label, count, ops, values), else ``None``: a value written
    around the route never reaches the agent."""
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    conditions = _normalize_conditions(parsed.get("conditions"))
    label = parsed.get("label")
    if conditions is None or _shape_error(conditions, label):
        return None
    return Exclusion(label, tuple(conditions))


# --------------------------------------------------------------------------- #
# The one canonical rendering
# --------------------------------------------------------------------------- #
def _sql_name(column: str) -> str:
    """``column`` as the engine reads it: bare when a plain lowercase
    identifier that is not a reserved word, else double-quoted."""
    if _PLAIN_COLUMN.fullmatch(column) and column.upper() not in _RESERVED:
        return column
    return f'"{column}"'


_PLAIN_QUALIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _qualifier_text(qualifier: str, show=None) -> str:
    """``qualifier.`` as the engine reads it (double-quoted unless a plain
    identifier that is not a reserved word), ``""`` for none."""
    if not qualifier:
        return ""
    shown = show(qualifier, "alias") if show is not None else qualifier
    if shown != qualifier:
        return f"{shown}."
    plain = _PLAIN_QUALIFIER.fullmatch(qualifier) and qualifier.upper() not in _RESERVED
    return f"{qualifier}." if plain else f'"{qualifier}".'


def _condition_text(condition: Condition, show=None, qualifier: str = "") -> str:
    shown = show(condition.column) if show is not None else condition.column
    column = _sql_name(shown) if shown == condition.column else shown
    left = f"COALESCE({_qualifier_text(qualifier, show)}{column}, '')"
    if condition.op == "empty":
        return f"{left} = ''"
    if condition.op == "not_empty":
        return f"{left} <> ''"
    values = ", ".join(f"'{v}'" for v in condition.values)
    return f"{left} {'IN' if condition.op == 'in' else 'NOT IN'} ({values})"


def _predicate_text(conditions, show=None, qualifier: str = "") -> str:
    return "NOT (" + " AND ".join(_condition_text(c, show, qualifier)
                                  for c in conditions) + ")"


def predicate_sql(fact, *, qualifier: str = "") -> str:
    """The ONE canonical SQL predicate a query of the table ANDs into the WHERE
    of its read to keep only records: ``NOT (<c1> AND <c2> ...)``. ``fact`` is
    an :class:`Exclusion` or an iterable of :class:`Condition`. With
    ``qualifier`` (a joined read's alias) each column is bound to it."""
    conditions = fact.conditions if isinstance(fact, Exclusion) else tuple(fact or ())
    return _predicate_text(conditions, qualifier=qualifier)


def _shown_predicate(names, conditions, qualifier: str = "") -> str:
    """The predicate for engine text, each name through ``names.show``."""
    return _predicate_text(conditions, names.show, qualifier)


# --------------------------------------------------------------------------- #
# The per-system registry (parallels record_key)
# --------------------------------------------------------------------------- #
def _as_fact(value) -> Exclusion | None:
    if isinstance(value, Exclusion):
        return value if value.conditions else None
    if isinstance(value, dict):
        try:
            fact = exclusion_from(value.get("label", ""), value.get("conditions"))
        except ValueError:
            return None
        return fact if fact.conditions else None
    return None


def remember(system_id: str, table_facts: dict) -> None:
    """Keep ``system_id``'s row exclusions, ``{table: Exclusion}`` (or a
    ``{label, conditions}`` dict), as its whole known state, set directly
    (tests, offline replays) rather than read from the graph: its manifest
    lists exactly these tables, none is unavailable, and the state is not
    read again. An empty map: no table of the system has a row exclusion."""
    from .row_exclusion_manifest import Entry
    if not system_id:
        return
    cleaned = {str(t).lower(): _as_fact(f) for t, f in (table_facts or {}).items()}
    cleaned = {t: f for t, f in cleaned.items() if f is not None}
    with _lock:
        _pinned.add(system_id)
    _set_state(system_id, cleaned, {}, {t: Entry("", False) for t in cleaned})


def _terminal(name: str) -> str:
    """The guard-facing TERMINAL table name: the last dotted part of a
    SchemaEntry name (``<namespace>.<table>`` -> ``<table>`` for S3 Tables), the
    name unchanged when it has no dot (Athena). The guard-visible registries
    (``_facts``, ``_unavailable``) are keyed by this, matching the terminal a
    guarded query names and the collision rule; the manifest/token bookkeeping
    (``_listed``, ``_settled``) keeps the real SchemaEntry name. ``ALL_TABLES``
    passes through unchanged."""
    if name == ALL_TABLES:
        return name
    return str(name or "").rpartition(".")[2] or str(name or "")


def _to_terminal_keys(mapping: dict) -> dict:
    """Re-key a ``{table: state}`` map by terminal name for the guard-visible
    registries. The steward route refuses an ambiguous terminal, so at most one
    state per terminal exists; a last-writer-wins collapse here is only ever a
    no-op in practice."""
    return {_terminal(t): v for t, v in mapping.items()}


def _set_state(system_id: str, present: dict, unavailable: dict, listed) -> None:
    """Replace the system's whole row-exclusion state in ONE assignment: the
    tables whose fact is known (``present``), those whose fact is not
    (``unavailable``, ``{table: Unavailable}``) and the manifest they were
    read from (``listed``, ``None`` when it is unknown); every other table is
    absent. ``present``/``unavailable`` are keyed under the guard-facing
    terminal name; ``listed`` keeps the real manifest names."""
    present = _to_terminal_keys(present)
    unavailable = _to_terminal_keys(unavailable)
    with _lock:
        for registry, value in ((_facts, present), (_unavailable, unavailable),
                                (_listed, listed)):
            if value or (registry is _listed and value is not None):
                registry[system_id] = dict(value)
            else:
                registry.pop(system_id, None)


def exclusion_for(system_id: str, table: str, *, quoted: bool = False) -> Exclusion | None:
    """The remembered exclusion for ``table``: an unquoted reference is looked
    up case-folded, a quoted one exactly (the remembered names are lowercase)."""
    lookup = str(table) if quoted else str(table).lower()
    with _lock:
        return _facts.get(system_id, {}).get(lookup)


def exclusions_for(system_id: str) -> dict:
    with _lock:
        return dict(_facts.get(system_id, {}))


def has_exclusions(system_id: str) -> bool:
    with _lock:
        return bool(_facts.get(system_id))


def reset() -> None:
    """Forget every system's row exclusions, unavailable states and manifests
    (tests)."""
    with _lock:
        for registry in (_facts, _unavailable, _listed, _settled, _stated):
            registry.clear()
        _pinned.clear()


def _checked_graph_read():
    """The CHECKED short-deadline graph read the system agent already makes
    per question (``live_concept_mappings._graph_read``: the local graph
    through ``db.neptune_query_checked``, else Neptune Analytics with short
    connect and read deadlines and one attempt). It raises when a read
    fails; the unchecked read answers no rows instead, which would look like
    a system or a table with no row exclusion. Short deadlines because it
    runs before a guarded query: a graph outage costs that query seconds at
    most, never the long statement limit."""
    from .live_concept_mappings import _graph_read
    return _graph_read


def _graph_query():
    """The graph read seam of the bake and its re-read (lazy import, patchable
    in tests): :func:`_checked_graph_read`. Raises on an import failure so the
    caller treats it as a failed read."""
    return _checked_graph_read()


def _unknown(last_read=()) -> dict:
    """The unavailable map of a system whose manifest could not be read:
    every data read of it is held (:func:`unavailable_for`). ``last_read``
    are the tables whose fact was read before, named in the hold's
    explanation only."""
    return {ALL_TABLES: Unavailable(ALL_TABLES, READ_FAILED, last_read=tuple(sorted(last_read)))}


def remember_from_graph(system_id: str, tables_desc: str = "") -> None:
    """The bake: read the system's row-exclusion manifest
    (``row_exclusion_manifest``) and the tables it lists (:func:`refresh`),
    keeping any last-known-good state this process already holds.

    Which tables carry an exclusion comes from the manifest alone, never from
    the baked table list (``tables_desc`` is not read), so an empty or
    unreadable list cannot hide one, and a system whose manifest is confirmed
    empty reads no fact at all. Never raises: a bake fault does not take the
    system agent down."""
    with _lock:
        _pinned.discard(system_id)
    refresh(system_id)


def refresh(system_id: str) -> None:
    """Bring the system's state up to date with ONE manifest read, then ONE
    batched read of only the listed tables that need it (:func:`_apply`).

    Before every guarded query, so a running agent sees a steward's set or
    clear on its next query; there is no pause after a failure, because a
    steward's write can land at any time. A manifest read that fails is tried
    once more at once (:data:`MANIFEST_READ_ATTEMPTS`), so one transient
    error changes nothing. When every attempt fails (or the read has no
    System row, or does not parse), every data read of the system is held
    (:func:`_manifest_unreadable`): a table the last manifest did not list
    may have been set since, and a fact last read may have been replaced
    since. Structural discovery is not held. The next query reads the
    manifest again and heals. While a re-provision's rewrite marker stands
    (``row_exclusion_manifest.REWRITE_KEY``), every listed table is held
    (:func:`_hold_for_rewrite`). A state set directly (:func:`remember`) is
    never re-read. Never raises."""
    if not system_id:
        return
    with _lock:
        lock = _refresh_locks.setdefault(system_id, threading.Lock())
    with lock:
        with _lock:
            if system_id in _pinned:
                return
        try:
            query = _graph_query()
            entries, rewriting = _read_manifest(system_id, query)
            if rewriting:
                _hold_for_rewrite(system_id, entries)
            else:
                _apply(system_id, entries, query)
        except Exception:  # noqa: BLE001 - which tables carry one is unknown
            _manifest_unreadable(system_id)


def _read_manifest(system_id: str, query) -> tuple:
    """``(entries, rewriting)``: the system's manifest entries, and whether a
    re-provision's rewrite marker stands, read up to
    :data:`MANIFEST_READ_ATTEMPTS` times; raises the last failure."""
    from . import row_exclusion_manifest as manifest
    for attempt in range(MANIFEST_READ_ATTEMPTS):
        try:
            raw, entries = manifest.read(system_id, query=query)
            return entries, manifest.rewrite_of(raw) is not None
        except Exception:  # noqa: BLE001 - tried once more, then raised
            if attempt + 1 >= MANIFEST_READ_ATTEMPTS:
                raise
    return {}, False   # not reached: MANIFEST_READ_ATTEMPTS >= 1


def _hold_for_rewrite(system_id: str, entries: dict) -> None:
    """A re-provision is carrying the system's row exclusions across its
    schema rewrite: every table the manifest lists is unavailable
    (:data:`REWRITE_IN_PROGRESS`), whatever this process last read for it,
    and no fact is read from the SchemaEntries being replaced. A table the
    manifest does not list carries none (no steward write lands while the
    marker stands), and structural discovery runs. The facts last read are
    kept as they are; the next read without the marker reads every listed
    table again (each is unavailable here, and each is re-tokened)."""
    # Guard-visible, so keyed by terminal name; ``_listed`` keeps the real
    # manifest names.
    held = {_terminal(t).lower(): Unavailable(t, REWRITE_IN_PROGRESS) for t in entries}
    with _lock:
        _listed[system_id] = dict(entries)
        if held:
            _unavailable[system_id] = held
        else:
            _unavailable.pop(system_id, None)


def _manifest_unreadable(system_id: str) -> None:
    """The manifest could not be read for this query: every data read of the
    system is held. No table is taken as carrying no exclusion (a steward's
    set may have landed just before the failed read), nor as carrying the
    fact last read for it (a steward may have replaced it since, with a rule
    neither stricter nor looser, and neither its token nor its content is
    known current). The facts last read are kept, to name in the hold's
    explanation and to compare against once a read succeeds. The next
    successful read replaces this hold."""
    with _lock:
        if system_id not in _listed:
            _facts.pop(system_id, None)
            _unavailable[system_id] = _unknown()
            return
        held = {t: u for t, u in _unavailable.get(system_id, {}).items() if t != ALL_TABLES}
        held.update(_unknown(last_read=_facts.get(system_id, {})))
        _unavailable[system_id] = held


def _apply(system_id: str, entries: dict, query) -> None:
    """Take a manifest read: drop the tables it no longer lists, keep the
    state of each settled table whose token is unchanged (last-known-good,
    present or absent), and re-read in one batch only the tables that are
    new, re-tokened, pending or unavailable (:func:`_read_tables`). The
    system's state is replaced in ONE assignment."""
    with _lock:
        prior_facts = dict(_facts.get(system_id, {}))
        prior_unavailable = dict(_unavailable.get(system_id, {}))
        settled = dict(_settled.get(system_id, {}))
    # The guard-visible registries are keyed by terminal name; this working set
    # is keyed by the real manifest name (``entries``), so map the carry-over
    # back through each listed table's terminal. ``_set_state`` re-terminalises
    # at the end. Only tables the manifest still lists are carried.
    facts, unavailable = {}, {}
    for real in entries:
        term = _terminal(real).lower()
        if term in prior_facts:
            facts[real.lower()] = prior_facts[term]
        if term in prior_unavailable:
            unavailable[real.lower()] = prior_unavailable[term]
    settled = {t: k for t, k in settled.items() if t in entries}
    stale = [t for t, e in entries.items()
             if e.pending or settled.get(t) != e.token or t.lower() in unavailable]
    states = _read_tables(system_id, stale, entries, query) if stale else {}
    for table, state in states.items():
        key = table.lower()
        facts.pop(key, None)
        unavailable.pop(key, None)
        settled.pop(table, None)
        if isinstance(state, Unavailable):
            unavailable[key] = state
            continue
        if state is not None:
            facts[key] = state
        if not entries[table].pending:
            settled[table] = entries[table].token
    _set_state(system_id, facts, unavailable, entries)
    with _lock:
        _settled[system_id] = settled


def _read_tables(system_id: str, tables: list, entries: dict, query) -> dict:
    """``{table: state}`` for ``tables``, from ONE read of their stored facts
    and ONE read of the Fields of those that hold one. A state is an
    :class:`Exclusion`, ``None`` (absent) or :class:`Unavailable`.

    A table the fact read answers no SchemaEntry row for is UNAVAILABLE
    (:data:`ENTRY_MISSING`), never absent. A matched row with no stored value
    is never absent either: a PENDING entry (a set or clear in progress, or
    interrupted between its writes, or a pending entry a re-provision carried
    with nothing stored) is :data:`WRITE_PENDING`, and a SETTLED entry means
    the fact was removed outside the steward route (:data:`FACT_MISSING`). A
    listed table is absent only once a clear has removed it from the
    manifest. A failed read, a stored
    value that no longer validates, or conditions that no longer fit the
    table's current Fields (``_column_error``, the route's own check) are
    unavailable too."""
    from . import schema_fields
    seids = {schema_fields.schema_entry_id(system_id, t): t for t in tables}
    try:
        rows = list(query(READ_ROW_EXCLUSIONS, {"sid": system_id, "seids": list(seids)}) or [])
    except Exception:  # noqa: BLE001 - a failed read is unavailable, not absent
        return {t: Unavailable(t, READ_FAILED) for t in tables}
    found = {r.get("seid"): r for r in rows if isinstance(r, dict) and r.get("seid") in seids}
    states: dict = {}
    for seid, table in seids.items():
        states[table] = _stored_state(table, entries[table], found.get(seid))
    present = {t: f for t, f in states.items() if isinstance(f, Exclusion)}
    states.update(_fitted_all(system_id, present, query))
    return states


def _stored_state(table: str, entry, row):
    """One table's state from its fact-read row (``None``: no row), with when
    the fact was set and from what source (:func:`_with_provenance`)."""
    if row is None:
        return Unavailable(table, ENTRY_MISSING)
    stored = row.get("row_exclusion")
    if not stored:
        return Unavailable(table, WRITE_PENDING if entry.pending else FACT_MISSING)
    fact = _parse_stored(stored)
    return _with_provenance(fact, row) if fact is not None else Unavailable(table,
                                                                           INVALID_STORED)


def _with_provenance(fact: Exclusion, row: dict) -> Exclusion:
    """``fact`` with the ``set_at`` / ``set_source`` of its read row, each a
    string or empty."""
    def text(key: str) -> str:
        value = row.get(key)
        return value if isinstance(value, str) else ""
    return replace(fact, at=text("set_at"), source=text("set_source"))


def _fitted_all(system_id: str, present: dict, query) -> dict:
    """Each present fact checked against its table's current Fields, read in
    ONE batch (:func:`_fitted`); a failed read leaves them unavailable."""
    from . import schema_fields
    if not present:
        return {}
    seids = {schema_fields.schema_entry_id(system_id, t): t for t in present}
    try:
        rows = list(query(FIELD_TYPES_BATCH, {"sid": system_id, "seids": list(seids)}) or [])
    except Exception:  # noqa: BLE001 - a failed read is unavailable, not absent
        return {t: Unavailable(t, READ_FAILED) for t in present}
    types: dict = {t: {} for t in present}
    for row in rows:
        table = seids.get(row.get("seid")) if isinstance(row, dict) else None
        if table is not None and row.get("name"):
            types[table][str(row.get("name")).lower()] = row.get("data_type")
    return {t: _fitted(t, fact, types[t]) for t, fact in present.items()}


def _fitted(table: str, fact: Exclusion, types: dict):
    """``fact`` when its conditions still fit the table's ``types``, else the
    :class:`Unavailable` that names the columns that no longer fit."""
    err = _column_error(list(fact.conditions), types)
    if err:
        reason, _detail, columns = err
        return Unavailable(table, reason, columns,
                           tuple(str(types.get(c) or "") for c in columns))
    return fact


def mark_unavailable(system_id: str, tables_desc: str = "") -> None:
    """A bake that could not run at all: the last-known-good state this
    process holds stands; with none, the system's manifest is UNKNOWN and
    every data read of it is held until a read succeeds. Never raises."""
    with _lock:
        _pinned.discard(system_id)
        if system_id not in _listed:
            _facts.pop(system_id, None)
            _unavailable[system_id] = _unknown()


def unavailable_for(system_id: str, sql: str):
    """The :class:`Unavailable` state that holds ``sql`` back, else ``None``,
    after the system's state is brought up to date (:func:`refresh`).

    A read that may reach an unavailable table
    (``row_exclusion_read.may_read``) is held, uncertain SQL included; while
    the manifest cannot be read, any read that may reach a data table is
    held (``row_exclusion_read.may_read_data``), a table with a fact last
    read included, and structural discovery (an ``information_schema``
    read) is not."""
    refresh(system_id)
    with _lock:
        entries = dict(_unavailable.get(system_id, {}))
    for table, entry in entries.items():
        if _may_read_data(sql) if table == ALL_TABLES else _may_read(sql, table):
            return entry
    return None


# --------------------------------------------------------------------------- #
# The schema fact line the system agent bakes
# --------------------------------------------------------------------------- #
def schema_lines(table_facts: dict) -> str:
    """One fact per table with a row exclusion, ``""`` for none: per the
    source owner, the table's records are the rows that satisfy the canonical
    predicate (shown once), and the rows that fail it are <label>, not
    records. Names print only through ``engine_names``."""
    from .engine_names import Withheld
    facts = {t: _as_fact(f) for t, f in (table_facts or {}).items()}
    facts = {t: f for t, f in facts.items() if f is not None}
    if not facts:
        return ""
    names = Withheld()
    parts = []
    for table, fact in facts.items():
        shown = names.show(table, "table")
        parts.append(f"{shown}: its records are the rows that satisfy "
                     f"{_shown_predicate(names, fact.conditions)}; the rows that fail it are "
                     f"{fact.label}, not records")
    return ("\n\nROW EXCLUSIONS (facts, from each table's source owner): some rows of these "
            f"tables are not records -- {'; '.join(parts)}. A count or list of a table's "
            "records covers only the rows that satisfy its predicate. The engine checks every "
            "read of such a table in a query (each UNION branch, each side of a join or "
            "self-join, each CTE, derived table or subquery): a read covers only records when "
            "its own WHERE ANDs the predicate, each column bound to that read's alias when the "
            "read is joined; a predicate in another branch, on another alias or in the outer "
            f"query does not apply to it.{names.sentence()}")


def current_schema_lines(system_id: str) -> str:
    """The ROW EXCLUSIONS for ONE question, from the system's state read
    again for it (:func:`refresh`): the rules that read confirmed
    (:func:`schema_lines`) and, once any rule was stated to the agent before
    (:data:`_stated`), a statement that these are the rules as of this
    question and replace any stated earlier in the conversation, naming each
    table stated earlier that has no rule now (every row is a record) or
    whose rule cannot be confirmed now (its reads are held). The system
    agent is a long-lived runtime whose conversation keeps earlier
    questions, so a rule said once stays in front of the model until a
    later question says otherwise; it is said again on every question, as
    the earlier block may still be in the conversation. A table held, or any
    table while the manifest cannot be read, is never stated as carrying a
    rule. Sent with each question, never baked into the system prompt. The
    engine enforces the same state on every query."""
    refresh(system_id)
    with _lock:
        held = dict(_unavailable.get(system_id, {}))
        facts = dict(_facts.get(system_id, {}))
        stated = set(_stated.get(system_id, ()))
    unknown = ALL_TABLES in held
    present = {} if unknown else {t: f for t, f in facts.items() if t not in held}
    lines = schema_lines(present)
    _mark_stated(system_id, present)
    if not stated:
        return lines
    changed = sorted(stated - set(present))
    return (f"\n\nROW EXCLUSIONS as of this question: these replace any row exclusion stated "
            f"earlier in this conversation.{lines}{_superseded_text(changed, held, unknown)}")


def _mark_stated(system_id: str, tables) -> None:
    """Note that ``tables``' rules were stated to the system agent."""
    if not tables:
        return
    with _lock:
        _stated.setdefault(system_id, set()).update(str(t).lower() for t in tables)


def _superseded_text(tables: list, held: dict, unknown: bool) -> str:
    """For the tables whose rule was stated earlier and is not stated now:
    no rule now, or its rule cannot be confirmed now (``""`` for none)."""
    if not tables:
        return ""
    from .engine_names import Withheld
    names = Withheld()
    parts = []
    for table in tables:
        shown = names.show(table, "table")
        if unknown or table in held:
            parts.append(f"{shown}: its row exclusion cannot be confirmed now, so its reads "
                         "are held")
        else:
            parts.append(f"{shown}: no row exclusion now, so every row of it is a record")
    return f"\n\nTables whose row exclusion was stated earlier: {'; '.join(parts)}." + (
        names.sentence())


# --------------------------------------------------------------------------- #
# Reading the query (the reader is row_exclusion_read)
# --------------------------------------------------------------------------- #
def classify(sql: str, table: str, fact: Exclusion | None) -> Reading:
    """How ``sql`` reads ``table`` against its exclusion ``fact``
    (``row_exclusion_read.classify``, see the module docstring): ``applied``,
    ``missing``, ``undecided`` (with why) or ``not_read``."""
    return _read(sql, table, [c.key() for c in fact.conditions] if fact is not None else ())


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExclusionRead:
    """A query that reads, or may read, ``table``: with the source owner's
    row exclusion on every read (``applied``) or not. Not applied, the query
    is refused once. ``sites`` are the reads that lack the predicate
    (``row_exclusion_read.ReadSite``), ``reason`` why the reader could not
    prove the query (``""`` when it read every read with certainty); neither
    is part of what makes two reads the same (the disclosure is per table)."""

    table: str
    label: str
    conditions: tuple
    applied: bool
    at: str = field(default="", compare=False)
    source: str = field(default="", compare=False)
    sites: tuple = field(default=(), compare=False)
    reason: str = field(default="", compare=False)

    @property
    def refuse(self) -> bool:
        return not self.applied

    def to_dict(self, names=None) -> dict:
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        out = {"table": names.show(self.table, "table"), "label": self.label,
               "applied": self.applied,
               "predicate": _shown_predicate(names, self.conditions)}
        if self.named_sites():
            out["reads"] = [{"read": _site_text(names, self.table, s),
                             "predicate": _shown_predicate(names, self.conditions, s.qualifier)}
                            for s in self.sites]
        if names.count:
            out["names_withheld"] = names.count
        return out

    def named_sites(self) -> bool:
        """True when the reads lacking the predicate are worth naming: more
        than one read, one bound to its alias in a joined block, or one in an
        expression subquery. A lone plain read keeps the one-read text."""
        return any(s.total > 1 or s.qualifier or s.subquery for s in self.sites)


_SUBQUERY_TEXT = {"in": "in an IN subquery", "exists": "in an EXISTS subquery",
                  "scalar": "in a scalar subquery"}


def _ordinal(n: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th`` ... ``11th`` ... ``21st``."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _site_text(names, table: str, site) -> str:
    """One read for engine text: which read of the table it is (in the order
    the query names them) and where it is."""
    shown = names.show(table, "table")
    head = (f"the read of {shown}" if site.total == 1
            else f"the {_ordinal(site.ordinal)} read of {shown}")
    where = []
    if site.alias:
        where.append(f"alias {names.show(site.alias, 'alias')}")
    if site.branch:
        where.append(f"branch {site.branch} of the {site.set_operator}")
    if site.subquery:
        where.append(_SUBQUERY_TEXT[site.subquery])
    if site.cte:
        where.append(f"in the CTE {names.show(site.cte, 'alias')}")
    return head + (f" ({', '.join(where)})" if where else "")


def decide(sql: str, table: str, fact: Exclusion | None) -> ExclusionRead | None:
    """The read of ``table`` with or without its exclusion that ``sql`` runs,
    else ``None`` when ``sql`` certainly does not read it (:func:`classify`).
    A statement that may read it but cannot be proven (``undecided``) is a
    read without the exclusion: refused once, then disclosed."""
    reading = classify(sql, table, fact)
    if reading.kind not in {"applied", "missing", "undecided"}:
        return None
    return ExclusionRead(str(table), fact.label, fact.conditions, reading.kind == "applied",
                         at=fact.at, source=fact.source, sites=reading.sites,
                         reason=reading.reason)


def card_record(read) -> dict | None:
    """The rule a query ran with, as its call's evidence card shows it:
    ``{"table", "label", "conditions", "at", "source"}``, the table, the
    owner's label and conditions (``{column, op[, values]}``) exactly as the
    read applied them, and when the rule was set and from what source (empty
    when unknown). Who set it is never carried: the card says an
    administrator did. ``None`` for no read or a read WITHOUT the rule: the
    card never says a rule was applied when it was not. Kept outside the
    tool result (``system_agent._keep_applied_exclusion``)."""
    if not isinstance(read, ExclusionRead) or not read.applied:
        return None
    return {"table": read.table, "label": read.label,
            "conditions": [c.to_dict() for c in read.conditions],
            "at": read.at, "source": read.source}


def decide_all_for(system_id: str, sql: str) -> tuple:
    """:func:`decide` for EVERY table ``system_id`` has an exclusion for:
    one :class:`ExclusionRead` per table ``sql`` reads or may read, in the
    registry's order (a join of two rule-bearing tables gives two). A
    decision states that table's rule to the system agent (its refusal or
    disclosure carries the predicate), so it is noted as stated
    (:func:`current_schema_lines`)."""
    found = []
    for table, fact in exclusions_for(system_id).items():
        read = decide(sql, table, fact)
        if read is not None:
            _mark_stated(system_id, [table])
            found.append(read)
    return tuple(found)


def decide_for(system_id: str, sql: str) -> ExclusionRead | None:
    """The first of :func:`decide_all_for`, else ``None``."""
    found = decide_all_for(system_id, sql)
    return found[0] if found else None


# --------------------------------------------------------------------------- #
# The refusal and the disclosures: facts, names only when printable
# --------------------------------------------------------------------------- #
#: Why the reader could not prove a query (``row_exclusion_read`` reasons).
_UNPROVEN_WHY = {
    "uncertain_tokens": "its text could not be read with certainty",
    "several_statements": "it holds more than one statement",
    "uncertain_relations": "a relation it reads could not be read with certainty",
    "uncertain_with": "its WITH clause is recursive or could not be read",
    "conditions_elsewhere": ("the owner's conditions are written outside the WHERE of the "
                             "read that needs them"),
}


def _refusal_opening(names, f: ExclusionRead) -> str:
    """What the refused query does, after "Refused before running: "."""
    table = names.show(f.table, "table")
    if f.reason:
        why = _UNPROVEN_WHY.get(f.reason, "its shape could not be read with certainty")
        return (f"the engine could not confirm that every read of {table} in this query "
                f"carries the source owner's row exclusion ({why}), so this query may read "
                f"{table} {MARKER_MISSING}")
    total = max((s.total for s in f.sites), default=1)
    if f.named_sites() and total > 1:
        lacking = len(f.sites)
        count = ("both" if total == 2 else f"all {total}") if lacking == total \
            else f"{lacking} of the {total}"
        return f"this query reads {table} {MARKER_MISSING} in {count} of its reads of {table}"
    return f"this query reads {table} {MARKER_MISSING}"


def _refusal_remedy(names, f: ExclusionRead) -> str:
    """The exact predicate to AND in, and where: into each named read's own
    WHERE (bound to its alias when it is joined), else into the WHERE of the
    query that reads the table."""
    table = names.show(f.table, "table")
    predicate = _shown_predicate(names, f.conditions)
    if f.named_sites():
        each = "; ".join(f"{_site_text(names, f.table, s)}: AND "
                         f"{_shown_predicate(names, f.conditions, s.qualifier)}"
                         for s in f.sites)
        return (f"To count or list records, AND the predicate into the WHERE of each read "
                f"of {table} that lacks it, in that read's own query block: {each}. Every "
                f"read of {table} needs it in its own WHERE: a predicate in another branch, "
                "on another alias or in the outer query does not apply to that read.")
    if f.reason:
        return (f"To count or list records, write the query as one statement in which each "
                f"read of {table} is a plain FROM {table} whose own WHERE has AND {predicate} "
                "(each column bound to that read's alias when the read is joined).")
    return (f"To count or list records, AND the predicate into the WHERE of the query that "
            f"reads {table}: AND {predicate}.")


class RowExclusionError(StorageBoundError):
    """A query refused before it ran for reading, or possibly reading, a
    table without its source owner's row exclusion on every read (reason
    :data:`REASON_ROW_EXCLUSION`)."""

    def __init__(self, found: ExclusionRead):
        super().__init__(StorageBound(found.table, (), (), ()), (), (),
                         reason=REASON_ROW_EXCLUSION)
        self.found = found

    def to_dict(self) -> dict:
        from .engine_names import Withheld
        f, names = self.found, Withheld()
        table = names.show(f.table, "table")
        predicate = _shown_predicate(names, f.conditions)
        guidance = (
            f"Refused before running: {_refusal_opening(names, f)}. Per the source "
            f"owner, the records of {table} are the rows that satisfy {predicate}; the rows "
            f"that fail it are {f.label}, not records, so this query's figures would include "
            f"any such rows. {_refusal_remedy(names, f)} If you mean to include those rows, "
            "send the same query again unchanged: it runs, and the answer says its figures may "
            f"include rows the source owner marks as {f.label}.")
        out = {
            "error": "row_exclusion",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": REASON_ROW_EXCLUSION,
            "retryable": True,
            **f.to_dict(names),
            "guidance": guidance + names.sentence(),
        }
        if f.reason:
            out["unproven"] = f.reason
        if names.count:
            out["names_withheld"] = names.count
        return out


def _distinct(windows) -> list:
    out = []
    for window in windows or ():
        if isinstance(window, (ExclusionRead, Unavailable)) and window not in out:
            out.append(window)
    return out[:6]


def _shown_table(names, entry: Unavailable) -> str:
    """The unavailable entry's table as engine text: :data:`ALL_TABLES` is
    "this system's tables"."""
    if entry.table == ALL_TABLES:
        return "this system's tables"
    return names.show(entry.table, "table")


#: The most tables a hold's explanation names (``Unavailable.last_read``).
_MAX_LAST_READ_SHOWN = 3


def _last_read_clause(names, entry: Unavailable) -> str:
    """For a system-wide hold: the tables whose fact was read before, which
    may have been replaced since, as a clause (``""`` for none)."""
    if entry.table != ALL_TABLES or not entry.last_read:
        return ""
    shown = [names.show(t, "table") for t in entry.last_read[:_MAX_LAST_READ_SHOWN]]
    more = len(entry.last_read) - len(shown)
    tables = " and ".join(shown) + (f" and {more} more" if more else "")
    return f", and the one last read for {tables} may have changed since"


def _unavailable_why(names, entry: Unavailable) -> str:
    """Why the table's row exclusion is not known, as a clause after "the
    source owner's row exclusion for <table>"."""
    from .engine_names import printable
    if entry.kind == READ_FAILED:
        return "could not be read" + _last_read_clause(names, entry)
    if entry.kind == ENTRY_MISSING:
        return "could not be read (the table is not in the graph)"
    if entry.kind == FACT_MISSING:
        return "is listed for this system but is no longer stored"
    if entry.kind == WRITE_PENDING:
        return "is being changed by a steward, or a change to it was interrupted"
    if entry.kind == REWRITE_IN_PROGRESS:
        return "is being carried across a re-provision of this system's schema"
    if entry.kind == INVALID_STORED:
        return "is stored in a form that no longer validates"
    shown = [names.show(c) for c in entry.columns] or ["a condition column"]
    if entry.kind == "unknown_column":
        return ("no longer fits the table: " + " and ".join(shown)
                + (" is" if len(shown) == 1 else " are") + " no longer "
                + ("a column" if len(shown) == 1 else "columns") + " of it")
    if entry.kind == "column_not_string":
        parts = []
        for column, data_type in zip(shown, entry.types or ("",) * len(shown)):
            kind = data_type if printable(data_type) else "of a type that is not a string"
            parts.append(f"{column} is now {kind}, not a string column")
        return "no longer fits the table: " + "; ".join(parts)
    return "no longer fits the table: a condition column is not a plain identifier"


def _unavailable_repair(names, entry: Unavailable) -> str:
    """The steward repair sentence for an unavailable state, ``""`` when it
    heals by itself (a failed read is read again before the next query)."""
    if entry.kind == READ_FAILED:
        return ""
    table = _shown_table(names, entry)
    if entry.kind == ENTRY_MISSING:
        return f"If {table} is gone, a steward must clear its row exclusion."
    if entry.kind == FACT_MISSING:
        return (f"A steward must set the row exclusion of {table} again, or clear it, "
                "before its figures can be confirmed.")
    if entry.kind == WRITE_PENDING:
        return (f"It is read again before the next query; if no steward is changing the row "
                f"exclusion of {table} now, a steward must set it again, or clear it, before "
                "its figures can be confirmed.")
    if entry.kind == REWRITE_IN_PROGRESS:
        return (f"It is read again once the re-provision ends; if that re-provision stopped, "
                f"provisioning this system again restores the row exclusion of {table}.")
    columns = " and ".join(names.show(c) for c in entry.columns)
    on = f" (change or remove its condition on {columns})" if columns else ""
    return (f"A steward must repair or clear the row exclusion of {table} before its "
            f"figures can be confirmed{on}.")


class RowExclusionUnavailableError(StorageBoundError):
    """A read refused before it ran, every time, because the table's row
    exclusion is not known (reason :data:`REASON_UNAVAILABLE`): its figures
    cannot be confirmed. No resend path: the same query is refused again until
    the fact can be read."""

    def __init__(self, entry: Unavailable):
        super().__init__(StorageBound(entry.table, (), (), ()), (), (),
                         reason=REASON_UNAVAILABLE)
        self.entry = entry

    def to_dict(self) -> dict:
        from .engine_names import Withheld, printable
        e, names = self.entry, Withheld()
        table = _shown_table(names, e)
        repair = _unavailable_repair(names, e)
        guidance = (
            f"Refused before running: the source owner's row exclusion for {table} "
            f"{_unavailable_why(names, e)}, so figures from {table} cannot be confirmed: the "
            f"engine cannot tell which of its rows are records.{' ' + repair if repair else ''} "
            f"Do not send this query again; say that figures from {table} could not be "
            "confirmed, and why.")
        out = {
            "error": "row_exclusion_unavailable",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME_UNAVAILABLE,
            "reason": REASON_UNAVAILABLE,
            "retryable": False,
            "table": e.table if printable(e.table) else "",
            "unavailable": e.kind,
            "columns": [c for c in e.columns if printable(c)],
            "guidance": guidance + names.sentence(),
        }
        if names.count:
            out["names_withheld"] = names.count
        return out


def _line_part(names, w) -> str:
    if isinstance(w, Unavailable):
        table = _shown_table(names, w)
        return (f"a read of {table} was refused because the source owner's row exclusion for "
                f"it {_unavailable_why(names, w)}, so figures from {table} could not be "
                "confirmed")
    table = names.show(w.table, "table")
    if w.applied:
        return (f"{table} was read {MARKER_APPLIED}, so rows the source owner marks as "
                f"{w.label} were excluded")
    read = "may have been read" if w.reason else "was read"
    return (f"{table} {read} {MARKER_MISSING}, so its figures may include rows the source "
            f"owner marks as {w.label}, which are not records")


def engine_line(windows) -> str:
    """One bounded engine line, as facts, for the reads of a table with an
    exclusion this call's queries ran, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    names = Withheld()
    parts = [_line_part(names, w) for w in shown]
    return f"{ENGINE_PREFIX} check, in this turn's queries: {'; '.join(parts)}.{names.sentence()}"


def _requirement(names, w) -> str:
    if isinstance(w, Unavailable):
        table = _shown_table(names, w)
        return (f"REQUIRED: the answer must say that figures from {table} could not be "
                f"confirmed, because the source owner's row exclusion for it "
                f"{_unavailable_why(names, w)}.")
    table = names.show(w.table, "table")
    if w.applied:
        return (f"REQUIRED: the answer must say, in one short clause, that it excluded rows of "
                f"{table} the source owner marks as {w.label} (not records).")
    return (f"REQUIRED: the answer must say that its figures from {table} may include rows the "
            f"source owner marks as {w.label}, which are not records: the query did not apply "
            "the owner's row exclusion.")


def requirement_block(windows) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these reads, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes = [_requirement(names, w) for w in shown]
    if names.count:
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
