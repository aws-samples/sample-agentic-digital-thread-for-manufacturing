# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Duration measure + field refs + percentile aggregation TYPES and the ONE
closed rule for them, the sibling of ``metric_predicate_rule``.

A steward-defined DIRECT metric may measure the DURATION between two timestamp
columns of its measure table instead of aggregating one concept column, and
read a PERCENTILE of that duration. This module owns:

* the frozen types ``FieldRef`` (``{concept, column}``, a concept pinned to
  ONE column, resolved against the live MAPS_TO graph at resolve time) and
  ``DurationMeasure`` (``{kind: "duration", start, end, unit, clamp_negative}``,
  one closed kind, no general expression grammar);
* ``check_field_ref`` / ``check_measure`` / ``check_percentiles``, TOTAL over
  arbitrary input (a problem string, never a raw ``TypeError``/``OverflowError``),
  so both the definition door and the layer validator fail loud;
* ``render_duration_expr`` / ``render_percentile_select``, the dialect-neutral
  SQL composers, built EXCLUSIVELY from identifiers validated through
  ``metric_binding`` and a closed unit/dialect vocabulary (no user input ever
  interpolated).

Import-pure (ADR 0001): no I/O, no LLM, no ``eval``/``exec``, no domain literal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .metric_binding import quote_column_identifier, validate_column_name

#: The three duration units a steward may declare, with the divisor that scales
#: a second count into that unit. Seconds carry no divisor (byte-identical expr).
DURATION_UNITS: tuple[str, ...] = ("seconds", "minutes", "hours")
_UNIT_DIVISOR: dict[str, float] = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}

#: The one measure kind. No general expression grammar (scope discipline).
DURATION_KIND = "duration"

#: 1..5 percentiles, each strictly between 0 and 1.
MAX_PERCENTILES = 5

#: The SQL backends a duration-percentile metric supports. ``duckdb`` is the
#: S3 Tables engine (in-process DuckDB over the same Iceberg data as Athena).
_DIALECTS = frozenset({"athena", "postgres", "duckdb"})
#: The one qualifier set (none, or the grouped joined measure alias), mirroring
#: ``metric_predicate._QUALIFIERS``.
_QUALIFIERS = frozenset({"", "m."})


@dataclass(frozen=True)
class FieldRef:
    """A concept pinned to ONE column of the measure table (resolved against
    the live MAPS_TO graph, fail-closed, at resolve time)."""

    concept: str
    column: str


@dataclass(frozen=True)
class DurationMeasure:
    """The duration between two timestamp field refs, in ``unit`` seconds-based
    units, negatives clamped to 0 when ``clamp_negative``."""

    kind: str
    start: FieldRef
    end: FieldRef
    unit: str
    clamp_negative: bool = True


def check_field_ref(ref: object) -> str | None:
    """A problem string for an unacceptable field ref, ``None`` if ok.

    Both parts are id-shaped (non-empty, no whitespace); the column must be a
    SQL identifier (a dotted physical column passes, every segment validated,
    ADR-0076 D4). Total over hostile input."""
    if not isinstance(ref, FieldRef):
        return "must be a {concept, column} field ref"
    if not isinstance(ref.concept, str) or not ref.concept.strip() \
            or any(ch.isspace() for ch in ref.concept):
        return "field ref concept must be a non-empty id with no whitespace"
    if not isinstance(ref.column, str) or not ref.column:
        return "field ref column must be a non-empty column name"
    try:
        validate_column_name(ref.column)
    except ValueError:
        return f"field ref column {ref.column!r} is not a SQL identifier"
    return None


def check_measure_shape(measure: object) -> str | None:
    """A problem string for a STRUCTURALLY unacceptable duration measure
    (kind, unit, boolean clamp, field refs), ``None`` if ok, used by the SQL
    composers, which support either clamp mode. Total over hostile input."""
    if not isinstance(measure, DurationMeasure):
        return "must be a duration measure object"
    if measure.kind != DURATION_KIND:
        return f"measure kind must be {DURATION_KIND!r}"
    if measure.unit not in _UNIT_DIVISOR:
        return f"measure unit must be one of {list(DURATION_UNITS)}"
    if not isinstance(measure.clamp_negative, bool):
        return "measure clamp_negative must be a boolean"
    start_problem = check_field_ref(measure.start)
    if start_problem:
        return f"measure start {start_problem}"
    end_problem = check_field_ref(measure.end)
    if end_problem:
        return f"measure end {end_problem}"
    return None


def check_measure(measure: object) -> str | None:
    """A problem string for an unacceptable duration measure, ``None`` if ok:
    the DEFINITION/layer policy: structurally valid AND ``clamp_negative`` is
    true (this measure kind always clamps negatives at 0). Total over
    hostile input."""
    shape = check_measure_shape(measure)
    if shape:
        return shape
    if measure.clamp_negative is not True:
        return ("measure clamp_negative must be true for a duration measure "
                "(negative durations are clamped at 0 by this metric kind)")
    return None


def check_percentiles(values: object) -> str | None:
    """A problem string for an unacceptable percentile list, ``None`` if ok.

    1..5 finite floats, each strictly 0 < p < 1, no repeats, no booleans.
    Total over hostile input."""
    if not isinstance(values, (tuple, list)):
        return "percentiles must be a list of 1..5 numbers"
    if not 1 <= len(values) <= MAX_PERCENTILES:
        return f"percentiles must carry 1..{MAX_PERCENTILES} values"
    seen: set[float] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "each percentile must be a number strictly between 0 and 1"
        number = float(value)
        if not math.isfinite(number) or not 0.0 < number < 1.0:
            return "each percentile must be a finite number strictly between 0 and 1"
        if number in seen:
            return "percentiles must not repeat"
        seen.add(number)
    return None


# --- SQL composition (identifiers validated; vocabularies closed) ----------- #

def _col(column: str, qualifier: str) -> str:
    return f"{qualifier}{quote_column_identifier(column)}"


def render_number(value: float) -> str:
    """A percentile literal: the exact float repr (``0.9``), trimmed for an
    integer-valued divisor (``3600.0``)."""
    number = float(value)
    return repr(number)


def render_duration_expr(measure: DurationMeasure, *, dialect: str,
                         qualifier: str = "", casts: "dict | None" = None) -> str:
    """The row-level duration expression in ``unit``, negatives clamped when
    the measure says so. Athena: ``date_diff('second', start, end)``;
    PostgreSQL: ``extract(epoch from (end - start))``. Composed only from
    validated identifiers and the closed unit divisor.

    ``casts`` optionally maps ``"start"``/``"end"`` to a physical type
    classification (``"date"`` ⇒ the column is wrapped in a dialect cast to
    timestamp); ``"timestamp"`` or absent ⇒ the column is used as-is, so a
    timestamp measure's SQL is byte-identical to before this parameter."""
    problem = check_measure_shape(measure)
    if problem:
        raise ValueError(problem)
    if qualifier not in _QUALIFIERS:
        raise ValueError(f"unsupported qualifier: {qualifier!r}")
    if dialect not in _DIALECTS:
        raise ValueError(f"unsupported dialect: {dialect!r}")
    casts = casts or {}
    start = _maybe_cast(_col(measure.start.column, qualifier), casts.get("start"), dialect)
    end = _maybe_cast(_col(measure.end.column, qualifier), casts.get("end"), dialect)
    if dialect == "athena":
        seconds = f"date_diff('second', {start}, {end})"
    elif dialect == "duckdb":
        seconds = _duckdb_whole_seconds(start, end)
    else:
        seconds = f"extract(epoch from ({end} - {start}))"
    if measure.clamp_negative:
        seconds = f"greatest({seconds}, 0)"
    divisor = _UNIT_DIVISOR[measure.unit]
    if divisor == 1.0:
        return seconds
    return f"{seconds} / {render_number(divisor)}"


def _duckdb_whole_seconds(start: str, end: str) -> str:
    """Whole seconds from ``start`` to ``end`` in DuckDB, truncated toward
    zero as Athena's ``date_diff('second', ...)`` is. DuckDB's own
    ``date_diff`` counts second BOUNDARIES crossed (0.9s -> 1.1s is 1), so it
    would disagree with Athena on every sub-second span."""
    return (f"CAST(trunc((epoch_us({end}) - epoch_us({start})) / 1000000.0) "
            "AS BIGINT)")


def _maybe_cast(ref: str, type_class: object, dialect: str) -> str:
    """Wrap ``ref`` in a dialect cast to timestamp when its column is a DATE; a timestamp column (or no classification) is returned unchanged."""
    if type_class != "date":
        return ref
    if dialect == "athena":
        return f"cast({ref} AS timestamp)"
    return f"({ref})::timestamp"


def render_percentile_select(expr: str, percentiles, *, dialect: str) -> str:
    """The comma-joined percentile SELECT terms over the duration ``expr``.
    Athena: ``approx_percentile(expr, p)`` (approximate, labelled in lineage);
    PostgreSQL: ``percentile_disc(p) WITHIN GROUP (ORDER BY expr)``;
    DuckDB: ``quantile_disc(expr, p)`` (exact, the same definition as
    PostgreSQL's ``percentile_disc``)."""
    problem = check_percentiles(percentiles)
    if problem:
        raise ValueError(problem)
    if dialect not in _DIALECTS:
        raise ValueError(f"unsupported dialect: {dialect!r}")
    terms = []
    for p in percentiles:
        literal = render_number(p)
        if dialect == "athena":
            terms.append(f"approx_percentile({expr}, {literal})")
        elif dialect == "duckdb":
            terms.append(f"quantile_disc({expr}, {literal})")
        else:
            terms.append(f"percentile_disc({literal}) WITHIN GROUP (ORDER BY {expr})")
    return ", ".join(terms)


def null_exclusion_where(measure: DurationMeasure, *, qualifier: str = "") -> str:
    """The ``start IS NOT NULL AND end IS NOT NULL`` guard (decision 3: rows
    where either timestamp is NULL are excluded). Identifiers validated."""
    start = _col(measure.start.column, qualifier)
    end = _col(measure.end.column, qualifier)
    return f"{start} IS NOT NULL AND {end} IS NOT NULL"


def dialect_is_approximate(dialect: str) -> bool:
    """ Athena's ``approx_percentile`` is an ESTIMATE; PostgreSQL's
    ``percentile_disc`` is exact. Stamped on the plan/lineage independently of
    any scan narrowing so the rendered block can say 'approximate' only for
    Athena. Unsupported dialects are rejected elsewhere (fail-closed)."""
    return dialect == "athena"


#: the physical column types a duration measure accepts, classified for
#: casting and for type-aware window literals. A timestamp WITH a time zone is
#: ``"zoned"``, a bare timestamp/datetime is ``"unzoned"``, a date is ``"date"``
#: (cast to timestamp per dialect), anything else WITHHOLDS. Matching is a
#: lowercase test, so ``timestamp(6) with time zone`` ⇒ zoned and ``timestamp(6)``
#: ⇒ unzoned. Closed vocabulary, no user input.
_TIMESTAMP_TYPE_PREFIXES: tuple[str, ...] = ("timestamp", "datetime")
_DATE_TYPE_PREFIXES: tuple[str, ...] = ("date",)
#: The temporal classifications a measure column may carry (all accepted).
TEMPORAL_TYPES: frozenset[str] = frozenset({"zoned", "unzoned", "date"})


def classify_timestamp_type(data_type: object) -> str:
    """``"zoned"``, ``"unzoned"``, ``"date"`` or ``"unsupported"`` for a physical
    column type string. A timestamp/datetime carrying a time zone (``with
    time zone`` / ``tz`` / a trailing offset) is zoned, else unzoned; a date is
    date; anything else is unsupported (withhold, never a guess). Total over
    hostile input."""
    if not isinstance(data_type, str):
        return "unsupported"
    norm = data_type.strip().lower()
    if not norm:
        return "unsupported"
    # a bare "date" is a date; "date" as a prefix of "datetime" must not win.
    if any(norm == p or norm.startswith(p + "(") or norm.startswith(p + " ")
           for p in _DATE_TYPE_PREFIXES) and not norm.startswith("datetime"):
        return "date"
    if any(norm.startswith(p) for p in _TIMESTAMP_TYPE_PREFIXES):
        zoned = ("with time zone" in norm or norm.endswith("tz")
                 or "timestamptz" in norm or "+" in norm)
        return "zoned" if zoned else "unzoned"
    return "unsupported"


def absolute_window_where(column: str, start_iso: str, end_iso: str, *,
                          dialect: str, qualifier: str = "",
                          col_type: str = "zoned") -> str:
    """A half-open ``[start, end)`` bound on ``column`` from two ISO-8601 UTC
    instants (the turn's resolved absolute window, applied to the
    measure's END column). The literal MATCHES the bound column's physical type
    (``col_type``: ``"zoned"`` | ``"unzoned"`` | ``"date"``), so a PostgreSQL
    ``timestamp without time zone`` column gets a ``TIMESTAMP`` literal with the
    offset dropped (the instant is UTC) and a zoned column gets ``TIMESTAMPTZ``;
    Trino gets ``from_iso8601_timestamp`` for a zoned column and a bare
    ``TIMESTAMP`` for an unzoned one. Identifiers validated; instants
    charset-gated, so no arbitrary text reaches SQL."""
    if dialect not in _DIALECTS:
        raise ValueError(f"unsupported dialect: {dialect!r}")
    if qualifier not in _QUALIFIERS:
        raise ValueError(f"unsupported qualifier: {qualifier!r}")
    lo = _timestamp_literal(start_iso, dialect, col_type)
    hi = _timestamp_literal(end_iso, dialect, col_type)
    ident = _col(column, qualifier)
    return f"{ident} >= {lo} AND {ident} < {hi}"  # nosec B608 - validated ident + literal


_ISO_INSTANT_CHARS = frozenset("0123456789-:T.Z+ ")


def _timestamp_literal(iso: object, dialect: str, col_type: str = "zoned") -> str:
    """A dialect TIMESTAMP literal from an ISO-8601 instant, charset-gated and
    MATCHED to the bound column's type. A trailing ``Z`` becomes ``+00:00``.
    A zoned column keeps the offset (Trino ``from_iso8601_timestamp`` / PG
    ``TIMESTAMPTZ``); an unzoned column or a cast date drops the offset to a
    naive UTC instant (``TIMESTAMP``). Raises on anything unparseable."""
    if not isinstance(iso, str) or not iso.strip():
        raise ValueError("window instant must be a non-empty ISO-8601 string")
    text = iso.strip()
    if any(ch not in _ISO_INSTANT_CHARS for ch in text) or "'" in text:
        raise ValueError(f"window instant {iso!r} is not an ISO-8601 instant")
    zoned_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    naive_text = _strip_offset(zoned_text)
    if col_type == "zoned":
        if dialect == "athena":
            return f"from_iso8601_timestamp('{zoned_text}')"
        return f"TIMESTAMPTZ '{zoned_text}'"
    # unzoned column or a cast date: a naive UTC instant literal.
    return f"TIMESTAMP '{naive_text}'"


def _strip_offset(text: str) -> str:
    """Drop a trailing ``+HH:MM`` / ``-HH:MM`` offset from an ISO instant, so a
    naive-timestamp literal carries the UTC wall time (the instant is UTC)."""
    body = text
    for sign in ("+", "-"):
        idx = body.find(sign, 11)  # past the date's own hyphens (YYYY-MM-DD)
        if idx != -1:
            body = body[:idx]
            break
    return body
