# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the admin ``POST /api/admin/connection/{system_id}/test`` route.

Exercises the real ``test_connection`` handler with the catalog, connection
probe, and audit log fully mocked — no live AWS / DB / HTTP / Neptune. Verifies:
  * 200 shape (status / latency_ms:int / connection_type / detail)
  * the response body never contains a 'token'/'password' substring
  * unknown system  -> 404
  * non-admin       -> 403

Runnable two ways (matches ``test_llm_settings_save.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_admin_connection_test.py
  * standalone:  python agentic_digital_thread/tests/unit/test_admin_connection_test.py

(``.venv-local`` doesn't ship pytest; the standalone runner works there.)
"""
import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
# Handler renamed from ``test_connection`` to ``probe_system_connection`` to avoid
# colliding with ``onboard_agent.test_connection`` and pytest's test_* collection.
from agents.admin_routes import probe_system_connection as connection_test_route  # noqa: E402
import agents.admin_routes as admin_routes  # noqa: E402
from agents.admin_routes import (  # noqa: E402
    update_connection as update_connection_route,
    update_highbyte_token as update_highbyte_token_route,
    i3x_explorer_link_route,
)


def _make_request(is_admin: bool, email: str = "admin@example.com"):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=is_admin,
        user=types.SimpleNamespace(email=email),
    )
    return req


class _FakeCatalog:
    def __init__(self, known: set):
        self._known = known

    def get(self, system_id):
        return {"connection_type": "PostgreSQL"} if system_id in self._known else None


def _patch_catalog(known: set):
    return patch("agents.catalog.catalog", _FakeCatalog(known))


def test_returns_200_shape_and_no_secret():
    """Happy path: 200 with correct shape, integer latency, and no secret leakage."""
    probe_result = {
        "status": "pass",
        "latency_ms": 42,
        "connection_type": "PostgreSQL",
        "detail": "SELECT 1 ok",
    }
    audit_calls = []

    with _patch_catalog({"cmms"}), \
            patch.object(cr, "resolve_connection",
                         lambda sid: {"connection_type": "PostgreSQL",
                                      "token": "tok-SECRET", "password": "pw-SECRET"}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: probe_result), \
            patch.object(cr, "audit_connection_event",
                         lambda *a, **k: audit_calls.append((a, k))):
        res = asyncio.run(connection_test_route("cmms", _make_request(is_admin=True)))

    assert res["status"] == "pass", res
    assert isinstance(res["latency_ms"], int) and res["latency_ms"] == 42, res
    assert res["connection_type"] == "PostgreSQL", res
    assert res["detail"] == "SELECT 1 ok", res

    # Secret-free response — neither key name with a value nor the secret values.
    blob = json.dumps(res).lower()
    assert "token" not in blob, blob
    assert "password" not in blob, blob
    assert "secret" not in blob, blob

    # Audit fired with the probe outcome.
    assert audit_calls, "audit_connection_event was not called"
    (args, kwargs) = audit_calls[0]
    assert args[0] == "test"
    assert args[1] == "cmms"
    assert kwargs.get("outcome") == "pass"


def test_unknown_system_returns_404():
    with _patch_catalog({"cmms"}):
        try:
            asyncio.run(connection_test_route("does-not-exist", _make_request(is_admin=True)))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code


def test_non_admin_returns_403():
    with _patch_catalog({"cmms"}):
        try:
            asyncio.run(connection_test_route("cmms", _make_request(is_admin=False)))
            raise AssertionError("expected HTTPException(403)")
        except HTTPException as e:
            assert e.status_code == 403, e.status_code


def test_blocked_target_returns_400():
    """A blocked_target probe result maps to HTTP 400 with the coarse reason."""
    probe_result = {
        "status": "fail",
        "latency_ms": 0,
        "connection_type": "REST API",
        "reason": "blocked_target",
        "detail": "connection target blocked by SSRF/scheme policy",
    }
    with _patch_catalog({"sitewise"}), \
            patch.object(cr, "resolve_connection", lambda sid: {"connection_type": "REST API"}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: probe_result), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(connection_test_route("sitewise", _make_request(is_admin=True)))

    # JSONResponse with status 400.
    assert getattr(res, "status_code", None) == 400, res
    body = json.loads(res.body)
    assert body["status"] == "fail", body
    assert body["reason"] == "blocked_target", body
    # No raw host/IP/port leaks in the coarse detail.
    for forbidden in ("169.254", "5432", "http://", "https://"):
        assert forbidden not in body["detail"], body


def test_normal_fail_returns_200_with_status_fail():
    """A reachable-but-unhealthy/unreachable target returns HTTP 200, status=fail."""
    probe_result = {
        "status": "fail",
        "latency_ms": 12,
        "connection_type": "PostgreSQL",
        "reason": "connection_refused",
        "detail": "connection refused",
    }
    with _patch_catalog({"erp"}), \
            patch.object(cr, "resolve_connection", lambda sid: {"connection_type": "PostgreSQL"}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: probe_result), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(connection_test_route("erp", _make_request(is_admin=True)))

    # Plain dict (FastAPI default 200) — not a JSONResponse with a 4xx/5xx code.
    assert isinstance(res, dict), res
    assert res["status"] == "fail", res
    assert res["reason"] == "connection_refused", res
    assert res["latency_ms"] == 12, res


def test_registered_system_without_node_probes_the_catalog_entry():
    """A system registered through /connect has no System node until it is
    provisioned, so the node-only resolver returns an unconfigured result. The
    Test button must probe what /connect stored on the catalog entry (its type,
    non-secret config and secret pointer), the same pre-node source the Edit
    form uses, instead of defaulting to PostgreSQL and failing with "no
    connection string configured" (seen live on a REST system)."""
    catalog_entry = {
        "connection_type": "Tulip (Tables API)",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/mes-tulip-AbCdEf",
        "connection_config": {"base_url": "https://mes.example.com", "target": "1001"},
    }
    unconfigured = {"system_id": "mes-tulip", "connection_type": None,
                    "connection_string": "", "base_url": "", "secret_arn": ""}
    probed = []

    def fake_probe(sid, resolved=None):
        probed.append(dict(resolved or {}))
        return {"status": "pass", "latency_ms": 5,
                "connection_type": resolved.get("connection_type"), "detail": "ok"}

    with _patch_update_catalog({"mes-tulip": catalog_entry}), \
            patch.object(cr, "resolve_connection", lambda sid: dict(unconfigured)), \
            patch.object(cr, "probe_connection", fake_probe), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(connection_test_route("mes-tulip", _make_request(is_admin=True)))

    assert res["status"] == "pass", res
    assert len(probed) == 1, probed
    got = probed[0]
    assert got["connection_type"] == "Tulip (Tables API)", got
    assert got["base_url"] == "https://mes.example.com", got
    assert got["target"] == "1001", got
    assert got["secret_arn"] == catalog_entry["secret_arn"], got


def test_provisioned_system_probes_the_node_not_the_catalog():
    """Once the System node exists it is the only source: catalog values never
    override what the resolver returned."""
    catalog_entry = {"connection_type": "Tulip (Tables API)",
                     "connection_config": {"base_url": "https://stale.example.com",
                                           "target": "9999"}}
    node = {"system_id": "mes-tulip", "connection_type": "Tulip (Tables API)",
            "base_url": "https://mes.example.com", "target": "1001",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/mes-tulip-AbCdEf"}
    probed = []

    def fake_probe(sid, resolved=None):
        probed.append(dict(resolved or {}))
        return {"status": "pass", "latency_ms": 5,
                "connection_type": resolved.get("connection_type"), "detail": "ok"}

    with _patch_update_catalog({"mes-tulip": catalog_entry}), \
            patch.object(cr, "resolve_connection", lambda sid: dict(node)), \
            patch.object(cr, "probe_connection", fake_probe), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        asyncio.run(connection_test_route("mes-tulip", _make_request(is_admin=True)))

    assert probed == [node], probed


# --------------------------------------------------------------------------- #
# PUT /api/admin/connection/{system_id} — update + live reconnect
# --------------------------------------------------------------------------- #

class _UpdateCatalog:
    """Fake catalog returning a fuller entry (name/schema/connection_type)."""

    def __init__(self, known: dict):
        self._known = known

    def get(self, system_id):
        return self._known.get(system_id)


def _patch_update_catalog(known: dict):
    return patch("agents.catalog.catalog", _UpdateCatalog(known))


class _RecorderSM:
    """Secrets Manager stand-in that records whether create/put were called."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self):
        self.create_calls = []
        self.put_calls = []
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException)

    def create_secret(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/conn/x"}

    def put_secret_value(self, **kwargs):
        self.put_calls.append(kwargs)
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/conn/x"}


def test_update_non_admin_returns_403():
    with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}):
        try:
            asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=False),
                {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp"}))
            raise AssertionError("expected HTTPException(403)")
        except HTTPException as e:
            assert e.status_code == 403, e.status_code


def test_update_unknown_system_returns_404():
    with _patch_update_catalog({"highbyte": {"name": "HighByte"}}):
        try:
            asyncio.run(update_connection_route(
                "nope", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp"}))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code


def test_update_validation_failed_does_not_write_and_returns_400():
    """A non-pass probe returns 400 validation_failed and writes NOTHING —
    put_secret_value/create_secret are never called."""
    fail_result = {"status": "fail", "latency_ms": 5, "connection_type": "MCP Server",
                   "reason": "connection_refused", "detail": "connection refused"}
    recorder = _RecorderSM()
    with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}), \
            patch.object(cr, "resolve_connection", lambda sid: {"connection_type": "MCP Server"}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: fail_result), \
            patch.object(cr, "_secrets_client", lambda: recorder), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(update_connection_route(
            "highbyte", _make_request(is_admin=True),
            {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp",
             "token": "tok-SECRET"}))

    assert getattr(res, "status_code", None) == 400, res
    body = json.loads(res.body)
    assert body["status"] == "validation_failed", body
    assert body["reason"] == "connection_refused", body
    # The secret was NOT written.
    assert recorder.create_calls == [] and recorder.put_calls == [], \
        (recorder.create_calls, recorder.put_calls)


def test_update_happy_path_writes_and_reconnects_in_order():
    """Happy path (LOCAL, MCP/client-based): writes the secret, then live-reconnects
    via the orchestrator MCP client swap — ``remove_system_agent`` →
    ``add_system_agent`` (NOT ``agent_manager.start_agent``, which in local mode
    spawned a failing/short-circuiting subprocess), and reports reconnected:true."""
    import agents.orchestrator as orch

    order = []
    write_calls = []
    pass_result = {"status": "pass", "latency_ms": 7, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "1 tool(s) discovered"}

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None):
        write_calls.append({"system_id": system_id, "non_secret": dict(non_secret),
                            "secret": secret})
        return "arn:aws:secretsmanager:::secret:adt/conn/highbyte"

    def _remove(sid):
        order.append(("remove_system_agent", sid))

    def _add(sid, url):
        order.append(("add_system_agent", sid, url))

    def _start(**kwargs):
        order.append(("start_agent", kwargs.get("system_id"),
                      kwargs.get("connection_type")))
        return "http://localhost:8100/mcp"

    fake_mgr = types.SimpleNamespace(start_agent=_start)
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte", "schema": "",
                                                  "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://mcp.example.com/mcp"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", _write), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(orch, "remove_system_agent", _remove), \
                patch.object(orch, "add_system_agent", _add):
            res = asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp",
                 "token": "tok-SECRET"}))
    finally:
        admin_routes.set_agent_manager(None)

    # The secret was written through write_connection.
    assert write_calls, "write_connection was not called"
    assert write_calls[0]["secret"] == {"token": "tok-SECRET"}, write_calls[0]
    # LOCAL client-swap order: remove → add, with NO subprocess spawn (start_agent).
    assert [c[0] for c in order] == ["remove_system_agent", "add_system_agent"], order
    assert "start_agent" not in [c[0] for c in order], order
    # add_system_agent got the (freshly-resolved) MCP URL.
    assert order[1][2] == "https://mcp.example.com/mcp", order
    # Response.
    assert isinstance(res, dict), res
    assert res["status"] == "ok", res
    assert res["system_id"] == "highbyte", res
    assert res["reconnected"] is True, res


def test_update_reconnect_failure_returns_200_reconnected_false():
    """When the live reconnect raises (e.g. add_system_agent fails), the endpoint
    still returns 200 with reconnected:false — the config write already succeeded
    and is persisted — NEVER an unhandled 500."""
    import agents.orchestrator as orch

    write_calls = []
    pass_result = {"status": "pass", "latency_ms": 3, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None):
        write_calls.append(system_id)
        return "arn:x"

    def _boom_add(sid, url):
        raise RuntimeError("orchestrator rebuild failed")

    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://mcp.example.com/mcp"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", _write), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", _boom_add):
            res = asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp",
                 "token": "tok-SECRET"}))
    finally:
        admin_routes.set_agent_manager(None)

    # 200 (plain dict), not a 500 JSONResponse.
    assert isinstance(res, dict), res
    assert res["status"] == "ok", res
    assert res["reconnected"] is False, res
    # The config write still happened.
    assert write_calls == ["highbyte"], write_calls


def test_update_success_audit_fires_ok_even_when_reconnect_false():
    """The success audit fires with outcome='ok' (and reconnected:false in the
    extra) INDEPENDENT of a failed reconnect."""
    import agents.orchestrator as orch

    audit_calls = []
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}

    def _audit(*a, **k):
        audit_calls.append((a, k))

    def _boom_add(sid, url):
        raise RuntimeError("reconnect boom")

    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://mcp.example.com/mcp"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
                patch.object(cr, "audit_connection_event", _audit), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", _boom_add):
            res = asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp",
                 "token": "tok-SECRET"}))
    finally:
        admin_routes.set_agent_manager(None)

    assert res["reconnected"] is False, res
    # Exactly one audit, outcome 'ok', with reconnected:false in the extra.
    assert audit_calls, "audit_connection_event was not called"
    (args, kwargs) = audit_calls[-1]
    assert args[0] == "update", args
    assert kwargs.get("outcome") == "ok", kwargs
    assert kwargs.get("extra", {}).get("reconnected") is False, kwargs


def test_update_secret_only_succeeds_when_url_resolves_from_existing():
    """A secret-only body (token, NO url) succeeds when a url already resolves from
    the existing config — the legacy token-only rotation path. No 'requires url'."""
    import agents.orchestrator as orch

    write_calls = []
    pass_result = {"status": "pass", "latency_ms": 2, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None):
        write_calls.append({"non_secret": dict(non_secret), "secret": secret})
        return "arn:x"

    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://existing.example.com/mcp"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", _write), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", lambda sid, url: None):
            res = asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "token": "rotated-tok"}))  # no url
    finally:
        admin_routes.set_agent_manager(None)

    # Not a 400 — the update went through to write.
    assert isinstance(res, dict), res
    assert res["status"] == "ok", res
    assert write_calls and write_calls[0]["secret"] == {"token": "rotated-tok"}, write_calls


def test_update_postgres_assembles_connection_string_and_preserves_password():
    """A PG edit posting host/port/password (no connection_string) assembles a
    connection_string from those + existing user/db; a BLANK password preserves the
    existing inline password."""
    existing_cs = ("postgresql://erp_user:old-pw@old-host:5432/erp_simulation"
                   "?options=-csearch_path%3Derp_data")
    captured = {}
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "PostgreSQL",
                   "reason": "ok", "detail": "SELECT 1 ok"}

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None):
        captured["non_secret"] = dict(non_secret)
        captured["secret"] = secret
        return ""

    # Case 1: new host/port + new password.
    with _patch_update_catalog({"erp": {"name": "ERP", "connection_type": "PostgreSQL"}}), \
            patch.object(cr, "resolve_connection",
                         lambda sid: {"connection_type": "PostgreSQL",
                                      "connection_string": existing_cs}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
            patch.object(cr, "write_connection", _write), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(update_connection_route(
            "erp", _make_request(is_admin=True),
            {"connection_type": "PostgreSQL", "host": "new-host", "port": "5433",
             "password": "new-pw"}))

    assert isinstance(res, dict) and res["status"] == "ok", res
    built = captured["non_secret"]["connection_string"]
    assert "new-host:5433" in built, built
    assert "erp_user:new-pw@" in built, built
    assert "/erp_simulation" in built, built
    assert "search_path%3Derp_data" in built, built
    # Password is inline in the connection_string; no separate secret created.
    assert captured["secret"] in (None, {}), captured["secret"]

    # Case 2: blank password preserves the existing inline password.
    captured.clear()
    with _patch_update_catalog({"erp": {"name": "ERP", "connection_type": "PostgreSQL"}}), \
            patch.object(cr, "resolve_connection",
                         lambda sid: {"connection_type": "PostgreSQL",
                                      "connection_string": existing_cs}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
            patch.object(cr, "write_connection", _write), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(update_connection_route(
            "erp", _make_request(is_admin=True),
            {"connection_type": "PostgreSQL", "host": "new-host", "port": "5433",
             "password": ""}))

    built2 = captured["non_secret"]["connection_string"]
    assert "erp_user:old-pw@new-host:5433" in built2, built2


def test_update_scheme_allowlist_is_resolver_constant_no_local_dup():
    """FIX 5 (DRY): admin_routes uses connection_resolver.SCHEME_ALLOWLIST and has
    no local _UPDATE_SCHEME_ALLOWLIST duplicate; a bad scheme is still rejected."""
    assert not hasattr(admin_routes, "_UPDATE_SCHEME_ALLOWLIST"), \
        "local _UPDATE_SCHEME_ALLOWLIST duplicate should be deleted"
    # The parser rejects a disallowed scheme using the resolver's constant.
    non_secret, secret, err = admin_routes._parse_connection_body(
        {"url": "http://evil.example.com/mcp"}, "MCP Server")
    assert non_secret is None and err and "not allowed" in err, (non_secret, err)
    # Sanity: the allowed schemes come straight from the resolver.
    assert "https" in cr.SCHEME_ALLOWLIST and "postgresql" in cr.SCHEME_ALLOWLIST


def test_update_masked_echo_is_secret_free():
    """The success echo carries a masked connection and never leaks the secret."""
    import agents.orchestrator as orch
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}
    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte", "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", lambda sid, url: None):
            res = asyncio.run(update_connection_route(
                "highbyte", _make_request(is_admin=True),
                {"connection_type": "MCP Server",
                 "url": "https://user:pw-SECRET@mcp.example.com/mcp",
                 "token": "tok-SECRET", "password": "pw-SECRET"}))

    finally:
        admin_routes.set_agent_manager(None)

    blob = json.dumps(res).lower()
    for leaked in ("tok-secret", "pw-secret"):
        assert leaked not in blob, blob
    # Masked echo present and inline URL creds masked.
    assert res["connection"]["url"] == "https://***@mcp.example.com/mcp", res["connection"]
    # Secret keys are not echoed as raw values.
    assert res["connection"].get("password", "***") == "***"


def test_highbyte_token_delegates_to_generalized_path():
    """The legacy PUT /highbyte-token shim delegates to the generalized update
    flow (system_id='highbyte', secret={'token':...}, validate=False — unconditional
    store) and still returns ok."""
    import agents.orchestrator as orch

    captured = {}

    def _do_update(system_id, request, body, *, connection_type=None, validate=True):
        captured["system_id"] = system_id
        captured["body"] = body
        captured["connection_type"] = connection_type
        captured["validate"] = validate
        return {"status": "ok", "system_id": system_id,
                "connection": {"connection_type": "MCP Server"}, "reconnected": True}

    # Build a request whose .json() returns the token body.
    req = _make_request(is_admin=True)

    async def _json():
        return {"token": "hb-tok"}
    req.json = _json

    with patch.object(admin_routes, "_do_update_connection", _do_update):
        res = asyncio.run(update_highbyte_token_route(req))

    assert captured["system_id"] == "highbyte", captured
    assert captured["connection_type"] == "MCP Server", captured
    assert captured["body"] == {"token": "hb-tok"}, captured
    # The legacy shim stores UNCONDITIONALLY — validate-first probe is skipped.
    assert captured["validate"] is False, captured
    assert res["status"] == "ok", res
    assert res.get("reconnected") is True, res
    # silence unused import lint (orch retained for parity with sibling tests)
    _ = orch


def test_update_sitewise_direct_invalidate_only_no_add_agent():
    """FIX 1: a SiteWise (Direct) / ``iotsitewise://`` update is NOT an orchestrator
    MCP client — it must NOT call ``add_system_agent``/``remove_system_agent``;
    reconnect is invalidate-only (and truthfully reports reconnected based on that,
    not a false ``True`` from a broken lazy MCP client built on a non-http URL)."""
    import agents.orchestrator as orch

    add_calls, remove_calls, invalidated = [], [], []
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "SiteWise (Direct)",
                   "reason": "ok", "detail": "1 asset model(s)"}

    def _resolve(sid):
        return {"connection_type": "SiteWise (Direct)",
                "connection_string": "iotsitewise://us-east-1.amazonaws.com",
                "secret_arn": "arn:secret:sw"}

    with _patch_update_catalog({"sitewise": {"name": "SiteWise",
                                             "connection_type": "SiteWise (Direct)"}}), \
            patch.object(cr, "resolve_connection", _resolve), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
            patch.object(cr, "write_connection", lambda sid, **k: "arn:secret:sw"), \
            patch.object(cr, "invalidate_secret", lambda arn: invalidated.append(arn)), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
            patch.object(orch, "remove_system_agent", lambda sid: remove_calls.append(sid)), \
            patch.object(orch, "add_system_agent", lambda sid, url: add_calls.append((sid, url))):
        res = asyncio.run(update_connection_route(
            "sitewise", _make_request(is_admin=True),
            {"connection_type": "SiteWise (Direct)",
             "connection_string": "iotsitewise://us-east-1.amazonaws.com"}))

    assert isinstance(res, dict) and res["status"] == "ok", res
    # Invalidate-only branch ran cleanly → reconnected True (NOT via a client swap).
    assert res["reconnected"] is True, res
    # No orchestrator client swap for a non-MCP-client system.
    assert add_calls == [], add_calls
    assert remove_calls == [], remove_calls
    # The cached secret WAS invalidated so the next resolve re-resolves.
    assert invalidated == ["arn:secret:sw"], invalidated


def test_update_mcp_client_removed_but_not_readded_returns_false():
    """FIX 1: when the system classifies as an http(s) MCP client (so the client is
    REMOVED) but no valid http(s) MCP url is resolvable to re-add, reconnect must
    return False — never a false True on a removed-but-not-re-added client."""
    import agents.orchestrator as orch

    removed, added = [], []
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}

    # Resolved carries only an http(s) base_url (enough to classify as an MCP http
    # client) but NO url/connection_string to re-add → removed-but-not-re-added.
    def _resolve(sid):
        return {"connection_type": "MCP Server", "base_url": "https://mcp.example.com/x"}

    with _patch_update_catalog({"hb": {"name": "HB", "connection_type": "MCP Server"}}), \
            patch.object(cr, "resolve_connection", _resolve), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
            patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
            patch.object(orch, "remove_system_agent", lambda sid: removed.append(sid)), \
            patch.object(orch, "add_system_agent", lambda sid, url: added.append((sid, url))):
        res = asyncio.run(update_connection_route(
            "hb", _make_request(is_admin=True),
            {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp"}))

    assert isinstance(res, dict) and res["status"] == "ok", res
    assert res["reconnected"] is False, res   # removed but not validly re-added
    assert removed == ["hb"], removed         # the client WAS removed
    assert added == [], added                 # ...but never re-added


def test_highbyte_token_validate_false_stores_even_when_probe_would_fail():
    """FIX 2 / Step-3 cut-over: the legacy highbyte-token route stores the token
    UNCONDITIONALLY (validate=False) — even if a live probe would fail. The token
    is written ONLY to the canonical conn/highbyte secret; the legacy
    highbyte-mcp-token secret is NEVER written (the dual-write bridge is gone). The
    route returns 200 (ok), NOT 400."""
    import agents.orchestrator as orch

    cr._secret_cache.clear()
    recorder = _RecorderSM()

    def _probe_fail(sid, resolved=None):
        # A probe that WOULD fail — but validate=False must skip it entirely.
        return {"status": "fail", "latency_ms": 0, "connection_type": "MCP Server",
                "reason": "connection_refused", "detail": "connection refused"}

    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    req = _make_request(is_admin=True)

    async def _json():
        return {"token": "hb-tok"}
    req.json = _json
    try:
        with _patch_update_catalog({"highbyte": {"name": "HighByte",
                                                 "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://hb.example.com/mcp"}), \
                patch.object(cr, "probe_connection", _probe_fail), \
                patch.object(cr, "_secrets_client", lambda: recorder), \
                patch.object(cr, "invalidate_secret", lambda arn: None), \
                patch.object(cr, "validate_connection_target",  # stored url is re-validated; no DNS offline
                             lambda target, connection_type="": "127.0.0.1"), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", lambda sid, url: None):
            res = asyncio.run(update_highbyte_token_route(req))
    finally:
        admin_routes.set_agent_manager(None)
        cr._secret_cache.clear()

    # Stored unconditionally: legacy-shaped 200 ok — NOT a 400 validation_failed.
    assert isinstance(res, dict), res
    assert res["status"] == "ok", res
    # Secret written ONLY to conn/highbyte — never the legacy bare-string secret.
    create_names = [c.get("Name") for c in recorder.create_calls]
    put_names = [c.get("SecretId") for c in recorder.put_calls]
    assert "adt/conn/highbyte" in create_names, create_names
    assert "adt/highbyte-mcp-token" not in create_names, create_names
    assert "adt/highbyte-mcp-token" not in put_names, put_names


def test_assemble_pg_percent_encodes_credentials_round_trip():
    """FIX 3: _assemble_pg_connection_string percent-encodes user/password so a
    password carrying URL-reserved chars (@ / : # space) does not corrupt the DSN —
    the assembled string parses back to the original host/db via urlsplit and the
    password round-trips through unquote."""
    from urllib.parse import urlsplit as _split, unquote as _unquote

    existing = {"connection_string":
                ("postgresql://erp_user:old-pw@db-host:5432/erp_simulation"
                 "?options=-csearch_path%3Derp_data")}
    raw_pw = "p@ss/w o:rd#x"   # @, /, space, :, # — all URL-reserved
    built = admin_routes._assemble_pg_connection_string(
        {"host": "db-host", "port": "5432"}, {"password": raw_pw}, existing)

    assert built, built
    parts = _split(built)
    # Structure parses cleanly despite the reserved chars in the password.
    assert parts.hostname == "db-host", parts.hostname
    assert parts.port == 5432, parts.port
    assert parts.path == "/erp_simulation", parts.path
    assert parts.username == "erp_user", parts.username
    # The raw (unencoded) password must NOT appear literally in the netloc...
    assert raw_pw not in parts.netloc, parts.netloc
    # ...and it round-trips through unquote intact.
    assert _unquote(parts.password) == raw_pw, (parts.password, raw_pw)
    # The preserved search_path query survives too.
    assert "search_path%3Derp_data" in built, built


def test_reconnect_exception_log_is_scrubbed_of_secret():
    """FIX 4: when the live reconnect raises with a message embedding the resolved
    secret, the logged record is scrubbed (secret -> ***) — the raw secret never
    reaches the logs."""
    import agents.orchestrator as orch
    from unittest.mock import MagicMock

    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}
    sensitive_value = "SEKRET-" "LEAK-" "12345"  # nosec B105 - fake test value, not a real secret

    def _resolve(sid):
        return {"connection_type": "MCP Server", "url": "https://hb.example.com/mcp",
                "token": sensitive_value}

    def _boom_add(sid, url):
        # A downstream error that embeds the resolved secret in its message.
        raise RuntimeError(f"orchestrator rebuild failed with token={sensitive_value}")

    fake_logger = MagicMock()
    fake_mgr = types.SimpleNamespace(start_agent=lambda **k: "http://localhost:8100/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"hb": {"name": "HB", "connection_type": "MCP Server"}}), \
                patch.object(cr, "resolve_connection", _resolve), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
                patch.object(admin_routes, "logger", fake_logger), \
                patch.object(orch, "remove_system_agent", lambda sid: None), \
                patch.object(orch, "add_system_agent", _boom_add):
            res = asyncio.run(update_connection_route(
                "hb", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://hb.example.com/mcp",
                 "token": sensitive_value}))
    finally:
        admin_routes.set_agent_manager(None)

    assert res["reconnected"] is False, res

    # Render every logged record (msg % args) and assert the secret is gone.
    rendered = []
    for call in (fake_logger.warning.call_args_list + fake_logger.error.call_args_list
                 + fake_logger.info.call_args_list):
        args = call.args
        if not args:
            continue
        try:
            rendered.append(str(args[0]) % tuple(args[1:]))
        except Exception:
            rendered.append(" ".join(str(a) for a in args))
    blob = " ".join(rendered)
    assert sensitive_value not in blob, blob   # raw secret NEVER logged
    assert "***" in blob, blob            # the scrub substituted the secret


def test_i3x_explorer_link_resolves_token_from_conn_highbyte():
    """Step-3 cut-over (T5.1): the i3x-explorer-link route sources the HighByte
    token from the canonical conn/highbyte secret via resolve_connection — it does
    NOT read the legacy adt/highbyte-mcp-token secret (no direct
    Secrets Manager read happens in the handler)."""
    import os as _os

    def _resolve(sid):
        assert sid == "highbyte", sid
        return {"connection_type": "MCP Server", "url": "https://hb.example.com/mcp",
                "token": "tok-from-conn"}

    # The handler must NOT construct a boto3 client to read any legacy secret.
    def _no_boto(*a, **k):
        raise AssertionError("i3x_explorer_link must not read a secret via boto3 directly")

    fake_boto3 = types.SimpleNamespace(client=_no_boto)
    with patch.dict(_os.environ, {"HIGHBYTE_URL": "https://hb.example.com"}, clear=False), \
            patch.object(cr, "resolve_connection", _resolve), \
            patch.dict(sys.modules, {"boto3": fake_boto3}):
        res = asyncio.run(i3x_explorer_link_route("highbyte", _make_request(is_admin=True)))

    assert res["status"] == "ok", res
    # The bearer token rides in the URL FRAGMENT (#token=...) so it never
    # reaches server logs / proxies / the Referer header. It must NOT appear
    # in the query string.
    assert "#token=tok-from-conn" in res["href"], res["href"]
    query = res["href"].split("#", 1)[0]
    assert "token=" not in query, res["href"]


_TESTS = [
    test_returns_200_shape_and_no_secret,
    test_blocked_target_returns_400,
    test_normal_fail_returns_200_with_status_fail,
    test_unknown_system_returns_404,
    test_non_admin_returns_403,
    test_registered_system_without_node_probes_the_catalog_entry,
    test_provisioned_system_probes_the_node_not_the_catalog,
    test_update_non_admin_returns_403,
    test_update_unknown_system_returns_404,
    test_update_validation_failed_does_not_write_and_returns_400,
    test_update_happy_path_writes_and_reconnects_in_order,
    test_update_reconnect_failure_returns_200_reconnected_false,
    test_update_success_audit_fires_ok_even_when_reconnect_false,
    test_update_secret_only_succeeds_when_url_resolves_from_existing,
    test_update_postgres_assembles_connection_string_and_preserves_password,
    test_update_scheme_allowlist_is_resolver_constant_no_local_dup,
    test_update_masked_echo_is_secret_free,
    test_highbyte_token_delegates_to_generalized_path,
    test_update_sitewise_direct_invalidate_only_no_add_agent,
    test_update_mcp_client_removed_but_not_readded_returns_false,
    test_highbyte_token_validate_false_stores_even_when_probe_would_fail,
    test_assemble_pg_percent_encodes_credentials_round_trip,
    test_reconnect_exception_log_is_scrubbed_of_secret,
    test_i3x_explorer_link_resolves_token_from_conn_highbyte,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} admin connection-test route tests passed "
          "(no real AWS/DB/HTTP)")
