# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ADR-0063 cross-system context placement."""
from __future__ import annotations

import agents.context_placement as cp
import agents.self_learning as self_learning

try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy


def _install(monkeypatch, table):
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [
        {"id": "sys-a", "name": "System A"},
        {"id": "sys-b", "name": "System B"},
    ])
    return spy


def test_extract_candidate_terms_positive_and_negative_shapes():
    text = ("ordinary Capitalized ALLCAPS snake_case MixedCaseTag lowerCamelId "
            "UPPER_SNAKE ABC-12* \"Machine Alpha\" 'Cell Beta'")
    terms = cp.extract_candidate_terms(text)
    assert terms == ["MixedCaseTag", "lowerCamelId", "UPPER_SNAKE",
                     "ABC-12*", "Machine Alpha", "Cell Beta"]
    assert cp.extract_candidate_terms("ordinary Capitalized ALLCAPS snake_case a*") == []


def test_extract_candidate_terms_is_deduplicated_and_bounded():
    text = " ".join(f"CODE_{index}" for index in range(200))
    terms = cp.extract_candidate_terms(text, limit=7)
    assert terms == [f"CODE_{index}" for index in range(7)]
    assert cp.extract_candidate_terms("CODE_ONE CODE_ONE") == ["CODE_ONE"]


def test_hit_requires_source_term_target_evidence_and_target_absence():
    surfaces = {
        "sys-a": {"context": 'Family "Machine Alpha" uses BatchCode.',
                  "schema": {"a": {"columns": ["description"]}}},
        "sys-b": {"context": "", "schema": {"events": {
            "columns": ["batch_code", "equipment"],
            "sample_values": {"equipment": ["Machine Alpha"]}}}},
    }
    hits = cp.find_context_placement_hits(surfaces)
    assert {(hit["term"], hit["evidence_kind"]) for hit in hits} == {
        ("Machine Alpha", "sample-value"), ("BatchCode", "schema-column")}
    assert all(hit["source_system"] == "sys-a" for hit in hits)
    assert all(hit["target_system"] == "sys-b" for hit in hits)


def test_prefixed_family_matches_a_sample_value():
    surfaces = {
        "sys-a": {"context": "Code family ABC* identifies the category.",
                  "schema": {"a": {"columns": ["note"]}}},
        "sys-b": {"context": "", "schema": {"events": {
            "columns": [{"name": "code", "sample_values": ["ABC-17"]}]}}},
    }
    hits = cp.find_context_placement_hits(surfaces)
    assert [hit["term"] for hit in hits] == ["ABC*"]


def test_term_already_present_in_target_context_does_not_fire():
    surfaces = {
        "sys-a": {"context": 'Uses "Machine Alpha".',
                  "schema": {"a": {"columns": ["note"]}}},
        "sys-b": {"context": "Machine Alpha is the finishing resource.",
                  "schema": {"events": {
                      "columns": [{"name": "asset", "sample_values": ["Machine Alpha"]}]}}},
    }
    assert cp.find_context_placement_hits(surfaces) == []


def test_context_read_failure_skips_system_instead_of_filing(monkeypatch):
    import agents.business_context as business_context
    import agents.schema_check as schema_check
    import agents.schema_drift as schema_drift

    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: ["sys-a", "sys-b"])
    monkeypatch.setattr(business_context, "read_business_context", lambda sid: (
        {"business_context": 'Uses "Machine Alpha".'} if sid == "sys-a" else None))
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: {
        "events": {"columns": [{"name": "asset", "sample_values": ["Machine Alpha"]}]}})
    monkeypatch.setattr(cp, "_load_cached_samples", lambda _sid: [])
    filed = []
    monkeypatch.setattr(cp, "_file_context_placement_proposals",
                        lambda findings: filed.extend(findings) or len(findings))
    monkeypatch.setattr(cp, "_sweep_resolved", lambda *_args: 0)

    result = cp.run_context_placement_check()

    assert result["findings"] == 0
    assert filed == []
    assert result["skipped"] == [
        {"system_id": "sys-b", "reason": "context-unavailable"}]


def test_schema_read_failure_is_an_honest_skip(monkeypatch):
    import agents.business_context as business_context
    import agents.schema_check as schema_check
    import agents.schema_drift as schema_drift

    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: ["sys-a"])
    monkeypatch.setattr(business_context, "read_business_context",
                        lambda _sid: {"business_context": "UPPER_CODE"})
    monkeypatch.setattr(schema_check, "load_system_schema", lambda _sid: None)
    monkeypatch.setattr(cp, "_file_context_placement_proposals",
                        lambda findings: len(findings))
    monkeypatch.setattr(cp, "_sweep_resolved", lambda *_args: 0)

    result = cp.run_context_placement_check()

    assert result["systems_checked"] == 0
    assert result["skipped"] == [
        {"system_id": "sys-a", "reason": "schema-unavailable"}]


def test_two_terms_coexist_as_separate_pending_rows(monkeypatch):
    table = FakeTable()
    _install(monkeypatch, table)
    findings = [
        {"term": "CODE_ONE", "source_system": "sys-a", "target_system": "sys-b",
         "evidence_kind": "sample-value", "evidence_ref": "events.code"},
        {"term": "CODE_TWO", "source_system": "sys-a", "target_system": "sys-b",
         "evidence_kind": "sample-value", "evidence_ref": "events.code"},
    ]

    assert cp._file_context_placement_proposals(findings) == 2
    rows = table.all_items()
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"pending"}
    assert {row["concept"] for row in rows} == {
        "context-placement:CODE_ONE", "context-placement:CODE_TWO"}
    assert all(row["source"] == cp.PLACEMENT_SOURCE for row in rows)
    assert not any(row.get("superseded_at") for row in rows)
