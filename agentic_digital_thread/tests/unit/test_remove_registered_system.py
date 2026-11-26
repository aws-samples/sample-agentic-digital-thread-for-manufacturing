# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A FULL removal can forget a 'registered' USER system.

A user system registered through Connect New System (and never onboarded), or
a once-onboarded system returned to 'registered' by a default removal, has no
:System graph node. The removal gate admitted only an 'onboarded' or
'removal_failed' entry (or a node carrying ``removal_incomplete``), so such a
registration answered ``{"error": "System '<id>' is not onboarded"}`` at HTTP
200 for BOTH a default and a full removal: neither the registration nor its
own ``adt/conn/<id>`` secret could ever be removed.

A FULL removal now admits a 'registered' USER system:

* the catalog entry is removed and its own bound secret scheduled for deletion
  (the existing ``delete_own_secret_before_removal`` rule), with no node needed;
* one log line names the system and whether its secret was scheduled;
* a DEFAULT removal of a registered system stays refused (nothing to reset);
* a demo/workshop/HighByte registration stays refused, and its shared secret
  is never touched;
* an unknown id stays refused.

Offline: Secrets Manager and the graph are fakes (same harness as
test_connect_offboard_secret_first.py). Every test drives the real
``POST /reset/<id>`` route (``onboard_reset_system``).
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_offboard_own_secret import HB_ARN, _FakeSM


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    fake.nodes = {}
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: fake.nodes.get(sid, ""))
    return fake


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    """Each test starts with an empty, in-memory reset job store."""
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))


@pytest.fixture
def steps():
    return []


def _reset_patches(catalog_obj, steps: list):
    """The cascade's AgentCore / graph / demo-reregister steps, stubbed; the
    catalog is REAL (so remove / update_status are exercised)."""
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return [patch.object(onboard, "neptune_write", MagicMock(return_value=True)),
            patch.object(onboard, "neptune_query", MagicMock(return_value=[{}])),
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


def _remove(catalog_obj, steps, system_id, *, full):
    return _run(catalog_obj, steps,
                lambda: onboard.onboard_reset_system(system_id, wait=True, full=full))


# ── a full removal forgets a registered USER system ─────────────────────────

def test_full_removal_of_a_registered_user_pg_system_schedules_its_secret(cat, secrets, sm, steps):
    """A registered user PostgreSQL system is removed from the catalog and its
    own bound secret scheduled for deletion."""
    _register_pg()
    assert cat.get(SID)["status"] == "registered"
    result = _remove(cat, steps, SID, full=True)
    assert result.get("status") == "reset", result
    assert result["removed"] is True and result["own_secret_deletion_scheduled"] is True
    assert cat.get(SID) is None, "the registration was not removed"
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]


def test_full_removal_of_a_registered_user_mcp_system_schedules_its_secret(cat, secrets, sm, steps):
    """Same for a registered user MCP system (no PostgreSQL schema)."""
    mid = "quality-mcp"
    arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{mid}-AbCdEf"
    secrets[arn] = {"token": "t", "connection_origin": "user", "system_id": mid}
    cat.add({"id": mid, "name": "Q MCP", "status": "registered",
             "connection_type": "MCP Server", "connection_origin": "user",
             "secret_arn": arn})
    result = _remove(cat, steps, mid, full=True)
    assert result.get("status") == "reset", result
    assert result["removed"] is True and result["own_secret_deletion_scheduled"] is True
    assert cat.get(mid) is None
    assert sm.deleted == [{"SecretId": arn, "RecoveryWindowInDays": 7}]


def test_the_removal_is_logged_with_the_system_and_its_secret(cat, secrets, sm, steps, caplog):
    """One INFO line names the system, its origin and whether its secret was
    scheduled; no secret value is logged."""
    caplog.set_level(logging.INFO, logger=onboard.logger.name)
    _register_pg()
    _remove(cat, steps, SID, full=True)
    lines = [r.getMessage() for r in caplog.records if "REGISTRATION_REMOVED" in r.getMessage()]
    assert lines, "no removal log line"
    assert f"system={SID}" in lines[0] and "origin=user" in lines[0]
    assert "secret_scheduled=True" in lines[0]
    assert all(CONN_ARN not in r.getMessage() for r in caplog.records)


# ── guards: default / demo / HighByte / unknown stay refused ────────────────

def test_default_removal_of_a_registered_user_system_is_refused_200(cat, secrets, sm, steps):
    """A DEFAULT removal of a registered system: nothing to reset, refused with
    the 'not onboarded' body at HTTP 200 (callers read data.error), the
    registration and its secret kept."""
    _register_pg()
    result = _remove(cat, steps, SID, full=False)
    assert result == {"error": f"System '{SID}' is not onboarded"}
    assert cat.get(SID) is not None
    assert sm.deleted == []


def test_full_removal_of_a_registered_demo_system_is_refused(cat, secrets, sm, steps):
    """A demo/workshop registration (origin 'demo') stays refused even for a
    full removal; its shared secret is never touched."""
    cat.add({"id": "cmms-demo", "name": "CMMS", "status": "registered",
             "connection_origin": "demo", "secret_arn": CONN_ARN})
    result = _remove(cat, steps, "cmms-demo", full=True)
    assert result == {"error": "System 'cmms-demo' is not onboarded"}
    assert cat.get("cmms-demo") is not None
    assert sm.deleted == []


def test_full_removal_of_a_registered_highbyte_is_refused(cat, secrets, sm, steps):
    """HighByte stays refused for a full removal (its origin may read 'user',
    but its shared secret must survive)."""
    cat.add({"id": "highbyte", "name": "HB", "status": "registered",
             "connection_origin": "user", "secret_arn": HB_ARN})
    result = _remove(cat, steps, "highbyte", full=True)
    assert result == {"error": "System 'highbyte' is not onboarded"}
    assert cat.get("highbyte") is not None
    assert sm.deleted == []


def test_full_removal_of_a_registered_system_without_origin_is_refused(cat, secrets, sm, steps):
    """No recorded origin is not a user registration: refused."""
    cat.add({"id": "legacy-reg", "name": "L", "status": "registered", "secret_arn": CONN_ARN})
    result = _remove(cat, steps, "legacy-reg", full=True)
    assert result == {"error": "System 'legacy-reg' is not onboarded"}
    assert sm.deleted == []


def test_unknown_id_is_refused_for_both_default_and_full(cat, secrets, sm, steps):
    assert _remove(cat, steps, "nope", full=False) == {"error": "Unknown system_id 'nope'"}
    assert _remove(cat, steps, "nope", full=True) == {"error": "Unknown system_id 'nope'"}


def test_a_demo_registered_systems_secret_is_never_scheduled(cat, secrets, sm, steps):
    """Even if a demo registered system reached the cascade, its shared secret
    is never touched (delete_own_secret_before_removal returns '')."""
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    cat.add({"id": "cmms-demo", "name": "CMMS", "status": "registered",
             "connection_origin": "demo", "secret_arn": CONN_ARN})
    result = _run(cat, steps, lambda: onboard._run_reset_steps("cmms-demo", full=True))
    assert result["own_secret_deletion_scheduled"] is False
    assert sm.deleted == []
