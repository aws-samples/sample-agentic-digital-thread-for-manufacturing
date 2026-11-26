# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0052 B4 — authority-vouching refinement of the ADR-0051 P8 gate.

Audited defect: 3 pending cross-site join proposals passed P8 via an
LLM-MINTED All-Sites AuthorityRule ("<system> is the authoritative source
for <concept> at All Sites") — a machine claim vouching for a machine claim.
Refinement pinned here (offline — FakeTable, monkeypatched ``known_systems``
catalog and graph seam):

  * an All-Sites authority satisfies P8 ONLY when steward-confirmed (user-tier
    ``method`` stamp — ``user_confirmed``/``crosswalk_confirmed`` — or
    non-empty ``approved_by``) or evidence-bearing (non-empty ``evidence``);
  * an UNVERIFIED All-Sites authority is flag-not-drop: the proposal proceeds
    to pending with the EXACT review_reason
    "cross-site link vouched only by unverified authority <id>"
    (B1 probe 5 and B2's conflict display key on this wording);
  * NO All-Sites authority at all ⇒ drop with the stated namespace-collision
    reason (unchanged P8 behavior);
  * user source bypasses; missing metadata and backend failures stay
    fail-open (quality gate, not a security gate).
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning

_SYSTEMS = [
    {"id": "mes-mil", "name": "Milwaukee MES", "system_type": "MES",
     "plant": "Milwaukee"},
    {"id": "mes-cr", "name": "Cedar Rapids MES", "system_type": "MES",
     "plant": "Cedar Rapids"},
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
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    self_learning._reset_accepted_coverage_cache()
    return fake


def _authority_rows(monkeypatch, rows):
    """Wire the graph seam: AuthorityRule reads return ``rows`` verbatim."""
    monkeypatch.setattr(
        self_learning, "_neptune_query",
        lambda c, p=None: (list(rows) if "AuthorityRule" in c else []))


def _log(*, source="agent", concept="operator-id"):
    return self_learning.log_proposed_mapping(
        "mes-mil", "work_orders.operator_id", "7",
        "mes-cr", "operators.operator_id", "7",
        concept, 0.9, "operator ids look identical across plants",
        proposed_type="value_mapping", source=source,
    )


def test_llm_minted_authority_flags_not_drops(monkeypatch, wired):
    # The audited residual: an LLM-minted All-Sites authority (no user-tier
    # method stamp, no approved_by, no evidence) no longer satisfies P8 —
    # the proposal lands PENDING carrying the exact contractual reason.
    _authority_rows(monkeypatch, [
        {"id": "ar-erp-operator-id", "sid": "erp", "concept": "operator-id",
         "method": "onboarding-llm"}])
    mid = _log()
    assert mid is not None
    row = wired.items[0]
    assert row["status"] == "pending"
    assert row["review_reason"] == ("cross-site link vouched only by "
                                    "unverified authority ar-erp-operator-id")


@pytest.mark.parametrize("stamp", [
    {"method": "user_confirmed"},
    {"method": "crosswalk_confirmed"},
    {"approved_by": "steward@example.com"},
])
def test_steward_confirmed_authority_passes_clean(monkeypatch, wired, stamp):
    _authority_rows(monkeypatch, [
        {"id": "ar-erp-operator-id", "sid": "erp",
         "concept": "operator-id", **stamp}])
    mid = _log()
    assert mid is not None
    row = wired.items[0]
    assert row["status"] == "pending"
    assert "review_reason" not in row  # clean pass — no vouching flag


def test_evidence_bearing_authority_passes(monkeypatch, wired):
    # Non-empty ``evidence`` is the seam for a future value-overlap minting
    # path (ADR-0052) — it vouches like a steward confirmation.
    _authority_rows(monkeypatch, [
        {"id": "ar-erp-operator-id", "sid": "erp", "concept": "operator-id",
         "evidence": "value-overlap 0.97 across 240 samples"}])
    mid = _log()
    assert mid is not None
    assert "review_reason" not in wired.items[0]


def test_no_authority_still_drops_with_stated_reason(monkeypatch, wired):
    _authority_rows(monkeypatch, [])
    assert _log() is None
    assert wired.items == []
    verdict, reason = self_learning._gate_cross_site_namespace(
        "value_mapping", "mes-mil", "mes-cr", "operator-id", "agent")
    assert verdict == "drop"
    assert "site-local identifier namespaces collide on format" in reason


def test_user_source_bypasses(monkeypatch, wired):
    _authority_rows(monkeypatch, [])
    mid = _log(source="user")
    assert mid is not None
    assert "review_reason" not in wired.items[0]


def test_backend_failure_fails_open(monkeypatch, wired):
    def _boom(c, p=None):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(self_learning, "_neptune_query", _boom)
    # Quality gate, not a security gate: a graph read failure inside the
    # vouching verdict must fail-open through the P8 wrapper — the
    # proposal is stored, unflagged.
    mid = _log()
    assert mid is not None
    assert "review_reason" not in wired.items[0]


def test_steward_authority_outranks_unverified_sibling(monkeypatch, wired):
    # One verified All-Sites authority among unverified ones ⇒ clean pass:
    # the verdict prefers ANY verified voucher over the flag.
    _authority_rows(monkeypatch, [
        {"id": "ar-erp-operator-id", "sid": "erp", "concept": "operator-id"},
        {"id": "subj-operator-id", "sid": "erp", "concept": "operator-id",
         "method": "user_confirmed"}])
    mid = _log()
    assert mid is not None
    assert "review_reason" not in wired.items[0]


def test_unverified_authority_without_id_falls_back_to_ar_sid(monkeypatch, wired):
    # Legacy AR nodes may lack an ``id`` property — the reason still names
    # a deterministic handle (``ar-<system_id>``), never an empty id.
    _authority_rows(monkeypatch, [{"sid": "erp", "concept": "operator-id"}])
    _log()
    assert wired.items[0]["review_reason"] == (
        "cross-site link vouched only by unverified authority ar-erp")
