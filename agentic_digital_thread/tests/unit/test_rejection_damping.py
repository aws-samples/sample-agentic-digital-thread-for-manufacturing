# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Rejection damping — consuming ``prior_rejections`` (ADR-0042 D3, offline).

Behavioral proof of the D3 branch in ``self_learning._classify_collision``
against the in-memory ``FakeTable`` double shared with
``test_self_learning_lifecycle.py``. No AWS, no Neptune, no network.

Pinned contract (ADR-0042 D3):
  - ``reject_mapping`` stamps ``rejected_at`` so damping/reason dates are
    queryable;
  - an exact agent re-proposal whose subject has ``>= REJECTION_DAMP_MIN_COUNT``
    (2) rejections with the most recent inside
    ``REJECTION_REPROPOSAL_COOLDOWN_DAYS`` (7d) is DROPPED (returns ``None``,
    nothing stored);
  - outside those conditions it inserts, with the history made consumable:
    ``prior_rejections`` is a list of ``{"mapping_id", "rejected_at"}`` dicts
    (additive shape change, new rows only) and the row carries a
    ``review_reason`` — inside the 90d tombstone TTL the PINNED ADR-0053 E6
    wording naming the rejection; for undated legacy rejections the R3
    last-rejected-date text;
  - a single rejection never damps (count threshold);
  - the damped path never fires for ``source="user"`` (user always wins).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import agents.self_learning as self_learning

import pytest


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


def _install(monkeypatch, table: FakeTable) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", NeptuneSpy())
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)


def _log(*, source: str = "agent") -> str | None:
    """The SAME exact-target value_mapping every time (one subject+target)."""
    return self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", "acct-7",
        "customer", 0.9, "ctx", source=source,
    )


def _backdate_rejections(fake: FakeTable, *, days: float) -> str:
    """Rewrite every rejected row's rejected_at to ``days`` ago; returns the stamp."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for item in fake._items.values():
        if item.get("status") == "rejected":
            item["rejected_at"] = ts
    return ts


def _two_rejections(fake: FakeTable) -> tuple[str, str]:
    """Propose → reject → re-propose → reject. Returns the two rejected ids."""
    first = _log()
    self_learning.reject_mapping(first)
    second = _log()               # single rejection → still inserts (history)
    self_learning.reject_mapping(second)
    return first, second


# --------------------------------------------------------------------------- #
# reject_mapping stamps rejected_at
# --------------------------------------------------------------------------- #
def test_reject_mapping_stamps_rejected_at(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    mid = _log()
    result = self_learning.reject_mapping(mid)
    assert result == {"status": "rejected"}
    item = fake.by_id(mid)
    assert item["status"] == "rejected"
    # Parseable ISO timestamp, roughly now.
    stamped = datetime.fromisoformat(item["rejected_at"])
    assert (datetime.now(timezone.utc) - stamped) < timedelta(minutes=1)


# --------------------------------------------------------------------------- #
# D3 — drop inside the window
# --------------------------------------------------------------------------- #
def test_two_recent_rejections_drop_the_reproposal(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _two_rejections(fake)

    third = _log()                # 2 rejections, latest just now → damped
    assert third is None
    assert len(fake) == 2         # nothing stored


def test_two_rejections_latest_one_day_ago_still_drops(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _two_rejections(fake)
    _backdate_rejections(fake, days=1)   # latest 1 day ago — inside 7d

    assert _log() is None
    assert len(fake) == 2


# --------------------------------------------------------------------------- #
# D3 — outside the window: insert with dict-shaped history + review reason
# --------------------------------------------------------------------------- #
def test_old_rejections_insert_with_dict_history_and_reason(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    first, second = _two_rejections(fake)
    backdated = _backdate_rejections(fake, days=10)  # latest 10d ago — outside 7d

    third = _log()
    assert third is not None
    item = fake.by_id(third)
    assert item["status"] == "pending"

    # prior_rejections is dict-shaped: {"mapping_id", "rejected_at"} per row.
    history = item["prior_rejections"]
    assert {h["mapping_id"] for h in history} == {first, second}
    for h in history:
        assert set(h.keys()) == {"mapping_id", "rejected_at"}
        assert h["rejected_at"] == backdated

    # ADR-0053 E6: 10d is outside D3's 7d damping window but inside the 90d
    # tombstone TTL — the re-proposal inserts FLAGGED with the pinned
    # tombstone review reason naming the rejection date + a rejected row's id.
    assert item["review_reason"].startswith(
        f"re-proposal of a shape the steward rejected on {backdated[:10]} "
        f"(mapping ")
    assert item["review_reason"].rstrip(")").rsplit(" ", 1)[-1] in {first, second}


def test_single_rejection_never_damps_even_when_recent(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)   # ONE rejection, just now

    second = _log()                        # below REJECTION_DAMP_MIN_COUNT
    assert second is not None
    item = fake.by_id(second)
    assert item["status"] == "pending"
    assert [h["mapping_id"] for h in item["prior_rejections"]] == [first]


def test_legacy_rejections_without_rejected_at_never_damp(monkeypatch):
    """Rows rejected before the rejected_at stamp existed carry no rejection
    date — recency cannot be established, so damping conservatively does NOT
    fire and the re-proposal inserts with its history surfaced."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    _two_rejections(fake)
    # Strip the stamps (legacy shape).
    for item in fake._items.values():
        item.pop("rejected_at", None)

    third = _log()
    assert third is not None
    item = fake.by_id(third)
    assert len(item["prior_rejections"]) == 2
    assert all(h["rejected_at"] == "" for h in item["prior_rejections"])
    assert "unknown date" in item["review_reason"]


# --------------------------------------------------------------------------- #
# Source-awareness: user always wins
# --------------------------------------------------------------------------- #
def test_damping_never_fires_for_user_source(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _two_rejections(fake)                  # latest just now — would damp an agent

    user_id = _log(source="user")
    assert user_id is not None             # stored, not damped
    item = fake.by_id(user_id)
    assert item["source"] == "user"
    assert "prior_rejections" not in item  # user path is the supersede branch
