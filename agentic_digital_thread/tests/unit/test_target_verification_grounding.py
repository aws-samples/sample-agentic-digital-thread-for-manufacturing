# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0063 D1/D2: generalized target verification, file-time grounding, and the
review trust signals. Offline — no AWS, no Neptune, no network.

The ADR-0051 P4 target check fired ONLY for UUID-shaped targets. A steward pass
over 101 pending cross-system proposals measured that the dominant false class
was an unverified NAME target (a value that does not exist in the target system
at all), so the guard never ran on any of them. These tests pin the generalized
seam:

  - a NAME target verifies when it exists in the TO system's projection, and
    fails CLOSED when it does not;
  - a name that exists only in the FROM system's projection does NOT verify;
  - a passing check stamps ``verified``/``verified_at`` (+ the honest
    ``verified_scope``) — no surface may claim confirmation without the stamp;
  - unknown stays UNKNOWN: an unreachable graph or a never-onboarded TO system
    leaves the file-time state ABSENT, never a fabricated ``false``;
  - filing NEVER depends on verification (ADR-0003 F swallow posture);
  - the review signals reuse ``_auto_accept_block_reason`` — the ONE policy.

Engine-neutral by contract: the system ids here are ``sys-a``/``sys-b`` and the
values are generic placeholders — no dataset literals (ADR-0044).
"""
from __future__ import annotations

import agents.self_learning as self_learning

from .test_self_learning_lifecycle import FakeTable, NeptuneSpy

_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# --------------------------------------------------------------------------- #
# Graph doubles
# --------------------------------------------------------------------------- #
def _reader(target_rows: list[dict], *, onboarded: tuple = ("sys-b",),
            raises: bool = False):
    """Fake ``_neptune_query``: answers the target probe with ``target_rows``
    and the TO-system presence probe from ``onboarded``."""

    def _read(cypher: str, parameters: dict | None = None):
        if raises:
            raise RuntimeError("graph unavailable")
        if "count(s)" in cypher:
            sid = str((parameters or {}).get("sid") or "")
            return [{"n": 1 if sid in onboarded else 0}]
        return [dict(r) for r in target_rows]

    return _read


def _item(**over) -> dict:
    base = {
        "mapping_id": "m-1", "discovered_at": "2026-08-18T00:00:00+00:00",
        "proposed_type": "value_mapping", "from_system": "sys-a",
        "from_field": "table_a.col_a", "from_value": "",
        "to_system": "sys-b", "to_field": "table_b.col_b",
        "to_value": "target-name-1", "concept": "asset", "confidence": "0.9",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# D1 — the generalized verdict (name targets included)
# --------------------------------------------------------------------------- #
def test_name_target_verifies_against_the_to_system_projection(monkeypatch):
    """The generalization itself: a NAME target that EXISTS in the TO system's
    projection verifies, and the proof is recorded as system-scoped."""
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "target-name-1",
                                  "system": "sys-b"}]))
    assert self_learning._target_verification(_item()) == {
        "verified": True, "scope": "system"}


def test_nonexistent_name_target_fails_closed(monkeypatch):
    """Kills accept-on-doubt: nothing matched in a reachable graph whose TO
    system IS onboarded ⇒ a determinate unverified (never True)."""
    monkeypatch.setattr(self_learning, "_neptune_query", _reader([]))
    state = self_learning._target_verification(_item())
    assert state == {"verified": False, "scope": None}
    assert self_learning._verify_target(_item()) is False


def test_name_only_in_the_from_system_does_not_verify(monkeypatch):
    """THE measured false class: the value exists — but only in the FROM
    system's own naming. A cross-system claim must not be satisfied by the
    from-side's projection."""
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "target-name-1",
                                  "system": "sys-a"}]))
    assert self_learning._target_verification(_item()) == {
        "verified": False, "scope": None}
    assert self_learning._verify_target(_item()) is False


def test_class_without_system_provenance_is_unknown_unscoped(monkeypatch):
    """A provenance-free match cannot prove the TO-system projection. It is
    retained only as an honest unscoped/unknown observation and grants no
    verification authority."""
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "target-name-1"}]))
    assert self_learning._target_verification(_item()) == {
        "verified": None, "scope": "unscoped"}
    assert self_learning._verify_target(_item()) is False


def test_name_contradicting_the_source_value_does_not_verify(monkeypatch):
    """The ADR-0051 P4 containment test survives generalization."""
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": _UUID, "name": "unrelated-node",
                                  "system": "sys-b"}]))
    assert self_learning._target_verification(
        _item(to_value=_UUID, from_value="source-value-1")) == {
            "verified": False, "scope": None}


def test_unreachable_graph_is_unknown_not_false(monkeypatch):
    """A raising read seam is doubt, not evidence: ``None`` (unknown) — and the
    gate still fails closed."""
    monkeypatch.setattr(self_learning, "_neptune_query", _reader([], raises=True))
    assert self_learning._target_verification(_item())["verified"] is None
    assert self_learning._verify_target(_item()) is False


def test_never_onboarded_to_system_is_unknown_not_false(monkeypatch):
    """A name absent from a system nobody ever onboarded means "nobody could
    look", not "the claim is false" (honest absence)."""
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([], onboarded=()))
    assert self_learning._target_verification(_item())["verified"] is None


def test_passing_check_stamps_verified_and_its_scope(monkeypatch):
    """No text may claim confirmation without the stamp — so the stamp must be
    written on the passing path, carrying the HONEST scope."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "target-name-1",
                                  "system": "sys-b"}]))
    item = _item()
    table.put_item({**item, "status": "pending"})
    assert self_learning._verify_target(item) is True
    stored = table.by_id("m-1")
    assert stored["verified"] == "true"
    assert stored["verified_at"]
    assert stored["verified_scope"] == "system"


def test_failing_check_never_rejects_or_deletes(monkeypatch):
    """Verification gates auto-accept and informs review — it is NOT a verdict:
    the row stays pending, present, and unstamped."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_query", _reader([]))
    table.put_item({**_item(), "status": "pending"})
    assert self_learning._verify_target(_item()) is False
    stored = table.by_id("m-1")
    assert stored["status"] == "pending"
    assert "verified" not in stored and len(table) == 1


# --------------------------------------------------------------------------- #
# D1 — grounding at FILE time
# --------------------------------------------------------------------------- #
def _file(monkeypatch, table: FakeTable, **over) -> "str | None":
    """File one proposal through the REAL ``log_proposed_mapping``."""
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(self_learning, "_neptune_write", NeptuneSpy())
    kwargs = {
        "from_system": "sys-a", "from_field": "table_a.col_a",
        "from_value": "source-value-1", "to_system": "sys-b",
        "to_field": "table_b.col_b", "to_value": "target-name-1",
        "concept": "asset", "confidence": 0.9,
        "query_context": "cross-system lookup", "proposed_type": "value_mapping",
    }
    kwargs.update(over)
    return self_learning.log_proposed_mapping(**kwargs)


def test_file_time_grounding_records_a_passing_cross_system_target(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "source-value-1",
                                  "system": "sys-b"}]))
    mid = _file(monkeypatch, table, to_value="source-value-1")
    assert table.by_id(mid)["target_verified"] == "true"
    assert table.by_id(mid)["verified_scope"] == "system"


def test_file_time_grounding_records_a_refuted_cross_system_target(monkeypatch):
    """The point of D1: the steward never sees a row whose grounding is simply
    unknown-because-nobody-looked."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_neptune_query", _reader([]))
    mid = _file(monkeypatch, table)
    stored = table.by_id(mid)
    assert stored["target_verified"] == "false"
    assert "verified" not in stored  # a REFUTED row is never stamped verified


def test_file_time_grounding_stays_absent_when_the_read_fails(monkeypatch):
    """Never a fabricated false: an unrunnable probe leaves the key ABSENT."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_neptune_query", _reader([], raises=True))
    mid = _file(monkeypatch, table)
    assert "target_verified" not in table.by_id(mid)


def test_same_system_row_carries_no_grounding_claim(monkeypatch):
    """Nothing to ground: a same-system pairing has no target projection."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _reader([{"id": "", "name": "source-value-1"}]))
    mid = _file(monkeypatch, table, to_system="sys-a", to_field="table_a.col_z")
    assert mid is not None
    assert "target_verified" not in table.by_id(mid)


def test_filing_survives_a_verification_that_raises(monkeypatch):
    """ADR-0003 F: verification is best-effort — a raising probe must never
    prevent (or fail) the filing."""
    table = FakeTable()

    def _boom(item):
        raise RuntimeError("verification exploded")

    monkeypatch.setattr(self_learning, "_target_verification", _boom)
    mid = _file(monkeypatch, table)
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"
    assert "target_verified" not in table.by_id(mid)


# --------------------------------------------------------------------------- #
# D2 — surfaced trust signals (display of the EXISTING policy)
# --------------------------------------------------------------------------- #
def test_hierarchy_position_still_blocks_auto_accept_and_says_why():
    """The P4 policy is unchanged AND now legible: the same single source of
    truth answers the accept gate and the review surface, with no graph read."""
    item = _item(method="hierarchy_position", confidence="0.95")
    assert "hierarchy_position" in self_learning._auto_accept_block_reason(
        item, verify=False)
    signals = self_learning.review_trust_signals(item)
    assert "hierarchy_position" in signals["auto_accept_block"]
    assert "target_verified" not in signals  # never checked ⇒ ABSENT


def test_trust_signals_absent_when_grounding_was_never_recorded():
    assert self_learning.review_trust_signals(_item()) == {}


def test_trust_signals_report_a_recorded_verdict_both_ways():
    assert self_learning.review_trust_signals(
        _item(target_verified="true", verified_scope="system"))["target_verified"] is True
    refuted = self_learning.review_trust_signals(_item(target_verified="false"))
    assert refuted["target_verified"] is False
    assert refuted["auto_accept_block"].startswith("name target not verified")


def test_recorded_verified_stamp_satisfies_the_display_signal():
    """The accept-gate stamp and the file-time state carry ONE meaning."""
    assert self_learning.review_trust_signals(
        _item(verified="true", verified_scope="system"))["target_verified"] is True


def test_display_mode_performs_no_graph_read(monkeypatch):
    """``verify=False`` is read-free by contract — a raising seam proves it."""
    def _boom(cypher, parameters=None):
        raise AssertionError("display mode must not read the graph")

    monkeypatch.setattr(self_learning, "_neptune_query", _boom)
    assert self_learning._auto_accept_block_reason(_item(), verify=False) == ""


def test_pending_read_paths_attach_the_signals(monkeypatch):
    """Both review read paths carry the signals, additively."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    table.put_item({**_item(), "status": "pending", "target_verified": "true",
                    "verified_scope": "system"})
    table.put_item({**_item(mapping_id="m-2", method="hierarchy_position"),
                    "status": "pending"})
    pending = {r["mapping_id"]: r for r in self_learning.get_pending_mappings()}
    assert pending["m-1"]["target_verified"] is True
    assert "hierarchy_position" in pending["m-2"]["auto_accept_block"]
    listed = {r["mapping_id"]: r for r in self_learning.get_all_mappings()}
    assert listed["m-1"]["target_verified"] is True
    assert "hierarchy_position" in listed["m-2"]["auto_accept_block"]


def test_decided_rows_carry_no_auto_accept_block(monkeypatch):
    """Auto-accept eligibility is meaningless once a steward has decided."""
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    table.put_item({**_item(method="hierarchy_position"), "status": "accepted",
                    "verified": "true", "verified_scope": "system"})
    row = self_learning.get_all_mappings()[0]
    assert "auto_accept_block" not in row
    assert row["target_verified"] is True


def test_legacy_verified_without_system_scope_is_non_authoritative():
    """Legacy rows predate verified_scope; absence cannot be upgraded to TO-system proof."""
    legacy = _item(verified="true", target_verified="true")
    assert self_learning._recorded_target_verified(legacy) is None
    assert self_learning.review_trust_signals(legacy) == {}


def test_only_system_scoped_stamp_can_surface_target_checked_authority():
    scoped = _item(verified="true", verified_scope="system")
    assert self_learning.review_trust_signals(scoped)["target_verified"] is True
    unscoped = _item(verified="true", verified_scope="unscoped")
    assert "target_verified" not in self_learning.review_trust_signals(unscoped)


def test_unscoped_or_legacy_uuid_never_receives_confirmed_score():
    from agents.proposal_scoring import score_proposal

    for row in (_item(to_value=_UUID, confidence="0.95", verified="true"),
                _item(to_value=_UUID, confidence="0.95", verified="true",
                      verified_scope="unscoped")):
        scored = score_proposal(row)
        assert scored["ai_confidence"] != "high"
        assert "confirmed" not in scored["ai_recommendation"].lower()
    confirmed = score_proposal(_item(
        to_value=_UUID, confidence="0.95", verified="true",
        verified_scope="system"))
    assert confirmed["ai_confidence"] == "high"
    assert "confirmed" in confirmed["ai_recommendation"].lower()
