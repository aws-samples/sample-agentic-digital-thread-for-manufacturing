# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables system is provisioned with a real baked ``tables_desc`` (the
same renderer the Athena path uses), so the turn-time registries fill and the
catalog window rules can fire, instead of ``tables_desc=""`` which left every
rule inactive (scope 3).

* ``_dp_s3tables_tables_desc`` discovers columns through the connector's own
  metadata path and renders them with ``format_tables_desc``, keyed by the
  terminal table name the DuckDB SQL uses.
* ``S3 Tables`` is in ``_BAKED_SCHEMA_TYPES`` and in the discovery-tools hint,
  so the "schema already provided" directive names the DuckDB tools.
* The baked desc fills ``state_times`` / ``storage_bounds`` / ``record_time``
  through the shared ``_remember_turn_time_facts`` path.

Offline: the engine's ``connection_factory`` seam injects an in-memory DuckDB.
Dataset-neutral: a namespace/table the shipped seed does not use.
"""
import duckdb
import pytest

import agents.onboard as onboard
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa
from agents.connection_types import ATHENA, S3TABLES_DUCKDB

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-bake"


@pytest.fixture(autouse=True)
def _confirmed_unpartitioned(monkeypatch):
    """Offline, the in-memory tables have no Iceberg metadata, so the real
    partition read raises PartitionSpecUnavailable. These bake tests exercise
    the column/collision rendering, not partition discovery, so stand the
    partition read in as CONFIRMED unpartitioned ([]). The partition-unavailable
    path has its own tests that do NOT use this fixture's return."""
    monkeypatch.setattr(onboard, "_s3tables_partition_fields",
                        lambda name, cfg, factory: [])


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.flow")
        con.execute(
            "CREATE TABLE s3t.flow.segments("
            "observed_at TIMESTAMP, state_ready_at TIMESTAMP, state_done_at TIMESTAMP,"
            " route_name VARCHAR, segment_type VARCHAR)")
        return con
    return factory


# --- the type is a first-class baked-schema type ----------------------------

def test_s3tables_is_a_baked_schema_type():
    assert S3TABLES_DUCKDB in sa._BAKED_SCHEMA_TYPES


def test_discovery_tools_hint_names_the_duckdb_tools():
    hint = sa._discovery_tools_hint(S3TABLES_DUCKDB)
    assert "s3tables_list_tables" in hint
    assert "s3tables_describe_table" in hint


# --- the baked desc ----------------------------------------------------------

def test_bake_renders_columns_keyed_by_qualified_table_name():
    # Live bug fix: the bake renders the FULLY QUALIFIED s3t.<ns>.<table> name
    # the agent must write, not a bare terminal it has to re-qualify (which let
    # it guess the wrong namespace and hit a nonexistent table).
    desc = onboard._dp_s3tables_tables_desc(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_factory())
    from agents.system_agent import desc_qualified_table_names
    assert "s3t.flow.segments" in desc_qualified_table_names(desc)  # the exact SQL relation name
    assert "observed_at" in desc
    assert "route_name" in desc


def test_baked_desc_is_parseable_and_keyed_by_terminal_name():
    # The desc the bake produces must parse through the shared reader keyed by
    # the terminal table name, so every turn-time registry (which all key on
    # rpartition('.')[2]) resolves the DuckDB SQL's terminal name.
    desc = onboard._dp_s3tables_tables_desc(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_factory())
    cols = storage_bounds.table_columns_from_tables_desc(desc)
    assert "segments" in cols
    assert {"observed_at", "route_name", "segment_type"} <= set(cols["segments"])


def test_remember_turn_time_facts_does_not_warn_schema_absent(caplog):
    import logging
    state_times.reset()
    storage_bounds.reset()
    desc = onboard._dp_s3tables_tables_desc(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_factory())
    with caplog.at_level(logging.WARNING, logger="agents.system_agent"):
        sa._remember_turn_time_facts(SYS, None, desc, connection_type=S3TABLES_DUCKDB)
    assert "MODE_A_SCHEMA_ABSENT" not in caplog.text  # a real desc was baked
    state_times.reset()
    storage_bounds.reset()


def test_bake_falls_back_to_empty_on_discovery_failure():
    def boom():
        raise RuntimeError("no bucket")
    desc = onboard._dp_s3tables_tables_desc(
        {"table_bucket_arn": ARN}, SYS, connection_factory=boom)
    assert desc == ""


# --- F3: provisioning fails closed when the schema cannot be baked ----------

def test_provision_fails_when_schema_discovery_is_empty(monkeypatch):
    import types

    started = {"called": False}

    def _no_schema(cfg, system_id, connection_factory=None):
        return "", "schema_unavailable"

    class _FakeMgr:
        _agents = {}

        def start_agent(self, **kwargs):
            started["called"] = True
            return "mcp://should-not-start"

    monkeypatch.setattr(onboard, "_dp_s3tables_schema", _no_schema)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeMgr())

    ctx = types.SimpleNamespace(
        system_id=SYS, system_name="S", schema="s3tables",
        body=types.SimpleNamespace(connection_config={"table_bucket_arn": ARN}),
        cm_items=[], field_descriptions=[])
    out = onboard._dp_agent_s3tables_duckdb(ctx)
    assert isinstance(out, dict) and out.get("reason") == "schema_unavailable"
    assert started["called"] is False  # no agent started on an empty rules schema


# --- F4: ambiguous terminal-only table identity is rejected -----------------

def _two_namespace_factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.nsa")
        con.execute("CREATE SCHEMA s3t.nsb")
        con.execute("CREATE TABLE s3t.nsa.events(a_col VARCHAR, observed_at TIMESTAMP)")
        con.execute("CREATE TABLE s3t.nsb.events(b_col VARCHAR, created_at TIMESTAMP)")
        return con
    return factory


def test_same_terminal_table_in_two_namespaces_is_rejected_not_overwritten():
    # Two namespaces each hold `events` with different columns. The registries
    # key by terminal name, so baking either would mis-apply one namespace's
    # facts to the other's table. The bake must reject the ambiguity, not pick
    # one silently.
    desc = onboard._dp_s3tables_tables_desc(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_two_namespace_factory())
    assert desc == ""   # ambiguous -> no baked schema -> provisioning fails (F3)


def test_render_inputs_flags_terminal_collision():
    details, hidden, ambiguous = onboard._s3tables_render_inputs_checked([
        {"name": "nsa.events", "columns": [{"name": "a_col", "data_type": "varchar"}]},
        {"name": "nsb.events", "columns": [{"name": "b_col", "data_type": "varchar"}]},
    ], {"table_bucket_arn": ARN}, None)
    assert ambiguous == ["events"]


def test_render_inputs_no_collision_is_not_ambiguous():
    details, hidden, ambiguous = onboard._s3tables_render_inputs_checked([
        {"name": "nsa.events", "columns": [{"name": "a_col", "data_type": "varchar"}]},
        {"name": "nsb.orders", "columns": [{"name": "b_col", "data_type": "varchar"}]},
    ], {"table_bucket_arn": ARN}, None)
    assert ambiguous == []
    assert set(details) == {"s3t.nsa.events", "s3t.nsb.orders"}  # keyed by qualified name


# --- R3-F2: a partition-spec that cannot be established fails provisioning --

def test_partition_spec_unavailable_fails_the_bake(monkeypatch):
    from agents.duckdb_tools import PartitionSpecUnavailable

    def _raise(name, cfg, factory):
        raise PartitionSpecUnavailable("metadata unreadable")

    monkeypatch.setattr(onboard, "_s3tables_partition_fields", _raise)
    desc, reason = onboard._dp_s3tables_schema(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_factory())
    assert desc == "" and reason == "schema_unavailable"


# --- R3-F4: distinct reasons and retryability -------------------------------

def test_schema_unavailable_is_retryable_with_a_safe_detail():
    rec = onboard._provision_error_record(
        {"error": onboard._S3TABLES_SCHEMA_UNAVAILABLE_ERROR, "reason": "schema_unavailable"})
    assert rec["retryable"] is True
    assert rec["reason"] == "schema_unavailable"
    assert rec["detail"] == onboard._S3TABLES_SCHEMA_UNAVAILABLE_ERROR


def test_namespace_ambiguous_is_non_retryable_with_narrow_guidance():
    rec = onboard._provision_error_record(
        {"error": onboard._S3TABLES_NAMESPACE_AMBIGUOUS_ERROR, "reason": "namespace_ambiguous"})
    assert rec["retryable"] is False
    assert rec["reason"] == "namespace_ambiguous"
    assert "connection_config.tables" in rec["detail"]


def test_schema_collision_bake_reports_namespace_ambiguous():
    _desc, reason = onboard._dp_s3tables_schema(
        {"table_bucket_arn": ARN}, SYS, connection_factory=_two_namespace_factory())
    assert reason == "namespace_ambiguous"


def test_reprovision_keeps_old_agent_on_a_one_shot_discovery_failure(monkeypatch):
    import types

    class _FakeMgr:
        def __init__(self):
            self._agents = {SYS: "existing-agent"}

        def start_agent(self, **kwargs):
            self._agents[SYS] = "new-agent"
            return "mcp://new"

    mgr = _FakeMgr()
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    # First attempt: discovery fails transiently.
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("", "schema_unavailable"))
    ctx = types.SimpleNamespace(
        system_id=SYS, system_name="S", schema="s3tables",
        body=types.SimpleNamespace(connection_config={"table_bucket_arn": ARN}),
        cm_items=[], field_descriptions=[])
    out = onboard._dp_agent_s3tables_duckdb(ctx)
    assert out["reason"] == "schema_unavailable"
    assert mgr._agents.get(SYS) == "existing-agent"  # the working agent was NOT removed


# --- R4-F5: a partial discovery must not provision a half-baked schema ------

def test_partial_discovery_fails_provisioning(monkeypatch):
    # discover_schema truncated the table list (partial=True): the query tool
    # could still read a table whose rules were never baked, so provisioning
    # must fail closed (retryable, narrow connection_config.tables).
    def _partial(cfg, source_system="", connection_factory=None, max_tables=25):
        return {"system_id": source_system, "schema_name": "s3t", "connection_type": "S3 Tables",
                "tables": [{"name": f"flow.t{i}", "columns": [{"name": "c", "data_type": "varchar"}]}
                           for i in range(25)],
                "partial": True}

    monkeypatch.setattr(onboard, "discover_schema", _partial, raising=False)
    from agents import duckdb_tools
    monkeypatch.setattr(duckdb_tools, "discover_schema", _partial)
    desc, reason = onboard._dp_s3tables_schema({"table_bucket_arn": ARN}, SYS)
    assert desc == "" and reason == "schema_unavailable"


def test_complete_discovery_still_bakes(monkeypatch):
    def _complete(cfg, source_system="", connection_factory=None, max_tables=25):
        return {"system_id": source_system, "schema_name": "s3t", "connection_type": "S3 Tables",
                "tables": [{"name": "flow.segments",
                            "columns": [{"name": "observed_at", "data_type": "timestamp"}]}],
                "partial": False}

    from agents import duckdb_tools
    monkeypatch.setattr(duckdb_tools, "discover_schema", _complete)
    desc, reason = onboard._dp_s3tables_schema({"table_bucket_arn": ARN}, SYS)
    assert reason is None and "segments(" in desc


# --- R5-F2: re-provision must REPLACE the agent against the real manager ----
#
# An earlier fake raised unconditionally, so it could not expose the no-op
# the real manager causes: AgentManager.start_agent returns the OLD url
# immediately when the id is still registered. These fakes replicate that
# early-return contract so a replacement that never starts is caught.

class _Inst:
    """Minimal AgentInstance stand-in: carries the mcp_url start_agent returns."""
    def __init__(self, mcp_url):
        self.mcp_url = mcp_url


class _RealishMgr:
    """AgentManager.start_agent contract faithful enough for provisioning:
    returns the EXISTING instance's mcp_url unchanged when the id is still
    registered (the early return that makes a pop-less replace a no-op)."""
    def __init__(self, existing=None):
        self._agents = dict(existing or {})
        self.started = []

    def start_agent(self, *, system_id, **kwargs):
        self.started.append((system_id, kwargs))
        if system_id in self._agents:            # the real early return
            return self._agents[system_id].mcp_url
        self._agents[system_id] = _Inst("mcp://new")
        return "mcp://new"


def _s3t_ctx():
    import types
    return types.SimpleNamespace(
        system_id=SYS, system_name="S", schema="s3tables",
        body=types.SimpleNamespace(connection_config={"table_bucket_arn": ARN}),
        cm_items=[], field_descriptions=[])


def test_reprovision_actually_starts_replacement_against_real_manager(monkeypatch):
    # An agent is already registered under SYS. Re-provisioning must remove it
    # so start_agent builds the replacement; a pop-less path would hit the
    # manager's early return and silently keep the stale agent.
    mgr = _RealishMgr(existing={SYS: _Inst("mcp://old")})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    persisted = {}
    monkeypatch.setattr(onboard, "_persist_s3tables_connection",
                        lambda sid, cfg: persisted.__setitem__(sid, cfg))
    added = {}
    monkeypatch.setattr(onboard, "add_system_agent",
                        lambda sid, url: added.__setitem__(sid, url))

    out = onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    assert out is None                                   # success
    assert mgr.started and mgr.started[0][0] == SYS      # a start WAS attempted
    assert mgr._agents[SYS].mcp_url == "mcp://new"       # the replacement is live
    assert added.get(SYS) == "mcp://new"                 # new url registered
    assert SYS in persisted                              # new connection persisted


def test_reprovision_keeps_old_agent_when_start_agent_fails(monkeypatch):
    # Discovery SUCCEEDS; only replacement startup fails. The working agent and
    # its routing must survive, and no empty registration or connection write
    # may happen.
    class _FailingMgr(_RealishMgr):
        def start_agent(self, *, system_id, **kwargs):
            raise RuntimeError("runtime start failed")

    mgr = _FailingMgr(existing={SYS: _Inst("mcp://old")})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    persisted = {"called": False}
    monkeypatch.setattr(onboard, "_persist_s3tables_connection",
                        lambda sid, cfg: persisted.__setitem__("called", True))
    added = {}
    monkeypatch.setattr(onboard, "add_system_agent",
                        lambda sid, url: added.__setitem__(sid, url))

    out = onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    # A START failure (schema already baked) is an engine-neutral start error,
    # not a schema-read error.
    assert isinstance(out, dict) and out.get("reason") == "agent_start_unavailable"
    assert mgr._agents.get(SYS).mcp_url == "mcp://old"   # the working agent survived
    assert added.get(SYS) is None                        # no empty registration written
    assert persisted["called"] is False                 # no connection overwrite on failure


# --- R5-F2: the Athena provision branch has the same pop-before-start shape -
#
# _dp_agent_athena must also remove the old instance before start_agent (so the
# manager's early return does not no-op the replacement) and, on a start
# failure, restore the old instance and return a retryable error rather than
# registering an empty MCP url.

def _athena_ctx():
    import types
    return types.SimpleNamespace(
        system_id=SYS, system_name="S", schema="athena",
        body=types.SimpleNamespace(connection_config={"workgroup": "wg", "database": "db"}),
        cm_items=[], field_descriptions=[])


def _patch_athena_commons(monkeypatch):
    from agents.catalog import catalog
    monkeypatch.setattr(catalog, "get", lambda sid: {}, raising=False)
    monkeypatch.setattr(onboard, "_dp_athena_tables_desc", lambda ctx, cfg: "t(c: varchar)")
    monkeypatch.setattr(onboard, "_athena_system_prompt", lambda name: "prompt")
    monkeypatch.setattr(onboard, "_with_concept_mappings", lambda cfg, cm, fd: cfg)
    import agents.resolve_evidence as re_mod
    monkeypatch.setattr(re_mod, "_with_cross_account_credentials", lambda sid, cfg: cfg, raising=False)
    persisted = {"called": False}
    monkeypatch.setattr(onboard, "_persist_node_connection",
                        lambda *a, **k: persisted.__setitem__("called", True))
    added = {}
    monkeypatch.setattr(onboard, "add_system_agent",
                        lambda sid, url: added.__setitem__(sid, url))
    return persisted, added


def test_athena_reprovision_actually_starts_replacement(monkeypatch):
    persisted, added = _patch_athena_commons(monkeypatch)
    mgr = _RealishMgr(existing={SYS: _Inst("mcp://old")})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)

    out = onboard._dp_agent_athena(_athena_ctx())
    assert out is None
    assert mgr.started and mgr.started[0][0] == SYS      # a start WAS attempted
    assert mgr._agents[SYS].mcp_url == "mcp://new"       # the replacement is live
    assert added.get(SYS) == "mcp://new"                 # new url registered
    assert persisted["called"] is True                  # new connection persisted


def test_athena_reprovision_keeps_old_agent_when_start_fails(monkeypatch):
    persisted, added = _patch_athena_commons(monkeypatch)

    class _FailingMgr(_RealishMgr):
        def start_agent(self, *, system_id, **kwargs):
            raise RuntimeError("runtime start failed")

    mgr = _FailingMgr(existing={SYS: _Inst("mcp://old")})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)

    out = onboard._dp_agent_athena(_athena_ctx())
    # an Athena START failure gets its own engine-neutral error,
    # not the S3 Tables schema error, and never mentions S3 Tables.
    assert isinstance(out, dict) and out.get("reason") == "agent_start_unavailable"
    assert "S3 Tables" not in out.get("error", "")
    assert "schema could not be read" not in out.get("error", "")
    assert mgr._agents.get(SYS).mcp_url == "mcp://old"   # the working agent survived
    assert added.get(SYS) is None                        # no empty registration written
    assert persisted["called"] is False                 # no connection overwrite on failure


# --- R6-F3: a successful local re-provision stops the old server/port -----
#
# A local-manager lifecycle fake: instances carry a server and port, and the
# manager's own instance-specific cleanup (_stop_server) stops the server and
# releases the port WITHOUT popping any _agents entry. One old server/port must
# be released on success and retained on failure.

class _LocalInst:
    def __init__(self, mcp_url, port, server):
        self.mcp_url = mcp_url
        self.port = port
        self.server = server
        self.system_id = SYS


class _FakeServer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _LocalMgr:
    """A local AgentManager stand-in with a real process/port lifecycle. Honors
    start_agent's existing-id early return, allocates a new port per start, and
    implements the instance-specific _stop_server the onboard helper must use."""
    def __init__(self, existing=None):
        self._agents = dict(existing or {})
        self._used_ports = {i.port for i in (existing or {}).values()}
        self._next_port = 8150

    def _release_port(self, port):
        self._used_ports.discard(port)

    def _stop_server(self, inst):
        try:
            inst.server.stop()
        finally:
            self._release_port(inst.port)

    def start_agent(self, *, system_id, **kwargs):
        if system_id in self._agents:
            return self._agents[system_id].mcp_url
        port = self._next_port
        self._next_port += 1
        self._used_ports.add(port)
        self._agents[system_id] = _LocalInst("mcp://new", port, _FakeServer())
        return "mcp://new"


def test_reprovision_stops_old_local_process_and_releases_port_on_success(monkeypatch):
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    monkeypatch.setattr(onboard, "_persist_s3tables_connection", lambda sid, cfg: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: None)

    out = onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    assert out is None                                   # success
    assert old_proc.stopped is True                      # old process stopped
    assert 8100 not in mgr._used_ports                   # old port released
    assert mgr._agents[SYS].mcp_url == "mcp://new"       # new instance live
    assert mgr._agents[SYS].server.stopped is False      # new server untouched


def test_reprovision_retains_old_local_process_on_start_failure(monkeypatch):
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)

    class _FailingLocalMgr(_LocalMgr):
        def start_agent(self, *, system_id, **kwargs):
            raise RuntimeError("runtime start failed")

    mgr = _FailingLocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    monkeypatch.setattr(onboard, "_persist_s3tables_connection", lambda sid, cfg: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: None)

    out = onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    assert isinstance(out, dict) and out.get("reason") == "agent_start_unavailable"
    assert old_proc.stopped is False                     # old process retained
    assert 8100 in mgr._used_ports                       # old port retained
    assert mgr._agents[SYS] is old                        # old instance restored


# --- R7-F3: stop the old agent only AFTER registration + persistence --------

def test_registration_failure_keeps_old_process_and_restores_route(monkeypatch):
    # The route switch (add_system_agent) failing -- a lost lease after persist
    # -- must roll back fully: the old process keeps running, its port stays
    # held, the manager entry is the old one, and the ROUTE is restored to the
    # old url. The error propagates.
    import pytest
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    monkeypatch.setattr(onboard, "_persist_s3tables_connection", lambda sid, cfg: None)
    route = {SYS: "mcp://old"}

    def _register(sid, url):
        if url == "mcp://new":                 # the forward switch to the new url fails
            raise RuntimeError("lease lost before orchestrator registration")
        route[sid] = url                        # the rollback's restore to the old url

    monkeypatch.setattr(onboard, "add_system_agent", _register)

    with pytest.raises(RuntimeError, match="lease lost"):
        onboard._dp_agent_s3tables_duckdb(_s3t_ctx())

    assert old_proc.stopped is False                 # old process still running
    assert 8100 in mgr._used_ports                   # old port retained
    assert mgr._agents[SYS] is old                   # old manager entry restored
    assert route[SYS] == "mcp://old"                 # old route restored


def test_persistence_failure_keeps_old_process_and_leaves_route_untouched(monkeypatch):
    # Persist runs FIRST; its failure must leave the route untouched (never
    # switched to the new url) and the old agent fully intact.
    import pytest
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    route = {SYS: "mcp://old"}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))

    def _persist_fails(_sid, _cfg):
        raise RuntimeError("node write failed")

    monkeypatch.setattr(onboard, "_persist_s3tables_connection", _persist_fails)

    with pytest.raises(RuntimeError, match="node write failed"):
        onboard._dp_agent_s3tables_duckdb(_s3t_ctx())

    assert old_proc.stopped is False
    assert 8100 in mgr._used_ports
    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"                 # route never switched


def test_old_process_stopped_only_after_registration_and_persistence(monkeypatch):
    # Ordering: the old process must not be stopped until BOTH add_system_agent
    # and the persist have run.
    order = []
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)

    class _OrderMgr(_LocalMgr):
        def _stop_server(self, inst):
            order.append("stop_old")
            super()._stop_server(inst)

    mgr = _OrderMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))
    monkeypatch.setattr(onboard, "add_system_agent",
                        lambda sid, url: order.append("register"))
    monkeypatch.setattr(onboard, "_persist_s3tables_connection",
                        lambda sid, cfg: order.append("persist"))

    out = onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    assert out is None
    # Persist FIRST, then switch the route, then retire the old process (R8-F3).
    assert order == ["persist", "register", "stop_old"]
    assert old_proc.stopped is True


def test_athena_registration_failure_keeps_old_process_and_restores_route(monkeypatch):
    # The Athena branch applies the same ordering (R7-F3).
    import pytest
    persisted, added = _patch_athena_commons(monkeypatch)
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)

    def _reg_fails(_sid, _url):
        raise RuntimeError("lease lost before orchestrator registration")

    monkeypatch.setattr(onboard, "add_system_agent", _reg_fails)

    with pytest.raises(RuntimeError, match="lease lost"):
        onboard._dp_agent_athena(_athena_ctx())

    assert old_proc.stopped is False
    assert 8100 in mgr._used_ports
    assert mgr._agents[SYS] is old


def test_athena_stops_old_only_after_persist_then_register(monkeypatch):
    # Athena ordering: persist FIRST, then switch the route, then retire the old
    # process.
    _patch_athena_commons(monkeypatch)
    order = []

    class _OrderMgr(_LocalMgr):
        def _stop_server(self, inst):
            order.append("stop_old")
            super()._stop_server(inst)

    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _OrderMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: order.append("register"))
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *a, **k: order.append("persist"))

    assert onboard._dp_agent_athena(_athena_ctx()) is None
    assert order == ["persist", "register", "stop_old"]
    assert old_proc.stopped is True


def test_athena_persistence_failure_leaves_route_untouched(monkeypatch):
    # Athena persist failing (persist-first) leaves the route untouched and the
    # old agent intact.
    import pytest
    _patch_athena_commons(monkeypatch)
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))
    monkeypatch.setattr(onboard, "_persist_node_connection",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("persist failed")))

    with pytest.raises(RuntimeError, match="persist failed"):
        onboard._dp_agent_athena(_athena_ctx())

    assert old_proc.stopped is False
    assert 8100 in mgr._used_ports
    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"


# --- R9-F3: registration failure rolls the persisted config back to prior ----
#
# The durable store is modelled by a `persisted` dict: persist writes the new
# config into it, the snapshot captures the prior value before persist, and the
# rollback restores that snapshot. After a route-registration failure the
# persisted state must equal the pre-attempt state.

def _wire_durable(monkeypatch, persisted):
    # Snapshot reads the durable store; restore writes it back (what the real
    # _snapshot/_restore do against the catalog + canonical write).
    monkeypatch.setattr(onboard, "_snapshot_persisted_connection",
                        lambda sid: {"value": persisted.get(sid)})
    monkeypatch.setattr(onboard, "_restore_persisted_connection",
                        lambda sid, prior: persisted.__setitem__(sid, (prior or {}).get("value")))


@pytest.mark.parametrize("engine", ["s3tables", "athena"])
def test_registration_failure_rolls_back_persisted_state(monkeypatch, engine):
    import pytest as _pt
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    mgr = _LocalMgr(existing={SYS: old})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    persisted = {SYS: "old-config"}
    _wire_durable(monkeypatch, persisted)

    def register(sid, url):
        if url == "mcp://new":                 # forward route switch fails
            raise RuntimeError("register failed")
        route[sid] = url                        # rollback restores the old url

    monkeypatch.setattr(onboard, "add_system_agent", register)
    if engine == "s3tables":
        _patch_s3tables(monkeypatch)
        monkeypatch.setattr(onboard, "_persist_s3tables_connection",
                            lambda sid, cfg: persisted.__setitem__(sid, "new-config"))
        invoke = lambda: onboard._dp_agent_s3tables_duckdb(_s3t_ctx())
    else:
        _patch_athena_commons(monkeypatch)
        monkeypatch.setattr(onboard, "add_system_agent", register)
        monkeypatch.setattr(onboard, "_persist_node_connection",
                            lambda sid, **kw: persisted.__setitem__(sid, "new-config"))
        invoke = lambda: onboard._dp_agent_athena(_athena_ctx())

    with _pt.raises(RuntimeError, match="register failed"):
        invoke()

    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"
    assert persisted[SYS] == "old-config"        # durable config rolled back
    assert old_proc.stopped is False


def _patch_s3tables(monkeypatch):
    monkeypatch.setattr(onboard, "_dp_s3tables_schema",
                        lambda cfg, sid, connection_factory=None: ("segments(observed_at: timestamp)", None))


def test_snapshot_captures_complete_record_including_connection_type(monkeypatch):
    # R10-F2: the snapshot reads the FULL node record (config + top-level
    # connection_type + secret_arn), whether it existed, and the own-secret
    # AWSCURRENT version id.
    from agents import connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: {"connection_config": {"database": "old"},
                                     "connection_type": "Athena",
                                     "secret_arn": "arn:old"})
    monkeypatch.setattr(cr, "_own_secret_current_version", lambda sid: "v-prev")
    snap = onboard._snapshot_persisted_connection(SYS)
    assert snap["existed"] is True
    assert snap["connection_config"] == {"database": "old"}
    assert snap["connection_type"] == "Athena"      # top-level type preserved
    assert snap["secret_arn"] == "arn:old"
    assert snap["own_secret_version"] == "v-prev"


def test_snapshot_marks_absent_record(monkeypatch):
    from agents import connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    monkeypatch.setattr(cr, "_own_secret_current_version", lambda sid: None)
    snap = onboard._snapshot_persisted_connection(SYS)
    assert snap["existed"] is False
    assert snap["own_secret_version"] is None


def test_restore_rewrites_exact_record_including_connection_type(monkeypatch):
    # R10-F2: restore of an existing record rewrites config + connection_type +
    # secret_arn via the canonical node write (require_node), and stages the own
    # secret back to its prior version.
    from agents import connection_resolver as cr
    wrote = {}
    monkeypatch.setattr(cr, "_persist_node_connection_config",
                        lambda sid, ns, ct, arn, *, require_node=False:
                        wrote.update(sid=sid, ns=ns, ct=ct, arn=arn, require_node=require_node))
    staged = {}
    monkeypatch.setattr(cr, "_restore_own_secret_version",
                        lambda sid, ver, written: staged.update(sid=sid, ver=ver))
    snap = {"existed": True, "connection_config": {"database": "old"},
            "connection_type": "Athena", "secret_arn": "arn:old",
            "own_secret_version": "v-prev"}
    onboard._restore_persisted_connection(SYS, snap)
    assert wrote["ns"] == {"database": "old"} and wrote["ct"] == "Athena"
    assert wrote["arn"] == "arn:old" and wrote["require_node"] is True
    assert staged["ver"] == "v-prev"               # own secret staged back first


def test_restore_deletes_record_that_did_not_exist(monkeypatch):
    # R10-F2: an absent prior record is restored to ABSENCE (node fields removed).
    from agents import connection_resolver as cr
    deleted = {}
    monkeypatch.setattr(cr, "_delete_node_connection", lambda sid: deleted.__setitem__("sid", sid))
    monkeypatch.setattr(cr, "_restore_own_secret_version", lambda sid, ver, written: None)
    onboard._restore_persisted_connection(SYS, {"existed": False, "own_secret_version": None})
    assert deleted["sid"] == SYS


def test_restore_none_snapshot_is_a_noop(monkeypatch):
    from agents import connection_resolver as cr
    called = {"restore": False}
    monkeypatch.setattr(cr, "restore_durable_record",
                        lambda sid, snap: called.__setitem__("restore", True))
    onboard._restore_persisted_connection(SYS, None)
    assert called["restore"] is False            # None -> nothing restored


@pytest.mark.no_offline_secret_stub
def test_own_secret_rollback_moves_stage_to_prior_version(monkeypatch):
    # R11-F1: a changed own secret is rolled back by moving AWSCURRENT to the
    # prior version (UpdateSecretVersionStage), but ONLY while the lease is held
    # and AWSCURRENT still equals the version THIS attempt wrote ("v-now").
    from agents import connection_resolver as cr
    import agents.lease_guard as lease_guard
    calls = {}

    class _FakeSM:
        def describe_secret(self, SecretId):
            return {"VersionIdsToStages": {"v-now": ["AWSCURRENT"]}}

        def update_secret_version_stage(self, **kw):
            calls.update(kw)

    monkeypatch.setattr(cr, "_secrets_client", lambda: _FakeSM())
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    monkeypatch.setattr(lease_guard, "check_write_guard", lambda: None)
    cr._restore_own_secret_version("sys-x", "v-prev", "v-now")
    assert calls["VersionStage"] == "AWSCURRENT"
    assert calls["MoveToVersionId"] == "v-prev"
    assert calls["RemoveFromVersionId"] == "v-now"


@pytest.mark.no_offline_secret_stub
def test_own_secret_rollback_deletes_a_secret_this_attempt_created(monkeypatch):
    # No prior own secret but one exists now whose AWSCURRENT is the version this
    # attempt wrote ("v-now") and the lease is held -> this attempt created it:
    # delete (R11-F1 lease + CAS gate holds).
    from agents import connection_resolver as cr
    import agents.lease_guard as lease_guard
    deleted = {}

    class _FakeSM:
        def describe_secret(self, SecretId):
            return {"VersionIdsToStages": {"v-now": ["AWSCURRENT"]}}

        def delete_secret(self, **kw):
            deleted.update(kw)

    monkeypatch.setattr(cr, "_secrets_client", lambda: _FakeSM())
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    monkeypatch.setattr(lease_guard, "check_write_guard", lambda: None)
    cr._restore_own_secret_version("sys-x", None, "v-now")
    assert deleted["ForceDeleteWithoutRecovery"] is True


def test_commit_does_not_succeed_when_strict_persist_loses_lease(monkeypatch):
    # R10-F1: the strict persist checks the lease BEFORE the secret write and
    # propagates; _commit_replacement rolls back and never switches the route or
    # stops the old agent, and no secret value is mutated.
    import pytest
    from agents import connection_resolver as cr
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    new = _LocalInst("mcp://new", 8150, _FakeServer())
    mgr = _LocalMgr(existing={SYS: new})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    secret_state = {"written": False}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))
    monkeypatch.setattr(onboard, "_snapshot_persisted_connection", lambda sid: {"existed": True})
    monkeypatch.setattr(onboard, "_restore_persisted_connection", lambda sid, prior: None)
    # check_write_guard (imported by write_connection from lease_guard) raises
    # (lease lost) BEFORE any secret write.
    import agents.lease_guard as lease_guard
    monkeypatch.setattr(lease_guard, "check_write_guard",
                        lambda: (_ for _ in ()).throw(RuntimeError("lease lost")))
    monkeypatch.setattr(cr, "_write_connection_secret",
                        lambda *a, **k: secret_state.__setitem__("written", True) or ("", {}))

    def persist():
        onboard._persist_node_connection(
            SYS, non_secret={"connection_type": "Athena", "database": "new"},
            secret={"external_id": "new-secret"}, secret_arn=None, strict=True)

    with pytest.raises(RuntimeError, match="lease lost"):
        onboard._commit_replacement(SYS, "mcp://new", old, persist)

    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"
    assert old_proc.stopped is False
    assert secret_state["written"] is False      # lease checked before the secret write


def test_commit_does_not_switch_route_when_strict_write_loses_lease(monkeypatch):
    # Folded from the reviewer's real-path lease probe (R10-F1): the strict
    # replacement persist loses the lease during the durable write. The commit
    # must NOT switch the route or stop the old agent, and the credential value
    # must not be mutated (the lease is checked before the secret write).
    import pytest
    from agents import connection_resolver as cr
    import agents.lease_guard as lease_guard

    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    new = _LocalInst("mcp://new", 8150, _FakeServer())
    mgr = _LocalMgr(existing={SYS: new})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    secret_state = {SYS: "old-secret"}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))
    monkeypatch.setattr(onboard, "_snapshot_persisted_connection", lambda sid: {"existed": True})
    monkeypatch.setattr(onboard, "_restore_persisted_connection", lambda sid, prior: None)
    # The lease is lost: the strict guard raises before any secret write.
    monkeypatch.setattr(lease_guard, "check_write_guard",
                        lambda: (_ for _ in ()).throw(RuntimeError("lease lost")))

    def _would_write_secret(*a, **k):
        secret_state[SYS] = "new-secret"            # must NEVER run under strict
        return ("", {})

    monkeypatch.setattr(cr, "_write_connection_secret", _would_write_secret)

    def persist():
        onboard._persist_node_connection(
            SYS, non_secret={"connection_type": "Athena", "database": "new"},
            secret={"external_id": "new-secret"}, secret_arn=None, strict=True)

    with pytest.raises(RuntimeError, match="lease lost"):
        onboard._commit_replacement(SYS, "mcp://new", old, persist)

    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"
    assert old_proc.stopped is False
    assert secret_state[SYS] == "old-secret"        # credential value never mutated


# --- Round 11: hardening the durable rollback ----------
# Three real-suite tests standing in for the reviewer's lost probes. Each
# asserts the behavior the R10 review named, and each was written to FAIL
# against the R10 code before the fix.


def test_snapshot_read_failure_is_not_misclassified_as_absent(monkeypatch):
    # R11-F2: a graph read fault while snapshotting the prior durable record
    # must NOT be recorded as "the record was absent". snapshot_durable_record
    # reads with a CHECKED query and RAISES, so _commit_replacement aborts the
    # replacement BEFORE any persist, keeps the old agent serving and routed,
    # and returns the retryable agent-start error. (R10 swallowed the read and
    # returned existed=False, which would let the rollback delete a record that
    # may still exist.)
    from agents import connection_resolver as cr
    from agents.db import GraphQueryError

    # The checked node read fails (e.g. Neptune transient / query rejected).
    def _boom(system_id):
        raise GraphQueryError("ServiceUnavailable", "graph read failed")

    monkeypatch.setattr(cr, "_fetch_node_config_checked", _boom)

    # A read fault must surface as an abort, never as a benign "absent" record.
    import pytest
    with pytest.raises(cr.DurableSnapshotError):
        cr.snapshot_durable_record(SYS)

    # End to end: the started-but-unrouted replacement is rolled back to the old
    # agent and the provision returns the retryable error, with no persist run.
    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    new = _LocalInst("mcp://new", 8150, _FakeServer())
    mgr = _LocalMgr(existing={SYS: new})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    persisted = {"ran": False}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))

    def persist():
        persisted["ran"] = True

    out = onboard._commit_replacement(SYS, "mcp://new", old, persist)
    assert out == onboard._agent_start_error()   # retryable, engine-neutral
    assert persisted["ran"] is False             # aborted BEFORE any persist
    assert mgr._agents[SYS] is old               # old agent restored
    assert route[SYS] == "mcp://old"             # route never switched
    assert old_proc.stopped is False             # old process never stopped


@pytest.mark.no_offline_secret_stub
def test_secret_rollback_does_not_move_stage_from_unknown_newer_version(monkeypatch):
    # R11-F1: the secret rollback is lease + CAS safe. When AWSCURRENT is a
    # version this attempt did NOT write (a newer lease owner staged its own
    # value after ours), the rollback must leave the secret alone: no stage
    # move, no delete. (R10 moved/removed whatever version was current.)
    from agents import connection_resolver as cr
    moved = {"staged": False, "deleted": False}

    class _FakeSM:
        def describe_secret(self, SecretId):
            # AWSCURRENT is "v-newer": NOT the version this attempt wrote.
            return {"VersionIdsToStages": {"v-newer": ["AWSCURRENT"]}}

        def update_secret_version_stage(self, **kw):
            moved["staged"] = True

        def delete_secret(self, **kw):
            moved["deleted"] = True

    monkeypatch.setattr(cr, "_secrets_client", lambda: _FakeSM())
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    # Lease is held throughout.
    import agents.lease_guard as lease_guard
    monkeypatch.setattr(lease_guard, "check_write_guard", lambda: None)

    # This attempt wrote "v-mine"; the prior (pre-attempt) version was "v-prev".
    cr._restore_own_secret_version("sys-x", "v-prev", "v-mine")
    assert moved["staged"] is False              # a newer owner holds AWSCURRENT
    assert moved["deleted"] is False             # never delete another's version

    # And the delete-created-secret path is equally CAS-guarded: this attempt
    # created a secret (prior None) but AWSCURRENT is a newer owner's version.
    cr._restore_own_secret_version("sys-x", None, "v-mine")
    assert moved["deleted"] is False


def test_absent_record_rollback_requires_confirmed_delete(monkeypatch):
    # R11-F2: restoring a node to ABSENCE (this attempt created the record) must
    # use a CONFIRMED graph write. An unconfirmed write (neptune_write -> False)
    # must RAISE, not be silently dropped, so the caller knows durable state was
    # not returned to absence. (R10 fired the REMOVE and ignored the result.)
    from agents import connection_resolver as cr
    import pytest

    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    monkeypatch.setattr(cr, "_neptune_write", lambda cypher, params: False)
    with pytest.raises(cr.DurableRestoreError):
        cr._delete_node_connection("sys-x")

    confirmed = {"wrote": False}
    monkeypatch.setattr(cr, "_neptune_write",
                        lambda cypher, params: confirmed.__setitem__("wrote", True) or True)
    cr._delete_node_connection("sys-x")          # confirmed write -> no raise
    assert confirmed["wrote"] is True


# --- Round 12: the own-secret snapshot is checked too --
# Folded from the reviewer's probe (review-r11/test_secret_snapshot_r11.py).


@pytest.mark.no_offline_secret_stub
def test_unreadable_own_secret_snapshot_aborts_before_persist(monkeypatch):
    # R12-F1: the node snapshot succeeds but DescribeSecret fails with a
    # NON-absent error. _own_secret_current_version must not report absence; the
    # snapshot raises DurableSnapshotError, so _commit_replacement aborts before
    # any persist, restores the old agent and route, stops the new process, and
    # returns the retryable engine-neutral error. (R11 turned every secret
    # failure into None, which looks like confirmed absence.)
    from agents import connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config_checked", lambda sid: {
        "connection_config": {"database": "old"}, "connection_type": "Athena",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/x",
        "connection_string": None})

    class _UnreadableSM:
        def describe_secret(self, SecretId):
            raise RuntimeError("secrets manager unavailable")

    monkeypatch.setattr(cr, "_secrets_client", lambda: _UnreadableSM())

    old_proc = _FakeServer()
    old = _LocalInst("mcp://old", 8100, old_proc)
    new_proc = _FakeServer()
    new = _LocalInst("mcp://new", 8150, new_proc)
    mgr = _LocalMgr(existing={SYS: new})
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    route = {SYS: "mcp://old"}
    monkeypatch.setattr(onboard, "add_system_agent", lambda sid, url: route.__setitem__(sid, url))
    persisted = {"ran": False}

    def persist():
        persisted["ran"] = True

    out = onboard._commit_replacement(SYS, "mcp://new", old, persist)
    assert out == onboard._agent_start_error()   # retryable, aborted
    assert persisted["ran"] is False             # before any persist
    assert mgr._agents[SYS] is old
    assert route[SYS] == "mcp://old"
    assert old_proc.stopped is False
    assert new_proc.stopped is True              # the started replacement is torn down


@pytest.mark.no_offline_secret_stub
def test_own_secret_snapshot_absent_only_on_resource_not_found(monkeypatch):
    # R12-F1: a confirmed ResourceNotFoundException is the ONLY secret failure
    # that snapshots as absence (None); any other failure raises.
    from agents import connection_resolver as cr
    import pytest

    class _NotFound(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "ResourceNotFoundException"}}

    class _AbsentSM:
        def describe_secret(self, SecretId):
            raise _NotFound()

    monkeypatch.setattr(cr, "_secrets_client", lambda: _AbsentSM())
    assert cr._own_secret_current_version("sys-x") is None   # confirmed absent

    class _ThrottledSM:
        def describe_secret(self, SecretId):
            raise RuntimeError("Throttling")

    monkeypatch.setattr(cr, "_secrets_client", lambda: _ThrottledSM())
    with pytest.raises(cr.DurableSnapshotError):
        cr._own_secret_current_version("sys-x")  # transient fault is not absence


@pytest.mark.no_offline_secret_stub
def test_existing_secret_without_awscurrent_is_not_reported_absent(monkeypatch):
    # R13-F1: DescribeSecret succeeded, so the secret EXISTS; with no AWSCURRENT
    # stage its prior version cannot be named, which must abort the snapshot
    # rather than read as absence (a rollback would then delete the secret).
    from agents import connection_resolver as cr
    import pytest

    class _PendingOnlySM:
        def describe_secret(self, SecretId):
            return {"VersionIdsToStages": {"v-pending": ["AWSPENDING"]}}

    monkeypatch.setattr(cr, "_secrets_client", lambda: _PendingOnlySM())
    with pytest.raises(cr.DurableSnapshotError):
        cr._own_secret_current_version("sys-x")


def test_existing_system_without_connection_fields_is_snapshot_as_absent(monkeypatch):
    # R12-F2: a System node that exists but carries NONE of the four connection
    # properties snapshots as absent (existed=False), so rollback restores
    # absence via the confirmed delete rather than writing an empty record.
    # (R11 used existed = node is not None, which recorded existed=True here.)
    from agents import connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config_checked", lambda sid: {
        "connection_config": None, "connection_type": None,
        "secret_arn": None, "connection_string": None})
    monkeypatch.setattr(cr, "_own_secret_current_version", lambda sid: None)
    assert cr.snapshot_durable_record(SYS)["existed"] is False

    # A node that DOES carry a connection property still snapshots as existing.
    monkeypatch.setattr(cr, "_fetch_node_config_checked", lambda sid: {
        "connection_config": None, "connection_type": "Athena",
        "secret_arn": None, "connection_string": None})
    assert cr.snapshot_durable_record(SYS)["existed"] is True
