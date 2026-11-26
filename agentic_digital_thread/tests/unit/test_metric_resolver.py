# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the S1.2 metric resolver (ADR 0015).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane. The graph (``neptune_query``/``neptune_write``)
and Postgres (``query_pg``) seams are monkeypatched with canned shapes; the
``AuthorityResolver`` is injected via the ``resolve_metric(authority=...)`` param
where convenient. ``query_pg`` is mocked to return a JSON **string** (matching
``agents.db.query_pg``) so the real ``json.loads`` parse path is exercised.
"""
from __future__ import annotations

import dataclasses
import json
from enum import Enum

import pytest

import agents.metric_resolver as mr

import pytest as _pytest_seed


@_pytest_seed.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Base mode ships an EMPTY live catalog (ADR-0030) and the demo-seed machinery
    has been DELETED from production (ADR-0035). These tests exercise demo systems
    (schemas) BY NAME, so temporarily seed the shared catalog from the test-local
    ``DEMO_SYSTEMS`` fixture, then restore the empty base-mode catalog afterward."""
    from agents.catalog import catalog as _cat
    try:
        from unit._demo_catalog import DEMO_SYSTEMS
    except ImportError:  # pragma: no cover - alternate invocation path
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from _demo_catalog import DEMO_SYSTEMS
    _cat._systems = [dict(s) for s in DEMO_SYSTEMS]
    try:
        yield
    finally:
        _cat._systems = []


from agents.metric_resolver import (
    AuthorityResolver,
    AuthoritySource,
    Lineage,
    MappingProvenance,
    MetricResolverError,
    QueryPlan,
    ResolvedMetric,
    _apply_op,
    _compose_sql,
    _eval_formula,
    _lineage_to_dict,
    resolve_metric,
    set_authority,
)
from agents.metric_layer import (
    Aggregation,
    CodeMetricLayer,
    ConceptRef,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricRef,
    Scalar,
    validate_metric_layer,
)


# --- Test doubles -----------------------------------------------------------

class _FakeAuthority(AuthorityResolver):
    """Injectable authority resolver returning a fixed system id (or None)."""

    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:
        return self._system_id


_PG_RESULT = json.dumps({"columns": ["sum"], "rows": [[18]], "row_count": 1})


def _maps_to_row(_cypher=None, _params=None):
    """Canned neptune_query MAPS_TO traversal row (table/column)."""
    return [{"table": "spare_parts", "column": "quantity_on_hand"}]


# --- Case 1: QueryPlan built correctly --------------------------------------

def test_query_plan_built_correctly(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    resolved = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    plan = resolved.query_plan
    assert isinstance(plan, QueryPlan)
    assert plan.system_id == "cmms"
    assert plan.schema == "cmms_maintenance"   # real catalog schema for cmms
    assert plan.table == "spare_parts"
    assert plan.column == "quantity_on_hand"
    assert plan.aggregation is Aggregation.SUM
    # full path returns the parsed scalar and threads the system id through
    assert resolved.value == 18
    assert resolved.system_id == "cmms"
    assert resolved.metric_id == "spare-parts-on-hand"


# --- Case 2: SELECT composition for each aggregation ------------------------

def test_compose_sql_for_each_aggregation():
    cases = {
        Aggregation.SUM: "SUM",
        Aggregation.AVG: "AVG",
        Aggregation.COUNT: "COUNT",
        Aggregation.MIN: "MIN",
        Aggregation.MAX: "MAX",
    }
    for agg, name in cases.items():
        plan = QueryPlan(
            system_id="cmms",
            schema="cmms_maintenance",
            table="spare_parts",
            column="quantity_on_hand",
            aggregation=agg,
        )
        assert _compose_sql(plan) == (
            f"SELECT {name}(quantity_on_hand), COUNT(quantity_on_hand) "  # nosec B608 - test fixture composing expected SQL from module constants
            f"FROM cmms_maintenance.spare_parts"
        )


# --- Case 3: authority fail-loud on ambiguity -------------------------------

def test_authority_fail_loud_two_systems(monkeypatch):
    # No AuthorityRule (fake returns None) → falls through to MAPS_TO scan, which
    # finds TWO systems mapping the concept → must fail loud naming set_authority.
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda cypher, params=None: [{"sid": "cmms"}, {"sid": "cmms2"}],
    )

    with pytest.raises(MetricResolverError) as excinfo:
        resolve_metric("spare-parts-on-hand", authority=_FakeAuthority(None))

    assert "set_authority" in str(excinfo.value)


# --- Case 4: single-system unambiguous path resolves ------------------------

def test_single_system_unambiguous_resolves(monkeypatch):
    def fake_nq(cypher, params=None):
        if "DISTINCT f.system_id" in cypher:      # _resolve_authority fallback
            return [{"sid": "cmms"}]
        if "se.name AS table" in cypher:          # _resolve_table_column
            return [{"table": "spare_parts", "column": "quantity_on_hand"}]
        return []

    monkeypatch.setattr(mr, "neptune_query", fake_nq)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    resolved = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority(None))

    assert resolved.system_id == "cmms"
    assert resolved.value == 18
    assert resolved.query_plan.table == "spare_parts"


# --- Case 5: derived metric evaluator (ADR 0016) ----------------------------
# The seed `oee` is now a DerivedMetric whose formula is a PRODUCT over three
# MetricRef inputs with a `prefer_stored="oee-score"` hybrid shortcut. These
# tests stay fully offline (no AWS/DB/graph/LLM) by mocking the *sub*-resolves
# with the Flag-4 canned-ResolvedMetric wrapper: it delegates to the REAL
# resolve_metric for "oee" (so the evaluator logic is genuinely exercised) and
# returns canned ResolvedMetrics for the sub-ids — raising MetricResolverError
# for any sub-id not provided (modelling an unmapped/unsatisfiable input).

def _patch_sub_resolves(monkeypatch, values, *, calls=None):
    """Patch ``mr.resolve_metric`` with a canned wrapper (plan Flag 4).

    - ``"oee"`` → delegate to the real ``resolve_metric`` (the function under test).
    - any sub-id in ``values`` → canned ``ResolvedMetric`` with that value.
    - any other sub-id → raise ``MetricResolverError`` (unsatisfiable input).
    - ``calls`` (optional list) records every sub-id resolve for spy assertions.
    """
    real_resolve = mr.resolve_metric

    def wrapper(metric_id, *, authority=None):
        if metric_id == "oee":
            return real_resolve(metric_id, authority=authority)
        if calls is not None:
            calls.append(metric_id)
        if metric_id not in values:
            raise MetricResolverError(
                f"no AuthorityRule for concept {metric_id!r} and 0 systems map it"
            )
        return ResolvedMetric(
            metric_id=metric_id, value=values[metric_id], unit="ratio",
            label=metric_id, query_plan=None, system_id="mes-mil",
        )

    monkeypatch.setattr(mr, "resolve_metric", wrapper)


# (a) derive product: stored unavailable → evaluate the normalized op-tree.
# Task 1A: the formula is SCALE(PRODUCT(A, P, SCALE(oee-quality, 0.01)), 100);
# with all-fractional mocks the inner ·0.01 and outer ·100 guards cancel, so the
# numeric result equals the plain A·P·Q product (byte-identical pin preserved).
def test_derived_oee_derives_product(monkeypatch):
    _patch_sub_resolves(monkeypatch, {
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    })  # oee-score absent → stored read raises → swallowed → derive

    resolved = mr.resolve_metric("oee")

    assert resolved.value == pytest.approx(0.8379, abs=1e-5)  # 0.9·0.95·0.98
    assert resolved.unit == "ratio"
    assert resolved.label == "Overall Equipment Effectiveness"
    assert resolved.metric_id == "oee"


# (a2) Task 1A acceptance: mixed-scale normalization. A and P resolve as 0-1
# ratios while the quality sub-reading (tracking quality-yield-rate) is a 0-100
# percentage — the documented guards make the DERIVED oee a true A×P×Q
# percentage on 0-100: 0.90 · 0.85 · (93.0/100) · 100 = 71.145.
def test_derived_oee_mixed_scales_yields_true_percentage(monkeypatch):
    _patch_sub_resolves(monkeypatch, {
        "availability": 0.90, "performance": 0.85, "oee-quality": 93.0,
    })  # oee-score absent → prefer_stored misses → derive

    resolved = mr.resolve_metric("oee")

    assert resolved.value == pytest.approx(71.1, abs=0.1)
    assert resolved.metric_id == "oee"
    assert resolved.system_id == "derived"


# (b) prefer_stored hit: oee-score resolves → stored wins, inputs NOT resolved.
def test_derived_oee_prefer_stored_hit_skips_inputs(monkeypatch):
    calls: list[str] = []
    _patch_sub_resolves(monkeypatch, {
        "oee-score": 0.85,
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    }, calls=calls)

    resolved = mr.resolve_metric("oee")

    assert resolved.value == 0.85
    assert resolved.metric_id == "oee"
    # the three formula inputs must NOT have been resolved (stored short-circuits)
    assert "availability" not in calls
    assert "performance" not in calls
    assert "oee-quality" not in calls
    assert calls.count("oee-score") == 1


# (c) prefer_stored miss → fall through to derive (same product as (a)).
def test_derived_oee_prefer_stored_miss_falls_through(monkeypatch):
    # oee-score absent → resolve raises MetricResolverError → swallowed → derive
    _patch_sub_resolves(monkeypatch, {
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    })

    resolved = mr.resolve_metric("oee")

    assert resolved.value == pytest.approx(0.8379, abs=1e-5)
    assert resolved.system_id == "derived"  # derived, not a stored system


# (d) unsatisfiable input → fail loud naming the input (no value / no zero).
def test_derived_oee_unsatisfiable_input_raises(monkeypatch):
    # oee-score absent (stored swallowed) AND availability absent → derive then
    # availability sub-resolve raises → propagates as a named 'derived metric unavailable'.
    _patch_sub_resolves(monkeypatch, {
        "performance": 0.95, "oee-quality": 0.98,
    })

    with pytest.raises(MetricResolverError) as excinfo:
        mr.resolve_metric("oee")

    msg = str(excinfo.value)
    assert "availability" in msg
    assert "derived metric unavailable" in msg


# (e) ratio divide-by-zero → raises (no inf/NaN).
def test_eval_ratio_divide_by_zero_raises():
    with pytest.raises(MetricResolverError, match="divide-by-zero"):
        _eval_formula(Formula(FormulaOp.RATIO, (Scalar(1.0), Scalar(0.0))), None)
    with pytest.raises(MetricResolverError, match="divide-by-zero"):
        _apply_op(FormulaOp.RATIO, [1.0, 0.0])


# (f) bare ConceptRef in a formula → raises (Decision A enforced at eval time).
def test_eval_conceptref_in_formula_raises():
    with pytest.raises(MetricResolverError) as excinfo:
        _eval_formula(ConceptRef("availability"), None)
    msg = str(excinfo.value)
    assert "bare concept" in msg
    assert "no aggregation" in msg


# (g) op numeric correctness (pure — no resolve).
def test_apply_op_numeric_correctness():
    assert _apply_op(FormulaOp.PRODUCT, [2, 3, 4]) == 24
    assert _apply_op(FormulaOp.SUM, [2, 3, 4]) == 9
    assert _apply_op(FormulaOp.RATIO, [10, 4]) == 2.5
    assert _apply_op(FormulaOp.SCALE, [5, 2]) == 10
    assert _apply_op(FormulaOp.WEIGHTED_SUM, [0.5, 10, 0.5, 20]) == 15

    # same ops via _eval_formula over Scalar / nested Formula literals
    assert _eval_formula(
        Formula(FormulaOp.PRODUCT, (Scalar(2.0), Scalar(3.0), Scalar(4.0))), None
    ) == 24
    assert _eval_formula(
        Formula(FormulaOp.SUM, (Scalar(2.0), Scalar(3.0), Scalar(4.0))), None
    ) == 9
    assert _eval_formula(
        Formula(FormulaOp.RATIO, (Scalar(10.0), Scalar(4.0))), None
    ) == 2.5
    assert _eval_formula(
        Formula(FormulaOp.SCALE, (Scalar(5.0), Scalar(2.0))), None
    ) == 10
    assert _eval_formula(
        Formula(
            FormulaOp.WEIGHTED_SUM,
            (Scalar(0.5), Scalar(10.0), Scalar(0.5), Scalar(20.0)),
        ),
        None,
    ) == 15


# (h) validation parity: the seed + evaluator stay consistent.
def test_validate_metric_layer_is_none():
    assert validate_metric_layer() is None


# --- Case 6: `latest` aggregation is deferred (NotImplementedError) ---------

def test_latest_aggregation_not_implemented(monkeypatch):
    latest_metric = DirectMetric(
        id="latest-on-hand", concept="mro-inventory",
        aggregation=Aggregation.LATEST, label="Latest", unit="units",
    )
    monkeypatch.setattr(
        mr, "get_metric_layer", lambda: CodeMetricLayer([latest_metric]),
    )
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row)

    with pytest.raises(NotImplementedError):
        resolve_metric("latest-on-hand", authority=_FakeAuthority("cmms"))


# --- Case 7: set_authority issues the expected writes -----------------------

def test_set_authority_issues_expected_writes(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def capture(cypher, params=None):
        calls.append((cypher, params or {}))
        return True

    monkeypatch.setattr(mr, "neptune_write", capture)

    ok = set_authority("mro-inventory", "cmms")
    assert ok is True
    assert len(calls) == 2

    # 1) AuthorityRule upsert with id ar-<sys>-<concept> + concept property.
    upsert_cypher, upsert_params = calls[0]
    assert "MERGE (ar:AuthorityRule {id: $arid})" in upsert_cypher
    assert "ar.concept = $concept" in upsert_cypher
    assert upsert_params["arid"] == "ar-cmms-mro-inventory"
    assert upsert_params["concept"] == "mro-inventory"
    assert upsert_params["sid"] == "cmms"

    # 2) APPLIES_TO edge to the System.
    edge_cypher, edge_params = calls[1]
    assert "APPLIES_TO" in edge_cypher
    assert edge_params["arid"] == "ar-cmms-mro-inventory"
    assert edge_params["sid"] == "cmms"


# --- Case 8: module import purity (ADR 0001) --------------------------------

def test_module_is_import_pure():
    # The deterministic resolver must not pull in any LLM/bedrock surface.
    assert not hasattr(mr, "boto3")
    assert not hasattr(mr, "bedrock_call")
    assert not hasattr(mr, "bedrock_stream")


# ============================================================================
# S2.2 — Lineage capture unit tests (ADR 0017 Decisions A-E).
#
# Offline + deterministic: the same monkeypatched graph/SQL seams + _FakeAuthority
# as the S2.1 cases above. The clock seam (mr._utcnow_iso) is monkeypatched for a
# stable executed_at. These tests are strictly ADDITIVE — every S2.1 test above is
# untouched, and _eval_formula remains a float shim so its direct-call assertions
# (test_apply_op_numeric_correctness, etc.) keep passing byte-identically (case g).
# ============================================================================

# Stable ISO clock value so executed_at is deterministic across the suite.
_FIXED_ISO = "2026-06-12T00:00:00+00:00"

# Composed SQL string the resolver builds for spare-parts-on-hand (cmms / SUM).
# 2A: the scalar compose pairs the aggregate with COUNT(col) (sample-count seam).
_SPARE_PARTS_SQL = ("SELECT SUM(quantity_on_hand), COUNT(quantity_on_hand) "
                    "FROM cmms_maintenance.spare_parts")


def _maps_to_row_full(_cypher=None, _params=None, *, version=None):
    """Canned MAPS_TO row carrying the S2.2 provenance keys (role/confidence/source).

    Mirrors the extended ``_resolve_table_column`` RETURN (r.role / r.confidence /
    r.method AS source / r.version AS version). ``source`` is None here (no r.method
    on MAPS_TO today) so the resolver coalesces it to ``'agent_proposed'``.

    ``version`` is the S3.1 Task 2.2 seam (r.version on the MAPS_TO edge): keyword-
    only and **defaulting to None → the ``version`` key is omitted entirely**, so
    every existing caller/assertion is byte-identical (back-compat). When set, the
    key is included so the resolver threads it into ``mapping.mapping_version``.
    """
    row = {
        "table": "spare_parts",
        "column": "quantity_on_hand",
        "role": "measure",
        "confidence": 0.91,
        "source": None,
    }
    if version is not None:
        row["version"] = version
    return [row]


def _iter_values(obj):
    """Yield every scalar value in a nested dict/list payload (for enum scans)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj


def _direct_sub_lineage(metric_id, *, concept=None, value=None):
    """Build a canned direct Lineage for a sub-metric (used by the derived tests)."""
    table = metric_id.replace("-", "_")
    return Lineage(
        metric_id=metric_id,
        kind="direct",
        concept=concept or metric_id,
        authority=AuthoritySource(system_id="mes-mil", source="authority_rule"),
        mapping=MappingProvenance(
            table=table, column="val", role="measure",
            confidence=0.9, source="agent_proposed",
        ),
        query_plan=QueryPlan(
            system_id="mes-mil", schema="mes_milwaukee",
            table=table, column="val", aggregation=Aggregation.AVG,
        ),
        sql=f"SELECT AVG(val) FROM mes_milwaukee.{table}",  # nosec B608 - test fixture composing expected SQL from module constants
        executed_at=_FIXED_ISO,
    )


def _patch_sub_resolves_with_lineage(monkeypatch, values, *, calls=None, lineages=None):
    """Like ``_patch_sub_resolves`` but each canned sub carries a direct Lineage.

    - ``"oee"`` → delegate to the REAL ``resolve_metric`` (function under test).
    - any sub-id in ``values`` → canned ``ResolvedMetric`` with that value AND a
      direct ``Lineage`` (from ``lineages[id]`` if provided, else generated).
    - any other sub-id → raise ``MetricResolverError`` (unsatisfiable input).
    """
    real_resolve = mr.resolve_metric
    lineages = lineages or {}

    def wrapper(metric_id, *, authority=None):
        if metric_id == "oee":
            return real_resolve(metric_id, authority=authority)
        if calls is not None:
            calls.append(metric_id)
        if metric_id not in values:
            raise MetricResolverError(
                f"no AuthorityRule for concept {metric_id!r} and 0 systems map it"
            )
        lin = lineages.get(metric_id) or _direct_sub_lineage(
            metric_id, value=values[metric_id]
        )
        return ResolvedMetric(
            metric_id=metric_id, value=values[metric_id], unit="ratio",
            label=metric_id, query_plan=None, system_id="mes-mil", lineage=lin,
        )

    monkeypatch.setattr(mr, "resolve_metric", wrapper)


# --- (a) DIRECT lineage shape -----------------------------------------------

def test_lineage_direct_shape(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row_full)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: _FIXED_ISO)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))
    lin = rm.lineage

    assert isinstance(lin, Lineage)
    assert lin.kind == "direct"
    assert lin.metric_id == "spare-parts-on-hand"
    assert lin.concept == "mro-inventory"
    # authority from the canned _FakeAuthority (AuthorityRule branch)
    assert lin.authority.system_id == "cmms"
    assert lin.authority.source == "authority_rule"
    # mapping table/column + role/confidence straight from the canned MAPS_TO row
    assert lin.mapping.table == "spare_parts"
    assert lin.mapping.column == "quantity_on_hand"
    assert lin.mapping.role == "measure"
    assert lin.mapping.confidence == 0.91
    # query_plan is the SAME QueryPlan object the resolver returned
    assert lin.query_plan is rm.query_plan
    assert lin.sql == _SPARE_PARTS_SQL
    # executed_at is an ISO string (stable via the patched clock seam)
    assert isinstance(lin.executed_at, str)
    assert lin.executed_at == _FIXED_ISO
    # direct lineage carries no derived fields
    assert lin.op is None
    assert lin.inputs == ()
    assert lin.prefer_stored_hit is False


# --- (d) SQL capture exact --------------------------------------------------

def test_lineage_direct_sql_exact(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row_full)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    # The captured SQL is byte-exact with the resolver-composed SELECT and with
    # _compose_sql over the same plan (schema/table/column from the canned data).
    assert rm.lineage.sql == _SPARE_PARTS_SQL
    assert rm.lineage.sql == _compose_sql(rm.query_plan)


# --- (e) source seam (r.method) + version seams -----------------------------

def test_lineage_mapping_source_user_confirmed(monkeypatch):
    def confirmed_row(_cypher=None, _params=None):
        return [{
            "table": "spare_parts", "column": "quantity_on_hand",
            "role": "measure", "confidence": 0.91, "source": "user_confirmed",
        }]

    monkeypatch.setattr(mr, "neptune_query", confirmed_row)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.lineage.mapping.source == "user_confirmed"
    # version seams default None until S3.1/S3.2 (no edge property exists today)
    assert rm.lineage.mapping.mapping_version is None
    assert rm.lineage.mapping.value_mapping_version is None


def test_lineage_mapping_source_defaults_agent_proposed(monkeypatch):
    def no_source_row(_cypher=None, _params=None):
        # No 'source' key at all (r.method absent) → coalesced to 'agent_proposed'.
        return [{
            "table": "spare_parts", "column": "quantity_on_hand",
            "role": "measure", "confidence": 0.91,
        }]

    monkeypatch.setattr(mr, "neptune_query", no_source_row)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.lineage.mapping.source == "agent_proposed"
    assert rm.lineage.mapping.mapping_version is None
    assert rm.lineage.mapping.value_mapping_version is None


# --- (e2) mapping_version read seam (S3.1 Task 2.2 / Task 3.2) ---------------
# The resolver now reads ``r.version`` off the MAPS_TO edge and threads it into
# ``lineage.mapping.mapping_version`` (additive; absent → None). ``value_mapping_version``
# has no resolve-time read site (FLAG a) and stays None.

def test_lineage_mapping_version_present(monkeypatch):
    # MAPS_TO row WITH r.version='2' → resolved mapping.mapping_version == '2'.
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda _cypher=None, _params=None: _maps_to_row_full(version="2"),
    )
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.lineage.mapping.mapping_version == "2"
    # no resolve-time value-translation read site (FLAG a) → stays None
    assert rm.lineage.mapping.value_mapping_version is None


def test_lineage_mapping_version_absent_backcompat(monkeypatch):
    # MAPS_TO row WITHOUT version (default helper) → mapping_version stays None.
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row_full)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.lineage.mapping.mapping_version is None
    assert rm.lineage.mapping.value_mapping_version is None


# --- (b) DERIVED recursive lineage ------------------------------------------

def test_lineage_derived_recursive_shape(monkeypatch):
    _patch_sub_resolves_with_lineage(monkeypatch, {
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    })  # oee-score absent → stored read raises → swallowed → derive

    rm = mr.resolve_metric("oee")
    lin = rm.lineage

    # Task 1A: the formula root is the outer presentation SCALE(product, 100);
    # the Scalar(100) has no data provenance so the root carries ONE input —
    # the PRODUCT node over A, P and the 0-1-normalized quality.
    assert lin.kind == "derived"
    assert lin.metric_id == "oee"
    assert lin.op == "scale"
    assert lin.prefer_stored_hit is False
    assert len(lin.inputs) == 1
    product = lin.inputs[0]
    assert product.kind == "derived"
    assert product.op == "product"
    assert len(product.inputs) == 3
    a_lin, p_lin, q_norm = product.inputs
    assert a_lin.kind == "direct" and a_lin.metric_id == "availability"
    assert p_lin.kind == "direct" and p_lin.metric_id == "performance"
    # the quality operand is the inner normalization guard SCALE(oee-quality, 0.01)
    assert q_norm.kind == "derived" and q_norm.op == "scale"
    assert len(q_norm.inputs) == 1
    assert q_norm.inputs[0].kind == "direct"
    assert q_norm.inputs[0].metric_id == "oee-quality"
    # the derive numeric path is unchanged (0.9 · 0.95 · 0.98 — guards cancel)
    assert rm.value == pytest.approx(0.8379, abs=1e-5)
    # derived top lineage has no direct-only fields
    assert lin.query_plan is None
    assert lin.sql is None


# --- (c) prefer_stored hit lineage ------------------------------------------

def test_lineage_prefer_stored_hit(monkeypatch):
    calls: list[str] = []
    oee_score_lineage = _direct_sub_lineage(
        "oee-score", concept="oee-score", value=0.85
    )
    _patch_sub_resolves_with_lineage(monkeypatch, {
        "oee-score": 0.85,
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    }, calls=calls, lineages={"oee-score": oee_score_lineage})

    rm = mr.resolve_metric("oee")
    lin = rm.lineage

    assert rm.value == 0.85
    assert lin.kind == "derived"
    assert lin.prefer_stored_hit is True
    # the stored direct lineage is carried verbatim as the single input
    assert lin.inputs == (oee_score_lineage,)
    # the three formula inputs were NOT resolved (stored short-circuits)
    assert "availability" not in calls
    assert "performance" not in calls
    assert "oee-quality" not in calls
    assert calls.count("oee-score") == 1


# --- (f) serialization (json.dumps + no enums survive) ----------------------

def test_lineage_to_dict_json_serializable_direct(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row_full)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))
    payload = _lineage_to_dict(rm.lineage)

    # round-trips through json without a custom encoder
    assert json.loads(json.dumps(payload)) == payload
    # the embedded aggregation enum was rendered to its plain str .value
    assert payload["query_plan"]["aggregation"] == "sum"
    assert type(payload["query_plan"]["aggregation"]) is str
    # no Enum instances survive anywhere in the tree
    assert not any(isinstance(v, Enum) for v in _iter_values(payload))


def test_lineage_to_dict_json_serializable_derived(monkeypatch):
    _patch_sub_resolves_with_lineage(monkeypatch, {
        "availability": 0.9, "performance": 0.95, "oee-quality": 0.98,
    })
    rm = mr.resolve_metric("oee")
    payload = _lineage_to_dict(rm.lineage)

    assert json.loads(json.dumps(payload)) == payload
    # op is a plain str (FormulaOp.value stamped at capture), not an enum —
    # the root op is the outer presentation "scale" (Task 1A normalized tree)
    assert payload["op"] == "scale"
    assert type(payload["op"]) is str
    # root SCALE carries the single PRODUCT node (Scalar(100) has no provenance)
    assert len(payload["inputs"]) == 1
    product = payload["inputs"][0]
    assert product["op"] == "product"
    assert len(product["inputs"]) == 3
    # the DIRECT leaves (availability, performance, and oee-quality inside the
    # inner normalization scale node) render their aggregation enum to .value
    a_child, p_child, q_norm = product["inputs"]
    assert a_child["query_plan"]["aggregation"] == "avg"
    assert p_child["query_plan"]["aggregation"] == "avg"
    assert q_norm["op"] == "scale"
    assert q_norm["inputs"][0]["query_plan"]["aggregation"] == "avg"
    assert not any(isinstance(v, Enum) for v in _iter_values(payload))


# --- (g) NO-REGRESSION sentinel ---------------------------------------------
# The existing S2.1 tests above (test_apply_op_numeric_correctness,
# test_eval_ratio_divide_by_zero_raises, test_eval_conceptref_in_formula_raises)
# call _eval_formula directly and assert float/raise behavior. This sentinel
# documents that the float-returning shim is preserved alongside lineage capture.

def test_eval_formula_shim_still_returns_float():
    result = _eval_formula(
        Formula(FormulaOp.PRODUCT, (Scalar(0.9), Scalar(0.95), Scalar(0.98))), None
    )
    assert isinstance(result, float)
    assert result == pytest.approx(0.8379, abs=1e-5)


# ============================================================================
# Metric-by-dimension resolver (Wave 1, ADR 0015/0016/0017): a NEW additive
# grouped path. The scalar resolve_metric/_compose_sql/_lineage_to_dict path is
# guarded byte-identical by the golden-regression tests below. Everything here
# is fully offline: graph reads are injected (query=...) or monkeypatched on the
# module's neptune_query, and query_pg is mocked to return a JSON *string*.
# ============================================================================

from agents.metric_resolver import (  # noqa: E402  (additive grouped surface)
    MAX_GROUPS,
    GroupedQueryPlan,
    GroupedResolvedMetric,
    GroupedRow,
    JoinPathProvenance,
    RowLineage,
    _compose_grouped_sql,
    _execute_grouped,
    _row_lineage_to_dict,
    _select_join_path,
    resolve_metric_by_dimension,
)

# Exact composed SQL strings the resolver must build (plan acceptance).
_COLOCATED_SQL = (
    'SELECT "line", AVG("pass_rate") '
    'FROM "mes_milwaukee"."quality_checks" '
    'GROUP BY "line" ORDER BY "line" LIMIT 200'
)
_JOINED_SQL = (
    'SELECT d."line", AVG(m."pass_rate") '
    'FROM "mes_milwaukee"."quality_checks" m '
    'JOIN "mes_milwaukee"."work_orders" d ON m."work_order_id" = d."work_order_id" '
    'GROUP BY d."line" ORDER BY d."line" LIMIT 200'
)

_GROUPED_PG = json.dumps({
    "columns": ["line", "avg"],
    "rows": [["Line1", 0.95], ["Line2", 0.9], ["Line3", 0.85]],
    "row_count": 3,
})


def _colocated_plan(agg=Aggregation.AVG):
    return GroupedQueryPlan(
        system_id="mes-mil", schema="mes_milwaukee",
        measure_table="quality_checks", measure_column="pass_rate",
        aggregation=agg,
        dimension_table="quality_checks", dimension_column="line",
        join=None,
    )


def _joined_plan(agg=Aggregation.AVG):
    join = JoinPathProvenance(
        join_id="jp-internal-mes-mil-quality_checks-work_order_id-work_orders",
        from_table="quality_checks", from_field="work_order_id",
        to_table="work_orders", to_field="work_order_id",
        transform="direct", method="foreign_key_match",
    )
    return GroupedQueryPlan(
        system_id="mes-mil", schema="mes_milwaukee",
        measure_table="quality_checks", measure_column="pass_rate",
        aggregation=agg,
        dimension_table="work_orders", dimension_column="line",
        join=join,
    )


# --- composed SQL: exact strings (co-located + joined) ----------------------

def test_compose_grouped_sql_colocated_exact():
    assert _compose_grouped_sql(_colocated_plan()) == _COLOCATED_SQL


def test_compose_grouped_sql_joined_exact():
    assert _compose_grouped_sql(_joined_plan()) == _JOINED_SQL


def test_compose_grouped_sql_row_cap_and_order_by():
    sql = _compose_grouped_sql(_colocated_plan())
    assert sql.endswith(f"ORDER BY \"line\" LIMIT {MAX_GROUPS}")
    assert MAX_GROUPS == 200
    # joined shape orders/limits on the aliased dimension column
    assert _joined_plan() and _compose_grouped_sql(_joined_plan()).endswith(
        f'GROUP BY d."line" ORDER BY d."line" LIMIT {MAX_GROUPS}'
    )


def test_compose_grouped_sql_latest_fail_loud_without_order_column():
    # LATEST with no order_column resolved → fail loud (MetricResolverError), never
    # a silent guess of a time/ordering column (3.1).
    with pytest.raises(MetricResolverError, match="time/ordering column"):
        _compose_grouped_sql(_colocated_plan(agg=Aggregation.LATEST))


# --- LATEST grouped SQL: exact DISTINCT ON strings (co-located + joined) -----
# The AVG/SUM strings above (_COLOCATED_SQL/_JOINED_SQL) MUST stay byte-identical;
# these pin the NEW LATEST shape only.

_COLOCATED_LATEST_SQL = (
    'SELECT DISTINCT ON ("line") "line", "pass_rate" '
    'FROM "mes_milwaukee"."quality_checks" '
    'ORDER BY "line", "check_time" DESC LIMIT 200'
)
_JOINED_LATEST_SQL = (
    'SELECT DISTINCT ON (d."line") d."line", m."pass_rate" '
    'FROM "mes_milwaukee"."quality_checks" m '
    'JOIN "mes_milwaukee"."work_orders" d ON m."work_order_id" = d."work_order_id" '
    'ORDER BY d."line", m."check_time" DESC LIMIT 200'
)


def _colocated_latest_plan():
    return dataclasses.replace(
        _colocated_plan(agg=Aggregation.LATEST), order_column="check_time")


def _joined_latest_plan():
    return dataclasses.replace(
        _joined_plan(agg=Aggregation.LATEST), order_column="check_time")


def test_compose_grouped_sql_latest_colocated_exact():
    assert _compose_grouped_sql(_colocated_latest_plan()) == _COLOCATED_LATEST_SQL


def test_compose_grouped_sql_latest_joined_exact():
    assert _compose_grouped_sql(_joined_latest_plan()) == _JOINED_LATEST_SQL


def test_compose_grouped_sql_latest_rejects_injection_order_column():
    bad = dataclasses.replace(_colocated_latest_plan(), order_column="ts; DROP TABLE x")
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


def test_compose_grouped_sql_avg_unchanged_after_latest_added():
    # Explicit no-regression: the AVG composed SQL is byte-identical to the golden
    # strings even though the LATEST branch now exists in _compose_grouped_sql.
    assert _compose_grouped_sql(_colocated_plan()) == _COLOCATED_SQL
    assert _compose_grouped_sql(_joined_plan()) == _JOINED_SQL


def test_execute_grouped_wraps_db_error_as_resolver_error(monkeypatch):
    """A DB failure in the grouped query (e.g. AVG over a non-numeric column when a
    concept is mis-mapped to a uuid/text field) must surface as MetricResolverError —
    so the /resolve-by-dimension route returns a friendly bar-error envelope and logs
    server-side detail, never a raw 500. Errors propagate; they are not swallowed."""
    from agents import metric_dimension as md

    monkeypatch.setattr(mr, "catalog", {"mes-mil": {"connection_type": "PostgreSQL"}})

    def _boom(_schema, _sql):
        raise RuntimeError("function avg(uuid) does not exist")

    monkeypatch.setattr(mr, "query_pg", _boom)
    with pytest.raises(MetricResolverError, match="grouped query failed"):
        md._execute_grouped(_colocated_plan(), _COLOCATED_SQL)


# --- identifier-injection rejection (validate_identifier mirrors entity_render)

def test_compose_grouped_sql_rejects_injection_dimension():
    bad = dataclasses.replace(_colocated_plan(), dimension_column="line; DROP TABLE x")
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


def test_compose_grouped_sql_rejects_injection_measure():
    bad = dataclasses.replace(_joined_plan(), measure_column='pass_rate" --')
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


def test_compose_grouped_sql_rejects_injection_join_field():
    plan = _joined_plan()
    bad_join = dataclasses.replace(plan.join, from_field="wo id")
    with pytest.raises(ValueError):
        _compose_grouped_sql(dataclasses.replace(plan, join=bad_join))


# --- join-path selection: either direction + schema-qualified normalization -

def test_select_join_path_canonical_orientation_resolves():
    # Edge stored in the CANONICAL onboard direction (from=FK/many side=measure,
    # to=PK/one side=dimension), schema-qualified. This is the SAFE many-to-one
    # shape: each measure row joins to exactly one dimension row. Selection must
    # normalize the schema-qualified names and keep the measure on the from side.
    edge = {
        "join_id": "jp1",
        "from_table": "mes_milwaukee.quality_checks", "from_field": "wo_fk",
        "to_table": "mes_milwaukee.work_orders", "to_field": "wo_pk",
        "transform": "direct", "method": "foreign_key_match",
    }
    jp = _select_join_path(
        "mes-mil", "quality_checks", "work_orders",
        query=lambda c, p=None: [edge],
    )
    assert isinstance(jp, JoinPathProvenance)
    assert jp.from_table == "quality_checks"   # measure side (FK/many)
    assert jp.from_field == "wo_fk"            # the measure-side join column
    assert jp.to_table == "work_orders"        # dimension side (PK/one)
    assert jp.to_field == "wo_pk"
    assert jp.join_id == "jp1"


def test_select_join_path_rejects_fan_out_orientation():
    # Edge stored with the measure table on the PK/one (``to``) side, i.e.
    # from=work_orders[dimension]/to=quality_checks[measure]. Under the onboard
    # convention (from=FK/many, to=PK/one) this is the FAN-OUT case: the measure
    # is on the PK side, so a many-to-one aggregate would double-count. The
    # resolver must FAIL LOUD instead of silently reversing the edge.
    edge = {
        "join_id": "jp1",
        "from_table": "mes_milwaukee.work_orders", "from_field": "wo_pk",
        "to_table": "mes_milwaukee.quality_checks", "to_field": "wo_fk",
        "transform": "direct", "method": "foreign_key_match",
    }
    with pytest.raises(MetricResolverError, match="fan out"):
        _select_join_path(
            "mes-mil", "quality_checks", "work_orders",
            query=lambda c, p=None: [edge],
        )


def test_select_join_path_colocated_returns_none():
    assert _select_join_path(
        "mes-mil", "quality_checks", "quality_checks",
        query=lambda c, p=None: [],
    ) is None


# --- join-path fail-loud: no path / ambiguous / multi-hop -------------------

def test_select_join_path_no_path_fails_loud():
    with pytest.raises(MetricResolverError, match="no JoinPath"):
        _select_join_path(
            "mes-mil", "quality_checks", "work_orders",
            query=lambda c, p=None: [],
        )


def test_select_join_path_exact_mirrors_collapse_only_to_fk_proven_orientation(caplog):
    canonical = {
        "join_id": "jp-internal-supply-system-item-suppliers",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    reverse = {
        "join_id": "jp-internal-supply-system-suppliers-item",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "value_overlap",
    }

    with caplog.at_level("WARNING", logger="agents.metric_dimension"):
        join = _select_join_path(
            "supply-system", "item_suppliers", "suppliers",
            query=lambda c, p=None: [reverse, canonical],
        )

    assert join.join_id == canonical["join_id"]
    assert join.from_table == "item_suppliers"
    assert join.to_table == "suppliers"
    assert any("metadata-proven mirrored JoinPaths" in record.message
               for record in caplog.records)


def test_select_join_path_mirror_does_not_collapse_on_a_column_name_guess():
    # A foreign_key_match edge whose basis is column_name_pattern is a bare
    # naming guess: no FK is declared and no data was checked. It must not be
    # treated as the metadata-proven orientation when it mirrors another edge,
    # so the mirror stays ambiguous and fails loud instead of executing a guess.
    guess = {
        "join_id": "guess",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
        "basis": "column_name_pattern",
    }
    reverse = {
        "join_id": "reverse",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "value_overlap",
    }
    with pytest.raises(MetricResolverError, match="ambiguous"):
        _select_join_path(
            "supply-system", "item_suppliers", "suppliers",
            query=lambda c, p=None: [guess, reverse],
        )


def test_select_join_path_guess_does_not_override_a_steward_confirmed_reverse():
    # The bad shape the review named: a steward-confirmed reverse edge (its own
    # method, not foreign_key_match) plus a NEW naming guess. The guess must not
    # win the collapse just because its legacy method is foreign_key_match while
    # its basis says the data was not checked.
    confirmed = {
        "join_id": "confirmed",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "user_confirmed",
    }
    guess = {
        "join_id": "guess",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
        "basis": "column_name_pattern",
    }
    with pytest.raises(MetricResolverError, match="ambiguous"):
        _select_join_path(
            "supply-system", "item_suppliers", "suppliers",
            query=lambda c, p=None: [confirmed, guess],
        )


def test_select_join_path_legacy_mirror_without_basis_still_collapses(caplog):
    # A row written before basis existed carries no basis. It keeps today's
    # behaviour: a single foreign_key_match edge is still the proven orientation.
    canonical = {
        "join_id": "canonical",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    reverse = {
        "join_id": "reverse",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "value_overlap",
    }
    with caplog.at_level("WARNING", logger="agents.metric_dimension"):
        join = _select_join_path(
            "supply-system", "item_suppliers", "suppliers",
            query=lambda c, p=None: [reverse, canonical],
        )
    assert join.join_id == "canonical"


def test_select_join_path_declared_fk_basis_still_proves_the_orientation(caplog):
    # A declared foreign key still proves the orientation of a mirror.
    canonical = {
        "join_id": "canonical",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
        "basis": "declared_foreign_key",
    }
    reverse = {
        "join_id": "reverse",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "value_overlap",
    }
    with caplog.at_level("WARNING", logger="agents.metric_dimension"):
        join = _select_join_path(
            "supply-system", "item_suppliers", "suppliers",
            query=lambda c, p=None: [reverse, canonical],
        )
    assert join.join_id == "canonical"


def test_select_join_path_unproven_exact_mirrors_fail_in_both_directions():
    forward = {
        "join_id": "forward",
        "from_table": "supply_data.item_suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    reverse = {
        "join_id": "reverse",
        "from_table": "supply_data.suppliers", "from_field": "supplier_id",
        "to_table": "supply_data.item_suppliers", "to_field": "supplier_id",
        "transform": "direct", "method": "foreign_key_match",
    }

    for measure_table, dimension_table in (
        ("item_suppliers", "suppliers"),
        ("suppliers", "item_suppliers"),
    ):
        with pytest.raises(MetricResolverError, match="ambiguous"):
            _select_join_path(
                "supply-system", measure_table, dimension_table,
                query=lambda c, p=None: [forward, reverse],
            )


def test_select_join_path_ambiguous_fails_loud():
    edge_a = {"join_id": "a", "from_table": "mes_milwaukee.quality_checks",
              "from_field": "work_order_id", "to_table": "mes_milwaukee.work_orders",
              "to_field": "work_order_id", "transform": "direct", "method": "fk"}
    edge_b = {"join_id": "b", "from_table": "mes_milwaukee.quality_checks",
              "from_field": "wo_id2", "to_table": "mes_milwaukee.work_orders",
              "to_field": "id2", "transform": "direct", "method": "fk"}
    with pytest.raises(MetricResolverError, match="ambiguous"):
        _select_join_path(
            "mes-mil", "quality_checks", "work_orders",
            query=lambda c, p=None: [edge_a, edge_b],
        )


def test_select_join_path_multi_hop_fails_loud():
    # quality_checks↔work_orders and work_orders↔shifts exist, but the request is
    # quality_checks↔shifts (only reachable via work_orders) → multi-hop deferred.
    edges = [
        {"join_id": "1", "from_table": "mes_milwaukee.quality_checks",
         "from_field": "work_order_id", "to_table": "mes_milwaukee.work_orders",
         "to_field": "work_order_id", "transform": "direct", "method": "fk"},
        {"join_id": "2", "from_table": "mes_milwaukee.work_orders",
         "from_field": "shift_id", "to_table": "mes_milwaukee.shifts",
         "to_field": "shift_id", "transform": "direct", "method": "fk"},
    ]
    with pytest.raises(MetricResolverError, match="multi-hop"):
        _select_join_path(
            "mes-mil", "quality_checks", "shifts",
            query=lambda c, p=None: edges,
        )


# --- full resolve: joined (quality-by-line) per-row lineage shape -----------

def _grouped_graph(measure_table="quality_checks", dim_rows=None, join_edge=None):
    """Build a canned neptune_query dispatcher for the grouped flow.

    Distinguishes the three reads by cypher content: JoinPath query, the scalar
    _resolve_table_column (``mr._MEASURE_BINDING_CYPHER``) measure read, and the _resolve_dimension_mappings
    (no LIMIT 1) read."""
    dim_rows = dim_rows if dim_rows is not None else [
        {"table": "work_orders", "column": "line", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
    ]
    measure_row = {"table": measure_table, "column": "pass_rate", "role": "measure",
                   "confidence": 0.9, "source": "foreign_key_match"}

    def nq(cypher, params=None):
        if "JoinPath" in cypher:
            return [join_edge] if join_edge else []
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return [measure_row]
        if "se.name AS table" in cypher:
            return dim_rows
        return []
    return nq


_JOIN_EDGE = {
    "join_id": "jp-internal-mes-mil-quality_checks-work_order_id-work_orders",
    "from_table": "mes_milwaukee.quality_checks", "from_field": "work_order_id",
    "to_table": "mes_milwaukee.work_orders", "to_field": "work_order_id",
    "transform": "direct", "method": "foreign_key_match",
}


def test_resolve_by_dimension_joined_full(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(join_edge=_JOIN_EDGE))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: "2026-06-17T00:00:00+00:00")

    result = resolve_metric_by_dimension(
        "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
    )

    assert isinstance(result, GroupedResolvedMetric)
    assert result.metric_id == "quality-rate"
    assert result.dimension_concept == "production-line"
    assert result.system_id == "mes-mil"
    assert result.unit == "ratio"
    # ordered rows, values coerced, dimension values preserved
    assert [r.dimension_value for r in result.rows] == ["Line1", "Line2", "Line3"]
    assert [r.value for r in result.rows] == [0.95, 0.9, 0.85]
    # query plan is the joined plan with the FK join attached
    plan = result.query_plan
    assert isinstance(plan, GroupedQueryPlan)
    assert plan.measure_table == "quality_checks"
    assert plan.dimension_table == "work_orders"
    assert plan.join is not None and plan.join.from_field == "work_order_id"
    # per-row lineage (ADR 0017): shared fields + that row's dimension_value
    row = result.rows[0]
    assert isinstance(row.lineage, RowLineage)
    lin = row.lineage
    assert lin.kind == "grouped"
    assert lin.metric_id == "quality-rate"
    assert lin.concept == "quality-metric"
    assert lin.dimension_concept == "production-line"
    assert lin.authority.system_id == "mes-mil"
    assert lin.measure_mapping.column == "pass_rate"
    assert lin.dimension_mapping.column == "line"
    assert lin.join_path is not None
    assert lin.join_path.from_field == "work_order_id"
    assert lin.query_plan is plan
    assert lin.sql == _JOINED_SQL
    assert lin.executed_at == "2026-06-17T00:00:00+00:00"
    assert lin.dimension_value == "Line1"
    # each row carries the SAME sql/executed_at but its OWN dimension_value
    assert {r.lineage.dimension_value for r in result.rows} == {"Line1", "Line2", "Line3"}
    assert all(r.lineage.sql == _JOINED_SQL for r in result.rows)


def test_resolve_by_dimension_colocated_full(monkeypatch):
    # dimension maps co-located on the measure table → no join, co-located SQL.
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(
        dim_rows=[{"table": "quality_checks", "column": "line", "role": "dimension",
                   "confidence": 1.0, "source": "user_confirmed"}],
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)

    result = resolve_metric_by_dimension(
        "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
    )

    assert result.query_plan.join is None
    assert result.rows[0].lineage.sql == _COLOCATED_SQL
    assert result.rows[0].lineage.join_path is None
    assert [r.value for r in result.rows] == [0.95, 0.9, 0.85]


# --- fail-loud: empty dimension mapping / derived metric --------------------

def test_resolve_by_dimension_empty_dimension_mapping_fails_loud(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(dim_rows=[]))
    with pytest.raises(MetricResolverError) as excinfo:
        resolve_metric_by_dimension(
            "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
        )
    msg = str(excinfo.value)
    assert "production-line" in msg
    assert "mes-mil" in msg


def test_resolve_by_dimension_derived_metric_fails_loud(monkeypatch):
    # `oee` is a DerivedMetric → metric-by-dimension is direct-only (deferred).
    with pytest.raises(MetricResolverError, match="derived"):
        resolve_metric_by_dimension(
            "oee", "production-line", authority=_FakeAuthority("mes-mil"),
        )


def test_resolve_by_dimension_unknown_metric_fails_loud():
    with pytest.raises(MetricResolverError, match="unknown metric"):
        resolve_metric_by_dimension(
            "nope-metric", "production-line", authority=_FakeAuthority("mes-mil"),
        )


def _latest_nq(dim_rows, time_rows, measure_table="quality_checks", join_edge=None):
    """neptune_query dispatcher for the LATEST resolve flow: distinguishes the
    measure read (``mr._MEASURE_BINDING_CYPHER``), the dimension read (concept=production-line), the
    time/order read (concept=event-time), and the JoinPath read."""
    measure_row = {"table": measure_table, "column": "pass_rate", "role": "measure",
                   "confidence": 0.9, "source": "fk"}

    def nq(cypher, params=None):
        p = params or {}
        if "JoinPath" in cypher:
            return [join_edge] if join_edge else []
        if p.get("concept") == "event-time":
            return time_rows
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return [measure_row]
        return dim_rows
    return nq


def test_resolve_by_dimension_latest_fail_loud_without_time_concept(monkeypatch):
    # LATEST metric but NO event-time field mapped on the measure table → fail loud
    # (correctly uncovered), never a silent ordering-column guess (3.1).
    latest_metric = DirectMetric(
        id="latest-quality", concept="quality-metric",
        aggregation=Aggregation.LATEST, label="Latest Q", unit="ratio",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([latest_metric]))
    monkeypatch.setattr(mr, "neptune_query", _latest_nq(
        dim_rows=[{"table": "quality_checks", "column": "line", "role": "dimension",
                   "confidence": 1.0, "source": "user_confirmed"}],
        time_rows=[],  # event-time unmapped
    ))
    with pytest.raises(MetricResolverError, match="event-time"):
        resolve_metric_by_dimension(
            "latest-quality", "production-line", authority=_FakeAuthority("mes-mil"),
        )


def test_resolve_by_dimension_latest_resolves_with_time_concept(monkeypatch):
    # LATEST metric WITH a co-located event-time mapping → composes the DISTINCT ON
    # SQL and resolves the latest value per group.
    latest_metric = DirectMetric(
        id="latest-quality", concept="quality-metric",
        aggregation=Aggregation.LATEST, label="Latest Q", unit="ratio",
    )
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([latest_metric]))
    monkeypatch.setattr(mr, "neptune_query", _latest_nq(
        dim_rows=[{"table": "quality_checks", "column": "line", "role": "dimension",
                   "confidence": 1.0, "source": "user_confirmed"}],
        time_rows=[{"table": "quality_checks", "column": "check_time", "role": "dimension",
                    "confidence": 1.0, "source": "user_confirmed"}],
    ))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)

    result = resolve_metric_by_dimension(
        "latest-quality", "production-line", authority=_FakeAuthority("mes-mil"),
    )
    assert result.query_plan.order_column == "check_time"
    assert result.query_plan.join is None
    assert result.rows[0].lineage.sql == _COLOCATED_LATEST_SQL
    assert [r.value for r in result.rows] == [0.95, 0.9, 0.85]


def test_resolve_by_dimension_no_join_path_fails_loud(monkeypatch):
    # dimension maps to a different table with NO connecting JoinPath → fail loud.
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(join_edge=None))
    with pytest.raises(MetricResolverError, match="no JoinPath"):
        resolve_metric_by_dimension(
            "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
        )


# --- FIX 1: fan-out orientation through the full resolve --------------------

def test_resolve_by_dimension_fan_out_orientation_fails_loud(monkeypatch):
    # The ONLY connecting JoinPath stores the measure (quality_checks) on the
    # PK/one (``to``) side and the dimension (work_orders) on the FK/many side.
    # A many-to-one aggregate over this orientation would fan out the measure →
    # the resolver must fail loud (no silent edge reversal / double-counting).
    fan_out_edge = {
        "join_id": "jp-fanout",
        "from_table": "mes_milwaukee.work_orders", "from_field": "quality_check_id",
        "to_table": "mes_milwaukee.quality_checks", "to_field": "quality_check_id",
        "transform": "direct", "method": "foreign_key_match",
    }
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(join_edge=fan_out_edge))
    with pytest.raises(MetricResolverError, match="fan out"):
        resolve_metric_by_dimension(
            "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
        )


# --- dimension selection: co-located ties resolve DETERMINISTICALLY ---------
# (identifier role > confidence > column name — see _prefer_colocated_mapping);
# joinable-TABLE ties still fail loud below.

def _grouped_nq(measure_row, dim_rows, edges):
    """Custom neptune_query dispatcher for dimension-tie tests (measure read is
    the measure binding read; dimension read returns ALL candidates; JoinPath read returns edges)."""
    def nq(cypher, params=None):
        if "JoinPath" in cypher:
            return edges
        if cypher == mr._MEASURE_BINDING_CYPHER:
            return [measure_row]
        if "se.name AS table" in cypher:
            return dim_rows
        return []
    return nq


def test_resolve_by_dimension_colocated_tie_resolves_deterministically(monkeypatch):
    # TWO dimension columns map on the SAME table as the measure (quality_checks).
    # The resolver no longer fails loud — it picks ONE deterministically
    # (identifier role > highest confidence > lexicographically smallest column;
    # here roles+confidence tie, so 'line' < 'station' wins) and records the
    # chosen column in the per-row lineage.
    measure_row = {"table": "quality_checks", "column": "pass_rate",
                   "role": "measure", "confidence": 0.9, "source": "fk"}
    dim_rows = [
        {"table": "quality_checks", "column": "station", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
        {"table": "quality_checks", "column": "line", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
    ]
    monkeypatch.setattr(mr, "neptune_query", _grouped_nq(measure_row, dim_rows, []))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)
    result = resolve_metric_by_dimension(
        "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
    )
    assert result.query_plan.dimension_column == "line"
    assert result.query_plan.join is None
    assert result.rows[0].lineage.dimension_mapping.column == "line"
    assert result.rows[0].lineage.sql == _COLOCATED_SQL


def test_resolve_by_dimension_colocated_identifier_role_wins(monkeypatch):
    # Same co-located tie, but ONE candidate's MAPS_TO role is 'identifier' —
    # it wins even though the other has higher confidence AND sorts first.
    measure_row = {"table": "quality_checks", "column": "pass_rate",
                   "role": "measure", "confidence": 0.9, "source": "fk"}
    dim_rows = [
        {"table": "quality_checks", "column": "aaa_status", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
        {"table": "quality_checks", "column": "line", "role": "identifier",
         "confidence": 0.5, "source": "agent_proposed"},
    ]
    monkeypatch.setattr(mr, "neptune_query", _grouped_nq(measure_row, dim_rows, []))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)
    result = resolve_metric_by_dimension(
        "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
    )
    assert result.query_plan.dimension_column == "line"
    assert result.rows[0].lineage.dimension_mapping.role == "identifier"


def test_resolve_by_dimension_ambiguous_joinable_tables_fails_loud(monkeypatch):
    # TWO dimension tables (work_orders, shifts), neither co-located with the
    # measure, BOTH single-hop joinable (canonical FK orientation) → table tie.
    measure_row = {"table": "quality_checks", "column": "pass_rate",
                   "role": "measure", "confidence": 0.9, "source": "fk"}
    dim_rows = [
        {"table": "work_orders", "column": "line", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
        {"table": "shifts", "column": "line", "role": "dimension",
         "confidence": 1.0, "source": "user_confirmed"},
    ]
    # measure (quality_checks) on the FK/from side for BOTH → both joinable.
    edges = [
        {"join_id": "j1", "from_table": "mes_milwaukee.quality_checks",
         "from_field": "work_order_id", "to_table": "mes_milwaukee.work_orders",
         "to_field": "work_order_id", "transform": "direct", "method": "fk"},
        {"join_id": "j2", "from_table": "mes_milwaukee.quality_checks",
         "from_field": "shift_id", "to_table": "mes_milwaukee.shifts",
         "to_field": "shift_id", "transform": "direct", "method": "fk"},
    ]
    monkeypatch.setattr(mr, "neptune_query", _grouped_nq(measure_row, dim_rows, edges))
    with pytest.raises(MetricResolverError, match="ambiguous"):
        resolve_metric_by_dimension(
            "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
        )


# --- FIX 4a: schema/table identifier-injection rejection --------------------

def test_compose_grouped_sql_rejects_injection_schema():
    bad = dataclasses.replace(_colocated_plan(), schema="mes; DROP TABLE x")
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


def test_compose_grouped_sql_rejects_injection_measure_table():
    bad = dataclasses.replace(_joined_plan(), measure_table='quality_checks" --')
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


def test_compose_grouped_sql_rejects_injection_dimension_table():
    bad = dataclasses.replace(_joined_plan(), dimension_table="work_orders); DROP")
    with pytest.raises(ValueError):
        _compose_grouped_sql(bad)


# --- FIX 4b: _execute_grouped row arity guard -------------------------------

def test_execute_grouped_row_arity_guard(monkeypatch):
    # A result row with <2 columns must raise MetricResolverError (not IndexError).
    monkeypatch.setattr(
        mr, "query_pg",
        lambda schema, sql, params=None: json.dumps(
            {"columns": ["line"], "rows": [["Line1"]], "row_count": 1}
        ),
    )
    plan = _colocated_plan()
    sql = _compose_grouped_sql(plan)
    with pytest.raises(MetricResolverError, match="<2 columns"):
        _execute_grouped(plan, sql)


# --- per-row lineage serializer (dict, JSON-safe, enum→.value) --------------

def test_row_lineage_to_dict_json_serializable(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _grouped_graph(join_edge=_JOIN_EDGE))
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _GROUPED_PG)
    result = resolve_metric_by_dimension(
        "quality-rate", "production-line", authority=_FakeAuthority("mes-mil"),
    )
    payload = _row_lineage_to_dict(result.rows[0].lineage)

    assert json.loads(json.dumps(payload)) == payload  # round-trips, no enums
    assert payload["kind"] == "grouped"
    assert payload["dimension_value"] == "Line1"
    assert payload["query_plan"]["aggregation"] == "avg"
    assert type(payload["query_plan"]["aggregation"]) is str
    assert payload["join_path"]["from_field"] == "work_order_id"
    assert payload["measure_mapping"]["column"] == "pass_rate"
    assert payload["dimension_mapping"]["column"] == "line"
    assert not any(isinstance(v, Enum) for v in _iter_values(payload))


def test_row_lineage_to_dict_none_safe():
    assert _row_lineage_to_dict(None) is None


# ============================================================================
# SCALAR-PATH GOLDEN REGRESSION — the scalar resolve_metric/_compose_sql/
# _lineage_to_dict outputs MUST stay byte-identical after the additive grouped
# path. These pin today's exact shapes so a future grouped change cannot silently
# mutate the scalar /api/metrics/resolve payload.
# ============================================================================

def test_golden_scalar_compose_sql_byte_identical():
    plan = QueryPlan(
        system_id="cmms", schema="cmms_maintenance",
        table="spare_parts", column="quantity_on_hand", aggregation=Aggregation.SUM,
    )
    # byte-exact, UNQUOTED scalar SELECT (no double-quoting on the scalar path).
    # 2A deliberate re-pin: the aggregate is PAIRED with COUNT(col) in the same
    # single query (the sample-count honest-data seam).
    assert _compose_sql(plan) == ("SELECT SUM(quantity_on_hand), "
                                  "COUNT(quantity_on_hand) "
                                  "FROM cmms_maintenance.spare_parts")


def test_golden_scalar_lineage_dict_keys_unchanged(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row_full)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: _FIXED_ISO)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))
    payload = _lineage_to_dict(rm.lineage)

    # exact top-level key set of the scalar Lineage serializer (unchanged)
    assert set(payload.keys()) == {
        "metric_id", "kind", "concept", "authority", "mapping", "query_plan",
        "sql", "executed_at", "op", "inputs", "prefer_stored_hit",
    }
    assert set(payload["query_plan"].keys()) == {
        "system_id", "schema", "table", "column", "aggregation",
    }
    # ADR-0076 D1: DELIBERATE one-key re-pin — ``binding_basis`` says WHY this
    # column on every resolve (never stripped for the ``single`` case).
    assert set(payload["mapping"].keys()) == {
        "table", "column", "role", "confidence", "source",
        "mapping_version", "value_mapping_version", "binding_basis",
    }
    assert payload["mapping"]["binding_basis"] == "single"
    assert payload["kind"] == "direct"
    assert payload["query_plan"]["aggregation"] == "sum"
    assert payload["sql"] == _SPARE_PARTS_SQL


# ============================================================================
# 2A — sample-count honesty (paired COUNT(col)) + by-dimension system_id pin.
# ============================================================================

_PG_RESULT_WITH_COUNT = json.dumps(
    {"columns": ["sum", "count"], "rows": [[18, 7]], "row_count": 1})


def test_resolve_metric_carries_sample_count_from_paired_count(monkeypatch):
    # A two-cell result row (aggregate, COUNT(col)) → sample_count threaded onto
    # the ResolvedMetric (7 non-null measure rows behind the value 18).
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row)
    monkeypatch.setattr(mr, "query_pg",
                        lambda schema, sql, params=None: _PG_RESULT_WITH_COUNT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.value == 18
    assert rm.sample_count == 7


def test_resolve_metric_sample_count_none_when_single_cell(monkeypatch):
    # A legacy/mocked single-cell row reports NO sample count (auxiliary field,
    # never fatal) — the default keeps old shapes byte-identical.
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row)
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: _PG_RESULT)

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.value == 18
    assert rm.sample_count is None


def test_resolve_metric_null_aggregate_zero_count(monkeypatch):
    # 0-row AVG: SQL returns (NULL, 0) — value None, sample_count 0. The resolver
    # does NOT invent a value; classification to 'no-data' happens in the card
    # pipeline (dashboard._build_card_metric).
    monkeypatch.setattr(mr, "neptune_query", _maps_to_row)
    monkeypatch.setattr(
        mr, "query_pg",
        lambda schema, sql, params=None: json.dumps(
            {"columns": ["sum", "count"], "rows": [[None, 0]], "row_count": 1}),
    )

    rm = resolve_metric("spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert rm.value is None
    assert rm.sample_count == 0


def test_coerce_count_is_fail_soft():
    assert mr._coerce_count(7) == 7
    assert mr._coerce_count("7") == 7
    assert mr._coerce_count(7.0) == 7
    assert mr._coerce_count(None) is None
    assert mr._coerce_count("not-a-number") is None
    assert mr._coerce_count(True) is None   # bools are not counts


def test_execute_shim_still_returns_first_scalar(monkeypatch):
    # Back-compat: mr._execute keeps its scalar contract (first cell coerced).
    monkeypatch.setattr(mr, "query_pg",
                        lambda schema, sql, params=None: _PG_RESULT_WITH_COUNT)
    plan = QueryPlan(system_id="cmms", schema="cmms_maintenance",
                     table="spare_parts", column="quantity_on_hand",
                     aggregation=Aggregation.SUM)
    assert mr._execute(plan, _SPARE_PARTS_SQL) == 18


# --- PinnedAuthority + the by-dimension route's optional system_id pin -------

def test_pinned_authority_returns_supplied_system_for_any_concept():
    pin = mr.PinnedAuthority("sys-a")
    assert pin.authoritative_system("any-concept") == "sys-a"
    assert pin.authoritative_system("another") == "sys-a"


def test_by_dimension_route_pins_authority_when_system_id_present(monkeypatch):
    import asyncio
    from agents import metric_dimension as md
    from agents.models import ResolveMetricByDimensionRequest

    seen = {}

    def _capture(mid, dim, **kwargs):
        seen["mid"], seen["dim"] = mid, dim
        seen["authority"] = kwargs.get("authority")
        raise MetricResolverError("stop here — capture only")

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _capture)
    # Route boundary (fix-pass): the pin must name an ONBOARDED system.
    monkeypatch.setattr(mr.catalog, "get_onboarded_ids", lambda: {"sys-b"})
    body = ResolveMetricByDimensionRequest(
        metric_id="m", dimension_concept="d", system_id="sys-b")
    asyncio.run(mr.resolve_metric_by_dimension_route(body))

    assert isinstance(seen["authority"], mr.PinnedAuthority)
    assert seen["authority"].authoritative_system("anything") == "sys-b"


@pytest.mark.parametrize("bad_id", ["nope", "erp'; DROP", "graph-only-system"])
def test_by_dimension_route_rejects_non_onboarded_system_id(monkeypatch, bad_id):
    # SECURITY (fix-pass): a user-controlled pin naming a system that is NOT in
    # the onboarded catalog set — unknown, offboarded, or graph-mapped but never
    # cataloged — is rejected AT THE ROUTE BOUNDARY with the structured
    # _grouped_error envelope (HTTP 200): the resolver core is never invoked,
    # so no catalog.get(...)->None AttributeError/500 can escape.
    import asyncio
    import json as _json
    from agents import metric_dimension as md
    from agents.models import ResolveMetricByDimensionRequest

    def _never(*_a, **_k):
        raise AssertionError("resolver core must not run for a rejected pin")

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _never)
    monkeypatch.setattr(mr.catalog, "get_onboarded_ids", lambda: {"sys-b"})
    body = ResolveMetricByDimensionRequest(
        metric_id="m", dimension_concept="d", system_id=bad_id)
    result = asyncio.run(mr.resolve_metric_by_dimension_route(body))

    # The exact structured error envelope (never a raised exception/500).
    assert result["status"] == "error"
    assert result["kind"] == "bar"
    assert result["group_by"] == "d"
    assert result["rows"] == [] and result["columns"] == [] and result["lineage"] == []
    assert "not-onboarded" in result["error"] or "unknown" in result["error"]
    _json.dumps(result)


def test_by_dimension_route_omits_authority_when_no_system_id(monkeypatch):
    # Absent system_id ⇒ byte-identical call: the authority kwarg is NOT passed
    # at all, and NO catalog validation read happens (regression pin for
    # today's single-authority behavior).
    import asyncio
    from agents import metric_dimension as md
    from agents.models import ResolveMetricByDimensionRequest

    seen = {}

    def _capture(mid, dim, **kwargs):
        seen["kwargs"] = kwargs
        raise MetricResolverError("stop here — capture only")

    monkeypatch.setattr(md, "resolve_metric_by_dimension", _capture)

    def _no_catalog_read():
        raise AssertionError("no catalog validation read without a pin")

    monkeypatch.setattr(mr.catalog, "get_onboarded_ids", _no_catalog_read)
    body = ResolveMetricByDimensionRequest(metric_id="m", dimension_concept="d")
    asyncio.run(mr.resolve_metric_by_dimension_route(body))

    assert seen["kwargs"] == {}


def test_by_dimension_request_system_id_defaults_none():
    from agents.models import ResolveMetricByDimensionRequest
    body = ResolveMetricByDimensionRequest(metric_id="m", dimension_concept="d")
    assert body.system_id is None
