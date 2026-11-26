# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Moving an already-provisioned demo PostgreSQL system to its read-only login.

The one-time upgrade step for an existing deployment is the admin connection
Edit with nothing changed (``PUT /api/admin/connection/<id>`` with ``{}``, the
UI's Save & Reconnect). It re-syncs the runtime: the agent-config secret is
rewritten with the schema's read-only login and a new runtime version reads it
at start. Pinned here:

* the Edit body ``{}`` is valid for a PostgreSQL system that already resolves;
* the re-sync KEEPS the generated prompt, table list and concept mappings of
  the current agent-config when the database is the same (the whole-value
  rewrite used to drop them), never a login or pointer from it;
* another database, another connection type, or an unreadable secret carries
  nothing forward;
* ``audit_runtime_env.py --reader-logins`` reports, annotate-only, each
  PostgreSQL system runtime's login by name (never a secret value) and flags
  one still on the deployment's database login.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.agent_manager import AgentManager
from agents.demo import db_routing

SEED_HOST = "seed-db.example.com"
OWNER_PW = "owner-VALUE-pw"  # nosec B105 - fake fixture
OWNER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:AdtDbSecretAAAA-abcdef"
READER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:SeedDbReadersCmms-a1b2c3"
GENERATED = {"system_prompt": "You are the CMMS agent.", "tables_desc": "assets(asset_id int)",
             "concept_mappings": [{"concept": "asset", "field": "assets.asset_id"}]}
OLD_PAYLOAD = {"secret_arn": OWNER_ARN, "host": SEED_HOST, "port": "5432", "dbname": "adt_demo",
               "user": "postgres", "password": OWNER_PW, **GENERATED}
RESOLVED = {"connection_type": "PostgreSQL", "schema": "cmms_maintenance", "secret_arn": OWNER_ARN,
            "host": SEED_HOST, "port": 5432, "dbname": "adt_demo", "username": "postgres",
            "password": OWNER_PW,
            "connection_string": f"postgresql://{SEED_HOST}:5432/adt_demo"}


@pytest.fixture
def demo_env(monkeypatch):
    for key, value in {"POSTGRES_HOST": SEED_HOST, "POSTGRES_USER": "postgres",
                       "POSTGRES_PASSWORD": OWNER_PW, "AWS_REGION": "us-east-1",
                       "DB_READER_SECRET_ARNS": json.dumps({"cmms_maintenance": READER_ARN})}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db_routing, "_read_reader_secret", lambda arn: {
        "username": "adt_reader_cmms_maintenance", "password": "r-cmms"})
    monkeypatch.setattr(db_routing, "_demo_db_pointer", lambda kind: {})


class _Secrets:
    class ResourceExistsException(Exception):
        pass

    def __init__(self, current=None, readable=True):
        self.current, self.readable, self.writes = current, readable, []
        self.exceptions = types.SimpleNamespace(ResourceExistsException=self.ResourceExistsException)

    def get_secret_value(self, SecretId):
        if not self.readable or self.current is None:
            raise RuntimeError("not readable")
        return {"SecretString": json.dumps(self.current)}

    def create_secret(self, Name, SecretString):
        raise self.ResourceExistsException()

    def put_secret_value(self, SecretId, SecretString):
        self.writes.append(json.loads(SecretString))

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}-x1y2z3"}


def _resync(secrets, resolved, connection_type="PostgreSQL"):
    mgr = AgentManager(mode="cloud")
    boto = types.SimpleNamespace(client=lambda name, **kw: secrets)
    with patch("agents.agent_manager.boto3", boto), \
            patch.object(mgr, "_apply_business_context", lambda sid, cfg, region: cfg), \
            patch.object(mgr, "_resync_force_runtime_pickup", lambda sid, arn, region: True):
        assert mgr.resync_cloud_connection("cmms", connection_type, connection_config=resolved) is True
    (payload,) = secrets.writes
    return payload


def test_the_upgrade_resync_keeps_the_generated_keys_and_drops_the_owner_login(demo_env):
    payload = _resync(_Secrets(current=OLD_PAYLOAD), dict(RESOLVED))
    assert (payload["user"], payload["password"]) == ("adt_reader_cmms_maintenance", "r-cmms")
    for key, value in GENERATED.items():
        assert payload[key] == value, key
    blob = json.dumps(payload)
    assert OWNER_PW not in blob and OWNER_ARN not in blob and "postgres\"" not in blob


def test_another_database_carries_nothing_forward(demo_env):
    moved = {**RESOLVED, "host": "other-db.example.com", "username": "anycompany_ro", "password": "theirs"}
    payload = _resync(_Secrets(current=OLD_PAYLOAD), moved)
    assert not any(key in payload for key in GENERATED)


def test_another_connection_type_carries_nothing_forward(demo_env):
    fresh = {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp", "token": "t"}
    payload = _resync(_Secrets(current={"url": "https://mcp.example.com/mcp", **GENERATED}), fresh,
                      connection_type="MCP Server")
    assert payload == fresh


def test_an_unreadable_current_secret_still_moves_the_login(demo_env):
    payload = _resync(_Secrets(current=OLD_PAYLOAD, readable=False), dict(RESOLVED))
    assert payload["user"] == "adt_reader_cmms_maintenance"
    assert not any(key in payload for key in GENERATED)


def test_an_empty_edit_body_is_valid_for_a_postgres_system():
    from agents.admin_routes import _parse_connection_body

    non_secret, secret, err = _parse_connection_body(
        {}, "PostgreSQL", existing={"connection_string": f"postgresql://{SEED_HOST}:5432/adt_demo"})
    assert err is None and secret == {}


# --- the audit annotation -----------------------------------------------------

_ADT_ROOT = Path(__file__).resolve().parents[2]


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_runtime_env_reader_logins", _ADT_ROOT / "scripts" / "audit_runtime_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("audit_runtime_env_reader_logins", module)
    spec.loader.exec_module(module)
    return module


AGENT_CONFIG = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/agent-config/{}-x1y2z3"


def _env(system_id, schema, connection_type="PostgreSQL"):
    return {"SYSTEM_ID": system_id, "SCHEMA": schema, "CONNECTION_TYPE": connection_type,
            "CONNECTION_SECRET_ARN": AGENT_CONFIG.format(system_id)}


@pytest.mark.parametrize("payload, status", [
    ({"user": "adt_reader_cmms_maintenance", "password": "r"}, "reader"),
    ({"username": "postgres", "password": OWNER_PW, "secret_arn": OWNER_ARN}, "deployment"),
    ({"user": "postgres", "password": OWNER_PW}, "deployment"),
    ({"user": "anycompany_ro", "password": "theirs",
      "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/cmms-abcdef"}, "own"),
    ({"tables_desc": "t"}, "no-login"),
])
def test_the_annotation_names_the_login_and_its_kind(payload, status):
    audit = _load_audit()
    note = audit.reader_login_annotation("adt_cmms", _env("cmms", "cmms_maintenance"),
                                         lambda arn: dict(payload))
    assert note["system"] == "cmms" and note["status"] == status
    assert note["login"] == str(payload.get("user") or payload.get("username") or "")
    rendered = audit.render_reader_logins([note])
    assert OWNER_PW not in rendered and "theirs" not in rendered and "secret:" not in rendered
    assert "annotation only" in rendered


def test_the_annotation_skips_other_runtimes_and_survives_an_unreadable_secret():
    audit = _load_audit()
    assert audit.reader_login_annotation("adt_lake", _env("lake", "lake", "Amazon Athena"),
                                         lambda arn: pytest.fail("read a non-PostgreSQL secret")) is None
    assert audit.reader_login_annotation("adt_orchestrator", {"CONNECTION_TYPE": "PostgreSQL"},
                                         lambda arn: pytest.fail("read for the orchestrator")) is None

    def boom(arn):
        raise RuntimeError(f"AccessDenied on {arn} VALUE")

    note = audit.reader_login_annotation("adt_cmms", _env("cmms", "cmms_maintenance"), boom)
    assert note["status"] == "unreadable" and note["detail"] == "RuntimeError"
    assert "VALUE" not in audit.render_reader_logins([note])


def test_the_annotation_never_gates():
    audit = _load_audit()
    assert "--reader-logins" in audit.__doc__
    notes = [{"system": "cmms", "login": "postgres", "status": "deployment", "detail": ""}]
    text = audit.render_reader_logins(notes)
    assert "cmms" in text and "postgres" in text and "docs/deployment.md" in text


def test_the_reader_login_prefix_is_shared():
    assert db_routing.READER_LOGIN_PREFIX == "adt_reader_"
