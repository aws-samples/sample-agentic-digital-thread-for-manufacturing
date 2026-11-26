# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The record-key PUT route stores the authenticated actor as the declarer.

Reproduced from a live rehearsal account (2026-10-19, ``s37/record-key-put.log``):
``PUT /api/admin/record-key/{system_id}`` stored the key with
``record_key_by: unknown`` even though the admin was authenticated (the demo
``ADT`` user, who has a username but no email). The audit line named the admin;
the stored record did not, because the route read only ``request.state.user.
email`` and fell back to ``"unknown"`` instead of resolving the actor the way
the audit does (``email`` else ``username``).

The fix resolves the route's actor the same way ``_request_actor`` does, so the
stored and read-back declarer equals the request's actor.

Style mirrors ``test_admin_business_context_routes.py``: the REAL router on a
bare app whose middleware stamps ``request.state.is_admin`` and
``request.state.user``; the graph write/read is an in-memory store; the pure
validators run for real. No AWS, no Neptune, no network.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
import agents.record_key as record_key
import agents.schema_fields as schema_fields
from agents import catalog as catalog_mod
from agents import connection_resolver as cr

_KNOWN = "sys-athena"
_TABLE = "segment_record_details_raw"
_COLUMNS = {"id", "segment_type", "entity_type", "revision", "cell"}


@dataclass
class _User:
    """A UserInfo-shaped stand-in; a demo admin has a username but no email."""
    email: str | None = None
    username: str | None = None


class _InjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, is_admin: bool, user: _User | None):
        super().__init__(app)
        self._is_admin, self._user = is_admin, user

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        request.state.user = self._user
        return await call_next(request)


def _build_app(*, is_admin=True, user=None) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_InjectMiddleware, is_admin=is_admin, user=user)
    return app


@pytest.fixture
def graph(monkeypatch):
    """In-memory SchemaEntry store wired through the record-key seams and
    schema_fields validators, so the route round-trips a real set then read."""
    store: dict[str, dict] = {}

    def _write(cypher, params):
        if cypher == record_key.SET_RECORD_KEY:
            store[params["seid"]] = {
                "record_key": params["record_key"],
                "record_key_by": params["by"],
                "record_key_at": params["at"],
                "record_key_source": params["source"]}

    def _query(cypher, params):
        if cypher == record_key.READ_RECORD_KEY:
            return [store.get(params["seid"], {})]
        return []

    monkeypatch.setattr(admin_routes, "_record_key_graph_seams",
                        lambda: (_write, _query))
    monkeypatch.setattr(catalog_mod.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "athena"}
                        if sid == _KNOWN else None)
    monkeypatch.setattr(schema_fields, "schema_entry_exists",
                        lambda sid, table, *, query: table == _TABLE)
    monkeypatch.setattr(schema_fields, "table_field_names",
                        lambda sid, table, *, query: set(_COLUMNS))
    monkeypatch.setattr(schema_fields, "schema_entry_id",
                        lambda sid, table: f"{sid}|{table}")
    monkeypatch.setattr(cr, "audit_connection_event", lambda *a, **k: None)
    return store


def _put(client, key):
    return client.put(f"/api/admin/record-key/{_KNOWN}",
                      json={"table": _TABLE, "record_key": key,
                            "source": "owner statement"})


def _get(client):
    return client.get(f"/api/admin/record-key/{_KNOWN}",
                      params={"table": _TABLE})


def test_declarer_is_the_username_when_the_admin_has_no_email(graph):
    """The demo admin (username, no email): the stored and read-back declarer
    is the username, never the literal 'unknown'."""
    client = TestClient(_build_app(user=_User(email=None, username="ADT")))
    assert _put(client, ["id", "revision"]).status_code == 200
    body = _get(client).json()
    assert body["record_key_by"] == "ADT"
    assert body["record_key_by"] != "unknown"


def test_declarer_is_the_email_when_present(graph):
    """When the actor has an email it is used, matching _request_actor."""
    client = TestClient(_build_app(user=_User(email="dana@example.com",
                                              username="dana")))
    assert _put(client, ["id"]).status_code == 200
    assert _get(client).json()["record_key_by"] == "dana@example.com"
