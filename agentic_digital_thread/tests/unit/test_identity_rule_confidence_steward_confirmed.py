# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A steward-confirmed identity rule: the relationship is human-confirmed; the
stored working confidence of a model-supplied number is capped.

Two writers store a rule a PERSON confirmed, with method ``user_confirmed``
(the route seed's top tier):

* accepting a learning proposal (``self_learning.accept_mapping`` ->
  ``_apply_identity_rule``, also on re-accept after a requeue). The steward
  confirms the relationship; the proposal's confidence is the model's own
  number (ADR-0043: it does not discriminate). The rule's working
  ``ir.confidence`` is min(it, ``identifier_translation.MODEL_CONFIDENCE_CEILING``);
  the model's number stays on the proposal's ledger row and is never written to
  the graph node (the chat's free graph queries could enumerate a
  node's properties and read it back).
* ``/api/thread/learn`` (``resolve._learn_identity_rule``): a steward teaching a
  rule directly. The number is the one the steward's request carries (the
  app's own Thread proposals send 0.7), a human assertion, so it is kept.

A steward's own correction through the user-define route
(``record_user_correction``) is the same kind of direct assertion and keeps
its number too. The two readers that order on the stored number are pinned on
the rules these writers store: two confirmed rules no longer split on a
model's number, and the tie falls to the rule id.

Neutral fixture: a shift log (``log-sys``) and a line's records (``line-sys``).
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.resolve as resolve  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents import orchestrator  # noqa: E402
from agents.identifier_translation import find_translations  # noqa: E402
from agents.models import LearnRequest  # noqa: E402
from tests.unit.test_requeue_mapping import FakeTable, NeptuneSpy  # noqa: E402

_RULE_MERGE = "MERGE (ir:IdentityRule"


class _Ledger(FakeTable):
    """``test_requeue_mapping``'s table, also answering the subject-key index
    and ``put_item`` the correction door uses."""

    def query(self, **kwargs) -> dict:
        if kwargs.get("IndexName") == "subject-key-index":
            key = kwargs["ExpressionAttributeValues"][":sk"]
            return {"Items": [dict(i) for i in self._items.values() if i.get("subject_key") == key]}
        return super().query(**kwargs)

    def put_item(self, Item) -> None:
        self.seed(Item)


@pytest.fixture
def ledger(monkeypatch):
    """The proposal table and the graph-write seam ``test_requeue_mapping``
    uses, plus an empty graph read for the accept's category check."""
    table, spy = _Ledger(), NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write_loud", spy)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    return table, spy


def _pending_rule_proposal(confidence) -> dict:
    """A pending identity-rule proposal as the ledger stores it (DynamoDB
    hands numbers back as ``Decimal``)."""
    return {
        "mapping_id": "m-ir-1", "discovered_at": "2026-10-01T00:00:00Z",
        "status": "pending", "proposed_type": "identity_rule", "version": "1",
        "subject_key": "identity_rule|log-sys|signed_by|line-sys|closed_by",
        "from_system": "log-sys", "from_field": "shift_notes.signed_by",
        "to_system": "line-sys", "to_field": "stops.closed_by",
        "concept": "operator", "confidence": confidence, "method": "agent_analysis",
        "source": "agent", "query_context": "who closed the stop",
    }


def _rule_merges(spy) -> list:
    return [(q, p) for q, p in spy.calls if q.startswith(_RULE_MERGE)]


def _taught_rule(monkeypatch, confidence: float, **over) -> tuple:
    """``(query, params)`` of the one IdentityRule ``/api/thread/learn`` writes."""
    writes = []
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    monkeypatch.setattr(resolve, "_validate_learn_target", lambda *_a, **_k: None)
    fields = {"concept": "operator", "source_system": "log-sys", "source_field": "signed_by",
              "source_pattern": "*", "target_system": "line-sys", "target_field": "closed_by",
              "target_pattern": "*", "confidence": confidence, **over}
    result = resolve._learn_identity_rule(LearnRequest(**fields))
    assert "error" not in result, result
    (rule,) = [(q, p) for q, p in writes if "CREATE (ir:IdentityRule" in q]
    return rule


# ── the writers ──────────────────────────────────────────────────────────────

def test_user_confirmed_is_not_a_measured_method():
    assert not onboard._has_measured_method({"method": "user_confirmed"})


def test_a_rule_taught_through_thread_learn_keeps_its_confidence(monkeypatch):
    """Pin: a steward's direct teach is a human assertion; its number stays."""
    query, params = _taught_rule(monkeypatch, 0.96, example_identifier="tech_07",
                                 example_matched_value="tech_07")
    assert params["method"] == "user_confirmed"
    assert params["confidence"] == 0.96
    assert "proposed_confidence" not in query


def test_an_accepted_proposal_is_capped_and_its_number_stays_on_the_ledger(ledger):
    """REPRODUCTION, through the public lifecycle: accept a
    pending proposal the model scored 0.96, requeue it, accept the persisted
    row again. Every MERGE writes the working confidence 0.7 and carries the
    model's 0.96 nowhere; the re-accept restores the rule to routing. The ledger row keeps the proposal's own number, so a
    re-accept caps it again rather than reading a capped copy."""
    table, spy = ledger
    table.seed(_pending_rule_proposal(Decimal("0.96")))
    first = self_learning.accept_mapping("m-ir-1", actor="steward@example.com")
    assert first["status"] == "accepted", first
    assert self_learning.requeue_mapping("m-ir-1", actor="steward@example.com")[
        "graph_artifact"] == "deactivated"
    again = self_learning.accept_mapping("m-ir-1", actor="steward@example.com")
    assert again["status"] == "accepted", again
    merges = _rule_merges(spy)
    assert len(merges) == 2
    for query, params in merges:
        assert "ir.method = 'user_confirmed'" in query and "ir.status = 'active'" in query
        assert params["conf"] == 0.7
        assert "proposed_confidence" not in query and 0.96 not in params.values()
        assert params["sid"] == first["neptune_node_id"] and params["pid"] == "m-ir-1"
    assert table.by_id("m-ir-1")["confidence"] == Decimal("0.96")


def test_an_accepted_proposal_under_the_ceiling_keeps_its_number(ledger):
    """Pin: the ceiling is a ceiling. A proposal the model scored 0.5 is
    written at 0.5."""
    table, spy = ledger
    table.seed(_pending_rule_proposal(Decimal("0.5")))
    assert self_learning.accept_mapping("m-ir-1", actor="steward@example.com")["status"] == "accepted"
    ((_query, params),) = _rule_merges(spy)
    assert params["conf"] == 0.5


def test_a_stewards_own_correction_keeps_its_number(ledger):
    """Pin: a steward's own identity-rule correction (the user-define route's
    steward door) is a direct assertion like ``/api/thread/learn``: its
    number is kept."""
    _table, spy = ledger
    result = self_learning.record_user_correction(
        "log-sys", "shift_notes.signed_by", "", "line-sys", "stops.closed_by", "",
        "operator", proposed_type="identity_rule", confidence=0.96,
        actor="steward@example.com")
    assert result["status"] == "accepted", result
    ((query, params),) = _rule_merges(spy)
    assert params["conf"] == 0.96
    assert "proposed_confidence" not in query


def _correction_row_id(table) -> str:
    (row,) = [r for r in table._items.values() if r.get("method") == "user_corrected"]
    return row["mapping_id"]


def test_a_requeued_stewards_correction_keeps_its_number_on_re_accept(ledger):
    """REPRODUCTION: a steward's correction sent back to pending
    (Requeue, then Accept) was capped to 0.7 on the re-accept, since
    the accept path capped every row. The number is the steward's own (the
    row says ``source='user'``, ``method='user_corrected'``), so it is kept on
    the ledger and on every MERGE."""
    table, spy = ledger
    self_learning.record_user_correction(
        "log-sys", "shift_notes.signed_by", "", "line-sys", "stops.closed_by", "",
        "operator", proposed_type="identity_rule", confidence=0.96,
        actor="steward@example.com")
    mapping_id = _correction_row_id(table)
    assert self_learning.requeue_mapping(mapping_id, actor="steward@example.com")[
        "graph_artifact"] == "deactivated"
    again = self_learning.accept_mapping(mapping_id, actor="steward@example.com")
    assert again["status"] == "accepted", again
    merges = _rule_merges(spy)
    assert len(merges) == 2
    assert [p["conf"] for _q, p in merges] == [0.96, 0.96]
    assert float(table.by_id(mapping_id)["confidence"]) == 0.96


def test_a_proposal_that_names_the_correction_method_is_still_capped(ledger):
    """Guard: only a steward's correction row keeps its number. An agent's
    proposal whose method field reads ``user_corrected`` (a filer chooses its
    row's method) is still a model's number and is capped."""
    table, spy = ledger
    table.seed({**_pending_rule_proposal(Decimal("0.96")), "method": "user_corrected"})
    assert self_learning.accept_mapping("m-ir-1", actor="steward@example.com")["status"] == "accepted"
    ((_query, params),) = _rule_merges(spy)
    assert params["conf"] == 0.7


# ── the readers that order on the stored number ──────────────────────────────

def _seed_row(sid: str, conf: float, source_pattern: str = "*", target_pattern: str = "*") -> dict:
    """A route-seed identity-rule entry for a stored rule (the traversal's own
    projection)."""
    return {"id": sid, "method": "user_confirmed", "confidence": conf,
            "source_system": "log-sys", "source_field": "signed_by",
            "source_pattern": source_pattern, "target_system": "line-sys",
            "target_field": "closed_by", "target_pattern": target_pattern}


def _two_confirmed_rules(ledger, monkeypatch) -> tuple:
    """Two ``user_confirmed`` rules for one field pair, each written by its
    real writer: a proposal the model scored 0.96, accepted, and a rule a
    steward taught at 0.70 (what the app's Thread proposals send). Returns
    their route-seed entries and ids ``(rows, accepted_id, taught_id)``."""
    table, spy = ledger
    table.seed(_pending_rule_proposal(Decimal("0.96")))
    accepted = self_learning.accept_mapping("m-ir-1", actor="steward@example.com")
    ((_q, acc),) = _rule_merges(spy)
    _query, taught = _taught_rule(monkeypatch, 0.70, source_pattern="A{id}",
                                  target_pattern="Q{id}")
    rows = [_seed_row(accepted["neptune_node_id"], acc["conf"]),
            _seed_row(taught["id"], taught["confidence"])]
    return rows, accepted["neptune_node_id"], taught["id"]


def test_the_route_seed_no_longer_orders_confirmed_rules_on_the_models_number(ledger, monkeypatch):
    """REPRODUCTION. The route seed orders a tier by confidence,
    then the rule id (``orchestrator._rule_sort_key``, the traversal's ORDER BY
    too), and caps each list at 50. The accepted proposal came first on its
    model number; now both rules stand at 0.70 and the tie falls to the rule
    id, so the taught rule (``ir-learned-...``) comes before the accepted one
    (``subj-...``)."""
    rows, accepted_id, taught_id = _two_confirmed_rules(ledger, monkeypatch)
    ordered = sorted(rows, key=orchestrator._rule_sort_key)
    assert [r["id"] for r in ordered] == [taught_id, accepted_id]
    assert [r["confidence"] for r in ordered] == [0.7, 0.7]


def test_a_conflicting_translation_no_longer_goes_to_the_models_number(ledger, monkeypatch):
    """REPRODUCTION. The identifier translations keep, for one
    identifier into one target field, the rule with the better (tier,
    confidence) and, on a tie, the first in the route seed's order. The
    accept writer stores the bare ``*`` pair, which translates nothing, so
    the two rules are given the transforms the reviewer probed (one
    identifier into one target field, two ways). The accepted rule's 0.96
    won (``K-123``); now the tie goes to the first rule in the route seed's
    order, the taught one (``Q123``)."""
    rows, accepted_id, taught_id = _two_confirmed_rules(ledger, monkeypatch)
    patterns = {accepted_id: ("A{id}", "K-{id}"), taught_id: ("A{id}", "Q{id}")}
    translating = [{**r, "source_pattern": patterns[r["id"]][0],
                    "target_pattern": patterns[r["id"]][1]}
                   for r in sorted(rows, key=orchestrator._rule_sort_key)]
    (found,) = find_translations("which stops did A123 close", translating)
    assert (found["translated"], found["confidence"]) == ("Q123", 0.7)


def test_no_graph_writer_stores_a_proposals_own_number():
    """Guard: the chat's ``explore_graph`` runs the model's own
    read-only queries, and ``UNWIND keys(ir) AS k RETURN k, ir[k]`` reads back
    any property a node has under any alias. So an accepted proposal's own
    number is kept off the graph entirely: it lives on the proposal's ledger
    row, which the rule's ``proposal_id`` points to."""
    from pathlib import Path
    agents_dir = Path(self_learning.__file__).parent
    offenders = [p.name for p in sorted(agents_dir.glob("*.py"))
                 if "proposed_confidence" in p.read_text(encoding="utf-8")]
    assert offenders == []
