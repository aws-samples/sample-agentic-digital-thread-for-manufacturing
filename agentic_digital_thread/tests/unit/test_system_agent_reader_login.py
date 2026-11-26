# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent on a demo database connects with its schema's read-only
login, never as the database owner.

The app starts each system runtime with an ``adt/agent-config/<id>`` secret
holding its connection (the runtime reads it through ``CONNECTION_SECRET_ARN``,
its only connection key in the env contract). For a schema of a database this
deployment provisions, that connection used to carry the owner login
(``postgres`` / ``erp_admin``). Pinned here:

* ``demo.db_routing.system_agent_reader_login``: the owner login on a demo
  database becomes the schema's reader (read from its secret, named by the
  ``DB_READER_SECRET_ARNS`` / ``ERP_DB_READER_SECRET_ARNS`` maps); a
  customer's own login, another host, or no map keeps the login; a map
  without the schema, or an unreadable or owner-named reader secret, fails
  closed.
* ``AgentManager._prepare_runtime_env`` (provision, restore) and
  ``resync_cloud_connection`` (admin edit) write the reader into the
  agent-config secret: no owner password and no owner secret pointer in the
  secret or the runtime env, and no new env key.
* ``runtime_app``: a PostgreSQL runtime makes no secret read beyond its
  agent-config secret at start (no lookup of the System node's owner pointer).
"""
from __future__ import annotations

import json

import pytest

from agents import model_config, runtime_env_contract
from agents.agent_manager import AgentManager
from agents.demo import db_routing
from agents.errors import SystemReaderLoginError

SEED_HOST, ERP_HOST = "seed-db.example.com", "erp-db.example.com"
OWNER_PW, ERP_OWNER_PW = "owner-VALUE-pw", "erp-owner-VALUE-pw"  # nosec B105 - fake fixtures
SEED_OWNER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:AdtDbSecretAAAA-abcdef"
ERP_OWNER_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:DatabaseSecretBBB-abcdef"
READER_ARNS = {
    "cmms_maintenance": "arn:aws:secretsmanager:us-east-1:111122223333:secret:SeedDbReadersCmms-a1b2c3",
    "mes_line3": "arn:aws:secretsmanager:us-east-1:111122223333:secret:SeedDbReadersLine3-a1b2c3",
}
ERP_READER_ARNS = {
    "erp_data": "arn:aws:secretsmanager:us-east-1:111122223333:secret:ErpDbReadersErp-a1b2c3",
}
READER_SECRETS = {
    READER_ARNS["cmms_maintenance"]: {"username": "adt_reader_cmms_maintenance", "password": "r-cmms"},
    READER_ARNS["mes_line3"]: {"username": "adt_reader_mes_line3", "password": "r-line3"},
    ERP_READER_ARNS["erp_data"]: {"username": "adt_reader_erp_data", "password": "r-erp"},
}


@pytest.fixture
def env(monkeypatch):
    values = {
        "POSTGRES_HOST": SEED_HOST, "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": OWNER_PW,
        "POSTGRES_DB": "adt_demo", "DB_SECRET_ARN": SEED_OWNER_ARN,
        "ERP_POSTGRES_HOST": ERP_HOST, "ERP_POSTGRES_USER": "erp_admin",
        "ERP_POSTGRES_PASSWORD": ERP_OWNER_PW, "ERP_POSTGRES_DB": "erp_simulation",
        "ERP_DB_SECRET_ARN": ERP_OWNER_ARN,
        "DB_READER_SECRET_ARNS": json.dumps(READER_ARNS),
        "ERP_DB_READER_SECRET_ARNS": json.dumps(ERP_READER_ARNS),
        "AWS_REGION": "us-east-1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    reads: list = []

    def fetch(arn):
        reads.append(arn)
        return dict(READER_SECRETS[arn])

    monkeypatch.setattr(db_routing, "_read_reader_secret", fetch)
    monkeypatch.setattr(db_routing, "_demo_db_pointer", lambda kind: {})
    return reads


def _owner(host=SEED_HOST, user="postgres", password=OWNER_PW, dbname="adt_demo"):
    return {"host": host, "port": "5432", "dbname": dbname, "user": user, "password": password}


# --- the demo seam ----------------------------------------------------------


def test_the_owner_login_becomes_the_schema_reader(env):
    login = db_routing.system_agent_reader_login("cmms_maintenance", _owner())
    assert login == {"host": SEED_HOST, "port": "5432", "dbname": "adt_demo",
                     "user": "adt_reader_cmms_maintenance", "password": "r-cmms"}
    assert env == [READER_ARNS["cmms_maintenance"]]


def test_the_erp_family_uses_its_own_map_host_and_owner(env):
    login = db_routing.system_agent_reader_login(
        "erp_data", _owner(ERP_HOST, "erp_admin", ERP_OWNER_PW, "erp_simulation"))
    assert (login["user"], login["password"], login["dbname"]) == (
        "adt_reader_erp_data", "r-erp", "erp_simulation")


def test_the_seed_resident_storyline_schema_uses_the_seed_map(env):
    assert db_routing.system_agent_reader_login("mes_line3", _owner())["user"] == "adt_reader_mes_line3"


def test_a_secret_shaped_owner_login_is_replaced_too(env):
    """The admin re-sync path carries the owner secret's own key spelling."""
    target = {"host": SEED_HOST, "port": 5432, "dbname": "adt_demo", "username": "postgres",
              "password": OWNER_PW, "secret_arn": SEED_OWNER_ARN}
    assert db_routing.system_agent_reader_login("cmms_maintenance", target)["user"] == \
        "adt_reader_cmms_maintenance"


def test_a_map_without_the_schema_fails_closed(env):
    with pytest.raises(SystemReaderLoginError, match="plm_engineering"):
        db_routing.system_agent_reader_login("plm_engineering", _owner())


@pytest.mark.parametrize("secret", [
    {"username": "postgres", "password": "x"},          # names the owner
    {"username": "adt_reader_cmms_maintenance"},         # no password
    {"password": "x"},                                   # no username
])
def test_an_incomplete_or_owner_reader_secret_fails_closed(env, monkeypatch, secret):
    monkeypatch.setattr(db_routing, "_read_reader_secret", lambda arn: dict(secret))
    with pytest.raises(SystemReaderLoginError):
        db_routing.system_agent_reader_login("cmms_maintenance", _owner())


def test_an_unreadable_reader_secret_fails_closed_without_the_message(env, monkeypatch):
    def boom(arn):
        raise RuntimeError("AccessDenied for arn:aws:secretsmanager:...:secret:x VALUE")

    monkeypatch.setattr(db_routing, "_read_reader_secret", boom)
    with pytest.raises(SystemReaderLoginError) as info:
        db_routing.system_agent_reader_login("cmms_maintenance", _owner())
    assert "VALUE" not in str(info.value) and "RuntimeError" in str(info.value)


def test_a_malformed_reader_map_fails_closed(env, monkeypatch):
    monkeypatch.setenv("DB_READER_SECRET_ARNS", '["not", "a", "map"]')
    with pytest.raises(SystemReaderLoginError):
        db_routing.system_agent_reader_login("cmms_maintenance", _owner())


def test_a_customer_login_on_the_same_host_is_kept(env):
    assert db_routing.system_agent_reader_login(
        "cmms_maintenance", _owner(user="anycompany_ro", password="theirs")) is None  # nosec B106 - fake test credentials, not real ones


def test_another_host_is_kept(env):
    assert db_routing.system_agent_reader_login(
        "cmms_maintenance", _owner(host="customer-db.example.com")) is None


def test_no_reader_map_keeps_the_login(env, monkeypatch):
    monkeypatch.delenv("DB_READER_SECRET_ARNS")
    assert db_routing.system_agent_reader_login("cmms_maintenance", _owner()) is None


def test_an_existing_reader_login_is_refreshed_not_kept(env):
    stale = _owner(user="adt_reader_cmms_maintenance", password="old")  # nosec B106 - fake test credentials, not real ones
    assert db_routing.system_agent_reader_login("cmms_maintenance", stale)["password"] == "r-cmms"


def test_the_pg_config_drops_every_key_the_owner_login_brought(env):
    config = {**_owner(), "secret_arn": SEED_OWNER_ARN, "username": "postgres",
              "connection_string": f"postgresql://postgres:{OWNER_PW}@{SEED_HOST}:5432/adt_demo",
              "schema": "cmms_maintenance", "concept_mappings": [{"c": 1}], "system_prompt": "p"}
    out = db_routing.system_agent_pg_config("cmms_maintenance", config)
    assert out["user"] == "adt_reader_cmms_maintenance" and out["password"] == "r-cmms"
    assert "secret_arn" not in out and "username" not in out and "connection_string" not in out
    assert out["concept_mappings"] == [{"c": 1}] and out["system_prompt"] == "p"
    assert OWNER_PW not in json.dumps(out) and "AdtDbSecret" not in json.dumps(out)


# --- provisioning (agent_manager) ------------------------------------------


@pytest.fixture
def mgr(env, monkeypatch):
    monkeypatch.setattr(model_config, "_ssm_value", lambda name, region: "")
    manager = AgentManager(mode="cloud")
    written: list = []

    def upsert(system_id, effective_config, system_prompt, tables_desc, region):
        written.append(dict(effective_config))
        return f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/agent-config/{system_id}-x1y2z3"

    monkeypatch.setattr(manager, "_upsert_agent_config_secret", upsert)
    manager.written = written
    return manager


def test_provisioning_writes_the_reader_into_the_agent_config_secret(mgr):
    env_vars = mgr._prepare_runtime_env(
        "cmms", "CMMS", "cmms_maintenance", "prompt", "tables", "PostgreSQL",
        {"secret_arn": SEED_OWNER_ARN}, _owner(), "us-east-1")
    (payload,) = mgr.written
    assert (payload["user"], payload["password"]) == ("adt_reader_cmms_maintenance", "r-cmms")
    blob = json.dumps(payload) + json.dumps(env_vars)
    assert OWNER_PW not in blob and SEED_OWNER_ARN not in blob and '"postgres"' not in blob
    assert env_vars["CONNECTION_SECRET_ARN"].endswith("secret:adt/agent-config/cmms-x1y2z3")
    assert set(env_vars) <= runtime_env_contract.known_keys("system")


def test_provisioning_fails_closed_when_the_schema_has_no_reader(mgr):
    with pytest.raises(SystemReaderLoginError):
        mgr._prepare_runtime_env("plm", "PLM", "plm_engineering", "p", "t", "PostgreSQL",
                                 {"secret_arn": SEED_OWNER_ARN}, _owner(), "us-east-1")
    assert mgr.written == []


def test_a_customer_postgres_registration_keeps_its_own_login(mgr):
    own = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/anycompany-abcdef"
    params = {"host": "db.anycompany.example.com", "port": "5432", "dbname": "plant",
              "user": "anycompany_ro", "password": "theirs"}
    mgr._prepare_runtime_env("anycompany", "Example Corp", "cmms_maintenance", "p", "t", "PostgreSQL",
                             {"secret_arn": own}, params, "us-east-1")
    (payload,) = mgr.written
    assert (payload["user"], payload["password"], payload["secret_arn"]) == ("anycompany_ro", "theirs", own)


def test_other_connection_types_are_untouched(mgr):
    config = {"workgroup": "wg", "database": "lake", "role_arn": "arn:aws:iam::111122223333:role/r"}
    mgr._prepare_runtime_env("lake", "Lake", "lake", "p", "t", "Amazon Athena", config, None, "us-east-1")
    assert mgr.written == [config]


class _Secrets:
    class exceptions:
        class ResourceExistsException(Exception):
            pass

    def __init__(self):
        self.values = []

    def create_secret(self, Name, SecretString):
        self.values.append(SecretString)
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{Name}-x1y2z3"}


def test_the_admin_resync_writes_the_reader_too(mgr, monkeypatch):
    secrets = _Secrets()
    import agents.agent_manager as am

    monkeypatch.setattr(am.boto3, "client", lambda name, **kw: secrets)
    monkeypatch.setattr(mgr, "_apply_business_context", lambda sid, cfg, region: cfg)
    monkeypatch.setattr(mgr, "_resync_force_runtime_pickup", lambda sid, arn, region: True)
    resolved = {"connection_type": "PostgreSQL", "schema": "cmms_maintenance",
                "secret_arn": SEED_OWNER_ARN, "host": SEED_HOST, "port": 5432,
                "dbname": "adt_demo", "username": "postgres", "password": OWNER_PW}
    assert mgr.resync_cloud_connection("cmms", "PostgreSQL", connection_config=resolved) is True
    (value,) = secrets.values
    payload = json.loads(value)
    assert (payload["user"], payload["password"]) == ("adt_reader_cmms_maintenance", "r-cmms")
    assert OWNER_PW not in value and SEED_OWNER_ARN not in value


def test_the_admin_resync_refuses_rather_than_writing_the_owner(mgr, monkeypatch):
    secrets = _Secrets()
    import agents.agent_manager as am

    monkeypatch.setattr(am.boto3, "client", lambda name, **kw: secrets)
    resolved = {"schema": "plm_engineering", "host": SEED_HOST, "dbname": "adt_demo",
                "username": "postgres", "password": OWNER_PW}
    assert mgr.resync_cloud_connection("plm", "PostgreSQL", connection_config=resolved) is False
    assert secrets.values == []


# --- the runtime ------------------------------------------------------------


def test_a_postgres_runtime_reads_no_secret_but_its_agent_config(monkeypatch):
    import agents.runtime_app as runtime_app

    monkeypatch.setattr("agents.resolve_evidence._with_cross_account_credentials",
                        lambda sid, cfg: pytest.fail("the System node's secret pointer was read"))
    config = {"host": SEED_HOST, "user": "adt_reader_cmms_maintenance", "password": "r-cmms"}
    out = runtime_app._resolve_runtime_connection_config("cmms", dict(config), "", "PostgreSQL")
    assert out == config


def test_other_runtimes_keep_the_cross_account_merge(monkeypatch):
    import agents.runtime_app as runtime_app

    calls = []
    monkeypatch.setattr("agents.resolve_evidence._with_cross_account_credentials",
                        lambda sid, cfg: calls.append(sid) or {**cfg, "external_id": "e"})
    out = runtime_app._resolve_runtime_connection_config(
        "sw", {"role_arn": "arn:aws:iam::111122223333:role/r"}, "", "SiteWise (Direct)")
    assert calls == ["sw"] and out["external_id"] == "e"
