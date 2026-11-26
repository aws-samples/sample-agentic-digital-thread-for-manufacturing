# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic entity-resolution render path (S5.3 Wave 1 / ADR 0021).

For each onboarded system this reads the S5.2 graph edges
(``System-[:HAS_SCHEMA]->SchemaEntry-[:MAPS_TO]->entityConcept`` plus
``SchemaEntry-[:HAS_FIELD]->Field-[:MAPS_TO]->fieldConcept``) and renders one
**tabular operational section per (system, table)** — real columns, ordered and
labeled by mapped field concepts. Execution routes by the system's
``connection_type`` (mirrors ``metric_resolver._execute``): PostgreSQL through the
existing ``db.query_pg`` schema routing, Athena through
``athena_tools.run_athena_query`` (same composed SQL, cross-account-safe config),
and SiteWise (Direct) through a native latest-value-per-asset read (no SQL exists
for it). Deterministic, no LLM — ADR 0001/0011.

Design discipline — copied from ``coverage.py`` / ``concept_layer.py`` (ADR 0001/0020):
  * Import-pure core — deterministic, no LLM/eval/I-O at import. The ONLY impure seams
    are the injected ``query`` (a graph read) and ``query_fn`` (a SQL executor), both
    default args swappable in tests.
  * ``neptune_query`` / the default ``query_fn`` are LAZY wrappers (they import ``.db``
    on first call) so this module imports WITHOUT pg8000/boto3 and degrades to ``[]``
    / a friendly error when the runtime deps are absent (no-graph / no-DB safe).

Forward-compatible contract (ADR 0029 — panel render-hint contract):
every section carries a ``kind`` render-hint from a closed vocabulary unified with
``Card.viz_type`` (``kpi|gauge|bar|series|heatmap|table|narrative``) and a row-shaped
payload. Only ``table`` is implemented now; ``kind``, ``group_by`` and ``filter`` are the
declared-but-gated forward-compat seams for the deferred rich panels (quality-by-line,
heatmap, active-issues, …) — present in every section but always ``None`` (group_by/filter)
in S5.3. Per-row/section lineage (ADR 0017) is carried from day one.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from . import connection_types
from .catalog import CONCEPT_NAME, catalog, get_schema

logger = logging.getLogger(__name__)

# --- per-request hot-read memos (C2b) ----------------------------------------
# Two reads repeat IDENTICALLY for every section of one system inside a single
# panel render: the SiteWise model enumeration (one describe_asset_model per
# model — ~28 boto3 calls) and the i3X graph Asset-row read. A short TTL memo
# collapses them to once per system per render window. 15s matches the
# frontend's SWR freshness window; entries are tiny (model/property lists).
_HOT_READ_TTL_S = 15.0
_hot_read_memo: dict = {}


def _memoized(key: tuple, fn):
    """Return ``fn()`` memoized under ``key`` for ``_HOT_READ_TTL_S`` seconds.

    Concurrency note: workers render DIFFERENT systems (system-level fan-out),
    and keys are system-scoped, so two threads never race on a useful entry;
    a benign duplicate compute on first contact is acceptable (last write
    wins, both values equivalent). Failures are NOT cached — they propagate
    to the caller's existing boundary."""
    import time as _time
    now = _time.monotonic()
    hit = _hot_read_memo.get(key)
    if hit is not None and now - hit[0] < _HOT_READ_TTL_S:
        return hit[1]
    value = fn()
    _hot_read_memo[key] = (now, value)
    return value

# Render constants behind the ADR-0008 seam (cols/rows caps).
MAX_COLS = 12
MAX_ROWS = 200
# SiteWise (Direct) native-section asset cap — bounds API fan-out (assets × properties).
MAX_SITEWISE_ASSETS = 50


class RenderHint(str, Enum):
    """Closed render-hint vocabulary, unified with ``card_layer.VizType``. Only ``TABLE``
    is implemented in S5.3; the rest are reserved/declared-but-gated for the deferred
    panel rebuild (mirrors how ``card_layer`` gates ``bar|line|gauge``)."""
    KPI = "kpi"
    GAUGE = "gauge"          # reserved
    BAR = "bar"              # reserved
    SERIES = "series"        # reserved
    HEATMAP = "heatmap"      # reserved
    TABLE = "table"          # implemented now
    NARRATIVE = "narrative"  # reserved


@dataclass
class SectionSpec:
    """One (system-agnostic) table section: the entity it renders + ordered/labeled columns.
    ``columns`` is a list of ``{"column", "label", "field_concept"}`` in render order."""
    table: str
    entity: str
    entity_label: str
    columns: list[dict] = field(default_factory=list)


# --- Impure seams (lazy, injectable default args) ---------------------------

def neptune_query(cypher: str, parameters=None) -> list:
    """The graph-read seam — a lazy, no-graph-safe Neptune read. Imports ``.db`` on first
    call so this module imports without pg8000/boto3; degrades to ``[]`` when absent."""
    try:
        from .db import neptune_query as _nq
    except Exception:  # nosec B110 - runtime deps absent (CDK venv) → no-graph safe
        return []
    return _nq(cypher, parameters)


def query_pg(schema: str, sql: str):
    """The SQL-exec seam — a lazy wrapper over ``db.query_pg`` (which routes ``erp_*``/
    ``mes_*``/``public`` to ERP Aurora and everything else to demo Postgres). Imported on
    first call to keep this module import-pure; failures surface to ``render_section``."""
    from .db import query_pg as _qp
    return _qp(schema, sql)


# --- read_sections: ONE graph read → ordered/labeled SectionSpecs -----------

_SECTIONS_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:MAPS_TO]->(ec:Concept) "
    "OPTIONAL MATCH (se)-[:HAS_FIELD]->(f:Field) "
    "OPTIONAL MATCH (f)-[r:MAPS_TO]->(fc:Concept) "
    "RETURN se.name AS table, ec.id AS entity, f.name AS column, "
    "fc.id AS field_concept, r.role AS role ORDER BY se.name, f.name"
)


def _humanize(name: str) -> str:
    """``order_number``/``sales-order`` → ``Order Number``/``Sales Order`` (pure)."""
    return " ".join(w.capitalize() for w in name.replace("-", "_").split("_") if w)


def _role_rank(role) -> int:
    """Identifier roles sort first (rank 0); everything else rank 1."""
    return 0 if role == "identifier" else 1


def _ordered_columns(col_candidates: dict, discovery: list) -> list[dict]:
    """Build the render-ordered, labeled column list for one table (ordering rule):
    field-concept-mapped columns first by ``(role_rank, field_concept_id)``, then unlabeled
    columns in discovered ``f.name`` order. Multi-concept-per-field is collapsed to one
    best mapping (highest role, then lexically smallest concept id)."""
    mapped, unmapped = [], []
    for col in discovery:
        best = min(col_candidates[col], key=lambda c: (_role_rank(c[1]), c[0] or ""))
        fc, _role = best
        if fc is not None:
            mapped.append((col, fc, _role))
        else:
            unmapped.append(col)
    mapped.sort(key=lambda t: (_role_rank(t[2]), t[1]))
    cols = [{"column": c, "label": CONCEPT_NAME.get(fc) or _humanize(c),
             "field_concept": fc} for c, fc, _r in mapped]
    cols += [{"column": c, "label": _humanize(c), "field_concept": None} for c in unmapped]
    return cols


def read_sections(system_id: str, *, query=neptune_query) -> list[SectionSpec]:
    """ONE graph read → a ``SectionSpec`` per (table, entity) for ``system_id``.

    Tables with ZERO Field nodes are SKIPPED (no empty SELECT). ``*_raw`` tables are
    dropped when the non-raw view of the same base name is present in the same system.
    No-graph safe (empty read → ``[]``)."""
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in query(_SECTIONS_CYPHER, {"sid": system_id}):
        col = row.get("column")
        if not col:  # zero-Field table (OPTIONAL MATCH miss) — register nothing
            continue
        key = (row.get("table"), row.get("entity"))
        if key not in groups:
            groups[key] = {"cands": {}, "discovery": []}
            order.append(key)
        g = groups[key]
        if col not in g["cands"]:
            g["cands"][col] = []
            g["discovery"].append(col)
        g["cands"][col].append((row.get("field_concept"), row.get("role")))

    specs = [
        SectionSpec(table=tbl, entity=ent,
                    entity_label=CONCEPT_NAME.get(ent) or _humanize(ent or ""),
                    columns=_ordered_columns(g["cands"], g["discovery"]))
        for (tbl, ent) in order
        for g in (groups[(tbl, ent)],)
    ]
    present = {s.table for s in specs}
    return [s for s in specs
            if not (s.table.endswith("_raw") and s.table[:-4] in present)]


# --- build_section_sql: pure, validated, no SELECT * ------------------------

def build_section_sql(schema: str, spec: SectionSpec, *,
                      max_cols: int = MAX_COLS, max_rows: int = MAX_ROWS) -> str:
    """Compose a read-only ``SELECT "c1","c2",... FROM "schema"."table" LIMIT n`` (pure).

    Every identifier (schema, table, each column) is validated via the same
    ``db.validate_identifier`` used elsewhere; columns are capped at ``max_cols``. No
    ``SELECT *``. Raises ``ValueError`` on any invalid identifier."""
    from .db import validate_identifier
    schema = validate_identifier(schema)
    table = validate_identifier(spec.table)
    cols = [validate_identifier(c["column"]) for c in spec.columns[:max_cols]]
    if not cols:
        raise ValueError(f"section {spec.table!r} has no columns to select")
    col_sql = ",".join(f'"{c}"' for c in cols)
    # nosec B608 / nosemgrep: identifiers validated by validate_identifier(); no user
    # values are interpolated — only schema/table/column names that match ^[A-Za-z_]\w*$.
    return f'SELECT {col_sql} FROM "{schema}"."{table}" LIMIT {max_rows}'  # nosec B608


# --- Athena struct-aware variant (nested ROW field paths) --------------------
# Athena semi-structured schemas can carry DOTTED STRUCT PATHS as graph column
# names (e.g. 'parent_struct.nested.leaf') when physical-column discovery was
# skipped at onboarding (cross-account AssumeRole failure — onboard.py logs
# 'Athena physical-column discovery skipped'). Those are valid Athena/Trino ROW
# field dereferences, not invalid identifiers — but ONLY on the Athena path.
# PostgreSQL sections keep ``build_section_sql``'s flat validation (a dotted
# column name there is still a hard ValueError).

MAX_STRUCT_DEPTH = 5  # bound on dotted-path parts — deeper is config noise, not data


def _athena_select_item(path: str) -> str:
    """One Athena SELECT item for a graph column name (pure).

    A flat name emits exactly what ``build_section_sql`` emits (``"col"``). A
    dotted name is treated as an Athena/Trino ROW (struct) field dereference:
    EVERY part is validated by the SAME ``db.validate_identifier`` regex
    (injection safety is per-part — no part may carry quotes/spaces/semicolons),
    each part is double-quoted and dot-joined (``"a"."b"."c"``), and the item is
    aliased back to the full dotted name (``AS "a.b.c"``) so result-row keys
    match the graph column name the envelope/lineage carry. Depth is bounded at
    ``MAX_STRUCT_DEPTH``. Raises ``ValueError`` on any invalid part."""
    from .db import validate_identifier
    parts = path.split(".")
    if len(parts) == 1:
        return f'"{validate_identifier(path)}"'
    if len(parts) > MAX_STRUCT_DEPTH:
        raise ValueError(
            f"struct path too deep ({len(parts)} > {MAX_STRUCT_DEPTH}): {path!r}")
    deref = ".".join(f'"{validate_identifier(p)}"' for p in parts)
    # The alias is safe to double-quote: every part matched ^[A-Za-z_]\w*$,
    # so the joined path contains only word characters and dots.
    return f'{deref} AS "{path}"'


def build_athena_section_sql(schema: str, spec: SectionSpec, *,
                             max_cols: int = MAX_COLS, max_rows: int = MAX_ROWS) -> str:
    """Athena variant of ``build_section_sql`` — struct-aware SELECT items (pure).

    Identical shape/validation discipline (validated schema/table, capped
    columns, no ``SELECT *``), but dotted column names compose ROW field
    dereferences via ``_athena_select_item``. A spec with only flat columns
    composes BYTE-IDENTICAL SQL to ``build_section_sql``. Raises ``ValueError``
    on any invalid identifier part, empty columns, or over-deep paths."""
    from .db import validate_identifier
    schema = validate_identifier(schema)
    table = validate_identifier(spec.table)
    items = [_athena_select_item(c["column"]) for c in spec.columns[:max_cols]]
    if not items:
        raise ValueError(f"section {spec.table!r} has no columns to select")
    col_sql = ",".join(items)
    # nosec B608 / nosemgrep: every identifier PART is validated by
    # validate_identifier(); no user values are interpolated.
    return f'SELECT {col_sql} FROM "{schema}"."{table}" LIMIT {max_rows}'  # nosec B608


# --- build_filtered_section_sql: WHERE from a CLOSED predicate vocabulary ----
# Wave 2 (active-issues §6.2) activates ``entity_render``'s declared-but-gated ``filter``
# seam. The security-sensitive surface is the ``WHERE`` builder, so it is deliberately
# narrow: a predicate is a (column, operator, value) triple drawn from a CLOSED vocabulary.
#   * the COLUMN is validated by the same ``db.validate_identifier`` used everywhere here;
#   * the OPERATOR is an ENUM member (no free-text operators);
#   * the VALUE is either a number (coerced via float/int) embedded bare, or a string drawn
#     from a strict uppercase-enum allowlist (``^[A-Z][A-Z0-9_]*$``) single-quoted — so it
#     can carry no quote/space/semicolon/comment and cannot break out of the literal.
# NO raw user/string value is ever %-interpolated into SQL (mirrors the ``# nosec B608``
# discipline of ``build_section_sql`` / ``metric_resolver._compose_sql``).


class PredicateOp(str, Enum):
    """Closed operator vocabulary for a section ``filter`` predicate. Free-text operators
    are impossible — the builder maps each enum member to a fixed SQL operator token."""
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    GT = "gt"
    LE = "le"
    GE = "ge"
    IN = "in"


_PREDICATE_OP_SQL: dict = {
    PredicateOp.EQ: "=",
    PredicateOp.NE: "<>",
    PredicateOp.LT: "<",
    PredicateOp.GT: ">",
    PredicateOp.LE: "<=",
    PredicateOp.GE: ">=",
}

# Closed allowlist for STRING literal values: uppercase enum tokens only (status codes
# like ``BACKORDERED`` / ``CANCELLED``). A value that matches this pattern cannot contain
# a quote, space, semicolon, or comment marker — so single-quoting it is injection-safe.
_ENUM_LITERAL = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class Predicate:
    """One closed-vocabulary section filter predicate: ``column <op> value``.

    ``value`` is a number (int/float, coerced+embedded bare), an allowlisted enum string
    (single-quoted), or — for ``PredicateOp.IN`` — a tuple/list of such literals."""
    column: str
    op: PredicateOp
    value: object


def _render_literal(value) -> str:
    """Render ONE predicate literal safely (pure). Numbers are coerced and embedded bare;
    enum strings are allowlist-validated then single-quoted. Raises ``ValueError`` on any
    value that is not a number or an allowlisted enum token (closed vocabulary)."""
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        raise ValueError(f"boolean predicate value not allowed: {value!r}")
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, str):
        if not _ENUM_LITERAL.match(value):
            raise ValueError(
                f"predicate string value not in closed allowlist (^[A-Z][A-Z0-9_]*$): {value!r}"
            )
        return f"'{value}'"
    raise ValueError(f"unsupported predicate value type: {type(value).__name__}")


def _predicate_sql(pred: Predicate) -> str:
    """Compose ONE validated SQL predicate from the closed vocabulary (pure).

    The column is validated via ``db.validate_identifier``; the operator must be a
    ``PredicateOp``; the value(s) go through ``_render_literal``. Raises ``ValueError`` on
    any invalid column / operator / value."""
    from .db import validate_identifier
    col = validate_identifier(pred.column)
    op = pred.op if isinstance(pred.op, PredicateOp) else PredicateOp(pred.op)
    if op is PredicateOp.IN:
        values = pred.value if isinstance(pred.value, (list, tuple)) else (pred.value,)
        if not values:
            raise ValueError("IN predicate requires at least one value")
        rendered = ",".join(_render_literal(v) for v in values)
        return f'"{col}" IN ({rendered})'
    return f'"{col}" {_PREDICATE_OP_SQL[op]} {_render_literal(pred.value)}'


def build_filtered_section_sql(schema: str, spec: SectionSpec, predicates, *,
                               max_cols: int = MAX_COLS, max_rows: int = MAX_ROWS) -> str:
    """Like ``build_section_sql`` but appends a ``WHERE`` built from a CLOSED predicate
    vocabulary (pure). Every schema/table/column identifier is validated via
    ``db.validate_identifier``; predicate operators are enum members and predicate values
    are number-coerced or enum-allowlisted (see ``_predicate_sql``) — NEVER a raw value
    interpolated into SQL. Multiple predicates are combined with ``AND``. Raises
    ``ValueError`` on any invalid identifier / operator / value."""
    from .db import validate_identifier
    schema = validate_identifier(schema)
    table = validate_identifier(spec.table)
    cols = [validate_identifier(c["column"]) for c in spec.columns[:max_cols]]
    if not cols:
        raise ValueError(f"section {spec.table!r} has no columns to select")
    col_sql = ",".join(f'"{c}"' for c in cols)
    where = ""
    if predicates:
        where = " WHERE " + " AND ".join(_predicate_sql(p) for p in predicates)
    # nosec B608 / nosemgrep: identifiers validated by validate_identifier(); predicate
    # operators are PredicateOp enum members and values are number-coerced or matched
    # against ^[A-Z][A-Z0-9_]*$ — no raw/user string is interpolated.
    return f'SELECT {col_sql} FROM "{schema}"."{table}"{where} LIMIT {max_rows}'  # nosec B608


# --- render_section: build SQL, execute, return the row-shaped contract -----

def _section_base(system: dict, spec: SectionSpec, columns: list[dict]) -> dict:
    """The shared (status-agnostic) section envelope — the forward-compatible contract."""
    return {
        "system_id": system.get("id"),
        "system_name": system.get("name"),
        "entity": spec.entity,
        "entity_label": spec.entity_label,
        "table": spec.table,
        "kind": RenderHint.TABLE.value,
        "columns": columns,
        "group_by": None,   # declared-but-gated forward-compat seam (S5.3 always None)
        "filter": None,     # declared-but-gated forward-compat seam (S5.3 always None)
        "lineage": [{"concept": c["field_concept"], "system_id": system.get("id"),
                     "table": spec.table, "column": c["column"]} for c in columns],
    }


def render_section(system: dict, spec: SectionSpec, *, query_fn=query_pg) -> dict:
    """Build the section by the system's ``connection_type`` and return the
    row-shaped section (mirrors ``metric_resolver._execute``'s routing):

    * **PostgreSQL** (default) — deterministic SQL through the schema-routing seam
      (``db.query_pg`` sends ``erp_*``/``mes_*`` to ERP Aurora and everything else
      to demo Postgres). Unchanged legacy path.
    * **Athena** — the SAME composed ``SELECT "c1",... FROM "schema"."table" LIMIT n``
      executed via ``athena_tools.run_athena_query`` with the cross-account-safe
      config (see ``_render_athena_section``).
    * **SiteWise (Direct)** — SQL cannot work; renders a native latest-value table
      instead (see ``_render_sitewise_section``).
    * **SiteWise (i3X)** — no SQL and no boto3 SiteWise API; renders the onboarded
      graph Asset hierarchy with an honest fallback (see ``_render_i3x_section``).

    NEVER raises and NEVER fabricates rows: any boundary error → ``status='error'``
    with a friendly message naming the connection type + empty rows, while the
    detail is logged server-side."""
    columns = spec.columns[:MAX_COLS]
    base = _section_base(system, spec, columns)
    schema = system.get("schema") or get_schema(system.get("id", ""))
    conn_type = system.get("connection_type") or connection_types.POSTGRESQL
    try:
        if conn_type == connection_types.SITEWISE_I3X:
            # Dispatched BEFORE the PG fall-through: an i3X system carries no SQL
            # schema, so falling through raised a ValueError + one WARNING
            # traceback PER SECTION. Structural graph render instead.
            return _render_i3x_section(system, spec, base)
        if conn_type == connection_types.SITEWISE_DIRECT:
            return _render_sitewise_section(system, spec)
        if conn_type == connection_types.ATHENA:
            return _render_athena_section(system, spec, base, columns, schema)
        sql = build_section_sql(schema, spec)
        raw = query_fn(schema, sql)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        rows = parsed.get("rows") or []
        return {**base, "rows": rows,
                "row_count": parsed.get("row_count", len(rows)), "status": "ok"}
    except Exception:  # boundary: never raise, never fabricate rows
        logger.warning("entity_render: section failed for %s.%s (connection_type=%s)",
                       schema, spec.table, conn_type, exc_info=True)
        return {**base, "rows": [], "row_count": 0, "status": "error",
                "error": (f"Section unavailable for {spec.entity_label} "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                          f"({spec.table}) via {conn_type}")}


def _render_athena_section(system: dict, spec: SectionSpec, base: dict,
                           columns: list[dict], schema: str) -> dict:
    """The Athena route — logic moved VERBATIM out of ``render_section`` (50-line
    gate). Resolves a missing catalog schema from the connection config, executes
    the composed SELECT via the shared Lake-Formation column-retry seam, and
    rebuilds the envelope/lineage when denied columns were pruned. Unrecoverable
    errors propagate to ``render_section``'s boundary."""
    if not schema:
        # An Athena system may carry NO catalog schema — its database name
        # lives in the connection config (the same 'database' key
        # athena_tools.run_athena_query puts in its QueryExecutionContext).
        # Resolve it BEFORE composing SQL so we never hand
        # build_section_sql an empty identifier (cryptic ValueError).
        schema = _athena_schema_from_config(system.get("id", ""))
        if not schema:
            logger.warning(
                "entity_render: no Athena database for section %s (system %s) — "
                "catalog schema and connection config 'database' both missing",
                spec.table, system.get("id"))
            return {**base, "rows": [], "row_count": 0, "status": "error",
                    "error": (f"Section unavailable for {spec.entity_label} "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                              f"({spec.table}) via {connection_types.ATHENA}: "
                              f"no Athena database configured (catalog schema and "
                              f"connection config 'database' are both missing)")}
    outcome = _run_athena_section(system.get("id", ""), schema, spec)
    parsed = outcome["result"]
    if outcome["denied"]:
        # Lake-Formation-denied columns were pruned: the envelope and
        # lineage must reflect the ACTUALLY selected columns, plus a
        # note naming what was omitted (never a whole-section error).
        kept = set(outcome["columns"])
        columns = [c for c in columns if c["column"] in kept]
        base = _section_base(system, spec, columns)
        base["note"] = (
            f"{len(outcome['denied'])} column(s) omitted — SELECT not "
            f"authorized for this principal (Lake Formation column-level "
            f"permissions): " + ", ".join(outcome["denied"])
        )
    rows = parsed.get("rows") or []
    return {**base, "rows": rows,
            "row_count": parsed.get("row_count", len(rows)), "status": "ok"}


def _render_i3x_section(system: dict, spec: SectionSpec, base: dict) -> dict:
    """SiteWise (i3X) section — structural graph-asset render, honest fallback.

    An i3X system's connection is the i3X REST API (``base_url`` — see
    ``agents/i3x_envelope.py``), NOT the boto3 ``iotsitewise`` SDK that
    ``_render_sitewise_section`` requires, so that path is NOT reusable here.
    i3X onboarding (``agents/onboard_i3x.py``) writes the Asset hierarchy into
    the graph, so the graph asset table is the genuinely useful rendering:
    structural data only — no telemetry reads (zero claim-check exposure) and
    no i3X envelope code (that lives ONLY in ``agents/i3x_envelope.py``).

    When the graph holds NO Asset nodes for the system (or the graph read
    fails), returns an honest ``status='error'`` section naming the connection
    type with ``rows=[], row_count=0`` (ADR-0045 — never fabricate rows) and
    logs ONE DEBUG line — never a WARNING traceback."""
    try:
        section = _render_graph_asset_section(system, spec)
    except Exception as exc:  # nosec B110 - graph read failed → same honest fallback
        logger.debug("entity_render: i3X graph asset read failed for %s (system %s): %s",
                     spec.table, system.get("id"), type(exc).__name__)
        section = None
    if section is not None:
        return section
    logger.debug(
        "entity_render: no graph Asset hierarchy for i3X section %s (system %s)",
        spec.table, system.get("id"))
    return {**base, "rows": [], "row_count": 0, "status": "error",
            "error": (f"Section unavailable for {spec.entity_label} "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
                      f"({spec.table}) via {connection_types.SITEWISE_I3X}: "
                      f"no onboarded asset hierarchy is in the knowledge "
                      f"graph for this system yet")}


def _athena_schema_from_config(system_id: str) -> str:
    """Resolve an Athena system's database (its 'schema' for SQL composition)
    from the FULL connection config when the catalog carries no schema.

    Reads the SAME cross-account-safe config ``_run_athena_section`` executes
    with (``metric_resolver._full_connection_config``) and the SAME key
    precedence ``athena_tools.run_athena_query`` uses for its
    QueryExecutionContext — ``database`` first, legacy ``schema`` fallback.
    Returns '' when neither key is present (the caller emits a clear
    ``status='error'`` instead of a cryptic empty-identifier ValueError).
    Lazy imports keep this module import-pure; a config-resolution failure is
    treated as 'no database' (never raises)."""
    try:
        from .metric_resolver import _full_connection_config
        cfg = _full_connection_config(system_id) or {}
    except Exception:  # nosec B110 - config unavailable → caller errors clearly
        logger.warning("entity_render: connection config resolution failed for %r",
                       system_id, exc_info=True)
        return ""
    return str(cfg.get("database", "") or cfg.get("schema", "") or "").strip()


def _run_athena_section(system_id: str, schema: str, spec: SectionSpec) -> dict:
    """Execute a section SELECT for an Athena system (lazy imports keep this module
    import-pure). Reuses the SAME connector + cross-account config resolution the
    metric resolver's ``_execute`` uses — via the SHARED
    ``athena_tools.run_athena_select_with_column_retry`` seam, so a
    COLUMN_NOT_FOUND naming a composed column (its SELECT denied by a Lake
    Formation column-level grant) drops that column and retries (bounded) instead
    of failing the whole section. Returns the helper's outcome
    ``{"result", "columns", "denied"}`` — ``columns`` are the ACTUALLY selected
    names so the caller's envelope/lineage reflect reality. Unrecoverable errors
    propagate to ``render_section``'s boundary."""
    from . import athena_tools
    from .metric_resolver import _full_connection_config
    by_name = {c["column"]: c for c in spec.columns[:MAX_COLS]}
    nested = [n for n in by_name if "." in n]
    if nested:
        # Honest breadcrumb (ONE line per section, not per column): dotted graph
        # column names mean physical-column discovery was skipped at onboarding
        # (see onboard.py 'Athena physical-column discovery skipped') — we
        # compose struct dereferences from the logical paths instead.
        logger.info(
            "entity_render: section %s.%s selects %d nested struct path(s) — "
            "physical-column discovery was skipped at onboarding; composing "
            "Athena ROW field access from the dotted graph column names",
            schema, spec.table, len(nested))

    def _compose(names: list[str]) -> str:
        pruned = SectionSpec(table=spec.table, entity=spec.entity,
                             entity_label=spec.entity_label,
                             columns=[by_name[n] for n in names])
        return build_athena_section_sql(schema, pruned)

    # Unverified struct paths (discovery skipped) may ALL be wrong — they must
    # not consume the bounded Lake-Formation prune budget (live 2026-07-25:
    # 4 wrong paths > default budget ⇒ whole section error'd on the ORIGINAL
    # failure). Extend by the nested count; essential columns stay protected.
    max_retries = (len(nested) + athena_tools.MAX_DENIED_COLUMN_RETRIES
                   if nested else None)
    return athena_tools.run_athena_select_with_column_retry(
        _compose, list(by_name), _full_connection_config(system_id),
        max_retries=max_retries)


def _format_epoch_seconds(ts) -> str:
    """Render a SiteWise ``timeInSeconds`` timestamp as an ISO-8601 UTC string
    (pure; empty string when absent/invalid — an empty cell, never an error)."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _match_section_columns_to_properties(model: dict, spec: SectionSpec,
                                         system_id: str) -> list[tuple[dict, str]]:
    """Match the section's graph-discovered columns to REAL model properties by
    name; columns with no matching property are dropped (nothing to read).
    Raises ``ValueError`` when NOTHING matches (propagates to the render
    boundary as the honest per-section error)."""
    from .metric_resolver import _match_by_name

    props = model.get("properties") or []
    matched: list[tuple[dict, str]] = []
    for col in spec.columns[:MAX_COLS]:
        prop = _match_by_name(props, col["column"])
        if prop and prop.get("id"):
            matched.append((col, prop["id"]))
    if not matched:
        raise ValueError(
            f"no section columns match properties on asset model {spec.table!r} "
            f"in system {system_id!r}"
        )
    return matched


def _sitewise_latest_value_rows(assets: list, matched: list, config: dict,
                                region: str | None) -> list[list]:
    """One row per asset: name, latest value per matched property, newest
    observation timestamp. A property with no data / a per-entry read failure
    yields an EMPTY cell (never fails the row). Reads go through ONE
    ``batch_get_property_values`` sweep for the whole section (the same
    batch reader the metric resolver uses — sequential pages inside, so the
    serial-telemetry invariant holds; a per-entry serial sweep would
    cost ~90s across a full panel render)."""
    from . import sitewise_read

    ids = [a.get("id") for a in assets if a.get("id")]
    entries = [{"assetId": aid, "propertyId": prop_id}
               for aid in ids for _col, prop_id in matched]
    values = sitewise_read.batch_get_property_values(config, region, entries) \
        if entries else {}
    rows: list[list] = []
    for asset in assets:
        asset_id = asset.get("id")
        if not asset_id:
            continue
        cells: list = [asset.get("name") or asset_id]
        newest_ts = None
        for _col, prop_id in matched:
            pv = values.get(f"{asset_id}_{prop_id}")
            if not isinstance(pv, dict) or pv.get("error") or pv.get("value") is None:
                cells.append(None)  # no data → empty cell, not an error
                continue
            cells.append(pv.get("value"))
            ts = pv.get("timestamp")
            if ts is not None and (newest_ts is None or ts > newest_ts):
                newest_ts = ts
        cells.append(_format_epoch_seconds(newest_ts))
        rows.append(cells)
    return rows


def _render_sitewise_section(system: dict, spec: SectionSpec) -> dict:
    """Native section for a SiteWise (Direct) system — no SQL is possible, so
    render the useful equivalent: one row per asset of the section's model with
    the LATEST value per mapped column (see ``_sitewise_latest_value_rows``) via
    the shared ``sitewise_read`` seam (DRY; caps: MAX_SITEWISE_ASSETS/MAX_COLS).
    Enumeration failures propagate to ``render_section``'s error boundary;
    rows are NEVER fabricated."""
    from . import sitewise_read
    from .metric_resolver import _full_connection_config, _match_by_name

    system_id = system.get("id", "")
    config = _full_connection_config(system_id)
    region = config.get("region") or os.environ.get("AWS_REGION")

    _list_models = sitewise_read.list_asset_models_with_properties
    models = _memoized(  # id() key part: see _render_graph_asset_section note
        ("sitewise-models", system_id, id(_list_models)),
        lambda: _list_models(config, region))
    model = _match_by_name(models, spec.table)
    if not model or not model.get("id"):
        # The section's table matches NO asset model — discovery intentionally
        # emits non-model pseudo-tables (e.g. the backward-compatible hierarchy
        # table). When the graph holds the onboarded Asset hierarchy for this
        # system, render the section from those Asset nodes instead of erroring.
        # Generic: keyed off 'no model matched' + 'graph has Assets', never a
        # table-name literal.
        section = _render_graph_asset_section(system, spec)
        if section is not None:
            return section
        raise ValueError(
            f"no SiteWise asset model named {spec.table!r} in system {system_id!r}"
        )
    matched = _match_section_columns_to_properties(model, spec, system_id)

    assets = sitewise_read.list_assets(
        config, region, asset_model_id=model["id"]
    )[:MAX_SITEWISE_ASSETS]

    rows = _sitewise_latest_value_rows(assets, matched, config, region)

    # Adjusted columns for the SiteWise shape: asset name first, then the mapped
    # properties (latest values), then the newest-observation timestamp. Labels are
    # generic (no dataset literals); the envelope contract is _section_base's.
    columns = (
        [{"column": "asset", "label": "Asset", "field_concept": None}]
        + [col for col, _pid in matched]
        + [{"column": "timestamp", "label": "As Of (UTC)", "field_concept": None}]
    )
    base = _section_base(system, spec, columns)
    return {**base, "rows": rows, "row_count": len(rows), "status": "ok"}


# The Asset-node hierarchy read for a non-model SiteWise section table. Columns
# mirror exactly what onboarding's Asset writer persists on each node
# (``MERGE (a:Asset {source_id}) SET a.name/a.asset_type/a.system/a.level/a.path``).
_ASSET_ROWS_CYPHER = (
    "MATCH (a:Asset {system: $sid}) "
    "RETURN a.name AS name, a.asset_type AS asset_type, a.path AS path, "
    "a.level AS level ORDER BY a.path, a.name "
    f"LIMIT {MAX_ROWS}"
)

_ASSET_SECTION_COLUMNS = [
    {"column": "name", "label": "Asset", "field_concept": None},
    {"column": "asset_type", "label": "Asset Type", "field_concept": None},
    {"column": "path", "label": "Path", "field_concept": None},
    {"column": "level", "label": "Level", "field_concept": None},
]


def _render_graph_asset_section(system: dict, spec: SectionSpec, *, query=None) -> dict | None:
    """Render a SiteWise section whose table is NOT an asset model from the
    graph's onboarded Asset hierarchy — one row per Asset node of this system
    (name / asset_type / path / level, the exact properties onboarding writes),
    capped at ``MAX_ROWS``.

    Returns ``None`` when the graph holds NO Asset nodes for the system (the
    caller keeps its honest 'no asset model' error). The graph read goes through
    the module's ``neptune_query`` seam (monkeypatchable; no-graph safe → [])."""
    q = query or neptune_query
    # id(q) in the key: a swapped query seam (tests, injectors) must never
    # share entries with the module seam — same system, different source.
    raw = _memoized(("graph-asset-rows", system.get("id", ""), id(q)),
                    lambda: q(_ASSET_ROWS_CYPHER, {"sid": system.get("id", "")}))
    rows = [[r.get("name"), r.get("asset_type"), r.get("path"), r.get("level")]
            for r in raw if r.get("name")]
    if not rows:
        return None
    rows = rows[:MAX_ROWS]
    base = _section_base(system, spec, list(_ASSET_SECTION_COLUMNS))
    return {**base, "rows": rows, "row_count": len(rows), "status": "ok"}


# --- system / all-systems fan-out -------------------------------------------

def render_system_sections(system: dict, *, table: str | None = None,
                           query=neptune_query, query_fn=query_pg) -> list[dict]:
    """Render one system's sections, optionally restricted to an exact table.

    Discovery remains one bounded graph read. Filtering happens before any
    source query, so a targeted drill cannot scan sibling tables.
    """
    specs = read_sections(system.get("id", ""), query=query)
    if table is not None:
        specs = [spec for spec in specs if spec.table == table]
    return [render_section(system, spec, query_fn=query_fn) for spec in specs]


def _render_system_sections_safe(system: dict, *, table: str | None = None,
                                 query=neptune_query,
                                 query_fn=query_pg) -> list[dict]:
    """System-level graph boundary: one system's section discovery NEVER
    raises. ``render_section`` already contains per-section source failures;
    this guards the earlier ``read_sections`` graph seam for full fan-out and
    exact targeted reads, yielding ``[]`` + ONE warning on failure."""
    try:
        return render_system_sections(
            system, table=table, query=query, query_fn=query_fn)
    except Exception:  # boundary: a system render must never raise
        logger.warning("entity_render: system render failed for %s",
                       system.get("id"), exc_info=True)
        return []


def render_all_sections(systems=None, *, query=neptune_query, query_fn=query_pg) -> list[dict]:
    """All rendered sections across onboarded systems (defaults to
    ``catalog.get_onboarded()``), fanned out in parallel ACROSS SYSTEMS.

    Concurrency contract (pinned by the serial-telemetry invariant):
    parallelism is ONE bounded worker PER SYSTEM
    — never per section. Section-level parallelism would fan out concurrent
    SiteWise/telemetry calls against ONE system, violating the pinned
    serial-telemetry invariant; system-level parallelism keeps every per-system
    telemetry sequence strictly serial (sections render serially inside each
    worker) while collapsing wall-clock to ~max(per-system time). Workers are
    bounded at ``min(8, len(systems))``; futures are submitted in input order
    and collected in that SAME order, so the flattened output order is
    byte-identical to the serial implementation's."""
    systems = catalog.get_onboarded() if systems is None else list(systems)
    # A vendor REST system has no section reader (see
    # ``connection_types.NO_STRUCTURED_READER_CONNECTION_TYPES``); rendering it
    # only produced one "Section unavailable" row per table.
    systems = [s for s in systems if connection_types.section_read_supported(s)]
    if not systems:
        return []
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(systems))) as pool:
        futures = [pool.submit(_render_system_sections_safe, system,
                               query=query, query_fn=query_fn)
                   for system in systems]
        for future in futures:  # in-order collection preserves section order
            out.extend(future.result())
    return out
