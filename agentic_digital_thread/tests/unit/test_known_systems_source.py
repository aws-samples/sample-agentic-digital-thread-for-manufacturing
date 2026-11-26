# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``known_systems()`` — process-agnostic known-systems source (offline).

Why: the unknown-system gate (3d in
``self_learning.log_proposed_mapping``) and the runtime insight extractor's
system detection read ``catalog.get_all()`` directly — but the ``catalog``
singleton is hydrated only in the ECS app process. In the AgentCore runtime
process it is EMPTY, and gate 3d exempts empty catalogs (fail-safe store-raw),
so both were INERT exactly where the junk originates. ``known_systems()``
makes them live in ANY process:

  * hydrated catalog non-empty (ECS path) → served as-is, ZERO graph reads;
  * catalog empty (runtime path) → read-only Neptune ``System`` read via the
    dedicated ``_known_systems_graph_read`` seam, TTL-cached in-process
    (positive ~300s / negative ~60s);
  * graph failure → ``[]`` (swallowed, negative-cached) — the gates fall back
    to store-raw exactly as before F6.

Contract pinned here: the gate is PROVED LIVE via the graph path (empty
catalog + graph systems → agent junk drops), failure → store raw, the TTL
cache serves the second call without a second read, and the ECS path never
touches the graph. Plus a source tripwire that the runtime_app insight call
site uses ``known_systems()`` (not a bare ``catalog.get_all()``).

No AWS / Neptune / LLM / network — the unit conftest patches the graph seam to
raise by default and resets the TTL cache around every test; tests here
re-patch the seam with counting fakes. ``FakeTable`` per
``test_self_learning_unknown_system_gate.py`` conventions.
"""
from __future__ import annotations

import pathlib

import pytest

import agents.catalog as catalog_mod
import agents.self_learning as self_learning


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


# The graph-read shape: {"id", "name", "system_type"} per System node.
GRAPH_SYSTEMS = [
    {"id": "erp", "name": "ERP", "system_type": "ERP"},
    {"id": "mes-mil", "name": "Milwaukee MES", "system_type": "MES"},
    {"id": "cmms", "name": "CMMS", "system_type": "CMMS"},
]

JUNK_SYSTEM = "us.amazon.nova-2-lite-v1:0"  # live junk fixture (handoff H3)


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
def empty_catalog(monkeypatch):
    """Force the singleton to the AgentCore-runtime state (never hydrated)."""
    monkeypatch.setattr(catalog_mod.catalog, "_systems", [])


class GraphReadCounter:
    """Counting graph-seam fake — returns ``systems`` or raises ``error``."""

    def __init__(self, systems=None, error: Exception | None = None):
        self.systems = systems or []
        self.error = error
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(s) for s in self.systems]


def _log_agent_junk_join():
    """An agent-sourced value_mapping naming a junk system (gate-3d bait)."""
    return self_learning.log_proposed_mapping(
        from_system=JUNK_SYSTEM, from_field="orders.line", from_value="A",
        to_system="cmms", to_field="events.line_code", to_value="B",
        concept="production-line", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping", source="agent",
    )


# ===========================================================================
# THE fix: empty catalog + graph systems → gate 3d is LIVE (junk drops)
# ===========================================================================
def test_gate_3d_live_via_graph_path_drops_agent_junk(fake_table, empty_catalog,
                                                      monkeypatch):
    counter = GraphReadCounter(systems=GRAPH_SYSTEMS)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)

    mid = _log_agent_junk_join()

    assert counter.calls == 1          # the graph, not the empty singleton
    assert mid is None                 # junk dropped — gate live in-runtime
    assert fake_table.all_items() == []


def test_gate_3d_via_graph_path_normalizes_display_names(fake_table,
                                                         empty_catalog,
                                                         monkeypatch):
    # Not just dropping: the 3d normalization also works off the graph read.
    monkeypatch.setattr(self_learning, "_known_systems_graph_read",
                        GraphReadCounter(systems=GRAPH_SYSTEMS))
    mid = self_learning.log_proposed_mapping(
        from_system="Milwaukee MES", from_field="ops.lot", from_value="A",
        to_system="ERP", to_field="orders.lot_no", to_value="B",
        concept="lot", confidence=0.9, query_context="ctx",
        proposed_type="value_mapping", source="agent",
    )
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["from_system"] == "mes-mil"
    assert item["to_system"] == "erp"


# ===========================================================================
# Fail-safe preserved: graph failure → [] → store raw (exactly pre-F6)
# ===========================================================================
def test_graph_failure_stores_agent_junk_raw(fake_table, empty_catalog,
                                             monkeypatch):
    monkeypatch.setattr(self_learning, "_known_systems_graph_read",
                        GraphReadCounter(error=RuntimeError("graph down")))

    mid = _log_agent_junk_join()

    assert mid is not None  # no basis to judge — store raw, never drop
    assert fake_table.all_items()[0]["from_system"] == JUNK_SYSTEM


def test_known_systems_never_raises_on_graph_failure(empty_catalog, monkeypatch):
    monkeypatch.setattr(self_learning, "_known_systems_graph_read",
                        GraphReadCounter(error=RuntimeError("graph down")))
    assert self_learning.known_systems() == []


# ===========================================================================
# TTL cache: second call served from cache — exactly ONE graph read
# ===========================================================================
def test_second_call_hits_cache_one_graph_read(empty_catalog, monkeypatch):
    counter = GraphReadCounter(systems=GRAPH_SYSTEMS)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)

    first = self_learning.known_systems()
    second = self_learning.known_systems()

    assert counter.calls == 1
    assert first == second == GRAPH_SYSTEMS


def test_negative_result_is_cached_briefly(empty_catalog, monkeypatch):
    counter = GraphReadCounter(error=RuntimeError("graph down"))
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)

    assert self_learning.known_systems() == []
    assert self_learning.known_systems() == []

    assert counter.calls == 1  # failure negative-cached — no hammering


def test_cache_expires_after_ttl(empty_catalog, monkeypatch):
    counter = GraphReadCounter(systems=GRAPH_SYSTEMS)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)

    now = 1000.0
    monkeypatch.setattr(self_learning.time, "monotonic", lambda: now)
    self_learning.known_systems()
    assert counter.calls == 1

    # Just before expiry: cached. Just after: re-read.
    now = 1000.0 + self_learning.KNOWN_SYSTEMS_TTL_SECONDS - 1
    self_learning.known_systems()
    assert counter.calls == 1
    now = 1000.0 + self_learning.KNOWN_SYSTEMS_TTL_SECONDS + 1
    self_learning.known_systems()
    assert counter.calls == 2


def test_negative_ttl_is_shorter_retry_window(empty_catalog, monkeypatch):
    counter = GraphReadCounter(error=RuntimeError("graph down"))
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)

    now = 1000.0
    monkeypatch.setattr(self_learning.time, "monotonic", lambda: now)
    self_learning.known_systems()
    assert counter.calls == 1

    now = 1000.0 + self_learning.KNOWN_SYSTEMS_NEGATIVE_TTL_SECONDS + 1
    self_learning.known_systems()
    assert counter.calls == 2  # failed read retried after the SHORT window


# ===========================================================================
# ECS path: catalog non-empty → served as-is, ZERO graph reads
# ===========================================================================
def test_hydrated_catalog_serves_without_graph_read(monkeypatch):
    counter = GraphReadCounter(systems=GRAPH_SYSTEMS)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)
    monkeypatch.setattr(catalog_mod.catalog, "_systems",
                        [dict(s) for s in GRAPH_SYSTEMS])

    out = self_learning.known_systems()

    assert counter.calls == 0
    assert [s["id"] for s in out] == ["erp", "mes-mil", "cmms"]


def test_gate_3d_ecs_path_unchanged_zero_graph_reads(fake_table, monkeypatch):
    counter = GraphReadCounter(systems=GRAPH_SYSTEMS)
    monkeypatch.setattr(self_learning, "_known_systems_graph_read", counter)
    monkeypatch.setattr(catalog_mod.catalog, "_systems",
                        [dict(s) for s in GRAPH_SYSTEMS])

    mid = _log_agent_junk_join()

    assert counter.calls == 0   # hydrated catalog — the graph is never touched
    assert mid is None          # and the gate still drops the junk


# ===========================================================================
# Wiring tripwire: the runtime_app insight call site uses known_systems()
# ===========================================================================
def test_runtime_app_insight_call_site_uses_known_systems():
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agents" / "runtime_app.py").read_text(encoding="utf-8")
    call = "insights = _extract_correlation_insights(full_text, "
    assert call + "known_systems())" in src, (
        "runtime_app must feed the insight extractor from "
        "self_learning.known_systems() (live in the AgentCore runtime process)"
    )
    assert call + "catalog.get_all())" not in src, (
        "bare catalog.get_all() at the insight call site is INERT in the "
        "AgentCore runtime process (empty singleton) — regression of F6"
    )
