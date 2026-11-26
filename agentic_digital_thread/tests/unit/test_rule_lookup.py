# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch accepted-row lookup + prefetched-rows bump (rules-applied metric, Task 2).

- ``self_learning.get_accepted_rules_by_node_ids``: ONE paginated status-index
  GSI query (never per-id) filtered to rows whose ``neptune_node_id`` matched;
  best-effort — ``[]`` on empty input (zero queries) or any backend failure.
- ``self_learning.record_rule_usage(..., prefetched_rows=...)``: when the
  caller already fetched the accepted rows (from the lookup above), the bump
  SKIPS the GSI query entirely — zero ``table.query`` calls, one
  ``update_item`` per matched row — and keeps the never-raise contract.

Offline: no AWS, no Neptune, no network. Table mocked via the
``monkeypatch.setattr(self_learning, "_get_table", ...)`` seam
(``test_usage_tracking.py`` conventions).
"""
from __future__ import annotations

import agents.self_learning as self_learning


class SpyTable:
    """Fake adt-proposed-mappings table: canned status-index pages + call spies.

    ``pages`` is a list of item lists; each ``query`` call serves the next
    page, carrying ``LastEvaluatedKey`` until the final page — so a
    multi-page backend still counts as ONE query *sequence*.
    """

    def __init__(self, pages, *, raise_on_query=False, raise_on_update=False):
        self._pages = pages
        self._raise_on_query = raise_on_query
        self._raise_on_update = raise_on_update
        self.query_calls = []  # ExclusiveStartKey (or None) per call
        self.updates = []      # update_item kwargs per call

    def query(self, **kwargs):
        if self._raise_on_query:
            raise RuntimeError("query boom")
        assert kwargs.get("IndexName") == "status-index"
        assert kwargs["ExpressionAttributeValues"] == {":status": "accepted"}
        self.query_calls.append(kwargs.get("ExclusiveStartKey"))
        page_idx = len(self.query_calls) - 1
        items = self._pages[page_idx] if page_idx < len(self._pages) else []
        out = {"Items": items}
        if page_idx < len(self._pages) - 1:
            out["LastEvaluatedKey"] = {"mapping_id": f"page-{page_idx}"}
        return out

    def update_item(self, **kwargs):
        if self._raise_on_update:
            raise RuntimeError("update boom")
        self.updates.append(kwargs)


def _accepted(mapping_id, node_id):
    return {"mapping_id": mapping_id,
            "discovered_at": f"2026-07-14T00:00:0{mapping_id[-1]}",
            "status": "accepted", "neptune_node_id": node_id}


# --------------------------------------------------------------------------- #
# Acceptance (b): get_accepted_rules_by_node_ids — one query sequence,
# matching rows only, best-effort
# --------------------------------------------------------------------------- #
def test_lookup_one_query_sequence_returns_only_matching_rows(monkeypatch):
    table = SpyTable([[
        _accepted("m1", "subj-a1b2c3d4e5f60718"),
        _accepted("m2", "subj-ffffffffffffffff"),  # accepted but NOT asked for
        _accepted("m3", "vm-learned-deadbeef"),
    ]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    rows = self_learning.get_accepted_rules_by_node_ids(
        {"subj-a1b2c3d4e5f60718", "vm-learned-deadbeef",
         "subj-0000000000000000"})  # third id has no accepted row

    assert [r["mapping_id"] for r in rows] == ["m1", "m3"]
    # ONE query sequence — a single page means exactly one table.query call,
    # regardless of how many ids were asked for (never per-id).
    assert table.query_calls == [None]


def test_lookup_multipage_is_still_one_sequence_and_finds_page_two(monkeypatch):
    table = SpyTable([
        [_accepted("m1", "subj-ffffffffffffffff")],       # page 1 — not asked for
        [_accepted("m2", "subj-a1b2c3d4e5f60718")],       # page 2 — MATCH
    ])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    rows = self_learning.get_accepted_rules_by_node_ids({"subj-a1b2c3d4e5f60718"})

    assert [r["mapping_id"] for r in rows] == ["m2"]
    # One sequence: page 2 reached by threading LastEvaluatedKey, not by
    # issuing a fresh per-id query.
    assert table.query_calls == [None, {"mapping_id": "page-0"}]


def test_lookup_empty_ids_returns_empty_with_zero_queries(monkeypatch):
    def _boom():
        raise AssertionError("_get_table must not be called for empty ids")
    monkeypatch.setattr(self_learning, "_get_table", _boom)
    assert self_learning.get_accepted_rules_by_node_ids(set()) == []


def test_lookup_raising_table_swallows_to_empty(monkeypatch):
    table = SpyTable([], raise_on_query=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    # Best-effort (ADR-0003 F read-only bulk category): never raises.
    assert self_learning.get_accepted_rules_by_node_ids(
        {"subj-a1b2c3d4e5f60718"}) == []


# --------------------------------------------------------------------------- #
# Acceptance (c): record_rule_usage(prefetched_rows=...) — zero queries,
# one update per matched row, never raises
# --------------------------------------------------------------------------- #
def test_prefetched_rows_skip_the_gsi_query_entirely(monkeypatch):
    table = SpyTable([], raise_on_query=True)  # ANY query call would raise
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    rows = [_accepted("m1", "subj-a1b2c3d4e5f60718"),
            _accepted("m2", "vm-learned-deadbeef")]

    bumped = self_learning.record_rule_usage(
        {"subj-a1b2c3d4e5f60718", "vm-learned-deadbeef"},
        prefetched_rows=rows)

    assert bumped == 2
    assert table.query_calls == []  # zero table.query calls
    assert len(table.updates) == 2
    for upd in table.updates:
        # Same atomic-counter expression as the default path.
        assert "ADD usage_count :one" in upd["UpdateExpression"]
        assert "SET last_used_at = :now" in upd["UpdateExpression"]


def test_prefetched_rows_still_filtered_by_node_ids_defensively(monkeypatch):
    table = SpyTable([])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    rows = [_accepted("m1", "subj-a1b2c3d4e5f60718"),
            _accepted("m2", "subj-ffffffffffffffff")]  # NOT in node_ids

    bumped = self_learning.record_rule_usage(
        {"subj-a1b2c3d4e5f60718"}, prefetched_rows=rows)

    assert bumped == 1
    assert len(table.updates) == 1
    assert table.updates[0]["Key"]["mapping_id"] == "m1"


def test_prefetched_empty_list_bumps_nothing(monkeypatch):
    table = SpyTable([])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    # Empty PREFETCHED rows (ids present) — explicit [] must NOT fall back to
    # the GSI query: `is not None` gate, not truthiness.
    bumped = self_learning.record_rule_usage(
        {"subj-a1b2c3d4e5f60718"}, prefetched_rows=[])
    assert bumped == 0
    assert table.query_calls == []
    assert table.updates == []


def test_prefetched_rows_raising_update_returns_zero_not_raises(monkeypatch):
    table = SpyTable([], raise_on_update=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    rows = [_accepted("m1", "subj-a1b2c3d4e5f60718")]
    # Never raises — best-effort returns 0 (ADR-0003 F bulk category).
    assert self_learning.record_rule_usage(
        {"subj-a1b2c3d4e5f60718"}, prefetched_rows=rows) == 0


def test_default_none_path_still_queries_the_gsi(monkeypatch):
    """Guard: prefetched_rows=None (the default) keeps the existing
    query-then-bump behavior — the deep pins live in test_usage_tracking.py."""
    table = SpyTable([[_accepted("m1", "subj-a1b2c3d4e5f60718")]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    bumped = self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"})

    assert bumped == 1
    assert table.query_calls == [None]  # the GSI query still happens
    assert len(table.updates) == 1
