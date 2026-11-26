# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The steward route for a table's ROW EXCLUSION: set, read, clear; admin only,
Athena only, audited, every malformed body a 422 with a reason.

Style mirrors ``test_record_key_route_actor.py``: the REAL router on a bare app
whose middleware stamps ``request.state.is_admin`` and ``request.state.user``;
the graph write/read is an in-memory store; the validators run for real. No
AWS, no graph, no network. Synthetic names.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
import agents.row_exclusion as row_exclusion
import agents.row_exclusion_manifest as row_exclusion_manifest
import agents.schema_fields as schema_fields
from agents import catalog as catalog_mod
from agents import connection_resolver as cr

_ATHENA = "sys-lake"
_PG = "sys-pg"
_TABLE = "job_events"
_TYPES = {"pipeline": "varchar", "run_ref": "varchar", "step_kind": "varchar",
          "qty": "integer"}
_BODY = {"table": _TABLE, "label": "shadow copies",
         "conditions": [{"column": "pipeline", "op": "not_empty"},
                        {"column": "run_ref", "op": "not_empty"},
                        {"column": "step_kind", "op": "not_in", "values": ["ASSEMBLE", "PACK"]}],
         "source": "owner statement, 2026-10-15"}


@dataclass
class _User:
    email: str | None = None
    username: str | None = None


class _Inject(BaseHTTPMiddleware):
    def __init__(self, app, *, is_admin: bool, user):
        super().__init__(app)
        self._is_admin, self._user = is_admin, user

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        request.state.user = self._user
        return await call_next(request)


def _client(*, is_admin=True, user=None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_Inject, is_admin=is_admin, user=user or _User(username="ADT"))
    return TestClient(app)


@pytest.fixture
def graph(monkeypatch):
    store: dict[str, dict] = {}
    audits: list = []

    manifest = {"value": None}

    def _write(cypher, params):
        if cypher == row_exclusion_manifest.SWAP_MANIFEST:
            if (manifest["value"] or "") != params["expected"]:
                return []
            manifest["value"] = params["manifest"]
            return [{"id": params["sid"]}]
        if cypher == row_exclusion.SET_ROW_EXCLUSION:
            if params["row_exclusion"]:
                store[params["seid"]] = {"row_exclusion": params["row_exclusion"],
                                         "row_exclusion_by": params["by"],
                                         "row_exclusion_at": params["at"],
                                         "row_exclusion_source": params["source"]}
            else:
                store.pop(params["seid"], None)
            return [{"seid": params["seid"]}]
        return None

    def _query(cypher, params):
        if cypher == row_exclusion_manifest.READ_MANIFEST:
            return [{"row_exclusion_manifest": manifest["value"]}]
        if cypher == row_exclusion.READ_ROW_EXCLUSION:
            return [store.get(params["seid"], {})]
        if cypher == row_exclusion.FIELD_TYPES:
            return [{"name": n, "data_type": t} for n, t in _TYPES.items()]
        return []

    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams", lambda: (_write, _query))
    monkeypatch.setattr(catalog_mod.catalog, "get", lambda sid: {
        _ATHENA: {"id": _ATHENA, "connection_type": "Athena"},
        _PG: {"id": _PG, "connection_type": "PostgreSQL"}}.get(sid))
    monkeypatch.setattr(schema_fields, "schema_entry_exists",
                        lambda sid, table, *, query: table == _TABLE)
    monkeypatch.setattr(cr, "audit_connection_event",
                        lambda *a, **k: audits.append((a, k)))
    store["_audits"] = audits
    store["_manifest"] = manifest
    return store


def _put(client, body, system=_ATHENA):
    return client.put(f"/api/admin/row-exclusion/{system}", json=body)


def _get(client, system=_ATHENA):
    return client.get(f"/api/admin/row-exclusion/{system}", params={"table": _TABLE})


def test_set_read_and_clear_round_trip_with_provenance(graph):
    client = _client(user=_User(email="dana@example.com", username="dana"))
    put = _put(client, _BODY)
    assert put.status_code == 200, put.text
    assert put.json()["predicate"].startswith("NOT (COALESCE(pipeline, '') <> ''")
    body = _get(client).json()
    assert body["label"] == "shadow copies"
    assert body["conditions"] == _BODY["conditions"]
    assert body["row_exclusion_by"] == "dana@example.com"
    assert body["row_exclusion_source"] == "owner statement, 2026-10-15"
    assert body["row_exclusion_at"]
    (args, kwargs), = graph["_audits"]
    assert args[:2] == ("row_exclusion", _ATHENA) and kwargs["outcome"] == "ok"
    cleared = _put(client, {"table": _TABLE, "conditions": []})
    assert cleared.status_code == 200
    assert _get(client).json()["conditions"] == []
    assert len(graph["_audits"]) == 2


def test_a_non_admin_is_refused(graph):
    client = _client(is_admin=False)
    assert _put(client, _BODY).status_code == 403
    assert _get(client).status_code == 403


def test_an_unknown_system_is_404(graph):
    assert _put(_client(), _BODY, system="nope").status_code == 404


def test_a_non_athena_system_is_refused(graph):
    out = _put(_client(), _BODY, system=_PG)
    assert out.status_code == 400 and out.json()["reason"] == "unsupported_backend"


@pytest.mark.parametrize("body,reason", [
    ({**_BODY, "surprise": 1}, "unknown_keys"),
    ({**_BODY, "table": 5}, "invalid_input"),
    ({**_BODY, "conditions": "pipeline"}, "invalid_input"),
    ({**_BODY, "label": 3}, "invalid_input"),
    ({**_BODY, "source": 9}, "invalid_input"),
    ({**_BODY, "conditions": [{"column": "pipeline", "op": "like"}]}, "invalid_op"),
    ({**_BODY, "conditions": [{"column": "qty", "op": "empty"}]}, "column_not_string"),
    ({**_BODY, "conditions": [{"column": "nope", "op": "empty"}]}, "unknown_column"),
    ({**_BODY, "conditions": [{"column": "step_kind", "op": "in", "values": ["A'B"]}]},
     "invalid_value"),
    ({**_BODY, "conditions": [{"column": f"c{i}", "op": "empty"} for i in range(9)]},
     "too_many_conditions"),
    ({**_BODY, "label": "it's"}, "invalid_label"),
    ({**_BODY, "table": "ghost"}, "unknown_table"),
])
def test_every_malformed_body_is_a_422_with_a_reason(graph, body, reason):
    out = _put(_client(), body)
    assert out.status_code == 422, out.text
    assert out.json()["detail"]["reason"] == reason
    assert not [k for k in graph if k not in {"_audits", "_manifest"}]
    assert graph["_manifest"]["value"] is None


def test_an_oversized_body_is_413(graph):
    big = {**_BODY, "source": "x" * (admin_routes.MAX_RECORD_KEY_BYTES + 10)}
    assert _put(_client(), big).status_code == 413


def test_set_lists_the_table_in_the_manifest_and_clear_removes_it(graph):
    client = _client()
    assert _put(client, _BODY).status_code == 200
    listed = row_exclusion_manifest.parse(graph["_manifest"]["value"])
    assert set(listed) == {_TABLE} and listed[_TABLE].pending is False
    assert _put(client, {"table": _TABLE, "conditions": []}).status_code == 200
    assert graph["_manifest"]["value"] is None


def test_a_write_that_does_not_land_is_a_500_and_leaves_the_table_listed(graph, monkeypatch):
    write, query = admin_routes._row_exclusion_graph_seams()

    def refuse_the_fact(cypher, params):
        return None if cypher == row_exclusion.SET_ROW_EXCLUSION else write(cypher, params)
    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams",
                        lambda: (refuse_the_fact, query))
    out = _put(_client(), _BODY)
    assert out.status_code == 500
    assert _TABLE not in graph
    assert row_exclusion_manifest.parse(graph["_manifest"]["value"])[_TABLE].pending is True
    assert not graph["_audits"]
