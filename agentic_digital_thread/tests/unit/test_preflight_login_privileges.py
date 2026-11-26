# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A user's PostgreSQL login that can do more than read is refused.

A system agent runs SQL its model wrote; it needs SELECT on its schema only.
For a PostgreSQL connection the USER registered (their own login), positive
evidence that the login is a superuser, or can create objects or change data
in the target schema, blocks it: preflight answers ``blocked`` (reason
``privileged_login``, the advice as the detail, no sample read run), the
onboarding chat's ``test_connection`` answers ``failed`` with the same advice,
and provisioning returns it as an error before any graph write. When the
privilege check itself cannot run, nothing is blocked and a warning says the
login could not be checked. A demo system (the deployment's own login, which
the system agent never uses: it gets the schema's reader) is not checked.

Offline: a fake ``agents.pgdriver.connect`` answers the catalog query.
"""
from __future__ import annotations

import time
import types

import pytest

import agents.onboard_preflight as op
from agents import pgdriver

_RESOLVED = {"system_id": "anycompany", "connection_type": "PostgreSQL",
             "connection_string": "postgresql://anycompany_rw@db.example.com:5432/plant",
             "schema": "plant"}
_OK_PROBE = {"status": "pass", "reason": "ok", "detail": "SELECT 1 ok", "latency_ms": 8,
             "connection_type": "PostgreSQL"}
_PRIVILEGED = [(True, True, True), (True, False, False), (False, True, False),
               (False, False, True)]


class _Cursor:
    def __init__(self, log, privileges):
        self._log, self._privileges, self._rows = log, privileges, []

    def execute(self, query, params=None):
        if not isinstance(query, str):  # a pgdriver.sql composition (the sample read)
            query = query.as_string(None)
        if "information_schema.tables" in query:
            self._rows = [("orders",)]
        elif "information_schema.columns" in query:
            self._rows = [(3,)]
        elif "rolsuper" in query:
            self._log.append(("privileges", params))
            if isinstance(self._privileges, Exception):
                raise self._privileges
            self._rows = [self._privileges]
        else:
            self._log.append(("sample", query))
            self._rows = [(1,)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def close(self):
        pass


@pytest.fixture
def fake_pg(monkeypatch):
    """Install the fake driver; returns ``install(privileges) -> log``."""
    def _install(privileges):
        log: list = []

        def connect(conn_str, **kw):
            log.append(("connect", conn_str))
            return types.SimpleNamespace(
                cursor=lambda: _Cursor(log, privileges), close=lambda: None, autocommit=False)
        monkeypatch.setattr(pgdriver, "connect", connect)
        monkeypatch.setattr("agents.connection_security.validate_connection_target",
                            lambda *a, **k: "")
        return log
    return _install


@pytest.fixture
def run(monkeypatch, fake_pg):
    def _run(privileges, user=True):
        log = fake_pg(privileges)
        monkeypatch.setattr(op, "_resolve", lambda sid: dict(_RESOLVED))
        monkeypatch.setattr(op, "_bootstrap_unregistered", lambda resolved, sid: resolved)
        monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))
        monkeypatch.setattr(op, "_is_user_login", lambda sid: user)
        return op.run_preflight("anycompany", deadline=time.monotonic() + 300), log
    return _run


# --- preflight ------------------------------------------------------------

@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_a_privileged_login_blocks_preflight(run, privileges):
    result, log = run(privileges)
    assert result["verdict"] == "blocked"
    conn = result["connection"]
    assert conn["status"] == "fail" and conn["reason"] == "privileged_login"
    assert "dedicated read-only login" in conn["detail"]
    assert ("privileges", ("plant", "plant")) in log
    # The refusal comes before any table is read.
    assert not [entry for entry in log if entry[0] == "sample"]
    assert result["tables"] == []
    (warning,) = result["warnings"]
    assert "privileged_login" in warning and conn["detail"] in warning


def test_a_superuser_is_named(run):
    result, _ = run((True, True, True))
    assert result["connection"]["detail"].startswith("the PostgreSQL login is a superuser.")


@pytest.mark.parametrize("privileges", [(False, True, False), (False, False, True)])
def test_a_login_that_can_create_or_write_is_named(run, privileges):
    result, _ = run(privileges)
    assert result["connection"]["detail"].startswith(
        "the PostgreSQL login can create objects or change data in schema 'plant'.")


@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_the_refusal_says_what_login_to_use_and_why(run, privileges):
    """The advice names the exact grants (CONNECT, USAGE on the schema,
    SELECT on its tables, nothing else) and says why: the model's queries can
    read whatever the login can read."""
    result, _ = run(privileges)
    detail = result["connection"]["detail"]
    assert "dedicated read-only login" in detail
    assert "CONNECT" in detail
    assert "USAGE on schema 'plant'" in detail
    assert "SELECT on its tables" in detail
    assert "nothing else" in detail
    assert "can read whatever this login can read" in detail
    assert "\u2014" not in detail and "\u2013" not in detail


def test_a_read_only_login_is_ready_with_no_warning(run):
    result, log = run((False, False, False))
    assert result["verdict"] == "ready" and result["warnings"] == []
    assert [entry for entry in log if entry[0] == "sample"]


def test_a_failed_check_blocks_nothing_and_says_it_was_not_checked(run):
    result, log = run(RuntimeError("permission denied for table pg_authid"))
    assert result["verdict"] == "ready"
    (warning,) = result["warnings"]
    assert warning == op.LOGIN_UNCHECKED_WARNING
    assert "could not be checked" in warning
    assert [entry for entry in log if entry[0] == "sample"]


@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_a_demo_system_is_not_checked(run, privileges):
    result, log = run(privileges, user=False)
    assert result["verdict"] == "ready" and result["warnings"] == []
    assert not [entry for entry in log if entry[0] == "privileges"]


# --- the shared check (provisioning and the onboarding chat) --------------

@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_check_registered_login_refuses_a_privileged_login(fake_pg, privileges):
    fake_pg(privileges)
    refusal, warnings = op.check_registered_pg_login(_RESOLVED["connection_string"], "plant")
    assert refusal and "dedicated read-only login" in refusal and warnings == []


def test_check_registered_login_passes_a_reader(fake_pg):
    fake_pg((False, False, False))
    assert op.check_registered_pg_login(_RESOLVED["connection_string"], "plant") == (None, [])


def test_check_registered_login_fails_open_when_it_cannot_connect(monkeypatch, fake_pg):
    fake_pg((True, True, True))

    def refuse(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(pgdriver, "connect", refuse)
    assert op.check_registered_pg_login(_RESOLVED["connection_string"], "plant") == (
        None, [op.LOGIN_UNCHECKED_WARNING])


def test_check_registered_login_fails_open_on_a_failed_check(fake_pg):
    fake_pg(RuntimeError("permission denied"))
    assert op.check_registered_pg_login(_RESOLVED["connection_string"], "plant") == (
        None, [op.LOGIN_UNCHECKED_WARNING])


# --- provisioning ---------------------------------------------------------

def _provision_ctx(monkeypatch, user=True):
    import agents.connection_resolver as cr
    import agents.onboard as onboard
    entry = {"system_id": "anycompany", "connection_type": "PostgreSQL"}
    monkeypatch.setattr(onboard, "_pg_entry_with_origin", lambda sid: (entry, ""))
    monkeypatch.setattr(cr, "is_user_registration", lambda e: user)
    monkeypatch.setattr(cr, "registered_pg_dsn",
                        lambda sid, e: (_RESOLVED["connection_string"], "arn:secret"))
    ctx = types.SimpleNamespace(system_id="anycompany", schema="plant",
                                registered_pg_dsn="", registered_pg_arn="", login_warnings=[])
    return onboard, ctx


@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_provisioning_a_privileged_user_login_is_refused(monkeypatch, fake_pg, privileges):
    fake_pg(privileges)
    onboard, ctx = _provision_ctx(monkeypatch)
    out = onboard._dp_pg_registered_connection(ctx)
    assert out is not None and "dedicated read-only login" in out["error"]


def test_provisioning_a_reader_login_proceeds(monkeypatch, fake_pg):
    fake_pg((False, False, False))
    onboard, ctx = _provision_ctx(monkeypatch)
    assert onboard._dp_pg_registered_connection(ctx) is None
    assert ctx.registered_pg_dsn == _RESOLVED["connection_string"]


def test_provisioning_proceeds_when_the_check_cannot_run(monkeypatch, fake_pg):
    fake_pg(RuntimeError("permission denied"))
    onboard, ctx = _provision_ctx(monkeypatch)
    assert onboard._dp_pg_registered_connection(ctx) is None
    assert ctx.login_warnings == [op.LOGIN_UNCHECKED_WARNING]


def test_provisioning_a_privileged_login_reports_its_reason(monkeypatch, fake_pg):
    fake_pg((True, True, True))
    onboard, ctx = _provision_ctx(monkeypatch)
    out = onboard._dp_pg_registered_connection(ctx)
    assert out["reason"] == "privileged_login"

def test_provisioning_a_demo_system_never_checks_the_login(monkeypatch, fake_pg):
    log = fake_pg((True, True, True))
    onboard, ctx = _provision_ctx(monkeypatch, user=False)
    assert onboard._dp_pg_registered_connection(ctx) is None
    assert log == []


# --- the onboarding chat --------------------------------------------------

def _chat(monkeypatch, user=True):
    import agents.connection_resolver as cr
    import agents.onboard_agent as oa
    entry = {"system_id": "anycompany", "name": "Example ERP", "connection_type": "PostgreSQL",
             "schema": "plant"}
    monkeypatch.setattr(oa.catalog, "lookup", lambda sid: dict(entry))
    monkeypatch.setattr(cr, "probe_connection", lambda sid: dict(_OK_PROBE))
    monkeypatch.setattr(cr, "is_user_registration", lambda e: user)
    monkeypatch.setattr(cr, "registered_pg_dsn",
                        lambda sid, e: (_RESOLVED["connection_string"], "arn:secret"))
    return oa.test_connection


@pytest.mark.parametrize("privileges", _PRIVILEGED)
def test_chat_test_connection_refuses_a_privileged_login(monkeypatch, fake_pg, privileges):
    fake_pg(privileges)
    out = _chat(monkeypatch)("anycompany")
    assert out["status"] == "failed"
    assert "dedicated read-only login" in out["error"]


def test_chat_test_connection_passes_a_reader(monkeypatch, fake_pg):
    fake_pg((False, False, False))
    out = _chat(monkeypatch)("anycompany")
    assert out["status"] == "connected" and "warning" not in out


def test_chat_test_connection_warns_when_the_check_cannot_run(monkeypatch, fake_pg):
    fake_pg(RuntimeError("permission denied"))
    out = _chat(monkeypatch)("anycompany")
    assert out["status"] == "connected" and out["warning"] == op.LOGIN_UNCHECKED_WARNING


def test_chat_test_connection_never_checks_a_demo_login(monkeypatch, fake_pg):
    log = fake_pg((True, True, True))
    out = _chat(monkeypatch, user=False)("anycompany")
    assert out["status"] == "connected" and log == []
