# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""On a workshop deployment a seeded system's removal keeps its registration.

An attendee who removes a seeded system and forgets its registration cannot
bring it back: the registration held the shared connection they never see.
So with workshop mode on (``agents.demo.workshop.is_enabled``, the same check
``GET /api/admin/workshop/story`` makes) a removal that asks to forget the
registration (``full=true``) of a system whose catalog ``connection_origin``
is not ``user`` (unknown counts as seeded) is refused with a 403 before
anything is claimed or removed, from the HTTP route and from the onboarding
chat's removal alike. A system the attendee connected (origin ``user``, e.g.
a REST CMMS) can be connected again, so it keeps every removal. A
removal that keeps the registration, module 4's "remove and onboard again",
runs as it does outside workshop mode. Outside workshop mode nothing changes.
``retry=true`` may still finish a full removal that did not finish (its mode
is fixed; the retry's own checks refuse anything else).

All side effects are mocked, as in ``test_onboard_reset_async.py``.
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_proposal_store(monkeypatch):
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    store = JobStore("reset", table=None)
    monkeypatch.setattr(onboard, "_RESET_JOBS", store)
    yield store


@pytest.fixture
def workshop_on(monkeypatch):
    monkeypatch.setenv("ADT_WORKSHOP", "true")


@pytest.fixture
def workshop_off(monkeypatch):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)


#: Catalog origins of a system the attendee cannot connect again: the
#: workshop/demo seed's, and an entry with no recorded origin (unknown).
SEEDED_ORIGINS = ["workshop", "demo", None]


def _fake_catalog(system_id, origin=None, present=True):
    fake = MagicMock()
    entry = {"id": system_id, "status": "onboarded"}
    if origin is not None:
        entry["connection_origin"] = origin
    fake.get.return_value = entry if present else None
    fake.remove.return_value = True
    return fake


def _patches(system_id, catalog):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return (
        patch.object(onboard, "neptune_write", MagicMock()),
        patch.object(onboard, "catalog", catalog),
        patch.object(onboard, "_agent_manager", MagicMock()),
        patch.object(onboard, "remove_system_agent", MagicMock()),
        patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
    )


async def _remove_via_route(system_id, catalog, full):
    """POST /api/onboard/reset/<id>[?full=true], then poll until the job ends."""
    p = _patches(system_id, catalog)
    with p[0], p[1], p[2], p[3], p[4]:
        started = await onboard.onboard_reset_system_route(system_id, full=full)
        status = await onboard.onboard_reset_status(system_id)
        deadline = time.monotonic() + 10.0
        while status["state"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            status = await onboard.onboard_reset_status(system_id)
    return started, status


@pytest.mark.parametrize("origin", SEEDED_ORIGINS)
def test_workshop_refuses_forgetting_a_seeded_systems_registration(workshop_on, origin):
    catalog = _fake_catalog("cmms-ind", origin)
    with patch.object(onboard, "_reset_preflight", side_effect=AssertionError(
            "the removal was checked and claimed")) as preflight:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_remove_via_route("cmms-ind", catalog, full=True))
    assert exc.value.status_code == 403
    assert "registration" in exc.value.detail
    assert "workshop" in exc.value.detail.lower()
    preflight.assert_not_called()  # refused before anything is checked or claimed
    catalog.remove.assert_not_called()
    assert asyncio.run(onboard.onboard_reset_status("cmms-ind"))["state"] == "none"


def test_workshop_refuses_a_system_missing_from_the_catalog(workshop_on):
    """No catalog entry: the origin is unknown, so it counts as seeded."""
    catalog = _fake_catalog("cmms-ind", present=False)
    with patch.object(onboard, "_reset_preflight", side_effect=AssertionError(
            "the removal was checked and claimed")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_remove_via_route("cmms-ind", catalog, full=True))
    assert exc.value.status_code == 403


def test_workshop_lets_a_user_system_forget_its_registration(workshop_on):
    """A system the attendee connected (a REST CMMS) can be connected
    again: its forgetting removal passes this gate and meets the usual checks."""
    catalog = _fake_catalog("rest-cmms", "user")
    with patch.object(onboard, "_reset_preflight", side_effect=AssertionError(
            "reached the usual checks")) as preflight:
        with pytest.raises(AssertionError, match="usual checks"):
            asyncio.run(_remove_via_route("rest-cmms", catalog, full=True))
    preflight.assert_called_once()
    assert preflight.call_args.args == ("rest-cmms", True)


@pytest.mark.parametrize("origin", SEEDED_ORIGINS + ["user"])
def test_workshop_keeps_remove_and_onboard_again(workshop_on, origin):
    """Module 4's recovery: remove keeping the registration; the system goes
    back to the catalog, registration intact, ready to onboard again."""
    catalog = _fake_catalog("cmms-ind", origin)
    started, status = asyncio.run(_remove_via_route("cmms-ind", catalog, full=False))
    assert started == {"status": "started", "system_id": "cmms-ind"}
    assert status["state"] == "done", status
    assert status["result"]["status"] == "reset"
    catalog.remove.assert_not_called()  # the registration is kept


def test_outside_workshop_a_seeded_full_removal_still_runs(workshop_off):
    catalog = _fake_catalog("cmms-ind", "workshop")
    started, status = asyncio.run(_remove_via_route("cmms-ind", catalog, full=True))
    assert started == {"status": "started", "system_id": "cmms-ind"}
    assert status["state"] == "done", status


def _chat_removal(system_id, catalog):
    """``start_removal_for_chat`` with the claim's checks passing and the job
    thread mocked; returns (answer, preflight mock, Thread mock)."""
    with patch.object(onboard, "catalog", catalog), \
            patch.object(onboard, "_reset_preflight", return_value=(None, None)) as preflight, \
            patch.object(onboard, "_reset_publish_claim_or_409"), \
            patch("agents.startup_restore.refusal", return_value=None), \
            patch.object(onboard.threading, "Thread") as thread:
        answer = onboard.start_removal_for_chat(system_id)
    return answer, preflight, thread


@pytest.mark.parametrize("origin", SEEDED_ORIGINS)
def test_workshop_refuses_the_chat_removal_of_a_seeded_system(workshop_on, origin):
    """The onboarding chat's removal is a full removal: refused the same way
    for a seeded system, with the reason relayed and nothing started."""
    answer, preflight, thread = _chat_removal("cmms-ind", _fake_catalog("cmms-ind", origin))
    assert answer["reason"] == "removal_refused"
    assert "registration" in answer["error"]
    preflight.assert_not_called()
    thread.assert_not_called()


def test_workshop_runs_the_chat_removal_of_a_user_system(workshop_on):
    answer, preflight, thread = _chat_removal(
        "rest-cmms", _fake_catalog("rest-cmms", "user"))
    assert answer["status"] == "removing", answer
    preflight.assert_called_once()
    thread.assert_called_once()


def _half_removed_catalog(system_id, origin="workshop", mode=None):
    """A catalog whose entry is ``removal_failed``, with ``removal_mode`` when
    the earlier removal's mode is recorded."""
    from agents.catalog import REMOVAL_FAILED, REMOVAL_MODE
    fake = MagicMock()
    entry = {"id": system_id, "status": REMOVAL_FAILED, "connection_origin": origin}
    if mode is not None:
        entry[REMOVAL_MODE] = mode
    fake.get.return_value = entry
    return fake


def _full_retry(catalog):
    """POST /reset/cmms-ind?full=true&retry=true with the preflight standing
    in for every later check (it raises once reached)."""
    with patch.object(onboard, "catalog", catalog), \
            patch.object(onboard, "_reset_preflight", side_effect=AssertionError(
                "reached the retry's own checks")) as preflight:
        try:
            asyncio.run(onboard.onboard_reset_system_route("cmms-ind", full=True, retry=True))
        except HTTPException as exc:
            return exc, preflight
        except AssertionError as exc:
            return exc, preflight
    return None, preflight


def test_workshop_lets_a_retry_finish_a_known_full_removal(workshop_on):
    """"Remove again" of a full removal that did not finish (its mode recorded
    as full) passes this gate and meets the retry's own checks."""
    result, preflight = _full_retry(_half_removed_catalog("cmms-ind", mode="full"))
    assert isinstance(result, AssertionError), result
    preflight.assert_called_once()


@pytest.mark.parametrize("mode", [None, "default"])
def test_workshop_refuses_a_full_retry_not_proven_full(workshop_on, mode):
    """An unfinished removal whose mode is unknown (an older deployment's) or
    default cannot be promoted to a forgetting one: refused, nothing checked."""
    result, preflight = _full_retry(_half_removed_catalog("cmms-ind", mode=mode))
    assert isinstance(result, HTTPException) and result.status_code == 403, result
    preflight.assert_not_called()


def test_workshop_refuses_a_forged_retry_of_an_onboarded_system(workshop_on):
    result, preflight = _full_retry(_fake_catalog("cmms-ind", "workshop"))
    assert isinstance(result, HTTPException) and result.status_code == 403, result
    preflight.assert_not_called()


def test_workshop_lets_a_user_system_retry_in_any_mode(workshop_on):
    result, preflight = _full_retry(_half_removed_catalog("cmms-ind", origin="user"))
    assert isinstance(result, AssertionError), result
    preflight.assert_called_once()


# --------------------------------------------------------------------------- #
# Reset All: a full one forgets every registration, seeded ones included.
# --------------------------------------------------------------------------- #

def _reset_all(route, full):
    """Run a global reset route with the reset-domain claim standing in for
    every later step (it raises once reached)."""
    claim = MagicMock(side_effect=AssertionError("claimed the reset domain"))
    with patch.object(onboard._RESET_DOMAIN, "claim_all", claim):
        try:
            asyncio.run(route(None, full=full))
        except (HTTPException, AssertionError) as exc:
            return exc, claim
    return None, claim


@pytest.mark.parametrize("route", ["onboard_reset", "onboard_reset_all_route"])
def test_workshop_refuses_a_full_reset_all(workshop_on, route):
    result, claim = _reset_all(getattr(onboard, route), full=True)
    assert isinstance(result, HTTPException) and result.status_code == 403, result
    assert "registration" in result.detail and "workshop" in result.detail.lower()
    claim.assert_not_called()  # refused before any lease or write


@pytest.mark.parametrize("route", ["onboard_reset", "onboard_reset_all_route"])
def test_workshop_lets_a_default_reset_all_through(workshop_on, route):
    result, claim = _reset_all(getattr(onboard, route), full=False)
    assert isinstance(result, AssertionError), result
    claim.assert_called_once()


@pytest.mark.parametrize("route", ["onboard_reset", "onboard_reset_all_route"])
def test_outside_workshop_a_full_reset_all_goes_on(workshop_off, route):
    result, claim = _reset_all(getattr(onboard, route), full=True)
    assert isinstance(result, AssertionError), result
    claim.assert_called_once()
