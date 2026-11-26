# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0051 insight lifecycle quality — per-finding grain, TTL, graduation.

Offline behavioral proof of the P1/P2 fixes from the 2026-07-24 learning-quality
audits:

  P1 — distinct findings on the same (system, topic) COEXIST as separate
       pendings (the audited defect: they superseded each other into ADR-0042
       quarantine); an identical re-observed finding is a replace-in-place
       dup no-op (pending cap 1 per finding); stale pending insights expire
       lazily after ``INSIGHT_PENDING_TTL_DAYS`` (30d).
  P2 — three corroborating findings on one (from_system, to_system, concept)
       group graduate into ONE pending ``correlation`` proposal; two do not;
       the graduated proposal NEVER auto-accepts (deny-tuple), and its accept
       path MERGEs a Correlation node (never a ValueMapping).

No AWS / Neptune / LLM / network — seams monkeypatched per the
``test_self_learning_lifecycle.py`` conventions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import agents.proposal_scoring as proposal_scoring
import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes


class FakeTable:
    """In-memory stand-in for the proposals table (lifecycle-test idiom)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    def of_status(self, status: str) -> list[dict]:
        return [i for i in self.all_items() if i.get("status") == status]

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
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **kwargs) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        expr = UpdateExpression.strip()
        if expr.upper().startswith("SET "):
            for clause in expr[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]

    def scan(self, **kwargs) -> dict:
        return {"Items": self.all_items()}


def _install(monkeypatch, table: FakeTable) -> list[tuple]:
    neptune_calls: list[tuple] = []
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda c, p=None: neptune_calls.append((c, p)) or True)
    route_returning_writes(monkeypatch, self_learning._neptune_write)
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [])
    return neptune_calls


def _log_insight(narrative: str, *, from_system="sys-a", to_system="sys-b",
                 concept="correlation") -> str | None:
    return self_learning.log_proposed_mapping(
        from_system, "", "", to_system, "", "",
        concept, 0.7, narrative, proposed_type="insight",
    )


def _backdate(table: FakeTable, mapping_id: str, days: float) -> None:
    """Rewrite a row's discovered_at to ``days`` ago (key change → re-insert)."""
    for key, item in list(table._items.items()):
        if item["mapping_id"] != mapping_id:
            continue
        del table._items[key]
        item["discovered_at"] = (
            datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        table._items[(item["mapping_id"], item["discovered_at"])] = item


# =========================================================================== #
# P1 — per-finding grain: distinct findings coexist, identical dedupes
# =========================================================================== #
def test_distinct_findings_coexist(monkeypatch):
    # The audited churn defect: a second, DIFFERENT finding on the same
    # (system, topic) used to SUPERSEDE the first and feed the ADR-0042
    # quarantine. Post-ADR-0051 they are distinct subjects and coexist.
    fake = FakeTable()
    _install(monkeypatch, fake)
    first = _log_insight("downtime spikes precede quality drops")
    second = _log_insight("operator shift changes correlate with rework")
    assert first and second and first != second
    pending = fake.of_status("pending")
    assert len(pending) == 2                    # both findings, both pending
    assert fake.of_status("superseded") == []   # NO supersede
    assert all(r.get("quarantined") != "true" for r in pending)  # NO quarantine


def test_identical_finding_is_noop(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    first = _log_insight("downtime spikes precede quality drops")
    again = _log_insight("downtime spikes precede quality drops")
    assert again == first                       # dup no-op returns existing id
    assert len(fake.of_status("pending")) == 1  # pending cap 1 per finding


# =========================================================================== #
# P1 — 30d TTL: stale pending insights expire lazily
# =========================================================================== #
def test_stale_insight_pending_expires_after_ttl(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    stale = _log_insight("an old finding nobody adjudicated")
    fresh = _log_insight("a recent finding")
    _backdate(fake, stale, days=self_learning.INSIGHT_PENDING_TTL_DAYS + 1)

    live = self_learning.get_pending_mappings()   # lazy expiry at read time

    assert [r["mapping_id"] for r in live] == [fresh]
    expired_row = fake.by_id(stale)
    assert expired_row["status"] == "expired"
    assert expired_row["expired_at"]


def test_fresh_insight_pending_does_not_expire(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    fresh = _log_insight("a recent finding")
    _backdate(fake, fresh, days=self_learning.INSIGHT_PENDING_TTL_DAYS - 1)
    live = self_learning.get_pending_mappings()
    assert [r["mapping_id"] for r in live] == [fresh]
    assert fake.by_id(fresh)["status"] == "pending"


def test_stale_pending_expires_at_propose_time_and_fresh_row_inserts(monkeypatch):
    # Lazy expiry also runs on the propose path's prior snapshot: an identical
    # re-observed finding must not dup against a stale pending row.
    fake = FakeTable()
    _install(monkeypatch, fake)
    stale = _log_insight("recurring finding")
    _backdate(fake, stale, days=self_learning.INSIGHT_PENDING_TTL_DAYS + 1)

    fresh = _log_insight("recurring finding")

    assert fresh is not None and fresh != stale
    assert fake.by_id(stale)["status"] == "expired"
    assert fake.by_id(fresh)["status"] == "pending"


# =========================================================================== #
# P2 — graduation ladder: N=3 distinct corroborations → ONE correlation
# =========================================================================== #
def _corroborate(n: int) -> None:
    for i in range(n):
        assert _log_insight(f"corroborating finding number {i} on throughput",
                            concept="throughput")


def test_three_corroborations_graduate_to_one_correlation_proposal(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _corroborate(3)

    result = self_learning.sweep_insight_graduation()

    assert result["graduated"] == 1
    correlations = [r for r in fake.all_items()
                    if r.get("proposed_type") == "correlation"]
    assert len(correlations) == 1
    (corr,) = correlations
    assert corr["status"] == "pending"          # never auto-applied
    assert corr["source"] == "agent"
    assert "3 corroborating findings" in corr["query_context"]
    # Idempotent re-run: same evidence → dup_pending, still ONE row.
    self_learning.sweep_insight_graduation()
    assert len([r for r in fake.all_items()
                if r.get("proposed_type") == "correlation"]) == 1


def test_two_corroborations_do_not_graduate(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _corroborate(2)
    result = self_learning.sweep_insight_graduation()
    assert result["graduated"] == 0
    assert result["skipped_below_threshold"] == 1
    assert [r for r in fake.all_items()
            if r.get("proposed_type") == "correlation"] == []


def test_single_system_findings_never_graduate(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    for i in range(3):
        _log_insight(f"single-system narrative {i}", to_system="")
    result = self_learning.sweep_insight_graduation()
    assert result["graduated"] == 0


def test_graduated_proposal_never_auto_accepts(monkeypatch):
    # Extends the test_insight_never_auto_accepted.py pattern: flag ON and the
    # scorer forced high — the deny-tuple still excludes `correlation` in BOTH
    # auto-accept paths, and the freshly-graduated row stays pending.
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda m: {"ai_confidence": "high"})
    fake = FakeTable()
    _install(monkeypatch, fake)
    accepts: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepts.append(mid) or {"status": "accepted"})
    _corroborate(3)
    accepts.clear()  # ignore any pre-graduation accept spies

    assert self_learning.sweep_insight_graduation()["graduated"] == 1
    (corr,) = [r for r in fake.all_items()
               if r.get("proposed_type") == "correlation"]
    assert corr["status"] == "pending"
    assert corr["mapping_id"] not in accepts

    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}
    assert "correlation" in self_learning._NEVER_AUTO_ACCEPT_TYPES


def test_correlation_review_card_renders_gracefully_server_side():
    # No frontend edits: the shared scorer's unknown-type fallback carries the
    # review card (P2 plan requirement — reuse, don't extend, the renderer).
    s = proposal_scoring.score_proposal({"proposed_type": "correlation"})
    assert s["ai_confidence"] == "low"
    assert s["ai_recommendation"]


def test_accepting_correlation_writes_correlation_node(monkeypatch):
    fake = FakeTable()
    neptune_calls = _install(monkeypatch, fake)
    _corroborate(3)
    assert self_learning.sweep_insight_graduation()["graduated"] == 1
    (corr,) = [r for r in fake.all_items()
               if r.get("proposed_type") == "correlation"]

    result = self_learning.accept_mapping(corr["mapping_id"])

    assert result["status"] == "accepted"
    merged = [c for c, _p in neptune_calls if "MERGE (c:Correlation" in c]
    assert merged, "accepting a correlation must MERGE a Correlation node"
    assert not any("MERGE (vm:ValueMapping" in c for c, _p in neptune_calls)
