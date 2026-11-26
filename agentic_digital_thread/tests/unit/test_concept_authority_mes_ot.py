# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MES is authoritative for the OT-native concepts the dashboard OEE / Line State /
Runtime-hours cards resolve against, and the mapper field lists include the new MES
demo-data columns.

Pure in-process unit tests — no AWS, no graph, no DB, no LLM. Carries no dataset/model
literals; the SQL column names asserted here are canonical MES field names.
"""
from __future__ import annotations

from agents.catalog import (
    CONCEPT_AFFINITY,
    CONCEPT_AUTHORITY,
    CORE_CONCEPT_DEFINITIONS,
)

# Concepts the MES now natively supplies from mes_line_status / mes_work_orders.
_MES_OT_CONCEPTS = ("machine-state", "throughput", "machine-speed", "runtime-hours")


def test_mes_is_authority_for_ot_concepts():
    """machine-state/throughput/machine-speed/runtime-hours resolve deterministically to MES."""
    for cid in _MES_OT_CONCEPTS:
        assert CONCEPT_AUTHORITY.get(cid) == "MES", cid


def test_ot_concepts_in_mes_affinity():
    """Authority ⊆ affinity: each OT concept is a member of the MES affinity set."""
    for cid in _MES_OT_CONCEPTS:
        assert cid in CONCEPT_AFFINITY["MES"], cid


def test_oee_score_field_list_includes_oee_percentage():
    """The stored composite maps mes_work_orders.oee_percentage so prefer_stored hits."""
    assert "oee_percentage" in CORE_CONCEPT_DEFINITIONS["oee-score"]


def test_throughput_field_list_includes_throughput_column():
    """The mes_line_status.throughput column maps to the throughput concept."""
    assert "throughput" in CORE_CONCEPT_DEFINITIONS["throughput"]


def test_ot_component_field_names_present():
    """Availability/performance/runtime/speed/state column names are in their field lists."""
    assert "availability" in CORE_CONCEPT_DEFINITIONS["availability"]
    assert "performance" in CORE_CONCEPT_DEFINITIONS["performance"]
    assert "runtime_hours" in CORE_CONCEPT_DEFINITIONS["runtime-hours"]
    assert "line_speed" in CORE_CONCEPT_DEFINITIONS["machine-speed"]
    assert "machine_state" in CORE_CONCEPT_DEFINITIONS["machine-state"]
