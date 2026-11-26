# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``agents.learning_health`` (ADR-0052 B1).

The shared stat-builder module behind ``GET /api/admin/learning/health``:
staging census (bounded prefix scan — staged rows are NEVER pending),
usage-ledger rollup (legacy rows fold as ``"tool-text"``), pure pending-age
and quarantine stats, integrity-mode echo, and the ``build_health``
assembler. Every backend seam is a fake table or a monkeypatched function —
no AWS, no network.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

import agents.artifact_usage as artifact_usage
import agents.concept_authority as concept_authority
import agents.learning_health as lh
import agents.self_learning as self_learning


# --------------------------------------------------------------------------- #
# Fakes (the test_artifact_usage.py RecordingTable idiom)
# --------------------------------------------------------------------------- #
class RecordingTable:
    """Fake proposals table: paged scan spy, optional failures."""

    def __init__(self, scan_pages=None, *, raise_on_scan=False):
        self._scan_pages = scan_pages or [[]]
        self._raise_on_scan = raise_on_scan
        self.scan_calls = []

    def scan(self, **kwargs):
        if self._raise_on_scan:
            raise RuntimeError("scan boom")
        self.scan_calls.append(kwargs)
        page_idx = len(self.scan_calls) - 1
        items = (self._scan_pages[page_idx]
                 if page_idx < len(self._scan_pages) else [])
        out = {"Items": items}
        if page_idx < len(self._scan_pages) - 1:
            out["LastEvaluatedKey"] = {"mapping_id": f"page-{page_idx}"}
        return out


class AlwaysMoreTable(RecordingTable):
    """Never-ending pagination — exercises the page cap."""

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": [], "LastEvaluatedKey": {"mapping_id": "again"}}


def _staged(subject: str) -> dict:
    return {"mapping_id": f"staged#{subject}-uuid", "subject_key": f"staged#{subject}",
            "discovered_at": "2026-07-20T00:00:00+00:00"}


# --------------------------------------------------------------------------- #
# staging_census
# --------------------------------------------------------------------------- #
def test_staging_census_counts_rows_and_distinct_subjects(monkeypatch):
    rows = [_staged("subj-a"), _staged("subj-a"), _staged("subj-b")]
    table = RecordingTable(scan_pages=[rows])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert lh.staging_census() == {"rows": 3, "distinct_subjects": 2}
    # The scan MUST be a prefix scan on the staged# mapping_id — never the
    # status GSI (staged rows carry no status attribute by design).
    scan = table.scan_calls[0]
    assert "begins_with(mapping_id, :prefix)" in scan["FilterExpression"]
    assert scan["ExpressionAttributeValues"][":prefix"] == self_learning._STAGED_PREFIX


def test_staging_census_paginates_across_pages(monkeypatch):
    table = RecordingTable(scan_pages=[[_staged("a")], [_staged("b")]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert lh.staging_census() == {"rows": 2, "distinct_subjects": 2}
    assert len(table.scan_calls) == 2
    assert "ExclusiveStartKey" in table.scan_calls[1]


def test_staging_census_scan_is_page_capped(monkeypatch):
    table = AlwaysMoreTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert lh.staging_census() == {"rows": 0, "distinct_subjects": 0}
    assert len(table.scan_calls) == lh._STAGING_SCAN_PAGE_CAP


def test_staging_census_swallows_backend_failure(monkeypatch):
    monkeypatch.setattr(self_learning, "_get_table",
                        lambda: RecordingTable(raise_on_scan=True))

    assert lh.staging_census() == {"rows": 0, "distinct_subjects": 0}


def test_staging_census_row_without_subject_key_counts_row_only(monkeypatch):
    rows = [{"mapping_id": "staged#orphan-uuid"}]
    table = RecordingTable(scan_pages=[rows])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert lh.staging_census() == {"rows": 1, "distinct_subjects": 0}


# --------------------------------------------------------------------------- #
# usage_ledger_stats
# --------------------------------------------------------------------------- #
def test_usage_ledger_stats_legacy_rows_fold_as_tool_text(monkeypatch):
    usage = {
        "node-1": {"artifact_id": "node-1", "kind": "join_path",
                   "usage_30d": 3, "last_used_at": "2026-07-20T00:00:00+00:00"},
        "concept-a": {"artifact_id": "concept-a", "kind": "concept",
                      "usage_30d": 2, "last_used_at": None},
    }
    seen = {}

    def fake_read(days=30):
        seen["days"] = days
        return usage

    monkeypatch.setattr(artifact_usage, "read_artifact_usage", fake_read)

    stats = lh.usage_ledger_stats(30)
    assert seen["days"] == 30
    assert stats == {"rows_30d": 5, "artifacts": 2,
                     "evidence_classes": ["tool-text"]}


def test_usage_ledger_stats_reports_evidence_classes_as_flags(monkeypatch):
    usage = {
        "node-1": {"artifact_id": "node-1", "kind": "join_path", "usage_30d": 4,
                   "evidence_classes": ["route-intersection"]},
        "node-2": {"artifact_id": "node-2", "kind": "identity_rule", "usage_30d": 1,
                   "evidence_classes": ["relationship-proof"]},
        "node-3": {"artifact_id": "node-3", "kind": "rule", "usage_30d": 2},
    }
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: usage)

    stats = lh.usage_ledger_stats(30)
    assert stats["rows_30d"] == 7
    assert stats["artifacts"] == 3
    assert stats["evidence_classes"] == ["relationship-proof",
                                         "route-intersection", "tool-text"]


def test_usage_ledger_stats_multiclass_artifact_never_double_counts(monkeypatch):
    """An artifact carrying MULTIPLE evidence classes must
    not inflate any total — the old per-class rollup added its full count to
    EVERY class (4 uses → 8 attributed). Classes are flags; ``rows_30d`` is
    the single honest total."""
    usage = {
        "node-1": {"artifact_id": "node-1", "kind": "join_path", "usage_30d": 4,
                   "evidence_classes": ["route-intersection",
                                        "relationship-proof"]},
    }
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: usage)

    stats = lh.usage_ledger_stats(30)
    assert stats["rows_30d"] == 4  # the ONLY use total — never 8
    assert stats["evidence_classes"] == ["relationship-proof",
                                         "route-intersection"]
    # No per-class counts anywhere in the shape (not a recoverable partition).
    assert set(stats) == {"rows_30d", "artifacts", "evidence_classes"}


def test_usage_ledger_stats_swallows_backend_failure(monkeypatch):
    def boom(days=30):
        raise RuntimeError("ledger boom")

    monkeypatch.setattr(artifact_usage, "read_artifact_usage", boom)

    assert lh.usage_ledger_stats(30) == {
        "rows_30d": 0, "artifacts": 0, "evidence_classes": []}


def test_usage_ledger_stats_garbage_counts_fold_to_zero(monkeypatch):
    usage = {"node-1": {"artifact_id": "node-1", "kind": "rule",
                        "usage_30d": "not-a-number"}}
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: usage)

    stats = lh.usage_ledger_stats(30)
    assert stats["rows_30d"] == 0
    assert stats["artifacts"] == 1
    assert stats["evidence_classes"] == ["tool-text"]


# --------------------------------------------------------------------------- #
# pending_age_days (pure)
# --------------------------------------------------------------------------- #
_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _pending(mapping_id: str, days_ago: float, **extra) -> dict:
    row = {"mapping_id": mapping_id, "status": "pending",
           "discovered_at": (_NOW - timedelta(days=days_ago)).isoformat()}
    row.update(extra)
    return row


def test_pending_age_days_computes_sorted_ages():
    rows = [_pending("m1", 2.5), _pending("m2", 0.1),
            {"mapping_id": "m3", "status": "accepted",
             "discovered_at": (_NOW - timedelta(days=9)).isoformat()}]

    ages = lh.pending_age_days(rows, now=_NOW)
    assert ages == [0.1, 2.5]


def test_pending_age_days_skips_staged_and_unparseable_rows():
    rows = [
        # Defense in depth: a staged row must never appear as pending even
        # if a caller passes an unfiltered scan.
        {"mapping_id": "staged#subj-uuid", "status": "pending",
         "discovered_at": _NOW.isoformat()},
        {"mapping_id": "m1", "status": "pending", "discovered_at": "garbage"},
        {"mapping_id": "m2", "status": "pending"},
        _pending("m3", 1.0),
    ]

    assert lh.pending_age_days(rows, now=_NOW) == [1.0]


def test_pending_age_days_clamps_future_timestamps_to_zero():
    rows = [_pending("m1", -3.0)]  # discovered_at in the future (clock skew)

    assert lh.pending_age_days(rows, now=_NOW) == [0.0]


def test_pending_age_days_naive_timestamp_treated_as_utc():
    naive = (_NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()
    rows = [{"mapping_id": "m1", "status": "pending", "discovered_at": naive}]

    assert lh.pending_age_days(rows, now=_NOW) == [2.0]


def test_pending_age_days_empty_input():
    assert lh.pending_age_days([]) == []
    assert lh.pending_age_days(None) == []


# --------------------------------------------------------------------------- #
# quarantine_stats (pure)
# --------------------------------------------------------------------------- #
def test_quarantine_stats_counts_pending_quarantined_only():
    rows = [
        _pending("m1", 1.0, quarantined="true"),
        _pending("m2", 1.0, quarantined="TRUE"),   # case-insensitive stamp
        _pending("m3", 1.0),                        # pending, not quarantined
        {"mapping_id": "m4", "status": "accepted", "quarantined": "true"},
        {"mapping_id": "m5", "status": "pending", "quarantined": "false"},
    ]

    assert lh.quarantine_stats(rows) == {"pending_quarantined": 2}


def test_quarantine_stats_empty_input():
    assert lh.quarantine_stats([]) == {"pending_quarantined": 0}
    assert lh.quarantine_stats(None) == {"pending_quarantined": 0}


# --------------------------------------------------------------------------- #
# integrity_mode — echo of the single source (agents/config.py)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("env_value,expected", [
    ("enforce", "enforce"),
    ("annotate", "annotate"),
    ("report-only", "report-only"),
    ("", "report-only"),
    ("bogus", "report-only"),
])
def test_integrity_mode_echoes_config(monkeypatch, env_value, expected):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", env_value)
    assert lh.integrity_mode() == expected


# --------------------------------------------------------------------------- #
# AuthorityRule graph integrity (pure over one graph read)
# --------------------------------------------------------------------------- #
def test_authority_rule_evidence_orphan_names_alternative_system():
    rows = [
        {"rule_id": "ar-system-a-state", "system_id": "system-a",
         "concept": "machine-state", "mapped_system_id": "system-b",
         "mapped_system_name": "System B"},
        {"rule_id": "ar-system-a-state", "system_id": "system-a",
         "concept": "machine-state", "mapped_system_id": "system-c",
         "mapped_system_name": ""},
    ]

    stats = lh.authority_rule_evidence_stats(rows)

    assert stats == {
        "available": True, "active_rules": 1, "orphan_count": 1,
        "evidence_orphans": [{
            "rule_id": "ar-system-a-state", "system_id": "system-a",
            "concept": "machine-state", "mapped_systems": [
                {"system_id": "system-b", "system_name": "System B"},
                {"system_id": "system-c", "system_name": "system-c"},
            ],
        }],
    }


def test_well_evidenced_authority_rule_is_not_flagged():
    rows = [
        {"rule_id": "ar-system-a-state", "system_id": "system-a",
         "concept": "machine-state", "mapped_system_id": "system-a",
         "mapped_system_name": "System A"},
        {"rule_id": "ar-system-a-state", "system_id": "system-a",
         "concept": "machine-state", "mapped_system_id": "system-b",
         "mapped_system_name": "System B"},
    ]

    stats = lh.authority_rule_evidence_stats(rows)

    assert stats["active_rules"] == 1
    assert stats["orphan_count"] == 0
    assert stats["evidence_orphans"] == []


# --------------------------------------------------------------------------- #
# build_health — the pinned response shape
# --------------------------------------------------------------------------- #
_PINNED_KEYS = {"integrity_mode", "usage_ledger", "staging",
                "pending_ages_days", "quarantine", "guidance_budget",
                "guidance_delivery", "graph_integrity"}


def test_build_health_assembles_pinned_shape(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    real_now = datetime.now(timezone.utc)
    mappings = [
        {"mapping_id": "m1", "status": "pending", "quarantined": "true",
         "discovered_at": (real_now - timedelta(days=1)).isoformat()},
        {"mapping_id": "m2", "status": "pending",
         "discovered_at": (real_now - timedelta(days=3)).isoformat()},
    ]
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: mappings)
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda **_k: [
        {"system_id": "sys-a", "over_budget": False, "over_by": 0},
        {"system_id": "sys-b", "over_budget": True, "over_by": 321},
    ])
    table = RecordingTable(scan_pages=[[_staged("subj-a"), _staged("subj-b")]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    usage = {"node-1": {"artifact_id": "node-1", "kind": "rule", "usage_30d": 2}}
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: usage)
    graph_integrity = {"authority_rule_evidence": {
        "available": True, "active_rules": 2, "orphan_count": 0,
        "evidence_orphans": [],
    }}
    monkeypatch.setattr(lh, "read_graph_integrity", lambda: graph_integrity)

    health = lh.build_health()

    assert set(health) == _PINNED_KEYS
    assert health["integrity_mode"] == "report-only"
    assert health["usage_ledger"] == {"rows_30d": 2, "artifacts": 1,
                                      "evidence_classes": ["tool-text"]}
    assert health["staging"] == {"rows": 2, "distinct_subjects": 2}
    assert health["pending_ages_days"] == pytest.approx([1.0, 3.0], abs=0.02)
    assert health["quarantine"] == {"pending_quarantined": 1}
    assert health["guidance_budget"] == {
        "available": True, "systems": 2, "over_budget": True, "over_by": 321,
        "over_budget_systems": [{"system_id": "sys-b", "over_by": 321}],
    }
    # The DELIVERY half of the same annotation: these rows carry no guidance
    # document, so nothing is dropped on the way into a prompt.
    assert health["guidance_delivery"] == {
        "available": True, "systems": 2, "field_cap": 1_024,
        "truncated": False, "notes_dropped": 0, "chars_dropped": 0,
        "truncated_systems": [],
    }
    assert health["graph_integrity"] == graph_integrity


def test_build_health_staging_rows_never_counted_as_pending(monkeypatch):
    """Staged rows appear ONLY in the staging census — every pending-derived
    number stays zero when the table holds nothing but staged rows."""
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "get_all_query_guidance", lambda **_k: [])
    table = RecordingTable(scan_pages=[[_staged("subj-a")] * 3])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: {})
    monkeypatch.setattr(lh, "read_graph_integrity", lambda: {
        "authority_rule_evidence": {
            "available": True, "active_rules": 0, "orphan_count": 0,
            "evidence_orphans": [],
        }})

    health = lh.build_health()

    assert health["staging"]["rows"] == 3
    assert health["pending_ages_days"] == []
    assert health["quarantine"] == {"pending_quarantined": 0}


def test_build_health_degrades_to_zeros_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(self_learning, "get_all_mappings", boom)
    monkeypatch.setattr(self_learning, "_get_table", boom)
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", boom)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)

    health = lh.build_health()

    assert set(health) == _PINNED_KEYS
    assert health["usage_ledger"] == {"rows_30d": 0, "artifacts": 0,
                                      "evidence_classes": []}
    assert health["staging"] == {"rows": 0, "distinct_subjects": 0}
    assert health["pending_ages_days"] == []
    assert health["quarantine"] == {"pending_quarantined": 0}
    assert health["integrity_mode"] == "report-only"


def test_zero_health_matches_pinned_shape(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")

    zero = lh.zero_health()

    assert set(zero) == _PINNED_KEYS
    assert zero["integrity_mode"] == "enforce"
    assert zero["usage_ledger"] == {"rows_30d": 0, "artifacts": 0,
                                    "evidence_classes": []}
    assert zero["guidance_budget"] == {
        "available": False, "systems": None, "over_budget": False, "over_by": 0,
        "over_budget_systems": [],
    }
    assert zero["guidance_delivery"] == {
        "available": False, "systems": None, "field_cap": 1_024,
        "truncated": False, "notes_dropped": 0, "chars_dropped": 0,
        "truncated_systems": [],
    }
    assert zero["graph_integrity"] == {"authority_rule_evidence": {
        "available": False, "active_rules": None, "orphan_count": None,
        "evidence_orphans": [],
    }}


# --------------------------------------------------------------------------- #
# Import purity — no boto3/pg8000 at module import time (ADR 0020/0021)
# --------------------------------------------------------------------------- #
def test_module_source_has_no_impure_top_level_imports():
    src = open(lh.__file__).read()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


def test_guidance_budget_read_failure_is_honestly_unavailable(monkeypatch):
    def unavailable(**_kwargs):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(self_learning, "get_all_query_guidance", unavailable)

    assert lh.read_guidance_budget_stats() == {
        "available": False, "systems": None, "over_budget": False,
        "over_by": 0, "over_budget_systems": [],
    }


def test_read_graph_integrity_uses_active_field_mapping_query(monkeypatch):
    import agents.db as db
    calls = []

    def query(cypher, parameters=None):
        calls.append((cypher, parameters or {}))
        return [{"rule_id": "ar-system-a-state", "system_id": "system-a",
                 "concept": "machine-state", "mapped_system_id": "system-a",
                 "mapped_system_name": "System A"}]

    monkeypatch.setattr(db, "neptune_query", query)

    result = lh.read_graph_integrity()

    assert result["authority_rule_evidence"]["orphan_count"] == 0
    assert len(calls) == 1
    cypher, parameters = calls[0]
    assert "coalesce(ar.status, '') <> 'deactivated'" in cypher
    # The field-evidence predicate is COMPOSED from its single home,
    # so this read can never drift from the onboarding gate or the route seed.
    assert (f"(mapped:System){concept_authority.FIELD_EVIDENCE_TRAVERSAL}"
            "(c:Concept) " in cypher)
    assert "concept_authority.FIELD_EVIDENCE_TRAVERSAL" in inspect.getsource(lh)
    # An ENTITY-level SchemaEntry mapping still must not count as evidence.
    assert "SchemaEntry)-[:MAPS_TO]" not in cypher
    assert parameters == {}
