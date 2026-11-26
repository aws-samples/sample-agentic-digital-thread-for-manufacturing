# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Thin pytest wrapper around the read-only onboarding-graph audit.

These tests run against a *reachable* Neptune graph that has >=1 onboarded
system. When no such graph is available (no ``GRAPH_IDENTIFIER`` / credentials,
or nothing onboarded), ``pick_target_system()`` returns ``None`` and the suite
**skips** rather than fails — mirroring the credential-gated ``ADT_PASSWORD``
skip philosophy in ``tests/e2e/conftest.py`` so the suite stays green in
credential-free CI.

All assertions are read-only; the heavy lifting lives in
``tests/audit/audit_onboarding_graph.py``.
"""

from __future__ import annotations

import pytest

from tests.audit.audit_onboarding_graph import (
    assert_authority,
    assert_field_concept_mappings,
    assert_joins,
    assert_no_orphaned_properties,
    assert_property_count_degree,
    assert_property_ownership,
    pick_i3x_target_system,
    pick_target_system,
)

pytestmark = pytest.mark.audit


@pytest.fixture(scope="session")
def audit_target() -> str:
    """Pick a system that has MAPS_TO edges, or skip if the graph isn't ready.

    Skips (rather than fails) when no graph is reachable / nothing is onboarded,
    matching the ``ADT_PASSWORD`` skip pattern in the e2e conftest.
    """
    target = pick_target_system()
    if target is None:
        pytest.skip(
            "no reachable Neptune graph with an onboarded system "
            "(no MAPS_TO edges found) — skipping graph-shape audit"
        )
    return target


def test_field_concept_mappings(audit_target: str) -> None:
    report = assert_field_concept_mappings(audit_target)
    assert report["pass"], (
        f"no field->concept mappings for system '{audit_target}': {report}"
    )


def test_authority(audit_target: str) -> None:
    report = assert_authority(audit_target)
    assert report["pass"], (
        f"no authority fact resolvable to a mapped concept for system "
        f"'{audit_target}': {report}"
    )


def test_joins(audit_target: str) -> None:
    report = assert_joins(audit_target)
    assert report["pass"], (
        f"no JoinPath touching system '{audit_target}': {report}"
    )


@pytest.fixture(scope="session")
def i3x_audit_target() -> str:
    """Pick a system owning :Property nodes (the i3X telemetry layer), or skip.

    Property nodes are written only by the i3X walker
    (``agents/onboard_i3x.py::provision_i3x_hierarchy``); relational-only
    deployments have none, so these tests skip rather than fail — same
    philosophy as ``audit_target``.
    """
    target = pick_i3x_target_system()
    if target is None:
        pytest.skip(
            "no reachable Neptune graph with an onboarded i3X-style system "
            "(no :Property nodes found) — skipping property-layer audit"
        )
    return target


def test_property_ownership(i3x_audit_target: str) -> None:
    """(d1) Every Property of the i3X system has an owning Asset via HAS_PROPERTY."""
    report = assert_property_ownership(i3x_audit_target)
    assert report["pass"], (
        f"{report['unowned_count']} of {report['property_count']} Properties of "
        f"system '{i3x_audit_target}' lack an owning Asset via HAS_PROPERTY: {report}"
    )


def test_property_count_matches_degree(i3x_audit_target: str) -> None:
    """(d2) Asset.property_count equals the HAS_PROPERTY out-degree per asset."""
    report = assert_property_count_degree(i3x_audit_target)
    assert report["pass"], (
        f"{report['mismatch_count']} of {report['asset_count']} Assets of system "
        f"'{i3x_audit_target}' have property_count != HAS_PROPERTY out-degree "
        f"({report['assets_missing_stamp']} missing the stamp entirely): {report}"
    )


def test_no_orphaned_properties(audit_target: str) -> None:
    """(d3) No orphaned Property graph-wide (reset/offboard cleaned up fully).

    Gated on ``audit_target`` only for graph reachability — the check itself
    is graph-wide and passes trivially when no Property nodes exist.
    """
    report = assert_no_orphaned_properties()
    assert report["pass"], (
        f"{report['orphaned_property_count']} orphaned :Property node(s) with no "
        f"owning Asset — an interrupted reset/offboard or a writer that skipped "
        f"HAS_PROPERTY: {report}"
    )
