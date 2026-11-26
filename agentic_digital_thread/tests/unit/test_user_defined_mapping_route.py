# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the user-defined-mapping route
``POST /api/proposed-mappings/user-define`` (#4).

Lets an END USER assert a relationship the AI never proposed — a thin adapter
over ``self_learning.record_user_correction`` (the ONE accepted lifecycle,
source='user'), for a STEWARD. No AWS, no Neptune, no network: a minimal FastAPI
app carries the REAL route object from ``agents.server`` behind a tiny middleware
that stamps ``request.state.is_admin = True`` the way ``AuthMiddleware`` does for a
steward, and drives it via ``TestClient`` (mirrors the harness in
``test_steward_gate.py``). ``self_learning.record_user_correction`` is
monkeypatched; the route imports it at call time, so patching the module global
takes effect. A non-steward's assertion takes the pending door instead; that is
pinned in ``test_non_steward_mapping_writes.py``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import agents.self_learning as self_learning
import agents.schema_check as schema_check
import agents.server as server
from agents.server import user_define_mapping

_VALID_BODY = {
    "from_system": "erp",
    "from_field": "orders.customer_code",
    "from_value": "ANYCOMPANY-001",
    "to_system": "mes",
    "to_field": "runs.cust_id",
    "to_value": "C1001",
    "concept": "customer",
}


@pytest.fixture(autouse=True)
def _schema_all_valid(monkeypatch):
    """Default the semantic-validation helpers to 'everything valid' so the presence
    / lifecycle tests exercise the route past the new schema gate. Individual
    validation tests override these to drive a specific 400. Offline — no graph."""
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)


def _client():
    route = next(
        r for r in server.app.routes
        if isinstance(r, APIRoute) and r.path == "/api/proposed-mappings/user-define"
        and "POST" in r.methods)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_steward(request, call_next):  # noqa: ANN001
        request.state.is_admin = True
        return await call_next(request)

    app.router.routes.append(route)
    return TestClient(app, raise_server_exceptions=False)


def _stub_result(**overrides):
    result = {
        "status": "accepted",
        "neptune_node_id": "vm-erp-mes-customer",
        "proposed_type": "value_mapping",
        "version": 1,
    }
    result.update(overrides)
    return result


# --------------------------------------------------------------------------- #
# Happy path — calls record_user_correction with the posted values, returns id/status
# --------------------------------------------------------------------------- #
def test_valid_user_defined_mapping_recorded(monkeypatch):
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return _stub_result()

    monkeypatch.setattr(self_learning, "record_user_correction", _record)

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "accepted"
    assert payload["neptune_node_id"] == "vm-erp-mes-customer"
    assert payload["version"] == 1

    # record_user_correction called exactly once with the posted values verbatim.
    assert len(calls) == 1
    kw = calls[0]
    assert kw["from_system"] == "erp"
    assert kw["from_field"] == "orders.customer_code"
    assert kw["from_value"] == "ANYCOMPANY-001"
    assert kw["to_system"] == "mes"
    assert kw["to_field"] == "runs.cust_id"
    assert kw["to_value"] == "C1001"
    assert kw["concept"] == "customer"
    # default proposed_type when omitted.
    assert kw["proposed_type"] == "value_mapping"


def test_optional_fields_forwarded(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        self_learning, "record_user_correction",
        lambda **kw: (calls.append(kw), _stub_result(proposed_type="identity_rule"))[1],
    )

    body = {**_VALID_BODY, "description": "same customer, different code",
            "proposed_type": "identity_rule"}
    resp = _client().post("/api/proposed-mappings/user-define", json=body)
    assert resp.status_code == 200
    assert resp.json()["proposed_type"] == "identity_rule"
    assert calls[0]["description"] == "same customer, different code"
    assert calls[0]["proposed_type"] == "identity_rule"


# --------------------------------------------------------------------------- #
# Validation — a missing/blank required field ⇒ 400, no write attempted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "missing_field",
    ["from_system", "from_field", "from_value", "to_system",
     "to_field", "to_value", "concept"],
)
def test_missing_required_field_returns_400(monkeypatch, missing_field):
    record = _stub_never_called(monkeypatch)

    body = {**_VALID_BODY}
    body.pop(missing_field)
    resp = _client().post("/api/proposed-mappings/user-define", json=body)

    assert resp.status_code == 400
    assert missing_field in resp.json()["error"]
    record.assert_not_called()


def test_blank_required_field_returns_400(monkeypatch):
    record = _stub_never_called(monkeypatch)

    body = {**_VALID_BODY, "concept": "   "}
    resp = _client().post("/api/proposed-mappings/user-define", json=body)

    assert resp.status_code == 400
    assert "concept" in resp.json()["error"]
    record.assert_not_called()


# --------------------------------------------------------------------------- #
# Semantic validation (#4) — beyond presence: unknown system / missing field /
# invalid proposed_type ⇒ 400, no write attempted. Reuses schema_check helpers.
# --------------------------------------------------------------------------- #
def test_unknown_from_system_returns_400(monkeypatch):
    record = _stub_never_called(monkeypatch)
    # 'erp' unknown; every other system onboarded. Fields exist.
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda sid: sid != "erp")
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)

    assert resp.status_code == 400
    assert "from_system" in resp.json()["error"]
    assert "erp" in resp.json()["error"]
    record.assert_not_called()


def test_from_field_not_in_schema_returns_400(monkeypatch):
    record = _stub_never_called(monkeypatch)
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    # from_field missing (False), to_field present (True).
    monkeypatch.setattr(
        schema_check, "column_exists",
        lambda sid, field: False if field == "orders.customer_code" else True,
    )

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)

    assert resp.status_code == 400
    assert "from_field" in resp.json()["error"]
    record.assert_not_called()


def test_field_check_skipped_when_schema_unavailable(monkeypatch):
    """A None (schema-unavailable) column check does NOT hard-block — the write
    proceeds (only a definitive False blocks)."""
    calls: list[dict] = []
    monkeypatch.setattr(
        self_learning, "record_user_correction",
        lambda **kw: (calls.append(kw), _stub_result())[1],
    )
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: None)

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert len(calls) == 1


def test_invalid_proposed_type_returns_400(monkeypatch):
    record = _stub_never_called(monkeypatch)
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)

    body = {**_VALID_BODY, "proposed_type": "totally_bogus"}
    resp = _client().post("/api/proposed-mappings/user-define", json=body)

    assert resp.status_code == 400
    assert "proposed_type" in resp.json()["error"]
    assert "totally_bogus" in resp.json()["error"]
    record.assert_not_called()


def test_valid_system_field_type_recorded(monkeypatch):
    """Happy path with all semantic checks passing → 200 accepted, one write."""
    calls: list[dict] = []
    monkeypatch.setattr(
        self_learning, "record_user_correction",
        lambda **kw: (calls.append(kw), _stub_result(proposed_type="join_path"))[1],
    )
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda _sid, _field: True)

    body = {**_VALID_BODY, "proposed_type": "join_path"}
    resp = _client().post("/api/proposed-mappings/user-define", json=body)

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert len(calls) == 1
    assert calls[0]["proposed_type"] == "join_path"


# --------------------------------------------------------------------------- #
# Fail-loud — a write failure propagates (not swallowed as a success)
# --------------------------------------------------------------------------- #
def test_write_failure_propagates(monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(self_learning, "record_user_correction", _boom)

    # raise_server_exceptions=False ⇒ the unhandled exception surfaces as a 500
    # (the route does NOT catch it — no silent success).
    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)
    assert resp.status_code == 500


def test_write_failure_raises_when_called_directly(monkeypatch):
    from agents.models import UserDefinedMappingRequest

    monkeypatch.setattr(
        self_learning, "record_user_correction",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("neptune down")),
    )
    body = UserDefinedMappingRequest(**_VALID_BODY)
    with pytest.raises(RuntimeError, match="neptune down"):
        user_define_mapping(body, True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _stub_never_called(monkeypatch):
    from unittest.mock import Mock

    record = Mock(name="record_user_correction")
    monkeypatch.setattr(self_learning, "record_user_correction", record)
    return record


# --------------------------------------------------------------------------- #
# M1 — the repoint's fail-loud ValueError ("corrected column … not in the
# discovered schema … correction not applied") must reach the client as an
# actionable 400 {"error": ...}, not a generic 500. Both routes. Other
# exceptions still propagate (fail-loud).
# --------------------------------------------------------------------------- #
_REPOINT_MSG = ("corrected column 'runs.cust_id' is not in the discovered schema "
                "for system 'mes' — correction not applied")


def test_user_define_repoint_valueerror_returns_400_with_message(monkeypatch):
    def _record(**_kw):
        raise ValueError(_REPOINT_MSG)

    monkeypatch.setattr(self_learning, "record_user_correction", _record)

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)
    assert resp.status_code == 400
    assert resp.json() == {"error": _REPOINT_MSG}


def test_user_define_other_exceptions_still_propagate(monkeypatch):
    def _record(**_kw):
        raise RuntimeError("Neptune write failed (returned False): MERGE ...")

    monkeypatch.setattr(self_learning, "record_user_correction", _record)

    resp = _client().post("/api/proposed-mappings/user-define", json=_VALID_BODY)
    assert resp.status_code == 500  # NOT swallowed into a 400


def _accept_client():
    from agents.server import accept_proposed_mapping
    app = FastAPI()
    app.add_api_route(
        "/api/proposed-mappings/{mapping_id}/accept", accept_proposed_mapping,
        methods=["POST"],
    )
    return TestClient(app, raise_server_exceptions=False)


def test_accept_route_repoint_valueerror_returns_400_with_message(monkeypatch):
    def _accept(_mid, actor="", confirmation_mode=""):
        raise ValueError(_REPOINT_MSG)

    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    resp = _accept_client().post("/api/proposed-mappings/m-1/accept")
    assert resp.status_code == 400
    assert resp.json() == {"error": _REPOINT_MSG}


def test_accept_route_other_exceptions_still_propagate(monkeypatch):
    def _accept(_mid, actor="", confirmation_mode=""):
        raise RuntimeError("Neptune write failed (returned False): MERGE ...")

    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    resp = _accept_client().post("/api/proposed-mappings/m-1/accept")
    assert resp.status_code == 500  # NOT swallowed into a 400


def test_accept_route_not_found_stays_404(monkeypatch):
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid, actor="", confirmation_mode="": {
                            "error": f"Mapping {mid} not found"})

    resp = _accept_client().post("/api/proposed-mappings/m-404/accept")
    assert resp.status_code == 404
