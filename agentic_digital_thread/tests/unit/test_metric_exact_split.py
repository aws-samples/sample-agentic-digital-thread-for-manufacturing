# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Metric layer on Athena: exact past the scan limit, never silently narrowed.

Before this change every metric on a large Athena table was narrowed up front
to the table's most recent partitions (else its last 30 days of event time),
and nothing in the result said so: a lifetime COUNT came back as a recent one.
Now (``metric_athena_scope``):

1. a COUNT / SUM / MIN / MAX metric over one table runs as defined, through
   the connector that computes it exactly from partition pieces when the
   engine refuses it for size; its lineage says when that happened;
2. only when the engine refused it AND no exact path existed is it narrowed,
   and the lineage says to what and why;
3. any other metric (AVG, a joined grouping) is narrowed up front as before,
   and the lineage now says so;
4. the metric-layer notice the model reads carries that disclosure; a read
   that ran as defined in one query keeps its payload byte-identical.

Fully offline (the connector and graph seams are stubbed); synthetic names.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents import connection_types as ct
from agents import metric_turn_render as render
from agents.metric_layer import Aggregation, CodeMetricLayer, DirectMetric
from agents.metric_turn_lineage import lineage_summary

_CFG = {"workgroup": "wg", "database": "lake_db"}
_PRUNE = "\"dt\" IN ('2026-07-14', '2026-07-13')"
_SPLIT = {"applied": True, "pieces": 5, "partition_column": "created", "snapshot_id": 9,
          "atomic": True, "exact": True}


class _Connector:
    """Stub for ``run_athena_query_exact``: records SQL; refuses any query
    without the prune when ``refuse_unpruned``; returns ``result``."""

    def __init__(self, result, *, refuse_unpruned=False):
        self.result, self.refuse_unpruned, self.calls = result, refuse_unpruned, []

    def __call__(self, sql, connection_config, **_kwargs):
        self.calls.append(sql)
        if self.refuse_unpruned and _PRUNE not in sql:
            error = at.AthenaEngineScanLimitError("over", limit_source="engine")
            error.exact_split = {"applied": False, "reason": "not_iceberg"}
            raise error
        return dict(self.result)


def _wire(monkeypatch, connector, aggregation=Aggregation.SUM, *, join=None):
    metric = DirectMetric(id="m-w", label="Widgets", unit="count",
                          concept="widget-count", aggregation=aggregation)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer([metric]))
    monkeypatch.setattr(mr, "neptune_query", lambda cypher, params=None: (
        [] if (params or {}).get("concept") == md.TIME_CONCEPT
        else [{"table": "orders", "column": "qty"}]))
    auth = type("A", (), {"system_id": "sys-lake", "concept": "widget-count", "source": None})()
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a=None: auth)
    monkeypatch.setattr(mr, "get_schema", lambda sid: "lake_db")
    monkeypatch.setattr(mr.catalog, "get", lambda sid: {"id": sid, "connection_type": ct.ATHENA})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: (
        f"{k.get('qualifier', '')}{_PRUNE}"))
    # The connector seam; the plain runner is stubbed too, so a resolver that
    # bypassed the exact path would hit the same stub (and show its SQL).
    monkeypatch.setattr(at, "run_athena_query_exact", connector, raising=False)
    monkeypatch.setattr(at, "run_athena_query", connector)
    dim = type("D", (), {"table": "orders" if join is None else "statuses",
                         "column": "status", "role": "identifier", "confidence": 0.9})()
    monkeypatch.setattr(md, "_resolve_dimension_mappings", lambda *a, **k: [dim])
    monkeypatch.setattr(md, "_select_dimension_mapping", lambda *a, **k: (dim, join))


_SCALAR = {"columns": ["_col0", "_col1"], "rows": [["42", "40"]], "row_count": 1}
_GROUPED = {"columns": ["status", "v"], "rows": [["OPEN", "7"]], "row_count": 1}


# --------------------------------------------------------------------------- #
# Scalar
# --------------------------------------------------------------------------- #
def test_a_decomposable_metric_is_not_narrowed_up_front(monkeypatch):
    connector = _Connector(_SCALAR)
    _wire(monkeypatch, connector)
    resolved = mr.resolve_metric("m-w")
    assert connector.calls == ["SELECT SUM(qty), COUNT(qty) FROM lake_db.orders"]
    assert resolved.value == 42 and resolved.lineage.scan is None
    assert "scan" not in mr._lineage_to_dict(resolved.lineage)


def test_an_exact_split_metric_says_how_it_was_computed(monkeypatch):
    _wire(monkeypatch, _Connector({**_SCALAR, "exact_split": _SPLIT}))
    resolved = mr.resolve_metric("m-w")
    assert resolved.lineage.scan["mode"] == "exact_split" and resolved.lineage.scan["pieces"] == 5
    assert mr._lineage_to_dict(resolved.lineage)["scan"]["mode"] == "exact_split"
    summary = lineage_summary(resolved)
    assert "computed exactly from 5 partition pieces" in summary["scan"]
    assert summary["scan_narrowed"] is False


def test_a_refused_metric_with_no_exact_path_is_narrowed_and_says_so(monkeypatch):
    connector = _Connector(_SCALAR, refuse_unpruned=True)
    _wire(monkeypatch, connector, Aggregation.COUNT)
    resolved = mr.resolve_metric("m-w")
    assert connector.calls[-1] == f"SELECT COUNT(qty), COUNT(qty) FROM lake_db.orders WHERE {_PRUNE}"
    scan = resolved.lineage.scan
    assert scan["mode"] == "narrowed" and scan["where"] == _PRUNE
    assert "could not be computed exactly" in scan["reason"]
    assert resolved.lineage.sql == connector.calls[-1]


def test_a_non_decomposable_metric_is_narrowed_up_front_and_says_so(monkeypatch):
    connector = _Connector(_SCALAR)
    _wire(monkeypatch, connector, Aggregation.AVG)
    resolved = mr.resolve_metric("m-w")
    assert connector.calls == [f"SELECT AVG(qty), COUNT(qty) FROM lake_db.orders WHERE {_PRUNE}"]
    from agents.metric_athena_scope import REASON_SHAPE
    assert resolved.lineage.scan == {"mode": "narrowed", "where": _PRUNE, "reason": REASON_SHAPE}


def test_a_refusal_with_nothing_to_narrow_to_is_raised(monkeypatch):
    _wire(monkeypatch, _Connector(_SCALAR, refuse_unpruned=True))
    monkeypatch.setattr(mr, "_athena_recent_partition_where", lambda *a, **k: None)
    monkeypatch.setattr(mr, "_athena_time_window_where", lambda *a, **k: None)
    with pytest.raises(at.AthenaEngineScanLimitError):
        mr.resolve_metric("m-w")


# --------------------------------------------------------------------------- #
# Grouped
# --------------------------------------------------------------------------- #
def test_a_decomposable_grouped_metric_is_not_narrowed_up_front(monkeypatch):
    connector = _Connector(_GROUPED)
    _wire(monkeypatch, connector)
    out = md.resolve_metric_by_dimension("m-w", "widget-state")
    assert connector.calls == ['SELECT "status", SUM("qty") FROM "lake_db"."orders" '
                               'GROUP BY "status" ORDER BY "status" LIMIT 200']
    assert out.rows[0].lineage.scan is None
    assert "scan" not in md._row_lineage_to_dict(out.rows[0].lineage)


def test_a_grouped_exact_split_rides_every_row(monkeypatch):
    _wire(monkeypatch, _Connector({**_GROUPED, "exact_split": _SPLIT}))
    out = md.resolve_metric_by_dimension("m-w", "widget-state")
    assert out.rows[0].lineage.scan["mode"] == "exact_split"
    assert md._row_lineage_to_dict(out.rows[0].lineage)["scan"]["pieces"] == 5


def test_a_refused_grouped_metric_is_narrowed_and_says_so(monkeypatch):
    connector = _Connector(_GROUPED, refuse_unpruned=True)
    _wire(monkeypatch, connector)
    out = md.resolve_metric_by_dimension("m-w", "widget-state")
    assert f"WHERE {_PRUNE} GROUP BY" in connector.calls[-1]
    assert out.rows[0].lineage.sql == connector.calls[-1]
    assert out.rows[0].lineage.scan["mode"] == "narrowed"


def test_a_joined_grouped_metric_is_narrowed_up_front_and_says_so(monkeypatch):
    join = md.JoinPathProvenance(join_id="j", from_table="orders", from_field="sid",
                                 to_table="statuses", to_field="id")
    connector = _Connector(_GROUPED)
    _wire(monkeypatch, connector, join=join)
    out = md.resolve_metric_by_dimension("m-w", "widget-state")
    assert f"WHERE m.{_PRUNE} GROUP BY" in connector.calls[0] and len(connector.calls) == 1
    assert out.rows[0].lineage.scan["where"] == f"m.{_PRUNE}"


# --------------------------------------------------------------------------- #
# The notice the model reads
# --------------------------------------------------------------------------- #
def _computed(**extra):
    return {"outcome": "computed", "term": "widgets", "metric_id": "m-w", "label": "Widgets",
            "source": "code", "value": 42, "unit": "count", "kind": "direct", "shape": "direct",
            "system_id": "sys-lake", "lineage": "sys-lake.orders.qty (sum)",
            "window_requested": None, "window_applied": None, **extra}


def test_notice_names_a_narrowed_value_and_does_not_call_it_lifetime():
    from agents.metric_athena_scope import narrowed, scan_text
    text = scan_text(narrowed(_PRUNE, "the whole query is over the limit"))
    clean = render.sanitize_result(_computed(scan=text, scan_narrowed=True))
    block = render._render_computed(clean, "- 'widgets'")
    assert "scan: NARROWED to rows where" in block and _PRUNE in block
    assert render.window_class_and_line(clean)[0] == "mixed"
    assert "NARROWED" in render.window_class_and_line(clean)[1]


def test_notice_for_a_read_that_ran_as_defined_is_unchanged():
    clean = render.sanitize_result(_computed())
    assert "scan" not in clean
    assert "scan:" not in render._render_computed(clean, "- 'widgets'")
    assert render.window_class_and_line(clean) == ("lifetime", "lifetime (no window declared)")


# --------------------------------------------------------------------------- #
# A split result the connector does not call exact
# --------------------------------------------------------------------------- #
def test_a_floating_point_split_is_not_called_exact(monkeypatch):
    # The connector marks a floating-point SUM from pieces exact=False: it
    # can differ from one query's sum in its last digits.
    _wire(monkeypatch, _Connector({**_SCALAR, "exact_split": {**_SPLIT, "exact": False}}))
    resolved = mr.resolve_metric("m-w")
    assert resolved.lineage.scan["exact"] is False
    summary = lineage_summary(resolved)
    assert "exactly" not in summary["scan"]
    assert "floating-point" in summary["scan"] and "last digits" in summary["scan"]
    assert "5 partition pieces" in summary["scan"] and summary["scan_inexact"] is True


def test_the_notice_does_not_present_a_floating_point_split_as_exact():
    from agents.metric_athena_scope import scan_text
    scan = {"mode": "exact_split", "pieces": 5, "atomic": True, "exact": False}
    clean = render.sanitize_result(_computed(scan=scan_text(scan), scan_inexact=True))
    block = render._render_computed(clean, "- 'widgets'")
    assert "NOT EXACT" in block and "computed exactly" not in block
    closing = render.scope_sentence([clean])
    assert "NOT EXACT" in closing


@pytest.mark.parametrize("split", [{"exact": True, "atomic": None}, {"exact": None, "atomic": True},
                                   {"exact": True}],
                         ids=["unpinned", "exactness unknown", "pinning unknown"])
def test_a_split_without_both_marks_is_not_called_exact(split):
    from agents.metric_athena_scope import from_result, scan_text
    scan = from_result({"exact_split": {"applied": True, "pieces": 3, **split}})
    assert "exactly" not in scan_text(scan)
    from agents.metric_turn_lineage import _scan_fields
    assert _scan_fields(scan)["scan_inexact"] is True


def test_an_exact_split_keeps_its_payload():
    from agents.metric_turn_lineage import _scan_fields
    fields = _scan_fields({"mode": "exact_split", "pieces": 5, "atomic": True, "exact": True})
    assert set(fields) == {"scan", "scan_narrowed"} and "computed exactly" in fields["scan"]


def test_a_site_whose_value_is_not_exact_says_so():
    from agents import metric_turn_sites as sites
    site = {"site": "north", "system_id": "sys-lake", "outcome": "computed", "value": 4.5,
            "unit": "kg", "lineage": "sys-lake.orders.w (sum)", "scan_inexact": True}
    assert "NOT EXACT" in sites._site_line(site, True, True)
    assert "NOT EXACT" not in sites._site_line({**site, "scan_inexact": False}, True, True)
