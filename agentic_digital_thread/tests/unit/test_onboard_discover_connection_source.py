# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.onboard.onboard_discover`` connection-SOURCE routing.

Step 3 / Task 2.5 (revised): the discover read sites must source the CONNECTION
(the DSN / base_url / MCP url + token) from the submitted request body first, then
``connection_resolver.resolve_connection`` — NEVER from a catalog
``connection_string`` / ``connection_config`` snapshot. The catalog ``get()`` is
still allowed for IDENTITY/menu fields (``schema``/``connection_type``/``name``).

Every catalog entry below carries POISON connection values that must NOT appear in
what discover actually uses. No live AWS/Postgres/Neptune/HTTP:

  * PostgreSQL → ``conn_str`` prefers ``body.connection_string``; when empty it
    falls back to ``resolve_connection(...).connection_string`` (NOT the catalog
    DSN snapshot).
  * REST API / SiteWise (i3X) → ``effective_conn_str`` + the discover backend's
    ``connection_config`` (base_url/token/auth_scheme) come from body/resolved,
    not the catalog ``connection_config``.
  * MCP Server → connection comes from body → live-env/SSM bootstrap
    (``_get_highbyte_url``/token) → resolved node config, never the catalog
    ``connection_config`` snapshot.

Runnable two ways (matches ``test_onboard_provision_node_write.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_discover_connection_source.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_discover_connection_source.py
"""
import asyncio
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
import agents.catalog as catalog_mod  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402

# --- POISON catalog values: must never reach the discover connection path -----
CATALOG_POISON_DSN = (
    "postgresql://catalog:POISONpw@catalog-host:5432/catalogdb"
    "?options=-csearch_path%3Dplm_x"
)
CATALOG_POISON_URL = "https://catalog-POISON.example.com"
CATALOG_POISON_TOKEN = "tok-catalog-POISON"  # nosec B105 - test marker, not a real secret


class _FakeCatalog:
    """Returns a single system entry under test; everything else is unknown."""
    def __init__(self, entry):
        self._entry = entry

    def get(self, system_id):
        return self._entry if system_id == self._entry.get("id") else None


class _StopConnect(Exception):
    """Sentinel raised by the fake pgdriver.connect to short-circuit the PG path."""


def _patch(saved, obj, name, value):
    saved.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)


def _restore(saved):
    for obj, name, value in reversed(saved):
        setattr(obj, name, value)


def _no_poison(blob: str):
    assert "POISON" not in blob, f"catalog POISON connection value leaked: {blob}"


# --- PostgreSQL discover ------------------------------------------------------

def _pg_entry():
    return {
        "id": "plm",
        "name": "PLM System",
        "connection_type": "PostgreSQL",
        "schema": "plm_x",
        "connection_string": CATALOG_POISON_DSN,  # must NOT be used
    }


def _run_pg_discover(body_conn_str, resolved):
    """Run discover for the PG branch; capture the DSN handed to pgdriver.connect."""
    captured = {}

    def _fake_connect(dsn, *a, **k):
        captured["dsn"] = dsn
        raise _StopConnect()

    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(_pg_entry()))
        _patch(saved, cr, "resolve_connection", lambda sid: dict(resolved))
        _patch(saved, onboard.pgdriver, "connect", _fake_connect)
        body = DiscoverRequest(system_id="plm", connection_string=body_conn_str,
                               connection_type="PostgreSQL")
        try:
            asyncio.run(onboard.onboard_discover(body))
        except _StopConnect:
            pass
        return captured
    finally:
        _restore(saved)


def test_pg_discover_prefers_body_connection_string():
    """body.connection_string wins over both catalog and resolved."""
    resolved = {"connection_string": "postgresql://resolved@h:5432/db"}
    body_dsn = "postgresql://body@h:5432/db"
    captured = _run_pg_discover(body_dsn, resolved)
    assert captured.get("dsn") == body_dsn, captured
    _no_poison(captured.get("dsn", ""))


def test_pg_discover_falls_back_to_resolve_not_catalog():
    """With empty body, conn_str comes from resolve_connection — NOT the catalog DSN."""
    resolved = {"connection_string": "postgresql://resolved@h:5432/db"}
    captured = _run_pg_discover("", resolved)
    assert captured.get("dsn") == "postgresql://resolved@h:5432/db", captured
    _no_poison(captured.get("dsn", ""))


# --- REST API / SiteWise (i3X) discover ---------------------------------------

def _rest_entry():
    return {
        "id": "i3x",
        "name": "i3X SiteWise",
        "connection_type": "REST API",
        "schema": "rest",
        "connection_string": CATALOG_POISON_URL,
        "connection_config": {"base_url": CATALOG_POISON_URL, "token": CATALOG_POISON_TOKEN,
                              "auth_scheme": "bearer"},
    }


def _run_rest_discover(body_conn_str, resolved):
    """Run discover for the REST branch; capture the entry handed to _discover_openapi."""
    captured = {}

    def _fake_discover_openapi(entry):
        captured["entry"] = entry
        return {"tables": [{"name": "GET /objects", "columns": [], "row_count": 0}]}

    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(_rest_entry()))
        _patch(saved, cr, "resolve_connection", lambda sid: dict(resolved))
        _patch(saved, onboard_agent, "_discover_openapi", _fake_discover_openapi)
        _patch(saved, onboard, "bedrock_call", lambda *a, **k: "ai analysis")
        body = DiscoverRequest(system_id="i3x", connection_string=body_conn_str,
                               connection_type="REST API")
        asyncio.run(onboard.onboard_discover(body))
        return captured["entry"]
    finally:
        _restore(saved)


def test_rest_discover_sources_config_from_resolve_not_catalog():
    """REST connection_config (base_url/token/auth_scheme) comes from resolved."""
    resolved = {"base_url": "https://resolved.example.com", "token": "tok-resolved",
                "auth_scheme": "bearer_literal", "connection_string": "https://resolved-spec"}
    entry = _run_rest_discover("", resolved)
    cfg = entry["connection_config"]
    assert cfg.get("base_url") == "https://resolved.example.com", entry
    assert cfg.get("token") == "tok-resolved", entry
    assert cfg.get("auth_scheme") == "bearer_literal", entry
    # Empty body → the identity block first fills conn_str from
    # resolve_connection(...).connection_string, which then drives effective_conn_str
    # (the spec URL). Either way it is resolved-sourced and POISON-free.
    assert entry["connection_string"] == "https://resolved-spec", entry
    _no_poison(repr(entry))


def test_rest_discover_prefers_body_for_effective_conn_str():
    """body.connection_string wins for the effective_conn_str (spec URL) of a
    node that holds no token. (With a token, a request URL on another server
    is refused: test_connect_rest_discover_one_stored_url.py.)"""
    resolved = {"base_url": "https://resolved.example.com"}
    entry = _run_rest_discover("https://body-spec.example.com", resolved)
    assert entry["connection_string"] == "https://body-spec.example.com", entry
    _no_poison(repr(entry))


# --- MCP Server discover ------------------------------------------------------

def _mcp_entry():
    return {
        "id": "highbyte",
        "name": "HighByte",
        "connection_type": "MCP Server",
        "schema": "",
        "connection_string": CATALOG_POISON_URL,
        "connection_config": {"url": CATALOG_POISON_URL, "token": CATALOG_POISON_TOKEN,
                              "auth_scheme": "bearer_literal"},
    }


def _run_mcp_discover(body_conn_str, resolved, *, live_url="", live_token=""):
    """Run discover for the MCP branch; capture the entry handed to _discover_mcp."""
    captured = {}

    def _fake_discover_mcp(entry, system_id=""):
        captured["entry"] = entry
        return {"tables": [{"name": "browse_namespace",
                            "columns": [{"name": "AnyCompany.Milwaukee.Line1.temp",
                                         "data_type": "float"}]}]}

    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(_mcp_entry()))
        _patch(saved, cr, "resolve_connection", lambda sid: dict(resolved))
        _patch(saved, onboard_agent, "_discover_mcp", _fake_discover_mcp)
        # Live-env/SSM bootstrap helpers (imported inside the function from .catalog).
        _patch(saved, catalog_mod, "_get_highbyte_url", lambda: live_url)
        _patch(saved, catalog_mod, "_get_highbyte_token", lambda: live_token)
        body = DiscoverRequest(system_id="highbyte", connection_string=body_conn_str,
                               connection_type="MCP Server")
        asyncio.run(onboard.onboard_discover(body))
        return captured["entry"]
    finally:
        _restore(saved)


def test_mcp_discover_sources_config_from_resolve_not_catalog():
    """With no live-env and empty body, MCP url/token come from resolved — not catalog."""
    resolved = {"url": "https://resolved-mcp.example.com/mcp", "token": "tok-resolved",
                "auth_scheme": "bearer", "connection_string": "https://resolved-mcp.example.com/mcp"}
    entry = _run_mcp_discover("", resolved, live_url="", live_token="")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    cfg = entry["connection_config"]
    assert cfg.get("url") == "https://resolved-mcp.example.com/mcp", entry
    assert cfg.get("token") == "tok-resolved", entry
    assert cfg.get("auth_scheme") == "bearer", entry
    _no_poison(repr(entry))


def test_mcp_discover_live_env_wins_over_resolve():
    """Live-env/SSM bootstrap (_get_highbyte_url/token) takes precedence over resolved,
    and the catalog snapshot is never read."""
    resolved = {"url": "https://resolved-mcp.example.com/mcp", "token": "tok-resolved"}
    entry = _run_mcp_discover("", resolved, live_url="https://live-hb.example.com",  # nosec B106 - offline test fixture/placeholder credential, not a live secret
                              live_token="tok-live")
    cfg = entry["connection_config"]
    assert cfg.get("url") == "https://live-hb.example.com/mcp", entry
    assert cfg.get("token") == "tok-live", entry
    assert entry["connection_string"] == "https://live-hb.example.com/mcp", entry
    _no_poison(repr(entry))


# --- Regression: body connection_type wins over a mis-hydrated catalog entry ---

class _FakeSWPaginator:
    def paginate(self, **k):
        return iter([])  # no models/assets — SiteWise branch builds an empty hierarchy


class _FakeSWClient:
    def get_paginator(self, name):
        return _FakeSWPaginator()


def test_discover_routes_on_body_type_despite_mislabeled_catalog():
    """A SiteWise (Direct) re-onboard MUST route on the request-body connection_type
    even when the catalog entry was mis-hydrated as PostgreSQL with a hyphenated
    schema ('sitewise-direct'). Regression for the live 500 — validate_identifier(
    'sitewise-direct') — after a redeploy re-hydrated the persisted node as PostgreSQL
    and the old code overwrote body.connection_type with the stale catalog value."""
    import agents.aws_session as aws_session
    mislabeled = {
        "id": "example-sitewise", "name": "Example SiteWise",
        "connection_type": "PostgreSQL",   # mis-hydrated by server.py startup restore
        "schema": "sitewise-direct",        # hyphenated -> validate_identifier would reject
    }
    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(mislabeled))
        _patch(saved, cr, "resolve_connection", lambda sid: {})
        _patch(saved, aws_session, "boto3_client_for", lambda *a, **k: _FakeSWClient())
        body = DiscoverRequest(system_id="example-sitewise", connection_type="SiteWise (Direct)")
        result = asyncio.run(onboard.onboard_discover(body))
        # Routed to the SiteWise (Direct) branch — NOT the PostgreSQL validate_identifier crash.
        assert "error" not in result, result
        assert result.get("connection_type") == "SiteWise (Direct)", result
    finally:
        _restore(saved)


# --- Athena discover: assumes cross-account role (regression) -----------------

def _athena_entry():
    return {
        "id": "athena-x",
        "name": "Athena X",
        "connection_type": "Athena",
        "schema": "gluedb",
        # NON-secret snapshot only — NO role_arn/external_id, exactly like the live
        # catalog (the STS creds live in the secret, not the catalog snapshot).
        "connection_config": {"connection_type": "Athena", "workgroup": "wg",
                              "database": "gluedb", "catalog": "AwsDataCatalog",
                              "region": "us-east-1"},
    }


def test_athena_discover_assumes_cross_account_role():
    """Athena discover MUST merge the cross-account STS role_arn/external_id
    (resolved via connection_resolver) into the config used for its
    information_schema queries, so introspection assumes the SAME role query-time
    uses. Without it the boto3 client falls back to AMBIENT creds and is denied on
    the cross-account workgroup (AccessDeniedException) — the live onboarding stall.
    Regression for the discover-time analogue of the resolve-evidence fix."""
    import agents.athena_tools as athena_tools
    captured = {"cfgs": []}

    def _fake_run_athena_query(sql, connection_config, **k):
        captured["cfgs"].append(dict(connection_config))
        if "information_schema.tables" in sql:
            return {"columns": ["table_name"], "rows": [["orders"]]}
        if "information_schema.columns" in sql:
            return {"columns": ["column_name", "data_type"], "rows": [["id", "bigint"]]}
        return {"columns": ["id"], "rows": [[1]]}  # sample-values query

    resolved = {"workgroup": "wg", "database": "gluedb", "catalog": "AwsDataCatalog",
                "region": "us-east-1",
                "role_arn": "arn:aws:iam::111122223333:role/CrossAcct",
                "external_id": "ext-abc"}
    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(_athena_entry()))
        _patch(saved, cr, "resolve_connection", lambda sid: dict(resolved))
        _patch(saved, athena_tools, "run_athena_query", _fake_run_athena_query)
        body = DiscoverRequest(
            system_id="athena-x", connection_type="Athena",
            connection_config={"connection_type": "Athena", "workgroup": "wg",
                               "database": "gluedb", "catalog": "AwsDataCatalog",
                               "region": "us-east-1"})
        result = asyncio.run(onboard.onboard_discover(body))
        assert "error" not in result, result
        assert captured["cfgs"], "run_athena_query was never called"
        # EVERY Athena query discover issues must carry the cross-account role.
        for cfg in captured["cfgs"]:
            assert cfg.get("role_arn") == resolved["role_arn"], cfg
            assert cfg.get("external_id") == resolved["external_id"], cfg
    finally:
        _restore(saved)


def test_athena_discover_resolves_role_from_agent_config_secret():
    """Pre-provision fallback: a freshly-REGISTERED Athena system has no graph node,
    so resolve_connection yields no creds — the cross-account role_arn/external_id
    must then come from the deterministic agent-config secret so discover still
    assumes the role (else ambient creds -> AccessDenied). Regression for the live
    re-register-then-onboard stall."""
    import agents.athena_tools as athena_tools
    import agents.system_agent as system_agent
    captured = {"cfgs": []}

    def _fake_run_athena_query(sql, connection_config, **k):
        captured["cfgs"].append(dict(connection_config))
        if "information_schema.tables" in sql:
            return {"columns": ["table_name"], "rows": [["orders"]]}
        if "information_schema.columns" in sql:
            return {"columns": ["column_name", "data_type"], "rows": [["id", "bigint"]]}
        return {"columns": ["id"], "rows": [[1]]}

    secret = {"role_arn": "arn:aws:iam::111122223333:role/CrossAcct",
              "external_id": "ext-sec", "region": "us-east-1"}
    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(_athena_entry()))
        _patch(saved, cr, "resolve_connection", lambda sid: {})  # no node (pre-provision)
        _patch(saved, system_agent, "_fetch_credentials", lambda sid: dict(secret))
        _patch(saved, athena_tools, "run_athena_query", _fake_run_athena_query)
        body = DiscoverRequest(
            system_id="athena-x", connection_type="Athena",
            connection_config={"connection_type": "Athena", "workgroup": "wg",
                               "database": "gluedb", "catalog": "AwsDataCatalog",
                               "region": "us-east-1"})
        result = asyncio.run(onboard.onboard_discover(body))
        assert "error" not in result, result
        assert captured["cfgs"], "run_athena_query was never called"
        for cfg in captured["cfgs"]:
            assert cfg.get("role_arn") == secret["role_arn"], cfg
            assert cfg.get("external_id") == secret["external_id"], cfg
    finally:
        _restore(saved)


_TESTS = [
    test_athena_discover_assumes_cross_account_role,
    test_athena_discover_resolves_role_from_agent_config_secret,
    test_pg_discover_prefers_body_connection_string,
    test_pg_discover_falls_back_to_resolve_not_catalog,
    test_rest_discover_sources_config_from_resolve_not_catalog,
    test_rest_discover_prefers_body_for_effective_conn_str,
    test_mcp_discover_sources_config_from_resolve_not_catalog,
    test_mcp_discover_live_env_wins_over_resolve,
    test_discover_routes_on_body_type_despite_mislabeled_catalog,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard discover connection-source tests passed")


# ── masked-DSN guard (the wizard 500, 2026-07-31) ───────────────────────────
#
# The catalog GET masks inline credentials to "://***@" before serving entries;
# the wizard round-tripped that DISPLAY value into discover and the driver 500'd
# on the literal "***". The DiscoverRequest model normalizes a masked string to
# ABSENT so the normal resolution (node connection → seed bootstrap) runs.

def test_masked_connection_string_reads_as_absent():
    masked = "postgresql://***@some-host:5432/db?options=-csearch_path%3Dcmms"
    assert DiscoverRequest(connection_string=masked).connection_string == ""
    assert DiscoverRequest().connection_string == ""


def test_real_connection_string_passes_through():
    real = "postgresql://user:pw@host:5432/db"
    assert DiscoverRequest(connection_string=real).connection_string == real
    # A literal *** elsewhere (e.g. in a database name) is not the mask marker.
    odd = "postgresql://user:pw@host:5432/db_***_x"
    assert DiscoverRequest(connection_string=odd).connection_string == odd
