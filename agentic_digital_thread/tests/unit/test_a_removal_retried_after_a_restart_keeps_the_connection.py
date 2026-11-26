# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A registration whose removal did not finish keeps its connection across a
restart, so a default Remove again leaves it able to connect.

A removal that did not finish leaves the System node marked
(``removal_incomplete``) and the catalog entry ``removal_failed``. After a
restart the catalog is rebuilt from the graph (``server._restore`` ->
``catalog.ensure_removal_failed``). A default Remove again then deletes the
node and returns the entry to ``registered``: from then on the entry is the
only place the registration's connection is recorded. So the rebuilt entry
carries what the node held that the catalog already keeps for a registration:
its non-secret connection settings (``connection_resolver.
sanitize_connection_config``), its connection type and schema, and the exact
pointer to its own secret, never a secret value.

Built from real writes: a user registration, its connection edited before the
first provision (the admin Edit's own body, which calls ``write_connection``),
then a default Reset All, or the card's default removal of the system once
onboarded, failing part-way; a restart through the real restore; the card's
default Remove again; then the registration's connection and the wizard's
discover. Offline: the graph, Secrets Manager, the source database and the MCP
server are fakes.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.server as server  # noqa: E402
from agents.catalog import REMOVAL_FAILED, REMOVAL_MODE, SystemCatalog  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from tests.unit.test_chat_finishes_only_a_full_removal_of_an_onboarded_system import (  # noqa: E402,F401
    _failed_card_removal, _failed_reset_all, removals)
from tests.unit.test_chat_reset_keeps_a_registration_edited_before_onboarding import (  # noqa: E402
    MCP_ARN, MCP_SID, NEW_DSN, _edited_mcp, _edited_pg)
from tests.unit.test_chat_reset_removes_only_what_was_onboarded import sm  # noqa: E402,F401
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_env_credential_fill import NEW_PW, deployed  # noqa: E402,F401

ROTATED_TOKEN = "Bearer rotated-example-token"  # nosec B105 - the fixture's Edit
MCP_URL = "https://mcp.example.com/mcp"


@pytest.fixture
def world(removals, monkeypatch):
    """The removal fakes, plus what a restart and a discover reach."""
    monkeypatch.setattr(server, "neptune_query", removals.query)
    monkeypatch.setattr(server, "_restore_seed_core_concepts", lambda: None)
    started: list = []
    monkeypatch.setattr(server, "_restore_one", lambda **kw: started.append(kw) or "mcp://x")
    monkeypatch.setattr(onboard, "bedrock_call", lambda *a, **k: "analysis")
    removals.started = started
    return removals


def _restart(monkeypatch) -> SystemCatalog:
    """A new process: an empty catalog, then the startup restore."""
    cold = SystemCatalog([])
    for mod in (catalog_mod, onboard, oa, server):
        monkeypatch.setattr(mod, "catalog", cold)
    server._restore_base()
    return cold


def _remove_again(sid) -> dict:
    """The card's Remove again of a default removal that did not finish."""
    return asyncio.run(onboard.onboard_reset_system(sid, wait=True, full=False, retry=True))


class _PgSource:
    """The registration's PostgreSQL database: one table."""

    def __init__(self):
        self.dsns: list = []

    def connect(self, dsn, hostaddr=None):
        self.dsns.append(dsn)

        class _Cursor:
            def execute(self, *_a, **_k):
                pass

            def fetchall(self):
                return [{"table_name": "samples", "relation_kind": "base_table"}]

            def close(self):
                pass

        class _Conn:
            def cursor(self, *_a, **_k):
                return _Cursor()

            def close(self):
                pass
        return _Conn()


@pytest.fixture
def pg_source(monkeypatch):
    import agents.db as db
    src = _PgSource()
    monkeypatch.setattr(onboard, "_pg_connect", src.connect)
    monkeypatch.setattr(db, "pg_inspect_tables", lambda cur, schema, names: [
        {"name": n, "row_count": 2, "columns": [{"name": "lot_id", "data_type": "text"}]}
        for n in names])
    return src


def _wizard_discover(cold, sid) -> dict:
    """The wizard's POST /discover for the system as GET /catalog shows it."""
    served = onboard._served_catalog_entry(cold.get(sid))
    return asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=sid, connection_type=served["connection_type"],
        connection_config=served.get("connection_config") or {})))


def _registered_with_its_connection(cold, secrets, sm, graph, sid, arn) -> dict:
    entry = cold.get(sid)
    assert entry is not None, "the registration was removed"
    assert (entry.get("status"), entry.get("connection_origin")) == ("registered", "user"), entry
    assert arn in secrets and sm.deleted == [], "the registration's own secret was removed"
    assert sid not in graph, "Remove again left the node"
    assert NEW_PW not in json.dumps(entry) and ROTATED_TOKEN not in json.dumps(entry), entry
    reg = cr.registration_connection(sid, entry)
    assert reg.get("_secret_unresolved") is False, reg
    assert reg.get("secret_arn") == arn
    return reg


# ── a default Reset All that did not finish ─────────────────────────────────

@pytest.mark.parametrize("at", ["agentcore", "graph"])
def test_a_postgres_registration_connects_after_a_restart_and_remove_again(
        cat, secrets, sm, graph, world, pg_source, monkeypatch, at):
    sid, arn = _edited_pg(graph)
    _failed_reset_all(world, full=False, at=at)
    cold = _restart(monkeypatch)
    rebuilt = cold.get(sid)
    assert (rebuilt["status"], rebuilt.get(REMOVAL_MODE)) == (REMOVAL_FAILED, "default"), rebuilt
    assert _remove_again(sid).get("kept_registration") is True
    reg = _registered_with_its_connection(cold, secrets, sm, graph, sid, arn)
    assert (reg["host"], reg["password"], reg["schema"]) == ("db2.example.internal", NEW_PW, "lims")
    disc = _wizard_discover(cold, sid)
    assert [t["name"] for t in disc.get("tables") or []] == ["samples"], disc
    assert NEW_PW in pg_source.dsns[0] and "db2.example.internal" in pg_source.dsns[0]
    assert world.started == [], "a system with an unfinished removal was served"


def test_an_mcp_registration_connects_after_a_restart_and_remove_again(
        cat, secrets, sm, graph, world, monkeypatch):
    sid, arn = _edited_mcp(graph)
    _failed_reset_all(world, full=False, at="agentcore")
    cold = _restart(monkeypatch)
    assert _remove_again(sid).get("kept_registration") is True
    reg = _registered_with_its_connection(cold, secrets, sm, graph, sid, arn)
    assert (reg.get("url") or reg.get("connection_string"), reg.get("token")) == \
        (MCP_URL, ROTATED_TOKEN), reg
    seen: list = []
    monkeypatch.setattr(oa, "_discover_mcp", lambda entry, system_id="": seen.append(entry) or {
        "tables": [{"name": "get_tags", "row_count": 1, "columns": [{"name": "a/b"}]}]})
    disc = _wizard_discover(cold, sid)
    assert [t["name"] for t in disc.get("tables") or []] == ["get_tags"], disc
    cfg = seen[0]["connection_config"]
    assert (seen[0]["connection_string"], cfg["token"]) == (MCP_URL, ROTATED_TOKEN), seen[0]


# ── the card's default removal that did not finish ──────────────────────────

def test_an_onboarded_registration_connects_after_a_failed_card_removal_a_restart_and_remove_again(
        cat, secrets, sm, graph, world, pg_source, monkeypatch):
    sid, arn = _edited_pg(graph)
    graph[sid]["status"] = "onboarded"
    cat.update_status(sid, "onboarded")
    _failed_card_removal(world, sid, full=False, at="agentcore")
    cold = _restart(monkeypatch)
    rebuilt = cold.get(sid)
    assert (rebuilt["status"], rebuilt.get(REMOVAL_MODE)) == (REMOVAL_FAILED, "default"), rebuilt
    assert _remove_again(sid).get("kept_registration") is True
    reg = _registered_with_its_connection(cold, secrets, sm, graph, sid, arn)
    assert (reg["host"], reg["password"]) == ("db2.example.internal", NEW_PW)
    assert _wizard_discover(cold, sid).get("tables"), "discover failed"


# ── what the rebuilt entry holds ────────────────────────────────────────────

def test_the_rebuilt_entry_holds_the_pointer_and_settings_never_a_secret_value():
    node = {"id": SID, "removal_incomplete": True, "removal_full": False,
            "connection_type": "PostgreSQL", "secret_arn": CONN_ARN,
            "connection_config": json.dumps({
                "host": "db2.example.internal", "port": "5432", "dbname": "lims",
                "schema": "lims", "search_path": "lims", "token": "t0ken",
                "password": NEW_PW, "url": "https://user:pw@mcp.example.com/mcp",
                "connection_string": "https://user:pw@mcp.example.com/mcp?token=t0ken"})}
    entry = SystemCatalog([]).ensure_removal_failed(node)
    assert entry["secret_arn"] == CONN_ARN
    assert entry["connection_string"] == "https://mcp.example.com/mcp"
    assert entry["schema"] == "lims" and entry["connection_type"] == "PostgreSQL"
    assert entry["connection_config"] == cr.sanitize_connection_config(node["connection_config"])
    blob = json.dumps(entry)
    assert NEW_PW not in blob and "t0ken" not in blob and "user:pw" not in blob, entry


def test_an_entry_this_process_holds_is_not_changed_beyond_its_state():
    hot = {"id": SID, "name": "Quality Lab", "status": "registered", "schema": "lims",
           "connection_type": "PostgreSQL", "connection_origin": "user", "secret_arn": CONN_ARN,
           "connection_config": {"host": "db.example.internal"}}
    cat = SystemCatalog([dict(hot)])
    cat.ensure_removal_failed({"id": SID, "removal_full": False, "secret_arn": "other",
                               "connection_config": json.dumps({"host": "elsewhere"})})
    assert {**cat.get(SID), "status": "registered"} == {**hot, REMOVAL_MODE: "default"}


_MARKED = {"removal_incomplete": True, "removal_full": False, "connection_type": "PostgreSQL",
           "secret_arn": CONN_ARN,
           "connection_config": json.dumps({"host": "db2.example.internal", "port": "5432",
                                            "dbname": "lims", "schema": "lims"})}


def _named_only(cypher, params=None):
    """A graph that answers only the properties a query names."""
    import re
    return [{alias: (SID if prop == "id" else _MARKED.get(prop))
             for prop, alias in re.findall(r"s\.(\w+) AS (\w+)", cypher)}]


def _rebuilt_by(read: str, monkeypatch) -> dict:
    import agents.db as db
    cold = SystemCatalog([])
    for mod in (catalog_mod, onboard, oa, server):
        monkeypatch.setattr(mod, "catalog", cold)
    if read == "reset-preflight":
        monkeypatch.setattr(onboard, "neptune_query", _named_only)
        onboard._reset_verify_onboarded(SID)
    elif read == "chat-guard":
        monkeypatch.setattr(db, "neptune_query_checked", _named_only)
        oa._removal_failed_refusal(SID)
    else:
        monkeypatch.setattr(db, "neptune_query", _named_only)
        cold.hydrate()
    return cold.get(SID)


@pytest.mark.parametrize("read", ["reset-preflight", "chat-guard", "hydrate"])
def test_every_cold_rebuild_keeps_the_pointer_and_settings(read, monkeypatch):
    """The removal's preflight, the chat tools' guard and the runtime's
    hydration rebuild a marked node's entry the same way the restore does."""
    entry = _rebuilt_by(read, monkeypatch)
    assert entry is not None and entry["status"] == REMOVAL_FAILED, entry
    assert entry.get("secret_arn") == CONN_ARN, entry
    assert entry.get("connection_config") == {"host": "db2.example.internal", "port": "5432",
                                              "dbname": "lims"}, entry
    assert entry.get("schema") == "lims"
