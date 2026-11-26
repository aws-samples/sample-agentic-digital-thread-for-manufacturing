# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E7 day-bucketed cost ledger (Task 2.2, ADR-0053 D-E7).

``agents/cost_ledger.py`` clones the ``artifact_usage`` ledger idiom on the
EXISTING proposals table: one row family ``mapping_id = "cost#turns"``,
sort key ``discovered_at = "YYYY-MM-DD"``, atomic
``ADD input_tokens / output_tokens / turns``, NO ``status`` attribute,
60 s read cache (``_reset_read_cache()`` seam), best-effort swallow — a
ledger write never fails a turn. The runtime hook
(``runtime_app._bump_turn_cost_hook``) reuses the sibling call-site gates:
automated briefings and B2 try-it sessions never write.

Offline: no AWS, no network. Table mocked via
``monkeypatch.setattr(self_learning, "_get_table", ...)``
(``test_artifact_usage.py`` conventions).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.cost_ledger as cost_ledger
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


USAGE = {"input_tokens": 5230, "output_tokens": 812, "total_tokens": 6042,
         "latency_ms": 41210, "scope": "orchestrator-model-only"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cost_ledger._reset_read_cache()
    yield
    cost_ledger._reset_read_cache()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


class RecordingTable:
    """Fake proposals table: update_item/query spies, optional failures."""

    def __init__(self, query_pages=None, *, raise_on_update=False,
                 raise_on_query=False):
        self._query_pages = query_pages or [[]]
        self._raise_on_update = raise_on_update
        self._raise_on_query = raise_on_query
        self.updates = []
        self.query_calls = []

    def update_item(self, **kwargs):
        if self._raise_on_update:
            raise RuntimeError("update boom")
        self.updates.append(kwargs)

    def query(self, **kwargs):
        if self._raise_on_query:
            raise RuntimeError("query boom")
        self.query_calls.append(kwargs)
        page_idx = len(self.query_calls) - 1
        items = (self._query_pages[page_idx]
                 if page_idx < len(self._query_pages) else [])
        out = {"Items": items}
        if page_idx < len(self._query_pages) - 1:
            out["LastEvaluatedKey"] = {"mapping_id": f"page-{page_idx}"}
        return out


# --------------------------------------------------------------------------- #
# bump_turn_cost — atomic ADD on today's day bucket, NO status attribute
# --------------------------------------------------------------------------- #
def test_day_bucket_add_semantics(monkeypatch):
    table = RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert cost_ledger.bump_turn_cost(USAGE) == 1

    assert len(table.updates) == 1
    upd = table.updates[0]
    assert upd["Key"] == {"mapping_id": "cost#turns", "discovered_at": _today()}
    assert upd["UpdateExpression"] == (
        "ADD input_tokens :in_t, output_tokens :out_t, turns :one")
    assert upd["ExpressionAttributeValues"] == {
        ":in_t": 5230, ":out_t": 812, ":one": 1}
    # NO status attribute — the row must stay invisible to status-GSI reads.
    assert "status" not in str(upd)

    # Additive: a second turn is another atomic ADD on the same bucket.
    assert cost_ledger.bump_turn_cost(
        {"input_tokens": 10, "output_tokens": 2}) == 1
    assert table.updates[1]["Key"]["discovered_at"] == _today()
    assert table.updates[1]["ExpressionAttributeValues"][":in_t"] == 10


def test_unmeasured_or_malformed_usage_writes_nothing(monkeypatch):
    """Absence = absent: a falsy/malformed payload must never write zeros."""
    def boom():
        raise AssertionError("_get_table must not be called for invalid usage")

    monkeypatch.setattr(self_learning, "_get_table", boom)
    for bad in (None, {}, "x", {"input_tokens": 5},
                {"input_tokens": -1, "output_tokens": 2},
                {"input_tokens": True, "output_tokens": 2},
                {"input_tokens": "5", "output_tokens": 2}):
        assert cost_ledger.bump_turn_cost(bad) == 0


def test_write_failure_never_raises(monkeypatch):
    table = RecordingTable(raise_on_update=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert cost_ledger.bump_turn_cost(USAGE) == 0

    def raising_get_table():
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(self_learning, "_get_table", raising_get_table)
    assert cost_ledger.bump_turn_cost(USAGE) == 0


# --------------------------------------------------------------------------- #
# read_cost_trend — pinned row shape, window, cache, swallow
# --------------------------------------------------------------------------- #
def test_read_trend_shape(monkeypatch):
    items = [
        {"mapping_id": "cost#turns", "discovered_at": _day(1),
         "turns": 3, "input_tokens": 900, "output_tokens": 120},
        {"mapping_id": "cost#turns", "discovered_at": _today(),
         "turns": 2, "input_tokens": 500, "output_tokens": 80},
        # Pre-window bucket: excluded even if the backend returned it.
        {"mapping_id": "cost#turns", "discovered_at": _day(30),
         "turns": 9, "input_tokens": 9999, "output_tokens": 9999},
    ]
    table = RecordingTable(query_pages=[items])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    rows = cost_ledger.read_cost_trend(days=7)

    assert rows == [
        {"date": _day(1), "turns": 3, "input_tokens": 900, "output_tokens": 120},
        {"date": _today(), "turns": 2, "input_tokens": 500, "output_tokens": 80},
    ]
    # The query is key-bounded: fixed partition key + sort-key cutoff.
    kwargs = table.query_calls[0]
    assert kwargs["KeyConditionExpression"] == (
        "mapping_id = :mid AND discovered_at >= :cutoff")
    assert kwargs["ExpressionAttributeValues"] == {
        ":mid": "cost#turns", ":cutoff": _day(6)}


def test_read_trend_cached_then_reset(monkeypatch):
    table = RecordingTable(query_pages=[[
        {"mapping_id": "cost#turns", "discovered_at": _today(),
         "turns": 1, "input_tokens": 10, "output_tokens": 2}]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    first = cost_ledger.read_cost_trend(days=7)
    second = cost_ledger.read_cost_trend(days=7)
    assert first == second
    assert len(table.query_calls) == 1  # 60 s cache absorbed the second read

    cost_ledger._reset_read_cache()
    cost_ledger.read_cost_trend(days=7)
    assert len(table.query_calls) == 2


def test_read_failure_swallows_to_empty(monkeypatch):
    table = RecordingTable(raise_on_query=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert cost_ledger.read_cost_trend(days=7) == []


# --------------------------------------------------------------------------- #
# runtime hook — sibling call-site gates (briefing + try-it), swallow
# --------------------------------------------------------------------------- #
def _metrics():
    return SimpleNamespace(
        accumulated_usage={"inputTokens": 100, "outputTokens": 20,
                           "totalTokens": 120},
        accumulated_metrics={"latencyMs": 1500},
    )


def test_briefing_and_tryit_skipped(monkeypatch):
    def boom():
        raise AssertionError("gated turns must never reach the table")

    monkeypatch.setattr(self_learning, "_get_table", boom)
    assert runtime_app._bump_turn_cost_hook(
        _metrics(), "Generate the operations briefing", "s-1") == 0
    assert runtime_app._bump_turn_cost_hook(
        _metrics(), "compare ERP and MES yields", "tryit-abc123-a") == 0


def test_hook_bumps_interactive_turn(monkeypatch):
    table = RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert runtime_app._bump_turn_cost_hook(
        _metrics(), "compare ERP and MES yields", "s-1") == 1
    assert table.updates[0]["ExpressionAttributeValues"] == {
        ":in_t": 100, ":out_t": 20, ":one": 1}


def test_hook_unmeasured_and_failure_swallow(monkeypatch):
    def boom():
        raise AssertionError("unmeasured turns must never reach the table")

    monkeypatch.setattr(self_learning, "_get_table", boom)
    # No metrics ⇒ no payload ⇒ zero writes (absence = absent).
    assert runtime_app._bump_turn_cost_hook(None, "a question", "s-1") == 0

    def raising_get_table():
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(self_learning, "_get_table", raising_get_table)
    assert runtime_app._bump_turn_cost_hook(_metrics(), "a question", "s-1") == 0
