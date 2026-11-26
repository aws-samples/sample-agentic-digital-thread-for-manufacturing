# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.connection_resolver`` with all I/O mocked.

Exercises the SSRF guard, canonical masking, the TTL secret cache, fail-soft
resolution, the probe dispatcher, and secret-free audit logging — without
touching real AWS, Postgres, HTTP, or Neptune.

Runnable two ways (matches ``test_llm_settings_save.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_connection_resolver.py
  * standalone:  python agentic_digital_thread/tests/unit/test_connection_resolver.py

(``.venv-local`` doesn't ship pytest; the standalone runner works there.)
"""
import ipaddress
from contextlib import contextmanager
import json
import logging
import os
import socket
import sys
import types
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.connection_security as connection_security  # noqa: E402

# Cross-account role ARN used by the round-trip fixtures below. Defaults to an
# AWS-reserved example account ID (safe to ship); override with a real ARN via
# ADT_TEST_ROLE_ARN when running these as a local integration/smoke test.
XACCT_ROLE_ARN = os.environ.get(
    "ADT_TEST_ROLE_ARN", "arn:aws:iam::444455556666:role/adt-readonly"
)


# --------------------------------------------------------------------------- #
# Test helpers — fake DNS resolution (NO real network)
# --------------------------------------------------------------------------- #

def _fake_getaddrinfo(mapping: dict):
    """Build a ``socket.getaddrinfo`` replacement.

    Literal IPs resolve to themselves (numeric, no network). Hostnames resolve
    via ``mapping`` (host -> ip); an unmapped hostname raises ``socket.gaierror``
    to simulate an unresolvable host.
    """
    def fake(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            if host not in mapping:
                raise socket.gaierror(f"name resolution failed: {host}")
            ip = mapping[host]
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port or 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
    return fake


_DOCUMENTATION_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))


@contextmanager
def _patch_dns(mapping: dict | None = None):
    """Patch DNS and classify documentation addresses as global test doubles."""
    production_classifier = connection_security._is_denied_address

    def classify(addr):
        if any(addr in network for network in _DOCUMENTATION_NETWORKS):
            return False
        return production_classifier(addr)

    with patch("socket.getaddrinfo", _fake_getaddrinfo(mapping or {})), \
            patch.object(connection_security, "_is_denied_address", classify):
        yield


# --------------------------------------------------------------------------- #
# validate_connection_target — scheme allowlist + SSRF
# --------------------------------------------------------------------------- #

def test_scheme_allowlist_rejects_dangerous_schemes():
    # Scheme is checked FIRST, before any DNS — so no resolution is needed here.
    for bad in ("http://example.com", "file:///etc/passwd", "gopher://evil/x"):
        try:
            cr.validate_connection_target(bad, connection_type="REST API")
            raise AssertionError(f"expected rejection for {bad}")
        except cr.ConnectionTargetError:
            pass
    # Allowed schemes must NOT raise. Documentation addresses represent public
    # targets; _patch_dns mocks the classifier boundary because ipaddress marks
    # documentation ranges non-global. No network occurs.
    with _patch_dns({"api.example.com": "192.0.2.2", "db": "10.0.0.7",
                     "us-east-1.amazonaws.com": "192.0.2.3"}):
        cr.validate_connection_target("https://api.example.com/spec", connection_type="REST API")
        cr.validate_connection_target("opc.tcp://10.0.0.9:4840", connection_type="OPC UA")
        cr.validate_connection_target("postgresql://u:p@db:5432/x", connection_type="PostgreSQL")
        cr.validate_connection_target("iotsitewise://us-east-1.amazonaws.com",
                                      connection_type="SiteWise")


def test_metadata_and_link_local_rejected():
    # Literal IPs resolve to themselves; metadata hostname is a literal fast-reject.
    with _patch_dns():
        for bad in (
            "https://169.254.169.254/latest/meta-data/",  # IMDS via URL
            "169.254.169.254",                            # IMDS bare host
            "https://169.254.10.20/health",               # link-local range
            "metadata.google.internal",                   # GCP metadata (literal host)
        ):
            try:
                cr.validate_connection_target(bad, connection_type="REST API")
                raise AssertionError(f"expected rejection for {bad}")
            except cr.ConnectionTargetError:
                pass


def test_dns_rebinding_to_metadata_rejected():
    """A hostname that RESOLVES to the IMDS / link-local address is rejected."""
    with _patch_dns({"evil.example.com": "169.254.169.254"}):
        try:
            cr.validate_connection_target("https://evil.example.com/health",
                                          connection_type="REST API")
            raise AssertionError("expected rejection — host resolves to IMDS")
        except cr.ConnectionTargetError:
            pass
    # IPv6 IMDS form via DNS.
    with _patch_dns({"evil6.example.com": "fd00:ec2::254"}):
        try:
            cr.validate_connection_target("https://evil6.example.com/health",
                                          connection_type="REST API")
            raise AssertionError("expected rejection — host resolves to IPv6 IMDS")
        except cr.ConnectionTargetError:
            pass


def test_hostname_resolving_to_rfc1918_allowed():
    """A hostname resolving to an RFC1918 address is allowed (in-VPC target)."""
    with _patch_dns({"db.example.com": "10.0.0.5"}):
        cr.validate_connection_target("postgresql://u:p@db.example.com:5432/x",
                                      connection_type="PostgreSQL")


def test_unresolvable_host_rejected():
    with _patch_dns({}):  # nothing resolves
        try:
            cr.validate_connection_target("https://does-not-resolve.example.com/health",
                                          connection_type="REST API")
            raise AssertionError("expected rejection for unresolvable host")
        except cr.ConnectionTargetError as exc:
            assert "did not resolve" in str(exc), str(exc)


def test_integer_ipv4_metadata_form_rejected():
    """Alternate IPv4 encodings (integer form) that the OS normalizes to the IMDS
    address are rejected. We simulate the OS normalization via getaddrinfo."""
    # 2852039166 == 0xA9FEA9FE == 169.254.169.254
    with _patch_dns({"2852039166": "169.254.169.254"}):
        try:
            cr.validate_connection_target("https://2852039166/", connection_type="REST API")
            raise AssertionError("expected rejection for integer IPv4 metadata form")
        except cr.ConnectionTargetError:
            pass


def test_ipv4_mapped_ipv6_metadata_rejected():
    """IPv4-mapped IPv6 (``::ffff:169.254.169.254``) must NOT bypass the IPv4
    metadata/link-local deny check (the HIGH finding)."""
    with _patch_dns():
        for bad in (
            "https://[::ffff:169.254.169.254]/",   # URL form, bracketed literal
            "::ffff:169.254.169.254",              # bare mapped literal
            "[::ffff:169.254.169.254]:5432",       # bracketed host:port
        ):
            try:
                cr.validate_connection_target(bad, connection_type="REST API")
                raise AssertionError(f"expected rejection for IPv4-mapped {bad}")
            except cr.ConnectionTargetError:
                pass


def test_ipv4_compat_6to4_teredo_metadata_rejected():
    """Other embedded-IPv4 IPv6 encodings that smuggle the IMDS/link-local IPv4
    are rejected: IPv4-compatible, 6to4 (2002::/16), Teredo (2001::/32)."""
    # Teredo embeds the client IPv4 obfuscated (XOR 0xFFFFFFFF).
    _client = int(ipaddress.ip_address("169.254.169.254")) ^ 0xFFFFFFFF
    teredo = f"2001:0000:4136:e378:8000:63bf:{_client >> 16:04x}:{_client & 0xFFFF:04x}"
    with _patch_dns():
        for bad in (
            "::169.254.169.254",        # IPv4-compatible (deprecated)
            "2002:a9fe:a9fe::",         # 6to4 embedding 169.254.169.254
            teredo,                     # Teredo embedding 169.254.169.254 client
        ):
            try:
                cr.validate_connection_target(bad, connection_type="REST API")
                raise AssertionError(f"expected rejection for embedded-IPv4 form {bad}")
            except cr.ConnectionTargetError:
                pass


def test_global_ipv6_allowed():
    """An IPv6 target classified as global by the policy boundary is allowed."""
    with _patch_dns({"cf.example.com": "2001:db8::1"}):
        cr.validate_connection_target("https://cf.example.com/health",
                                      connection_type="REST API")
    # Bare global IPv6 literal too.
    with _patch_dns():
        cr.validate_connection_target("https://[2001:db8::1]/health",
                                      connection_type="REST API")


def test_validate_returns_pinned_ip():
    """validate_connection_target / resolve_and_validate return the first safe
    resolved IP, so the caller can pin it to the socket (DNS-rebinding defense)."""
    with _patch_dns({"db.example.com": "10.0.0.5"}):
        pinned = cr.validate_connection_target("postgresql://u:p@db.example.com:5432/x",
                                               connection_type="PostgreSQL")
        assert pinned == "10.0.0.5", pinned
        assert cr.resolve_and_validate("postgresql://u:p@db.example.com:5432/x",
                                       connection_type="PostgreSQL") == "10.0.0.5"
    # Literal IP returns itself.
    with _patch_dns():
        assert cr.validate_connection_target("https://192.0.2.2/health",
                                             connection_type="REST API") == "192.0.2.2"


def test_probe_postgres_pins_validated_ip_via_hostaddr():
    """_probe_postgres connects with ``hostaddr=<validated IP>`` while keeping the
    original DSN host — pinning the socket to the validated address closes the
    DNS-rebinding TOCTOU window for PG."""
    captured = {}

    class _FakeCur:
        def execute(self, q):
            assert q == "SELECT 1"

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    def _connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _FakeConn()

    conn_str = "postgresql://u:p@db.example.com:5432/db"
    with _patch_dns({"db.example.com": "10.0.0.7"}), \
            patch("agents.pgdriver.connect", _connect):
        res = cr.probe_connection("erp", {"connection_type": "PostgreSQL",
                                          "connection_string": conn_str})
    assert res["status"] == "pass", res
    # Original hostname-bearing DSN preserved for TLS/ident...
    assert captured["dsn"] == conn_str
    # ...while the socket is pinned to the validated IP.
    assert captured["kwargs"].get("hostaddr") == "10.0.0.7", captured


def test_rfc1918_private_targets_allowed():
    # In-VPC private targets are expected and must NOT be blocked (literal IPs).
    with _patch_dns():
        cr.validate_connection_target("postgresql://u:p@10.0.0.5:5432/db", connection_type="PostgreSQL")
        cr.validate_connection_target("10.0.0.5", connection_type="PostgreSQL")
        cr.validate_connection_target("10.0.0.5:5432", connection_type="PostgreSQL")
        cr.validate_connection_target("https://172.16.5.4:8443/health", connection_type="REST API")
        cr.validate_connection_target("192.168.1.10", connection_type="PostgreSQL")
        # Loopback stays allowed for local dev probes.
        cr.validate_connection_target("https://127.0.0.1:8080/health", connection_type="REST API")


def test_empty_target_rejected():
    for bad in ("", "   ", None):
        try:
            cr.validate_connection_target(bad, connection_type="PostgreSQL")
            raise AssertionError("expected rejection for empty target")
        except cr.ConnectionTargetError:
            pass


# --------------------------------------------------------------------------- #
# mask_connection
# --------------------------------------------------------------------------- #

def test_mask_connection_redacts_and_does_not_mutate():
    cfg = {
        "url": "postgresql://admin:s3cr3t@db.example.com:5432/x",
        "connection_string": "https://user:pw-xyz@host/mcp",
        "token": "tok-abc",
        "password": "pw-xyz",
        "api_key": "ak-123",
        "auth_scheme": "bearer",
        "nested": {"secret": "nested-sek", "host": "h.example.com"},
    }
    masked = cr.mask_connection(cfg)
    blob = json.dumps(masked)
    for leaked in ("s3cr3t", "tok-abc", "pw-xyz", "ak-123", "nested-sek"):
        assert leaked not in blob, f"secret {leaked} leaked into masked output"
    assert masked["url"] == "postgresql://***@db.example.com:5432/x"
    assert masked["connection_string"] == "https://***@host/mcp"
    assert masked["token"] == "***"
    assert masked["password"] == "***"
    assert masked["api_key"] == "***"
    assert masked["auth_scheme"] == "bearer"        # non-secret preserved
    assert masked["nested"]["secret"] == "***"      # recursion
    assert masked["nested"]["host"] == "h.example.com"
    # Input dict must be untouched (immutability).
    assert cfg["token"] == "tok-abc"
    assert cfg["url"] == "postgresql://admin:s3cr3t@db.example.com:5432/x"
    assert cfg["nested"]["secret"] == "nested-sek"


def test_mask_connection_redacts_query_string_secrets():
    """Secret-bearing query params in url/connection_string are redacted too."""
    cfg = {
        "connection_string": "https://api.example.com/v1/openapi.json?api_key=AK-LEAK&page=1",
        "url": "https://svc.example.com/mcp?token=TOK-LEAK&access_token=AT-LEAK&debug=true",
    }
    masked = cr.mask_connection(cfg)
    blob = json.dumps(masked)
    for leaked in ("AK-LEAK", "TOK-LEAK", "AT-LEAK"):
        assert leaked not in blob, f"query secret {leaked} leaked: {blob}"
    # Non-secret params are preserved.
    assert "page=1" in masked["connection_string"]
    assert "debug=true" in masked["url"]
    # Original input is not mutated.
    assert "AK-LEAK" in cfg["connection_string"]


# --------------------------------------------------------------------------- #
# _fetch_secret — TTL cache
# --------------------------------------------------------------------------- #

class _FakeSM:
    def __init__(self):
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": json.dumps({"token": f"sek-{self.calls}"})}


def test_secret_cache_ttl_then_refetch():
    cr._secret_cache.clear()
    clock = {"t": 1000.0}
    fake = _FakeSM()
    orig_mono, orig_client = cr._monotonic, cr._secrets_client
    cr._monotonic = lambda: clock["t"]
    cr._secrets_client = lambda: fake
    try:
        v1 = cr._fetch_secret("arn:secret:1")
        assert fake.calls == 1 and v1["token"] == "sek-1"
        # Within TTL → served from cache, no new fetch.
        v2 = cr._fetch_secret("arn:secret:1")
        assert fake.calls == 1 and v2["token"] == "sek-1"
        # Advance past TTL → refetch.
        clock["t"] += cr._TTL + 1
        v3 = cr._fetch_secret("arn:secret:1")
        assert fake.calls == 2 and v3["token"] == "sek-2"
        # invalidate forces a refetch even within TTL.
        cr.invalidate_secret("arn:secret:1")
        cr._fetch_secret("arn:secret:1")
        assert fake.calls == 3
    finally:
        cr._monotonic, cr._secrets_client = orig_mono, orig_client
        cr._secret_cache.clear()


def test_parse_secret_json_and_bare_string():
    assert cr._parse_secret(json.dumps({"username": "u", "password": "p"})) == {"username": "u", "password": "p"}
    assert cr._parse_secret("raw-bearer-token") == {"token": "raw-bearer-token"}
    assert cr._parse_secret("") == {}


# --------------------------------------------------------------------------- #
# resolve_connection — NODE-ONLY (no catalog fallback)
# --------------------------------------------------------------------------- #

def _raise_if_catalog_read(_sid):
    raise AssertionError("catalog must not be read — resolve_connection is node-only")


def test_resolve_connection_reads_from_registered_node():
    """A registered node is the SOLE source: non-secret config + secret_arn +
    connection_type + connection_string come from the node, the secret is merged,
    and the catalog is NEVER read."""
    node = {
        "connection_config": json.dumps({"host": "10.0.0.5", "port": 5432,
                                         "dbname": "erp", "schema": "erp_data"}),
        "secret_arn": "arn:secret:erp",
        "connection_type": "PostgreSQL",
        "connection_string": "postgresql://10.0.0.5:5432/erp",
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = lambda arn: {"username": "u", "password": "pw"}
    try:
        resolved = cr.resolve_connection("erp")
        assert resolved.get("_unconfigured") is not True
        assert resolved["host"] == "10.0.0.5"
        assert resolved["dbname"] == "erp"
        assert resolved["connection_type"] == "PostgreSQL"
        assert resolved["connection_string"] == "postgresql://10.0.0.5:5432/erp"
        assert resolved["secret_arn"] == "arn:secret:erp"
        assert resolved["password"] == "pw"          # secret merged in
        assert resolved["_secret_unresolved"] is False
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch


def test_resolve_connection_unregistered_node_is_unconfigured_no_catalog_read():
    """An unregistered system (no node row) resolves to the _unconfigured result —
    NEVER a catalog default, and the catalog is not read at all."""
    orig_node, orig_cat = cr._fetch_node_config, cr._catalog_get
    cr._fetch_node_config = lambda sid: None            # unregistered
    cr._catalog_get = _raise_if_catalog_read
    try:
        resolved = cr.resolve_connection("ghost")
        assert resolved["_unconfigured"] is True
        assert resolved["connection_type"] is None
        assert resolved["connection_string"] == ""
        assert resolved["secret_arn"] == ""
        assert resolved["_secret_unresolved"] is False
    finally:
        cr._fetch_node_config, cr._catalog_get = orig_node, orig_cat


def test_resolve_connection_node_without_config_is_unconfigured():
    """A node row that exists but carries NO connection_config is treated as
    unregistered → _unconfigured, no catalog read."""
    orig_node, orig_cat = cr._fetch_node_config, cr._catalog_get
    cr._fetch_node_config = lambda sid: {"connection_config": None, "secret_arn": ""}
    cr._catalog_get = _raise_if_catalog_read
    try:
        resolved = cr.resolve_connection("ghost")
        assert resolved["_unconfigured"] is True
        assert resolved["connection_string"] == ""
    finally:
        cr._fetch_node_config, cr._catalog_get = orig_node, orig_cat


def test_resolve_connection_graph_read_failure_is_unconfigured_no_raise():
    """A graph read failure yields the _unconfigured result (fail-soft) and NEVER
    raises on the read path — and the catalog is not consulted."""
    orig_node, orig_cat = cr._fetch_node_config, cr._catalog_get

    def _raise(_sid):
        raise RuntimeError("Neptune unavailable (local mode)")

    cr._fetch_node_config = _raise
    cr._catalog_get = _raise_if_catalog_read
    try:
        resolved = cr.resolve_connection("erp")        # must NOT raise
        assert resolved["_unconfigured"] is True
        assert resolved["connection_type"] is None
        assert resolved["connection_string"] == ""
        assert resolved["secret_arn"] == ""
    finally:
        cr._fetch_node_config, cr._catalog_get = orig_node, orig_cat


def test_resolve_connection_merges_secret():
    node = {
        "connection_config": json.dumps({"url": "https://highbyte.example.com/mcp",
                                         "auth_scheme": "bearer_literal"}),
        "secret_arn": "arn:secret:hb",
        "connection_type": "MCP Server",
        "connection_string": "https://highbyte.example.com/mcp",
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = lambda arn: {"token": "live-token"}
    try:
        resolved = cr.resolve_connection("highbyte")
        assert resolved["token"] == "live-token"      # secret merged in
        assert resolved["auth_scheme"] == "bearer_literal"
        assert resolved["secret_arn"] == "arn:secret:hb"
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch


# --------------------------------------------------------------------------- #
# probe_connection — SSRF guard, pass path, secret-free detail
# --------------------------------------------------------------------------- #

def test_probe_rejects_metadata_target():
    with _patch_dns():
        res = cr.probe_connection("x", {"connection_type": "REST API",
                                        "connection_string": "https://169.254.169.254/"})
    assert res["status"] == "fail"
    assert res["reason"] == "blocked_target", res
    assert res["connection_type"] == "REST API"
    # Coarse detail only — no raw IP leaks to the caller.
    assert "169.254" not in res["detail"], res["detail"]


def test_probe_postgres_pass_with_mock():
    class _FakeCur:
        def execute(self, q):
            assert q == "SELECT 1"

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    with _patch_dns(), patch("agents.pgdriver.connect", lambda cs, **kw: _FakeConn()):
        res = cr.probe_connection("erp", {"connection_type": "PostgreSQL",
                                          "connection_string": "postgresql://u:p@10.0.0.5:5432/db"})
    assert res["status"] == "pass", res
    assert res["reason"] == "ok", res
    assert res["detail"] == "SELECT 1 ok"
    assert isinstance(res["latency_ms"], int) and res["latency_ms"] >= 0


def test_probe_postgres_provisioned_node_without_dsn():
    """The card Test button probes the System node as provision writes it: host/
    port/dbname/schema and a pointer to the shared DB secret, no DSN (req6c). The
    probe connects with the same params the agents use (pg_conn_params), with
    the validated IP pinned, instead of failing with 'no connection string'."""
    from agents.onboard import _sql_non_secret
    node_cfg = _sql_non_secret(
        "postgresql://db.example.com:5432/erp_simulation", "erp_data")
    node = {"connection_config": json.dumps(node_cfg), "connection_type": "PostgreSQL",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:db-AbCdEf"}
    secret = {"username": "app", "password": "pw", "host": "db.example.com",
              "port": 5432, "dbname": "erp_simulation"}
    captured = {}

    class _FakeConn:
        def cursor(self):
            return types.SimpleNamespace(execute=lambda q: None,
                                         fetchone=lambda: (1,), close=lambda: None)

        def close(self):
            pass

    def _connect(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return _FakeConn()

    with _patch_dns({"db.example.com": "10.0.0.5"}), \
            patch.object(cr, "_fetch_node_config", lambda sid: node), \
            patch.object(cr, "_fetch_secret", lambda arn: dict(secret)), \
            patch("agents.pgdriver.connect", _connect):
        res = cr.probe_connection("erp", cr.resolve_connection("erp"))
    assert res["status"] == "pass", res
    kw = captured["kwargs"]
    assert captured["args"] == (), captured
    assert (kw["host"], kw["user"], kw["password"], kw["dbname"]) == (
        "db.example.com", "app", "pw", "erp_simulation"), kw
    assert kw["hostaddr"] == "10.0.0.5", kw
    assert kw["options"] == "-csearch_path=erp_data", kw


def test_probe_postgres_no_dsn_user_registration_never_env_filled():
    """A user's PostgreSQL registration with no password of its own is never
    probed with the deployment's env credentials, even on the env host."""
    from agents.catalog import catalog
    entry = {"id": "u1", "connection_type": "PostgreSQL",
             cr.CONNECTION_ORIGIN_KEY: cr.ORIGIN_USER}
    env = {"POSTGRES_HOST": "db.example.com", "POSTGRES_DB": "erp_simulation",
           "POSTGRES_PASSWORD": "deploy-pw"}
    calls = []
    with _patch_dns({"db.example.com": "10.0.0.5"}), patch.dict(os.environ, env), \
            patch.object(catalog, "get", lambda sid: entry if sid == "u1" else None), \
            patch("agents.pgdriver.connect", lambda *a, **kw: calls.append(kw)):
        res = cr.probe_connection("u1", {"system_id": "u1", "connection_type": "PostgreSQL",
                                         "connection_string": "", "host": "db.example.com",
                                         "dbname": "erp_simulation", "secret_arn": ""})
    assert res["status"] == "fail", res
    assert res["reason"] == "credential_unresolved", res
    assert res["detail"] == cr._REASON_DETAIL["credential_unresolved"], res
    assert calls == [], calls


def test_probe_postgres_pre_node_demo_entry_routes_by_entry_schema():
    """A registered demo system not yet provisioned has no node. The Test route
    probes it from its catalog entry, whose schema sits at the top level: an
    ERP-family schema reaches the ERP database, not the seed database."""
    from agents.admin_routes import _with_pre_node_catalog
    from agents.catalog import catalog
    entry = {"id": "erp", "connection_type": "PostgreSQL", "schema": "erp_data"}
    env = {"POSTGRES_HOST": "seed.example.com", "POSTGRES_DB": "adt_demo",
           "POSTGRES_PASSWORD": "seed-pw", "ERP_POSTGRES_HOST": "erp.example.com",
           "ERP_POSTGRES_DB": "erp_simulation", "ERP_POSTGRES_USER": "erp_user",
           "ERP_POSTGRES_PASSWORD": "erp-pw"}
    captured = {}

    class _FakeConn:
        def cursor(self):
            return types.SimpleNamespace(execute=lambda q: None,
                                         fetchone=lambda: (1,), close=lambda: None)

        def close(self):
            pass

    def _connect(*args, **kwargs):
        captured.update(kwargs)
        return _FakeConn()

    dns = {"erp.example.com": "10.0.0.8", "seed.example.com": "10.0.0.9"}
    with _patch_dns(dns), patch.dict(os.environ, env), \
            patch.object(cr, "_fetch_node_config", lambda sid: None), \
            patch.object(catalog, "get", lambda sid: entry if sid == "erp" else None), \
            patch("agents.pgdriver.connect", _connect):
        res = cr.probe_connection("erp", _with_pre_node_catalog("erp", cr.resolve_connection("erp")))
    assert res["status"] == "pass", res
    assert (captured["host"], captured["dbname"], captured["user"]) == (
        "erp.example.com", "erp_simulation", "erp_user"), captured
    assert captured["hostaddr"] == "10.0.0.8", captured
    assert captured["options"] == "-csearch_path=erp_data", captured


def test_probe_failure_detail_is_coarse_no_host_ip_port_or_secret():
    """Failure detail returned to the caller is a coarse enum string — no secret,
    no raw host/IP/port (blind-SSRF / port-scan oracle is closed)."""
    def _boom(cs, **kw):
        raise Exception("connection refused: could not connect to server at "
                        "10.0.0.5 port 5432 (password=supersecret)")

    with _patch_dns(), patch("agents.pgdriver.connect", _boom):
        res = cr.probe_connection("erp", {
            "connection_type": "PostgreSQL",
            "connection_string": "postgresql://u:supersecret@10.0.0.5:5432/db",
            "password": "supersecret",
        })
    assert res["status"] == "fail"
    assert res["reason"] == "connection_refused", res
    for forbidden in ("supersecret", "10.0.0.5", "5432"):
        assert forbidden not in res["detail"], res["detail"]


def test_sitewise_i3x_routes_to_rest_not_boto3():
    """SiteWise (i3X) is an HTTP/i3X REST API — it must use the REST probe and
    must NOT create a boto3 SiteWise client."""
    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False, "REST probe must not follow redirects"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, extensions=None):
            # IP pinning: the socket target is the VALIDATED IP, while the Host
            # header + TLS SNI still carry the original hostname.
            assert url == "https://192.0.2.2/v1/health", url
            assert (headers or {}).get("Host") == "api.i3x.dev", headers
            assert (extensions or {}).get("sni_hostname") == "api.i3x.dev", extensions
            return _FakeResp()

    fake_httpx = types.SimpleNamespace(Client=_FakeClient)

    boto_calls = []

    def _fake_client(*a, **k):
        boto_calls.append((a, k))
        raise AssertionError("boto3 SiteWise client must not be created for i3X")

    fake_boto3 = types.SimpleNamespace(client=_fake_client)

    resolved = {
        "connection_type": "SiteWise (i3X)",
        "connection_string": "https://api.i3x.dev/v1/openapi.json",
        "base_url": "https://api.i3x.dev/v1",
        "auth_scheme": "bearer",
    }
    with _patch_dns({"api.i3x.dev": "192.0.2.2"}), \
            patch.dict(sys.modules, {"httpx": fake_httpx, "boto3": fake_boto3}):
        res = cr.probe_connection("sitewise", resolved)
    assert res["status"] == "pass", res
    assert res["connection_type"] == "SiteWise (i3X)"
    assert boto_calls == [], "boto3 client must not have been called"


def test_sitewise_direct_routes_to_boto3():
    """SiteWise (Direct) / iotsitewise:// uses the boto3 list_asset_models probe."""
    boto_calls = []

    class _FakeSW:
        def list_asset_models(self, **k):
            return {"assetModelSummaries": [{"id": "m1"}]}

    def _fake_client(name, **k):
        boto_calls.append(name)
        return _FakeSW()

    # Probes now build clients via aws_session.session_for (cross-account-aware,
    # 2026-07-26 fix) — patch it directly (aws_session holds a module-level
    # boto3 reference, so a sys.modules swap cannot reach it).
    import agents.aws_session as aws_session
    fake_session = types.SimpleNamespace(
        client=lambda name, region_name=None: _fake_client(name))
    resolved = {
        "connection_type": "SiteWise (Direct)",
        "connection_string": "iotsitewise://us-east-1.amazonaws.com",
    }
    with _patch_dns({"us-east-1.amazonaws.com": "192.0.2.3"}), \
            patch.object(aws_session, "session_for",
                         lambda cfg, default_region=None, service="adt": fake_session):
        res = cr.probe_connection("sitewise-direct", resolved)
    assert res["status"] == "pass", res
    assert boto_calls == ["iotsitewise"], boto_calls


def test_credential_unresolved_reason():
    """A CONFIGURED secret that fails to resolve surfaces 'credential_unresolved',
    not a misleading auth failure — and the probe never runs (no boto3/httpx)."""
    def _no_probe(*a, **k):
        raise AssertionError("probe must not run when credential is unresolved")

    resolved = {
        "connection_type": "MCP Server",
        "connection_string": "https://hb.example.com/mcp",
        "secret_arn": "arn:secret:hb",
        "_secret_unresolved": True,
    }
    orig = dict(cr._PROBES)
    cr._PROBES.update({k: _no_probe for k in cr._PROBES})
    try:
        res = cr.probe_connection("highbyte", resolved)
    finally:
        cr._PROBES.clear()
        cr._PROBES.update(orig)
    assert res["status"] == "fail", res
    assert res["reason"] == "credential_unresolved", res
    assert "arn" not in res["detail"].lower()


def test_resolve_connection_marks_unresolved_secret():
    """When a configured secret_arn cannot be fetched, resolve flags it (fail-soft)."""
    node = {
        "connection_config": json.dumps({"url": "https://hb.example.com/mcp"}),
        "secret_arn": "arn:secret:hb",
        "connection_type": "MCP Server",
        "connection_string": "https://hb.example.com/mcp",
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret

    def _boom(_arn):
        raise RuntimeError("AccessDenied reading secret")

    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = _boom
    try:
        resolved = cr.resolve_connection("highbyte")
        assert resolved["_secret_unresolved"] is True
        assert "token" not in resolved  # secret never merged
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch


# --------------------------------------------------------------------------- #
# write_connection — Secrets Manager write + cache invalidation + catalog persist
# --------------------------------------------------------------------------- #

class _FakeSMWriter:
    """Records create/put calls and exposes the ResourceExistsException shape."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self, exists: bool = False):
        self._exists = exists
        self.created = []
        self.put = []

        class _Exc:
            ResourceExistsException = _FakeSMWriter.ResourceExistsException

        self.exceptions = _Exc()

    def create_secret(self, Name, SecretString, **kw):
        if self._exists:
            raise self.ResourceExistsException("already exists")
        self.created.append((Name, SecretString))
        return {"ARN": f"arn:aws:secretsmanager:::secret:{Name}"}

    def put_secret_value(self, SecretId, SecretString, **kw):
        self.put.append((SecretId, SecretString))
        return {"ARN": f"arn:aws:secretsmanager:::secret:{SecretId}"}


def test_write_connection_writes_secret_invalidates_and_persists():
    """write_connection writes to conn/<id>, returns the ARN, refreshes the TTL
    cache, and persists the non-secret config + ARN ONLY onto the System node —
    the catalog is never read or mutated (the node is the sole source)."""
    cr._secret_cache.clear()
    fake_sm = _FakeSMWriter(exists=False)
    invalidated = []
    captured = {}
    orig_client, orig_cat, orig_inval = cr._secrets_client, cr._catalog_get, cr.invalidate_secret
    orig_nw = cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read   # write_connection must NOT read the catalog
    cr.invalidate_secret = lambda arn: invalidated.append(arn)

    def _capture(cypher, params):
        captured["cypher"] = cypher
        captured["params"] = params
        return True

    cr._neptune_write = _capture
    try:
        with _patch_dns({"hb.example.com": "10.0.0.9"}):
            arn = cr.write_connection(
                "highbyte",
                non_secret={"url": "https://hb.example.com/mcp", "connection_type": "MCP Server"},
                secret={"token": "live-tok"},
            )
        # Wrote to conn/<id> and returned the ARN.
        assert fake_sm.created and fake_sm.created[0][0] == "adt/conn/highbyte"
        assert arn == "arn:aws:secretsmanager:::secret:adt/conn/highbyte"
        # Secret VALUE was stored (not logged), cache invalidated for the new ARN.
        assert json.loads(fake_sm.created[0][1]) == {"token": "live-tok"}
        assert invalidated == [arn]
        # The node MERGE is the ONLY persist: it carries the non-secret config + ARN.
        assert "MERGE (s:System {id: $sid})" in captured["cypher"]
        params = captured["params"]
        assert params["arn"] == arn
        cfg = json.loads(params["cfg_json"])
        assert cfg["url"] == "https://hb.example.com/mcp"
        # Secret never reaches the node write.
        blob = captured["cypher"] + json.dumps(params)
        assert "live-tok" not in blob and "token" not in blob
    finally:
        cr._secrets_client, cr._catalog_get, cr.invalidate_secret = orig_client, orig_cat, orig_inval
        cr._neptune_write = orig_nw
        cr._secret_cache.clear()


def test_write_connection_put_when_secret_exists():
    """When the secret already exists, write_connection falls back to put_secret_value."""
    entry = {"connection_type": "MCP Server", "connection_config": {}, "secret_arn": ""}
    fake_sm = _FakeSMWriter(exists=True)
    orig_client, orig_cat = cr._secrets_client, cr._catalog_get
    orig_nw = cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: entry
    cr._neptune_write = lambda cypher, params: True
    try:
        with _patch_dns({"hb.example.com": "10.0.0.9"}):
            arn = cr.write_connection("highbyte",
                                      non_secret={"url": "https://hb.example.com/mcp"},
                                      secret={"token": "tok"})
        assert fake_sm.put and fake_sm.put[0][0] == "adt/conn/highbyte"
        assert arn.endswith("adt/conn/highbyte")
    finally:
        cr._secrets_client, cr._catalog_get = orig_client, orig_cat
        cr._neptune_write = orig_nw


def test_write_connection_highbyte_does_not_write_legacy_token():
    """Step 3 cut-over (T5.1): updating system_id='highbyte' with a token writes
    ONLY the canonical conn/highbyte secret — the legacy
    adt/highbyte-mcp-token bare-string secret is NEVER written
    (the dual-write bridge is gone)."""
    cr._secret_cache.clear()
    entry = {"connection_type": "MCP Server", "connection_config": {}, "secret_arn": ""}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat = cr._secrets_client, cr._catalog_get
    orig_nw = cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: entry
    cr._neptune_write = lambda cypher, params: True
    try:
        with _patch_dns({"hb.example.com": "10.0.0.9"}):
            cr.write_connection(
                "highbyte",
                non_secret={"url": "https://hb.example.com/mcp", "connection_type": "MCP Server"},
                secret={"token": "hb-live-tok"},
            )
        names = [name for name, _ in fake_sm.created]
        put_names = [name for name, _ in fake_sm.put]
        # ONLY the canonical conn/* secret was written...
        assert "adt/conn/highbyte" in names, names
        # ...and the legacy bare-string secret was NEVER created OR put.
        assert "adt/highbyte-mcp-token" not in names, names
        assert "adt/highbyte-mcp-token" not in put_names, put_names
        # The conn/* secret stores JSON.
        conn = dict(fake_sm.created)["adt/conn/highbyte"]
        assert json.loads(conn) == {"token": "hb-live-tok"}, conn
    finally:
        cr._secrets_client, cr._catalog_get = orig_client, orig_cat
        cr._neptune_write = orig_nw
        cr._secret_cache.clear()


def test_write_connection_non_highbyte_does_not_write_legacy_token():
    """A non-highbyte system with a token writes ONLY conn/<id> — never the legacy
    highbyte-mcp-token secret."""
    cr._secret_cache.clear()
    entry = {"connection_type": "MCP Server", "connection_config": {}, "secret_arn": ""}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat = cr._secrets_client, cr._catalog_get
    orig_nw = cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: entry
    cr._neptune_write = lambda cypher, params: True
    try:
        with _patch_dns({"mcp.example.com": "10.0.0.9"}):
            cr.write_connection(
                "some_other_mcp",
                non_secret={"url": "https://mcp.example.com/mcp", "connection_type": "MCP Server"},
                secret={"token": "tok"},
            )
        names = [name for name, _ in fake_sm.created]
        assert "adt/conn/some_other_mcp" in names, names
        assert "adt/highbyte-mcp-token" not in names, names
    finally:
        cr._secrets_client, cr._catalog_get = orig_client, orig_cat
        cr._neptune_write = orig_nw
        cr._secret_cache.clear()


def test_write_connection_rejects_bad_scheme_before_writing():
    """A bad scheme is rejected by validate_connection_target BEFORE any write —
    no secret is created/put."""
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat = cr._secrets_client, cr._catalog_get
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: {"connection_config": {}}
    try:
        with _patch_dns({"evil.example.com": "192.0.2.2"}):
            try:
                cr.write_connection("x",
                                    non_secret={"url": "http://evil.example.com/mcp"},
                                    secret={"token": "tok"})
                raise AssertionError("expected ConnectionTargetError for bad scheme")
            except cr.ConnectionTargetError:
                pass
        # Nothing was written.
        assert fake_sm.created == [] and fake_sm.put == []
    finally:
        cr._secrets_client, cr._catalog_get = orig_client, orig_cat


def test_write_connection_rejects_rebinding_target_before_writing():
    """A host resolving to the IMDS address is rejected before any secret write."""
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat = cr._secrets_client, cr._catalog_get
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: {"connection_config": {}}
    try:
        with _patch_dns({"evil.example.com": "169.254.169.254"}):
            try:
                cr.write_connection("x",
                                    non_secret={"url": "https://evil.example.com/mcp"},
                                    secret={"token": "tok"})
                raise AssertionError("expected ConnectionTargetError for rebinding target")
            except cr.ConnectionTargetError:
                pass
        assert fake_sm.created == [] and fake_sm.put == []
    finally:
        cr._secrets_client, cr._catalog_get = orig_client, orig_cat


# --------------------------------------------------------------------------- #
# httpx (REST/MCP) probes — transport-level IP pinning (DNS-rebinding defense)
# --------------------------------------------------------------------------- #

def test_probe_rest_pins_validated_ip_and_preserves_host_and_sni():
    """The REST probe connects to the VALIDATED IP (pinned) while the Host header
    and TLS SNI still carry the original hostname (closes the rebinding TOCTOU)."""
    seen = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, extensions=None):
            seen["url"] = url
            seen["headers"] = headers or {}
            seen["extensions"] = extensions or {}
            return _FakeResp()

    fake_httpx = types.SimpleNamespace(Client=_FakeClient)
    resolved = {"connection_type": "REST API", "base_url": "https://svc.example.com:8443/v1",
                "token": "tok", "auth_scheme": "bearer"}
    with _patch_dns({"svc.example.com": "192.0.2.2"}), \
            patch.dict(sys.modules, {"httpx": fake_httpx}):
        res = cr.probe_connection("rest-sys", resolved)
    assert res["status"] == "pass", res
    # Socket target is the validated IP, original port/path preserved.
    assert seen["url"] == "https://192.0.2.2:8443/v1/health", seen["url"]
    # Host header + TLS SNI preserve the original hostname.
    assert seen["headers"].get("Host") == "svc.example.com:8443", seen["headers"]
    assert seen["extensions"].get("sni_hostname") == "svc.example.com", seen["extensions"]


def test_probe_mcp_pins_validated_ip_and_preserves_host():
    """The MCP probe pins the validated IP and preserves the Host header on every
    POST while keeping follow_redirects=False."""
    posts = []

    class _FakeResp:
        status_code = 200
        headers = {"mcp-session-id": "sess-1"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"tools": [{"name": "t1"}, {"name": "t2"}]}}

    class _FakeClient:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, extensions=None):
            posts.append({"url": url, "headers": headers or {}, "extensions": extensions or {}})
            return _FakeResp()

    fake_httpx = types.SimpleNamespace(Client=_FakeClient)
    resolved = {"connection_type": "MCP Server", "url": "https://mcp.example.com/mcp",
                "token": "tok", "auth_scheme": "bearer_literal"}
    with _patch_dns({"mcp.example.com": "192.0.2.2"}), \
            patch.dict(sys.modules, {"httpx": fake_httpx}):
        res = cr.probe_connection("mcp-sys", resolved)
    assert res["status"] == "pass", res
    assert res["detail"] == "2 tool(s) discovered", res
    assert posts, "no POST issued"
    for p in posts:
        assert p["url"] == "https://192.0.2.2/mcp", p["url"]
        assert p["headers"].get("Host") == "mcp.example.com", p["headers"]
        assert p["extensions"].get("sni_hostname") == "mcp.example.com", p["extensions"]


def test_probe_rest_rebinding_to_metadata_rejected_before_httpx():
    """A REST target whose host resolves to the IMDS address is rejected by the
    SSRF guard before any httpx call is made (blocked_target)."""
    def _no_httpx(*a, **k):
        raise AssertionError("httpx must not be constructed for a blocked target")

    fake_httpx = types.SimpleNamespace(Client=_no_httpx)
    resolved = {"connection_type": "REST API", "base_url": "https://rebind.example.com/v1"}
    with _patch_dns({"rebind.example.com": "169.254.169.254"}), \
            patch.dict(sys.modules, {"httpx": fake_httpx}):
        res = cr.probe_connection("rest-sys", resolved)
    assert res["status"] == "fail", res
    assert res["reason"] == "blocked_target", res
    assert "169.254" not in res["detail"], res["detail"]


# --------------------------------------------------------------------------- #
# audit_connection_event — never emits secrets
# --------------------------------------------------------------------------- #

def test_audit_event_emits_no_secret():
    with patch.object(cr, "logger") as mock_logger:
        cr.audit_connection_event(
            "update", "highbyte", "admin@example.com", "ok",
            extra={"token": "sekret-token", "password": "pw", "url": "https://hb.example.com/mcp"},
        )
        line = mock_logger.info.call_args[0][0]
    payload = json.loads(line)
    assert payload["event"] == "conn_audit"
    assert payload["action"] == "update"
    assert payload["system_id"] == "highbyte"
    assert payload["actor"] == "admin@example.com"
    assert payload["outcome"] == "ok"
    assert "ts" in payload
    assert "token" not in payload and "password" not in payload   # secret keys dropped
    assert payload["url"] == "https://hb.example.com/mcp"            # non-secret extra kept
    assert "sekret-token" not in line and "pw" not in json.loads(line).get("token", "x")


# --------------------------------------------------------------------------- #
# Step 3 Task 1.1 — base_url masking, node write, describe, CONN_SOURCE
# --------------------------------------------------------------------------- #

def test_mask_connection_masks_base_url_userinfo_and_query_token():
    """(a) base_url is now masked: inline user:pass@ AND a ?token= query param in
    base_url are redacted (req6b — previously a known gap)."""
    cfg = {
        "base_url": "https://u:p-LEAK@i3x.example.com/v1?token=BU-TOK-LEAK&page=2",
        "connection_type": "SiteWise (i3X)",
    }
    masked = cr.mask_connection(cfg)
    blob = json.dumps(masked)
    for leaked in ("p-LEAK", "BU-TOK-LEAK"):
        assert leaked not in blob, f"base_url secret {leaked} leaked: {blob}"
    assert masked["base_url"].startswith("https://***@i3x.example.com/v1"), masked["base_url"]
    assert "page=2" in masked["base_url"]                 # non-secret param kept
    assert cfg["base_url"].startswith("https://u:p-LEAK@")  # input not mutated


def test_write_connection_node_write_contains_config_arn_no_secret():
    """(b) write_connection with secret={...} issues a neptune_write MERGE carrying
    connection_config + secret_arn and NO token/password substring."""
    cr._secret_cache.clear()
    fake_sm = _FakeSMWriter(exists=False)
    captured = {}
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read   # the node write is the only persist

    def _capture(cypher, params):
        captured["cypher"] = cypher
        captured["params"] = params
        return True

    cr._neptune_write = _capture
    try:
        with _patch_dns({"hb.example.com": "10.0.0.9"}):
            arn = cr.write_connection(
                "highbyte",
                non_secret={"url": "https://hb.example.com/mcp", "auth_scheme": "bearer_literal",
                            "connection_type": "MCP Server"},
                secret={"token": "node-secret-tok"},
            )
        # A MERGE on the System node was issued with the config + ARN + type params.
        assert "MERGE (s:System {id: $sid})" in captured["cypher"], captured["cypher"]
        assert "s.connection_config" in captured["cypher"]
        assert "s.secret_arn" in captured["cypher"]
        params = captured["params"]
        assert params["sid"] == "highbyte"
        assert params["arn"] == arn
        assert params["ctype"] == "MCP Server"
        cfg = json.loads(params["cfg_json"])
        assert cfg["url"] == "https://hb.example.com/mcp"
        assert cfg["auth_scheme"] == "bearer_literal"
        # The secret VALUE must never appear in the node write (cypher OR params).
        blob = captured["cypher"] + json.dumps(captured["params"])
        for forbidden in ("node-secret-tok", "token", "password"):
            assert forbidden not in blob, f"{forbidden!r} leaked into node write: {blob}"
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()


def test_write_connection_with_existing_secret_arn_writes_no_new_secret():
    """(c) write_connection with secret_arn=<existing> stores that ARN on the node
    and writes NO new secret (create/put_secret_value NOT called)."""
    cr._secret_cache.clear()
    entry = {"connection_type": "PostgreSQL", "connection_config": {}, "secret_arn": ""}
    fake_sm = _FakeSMWriter(exists=False)
    captured = {}
    existing_arn = "arn:aws:secretsmanager:us-east-1:111:secret:adt/shared-db-AbCdEf"
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = lambda sid: entry
    cr._neptune_write = lambda cypher, params: captured.update(params) or True
    try:
        with _patch_dns({"db.example.com": "10.0.0.5"}):
            arn = cr.write_connection(
                "erp",
                non_secret={"host": "db.example.com", "port": 5432, "dbname": "erp",
                            "schema": "erp_data", "connection_type": "PostgreSQL"},
                secret_arn=existing_arn,
            )
        # The EXISTING ARN is returned and written to the node — no new secret minted.
        assert arn == existing_arn
        assert captured["arn"] == existing_arn
        assert fake_sm.created == [] and fake_sm.put == [], (fake_sm.created, fake_sm.put)
        # Node config holds only credential-free DSN fields.
        cfg = json.loads(captured["cfg_json"])
        assert cfg["host"] == "db.example.com" and cfg["dbname"] == "erp"
        assert "password" not in cfg and "token" not in cfg
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()


def test_write_connection_node_write_failsoft_still_returns_arn():
    """(d) write_connection completes and returns the ARN when neptune_write raises
    (fail-soft — local dev with no Memgraph still gets the secret write)."""
    cr._secret_cache.clear()
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read   # no catalog read on the write path

    def _boom(cypher, params):
        raise RuntimeError("Memgraph unavailable (local mode)")

    cr._neptune_write = _boom
    try:
        with _patch_dns({"hb.example.com": "10.0.0.9"}):
            arn = cr.write_connection(
                "highbyte",
                non_secret={"url": "https://hb.example.com/mcp", "connection_type": "MCP Server"},
                secret={"token": "tok"},
            )
        # Secret write still completed; ARN returned despite the node-write failure.
        assert arn == "arn:aws:secretsmanager:::secret:adt/conn/highbyte"
        assert fake_sm.created and fake_sm.created[0][0] == "adt/conn/highbyte"
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()


def test_describe_connection_is_masked_and_presence_only():
    """(e) describe_connection exposes non-secret fields + has_secret/secret_fields
    only — never a connection_string, an inline-cred url, a token, or a password.
    Sourced node-only (catalog never read). Task 2.3 extends the projection with
    dbname / search_path / secret_arn — the ARN is a POINTER (identifier), never
    a secret value."""
    node = {
        "connection_config": json.dumps({"base_url": "https://u:bu-LEAK@i3x.example.com/v1",
                                         "scheme": "https", "host": "i3x.example.com", "port": 443,
                                         "dbname": "adt_db", "search_path": "cmms_maintenance"}),
        "secret_arn": "arn:secret:i3x",
        "connection_type": "SiteWise (i3X)",
        "connection_string": "https://u:dsn-LEAK@i3x.example.com/v1/openapi.json",
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = lambda arn: {"token": "live-tok-LEAK"}
    try:
        out = cr.describe_connection("sitewise")
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch
    blob = json.dumps(out)
    for forbidden in ("live-tok-LEAK", "bu-LEAK", "dsn-LEAK"):
        assert forbidden not in blob, f"{forbidden!r} leaked into describe output: {blob}"
    # No raw secret-bearing fields are present as VALUES.
    assert "connection_string" not in out
    assert "token" not in out and "password" not in out
    # Presence-only secret reporting.
    assert out["has_secret"] is True
    assert out["secret_fields"] == ["token"], out["secret_fields"]
    # Non-secret summary fields present (base_url masked).
    assert out["system_id"] == "sitewise"
    assert out["connection_type"] == "SiteWise (i3X)"
    assert out["host"] == "i3x.example.com"
    assert out["scheme"] == "https"
    assert out["base_url"].startswith("https://***@i3x.example.com/v1")
    # Task 2.3: the record read for client tooling (ADR-0044 Decision 3) —
    # dbname / search_path served, secret_arn as an ARN POINTER only.
    assert out["dbname"] == "adt_db"
    assert out["search_path"] == "cmms_maintenance"
    assert out["secret_arn"] == "arn:secret:i3x"


def test_describe_connection_search_path_schema_alias_and_unconfigured_arn():
    """Task 2.3: (a) a node persisting only ``schema`` serves it as
    ``search_path`` (same alias rule as pg_conn_params); (b) an unconfigured
    system yields empty-string secret_arn and None dbname/search_path — never a
    KeyError for record-reading clients (reonboard, ADR-0044 Decision 3)."""
    node = {
        "connection_config": json.dumps({"host": "db.example.com", "port": 5432,
                                         "dbname": "erp_simulation",
                                         "schema": "erp_data"}),
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:shared-db",
        "connection_type": "PostgreSQL",
        "connection_string": None,
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = lambda arn: {"user": "postgres", "password": "pw-LEAK"}
    try:
        out = cr.describe_connection("erp")
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch
    assert out["dbname"] == "erp_simulation"
    assert out["search_path"] == "erp_data"          # schema alias
    assert out["secret_arn"].endswith(":secret:shared-db")
    assert "pw-LEAK" not in json.dumps(out)          # secret VALUE never served
    assert "password" not in out

    # (b) unconfigured system: keys present, honest empties.
    orig_node = cr._fetch_node_config
    cr._fetch_node_config = lambda sid: None
    try:
        out = cr.describe_connection("ghost")
    finally:
        cr._fetch_node_config = orig_node
    assert out["secret_arn"] == ""
    assert out["dbname"] is None and out["search_path"] is None
    assert out["has_secret"] is False


def test_resolve_connection_is_node_only_no_catalog_leak():
    """(f) resolve_connection is NODE-ONLY: a DIVERGENT catalog value cannot leak
    because the catalog is never read at all (node is the sole source)."""
    node = {
        "connection_config": json.dumps({"url": "https://node.example.com/mcp"}),
        "secret_arn": "arn:secret:node",
        "connection_type": "MCP Server",
        "connection_string": None,
    }
    orig_node, orig_cat, orig_fetch = (
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret)
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read   # must NOT be called
    cr._fetch_secret = lambda arn: {"token": "node-tok"}
    try:
        resolved = cr.resolve_connection("mcp-sys")
    finally:
        (cr._fetch_node_config, cr._catalog_get, cr._fetch_secret) = (
            orig_node, orig_cat, orig_fetch)
    blob = json.dumps(resolved)
    # The node value wins; no catalog value can appear (catalog never read).
    assert resolved["url"] == "https://node.example.com/mcp"
    assert "catalog" not in blob, blob
    assert resolved.get("schema") is None  # no catalog-only field merged
    assert resolved["secret_arn"] == "arn:secret:node"
    assert resolved["connection_string"] == ""  # node connection_string was None


def test_write_connection_strips_inline_url_credentials_at_rest():
    """FIX 2: a url/base_url carrying inline ``user:pass@`` userinfo or a secret
    query param is STRIPPED to a credential-free URL before being persisted to the
    Neptune node (DROP, not mask — nothing secret stored at rest). The node is the
    sole persist; the catalog is never read or mutated."""
    cr._secret_cache.clear()
    captured = {}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read   # no catalog read/mutate on the write path
    cr._neptune_write = lambda cypher, params: captured.update(params) or True
    try:
        with _patch_dns({"h": "192.0.2.2"}):
            cr.write_connection(
                "rest-sys",
                non_secret={"connection_type": "REST API",
                            "url": "https://u:p@h/x?token=abc",
                            "base_url": "https://u:p@h/v1?api_key=KEY-LEAK&page=2"},
            )
        # Node write: credential-free url/base_url only (userinfo + secret query gone).
        node_cfg = json.loads(captured["cfg_json"])
        assert node_cfg["url"] == "https://h/x", node_cfg
        assert "u:p@" not in node_cfg["url"] and "token=abc" not in node_cfg["url"]
        assert "u:p@" not in node_cfg["base_url"] and "KEY-LEAK" not in node_cfg["base_url"]
        assert "page=2" in node_cfg["base_url"]  # non-secret query param preserved
        # Nowhere in the captured node params either.
        blob = json.dumps(captured)
        assert "u:p@" not in blob and "token=abc" not in blob and "KEY-LEAK" not in blob
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()


def test_strip_url_credentials_helper():
    """FIX 2 helper: _strip_url_credentials DROPS userinfo + secret query params,
    keeps non-secret params, and leaves non-URL/clean inputs unchanged."""
    assert cr._strip_url_credentials("https://u:p@h/x?token=abc") == "https://h/x"
    assert cr._strip_url_credentials("https://h/x") == "https://h/x"
    out = cr._strip_url_credentials("https://u:p@h/v1?api_key=K&page=2")
    assert out == "https://h/v1?page=2", out
    assert cr._strip_url_credentials("") == ""
    assert cr._strip_url_credentials("not-a-url") == "not-a-url"


# --------------------------------------------------------------------------- #
# pg_conn_params — DRY SQL-reassembly helper (Step 3 revised, RW1 / Task 1.1)
# --------------------------------------------------------------------------- #

class _ListHandler(logging.Handler):
    """Capture emitted log records so a test can inspect the FORMATTED message."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_pg_conn_params_full_node_config_and_secret():
    """Full node config + merged secret → pgdriver kwargs sourced entirely from the
    resolved dict, including the search_path option; env fallback is NOT consulted
    (neither the env-only split nor the records-first entrypoint)."""
    import agents.db as db

    def _must_not_run(_schema):
        raise AssertionError("env fallback must not run when a password is present")

    orig_env, orig_rec = db._env_conn_params_for_schema, db._get_conn_params_for_schema
    db._env_conn_params_for_schema = _must_not_run
    db._get_conn_params_for_schema = _must_not_run
    try:
        resolved = {
            "system_id": "erp",
            "connection_type": "PostgreSQL",
            "host": "db.example.com",
            "port": 5432,
            "dbname": "erp_simulation",
            "schema": "erp_data",
            "username": "erp_user",          # RDS-style alias of ``user``
            "password": "node-secret-pw",
        }
        params = cr.pg_conn_params(resolved)
    finally:
        db._env_conn_params_for_schema = orig_env
        db._get_conn_params_for_schema = orig_rec
    assert params["host"] == "db.example.com"
    assert params["port"] == 5432
    assert params["dbname"] == "erp_simulation"
    assert params["user"] == "erp_user"      # username alias mapped to user
    assert params["password"] == "node-secret-pw"
    assert params["options"] == "-csearch_path=erp_data", params


def test_pg_conn_params_falls_back_to_env_when_no_password():
    """Credential-free node config (secret_arn empty → no password) → host/port/dbname
    kept from the resolved node, user/password bootstrapped from the live env via
    db._env_conn_params_for_schema (the ENV-ONLY split — Task 2.3 repoint), routed
    by schema. The records-first db._get_conn_params_for_schema must NOT be
    consulted: it calls back into resolve_connection → pg_conn_params and would
    recurse (plan risk R3).

    The env credentials fill ONLY the env target, so the
    node here names the env's own host + database (a demo node built through
    demo_pg_dsn always does); a node on another host is refused — see
    test_pg_conn_params_refuses_env_fill_for_a_foreign_host below."""
    import agents.db as db

    calls = []

    def _fake_env(schema):
        calls.append(schema)
        return {"host": "env-host", "port": "5432", "dbname": "adt_demo",
                "user": "env_user", "password": "env-pw"}

    def _must_not_run(_schema):
        raise AssertionError(
            "records-first _get_conn_params_for_schema must not be consulted by "
            "the pg_conn_params env bootstrap (recursion risk R3)")

    orig_env, orig_rec = db._env_conn_params_for_schema, db._get_conn_params_for_schema
    db._env_conn_params_for_schema = _fake_env
    db._get_conn_params_for_schema = _must_not_run
    try:
        resolved = {"system_id": "mes", "connection_type": "PostgreSQL",
                    "host": "ENV-HOST", "port": 5433, "dbname": "adt_demo",
                    "schema": "mes_data"}
        params = cr.pg_conn_params(resolved)
    finally:
        db._env_conn_params_for_schema = orig_env
        db._get_conn_params_for_schema = orig_rec
    # Fallback was consulted and routed by the node schema.
    assert calls == ["mes_data"], calls
    # Non-secret target preferred from the node (host compared case-insensitively)...
    assert params["host"] == "ENV-HOST"
    assert params["port"] == 5433
    assert params["dbname"] == "adt_demo"
    # ...credential bootstrapped from the live env.
    assert params["user"] == "env_user"
    assert params["password"] == "env-pw"
    assert params["options"] == "-csearch_path=mes_data", params


def test_pg_conn_params_refuses_env_fill_for_a_foreign_host():
    """The behaviour this file used to pin (node-host + env password) was a
    leak: the deployment's password sent to a host an admin moved the
    system to. The env credentials now fill only the env target."""
    import agents.db as db
    from agents.errors import ConnectionSecretUnavailableError
    orig_env = db._env_conn_params_for_schema
    db._env_conn_params_for_schema = lambda schema: {
        "host": "env-host", "port": "5432", "dbname": "adt_demo",
        "user": "env_user", "password": "env-pw"}
    try:
        for target in ({"host": "node-host", "dbname": "adt_demo"},
                       {"host": "env-host", "dbname": "node_db"}):
            try:
                cr.pg_conn_params({"system_id": "mes", "schema": "mes_data", **target})
            except ConnectionSecretUnavailableError as exc:
                assert "env-pw" not in str(exc)
            else:
                raise AssertionError(f"env credentials filled a foreign target {target}")
    finally:
        db._env_conn_params_for_schema = orig_env


def test_pg_conn_params_empty_resolved_bootstraps_all_from_env():
    """A wholly-unconfigured node (empty resolved) → every field from the live-env
    bootstrap (the env-only split), and no search_path option (no schema present)."""
    import agents.db as db

    orig = db._env_conn_params_for_schema
    db._env_conn_params_for_schema = lambda schema: {
        "host": "localhost", "port": "5432", "dbname": "adt_demo",
        "user": "postgres", "password": "env-pw"}
    try:
        params = cr.pg_conn_params({})
    finally:
        db._env_conn_params_for_schema = orig
    assert params == {"host": "localhost", "port": "5432",
                      "dbname": "adt_demo", "user": "postgres",
                      "password": "env-pw"}
    assert "options" not in params  # no schema/search_path → no options


def test_pg_conn_params_never_logs_password():
    """The assembled password VALUE never appears in ANY log line the helper emits,
    for BOTH the resolved-secret and the env-bootstrap credential sources."""
    import agents.db as db

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    orig_level = cr.logger.level
    cr.logger.addHandler(handler)
    cr.logger.setLevel(logging.DEBUG)
    orig = db._env_conn_params_for_schema
    db._env_conn_params_for_schema = lambda schema: {
        "host": "h", "port": "5432", "dbname": "d", "user": "u",
        "password": "ENV-PW-SHOULD-NOT-LOG"}
    try:
        # (a) password from the resolved secret.
        cr.pg_conn_params({"host": "h", "port": 5432, "dbname": "d",
                           "schema": "erp_data", "user": "u",
                           "password": "RESOLVED-PW-SHOULD-NOT-LOG"})
        # (b) password from the env bootstrap.
        cr.pg_conn_params({"host": "h", "dbname": "d", "schema": "erp_data"})
    finally:
        cr.logger.removeHandler(handler)
        cr.logger.setLevel(orig_level)
        db._env_conn_params_for_schema = orig
    blob = "\n".join(r.getMessage() for r in handler.records)
    for forbidden in ("RESOLVED-PW-SHOULD-NOT-LOG", "ENV-PW-SHOULD-NOT-LOG"):
        assert forbidden not in blob, f"password leaked into log: {blob}"


# --------------------------------------------------------------------------- #
# db_secret_arn_for_schema — Task 2.3 delegation to the ONE demo routing module
# (agents/demo/db_routing.py, ADR-0044 Decision 2). The local erp_prefixes tuple
# is DELETED from this file; behavior parity with the legacy env routing is
# pinned below (poison-env style), plus the "" fail-soft.
# --------------------------------------------------------------------------- #

def test_db_secret_arn_for_schema_delegates_to_demo_module():
    """The public name/signature stays (onboard.py keeps its import unchanged)
    but the BODY is a pure delegation to demo.db_routing.demo_db_secret_arn."""
    import agents.demo.db_routing as db_routing

    calls = []
    orig = db_routing.demo_db_secret_arn
    db_routing.demo_db_secret_arn = (
        lambda schema, env=None: calls.append(schema) or "arn:demo:layer")
    try:
        out = cr.db_secret_arn_for_schema("erp_data")
    finally:
        db_routing.demo_db_secret_arn = orig
    assert out == "arn:demo:layer"
    assert calls == ["erp_data"], calls


def test_db_secret_arn_for_schema_env_routing_parity():
    """Poison-env parity with the deleted local routing: ERP-family schemas
    (erp_*/mes_*/public, ERP host set) → ERP_DB_SECRET_ARN; everything else →
    DB_SECRET_ARN. SSM is never consulted while env is set (pointer tripwire)."""
    import agents.demo.db_routing as db_routing

    def _no_ssm(kind):
        raise AssertionError(f"SSM pointer read for {kind!r} — env must win")

    env = {"ERP_POSTGRES_HOST": "aurora.example.com",
           "ERP_DB_SECRET_ARN": "arn:erp-secret",
           "DB_SECRET_ARN": "arn:adt-secret"}
    orig_ptr = db_routing._demo_db_pointer
    db_routing._demo_db_pointer = _no_ssm
    try:
        with patch.dict(os.environ, env, clear=False):
            for schema in ("erp_data", "mes_milwaukee", "public"):
                assert cr.db_secret_arn_for_schema(schema) == "arn:erp-secret", schema
            for schema in ("cmms_maintenance", "plm_engineering", "adt_demo"):
                assert cr.db_secret_arn_for_schema(schema) == "arn:adt-secret", schema
        # ERP family WITHOUT an ERP host routes to the shared demo secret
        # (legacy behavior kept by the demo module); pointer still not read
        # because DB_SECRET_ARN is set. NOTE: the missing-erp-host branch DOES
        # probe the erp-db pointer first, so allow it to return {} here.
        db_routing._demo_db_pointer = lambda kind: {}
        no_erp_host = {k: v for k, v in env.items() if k != "ERP_POSTGRES_HOST"}
        with patch.dict(os.environ, no_erp_host, clear=True):
            assert cr.db_secret_arn_for_schema("erp_data") == "arn:adt-secret"
    finally:
        db_routing._demo_db_pointer = orig_ptr


def test_db_secret_arn_for_schema_empty_failsoft_and_no_tuple_left():
    """No env + empty SSM pointers → "" (fail-soft, never raises); and the grep
    gate for THIS file: the local erp_prefixes routing tuple is gone from
    connection_resolver.py (demo knowledge lives only in agents/demo/)."""
    import agents.demo.db_routing as db_routing

    orig_ptr = db_routing._demo_db_pointer
    db_routing._demo_db_pointer = lambda kind: {}
    try:
        with patch.dict(os.environ, {}, clear=True):
            assert cr.db_secret_arn_for_schema("erp_data") == ""
            assert cr.db_secret_arn_for_schema("cmms_maintenance") == ""
            assert cr.db_secret_arn_for_schema("") == ""
    finally:
        db_routing._demo_db_pointer = orig_ptr
    source = Path(cr.__file__).read_text()
    assert "erp_prefixes" not in source, (
        "the local ERP routing tuple must stay deleted from connection_resolver.py "
        "(ADR-0044 Decision 2 — demo routing lives in agents/demo/db_routing.py)")


# --------------------------------------------------------------------------- #
# G2 — typed (Athena) + cross-account config durability across an app/ECS restart
# --------------------------------------------------------------------------- #

def test_write_connection_persists_athena_and_cross_account_non_secret_keys():
    """G2: the typed Athena keys (workgroup/database/catalog/output_location/region)
    and the cross-account ``role_arn`` survive the node write — they are no longer
    dropped by the ``_NODE_CONFIG_KEYS`` allowlist, so a cross-account Athena system
    is durable across an app/ECS restart. (``role_arn`` is an identifier, not a
    secret; ``external_id`` is the secret and is covered separately.)"""
    cr._secret_cache.clear()
    captured = {}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read
    cr._neptune_write = lambda cypher, params: captured.update(params) or True
    try:
        cr.write_connection(
            "ags",
            non_secret={
                "connection_type": "Athena",
                "workgroup": "primary", "database": "mfg_db",
                "catalog": "awsdatacatalog", "output_location": "s3://amzn-s3-demo-results/",
                "region": "us-east-1",
                "role_arn": XACCT_ROLE_ARN,
            },
        )
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()
    assert captured["ctype"] == "Athena", captured
    cfg = json.loads(captured["cfg_json"])
    for k, v in (("workgroup", "primary"), ("database", "mfg_db"),
                 ("catalog", "awsdatacatalog"), ("output_location", "s3://amzn-s3-demo-results/"),
                 ("region", "us-east-1"),
                 ("role_arn", XACCT_ROLE_ARN)):
        assert cfg.get(k) == v, (k, cfg)


def test_write_connection_persists_athena_tunable_timeout_knobs():
    """Tasks G+H durability: the two admin-tunable Athena poll bounds
    (``poll_timeout_seconds`` / ``introspection_timeout_seconds``) are the TOP
    tier of their config > env > default precedence, so — exactly like
    ``max_scanned_bytes`` — they must survive the node write or the per-system
    tier silently evaporates on an ECS task recycle."""
    cr._secret_cache.clear()
    captured = {}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read
    cr._neptune_write = lambda cypher, params: captured.update(params) or True
    try:
        cr.write_connection(
            "ags_timeouts",
            non_secret={
                "connection_type": "Athena",
                "workgroup": "primary", "database": "mfg_db", "region": "us-east-1",
                "max_scanned_bytes": 5_000_000,
                "poll_timeout_seconds": 180,
                "introspection_timeout_seconds": 4.5,
            },
        )
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()
    cfg = json.loads(captured["cfg_json"])
    assert cfg["max_scanned_bytes"] == 5_000_000, cfg
    assert cfg["poll_timeout_seconds"] == 180, cfg
    assert cfg["introspection_timeout_seconds"] == 4.5, cfg


def test_tunable_timeout_knobs_are_not_catalog_safe():
    """Operational tuning knobs are NOT display-relevant target fields — they stay
    OUT of the served catalog/UI subset (same treatment as ``max_scanned_bytes``)."""
    for key in ("poll_timeout_seconds", "introspection_timeout_seconds",
                "max_scanned_bytes"):
        assert key not in cr.CATALOG_SAFE_CONFIG_KEYS
    served = cr.sanitize_connection_config({
        "workgroup": "primary", "poll_timeout_seconds": 180,
        "introspection_timeout_seconds": 4.5, "max_scanned_bytes": 1,
    })
    assert served == {"workgroup": "primary"}, served


def test_write_connection_external_id_routed_via_secret_never_on_node():
    """G2 secret hygiene: the cross-account ``external_id`` is a secret — it is
    routed through Secrets Manager (``secret=...``) and NEVER written to the node
    config. The node write blob must not contain the external_id value."""
    cr._secret_cache.clear()
    captured = {}
    fake_sm = _FakeSMWriter(exists=False)
    orig_client, orig_cat, orig_nw = cr._secrets_client, cr._catalog_get, cr._neptune_write
    cr._secrets_client = lambda: fake_sm
    cr._catalog_get = _raise_if_catalog_read
    cr._neptune_write = lambda cypher, params: captured.update(params) or True
    try:
        arn = cr.write_connection(
            "ags",
            non_secret={"connection_type": "Athena", "workgroup": "primary",
                        "database": "mfg_db",
                        "role_arn": XACCT_ROLE_ARN},
            secret={"external_id": "ext-SECRET-999"},
        )
    finally:
        cr._secrets_client, cr._catalog_get, cr._neptune_write = orig_client, orig_cat, orig_nw
        cr._secret_cache.clear()
    # A conn/<id> secret was minted carrying the external_id...
    assert fake_sm.created and fake_sm.created[0][0] == "adt/conn/ags"
    assert json.loads(fake_sm.created[0][1]) == {"external_id": "ext-SECRET-999"}
    assert captured["arn"] == arn
    # ...and the external_id value NEVER reaches the node write (cypher OR params).
    blob = json.dumps(captured)
    assert "ext-SECRET-999" not in blob, blob
    cfg = json.loads(captured["cfg_json"])
    assert "external_id" not in cfg, cfg


def test_resolve_connection_round_trips_athena_and_merges_external_id_secret():
    """G2 round-trip: an Athena node persisted with the typed + cross-account keys
    resolves back with workgroup/database/region/role_arn AND the external_id
    merged from the referenced secret — so a restored agent can re-assume the
    cross-account role after an ECS task recycle. Node is the sole source."""
    node = {
        "connection_config": json.dumps({
            "workgroup": "primary", "database": "mfg_db", "catalog": "awsdatacatalog",
            "region": "us-east-1",
            "role_arn": XACCT_ROLE_ARN,
        }),
        "secret_arn": "arn:secret:ags",
        "connection_type": "Athena",
        "connection_string": None,
    }
    orig_node, orig_cat, orig_fetch = cr._fetch_node_config, cr._catalog_get, cr._fetch_secret
    cr._fetch_node_config = lambda sid: node
    cr._catalog_get = _raise_if_catalog_read
    cr._fetch_secret = lambda arn: {"external_id": "ext-SECRET-999"}
    try:
        resolved = cr.resolve_connection("ags")
    finally:
        cr._fetch_node_config, cr._catalog_get, cr._fetch_secret = orig_node, orig_cat, orig_fetch
    assert resolved["workgroup"] == "primary"
    assert resolved["database"] == "mfg_db"
    assert resolved["catalog"] == "awsdatacatalog"
    assert resolved["region"] == "us-east-1"
    assert resolved["role_arn"] == XACCT_ROLE_ARN
    assert resolved["connection_type"] == "Athena"
    assert resolved["external_id"] == "ext-SECRET-999"     # merged from the secret


def test_write_scan_budget_sets_key_and_never_touches_secret_arn():
    """write_scan_budget does a TARGETED node write: it sets connection_config's
    ``max_scanned_bytes`` onto the EXISTING config blob and writes ONLY
    ``connection_config`` — never ``secret_arn`` — so a credential pointer can't be
    wiped the way a full write_connection (which rewrites secret_arn) would."""
    captured = {}
    orig_fetch, orig_nw = cr._fetch_node_config, cr._neptune_write
    cr._fetch_node_config = lambda sid: {
        "connection_config": json.dumps({"workgroup": "primary", "database": "mfg_db"}),
        "secret_arn": "arn:aws:secretsmanager:::secret:keepme",
    }
    cr._neptune_write = lambda cypher, params: (
        captured.update({"cypher": cypher, **params}) or True)
    try:
        cr.write_scan_budget("ags", 4096)
    finally:
        cr._fetch_node_config, cr._neptune_write = orig_fetch, orig_nw
    cfg = json.loads(captured["cfg_json"])
    assert cfg["max_scanned_bytes"] == 4096
    # Existing config keys are preserved (merge, not overwrite).
    assert cfg["workgroup"] == "primary" and cfg["database"] == "mfg_db"
    # Only connection_config is SET; secret_arn is neither in the cypher nor params.
    assert "secret_arn" not in captured["cypher"]
    assert "arn" not in captured


def test_write_scan_budget_none_clears_key_preserving_rest():
    captured = {}
    orig_fetch, orig_nw = cr._fetch_node_config, cr._neptune_write
    cr._fetch_node_config = lambda sid: {
        "connection_config": json.dumps({"workgroup": "primary", "max_scanned_bytes": 4096}),
        "secret_arn": "",
    }
    cr._neptune_write = lambda cypher, params: (captured.update(params) or True)
    try:
        cr.write_scan_budget("ags", None)
    finally:
        cr._fetch_node_config, cr._neptune_write = orig_fetch, orig_nw
    cfg = json.loads(captured["cfg_json"])
    assert "max_scanned_bytes" not in cfg   # cleared
    assert cfg["workgroup"] == "primary"    # rest preserved


def test_write_scan_budget_fail_loud_on_write_failure():
    """A graph write failure raises (fail-loud) so the admin route surfaces an
    error instead of silently dropping the setting."""
    orig_fetch, orig_nw = cr._fetch_node_config, cr._neptune_write
    cr._fetch_node_config = lambda sid: {"connection_config": "{}", "secret_arn": ""}
    cr._neptune_write = lambda cypher, params: False   # simulate graph write failure
    raised = False
    try:
        cr.write_scan_budget("ags", 4096)
    except RuntimeError:
        raised = True
    finally:
        cr._fetch_node_config, cr._neptune_write = orig_fetch, orig_nw
    assert raised, "write_scan_budget must raise on a failed node write"


_TESTS = [
    test_scheme_allowlist_rejects_dangerous_schemes,
    test_metadata_and_link_local_rejected,
    test_dns_rebinding_to_metadata_rejected,
    test_hostname_resolving_to_rfc1918_allowed,
    test_unresolvable_host_rejected,
    test_integer_ipv4_metadata_form_rejected,
    test_ipv4_mapped_ipv6_metadata_rejected,
    test_ipv4_compat_6to4_teredo_metadata_rejected,
    test_global_ipv6_allowed,
    test_validate_returns_pinned_ip,
    test_probe_postgres_pins_validated_ip_via_hostaddr,
    test_rfc1918_private_targets_allowed,
    test_empty_target_rejected,
    test_mask_connection_redacts_and_does_not_mutate,
    test_mask_connection_redacts_query_string_secrets,
    test_secret_cache_ttl_then_refetch,
    test_parse_secret_json_and_bare_string,
    test_resolve_connection_reads_from_registered_node,
    test_resolve_connection_unregistered_node_is_unconfigured_no_catalog_read,
    test_resolve_connection_node_without_config_is_unconfigured,
    test_resolve_connection_graph_read_failure_is_unconfigured_no_raise,
    test_resolve_connection_merges_secret,
    test_resolve_connection_marks_unresolved_secret,
    test_probe_rejects_metadata_target,
    test_probe_postgres_pass_with_mock,
    test_probe_postgres_provisioned_node_without_dsn,
    test_probe_postgres_no_dsn_user_registration_never_env_filled,
    test_probe_postgres_pre_node_demo_entry_routes_by_entry_schema,
    test_probe_failure_detail_is_coarse_no_host_ip_port_or_secret,
    test_sitewise_i3x_routes_to_rest_not_boto3,
    test_sitewise_direct_routes_to_boto3,
    test_credential_unresolved_reason,
    test_write_connection_writes_secret_invalidates_and_persists,
    test_write_connection_put_when_secret_exists,
    test_write_connection_highbyte_does_not_write_legacy_token,
    test_write_connection_non_highbyte_does_not_write_legacy_token,
    test_write_connection_rejects_bad_scheme_before_writing,
    test_write_connection_rejects_rebinding_target_before_writing,
    test_probe_rest_pins_validated_ip_and_preserves_host_and_sni,
    test_probe_mcp_pins_validated_ip_and_preserves_host,
    test_probe_rest_rebinding_to_metadata_rejected_before_httpx,
    test_audit_event_emits_no_secret,
    test_mask_connection_masks_base_url_userinfo_and_query_token,
    test_write_connection_node_write_contains_config_arn_no_secret,
    test_write_connection_with_existing_secret_arn_writes_no_new_secret,
    test_write_connection_node_write_failsoft_still_returns_arn,
    test_describe_connection_is_masked_and_presence_only,
    test_describe_connection_search_path_schema_alias_and_unconfigured_arn,
    test_write_connection_strips_inline_url_credentials_at_rest,
    test_strip_url_credentials_helper,
    test_resolve_connection_is_node_only_no_catalog_leak,
    test_pg_conn_params_full_node_config_and_secret,
    test_pg_conn_params_falls_back_to_env_when_no_password,
    test_pg_conn_params_empty_resolved_bootstraps_all_from_env,
    test_pg_conn_params_never_logs_password,
    test_db_secret_arn_for_schema_delegates_to_demo_module,
    test_db_secret_arn_for_schema_env_routing_parity,
    test_db_secret_arn_for_schema_empty_failsoft_and_no_tuple_left,
    test_write_connection_persists_athena_and_cross_account_non_secret_keys,
    test_write_connection_external_id_routed_via_secret_never_on_node,
    test_resolve_connection_round_trips_athena_and_merges_external_id_secret,
    test_write_scan_budget_sets_key_and_never_touches_secret_arn,
    test_write_scan_budget_none_clears_key_preserving_rest,
    test_write_scan_budget_fail_loud_on_write_failure,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} connection_resolver unit tests passed (no real AWS/DB/HTTP)")
