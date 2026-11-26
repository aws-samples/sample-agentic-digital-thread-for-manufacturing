# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ingestion system-id normalization gate (offline).

Proves ``log_proposed_mapping`` gate 3d: ``from_system``/``to_system`` are
normalized against the LIVE catalog (read lazily inside the function via
``catalog.get_all()`` — no import-time I/O) using
``system_normalize.normalize_system_id``:

  * display name → canonical id ("Milwaukee MES" → ``mes-mil``);
  * unique system_type → canonical id ("ERP" → ``erp``);
  * ambiguous ("MES" with three MES systems) → passthrough unchanged;
  * unknown (no catalog id/name/type match) → AGENT-sourced proposals are
    DROPPED (junk: model ids, 'None', raw UUIDs…), USER-sourced stored raw;
  * knowledge-graph reference (the ``None`` sentinel) → the JOIN-LIKE proposal
    is DROPPED at the source (returns ``None``, nothing stored);
  * non-join-like types are never dropped by the KG sentinel;
  * best-effort: a raising catalog read falls through to the raw ids.

No AWS / Neptune / LLM / network — the DynamoDB table is an in-memory
``FakeTable`` and the shared catalog is seeded from the test-only demo fixture
(``tests/unit/_demo_catalog.py``) via monkeypatch (auto-restored).
"""
from __future__ import annotations

import pytest

import agents.catalog as catalog_mod
import agents.self_learning as self_learning


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


try:
    from unit._demo_catalog import DEMO_SYSTEMS
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from _demo_catalog import DEMO_SYSTEMS


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
            result = [i for i in items if i.get("subject_key") == values.get(":sk")]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values.get(":status")]
        else:
            result = [i for i in items if i.get("mapping_id") == values.get(":mid")]
        result.sort(key=lambda x: x.get("discovered_at", ""),
                    reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(i) for i in result]}

    def update_item(self, **kwargs) -> None:
        pass

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]


@pytest.fixture
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


@pytest.fixture
def seeded_catalog(monkeypatch):
    """Seed the SHARED in-memory catalog with the demo fixture (auto-restored)."""
    monkeypatch.setattr(catalog_mod.catalog, "_systems",
                        [dict(s) for s in DEMO_SYSTEMS])
    return catalog_mod.catalog


def _log_vm(*, from_system="erp", to_system="cmms", from_value="A", to_value="B"):
    return self_learning.log_proposed_mapping(
        from_system=from_system, from_field="orders.line", from_value=from_value,
        to_system=to_system, to_field="events.line_code", to_value=to_value,
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )


# ===========================================================================
# Normalization to canonical catalog ids (lazy catalog read)
# ===========================================================================
def test_display_name_normalized_to_canonical_id(fake_table, seeded_catalog):
    mid = _log_vm(from_system="Milwaukee MES")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["from_system"] == "mes-mil"
    # Normalization happens BEFORE the canonical key is computed.
    assert "mes-mil" in item["subject_key"]


def test_unique_system_type_normalized(fake_table, seeded_catalog):
    _log_vm(from_system="ERP", to_system="CMMS")
    item = fake_table.all_items()[0]
    assert item["from_system"] == "erp"
    assert item["to_system"] == "cmms"


def test_ambiguous_type_passes_through_unchanged(fake_table, seeded_catalog):
    # Three MES systems in the catalog — a bare "MES" must never be guessed.
    _log_vm(from_system="MES")
    assert fake_table.all_items()[0]["from_system"] == "MES"


def test_canonical_id_passes_through_unchanged(fake_table, seeded_catalog):
    _log_vm(from_system="mes-mil")
    assert fake_table.all_items()[0]["from_system"] == "mes-mil"


def test_unknown_system_agent_sourced_drops_user_sourced_stores(fake_table, seeded_catalog):
    # An AGENT-sourced proposal naming a system that matches
    # NO catalog id/name/type is junk — dropped. A USER-sourced proposal with
    # the same unknown reference stays stored raw (an operator naming a system
    # they know is coming must never be dropped).
    mid = _log_vm(from_system="some-custom-sys")
    assert mid is None
    assert fake_table.all_items() == []

    mid = self_learning.log_proposed_mapping(
        from_system="some-custom-sys", from_field="orders.line", from_value="A",
        to_system="cmms", to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping", source="user",
    )
    assert mid is not None
    assert fake_table.all_items()[0]["from_system"] == "some-custom-sys"


# ===========================================================================
# Self-referential (knowledge-graph) references DROP join-like proposals
# ===========================================================================
def test_knowledge_graph_from_system_drops_join_like(fake_table, seeded_catalog):
    mid = self_learning.log_proposed_mapping(
        from_system="Knowledge Graph", from_field="nodes.id", from_value="",
        to_system="erp", to_field="orders.id", to_value="",
        concept="work-order", confidence=0.8, query_context="ctx",
        proposed_type="join_path",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_digital_thread_to_system_drops_identity_rule(fake_table, seeded_catalog):
    mid = self_learning.log_proposed_mapping(
        from_system="erp", from_field="orders.wo_id", from_value="",
        to_system="digital-thread", to_field="nodes.wo_id", to_value="",
        concept="work-order", confidence=0.8, query_context="ctx",
        proposed_type="identity_rule",
    )
    assert mid is None
    assert fake_table.all_items() == []


def test_non_join_like_type_never_dropped_by_normalization(fake_table, seeded_catalog):
    # field_annotation is not join-like: a knowledge-graph system reference must
    # NOT trigger the drop (only join-like types are gated).
    mid = self_learning.log_proposed_mapping(
        from_system="Knowledge Graph", from_field="work_orders.status", from_value="",
        to_system="Knowledge Graph", to_field="work_orders.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: Enum values OPEN/CLOSED — a type gotcha",
        proposed_type="field_annotation",
    )
    assert mid is not None
    assert len(fake_table.all_items()) == 1


# ===========================================================================
# Best-effort contract: a raising catalog read falls through to raw ids
# ===========================================================================
def test_normalization_failure_stores_raw_ids(fake_table, seeded_catalog, monkeypatch):
    def _boom():
        raise RuntimeError("catalog boom")

    monkeypatch.setattr(catalog_mod.catalog, "get_all", _boom)
    mid = _log_vm(from_system="Milwaukee MES")
    assert mid is not None  # never fail the propose path
    assert fake_table.all_items()[0]["from_system"] == "Milwaukee MES"


# ===========================================================================
# Plant references (rung 4b — live incident 2026-07-14): a from_system naming
# the PLANT a system lives at. Unique plant → normalized to the system id at
# creation; multi-system plant → STORED passthrough (never guess) with a
# review_reason naming the candidate ids — the display-time unknown-system
# verdict then blocks accept until the operator resolves it.
# ===========================================================================
_PLANT_SYSTEMS = [
    {"id": "siteb-athena", "name": "Site B Athena", "plant": "SITEB",
     "system_type": "Data Lake", "status": "onboarded"},
    {"id": "siteb-sitewise", "name": "Site B SiteWise", "plant": "SITEB",
     "system_type": "Historian", "status": "onboarded"},
    {"id": "sitec-hist", "name": "Site C Historian", "plant": "Site C",
     "system_type": "Historian", "status": "onboarded"},
    {"id": "corp-ledger", "name": "Corporate Ledger", "plant": "All Sites",
     "system_type": "ERP", "status": "onboarded"},
]


@pytest.fixture
def plant_catalog(monkeypatch):
    monkeypatch.setattr(catalog_mod.catalog, "_systems",
                        [dict(s) for s in _PLANT_SYSTEMS])
    return catalog_mod.catalog


def _log_annotation(*, from_system, to_system):
    """A non-degenerate field_annotation (passes the 3a degeneracy gate)."""
    return self_learning.log_proposed_mapping(
        from_system=from_system, from_field="segments.status", from_value="",
        to_system=to_system, to_field="segments.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: Enum values OPEN/CLOSED — a type gotcha",
        proposed_type="field_annotation",
    )


def test_unique_plant_reference_normalized_at_creation(fake_table, plant_catalog):
    mid = _log_annotation(from_system="Site C", to_system="Site C")
    assert mid is not None
    item = fake_table.all_items()[0]
    # Creation-time normalization resolved the plant to its ONLY system's id.
    assert item["from_system"] == "sitec-hist"
    assert item["to_system"] == "sitec-hist"
    assert "sitec-hist" in item["subject_key"]


def test_multi_system_plant_stored_with_candidate_review_reason(fake_table,
                                                                plant_catalog):
    # The incident shape: an agent field_annotation naming a TWO-system plant.
    # Not junk (it names a real plant) so it is NOT dropped as no_match — it is
    # stored passthrough as AMBIGUOUS with the candidates in review_reason.
    mid = _log_annotation(from_system="SITEB", to_system="SITEB")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["from_system"] == "SITEB"  # never guess between the two
    reason = item.get("review_reason", "")
    assert "SITEB" in reason
    assert "siteb-athena" in reason and "siteb-sitewise" in reason
    assert "confirm which system" in reason


def test_unique_plant_join_like_normalized_both_sides(fake_table, plant_catalog):
    # The target is an All Sites system: a link from one plant's system to
    # it is in scope for the cross-site gate (ADR-0051 P8).
    mid = self_learning.log_proposed_mapping(
        from_system="Site C", from_field="orders.line", from_value="A",
        to_system="corp-ledger", to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["from_system"] == "sitec-hist"
    assert item["to_system"] == "corp-ledger"


def test_normalized_plant_reference_is_site_scoped(fake_table, plant_catalog):
    # Normalization (3d) runs before the cross-site gate (P8), so a plant
    # reference resolved to its one system is judged by that system's plant:
    # a value mapping to another plant's system is dropped.
    mid = self_learning.log_proposed_mapping(
        from_system="Site C", from_field="orders.line", from_value="A",
        to_system="siteb-athena", to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping",
    )
    assert mid is None
    assert fake_table.all_items() == []
