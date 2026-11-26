# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for curated-annotation seeding + field_annotation replay.

No AWS, no Neptune, no network. Covers the task's acceptance matrix:

  1. seeding writes deterministic ids — calling twice produces the same
     (mapping_id, discovered_at) keys and identical payloads (modulo the
     ``accepted_at`` audit stamp), so re-seeds overwrite, never duplicate;
  2. demo disabled ⇒ the content module returns ``{}`` and the engine seam
     performs ZERO writes (the store is never touched);
  3. replay — an accepted ``field_annotation`` replays through the SAME write
     acceptance performs (``_apply_field_annotation`` ⇒ ``SET
     f.disambiguation``), is EXCLUDED from ``get_accepted_for_system`` (the
     §i ValueMapping force-fit), and a missing target field is a warning,
     never a provision failure; a value_mapping item still replays as before;
  4. grep gate — the annotation TEXT lives ONLY in
     ``agents/demo/curated_annotations.py``, never in engine files.

Seams: ``self_learning._get_table`` takes an in-memory FakeTable (the
``test_self_learning_lifecycle.py`` double, trimmed); the graph writes are
captured via ``self_learning._neptune_write_loud`` / ``_graph_rows_matched``.
"""
from __future__ import annotations

import pathlib

import agents.demo as demo
import agents.onboard as onboard
import agents.self_learning as self_learning
from agents.demo.curated_annotations import CURATED_FIELD_ANNOTATIONS, curated_annotations


class FakeTable:
    """In-memory stand-in keyed by the real (mapping_id, discovered_at) PK."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}
        self.put_payloads: list[dict] = []

    def __len__(self) -> int:
        return len(self._items)

    def put_item(self, Item: dict) -> None:
        self.put_payloads.append(dict(Item))
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if kwargs.get("IndexName") == "status-index":
            return {"Items": [i for i in items if i.get("status") == values[":status"]]}
        return {"Items": [i for i in items if i.get("mapping_id") == values.get(":mid")]}


_ANNOTATIONS = {  # generic test content — NOT the demo dict
    "sys-a": {
        "orders.line": "identifiers are canonical-cased in this table",
        "orders.qty": "no derived columns exist; compute deltas client-side",
    },
}


# --------------------------------------------------------------------------- #
# 1 — deterministic, idempotent seeding mechanism (engine, content-free)
# --------------------------------------------------------------------------- #

def test_seed_twice_is_idempotent_with_deterministic_ids(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert self_learning.seed_preaccepted_annotations(_ANNOTATIONS) == 2
    first = [dict(p) for p in table.put_payloads]
    assert self_learning.seed_preaccepted_annotations(_ANNOTATIONS) == 2
    second = table.put_payloads[2:]

    assert len(table) == 2  # same PKs overwritten — never duplicated
    for a, b in zip(first, second):
        assert (a["mapping_id"], a["discovered_at"]) == (b["mapping_id"], b["discovered_at"])
        a.pop("accepted_at"), b.pop("accepted_at")  # audit stamp may differ
        assert a == b

    item = first[0]
    assert item["mapping_id"].startswith("fa-")
    assert item["status"] == "accepted"
    assert item["proposed_type"] == "field_annotation"
    assert item["source"] == "curated-seed"
    assert item["concept"] == "schema-annotation"
    assert item["from_system"] == "sys-a"
    assert item["query_context"] in _ANNOTATIONS["sys-a"].values()
    assert item["subject_key"]  # lifecycle keys present (canonical_keys)
    assert item["target_key"]


def test_seed_empty_dict_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        self_learning, "_get_table",
        lambda: (_ for _ in ()).throw(AssertionError("store must not be touched")))
    # _get_table IS called (it's the seam), so use a table that forbids puts:
    table = FakeTable()
    table.put_item = lambda **_kw: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("no puts for empty content"))
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert self_learning.seed_preaccepted_annotations({}) == 0
    assert self_learning.seed_preaccepted_annotations({"sys-a": {}}) == 0


# --------------------------------------------------------------------------- #
# 2 — demo gate: disabled ⇒ zero writes; enabled ⇒ content flows to mechanism
# --------------------------------------------------------------------------- #

def test_demo_disabled_content_empty_and_seam_noop(monkeypatch):
    monkeypatch.setattr(demo, "_override", False)
    assert curated_annotations() == {}

    def forbid():
        raise AssertionError("store must not be touched when demo is disabled")
    monkeypatch.setattr(self_learning, "_get_table", forbid)
    assert onboard._seed_demo_curated_annotations() == 0


def test_demo_enabled_content_shape_and_copy(monkeypatch):
    monkeypatch.setattr(demo, "_override", True)
    content = curated_annotations()
    assert set(content) == {"mes-mil", "mes-det", "mes-cr"}  # canonical demo ids
    for fields in content.values():
        assert "work_orders.line" in fields
    content["mes-mil"]["work_orders.line"] = "mutated"
    assert CURATED_FIELD_ANNOTATIONS["mes-mil"]["work_orders.line"] != "mutated"


def test_seam_routes_demo_content_through_engine_mechanism(monkeypatch):
    monkeypatch.setattr(demo, "_override", True)
    seen: list[dict] = []
    monkeypatch.setattr(self_learning, "seed_preaccepted_annotations",
                        lambda ann: seen.append(ann) or len(ann))
    assert onboard._seed_demo_curated_annotations() == 3
    assert set(seen[0]) == {"mes-mil", "mes-det", "mes-cr"}


# --------------------------------------------------------------------------- #
# 3 — replay: annotations NEVER force-fit into ValueMappings
# --------------------------------------------------------------------------- #

def _fa_item(sys_id: str = "sys-a", field: str = "orders.line") -> dict:
    return {
        "mapping_id": "fa-abc123", "discovered_at": "2026-07-20T00:00:00+00:00",
        "status": "accepted", "proposed_type": "field_annotation",
        "from_system": sys_id, "from_field": field,
        "to_system": sys_id, "to_field": field,
        "from_value": "", "to_value": "",
        "concept": "schema-annotation",
        "query_context": "identifiers are canonical-cased in this table",
        "version": "1",
    }


def _vm_item(sys_id: str = "sys-a") -> dict:
    return {
        "mapping_id": "vm-001", "discovered_at": "2026-07-01T00:00:00+00:00",
        "status": "accepted", "proposed_type": "value_mapping",
        "from_system": sys_id, "from_field": "orders.plant",
        "from_value": "P1", "to_system": "sys-b", "to_field": "sites.code",
        "to_value": "PLANT-1", "concept": "site-id",
    }


def test_get_accepted_for_system_excludes_field_annotations(monkeypatch):
    table = FakeTable()
    for item in (_fa_item(), _vm_item(), _vm_item("sys-other") | {"mapping_id": "vm-002"}):
        table.put_item(Item=item)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    replayable = self_learning.get_accepted_for_system("sys-a")
    assert [i["mapping_id"] for i in replayable] == ["vm-001"]  # vm still replays
    assert all(i.get("proposed_type") != "field_annotation" for i in replayable)

    annotations = self_learning.get_accepted_field_annotations("sys-a")
    assert [i["mapping_id"] for i in annotations] == ["fa-abc123"]
    assert self_learning.get_accepted_field_annotations("sys-other") == []


def test_replay_applies_disambiguation_write_and_no_value_mapping_merge(monkeypatch):
    monkeypatch.setattr(self_learning, "get_accepted_field_annotations",
                        lambda _sid: [_fa_item()])
    monkeypatch.setattr(self_learning, "_graph_rows_matched", lambda _c, _p: 1)
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(self_learning, "_neptune_write_loud",
                        lambda cypher, params=None: writes.append((cypher, params or {})))
    graph_writes: list[str] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda cypher, params=None: graph_writes.append(cypher) or True)

    assert onboard._replay_accepted_field_annotations("sys-a") == 1
    assert len(writes) == 1
    cypher, params = writes[0]
    assert "SET f.disambiguation" in cypher
    assert params["annotation"] == "identifiers are canonical-cased in this table"
    assert params["sid"] == "sys-a" and params["fid"] == "sys-a.orders.line"
    all_cypher = cypher + "".join(graph_writes)
    assert "ValueMapping" not in all_cypher  # NEVER force-fit into a VM node


def test_replay_missing_target_field_warns_and_continues(monkeypatch, caplog):
    items = [_fa_item(field="orders.ghost_column"), _fa_item(field="orders.line")]
    monkeypatch.setattr(self_learning, "get_accepted_field_annotations",
                        lambda _sid: items)
    # first probe misses (0 rows ⇒ the applier's fail-loud ValueError), second hits
    probes = iter([0, 1])
    monkeypatch.setattr(self_learning, "_graph_rows_matched",
                        lambda _c, _p: next(probes))
    monkeypatch.setattr(self_learning, "_neptune_write_loud",
                        lambda _c, _p=None: None)

    with caplog.at_level("WARNING"):
        count = onboard._replay_accepted_field_annotations("sys-a")
    assert count == 1  # the healthy item still replayed
    assert "replay skipped" in caplog.text
    assert "orders.ghost_column" in caplog.text


def test_post_pass_seeds_before_replay_nonfatally(monkeypatch):
    import agents.metamodel as metamodel
    monkeypatch.setattr(metamodel, "write_metamodel", lambda: None)
    monkeypatch.setattr(onboard, "_persist_equipment_value_mappings", lambda: 0)
    order: list[str] = []

    def seed_boom():
        order.append("seed")
        raise RuntimeError("dynamodb unavailable")
    monkeypatch.setattr(onboard, "_seed_demo_curated_annotations", seed_boom)
    monkeypatch.setattr(onboard, "_replay_accepted_field_annotations",
                        lambda _sid: order.append("replay") or 2)

    from agents.models import ProvisionRequest
    result: dict = {}
    onboard._provision_knowledge_post_pass(
        ProvisionRequest(system_id="sys-a", connection_type="PostgreSQL"), result)
    assert order == ["seed", "replay"]  # seed first; its failure is non-fatal
    assert result["self_learned_reapplied"] == 2


# --------------------------------------------------------------------------- #
# 4 — grep gate: annotation TEXT never leaks into engine files
# --------------------------------------------------------------------------- #

def test_no_annotation_text_in_engine_files():
    agents_dir = pathlib.Path(self_learning.__file__).resolve().parent
    fragments = {
        note[:40]
        for fields in CURATED_FIELD_ANNOTATIONS.values()
        for note in fields.values()
    }
    assert fragments  # the demo module does carry content
    for engine_file in ("self_learning.py", "onboard.py"):
        src = (agents_dir / engine_file).read_text(encoding="utf-8")
        for frag in fragments:
            assert frag not in src, f"annotation text leaked into {engine_file}"
        assert "LINE1" not in src and "variance_quantity" not in src
