# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A DIRECT duration-measure metric binds through its pinned measure columns
when its OWN concept is unrecorded (no AuthorityRule, no MAPS_TO anywhere).

The lake-measure shape (neutral names): an UNBOUND metric concept; a time concept
bound on two systems with both pin columns present on only one table of one
system; a three-way-bound segment-type concept used with a column pin; a
segment-state concept; a dotted group-by column that renders as ONE quoted
identifier. Pins covered here:

* scalar and grouped resolution succeed with the composed SQL on the pinned
  table, through the measure pins when the metric concept maps nowhere;
* the honest selection basis/lineage (table from the pins, no fake concept
  column);
* zero pin tables raise a coverage gap; two (system, table) pairs fail loud
  naming both; an AuthorityRule for the metric concept still wins over the
  pins;
* a non-measure metric with an unbound concept stays a coverage gap.

Offline, graph/DB seams monkeypatched on ``metric_resolver`` (and
``metric_dimension`` for the grouped path)."""
from __future__ import annotations

import json

import pytest

import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp as Op
from agents.metric_measure_rule import DurationMeasure, FieldRef
from agents.metric_resolver import AuthorityResolver, resolve_metric

# --- the lake-measure shape, neutral names --------------------------------- #

MEASURE_CONCEPT = "cycle_time_pctl"       # the metric's OWN concept: UNBOUND
TIME = "event-time"                       # bound on two systems, pins on one table
SEG_STATE = "segment_state"               # one field on the measure table
SEG_TYPE = "manufacturing_segment"        # three-way bound; needs a column pin
MEDIA = "media_type"                      # dotted group-by column

SYS = "sys-lake"
OTHER_SYS = "sys-points"
TABLE = "segment_records"
START = "start_date"
END = "complete_date"
MEDIA_COL = "entity_type_details.product_data.ink_color"

_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, START),
    end=FieldRef(TIME, END), unit="hours", clamp_negative=True)

# MAPS_TO facts. The metric concept is deliberately ABSENT. ``event-time`` is
# bound on two systems; both pin columns are co-located on ONE table of SYS.
# ``OTHER_SYS`` maps the time concept on a different table with different field
# names, so it is NOT a pin table for (start_date, complete_date).
_BOUND = {
    TIME: [
        {"system": SYS, "table": TABLE, "column": START},
        {"system": SYS, "table": TABLE, "column": END},
        {"system": OTHER_SYS, "table": "points", "column": "header_timestamp"},
    ],
    SEG_STATE: [{"system": SYS, "table": TABLE, "column": "state"}],
    SEG_TYPE: [
        {"system": SYS, "table": TABLE, "column": "segment_type"},
        {"system": SYS, "table": TABLE, "column": "parent.segment_type"},
        {"system": SYS, "table": TABLE,
         "column": "entity_type_details.problem_solve_data.trigger.segment_type"},
    ],
    MEDIA: [{"system": SYS, "table": TABLE, "column": MEDIA_COL}],
}

_FILTERS = (
    Predicate(SEG_STATE, Op.EQ, ("COMPLETED",)),
    Predicate(SEG_TYPE, Op.EQ, ("PAPERBACK_ROUTE",), "segment_type"),
)

_PG_EXPR = (f'greatest(extract(epoch from ("{END}" - "{START}")), 0) / 3600.0')
_ATHENA_EXPR = (f'greatest(date_diff(\'second\', "{START}", "{END}"), 0) / 3600.0')
_NOTNULL = f'"{START}" IS NOT NULL AND "{END}" IS NOT NULL'
_MEDIA_IDENT = f'"{MEDIA_COL}"'

_PG_RESULT = json.dumps(
    {"columns": ["p", "p", "c"], "rows": [[61.8, 121.4, 2251968]], "row_count": 1})
_GROUPED_PG_RESULT = json.dumps({
    "columns": ["d", "p", "p", "c"],
    "rows": [["B&W", 50.0, 90.0, 10], ["Color", 70.0, 140.0, 8]],
    "row_count": 2})


class _NoAuthority(AuthorityResolver):
    """No AuthorityRule names a system for any concept (the unbound state)."""

    def authoritative_system(self, concept):  # noqa: ARG002
        return None


class _PinAuthority(AuthorityResolver):
    """An AuthorityRule names SYS for the metric concept (precedence test)."""

    def authoritative_system(self, concept):
        return SYS if concept == MEASURE_CONCEPT else None


def _metric(**over) -> DirectMetric:
    base = dict(id="cycle-tp", concept=MEASURE_CONCEPT,
                aggregation=Aggregation.COUNT, label="PB route cycle time",
                unit="hours", measure=_MEASURE, percentiles=(0.9, 0.99))
    base.update(over)
    return DirectMetric(**base)


def _rows_for(cypher: str, params: dict, bound: dict, removing=()) -> list:
    """Answer the resolver's graph reads from ``bound`` (a {concept: [facts]}).
    ``removing`` names systems whose removal has started; the pin read carries
    the removal marker for them (mirroring the real REMOVAL_STARTED_COLUMN)."""
    concept = params.get("concept")
    sid = params.get("sid")
    table = params.get("table")
    if cypher == mr._FIELD_TYPE_CYPHER:
        return [{"data_type": "timestamp(6) with time zone"}]
    # the metric-concept authority MAPS_TO read (single-system fallback):
    # Field-[:MAPS_TO]->Concept, returns f.system_id as sid.
    if "MAPS_TO" in cypher and "f.system_id" in cypher:
        facts = bound.get(concept, [])
        return [{"sid": f["system"]} for f in facts]
    # the measure-pin (system, table) read introduced by this fix.
    if cypher == mr._MEASURE_PIN_CYPHER:
        start_c, end_c = params["start_concept"], params["end_concept"]
        start_col, end_col = params["start_column"], params["end_column"]
        pairs = set()
        for s_fact in bound.get(start_c, []):
            if s_fact["column"] != start_col:
                continue
            for e_fact in bound.get(end_c, []):
                if (e_fact["column"] == end_col
                        and e_fact["system"] == s_fact["system"]
                        and e_fact["table"] == s_fact["table"]):
                    pairs.add((s_fact["system"], s_fact["table"]))
        return [{"system_id": s, "table": t,
                 "removal_incomplete": s in removing}
                for s, t in sorted(pairs)]
    # candidate binding reads (own concept, predicate, field-ref, group-by),
    # scoped to a system and optionally a table.
    if cypher in (mr._MEASURE_BINDING_CYPHER, mr._PREDICATE_BINDING_CYPHER):
        facts = [f for f in bound.get(concept, [])
                 if (sid is None or f["system"] == sid)
                 and (table is None or f["table"] == table)]
        pairs = sorted({(f["table"], f["column"]) for f in facts})
        return [{"table": t, "column": c} for t, c in pairs][
            :mb.BINDING_CANDIDATE_READ_LIMIT]
    return []


def _wire(monkeypatch, *, metric=None, bound=None, conn=None, authority=None,
          conn_type="PostgreSQL", removing=()):
    metric = metric or _metric()
    bound = _BOUND if bound is None else bound
    conn = conn or {SYS: conn_type, OTHER_SYS: conn_type}
    registry = {metric.id: metric}
    captured = {"sql": []}

    def _neptune(cypher, params=None):
        return _rows_for(cypher, params or {}, bound, removing=removing)

    def _pg(schema, sql, params=None):
        captured["sql"].append(sql)
        return captured.get("pg_result", _PG_RESULT)

    class _Layer:
        def get_metric(self, mid):
            return registry[mid]

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn.get(sid, conn_type))
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *a, **k: None)
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: END)
    return captured


def _resolve(mid="cycle-tp", authority=None):
    return resolve_metric(mid, authority=authority or _NoAuthority())


# --- scalar resolution through the pins ------------------------------------ #

def test_scalar_resolves_through_measure_pins(monkeypatch):
    metric = _metric(filter=_FILTERS)
    captured = _wire(monkeypatch, metric=metric)
    rm = _resolve()
    assert rm.system_id == SYS
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))
    assert rm.sample_count == 2251968
    assert rm.value is None and rm.status is None
    assert captured["sql"] == [
        f"SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"percentile_disc(0.99) WITHIN GROUP (ORDER BY {_PG_EXPR}), "
        f"COUNT({_PG_EXPR}) FROM plant.{TABLE} "
        f"WHERE {_NOTNULL} AND \"state\" = 'COMPLETED' "
        f"AND \"segment_type\" = 'PAPERBACK_ROUTE'"
    ]


def test_scalar_lineage_is_honest_about_the_basis(monkeypatch):
    _wire(monkeypatch)
    rm = _resolve()
    payload = mr._lineage_to_dict(rm.lineage)
    # the table came from the measure pins, not a fake concept column.
    assert payload["authority"]["system_id"] == SYS
    assert payload["authority"]["source"] == mr.BASIS_MEASURE_PINS
    assert payload["mapping"]["table"] == TABLE
    assert payload["mapping"]["binding_basis"] == mr.BASIS_MEASURE_PINS
    assert payload["mapping"]["column"] == ""
    assert payload["query_plan"]["table"] == TABLE
    assert payload["measure"]["start"]["column"] == START
    assert payload["measure"]["end"]["column"] == END
    # plan_lineage names the pinned duration columns, not an empty slot.
    assert mr.plan_lineage(rm.query_plan) == (
        f"{SYS}.{TABLE}.duration({START}, {END}) (count)")
    json.dumps(payload)


# --- grouped resolution through the pins ----------------------------------- #

def test_grouped_resolves_through_measure_pins(monkeypatch):
    metric = _metric(filter=_FILTERS,
                     group_by=FieldRef(MEDIA, MEDIA_COL))
    captured = _wire(monkeypatch, metric=metric)
    captured["pg_result"] = _GROUPED_PG_RESULT
    grm = md.resolve_metric_by_dimension(
        metric.id, MEDIA, authority=_NoAuthority(), dimension_column=MEDIA_COL)
    assert grm.system_id == SYS
    assert [r.dimension_value for r in grm.rows] == ["B&W", "Color"]
    assert grm.rows[0].percentiles == ((0.9, 50.0), (0.99, 90.0))
    sql = captured["sql"][0]
    assert f'SELECT {_MEDIA_IDENT}, ' in sql
    assert f'percentile_disc(0.9) WITHIN GROUP (ORDER BY {_PG_EXPR})' in sql
    assert f'FROM "plant"."{TABLE}"' in sql
    assert f'GROUP BY {_MEDIA_IDENT}' in sql
    assert "\"state\" = 'COMPLETED'" in sql
    assert "\"segment_type\" = 'PAPERBACK_ROUTE'" in sql


# --- fail-loud / precedence edges ------------------------------------------ #

def test_zero_pin_tables_is_a_coverage_gap(monkeypatch):
    # the end-column pin maps on a DIFFERENT table than the start-column pin,
    # so no (system, table) pair carries both.
    bound = {**_BOUND, TIME: [
        {"system": SYS, "table": TABLE, "column": START},
        {"system": SYS, "table": "other_table", "column": END},
    ]}
    _wire(monkeypatch, bound=bound)
    with pytest.raises(mr.CoverageGapError) as ei:
        _resolve()
    assert ei.value.concept == MEASURE_CONCEPT
    # the gap is reported by the measure-pin path (the time concept IS mapped,
    # so the old 'no onboarded system maps the concept' message does not apply).
    assert "measure" in str(ei.value).lower()


def test_two_pin_pairs_fail_loud_naming_both(monkeypatch):
    # both pin columns also co-locate on a SECOND system's table.
    bound = {**_BOUND, TIME: _BOUND[TIME] + [
        {"system": OTHER_SYS, "table": "mirror", "column": START},
        {"system": OTHER_SYS, "table": "mirror", "column": END},
    ]}
    _wire(monkeypatch, bound=bound)
    with pytest.raises(mr.MetricResolverError) as ei:
        _resolve()
    msg = str(ei.value)
    assert SYS in msg and OTHER_SYS in msg
    assert TABLE in msg and "mirror" in msg
    # the steward remediation names concept + system_id and NO field: the
    # metric concept has no MAPS_TO field, so a field-carrying rule is invalid.
    assert "concept" in msg and "system_id" in msg
    assert "{field}" not in msg and "field}" not in msg


def test_authority_rule_wins_over_the_pins(monkeypatch):
    # an AuthorityRule names SYS for the metric concept; but the concept is
    # still unbound there, so the measure-pin table resolution supplies the
    # table on the rule's system (no fake concept column).
    _wire(monkeypatch, authority=_PinAuthority())
    rm = _resolve(authority=_PinAuthority())
    payload = mr._lineage_to_dict(rm.lineage)
    assert payload["authority"]["system_id"] == SYS
    assert payload["authority"]["source"] == "authority_rule"
    assert payload["mapping"]["table"] == TABLE
    assert payload["mapping"]["binding_basis"] == mr.BASIS_MEASURE_PINS
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))


def test_non_measure_unbound_concept_stays_a_coverage_gap(monkeypatch):
    plain = DirectMetric(id="plain", concept=MEASURE_CONCEPT,
                         aggregation=Aggregation.SUM, label="Plain", unit="u")
    _wire(monkeypatch, metric=plain)
    with pytest.raises(mr.CoverageGapError) as ei:
        _resolve("plain")
    assert ei.value.concept == MEASURE_CONCEPT


# --- preview / dry-run path inherits the shared helper --------------------- #

def test_preview_resolve_definition_inherits_measure_pins(monkeypatch):
    # the dry-run preview hands a PENDING definition to resolve_definition with
    # an explicit layer; a measure metric whose concept is unbound must still
    # resolve through the measure pins.
    metric = _metric(filter=_FILTERS)
    _wire(monkeypatch, metric=metric)

    class _Layer:
        def get_metric(self, mid):
            return metric

    rm = mr.resolve_definition(metric, metric_id=metric.id,
                               authority=_NoAuthority(), layer=_Layer())
    assert rm.system_id == SYS
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))
    assert rm.lineage.authority.source == mr.BASIS_MEASURE_PINS


# --- propose-route validation accepts the definition ----------------------- #

def test_propose_validation_accepts_unbound_measure_concept():
    # a measure definition, in document form: the metric concept EXISTS in
    # the deployment vocabulary but is NOT bound to any column. The propose
    # route's validator (POST /api/admin/vocabulary/metrics/propose) must
    # accept it (concept membership only, binding not required).
    from agents.metric_definition import validate_definition
    from agents.metric_dimension import WINDOW_VOCAB

    doc = {
        "id": "pb-route-cycle-time-percentiles",
        "kind": "direct",
        "label": "PB route cycle time (lake measure)",
        "synonyms": ["TP90 and TP99 cycle time"],
        "unit": "hours",
        "concept": MEASURE_CONCEPT,
        "aggregation": "count",
        "measure": {"kind": "duration",
                    "start": {"concept": TIME, "column": START},
                    "end": {"concept": TIME, "column": END},
                    "unit": "hours", "clamp_negative": True},
        "percentiles": [0.9, 0.99],
        "filter": [{"concept": SEG_STATE, "column": "state", "op": "eq",
                    "value": "COMPLETED"},
                   {"concept": SEG_TYPE, "column": "segment_type", "op": "eq",
                    "value": "PAPERBACK_ROUTE"}],
        "group_by": {"concept": MEDIA, "column": MEDIA_COL},
    }
    known = {MEASURE_CONCEPT, TIME, SEG_STATE, SEG_TYPE, MEDIA}
    # no raise: concept must EXIST, need not be BOUND.
    validate_definition(doc, known_concepts=known, known_metric_ids=set(),
                        code_metric_ids=set(), window_vocab=WINDOW_VOCAB)


# --- removed/removing systems are not selected by pin discovery ------------ #

def test_only_a_removing_pin_pair_stays_a_coverage_gap(monkeypatch):
    # SYS is the only pin table, but its removal has started: pin discovery
    # must exclude it (same served/removal rule as _served_system_ids), so the
    # resolve is a coverage gap, never a resolve against a system going away.
    _wire(monkeypatch, removing={SYS})
    with pytest.raises(mr.CoverageGapError) as ei:
        _resolve()
    assert ei.value.concept == MEASURE_CONCEPT
    assert "measure" in str(ei.value).lower()


def test_one_served_plus_one_removing_pair_selects_the_served(monkeypatch):
    # both SYS and OTHER_SYS carry both pins on one table, but OTHER_SYS is
    # being removed: the served SYS is selected with no multi-pair ambiguity.
    bound = {**_BOUND, TIME: _BOUND[TIME] + [
        {"system": OTHER_SYS, "table": "mirror", "column": START},
        {"system": OTHER_SYS, "table": "mirror", "column": END},
    ]}
    _wire(monkeypatch, bound=bound, removing={OTHER_SYS})
    rm = _resolve()
    assert rm.system_id == SYS
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))


# --- Athena pin path (scalar + grouped) through the executor seam ---------- #

_DENIAL = ("Athena query failed: COLUMN_NOT_FOUND: line 1:28: Column {col} "
           "cannot be resolved or requester is not authorized to access "
           "requested resources")


def _athena_runner(capture, denied=None):
    def run(sql, connection_config):
        capture.append(sql)
        for col in (denied or []):
            if f'"{col}"' in sql:
                raise RuntimeError(_DENIAL.format(col=col))
        return {"columns": ["p", "p", "c"], "rows": [[61.8, 121.4, 2251968]],
                "row_count": 1}
    return run


def _athena_grouped_runner(capture, denied=None):
    def run(sql, connection_config):
        capture.append(sql)
        for col in (denied or []):
            if f'"{col}"' in sql:
                raise RuntimeError(_DENIAL.format(col=col))
        return {"columns": ["d", "p", "p", "c"],
                "rows": [["B&W", 50.0, 90.0, 10], ["Color", 70.0, 140.0, 8]],
                "row_count": 2}
    return run


def _athena_catalog(monkeypatch):
    """Point catalog.get at an Athena entry so the executor takes the Athena
    branch (``_execute_row`` / ``_execute_grouped`` read the catalog directly)."""
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config",
                        lambda sid: {"database": "plant"})


def test_scalar_athena_pin_path_sql(monkeypatch):
    metric = _metric(filter=_FILTERS)
    _wire(monkeypatch, metric=metric, conn_type="Athena")
    _athena_catalog(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(mr.athena_tools, "run_athena_query_exact",
                        _athena_runner(seen))
    rm = _resolve()
    assert rm.percentiles == ((0.9, 61.8), (0.99, 121.4))
    sql = seen[-1]
    assert (f"approx_percentile({_ATHENA_EXPR}, 0.9), "
            f"approx_percentile({_ATHENA_EXPR}, 0.99), "
            f"COUNT({_ATHENA_EXPR})") in sql
    assert f"FROM plant.{TABLE} WHERE {_NOTNULL}" in sql
    assert "\"state\" = 'COMPLETED'" in sql
    assert "\"segment_type\" = 'PAPERBACK_ROUTE'" in sql


@pytest.mark.parametrize("denied_col", [START, END])
def test_scalar_athena_pin_column_denial_is_clear(monkeypatch, denied_col):
    _wire(monkeypatch, conn_type="Athena")
    _athena_catalog(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(mr.athena_tools, "run_athena_query_exact",
                        _athena_runner(seen, denied=[denied_col]))
    with pytest.raises(mr.MetricResolverError) as ei:
        _resolve()
    msg = str(ei.value)
    assert "Lake Formation" in msg and f"'{denied_col}'" in msg
    assert "COLUMN_NOT_FOUND" not in msg


def test_grouped_athena_pin_path_sql(monkeypatch):
    metric = _metric(filter=_FILTERS, group_by=FieldRef(MEDIA, MEDIA_COL))
    _wire(monkeypatch, metric=metric, conn_type="Athena")
    _athena_catalog(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(mr.athena_tools, "run_athena_query_exact",
                        _athena_grouped_runner(seen))
    grm = md.resolve_metric_by_dimension(
        metric.id, MEDIA, authority=_NoAuthority(), dimension_column=MEDIA_COL,
        turn_window=mr.AbsoluteWindow("2026-08-20T00:00:00+00:00",
                                      "2026-08-21T00:00:00+00:00"))
    assert [r.dimension_value for r in grm.rows] == ["B&W", "Color"]
    sql = seen[-1]
    assert f"SELECT {_MEDIA_IDENT}, approx_percentile({_ATHENA_EXPR}, 0.9)" in sql
    assert f'FROM "plant"."{TABLE}"' in sql
    assert f"GROUP BY {_MEDIA_IDENT}" in sql
    assert "\"state\" = 'COMPLETED'" in sql
    assert "\"segment_type\" = 'PAPERBACK_ROUTE'" in sql
    # the absolute turn window lands on the measure END column.
    assert f'"{END}" >= ' in sql and f'"{END}" < ' in sql


@pytest.mark.parametrize("denied_col", [START, END])
def test_grouped_athena_pin_column_denial_is_clear(monkeypatch, denied_col):
    metric = _metric(filter=_FILTERS, group_by=FieldRef(MEDIA, MEDIA_COL))
    _wire(monkeypatch, metric=metric, conn_type="Athena")
    _athena_catalog(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(mr.athena_tools, "run_athena_query_exact",
                        _athena_grouped_runner(seen, denied=[denied_col]))
    with pytest.raises(mr.MetricResolverError) as ei:
        md.resolve_metric_by_dimension(
            metric.id, MEDIA, authority=_NoAuthority(), dimension_column=MEDIA_COL)
    msg = str(ei.value)
    assert "Lake Formation" in msg and f"'{denied_col}'" in msg
    assert "grouped query failed" not in msg


# --- turn-lineage text renders the pinned duration columns ----------------- #

def test_turn_lineage_text_names_the_duration_columns(monkeypatch):
    from agents import metric_turn_lineage as mtl
    _wire(monkeypatch)
    rm = _resolve()
    summary = mtl.lineage_summary(rm)
    plan_text = json.dumps(summary)
    # the lineage is the rendered definition, which names both pinned columns
    assert f"duration({START} -> {END})" in plan_text
    assert f"{TABLE}. (" not in plan_text


# --- essential columns: only a pin-bound plan changes from the base -------- #

def _essential_plan(*, scalar: bool, column: str, measure: bool):
    from types import SimpleNamespace
    dur = (SimpleNamespace(start=SimpleNamespace(column="start"),
                           end=SimpleNamespace(column="end")) if measure else None)
    if scalar:
        return SimpleNamespace(column=column, measure=dur)
    return SimpleNamespace(measure_column=column, dimension_column="group", measure=dur)


@pytest.mark.parametrize("scalar,column,measure,expected", [
    (True, "value", False, ("value",)),
    (False, "value", False, ("value", "group")),
    (True, "concept_value", True, ("concept_value",)),
    (False, "concept_value", True, ("concept_value", "group")),
    (True, "", True, ("start", "end")),
    (False, "", True, ("start", "end", "group")),
])
def test_essential_columns_change_only_for_a_pin_bound_plan(scalar, column, measure, expected):
    # a plan whose concept column is bound keeps the essential set the base
    # code passed; only a pin-bound plan (empty concept column) reads the pins.
    plan = _essential_plan(scalar=scalar, column=column, measure=measure)
    extra = () if scalar else (plan.dimension_column,)
    assert mr._athena_essential_columns(plan, *extra) == expected
