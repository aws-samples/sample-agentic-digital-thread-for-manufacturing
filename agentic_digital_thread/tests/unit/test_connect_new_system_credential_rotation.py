# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect New System credential rotations and i3X / Tulip provision sources.
Offline — no AWS, no Neptune, no PostgreSQL, no network.

The rules these pin:

* a supported credential rotation (``write_connection``: the admin
  Edit-connection form, the Settings HighByte token card) never leaves a user
  system with a tokenless runtime or the deployment's credentials:
  (1) an entry pointer the graph node corroborates is server-written, so its
  non-PostgreSQL payload needs no binding; (2) a user MCP whose own secret is
  unusable gets a retryable error, never a tokenless runtime; (3) a rewrite of a
  bound secret keeps its binding, and a user PostgreSQL rotation lands in the
  system's own secret (never wiping its pointer, which left records-first
  sampling filling the DEPLOYMENT's database password for the user's host);
* an admin-edited HighByte node is ONE source;
* the onboarding harness says what to do when a minted Tulip secret is
  not reachable through the registration's pointer;
* i3X / Tulip PROVISION take a known system's URL + token from one
  server-side source (a different request URL is refused; request-only runs
  tokenless), for the agent start and the i3X hierarchy walk.

Demo guards: a demo PostgreSQL admin edit, the HighByte token card and a demo
provision write exactly what they wrote before.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, DSN, PASSWORD, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    ATTACKER, HB_TOKEN, HB_URL, MCP_URL, USER_TOKEN, _mcp_discover, _register_mcp,
    highbyte, httpx_spy)
from tests.unit.test_connect_new_system_reonboard_source import (  # noqa: E402
    HB_ARN, I3X, I3X_TOKEN, I3X_URL, TULIP, TULIP_URL, _node_row, _provision_mcp, _restart)

NEW_TOKEN = "rotated-token-321"  # nosec B105 - fixture
NEW_PW = "rotated-pw-654"  # nosec B105 - fixture
DEMO_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-db-credentials-XyZ123"


@pytest.fixture
def graph(secrets, monkeypatch):
    """The System nodes ``write_connection`` reads and writes (by id)."""
    nodes: dict = {}
    reads: list = []
    monkeypatch.setattr(cr, "_fetch_node_config",
                        lambda sid: reads.append(sid) or nodes.get(sid))

    def _write(cypher, params):
        if "SET s.connection_config = $cfg_json, s.secret_arn = $arn" in cypher:
            nodes[params["sid"]] = {"connection_config": params["cfg_json"],
                                    "secret_arn": params["arn"],
                                    "connection_type": params["ctype"]}
        return True
    monkeypatch.setattr(cr, "_neptune_write", _write)
    nodes["_reads"] = reads
    return nodes


def _provision(entry: dict, monkeypatch, provision: dict) -> tuple:
    from tests.unit import test_onboard_provision_node_write as h
    started = []
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: started.append(kw) or "mcp://test-agent")
    entry = {**entry, "_provision": provision}
    result, calls = h._run_provision(entry)
    return result, calls, started


# ── a rotation reaches the runtime; nothing is silently tokenless ────

def _provisioned_user_mcp(cat, graph):
    _register_mcp()
    graph[SID] = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")


def _rotate_mcp():
    cr.write_connection(SID, non_secret={"connection_type": "MCP Server", "url": MCP_URL,
                                         "auth_scheme": "bearer"},
                        secret={"token": NEW_TOKEN})


def test_md3_a_rewrite_of_a_bound_secret_keeps_its_binding(cat, secrets, graph):
    _provisioned_user_mcp(cat, graph)
    _rotate_mcp()
    assert secrets[CONN_ARN] == {"token": NEW_TOKEN, "connection_origin": "user",
                                 "system_id": SID}
    assert graph[SID]["secret_arn"] == CONN_ARN


def test_md_mcp_rotation_reaches_the_runtime_and_keeps_the_pointer(
        cat, secrets, graph, httpx_spy, monkeypatch):
    _provisioned_user_mcp(cat, graph)
    _rotate_mcp()
    _mcp_discover(SID, "")
    assert httpx_spy.calls and NEW_TOKEN in httpx_spy.calls[0][1], httpx_spy.calls
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert started and started[-1]["connection_config"]["token"] == NEW_TOKEN, started
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN and node["secret"] is None, node


def test_md1_an_unbound_payload_at_a_node_corroborated_pointer_is_used(
        cat, secrets, graph, monkeypatch):
    """Part 1 alone: a secret rewritten unbound (an older writer) at the pointer
    the node also holds is server-written, so its token is used."""
    _provisioned_user_mcp(cat, graph)
    secrets[CONN_ARN] = {"token": NEW_TOKEN}
    assert cr.registration_connection(SID, cat.get(SID))["token"] == NEW_TOKEN
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert started[-1]["connection_config"]["token"] == NEW_TOKEN, started


@pytest.mark.parametrize("payload", [
    {"token": NEW_TOKEN},                                                  # unbound, no node
    None,                                                                  # unreadable
])
def test_md2_an_unusable_own_secret_is_a_retryable_error_not_a_tokenless_runtime(
        cat, secrets, graph, httpx_spy, monkeypatch, payload):
    _register_mcp()
    if payload is None:
        secrets.clear()
    else:
        secrets[CONN_ARN] = payload
    res = _mcp_discover(SID, "")
    assert "error" in res and httpx_spy.calls == [], (res, httpx_spy.calls)
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert "error" in result and started == [] and calls == [], result


def test_md2_a_user_mcp_without_a_token_still_provisions(cat, secrets, graph, monkeypatch):
    _register_mcp(token="")  # nosec B106 - an empty test token
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert result.get("status") == "provisioned", result
    assert started[-1]["connection_config"]["token"] == ""


# PostgreSQL password rotation through the admin Edit-connection parse path.

def _provisioned_user_pg(cat, graph):
    _register_pg()
    graph[SID] = _node_row({"host": "db.example.internal", "port": "5432", "dbname": "lims",
                            "schema": "lims", "search_path": "lims"}, CONN_ARN, "PostgreSQL")


def _admin_edit_pg(body: dict):
    existing = cr.resolve_connection(SID)
    non_secret, secret, err = admin_routes._parse_connection_body(
        body, "PostgreSQL", existing=existing)
    assert err is None, err
    non_secret["connection_type"] = "PostgreSQL"
    cr.write_connection(SID, non_secret=non_secret, secret=secret or None)


NEW_DSN = (f"postgresql://lab_reader:{NEW_PW}@db2.example.internal:5433/lims"
           "?options=-csearch_path%3Dlims")


def test_md3_pg_password_rotation_lands_in_the_own_secret(cat, secrets, graph, monkeypatch):
    _provisioned_user_pg(cat, graph)
    _admin_edit_pg({"host": "db2.example.internal", "port": "5433", "password": NEW_PW})
    assert graph[SID]["secret_arn"] == CONN_ARN, graph[SID]
    assert NEW_PW not in json.dumps(graph[SID])
    assert secrets[CONN_ARN] == {"connection_string": NEW_DSN, "user": "lab_reader",
                                 "password": NEW_PW, "connection_origin": "user",
                                 "system_id": SID}
    db.invalidate_schema_conn_cache()
    params = db._records_conn_params_for_schema("lims")
    assert (params["host"], str(params["port"]), params["password"]) == \
        ("db2.example.internal", "5433", NEW_PW), {k: params[k] for k in ("host", "port")}
    entry = dict(cat.get(SID))
    result, calls, started = _provision(entry, monkeypatch, {
        "system_id": SID, "system_name": "Quality Lab", "plant": "Custom",
        "schema": "lims", "connection_type": "PostgreSQL"})
    assert started[-1]["conn_params"] == cr.pg_params_from_dsn(NEW_DSN)
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN


def test_md3_pg_rotation_keeps_the_database_and_schema_on_the_node(cat, secrets, graph):
    _provisioned_user_pg(cat, graph)
    _admin_edit_pg({"host": "db2.example.internal", "port": "5433", "password": NEW_PW})
    cfg = json.loads(graph[SID]["connection_config"])
    assert (cfg["dbname"], cfg["schema"], cfg["search_path"]) == ("lims", "lims", "lims"), cfg
    db.invalidate_schema_conn_cache()
    params = db._records_conn_params_for_schema("lims")
    assert params["dbname"] == "lims" and params["options"] == "-csearch_path=lims", params


def test_md3_pg_rotation_never_fills_the_deployment_password(cat, secrets, graph):
    """Records-first sampling right after an admin password edit of a user
    PostgreSQL system must use the user's own new credentials — never the
    deployment's POSTGRES_PASSWORD for the user's host."""
    _provisioned_user_pg(cat, graph)
    _admin_edit_pg({"host": "db2.example.internal", "port": "5433", "password": NEW_PW})
    db.invalidate_schema_conn_cache()
    params = db._records_conn_params_for_schema("lims")
    assert params["password"] != os.environ["POSTGRES_PASSWORD"], "deployment password used"
    assert params["password"] == NEW_PW


@pytest.mark.parametrize("body", [
    {"connection_string": "postgresql://db3.example.internal:5432/lims"
                          "?options=-csearch_path%3Dlims"},            # no user / password
    {"connection_string": f"postgresql://lab_reader:{NEW_PW}@db3.example.internal:5432/lims"
                          "?options=-csearch_path%3Dother"},           # a different schema
])
def test_md3_an_unusable_pg_rotation_is_refused_and_changes_nothing(cat, secrets, graph, body):
    _provisioned_user_pg(cat, graph)
    before_node, before_secret = dict(graph[SID]), dict(secrets[CONN_ARN])
    with pytest.raises(cr.ConnectionTargetError):
        _admin_edit_pg(body)
    assert graph[SID] == before_node and secrets[CONN_ARN] == before_secret


# Demo guards for write_connection.

def test_guard_demo_pg_admin_edit_writes_what_it_wrote_before(cat, secrets, graph, monkeypatch):
    """The WRITE is unchanged (pointer wiped, no secret, as on main). What
    happens NEXT changed on purpose: the node now names a host other than the
    env's, so the deployment's credentials are no longer filled in for it
    (re-onboarding the demo system restores its demo connection). The admin
    Edit refuses such a host change up front
    (test_connect_demo_pg_edit_refused.py)."""
    monkeypatch.setenv("POSTGRES_HOST", "seed")
    graph["cmms"] = _node_row({"host": "seed", "port": "5432", "dbname": "adt_demo",
                               "schema": "cmms_maintenance"}, DEMO_ARN, "PostgreSQL")
    cr.write_connection("cmms", non_secret={
        "connection_type": "PostgreSQL", "host": "seed2",
        "connection_string": "postgresql://postgres:pw@seed2:5432/adt_demo"}, secret=None)
    assert graph["cmms"]["secret_arn"] == "" and secrets == {}
    from agents.errors import ConnectionSecretUnavailableError
    with pytest.raises(ConnectionSecretUnavailableError):
        cr.pg_conn_params(cr.resolve_connection("cmms"))


@pytest.mark.parametrize("node", [None, _node_row({}, HB_ARN, "MCP Server")])
def test_guard_highbyte_token_card_writes_an_unbound_token(cat, secrets, graph, node):
    if node:
        graph["highbyte"] = node
        secrets[HB_ARN] = {"token": "old"}
    cr.write_connection("highbyte", non_secret={"connection_type": "MCP Server"},
                        secret={"token": HB_TOKEN})
    stored = [v for k, v in secrets.items() if "adt/conn/highbyte-AbCdEf" in k]
    assert stored == [{"token": HB_TOKEN}], secrets


def test_guard_demo_provision_with_a_server_pointer_reads_no_node(cat, secrets, graph):
    cr.write_connection("plm", non_secret={"connection_type": "PostgreSQL", "host": "seed"},
                        secret_arn=DEMO_ARN)
    assert graph["_reads"] == [] and graph["plm"]["secret_arn"] == DEMO_ARN


# ── an admin-edited HighByte node is one source ─────────────────────

ADMIN_HB_URL = "https://hb-admin.example.com/mcp"


@pytest.mark.parametrize("node_url, pair", [
    (ADMIN_HB_URL, (ADMIN_HB_URL, "Bearer admin-token")),     # admin-set URL ≠ live HighByte URL
    (HB_URL + "/mcp", (HB_URL + "/mcp", "Bearer " + HB_TOKEN)),  # admin-set URL == live URL
])
def test_lf_admin_edited_highbyte_node_is_one_source(cat, secrets, graph, highbyte,
                                                     httpx_spy, node_url, pair):
    cat.add({"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "onboarded"})
    graph["highbyte"] = _node_row({"url": node_url, "auth_scheme": "bearer"}, HB_ARN,
                                  "MCP Server")
    secrets[HB_ARN] = {"token": "admin-token"}
    _mcp_discover("highbyte", "")
    res = _mcp_discover("highbyte", ATTACKER)
    assert "error" in res
    assert httpx_spy.calls == [pair], httpx_spy.calls
    # HighByte's own token never travels to an admin-set URL, nor the node's to HighByte.
    for url, auth in httpx_spy.calls:
        assert (HB_TOKEN in auth) == (url == HB_URL + "/mcp")


# ── the harness says what to do after a mint with no pointer ───────

class _App:
    def __init__(self, entry):
        self._entry = entry

    def catalog(self):
        return {"tulip": self._entry} if self._entry is not None else {}


@pytest.mark.parametrize("entry, hint", [
    ({"id": "tulip", "connection_type": TULIP}, True),
    ({"id": "tulip", "connection_type": TULIP,
      "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/tulip-AbCdEf"},
     False),
])
def test_lg_harness_prints_the_remedy_when_the_mint_is_unreachable(monkeypatch, capsys,
                                                                   entry, hint):
    from tests.unit import test_onboarding_smoketest_connection_input as h
    client = h._FakeSecretsClient(exists=False)
    h._patch_boto(monkeypatch, client)
    assert h.smoke.supply_secret_out_of_band(_App(entry), "tulip", "basic-token-abc") is True
    out = capsys.readouterr().out
    assert ("Connect New System" in out and "API key" in out) is hint, out


def test_lg_include_ot_docs_name_the_tulip_requirement():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    for name in ("onboarding_smoketest.py", "onboarding_smoketest.sh"):
        src = (root / "scripts" / name).read_text()
        assert "registered in" in src and "Connect New System with its API key" in src, name


# ── i3X / Tulip provision — one source, no stored token to a request URL ─

def _restored_rest(cat, graph, secrets, ctype, url, token):
    _restart(cat, {"connection_type": ctype})
    graph[SID] = _node_row({"base_url": url, "auth_scheme": "bearer"}, CONN_ARN, ctype)
    secrets[CONN_ARN] = {"token": token}


def _provision_rest(cat, monkeypatch, ctype, body_url):
    return _provision(dict(cat.get(SID)), monkeypatch, {
        "system_id": SID, "system_name": "Q", "plant": "", "connection_type": ctype,
        "connection_string": body_url})


@pytest.mark.parametrize("ctype, url, token", [(I3X, I3X_URL, I3X_TOKEN),
                                               (TULIP, TULIP_URL, "tulip-basic")])
def test_px3_provision_refuses_a_different_request_url(cat, secrets, graph, monkeypatch,
                                                       ctype, url, token):
    _restored_rest(cat, graph, secrets, ctype, url, token)
    result, calls, started = _provision_rest(cat, monkeypatch, ctype, "https://attacker.example")
    assert "error" in result and started == [] and calls == [], (result, started)


@pytest.mark.parametrize("ctype, url, token", [(I3X, I3X_URL, I3X_TOKEN),
                                               (TULIP, TULIP_URL, "tulip-basic")])
def test_px3_provision_uses_the_node_pair(cat, secrets, graph, monkeypatch, ctype, url, token):
    _restored_rest(cat, graph, secrets, ctype, url, token)
    result, calls, started = _provision_rest(cat, monkeypatch, ctype, url + "/")
    cfg = started[-1]["connection_config"]
    assert (cfg["base_url"], cfg["token"]) == (url, token), cfg
    node = [c for c in calls if c["system_id"] == SID][-1]
    assert node["secret_arn"] == CONN_ARN and node["secret"] is None, node


def test_px3_provision_request_only_url_runs_tokenless(cat, secrets, graph, monkeypatch):
    _restart(cat, {"connection_type": I3X})
    result, calls, started = _provision_rest(cat, monkeypatch, I3X, I3X_URL)
    cfg = started[-1]["connection_config"]
    assert (cfg["base_url"], cfg["token"]) == (I3X_URL, ""), cfg


@pytest.fixture
def walker(monkeypatch):
    seen = []
    monkeypatch.setattr("agents.onboard_i3x.provision_i3x_hierarchy",
                        lambda sid, name, base_url, token="", auth_scheme="bearer":  # nosec B107
                        seen.append((base_url, token)) or {"assets": 0})
    monkeypatch.setattr("agents.onboard_i3x.bridge_i3x_schema", lambda sid: None)
    return seen


@pytest.mark.parametrize("body_url, expected", [
    ("https://attacker.example", []),
    (I3X_URL, [(I3X_URL, I3X_TOKEN)]),
])
def test_px3_i3x_walk_uses_one_source(cat, secrets, graph, walker, body_url, expected):
    _restored_rest(cat, graph, secrets, I3X, I3X_URL, I3X_TOKEN)
    onboard._provision_i3x_post_pass(ProvisionRequest(
        system_id=SID, system_name="Q", connection_type=I3X, connection_string=body_url))
    assert walker == expected, walker


def test_px3_user_registration_with_an_unusable_secret_is_refused(cat, secrets, graph,
                                                                  monkeypatch):
    """The MCP unusable-secret rule for the REST family: a user registration whose
    own secret is unusable never provisions a tokenless runtime."""
    _register({"name": "Quality Lab", "connection_type": I3X, "connection_string": I3X_URL,
               "connection_config": {"base_url": I3X_URL, "token": I3X_TOKEN}})
    secrets[CONN_ARN] = {"token": I3X_TOKEN}                         # unbound, no node
    result, calls, started = _provision_rest(cat, monkeypatch, I3X, I3X_URL)
    assert "error" in result and started == [], result
