# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline prompt-shape guards for the W2 mapping-disambiguation (ADR 0020).

These lock the product-capability change: the shared MAPPING_DISAMBIGUATION guidance
carries the eval-exposed hints, and BOTH onboarding map-prompt builders embed it so a
future prompt edit can't silently drop the disambiguation. The builders are pure string
assembly — no AWS, no graph, no DB, no LLM — so they run in the default (unmarked) lane.
"""
from __future__ import annotations

import agents.onboard as onboard
import agents.onboard_agent as onboard_agent
from agents.catalog import MAPPING_DISAMBIGUATION


def test_disambiguation_constant_has_hints():
    """The constant carries the MOST-SPECIFIC principle plus every eval-exposed hint."""
    text = MAPPING_DISAMBIGUATION
    assert "MOST SPECIFIC" in text
    # measure-vs-entity (spare-part quantity is the mro-inventory MEASURE)
    assert "mro-inventory" in text
    assert "spare-part" in text
    assert "inventory-level" in text
    # attribute-vs-entity (work_orders.priority/status are their own concepts)
    assert "criticality" in text
    assert "order-status" in text
    assert "lifecycle-state" in text
    # compatible_asset_type -> asset-class, NOT equipment-id
    assert "asset-class" in text
    assert "equipment-id" in text
    # assets.facility -> facility/site, NOT location
    assert "facility" in text
    assert "location" in text


def test_map_prompt_includes_disambiguation():
    """onboard._build_map_prompt embeds the constant verbatim and the call inputs."""
    prompt = onboard._build_map_prompt(
        "cmms",
        "spare-part, mro-inventory, inventory-level, criticality",
        "spare_parts: quantity_on_hand, reorder_point",
    )
    assert MAPPING_DISAMBIGUATION in prompt
    assert "spare_parts: quantity_on_hand, reorder_point" in prompt
    assert "spare-part, mro-inventory, inventory-level, criticality" in prompt
    assert "system 'cmms'" in prompt


def test_mapfields_prompt_includes_disambiguation_sql():
    """onboard_agent._build_mapfields_prompt carries the constant on the SQL branch."""
    prompt = onboard_agent._build_mapfields_prompt(
        "cmms",
        "spare-part, mro-inventory, criticality",
        "  mro-inventory: On-hand quantity of MRO spare parts.",
        "spare_parts: quantity_on_hand, reorder_point",
        is_mcp=False,
    )
    assert MAPPING_DISAMBIGUATION in prompt
    assert "spare_parts: quantity_on_hand, reorder_point" in prompt
    # SQL branch must NOT carry the OPC UA tag-path block
    assert "CRITICAL OPC UA TAG PATH MAPPING" not in prompt


def test_mapfields_prompt_includes_disambiguation_mcp():
    """The MCP/OPC UA branch still carries the disambiguation alongside the tag-path block."""
    prompt = onboard_agent._build_mapfields_prompt(
        "highbyte",
        "sensor-reading, downtime, production-line",
        "  sensor-reading: A real-time measurement from an OPC UA tag.",
        "Line1Washer: AnyCompany/Milwaukee/Bottling/Line1/Line1Washer/Line1Washer_Down",
        is_mcp=True,
    )
    assert MAPPING_DISAMBIGUATION in prompt
    assert "CRITICAL OPC UA TAG PATH MAPPING" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# S5.2 Wave 3.2 (ADR 0021): the FORCE-map ("map every field") instruction is
# replaced by the two-layer instruction — map genuine matches UNCONDITIONALLY,
# leave non-matching columns UNMAPPED, and never map the generic/audit ignore-set.
# Both builders carry the same instruction (kept in sync).
# ─────────────────────────────────────────────────────────────────────────────
# The generic/audit/surrogate ignore-set the prompt must name as 'do not map'.
_IGNORE_SET = (
    "created_at", "updated_at", "notes", "is_active",
    "entity_type", "source_system", "unit_of_measure",
)
# Must-map tracers that the two-layer instruction must still call out (mapping quality).
_MUST_MAP_TRACERS = (
    "wo_number", "part_number", "asset_tag", "quantity_on_hand",
)


def _assert_two_layer(prompt: str) -> None:
    """Both prompts: two-level instruction + do-not-map ignore-set; NOT the old FORCE-map text."""
    # (a) Two-level instruction.
    assert "TWO-LAYER MAPPING" in prompt
    assert "UNMAPPED" in prompt
    assert "ONLY when it genuinely matches" in prompt
    assert "mappings: []" in prompt
    # (b) Genuine matches stay unconditional — the must-map tracers are still named.
    for tracer in _MUST_MAP_TRACERS:
        assert tracer in prompt, f"must-map tracer '{tracer}' dropped from prompt"
    # (c) The generic/audit ignore-set is listed as 'do not map'.
    assert "DO NOT MAP" in prompt
    for name in _IGNORE_SET:
        assert name in prompt, f"ignore-set column '{name}' missing from prompt"
    # (d) The old FORCE-map ('map every field') instruction is GONE.
    assert "Every field belongs to a concept" not in prompt


def test_map_prompt_two_layer_instruction():
    """onboard._build_map_prompt carries the two-layer instruction + ignore-set (not FORCE-map)."""
    prompt = onboard._build_map_prompt(
        "cmms",
        "spare-part, mro-inventory, work-order, equipment-id",
        "spare_parts: part_number, quantity_on_hand, created_at",
    )
    _assert_two_layer(prompt)


def test_mapfields_prompt_two_layer_instruction_sql():
    """onboard_agent._build_mapfields_prompt (SQL branch) carries the two-layer instruction."""
    prompt = onboard_agent._build_mapfields_prompt(
        "cmms",
        "spare-part, mro-inventory, work-order, equipment-id",
        "  mro-inventory: On-hand quantity of MRO spare parts.",
        "spare_parts: part_number, quantity_on_hand, created_at",
        is_mcp=False,
    )
    _assert_two_layer(prompt)


def test_mapfields_prompt_two_layer_instruction_mcp():
    """The MCP/OPC UA branch also carries the two-layer instruction (builders kept in sync)."""
    prompt = onboard_agent._build_mapfields_prompt(
        "highbyte",
        "sensor-reading, downtime, production-line",
        "  sensor-reading: A real-time measurement from an OPC UA tag.",
        "Line1Washer: AnyCompany/Milwaukee/Bottling/Line1/Line1Washer/Line1Washer_Down",
        is_mcp=True,
    )
    _assert_two_layer(prompt)


# ─────────────────────────────────────────────────────────────────────────────
# C2 (item 5): _run_map_job passes catalog.concepts_prompt_block() output — one
# `- <id>: <desc>` line per non-retired concept — instead of the old comma-join.
# The substitution is the ONLY prompt change: MAPPING_DISAMBIGUATION and the rest
# of the builder stay byte-identical (pinned above; _build_map_prompt unchanged).
# ─────────────────────────────────────────────────────────────────────────────
def test_run_map_job_prompt_carries_concept_description_lines(monkeypatch):
    """The live prompt's concepts string is the id+description block (offline it
    falls back to the curated CORE_CONCEPT_DEFINITIONS — still `- id: desc` lines)."""
    import json as _json

    import agents.onboard as _onboard
    from agents.catalog import concepts_prompt_block

    prompts: list[str] = []

    def _fake(prompt, **_kw):
        prompts.append(prompt)
        return _json.dumps([{
            "field": "assets.serial_number", "table": "assets",
            "mappings": [{"concept": "equipment-id", "role": "identifier",
                          "confidence": 0.95}],
        }])

    monkeypatch.setattr(_onboard, "bedrock_call", _fake)
    monkeypatch.setattr(_onboard.catalog, "get",
                        lambda sid: {"connection_type": "PostgreSQL"})
    tables = [{"name": "assets", "relation_kind": "base_table",
               "schema_revision": "a" * 32,
               "columns": [{"name": "serial_number", "data_type": "varchar"}]}]
    job_id = "promptshape"
    _onboard._map_jobs.pop(job_id, None)
    _onboard._run_map_job(job_id, "example-sys", tables)
    job = _onboard._map_jobs.pop(job_id)
    assert job["status"] == "done", job
    assert prompts, "no map prompt captured"
    prompt = prompts[0]
    # The deterministic description block rides in the prompt verbatim…
    assert concepts_prompt_block() in prompt
    # …as `- <id>: <desc>` lines (description injection), e.g. a core concept:
    assert "- asset: " in prompt
    # …and the disambiguation guidance stays byte-identical alongside it.
    assert MAPPING_DISAMBIGUATION in prompt
