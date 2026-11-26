# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A computed metric-layer block says what it covers when the question names a
site, and a grouped block carries the definition a system agent can apply.

Measured: a grouped duration metric (no site filter, one table holding every
site) answered a question naming one site with the all-sites figure. The
grouped block said only ``scope: grouped by <dimension>`` and its lineage was
``grouped by <dimension>``, so it neither said it covers every site nor gave
the system agent the definition to apply at the named site.

Pinned here, one test per behaviour:

* the grouped block's scope is deployment-wide in the scalar block's own words
  and its lineage carries the metric's rendered formula and the grouping;
* a question naming a site whose authority system is unsited adds ONE line to
  the scalar and the grouped block: the value covers every site, and that
  site's figure is PERMITTED only through the system agent at that site;
* a named site that is the authority's own site, the per-site path, a derived
  formula and a question naming no site render byte-identically;
* the runtime sanitizer drops a malformed site list; a failure detecting sites
  adds no line.

Offline. The schema and sites are invented (``jobs``, ``Northfield``).
"""
from __future__ import annotations

import json

import pytest

import agents.calendar_policy as cp
import agents.metric_binding as mb
import agents.metric_definition as mdf
import agents.metric_dimension as md
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as render
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "job-duration"
TIME = "job-time"
SHADE = "job-shade"
KIND = "job-kind"
TABLE = "jobs"
_MEASURE = DurationMeasure(
    kind="duration", start=FieldRef(TIME, "began_at"),
    end=FieldRef(TIME, "finished_at"), unit="hours", clamp_negative=True)
_BOUND = {
    CONCEPT: [{"table": TABLE, "column": "job_id"}],
    TIME: [{"table": TABLE, "column": "began_at"},
           {"table": TABLE, "column": "finished_at"}],
    SHADE: [{"table": TABLE, "column": "shade_code"}],
    KIND: [{"table": TABLE, "column": "kind_code"}],
}
_FORMULA = ("p90, p99 of duration(began_at -> finished_at) in hours by shade_code "
            "where job-kind eq 'RUN'")
_POLICY = cp.CalendarPolicy(sites=(cp.SiteZone("Northfield", "UTC"),
                                   cp.SiteZone("Southport", "UTC")))
_QUESTION = "What is the TP90 job time at Northfield on 8/20 by shade?"


class _Auth(mr.AuthorityResolver):
    def authoritative_system(self, concept):  # noqa: ARG002
        return "sys-a"


def _metric(grouped: bool) -> DirectMetric:
    return DirectMetric(
        id="job-tp", concept=CONCEPT, aggregation=Aggregation.COUNT,
        label="Job Time", unit="hours", measure=_MEASURE, percentiles=(0.9, 0.99),
        filter=(Predicate(KIND, PredicateOp.EQ, ("RUN",)),),
        group_by=FieldRef(SHADE, "shade_code") if grouped else None)


def _neptune(cypher, params=None):
    params = params or {}
    if cypher == mr._FIELD_TYPE_CYPHER:
        return [{"data_type": "timestamp(6) with time zone"}]
    if cypher in (md._DIM_MAPS_TO_CYPHER, mr._MEASURE_BINDING_CYPHER,
                  mr._PREDICATE_BINDING_CYPHER):
        table = params.get("table")
        pairs = sorted({(r["table"], r["column"]) for r in _BOUND.get(params.get("concept"), [])
                        if table is None or r["table"] == table})
        return [{"table": t, "column": c} for t, c in pairs][:mb.BINDING_CANDIDATE_READ_LIMIT]
    return []


def _pg(schema, sql, params=None):
    if " GROUP BY " in sql:
        return json.dumps({"columns": ["g", "p", "p", "c"], "row_count": 2,
                           "rows": [["light", 10.0, 20.0, 100], ["dark", 30.0, 40.0, 50]]})
    return json.dumps({"columns": ["p", "p", "c"], "rows": [[18.0, 36.0, 150]], "row_count": 1})


def _wire(monkeypatch, *, grouped=True, plant="", policy=_POLICY):
    metric = _metric(grouped)
    layer = type("L", (), {"get_metric": staticmethod(lambda mid: {metric.id: metric}[mid])})()
    monkeypatch.setattr(mr, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(ml, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth())
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant_db")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: "PostgreSQL")
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {"id": sid, "plant": plant})
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: "began_at")
    monkeypatch.setattr(cp, "load_policy", lambda query=None: policy)
    monkeypatch.setattr(mtn, "_turn_window", lambda q: mr.AbsoluteWindow(
        start_utc="2026-08-20T00:00:00+00:00", end_utc="2026-08-21T00:00:00+00:00"))
    match = type("M", (), {"ambiguous": False, "metric_ids": ["job-tp"],
                           "term": "job time", "label": "Job Time"})()
    monkeypatch.setattr(mtn, "_matches_for_turn", lambda q: ([match], None))
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    mtn.invalidate()


def _notice(question=_QUESTION) -> str:
    mtn.invalidate()
    return render.render_metric_layer_notice(mtn.results_for_turn(question))


def _site_line(notice: str) -> list[str]:
    return [line for line in notice.splitlines() if "named in the question" in line]


# --------------------------------------------------------------------------- #
# the grouped block: deployment-wide scope, formula lineage
# --------------------------------------------------------------------------- #
def test_grouped_lineage_carries_the_formula_and_the_grouping(monkeypatch):
    """Without it: the lineage is ``grouped by <dimension>`` and the system
    agent has no definition to apply."""
    _wire(monkeypatch)
    result = mtn._resolve_one("job-tp")
    assert result["lineage"] == f"sys-a.{TABLE}: {_FORMULA}"


def test_grouped_scope_says_deployment_wide_in_the_scalar_words(monkeypatch):
    _wire(monkeypatch)
    notice = _notice(_NO_SITE)
    assert "scope: deployment-wide, grouped by job-shade; authority system 'sys-a'" in notice
    assert f"lineage: sys-a.{TABLE}: {_FORMULA}" in notice
    assert "deployment-wide scalar" not in notice


def test_rendered_metric_is_the_definition_formula():
    """One register: the served metric renders exactly as the definition it
    was built from."""
    doc = {"id": "job-tp", "kind": "direct", "label": "Job Time", "unit": "hours",
           "concept": CONCEPT, "aggregation": "count",
           "measure": {"kind": "duration",
                       "start": {"concept": TIME, "column": "began_at"},
                       "end": {"concept": TIME, "column": "finished_at"},
                       "unit": "hours", "clamp_negative": True},
           "percentiles": [0.9, 0.99],
           "group_by": {"concept": SHADE, "column": "shade_code"},
           "filter": [{"concept": KIND, "op": "eq", "value": "RUN"},
                      {"concept": SHADE, "op": "in", "values": ["light", "dark"]}]}
    assert mdf.render_metric(mdf.to_metric(doc)) == mdf.render_formula(doc)


# --------------------------------------------------------------------------- #
# a named site with an unsited authority adds the line (scalar and grouped)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_named_site_with_an_unsited_authority_adds_the_line(monkeypatch, grouped):
    """Without it: the all-sites figure is read as the named site's figure."""
    _wire(monkeypatch, grouped=grouped)
    lines = _site_line(_notice())
    assert lines == [render.NAMED_SITE_LINE.format(sites="Northfield")]
    assert "covers every site" in lines[0] and "PERMITTED only" in lines[0]


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_named_site_line_comes_after_the_permitted_line(monkeypatch, grouped):
    _wire(monkeypatch, grouped=grouped)
    notice = _notice()
    assert render.PERMITTED_LINE + "\n" + _site_line(notice)[0] in notice


# --------------------------------------------------------------------------- #
# byte-identical where no line applies
# --------------------------------------------------------------------------- #
_NO_SITE = "What is the TP90 job time on 8/20 by shade?"


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_no_named_site_renders_unchanged(monkeypatch, grouped):
    """A question naming no site renders exactly what it renders with no
    site detection at all (no policy)."""
    _wire(monkeypatch, grouped=grouped)
    plain = _notice(_NO_SITE)
    _wire(monkeypatch, grouped=grouped, policy=None)
    assert _notice(_NO_SITE) == plain and not _site_line(plain)


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_named_site_that_is_the_authority_site_renders_unchanged(monkeypatch, grouped):
    _wire(monkeypatch, grouped=grouped, plant="northfield")
    sited = _notice()
    _wire(monkeypatch, grouped=grouped, plant="northfield", policy=None)
    assert sited == _notice() and not _site_line(sited)


def test_per_site_and_formula_results_are_not_annotated():
    from agents import metric_turn_sites as sites
    per_site = {"outcome": sites.SITES_OUTCOME, "metric_id": "m", "sites": []}
    formula = {"outcome": "computed", "shape": "formula", "system_id": "sys-a"}
    withheld = {"outcome": "withheld", "status": "no-data"}
    assert sites.with_named_sites([per_site, formula, withheld], ["Northfield"]) == [
        per_site, formula, withheld]


def test_site_detection_failure_adds_no_line(monkeypatch):
    _wire(monkeypatch)

    def _boom(query=None):
        raise RuntimeError("graph down")

    monkeypatch.setattr(cp, "load_policy", _boom)
    assert not _site_line(_notice())


# --------------------------------------------------------------------------- #
# the runtime sanitizer
# --------------------------------------------------------------------------- #
def _computed(**extra) -> dict:
    return {"outcome": "computed", "term": "job time", "metric_id": "job-tp", "label": "Job",
            "source": "code", "value": 4.5, "unit": "hours", "kind": "direct",
            "shape": "direct", "system_id": "sys-a", "lineage": "sys-a.jobs.job_id (count)",
            **extra}


def test_sanitizer_keeps_a_well_formed_site_list():
    clean = render.sanitize_result(_computed(named_sites=["Northfield", "Southport"]))
    assert clean["named_sites"] == ["Northfield", "Southport"]
    block = render._render_computed(clean, "- 'job time'")
    assert render.NAMED_SITE_LINE.format(sites="Northfield, Southport") in block


@pytest.mark.parametrize("raw", [
    "Northfield", ["Northfield", 3], ["Northfield", ""], [], {"site": "Northfield"},
    ["s"] * (render.MAX_LIST_ITEMS + 1),
], ids=["string", "non-string", "blank", "empty", "dict", "oversize"])
def test_sanitizer_drops_a_malformed_site_list(raw):
    clean = render.sanitize_result(_computed(named_sites=raw))
    assert "named_sites" not in clean
    assert clean == render.sanitize_result(_computed())


def test_a_formula_block_never_renders_the_site_line():
    clean = render.sanitize_result(_computed(shape="formula", named_sites=["Northfield"]))
    assert "named in the question" not in render._render_computed(clean, "- 'job time'")
