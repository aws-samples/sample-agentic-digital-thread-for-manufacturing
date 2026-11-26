# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Same-named identifier columns that hold different identifiers (measured).

Workshop rehearsal, 2026-10-17: one system kept a concept
under two columns with the SAME name in two tables, each mapped as that
concept's identifier, but each column held a different identifier: its
values had another shape and never occurred in the other column. The system
agent was told only that both columns were the concept's identifier, and its
schema samples are "e.g." values its own rules say can never show a value's
shape. It joined the two columns to each other, looked values read from one
up in the other (no rows), then rewrote the values into the other column's
form by hand and guessed at the records.

Onboarding now MEASURES each such pair on the database itself
(:func:`measure`) and stores each pair it names in the knowledge graph, as one
``DIFFERENT_IDENTIFIER_FROM`` edge between the two columns' Field nodes
carrying each column's shape signature (:func:`encode_signature`;
``onboard._dp_store_distinct_identifiers``, ADR-0098). The record read back
from the graph (:func:`record_from_row`) is what the baked schema says, with
an example of each side's shape when one can be printed there
(``schema_format.with_distinct_identifiers``, :func:`shape_example`), and
what the system agent's PostgreSQL tool refuses lookups by
(``identifier_shape_guard``, from the signatures, printable or not).

A pair is two rendered tables whose same-named column is mapped as an
identifier of the same concept. A pair whose two columns the database's own
foreign keys already tie to one key holds one identifier by construction and
is not read. Each remaining pair, up to :data:`MAX_PAIRS`, is read in ONE
query under a short statement deadline and a total time budget: from each
column the lowest and the highest distinct values (each end bounded in rows
read before DISTINCT, so the sample is the same on every run; an index on the
column lets that read stop early, and the deadline bounds it when there is
none), and for each value read whether it occurs ANYWHERE in
the other column. The pair is named only when every one of these holds
(:func:`verdict`):

* each side read at least :data:`MIN_SAMPLED` values and none occurs in the
  other column;
* each side's values are consistent: at least :data:`SHAPE_MIN_SHARE` of
  them share one shape, so a free-text or mixed column never qualifies;
* the two shapes differ in more than the count of characters in a segment (a
  different prefix, separator layout, segment count or character class: one
  sequence can grow a digit, and two disjoint populations of ONE identifier
  have the same shape), and no value read on one side has the other side's
  shape.

The shape is the one the system agent's IDENTIFIER-MISS sanitizer compares
(``system_agent._shape_signature`` / ``_signatures_match``), so a value from
one column of a named pair never matches the other column's format there and
the agent's own wrong-vocabulary protocol carries the miss.

Generic: no dataset, system, table, column or concept literals. Pure helpers
plus one IO entry point that takes the ``pg`` function, so the caller's
seam (and its tests' fake) is the only database access.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Callable, Iterable

from .db import validate_identifier

logger = logging.getLogger(__name__)

#: Distinct values read from EACH end (lowest, highest) of each column.
SAMPLE_PER_END = 100
#: Rows taken from each end before DISTINCT (an index on the column lets the
#: read stop after them; without one the per-read deadline bounds the scan).
SAMPLE_ROWS = 20_000
#: Fewest distinct values each side must read before "none shared" means anything
#: (the cross-system join discovery's own overlap floor).
MIN_SAMPLED = 5
#: Share of a side's values that must have its most common shape.
SHAPE_MIN_SHARE = 0.9
#: Most pairs measured per provision; the rest are counted, never silently dropped.
MAX_PAIRS = 8
#: Statement deadline of one pair's read, in milliseconds.
QUERY_TIMEOUT_MS = 3_000
#: Total time every read of one provision may take, in seconds.
TOTAL_BUDGET_S = 10.0
#: A read is not started with less than this left of the budget, in milliseconds.
MIN_QUERY_MS = 500

#: The roles that make a mapped column an identifier of its concept.
_IDENTIFIER_ROLES = frozenset({"identifier"})
#: ``error_category`` (``db.pg``) or exception class of a statement deadline.
_TIMEOUT_CATEGORIES = frozenset({"QueryCanceled"})


def _split(field: str) -> tuple[str, str] | None:
    """``(table, column)`` of a ``table.column`` mapping field, else None."""
    table, dot, column = str(field or "").rpartition(".")
    return (table, column) if dot and table and column else None


def candidate_pairs(cm_items: Iterable, tables: Iterable[str],
                    foreign_keys: Iterable = ()) -> list[dict]:
    """Every pair worth measuring, sorted by ``(concept, column, table_a, table_b)``.

    A pair is ``{concept, column, table_a, table_b}``: two DIFFERENT rendered
    ``tables`` whose same-named column is mapped (``cm_items``, the KNOWN
    CONCEPT MAPPINGS records ``{concept, field: "table.column", role}``) as an
    identifier of the same concept. A pair the ``foreign_keys``
    (``{from_table, from_field, to_table, to_field}``) tie to one key is left
    out: one column references the other, or both reference the same key.
    Pure."""
    known = {str(t) for t in tables or ()}
    refs: dict[tuple[str, str], tuple[str, str]] = {}
    for fk in foreign_keys or ():
        if isinstance(fk, dict):
            src = (str(fk.get("from_table") or ""), str(fk.get("from_field") or ""))
            dst = (str(fk.get("to_table") or ""), str(fk.get("to_field") or ""))
            if all(src) and all(dst):
                refs.setdefault(src, dst)
    groups: dict[tuple[str, str], set[str]] = {}
    for item in cm_items or ():
        if not isinstance(item, dict) or str(item.get("role") or "") not in _IDENTIFIER_ROLES:
            continue
        concept, split = str(item.get("concept") or ""), _split(item.get("field"))
        if concept and split and split[0] in known:
            groups.setdefault((concept, split[1]), set()).add(split[0])

    def _key(table: str, column: str) -> tuple[str, str]:
        return refs.get((table, column), (table, column))

    pairs: list[dict] = []
    for (concept, column), found in sorted(groups.items()):
        ordered = sorted(found)
        for i, table_a in enumerate(ordered):
            for table_b in ordered[i + 1:]:
                if _key(table_a, column) == _key(table_b, column):
                    continue
                pairs.append({"concept": concept, "column": column,
                              "table_a": table_a, "table_b": table_b})
    return pairs


def measure_sql(schema: str, pair: dict, same_type: bool) -> str:
    """The ONE read for ``pair``. Returns one row per distinct value read,
    ``(side, value, seen_in_other)``: side ``a`` then ``b``, each the lowest
    and the highest :data:`SAMPLE_PER_END` distinct values of its column, each
    end taken from the first :data:`SAMPLE_ROWS` rows in column order, so the
    sample is deterministic. An index on the column lets PostgreSQL stop after
    those rows; without one it scans the table and keeps a top-N sort, and the
    per-read deadline and total budget in :func:`measure` bound that work.
    Each sampled value is then looked up anywhere in the other column.
    ``same_type`` compares the stored values directly (an index on the other
    column can serve the lookup), else both sides as text, which no ordinary
    index serves. Every name is checked with
    ``validate_identifier``; every limit is a bound parameter
    (:func:`measure_params`)."""
    for name in (schema, pair["table_a"], pair["table_b"], pair["column"]):
        validate_identifier(name)
    col = f'"{pair["column"]}"'
    value = col if same_type else f"CAST({col} AS TEXT)"
    other = f"o.{col}" if same_type else f"CAST(o.{col} AS TEXT)"

    def end(table: str, direction: str) -> str:
        return (f"SELECT v FROM (SELECT DISTINCT v FROM (SELECT {value} AS v "  # nosec B608 - names validated above; limits are bound
                f'FROM "{schema}"."{table}" WHERE {col} IS NOT NULL ORDER BY {col} {direction} '
                f"LIMIT %s) r ORDER BY v {direction} LIMIT %s) e")

    def side(table: str) -> str:
        return f"{end(table, 'ASC')} UNION {end(table, 'DESC')}"

    def seen(cte: str, table: str) -> str:
        return (f"SELECT '{cte}', {cte}.v, EXISTS (SELECT 1 "  # nosec B608 - names validated above
                f'FROM "{schema}"."{table}" o WHERE {other} = {cte}.v) FROM {cte}')

    return (f"WITH a AS ({side(pair['table_a'])}), b AS ({side(pair['table_b'])}) "
            f"{seen('a', pair['table_b'])} UNION ALL {seen('b', pair['table_a'])}")


def measure_params() -> tuple:
    """The bound limits of :func:`measure_sql`, in order."""
    return (SAMPLE_ROWS, SAMPLE_PER_END) * 4


# --------------------------------------------------------------------------- #
# Shapes (the system agent's IDENTIFIER-MISS signature)
# --------------------------------------------------------------------------- #

def _signature(value: str) -> tuple:
    from .system_agent import _shape_signature
    return _shape_signature(value)


def _same_shape(sig_a: tuple, sig_b: tuple) -> bool:
    from .system_agent import _signatures_match
    return _signatures_match(sig_a, sig_b)


def side_shape(values: list[str]) -> tuple | None:
    """The shape at least :data:`SHAPE_MIN_SHARE` of ``values`` have, or None
    (no values, no such majority, or a tie for the most common shape). Pure."""
    sigs = [_signature(v) for v in values if str(v).strip()]
    if not sigs:
        return None
    (top, _count), *rest = Counter(sigs).most_common(2)
    if rest and rest[0][1] == _count:
        return None
    share = sum(1 for s in sigs if _same_shape(s, top)) / len(sigs)
    return top if share >= SHAPE_MIN_SHARE else None


def _segment_kind(segment: tuple) -> tuple:
    """What a shape segment is, without its length: a prefix keeps its text,
    a digit or hex run is one kind (the agent's own leniency), else its class."""
    if segment[0] == "prefix":
        return segment
    return ("number",) if segment[1] in ("digit", "hex") else (segment[1],)


def differ_beyond_length(sig_a: tuple, sig_b: tuple) -> bool:
    """True when two shapes differ in more than a segment's length: their
    separators, segment count, a prefix's text or a segment's class. Pure."""
    if _same_shape(sig_a, sig_b):
        return False
    (seps_a, segs_a), (seps_b, segs_b) = sig_a, sig_b
    if seps_a != seps_b or len(segs_a) != len(segs_b):
        return True
    return any(_segment_kind(a) != _segment_kind(b) for a, b in zip(segs_a, segs_b))


#: What a rendered shape example may hold: no space, comma, semicolon,
#: parenthesis or bracket (they would end a baked list entry or a table), and
#: it starts and ends with a letter or digit (a trailing dot would close it).
_EXAMPLE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_/.:-]*[A-Za-z0-9])?")


def _segment_text(segment) -> str | None:
    """A run of text with the shape of one signature segment, or None."""
    if segment[0] == "prefix":
        text = str(segment[1])
        return text.upper() if text.upper().lower() == text else text
    length, kind = segment
    if isinstance(length, bool) or not isinstance(length, int):
        return None
    if kind == "digit" and length > 0:
        return "9" * length
    if kind == "hex" and length > 0:
        return "a" * length
    if kind == "alnum":
        # A one-character mixed segment is a single symbol; an empty one sits
        # between two separators.
        return "X" + "9" * (length - 1) if length > 1 else ("#" if length == 1 else "")
    return None


def shape_text(signature) -> str | None:
    """A value whose shape IS ``signature`` (a prefix keeps its text, a digit
    run is ``9``s, a hex run ``a``s, a mixed run ``X9...``, a one-character
    mixed run ``#``), whatever characters that takes, or None when no text
    reads back to exactly that signature. What the guard's refusal prints for
    a measured column; :func:`shape_example` is the subset a baked list entry
    can hold. Pure."""
    try:
        separators, segments = signature
        parts = [_segment_text(segment) for segment in segments]
        if not parts or None in parts or len(separators) != len(parts) - 1:
            return None
        text = parts[0] + "".join(sep + part for sep, part in zip(separators, parts[1:]))
    except (TypeError, ValueError, IndexError):
        return None
    return text if _signature(text) == tuple(signature) else None


def shape_example(signature) -> str | None:
    """A value written so that its shape IS ``signature`` (:func:`shape_text`),
    or None when it could not sit in a baked list entry: it holds a space or
    a symbol, a segment of one character or none, or ``e.g``. The guard in
    the system agent's query tool reads the example back with the same
    signature function. Pure."""
    text = shape_text(signature)
    if text is None or any(seg[0] != "prefix" and seg[1] == "alnum" and seg[0] <= 1
                           for seg in signature[1]):
        return None
    if not _EXAMPLE_RE.fullmatch(text) or "e.g" in text.lower():
        return None
    return text if _same_shape(_signature(text), signature) else None


# --------------------------------------------------------------------------- #
# The stored signature (one canonical JSON form)
# --------------------------------------------------------------------------- #

_SEGMENT_CLASSES = frozenset({"digit", "hex", "alnum"})


def encode_signature(signature) -> str | None:
    """``signature`` (``system_agent._shape_signature``) as the compact JSON
    the graph and the agent config store, or None when it is not one that
    function produces (:func:`decode_signature`). Pure."""
    try:
        text = json.dumps(signature, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        return None
    return text if decode_signature(text) is not None else None


def decode_signature(text) -> tuple | None:
    """The signature a stored JSON text holds, as ``_shape_signature``
    returns it, or None for anything else: not JSON, another structure, a
    character that is not a separator, an unknown segment class, or a
    signature no value has (:func:`shape_text` must read back to it
    exactly). Pure."""
    from .system_agent import _MISS_SEPARATOR_CHARS
    try:
        data = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    if not (isinstance(data, list) and len(data) == 2
            and all(isinstance(part, list) for part in data)):
        return None
    raw_seps, raw_segs = data
    if not all(isinstance(s, str) and len(s) == 1 and s in _MISS_SEPARATOR_CHARS
               for s in raw_seps):
        return None
    segments = []
    for seg in raw_segs:
        if not (isinstance(seg, list) and len(seg) == 2):
            return None
        head, tail = seg
        if head == "prefix" and isinstance(tail, str) and tail:
            segments.append(("prefix", tail))
        elif isinstance(head, int) and not isinstance(head, bool) and tail in _SEGMENT_CLASSES:
            segments.append((head, tail))
        else:
            return None
    signature = (tuple(raw_seps), tuple(segments))
    return signature if shape_text(signature) is not None else None


def side_signatures(rows) -> dict:
    """``{signature_a, signature_b}``: each side's shape (:func:`side_shape`)
    in the measured ``(side, value, seen)`` rows, encoded
    (:func:`encode_signature`), or ``{}`` when either side has none. Kept
    whether or not an example of it can be printed. Pure."""
    values: dict[str, list[str]] = {"a": [], "b": []}
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) == 3 and row[0] in values \
                and row[1] is not None:
            values[row[0]].append(str(row[1]))
    out = {}
    for side in ("a", "b"):
        shape = side_shape(values[side])
        encoded = encode_signature(shape) if shape is not None else None
        if encoded is None:
            return {}
        out[f"signature_{side}"] = encoded
    return out


def side_examples(rows) -> dict:
    """``{shape_a, shape_b}``: :func:`shape_example` of each side's shape
    (:func:`side_shape`) in the measured ``(side, value, seen)`` rows, or ``{}``
    when either side has none. Pure."""
    values: dict[str, list[str]] = {"a": [], "b": []}
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) == 3 and row[0] in values \
                and row[1] is not None:
            values[row[0]].append(str(row[1]))
    out = {}
    for side in ("a", "b"):
        shape = side_shape(values[side])
        example = shape_example(shape) if shape is not None else None
        if example is None:
            return {}
        out[f"shape_{side}"] = example
    return out


def verdict(rows) -> dict | None:
    """``{read_a, read_b}`` when the measured ``(side, value, seen)`` rows
    show two different identifiers (module docstring), else None. Pure."""
    values: dict[str, list[str]] = {"a": [], "b": []}
    seen = {"a": 0, "b": 0}
    for row in rows or ():
        if not isinstance(row, (list, tuple)) or len(row) != 3 or row[0] not in values:
            return None
        if row[1] is None:
            continue
        values[row[0]].append(str(row[1]))
        if row[2] is True or str(row[2]).lower() in ("true", "t", "1"):
            seen[row[0]] += 1
    a, b = values["a"], values["b"]
    if min(len(a), len(b)) < MIN_SAMPLED or seen["a"] or seen["b"]:
        return None
    shape_a, shape_b = side_shape(a), side_shape(b)
    if shape_a is None or shape_b is None or not differ_beyond_length(shape_a, shape_b):
        return None
    if (any(_same_shape(_signature(v), shape_b) for v in a)
            or any(_same_shape(_signature(v), shape_a) for v in b)):
        return None
    return {"read_a": len(a), "read_b": len(b)}


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #

def _column_type(table_details: dict, table: str, column: str) -> str:
    for entry in table_details.get(table) or ():
        if isinstance(entry, (list, tuple)) and entry and entry[0] == column:
            return str(entry[1] if len(entry) > 1 else "").lower()
    return ""


def _read(pg: Callable, schema: str, pair: dict, table_details: dict, timeout_ms: int):
    """``(status, rows)``: status ``ok``, ``timeout`` or ``failed``."""
    type_a = _column_type(table_details, pair["table_a"], pair["column"])
    type_b = _column_type(table_details, pair["table_b"], pair["column"])
    try:
        sql = measure_sql(schema, pair, same_type=bool(type_a) and type_a == type_b)
        res = pg(schema, sql, measure_params(), statement_timeout_ms=timeout_ms)
    except Exception as e:  # noqa: BLE001 - a measurement is an addition, never required
        status = "timeout" if type(e).__name__ in _TIMEOUT_CATEGORIES else "failed"
        logger.info("Distinct-identifier read skipped for %s.%s / %s.%s: %s",
                    pair["table_a"], pair["column"], pair["table_b"], pair["column"],
                    type(e).__name__)
        return status, []
    if not isinstance(res, dict) or res.get("unavailable") or res.get("truncated"):
        category = res.get("error_category") if isinstance(res, dict) else None
        return ("timeout" if category in _TIMEOUT_CATEGORIES else "failed"), []
    return "ok", res.get("rows") or []


def measure(pg: Callable, schema: str, cm_items: Iterable, table_details: dict,
            foreign_keys: Iterable = (), clock: Callable[[], float] = time.monotonic) -> dict:
    """Measure every candidate pair (:func:`candidate_pairs`) of the rendered
    ``table_details`` (``{table: [(column, data_type), ...]}``), calling
    ``pg(schema, sql, params, statement_timeout_ms=...)`` once per pair.

    Bounded: at most :data:`MAX_PAIRS` reads, each under a deadline of
    :data:`QUERY_TIMEOUT_MS` or what is left of :data:`TOTAL_BUDGET_S`
    (``clock``), whichever is shorter; no read starts with less than
    :data:`MIN_QUERY_MS` left.

    Returns ``{"distinct": [{concept, column, table_a, table_b, read_a,
    read_b, signature_a, signature_b, shape_a, shape_b}], "not_measured":
    [{concept, column, table_a, table_b, reason}]}``: ``signature_a`` /
    ``signature_b`` (:func:`side_signatures`) are each side's measured shape,
    ``shape_a`` / ``shape_b`` (:func:`side_examples`) are left out when a
    side's shape has no example a baked list entry can hold. A not-measured
    pair carries reason ``failed``, ``timeout``, ``budget`` or ``pair_cap``.
    A failed, timed-out or skipped pair is never reported as distinct."""
    out: dict = {"distinct": [], "not_measured": []}
    pairs = candidate_pairs(cm_items, table_details, foreign_keys)
    started = clock()
    for i, pair in enumerate(pairs[:MAX_PAIRS]):
        left_ms = int((TOTAL_BUDGET_S - (clock() - started)) * 1000)
        if left_ms < MIN_QUERY_MS:
            out["not_measured"].extend({**p, "reason": "budget"} for p in pairs[i:MAX_PAIRS])
            break
        status, rows = _read(pg, schema, pair, table_details, min(QUERY_TIMEOUT_MS, left_ms))
        if status != "ok":
            out["not_measured"].append({**pair, "reason": status})
            continue
        found = verdict(rows)
        if found:
            out["distinct"].append({**pair, **found, **side_signatures(rows),
                                    **side_examples(rows)})
    out["not_measured"].extend({**pair, "reason": "pair_cap"} for pair in pairs[MAX_PAIRS:])
    return out


# --------------------------------------------------------------------------- #
# The stored record (the knowledge graph holds the measured pairs)
# --------------------------------------------------------------------------- #

#: The ``basis`` of a stored pair: it was read on the data at onboarding.
BASIS_MEASURED = "measured"
#: Characters a stored name may not hold (they would end a baked list entry).
_NAME_STOPS = frozenset(" ,;:().[]")


def _stored_name(value) -> str | None:
    return value if isinstance(value, str) and value and not _NAME_STOPS & set(value) else None


def _stored_count(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count == value or isinstance(value, str) else None


def record_from_row(row) -> dict | None:
    """One stored pair as the graph read returns it (``onboard``'s
    ``DISTINCT_IDENTIFIER_RECORDS_READ``: ``{concept, column, column_b,
    table_a, table_b, read_a, read_b, signature_a, signature_b, measured_at,
    basis}``), as the record ``{concept, column, table_a, table_b, read_a,
    read_b, signature_a, signature_b, measured_at, basis}`` with the two
    signatures in their one encoded form, plus ``shape_a`` / ``shape_b`` when
    both can be printed in a baked list entry (:func:`shape_example` of each
    signature). The signatures are the shape source: a shape example on the
    row is not read.

    None for a row of any other form, a row the guard cannot use included: a
    signature missing, or one ``system_agent._shape_signature`` could not
    produce (:func:`decode_signature`; a shape-only edge, as an earlier
    build wrote, carried shape examples and no signature), a name missing or
    holding list or sentence punctuation, two column names that differ, a
    count that is not a whole number, or a basis other than
    :data:`BASIS_MEASURED`. Pure."""
    if not isinstance(row, dict) or row.get("basis") != BASIS_MEASURED:
        return None
    names = [_stored_name(row.get(k)) for k in ("concept", "column", "table_a", "table_b")]
    counts = [_stored_count(row.get(k)) for k in ("read_a", "read_b")]
    signatures = [decode_signature(row.get(k)) for k in ("signature_a", "signature_b")]
    if (not all(names) or row.get("column_b") != row.get("column") or None in counts
            or None in signatures):
        return None
    concept, column, table_a, table_b = names
    record = {"concept": concept, "column": column, "table_a": table_a, "table_b": table_b,
              "read_a": counts[0], "read_b": counts[1],
              "signature_a": encode_signature(signatures[0]),
              "signature_b": encode_signature(signatures[1]),
              "measured_at": str(row.get("measured_at") or ""), "basis": BASIS_MEASURED}
    examples = [shape_example(s) for s in signatures]
    if None not in examples:
        record["shape_a"], record["shape_b"] = examples
    return record


class UnusableRecordError(ValueError):
    """A stored different-identifier row that is not a record the guard can
    use (:func:`record_from_row`). Raised instead of skipping it: a skipped
    row reads as "no record", and a record without signatures would stand in
    for one, either way the guard would be lost without a word."""


def records_from_rows(rows) -> list[dict]:
    """:func:`record_from_row` of every row, in order; raises
    :class:`UnusableRecordError` (naming how many) when any row is not one.
    Pure."""
    rows = list(rows or [])
    records = [record_from_row(row) for row in rows]
    unusable = sum(1 for r in records if r is None)
    if unusable:
        raise UnusableRecordError(f"{unusable} of {len(rows)} stored distinct-identifier "
                                  "rows are not usable records")
    return records


def signed_records(records) -> list[dict] | None:
    """Records handed over in an agent config (``distinct_identifiers``),
    each checked again (:func:`record_from_row`), or None when any one is not
    a record the guard can use (a shape-only record, a signature
    missing or invalid, anything else). ``[]`` for an empty list. Pure."""
    checked = [record_from_row({**r, "column_b": r.get("column")}) if isinstance(r, dict)
               else None for r in (records or [])]
    return None if None in checked else checked


def shaped_pairs(records) -> list[dict]:
    """``{concept, column, table_a, table_b, signature_a, signature_b}`` of
    each stored record (:func:`record_from_row`) that carries both
    signatures, decoded (:func:`decode_signature`), in the given order. A
    record of any other form is skipped. Pure."""
    pairs: list[dict] = []
    for record in records or ():
        if not isinstance(record, dict):
            continue
        checked = record_from_row({**record, "column_b": record.get("column")})
        if checked and "signature_a" in checked:
            pair = {k: checked[k] for k in ("concept", "column", "table_a", "table_b")}
            pair["signature_a"] = decode_signature(checked["signature_a"])
            pair["signature_b"] = decode_signature(checked["signature_b"])
            pairs.append(pair)
    return pairs
