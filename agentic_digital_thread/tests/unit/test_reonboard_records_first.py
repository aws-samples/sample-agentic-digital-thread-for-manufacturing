# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the RECORDS-FIRST connection derivation in
scripts/reonboard.py (``records_connection_inputs``, ADR-0044 Decision 3).

Everything here is fully offline: the app REST client and the Secrets Manager
client are stubs (no boto3 construction, no HTTP, no live AWS), mirroring the
DI-seam stub style of ``test_reonboard_lib.py``.

Coverage (task acceptance):
  * a usable persisted record (host+dbname+secret_arn) yields a DSN built from
    the RECORD host — a poison env proves env is NOT consulted;
  * record absent/unconfigured → per-system env fallback + the LOUD
    "fallback to env" log line (caplog);
  * describe_connection erroring → same loud per-system fallback;
  * usable record + operator-side get_secret_value DENIED → ``ReonboardError``
    naming the secret ARN and ``secretsmanager:GetSecretValue`` — NO env
    fallback (``--from-env`` is the escape hatch);
  * missing BOTH (record unusable AND env empty) → ``reonboard_systems`` raises
    ``ReonboardError`` (never a silent "done");
  * secret VALUES never appear in any log line (caplog text);
  * non-SQL types keep their existing env derivation;
  * ``--from-env`` / injected ``connection_inputs`` preserves the pure-env path;
  * ``reonboard_systems`` defaults to the records-first resolver (DI intact).

Run:
  * pytest: pytest agentic_digital_thread/tests/unit/test_reonboard_records_first.py -q
"""
import json
import logging
import sys
from pathlib import Path

# repo-root scripts/ (for the helpers under test) — same insertion as
# test_reonboard_lib.py.
_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import reonboard  # noqa: E402
from _reonboard_lib import ReonboardError  # noqa: E402
from reonboard import DESCRIBE_CONNECTION_ROUTE, records_connection_inputs  # noqa: E402

SECRET_ARN = ("arn:aws:secretsmanager:us-east-1:111122223333:"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
              "secret:adt/db-AbCdEf")
SECRET_PASSWORD = "s3cr3t-VALUE"  # noqa: S105 — offline stub credential  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture

# A poison env: if the records path ever consults it, the derived DSN would
# carry this host and the assertions below fail.
POISON_ENV = {
    "POSTGRES_HOST": "poison-env-host",
    "POSTGRES_PORT": "9999",
    "POSTGRES_DB": "poison_db",
    "POSTGRES_USER": "poison_user",
    "POSTGRES_PASSWORD": "poison-pw",
    "ERP_POSTGRES_HOST": "poison-erp-host",
    "ERP_POSTGRES_PASSWORD": "poison-erp-pw",
}


# --------------------------------------------------------------------------- #
# Fakes (DI seams — no boto3, no HTTP)
# --------------------------------------------------------------------------- #
class _FakeApp:
    """REST-client stub: ``catalog`` + ``call`` serving describe_connection records."""

    def __init__(self, catalog=None, records=None, describe_error=None):
        self._catalog = dict(catalog or {})
        self._records = dict(records or {})
        self._describe_error = describe_error
        self.calls: list[tuple] = []

    def catalog(self):
        return dict(self._catalog)

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        prefix = DESCRIBE_CONNECTION_ROUTE.format(system_id="")
        if path.startswith(prefix):
            if self._describe_error is not None:
                raise self._describe_error
            sid = path[len(prefix):]
            return dict(self._records.get(sid, {}))
        return {}


class _FakeSecrets:
    """Secrets Manager stub: get_secret_value keyed by ARN."""

    def __init__(self, secrets=None, error=None):
        self._secrets = dict(secrets or {})
        self._error = error
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 — boto3 API shape
        self.requested.append(SecretId)
        if self._error is not None:
            raise self._error
        return {"SecretString": json.dumps(self._secrets[SecretId])}


def _pg_record(host="record-host.example", dbname="adt_demo",
               search_path="cmms_maintenance", secret_arn=SECRET_ARN, port=5432):
    return {"connection_type": "PostgreSQL", "host": host, "port": port,
            "dbname": dbname, "search_path": search_path, "secret_arn": secret_arn}


def _catalog(sid="cmms", connection_type="PostgreSQL", schema="cmms_maintenance"):
    return {sid: {"name": sid, "connection_type": connection_type, "schema": schema}}


# --------------------------------------------------------------------------- #
# Records path — the record wins; env is NEVER consulted
# --------------------------------------------------------------------------- #
def test_usable_record_derives_dsn_from_record_not_env():
    app = _FakeApp(records={"cmms": _pg_record()})
    sm = _FakeSecrets({SECRET_ARN: {"username": "adt_admin", "password": SECRET_PASSWORD}})
    out = records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                    catalog_meta=_catalog(), log=lambda _m: None)
    dsn = out["cmms"]["connection_string"]
    assert "record-host.example" in dsn
    assert "poison" not in dsn  # env NOT consulted for any component
    assert dsn.startswith("postgresql://adt_admin:")
    assert dsn.endswith("@record-host.example:5432/adt_demo"
                        "?options=-csearch_path%3Dcmms_maintenance")
    assert out["cmms"]["source"] == "record"
    assert sm.requested == [SECRET_ARN]
    # The record was read via the exact admin describe_connection route.
    assert ("GET", DESCRIBE_CONNECTION_ROUTE.format(system_id="cmms"), None) in app.calls


def test_record_dsn_quotes_userinfo():
    app = _FakeApp(records={"cmms": _pg_record()})
    sm = _FakeSecrets({SECRET_ARN: {"username": "u@ser", "password": "p:a/s@s"}})
    out = records_connection_inputs(app, ["cmms"], sm, {}, catalog_meta=_catalog(),
                                    log=lambda _m: None)
    dsn = out["cmms"]["connection_string"]
    # Mirrors _pg_dsn_from_env: userinfo is percent-quoted (safe="").
    assert "u%40ser:p%3Aa%2Fs%40s@record-host.example" in dsn


def test_record_missing_search_path_falls_back_to_catalog_schema():
    rec = _pg_record(search_path="")
    app = _FakeApp(records={"cmms": rec})
    sm = _FakeSecrets({SECRET_ARN: {"username": "u", "password": "p"}})
    out = records_connection_inputs(app, ["cmms"], sm, {}, catalog_meta=_catalog(),
                                    log=lambda _m: None)
    assert out["cmms"]["connection_string"].endswith(
        "?options=-csearch_path%3Dcmms_maintenance")


# --------------------------------------------------------------------------- #
# LOUD per-system env fallback
# --------------------------------------------------------------------------- #
def test_absent_record_falls_back_to_env_loudly(caplog):
    app = _FakeApp(records={})  # describe returns {} — unconfigured/wiped node
    sm = _FakeSecrets()
    with caplog.at_level(logging.WARNING, logger="reonboard"):
        out = records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                        catalog_meta=_catalog(), log=lambda _m: None)
    # Env fallback used for THIS system (the poison env host proves the path).
    assert "poison-env-host" in out["cmms"]["connection_string"]
    assert out["cmms"]["source"] == "env"
    # The LOUD line names the system and the greppable phrase.
    assert any("fallback to env" in r.message and "cmms" in r.message
               for r in caplog.records)
    assert sm.requested == []  # nothing to fetch — no secret_arn served


def test_describe_error_falls_back_to_env_loudly(caplog):
    app = _FakeApp(describe_error=RuntimeError("HTTP 403"))
    sm = _FakeSecrets()
    with caplog.at_level(logging.WARNING, logger="reonboard"):
        out = records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                        catalog_meta=_catalog(), log=lambda _m: None)
    assert "poison-env-host" in out["cmms"]["connection_string"]
    assert any("fallback to env" in r.message for r in caplog.records)


def test_denied_secret_fetch_fails_loud_names_arn_and_iam(caplog):
    """Usable record + operator-side get_secret_value DENIED → ReonboardError
    naming the secret ARN and the required IAM action — NEVER a silent env
    fallback (the F2 incident class: stale laptop env overriding a known-good
    record). --from-env is the documented escape hatch."""
    app = _FakeApp(records={"cmms": _pg_record()})
    sm = _FakeSecrets(error=RuntimeError("AccessDeniedException"))
    with caplog.at_level(logging.WARNING, logger="reonboard"):
        with pytest.raises(ReonboardError) as exc:
            records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                      catalog_meta=_catalog(), log=lambda _m: None)
    msg = str(exc.value)
    assert SECRET_ARN in msg
    assert "secretsmanager:GetSecretValue" in msg
    assert "--from-env" in msg
    assert "cmms" in msg
    # No env fallback occurred, no loud fallback line, no value leak.
    assert "poison" not in msg
    assert not any("fallback to env" in r.message for r in caplog.records)


def test_secret_without_password_falls_back_loudly(caplog):
    app = _FakeApp(records={"cmms": _pg_record()})
    sm = _FakeSecrets({SECRET_ARN: {"username": "u"}})  # no password — fail-closed
    with caplog.at_level(logging.WARNING, logger="reonboard"):
        out = records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                        catalog_meta=_catalog(), log=lambda _m: None)
    assert out["cmms"]["source"] == "env"
    assert any("fallback to env" in r.message for r in caplog.records)


def test_fallback_is_per_system_not_global(caplog):
    """One wiped node falls back; the sibling with a record stays records-sourced."""
    app = _FakeApp(records={"plm": _pg_record(host="plm-record-host",
                                              search_path="plm_engineering")})
    sm = _FakeSecrets({SECRET_ARN: {"username": "u", "password": "p"}})
    cat = {**_catalog("cmms"), **_catalog("plm", schema="plm_engineering")}
    with caplog.at_level(logging.WARNING, logger="reonboard"):
        out = records_connection_inputs(app, ["cmms", "plm"], sm, POISON_ENV,
                                        catalog_meta=cat, log=lambda _m: None)
    assert out["plm"]["source"] == "record"
    assert "plm-record-host" in out["plm"]["connection_string"]
    assert out["cmms"]["source"] == "env"
    loud = [r.message for r in caplog.records if "fallback to env" in r.message]
    assert len(loud) == 1 and "cmms" in loud[0] and "plm" not in loud[0]


# --------------------------------------------------------------------------- #
# Secret values never appear in logs
# --------------------------------------------------------------------------- #
def test_secret_values_never_logged(caplog):
    seen: list[str] = []
    app = _FakeApp(records={"cmms": _pg_record()})
    sm = _FakeSecrets({SECRET_ARN: {"username": "adt_admin", "password": SECRET_PASSWORD}})
    with caplog.at_level(logging.DEBUG, logger="reonboard"):
        records_connection_inputs(app, ["cmms"], sm, POISON_ENV,
                                  catalog_meta=_catalog(), log=seen.append)
    all_text = " ".join(r.message for r in caplog.records) + " " + " ".join(seen)
    assert SECRET_PASSWORD not in all_text
    assert "poison" not in all_text


# --------------------------------------------------------------------------- #
# Non-SQL types keep their existing derivation
# --------------------------------------------------------------------------- #
def test_non_sql_type_uses_env_derivation():
    calls: list[tuple] = []

    def fake_fallback(cm, systems, env):
        calls.append((tuple(systems),))
        return {"highbyte": {"connection_string": "http://hub/mcp", "token": "tok",
                             "connection_type": "MCP Server", "schema": ""}}

    app = _FakeApp(records={"highbyte": {"connection_type": "MCP Server",
                                         "base_url": "http://hub"}})
    out = records_connection_inputs(
        app, ["highbyte"], _FakeSecrets(), {},
        catalog_meta={"highbyte": {"connection_type": "MCP Server", "schema": ""}},
        fallback=fake_fallback, log=lambda _m: None)
    assert out["highbyte"]["connection_string"] == "http://hub/mcp"
    assert out["highbyte"]["token"] == "tok"
    assert out["highbyte"]["source"] == "env"
    assert calls  # existing derivation invoked


# --------------------------------------------------------------------------- #
# reonboard_systems wiring — records-first default, fail-loud, --from-env intact
# --------------------------------------------------------------------------- #
class _NoopControl:
    """Control-plane stub for wiring tests that never get past connection derivation."""

    def list_agent_runtimes(self):
        return {"agentRuntimeSummaries": []}

    def list_gateway_targets(self, gatewayIdentifier):  # noqa: N803
        return {"items": []}


def test_reonboard_systems_defaults_to_records_first(monkeypatch):
    """With no connection_resolver injected, reonboard_systems derives via
    records_connection_inputs (spied), not the env-only connection_inputs."""
    spied: dict = {}

    def spy(app, systems, sm_client, env, *, catalog_meta=None, fallback=None, log=None):
        spied["systems"] = list(systems)
        spied["sm_client"] = sm_client
        # Empty connection → the fail-loud ReonboardError below proves the
        # result of THIS resolver (not env) drove the decision.
        return {s: {"connection_string": ""} for s in systems}

    monkeypatch.setattr(reonboard, "records_connection_inputs", spy)
    sentinel_sm = object()
    with pytest.raises(ReonboardError) as exc:
        reonboard.reonboard_systems(
            _FakeApp(catalog=_catalog()), _NoopControl(), "gw-1", ["cmms"],
            env=POISON_ENV, sm_client=sentinel_sm,
            poll_s=0, sleep=lambda _s: None, log=lambda _m: None,
        )
    assert spied["systems"] == ["cmms"]
    assert spied["sm_client"] is sentinel_sm  # injected client is threaded through
    assert "cmms" in str(exc.value)


def test_reonboard_systems_missing_both_fails_loud():
    """Record unusable AND env empty → ReonboardError (never a silent 'done')."""
    app = _FakeApp(catalog=_catalog(), records={})
    with pytest.raises(ReonboardError):
        reonboard.reonboard_systems(
            app, _NoopControl(), "gw-1", ["cmms"],
            env={}, sm_client=_FakeSecrets(),
            poll_s=0, sleep=lambda _s: None, log=lambda _m: None,
        )


def test_injected_connection_inputs_preserves_pure_env_path():
    """The --from-env escape hatch (connection_resolver=connection_inputs) derives
    from env only — describe_connection is never called."""
    app = _FakeApp(catalog=_catalog(), records={"cmms": _pg_record()})

    class _StopAfterDerivation(Exception):
        def __init__(self, dsn):
            self.dsn = dsn

    def prov(app_, sid, *, meta, connection_string, token):
        raise _StopAfterDerivation(connection_string)

    with pytest.raises(_StopAfterDerivation) as exc:
        reonboard.reonboard_systems(
            app, _NoopControl(), "gw-1", ["cmms"],
            env=POISON_ENV, provision_fn=prov,
            connection_resolver=reonboard.connection_inputs,
            poll_s=0, sleep=lambda _s: None, log=lambda _m: None,
        )
    assert "poison-env-host" in exc.value.dsn  # pure env derivation
    describe_calls = [c for c in app.calls
                      if c[1].startswith("/api/admin/connection/")]
    assert describe_calls == []  # records path fully bypassed


def test_main_has_from_env_flag(monkeypatch):
    """--from-env parses (the CLI escape hatch exists); missing base-url exits 2
    before any network/boto3 use."""
    monkeypatch.delenv("ADT_BASE_URL", raising=False)
    assert reonboard.main(["--from-env", "--systems", "a", "--gateway-id", "g"]) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
