# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Startup restore never starts a user's PostgreSQL login that can do more
than read.

The startup restore rebuilds every onboarded PostgreSQL system from its
System node and starts its agent with the effective login. A user
registration onboarded before the login check existed (or a node a late
provision refusal left onboarded) would come back on the next restart with
a superuser or schema-writer login. For a USER registration the restore now
checks the effective login (``onboard_preflight.check_pg_login_params``, the
check onboarding runs) before it reads the schema's tables or starts the
agent: positive evidence starts no agent, registers nothing and logs one
line naming the system and the fix. A check that cannot run keeps today's
restore and logs the warning. Demo systems are never checked. Offline.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
import agents.server as server  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402

SID = "customer-pg"
# Login name -> (superuser, can CREATE in the schema, can change a table).
_PRIVILEGES = {"owner": (True, True, True), "writer": (False, False, True),
               "reader": (False, False, False)}


def _row(origin="user") -> dict:
    return {"id": SID, "name": "Customer PG", "schema": "plant", "plant": "",
            "connection_type": "PostgreSQL", "system_type": "", "connection_origin": origin,
            "status": "onboarded", "removal_incomplete": None, "removal_full": None,
            "connection_config": "{}", "secret_arn": ""}


class _Conn:
    def __init__(self, user, log):
        self._user, self._log = user, log

    def cursor(self):
        user, log = self._user, self._log

        class _Cur:
            _row = None

            def execute(self, query, params=None):
                log.append((user, params))
                if user not in _PRIVILEGES:
                    raise RuntimeError("permission denied for table pg_authid")
                self._row = _PRIVILEGES[user]

            def fetchone(self):
                return self._row

            def close(self):
                pass
        return _Cur()

    def close(self):
        pass


@pytest.fixture
def restore(monkeypatch):
    """Drive ONE system of the SQL restore sweep with the given login. Returns
    ``run(login, origin) -> state`` with the privilege checks run, the table
    reads and the agent starts made."""
    def _run(login, origin="user"):
        state = type("State", (), {})()
        state.checks, state.introspected, state.started = [], [], []
        cold = SystemCatalog([])
        monkeypatch.setattr(server, "catalog", cold)
        monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
            "host": "db.example.internal", "port": "5432", "dbname": "plant",
            "search_path": "plant", "user": login, "password": "pw-" + login,
            "secret_arn": ""})
        monkeypatch.setattr(op, "_pg_connect",
                            lambda dsn: _Conn(urlparse(dsn).username, state.checks))
        monkeypatch.setattr(server, "_restore_sql_table_details",
                            lambda schema: state.introspected.append(schema)
                            or {"orders": [("id", "integer")]})
        monkeypatch.setattr(server, "_restore_one",
                            lambda **kw: state.started.append(kw["conn_params"]["user"])
                            or "mcp://restored")
        monkeypatch.setattr(server, "neptune_query", lambda cypher, params=None: [_row(origin)])
        state.restored = server._restore_sql_systems()
        state.catalog = cold
        return state
    return _run


@pytest.mark.parametrize("login", ["owner", "writer"])
def test_a_privileged_user_login_is_not_restored(restore, login, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.server"):
        state = restore(login)
    assert state.checks == [(login, ("plant", "plant"))]
    assert state.started == [], "an agent was started with a privileged login"
    assert state.introspected == [], "the schema was read with a privileged login"
    lines = [r.getMessage() for r in caplog.records if SID in r.getMessage()]
    assert any("Restore of customer-pg skipped" in m and "dedicated read-only login" in m
               and "No agent was started" in m for m in lines), lines
    assert all("pw-" not in m for m in lines), "a password reached the log"


def test_a_reader_user_login_is_restored(restore):
    state = restore("reader")
    assert state.checks == [("reader", ("plant", "plant"))]
    assert state.started == ["reader"] and state.introspected == ["plant"]


def test_an_unchecked_user_login_is_restored_with_the_warning_logged(restore, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.server"):
        state = restore("unknown")
    assert state.started == ["unknown"]
    assert any(SID in r.getMessage() and op.LOGIN_UNCHECKED_WARNING in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("login", ["owner", "reader"])
def test_a_demo_system_is_never_checked(restore, login):
    state = restore(login, origin=None)
    assert state.checks == []
    assert state.started == [login]


def test_the_login_check_reads_the_params_it_is_given(monkeypatch):
    seen = []
    monkeypatch.setattr(op, "check_registered_pg_login",
                        lambda dsn, schema: seen.append((dsn, schema)) or (None, []))
    op.check_pg_login_params({"host": "db.example.internal", "port": 5432, "dbname": "plant",
                              "user": "app reader", "password": "p@ss:w/rd",
                              "options": "-csearch_path=plant"}, "plant")
    (dsn, schema), = seen
    parts = urlparse(dsn)
    assert parts.scheme == "postgresql" and schema == "plant"
    assert (parts.hostname, parts.port, parts.path) == ("db.example.internal", 5432, "/plant")
    from urllib.parse import unquote
    assert (unquote(parts.username), unquote(parts.password)) == ("app reader", "p@ss:w/rd")


def test_the_login_check_brackets_an_ipv6_host(monkeypatch):
    seen = []
    monkeypatch.setattr(op, "check_registered_pg_login",
                        lambda dsn, schema: seen.append(dsn) or (None, []))
    op.check_pg_login_params({"host": "fd00::10", "port": "5432", "dbname": "plant",
                              "user": "r", "password": "p"}, "plant")
    assert urlparse(seen[0]).hostname == "fd00::10"
