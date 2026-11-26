# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A full removal of a USER registration schedules deletion of its own secret.

``adt/conn/<id>`` was never removed on offboard, so a fully removed user
registration left its credentials in Secrets Manager indefinitely. Now a FULL
removal (``POST /api/onboard/reset/{id}?full=true`` and the global
``?full=true`` reset) of a USER registration schedules deletion of that
registration's own secret: ``DeleteSecret`` with a recovery window (never
``ForceDeleteWithoutRecovery``), only through its exact own pointer, only when
the payload is bound to it, and FIRST, while the node and catalog entry still
record the pointer (test_connect_offboard_secret_first.py: a failure stops the
removal with 503). A default removal keeps the registration and so
keeps its secret. Demo, workshop, HighByte, legacy and unbound secrets, and any
other system's secret, are never touched. A later registration of the same id
inside the recovery window restores the name and writes its own payload.
Offline: Secrets Manager is a fake.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402

HB_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/highbyte-HbHbHb"
OTHER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/quality-lab-2-AbCdEf"
DEMO_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-db-credentials-XyZ123"
_REAL_STORE = cr._store_connection_secret     # before the secrets fixture replaces it


class _FakeSM:
    """The Secrets Manager calls the offboard and the registration make."""

    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions
        class ResourceExistsException(Exception):
            pass

        class InvalidRequestException(Exception):
            pass

    def __init__(self, store: dict):
        self.store = store
        self.deleted: list = []
        self.calls: list = []
        self.pending: set = set()

    def delete_secret(self, **kw):
        self.calls.append(("delete_secret", kw))
        self.deleted.append(kw)
        self.pending.add(kw["SecretId"])
        return {}

    def create_secret(self, Name, SecretString):  # noqa: N803 - boto3 names
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{Name}-AbCdEf"
        if arn in self.pending:
            raise self.exceptions.InvalidRequestException(
                "You can't create this secret because a secret with this name is already "
                "scheduled for deletion.")
        if arn in self.store:
            raise self.exceptions.ResourceExistsException(Name)
        self.store[arn] = json.loads(SecretString)
        return {"ARN": arn}

    def restore_secret(self, SecretId):  # noqa: N803
        self.calls.append(("restore_secret", SecretId))
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}-AbCdEf"
        self.pending.discard(arn)
        return {}

    def put_secret_value(self, SecretId, SecretString):  # noqa: N803
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}-AbCdEf"
        if arn in self.pending:
            raise self.exceptions.InvalidRequestException("scheduled for deletion")
        self.store[arn] = json.loads(SecretString)
        return {"ARN": arn}


@pytest.fixture
def sm(secrets, monkeypatch):
    fake = _FakeSM(secrets)
    monkeypatch.setattr(cr, "_secrets_client", lambda: fake)
    # A full removal reads the node's pointer with the CHECKED read.
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: "")
    return fake


def _deleted_ids(sm) -> list:
    return [kw["SecretId"] for kw in sm.deleted]


# ── the deletion rule ──────────────────────────────────────────────────────

def test_a_user_registrations_own_secret_is_scheduled_with_a_recovery_window(
        cat, secrets, sm):
    _register_pg()
    out = cr.delete_own_secret_before_removal(SID, cat.get(SID))
    assert out == CONN_ARN
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}], sm.deleted
    assert all("ForceDeleteWithoutRecovery" not in kw for kw in sm.deleted)


def test_after_a_restart_the_pointer_comes_from_the_node(cat, secrets, sm, monkeypatch):
    _register_pg()
    entry = {**cat.get(SID)}
    entry.pop("secret_arn")
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: CONN_ARN)
    assert cr.delete_own_secret_before_removal(SID, entry) == CONN_ARN


@pytest.mark.parametrize("entry, node, why", [
    ({"id": SID, "connection_origin": "demo", "secret_arn": CONN_ARN}, {}, "demo origin"),
    ({"id": SID, "secret_arn": CONN_ARN}, {}, "legacy, unmarked"),
    ({"id": SID, "connection_origin": "user", "secret_arn": DEMO_ARN}, {}, "shared demo secret"),
    ({"id": SID, "connection_origin": "user", "secret_arn": OTHER_ARN}, {}, "another system's"),
    ({"id": SID, "connection_origin": "user"}, {"secret_arn": OTHER_ARN}, "another system's (node)"),
])
def test_guard_no_other_secret_is_ever_scheduled(cat, secrets, sm, monkeypatch,
                                                 entry, node, why):
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    secrets[OTHER_ARN] = {"token": "t", "connection_origin": "user", "system_id": "quality-lab-2"}
    monkeypatch.setattr(cr, "node_secret_arn_checked", lambda sid: node.get("secret_arn", ""))
    assert cr.delete_own_secret_before_removal(SID, entry) == "", why
    assert sm.deleted == [], why


def test_guard_an_unbound_own_secret_is_left_in_place(cat, secrets, sm):
    _register_pg()
    secrets[CONN_ARN] = {"token": "t"}                     # rewritten unbound by an older writer
    assert cr.delete_own_secret_before_removal(SID, cat.get(SID)) == ""
    assert sm.deleted == []


def test_guard_the_highbyte_secret_is_never_scheduled(cat, secrets, sm):
    secrets[HB_ARN] = {"token": "hb", "connection_origin": "user", "system_id": "highbyte"}
    entry = {"id": "highbyte", "connection_origin": "user", "secret_arn": HB_ARN}
    assert cr.delete_own_secret_before_removal("highbyte", entry) == ""
    assert sm.deleted == []


def test_a_delete_failure_is_raised_for_the_removal_to_stop(cat, secrets, sm):
    """test_connect_offboard_secret_first.py: the removal then answers 503."""
    _register_pg()

    def _boom(**kw):
        raise RuntimeError("AccessDeniedException")
    sm.delete_secret = _boom
    with pytest.raises(cr.OwnSecretDeletionError) as err:
        cr.delete_own_secret_before_removal(SID, cat.get(SID))
    assert "RuntimeError" in str(err.value) and "AccessDenied" not in str(err.value)


# ── through the offboard routes ────────────────────────────────────────────

def _reset_patches(catalog_obj, node: dict):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return [patch.object(onboard, "neptune_write", MagicMock()),
            patch.object(onboard, "catalog", catalog_obj),
            patch.object(onboard, "_agent_manager", MagicMock()),
            patch.object(onboard, "remove_system_agent", MagicMock()),
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0),
            patch.object(onboard, "_reset_delete_graph_artifacts_invalidating", lambda sid: None),
            patch.object(onboard, "_reregister_demo_system", lambda sid: False),
            patch.object(cr, "_fetch_node_config", lambda sid: node.get(sid)),
            patch.object(cr, "node_secret_arn_checked",
                         lambda sid: (node.get(sid) or {}).get("secret_arn", ""))]


def _run_reset(catalog_obj, node: dict, *, full: bool):
    patches = _reset_patches(catalog_obj, node)
    for p in patches:
        p.start()
    try:
        return asyncio.run(onboard._run_reset_steps(SID, full=full))
    finally:
        for p in reversed(patches):
            p.stop()


def test_a_full_removal_of_a_user_registration_schedules_its_secret(cat, secrets, sm):
    _register_pg()
    result = _run_reset(cat, {}, full=True)
    assert result.get("own_secret_deletion_scheduled") is True, result
    assert _deleted_ids(sm) == [CONN_ARN]


def test_a_full_removal_after_a_restart_reads_the_pointer_before_the_graph_goes(
        cat, secrets, sm):
    _register_pg()
    cat.get(SID).pop("secret_arn")                         # a restart rebuilt the entry
    node = {SID: _node_row({"host": "db.example.internal"}, CONN_ARN, "PostgreSQL")}
    result = _run_reset(cat, node, full=True)
    assert _deleted_ids(sm) == [CONN_ARN], result


def test_guard_a_default_removal_keeps_the_secret(cat, secrets, sm):
    _register_pg()
    result = _run_reset(cat, {}, full=False)
    assert result["kept_registration"] is True
    assert sm.deleted == []


def test_guard_a_full_removal_of_a_demo_system_schedules_nothing(cat, secrets, sm):
    cat.add({"id": SID, "name": "CMMS", "connection_type": "PostgreSQL", "schema": "cmms",
             "status": "onboarded", "connection_origin": "demo", "secret_arn": DEMO_ARN})
    _run_reset(cat, {}, full=True)
    assert sm.deleted == []


def test_a_full_global_reset_schedules_only_user_registrations(secrets, sm, monkeypatch):
    secrets[CONN_ARN] = {"token": "t", "connection_origin": "user", "system_id": SID}
    secrets[HB_ARN] = {"token": "hb"}
    fresh = SystemCatalog([
        {"id": SID, "name": "Q", "status": "onboarded", "connection_origin": "user",
         "secret_arn": CONN_ARN},
        {"id": "highbyte", "name": "HB", "status": "onboarded", "connection_origin": "demo",
         "secret_arn": HB_ARN},
        {"id": "cmms", "name": "CMMS", "status": "onboarded", "connection_origin": "demo"},
    ])
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    with patch.object(onboard, "neptune_write", MagicMock()), \
            patch.object(onboard, "catalog", fresh), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "_graph_system_ids", lambda: []), \
            patch.object(onboard, "_retire_many_or_incomplete", lambda ids: 0), \
            patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                         return_value={"runtimes_awaited": [], "targets_awaited": [],
                                       "wait_timed_out": False}):
        result = asyncio.run(onboard._global_reset_steps(True))
    assert result.get("own_secrets_deletion_scheduled") == 1, result
    assert _deleted_ids(sm) == [CONN_ARN]


# ── the same id can be registered again inside the recovery window ─────────

def test_registering_the_id_again_restores_and_overwrites_the_name(cat, secrets, sm,
                                                                  monkeypatch):
    _register_pg()
    cr.delete_own_secret_before_removal(SID, cat.get(SID))
    cat.remove(SID)
    monkeypatch.setattr(cr, "_store_connection_secret", _REAL_STORE)   # the real create path
    res = _register_pg()
    assert res.get("status") == "created", res
    assert ("restore_secret", f"adt/conn/{SID}") in sm.calls, sm.calls
    assert secrets[CONN_ARN]["system_id"] == SID and secrets[CONN_ARN]["connection_origin"] == "user"


def test_guard_any_other_invalid_request_still_fails_the_registration(cat, secrets, sm,
                                                                      monkeypatch):
    def _create(Name, SecretString):  # noqa: N803
        raise sm.exceptions.InvalidRequestException("some other invalid request")
    sm.create_secret = _create
    monkeypatch.setattr(cr, "_store_connection_secret", _REAL_STORE)
    res = _register_pg()
    assert "error" in res and cat.get(SID) is None, res
    assert not [c for c in sm.calls if c[0] == "restore_secret"]
