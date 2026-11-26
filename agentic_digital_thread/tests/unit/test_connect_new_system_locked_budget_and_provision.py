# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System: the scan budget and the PostgreSQL agent start hold the
connection lock. Offline: no AWS, no Neptune, no PostgreSQL, no network.

* ``PUT /api/admin/scan-budget/{id}`` runs its node read-modify-write
  (``write_scan_budget``) under the system's keyed connection lock, offloaded.
  A connection Edit or a provision of the same system can no longer land
  between the budget's node read and its write (which would restore the stale
  endpoint next to the new credential pointer). Busy -> the Edit's retryable
  409, nothing written.
* a USER PostgreSQL provision re-reads its registered connection under
  the keyed lock right before the runtime starts and holds the lock through
  ``start_agent`` and the URL registration; an Edit that landed after the
  connection phase is what the runtime gets, never the phase-a snapshot.
"""
from __future__ import annotations

import inspect
import json
import os
import threading
import types

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401 - fixtures
    NEW_PW, _no_deploy_password, deployed)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_scan_budget_routes import _build_app  # noqa: E402

BUDGET = 5_000_000
NEW_DSN = (f"postgresql://lab_reader:{NEW_PW}@db2.example.internal:5432/lims"
           "?options=-csearch_path%3Dlims")


# ── the scan budget's read-modify-write holds the connection lock ──


def test_m1_the_budget_write_is_offloaded_and_locked():
    src = inspect.getsource(admin_routes.set_scan_budget)
    assert "await offload(_set_scan_budget_locked" in src, src
    assert "write_scan_budget(" not in src, src       # no inline call on the event loop
    helper = inspect.getsource(admin_routes._set_scan_budget_locked)
    assert "connection_lock(system_id)" in helper and "write_scan_budget" in helper


# ── a user PostgreSQL runtime starts from the connection as of start ─

def _pg_ctx() -> onboard._ProvisionState:
    body = ProvisionRequest(system_id=SID, system_name="Quality Lab", plant="",
                            connection_type="PostgreSQL", schema="lims")
    return onboard._ProvisionState(body=body, system_id=SID, system_name="Quality Lab",
                                   plant="", schema="lims", rules=[], mappings=[],
                                   connection_type="PostgreSQL", cm_items=[])


@pytest.fixture
def pg_runtime(monkeypatch):
    """Stand-ins for everything _dp_start_pg_agent does besides the connection."""
    from agents import connection_lock as cl
    started: list = []
    events: list = []

    class _Mgr:
        _agents: dict = {}

        def start_agent(self, **kw):
            started.append({**kw, "_lock_held": cl.is_held(SID)})
            hook = started_hook[0]
            if hook:
                hook()
            return "mcp://pg-agent"
    started_hook: list = [None]
    monkeypatch.setattr(onboard, "_agent_manager", _Mgr())
    monkeypatch.setattr(onboard, "add_system_agent",
                        lambda sid, url: events.append(("register", sid, url, cl.is_held(SID))))
    monkeypatch.setattr(onboard, "bedrock_call", lambda *a, **k: "prompt")
    monkeypatch.setattr(onboard, "_dp_pg_tables_desc", lambda ctx: "tables")
    return types.SimpleNamespace(started=started, events=events, hook=started_hook)


def _connection_phase(ctx) -> None:
    """The user-PG part of provision's (locked) connection phase."""
    assert onboard._dp_pg_registered_connection(ctx) is None
    onboard._dp_persist_sql_connection(ctx)


def _pg_edit_to_new_dsn():
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    return admin_routes._do_update_connection(SID, request, {"connection_string": NEW_DSN})


@pytest.fixture
def pg_edit_env(monkeypatch, secrets):
    monkeypatch.setattr(cr, "probe_connection", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(cr, "audit_connection_event", lambda *a, **k: None)
    monkeypatch.setattr(cr, "read_secret_uncached", lambda arn: dict(secrets[arn]))
    monkeypatch.setattr(admin_routes, "_reconnect_system", lambda *a, **k: True)


def test_m2_an_edit_after_the_connection_phase_is_what_the_runtime_starts_with(
        cat, secrets, graph, deployed, pg_edit_env, pg_runtime):
    """Provision paused after _dp_connection_phase; a PostgreSQL Edit to a second
    valid DSN completes; provision resumes at the runtime start."""
    _register_pg()
    ctx = _pg_ctx()
    _connection_phase(ctx)
    assert "@db.example.internal:" in ctx.registered_pg_dsn
    res = _pg_edit_to_new_dsn()
    assert isinstance(res, dict) and res.get("status") == "ok", getattr(res, "body", res)
    onboard._dp_start_pg_agent(ctx)
    params = pg_runtime.started[-1]["conn_params"]
    assert (params["host"], params["user"], params["password"]) == \
        ("db2.example.internal", "lab_reader", NEW_PW), params
    _no_deploy_password(params)
    assert PASSWORD not in json.dumps(params)
    node = json.loads(graph[SID]["connection_config"])
    assert node["host"] == "db2.example.internal" and graph[SID]["secret_arn"] == CONN_ARN
    assert ctx.registered_pg_dsn == NEW_DSN and ctx.registered_pg_arn == CONN_ARN


def test_m2_the_lock_is_held_through_start_and_url_registration(
        cat, secrets, graph, deployed, pg_edit_env, pg_runtime):
    """An Edit attempted while the runtime starts never lands between the
    re-read and the start: the provision holds the lock through the URL
    registration, so the Edit is refused at once (409 "provisioning", nothing
    written) instead of waiting behind the runtime creation."""
    _register_pg()
    ctx = _pg_ctx()
    _connection_phase(ctx)
    secret_before = dict(secrets[CONN_ARN])
    edit_done = threading.Event()
    seen: list = []
    edit_thread: list = []
    edit_result: list = []

    def _edit_during_start():
        def _run():
            edit_result.append(_pg_edit_to_new_dsn())
            pg_runtime.events.append(("edit",))
            edit_done.set()
        t = threading.Thread(target=_run)
        t.start()
        edit_thread.append(t)
        seen.append(edit_done.wait(2.0))       # the refusal comes at once
    pg_runtime.hook[0] = _edit_during_start
    onboard._dp_start_pg_agent(ctx)
    edit_thread[0].join(5)
    assert seen == [True], "the Edit waited behind the runtime creation"
    res = edit_result[0]
    assert getattr(res, "status_code", None) == 409, res
    assert json.loads(res.body)["reason"] == "provisioning", res.body
    assert secrets[CONN_ARN] == secret_before, "the refused Edit wrote"
    assert pg_runtime.started[-1]["_lock_held"] is True
    assert [e[0] for e in pg_runtime.events] == ["edit", "register"], pg_runtime.events
    assert pg_runtime.events[1][3] is True                  # registered under the lock
    assert pg_runtime.started[-1]["conn_params"]["host"] == "db.example.internal"


def test_m2_an_unusable_secret_at_start_raises_instead_of_starting(
        cat, secrets, graph, deployed, pg_runtime):
    from agents.errors import ConnectionSecretUnavailableError
    _register_pg()
    ctx = _pg_ctx()
    _connection_phase(ctx)
    secrets.pop(CONN_ARN)
    with pytest.raises(ConnectionSecretUnavailableError):
        onboard._dp_start_pg_agent(ctx)
    assert pg_runtime.started == []


def test_guard_m2_demo_pg_start_takes_no_lock_and_reads_no_registration(
        cat, secrets, graph, deployed, pg_runtime, monkeypatch):
    monkeypatch.setattr(cr, "registered_pg_dsn", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registered_pg_dsn called for a demo system")))
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "registered", "connection_origin": "demo"})
    body = ProvisionRequest(system_id="cmms", system_name="CMMS", plant="",
                            connection_type="PostgreSQL", schema="cmms_maintenance")
    ctx = onboard._ProvisionState(body=body, system_id="cmms", system_name="CMMS", plant="",
                                  schema="cmms_maintenance", rules=[], mappings=[],
                                  connection_type="PostgreSQL", cm_items=[])
    from agents import connection_lock as cl
    held: list = []
    real = onboard._pg_agent_conn_params
    monkeypatch.setattr(onboard, "_pg_agent_conn_params",
                        lambda c, s: held.append(cl.is_held("cmms")) or real(c, s))
    onboard._dp_start_pg_agent(ctx)
    assert held == [False] and pg_runtime.started
