# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the Aurora UTC timezone fix (Wave 1 Task C3).

``fix_timezone`` lives in BOTH maintenance entry points:

* ``lambda-db-init/handler.py`` — direct-invoke ``{"action": "fix_timezone"}``
  short-circuits the CloudFormation lifecycle path;
* ``lambda_/lambda_seed_data.py`` — dispatched from its ``lambda_handler``.

Both connect to the ``postgres`` maintenance database and run
``ALTER DATABASE ... SET timezone = 'UTC'`` against the TARGET's actual
database names — ``erp_simulation`` (stacks/erp_database_stack.py:54) always,
and ``adt_demo`` (stacks/adt_seed_data_stack.py:96) only when that
database exists on the same host (it normally lives on a separate RDS
instance, so the conditional matters).

Fully offline: ``pgdriver.connect`` and ``boto3.client`` are monkeypatched —
no sockets, no AWS.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Repo root (for the lambda_ package, erp_simulation/, and lambda-db-init/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import pgdriver  # noqa: E402
from lambda_ import lambda_seed_data as seed_mod  # noqa: E402


def _load_db_init_handler():
    """lambda-db-init has a dash in the directory name — load it by path."""
    path = _REPO_ROOT / "lambda-db-init" / "handler.py"
    spec = importlib.util.spec_from_file_location("adt_db_init_handler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db_init_mod = _load_db_init_handler()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeCursor:
    def __init__(self, log, app_db_exists):
        self._log = log
        self._app_db_exists = app_db_exists
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = str(query)
        self._log.append((text, params))
        self._last = text

    def fetchone(self):
        if "pg_database" in self._last:
            return (1,) if self._app_db_exists else None
        if "SHOW timezone" in self._last:
            return ("UTC",)
        return (0,)

    def close(self):
        pass


class FakeConn:
    def __init__(self, log, app_db_exists, database):
        self._log = log
        self._app_db_exists = app_db_exists
        self.database = database
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self._log, self._app_db_exists)

    def close(self):
        self.closed = True


class FakeSecretsClient:
    def get_secret_value(self, SecretId):
        return {
            "SecretString": json.dumps({
                "host": "db.example.internal",
                "port": 5432,
                "username": "u",
                "password": "p",
            })
        }


@pytest.fixture()
def pg(monkeypatch):
    """Patch pgdriver.connect; return (statement_log, connected_dbs, flags)."""
    log = []
    connected = []
    flags = {"app_db_exists": True}

    def fake_connect(**kw):
        connected.append(kw.get("database"))
        return FakeConn(log, flags["app_db_exists"], kw.get("database"))

    # Both Lambdas import the driver lazily: the seed Lambda from
    # erp_simulation, db-init its layer copy, else erp_simulation's.
    monkeypatch.setattr(pgdriver, "connect", fake_connect)
    monkeypatch.setattr(db_init_mod._pgdriver(), "connect", fake_connect)
    return log, connected, flags


def _alters(log):
    return [t for t, _ in log if "ALTER DATABASE" in t]


# --------------------------------------------------------------------------- #
# lambda-db-init/handler.py
# --------------------------------------------------------------------------- #

def test_db_init_fix_timezone_targets_actual_db_names(pg, monkeypatch):
    log, connected, _ = pg
    monkeypatch.setattr(
        db_init_mod, "get_db_credentials",
        lambda: {"host": "db.example.internal", "port": 5432,
                 "username": "u", "password": "p"},
    )
    result = db_init_mod.fix_timezone()

    assert result["Status"] == "SUCCESS"
    # ALTER DATABASE runs from the postgres maintenance DB, then verification
    # reconnects to the ERP DB.
    assert connected[0] == "postgres"
    assert connected[-1] == "erp_simulation"

    alters = _alters(log)
    assert len(alters) == 2
    assert "erp_simulation" in alters[0] and "UTC" in alters[0]
    assert "adt_demo" in alters[1] and "UTC" in alters[1]
    assert result["Data"]["Databases"] == ["erp_simulation", "adt_demo"]

    # The existence probe for the app DB is parameterized, not interpolated.
    probe = next((t, p) for t, p in log if "pg_database" in t)
    assert probe[1] == ("adt_demo",)


def test_db_init_fix_timezone_skips_absent_app_db(pg, monkeypatch):
    log, _, flags = pg
    flags["app_db_exists"] = False
    monkeypatch.setattr(
        db_init_mod, "get_db_credentials",
        lambda: {"host": "db.example.internal", "port": 5432,
                 "username": "u", "password": "p"},
    )
    result = db_init_mod.fix_timezone()

    alters = _alters(log)
    assert len(alters) == 1
    assert "erp_simulation" in alters[0]
    assert not any("adt_demo" in a for a in alters)
    assert result["Data"]["Databases"] == ["erp_simulation"]


def test_db_init_handler_dispatches_fix_timezone(monkeypatch):
    sentinel = {"Status": "SUCCESS", "Data": {"Message": "sentinel"}}
    monkeypatch.setattr(db_init_mod, "fix_timezone", lambda: sentinel)
    result = db_init_mod.handler({"action": "fix_timezone"}, None)
    assert result is sentinel


def test_db_init_cfn_delete_path_unaffected():
    result = db_init_mod.handler({"RequestType": "Delete"}, None)
    assert result["Status"] == "SUCCESS"


# --------------------------------------------------------------------------- #
# lambda_/lambda_seed_data.py
# --------------------------------------------------------------------------- #

def _patch_seed_env(monkeypatch):
    monkeypatch.setenv("DB_SECRET_NAME", "erp-simulation/database-lambda-vpc")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    import boto3
    monkeypatch.setattr(
        boto3, "client", lambda service, region_name=None: FakeSecretsClient()
    )


def test_seed_fix_timezone_targets_actual_db_names(pg, monkeypatch):
    log, connected, _ = pg
    _patch_seed_env(monkeypatch)

    result = seed_mod.fix_timezone()

    assert result["status"] == "success"
    assert connected[0] == "postgres"
    assert connected[-1] == "erp_simulation"

    alters = _alters(log)
    assert len(alters) == 2
    assert "erp_simulation" in alters[0] and "UTC" in alters[0]
    assert "adt_demo" in alters[1] and "UTC" in alters[1]
    assert result["databases"] == ["erp_simulation", "adt_demo"]
    assert result["verified_timezone"] == "UTC"


def test_seed_fix_timezone_skips_absent_app_db(pg, monkeypatch):
    log, _, flags = pg
    flags["app_db_exists"] = False
    _patch_seed_env(monkeypatch)

    result = seed_mod.fix_timezone()

    alters = _alters(log)
    assert len(alters) == 1
    assert "erp_simulation" in alters[0]
    assert result["databases"] == ["erp_simulation"]


def test_seed_lambda_handler_dispatches_fix_timezone(monkeypatch):
    monkeypatch.setattr(seed_mod, "fix_timezone", lambda: {"status": "sentinel"})
    response = seed_mod.lambda_handler({"action": "fix_timezone"}, None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["action"] == "fix_timezone"
    assert body["result"] == {"status": "sentinel"}
