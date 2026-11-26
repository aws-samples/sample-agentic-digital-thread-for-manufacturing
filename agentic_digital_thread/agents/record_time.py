# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A record's own time column (a struct sibling), and a window on another time.

Measured on an analytics deployment, in a live dry run on one build: asked for
a daily count of quality defects by a defect reason code over a window, a SQL
system agent defined the population by the defect code column (a flattened
struct field ``quality_data.defect_type``) and windowed on a lifecycle time
(``fail_date``) the question did not name, pinning ``state = 'FAILED'`` on top.
The defect record has its OWN time of its own (the one sibling timestamp under
its struct, ``quality_data.defect_detected_date``), and on that column the same
defect is tens of thousands, not three. The STATE TIME check does not fire (it
counts FAILED rows on FAILED's own column), and nothing paired the defect field
with its sibling timestamp. So the engine enforces it.

* :func:`own_time_column` is the schema fact (brief item 1): for a flattened
  column ``a.b.c``, its own time column is the single date or timestamp column
  named ``a.b.<x>`` (the same full parent prefix, one more segment), when
  exactly one such column exists in that table; else none. A top-level column
  (no dot) has no own time column, and two sibling timestamps under one parent
  are ambiguous, so none. Read from the schema the system agent is baked with
  (the catalog column types ``state_times.from_tables_desc`` reads too).

* :func:`decide` reads one certain single-table query (the readers
  ``state_times`` and ``storage_bound_rule`` use; same certainty, anything
  uncertain gives no decision). It fires when

  - the WHERE (top-level AND) constrains a column ``C`` that has an own time
    column ``T`` (``=``, ``IN``, ``LIKE``, ``IS NOT NULL``, ``<> ''``,
    ``<> '[]'`` on ``C``; a GROUP BY alone does not count), and
  - the query bounds at least one date or timestamp column, none of them
    ``T``.

  The delegated question is not read. Word rules could not tell a question
  that names a time from one that rejects it ("excluding failure date",
  "failure date is not the intended window"), and reading a rejection as a
  request ran the rejected window with no refusal. A question that does want
  the other window costs one refusal: the identical resend runs, disclosed.

  Then the caller (the system agent's guarded Athena tool) refuses it the first time on the ``StorageBoundError`` path
  (:class:`RecordTimeWindowError`, reason :data:`REASON_RECORD_TIME`) with a
  remedy: window on ``T``, or send the query again if the question wants that
  other window. The identical query sent again runs, disclosed by :func:`engine_line` and :func:`requirement_block`,
  the same shape and marker discipline as ``state_times``: the answer must say
  the figure counts records by a window column ``W``, not by ``C``'s own time
  ``T``, and that records with no ``W`` are not counted. Under
  ``RESPONSE_INTEGRITY_MODE=enforce`` the disclosure would be silent, so the
  first send is refused there (:func:`_disclosure_dropped`).

Not a window on another time: the query bounds ``T`` (the record's own time);
``C`` has no own time column; the
query bounds no other time column; more than one table; anything the reader
cannot read with certainty. A table or column name is printed only when
``engine_names.printable`` passes. Generic: no column, table or system names.
Pure apart from the per-system registry the system agent fills at construction.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from .athena_exact_shape import _split_commas
from .athena_scope import _split_boolean, _strip_outer, _where_expression, relations, tokenize
from .storage_bound_rule import StorageBound, StorageBoundError, _temporal
from .state_times import _string

ERROR_NAME = "RECORD_TIME_WINDOW"
REASON_RECORD_TIME = "window_not_on_record_time"
#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] Record time window"
MARKER = "other than record time"
_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*")
#: String literals that ``<>`` treats as "the field carries nothing".
_EMPTY_LITERALS = ("", "[]")
_lock = threading.Lock()
_types: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# The schema fact: a flattened column's own sibling time column
# --------------------------------------------------------------------------- #
def own_time_column(column: str, column_types) -> str | None:
    """``column``'s own time column: the single date or timestamp column whose
    name is ``<column's parent prefix><one more segment>`` (one sibling under
    the same flattened struct), when exactly one exists in ``column_types``
    (a ``{name: type}`` map). ``None`` for a top-level column (no dot), for a
    column that is itself a date, timestamp or time-of-day (a time is never a
    population field, so it has no own time), for a column with no sibling
    timestamp, and for two or more sibling timestamps (ambiguous). ``column``
    itself is never its own time column."""
    name = str(column or "").strip().lower()
    head, sep, _last = name.rpartition(".")
    if not sep:
        return None
    if _time_like(_own_type(name, column_types)):
        return None
    prefix = head + sep
    hits = [str(c) for c, t in (column_types or {}).items()
            if str(c).lower() != name and str(c).lower().startswith(prefix)
            and "." not in str(c).lower()[len(prefix):] and _temporal(t)]
    return hits[0] if len(hits) == 1 else None


def _own_type(name: str, column_types) -> str:
    """``name``'s catalog type, the map's keys compared without case (as the
    sibling scan compares them). ``""`` when the map has no such column."""
    for column, data_type in (column_types or {}).items():
        if str(column).lower() == name:
            return str(data_type or "")
    return ""


def _time_like(data_type: str) -> bool:
    """A date, timestamp or time-of-day type, whatever its precision or zone.
    Wider than the window reader's date/timestamp test on purpose: a
    time-of-day is still a time, never a value that picks records. An empty or
    unknown type is not a time, so the column keeps the value-field reading."""
    return _temporal(data_type) or str(data_type or "").strip().lower().startswith("time")


# --------------------------------------------------------------------------- #
# The per-system registry (parallels state_times / storage_bounds)
# --------------------------------------------------------------------------- #
def remember(system_id: str, column_types: dict) -> None:
    """Keep ``system_id``'s column types for its own-time lookups."""
    if not system_id:
        return
    with _lock:
        if column_types:
            _types[system_id] = dict(column_types)
        else:
            _types.pop(system_id, None)


def _desc_column_types(tables_desc: str) -> dict:
    """``{column (lowercased): type}`` from a baked ``tables_desc``, over every
    table's columns (dotted struct fields kept whole), the same source
    ``state_times.from_tables_desc`` reads."""
    from .state_times import _fragment
    from .system_agent import _desc_entries
    out: dict = {}
    for _table, fragments, _marked, _hidden in _desc_entries(tables_desc or ""):
        for fragment in fragments:
            name, data_type, _samples = _fragment(fragment)
            if name and _COLUMN_RE.fullmatch(name):
                out[name.lower()] = data_type
    return out


def remember_tables_desc(system_id: str, tables_desc: str) -> None:
    """Keep ``system_id``'s column types from its baked ``tables_desc`` when at
    least one flattened column has an own sibling time column, so the gate
    (:func:`has_types`) is true only for a system the rule could ever fire on.
    A schema with no such pairing stores nothing."""
    types = _desc_column_types(tables_desc)
    usable = types if any(own_time_column(c, types) for c in types) else {}
    remember(system_id, usable)


def types_for(system_id: str) -> dict:
    with _lock:
        return dict(_types.get(system_id, {}))


def has_types(system_id: str) -> bool:
    with _lock:
        return bool(_types.get(system_id))


def reset() -> None:
    """Forget every system's column types (tests)."""
    with _lock:
        _types.clear()


# --------------------------------------------------------------------------- #
# Reading the query: the columns the WHERE constrains
# --------------------------------------------------------------------------- #
def _upper(token) -> str:
    return token.value.upper() if token.kind == "word" else ""


def _chain(tokens) -> str | None:
    """The dotted name a reference spells, as written (lowercased): a bare,
    quoted or qualified identifier, possibly several segments deep
    (``quality_data.defect_type``), else ``None`` for anything wrapped,
    computed or compound (``LOWER(c)``, ``a + b``)."""
    tokens = _strip_outer(tokens)
    if not tokens:
        return None
    parts = []
    for i, token in enumerate(tokens):
        if i % 2 == 0:
            if token.kind not in {"word", "identifier"}:
                return None
            parts.append(str(token.value))
        elif token.text != ".":
            return None
    if len(tokens) % 2 == 0:
        return None
    return ".".join(parts).lower()


def _pin_chain(part) -> str | None:
    """The column of ``<column> = <literal>`` or ``<column> IN (literals)``
    (top-level AND conjunct), keeping its full dotted spelling, else
    ``None``."""
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    if any(word == "NOT" for _i, word in words):
        return None
    for i, word in words:
        if word == "IN":
            values = _strip_outer(part[i + 1:])
            if i + 1 >= len(part) or part[i + 1].text != "(" or not values:
                return None
            got = [_string(item) for item in _split_commas(values)]
            return _chain(part[:i]) if got and not any(g is None for g in got) else None
    for i, token in enumerate(part):
        if token.depth == base and token.kind == "operator":
            if token.text != "=":
                return None
            for side, other in ((part[:i], part[i + 1:]), (part[i + 1:], part[:i])):
                if _string(other) is not None:
                    chain = _chain(side)
                    if chain:
                        return chain
            return None
    return None


def _like(part) -> str | None:
    """The column of ``<column> LIKE <literal>`` (not ``NOT LIKE``), else
    ``None``. A wrapped column (``LOWER(c) LIKE ...``) is not read here."""
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    if any(word == "NOT" for _i, word in words):
        return None
    at = next((i for i, word in words if word == "LIKE"), None)
    if at is None or _string(part[at + 1:]) is None:
        return None
    return _chain(part[:at])


def _null_or_empty(part) -> str | None:
    """The column of ``<column> IS NOT NULL``, ``<column> <> ''`` or
    ``<column> <> '[]'``, else ``None``: a constraint that the field carries a
    value, the way the live query defined its population."""
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    at = next((i for i, word in words if word == "IS"), None)
    if at is not None:
        rest = [word for i, word in words if i > at]
        if rest == ["NOT", "NULL"] and len(part) == at + 3:
            return _chain(part[:at])
        return None
    for i, token in enumerate(part):
        if token.depth == base and token.kind == "operator" and token.text in ("<>", "!="):
            value = _string(part[i + 1:])
            return _chain(part[:i]) if value in _EMPTY_LITERALS else None
    return None


def _resolve(spelling: str, column_types: dict) -> str | None:
    """The flattened column of ``column_types`` ``spelling`` names (dotted or
    a trailing part of a dotted column, ``dotted_columns``), else ``None``
    when none or more than one matches."""
    from .dotted_columns import ONE, read_column
    columns = [("t", str(c)) for c in column_types or {}]
    direct = {str(c).lower() for c in column_types or {}}
    if spelling in direct:
        return spelling
    reading = read_column(spelling, columns)
    return reading.column.lower() if reading.status == ONE else None


def _constrained(where, column_types: dict) -> set:
    """The flattened columns the top-level AND of ``where`` constrains in a
    way that defines the population: ``=``, ``IN``, ``LIKE``, ``IS NOT NULL``,
    ``<> ''`` or ``<> '[]'``. Nested ANDs are flattened (the scan guard wraps
    the model's WHERE in one parenthesised group), but a conjunct under a
    top-level OR, or any other form, is not read. Each spelling is resolved
    against the schema so a dotted struct field (``quality_data.defect_type``)
    is read whole."""
    from .state_times import _conjuncts
    out: set = set()
    for part in _conjuncts(where):
        if len(_split_boolean(part, "OR")) > 1:
            continue
        for reader in (_pin_chain, _like, _null_or_empty):
            spelling = reader(part)
            if spelling:
                resolved = _resolve(spelling, column_types)
                if resolved:
                    out.add(resolved)
                break
    return out


def sql_candidate(sql: str) -> bool:
    """A cheap, catalog-free pre-check: ``sql`` is a single-table read whose
    WHERE constrains some DOTTED column (a possible struct field) and bounds
    at least one column. The eligibility gate uses it so a query that could
    never pair a struct field with a sibling time reads no catalog; the real
    decision (:func:`decide`) reads the flattened catalog types.

    The window bound is read through the shared ``bounded_columns`` with the
    same-column OR form on (one implementation of that rule), so a window
    written as an OR of ranges on one column makes the query eligible. The
    population constraint below keeps its own OR skip: a value column under a
    top-level OR is not a population constraint."""
    from .storage_bound_rule import bounded_columns
    tokens, uncertain = tokenize(sql if isinstance(sql, str) else "")
    if uncertain or not tokens:
        return False
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return False
    read = bounded_columns(sql, include_same_column_or=True)
    if not read or not read[1]:
        return False
    where = _where_expression(tokens, scans[0]) or []
    from .state_times import _conjuncts
    for part in _conjuncts(where):
        if len(_split_boolean(part, "OR")) > 1:
            continue
        for reader in (_pin_chain, _like, _null_or_empty):
            spelling = reader(part)
            if spelling and "." in spelling:
                return True
    return False


def _disclosure_dropped() -> bool:
    """True under ``RESPONSE_INTEGRITY_MODE=enforce``, which appends no engine
    lines: a window that would only be disclosed is refused there instead."""
    from .config import response_integrity_enforced
    return response_integrity_enforced()


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecordTimeWindow:
    """A query that defines its population by ``population_column`` (whose
    record carries its own time ``own_column``) and windows on ``windows``,
    none of them ``own_column``."""

    table: str
    population_column: str
    own_column: str
    windows: tuple           # the bounded time columns, none the own column
    conjuncts: tuple = ()
    #: Always refuse-once: the rule reads the query alone, so it is never
    #: only-disclosed.
    refuse: bool = True

    def to_dict(self, names=None) -> dict:
        """The record a result or a refusal carries. A name that is not a
        plain identifier (``engine_names``) is a placeholder, counted in
        ``names_withheld``."""
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        window = self.windows[0] if len(self.windows) == 1 else None
        out = {"table": names.show(self.table, "table"),
               "population_column": names.show(self.population_column),
               "own_column": names.show(self.own_column),
               "window_column": names.show(window) if window else None,
               "windows": [names.show(w) for w in self.windows]}
        if names.count:
            out["names_withheld"] = names.count
        return out


def decide(sql: str, table: str, bounded: dict, column_types: dict) -> RecordTimeWindow | None:
    """The record-own-time window this query runs with, else ``None`` (see the
    module docstring). ``bounded`` is ``storage_bound_rule.bounded_columns``'s
    top-level-AND map for the query, ``column_types`` the catalog's."""
    tokens, uncertain = tokenize(sql if isinstance(sql, str) else "")
    scans, scan_uncertain = relations(tokens) if not uncertain and tokens else ([], True)
    if scan_uncertain or len(scans) != 1:
        return None
    temporal = {str(c).lower() for c, t in (column_types or {}).items() if _temporal(t)}
    windows = sorted(c for c in (bounded or {}) if c in temporal)
    if not windows:
        return None
    where = _where_expression(tokens, scans[0]) or []
    for column in sorted(_constrained(where, column_types)):
        own = own_time_column(column, column_types)
        if own is None or own.lower() in windows:
            continue
        conjuncts = tuple(bounded[w] for w in windows)
        flat = tuple(text for texts in conjuncts for text in texts)[:4]
        return RecordTimeWindow(table, column, own, tuple(windows), flat,
                                refuse=True)
    return None


# --------------------------------------------------------------------------- #
# The refusal and the disclosure: facts, names only when printable
# --------------------------------------------------------------------------- #
def _window_text(names, windows) -> str:
    return " and ".join(names.show(w) for w in windows)


def _row_fact(names, window: RecordTimeWindow, *, ran: bool = True) -> str:
    """The window, as facts: which time column the records were counted by,
    that it is not the record's own time, and that records with no window time
    are not counted (``ran``), or would not be (a refusal)."""
    population = names.show(window.population_column)
    own = names.show(window.own_column)
    windows = _window_text(names, window.windows)
    were, counted = ("were", "are not counted") if ran else ("are", "would not be counted")
    missing = names.show(window.windows[0]) if len(window.windows) == 1 else "those columns"
    return (f"records selected by {population} {were} counted by {windows}, not by the "
            f"record's own time {own}, so records with no {missing} {counted}")


class RecordTimeWindowError(StorageBoundError):
    """A query refused before it ran for counting records of a population
    column by a time column that is not the record's own time
    (reason :data:`REASON_RECORD_TIME`)."""

    def __init__(self, found: RecordTimeWindow):
        super().__init__(StorageBound(found.table, (), tuple(found.windows), found.conjuncts),
                         (), tuple(found.windows), reason=REASON_RECORD_TIME)
        self.found = found

    def to_dict(self) -> dict:
        from .engine_names import Withheld
        f, names = self.found, Withheld()
        table = names.show(f.table, "table")
        population = names.show(f.population_column)
        own = names.show(f.own_column)
        fact = _row_fact(names, f, ran=False)
        what = (f"Refused before running: this query selects records of {table} by "
                f"{population}, and {fact}. A record's own time {own} records when the "
                "selected event happened; another time column can be empty or outside the "
                "window when the record's own time is inside it, so the result may not count "
                "the records the question means.")
        guidance = (f"{what} {population} is recorded with its own time {own}: window on "
                    f"{own}. If the question asks for that other "
                    "window itself, send the same query again unchanged: it runs, and the "
                    "answer says how those records were counted.")
        record = f.to_dict(names)
        out = {
            "error": "record_time_window",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": REASON_RECORD_TIME,
            "retryable": True,
            **record,
            "guidance": guidance + names.sentence(),
        }
        if names.count:
            out["names_withheld"] = names.count
        return out


def _distinct(windows) -> list:
    out = []
    for window in windows or ():
        if isinstance(window, RecordTimeWindow) and window not in out:
            out.append(window)
    return out[:6]


def engine_line(windows) -> str:
    """One bounded engine line, as facts, for the record-own-time windows this
    call's queries ran with, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    names = Withheld()
    parts = [f"in {names.show(w.table, 'table')}, {_row_fact(names, w)}" for w in shown]
    return (f"{ENGINE_PREFIX} check, in this turn's queries: "
            f"{'; '.join(parts)}. Each is a window {MARKER}.{names.sentence()}")


def requirement_block(windows) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these windows, as facts,
    ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes = []
    for w in shown:
        population = names.show(w.population_column)
        own = names.show(w.own_column)
        windows_text = _window_text(names, w.windows)
        missing = names.show(w.windows[0]) if len(w.windows) == 1 else "those columns"
        notes.append(f"REQUIRED: the answer must say that its {population} figures count records "
                     f"by {windows_text}, not by the record's own time {own}, so records with "
                     f"no {missing} are not counted.")
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
