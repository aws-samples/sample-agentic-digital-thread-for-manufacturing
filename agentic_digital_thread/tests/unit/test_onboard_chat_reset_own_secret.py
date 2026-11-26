# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding chat's ``reset_system`` deletes a user registration's own
secret first, or removes nothing.

The chat tool used to remove the System node and the catalog entry and
never touched ``adt/conn/<id>``, so a user registration removed from the
chat left its credentials in Secrets Manager with nothing pointing at them.
The tool now runs the app's own full removal, so the rule is the one a full
removal from the card follows (``connection_resolver.
delete_own_secret_before_removal``): the secret is scheduled BEFORE the
graph and catalog steps, and a failure removes nothing and says to retry.
Demo, HighByte and other-account secrets are never touched. In the
onboarding runtime container the tool removes nothing at all (it cannot run
the app's removal there). Offline: Secrets Manager and the graph are fakes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import SystemCatalog
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import HB_ARN, _FakeSM

FOREIGN_ARN = f"arn:aws:secretsmanager:us-east-1:444455556666:secret:adt/conn/{SID}-AbCdEf"


class AccessDeniedException(Exception):
    pass


@pytest.fixture
def env(secrets, monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    fake = _FakeSM(secrets)
    removed: list = []
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    for target, value in (("neptune_write", MagicMock(return_value=True)),
                          ("neptune_query", MagicMock(return_value=[{}])),
                          ("_agent_manager", MagicMock()),
                          ("remove_system_agent", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", cleanup),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_reset_delete_graph_artifacts_invalidating",
                           lambda sid: removed.append(("graph", sid))),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    store = MagicMock()
    monkeypatch.setattr(oa, "_store", lambda: store)
    nodes: set = set()
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: {"id": sid, "status": "onboarded"} if sid in nodes else None)
    return type("Env", (), {"sm": fake, "removed": removed, "session": store, "nodes": nodes})


def _onboarded(cat, env):
    """A user registration that was onboarded: its entry and its System node."""
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.nodes.add(SID)


def test_a_user_registrations_own_secret_is_deleted_before_the_node_and_entry(cat, env):
    _onboarded(cat, env)
    seen = {}
    real_delete = env.sm.delete_secret

    def _delete(**kw):
        seen["removed"], seen["registered"] = list(env.removed), cat.get(SID) is not None
        return real_delete(**kw)
    env.sm.delete_secret = _delete
    res = removed_by_the_chat(oa.reset_system, SID)
    assert res.get("status") == "reset" and res.get("own_secret_deletion_scheduled") is True, res
    assert env.sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]
    assert seen == {"removed": [], "registered": True}, seen
    assert env.removed == [("graph", SID)] and cat.get(SID) is None


def test_a_registered_user_system_is_not_removed_from_the_chat(cat, env, secrets):
    """Never onboarded (no System node): the chat removes nothing and points
    to Remove registration on its card; the registration and its own secret
    stay."""
    from agents.onboarding_session import SessionNotFoundError
    env.session.get_session.side_effect = SessionNotFoundError(SID)
    _register_pg()
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app" and "Remove registration" in res["error"], res
    assert cat.get(SID) is not None and CONN_ARN in secrets
    assert env.sm.deleted == [] and env.removed == []


def test_a_failed_deletion_removes_nothing(cat, env):
    _onboarded(cat, env)

    def _boom(**kw):
        raise AccessDeniedException("not authorized")
    env.sm.delete_secret = _boom
    res = removed_by_the_chat(oa.reset_system, SID)
    assert "error" in res and "status" not in res, res
    assert res.get("reason") == "removal_incomplete"
    assert "could not be scheduled for deletion" in res["error"] and "retry" in res["error"]
    assert env.removed == [] and cat.get(SID) is not None
    env.session.delete_session.assert_not_called()


def test_in_the_runtime_container_nothing_is_removed(cat, env, monkeypatch):
    _onboarded(cat, env)
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert env.sm.deleted == [] and env.removed == [] and cat.get(SID) is not None


@pytest.mark.parametrize("case", ["demo-entry", "no-origin", "highbyte", "other-account",
                                  "unbound"])
def test_guard_secrets_that_are_not_this_registrations_are_never_touched(
        env, monkeypatch, secrets, case):
    sid, arn = SID, CONN_ARN
    entry = {"id": SID, "name": "Q", "status": "onboarded"}
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    if case == "demo-entry":
        entry.update(connection_origin="demo", secret_arn=CONN_ARN)
    elif case == "no-origin":
        entry.update(secret_arn=CONN_ARN)
    elif case == "highbyte":
        sid, arn = "highbyte", HB_ARN
        entry = {"id": sid, "name": "HB", "status": "onboarded", "connection_origin": "user",
                 "secret_arn": HB_ARN}
        secrets[HB_ARN] = {"token": "hb", "connection_origin": "user", "system_id": "highbyte"}
    elif case == "other-account":
        entry.update(connection_origin="user", secret_arn=FOREIGN_ARN)
        monkeypatch.setattr(cr, "_fetch_secret", lambda a: (_ for _ in ()).throw(
            cr.ForeignAccountSecretError("other account")))
    else:
        entry.update(connection_origin="user", secret_arn=CONN_ARN)
        secrets[CONN_ARN] = {"token": "t"}
    fresh = SystemCatalog([entry])
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "catalog", fresh)
    res = oa.reset_system(sid)
    assert env.sm.deleted == [], (case, res)
    assert arn  # the case's secret stayed in place
