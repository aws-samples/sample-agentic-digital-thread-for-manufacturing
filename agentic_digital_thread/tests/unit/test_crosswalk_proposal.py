# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Crosswalk foundation: a human-confirmed value↔value bridge for disjoint-vocabulary
entities travels the SAME proposal lifecycle and, on accept, persists a ValueMapping.

Covers: canonical keying for the new ``crosswalk`` proposed_type; storage as a
pending proposal in the shared accept/reject surface; NEVER auto-accepted (a
name/token-similarity guess must be human-confirmed); score_proposal never returns
'high'; and accept → ValueMapping (method='crosswalk_confirmed').

No AWS / Neptune / network. GENERIC — synthetic systems/fields/values only.
"""
from __future__ import annotations

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes
from agents.proposal_key import canonical_keys
from agents.proposal_scoring import score_proposal


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

    def find(self, needle: str):
        for cypher, params in self.calls:
            if needle in cypher:
                return cypher, params
        return None, None


def _install(monkeypatch):
    table = FakeTable()
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    route_returning_writes(monkeypatch, spy)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return table, spy


def _log_crosswalk():
    return self_learning.log_proposed_mapping(
        "sys-a", "wo.machine_name", "Press Line 1",
        "sys-b", "asset.machine", "press line one",
        "machine", 0.67, "Auto-analysis: crosswalk candidate (name/token similarity 0.67)",
        proposed_type="crosswalk",
    )


def test_crosswalk_canonical_key_is_value_grained():
    keys = canonical_keys(
        "crosswalk", from_system="sys-a", from_field="wo.machine_name",
        from_value="Press Line 1", to_system="sys-b", to_field="asset.machine",
        to_value="press line one",
    )
    # Subject includes the from-value (each candidate pair independently reviewable).
    assert "press line 1" in keys.subject_key
    assert keys.target_key.endswith("press line one")


def test_crosswalk_proposal_is_stored_pending(monkeypatch):
    _install(monkeypatch)
    mid = _log_crosswalk()
    item = self_learning._get_table().by_id(mid)
    assert item["proposed_type"] == "crosswalk"
    assert item["status"] == "pending"
    assert item["from_value"] == "Press Line 1"
    assert item["to_value"] == "press line one"
    assert item["concept"] == "machine"


def test_crosswalk_is_never_auto_accepted(monkeypatch):
    _table, spy = _install(monkeypatch)
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")  # even with auto-accept ON
    mid = _log_crosswalk()
    item = self_learning._get_table().by_id(mid)
    assert item["status"] == "pending"                 # stayed pending
    assert "crosswalk" not in self_learning._AUTO_ACCEPT_TYPES
    # No Neptune node written (not accepted).
    assert spy.find("ValueMapping") == (None, None)


def test_crosswalk_scores_below_high():
    low = score_proposal({"proposed_type": "crosswalk", "from_value": "a",
                          "to_value": "b", "confidence": 0.5})
    assert low["ai_confidence"] != "high"
    strong = score_proposal({"proposed_type": "crosswalk", "from_value": "a",
                             "to_value": "b", "confidence": 0.9})
    assert strong["ai_confidence"] != "high"


def test_confirmed_crosswalk_persists_a_value_mapping(monkeypatch):
    _table, spy = _install(monkeypatch)
    mid = _log_crosswalk()
    result = self_learning.accept_mapping(mid)
    assert result["status"] == "accepted"
    cypher, params = spy.find("MERGE (vm:ValueMapping")
    assert cypher is not None, "a confirmed crosswalk must persist a ValueMapping node"
    assert params["method"] == "crosswalk_confirmed"
    assert params["fval"] == "Press Line 1"
    assert params["tval"] == "press line one"
    assert params["concept"] == "machine"
