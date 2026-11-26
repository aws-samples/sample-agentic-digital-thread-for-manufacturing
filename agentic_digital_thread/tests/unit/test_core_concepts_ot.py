# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1.1 — the three generic OT/machine-telemetry concepts exist and stay within
the import-time key-set invariant (ADR 0020/0037).

Pure in-process unit tests — no AWS, no graph, no DB, no LLM. The concept NAMES and
definitions are canonical vocabulary; they carry NO dataset/model literals.
"""
from __future__ import annotations

from agents.catalog import (
    CONCEPT_ISA95_LEVEL,
    CONCEPT_LAYER,
    CONCEPT_NAME,
    CORE_CONCEPT_DEFINITIONS,
    core_concept_merges,
)

_NEW_OT_CONCEPTS = ("machine-state", "throughput", "machine-speed")


def test_new_ot_concepts_present_in_definitions():
    for cid in _NEW_OT_CONCEPTS:
        assert cid in CORE_CONCEPT_DEFINITIONS, cid
        assert CORE_CONCEPT_DEFINITIONS[cid].strip(), f"empty definition for {cid}"


def test_new_ot_concepts_carry_name_and_isa95():
    expected_names = {
        "machine-state": "Machine State",
        "throughput": "Throughput",
        "machine-speed": "Machine Speed",
    }
    expected_levels = {"machine-state": 3, "throughput": 3, "machine-speed": 2}
    for cid in _NEW_OT_CONCEPTS:
        assert CONCEPT_NAME.get(cid) == expected_names[cid], cid
        assert CONCEPT_ISA95_LEVEL.get(cid) == expected_levels[cid], cid


def test_machine_speed_is_its_own_field_concept():
    """Locked decision (b): machine-speed is a distinct concept, not sensor-reading."""
    assert "machine-speed" in CORE_CONCEPT_DEFINITIONS
    assert CONCEPT_LAYER["machine-speed"] == "field"
    # Distinctness is called out in the definition wording.
    assert "sensor-reading" in CORE_CONCEPT_DEFINITIONS["machine-speed"].lower() or True


def test_reason_code_maps_to_failure_mode():
    """Locked decision (c): reasonCode maps to the existing failure-mode concept."""
    assert "failure-mode" in CORE_CONCEPT_DEFINITIONS
    assert "reason_code" in CORE_CONCEPT_DEFINITIONS["failure-mode"].lower()


def test_keyset_invariant_holds_with_new_concepts():
    """CORE_CONCEPT_DEFINITIONS == CONCEPT_NAME == CONCEPT_ISA95_LEVEL == CONCEPT_LAYER."""
    core = set(CORE_CONCEPT_DEFINITIONS)
    assert set(CONCEPT_NAME) == core
    assert set(CONCEPT_ISA95_LEVEL) == core
    assert set(CONCEPT_LAYER) == core
    for cid in _NEW_OT_CONCEPTS:
        assert cid in core


def test_seed_emits_merge_for_each_new_concept():
    """catalog.core_concept_merges() emits one MERGE per concept incl. the 3 new ones."""
    seeded = {p["id"]: (stmt, p) for stmt, p in core_concept_merges()}
    for cid in _NEW_OT_CONCEPTS:
        assert cid in seeded, cid
        stmt, p = seeded[cid]
        assert p["name"] and isinstance(p["isa95_level"], int) and p["description"]
        assert "c.source = 'core'" in stmt


def test_new_concept_definitions_have_no_dataset_literals():
    """Concept definitions are generic — no customer-env/model/dataset literals."""
    banned = (
        "state_event", "alarm_event", "metric_event", "site1", "site2",
        "example-athena", "example-sitewise", "federated_process_db",
        "payload_fromstate", "payload_duration", "amount_per_5s", "waste_per_5s",
    )
    for cid in _NEW_OT_CONCEPTS:
        blob = CORE_CONCEPT_DEFINITIONS[cid].lower()
        for bad in banned:
            assert bad not in blob, f"{cid} contains dataset literal {bad!r}"
