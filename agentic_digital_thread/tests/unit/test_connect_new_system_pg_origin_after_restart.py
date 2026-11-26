# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: a user PostgreSQL system's origin after a restart.
Offline: no AWS, no Neptune, no PostgreSQL, no network.

* a USER PostgreSQL system gets its origin back after a restart even
  when its database is unreachable (the marker comes from its own bound
  secret, before table introspection). When a PostgreSQL entry is still
  unmarked but its node points at this system's own ``adt/conn/<id>``, the
  discover / provision connection phase / runtime start read that secret:
  bound to a user -> marked user, user path (validated, pinned DSN; its own
  pointer kept); unbound -> the demo path (an admin-edited demo system);
  unreadable -> the retryable "could not be read" error, nothing written,
  nothing started. Never the demo DB secret, never an unpinned connect.
"""
from __future__ import annotations

import json
import os
import types
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.server as server  # noqa: E402 - pytest blocks sockets; no AWS call
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_reonboard_source import _node_row, _restart  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401 - fixtures
    DEPLOYED, _no_deploy_password, deployed)
from tests.unit.test_connect_new_system_locked_budget_and_provision import (  # noqa: E402,F401 - fixtures
    _pg_ctx, pg_runtime)

PINNED_IP = "203.0.113.10"
UNREADABLE = "could not be read"
PG_NODE = {"host": "db.example.internal", "port": "5432", "dbname": "lims", "schema": "lims",
           "search_path": "lims"}


def _restarted_user_pg(cat, graph, *, marked: bool = False):
    """A provisioned user PostgreSQL system after an ECS restart: the node keeps
    its own pointer; the catalog entry is rebuilt without pointer or origin."""
    _register_pg()
    _restart(cat, {"connection_type": "PostgreSQL", "schema": "lims",
                   **({"connection_origin": "user"} if marked else {})})
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")


@pytest.fixture
def no_demo_secret(monkeypatch):
    """Provision's demo branch would point the node at the shared demo DB secret."""
    monkeypatch.setattr(cr, "db_secret_arn_for_schema", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the demo DB secret was used for a user registration")))


# ── (1) the restore re-marks a user PG system before introspection ─────────

def _restore_with_db_unavailable(cat, monkeypatch):
    ns = {"id": SID, "name": "Quality Lab", "schema": "lims", "plant": "",
          "connection_type": "PostgreSQL", "system_type": "", "status": "onboarded"}
    mgr = MagicMock()
    monkeypatch.setattr(server, "catalog", cat)
    monkeypatch.setattr(server, "neptune_query", MagicMock(side_effect=[[ns], []]))
    monkeypatch.setattr(server, "pg", lambda *a, **k: {"columns": [], "rows": [],
                                                       "error": "unavailable"})
    monkeypatch.setattr(server, "_agent_manager", mgr)
    monkeypatch.setattr(server, "add_system_agent", MagicMock())
    monkeypatch.setattr(server, "_reconcile_restored_sql_provenance", MagicMock())
    monkeypatch.setattr(server, "_restore_seed_core_concepts", lambda: None)
    server._restore()
    return mgr


def test_m1_restore_re_marks_a_user_pg_system_when_its_database_is_unavailable(
        cat, secrets, graph, deployed, monkeypatch):
    _register_pg()
    cat.remove(SID)                                   # the restart: catalog rebuilt from the graph
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    mgr = _restore_with_db_unavailable(cat, monkeypatch)
    assert cat.get(SID).get("connection_origin") == "user", cat.get(SID)
    mgr.start_agent.assert_not_called()               # no tables: no agent, as before


def test_guard_m1_restore_leaves_a_demo_pg_system_unmarked(cat, secrets, graph, deployed,
                                                           monkeypatch):
    seed = "arn:aws:secretsmanager:us-east-1:111122223333:secret:seed-db-AbCdEf"
    graph[SID] = _node_row(PG_NODE, seed, "PostgreSQL")
    _restore_with_db_unavailable(cat, monkeypatch)
    assert not cat.get(SID).get("connection_origin"), cat.get(SID)


# ── (2) discover: an unmarked entry with an own pointer on the node ────────

def test_m1_discover_of_an_unmarked_entry_with_an_own_bound_secret_is_pinned(
        cat, secrets, graph, deployed):
    _restarted_user_pg(cat, graph)
    # onboard_discover fills conn_str from resolve_connection: the user's full DSN.
    assert onboard._pg_discover_target(SID, DSN, "lims") == (DSN, PINNED_IP, "")
    assert onboard._pg_discover_target(SID, "", "lims") == (DSN, PINNED_IP, "")
    assert cat.get(SID).get("connection_origin") == "user"


def test_m1_discover_of_an_unmarked_entry_with_an_unreadable_own_secret_is_refused(
        cat, secrets, graph, deployed, monkeypatch):
    _restarted_user_pg(cat, graph)
    secrets.pop(CONN_ARN)
    connects: list = []
    monkeypatch.setattr(onboard.pgdriver, "connect", lambda *a, **k: connects.append(a))
    for conn_str in (DSN, ""):
        dsn, hostaddr, err = onboard._pg_discover_target(SID, conn_str, "lims")
        assert (dsn, hostaddr) == ("", None) and UNREADABLE in err, (dsn, hostaddr, err)
    assert connects == [] and not cat.get(SID).get("connection_origin")


def test_guard_m1_unmarked_entry_with_an_unbound_own_secret_takes_the_demo_path(
        cat, secrets, graph, deployed):
    """An admin-edited demo system: its own adt/conn/<id> carries no user binding."""
    cat.add({"id": SID, "name": "Quality Lab", "connection_type": "PostgreSQL",
             "schema": "lims", "status": "onboarded"})
    secrets[CONN_ARN] = {"token": "x"}
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    from agents.demo.db_routing import demo_pg_dsn
    assert onboard._pg_discover_target(SID, "", "lims") == (demo_pg_dsn("lims"), None, "")
    assert not cat.get(SID).get("connection_origin")


def test_guard_m1_unmarked_entry_with_a_shared_pointer_reads_no_secret(
        cat, secrets, graph, deployed, monkeypatch):
    seed = "arn:aws:secretsmanager:us-east-1:111122223333:secret:seed-db-AbCdEf"
    cat.add({"id": SID, "name": "Quality Lab", "connection_type": "PostgreSQL",
             "schema": "lims", "status": "onboarded"})
    graph[SID] = _node_row(PG_NODE, seed, "PostgreSQL")
    monkeypatch.setattr(cr, "_read_registration_secret", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a registration secret was read for a demo system")))
    dsn, hostaddr, err = onboard._pg_discover_target(SID, "postgresql://h/db", "lims")
    assert (dsn, hostaddr, err) == ("postgresql://h/db", None, "")


# ── (2) provision connection phase ─────────────────────────────────────────

def test_m1_provision_of_an_unmarked_entry_with_an_own_bound_secret_keeps_its_pointer(
        cat, secrets, graph, deployed, no_demo_secret):
    _restarted_user_pg(cat, graph)
    ctx = _pg_ctx()
    assert onboard._dp_start_agent(ctx) is None
    onboard._dp_persist_sql_connection(ctx)
    assert graph[SID]["secret_arn"] == CONN_ARN, graph[SID]
    assert json.loads(graph[SID]["connection_config"])["host"] == "db.example.internal"
    assert ctx.registered_pg_dsn == DSN and cat.get(SID).get("connection_origin") == "user"


def test_m1_provision_of_an_unmarked_entry_with_an_unreadable_own_secret_writes_nothing(
        cat, secrets, graph, deployed, no_demo_secret):
    _restarted_user_pg(cat, graph)
    secrets.pop(CONN_ARN)
    before = dict(graph[SID])
    err = onboard._dp_start_agent(_pg_ctx())
    assert isinstance(err, dict) and UNREADABLE in err.get("error", ""), err
    assert graph[SID] == before


def test_m1_the_provision_coordinator_stops_on_a_start_error(monkeypatch):
    """An error dict from the runtime start ends the provision there (no
    finalize, no 'provisioned' result)."""
    ran: list = []
    monkeypatch.setattr(onboard, "_make_provision_state",
                        lambda body: types.SimpleNamespace(system_id=SID, body=body))
    for name in ("_dp_collect_tables", "_dp_discover_athena_physical", "_dp_schema_entries",
                 "_dp_field_mappings", "_dp_athena_physical_fields",
                 "_dp_discovery_completeness", "_dp_reapply_user_corrections",
                 "_dp_identity_key_edges", "_dp_mint_unmapped", "_dp_internal_joinpaths",
                 "_dp_mirrored_measure_tags", "_dp_settle_field_concepts",
                 "_dp_valueoverlap_joinpaths", "_dp_samerow_joinpaths", "_dp_identity_rules",
                 "_dp_rule_joinpaths", "_dp_cross_system_vms", "_dp_equipment_vms_legacy",
                 "_dp_authority_rules", "_dp_onboarding_log"):
        monkeypatch.setattr(onboard, name, lambda ctx: None)
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda ctx: None)
    monkeypatch.setattr(onboard, "_dp_start_pg_agent", lambda ctx: {"error": "start refused"})
    monkeypatch.setattr(onboard, "_dp_finalize", lambda ctx: ran.append("finalize"))
    monkeypatch.setattr(onboard, "_dp_result", lambda ctx: {"status": "provisioned"})
    assert onboard._do_provision_base(types.SimpleNamespace(system_id=SID)) == \
        {"error": "start refused"}
    assert ran == []


# ── (2) runtime start ──────────────────────────────────────────────────────

def test_m1_start_of_an_unmarked_entry_with_an_own_bound_secret_uses_its_database(
        cat, secrets, graph, deployed, pg_runtime):
    _restarted_user_pg(cat, graph)
    assert onboard._dp_start_pg_agent(_pg_ctx()) is None
    kw = pg_runtime.started[-1]
    params = kw["conn_params"]
    assert (params["host"], params["user"], params["password"]) == \
        ("db.example.internal", "lab_reader", PASSWORD), params
    _no_deploy_password(params)
    assert kw["_lock_held"] is True


def test_m1_start_of_an_unmarked_entry_with_an_unreadable_own_secret_starts_nothing(
        cat, secrets, graph, deployed, pg_runtime):
    _restarted_user_pg(cat, graph)
    secrets.pop(CONN_ARN)
    err = onboard._dp_start_pg_agent(_pg_ctx())
    assert isinstance(err, dict) and UNREADABLE in err.get("error", ""), err
    assert pg_runtime.started == []


def test_guard_m1_start_of_a_restored_demo_pg_system_is_unchanged(
        cat, secrets, graph, deployed, pg_runtime, monkeypatch):
    seed = "arn:aws:secretsmanager:us-east-1:111122223333:secret:seed-db-AbCdEf"
    cat.add({"id": SID, "name": "Quality Lab", "connection_type": "PostgreSQL",
             "schema": "lims", "status": "onboarded"})
    graph[SID] = _node_row(PG_NODE, seed, "PostgreSQL")
    monkeypatch.setattr(cr, "registered_pg_dsn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registered_pg_dsn called for a demo system")))
    assert onboard._dp_start_pg_agent(_pg_ctx()) is None
    assert pg_runtime.started[-1]["conn_params"]["host"] == DEPLOYED["POSTGRES_HOST"]


# ── the start records the pointer it validated; no by-pass write ────

FOREIGN_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/systems/quality-lab-AbCdEf"


def test_l1_start_records_the_validated_pointer_not_the_entry_one(
        cat, secrets, graph, deployed, pg_runtime):
    """A non-own entry pointer (e.g. written by /api/onboard/credentials) and the
    node's own pointer: registered_pg_dsn validated the node's, so that is the
    one the runtime config carries."""
    _register_pg()
    cat.get(SID)["secret_arn"] = FOREIGN_ARN
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    assert onboard._dp_start_pg_agent(_pg_ctx()) is None
    assert pg_runtime.started[-1]["connection_config"]["secret_arn"] == CONN_ARN


@pytest.fixture
def store_secret_calls(monkeypatch):
    import agents.onboard_agent as onboard_agent
    calls: list = []
    monkeypatch.setattr(onboard_agent, "_store_secret",
                        lambda sid, creds: calls.append(sid) or FOREIGN_ARN)
    return calls


def test_l1_the_credentials_route_refuses_a_user_registration(cat, secrets, store_secret_calls):
    _register_pg()
    res = onboard.onboard_credentials(
        {"system_id": SID, "credentials": {"password": "other"}})
    assert "error" in res and "Edit connection" in res["error"], res
    assert store_secret_calls == []
    assert cat.get(SID)["secret_arn"] == CONN_ARN


def test_guard_l1_the_credentials_route_still_stores_for_other_systems(
        cat, secrets, store_secret_calls, graph, monkeypatch):
    """A system the deployment registered whose next steps read credentials
    through its node's pointer (a REST API system with a connection record):
    the store writes the secret, points the node at it with one
    compare-and-set statement, then the catalog entry."""
    cat.add({"id": "legacy", "name": "Legacy", "connection_type": "REST API", "schema": "",
             "status": "registered"})
    graph["legacy"] = {**_node_row({"base_url": "https://legacy.example.com"}, "",
                                   "REST API"), "status": "onboarded"}

    def _cas(cypher, params):
        node = graph.get(params["sid"])
        if node is None or node["secret_arn"] != params["expected"] \
                or node["connection_type"] != params["ctype"]:
            return []
        node["secret_arn"] = params["arn"]
        return [{"secret_arn": node["secret_arn"]}]
    monkeypatch.setattr(cr, "_neptune_write_returning", _cas)
    res = onboard.onboard_credentials({"system_id": "legacy", "credentials": {"token": "t"}})
    assert res["status"] == "stored" and res["secret_arn"] == FOREIGN_ARN, res
    assert store_secret_calls == ["legacy"] and cat.get("legacy")["secret_arn"] == FOREIGN_ARN
    assert graph["legacy"]["secret_arn"] == FOREIGN_ARN


# ── a busy lock at the start is the connection phase's busy error ───

def test_l2_a_busy_lock_at_start_returns_the_connection_phase_busy_error(
        cat, secrets, graph, deployed, pg_runtime, monkeypatch):
    import threading
    from agents import connection_lock as cl
    _register_pg()
    graph[SID] = _node_row(PG_NODE, CONN_ARN, "PostgreSQL")
    monkeypatch.setattr(cl, "CONNECTION_LOCK_TIMEOUT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def _hold():
        with cl.connection_lock(SID):
            held.set()
            release.wait(5)
    t = threading.Thread(target=_hold)
    t.start()
    held.wait(5)
    try:
        phase = onboard._dp_connection_phase(_pg_ctx())
        start = onboard._dp_start_pg_agent(_pg_ctx())
    finally:
        release.set()
        t.join(5)
    assert isinstance(phase, dict) and "in progress" in phase["error"], phase
    assert start == phase, (start, phase)
    assert pg_runtime.started == []
