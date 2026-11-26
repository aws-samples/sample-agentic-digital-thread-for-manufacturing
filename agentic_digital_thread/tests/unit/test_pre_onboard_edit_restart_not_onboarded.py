# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system Edited before onboarding is not "onboarded" after a restart.

Before the first provision an admin connection Edit lands in
``connection_resolver.write_connection``, which MERGEs the system's
:System node with its connection settings, credentials pointer and
connection type, and nothing else. Every provision path writes the node's
onboarding marker (``s.status = 'onboarded'``, with its name); this node has
none. A restart drops the in-memory registration, and two readers then turned
the node into an onboarded system:

* the catalog's graph hydration (``SystemCatalog.hydrate``, run on a lookup
  miss: the demo registration hook at every startup, a Connect New System of
  any new id) added every unseen :System node as 'onboarded', whatever its type;
* the startup restore's direct-source sweep (MCP / REST / Athena / SiteWise /
  i3X / Tulip) selected the node by its connection type, marked it
  onboarded and started an agent for it.

Both now require the onboarding marker; a node without it is skipped (logged).
A node whose removal did not finish still comes back as ``removal_failed``,
and an onboarded node still comes back onboarded. Offline: the graph is a
small fake answering the real queries, Secrets Manager a fake, the restart a
fresh catalog.
"""
from __future__ import annotations

import json
import logging
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.server as server  # noqa: E402
from agents.catalog import REMOVAL_FAILED, SystemCatalog  # noqa: E402
from agents.connection_types import (  # noqa: E402
    ATHENA, DIRECT_SOURCE_CONNECTION_TYPES, SITEWISE_DIRECT)
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    MCP_URL, _register_mcp)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    _admin_update, deployed)
from tests.unit.test_connect_new_system_ui_pg_edit import route_env  # noqa: E402,F401

NEW_SCHEMA_DSN = DSN.replace("search_path%3Dlims", "search_path%3Dlims_v2")
NEW_TOKEN = "rotated-before-onboarding-1"  # nosec B105 - fixture
OTHER_DSN = DSN.replace("/lims?", "/spares?").replace("search_path%3Dlims", "search_path%3Dspares")

#: The fields a restore / hydration row reads off a :System node.
_ROW_KEYS = ("name", "plant", "schema", "connection_type", "system_type",
             "connection_origin", "status", "removal_incomplete")


def _rows(graph: dict, keep) -> list[dict]:
    return [{"id": sid, **{k: node.get(k) for k in _ROW_KEYS}}
            for sid, node in graph.items() if sid != "_reads" and keep(node)]


def _answer(graph: dict):
    """The restart's graph reads, answered from the fake nodes: the SQL sweep
    (nodes with a SchemaEntry), the direct-source sweep (by type, or a
    removal marker) and the catalog's hydration read (every node)."""
    def query(cypher, params=None):
        if "HAS_SCHEMA]->(se:SchemaEntry)" in cypher and "RETURN DISTINCT" in cypher:
            return _rows(graph, lambda n: n.get("has_schema"))
        if "s.connection_type IN [" in cypher:
            return _rows(graph, lambda n: n.get("connection_type") in DIRECT_SOURCE_CONNECTION_TYPES
                         or ("removal_incomplete" in cypher and n.get("removal_incomplete")))
        if cypher.startswith("MATCH (s:System) RETURN DISTINCT"):
            return _rows(graph, lambda n: True)
        return []
    return query


@pytest.fixture(autouse=True)
def graph_reads(graph, monkeypatch):
    """Every catalog's hydration read (``SystemCatalog._graph_rows`` reads
    through ``db.neptune_query``) and the restore sweeps see the fake graph."""
    query = _answer(graph)
    monkeypatch.setattr(db, "neptune_query", query)
    monkeypatch.setattr(server, "neptune_query", query)
    return query


@pytest.fixture
def restart(graph, monkeypatch):
    """An ECS restart: a new, empty catalog, then the startup restore over the
    graph as the Edit left it. Agent starts are recorded, not made."""
    started: list = []

    def _do() -> SystemCatalog:
        cold = SystemCatalog([])
        for mod in (catalog_mod, onboard, onboard_agent, server):
            monkeypatch.setattr(mod, "catalog", cold)
        monkeypatch.setattr(server, "_restore_seed_core_concepts", lambda: None)
        monkeypatch.setattr(server, "_restore_one",
                            lambda **kw: started.append(kw["system_id"]) or "mcp://restored")
        server._restore_base()
        return cold
    _do.started = started
    return _do


def _not_onboarded(cold: SystemCatalog, sid: str = SID) -> None:
    entry = cold.get(sid)
    assert (entry or {}).get("status") != "onboarded", entry
    assert sid not in cold.get_onboarded_ids()


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


# ── the reported case: a user PostgreSQL registration's schema change ──────

def test_a_postgres_registration_edited_before_onboarding_is_not_onboarded_after_a_restart(
        cat, secrets, graph, deployed, restart):
    assert _register_pg()["status"] == "created"
    assert _body(_admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})).get("status") == "ok"
    assert graph[SID]["connection_type"] == "PostgreSQL"     # the Edit wrote the node
    cold = restart()
    # The next Connect New System (any new id) misses the cold catalog and
    # hydrates it from the graph, the way the restart's first lookup does.
    other = _register({"name": "Spares Lab", "system_type": "Custom",
                       "connection_type": "PostgreSQL", "schema": "spares",
                       "connection_config": {"dsn": OTHER_DSN}})
    assert other.get("status") == "created", other
    _not_onboarded(cold)
    assert restart.started == []


def test_an_mcp_registration_edited_before_onboarding_gets_no_agent_after_a_restart(
        cat, secrets, graph, route_env, restart):
    assert _register_mcp().get("status") == "created"
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert getattr(res, "status_code", 200) == 200, getattr(res, "body", res)
    assert graph[SID]["connection_type"] == "MCP Server"
    cold = restart()
    assert restart.started == [], "an agent was started for a system never onboarded"
    _not_onboarded(cold)
    assert cold.lookup("not-registered") is None              # a later lookup miss
    _not_onboarded(cold)


# ── every connector type: the node exactly as an Edit's write leaves it ────

def _edit_target(ctype: str) -> dict:
    if ctype == ATHENA:
        return {"workgroup": "primary", "database": "lake", "region": "us-east-1"}
    if ctype == SITEWISE_DIRECT:
        return {"region": "us-east-1", "role_arn": "arn:aws:iam::111122223333:role/sw-read"}
    if ctype == "MCP Server":
        return {"url": MCP_URL}
    return {"base_url": "https://source.example.com/api"}


@pytest.mark.parametrize("ctype", ["PostgreSQL", *DIRECT_SOURCE_CONNECTION_TYPES])
def test_an_edit_before_onboarding_never_restores_as_onboarded(ctype, cat, secrets, graph, restart):
    sid = "edited-" + ctype.split()[0].lower().strip("()")
    non_secret = ({"connection_type": ctype, "host": "db.example.internal", "port": "5432",
                   "dbname": "lims", "schema": "lims"} if ctype == "PostgreSQL"
                  else {"connection_type": ctype, **_edit_target(ctype)})
    cr.write_connection(sid, non_secret=non_secret)
    assert graph[sid]["connection_type"] == ctype and "status" not in graph[sid]
    cold = restart()
    assert cold.lookup("not-registered") is None              # hydrates the cold catalog
    _not_onboarded(cold, sid)
    assert cold.get(sid) is None
    assert restart.started == []


def test_the_skip_is_logged_with_the_system_id(cat, secrets, graph, restart, caplog):
    cr.write_connection("edited-mcp", non_secret={"connection_type": "MCP Server", "url": MCP_URL})
    with caplog.at_level(logging.DEBUG):
        cold = restart()
        cold.lookup("not-registered")
    skipped = [r.getMessage() for r in caplog.records if "edited-mcp" in r.getMessage()]
    assert any("never onboarded" in m for m in skipped), skipped


# ── guards: onboarded and half-removed systems keep their restore ──────────

def _onboarded_node(**props) -> dict:
    return {"status": "onboarded", "name": "Quality Lab", "plant": "site-1",
            "connection_config": json.dumps({"url": MCP_URL}), "secret_arn": "", **props}


def test_guard_an_onboarded_direct_system_still_restores_onboarded_with_its_agent(
        cat, secrets, graph, restart):
    graph[SID] = _onboarded_node(connection_type="MCP Server", has_schema=True)
    cold = restart()
    assert cold.get(SID)["status"] == "onboarded"
    assert restart.started == [SID]


def test_guard_an_onboarded_postgres_system_still_restores_onboarded_with_its_agent(
        cat, secrets, graph, restart, monkeypatch):
    graph[SID] = _onboarded_node(connection_type="PostgreSQL", schema="lims", has_schema=True,
                                 connection_config=json.dumps({"host": "db.example.internal"}))
    monkeypatch.setattr(server, "_restore_sql_table_details",
                        lambda schema: {"samples": [("sample_id", "text")]})
    monkeypatch.setattr(server, "_restore_sql_conn_params",
                        lambda sid, resolved=None: {"host": "db.example.internal"})
    cold = restart()
    assert cold.get(SID)["status"] == "onboarded"
    assert restart.started == [SID]


def test_guard_an_onboarded_node_still_hydrates_onboarded(cat, secrets, graph):
    """The onboarding runtime's containers never run the restore: they build
    their catalog from this read alone."""
    graph[SID] = _onboarded_node(connection_type="PostgreSQL", schema="lims")
    graph["edited-pg"] = {"connection_type": "PostgreSQL",
                          "connection_config": json.dumps({"host": "db.example.internal"})}
    cold = SystemCatalog([])
    assert cold.lookup(SID)["status"] == "onboarded"
    assert cold.get("edited-pg") is None


@pytest.mark.parametrize("ctype, has_schema", [("PostgreSQL", False), ("PostgreSQL", True),
                                               ("MCP Server", True)])
def test_guard_a_half_removed_system_still_restores_as_removal_failed(
        ctype, has_schema, cat, secrets, graph, restart):
    graph[SID] = _onboarded_node(connection_type=ctype, has_schema=has_schema,
                                 removal_incomplete=True)
    cold = restart()
    assert cold.get(SID)["status"] == REMOVAL_FAILED
    assert restart.started == []


def test_guard_a_half_removed_node_without_the_marker_still_hydrates_as_removal_failed(
        cat, secrets, graph):
    graph[SID] = {"connection_type": "PostgreSQL", "removal_incomplete": True}
    cold = SystemCatalog([])
    assert cold.lookup(SID)["status"] == REMOVAL_FAILED


# ── the SQL sweep reads the same marker ────────────────────────────────────

def _pg_restore_seams(monkeypatch) -> None:
    monkeypatch.setattr(server, "_restore_sql_table_details",
                        lambda schema: {"samples": [("sample_id", "text")]})
    monkeypatch.setattr(server, "_restore_sql_conn_params",
                        lambda sid, resolved=None: {"host": "db.example.internal"})


def test_a_postgres_node_with_a_schema_entry_but_no_marker_is_not_restored(
        cat, secrets, graph, restart, monkeypatch):
    """A connection-only node can gain a SchemaEntry edge without being
    onboarded (the orphan-schema repair re-attaches entries to whatever
    System node holds the id); the SchemaEntry is not onboarding evidence."""
    graph[SID] = {"connection_type": "PostgreSQL", "schema": "lims", "has_schema": True,
                  "connection_config": json.dumps({"host": "db.example.internal"})}
    _pg_restore_seams(monkeypatch)
    cold = restart()
    _not_onboarded(cold)
    assert cold.get(SID) is None and restart.started == []


# ── the same id after a restart: an honest refusal ─────────────────────────

PREVIOUS_REGISTRATION = (f"A previous registration of '{SID}' is still recorded, so this id "
                         "can't be registered again. Choose a different system id.")


@pytest.fixture
def graph_writes(monkeypatch):
    """``arm()`` records every graph write a registration could make from then
    on (none expected) and returns the list."""
    def arm() -> list:
        writes: list = []
        monkeypatch.setattr(onboard_agent, "neptune_write",
                            lambda cypher, params=None: writes.append(cypher) or True)
        monkeypatch.setattr(cr, "_neptune_write",
                            lambda cypher, params=None: writes.append(cypher) or True)
        return writes
    return arm


def test_the_same_id_after_a_restart_is_refused_as_a_previous_registration(
        cat, secrets, graph, deployed, restart, graph_writes):
    assert _register_pg()["status"] == "created"
    assert _body(_admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})).get("status") == "ok"
    node, stored = dict(graph[SID]), json.loads(json.dumps(secrets))
    writes = graph_writes()
    cold = restart()
    res = _register_pg()
    assert res == {"error": PREVIOUS_REGISTRATION}, res
    assert graph[SID] == node and secrets == stored, "a refused registration changed something"
    assert writes == [] and cold.get(SID) is None


def test_guard_an_onboarded_system_with_no_entry_is_still_already_exists(
        cat, secrets, graph, graph_writes, monkeypatch):
    """A cold catalog that missed an onboarded node keeps the ordinary refusal."""
    graph[SID] = {"connection_type": "PostgreSQL", "connection_config": "{}",
                  "status": "onboarded"}
    monkeypatch.setattr(cat, "_graph_rows", lambda: [])
    writes = graph_writes()
    res = _register_pg()
    assert res == {"error": f"System {SID} already exists"}, res
    assert not secrets and writes == []


def test_guard_a_registered_entry_is_still_already_exists(cat, secrets, graph, deployed):
    assert _register_pg()["status"] == "created"
    assert _body(_admin_update(SID, {"connection_string": NEW_SCHEMA_DSN})).get("status") == "ok"
    res = _register_pg()
    assert res == {"error": f"System {SID} already exists"}, res


def test_guard_a_half_removed_id_is_still_refused_as_a_removal(cat, secrets, graph):
    graph[SID] = {"connection_type": "PostgreSQL", "removal_incomplete": True}
    res = _register_pg()
    assert res.get("reason") == "removal_failed", res
    assert SID in graph and not secrets
