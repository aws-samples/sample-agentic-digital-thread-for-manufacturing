# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integrity-path ``rules_applied`` + ``planned_with`` (Task A1).

``_RuntimeIntegrityInvocation.final_events`` previously emitted
``rules_applied=[]`` behind a stale comment claiming the integrity path
retains nothing scannable. The ``IntegrityContext`` DOES retain bounded
strings — trusted-relationship proof tuples (first element =
``RelationshipProof.graph_record_id``), bounded string ``ClaimInput.value``
scalars, and claim/evidence ``semantic_field``/``locator`` — so
``runtime_app._integrity_rules_applied`` mirrors the legacy extraction:
``usage_tracking.extract_rule_node_ids`` → ONE
``self_learning.get_accepted_rules_by_node_ids`` query → human rows
(matched ⇒ ``format_rule_label``; unmatched ⇒ label == id, sorted).
Best-effort: any failure ⇒ ``[]`` and the three final events still emit.
``final_events`` stores the single extraction on the invocation
(``rule_ids``/``matched_rows``) so the R1 post-turn learning seam
(``_integrity_post_turn_learning`` — see
``test_integrity_post_turn_learning.py``) reuses it as
``precomputed_ids``/``prefetched_rows`` without re-scanning.

Offline: no AWS, no network. Enforce-path tests monkeypatch
``RESPONSE_INTEGRITY_MODE`` (conventions.md).
"""
from __future__ import annotations

import pytest

from agents import runtime_app, self_learning
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, SystemAnswer,
)

SUBJ_ID = "subj-" + "a" * 16
VM_ID = "vm-learned-12345678"

MATCHED_ROW = {
    "neptune_node_id": SUBJ_ID,
    "proposed_type": "value_mapping",
    "from_system": "erp", "from_field": "supplier_id",
    "to_system": "mes", "to_field": "vendor_ref",
    "concept": "supplier",
}


@pytest.fixture()
def enforce_mode(monkeypatch):
    """Enforce-path tests must monkeypatch the env (conventions.md)."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")


def _invocation_with_retained_evidence() -> "runtime_app._RuntimeIntegrityInvocation":
    """A fabricated invocation whose context retains BOTH id shapes.

    The subj id rides a trusted-relationship proof tuple; the vm-learned id
    rides a bounded string ``ClaimInput.value`` — the two retained-string
    sources the extraction scans (plus semantic_field/locator).
    """
    invocation = runtime_app._RuntimeIntegrityInvocation("compare supplier ids")
    invocation.context.trusted_relationships.add(
        (SUBJ_ID, "erp", "S-100", "supplier_id", "mes", "V-100", "vendor_ref"),
    )
    invocation.context.system_answers.append(SystemAnswer(
        status="complete",
        claims=(AnswerClaim(
            claim_id="c1", kind=ClaimKind.VALUE, label="supplier",
            inputs=(ClaimInput(
                label="supplier", value=f"translated via {VM_ID}",
                semantic_field="supplier_ref", locator="row:0",
            ),),
        ),),
        evidence=(),
    ))
    return invocation


def _final_summary(monkeypatch, invocation) -> tuple[list[dict], dict]:
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    events = invocation.final_events()
    summary = next(
        e["intelligence_summary"] for e in events if "intelligence_summary" in e)
    return events, summary


# --------------------------------------------------------------------------- #
# rules_applied — retained-string extraction, matched + unmatched rows
# --------------------------------------------------------------------------- #
def test_final_events_carries_matched_and_unmatched_rules(enforce_mode, monkeypatch):
    invocation = _invocation_with_retained_evidence()
    seen_ids: list[set] = []

    def _lookup(ids):
        seen_ids.append(set(ids))
        return [MATCHED_ROW]

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", _lookup)
    _events, summary = _final_summary(monkeypatch, invocation)
    # ONE lookup, fed by BOTH retained-string sources.
    assert seen_ids == [{SUBJ_ID, VM_ID}]
    rules = summary["rules_applied"]
    assert {"id": SUBJ_ID,
            "label": "value mapping: erp.supplier_id -> mes.vendor_ref "
                     "(concept: supplier)"} in rules
    # Unmatched id falls back label == id (Decision 3).
    assert {"id": VM_ID, "label": VM_ID} in rules
    assert len(rules) == 2


def test_raising_lookup_yields_empty_rules_and_all_three_events(enforce_mode, monkeypatch):
    invocation = _invocation_with_retained_evidence()

    def _boom(_ids):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", _boom)
    events, summary = _final_summary(monkeypatch, invocation)
    assert summary["rules_applied"] == []
    # The three final events still emit: data, intelligence_summary, integrity.
    assert len(events) == 3
    assert "data" in events[0]
    assert "intelligence_summary" in events[1]
    assert "response_integrity" in events[2]


def test_no_retained_evidence_yields_empty_rules_without_lookup(enforce_mode, monkeypatch):
    """HONESTY: nothing retained this turn ⇒ rules_applied == [], no GSI query."""
    invocation = runtime_app._RuntimeIntegrityInvocation("hello")

    def _fail(_ids):  # pragma: no cover - defended against
        raise AssertionError("lookup must not run without extracted ids")

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", _fail)
    _events, summary = _final_summary(monkeypatch, invocation)
    assert summary["rules_applied"] == []


def test_helper_is_best_effort_on_hostile_context():
    """A context missing the retained attributes entirely ⇒ [] (never raises)."""
    assert runtime_app._integrity_rules_applied(object()) == []


# --------------------------------------------------------------------------- #
# planned_with — integrity path carries the prelude capture; None ⇒ absent
# --------------------------------------------------------------------------- #
def test_final_events_carries_planned_with_when_set(enforce_mode, monkeypatch):
    invocation = runtime_app._RuntimeIntegrityInvocation("hello")
    invocation.planned_with = {"systems": 2, "shared_concepts": ["work order"],
                               "join_paths": 1, "value_mappings": 0,
                               "identity_rules": 3}
    _events, summary = _final_summary(monkeypatch, invocation)
    assert summary["planned_with"]["systems"] == 2
    assert summary["planned_with"]["shared_concepts"] == ["work order"]
    assert summary["planned_with"]["identity_rules"] == 3


def test_final_events_omits_planned_with_by_default(enforce_mode, monkeypatch):
    """HONESTY: no route seed this turn ⇒ no planned_with key."""
    invocation = runtime_app._RuntimeIntegrityInvocation("hello")
    _events, summary = _final_summary(monkeypatch, invocation)
    assert "planned_with" not in summary
