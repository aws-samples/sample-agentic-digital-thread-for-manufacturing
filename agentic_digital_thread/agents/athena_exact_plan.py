# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read an Iceberg table's file statistics into the groups the box planner cuts.

The split columns (``athena_exact_boxes.Dim``) are the table's PARTITION
SOURCE column (the column a partition field such as ``<col>_day`` is computed
from) and up to :data:`MAX_EVENT_DIMS` temporal columns the query itself
bounds. Files are placed on the partition column by the partition value in
their path, and on an event column by their value bounds. A file with no
partition value in its path and no bounds on the partition column (written
under an older, unpartitioned spec) is placed at that column's NULL end: its rows are older than the
partitioned data, or NULL when the column was added after the file was
written, and the engine prunes it from every higher range by the file's own
row-group statistics, which the file list does not carry (measured on a real
table: pieces above the low end read none of it). When that guess is wrong
the piece that reads it is refused or measures high, and the queue is
re-planned at the new calibration; exactness never depends on it. A file
that can be placed on neither way on an event column (no bounds) is
unplaceable there: every piece reads it.

Two reads of the metadata relation ``"<table>$files"``:

1. ``coverage_sql``: which partition fields the files' paths carry, and how
   many bytes each covers, so the source column most data is partitioned on
   is chosen;
2. ``planning_sql``: one row per set of files placed alike (partition value;
   event bounds as days, clipped to the query's range widened by a day),
   with the estimated bytes a piece reads from them. Only files whose bounds
   can overlap the query's own bounds are placed (the engine prunes the rest
   with the query's WHERE); they collapse into one unplaced row.

The estimate per group is the sizes of the query's own columns plus a
per-file overhead (``match_bytes``), and, per split column the query does not
read already, that column's size (``x<d>``): a piece reads it only when its
box filters on that column (``Group.extra``). A column whose size the file
does not report counts an average column's share of the file. A delete file
(``content`` other than 0) counts its whole size and sits on the partition
value only. Measured on a real table the estimate under-counted the bytes
the engine scanned by 1.3x to 2.1x, so the caller calibrates and halves a
piece the engine still refuses. Everything here except
:func:`read_table_meta` is pure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .athena_exact_boxes import Dim, Group, Span
from .athena_exact_shape import Shape

_TRANSFORM_SUFFIXES = (("_day", "day"), ("_hour", "hour"), ("_month", "month"), ("_year", "year"))
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Bytes a piece reads per file whatever its columns (footer and metadata).
FILE_OVERHEAD_BYTES = 16_384
MAX_BOUNDED_COLUMNS = 3
#: Event columns used as split dimensions (each multiplies the planning rows).
MAX_EVENT_DIMS = 2
_NULL_VARCHAR = "CAST(NULL AS varchar)"


@dataclass(frozen=True)
class TableMeta:
    columns: dict[str, tuple[str, str]]
    field_ids: dict[str, int]
    iceberg: bool


@dataclass(frozen=True)
class Candidate:
    column: str
    col_type: str
    transform: str
    field: str
    covered_bytes: int


@dataclass(frozen=True)
class Layout:
    dims: tuple
    groups: tuple
    newest: dict
    unplaced_files: int = 0
    #: Files placed at the partition column's NULL end for want of a value.
    low_end_files: int = 0

    @property
    def total_est(self) -> int:
        return sum(g.est for g in self.groups)

    def without_low_end_guess(self) -> "Layout":
        """The same files with the NULL-end guess dropped: a file placed there
        for want of a value is unplaceable on the partition column again
        (every piece reads it)."""
        groups = tuple(Group((None,) + g.spans[1:], g.est, g.files, g.extra)
                       if g.spans and g.spans[0] == LOW_END else g for g in self.groups)
        return Layout(self.dims, groups, self.newest, self.unplaced_files, 0)


# --------------------------------------------------------------------------- #
# Table metadata (field ids from the catalog) and the split columns
# --------------------------------------------------------------------------- #
def _glue_table(glue, catalog_id: str | None, database: str, table: str) -> dict:
    kwargs = {"DatabaseName": database, "Name": table}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id
    found = glue.get_table(**kwargs)["Table"]
    target = found.get("TargetTable")
    if isinstance(target, dict) and target.get("Name"):
        return _glue_table(glue, target.get("CatalogId"), target.get("DatabaseName", database),
                           target["Name"])
    return found


def read_table_meta(glue, catalog_id: str | None, database: str, table: str) -> TableMeta:
    """Columns, Iceberg field ids and format from the Glue catalog entry
    (a resource link is followed once). Raises on any read failure."""
    found = _glue_table(glue, catalog_id, database, table)
    params = {str(k).lower(): str(v).lower() for k, v in (found.get("Parameters") or {}).items()}
    columns: dict[str, tuple[str, str]] = {}
    field_ids: dict[str, int] = {}
    for column in (found.get("StorageDescriptor") or {}).get("Columns") or []:
        name = str(column.get("Name") or "")
        if not name:
            continue
        columns[name.lower()] = (name, str(column.get("Type") or ""))
        raw = (column.get("Parameters") or {}).get("iceberg.field.id")
        if str(raw or "").strip().isdigit():
            field_ids[name.lower()] = int(str(raw).strip())
    return TableMeta(columns, field_ids, params.get("table_type") == "iceberg")


def _temporal(col_type: str) -> bool:
    return col_type.lower().startswith(("date", "timestamp"))


def candidates(field_bytes: dict[str, int], meta: TableMeta) -> list[Candidate]:
    """Partition fields computed from a date/timestamp column, most bytes first."""
    out = []
    for field, nbytes in field_bytes.items():
        lower = str(field).lower()
        if not _FIELD_NAME.match(lower):
            continue
        found = None
        for suffix, transform in _TRANSFORM_SUFFIXES:
            source = lower[:-len(suffix)]
            if lower.endswith(suffix) and source in meta.columns and _temporal(meta.columns[source][1]):
                if transform != "hour" or not meta.columns[source][1].lower().startswith("date"):
                    found = (source, transform)
        if found is None and lower in meta.columns and meta.columns[lower][1].lower().startswith("date"):
            found = (lower, "identity")
        if found:
            name, col_type = meta.columns[found[0]]
            out.append(Candidate(name, col_type, found[1], str(field), int(nbytes or 0)))
    return sorted(out, key=lambda c: (-c.covered_bytes, c.field))


def _real_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def bounded_columns(shape: Shape, meta: TableMeta) -> list[tuple[str, str | None, str | None, int]]:
    """``(column, lo_day, hi_day, field_id)`` for each temporal column the
    query bounds that has a field id (at most :data:`MAX_BOUNDED_COLUMNS`)."""
    out = []
    for column, (lo, hi) in sorted(shape.bounds.items()):
        if column in meta.field_ids and column in meta.columns and _temporal(meta.columns[column][1]):
            if (lo or hi) and all(d is None or _real_day(d) for d in (lo, hi)):
                out.append((meta.columns[column][0], lo, hi, meta.field_ids[column]))
    return out[:MAX_BOUNDED_COLUMNS]


def _day_start(text: str | None) -> datetime | None:
    return datetime.strptime(text, "%Y-%m-%d") if text else None


def _query_range(lo: str | None, hi: str | None) -> tuple:
    """The query's own day bounds as ``[lo, hi)`` (the upper day is included)."""
    return _day_start(lo), (_day_start(hi) + timedelta(days=1) if hi else None)


def split_dims(cand: Candidate | None, shape: Shape, meta: TableMeta) -> tuple:
    """The split columns: the partition source column (when there is one),
    then the query's bounded temporal columns (two-sided bounds first). Each
    carries the query's own bounds on it, so pieces are planned clipped."""
    bounded = sorted(bounded_columns(shape, meta), key=lambda b: (b[1] is None or b[2] is None, b[0]))
    ranges = {c.lower(): _query_range(lo, hi) for c, lo, hi, _f in bounded}
    dims = ([Dim(cand.column, cand.col_type, "partition", *ranges.get(cand.column.lower(), (None, None)))]
            if cand else [])
    taken = {cand.column.lower()} if cand else set()
    for column, _lo, _hi, _fid in bounded:
        if column.lower() in taken or len(dims) - bool(cand) >= MAX_EVENT_DIMS:
            continue
        dims.append(Dim(column, meta.columns[column.lower()][1], "event", *ranges[column.lower()]))
        taken.add(column.lower())
    return tuple(dims)


# --------------------------------------------------------------------------- #
# The two "$files" reads
# --------------------------------------------------------------------------- #
def coverage_sql(relation: str) -> str:
    """Bytes covered by each partition field named in the files' paths."""
    return (
        f"SELECT f AS field, sum(file_size_in_bytes) AS bytes FROM {relation} "  # nosec B608 - relation is quoted fail-closed by the caller; no untrusted values
        "CROSS JOIN UNNEST(regexp_extract_all(file_path, '/([A-Za-z_][A-Za-z0-9_]*)=[^/]*', 1)) "
        "AS u (f) GROUP BY f"
    )


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _overlap_sql(field_id: int, lo: str | None, hi: str | None) -> str:
    """A file can hold a value in ``[lo, hi]`` (days, widened by one) unless
    its ISO bounds say otherwise; an unreadable bound counts as overlapping."""
    parts = []
    iso = "'^[0-9]{4}-[0-9]{2}-[0-9]{2}'"
    if lo:
        bound = f"element_at(upper_bounds, {int(field_id)})"
        parts.append(f"(NOT regexp_like(coalesce({bound}, ''), {iso}) "
                     f"OR substr({bound}, 1, 10) >= '{_shift(lo, -1)}')")
    if hi:
        bound = f"element_at(lower_bounds, {int(field_id)})"
        parts.append(f"(NOT regexp_like(coalesce({bound}, ''), {iso}) "
                     f"OR substr({bound}, 1, 10) <= '{_shift(hi, 1)}')")
    return " AND ".join(parts) or "TRUE"


def estimate_columns(shape: Shape, meta: TableMeta, dims=()) -> list[str]:
    """The columns the query itself reads (a split column it does not read is
    counted separately, only for the pieces that filter on it)."""
    return [c for c in meta.columns if c in shape.referenced]


def _column_size_sql(meta: TableMeta, column: str) -> str:
    share = f"file_size_in_bytes / {max(1, len(meta.columns))}"
    if column in meta.field_ids:
        return f"coalesce(element_at(column_sizes, {meta.field_ids[column]}), {share})"
    return share


def _estimate_sql(shape: Shape, meta: TableMeta, dims=()) -> str:
    terms = [_column_size_sql(meta, c) for c in estimate_columns(shape, meta, dims)]
    data = " + ".join([*terms, str(FILE_OVERHEAD_BYTES)])
    return f"CASE WHEN content <> 0 THEN file_size_in_bytes ELSE {data} END"


def _extra_sql(shape: Shape, meta: TableMeta, dim) -> str:
    """Bytes a piece adds from one file by filtering on ``dim``'s column."""
    column = dim.column.lower()
    if column in shape.referenced or column not in meta.columns:
        return "0"
    return f"CASE WHEN content <> 0 THEN 0 ELSE {_column_size_sql(meta, column)} END"


def _clipped(bounds: str, fid: int, clip: str | None, keep: str) -> str:
    raw = f"substr(element_at({bounds}, {int(fid)}), 1, 10)"
    value = f"{keep}({raw}, '{clip}')" if clip else raw
    return f"CASE WHEN content = 0 THEN {value} END"


def _event_columns(dims, shape: Shape, meta: TableMeta) -> tuple[list[str], list[tuple[str, int]]]:
    """The inner SELECT's event-bound expressions, and ``(column, field id)``
    of each event dimension (its newest bound is reported too)."""
    by_column = {c.lower(): (lo, hi) for c, lo, hi, _f in bounded_columns(shape, meta)}
    exprs, events = [], []
    for i, dim in enumerate(d for d in dims if d.kind == "event"):
        fid = meta.field_ids[dim.column.lower()]
        lo, hi = by_column.get(dim.column.lower(), (None, None))
        lo_clip = _shift(lo, -1) if lo else None
        hi_clip = _shift(hi, 1) if hi else None
        exprs.append(f"{_clipped('lower_bounds', fid, lo_clip, 'greatest')} AS e{i}lo, "
                     f"{_clipped('upper_bounds', fid, hi_clip, 'least')} AS e{i}hi, "
                     f"CASE WHEN content = 0 THEN element_at(upper_bounds, {int(fid)}) END AS n{i}")
        events.append((dim.column, fid))
    return exprs, events


def _partition_columns(cand: Candidate | None, meta: TableMeta) -> str:
    if cand is None:
        return f"{_NULL_VARCHAR} AS pv, {_NULL_VARCHAR} AS plo, {_NULL_VARCHAR} AS phi"
    if not _FIELD_NAME.match(cand.field):
        raise ValueError(f"invalid partition field name: {cand.field!r}")
    pid = meta.field_ids.get(cand.column.lower())
    lo = (f"CASE WHEN content = 0 THEN substr(element_at(lower_bounds, {pid}), 1, 10) END"
          if pid else _NULL_VARCHAR)
    hi = (f"CASE WHEN content = 0 THEN substr(element_at(upper_bounds, {pid}), 1, 10) END"
          if pid else _NULL_VARCHAR)
    return (f"regexp_extract(file_path, '/{cand.field}=([^/]*)/', 1) AS pv, "
            f"{lo} AS plo, {hi} AS phi")


def planning_sql(relation: str, cand: Candidate | None, dims, meta: TableMeta,
                 shape: Shape) -> tuple[str, list[str]]:
    """The planning read, and the event columns whose newest bound it reports.

    Columns: ``m``, ``pv``, ``plo``, ``phi``, then ``e<i>lo``/``e<i>hi`` per
    event dimension, ``files``, ``match_bytes``, ``file_bytes``, ``n<i>`` per
    event dimension, then ``x<d>`` per split column."""
    match = " AND ".join(_overlap_sql(fid, lo, hi) for _c, lo, hi, fid in bounded_columns(shape, meta))
    exprs, events = _event_columns(dims, shape, meta)
    keys = ["pv", "plo", "phi"] + [f"e{i}{end}" for i in range(len(events)) for end in ("lo", "hi")]
    placed = ", ".join(
        f"CASE WHEN m AND pv IS NULL THEN {k} END AS {k}" if k in ("plo", "phi")
        else f"CASE WHEN m THEN {k} END AS {k}" for k in keys)
    newest = "".join(f", max(n{i}) AS n{i}" for i in range(len(events)))
    extras = "".join(f", sum(CASE WHEN m THEN x{d} ELSE 0 END) AS x{d}" for d in range(len(dims)))
    inner = (
        f"SELECT (content <> 0 OR ({match or 'TRUE'})) AS m, {_partition_columns(cand, meta)}"  # nosec B608 - identifiers validated (_FIELD_NAME / caller quoting); field ids are ints
        + "".join(f", {e}" for e in exprs)
        + f", ({_estimate_sql(shape, meta, dims)}) AS est"
        + "".join(f", ({_extra_sql(shape, meta, d)}) AS x{i}" for i, d in enumerate(dims))
        + f", file_size_in_bytes AS fbytes FROM {relation}"
    )
    group_by = ", ".join(str(i) for i in range(1, len(keys) + 2))
    sql = (
        f"SELECT m, {placed}, count(*) AS files, sum(CASE WHEN m THEN est ELSE 0 END) AS match_bytes, "  # nosec B608 - composed from the validated inner query above; group_by is a list of ints
        f"sum(fbytes) AS file_bytes{newest}{extras} FROM ({inner}) GROUP BY {group_by}"
    )
    return sql, [c for c, _fid in events]


# --------------------------------------------------------------------------- #
# Parse the plan rows
# --------------------------------------------------------------------------- #
def _add_months(moment: datetime, months: int) -> datetime:
    total = moment.year * 12 + moment.month - 1 + months
    return moment.replace(year=total // 12, month=total % 12 + 1)


def partition_range(cand: Candidate, value: str) -> tuple[datetime, datetime] | None:
    """``[start, end)`` of one partition value as the path prints it."""
    text = str(value or "")
    try:
        if cand.transform in {"day", "identity"} and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            start = datetime.strptime(text, "%Y-%m-%d")
            return start, start + timedelta(days=1)
        if cand.transform == "hour" and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}", text):
            start = datetime.strptime(text, "%Y-%m-%d-%H")
            return start, start + timedelta(hours=1)
        if cand.transform == "month" and re.fullmatch(r"\d{4}-\d{2}", text):
            start = datetime.strptime(text, "%Y-%m")
            return start, _add_months(start, 1)
        if cand.transform == "year" and re.fullmatch(r"\d{4}", text):
            start = datetime.strptime(text, "%Y")
            return start, start.replace(year=start.year + 1)
    except ValueError:
        return None
    return None


def _int(cell) -> int:
    try:
        return max(0, int(str(cell)))
    except (TypeError, ValueError):
        return 0


def _day(cell) -> datetime | None:
    text = str(cell or "")
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d") if _ISO_DAY.match(text) else None
    except ValueError:
        return None


#: The partition-column span of a file with neither a partition value nor
#: bounds there: its NULL end (see the module doc), marked as a guess.
LOW_END = Span(None, None, nulls=True, assumed=True)


def partition_span(cand: Candidate, pv, plo, phi) -> Span | None:
    """Where a file sits on the partition column: its partition value, the
    NULL partition, its own bounds (widened; it may hold NULLs), the NULL end
    when it has neither (:data:`LOW_END`), or nowhere (an unreadable value)."""
    if pv is not None and str(pv) == "null":
        return Span(None, None, nulls=True)
    if pv is not None:
        found = partition_range(cand, str(pv))
        return Span(found[0], found[1]) if found else None
    lo, hi = _day(plo), _day(phi)
    if lo is None or hi is None:
        return LOW_END
    return Span(lo - timedelta(days=1), hi + timedelta(days=2), nulls=True)


def event_span(lo_cell, hi_cell) -> Span | None:
    """Where a file sits on an event column, by its day bounds, or nowhere.
    NULLs never matter there: the query's own comparison excludes them."""
    lo, hi = _day(lo_cell), _day(hi_cell)
    if lo is None or hi is None or hi < lo:
        return None
    return Span(lo, hi + timedelta(days=1))


def _iso_moment(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _truthy(cell) -> bool:
    return str(cell).strip().lower() in {"true", "1", "t"}


def _note_newest(newest: dict, columns: list[str], cells: list) -> None:
    for column, text in zip(columns, cells):
        moment = _iso_moment(str(text)) if text is not None else None
        best = newest.get(column)
        if moment is not None and (best is None or moment > best[0]):
            newest[column] = (moment, str(text))


def parse_plan(rows: list[list], cand: Candidate | None, dims, newest_columns: list[str]) -> Layout:
    """The planning rows as groups (one span per dimension), plus the newest
    bound per event column."""
    events = len(newest_columns)
    width = 4 + 2 * events + 3
    groups, newest, unplaced, low_end = [], {}, 0, 0
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < width + events + len(dims):
            raise ValueError("a planning row has the wrong shape")
        cells = list(row)
        _note_newest(newest, newest_columns, cells[width:width + events])
        files, est = _int(cells[width - 3]), _int(cells[width - 2])
        if not _truthy(cells[0]):
            unplaced += files
            continue
        spans = [partition_span(cand, cells[1], cells[2], cells[3])] if cand else []
        if cand and cells[1] is None and spans[0] == LOW_END:
            low_end += files
        spans += [event_span(cells[4 + 2 * i], cells[5 + 2 * i]) for i in range(events)]
        extra = tuple(_int(c) for c in cells[width + events:width + events + len(dims)])
        groups.append(Group(tuple(spans), est, files, extra))
    return Layout(tuple(dims), tuple(groups), {c: v[1] for c, v in newest.items()}, unplaced,
                  low_end)
