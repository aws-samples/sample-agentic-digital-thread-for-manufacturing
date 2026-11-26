# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic metric resolver (ADR 0015): the single ``resolve_metric`` path
(ADR 0001) that turns a ``Metric`` from the metric layer (ADR 0002 / ADR 0014)
into a value via the graph — callable identically by the dashboard (S1.3) and
agents-as-tools.

The resolution is an explicit two-phase split (Decision A): graph resolution
builds a serializable ``QueryPlan`` (the S2.2 lineage seam), then execution
turns the plan into SQL and a scalar value. Authority is sourced from the graph
behind the ``AuthorityResolver`` interface (Decision B, ADR 0002 / ADR 0008),
runtime-settable via ``set_authority``. Derived metrics and the ``latest``
aggregation are deferred (Decisions C/E) and fail loud. Direct-only, no silent
fallback (ADR-0011 F5).

This module is determinism-pure (ADR 0001): no LLM/bedrock import, no
``eval``/``exec``. SQL/identifier safety is reused from ``agents.db`` verbatim.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from . import athena_tools, connection_types, duckdb_tools, rederive, sitewise_read
from . import metric_binding as mb
from . import metric_measure_rule as mmr
from . import metric_predicate as mp
from .metric_execution_trace import active_trace
from .catalog import catalog, get_schema, CONCEPT_NAME
from .catalog import REMOVAL_STARTED_COLUMN, removed_ids
from .db import neptune_query, neptune_write, query_pg, validate_identifier  # noqa: F401 (neptune_write: the graph-write seam metric_authority uses)
from .self_learning import record_user_correction, record_user_proposal
from .metric_layer import (
    Aggregation,
    ConceptRef,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    Metric,
    MetricKind,
    MetricRef,
    Predicate,
    Scalar,
    get_metric_layer,
)
from .models import ResolveMetricRequest, ResolveMetricByDimensionRequest, ResolveHeatmapRequest, SetAuthorityRequest
from .query_router import query_system


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


# --- Types (frozen dataclasses) — the graph-resolution↔execution split ---

@dataclass(frozen=True)
class QueryPlan:
    system_id: str
    schema: str
    table: str
    column: str
    aggregation: Aggregation
    # a duration-PERCENTILE plan carries the bound measure (its two
    # timestamp columns pinned on this table) and the 1..5 percentiles instead
    # of aggregating ``column``. Both default None so a plain plan is
    # byte-identical.
    measure: "mmr.DurationMeasure | None" = None
    percentiles: "tuple[float, ...] | None" = None
    # the per-role ({start, end}) DATE→timestamp cast map for the
    # measure columns; empty/None for timestamp columns (byte-identical SQL).
    measure_casts: "dict | None" = None
    # a resolved absolute [start, end) UTC window applied to the
    # measure's END column (the turn's date window). None keeps the lifetime
    # or declared-token behaviour.
    absolute_window: "AbsoluteWindow | None" = None


@dataclass(frozen=True)
class AbsoluteWindow:
    """A resolved ``[start_utc, end_utc)`` turn window (ISO-8601 instants),
    applied to a duration measure's END column."""
    start_utc: str
    end_utc: str


@dataclass(frozen=True)
class AuthoritySource:
    """How the authoritative system was selected (real basis, no invented value)."""
    system_id: str
    source: str            # 'authority_rule' | 'single_system_map'


@dataclass(frozen=True)
class MappingProvenance:
    """The MAPS_TO edge a direct resolve traversed (today's depth, ADR 0017 E)."""
    table: str
    column: str
    role: str | None
    confidence: float | None
    source: str                                # r.method else 'agent_proposed'
    mapping_version: str | None = None         # S3.1 — read from MAPS_TO r.version (onboarded ⇒ "1")
    # Intentionally None — NOT a missing feature. ValueMappings (row-level value
    # translation, e.g. cross-system code/identity mapping) are consumed by the LLM
    # agent/orchestrator path, never by this deterministic resolver, which aggregates
    # columns and does not translate individual values. With no resolve-time value-
    # mapping read site, there is no version to stamp. Populate here only if/when the
    # resolver itself reads a ValueMapping at resolve time (would carry the VM version).
    value_mapping_version: str | None = None
    # ADR-0076 D1: WHY this column — ``single`` | ``authority_rule`` (the
    # metric's own concept); ``None`` where the rule does not select.
    binding_basis: str | None = None


@dataclass(frozen=True)
class InvariantViolation:
    """Evidence for a deterministic post-resolution sanity failure."""
    reason: str
    detail: str
    offending_value: float | int | None = None
    observed_denominator_concept: str | None = None


@dataclass(frozen=True)
class Lineage:
    """Ephemeral record stamped on the value in the pass that produced it (ADR 0017)."""
    metric_id: str | None
    kind: str                                  # 'direct' | 'derived'
    # --- direct group (None for derived) ---
    concept: str | None = None
    authority: AuthoritySource | None = None
    mapping: MappingProvenance | None = None
    query_plan: QueryPlan | None = None
    sql: str | None = None
    executed_at: str | None = None
    # --- derived group (defaults for direct) ---
    op: str | None = None                      # FormulaOp.value
    inputs: tuple["Lineage", ...] = ()
    prefer_stored_hit: bool = False
    # --- scalar window honesty (fix-pass, Task 2D parity) --------------------
    # Serialized ONLY when the metric DECLARES a window (``window_requested`` is
    # set) — ``_lineage_to_dict`` strips all three keys otherwise, so every
    # non-windowed scalar lineage payload stays byte-identical (golden pin).
    #   window_requested  the metric's declared WINDOW_VOCAB token
    #   window            the token actually applied (None ⇒ the HONEST lifetime
    #                     fallback — no event-time mapping / non-SQL backend)
    #   window_column     the mapped event-time column the WHERE targeted
    window_requested: str | None = None
    window: str | None = None
    window_column: str | None = None
    # --- declared metric sanity invariants ----------------------------------
    # These keys are stripped by ``_lineage_to_dict`` unless declared, exactly
    # like the scalar-window fields above. A violation preserves the measured
    # value only as evidence here; ``ResolvedMetric.value`` is withheld.
    valid_range: tuple[float, float] | None = None
    max_exclusive: bool = False
    denominator_provenance: str | None = None
    invariant_violation: InvariantViolation | None = None
    # --- ADR-0075 row predicates: the bound (concept, column, op, values) of a
    # filtered DIRECT metric. Stripped by ``_lineage_to_dict`` when ``None`` so
    # every filter-less lineage payload stays byte-identical (golden pin).
    predicates: tuple[mp.PredicateBinding, ...] | None = None
    # a duration-PERCENTILE metric's bound measure (unit, clamp, the two
    # pinned columns, and that NULL-timestamp rows are excluded) and the
    # declared percentiles. Both stripped by ``_lineage_to_dict`` when ``None``
    # so every non-measure payload stays byte-identical.
    measure: "mmr.DurationMeasure | None" = None
    percentiles: tuple[float, ...] | None = None
    # a duration-percentile computed on Athena uses approx_percentile
    # (an ESTIMATE); PostgreSQL percentile_disc is exact. Stamped by DIALECT,
    # independent of scan narrowing, so the renderer can say 'approximate' only
    # for Athena. None/False for every non-measure or PostgreSQL payload
    # (stripped when None, byte-identical).
    approximate: bool | None = None
    # --- how an Athena read stayed under the per-query scan limit
    # (``metric_athena_scope``): computed exactly from partition pieces, or
    # narrowed (and to what). ``None`` when it ran as defined in one query;
    # stripped by ``_lineage_to_dict`` then (byte-identical payloads).
    scan: dict | None = None


@dataclass(frozen=True)
class ResolvedMetric:
    metric_id: str
    value: float | int | None
    unit: str
    label: str
    query_plan: QueryPlan | None
    system_id: str
    lineage: Lineage | None = None
    # 2A honest-data seam: number of non-NULL measure rows behind the scalar (the
    # paired ``COUNT(col)`` of ``_compose_sql``; asset count on the SiteWise path).
    # ``None`` when the execution path cannot report one (defaults keep every
    # existing constructor call byte-identical).
    sample_count: int | None = None
    # Present only for a declared invariant failure. Existing successful and
    # no-data resolves retain their historical shape at serialization.
    status: str | None = None
    detail: str | None = None
    # a duration-PERCENTILE metric resolves to one (p, value) pair per
    # declared percentile (``value`` stays None, there is no single scalar).
    # ``None`` for every non-percentile metric (byte-identical).
    percentiles: "tuple[tuple[float, float | int | None], ...] | None" = None


class MetricResolverError(Exception):
    """Raised on a resolution boundary error; carries metric/concept context."""


class ExactValueNotInTime(MetricResolverError):
    """A chat turn's metric read is over the per-query scan limit and its exact
    computation from partition pieces did not finish inside the turn's time
    budget. Not a withhold: the system agent of ``system_id`` can compute the
    same value with the full split budget.

    With ``window`` (the read was bounded by an explicit turn window, so it is
    a duration metric that is never narrowed and that no split can compute),
    the engine refused the one windowed query: the system agent can compute
    it with one query inside ``window`` on ``table``, applying the metric's
    definition."""

    def __init__(self, system_id: str, lineage: str, *, table: str | None = None,
                 window: "AbsoluteWindow | None" = None):
        if window is None:
            message = (f"the value of {lineage} is over the per-query scan limit and its "
                       "exact computation from partition pieces did not finish in the "
                       "metric layer's time budget")
        else:
            message = (f"the value of {lineage} inside its stated window is over the "
                       "per-query scan limit; the metric layer did not compute it")
        super().__init__(message)
        self.system_id = system_id
        self.lineage = lineage
        self.table = table
        self.window = window


class CoverageGapError(MetricResolverError):
    """A resolution failure whose cause is a MAPPING GAP, not a fault: the
    concept simply is not mapped by any (or the authoritative) onboarded
    system yet. Routes classify this as ``status: "uncovered"`` — the honest
    needs-data presentation (finish onboarding), never error styling.
    Carries the missing concept id for UI guidance."""

    def __init__(self, message: str, *, concept: str | None = None):
        super().__init__(message)
        self.concept = concept


class UnresolvedPredicateError(CoverageGapError):
    """ADR-0075 D3: a filtered DIRECT metric's predicate concept has no field
    bound on the resolved system's MEASURE table. The scalar path turns it into
    the ``unresolved_predicate`` STATUS (value withheld, concept NAMED); the
    grouped path lets it surface as the existing ``CoverageGapError`` →
    ``uncovered + missing_concepts`` envelope. Never a proxy, never a guess."""


#: ADR-0075 D3 — the FOURTH honest-data status (distinct from ``no-data``,
#: ``unavailable`` and ``invariant_violation``). The ONE spelling; consumers
#: import it rather than re-typing the string.
UNRESOLVED_PREDICATE_STATUS = "unresolved_predicate"


class AmbiguousBindingError(CoverageGapError):
    """ADR-0076 D3: a concept bound to 2+ columns on one system with no steward
    choice (``AuthorityRule.field``). Scalar path → the ``ambiguous_binding``
    STATUS; grouped paths → the ``uncovered`` envelope. Candidates NAMED."""

    def __init__(self, message: str, *, concept: str, candidates=()):
        super().__init__(message, concept=concept)
        self.candidates = tuple(candidates)


#: ADR-0076 D3 — the FIFTH honest-data status (the ONE spelling lives in
#: ``metric_binding``; re-exported for consumers of this module).
AMBIGUOUS_BINDING_STATUS = mb.AMBIGUOUS_BINDING_STATUS
_MEASURE_BINDING_CYPHER = mb.MEASURE_BINDING_CYPHER
_PREDICATE_BINDING_CYPHER = mb.PREDICATE_BINDING_CYPHER
_BINDING_RULE_CYPHER = mb.BINDING_RULE_CYPHER


class UnresolvedInputError(MetricResolverError):
    """ADR-0080 D3: a derived definition's ``MetricRef`` names an id that is
    neither a served metric nor a pending definition supplied with a dry-run
    preview. Raised ONLY on the explicit-layer (preview) path; the resolver
    turns it into the ``unresolved_input`` STATUS that NAMES the id —
    withhold, never proxy."""

    def __init__(self, metric_id: str):
        super().__init__(
            f"input metric {metric_id!r} resolves to neither a served metric "
            "nor a pending definition supplied with the preview"
        )
        self.metric_id = metric_id


#: ADR-0080 D3 — the preview-only withhold status for a derived definition
#: whose input does not resolve. The ONE spelling; consumers import it.
UNRESOLVED_INPUT_STATUS = "unresolved_input"


class StructuralMetricError(MetricResolverError):
    """A resolution failure caused by the SOURCE SYSTEM'S DATA MODEL, not a
    transient fault and not a mapping gap: no retry and no remap of the same
    properties can ever succeed (observed live: a direct-source measure and
    dimension mapped on DIFFERENT asset models, where the engine has no
    cross-model join). Routes mark the error envelope ``structural: True`` so
    the UI can drop the Retry affordance — offering Retry for a constraint
    that cannot change misleads the operator into rerunning a dead end."""


class UnreadableSystemError(StructuralMetricError):
    """The authoritative system is on a connection type the metric layer has
    no reader for (``connection_types.structured_read_supported``: the REST
    vendor registry gives the type no ``structured_reader``). Its data is read
    only by its own agent, so the resolver composes and sends no query for it
    (:func:`require_metric_reader`, called before any SQL is composed). Before
    this check such a system's empty catalog schema or vendor table label
    failed identifier validation with a bare ``ValueError``, outside this
    error contract, and valid names sent the composed SQL to a source that
    cannot run it. Structural: no retry can succeed."""

    def __init__(self, system_id: str, connection_type: str):
        self.system_id = system_id
        self.connection_type = connection_type
        super().__init__(
            f"{unreadable_system_reason(system_id, connection_type)}; it computes "
            "no value from that system")


def unreadable_system_reason(system_id: str, connection_type: str) -> str:
    """The fixed reason an :class:`UnreadableSystemError` names: the system id
    and its connection type, nothing from a backend."""
    return (f"system {system_id!r} ({connection_type}) is on a connection type the "
            "metric layer has no reader for")


def require_metric_reader(system_id: str) -> None:
    """Raise :class:`UnreadableSystemError` when the metric layer has no
    reader for ``system_id``'s connection type. The REST vendor registry owns
    the answer (``connection_types.structured_read_supported``, the rule the
    dashboard and metric fan-outs already apply), so a new vendor needs no
    entry here. A system missing from the catalog keeps the PostgreSQL
    default, as :func:`_connection_type` does."""
    conn = _connection_type(system_id)
    if not connection_types.structured_read_supported({"connection_type": conn}):
        raise UnreadableSystemError(system_id, conn)


# --- Authority interface (ADR 0008 read-seam + graph-default, ADR 0002) ---

class AmbiguousAuthorityError(MetricResolverError):
    """2+ ACTIVE AuthorityRules for one concept name different systems
    (:class:`GraphAuthorityResolver`). Carries the concept and the sorted
    system ids so a caller can tell a per-site partition (one authority per
    site, ADR-0094) from a genuine conflict — the scalar resolve still fails
    loud either way."""

    def __init__(self, message: str, *, concept: str, system_ids=()):
        super().__init__(message)
        self.concept = concept
        self.system_ids = tuple(system_ids)


class AuthorityResolver(ABC):
    @abstractmethod
    def authoritative_system(self, concept: str) -> str | None: ...


#: The tail of an authority read: each row's system id (``{sid}``, an
#: expression over the matched rule or field) and whether that system's
#: removal has started (``catalog.REMOVAL_STARTED_COLUMN``, read through
#: ``catalog.removed_ids`` with this process's catalog). A rule or field of
#: a system being removed, or one whose removal did not finish, is kept in the
#: graph for audit but never selected (:func:`_served_system_ids`): selecting
#: it would name a system that is going away, and counting it next to a new
#: same-site system's rule would make that concept ambiguous.
_SYSTEM_MARKER_JOIN = ("OPTIONAL MATCH (s:System) WHERE s.id = {sid} "
                       "RETURN DISTINCT {sid} AS sid, " + REMOVAL_STARTED_COLUMN)


def _served_system_ids(rows) -> list:
    """The sorted distinct system ids of an authority read, without the
    systems being removed (``catalog.removed_ids``)."""
    removing = removed_ids(rows, "sid", catalog)
    return sorted({str(r.get("sid")) for r in (rows or [])
                   if r.get("sid") and str(r.get("sid")) not in removing})


class GraphAuthorityResolver(AuthorityResolver):
    """Reads the ``AuthorityRule.concept`` PROPERTY (canonical per ADR-0011 F3).

    Soft-deactivated rules (``status='deactivated'`` — T1.4 lifecycle,
    ADR-0050 addendum) never participate. ``set_authority`` encodes the system
    in the rule id, so a competing rule for the same concept creates a SECOND
    node: multiple ACTIVE rules naming DIFFERENT systems used to resolve as
    ``rows[0]`` of an unordered MATCH — a per-query coin flip. That state now
    FAILS LOUD (``MetricResolverError``, consistent with
    ``_resolve_authority``'s ambiguous-⇒-fail-loud posture); duplicates naming
    the SAME system dedupe deterministically.
    """

    def authoritative_system(self, concept: str) -> str | None:
        rows = neptune_query(
            "MATCH (ar:AuthorityRule {concept: $concept}) "
            "WHERE coalesce(ar.status, '') <> 'deactivated' "
            + _SYSTEM_MARKER_JOIN.format(sid="ar.system_id"),
            {"concept": concept},
        )
        sids = _served_system_ids(rows)
        if len(sids) > 1:
            raise AmbiguousAuthorityError(
                f"ambiguous authority for concept {concept!r}: {len(sids)} "
                f"active AuthorityRules name different systems ({sids}); "
                "deactivate the wrong rule via "
                "POST /api/thread/rules/{rule_id}/deactivate "
                "or call set_authority(concept, system_id)",
                concept=concept, system_ids=sids,
            )
        return sids[0] if sids else None


class PinnedAuthority(AuthorityResolver):
    """Force ONE system to be authoritative for ANY concept (the 2A pin seam).

    Generic — carries no concept/system literals; the caller supplies the system
    id. Canonical home of this shape for ``dashboard`` / ``metric_multisite``;
    used here by the by-dimension route's optional ``system_id`` pin (per-site
    bars / the 2C drill)."""

    def __init__(self, system_id: str) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:  # noqa: ARG002
        return self._system_id


@lru_cache(maxsize=1)
def get_authority_resolver() -> AuthorityResolver:
    return GraphAuthorityResolver()


# ``set_authority`` — the ONE steward writer of AuthorityRule (ADR 0008 seam;
# ADR-0076 D2 adds the steward-chosen binding ``field``) — lives in
# ``agents/metric_authority.py`` and is re-exported here for existing callers.
from .metric_authority import set_authority, set_authority_payload  # noqa: E402,F401


# --- Resolution helpers ---

def _utcnow_iso() -> str:
    """Clock seam (monkeypatchable in tests) — ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_authority(concept: str, authority: AuthorityResolver | None) -> AuthoritySource:
    """Return the authoritative system for a concept + its selection basis (fail loud).

    Precedence is per-concept: an ``AuthorityRule`` node wins; else a single mapping
    system; else fail loud. A system's ``system_type`` label is NEVER consulted here
    (it is descriptive UI/affinity metadata only — see ``set_authority`` above).
    """
    resolver = authority or get_authority_resolver()
    system_id = resolver.authoritative_system(concept)
    if system_id:
        return AuthoritySource(system_id=system_id, source="authority_rule")
    rows = neptune_query(
        "MATCH (f:Field)-[:MAPS_TO]->(c:Concept {id: $concept}) "
        + _SYSTEM_MARKER_JOIN.format(sid="f.system_id"),
        {"concept": concept},
    )
    ids = _served_system_ids(rows)
    if len(ids) == 1:
        return AuthoritySource(system_id=ids[0], source="single_system_map")
    if not ids:
        # Nothing maps the concept at all — a pure coverage gap (onboarding
        # incomplete), not a configuration fault.
        raise CoverageGapError(
            f"no onboarded system maps concept {concept!r} yet",
            concept=concept,
        )
    raise MetricResolverError(
        f"no AuthorityRule for concept {concept!r} and {len(ids)} systems map it "
        f"({ids}); call set_authority(concept, system_id)"
    )


def _mapping_from_row(row: dict, binding_basis: str | None = None) -> MappingProvenance:
    """Map a MAPS_TO traversal row → MappingProvenance (fail loud on table/column).

    The TABLE keeps its schema-tail normalisation; the COLUMN is EXACT as
    stored — a dotted physical column is ONE column (ADR-0076 D4)."""
    table = (row.get("table") or "").split(".")[-1]
    column = row.get("column") or ""
    if not table or not column:
        raise MetricResolverError(
            f"incomplete MAPS_TO mapping: table={table!r} column={column!r}"
        )
    return MappingProvenance(
        table=table,
        column=column,
        role=row.get("role"),
        confidence=row.get("confidence"),
        source=row.get("source") or "agent_proposed",
        mapping_version=row.get("version"),    # S3.1 Task 2.2 — additive; absent/None → None (back-compat)
        binding_basis=binding_basis,
    )


def _select_binding(system_id: str, concept: str, cypher: str, params: dict):
    """ADR-0076 D1 through the resolver's graph seam: ``None`` (unbound), a
    :class:`mb.BindingChoice`, or a :class:`mb.BindingAmbiguity`. The rule
    read runs ONLY when 2+ candidates exist."""
    candidates = mb.candidates_from_rows(neptune_query(cypher, params) or [])
    if not candidates:
        return None
    return mb.select_binding(
        concept, system_id, candidates,
        lambda: neptune_query(
            _BINDING_RULE_CYPHER, {"sid": system_id, "concept": concept}) or [],
    )


def _ambiguous(outcome: mb.BindingAmbiguity, prefix: str = "") -> AmbiguousBindingError:
    return AmbiguousBindingError(f"{prefix}{outcome.detail}", concept=outcome.concept,
                                 candidates=outcome.candidates)


def _resolve_table_column(system_id: str, concept: str) -> MappingProvenance:
    """The concept's ONE column on ``system_id`` (ADR-0076 D1) — unbound is a
    coverage gap, 2+ without a steward choice an :class:`AmbiguousBindingError`."""
    params = {"sid": system_id, "concept": concept}
    outcome = _select_binding(system_id, concept, _MEASURE_BINDING_CYPHER, params)
    if outcome is None:
        raise CoverageGapError(
            f"no MAPS_TO field for concept {concept!r} in system {system_id!r}",
            concept=concept,
        )
    if isinstance(outcome, mb.BindingAmbiguity):
        raise _ambiguous(outcome)
    try:
        return _mapping_from_row(dataclasses.asdict(outcome.candidate), outcome.basis)
    except MetricResolverError as e:
        raise MetricResolverError(
            f"incomplete MAPS_TO mapping for concept {concept!r} in system "
            f"{system_id!r}: {e}"
        ) from e


#: ADR-0076 extension: the honest selection basis of a duration-measure metric
#: whose OWN concept is unrecorded (no AuthorityRule, no MAPS_TO anywhere) and
#: whose (system, table) was located by the two pinned measure columns instead.
#: Carried on both ``AuthoritySource.source`` and the measure-concept
#: ``MappingProvenance.binding_basis`` so lineage says truthfully that the
#: table came from the pins, not from a (non-existent) concept column.
BASIS_MEASURE_PINS = "measure_pins"

#: The (system, table) pairs where a field named ``$start_column`` MAPS_TO
#: ``$start_concept`` AND a field named ``$end_column`` MAPS_TO ``$end_concept``
#: on the SAME SchemaEntry of the SAME System. Both legs are matched by EXACT
#: field name (the declared pin column) and exact concept, so a table only
#: qualifies when it carries BOTH pins. Returns DISTINCT pairs, deterministic
#: by (system_id, table); the caller fails loud on 2+ and gaps on 0 (never a
#: row-order pick).
_MEASURE_PIN_CYPHER = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)"
    "-[:HAS_FIELD]->(fs:Field {name: $start_column})"
    "-[:MAPS_TO]->(:Concept {id: $start_concept}) "
    "MATCH (se)-[:HAS_FIELD]->(fe:Field {name: $end_column})"
    "-[:MAPS_TO]->(:Concept {id: $end_concept}) "
    "RETURN DISTINCT s.id AS system_id, se.name AS table, "
    + REMOVAL_STARTED_COLUMN +
    " ORDER BY system_id, table"
)


def _measure_pin_pairs(measure: "mmr.DurationMeasure",
                       system_id: str | None = None) -> list[tuple[str, str]]:
    """The DISTINCT ``(system_id, table)`` pairs where BOTH pinned measure
    columns bind on the SAME table (``_MEASURE_PIN_CYPHER``). Scoped to one
    system when ``system_id`` is given (the authority-known case), else the
    whole estate. A system whose removal has started is excluded (the same
    served/removal rule as ``_served_system_ids``): a system going away must
    never become the source, nor create a false multi-pair ambiguity. The
    SchemaEntry name tail is normalised to match ``_mapping_from_row``.
    Deterministic by name; no row-order pick."""
    rows = neptune_query(_MEASURE_PIN_CYPHER, {
        "start_concept": measure.start.concept, "start_column": measure.start.column,
        "end_concept": measure.end.concept, "end_column": measure.end.column,
    }) or []
    removing = removed_ids(rows, "system_id", catalog)
    pairs = sorted({
        (str(r.get("system_id")), (r.get("table") or "").split(".")[-1])
        for r in rows
        if r.get("system_id") and r.get("table")
        and str(r.get("system_id")) not in removing
        and (system_id is None or str(r.get("system_id")) == system_id)
    })
    return pairs


def _measure_pin_fail(measure: "mmr.DurationMeasure") -> str:
    """The ``start:col, end:col`` fragment naming the two pins (error detail)."""
    return (f"{measure.start.concept!r}:{measure.start.column!r}, "
            f"{measure.end.concept!r}:{measure.end.column!r}")


def _one_pin_pair(pairs: list, measure: "mmr.DurationMeasure", concept: str,
                  scope: str, named) -> tuple[str, str]:
    """Exactly one ``(system, table)`` pin pair, or fail loud: zero ⇒ coverage
    gap; 2+ ⇒ a steward-resolved ambiguity naming the candidates. ``named``
    renders each candidate for the 2+ message; ``scope`` labels the search."""
    if not pairs:
        raise CoverageGapError(
            f"no {scope} binds both measure pins ({_measure_pin_fail(measure)}) "
            f"for concept {concept!r}", concept=concept) from None
    if len(pairs) > 1:
        raise MetricResolverError(
            f"concept {concept!r} is unbound and its measure pins bind on more "
            f"than one {scope} ({', '.join(named(p) for p in pairs)}); the "
            f"concept has no MAPS_TO field, so a steward must set system "
            f"authority via POST /api/metrics/set-authority with concept and "
            f"system_id and no field"
        ) from None
    return pairs[0]


def _measure_pin_mapping(table: str) -> MappingProvenance:
    """The metric concept's HONEST mapping when the table came from the pins:
    the table, NO concept column, basis ``measure_pins``."""
    return MappingProvenance(
        table=table, column="", role=None, confidence=None,
        source=BASIS_MEASURE_PINS, binding_basis=BASIS_MEASURE_PINS)


def _resolve_measure_target(metric: DirectMetric,
                            authority: AuthorityResolver | None
                            ) -> tuple[AuthoritySource, MappingProvenance]:
    """The ONE (authority, measure-table) resolution shared by the scalar and
    grouped measure paths.

    Precedence unchanged first (AuthorityRule, else single mapping system);
    when the concept is bound there the behaviour is byte-identical. NEW, only
    for a duration-``measure`` metric: a concept that maps NOWHERE locates the
    ``(system, table)`` by its two pinned columns across the estate, and a
    concept KNOWN on the authority system but not bound there takes the one
    table on that system where both pins bind. Zero pins ⇒ coverage gap; 2+ ⇒
    fail loud. A non-measure metric is unchanged (unbound ⇒ coverage gap)."""
    measure = metric.measure
    try:
        auth = _resolve_authority(metric.concept, authority)
    except CoverageGapError as gap:
        # a PLAIN gap (concept maps nowhere); a subclass is a different gap.
        if measure is None or type(gap) is not CoverageGapError:
            raise
        system_id, table = _one_pin_pair(
            _measure_pin_pairs(measure), measure, metric.concept,
            "onboarded (system, table)", lambda p: f"{p[0]}.{p[1]}")
        return (AuthoritySource(system_id=system_id, source=BASIS_MEASURE_PINS),
                _measure_pin_mapping(table))
    try:
        return auth, _resolve_table_column(auth.system_id, metric.concept)
    except CoverageGapError as gap:
        # only a PLAIN gap falls through to the pins; ambiguous / unresolved
        # predicate keep their loud withhold (both subclass CoverageGapError).
        if measure is None or type(gap) is not CoverageGapError:
            raise
        _sid, table = _one_pin_pair(
            _measure_pin_pairs(measure, system_id=auth.system_id), measure,
            metric.concept, f"table of system {auth.system_id!r}",
            lambda p: repr(p[1]))
        return auth, _measure_pin_mapping(table)


#: Withhold-path detail ONLY (never selects a column): one other table the
#: concept is bound on, deterministic by name.
_PREDICATE_ELSEWHERE_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)"
    "-[:HAS_FIELD]->(f:Field)-[:MAPS_TO]->(c:Concept {id: $concept}) "
    "WHERE se.name <> $table "
    "RETURN DISTINCT se.name AS table ORDER BY table LIMIT 1"
)


def _unbound_predicate_detail(params: dict) -> str:
    concept, system_id, table = params["concept"], params["sid"], params["table"]
    rows = neptune_query(_PREDICATE_ELSEWHERE_CYPHER, params) or []
    elsewhere = [r.get("table") for r in rows if r.get("table")]
    if elsewhere:
        return (
            f"predicate concept {concept!r} is bound on table {elsewhere[0]!r}, "
            f"not the measure table {table!r}, on system {system_id!r}"
        )
    return (
        f"predicate concept {concept!r} has no field bound on system "
        f"{system_id!r} table {table!r}"
    )


def _bound_column(pred: Predicate, system_id: str, column: str) -> str:
    """The bound field EXACTLY as stored, fail-closed on a non-column name
    (a dotted physical column passes — every segment validated, ADR-0076 D4)."""
    if not column:
        raise MetricResolverError(
            f"incomplete MAPS_TO mapping for predicate concept "
            f"{pred.concept!r} in system {system_id!r}"
        )
    try:
        return mb.validate_column_name(column)
    except ValueError as e:
        raise MetricResolverError(
            f"invalid metric predicate: concept {pred.concept!r} is bound to "
            f"field {column!r} on system {system_id!r}, not a SQL identifier"
        ) from e


def _bind_predicate(system_id: str, table: str, pred: Predicate) -> mp.PredicateBinding:
    """Bind ONE predicate concept to EXACTLY one field on the measure table.

    Zero bindings WITHHOLD via ``UnresolvedPredicateError`` (ADR-0075 D3);
    2+ select through the ONE rule (ADR-0076 D1) or WITHHOLD via
    ``AmbiguousBindingError`` — graph row order never decides the quantity."""
    params = {"sid": system_id, "table": table, "concept": pred.concept}
    pinned = getattr(pred, "column", None)
    if pinned is not None:  # an exact column pin, verified fail-closed
        column = _bind_field_ref(system_id, table, f"filter[{pred.concept}]",
                                 mmr.FieldRef(pred.concept, pinned))
        return mp.PredicateBinding(pred.concept, column, pred.op, pred.values)
    outcome = _select_binding(system_id, pred.concept, _PREDICATE_BINDING_CYPHER, params)
    if outcome is None:
        raise UnresolvedPredicateError(
            _unbound_predicate_detail(params), concept=pred.concept)
    if isinstance(outcome, mb.BindingAmbiguity):
        raise _ambiguous(outcome, prefix="predicate ")
    column = _bound_column(pred, system_id, outcome.candidate.column)
    return mp.PredicateBinding(pred.concept, column, pred.op, pred.values)


def _resolve_predicate_bindings(
    system_id: str, table: str, predicates: tuple[Predicate, ...]
) -> tuple[mp.PredicateBinding, ...]:
    """Bind every predicate concept on the measure table through the system's
    ``MAPS_TO`` (ADR-0075 D2); the first unresolved concept is NAMED."""
    return tuple(_bind_predicate(system_id, table, pred) for pred in predicates)


def _and_where(sql: str, predicate: str) -> str:
    """Append ``predicate`` to a composed SELECT: `` WHERE `` if none yet, else
    `` AND `` (the ONE joiner for prune / window / predicate composition)."""
    joiner = " AND " if " WHERE " in sql else " WHERE "
    return f"{sql}{joiner}{predicate}"


def _predicate_bindings_for(
    system_id: str, table: str, predicates: tuple[Predicate, ...] | None
) -> tuple[mp.PredicateBinding, ...] | None:
    """``None`` for a filter-less metric; else the bound clauses. A non-SQL
    backend is STRUCTURAL: dropping a predicate would be a proxy (ADR-0075 D4)."""
    if not predicates:
        return None
    conn = _connection_type(system_id)
    if conn not in connection_types.METRIC_SQL_CONNECTION_TYPES:
        raise StructuralMetricError(
            "metric predicates are supported on SQL backends only; system "
            f"{system_id!r} is {conn!r}"
        )
    return _resolve_predicate_bindings(system_id, table, predicates)


def _apply_predicates(sql: str, bindings: tuple[mp.PredicateBinding, ...]) -> str:
    """AND the rendered predicate onto ``sql``; any render failure (hostile
    literal, invalid identifier) fails the resolve BEFORE execution."""
    try:
        fragment = mp.render_predicate_sql(bindings)
    except ValueError as e:
        raise MetricResolverError(f"invalid metric predicate: {e}") from e
    return _and_where(sql, fragment)


def _measure_dialect(system_id: str) -> str:
    """The duration/percentile SQL dialect for a system's connection type."""
    conn = _connection_type(system_id)
    if conn == connection_types.ATHENA:
        return "athena"
    return "duckdb" if conn == connection_types.S3TABLES_DUCKDB else "postgres"


def table_qualifier(system_id: str, schema: str, *, quoted: bool = False) -> str:
    """The qualifier a composed table reference puts before the table name:
    ``schema`` on PostgreSQL / Athena, ``s3t.<namespace>`` on an S3 Tables
    system (the connector's attached catalog is the first name part).
    ``quoted`` double-quotes each part, for the grouped / heatmap shapes."""
    parts = [schema]
    if _connection_type(system_id) == connection_types.S3TABLES_DUCKDB:
        parts.insert(0, duckdb_tools.CATALOG)
    return ".".join(f'"{p}"' if quoted else p for p in parts)


def _compose_measure_sql(plan: QueryPlan) -> str:
    """The duration-PERCENTILE SELECT: each declared percentile of the
    clamped, unit-scaled duration between the two pinned columns, PAIRED with
    ``COUNT`` of the same expression (the sample count), with the NULL-timestamp
    rows excluded in the WHERE. Athena ``approx_percentile`` / PostgreSQL
    ``percentile_disc``; identifiers validated, percentiles from a closed 1..5
    list. The plan's ``column`` is unused for the aggregate (the measure pins
    its own two columns)."""
    dialect = _measure_dialect(plan.system_id)
    schema = validate_identifier(plan.schema)
    table = validate_identifier(plan.table)
    qualifier = table_qualifier(plan.system_id, schema)
    expr = mmr.render_duration_expr(plan.measure, dialect=dialect,
                                    casts=plan.measure_casts)
    pct = mmr.render_percentile_select(expr, plan.percentiles, dialect=dialect)
    where = mmr.null_exclusion_where(plan.measure)
    return (
        f"SELECT {pct}, COUNT({expr}) FROM {qualifier}.{table} WHERE {where}"  # nosec B608 - schema and table pass validate_identifier() (the qualifier adds only the fixed DuckDB catalog name); columns are quoted; percentiles are 1..5 finite floats between 0 and 1
    )


def _compose_sql(plan: QueryPlan) -> str:
    """Compose a read-only aggregate SELECT. Identifiers validated; latest deferred.

    2A honest-data seam: the aggregate is PAIRED with ``COUNT(column)`` in the
    SAME query (one scan, generic — no extra round trip), so callers can tell a
    real value apart from a NULL/0-row degenerate aggregate (``sample_count`` on
    :class:`ResolvedMetric`). A plan carrying a duration ``measure`` emits
    the percentile shape instead."""
    if plan.measure is not None:
        return _compose_measure_sql(plan)
    if plan.aggregation is Aggregation.LATEST:
        raise NotImplementedError(
            "'latest' aggregation needs a time/ordering dimension — deferred"
        )
    agg = plan.aggregation.value.upper()
    schema = validate_identifier(plan.schema)
    table = validate_identifier(plan.table)
    qualifier = table_qualifier(plan.system_id, schema)
    column = mb.render_column_identifier(plan.column)  # flat → bare; dotted → ONE "a.b"
    return f"SELECT {agg}({column}), COUNT({column}) FROM {qualifier}.{table}"  # nosec B608 - identifiers validated (validate_identifier / validate_column_name)


def _coerce_scalar(value) -> float | int | None:
    """Coerce a SQL scalar to float/int, preserving None."""
    if value is None:
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value)
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except (TypeError, ValueError):
        raise MetricResolverError(f"non-numeric metric value: {value!r}")


def _coerce_count(value) -> int | None:
    """Best-effort int coercion of the paired ``COUNT(col)`` column (2A).

    The sample count is AUXILIARY — it must never fail a resolve that produced a
    usable aggregate, so anything non-numeric/unusable coerces to ``None``."""
    try:
        n = _coerce_scalar(value)
    except MetricResolverError:
        return None
    if n is None or isinstance(n, bool):
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _note_execution(system_id: str, scanned_bytes: object = None) -> None:
    """Count one value execution on the active preview trace (ADR-0080 D2);
    a no-op on the live path (no trace active)."""
    trace = active_trace()
    if trace is not None:
        trace.record(system_id, scanned_bytes)


def _preview_athena_runner(trace, system_id: str):
    """The Athena runner for a traced (preview) resolve: the SAME bounded
    connector (exact past the scan limit), carrying the ``:preview``
    ``source_system`` attribution."""
    from . import metric_athena_scope as mas
    return mas.exact_runner(trace.attribution(system_id))


def _athena_essential_columns(plan, *extra: str) -> tuple[str, ...]:
    """The columns an Athena read cannot drop on a Lake Formation denial. A
    plan whose metric concept is bound keeps its aggregate column (``column``
    for a scalar plan, ``measure_column`` for a grouped plan), exactly as
    before. Only a duration-measure plan whose table came from the measure pins
    (empty concept column) reads its two pinned columns instead. ``extra``
    appends further essential columns (the grouped dimension). Empty names are
    dropped so an empty concept column is never passed as a column."""
    own = getattr(plan, "column", None) or getattr(plan, "measure_column", "")
    if not own and getattr(plan, "measure", None) is not None:
        cols = (plan.measure.start.column, plan.measure.end.column, *extra)
    else:
        cols = (own, *extra)
    return tuple(c for c in cols if c)


def _execute_athena(plan: QueryPlan, sql: str) -> dict:
    """Run the composed aggregate through the shared Athena connector.

    DRY: the composed aggregate SELECT is valid Athena SQL — run it through
    the SAME connector Mode A/B's run_query uses (no new boto3/Athena code).
    connection_config carries the workgroup/database/catalog target AND, for a
    cross-account system, the STS role_arn/external_id (resolved from the
    agent-config secret, not just the non-secret catalog snapshot). The shared
    denied-column seam turns a Lake Formation column-level denial of the
    MEASURE column (nothing to drop in a scalar aggregate) into a clear
    MetricResolverError instead of the cryptic COLUMN_NOT_FOUND. Only while a
    preview trace is active is the runner swapped for the attributed one — the
    live call is byte-identical (no ``run`` keyword at all).
    """
    from . import metric_athena_scope as mas
    connection_config = _full_connection_config(plan.system_id)
    trace = active_trace()
    extra = ({"run": _preview_athena_runner(trace, plan.system_id)}
             if trace is not None else {})
    # A duration-measure plan whose table came from the measure pins has an
    # empty ``plan.column`` and reads its TWO pinned columns; a bound concept
    # keeps ``plan.column``. A Lake Formation denial of either fails loud.
    essential = _athena_essential_columns(plan)
    try:
        with mas.read_scope(plan.system_id, plan_lineage(plan)):
            outcome = athena_tools.run_athena_select_with_column_retry(
                lambda _cols: sql, list(essential), connection_config,
                essential=essential, **extra,
            )
    except athena_tools.AthenaColumnDeniedError as e:
        raise MetricResolverError(str(e)) from e
    result = outcome["result"]
    if trace is not None:
        trace.record(plan.system_id, result.get("scanned_bytes"))
    from . import metric_athena_scope as mas
    mas.record(result)
    return result


def _execute_s3tables(plan: QueryPlan, sql: str) -> dict:
    """Run the composed aggregate through the S3 Tables connector.

    The connector applies its own read-only guards (authorization, select-only,
    row cap, the system's table scope). It gets the system's FULL connection
    config (:func:`_full_connection_config`), so a cross-account bucket reads
    through its ``role_arn`` / ``external_id`` / ``catalog_endpoint`` and never
    falls back to the app's own credentials. Unlike Athena there is no scan
    cap to split around, so the aggregate runs as defined."""
    try:
        result = duckdb_tools.run_select(
            _full_connection_config(plan.system_id), sql, plan.system_id)
    except (duckdb_tools.DuckDBConfigError, duckdb_tools.QueryError) as e:
        raise MetricResolverError(str(e)) from e
    _note_execution(plan.system_id)
    return result


def plan_lineage(plan) -> str:
    """``system.table.column (aggregation)`` for a scalar or grouped plan. A
    duration-measure plan whose concept column is empty (the table came from
    the measure pins) names its two pinned columns instead of an empty slot, so
    the lineage string stays honest and readable."""
    table = getattr(plan, "table", None) or getattr(plan, "measure_table", "")
    column = getattr(plan, "column", None) or getattr(plan, "measure_column", "")
    aggregation = getattr(plan.aggregation, "value", plan.aggregation)
    measure = getattr(plan, "measure", None)
    if not column and measure is not None:
        column = f"duration({measure.start.column}, {measure.end.column})"
    return f"{plan.system_id}.{table}.{column} ({aggregation})"


def _execute_row(plan: QueryPlan, sql: str) -> list:
    """Execute the plan's SQL via the system's connection_type; return the first
    result ROW (uncoerced). The 2A seam: the scalar compose now pairs the
    aggregate with ``COUNT(col)``, so callers need both cells of the row."""
    entry = catalog.get(plan.system_id)
    conn_type = (
        entry.get("connection_type", connection_types.POSTGRESQL)
        if entry else connection_types.POSTGRESQL
    )
    if conn_type == connection_types.POSTGRESQL:
        parsed = json.loads(query_pg(plan.schema, sql))
        _note_execution(plan.system_id)
    elif conn_type == connection_types.ATHENA:
        parsed = _execute_athena(plan, sql)
    elif conn_type == connection_types.S3TABLES_DUCKDB:
        parsed = _execute_s3tables(plan, sql)
    else:
        parsed = query_system(plan.system_id, sql)
        _note_execution(plan.system_id)
    rows = parsed.get("rows") or []
    if not rows or not rows[0]:
        raise MetricResolverError(
            f"empty result executing plan for system {plan.system_id!r}: {sql}"
        )
    return rows[0]


def _execute(plan: QueryPlan, sql: str) -> float | int | None:
    """Execute the plan's SQL via the system's connection_type; return a scalar.
    Back-compat shim over :func:`_execute_row` (first cell only)."""
    return _coerce_scalar(_execute_row(plan, sql)[0])


def _connection_type(system_id: str) -> str:
    """The system's connection_type (default PostgreSQL) — the execution-branch key."""
    entry = catalog.get(system_id)
    return (
        entry.get("connection_type", connection_types.POSTGRESQL)
        if entry else connection_types.POSTGRESQL
    )


def _full_connection_config(system_id: str) -> dict:
    """Return the COMPLETE connection_config for a system — cross-account safe.

    In the APP process the catalog entry carries only the NON-SECRET connection
    snapshot: it lacks the cross-account STS ``external_id`` (a secret) and may
    lack ``role_arn``. A boto3 client built from that snapshot would silently fall
    back to AMBIENT (deploy-account) credentials and query the WRONG account —
    e.g. listing the deploy account's SiteWise asset models instead of the
    cross-account pilot account's. Mirror the query-runtime resolution
    (``runtime_app`` ~L46-59): when the snapshot is missing cross-account
    credentials (no ``role_arn`` or no ``external_id``) AND the catalog entry
    carries a ``secret_arn``, merge in the referenced Secrets Manager secret via
    the SAME ``_fetch_credentials`` helper the query runtime uses — secret fields
    win over the non-secret snapshot.

    Best-effort on the secret read (a fetch failure leaves the snapshot as-is and
    is logged); the caller still fails loud downstream if the merged config is
    unusable (e.g. Athena missing ``workgroup``, or the wrong-account SiteWise
    lookup raising ``MetricResolverError``).
    """
    entry = catalog.get(system_id) or {}
    config = dict(entry.get("connection_config") or {})
    if "role_arn" not in config or "external_id" not in config:
        # The app catalog snapshot omits secret fields (and often the whole
        # connection_config + secret_arn). The per-system connection config —
        # including the cross-account STS role_arn/external_id — is stored under
        # the DETERMINISTIC secret name the provisioner/runtime use
        # (``adt/agent-config/<system_id>``); prefer an explicit
        # catalog ``secret_arn`` when present, else fall back to that convention
        # (generic — derived from system_id, no dataset literals).
        secret_id = entry.get("secret_arn") or f"adt/agent-config/{system_id}"
        # Reuse the query-runtime's Secrets Manager reader (no new SM code).
        from .system_agent import _fetch_credentials
        try:
            secret = _fetch_credentials(secret_id) or {}
        except Exception:  # nosec B110 - best-effort; caller fails loud downstream
            logger.warning(
                "metric_resolver: secret fetch failed for system %r — using "
                "non-secret snapshot", system_id, exc_info=True,
            )
            secret = {}
        if isinstance(secret, dict):
            config.update(secret)  # secret fields win over the non-secret snapshot
    return config


def _athena_recent_partition_where(system_id: str, table: str, *, qualifier: str = "") -> str | None:
    """Best-effort partition-pruning predicate for one Athena table, or None.

    SHARED by the scalar (:func:`resolve_metric`) and grouped
    (``metric_dimension.resolve_metric_by_dimension``) Athena paths: an unpruned
    full-table aggregate over a large partitioned table trips the workgroup's
    bytes-scanned limit, so the composed SQL is restricted to the table's
    :data:`athena_tools.RECENT_PARTITIONS_LIMIT` most recent partitions when it
    has partition columns. Strictly best-effort (never raises): an unpartitioned
    table, a discovery failure, or an unusable connection config all yield None
    => the query runs exactly as before. ``qualifier`` prefixes column references
    (``"m."`` for the grouped joined shape's measure alias).
    """
    try:
        config = _full_connection_config(system_id)
        return athena_tools.build_recent_partition_predicate(
            table, config, qualifier=qualifier
        )
    except Exception:  # noqa: BLE001 - pruning must never break the query path
        logger.debug(
            "metric_resolver: partition-prune discovery failed for %r.%r — "
            "running unpruned", system_id, table, exc_info=True,
        )
        return None


def _athena_time_window_where(system_id: str, table: str, *, qualifier: str = "") -> str | None:
    """Second-tier Athena scan bound (best-effort, never raises): when partition
    pruning yields NO predicate (unpartitioned table / discovery failure), bound
    the scan with a time-window WHERE on the table's mapped event-time column —
    the SAME graph read ``metric_dimension._resolve_order_column`` uses —
    restricted to the last :data:`metric_dimension.ATHENA_TIME_WINDOW_DAYS` days.
    ``None`` when no time column is mapped either (unchanged behavior — the
    honest limit error). Lazy delegate to :mod:`agents.metric_dimension` (which
    imports this module at load; the deferral keeps the module graph acyclic)."""
    try:
        from . import metric_dimension as md
        return md._athena_time_window_where(system_id, table, qualifier=qualifier)
    except Exception:  # noqa: BLE001 - the fallback bound must never break the query path
        logger.debug(
            "metric_resolver: time-window fallback resolution failed for %r.%r",
            system_id, table, exc_info=True,
        )
        return None


def _match_by_name(items, name: str):
    """Case-insensitive exact-name match over discovery dicts (``{'name', ...}``)."""
    target = (name or "").strip().lower()
    for item in items:
        if (item.get("name") or "").strip().lower() == target:
            return item
    return None


def _model_has_columns(model: dict, columns: list[str]) -> bool:
    """True when the model carries EVERY required property (same case-insensitive
    exact-name semantics as :func:`_match_by_name`, and the property has an id)."""
    props = model.get("properties") or []
    for col in columns:
        prop = _match_by_name(props, col)
        if not prop or not prop.get("id"):
            return False
    return True


def _describe_model_candidates(models: list[dict]) -> str:
    """Render candidate models as ``'Name' (id=..., N properties)`` for errors."""
    return ", ".join(
        f"{(m.get('name') or '?')!r} (id={m.get('id') or '?'}, "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
        f"{len(m.get('properties') or [])} properties)"
        for m in models
    )


def _select_sitewise_model(
    models: list[dict], table: str, columns: list[str], *, system_id: str
) -> dict:
    """Select the SiteWise asset model for a mapped ``table`` name, disambiguating
    duplicate-named models (real estates carry renamed/duplicate hierarchy shells
    that shadow the propertied model under a plain first-name-match).

    Selection order (fail-loud disambiguation, NEVER a fabricating fallback):

    1. A model whose name matches ``table`` (the :func:`_match_by_name`
       case-insensitive exact semantics, first match wins) AND that carries every
       required property in ``columns`` — byte-identical to the historical
       behavior whenever the first name match suffices.
    2. Else, if exactly ONE model in the whole estate carries every required
       property, use it — with a ``logger.warning`` naming the substitution
       (honest telemetry, not silent).
    3. Else fail loud with an error listing the candidate models seen (name
       matches and property carriers) and their property counts.

    A mapped name with NO model match at all keeps the existing
    'no SiteWise asset model named' fail-loud error (a genuinely stale mapping is
    not a disambiguation problem).
    """
    target = (table or "").strip().lower()
    named = [m for m in models if (m.get("name") or "").strip().lower() == target]
    if not named:
        raise MetricResolverError(
            f"no SiteWise asset model named {table!r} in system {system_id!r}"
        )
    # De-duplicate required property names (grouped COUNT distributions use the
    # same property as measure AND dimension) while preserving order.
    required: list[str] = []
    seen: set[str] = set()
    for col in columns:
        key = (col or "").strip().lower()
        if key not in seen:
            seen.add(key)
            required.append(col)
    # (1) a same-named model that has the required property/ies.
    for model in named:
        if model.get("id") and _model_has_columns(model, required):
            return model
    # (2) exactly one model in the estate carries the required property/ies.
    propertied = [
        m for m in models if m.get("id") and _model_has_columns(m, required)
    ]
    if len(propertied) == 1:
        chosen = propertied[0]
        logger.warning(
            "SiteWise model disambiguation in system %r: mapped asset model %r "
            "does not carry required propert%s %s; substituting the unique model "
            "%r (id=%s) that does",
            system_id, table, "y" if len(required) == 1 else "ies",
            ", ".join(repr(c) for c in required),
            chosen.get("name"), chosen.get("id"),
        )
        return chosen
    # (3) ambiguous (or absent everywhere) → fail loud with the landscape.
    cols = ", ".join(repr(c) for c in required)
    detail = f"candidate models by name: [{_describe_model_candidates(named)}]"
    if propertied:
        detail += (
            f"; models carrying the propert"
            f"{'y' if len(required) == 1 else 'ies'}: "
            f"[{_describe_model_candidates(propertied)}]"
        )
    raise MetricResolverError(
        f"no SiteWise property named {cols} on asset model {table!r} in system "
        f"{system_id!r}; {detail}"
    )


def _aggregate_sitewise(aggregation: Aggregation, usable: list[tuple]) -> float | int | None:
    """Aggregate ``[(value, timestamp), ...]`` per the metric's aggregation.

    COUNT = number of usable assets; LATEST = the value with the newest timestamp
    (so it needs NO time/ordering SQL dimension — the reason SiteWise bypasses
    ``_compose_sql``); AVG/SUM/MIN/MAX operate on the numeric-coerced values.
    """
    if aggregation is Aggregation.COUNT:
        return len(usable)
    if aggregation is Aggregation.LATEST:
        newest = max(
            usable, key=lambda pair: pair[1] if pair[1] is not None else float("-inf")
        )
        return _coerce_scalar(newest[0])
    numbers = [_coerce_scalar(value) for (value, _ts) in usable]
    if aggregation is Aggregation.SUM:
        return sum(numbers)
    if aggregation is Aggregation.AVG:
        return sum(numbers) / len(numbers)
    if aggregation is Aggregation.MIN:
        return min(numbers)
    if aggregation is Aggregation.MAX:
        return max(numbers)
    raise MetricResolverError(f"unsupported SiteWise aggregation: {aggregation!r}")


def _execute_sitewise(plan: QueryPlan) -> tuple[float | int | None, int]:
    """Resolve a DIRECT metric scalar for a SiteWise (Direct) system (non-SQL).

    Returns ``(value, sample_count)`` where ``sample_count`` is the number of
    usable per-asset values behind the aggregate (the 2A honest-data seam — the
    SiteWise analogue of the paired ``COUNT(col)``).

    Mirrors the metric's aggregation over the mapped property across EVERY asset of
    the mapped asset model, via the shared ``sitewise_read`` seam (DRY — no new
    SiteWise SDK code here). ``plan.table`` is the asset-MODEL name and
    ``plan.column`` the PROPERTY name (the onboarding pseudo-table / field).

    Fail loud (``MetricResolverError``) if the model/property name is unresolved or
    no asset yields a usable value — never a silent/fake fallback (ADR-0011 F5).
    connection_config is resolved via ``_full_connection_config`` (the FULL config
    incl. any cross-account role_arn/external_id from the agent-config secret, not
    just the non-secret catalog snapshot); region from that config or the
    ``AWS_REGION`` env.
    """
    connection_config = _full_connection_config(plan.system_id)
    region = connection_config.get("region") or os.environ.get("AWS_REGION")

    models = sitewise_read.list_asset_models_with_properties(connection_config, region)
    # Duplicate-named models exist in real estates (hierarchy shells shadowing the
    # propertied model) — disambiguate instead of taking a blind first name match.
    model = _select_sitewise_model(
        models, plan.table, [plan.column], system_id=plan.system_id
    )
    prop = _match_by_name(model.get("properties") or [], plan.column)
    if not prop or not prop.get("id"):
        raise MetricResolverError(
            f"no SiteWise property named {plan.column!r} on asset model "
            f"{plan.table!r} in system {plan.system_id!r}"
        )

    assets = sitewise_read.list_assets(
        connection_config, region, asset_model_id=model["id"]
    )
    # BATCH read (event-loop fix): one BatchGetAssetPropertyValue sweep (pages of
    # 16) instead of N sequential GetAssetPropertyValue round-trips — same change
    # as the grouped path (metric_dimension._execute_sitewise_grouped). Per-entry
    # errors surface as {"error": ...} per key: that asset is skipped, the rest
    # of the sweep survives (unchanged tolerance).
    asset_ids = [a.get("id") for a in assets if a.get("id")]
    values = sitewise_read.batch_get_property_values(
        connection_config, region,
        [{"assetId": aid, "propertyId": prop["id"]} for aid in asset_ids],
    )
    usable: list[tuple] = []
    for asset_id in asset_ids:
        pv = values.get(f"{asset_id}_{prop['id']}")
        if not isinstance(pv, dict) or pv.get("error"):
            continue
        value = pv.get("value")
        if value is None:
            continue
        usable.append((value, pv.get("timestamp")))

    if not usable:
        raise MetricResolverError(
            f"no usable SiteWise values for {plan.table!r}.{plan.column!r} across "
            f"{len(assets)} asset(s) in system {plan.system_id!r}"
        )
    return _aggregate_sitewise(plan.aggregation, usable), len(usable)


def _build_direct_lineage(
    metric_id: str,
    concept: str,
    auth: AuthoritySource,
    mapping: MappingProvenance,
    plan: QueryPlan,
    sql: str,
) -> Lineage:
    """Stamp the direct-resolve lineage from data already touched (ADR 0001/0017)."""
    return Lineage(
        metric_id=metric_id,
        kind="direct",
        concept=concept,
        authority=auth,
        mapping=mapping,
        query_plan=plan,
        sql=sql,
        executed_at=_utcnow_iso(),
    )


def _window_predicate(conn: str, window: str, column: str) -> str:
    """The closed-vocabulary event-time predicate for ``column`` in the SQL
    dialect of ``conn``. Athena and S3 Tables share the
    ``current_timestamp - INTERVAL 'n' UNIT`` form (valid in both engines);
    PostgreSQL keeps its own."""
    from . import metric_dimension as md
    if conn == connection_types.POSTGRESQL:
        return md._pg_window_predicate(window, column)
    return md._athena_window_predicate(column, md.WINDOW_VOCAB[window][1])


def _apply_scalar_window(plan: QueryPlan, sql: str, window: str) -> tuple[str | None, str | None, str]:
    """Apply a windowed metric's event-time WHERE to a composed scalar aggregate
    (fix-pass: scalar parity with the grouped Task-2D seam). Returns
    ``(applied_token, window_column, sql)``.

    The predicate column is the measure table's mapped :data:`metric_dimension.
    TIME_CONCEPT` column (the SAME graph read the grouped/LATEST paths use) and
    the interval comes from the CLOSED ``WINDOW_VOCAB`` — the composers are the
    shared ``metric_dimension`` predicate helpers (lazy import, same acyclicity
    note as :func:`_athena_time_window_where`). NO event-time mapping — or a
    backend the predicate dialects don't cover — returns ``(None, None, sql)``:
    the HONEST lifetime fallback (``window: null`` in lineage, never a
    fabricated window). The scalar SELECT has no GROUP BY, so the predicate
    appends at the end (AND-extending an existing Athena-prune WHERE)."""
    from . import metric_dimension as md
    conn = _connection_type(plan.system_id)
    if conn not in connection_types.METRIC_SQL_CONNECTION_TYPES:
        return None, None, sql
    column = md._resolve_order_column(plan.system_id, plan.table)
    if not column:
        return None, None, sql
    predicate = _window_predicate(conn, window, column)
    return window, column, _and_where(sql, predicate)


_INVARIANT_STATUS = "invariant_violation"
_RANGE_VIOLATION_DETAIL = "value outside declared valid range"


def _declared_denominator(metric: Metric) -> str | None:
    if isinstance(metric, DerivedMetric):
        return metric.denominator_provenance
    return None


def _attach_invariant_declarations(
    metric: Metric, resolved: ResolvedMetric
) -> ResolvedMetric:
    lineage = resolved.lineage
    if lineage is None:
        return resolved
    lineage = dataclasses.replace(
        lineage,
        valid_range=metric.valid_range,
        max_exclusive=metric.max_exclusive,
        denominator_provenance=_declared_denominator(metric),
    )
    return dataclasses.replace(resolved, lineage=lineage)


def _invariant_failure(
    resolved: ResolvedMetric, violation: InvariantViolation
) -> ResolvedMetric:
    lineage = resolved.lineage
    if lineage is not None:
        lineage = dataclasses.replace(lineage, invariant_violation=violation)
    return dataclasses.replace(
        resolved,
        value=None,
        lineage=lineage,
        status=_INVARIANT_STATUS,
        detail=violation.detail,
    )


def _check_denominator_invariant(
    provenance: str | None,
    denominator_lineages: tuple[Lineage | None, ...] | None,
    resolved: ResolvedMetric,
) -> ResolvedMetric | None:
    if provenance is None or denominator_lineages is None:
        return None
    observed = tuple(
        denominator.concept if denominator is not None else None
        for denominator in denominator_lineages
    )
    if observed and all(concept == provenance for concept in observed):
        return None
    mismatch = next((concept for concept in observed if concept != provenance), None)
    detail = (
        "cannot compute: denominator undefined — declared basis "
        f"{provenance!r} not satisfied"
    )
    return _invariant_failure(
        resolved,
        InvariantViolation(
            reason=_INVARIANT_STATUS,
            detail=detail,
            observed_denominator_concept=mismatch,
        ),
    )


def _check_range_invariant(
    valid_range: tuple[float, float] | None,
    max_exclusive: bool,
    resolved: ResolvedMetric,
) -> ResolvedMetric | None:
    if valid_range is None or resolved.value is None:
        return None
    lower, upper = valid_range
    upper_satisfied = (
        resolved.value < upper if max_exclusive else resolved.value <= upper
    )
    if lower <= resolved.value and upper_satisfied:
        return None
    return _invariant_failure(
        resolved,
        InvariantViolation(
            reason=_INVARIANT_STATUS,
            detail=_RANGE_VIOLATION_DETAIL,
            offending_value=resolved.value,
        ),
    )


def _finalize_metric_invariants(
    metric: Metric,
    resolved: ResolvedMetric,
    *,
    denominator_lineages: tuple[Lineage | None, ...] | None = None,
) -> ResolvedMetric:
    """Withhold values that violate declared deterministic invariants."""
    provenance = _declared_denominator(metric)
    if metric.valid_range is None and provenance is None:
        return resolved
    resolved = _attach_invariant_declarations(metric, resolved)
    denominator_failure = _check_denominator_invariant(
        provenance, denominator_lineages, resolved
    )
    if denominator_failure is not None:
        return denominator_failure
    return (
        _check_range_invariant(
            metric.valid_range,
            metric.max_exclusive,
            resolved,
        )
        or resolved
    )


class _DirectExecution(NamedTuple):
    value: float | int | None
    sample_count: int | None
    sql: str
    window_token: str | None = None
    window_column: str | None = None
    predicates: tuple[mp.PredicateBinding, ...] | None = None
    scan: dict | None = None
    # the resolved (p, value) pairs for a duration-percentile metric.
    percentiles: "tuple[tuple[float, float | int | None], ...] | None" = None


def _requested_window(metric_id: str, metric: DirectMetric) -> str | None:
    requested = metric.window
    if requested is not None:
        from . import metric_dimension as md
        md._validate_window(requested, metric_id)
    return requested


def _s3tables_namespace(system_id: str, table: str | None) -> str:
    """The S3 Tables namespace the metric SQL names (``s3t.<namespace>.<table>``).

    The metric layer carries bare table names, but the connector needs the
    namespace too, and the catalog ``schema`` of an S3 Tables system is only a
    label. The namespace comes from the system's own connection config: the
    scoped ``tables`` entry (``namespace.table``) that names ``table``, else the
    one configured namespace (else the catalog schema when it is one of
    several). Anything ambiguous or absent fails loud, never a guess."""
    cfg = _full_connection_config(system_id) or {}
    wanted = (table or "").lower()
    scoped = {str(e).partition(".")[0] for e in cfg.get("tables") or []
              if "." in str(e) and (not wanted
                                    or str(e).partition(".")[2].lower() == wanted)}
    namespaces = {str(n) for n in cfg.get("namespaces") or []}
    # A configured ``tables`` list is authoritative for a named table: a table
    # it does not list is outside the system's scope, whatever namespaces say.
    candidates = scoped if (cfg.get("tables") and wanted) else (scoped or namespaces)
    if len(candidates) > 1:
        label = get_schema(system_id)
        candidates = {n for n in candidates if n.lower() == (label or "").lower()}
    if len(candidates) != 1:
        raise MetricResolverError(
            f"no single S3 Tables namespace resolved for system {system_id!r}"
            + (f" table {table!r}" if table else "")
            + " (the connection config lists none, or several)")
    return validate_identifier(next(iter(candidates)))


def plan_schema(system_id: str, table: str | None = None) -> str:
    """The ONE schema/database resolution for composed metric SQL (scalar AND
    grouped paths). The catalog schema may be EMPTY for an Athena system — its
    database lives in the connection config — so resolve BEFORE composition;
    otherwise ``validate_identifier('')`` escaped as an opaque 500 on every
    Athena-backed direct metric (live-found 2026-09-26 on a predicate preview).
    Fail loud when neither source provides a database. An S3 Tables system's
    "schema" is its namespace (:func:`_s3tables_namespace`, which reads
    ``table`` to pick it when several are configured)."""
    conn = _connection_type(system_id)
    if conn == connection_types.S3TABLES_DUCKDB:
        return _s3tables_namespace(system_id, table)
    schema = get_schema(system_id)
    if not schema and conn == connection_types.ATHENA:
        from .entity_render import _athena_schema_from_config
        schema = _athena_schema_from_config(system_id)
    if not schema and conn in (connection_types.POSTGRESQL, connection_types.ATHENA):
        # Non-SQL systems (SiteWise) never compose SQL — an empty schema is fine.
        raise MetricResolverError(
            f"no schema/database resolved for system {system_id!r} "
            f"(catalog schema and connection config are both missing)"
        )
    return schema


def _bind_field_ref(system_id: str, table: str, role: str,
                    ref: "mmr.FieldRef") -> str:
    """Pin a measure field ref's column on the measure ``table``: the column must be a column of ``table`` that MAPS_TO ``ref.concept``
    in the live graph. Fail-closed, an unmapped or wrong-table column
    WITHHOLDS via ``UnresolvedPredicateError`` NAMING the field (never a
    guess). Returns the validated column name EXACTLY as declared."""
    params = {"sid": system_id, "table": table, "concept": ref.concept}
    candidates = mb.candidates_from_rows(
        neptune_query(_PREDICATE_BINDING_CYPHER, params) or [])
    if ref.column not in {c.column for c in candidates}:
        raise UnresolvedPredicateError(
            f"measure {role} field: column {ref.column!r} is not mapped to "
            f"concept {ref.concept!r} on the measure table {table!r} in system "
            f"{system_id!r}",
            concept=ref.concept,
        )
    try:
        return mb.validate_column_name(ref.column)
    except ValueError as e:
        raise MetricResolverError(
            f"invalid measure {role} field: column {ref.column!r} is not a SQL "
            f"identifier"
        ) from e


def _bind_measure(system_id: str, table: str,
                  measure: "mmr.DurationMeasure") -> "tuple[mmr.DurationMeasure, dict]":
    """Both timestamp columns pinned on the measure table. The concepts
    are kept; the columns are verified-and-returned EXACTLY as declared. Also
    returns the per-role cast map: a DATE column is cast to timestamp
    in the duration expr, a TIMESTAMP column is used as-is, an unsupported
    physical type WITHHOLDS before any SQL."""
    start_col = _bind_field_ref(system_id, table, "start", measure.start)
    end_col = _bind_field_ref(system_id, table, "end", measure.end)
    bound = dataclasses.replace(
        measure,
        start=mmr.FieldRef(measure.start.concept, start_col),
        end=mmr.FieldRef(measure.end.concept, end_col),
    )
    casts = _measure_casts(system_id, table, bound)
    return bound, casts


#: the physical type of ONE column on a system table, from the Field the
#: resolver's own graph already carries (``Field.data_type``). ``None`` when the
#: deployment recorded no type for it (unknown, the caller then WITHHOLDS,
#: a type that cannot be proven temporal fails closed).
_FIELD_TYPE_CYPHER = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)"
    "-[:HAS_FIELD]->(f:Field) "
    "WHERE se.name = $table AND f.name = $column "
    "RETURN f.data_type AS data_type LIMIT 1")


def _measure_column_type(system_id: str, table: str, column: str) -> str | None:
    """``Field.data_type`` for one measure column, or ``None`` when no type was
    recorded. Fail-soft: any graph fault yields ``None`` (unknown)."""
    try:
        rows = neptune_query(
            _FIELD_TYPE_CYPHER,
            {"sid": system_id, "table": table, "column": column}) or []
    except Exception:  # noqa: BLE001 - a type read must never break a resolve
        return None
    data_type = rows[0].get("data_type") if rows else None
    return data_type if isinstance(data_type, str) and data_type.strip() else None


def _measure_casts(system_id: str, table: str,
                   measure: "mmr.DurationMeasure") -> dict:
    """The per-role (``start``/``end``) type classification for the measure
    columns. Each value is ``"zoned"`` | ``"unzoned"`` | ``"date"``, the
    duration expr casts a ``date`` to timestamp and the window literal matches
    the column's zone. A type that cannot be PROVEN temporal (a recorded
    non-temporal type, OR no recorded type at all, a graph miss or read
    failure) WITHHOLDS via ``UnresolvedPredicateError`` NAMING the field, so an
    incompletely recorded schema never reaches backend SQL that fails only at
    execution (fail-closed)."""
    casts: dict[str, str] = {}
    for role, ref in (("start", measure.start), ("end", measure.end)):
        data_type = _measure_column_type(system_id, table, ref.column)
        classification = mmr.classify_timestamp_type(data_type)
        if classification not in mmr.TEMPORAL_TYPES:
            shown = repr(data_type) if data_type else "unrecorded"
            raise UnresolvedPredicateError(
                f"measure {role} field: column {ref.column!r} physical type "
                f"({shown}) cannot be proven to be a timestamp or date; a "
                "duration measure needs a temporal column",
                concept=ref.concept,
            )
        casts[role] = classification
    return casts


def _direct_plan(
    metric: DirectMetric, auth: AuthoritySource,
    mapping: MappingProvenance | None = None,
) -> tuple[MappingProvenance, QueryPlan]:
    """The ``(mapping, plan)`` for a direct metric. ``mapping`` is the resolved
    metric-concept table/column; when omitted it is resolved here through
    ``_resolve_table_column`` (byte-identical to the original two-argument
    form). A measure metric whose concept is unbound passes the measure-pin
    mapping (table from the pins, empty concept column) so no fake column is
    invented."""
    if mapping is None:
        mapping = _resolve_table_column(auth.system_id, metric.concept)
    measure = None
    casts = None
    if metric.measure is not None:  # pin the two timestamp columns here
        measure, casts = _bind_measure(auth.system_id, mapping.table, metric.measure)
    plan = QueryPlan(
        system_id=auth.system_id,
        schema=plan_schema(auth.system_id, mapping.table),
        table=mapping.table,
        column=mapping.column,
        aggregation=metric.aggregation,
        measure=measure,
        percentiles=metric.percentiles,
        measure_casts=casts,
    )
    return mapping, plan


def _execute_direct_plan(
    plan: QueryPlan,
    requested_window: str | None,
    predicates: tuple[Predicate, ...] | None = None,
) -> _DirectExecution:
    # ADR-0075: bind predicates FIRST, so an unbound concept (or a non-SQL
    # backend) fails before any SQL, prune discovery or execution.
    bindings = _predicate_bindings_for(plan.system_id, plan.table, predicates)
    connection_type = _connection_type(plan.system_id)
    _reject_unsupported_measure_backend(plan, connection_type)  #
    require_metric_reader(plan.system_id)  # no SQL for a system with no reader
    if connection_type == connection_types.SITEWISE_DIRECT:
        value, count = _execute_sitewise(plan)
        _note_execution(plan.system_id)
        sql = f"sitewise:{plan.aggregation.value}({plan.table}.{plan.column})"
        return _DirectExecution(value, count, sql)
    from . import metric_athena_scope as mas
    sql, scan = _compose_sql(plan), None
    athena = connection_type == connection_types.ATHENA
    measure = plan.measure is not None
    # a duration-percentile is NOT decomposable (like AVG), so it is
    # narrowed up front on Athena, same as a non-eligible aggregate; an
    # explicit turn window is the bound instead (never narrowed past it).
    if athena and plan.absolute_window is None and (
            measure or not mas.exact_eligible(plan.aggregation)):
        sql, scan = _athena_narrowed(plan, sql, mas.REASON_SHAPE)
    window_token, window_column, sql = _apply_direct_window(
        plan, sql, requested_window, measure)
    if bindings:
        sql = _apply_predicates(sql, bindings)
    if athena:
        row, sql, scan = _execute_athena_scalar(plan, sql, scan)
    else:
        row = _execute_row(plan, sql)
    if measure:
        return _measure_execution(plan, row, sql, window_token, window_column,
                                  bindings, scan)
    value = _coerce_scalar(row[0])
    count = _coerce_count(row[1]) if len(row) > 1 else None
    return _DirectExecution(
        value, count, sql, window_token, window_column, bindings, scan
    )


def _reject_unsupported_measure_backend(plan: QueryPlan, connection_type) -> None:
    """ a duration-percentile is a SQL-backend metric ONLY, reject every
    other backend (incl. SiteWise) BEFORE the SiteWise branch runs."""
    if plan.measure is not None and (
            connection_type not in connection_types.METRIC_SQL_CONNECTION_TYPES):
        raise StructuralMetricError(
            "a duration-percentile metric is supported on Athena / PostgreSQL "
            f"/ S3 Tables only; system {plan.system_id!r} is {connection_type!r}")


def _apply_direct_window(plan: QueryPlan, sql: str, requested_window,
                         measure: bool) -> tuple[str | None, str | None, str]:
    """Apply the scalar/measure window. A measure plan's absolute turn window takes precedence over any declared token; otherwise the declared
    token applies as before. ``(window_token, window_column, sql)``."""
    if measure and plan.absolute_window is not None:
        window_column, sql = _apply_absolute_measure_window(
            plan, sql, plan.absolute_window)
        return "turn-window", window_column, sql
    if requested_window is not None:
        return (_apply_measure_window(plan, sql, requested_window) if measure
                else _apply_scalar_window(plan, sql, requested_window))
    return None, None, sql


def _measure_execution(plan: QueryPlan, row, sql: str, window_token,
                       window_column, bindings, scan) -> _DirectExecution:
    """Read the N leading percentile cells plus the trailing COUNT of a
    duration-percentile result. ``value`` stays None, there is no single
    scalar; the (p, value) pairs ride ``percentiles``."""
    n = len(plan.percentiles)
    pairs = tuple((p, _coerce_scalar(row[i]))
                  for i, p in enumerate(plan.percentiles))
    count = _coerce_count(row[n]) if len(row) > n else None
    return _DirectExecution(
        None, count, sql, window_token, window_column, bindings, scan,
        percentiles=pairs,
    )


def _apply_measure_window(plan: QueryPlan, sql: str,
                          window: str) -> tuple[str | None, str | None, str]:
    """Apply a windowed duration-percentile metric's event-time WHERE to the
    measure's END column (: the moment the duration completes),
    not a generic event-time mapping. Composed from the closed WINDOW_VOCAB;
    identifiers validated."""
    from . import metric_dimension as md
    conn = _connection_type(plan.system_id)
    if conn not in connection_types.METRIC_SQL_CONNECTION_TYPES:
        return None, None, sql
    column = plan.measure.end.column
    return window, column, _and_where(sql, _window_predicate(conn, window, column))

def _apply_absolute_measure_window(plan: QueryPlan, sql: str,
                                   window: "AbsoluteWindow") -> tuple[str, str]:
    """Apply an absolute ``[start, end)`` UTC window to the measure's END
    column. Returns ``(column, sql)``; the dialect literal composer
    lives in ``metric_measure_rule`` (identifiers validated, instants
    charset-gated)."""
    column = plan.measure.end.column
    col_type = (plan.measure_casts or {}).get("end", "zoned")
    predicate = mmr.absolute_window_where(
        column, window.start_utc, window.end_utc,
        dialect=_measure_dialect(plan.system_id), col_type=col_type)
    return column, _and_where(sql, predicate)



def _athena_narrowed(plan: QueryPlan, sql: str, reason: str) -> tuple[str, dict | None]:
    """``sql`` bounded to the table's recent partitions (else its last days of
    event time), and the disclosure of that narrowing; unchanged when neither
    bound is available. A measure (duration) metric bounds the measure's END
    column (the column it measures to) with the same window rule a resolved
    question uses: ANDed onto a recent-partition bound (which keeps the
    pruning), or in place of the generic event-time fallback. Where no bound
    would apply the query is unchanged, and a non-measure metric keeps the
    generic choice (byte-identical)."""
    from . import metric_athena_scope as mas
    partition = _athena_recent_partition_where(plan.system_id, plan.table)
    where = partition or _athena_time_window_where(plan.system_id, plan.table)
    if not where:
        return sql, None
    if plan.measure is not None:  # bound the END column, not the generic one
        from . import metric_dimension as md
        end = md._athena_window_predicate(
            plan.measure.end.column, (str(md.ATHENA_TIME_WINDOW_DAYS), "DAY"))
        where = f"({partition}) AND ({end})" if partition else end
    return _and_where(sql, where), mas.narrowed(where, reason)


def _execute_athena_scalar(plan: QueryPlan, sql: str, scan: dict | None):
    """Run the metric as defined (exact past the scan limit); narrow and say so
    only when the engine refused it and no exact path existed. A read bounded
    by an explicit turn window is never narrowed (``mas.refuse_windowed``)."""
    from . import metric_athena_scope as mas
    try:
        row, recorded = mas.collecting(_execute_row, plan, sql)
    except Exception as exc:
        if scan is not None or not mas.is_scan_refusal(exc):
            raise
        if plan.absolute_window is not None:
            mas.refuse_windowed(exc, plan)
        reason = mas.refusal_reason(exc, plan.system_id, plan_lineage(plan))
        sql, scan = _athena_narrowed(plan, sql, reason)
        if scan is None:
            raise
        row, recorded = mas.collecting(_execute_row, plan, sql)
    return row, sql, scan or recorded


def _withheld(
    metric_id: str,
    metric: DirectMetric,
    auth: AuthoritySource,
    err: CoverageGapError,
    mapping: MappingProvenance | None = None,
    plan: QueryPlan | None = None,
) -> ResolvedMetric:
    """ADR-0075 D3 / ADR-0076 D3: WITHHOLD — nothing executed, the gap NAMED.
    An ambiguous OWN-concept binding withholds before a plan exists (``plan``
    and ``mapping`` ``None``; the system is known). Deliberately not routed
    through the invariant finalizer (no value exists)."""
    status = (AMBIGUOUS_BINDING_STATUS if isinstance(err, AmbiguousBindingError)
              else UNRESOLVED_PREDICATE_STATUS)
    lineage = Lineage(
        metric_id=metric_id, kind="direct", concept=metric.concept,
        authority=auth, mapping=mapping, query_plan=plan, sql=None,
        executed_at=_utcnow_iso(),
    )
    return ResolvedMetric(
        metric_id=metric_id, value=None, unit=metric.unit, label=metric.label,
        query_plan=plan, system_id=auth.system_id, lineage=lineage,
        sample_count=None, status=status, detail=str(err),
    )


def _resolve_direct(
    metric_id: str,
    metric: DirectMetric,
    authority: AuthorityResolver | None,
    turn_window: "AbsoluteWindow | None" = None,
) -> ResolvedMetric:
    requested_window = _requested_window(metric_id, metric)
    try:
        auth, mapping = _resolve_measure_target(metric, authority)
    except (AmbiguousBindingError, UnresolvedPredicateError) as e:
        # an ambiguous OWN-concept binding withholds before a plan exists; the
        # authority system resolved (the ambiguity is the column), so
        # re-resolve it for the withhold lineage.
        return _withheld(
            metric_id, metric,
            _resolve_authority(metric.concept, authority), e)
    try:
        _mapping, plan = _direct_plan(metric, auth, mapping)
    except (AmbiguousBindingError, UnresolvedPredicateError) as e:
        # a measure field ref whose column is not mapped on the measure
        # table withholds here, before a plan exists (the field is NAMED).
        return _withheld(metric_id, metric, auth, e)
    if turn_window is not None and plan.measure is not None:
        plan = dataclasses.replace(plan, absolute_window=turn_window)
    try:
        execution = _execute_direct_plan(plan, requested_window, metric.filter)
    except (UnresolvedPredicateError, AmbiguousBindingError) as e:
        return _withheld(metric_id, metric, auth, e, mapping, plan)
    lineage = _decorate_direct_lineage(
        metric_id, metric, auth, mapping, plan, execution, requested_window)
    resolved = ResolvedMetric(
        metric_id=metric_id, value=execution.value, unit=metric.unit,
        label=metric.label, query_plan=plan, system_id=plan.system_id,
        lineage=lineage, sample_count=execution.sample_count,
        percentiles=execution.percentiles,
    )
    return _finalize_metric_invariants(metric, resolved)


def _decorate_direct_lineage(metric_id, metric, auth, mapping, plan, execution,
                             requested_window) -> Lineage:
    """Build the direct-resolve lineage and stamp the optional scan / predicate
    / window / measure facets (unchanged from the inline form)."""
    lineage = _build_direct_lineage(
        metric_id, metric.concept, auth, mapping, plan, execution.sql)
    if execution.scan is not None:
        lineage = dataclasses.replace(lineage, scan=execution.scan)
    if metric.filter is not None:
        lineage = dataclasses.replace(lineage, predicates=execution.predicates)
    if requested_window is not None:
        lineage = dataclasses.replace(
            lineage, window_requested=requested_window,
            window=execution.window_token, window_column=execution.window_column)
    if plan.measure is not None:  # the bound measure + declared percentiles
        lineage = dataclasses.replace(
            lineage, measure=plan.measure, percentiles=metric.percentiles,
            approximate=mmr.dialect_is_approximate(_measure_dialect(plan.system_id)))
    return lineage


#: ADR-0066 D6 / ADR-0052 evidence class: a graph-resident metric definition
#: RESOLVED a value through this path. Records DELIVERY/COMPOSITION only —
#: never a claim the model "followed" the definition (ADR-0063 D7: that claim
#: is unprovable and stays unmade).
_GRAPH_DEFINITION_RESOLVE_EVIDENCE = "metric-resolve"


def _record_graph_definition_usage(metric_id: str) -> None:
    """Best-effort usage-ledger bump after a successful GRAPH-DEFINED resolve.

    The ADR-0052 gate arm for the ``metric_definition`` artifact: the writer
    (lifecycle accept) ships with its read path (the composed layer this
    resolve just went through) plus ledger evidence it fired. Gated on the
    composed layer's ``is_graph_defined`` seam, so code metrics bump NOTHING —
    their usage story is the existing card/rule machinery — and a plain code
    layer (no seam) degrades to no-op. Evidence proves delivery, never
    obedience (ADR-0066 D6). SWALLOWS everything: a ledger write must never
    fail a resolve.
    """
    try:
        is_graph_defined = getattr(get_metric_layer(), "is_graph_defined", None)
        if not callable(is_graph_defined) or not is_graph_defined(metric_id):
            return
        from .artifact_usage import bump_artifact_usage
        bump_artifact_usage([("metric_definition", metric_id)],
                            evidence=_GRAPH_DEFINITION_RESOLVE_EVIDENCE)
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a resolve
        logger.debug(f"Graph-definition usage bump skipped (non-critical): {e}")


def resolve_metric(metric_id: str, *, authority: AuthorityResolver | None = None,
                   turn_window: "AbsoluteWindow | None" = None) -> ResolvedMetric:
    """Resolve one metric through the deterministic shared path.

    ``turn_window`` is the turn's resolved absolute ``[start, end)``
    UTC window; when the metric carries a duration measure it bounds the
    measure's END column (the moment the duration completes), overriding a
    declared window token. ``None`` keeps the declared-token / lifetime path.
    """
    try:
        metric = get_metric_layer().get_metric(metric_id)
    except KeyError as e:
        raise MetricResolverError(f"unknown metric id: {metric_id!r}") from e
    if turn_window is not None and getattr(metric, "measure", None) is not None:
        resolved = _resolve_direct(metric_id, metric, authority,
                                   turn_window=turn_window)
    else:
        resolved = resolve_definition(metric, metric_id=metric_id, authority=authority)
    # ADR-0066 D6: fires ONLY for graph-defined ids; a code-metric resolve is
    # byte-identical to the pre-telemetry path (no ledger interaction at all).
    _record_graph_definition_usage(metric_id)
    return resolved


def resolve_turn_metric(metric_id: str, *,
                        authority: AuthorityResolver | None = None,
                        turn_window: "AbsoluteWindow | None" = None):
    """The TURN entry point: resolve the SHAPE the question asks for.

    A duration-percentile metric that DECLARES a ``group_by`` field ref resolves
    through the grouped resolver (one row per group, each with its percentiles
    and sample count); everything else resolves scalar. ``turn_window`` (an
    absolute ``[start, end)`` UTC window) is applied to the measure's END column
    on either path. Returns a ``GroupedResolvedMetric`` for the grouped path or
    a ``ResolvedMetric`` for the scalar path, the turn-notice classifier
    handles both.
    """
    try:
        metric = get_metric_layer().get_metric(metric_id)
    except Exception:  # noqa: BLE001 - layer read optional; scalar path handles it
        metric = None
    group_by = getattr(metric, "group_by", None) if metric is not None else None
    if group_by is not None and getattr(metric, "measure", None) is not None:
        from . import metric_dimension as md
        return md.resolve_metric_by_dimension(
            metric_id, group_by.concept, authority=authority,
            dimension_column=group_by.column, turn_window=turn_window)
    if turn_window is None:  # byte-identical to the plain scalar path
        return resolve_metric(metric_id, authority=authority)
    return resolve_metric(metric_id, authority=authority, turn_window=turn_window)


def resolve_definition(
    metric: Metric,
    *,
    metric_id: str | None = None,
    authority: AuthorityResolver | None = None,
    layer=None,
) -> ResolvedMetric:
    """Resolve an already-built metric definition — the ONE computation home.

    ``resolve_metric`` is "look the id up in the served layer, then this".
    ADR-0080 D1: a dry-run preview hands a PENDING definition here directly
    (the served layer is neither consulted for it nor mutated, and no usage
    telemetry fires). ``layer`` is the explicit metric layer derived
    ``MetricRef`` inputs resolve against; ``None`` keeps the live path
    (inputs go through ``resolve_metric``). With an explicit layer an input it
    does not hold WITHHOLDS as ``unresolved_input`` naming the id.
    """
    mid = metric_id or metric.id
    try:
        return _resolve_any(metric, mid, authority, layer)
    except UnresolvedInputError as e:
        return ResolvedMetric(
            metric_id=mid, value=None, unit=metric.unit, label=metric.label,
            query_plan=None, system_id="derived", lineage=None,
            sample_count=None, status=UNRESOLVED_INPUT_STATUS, detail=str(e),
        )


def _resolve_any(metric: Metric, metric_id: str,
                 authority: AuthorityResolver | None, layer) -> ResolvedMetric:
    """Dispatch on kind; a preview input gap PROPAGATES from here."""
    if isinstance(metric, DerivedMetric) or metric.kind is MetricKind.DERIVED:
        return _resolve_derived(metric, authority, layer)
    return _resolve_direct(metric_id, metric, authority)


def _resolve_ref(metric_id: str, authority: AuthorityResolver | None, layer) -> ResolvedMetric:
    """Resolve one ``MetricRef`` input.

    ``layer is None`` ⇒ the live path through the module-level
    ``resolve_metric`` (served layer + D6 telemetry; the monkeypatch point the
    offline lane uses). An explicit ``layer`` (preview) looks the id up THERE,
    fires no telemetry, and raises ``UnresolvedInputError`` for an id it does
    not hold."""
    if layer is None:
        return resolve_metric(metric_id, authority=authority)
    try:
        metric = layer.get_metric(metric_id)
    except KeyError:
        raise UnresolvedInputError(metric_id) from None
    return _resolve_any(metric, metric_id, authority, layer)


# --- Derived-metric resolution (ADR 0016): op-tree evaluator + prefer_stored ---

def _stored_value(metric: DerivedMetric, authority: AuthorityResolver | None,
                  layer) -> ResolvedMetric | None:
    """The ``prefer_stored`` read, or ``None`` when it is unusable (fall through)."""
    if metric.prefer_stored is None or metric.denominator_provenance is not None:
        return None
    try:
        stored = _resolve_ref(metric.prefer_stored, authority, layer)
    except MetricResolverError:
        return None  # stored read unavailable ⇒ not fatal → derive
    if stored is None or stored.value is None:
        return None
    return ResolvedMetric(
        metric_id=metric.id,
        value=stored.value,
        unit=metric.unit,
        label=metric.label,
        query_plan=None,
        system_id=stored.system_id,
        lineage=Lineage(
            metric_id=metric.id,
            kind="derived",
            op=metric.formula.op.value,
            inputs=(stored.lineage,),
            prefer_stored_hit=True,
        ),
        sample_count=stored.sample_count,
    )


def _resolve_derived(metric: DerivedMetric, authority: AuthorityResolver | None,
                     layer=None) -> ResolvedMetric:
    """Resolve a formula, using stored data only when no denominator is declared.

    Stored-read failures fall through; declared provenance always forces formula
    resolution so the live denominator lineage can be verified.
    """
    stored = _stored_value(metric, authority, layer)
    if stored is not None:
        return _finalize_metric_invariants(metric, stored)
    ev = _eval_term(metric.formula, authority, layer)
    lineage = dataclasses.replace(
        ev.lineage, metric_id=metric.id, prefer_stored_hit=False
    )
    resolved = ResolvedMetric(
        metric_id=metric.id,
        value=ev.value,
        unit=metric.unit,
        label=metric.label,
        query_plan=None,
        system_id="derived",
        lineage=lineage,
    )
    return _finalize_metric_invariants(
        metric,
        resolved,
        denominator_lineages=ev.denominator_lineages,
    )


class _Eval(NamedTuple):
    """Internal evaluator result: value, lineage, and ratio denominator lineage."""
    value: float
    lineage: Lineage | None
    denominator_lineages: tuple[Lineage | None, ...] = ()


def _eval_metric_ref(term: MetricRef, authority: AuthorityResolver | None, layer) -> _Eval:
    """One ``MetricRef`` leaf → value; a named preview gap propagates as-is."""
    try:
        sub = _resolve_ref(term.metric, authority, layer)
    except UnresolvedInputError:
        raise
    except (MetricResolverError, NotImplementedError) as cause:
        raise MetricResolverError(
            f"derived metric unavailable: input {term.metric!r} unresolvable: {cause}"
        ) from cause
    if sub.value is None and sub.status:
        raise MetricResolverError(
            f"derived metric unavailable: input {term.metric!r} withheld "
            f"({sub.status}): {sub.detail}"
        )
    if sub.value is None:
        raise MetricResolverError(
            f"derived metric unavailable: input {term.metric!r} resolved to no value"
        )
    return _Eval(float(sub.value), sub.lineage)


def _eval_term(term, authority: AuthorityResolver | None, layer=None) -> _Eval:
    """Evaluate an op-tree term to a value + lineage (ADR 0016/0017, fail-loud)."""
    if isinstance(term, MetricRef):
        return _eval_metric_ref(term, authority, layer)
    if isinstance(term, Scalar):
        return _Eval(float(term.value), None)  # literal — no data provenance
    if isinstance(term, Formula):
        evals = [_eval_term(i, authority, layer) for i in term.inputs]
        value = _apply_op(term.op, [e.value for e in evals])
        lineage = Lineage(
            metric_id=None,
            kind="derived",
            op=term.op.value,
            inputs=tuple(e.lineage for e in evals if e.lineage is not None),
        )
        denominator_lineages = tuple(
            denominator
            for evaluated in evals
            for denominator in evaluated.denominator_lineages
        )
        if term.op is FormulaOp.RATIO and len(evals) == 2:
            denominator_lineages += (evals[1].lineage,)
        return _Eval(value, lineage, denominator_lineages)
    if isinstance(term, ConceptRef):
        raise MetricResolverError(
            "formula inputs must be MetricRef/Scalar; bare concept "
            f"{term.concept!r} has no aggregation"
        )
    raise MetricResolverError(f"unknown formula term: {term!r}")


def _eval_formula(term, authority: AuthorityResolver | None) -> float:
    """Compat shim (S2.1): float-only view over ``_eval_term`` (ADR 0016 B)."""
    return _eval_term(term, authority).value


def _apply_op(op: FormulaOp, values: list[float]) -> float:
    """Combine already-evaluated floats per the closed op-vocabulary (ADR 0016 B)."""
    if op is FormulaOp.PRODUCT:
        return math.prod(values)
    if op is FormulaOp.SUM:
        return sum(values)
    if op is FormulaOp.RATIO:
        if len(values) != 2:
            raise MetricResolverError(f"ratio expects arity 2, got {len(values)}")
        if values[1] == 0:
            raise MetricResolverError("ratio divide-by-zero")
        return values[0] / values[1]
    if op is FormulaOp.SCALE:
        if len(values) != 2:
            raise MetricResolverError(f"scale expects arity 2, got {len(values)}")
        return values[0] * values[1]
    if op is FormulaOp.WEIGHTED_SUM:
        if len(values) == 0 or len(values) % 2 != 0:
            raise MetricResolverError(
                f"weighted_sum expects even arity (pairs), got {len(values)}"
            )
        return sum(values[i] * values[i + 1] for i in range(0, len(values), 2))
    raise MetricResolverError(f"unknown formula op: {op!r}")


# --- Thin additive FastAPI routes (ADR 0001 same-function path) ---
# Inherit the app-wide AuthMiddleware; no new public/unauthenticated surface.
# Plain-dict error style mirrors resolve.py (no HTTPException).

# --- EVENT-LOOP OFFLOAD PATTERN (single-worker uvicorn protection) -----------
# The resolver core is SYNC (boto3/PostgreSQL/Neptune HTTPS round-trips). Running
# it inline in an ``async def`` handler froze the ONLY uvicorn worker's event
# loop for the duration (observed: 60s stalls, /health timeouts → ECS replaced
# the task, apscheduler "missed by 0:01:13"). The pattern, applied consistently
# to every resolver-backed route here and in dashboard.py / dashboard_drill.py:
#
#   * the handler stays ``async def`` (so it can share this module's semaphore),
#   * its ENTIRE sync body lives in a plain module-level function,
#   * the handler's only statement is ``return await offload_resolver(fn, ...)``
#     — ``asyncio.to_thread`` moves the blocking work to a worker thread, and a
#     small per-event-loop semaphore bounds resolver concurrency so a request
#     convoy cannot pile up unbounded threads against the backends.
#
# /health (server.py) stays trivially non-blocking and is NOT routed through
# this seam. Pinned by tests/unit/test_event_loop_offload.py.
#
# The implementation lives in ``agents/offload.py`` (the ONE home for the
# pattern, shared with every other route family); these names are re-exported
# so existing callers and tests keep working.
from .offload import (  # noqa: E402  (re-export; see agents/offload.py)
    RESOLVER_MAX_CONCURRENCY as _OFFLOAD_MAX_CONCURRENCY,
    _resolver_semaphore as _offload_semaphore,
    _resolver_semaphores as _offload_semaphores,
    offload,
    offload_resolver,
)
from .steward_gate import check_steward, is_steward  # noqa: E402


def _predicates_to_dict(bindings: tuple[mp.PredicateBinding, ...]) -> list[dict]:
    return [
        {"concept": b.concept, "column": b.column, "op": b.op.value,
         "values": list(b.values)}
        for b in bindings
    ]


def _strip_plan_measure_keys(plan_dict: dict, plan: QueryPlan) -> None:
    """Keep a non-measure plan's serialized shape byte-identical: a plan
    with no duration measure carries NONE of the measure keys; a measure plan
    keeps ``percentiles`` as a plain list and drops the internal-only cast map /
    absolute-window object (not part of the public plan shape)."""
    plan_dict.pop("measure_casts", None)
    plan_dict.pop("absolute_window", None)
    if plan.measure is None:
        plan_dict.pop("measure", None)
        plan_dict.pop("percentiles", None)
    elif plan.percentiles is not None:
        plan_dict["percentiles"] = list(plan.percentiles)


def _measure_to_dict(lin: Lineage) -> dict:
    """The lineage ``measure`` block: the bound columns, unit, clamp, and
    that NULL-timestamp rows are excluded (decision 3)."""
    return {
        "kind": lin.measure.kind,
        "start": {"concept": lin.measure.start.concept,
                  "column": lin.measure.start.column},
        "end": {"concept": lin.measure.end.concept,
                "column": lin.measure.end.column},
        "unit": lin.measure.unit,
        "clamp_negative": lin.measure.clamp_negative,
        "excludes_null": True,
    }


def _lineage_to_dict(lin: Lineage | None) -> dict | None:
    """None-safe recursive asdict with embedded enums rendered ``.value`` (ADR 0017)."""
    if lin is None:
        return None
    d = dataclasses.asdict(lin)
    if lin.query_plan is not None:
        d["query_plan"]["aggregation"] = lin.query_plan.aggregation.value
        _strip_plan_measure_keys(d["query_plan"], lin.query_plan)
    if lin.window_requested is None:
        # Non-windowed metric: strip the window-honesty keys so the scalar
        # lineage payload stays byte-identical (golden exact-key-set pin). A
        # windowed metric keeps all three — ``window: null`` IS the honest
        # lifetime-fallback signal (mirrors the grouped plan's semantics).
        d.pop("window_requested", None)
        d.pop("window", None)
        d.pop("window_column", None)
    if lin.valid_range is None:
        d.pop("valid_range", None)
    else:
        d["valid_range"] = list(lin.valid_range)
    if not lin.max_exclusive:
        d.pop("max_exclusive", None)
    if lin.denominator_provenance is None:
        d.pop("denominator_provenance", None)
    if lin.scan is None:
        d.pop("scan", None)
    if lin.predicates is None:
        d.pop("predicates", None)
    else:
        d["predicates"] = _predicates_to_dict(lin.predicates)
    if lin.measure is None:  # byte-identical when absent
        d.pop("measure", None)
        d.pop("percentiles", None)
        d.pop("approximate", None)
    else:
        d["measure"] = _measure_to_dict(lin)
        d["percentiles"] = list(lin.percentiles or ())
        d["approximate"] = bool(lin.approximate)
    if lin.invariant_violation is None:
        d.pop("invariant_violation", None)
    else:
        d["invariant_violation"] = {
            key: value
            for key, value in d["invariant_violation"].items()
            if value is not None
        }
    d["inputs"] = [_lineage_to_dict(child) for child in lin.inputs]
    return d  # ``op`` is already a str (FormulaOp.value stamped at capture)


def _resolve_metric_payload(body: ResolveMetricRequest) -> dict:
    """Sync body of ``POST /resolve`` (runs in a worker thread — see the
    EVENT-LOOP OFFLOAD PATTERN note above)."""
    try:
        m = resolve_metric(body.metric_id)
    except NotImplementedError as e:
        return {"error": str(e), "status": "not_implemented"}
    except (MetricResolverError, KeyError) as e:
        return {"error": str(e)}
    if m.query_plan is None:  # derived metric: no single QueryPlan (ADR 0016 / Flag 1)
        plan = None
    else:
        plan = dataclasses.asdict(m.query_plan)
        plan["aggregation"] = m.query_plan.aggregation.value
        _strip_plan_measure_keys(plan, m.query_plan)
    payload = {
        "metric_id": m.metric_id,
        "value": m.value,
        "unit": m.unit,
        "label": m.label,
        "system_id": m.system_id,
        "query_plan": plan,
        "lineage": _lineage_to_dict(m.lineage),
    }
    if m.status is not None:
        payload["status"] = m.status
    if m.detail is not None:
        payload["detail"] = m.detail
    if getattr(m, "percentiles", None) is not None:  # measure result only
        payload["percentiles"] = [{"p": p, "value": v} for p, v in m.percentiles]
        payload["sample_count"] = m.sample_count
    return payload


@router.post("/resolve")
async def resolve_metric_route(body: ResolveMetricRequest) -> dict:
    """Resolve a metric to a value via the shared resolver core (thin wrapper,
    offloaded — EVENT-LOOP OFFLOAD PATTERN)."""
    return await offload_resolver(_resolve_metric_payload, body)


@router.post("/set-authority", dependencies=[Depends(check_steward)])
async def set_authority_route(body: SetAuthorityRequest, request: Request):
    """Steward seam (ADR 0008 / ADR-0076 D2): the authoritative system for a
    concept and, optionally, its chosen binding ``field`` (offloaded)."""
    return await offload(set_authority_payload, body, request)


class CorrectMappingRequest(BaseModel):
    """User correction of a concept→column mapping from the card lineage view."""
    system_id: str
    concept: str
    to_field: str            # "table.column" — the corrected authoritative column
    from_field: str = ""     # optional prior column, for audit only
    confidence: float = 1.0


def _correct_mapping_payload(body: CorrectMappingRequest, request=None) -> dict:
    """Sync body of ``POST /correct-mapping`` (offloaded on the resolver lane).
    A STEWARD (``steward_gate.is_steward(request)``, read here so the async
    handler holds nothing but the offload) repoints (system, concept)→column
    via the ONE lifecycle (S3.2/ADR 0003), then ripples only affected cards,
    exactly as before. Anyone else, and a call with no request at all, files
    the same repoint PENDING for steward review
    (``_propose_mapping_correction``): nothing reaches the graph, no card
    changes, no cache is dropped. Plain-dict errors."""
    steward = request is not None and is_steward(request)
    # A flattened nested column keeps its dots: the table is the first
    # segment and the column is everything after it.
    table, _sep, column = body.to_field.partition(".")
    if not (table and column):
        return {"error": f"to_field must be 'table.column', got {body.to_field!r}"}
    if not steward:
        return _propose_mapping_correction(body)
    try:
        result = record_user_correction(
            proposed_type="concept_mapping", to_system=body.system_id,
            to_field=body.to_field, concept=body.concept,
            from_system="", from_field=body.from_field, from_value="",
            to_value="", confidence=body.confidence, query_context="user_correction",
        )
    except Exception as e:  # fail-loud surfaced as the plain-dict error style
        from .public_errors import public_error
        return public_error(e, "metric.correct_mapping")
    if os.environ.get("RESOLVER_REDERIVE", "").strip():
        rederive.enqueue(body.concept, audit={
            "text": f"updated from correction · mapping v{result['version']}",
            "version": result.get("version"), "source": "user", "at": _utcnow_iso(),
        })
    # A correction changes what the affected cards AND the mapping-driven panel
    # reads resolve to. Drop both dashboard caches so the NEXT /api/dashboard and
    # the next panel read reflect the corrected mapping immediately (independent
    # of RESOLVER_REDERIVE / the SSE ripple). Lazy import to avoid an import
    # cycle; fail-soft — a correction must never 500 on a cache-bust error.
    try:
        from . import dashboard
        dashboard.invalidate_dashboard_caches()
    except Exception:  # nosec B110 - best-effort cache bust, non-fatal
        logger.debug("invalidate_dashboard_caches after correction failed (non-fatal)",
                     exc_info=True)
    return {
        "status": "accepted",
        "neptune_node_id": result.get("neptune_node_id"),
        "version": result.get("version"),
        "concept": body.concept,
        "affected_cards": rederive.affected_cards(body.concept),
    }


def _propose_mapping_correction(body: CorrectMappingRequest) -> dict:
    """A non-steward's repoint, filed as a pending ``concept_mapping`` proposal
    with the keys the steward path uses. It touches no graph, reads included:
    a system that is not onboarded is refused from the in-memory catalog, and
    the column check (a schema read) runs at the steward's accept
    (``self_learning._user_proposal_refusal``, then the repoint's own
    fail-loud field check). Until a steward accepts it, no card is affected."""
    from . import schema_check
    if not schema_check.is_onboarded_system(body.system_id):
        return {"error": f"system '{body.system_id}' is not an onboarded system"}
    try:
        result = record_user_proposal(
            proposed_type="concept_mapping", to_system=body.system_id,
            to_field=body.to_field, concept=body.concept,
            from_system="", from_field=body.from_field, from_value="",
            to_value="", confidence=body.confidence, query_context="user_correction",
        )
    except Exception as e:  # fail-loud surfaced as the plain-dict error style
        from .public_errors import public_error
        return public_error(e, "metric.correct_mapping")
    return {
        "status": result.get("status"),
        "mapping_id": result.get("mapping_id"),
        "version": result.get("version"),
        "concept": body.concept,
        "affected_cards": [],
    }


@router.post("/correct-mapping")
async def correct_mapping_route(body: CorrectMappingRequest, request: Request) -> dict:
    """Repoint (system, concept)→column. Inherits AuthMiddleware; only a
    steward's repoint applies at once (see ``_correct_mapping_payload``).
    Offloaded on ``offload_resolver``, the metric-resolver family's lane
    (agents/offload.py): it changes what the metric cards resolve to and
    ripples them, and its body writes DynamoDB and the graph."""
    return await offload_resolver(_correct_mapping_payload, body, request)


class RetireMappingRequest(BaseModel):
    """A steward's retirement of ONE concept -> column link."""
    system_id: str
    concept: str
    field: str               # "table.column" of the link to remove (nested keeps its dots)


def _retire_mapping_payload(body: RetireMappingRequest, request=None) -> dict:
    """Sync body of ``POST /retire-mapping`` (offloaded on the resolver lane).

    Removes exactly one ``Field-[:MAPS_TO]->Concept`` edge; the concept's
    other links stay (the correction route REPOINTS a concept, deleting all
    of them). A steward's call is recorded ACCEPTED through the same door as
    a correction (``record_user_correction``: source user, audited, the edge
    removed at once) and is replayed after every provision so a re-onboard
    cannot bring the link back; the next delegated question no longer sees
    it (the system agent reads its mappings per question). Anyone else's
    call is filed pending for a steward. Plain-dict errors."""
    system_id, concept, field = (body.system_id.strip(), body.concept.strip(),
                                 body.field.strip())
    table, _sep, column = field.partition(".")
    if not (system_id and concept and table and column):
        return {"error": "system_id, concept and field ('table.column') are required, "
                         f"got field {body.field!r}"}
    steward = request is not None and is_steward(request)
    kwargs = dict(proposed_type="concept_mapping_retire", to_system=system_id,
                  to_field=field, concept=concept, from_system="", from_field="",
                  from_value="", to_value="", confidence=1.0,
                  query_context="user_correction")
    if not steward:
        from . import schema_check
        if not schema_check.is_onboarded_system(system_id):
            return {"error": f"system '{system_id}' is not an onboarded system"}
        try:
            result = record_user_proposal(**kwargs)
        except Exception as e:  # fail-loud surfaced as the plain-dict error style
            from .public_errors import public_error
            return public_error(e, "metric.retire_mapping")
        return {"status": result.get("status"), "mapping_id": result.get("mapping_id"),
                "concept": concept, "field": field}
    try:
        result = record_user_correction(**kwargs)
    except Exception as e:  # fail-loud surfaced as the plain-dict error style
        from .public_errors import public_error
        return public_error(e, "metric.retire_mapping")
    try:
        from . import dashboard
        dashboard.invalidate_dashboard_caches()
    except Exception:  # nosec B110 - best-effort cache bust, non-fatal
        logger.debug("invalidate_dashboard_caches after retirement failed (non-fatal)",
                     exc_info=True)
    return {"status": "accepted", "neptune_node_id": result.get("neptune_node_id"),
            "version": result.get("version"), "concept": concept, "field": field}


@router.post("/retire-mapping")
async def retire_mapping_route(body: RetireMappingRequest, request: Request) -> dict:
    """Retire one concept -> column link. Inherits AuthMiddleware; only a
    steward's call applies at once (see ``_retire_mapping_payload``)."""
    return await offload_resolver(_retire_mapping_payload, body, request)


# --- Metric-by-dimension route (Wave 2) -------------------------------------
# Row-shaped grouped payload (S5.3 §6.3). Thin wrapper over the grouped resolver
# core in ``agents.metric_dimension`` (imported lazily inside the handler to keep
# the load-time module graph acyclic — see the re-export note below). Inherits
# the app-wide AuthMiddleware; plain-dict errors (HTTP 200) like the other routes.


def _grouped_error(dimension_concept: str, msg: str, *,
                   structural: bool = False) -> dict:
    """Friendly row-shaped error envelope (HTTP 200) — mirrors the never-raise
    boundary style of ``resolve.py`` / the sections route. ``rows``/``columns``/
    ``lineage`` are empty so the bar renderer degrades gracefully.
    ``structural`` (key present only when True — honest absence) marks a
    data-model constraint no retry can fix; the UI drops Retry for it."""
    return {
        "kind": "bar",
        "status": "error",
        "error": msg,
        **({"structural": True} if structural else {}),
        "group_by": dimension_concept,
        "columns": [],
        "rows": [],
        "lineage": [],
    }


def _grouped_uncovered(dimension_concept: str, detail: str, missing: str | None) -> dict:
    """Row-shaped UNCOVERED envelope (HTTP 200): the metric/dimension is not
    mapped by the onboarded systems yet. Mirrors the KPI-card ``uncovered``
    status (ADR 0006 / the honest-data rule) so panels render needs-data
    guidance instead of error styling; ``detail`` keeps the technical cause
    for the expander."""
    return {
        "kind": "bar",
        "status": "uncovered",
        "missing_concepts": [missing] if missing else [],
        "detail": detail,
        "group_by": dimension_concept,
        "columns": [],
        "rows": [],
        "lineage": [],
    }


def _rejected_pin_error(body: "ResolveMetricByDimensionRequest") -> dict:
    """Envelope + warning log for a non-onboarded ``system_id`` pin (extracted
    from ``_resolve_by_dimension_payload`` for the 50-line gate)."""
    logger.warning(
        "resolve-by-dimension rejected non-onboarded system_id pin: "
        "system=%r metric=%r dimension=%r",
        body.system_id, body.metric_id, body.dimension_concept,
    )
    return _grouped_error(
        body.dimension_concept,
        f"unknown or not-onboarded system_id: {body.system_id!r}",
    )


def _resolve_by_dimension_payload(body: ResolveMetricByDimensionRequest) -> dict:
    """Sync body of ``POST /resolve-by-dimension`` (runs in a worker thread —
    see the EVENT-LOOP OFFLOAD PATTERN note above).

    2A pin seam: an optional ``system_id`` on the request pins authority to that
    ONE system (``PinnedAuthority`` through the grouped core's EXISTING
    ``authority=`` parameter) — enables per-site bars and the line-state
    per-machine drill (2C). Absent ⇒ today's single-authority behavior,
    byte-identical (the kwarg is not even passed).

    SECURITY (route boundary): ``system_id`` is USER-CONTROLLED. It is validated
    here against the onboarded catalog set BEFORE any resolver work — a pin
    naming a system that is not onboarded (unknown, offboarded, or graph-mapped
    but never cataloged) returns the structured ``_grouped_error`` envelope
    (HTTP 200, never an AttributeError/500 escaping from a ``catalog.get(...)
    -> None`` downstream)."""
    from . import metric_dimension as md
    try:
        if body.system_id:
            if body.system_id not in catalog.get_onboarded_ids():
                return _rejected_pin_error(body)
            grm = md.resolve_metric_by_dimension(
                body.metric_id, body.dimension_concept,
                authority=PinnedAuthority(body.system_id),
            )
        else:
            grm = md.resolve_metric_by_dimension(body.metric_id, body.dimension_concept)
    except CoverageGapError as e:
        # Mapping gap, not a fault (e.g. mid-onboarding): the honest
        # needs-data state, never error styling. INFO log — expected state.
        logger.info(
            "resolve-by-dimension uncovered: metric=%r dimension=%r missing=%r",
            body.metric_id, body.dimension_concept, e.concept,
        )
        return _grouped_uncovered(body.dimension_concept, str(e), e.concept)
    except (MetricResolverError, NotImplementedError, KeyError) as e:
        per_site = None if body.system_id else _grouped_per_site(body, e)
        if per_site is not None:
            return per_site
        logger.warning(
            "resolve-by-dimension failed: metric=%r dimension=%r",
            body.metric_id, body.dimension_concept, exc_info=True,
        )
        return _grouped_error(body.dimension_concept, str(e),
                              structural=isinstance(e, StructuralMetricError))
    return _grouped_ok(body.dimension_concept, grm)


def _grouped_per_site(body: "ResolveMetricByDimensionRequest", exc: BaseException) -> dict | None:
    """ADR-0094 for the grouped route: when the unpinned resolve failed on an
    authority ambiguity that is a PER-SITE PARTITION (one authority per plant,
    the normal state of a multi-plant deployment), resolve each site pinned and
    return one bar per (site, dimension value). ``None`` when the failure is
    anything else — a genuine conflict keeps the error envelope verbatim.

    Each site lands in its own register: ``ok`` with its rows, ``uncovered``
    (mapping gap on that plant) or ``error``. All sites uncovered is the honest
    uncovered envelope; no site answering at all keeps the original error."""
    from . import metric_dimension as md
    from .metric_turn_sites import ambiguous_authority_in, site_partition
    ambiguity = ambiguous_authority_in(exc)
    if ambiguity is None:
        return None
    partition = site_partition(ambiguity.system_ids, catalog.get_onboarded())
    if partition is None:
        return None
    sites, grouped = [], []
    for p in partition:
        try:
            grm = md.resolve_metric_by_dimension(
                body.metric_id, body.dimension_concept,
                authority=PinnedAuthority(p["system_id"]))
        except CoverageGapError as e:
            sites.append({**p, "status": "uncovered", "missing": e.concept})
        except UnreadableSystemError as e:  # its own fields, never its message
            sites.append({**p, "status": "error", "structural": True,
                          "detail": unreadable_system_reason(e.system_id,
                                                             e.connection_type)})
        except (MetricResolverError, NotImplementedError, KeyError) as e:
            logger.warning("resolve-by-dimension per-site failed: metric=%r site=%r (%s)",
                           body.metric_id, p["site"], e)
            sites.append({**p, "status": "error"})
        else:
            sites.append({**p, "status": "ok", "rows": len(grm.rows)})
            grouped.append((p["site"], grm))
    logger.info("resolve-by-dimension per-site: metric=%r dimension=%r sites=%s",
                body.metric_id, body.dimension_concept,
                [(s["site"], s["status"]) for s in sites])
    return _grouped_sites_envelope(body.dimension_concept, sites, grouped, exc)


def _grouped_sites_envelope(dim: str, sites: list[dict], grouped: list, exc) -> dict:
    """The per-site envelope: ``_grouped_ok``'s shape with ``system_id`` None,
    site-prefixed dimension values and a ``sites`` breakdown."""
    if not grouped:
        missing = [s.get("missing") for s in sites if s["status"] == "uncovered"]
        if len(missing) == len(sites):
            return _grouped_uncovered(dim, str(exc), missing[0] if missing else None)
        unreadable = [s["detail"] for s in sites if s.get("structural")]
        if len(unreadable) == len(sites):  # no site can be read: say why, not the split
            return _grouped_error(dim, "; ".join(unreadable), structural=True)
        return _grouped_error(dim, str(exc))
    from . import metric_dimension as md
    first = grouped[0][1]
    measure_concept = first.rows[0].lineage.concept if first.rows else None
    return {
        "metric_id": first.metric_id,
        "unit": first.unit,
        "label": first.label,
        "dimension_concept": dim,
        "system_id": None,
        "kind": "bar",
        "group_by": dim,
        "columns": [
            {"label": CONCEPT_NAME.get(dim, dim), "field_concept": dim},
            {"label": first.label, "field_concept": measure_concept},
        ],
        "rows": [[f"{site} · {r.dimension_value}", _grouped_row_cells(r)[1]]
                 for site, grm in grouped for r in grm.rows],
        "lineage": [md._row_lineage_to_dict(r.lineage)
                    for _site, grm in grouped for r in grm.rows],
        "sites": [{k: v for k, v in s.items() if v is not None or k != "missing"}
                  for s in sites],
        "status": "ok",
    }


def _cached_resolve_by_dimension_payload(body: ResolveMetricByDimensionRequest) -> dict:
    """Shared body of ``POST /resolve-by-dimension``.

    Three of the dashboard's grouped panels hit this route on every activation,
    so identical requests collapse to ONE resolve per window (bounded panel-read
    cache in ``dashboard``; lazy import keeps the module graph acyclic).

    The key carries EVERY field of the request model, with the optional
    ``system_id`` authority pin as-is — ``None`` (no pin) stays distinct from an
    empty string, so a pinned per-site answer can never be served for the global
    request. Error envelopes are not cached; ``uncovered`` is (a mapping gap is
    an honest, equally expensive answer, and the two invalidation paths below
    drop it the moment a mapping changes)."""
    from . import dashboard
    key = ("resolve-by-dimension", body.metric_id, body.dimension_concept,
           body.system_id)
    return dashboard.dashboard_read_cached(key, _resolve_by_dimension_payload, body)


def _grouped_ok(dim: str, grm) -> dict:
    """Success envelope for the by-dimension route: the row-shaped bar payload
    (columns/rows/per-row lineage) with status ok."""
    from . import metric_dimension as md
    measure_concept = grm.rows[0].lineage.concept if grm.rows else None
    return {
        "metric_id": grm.metric_id,
        "unit": grm.unit,
        "label": grm.label,
        "dimension_concept": dim,
        "system_id": grm.system_id,
        "kind": "bar",
        "group_by": dim,
        "columns": [
            {"label": CONCEPT_NAME.get(dim, dim), "field_concept": dim},
            {"label": grm.label, "field_concept": measure_concept},
        ],
        "rows": [_grouped_row_cells(r) for r in grm.rows],
        "lineage": [md._row_lineage_to_dict(r.lineage) for r in grm.rows],
        "status": "ok",
    }


def _grouped_row_cells(r) -> list:
    """One grouped result row as wire cells: a duration-PERCENTILE row
    carries ``[dimension_value, {percentiles:[{p,value}], sample_count}]``; a
    plain row stays ``[dimension_value, value]`` (byte-identical)."""
    pct = getattr(r, "percentiles", None)
    if pct is not None:
        return [r.dimension_value,
                {"percentiles": [{"p": p, "value": v} for p, v in pct],
                 "sample_count": r.sample_count}]
    return [r.dimension_value, r.value]


@router.post("/resolve-by-dimension")
async def resolve_metric_by_dimension_route(body: ResolveMetricByDimensionRequest) -> dict:
    """Resolve a direct metric GROUP BY a dimension concept → the row-shaped bar
    payload (S5.3 §6.3). Boundary errors return a friendly row-shaped error dict
    (HTTP 200) AND log server-side detail; the grouped core composes no payload.
    Thin wrapper, offloaded — EVENT-LOOP OFFLOAD PATTERN (full semantics on
    ``_resolve_by_dimension_payload``); SHARED READ via
    ``_cached_resolve_by_dimension_payload``."""
    return await offload_resolver(_cached_resolve_by_dimension_payload, body)


# --- Sensor-heatmap route (Wave 3) ------------------------------------------
# Thin wrapper over the heatmap resolver core in ``agents.heatmap_resolver``
# (imported lazily inside the handler to keep the load-time module graph acyclic).
# Inherits the app-wide AuthMiddleware; plain-dict friendly error (HTTP 200) +
# server-side logger.warning — mirrors resolve_metric_by_dimension_route.


def _heatmap_error(dims: list[str], msg: str) -> dict:
    """Friendly heatmap error envelope (HTTP 200) — empty rows/columns/lineage so
    the heatmap renderer degrades gracefully (fail-soft / panel hidden)."""
    return {
        "kind": "heatmap",
        "status": "error",
        "error": msg,
        "dims": dims,
        "threshold_status": "uncovered",
        "columns": [],
        "rows": [],
        "lineage": [],
    }


def _resolve_heatmap_payload(body: ResolveHeatmapRequest) -> dict:
    """Sync body of ``POST /resolve-heatmap`` (runs in a worker thread — see the
    EVENT-LOOP OFFLOAD PATTERN note above)."""
    from . import heatmap_resolver as hm
    dims = [body.dimension_concept_x, body.dimension_concept_y]
    try:
        return hm.resolve_heatmap(
            body.metric_id, body.dimension_concept_x, body.dimension_concept_y,
            body.threshold_concept,
        )
    except CoverageGapError as e:
        # Mapping gap (a dimension with no MAPS_TO field on the authority), not a
        # fault: the honest needs-data envelope, never error styling + Retry.
        logger.info("resolve-heatmap uncovered: metric=%r dims=%r missing=%r",
                    body.metric_id, dims, e.concept)
        return hm.heatmap_uncovered(body.metric_id, dims, body.threshold_concept,
                                    [e.concept] if e.concept else [])
    except (MetricResolverError, NotImplementedError, KeyError) as e:
        logger.warning(
            "resolve-heatmap failed: metric=%r dims=(%r,%r) threshold=%r",
            body.metric_id, body.dimension_concept_x, body.dimension_concept_y,
            body.threshold_concept, exc_info=True,
        )
        return _heatmap_error(dims, str(e))


def _cached_resolve_heatmap_payload(body: ResolveHeatmapRequest) -> dict:
    """Shared body of ``POST /resolve-heatmap`` — one resolve per distinct
    request per window. The key carries every field of the request model,
    including each dimension axis SEPARATELY (so swapping x/y is a different
    read) and the optional ``threshold_concept`` as-is (``None`` stays distinct
    from an empty string). Error envelopes are not cached."""
    from . import dashboard
    key = ("resolve-heatmap", body.metric_id, body.dimension_concept_x,
           body.dimension_concept_y, body.threshold_concept)
    return dashboard.dashboard_read_cached(key, _resolve_heatmap_payload, body)


@router.post("/resolve-heatmap")
async def resolve_heatmap_route(body: ResolveHeatmapRequest) -> dict:
    """Resolve a direct metric over two dimension concepts → the heatmap envelope
    (S5.3 §6.3). Boundary errors return a friendly heatmap error dict (HTTP 200)
    AND log server-side detail; the resolver core composes no payload on error.
    Thin wrapper, offloaded — EVENT-LOOP OFFLOAD PATTERN; SHARED READ via
    ``_cached_resolve_heatmap_payload``."""
    return await offload_resolver(_cached_resolve_heatmap_payload, body)


# --- Metric-by-dimension re-export (Wave 1) ---------------------------------
# The grouped resolver core lives in ``agents.metric_dimension`` (file-size
# discipline). It is re-exported here LAZILY (PEP 562 module ``__getattr__``) so
# the public import path ``agents.metric_resolver.resolve_metric_by_dimension``
# (and the grouped types) is stable, while avoiding a load-time circular import:
# ``metric_dimension`` imports this module by attribute (its monkeypatch-able
# ``neptune_query``/``query_pg``/``_utcnow_iso``/``get_metric_layer`` seams drive
# the grouped path), and this re-export is deferred until first access.

_GROUPED_EXPORTS = frozenset({
    "MAX_GROUPS",
    "GroupedQueryPlan",
    "GroupedResolvedMetric",
    "GroupedRow",
    "JoinPathProvenance",
    "RowLineage",
    "resolve_metric_by_dimension",
    "_compose_grouped_sql",
    "_compose_latest_grouped_sql",
    "_execute_grouped",
    "_resolve_dimension_mappings",
    "_resolve_order_column",
    "_row_lineage_to_dict",
    "_select_dimension_mapping",
    "_select_join_path",
})


def __getattr__(name: str):
    """Lazily re-export the grouped metric-by-dimension surface (PEP 562)."""
    if name in _GROUPED_EXPORTS:
        from . import metric_dimension
        return getattr(metric_dimension, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
