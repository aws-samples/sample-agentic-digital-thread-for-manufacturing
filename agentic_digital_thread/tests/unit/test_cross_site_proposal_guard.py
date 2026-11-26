# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0051 P8 — cross-site proposal guard (same-class cases).

Audited defect: the agent proposed identifier mappings between two MES
systems at DIFFERENT plants (the mes-mil↔mes-cr ``operator_id`` family).
Systems at different sites use SITE-LOCAL identifier namespaces —
``operator_id 7`` at one plant and ``operator_id 7`` at another collide on
format, not identity. Such a concept may only bridge plants through an
All-Sites authority system (``site_of`` → ``""``) or a steward/user assertion.
Since the 2026-10-19 amendment the guard also covers two systems of
different classes at different plants; those cases are pinned in
test_cross_site_cross_class_proposals.py.

Pinned here (offline — FakeTable, monkeypatched ``known_systems`` catalog and
graph seam):
  * same type + different ``site_of`` + no All-Sites authority ⇒ REJECTED with
    the stated reason ("site-local identifier namespaces collide on format");
  * an All-Sites authority owning the concept ⇒ passes;
  * a link to an All-Sites system of another class ⇒ passes;
  * user source bypasses (ADR-0003 C);
  * same site, or missing site/type/catalog metadata ⇒ fail-open (a
    proposal-quality gate, not a security gate).

``site_of`` is imported from ``agents.metric_multisite`` (ADR-0045 canonical
home) inside the guard — never copied.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning

# Generic catalog fixture mirroring the audited shape: two same-class systems
# at different plants + an All-Sites system of another class.
_SYSTEMS = [
    {"id": "mes-mil", "name": "Milwaukee MES", "system_type": "MES",
     "plant": "Milwaukee"},
    {"id": "mes-cr", "name": "Cedar Rapids MES", "system_type": "MES",
     "plant": "Cedar Rapids"},
    {"id": "mes-det", "name": "Detroit MES", "system_type": "MES",
     "plant": "Milwaukee"},  # same site as mes-mil (control case)
    {"id": "erp", "name": "ERP", "system_type": "ERP", "plant": "All Sites"},
]


class FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": [i for i in self.items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self.items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def delete_item(self, Key):
        self.items = [i for i in self.items
                      if not (i.get("mapping_id") == Key["mapping_id"]
                              and i.get("discovered_at") == Key["discovered_at"])]


@pytest.fixture()
def wired(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(_SYSTEMS))
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    # Default graph: no AuthorityRule rows, no JoinPath rows.
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    self_learning._reset_accepted_coverage_cache()
    return fake


def _authority(monkeypatch, sids, concept="operator-id", **attrs):
    # The guard reads concept-bearing AuthorityRule rows and compares the
    # concept in Python (casefold + key_norm both sides).
    # ``attrs`` (id/method/approved_by/evidence) shape the ADR-0052 B4
    # vouching verdict; bare rows are UNVERIFIED (LLM-minted shape).
    monkeypatch.setattr(
        self_learning, "_neptune_query",
        lambda c, p=None: ([{"sid": s, "concept": concept, **attrs}
                            for s in sids]
                           if "AuthorityRule" in c else []))


def _log(*, from_system="mes-mil", to_system="mes-cr", source="agent",
         concept="operator-id"):
    return self_learning.log_proposed_mapping(
        from_system, "work_orders.operator_id", "7",
        to_system, "operators.operator_id", "7",
        concept, 0.9, "operator ids look identical across plants",
        proposed_type="value_mapping", source=source,
    )


def test_mes_to_mes_cross_site_operator_id_rejected(wired):
    # Named regression: the audited mes-mil↔mes-cr operator_id defect.
    assert _log() is None
    assert wired.items == []


def test_cross_site_crosswalk_guess_also_rejected(wired):
    # A crosswalk is a name-similarity GUESS — across sites of one system
    # class it is the same namespace collision and is equally guarded.
    mid = self_learning.log_proposed_mapping(
        "mes-mil", "lines.line_name", "Line 2",
        "mes-cr", "lines.line_name", "Line 2B",
        "line-id", 0.7, "name similarity across plants",
        proposed_type="crosswalk", source="agent",
    )
    assert mid is None
    assert wired.items == []


def test_rejection_reason_states_the_namespace_collision(wired):
    verdict, reason = self_learning._gate_cross_site_namespace(
        "value_mapping", "mes-mil", "mes-cr", "operator-id", "agent")
    assert verdict == "drop"
    assert "site-local identifier namespaces collide on format" in reason
    assert "operator-id" in reason


def test_cross_site_with_all_sites_authority_passes(monkeypatch, wired):
    # ADR-0052 B4: a bare (LLM-minted-shape) All-Sites authority no longer
    # passes CLEAN — flag-not-drop: the row still lands pending, carrying the
    # unverified-vouch review_reason (exact wording pinned in
    # test_cross_site_vouching.py).
    _authority(monkeypatch, ["erp"])  # erp is All Sites → flag, not drop
    mid = _log()
    assert mid is not None
    assert wired.items[0]["status"] == "pending"
    assert wired.items[0]["review_reason"].startswith(
        "cross-site link vouched only by unverified authority")


def test_site_local_authority_does_not_satisfy_the_guard(monkeypatch, wired):
    _authority(monkeypatch, ["mes-det"])  # sited authority — still rejected
    assert _log() is None


def test_user_source_bypasses_guard(wired):
    assert _log(source="user") is not None


def test_same_site_same_type_unaffected(wired):
    # mes-mil and mes-det share a plant — the guard never fires.
    assert _log(to_system="mes-det") is not None


def test_link_to_an_all_sites_system_of_another_class_unaffected(wired):
    # MES↔ERP: the ERP covers All Sites, so the plant gate never fires.
    assert _log(to_system="erp") is not None


def test_missing_site_metadata_fails_open(monkeypatch, wired):
    systems = [dict(s) for s in _SYSTEMS]
    systems[1]["plant"] = ""  # mes-cr loses its site metadata
    monkeypatch.setattr(self_learning, "known_systems", lambda: systems)
    assert _log() is not None


def test_unknown_systems_fail_open(monkeypatch, wired):
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    assert _log() is not None


def test_backend_failure_fails_open(monkeypatch, wired):
    def _boom():
        raise RuntimeError("catalog unavailable")
    monkeypatch.setattr(self_learning, "known_systems", _boom)
    assert _log() is not None


@pytest.mark.parametrize("concept", ["unknown", "Unknown", ""])
def test_missing_concept_is_metadata_not_evidence(wired, concept):
    # 'unknown' is the ingest default — an absent concept
    # is MISSING METADATA and must fail-open (ADR-0051 e), never fail-closed
    # into dropping legitimate cross-site proposals.
    assert self_learning._gate_cross_site_namespace(
        "value_mapping", "mes-mil", "mes-cr", concept, "agent") == ("", "")


def test_unknown_concept_proposal_passes_end_to_end(wired):
    assert _log(concept="unknown") is not None


def test_authority_concept_comparison_survives_case_drift(monkeypatch, wired):
    # 'Equipment-ID' on the AuthorityRule node must satisfy a
    # proposal spelled 'equipment-id' — both sides casefold + key_norm.
    _authority(monkeypatch, ["erp"], concept="Equipment-ID")
    assert _log(concept="equipment-id") is not None


def test_authority_concept_mismatch_still_rejects(monkeypatch, wired):
    # A REAL concept mismatch (not case drift) keeps the guard closed.
    _authority(monkeypatch, ["erp"], concept="batch-id")
    assert _log(concept="operator-id") is None
