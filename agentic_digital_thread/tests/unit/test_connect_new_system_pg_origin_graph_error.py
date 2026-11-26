# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: a graph error never decides a PostgreSQL entry's origin.
Offline: no AWS, no Neptune, no network.

``_pg_entry_with_origin`` decides an UNMARKED PostgreSQL entry's origin from
its graph node's pointer. ``neptune_query`` returns ``[]`` on a graph error, so
a transient read failure used to look like "no own pointer" and took the demo
path (provision then re-pointed the node at the demo DB secret). The node is
now read with ``neptune_query_checked``: a graph read error on an unmarked
entry is the retryable "could not be read" error, nothing written, nothing
started. A marked entry reads no node.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import deployed  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_locked_budget_and_provision import _pg_ctx, pg_runtime  # noqa: E402,F401
from tests.unit.test_connect_new_system_pg_origin_after_restart import (  # noqa: E402,F401 - fixtures
    PG_NODE, _restarted_user_pg, no_demo_secret)

#: The real checked read, captured before any fixture replaces it (absent on
#: commits before this fix, where the helper read the node unchecked).
_REAL_CHECKED = getattr(cr, "node_secret_arn_checked", None)
UNREADABLE = "could not be read"


@pytest.fixture
def graph_read_fails(graph, monkeypatch):
    """The graph is unavailable (Neptune throttled / unreachable): every checked
    read raises, and every unchecked read sees what ``neptune_query`` returns on
    an error, ``[]`` (so ``_fetch_node_config`` -> no node). The fake graph still
    records writes, so a write made after the outage clears is visible."""
    calls: list = []

    def _raise(cypher, parameters=None):
        calls.append(cypher)
        raise db.GraphQueryError("ThrottlingException", "graph unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _raise)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    if _REAL_CHECKED is not None:
        monkeypatch.setattr(cr, "node_secret_arn_checked", _REAL_CHECKED)
    return calls


def test_l1_discover_refuses_when_the_node_cannot_be_read(
        cat, secrets, graph, deployed, graph_read_fails):
    _restarted_user_pg(cat, graph)
    for conn_str in (DSN, ""):
        dsn, hostaddr, err = onboard._pg_discover_target(SID, conn_str, "lims")
        assert (dsn, hostaddr) == ("", None) and UNREADABLE in err, (dsn, hostaddr, err)
    assert not cat.get(SID).get("connection_origin")


def test_l1_connection_phase_refuses_and_writes_nothing(
        cat, secrets, graph, deployed, no_demo_secret, graph_read_fails):
    _restarted_user_pg(cat, graph)
    before = dict(graph[SID])
    err = onboard._dp_connection_phase(_pg_ctx())
    assert isinstance(err, dict) and UNREADABLE in err.get("error", ""), err
    assert graph[SID] == before


def test_l1_start_refuses_and_starts_nothing(
        cat, secrets, graph, deployed, pg_runtime, graph_read_fails):
    _restarted_user_pg(cat, graph)
    err = onboard._dp_start_pg_agent(_pg_ctx())
    assert isinstance(err, dict) and UNREADABLE in err.get("error", ""), err
    assert pg_runtime.started == []


def test_guard_l1_a_marked_entry_reads_no_node(cat, secrets, graph, deployed,
                                               graph_read_fails):
    _restarted_user_pg(cat, graph, marked=True)
    before = len(graph_read_fails)
    assert onboard._pg_entry_with_origin(SID) == (cat.get(SID), "")
    assert graph_read_fails[before:] == [], graph_read_fails[before:]


def test_guard_l1_a_checked_read_with_no_node_takes_the_demo_path(
        cat, secrets, graph, deployed, monkeypatch):
    """A successful read that finds no node is still "no own pointer"."""
    if _REAL_CHECKED is not None:
        monkeypatch.setattr(cr, "node_secret_arn_checked", _REAL_CHECKED)
    monkeypatch.setattr(db, "neptune_query_checked", lambda *a, **k: [])
    cat.add({"id": SID, "name": "Quality Lab", "connection_type": "PostgreSQL",
             "schema": "lims", "status": "onboarded"})
    assert onboard._pg_entry_with_origin(SID) == (cat.get(SID), "")
