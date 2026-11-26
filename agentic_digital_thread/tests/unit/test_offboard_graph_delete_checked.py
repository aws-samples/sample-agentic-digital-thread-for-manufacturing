# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A removal whose graph delete fails keeps its registration and is retried.

After the own secret is scheduled for deletion, the three removal paths went
back to fail-soft graph writes: ``db.neptune_write`` returns ``False`` on a
failure and nobody checked it. The single removal
(``_reset_delete_graph_artifacts``), the global reset
(``_global_reset_wipe_graph``) and the chat ``reset_system`` then removed
the catalog entry / session and answered success over live graph
artifacts, discarding what a retry needs. Every mandatory graph delete is
now checked; a failure keeps the catalog and session and answers
``ResetIncompleteError`` (the routes' 503) or, from the chat tool (which runs
the same removal), a retryable error, and the System node (the liveness
sentinel a retry needs) is deleted only after every other delete landed. A retry completes: the scheduled
deletion is idempotent. Offline: Secrets Manager and the graph are fakes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import SystemCatalog
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import _FakeSM

#: The :System node delete only (the removal marker's SET also names the node).
SYSTEM_DELETE = "{id: $sid}) DETACH DELETE s"


class _Graph:
    """neptune_write stand-in: records statements, fails those matching ``fail``."""

    def __init__(self):
        self.writes: list = []
        self.fail: set = set()

    def __call__(self, cypher, params=None):
        self.writes.append(cypher)
        return not any(token in cypher for token in self.fail)


@pytest.fixture
def env(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    graph = _Graph()
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    monkeypatch.setattr(onboard, "neptune_write", graph)
    monkeypatch.setattr(onboard, "neptune_query", lambda q, p=None: [{"id": SID}])
    monkeypatch.setattr(oa, "neptune_write", graph)
    store = MagicMock()
    monkeypatch.setattr(oa, "_store", lambda: store)
    return type("Env", (), {"sm": fake, "graph": graph, "session": store})


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())


def _cascade_patches(cat):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return [patch.object(onboard, "catalog", cat),
            patch.object(onboard, "_agent_manager", MagicMock()),
            patch.object(onboard, "remove_system_agent", MagicMock()),
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0),
            patch.object(onboard, "_reregister_demo_system", lambda sid: False)]


def _has_a_system_node(monkeypatch):
    """The onboarded system's System node, as the chat's checked read sees it."""
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: {"id": sid, "status": "onboarded"} if sid == SID else None)


def _chat(cat) -> dict:
    """The chat tool's removal (the app's full removal, run by the tool)."""
    patches = _cascade_patches(cat)
    for p in patches:
        p.start()
    try:
        return removed_by_the_chat(oa.reset_system, SID)
    finally:
        for p in reversed(patches):
            p.stop()


def _single(cat, *, full: bool):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    with patch.object(onboard, "catalog", cat), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup), \
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0), \
            patch.object(onboard, "_reregister_demo_system", lambda sid: False):
        return asyncio.run(onboard._run_reset_steps(SID, full=full))


# ── the single removal ─────────────────────────────────────────────────────

@pytest.mark.parametrize("full", [True, False], ids=["full", "default"])
@pytest.mark.parametrize("fails", ["IdentityRule", "SchemaEntry", SYSTEM_DELETE])
def test_a_failed_graph_delete_keeps_the_registration(cat, env, full, fails):
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.fail = {fails}
    with pytest.raises(onboard.ResetIncompleteError) as err:
        _single(cat, full=full)
    assert "graph" in str(err.value) and "retry" in str(err.value), str(err.value)
    entry = cat.get(SID)
    # Kept, in the removal_failed state (its card offers only Remove again:
    # test_offboard_removal_marker_restart.py).
    assert entry is not None and entry["status"] == "removal_failed", entry
    if fails != SYSTEM_DELETE:
        # The sentinel survives so a retry is admitted even after a restart.
        assert not any(w.startswith("MATCH (s:System {id: $sid}) DETACH DELETE s")
                       for w in env.graph.writes), env.graph.writes
    if full:
        assert [kw["SecretId"] for kw in env.sm.deleted] == [CONN_ARN]


def test_a_retry_after_a_failed_graph_delete_completes(cat, env):
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.fail = {"IdentityRule"}
    with pytest.raises(onboard.ResetIncompleteError):
        _single(cat, full=True)
    env.graph.fail = set()
    result = _single(cat, full=True)            # the secret is already pending deletion
    assert result["removed"] is True and result["own_secret_deletion_scheduled"] is True
    assert cat.get(SID) is None


def test_the_route_answers_503(cat, env, monkeypatch):
    from fastapi import HTTPException
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.fail = {"JoinPath"}

    async def _no_claim(system_id, wait, full=False, retry=False):
        return None
    monkeypatch.setattr(onboard, "_claim_reset", _no_claim)
    with patch.object(onboard, "catalog", cat), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "_reset_cleanup_agentcore_sync",
                         MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                                 "wait_timed_out": False})), \
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0), \
            pytest.raises(HTTPException) as err:
        asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=True))
    assert err.value.status_code == 503 and cat.get(SID) is not None


def test_guard_every_write_landing_removes_the_system_last(cat, env):
    _register_pg()
    cat.update_status(SID, "onboarded")
    _single(cat, full=True)
    system_deletes = [i for i, w in enumerate(env.graph.writes)
                      if w.startswith("MATCH (s:System {id: $sid}) DETACH DELETE s")]
    assert system_deletes == [len(env.graph.writes) - 1], env.graph.writes


# ── the global full reset ──────────────────────────────────────────────────

def _global(catalog_obj):
    with patch.object(onboard, "catalog", catalog_obj), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "_graph_system_ids", lambda: []), \
            patch.object(onboard, "_retire_many_or_incomplete", lambda ids: 0), \
            patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                         return_value={"runtimes_awaited": [], "targets_awaited": [],
                                       "wait_timed_out": False}):
        return asyncio.run(onboard._global_reset_steps(True))


def _one_user_registration(secrets):
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    return SystemCatalog([{"id": SID, "name": "Q", "status": "onboarded",
                           "connection_origin": "user", "secret_arn": CONN_ARN}])


def test_a_failed_global_wipe_keeps_every_registration(secrets, env):
    fresh = _one_user_registration(secrets)
    env.graph.fail = {"MATCH (n:IdentityRule)"}
    with pytest.raises(onboard.ResetIncompleteError) as err:
        _global(fresh)
    assert "IdentityRule" in str(err.value) and "retry" in str(err.value)
    assert [e["id"] for e in fresh.get_all()] == [SID]
    assert fresh.get(SID)["status"] == "removal_failed"
    assert [kw["SecretId"] for kw in env.sm.deleted] == [CONN_ARN]


def test_a_global_retry_completes(secrets, env):
    fresh = _one_user_registration(secrets)
    env.graph.fail = {"MATCH (n:IdentityRule)"}
    with pytest.raises(onboard.ResetIncompleteError):
        _global(fresh)
    env.graph.fail = set()
    result = _global(fresh)
    assert result["status"] == "reset" and fresh.get_all() == []


# ── the chat reset_system ──────────────────────────────────────────────────

def test_a_failed_chat_graph_delete_keeps_the_catalog_and_session(cat, env, monkeypatch):
    _register_pg()
    cat.update_status(SID, "onboarded")
    _has_a_system_node(monkeypatch)
    env.graph.fail = {SYSTEM_DELETE}
    res = _chat(cat)
    assert res.get("reason") == "removal_incomplete" and "retry" in res["error"], res
    assert cat.get(SID)["status"] == "removal_failed"
    env.session.delete_session.assert_not_called()
    assert [kw["SecretId"] for kw in env.sm.deleted] == [CONN_ARN]


def test_a_chat_retry_completes(cat, env, monkeypatch):
    _register_pg()
    cat.update_status(SID, "onboarded")
    _has_a_system_node(monkeypatch)
    env.graph.fail = {SYSTEM_DELETE}
    assert "error" in _chat(cat)
    env.graph.fail = set()
    res = _chat(cat)
    assert res.get("status") == "reset" and res.get("own_secret_deletion_scheduled") is True
    assert cat.get(SID) is None
    env.session.delete_session.assert_called_once_with(SID)
