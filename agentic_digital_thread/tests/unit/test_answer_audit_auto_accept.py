# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``answer-audit`` proposals never auto-accept (ADR-0062 D6) — BEHAVIORAL pin.

A findings ``suggested_mapping`` reaches durable storage through exactly ONE
door: a steward-triggered ``POST /findings/{id}/file-proposal`` calling
``log_proposed_mapping(source="answer-audit")``. Those mapping VALUES are
LLM-authored untrusted text (ADR-0062 D8), so filing one means "worth
reviewing", never "apply this" — the row must land PENDING and stay pending
until a steward accepts it. ``"answer-audit"`` therefore sits in
``self_learning._NEVER_AUTO_ACCEPT_SOURCES`` beside ``"user-feedback"``.

Pinned BEHAVIORALLY, not by tuple membership: a high-confidence
``answer-audit`` proposal is inserted into the in-memory proposal store and the
REAL sweep is driven through ``server._run_auto_accept_sweep`` (the one wrapper
the scheduler safety-net and the post-``/api/ask`` hook both call). Every gate
the sweep consults runs for real — ``get_pending_mappings`` reads the store,
``proposal_scoring.score_proposal`` scores the row, ``_auto_accept_block_reason``
runs its UUID-target check, and ``accept_mapping`` flips real status rows. Only
the Neptune read/write seams are stubbed (offline lane).

EVERY deny assertion carries a same-sweep POSITIVE CONTROL from a
non-protected source: without it the test would pass vacuously the moment the
sweep breaks, is renamed into a no-op, or stops accepting anything at all.
The tuple-membership assertion is kept as a cheap by-construction companion —
it is NOT the pin.
"""
from __future__ import annotations

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes
import agents.server as server

_UUID_AUDIT = "11111111-2222-3333-4444-555555555555"
_UUID_AGENT = "66666666-7777-8888-9999-aaaaaaaaaaaa"


class FakeTable:
    """In-memory stand-in for ``adt-proposed-mappings`` (lifecycle-test idiom).

    Keyed by the real ``(mapping_id, discovered_at)`` PK/SK. ``query`` handles
    the exact kwargs ``self_learning`` emits: the ``status-index`` GSI
    (``get_pending_mappings``), the ``subject-key-index`` GSI, and the
    main-table query by ``mapping_id`` (``accept_mapping``).
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    # -- assertion helpers -------------------------------------------------- #
    def by_id(self, mapping_id: str) -> dict:
        for item in self._items.values():
            if item.get("mapping_id") == mapping_id:
                return dict(item)
        raise KeyError(mapping_id)

    # -- DynamoDB-shaped surface -------------------------------------------- #
    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:  # main-table query by mapping_id (accept_mapping)
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        assert UpdateExpression.strip().upper().startswith("SET ")
        for clause in UpdateExpression.strip()[4:].split(","):
            lhs, rhs = (s.strip() for s in clause.split("="))
            item[names.get(lhs, lhs)] = values[rhs]

    def scan(self, **kwargs) -> dict:
        return {"Items": [dict(v) for v in self._items.values()]}


def _pending_row(mapping_id: str, source: str, to_value: str,
                 discovered_at: str) -> dict:
    """A PENDING row whose shape scores 'high' under the REAL scorer.

    ``value_mapping`` + canonical-UUID ``to_value`` + confidence ≥ 0.85 is the
    one combination ``proposal_scoring._score_value_mapping`` calls high, so
    the only thing separating the two fixtures below is ``source``.
    """
    return {
        "mapping_id": mapping_id,
        "discovered_at": discovered_at,
        "status": "pending",
        "proposed_type": "value_mapping",
        "source": source,
        "from_system": "erp",
        "from_field": "work_orders.lot_id",
        "from_value": f"LOT-{mapping_id}",
        "to_system": "mes",
        "to_field": "batches.batch_uuid",
        "to_value": to_value,
        "concept": "lot",
        "confidence": "0.92",
        "method": "value_join",
        "subject_key": f"vm|erp|work_orders.lot_id|LOT-{mapping_id}|mes",
        "version": "1",
    }


def _install(monkeypatch, table: FakeTable) -> list[str]:
    """Wire the REAL self_learning functions onto ``table``; stub only the
    Neptune seams. Returns the list the write seam records cypher into."""
    writes: list[str] = []

    def _write(cypher, parameters=None):
        writes.append(cypher)
        return True

    def _read(cypher, parameters=None):
        if "count(s)" in cypher:          # _require_graph_systems probe
            return [{"n": 1}]
        if "n.id = $id" in cypher:        # scoped _verify_target probe
            return [{"id": (parameters or {}).get("id", ""), "name": "",
                     "system": (parameters or {}).get("sid", "")}]
        return []

    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", _write)
    route_returning_writes(monkeypatch, _write)
    monkeypatch.setattr(self_learning, "_neptune_query", _read)
    return writes


# --------------------------------------------------------------------------- #
# THE pin: the real sweep leaves answer-audit pending, same pass, live control
# --------------------------------------------------------------------------- #
def test_sweep_leaves_answer_audit_pending_while_sibling_accepts(monkeypatch):
    """Kills: dropping ``answer-audit`` from ``_NEVER_AUTO_ACCEPT_SOURCES``, or
    removing the source deny from ``sweep_auto_accept``.

    Two proposals identical but for ``source`` sit pending in one store. The
    real sweep runs once through the production wrapper. The ``agent`` row must
    come out ACCEPTED (the control that makes this test non-vacuous) and the
    ``answer-audit`` row must still be PENDING with no accept side effects.
    """
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    table = FakeTable()
    writes = _install(monkeypatch, table)
    table.put_item(_pending_row("m-audit", "answer-audit", _UUID_AUDIT,
                                "2026-08-18T10:00:00+00:00"))
    table.put_item(_pending_row("m-agent", "agent", _UUID_AGENT,
                                "2026-08-18T10:00:01+00:00"))

    result = server._run_auto_accept_sweep("post-query")

    # Positive control: the sweep DID accept — it is neither broken nor a no-op.
    assert result == {"accepted": 1, "failed": 0}
    control = table.by_id("m-agent")
    assert control["status"] == "accepted"
    assert control["neptune_node_id"]
    assert control["accepted_at"]
    assert any("ValueMapping" in cypher for cypher in writes)

    # The deny: same shape, protected source ⇒ still pending, nothing applied.
    audit = table.by_id("m-audit")
    assert audit["status"] == "pending"
    assert "accepted_at" not in audit
    assert "neptune_node_id" not in audit
    # Not even the UUID-verification stamp ran: the source deny precedes it.
    assert "verified" not in audit
    assert not any(_UUID_AUDIT in cypher for cypher in writes)


# --------------------------------------------------------------------------- #
# Ingestion-time path: _maybe_auto_accept denies the same source
# --------------------------------------------------------------------------- #
def test_ingestion_auto_accept_leaves_answer_audit_pending(monkeypatch):
    """Kills: removing the source deny from ``_maybe_auto_accept`` (the
    defense-in-depth twin — a proposal filed while the flag is on must not be
    accepted at write time either)."""
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    table = FakeTable()
    _install(monkeypatch, table)
    audit = _pending_row("m-audit", "answer-audit", _UUID_AUDIT,
                         "2026-08-18T10:00:00+00:00")
    control = _pending_row("m-agent", "agent", _UUID_AGENT,
                           "2026-08-18T10:00:01+00:00")
    table.put_item(audit)
    table.put_item(control)

    # Positive control: this exact shape DOES auto-accept at ingestion.
    assert self_learning._maybe_auto_accept("m-agent", control) is True
    assert table.by_id("m-agent")["status"] == "accepted"

    assert self_learning._maybe_auto_accept("m-audit", audit) is False
    assert table.by_id("m-audit")["status"] == "pending"


# --------------------------------------------------------------------------- #
# By-construction companion (NOT the pin)
# --------------------------------------------------------------------------- #
def test_source_deny_tuple_carries_all_untrusted_sources():
    """ADR-0062 D6 plus ADR-0066 D3 plus ADR-0050 (a non-steward's pending
    ``user`` assertion): every source that needs a steward accept is pinned
    exactly so removing any entry fails here as well as behaviorally."""
    assert self_learning._NEVER_AUTO_ACCEPT_SOURCES == (
        "user-feedback", "answer-audit", "conversation", "user")
