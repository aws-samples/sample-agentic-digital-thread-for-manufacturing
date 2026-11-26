# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Groups of a result that are one JSON value written in several spellings.

Measured in dry runs on an analytics deployment: a string column held a
serialized JSON list of objects, and one object was stored with its keys in
two orders, close to an even split on every day. Every query that grouped by
the column's text returned each value as two groups, so the answer reported
one of the two, or added them up by hand and got the sum wrong. A prompt line
saying to group by the id inside the value was followed about one time in
eight. One row per list element, grouped by a field inside it, gives one group
per value, and that form had already run on the same table.

* :func:`find` reads one query and its rows: the outputs that hold grouped
  values come from ``grouped_keys.read`` (GROUP BY with any aggregates,
  HAVING, ORDER BY and LIMIT; SELECT DISTINCT; one CTE or derived-table hop;
  any query it cannot read with certainty is not read). A grouped column
  qualifies when every non-null cell is a serialized JSON object, or a list
  whose elements are all objects (``schema_shape.looks_serialized_collection``,
  then a parse); a list of scalars or a column mixing the two is not read.
  Rows whose grouped values are equal once each such cell is read as JSON
  (sorted keys, compact separators) but whose text differs are one value in
  several spellings. It returns a :class:`JsonSpellings` record: the column,
  the result's rows and values, how many values came in more than one
  spelling, two example rows, the keys the items carry, the field that
  identifies every observed item (present and scalar in every one, with one
  value per item), the longest list and whether a row holds an empty list.
  For a list column it also keeps the rows whose cell is an empty list or
  NULL, which an element expansion drops: how many, and, when the query has
  exactly one ``COUNT(*)`` output over one table, the records they count and
  those rows merged across spellings by the other grouped values (exact, as
  an empty list has one value). The caller says whether the result held every
  row of its query (``complete``); a cut result, a HAVING or a second SELECT
  makes the count a lower bound. Anything else gives ``None``.

* The caller (the system agent's guarded Athena tool, in a Mode A call)
  withholds the rows the first time and returns :class:`JsonSpellingsError`'s
  payload (on the ``StorageBoundError`` refusal path the window rules use):
  the element-expansion form (:data:`EXPANSION`, :data:`ELEMENT`) grouped by
  the identifying field, or, when no field identifies every item, by a field
  the agent must pick from the keys listed; and the two facts about counting
  that way (a row counts once per element, and a row whose list is empty or
  NULL has no element and drops out, counted with :data:`EMPTY_COUNT`), with
  the records this result holds of those rows and the rows themselves
  (``empty_list_rows``). The identical query sent again runs, labelled, and
  the answer carries :func:`engine_line` and :func:`requirement_block`. Under
  ``RESPONSE_INTEGRITY_MODE=enforce`` the refusal is the same and no engine
  line is added. ``empty_lists`` carries the count to a later expansion.

A column, table or key name is printed only when ``engine_names.printable``
passes; key names come from the data. Generic: no column, table or system
names. Pure: the standard library, ``grouped_keys``, the shared tokenizer,
``storage_bound_rule``'s refusal type and ``engine_names``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .storage_bound_rule import StorageBound, StorageBoundError

ERROR_NAME = "JSON_SPELLINGS"
REASON = "json_value_in_several_spellings"
#: Opens the engine line; :func:`neutralize` makes a model-written copy inert.
ENGINE_PREFIX = "[engine] JSON spellings"
MARKER = "grouped by JSON text"
#: One row per element of a serialized JSON list, and an element's field, in
#: Athena SQL (the same form the IDENTIFIER FORMAT rule gives).
EXPANSION = "CROSS JOIN UNNEST(CAST(json_parse({column}) AS ARRAY(JSON))) AS t(e)"
ELEMENT = "json_extract_scalar(e, '$.{key}')"
#: A field of a serialized JSON object.
OBJECT_FIELD = "json_extract_scalar({column}, '$.{key}')"
#: The rows of a serialized list column that an element expansion drops: an
#: empty list in any spelling, and a NULL cell (``json_array_length(NULL)`` is
#: NULL, so a bare ``= 0`` misses it).
EMPTY_COUNT = "COALESCE(json_array_length({column}), 0) = 0"
#: Rows of ``empty_list_rows`` a refusal shows, at most.
MAX_EMPTY_ROWS = 20
#: A cell longer than this is not parsed, so its column is not read.
MAX_CELL_CHARS = 65_536
#: Rows a result may hold for its cells to be read.
MAX_ROWS = 20_000
#: Element keys a text names, at most.
MAX_KEYS_SHOWN = 12
_MAX_SHOWN = 6
#: Placeholders when no plain name can be printed in the SQL form.
_COLUMN_PLACEHOLDER = "col"
_KEY_PLACEHOLDER = "<field>"
_PREFERRED_KEY = "id"
#: A key ``$.<key>`` reads as one top-level field: one plain JSONPath segment.
#: A dot, space, quote, bracket or leading digit would read as another path.
_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JsonSpellings:
    """A grouped result whose key column ``column`` holds ``split`` of its
    ``values`` JSON values in more than one spelling, over ``groups`` rows."""

    table: str
    column: str
    plain: bool                 # the key is one column reference, named ``column``
    groups: int
    values: int
    split: int
    example_rows: tuple         # two 1-based rows holding one value in two spellings
    container: str              # "list" or "object"
    element_keys: tuple         # the keys the elements (or the object) carry, sorted
    field: str                  # a key that identifies every observed item, else ""
    longest: int                # the longest list (0 for an object)
    empty_lists: bool           # a row holds an empty list
    #: Always refuse-once: the check reads the result alone.
    refuse: bool = True
    #: Rows of a list column whose cell is an empty list or NULL; an element
    #: expansion has no row for their records (0 for an object column).
    empty_rows: int = 0
    #: Which of the two those rows hold: ``"empty"``, ``"null"`` or ``"both"``.
    empty_kind: str = ""
    #: The records those rows count (their one ``COUNT(*)`` output summed),
    #: else ``None``; a lower bound unless ``complete``.
    empty_records: int | None = None
    #: ``(column labels, rows)``: those rows merged across spellings by the
    #: other grouped values, with the count, when ``empty_records`` is known.
    empty_table: tuple = ()
    #: The result held every row of its query, and no HAVING or second
    #: SELECT narrowed which groups it shows.
    complete: bool = True

    @property
    def records_known(self) -> bool:
        """The count of records with an empty or NULL list is exact: a
        complete result that counts them, or holds no such row."""
        if self.container != "list" or not self.complete:
            return False
        return self.empty_rows == 0 or self.empty_records is not None

    def empty_rows_dict(self, names) -> dict | None:
        """``{columns, rows}`` of :attr:`empty_table`, at most
        :data:`MAX_EMPTY_ROWS` rows (``rows_not_shown`` counts the rest); a
        column label that is not a plain identifier is a placeholder."""
        if not self.empty_table:
            return None
        labels, rows = self.empty_table
        out = {"columns": [names.show(str(label)) for label in labels],
               "rows": [list(row) for row in rows[:MAX_EMPTY_ROWS]]}
        if len(rows) > MAX_EMPTY_ROWS:
            out["rows_not_shown"] = len(rows) - MAX_EMPTY_ROWS
        return out

    def to_dict(self, names=None) -> dict:
        """The record a result or a refusal carries. A name that is not a
        plain identifier (``engine_names``) is a placeholder, and a key name
        that is not one is left out, both counted in ``names_withheld``."""
        from .engine_names import Withheld
        names = names if names is not None else Withheld()
        out = {"table": names.show(self.table, "table") if self.table else "",
               "column": names.show(self.column),
               "groups": self.groups,
               "values": self.values,
               "values_in_several_spellings": self.split,
               "example_rows": list(self.example_rows),
               "container": self.container,
               "element_keys": names.keep(self.element_keys[:MAX_KEYS_SHOWN], "field"),
               "group_field": self.field or None,
               "longest_list": self.longest,
               "empty_lists": self.empty_lists}
        if self.empty_rows:
            out["rows_with_empty_list"] = self.empty_rows
            if self.empty_records is not None:
                key = ("records_with_empty_list" if self.complete
                       else "records_with_empty_list_at_least")
                out[key] = self.empty_records
            shown = self.empty_rows_dict(names)
            if shown is not None:
                out["empty_list_rows"] = shown
        if names.count:
            out["names_withheld"] = names.count
        return out


def _parse(value):
    """The JSON object or list a cell's text holds, else ``None``."""
    from .schema_shape import looks_serialized_collection
    if not isinstance(value, str) or len(value) > MAX_CELL_CHARS:
        return None
    if not looks_serialized_collection(value):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _canonical(parsed) -> str:
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_cells(cells) -> list | None:
    """Each cell read as JSON (``None`` for NULL) when every non-null cell is
    a JSON object or list and at least one is not NULL, else ``None``."""
    out, seen = [], False
    for cell in cells:
        if cell is None:
            out.append(None)
            continue
        parsed = _parse(cell)
        if parsed is None:
            return None
        out.append(parsed)
        seen = True
    return out if seen else None


def _container(values: list) -> str | None:
    """``"object"`` when every value is a JSON object, ``"list"`` when every
    value is a list whose every element is an object and some element exists,
    else ``None`` (a list of scalars, mixed containers): only these two have
    fields to group by."""
    if all(isinstance(v, dict) for v in values):
        return "object"
    if all(isinstance(v, list) and all(isinstance(e, dict) for e in v) for v in values) \
            and any(values):
        return "list"
    return None


def _elements(values: list, container: str) -> list:
    if container == "object":
        return list(values)
    return [e for v in values for e in v]


def _field_text(value) -> str | None:
    """How ``json_extract_scalar`` returns a field's value: a string as it is,
    a number or a boolean as its JSON text, ``None`` for a null or a nested
    value (which it cannot return)."""
    if value is None or isinstance(value, (dict, list)):
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _identifies(key: str, objects: list, canonical: list) -> bool:
    """True when ``key`` is a scalar in every object and its values map one
    to one to the objects' canonical texts."""
    seen: dict = {}
    for item, text in zip(objects, canonical):
        value = _field_text(item.get(key))
        if value is None or seen.setdefault(value, text) != text:
            return False
    return True


def _path_segment(key) -> bool:
    """True when ``$.<key>`` addresses the top-level key ``key`` itself and
    the key may be printed (``engine_names.printable``)."""
    from .engine_names import printable
    return (isinstance(key, str) and _PATH_SEGMENT.fullmatch(key) is not None
            and printable(key))


def _identifying_field(objects: list) -> str:
    """The field to group by: a key that is one plain, printable JSONPath
    segment (:func:`_path_segment`) and identifies every observed item
    (:func:`_identifies`), ``id`` first, else ``""``."""
    canonical = [_canonical(item) for item in objects]
    found = [key for key in sorted({str(k) for item in objects for k in item})
             if _path_segment(key) and _identifies(key, objects, canonical)]
    if not found:
        return ""
    return _PREFERRED_KEY if _PREFERRED_KEY in found else found[0]


def _keyed_cells(rows, positions) -> dict:
    """``{position: (container, values)}`` for the grouped positions whose
    cells are JSON objects, or lists of objects, throughout."""
    out = {}
    for position in positions:
        cells = _json_cells([r[position] for r in rows])
        container = _container([v for v in cells if v is not None]) if cells else None
        if container is not None:
            out[position] = (container, cells)
    return out


def _rows_fit(columns, rows, width: int) -> bool:
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        return False
    if isinstance(columns, list) and columns and len(columns) != width:
        return False
    return all(isinstance(r, (list, tuple)) and len(r) == width for r in rows)


def _merged(rows, keys, parsed) -> dict:
    """``{value: {spelling: first 1-based row}}``: each row's key tuple with
    every JSON cell read as JSON (the value) and as written (the spelling)."""
    canonical = {i: [None if v is None else _canonical(v) for v in cells]
                 for i, (_container_kind, cells) in parsed.items()}
    merged: dict = {}
    for n, row in enumerate(rows):
        value = tuple(canonical[i][n] if i in canonical else row[i] for i in keys)
        merged.setdefault(value, {}).setdefault(tuple(row[i] for i in keys), n + 1)
    return merged


def _shape_of(container: str, values: list) -> dict:
    """The container, element keys, identifying field, longest list and
    whether an empty list occurs, over one column's parsed values."""
    objects = _elements(values, container)
    return {"container": container,
            "element_keys": tuple(sorted({str(k) for o in objects for k in o})),
            "field": _identifying_field(objects),
            "longest": max((len(v) for v in values if isinstance(v, list)), default=0),
            "empty_lists": any(isinstance(v, list) and not v for v in values)}


def _whole_number(cell) -> int | None:
    """A count cell as an int, else ``None``."""
    text = str(cell).strip() if isinstance(cell, (str, int)) and not isinstance(cell, bool) else ""
    return int(text) if text.isdigit() else None


def _narrowed(sql: str) -> bool:
    """A HAVING, or a second SELECT (a CTE, a derived table, a subquery):
    the shown groups may be fewer than the WHERE filters select."""
    from .athena_scope import tokenize
    tokens, _uncertain = tokenize(sql)
    words = [t.value.upper() for t in tokens if t.kind == "word"]
    return "HAVING" in words or words.count("SELECT") > 1


def _empty_lists(sql, shape, columns, rows, keys, slot, cells, complete: bool) -> dict:
    """The rows whose list (``cells``, the column at ``keys[slot]``) is empty
    or NULL, and, when the query has exactly one ``COUNT(*)`` output and
    scans one table with no list expansion of its own, the records they count
    and those rows merged across spellings by the other grouped values."""
    from .grouped_keys import row_counts
    empty = [n for n, v in enumerate(cells) if v is None or (isinstance(v, list) and not v)]
    narrowed = _narrowed(sql)
    out = {"empty_rows": len(empty), "complete": bool(complete) and not narrowed}
    if not empty:
        return out
    nulls = sum(cells[n] is None for n in empty)
    out["empty_kind"] = "null" if nulls == len(empty) else "both" if nulls else "empty"
    counts = row_counts(sql)
    labelled = isinstance(columns, list) and len(columns) == shape.width
    if len(counts) != 1 or not shape.table or "UNNEST" in sql.upper() or not labelled:
        return out
    count = counts[0]
    others = [p for i, p in enumerate(keys) if i != slot]
    merged: dict = {}
    total = 0
    for n in empty:
        value = _whole_number(rows[n][count])
        if value is None:
            return out
        row = rows[n]
        key = tuple(_merge_key(row[p]) for p in others)
        entry = merged.setdefault(key, [[row[p] for p in others], 0])
        entry[1] += value
        total += value
    labels = tuple(str(columns[p]) for p in others) + (str(columns[count]),)
    table = tuple(tuple(shown) + (str(n),) for shown, n in merged.values())
    out.update(empty_records=total, empty_table=(labels, table))
    return out


def _merge_key(cell):
    """A grouped value as one key: a JSON cell read as JSON, any other as written."""
    parsed = _parse(cell)
    return _canonical(parsed) if parsed is not None else cell


def find(sql, columns, rows, *, complete: bool = True) -> JsonSpellings | None:
    """The JSON values ``rows`` holds in several spellings, as one
    :class:`JsonSpellings`, else ``None`` (see the module docstring).
    ``columns`` are the result's column labels; ``complete`` says the result
    holds every row of its query (``empty_lists.complete_result``)."""
    from .grouped_keys import read
    shape = read(sql)
    if shape is None or not shape.keys or not _rows_fit(columns, rows, shape.width):
        return None
    keys = [position for position, _column in shape.keys]
    parsed = _keyed_cells(rows, keys)
    if not parsed:
        return None
    merged = _merged(rows, keys, parsed)
    split = [spellings for spellings in merged.values() if len(spellings) > 1]
    if not split:
        return None
    first = split[0]
    slot = next(p for p, i in enumerate(keys)
                if i in parsed and len({text[p] for text in first}) > 1)
    column = shape.keys[slot][1]
    container, cells = parsed[keys[slot]]
    label = columns[keys[slot]] if isinstance(columns, list) and columns else ""
    empty = (_empty_lists(sql, shape, columns, rows, keys, slot, cells, complete)
             if container == "list" else {"complete": bool(complete)})
    return JsonSpellings(
        table=shape.table,
        column=column if column is not None else str(label or ""),
        plain=column is not None,
        groups=len(rows), values=len(merged), split=len(split),
        example_rows=tuple(sorted(first.values())[:2]),
        **_shape_of(container, [v for v in cells if v is not None]), **empty)


# --------------------------------------------------------------------------- #
# The refusal and the disclosure: facts, names only when printable
# --------------------------------------------------------------------------- #
def _keys_text(names, found: JsonSpellings) -> str:
    shown = names.keep(found.element_keys[:MAX_KEYS_SHOWN], "field")
    more = len(found.element_keys) - MAX_KEYS_SHOWN
    if not found.element_keys:
        return ""
    if not shown:
        return "keys whose names are withheld"
    text = ", ".join(shown)
    return f"{text} and {more} more" if more > 0 else text


def _field_choice(names, found: JsonSpellings) -> tuple[str, str]:
    """``(the field in the SQL form, the sentences that say why)``: the field
    that identifies every observed item (``found.field``), else a placeholder,
    the candidate fields, and that one identifying the item must be picked."""
    from .engine_names import printable
    field = found.field if _path_segment(found.field) and printable(found.field) else ""
    item = "value" if found.container == "object" else "element"
    keys = _keys_text(names, found)
    seen = f" The {item}s carry the keys {keys}." if keys else ""
    if field:
        return field, (f"{seen} In this result {field} is in every {item} and has one "
                       "value per item.")
    return _KEY_PLACEHOLDER, (f"{seen} In this result no field named only by letters, digits "
                              f"and underscores is in every {item} with one value per item, so "
                              "pick a field that identifies the item for "
                              f"{_KEY_PLACEHOLDER}.")


def _remedy(names, found: JsonSpellings) -> str:
    """The SQL form that groups by a field inside the value, and how it counts.
    The column is printed in the SQL only when it is one plain column
    reference; otherwise ``col`` stands in and a sentence says for what."""
    from .engine_names import printable
    stand_in = ""
    if found.plain and printable(found.column):
        column = f'"{found.column}"'
    else:
        column = _COLUMN_PLACEHOLDER
        what = ("the grouped column, whose name is withheld" if found.plain
                else "the column the grouped expression reads")
        stand_in = f" Here {column} is {what}."
        if found.plain:
            names.show(found.column)
    key, why = _field_choice(names, found)
    if found.container == "object":
        return (f"Group by a field inside the value instead: GROUP BY "
                f"{OBJECT_FIELD.format(column=column, key=key)}.{why}{stand_in}")
    expansion = EXPANSION.format(column=column)
    element = ELEMENT.format(key=key)
    plural = "element" if found.longest == 1 else "elements"
    return (f"Group by a field inside the value instead: {expansion} gives one row per "
            f"element of the list, then GROUP BY {element}.{why}{stand_in} "
            "Counted that way, a row counts once per element: a row whose list holds two "
            f"elements counts twice (the longest list here holds {found.longest} {plural}), "
            "and a row whose list is empty or NULL has no element and drops out, so count "
            f"those rows on their own with {EMPTY_COUNT.format(column=column)}, which counts "
            f"every spelling of an empty list and a NULL one.{_empty_sentence(names, found)}")


def _empty_sentence(names, found: JsonSpellings) -> str:
    """What this result holds of those rows: how many, and, when the query
    counts its records, how many records the expanded counts leave out."""
    if not found.empty_rows:
        return ""
    held = {"empty": "an empty list", "null": "a NULL list"}.get(found.empty_kind,
                                                                  "an empty or NULL list")
    one = found.empty_rows == 1
    rows = (f" In this result {found.empty_rows} of the {found.groups} rows "
            f"{'holds' if one else 'hold'} {held}")
    if found.empty_records is None:
        return (f"{rows}; the expanded counts leave their records out, so count them as "
                "above and report them beside the per-element counts.")
    count = names.show(found.empty_table[0][-1]) if found.empty_table else "count"
    total = found.empty_records
    bound = "" if found.complete else "at least "
    return (f"{rows} and {'its' if one else 'their'} {count} "
            f"{'is' if one else 'adds up to'} {total}: {bound}{total} "
            f"record{'' if total == 1 else 's'} under this query's filters "
            f"{'has' if total == 1 else 'have'} no element, so the expanded counts leave "
            "them out; they are in empty_list_rows, report them beside the per-element counts.")


def _spelling_fact(names, found: JsonSpellings) -> str:
    """The split in a result that ran, as facts: how many values came in more
    than one group and what the groups were counted by."""
    column = names.show(found.column)
    were = "was" if found.split == 1 else "were"
    return (f"{found.split} of the {found.values} values grouped by {column} {were} the "
            "same JSON value as another group, stored with its keys in another order, so "
            "each such value was counted once per spelling")


class JsonSpellingsError(StorageBoundError):
    """A grouped result withheld because its key column holds one JSON value
    in several spellings (reason :data:`REASON`). It rides the refusal path
    the window rules use, so ``athena_tools._tool_error`` returns
    :meth:`to_dict` as it does for theirs."""

    def __init__(self, found: JsonSpellings):
        super().__init__(StorageBound(found.table, (), (), ()), (), (), reason=REASON)
        self.found = found

    def to_dict(self) -> dict:
        from .engine_names import Withheld
        f, names = self.found, Withheld()
        column = names.show(f.column)
        first, second = (f.example_rows + (0, 0))[:2]
        what = (f"Rows withheld: this result's {f.groups} rows, grouped by {column}, hold "
                f"{f.values} JSON {'value' if f.values == 1 else 'values'}, and {f.split} of them "
                f"{'appears' if f.split == 1 else 'appear'} in more than one "
                "row, the same value stored with its keys in another order (for example "
                f"rows {first} and {second}). GROUP BY or DISTINCT on the text keeps such a "
                "value once per spelling, so one value shows up as several rows and a count "
                "of it is split between them.")
        again = (" If the question asks for the stored text itself, send the same query "
                 "again unchanged: it runs, and the answer says the groups are spellings "
                 "of fewer values.")
        guidance = f"{what} {_remedy(names, f)}{again}"
        record = f.to_dict(names)
        out = {
            "error": "json_spellings",
            "error_type": "invalid_request",
            "error_name": ERROR_NAME,
            "reason": REASON,
            "retryable": True,
            **record,
            "guidance": guidance + names.sentence(),
        }
        if names.count:
            out["names_withheld"] = names.count
        return out


def _distinct(found) -> list:
    out = []
    for item in found or ():
        if isinstance(item, JsonSpellings) and item not in out:
            out.append(item)
    return out[:_MAX_SHOWN]


def engine_line(found) -> str:
    """One bounded engine line, as facts, for the results of this call's
    queries that held JSON values in several spellings, ``""`` for none."""
    from .engine_names import Withheld
    shown = _distinct(found)
    if not shown:
        return ""
    names = Withheld()
    parts = [(f"in {names.show(f.table, 'table')}, " if f.table else "") + _spelling_fact(names, f)
             for f in shown]
    return (f"{ENGINE_PREFIX} check, in this turn's queries: "
            f"{'; '.join(parts)}. Each is a result {MARKER}.{names.sentence()}")


def requirement_block(found) -> str:
    """The FINAL-ANSWER REQUIREMENTS block for these results, as facts, ``""``
    for none."""
    from .engine_names import Withheld
    shown = _distinct(found)
    if not shown:
        return ""
    from .guidance_scope import (GuidanceScope, append_note, empty_document,
                                 render_orchestrator_sections)
    names = Withheld()
    notes = []
    for f in shown:
        column = names.show(f.column)
        notes.append(f"REQUIRED: the answer must say that its figures grouped by {column} "
                     "count each JSON value once per spelling, so "
                     f"{f.split} of its {f.values} values "
                     f"{'appears' if f.split == 1 else 'appear'} in more than one group.")
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
