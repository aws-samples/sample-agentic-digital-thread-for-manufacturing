# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""JoinPath end-to-end transform + time_key threading (self_learning).

Proves a discovered ``join_path`` proposal carries its REAL transform (identity |
casefold | strip_prefix:<affix> | strip_suffix:<affix> | token_extract) and an
optional ``time_key`` from ``log_proposed_mapping`` → the stored DynamoDB item →
the accepted Neptune ``JoinPath`` node — replacing the old hardcoded ``'direct'`` —
while staying additive/back-compat (no transform → identity default; other types
untouched).

No AWS / Neptune / network: an in-memory ``FakeTable`` and a ``NeptuneSpy`` capture
writes. GENERIC — synthetic systems/fields only.
"""
from __future__ import annotations

import agents.self_learning as self_learning

import pytest

from tests.unit._value_mapping_write_seam import route_returning_writes


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)



class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_part = UpdateExpression.strip()
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                if "=" not in clause:
                    continue
                lhs, rhs = (s.strip() for s in clause.split("=", 1))
                item[names.get(lhs, lhs)] = values[rhs]

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)


class NeptuneSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, cypher: str, parameters: dict | None = None):
        self.calls.append((cypher, parameters))
        return True

    def joinpath_write(self):
        for cypher, params in self.calls:
            if "MERGE (jp:JoinPath" in cypher:
                return cypher, params
        return None, None


def _install(monkeypatch):
    table = FakeTable()
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    route_returning_writes(monkeypatch, spy)
    return table, spy


def _log_join(transform="", time_key=None):
    return self_learning.log_proposed_mapping(
        "sys-a", "wo.cell", "",
        "sys-b", "asset.site_name", "",
        "site", 0.9, "Auto-analysis: value-overlap join",
        proposed_type="join_path", transform=transform, time_key=time_key,
    )


def test_join_path_item_stores_transform_and_time_key(monkeypatch):
    _install(monkeypatch)
    tk = {"from_time_field": "start_date", "to_time_field": "event_time",
          "semantics": "window"}
    mid = _log_join(transform="strip_prefix:WO-", time_key=tk)
    table = self_learning._get_table()
    item = table.by_id(mid)
    assert item["transform"] == "strip_prefix:WO-"
    assert item["time_key"] == {"from_time_field": "start_date",
                                "to_time_field": "event_time", "semantics": "window"}


def test_accepted_join_path_writes_real_transform_and_time_key(monkeypatch):
    _table, spy = _install(monkeypatch)
    tk = {"from_time_field": "start_date", "to_time_field": "event_time",
          "semantics": "window"}
    mid = _log_join(transform="casefold", time_key=tk)
    self_learning.accept_mapping(mid)
    cypher, params = spy.joinpath_write()
    assert cypher is not None
    assert params["xform"] == "casefold"
    assert "jp.transform = $xform" in cypher
    assert "jp.transform = 'direct'" not in cypher  # the hardcode is gone
    assert params["tfrom"] == "start_date"
    assert params["tto"] == "event_time"
    assert params["tsem"] == "window"
    assert "jp.time_from_field = $tfrom" in cypher


def test_join_path_without_transform_defaults_to_identity(monkeypatch):
    _table, spy = _install(monkeypatch)
    mid = _log_join()  # no transform, no time_key (back-compat / legacy path)
    self_learning.accept_mapping(mid)
    cypher, params = spy.joinpath_write()
    assert params["xform"] == "identity"          # default, not 'direct'
    # No time_key → no time_* clause / params.
    assert "jp.time_from_field" not in cypher
    assert "tfrom" not in params


def test_value_mapping_is_unaffected_by_transform_threading(monkeypatch):
    table, _spy = _install(monkeypatch)
    mid = self_learning.log_proposed_mapping(
        "sys-a", "t.code", "131607000",
        "sys-a", "t.desc", "Feeder jam",
        "failure-mode", 0.9, "ctx", proposed_type="value_mapping",
    )
    item = table.by_id(mid)
    assert "transform" not in item      # additive: never added to non-join types
    assert "time_key" not in item
