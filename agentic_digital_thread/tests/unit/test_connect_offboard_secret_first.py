# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A full removal deletes the registration's own secret FIRST, or stops.

A full removal (``POST /api/onboard/reset/{id}?full=true`` and the global
``?full=true`` reset) removed the System node and the catalog entry and only
then tried, best-effort, to schedule deletion of the USER registration's own
``adt/conn/<id>`` secret. A failed read or delete was logged and the removal
answered "reset": the only record of the pointer and its ownership was gone,
so the credentials were orphaned for good. Now the secret is verified and
its deletion scheduled BEFORE the graph and catalog steps; any failure
(graph read, account check, secret read, DeleteSecret) answers 503
(incomplete, retryable) with the registration and graph intact. A retry
after an earlier attempt already scheduled the deletion completes. Demo,
HighByte, unbound and other-account secrets are still never touched.
Offline: Secrets Manager and the graph are fakes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import agents.connection_resolver as cr
import agents.onboard as onboard
from agents.catalog import SystemCatalog
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import HB_ARN, _FakeSM

FOREIGN_ARN = f"arn:aws:secretsmanager:us-east-1:444455556666:secret:adt/conn/{SID}-AbCdEf"


class AccessDeniedException(Exception):
    pass


class InvalidRequestException(Exception):
    pass


class ResourceNotFoundException(Exception):
    pass


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    fake.nodes = {}
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: fake.nodes.get(sid, ""))
    return fake


@pytest.fixture
def steps():
    """The cascade's later steps, recorded in order."""
    return []


def _reset_patches(catalog_obj, steps: list):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return [patch.object(onboard, "neptune_write", MagicMock()),
            patch.object(onboard, "catalog", catalog_obj),
            patch.object(onboard, "_agent_manager", MagicMock()),
            patch.object(onboard, "remove_system_agent", MagicMock()),
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0),
            patch.object(onboard, "_reset_delete_graph_artifacts_invalidating",
                         lambda sid: steps.append(("graph", sid))),
            patch.object(onboard, "_reregister_demo_system", lambda sid: False)]


def _run(catalog_obj, steps, coro_fn):
    patches = _reset_patches(catalog_obj, steps)
    for p in patches:
        p.start()
    try:
        return asyncio.run(coro_fn())
    finally:
        for p in reversed(patches):
            p.stop()


def _full_removal(cat, steps):
    return _run(cat, steps, lambda: onboard._run_reset_steps(SID, full=True))


def _failing(sm, exc):
    def _delete(**kw):
        sm.calls.append(("delete_secret", kw))
        raise exc
    sm.delete_secret = _delete


# ── a failure stops the removal with the registration intact ──────────────

@pytest.mark.parametrize("break_it", ["delete", "secret_read", "graph_read", "account"])
def test_a_failure_stops_the_removal_before_the_graph_and_catalog(
        cat, secrets, sm, steps, monkeypatch, break_it):
    _register_pg()
    if break_it == "delete":
        _failing(sm, AccessDeniedException("not authorized"))
    elif break_it == "secret_read":
        monkeypatch.setattr(cr, "_fetch_secret",
                            lambda arn: (_ for _ in ()).throw(RuntimeError("Throttling")))
    elif break_it == "graph_read":
        cat.get(SID).pop("secret_arn")            # after a restart: pointer only on the node

        def _graph_down(sid):
            from agents.db import GraphQueryError
            raise GraphQueryError("neptune unavailable")
        monkeypatch.setattr(cr, "node_secret_arn_checked", _graph_down)
    else:
        monkeypatch.setattr(cr, "_fetch_secret", lambda arn: (_ for _ in ()).throw(
            cr.AccountUnknownSecretError("account unknown")))
    with pytest.raises(onboard.ResetIncompleteError) as err:
        _full_removal(cat, steps)
    msg = str(err.value)
    assert "could not be scheduled for deletion" in msg and "retry" in msg, msg
    assert steps == [], "the graph was cleared before the secret was dealt with"
    assert cat.get(SID) is not None, "the registration was removed"
    assert [c for c in sm.calls if c[0] == "delete_secret"] == (
        [("delete_secret", {"SecretId": CONN_ARN, "RecoveryWindowInDays": 7})]
        if break_it == "delete" else [])


def test_the_secret_is_scheduled_before_the_graph_and_catalog_go(cat, secrets, sm, steps):
    _register_pg()
    seen = {}
    real_delete = sm.delete_secret

    def _delete(**kw):
        seen["graph_steps"] = list(steps)
        seen["registered"] = cat.get(SID) is not None
        return real_delete(**kw)
    sm.delete_secret = _delete
    result = _full_removal(cat, steps)
    assert seen == {"graph_steps": [], "registered": True}, seen
    assert result["own_secret_deletion_scheduled"] is True and result["removed"] is True
    assert steps == [("graph", SID)] and cat.get(SID) is None


def test_the_route_answers_503_and_keeps_the_registration(cat, secrets, sm, steps,
                                                          monkeypatch):
    _register_pg()
    _failing(sm, AccessDeniedException("not authorized"))

    async def _no_claim(system_id, wait, full=False, retry=False):
        return None
    monkeypatch.setattr(onboard, "_claim_reset", _no_claim)
    with pytest.raises(HTTPException) as err:
        _run(cat, steps, lambda: onboard.onboard_reset_system(SID, wait=True, full=True))
    assert err.value.status_code == 503
    assert "could not be scheduled for deletion" in err.value.detail
    assert cat.get(SID) is not None and steps == []


# ── retry and the "nothing to delete" cases complete ──────────────────────

@pytest.mark.parametrize("where", ["read", "delete"])
def test_a_retry_after_the_deletion_was_already_scheduled_completes(
        cat, secrets, sm, steps, monkeypatch, where):
    _register_pg()
    pending = InvalidRequestException(
        "You can't perform this operation on the secret because it was marked for deletion.")
    if where == "read":
        monkeypatch.setattr(cr, "_fetch_secret", lambda arn: (_ for _ in ()).throw(pending))
    else:
        _failing(sm, pending)
    result = _full_removal(cat, steps)
    assert result["own_secret_deletion_scheduled"] is True and cat.get(SID) is None


def test_a_secret_that_is_already_gone_completes(cat, secrets, sm, steps, monkeypatch):
    _register_pg()
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: (_ for _ in ()).throw(
        ResourceNotFoundException("not found")))
    result = _full_removal(cat, steps)
    assert result["own_secret_deletion_scheduled"] is False and cat.get(SID) is None


@pytest.mark.parametrize("entry", [
    {"id": SID, "name": "CMMS", "status": "onboarded", "connection_origin": "demo",
     "secret_arn": CONN_ARN},
    {"id": SID, "name": "Q", "status": "onboarded", "connection_origin": "user",
     "secret_arn": FOREIGN_ARN},
], ids=["demo", "other-account"])
def test_guard_secrets_that_are_not_this_registrations_are_never_touched(
        cat, secrets, sm, steps, entry, monkeypatch):
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: (
        (_ for _ in ()).throw(cr.ForeignAccountSecretError("other account"))
        if arn == FOREIGN_ARN else dict(secrets[arn])))
    cat.add(entry)
    result = _full_removal(cat, steps)
    assert sm.deleted == [] and result["removed"] is True


def test_guard_highbyte_is_never_touched(secrets, sm, steps):
    fresh = SystemCatalog([{"id": "highbyte", "name": "HB", "status": "onboarded",
                            "connection_origin": "user", "secret_arn": HB_ARN}])
    secrets[HB_ARN] = {"token": "hb", "connection_origin": "user", "system_id": "highbyte"}
    _run(fresh, steps, lambda: onboard._run_reset_steps("highbyte", full=True))
    assert sm.deleted == []


# ── the global full reset ──────────────────────────────────────────────────

def _global(catalog_obj, wipes: list):
    with patch.object(onboard, "neptune_write", MagicMock(side_effect=lambda *a, **k: (
            wipes.append(a[0]) if "DELETE" in a[0] else None) or True)), \
            patch.object(onboard, "catalog", catalog_obj), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "_graph_system_ids", lambda: []), \
            patch.object(onboard, "_retire_many_or_incomplete", lambda ids: 0), \
            patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                         return_value={"runtimes_awaited": [], "targets_awaited": [],
                                       "wait_timed_out": False}):
        return asyncio.run(onboard._global_reset_steps(True))


def _two_user_registrations(secrets):
    other = "quality-lab-2"
    other_arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{other}-AbCdEf"
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    secrets[other_arn] = {"token": "t", "connection_origin": "user", "system_id": other}
    return SystemCatalog([
        {"id": SID, "name": "Q", "status": "onboarded", "connection_origin": "user",
         "secret_arn": CONN_ARN},
        {"id": other, "name": "Q2", "status": "onboarded", "connection_origin": "user",
         "secret_arn": other_arn},
    ]), other_arn


def test_a_global_full_reset_stops_before_the_wipe_when_a_deletion_fails(secrets, sm):
    fresh, other_arn = _two_user_registrations(secrets)
    real_delete = sm.delete_secret

    def _delete(**kw):
        if kw["SecretId"] == other_arn:
            raise AccessDeniedException("not authorized")
        return real_delete(**kw)
    sm.delete_secret = _delete
    wipes: list = []
    with pytest.raises(onboard.ResetIncompleteError) as err:
        _global(fresh, wipes)
    assert "quality-lab-2" in str(err.value) and "retry" in str(err.value)
    assert wipes == [], "the graph was wiped before the secrets were dealt with"
    assert len(fresh.get_all()) == 2, "registrations were cleared"


def test_a_global_full_reset_schedules_before_the_wipe(secrets, sm):
    fresh, other_arn = _two_user_registrations(secrets)
    seen = []
    real_delete = sm.delete_secret
    wipes: list = []

    def _delete(**kw):
        seen.append((kw["SecretId"], list(wipes), len(fresh.get_all())))
        return real_delete(**kw)
    sm.delete_secret = _delete
    result = _global(fresh, wipes)
    assert result["own_secrets_deletion_scheduled"] == 2, result
    assert all(w == [] and n == 2 for _, w, n in seen), seen
    assert fresh.get_all() == []
