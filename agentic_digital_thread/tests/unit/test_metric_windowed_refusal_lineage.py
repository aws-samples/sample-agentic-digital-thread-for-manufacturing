# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A duration metric refused for scan size inside an explicit turn window is
handed to the system agent with the definition and the window it must apply,
never as an exact split it cannot do.

On the explicit-window branch, such a refusal reaches ``not_computed_in_time``
(the system agent can compute it). That block carried ``plan_lineage`` (for
example ``sys-a.jobs.job_id (count)``: no measure, percentiles, filter or
window) and said the system agent "computes it exactly from partition
pieces", which a percentile cannot be. A delegated agent could compute a plain
count over every day. Pinned here:

* on the scalar, grouped and per-site paths the block carries the rendered
  definition (the computed blocks' ``definition_lineage``) and the applied
  ``[start, end)`` range, and its line asks for ONE query inside that window,
  applying that definition, reported as computed by that system agent;
* a definition that does not fit whole gives no permission: the metric layer
  could not compute it, and no number is given;
* the exact-split outcome (no window, a decomposable metric) is byte-identical.

Offline. The schema and sites are invented (``jobs``, ``East`` / ``West``).
"""
from __future__ import annotations

import pytest

import agents.metric_resolver as mr
import agents.metric_turn_lineage as mtl
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as render
import agents.metric_turn_sites as mts

from .test_metric_turn_window_scope import (
    _END, _LONG, _START, TABLE, _metric, _win, _wire,
)

_DEFINITION = "p90, p99 of duration(began_at -> finished_at) in hours"
_RANGE = {"start": _START, "end": _END}
_ONE_QUERY = "ONE query inside the window above"


def _block(result: dict) -> str:
    clean = render.sanitize_result({**result, "term": "job time", "metric_id": "job-tp",
                                    "label": "Job Time"})
    return render.render_metric_layer_notice([clean])


# --------------------------------------------------------------------------- #
# scalar and grouped: the definition, the window, one query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_refused_windowed_measure_carries_the_definition_and_window(monkeypatch, grouped):
    """Without it: ``lineage: sys-a.jobs.job_id (count)`` and no window."""
    _wire(monkeypatch, _metric(grouped=grouped), athena=True, refuse=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    by = " by shade_code" if grouped else ""
    assert result["outcome"] == mtn.NOT_IN_TIME_OUTCOME
    assert result["lineage"] == f"sys-a.{TABLE}: {_DEFINITION}{by} where job-kind eq 'RUN'"
    assert result["window_applied_range"] == _RANGE
    assert "lineage_incomplete" not in result


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_refused_windowed_measure_block_asks_for_one_query(monkeypatch, grouped):
    """Without it: the block says the system agent computes it "exactly from
    partition pieces", which a percentile cannot be."""
    _wire(monkeypatch, _metric(grouped=grouped), athena=True, refuse=True)
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert "partition pieces" not in notice and "exactly" not in notice
    assert f"window: applied window [{_START}, {_END})" in notice
    assert f"lineage: sys-a.{TABLE}: {_DEFINITION}" in notice
    assert render.NOT_IN_TIME_ONE_QUERY_LINE in notice
    assert _ONE_QUERY in render.NOT_IN_TIME_ONE_QUERY_LINE
    assert "computed by that system agent" in render.NOT_IN_TIME_ONE_QUERY_LINE


@pytest.mark.parametrize("grouped", [False, True], ids=["scalar", "grouped"])
def test_refused_definition_over_the_bound_gives_no_permission(monkeypatch, grouped):
    _wire(monkeypatch, _metric(grouped=grouped, flt=_LONG), athena=True, refuse=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    assert result["lineage"].endswith(mtl.CUT_MARKER)
    assert result["lineage_incomplete"] is True
    notice = _block(result)
    assert "PERMITTED" not in notice.split("REQUIRED RESPONSE")[0]
    assert render.NOT_IN_TIME_NO_PERMISSION_LINE in notice
    assert "could not compute it" in render.NOT_IN_TIME_NO_PERMISSION_LINE
    assert render.WITHHELD_SCOPE_LINE in notice


def test_unreadable_definition_gives_no_permission(monkeypatch):
    """A refusal classified without its metric (the definition cannot be
    read) keeps the short lineage, marked incomplete."""
    exc = mr.ExactValueNotInTime("sys-a", "sys-a.jobs.job_id (count)", table=TABLE,
                                 window=_win())
    result = mtn._classify_error(exc)
    assert result["lineage"] == "sys-a.jobs.job_id (count)"
    assert result["lineage_incomplete"] is True
    assert render.NOT_IN_TIME_NO_PERMISSION_LINE in _block(result)


# --------------------------------------------------------------------------- #
# per site
# --------------------------------------------------------------------------- #
def test_refused_per_site_measure_carries_the_definition_and_window(monkeypatch):
    _wire(monkeypatch, _metric(), athena=True, ambiguous=True, refuse=True)
    result = mtn._resolve_one("job-tp", turn_window=_win())
    for site in result["sites"]:
        assert site["outcome"] == mtn.NOT_IN_TIME_OUTCOME
        assert site["lineage"] == (f"{site['system_id']}.{TABLE}: {_DEFINITION} "
                                   "where job-kind eq 'RUN'")
        assert site["window_applied_range"] == _RANGE
    clean = render.sanitize_result({**result, "term": "job time", "metric_id": "job-tp",
                                    "label": "Job Time"})
    assert clean["sites"][0]["window_applied_range"] == _RANGE
    notice = render.render_metric_layer_notice([clean])
    assert "partition pieces" not in notice and "exactly" not in notice
    assert (f"  East (sys-east): NOT COMPUTED IN TIME (its query inside window "
            f"[{_START}, {_END}) is over the per-query scan limit; that system's agent can "
            f"compute it with one query inside that window applying: sys-east.{TABLE}: "
            f"{_DEFINITION} where job-kind eq 'RUN')") in notice


def test_refused_per_site_definition_over_the_bound_gives_no_number(monkeypatch):
    _wire(monkeypatch, _metric(flt=_LONG), athena=True, ambiguous=True, refuse=True)
    notice = _block(mtn._resolve_one("job-tp", turn_window=_win()))
    assert "can compute it" not in notice
    assert ("  East (sys-east): NOT COMPUTED (its query inside window "
            f"[{_START}, {_END}) is over the per-query scan limit; the metric layer could "
            "not compute it): NO number for this site") in notice


# --------------------------------------------------------------------------- #
# the exact-split outcome is byte-identical
# --------------------------------------------------------------------------- #
_EXACT_BLOCK = (
    "- 'widgets' → metric 'm-w' (\"Widgets\"): NOT COMPUTED IN THE METRIC LAYER'S TIME "
    "BUDGET THIS TURN: it is over the per-query scan limit, and the system agent of "
    "'sys-lake' can compute it exactly.\n  lineage: sys-lake.orders.qty (sum)\n  PERMITTED: "
    "delegate ONE query to that system agent for this metric as defined (lineage above); it "
    "computes it exactly from partition pieces. Report its result as computed by that system "
    "agent, not by the metric layer. Give no number for it before that answer.")


def test_exact_split_not_in_time_is_byte_identical():
    exc = mr.ExactValueNotInTime("sys-lake", "sys-lake.orders.qty (sum)")
    assert str(exc) == ("the value of sys-lake.orders.qty (sum) is over the per-query scan "
                        "limit and its exact computation from partition pieces did not finish "
                        "in the metric layer's time budget")
    result = mtn._classify_error(exc)
    assert result == {"outcome": mtn.NOT_IN_TIME_OUTCOME, "system_id": "sys-lake",
                      "lineage": "sys-lake.orders.qty (sum)"}
    clean = render.sanitize_result({**result, "term": "widgets", "metric_id": "m-w",
                                    "label": "Widgets"})
    assert clean == {"outcome": mtn.NOT_IN_TIME_OUTCOME, "term": "widgets", "metric_id": "m-w",
                     "label": "Widgets", "source": "", "system_id": "sys-lake",
                     "lineage": "sys-lake.orders.qty (sum)"}
    assert render._render_block(clean) == _EXACT_BLOCK
    assert mtn._missed({"split": True, "system_id": "sys-lake",
                        "lineage": "sys-lake.orders.qty (sum)"}) == result


def test_exact_split_site_line_is_byte_identical():
    site = {"site": "A", "system_id": "sys-a", "outcome": mtn.NOT_IN_TIME_OUTCOME,
            "lineage": "sys-a.orders.qty (sum)"}
    assert mts._site_line(site, True, True) == (
        "  A (sys-a): NOT COMPUTED IN TIME (over the per-query scan limit; that system's "
        "agent can compute it exactly: sys-a.orders.qty (sum))")


def test_sanitizer_drops_one_query_fields_it_cannot_trust():
    base = {"outcome": mtn.NOT_IN_TIME_OUTCOME, "term": "t", "metric_id": "m", "label": "L",
            "system_id": "sys-a", "lineage": "sys-a.jobs: x", "computation": "one_query"}
    bad = render.sanitize_result({**base, "window_applied_range": {"start": "x;y",
                                                                   "end": _END}})
    assert bad["lineage_incomplete"] is True  # no window to state, so no permission
    assert render.sanitize_result({**base, "computation": "other"}) == render.sanitize_result(
        {k: v for k, v in base.items() if k != "computation"})
