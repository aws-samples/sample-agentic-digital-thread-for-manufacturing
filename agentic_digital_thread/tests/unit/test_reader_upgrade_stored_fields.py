# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one-time upgrade step works for a demo PostgreSQL system as it is stored.

A demo or workshop PostgreSQL system's System node holds its connection as
fields, ``host`` / ``port`` / ``dbname`` / ``schema`` / ``search_path``, with a
``secret_arn`` pointing at the deployment's database secret, and no
connection string anywhere. The upgrade step (docs/deployment.md, "Upgrading
an existing deployment") is the admin connection Edit with nothing changed:
``PUT /api/admin/connection/<id>`` with ``{}``, or the UI's Save & Reconnect,
whose body is the host and port the form shows. On a rehearsal account both
were refused before anything moved ("PostgreSQL requires a
'connection_string'") because the Edit only knew a connection string.

Pinned here, through the real ``_do_update_connection`` and the real
``resync_cloud_connection``:

* both bodies reconnect the system onto its schema's read-only login, keep the
  agent-config's generated prompt, table list, concept mappings and business
  context, put no owner password or owner secret pointer in it, and leave the
  stored connection exactly as it was (nothing written, nothing probed);
* the database secret may hold only the password, or the full set a CDK
  database secret holds (username, password, host, port as a number, dbname);
* a real change to such a system is still validated before it is written,
  and nothing is written when it fails;
* a user-registered PostgreSQL system (its own secret holds a connection
  string) and an MCP Server system behave as before: probed, written, and the
  user's system keeps its own login.

Offline: the graph, Secrets Manager and PostgreSQL are in-memory stand-ins; a
login attempt without a user or a database is refused, as a real server does.
"""
from __future__ import annotations

import json
import os
import types
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from agents.agent_manager import AgentManager  # noqa: E402
from agents.demo import db_routing  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    DEPLOYED, ERP_ARN, ERP_PW, SEED_ARN, SEED_PW, deployed)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402

SEED_READER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:SeedDbReadersCmms-a1b2c3"
ERP_READER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:ErpDbReadersErp-a1b2c3"
READERS = {SEED_READER_ARN: {"username": "adt_reader_cmms_maintenance", "password": "r-cmms"},
           ERP_READER_ARN: {"username": "adt_reader_erp_data", "password": "r-erp"}}
GENERATED = {"system_prompt": "You answer from this system's tables.",
             "tables_desc": "assets(asset_id integer, name text)",
             "concept_mappings": [{"concept": "asset", "field": "assets.asset_id"}]}
BUSINESS_CONTEXT = "Steward note kept across the upgrade."

#: (system id, schema, database env prefix, secret ARN, password) per demo database.
SYSTEMS = {
    "cmms": ("cmms_maintenance", "", SEED_ARN, SEED_PW),
    "erp": ("erp_data", "ERP_", ERP_ARN, ERP_PW),
}


def _stored_node(schema: str, prefix: str) -> dict:
    """The fields a demo PostgreSQL node stores (onboard._sql_non_secret)."""
    return {"connection_type": "PostgreSQL", "host": DEPLOYED[f"{prefix}POSTGRES_HOST"],
            "port": "5432", "dbname": DEPLOYED[f"{prefix}POSTGRES_DB"],
            "schema": schema, "search_path": schema}


def _database_secret(shape: str, prefix: str, password: str) -> dict:
    """The deployment's database secret: the password only, or the set a CDK
    database secret holds once attached to its database."""
    if shape == "password_only":
        return {"password": password}
    return {"username": DEPLOYED[f"{prefix}POSTGRES_USER"], "password": password,
            "host": DEPLOYED[f"{prefix}POSTGRES_HOST"], "port": 5432,
            "dbname": DEPLOYED[f"{prefix}POSTGRES_DB"], "engine": "postgres"}


class _AgentConfigSecrets:
    """Secrets Manager for the agent-config secrets the re-sync reads and writes."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self, current: dict):
        self.current, self.writes = dict(current), {}
        self.exceptions = types.SimpleNamespace(ResourceExistsException=self.ResourceExistsException)

    def get_secret_value(self, SecretId):
        if SecretId not in self.current:
            raise RuntimeError("ResourceNotFoundException")
        return {"SecretString": json.dumps(self.current[SecretId])}

    def create_secret(self, Name, SecretString):
        raise self.ResourceExistsException()

    def put_secret_value(self, SecretId, SecretString):
        self.writes[SecretId] = json.loads(SecretString)

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}-x1y2z3"}


class _Postgres:
    """pgdriver stand-in for the validate-first probe: a login without a user
    or a database is refused, as a real server refuses it."""

    def __init__(self):
        self.logins = []

    def connect(self, dsn, **kwargs):
        from agents import pgdriver
        parts = urlsplit(dsn)
        self.logins.append((parts.hostname, parts.username, parts.path.lstrip("/")))
        if not parts.username or not parts.path.lstrip("/"):
            raise pgdriver.OperationalError("no user or database in the connection")
        cursor = types.SimpleNamespace(execute=lambda *a, **k: None, fetchone=lambda: (1,),
                                       close=lambda: None)
        return types.SimpleNamespace(cursor=lambda: cursor, close=lambda: None)


@pytest.fixture
def account(cat, secrets, graph, deployed, monkeypatch):
    """A deployed account: the reader maps on the app, the database secrets,
    the reader secrets, a cloud agent manager whose runtime pickup is
    recorded, and a PostgreSQL stand-in for the probe."""
    from agents import pgdriver

    monkeypatch.setenv("DB_READER_SECRET_ARNS", json.dumps({"cmms_maintenance": SEED_READER_ARN}))
    monkeypatch.setenv("ERP_DB_READER_SECRET_ARNS", json.dumps({"erp_data": ERP_READER_ARN}))
    monkeypatch.setattr(db_routing, "_read_reader_secret", lambda arn: dict(READERS[arn]))
    monkeypatch.setattr(cr, "read_secret_uncached", lambda arn: dict(secrets[arn]))
    postgres = _Postgres()
    monkeypatch.setattr(pgdriver, "connect", postgres.connect)
    monkeypatch.setattr("agents.connection_probes.validate_connection_target",
                        lambda target, *a, **kw: "203.0.113.10")
    mgr = AgentManager(mode="cloud")
    pickups: list = []
    monkeypatch.setattr(mgr, "_apply_business_context",
                        lambda sid, cfg, region: {**cfg, "business_context": BUSINESS_CONTEXT})
    monkeypatch.setattr(mgr, "_resync_force_runtime_pickup",
                        lambda sid, arn, region: pickups.append(sid) or True)
    monkeypatch.setattr(admin_routes, "_get_agent_manager", lambda: mgr)
    monkeypatch.setattr("agents.config.get_config",
                        lambda: types.SimpleNamespace(has_gateway=True))
    return types.SimpleNamespace(cat=cat, secrets=secrets, graph=graph, mgr=mgr,
                                 pickups=pickups, postgres=postgres)


def _demo_system(account, system_id: str, shape: str):
    schema, prefix, arn, password = SYSTEMS[system_id]
    account.cat.add({"id": system_id, "name": system_id.upper(), "connection_type": "PostgreSQL",
                     "schema": schema, "status": "onboarded", "connection_origin": "demo"})
    account.graph[system_id] = _node_row(_stored_node(schema, prefix), arn, "PostgreSQL")
    account.secrets[arn] = _database_secret(shape, prefix, password)
    owner = {"host": DEPLOYED[f"{prefix}POSTGRES_HOST"], "port": "5432",
             "dbname": DEPLOYED[f"{prefix}POSTGRES_DB"],
             "user": DEPLOYED[f"{prefix}POSTGRES_USER"], "password": password,
             "secret_arn": arn, **GENERATED}
    return _AgentConfigSecrets({f"adt/agent-config/{system_id}": owner})


def _put(system_id: str, body: dict, agent_config: _AgentConfigSecrets):
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    with patch("agents.agent_manager.boto3", types.SimpleNamespace(client=lambda n, **k: agent_config)):
        res = admin_routes._do_update_connection(system_id, request, body)
    return (getattr(res, "status_code", 200), json.loads(res.body) if hasattr(res, "body") else res)


def _ui_body(system_id: str) -> dict:
    """What Save & Reconnect sends for an unchanged PostgreSQL form: the
    non-secret fields it prefilled from GET /api/admin/connection/<id> (host,
    port as text), no password (frontend payloads.buildEditConnectionBody)."""
    shown = cr.describe_connection(system_id)
    return {"host": str(shown["host"]), "port": str(shown["port"])}


@pytest.mark.parametrize("shape", ["password_only", "cdk_database_secret"])
@pytest.mark.parametrize("system_id", sorted(SYSTEMS))
@pytest.mark.parametrize("body_kind", ["empty", "ui_unchanged"])
def test_an_unchanged_edit_moves_a_demo_system_to_its_reader(account, system_id, shape, body_kind):
    agent_config = _demo_system(account, system_id, shape)
    node_before = dict(account.graph[system_id])
    body = {} if body_kind == "empty" else _ui_body(system_id)
    status, answer = _put(system_id, body, agent_config)
    assert status == 200 and answer.get("status") == "ok", answer
    assert answer["reconnected"] is True and account.pickups == [system_id]
    payload = agent_config.writes[f"adt/agent-config/{system_id}"]
    schema, prefix, arn, password = SYSTEMS[system_id]
    reader = READERS[SEED_READER_ARN if not prefix else ERP_READER_ARN]
    assert (payload["user"], payload["password"]) == (reader["username"], reader["password"])
    assert (payload["host"], payload["dbname"]) == (DEPLOYED[f"{prefix}POSTGRES_HOST"],
                                                    DEPLOYED[f"{prefix}POSTGRES_DB"])
    for key, value in GENERATED.items():
        assert payload[key] == value, key
    assert payload["business_context"] == BUSINESS_CONTEXT
    blob = json.dumps(payload)
    assert password not in blob and arn not in blob and "connection_string" not in payload
    assert account.graph[system_id] == node_before, "the stored connection must not change"
    assert account.postgres.logins == [], "nothing to validate, so nothing is probed"


def test_a_real_change_to_a_demo_system_is_still_validated_and_writes_nothing_on_failure(account):
    agent_config = _demo_system(account, "cmms", "cdk_database_secret")
    node_before = dict(account.graph["cmms"])
    status, answer = _put("cmms", {"host": DEPLOYED["POSTGRES_HOST"], "port": "6543"}, agent_config)
    assert status == 400 and answer["status"] == "validation_failed", answer
    assert account.graph["cmms"] == node_before and agent_config.writes == {}
    assert account.pickups == []


@pytest.mark.parametrize("shape", ["password_only", "cdk_database_secret"])
def test_a_demo_password_edit_is_still_refused(account, shape):
    agent_config = _demo_system(account, "cmms", shape)
    node_before = dict(account.graph["cmms"])
    status, answer = _put("cmms", {"password": "new-pw"}, agent_config)
    assert status == 400 and answer["status"] == "validation_failed", answer
    assert account.graph["cmms"] == node_before
    assert agent_config.writes == {} and account.pickups == []


def test_a_user_postgres_registration_is_probed_written_and_keeps_its_own_login(account):
    _register_pg()
    account.cat.update_status(SID, "onboarded")
    account.graph[SID] = _node_row({"connection_type": "PostgreSQL", "host": "db.example.internal",
                                    "port": "5432", "dbname": "lims", "schema": "lims",
                                    "search_path": "lims"}, CONN_ARN, "PostgreSQL")
    agent_config = _AgentConfigSecrets({f"adt/agent-config/{SID}": {
        "host": "db.example.internal", "dbname": "lims", "user": "lab_reader",
        "password": "s3cr3t-pw", **GENERATED}})
    status, answer = _put(SID, {}, agent_config)
    assert status == 200 and answer.get("status") == "ok", answer
    assert account.postgres.logins == [("db.example.internal", "lab_reader", "lims")]
    assert account.graph[SID]["secret_arn"] == CONN_ARN
    payload = agent_config.writes[f"adt/agent-config/{SID}"]
    assert payload["user"] == "lab_reader" and "adt_reader_" not in json.dumps(payload)


def test_an_mcp_system_edit_is_probed_and_written_as_before(account, monkeypatch):
    probes: list = []
    monkeypatch.setattr(cr, "probe_connection",
                        lambda sid, cfg: probes.append(cfg.get("url")) or {"status": "pass"})
    monkeypatch.setattr(admin_routes, "_reconnect_system", lambda *a: True)
    account.cat.add({"id": "hub", "name": "Hub", "connection_type": "MCP Server",
                     "status": "onboarded", "connection_origin": "demo"})
    account.graph["hub"] = _node_row({"connection_type": "MCP Server",
                                      "url": "https://hub.example.com/mcp"}, "", "MCP Server")
    status, answer = _put("hub", {}, _AgentConfigSecrets({}))
    assert status == 200 and answer.get("status") == "ok", answer
    assert probes == ["https://hub.example.com/mcp"]
    assert json.loads(account.graph["hub"]["connection_config"])["url"] == "https://hub.example.com/mcp"


# ---------------------------------------------------------------------------
# Which Edits may skip the probe: only a demo system, decided by the catalog
# entry and the stored credentials, never by the connection's shape alone or
# by the connection type the request body names.
# ---------------------------------------------------------------------------
_OWN_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/lims-a1b2c3"


def _fields(**overrides) -> dict:
    """A resolved connection stored as fields (no connection string)."""
    return {"connection_type": "PostgreSQL", "host": "db.example.internal", "port": "5432",
            "dbname": "lims", "schema": "lims", "search_path": "lims", "secret_arn": SEED_ARN,
            **overrides}


def _entry(**overrides) -> dict:
    return {"id": "lims", "connection_type": "PostgreSQL", "connection_origin": "demo", **overrides}


@pytest.mark.parametrize("entry, existing, body, secret, expected", [
    pytest.param(_entry(), _fields(), {}, None, True, id="demo-system-unchanged"),
    pytest.param(_entry(connection_origin="user"), _fields(secret_arn=_OWN_ARN), {}, None, False,
                 id="user-registration-stored-as-fields"),
    pytest.param(_entry(connection_origin="user"), _fields(), {}, None, False,
                 id="user-registration-on-a-shared-secret"),
    pytest.param(_entry(connection_origin=None), _fields(secret_arn=_OWN_ARN), {}, None, False,
                 id="no-origin-but-its-own-secret"),
    pytest.param(_entry(connection_type="MCP Server"), _fields(connection_type="MCP Server"),
                 {"connection_type": "PostgreSQL"}, None, False,
                 id="non-postgres-entry-the-body-calls-postgres"),
    pytest.param(_entry(), _fields(), {"connection_type": "MCP Server"}, None, False,
                 id="body-changes-the-connection-type"),
    pytest.param(_entry(), _fields(), {}, {"password": "new-pw"}, False, id="a-password-is-sent"),
])
def test_only_a_demo_systems_unchanged_edit_skips_the_probe(entry, existing, body, secret, expected):
    merged = {**existing, **body}
    assert admin_routes._unchanged_pg_fields_edit(entry, existing, merged, secret) is expected


def test_a_field_stored_user_registration_is_probed(account, monkeypatch):
    """A user registration whose stored connection has fields but no string (an
    older or damaged record) goes through the probe like any other Edit."""
    skipped: list = []
    real = admin_routes._reconnect_unchanged
    monkeypatch.setattr(admin_routes, "_reconnect_unchanged",
                        lambda *a, **k: skipped.append(a[0]) or real(*a, **k))
    _register_pg()
    account.cat.update_status(SID, "onboarded")
    account.graph[SID] = _node_row({"connection_type": "PostgreSQL", "host": "db.example.internal",
                                    "port": "5432", "dbname": "lims", "schema": "lims",
                                    "search_path": "lims"}, CONN_ARN, "PostgreSQL")
    account.secrets[CONN_ARN] = {k: v for k, v in account.secrets[CONN_ARN].items()
                                 if k not in ("connection_string", "dsn")}
    _put(SID, {}, _AgentConfigSecrets({}))
    assert skipped == [], "a user registration must never skip the probe"
