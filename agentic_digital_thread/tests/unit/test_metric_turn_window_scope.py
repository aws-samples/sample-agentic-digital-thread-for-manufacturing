# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metric resolved for an explicit turn window says that window and carries
a definition a system agent can apply, on every computed path.

The review of the explicit-window fix found four gaps, pinned here:

* a scalar duration result bounded by the turn's ``[start, end)`` printed
  ``window: lifetime (no window declared)``: it now carries the applied range,
  as a grouped result does (a non-measure scalar never takes a turn window);
* the ADR-0094 per-site path resolved each site WITHOUT the turn window, so
  each site query read the recent-days bound instead of the asked day: the
  window now reaches every pinned site resolve, each site entry carries the
  applied range, and a scan refusal is that site's ``not_computed_in_time``;
* a scalar duration lineage named only ``system.table.duration(a, b)
  (count)``, without the percentiles or the filter, yet the named-site line
  called it "this exact lineage": it now carries the rendered definition (a
  non-measure lineage is byte-identical);
* a definition longer than the notice's lineage bound fell back silently to
  ``grouped by <dimension>``: it is now carried up to that bound, marked as
  cut, and the named-site line then gives no permission.

Offline. The schema and sites are invented (``jobs``, ``East`` / ``West``).
"""
from __future__ import annotations

import json

import pytest

import agents.athena_tools as at
import agents.calendar_policy as cp
import agents.catalog as catalog_mod
import agents.metric_binding as mb
import agents.metric_dimension as md
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_turn_lineage as mtl
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as render
import agents.metric_turn_sites as mts
from agents import connection_types as ct
from agents.metric_layer import Aggregation, DirectMetric, Predicate, PredicateOp
from agents.metric_measure_rule import DurationMeasure, FieldRef

CONCEPT = "job-duration"
TIME = "job-time"
SHADE = "job-shade"
KIND = "job-kind"
TABLE = "jobs"
_CFG = {"workgroup": "wg", "database": "lake_db"}
_PART = "\"day_part\" >= DATE '2026-10-10'"
_INTERVAL = "current_timestamp - INTERVAL"
_START, _END = "2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00"
_APPLIED = f"window: applied window [{_START}, {_END})"
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
_RUN = (Predicate(KIND, PredicateOp.EQ, ("RUN",)),)
_LONG = (Predicate(KIND, PredicateOp.IN, tuple(f"kind-value-{c * 20}" for c in "abcdef")),)
_SCALAR_FORMULA = "p90, p99 of duration(began_at -> finished_at) in hours where job-kind eq 'RUN'"
_SITES = [{"id": "sys-west", "plant": "West", "status": "onboarded"},
          {"id": "sys-east", "plant": "East", "status": "onboarded"}]
_POLICY = cp.CalendarPolicy(sites=(cp.SiteZone("Northfield", "UTC"),))


def _win():
    return mr.AbsoluteWindow(start_utc=_START, end_utc=_END)


def _metric(*, grouped=False, measure=True, flt=_RUN, window=None) -> DirectMetric:
    if not measure:
        return DirectMetric(id="job-tp", concept=CONCEPT, aggregation=Aggregation.SUM,
                            label="Job Count", unit="jobs", filter=flt, window=window)
    return DirectMetric(
        id="job-tp", concept=CONCEPT, aggregation=Aggregation.COUNT, label="Job Time",
        unit="hours", measure=_MEASURE, percentiles=(0.9, 0.99), filter=flt, window=window,
        group_by=FieldRef(SHADE, "shade_code") if grouped else None)


class _Auth(mr.AuthorityResolver):
    def __init__(self, ambiguous=False):
        self.ambiguous = ambiguous

    def authoritative_system(self, concept):
        if self.ambiguous:
            raise mr.AmbiguousAuthorityError("ambiguous", concept=concept,
                                             system_ids=("sys-east", "sys-west"))
        return "sys-a"


class _Connector:
    def __init__(self, refuse=False):
        self.refuse, self.calls = refuse, []

    def __call__(self, sql, connection_config, **_kwargs):
        self.calls.append(sql)
        if self.refuse:
            error = at.AthenaEngineScanLimitError("over", limit_source="engine")
            error.exact_split = {"applied": False, "reason": "shape"}
            raise error
        if " GROUP BY " in sql:
            return {"columns": ["g", "p", "p", "c"], "row_count": 1,
                    "rows": [["light", "5.0", "9.0", "20"]]}
        return {"columns": ["p", "p", "c"], "rows": [["5.0", "9.0", "30"]], "row_count": 1}


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
        return json.dumps({"columns": ["g", "p", "p", "c"], "row_count": 1,
                           "rows": [["light", 10.0, 20.0, 100]]})
    return json.dumps({"columns": ["p", "p", "c"], "rows": [[18.0, 36.0, 150]], "row_count": 1})


def _wire(monkeypatch, metric, *, athena=False, refuse=False, ambiguous=False, plant=""):
    layer = type("L", (), {"get_metric": staticmethod(lambda mid: {metric.id: metric}[mid])})()
    connector = _Connector(refuse)
    conn = ct.ATHENA if athena else "PostgreSQL"
    monkeypatch.setattr(mr, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(ml, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(mr, "get_authority_resolver", lambda: _Auth(ambiguous))
    monkeypatch.setattr(mr, "neptune_query", _neptune)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn)
    monkeypatch.setattr(mr, "query_pg", _pg)
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {"id": sid, "connection_type": conn,
                                                        "plant": plant})
    monkeypatch.setattr(catalog_mod.catalog, "get_onboarded", lambda: list(_SITES))
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: (
        f"{k.get('qualifier', '')}{_PART}"))
    monkeypatch.setattr(md, "_resolve_order_column", lambda sid, t, query=None: "began_at")
    monkeypatch.setattr(at, "run_athena_query_exact", connector, raising=False)
    monkeypatch.setattr(at, "run_athena_query", connector)
    monkeypatch.setattr(cp, "load_policy", lambda query=None: _POLICY)
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    mtn.invalidate()
    return connector


def _block(result: dict) -> str:
    clean = render.sanitize_result({**result, "term": "job time", "metric_id": "job-tp",
                                    "label": "Job Time"})
    return render.render_metric_layer_notice([clean])


def _notice_for(monkeypatch, question="What is the TP90 job time at Northfield on 8/20?"):
    match = type("M", (), {"ambiguous": False, "metric_ids": ["job-tp"],
                           "term": "job time", "label": "Job Time"})()
    monkeypatch.setattr(mtn, "_matches_for_turn", lambda q: ([match], None))
    monkeypatch.setattr(mtn, "_turn_window", lambda q: _win())
    mtn.invalidate()
    return render.render_metric_layer_notice(mtn.results_for_turn(question))


# --------------------------------------------------------------------------- #
# 1. a scalar result bounded by the turn window says that window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("athena", [False, True], ids=["postgres", "athena"])
def test_scalar_measure_result_carries_the_applied_range(monkeypatch, athena):
    """Without it: the day's value is labelled lifetime."""
    _wire(monkeypatch, _metric(), athena=athena)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["window_applied_range"] == {"start": _START, "end": _END}
    notice = _block(result)
    assert _APPLIED in notice
    assert "lifetime" not in notice


def test_scalar_measure_with_a_declared_token_says_the_turn_window(monkeypatch):
    """A declared token the turn window overrode is not reported as the
    token applied as lifetime."""
    _wire(monkeypatch, _metric(window="24h"))
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert _APPLIED in notice and "applied as lifetime" not in notice


def test_scalar_without_a_window_and_non_measure_scalar_carry_no_range(monkeypatch):
    """No window: unchanged. A non-measure scalar takes no turn window today
    (``resolve_metric`` passes it to measure metrics only)."""
    _wire(monkeypatch, _metric())
    assert "window_applied_range" not in mtn._resolve_one("job-tp")
    _wire(monkeypatch, _metric(measure=False))
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert "window_applied_range" not in result
    assert "lifetime (no window declared)" in _block(result)


def test_sanitizer_keeps_a_scalar_range_and_drops_a_malformed_one():
    base = {"outcome": "computed", "term": "t", "metric_id": "m", "label": "L", "value": 1.0,
            "unit": "h", "system_id": "sys-a", "lineage": "sys-a.jobs.job_id (count)"}
    clean = render.sanitize_result({**base, "window_applied_range": {"start": _START,
                                                                     "end": _END}})
    assert clean["window_applied_range"] == {"start": _START, "end": _END}
    bad = render.sanitize_result({**base, "window_applied_range": {"start": "x;DROP",
                                                                   "end": _END}})
    assert "window_applied_range" not in bad


# --------------------------------------------------------------------------- #
# 2. the per-site path keeps the turn window
# --------------------------------------------------------------------------- #
def test_per_site_queries_stay_inside_the_turn_window(monkeypatch):
    """Without it: each site query reads the recent-days bound, not the day."""
    connector = _wire(monkeypatch, _metric(), athena=True, ambiguous=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["outcome"] == mts.SITES_OUTCOME
    assert len(connector.calls) == 2
    for sql in connector.calls:
        assert _START in sql and _END in sql
        assert _INTERVAL not in sql and "day_part" not in sql
    for site in result["sites"]:
        assert site["outcome"] == "computed"
        assert site["window_applied_range"] == {"start": _START, "end": _END}
    notice = _block(result)
    assert _APPLIED in notice and "lifetime" not in notice


def test_per_site_scan_refusal_is_not_computed_in_time(monkeypatch):
    connector = _wire(monkeypatch, _metric(), athena=True, ambiguous=True, refuse=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert [s["outcome"] for s in result["sites"]] == [mtn.NOT_IN_TIME_OUTCOME] * 2
    assert [s["system_id"] for s in result["sites"]] == ["sys-east", "sys-west"]
    assert len(connector.calls) == 2
    assert all(_START in sql and _INTERVAL not in sql for sql in connector.calls)


def test_per_site_without_a_window_is_unchanged(monkeypatch):
    connector = _wire(monkeypatch, _metric(), athena=True, ambiguous=True)
    result = mtn._resolve_one("job-tp")
    assert all("day_part" in sql for sql in connector.calls)
    assert all("window_applied_range" not in s for s in result["sites"])


# --------------------------------------------------------------------------- #
# 3. a scalar duration lineage carries the rendered definition
# --------------------------------------------------------------------------- #
def test_scalar_measure_lineage_is_the_rendered_definition(monkeypatch):
    """Without it: ``sys-a.jobs.duration(began_at, finished_at) (count)``,
    no percentiles and no filter."""
    _wire(monkeypatch, _metric())
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["lineage"] == f"sys-a.{TABLE}: {_SCALAR_FORMULA}"
    assert "lineage_incomplete" not in result


def test_per_site_measure_lineage_is_the_rendered_definition(monkeypatch):
    _wire(monkeypatch, _metric(), athena=True, ambiguous=True)
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert f"lineage: <site system>.{TABLE}: {_SCALAR_FORMULA}" in notice


def test_non_measure_scalar_lineage_is_unchanged(monkeypatch):
    _wire(monkeypatch, _metric(measure=False, flt=None))
    result = mtn._resolve_one("job-tp")
    assert result["lineage"] == "sys-a.jobs.job_id (sum)"
    assert "lineage_incomplete" not in result


def test_filtered_non_measure_scalar_gives_no_named_site_permission(monkeypatch):
    """Its lineage (unchanged) omits the filter, so it is not an exact
    lineage to apply at a site."""
    _wire(monkeypatch, _metric(measure=False))
    notice = _notice_for(monkeypatch)
    assert "lineage: sys-a.jobs.job_id (sum)" in notice
    assert render.NAMED_SITE_UNSCOPED_LINE.format(sites="Northfield") in notice
    assert render.NAMED_SITE_LINE.format(sites="Northfield") not in notice


# --------------------------------------------------------------------------- #
# 4. a long definition is carried up to the bound, never silently replaced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_a_long_definition_within_the_bound_is_carried_whole(monkeypatch, grouped):
    flt = (Predicate(KIND, PredicateOp.IN, ("alpha-long-value", "bravo-long-value",
                                            "charlie-long-value", "delta-long-value",
                                            "echo-long-value")),)
    _wire(monkeypatch, _metric(grouped=grouped, flt=flt))
    lineage = mtn._resolve_one("job-tp", turn_window=_win())["lineage"]
    assert 160 < len(lineage) <= render.MAX_DETAIL_CHARS
    assert lineage.startswith(f"sys-a.{TABLE}: p90, p99 of duration")
    assert lineage.endswith("'echo-long-value')")
    notice = _notice_for(monkeypatch)
    assert render.NAMED_SITE_LINE.format(sites="Northfield") in notice


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_a_definition_over_the_bound_is_cut_and_gives_no_permission(monkeypatch, grouped):
    """Without it: the grouped lineage is ``grouped by job-shade`` and the
    named-site line still permits applying "this exact lineage"."""
    _wire(monkeypatch, _metric(grouped=grouped, flt=_LONG))
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["lineage"].startswith(f"sys-a.{TABLE}: p90, p99 of duration")
    assert result["lineage"].endswith(mtl.CUT_MARKER)
    assert len(result["lineage"]) <= render.MAX_DETAIL_CHARS
    assert result["lineage_incomplete"] is True
    notice = _notice_for(monkeypatch)
    assert render.NAMED_SITE_UNSCOPED_LINE.format(sites="Northfield") in notice
    assert render.NAMED_SITE_LINE.format(sites="Northfield") not in notice


def test_sanitizer_marks_a_lineage_it_had_to_cut():
    base = {"outcome": "computed", "term": "t", "metric_id": "m", "label": "L", "value": 1.0,
            "unit": "h", "system_id": "sys-a", "named_sites": ["Northfield"]}
    long = render.sanitize_result({**base, "lineage": "x" * (render.MAX_DETAIL_CHARS + 1)})
    assert long["lineage_incomplete"] is True
    assert "PERMITTED" not in _block(long).split("Site(s) named")[1].splitlines()[0]
    whole = render.sanitize_result({**base, "lineage": "x" * render.MAX_DETAIL_CHARS})
    assert "lineage_incomplete" not in whole
