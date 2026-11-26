# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Coverage-gap analyzer — offline. Automatic pickup after a manual remap,
as the acknowledge-only ``coverage_gap`` proposal type (it does not reuse
concept_mapping).

Behavioral proof against the SAME in-memory ``FakeTable``/``NeptuneSpy``
doubles as ``test_schema_drift_proposals.py``. No AWS, no Neptune.

Pins:

- ``extract_candidate_field_names`` / ``candidate_field_matches`` are PURE
  token-SET-containment matchers, mirroring ``concept_suggest``'s discipline;
- ``analyze_coverage_gaps`` cross-references gaps against schemas and returns
  one finding per (system, table, column, concept) hit, sorted deterministically;
- ``run_coverage_gap_check`` files through the ``coverage_gap`` type — NEVER
  ``concept_mapping`` (whose accept path runs ``_repoint_maps_to``'s
  column-keyed delete) and NEVER ``record_user_correction``;
- filing NEVER writes a MAPS_TO edge; ACCEPTING a filed row is
  acknowledge-only — the REAL ``accept_mapping`` performs ZERO graph writes
  (blocker 1 closed, proven live below);
- TWO candidate fields for the same unmapped concept COEXIST as separate
  PENDING rows — status asserted, not just row count (blocker 2 closed);
- the SAME (system, concept, field) gap does not re-file on a second run
  (canonical-key dedup — found, not filed twice);
- an agent-sourced finding naming an UNKNOWN system is dropped at file time
  (the schema_drift posture in ``_gate_drop_unknown_agent_systems``);
- an EMPTY covered set with an unreachable graph SKIPS (never a mass filing);
- resolved gaps are swept to ``superseded`` (replace-not-accrete), scoped to
  the checked systems, and a mid-sweep steward decision wins;
- any exception anywhere in the pipeline degrades to the honest all-zero
  result — a coverage check must never break its caller.
"""
from __future__ import annotations

import pytest

import agents.coverage_gap_analyzer as cga
import agents.schema_check as schema_check
import agents.self_learning as self_learning

try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy

_THROUGHPUT_DEF = (
    "Produced-output rate or count over a time bucket. Fields: amount, count, "
    "units_produced, produced_per_interval, output_count, good_count, "
    "amount_per_interval, throughput."
)


def _install(monkeypatch, table: FakeTable, spy: "NeptuneSpy | None" = None):
    spy = spy or NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    monkeypatch.setattr(self_learning, "known_systems",
                        lambda: [{"id": "sys-a", "name": "System A"}])
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return spy


_FINDING = {"system_id": "sys-a", "table": "Metric_Event",
            "column": "amount_per_5s", "concept": "throughput",
            "matched_candidate": "amount", "blocks_cards": ["throughput-card"]}


# --------------------------------------------------------------------------- #
# Pure matchers
# --------------------------------------------------------------------------- #
def test_extract_candidate_field_names_parses_the_fields_clause():
    names = cga.extract_candidate_field_names(_THROUGHPUT_DEF)
    assert "amount" in names and "throughput" in names
    assert cga.extract_candidate_field_names("No Fields clause here.") == []


def test_candidate_field_matches_is_token_set_containment_not_substring():
    schema = {"Metric_Event": {"columns": ["amount_per_5s", "amount_unit_price"]}}
    hits = cga.candidate_field_matches("throughput", _THROUGHPUT_DEF, schema)
    cols = {h["column"] for h in hits}
    assert "amount_per_5s" in cols
    assert cols == {"amount_per_5s", "amount_unit_price"}  # both contain "amount"


def test_candidate_field_matches_returns_empty_for_no_fields_clause():
    schema = {"t": {"columns": ["x"]}}
    assert cga.candidate_field_matches("c", "No clause.", schema) == []


def test_analyze_coverage_gaps_cross_references_and_sorts():
    gaps = [{"concept": "throughput", "blocks_cards": ["throughput-card"]}]
    schemas = {
        "sys-b": {"Metric_Event": {"columns": ["amount_per_5s"]}},
        "sys-a": {"Metric_Event": {"columns": ["amount_per_5s"]}},
    }
    findings = cga.analyze_coverage_gaps(gaps, schemas)
    assert [f["system_id"] for f in findings] == ["sys-a", "sys-b"]  # sorted
    assert findings[0]["concept"] == "throughput"
    assert findings[0]["blocks_cards"] == ["throughput-card"]


def test_analyze_coverage_gaps_skips_non_core_concepts_honestly():
    gaps = [{"concept": "not-a-real-concept", "blocks_cards": []}]
    findings = cga.analyze_coverage_gaps(gaps, {"sys-a": {"t": {"columns": ["x"]}}})
    assert findings == []


# --------------------------------------------------------------------------- #
# End-to-end filing (real gates, fake graph)
# --------------------------------------------------------------------------- #
def test_filing_uses_the_coverage_gap_type_never_concept_mapping(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    filed = cga._file_coverage_gap_proposals([dict(_FINDING)])

    assert filed == 1
    rows = fake.all_items()
    assert len(rows) == 1
    row = rows[0]
    assert row["proposed_type"] == "coverage_gap"   # NEVER concept_mapping
    assert row["source"] == "agent"
    assert row["method"] == "coverage-gap-name-match"
    assert row["status"] == "pending"          # NOT accepted — steward reviews
    assert row["to_system"] == "sys-a"
    assert row["to_field"] == "Metric_Event.amount_per_5s"
    assert row["concept"] == "throughput"
    assert row["reason"] == "concept_unmapped"
    assert "throughput-card" in row["query_context"]


def test_second_run_dedupes_the_same_finding_not_double_filed(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = cga._file_coverage_gap_proposals([dict(_FINDING)])
    second = cga._file_coverage_gap_proposals([dict(_FINDING)])

    assert first == 1
    assert second == 0  # dup_pending — found, not re-filed
    assert len(fake.all_items()) == 1


def test_two_candidates_for_one_concept_coexist_as_pending_rows(monkeypatch):
    """BLOCKER 2 (closed): the coverage_gap grain puts the candidate COLUMN in
    the SUBJECT ((to_system, concept, to_field)), so a second candidate for
    the SAME unmapped concept is a DISTINCT subject — it inserts, it does not
    ride _classify_active_diff's same-subject supersede path. Asserts the
    STATUS of both rows, not just the count (the original test's gap)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    findings = [
        dict(_FINDING, blocks_cards=[]),
        dict(_FINDING, column="output_count",
             matched_candidate="output_count", blocks_cards=[]),
    ]
    filed = cga._file_coverage_gap_proposals(findings)

    assert filed == 2
    rows = fake.all_items()
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"pending"}   # BOTH still pending
    assert {r["to_field"] for r in rows} == {
        "Metric_Event.amount_per_5s", "Metric_Event.output_count"}
    assert all(r["proposed_type"] == "coverage_gap" for r in rows)
    assert not any(r.get("superseded_at") for r in rows)


def test_agent_finding_naming_an_unknown_system_is_dropped_at_file_time(monkeypatch):
    """coverage_gap sits in _gate_drop_unknown_agent_systems (the schema_drift
    posture): the analyzer only scans onboarded systems, so an agent-sourced
    row naming a system the catalog does not know is junk — dropped at the
    source, never a pending row a steward must triage."""
    fake = FakeTable()
    _install(monkeypatch, fake)  # only 'sys-a' is a known system
    findings = [dict(_FINDING, system_id="ghost-system")]
    filed = cga._file_coverage_gap_proposals(findings)
    assert filed == 0
    assert fake.all_items() == []


# --------------------------------------------------------------------------- #
# BLOCKER 1 (closed): accept is acknowledge-only — LIVE-code proof
# --------------------------------------------------------------------------- #
def test_accepting_a_coverage_gap_row_performs_zero_graph_writes(monkeypatch):
    """The REAL self_learning.accept_mapping on a REAL filed row: the
    coverage_gap branch is acknowledge-only (_accept_coverage_gap). ZERO
    Neptune writes — in particular no DELETE and no MAPS_TO merge, so a
    neighboring concept's edge on the candidate column CANNOT be stripped
    (the _repoint_maps_to column-keyed-delete hazard this type exists to
    avoid). Not a mocked assertion: the spy captures every write the accept
    path would issue."""
    fake = FakeTable()
    spy = _install(monkeypatch, fake)

    filed = cga._file_coverage_gap_proposals([dict(_FINDING)])
    assert filed == 1
    mapping_id = fake.all_items()[0]["mapping_id"]
    writes_before_accept = list(spy.calls)

    result = self_learning.accept_mapping(mapping_id, actor="steward@example.com")

    assert result == {"status": "accepted", "acknowledged": True,
                      "proposed_type": "coverage_gap", "version": 1}
    row = fake.by_id(mapping_id)
    assert row["status"] == "accepted"
    assert row["accepted_by"] == "steward@example.com"
    assert "neptune_node_id" not in row      # honest absence — nothing applied
    # THE blocker-1 proof: accept issued NO graph writes at all.
    assert spy.calls == writes_before_accept
    assert not any("DELETE" in c[0].upper() for c in spy.calls)
    assert not any("MAPS_TO" in c[0] for c in spy.calls)


def test_coverage_gap_sits_in_the_auto_accept_deny_tuple():
    """The flag cannot silently admit it (the schema_drift posture)."""
    assert "coverage_gap" in self_learning._NEVER_AUTO_ACCEPT_TYPES


# --------------------------------------------------------------------------- #
# Resolved-gap sweep (replace-not-accrete)
# --------------------------------------------------------------------------- #
def test_sweep_supersedes_a_resolved_gap_row(monkeypatch):
    """Once the concept is covered (or the column vanished), the pending row
    is no longer in the current finding set — machine-legal supersede."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    cga._file_coverage_gap_proposals([dict(_FINDING)])

    swept = cga.sweep_resolved_gaps(["sys-a"], [])  # gap resolved: no findings

    assert swept == 1
    row = fake.all_items()[0]
    assert row["status"] == "superseded"
    assert row["superseded_reason"] == "gap-resolved"


def test_sweep_keeps_a_still_open_finding_pending(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    cga._file_coverage_gap_proposals([dict(_FINDING)])

    swept = cga.sweep_resolved_gaps(["sys-a"], [dict(_FINDING)])

    assert swept == 0
    assert fake.all_items()[0]["status"] == "pending"


def test_sweep_is_scoped_never_touches_another_systems_rows(monkeypatch):
    """A one-system investigate must not sweep another system's open rows."""
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "_neptune_write", NeptuneSpy())
    monkeypatch.setattr(self_learning, "known_systems",
                        lambda: [{"id": "sys-a", "name": "A"},
                                 {"id": "sys-b", "name": "B"}])
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    cga._file_coverage_gap_proposals([dict(_FINDING, system_id="sys-b")])

    swept = cga.sweep_resolved_gaps(["sys-a"], [])  # sys-a scope only

    assert swept == 0
    assert fake.all_items()[0]["status"] == "pending"


def test_sweep_mid_flight_steward_decision_wins(monkeypatch):
    """The conditional-update idiom: a steward accept landing mid-sweep is
    skipped and uncounted, never clobbered back to superseded."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    cga._file_coverage_gap_proposals([dict(_FINDING)])
    mapping_id = fake.all_items()[0]["mapping_id"]
    # Steward accepted between the sweep's read and its write:
    self_learning.accept_mapping(mapping_id)

    swept = cga.sweep_resolved_gaps(["sys-a"], [])

    assert swept == 0
    assert fake.by_id(mapping_id)["status"] == "accepted"


# --------------------------------------------------------------------------- #
# Orchestrating entrypoint
# --------------------------------------------------------------------------- #
def _wire_pipeline(monkeypatch, fake: FakeTable):
    spy = _install(monkeypatch, fake)
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: {"Metric_Event": {"columns": ["amount_per_5s"]}}
                        if sid == "sys-a" else None)
    import agents.coverage as coverage
    monkeypatch.setattr(coverage, "covered_concepts", lambda: frozenset())
    monkeypatch.setattr(coverage, "thread_coverage", lambda covered, **kw: {
        "gaps": [{"concept": "throughput", "blocks_cards": ["throughput-card"]}]})
    monkeypatch.setattr(cga, "_graph_reachable", lambda: True)
    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: ["sys-a"])
    return spy


def test_run_coverage_gap_check_never_writes_a_maps_to_edge_directly(monkeypatch):
    fake = FakeTable()
    spy = _wire_pipeline(monkeypatch, fake)

    result = cga.run_coverage_gap_check()

    assert result == {"gaps_checked": 1, "systems_checked": 1,
                      "findings": 1, "filed": 1, "resolved_swept": 0}
    # NeptuneSpy captured the pending-proposal write only — never a MAPS_TO merge.
    writes = [c for c in spy.calls if "MAPS_TO" in c[0]]
    assert writes == []


def test_run_coverage_gap_check_skips_when_graph_is_unreachable(monkeypatch):
    """HIGH finding (closed): the covered_concepts read seam swallows backend
    errors into an empty set. An empty covered set is only trusted after the
    System-count reachability cross-check — unreachable means SKIP with
    nothing filed, never every concept mass-filed as a gap."""
    fake = FakeTable()
    _wire_pipeline(monkeypatch, fake)
    monkeypatch.setattr(cga, "_graph_reachable", lambda: False)

    result = cga.run_coverage_gap_check()

    assert result == {"gaps_checked": 0, "systems_checked": 0, "findings": 0,
                      "filed": 0, "resolved_swept": 0,
                      "skipped": "graph-unavailable"}
    assert fake.all_items() == []           # nothing filed on a blind read


def test_run_coverage_gap_check_trusts_a_nonempty_covered_set_without_the_guard(monkeypatch):
    """A NON-empty covered set is proof the graph answered — the reachability
    cross-check must not even run (no second read on the happy path)."""
    fake = FakeTable()
    _wire_pipeline(monkeypatch, fake)
    import agents.coverage as coverage
    monkeypatch.setattr(coverage, "covered_concepts",
                        lambda: frozenset({"actual-quantity"}))
    def boom():
        raise AssertionError("guard must not run when covered is non-empty")
    monkeypatch.setattr(cga, "_graph_reachable", boom)

    result = cga.run_coverage_gap_check()

    assert result["filed"] == 1


def test_run_coverage_gap_check_degrades_to_zero_on_any_failure(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("graph unreachable")
    import agents.coverage as coverage
    monkeypatch.setattr(coverage, "covered_concepts", boom)

    result = cga.run_coverage_gap_check()

    assert result == {"gaps_checked": 0, "systems_checked": 0,
                      "findings": 0, "filed": 0, "resolved_swept": 0}


def test_run_coverage_gap_check_accepts_an_explicit_system_scope(monkeypatch):
    """The steward-gated investigate route scopes to ONE system — must not
    silently scan every onboarded system when a scope is given."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: {"Metric_Event": {"columns": ["amount_per_5s"]}})
    import agents.coverage as coverage
    monkeypatch.setattr(coverage, "covered_concepts", lambda: frozenset())
    monkeypatch.setattr(coverage, "thread_coverage", lambda covered, **kw: {
        "gaps": [{"concept": "throughput", "blocks_cards": []}]})
    monkeypatch.setattr(cga, "_graph_reachable", lambda: True)
    calls = []
    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "onboarded_system_ids",
                        lambda: calls.append("called") or ["sys-a", "sys-b"])

    result = cga.run_coverage_gap_check(system_ids=["sys-a"])

    assert result["systems_checked"] == 1
    assert calls == []  # onboarded_system_ids never consulted when scoped


def test_run_coverage_gap_check_sweeps_resolved_rows_in_the_same_pass(monkeypatch):
    """Filing and sweeping ride one entrypoint: a row whose gap has since
    resolved is superseded by the SAME run that files the still-open ones."""
    fake = FakeTable()
    _wire_pipeline(monkeypatch, fake)
    # Seed a stale row for a DIFFERENT (now-resolved) candidate on sys-a.
    cga._file_coverage_gap_proposals(
        [dict(_FINDING, column="good_count", matched_candidate="good_count")])

    result = cga.run_coverage_gap_check(system_ids=["sys-a"])

    assert result["filed"] == 1            # the current finding
    assert result["resolved_swept"] == 1   # the stale one
    by_field = {r["to_field"]: r["status"] for r in fake.all_items()}
    assert by_field == {"Metric_Event.good_count": "superseded",
                        "Metric_Event.amount_per_5s": "pending"}
