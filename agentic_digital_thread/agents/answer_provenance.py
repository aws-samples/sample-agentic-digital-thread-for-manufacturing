# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Engine provenance that reaches the FINAL answer, not only the system's.

Measured on an analytics deployment: the connector computed an exact count
from partition pieces at one table snapshot (``athena_exact_split``), the
system agent's answer carried the engine's executed-filters line saying so,
and the orchestrator's final answer said the figure was "computed directly"
and never mentioned the pieces. A prompt rule asking for the disclosure was
not enough, so the disclosure is now carried by the engine, in two hops:

1. **Into the system agent's answer** (``system_agent._append_engine_facts``,
   the same seam as the cut, grain and executed-filters lines). The capture
   hook keeps, per executed query, the split's lineage and the figures it
   computed (:func:`split_record`: the values of its aggregate columns). When
   the system agent's own prose states one of those figures,
   :func:`split_lines` adds ONE fixed line naming the figures as written in
   the prose, the piece count, the table and the one snapshot. Model text that
   reads as that line is made inert first (:func:`neutralize`).
2. **Into the orchestrator's final answer** (``answer_provenance_stream``,
   beside the runtime's deterministic chart injection). The orchestrator's
   tool results are read for those lines (:func:`facts_in_tool_text`; a raw
   connector result carrying ``exact_split`` counts too), and each fact is
   bound to its source: the tool result's citation ordinal (its trailing
   ``[engine] Cite this result as [n].`` stamp, else the client's own count)
   and its system. :func:`final_lines` gives the provenance sentence, placed
   before the answer's control blocks so the answer finalizer keeps it, only
   when the final answer states a tagged figure where it attributes it to
   that source (:func:`attributed_figures`): in a sentence (a clause after a
   semicolon counts as its own) that cites that ordinal, or, with no
   citation, names that system and no other system of the turn, or, with
   neither, in an answer whose citations and system names all point at that
   one system and at that fact's own result (its ordinal among the answer's
   citations, or, with no citation, the system's only cited result this turn;
   :func:`_only_source_carries`). A figure that only equals a split figure,
   attributed elsewhere or in an answer with two or more sources or results
   it could mean, gets no sentence.

A figure matches when a number written in the text equals it at the precision
the text shows it (``8,276.6`` matches ``8276.58``; ``8.3 million`` matches
nothing). A number inside a word, a date, a time or a ``[n]`` citation is not
read. The lines name no figure the text did not already state and add no
number of their own except the piece count.

A query that ran with a bound on a storage (partition) column
(``storage_bound_rule``: a non-temporal storage column, a time storage
column with no other time bound, or a Hive table) travels the same two hops:
the connector labels the result (``storage_bound``), :func:`storage_lines`
gives the system agent's ``[engine] Storage bound ran:`` line naming the
result's figures the answer states (none stated, or the delegated question
names the column: no line), and :func:`final_lines` discloses it, naming the
column and its range, under the same attribution rule.

A result an approximate aggregate computed (``approx_percentile`` and the
rest of ``approximate_aggregates.APPROXIMATE_FUNCTIONS``) travels them too:
the guarded tool labels it (``approximate``: the functions and the result
columns that use one), :func:`approximate_lines` gives the system agent's
``[engine] Approximate:`` line naming those columns' figures the answer
states, leaving out a figure that a later successful data result of the same
call also holds; when none is stated as written (a converted value, a
label whose columns could not be read, or only other numbers), a neutral
line says the query also computed approximate values with that function.
In the final answer an approximate fact is decided only by the sentences
that cite its own result ordinal: the figure sentence for its figures they
state as written, else the neutral sentence; an uncited result, or a
sentence citing only another result, gets nothing. A JSON tool result is
never scanned for these lines: its approximate facts come from its
top-level label or the system agent's engine-owned note. An approximate
exact split (a percentile merged from qdigest pieces) says so in its own
line and sentence.

A query whose figures were computed on rows a top-level text filter chose
(``kind = 'X'`` or ``kind IN (...)``) travels them as well, when the final
answer could misname that population: the capture keeps, per executed query,
its top-level text pins, its figures and the code-like cells it returned
(:func:`population_record`); :func:`population_lines` gives the system
agent's ``[engine] Filtered rows:`` line for a query whose figures the answer
states, naming the pin and the other code-like values of that column the call
read (other queries' pins and returned cells; a value like ``STEP_A_ROUTE``
that folds to two or more words, never a single word such as ``DONE``).
In the final answer, :func:`final_lines` adds ONE fixed sentence per such
fact when the text stating its figures (a sentence, or a table row and its
own cells; the paragraph above the table only when it is about that one
value, :func:`_paragraph_value`) names one of those other values outside a
code span, as a whole (``step a route`` and ``Step-A-Route`` name
``STEP_A_ROUTE``; ``STEP_A_ROUTE_EXTRA`` does not), and no figure
that text states comes from rows pinned to that value
(:func:`_population_final`). The sentence states the pin and where the
other value is named (that text, or the paragraph above its table), never
what the answer means by it. It is a disclosure, never a refusal.

Scope: both hops run in the report-only and annotate modes only. Under
``RESPONSE_INTEGRITY_MODE=enforce`` the answer is the server-built typed
envelope: ``system_agent._append_engine_facts`` adds no line and
``answer_provenance_stream.wrap_stream`` does not wrap the stream, so no
provenance sentence is added.
Stdlib only, pure.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

#: The system agent's engine line for an exact-split figure.
SPLIT_PREFIX = "[engine] Exact split: "
#: The final answer's provenance sentence.
ANSWER_PREFIX = "How this was computed: "
#: Figures read from one split result, figures named in one line, facts kept
#: from one turn's tool results.
MAX_FIGURES_READ = 200
MAX_FIGURES_SHOWN = 8
MAX_FACTS = 6
_TABLE_FALLBACK = "its table"
_TABLE_RE = re.compile(r"[A-Za-z0-9_$.\-]{1,160}")
_SYSTEM_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
#: A number as prose writes it: optional sign, thousands groups, decimals.
#: Not inside a word or an identifier, a date (``2026-01-05``), a time
#: (``06:30``), a path or a ``[n]`` citation.
_NUMBER_RE = re.compile(
    r"(?<![\w.\-/:\[#])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w\-/:\]]|\.\d|,\d{3})")
#: A result cell that is a number (the engine renders numbers as strings).
_CELL_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_MAX_NUMBER_CHARS = 40
#: The clause an approximate split's line and sentence end with.
_SPLIT_APPROXIMATE = "; its percentile figures are approximate (qdigest), not exact"
_SPLIT_RE = re.compile(
    re.escape(SPLIT_PREFIX)
    + r"(?P<figures>-?[0-9][0-9,.; \-]{0,300}?) in this answer (?:is|are) from one query "
    r"result the connector computed from (?P<pieces>[0-9][0-9,]{0,9}) partition pieces of "
    r"(?P<table>[A-Za-z0-9_$.\-]{1,160}|" + _TABLE_FALLBACK + r"), all read at one table "
    r"snapshot(?P<approximate>" + re.escape(_SPLIT_APPROXIMATE) + r")?\.")


# --------------------------------------------------------------------------- #
# Numbers and figures
# --------------------------------------------------------------------------- #
def _decimal(text) -> Decimal | None:
    raw = str(text).replace(",", "").strip()
    if not raw or len(raw) > _MAX_NUMBER_CHARS:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def numbers_in(text) -> list[tuple[str, Decimal, int]]:
    """``(spelling, value, decimal places)`` of every number ``text`` writes."""
    out = []
    for match in _NUMBER_RE.finditer(text if isinstance(text, str) else ""):
        spelling = match.group(0)
        value = _decimal(spelling)
        if value is not None:
            places = len(spelling.split(".", 1)[1]) if "." in spelling else 0
            out.append((spelling, value, places))
    return out


def _matches(figure: Decimal, shown: Decimal, places: int) -> bool:
    """``shown`` is ``figure`` written to ``places`` decimal places."""
    return abs(figure - shown) * 2 <= Decimal(1).scaleb(-places)


def figures_used(figures, text) -> list[str]:
    """The spellings in ``text`` of numbers equal to one of ``figures``, in
    text order, each once, at most :data:`MAX_FIGURES_SHOWN`."""
    values = [v for v in (_decimal(f) for f in (figures or ())[:MAX_FIGURES_READ])
              if v is not None]
    if not values:
        return []
    used: list[str] = []
    for spelling, shown, places in numbers_in(text):
        if spelling not in used and any(_matches(v, shown, places) for v in values):
            used.append(spelling)
            if len(used) == MAX_FIGURES_SHOWN:
                break
    return used


# --------------------------------------------------------------------------- #
# The split's own figures (system side)
# --------------------------------------------------------------------------- #
def _aggregate_indexes(sql, width: int) -> list[int]:
    """Result columns that are aggregates, read with the split's own shape
    parser; every column when the executed SQL does not parse."""
    try:
        from .athena_exact_shape import parse_shape
        shape = parse_shape(sql)
        indexes = [i for i, out in enumerate(shape.outputs) if out.kind == "agg"]
        return [i for i in indexes if i < width] or list(range(width))
    except Exception:  # noqa: BLE001 - a figure list is best-effort
        return list(range(width))


def split_record(payload) -> dict | None:
    """``{"pieces", "table", "figures"}`` for a connector result an exact
    split computed (``exact_split.applied``), else ``None``. ``figures`` are
    the numeric cells of its aggregate columns as the engine rendered them;
    ``approximate`` is set for a split that merged an approximate percentile
    (``exact_split.approximate``)."""
    if not isinstance(payload, dict):
        return None
    split = payload.get("exact_split")
    if not (isinstance(split, dict) and split.get("applied") is True):
        return None
    pieces = split.get("pieces")
    if isinstance(pieces, bool) or not isinstance(pieces, int) or pieces < 1:
        return None
    table = str(split.get("table") or "")
    record = {"pieces": pieces, "table": table if _shown_table(table) else "",
              "figures": _result_figures(payload)}
    if split.get("approximate") is True:
        record["approximate"] = True
    return record


def _result_figures(payload: dict) -> list[str]:
    """The numeric cells of a connector result's aggregate columns, as the
    engine rendered them (every column when the executed SQL does not parse)."""
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    width = max((len(r) for r in rows if isinstance(r, list)), default=0)
    return _cells(payload, _aggregate_indexes(payload.get("_adt_executed_query"), width))


def _cells(payload: dict, indexes) -> list[str]:
    """The numeric cells of a connector result's columns at ``indexes``, row
    by row, as the engine rendered them, at most :data:`MAX_FIGURES_READ`."""
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    figures: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        for index in indexes:
            cell = row[index] if index < len(row) else None
            text = str(cell) if isinstance(cell, (str, int, float)) and not isinstance(cell, bool) else ""
            if _CELL_RE.fullmatch(text) and len(figures) < MAX_FIGURES_READ:
                figures.append(text)
    return figures


def _shown_table(table: str) -> bool:
    """A table name an engine line or sentence may print: the line's own
    shape, and a plain identifier (``engine_names.printable``)."""
    from .engine_names import printable
    return bool(_TABLE_RE.fullmatch(table)) and printable(table)


def split_line(record, answer) -> str:
    """The engine line for one split record whose figures ``answer`` states,
    else ``""``."""
    if not isinstance(record, dict):
        return ""
    used = figures_used(record.get("figures"), answer)
    pieces = record.get("pieces")
    if not used or isinstance(pieces, bool) or not isinstance(pieces, int) or pieces < 1:
        return ""
    table = str(record.get("table") or "")
    table = table if _shown_table(table) else _TABLE_FALLBACK
    verb = "is" if len(used) == 1 else "are"
    approximate = _SPLIT_APPROXIMATE if record.get("approximate") is True else ""
    return (f"{SPLIT_PREFIX}{'; '.join(used)} in this answer {verb} from one query result "
            f"the connector computed from {pieces:,} partition pieces of {table}, all read "
            f"at one table snapshot{approximate}.")


def split_lines(records, answer) -> list[str]:
    """One line per distinct split record whose figures ``answer`` states."""
    lines: list[str] = []
    for record in records or ():
        line = split_line(record, answer)
        if line and line not in lines:
            lines.append(line)
    return lines[:MAX_FACTS]


def neutralize(text):
    """``text`` with any copy of the engine's split, storage-bound,
    approximate or filtered-rows prefix made inert, so a model-written or
    data-carried line never reads as the engine's."""
    if not isinstance(text, str):
        return text
    for prefix in (SPLIT_PREFIX, STORAGE_PREFIX, APPROXIMATE_PREFIX, POPULATION_PREFIX):
        if prefix in text:
            text = text.replace(prefix, prefix[len("[engine] "):])
    return text


# --------------------------------------------------------------------------- #
# A storage-column bound that ran (system side)
# --------------------------------------------------------------------------- #
#: The system agent's engine line for a query that ran with a bound on a
#: storage partition column (``storage_bound_rule``: no other time column).
STORAGE_PREFIX = "[engine] Storage bound ran: "
_CONJUNCT_CHARS = r"A-Za-z0-9_ .:'()<>=!,+\-*/%"
_CONJUNCT_UNSAFE = re.compile(f"[^{_CONJUNCT_CHARS}]")
_COLUMN_RE = re.compile(r"[A-Za-z0-9_]{1,128}")
_MAX_CONJUNCT_TEXT = 300
_STORAGE_RE = re.compile(
    re.escape(STORAGE_PREFIX)
    + r"(?P<figures>-?[0-9][0-9,.; \-]{0,300}?) in this answer (?:is|are) from "
    r"(?P<table>[A-Za-z0-9_$.\-]{1,160}) rows limited to "
    f"(?P<conjuncts>[{_CONJUNCT_CHARS}]{{1,{_MAX_CONJUNCT_TEXT}}}?), a bound on its storage "
    r"partition column (?P<columns>[A-Za-z0-9_, ]{1,300})\.")


def storage_record(payload) -> dict | None:
    """``{"table", "columns", "conjuncts", "figures", "withheld"}`` of the
    storage bound a connector result ran with (``storage_bound``), cleaned for
    an engine line, with the result's figures; else ``None``. Quotes are
    dropped and any character outside a plain SQL comparison is replaced, so
    the text cannot carry markup or a line. A table, column or conjunct that
    carries a name which is not a plain identifier (``engine_names``) is never
    kept: a column or conjunct is counted in ``withheld``, and a table
    withholds the record."""
    from .engine_names import printable, printable_sql
    bound = payload.get("storage_bound") if isinstance(payload, dict) else None
    if not isinstance(bound, dict):
        return None
    table = str(bound.get("table") or "")
    named = [str(c) for c in bound.get("columns") or () if str(c).strip()]
    columns = [c for c in named if _COLUMN_RE.fullmatch(c) and printable(c)]
    written = [str(c) for c in bound.get("conjuncts") or () if str(c).strip()]
    kept = [c for c in written if printable_sql(c)]
    conjuncts = [_CONJUNCT_UNSAFE.sub("?", c.replace('"', "")) for c in kept]
    text = " AND ".join(conjuncts)
    if not (_TABLE_RE.fullmatch(table) and printable(table) and columns and text):
        return None
    if len(text) > _MAX_CONJUNCT_TEXT:
        text = text[:_MAX_CONJUNCT_TEXT - 3] + "..."
    return {"table": table, "columns": columns, "conjuncts": text,
            "figures": _result_figures(payload),
            "withheld": (len(named) - len(columns)) + (len(written) - len(kept))}


def storage_lines(records, answer, question: str = "") -> list[str]:
    """One engine line per distinct storage bound whose result's figures
    ``answer`` states, unless the delegated question names every bounded
    column (then the bound is the one it asked for)."""
    from .engine_names import count_sentence
    from .storage_bounds import _asked
    lines: list[str] = []
    for record in records or ():
        if not isinstance(record, dict) or all(_asked(question, c) for c in record["columns"]):
            continue
        used = figures_used(record.get("figures"), answer)
        if not used:
            continue
        verb = "is" if len(used) == 1 else "are"
        line = (f"{STORAGE_PREFIX}{'; '.join(used)} in this answer {verb} from "
                f"{record['table']} rows limited to {record['conjuncts']}, a bound on its "
                f"storage partition column {', '.join(record['columns'])}."
                f"{count_sentence(record.get('withheld') or 0)}")
        if line not in lines:
            lines.append(line)
    return lines[:MAX_FACTS]


# --------------------------------------------------------------------------- #
# Figures an approximate aggregate computed (system side)
# --------------------------------------------------------------------------- #
#: The system agent's engine line for figures an approximate aggregate
#: computed (``approximate_aggregates``).
APPROXIMATE_PREFIX = "[engine] Approximate: "
_FUNCTION_NAME = r"[a-z_]{1,40}"
_FUNCTION_LIST = (r"(?P<functions>" + _FUNCTION_NAME + r"(?:(?:, | and )" + _FUNCTION_NAME
                  + r"){0,8})")
_APPROXIMATE_RE = re.compile(
    re.escape(APPROXIMATE_PREFIX)
    + r"(?P<figures>-?[0-9][0-9,.; \-]{0,300}?) in this answer (?:is an estimate|are "
    r"estimates) of the same measure from " + _FUNCTION_LIST
    + r", not (?:an exact value|exact values)\.")
#: The neutral line for an approximate result none of whose figures is
#: stated as written: it says what the query computed, not what the answer
#: used.
_NEUTRAL_HEAD = "this system's query also computed approximate values with "
_NEUTRAL_TAIL = "; any value taken or converted from those outputs is an estimate."
_APPROXIMATE_QUERY_RE = re.compile(
    re.escape(APPROXIMATE_PREFIX + _NEUTRAL_HEAD) + _FUNCTION_LIST + re.escape(_NEUTRAL_TAIL))


def _approximate_functions(names) -> list[str]:
    """``names`` that are approximate aggregates, each once, in order."""
    from .approximate_aggregates import APPROXIMATE_FUNCTIONS
    out: list[str] = []
    for name in names or ():
        if isinstance(name, str) and name in APPROXIMATE_FUNCTIONS and name not in out:
            out.append(name)
    return out


def approximate_record(payload) -> dict | None:
    """``{"functions", "figures"}`` for a result the guarded tool labelled
    ``approximate`` that has rows, else ``None``: the approximate aggregates
    it was computed with and the numeric cells of the columns that use one,
    as the engine rendered them (none when the label names no column). A
    result with no rows computed no value."""
    found = payload.get("approximate") if isinstance(payload, dict) else None
    if not isinstance(found, dict) or not payload.get("rows"):
        return None
    functions = _approximate_functions(found.get("functions"))
    if not functions:
        return None
    indexes = [i for i in found.get("column_indexes") or ()
               if isinstance(i, int) and not isinstance(i, bool) and i >= 0]
    return {"functions": functions, "figures": _cells(payload, indexes)}


def _approximate_claim(used: list, functions) -> str:
    from .approximate_aggregates import joined
    if len(used) == 1:
        return (f"is an estimate of the same measure from {joined(functions)}, not an exact "
                "value.")
    return f"are estimates of the same measure from {joined(functions)}, not exact values."


def result_cells(payload) -> list[str]:
    """The numeric cells of every column of a connector result, row by row,
    as the engine rendered them, at most :data:`MAX_FIGURES_READ`."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    width = max((len(r) for r in rows if isinstance(r, list)), default=0)
    return _cells(payload, range(width))


def _shadowed(spelling: str, values) -> bool:
    """Whether ``values`` (figures of a result that is not approximate) hold
    ``spelling`` at the precision it is written."""
    return bool(figures_used(list(values or ())[:MAX_FIGURES_READ], spelling))


def approximate_lines(entries, answer) -> list[str]:
    """The engine lines for this call's approximate results
    (``tool_result_capture.collect_approximate_results``: in call order,
    ``approximate`` entries and the ``shadow`` cells of each later successful
    data result).

    Per approximate entry: one line naming its figures ``answer`` states as
    written, leaving out any a later data result of the call also holds;
    else the neutral line naming the function, whatever other numbers the
    answer holds."""
    entries = [e for e in entries or () if isinstance(e, dict)]
    lines: list[str] = []
    for index, entry in enumerate(entries):
        functions = (_approximate_functions(entry.get("functions"))
                     if entry.get("kind") == "approximate" else [])
        if not functions:
            continue
        later = [e.get("figures") for e in entries[index + 1:] if e.get("kind") == "shadow"]
        used = [f for f in figures_used(entry.get("figures"), answer)
                if not any(_shadowed(f, cells) for cells in later)]
        line = (f"{APPROXIMATE_PREFIX}{'; '.join(used)} in this answer "
                f"{_approximate_claim(used, functions)}" if used
                else _neutral_line(functions))
        if line not in lines:
            lines.append(line)
    return lines[:MAX_FACTS]


def _neutral_line(functions) -> str:
    from .approximate_aggregates import joined
    return f"{APPROXIMATE_PREFIX}{_NEUTRAL_HEAD}{joined(functions)}{_NEUTRAL_TAIL}"


# --------------------------------------------------------------------------- #
# The population a query's text filter chose (system side)
# --------------------------------------------------------------------------- #
#: The system agent's engine line for figures computed on rows a top-level
#: text filter chose, when the call read other code-like values of that column.
POPULATION_PREFIX = "[engine] Filtered rows: "
#: A code-like value: two or more letter words (each may end in digits)
#: joined by ``_`` or ``-``, such as ``STEP_A_ROUTE``. A single word, a date
#: or an id with mixed letters and digits is not one.
_CODE_RE = re.compile(r"[A-Za-z]+[0-9]*(?:[_-][A-Za-z]+[0-9]*)+")
_MAX_CODE_CHARS = 64
#: A filter value an engine line may print in quotes.
_PIN_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.&/\-]{0,63}")
#: Bounds on one record and on one line.
_MAX_PIN_COLUMNS = 8
_MAX_PIN_VALUES = 8
_MAX_CELL_COLUMNS = 16
_MAX_CELL_VALUES = 16
_MAX_OTHERS_SHOWN = 8
_COLUMN_WITHHELD = "a column whose name is withheld"
_PIN_NAME = r"(?:[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*|" + \
    re.escape(_COLUMN_WITHHELD) + r")"
_PIN_VALUE = r"[A-Za-z0-9][A-Za-z0-9 _.&/\-]{0,63}"
_CODE = r"[A-Za-z]+[0-9]*(?:[_-][A-Za-z]+[0-9]*)+"
_POPULATION_RE = re.compile(
    re.escape(POPULATION_PREFIX)
    + r"(?P<figures>-?[0-9][0-9,.; \-]{0,300}?) in this answer (?:is|are) from rows with "
    r"(?P<column>" + _PIN_NAME + r") (?:= '(?P<one>" + _PIN_VALUE + r")'|IN \((?P<many>'"
    + _PIN_VALUE + r"'(?:, '" + _PIN_VALUE + r"'){0,7})\)) only(?:, not from its other values "
    r"this call read: (?P<others>" + _CODE + r"(?:, " + _CODE + r"){0,7}))?\.")


def _code_like(value) -> bool:
    return (isinstance(value, str) and len(value) <= _MAX_CODE_CHARS
            and _CODE_RE.fullmatch(value) is not None)


def _pin_value_shown(value) -> bool:
    """A filter value an engine line or sentence may print in quotes."""
    from .untrusted_text import sanitize_untrusted, validate_untrusted
    return (isinstance(value, str) and _PIN_VALUE_RE.fullmatch(value) is not None
            and sanitize_untrusted(value) == value
            and validate_untrusted(value, max_len=_MAX_CODE_CHARS, field="value") is None)


def text_filters(sql) -> dict:
    """``{column: [values]}`` of the top-level AND conjuncts of a one-relation
    query's WHERE that pin a column to string literals with ``=`` or ``IN``
    (column names case folded, values sorted). A conjunct under OR or NOT,
    any other comparison, and any query the shared tokenizer cannot read or
    that scans more than one relation give nothing."""
    from .athena_scope import _where_expression, relations, tokenize
    from .state_times import _conjuncts, _pin
    if not isinstance(sql, str) or not sql.strip():
        return {}
    tokens, uncertain = tokenize(sql)
    if uncertain or not tokens:
        return {}
    scans, scan_uncertain = relations(tokens)
    if scan_uncertain or len(scans) != 1:
        return {}
    pins: dict = {}
    for part in _conjuncts(_where_expression(tokens, scans[0]) or []):
        if any(t.kind == "word" and t.value.upper() == "OR" for t in part):
            continue
        pin = _pin(part)
        if pin and len(pins) < _MAX_PIN_COLUMNS:
            column, values = pin
            pins[column] = set(values) if column not in pins else pins[column] & set(values)
    return {column: sorted(values)[:_MAX_PIN_VALUES] for column, values in pins.items()
            if values}


def _code_cells(payload: dict) -> dict:
    """``{column: [code-like values]}`` the result's text cells hold, column
    names case folded, distinct, bounded."""
    columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    cells: dict = {}
    for index, column in enumerate(columns[:64]):
        if not isinstance(column, str) or len(cells) >= _MAX_CELL_COLUMNS:
            continue
        found: list = []
        for row in rows:
            cell = row[index] if isinstance(row, list) and index < len(row) else None
            if _code_like(cell) and cell not in found:
                found.append(cell)
                if len(found) >= _MAX_CELL_VALUES:
                    break
        if found:
            cells[column.lower()] = found
    return cells


def population_record(payload) -> dict | None:
    """``{"filters", "figures", "cells"}`` of one successful connector data
    result: its executed query's top-level text pins (:func:`text_filters`),
    the numeric cells of its aggregate columns as the engine rendered them,
    and the code-like values each of its columns returned; ``None`` when it
    has neither a pin nor a code-like cell."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list) \
            or "error" in payload:
        return None
    filters = text_filters(payload.get("_adt_executed_query"))
    cells = _code_cells(payload)
    if not filters and not cells:
        return None
    return {"filters": filters, "figures": _result_figures(payload) if filters else [],
            "cells": cells}


def _pin_text(values) -> str:
    if len(values) == 1:
        return f"= '{values[0]}'"
    return "IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _telling(spelling: str) -> bool:
    """A figure as an answer writes it that can tie text to one result: not
    a single-digit whole number, which prose writes for anything."""
    value = _decimal(spelling)
    return value is not None and ("." in spelling or abs(value) >= 10)


def _left_out_elsewhere(records, index: int, column: str, values) -> bool:
    """Whether another record of the call pins ``column`` to values that
    leave out one of this record's code-like ``values``: that record's line
    names the value, and the final answer needs this one to see both."""
    mine = {v for v in values if _code_like(v)}
    return any(mine - set((other.get("filters") or {}).get(column) or mine)
               for i, other in enumerate(records) if i != index)


def population_lines(records, answer) -> list[str]:
    """The engine lines for this call's population records (in call order)
    whose figures ``answer`` states: one per pinned column whose call also
    read other code-like values of it (other records' pins and cells), or
    whose code-like pin another record of the call pins differently. A
    column name that is not a plain identifier is a placeholder; a pin value
    that cannot be printed gives no line."""
    from .engine_names import printable
    records = [r for r in records or () if isinstance(r, dict)]
    seen: dict = {}
    for record in records:
        for column, values in (record.get("filters") or {}).items():
            seen.setdefault(column, set()).update(v for v in values if _code_like(v))
        for column, values in (record.get("cells") or {}).items():
            seen.setdefault(column, set()).update(v for v in values if _code_like(v))
    lines: list[str] = []
    for index, record in enumerate(records):
        used = [f for f in figures_used(record.get("figures"), answer) if _telling(f)]
        if not used:
            continue
        for column, values in sorted((record.get("filters") or {}).items()):
            if not values or not all(_pin_value_shown(v) for v in values):
                continue
            others = sorted(seen.get(column, set()) - set(values))
            if not others and not _left_out_elsewhere(records, index, column, values):
                continue
            column_shown = column if printable(column) else _COLUMN_WITHHELD
            verb = "is" if len(used) == 1 else "are"
            line = (f"{POPULATION_PREFIX}{'; '.join(used)} in this answer {verb} from rows "
                    f"with {column_shown} {_pin_text(values)} only")
            if others:
                line += (", not from its other values this call read: "
                         + ", ".join(others[:_MAX_OTHERS_SHOWN]))
            line += "."
            if line not in lines:
                lines.append(line)
    return lines[:MAX_FACTS]


# --------------------------------------------------------------------------- #
# The orchestrator side
# --------------------------------------------------------------------------- #
#: The citation stamp the orchestrator's hook appends LAST to a cited tool
#: result (``citation_ordinals``); only a trailing one counts, so a copy inside
#: the result's own text never sets the ordinal.
_STAMP_RE = re.compile(r"\[engine\] Cite this result as \[(\d{1,4})\]\.\s*\Z")
#: A sentence, or a clause after a semicolon, of the final answer.
_UNIT_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+|;\s*")
_CITE_RE = re.compile(r"\[(\d{1,4}(?:\s*,\s*\d{1,4})*)\]")


def trailing_ordinal(text) -> int | None:
    """The citation ordinal a tool result's trailing stamp gives, else ``None``."""
    match = _STAMP_RE.search(text) if isinstance(text, str) else None
    return int(match.group(1)) if match else None


def _leading_json(text: str):
    """The JSON value ``text`` starts with (a citation stamp may follow it)."""
    return _leading_json_and_rest(text)[0]


def _leading_json_and_rest(text: str):
    """The JSON value ``text`` starts with and the text after it, or
    ``(None, text)`` when ``text`` does not start with one JSON value."""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None, text
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None, text
    return value, stripped[end:]


def _split_fact(system: str, pieces: int, table: str, figures: list, ordinal,
                approximate: bool = False) -> dict:
    return {"kind": "split", "system": system, "ordinal": ordinal, "pieces": pieces,
            "table": table, "figures": list(figures), "approximate": bool(approximate)}


def _storage_fact(system: str, table: str, columns: list, conjuncts: str, figures: list,
                  ordinal) -> dict:
    return {"kind": "storage", "system": system, "ordinal": ordinal, "table": table,
            "columns": list(columns), "conjuncts": conjuncts, "figures": list(figures)}


def _approximate_fact(system: str, functions: list, figures: list, ordinal) -> dict:
    return {"kind": "approximate", "system": system, "ordinal": ordinal,
            "functions": list(functions), "figures": list(figures)}


def _population_facts(text: str, system: str, ordinal) -> list[dict]:
    """The population facts of the system agent's ``[engine] Filtered rows:``
    lines in ``text``, read as :func:`_approximate_facts` reads its lines:
    never from the cells of a leading JSON value (only from its engine-owned
    note) and from all text outside one. An ``others`` value that is not
    code-like is dropped."""
    from .tool_result_capture import RESULT_CUT_NOTE_KEY
    payload, rest = _leading_json_and_rest(text)
    readable = [text] if payload is None else [rest]
    if isinstance(payload, dict) and isinstance(payload.get(RESULT_CUT_NOTE_KEY), str):
        readable.insert(0, payload[RESULT_CUT_NOTE_KEY])
    facts = []
    for match in (m for part in readable for m in _POPULATION_RE.finditer(part)):
        values = ([match.group("one")] if match.group("one") is not None
                  else re.findall(r"'(" + _PIN_VALUE + r")'", match.group("many") or ""))
        others = [v for v in (match.group("others") or "").split(", ") if _code_like(v)]
        facts.append({"kind": "population", "system": system, "ordinal": ordinal,
                      "figures": _figures_of(match.group("figures")),
                      "column": match.group("column"), "values": values, "others": others})
    return facts


def _figures_of(group: str) -> list[str]:
    return [f.strip() for f in group.split(";") if f.strip()]


def facts_in_tool_text(text, system: str = "", ordinal: int | None = None) -> list[dict]:
    """The split, storage, approximate and population facts one orchestrator
    tool result carries (the system agent's engine lines, or a raw connector
    result with ``exact_split``, ``storage_bound`` or ``approximate``), each
    bound to that result's ``ordinal`` and ``system``."""
    if not isinstance(text, str) or not text:
        return []
    system = system if _SYSTEM_RE.fullmatch(system or "") else ""
    facts: list[dict] = []
    for match in _SPLIT_RE.finditer(text):
        pieces = int(match.group("pieces").replace(",", ""))
        facts.append(_split_fact(system, pieces, match.group("table"),
                                 _figures_of(match.group("figures")), ordinal,
                                 bool(match.group("approximate"))))
    for match in _STORAGE_RE.finditer(text):
        columns = [c.strip() for c in match.group("columns").split(",") if c.strip()]
        facts.append(_storage_fact(system, match.group("table"), columns,
                                   match.group("conjuncts"),
                                   _figures_of(match.group("figures")), ordinal))
    payload = _leading_json(text)
    facts += _approximate_facts(text, system, ordinal)
    record = split_record(payload)
    if record and record["figures"]:
        facts.append(_split_fact(system, record["pieces"], record["table"] or _TABLE_FALLBACK,
                                 record["figures"], ordinal, bool(record.get("approximate"))))
    bound = storage_record(payload)
    if bound and bound["figures"]:
        facts.append(_storage_fact(system, bound["table"], bound["columns"], bound["conjuncts"],
                                   bound["figures"], ordinal))
    facts += _population_facts(text, system, ordinal)
    unique: list[dict] = []
    for fact in facts:
        if fact not in unique:
            unique.append(fact)
    return unique[:MAX_FACTS]


def _approximate_facts(text: str, system: str, ordinal) -> list[dict]:
    """The approximate facts of one tool result. The cells of a leading JSON
    value are data and are never scanned: a connector result gives its
    validated top-level ``approximate`` label, and a system agent's JSON
    answer the lines in its engine-owned top-level note
    (``tool_result_capture.RESULT_CUT_NOTE_KEY``). Text outside that value
    is read for the engine's approximate lines: all of a prose answer,
    including one that only opens with a brace and does not decode, and
    whatever follows a leading JSON value. ``_with_result_cut_fact`` appends
    its lines after any answer that is not ONE JSON value, and a connector
    result is followed only by its citation stamp."""
    from .tool_result_capture import RESULT_CUT_NOTE_KEY
    payload, rest = _leading_json_and_rest(text)
    if payload is None:
        return _approximate_line_facts(text, system, ordinal)
    facts = []
    if isinstance(payload, dict):
        record = approximate_record(payload)
        if record:
            facts.append(_approximate_fact(system, record["functions"], record["figures"],
                                           ordinal))
        note = payload.get(RESULT_CUT_NOTE_KEY)
        if isinstance(note, str):
            facts += _approximate_line_facts(note, system, ordinal)
    facts += _approximate_line_facts(rest, system, ordinal)
    return facts


def _approximate_line_facts(text: str, system: str, ordinal) -> list[dict]:
    facts = []
    for match in _APPROXIMATE_RE.finditer(text):
        functions = _approximate_functions(re.split(r", | and ", match.group("functions")))
        if functions:
            facts.append(_approximate_fact(system, functions,
                                           _figures_of(match.group("figures")), ordinal))
    for match in _APPROXIMATE_QUERY_RE.finditer(text):
        functions = _approximate_functions(re.split(r", | and ", match.group("functions")))
        if functions:
            facts.append(_approximate_fact(system, functions, [], ordinal))
    return facts
    for fact in facts:
        if fact not in unique:
            unique.append(fact)
    return unique[:MAX_FACTS]


def _system_pattern(system: str) -> re.Pattern:
    """A system id as prose writes it: case folded, ``-``/``_``/space alike."""
    parts = [re.escape(p) for p in re.split(r"[-_\s]+", system) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s_-]*".join(parts) + r"(?![A-Za-z0-9])",
                      re.IGNORECASE)


def answer_sources(body, systems=(), ordinal_systems=None) -> set[str] | None:
    """The systems the whole answer attributes anything to: the system of
    every citation ordinal it cites (``ordinal_systems``) and every system of
    the turn it names. ``None`` when a cited ordinal resolves to no known
    system (an unknown source, never assumed to be the fact's)."""
    if not isinstance(body, str):
        return None
    resolved = ordinal_systems or {}
    sources: set[str] = set()
    for group in _CITE_RE.findall(body):
        for number in re.split(r"\s*,\s*", group):
            system = resolved.get(int(number))
            if not system:
                return None
            sources.add(system)
    sources |= {s for s in systems if s and _system_pattern(s).search(body)}
    return sources


def _cited_ordinals(text: str) -> set[int]:
    return {int(n) for group in _CITE_RE.findall(text) for n in re.split(r"\s*,\s*", group)}


def _only_source_carries(fact: dict, body, systems, ordinal_systems) -> bool:
    """Whether the whole-answer fallback lets an unattributed figure count for
    ``fact``: the answer cites or names exactly one source, the fact's system
    (:func:`answer_sources`), and the fact's own result is the one it points
    at. When the answer has citations, they must be the fact's ordinal alone
    (a citation of another result of the same system means the figure may be
    that result's, whatever the numbers say); with none, the system must have
    exactly one cited result this turn (the fact's) so the name can point at
    no other."""
    own = fact.get("system") or ""
    if not own or answer_sources(body, systems, ordinal_systems) != {own}:
        return False
    cited = _cited_ordinals(body)
    if cited:
        return cited == {fact.get("ordinal")}
    results = {o for o, system in (ordinal_systems or {}).items() if system == own}
    return results == {fact.get("ordinal")}


def attributed_figures(fact: dict, body, systems=(), ordinal_systems=None) -> list[str]:
    """The figures of ``fact`` that ``body`` states where it attributes them to
    the fact's source. ``body`` is read by sentence (a clause after a
    semicolon counts as its own). A sentence with a matched figure counts when
    it cites the fact's ordinal (``[n]``); with no citation, when it names the
    fact's system and no other system of the turn (``systems``); with neither,
    when the whole answer points at the fact's own result alone
    (:func:`_only_source_carries`). Anything else does not count, so with two
    sources, or two results of one source the answer could mean, an
    unattributed figure counts for neither."""
    if not isinstance(body, str) or not isinstance(fact, dict):
        return []
    own = fact.get("system") or ""
    patterns = {s: _system_pattern(s) for s in {*systems, own} if s}
    only_source = _only_source_carries(fact, body, systems, ordinal_systems)
    used: list[str] = []
    for unit in _UNIT_SPLIT.split(body):
        used += [f for f in _unit_figures(fact, unit, patterns, only_source) if f not in used]
    return used[:MAX_FIGURES_SHOWN]


def _unit_figures(fact: dict, unit: str, patterns: dict, only_source: bool) -> list[str]:
    """The figures of ``fact`` that one text ``unit`` states where it
    attributes them to the fact's source (:func:`attributed_figures`)."""
    figures = figures_used(fact.get("figures"), unit)
    if not figures:
        return []
    own = fact.get("system") or ""
    cited = _cited_ordinals(unit)
    named = {s for s, pattern in patterns.items() if pattern.search(unit)}
    if cited:
        counts = fact.get("ordinal") in cited
    elif named:
        counts = bool(own) and named == {own}
    else:
        counts = only_source
    return figures if counts else []


def final_lines(facts, body, systems=(), ordinal_systems=None) -> list[str]:
    """The provenance sentences for this turn: one per split or storage fact
    whose figures ``body`` (the final answer before its control blocks)
    states and attributes to that fact's source (:func:`attributed_figures`),
    one per approximate fact whose result the answer cites
    (:func:`_approximate_final`), then one per population fact whose figures
    the answer describes with another value of the pinned column
    (:func:`_population_final`). ``ordinal_systems`` maps each cited tool
    result's ordinal to its system."""
    lines: list[str] = []
    for fact in facts or ():
        if not isinstance(fact, dict) or fact.get("kind") not in (
                "split", "storage", "approximate"):
            continue
        if fact["kind"] == "approximate":
            line = _approximate_final(fact, body)
        else:
            used = attributed_figures(fact, body, systems, ordinal_systems)
            line = "" if not used else (_split_sentence(fact, used) if fact["kind"] == "split"
                                        else _storage_sentence(fact))
        if line and line not in lines:
            lines.append(line)
    for line in _population_final(facts, body, systems, ordinal_systems):
        if line not in lines:
            lines.append(line)
    return lines[:MAX_FACTS]


# --------------------------------------------------------------------------- #
# The population sentence (orchestrator side)
# --------------------------------------------------------------------------- #
_TABLE_ROW_RE = re.compile(r"\s*\|")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")


def _population_units(body: str) -> list[tuple[str, str]]:
    """The texts that state figures, as ``(text, paragraph)`` pairs: each
    sentence or clause of a prose line (as :func:`attributed_figures` reads
    them) with ``paragraph`` empty, and each table row with the paragraph
    above its table (the last prose paragraph before it, ``""`` when none)."""
    units: list[tuple[str, str]] = []
    paragraph: list[str] = []      # the prose paragraph being read, or the last one ended
    ended = False
    caption = ""
    in_table = False
    for line in body.split("\n"):
        if _TABLE_ROW_RE.match(line):
            if not in_table:
                caption, in_table = " ".join(paragraph), True
            units.append((line, caption))
            continue
        if in_table:               # a paragraph never carries past its table
            paragraph, ended, in_table = [], False, False
        if not line.strip():
            ended = True
            continue
        if ended:
            paragraph, ended = [], False
        paragraph.append(line.strip())
        units += [(u, "") for u in _UNIT_SPLIT.split(line) if u]
    return units


def _value_words(value) -> tuple:
    """``value``'s words, case and separator folded (``normalize_term``)."""
    from .metric_term_resolution import normalize_term
    return tuple(normalize_term(value).split())


#: An identifier-shaped token: letters and digits, joined by ``_`` or ``-``.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[_-]+[A-Za-z0-9]+)*")
#: What may stand between the spelled-out words of one value.
_WORD_GAP_RE = re.compile(r"[ \t]+")


def _plain(text: str) -> str:
    """``text`` with each code span blanked to its backticks (offsets kept),
    so nothing inside one is read and no phrase runs across one."""
    return _CODE_SPAN_RE.sub(lambda m: "`" + " " * (len(m.group(0)) - 2) + "`", text)


def _named_values(text: str, values) -> list[tuple[str, int, int]]:
    """The ``values`` (code-like values of one column) that ``text`` names
    outside its code spans, as ``(value, start, end)`` spans in text order:
    whole identifier-shaped tokens whose words, case and separator folded,
    are exactly the value's (``Step-A-Route`` and ``step a route`` name
    ``STEP_A_ROUTE``; ``STEP_A_ROUTE_EXTRA`` and ``EXTRA_STEP_A_ROUTE`` do
    not), the tokens of one name separated by spaces only. Where more than
    one value matches, the longest is the one named, so ``step a route
    buffer`` names ``STEP_A_ROUTE_BUFFER`` when that is one of ``values``."""
    wanted: dict = {}
    for value in values or ():
        if _code_like(value):
            wanted.setdefault(_value_words(value), []).append(value)
    if not wanted or not isinstance(text, str):
        return []
    longest = max(len(words) for words in wanted)
    plain = _plain(text)
    tokens = [(m.start(), m.end(), _value_words(m.group(0))) for m in _TOKEN_RE.finditer(plain)]
    found: list = []
    i = 0
    while i < len(tokens):
        best, words = None, ()
        for j in range(i, len(tokens)):
            if j > i and not _WORD_GAP_RE.fullmatch(plain[tokens[j - 1][1]:tokens[j][0]]):
                break
            words += tokens[j][2]
            if len(words) > longest:
                break
            if words in wanted:
                best = (j, words)
        if best is None:
            i += 1
            continue
        j, words = best
        found += [(value, tokens[i][0], tokens[j][1])
                  for value in _spelled(plain[tokens[i][0]:tokens[j][1]], wanted[words])]
        i = j + 1
    return found


def _spelled(spelling: str, values: list) -> list:
    """The ``values`` (all folding to the same words) that ``spelling``
    names: all of them when they are one value; else the one ``spelling``
    writes exactly, else the one it writes up to case, else none, so text
    naming one of several values spelled alike never names the others."""
    if len(set(values)) <= 1:
        return values
    for same in (lambda v: v == spelling, lambda v: v.casefold() == spelling.casefold()):
        exact = {v for v in values if isinstance(v, str) and same(v)}
        if exact:
            return sorted(exact) if len(exact) == 1 else []
    return []


#: A word or figure right after a value: the value modifies it.
_NEXT_WORD_RE = re.compile(r"[ \t]*[A-Za-z0-9]")
#: Two subjects joined, outside parentheses.
_COORDINATOR_RE = re.compile(r"\b(?:and|or|plus|versus|vs)\b|[&/]", re.IGNORECASE)


def _paragraph_value(paragraph: str, values) -> set:
    """``{value}`` when the paragraph above a table, outside code spans and
    citations, is about ONE value of ``values`` (the column's code-like
    values) as the table's population, else ``set()``: it names exactly one
    of them (:func:`_named_values`, parentheses included), names it outside
    parentheses, that value ends its phrase there (no word follows it, as in
    ``STEP_A_ROUTE status``), and outside parentheses the paragraph joins no
    two subjects (``and``, ``or``, ``plus``, ``vs``, ``&``, ``/``)."""
    if not paragraph:
        return set()
    plain = _CITE_RE.sub(lambda m: " " * len(m.group(0)), _plain(paragraph))
    named = _named_values(plain, values)
    if len({value for value, _start, _end in named}) != 1:
        return set()
    inside, depth = [], 0
    for char in plain:
        if char == ")":
            depth = max(depth - 1, 0)
        inside.append(depth > 0)
        if char == "(":
            depth += 1
    outer = "".join(" " if hidden else char for char, hidden in zip(plain, inside))
    spans = [(start, end) for _value, start, end in named if not inside[start]]
    if not spans or _COORDINATOR_RE.search(outer) \
            or any(_NEXT_WORD_RE.match(outer, end) for _start, end in spans):
        return set()
    return {named[0][0]}


def _population_final(facts, body, systems=(), ordinal_systems=None) -> list[str]:
    """One sentence per population fact whose figures a text unit
    (:func:`_population_units`) states, attributed to the fact's source as
    :func:`attributed_figures` decides (a table row by its own text and the
    paragraph above its table), while that unit's text, or that paragraph
    when it is about one population (:func:`_paragraph_value`), outside code
    spans, names a code-like value of the pinned column the
    fact's rows leave out, and no population fact stated in the same unit
    pins that column to that value. The sentence says where the value is
    named, never what the answer means by it. A value is named only as a
    whole (:func:`_named_values`), among every code-like value of its column
    the turn's population facts know."""
    population = [f for f in facts or () if isinstance(f, dict)
                  and f.get("kind") == "population" and f.get("values")]
    if not population or not isinstance(body, str):
        return []
    sources = []
    known: dict = {}
    for fact in population:
        own = fact.get("system") or ""
        sources.append(({s: _system_pattern(s) for s in {*systems, own} if s},
                        _only_source_carries(fact, body, systems, ordinal_systems)))
        known.setdefault(str(fact.get("column") or ""), set()).update(
            v for v in [*(fact.get("values") or ()), *(fact.get("others") or ())]
            if _code_like(v))
    found: dict = {}
    for text, paragraph in _population_units(body):
        unit = f"{paragraph}\n{text}" if paragraph else text
        stated = [(i, figures) for i, fact in enumerate(population)
                  if (figures := _unit_figures(fact, unit, *sources[i]))]
        if not stated:
            continue
        for i, figures in stated:
            fact = population[i]
            values = known.get(str(fact.get("column") or ""))
            pooled = {v for j, _f in stated if population[j].get("column") == fact.get("column")
                      for v in population[j].get("values") or ()}
            others = [o for o in fact.get("others") or () if _code_like(o) and o not in pooled]
            named = {v for v, _start, _end in _named_values(text, values)}
            framed_here = _paragraph_value(paragraph, values)
            in_text = [o for o in others if o in named]
            in_paragraph = [o for o in others if o not in in_text and o in framed_here]
            if in_text or in_paragraph:
                # One sentence per source, column and population, however many
                # of its results the text states.
                key = (fact.get("system") or "", str(fact.get("column") or ""),
                       tuple(fact.get("values") or ()))
                _fact, shown, names, framed = found.setdefault(key, (fact, [], [], []))
                new = sorted((f for f in figures if f not in shown), key=unit.find)
                shown += [f for f in new if f not in shown]
                names += [o for o in in_text if o not in names]
                framed += [o for o in in_paragraph if o not in framed]
    return [line for line in (
        _population_sentence(fact, shown, names, [o for o in framed if o not in names])
        for fact, shown, names, framed in found.values()) if line][:MAX_FACTS]


def _population_sentence(fact: dict, used: list, named: list, framed=()) -> str:
    """The population disclosure, naming the figures as the final answer
    writes them, the pin their rows were computed on, and the other values of
    that column that the text stating them (``named``) or the paragraph above
    its table (``framed``) names. It states where each value is named and
    nothing about what the answer means by it. A column name that is not a
    plain identifier, or a value that cannot be printed, is withheld."""
    from .engine_names import printable
    column = str(fact.get("column") or "")
    shown_column = printable(column)
    column = column if shown_column else _COLUMN_WITHHELD
    values = [v for v in fact.get("values") or () if _pin_value_shown(v)]
    pin = _pin_text(values) if values else "= a value that is withheld"
    system = f"{fact['system']} " if fact.get("system") else ""
    more = max(len(used) - MAX_FIGURES_SHOWN, 0)
    used = used[:MAX_FIGURES_SHOWN]
    noun = "figure" if len(used) + more == 1 else "figures"
    shown = f"{', '.join(used)} and {more} more" if more else _and_list(used)
    pronoun, verb = ("it", "was") if len(used) + more == 1 else ("them", "were")
    printed = [n for n in [*named, *framed] if _code_like(n) and _pin_value_shown(n)]
    printed = printed[:_MAX_OTHERS_SHOWN]
    if not printed:
        return ""
    in_text = any(n in printed for n in named)
    in_paragraph = any(n in printed for n in framed)
    if in_text and in_paragraph:
        where = f"the text stating {pronoun}, or the paragraph above its table, names"
    elif in_text:
        where = f"the text stating {pronoun} also names"
    else:
        where = f"the paragraph above the table stating {pronoun} names"
    which = (f"{column} value" if shown_column else "the same column's value") + \
        ("s" if len(printed) > 1 else "")
    quoted = _and_list([f"'{n}'" for n in printed])
    return (f"{ANSWER_PREFIX}the {system}{noun} {shown} in this answer {verb} computed on "
            f"rows with {column} {pin}; {where} {which} {quoted}.")


def _and_list(items: list) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _approximate_final(fact: dict, body) -> str:
    """The sentence for one approximate fact, decided only by the sentences
    of ``body`` that cite the fact's own result ordinal: the figure sentence
    for its figures they state as written, else the neutral sentence; none
    when no sentence cites it."""
    ordinal = fact.get("ordinal")
    if not isinstance(body, str) or not isinstance(ordinal, int):
        return ""
    units = [u for u in _UNIT_SPLIT.split(body) if ordinal in _cited_ordinals(u)]
    if not units:
        return ""
    used: list[str] = []
    for unit in units:
        used += [f for f in figures_used(fact.get("figures"), unit) if f not in used]
    if used:
        return _approximate_sentence(fact, used[:MAX_FIGURES_SHOWN])
    from .approximate_aggregates import joined
    who = f"{fact['system']}'s" if fact.get("system") else "the system's"
    return (f"{ANSWER_PREFIX}{who} query also computed approximate values with "
            f"{joined(fact.get('functions'))}{_NEUTRAL_TAIL}")


def _split_sentence(fact: dict, used: list) -> str:
    system = f"{fact['system']} " if fact.get("system") else ""
    noun, verb = ("figure", "is") if len(used) == 1 else ("figures", "are")
    table = str(fact.get("table") or "")
    approximate = _SPLIT_APPROXIMATE if fact.get("approximate") is True else ""
    return (f"{ANSWER_PREFIX}the {system}{noun} in this answer {verb} from one query result "
            f"that the connector computed from {fact['pieces']:,} partition pieces of "
            f"{table if _shown_table(table) else _TABLE_FALLBACK}, all read at one table "
            f"snapshot{approximate}.")


def _approximate_sentence(fact: dict, used: list) -> str:
    """The approximate disclosure, naming the figures as the final answer
    writes them (no figure of its own)."""
    system = f"{fact['system']} " if fact.get("system") else ""
    noun = "figure" if len(used) == 1 else "figures"
    shown = used[0] if len(used) == 1 else f"{', '.join(used[:-1])} and {used[-1]}"
    return (f"{ANSWER_PREFIX}the {system}{noun} {shown} in this answer "
            f"{_approximate_claim(used, fact.get('functions'))}")


def _storage_sentence(fact: dict) -> str:
    """The storage disclosure; a name that is not a plain identifier
    (``engine_names.printable``) is a placeholder."""
    from .engine_names import printable
    who = fact.get("system") or "the system"
    columns = ", ".join(c for c in fact.get("columns") or () if printable(c)) or (
        "a column whose name is withheld")
    table = str(fact.get("table") or "")
    table = table if _shown_table(table) else _TABLE_FALLBACK
    return (f"{ANSWER_PREFIX}{who} queried {table} with a bound on its storage "
            f"partition column {columns} ({fact['conjuncts']}), so figures from that query "
            "leave out rows outside that range.")
