# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Each lifecycle state's own time column, and a window on another state's.

Measured on an analytics deployment, in two live dry runs on one build: a
count of failed rows was windowed on the completion time, and most failed
rows have no completion time, so the count came back well under half of the
one the failure time gives; a percentile over completed rows was windowed on
the start time. The system agent's STATE TIME prompt rule and a steward hint
saying the same thing were both ignored, so the engine enforces it.

* :func:`from_tables_desc` reads, from the schema the system agent is baked
  with, a table's STATE columns and each state's own TIME column, only where
  the names say so unambiguously: a date or timestamp column named for a
  state value (``fail_*`` or ``failed_*`` for FAILED, ``complete_*`` for
  COMPLETED, ``start_*`` for STARTED, ``cancel_*`` for CANCELLED; see
  :func:`_forms`), after the state column's own name prefix (``run_complete_*``
  for a ``run_status`` column, so a record's status and a sub-record's status
  each keep their own columns), exactly one per state, for a state column
  whose sample values name at least two such states. Two state columns that
  claim the same time column are both left out. Nothing else is guessed. A
  value the query pins that the samples did not show is mapped by the same
  name rule (:meth:`StateTimes.time_of`).
* :func:`decide` reads one certain single-table query, the SQL that will
  run (after the scan guard adds its own partition predicate). A CTE or a
  derived table that only selects the table's rows as they are and filters
  them, read as the only source of one top-level query block, is the same
  query: its WHERE is read where it is, and the grouping, HAVING and count in
  the block that reads it (:func:`_counting_block`); that block's own WHERE
  is read, ANDed with the inner one, only when it is ``=`` or ``IN`` pins of
  the state column (:func:`_outer_pins`). It is refused when

  - its WHERE pins the state column to values (``=`` or ``IN``, AND), it
    GROUPs BY that column (a top-level HAVING pin of the column, ``=`` or
    ``IN``, narrows the states a grouped query counts), or it counts a state
    inside a conditional aggregate of its SELECT list
    (:func:`_conditional_states`: a searched ``COUNT(CASE WHEN state = 'X'
    THEN 1 END)`` or a simple ``COUNT(CASE state WHEN 'X' THEN 1 END)``,
    ``SUM(CASE ...)``, ``COUNT(...) FILTER (WHERE ...)``, ``COUNT_IF(...)`` and
    the Trino ``IF(...)`` forms, at any depth of the SELECT item so a wrapper
    such as ``COALESCE(...)``, outer parentheses, arithmetic or ``CAST(... AS
    t)`` does not hide one, but not one followed by ``OVER`` or inside a nested
    query block), and its only bounded
    date or timestamp column is the own time column of another state than one
    it counts;
  - or it counts rows (a ``COUNT(...)`` in its SELECT list, not one followed
    by ``OVER``, or a conditional count) of certainly two
    or more states of the state column (the column pinned by ``=`` or ``IN``,
    not named in the WHERE, or counted inside conditional aggregates, any
    HAVING on it read), and its only bounded
    date or timestamp column is one the engine did not identify as the own
    column of any state of the table's state columns: every counted state's
    rows share a time that is not their own. A single state counted on such a
    column is a cohort of that state's rows and runs;
  - or its WHERE has the state column under an OR (and names a time column):
    accepted only when every branch pins its states and bounds no time column
    but each one's own, with no time bound outside the OR; anything else
    under such an OR is not proven.

  **A block's conditional aggregates are decided together, each exactly as
  the filtered COUNT or GROUP BY query it is equivalent to, by construction**
  (:func:`_conditional_states`). Each aggregate is parsed into its counting
  paths (:func:`_agg_paths`, :func:`_case_paths`): each path is the
  conjunction of conditions that reach a contributing result. A branch's
  result contributes unless it is the literal ``NULL`` (every counting
  aggregate) or, for SUM only, the literal ``0`` (:func:`_counts_a_row`), so a
  SUM of a column, a negative or a fraction is read like its WHERE-pinned
  form; a result that is itself a nested CASE/IF contributes its own paths
  ANDed with the branch's (a nested CASE is one unit, matched by a CASE/END
  counter); a branch k's path negates the earlier branches as the CASE
  fallthrough does, ``NOT COALESCE((c_1), FALSE) AND ... AND c_k`` (``c IS NOT
  TRUE``, so a row where ``c`` is UNKNOWN is kept, :func:`_negated`); a simple
  CASE on the state column turns ``WHEN 'X'`` into ``state = 'X'``; a
  contributing ELSE contributes the negation of all conditions. An
  aggregate's ``FILTER (WHERE f)`` is ANDed into every one of the argument's
  counting paths (``f`` and the CASE condition both apply). More than eight
  paths, or nesting deeper than three, leaves the aggregate unread.

  For each path the engine builds ``SELECT COUNT(*) FROM <table> [alias] WHERE
  <block WHERE> AND <path>`` (the relation alias kept so a reader's qualified
  ``b.state`` binds, :func:`_from_text`) and reads it with the same decision
  code: the states the path pins, and any time window written only inside the
  path (not in the outer WHERE), are a decision of the outer query too. A path
  that also bounds or names the pinned state's own time (``fail_time`` in
  range, or ``fail_time IS NOT NULL``), or whose equivalent bounds two time
  columns, drops that state, exactly as its WHERE-pinned form gets no decision.
  An aggregate every one of whose counting paths does not constrain the state
  column counts rows across states (``COUNT(CASE ... THEN 1 ELSE 0 END)`` is a
  plain row count) and is left unread; a non-count aggregate of a CASE (MAX,
  MIN, AVG) contributes no path. A path that carries an OR naming the state
  column is decided whole by building its equivalent and letting
  :func:`_decide_or` read its branches (so branch-specific predicates are
  honoured), not by a separate OR reader.

  The block's paths are grouped by window: the paths on the outer WHERE
  window join the outer decision (:func:`_decide_one`, :func:`_decide_unowned`),
  each window written only inside paths is its own group decided as the IN pin
  of its states on that window, and each OR path is decided whole. Each group
  and each OR path is its own part (:func:`decide_parts`), decided the way an
  IN pin or GROUP BY of the same states on the same window does; the parts are
  never merged, so the same state counted on two windows is two parts.
  Two states counted by paths on a column no state owns are refused once, just
  as ``GROUP BY state`` or that IN pin is (a single state stays a cohort and
  runs). When the WHERE pins the state column or the query groups by it, the
  conditional states add nothing new; they decide only a query that does
  neither. A cohort question written with ``COUNT(*)`` beside a conditional
  count of another state, or two conditional counts one of which counts the
  window's own state, is refused once just as the same question written
  ``GROUP BY`` the state column or as an IN pin of those states already is,
  and pays the same one-refusal cost (words cannot tell a cohort from a
  breakdown; see "a written column name exempts nothing" below).

  Each part is a :class:`StateTimeWindow`; the caller (the system agent's
  guarded Athena tool, which knows the question) refuses each refusing part
  the first time, all in one refusal
  (:class:`StateTimeWindowError`, on the ``StorageBoundError`` path, with a
  remedy of one query per state, each filtering the state column to that one
  state and windowed on its own time column, or, when no counted state has a
  time column the engine identified as its own, to send it again unchanged)
  and lets the same query sent
  again run, disclosed by :func:`engine_line` and :func:`requirement_block`.
  Both state facts only: which states' rows were placed in the window by which
  state's time column, not by their own (or that the engine identified no
  time column of their own), so that rows whose window column is outside the
  window or empty are not counted, and that rows are counted by when that
  other state was reached (``when they reached the STARTED state``; by the
  column when the state value is not printable); on a column
  no state was identified to own, that column, and each counted state's own
  one or that the engine identified none. For a GROUP BY that no pin (WHERE
  or read HAVING) narrows, either also says that the rows of every other
  state value are on that column too. The line opens by saying the question
  did not name the window, or that it named it only by a state word.

Not a window on another state's time: the window is the counted state's own
column; the WHERE
mentions a counted state's own column in any form (plain rule); a state with
no time column of its own; no mapping; more than one table; anything the
reader cannot read with certainty outside an OR on the state column; the
block that reads a wrapper which groups, de-duplicates, renames, joins or
limits its rows, or filters them again other than by pins of the state
column (only the table's own block is read then). On a column no state was
identified to own, also not: a query that
counts no rows (it lists rows or states, or aggregates otherwise); one
counted state; a state column negated or filtered in a form the reader does
not read; a counted state whose own column by name is the window; a window
that is the own time of a record field the WHERE constrains
(``record_time.own_time_column``). When the
question names the window column's state word (``completed`` for a completion
time) in a positive mention, the query is not refused but is still disclosed:
words cannot tell a cohort window from an incidental mention. The exception
is a state breakdown, a query that counts the window's own state beside
another state that has its own time column (``GROUP BY`` the state column, an
``IN`` pin holding both, or a conditional count of the other state beside an
unconditional count of the window's own rows -- the WHERE not pinning the
state column away from the window's state -- or a conditional count of the
window's own state too, as in Q2's ``COUNT(*)`` with
``COUNT(CASE WHEN state = 'FAILED' THEN 1 END)`` on COMPLETED's own column):
the word justifies the window for that state's
rows only, so it is refused once like an unnamed one. A HAVING that names the
state column in a form the reader does not read leaves the groups unknown, so
such a query is not taken as a breakdown. A time the question mentions only
as an end of a duration or difference (``X minus Y``, ``X - Y``, the
difference between X and Y, from X to Y, the arguments of a ``date_diff`` or
``date_add`` style call) names neither the window column nor its state:
:func:`_outside_durations` blanks those mentions before either is read, and a
mention it is unsure of is blanked too, so the cost of a wrong reading is one
refusal and the resend. A mention tied to the window ("whose start time falls
in the window", "started on", "by start date") still names it. A table,
column or state name is printed only when ``engine_names.printable`` passes.
Generic: no column, table or system names. Pure apart from the per-system
registry the system agent fills at construction.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, replace
from functools import partial

from .athena_exact_shape import _column_ref, _split_alias, _split_commas
from .athena_scope import (_RESERVED_AFTER_RELATION, _matching_close, _split_boolean,
                           _strip_outer, _where_expression, relations, tokenize)
from .storage_bound_rule import (StorageBound, StorageBoundError, _temporal, _upper,
                                 bounded_columns, where_names)

ERROR_NAME = "STATE_TIME_WINDOW"
REASON_STATE_TIME = "window_on_other_state_time"
#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] State time windows"
MARKER = "state time, not asked for"
_MIN_FORM = 4
_MAX_WINDOWS = 6
_VALUE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,63}")
_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_GROUP_END = frozenset({"HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT",
                        "FETCH", "WINDOW", "QUALIFY", "SELECT"})

_lock = threading.Lock()
_maps: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# The mapping
# --------------------------------------------------------------------------- #
def _forms(value: str) -> set[str]:
    """The name stems a state value's own time column may start with: the
    value itself and, for a past form, its verb (``failed`` -> ``fail``,
    ``completed`` -> ``complete``, ``cancelled`` -> ``cancel``)."""
    text = str(value or "").strip()
    if not _VALUE_RE.fullmatch(text):
        return set()
    v = text.lower()
    forms = {v}
    if v.endswith("ed"):
        forms |= {v[:-1], v[:-2]}
        if len(v) > 4 and v[-3] == v[-4]:
            forms.add(v[:-3])
    return {f for f in forms if len(f) >= _MIN_FORM}


def _prefix(state_column: str) -> str:
    """The name prefix a state column's own time columns share: everything
    before its last word (``""`` for ``state``, ``run_`` for ``run_status``),
    so a record's status and a sub-record's status each keep their own
    columns."""
    head, sep, _last = str(state_column).lower().rpartition("_")
    return head + sep


def _own_column(value: str, temporal, prefix: str = "") -> str | None:
    """The one date/timestamp column named ``<prefix><form>`` or
    ``<prefix><form>_...`` for a form of ``value``, else ``None``."""
    forms = {prefix + f for f in _forms(value)}
    hits = [c for c in temporal
            if any(c.lower() == f or c.lower().startswith(f + "_") for f in forms)]
    return hits[0] if len(hits) == 1 else None


@dataclass(frozen=True)
class StateTimes:
    """One state column of a table and each state's own time column."""

    table: str
    state_column: str
    times: tuple          # ((value as written, own column), ...)
    temporal: tuple       # every date/timestamp column of the table

    def _claimed(self) -> dict:
        return {column.lower(): value for value, column in self.times}

    def time_of(self, value: str) -> str | None:
        """``value``'s own time column: from the samples, else by the same
        name rule when no other state owns the column it names."""
        for known, column in self.times:
            if known.lower() == str(value).lower():
                return column
        column = _own_column(value, self.temporal, _prefix(self.state_column))
        if column is None or column.lower() in self._claimed():
            return None
        return column

    def state_of(self, column: str) -> str | None:
        """The state whose own time column ``column`` is (samples only)."""
        return self._claimed().get(str(column).lower())


def _fragment(text: str) -> tuple:
    """``(name, type, [samples])`` of one baked column fragment."""
    name, _sep, rest = str(text).partition(":")
    data_type, _sep, samples = rest.partition(" e.g. ")
    values = [s.strip() for s in samples.split(",") if s.strip()] if samples else []
    return name.strip(), data_type.strip(), values


def _state_column(table: str, name: str, samples, temporal) -> StateTimes | None:
    """``name``'s mapping when at least two of its sample values each name
    exactly one time column of their own (no column claimed twice)."""
    found: dict = {}
    for value in samples:
        column = _own_column(value, temporal, _prefix(name))
        if column and value.lower() not in {v.lower() for v in found}:
            found[value] = column
    owners = Counter(c.lower() for c in found.values())
    times = tuple((v, c) for v, c in found.items() if owners[c.lower()] == 1)
    return StateTimes(table, name, times, temporal) if len(times) >= 2 else None


def from_tables_desc(tables_desc: str) -> dict:
    """``{table (lowercased): (StateTimes, ...)}`` from a baked
    ``tables_desc``: one per state column whose time columns are
    unambiguous. Two state columns that claim the same time column are both
    left out."""
    from .system_agent import _desc_entries
    out: dict = {}
    for table, fragments, _marked, _hidden in _desc_entries(tables_desc or ""):
        columns = [_fragment(f) for f in fragments]
        temporal = tuple(n for n, t, _s in columns if _temporal(t) and _COLUMN_RE.fullmatch(n))
        found = [m for name, data_type, samples in columns
                 if samples and not _temporal(data_type) and _COLUMN_RE.fullmatch(name)
                 for m in [_state_column(table, name, samples, temporal)] if m]
        claims = Counter(c.lower() for m in found for _v, c in m.times)
        kept = tuple(m for m in found if all(claims[c.lower()] == 1 for _v, c in m.times))
        key = str(table).rpartition(".")[2].lower()
        if key and kept:
            out[key] = kept
    return out


def remember_tables_desc(system_id: str, tables_desc: str) -> None:
    """Keep ``system_id``'s mappings from its baked ``tables_desc``."""
    if not system_id:
        return
    mapping = from_tables_desc(tables_desc)
    with _lock:
        if mapping:
            _maps[system_id] = mapping
        else:
            _maps.pop(system_id, None)


def remember(system_id: str, mapping: dict) -> None:
    """Keep ``system_id``'s ``{table: (StateTimes, ...)}`` mapping directly
    (table keys lowercased to the terminal name, as :func:`mappings_for`
    reads them). An empty/absent mapping forgets it. The direct seam beside
    :func:`remember_tables_desc`, used where the mapping is built already."""
    if not system_id:
        return
    keyed = {str(t).rpartition(".")[2].lower(): tuple(m)
             for t, m in (mapping or {}).items() if m}
    with _lock:
        if keyed:
            _maps[system_id] = keyed
        else:
            _maps.pop(system_id, None)


def has_mapping(system_id: str) -> bool:
    with _lock:
        return bool(_maps.get(system_id))


def mappings_for(system_id: str, table: str) -> tuple:
    """The table's state-column mappings, ``()`` for none."""
    with _lock:
        return tuple(_maps.get(system_id, {}).get(
            str(table or "").rpartition(".")[2].lower(), ()))


def business_time_columns(mappings) -> tuple:
    """The lowercased BUSINESS time columns of a table's state mappings: the
    own-time column of each identified state (completion/failure/start/...).
    These are the times the question's state basis is counted on, as opposed to
    a storage/write column. Sorted, de-duplicated, ``()`` for no mapping. Used
    by the window-on-storage refusal to name a column the agent should filter
    on instead of the write column."""
    out: set[str] = set()
    for mapping in mappings or ():
        for _value, column in getattr(mapping, "times", ()):
            if column:
                out.add(str(column).lower())
    return tuple(sorted(out))


def reset() -> None:
    """Forget every system's mapping (tests)."""
    with _lock:
        _maps.clear()


# --------------------------------------------------------------------------- #
# Reading the query
# --------------------------------------------------------------------------- #
def _string(tokens) -> str | None:
    tokens = _strip_outer(tokens)
    if len(tokens) == 1 and tokens[0].kind == "literal":
        text = tokens[0].text
        if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
            return text[1:-1].replace("''", "'")
    return None


def _pin(part) -> tuple | None:
    """``(column, {values})`` when one conjunct pins a column to string
    literals by ``=`` or ``IN``, else ``None``."""
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    if any(word == "NOT" for _i, word in words):
        return None
    for i, word in words:
        if word == "IN":
            column = _column_ref(part[:i])
            values = _strip_outer(part[i + 1:])
            if column is None or i + 1 >= len(part) or part[i + 1].text != "(" or not values:
                return None
            got = [_string(item) for item in _split_commas(values)]
            return None if any(g is None for g in got) else (column.lower(), set(got))
    for i, token in enumerate(part):
        if token.depth == base and token.kind == "operator":
            if token.text != "=":
                return None
            for side, other in ((part[:i], part[i + 1:]), (part[i + 1:], part[:i])):
                column, value = _column_ref(side), _string(other)
                if column and value is not None:
                    return column.lower(), {value}
            return None
    return None


def _pinned(where, column: str) -> set | None:
    """The values the top-level AND of ``where`` pins ``column`` to, else ``None``."""
    values = None
    for part in (_split_boolean(where, "AND") if where else []):
        part = _strip_outer(part)
        if not part or len(_split_boolean(part, "OR")) > 1:
            continue
        pin = _pin(part)
        if pin and pin[0] == column:
            values = pin[1] if values is None else values & pin[1]
    return values


def _clause_end(tokens, start: int, depth: int) -> int:
    """The index where a clause starting at ``start`` of a query block at
    ``depth`` ends: the next clause keyword or semicolon of that block, or
    the block's end."""
    end = start
    while end < len(tokens) and tokens[end].depth >= depth and not (
            tokens[end].depth == depth and (_upper(tokens[end]) in _GROUP_END
                                            or tokens[end].kind == "semicolon")):
        end += 1
    return end


def _group_by_span(tokens, relation) -> tuple | None:
    """``(start, end, from_at)`` of the relation's own query block's GROUP BY
    list (``from_at`` is its FROM), else ``None``."""
    depth, start, from_at = relation.depth, None, None
    for i in range(relation.select_index + 1, len(tokens) - 1):
        token = tokens[i]
        if token.depth < depth or (token.depth == depth and _upper(token) == "SELECT"):
            break
        if token.depth == depth and _upper(token) == "FROM" and from_at is None:
            from_at = i
        if token.depth == depth and _upper(token) == "GROUP" and _upper(tokens[i + 1]) == "BY":
            start = i + 2
            break
    if start is None or from_at is None:
        return None
    return start, _clause_end(tokens, start, depth), from_at


def _grouped(tokens, relation) -> set:
    """The columns the relation's own query block GROUPs BY (a name or the
    ordinal of a selected column); anything else is not read."""
    span = _group_by_span(tokens, relation)
    if span is None:
        return set()
    start, end, from_at = span
    selected = _split_commas(tokens[relation.select_index + 1:from_at])
    out = set()
    for item in _split_commas(tokens[start:end]):
        item = _strip_outer(item)
        if len(item) == 1 and item[0].kind == "number" and item[0].text.isdigit():
            n = int(item[0].text) - 1
            item = _split_alias(selected[n])[0] if 0 <= n < len(selected) else []
        column = _column_ref(item) if item else None
        if column:
            out.add(column.lower())
    return out


def _having(tokens, relation) -> list:
    """The HAVING expression right after the relation's own GROUP BY, ``[]``
    for none."""
    span = _group_by_span(tokens, relation)
    if span is None:
        return []
    at, depth = span[1], relation.depth
    if at >= len(tokens) or tokens[at].depth != depth or _upper(tokens[at]) != "HAVING":
        return []
    return tokens[at + 1:_clause_end(tokens, at + 1, depth)]


def _having_states(having, column: str) -> tuple:
    """``(values, read)``: the values the top-level AND of a HAVING pins
    ``column`` to (``=`` or ``IN``; ``None`` for no pin), and ``read`` False
    when it names ``column`` in any other form (under OR or NOT, computed,
    compared other than by ``=``/``IN``), so which groups it keeps is not
    known."""
    values = None
    for part in _conjuncts(having):
        if column not in _names_in(part):
            continue
        pin = _pin(part) if len(_split_boolean(part, "OR")) == 1 else None
        if not pin or pin[0] != column:
            return None, False
        values = pin[1] if values is None else values & pin[1]
    return values, True


#: Words that make a query block more than a selection and filter of its
#: table's rows (read at the block's own depth).
_SHAPING = frozenset({"GROUP", "HAVING", "DISTINCT", "JOIN", "UNION", "INTERSECT", "EXCEPT",
                      "ORDER", "LIMIT", "OFFSET", "FETCH", "WINDOW", "QUALIFY", "TABLESAMPLE"})
#: The clauses the block that reads a wrapper may have after its FROM.
_READER_TAIL = frozenset({"GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH"})
#: Words that end a reader's tail being read: another source, a filter, a
#: set operation or a nested query block.
_NOT_A_TAIL = frozenset({"SELECT", "WHERE", "JOIN", "UNION", "INTERSECT", "EXCEPT"})


def _from_at(tokens, select_at: int, depth: int) -> int | None:
    """The FROM of the query block whose SELECT is at ``select_at``, else
    ``None``."""
    for i in range(select_at + 1, len(tokens)):
        if tokens[i].depth < depth:
            return None
        if tokens[i].depth == depth and _upper(tokens[i]) == "FROM":
            return i
    return None


def _select_items(tokens, select_at: int, depth: int) -> list | None:
    """The SELECT list items of the query block whose SELECT is at
    ``select_at``, ``None`` when the block has no FROM at its depth."""
    at = _from_at(tokens, select_at, depth)
    return None if at is None else _split_commas(tokens[select_at + 1:at])


def _plain_projection(items) -> bool:
    """True for ``*``, ``<name>.*`` or a list of column references each kept
    under its own name: the rows selected as they are."""
    if not items or any(not item for item in items):
        return False
    texts = [t.text for t in items[0]]
    if len(items) == 1 and (texts == ["*"] or (len(texts) == 3 and texts[1:] == [".", "*"])):
        return True
    for item in items:
        expression, alias = _split_alias(item)
        column = _column_ref(expression)
        if column is None or (alias is not None and alias.lower() != column.lower()):
            return False
    return True


def _filters_only(tokens, select_at: int, close_at: int, depth: int) -> bool:
    """True when the block from ``select_at`` to ``close_at`` selects its one
    table's rows as they are (:func:`_plain_projection`) and at most filters
    them: no other query block, grouping, de-duplication, join, set
    operation, ordering or row limit."""
    block = tokens[select_at:close_at]
    if any(_upper(t) == "SELECT" for t in block[1:]):
        return False
    if any(t.depth == depth and _upper(t) in _SHAPING for t in block):
        return False
    return _plain_projection(_select_items(tokens, select_at, depth))


def _reads_only(tokens, at: int) -> bool:
    """True when what follows a wrapper in the top-level FROM that reads it,
    from ``at``, is an optional ``[AS] alias`` (no column list), then an
    optional WHERE with no other query block in it (whether it is read is
    decided by :func:`_outer_pins`), and then nothing or only GROUP BY,
    HAVING, ORDER BY and a row limit: no other source, no other query
    block."""
    if at < len(tokens) and _upper(tokens[at]) == "AS":
        at += 1
        if at >= len(tokens) or tokens[at].kind not in {"word", "identifier"}:
            return False
        at += 1
    elif (at < len(tokens) and tokens[at].depth == 0
          and tokens[at].kind in {"word", "identifier"}
          and _upper(tokens[at]) not in _RESERVED_AFTER_RELATION):
        at += 1
    rest = [t for t in tokens[at:] if t.kind != "semicolon"]
    if rest and rest[0].depth == 0 and _upper(rest[0]) == "WHERE":
        end = next((i for i, t in enumerate(rest)
                    if i and t.depth == 0 and _upper(t) in _READER_TAIL), len(rest))
        if end == 1 or any(_upper(t) in _NOT_A_TAIL for t in rest[1:end]):
            return False
        rest = rest[end:]
    if rest and (rest[0].depth != 0 or _upper(rest[0]) not in _READER_TAIL):
        return False
    return not any(_upper(t) in _NOT_A_TAIL for t in rest)


def _same_name(left, right) -> bool:
    """True when two name tokens name the same relation: case-folded when
    both are plain words, exactly when either is quoted."""
    if left.kind == "word" and right.kind == "word":
        return str(left.value).lower() == str(right.value).lower()
    return left.kind == right.kind == "identifier" and left.value == right.value


def _reader_of(tokens, open_at: int, close_at: int) -> int | None:
    """The SELECT index of the one top-level query block that reads the
    parenthesised block ``open_at``..``close_at`` as its only source: the
    one CTE of ``WITH w AS (...) SELECT ... FROM w`` (no column list) or the
    derived table of ``SELECT ... FROM (...) [AS] w``; else ``None``."""
    if tokens[open_at].depth != 0:
        return None
    if (open_at == 3 and _upper(tokens[0]) == "WITH" and _upper(tokens[2]) == "AS"
            and tokens[1].kind in {"word", "identifier"}):
        reader = close_at + 1
        if reader >= len(tokens) or _upper(tokens[reader]) != "SELECT":
            return None
        from_at = _from_at(tokens, reader, 0)
        if (from_at is None or from_at + 1 >= len(tokens)
                or not _same_name(tokens[1], tokens[from_at + 1])
                or any(_upper(t) == "SELECT" for t in tokens[reader + 1:from_at])):
            return None
        return reader if _reads_only(tokens, from_at + 2) else None
    if (open_at >= 2 and _upper(tokens[0]) == "SELECT" and _from_at(tokens, 0, 0) == open_at - 1
            and not any(_upper(t) == "SELECT" for t in tokens[1:open_at - 1])):
        return 0 if _reads_only(tokens, close_at + 1) else None
    return None


def _counting_block(tokens, scan) -> tuple:
    """``(block, outer WHERE)``: the query block that counts ``scan``'s rows
    and the WHERE of that block when it is not the scan's own (``[]`` for
    none). The block is the scan's own, or, when that block only selects and
    filters the table (:func:`_filters_only`) inside one CTE or derived table
    that one top-level block reads as its only source (:func:`_reader_of`),
    that reader. Grouping, HAVING and the count are read in it; the scan's
    own WHERE is read where it is, and the reader's only through
    :func:`_outer_pins`."""
    open_at = scan.select_index - 1
    if scan.depth == 0 or open_at < 0 or tokens[open_at].text != "(":
        return scan, []
    close_at = _matching_close(tokens, open_at)
    if close_at is None or not _filters_only(tokens, scan.select_index, close_at, scan.depth):
        return scan, []
    reader = _reader_of(tokens, open_at, close_at)
    if reader is None:
        return scan, []
    block = replace(scan, depth=tokens[reader].depth, select_index=reader)
    return block, [t for t in _where_expression(tokens, block) if t.kind != "semicolon"]


def _outer_pins(sql: str, where, outer, state_columns) -> list | None:
    """The scan's WHERE ``where`` ANDed with the reader's WHERE ``outer``,
    as one token list, when every AND conjunct of ``outer`` that names one
    of ``state_columns`` pins it by ``=`` or ``IN`` (:func:`_pin`); a
    conjunct naming no state column only narrows the rows (the wrapper keeps
    every column under its own name, :func:`_plain_projection`). Else
    ``None`` (the reader's WHERE is not read, so neither is the reader)."""
    parts = _conjuncts(outer)
    if not parts:
        return None
    for part in parts:
        if not (_names_in(part) & set(state_columns)):
            continue
        pin = _pin(part) if len(_split_boolean(part, "OR")) == 1 else None
        if pin is None or pin[0] not in state_columns:
            return None
    pieces = []
    if where:
        text = sql[where[0].start:where[-1].end]
        pieces.append(f"({text})" if len(_split_boolean(where, "OR")) > 1 else text)
    for part in parts:
        text = sql[part[0].start:part[-1].end]
        pieces.append(f"({text})" if len(_split_boolean(part, "OR")) > 1 else text)
    combined, uncertain = tokenize(" AND ".join(pieces))
    return None if uncertain or not combined else combined


def _counts_rows(tokens, block) -> bool:
    """True when a SELECT item of ``block`` holds a ``COUNT(...)`` call
    (``COUNT(*)``, ``COUNT(column)``, ``COUNT(DISTINCT ...)``, alone or in an
    expression) outside any nested query block. A ``COUNT(...) OVER (...)``,
    with or without an aggregate ``FILTER (...)`` before ``OVER``, is a
    window over the result rows, not a count of the table's rows."""
    for item in _select_items(tokens, block.select_index, block.depth) or ():
        if any(_upper(t) == "SELECT" for t in item):
            continue
        for i, t in enumerate(item):
            if not (t.kind == "word" and _upper(t) == "COUNT" and i + 1 < len(item)
                    and item[i + 1].text == "("):
                continue
            close = _matching_close(item, i + 1)
            # An aggregate FILTER (...) may stand between the call and OVER.
            if (close is not None and close + 2 < len(item)
                    and _upper(item[close + 1]) == "FILTER" and item[close + 2].text == "("):
                close = _matching_close(item, close + 2)
            if close is not None and close + 1 < len(item) and _upper(item[close + 1]) == "OVER":
                continue
            return True
    return False


# --------------------------------------------------------------------------- #
# A conditional aggregate decides as the filtered COUNT(*) query it equals
# --------------------------------------------------------------------------- #
#: Count-style aggregates whose conditional form counts rows of a state. A
#: non-count aggregate of a CASE (MAX, MIN, AVG) is excluded by its absence
#: here, so a state inside it is never read.
_COUNTING_AGGREGATES = frozenset({"COUNT", "SUM", "COUNT_IF"})
#: A conditional aggregate with more counting paths than this, or nested
#: deeper than :data:`_NEST_CAP`, is left unread (no guess).
_PATH_CAP = 8
_NEST_CAP = 3


def _counts_a_row(result, func: str) -> bool:
    """Whether a CASE/IF branch's THEN (or an ELSE) contributes to the
    aggregate ``func`` of one state's rows: it contributes unless it is the
    literal ``NULL`` (for every counting aggregate), or, for SUM only, the
    literal ``0`` (a zero adds nothing to a sum). Any other value contributes
    -- for COUNT a non-NULL literal or column, for SUM a column, a negative or
    a fraction -- so a SUM decides like its WHERE-pinned equivalent rather
    than only for a positive literal. ``THEN 1 ELSE 0`` under COUNT therefore
    has a contributing ELSE (COUNT counts the 0s) and is left unread; a SUM
    ``ELSE 1`` likewise contributes."""
    result = _strip_outer(result)
    if not result:
        return False
    if len(result) == 1 and _upper(result[0]) == "NULL":
        return False
    if func == "SUM" and len(result) == 1 and result[0].kind == "number":
        try:
            return float(result[0].text) != 0
        except ValueError:
            return True
    return True


def _text(tokens) -> str:
    """The SQL text of a token list, rebuilt from the tokens' own text joined
    by single spaces (whitespace-insensitive, so it re-tokenizes the same)."""
    return " ".join(t.text for t in tokens)


def _is_conditional(result):
    """The inner CASE or IF token list when a branch result is itself a
    conditional, else ``None``."""
    result = _strip_outer(result)
    if result and _upper(result[0]) == "CASE":
        return result
    if (result and _upper(result[0]) == "IF" and len(result) > 1 and result[1].text == "("
            and _matching_close(result, 1) == len(result) - 1):
        return result
    return None


def _case_markers(body, base) -> list | None:
    """Indices of the top-level ``WHEN`` / ``THEN`` / ``ELSE`` of a CASE body,
    skipping any nested ``CASE ... END`` (tracked by a CASE/END counter), else
    ``None`` when the nesting is unbalanced."""
    marks, nest = [], 0
    for i, t in enumerate(body):
        word = _upper(t)
        if word == "CASE":
            nest += 1
        elif word == "END":
            nest -= 1
            if nest < 0:
                return None
        elif nest == 0 and t.depth == base and word in {"WHEN", "THEN", "ELSE"}:
            marks.append(i)
    return marks if nest == 0 else None


def _case_paths(inner, func: str, column: str, depth: int) -> list | None:
    """The counting paths of a ``CASE`` expression as condition token-lists,
    else ``None`` (not read). A searched ``CASE WHEN c THEN x ...`` gives each
    contributing branch the path ``NOT(earlier conds) AND c``; a simple
    ``CASE <column> WHEN 'X' THEN x ...`` on exactly ``column`` turns each
    ``WHEN 'X'`` into ``column = 'X'``. A THEN that is itself a CASE/IF
    contributes its own paths, each ANDed with the branch path (recursion). A
    contributing ELSE contributes ``NOT(all conds)``. Capped by
    :data:`_NEST_CAP` and :data:`_PATH_CAP`."""
    if depth > _NEST_CAP or not inner or _upper(inner[0]) != "CASE":
        return None
    base = inner[0].depth
    if not (inner[-1].depth == base and _upper(inner[-1]) == "END"):
        return None
    body = inner[1:-1]
    marks = _case_markers(body, base)
    if not marks or _upper(body[marks[0]]) != "WHEN":
        return None
    operand = body[:marks[0]]
    simple_col = _column_ref(operand) if operand else None
    if operand and (simple_col is None or simple_col.lower() != column):
        return None
    segments = [(body[marks[k]], body[marks[k] + 1:marks[k + 1] if k + 1 < len(marks)
                                       else len(body)]) for k in range(len(marks))]
    paths: list = []
    conds: list = []            # each branch's condition tokens, for the ELSE path
    pending = None
    for head, payload in segments:
        kind = _upper(head)
        if kind == "WHEN":
            if operand:
                value = _string(payload)
                if value is None:
                    return None     # a simple CASE WHEN value that is not a literal
                pending = tokenize(f"{_text(operand)} = '{value}'")[0]
            else:
                pending = payload
        elif kind == "THEN":
            if pending is None:
                return None
            sub = _branch_paths(payload, func, column, pending, list(conds), depth)
            if sub is None:
                return None
            paths.extend(sub)
            conds.append(pending)
            pending = None
        else:  # ELSE
            sub = _branch_paths(payload, func, column, None, list(conds), depth)
            if sub is None:
                return None
            paths.extend(sub)
        if len(paths) > _PATH_CAP:
            return None
    return paths


def _branch_paths(result, func, column, cond, earlier, depth) -> list | None:
    """The paths one branch contributes. ``cond`` is the branch's own
    condition (``None`` for an ELSE); ``earlier`` the earlier branches'
    conditions to negate. A result that does not contribute gives no path; a
    conditional result recurses; any other contributing result gives one path
    (``NOT(earlier) AND cond``)."""
    prefix = [_negated(e) for e in earlier]
    if cond is not None:
        prefix = prefix + [cond]
    nested = _is_conditional(result)
    if nested is not None:
        inner_paths = (_case_paths(nested, func, column, depth + 1)
                       if _upper(nested[0]) == "CASE"
                       else _if_paths(nested, func, column, depth + 1))
        if inner_paths is None:
            return None
        return [prefix + inner for inner in inner_paths]
    if not _counts_a_row(result, func):
        return []
    return [prefix]


def _negated(cond) -> list:
    """``NOT COALESCE((<cond>), FALSE)`` as tokens, for an earlier branch
    excluded from a later branch's path. A CASE proceeds to a later branch
    when an earlier WHEN is FALSE or UNKNOWN, so the exact exclusion is
    ``<cond> IS NOT TRUE``; in Trino that is ``NOT COALESCE(<cond>, FALSE)``.
    A plain ``NOT (<cond>)`` would drop rows where ``<cond>`` is UNKNOWN (a
    NULL input), which CASE keeps, so the built equivalent would exclude rows
    the aggregate counts."""
    return tokenize(f"NOT COALESCE(({_text(cond)}), FALSE)")[0]


def _if_paths(inner, func: str, column: str, depth: int) -> list | None:
    """The counting paths of a Trino ``IF(c, then [, else])``: ``then`` under
    ``c``, ``else`` under ``NOT c`` (as the equivalent CASE)."""
    if depth > _NEST_CAP or not inner or _upper(inner[0]) != "IF" \
            or len(inner) < 2 or inner[1].text != "(":
        return None
    if _matching_close(inner, 1) != len(inner) - 1:
        return None
    args = _split_commas(inner[2:-1])
    if len(args) not in (2, 3):
        return None
    cond = args[0]
    # An unread THEN leaves the whole IF unread: its rows cannot be placed by
    # state, so the ELSE alone must not make the aggregate read as one state's.
    paths = _branch_paths(args[1], func, column, cond, [], depth)
    if paths is None:
        return None
    if len(args) == 3:
        els = _branch_paths(args[2], func, column, None, [cond], depth)
        if els is None:
            return None
        paths = paths + els
    return paths if len(paths) <= _PATH_CAP else None


def _followed_by_over(item, close: int) -> bool:
    """True when the token after the aggregate's close (skipping one
    ``FILTER (...)``) is ``OVER``: a window over the result rows, not a count
    of the table's rows, as :func:`_counts_rows` also skips."""
    at = close + 1
    if at < len(item) and _upper(item[at]) == "FILTER" and at + 1 < len(item) \
            and item[at + 1].text == "(":
        fclose = _matching_close(item, at + 1)
        if fclose is not None:
            at = fclose + 1
    return at < len(item) and _upper(item[at]) == "OVER"


def _agg_paths(item, column: str) -> list:
    """One entry per readable conditional aggregate in a SELECT item: the list
    of that aggregate's counting paths (each a list of condition token lists
    the aggregate adds to the block's WHERE to count a state's rows).
    Aggregates are read at ANY depth of the item (a wrapper such as
    ``COALESCE(...)``, parentheses, arithmetic or ``CAST(... AS t)`` does not
    hide one); an aggregate followed by ``OVER``, a non-count aggregate of a
    CASE (MAX, MIN, AVG) and anything the reader cannot read contribute no
    entry (never a guess)."""
    out: list = []
    for i, t in enumerate(item):
        if t.kind != "word" or i + 1 >= len(item) or item[i + 1].text != "(":
            continue
        entry = _agg_entry(item, i, _upper(t), column)
        if entry is not None:
            out.append(entry)
    return out


#: :func:`_filter_condition` found a FILTER it could not read.
_FILTER_UNREAD = object()


def _filter_condition(item, close: int):
    """The condition of a ``FILTER (WHERE ...)`` after the aggregate whose
    argument closes at ``close``: its tokens, ``None`` when there is no
    FILTER, :data:`_FILTER_UNREAD` when there is one it cannot read."""
    if not (close + 2 < len(item) and _upper(item[close + 1]) == "FILTER"
            and item[close + 2].text == "("):
        return None
    fclose = _matching_close(item, close + 2)
    if fclose is None:
        return _FILTER_UNREAD
    where = item[close + 3:fclose]
    if not (where and _upper(where[0]) == "WHERE"):
        return _FILTER_UNREAD
    return where[1:]


def _agg_entry(item, i: int, func: str, column: str):
    """The counting paths of the aggregate named ``func`` at ``item[i]``,
    whose ``(`` is ``item[i + 1]`` (one entry of :func:`_agg_paths`), else
    ``None`` when it is not a readable conditional count."""
    if func != "COUNT_IF" and func not in _COUNTING_AGGREGATES:
        return None
    close = _matching_close(item, i + 1)
    if close is None or _followed_by_over(item, close):
        return None
    if func == "COUNT_IF":
        return [[item[i + 2:close]]]
    filter_cond = _filter_condition(item, close)
    if filter_cond is _FILTER_UNREAD:
        return None
    inner = item[i + 2:close]
    if inner and _upper(inner[0]) == "DISTINCT":
        inner = inner[1:]
    arg_paths = (_case_paths(inner, func, column, 1) if inner and _upper(inner[0]) == "CASE"
                 else _if_paths(inner, func, column, 1) if inner and _upper(inner[0]) == "IF"
                 else None)
    if filter_cond is not None:
        # SQL applies the FILTER predicate AND the aggregate argument's
        # own condition. A plain argument (COUNT(*), COUNT(col), SUM(col),
        # no CASE/IF) contributes only the FILTER; a CASE/IF argument
        # contributes each of its paths ANDed with the FILTER.
        if arg_paths is None:
            if inner and any(_upper(t) in {"CASE", "IF"} for t in inner):
                return None  # an unread CASE/IF argument leaves it unread
            return [[filter_cond]]
        if len(arg_paths) <= _PATH_CAP:
            return [[filter_cond] + path for path in arg_paths]
        return None
    return arg_paths if arg_paths else None


def _path_constrains_state(path, column: str) -> bool:
    """True when a counting path constrains the state column so the aggregate
    counts rows of specific states (not across all of them): its top-level AND
    holds a ``=`` / ``IN`` pin of ``column`` (:func:`_pinned`), or a conjunct
    that names ``column`` is an OR every branch of which pins ``column`` (with
    or without extra branch-specific predicates, so the branches read by the
    built equivalent's :func:`_decide_or` are accepted). A path that leaves the
    state column free counts rows across states, so the whole aggregate is not
    a per-state count and is left unread."""
    text = " AND ".join(_and_clause(_text(p)) for p in path if p)
    toks, uncertain = tokenize(text)
    if uncertain or not toks:
        return False
    if _pinned(toks, column) is not None:
        return True
    for part in _conjuncts(toks):
        if column not in _names_in(part):
            continue
        branches = _split_boolean(part, "OR")
        # Every OR branch must pin the state column somewhere in its own
        # conjuncts; extra branch-specific predicates are kept (the equivalent
        # carries the whole OR and _decide_or reads it).
        if len(branches) > 1 and all(_branch_pins(b, column) for b in branches):
            return True
    return False


def _branch_pins(branch, column: str) -> bool:
    """True when one OR branch pins the state column by ``=`` / ``IN`` in its
    top-level AND conjuncts (ignoring any extra branch-specific predicates)."""
    for conj in _conjuncts(_strip_outer(branch)):
        if len(_split_boolean(conj, "OR")) > 1:
            continue
        pin = _pin(conj)
        if pin and pin[0] == column:
            return True
    return False


@dataclass(frozen=True)
class _BlockConditional:
    """What a block's conditional aggregates count, read together so the block
    is decided across every window at once.

    ``outer_states`` are the states counted by paths whose only window is the
    block's outer WHERE window (decided by the plain rule with the block's own
    bounded map, so Q2's ``COUNT(*)`` plus conditional-count breakdown is
    unchanged). ``path_groups`` maps a window column written only inside the
    paths to ``(its bound atoms, the states counted on it)``; each is decided
    as the IN pin of those states on that window. ``or_equivalents`` are the
    built ``SELECT COUNT(*)`` equivalents of every path that carries an OR on
    the state column, each decided whole by :func:`_decide_or`. Each group
    and OR path is its own part (:func:`_decide_block_parts`); none is
    returned before the rest of the block is read."""

    outer_states: frozenset = frozenset()
    path_groups: tuple = ()        # ((column, (atoms,), (states,)), ...)
    or_equivalents: tuple = ()     # (equivalent sql, ...)


def _path_windows(path, where_text, from_text, table, column_types, mappings, column):
    """``(pinned states, window column or None, its bound atoms)`` for one
    non-OR counting path, read from its built equivalent ``SELECT COUNT(*)
    FROM <from> WHERE (<block WHERE>) AND (<path>)``.

    The window column is the single temporal column the equivalent bounds when
    there is exactly one; a path bounding two or more time columns (its own
    time beside the outer window, the intentional intersection) keeps today's
    reading: its own equivalent gets no decision, so it contributes no group.
    Unlike a per-aggregate reading, a path that counts the window state on its
    OWN column is NOT dropped here: grouping by window and deciding the group
    as an IN pin lets the plain rule place the window state in the breakdown.
    ``(set(), None, ())`` when the path contributes no window group; ``None``
    when the equivalent cannot be read."""
    clauses = [where_text] + [_text(p) for p in path if p]
    where_sql = " AND ".join(_and_clause(c) for c in clauses if c)
    path_sql = " AND ".join(_and_clause(_text(p)) for p in path if p)
    if not where_sql:
        return None
    equivalent = f"SELECT COUNT(*) FROM {from_text} WHERE {where_sql}"  # nosec B608 - analysis text only: it is tokenized, never executed (this module has no database access)
    eq_tokens, uncertain = tokenize(equivalent)
    if uncertain or not eq_tokens:
        return None
    # Decide the whole equivalent too, so the recursion guard and the built
    # text (earlier-branch negation, relation alias) are exercised exactly as
    # a real query's; the states and windows are read from the same string.
    _decide_equivalent(equivalent, table, column_types, mappings)
    read = bounded_columns(equivalent, include_same_column_or=True)
    bmap = read[1] if read else {}
    temporal = {str(c).lower() for c, t in (column_types or {}).items() if _temporal(t)}
    path_tokens, path_unc = tokenize(path_sql) if path_sql else ([], False)
    pinned = _pinned(path_tokens, column) if path_tokens and not path_unc else None
    if not pinned:
        return set(), None, ()
    eq_bounds = {c for c in bmap if c in temporal}
    if len(eq_bounds) != 1:
        # No window, or two windows (own time beside the outer window): the
        # path's own equivalent already decided it (no decision), so it joins
        # no group.
        return set(), None, ()
    window = next(iter(eq_bounds))
    if _cohort_on_own_time(pinned, window, path_sql, from_text, temporal, mappings, column):
        return set(), None, ()
    return {str(v) for v in pinned}, window, tuple(bmap[window])


def _cohort_on_own_time(pinned, window, path_sql, from_text, temporal, mappings,
                        column) -> bool:
    """True when a path names or bounds a pinned state's OWN time column and
    that own column is not ``window`` itself: a correct cohort on its own time
    (its WHERE-pinned equivalent gets no decision), so it joins no group. When
    the own column IS the window (the window state counted on its own column),
    the state is kept so the group's IN pin reads the breakdown."""
    mapping = next((m for m in mappings if m.state_column.lower() == column), None)
    own_columns = ({str(mapping.time_of(v) or "").lower() for v in pinned} - {""}
                   if mapping else set())
    path_named = where_names(
        f"SELECT 1 FROM {from_text} WHERE {path_sql}"  # nosec B608 - analysis text only, never executed
    ) or set() \
        if path_sql else set()
    path_bounds = set()
    if path_sql:
        path_read = bounded_columns(f"SELECT 1 FROM {from_text} WHERE {path_sql}",  # nosec B608 - analysis text only, never executed
                                    include_same_column_or=True)
        path_bounds = {c for c in (path_read[1] if path_read else {}) if c in temporal}
    return bool((own_columns & (path_named | path_bounds)) - {window})


def _path_is_or(path, column: str) -> bool:
    """True when a path carries an OR that names the state column: it is read
    by building its whole equivalent and letting :func:`_decide_or` decide."""
    text = " AND ".join(_and_clause(_text(p)) for p in path if p)
    toks, uncertain = tokenize(text)
    if uncertain or not toks:
        return False
    return any(len(_split_boolean(part, "OR")) > 1 and column in _names_in(part)
               for part in _conjuncts(toks))


def _conditional_states(tokens, block, mapping, sql, table, column_types, where, mappings,
                        question=""):
    """Partition ``block``'s conditional aggregates into window groups and OR
    equivalents (:class:`_BlockConditional`), so the core can decide each as
    its filtered or IN-pin equivalent, each its own part.

    Each aggregate is parsed into its counting paths (:func:`_agg_paths`). An
    aggregate every one of whose paths does not constrain the state column
    counts rows across states and is left unread. A path with an OR on the
    state column becomes an OR equivalent (decided whole by
    :func:`_decide_or`). Every other path contributes the states it pins to
    the group of the single window its equivalent bounds: the block's outer
    WHERE window (``outer_states``, decided by the plain rule) or a window
    written only inside the path (``path_groups``, decided as an IN pin). A
    path bounding two time columns contributes no group (its own equivalent
    has no decision, exactly as the WHERE-pinned form does)."""
    where_text = _text(where) if where else ""
    from_text = _from_text(tokens, block)
    if from_text is None:
        return _BlockConditional()
    column = mapping.state_column.lower()
    temporal = {str(c).lower() for c, t in (column_types or {}).items() if _temporal(t)}
    outer_window = _outer_window(from_text, where_text, temporal)
    outer_states: set = set()
    groups: dict = {}              # window column -> [atoms, set(states)]
    or_equivalents: list = []
    for path in _per_state_paths(tokens, block, column):
        if _path_is_or(path, column):
            equivalent = _or_equivalent(path, where_text, from_text)
            if equivalent:
                or_equivalents.append(equivalent)
            continue
        reading = _path_windows(path, where_text, from_text, table, column_types,
                                mappings, column)
        if reading is None:
            continue
        states, window, atoms = reading
        if not states or window is None:
            continue
        if outer_window is not None and window == outer_window:
            outer_states |= states
        else:
            slot = groups.setdefault(window, [atoms, set()])
            slot[1] |= states
    path_groups = tuple((w, atoms, tuple(sorted(st))) for w, (atoms, st) in groups.items())
    return _BlockConditional(frozenset(outer_states), path_groups, tuple(or_equivalents))


def _per_state_paths(tokens, block, column: str):
    """Every counting path of ``block``'s per-state conditional aggregates, in
    SELECT order. An item holding a nested SELECT is skipped, and so is an
    aggregate any of whose paths does not constrain the state column (it
    counts rows across states, not a per-state count)."""
    for item in _select_items(tokens, block.select_index, block.depth) or ():
        if any(_upper(t) == "SELECT" for t in item):
            continue
        for aggregate in _agg_paths(item, column):
            if all(_path_constrains_state(path, column) for path in aggregate):
                yield from aggregate


def _outer_window(from_text: str, where_text: str, temporal: set) -> str | None:
    """The block's outer WHERE window: the one time column its WHERE bounds,
    else ``None`` (none, or several)."""
    outer_bounds = (bounded_columns(f"SELECT 1 FROM {from_text} WHERE {where_text}",  # nosec B608 - analysis text only, never executed
                                    include_same_column_or=True)[1] if where_text else {})
    return next((c for c in outer_bounds if c in temporal), None) \
        if len([c for c in outer_bounds if c in temporal]) == 1 else None


def _or_equivalent(path, where_text: str, from_text: str) -> str | None:
    """The built ``SELECT COUNT(*)`` equivalent of an OR path (the block's
    WHERE AND the path's conditions), decided whole by :func:`_decide_or`;
    ``None`` when there is no condition at all."""
    clauses = [where_text] + [_text(p) for p in path if p]
    where_sql = " AND ".join(_and_clause(c) for c in clauses if c)
    return f"SELECT COUNT(*) FROM {from_text} WHERE {where_sql}" if where_sql else None  # nosec B608 - analysis text only, never executed


def _and_clause(text: str) -> str:
    """One AND clause of a built WHERE: parenthesised only when it holds a
    top-level OR, so its precedence is kept while a bare AND chain stays
    flat (every bound atom at the top level for ``bounded_columns``)."""
    toks, uncertain = tokenize(text)
    if uncertain or not toks:
        return f"({text})"
    return f"({text})" if len(_split_boolean(toks, "OR")) > 1 else text


def _from_text(tokens, block) -> str | None:
    """The scanned table as a reference for the equivalent query: the terminal
    base relation, quoted when the SQL quoted it, with the reader's alias kept
    (``events b``) when ``block`` reads the table through a CTE or derived
    table it bound to that alias. The equivalent is a single-table
    ``COUNT(*)`` whose WHERE already carries the block's filters and may
    qualify the state column with that alias (``b.state``), so the alias must
    bind in the FROM for the text to be valid SQL."""
    scans, uncertain = relations(tokens)
    if uncertain or not scans:
        return None
    rel = next((s for s in scans if s.depth == block.depth), None)
    base = rel or scans[0]
    parts = base.parts
    if not parts:
        return None
    name = ".".join(f'"{p}"' if base.quoted else str(p) for p in parts)
    # The alias the state column is qualified by: the base relation's own
    # alias when the block is the table's own block, else the alias the reader
    # bound its CTE / derived source to (:func:`_reader_source_alias`).
    alias = str(getattr(base, "alias", "") or "") if rel else ""
    if not alias or alias.lower() == parts[-1].lower():
        alias = _reader_source_alias(tokens, block) or ""
    if alias and alias.lower() != parts[-1].lower():
        return f"{name} {alias}"
    return name


def _reader_source_alias(tokens, block) -> str | None:
    """The alias a CTE/derived reader bound its source to (``base b`` or
    ``(...) b`` -> ``b``), read right after the reader's FROM source; ``None``
    when there is none or the block is not a reader."""
    from_at = _from_at(tokens, block.select_index, block.depth)
    if from_at is None:
        return None
    at = from_at + 1
    if at >= len(tokens):
        return None
    if tokens[at].text == "(":
        close = _matching_close(tokens, at)
        if close is None:
            return None
        at = close + 1
    else:
        at += 1  # past the CTE name
    if at < len(tokens) and _upper(tokens[at]) == "AS":
        at += 1
    if (at < len(tokens) and tokens[at].depth == block.depth
            and tokens[at].kind in {"word", "identifier"}
            and _upper(tokens[at]) not in _RESERVED_AFTER_RELATION):
        return str(tokens[at].value)
    return None


def _plain_row_count(tokens, block) -> bool:
    """True when a SELECT item of ``block`` holds an unconditional row count:
    ``COUNT(*)`` or ``COUNT(<plain column>)`` (not a ``CASE``, ``IF`` or
    ``FILTER``), at any depth of the item but outside any nested query block,
    and not followed by ``OVER``.
    It counts rows of every state the WHERE does not pin away, so it counts
    the window state's rows unless the WHERE pins the state column away from
    it."""
    for item in _select_items(tokens, block.select_index, block.depth) or ():
        if any(_upper(t) == "SELECT" for t in item):
            continue
        for i, t in enumerate(item):
            if not (t.kind == "word" and _upper(t) == "COUNT"
                    and i + 1 < len(item) and item[i + 1].text == "("):
                continue
            close = _matching_close(item, i + 1)
            if close is None or _followed_by_over(item, close):
                continue
            if close + 1 < len(item) and _upper(item[close + 1]) == "FILTER":
                continue
            inner = item[i + 2:close]
            if inner and _upper(inner[0]) == "DISTINCT":
                inner = inner[1:]
            if any(_upper(t) in {"CASE", "IF"} for t in inner):
                continue
            return True
    return False


#: Words that negate a state word up to three words after them.
_NEGATORS = frozenset({"not", "never", "no", "without"})
_NEGATED_PREFIX_RE = re.compile(r"(?:^|[^a-z0-9])(?:un|in|non)-$")
_WORD_RE = re.compile(r"[a-z0-9']+")


def _positive(question: str, word: str) -> bool:
    """True when ``word`` occurs in ``question`` at least once without a
    negator (``not``, ``never``, ``no``, ``without`` or a ``n't`` word) among
    the three words before it and without a negating prefix (``un-``, ``in-``,
    ``non-``; joined forms such as ``uncompleted`` or ``incomplete`` are a
    different word and never match)."""
    text = str(question or "").lower().replace("\u2019", "'")
    pattern = re.compile(r"(?<![a-z0-9_])" + re.escape(word.lower()) + r"(?![a-z0-9_])")
    for match in pattern.finditer(text):
        before = text[:match.start()]
        if _NEGATED_PREFIX_RE.search(before):
            continue
        if any(w in _NEGATORS or w.endswith("n't") for w in _WORD_RE.findall(before)[-3:]):
            continue
        return True
    return False


def _disclosure_dropped() -> bool:
    """True under ``RESPONSE_INTEGRITY_MODE=enforce``, which appends no engine
    lines (``system_agent._append_engine_facts``): a window that would only be
    disclosed is refused there instead, so it is never silent."""
    from .config import response_integrity_enforced
    return response_integrity_enforced()


def _named(question: str, mapping: StateTimes, column: str) -> bool:
    """True when the question names the state word of the state whose own
    time column ``column`` is (``completed`` or ``complete`` for COMPLETED)
    in a positive mention (:func:`_positive`). Words alone cannot tell a
    cohort window ("were completed on 1/5") from an incidental mention ("what
    does completed mean?"), so such a query is not refused but is always
    disclosed (:attr:`StateTimeWindow.refuse` is False), unless it is a state
    breakdown (:func:`_decide_one`). The physical column name exempts it from
    neither: words cannot tell a chosen window from a duration's end or a
    column the question only mentions. :func:`decide` passes the question
    with a duration's endpoints blanked (:func:`_outside_durations`), so
    neither reads them."""
    state = mapping.state_of(column)
    return bool(state) and any(_positive(question, word)
                               for word in {str(state).lower(), *_forms(state)})


# --------------------------------------------------------------------------- #
# A duration's endpoints are not the window
# --------------------------------------------------------------------------- #
#: Words that join the two ends of a duration or difference ("X minus Y",
#: "X - Y", "from X to Y", "start-to-complete").
_JOINS = frozenset({"minus", "-", "to", "until", "till", "through", "thru"})
#: Words a duration's ends may carry around the word that joins them ("the
#: complete time minus the start time", "when it completed").
_PADDING = frozenset({"the", "a", "an", "its", "their", "each", "every", "this", "that", "when",
                      "it", "they", "of", "time", "times", "timestamp", "timestamps", "date",
                      "dates", "datetime", "ts"})
#: Words before the first end of "between X and Y", "difference of X and Y".
_PAIR_OPENERS = frozenset({"between", "difference"})
_PUNCT = frozenset("()[]'\"`")
_MAX_PADDING = 3
#: A time word's own suffix: "start time", "start_timestamp", "start-date".
_SUFFIX = (r"(?:(?:[\s_]+|-)(?:time|times|timestamp|timestamps|date|dates|datetime|ts|at)"
           r"(?![a-z0-9_]))*")
#: A call whose arguments are a duration's ends or an offset from one.
_DURATION_CALL_RE = re.compile(
    r"(?<![a-z0-9_])(?:(?:date|time|timestamp|datetime)_?(?:diff|add|sub)|age(?=\())\s*\(")
_TOKEN_RE = re.compile(r"[a-z0-9_]+(?:'s)?|[^\sa-z0-9_]")
_PLAIN_WORD_RE = re.compile(r"[a-z_]+(?:'s)?")
#: Dashes that can stand for minus in copied text, an explicit set: hyphen,
#: non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus
#: sign, heavy minus sign, and the small and fullwidth forms.
_MINUS_LIKE_RE = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2796\ufe58\ufe63\uff0d]")


def _mention_re(mappings, temporal):
    """A time mention in lowercased question text: a date or timestamp column
    of the table (as written, or with spaces for underscores), or a word that
    starts with a form of a sampled state value (``start``, ``starting``,
    ``completion``), with its own suffix words. ``None`` when there is none."""
    columns = {str(c).lower() for c in temporal or ()}
    columns |= {str(c).lower() for m in mappings for c in m.temporal}
    spelled = {c for c in columns if c} | {c.replace("_", " ") for c in columns if c}
    forms = {f for m in mappings for value, _c in m.times for f in _forms(value)}
    alternatives = []
    if spelled:
        alternatives.append("|".join(re.escape(c) for c in sorted(spelled, key=len, reverse=True)))
    if forms:
        alternatives.append("(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len,
                                                                          reverse=True))
                            + ")[a-z]*")
    if not alternatives:
        return None
    return re.compile(r"(?<![a-z0-9_])(?:" + "|".join(alternatives) + ")" + _SUFFIX
                      + r"(?![a-z0-9_])")


def _close_of(text: str, open_at: int) -> int:
    """The index of the ``)`` that closes the ``(`` at ``open_at``, or the end
    of the text when none does (every later mention is then an argument)."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _joined(gap: str) -> bool:
    """True when the text between two time mentions joins them as a
    duration's ends: a join word (``minus``, ``-``, ``to``) and at most
    :data:`_MAX_PADDING` padding words (``the``, ``its``, a possessive, a call
    name such as ``to_unixtime(``), brackets and quotes; no number."""
    tokens = _TOKEN_RE.findall(gap)
    if not any(t in _JOINS for t in tokens):
        return False
    words = 0
    for n, token in enumerate(tokens):
        if token in _JOINS or token in _PUNCT:
            continue
        call = n + 1 < len(tokens) and tokens[n + 1] == "("
        if not _PLAIN_WORD_RE.fullmatch(token) or not (
                token in _PADDING or token.endswith("'s") or call):
            return False
        words += 1
    return words <= _MAX_PADDING


def _and_joined(gap: str, before: str) -> bool:
    """True for the ends of "between X and Y" or "difference of X and Y":
    the gap is one ``and`` among plain words, and an opener stands at most
    :data:`_MAX_PADDING` plain words before the first end."""
    tokens = [t for t in _TOKEN_RE.findall(gap) if t not in _PUNCT]
    if tokens.count("and") != 1 or len(tokens) > 2 * _MAX_PADDING + 1:
        return False
    if not all(_PLAIN_WORD_RE.fullmatch(t) for t in tokens):
        return False
    head = [t for t in _TOKEN_RE.findall(before) if t not in _PUNCT][-(_MAX_PADDING + 1):]
    for n in range(len(head) - 1, -1, -1):
        if head[n] in _PAIR_OPENERS:
            return all(_PLAIN_WORD_RE.fullmatch(t) for t in head[n + 1:])
    return False


def _outside_durations(question: str, mappings, temporal) -> str:
    """``question``, lowercased, with every time mention that is an end of a
    duration or difference blanked: two mentions joined by
    :func:`_joined` or :func:`_and_joined`, and any mention inside the
    arguments of a ``date_diff``, ``date_add``, ``date_sub`` or ``age`` style
    call. A mention is read only for whether it is such an end; one that
    might be is blanked (a window it named costs one refusal and the
    resend). Every other mention is left as it is, so a window word ("whose
    start time falls in the window", "started on") still names the window.
    Generic: the mentions are the table's own time columns and the forms of
    its sampled state values."""
    text = str(question or "").lower().replace("\u2019", "'")
    # Every dash a copied formula may carry for minus reads as "-" (a
    # presentation selector after it, as in an emoji minus, is dropped first).
    text = _MINUS_LIKE_RE.sub("-", text.replace("\ufe0f", "").replace("\ufe0e", ""))
    mention = _mention_re(mappings, temporal)
    if mention is None:
        return text
    spans = [m.span() for m in mention.finditer(text)]
    blanked: set = set()
    for call in _DURATION_CALL_RE.finditer(text):
        # Every time inside a date call is a duration end, compared to a value
        # or not: text cannot tell an elapsed-time filter from a window, so an
        # unsure reading is refused once and the resend runs.
        close = _close_of(text, call.end() - 1)
        blanked |= {i for i, (start, end) in enumerate(spans)
                    if call.end() <= start and end <= close}
    for i in range(len(spans) - 1):
        gap = text[spans[i][1]:spans[i + 1][0]]
        if _joined(gap) or _and_joined(gap, text[:spans[i][0]]):
            blanked |= {i, i + 1}
    out = list(text)
    for i in blanked:
        start, end = spans[i]
        out[start:end] = " " * (end - start)
    return _one_named_column("".join(out), mappings, temporal)


def _one_named_column(text: str, mappings, temporal) -> str:
    """``text`` with every time column name blanked when it still names two or
    more different time columns of the table: the names alone cannot say
    which one is the window (one may be a duration's end, however it is
    worded), so none counts as naming it (one refusal, then the resend)."""
    columns = {str(c).lower() for c in temporal or ()}
    columns |= {str(c).lower() for m in mappings for c in m.temporal}
    spelled = {c: c for c in columns if c} | {c.replace("_", " "): c for c in columns if c}
    if not spelled:
        return text
    name = re.compile(r"(?<![a-z0-9_])(?:" + "|".join(
        re.escape(s) for s in sorted(spelled, key=len, reverse=True)) + r")(?![a-z0-9_])")
    found = list(name.finditer(text))
    if len({spelled[m.group()] for m in found}) < 2:
        return text
    out = list(text)
    for m in found:
        out[m.start():m.end()] = " " * (m.end() - m.start())
    return "".join(out)


def _names_in(tokens) -> set:
    return {str(t.value).lower() for t in tokens or () if t.kind in {"word", "identifier"}}


def _has_or(tokens) -> bool:
    return any(_upper(t) == "OR" for t in tokens)


def _conjuncts(tokens) -> list:
    """The AND conjuncts of a boolean expression, nested ANDs flattened."""
    out: list = []
    for part in (_split_boolean(tokens, "AND") if tokens else []):
        part = _strip_outer(part)
        if not part:
            continue
        if len(_split_boolean(part, "AND")) > 1:
            out.extend(_conjuncts(part))
        else:
            out.append(part)
    return out


def _null_test(part) -> str | None:
    """The column of ``<column> IS [NOT] NULL``, else ``None``: a filter on
    whether a row has that time, not a window on it."""
    base = min(t.depth for t in part)
    words = [(i, _upper(t)) for i, t in enumerate(part) if t.depth == base]
    at = next((i for i, word in words if word == "IS"), None)
    rest = [word for i, word in words if at is not None and i > at]
    if at is None or rest not in (["NULL"], ["NOT", "NULL"]) or len(part) != at + 1 + len(rest):
        return None
    column = _column_ref(part[:at])
    return column.lower() if column else None


def _branch_facts(conjuncts, column: str, temporal: set) -> tuple:
    """``(values the conjuncts pin the state column to or None, bounded time
    columns, read)``: ``read`` is False when a conjunct names the state column
    or a time column in a form this reader does not read (under OR or NOT,
    wrapped, computed). ``<time column> IS [NOT] NULL`` is not a window: it
    neither bounds nor makes the branch unread."""
    from .storage_bound_rule import _bounded_column
    values, bounds, read = None, set(), True
    for part in conjuncts:
        names = _names_in(part)
        if len(_split_boolean(part, "OR")) == 1:
            pin = _pin(part)
            if pin and pin[0] == column:
                values = pin[1] if values is None else values & pin[1]
                continue
            if _null_test(part) in temporal:
                continue
            bounded = _bounded_column(part)
            if bounded and bounded.lower() in temporal:
                bounds.add(bounded.lower())
                continue
        if names & ({column} | temporal):
            read = False
    return values, bounds, read


@dataclass(frozen=True)
class StateTimeWindow:
    """A query whose rows of some states are not windowed only on each
    state's own time column."""

    table: str
    state_column: str
    rows: tuple           # ((state, its own column, (window columns read), read), ...)
    owners: tuple         # ((window column, the state it is the own column of, or ""), ...)
    conjuncts: tuple = ()
    under_or: bool = False
    #: False when the question names the window's state word and the query
    #: counts only other states: the query runs unrefused, and is still
    #: disclosed. A state breakdown is always refused once.
    refuse: bool = True
    #: True when the query also counts the rows of every state not in
    #: ``rows`` (a GROUP BY of the state column no pin narrows), on the same
    #: window: states the schema's samples did not show or gave no own time.
    others: bool = False
    #: True when the question named every window's state word (but not its
    #: column): the disclosure does not then say the question did not name it.
    named: bool = False

    def windows(self) -> list:
        return sorted({w for _s, _o, ws, _r in self.rows for w in ws})

    def to_dict(self, names=None) -> dict:
        """The record a result or a refusal carries. A name that is not a
        plain identifier (``engine_names``) is a placeholder, counted in
        ``names_withheld``."""
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        owner = dict(self.owners)
        windows = self.windows()
        single = windows[0] if len(windows) == 1 and all(ws for _s, _o, ws, _r in self.rows) \
            else None
        out = {"table": names.show(self.table, "table"),
               "state_column": names.show(self.state_column),
               "window_column": names.show(single) if single else None,
               "window_state": (names.show(owner[single], "state")
                                if single and owner.get(single) else None),
               "states": [names.show(s, "state") for s, _o, _w, _r in self.rows],
               "own_columns": {names.show(s, "state"): names.show(o)
                               for s, o, _w, _r in self.rows if o},
               "windows": {names.show(s, "state"): [names.show(w) for w in ws]
                           for s, _o, ws, _r in self.rows},
               "under_or": self.under_or}
        if self.others:
            out["other_states_counted"] = True
        if names.count:
            out["names_withheld"] = names.count
        return out


#: :func:`_decide_or` found no OR on the state column: the plain rule decides.
_NO_OR = object()


def decide_parts(sql: str, table: str, bounded: dict, column_types: dict, mappings, *,
                 question: str) -> tuple:
    """Every window on another state's time this query runs with, one
    :class:`StateTimeWindow` per part, else ``()`` (see the module
    docstring). A block's conditional aggregates can count states on several
    windows (or the same state on two windows, as two separate figures); each
    window group and each OR path is its own part, decided as the filtered,
    IN-pin or OR equivalent it equals, in that order: the outer-WHERE group,
    each path-only window group, each OR path. Equal parts appear once.
    ``bounded`` is ``storage_bound_rule.bounded_columns``'s map for the query
    (top-level AND bounds), ``column_types`` the catalog's and ``mappings``
    the table's :class:`StateTimes` (one per state column)."""
    if isinstance(mappings, StateTimes):
        mappings = (mappings,)
    if not mappings:
        return ()
    return _decide_core(sql, table, bounded, column_types, mappings, question=question,
                        allow_conditional=True)


def decide(sql: str, table: str, bounded: dict, column_types: dict, mappings, *,
           question: str):
    """:func:`decide_parts` for a caller that expects one decision: ``None``
    for none, the :class:`StateTimeWindow` when there is exactly one part, and
    the whole tuple of parts when there are several (never one part of
    several). Production callers use :func:`decide_parts`."""
    parts = decide_parts(sql, table, bounded, column_types, mappings, question=question)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else parts


def _decide_equivalent(sql: str, table: str, column_types: dict, mappings,
                       question: str = "") -> tuple:
    """Decide a built equivalent ``SELECT COUNT(*) ...`` query with the same
    code the outer call uses, with the conditional-aggregate reader off (the
    equivalent has none, and this guards against recursion). Its own bounded
    map is read here, so the equivalent is decided end to end exactly as a
    real query would be. ``question`` is passed on so a decision returned to
    the caller (an OR path's whole decision) reads the real question's state
    word; it is left empty when only the equivalent's states are read. The
    parts, as :func:`decide_parts` gives them (at most one here)."""
    read = bounded_columns(sql, include_same_column_or=True)
    bounded = read[1] if read else {}
    return _decide_core(sql, table, bounded, column_types, mappings, question=question,
                        allow_conditional=False)


def _decide_core(sql: str, table: str, bounded: dict, column_types: dict, mappings, *,
                 question: str, allow_conditional: bool) -> tuple:
    """The shared decision body for a real query and for a conditional
    aggregate's built equivalent: the tuple of part decisions, ``()`` for
    none. ``allow_conditional`` reads the states a conditional aggregate
    counts by building its equivalent queries and deciding them here too
    (:func:`_conditional_states`); it is off for those equivalents, which
    give at most one part."""
    tokens, uncertain = tokenize(sql if isinstance(sql, str) else "")
    scans, scan_uncertain = relations(tokens) if not uncertain and tokens else ([], True)
    if scan_uncertain or len(scans) != 1:
        return ()
    temporal = {str(c).lower() for c, t in (column_types or {}).items() if _temporal(t)}
    # A time named only as a duration's end is not the window's (both the
    # column and the state word are read on what is left).
    question = _outside_durations(question, mappings, temporal)
    where = _where_expression(tokens, scans[0]) or []
    block, outer = _counting_block(tokens, scans[0])
    if outer:
        combined = _outer_pins(sql, where, outer, {m.state_column.lower() for m in mappings})
        if combined is None:
            block = scans[0]
        else:
            where = combined
    grouped = _grouped(tokens, block)
    having = _having(tokens, block)
    owned = frozenset(c.lower() for m in mappings for _v, c in m.times)
    counts = _counts_rows(tokens, block)
    plain_count = _plain_row_count(tokens, block)
    for mapping in mappings:
        found = _decide_or(table, mapping, where, temporal, question)
        if found is _NO_OR:
            if allow_conditional:
                read = _conditional_states(tokens, block, mapping, sql, table, column_types,
                                           where, mappings, question=question)
                parts = _decide_block_parts(
                    read, sql, table, bounded, mapping, where, (grouped, having), question,
                    owned, column_types, counts, plain_count, temporal, from_text=_from_text(
                        tokens, block), mappings=mappings)
            else:
                top = [c for c in (bounded or {}) if c in temporal]
                found = _decide_one(sql, table, bounded, top, mapping, where, (grouped, having),
                                    question, owned, column_types, counts, frozenset(), plain_count)
                parts = (found,) if found is not None else ()
        else:
            parts = (found,) if found is not None else ()
        if parts:
            return parts
    return ()


def _decide_block_parts(read, sql, table, bounded, mapping, where, grouping, question,
                        owned, column_types, counts, plain_count, temporal, *, from_text,
                        mappings) -> tuple:
    """Every window-group and OR decision of one block, each its own
    :class:`StateTimeWindow` part (:func:`_conditional_states` partitioned
    them), in order: the outer-WHERE group, each path-only window group, each
    OR path. The outer-WHERE group keeps the plain rule (Q2's ``COUNT(*)`` plus
    a conditional count is unchanged); each path-only window group and each OR
    path is decided as the equivalent it equals. The parts are not merged: the
    same state counted on two windows is two figures, and each part keeps its
    own rows, refusal and named flag. Equal parts appear once."""
    parts: list = []

    def keep(found) -> None:
        for part in found:
            if part is not None and part not in parts:
                parts.append(part)

    # The outer-WHERE window group, plus any unconditional count, is decided by
    # the plain rule with the block's own bounded map (Q2 shape unchanged).
    top = [c for c in (bounded or {}) if c in temporal]
    keep((_decide_one(sql, table, bounded, top, mapping, where, grouping, question, owned,
                      column_types, counts, read.outer_states, plain_count),))
    # Each path-only window group: the IN pin of its states on that window.
    for _window, atoms, states in read.path_groups:
        if from_text is None or not states:
            continue
        clauses = []
        if where:
            clauses.append(_and_clause(_text(where)))
        clauses.append(_and_clause(" AND ".join(atoms)))
        clauses.append(_in_pin_clause(mapping.state_column, states))
        where_sql = " AND ".join(c for c in clauses if c)
        equivalent = "SELECT COUNT(*) FROM " + from_text + " WHERE " + where_sql  # nosec B608 - analysis text only, never executed
        keep(_decide_equivalent(equivalent, table, column_types, mappings, question=question))
    # Each OR path, decided whole by _decide_or (branch-specific predicates).
    for equivalent in read.or_equivalents:
        keep(_decide_equivalent(equivalent, table, column_types, mappings, question=question))
    return tuple(parts)


def _in_pin_clause(state_column: str, states) -> str:
    """``<state column> IN ('A', 'B')`` as a bare SQL clause for a window
    group's equivalent query (built by concatenation, not a note)."""
    values = ", ".join("'" + str(s) + "'" for s in states)
    return str(state_column) + " IN (" + values + ")"


def _decide_one(sql, table, bounded, top, mapping, where, grouping, question, owned,
                column_types, counts, cond_states=frozenset(), plain_count=False):
    """The plain rule: one bounded time column, the own column of a state the
    query does not count (by pin, GROUP BY narrowed by a HAVING pin, or a
    conditional aggregate). A
    query that also counts the window's own state is a state breakdown and is
    refused even when the question names that state (:func:`_named`), unless
    a HAVING on the state column could not be read. A window on a column that
    no state of any of the table's state columns owns (``owned``) is decided
    by :func:`_decide_unowned`, for a query that counts rows (``counts``).
    ``cond_states`` are the states counted inside conditional aggregates and
    ``plain_count`` is whether an unconditional row count is present (both
    read by :func:`decide`)."""
    if len(top) != 1:
        return None
    window = top[0]
    window_state = mapping.state_of(window)
    if window_state is None:
        # A conditional aggregate counts rows too, even when _counts_rows did
        # not see a plain COUNT( call (SUM(CASE ...), COUNT_IF(...)).
        if window in owned or not (counts or cond_states):
            return None
        return _decide_unowned(sql, table, bounded, window, mapping, where, grouping,
                               question, column_types, cond_states)
    counted = _counted_states(mapping, where, grouping, cond_states)
    if counted is None:
        return None
    values, certain = counted
    own = tuple(sorted((v, mapping.time_of(v)) for v in values
                       if mapping.time_of(v) and mapping.time_of(v).lower() != window))
    # The question writing the window column's name does not exempt it: words
    # cannot tell a chosen window from a duration's end or a column it only
    # mentions, so only the state word (:func:`_named`) lowers this to a
    # disclosure, and with neither the window is refused once.
    if not own:
        return None
    named = where_names(sql)
    if named is None or named & {c.lower() for _v, c in own}:
        return None
    # A state breakdown (the window's own state counted beside the others):
    # the state word justifies the window for that state's rows only. The
    # window's own state is counted when it is among the counted states, or
    # when an unconditional row count that the WHERE does not pin away from it
    # is present (as in Q2's COUNT(*) beside a conditional count of another
    # state). Only when the counted states are certain: an unread HAVING
    # leaves which groups the query returns unknown, so it is not a breakdown.
    pin = _pinned(where, mapping.state_column.lower())
    counts_window = certain and (
        window_state.lower() in {str(v).lower() for v in values}
        or (plain_count and (pin is None or window_state.lower() in {str(v).lower() for v in pin})))
    breakdown = bool(counts_window)
    others = _others_counted(where, grouping, mapping.state_column.lower())
    # A certainly counted state the name rule gives no own time column is on
    # the same window; an unnarrowed GROUP BY says so for them (``others``).
    ownless = () if others or not certain else tuple(
        (v, None) for v in values if mapping.time_of(v) is None)
    named_word = _named(question, mapping, window)
    return StateTimeWindow(table, mapping.state_column,
                           tuple((v, c, (window,), True)
                                 for v, c in sorted(own + ownless, key=lambda r: str(r[0]))),
                           ((window, window_state),), tuple(bounded[window])[:4],
                           refuse=(_disclosure_dropped() or breakdown or not named_word),
                           others=others, named=named_word)


def _others_counted(where, grouping, column: str) -> bool:
    """True when the query GROUPs BY the state column ``column`` and neither
    its WHERE nor its HAVING (read) pins it: it returns a group for every
    state value, including those the samples did not show or gave no own
    time column."""
    grouped, having = grouping
    kept, read = _having_states(having, column)
    return column in grouped and _pinned(where, column) is None and kept is None and read


def _decide_unowned(sql, table, bounded, window, mapping, where, grouping, question,
                    column_types, cond_states=frozenset()):
    """The plain rule for a count (the caller checked :func:`_counts_rows`)
    on a time column no state was identified to own: a query that certainly
    counts two or more states of ``mapping``'s state column (the column
    pinned by ``=`` or ``IN``, not named in the WHERE, or counted inside
    conditional aggregates, any HAVING on it
    read) is refused once, every counted state with its own time column in
    the remedy. A pinned state the name rule gives no own time column is
    named as counted on the window too; when no counted state has one, no
    other query would count them better, so the count runs, disclosed
    (refused once only under enforce, :func:`_disclosure_dropped`). For a
    GROUP BY no pin narrows, so are the rows of every other state
    (``others``). A single state
    keeps the cohort reading and runs. Not refused either: a WHERE that names
    a counted state's own column (so also
    a window that is a counted state's own column by name, a state the
    samples did not show), and a window that is the own time of a record
    field the WHERE constrains (:func:`_record_time_of_constrained`)."""
    column = mapping.state_column.lower()
    pinned = _pinned(where, column)
    if pinned is None and column in _names_in(where):
        return None
    counted = _counted_states(mapping, where, grouping, cond_states)
    if counted is None:
        return None
    values, certain = counted
    if not certain or len({str(v).lower() for v in values}) < 2:
        return None
    own = tuple(sorted((v, mapping.time_of(v)) for v in values if mapping.time_of(v)))
    # As in :func:`_decide_one`, a written column name exempts nothing: the
    # window may be the time of a state the samples never showed, written
    # only as a duration's end.
    named = where_names(sql)
    if named is None or named & {c.lower() for _v, c in own}:
        return None
    if _record_time_of_constrained(where, window, column_types):
        return None
    return StateTimeWindow(table, mapping.state_column,
                           tuple((v, mapping.time_of(v), (window,), True)
                                 for v in sorted(values)),
                           ((window, ""),), tuple(bounded[window])[:4],
                           # No counted state has an own time column to query
                           # instead: the count runs, disclosed (refused only
                           # where engine lines are not appended).
                           refuse=bool(own) or _disclosure_dropped(),
                           others=_others_counted(where, grouping, column))


def _record_time_of_constrained(where, window: str, column_types) -> bool:
    """True when the WHERE constrains a record field (``record_time``'s
    reading) whose own sibling time column is ``window``: the window is that
    record's own time, which the record-time rule asks for."""
    from .record_time import _constrained, own_time_column
    types = column_types or {}
    return any((own_time_column(c, types) or "").lower() == window
               for c in _constrained(where, types))


def _counted_states(mapping, where, grouping, cond_states=frozenset()) -> tuple | None:
    """``(states, certain)`` a query counts by ``mapping``'s state column:
    its WHERE pin, narrowed by a HAVING pin when it groups by that column;
    every sampled state for a grouped query with no pin; the states counted
    inside its conditional aggregates (``cond_states``) when it neither pins
    nor groups by the column. ``certain`` is
    False when a HAVING names the column in a form :func:`_having_states`
    cannot read (the groups it keeps are not known). ``None`` when the query
    neither pins nor groups by the column nor counts any state conditionally.

    When the WHERE pins the column, conditional states are intersected with
    the pin (so they add nothing the pin did not already count); when the
    query groups by the column or pins it, the conditional states add nothing
    new, so they are read only to decide a query that does neither."""
    grouped, having = grouping
    column = mapping.state_column.lower()
    values = _pinned(where, column)
    if column not in grouped:
        if values is None:
            # Not pinned and not grouped by the state column: the only states
            # it counts are those its conditional aggregates pin.
            return (set(cond_states), True) if cond_states else None
        return values, True
    kept, certain = _having_states(having, column)
    if kept is not None:
        values = kept if values is None else values & kept
    if values is None:
        values = {value for value, _c in mapping.times}
    return values, certain


def _note_or_states(found: dict, refuse: list, mapping, question: str, values,
                    windows: set, read: bool) -> None:
    """For :func:`_decide_or`: record in ``found`` each state of ``values``
    whose branch ``windows`` are not proven its own column; add to
    ``refuse`` those whose windows the question does not name by state word
    (a written column name counts for nothing)."""
    for value in values:
        own = mapping.time_of(value)
        if own is None:
            continue
        if read and windows == {own.lower()}:
            continue
        # As in :func:`_decide_one`, a written column name exempts nothing.
        if not (read and windows and all(_named(question, mapping, w) for w in windows)):
            refuse.append(value)
        seen, was_read = found.get(value, (set(), True))
        found[value] = (seen | windows, was_read and read)


def _decide_or(table, mapping, where, temporal: set, question: str):
    """The state column under an OR. Proven, and ``None``, only when every
    branch pins its states and bounds no time column but each one's own (with
    no time bound outside the OR); :data:`_NO_OR` when no OR involves the
    state column or the WHERE names no time column; else the
    :class:`StateTimeWindow` of the states it could not prove."""
    column = mapping.state_column.lower()
    if not where or not (_names_in(where) & temporal):
        return _NO_OR
    parts = _conjuncts(where)
    or_at = {i for i, p in enumerate(parts)
             if len(_split_boolean(p, "OR")) > 1 and column in _names_in(p)}
    nested = any(i not in or_at and column in _names_in(p) and _has_or(p)
                 for i, p in enumerate(parts))
    if not or_at and not nested:
        return _NO_OR
    outer_values, outer_bounds, outer_read = _branch_facts(
        [p for i, p in enumerate(parts) if i not in or_at], column, temporal)
    every = {value for value, _c in mapping.times}
    found: dict = {}
    refuse: list = []
    note = partial(_note_or_states, found, refuse, mapping, question)
    if not or_at:
        note(outer_values if outer_values is not None else every, outer_bounds, False)
    for i in sorted(or_at):
        for branch in _split_boolean(parts[i], "OR"):
            values, bounds, read = _branch_facts(_conjuncts(_strip_outer(branch)), column,
                                                 temporal)
            if values is None:
                values = outer_values if outer_values is not None else every
            elif outer_values is not None:
                values = values & outer_values
            note(values, bounds | outer_bounds, read and outer_read)
    if not found:
        return None
    rows = tuple(sorted((v, mapping.time_of(v), tuple(sorted(ws)), read)
                        for v, (ws, read) in found.items()))
    owners = tuple(sorted({(w, mapping.state_of(w) or "") for _v, _o, ws, _r in rows
                           for w in ws}))
    return StateTimeWindow(table, mapping.state_column, rows, owners, (), under_or=True,
                           refuse=bool(refuse) or _disclosure_dropped(), named=not refuse)


# --------------------------------------------------------------------------- #
# The refusal and the disclosure: facts, names only when printable
# --------------------------------------------------------------------------- #
def _window_text(names, windows, owners) -> str:
    owner = dict(owners)
    shown = []
    for window in windows:
        state = owner.get(window)
        text = names.show(window)
        shown.append(f"{text} (the time column of {names.show(state, 'state')} rows)"
                     if state else text)
    return " and ".join(shown)


def _unowned(window: StateTimeWindow) -> str | None:
    """The plain rule's window when no state owns it (owner ``""``), else
    ``None``."""
    if window.under_or or len(window.owners) != 1:
        return None
    column, owner = window.owners[0]
    return None if owner else column


def _row_fact(names, window: StateTimeWindow, row, *, ran: bool = True) -> str:
    """One state's rows, as facts: which time column windowed them, and that
    rows without it are not counted (``ran``), or would not be (a refusal).
    On a window no state owns: that column, and the state's own one, or that
    the engine identified none."""
    state, own, windows, read = row
    shown = names.show(state, "state")
    were, counted = ("were", "are not counted") if ran else ("are", "would not be counted")
    if not read or not windows:
        return (f"{shown} rows {were} selected under an OR whose time window the engine could "
                "not read")
    unowned = _unowned(window)
    if unowned and list(windows) == [unowned]:
        if own:
            return (f"{shown} rows {were} windowed on {names.show(unowned)}, not on their own "
                    f"{names.show(own)}")
        return (f"{shown} rows {were} windowed on {names.show(unowned)}, and the engine "
                "identified no time column of their own")
    missing = names.show(windows[0]) if len(windows) == 1 else "any of those columns"
    if ran and len(windows) == 1:
        # The effect, in plain words: which rows the window keeps, and by
        # whose time they are counted.
        on = names.show(windows[0])
        own_part = (f", not by their own {names.show(own)}" if own
                    else ", and the engine identified no time column of their own")
        return (f"{shown} rows were placed in the window by "
                f"{_window_text(names, windows, window.owners)}{own_part}, so {shown} rows whose "
                f"{on} is outside the window or empty are not counted, and {shown} rows are "
                f"counted by {_counted_by(names, windows[0], window.owners)}")
    return (f"{shown} rows {were} windowed on {_window_text(names, windows, window.owners)}, "
            f"so {shown} rows with no {missing} {counted}")


def _counted_by(names, window: str, owners) -> str:
    """How rows placed in the window by ``window`` are counted: by when they
    reached the state it is the own time of (``when they reached the STARTED
    state``), said that way for any printable state value so a value is never
    inflected into a verb, else by ``window`` itself."""
    state = dict(owners).get(window)
    shown = names.show(state, "state") if state else ""
    if state and shown == state:
        return f"when they reached the {shown} state"
    return f"their {names.show(window)}"


def _row_facts(names, window: StateTimeWindow, *, ran: bool = True) -> list:
    """Every row fact of ``window`` (:func:`_row_fact`), then, when it also
    counts every other state (``others``), that those rows share the window:
    on a column no state owns, that they are on it too; on a state's own
    column, also that those rows without it are not counted."""
    facts = [_row_fact(names, window, row, ran=ran) for row in window.rows]
    column = _others_window(window)
    if column:
        by = names.show(window.state_column)
        were, counted = ("were", "are not counted") if ran else ("are", "would not be counted")
        if _unowned(window):
            facts.append(f"rows of any other {by} value {were} windowed on "
                         f"{names.show(column)} too")
        elif ran:
            facts.append(f"rows of any other {by} value were placed in the window by "
                         f"{_window_text(names, [column], window.owners)} too, so those rows "
                         f"whose {names.show(column)} is outside the window or empty are not "
                         f"counted, and those rows are counted by "
                         f"{_counted_by(names, column, window.owners)}")
        else:
            facts.append(f"rows of any other {by} value {were} windowed on "
                         f"{_window_text(names, [column], window.owners)} too, so those rows "
                         f"with no {names.show(column)} {counted}")
    return facts


def _others_window(window: StateTimeWindow) -> str | None:
    """The one window column the rows of every other state share, when the
    plain rule found that the query counts them (``others``), else ``None``."""
    if not window.others or window.under_or or len(window.owners) != 1:
        return None
    return window.owners[0][0]


def _own_text(names, window: StateTimeWindow) -> str:
    own = [f"{names.show(s, 'state')} rows on {names.show(o)}" for s, o, _w, _r in window.rows if o]
    return "; ".join(own) if own else "each state's own time column"


class StateTimeWindowError(StorageBoundError):
    """A query refused before it ran for windowing states' rows on another
    state's own time column, or under an OR the reader could not prove
    (reason :data:`REASON_STATE_TIME`)."""

    def __init__(self, found: StateTimeWindow):
        super().__init__(StorageBound(found.table, (), tuple(found.windows()), found.conjuncts),
                         (), tuple(found.windows()), reason=REASON_STATE_TIME)
        self.found = found

    def to_dict(self) -> dict:
        from .engine_names import Withheld
        f, names = self.found, Withheld()
        table, column = names.show(f.table, "table"), names.show(f.state_column)
        facts = "; ".join(_row_facts(names, f, ran=False)) or (
            "the states it selects could not be read")
        unowned = _unowned(f)
        stay = ""
        if f.under_or:
            what = (f"Refused before running: this query selects rows of {table} by {column} "
                    "under an OR, and the engine could not check that each state's rows are "
                    f"windowed only on that state's own time column: {facts}.")
            repeat = ("If the query is what the question asks, send it again unchanged: it runs, "
                      "and the answer says how those rows were windowed.")
        else:
            if unowned:
                shown = names.show(unowned)
                why = (f"The engine did not identify {shown} as any state's own time column, "
                       f"and a row's {shown} can be empty or outside the window when its own "
                       "state was reached inside it, or inside the window when its state was "
                       "reached earlier")
                stay = f" Rows of a state with no time column of its own can stay on {shown}."
            else:
                why = ("A row's time column for another state can be empty or outside the "
                       "window when its own state happened inside it")
                ownless = [names.show(s, "state") for s, o, _w, _r in f.rows if not o]
                if ownless:
                    listed = (ownless[0] if len(ownless) == 1
                              else f"{', '.join(ownless[:-1])} and {ownless[-1]}")
                    stay = (" The engine identified no time column of their own for "
                            f"{listed} rows.")
            what = (f"Refused before running: this query counts rows of {table} by {column}, "
                    f"and {facts}. {why}, so the result may not count the rows the question "
                    "means.")
            repeat = ("If the question asks for that window itself, send the same query again "
                      "unchanged: it runs, and the answer says how those rows were windowed.")
        if unowned and not any(o for _s, o, _w, _r in f.rows):
            # No counted state has a time column of its own to name.
            guidance = (f"{what} The engine names no other time column for these states. Send "
                        "the same query again unchanged: it runs, and the answer says how "
                        "those rows were windowed.")
        else:
            guidance = (f"{what} Use one query per state, each filtering {column} to that one "
                        "state and windowed on that state's own time column "
                        f"({_own_text(names, f)}).{stay} A window on a state's own time column "
                        "does not filter by state: rows of other states can have that time "
                        f"too. {repeat}")
        record = f.to_dict(names)
        out = {
            "error": "state_time_window",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": REASON_STATE_TIME,
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
        if isinstance(window, StateTimeWindow) and window not in out:
            out.append(window)
    return out[:_MAX_WINDOWS]


def engine_line(windows) -> str:
    """One bounded engine line, as facts, for the windows this call's queries
    ran with, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(windows)
    if not shown:
        return ""
    names = Withheld()
    named = {w.named for w in shown}
    which = {True: ", a window the question named only by a state word",
             False: ", a window the question did not name"} if len(named) > 1 else {}
    parts = [f"in {names.show(w.table, 'table')} (by {names.show(w.state_column)}"
             f"{which.get(w.named, '')}), " + "; ".join(_row_facts(names, w)) for w in shown]
    # The opener says only what the rule read in the question: no mention of
    # the window, or its state word (a written column name is not read).
    opener = {frozenset({False}): " the question did not name,",
              frozenset({True}): " the question named only by a state word,"}.get(
                  frozenset(named), "")
    return (f"{ENGINE_PREFIX}{opener} in this turn's queries: "
            f"{'; '.join(parts)}. Each is a {MARKER}.{names.sentence()}")


def _row_requirement(names, window: StateTimeWindow, row) -> str:
    """The answer requirement for one state's rows of ``window``: on a column
    no state owns, which column counted them; on one other column, the
    effect (as :func:`_row_fact` says it when the query ran); on several,
    that rows with none of them are not counted; unread, that it could not be
    read."""
    state, own, on, read = row
    s = names.show(state, "state")
    unowned = _unowned(window)
    if read and unowned and list(on) == [unowned]:
        not_own = f", not rows whose {names.show(own)} is" if own else ""
        return (f"REQUIRED: the answer must say that its {s} figures count {s} "
                f"rows whose {names.show(unowned)} is in the window{not_own}.")
    if read and len(on) == 1:
        own_part = f", not by their own {names.show(own)}" if own else ""
        return (f"REQUIRED: the answer must say that its {s} figures place {s} rows in the "
                f"window by {_window_text(names, on, window.owners)}{own_part}: {s} rows whose "
                f"{names.show(on[0])} is outside the window or empty are not counted, and {s} "
                f"rows are counted by {_counted_by(names, on[0], window.owners)}.")
    if read and on:
        return (f"REQUIRED: the answer must say that its {s} figures count only {s} rows whose "
                f"{_window_text(names, on, window.owners)} is in the window, so {s} rows with "
                "no any of those columns are not counted.")
    return ("REQUIRED: the answer must say that the engine could not read "
            f"which time column windows its {s} figures.")


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
        unowned = _unowned(w)
        notes.extend(_row_requirement(names, w, row) for row in w.rows)
        column = _others_window(w)
        if column and unowned:
            notes.append("REQUIRED: the answer must say that its figures for any other "
                         f"{names.show(w.state_column)} value count rows whose "
                         f"{names.show(unowned)} is in the window.")
        elif column:
            notes.append("REQUIRED: the answer must say that its figures for any other "
                         f"{names.show(w.state_column)} value place those rows in the window "
                         f"by {_window_text(names, [column], w.owners)}: those rows whose "
                         f"{names.show(column)} is outside the window or empty are not "
                         "counted, and those rows are counted by "
                         f"{_counted_by(names, column, w.owners)}.")
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
