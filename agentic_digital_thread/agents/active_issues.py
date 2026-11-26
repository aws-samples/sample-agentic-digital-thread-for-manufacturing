# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Active-issues composer (S5.3 Item 1, panel-rebuild research §6.2 / §4).

A cross-entity **filtered record list** of current operational problems, unioned from a
small set of FIXED entity sources. Each emitted issue item is::

    {severity, title, detail, source_system, entity, lineage}

where ``severity`` is ``'warning' | 'critical'`` and is computed **server-side,
deterministically** from the row's own values (never sourced from a free-text column).
``lineage`` carries the exact read-only SQL that produced the row (ADR 0017 style).

Design discipline (mirrors ``entity_render`` / ``metric_resolver``):
  * Import-pure core — deterministic, no LLM/eval/I-O at import. The only impure seam is the
    injected ``query_fn`` (a SQL executor), a default arg swappable in tests.
  * ``query_pg`` is a LAZY wrapper (imports ``.db`` on first call) so this module imports
    WITHOUT pg8000/boto3 (CDK venv safe).
  * SQL safety is delegated entirely to ``entity_render.build_filtered_section_sql`` + its
    CLOSED predicate vocabulary (``Predicate``/``PredicateOp``): validated identifiers,
    enum operators, number-coerced / enum-allowlisted literals. NO raw value is ever
    interpolated into SQL here.
  * Fail-LOUD on a bad mapping (a column index/coercion error raises) — the *route*
    (``dashboard.py``) degrades each failure to a friendly HTTP-200 envelope.

The three sources are confined to columns/joins confirmed present in the seeded schema +
catalog ``query_guidance``:
  * MES ``quality_checks`` WHERE ``pass_rate < 90`` (the generated 0-100 pass-rate column).
  * MES ``work_orders`` WHERE ``status IN ('CANCELLED','PAUSED')`` (status values indicating
    a problem — from the table's CHECK-constraint allowlist).
  * ERP ``customer_orders`` WHERE ``status = 'BACKORDERED'`` (exact enum from the schema).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from .catalog import catalog, get_schema
from .entity_render import Predicate, PredicateOp, SectionSpec, build_filtered_section_sql

logger = logging.getLogger(__name__)

# Cap rows per source so one noisy system can't flood the panel (ADR 0008 seam style).
MAX_ISSUES_PER_SOURCE = 50

# Server-side severity thresholds (deterministic; never read from a free-text column).
_QUALITY_CRITICAL_PASS_RATE = 70.0   # pass_rate below this is critical, else warning


# --- Impure seam (lazy, injectable default arg) -----------------------------

def query_pg(schema: str, sql: str):
    """The SQL-exec seam — a lazy wrapper over ``db.query_pg`` (routes ``erp_*``/``mes_*``/
    ``public`` to ERP Aurora and everything else to demo Postgres). Imported on first call to
    keep this module import-pure; failures surface to the caller (route degrades)."""
    from .db import query_pg as _qp
    return _qp(schema, sql)


# --- Issue source specs (fixed, deterministic) ------------------------------

@dataclass(frozen=True)
class IssueSource:
    """One fixed cross-entity issue source: which system TYPE provides it, the view/table
    to read, the ordered columns to SELECT, the closed-vocabulary filter predicates, and a
    pure ``classify(row) -> {severity, title, detail}`` mapper. ``row`` is a column→value
    dict built positionally from the SELECT column order."""
    key: str
    entity: str
    system_type: str
    table: str
    columns: list
    predicates: list
    classify: Callable[[dict], dict]


def _num(value):
    """Coerce a SQL scalar to float for threshold comparison; None on non-numeric/None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _classify_quality(row: dict) -> dict:
    """MES quality failure → severity by pass_rate, human title/detail (pure)."""
    pass_rate = _num(row.get("pass_rate"))
    severity = "critical" if (pass_rate is not None and pass_rate < _QUALITY_CRITICAL_PASS_RATE) else "warning"
    check_type = row.get("check_type") or "Quality"
    rate_txt = f"{pass_rate:.1f}%" if pass_rate is not None else "unknown"
    fail_count = row.get("fail_count")
    sample = row.get("sample_size")
    counts = ""
    if fail_count is not None and sample is not None:
        counts = f" ({fail_count} of {sample} failed)"
    return {
        "severity": severity,
        "title": f"{check_type} check below threshold",
        "detail": f"Pass rate {rate_txt}{counts}",
    }


def _classify_work_order(row: dict) -> dict:
    """MES work order in a problem status → severity by status (pure)."""
    status = (row.get("status") or "").upper()
    severity = "critical" if status == "CANCELLED" else "warning"
    wo = row.get("work_order_number") or "Work order"
    line = row.get("line")
    good = row.get("good_quantity")
    planned = row.get("planned_quantity")
    bits = []
    if line:
        bits.append(str(line))
    if good is not None and planned is not None:
        bits.append(f"{good}/{planned} good units")
    suffix = (" — " + ", ".join(bits)) if bits else ""
    return {
        "severity": severity,
        "title": f"{wo} {status.lower() or 'flagged'}",
        "detail": f"Work order status {status or 'unknown'}{suffix}",
    }


def _classify_backordered(row: dict) -> dict:
    """ERP backordered order → warning severity, human title/detail (pure)."""
    order = row.get("order_number") or "Order"
    total = row.get("total_amount")
    detail = "Order is backordered"
    if total is not None:
        detail = f"Order is backordered (total {total})"
    return {
        "severity": "warning",
        "title": f"{order} backordered",
        "detail": detail,
    }


ISSUE_SOURCES: list = [
    IssueSource(
        key="quality-fail",
        entity="quality-check",
        system_type="MES",
        table="quality_checks",
        columns=["check_type", "pass_rate", "fail_count", "sample_size", "work_order_id"],
        predicates=[Predicate("pass_rate", PredicateOp.LT, 90)],
        classify=_classify_quality,
    ),
    IssueSource(
        key="work-order-flagged",
        entity="work-order",
        system_type="MES",
        table="work_orders",
        columns=["work_order_number", "status", "good_quantity", "planned_quantity", "line"],
        predicates=[Predicate("status", PredicateOp.IN, ("CANCELLED", "PAUSED"))],
        classify=_classify_work_order,
    ),
    IssueSource(
        key="order-backordered",
        entity="sales-order",
        system_type="ERP",
        table="customer_orders",
        columns=["order_number", "status", "total_amount"],
        predicates=[Predicate("status", PredicateOp.EQ, "BACKORDERED")],
        classify=_classify_backordered,
    ),
]


# --- Composition ------------------------------------------------------------

def _systems_of_type(systems: list, system_type: str) -> list:
    """Onboarded systems whose ``system_type`` matches the source's expected type
    AND whose connection the fixed sources can actually query.

    ``ISSUE_SOURCES`` are executed through ``query_pg`` — a PostgreSQL-only
    path by contract. A system of the right TYPE on a non-SQL connection
    (Athena / SiteWise / REST / MCP — observed live: an Athena-backed MES on a
    base install with no PostgreSQL at all) can NEVER be served by these
    sources; including it made every compose raise, which the route wrapped
    into a false "temporarily unavailable" (implying a retry could fix a
    structural mismatch). Skipping is the honest outcome: the capability does
    not apply, the panel stays hidden, and SQL-backed systems still report."""
    from .connection_types import NON_SQL_CONNECTION_TYPES

    return [
        s for s in systems
        if s.get("system_type") == system_type
        and s.get("connection_type") not in NON_SQL_CONNECTION_TYPES
    ]


def _spec_for(source: IssueSource) -> SectionSpec:
    """Build the ``SectionSpec`` (table + ordered columns) the SQL builder consumes."""
    return SectionSpec(
        table=source.table,
        entity=source.entity,
        entity_label=source.entity,
        columns=[{"column": c, "label": c, "field_concept": None} for c in source.columns],
    )


def _collect_source(source: IssueSource, system: dict, query_fn) -> list:
    """Run ONE source against ONE system → a list of issue items. Fail-loud: a bad mapping
    (missing classify field, non-dict result, etc.) raises so the route can degrade. The
    composed SQL is read-only, filtered via the closed predicate vocabulary, and LIMIT-ed."""
    schema = system.get("schema") or get_schema(system.get("id", ""))
    spec = _spec_for(source)
    sql = build_filtered_section_sql(
        schema, spec, source.predicates, max_rows=MAX_ISSUES_PER_SOURCE
    )
    raw = query_fn(schema, sql)
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    columns = parsed.get("columns") or [c["column"] for c in spec.columns]
    rows = parsed.get("rows") or []
    system_name = system.get("name") or system.get("id")
    issues = []
    for row in rows:
        row_dict = dict(zip(columns, row)) if isinstance(row, (list, tuple)) else dict(row)
        classified = source.classify(row_dict)
        issues.append({
            "severity": classified["severity"],
            "title": classified["title"],
            "detail": classified["detail"],
            "source_system": system_name,
            "entity": source.entity,
            "lineage": {"system_id": system.get("id"), "schema": schema,
                        "table": source.table, "sql": sql},
        })
    return issues


#: PostgreSQL SQLSTATEs that mean "this schema does not have the fixed source's
#: table/column" — a VOCABULARY mismatch, not an outage: 42P01 undefined_table,
#: 42703 undefined_column. Matched by the ``pgcode`` attribute pgdriver puts on
#: its exceptions (duck-typed so this module stays importable without a driver).
_VOCABULARY_MISMATCH_PGCODES = frozenset({"42P01", "42703"})


def _is_vocabulary_mismatch(exc: Exception) -> bool:
    return getattr(exc, "pgcode", None) in _VOCABULARY_MISMATCH_PGCODES


def compose_active_issues(systems=None, *, query_fn=query_pg) -> list:
    """Compose the cross-entity union of active-issue items across onboarded systems.

    Iterates the FIXED ``ISSUE_SOURCES``; for each, runs every onboarded system of the
    matching ``system_type`` through ``_collect_source`` and unions the results. Defaults to
    ``catalog.get_onboarded()``. Fail-LOUD (propagates) on any DB/mapping error — the route
    wraps the call in a friendly HTTP-200 envelope.

    ONE exception is skipped per (source, system) rather than propagated: a schema that
    lacks the source's fixed table or column (SQLSTATE 42P01/42703). The sources name the
    vocabulary of ONE seeded shape; a system of the right TYPE with a different vocabulary
    (live 2026-09-18: a plant MES with ``production_runs`` and a ``quality_checks`` table
    without ``pass_rate``, a plant ERP with ``orders`` instead of ``customer_orders``) can
    NEVER be served by them, and letting it raise took the whole panel down for every other
    system that could report (same honesty rule as the non-SQL skip in ``_systems_of_type``:
    the capability does not apply, so that pair contributes nothing). Outages and every other
    error still propagate."""
    systems = catalog.get_onboarded() if systems is None else systems
    issues: list = []
    for source in ISSUE_SOURCES:
        for system in _systems_of_type(systems, source.system_type):
            try:
                issues.extend(_collect_source(source, system, query_fn))
            except Exception as exc:
                if not _is_vocabulary_mismatch(exc):
                    raise
                logger.info(
                    "active-issues source %r does not apply to system %r (%s): %s",
                    source.key, system.get("id"), getattr(exc, "pgcode", ""), exc)
    return issues
