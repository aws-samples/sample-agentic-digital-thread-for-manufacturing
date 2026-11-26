# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Subject-level churn controls — cooldown + flip-flop quarantine (ADR-0042, offline).

Behavioral proof of the D1/D2 branches in ``self_learning._classify_collision``
plus the R1 ambiguity review reason (D4), against the in-memory ``FakeTable`` /
``NeptuneSpy`` doubles shared with ``test_self_learning_lifecycle.py``. No AWS,
no Neptune, no network.

Pinned contract (ADR-0042):
  - D1 COOLDOWN: an agent proposal that would supersede on a subject whose most
    recent supersede is inside ``SUBJECT_SUPERSEDE_COOLDOWN_HOURS`` (24h) is a
    dup-style no-op — no new row, no supersede, the active mapping id returned;
    outside the window the supersede proceeds; the FIRST supersede on a subject
    is never cooled (there is no prior supersede to be "recent").
  - D2 CIRCUIT BREAKER: once ``SUBJECT_FLIPFLOP_QUARANTINE_THRESHOLD`` (3)
    supersedes have accrued on a subject, the next different-target agent
    proposal is stored ``pending`` with ``quarantined="true"`` and a stated
    ``review_reason`` naming the flip-flop count and the distinct targets;
    prior active rows are NOT superseded; the subject is FROZEN — further agent
    proposals (any target) also quarantine — until a human accept/reject; a
    USER correction cuts through the freeze (user source always wins).
  - D2 + auto-accept: a quarantined row is frozen until a HUMAN accept/reject —
    neither ``_maybe_auto_accept`` nor ``sweep_auto_accept`` ever touches it.
  - R1 (D4): an ambiguous system reference ("MES" matching several MES systems)
    stores its proposal with a ``review_reason`` naming the candidates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import agents.self_learning as self_learning

import pytest

from tests.unit._value_mapping_write_seam import route_returning_writes


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    this suite keeps pinning its own concern with single-call inserts; the
    buffer itself is covered by test_self_learning_lifecycle.py."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy

try:
    from unit._demo_catalog import DEMO_SYSTEMS
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from _demo_catalog import DEMO_SYSTEMS


# --------------------------------------------------------------------------- #
# Wiring helpers
# --------------------------------------------------------------------------- #
def _install(monkeypatch, table: FakeTable) -> NeptuneSpy:
    neptune = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", neptune)
    route_returning_writes(monkeypatch, neptune)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return neptune


def _log_jp(*, to_field: str = "accounts.id", source: str = "agent") -> str | None:
    """A well-shaped join_path on ONE subject (erp, orders.customer_id).

    Different target = different ``to_field`` (a join_path's target is
    (to_system, to_field)), so the flip-flop review reason names DISTINCT
    ``to_system.to_field`` targets.
    """
    return self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "",
        "crm", to_field, "",
        "customer", 0.9, "ctx",
        proposed_type="join_path", source=source,
    )


def _backdate_supersedes(fake: FakeTable, *, hours: float) -> None:
    """Rewrite every superseded row's superseded_at to ``hours`` ago (test-side
    clock control — the code path reads the stored timestamps)."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for item in fake._items.values():
        if item.get("status") == "superseded":
            item["superseded_at"] = ts


def _drive_to_quarantine(fake: FakeTable) -> tuple[str, str]:
    """3 supersedes on one subject, then a 4th different-target proposal.

    Returns ``(last_active_id, quarantined_id)``. Each supersede's timestamp is
    backdated past the 24h cooldown so D1 never masks the D2 threshold.
    """
    _log_jp(to_field="a.id")
    _log_jp(to_field="b.id")           # supersede #1
    _backdate_supersedes(fake, hours=25)
    _log_jp(to_field="c.id")           # supersede #2
    _backdate_supersedes(fake, hours=25)
    last_active = _log_jp(to_field="d.id")   # supersede #3 → threshold reached
    _backdate_supersedes(fake, hours=25)
    quarantined = _log_jp(to_field="e.id")   # 4th different target → quarantine
    return last_active, quarantined


# --------------------------------------------------------------------------- #
# D2 — flip-flop circuit breaker (quarantine)
# --------------------------------------------------------------------------- #
def test_flipflop_quarantine_after_threshold(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    last_active, quarantined = _drive_to_quarantine(fake)

    q_item = fake.by_id(quarantined)
    assert q_item["status"] == "pending"
    assert q_item["quarantined"] == "true"
    # Stated reason: names the flip-flop count and the distinct targets.
    reason = q_item["review_reason"]
    assert "flip-flop" in reason
    assert "3 times" in reason
    assert "crm.a.id" in reason
    assert "crm.d.id" in reason
    # Prior active row is NOT superseded (nothing superseded by a quarantine).
    assert fake.by_id(last_active)["status"] == "pending"


def test_frozen_subject_further_agent_proposals_also_quarantine(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    last_active, _ = _drive_to_quarantine(fake)

    # 5th proposal on the frozen subject (yet another target) → also quarantined.
    fifth = _log_jp(to_field="f.id")
    fifth_item = fake.by_id(fifth)
    assert fifth_item["status"] == "pending"
    assert fifth_item["quarantined"] == "true"
    assert "flip-flop" in fifth_item["review_reason"]
    # Still no supersede churn while frozen.
    assert fake.by_id(last_active)["status"] == "pending"


def _set_ts(fake: FakeTable, mapping_id: str, field: str, *, hours: float) -> None:
    """Set one row's timestamp attribute to ``hours`` ago (test-side clock)."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for item in fake._items.values():
        if item.get("mapping_id") == mapping_id:
            item[field] = ts


def test_human_adjudication_re_arms_the_breaker(monkeypatch):
    """ADR-0042 D2 re-arm: after a human accept/reject of the quarantined row,
    the next agent proposal does NOT quarantine — the normal supersede path
    resumes (the threshold input counts supersedes SINCE the adjudication, not
    the monotonic lifetime count)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    last_active, quarantined = _drive_to_quarantine(fake)

    # Human adjudication: reject the quarantined row (stamps rejected_at).
    result = self_learning.reject_mapping(quarantined)
    assert result == {"status": "rejected"}

    # Next agent proposal on the subject: no quarantine — normal supersede.
    nxt = _log_jp(to_field="f.id")
    item = fake.by_id(nxt)
    assert item["status"] == "pending"
    assert "quarantined" not in item
    assert fake.by_id(last_active)["status"] == "superseded"


def test_fresh_flipflop_streak_after_adjudication_re_trips_at_threshold(monkeypatch):
    """A NEW flip-flop streak after the adjudication re-trips the breaker at
    the same threshold (3): only supersedes dated after the adjudication count."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    last_active, quarantined = _drive_to_quarantine(fake)

    self_learning.reject_mapping(quarantined)
    # Place the adjudication in the past and the ORIGINAL streak before it —
    # settled history must never count again.
    _set_ts(fake, quarantined, "rejected_at", hours=300)
    _backdate_supersedes(fake, hours=400)

    # Fresh streak: 3 supersedes AFTER the adjudication (each outside the 24h
    # cooldown so D1 never masks D2).
    first = _log_jp(to_field="f.id")          # supersedes last_active (#1)
    _set_ts(fake, last_active, "superseded_at", hours=100)
    second = _log_jp(to_field="g.id")         # supersede #2
    _set_ts(fake, first, "superseded_at", hours=50)
    third = _log_jp(to_field="h.id")          # supersede #3 → threshold reached
    _set_ts(fake, second, "superseded_at", hours=30)

    fourth = _log_jp(to_field="i.id")         # 4th different target → quarantine
    q_item = fake.by_id(fourth)
    assert q_item["status"] == "pending"
    assert q_item["quarantined"] == "true"
    assert "flip-flop" in q_item["review_reason"]
    # The active row of the fresh streak is NOT superseded by the quarantine.
    assert fake.by_id(third)["status"] == "pending"


def test_user_correction_cuts_through_the_freeze(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    last_active, quarantined = _drive_to_quarantine(fake)

    # USER correction on the frozen subject → normal supersede (user wins);
    # the quarantined pending row is superseded too (quarantine cleared path).
    result = self_learning.record_user_correction(
        "erp", "orders.customer_id", "",
        "crm", "accounts.parent_id", "",
        "customer", proposed_type="join_path",
    )
    assert result["status"] == "accepted"
    assert fake.by_id(last_active)["status"] == "superseded"
    assert fake.by_id(quarantined)["status"] == "superseded"
    user_item = next(i for i in fake.all_items() if i["source"] == "user")
    assert user_item["status"] == "accepted"
    assert "quarantined" not in user_item


def test_quarantine_never_fires_for_user_source(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    # Drive the subject past the threshold, then propose USER-sourced via the
    # SAME log entry point: user classifies supersede, never quarantine.
    last_active, _ = _drive_to_quarantine(fake)

    user_id = _log_jp(to_field="g.id", source="user")
    assert user_id is not None
    user_item = fake.by_id(user_id)
    assert "quarantined" not in user_item
    # User supersedes the active rows instead of quarantining.
    assert fake.by_id(last_active)["status"] == "superseded"


# --------------------------------------------------------------------------- #
# D1 — supersede cooldown
# --------------------------------------------------------------------------- #
def test_supersede_inside_cooldown_is_noop(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    _log_jp(to_field="a.id")
    second = _log_jp(to_field="b.id")   # first supersede — proceeds (no prior)
    _backdate_supersedes(fake, hours=1)  # last supersede 1h ago (< 24h)

    third = _log_jp(to_field="c.id")     # would-be supersede inside the window
    assert third == second               # dup-style no-op: existing id returned
    assert len(fake) == 2                # no new row
    assert fake.by_id(second)["status"] == "pending"  # nothing superseded


def test_supersede_after_cooldown_proceeds(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    _log_jp(to_field="a.id")
    second = _log_jp(to_field="b.id")
    _backdate_supersedes(fake, hours=25)  # last supersede 25h ago (> 24h)

    third = _log_jp(to_field="c.id")
    assert third != second
    assert fake.by_id(second)["status"] == "superseded"
    assert fake.by_id(third)["status"] == "pending"


def test_first_supersede_on_a_subject_is_never_cooled(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log_jp(to_field="a.id")
    second = _log_jp(to_field="b.id")   # immediate — but no PRIOR supersede
    assert second != first
    assert fake.by_id(first)["status"] == "superseded"


def test_cooldown_never_fires_for_user_source(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    _log_jp(to_field="a.id")
    second = _log_jp(to_field="b.id")
    _backdate_supersedes(fake, hours=1)  # inside the window

    user_id = _log_jp(to_field="c.id", source="user")  # user wins regardless
    assert user_id not in (None, second)
    assert fake.by_id(second)["status"] == "superseded"


# --------------------------------------------------------------------------- #
# D2 + auto-accept: quarantined rows are frozen until a HUMAN accept/reject
# --------------------------------------------------------------------------- #
def test_quarantined_row_is_never_auto_accepted_at_ingestion(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    accepts: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepts.append(mid) or {"status": "accepted"})

    # value_mapping (an _AUTO_ACCEPT_TYPES member) driven to quarantine.
    def _log_vm(to_value: str) -> str | None:
        return self_learning.log_proposed_mapping(
            "erp", "orders.customer_id", "c-100",
            "crm", "accounts.id", to_value,
            "customer", 0.95, "ctx",
        )

    _log_vm("t1")
    _log_vm("t2")
    _backdate_supersedes(fake, hours=25)
    _log_vm("t3")
    _backdate_supersedes(fake, hours=25)
    _log_vm("t4")
    _backdate_supersedes(fake, hours=25)
    accepts.clear()                       # ignore pre-quarantine auto-accepts
    quarantined = _log_vm("t5")           # threshold reached → quarantine

    assert fake.by_id(quarantined)["quarantined"] == "true"
    assert accepts == []                  # the quarantined row was never accepted


def test_sweep_auto_accept_skips_quarantined_rows(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    import agents.proposal_scoring as proposal_scoring
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda item: {"ai_confidence": "high"})
    accepts: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepts.append(mid) or {"status": "accepted"})

    fake.put_item(Item={
        "mapping_id": "m-quar", "discovered_at": "2026-07-14T00:00:00+00:00",
        "status": "pending", "proposed_type": "value_mapping",
        "quarantined": "true", "review_reason": "flip-flopped 3 times — adjudicate",
    })
    result = self_learning.sweep_auto_accept()
    assert result == {"accepted": 0, "failed": 0}
    assert accepts == []


# --------------------------------------------------------------------------- #
# R1 (D4) — ambiguous system reference carries a stated review reason
# --------------------------------------------------------------------------- #
def test_ambiguous_system_reference_review_reason(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: DEMO_SYSTEMS)

    mid = self_learning.log_proposed_mapping(
        "MES", "work_orders.line", "LINE1",
        "erp", "orders.line_code", "DET-L1",
        "production-line", 0.9, "ctx",
    )
    assert mid is not None  # ambiguous is a legitimate passthrough — stored
    item = fake.by_id(mid)
    assert item["review_reason"] == (
        "System reference 'MES' is ambiguous across mes-cr, mes-det, mes-mil "
        "— confirm which system before accepting"
    )
    # from_system stays the raw passthrough (never guess).
    assert item["from_system"] == "MES"
    assert item["to_system"] == "erp"  # unique type match still normalizes


def test_unambiguous_references_store_no_review_reason(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: DEMO_SYSTEMS)

    mid = self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "plm", "items.customer_ref", "P-556",
        "customer", 0.9, "ctx",
    )
    assert mid is not None
    item = fake.by_id(mid)
    assert "review_reason" not in item   # stored only when non-empty
    assert "quarantined" not in item
