# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests — agents/notification_routes.py (the header-bell feed).

Fully offline: every source is a fake (monkeypatched module attribute), same
idiom as test_onboard_reset_writer_gate.py's plain-dict ``_RESET_JOBS``
double. Pins:

* aggregation shapes per source (proposals count, removal jobs incl. the
  cross-replica active marker, snapshot import/repair single slots);
* FAIL-SOFT: one source raising yields a ``{"kind", "error"}`` item while
  the other sources' items survive intact;
* auth posture: mounted under ``/api`` (NOT ``/api/admin``) with no in-route
  admin gate — same as the sibling ``/api/proposed-mappings``.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.notification_routes as nr  # noqa: E402


class FakeJobStore:
    """Minimal JobStore double: ``_mem`` cache + ``get`` (the read surface
    notification_routes uses)."""

    def __init__(self, entries=None):
        self._mem = dict(entries or {})

    def get(self, key, default=None):
        return self._mem.get(key, default)


@pytest.fixture()
def quiet_sources(monkeypatch):
    """All five sources empty — each test overrides what it exercises."""
    monkeypatch.setattr(nr, "_proposal_items", lambda: [])
    monkeypatch.setattr(nr, "_question_items", lambda: [])
    monkeypatch.setattr(nr, "_removal_items", lambda: [])
    monkeypatch.setattr(nr, "_import_items", lambda: [])
    monkeypatch.setattr(nr, "_repair_items", lambda: [])


# ── response envelope ────────────────────────────────────────────────────────

def test_empty_feed_envelope(quiet_sources):
    out = nr.get_notifications()
    assert out["items"] == []
    assert isinstance(out["generated_at"], str) and out["generated_at"]


def test_items_aggregate_across_sources(quiet_sources, monkeypatch):
    monkeypatch.setattr(nr, "_proposal_items", lambda: [
        {"kind": "proposals", "count": 3, "label": "x", "href": "/admin.html"}])
    monkeypatch.setattr(nr, "_removal_items", lambda: [
        {"kind": "removal", "state": "running", "system_id": "erp",
         "label": "y", "href": "/#onboard"}])
    out = nr.get_notifications()
    assert [i["kind"] for i in out["items"]] == ["proposals", "removal"]


# ── fail-soft (pinned contract) ──────────────────────────────────────────────

def test_one_raising_source_yields_error_item_others_intact(
        quiet_sources, monkeypatch):
    def boom():
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(nr, "_proposal_items", boom)
    monkeypatch.setattr(nr, "_removal_items", lambda: [
        {"kind": "removal", "state": "done", "system_id": "mes",
         "label": "z", "href": "/#onboard"}])
    out = nr.get_notifications()
    kinds = {i["kind"]: i for i in out["items"]}
    assert "error" in kinds["proposals"]
    assert "Reference: " in kinds["proposals"]["error"]
    assert "graph unavailable" not in kinds["proposals"]["error"]
    # The healthy source's item survives, untouched.
    assert kinds["removal"]["state"] == "done"


def test_every_source_raising_never_raises(quiet_sources, monkeypatch):
    def boom():
        raise ValueError("nope")
    for fn in ("_proposal_items", "_question_items", "_removal_items",
               "_import_items", "_repair_items"):
        monkeypatch.setattr(nr, fn, boom)
    out = nr.get_notifications()
    assert len(out["items"]) == 5
    assert all("error" in i for i in out["items"])


# ── proposals source ─────────────────────────────────────────────────────────

def _patch_mappings(monkeypatch, rows):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "get_all_mappings", lambda: rows)


def test_proposals_counts_only_pending(monkeypatch):
    _patch_mappings(monkeypatch, [
        {"status": "pending"}, {"status": "pending"},
        {"status": "accepted"}, {"status": "rejected"}, {}])
    items = nr._proposal_items()
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "proposals"
    assert item["count"] == 2
    assert item["href"] == "/admin.html#tab-vocabulary"
    assert "2" in item["label"]


def test_proposals_absent_when_none_pending(monkeypatch):
    _patch_mappings(monkeypatch, [{"status": "accepted"}])
    assert nr._proposal_items() == []


def test_proposals_exclude_steward_questions(monkeypatch):
    """ADR-0059 D3/D5 pin: a pending steward_question row counts in the
    'questions' source ONLY — never double-notified as a proposal."""
    _patch_mappings(monkeypatch, [
        {"status": "pending"},
        {"status": "pending", "proposed_type": "steward_question"},
        {"status": "pending", "proposed_type": "steward_question"},
    ])
    items = nr._proposal_items()
    assert len(items) == 1 and items[0]["count"] == 1


# ── questions source (ADR-0059 D5) ───────────────────────────────────────────

def test_questions_counts_pending_steward_questions_as_one_item(monkeypatch):
    _patch_mappings(monkeypatch, [
        {"status": "pending", "proposed_type": "steward_question"},
        {"status": "pending", "proposed_type": "steward_question"},
        {"status": "pending", "proposed_type": "steward_question"},
        # Non-pending and non-question rows never count.
        {"status": "superseded", "proposed_type": "steward_question"},
        {"status": "accepted", "proposed_type": "steward_question"},
        {"status": "pending", "proposed_type": "value_mapping"},
        {"status": "pending"}, {},
    ])
    items = nr._question_items()
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "questions"
    assert item["count"] == 3
    assert item["label"] == "The system has 3 question(s) for you"
    assert item["href"] == "/#onboard/questions"


def test_questions_absent_when_none_pending(monkeypatch):
    _patch_mappings(monkeypatch, [
        {"status": "superseded", "proposed_type": "steward_question"},
        {"status": "pending", "proposed_type": "value_mapping"},
    ])
    assert nr._question_items() == []


def test_questions_source_registered_and_fail_soft(quiet_sources, monkeypatch):
    """The source is wired into _SOURCES and fail-soft like its siblings: a
    raising store yields the {'kind','error'} item, others intact."""
    assert ("questions", "_question_items") in nr._SOURCES

    def boom():
        raise RuntimeError("table unavailable")
    monkeypatch.setattr(nr, "_question_items", boom)
    monkeypatch.setattr(nr, "_proposal_items", lambda: [
        {"kind": "proposals", "count": 1, "label": "x", "href": "/admin.html"}])
    out = nr.get_notifications()
    kinds = {i["kind"]: i for i in out["items"]}
    assert "Reference: " in kinds["questions"]["error"]
    assert "table unavailable" not in kinds["questions"]["error"]
    assert kinds["proposals"]["count"] == 1


# ── removal source ───────────────────────────────────────────────────────────

def test_removal_running_done_error_states(monkeypatch):
    import agents.onboard as onboard
    store = FakeJobStore({
        "erp": {"state": "running"},
        "mes": {"state": "done", "result": {"status": "reset"}},
        "cmms": {"state": "error", "error": "Boom: x"},
    })
    monkeypatch.setattr(onboard, "_RESET_JOBS", store)
    items = {i["system_id"]: i for i in nr._removal_items()}
    assert items["erp"]["state"] == "running"
    assert items["mes"]["state"] == "done"
    assert items["cmms"]["state"] == "error"
    assert all(i["kind"] == "removal" for i in items.values())
    assert all(i["href"] == "/#onboard" for i in items.values())


def test_removal_active_marker_surfaces_cross_replica_job(monkeypatch):
    import agents.onboard as onboard

    class MarkerStore(FakeJobStore):
        """Marker + job readable via .get but NOT enumerated in _mem —
        the other-replica case."""

        def __init__(self):
            super().__init__({})
            self._remote = {
                onboard._ACTIVE_RESET_KEY: {"system_id": "plm"},
                "plm": {"state": "running"},
            }

        def get(self, key, default=None):
            return self._remote.get(key, default)

    monkeypatch.setattr(onboard, "_RESET_JOBS", MarkerStore())
    items = nr._removal_items()
    assert [i["system_id"] for i in items] == ["plm"]
    assert items[0]["state"] == "running"


def test_removal_marker_key_never_becomes_an_item(monkeypatch):
    import agents.onboard as onboard
    store = FakeJobStore({onboard._ACTIVE_RESET_KEY: {"system_id": ""}})
    monkeypatch.setattr(onboard, "_RESET_JOBS", store)
    assert nr._removal_items() == []


# ── snapshot import / repair sources ─────────────────────────────────────────

def test_import_job_running_with_started_at(monkeypatch):
    import agents.graph_snapshot_routes as gsr
    monkeypatch.setattr(gsr, "_IMPORT_JOBS", FakeJobStore({
        "global": {"state": "running", "started_at": "2026-08-06T00:00:00Z"}}))
    items = nr._import_items()
    assert len(items) == 1
    assert items[0]["kind"] == "import"
    assert items[0]["state"] == "running"
    assert items[0]["started_at"] == "2026-08-06T00:00:00Z"


def test_repair_job_done(monkeypatch):
    import agents.graph_snapshot_routes as gsr
    monkeypatch.setattr(gsr, "_REPAIR_JOBS", FakeJobStore({
        "global": {"state": "done", "result": {}}}))
    items = nr._repair_items()
    assert len(items) == 1
    assert items[0]["kind"] == "repair"
    assert items[0]["state"] == "done"


def test_snapshot_jobs_empty_when_no_job(monkeypatch):
    import agents.graph_snapshot_routes as gsr
    monkeypatch.setattr(gsr, "_IMPORT_JOBS", FakeJobStore())
    monkeypatch.setattr(gsr, "_REPAIR_JOBS", FakeJobStore())
    assert nr._import_items() == []
    assert nr._repair_items() == []


def test_repair_store_absent_is_fail_soft(monkeypatch):
    """The reconcile-repair store is read via getattr — its absence (older
    checkout) yields no items, never an exception."""
    import agents.graph_snapshot_routes as gsr
    monkeypatch.delattr(gsr, "_REPAIR_JOBS", raising=False)
    assert nr._repair_items() == []


# ── auth posture (route mount, no admin gate) ────────────────────────────────

def test_route_is_api_not_admin_and_ungated(quiet_sources):
    """Sibling-non-admin posture: path is /api/notifications (auth handled by
    the app-level middleware), and the handler runs with NO request.state —
    an admin gate reading request.state.is_admin would 403/blow up here."""
    app = FastAPI()
    app.include_router(nr.router)
    client = TestClient(app)
    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert "generated_at" in body
    # And the admin prefix is genuinely not used.
    paths = {r.path for r in nr.router.routes}
    assert paths == {"/api/notifications"}
