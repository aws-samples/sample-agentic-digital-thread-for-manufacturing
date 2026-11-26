# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``server._restore`` connection sourcing (Step-3 Wave-2 Task 2.1).

Background: ``server._restore`` (run on app startup) used to source each
system's connection from the in-memory catalog:

  * SQL systems built pgdriver params via ``db._get_conn_params_for_schema(schema)``;
  * MCP/REST/SiteWise systems read ``catalog.get(sid)['connection_config']``.

Step-3 routes BOTH read sites through ``resolve_connection`` (the System node is
the source of truth; the catalog is no longer read for connection data). The
reroute is behavior-preserving while ``resolve_connection`` still carries the
catalog fallback (CONN_SOURCE=catalog default), so these tests pin the WIRING:

  * the SQL branch calls ``resolve_connection(sid)`` -> ``pg_conn_params(...)`` and
    passes the result as ``conn_params`` to ``start_agent`` — NOT
    ``_get_conn_params_for_schema``;
  * the MCP branch calls ``resolve_connection(sid)`` and passes it as
    ``connection_config``, taking the system name from the restore Cypher result
    (``ns['name']``), and NEVER calls ``catalog.get(sid)`` for connection data.

Runnable two ways (matches ``test_reconnect_propagation.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_server_restore.py
  * standalone:  python agentic_digital_thread/tests/unit/test_server_restore.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.server as server  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402  (the measured-records read a PG restore makes)
from agents.catalog import SystemCatalog  # noqa: E402


def _fake_catalog(known_ids):
    """Catalog stub: get_all() reports the SQL system as already present (so the
    add() path is skipped). ``get`` is a MagicMock so we can assert it is NEVER
    used for connection data inside _restore."""
    cat = MagicMock()
    cat.get_all.return_value = [{"id": i} for i in known_ids]
    cat.update_status.return_value = None
    cat.add.return_value = None
    return cat


def _fake_pg(*args, **kwargs):
    """Return tables for the introspection query, columns otherwise — enough to
    populate table_details so the SQL agent is started. The columns query now
    selects (column_name, data_type), so rows carry both."""
    sql = args[1] if len(args) > 1 else ""
    if "information_schema.tables" in sql:
        return {"rows": [["orders"]]}
    if "information_schema.columns" in sql:
        return {"rows": [["id", "integer"], ["qty", "integer"]]}
    return {"rows": []}


def _run_restore(sql_systems, mcp_systems, resolved_map, pg_params):
    """Invoke server._restore with all connection/IO seams mocked. Returns the
    recorded calls for assertions."""
    fake_mgr = MagicMock()
    fake_mgr.start_agent.return_value = "https://gateway.example.com/mcp"
    fake_cat = _fake_catalog(known_ids=[s["id"] for s in sql_systems])

    neptune_query = MagicMock(side_effect=[sql_systems, mcp_systems])
    resolve_connection = MagicMock(side_effect=lambda sid: dict(resolved_map[sid]))
    pg_conn_params = MagicMock(return_value=dict(pg_params))
    add_system_agent = MagicMock()

    with patch.object(server, "_agent_manager", fake_mgr), \
            patch.object(server, "_sql_tables_desc_from_graph", return_value=""), \
            patch.object(server, "neptune_query", neptune_query), \
            patch.object(server, "pg", _fake_pg), \
            patch.object(server, "catalog", fake_cat), \
            patch.object(server, "add_system_agent", add_system_agent), \
            patch.object(cr, "resolve_connection", resolve_connection), \
            patch.object(cr, "pg_conn_params", pg_conn_params), \
            patch.object(onboard, "neptune_query_checked", return_value=[]), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()

    return {
        "mgr": fake_mgr,
        "catalog": fake_cat,
        "resolve_connection": resolve_connection,
        "pg_conn_params": pg_conn_params,
        "add_system_agent": add_system_agent,
    }


# --------------------------------------------------------------------------- #
# SQL branch
# --------------------------------------------------------------------------- #

def test_sql_restore_uses_resolve_connection_and_pg_conn_params():
    """SQL systems must source conn_params from resolve_connection -> pg_conn_params,
    NOT from catalog.get() / _get_conn_params_for_schema."""
    sql_systems = [{"id": "erp", "name": "ERP", "schema": "erp_data", "plant": "P1",
                    "status": "onboarded"}]
    resolved = {"erp": {"system_id": "erp", "connection_type": "PostgreSQL",
                        "host": "db-host", "port": 5432, "dbname": "vf",
                        "schema": "erp_data"}}
    pg_params = {"host": "db-host", "port": 5432, "dbname": "vf",
                 "user": "u", "password": "pw", "options": "-csearch_path=erp_data"}

    calls = _run_restore(sql_systems, [], resolved, pg_params)

    # Connection resolved via the node, not the catalog.
    calls["resolve_connection"].assert_called_once_with("erp")
    calls["pg_conn_params"].assert_called_once_with(resolved["erp"])
    # catalog.get() is never used for connection data anywhere in _restore.
    calls["catalog"].get.assert_not_called()

    # start_agent received the pg_conn_params output as conn_params.
    kw = calls["mgr"].start_agent.call_args.kwargs
    assert kw["system_id"] == "erp", kw
    assert kw["conn_params"] == pg_params, kw
    assert kw["schema"] == "erp_data", kw
    calls["add_system_agent"].assert_called_once_with("erp", "https://gateway.example.com/mcp")


# --------------------------------------------------------------------------- #
# MCP branch
# --------------------------------------------------------------------------- #

def test_mcp_restore_uses_resolve_connection_not_catalog_get():
    """MCP systems must pass resolve_connection(sid) as connection_config and take
    the name from the restore Cypher result — never catalog.get(sid)."""
    mcp_systems = [{"id": "highbyte", "name": "HighByte UNS", "plant": "P1",
                    "connection_type": "MCP Server", "status": "onboarded"}]
    resolved = {"highbyte": {"system_id": "highbyte", "connection_type": "MCP Server",
                             "url": "https://hb.example.com/mcp", "token": "tok"}}

    calls = _run_restore([], mcp_systems, resolved, {})

    calls["resolve_connection"].assert_called_once_with("highbyte")
    calls["catalog"].get.assert_not_called()

    kw = calls["mgr"].start_agent.call_args.kwargs
    assert kw["system_id"] == "highbyte", kw
    # name from the restore Cypher (ns['name']), not the catalog.
    assert kw["system_name"] == "HighByte UNS", kw
    assert kw["connection_type"] == "MCP Server", kw
    assert kw["connection_config"] == resolved["highbyte"], kw
    calls["add_system_agent"].assert_called_once_with(
        "highbyte", "https://gateway.example.com/mcp")


def test_mcp_restore_skips_unconfigured_node():
    """A node with no url/base_url/connection_string and no secret_arn is treated
    as unregistered — skipped, no agent started (fail-soft, no catalog read)."""
    mcp_systems = [{"id": "ghost", "name": "Ghost", "plant": "P1",
                    "connection_type": "MCP Server", "status": "onboarded"}]
    resolved = {"ghost": {"system_id": "ghost", "connection_type": "MCP Server",
                          "connection_string": "", "secret_arn": ""}}

    calls = _run_restore([], mcp_systems, resolved, {})

    calls["resolve_connection"].assert_called_once_with("ghost")
    calls["catalog"].get.assert_not_called()
    calls["mgr"].start_agent.assert_not_called()
    calls["add_system_agent"].assert_not_called()


# --------------------------------------------------------------------------- #
# Direct-source dispatch (G2) — restore uses the node's REAL connection_type
# --------------------------------------------------------------------------- #

def test_direct_restore_dispatches_real_athena_connection_type():
    """G2: an onboarded Athena node restores with connection_type='Athena' (its REAL
    type from the node) — NOT a hardcoded 'MCP Server' — and is NOT skipped even
    though it has no url/base_url/connection_string/secret_arn (its connection is a
    boto3 workgroup + a cross-account role_arn). This is what lets a cross-account
    Athena system survive an ECS task recycle."""
    direct_systems = [{"id": "ags", "name": "AGS Athena", "plant": "P1",
                       "connection_type": "Athena", "status": "onboarded"}]
    resolved = {"ags": {"system_id": "ags", "connection_type": "Athena",
                        "workgroup": "primary", "database": "mfg_db",
                        "region": "us-east-1",
                        "role_arn": "arn:aws:iam::444455556666:role/adt-readonly",
                        "secret_arn": ""}}

    calls = _run_restore([], direct_systems, resolved, {})

    calls["resolve_connection"].assert_called_once_with("ags")
    calls["catalog"].get.assert_not_called()
    kw = calls["mgr"].start_agent.call_args.kwargs
    assert kw["system_id"] == "ags", kw
    assert kw["connection_type"] == "Athena", kw          # real type, not 'MCP Server'
    assert kw["connection_config"] == resolved["ags"], kw
    calls["add_system_agent"].assert_called_once_with(
        "ags", "https://gateway.example.com/mcp")


def test_direct_restore_dispatches_real_sitewise_direct_connection_type():
    """G2: a SiteWise (Direct) node restores with its real connection_type (not a
    hardcoded 'MCP Server') and carries any cross-account role_arn from the node."""
    direct_systems = [{"id": "sw", "name": "SiteWise", "plant": "P1",
                       "connection_type": "SiteWise (Direct)", "status": "onboarded"}]
    resolved = {"sw": {"system_id": "sw", "connection_type": "SiteWise (Direct)",
                       "connection_string": "iotsitewise://us-east-1.amazonaws.com",
                       "role_arn": "arn:aws:iam::444455556666:role/adt-readonly",
                       "secret_arn": ""}}

    calls = _run_restore([], direct_systems, resolved, {})

    kw = calls["mgr"].start_agent.call_args.kwargs
    assert kw["connection_type"] == "SiteWise (Direct)", kw
    assert kw["connection_config"] == resolved["sw"], kw
    calls["add_system_agent"].assert_called_once_with(
        "sw", "https://gateway.example.com/mcp")


# --------------------------------------------------------------------------- #
# Startup-hydration mislabel root-fix — a non-PostgreSQL system that ALSO carries
# a SchemaEntry (SiteWise (Direct)/Athena/... all get one at provision time) must
# NOT be relabeled 'PostgreSQL' by the SQL restore sweep, and must retain its REAL
# connection_type in the catalog after a restart. Uses the REAL SystemCatalog
# (empty, as on a fresh ECS task) so we can assert the hydrated catalog state.
# --------------------------------------------------------------------------- #

def _run_restore_real_catalog(sql_systems, mcp_systems, resolved_map, pg_params):
    """Invoke server._restore against a REAL (empty) SystemCatalog, mocking only
    the Neptune/DB/agent seams. Returns the recorded calls + the live catalog so
    the test can assert the final hydrated connection_type per system."""
    fake_mgr = MagicMock()
    fake_mgr.start_agent.return_value = "https://gateway.example.com/mcp"
    real_cat = SystemCatalog([])

    neptune_query = MagicMock(side_effect=[sql_systems, mcp_systems])
    resolve_connection = MagicMock(side_effect=lambda sid: dict(resolved_map[sid]))
    pg_conn_params = MagicMock(return_value=dict(pg_params))
    add_system_agent = MagicMock()

    with patch.object(server, "_agent_manager", fake_mgr), \
            patch.object(server, "_sql_tables_desc_from_graph", return_value=""), \
            patch.object(server, "neptune_query", neptune_query), \
            patch.object(server, "pg", _fake_pg), \
            patch.object(server, "catalog", real_cat), \
            patch.object(server, "add_system_agent", add_system_agent), \
            patch.object(cr, "resolve_connection", resolve_connection), \
            patch.object(cr, "pg_conn_params", pg_conn_params), \
            patch.object(onboard, "neptune_query_checked", return_value=[]), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()

    return {"mgr": fake_mgr, "catalog": real_cat, "add_system_agent": add_system_agent}


def test_startup_hydration_preserves_real_connection_type():
    """ROOT-FIX regression: a SiteWise (Direct) system with a SchemaEntry is
    returned by BOTH the SQL sweep (it has HAS_SCHEMA->SchemaEntry) AND the
    direct-source sweep. After restore it must be catalogued as
    'SiteWise (Direct)' (NOT 'PostgreSQL') and started via the node-connection
    path — while a genuine PostgreSQL system still restores as PostgreSQL with a
    SQL agent."""
    # The SQL query (HAS_SCHEMA->SchemaEntry) returns BOTH — SiteWise gets a
    # SchemaEntry (schema='sitewise-direct') at provision time, just like PG.
    sql_systems = [
        {"id": "erp", "name": "ERP", "schema": "erp_data", "plant": "P1",
         "connection_type": "PostgreSQL", "status": "onboarded"},
        {"id": "sw", "name": "SiteWise", "schema": "sitewise-direct", "plant": "P1",
         "connection_type": "SiteWise (Direct)", "status": "onboarded"},
    ]
    # The direct-source query returns only the non-SQL system.
    mcp_systems = [
        {"id": "sw", "name": "SiteWise", "plant": "P1",
         "connection_type": "SiteWise (Direct)", "status": "onboarded"},
    ]
    resolved = {
        "erp": {"system_id": "erp", "connection_type": "PostgreSQL",
                "host": "db-host", "port": 5432, "dbname": "vf", "schema": "erp_data"},
        "sw": {"system_id": "sw", "connection_type": "SiteWise (Direct)",
               "connection_string": "iotsitewise://us-east-1.amazonaws.com",
               "secret_arn": ""},
    }
    pg_params = {"host": "db-host", "port": 5432, "dbname": "vf",
                 "user": "u", "password": "pw", "options": "-csearch_path=erp_data"}

    calls = _run_restore_real_catalog(sql_systems, mcp_systems, resolved, pg_params)
    cat = calls["catalog"]

    # The SiteWise system is catalogued with its REAL type — NOT 'PostgreSQL'.
    sw_entry = cat.get("sw")
    assert sw_entry is not None, "SiteWise system missing from catalog after restore"
    assert sw_entry["connection_type"] == "SiteWise (Direct)", sw_entry
    # The real PostgreSQL system still restores as PostgreSQL.
    erp_entry = cat.get("erp")
    assert erp_entry is not None, "ERP system missing from catalog after restore"
    assert erp_entry["connection_type"] == "PostgreSQL", erp_entry

    # Agent dispatch: SiteWise via node-connection path (connection_type +
    # connection_config), PostgreSQL via the SQL path (conn_params + schema).
    by_id = {c.kwargs["system_id"]: c.kwargs for c in calls["mgr"].start_agent.call_args_list}
    assert set(by_id) == {"erp", "sw"}, by_id
    # SiteWise: dispatched with its real connection_type and NO PostgreSQL conn_params.
    assert by_id["sw"]["connection_type"] == "SiteWise (Direct)", by_id["sw"]
    assert by_id["sw"]["connection_config"] == resolved["sw"], by_id["sw"]
    assert "conn_params" not in by_id["sw"], by_id["sw"]
    # PostgreSQL: dispatched via the SQL path (conn_params + schema), NOT relabeled.
    assert by_id["erp"]["conn_params"] == pg_params, by_id["erp"]
    assert by_id["erp"]["schema"] == "erp_data", by_id["erp"]
    assert "connection_type" not in by_id["erp"], by_id["erp"]


# --------------------------------------------------------------------------- #
# Direct-sweep QUERY FILTER root-fix — the durability gap the reroute tests above
# miss because they feed ``mcp_systems`` directly. The direct-source sweep must
# select systems by their non-PostgreSQL connection_type ALONE — NOT gated on
# SchemaEntry presence/absence and with no status filter in the query —
# otherwise a SiteWise (Direct)/Athena node (carries a SchemaEntry, skipped by
# the SQL sweep) is restored by NEITHER sweep and vanishes from the catalog on
# restart. The node's onboarding marker is RETURNed and judged per node
# (``server._restore_skip``), so the skip of a never-onboarded node is
# logged by id.
# --------------------------------------------------------------------------- #

def test_direct_sweep_query_selects_by_connection_type_independent_of_status():
    """Pin the direct-source restore Cypher: it must select via
    ``s.connection_type IN [<DIRECT_SOURCE_CONNECTION_TYPES>]``, must NOT filter
    on ``status`` in its WHERE clause, and must NOT gate on
    ``HAS_SCHEMA``/``SchemaEntry`` — that is what lets a re-provisioned
    SiteWise (Direct)/Athena system (which has a SchemaEntry) survive a restart
    instead of vanishing from the catalog. It returns ``s.status`` so each node
    is judged in code, where a skip is logged."""
    from agents.connection_types import DIRECT_SOURCE_CONNECTION_TYPES

    fake_mgr = MagicMock()
    fake_mgr.start_agent.return_value = "https://gateway.example.com/mcp"
    recorded_queries: list[str] = []

    def _record_query(cypher, *a, **k):
        recorded_queries.append(cypher)
        return []  # no systems — we only assert on the query strings here

    with patch.object(server, "_agent_manager", fake_mgr), \
            patch.object(server, "neptune_query", side_effect=_record_query), \
            patch.object(server, "pg", _fake_pg), \
            patch.object(server, "catalog", _fake_catalog([])), \
            patch.object(server, "add_system_agent", MagicMock()), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()

    # Two sweeps -> two neptune_query calls: SQL sweep first, direct sweep second.
    assert len(recorded_queries) == 2, recorded_queries
    direct_q = recorded_queries[1]
    # Selects by connection_type using the canonical direct-source type set.
    assert "s.connection_type IN [" in direct_q, direct_q
    for t in DIRECT_SOURCE_CONNECTION_TYPES:
        assert f"'{t}'" in direct_q, (t, direct_q)
    # No status filter in the query: every node of a direct type comes back,
    # and the onboarding marker is read so each node is judged (and a skip
    # logged) in code.
    where = direct_q.split(" RETURN ", 1)[0]
    assert "status" not in where, direct_q
    assert "s.status AS status" in direct_q, direct_q
    # Independent of SchemaEntry — the direct sweep must neither require nor exclude
    # a HAS_SCHEMA->SchemaEntry edge (SiteWise/Athena carry one).
    assert "HAS_SCHEMA" not in direct_q and "SchemaEntry" not in direct_q, direct_q


def test_direct_sweep_skips_system_already_restored_by_sql_sweep():
    """Double-processing guard: a system restored by the SQL (PostgreSQL) sweep is
    NOT processed again by the direct-source sweep, even if it also appears in the
    direct query result. It is started exactly once, via the SQL path — PostgreSQL
    restore behavior is unchanged."""
    sql_systems = [{"id": "erp", "name": "ERP", "schema": "erp_data", "plant": "P1",
                    "connection_type": "PostgreSQL", "status": "onboarded"}]
    # Same id (defensively) also returned by the direct query. Give it a usable
    # connection marker so that WITHOUT the guard the direct sweep would start a
    # SECOND agent — isolating the guard as the thing under test.
    mcp_systems = [{"id": "erp", "name": "ERP", "plant": "P1",
                    "connection_type": "PostgreSQL", "status": "onboarded"}]
    resolved = {"erp": {"system_id": "erp", "connection_type": "PostgreSQL",
                        "host": "db-host", "port": 5432, "dbname": "vf",
                        "schema": "erp_data",
                        "connection_string": "postgresql://db-host/vf"}}
    pg_params = {"host": "db-host", "port": 5432, "dbname": "vf",
                 "user": "u", "password": "pw", "options": "-csearch_path=erp_data"}

    calls = _run_restore(sql_systems, mcp_systems, resolved, pg_params)

    # Started exactly once — via the SQL path (conn_params + schema), not twice.
    assert calls["mgr"].start_agent.call_count == 1, calls["mgr"].start_agent.call_args_list
    kw = calls["mgr"].start_agent.call_args.kwargs
    assert kw["system_id"] == "erp", kw
    assert kw["conn_params"] == pg_params, kw
    assert kw["schema"] == "erp_data", kw
    calls["add_system_agent"].assert_called_once_with(
        "erp", "https://gateway.example.com/mcp")


def test_sql_tables_desc_from_graph_emits_columns_types_and_partition_flag():
    """The restore helper rebuilds a SQL system's schema from
    (System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field) as
    ``tbl(col: type, ...)`` with partition keys flagged — so an Athena runtime is
    recreated with the schema baked in and does not re-discover it per sub-question."""
    field_rows = [
        {"tbl": "events", "relation_kind": "base_table", "col": "dt", "dtype": "string", "is_part": True},
        {"tbl": "events", "relation_kind": "base_table", "col": "value", "dtype": "double", "is_part": False},
        {"tbl": "runs", "relation_kind": "base_table", "col": "run_id", "dtype": "varchar", "is_part": False},
    ]
    with patch.object(server, "neptune_query", MagicMock(return_value=field_rows)):
        out = server._sql_tables_desc_from_graph("lake-x")

    assert "events [base_table](dt: string (partition key), value: double)" in out
    assert "runs [base_table](run_id: varchar)" in out


def test_sql_tables_desc_from_graph_empty_when_no_fields():
    """No Field rows -> empty string (agent falls back to runtime discovery)."""
    with patch.object(server, "neptune_query", MagicMock(return_value=[])):
        assert server._sql_tables_desc_from_graph("lake-x") == ""


_TESTS = [
    test_sql_restore_uses_resolve_connection_and_pg_conn_params,
    test_sql_tables_desc_from_graph_emits_columns_types_and_partition_flag,
    test_sql_tables_desc_from_graph_empty_when_no_fields,
    test_mcp_restore_uses_resolve_connection_not_catalog_get,
    test_mcp_restore_skips_unconfigured_node,
    test_direct_restore_dispatches_real_athena_connection_type,
    test_direct_restore_dispatches_real_sitewise_direct_connection_type,
    test_startup_hydration_preserves_real_connection_type,
    test_direct_sweep_query_selects_by_connection_type_independent_of_status,
    test_direct_sweep_skips_system_already_restored_by_sql_sweep,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} server._restore reroute tests passed "
          "(no real Neptune/DB/agent)")
