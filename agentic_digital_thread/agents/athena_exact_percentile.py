# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Combine an approx_percentile split's per-piece quantile digests.

The exact-split engine runs a percentile query as pieces, each over a disjoint
part of the table (``athena_exact_split``). An ordinary COUNT/SUM/MIN/MAX merges
its pieces with arithmetic (``athena_exact_merge``). A percentile cannot: the
engine builds a ``qdigest`` per group that only ``merge`` combines. So a
percentile piece returns, per group, a serialized ``qdigest_agg`` of the value
(hex of its varbinary -- verified to survive as a result cell and to be
mergeable by a later query, ``s40/engine-check*.log``), and this module:

1. collects the per-group digests across every piece (each group is a tuple of
   its GROUP BY key texts; a scalar query has the one empty-key group), keyed by
   a small integer group id so the key values are never rendered into SQL;
2. runs ONE later Athena query over a ``VALUES`` list of ``(group id, digest)``
   that, per group id, reconstitutes each piece's digest
   (``CAST(from_hex(d) AS qdigest(double))``), merges them, and reads the
   requested quantiles (``value_at_quantile`` for a single probability,
   ``values_at_quantiles`` for the ``ARRAY[...]`` form);
3. assembles the result rows (the group key texts plus the quantile value) and
   applies the query's ORDER BY and LIMIT once over the groups.

The result is APPROXIMATE, the same kind as ``approx_percentile``; the caller
labels it so and never calls it exact. A piece that failed, timed out, was cut,
or held more groups than the cap gives NO result, exactly as for a COUNT split.
Generic: no table, column or state names in code. The only impure part is the
one merge query it runs through the connector.
"""
from __future__ import annotations

from .athena_exact_shape import Shape

#: One group id per piece row holds at most this many digests before the merge
#: VALUES list is considered too large to send (a group's pieces equal the
#: piece count, well under this; the bound only fails truly pathological runs).
MAX_DIGEST_ROWS = 200_000


class PercentileMergeError(Exception):
    """The digests could not be merged into quantiles (``reason`` a code)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _key_indices(shape: Shape) -> list[int]:
    return [i for i, o in enumerate(shape.outputs) if o.kind == "key"]


def _percentile_index(shape: Shape) -> int:
    return next(i for i, o in enumerate(shape.outputs) if o.kind == "percentile")


def _agg_indices(shape: Shape) -> list[int]:
    """The ordinary COUNT/SUM/MIN/MAX outputs beside the percentile."""
    return [i for i, o in enumerate(shape.outputs) if o.kind == "agg"]


def collect(shape: Shape, piece_rows):
    """``(ordered group keys, {group id: [digest,...]}, {group id: {agg index:
    accumulator}}, {group id: display key texts})`` over every piece's rows.
    Each piece row is ``[key..., agg..., digest]`` in output order. Groups are
    keyed by the TYPED engine-equivalent key (``athena_exact_merge._group_key``
    off the key columns' types, so DECIMAL ``1.5`` and ``1.50`` are one group,
    signed zero and NaN as the engine groups them), with the first display text
    kept; the merge's unsupported-type refusals apply. A group appears once
    even if its digest is NULL (approx_percentile returns NULL then)."""
    from .athena_exact_merge import MergeError, _Accumulator, _family, _group_key
    keys = _key_indices(shape)
    pidx = _percentile_index(shape)
    aggs = _agg_indices(shape)
    types = None
    order: list = []                            # group-id order of first appearance
    digests: dict = {}
    accs: dict = {}
    display: dict = {}
    seen: dict = {}                             # typed key -> group id
    total = 0
    for piece in piece_rows:
        if types is None:
            types = [str(t or "") for t in piece.types]
            families = [_family(types[i]) for i in keys]
        for row in piece.rows:
            if not isinstance(row, (list, tuple)) or len(row) != len(shape.outputs):
                raise PercentileMergeError("unmergeable_shape", "a piece returned a malformed row")
            try:
                typed = tuple(_group_key(row[i], f) for i, f in zip(keys, families))
            except MergeError as exc:
                raise PercentileMergeError(exc.reason, exc.detail) from exc
            gid = seen.get(typed)
            if gid is None:
                gid = seen[typed] = len(order)
                order.append(gid)
                digests[gid] = []
                display[gid] = tuple(row[i] for i in keys)
                accs[gid] = {i: _Accumulator(shape.outputs[i].func, types[i]) for i in aggs}
            digest = row[pidx]
            if digest is not None and str(digest) != "":
                digests[gid].append(str(digest))
                total += 1
                if total > MAX_DIGEST_ROWS:
                    raise PercentileMergeError("too_many_digests",
                                               "the pieces returned too many group digests to merge")
            for i in aggs:
                accs[gid][i].add(row[i])
    return order, digests, accs, display


def _quantile_call(shape: Shape, use, column: str) -> str:
    """``values_at_quantiles`` / ``value_at_quantile`` over the merged digest,
    reconstituted at the value expression's OWN proved type (``use.value_type``,
    not forced to double), reading the SAME quantile spec the query asked for
    (sliced verbatim)."""
    quantiles = shape.sql[use.q_start:use.q_end]
    value_type = (use.value_type or "double").strip().lower()
    merged = f"merge(CAST(from_hex({column}) AS qdigest({value_type})))"
    func = "values_at_quantiles" if use.array else "value_at_quantile"
    return f"{func}({merged}, {quantiles})"


def _values_list(order: list, digests: dict) -> str:
    """A ``VALUES (gid, 'digest')`` list over the group ids that have a digest.
    A group with no digest is left out (it yields NULL, attached afterwards)."""
    rows = []
    for gid in order:
        for digest in digests.get(gid, []):
            literal = digest.replace("'", "''")
            rows.append(f"({gid}, '{literal}')")
    return ", ".join(rows)


def merge_sql(shape: Shape, values: str) -> str:
    """The later merge query: per group id, the merged-digest quantiles over a
    ``VALUES`` list of that group's piece digests."""
    use = shape.percentiles[0]
    call = _quantile_call(shape, use, "d")
    return (f"SELECT gid, {call} AS p FROM (VALUES {values}) AS t(gid, d) GROUP BY gid")  # nosec B608 - call slices the quantile spec of the query the user already ran; digests are quote-escaped, group ids are ints; the caller runs it through ensure_select_only


def quantiles_by_group(run_merge, shape: Shape, order: list, digests: dict) -> dict:
    """Run the later merge query (``run_merge(sql) -> rows of [gid, quantiles]``)
    and map each group id to its quantile text. A group with NO digest input
    gets NULL without any query. Every group id that HAD digests must come back
    as exactly ONE well-formed ``[gid, quantiles]`` row with a NON-NULL
    quantile: a missing, duplicate, unknown, wrong-width or NULL-quantile row
    refuses (``merge_incomplete``) -- only a digest-less group may be NULL."""
    expected = [gid for gid in order if digests.get(gid)]
    out: dict = {gid: None for gid in order}
    if not expected:                            # no group had any value: no query
        return out
    rows = run_merge(merge_sql(shape, _values_list(order, digests)))
    got: dict = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or row[0] is None:
            raise PercentileMergeError("merge_incomplete",
                                       "the merge query returned a malformed row")
        try:
            gid = int(str(row[0]))
        except ValueError as exc:
            raise PercentileMergeError("merge_incomplete",
                                       "the merge query returned an unreadable group id") from exc
        if gid not in out:
            raise PercentileMergeError("merge_incomplete",
                                       "the merge query returned an unknown group id")
        if gid in got:
            raise PercentileMergeError("merge_incomplete",
                                       "the merge query returned a duplicate group id")
        if row[1] is None:
            raise PercentileMergeError("merge_incomplete",
                                       "a group with digest input got a NULL quantile")
        got[gid] = row[1]
    if set(got) != set(expected):
        raise PercentileMergeError("merge_incomplete",
                                   "the merge query did not return every group's quantiles")
    out.update(got)
    return out


def _ordered(shape: Shape, rows: list[list], types: list[str]) -> list[list]:
    """Apply the query's ORDER BY and LIMIT once over the result rows. ORDER BY
    is the ORDINARY merge's implementation (``athena_exact_merge._ordered``),
    so every one of its fail-closed refusals -- NaN, signed-zero ties,
    unsupported types -- applies here too; a percentile output can never be an
    ORDER BY column (refused at parse time). LIMIT is applied after."""
    from .athena_exact_merge import MergeError, _ordered as merge_ordered
    try:
        out = merge_ordered(shape, list(rows), types)
    except MergeError as exc:
        raise PercentileMergeError(exc.reason, exc.detail) from exc
    if shape.limit is not None:
        out = out[:shape.limit]
    return out


def build_rows(shape: Shape, order: list, by_gid: dict, accs: dict, display: dict,
               types: list[str]) -> tuple[list[list], int]:
    """The result rows (``[key..., agg..., quantile]`` in output order) and the
    group count before the query's LIMIT. The quantile cell is the engine's
    text, NULL for a group no piece gave a value; the aggregate cells are the
    exact merged values; the key cells are each group's first display text.
    ``types`` are the result column types (for ordering)."""
    keys = _key_indices(shape)
    pidx = _percentile_index(shape)
    rows: list[list] = []
    for gid in order:
        row: list = [None] * len(shape.outputs)
        for pos, i in enumerate(keys):
            row[i] = display[gid][pos]
        for i, acc in accs.get(gid, {}).items():
            row[i] = acc.result()
        row[pidx] = by_gid.get(gid)
        rows.append(row)
    groups = len(rows)
    return _ordered(shape, rows, types), groups
