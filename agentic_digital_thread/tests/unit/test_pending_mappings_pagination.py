# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``self_learning.get_pending_mappings`` pagination.

S-C: the ADR-0043 promotion threshold depends on SNAPSHOT COMPLETENESS — a
single DynamoDB page is capped at 1 MB, so pending rows past page 1 must
still be read. ``get_pending_mappings`` paginates on ``LastEvaluatedKey``
(the ``record_rule_usage`` idiom: defensive cap of 20 pages, never spins).

No AWS: ``_get_table`` is monkeypatched with in-memory fakes.
"""
from __future__ import annotations

import agents.self_learning as self_learning
from agents.self_learning import get_pending_mappings


class _TwoPageTable:
    """First query returns page 1 + LastEvaluatedKey; the follow-up query
    (carrying ExclusiveStartKey) returns page 2 with no further key."""

    def __init__(self):
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(dict(kwargs))
        if "ExclusiveStartKey" not in kwargs:
            return {
                "Items": [{"mapping_id": "vm-page1", "status": "pending"}],
                "LastEvaluatedKey": {"mapping_id": "vm-page1",
                                     "discovered_at": "2026-07-01T00:01:00+00:00"},
            }
        return {"Items": [{"mapping_id": "vm-page2", "status": "pending"}]}


class _EndlessTable:
    """Always returns a LastEvaluatedKey — exercises the defensive page cap."""

    def __init__(self):
        self.calls = 0

    def query(self, **kwargs):
        self.calls += 1
        return {
            "Items": [{"mapping_id": f"vm-{self.calls:03d}", "status": "pending"}],
            "LastEvaluatedKey": {"mapping_id": f"vm-{self.calls:03d}"},
        }


def test_rows_past_the_first_page_are_included(monkeypatch):
    table = _TwoPageTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    items = get_pending_mappings()

    assert [i["mapping_id"] for i in items] == ["vm-page1", "vm-page2"]
    assert len(table.calls) == 2
    # The follow-up query resumes exactly where page 1 stopped.
    assert table.calls[1]["ExclusiveStartKey"] == {
        "mapping_id": "vm-page1",
        "discovered_at": "2026-07-01T00:01:00+00:00",
    }
    # The GSI/key shape is unchanged across pages.
    for call in table.calls:
        assert call["IndexName"] == "status-index"
        assert call["ExpressionAttributeValues"] == {":status": "pending"}


def test_pagination_is_bounded_at_twenty_pages(monkeypatch):
    table = _EndlessTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    items = get_pending_mappings()

    assert table.calls == 20  # defensive cap — never spins
    assert len(items) == 20


def test_read_failure_still_swallows_to_empty(monkeypatch):
    class _Boom:
        def query(self, **kwargs):
            raise RuntimeError("dynamo down")

    monkeypatch.setattr(self_learning, "_get_table", lambda: _Boom())
    assert get_pending_mappings() == []
