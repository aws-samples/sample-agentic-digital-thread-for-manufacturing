# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Negative learning — tombstones + /reopen (ADR-0053 E6, offline).

Behavioral proof of the E6 design against the in-memory ``FakeTable`` double
shared with ``test_self_learning_lifecycle.py``. No AWS, no Neptune, no
network.

Pinned contract (ADR-0053 E6 — rejected rows ARE the tombstones, no new
store):
  - ONE steward rejection inside ``TOMBSTONE_TTL_DAYS`` (90) FLAGS an exact
    agent re-proposal — it still inserts PENDING with the EXACT pinned
    review reason ``re-proposal of a shape the steward rejected on
    <YYYY-MM-DD> (mapping <mapping_id>)`` — flag-not-drop;
  - D3 damping still WINS: >= 2 rejections with the latest inside 7d drop
    the re-proposal (no row, no flag);
  - past the TTL the tombstone DECAYS: a dated old rejection re-proposes
    clean (no flag, no history);
  - ``source="user"`` bypasses tombstones entirely (user always wins);
  - the ``staged#`` promotion path re-enters the classifier — staging cannot
    resurrect a tombstoned shape;
  - the machine NEVER mints a tombstone: ``status="rejected"`` is written
    only by ``reject_mapping`` (steward seam) — ingestion drop paths write
    nothing;
  - ``reopen_mapping`` / ``POST /api/proposed-mappings/{id}/reopen``
    (steward-gated) flips rejected → pending, clearing the tombstone.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import agents.self_learning as self_learning

# ``agents.server`` mounts ``agents.vocabulary_routes`` at import time; stub
# it if absent so this file stays self-contained (same guard as
# test_steward_gate.py — the real module wins whenever it exists).
if importlib.util.find_spec("agents.vocabulary_routes") is None:  # pragma: no cover
    _stub = types.ModuleType("agents.vocabulary_routes")
    _stub.router = APIRouter(prefix="/api/admin/vocabulary")
    sys.modules["agents.vocabulary_routes"] = _stub

import agents.server as server

try:
    from unit.test_self_learning_lifecycle import FakeTable, NeptuneSpy
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from test_self_learning_lifecycle import FakeTable, NeptuneSpy


@pytest.fixture(autouse=True)
def _staging_buffer_off(monkeypatch):
    """ADR-0051 P5: threshold 1 disables the co-reference staging buffer so
    single-call inserts pin this suite's concern; the staging × tombstone
    interplay test re-enables it explicitly."""
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


def _install(monkeypatch, table: FakeTable) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", NeptuneSpy())
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)


def _log(*, source: str = "agent", ctx: str = "ctx") -> str | None:
    """The SAME exact-target value_mapping every time (one subject+target)."""
    return self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", "acct-7",
        "customer", 0.9, ctx, source=source,
    )


def _backdate_rejections(fake: FakeTable, *, days: float) -> str:
    """Rewrite every rejected row's rejected_at to ``days`` ago; returns the stamp."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for item in fake._items.values():
        if item.get("status") == "rejected":
            item["rejected_at"] = ts
    return ts


def _pinned_reason(rejected_at: str, mapping_id: str) -> str:
    return (f"re-proposal of a shape the steward rejected on "
            f"{rejected_at[:10]} (mapping {mapping_id})")


# --------------------------------------------------------------------------- #
# Flag-not-drop with the EXACT pinned wording
# --------------------------------------------------------------------------- #
def test_single_rejection_flags_not_drops(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)
    rejected_at = fake.by_id(first)["rejected_at"]

    second = _log()                # ONE recent rejection → flagged, not dropped
    assert second is not None
    item = fake.by_id(second)
    assert item["status"] == "pending"
    # EXACT pinned wording — audit probes + the SlCards banner key on it.
    assert item["review_reason"] == _pinned_reason(rejected_at, first)
    # Consumable history rides along (same shape as D3 R3).
    assert item["prior_rejections"] == [
        {"mapping_id": first, "rejected_at": rejected_at}]


def test_damping_still_wins_over_tombstone(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)
    second = _log()
    self_learning.reject_mapping(second)

    third = _log()                 # 2 rejections inside 7d → D3 drop, no flag row
    assert third is None
    assert len(fake) == 2          # nothing new stored
    assert all(i["status"] == "rejected" for i in fake.all_items())


def test_ttl_expired_rejection_inserts_clean(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)
    _backdate_rejections(fake, days=91)   # past TOMBSTONE_TTL_DAYS (90)

    second = _log()                # tombstone decayed → plain insert, no flag
    assert second is not None
    item = fake.by_id(second)
    assert item["status"] == "pending"
    assert item.get("review_reason", "") == ""
    assert "prior_rejections" not in item


def test_user_source_bypasses_tombstone(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)    # recent — would tombstone an agent

    user_id = _log(source="user")
    assert user_id is not None
    item = fake.by_id(user_id)
    assert item["source"] == "user"
    assert item.get("review_reason", "") == ""      # never flagged
    assert "prior_rejections" not in item


# --------------------------------------------------------------------------- #
# Staging interplay — promotion re-enters the classifier
# --------------------------------------------------------------------------- #
def test_staged_promotion_hits_tombstone(monkeypatch):
    """Staging cannot resurrect a tombstoned shape: after the steward rejects
    a PROMOTED row, the next observation of the same shape classifies
    tombstoned (immediate flagged pending — never re-buffered)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 2)

    assert _log(ctx="obs one") is None          # first co-reference → staged
    staged = [i for i in fake.all_items()
              if str(i.get("mapping_id", "")).startswith("staged#")]
    assert len(staged) == 1 and "status" not in staged[0]

    promoted = _log(ctx="obs two")              # threshold-th → promoted pending
    assert promoted is not None
    self_learning.reject_mapping(promoted)
    rejected_at = fake.by_id(promoted)["rejected_at"]

    third = _log(ctx="obs three")               # re-enters classify → tombstoned
    assert third is not None
    item = fake.by_id(third)
    assert item["status"] == "pending"
    assert item["review_reason"] == _pinned_reason(rejected_at, promoted)
    # It landed as a real pending row, not back in the buffer.
    assert not any(str(i.get("mapping_id", "")).startswith("staged#")
                   for i in fake.all_items())


# --------------------------------------------------------------------------- #
# Vouching invariant — the machine never mints a tombstone
# --------------------------------------------------------------------------- #
def test_machine_never_mints_tombstone(monkeypatch):
    """Every ingestion drop path writes NOTHING — ``status="rejected"`` rows
    come only from ``reject_mapping`` (the steward seam)."""
    puts: list[dict] = []

    class RecordingTable(FakeTable):
        def put_item(self, Item: dict) -> None:  # noqa: N803 - boto3 surface
            puts.append(dict(Item))
            super().put_item(Item)

    fake = RecordingTable()
    _install(monkeypatch, fake)

    # Gate 3b drop: degenerate self-join (same system + field both sides).
    assert self_learning.log_proposed_mapping(
        "erp", "orders.id", "o-1", "erp", "orders.id", "o-1",
        "order", 0.9, "ctx", proposed_type="join_path") is None
    # Gate 3f drop: failed-lookup sentinel on the value_mapping to-side.
    assert self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", "NOT_FOUND",
        "customer", 0.9, "ctx") is None
    # D3 damped drop: two steward rejections, then the re-proposal.
    first = _log()
    self_learning.reject_mapping(first)
    second = _log()
    self_learning.reject_mapping(second)
    assert _log() is None

    # No ingestion path ever PUT a rejected row; the only rejected rows are
    # the two the steward (reject_mapping) adjudicated.
    assert all(p.get("status") != "rejected" for p in puts)
    rejected = [i for i in fake.all_items() if i.get("status") == "rejected"]
    assert {i["mapping_id"] for i in rejected} == {first, second}
    assert all(i.get("rejected_at") for i in rejected)  # human stamp present


# --------------------------------------------------------------------------- #
# reopen_mapping — the steward's escape hatch
# --------------------------------------------------------------------------- #
def test_reopen_flips_rejected_to_pending(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)

    row = self_learning.reopen_mapping(first, actor="steward@example.com")
    assert "error" not in row
    assert row["mapping_id"] == first
    assert row["status"] == "pending"
    assert row["review_reason"] == "re-opened by steward"
    assert row["reopened_by"] == "steward@example.com"
    stored = fake.by_id(first)
    assert stored["status"] == "pending"
    assert stored["review_reason"] == "re-opened by steward"
    assert stored["reopened_by"] == "steward@example.com"


def test_reopen_clears_tombstone(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    self_learning.reject_mapping(first)
    self_learning.reopen_mapping(first, actor="steward@example.com")

    # The same shape now classifies dup_pending — NOT tombstoned.
    again = _log()
    assert again == first          # existing pending row returned, no new row
    assert len(fake) == 1
    assert fake.by_id(first)["status"] == "pending"


def test_reopen_only_rejected_and_unknown_errors(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    assert "not found" in self_learning.reopen_mapping("nope")["error"]
    pending = _log()
    err = self_learning.reopen_mapping(pending)["error"]
    assert "only rejected proposals can be re-opened" in err
    assert fake.by_id(pending)["status"] == "pending"   # untouched


# --------------------------------------------------------------------------- #
# Route — steward-gated, mirrors requeue's posture
# --------------------------------------------------------------------------- #
from fastapi.routing import APIRoute

_REOPEN_PATH = "/api/proposed-mappings/{mapping_id}/reopen"


def _real_reopen_route() -> APIRoute:
    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path == _REOPEN_PATH and "POST" in r.methods:
            return r
    raise AssertionError(f"route not registered on server.app: POST {_REOPEN_PATH}")


def _client(is_admin: "bool | None" = None) -> TestClient:
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.router.routes.append(_real_reopen_route())
    return TestClient(app)


def test_reopen_route_steward_gated(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("reopen_mapping must not run behind a failed gate")

    monkeypatch.setattr(self_learning, "reopen_mapping", _boom)
    # Explicit non-admin verdict → 403.
    resp = _client(is_admin=False).post("/api/proposed-mappings/m1/reopen")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"
    # ABSENT verdict fails closed too (no middleware = no is_admin attribute).
    resp = _client(is_admin=None).post("/api/proposed-mappings/m1/reopen")
    assert resp.status_code == 403


def test_reopen_route_status_codes(monkeypatch):
    monkeypatch.setattr(
        self_learning, "reopen_mapping",
        lambda mid, actor="": {"mapping_id": mid, "status": "pending",
                               "review_reason": "re-opened by steward"})
    resp = _client(is_admin=True).post("/api/proposed-mappings/m1/reopen")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    monkeypatch.setattr(self_learning, "reopen_mapping",
                        lambda mid, actor="": {"error": f"Mapping {mid} not found"})
    assert _client(is_admin=True).post(
        "/api/proposed-mappings/m1/reopen").status_code == 404

    monkeypatch.setattr(
        self_learning, "reopen_mapping",
        lambda mid, actor="": {"error": "Mapping m1 is not rejected "
                                        "(status=pending) — only rejected "
                                        "proposals can be re-opened"})
    assert _client(is_admin=True).post(
        "/api/proposed-mappings/m1/reopen").status_code == 400
