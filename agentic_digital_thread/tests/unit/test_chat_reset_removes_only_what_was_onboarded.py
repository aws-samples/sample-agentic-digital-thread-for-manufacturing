# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's reset_system removes only what was onboarded.

In the app, ``reset_system`` runs the card's full removal (which forgets a
user registration and schedules its own secret for deletion) only when a
checked read finds the system's System node. A system with no System node is
never removed from the chat, whatever its onboarding session says:

* a chat onboarding that stopped part-way (discovered, mapped, resolved,
  failed, or onboarded and removed since) is started over: only the session
  is cleared, conditionally on the status read;
* a session still ``discovering`` (written by a discover that is between its
  two writes) is left alone while it was updated within
  ``onboard_agent.DISCOVERING_STALE_AFTER_SECONDS``, and cleared once older;
* a ``provisioning`` session no provision in this process holds (the app
  restarted mid-provision) is marked ``failed`` and then cleared; while a
  provision holds it, the reset is refused;
* with no session, nothing is changed and the answer names the card action.

Fixtures are real user registrations (Connect New System: ``connection_origin``
user, credentials in their own ``adt/conn/<id>`` secret) and the real
onboarding session store over a conditional in-memory table. Offline.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import REMOVAL_FAILED  # noqa: E402
from agents.onboarding_session import OnboardingSessionStore, SessionNotFoundError  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402
    _Resource, _SessionTable)
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import _FakeSM  # noqa: E402

MCP_SID = "line-scada"

#: ``onboard_agent.DISCOVERING_STALE_AFTER_SECONDS``: past it, a discovering
#: session no discover is still writing.
STALE_S = getattr(oa, "DISCOVERING_STALE_AFTER_SECONDS", 120) + 5
MCP_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{MCP_SID}-AbCdEf"

_WALK = ["discovered", "mapped", "resolved"]


class _Table(_SessionTable):
    """The session table, with hooks: reads that fail, and a step run just
    before a conditional delete or update."""

    def __init__(self):
        super().__init__()
        self.fail_reads = False
        self.before_conditional = None
        self.unconditional_deletes: list = []

    def get_item(self, Key):
        if self.fail_reads:
            raise RuntimeError("ProvisionedThroughputExceededException")
        return super().get_item(Key)

    def delete_item(self, Key, ConditionExpression=None, **kw):
        if ConditionExpression is None:
            self.unconditional_deletes.append(Key["system_id"])
        elif self.before_conditional:
            hook, self.before_conditional = self.before_conditional, None
            hook()
        return super().delete_item(Key, ConditionExpression, **kw)

    def update_item(self, **kw):
        if self.before_conditional:
            hook, self.before_conditional = self.before_conditional, None
            hook()
        return super().update_item(**kw)


class Sessions:
    def __init__(self):
        self.table = _Table()
        self.store = OnboardingSessionStore(table_name="t", dynamodb_resource=_Resource(self.table))

    def put(self, sid: str, status: str, *, age_s: float = 0.0) -> None:
        """A session of ``sid`` walked to ``status`` through the store's own
        transitions, last updated ``age_s`` ago."""
        self.store.create_session(sid, initiated_by="test", connection_type="PostgreSQL")
        prev = "discovering"
        for step in _WALK[:_WALK.index(status) + 1] if status in _WALK else []:
            self.store.advance_status(sid, prev, step)
            prev = step
        if status in ("provisioning", "onboarded", "failed"):
            for step in _WALK:
                self.store.advance_status(sid, prev, step)
                prev = step
            self.store.claim_provisioning(sid, provision_job_id="job-1")
            if status != "provisioning":
                self.store.finalize(sid, status)
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        self.table.items[sid]["updated_at"] = stamp

    def status(self, sid: str) -> str | None:
        try:
            return self.store.get_session(sid)["status"]
        except SessionNotFoundError:
            return None


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
    s = Sessions()
    monkeypatch.setattr(oa, "_store", lambda: s.store)
    return s


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


def _registration(kind):
    if kind == "postgresql":
        assert _register_pg().get("status") == "created"
        return SID, CONN_ARN
    assert _register_mcp().get("status") == "created"
    return MCP_SID, MCP_ARN


@pytest.fixture(params=["app_tool", "onboarding_tool"])
def reset(request):
    """Both ways the chat reaches the app's reset in the app process: the
    app's own chat tool, and the onboarding tool's app branch."""
    import agents.onboard_chat_lifecycle as lifecycle
    tool = {"app_tool": lifecycle.reset_system, "onboarding_tool": oa.reset_system}[request.param]
    return lambda sid: tool(sid)


def _kept(cat, secrets, sm, cascade, sid, arn, status="registered"):
    entry = cat.get(sid)
    assert entry is not None, "the registration was removed"
    assert entry.get("connection_origin") == "user"
    assert entry.get("status") == status
    assert arn in secrets, "the registration's own secret was removed"
    assert sm.deleted == [], f"the secret was scheduled for deletion: {sm.deleted}"
    cascade.assert_not_called()
    assert cascade.graph_deletes == []
    assert onboard._RESET_JOBS.get(sid) is None, "a removal was claimed"


# ── node-less: started over, never removed ──────────────────────────────────

@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed", "onboarded"])
def test_a_half_done_onboarding_is_started_over_and_the_registration_kept(
        cat, secrets, sm, sessions, cascade, reset, kind, status):
    sid, arn = _registration(kind)
    sessions.put(sid, status)
    res = reset(sid)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == status
    assert sessions.status(sid) is None and sessions.table.unconditional_deletes == []
    _kept(cat, secrets, sm, cascade, sid, arn)


@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
def test_a_discover_in_progress_is_left_alone(cat, secrets, sm, sessions, cascade, reset, kind):
    sid, arn = _registration(kind)
    sessions.put(sid, "discovering", age_s=5)
    res = reset(sid)
    assert res.get("reason") == "discovering", res
    assert "still being discovered" in res["error"] and "Try again shortly" in res["error"]
    assert sessions.status(sid) == "discovering"
    _kept(cat, secrets, sm, cascade, sid, arn)


@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
def test_a_stale_discovering_session_is_cleared(cat, secrets, sm, sessions, cascade, reset, kind):
    sid, arn = _registration(kind)
    sessions.put(sid, "discovering", age_s=STALE_S)
    res = reset(sid)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == "discovering"
    assert sessions.status(sid) is None and sessions.table.unconditional_deletes == []
    _kept(cat, secrets, sm, cascade, sid, arn)


def test_a_stale_discovering_session_that_moves_on_is_not_cleared(cat, secrets, sm, sessions,
                                                                  cascade, reset):
    sid, arn = _registration("postgresql")
    sessions.put(sid, "discovering", age_s=STALE_S)
    sessions.table.before_conditional = lambda: sessions.store.advance_status(
        sid, "discovering", "discovered")
    res = reset(sid)
    assert res.get("reason") == "session_changed", res
    assert sessions.status(sid) == "discovered"
    _kept(cat, secrets, sm, cascade, sid, arn)


@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
def test_a_provision_lost_in_a_restart_is_marked_failed_then_started_over(
        cat, secrets, sm, sessions, cascade, reset, kind, monkeypatch):
    sid, arn = _registration(kind)
    sessions.put(sid, "provisioning")
    marked: list = []
    real = sessions.store.finalize

    def _finalize(s, status, **fields):
        marked.append((s, status, fields.get("failure_code")))
        return real(s, status, **fields)
    monkeypatch.setattr(sessions.store, "finalize", _finalize)
    res = reset(sid)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == "provisioning"
    assert marked == [(sid, "failed", "provision_interrupted")]
    assert sessions.status(sid) is None and sessions.table.unconditional_deletes == []
    _kept(cat, secrets, sm, cascade, sid, arn)


def test_a_lost_provision_that_finishes_meanwhile_is_not_cleared(cat, secrets, sm, sessions,
                                                                 cascade, reset):
    sid, arn = _registration("postgresql")
    sessions.put(sid, "provisioning")
    sessions.table.before_conditional = lambda: sessions.store.finalize(sid, "onboarded")
    res = reset(sid)
    assert res.get("reason") == "session_changed", res
    assert sessions.status(sid) == "onboarded"
    _kept(cat, secrets, sm, cascade, sid, arn)


@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
def test_a_provision_running_here_refuses_the_reset(cat, secrets, sm, sessions, cascade, reset,
                                                    kind):
    sid, arn = _registration(kind)
    sessions.put(sid, "provisioning")
    with onboard._claim_provision(sid):
        res = reset(sid)
    assert res.get("reason") == "removal_refused", res
    assert "being provisioned" in res["error"]
    assert sessions.status(sid) == "provisioning"
    _kept(cat, secrets, sm, cascade, sid, arn)


@pytest.mark.parametrize("kind", ["postgresql", "mcp"])
def test_without_a_session_nothing_is_removed_and_the_card_is_named(
        cat, secrets, sm, sessions, cascade, reset, kind):
    sid, arn = _registration(kind)
    res = reset(sid)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove registration" in res["error"] and "Nothing was changed" in res["error"]
    _kept(cat, secrets, sm, cascade, sid, arn)


def test_a_half_removed_system_without_a_node_is_handed_to_remove_again(
        cat, secrets, sm, sessions, cascade, reset):
    sid, arn = _registration("postgresql")
    cat.mark_removal_failed(sid, False)
    res = reset(sid)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove again" in res["error"]
    _kept(cat, secrets, sm, cascade, sid, arn, status=REMOVAL_FAILED)


# ── unreadable state changes nothing ────────────────────────────────────────

def test_an_unreadable_session_removes_nothing(cat, secrets, sm, sessions, cascade, reset):
    sid, arn = _registration("postgresql")
    sessions.put(sid, "provisioning")
    sessions.table.fail_reads = True
    res = reset(sid)
    assert res.get("reason") == "session_unavailable", res
    sessions.table.fail_reads = False
    assert sessions.status(sid) == "provisioning"
    _kept(cat, secrets, sm, cascade, sid, arn)


def test_an_unreadable_graph_removes_nothing(cat, secrets, sm, sessions, cascade, reset,
                                             monkeypatch):
    sid, arn = _registration("postgresql")
    sessions.put(sid, "discovering", age_s=STALE_S)
    import agents.db as db

    def _down(*_a, **_k):
        raise db.GraphQueryError("neptune unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    res = reset(sid)
    assert res.get("reason") == "graph_unavailable", res
    assert sessions.status(sid) == "discovering"
    _kept(cat, secrets, sm, cascade, sid, arn)


# ── with a System node: the card's full removal ─────────────────────────────

def test_an_onboarded_user_registration_is_removed_completely(cat, secrets, sm, sessions, cascade,
                                                              reset, monkeypatch):
    sid, arn = _registration("postgresql")
    cat.update_status(sid, "onboarded")
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda s: {"id": s, "status": "onboarded"} if s == sid else None)
    sessions.put(sid, "onboarded")
    res = removed_by_the_chat(reset, sid)
    assert res.get("status") == "reset", res
    assert cat.get(sid) is None
    assert sm.deleted == [{"SecretId": arn, "RecoveryWindowInDays": 7}]
    assert cascade.graph_deletes == [sid]
    assert sessions.status(sid) is None


def test_discover_runs_again_after_a_start_over(cat, secrets, sm, sessions, cascade, reset,
                                                monkeypatch):
    sid, arn = _registration("postgresql")
    sessions.put(sid, "provisioning")
    monkeypatch.setattr(oa, "_discover_backend_result", lambda *a, **k: {
        "tables": [{"name": "samples", "row_count": 2, "columns": [{"name": "lot_id"}]}]})
    assert reset(sid).get("status") == "session_cleared"
    assert oa.discover(sid).get("total_tables") == 1
    assert sessions.status(sid) == "discovered"
    _kept(cat, secrets, sm, cascade, sid, arn)
