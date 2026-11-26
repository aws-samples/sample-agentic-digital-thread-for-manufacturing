# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-rule authority resolution is fail-loud, never a coin flip (T1.4).

``set_authority`` encodes the system in the rule id (``ar-{system}-{concept}``)
so a competing rule for the same concept creates a SECOND node rather than
overwriting — and ``GraphAuthorityResolver.authoritative_system`` used to
return ``rows[0]`` from an UNORDERED MATCH: with two rules present, authority
flip-flopped per query (the live pilot-env "suspect authority rules" failure class).

Contract pinned here (consistent with ``_resolve_authority``'s existing
ambiguous-⇒-fail-loud posture): two ACTIVE rules naming DIFFERENT systems for
one concept raise ``MetricResolverError`` naming both; duplicates naming the
SAME system dedupe deterministically; deactivated rules never participate.

Offline: monkeypatched neptune_query seam. GENERIC — synthetic systems only.
"""
from __future__ import annotations

import pytest

import agents.metric_resolver as mr


def _resolver(monkeypatch, rows):
    monkeypatch.setattr(mr, "neptune_query", lambda c, p=None: rows)
    return mr.GraphAuthorityResolver()


# ── Happy paths (pinned so the fail-loud change cannot regress them) ─────────

def test_single_rule_returns_its_system(monkeypatch):
    resolver = _resolver(monkeypatch, [{"sid": "sys-a"}])
    assert resolver.authoritative_system("site") == "sys-a"


def test_no_rule_returns_none(monkeypatch):
    resolver = _resolver(monkeypatch, [])
    assert resolver.authoritative_system("site") is None


def test_duplicate_rules_naming_same_system_dedupe(monkeypatch):
    """Two AuthorityRule nodes both naming sys-a are redundant, not
    ambiguous — deterministic, no raise."""
    resolver = _resolver(monkeypatch, [{"sid": "sys-a"}, {"sid": "sys-a"}])
    assert resolver.authoritative_system("site") == "sys-a"


# ── Fail-loud on genuine ambiguity ───────────────────────────────────────────

def test_two_active_rules_for_different_systems_fail_loud(monkeypatch):
    resolver = _resolver(monkeypatch, [{"sid": "sys-a"}, {"sid": "sys-b"}])
    with pytest.raises(mr.MetricResolverError) as ei:
        resolver.authoritative_system("machine-state")
    msg = str(ei.value)
    assert "machine-state" in msg
    assert "sys-a" in msg and "sys-b" in msg
    # Remediation is actionable: name the lifecycle surface.
    assert "deactivate" in msg


def test_resolve_authority_propagates_ambiguity(monkeypatch):
    """_resolve_authority must NOT swallow the ambiguity into its
    single-system-map fallback — the conflict surfaces to the caller."""
    resolver = _resolver(monkeypatch, [{"sid": "sys-a"}, {"sid": "sys-b"}])
    with pytest.raises(mr.MetricResolverError):
        mr._resolve_authority("machine-state", resolver)


# ── Query-shape contract ─────────────────────────────────────────────────────

def test_authority_query_is_distinct_and_filters_deactivated(monkeypatch):
    """DISTINCT keeps same-system duplicates from LOOKING ambiguous at the
    row level; the status filter keeps deactivated rules out entirely."""
    captured: list[str] = []
    monkeypatch.setattr(mr, "neptune_query",
                        lambda c, p=None: captured.append(c) or [])
    mr.GraphAuthorityResolver().authoritative_system("site")
    assert "DISTINCT" in captured[0]
    assert "coalesce(ar.status, '') <> 'deactivated'" in captured[0]
