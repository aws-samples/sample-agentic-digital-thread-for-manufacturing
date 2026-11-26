# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0077 D2 — a prior accepted row never blinds drift re-detection (offline).

An exact-target ACCEPTED row classifies ``noop`` (ADR-0003 C), so before
ADR-0077 an acknowledge-only accept of a ``column_added`` made the gap
permanently invisible. ``run_drift_check`` now marks additive drift whose
subject was ever accepted with ``REDETECTED_DESCRIPTION``; the UNCHANGED
lifecycle supersedes the old accepted row (stamps survive) and files a fresh
pending row. Doubles: ``FakeTable``/``NeptuneSpy`` from the lifecycle suite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import agents.schema_drift as schema_drift
import agents.self_learning as self_learning
from agents.proposal_key import canonical_keys

try:
    from unit.test_schema_drift_proposals import _install, _stub_discover
    from unit.test_self_learning_lifecycle import FakeTable
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_schema_drift_proposals import _install, _stub_discover
    from test_self_learning_lifecycle import FakeTable

_WITH_PRIORITY = [("id", "integer"), ("status", "text"), ("priority", "text")]
_MARK = schema_drift.REDETECTED_DESCRIPTION


def _seed(fake: FakeTable, *, status: str, reason: str = "column_added",
          from_field: str = "work_orders.priority", accepted: bool = True) -> dict:
    """A pre-ADR schema_drift row with its canonical keys (empty description)."""
    keys = canonical_keys("schema_drift", from_system="erp", from_field=from_field,
                          reason=reason, description="")
    when = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    item = {"mapping_id": str(uuid.uuid4()), "discovered_at": when,
            "status": status, "proposed_type": "schema_drift", "source": "agent",
            "method": "schema-drift", "reason": reason, "from_system": "erp",
            "to_system": "erp", "from_field": from_field, "from_value": "",
            "to_field": "", "to_value": "", "concept": "", "version": "1",
            "subject_key": keys.subject_key, "target_key": keys.target_key}
    if accepted:
        item.update(accepted_at=when, accepted_by="steward@example.com")
    if status == "rejected":
        item.update(rejected_at=when)
    fake.put_item(Item=item)
    return item


def _marked_key(from_field="work_orders.priority", reason="column_added") -> str:
    return canonical_keys("schema_drift", from_system="erp", from_field=from_field,
                          reason=reason, description=_MARK).target_key


def test_prior_accept_is_superseded_and_refiled(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    old = _seed(fake, status="accepted")

    result = schema_drift.run_drift_check("erp")

    assert result["proposals_filed"] == 1
    assert result["refiled_after_accept"] == 1
    prior = fake.by_id(old["mapping_id"])
    assert prior["status"] == "superseded"
    assert prior["accepted_at"] and prior["accepted_by"]  # stamps survive
    pending = [r for r in fake.all_items() if r["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["to_description"] == _MARK
    assert pending[0]["target_key"] == _marked_key()


def test_second_run_is_stable_without_a_store_lookup(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="accepted")
    schema_drift.run_drift_check("erp")
    rows_before = len(fake)
    lookups: list = []
    monkeypatch.setattr(schema_drift, "_accept_generation",
                        lambda *a: lookups.append(a) or 1)

    second = schema_drift.run_drift_check("erp")

    assert second["proposals_filed"] == 0  # dup_pending — no churn
    assert second["refiled_after_accept"] == 1
    assert second["resolved_swept"] == 0   # the marked row is current
    assert len(fake) == rows_before
    assert lookups == []  # the marked key is already pending


def test_no_prior_accept_files_unmarked(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)

    result = schema_drift.run_drift_check("erp")

    assert result["refiled_after_accept"] == 0
    (row,) = fake.all_items()
    assert row.get("to_description", "") == ""
    assert row["target_key"] == canonical_keys(
        "schema_drift", from_system="erp", from_field="work_orders.priority",
        reason="column_added", description="").target_key


def test_rejected_only_history_is_not_a_prior_accept(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="rejected", accepted=False)

    result = schema_drift.run_drift_check("erp")

    assert result["refiled_after_accept"] == 0
    pending = [r for r in fake.all_items() if r["status"] == "pending"]
    assert [r.get("to_description", "") for r in pending] == [""]


def test_removal_with_prior_accept_is_never_marked(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer")])  # status removed
    old = _seed(fake, status="accepted", reason="column_removed",
                from_field="work_orders.status")

    result = schema_drift.run_drift_check("erp")

    assert result["refiled_after_accept"] == 0
    assert fake.by_id(old["mapping_id"])["status"] == "accepted"  # noop, untouched
    assert [r for r in fake.all_items() if r["status"] == "pending"] == []


def test_mark_returns_new_objects_and_leaves_inputs_alone(monkeypatch):
    monkeypatch.setattr(schema_drift, "_accept_generation", lambda *a: 1)
    drifts = [{"reason": "column_added", "from_field": "work_orders.priority",
               "description": ""},
              {"reason": "column_retyped", "from_field": "work_orders.status",
               "description": "was text, now uuid"}]
    out = schema_drift._mark_unmaterialised("erp", drifts, [])
    assert out[0] is not drifts[0] and out[1] is not drifts[1]
    assert out[0]["description"] == _MARK
    assert out[1] == drifts[1]
    assert drifts[0]["description"] == ""  # input untouched


def test_prior_accept_predicate_uses_accepted_at(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    _seed(fake, status="superseded")  # was accepted once, then displaced
    assert schema_drift._accept_generation("erp", "work_orders.priority", "column_added") == 1
    assert schema_drift._accept_generation("erp", "work_orders.other", "column_added") == 0
    # A different reason on the same subject is not this addition's lineage.
    assert schema_drift._accept_generation("erp", "work_orders.priority", "table_added") == 0


def test_generation_marker_round_trips():
    assert schema_drift.redetected_description(1) == _MARK
    assert schema_drift.redetected_description(0) == _MARK
    assert schema_drift.redetected_description(3) == f"{_MARK} (generation 3)"
    for n in (1, 2, 7):
        assert schema_drift.redetected_generation(schema_drift.redetected_description(n)) == n
    for text in ("", None, "was text, now uuid", f"{_MARK} (generation x)",
                 f"prefix {_MARK}", f"{_MARK} (generation 2) tail"):
        assert schema_drift.redetected_generation(text) == 0


def _steward_accept(fake: FakeTable, row: dict, *, days_ago: int = 0) -> None:
    """Emulate the steward's accept stamps on a stored row (the lifecycle's
    accept writes exactly these); also backdate any supersede stamp past the
    ADR-0042 D1 one-day cooldown so the next re-detection is not rate-limited."""
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    stored = fake.by_id(row["mapping_id"])
    stored.update(status="accepted", accepted_at=when, accepted_by="steward@example.com")
    fake.put_item(Item=stored)
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    for item in fake.all_items():
        if item.get("status") == "superseded":
            fake.put_item(Item={**item, "superseded_at": stale})


def test_accepted_marked_refile_never_blinds_the_next_redetection(monkeypatch):
    """plain accepted → marked (gen 1) accepted/materialised → the Field
    disappears again → a FRESH pending gen-2 proposal (never an exact-accepted
    ``noop``); a further run is stable (dup_pending)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="accepted")
    schema_drift.run_drift_check("erp")
    (gen1,) = [r for r in fake.all_items() if r["status"] == "pending"]
    assert gen1["to_description"] == _MARK
    _steward_accept(fake, gen1)

    again = schema_drift.run_drift_check("erp")   # Field still absent from the graph

    assert again["proposals_filed"] == 1 and again["refiled_after_accept"] == 1
    (gen2,) = [r for r in fake.all_items() if r["status"] == "pending"]
    assert gen2["to_description"] == f"{_MARK} (generation 2)"
    assert gen2["target_key"] != gen1["target_key"]
    assert fake.by_id(gen1["mapping_id"])["status"] == "superseded"
    assert fake.by_id(gen1["mapping_id"])["accepted_at"]  # stamps survive
    rows_before = len(fake)
    third = schema_drift.run_drift_check("erp")
    assert third["proposals_filed"] == 0 and len(fake) == rows_before
    _steward_accept(fake, gen2)
    schema_drift.run_drift_check("erp")
    (gen3,) = [r for r in fake.all_items() if r["status"] == "pending"]
    assert gen3["to_description"] == f"{_MARK} (generation 3)"


def _pending_marked(fake: FakeTable) -> list:
    import agents.schema_repair as sr
    return sr._marked_rows("erp", [r for r in fake.all_items() if r["status"] == "pending"])


def test_accepted_removal_never_authorises_a_later_addition(monkeypatch):
    """column_removed accepted → column_added re-appears: UNMARKED pending
    (a fresh steward review), never a repair-schema candidate."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="accepted", reason="column_removed")

    result = schema_drift.run_drift_check("erp")

    assert result["refiled_after_accept"] == 0
    (row,) = [r for r in fake.all_items() if r["status"] == "pending"]
    assert row["reason"] == "column_added" and row.get("to_description", "") == ""
    assert _pending_marked(fake) == []


def test_accepted_retype_never_authorises_a_later_addition(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="accepted", reason="column_retyped")
    assert schema_drift.run_drift_check("erp")["refiled_after_accept"] == 0
    assert _pending_marked(fake) == []


def test_latest_accept_decides_the_lineage(monkeypatch):
    """An older accepted addition followed by a NEWER accepted removal: the
    removal is the latest steward decision, so the re-appearing column is
    reviewed afresh (unmarked)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, _WITH_PRIORITY)
    _seed(fake, status="superseded")
    removal = _seed(fake, status="accepted", reason="column_removed")
    fake.put_item(Item={**fake.by_id(removal["mapping_id"]), "accepted_at": (
        datetime.now(timezone.utc) - timedelta(days=10)).isoformat()})
    assert schema_drift.run_drift_check("erp")["refiled_after_accept"] == 0
    assert _pending_marked(fake) == []


def test_accepted_table_removal_never_authorises_a_table_addition(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"tables": [
        {"name": "work_orders", "columns": [{"name": "id", "data_type": "integer"},
                                            {"name": "status", "data_type": "text"}]},
        {"name": "segment_events", "columns": [{"name": "id", "data_type": "integer"}]}]})
    _seed(fake, status="accepted", reason="table_removed", from_field="segment_events.")

    result = schema_drift.run_drift_check("erp")

    assert result["kinds"] == {"table_added": 1} and result["refiled_after_accept"] == 0
    (row,) = [r for r in fake.all_items() if r["status"] == "pending"]
    assert row["reason"] == "table_added" and row.get("to_description", "") == ""
    assert _pending_marked(fake) == []


def test_accepted_table_addition_is_refiled_marked(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(schema_drift, "_discover_fresh", lambda sid: {"tables": [
        {"name": "work_orders", "columns": [{"name": "id", "data_type": "integer"},
                                            {"name": "status", "data_type": "text"}]},
        {"name": "segment_events", "columns": [{"name": "id", "data_type": "integer"}]}]})
    _seed(fake, status="accepted", reason="table_added", from_field="segment_events.")
    assert schema_drift.run_drift_check("erp")["refiled_after_accept"] == 1
    assert [r["from_field"] for r in _pending_marked(fake)] == ["segment_events."]
