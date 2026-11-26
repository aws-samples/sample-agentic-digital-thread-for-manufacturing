# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: "no node" and "a node with no pointer" were one answer.

``node_secret_arn_checked`` returns ``""`` both when there is no System node and
when a node exists with a ``connection_config`` but an empty ``secret_arn`` (a
half-written or legacy node). The admin connection view and the Edit's
"not yet provisioned" guard read ``""`` as "no node" and took the REGISTRATION
path, so the view showed the node's host beside the registration's pointer and
``has_secret: true``, a summary of a connection that exists nowhere.

Now a CHECKED read (``node_state_checked``) says whether the node holds a
pointer and whether it holds a config. Any node state is summarised from the
node, exactly as a provisioned system is; a checked read that sees a node the
unchecked read missed is an error, never a guess. Offline: no AWS, no graph.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_describe_unprovisioned import _get  # noqa: E402
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import NEW_PW, deployed  # noqa: E402,F401
from tests.unit.test_connect_new_system_checked_read_seam import _graph_unavailable  # noqa: E402
from tests.unit.test_connect_new_system_ui_pg_edit import (  # noqa: E402,F401 - fixtures
    _put, _ui_body, route_env, user_db)

PG_NODE = {"host": "db2.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}
STATE_QUERY = ("MATCH (s:System {id: $sid}) "
               "RETURN s.secret_arn AS secret_arn, s.connection_config AS connection_config")


def _no_registration(monkeypatch):
    monkeypatch.setattr(cr, "registration_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registration_connection used for a system that has a node")))


# ── the checked node-state read ────────────────────────────────────────────

def test_the_checked_node_state_read_reports_pointer_and_config(monkeypatch):
    seen: list = []

    def _graph(cypher, parameters=None):
        seen.append((cypher, dict(parameters or {})))
        return [{"secret_arn": None, "connection_config": json.dumps(PG_NODE)}]
    monkeypatch.setattr(db, "neptune_query_checked", _graph)
    assert cr.node_state_checked(SID) == {"exists": True, "secret_arn": "", "has_config": True}
    assert seen == [(STATE_QUERY, {"sid": SID})], seen


def test_the_checked_node_state_read_with_no_node(monkeypatch):
    monkeypatch.setattr(db, "neptune_query_checked", lambda *a, **k: [])
    assert cr.node_state_checked(SID) == {"exists": False, "secret_arn": "", "has_config": False}


def test_the_checked_node_state_read_raises_on_a_graph_error(monkeypatch):
    def _raise(cypher, parameters=None):
        raise db.GraphQueryError("ThrottlingException", "graph unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _raise)
    try:
        cr.node_state_checked(SID)
    except db.GraphQueryError:
        return
    raise AssertionError("a graph error must raise, not read as 'no node'")


# ── admin connection view ──────────────────────────────────────────────────

def test_a_user_pg_node_with_config_and_no_pointer_is_summarised_from_the_node(
        cat, secrets, graph, monkeypatch):
    _register_pg()                                    # the entry holds its own pointer
    graph[SID] = _node_row(PG_NODE, "", "PostgreSQL")  # the node holds none
    _no_registration(monkeypatch)
    res = _get()
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["host"] == "db2.example.internal", body
    assert body["secret_arn"] == "" and body["has_secret"] is False, body
    assert body == cr.describe_connection(SID)


def test_a_node_config_only_the_checked_read_sees_is_an_error(cat, secrets, graph, monkeypatch):
    _register_pg()
    _graph_unavailable(monkeypatch, checked=lambda *a, **k: [
        {"secret_arn": None, "connection_config": json.dumps(PG_NODE)}])
    res = _get()
    assert res.status_code == 500 and res.json()["status"] == "error", res.text
    assert "host" not in res.json()


# ── Edit: "not yet provisioned" needs no node at all ───────────────────────

def test_an_edit_when_only_the_checked_read_sees_a_node_config_changes_nothing(
        cat, secrets, graph, deployed, user_db, route_env, monkeypatch):
    """The unchecked read missed the node; the checked read finds one with a
    config and no pointer. The reads disagree, so the Edit refuses rather than
    take the registration path for a system that has a node."""
    _register_pg()
    user_db["password"] = NEW_PW
    before = dict(secrets[CONN_ARN])
    _graph_unavailable(monkeypatch, checked=lambda *a, **k: [
        {"secret_arn": None, "connection_config": json.dumps(PG_NODE)}])
    res = _put(_ui_body(cat))
    assert res.status_code == 500 and "nothing was changed" in res.json()["detail"], res.text
    assert secrets[CONN_ARN] == before and user_db["seen"] == []


def test_guard_an_unprovisioned_user_pg_edit_still_takes_the_registration_path(
        cat, secrets, graph, deployed, user_db, route_env):
    """No node at all: the Edit form's host/port/password rotation still works
    (the unprovisioned registration path), through the offline seam's
    node-state answer."""
    _register_pg()
    assert SID not in graph
    user_db["password"] = NEW_PW
    res = _put(_ui_body(cat))
    assert res.status_code == 200, res.text
