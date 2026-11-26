# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the concept-coverage analytics route (agents/graph_analytics.py).

Covers the [F5] S0.1-audit fix: a ``MAPS_TO`` edge missing its ``role`` must fail
loud (no silent ``"identifier"`` fallback that would mis-bucket the mapping). Every
mapping edge carries a role since the S5.2 two-layer re-onboard, so a missing role
is a graph data-integrity violation, not a legacy case to paper over.
"""
from unittest.mock import patch

import pytest

from agents import graph_analytics


def _three_calls(mappings, concepts, systems):
    """side_effect for the three neptune_query calls in concept_coverage()."""
    return [mappings, concepts, systems]


def test_concept_coverage_builds_roles_from_edges():
    mappings = [
        {"concept": "spare-part", "concept_name": "Spare Part", "level": 3,
         "system_id": "cmms-1", "system_name": "CMMS", "role": "identifier", "confidence": 0.9},
        {"concept": "spare-part", "concept_name": "Spare Part", "level": 3,
         "system_id": "cmms-1", "system_name": "CMMS", "role": "measure", "confidence": 0.8},
    ]
    concepts = [{"id": "spare-part", "name": "Spare Part", "level": 3, "source": "core"}]
    systems = [{"id": "cmms-1", "name": "CMMS"}]
    with patch.object(graph_analytics, "neptune_query",
                      side_effect=_three_calls(mappings, concepts, systems)):
        out = graph_analytics.concept_coverage()

    assert out["total_concepts"] == 1
    assert out["unmapped_count"] == 0
    cov = out["coverage"][0]
    assert cov["concept"] == "spare-part" and cov["system"] == "cmms-1"
    roles = {r["role"]: r["confidence"] for r in cov["roles"]}
    assert roles == {"identifier": 0.9, "measure": 0.8}


def test_concept_coverage_fails_loud_on_missing_role():
    """[F5] a MAPS_TO edge with no role -> RuntimeError, not a silent 'identifier'."""
    mappings = [
        {"concept": "spare-part", "concept_name": "Spare Part", "level": 3,
         "system_id": "cmms-1", "system_name": "CMMS", "role": None, "confidence": 0.9},
    ]
    concepts = [{"id": "spare-part", "name": "Spare Part", "level": 3, "source": "core"}]
    systems = [{"id": "cmms-1", "name": "CMMS"}]
    with patch.object(graph_analytics, "neptune_query",
                      side_effect=_three_calls(mappings, concepts, systems)):
        with pytest.raises(RuntimeError, match="missing required role"):
            graph_analytics.concept_coverage()
