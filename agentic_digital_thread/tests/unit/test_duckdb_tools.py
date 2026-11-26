# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the generic DuckDB/S3-Tables connector (agents.duckdb_tools).

No live S3 Tables bucket and no AWS creds: the engine's own ``connection_factory``
test seam injects an in-memory DuckDB that already exposes a catalog named ``s3t``.

DATASET-NEUTRALITY: the fixtures deliberately use a schema the shipped seed
does NOT use (namespace ``sensors``, table ``readings`` with a ``reading_value``
column), so these tests prove the connector works on ANY S3 Tables bucket and
cannot secretly depend on our SFC column names. One test additionally builds the
exact SFC-shaped table to confirm the same generic path also serves it.
"""
import json

import duckdb
import pytest

from agents.duckdb_tools import (
    CATALOG,
    DuckDBConfigError,
    _resolve_config,
    _tool_error,
    discover_schema,
    make_duckdb_tools,
)
from agents.sql_safety import MAX_ROWS

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
CFG = {"table_bucket_arn": ARN}


def _call(tool):
    """Return the plain underlying function of a strands @tool object."""
    return tool.__wrapped__


def _neutral_factory():
    """In-memory DuckDB with a catalog named s3t and a NON-SFC schema."""
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.sensors")
        con.execute(
            "CREATE TABLE s3t.sensors.readings("
            "observed_at TIMESTAMPTZ, device VARCHAR, metric VARCHAR, reading_value DOUBLE)"
        )
        con.execute(
            "INSERT INTO s3t.sensors.readings VALUES "
            "(TIMESTAMPTZ '2026-01-01 00:00:00+00','dev-1','temp', 20.5),"
            "(TIMESTAMPTZ '2026-01-01 00:01:00+00','dev-1','temp', 21.0)"
        )
        return con
    return factory


def _sfc_factory():
    """In-memory DuckDB whose table matches ADT's SFC readings schema."""
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.sfc")
        con.execute(
            "CREATE TABLE s3t.sfc.readings("
            "event_time TIMESTAMPTZ, site VARCHAR, area VARCHAR, line VARCHAR,"
            " asset VARCHAR, channel VARCHAR, value DOUBLE, quality VARCHAR)"
        )
        con.execute(
            "INSERT INTO s3t.sfc.readings VALUES "
            "(TIMESTAMPTZ '2026-01-01 00:00:00+00','S','A','L','M','E_S_A_L_n_p',1.5,'GOOD')"
        )
        return con
    return factory


# --- config validation (eager) ----------------------------------------------

def test_resolve_config_requires_table_bucket_arn():
    with pytest.raises(DuckDBConfigError):
        _resolve_config({"namespaces": ["x"]})


def test_resolve_config_defaults_row_cap():
    cfg = _resolve_config(CFG)
    assert cfg["table_bucket_arn"] == ARN
    assert cfg["max_rows"] >= 1


def test_resolve_config_keeps_optional_scope_and_tuning():
    cfg = _resolve_config({**CFG, "namespaces": ["sensors"], "duckdb_threads": 4,
                           "query_timeout_s": 30, "max_rows": 10})
    assert cfg["namespaces"] == ["sensors"]
    assert cfg["duckdb_threads"] == 4
    assert cfg["query_timeout_s"] == 30.0
    assert cfg["max_rows"] == 10


# --- DDB-08: fail-closed config bounds ---------------------------------------

def test_resolve_config_malformed_bucket_arn_rejected():
    with pytest.raises(DuckDBConfigError):
        _resolve_config({"table_bucket_arn": "not-an-arn"})


@pytest.mark.parametrize("bad", [0, -1, -5, True, False, "two", 2.5, float("nan")])
def test_resolve_config_rejects_bad_threads(bad):
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "duckdb_threads": bad})


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "soon", True])
def test_resolve_config_rejects_bad_timeout(bad):
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "query_timeout_s": bad})


@pytest.mark.parametrize("bad", ["2 gigs", "GB", "-2GB", "abc", "5"])
def test_resolve_config_rejects_bad_memory_limit(bad):
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "duckdb_memory_limit": bad})


def test_resolve_config_empty_memory_limit_is_unset_not_error():
    # Empty/falsy means "use the engine default", not a malformed value.
    cfg = _resolve_config({**CFG, "duckdb_memory_limit": ""})
    assert "duckdb_memory_limit" not in cfg


def test_resolve_config_rejects_malformed_scope_entries():
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "tables": ["no_dot_here"]})
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "tables": ["a.b.c"]})  # too many segments


def test_resolve_config_max_rows_zero_is_rejected_not_silently_defaulted():
    # The old `cfg.get("max_rows") or MAX_ROWS` silently turned 0 into the
    # default. 0 (and negatives) must fail closed instead.
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "max_rows": 0})
    with pytest.raises(DuckDBConfigError):
        _resolve_config({**CFG, "max_rows": -3})


def test_resolve_config_max_rows_capped_at_hard_ceiling():
    # An arbitrarily large operator value cannot remove the practical bound.
    cfg = _resolve_config({**CFG, "max_rows": 10_000_000})
    assert cfg["max_rows"] <= 100_000


def test_resolve_config_defaults_are_sane():
    cfg = _resolve_config(CFG)
    assert cfg["max_rows"] == MAX_ROWS  # sql_safety default when unset


def test_make_duckdb_tools_validates_eagerly():
    with pytest.raises(DuckDBConfigError):
        make_duckdb_tools({"namespaces": ["x"]})  # missing table_bucket_arn


def test_make_duckdb_tools_returns_three_named_tools():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert names == {"s3tables_list_tables", "s3tables_describe_table", "s3tables_run_query"}


# --- the three tools, driven against a NON-SFC schema ------------------------

def test_list_tables_on_neutral_schema():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[0])())
    assert out["tables"] == ["sensors.readings"]


def test_describe_table_on_neutral_schema():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[1])("sensors.readings"))
    cols = {c["name"]: c["type"] for c in out["columns"]}
    assert cols["reading_value"] == "DOUBLE"
    assert cols["device"] == "VARCHAR"


def test_describe_table_requires_namespace_dot_table():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[1])("readings"))  # no namespace
    # DuckDBConfigError is a public_errors SAFE_MESSAGE_TYPE: its own message
    # surfaces verbatim (it only quotes the caller's own input back).
    assert "namespace.table" in out["error"]


def test_run_query_reads_rows_on_neutral_schema():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[2])("SELECT reading_value FROM s3t.sensors.readings ORDER BY 1"))
    assert out["row_count"] == 2
    assert [r[0] for r in out["rows"]] == [20.5, 21.0]


def test_run_query_rejects_non_select():
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[2])("DROP TABLE s3t.sensors.readings"))
    assert "error" in out  # ensure_select_only refused it; table still intact


def _many_rows_factory(n):
    """In-memory catalog with ``n`` rows, to exercise the row-cap overflow path."""
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.sensors")
        con.execute("CREATE TABLE s3t.sensors.readings(reading_value DOUBLE)")
        con.execute(f"INSERT INTO s3t.sensors.readings SELECT * FROM range({n})")
        return con
    return factory


def test_run_query_over_cap_is_marked_truncated_not_silently_complete():
    # DDB-03 + DDB-05: an over-cap query must come back with EXACTLY the cap of
    # rows AND an explicit truncated flag — never a silently-complete capped set.
    tools = make_duckdb_tools({**CFG, "max_rows": 3}, connection_factory=_many_rows_factory(10))
    out = json.loads(_call(tools[2])("SELECT reading_value FROM s3t.sensors.readings"))
    assert out["row_count"] == 3
    assert out["truncated"] is True
    assert out["partial_reason"] == "row_limit"


def test_run_query_at_cap_is_complete():
    # Exactly cap rows is a complete result, not a truncation.
    tools = make_duckdb_tools({**CFG, "max_rows": 10}, connection_factory=_many_rows_factory(10))
    out = json.loads(_call(tools[2])("SELECT reading_value FROM s3t.sensors.readings"))
    assert out["row_count"] == 10
    assert not out.get("truncated")


def test_run_query_below_cap_returns_all_rows():
    tools = make_duckdb_tools({**CFG, "max_rows": 100}, connection_factory=_many_rows_factory(4))
    out = json.loads(_call(tools[2])("SELECT reading_value FROM s3t.sensors.readings"))
    assert out["row_count"] == 4
    assert not out.get("truncated")


def test_run_query_explicit_limit_above_cap_still_bounded():
    # A model-supplied LIMIT larger than the cap must not escape the cap.
    tools = make_duckdb_tools({**CFG, "max_rows": 3}, connection_factory=_many_rows_factory(10))
    out = json.loads(_call(tools[2])(
        "SELECT reading_value FROM s3t.sensors.readings LIMIT 9"))
    assert out["row_count"] == 3
    assert out["truncated"] is True


def test_run_query_result_carries_athena_contract_shape():
    # DDB-05: columns, executed SQL, and source metadata ride the result.
    tools = make_duckdb_tools(CFG, source_system="sys-1", connection_factory=_neutral_factory())
    out = json.loads(_call(tools[2])("SELECT reading_value AS v FROM s3t.sensors.readings"))
    assert out["columns"] == ["v"]
    assert out["_adt_executed_query"].lower().startswith("select")
    assert "_adt_source" in out


# --- same generic path also serves the real SFC schema ----------------------

def test_same_tools_serve_the_sfc_schema():
    tools = make_duckdb_tools(CFG, connection_factory=_sfc_factory())
    listed = json.loads(_call(tools[0])())
    assert listed["tables"] == ["sfc.readings"]
    rows = json.loads(_call(tools[2])(
        "SELECT value, quality FROM s3t.sfc.readings"))
    assert rows["row_count"] == 1
    assert rows["rows"][0] == [1.5, "GOOD"]


# --- error mapping ------------------------------------------------------------

def test_tool_error_config_error_surfaces_safe_message():
    # DuckDBConfigError is a SAFE_MESSAGE_TYPE, so public_error returns its own
    # message verbatim (no fixed category substitution).
    payload = _tool_error(DuckDBConfigError("no arn"))
    assert payload["error"] == "no arn"


def test_tool_error_generic_is_categorized_not_raw():
    # A non-safe exception must NOT leak its raw text; public_error gives a
    # fixed category + reference instead.
    payload = _tool_error(RuntimeError("secret-internal-detail-xyz"))
    assert "secret-internal-detail-xyz" not in json.dumps(payload)
    assert "error_category" in payload or "error_ref" in payload


def test_catalog_constant_is_the_attach_alias():
    # The connector no longer imports an external engine, so there is nothing
    # to compare with. The fixtures above ATTACH the catalog as s3t, and the
    # scope check and the reader's ATTACH both use this constant, so it must
    # stay "s3t".
    assert CATALOG == "s3t"


# --- DDB-01/02: fail-closed SQL authorization + scope -------------------------
# A valid DuckDB SELECT can otherwise read local files, HTTP/S3 URLs, secrets,
# or run dynamic SQL via table/scalar functions. authorize_duckdb_sql is the
# fail-closed boundary: SELECT/WITH only, every base relation must be an
# in-scope s3t.<ns>.<table>, no table functions / file / URL / secret / dynamic
# SQL reach. These assert the REJECTIONS (fail closed) and the allowed shapes.

from agents.duckdb_tools import DuckDBAuthorizationError, authorize_duckdb_sql  # noqa: E402

_SCOPE_ANY = ()  # empty scope = whole-bucket grant (documented)
_SCOPE_SENSORS = (("sensors", "readings"),)


def _blocked(sql, scope=_SCOPE_ANY):
    with pytest.raises(DuckDBAuthorizationError):
        authorize_duckdb_sql(sql, scope)


def test_authz_allows_plain_select_on_in_catalog_table():
    authorize_duckdb_sql("SELECT a FROM s3t.sensors.readings WHERE a > 1", _SCOPE_ANY)


def test_authz_allows_cte_and_join_within_catalog():
    authorize_duckdb_sql(
        "WITH x AS (SELECT a FROM s3t.ns.t1) "
        "SELECT x.a FROM x JOIN s3t.ns.t2 USING (a)", _SCOPE_ANY)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_parquet('s3://other-bucket/x.parquet')",
    "SELECT * FROM parquet_scan('s3://other-bucket/x.parquet')",
    "SELECT * FROM read_json('/tmp/x.json')",
    "SELECT * FROM read_text('/etc/hostname')",
    "SELECT * FROM read_blob('/etc/hostname')",
    "SELECT * FROM glob('/**')",
    "SELECT * FROM query('SELECT 1')",
    "SELECT * FROM query_table('s3t.sensors.readings')",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM duckdb_settings()",
])
def test_authz_blocks_table_functions_and_file_url_secret_reads(sql):
    _blocked(sql)


@pytest.mark.parametrize("sql", [
    # DDB-01 residual: data-reading functions in SCALAR position (not as a
    # table function) must also be rejected wherever they appear.
    "SELECT read_text('/etc/hostname')",
    "SELECT read_blob('/etc/hostname')",
    "SELECT (SELECT read_text('/etc/hostname'))",
    "SELECT a FROM s3t.sensors.readings WHERE a = (SELECT read_text('/x'))",
    "SELECT getvariable('x')",
    "WITH e AS (SELECT read_text('/x') AS v) SELECT v FROM e",
])
def test_authz_blocks_scalar_file_url_secret_reads(sql):
    _blocked(sql)


@pytest.mark.parametrize("sql", [
    # Legitimate analytical SQL must NOT be false-positived by the function
    # denylist: ordinary scalar/aggregate/date functions are fine.
    "SELECT a, sum(b) FROM s3t.sensors.readings GROUP BY a",
    "SELECT count(*), max(a) FROM s3t.sensors.readings",
    "SELECT date_trunc('hour', a) FROM s3t.sensors.readings GROUP BY 1",
    "SELECT regexp_extract(device, '[a-z]+', 0) FROM s3t.sensors.readings",
])
def test_authz_allows_ordinary_scalar_and_aggregate_functions(sql):
    authorize_duckdb_sql(sql, _SCOPE_ANY)


@pytest.mark.parametrize("sql", [
    "ATTACH 'other.db' AS o",
    "COPY (SELECT 1) TO '/tmp/out.csv'",
    "PRAGMA database_list",
    "CALL pragma_table_info('x')",
    "INSTALL httpfs",
    "SET s3_region='us-east-1'",
    "DROP TABLE s3t.sensors.readings",
    "INSERT INTO s3t.sensors.readings VALUES (1)",
    "SELECT 1; SELECT 2",
])
def test_authz_blocks_non_select_and_statements(sql):
    _blocked(sql)


def test_authz_blocks_reference_to_other_catalog():
    # A base table outside the s3t catalog (e.g. memory/system catalogs).
    _blocked("SELECT * FROM memory.main.x")
    _blocked("SELECT * FROM system.information_schema.tables")


def test_authz_enforces_namespace_table_scope():
    # In scope: sensors.readings. Out of scope: anything else, even in s3t.
    authorize_duckdb_sql("SELECT a FROM s3t.sensors.readings", _SCOPE_SENSORS)
    _blocked("SELECT a FROM s3t.sensors.other", _SCOPE_SENSORS)
    _blocked("SELECT a FROM s3t.secret_ns.t", _SCOPE_SENSORS)


def test_authz_scope_holds_through_cte_alias_and_subquery():
    # Out-of-scope relation hidden behind a CTE / alias / subquery is still blocked.
    _blocked("WITH x AS (SELECT a FROM s3t.secret_ns.t) SELECT * FROM x", _SCOPE_SENSORS)
    _blocked("SELECT * FROM (SELECT a FROM s3t.secret_ns.t) q", _SCOPE_SENSORS)
    _blocked("SELECT a FROM s3t.sensors.readings r "
             "JOIN s3t.secret_ns.t t USING (a)", _SCOPE_SENSORS)


def test_authz_blocks_unparseable_and_empty():
    _blocked("")
    _blocked("this is not sql")


def test_run_query_rejects_file_read_through_the_tool():
    # End-to-end: the tool wires the authorizer before touching the engine.
    tools = make_duckdb_tools(CFG, connection_factory=_neutral_factory())
    out = json.loads(_call(tools[2])("SELECT * FROM read_csv('/etc/passwd')"))
    assert "error" in out and out.get("row_count") is None


# --- DDB-04: the bucket ARN + scope survive persistence/sanitization ---------

def test_node_config_keys_include_the_duckdb_target():
    # Persistence drops any key not in _NODE_CONFIG_KEYS; the bucket ARN MUST
    # survive or a restored agent cannot rebuild (eager tool construction fails).
    from agents.connection_resolver import _NODE_CONFIG_KEYS
    for key in ("table_bucket_arn", "namespaces", "tables",
                "duckdb_memory_limit", "duckdb_threads", "query_timeout_s", "max_rows"):
        assert key in _NODE_CONFIG_KEYS, key


def test_sanitize_keeps_bucket_and_scope_drops_nothing_secret():
    from agents.connection_resolver import sanitize_connection_config
    safe = sanitize_connection_config({
        "connection_type": "S3 Tables",
        "table_bucket_arn": ARN,
        "namespaces": ["sensors"],
        "tables": ["sensors.readings"],
    })
    assert safe["table_bucket_arn"] == ARN
    assert safe["namespaces"] == ["sensors"]
    assert safe["tables"] == ["sensors.readings"]


def test_restore_round_trip_rebuilds_tools_from_persisted_config():
    # Simulate restore: persist -> sanitize -> rebuild tools from ONLY the
    # surviving non-secret config. make_duckdb_tools validates eagerly, so if the
    # bucket ARN were dropped this would raise DuckDBConfigError.
    from agents.connection_resolver import sanitize_connection_config
    persisted = sanitize_connection_config({
        "connection_type": "S3 Tables", "table_bucket_arn": ARN,
        "namespaces": ["sensors"], "tables": ["sensors.readings"],
    })
    tools = make_duckdb_tools(persisted, connection_factory=_neutral_factory())
    names = {getattr(t, "tool_name", "") for t in tools}
    assert names == {"s3tables_list_tables", "s3tables_describe_table", "s3tables_run_query"}


# --- discover_schema (onboarding introspection) ------------------------------

def test_discover_schema_shape_on_neutral_schema():
    out = discover_schema(CFG, source_system="sys-1", connection_factory=_neutral_factory())
    # Same contract as onboard_agent._discover_athena_schema.
    assert out["system_id"] == "sys-1"
    assert out["connection_type"] == "S3 Tables"
    assert out["schema_name"] == CATALOG
    names = {t["name"] for t in out["tables"]}
    assert names == {"sensors.readings"}
    cols = {c["name"]: c["data_type"] for c in out["tables"][0]["columns"]}
    assert cols["reading_value"] == "DOUBLE"
    assert "discovered 1 table" in out["ai_analysis"].lower()


def test_discover_schema_also_describes_the_sfc_schema():
    out = discover_schema(CFG, source_system="sys-2", connection_factory=_sfc_factory())
    assert {t["name"] for t in out["tables"]} == {"sfc.readings"}
    cols = {c["name"] for c in out["tables"][0]["columns"]}
    assert {"event_time", "value", "quality", "channel"} <= cols


def test_discover_schema_bad_config_returns_error_shape():
    out = discover_schema({"namespaces": ["x"]})  # no table_bucket_arn
    assert "error" in out and "table_bucket_arn" in out["error"]
