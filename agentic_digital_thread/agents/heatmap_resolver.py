# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sensor-heatmap resolver (Wave 3, S5.3 §6.3): resolve a DIRECT metric over TWO
dimension concepts into a severity grid — the compositional 2-D case the research
calls "metric-by-dimension, twice, with a threshold column".

It composes over the SAME single-dim machinery in ``metric_dimension`` (file-size
discipline; identifier validation, JoinPath orientation/fan-out fail-loud, the
``event-time`` ordering column for ``LATEST``) rather than re-inventing it:

  * Two dimension concepts → two GROUP BY keys (or ``DISTINCT ON (d1, d2)`` for the
    ``LATEST`` 'latest value per cell' shape). Both dimensions must be co-located
    on the measure table (the canonical ``sensor_readings``-shaped table); a
    cross-table second grouping dimension is deferred (fail loud).
  * Optional threshold: a per-cell comparison value pulled from a
    ``specification``/decision-boundary (or sensor ``threshold``) concept, joined
    by the EXISTING single-hop ``JoinPath`` selection (``_select_join_path`` /
    ``_orient_join``) — fail-loud on a fan-out orientation, NEVER silently
    reversed. When the threshold concept is unmapped/sparse / not single-hop
    joinable, the grid degrades cleanly to 'uncovered' (no fabricated threshold).
  * Server-side DETERMINISTIC severity per cell (``ok``/``warning``/``critical``,
    or ``uncovered`` when there is no threshold) from value vs threshold.

Envelope (row-shaped, S5.3 §6.3)::

    {kind:'heatmap', dims:[concept_d1, concept_d2],
     rows:[[d1, d2, value, threshold, severity], ...],
     lineage:[...], status:'ok', threshold_status:'covered'|'uncovered', ...}

Determinism-pure (ADR 0001): no LLM, no ``eval``/``exec``. The impure seams
(``neptune_query``/``query_pg``/``query_system``/clock/metric layer) are reused
from ``metric_resolver`` by attribute (so existing monkeypatches drive this path);
graph reads ALSO accept an injected ``query=`` arg for offline unit tests. The
core FAILS LOUD; the thin route wrapper composes the friendly HTTP-200 envelope.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import metric_binding as mb
from . import metric_dimension as md
from . import metric_resolver as mr
from .catalog import CONCEPT_NAME
from .coverage import covered_concepts
from .metric_layer import Aggregation, DerivedMetric, MetricKind
from .metric_resolver import (
    AuthorityResolver,
    AuthoritySource,
    MappingProvenance,
    MetricResolverError,
)

# Row cap shared with the single-dim grouped resolver (a heatmap is a 2-D grid,
# so the cell count is naturally bounded by MAX_GROUPS distinct (d1, d2) pairs).
MAX_CELLS = md.MAX_GROUPS

# Severity is a deterministic function of value vs threshold. A reading AT or ABOVE
# its threshold is critical; within ``WARN_FRACTION`` of it is a warning; below is
# ok. (Sensor thresholds in this demo are upper limits — temperature/pressure/
# vibration alarms.) When there is no threshold the cell is 'uncovered', never a
# fabricated ok/critical.
WARN_FRACTION = 0.9

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_UNCOVERED = "uncovered"


@dataclass(frozen=True)
class HeatmapPlan:
    """Serializable 2-D grid plan (graph-resolution↔execution split). ``join`` is
    the single-hop threshold join (None when the threshold is co-located on the
    measure table or absent). ``order_column`` is the measure-side time column for
    the ``LATEST`` shape (None otherwise)."""
    system_id: str
    schema: str
    measure_table: str
    measure_column: str
    aggregation: Aggregation
    dim_x_column: str
    dim_y_column: str
    order_column: str | None = None
    threshold_table: str | None = None
    threshold_column: str | None = None
    threshold_join: md.JoinPathProvenance | None = None
    # The threshold table's own namespace when it differs from ``schema`` (an
    # S3 Tables join across namespaces); None means the measure's schema.
    threshold_schema: str | None = None


@dataclass(frozen=True)
class HeatmapCellLineage:
    """Per-cell lineage (ADR 0017) — cells share authority/mappings/join/sql/
    executed_at and differ only by their (dim_x, dim_y) coordinate + value."""
    metric_id: str | None
    concept: str | None
    dimension_concepts: tuple[str, str]
    threshold_concept: str | None
    aggregation: str | None
    authority: AuthoritySource | None
    measure_mapping: MappingProvenance | None
    dim_x_mapping: MappingProvenance | None
    dim_y_mapping: MappingProvenance | None
    threshold_mapping: MappingProvenance | None
    threshold_join: md.JoinPathProvenance | None
    sql: str | None
    executed_at: str | None
    dim_x_value: str | int | float | None
    dim_y_value: str | int | float | None
    value: float | int | None
    threshold: float | int | None
    severity: str
    kind: str = "heatmap-cell"


# --- Severity (server-side, deterministic) ----------------------------------

def _severity(value, threshold) -> str:
    """Deterministic per-cell severity from value vs threshold (closed vocabulary).
    No threshold (or non-numeric) → 'uncovered' (never a fabricated reading)."""
    if value is None or threshold is None:
        return SEVERITY_UNCOVERED
    try:
        v = float(value)
        t = float(threshold)
    except (TypeError, ValueError):
        return SEVERITY_UNCOVERED
    if t == 0:
        return SEVERITY_CRITICAL if v != 0 else SEVERITY_OK
    if v >= t:
        return SEVERITY_CRITICAL
    if v >= WARN_FRACTION * t:
        return SEVERITY_WARNING
    return SEVERITY_OK


# --- Dimension + threshold resolution (reuse the single-dim machinery) -------

def _pick_colocated_dimension(concept: str, candidates: list[MappingProvenance], measure_table: str) -> MappingProvenance:
    """Pick the dimension mapping co-located on the measure table. A heatmap groups
    on two keys of the SAME measure row, so each dimension MUST be co-located; a
    cross-table second grouping dimension is deferred (fail loud, no silent join)."""
    co_located = [c for c in candidates if c.table == measure_table]
    if not co_located:
        raise MetricResolverError(
            f"heatmap dimension {concept!r} is not co-located on measure table "
            f"{measure_table!r} (cross-table heatmap dimensions are deferred)"
        )
    if len(co_located) > 1:
        raise MetricResolverError(
            f"ambiguous co-located mappings for heatmap dimension {concept!r} on "
            f"{measure_table!r}: {[c.column for c in co_located]}"
        )
    return co_located[0]


def _resolve_threshold(system_id: str, measure_table: str, threshold_concept: str | None, *, query=None):
    """Resolve an optional threshold mapping + (optional) single-hop join.

    Returns ``(mapping, join)`` when a threshold is resolvable (``join`` is None
    for a co-located threshold), or ``None`` to degrade to 'uncovered' when the
    threshold concept is absent / unmapped / sparse / not single-hop joinable.
    A fan-out join orientation FAILS LOUD (propagates) — the threshold must never
    be pulled through a measure-fanning join (it would mis-attribute limits)."""
    if not threshold_concept:
        return None  # no threshold requested → uncovered
    candidates = md._resolve_dimension_mappings(system_id, threshold_concept, query=query)
    if not candidates:
        return None  # threshold concept unmapped → uncovered (degrade cleanly)
    co_located = [c for c in candidates if c.table == measure_table]
    if len(co_located) == 1:
        return co_located[0], None
    if len(co_located) > 1:
        return None  # ambiguous co-located threshold → uncovered (no silent pick)
    # Not co-located: require exactly one single-hop-joinable candidate.
    joinable = []
    for c in candidates:
        try:
            j = md._select_join_path(system_id, measure_table, c.table, query=query)
        except MetricResolverError as e:
            if "fan out" in str(e):
                raise  # correctness hazard — fail loud, never silently reverse
            continue   # no path / multi-hop / ambiguous → not joinable → uncovered
        if j is not None:
            joinable.append((c, j))
    if len(joinable) == 1:
        return joinable[0]
    return None  # zero or >1 joinable threshold candidates → uncovered


# --- SQL composition ---------------------------------------------------------

def _heatmap_schema(system_id: str, table: str) -> str:
    """The plan schema: the catalog schema, except an S3 Tables system, whose
    "schema" is the namespace the connector needs (``mr.plan_schema``)."""
    if mr._connection_type(system_id) == mr.connection_types.S3TABLES_DUCKDB:
        return mr.plan_schema(system_id, table)
    return mr.get_schema(system_id)


def _threshold_schema(system_id: str, threshold_table: str | None,
                      measure_table: str) -> str | None:
    """The threshold table's own namespace for an S3 Tables joined threshold;
    None when there is no joined threshold or the system is not S3 Tables."""
    if (not threshold_table or threshold_table == measure_table
            or mr._connection_type(system_id) != mr.connection_types.S3TABLES_DUCKDB):
        return None
    return mr.plan_schema(system_id, threshold_table)


def _heatmap_identifiers(plan: HeatmapPlan) -> dict:
    """Validated identifiers: schema/tables BARE (quoted by the template),
    columns ALREADY quoted — a dotted physical column is ONE identifier
    (ADR-0076 D4); flat names render byte-identically. A system the metric
    layer has no reader for is refused first (no SQL is composed for it)."""
    mr.require_metric_reader(plan.system_id)
    q = mb.quote_column_identifier
    # ``schema`` is the quoted table qualifier: ``"schema"`` on PostgreSQL /
    # Athena, ``"s3t"."namespace"`` on an S3 Tables system.
    ids = {"schema": mr.table_qualifier(
               plan.system_id, mr.validate_identifier(plan.schema), quoted=True),
           "m_table": mr.validate_identifier(plan.measure_table),
           "m_col": q(plan.measure_column), "dx": q(plan.dim_x_column),
           "dy": q(plan.dim_y_column)}
    if plan.threshold_column is not None:
        ids["t_col"] = q(plan.threshold_column)
    if plan.threshold_join is not None:
        ids["t_table"] = mr.validate_identifier(plan.threshold_table)
        ids["t_schema"] = mr.table_qualifier(
            plan.system_id,
            mr.validate_identifier(plan.threshold_schema or plan.schema), quoted=True)
        ids["from_field"] = q(plan.threshold_join.from_field)
        ids["to_field"] = q(plan.threshold_join.to_field)
    return ids


def _compose_heatmap_sql(plan: HeatmapPlan) -> str:
    """Compose the read-only 2-key grid SELECT (identifiers validated + double-
    quoted, mirroring metric_dimension._compose_grouped_sql). LATEST → ``DISTINCT
    ON (d1, d2) … ORDER BY d1, d2, <order> DESC``; else ``GROUP BY d1, d2``. The
    threshold (when present) is selected per cell (DISTINCT ON) or aggregated with
    ``MAX`` (GROUP BY, deterministic for a per-group-constant limit)."""
    ids = _heatmap_identifiers(plan)
    schema, m_table, m_col = ids["schema"], ids["m_table"], ids["m_col"]
    dx, dy, t_col = ids["dx"], ids["dy"], ids.get("t_col")
    has_threshold = t_col is not None
    # FROM clause: bare measure table, or measure ``m`` JOIN threshold ``t``.
    if plan.threshold_join is not None:
        from_clause = (
            f'FROM {schema}."{m_table}" m '
            f'JOIN {ids["t_schema"]}."{ids["t_table"]}" t ON m.{ids["from_field"]} = t.{ids["to_field"]}'
        )
        mp, tp = 'm.', 't.'
    else:
        from_clause = f'FROM {schema}."{m_table}"'
        mp = tp = ''

    if plan.aggregation is Aggregation.LATEST:
        if not plan.order_column:
            raise MetricResolverError(
                f"'latest' heatmap requires a time/ordering column for measure "
                f"{plan.measure_table!r}.{plan.measure_column!r} but none was "
                f"resolved (no time field-concept mapped) — uncovered, no guess"
            )
        order_col = mb.quote_column_identifier(plan.order_column)
        select_cols = f'{mp}{dx}, {mp}{dy}, {mp}{m_col}'
        if has_threshold:
            select_cols += f', {tp}{t_col}'
        return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
            f'SELECT DISTINCT ON ({mp}{dx}, {mp}{dy}) {select_cols} '
            f'{from_clause} '
            f'ORDER BY {mp}{dx}, {mp}{dy}, {mp}{order_col} DESC LIMIT {MAX_CELLS}'
        )

    agg = plan.aggregation.value.upper()
    select_cols = f'{mp}{dx}, {mp}{dy}, {agg}({mp}{m_col})'
    if has_threshold:
        select_cols += f', MAX({tp}{t_col})'
    return (  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)
        f'SELECT {select_cols} '
        f'{from_clause} '
        f'GROUP BY {mp}{dx}, {mp}{dy} '
        f'ORDER BY {mp}{dx}, {mp}{dy} LIMIT {MAX_CELLS}'
    )


def _execute_heatmap(plan: HeatmapPlan, sql: str, *, has_threshold: bool) -> list[tuple]:
    """Execute the grid SQL via the system's connection_type (mirrors
    metric_dimension._execute_grouped); return ``(d1, d2, value[, threshold])``
    tuples (values coerced). A DB failure surfaces as MetricResolverError."""
    entry = mr.catalog.get(plan.system_id)
    conn_type = entry.get("connection_type", "PostgreSQL") if entry else "PostgreSQL"
    try:
        if conn_type == "PostgreSQL":
            parsed = json.loads(mr.query_pg(plan.schema, sql))
        elif conn_type == mr.connection_types.S3TABLES_DUCKDB:
            parsed = mr._execute_s3tables(plan, sql)
        else:
            parsed = mr.query_system(plan.system_id, sql)
    except MetricResolverError:
        raise
    except Exception as e:
        # Generic client-facing message; raw DB detail stays server-side via the
        # chained exception in the route's exc_info log (no schema/SQL leak).
        raise MetricResolverError(
            "heatmap query failed while reading the source database"
        ) from e
    min_cols = 4 if has_threshold else 3
    rows = parsed.get("rows") or []
    out: list[tuple] = []
    for r in rows:
        if not r:
            continue
        if len(r) < min_cols:
            raise MetricResolverError(
                f"heatmap result row has <{min_cols} columns (expected "
                f"dim_x, dim_y, value{', threshold' if has_threshold else ''}): {r!r}"
            )
        d1, d2 = r[0], r[1]
        value = mr._coerce_scalar(r[2])
        threshold = mr._coerce_scalar(r[3]) if has_threshold else None
        out.append((d1, d2, value, threshold))
    return out


# --- Public resolve ----------------------------------------------------------

def _cell_lineage_to_dict(lin: HeatmapCellLineage) -> dict:
    """JSON-safe asdict for a per-cell lineage (no enums survive — ``aggregation``
    is already a plain str; nested mappings/join/authority are str dataclasses)."""
    return asdict(lin)


def heatmap_uncovered(metric_id: str, dims: list[str], threshold_concept: str | None,
                      missing_concepts: list[str], *, label: str | None = None,
                      unit: str | None = None, suggestion: str | None = None) -> dict:
    """The honest needs-data envelope for a heatmap whose measure OR a dimension
    is not mapped yet — never error styling (mirrors ``_grouped_uncovered``)."""
    return {
        "kind": "heatmap",
        "status": "uncovered",
        "metric_id": metric_id,
        "label": label,
        "unit": unit,
        "dims": list(dims),
        "threshold_concept": threshold_concept,
        "threshold_status": "uncovered",
        "missing_concepts": list(missing_concepts),
        "suggestion": suggestion or (
            "map the missing concept on the authoritative system (onboarding "
            "review) to light up this panel"),
        "columns": [],
        "rows": [],
        "lineage": [],
    }


def _require_dimension_mappings(system_id: str, concept: str, query) -> list:
    """Dimension MAPS_TO candidates on the authority, or ``CoverageGapError``
    (a mapping gap, never a fault) when the concept has none there."""
    candidates = md._resolve_dimension_mappings(system_id, concept, query=query)
    if not candidates:
        raise mr.CoverageGapError(
            f"no MAPS_TO field for heatmap dimension {concept!r} in "
            f"system {system_id!r}", concept=concept,
        )
    return candidates


def _resolve_direct_metric(metric_id: str):
    """The metric for ``metric_id``, or ``MetricResolverError`` when the id is
    unknown or the metric is derived (heatmap is direct-only; derived deferred)."""
    try:
        metric = mr.get_metric_layer().get_metric(metric_id)
    except KeyError as e:
        raise MetricResolverError(f"unknown metric id: {metric_id!r}") from e
    if isinstance(metric, DerivedMetric) or metric.kind is MetricKind.DERIVED:
        raise MetricResolverError(
            f"metric {metric_id!r} is derived; heatmap is direct-only (deferred)"
        )
    return metric


def _heatmap_coverage_uncovered(metric, metric_id, dims, threshold_concept, query):
    """The 'uncovered' envelope when the metric's concept is not covered by any
    onboarded system, else ``None``. Fails loud on a coverage read fault."""
    try:
        concept_coverage = covered_concepts(query=query or mr.neptune_query)
    except Exception as e:
        raise MetricResolverError(
            "heatmap coverage query failed while reading graph metadata"
        ) from e
    if metric.concept in concept_coverage:
        return None
    return heatmap_uncovered(
        metric_id, list(dims), threshold_concept,
        [metric.concept], label=metric.label, unit=metric.unit,
        suggestion="onboard a system that provides real-time sensor data")


@dataclass
class _HeatmapResolved:
    """The resolved plan + mappings a heatmap needs to execute and to build its
    envelope. A bundle so ``resolve_heatmap`` stays short; fields mirror the
    locals the inline resolution produced."""
    auth: object
    measure_mapping: object
    dim_x_mapping: object
    dim_y_mapping: object
    threshold_mapping: object
    threshold_join: object
    has_threshold: bool
    plan: HeatmapPlan
    sql: str
    executed_at: str


def _resolve_heatmap_plan(metric, metric_id, dimension_concept_x, dimension_concept_y,
                          threshold_concept, authority, query) -> _HeatmapResolved:
    """Resolve the authority, the measure/dimension mappings (both co-located on
    the measure table), the ``LATEST`` order column, the optional threshold, and
    compose the SQL -- the structural resolution, fail-loud, unchanged."""
    auth = mr._resolve_authority(metric.concept, authority)
    measure_mapping = mr._resolve_table_column(auth.system_id, metric.concept)
    measure_table = measure_mapping.table

    dim_x_candidates = _require_dimension_mappings(auth.system_id, dimension_concept_x, query)
    dim_y_candidates = _require_dimension_mappings(auth.system_id, dimension_concept_y, query)
    dim_x_mapping = _pick_colocated_dimension(dimension_concept_x, dim_x_candidates, measure_table)
    dim_y_mapping = _pick_colocated_dimension(dimension_concept_y, dim_y_candidates, measure_table)

    order_column = None
    if metric.aggregation is Aggregation.LATEST:
        order_column = md._resolve_order_column(auth.system_id, measure_table, query=query)
        if not order_column:
            raise MetricResolverError(
                f"'latest' heatmap metric {metric_id!r} needs a {md.TIME_CONCEPT!r} "
                f"field mapped on measure table {measure_table!r} in system "
                f"{auth.system_id!r}; none found (uncovered)"
            )

    threshold = _resolve_threshold(auth.system_id, measure_table, threshold_concept, query=query)
    threshold_mapping, threshold_join = (threshold if threshold else (None, None))
    has_threshold = threshold_mapping is not None

    plan = HeatmapPlan(
        system_id=auth.system_id, schema=_heatmap_schema(auth.system_id, measure_table),
        measure_table=measure_table, measure_column=measure_mapping.column,
        aggregation=metric.aggregation,
        dim_x_column=dim_x_mapping.column, dim_y_column=dim_y_mapping.column,
        order_column=order_column,
        threshold_table=(threshold_mapping.table if threshold_mapping else None),
        threshold_column=(threshold_mapping.column if threshold_mapping else None),
        threshold_join=threshold_join,
        threshold_schema=_threshold_schema(
            auth.system_id,
            threshold_mapping.table if threshold_join else None, measure_table),
    )
    return _HeatmapResolved(
        auth=auth, measure_mapping=measure_mapping,
        dim_x_mapping=dim_x_mapping, dim_y_mapping=dim_y_mapping,
        threshold_mapping=threshold_mapping, threshold_join=threshold_join,
        has_threshold=has_threshold, plan=plan, sql=_compose_heatmap_sql(plan),
        executed_at=mr._utcnow_iso())


def _heatmap_rows_and_lineage(r: _HeatmapResolved, metric, metric_id,
                              dimension_concept_x, dimension_concept_y, threshold_concept):
    """Execute the plan and build the ``rows`` grid and per-cell ``lineage`` --
    the execution loop, unchanged."""
    rows_out, lineage_out = [], []
    for d1, d2, value, thr in _execute_heatmap(r.plan, r.sql, has_threshold=r.has_threshold):
        sev = _severity(value, thr)
        rows_out.append([d1, d2, value, thr, sev])
        lineage_out.append(_cell_lineage_to_dict(HeatmapCellLineage(
            metric_id=metric_id, concept=metric.concept,
            dimension_concepts=(dimension_concept_x, dimension_concept_y),
            threshold_concept=threshold_concept,
            aggregation=metric.aggregation.value,
            authority=r.auth, measure_mapping=r.measure_mapping,
            dim_x_mapping=r.dim_x_mapping, dim_y_mapping=r.dim_y_mapping,
            threshold_mapping=r.threshold_mapping, threshold_join=r.threshold_join,
            sql=r.sql, executed_at=r.executed_at,
            dim_x_value=d1, dim_y_value=d2, value=value, threshold=thr, severity=sev,
        )))
    return rows_out, lineage_out


def _heatmap_envelope(r: _HeatmapResolved, metric, metric_id, dimension_concept_x,
                      dimension_concept_y, threshold_concept, rows_out, lineage_out) -> dict:
    """Assemble the row-shaped heatmap envelope (S5.3 §6.3), unchanged."""
    return {
        "kind": "heatmap",
        "status": "ok" if rows_out else "no-data",
        "metric_id": metric_id,
        "label": metric.label,
        "unit": metric.unit,
        "system_id": r.auth.system_id,
        "dims": [dimension_concept_x, dimension_concept_y],
        "threshold_concept": threshold_concept,
        "threshold_status": "covered" if r.has_threshold else "uncovered",
        "columns": [
            {"label": CONCEPT_NAME.get(dimension_concept_x, dimension_concept_x),
             "field_concept": dimension_concept_x},
            {"label": CONCEPT_NAME.get(dimension_concept_y, dimension_concept_y),
             "field_concept": dimension_concept_y},
            {"label": metric.label, "field_concept": metric.concept},
        ],
        "rows": rows_out,
        "lineage": lineage_out,
    }


def resolve_heatmap(metric_id: str, dimension_concept_x: str, dimension_concept_y: str, threshold_concept: str | None = None, *, authority: AuthorityResolver | None = None, query=None) -> dict:
    """Resolve a DIRECT metric over two dimension concepts → the heatmap envelope
    (S5.3 §6.3). FAIL LOUD on a structural/resolution error (the route wraps it in
    a friendly HTTP-200 error envelope); degrade cleanly to 'uncovered' threshold
    when the threshold concept is unmapped/sparse. Derived metrics are deferred.

    A dimension with NO ``MAPS_TO`` field on the authoritative system raises
    ``CoverageGapError`` (a mapping gap → the route's 'uncovered' envelope),
    exactly as ``metric_dimension.resolve_metric_by_dimension`` classifies the
    same condition — never an error a Retry could fix (ADR-0065, 2026-09-18)."""
    metric = _resolve_direct_metric(metric_id)
    dims = (dimension_concept_x, dimension_concept_y)
    uncovered = _heatmap_coverage_uncovered(metric, metric_id, dims, threshold_concept, query)
    if uncovered is not None:
        return uncovered
    resolved = _resolve_heatmap_plan(
        metric, metric_id, dimension_concept_x, dimension_concept_y,
        threshold_concept, authority, query)
    rows_out, lineage_out = _heatmap_rows_and_lineage(
        resolved, metric, metric_id, dimension_concept_x, dimension_concept_y,
        threshold_concept)
    return _heatmap_envelope(
        resolved, metric, metric_id, dimension_concept_x, dimension_concept_y,
        threshold_concept, rows_out, lineage_out)
