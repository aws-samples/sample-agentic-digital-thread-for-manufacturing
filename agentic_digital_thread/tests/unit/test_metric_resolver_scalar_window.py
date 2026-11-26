# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the SCALAR windowed-metric path (fix-pass, Task 2D
parity): ``metric_resolver.resolve_metric`` for a ``DirectMetric`` carrying
``window=``.

Pins the honest contract:
  * event-time mapping PRESENT → the same closed-vocab event-time WHERE the
    grouped path composes is applied to the scalar aggregate, and lineage
    carries ``window_requested``/``window``/``window_column``;
  * event-time mapping ABSENT → the resolve proceeds with the LIFETIME shape
    (SQL byte-identical to a non-windowed resolve) and lineage surfaces
    ``window: null`` honesty (``window_requested`` set, ``window`` null);
  * an unknown vocabulary token fails LOUD before any graph/SQL work;
  * a NON-windowed metric's lineage payload stays byte-identical — the three
    window keys are absent entirely (exact-key-set discipline, golden pin).

No AWS, no graph, no DB. Mirrors ``test_metric_resolver.py`` seams
(``neptune_query``/``query_pg``/``get_schema``/``_connection_type`` on
``agents.metric_resolver``; ``_resolve_order_column`` on
``agents.metric_dimension``).
"""
from __future__ import annotations

import json

import pytest

import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation, DirectMetric
from agents.metric_resolver import (
    AuthorityResolver,
    MetricResolverError,
    _lineage_to_dict,
    resolve_metric,
)


class _FakeAuthority(AuthorityResolver):
    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:  # noqa: ARG002
        return self._system_id


_PG_RESULT = json.dumps(
    {"columns": ["sum", "count"], "rows": [[42, 6]], "row_count": 1})

_LIFETIME_SQL = "SELECT SUM(qty), COUNT(qty) FROM factory.orders"
_WINDOWED_SQL = (
    "SELECT SUM(qty), COUNT(qty) FROM factory.orders "
    "WHERE \"event_ts\" >= (now() - INTERVAL '24 hours')"
)


def _wire(monkeypatch, *, window="24h", order_col="event_ts",
          conn_type="PostgreSQL", aggregation=Aggregation.SUM):
    """Wire ``resolve_metric`` with a synthetic (windowed) metric and mocked
    graph/connector seams; return a dict capturing the executed SQL."""
    metric = DirectMetric(
        id="m-win", label="Output", unit="units",
        concept="widget-count", aggregation=aggregation, window=window,
    )

    class _Layer:
        def get_metric(self, mid):
            if mid != "m-win":
                raise KeyError(mid)
            return metric

    monkeypatch.setattr(mr, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda *_a, **_k: [{"table": "orders", "column": "qty"}])
    monkeypatch.setattr(mr, "get_schema", lambda sid: "factory")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: conn_type)
    # The event-time mapping (window column) — the SAME graph read the grouped
    # path / LATEST use, monkeypatched on the metric_dimension seam.
    monkeypatch.setattr(md, "_resolve_order_column",
                        lambda sid, table, query=None: order_col)
    # Keep the Athena scan-guard seams hermetic.
    monkeypatch.setattr(mr, "_athena_recent_partition_where",
                        lambda *_a, **_k: None)

    captured = {}

    def _fake_pg(schema, sql, params=None):
        captured["schema"], captured["sql"] = schema, sql
        return _PG_RESULT

    monkeypatch.setattr(mr, "query_pg", _fake_pg)
    return captured


# --- Windowed scalar WITH an event-time mapping -------------------------------

def test_windowed_scalar_applies_event_time_where(monkeypatch):
    captured = _wire(monkeypatch)
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    assert captured["sql"] == _WINDOWED_SQL
    assert rm.value == 42 and rm.sample_count == 6
    # Lineage records EXACTLY the executed SQL + the applied window.
    assert rm.lineage.sql == _WINDOWED_SQL
    assert rm.lineage.window_requested == "24h"
    assert rm.lineage.window == "24h"
    assert rm.lineage.window_column == "event_ts"


def test_windowed_scalar_lineage_dict_carries_window_keys(monkeypatch):
    _wire(monkeypatch)
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    payload = _lineage_to_dict(rm.lineage)
    assert payload["window_requested"] == "24h"
    assert payload["window"] == "24h"
    assert payload["window_column"] == "event_ts"
    # The scalar QueryPlan itself stays byte-identical (golden exact-key pin).
    assert set(payload["query_plan"].keys()) == {
        "system_id", "schema", "table", "column", "aggregation",
    }
    json.dumps(payload)


# --- Windowed scalar WITHOUT an event-time mapping (honest fallback) ----------

def test_windowed_scalar_without_mapping_resolves_lifetime(monkeypatch):
    captured = _wire(monkeypatch, order_col=None)
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    # SQL byte-identical to a non-windowed resolve — never a fabricated window.
    assert captured["sql"] == _LIFETIME_SQL
    assert rm.lineage.window_requested == "24h"
    assert rm.lineage.window is None
    assert rm.lineage.window_column is None


def test_windowed_scalar_fallback_surfaces_window_null_in_lineage(monkeypatch):
    _wire(monkeypatch, order_col=None)
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    payload = _lineage_to_dict(rm.lineage)
    # window:null-style honesty — the keys are PRESENT with the null signal.
    assert payload["window_requested"] == "24h"
    assert payload["window"] is None
    assert payload["window_column"] is None


# --- Closed vocabulary: unknown token fails loud -------------------------------

def test_unknown_window_token_fails_loud_before_any_work(monkeypatch):
    captured = _wire(monkeypatch, window="24 hours'); DROP TABLE orders;--")

    def _no_graph(*_a, **_k):
        raise AssertionError("no graph read for an invalid window token")

    monkeypatch.setattr(mr, "neptune_query", _no_graph)
    with pytest.raises(MetricResolverError, match="unsupported window"):
        resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    assert "sql" not in captured  # nothing executed


# --- Non-windowed regression: no window keys at all ----------------------------

def test_non_windowed_scalar_lineage_has_no_window_keys(monkeypatch):
    captured = _wire(monkeypatch, window=None)
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    assert captured["sql"] == _LIFETIME_SQL
    payload = _lineage_to_dict(rm.lineage)
    # Exact-key-set discipline: the golden scalar Lineage key set, unchanged.
    assert set(payload.keys()) == {
        "metric_id", "kind", "concept", "authority", "mapping", "query_plan",
        "sql", "executed_at", "op", "inputs", "prefer_stored_hit",
    }


# --- Non-SQL backend: honest lifetime fallback ----------------------------------

def test_non_sql_backend_skips_window(monkeypatch):
    captured = _wire(monkeypatch, conn_type="MCP Server")
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    # The predicate dialects cover Postgres/Athena only — never a wrong-dialect
    # WHERE; lineage still surfaces the honesty.
    assert captured["sql"] == _LIFETIME_SQL
    assert rm.lineage.window_requested == "24h"
    assert rm.lineage.window is None


# --- Athena: window predicate ANDs onto the scan-guard WHERE --------------------

def test_windowed_scalar_athena_ands_onto_partition_prune(monkeypatch):
    # The up-front prune remains only for an aggregate the connector cannot
    # compute exactly from pieces (here AVG); COUNT/SUM/MIN/MAX run as defined.
    captured = _wire(monkeypatch, conn_type="Athena", aggregation=Aggregation.AVG)
    monkeypatch.setattr(
        mr, "_athena_recent_partition_where",
        lambda sid, table, qualifier="": "\"dt\" >= '2026-07'",
    )
    rm = resolve_metric("m-win", authority=_FakeAuthority("sys-1"))
    assert captured["sql"] == (
        "SELECT AVG(qty), COUNT(qty) FROM factory.orders "
        "WHERE \"dt\" >= '2026-07' "
        "AND \"event_ts\" >= (current_timestamp - INTERVAL '24' HOUR)"
    )
    assert rm.lineage.window == "24h"
    assert rm.lineage.sql == captured["sql"]
