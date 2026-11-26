# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for DynamoDBStorage.scan pagination.

A single DynamoDB scan returns at most ~1MB. Before this fix scan() returned
only the first page, which silently truncated the prompt listing and — worse —
the registry re-seed's delete pass, so stale/duplicate rows accumulated because
each boot only cleared a partial page. scan() must now follow LastEvaluatedKey
to the end. No AWS — a fake table stands in for the boto3 resource Table.
"""

from agents.storage import DynamoDBStorage


class _FakeTable:
    """Returns `pages` in sequence, wiring LastEvaluatedKey between them."""

    def __init__(self, pages):
        self._pages = pages
        self.scan_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        idx = len(self.scan_calls) - 1
        resp = {"Items": self._pages[idx]}
        if idx < len(self._pages) - 1:
            resp["LastEvaluatedKey"] = {"template_id": f"cursor-{idx}"}
        return resp


def _store_with(table):
    s = DynamoDBStorage("some-table", "us-east-1")
    s._table = table  # bypass boto3
    return s


def test_scan_follows_last_evaluated_key_across_pages():
    table = _FakeTable([
        [{"template_id": "a"}, {"template_id": "b"}],
        [{"template_id": "c"}, {"template_id": "d"}],
        [{"template_id": "e"}],
    ])
    store = _store_with(table)
    items = store.scan()
    assert [i["template_id"] for i in items] == ["a", "b", "c", "d", "e"]
    # 3 pages ⇒ 3 scan calls; second/third carry the cursor forward.
    assert len(table.scan_calls) == 3
    assert "ExclusiveStartKey" in table.scan_calls[1]
    assert "ExclusiveStartKey" in table.scan_calls[2]


def test_scan_single_page_no_extra_calls():
    table = _FakeTable([[{"template_id": "only"}]])
    store = _store_with(table)
    items = store.scan()
    assert [i["template_id"] for i in items] == ["only"]
    assert len(table.scan_calls) == 1


def test_scan_with_explicit_limit_returns_single_page():
    """An explicit Limit means the caller wants one bounded page — don't paginate."""
    table = _FakeTable([
        [{"template_id": "a"}],
        [{"template_id": "b"}],
    ])
    store = _store_with(table)
    items = store.scan(Limit=1)
    assert [i["template_id"] for i in items] == ["a"]
    assert len(table.scan_calls) == 1


def test_scan_no_table_configured_returns_empty():
    store = DynamoDBStorage(None, "us-east-1")
    assert store.scan() == []
