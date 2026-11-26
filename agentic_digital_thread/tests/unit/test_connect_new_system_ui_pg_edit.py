# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The admin Edit connection form on a USER PostgreSQL registration that has
not been provisioned yet. Offline: no AWS, no Neptune, no PostgreSQL, no network.

Through the REAL ``PUT /api/admin/connection/{id}`` route, the REAL
validate-first probe (``_probe_postgres``; only ``pgdriver.connect`` is faked,
as the user's database: it accepts exactly user lab_reader, database lims, the
current password, on the current host) and the REAL ``write_connection``.

The body is what the frontend Edit form sends for a PostgreSQL system
(``payloads.obConnFields`` -> ``host``, ``port``, ``password``;
``buildEditConnectionBody`` omits a blank secret; host/port are prefilled from
the catalog entry's ``connection_config``). Before the first provision there is
no graph node, so the route's current connection must come from the
registration itself (user, database, search_path, host, port, own secret):
the assembled DSN keeps them, the probe and the write use the user's own
secret only, the node keeps the own pointer, and the deployment's env
credentials are never used.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401 - fixtures
    NEW_PW, _no_deploy_password, deployed)
from tests.unit.test_scan_budget_routes import _build_app  # noqa: E402

HOST, NEW_HOST = "db.example.internal", "db2.example.internal"


@pytest.fixture
def user_db(monkeypatch):
    """pgdriver.connect as the user's own database server(s)."""
    from agents import pgdriver
    from agents.pgdriver import parse_dsn
    state = {"password": PASSWORD, "hosts": {HOST, NEW_HOST}, "seen": []}

    class _Conn:
        def cursor(self):
            class _Cur:
                def execute(self, *a):
                    return None

                def fetchone(self):
                    return (1,)

                def close(self):
                    return None
            return _Cur()

        def close(self):
            return None

    def _connect(dsn, **kw):
        p = parse_dsn(dsn)
        state["seen"].append(p)
        if (p.get("user") != "lab_reader" or p.get("dbname") != "lims"
                or p.get("host") not in state["hosts"] or p.get("password") != state["password"]):
            raise pgdriver.OperationalError("authentication failed")
        return _Conn()
    monkeypatch.setattr(pgdriver, "connect", _connect)
    return state


@pytest.fixture
def route_env(monkeypatch, secrets):
    import agents.admin_routes as admin_routes
    import agents.connection_probes as probes
    monkeypatch.setattr(cr, "audit_connection_event", lambda *a, **k: None)
    monkeypatch.setattr(cr, "read_secret_uncached", lambda arn: dict(secrets[arn]))
    monkeypatch.setattr(admin_routes, "_reconnect_system", lambda *a, **k: True)
    # The probe's own SSRF guard import (offline: every host resolves to a
    # documentation address, like the no_dns fixture does for the resolver).
    monkeypatch.setattr(probes, "validate_connection_target",
                        lambda target, *a, **kw: "203.0.113.10")


def _ui_body(cat, *, host: str | None = None, password: str = NEW_PW) -> dict:
    """The Edit form's body: obConnFields('PostgreSQL') = host, port, password;
    host/port prefilled from the catalog entry's connection_config."""
    prefill = cat.get(SID).get("connection_config") or {}
    values = {"host": host or prefill.get("host", ""), "port": prefill.get("port", ""),
              "password": password}
    return {k: v.strip() for k, v in values.items() if v and v.strip()}


def _put(body: dict):
    from fastapi.testclient import TestClient
    return TestClient(_build_app(is_admin=True)).put(f"/api/admin/connection/{SID}", json=body)


def _assert_rotated(secrets, graph, host: str):
    stored = secrets[CONN_ARN]
    assert stored["password"] == NEW_PW and stored["user"] == "lab_reader", stored
    assert f"@{host}:5432/lims" in stored["connection_string"], stored
    assert "search_path%3Dlims" in stored["connection_string"], stored
    assert (stored["connection_origin"], stored["system_id"]) == ("user", SID), stored
    assert graph[SID]["secret_arn"] == CONN_ARN, graph[SID]
    assert NEW_PW not in json.dumps(graph[SID])
    params = db._records_conn_params_for_schema("lims")
    _no_deploy_password(params)
    assert (params["host"], params["user"], params["password"]) == (host, "lab_reader", NEW_PW)


@pytest.mark.parametrize("new_host", [None, NEW_HOST], ids=["password-only", "host+password"])
def test_ui_edit_rotates_an_unprovisioned_user_pg_registration(
        cat, secrets, graph, deployed, user_db, route_env, new_host):
    _register_pg()
    assert SID not in graph                                   # not provisioned: no node
    user_db["password"] = NEW_PW                              # the DBA rotated it already
    body = _ui_body(cat, host=new_host)
    assert set(body) == {"host", "port", "password"}, body
    res = _put(body)
    assert res.status_code == 200 and res.json().get("status") == "ok", res.text
    _assert_rotated(secrets, graph, new_host or HOST)
    # The probe connected with the registration's user + database, never env creds.
    assert all(p.get("user") == "lab_reader" and p.get("dbname") == "lims"
               for p in user_db["seen"]), user_db["seen"]


def test_ui_edit_with_a_wrong_password_writes_nothing(cat, secrets, graph, deployed,
                                                      user_db, route_env):
    _register_pg()
    before = dict(secrets[CONN_ARN])
    res = _put(_ui_body(cat, password="not-the-password"))  # nosec B106 - a fake test password
    assert res.status_code == 400 and res.json()["status"] == "validation_failed", res.text
    assert secrets[CONN_ARN] == before and SID not in graph


def test_ui_edit_with_an_unreadable_own_secret_changes_nothing(cat, secrets, graph, deployed,
                                                               user_db, route_env):
    """Before the first provision the registration is the only source of the
    user, database and search_path; unreadable -> refused, nothing written,
    nothing probed (never assembled from the deployment's env)."""
    _register_pg()
    entry_cfg = dict(cat.get(SID)["connection_config"])
    secrets.pop(CONN_ARN)
    res = _put(_ui_body(cat))
    assert res.status_code == 500 and "nothing was changed" in res.json()["detail"], res.text
    assert user_db["seen"] == [] and SID not in graph and CONN_ARN not in secrets
    assert cat.get(SID)["connection_config"] == entry_cfg


def test_guard_api_full_dsn_edit_still_rotates(cat, secrets, graph, deployed, user_db,
                                               route_env):
    _register_pg()
    user_db["password"] = NEW_PW
    new_dsn = DSN.replace(PASSWORD, NEW_PW)
    res = _put({"connection_string": new_dsn})
    assert res.status_code == 200 and res.json().get("status") == "ok", res.text
    _assert_rotated(secrets, graph, HOST)
