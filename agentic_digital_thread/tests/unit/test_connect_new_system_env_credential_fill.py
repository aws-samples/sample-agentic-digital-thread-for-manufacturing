# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: the deployment's env credentials reach only the env target.
Offline — no AWS, no Neptune, no PostgreSQL, no network.

The rules these pin:

* the deployment's env database credentials fill ONLY the
  env target: a resolved host or database that differs from
  ``_env_conn_params_for_schema``'s is refused (``ConnectionSecretUnavailableError``),
  for every system (a demo PostgreSQL system whose host an admin moved included);
* an admin edit of a user PostgreSQL registration BEFORE its first
  provision lands in its own secret (the admin route passes the catalog entry to
  ``write_connection``; ``write_connection`` itself still reads no catalog);
* a user registration never gets env credentials, even for a host
  that equals the env host (the deployment's superuser on the seed database);
* a credentials-only update (token rotation through the API or the
  Settings HighByte card) keeps the node's existing connection config;
* the CRITICAL guard — every PostgreSQL system in the demo and workshop
  manifests, ENUMERATED FROM THE CODE under every mode, still gets the env fill at
  restore, records-first sampling, preflight and provision, in the deployed
  compute-task env and in local dev.
"""
from __future__ import annotations

import asyncio
import json
import os
import types

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.demo.db_routing as routing  # noqa: E402
import agents.errors as errors  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
import agents.server as server  # noqa: E402 - pytest blocks sockets; no AWS call
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    HB_URL, MCP_URL, USER_TOKEN, _register_mcp)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture

SEED_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:seed-db-AbCdEf"
ERP_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:erp-db-AbCdEf"
SEED_PW, ERP_PW = "seed-deploy-pw", "erp-deploy-pw"  # nosec B105 - fixtures
#: The compute task's env (stacks/adt_compute_stack.py _env_data_stores +
#: _app_container_secrets) for a deploy with the seed and ERP databases — every
#: --seed / --simulator / --workshop deploy, and the Workshop Studio package.
DEPLOYED = {
    "POSTGRES_HOST": "adtseed-db.abc123xyz.us-east-1.rds.amazonaws.com", "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "adt_demo", "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": SEED_PW,
    "DB_SECRET_ARN": SEED_ARN,
    "ERP_POSTGRES_HOST": "adterp-cluster.cluster-abc123xyz.us-east-1.rds.amazonaws.com",
    "ERP_POSTGRES_PORT": "5432", "ERP_POSTGRES_DB": "erp_simulation",
    "ERP_POSTGRES_USER": "erpadmin", "ERP_POSTGRES_PASSWORD": ERP_PW, "ERP_DB_SECRET_ARN": ERP_ARN,
}
#: Local development (.env.local.example): one database, no ERP host.
LOCAL = {"POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432", "POSTGRES_DB": "adt_demo",
         "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": SEED_PW}
NEW_PW = "rotated-pw-654"  # nosec B105 - fixture
NEW_TOKEN = "rotated-token-321"  # nosec B105 - fixture


def _use_env(monkeypatch, env: dict):
    for k in set(DEPLOYED) | set(LOCAL):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(routing, "_read_demo_db_pointer", lambda kind: {})   # no SSM
    routing._POINTER_CACHE.clear()
    db.invalidate_schema_conn_cache()


@pytest.fixture
def deployed(monkeypatch):
    _use_env(monkeypatch, DEPLOYED)


def _admin_update(system_id: str, body: dict):
    """PUT /api/admin/connection/{id} through its real implementation (the
    validate-first probe passes — the admin controls the target server)."""
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    from unittest.mock import patch
    with patch.object(cr, "probe_connection", return_value={"status": "pass"}), \
            patch.object(admin_routes, "_reconnect_system", return_value=True):
        # main's _do_update_connection is the sync body (the route offloads it).
        return admin_routes._do_update_connection(system_id, request, body)


def _no_deploy_password(params_or_exc):
    blob = json.dumps(params_or_exc, default=str)
    assert SEED_PW not in blob and ERP_PW not in blob, "deployment password used"


# ── env credentials fill only the env target ──────────

@pytest.mark.parametrize("resolved", [
    {"host": "db.elsewhere.example", "dbname": "adt_demo"},           # moved host
    {"host": DEPLOYED["POSTGRES_HOST"], "dbname": "other_db"},        # same host, other database
])
def test_mh1_env_credentials_never_fill_a_foreign_target(deployed, resolved):
    with pytest.raises(errors.ConnectionSecretUnavailableError):
        cr.pg_conn_params({"system_id": "cmms", "schema": "cmms_maintenance",
                           "secret_arn": "", **resolved})


def test_px4_demo_pg_admin_host_edit_never_sends_the_deployment_password(
        cat, secrets, graph, deployed, monkeypatch):
    """Probe B: a demo PostgreSQL system (node -> shared demo secret) whose host an
    admin moves to an external server, through the real admin route. The Edit
    is refused up front (the deployment's credentials only go to its own
    database), so the node keeps its host and the env credentials stay on the
    deployment host."""
    from tests.unit.test_connect_new_system_credential_rotation import _node_row as row
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "onboarded", "connection_origin": "demo"})
    graph["cmms"] = row({"host": DEPLOYED["POSTGRES_HOST"], "port": "5432", "dbname": "adt_demo",
                         "schema": "cmms_maintenance", "search_path": "cmms_maintenance"},
                        SEED_ARN, "PostgreSQL")
    before = dict(graph["cmms"])
    res = _admin_update("cmms", {"host": "db.elsewhere.example", "port": "5432"})
    assert getattr(res, "status_code", None) == 400, res
    assert json.loads(res.body)["reason"] == "deployment_credentials"
    assert graph["cmms"] == before
    params = cr.pg_conn_params(cr.resolve_connection("cmms"))
    assert params["host"] == DEPLOYED["POSTGRES_HOST"], params


# ── a pre-provision user PG edit ────────────────────────

NEW_DSN = (f"postgresql://lab_reader:{NEW_PW}@db2.example.internal:5432/lims"
           "?options=-csearch_path%3Dlims")


def test_mh2_pre_provision_user_pg_edit_lands_in_its_own_secret(cat, secrets, graph, deployed):
    _register_pg()                                    # registered, NOT provisioned (no node)
    res = _admin_update(SID, {"connection_string": NEW_DSN, "host": "db2.example.internal"})
    assert res.get("status") == "ok", res
    assert graph[SID]["secret_arn"] == CONN_ARN, graph[SID]
    assert secrets[CONN_ARN]["password"] == NEW_PW and \
        secrets[CONN_ARN]["connection_origin"] == "user", secrets[CONN_ARN]
    params = db._records_conn_params_for_schema("lims")
    _no_deploy_password(params)
    assert (params["host"], params["user"], params["password"]) == \
        ("db2.example.internal", "lab_reader", NEW_PW)


def test_mh2_write_connection_reads_no_catalog(cat, secrets, graph, deployed, monkeypatch):
    """The entry is DATA passed by the admin route; write_connection never reads
    the catalog itself (the node is the sole source — test_connection_resolver)."""
    _register_pg()
    monkeypatch.setattr(cr, "_catalog_get", lambda sid: (_ for _ in ()).throw(
        AssertionError("write_connection read the catalog")))
    cr.write_connection(SID, non_secret={"connection_type": "PostgreSQL",
                                         "connection_string": NEW_DSN},
                        entry=dict(cat.get(SID)))
    assert graph[SID]["secret_arn"] == CONN_ARN


def test_mh3_a_user_registration_never_gets_env_credentials_even_on_the_env_host(
        cat, secrets, graph, deployed):
    """A user registration's node without its own credentials (e.g. written by an
    older writer) on the SAME host + database as the env target: the deployment's
    superuser credentials are still never used."""
    _register_pg()
    graph[SID] = _node_row({"host": DEPLOYED["POSTGRES_HOST"], "port": "5432",
                            "dbname": "adt_demo", "schema": "lims"}, "", "PostgreSQL")
    entry = dict(cat.get(SID))
    with pytest.raises(errors.ConnectionSecretUnavailableError):
        cr.pg_conn_params(cr.resolve_connection(SID), entry=entry)
    with pytest.raises(errors.MissingDbPasswordError):
        db._records_conn_params_for_schema("lims")


# ── a credentials-only update keeps the node's connection config ────

def test_px5_token_only_rotation_keeps_the_node_url(cat, secrets, graph):
    _register_mcp()
    graph[SID] = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")
    cr.write_connection(SID, non_secret={"connection_type": "MCP Server"},
                        secret={"token": NEW_TOKEN})
    assert json.loads(graph[SID]["connection_config"]) == {"url": MCP_URL, "auth_scheme": "bearer"}
    resolved = cr.resolve_connection(SID)
    assert (resolved["url"], resolved["token"]) == (MCP_URL, NEW_TOKEN), resolved


def test_px5_highbyte_token_card_keeps_the_node_url(cat, secrets, graph):
    hb_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/highbyte-AbCdEf"
    graph["highbyte"] = _node_row({"url": HB_URL + "/mcp", "auth_scheme": "bearer_literal"},
                                  hb_arn, "MCP Server")
    secrets[hb_arn] = {"token": "old"}
    cr.write_connection("highbyte", non_secret={"connection_type": "MCP Server"},
                        secret={"token": "hb-new"})
    assert json.loads(graph["highbyte"]["connection_config"])["url"] == HB_URL + "/mcp"


def test_guard_px5_ui_edit_full_resend_writes_exactly_what_it_wrote_before(cat, secrets, graph):
    """The UI Edit form resends the URL (pre-filled): the node is REPLACED exactly
    as before (keys the form does not send are not carried over)."""
    _register_mcp()
    graph[SID] = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")
    new_url = "https://mcp2.example.com/mcp"
    non_secret, secret, err = admin_routes._parse_connection_body(
        {"url": new_url, "token": NEW_TOKEN}, "MCP Server", existing={})
    assert err is None
    non_secret["connection_type"] = "MCP Server"
    cr.write_connection(SID, non_secret=non_secret, secret=secret)
    assert json.loads(graph[SID]["connection_config"]) == {"url": new_url}


# ── CRITICAL guard: every demo/workshop PostgreSQL system keeps the env fill ─

def _manifest_pg_systems(monkeypatch) -> list[tuple[str, str, dict]]:
    """Every PostgreSQL system in the demo + workshop manifests, enumerated from
    the code: ``manifest_systems()`` in demo / workshop-frozen / workshop-live
    mode, plus every list-of-system-dicts in agents.demo.systems /
    agents.demo.workshop (so a manifest list not yet wired into
    manifest_systems() is covered too)."""
    import agents.demo.systems as systems
    import agents.demo.workshop as workshop
    found: dict = {}
    for mode in ({}, {"ADT_WORKSHOP": "1", "ADT_WORKSHOP_STORY": "frozen"},
                 {"ADT_WORKSHOP": "1", "ADT_WORKSHOP_STORY": "live"}):
        monkeypatch.delenv("ADT_WORKSHOP", raising=False)
        monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)
        for k, v in mode.items():
            monkeypatch.setenv(k, v)
        for s in systems.manifest_systems():
            if s.get("connection_type") == "PostgreSQL":
                found[(s["id"], s["schema"])] = s
    for mod in (systems, workshop):
        for value in vars(mod).values():
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                for s in value:
                    if s.get("connection_type") == "PostgreSQL" and s.get("schema"):
                        found[(s["id"], s["schema"])] = s
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP_STORY", raising=False)
    return [(sid, schema, s) for (sid, schema), s in sorted(found.items())]


@pytest.fixture
def trip_registration_helpers(monkeypatch):
    hits = []
    for name in ("registration_connection", "registered_pg_dsn", "pg_params_from_dsn"):
        monkeypatch.setattr(cr, name, lambda *a, _n=name, **k: hits.append(_n) or (_ for _ in ())
                            .throw(AssertionError(f"demo path called {_n}")))
    return hits


def _expected_env_target(schema: str) -> dict:
    return routing.demo_conn_params_for_schema(schema) or db.get_conn_params()


@pytest.mark.parametrize("env", [DEPLOYED, LOCAL], ids=["deployed", "local"])
def test_guard_every_manifest_pg_system_keeps_the_env_fill(
        cat, secrets, graph, monkeypatch, trip_registration_helpers, env):
    systems = _manifest_pg_systems(monkeypatch)
    _use_env(monkeypatch, env)
    monkeypatch.setattr(server, "catalog", cat)
    assert systems, "no PostgreSQL system found in the manifests"
    families = {routing.is_erp_schema(schema) for _, schema, _ in systems}
    assert families == {True, False}, "both the ERP and the seed database must be covered"
    for sid, schema, sysdef in systems:
        for s in list(cat.get_all()):
            cat.remove(s["id"])
        graph.pop(sid, None)
        secrets.clear()
        db.invalidate_schema_conn_cache()
        # Registered exactly as the demo hook registers it.
        res = onboard_agent.register_demo_system(
            name=sysdef["name"], system_type=sysdef.get("system_type", ""),
            plant="", connection_type="PostgreSQL",
            connection_string=routing.demo_pg_dsn(schema), schema=schema, system_id=sid)
        assert res.get("status") == "created", (sid, res)
        target = _expected_env_target(schema)
        # PREFLIGHT before the first provision: the seed bootstrap DSN.
        pre = op._bootstrap_unregistered(cr.resolve_connection(sid), sid)
        assert pre["connection_string"] == routing.demo_pg_dsn(schema), sid
        # PROVISION: the node exactly as provision writes it, and the agent params.
        ctx = types.SimpleNamespace(system_id=sid, plant="", schema=schema,
                                    connection_type="PostgreSQL", registered_pg_dsn="",
                                    registered_pg_arn="", nodes=0)
        assert onboard._dp_pg_registered_connection(ctx) is None
        onboard._dp_persist_sql_connection(ctx)
        node_cfg = json.loads(graph[sid]["connection_config"])
        assert (node_cfg["host"].lower(), node_cfg["dbname"]) == \
            (str(target["host"]).lower(), target["dbname"]), (sid, node_cfg, target)   # STOP check
        assert graph[sid]["secret_arn"] == cr.db_secret_arn_for_schema(schema), sid
        assert onboard._pg_agent_conn_params(ctx, schema) == \
            (routing.demo_conn_params_for_schema(schema) or onboard._agent_conn_params_for_schema(schema))
        # RESTORE and RECORDS-FIRST with the shared secret unreadable: the env fill.
        for params in (server._restore_sql_conn_params(sid),
                       db._records_conn_params_for_schema(schema)):
            assert params is not None, sid
            assert (params["host"], params["user"], params["password"]) == \
                (target["host"], target["user"], target["password"]), (sid, params["host"])
        # PREFLIGHT after provision: still the seed bootstrap DSN.
        pre = op._bootstrap_unregistered(cr.resolve_connection(sid), sid)
        assert pre["connection_string"] == routing.demo_pg_dsn(schema), sid
        # RESTORE with the shared secret readable: the secret's credentials, as before.
        arn = graph[sid]["secret_arn"]
        if arn:
            secrets[arn] = {"username": "secret_user", "password": "secret-pw",
                            "host": target["host"], "port": "5432", "dbname": target["dbname"]}
            params = server._restore_sql_conn_params(sid)
            assert (params["user"], params["password"]) == ("secret_user", "secret-pw"), sid
    assert trip_registration_helpers == []
