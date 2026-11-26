# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unknown-system ingestion drop — live-junk regression fixtures (offline).

Found 2026-07-13: the Demo env's ACCEPTED set contained
proposals whose ``from_system`` was a model id, the string ``'None'``, a raw
asset UUID, an Athena catalog/table name, or a site code — none of them catalog
systems. Gate 3d in ``log_proposed_mapping`` now DROPS an AGENT-sourced
(``source != 'user'``) join-like or ``field_annotation`` proposal whose system
reference matches NO catalog id/name/type (``system_normalize.resolve_system_id``
verdict ``no_match``). Contract pinned here, with the handoff's live junk
values as regression fixtures:

  * agent-sourced join-like (value_mapping / identity_rule / join_path) naming
    a junk system → DROPPED (returns ``None``, nothing stored);
  * agent-sourced field_annotation naming a junk system → DROPPED;
  * the SAME junk values user-sourced → STORED raw (operator knowledge wins);
  * ambiguous "MES" (three MES systems in the catalog) → STORED (the
    ``ambiguous`` verdict is NOT ``no_match`` — never drop a legitimate
    never-guess passthrough);
  * EMPTY catalog → stored raw (no basis to judge — base mode pre-onboarding);
  * field_annotation with from==to on a KNOWN system → STORED (the intended
    self-referential annotation shape; only KG/digital-thread sentinel drops).

No AWS / Neptune / LLM / network — in-memory ``FakeTable`` + the test-only demo
catalog fixture via monkeypatch (auto-restored), per
``test_self_learning_system_normalization.py`` conventions.
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


# The handoff's live junk from_system values (Demo env accepted-set noise):
# a Bedrock model id, the string 'None', a raw asset UUID, an Athena
# catalog.table, a bare table name, and a site code.
JUNK_SYSTEMS = [
    "us.amazon.nova-2-lite-v1:0",
    "None",
    "2baf4c3a-0000-0000-0000-000000000000",
    "AwsDataCatalog.nan_process_data_global",
    "segment_record_details_raw",
    "SITE1",
]

JOIN_LIKE_TYPES = ["value_mapping", "identity_rule", "join_path"]


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


def _log_join_like(proposed_type, *, from_system="erp", to_system="cmms",
                   source="agent"):
    """A well-shaped join-like proposal (fields pass the 3e shape-gate)."""
    return self_learning.log_proposed_mapping(
        from_system=from_system, from_field="orders.line", from_value="A",
        to_system=to_system, to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type=proposed_type, source=source,
    )


def _log_annotation(*, from_system="erp", to_system="erp", source="agent"):
    """A non-degenerate field_annotation (passes the 3a degeneracy gate)."""
    return self_learning.log_proposed_mapping(
        from_system=from_system, from_field="work_orders.status", from_value="",
        to_system=to_system, to_field="work_orders.status", to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: Enum values OPEN/CLOSED — a type gotcha",
        proposed_type="field_annotation", source=source,
    )


# ===========================================================================
# Agent-sourced junk systems DROP — join-like and field_annotation
# ===========================================================================
@pytest.mark.parametrize("junk", JUNK_SYSTEMS)
@pytest.mark.parametrize("ptype", JOIN_LIKE_TYPES)
def test_agent_join_like_with_junk_from_system_drops(fake_table, seeded_catalog,
                                                     junk, ptype):
    mid = _log_join_like(ptype, from_system=junk)
    assert mid is None
    assert fake_table.all_items() == []


@pytest.mark.parametrize("junk", JUNK_SYSTEMS)
def test_agent_join_like_with_junk_to_system_drops(fake_table, seeded_catalog, junk):
    mid = _log_join_like("value_mapping", to_system=junk)
    assert mid is None
    assert fake_table.all_items() == []


@pytest.mark.parametrize("junk", JUNK_SYSTEMS)
def test_agent_field_annotation_with_junk_system_drops(fake_table, seeded_catalog,
                                                       junk):
    mid = _log_annotation(from_system=junk, to_system=junk)
    assert mid is None
    assert fake_table.all_items() == []


# ===========================================================================
# The SAME junk values user-sourced STORE raw (operator knowledge wins)
# ===========================================================================
@pytest.mark.parametrize("junk", JUNK_SYSTEMS)
def test_user_join_like_with_junk_system_stores_raw(fake_table, seeded_catalog, junk):
    mid = _log_join_like("value_mapping", from_system=junk, source="user")
    assert mid is not None
    assert fake_table.all_items()[0]["from_system"] == junk


@pytest.mark.parametrize("junk", JUNK_SYSTEMS)
def test_user_field_annotation_with_junk_system_stores_raw(fake_table,
                                                           seeded_catalog, junk):
    mid = _log_annotation(from_system=junk, to_system=junk, source="user")
    assert mid is not None
    assert fake_table.all_items()[0]["from_system"] == junk


# ===========================================================================
# Ambiguous is NOT no-match: "MES" (×3 in catalog) still stores, agent-sourced
# ===========================================================================
def test_ambiguous_mes_agent_sourced_still_stores(fake_table, seeded_catalog):
    mid = _log_join_like("value_mapping", from_system="MES")
    assert mid is not None
    assert fake_table.all_items()[0]["from_system"] == "MES"


# ===========================================================================
# Empty catalog: no basis to judge — agent-sourced junk stores raw
# ===========================================================================
def test_empty_catalog_stores_agent_junk_raw(fake_table, monkeypatch):
    monkeypatch.setattr(catalog_mod.catalog, "_systems", [])
    mid = _log_join_like("value_mapping",
                         from_system="us.amazon.nova-2-lite-v1:0")
    assert mid is not None
    assert fake_table.all_items()[0]["from_system"] == "us.amazon.nova-2-lite-v1:0"


# ===========================================================================
# field_annotation from==to on a KNOWN system is the INTENDED shape — stored
# ===========================================================================
def test_annotation_same_known_system_both_sides_stores(fake_table, seeded_catalog):
    mid = _log_annotation(from_system="erp", to_system="erp")
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["from_system"] == "erp"
    assert item["to_system"] == "erp"
