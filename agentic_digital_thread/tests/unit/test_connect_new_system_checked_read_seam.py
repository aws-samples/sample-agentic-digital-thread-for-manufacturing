# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: the checked node reads run for real in the unit lane.
Offline: no AWS, no Neptune, no network.

* the unit lane fakes the graph at the I/O boundary
  (``db.neptune_query_checked``) for the checked node-pointer read, so the REAL
  ``connection_resolver.node_secret_arn_checked`` runs in every test: a broken
  query alias or key would now fail the PostgreSQL origin tests instead of
  silently bringing back the demo path.
"""
from __future__ import annotations

import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import deployed  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_locked_budget_and_provision import _pg_ctx, pg_runtime  # noqa: E402,F401
from tests.unit.test_connect_new_system_pg_origin_after_restart import (  # noqa: E402,F401 - fixtures
    _restarted_user_pg, no_demo_secret)

#: The real function, captured at import (before any fixture runs).
_REAL_CHECKED = cr.node_secret_arn_checked
PINNED_IP = "203.0.113.10"


# ── the real checked read runs; only the graph I/O is faked ────────

def test_l1_the_offline_seam_leaves_the_real_checked_read_in_place():
    assert cr.node_secret_arn_checked is _REAL_CHECKED


def test_l1_the_real_checked_read_returns_the_node_pointer(monkeypatch):
    seen: list = []

    def _graph(cypher, parameters=None):
        seen.append((cypher, dict(parameters or {})))
        return [{"secret_arn": CONN_ARN}]
    monkeypatch.setattr(db, "neptune_query_checked", _graph)
    assert _REAL_CHECKED(SID) == CONN_ARN
    assert seen == [("MATCH (s:System {id: $sid}) RETURN s.secret_arn AS secret_arn",
                     {"sid": SID})], seen


def test_l1_the_real_checked_read_with_no_node_is_empty(monkeypatch):
    monkeypatch.setattr(db, "neptune_query_checked", lambda *a, **k: [])
    assert _REAL_CHECKED(SID) == ""


def test_l1_end_to_end_user_path_with_the_real_checked_read(
        cat, secrets, graph, deployed, no_demo_secret, pg_runtime, monkeypatch):
    """The restart case of test_connect_new_system_pg_origin_after_restart.py
    (unmarked entry, node -> own bound secret) with the
    REAL function in place: discover pinned, connection phase keeps the own
    pointer, start uses the user's database, the entry ends up marked user."""
    monkeypatch.setattr(cr, "node_secret_arn_checked", _REAL_CHECKED)
    _restarted_user_pg(cat, graph)
    assert onboard._pg_discover_target(SID, "", "lims") == (DSN, PINNED_IP, "")
    assert cat.get(SID).get("connection_origin") == "user"
    ctx = _pg_ctx()
    assert onboard._dp_connection_phase(ctx) is None
    assert graph[SID]["secret_arn"] == CONN_ARN
    assert onboard._dp_start_pg_agent(_pg_ctx()) is None
    params = pg_runtime.started[-1]["conn_params"]
    assert (params["host"], params["password"]) == ("db.example.internal", PASSWORD), params


# ── "unprovisioned" is decided by a CHECKED node read ──────────────

import pytest  # noqa: E402

from agents.db import GraphQueryError  # noqa: E402
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_env_credential_fill import NEW_PW  # noqa: E402
from tests.unit.test_connect_new_system_ui_pg_edit import (  # noqa: E402,F401 - fixtures
    _put, _ui_body, route_env, user_db)

PG_NODE = {"host": "db.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}


def _graph_unavailable(monkeypatch, checked=None):
    """Unchecked reads see what neptune_query returns on an error ([] -> no
    node); checked reads raise, unless ``checked`` answers them."""
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)

    def _raise(cypher, parameters=None):
        raise GraphQueryError("ThrottlingException", "graph unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", checked or _raise)


def test_l2_a_provisioned_user_pg_edit_during_a_graph_outage_changes_nothing(
        cat, secrets, graph, deployed, user_db, route_env, monkeypatch):
    _register_pg()                                    # the entry keeps its own pointer
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    user_db["password"] = NEW_PW
    before = dict(secrets[CONN_ARN])
    _graph_unavailable(monkeypatch)
    res = _put(_ui_body(cat))
    assert res.status_code == 500 and "nothing was changed" in res.json()["detail"], res.text
    assert secrets[CONN_ARN] == before and user_db["seen"] == []


def test_l2_a_node_pointer_seen_only_by_the_checked_read_changes_nothing(
        cat, secrets, graph, deployed, user_db, route_env, monkeypatch):
    """The unchecked read missed the node but the checked read finds its
    pointer: the reads disagree, so the Edit refuses rather than guess."""
    _register_pg()
    user_db["password"] = NEW_PW
    before = dict(secrets[CONN_ARN])
    _graph_unavailable(monkeypatch, checked=lambda *a, **k: [{"secret_arn": CONN_ARN}])
    res = _put(_ui_body(cat))
    assert res.status_code == 500, res.text
    assert secrets[CONN_ARN] == before and user_db["seen"] == []


# ── the refusal's audit names the admin who made the Edit ──────────

def _app_with_user(email: str):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    import types as _types
    import agents.admin_routes as admin_routes

    class _Who(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.is_admin = True
            request.state.user = _types.SimpleNamespace(email=email)
            return await call_next(request)
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_Who)
    return app


@pytest.mark.parametrize("graph_down", [False, True], ids=["unreadable-secret", "graph-down"])
def test_l3_the_refusal_audit_records_the_actor(cat, secrets, graph, deployed, user_db,
                                                route_env, monkeypatch, graph_down):
    from fastapi.testclient import TestClient
    _register_pg()
    if graph_down:
        _graph_unavailable(monkeypatch)
    else:
        secrets.pop(CONN_ARN)
    events: list = []
    monkeypatch.setattr(cr, "audit_connection_event",
                        lambda *a, **k: events.append({"args": a, **k}))
    res = TestClient(_app_with_user("admin@example.com")).put(
        f"/api/admin/connection/{SID}", json=_ui_body(cat))
    assert res.status_code == 500, res.text
    assert [(e.get("actor"), e.get("outcome")) for e in events] == \
        [("admin@example.com", "error")], events
