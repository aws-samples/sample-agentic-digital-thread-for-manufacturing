# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Boxes: the pieces of an exact-total split, one half-open range per split column.

A split column ("dimension") is either the table's partition source column or
a temporal column the query itself bounds. A box gives each dimension one
range ``[lo, hi)``; ``lo is None`` means "from the lowest value, NULL
included" and ``hi is None`` means "to the highest value". The planner starts
from the box that covers every dimension's whole domain and only ever cuts a
box in two at one point on one dimension, so the leaves always tile the whole
row space: every row, NULLs included, lands in exactly one leaf whatever the
query's own WHERE says. Exactness never depends on the estimates below.

Where to cut comes from file statistics grouped into :class:`Group` rows (one
per set of files placed alike). A group has one :class:`Span` per dimension:
placed by a partition value or by the files' value bounds, or ``None`` when
the files cannot be placed on that dimension (an unpartitioned file has no
partition value, a file may carry no bounds). An unplaceable group is read by
every box, so it can never be cut away on that dimension.

Every piece also carries the query's own WHERE, so on a dimension the query
bounds (``Dim.lo``/``Dim.hi``) a box only ever reads that part of its range:
estimates, floors and cut points use the box CLIPPED to the query's bounds
(:func:`effective`), while the box itself stays open-ended so the leaves
still tile everything. A group's ``est`` is what a piece reads from it for
the query's own columns; ``extra[d]`` is what it adds when the piece also
filters on split column ``d`` the query does not read already.

* :func:`estimate` is the bytes a box would read: every group that can match
  the clipped box on every dimension;
* :func:`floor` is the part no further cut can remove (groups that cover the
  clipped box on every dimension), and :func:`irreducible` adds what the
  piece holding the box's low corner always reads (a group at the NULL end of
  a dimension the box is open-ended below on stays in that piece);
* :func:`plan_boxes` cuts the largest box over the target at the dimension
  and point that need the fewest pieces for its two children (then the
  smaller larger child), stops cutting a box whose floor is already at the
  target or that no cut makes smaller (such a box runs whole), and refuses
  when the leaves would be more than the piece cap;
* :func:`split_box` halves a box the engine still refused, by the estimate
  when it helps and otherwise at a time midpoint.

Pure and offline.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import accumulate

Rng = tuple  # (lo: datetime | None, hi: datetime | None)


@dataclass(frozen=True)
class Dim:
    """One split column. ``lo``/``hi`` are the query's own bounds on it as
    days (``None`` where it has none): every piece reads only that range."""
    column: str
    col_type: str
    kind: str
    lo: datetime | None = None
    hi: datetime | None = None

    @property
    def is_date(self) -> bool:
        return self.col_type.strip().lower().startswith("date")

    @property
    def zoned(self) -> bool:
        return "with time zone" in self.col_type.lower()


@dataclass(frozen=True)
class Span:
    """Where a group sits on one dimension: ``[start, end)``, or only NULL
    values (``start`` and ``end`` both ``None``). ``nulls`` says the files may
    also hold NULLs there; ``assumed`` that the placement is a guess the run
    may drop (``athena_exact_plan.LOW_END``)."""
    start: datetime | None
    end: datetime | None
    nulls: bool = False
    assumed: bool = False

    @property
    def only_null(self) -> bool:
        return self.start is None and self.end is None


@dataclass(frozen=True)
class Group:
    spans: tuple
    est: int
    files: int = 0
    extra: tuple = ()

    def weight(self, restricted) -> int:
        """Bytes a piece reads from this group when it filters on ``restricted``."""
        return self.est + sum(self.extra[d] for d in restricted if d < len(self.extra))


class TooManyPieces(ValueError):
    """The plan needs more leaves than the piece cap allows."""


def full_box(dims) -> tuple:
    return tuple((None, None) for _ in dims)


def restricted(box) -> tuple:
    """The dimensions a box's predicate filters on."""
    return tuple(d for d, rng in enumerate(box) if rng != (None, None))


def _clip(rng: Rng, dim: Dim) -> Rng:
    lo, hi = rng
    if dim.lo is not None and (lo is None or lo < dim.lo):
        lo = dim.lo
    if dim.hi is not None and (hi is None or hi > dim.hi):
        hi = dim.hi
    return lo, hi


def effective(box, dims=()) -> tuple:
    """``box`` clipped to the query's own bounds on each dimension (the rows a
    piece can read). Without ``dims`` the box is returned as it is."""
    if not dims:
        return tuple(box)
    return tuple(_clip(rng, dim) for rng, dim in zip(box, dims))


def _empty(box) -> bool:
    return any(lo is not None and hi is not None and lo >= hi for lo, hi in box)


def _placed_overlap(span: Span, rng: Rng) -> bool:
    lo, hi = rng
    return (hi is None or span.start < hi) and (lo is None or span.end > lo)


def overlaps(span: Span | None, rng: Rng) -> bool:
    """Can a file placed at ``span`` hold a row inside ``rng``?"""
    if span is None:
        return True
    if span.nulls and rng[0] is None:
        return True
    return not span.only_null and _placed_overlap(span, rng)


def covers(span: Span | None, rng: Rng) -> bool:
    """Is ``span`` inside both halves of every cut of ``rng``?"""
    if span is None:
        return True
    lo, hi = rng
    return (not span.only_null and lo is not None and hi is not None
            and span.start <= lo and span.end >= hi)


def in_box(group: Group, box, dims=()) -> bool:
    eff = effective(box, dims)
    return not _empty(eff) and all(overlaps(s, r) for s, r in zip(group.spans, eff))


def estimate(groups, box, dims=()) -> int:
    """Estimated bytes one piece over ``box`` reads."""
    used = restricted(box)
    return sum(g.weight(used) for g in groups if in_box(g, box, dims))


def floor(groups, box, dims=()) -> int:
    """The part of :func:`estimate` that no cut of ``box`` can remove."""
    eff, used = effective(box, dims), restricted(box)
    if _empty(eff):
        return 0
    return sum(g.weight(used) for g in groups
               if all(covers(s, r) for s, r in zip(g.spans, eff)))


def _pinned(span: Span | None, rng: Rng) -> bool:
    """Is a group at ``span`` in the piece holding the low corner of ``rng``
    however ``rng`` is cut (it covers the range, or sits at its NULL end)?"""
    return covers(span, rng) or (span is not None and span.nulls and rng[0] is None)


def irreducible(groups, box, dims=()) -> int:
    """A lower bound on the biggest piece any split of ``box`` leaves: the
    larger of :func:`floor` and what the piece holding its low corner reads."""
    eff, used = effective(box, dims), restricted(box)
    if _empty(eff):
        return 0
    low = sum(g.weight(used) for g in groups
              if all(_pinned(s, r) for s, r in zip(g.spans, eff)))
    return max(low, floor(groups, box, dims))


def cut(box, dim: int, point: datetime) -> tuple:
    """The two boxes ``box`` splits into at ``point`` on dimension ``dim``."""
    lo, hi = box[dim]
    left = box[:dim] + ((lo, point),) + box[dim + 1:]
    right = box[:dim] + ((point, hi),) + box[dim + 1:]
    return left, right


def union(a, b) -> tuple | None:
    """The one box ``a`` and ``b`` make together, or ``None``: they must agree
    on every dimension but one, and meet there end to start. Replacing two
    leaves by their union keeps the leaves tiling every row exactly once."""
    differ = [d for d, (ra, rb) in enumerate(zip(a, b)) if ra != rb]
    if len(differ) != 1:
        return None
    d = differ[0]
    low, high = (a, b) if a[d][1] is not None and a[d][1] == b[d][0] else (b, a)
    if low[d][1] is None or low[d][1] != high[d][0]:
        return None
    return a[:d] + ((low[d][0], high[d][1]),) + a[d + 1:]


def _dim_scores(groups, box, eff, dim: int):
    """``(score, both, point)`` for every useful cut point on ``dim`` inside the
    clipped range: score is the larger child's estimate, ``both`` the two
    children's sum (a cut on a new dimension adds that column's bytes)."""
    lo, hi = eff[dim]
    used = tuple(sorted(set(restricted(box)) | {dim}))
    always_left, both_sides, starts, ends = 0, 0, [], []
    for g in groups:
        span, weight = g.spans[dim], g.weight(used)
        if span is None:
            both_sides += weight
            continue
        null_left = span.nulls and lo is None
        placed = not span.only_null and _placed_overlap(span, (lo, hi))
        if null_left:
            always_left += weight
        elif placed:
            starts.append((span.start, weight))
        if placed:
            ends.append((span.end, weight))
    starts.sort(key=lambda p: p[0])
    ends.sort(key=lambda p: p[0])
    start_at, start_sum = [p[0] for p in starts], [0, *accumulate(p[1] for p in starts)]
    end_at, end_sum = [p[0] for p in ends], [0, *accumulate(p[1] for p in ends)]
    points = {p for p in start_at + end_at
              if (lo is None or p > lo) and (hi is None or p < hi)}
    for point in points:
        left = both_sides + always_left + start_sum[bisect_left(start_at, point)]
        right = both_sides + end_sum[-1] - end_sum[bisect_right(end_at, point)]
        yield max(left, right), left + right, point


def _pieces_for(size: int, target: float | None) -> int:
    return 0 if target is None else max(1, math.ceil(size / target))


def best_cut(groups, box, dims, target: float | None = None) -> tuple | None:
    """``(score, dim, point)`` of the best cut that prunes (its larger child
    reads less than ``box`` would with that column filtered too), or ``None``
    when there is none. Best: the fewest pieces its two children need at
    ``target`` (when given), then the smallest larger child (the ``score``),
    then the least read twice."""
    eff = effective(box, dims)
    inside = [g for g in groups if in_box(g, box, dims)]
    used = set(restricted(box))
    best = None
    for dim in range(len(dims)):
        whole = sum(g.weight(tuple(used | {dim})) for g in inside)
        for score, both, point in _dim_scores(inside, box, eff, dim):
            if score >= whole:
                continue
            need = _pieces_for(score, target) + _pieces_for(both - score, target)
            key = (need, score, both, dim, point)
            if best is None or key < best:
                best = key
    return None if best is None else (best[1], best[3], best[4])


def plan_boxes(dims, groups, target: float, max_pieces: int, start=None) -> list:
    """The leaves of ``start`` (default: every row): every box at most
    ``target`` estimated bytes where a cut can get it there; a box whose
    irreducible part is already at the target runs whole. Raises
    :class:`TooManyPieces` past ``max_pieces``."""
    leaves = [tuple(start) if start is not None else full_box(dims)]
    sizes = {leaves[0]: estimate(groups, leaves[0], dims)}
    frozen: set = set()
    while True:
        over = [b for b in leaves if b not in frozen and sizes[b] > target]
        if not over:
            return leaves
        box = max(over, key=lambda b: sizes[b])
        found = (None if irreducible(groups, box, dims) >= target
                 else best_cut(groups, box, dims, target))
        if found is None:
            frozen.add(box)
            continue
        left, right = cut(box, found[1], found[2])
        at = leaves.index(box)
        leaves[at:at + 1] = [left, right]
        sizes[left], sizes[right] = estimate(groups, left, dims), estimate(groups, right, dims)
        if len(leaves) > max_pieces:
            raise TooManyPieces(f"the plan needs more than {max_pieces} pieces")


def _step(dim: Dim) -> timedelta:
    return timedelta(days=1) if dim.is_date else timedelta(hours=1)


def _extent(groups, box, dim: int, dims) -> tuple:
    """The finite range a midpoint cut on ``dim`` can use: the clipped box's
    own ends, else the data's extent."""
    lo, hi = effective(box, dims)[dim]
    placed = [g.spans[dim] for g in groups
              if g.spans[dim] is not None and not g.spans[dim].only_null and in_box(g, box, dims)]
    low = lo or min((s.start for s in placed), default=None)
    high = hi or max((s.end for s in placed), default=None)
    return low, high


def _midpoint(low: datetime, high: datetime, dim: Dim) -> datetime | None:
    step = _step(dim)
    if high - low <= step:
        return None
    mid = low + (high - low) / 2
    mid = datetime(mid.year, mid.month, mid.day) + (
        timedelta(0) if dim.is_date else timedelta(hours=mid.hour))
    return mid if low < mid < high else low + step


def _midpoint_cut(groups, box, dims) -> tuple | None:
    best = None
    order = sorted(range(len(dims)), key=lambda i: (dims[i].kind != "event", i))
    eff = effective(box, dims)
    for dim in order:
        low, high = _extent(groups, box, dim, dims)
        lo, hi = eff[dim]
        if low is None or high is None or low >= high:
            continue
        point = _midpoint(low, high, dims[dim])
        if point is None or (lo is not None and point <= lo) or (hi is not None and point >= hi):
            continue
        width = (high - low) / _step(dims[dim])
        if best is None or width > best[0]:
            best = (width, dim, point)
    return None if best is None else (best[1], best[2])


def split_box(groups, box, dims) -> tuple | None:
    """Two boxes tiling ``box`` for a piece the engine refused: the estimate's
    best cut when it makes the box smaller, else a time midpoint (the
    statistics are coarser than the data, so a finer cut can still prune)."""
    found = best_cut(groups, box, dims)
    if found is not None:
        return cut(box, found[1], found[2])
    mid = _midpoint_cut(groups, box, dims)
    return None if mid is None else cut(box, mid[0], mid[1])


# --------------------------------------------------------------------------- #
# Rendering a box
# --------------------------------------------------------------------------- #
def literal(dim: Dim, moment: datetime) -> str:
    if dim.is_date:
        return f"DATE '{moment:%Y-%m-%d}'"
    zone = " UTC" if dim.zoned else ""
    return f"TIMESTAMP '{moment:%Y-%m-%d %H:%M:%S}{zone}'"


def _conjunct(dim: Dim, rng: Rng, quoted: str) -> str | None:
    lo, hi = rng
    if lo is None and hi is None:
        return None
    if lo is None:
        return f"({quoted} < {literal(dim, hi)} OR {quoted} IS NULL)"
    if hi is None:
        return f"{quoted} >= {literal(dim, lo)}"
    return f"({quoted} >= {literal(dim, lo)} AND {quoted} < {literal(dim, hi)})"


def box_predicate(dims, box, quote) -> str | None:
    """The conjunct one piece adds (``quote`` quotes a column name), or
    ``None`` for the box that covers everything."""
    parts = [c for d, r in zip(dims, box) if (c := _conjunct(d, r, quote(d.column)))]
    return " AND ".join(parts) or None


def _show(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d" if moment.time() == datetime.min.time() else "%Y-%m-%d %H:%M")


def box_label(dims, box) -> str:
    """A piece's ranges as the result's lineage shows them; a column name that
    is not a plain identifier (``engine_names``) is a placeholder."""
    from .engine_names import Withheld
    names, parts = Withheld(), []
    for dim, (lo, hi) in zip(dims, box):
        if lo is None and hi is None:
            continue
        column = names.show(dim.column)
        if lo is None:
            parts.append(f"{column} before {_show(hi)} or null")
        elif hi is None:
            parts.append(f"{column} from {_show(lo)}")
        else:
            parts.append(f"{column} {_show(lo)} to {_show(hi)}")
    return "; ".join(parts) or "every row"


def box_order(box) -> tuple:
    """A sort key: leaves in column order, lowest ranges first."""
    return tuple((lo is not None, lo or datetime.min) for lo, _hi in box)
