# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``GET /api/admin/connection/{id}`` for a USER PostgreSQL registration before
its first provision (a live run, 2026-10-08: host/dbname/search_path null,
secret_arn ""). Offline: no AWS, no Neptune, no network.

``describe_connection`` reads the graph node only, and an unprovisioned
registration has no node. Its summary now comes from its registration
(``registration_connection``: the entry's non-secret settings + its exact own,
bound ``adt/conn/<id>`` pointer), projected like every other summary: non-secret
fields and presence indicators only, never the DSN or the password. A failed
checked node read, a pointer only the checked read sees, or an unreadable own
secret -> 500 ``{"status": "error", "detail": ...}``, no fallback. Demo and
provisioned systems are summarised from the node exactly as before.
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
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_scan_budget_routes import _build_app  # noqa: E402

PG_NODE = {"host": "db.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}
SEED_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:seed-db-AbCdEf"


def _get(system_id: str = SID):
    from fastapi.testclient import TestClient
    return TestClient(_build_app(is_admin=True)).get(f"/api/admin/connection/{system_id}")


def _no_secret_values(text: str):
    assert PASSWORD not in text and "lab_reader:" not in text and DSN not in text, text
    assert "connection_string" not in json.loads(text), text


def test_unprovisioned_user_pg_summary_comes_from_its_registration(cat, secrets, graph):
    _register_pg()
    assert SID not in graph
    res = _get()
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["host"], body["port"], body["dbname"], body["search_path"]) == \
        ("db.example.internal", "5432", "lims", "lims"), body
    assert body["secret_arn"] == CONN_ARN and body["has_secret"] is True, body
    assert body["connection_type"] == "PostgreSQL", body
    _no_secret_values(res.text)


def test_unprovisioned_user_pg_with_an_unreadable_own_secret_is_an_error(cat, secrets, graph):
    _register_pg()
    secrets.pop(CONN_ARN)
    res = _get()
    assert res.status_code == 500 and res.json()["status"] == "error", res.text
    assert "host" not in res.json()


def test_unprovisioned_user_pg_with_a_failed_graph_read_is_an_error(cat, secrets, graph,
                                                                    monkeypatch):
    _register_pg()

    def _raise(cypher, parameters=None):
        raise db.GraphQueryError("ThrottlingException", "graph unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _raise)
    res = _get()
    assert res.status_code == 500 and res.json()["status"] == "error", res.text


def test_a_node_pointer_only_the_checked_read_sees_is_an_error(cat, secrets, graph,
                                                               monkeypatch):
    _register_pg()
    monkeypatch.setattr(db, "neptune_query_checked",
                        lambda *a, **k: [{"secret_arn": CONN_ARN}])
    res = _get()
    assert res.status_code == 500 and res.json()["status"] == "error", res.text


def test_guard_a_provisioned_user_pg_system_is_summarised_from_its_node(
        cat, secrets, graph, monkeypatch):
    _register_pg()
    graph[SID] = _node_row({**PG_NODE, "host": "db2.example.internal"}, CONN_ARN, "PostgreSQL")
    monkeypatch.setattr(cr, "registration_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registration_connection used for a provisioned system")))
    res = _get()
    assert res.status_code == 200, res.text
    assert res.json()["host"] == "db2.example.internal" and res.json()["secret_arn"] == CONN_ARN
    assert res.json() == cr.describe_connection(SID)


@pytest.mark.parametrize("with_node", [True, False], ids=["provisioned", "fresh"])
def test_guard_a_demo_pg_system_is_unchanged(cat, secrets, graph, monkeypatch, with_node):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "registered", "connection_origin": "demo"})
    if with_node:
        graph["cmms"] = _node_row({**PG_NODE, "schema": "cmms_maintenance"}, SEED_ARN,
                                  "PostgreSQL")
    monkeypatch.setattr(cr, "registration_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registration_connection used for a demo system")))
    res = _get("cmms")
    assert res.status_code == 200, res.text
    assert res.json() == cr.describe_connection("cmms")
