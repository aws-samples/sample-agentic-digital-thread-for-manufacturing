# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.onboard._do_provision`` System-node connection writes.

Step 3 / Task 2.1 (seed-unification + Option 3): the provision-confirm path must
route the ``System``-node connection write through the SINGLE canonical
``connection_resolver.write_connection`` for EVERY connection_type, so seeded/demo
systems take the IDENTICAL path as user-onboarded ones. These tests assert, with
NO live AWS/Postgres/Neptune/HTTP:

  * PostgreSQL → ``write_connection`` is called with a CREDENTIAL-FREE non_secret
    (no password/user/inline connection_string) and ``secret_arn`` set to the
    SERVER-SIDE shared DB secret ARN (``secret`` is None — no conn/<id> minted).
  * MCP Server (with a token) → ``write_connection`` is called with
    ``secret={"token": ...}`` so onboarding mints conn/<id>.
  * SiteWise (Direct) → ``write_connection`` is called with ``secret=None`` and
    ``secret_arn=None`` (IAM-role auth).
  * A ``write_connection`` exception is fail-soft — provisioning still completes.

Runnable two ways (matches ``test_connection_resolver.py`` /
``test_onboard_agent_test_connection.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_provision_node_write.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_provision_node_write.py
"""
import os
import sys
from pathlib import Path

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

# A password MUST be set or demo.db_routing / db raise; tests never connect.
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402


class _FakeAgentManager:
    """Minimal stand-in for the agent manager — never touches AgentCore/AWS."""
    def __init__(self):
        self._agents = {}

    def start_agent(self, **kwargs):
        return "mcp://test-agent"

    def get_mcp_url(self, system_id):
        return "mcp://test-agent"


class _FakeCatalog:
    """Returns a single system entry under test; everything else is unknown.

    Also captures ``set_connection_config`` calls (the provision-path catalog
    persistence of the sanitized config + secret_arn POINTER) into ``persisted``.
    """
    def __init__(self, entry):
        self._entry = entry
        self.persisted = []

    def get(self, system_id):
        return self._entry if system_id == self._entry.get("id") else None

    def update_status(self, system_id, status):
        pass

    def set_connection_config(self, system_id, connection_config, *, secret_arn=None):
        self.persisted.append({
            "system_id": system_id,
            "connection_config": dict(connection_config or {}),
            "secret_arn": secret_arn,
        })
        return True

    def get_onboarded_ids(self):
        return set()


def _run_provision(entry, *, env=None, write_connection_raises=False, catalog_out=None):
    """Run ``_do_provision`` with all external side-effects mocked; capture the
    ``write_connection`` calls. Returns ``(result, captured_calls)``. Pass a list
    as ``catalog_out`` to receive the ``_FakeCatalog`` (its ``persisted`` records
    the provision-path catalog ``set_connection_config`` calls)."""
    captured = []

    def _fake_write_connection(system_id, *, non_secret, secret=None, secret_arn=None,
                               require_node=False, strict=False, allow_schema_change=False):
        captured.append({
            "system_id": system_id,
            "non_secret": dict(non_secret or {}),
            "secret": secret,
            "secret_arn": secret_arn,
        })
        if write_connection_raises:
            raise RuntimeError("simulated node/secret write failure")
        # Mirror the real return: the passed pointer, a minted conn/<id> ARN only
        # when a secret was actually written, else "" (IAM-role auth — no secret).
        return secret_arn or ("arn:aws:secretsmanager:test:conn" if secret else "")

    # Save originals.
    saved = {
        "cr_write_connection": cr.write_connection,
        "neptune_write": onboard.neptune_write,
        "neptune_query": onboard.neptune_query,
        "bedrock_call": onboard.bedrock_call,
        "pg": onboard.pg,
        "catalog": onboard.catalog,
        "agent_manager": onboard._agent_manager,
        "add_system_agent": onboard.add_system_agent,
        "detect_joins": onboard._detect_internal_joinpaths,
        "mqtt_start": onboard.mqtt_subscriber.start_subscriber,
    }
    import agents.model_config as model_config
    import agents.self_learning as self_learning
    import boto3
    saved_models = model_config.get_configured_models
    saved_accepted = self_learning.get_accepted_for_system
    saved_boto = boto3.client

    saved_env = {}
    for k, v in (env or {}).items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        cr.write_connection = _fake_write_connection
        onboard.neptune_write = lambda *a, **k: None
        onboard.neptune_query = lambda *a, **k: []
        onboard.bedrock_call = lambda *a, **k: "generated prompt"
        onboard.pg = lambda *a, **k: {"columns": [], "rows": [], "row_count": 0}
        _fake_cat = _FakeCatalog(entry)
        if catalog_out is not None:
            catalog_out.append(_fake_cat)
        onboard.catalog = _fake_cat
        onboard._agent_manager = _FakeAgentManager()
        onboard.add_system_agent = lambda *a, **k: None
        onboard._detect_internal_joinpaths = lambda *a, **k: []
        onboard.mqtt_subscriber.start_subscriber = lambda *a, **k: None
        model_config.get_configured_models = lambda: {"agent": "test-agent-model", "orchestrator": "test"}
        self_learning.get_accepted_for_system = lambda *a, **k: []

        def _boto_raises(*a, **k):
            raise RuntimeError("boto3 disabled in unit test")
        boto3.client = _boto_raises

        body = ProvisionRequest(**entry["_provision"])
        result = onboard._do_provision(body)
        return result, captured
    finally:
        cr.write_connection = saved["cr_write_connection"]
        onboard.neptune_write = saved["neptune_write"]
        onboard.neptune_query = saved["neptune_query"]
        onboard.bedrock_call = saved["bedrock_call"]
        onboard.pg = saved["pg"]
        onboard.catalog = saved["catalog"]
        onboard._agent_manager = saved["agent_manager"]
        onboard.add_system_agent = saved["add_system_agent"]
        onboard._detect_internal_joinpaths = saved["detect_joins"]
        onboard.mqtt_subscriber.start_subscriber = saved["mqtt_start"]
        model_config.get_configured_models = saved_models
        self_learning.get_accepted_for_system = saved_accepted
        boto3.client = saved_boto
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- secret-leak helpers -----------------------------------------------------

_SECRET_KEYS = ("password", "user", "username", "connection_string")
_SECRET_VALUES = ("supersecretpw", "demouser")


def _assert_credential_free(non_secret):
    for k in _SECRET_KEYS:
        assert k not in non_secret, f"credential-free node config must not carry {k!r}: {non_secret}"
    blob = repr(non_secret)
    assert "@" not in blob, f"node config must not carry inline userinfo: {non_secret}"
    for v in _SECRET_VALUES:
        assert v not in blob, f"node config leaked secret value {v!r}: {non_secret}"


# --- tests -------------------------------------------------------------------

_PG_ARN = "arn:aws:secretsmanager:us-east-1:444455556666:secret:adt-db-AbCdEf"


def _pg_entry():
    return {
        "id": "plm",
        "name": "PLM System",
        "system_type": "PLM",
        "connection_type": "PostgreSQL",
        "schema": "plm_engineering",
        "status": "available",
        # Catalog connection_string is NO LONGER read by provision (the node DSN is
        # derived server-side from demo_pg_dsn(schema)); kept only as catalog state.
        "connection_string": "postgresql://demouser:supersecretpw@db.example.com:5432/"
                             "adt_demo?options=-csearch_path%3Dplm_engineering",
        "_provision": {
            "system_id": "plm", "system_name": "PLM System", "plant": "All Sites",
            "schema": "plm_engineering", "connection_type": "PostgreSQL",
        },
    }


def test_postgres_writes_credential_free_config_with_db_secret_arn_pointer():
    """PostgreSQL → write_connection gets a credential-free non_secret + the
    server-side shared DB secret ARN (Option 3); no conn/<id> secret is minted.

    The DSN is derived SERVER-SIDE from ``demo_pg_dsn(schema)`` (env), not from the
    catalog entry's connection_string (the node is the sole source)."""
    result, calls = _run_provision(
        _pg_entry(),
        env={"DB_SECRET_ARN": _PG_ARN, "POSTGRES_HOST": "db.example.com",
             "POSTGRES_DB": "adt_demo"},
    )
    assert result["status"] == "provisioned", result
    pg_calls = [c for c in calls if c["system_id"] == "plm"]
    assert len(pg_calls) == 1, f"exactly one node write expected, got {calls}"
    call = pg_calls[0]
    ns = call["non_secret"]
    # Option 3: secret_arn points at the EXISTING shared DB secret; NO conn/<id>.
    assert call["secret"] is None, "SQL must NOT mint a conn/<id> secret"
    assert call["secret_arn"] == _PG_ARN, call
    # Credential-free DSN parts only.
    _assert_credential_free(ns)
    assert ns["connection_type"] == "PostgreSQL"
    assert ns["host"] == "db.example.com"
    assert ns["port"] == "5432"
    assert ns["dbname"] == "adt_demo"
    assert ns["schema"] == "plm_engineering"
    assert ns["search_path"] == "plm_engineering"


def test_mcp_with_token_writes_token_secret():
    """MCP Server with a token → write_connection gets secret={'token': ...} so
    onboarding mints conn/<id> (not the import-time catalog snapshot)."""
    entry = {
        "id": "highbyte", "name": "HighByte", "system_type": "IIoT",
        "connection_type": "MCP Server", "schema": "", "status": "available",
        "connection_config": {"url": "https://hb.example.com/mcp",
                              "token": "tok-abc-123", "auth_scheme": "bearer"},
        "_provision": {
            "system_id": "highbyte", "system_name": "HighByte", "plant": "All Sites",
            "connection_type": "MCP Server", "connection_string": "https://hb.example.com/mcp",
        },
    }
    result, calls = _run_provision(entry)
    assert result["status"] == "provisioned", result
    hb_calls = [c for c in calls if c["system_id"] == "highbyte"]
    assert len(hb_calls) == 1, f"exactly one node write expected, got {calls}"
    call = hb_calls[0]
    assert call["secret"] == {"token": "tok-abc-123"}, call
    assert call["secret_arn"] is None, call
    ns = call["non_secret"]
    assert ns["connection_type"] == "MCP Server"
    assert ns["url"] == "https://hb.example.com/mcp"
    assert ns["auth_scheme"] == "bearer"
    # The token is the SECRET, never the non-secret node config.
    assert "token" not in ns, ns
    assert "tok-abc-123" not in repr(ns), ns


def test_sitewise_direct_writes_no_secret_and_no_arn():
    """SiteWise (Direct) → write_connection gets secret=None AND secret_arn=None
    (IAM-role auth — no secret material at all)."""
    entry = {
        "id": "sitewise-direct", "name": "SiteWise Direct", "system_type": "Historian",
        "connection_type": "SiteWise (Direct)", "schema": "", "status": "available",
        "_provision": {
            "system_id": "sitewise-direct", "system_name": "SiteWise Direct",
            "plant": "All Sites", "connection_type": "SiteWise (Direct)",
        },
    }
    result, calls = _run_provision(entry)
    assert result["status"] == "provisioned", result
    sw_calls = [c for c in calls if c["system_id"] == "sitewise-direct"]
    assert len(sw_calls) == 1, f"exactly one node write expected, got {calls}"
    call = sw_calls[0]
    assert call["secret"] is None, call
    assert call["secret_arn"] is None, call
    assert call["non_secret"]["connection_type"] == "SiteWise (Direct)"
    assert call["non_secret"]["connection_string"].startswith("iotsitewise://")


def test_athena_cross_account_writes_role_arn_config_and_external_id_secret():
    """G2 threading: an Athena provision persists the typed keys + the cross-account
    ``role_arn`` in the node config, and routes ``external_id`` through the secret
    path (never the node config) — so the system survives an ECS restart and the
    external_id is not stored in cleartext on the node."""
    entry = {
        "id": "ags", "name": "AGS", "system_type": "DataLake",
        "connection_type": "Athena", "schema": "", "status": "available",
        "_provision": {
            "system_id": "ags", "system_name": "AGS", "plant": "All Sites",
            "connection_type": "Athena",
            "connection_config": {
                "workgroup": "primary", "database": "mfg_db", "catalog": "awsdatacatalog",
                "output_location": "s3://amzn-s3-demo-results/", "region": "us-east-1",
                "role_arn": "arn:aws:iam::444455556666:role/adt-readonly",
                "external_id": "ext-SECRET-999",
            },
        },
    }
    result, calls = _run_provision(entry)
    assert result["status"] == "provisioned", result
    ath = [c for c in calls if c["system_id"] == "ags"]
    assert len(ath) == 1, f"exactly one node write expected, got {calls}"
    call = ath[0]
    ns = call["non_secret"]
    assert ns["connection_type"] == "Athena"
    assert ns["workgroup"] == "primary"
    assert ns["database"] == "mfg_db"
    assert ns["catalog"] == "awsdatacatalog"
    assert ns["output_location"] == "s3://amzn-s3-demo-results/"
    assert ns["region"] == "us-east-1"
    assert ns["role_arn"] == "arn:aws:iam::444455556666:role/adt-readonly"
    # external_id is a SECRET — routed via secret=, never the node config.
    assert "external_id" not in ns, ns
    assert "ext-SECRET-999" not in repr(ns), ns
    assert call["secret"] == {"external_id": "ext-SECRET-999"}, call
    assert call["secret_arn"] is None, call


def test_athena_without_external_id_mints_no_secret():
    """G2: an Athena provision WITHOUT an external_id still persists role_arn in the
    node config but mints NO conn/<id> secret (secret=None) — cross-account with a
    role_arn only (no ExternalId) is the common case."""
    entry = {
        "id": "ags2", "name": "AGS2", "system_type": "DataLake",
        "connection_type": "Athena", "schema": "", "status": "available",
        "_provision": {
            "system_id": "ags2", "system_name": "AGS2", "plant": "All Sites",
            "connection_type": "Athena",
            "connection_config": {
                "workgroup": "primary", "database": "mfg_db",
                "role_arn": "arn:aws:iam::444455556666:role/adt-readonly",
            },
        },
    }
    result, calls = _run_provision(entry)
    assert result["status"] == "provisioned", result
    call = [c for c in calls if c["system_id"] == "ags2"][0]
    assert call["non_secret"]["role_arn"] == "arn:aws:iam::444455556666:role/adt-readonly"
    assert call["secret"] is None, call
    assert call["secret_arn"] is None, call


def test_write_connection_failure_does_not_abort_provisioning():
    """A write_connection exception is fail-soft: provisioning still completes."""
    result, calls = _run_provision(
        _pg_entry(), env={"DB_SECRET_ARN": _PG_ARN}, write_connection_raises=True,
    )
    # Provisioning completed despite the node-write failure.
    assert result["status"] == "provisioned", result
    # The write was still ATTEMPTED (guarded, not skipped).
    assert any(c["system_id"] == "plm" for c in calls), calls


def test_postgres_erp_schema_uses_erp_secret_arn():
    """ERP-routed schema prefers the ERP DB secret ARN (server-side routing)."""
    entry = _pg_entry()
    entry["schema"] = "erp_data"
    entry["_provision"]["schema"] = "erp_data"
    entry["connection_string"] = ("postgresql://erpuser:supersecretpw@erp.example.com:5432/"
                                  "erp_simulation?options=-csearch_path%3Derp_data")
    env = {
        "ERP_POSTGRES_HOST": "erp.example.com",
        "ERP_DB_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:444455556666:secret:erp-db-XyZ",
        "DB_SECRET_ARN": _PG_ARN,
    }
    result, calls = _run_provision(entry, env=env)
    assert result["status"] == "provisioned", result
    pg_calls = [c for c in calls if c["system_id"] == "plm"]
    assert len(pg_calls) == 1, calls
    assert pg_calls[0]["secret_arn"] == env["ERP_DB_SECRET_ARN"], pg_calls[0]
    _assert_credential_free(pg_calls[0]["non_secret"])
    assert pg_calls[0]["non_secret"]["dbname"] == "erp_simulation"


# --- provision-path catalog persistence (secret_arn pointer + sanitized config) ---

def test_provision_persists_catalog_pointer_and_sanitized_config_postgres():
    """PostgreSQL provision ALSO persists the sanitized non-secret config + the
    shared DB secret ARN POINTER onto the catalog entry — fulfilling
    metric_resolver._full_connection_config's contract without lazy backfill."""
    cat_out = []
    result, _calls = _run_provision(
        _pg_entry(),
        env={"DB_SECRET_ARN": _PG_ARN, "POSTGRES_HOST": "db.example.com",
             "POSTGRES_DB": "adt_demo"},
        catalog_out=cat_out,
    )
    assert result["status"] == "provisioned", result
    persisted = [p for p in cat_out[0].persisted if p["system_id"] == "plm"]
    assert len(persisted) == 1, cat_out[0].persisted
    p = persisted[0]
    assert p["secret_arn"] == _PG_ARN, p  # ARN POINTER, never a secret value
    cfg = p["connection_config"]
    # allowlist-sanitized: every persisted key is CATALOG_SAFE_CONFIG_KEYS
    assert set(cfg) <= set(cr.CATALOG_SAFE_CONFIG_KEYS), cfg
    _assert_credential_free(cfg)
    assert cfg["host"] == "db.example.com"
    assert cfg["dbname"] == "adt_demo"


def test_provision_persists_catalog_pointer_athena_cross_account():
    """Athena cross-account provision persists role_arn (identifier) in the
    sanitized catalog config and the minted conn/<id> ARN pointer; the
    external_id secret VALUE never lands on the catalog entry."""
    entry = {
        "id": "ags", "name": "AGS", "system_type": "DataLake",
        "connection_type": "Athena", "schema": "", "status": "available",
        "_provision": {
            "system_id": "ags", "system_name": "AGS", "plant": "All Sites",
            "connection_type": "Athena",
            "connection_config": {
                "workgroup": "primary", "database": "mfg_db",
                "region": "us-east-1",
                "role_arn": "arn:aws:iam::444455556666:role/adt-readonly",
                "external_id": "ext-SECRET-999",
            },
        },
    }
    cat_out = []
    result, _calls = _run_provision(entry, catalog_out=cat_out)
    assert result["status"] == "provisioned", result
    persisted = [p for p in cat_out[0].persisted if p["system_id"] == "ags"]
    assert len(persisted) == 1, cat_out[0].persisted
    p = persisted[0]
    # external_id was routed via secret= → the fake mints conn/<id>; the entry
    # carries the POINTER so _full_connection_config can merge the secret later.
    assert p["secret_arn"] == "arn:aws:secretsmanager:test:conn", p
    cfg = p["connection_config"]
    assert cfg["role_arn"] == "arn:aws:iam::444455556666:role/adt-readonly"
    assert cfg["workgroup"] == "primary"
    assert "external_id" not in cfg, cfg
    assert "ext-SECRET-999" not in repr(p), p


def test_provision_sitewise_direct_no_secret_persists_no_pointer():
    """SiteWise (Direct) with IAM-role/ambient auth (no secret, no ARN): nothing
    to point at → NO catalog persist is attempted, and nothing raises."""
    entry = {
        "id": "sitewise-direct", "name": "SiteWise Direct", "system_type": "Historian",
        "connection_type": "SiteWise (Direct)", "schema": "", "status": "available",
        "_provision": {
            "system_id": "sitewise-direct", "system_name": "SiteWise Direct",
            "plant": "All Sites", "connection_type": "SiteWise (Direct)",
        },
    }
    cat_out = []
    result, _calls = _run_provision(entry, catalog_out=cat_out)
    assert result["status"] == "provisioned", result
    # non_secret sanitizes to {} (connection_type/connection_string are not
    # allowlisted) and the returned ARN is "" → no pointer, no persist, no raise.
    assert [p for p in cat_out[0].persisted if p["system_id"] == "sitewise-direct"] == []


def test_provision_write_failure_skips_catalog_persist():
    """A write_connection failure is fail-soft AND skips the catalog persist —
    no half-written pointer for a connection that was never stored."""
    cat_out = []
    result, _calls = _run_provision(
        _pg_entry(), env={"DB_SECRET_ARN": _PG_ARN},
        write_connection_raises=True, catalog_out=cat_out,
    )
    assert result["status"] == "provisioned", result
    assert cat_out[0].persisted == [], cat_out[0].persisted


def test_metamodel_write_called_and_failure_is_non_fatal():
    """Provision writes the graph metamodel (g2) AFTER the status update,
    and a metamodel failure NEVER aborts provisioning (the try/except swallows
    it — the metamodel is prompt documentation, not provisioning state)."""
    import agents.metamodel as metamodel

    calls = []

    def _raising_write_metamodel():
        calls.append(True)
        raise RuntimeError("simulated metamodel write failure")

    saved = metamodel.write_metamodel
    metamodel.write_metamodel = _raising_write_metamodel
    try:
        result, _ = _run_provision(_pg_entry())
    finally:
        metamodel.write_metamodel = saved

    assert result["status"] == "provisioned", result   # provision unaffected
    assert calls, "write_metamodel must be attempted during provision"


_TESTS = [
    test_postgres_writes_credential_free_config_with_db_secret_arn_pointer,
    test_mcp_with_token_writes_token_secret,
    test_sitewise_direct_writes_no_secret_and_no_arn,
    test_athena_cross_account_writes_role_arn_config_and_external_id_secret,
    test_athena_without_external_id_mints_no_secret,
    test_write_connection_failure_does_not_abort_provisioning,
    test_postgres_erp_schema_uses_erp_secret_arn,
    test_provision_persists_catalog_pointer_and_sanitized_config_postgres,
    test_provision_persists_catalog_pointer_athena_cross_account,
    test_provision_sitewise_direct_no_secret_persists_no_pointer,
    test_provision_write_failure_skips_catalog_persist,
    test_metamodel_write_called_and_failure_is_non_fatal,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard provision node-write tests passed")
