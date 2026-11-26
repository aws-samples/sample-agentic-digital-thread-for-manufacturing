# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A privileged PostgreSQL login cannot reach a system agent through an Edit
or a swap after the provision check, and an unchecked login is never silent.

A user-registered PostgreSQL login that is a superuser, or can create objects
or change data in its schema, is refused wherever the connection can change
under the agent:

* the admin Edit checks the fully merged candidate before it is written, and
  answers 400 ``privileged_login`` with the advice (nothing written, nothing
  reconnected);
* the provision agent start re-reads the registered connection under the
  connection lock and checks it again, so a login swapped in after the
  connection phase's check never starts an agent.

When the check cannot run nothing is refused, and the warning reaches the
caller: the Edit response and the provision result (and so the provision
status reply) carry ``warnings``. A provision refused for a privileged login
reports reason ``privileged_login`` with the fixed advice, not only
``provision_failed``. Demo systems are never checked. Offline.
"""
from __future__ import annotations

import json
import os
import types
from urllib.parse import urlparse

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    NEW_PW, _admin_update, deployed)

# Login name -> (superuser, can CREATE in the schema, can change a table).
_PRIVILEGES = {"lab_owner": (True, True, True), "lab_writer": (False, False, True),
               "lab_reader": (False, False, False)}


def _dsn(user: str, schema: str = "lims") -> str:
    return (f"postgresql://{user}:{NEW_PW}@db.example.internal:5432/lims"
            f"?options=-csearch_path%3D{schema}")


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
def pg(monkeypatch):
    """The privilege check's connection, answered by login name. Returns the
    log of ``(login, params)`` checks run."""
    log: list = []
    monkeypatch.setattr(op, "_pg_connect",
                        lambda dsn: _Conn(urlparse(dsn).username, log))
    return log


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


# --- the admin Edit -------------------------------------------------------

@pytest.mark.parametrize("login", ["lab_owner", "lab_writer"])
def test_an_edit_to_a_privileged_login_is_refused_and_nothing_is_written(
        cat, secrets, graph, deployed, pg, monkeypatch, login):
    import agents.admin_routes as admin_routes
    _register_pg()
    before = dict(secrets[CONN_ARN])
    reconnects = []
    monkeypatch.setattr(admin_routes, "_persist_and_reconnect",
                        lambda *a, **k: reconnects.append(a) or pytest.fail("persisted"))
    res = _admin_update(SID, {"connection_string": _dsn(login)})
    body = _body(res)
    assert getattr(res, "status_code", None) == 400, body
    assert body["status"] == "validation_failed" and body["reason"] == "privileged_login"
    assert "dedicated read-only login" in body["detail"]
    assert "USAGE on schema 'lims'" in body["detail"]
    assert secrets[CONN_ARN] == before and SID not in graph and reconnects == []
    assert pg == [(login, ("lims", "lims"))]


def test_an_edit_to_a_reader_login_is_written_with_no_warning(cat, secrets, graph, deployed, pg):
    _register_pg()
    body = _body(_admin_update(SID, {"connection_string": _dsn("lab_reader")}))
    assert body.get("status") == "ok", body
    assert "warnings" not in body
    assert "lab_reader" in secrets[CONN_ARN]["connection_string"]


def test_an_edit_whose_check_cannot_run_is_written_and_says_so(cat, secrets, graph, deployed, pg):
    _register_pg()
    body = _body(_admin_update(SID, {"connection_string": _dsn("lab_unknown")}))
    assert body.get("status") == "ok", body
    assert body["warnings"] == [op.LOGIN_UNCHECKED_WARNING]


def test_an_edit_that_moves_the_schema_checks_the_new_schema(cat, secrets, graph, deployed, pg):
    _register_pg()
    res = _admin_update(SID, {"connection_string": _dsn("lab_writer", "lims_v2")})
    assert getattr(res, "status_code", None) == 400
    assert pg == [("lab_writer", ("lims_v2", "lims_v2"))]
    assert cat.get(SID)["schema"] == "lims", "the schema claim was not rolled back"


def test_the_edit_gate_never_checks_a_demo_system(monkeypatch):
    import agents.admin_routes as admin_routes
    monkeypatch.setattr(op, "check_registered_pg_login",
                        lambda *a, **k: pytest.fail("a demo login was checked"))
    demo = {"id": "erp", "connection_type": "PostgreSQL", "schema": "erp",
            "connection_origin": "demo"}
    assert admin_routes._registered_pg_login_gate(
        "erp", demo, "PostgreSQL", {"connection_string": _dsn("lab_owner")}) == (None, [])


# --- the provision agent start --------------------------------------------

class _Resolver:
    """``connection_resolver.registered_pg_dsn`` answering the CURRENT DSN."""
    def __init__(self, dsn):
        self.dsn = dsn

    def __call__(self, system_id, entry):
        return self.dsn, "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/x"


@pytest.fixture
def provision(monkeypatch, pg):
    import agents.connection_resolver as cr
    entry = {"id": SID, "connection_type": "PostgreSQL", "schema": "lims",
             "connection_origin": "user"}
    resolver = _Resolver(_dsn("lab_reader"))
    started: list = []
    monkeypatch.setattr(onboard, "_pg_entry_with_origin", lambda sid: (entry, ""))
    monkeypatch.setattr(onboard, "catalog", types.SimpleNamespace(get=lambda sid: entry))
    monkeypatch.setattr(cr, "is_user_registration", lambda e: True)
    monkeypatch.setattr(cr, "registered_pg_dsn", resolver)
    monkeypatch.setattr(onboard, "_start_pg_agent_with",
                        lambda ctx, prompt, tables, params, arn: started.append(params) or "mcp://x")
    from agents.models import ProvisionRequest
    ctx = onboard._ProvisionState(
        body=ProvisionRequest(system_id=SID, system_name="Quality Lab", schema="lims",
                              connection_type="PostgreSQL"),
        system_id=SID, system_name="Quality Lab", plant="", schema="lims",
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[])
    return types.SimpleNamespace(ctx=ctx, resolver=resolver, started=started, log=pg)


@pytest.mark.parametrize("login", ["lab_owner", "lab_writer"])
def test_a_login_swapped_in_after_the_check_never_starts_an_agent(provision, login):
    ctx = provision.ctx
    assert onboard._dp_pg_registered_connection(ctx) is None      # the reader passes
    provision.resolver.dsn = _dsn(login)                           # an Edit lands
    out = onboard._start_user_pg_agent(ctx, "prompt", "tables")
    assert isinstance(out, dict) and out["reason"] == "privileged_login"
    assert out["error"] == onboard._PRIVILEGED_LOGIN_PROVISION_ERROR
    assert provision.started == []
    assert [entry[0] for entry in provision.log] == ["lab_reader", login]


def test_a_reader_at_the_start_starts_the_agent(provision):
    out = onboard._start_user_pg_agent(provision.ctx, "prompt", "tables")
    assert out == "mcp://x" and len(provision.started) == 1
    assert provision.ctx.login_warnings == []


def test_an_unchecked_start_starts_and_records_the_warning_once(provision):
    provision.resolver.dsn = _dsn("lab_unknown")
    ctx = provision.ctx
    assert onboard._dp_pg_registered_connection(ctx) is None
    assert onboard._start_user_pg_agent(ctx, "prompt", "tables") == "mcp://x"
    assert ctx.login_warnings == [op.LOGIN_UNCHECKED_WARNING]


def test_a_privileged_login_at_the_connection_phase_reports_its_reason(provision):
    provision.resolver.dsn = _dsn("lab_owner")
    out = onboard._dp_pg_registered_connection(provision.ctx)
    assert out == {"error": onboard._PRIVILEGED_LOGIN_PROVISION_ERROR,
                   "reason": "privileged_login"}


def test_the_job_record_explains_a_privileged_login_refusal():
    record = onboard._provision_error_record(
        {"error": onboard._PRIVILEGED_LOGIN_PROVISION_ERROR, "reason": "privileged_login"})
    assert record == {"status": "error", "error": "provision_failed",
                      "reason": "privileged_login",
                      "detail": onboard._PRIVILEGED_LOGIN_PROVISION_ERROR,
                      "retryable": False}
    assert "dedicated read-only login" in record["detail"]
    # A different text under that reason is not echoed.
    assert onboard._provision_error_record(
        {"error": "something else", "reason": "privileged_login"}) == {
            "status": "error", "error": "provision_failed"}


# --- the provision result and status reply --------------------------------

def _result(monkeypatch, ctx):
    monkeypatch.setattr(onboard, "_dp_written_artifacts", lambda sid: ([], []))
    monkeypatch.setattr(onboard, "_dp_reapply_self_learned", lambda sid: 0)
    monkeypatch.setattr(onboard, "_dp_mapped_field_count", lambda sid: 0)
    monkeypatch.setattr(onboard, "_log_provision_summary", lambda *a, **k: None)
    return onboard._dp_result(ctx)


def test_the_provision_result_carries_an_unchecked_login_warning(provision, monkeypatch):
    provision.resolver.dsn = _dsn("lab_unknown")
    ctx = provision.ctx
    assert onboard._dp_pg_registered_connection(ctx) is None
    result = _result(monkeypatch, ctx)
    assert result["warnings"] == [op.LOGIN_UNCHECKED_WARNING]
    reply = onboard._finished_provision_payload({"status": "done", "result": result})
    assert reply["status"] == "done" and reply["warnings"] == [op.LOGIN_UNCHECKED_WARNING]


def test_a_checked_provision_result_has_no_warnings_key(provision, monkeypatch):
    ctx = provision.ctx
    assert onboard._dp_pg_registered_connection(ctx) is None
    assert "warnings" not in _result(monkeypatch, ctx)
