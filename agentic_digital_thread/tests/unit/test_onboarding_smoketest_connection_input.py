# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``scripts/onboarding_smoketest.py`` — Step-3 (revised) Task 4.1.

Asserts the seed harness behaves as an AUTOMATED USER that DERIVES each demo
system's connection from the DEPLOY ENV — deriving the ``connection_bootstrap``
descriptor per connection TYPE from each system's LIVE registration identity
(``connection_type`` + ``schema`` from ``GET /api/onboard/catalog``) — and
SUPPLIES it as INPUT to the onboarding API, never relying on a server-side
catalog connection snapshot or a baked-in per-system manifest.

Two layers, both hermetic (mocked env + mocked ``App.call`` — NO live network):
  * pure derivation: ``_pg_dsn_from_env`` / ``build_connection`` /
    ``bootstrap_descriptor`` / ``connection_inputs`` build the right connection
    from env per the live identity.
  * end-to-end wiring: running ``main()`` INCLUDES ``connection_string`` in the
    discover AND provision request bodies, and supplies the highbyte bearer token
    out-of-band via ``PUT /api/admin/highbyte-token`` (the bodies carry no token).

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboarding_smoketest_connection_input.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboarding_smoketest_connection_input.py
"""
import importlib.util
import sys
from pathlib import Path

# Load scripts/onboarding_smoketest.py by file path (scripts/ is not a package).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"
_spec = importlib.util.spec_from_file_location("onboarding_smoketest", _HARNESS_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


# Live-registration identity the deployed catalog returns per demo system
# (connection_type + schema). The harness reads THIS (via GET /api/onboard/catalog)
# — never a hardcoded manifest — and derives the env-driven connection per TYPE.
_IDENTITY = {
    "erp":             {"connection_type": "PostgreSQL", "schema": "erp_data"},
    "mes-mil":         {"connection_type": "PostgreSQL", "schema": "mes_milwaukee"},
    "mes-det":         {"connection_type": "PostgreSQL", "schema": "mes_detroit"},
    "mes-cr":          {"connection_type": "PostgreSQL", "schema": "mes_cedarrapids"},
    "cmms":            {"connection_type": "PostgreSQL", "schema": "cmms_maintenance"},
    "cmms2":           {"connection_type": "PostgreSQL", "schema": "cmms2_maintenance"},
    "plm":             {"connection_type": "PostgreSQL", "schema": "plm_engineering"},
    "highbyte":        {"connection_type": "MCP Server", "schema": ""},
    "sitewise":        {"connection_type": "SiteWise (i3X)", "schema": ""},
    "sitewise-direct": {"connection_type": "SiteWise (Direct)", "schema": ""},
}

# A complete deploy env (demo + ERP DBs, HighByte, region). No SITEWISE_I3X_URL, so
# the i3X tile derives an empty connection and is skipped.
_ENV = {
    "POSTGRES_HOST": "demohost", "POSTGRES_PORT": "5432", "POSTGRES_DB": "adt_demo",
    "POSTGRES_USER": "demouser", "POSTGRES_PASSWORD": "demopass",
    "ERP_POSTGRES_HOST": "erphost", "ERP_POSTGRES_PORT": "5432", "ERP_POSTGRES_DB": "erp_simulation",
    "ERP_POSTGRES_USER": "erpuser", "ERP_POSTGRES_PASSWORD": "erppass",
    "HIGHBYTE_URL": "https://hb.example.com/", "HIGHBYTE_I3X_TOKEN": "tok-123",
    "AWS_REGION": "us-west-2",
}


# --------------------------------------------------------------------------- #
# Layer 1 — pure env derivation per the manifest descriptor.
# --------------------------------------------------------------------------- #
def test_pg_dsn_routes_erp_schemas_to_erp_db_with_credentials():
    dsn = smoke._pg_dsn_from_env("erp_data", _ENV)
    assert dsn == "postgresql://erpuser:erppass@erphost:5432/erp_simulation?options=-csearch_path%3Derp_data"
    # mes_* also routes to the ERP database.
    assert smoke._pg_dsn_from_env("mes_milwaukee", _ENV).startswith("postgresql://erpuser:erppass@erphost:5432/")


def test_pg_dsn_routes_non_erp_schemas_to_adt_db():
    dsn = smoke._pg_dsn_from_env("cmms_maintenance", _ENV)
    assert dsn == "postgresql://demouser:demopass@demohost:5432/adt_demo?options=-csearch_path%3Dcmms_maintenance"


def test_pg_dsn_url_encodes_special_chars_in_credential():
    env = dict(_ENV, POSTGRES_USER="u@s/r", POSTGRES_PASSWORD="p@ss:w/rd")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    dsn = smoke._pg_dsn_from_env("plm_engineering", env)
    # '@', ':' and '/' in the userinfo must be percent-encoded so the host parses.
    assert "u%40s%2Fr:p%40ss%3Aw%2Frd@demohost" in dsn


def test_pg_dsn_empty_without_password_signals_fail_loud():
    """No password → empty DSN. Post-cut-over there is NO server fallback: an empty
    DSN signals the CALLER must fail loudly for that system (see
    test_main_fails_loudly_when_pg_connection_not_derivable). The pure derivation
    still returns "" so the caller can detect it."""
    env = {k: v for k, v in _ENV.items() if k not in ("POSTGRES_PASSWORD", "ERP_POSTGRES_PASSWORD")}
    assert smoke._pg_dsn_from_env("cmms_maintenance", env) == ""
    assert smoke._pg_dsn_from_env("erp_data", env) == ""


def test_build_connection_mcp_appends_suffix_and_carries_token():
    boot = smoke.bootstrap_descriptor("MCP Server", "")
    conn = smoke.build_connection(boot, _ENV)
    assert conn["connection_string"] == "https://hb.example.com/mcp"
    assert conn["token"] == "tok-123"


def test_build_connection_rest_uses_base_url_env():
    boot = smoke.bootstrap_descriptor("SiteWise (i3X)", "")
    assert smoke.build_connection(boot, dict(_ENV, SITEWISE_I3X_URL="https://i3x.example.com/")) == {
        "connection_string": "https://i3x.example.com", "token": ""}
    # No env → empty connection (system gets skipped by main()).
    assert smoke.build_connection(boot, _ENV)["connection_string"] == ""


def test_build_connection_sitewise_direct_uses_region():
    boot = smoke.bootstrap_descriptor("SiteWise (Direct)", "")
    # Resolvable service-endpoint host (iotsitewise.<region>.amazonaws.com), not bare region.
    assert smoke.build_connection(boot, _ENV)["connection_string"] == "iotsitewise://iotsitewise.us-west-2.amazonaws.com"


def test_bootstrap_descriptor_per_connection_type():
    # PostgreSQL carries the schema; unknown types yield a no-op descriptor.
    assert smoke.bootstrap_descriptor("PostgreSQL", "erp_data") == {
        "kind": "pg", "schema": "erp_data", "from_env": "pg"}
    assert smoke.bootstrap_descriptor("MCP Server", "")["kind"] == "mcp"
    assert smoke.bootstrap_descriptor("SiteWise (i3X)", "")["kind"] == "rest"
    assert smoke.bootstrap_descriptor("SiteWise (Direct)", "")["kind"] == "sitewise_direct"
    assert smoke.bootstrap_descriptor("Something Else", "")["kind"] == "none"


def test_connection_inputs_builds_each_system_from_live_identity():
    systems = ["erp", "cmms", "highbyte", "sitewise-direct", "sitewise"]
    ci = smoke.connection_inputs(_IDENTITY, systems, _ENV)
    assert ci["erp"]["connection_string"].startswith("postgresql://erpuser:erppass@erphost")
    assert ci["erp"]["connection_type"] == "PostgreSQL"
    assert ci["cmms"]["connection_string"].startswith("postgresql://demouser:demopass@demohost")
    assert ci["highbyte"]["connection_string"] == "https://hb.example.com/mcp"
    assert ci["highbyte"]["token"] == "tok-123"
    assert ci["sitewise-direct"]["connection_string"] == "iotsitewise://iotsitewise.us-west-2.amazonaws.com"
    assert ci["sitewise"]["connection_string"] == ""  # no SITEWISE_I3X_URL → skipped


# --------------------------------------------------------------------------- #
# Layer 2 — end-to-end wiring: connection supplied as INPUT in the API bodies.
# --------------------------------------------------------------------------- #
class _Recorder:
    """Stand-in for ``App.call`` that records every request and returns canned
    responses so ``main()`` runs to completion with no network."""
    def __init__(self, systems):
        self.calls = []  # list of (method, path, body)
        self._systems = systems

    def __call__(self, method, path, body=None, timeout=120):
        self.calls.append((method, path, dict(body) if isinstance(body, dict) else body))
        if path == "/api/auth/login":
            return {"status": "ok", "email": "ADT"}
        if path == "/api/onboard/catalog":
            return {"systems": [
                {"id": s, "status": "onboarded", "name": s, **_IDENTITY.get(s, {})}
                for s in self._systems]}
        if path == "/api/onboard/discover":
            return {"tables": [{"name": "t", "columns": []}], "schema_name": ""}
        if path == "/api/onboard/map":
            return {"mappings": [{"table": "t", "field": "f", "concept": "batch"}]}
        if path == "/api/onboard/resolve":
            return {"rules": []}
        if path == "/api/onboard/provision":
            return {"status": "done"}
        if path == "/api/admin/highbyte-token":
            return {"status": "ok"}
        if path == "/api/thread/analytics/vocabulary":
            return {"total_fields": 1, "mapped_concepts": 1}
        if path == "/api/thread/analytics/concept-coverage":
            return {"systems": [{"id": s} for s in self._systems]}
        return {}

    def bodies(self, path):
        return [b for (_m, p, b) in self.calls if p == path]


def _run_main(monkeypatch, argv, env):
    systems = list(smoke.DEFAULT_SYSTEMS) + list(smoke.OT_SYSTEMS)
    rec = _Recorder(systems)
    monkeypatch.setattr(smoke.App, "call", rec, raising=True)
    # The harness reads system identity from the live catalog (the _Recorder canned
    # response carries connection_type/schema) — no manifest import / boto side effects.
    for k in list(env):
        monkeypatch.setenv(k, env[k])
    monkeypatch.setattr(sys, "argv", argv)
    rc = smoke.main()
    return rec, rc


def test_main_supplies_connection_string_in_discover_and_provision(monkeypatch):
    rec, rc = _run_main(
        monkeypatch,
        ["onboarding_smoketest.py", "--base-url", "http://127.0.0.1:8000",
         "--password", "pw", "--include-ot", "--skip-phase2"],
        _ENV,
    )
    assert rc == 0, "harness should pass with fully-onboarded canned catalog"

    disc = rec.bodies("/api/onboard/discover")
    prov = rec.bodies("/api/onboard/provision")
    disc_by_id = {b["system_id"]: b for b in disc}
    prov_by_id = {b["system_id"]: b for b in prov}

    # Every PG system supplied its env-derived DSN as INPUT in BOTH bodies.
    for sid, host in (("erp", "erphost"), ("mes-mil", "erphost"),
                      ("cmms", "demohost"), ("plm", "demohost")):
        assert "connection_string" in disc_by_id[sid], f"{sid} discover missing connection_string"
        assert disc_by_id[sid]["connection_string"].startswith("postgresql://"), sid
        assert host in disc_by_id[sid]["connection_string"], f"{sid} wrong DB host"
        assert prov_by_id[sid]["connection_string"] == disc_by_id[sid]["connection_string"], sid

    # highbyte (MCP): connection_string is the MCP url; the token is NOT in the body.
    assert disc_by_id["highbyte"]["connection_string"] == "https://hb.example.com/mcp"
    assert "token" not in disc_by_id["highbyte"]
    assert "token" not in prov_by_id["highbyte"]

    # sitewise-direct: region-derived connection_string supplied as input.
    assert prov_by_id["sitewise-direct"]["connection_string"] == "iotsitewise://iotsitewise.us-west-2.amazonaws.com"

    # sitewise (i3X) has no env → skipped entirely (no discover/provision).
    assert "sitewise" not in disc_by_id


def test_main_supplies_highbyte_token_out_of_band_before_provision(monkeypatch):
    rec, _rc = _run_main(
        monkeypatch,
        ["onboarding_smoketest.py", "--base-url", "http://127.0.0.1:8000",
         "--password", "pw", "--include-ot", "--skip-phase2"],
        _ENV,
    )
    tok_bodies = rec.bodies("/api/admin/highbyte-token")
    assert tok_bodies == [{"token": "tok-123"}], "highbyte token must be supplied via the admin input"

    # The token PUT must happen BEFORE the highbyte provision (write_connection mints
    # conn/highbyte before the runtime is provisioned).
    paths = [p for (_m, p, _b) in rec.calls]
    tok_idx = paths.index("/api/admin/highbyte-token")
    prov_idxs = [i for i, (_m, p, b) in enumerate(rec.calls)
                 if p == "/api/onboard/provision" and isinstance(b, dict) and b.get("system_id") == "highbyte"]
    assert prov_idxs and tok_idx < prov_idxs[0], "token must be set before highbyte provision"


def test_main_fails_loudly_when_pg_connection_not_derivable(monkeypatch):
    """A required (SQL/default) system whose connection cannot be derived from env
    (no password) must FAIL LOUDLY: the harness returns non-zero, never sends an
    empty/absent connection_string discover for that system, and never leans on a
    (now-removed) server fallback."""
    env_no_pw = {k: v for k, v in _ENV.items()
                 if k not in ("POSTGRES_PASSWORD", "ERP_POSTGRES_PASSWORD")}
    systems = list(smoke.DEFAULT_SYSTEMS)
    rec = _Recorder(systems)
    monkeypatch.setattr(smoke.App, "call", rec, raising=True)
    # Clear any inherited password env so derivation truly fails.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("ERP_POSTGRES_PASSWORD", raising=False)
    for k, v in env_no_pw.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv",
                        ["onboarding_smoketest.py", "--base-url", "http://127.0.0.1:8000",
                         "--password", "pw", "--skip-phase2"])
    rc = smoke.main()

    # Non-zero exit: the required PG systems could not be derived → loud failure.
    assert rc == 1, "harness must fail when a required PG connection cannot be derived"
    # No discover/provision was issued for an underivable system (no empty-DSN send).
    assert rec.bodies("/api/onboard/discover") == [], rec.bodies("/api/onboard/discover")
    assert rec.bodies("/api/onboard/provision") == [], rec.bodies("/api/onboard/provision")


def test_main_does_not_rely_on_catalog_for_connection(monkeypatch):
    """The harness never reads connection from the server catalog: the catalog
    endpoint is used only for identity/status, and connection_string is sourced
    from env. Here we PROVE the supplied connection_string is the env DSN, not
    anything the catalog returned (the canned catalog carries no connection)."""
    rec, _rc = _run_main(
        monkeypatch,
        ["onboarding_smoketest.py", "--base-url", "http://127.0.0.1:8000",
         "--password", "pw", "--skip-phase2"],
        _ENV,
    )
    erp = next(b for b in rec.bodies("/api/onboard/provision") if b["system_id"] == "erp")
    assert erp["connection_string"] == smoke._pg_dsn_from_env("erp_data", _ENV)


# --------------------------------------------------------------------------- #
# Tulip (Tables API) — scripted greenfield onboarding. First-class type.
# --------------------------------------------------------------------------- #
_TULIP_ENV = {
    "TULIP_URL": "https://your-instance.tulip.co/",
    "TULIP_API_KEY": "apikey-123",
    "TULIP_API_SECRET": "secret-xyz",
}


# Tulip is a first-class type — the smoketest
# derives the Tulip descriptor from the declared "Tulip (Tables API)" identity,
# and a bare "REST API" is now a generic (skipped) system.
_TULIP_TYPE = "Tulip (Tables API)"


def test_bootstrap_descriptor_tulip_type_is_tulip():
    boot = smoke.bootstrap_descriptor(_TULIP_TYPE, "")
    assert boot["kind"] == "tulip"
    assert boot["url_env"] == "TULIP_URL"
    assert boot["key_env"] == "TULIP_API_KEY"
    assert boot["secret_env"] == "TULIP_API_SECRET"


def test_bootstrap_descriptor_plain_rest_is_none():
    # A generic "REST API" system is no longer auto-mapped to Tulip; the
    # demo has none, so it yields kind "none" (derive-empty / skip).
    assert smoke.bootstrap_descriptor("REST API", "") == {"kind": "none"}


def test_build_connection_tulip_mints_basic_token():
    import base64
    boot = smoke.bootstrap_descriptor(_TULIP_TYPE, "")
    conn = smoke.build_connection(boot, _TULIP_ENV)
    assert conn["connection_string"] == "https://your-instance.tulip.co"
    assert conn["token"] == base64.b64encode(b"apikey-123:secret-xyz").decode()


def test_build_connection_tulip_empty_without_credentials():
    boot = smoke.bootstrap_descriptor(_TULIP_TYPE, "")
    # URL present but no key/secret → empty connection_string (OT-skip path).
    assert smoke.build_connection(boot, {"TULIP_URL": "https://your-instance.tulip.co"}) == {
        "connection_string": "", "token": ""}
    # No env at all → empty too.
    assert smoke.build_connection(boot, {})["connection_string"] == ""


def test_connection_inputs_tulip_from_live_tulip_identity():
    identity = {"tulip": {"connection_type": _TULIP_TYPE, "schema": ""}}
    ci = smoke.connection_inputs(identity, ["tulip"], _TULIP_ENV)
    assert ci["tulip"]["connection_type"] == _TULIP_TYPE
    assert ci["tulip"]["connection_string"] == "https://your-instance.tulip.co"
    assert ci["tulip"]["token"]  # base64 basic token present


class _FakeSecretsClient:
    """Records create/put calls; raises ResourceExistsException on demand."""
    class exceptions:  # noqa: N801 - mirrors botocore client.exceptions shape
        class ResourceExistsException(Exception):
            pass

    def __init__(self, exists=False):
        self._exists = exists
        self.created = []
        self.put = []

    def create_secret(self, Name, SecretString):  # noqa: N803 - boto3 kwarg names
        if self._exists:
            raise self.exceptions.ResourceExistsException()
        self.created.append((Name, SecretString))
        return {"ARN": f"arn:{Name}"}

    def put_secret_value(self, SecretId, SecretString):  # noqa: N803
        self.put.append((SecretId, SecretString))
        return {"ARN": f"arn:{SecretId}"}


class _FakeSession:
    def __init__(self, client):
        self._client = client

    def client(self, name):
        assert name == "secretsmanager"
        return self._client


def _patch_boto(monkeypatch, client):
    import boto3
    monkeypatch.setattr(boto3, "Session", lambda **kw: _FakeSession(client), raising=True)


def test_supply_secret_tulip_creates_secret(monkeypatch):
    import json
    client = _FakeSecretsClient(exists=False)
    _patch_boto(monkeypatch, client)
    ok = smoke.supply_secret_out_of_band(None, "tulip", "basic-token-abc")
    assert ok is True
    assert len(client.created) == 1
    name, payload = client.created[0]
    assert name == "adt/conn/tulip"
    assert json.loads(payload) == {"token": "basic-token-abc", "auth_scheme": "basic",
                                   "connection_origin": "user", "system_id": "tulip"}


def test_supply_secret_tulip_updates_existing_secret(monkeypatch):
    import json
    client = _FakeSecretsClient(exists=True)  # create raises → put path
    _patch_boto(monkeypatch, client)
    ok = smoke.supply_secret_out_of_band(None, "tulip", "basic-token-abc")
    assert ok is True
    assert client.created == []
    assert len(client.put) == 1
    sid_name, payload = client.put[0]
    assert sid_name == "adt/conn/tulip"
    assert json.loads(payload)["auth_scheme"] == "basic"


def test_supply_secret_no_token_is_noop(monkeypatch):
    # No token → no AWS call, returns False (mirrors highbyte).
    called = {"boto": False}
    import boto3
    monkeypatch.setattr(boto3, "Session",
                        lambda **kw: called.__setitem__("boto", True), raising=True)
    assert smoke.supply_secret_out_of_band(None, "tulip", "") is False
    assert called["boto"] is False


# --------------------------------------------------------------------------- #
# Standalone runner (``.venv-local`` ships no pytest).
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import types

    class _MP:
        """Minimal monkeypatch shim for standalone runs."""
        def __init__(self):
            self._undo = []
        def setattr(self, target, name, value=None, raising=True):
            if value is None and not isinstance(target, str):
                raise ValueError("use setattr(obj, name, value)")
            obj, attr = target, name
            old = getattr(obj, attr)
            self._undo.append((obj, attr, old))
            setattr(obj, attr, value)
        def setenv(self, k, v):
            import os
            self._undo.append(("__env__", k, os.environ.get(k)))
            os.environ[k] = v
        def delenv(self, k, raising=True):
            import os
            self._undo.append(("__env__", k, os.environ.get(k)))
            os.environ.pop(k, None)
        def undo(self):
            import os
            for obj, attr, old in reversed(self._undo):
                if obj == "__env__":
                    if old is None:
                        os.environ.pop(attr, None)
                    else:
                        os.environ[attr] = old
                else:
                    setattr(obj, attr, old)
            self._undo.clear()

    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and isinstance(v, types.FunctionType)]
    _failures = 0
    for _t in _tests:
        _mp = _MP()
        try:
            if _t.__code__.co_argcount == 1:
                _t(_mp)
            else:
                _t()
            print(f"PASS  {_t.__name__}")
        except Exception as exc:  # noqa: BLE001
            _failures += 1
            print(f"FAIL  {_t.__name__}: {exc!r}")
        finally:
            _mp.undo()
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
