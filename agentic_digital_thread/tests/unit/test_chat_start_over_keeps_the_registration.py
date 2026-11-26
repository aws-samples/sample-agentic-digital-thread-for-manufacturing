# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Starting over a half-done chat onboarding never removes the registration.

``reset_system`` in the app starts over a chat onboarding that stopped
part-way (its session is discovered, mapped, resolved or failed) of a system
with no System node: only the session is cleared, so discover can run again,
and the registration and its connection stay. The full removal (the card's,
which forgets a user registration and schedules its own secret for deletion)
runs only otherwise. So the session and a checked System-node read come
first; a full removal is never tried while a start over applies, nor when the
session or the node cannot be read. Fixtures are real user registrations
(Connect New System: ``connection_origin`` user, credentials in their own
``adt/conn/<id>`` secret). Offline: Secrets Manager, the graph and the
session table are fakes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.onboarding_session import OutOfOrderError, SessionNotFoundError  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import _FakeSM  # noqa: E402

MCP_SID = "line-scada"
MCP_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{MCP_SID}-AbCdEf"


class _Sessions:
    """The onboarding session table's store API, in memory, with its
    conditional semantics."""

    def __init__(self):
        self.items: dict = {}
        self.fail_reads = False
        self.before_conditional_delete = None
        self.unconditional_deletes: list = []

    def put(self, sid: str, status: str):
        self.items[sid] = {"system_id": sid, "status": status,
                           "updated_at": datetime.now(timezone.utc).isoformat()}

    def get_session(self, sid):
        if self.fail_reads:
            raise RuntimeError("ProvisionedThroughputExceededException")
        if sid not in self.items:
            raise SessionNotFoundError(sid)
        return dict(self.items[sid])

    def delete_session_if_status(self, sid, status):
        if self.before_conditional_delete:
            self.before_conditional_delete()
        item = self.items.get(sid)
        if not item or item["status"] != status:
            raise OutOfOrderError(sid)
        del self.items[sid]

    def delete_session(self, sid):
        self.unconditional_deletes.append(sid)
        self.items.pop(sid, None)

    def claim_provisioning(self, sid, **_fields):
        self.items[sid]["status"] = "provisioning"


@pytest.fixture(autouse=True)
def _app_process(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    return fake


@pytest.fixture
def sessions(monkeypatch):
    store = _Sessions()
    monkeypatch.setattr(oa, "_store", lambda: store)
    return store


@pytest.fixture
def cascade(monkeypatch):
    """The card removal's AgentCore / graph / proposal steps, recorded."""
    calls = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                    "wait_timed_out": False})
    graph_deletes: list = []
    for target, value in (("neptune_write", lambda *a, **k: True),
                          ("neptune_query", MagicMock(return_value=[{}])),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", calls),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating", graph_deletes.append),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    calls.graph_deletes = graph_deletes
    return calls


def _register_mcp():
    return _register({"name": "Line SCADA", "system_type": "SCADA",
                      "connection_type": "MCP Server",
                      "connection_string": "https://mcp.example.com/mcp",
                      "connection_config": {"token": "Bearer example-token"}})


def _registration(cat, kind):
    if kind == "postgresql":
        assert _register_pg().get("status") == "created"
        return SID, CONN_ARN
    assert _register_mcp().get("status") == "created"
    return MCP_SID, MCP_ARN


def _app_reset_tools():
    """Both ways the chat reaches the app's reset in the app process: the
    app's own chat tool, and the onboarding tool's app branch."""
    import agents.onboard_chat_lifecycle as lifecycle
    return {"app_tool": lifecycle.reset_system, "onboarding_tool": oa.reset_system}


@pytest.fixture(params=["app_tool", "onboarding_tool"])
def reset(request):
    return lambda sid: _app_reset_tools()[request.param](sid)


def _registration_kept(cat, secrets, sm, sid, arn):
    entry = cat.get(sid)
    assert entry is not None, "the registration was removed"
    assert entry.get("connection_origin") == "user"
    assert entry.get("status") == "registered"
    assert arn in secrets, "the registration's own secret was removed"
    assert sm.deleted == [], f"the secret was scheduled for deletion: {sm.deleted}"


# ── start over: only the session goes ──────────────────────────────────────

@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed"])
def test_starting_over_a_user_registration_keeps_it(cat, secrets, sm, sessions, cascade,
                                                     reset, kind, status):
    sid, arn = _registration(cat, kind)
    sessions.put(sid, status)
    res = reset(sid)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == status
    assert sid not in sessions.items and sessions.unconditional_deletes == []
    _registration_kept(cat, secrets, sm, sid, arn)
    cascade.assert_not_called()
    assert cascade.graph_deletes == []
    assert onboard._RESET_JOBS.get(sid) is None, "a removal was claimed"


def test_a_session_that_moved_on_is_not_cleared_and_nothing_is_removed(
        cat, secrets, sm, sessions, cascade, reset):
    sid, arn = _registration(cat, "postgresql")
    sessions.put(sid, "resolved")
    sessions.before_conditional_delete = lambda: sessions.claim_provisioning(sid)
    res = reset(sid)
    assert res.get("reason") == "session_changed", res
    assert sessions.items[sid]["status"] == "provisioning"
    _registration_kept(cat, secrets, sm, sid, arn)
    cascade.assert_not_called()


def test_an_unreadable_session_removes_nothing(cat, secrets, sm, sessions, cascade, reset):
    sid, arn = _registration(cat, "postgresql")
    sessions.put(sid, "mapped")
    sessions.fail_reads = True
    res = reset(sid)
    assert res.get("reason") == "session_unavailable", res
    _registration_kept(cat, secrets, sm, sid, arn)
    cascade.assert_not_called()


def test_an_unreadable_graph_removes_nothing(cat, secrets, sm, sessions, cascade, reset,
                                             monkeypatch):
    sid, arn = _registration(cat, "postgresql")
    sessions.put(sid, "mapped")
    import agents.db as db

    def _down(*_a, **_k):
        raise db.GraphQueryError("neptune unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    res = reset(sid)
    assert res.get("reason") == "graph_unavailable", res
    assert sessions.items[sid]["status"] == "mapped"
    _registration_kept(cat, secrets, sm, sid, arn)
    cascade.assert_not_called()


def test_discover_runs_again_after_the_start_over(cat, secrets, sm, sessions, cascade, reset,
                                                  monkeypatch):
    sid, arn = _registration(cat, "postgresql")
    sessions.put(sid, "resolved")
    monkeypatch.setattr(oa, "_discover_backend_result", lambda *a, **k: {
        "tables": [{"name": "samples", "row_count": 2, "columns": [{"name": "lot_id"}]}]})
    sessions.create_session = lambda s, initiated_by, **f: sessions.items.__setitem__(
        s, {"system_id": s, "status": "discovering", **f})
    sessions.advance_status = lambda s, f, t, **kw: sessions.items[s].update(status=t, **kw)
    assert reset(sid).get("status") == "session_cleared"
    assert oa.discover(sid).get("total_tables") == 1
    _registration_kept(cat, secrets, sm, sid, arn)
