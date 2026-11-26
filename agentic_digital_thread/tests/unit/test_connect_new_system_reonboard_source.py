# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System across a restart, and i3X / Tulip discover sources.
Offline — no AWS, no Neptune, no PostgreSQL, no network (``agents.server`` is
imported only under pytest's socket block, where its import-time AWS client
cannot connect).

The rules these pin:

* one pointer rule after a restart: a user registration's own secret is
  found through the catalog entry's pointer, else the graph node's (exact own
  ARN + a payload bound to the system), so a re-onboard discover still connects
  to the registered database, pinned;
* a user MCP system keeps its origin (bound ``adt/conn/<id>`` payload,
  re-marked by the direct-source restore sweep), and after a restart its
  discover and provision use its node's URL + token + pointer as one source;
  an unmarked MCP system with its own node connection is never answered with
  the HighByte bootstrap pair, and its provision keeps its node pointer;
* the chat tool does not advertise the untraced PostgreSQL DSN path;
* i3X and Tulip discover take a known system's URL + token from one
  server-side source; a different request URL is refused; a request-only URL
  runs tokenless;
* a registration-time token is read only through the entry's own
  pointer with a bound payload, never ``adt/conn/<id>`` by name, so a reused
  id cannot inherit a removed system's token.

Demo guards: the HighByte bootstrap (first onboard, after the admin token PUT,
after provision), a provisioned i3X / Tulip system's node token, the app's own
Tulip / i3X registration-with-credentials path and HighByte provision keep
resolving exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_mod  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    ATTACKER, HB_TOKEN, HB_URL, MCP_URL, PINNED_IP, USER_TOKEN, _mcp_discover,
    _register_mcp, highbyte, httpx_spy)

TULIP = "Tulip (Tables API)"
I3X = "SiteWise (i3X)"
TULIP_URL = "https://lab.tulip.example.com"
I3X_URL = "https://i3x.example.com"
I3X_TOKEN = "i3x-token-789"  # nosec B105 - fixture
HB_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/highbyte-HbHbHb"


def _arn(system_id: str) -> str:
    return f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{system_id}-AbCdEf"


@pytest.fixture
def by_name(secrets, monkeypatch):
    """GetSecretValue accepts a secret NAME as well as its ARN (as AWS does), so
    a by-name read of ``adt/conn/<id>`` finds a secret stored under its ARN."""
    def _fetch(secret_id):
        key = _arn(secret_id[len("adt/conn/"):]) if secret_id.startswith("adt/conn/") else secret_id
        if key not in secrets:
            raise RuntimeError("ResourceNotFoundException")
        return dict(secrets[key])
    monkeypatch.setattr(cr, "_fetch_secret", _fetch)
    return secrets


def _node(monkeypatch, nodes: dict):
    """Graph System nodes by id (absent id ⇒ no node)."""
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: nodes.get(sid))


def _node_row(cfg: dict, arn: str, ctype: str) -> dict:
    return {"connection_config": json.dumps(cfg), "secret_arn": arn, "connection_type": ctype}


def _restart(cat, entry_fields: dict):
    """An ECS restart: the in-memory catalog is rebuilt from the graph — no
    pointer, no connection_config, no connection_string — and the restore sweep
    re-marks the origin (exercised in test_mb_restore_*)."""
    cat.remove(SID)
    cat.add({"id": SID, "name": "Quality Lab", "plant": "", "system_type": "",
             "connection_string": "", "schema": "", "status": "onboarded", **entry_fields})


# ── re-onboard discover after a restart uses the registered DSN ──────

def test_ma_reonboard_discover_after_restart_uses_the_registered_dsn_pinned(
        cat, secrets, monkeypatch):
    _register_pg()
    _restart(cat, {"connection_type": "PostgreSQL", "schema": "lims",
                   "connection_origin": "user"})
    _node(monkeypatch, {SID: _node_row({"host": "db.example.internal", "port": "5432",
                                        "dbname": "lims", "schema": "lims",
                                        "search_path": "lims"}, CONN_ARN, "PostgreSQL")})
    assert onboard._pg_discover_target(SID, "", "lims") == (DSN, PINNED_IP, "")


def test_ma_node_pointer_to_an_unbound_or_foreign_secret_is_not_used(cat, secrets, monkeypatch):
    _register_pg()
    secrets[CONN_ARN] = {**secrets[CONN_ARN], "system_id": "someone-else"}
    _restart(cat, {"connection_type": "PostgreSQL", "schema": "lims",
                   "connection_origin": "user"})
    _node(monkeypatch, {SID: _node_row({"host": "db.example.internal"}, CONN_ARN, "PostgreSQL")})
    dsn, _, err = onboard._pg_discover_target(SID, "", "lims")
    assert dsn == "" and err, (dsn, err)


# ── user MCP keeps origin, token and pointer across a restart ───────

def test_mb_mcp_registration_secret_is_bound(cat, secrets):
    _register_mcp()
    assert secrets[CONN_ARN] == {"token": USER_TOKEN, "connection_origin": "user",
                                 "system_id": SID}


def _restore_direct(monkeypatch, resolved: dict) -> list:
    """Run server._restore's direct-source sweep for one MCP system; return the
    (system_id, origin) re-marks."""
    from unittest.mock import MagicMock, patch
    import agents.server as server
    from tests.unit import test_restore_isolation as iso
    cat_mock = MagicMock()
    cat_mock.get_all.return_value = [{"id": SID}]
    mgr = MagicMock()
    mgr.start_agent = iso._Starts(set(), RuntimeError("unused"))
    with patch.object(server, "_agent_manager", mgr), \
            patch.object(server, "neptune_query", MagicMock(side_effect=[
                [], [{"id": SID, "name": "Q", "plant": "", "connection_type": "MCP Server",
                       "status": "onboarded"}]])), \
            patch.object(server, "catalog", cat_mock), \
            patch.object(server, "add_system_agent", MagicMock()), \
            patch.object(cr, "resolve_connection", MagicMock(return_value=resolved)), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    return [c.args for c in cat_mock.set_connection_origin.call_args_list]


def test_mb_restore_re_marks_a_user_mcp_system_from_its_bound_secret(monkeypatch):
    resolved = {"system_id": SID, "connection_type": "MCP Server", "url": MCP_URL,
                "secret_arn": CONN_ARN, "token": USER_TOKEN, "connection_origin": "user"}
    assert _restore_direct(monkeypatch, resolved) == [(SID, "user")]


def test_mb_restore_does_not_re_mark_an_unbound_mcp_system(monkeypatch):
    resolved = {"system_id": SID, "connection_type": "MCP Server", "url": MCP_URL,
                "secret_arn": CONN_ARN, "token": USER_TOKEN}
    assert _restore_direct(monkeypatch, resolved) == []


def _restored_user_mcp(cat, monkeypatch):
    _register_mcp()
    _restart(cat, {"connection_type": "MCP Server", "connection_origin": "user"})
    _node(monkeypatch, {SID: _node_row({"url": MCP_URL, "auth_scheme": "bearer"},
                                       CONN_ARN, "MCP Server")})


def test_mb_restored_user_mcp_discover_uses_its_node_pair(cat, secrets, highbyte,
                                                          httpx_spy, monkeypatch):
    _restored_user_mcp(cat, monkeypatch)
    _mcp_discover(SID, MCP_URL)
    _mcp_discover(SID, "")
    assert [u for u, _ in httpx_spy.calls] == [MCP_URL, MCP_URL], httpx_spy.calls
    assert all(USER_TOKEN in auth for _, auth in httpx_spy.calls), httpx_spy.calls


def _provision_mcp(cat, monkeypatch, body_url=MCP_URL, system_id=SID):
    from tests.unit import test_onboard_provision_node_write as h
    started = []
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: started.append(kw) or "mcp://test-agent")
    entry = dict(cat.get(system_id))
    entry["_provision"] = {"system_id": system_id, "system_name": "Q", "plant": "",
                           "connection_type": "MCP Server", "connection_string": body_url}
    result, calls = h._run_provision(entry)
    return result, calls, started


def test_mb_restored_user_mcp_provision_keeps_token_and_pointer(cat, secrets, monkeypatch):
    _restored_user_mcp(cat, monkeypatch)
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert result["status"] == "provisioned", result
    assert started[-1]["connection_config"]["token"] == USER_TOKEN, started[-1]
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN and node["secret"] is None, node


def _unmarked_mcp_with_own_node(cat, secrets, monkeypatch):
    """An MCP system that lost its origin marker (e.g. an admin token rotation
    rewrote its secret without the binding) but has its own node connection."""
    _restart(cat, {"connection_type": "MCP Server"})
    secrets[CONN_ARN] = {"token": USER_TOKEN}
    _node(monkeypatch, {SID: _node_row({"url": MCP_URL, "auth_scheme": "bearer"},
                                       CONN_ARN, "MCP Server")})


def test_mb_unmarked_mcp_with_its_own_node_is_not_answered_by_highbyte(
        cat, secrets, highbyte, httpx_spy, monkeypatch):
    _unmarked_mcp_with_own_node(cat, secrets, monkeypatch)
    _mcp_discover(SID, MCP_URL)
    assert [u for u, _ in httpx_spy.calls] == [MCP_URL], httpx_spy.calls
    assert USER_TOKEN in httpx_spy.calls[0][1], httpx_spy.calls


def test_mb_unmarked_mcp_provision_keeps_its_node_token_and_pointer(cat, secrets, monkeypatch):
    _unmarked_mcp_with_own_node(cat, secrets, monkeypatch)
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert started[-1]["connection_config"]["token"] == USER_TOKEN, started[-1]
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN and node["secret"] is None, node


def test_mb_unmarked_mcp_provision_refuses_a_different_request_url(cat, secrets, monkeypatch):
    _unmarked_mcp_with_own_node(cat, secrets, monkeypatch)
    result, calls, started = _provision_mcp(cat, monkeypatch, body_url=ATTACKER)
    assert "error" in result and started == [] and calls == [], result


# ── HighByte demo guards (unchanged) ───────────────────────────────────────

def _add_highbyte(cat):
    cat.add({"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "registered"})


@pytest.mark.parametrize("node", [
    None,                                                           # first onboard
    _node_row({}, HB_ARN, "MCP Server"),                            # after the admin token PUT
    _node_row({"url": HB_URL + "/mcp", "auth_scheme": "bearer_literal"}, "", "MCP Server"),  # after provision
    _node_row({"url": "https://old-hb.example.com/mcp"}, "", "MCP Server"),  # stale URL after a move
])
def test_guard_highbyte_discover_keeps_its_bootstrap_pair(cat, secrets, highbyte, httpx_spy,
                                                          monkeypatch, node):
    _add_highbyte(cat)
    secrets[HB_ARN] = {"token": HB_TOKEN}
    _node(monkeypatch, {"highbyte": node} if node else {})
    for body_url in ("", HB_URL + "/mcp"):
        _mcp_discover("highbyte", body_url)
    # The bootstrap pair goes out as Bearer (agents/builtin_highbyte.py).
    assert httpx_spy.calls == [(HB_URL + "/mcp", f"Bearer {HB_TOKEN}")] * 2, httpx_spy.calls


@pytest.mark.parametrize("entry_cfg", [None, {"url": HB_URL + "/mcp", "token": HB_TOKEN,
                                              "auth_scheme": "bearer_literal"}])
def test_guard_highbyte_provision_source_is_unchanged(cat, secrets, monkeypatch, entry_cfg):
    _add_highbyte(cat)
    if entry_cfg:
        cat.get("highbyte")["connection_config"] = entry_cfg
    _node(monkeypatch, {"highbyte": _node_row({"url": HB_URL + "/mcp"}, "", "MCP Server")})

    class _Ctx:
        system_id = "highbyte"

        class body:
            connection_string = HB_URL + "/mcp"
    expected_cfg = entry_cfg or {"url": HB_URL + "/mcp", "token": "",
                                 "auth_scheme": "bearer_literal"}
    assert onboard._dp_mcp_source(_Ctx()) == (HB_URL + "/mcp", expected_cfg, "")


# ── the chat tool does not advertise the untraced DSN path ──────────

def test_lc_chat_tool_does_not_advertise_a_postgresql_dsn():
    description = onboard_agent.connect_new_system.tool_spec["description"]
    assert "dsn" not in description.lower(), description


# ── i3X / Tulip discover — one source, no stored token to a request URL ─

def _register_tulip(**cfg):
    return _register({"name": "Quality Lab", "connection_type": TULIP,
                      "connection_string": TULIP_URL,
                      "connection_config": {"base_url": TULIP_URL, **cfg}})


def _register_i3x():
    return _register({"name": "Quality Lab", "connection_type": I3X,
                      "connection_string": I3X_URL,
                      "connection_config": {"base_url": I3X_URL, "auth_scheme": "bearer",
                                            "token": I3X_TOKEN}})


@pytest.fixture
def tulip_get(monkeypatch):
    from agents import pinned_http
    calls = []

    def _get(url, headers=None, **kw):
        calls.append((url, (headers or {}).get("Authorization", "")))
        raise RuntimeError("stop after the first request")

    class _Client:
        """Tulip discovery streams its capped reads through ``client().stream``."""
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, headers=None, **kw):
            return _get(url, headers=headers)

    monkeypatch.setattr(pinned_http, "get", _get)
    monkeypatch.setattr(pinned_http, "client", lambda **kw: _Client())
    return calls


def _tulip_discover(system_id, body_url):
    body = DiscoverRequest(system_id=system_id, connection_type=TULIP,
                           connection_string=body_url)
    return asyncio.run(onboard._discover_rest_or_i3x(body, system_id, body_url, "", TULIP))


def test_px1_tulip_body_url_override_is_refused(cat, by_name, tulip_get):
    _register_tulip(api_key="k", api_secret="s")  # nosec B106 - fake test credentials, not real ones
    res = _tulip_discover(SID, "https://attacker.example")
    assert "error" in res and tulip_get == [], (res, tulip_get)


def test_px1_tulip_same_url_gets_its_registered_token(cat, by_name, tulip_get):
    _register_tulip(api_key="k", api_secret="s")  # nosec B106 - fake test credentials, not real ones
    _tulip_discover(SID, TULIP_URL + "/")
    assert [u for u, _ in tulip_get] == [TULIP_URL + "/api/v3/tables"], tulip_get
    assert tulip_get[0][1].startswith("Basic "), tulip_get


@pytest.fixture
def i3x_spy(monkeypatch):
    seen = []
    monkeypatch.setattr(onboard_agent, "_discover_i3x_data_model",
                        lambda entry: seen.append(entry) or {"error": "stop"})
    return seen


def _i3x_discover(system_id, body_url):
    body = DiscoverRequest(system_id=system_id, connection_type=I3X, connection_string=body_url)
    return asyncio.run(onboard._onboard_discover_i3x(body))


def test_px1_i3x_body_url_override_is_refused(cat, by_name, i3x_spy):
    _register_i3x()
    res = _i3x_discover(SID, "https://attacker.example")
    assert "error" in res and i3x_spy == [], (res, i3x_spy)


def test_px1_i3x_same_url_gets_its_registered_token(cat, by_name, i3x_spy):
    _register_i3x()
    _i3x_discover(SID, I3X_URL)
    assert i3x_spy[0]["connection_config"]["base_url"] == I3X_URL
    assert i3x_spy[0]["connection_config"]["token"] == I3X_TOKEN, i3x_spy


def test_px1_i3x_request_only_url_runs_tokenless(cat, by_name, i3x_spy):
    _i3x_discover("", "https://lab.example.com")
    assert i3x_spy[0]["connection_config"] == {"base_url": "https://lab.example.com",
                                               "token": "", "auth_scheme": "bearer"}, i3x_spy


# ── no token by name; a reused id never inherits an orphan ──────────

def test_px2_reused_id_does_not_inherit_an_orphan_token(cat, by_name, tulip_get):
    by_name[_arn(SID)] = {"token": "orphan-token", "auth_scheme": "basic",
                          "connection_origin": "user", "system_id": SID}
    _register_tulip()                                  # no credentials this time
    assert cr.resolve_registered_token(SID, entry=cat.get(SID))[0] == ""
    res = _tulip_discover(SID, TULIP_URL)
    assert "error" in res and tulip_get == [], (res, tulip_get)


@pytest.mark.parametrize("payload, token", [
    ({"token": "t1", "connection_origin": "user", "system_id": SID}, "t1"),
    ({"token": "t1"}, ""),                                                   # unbound
    ({"token": "t1", "connection_origin": "user", "system_id": "other"}, ""),  # bound elsewhere
])
def test_px2_entry_pointer_needs_a_bound_payload(cat, by_name, payload, token):
    _register_tulip(api_key="k", api_secret="s")  # nosec B106 - fake test credentials, not real ones
    by_name[CONN_ARN] = payload
    assert cr.resolve_registered_token(SID, entry=cat.get(SID))[0] == token


# ── i3X / Tulip demo guards (unchanged) ────────────────────────────────────

def test_guard_provisioned_tulip_and_i3x_keep_their_node_token(cat, by_name, tulip_get,
                                                               i3x_spy, monkeypatch):
    """A provisioned system's node points at its secret (written by provision,
    unbound): discover with the same URL keeps sending that token."""
    for sid, ctype, url in (("tulip-sys", TULIP, TULIP_URL), ("i3x-sys", I3X, I3X_URL)):
        cat.add({"id": sid, "name": sid, "connection_type": ctype, "schema": "",
                 "connection_string": "", "status": "onboarded"})
        by_name[_arn(sid)] = {"token": f"{sid}-token"}
    _node(monkeypatch, {
        "tulip-sys": _node_row({"base_url": TULIP_URL, "auth_scheme": "basic"},
                               _arn("tulip-sys"), TULIP),
        "i3x-sys": _node_row({"base_url": I3X_URL, "auth_scheme": "bearer"},
                             _arn("i3x-sys"), I3X)})
    _tulip_discover("tulip-sys", TULIP_URL)
    _i3x_discover("i3x-sys", I3X_URL)
    assert tulip_get == [(TULIP_URL + "/api/v3/tables", "Basic tulip-sys-token")], tulip_get
    assert i3x_spy[0]["connection_config"]["token"] == "i3x-sys-token", i3x_spy


def test_guard_harness_tulip_secret_is_bound(monkeypatch):
    """The onboarding harness's out-of-band Tulip mint writes the binding, so it
    never strips a registration's bound secret down to an unreadable one."""
    from tests.unit import test_onboarding_smoketest_connection_input as h
    smoke = h.smoke
    client = h._FakeSecretsClient(exists=False)
    h._patch_boto(monkeypatch, client)
    assert smoke.supply_secret_out_of_band(None, "tulip", "basic-token-abc") is True
    assert json.loads(client.created[0][1]) == {
        "token": "basic-token-abc", "auth_scheme": "basic",
        "connection_origin": "user", "system_id": "tulip"}
