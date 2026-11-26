# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user PostgreSQL credentials edit that names another schema.

A full-DSN Edit (the API shape) of a user PostgreSQL registration whose
search_path named a different schema was refused as the generic
``blocked_target``. Before the first provision nothing describes the old
schema yet, so the Edit now moves the registration to the new schema with the
checks registration uses: the full registration DSN, the target guard, and the
duplicate-schema claim (atomic in the catalog; another system's schema is
refused and nothing changes). Records-first sampling then routes the new
schema to the registration's own credentials. Once provisioned, the graph and
the running agent describe the old schema's tables, so the Edit is refused with
that reason (remove the system and register it again). Offline.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.db as db  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    NEW_PW, _admin_update, _no_deploy_password, deployed)

NEW_SCHEMA_DSN = (f"postgresql://lab_reader:{NEW_PW}@db.example.internal:5432/lims"
                  "?options=-csearch_path%3Dlims_v2")
PG_NODE = {"host": "db.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


def test_an_unprovisioned_registration_moves_to_a_free_schema(cat, secrets, graph, deployed):
    _register_pg()
    res = _admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})
    assert _body(res).get("status") == "ok", _body(res)
    assert cat.get(SID)["schema"] == "lims_v2"
    stored = secrets[CONN_ARN]
    assert "search_path%3Dlims_v2" in stored["connection_string"] and stored["password"] == NEW_PW
    assert (stored["connection_origin"], stored["system_id"]) == ("user", SID)
    params = db._records_conn_params_for_schema("lims_v2")
    _no_deploy_password(params)
    assert (params["user"], params["password"]) == ("lab_reader", NEW_PW), params
    assert db._records_conn_params_for_schema("lims") is None    # the old schema is free


def test_a_schema_another_system_holds_is_refused_and_nothing_changes(
        cat, secrets, graph, deployed):
    _register_pg()
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "lims_v2", "status": "registered", "connection_origin": "demo"})
    before = dict(secrets[CONN_ARN])
    res = _admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})
    body = _body(res)
    assert getattr(res, "status_code", None) == 400, body
    assert body["reason"] == "duplicate_schema", body
    assert "already registered by system cmms" in body["detail"], body
    assert cat.get(SID)["schema"] == "lims" and secrets[CONN_ARN] == before
    assert SID not in graph


def test_a_provisioned_system_cannot_change_schema_and_says_why(cat, secrets, graph, deployed):
    _register_pg()
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    cat.update_status(SID, "onboarded")
    before = dict(secrets[CONN_ARN])
    res = _admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})
    body = _body(res)
    assert getattr(res, "status_code", None) == 400, body
    assert body["reason"] == "schema_change_needs_reonboard", body
    assert "register it again" in body["detail"], body
    assert cat.get(SID)["schema"] == "lims" and secrets[CONN_ARN] == before


def test_two_schema_changes_before_onboarding_both_apply(cat, secrets, graph, deployed):
    """The first Edit writes the node's connection (as every pre-provision Edit
    does); the system is still not onboarded, so a second change applies too."""
    _register_pg()
    assert _body(_admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})).get("status") == "ok"
    assert SID in graph
    third = NEW_SCHEMA_DSN.replace("lims_v2", "lims_v3")
    assert _body(_admin_update(SID, {"connection_string": third})).get("status") == "ok"
    assert cat.get(SID)["schema"] == "lims_v3"
    assert json.loads(graph[SID]["connection_config"])["search_path"] == "lims_v3"


def test_a_failed_probe_rolls_the_schema_claim_back(cat, secrets, graph, deployed):
    from unittest.mock import patch
    import types
    import agents.admin_routes as admin_routes
    import agents.connection_resolver as cr
    _register_pg()
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    with patch.object(cr, "probe_connection", return_value={"status": "fail",
                                                            "reason": "auth_failed"}):
        res = admin_routes._do_update_connection(SID, request,
                                                 {"connection_string": NEW_SCHEMA_DSN})
    assert getattr(res, "status_code", None) == 400
    assert cat.get(SID)["schema"] == "lims", "the claim was not rolled back"


def test_guard_the_same_schema_edit_is_unchanged(cat, secrets, graph, deployed):
    _register_pg()
    dsn = NEW_SCHEMA_DSN.replace("lims_v2", "lims")
    res = _admin_update(SID, {"connection_string": dsn})
    assert _body(res).get("status") == "ok", _body(res)
    assert cat.get(SID)["schema"] == "lims"
