# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cross-account S3 Tables reads for the DuckDB connector (ADR-0099).

A bucket in another account is reached through the Glue Iceberg REST endpoint
(Lake Formation grants) as the cross-account data role, the same identity the
Athena connector assumes. These pin the config surface, the ATTACH target, and
that assumed-role key material never lands in SQL text.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from agents import duckdb_tools as dt_tools

BUCKET = "arn:aws:s3tables:us-east-1:777788889999:bucket/example-data-lake"
ROLE = "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly"


def test_default_endpoint_is_s3_tables():
    cfg = dt_tools._resolve_config({"table_bucket_arn": BUCKET})
    assert cfg["catalog_endpoint"] == "s3_tables"


def test_glue_endpoint_is_accepted_and_bad_one_rejected():
    cfg = dt_tools._resolve_config({"table_bucket_arn": BUCKET, "catalog_endpoint": "glue"})
    assert cfg["catalog_endpoint"] == "glue"
    with pytest.raises(dt_tools.DuckDBConfigError):
        dt_tools._resolve_config({"table_bucket_arn": BUCKET, "catalog_endpoint": "rest"})


def test_role_arn_and_external_id_survive_resolution():
    cfg = dt_tools._resolve_config({"table_bucket_arn": BUCKET, "role_arn": ROLE,
                                    "external_id": "xid"})
    assert cfg["role_arn"] == ROLE and cfg["external_id"] == "xid"


def test_malformed_role_arn_fails_closed():
    with pytest.raises(dt_tools.DuckDBConfigError):
        dt_tools._resolve_config({"table_bucket_arn": BUCKET, "role_arn": "not-an-arn"})


def test_attach_target_s3_tables_uses_bucket_arn():
    target, endpoint = dt_tools._attach_target({"table_bucket_arn": BUCKET,
                                                "catalog_endpoint": "s3_tables"})
    assert (target, endpoint) == (BUCKET, "s3_tables")


def test_attach_target_glue_uses_federated_s3tables_catalog():
    target, endpoint = dt_tools._attach_target({"table_bucket_arn": BUCKET,
                                                "catalog_endpoint": "glue"})
    assert (target, endpoint) == ("777788889999:s3tablescatalog/example-data-lake", "glue")


def test_ambient_secret_uses_credential_chain():
    sql, params, expiry = dt_tools._secret_statement({"table_bucket_arn": BUCKET})
    assert "credential_chain" in sql and params == ["us-east-1"] and expiry is None


def test_role_secret_keeps_key_material_out_of_sql(monkeypatch):
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    frozen = SimpleNamespace(access_key="AKIAEXAMPLE", secret_key="SECRETEXAMPLE",
                             token="TOKENEXAMPLE")
    creds = SimpleNamespace(get_frozen_credentials=lambda: frozen, _expiry_time=expires)
    seen = {}

    def fake_session_for(cfg, default_region=None, service="adt"):
        seen["cfg"] = cfg
        return SimpleNamespace(get_credentials=lambda: creds)

    monkeypatch.setattr(dt_tools, "_session_for", fake_session_for)
    sql, params, expiry = dt_tools._secret_statement(
        {"table_bucket_arn": BUCKET, "role_arn": ROLE, "external_id": "xid"})
    for value in ("AKIAEXAMPLE", "SECRETEXAMPLE", "TOKENEXAMPLE"):
        assert value not in sql
        assert value in params
    assert expiry == expires
    assert seen["cfg"]["role_arn"] == ROLE and seen["cfg"]["external_id"] == "xid"


def test_reader_reconnects_when_assumed_credentials_near_expiry(monkeypatch):
    opened = []

    class FakeCon:
        def close(self):
            pass

    reader = dt_tools._S3TablesReader({"table_bucket_arn": BUCKET, "role_arn": ROLE})

    def fake_connect():
        opened.append(1)
        return FakeCon(), dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)

    monkeypatch.setattr(reader, "_connect", fake_connect)
    reader._connection()
    reader._connection()  # 2 min left is inside the refresh margin -> reconnect
    assert len(opened) == 2


def test_reader_reuses_connection_with_fresh_credentials(monkeypatch):
    opened = []
    reader = dt_tools._S3TablesReader({"table_bucket_arn": BUCKET, "role_arn": ROLE})

    def fake_connect():
        opened.append(1)
        return object(), dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)

    monkeypatch.setattr(reader, "_connect", fake_connect)
    reader._connection()
    reader._connection()
    assert len(opened) == 1


def test_real_connect_path_runs_without_injected_connection(monkeypatch):
    """The non-test-seam _connect path (the one a deployed agent runs) executes:
    extension dir from env, secret, then ATTACH on the configured endpoint. The
    unit suite otherwise only exercises the injected connection_factory."""
    executed = []

    class FakeCon:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    import duckdb

    monkeypatch.setattr(duckdb, "connect", lambda config=None: FakeCon())
    monkeypatch.setenv("S3T_DUCKDB_EXTENSION_DIR", "/opt/duckdb-ext")
    monkeypatch.setattr(dt_tools, "_secret_statement",
                        lambda cfg: ("CREATE SECRET s3t_secret (TYPE s3, KEY_ID ?)", ["k"], None))
    reader = dt_tools._S3TablesReader(dt_tools._resolve_config(
        {"table_bucket_arn": BUCKET, "catalog_endpoint": "glue"}))
    reader._connect()
    attach = [e for e in executed if e[0].startswith("ATTACH")]
    assert attach == [("ATTACH ? AS s3t (TYPE iceberg, ENDPOINT_TYPE glue, READ_ONLY)",
                       ["777788889999:s3tablescatalog/example-data-lake"])]


def _failing_attach_reader(monkeypatch, attach_fails):
    """A reader on the real _connect path whose ATTACH fails while
    ``attach_fails[0]`` is true. Returns (reader, opened connections)."""
    opened = []
    new_expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)

    class FakeCon:
        def __init__(self):
            self.closed = False
            opened.append(self)

        def execute(self, sql, params=None):
            if sql.startswith("ATTACH") and attach_fails[0]:
                raise RuntimeError("attach refused")

        def close(self):
            self.closed = True

    import duckdb

    monkeypatch.setattr(duckdb, "connect", lambda config=None: FakeCon())
    monkeypatch.setattr(dt_tools, "_secret_statement",
                        lambda cfg: ("CREATE SECRET s (TYPE s3, KEY_ID ?)", ["k"], new_expiry))
    reader = dt_tools._S3TablesReader(dt_tools._resolve_config(
        {"table_bucket_arn": BUCKET, "role_arn": ROLE}))
    return reader, opened, new_expiry


def test_a_failed_refresh_keeps_the_old_expiry_and_closes_the_candidate(monkeypatch):
    attach_fails = [False]
    reader, opened, new_expiry = _failing_attach_reader(monkeypatch, attach_fails)
    reader._acquire()                       # first connection succeeds
    reader._release(reader._con)
    old_con = reader._con
    old_expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
    reader._expiry = old_expiry             # inside the refresh margin: stale
    attach_fails[0] = True

    with pytest.raises(RuntimeError, match="attach refused"):
        reader._acquire()

    assert reader._expiry == old_expiry     # the new expiry was never adopted
    assert reader._con is old_con and old_con.closed is False
    candidate = opened[-1]
    assert candidate is not old_con and candidate.closed is True


def test_the_next_acquire_retries_the_refresh_after_a_failure(monkeypatch):
    attach_fails = [False]
    reader, opened, new_expiry = _failing_attach_reader(monkeypatch, attach_fails)
    reader._acquire()
    reader._release(reader._con)
    old_con = reader._con
    reader._expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
    attach_fails[0] = True
    with pytest.raises(RuntimeError):
        reader._acquire()
    attempts_after_failure = len(opened)

    attach_fails[0] = False
    con = reader._acquire()                 # must retry, not lease the stale one
    assert len(opened) == attempts_after_failure + 1
    assert con is not old_con and reader._expiry == new_expiry
    assert old_con.closed is True           # the idle old generation is retired
