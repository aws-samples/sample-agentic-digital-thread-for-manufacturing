# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What an exact metric split may cost depends on who asked.

A COUNT/SUM/MIN/MAX metric on Athena now runs as defined and, past the scan
limit, is computed exactly from partition pieces. That can take many pieces
and minutes, so (``metric_athena_scope.read_scope``):

1. a dashboard or card refresh (the live path) gets its own lower caps (4
   pieces, 30 s); past them the value is narrowed WITH the disclosure;
2. a chat turn's metric notice gives the split its timebox as a deadline; a
   split that does not finish in time is a distinct outcome
   (``not_computed_in_time``), not a withhold, so the orchestrator may ask
   the system agent (with the full split budget) for the same value; a
   resolve still inside a split when the notice's timebox ends says the same;
3. a steward's dry-run preview keeps the system's own caps.

Fully offline (connector and graph seams stubbed); synthetic names only.
"""
from __future__ import annotations

import time

import pytest

import agents.athena_tools as at
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents import athena_exact_split as sp
from agents import connection_types as ct
from agents import metric_athena_scope as mas
from agents import metric_turn_notice as notice
from agents import metric_turn_render as render
from agents.metric_execution_trace import PREVIEW_SUFFIX, TURN_SUFFIX, execution_trace
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric

_CFG = {"workgroup": "wg", "database": "lake_db"}
_PRUNE = "\"dt\" IN ('2026-07-14', '2026-07-13')"
_ROW = {"columns": ["_col0", "_col1"], "rows": [["42", "40"]], "row_count": 1}


class _Connector:
    """Records the split caps in force at each call; refuses any query
    without the prune with ``reason`` (or returns ``_ROW``)."""

    def __init__(self, reason: str | None = None):
        self.reason, self.calls, self.caps = reason, [], []

    def __call__(self, sql, connection_config, **_kwargs):
        self.calls.append(sql)
        self.caps.append(sp.resolve_caps(dict(_CFG)))
        if self.reason and _PRUNE not in sql:
            error = at.AthenaEngineScanLimitError("over", limit_source="engine")
            error.exact_split = {"applied": False, "reason": self.reason}
            raise error
        return dict(_ROW)


def _wire(monkeypatch, connector):
    metric = DirectMetric(id="m-w", label="Widgets", unit="count",
                          concept="widget-count", aggregation=Aggregation.COUNT)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", lambda cypher, params=None: (
        [] if (params or {}).get("concept") == md.TIME_CONCEPT
        else [{"table": "orders", "column": "qty"}]))
    auth = type("A", (), {"system_id": "sys-lake", "concept": "widget-count", "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {"id": sid, "connection_type": ct.ATHENA})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: _PRUNE)
    monkeypatch.setattr(at, "run_athena_query_exact", connector, raising=False)
    monkeypatch.setattr(at, "run_athena_query", connector)


# --------------------------------------------------------------------------- #
# 1. Dashboard / card refresh
# --------------------------------------------------------------------------- #
def test_a_card_refresh_splits_with_its_own_lower_caps(monkeypatch):
    connector = _Connector()
    _wire(monkeypatch, connector)
    assert mr.resolve_metric("m-w").value == 42
    assert connector.caps[0].max_pieces == mas.CARD_MAX_PIECES == 4
    assert connector.caps[0].max_seconds == mas.CARD_MAX_SECONDS == 30
    # A card shows at most metric_dimension.MAX_GROUPS rows of a grouped metric.
    assert connector.caps[0].max_groups == mas.CARD_MAX_GROUPS < sp.DEFAULT_MAX_GROUPS
    assert mas.CARD_MAX_GROUPS >= 10 * md.MAX_GROUPS


@pytest.mark.parametrize("reason", ["piece_cap", "wall_time", "byte_cap", "group_cap", "group_bytes"])
def test_a_card_refresh_over_its_caps_is_narrowed_and_says_so(monkeypatch, reason):
    _wire(monkeypatch, _Connector(reason))
    scan = mr.resolve_metric("m-w").lineage.scan
    assert scan == {"mode": "narrowed", "where": _PRUNE, "reason": mas.REASON_CARD}
    assert "dashboard refresh" in mas.scan_text(scan)


def test_a_preview_keeps_the_systems_own_caps(monkeypatch):
    connector = _Connector()
    _wire(monkeypatch, connector)
    with execution_trace(PREVIEW_SUFFIX):
        mr.resolve_metric("m-w")
    assert connector.caps[0] == sp.resolve_caps(dict(_CFG))
    assert connector.caps[0].max_pieces == sp.DEFAULT_MAX_PIECES


def test_caps_limits_only_tighten_and_are_restored():
    base = sp.resolve_caps({"exact_split_max_pieces": 3})
    with sp.limited(sp.CapsLimit(max_pieces=8, max_seconds=999)):
        assert sp.resolve_caps({"exact_split_max_pieces": 3}) == base
    with sp.limited(sp.CapsLimit(deadline=time.monotonic() + 5)):
        assert sp.resolve_caps({}).max_seconds <= 5
    assert sp.resolve_caps({}) == sp.resolve_caps({})


# --------------------------------------------------------------------------- #
# 2. A chat turn's metric notice
# --------------------------------------------------------------------------- #
def test_a_turn_split_gets_the_notice_deadline(monkeypatch):
    connector = _Connector()
    _wire(monkeypatch, connector)
    with execution_trace(TURN_SUFFIX), mas.turn_budget(time.monotonic() + 6):
        mr.resolve_metric("m-w")
    assert connector.caps[0].max_seconds <= 6
    assert connector.caps[0].max_pieces == sp.DEFAULT_MAX_PIECES


def test_a_turn_split_out_of_time_is_not_computed_in_time(monkeypatch):
    _wire(monkeypatch, _Connector("wall_time"))
    with execution_trace(TURN_SUFFIX), mas.turn_budget(time.monotonic() + 6):
        with pytest.raises(mr.ExactValueNotInTime) as info:
            mr.resolve_metric("m-w")
    assert info.value.system_id == "sys-lake"
    assert info.value.lineage == "sys-lake.orders.qty (count)"


def test_a_turn_split_refused_for_another_reason_is_narrowed(monkeypatch):
    _wire(monkeypatch, _Connector("piece_cap"))
    with execution_trace(TURN_SUFFIX), mas.turn_budget(time.monotonic() + 6):
        scan = mr.resolve_metric("m-w").lineage.scan
    assert scan["mode"] == "narrowed" and scan["reason"] == mas.REASON_REFUSED


def test_the_notice_reports_not_computed_in_time_as_its_own_outcome(monkeypatch):
    _wire(monkeypatch, _Connector("wall_time"))
    result = notice._resolve_one("m-w", time.monotonic() + 6, {})
    assert result["outcome"] == render.NOT_IN_TIME_OUTCOME
    clean = render.sanitize_result({**result, "term": "widgets", "metric_id": "m-w",
                                    "label": "Widgets"})
    assert clean["system_id"] == "sys-lake"
    text = render.render_metric_layer_notice([clean])
    assert "NOT COMPUTED IN THE METRIC LAYER'S TIME BUDGET" in text
    assert "system agent of 'sys-lake' can compute it exactly" in text
    assert "delegate ONE query to that system agent" in text
    assert "WITHHELD" not in text.split("REQUIRED RESPONSE")[0]


def test_a_resolve_still_in_a_split_when_the_timebox_ends_says_so(monkeypatch):
    def slow(metric_id, **_kwargs):
        with mas.read_scope("sys-lake", "sys-lake.orders.qty (count)"):
            listener = sp._LISTENER.get()
            listener()
            time.sleep(0.6)
        raise AssertionError("not reached in time")

    monkeypatch.setattr(mr, "resolve_metric", slow)
    out = notice._submit_all(["m-split"], 0.2, 60.0)
    assert out["m-split"]["outcome"] == render.NOT_IN_TIME_OUTCOME
    assert out["m-split"]["system_id"] == "sys-lake"


def test_a_resolve_that_misses_the_timebox_outside_a_split_stays_unavailable(monkeypatch):
    monkeypatch.setattr(mr, "resolve_metric", lambda metric_id, **_k: time.sleep(0.6))
    out = notice._submit_all(["m-slow"], 0.2, 60.0)
    assert out["m-slow"] == {"outcome": "result_unavailable", "reason": "timebox"}


def test_a_per_site_value_not_computed_in_time_renders_per_site():
    from agents import metric_turn_sites as sites
    item = {"outcome": sites.SITES_OUTCOME, "term": "widgets", "metric_id": "m-w",
            "label": "Widgets", "source": "code", "concept": "widget-count",
            "query_count": 1, "systems": ["sys-a", "sys-b"],
            "sites": [{"site": "A", "system_id": "sys-a", "outcome": render.NOT_IN_TIME_OUTCOME,
                       "lineage": "sys-a.orders.qty (count)"},
                      {"site": "B", "system_id": "sys-b", "outcome": "computed", "value": 3,
                       "unit": "count", "kind": "direct", "shape": "direct",
                       "lineage": "sys-b.orders.qty (count)"}]}
    clean = render.sanitize_result(item)
    assert clean is not None and clean["sites"][0]["outcome"] == render.NOT_IN_TIME_OUTCOME
    assert "A (sys-a): NOT COMPUTED IN TIME" in render.render_metric_layer_notice([clean])
