# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the correction route ``POST /api/metrics/correct-mapping``
(S3.2 / W2.4).

No AWS, no graph, no network. Two complementary styles:

  • Direct route-coroutine calls (``asyncio.run``) drive the handler logic with
    ``record_user_correction`` / ``rederive.enqueue`` / ``rederive.affected_cards``
    stubbed as module globals — mirrors the ``resolve_metric_route`` pattern in
    ``test_dashboard_cards.py``.
  • A minimal FastAPI app + ``AuthMiddleware`` + ``TestClient`` proves the route
    inherits the app-wide auth exactly like the other ``/api/metrics/*`` routes
    (401 unauthenticated; passes under the complete local-auth fixture).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.metric_resolver as mr
from agents.auth.middleware import AuthMiddleware
from agents.config import get_config
from agents.metric_resolver import CorrectMappingRequest, correct_mapping_route


#: A request carrying the steward verdict ``AuthMiddleware`` stamps; these
#: tests pin the accepted-on-entry door a steward keeps (the non-steward
#: pending door is pinned in ``test_non_steward_mapping_writes.py``).
_STEWARD = SimpleNamespace(state=SimpleNamespace(is_admin=True))


def _stub_record(**_overrides):
    """A record_user_correction stub returning the S3.1 result dict shape."""
    result = {
        "status": "accepted",
        "neptune_node_id": "mapsto-cmms-mro-inventory",
        "proposed_type": "concept_mapping",
        "version": 2,
    }
    result.update(_overrides)
    return Mock(name="record_user_correction", return_value=result)


def _install_route_stubs(monkeypatch, *, record=None, affected=("oee",)):
    """Stub the three module globals the route touches; return (record, enqueue)."""
    record = record or _stub_record()
    enqueue = Mock(name="rederive.enqueue")
    monkeypatch.setattr(mr, "record_user_correction", record)
    monkeypatch.setattr(mr.rederive, "enqueue", enqueue)
    monkeypatch.setattr(mr.rederive, "affected_cards", lambda concept: list(affected))
    return record, enqueue


# --------------------------------------------------------------------------- #
# Direct route-coroutine behaviour
# --------------------------------------------------------------------------- #
def test_valid_correction_accepted_with_affected_cards(monkeypatch):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    record, enqueue = _install_route_stubs(monkeypatch, affected=("oee",))

    body = CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand",
    )
    result = asyncio.run(correct_mapping_route(body, _STEWARD))

    assert result["status"] == "accepted"
    assert result["version"] == 2
    assert result["neptune_node_id"] == "mapsto-cmms-mro-inventory"
    assert result["concept"] == "mro-inventory"
    assert result["affected_cards"] == ["oee"]

    # record_user_correction called once with the concept_mapping mapping.
    assert record.call_count == 1
    kwargs = record.call_args.kwargs
    assert kwargs["proposed_type"] == "concept_mapping"
    assert kwargs["to_system"] == "cmms"
    assert kwargs["concept"] == "mro-inventory"
    assert kwargs["to_field"] == "parts.qty_on_hand"

    # Flag on ⇒ ripple enqueued exactly once with the corrected concept.
    assert enqueue.call_count == 1
    assert enqueue.call_args.args[0] == "mro-inventory"


def test_flag_off_does_not_enqueue_but_still_accepts(monkeypatch):
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    record, enqueue = _install_route_stubs(monkeypatch)

    body = CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand",
    )
    result = asyncio.run(correct_mapping_route(body, _STEWARD))

    assert result["status"] == "accepted"
    assert record.call_count == 1          # the write half is NOT gated
    enqueue.assert_not_called()            # the ripple IS gated (flag off)


@pytest.mark.parametrize("bad_to_field", ["parts", "no_dot_here", ".col", "tbl.", "."])
def test_to_field_must_name_a_table_and_a_column(monkeypatch, bad_to_field):
    record, enqueue = _install_route_stubs(monkeypatch)

    body = CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field=bad_to_field,
    )
    result = asyncio.run(correct_mapping_route(body, _STEWARD))

    # Fail-fast validation: error returned, no correction recorded, no ripple.
    assert "error" in result
    assert "to_field" in result["error"]
    record.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.parametrize("nested", ["table_x.struct_a.leaf",
                                    "table_x.struct_a.inner_b.leaf"])
def test_nested_column_to_field_reaches_the_correction_unchanged(monkeypatch, nested):
    """A flattened nested column is ONE column name with dots in it: the table
    is the first segment, everything after the first dot is the column."""
    record, enqueue = _install_route_stubs(monkeypatch)

    body = CorrectMappingRequest(system_id="system-a", concept="concept-a",
                                 to_field=nested)
    result = asyncio.run(correct_mapping_route(body, _STEWARD))

    assert result["status"] == "accepted"
    assert record.call_count == 1
    assert record.call_args.kwargs["to_field"] == nested


def test_record_failure_surfaces_as_error_dict(monkeypatch):
    monkeypatch.setenv("RESOLVER_REDERIVE", "1")
    boom = Mock(name="record_user_correction", side_effect=RuntimeError("neptune down"))
    _record, enqueue = _install_route_stubs(monkeypatch, record=boom)

    body = CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand",
    )
    result = asyncio.run(correct_mapping_route(body, _STEWARD))

    assert "error" in result
    assert "Reference: " in result["error"] and "neptune down" not in result["error"]
    # A failed write must NOT ripple.
    enqueue.assert_not_called()


# --------------------------------------------------------------------------- #
# AuthMiddleware coverage via TestClient (mirrors the app-wide auth)
# --------------------------------------------------------------------------- #
def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(mr.router)
    return app


_VALID_BODY = {
    "system_id": "cmms", "concept": "mro-inventory", "to_field": "parts.qty_on_hand",
}


def test_correct_mapping_requires_auth(monkeypatch):
    # No Cognito configured and DEV_NO_AUTH unset ⇒ the /api/ route is rejected
    # 401 exactly like the other /api/metrics/* routes.
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    get_config.cache_clear()
    client = TestClient(_build_app())

    resp = client.post("/api/metrics/correct-mapping", json=_VALID_BODY)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_correct_mapping_passes_under_dev_no_auth(monkeypatch, local_auth_env):
    # The complete local-auth fixture lets the request through to the handler,
    # which returns the accepted dict (stubs prevent any AWS contact).
    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    get_config.cache_clear()
    _install_route_stubs(monkeypatch, affected=("oee",))

    client = TestClient(_build_app())
    resp = client.post("/api/metrics/correct-mapping", json=_VALID_BODY)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "accepted"
    assert payload["affected_cards"] == ["oee"]


def test_correct_mapping_validation_error_still_authed(monkeypatch, local_auth_env):
    # Auth passes (DEV_NO_AUTH), but a malformed to_field fails fast with the
    # plain-dict error (still HTTP 200 per the resolver's error style).
    get_config.cache_clear()
    record, _enqueue = _install_route_stubs(monkeypatch)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/metrics/correct-mapping",
        json={"system_id": "cmms", "concept": "mro-inventory", "to_field": "no_dot"},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()
    record.assert_not_called()


# --------------------------------------------------------------------------- #
# The metric family's offload lane
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("request_obj", [_STEWARD, SimpleNamespace(state=SimpleNamespace())],
                         ids=["steward", "absent-verdict"])
def test_correction_runs_on_the_resolver_lane(monkeypatch, request_obj):
    """agents/offload.py: the metric-resolver family runs on ``offload_resolver``
    (its own 4-thread pool), not the general lane. Both doors are pinned."""
    import threading
    seen: list[str] = []

    def _door(**_kw):
        seen.append(threading.current_thread().name)
        return {"status": "accepted", "neptune_node_id": "n", "version": 1,
                "mapping_id": "m", "proposed_type": "concept_mapping"}

    monkeypatch.delenv("RESOLVER_REDERIVE", raising=False)
    monkeypatch.setattr(mr, "record_user_correction", _door)
    monkeypatch.setattr(mr, "record_user_proposal", _door)
    monkeypatch.setattr(mr.rederive, "affected_cards", lambda concept: [])
    import agents.schema_check as schema_check
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)

    body = CorrectMappingRequest(
        system_id="cmms", concept="mro-inventory", to_field="parts.qty_on_hand")
    asyncio.run(correct_mapping_route(body, request_obj))

    assert len(seen) == 1 and seen[0].startswith("adt-offload-resolver"), seen
