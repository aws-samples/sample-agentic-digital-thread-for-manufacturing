# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The S3 Tables connector returns the model a sanitized reason when DuckDB
rejects a query (binder/parser/catalog/invalid-input), the way the Athena
path surfaces an engine complaint, instead of a fixed opaque string.

Live finding (parity brief, scope 4): the S3 Tables agent hit
InvalidInputException/BinderException repeatedly on one question and gave up,
because every engine rejection collapsed to "Query against S3 Tables failed"
with no hint of what to fix. These tests prove the connector now names the
engine's own complaint (column not found, syntax error) while never leaking
credentials, file paths, URLs or bucket internals.

No live bucket: the engine's ``connection_factory`` test seam injects an
in-memory DuckDB with a catalog named ``s3t``. Dataset-neutral: the fixture
uses a namespace/table the shipped seed does not use.
"""
import json

import duckdb

from agents.duckdb_tools import make_duckdb_tools, sanitize_engine_error

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.sensors")
        con.execute("CREATE TABLE s3t.sensors.readings(observed_at TIMESTAMP, reading_value DOUBLE)")
        con.execute("INSERT INTO s3t.sensors.readings VALUES (TIMESTAMP '2026-01-01 00:00:00', 1.0)")
        return con
    return factory


def _run(sql):
    tools = make_duckdb_tools({"table_bucket_arn": ARN}, connection_factory=_factory())
    return json.loads(tools[2].__wrapped__(sql))


# --- the sanitizer, in isolation --------------------------------------------

def test_sanitize_names_a_missing_column():
    text = 'Binder Error: Referenced column "nope" not found in FROM clause!'
    out = sanitize_engine_error(text)
    assert out is not None
    assert "column" in out.lower()
    assert "nope" in out


def test_sanitize_names_a_syntax_error():
    out = sanitize_engine_error('Parser Error: syntax error at or near "FORM"')
    assert out is not None
    assert "syntax" in out.lower()


def test_sanitize_returns_none_for_an_unrecognized_message():
    assert sanitize_engine_error("some unrelated runtime failure") is None


def test_sanitize_drops_file_paths_urls_and_secrets():
    text = ('IO Error: Cannot open file "/var/secrets/key.pem" at '
            "https://s3.amazonaws.com/internal-bucket/x; aws_secret_access_key=ABC123")
    out = sanitize_engine_error(text)
    # Either it is not surfaced at all, or the sensitive tokens are stripped.
    if out is not None:
        low = out.lower()
        assert "/var/secrets" not in out
        assert "amazonaws" not in low
        assert "aws_secret" not in low
        assert "internal-bucket" not in low


# --- end to end through the run_query tool ----------------------------------

def test_run_query_surfaces_missing_column():
    out = _run("SELECT does_not_exist FROM s3t.sensors.readings")
    assert "error" in out
    blob = json.dumps(out).lower()
    assert "query against s3 tables failed" not in blob
    assert "does_not_exist" in json.dumps(out)


def test_run_query_surfaces_syntax_error():
    out = _run("SELECT reading_value FORM s3t.sensors.readings")
    assert "error" in out
    assert "query against s3 tables failed" not in json.dumps(out).lower()


def test_run_query_never_leaks_internal_detail_on_a_good_query():
    out = _run("SELECT reading_value FROM s3t.sensors.readings")
    assert "error" not in out
    assert out["rows"] == [[1.0]]


# --- F5: allowlist -- accepted-prefix messages with sensitive payloads ------
# These all START with an accepted SQL-fault prefix (so the old denylist let
# them through) but carry a path, catalog/bucket name, ARN, account id, key or
# URL. The allowlist must NOT surface any of them: a message that is not a
# recognised unknown-identifier / syntax structure gets the fixed opaque
# message (None), and even a recognised one is redacted.

import pytest

_LEAKY = [
    "Binder Error: Cannot open file /root/.aws/config",
    "Binder Error: Cannot open file /opt/duckdb/secrets.db",
    r"Binder Error: cannot read C:\Users\admin\key.pem",
    "Binder Error: relative path ./conf/creds.ini could not be read",
    "Catalog Error: Catalog adt-prod-iceberg does not exist",
    "Catalog Error: Schema s3tablescatalog/example-data-lake not found",
    "Binder Error: arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly denied",
    "Binder Error: account 444455556666 is not authorized",
    "Invalid Input Error: external_id=super-secret-xid rejected",
    "Invalid Input Error: aws_access_key_id=AKIAABCDEFGHIJKLMNOP invalid",
    "Invalid Input Error: aws_session_token=FwoGZXIvYXdzEABCDEF expired",
    "Binder Error: failed to fetch https://s3.amazonaws.com/example-data-lake/x",
    "Catalog Error: httpfs could not open s3://example-data-lake/metadata.json",
]


@pytest.mark.parametrize("msg", _LEAKY)
def test_leaky_accepted_prefix_messages_do_not_surface_sensitive_text(msg):
    out = sanitize_engine_error(msg)
    if out is not None:
        low = out.lower()
        for token in ["/root", "/opt", "c:\\", "./conf", "adt-prod-iceberg",
                      "s3tablescatalog", "example-data-lake", "arn:aws", "444455556666",
                      "super-secret-xid", "akiaabcdefghijklmnop", "fwogzxi", "amazonaws",
                      "s3://", "https://"]:
            assert token not in low, f"{token!r} leaked in {out!r}"


# --- F5: the common binder errors still give a useful, specific reason ------

def test_unknown_column_surfaces_the_submitted_column_name():
    out = sanitize_engine_error(
        'Binder Error: Referenced column "segment_x" not found in FROM clause!')
    assert out is not None
    assert "segment_x" in out
    assert "column" in out.lower()


def test_unknown_table_surfaces_a_table_hint():
    out = sanitize_engine_error(
        'Catalog Error: Table with name "orders_typo" does not exist!')
    assert out is not None
    assert "orders_typo" in out


def test_syntax_error_surfaces_the_token():
    out = sanitize_engine_error('Parser Error: syntax error at or near "FORM"')
    assert out is not None
    assert "syntax" in out.lower()


# --- R3-F5: Invalid Input and Conversion give a fixed, useful, detail-free reason

def test_sanitize_invalid_input_returns_a_fixed_category():
    out = sanitize_engine_error(
        'Invalid Input Error: Could not parse string "xx" according to format specifier "%Y"')
    assert out is not None
    assert "input" in out.lower()
    # Detail-free: the offending string / format is NOT echoed.
    assert "xx" not in out
    assert "%Y" not in out


def test_sanitize_conversion_returns_a_fixed_category():
    out = sanitize_engine_error("Conversion Error: Could not convert string 'abc' to INT32")
    assert out is not None
    assert "conversion" in out.lower() or "convert" in out.lower()
    assert "abc" not in out
    assert "INT32" not in out


def _run_conv(sql):
    # A table with a VARCHAR column so a cast/parse can fail at runtime.
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.sensors")
        con.execute("CREATE TABLE s3t.sensors.vals(v VARCHAR)")
        con.execute("INSERT INTO s3t.sensors.vals VALUES ('abc')")
        return con
    tools = make_duckdb_tools({"table_bucket_arn": ARN}, connection_factory=factory)
    return json.loads(tools[2].__wrapped__(sql))


def test_run_query_conversion_error_gives_the_fixed_reason_end_to_end():
    out = _run_conv("SELECT CAST(v AS INTEGER) FROM s3t.sensors.vals")
    assert "error" in out
    blob = json.dumps(out)
    assert "query against s3 tables failed" not in blob.lower()
    assert "conversion" in blob.lower() or "convert" in blob.lower()
    assert "'abc'" not in blob and "INT32" not in blob  # source text not leaked


def test_run_query_invalid_input_gives_the_fixed_reason_end_to_end():
    out = _run_conv("SELECT strptime(v, '%Y') FROM s3t.sensors.vals")
    assert "error" in out
    blob = json.dumps(out)
    assert "query against s3 tables failed" not in blob.lower()
    assert "input" in blob.lower()
