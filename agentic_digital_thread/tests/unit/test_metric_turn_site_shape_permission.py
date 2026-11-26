# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Round 2 of the explicit-window review: what a per-site or incomplete block
may say, pinned one test per finding.

* HIGH: the per-site path rendered a duration metric as one unlabelled value
  (the highest percentile) and dropped a definition's grouping. Each site now
  lists its labelled percentiles, marked approximate on Athena; a grouped
  definition is not resolved per site at all, and (round 3) not delegated
  either: this path does not check its bindings at each site, so each site
  gives no number and no one-query permission.
* MED: an incomplete lineage still carried the generic scoped-query
  permission, on the scalar/grouped block and on the per-site block.
* MED: a lineage ending in the cut marker was trusted as whole when the
  ``lineage_incomplete`` flag did not arrive.
* LOW: a filtered non-duration result with no window and no named site
  gained a ``lineage_incomplete`` key; its payload is byte-identical again,
  and the flag is set only where a named site makes it matter.

Offline. The schema and sites are invented (``jobs``, ``East`` / ``West``).
"""
from __future__ import annotations

import json

import agents.metric_resolver as mr
import agents.metric_turn_lineage as mtl
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as render

from .test_metric_turn_window_scope import (
    _END, _LONG, _START, TABLE, _metric, _notice_for, _pg, _win, _wire,
)

_RANGE = {"start": _START, "end": _END}
_DEFINITION = "p90, p99 of duration(began_at -> finished_at) in hours"
_NO_SITE = "What is the job count on 8/20?"


def _block(result: dict) -> str:
    clean = render.sanitize_result({**result, "term": "job time", "metric_id": "job-tp",
                                    "label": "Job Time"})
    return render.render_metric_layer_notice([clean])


# --------------------------------------------------------------------------- #
# HIGH: per-site percentiles are labelled; a grouped definition is handed over
# --------------------------------------------------------------------------- #
def test_per_site_percentiles_are_labelled_and_approximate(monkeypatch):
    """Without it: ``East (sys-east): 9 hours | samples: 30``."""
    _wire(monkeypatch, _metric(), athena=True, ambiguous=True)
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert ("  East (sys-east): percentiles: p90=5 hours, p99=9 hours (approximate: Athena "
            "approx_percentile) | samples: 30") in notice
    assert "(sys-east): 9 hours" not in notice
    assert notice.endswith(render.APPROXIMATE_SCOPE_SENTENCE)


def test_per_site_grouped_definition_is_not_computed_and_not_delegated(monkeypatch):
    """Without it: the grouping is dropped and each site shows one number.
    Round 3: the per-site path does not validate a grouped definition's field
    bindings at each site, so it gives no number AND no one-query permission
    (a site whose measure field is unmapped must not be told it can run it)."""
    connector = _wire(monkeypatch, _metric(grouped=True), athena=True, ambiguous=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert connector.calls == []
    for site in result["sites"]:
        assert site["outcome"] == mtn.NOT_IN_TIME_OUTCOME
        assert site["computation"] == render.GROUPED_COMPUTATION
        assert site[render.LINEAGE_INCOMPLETE_KEY] is True
        assert site["window_applied_range"] == _RANGE
    notice = _block(result)
    assert " hours |" not in notice and "9 hours" not in notice
    assert (f"  East (sys-east): NOT COMPUTED (its query inside window [{_START}, {_END}) is "
            "grouped, and the metric layer gives no per-group figures per site; the metric "
            "layer could not compute it): NO number for this site") in notice
    assert "can compute it with one query" not in notice
    assert render.PERMITTED_LINE not in notice
    assert "scan limit" not in notice


def test_per_site_grouped_definition_without_a_window_gives_no_number(monkeypatch):
    _wire(monkeypatch, _metric(grouped=True), athena=True, ambiguous=True)
    notice = _block(mtn._resolve_one("job-tp"))
    assert ("  East (sys-east): NOT COMPUTED (its query is grouped, and the metric layer gives "
            "no per-group figures per site; the metric layer could not compute it): NO number "
            "for this site") in notice
    assert "[, )" not in notice and " hours |" not in notice


# --------------------------------------------------------------------------- #
# MED: an incomplete lineage gives no generic scoped-query permission
# --------------------------------------------------------------------------- #
def test_cut_grouped_lineage_with_a_named_site_gives_no_permission(monkeypatch):
    """Without it: the generic PERMITTED line beside the no-permission site line."""
    _wire(monkeypatch, _metric(grouped=True, flt=_LONG))
    notice = _notice_for(monkeypatch)
    assert render.PERMITTED_LINE not in notice
    assert render.NOT_AVAILABLE_LINE in notice
    assert render.NAMED_SITE_UNSCOPED_LINE.format(sites="Northfield") in notice


def test_filtered_non_measure_with_a_named_site_gives_no_permission(monkeypatch):
    _wire(monkeypatch, _metric(measure=False))
    notice = _notice_for(monkeypatch)
    assert render.PERMITTED_LINE not in notice
    assert render.NOT_AVAILABLE_LINE in notice


def test_cut_per_site_lineage_gives_no_permission(monkeypatch):
    _wire(monkeypatch, _metric(flt=_LONG), athena=True, ambiguous=True)
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert mtl.CUT_MARKER in notice
    assert render.PERMITTED_LINE not in notice
    assert render.NOT_AVAILABLE_LINE in notice


# --------------------------------------------------------------------------- #
# MED: the cut marker alone makes a lineage incomplete
# --------------------------------------------------------------------------- #
_CUT = f"sys-a.{TABLE}: {_DEFINITION} where job-kind in ('a', 'b'{mtl.CUT_MARKER}"


def test_cut_marker_without_the_flag_gives_no_hand_over():
    """Without it: the one-query PERMITTED line from a partial definition."""
    payload = {"outcome": mtn.NOT_IN_TIME_OUTCOME, "system_id": "sys-a", "lineage": _CUT,
               "computation": render.ONE_QUERY_COMPUTATION, "window_applied_range": _RANGE}
    clean = render.sanitize_result({**payload, "term": "t", "metric_id": "m", "label": "L"})
    assert clean["lineage_incomplete"] is True
    notice = _block(payload)
    assert render.NOT_IN_TIME_NO_PERMISSION_LINE in notice
    assert render.NOT_IN_TIME_ONE_QUERY_LINE not in notice


def test_cut_marker_without_the_flag_gives_no_computed_permission():
    payload = {"outcome": "computed", "value": 9.0, "unit": "hours", "system_id": "sys-a",
               "lineage": _CUT, "named_sites": ["Northfield"]}
    notice = _block(payload)
    assert render.PERMITTED_LINE not in notice
    assert render.NAMED_SITE_UNSCOPED_LINE.format(sites="Northfield") in notice


# --------------------------------------------------------------------------- #
# LOW: no window and no named site is byte-identical; the cache stays safe
# --------------------------------------------------------------------------- #
def test_filtered_non_measure_without_window_or_site_is_byte_identical(monkeypatch):
    _wire(monkeypatch, _metric(measure=False))
    assert mtn._resolve_one("job-tp") == {
        "outcome": "computed", "value": 18.0, "unit": "jobs", "system_id": "sys-a",
        "kind": "direct", "shape": "direct", "lineage": "sys-a.jobs.job_id (sum)",
        "window_requested": None, "window_applied": None, "sample_count": 36,
        "query_count": 1, "systems": ["sys-a"], "source": "code"}
    notice = _notice_for(monkeypatch, _NO_SITE)
    assert render.PERMITTED_LINE in notice and "Site(s) named" not in notice


def test_one_cached_result_serves_two_questions_safely(monkeypatch):
    _wire(monkeypatch, _metric(measure=False))
    reads = []
    monkeypatch.setattr(mr, "query_pg", lambda schema, sql, params=None: (
        reads.append(sql) or _pg(schema, sql, params)))
    match = type("M", (), {"ambiguous": False, "metric_ids": ["job-tp"],
                           "term": "job count", "label": "Job Count"})()
    monkeypatch.setattr(mtn, "_matches_for_turn", lambda q: ([match], None))
    monkeypatch.setattr(mtn, "_turn_window", lambda q: None)
    mtn.invalidate()
    named = mtn.results_for_turn("What is the job count at Northfield?")[0]
    plain = mtn.results_for_turn(_NO_SITE)[0]
    assert len(reads) == 1
    assert named["named_sites"] == ["Northfield"] and named["lineage_incomplete"] is True
    assert "named_sites" not in plain and "lineage_incomplete" not in plain
    assert json.dumps(plain, sort_keys=True) == json.dumps(
        {k: v for k, v in named.items() if k not in ("named_sites", "lineage_incomplete")},
        sort_keys=True)
