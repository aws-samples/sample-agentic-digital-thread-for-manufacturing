# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An Edit of a user PostgreSQL node that holds settings but no credentials pointer.

A half-written or legacy System node can carry a ``connection_config`` with an
empty ``secret_arn``. The admin Edit then built its validate-first candidate
from the node's credential-free settings (no user), the probe failed and the
form showed ``auth_failed``: a wrong reason, and nothing the admin could do
from the form would fix it. The Edit now refuses up front with the actual
reason and the repair, writes nothing and reads no credentials (the catalog
entry's registration pointer is not borrowed to fill the gap). Offline.
"""
from __future__ import annotations

import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import NEW_PW, deployed  # noqa: E402,F401
from tests.unit.test_connect_new_system_ui_pg_edit import (  # noqa: E402,F401 - fixtures
    _put, _ui_body, route_env, user_db)

PG_NODE = {"host": "db.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}


def _cfg_without_pointer(cat, graph):
    _register_pg()
    graph[SID] = _node_row(PG_NODE, "", "PostgreSQL")


def test_an_edit_of_a_node_without_a_pointer_says_why_and_changes_nothing(
        cat, secrets, graph, deployed, user_db, route_env):
    _cfg_without_pointer(cat, graph)
    user_db["password"] = NEW_PW
    before_secret, before_node = dict(secrets[CONN_ARN]), dict(graph[SID])
    res = _put(_ui_body(cat))
    body = res.json()
    assert res.status_code == 400, res.text
    assert body.get("reason") == "credentials_not_linked", body
    assert "auth" not in body.get("reason", ""), body
    assert "onboard it again" in body["detail"] and "nothing was changed" in body["detail"], body
    assert secrets[CONN_ARN] == before_secret and graph[SID] == before_node
    assert user_db["seen"] == [], "no probe: the refusal comes before any connection attempt"


def test_guard_a_provisioned_user_pg_node_with_its_pointer_still_edits(
        cat, secrets, graph, deployed, user_db, route_env):
    _register_pg()
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    user_db["password"] = NEW_PW
    res = _put(_ui_body(cat))
    assert res.status_code == 200, res.text
    assert secrets[CONN_ARN]["password"] == NEW_PW


def test_guard_an_unprovisioned_user_pg_registration_still_edits(
        cat, secrets, graph, deployed, user_db, route_env):
    _register_pg()
    user_db["password"] = NEW_PW
    res = _put(_ui_body(cat))
    assert res.status_code == 200, res.text
