# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the S4.3 concept-affinity prior (ADR 0007).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane.
"""
from __future__ import annotations

import pytest

from agents.catalog import (
    CONCEPT_AFFINITY,
    CONCEPT_AUTHORITY,
    CORE_CONCEPT_DEFINITIONS,
    _validate_concept_affinity,
    missing_expected,
    system_types_for,
)


def test_affinity_concept_ids_are_known():
    """Every concept id in every affinity set is a known core concept."""
    for system_type, concepts in CONCEPT_AFFINITY.items():
        for concept in concepts:
            assert concept in CORE_CONCEPT_DEFINITIONS, (
                f"{concept!r} in CONCEPT_AFFINITY[{system_type!r}] is not a core concept"
            )


def test_authority_subset_of_affinity():
    """Every authoritative owner of a concept is a member of that type's affinity set."""
    for concept, system_type in CONCEPT_AUTHORITY.items():
        if system_type is None:
            continue
        assert concept in CONCEPT_AFFINITY[system_type], (
            f"authority {concept!r}->{system_type!r} not in CONCEPT_AFFINITY[{system_type!r}]"
        )


def test_validate_raises_on_unknown_concept():
    """A bogus concept id in an affinity set is rejected at validation."""
    with pytest.raises(ValueError):
        _validate_concept_affinity(
            {"MES": frozenset({"not-a-concept"})},
            {},
            CORE_CONCEPT_DEFINITIONS,
        )


def test_validate_raises_on_authority_not_in_affinity():
    """An authority owner absent from its affinity set is rejected at validation."""
    with pytest.raises(ValueError):
        _validate_concept_affinity(
            {"MES": frozenset({"equipment-id"})},
            {"production-run": "MES"},  # owned by MES but missing from affinity
            CORE_CONCEPT_DEFINITIONS,
        )


def test_system_types_for_sorted_and_memoized():
    """Locked representative lookups are sorted; results are memoized (same object)."""
    assert system_types_for("downtime") == ("CMMS", "Historian", "MES")
    assert system_types_for("work-order") == ("CMMS", "ERP", "MES")
    assert system_types_for("maintenance-event") == ("CMMS",)
    assert system_types_for("sensor-reading") == ("Historian",)
    # sorted invariant
    result = system_types_for("downtime")
    assert list(result) == sorted(result)
    # memoized — second call returns the identical cached tuple object
    assert system_types_for("downtime") is result


def test_system_types_for_unassigned_returns_empty():
    """Deliberately unassigned concepts return ()."""
    for concept in ("personnel", "facility", "site", "location"):
        assert system_types_for(concept) == ()


def test_missing_expected_sorted_and_set_difference():
    """Set-difference is sorted; unknown type -> []; accepts any iterable."""
    cmms_expected = sorted(CONCEPT_AFFINITY["CMMS"])
    expected_minus_one = [c for c in cmms_expected if c != "equipment-id"]

    # accepts a set
    assert missing_expected("CMMS", {"equipment-id"}) == expected_minus_one
    # accepts a list
    assert missing_expected("CMMS", ["equipment-id"]) == expected_minus_one
    # accepts a frozenset
    assert missing_expected("CMMS", frozenset({"equipment-id"})) == expected_minus_one
    # result is sorted
    result = missing_expected("CMMS", set())
    assert result == sorted(result) == cmms_expected
    # unknown type -> []
    assert missing_expected("UNKNOWN", set()) == []


def test_historian_can_provide_machine_state_without_affinity_regression():
    """OT history can own machine state; representative prior sets stay intact."""
    assert "machine-state" in CONCEPT_AFFINITY["Historian"]
    assert system_types_for("machine-state") == ("Historian", "MES")
    assert system_types_for("downtime") == ("CMMS", "Historian", "MES")
    assert system_types_for("sensor-reading") == ("Historian",)
    assert "machine-state" not in CONCEPT_AFFINITY["ERP"]
