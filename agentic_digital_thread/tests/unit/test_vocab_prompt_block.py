# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the vocab read-model core (contracts C1 + C2).

C1 — ``catalog.get_all_concepts(include_retired=False)``: the default EXCLUDES
concepts whose graph node carries ``retired = true`` (ADR 0050 D5 conformance);
``include_retired=True`` restores the unfiltered id-set; graph-unavailable
falls back to CORE_CONCEPTS unchanged.

C2 — ``catalog.concepts_prompt_block()``: deterministic ``- <id>: <description>``
lines (sorted by id, truncated, bare ``- <id>`` when no description), one graph
read, CORE_CONCEPT_DEFINITIONS fallback offline; consumed by the
``onboard_agent.map_fields`` prompt in place of the comma-joined id list.

Fully offline: the ONE lazy graph seam (``agents.db.neptune_query``) is
monkeypatched; fixtures use generic concept ids only. Runs under
``--disable-socket``.
"""
from __future__ import annotations

import json

import agents.db as db
import agents.catalog as catalog
from agents.catalog import CORE_CONCEPTS, CORE_CONCEPT_DEFINITIONS

_RETIRED_FILTER = "coalesce(c.retired, false) = false"

# Generic fixture vocabulary (no dataset/customer literals).
_ACTIVE_ROWS = [
    {"id": "pressure-setpoint", "description": "Configured target pressure for a process cell."},
    {"id": "line-speed-limit", "description": None},
]
_RETIRED_ROW = {"id": "legacy-batch-code", "description": "Superseded batch code."}


def _routing_query(calls=None):
    """Fake ``neptune_query`` routing on the retired-filter WHERE clause."""
    def _q(cypher, parameters=None):
        if calls is not None:
            calls.append(cypher)
        assert "MATCH (c:Concept)" in cypher
        if _RETIRED_FILTER in cypher:
            return [dict(r) for r in _ACTIVE_ROWS]
        return [dict(r) for r in _ACTIVE_ROWS + [_RETIRED_ROW]]
    return _q


def _raising_query(*_a, **_k):
    raise RuntimeError("graph unavailable")


# --------------------------------------------------------------------------
# C1 — get_all_concepts retired filter
# --------------------------------------------------------------------------

def test_get_all_concepts_default_excludes_retired(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "neptune_query", _routing_query(calls))
    ids = catalog.get_all_concepts()
    assert "pressure-setpoint" in ids
    assert "line-speed-limit" in ids
    assert "legacy-batch-code" not in ids          # retired ⇒ out by default
    assert set(CORE_CONCEPTS) <= set(ids)          # core merge unchanged
    assert ids == sorted(ids)
    assert len(calls) == 1 and _RETIRED_FILTER in calls[0]


def test_get_all_concepts_include_retired_restores_full_set(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "neptune_query", _routing_query(calls))
    ids = catalog.get_all_concepts(include_retired=True)
    assert "legacy-batch-code" in ids              # today's behavior restored
    assert "pressure-setpoint" in ids
    assert len(calls) == 1 and _RETIRED_FILTER not in calls[0]


def test_get_all_concepts_fallback_returns_core(monkeypatch):
    monkeypatch.setattr(db, "neptune_query", _raising_query)
    assert catalog.get_all_concepts() == sorted(CORE_CONCEPTS)
    assert catalog.get_all_concepts(include_retired=True) == sorted(CORE_CONCEPTS)


# --------------------------------------------------------------------------
# C2 — concepts_prompt_block formatting
# --------------------------------------------------------------------------

def test_prompt_block_lines_sorted_formatted_no_retired(monkeypatch):
    monkeypatch.setattr(db, "neptune_query", _routing_query())
    block = catalog.concepts_prompt_block()
    lines = block.split("\n")
    assert all(line.startswith("- ") for line in lines)
    ids = [line[2:].split(":", 1)[0] for line in lines]
    assert ids == sorted(ids)                      # deterministic, sorted by id
    assert "- pressure-setpoint: Configured target pressure for a process cell." in lines
    assert "- line-speed-limit" in lines           # no description ⇒ bare id
    assert not any(line.startswith("- legacy-batch-code") for line in lines)
    # Core concepts ride along with their curated descriptions.
    assert any(line.startswith("- asset: ") for line in lines)


def test_prompt_block_truncates_descriptions(monkeypatch):
    long_desc = "measured value " * 40             # ≫140 chars
    monkeypatch.setattr(
        db, "neptune_query",
        lambda *_a, **_k: [{"id": "vibration-envelope", "description": long_desc}],
    )
    line = next(
        ln for ln in catalog.concepts_prompt_block().split("\n")
        if ln.startswith("- vibration-envelope: ")
    )
    assert len(line) == len("- vibration-envelope: ") + 140
    short = next(
        ln for ln in catalog.concepts_prompt_block(max_desc_chars=25).split("\n")
        if ln.startswith("- vibration-envelope: ")
    )
    assert len(short) == len("- vibration-envelope: ") + 25


def test_prompt_block_normalizes_multiline_descriptions(monkeypatch):
    monkeypatch.setattr(
        db, "neptune_query",
        lambda *_a, **_k: [{"id": "torque-reading", "description": "spindle\n  torque\tsample"}],
    )
    block = catalog.concepts_prompt_block()
    assert "- torque-reading: spindle torque sample" in block.split("\n")


def test_prompt_block_fallback_uses_core_definitions(monkeypatch):
    monkeypatch.setattr(db, "neptune_query", _raising_query)
    lines = catalog.concepts_prompt_block().split("\n")
    assert len(lines) == len(CORE_CONCEPT_DEFINITIONS)
    for cid, desc in CORE_CONCEPT_DEFINITIONS.items():
        normalized = " ".join(desc.split())[:140]
        assert f"- {cid}: {normalized}" in lines


def test_prompt_block_core_descriptions_stay_curated(monkeypatch):
    """A graph row for a CORE id never overrides the curated definition text."""
    monkeypatch.setattr(
        db, "neptune_query",
        lambda *_a, **_k: [{"id": "asset", "description": "hijacked text"}],
    )
    block = catalog.concepts_prompt_block()
    assert "hijacked text" not in block
    curated = " ".join(CORE_CONCEPT_DEFINITIONS["asset"].split())[:140]
    assert f"- asset: {curated}" in block.split("\n")


# --------------------------------------------------------------------------
# Consumer swap — onboard_agent.map_fields passes the block (not a comma list)
# --------------------------------------------------------------------------

class _FakeStore:
    """Minimal session store double for the map_fields happy path."""

    def __init__(self, item):
        self.item = item

    def get_session(self, system_id):
        return self.item

    def advance_status(self, system_id, from_status, to_status, **fields):
        assert self.item["status"] == from_status
        self.item["status"] = to_status
        self.item.update(fields)


def test_map_fields_prompt_carries_description_block(monkeypatch):
    import agents.onboard_agent as oa

    store = _FakeStore({
        "system_id": "generic-sql", "status": "discovered",
        "connection_type": "PostgreSQL",
        "discovered_tables": [{"name": "orders", "columns": [{"name": "po_number"}]}],
    })
    monkeypatch.setattr(oa, "_store", lambda: store)
    monkeypatch.setattr(db, "neptune_query", _raising_query)  # block → core fallback
    # The removal guard's node read still answers (no node: not a half-removed
    # system); only the vocabulary reads fail here.
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    prompts = []
    monkeypatch.setattr(
        oa, "bedrock_call", lambda prompt, **_k: prompts.append(prompt) or "[]"
    )

    result = oa.map_fields("generic-sql")

    assert "error" not in result
    assert prompts, "map_fields made no model call"
    prompt = prompts[0]
    # The concepts slot now carries '- <id>: <description>' lines …
    assert "Available concept IDs: - asset: " in prompt or "\n- asset: " in prompt
    curated = " ".join(CORE_CONCEPT_DEFINITIONS["asset"].split())[:140]
    assert f"- asset: {curated}" in prompt
    # … and valid JSON handling downstream still advanced the session.
    assert store.item["status"] == "mapped"
    assert json.loads("[]") == store.item["mappings"]


# --------------------------------------------------------------------------
# Source gate — this test file carries no dataset/demo literals (repo pattern)
# --------------------------------------------------------------------------

def test_vocab_prompt_block_functions_have_no_demo_literals():
    """The two touched catalog functions stay generic (no demo/dataset ids)."""
    import inspect
    src = inspect.getsource(catalog.get_all_concepts) + inspect.getsource(
        catalog.concepts_prompt_block
    )
    for token in ("AnyCompany", "Milwaukee", "Bottling", "highbyte"):
        assert token.lower() not in src.lower()
