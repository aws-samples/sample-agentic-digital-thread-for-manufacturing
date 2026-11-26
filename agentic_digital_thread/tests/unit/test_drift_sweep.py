# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Machine-legal drift sweep — offline.

``schema_drift.sweep_resolved_drift`` flips pending, agent-sourced,
non-quarantined ``schema_drift`` rows whose stored ``target_key`` is no
longer in the current drift set to ``superseded`` (ADR-0052
replace-not-accrete; ADR-0053: the machine NEVER writes the steward-only
tombstone status). Behavioral proof against the in-memory ``FakeTable`` /
``NeptuneSpy`` doubles shared with ``test_self_learning_lifecycle.py`` —
no AWS, no Neptune, no network.

The five acceptance pins:

1. resolved drift swept — a re-check whose diff drops one of two pending
   drifts supersedes the resolved row (``superseded_reason="drift-resolved"``)
   and the pinned result entry carries ``resolved_swept``;
2. the machine never writes the steward tombstone status — every sweep write
   is ``superseded`` (spy on ``_update_status``) and the module source
   contains no such write (grep-level);
3. steward rows survive — an ``accepted`` (acknowledged) row, a
   ``source="user"`` row, and a quarantined row are untouched;
4. provision hook — a successful ``_run_provision_job`` invokes the sweep
   with ``current_drifts=[]``; a RAISING sweep leaves the job ``done``;
5. re-detection unaffected — an unresolved drift stays pending (and still
   dedupes ``dup_pending``); target-grain matching keeps an unresolved
   type-flap row pending (subject-grain would sweep it as a sibling).

Plus the TOCTOU pin: each supersede is conditional on the row still being
pending (``expect_status`` — the ADR-0043 subsume idiom), so a steward
decision landing between read and write wins — skipped, uncounted, no raise.

Placeholder ids only (no demo literals).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import agents.onboard as onboard
import agents.schema_check as schema_check
import agents.schema_drift as schema_drift
import agents.self_learning as self_learning
from agents.job_store import JobStore
from agents.models import ProvisionRequest
from agents.proposal_key import canonical_keys

try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy

_SYSTEM = "sys-a"

_BASELINE = {
    "records": {
        "columns": ["id", "label"],
        "column_types": {"id": "integer", "label": "text"},
        "concept_map": {}, "relation_kind": "base_table",
    },
}


def _install(monkeypatch, table: FakeTable) -> NeptuneSpy:
    spy = NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", spy)
    monkeypatch.setattr(self_learning, "known_systems",
                        lambda: [{"id": _SYSTEM, "name": "System A"}])
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: dict(_BASELINE) if sid == _SYSTEM else None)
    return spy


def _fresh(columns) -> dict:
    return {"tables": [{"name": "records",
                        "columns": [{"name": n, "data_type": t}
                                    for n, t in columns]}]}


def _stub_discover(monkeypatch, columns) -> None:
    monkeypatch.setattr(schema_drift, "_discover_fresh",
                        lambda sid: _fresh(columns))


def _seed_drift_row(fake: FakeTable, *, status="pending", source="agent",
                    quarantined="", from_field="records.col",
                    reason="column_added", description="") -> dict:
    """Directly seed a schema_drift proposal row carrying its canonical keys
    exactly as ``self_learning._build_item`` stores them (``subject_key`` +
    ``target_key`` attributes on every item)."""
    keys = canonical_keys("schema_drift", from_system=_SYSTEM,
                          from_field=from_field, reason=reason,
                          description=description)
    item = {
        "mapping_id": str(uuid.uuid4()),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "status": status, "proposed_type": "schema_drift",
        "from_system": _SYSTEM, "to_system": _SYSTEM,
        "from_field": from_field, "from_value": "", "to_field": "",
        "to_value": "", "concept": "", "source": source,
        "subject_key": keys.subject_key, "target_key": keys.target_key,
    }
    if quarantined:
        item["quarantined"] = quarantined
    fake.put_item(Item=item)
    return item


# ── pin 1: resolved drift swept, resolved_swept counted ─────────────────────

def test_resolved_drift_swept_with_count(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    # Two added columns -> two pending drift rows.
    _stub_discover(monkeypatch, [("id", "integer"), ("label", "text"),
                                 ("col_x", "text"), ("col_y", "text")])
    first = schema_drift.run_drift_check(_SYSTEM)
    assert first["proposals_filed"] == 2
    assert first["resolved_swept"] == 0  # both drifts still current

    # col_y disappeared from the diff — its row is resolved and swept.
    _stub_discover(monkeypatch, [("id", "integer"), ("label", "text"),
                                 ("col_x", "text")])
    second = schema_drift.run_drift_check(_SYSTEM)

    assert second == {"system_id": _SYSTEM, "status": "ok",
                      "drifts_found": 1, "proposals_filed": 0, "proposal_noops": 1,
                      "kinds": {"column_added": 1}, "resolved_swept": 1, "refiled_after_accept": 0}
    by_field = {r["from_field"]: r for r in fake.all_items()}
    swept = by_field["records.col_y"]
    assert swept["status"] == "superseded"
    assert swept["superseded_reason"] == "drift-resolved"
    assert swept["superseded_at"]  # stamped
    assert by_field["records.col_x"]["status"] == "pending"  # unresolved


# ── pin 2: machine never writes the steward tombstone status ─────────────────

def test_sweep_writes_only_superseded_never_the_steward_status(monkeypatch):
    fake = FakeTable()
    spy = _install(monkeypatch, fake)
    _seed_drift_row(fake, from_field="records.gone")

    statuses: list[str] = []
    real_update = self_learning._update_status

    def spy_update(table, mapping_id, discovered_at, status, extra=None, **kw):
        statuses.append(status)
        return real_update(table, mapping_id, discovered_at, status, extra, **kw)

    monkeypatch.setattr(self_learning, "_update_status", spy_update)
    swept = schema_drift.sweep_resolved_drift(_SYSTEM, [])

    assert swept == 1
    assert statuses == ["superseded"]  # the ONLY machine-legal terminal
    assert spy.calls == []  # DynamoDB-only — the sweep never touches the graph
    # Grep-level ADR-0053 pin: the module contains no steward-tombstone write.
    source = Path(schema_drift.__file__).read_text(encoding="utf-8")
    assert "rejected" not in source


# ── pin 3: steward rows survive ──────────────────────────────────────────────

def test_steward_rows_survive_the_sweep(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    agent_row = _seed_drift_row(fake, from_field="records.stale")
    accepted = _seed_drift_row(fake, status="accepted",
                               from_field="records.acked")
    user_row = _seed_drift_row(fake, source="user",
                               from_field="records.steward-noted")
    quarantined = _seed_drift_row(fake, quarantined="true",
                                  from_field="records.held")

    swept = schema_drift.sweep_resolved_drift(_SYSTEM, [])

    assert swept == 1  # only the pending agent row
    assert fake.by_id(agent_row["mapping_id"])["status"] == "superseded"
    assert fake.by_id(accepted["mapping_id"])["status"] == "accepted"
    assert fake.by_id(user_row["mapping_id"])["status"] == "pending"
    assert fake.by_id(quarantined["mapping_id"])["status"] == "pending"
    for row in (accepted, user_row, quarantined):
        assert "superseded_reason" not in fake.by_id(row["mapping_id"])


def test_sweep_scoped_to_the_named_system(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    other = _seed_drift_row(fake, from_field="records.other")
    other_system = dict(other, mapping_id=str(uuid.uuid4()),
                        from_system="sys-b", to_system="sys-b")
    fake.put_item(Item=other_system)

    swept = schema_drift.sweep_resolved_drift(_SYSTEM, [])

    assert swept == 1
    assert fake.by_id(other_system["mapping_id"])["status"] == "pending"


def test_row_accepted_between_read_and_write_is_not_superseded(monkeypatch):
    """TOCTOU pin: the supersede is CONDITIONAL on the row STILL being pending
    (``expect_status`` — the ADR-0043 subsume idiom). A steward accept landing
    between the sweep's read and its write WINS: the FakeTable's conditional
    write raises ``ConditionalCheckFailedException``, the row is skipped
    silently (NOT counted in ``resolved_swept``), and the sweep completes."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    racing = _seed_drift_row(fake, from_field="records.racing")
    stale = _seed_drift_row(fake, from_field="records.stale")

    real_pending = self_learning.get_pending_mappings

    def pending_then_steward_accepts():
        rows = real_pending()
        # Simulate the concurrent steward action AFTER the sweep's read.
        key = (racing["mapping_id"], racing["discovered_at"])
        fake._items[key]["status"] = "accepted"
        return rows

    monkeypatch.setattr(self_learning, "get_pending_mappings",
                        pending_then_steward_accepts)

    swept = schema_drift.sweep_resolved_drift(_SYSTEM, [])  # must not raise

    assert swept == 1  # only the undisputed stale row
    winner = fake.by_id(racing["mapping_id"])
    assert winner["status"] == "accepted"  # the steward decision survived
    assert "superseded_reason" not in winner
    assert fake.by_id(stale["mapping_id"])["status"] == "superseded"


# ── pin 4: provision hook — best-effort, never alters the job result ─────────

def _stub_provision(monkeypatch):
    monkeypatch.setattr(onboard, "_provision_jobs",
                        JobStore("provision", table=None))
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda b: b)
    monkeypatch.setattr(onboard, "_do_provision", lambda b: {"provisioned": True})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds",
                        lambda sid, mappings: None)


def test_provision_done_invokes_sweep_with_empty_drifts(monkeypatch):
    _stub_provision(monkeypatch)
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift",
                        lambda sid, drifts: calls.append((sid, drifts)) or 0)

    onboard._run_provision_job("job-1", ProvisionRequest(system_id=_SYSTEM))

    # Re-onboard rewrote the baseline — every pending drift row is resolved.
    assert calls == [(_SYSTEM, [])]
    assert onboard._provision_jobs["job-1"]["status"] == "done"


def test_raising_sweep_leaves_provision_job_done(monkeypatch):
    _stub_provision(monkeypatch)

    def boom(sid, drifts):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(schema_drift, "sweep_resolved_drift", boom)

    onboard._run_provision_job("job-2", ProvisionRequest(system_id=_SYSTEM))

    job = onboard._provision_jobs["job-2"]
    assert job["status"] == "done"  # sweep failure NEVER alters the result
    assert job["result"] == {"provisioned": True}


# ── pin 5: re-detection unaffected; target-grain keeps flap rows pending ────

def test_unresolved_drift_stays_pending_and_dedupes(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    _stub_discover(monkeypatch, [("id", "integer"), ("label", "text"),
                                 ("col_x", "text")])

    first = schema_drift.run_drift_check(_SYSTEM)
    second = schema_drift.run_drift_check(_SYSTEM)

    assert first["proposals_filed"] == 1
    # dup_pending: found, not filed — and NOT swept (still in the diff).
    assert second == {"system_id": _SYSTEM, "status": "ok",
                      "drifts_found": 1, "proposals_filed": 0, "proposal_noops": 1,
                      "kinds": {"column_added": 1}, "resolved_swept": 0, "refiled_after_accept": 0}
    (row,) = fake.all_items()
    assert row["status"] == "pending"


def test_unresolved_type_flap_row_stays_pending(monkeypatch):
    """Target-grain (not subject-grain) matching: two rows on the SAME
    ``table.column`` subject with different descriptors — the one whose
    descriptor is still in the current diff stays pending; only the stale
    sibling is swept (Risk 5: subject-grain would sweep both)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    stale = _seed_drift_row(fake, from_field="records.label",
                            reason="column_retyped",
                            description="was text, now varchar")
    current = _seed_drift_row(fake, from_field="records.label",
                              reason="column_retyped",
                              description="was text, now uuid")

    swept = schema_drift.sweep_resolved_drift(_SYSTEM, [
        {"from_field": "records.label", "reason": "column_retyped",
         "description": "was text, now uuid"},
    ])

    assert swept == 1
    assert fake.by_id(stale["mapping_id"])["status"] == "superseded"
    assert fake.by_id(current["mapping_id"])["status"] == "pending"
