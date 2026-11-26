# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A demo PostgreSQL edit that names another host, or a password, is refused up front.

A demo / workshop / legacy PostgreSQL system reads its credentials from the
deployment (its node points at a shared database secret, or at none and the
env fills them). Those credentials only ever go to the deployment's own
database host. An admin Edit that moved such a system to another host was
accepted when the shared secret could not be read at Edit time (the advisory
"also stored in the secret" check never blocks on an unreadable secret): the
new host was stored, the edit's password was dropped (it rides inline and is
stripped at rest), and the system could then never restore or sample. Now the
Edit is refused before anything is probed or written, with what to do. A port
change on the deployment host is unaffected, and a user registration's own
credentials edit is unaffected. Offline.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    DEPLOYED, SEED_ARN, _admin_update, deployed)

DEMO_NODE = {"host": DEPLOYED["POSTGRES_HOST"], "port": "5432", "dbname": "adt_demo",
             "schema": "cmms_maintenance", "search_path": "cmms_maintenance"}


def _demo_pg(cat, graph, arn: str = SEED_ARN):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "onboarded", "connection_origin": "demo"})
    graph["cmms"] = _node_row(DEMO_NODE, arn, "PostgreSQL")


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


@pytest.mark.parametrize("arn", [SEED_ARN, ""], ids=["shared-secret-unreadable", "env-only"])
@pytest.mark.parametrize("edit", [
    {"host": "db.elsewhere.example", "port": "5432"},
    {"host": "db.elsewhere.example", "port": "5432", "password": "new-pw"},
    {"connection_string": "postgresql://postgres:pw@db.elsewhere.example:5432/adt_demo"},
    {"connection_string": f"postgresql://postgres@{DEPLOYED['POSTGRES_HOST']}:5432/other_db"},
], ids=["host", "host+password", "full-dsn", "other-database"])
def test_a_demo_edit_the_deployment_credentials_cannot_serve_is_refused(
        cat, secrets, graph, deployed, arn, edit):
    _demo_pg(cat, graph, arn)
    before = dict(graph["cmms"])
    res = _admin_update("cmms", edit)
    body = _body(res)
    assert getattr(res, "status_code", None) == 400, body
    assert body["reason"] == "deployment_credentials", body
    assert "nothing was changed" in body["detail"] and "Connect New System" in body["detail"]
    assert graph["cmms"] == before and secrets == {}


def test_guard_a_demo_password_only_edit_is_still_refused_and_writes_nothing(
        cat, secrets, graph, deployed):
    _demo_pg(cat, graph)
    before = dict(graph["cmms"])
    res = _admin_update("cmms", {"password": "new-pw"})
    assert getattr(res, "status_code", None) == 400, _body(res)
    assert graph["cmms"] == before and secrets == {}


def test_guard_a_port_change_on_the_deployment_host_still_saves(cat, secrets, graph, deployed):
    _demo_pg(cat, graph)
    res = _admin_update("cmms", {"host": DEPLOYED["POSTGRES_HOST"].upper(), "port": "6543"})
    assert _body(res).get("status") == "ok", _body(res)
    assert json.loads(graph["cmms"]["connection_config"])["port"] == "6543"


def test_guard_a_user_registrations_host_and_password_edit_still_saves(
        cat, secrets, graph, deployed):
    from tests.unit.test_connect_new_system_custom_types import CONN_ARN, SID, _register_pg
    _register_pg()
    dsn = ("postgresql://lab_reader:new-pw@db2.example.internal:5432/lims"
           "?options=-csearch_path%3Dlims")
    res = _admin_update(SID, {"connection_string": dsn})
    assert _body(res).get("status") == "ok", _body(res)
    assert secrets[CONN_ARN]["password"] == "new-pw"
