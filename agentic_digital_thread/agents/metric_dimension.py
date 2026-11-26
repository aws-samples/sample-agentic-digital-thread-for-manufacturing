# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Metric-by-dimension resolver (Wave 1, ADR 0015/0016/0017): a NEW additive
GROUPED path that resolves a direct ``Metric`` (concept + aggregation) **GROUP BY
a dimension concept**, returning ordered ``{dimension_value, value, lineage}``
rows with **per-row lineage** (ADR 0017) instead of a scalar.

This is extracted from ``metric_resolver`` (file-size discipline; re-exported
there so the public import path ``agents.metric_resolver.resolve_metric_by_dimension``
is unchanged). The scalar ``resolve_metric`` path is left byte-identical — a
golden regression test guards it.

Two SQL shapes, one selector (mirrors ``entity_render.build_section_sql`` for
identifier validation + double-quoting + caps, and ``_execute``'s connection-type
routing):
  * Co-located (measure + dimension on the same table) →
    ``SELECT "<dim>", AGG("<measure>") FROM "<schema>"."<t>" GROUP BY "<dim>"
    ORDER BY "<dim>" LIMIT n``.
  * Joined (single-hop, same system) → aliased ``m``/``d`` JOIN over the graph's
    ``JoinPath`` FK edge.

Determinism-pure (ADR 0001): no LLM, no ``eval``/``exec``. The impure seams
(``neptune_query``/``query_pg``/``query_system``/clock/metric layer) are reused
from ``metric_resolver`` **by attribute** so the existing test monkeypatches on
that module continue to drive this path; graph reads ALSO accept an injected
``query=`` arg (like ``entity_render``) for offline unit tests.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace

from . import joinpath_rule_link as _jp_rule
from . import metric_binding as mb
from . import metric_measure_rule as mmr
from . import metric_predicate as mp
from . import metric_resolver as mr
from .metric_layer import Aggregation, DerivedMetric, MetricKind
from .metric_resolver import (
    AuthorityResolver,
    AuthoritySource,
    MappingProvenance,
    MetricResolverError,
)

logger = logging.getLogger(__name__)


# --- Grouped types (NEW + additive; separate from Lineage/QueryPlan) ---------

MAX_GROUPS = 200    # row cap for a grouped result (mirrors entity_render.MAX_ROWS)

# The time/ordering field-concept used to pick the LATEST value per group
# (DISTINCT ON … ORDER BY <dim>, <order_column> DESC). A measure system must map
# one of its timestamp columns (check_time/event_time/reading_time/…) to this
# concept for ``Aggregation.LATEST`` to be resolvable; otherwise LATEST fails loud
# (correctly uncovered — no silent guess of an ordering column).
TIME_CONCEPT = "event-time"

# --- Metric time windows (Task 2D — ADR 0014 ``window`` seam activation) -----
# CLOSED vocabulary of supported metric windows. A ``DirectMetric.window`` token
# MUST be a key here (fail loud otherwise — a registry/config error, never user
# input). Each value carries the two SQL-dialect interval spellings:
#   * Postgres interval literal for ``now() - INTERVAL '<literal>'``;
#   * Athena ``(quantity, unit)`` for ``current_timestamp - INTERVAL '<q>' <u>``.
# The predicate column is the measure table's mapped ``TIME_CONCEPT`` column
# (the SAME graph read LATEST uses); identifiers go through
# ``validate_identifier`` — NO string-interpolated user input reaches the SQL.
WINDOW_VOCAB: dict[str, tuple[str, tuple[str, str]]] = {
    "1h": ("1 hour", ("1", "HOUR")),
    "24h": ("24 hours", ("24", "HOUR")),
    "7d": ("7 days", ("7", "DAY")),
    "30d": ("30 days", ("30", "DAY")),
}


@dataclass(frozen=True)
class JoinPathProvenance:
    """The single-hop ``JoinPath`` edge a grouped resolve traversed, oriented
    measure→dimension (``from_*`` is the measure-side table/column)."""
    join_id: str | None
    from_table: str
    from_field: str
    to_table: str
    to_field: str
    transform: str | None = None
    method: str | None = None


@dataclass(frozen=True)
class GroupedQueryPlan:
    """Serializable grouped plan (the graph-resolution↔execution split). ``join``
    is None for the co-located shape, set for the single-hop joined shape.

    ``order_column`` is the measure-side time/ordering column used ONLY by the
    ``Aggregation.LATEST`` shape (DISTINCT ON … ORDER BY <dim>, <order_column>
    DESC); it stays None for every non-LATEST aggregation (the AVG/SUM/COUNT SQL
    is byte-identical to before this field existed).

    ``window``/``window_column`` (Task 2D, additive — default None so every
    pre-existing plan/lineage stays byte-compatible): a WINDOW_VOCAB token and
    the measure table's mapped event-time column, set only when a windowed
    metric's time predicate ACTUALLY applies. ``window`` is None on the honest
    lifetime fallback (no event-time mapping / non-SQL system), and rides into
    per-row lineage via ``_row_lineage_to_dict`` — the frontend caption keys off
    it ('24h' window vs 'lifetime total')."""
    system_id: str
    schema: str
    measure_table: str
    measure_column: str
    aggregation: Aggregation
    dimension_table: str
    dimension_column: str
    join: JoinPathProvenance | None = None
    order_column: str | None = None
    window: str | None = None
    window_column: str | None = None
    # a duration-PERCENTILE grouped plan carries the bound measure and the
    # declared percentiles; each group yields one row per percentile plus the
    # sample count. Both default None so a plain grouped plan is byte-identical.
    measure: "object | None" = None
    percentiles: "tuple[float, ...] | None" = None
    # an absolute [start, end) UTC turn window applied to the measure's
    # END column; None keeps the declared-token / lifetime behaviour.
    absolute_window: "object | None" = None
    # per-role ({start, end}) type classification (zoned /
    # unzoned / date) for the measure columns, casts a date operand and picks
    # the window literal's type. dialect approximation (Athena
    # approx_percentile) stamped here so the grouped rows/render can say so.
    measure_casts: "dict | None" = None
    approximate: "bool | None" = None
    # The dimension table's own namespace when it differs from ``schema`` (an
    # S3 Tables join across namespaces); None means the same schema as the
    # measure, so PostgreSQL / Athena plans and their lineage are unchanged.
    dimension_schema: "str | None" = None


@dataclass(frozen=True)
class RowLineage:
    """Per-row grouped lineage (ADR 0017) — a SEPARATE type from ``Lineage``.
    Rows share authority/mappings/join/plan/sql/executed_at and differ only by
    ``dimension_value``."""
    metric_id: str | None
    concept: str | None
    dimension_concept: str | None
    authority: AuthoritySource | None
    measure_mapping: MappingProvenance | None
    dimension_mapping: MappingProvenance | None
    join_path: JoinPathProvenance | None
    query_plan: GroupedQueryPlan | None
    sql: str | None
    executed_at: str | None
    dimension_value: str | int | float | None = None
    kind: str = "grouped"
    # How the Athena read stayed under the scan limit (``metric_athena_scope``);
    # ``None`` when it ran as defined in one query (stripped when serialized).
    scan: dict | None = None


@dataclass(frozen=True)
class GroupedRow:
    dimension_value: str | int | float | None
    value: float | int | None
    lineage: RowLineage
    # a duration-PERCENTILE group carries its (p, value) pairs and sample
    # count instead of a single ``value`` (which stays None then).
    percentiles: "tuple[tuple[float, float | int | None], ...] | None" = None
    sample_count: int | None = None


@dataclass(frozen=True)
class GroupedResolvedMetric:
    metric_id: str
    unit: str
    label: str
    dimension_concept: str
    system_id: str
    query_plan: GroupedQueryPlan
    rows: tuple[GroupedRow, ...]


# --- Graph reads (the dimension mappings + the single-hop JoinPath) ----------

_DIM_MAPS_TO_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)"
    "-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept}) "
    "RETURN se.name AS table, f.name AS column, "
    "r.role AS role, r.confidence AS confidence, r.method AS source, "
    "r.version AS version"
)

#: Intra-system JoinPaths of one system, minus the switched-off ones (their own
#: status, or the IdentityRule they came from: :mod:`joinpath_rule_link`).
_JOINPATH_CYPHER = (
    "MATCH (jp:JoinPath) WHERE jp.from_system = $sid AND jp.to_system = $sid "
    "AND " + _jp_rule.rule_active("jp") + " "
    + _jp_rule.drop_if_rule_inactive() +
    "RETURN jp.id AS join_id, jp.from_table AS from_table, "
    "jp.from_field AS from_field, jp.to_table AS to_table, "
    "jp.to_field AS to_field, jp.transform AS transform, jp.method AS method, "
    "jp.basis AS basis, jp.canonical AS canonical"
)

#: JoinPath bases that prove nothing about the direction of a join from
#: metadata: the ``*_id`` column name guess, and that guess measured against
#: the data at onboarding (``measured_reference``, evidence for the
#: identifier-forms pairing only, never a declared key). A
#: ``foreign_key_match`` edge carrying either never wins the mirror collapse
#: (a legacy row has no basis and keeps the method-only behaviour).
_UNMEASURED_BASES = frozenset({"column_name_pattern", "measured_reference"})


def _norm_table(name: str | None) -> str:
    """Normalize a (possibly schema-qualified) JoinPath table name → bare table."""
    return (name or "").split(".")[-1]


def _resolve_dimension_mappings(system_id: str, dimension_concept: str, *, query=None) -> list[MappingProvenance]:
    """ALL columns ``dimension_concept`` maps to in ``system_id`` (NO ``LIMIT 1`` —
    dimensions are intentionally multi-mapped, e.g. ``production-line``)."""
    q = query or mr.neptune_query
    rows = q(_DIM_MAPS_TO_CYPHER, {"sid": system_id, "concept": dimension_concept})
    return [mr._mapping_from_row(r) for r in rows]


def _resolve_order_column(system_id: str, measure_table: str, *, query=None) -> str | None:
    """Resolve the measure-side time/ordering column for ``Aggregation.LATEST``:
    the column on ``measure_table`` that ``TIME_CONCEPT`` maps to in ``system_id``.
    Returns None when no such (co-located) time mapping exists — the caller then
    fails loud so LATEST stays correctly uncovered (no silent guess)."""
    q = query or mr.neptune_query
    rows = q(_DIM_MAPS_TO_CYPHER, {"sid": system_id, "concept": TIME_CONCEPT})
    co_located = [
        mr._mapping_from_row(r) for r in rows
        if (r.get("table") or "").split(".")[-1] == measure_table
    ]
    if not co_located:
        return None
    return co_located[0].column


def _orient_join(edge: dict, measure_table: str, dimension_table: str) -> JoinPathProvenance:
    """Validate + orient a JoinPath edge measure→dimension for a SAFE many-to-one
    aggregate. Onboarding stores every JoinPath ``from``=FK/many side, ``to``=PK/one
    side (``onboard._detect_internal_joins``). A safe aggregate REQUIRES the measure
    table on the FK/many (``from``) side so each measure row joins to exactly one
    dimension row; if the measure is on the PK/one (``to``) side the join fans out
    and AVG/SUM silently double-count → fail loud (no silent reversal)."""
    eft = _norm_table(edge.get("from_table"))
    ett = _norm_table(edge.get("to_table"))
    if eft == measure_table and ett == dimension_table:
        return JoinPathProvenance(
            join_id=edge.get("join_id"),
            from_table=measure_table, from_field=edge.get("from_field"),
            to_table=dimension_table, to_field=edge.get("to_field"),
            transform=edge.get("transform"), method=edge.get("method"),
        )
    raise MetricResolverError(
        f"Cannot group by dimension {dimension_table!r}: join orientation would "
        f"fan out the measure (measure table {measure_table!r} is on the PK/one "
        f"side of joinpath {edge.get('join_id')!r}, not the FK/many side); "
        f"unsupported/deferred"
    )


def _collapse_exact_mirror_joinpaths(
    direct: list[dict], system_id: str, measure_table: str, dimension_table: str
) -> dict | None:
    """Return one exact mirror only when metadata proves its direction.

    Requested measure orientation is deliberately not evidence. Exact endpoint
    symmetry merely proves duplication; it does not prove which table is the
    FK/many side. A unique explicit canonical marker wins, otherwise exactly one
    FK-derived edge is accepted as the multiplicity-safe direction. Contradictory
    mirrors with the same provenance remain ambiguous and fail loud.
    """
    fields = {
        field
        for edge in direct
        for field in (edge.get("from_field"), edge.get("to_field"))
    }
    transforms = {edge.get("transform") for edge in direct}
    orientations = {
        (_norm_table(edge.get("from_table")), _norm_table(edge.get("to_table")))
        for edge in direct
    }
    expected_orientations = {
        (measure_table, dimension_table),
        (dimension_table, measure_table),
    }
    if len(fields) != 1 or len(transforms) != 1 or orientations != expected_orientations:
        return None

    explicit = [edge for edge in direct if edge.get("canonical") is True]
    if len(explicit) == 1:
        proven = explicit[0]
    else:
        fk_derived = [
            edge for edge in direct
            if str(edge.get("method") or "").casefold() == "foreign_key_match"
            and edge.get("basis") not in _UNMEASURED_BASES
        ]
        if len(explicit) != 0 or len(fk_derived) != 1:
            return None
        proven = fk_derived[0]

    logger.warning(
        "Collapsing %d metadata-proven mirrored JoinPaths between %r and %r "
        "in %r to %r: %s",
        len(direct), measure_table, dimension_table, system_id,
        proven.get("join_id"), [edge.get("join_id") for edge in direct],
    )
    return proven


def _select_join_path(system_id: str, measure_table: str, dimension_table: str, *, query=None) -> JoinPathProvenance | None:
    """Single-hop JoinPath connecting the two tables (either direction), scoped to
    one system. None if co-located; fail loud on no edge / >1 distinct / multi-hop."""
    if measure_table == dimension_table:
        return None
    q = query or mr.neptune_query
    edges = q(_JOINPATH_CYPHER, {"sid": system_id})
    pair = {measure_table, dimension_table}
    direct = [e for e in edges
              if {_norm_table(e.get("from_table")), _norm_table(e.get("to_table"))} == pair]
    if len(direct) == 1:
        return _orient_join(direct[0], measure_table, dimension_table)
    if len(direct) > 1:
        mirror = _collapse_exact_mirror_joinpaths(
            direct, system_id, measure_table, dimension_table
        )
        if mirror is not None:
            return _orient_join(mirror, measure_table, dimension_table)
        raise MetricResolverError(
            f"ambiguous join: {len(direct)} JoinPaths connect {measure_table!r} "
            f"and {dimension_table!r} in {system_id!r}"
        )
    tables = {_norm_table(e.get("from_table")) for e in edges} | \
             {_norm_table(e.get("to_table")) for e in edges}
    if measure_table in tables and dimension_table in tables:
        raise MetricResolverError(
            f"multi-hop join from {measure_table!r} to {dimension_table!r} "
            f"not supported (deferred)"
        )
    raise MetricResolverError(
        f"no JoinPath connecting {measure_table!r} and {dimension_table!r} "
        f"in system {system_id!r}"
    )


def _prefer_colocated_mapping(co_located: list[MappingProvenance]) -> MappingProvenance:
    """Deterministically pick ONE mapping among co-located dimension candidates
    (two+ columns on the measure table mapping the same dimension concept). The
    tie-break is a TOTAL order — never ambiguous:

      1. a MAPS_TO ``role`` of ``'identifier'`` beats any other role;
      2. then the highest mapping ``confidence`` (``None`` ranks below any number);
      3. then the lexicographically smallest column name (final, always distinct).

    Role/confidence come from the MAPS_TO edge already fetched by
    ``_resolve_dimension_mappings`` (``r.role`` / ``r.confidence``)."""
    return min(
        co_located,
        key=lambda c: (
            0 if c.role == "identifier" else 1,
            -(c.confidence if c.confidence is not None else float("-inf")),
            c.column or "",
        ),
    )


def _select_dimension_mapping(measure_mapping: MappingProvenance, dim_candidates: list[MappingProvenance], system_id: str, *, query=None):
    """Pick the dimension mapping + (optional) join: prefer co-located with the
    measure table (multiple co-located candidates are resolved DETERMINISTICALLY
    via ``_prefer_colocated_mapping`` — identifier role, then confidence, then
    column name), else the single single-hop-joinable candidate. Fail loud on
    joinable-table ties / no joinable candidate."""
    co_located = [c for c in dim_candidates if c.table == measure_mapping.table]
    if co_located:
        return _prefer_colocated_mapping(co_located), None
    if len(dim_candidates) == 1:
        c = dim_candidates[0]
        return c, _select_join_path(system_id, measure_mapping.table, c.table, query=query)
    joinable = []
    for c in dim_candidates:
        try:
            j = _select_join_path(system_id, measure_mapping.table, c.table, query=query)
        except MetricResolverError:
            continue
        if j is not None:
            joinable.append((c, j))
    if not joinable:
        raise MetricResolverError(
            f"no single-hop join from measure table {measure_mapping.table!r} to "
            f"any dimension candidate {[c.table for c in dim_candidates]}"
        )
    if len(joinable) > 1:
        raise MetricResolverError(
            f"ambiguous: {len(joinable)} dimension candidates joinable to "
            f"{measure_mapping.table!r}"
        )
    return joinable[0]


def _dimension_qualifier(plan: GroupedQueryPlan) -> str:
    """The quoted qualifier of the DIMENSION table in a joined shape: its own
    namespace when the plan carries one (an S3 Tables join across namespaces),
    else the measure's, so single-schema SQL is unchanged."""
    return mr.table_qualifier(
        plan.system_id, mr.validate_identifier(plan.dimension_schema or plan.schema),
        quoted=True)


def _compose_grouped_sql(plan: GroupedQueryPlan) -> str:
    """Compose a read-only grouped aggregate SELECT (identifiers validated +
    double-quoted, mirroring entity_render.build_section_sql).

    Two aggregate shapes plus the ``Aggregation.LATEST`` 'latest value per group'
    shape (Postgres ``DISTINCT ON``). The non-LATEST (AVG/SUM/COUNT/MIN/MAX) SQL
    is byte-identical to the pre-LATEST resolver (golden-regression guarded). The
    LATEST shape FAILS LOUD when the plan has no ``order_column`` (no silent guess
    of a time/ordering column — ADR 0011 F5)."""
    # ``schema`` is the quoted table qualifier: ``"schema"`` on PostgreSQL /
    # Athena, ``"s3t"."namespace"`` on an S3 Tables system.
    schema = mr.table_qualifier(
        plan.system_id, mr.validate_identifier(plan.schema), quoted=True)
    m_table = mr.validate_identifier(plan.measure_table)
    d_table = mr.validate_identifier(plan.dimension_table)
    d_col = mb.quote_column_identifier(plan.dimension_column)
    if plan.measure is not None:
        # the measure pins its own two columns; ``measure_column`` is unused
        # (and empty when the table came from the measure pins).
        return _compose_measure_grouped_sql(plan, schema, m_table, d_table, d_col)
    m_col = mb.quote_column_identifier(plan.measure_column)
    if plan.aggregation is Aggregation.LATEST:
        return _compose_latest_grouped_sql(plan, schema, m_table, m_col, d_table, d_col)
    agg = plan.aggregation.value.upper()
    if plan.join is None:
        return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
            f'SELECT {d_col}, {agg}({m_col}) '  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
            f'FROM {schema}."{m_table}" '
            f'GROUP BY {d_col} ORDER BY {d_col} LIMIT {MAX_GROUPS}'
        )
    from_field = mb.quote_column_identifier(plan.join.from_field)
    to_field = mb.quote_column_identifier(plan.join.to_field)
    return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
        f'SELECT d.{d_col}, {agg}(m.{m_col}) '  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
        f'FROM {schema}."{m_table}" m '
        f'JOIN {_dimension_qualifier(plan)}."{d_table}" d ON m.{from_field} = d.{to_field} '
        f'GROUP BY d.{d_col} ORDER BY d.{d_col} LIMIT {MAX_GROUPS}'
    )


def _compose_latest_grouped_sql(plan, schema, m_table, m_col, d_table, d_col) -> str:
    """Compose the 'latest value per group' SELECT via Postgres ``DISTINCT ON``:
    one row per dimension value, taking the measure of the most-recent (highest
    ``order_column``) record. The DISTINCT ON key MUST be the leftmost ORDER BY
    expression (Postgres requirement), so we order by the dimension first then the
    time column DESC. The time/order column lives on the MEASURE side (``m`` in the
    joined shape). FAIL LOUD if it was not resolved — never guess one."""
    if not plan.order_column:
        raise MetricResolverError(
            f"'latest' aggregation requires a time/ordering column for measure "
            f"{plan.measure_table!r}.{plan.measure_column!r} but none was resolved "
            f"(no time field-concept mapped) — uncovered, no silent guess"
        )
    order_col = mb.quote_column_identifier(plan.order_column)
    if plan.join is None:
        return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
            f'SELECT DISTINCT ON ({d_col}) {d_col}, {m_col} '  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
            f'FROM {schema}."{m_table}" '
            f'ORDER BY {d_col}, {order_col} DESC LIMIT {MAX_GROUPS}'
        )
    from_field = mb.quote_column_identifier(plan.join.from_field)
    to_field = mb.quote_column_identifier(plan.join.to_field)
    return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
        f'SELECT DISTINCT ON (d.{d_col}) d.{d_col}, m.{m_col} '  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
        f'FROM {schema}."{m_table}" m '
        f'JOIN {_dimension_qualifier(plan)}."{d_table}" d ON m.{from_field} = d.{to_field} '
        f'ORDER BY d.{d_col}, m.{order_col} DESC LIMIT {MAX_GROUPS}'
    )


def _compose_measure_grouped_sql(plan, schema, m_table, d_table, d_col) -> str:
    """Compose the grouped duration-PERCENTILE SELECT: one row
    per dimension value carrying each percentile of the clamped, unit-scaled
    duration plus the sample count (COUNT of the same expr), NULL-timestamp
    rows excluded. Measure columns carry the ``m.`` alias in the joined shape;
    identifiers validated, percentiles from a closed 1..5 list."""
    dialect = mr._measure_dialect(plan.system_id)
    qualifier = "m." if plan.join is not None else ""
    expr = mmr.render_duration_expr(plan.measure, dialect=dialect,
                                    qualifier=qualifier, casts=plan.measure_casts)
    pct = mmr.render_percentile_select(expr, plan.percentiles, dialect=dialect)
    where = mmr.null_exclusion_where(plan.measure, qualifier=qualifier)
    if plan.join is None:
        return (  # nosec B608 - identifiers validated; percentiles from a closed vocabulary
            f'SELECT {d_col}, {pct}, COUNT({expr}) '  # nosec B608
            f'FROM {schema}."{m_table}" WHERE {where} '
            f'GROUP BY {d_col} ORDER BY {d_col} LIMIT {MAX_GROUPS}'
        )
    from_field = mb.quote_column_identifier(plan.join.from_field)
    to_field = mb.quote_column_identifier(plan.join.to_field)
    return (  # nosec B608 - identifiers validated; percentiles from a closed vocabulary
        f'SELECT d.{d_col}, {pct}, COUNT({expr}) '  # nosec B608
        f'FROM {schema}."{m_table}" m '
        f'JOIN {_dimension_qualifier(plan)}."{d_table}" d ON m.{from_field} = d.{to_field} '
        f'WHERE {where} GROUP BY d.{d_col} ORDER BY d.{d_col} LIMIT {MAX_GROUPS}'
    )


def _prune_athena_grouped_sql(plan: GroupedQueryPlan, sql: str, *, query=None) -> str:
    """Insert a recent-partitions WHERE into a composed Athena grouped SELECT.

    The predicate targets the MEASURE table (the big scanned fact table) via the
    shared :func:`metric_resolver._athena_recent_partition_where` helper — in the
    joined shape the measure carries the ``m`` alias, so column references are
    qualified ``m.``. The WHERE lands immediately before ``GROUP BY`` (valid for
    both grouped shapes). When NO partition predicate is available (unpartitioned
    table / discovery failure — LIVE-CONFIRMED to still trip the bytes-scanned
    limit), a SECOND-TIER bound applies: a time-window WHERE on the measure
    table's mapped event-time column (:func:`_athena_time_window_where`, the same
    graph read LATEST's ``_resolve_order_column`` uses), restricted to the last
    :data:`ATHENA_TIME_WINDOW_DAYS` days. Best-effort throughout: no predicate of
    either tier, or no ``GROUP BY`` marker => the SQL is returned unchanged (the
    honest limit error).
    """
    marker = " GROUP BY "
    where = _athena_grouped_prune_where(plan, query=query) if marker in sql else None
    if not where:
        return sql
    # AND-extend an existing WHERE (the measure null guard already
    # landed one) instead of emitting a second WHERE (invalid SQL).
    return _insert_measure_where(sql, where)


def _athena_grouped_prune_where(plan: GroupedQueryPlan, *, query=None) -> str | None:
    """The recent-partitions (else recent event-time) bound on the measure
    table that :func:`_prune_athena_grouped_sql` inserts, or ``None``."""
    qualifier = "m." if plan.join is not None else ""
    return (mr._athena_recent_partition_where(plan.system_id, plan.measure_table,
                                              qualifier=qualifier)
            or _athena_time_window_where(plan.system_id, plan.measure_table,
                                         qualifier=qualifier, query=query))


# Second-tier Athena scan bound: when a table has no partition predicate, bound
# the composed aggregate to the last N days via the mapped event-time column.
ATHENA_TIME_WINDOW_DAYS = 30


def _athena_window_predicate(
    column: str, interval: tuple[str, str], *, qualifier: str = ""
) -> str:
    """Compose the Athena event-time window predicate for one measure column.

    Shared by the grouped :func:`_athena_time_window_where` seam and the SCALAR
    windowed path (``metric_resolver._apply_scalar_window``). The identifier is
    validated; ``interval`` is a ``(quantity, unit)`` pair from the CLOSED
    :data:`WINDOW_VOCAB` (or the static 30-day default) — no user input is ever
    interpolated."""
    ident = mb.quote_column_identifier(column)
    qty, unit = interval
    return (  # nosec B608 - identifier validated; interval from a closed vocabulary
        f'{qualifier}{ident} >= (current_timestamp - '
        f"INTERVAL '{qty}' {unit})"
    )


def _athena_time_window_where(
    system_id: str, measure_table: str, *, qualifier: str = "", query=None,
    interval: tuple[str, str] | None = None,
) -> str | None:
    """Best-effort time-window predicate for one Athena measure table, or None.

    Resolves the system's event-time column for ``measure_table`` via the SAME
    graph read the LATEST aggregation uses (:func:`_resolve_order_column` — the
    co-located :data:`TIME_CONCEPT` mapping) and restricts to the last
    :data:`ATHENA_TIME_WINDOW_DAYS` days by default. ``interval`` (Task 2D)
    overrides the bound with a ``(quantity, unit)`` pair taken from the CLOSED
    :data:`WINDOW_VOCAB` — never from user input — so windowed metrics reuse
    this seam with their own window. ``None`` when no time column is mapped
    or on any resolution failure (the caller runs the query unchanged — the
    honest limit error). ``qualifier`` prefixes the column reference (``"m."``
    for the grouped joined shape's measure alias). Never raises.
    """
    try:
        column = _resolve_order_column(system_id, measure_table, query=query)
        if not column:
            return None
        return _athena_window_predicate(
            column, interval or (str(ATHENA_TIME_WINDOW_DAYS), "DAY"),
            qualifier=qualifier,
        )
    except Exception:  # noqa: BLE001 - the fallback bound must never break the query path
        return None


# --- Metric time-window predicates (Task 2D) ---------------------------------

def _validate_window(window: str, metric_id: str) -> None:
    """THE single closed-vocabulary window validation (fix-pass hoist).

    A ``DirectMetric.window`` token MUST be a :data:`WINDOW_VOCAB` key — an
    unknown token is a metric-REGISTRY error (never user input) and fails loud
    before any SQL is composed. Shared by :func:`resolve_metric_by_dimension`,
    :func:`_resolve_metric_window`, and the scalar path
    (``metric_resolver.resolve_metric``) — the ONE home of this check."""
    if window not in WINDOW_VOCAB:
        raise MetricResolverError(
            f"metric {metric_id!r} declares unsupported window {window!r}; "
            f"supported window vocabulary: {sorted(WINDOW_VOCAB)}"
        )


def _pg_window_predicate(window: str, column: str, *, qualifier: str = "") -> str:
    """Compose the Postgres event-time window predicate for one measure column.

    Shared by the grouped :func:`_pg_time_window_where` and the SCALAR windowed
    path (``metric_resolver._apply_scalar_window``). Composed EXCLUSIVELY from a
    validated identifier plus a closed-vocabulary interval literal
    (``WINDOW_VOCAB``) — no user input is ever interpolated."""
    interval_literal = WINDOW_VOCAB[window][0]
    ident = mb.quote_column_identifier(column)
    return (  # nosec B608 - identifier validated; interval from the closed WINDOW_VOCAB
        f'{qualifier}{ident} >= (now() - INTERVAL \'{interval_literal}\')'
    )


def _pg_time_window_where(plan: GroupedQueryPlan) -> str | None:
    """Postgres event-time window predicate for a windowed grouped plan, or None.

    Delegates to the shared :func:`_pg_window_predicate` composer. The predicate
    targets the measure side (``m.`` alias in the joined shape)."""
    if not plan.window or not plan.window_column:
        return None
    return _pg_window_predicate(
        plan.window, plan.window_column,
        qualifier="m." if plan.join is not None else "",
    )


def _insert_measure_where(sql: str, predicate: str) -> str:
    """Insert (or AND-extend) a measure-side WHERE immediately before ``GROUP BY``.

    Handles both grouped aggregate shapes; ANDs onto an existing WHERE (e.g. the
    Athena partition prune already landed one). No ``GROUP BY`` marker (the
    LATEST/DISTINCT ON shape) ⇒ SQL returned unchanged — windows never apply to
    LATEST (its 'current value' semantics are already time-anchored)."""
    marker = " GROUP BY "
    if marker not in sql:
        return sql
    head, tail = sql.split(marker, 1)
    if " WHERE " in head:
        return f"{head} AND {predicate}{marker}{tail}"
    return f"{head} WHERE {predicate}{marker}{tail}"


def _resolve_metric_window(
    window: str, metric_id: str, system_id: str, measure_table: str, *, query=None
) -> tuple[str | None, str | None]:
    """Resolve a windowed metric's ``(window, window_column)`` for one system.

    The token MUST come from :data:`WINDOW_VOCAB` (closed vocabulary — the ONE
    :func:`_validate_window` check, fail loud). The predicate column is the
    measure table's mapped :data:`TIME_CONCEPT` column (the same graph read
    LATEST uses). NO event-time mapping ⇒ ``(None, None)`` — the HONEST
    fallback: the resolve proceeds with the lifetime shape and the payload
    carries ``window: null`` (never a fabricated window)."""
    _validate_window(window, metric_id)
    column = _resolve_order_column(system_id, measure_table, query=query)
    if not column:
        return None, None
    return window, column


def _execute_grouped(plan: GroupedQueryPlan, sql: str) -> list[tuple]:
    """Execute the grouped SQL via the system's connection_type (mirrors _execute);
    return ordered ``(dimension_value, scalar)`` tuples (value coerced)."""
    entry = mr.catalog.get(plan.system_id)
    conn_type = (
        entry.get("connection_type", mr.connection_types.POSTGRESQL)
        if entry else mr.connection_types.POSTGRESQL
    )
    # The composed SQL can still fail at the database (e.g. AVG over a non-numeric
    # measure column when a concept is mapped to the wrong field). Surface that as a
    # MetricResolverError so the route degrades to a friendly bar-error envelope and
    # logs the detail server-side — never a raw 500. (Errors propagate, not swallowed.)
    try:
        if conn_type == mr.connection_types.POSTGRESQL:
            parsed = json.loads(mr.query_pg(plan.schema, sql))
            mr._note_execution(plan.system_id)
        elif conn_type == mr.connection_types.ATHENA:
            parsed = _athena_grouped_result(plan, sql)
        elif conn_type == mr.connection_types.S3TABLES_DUCKDB:
            parsed = mr._execute_s3tables(plan, sql)
        else:
            parsed = mr.query_system(plan.system_id, sql)
            mr._note_execution(plan.system_id)
    except MetricResolverError:
        raise
    except Exception as e:
        # Generic client-facing message — the raw DB error (which can include schema/
        # column names + SQL fragments) is preserved server-side only, via the chained
        # exception in the route's logger.warning(exc_info=True). Do not echo it back.
        raise MetricResolverError(
            "grouped query failed while reading the source database"
        ) from e
    rows = parsed.get("rows") or []
    return _parse_grouped_rows(plan, rows)


def _parse_grouped_rows(plan: GroupedQueryPlan, rows) -> list[tuple]:
    """Coerce grouped result rows. A plain metric yields ``(dim, value)``; a
    duration-PERCENTILE metric yields ``(dim, (p, value)..., count)``, one row
    per group."""
    measure = plan.measure is not None
    n = len(plan.percentiles) if measure else 0
    out: list[tuple] = []
    for r in rows:
        if not r:
            continue
        if measure:
            if len(r) < n + 2:
                raise MetricResolverError(
                    f"grouped percentile row has <{n + 2} columns (expected "
                    f"dimension_value, {n} percentiles, count): {r!r}")
            pairs = tuple((p, mr._coerce_scalar(r[1 + i]))
                          for i, p in enumerate(plan.percentiles))
            out.append((r[0], pairs, mr._coerce_count(r[1 + n])))
            continue
        if len(r) < 2:
            raise MetricResolverError(
                f"grouped result row has <2 columns (expected "
                f"dimension_value, value): {r!r}")
        out.append((r[0], mr._coerce_scalar(r[1])))
    return out


def _athena_grouped_result(plan: GroupedQueryPlan, sql: str) -> dict:
    """Run the grouped SELECT on Athena.

    DRY (mirrors metric_resolver._execute's Athena branch): the composed
    grouped SELECT is valid Athena SQL — run it through the SAME connector,
    with the cross-account-safe FULL connection config (role_arn/external_id
    merged from the agent-config secret, not just the catalog snapshot).
    The shared denied-column seam turns a Lake Formation column-level
    denial of the MEASURE or DIMENSION column (nothing to drop in a
    grouped aggregate) into a clear error instead of COLUMN_NOT_FOUND. How
    the read was computed is noted for the caller (``metric_athena_scope``).
    """
    from . import metric_athena_scope as mas
    # Essential columns the LF-denial seam must not drop: measure + dimension,
    # or, when the table came from the measure pins (empty measure_column), the
    # two pinned duration columns + dimension.
    essential = mr._athena_essential_columns(plan, plan.dimension_column)
    try:
        with mas.read_scope(plan.system_id, mr.plan_lineage(plan)):
            outcome = mr.athena_tools.run_athena_select_with_column_retry(
                lambda _cols: sql,
                list(essential),
                mr._full_connection_config(plan.system_id),
                essential=essential,
            )
    except mr.athena_tools.AthenaColumnDeniedError as e:
        raise MetricResolverError(str(e)) from e
    parsed = outcome["result"]
    mas.record(parsed)
    trace = mr.active_trace()
    if trace is not None:
        trace.record(plan.system_id, parsed.get("scanned_bytes"))
    return parsed


def _build_row_lineage(metric, dimension_concept, auth, measure_mapping, dimension_mapping, join, plan, sql, executed_at, dimension_value) -> RowLineage:
    """Stamp one row's grouped lineage (ADR 0017) from data already touched."""
    return RowLineage(
        metric_id=metric.id, concept=metric.concept,
        dimension_concept=dimension_concept, authority=auth,
        measure_mapping=measure_mapping, dimension_mapping=dimension_mapping,
        join_path=join, query_plan=plan, sql=sql, executed_at=executed_at,
        dimension_value=dimension_value,
    )


def _execute_sitewise_grouped(plan: GroupedQueryPlan) -> list[tuple]:
    """Execute a grouped metric for a SiteWise (Direct) system (non-SQL).

    SiteWise has no SQL/join engine, so grouping is supported ONLY for the
    CO-LOCATED shape: the measure property and the dimension property live on the
    SAME asset model (``plan.measure_table == plan.dimension_table`` — enforced by
    the caller). For every asset of that model we read the CURRENT value of both
    the dimension property (the group key) and the measure property, bucket the
    ``(measure_value, timestamp)`` pairs by the dimension value, then reduce each
    bucket with the SAME per-group aggregator the scalar SiteWise path uses
    (``metric_resolver._aggregate_sitewise``) — so a COUNT yields the number of
    assets in each state (the machine-state distribution) and SUM/AVG/MIN/MAX/
    LATEST reduce the measure per group.

    Reuses the shared ``sitewise_read`` seam + the scalar path's helpers
    (``_full_connection_config`` / ``_match_by_name`` / ``_aggregate_sitewise``) —
    no new SiteWise SDK code. Returns ``(group_value, value)`` tuples ordered by
    group for deterministic output. Fail loud (``MetricResolverError``) if the
    model/either property is unresolved or no asset yields a usable value (never a
    silent/fake fallback, ADR-0011 F5)."""
    connection_config = mr._full_connection_config(plan.system_id)
    region = connection_config.get("region") or os.environ.get("AWS_REGION")

    models = mr.sitewise_read.list_asset_models_with_properties(connection_config, region)
    # Duplicate-named models exist in real estates (hierarchy shells shadowing the
    # propertied model). Disambiguate requiring BOTH the dimension AND the measure
    # property on the selected model (the co-located grouped contract).
    model = mr._select_sitewise_model(
        models, plan.dimension_table,
        [plan.dimension_column, plan.measure_column],
        system_id=plan.system_id,
    )
    props = model.get("properties") or []
    dim_prop = mr._match_by_name(props, plan.dimension_column)
    measure_prop = mr._match_by_name(props, plan.measure_column)
    if not dim_prop or not dim_prop.get("id"):
        raise MetricResolverError(
            f"no SiteWise property named {plan.dimension_column!r} on asset model "
            f"{plan.dimension_table!r} in system {plan.system_id!r}"
        )
    if not measure_prop or not measure_prop.get("id"):
        raise MetricResolverError(
            f"no SiteWise property named {plan.measure_column!r} on asset model "
            f"{plan.measure_table!r} in system {plan.system_id!r}"
        )

    assets = mr.sitewise_read.list_assets(
        connection_config, region, asset_model_id=model["id"]
    )
    same_prop = measure_prop["id"] == dim_prop["id"]
    # BATCH read (event-loop fix): ONE BatchGetAssetPropertyValue sweep (pages of
    # 16, the API cap) instead of 1–2 sequential GetAssetPropertyValue HTTPS
    # round-trips PER asset — the old per-asset loop made a ~39-machine
    # machine-state resolve take tens of seconds. Per-entry error tolerance is
    # preserved: a failed entry surfaces as {"error": ...} for that key only, so
    # a bad asset is skipped exactly like before.
    entries: list[dict] = []
    for asset in assets:
        asset_id = asset.get("id")
        if not asset_id:
            continue
        entries.append({"assetId": asset_id, "propertyId": dim_prop["id"]})
        if not same_prop:
            entries.append({"assetId": asset_id, "propertyId": measure_prop["id"]})
    values = mr.sitewise_read.batch_get_property_values(
        connection_config, region, entries
    )
    groups: dict = {}
    for asset in assets:
        asset_id = asset.get("id")
        if not asset_id:
            continue
        dpv = values.get(f"{asset_id}_{dim_prop['id']}")
        if not isinstance(dpv, dict) or dpv.get("error"):
            continue
        group_key = dpv.get("value")
        if group_key is None:
            continue
        # Same property serves as both group key AND measure (e.g. a categorical
        # COUNT distribution) — reuse the single read instead of a second entry.
        mpv = dpv if same_prop else values.get(f"{asset_id}_{measure_prop['id']}")
        if not isinstance(mpv, dict) or mpv.get("error"):
            continue
        m_value = mpv.get("value")
        if m_value is None:
            continue
        groups.setdefault(group_key, []).append((m_value, mpv.get("timestamp")))

    if not groups:
        raise MetricResolverError(
            f"no usable SiteWise values grouping {plan.measure_table!r}."
            f"{plan.measure_column!r} by {plan.dimension_column!r} across "
            f"{len(assets)} asset(s) in system {plan.system_id!r}"
        )
    return [
        (group_key, mr._aggregate_sitewise(plan.aggregation, usable))
        for group_key, usable in sorted(groups.items(), key=lambda kv: str(kv[0]))
    ]


def _resolve_by_dimension_sitewise(metric, dimension_concept, auth, measure_mapping) -> GroupedResolvedMetric:
    """Resolve a DIRECT metric GROUP BY a dimension concept for a SiteWise (Direct)
    system — the non-SQL analogue of the SQL grouped path.

    The dimension mapping is the system's SOLE authoritative property for the
    dimension concept (via ``metric_resolver._resolve_table_column`` — the same
    single-edge read the scalar/SQL paths use). SiteWise cannot join across asset
    models, so this REQUIRES the measure and dimension properties to be co-located
    on the same asset model (fail loud otherwise). Execution + per-group
    aggregation happen in ``_execute_sitewise_grouped``; the plan carries no
    ``join``/``order_column`` (both are SQL-only)."""
    dimension_mapping = mr._resolve_table_column(auth.system_id, dimension_concept)
    if measure_mapping.table != dimension_mapping.table:
        raise mr.StructuralMetricError(
            f"SiteWise metric-by-dimension requires the measure and dimension on the "
            f"same asset model (SiteWise has no cross-model join); measure "
            f"{measure_mapping.table!r} vs dimension {dimension_mapping.table!r} in "
            f"system {auth.system_id!r}"
        )
    plan = GroupedQueryPlan(
        system_id=auth.system_id, schema=mr.get_schema(auth.system_id),
        measure_table=measure_mapping.table, measure_column=measure_mapping.column,
        aggregation=metric.aggregation,
        dimension_table=dimension_mapping.table, dimension_column=dimension_mapping.column,
        join=None, order_column=None,
    )
    sql = (f"sitewise:group_{metric.aggregation.value}("
           f"{plan.measure_table}.{plan.measure_column} by {plan.dimension_column})")
    executed_at = mr._utcnow_iso()
    rows = tuple(
        GroupedRow(
            dimension_value=dim_value, value=value,
            lineage=_build_row_lineage(
                metric, dimension_concept, auth, measure_mapping, dimension_mapping,
                None, plan, sql, executed_at, dim_value,
            ),
        )
        for dim_value, value in _execute_sitewise_grouped(plan)
    )
    return GroupedResolvedMetric(
        metric_id=metric.id, unit=metric.unit, label=metric.label,
        dimension_concept=dimension_concept, system_id=auth.system_id,
        query_plan=plan, rows=rows,
    )


def _grouped_order_column(metric, metric_id: str, system_id: str, measure_table: str, *, query=None) -> str | None:
    """LATEST needs an event-time order column on the measure table — fail loud
    when unmapped (its 'current value' semantics are undefined without one)."""
    if metric.aggregation is not Aggregation.LATEST:
        return None
    order_column = _resolve_order_column(system_id, measure_table, query=query)
    if not order_column:
        raise MetricResolverError(
            f"'latest' metric {metric_id!r} needs a {TIME_CONCEPT!r} field mapped "
            f"on measure table {measure_table!r} in system "
            f"{system_id!r}; none found (uncovered)"
        )
    return order_column


def _grouped_predicates(metric, auth: AuthoritySource,
                        measure_mapping: MappingProvenance):
    """ADR-0075: bind a filtered metric's predicates on the MEASURE table, or
    ``None`` when it declares no filter. A non-SQL backend is STRUCTURAL and an
    unbound concept raises ``UnresolvedPredicateError`` (a ``CoverageGapError``
    → ``uncovered + missing_concepts``). LATEST + filter fails CLOSED: its
    DISTINCT ON shape has no GROUP BY, so the predicate could not be inserted."""
    predicates = getattr(metric, "filter", None)
    if not predicates:
        return None
    if metric.aggregation is Aggregation.LATEST:
        raise MetricResolverError(
            f"metric {metric.id!r}: a filter is not valid with the 'latest' aggregation"
        )
    return mr._predicate_bindings_for(
        auth.system_id, measure_mapping.table, predicates
    )


def _apply_athena_grouped_window(plan: GroupedQueryPlan, sql: str, *, query=None) -> str:
    """The Athena grouped event-time window WHERE: bound on the
    measure's END column for a measure plan, else the first co-located
    event-time mapping. Best-effort, SQL unchanged when no column resolves."""
    qualifier = "m." if plan.join is not None else ""
    if plan.measure is not None and plan.window_column:
        w = _athena_window_predicate(
            plan.window_column, WINDOW_VOCAB[plan.window][1], qualifier=qualifier)
    else:
        w = _athena_time_window_where(
            plan.system_id, plan.measure_table, qualifier=qualifier,
            query=query, interval=WINDOW_VOCAB[plan.window][1])
    return _insert_measure_where(sql, w) if w else sql


def _apply_grouped_absolute_window(plan: GroupedQueryPlan, sql: str) -> str:
    """The turn's absolute ``[start, end)`` window on the measure END column, qualified ``m.`` in the joined shape. The dialect literal composer
    lives in ``metric_measure_rule`` (identifiers validated, instants gated)."""
    qualifier = "m." if plan.join is not None else ""
    col_type = (plan.measure_casts or {}).get("end", "zoned")
    w = mr.mmr.absolute_window_where(
        plan.measure.end.column, plan.absolute_window.start_utc,
        plan.absolute_window.end_utc, dialect=mr._measure_dialect(plan.system_id),
        qualifier=qualifier, col_type=col_type)
    return _insert_measure_where(sql, w)


def _grouped_sql(plan: GroupedQueryPlan, *, query=None, predicates=None,
                 narrow: bool | None = None) -> str:
    """Compose the grouped SQL, then apply per-backend finalization.

    Athena scan-cost guard (LIVE-CONFIRMED: an unpruned full-table GROUP BY over
    a large partitioned table is CANCELLED by the workgroup's bytes-scanned
    limit): restrict the measure table to its most recent partitions. Applied
    ONLY to the Athena branch, BEFORE lineage stamping so the recorded per-row
    SQL is exactly what executes; best-effort (None => SQL unchanged). The
    Postgres SQL is untouched (golden-regression guarded). Windowed metrics AND
    the closed-vocab event-time WHERE onto their dialect (Task 2D)."""
    sql = _compose_grouped_sql(plan)
    if mr._connection_type(plan.system_id) == mr.connection_types.ATHENA:
        if narrow is None:
            from . import metric_athena_scope as mas
            # a duration-percentile is not decomposable → narrow up front,
            # unless an explicit turn window bounds it (never narrowed past it).
            narrow = plan.absolute_window is None and (
                plan.measure is not None
                or not mas.exact_eligible(plan.aggregation, joined=plan.join is not None))
        if narrow:
            sql = _prune_athena_grouped_sql(plan, sql, query=query)
        if plan.window:
            sql = _apply_athena_grouped_window(plan, sql, query=query)
    elif plan.window:
        w = _pg_time_window_where(plan)
        if w:
            sql = _insert_measure_where(sql, w)
    if plan.absolute_window is not None and plan.measure is not None:
        sql = _apply_grouped_absolute_window(plan, sql)
    if predicates:  # ADR-0075: after prune + window, measure side (m. when joined)
        try:
            fragment = mp.render_predicate_sql(
                predicates, qualifier="m." if plan.join is not None else "")
        except ValueError as e:
            raise MetricResolverError(f"invalid metric predicate: {e}") from e
        sql = _insert_measure_where(sql, fragment)
    return sql


def _grouped_schema(system_id: str, table: str | None = None) -> str:
    """Schema resolution for the grouped SQL path — the ONE rule lives in
    ``metric_resolver.plan_schema`` (shared with the scalar path)."""
    return mr.plan_schema(system_id, table)


def _grouped_window(metric, metric_id: str, system_id: str, measure_table: str, *, query=None) -> tuple[str | None, str | None]:
    """Metric time window (Task 2D — ADR 0014 seam). Validate the token FIRST
    (the ONE _validate_window check — closed vocabulary, fail loud regardless
    of connection type), then resolve the event-time column only for the SQL
    backends the predicate dialects cover. No event-time mapping or a
    non-SQL/unrecognized backend ⇒ the HONEST lifetime fallback
    (window=None on the plan → ``window: null`` in per-row lineage). LATEST is
    excluded: its 'current value' semantics are already time-anchored."""
    metric_window = getattr(metric, "window", None)
    if metric_window is None or metric.aggregation is Aggregation.LATEST:
        return None, None
    _validate_window(metric_window, metric_id)
    if mr._connection_type(system_id) in mr.connection_types.METRIC_SQL_CONNECTION_TYPES:
        return _resolve_metric_window(
            metric_window, metric_id, system_id, measure_table, query=query,
        )
    return None, None


def _grouped_target(metric_id: str, authority: AuthorityResolver | None):
    """Phase 1 of the grouped resolve: metric definition + authority + measure
    mapping. Fail loud on unknown/derived metrics (derived-by-dimension deferred)."""
    try:
        metric = mr.get_metric_layer().get_metric(metric_id)
    except KeyError as e:
        raise MetricResolverError(f"unknown metric id: {metric_id!r}") from e
    if isinstance(metric, DerivedMetric) or metric.kind is MetricKind.DERIVED:
        raise MetricResolverError(
            f"metric {metric_id!r} is derived; metric-by-dimension is direct-only "
            f"(derived-by-dimension deferred)"
        )
    auth, measure_mapping = mr._resolve_measure_target(metric, authority)
    return metric, auth, measure_mapping


def _grouped_rows_from_values(values, plan, metric, dimension_concept, auth,
                             measure_mapping, dimension_mapping, join, sql, scan):
    """Build the per-group rows: a duration-PERCENTILE plan yields one row
    per group carrying its (p, value) pairs and sample count; a plain plan
    yields the scalar ``value`` (byte-identical to before)."""
    executed_at = mr._utcnow_iso()

    def _lineage(dim_value):
        return replace(_build_row_lineage(
            metric, dimension_concept, auth, measure_mapping, dimension_mapping,
            join, plan, sql, executed_at, dim_value), scan=scan)

    if plan.measure is not None:
        return tuple(
            GroupedRow(dimension_value=dim_value, value=None, percentiles=pairs,
                       sample_count=count, lineage=_lineage(dim_value))
            for dim_value, pairs, count in values)
    return tuple(
        GroupedRow(dimension_value=dim_value, value=value,
                   lineage=_lineage(dim_value))
        for dim_value, value in values)


def resolve_metric_by_dimension(metric_id: str, dimension_concept: str, *, authority: AuthorityResolver | None = None, query=None, dimension_column: str | None = None, turn_window=None) -> GroupedResolvedMetric:
    """Resolve a DIRECT metric GROUP BY a dimension concept → ordered grouped rows
    with per-row lineage (ADR 0016/0017). Derived metrics are deferred (fail loud).

    ``dimension_column`` pins the EXACT column the dimension concept
    maps to on the measure table (verified fail-closed); ``None`` keeps the
    deterministic candidate selection. ``turn_window`` is an absolute
    ``[start, end)`` UTC window applied to the measure's END column."""
    metric, auth, measure_mapping = _grouped_target(metric_id, authority)
    predicates = _grouped_predicates(metric, auth, measure_mapping)  # ADR-0075; SiteWise ⇒ structural
    conn = mr._connection_type(auth.system_id)
    if metric.measure is not None and (
            conn not in mr.connection_types.METRIC_SQL_CONNECTION_TYPES):
        # a duration-percentile is a SQL-backend metric only, reject
        # every other backend BEFORE the SiteWise grouped branch.
        raise mr.StructuralMetricError(
            "a duration-percentile metric is supported on Athena / PostgreSQL "
            f"/ S3 Tables only; system {auth.system_id!r} is {conn!r}")
    mr.require_metric_reader(auth.system_id)  # no SQL for a system with no reader
    # SiteWise (Direct): no SQL/join engine — non-SQL grouped resolver (co-located only).
    if conn == mr.connection_types.SITEWISE_DIRECT:
        return _resolve_by_dimension_sitewise(metric, dimension_concept, auth, measure_mapping)
    dimension_mapping, join = _resolve_dimension_binding(
        auth.system_id, dimension_concept, measure_mapping, dimension_column,
        query=query)
    plan = _build_grouped_plan(metric, metric_id, auth, measure_mapping,
                               dimension_mapping, join, turn_window, query=query)
    values, sql, scan = _execute_grouped_scoped(plan, query=query, predicates=predicates)
    rows = _grouped_rows_from_values(
        values, plan, metric, dimension_concept, auth, measure_mapping,
        dimension_mapping, join, sql, scan)
    return GroupedResolvedMetric(
        metric_id=metric_id, unit=metric.unit, label=metric.label,
        dimension_concept=dimension_concept, system_id=auth.system_id,
        query_plan=plan, rows=rows,
    )


def _dimension_schema(system_id: str, measure_table: str, dimension_table: str,
                      join) -> str | None:
    """The dimension table's own namespace for a JOINED S3 Tables plan; None
    everywhere else (co-located, PostgreSQL, Athena), so those plans and their
    lineage are unchanged."""
    if join is None or dimension_table == measure_table or (
            mr._connection_type(system_id) != mr.connection_types.S3TABLES_DUCKDB):
        return None
    return mr.plan_schema(system_id, dimension_table)


def _build_grouped_plan(metric, metric_id, auth, measure_mapping,
                        dimension_mapping, join, turn_window, *, query=None):
    """The ``GroupedQueryPlan`` for a grouped resolve. A measure plan
    carries the bound measure, its percentiles, the END-column window and
    the absolute turn window; a plain plan is byte-identical to before."""
    order_column = _grouped_order_column(metric, metric_id, auth.system_id,
                                         measure_mapping.table, query=query)
    window_token, window_column = _grouped_window(
        metric, metric_id, auth.system_id, measure_mapping.table, query=query)
    bound_measure, measure_casts = (
        mr._bind_measure(auth.system_id, measure_mapping.table, metric.measure)
        if metric.measure is not None else (None, None))
    if bound_measure is not None and window_token is not None:
        window_column = bound_measure.end.column  # the END column
    approximate = (mr.mmr.dialect_is_approximate(mr._measure_dialect(auth.system_id))
                   if bound_measure is not None else None)
    return GroupedQueryPlan(
        system_id=auth.system_id,
        schema=_grouped_schema(auth.system_id, measure_mapping.table),
        dimension_schema=_dimension_schema(auth.system_id, measure_mapping.table,
                                           dimension_mapping.table, join),
        measure_table=measure_mapping.table, measure_column=measure_mapping.column,
        aggregation=metric.aggregation,
        dimension_table=dimension_mapping.table, dimension_column=dimension_mapping.column,
        join=join, order_column=order_column,
        window=window_token, window_column=window_column,
        measure=bound_measure, percentiles=metric.percentiles,
        absolute_window=(turn_window if bound_measure is not None else None),
        measure_casts=measure_casts, approximate=approximate,
    )


def _resolve_dimension_binding(system_id: str, dimension_concept: str,
                               measure_mapping, dimension_column: str | None, *,
                               query=None):
    """The dimension ``(mapping, join)`` for the grouped resolve. With an exact
    ``dimension_column`` pin the column is verified against live
    MAPS_TO on the measure table (fail-closed, co-located, no join); without a
    pin, the deterministic candidate selection runs unchanged."""
    if dimension_column is not None:
        column = mr._bind_field_ref(
            system_id, measure_mapping.table, f"group_by[{dimension_concept}]",
            mr.mmr.FieldRef(dimension_concept, dimension_column))
        return MappingProvenance(
            table=measure_mapping.table, column=column, role=None,
            confidence=None, source="pinned", binding_basis="single"), None
    dim_candidates = _resolve_dimension_mappings(system_id, dimension_concept, query=query)
    if not dim_candidates:
        raise mr.CoverageGapError(
            f"no MAPS_TO field for dimension concept {dimension_concept!r} in "
            f"system {system_id!r}",
            concept=dimension_concept,
        )
    return _select_dimension_mapping(
        measure_mapping, dim_candidates, system_id, query=query)


def _execute_grouped_scoped(plan: GroupedQueryPlan, *, query=None, predicates=None):
    """``(rows, executed sql, scan)``: the grouped metric as defined (exact past
    the scan limit on Athena), narrowed and said so only where it must be. A
    read bounded by an explicit turn window is never narrowed
    (``metric_athena_scope.refuse_windowed``)."""
    from . import metric_athena_scope as mas
    athena = mr._connection_type(plan.system_id) == mr.connection_types.ATHENA
    exact = athena and mas.exact_eligible(plan.aggregation, joined=plan.join is not None)
    windowed = plan.absolute_window is not None
    sql = _grouped_sql(plan, query=query, predicates=predicates)
    scan = None
    if athena and not exact and not windowed:
        where = _athena_grouped_prune_where(plan, query=query)
        scan = mas.narrowed(where, mas.REASON_SHAPE) if where else None
    try:
        values, recorded = mas.collecting(_execute_grouped, plan, sql)
    except MetricResolverError as exc:
        if athena and windowed and mas.is_scan_refusal(exc):
            mas.refuse_windowed(exc, plan)
        where = _athena_grouped_prune_where(plan, query=query) if exact else None
        if not (where and mas.is_scan_refusal(exc)):
            raise
        reason = mas.refusal_reason(exc, plan.system_id, mr.plan_lineage(plan))
        sql = _grouped_sql(plan, query=query, predicates=predicates, narrow=True)
        values, recorded = mas.collecting(_execute_grouped, plan, sql)
        scan = mas.narrowed(where, reason)
    return values, sql, scan or recorded


def _row_lineage_to_dict(lin: RowLineage | None) -> dict | None:
    """None-safe asdict for a grouped per-row lineage, with the embedded
    ``GroupedQueryPlan.aggregation`` enum rendered ``.value`` (ADR 0017). Nested
    AuthoritySource/MappingProvenance/JoinPathProvenance are plain-string dataclasses."""
    if lin is None:
        return None
    d = asdict(lin)
    if lin.query_plan is not None:
        d["query_plan"]["aggregation"] = lin.query_plan.aggregation.value
        plan = lin.query_plan
        d["query_plan"].pop("absolute_window", None)  # internal-only
        d["query_plan"].pop("measure_casts", None)    # internal-only
        if getattr(plan, "dimension_schema", None) is None:  # strip when absent
            d["query_plan"].pop("dimension_schema", None)
        if getattr(plan, "approximate", None) is None:  # strip when absent
            d["query_plan"].pop("approximate", None)
        if getattr(plan, "measure", None) is None:  # byte-identical when absent
            d["query_plan"].pop("measure", None)
            d["query_plan"].pop("percentiles", None)
        elif plan.percentiles is not None:
            d["query_plan"]["percentiles"] = list(plan.percentiles)
    if lin.scan is None:
        d.pop("scan", None)
    return d
