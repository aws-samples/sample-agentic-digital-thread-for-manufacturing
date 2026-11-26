# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the generic Athena connector (agents.athena_tools).

All AWS access is mocked via an injected fake client — no network, no creds.
"""
import json

import pytest

from agents.athena_tools import (
    AthenaConfigError,
    AthenaEngineScanLimitError,
    AthenaQueryTimeoutError,
    AthenaScanBudgetError,
    _coerce_budget,
    _resolve_config,
    _tool_error,
    make_athena_tools,
    resolve_scan_budget,
    run_athena_query,
)

CFG = {"workgroup": "wg", "database": "db", "region": "us-east-1"}


class FakeAthena:
    """Minimal Athena client double recording calls and returning canned data."""

    def __init__(self, states=None, result_set=None, scanned=None):
        self.states = states or ["SUCCEEDED"]
        self._i = 0
        self.result_set = result_set or {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": []}, "Rows": []}}
        self.started = []
        self.stopped = []
        # Optional bytes-scanned per poll: None (no Statistics), an int (constant),
        # or a list indexed by poll number. Drives the FIX B app-level guard tests.
        self.scanned = scanned

    def start_query_execution(self, **kw):
        self.started.append(kw)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        idx = self._i
        state = self.states[min(idx, len(self.states) - 1)]
        self._i += 1
        status = {"State": state}
        if state == "FAILED":
            status["StateChangeReason"] = "boom"
        execution = {"Status": status}
        if self.scanned is not None:
            value = self.scanned[min(idx, len(self.scanned) - 1)] if isinstance(self.scanned, list) else self.scanned
            execution["Statistics"] = {"DataScannedInBytes": value}
        return {"QueryExecution": execution}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)

    def get_query_results(self, **kw):
        return self.result_set


def _result_set(cols, data_rows, with_header=True):
    rows = []
    if with_header:
        rows.append({"Data": [{"VarCharValue": c} for c in cols]})
    for r in data_rows:
        rows.append({"Data": [({"VarCharValue": v} if v is not None else {}) for v in r]})
    return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in cols]}, "Rows": rows}}


# --- config validation ------------------------------------------------------

def test_resolve_config_requires_workgroup():
    with pytest.raises(AthenaConfigError):
        _resolve_config({"database": "db"})


def test_resolve_config_requires_database():
    with pytest.raises(AthenaConfigError):
        _resolve_config({"workgroup": "wg"})


def test_resolve_config_defaults_catalog():
    cfg = _resolve_config({"workgroup": "wg", "schema": "db"})  # 'schema' aliases database
    assert cfg["database"] == "db"
    assert cfg["catalog"] == "AwsDataCatalog"


# --- run_athena_query happy path + guards -----------------------------------

def test_run_query_parses_results_and_drops_header():
    fake = FakeAthena(result_set=_result_set(["a", "b"], [["1", "2"], ["3", None]]))
    out = run_athena_query("SELECT a,b FROM t", CFG, client=fake)
    assert out["columns"] == ["a", "b"]
    assert out["rows"] == [["1", "2"], ["3", None]]
    assert out["row_count"] == 2


def test_run_query_injects_row_cap_into_sql():
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert "LIMIT 500" in fake.started[0]["QueryString"]
    assert fake.started[0]["WorkGroup"] == "wg"
    assert fake.started[0]["QueryExecutionContext"]["Database"] == "db"
    assert fake.started[0]["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"


def test_run_query_rejects_non_select():
    fake = FakeAthena()
    with pytest.raises(ValueError):
        run_athena_query("DROP TABLE t", CFG, client=fake)
    assert fake.started == []  # never reached start_query_execution


def test_run_query_raises_on_failed_state():
    fake = FakeAthena(states=["FAILED"])
    with pytest.raises(RuntimeError, match="boom"):
        run_athena_query("SELECT 1", CFG, client=fake)


def test_run_query_times_out():
    fake = FakeAthena(states=["QUEUED"])
    with pytest.raises(AthenaQueryTimeoutError):
        run_athena_query("SELECT 1", CFG, client=fake, poll_timeout=0.0)
    assert fake.stopped == ["q1"]  # best-effort cancel attempted


def test_run_query_honors_output_location():
    fake = FakeAthena(result_set=_result_set(["a"], []))
    cfg = dict(CFG, output_location="s3://amzn-s3-demo-results/prefix/")
    run_athena_query("SELECT a FROM t", cfg, client=fake)
    assert fake.started[0]["ResultConfiguration"]["OutputLocation"] == "s3://amzn-s3-demo-results/prefix/"


# --- tool factory ------------------------------------------------------------

def test_make_athena_tools_returns_three_named_tools():
    tools = make_athena_tools(CFG)
    names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert names == {"athena_list_tables", "athena_describe_table", "athena_run_query"}


def test_make_athena_tools_validates_eagerly():
    with pytest.raises(AthenaConfigError):
        make_athena_tools({"workgroup": "wg"})  # missing database


# --- FIX A: output_location validation + allowlist (egress control) ----------

def test_output_location_valid_s3_uri_passes():
    cfg = _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-athena/prefix/"))
    assert cfg["output_location"] == "s3://amzn-s3-demo-athena/prefix/"


def test_output_location_bucket_only_passes():
    cfg = _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-athena"))
    assert cfg["output_location"] == "s3://amzn-s3-demo-athena"


def test_output_location_non_s3_scheme_rejected():
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, output_location="https://amzn-s3-demo-athena/prefix/"))


def test_output_location_missing_scheme_rejected():
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, output_location="amzn-s3-demo-athena/prefix/"))


def test_output_location_empty_bucket_rejected():
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, output_location="s3:///prefix/"))


def test_output_location_malformed_bucket_rejected():
    # Uppercase is not a valid S3 bucket character.
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-Bad_Bucket/prefix/"))


def test_omitted_output_location_not_sent_to_start(monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_OUTPUT_ALLOWLIST", raising=False)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", CFG, client=fake)  # CFG has no output_location
    assert "ResultConfiguration" not in fake.started[0]


def test_output_location_allowlist_in_list_passes(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_OUTPUT_ALLOWLIST", "s3://amzn-s3-demo-allowed/,s3://amzn-s3-demo-other/")
    cfg = _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-allowed/results/"))
    assert cfg["output_location"] == "s3://amzn-s3-demo-allowed/results/"


def test_output_location_allowlist_out_of_list_rejected(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_OUTPUT_ALLOWLIST", "s3://amzn-s3-demo-allowed/")
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-elsewhere/results/"))


def test_output_location_allowlist_unset_format_valid_passes(monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_OUTPUT_ALLOWLIST", raising=False)
    cfg = _resolve_config(dict(CFG, output_location="s3://amzn-s3-demo-any/results/"))
    assert cfg["output_location"] == "s3://amzn-s3-demo-any/results/"


# --- FIX B: app-level scanned-bytes guard (cost / DoS) -----------------------

def test_scanned_bytes_over_budget_stops_and_raises(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "1000")
    fake = FakeAthena(states=["RUNNING"], scanned=[5000])
    with pytest.raises(RuntimeError, match="scanned-bytes budget"):
        run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert fake.stopped == ["q1"]  # fail-closed cancel


def test_scanned_bytes_under_budget_completes(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "1000000")
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=500)
    out = run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert out["row_count"] == 1
    assert fake.stopped == []  # never stopped


def test_scanned_bytes_unset_no_cap(monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    # Huge scan, but no budget configured => no stop, completes normally.
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=10**12)
    out = run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert out["row_count"] == 1
    assert fake.stopped == []


def test_scanned_bytes_malformed_env_rejected(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "not-an-int")
    fake = FakeAthena(result_set=_result_set(["a"], []))
    with pytest.raises(AthenaConfigError):
        run_athena_query("SELECT a FROM t", CFG, client=fake)


# --- cross-account wiring: run_athena_query builds its client via the shared helper ---

def test_run_query_builds_client_via_aws_session_helper(monkeypatch):
    """When no client is injected, run_athena_query delegates client creation to
    the generic ``agents.aws_session.boto3_client_for`` helper (NOT boto3.client
    directly), passing the resolved connection_config so optional cross-account
    keys (role_arn/external_id) flow through."""
    import agents.athena_tools as at

    seen = {}

    def fake_helper(service, connection_config, default_region=None):
        seen["service"] = service
        seen["config"] = connection_config
        seen["default_region"] = default_region
        return FakeAthena(result_set=_result_set(["a"], [["1"]]))

    monkeypatch.setattr(at, "boto3_client_for", fake_helper)
    cfg = dict(CFG, role_arn="arn:aws:iam::111122223333:role/x", external_id="e1")
    out = at.run_athena_query("SELECT a FROM t", cfg)
    assert out["row_count"] == 1
    assert seen["service"] == "athena"
    # The resolved config carrying the optional cross-account keys is passed through.
    assert seen["config"]["role_arn"] == "arn:aws:iam::111122223333:role/x"
    assert seen["config"]["external_id"] == "e1"
    assert seen["default_region"] == "us-east-1"


def test_resolve_config_preserves_cross_account_keys():
    cfg = _resolve_config(dict(CFG, role_arn="arn:aws:iam::111122223333:role/x", external_id="e1"))
    assert cfg["role_arn"] == "arn:aws:iam::111122223333:role/x"
    assert cfg["external_id"] == "e1"


def test_resolve_config_cross_account_keys_default_blank():
    cfg = _resolve_config(CFG)
    assert cfg["role_arn"] == ""
    assert cfg["external_id"] == ""


# --- FIX 1: actionable COLUMN_NOT_FOUND / TABLE_NOT_FOUND enrichment ---------
#
# When a query fails because the agent guessed a column/table name, the raw
# Athena error is a dead-end that invites another guess. run_athena_query must
# enrich the error with the REAL available names (a bounded, best-effort
# information_schema lookup) so the agent can self-correct. All GENERIC — the
# fake supplies arbitrary names; nothing is dataset-specific.


class EnrichingFakeAthena:
    """Athena double that keys behavior off the QueryString so a single fake can
    serve BOTH the failing user query AND the follow-up information_schema
    enrichment lookups. The user query returns FAILED with ``fail_reason``; any
    ``information_schema.columns`` query returns ``columns``; any
    ``information_schema.tables`` query returns ``tables``."""

    def __init__(self, fail_reason, *, tables=None, columns=None, lookup_raises=False):
        self.fail_reason = fail_reason
        self.tables = tables or []
        self.columns = columns or []
        self.lookup_raises = lookup_raises
        self._sql_by_qid = {}
        self._n = 0
        self.started = []

    def _is_lookup(self, sql):
        return "information_schema" in sql.lower()

    def start_query_execution(self, **kw):
        self._n += 1
        qid = f"q{self._n}"
        self._sql_by_qid[qid] = kw["QueryString"]
        self.started.append(kw)
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        sql = self._sql_by_qid[QueryExecutionId]
        if self._is_lookup(sql):
            if self.lookup_raises:
                raise RuntimeError("enrichment lookup boom")
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.fail_reason}}}

    def stop_query_execution(self, QueryExecutionId):
        pass

    def get_query_results(self, **kw):
        sql = self._sql_by_qid[kw["QueryExecutionId"]]
        low = sql.lower()
        if "information_schema.columns" in low:
            return _result_set(["column_name"], [[c] for c in self.columns])
        if "information_schema.tables" in low:
            return _result_set(["table_name"], [[t] for t in self.tables])
        return _result_set([], [])


def test_column_not_found_error_is_enriched_with_real_columns():
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 1:8: Column 'guessed_col' cannot be resolved",
        columns=["order_id", "customer_ref", "created_at"],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT guessed_col FROM orders WHERE 1=1", CFG, client=fake)
    msg = str(exc.value)
    # The original error is preserved AND the REAL columns are appended.
    assert "COLUMN_NOT_FOUND" in msg
    assert "order_id" in msg
    assert "customer_ref" in msg
    assert "created_at" in msg
    # The referenced table name from the FROM clause is surfaced.
    assert "orders" in msg
    # Not a self-alias case: the note must not appear.
    assert "alias THIS query defines" not in msg


def test_self_alias_column_not_found_names_the_actual_problem():
    """Observed live: a query defined ``CASE ... END AS bucket`` and then
    referenced ``bucket`` at the same SELECT level — COLUMN_NOT_FOUND three
    times in a row, because the available-columns hint can never contain the
    query's OWN alias. The enrichment must lead with the self-alias note so
    the agent stops retrying the identical mistake."""
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 3:10: Column 'bucket' cannot be resolved",
        columns=["order_id", "created_at"],
    )
    sql = ("SELECT CASE WHEN order_id > 5 THEN 'hi' ELSE 'lo' END AS bucket, "
           "COUNT(*) FROM orders GROUP BY bucket")
    with pytest.raises(RuntimeError) as exc:
        run_athena_query(sql, CFG, client=fake)
    msg = str(exc.value)
    assert "'bucket' is an alias THIS query defines" in msg
    assert "repeat the full expression" in msg
    # The generic available-columns hint still rides along (best-effort).
    assert "order_id" in msg


def test_self_alias_hint_requires_a_matching_alias_definition():
    """A genuinely missing column (no AS <name> in the SQL) must NOT get the
    self-alias note — that would misdirect the fix."""
    from agents.athena_tools import _self_alias_hint
    detail = "COLUMN_NOT_FOUND: line 1:8: Column 'ghost' cannot be resolved"
    assert _self_alias_hint("SELECT ghost FROM orders", detail) == ""
    # qualified form still detected via the last dotted segment
    detail2 = "COLUMN_NOT_FOUND: line 1:8: Column 't.bucket' cannot be resolved"
    assert "alias THIS query defines" in _self_alias_hint(
        "SELECT x AS bucket FROM t GROUP BY t.bucket", detail2)


def test_underscored_variant_of_dotted_column_names_the_real_column():
    """Observed live: an agent normalized a dotted physical column to its
    underscored variant (a name that does not exist) and failed
    COLUMN_NOT_FOUND. The enrichment must name the REAL dotted column exactly
    — deterministic (dots->underscores equality), never fuzzy."""
    from agents.athena_tools import _underscored_variant_hint
    detail = ("COLUMN_NOT_FOUND: line 6:7: Column "
              "'details_quality_defect_date' cannot be resolved")
    hint = _underscored_variant_hint(
        "SELECT details_quality_defect_date FROM t", detail,
        ["order_id", "details.quality.defect_date", "created_at"])
    assert 'the real column is "details.quality.defect_date"' in hint
    assert "never an underscored variant" in hint
    # No dotted counterpart -> no hint (a genuinely wrong name gets the
    # generic available-columns list only).
    assert _underscored_variant_hint(
        "SELECT ghost_col FROM t", 
        "COLUMN_NOT_FOUND: line 1:1: Column 'ghost_col' cannot be resolved",
        ["order_id", "created_at"]) == ""
    # Underscore-named REAL columns never trigger it (dots required).
    assert _underscored_variant_hint(
        "SELECT order_id FROM t",
        "COLUMN_NOT_FOUND: line 1:1: Column 'order_id' cannot be resolved",
        ["order_id"]) == ""


def test_underscored_variant_hint_rides_the_full_enrichment():
    """End-to-end through run_athena_query: the failing user query gets BOTH
    the variant note and the available-columns list."""
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 1:8: Column 'a_b_c' cannot be resolved",
        columns=["a.b.c", "other_col"],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT a_b_c FROM orders", CFG, client=fake)
    msg = str(exc.value)
    assert 'the real column is "a.b.c"' in msg
    assert "available columns" in msg


def test_segment_quoted_real_dotted_column_names_the_quoting_problem():
    """Observed live: the agent referenced a REAL dotted column as
    segment-quoted qualified access ('\"parent\".\"attempt_id\"'); the engine
    reports the dotted name missing while the enrichment lists the same name
    as available — an unresolvable contradiction for the retry loop. The
    quoting hint must say the name was right and the quoting was wrong."""
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 1:15: Column 'parent.attempt_id' cannot be "
        "resolved",
        columns=["cell", "parent.attempt_id"],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query(
            'SELECT "parent"."attempt_id" FROM orders', CFG, client=fake)
    msg = str(exc.value)
    assert 'IS a real column' in msg
    assert 'ONE double-quoted identifier instead: "parent.attempt_id"' in msg


def test_segment_quoting_hint_stays_silent_for_genuinely_missing_names():
    from agents.athena_tools import _segment_quoting_hint
    detail = "COLUMN_NOT_FOUND: line 1:1: Column 'ghost.name' cannot be resolved"
    assert _segment_quoting_hint(detail, ["cell", "other.col"]) == ""
    # non-dotted missing names never trigger it
    detail2 = "COLUMN_NOT_FOUND: line 1:1: Column 'cell' cannot be resolved"
    assert _segment_quoting_hint(detail2, ["cell"]) == ""


def test_segment_quoted_real_dotted_column_names_the_quoting_problem():
    """Observed live: the agent referenced a REAL dotted column as
    segment-quoted qualified access ('\"parent\".\"attempt_id\"'); the engine
    reports the dotted name missing while the enrichment lists the same name
    as available — an unresolvable contradiction for the retry loop. The
    quoting hint must say the name was right and the quoting was wrong."""
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 1:15: Column 'parent.attempt_id' cannot be "
        "resolved",
        columns=["cell", "parent.attempt_id"],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query(
            'SELECT "parent"."attempt_id" FROM orders', CFG, client=fake)
    msg = str(exc.value)
    assert 'IS a real column' in msg
    assert 'ONE double-quoted identifier instead: "parent.attempt_id"' in msg


def test_segment_quoting_hint_stays_silent_for_genuinely_missing_names():
    from agents.athena_tools import _segment_quoting_hint
    detail = "COLUMN_NOT_FOUND: line 1:1: Column 'ghost.name' cannot be resolved"
    assert _segment_quoting_hint(detail, ["cell", "other.col"]) == ""
    # non-dotted missing names never trigger it
    detail2 = "COLUMN_NOT_FOUND: line 1:1: Column 'cell' cannot be resolved"
    assert _segment_quoting_hint(detail2, ["cell"]) == ""


def test_table_not_found_error_is_enriched_with_real_tables():
    fake = EnrichingFakeAthena(
        "TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.db.nope' does not exist",
        tables=["orders", "customers", "line_items"],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT * FROM nope", CFG, client=fake)
    msg = str(exc.value)
    assert "TABLE_NOT_FOUND" in msg
    assert "orders" in msg
    assert "customers" in msg
    assert "line_items" in msg


def test_enrichment_is_best_effort_returns_original_error_on_lookup_failure():
    fake = EnrichingFakeAthena(
        "COLUMN_NOT_FOUND: line 1:8: Column 'guessed_col' cannot be resolved",
        columns=["order_id"],
        lookup_raises=True,  # the information_schema lookup itself blows up
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT guessed_col FROM orders", CFG, client=fake)
    msg = str(exc.value)
    # Original error survives; enrichment silently no-ops (no crash, no columns).
    assert "COLUMN_NOT_FOUND" in msg
    assert "order_id" not in msg


def test_successful_query_is_not_enriched_and_issues_single_execution():
    fake = FakeAthena(result_set=_result_set(["a", "b"], [["1", "2"]]))
    out = run_athena_query("SELECT a,b FROM t", CFG, client=fake)
    assert out["row_count"] == 1
    # A success path must NOT trigger any enrichment lookups.
    assert len(fake.started) == 1


def test_generic_failure_without_column_or_table_marker_is_not_enriched():
    # A failure that is neither COLUMN_NOT_FOUND nor TABLE_NOT_FOUND must not
    # trigger an information_schema lookup — enrichment is scoped to guessable
    # name errors only.
    fake = EnrichingFakeAthena("boom generic failure", columns=["x"], tables=["y"])
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert "boom generic failure" in str(exc.value)
    assert len(fake.started) == 1  # no follow-up lookup query


# --- FIX 2 (RECOVERY): TYPE_MISMATCH + scan/bytes-limit enrichment -----------
#
# Extends the actionable-error pattern generically. A TYPE_MISMATCH gets the
# referenced columns' TYPES + a cast hint; a scan/bytes-limit rejection gets the
# referenced table's PARTITION columns + a filter+LIMIT hint. All GENERIC — the
# fake supplies arbitrary names/types; nothing is dataset-specific.


class TypedEnrichingFakeAthena:
    """Athena double keying off the QueryString: the user query FAILS with
    ``fail_reason``; an ``information_schema.columns`` lookup returns the supplied
    ``(column_name, data_type, extra_info)`` rows so a single fake serves BOTH the
    failing query AND the follow-up type/partition introspection."""

    def __init__(self, fail_reason, *, cols_rows=None):
        self.fail_reason = fail_reason
        self.cols_rows = cols_rows or []
        self._sql_by_qid = {}
        self._n = 0
        self.started = []

    def _is_lookup(self, sql):
        return "information_schema" in sql.lower()

    def start_query_execution(self, **kw):
        self._n += 1
        qid = f"q{self._n}"
        self._sql_by_qid[qid] = kw["QueryString"]
        self.started.append(kw)
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        sql = self._sql_by_qid[QueryExecutionId]
        if self._is_lookup(sql):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.fail_reason}}}

    def stop_query_execution(self, QueryExecutionId):
        pass

    def get_query_results(self, **kw):
        sql = self._sql_by_qid[kw["QueryExecutionId"]].lower()
        if "information_schema.columns" in sql:
            return _result_set(["column_name", "data_type", "extra_info"], self.cols_rows)
        return _result_set([], [])


def test_type_mismatch_error_is_enriched_with_column_types_and_cast_hint():
    fake = TypedEnrichingFakeAthena(
        "TYPE_MISMATCH: line 1:20: '=' cannot be applied to varchar, integer",
        cols_rows=[["order_id", "varchar", ""], ["qty", "integer", ""]],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT order_id FROM orders WHERE qty = order_id", CFG, client=fake)
    msg = str(exc.value)
    assert "TYPE_MISMATCH" in msg
    # Column names + their TYPES are surfaced.
    assert "order_id varchar" in msg
    assert "qty integer" in msg
    # A generic cast-to-compare hint is appended.
    assert "CAST" in msg.upper()


def test_temporal_mismatch_hint_names_the_typed_literal_not_a_varchar_cast():
    """THE dominant live failure (8 of 10): a timestamp column compared to a
    bare 'YYYY-MM-DD' string. The generic advice invited CAST(col AS varchar),
    which compiles but silently defeats partition pruning."""
    fake = TypedEnrichingFakeAthena(
        "TYPE_MISMATCH: line 4:40: Cannot apply operator: timestamp(6) <= varchar(10)",
        cols_rows=[["event_time", "timestamp(6)", ""], ["cell", "varchar", ""]],
    )
    with pytest.raises(RuntimeError) as exc:
        run_athena_query(
            "SELECT cell FROM segs WHERE event_time <= '2026-01-31'", CFG, client=fake)
    msg = str(exc.value)
    assert "event_time timestamp(6)" in msg          # types still surfaced
    assert "TIMESTAMP '" in msg and "DATE '" in msg  # the CORRECT recipe
    assert "half-open" in msg
    assert "defeats partition pruning" in msg
    assert "CAST(col AS varchar)" not in msg, "must not advise the pruning-killing cast"


def test_scan_limit_error_is_enriched_with_partition_columns_and_hint():
    fake = TypedEnrichingFakeAthena(
        "Bytes scanned limit was exceeded",
        cols_rows=[
            ["id", "varchar", ""],
            ["ingest_date", "varchar", "partition key"],
            ["region", "varchar", "partition key"],
        ],
    )
    # An ENGINE scan-limit rejection now surfaces as the structured, retryable
    # scan_budget_exceeded shape (sibling of AthenaScanBudgetError), with the
    # computed partition hint carried on the payload instead of being flattened
    # into a free-text RuntimeError that _tool_error would drop.
    with pytest.raises(AthenaEngineScanLimitError) as exc:
        run_athena_query("SELECT id FROM events", CFG, client=fake)
    payload = exc.value.to_dict()
    assert payload["error_type"] == "scan_budget_exceeded"
    assert payload["retryable"] is True
    assert payload["limit_source"] == "engine"
    hint = payload["partition_hint"]
    # The PARTITION columns are surfaced (not the non-partition 'id').
    assert "ingest_date" in hint
    assert "region" in hint
    assert "partition" in hint.lower()
    assert "limit" in hint.lower()


def test_app_level_scanned_bytes_guard_appends_neutral_narrowing_hint(monkeypatch):
    """The app-level scanned-bytes budget guard (StopQueryExecution) also carries a
    RECOVERY narrowing hint so the agent narrows the scan on retry. A STATIC
    metadata-safe hint (no extra introspection query, and NO partition wording —
    this path has no partition knowledge) — so the fail-closed cancel still
    fires exactly once."""
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "1000")
    fake = FakeAthena(states=["RUNNING"], scanned=[5000])
    with pytest.raises(RuntimeError) as exc:
        run_athena_query("SELECT id FROM events", CFG, client=fake)
    msg = str(exc.value)
    assert "scanned-bytes budget" in msg
    assert "narrow the time range" in msg.lower()
    assert "limit" in msg.lower()
    assert "partition" not in msg.lower()  # metadata-safe: no partition advice
    assert fake.stopped == ["q1"]  # fail-closed cancel fires exactly once


def test_scan_limit_enrichment_best_effort_on_lookup_failure():
    """If the partition introspection itself fails, the structured engine
    scan-limit error still surfaces (per-table hint degrades to the generic
    filter+LIMIT phrasing, never masks the real classification): error_type
    stays scan_budget_exceeded and no column names or raw reason text leak."""

    class _NoLookupFake(TypedEnrichingFakeAthena):
        def get_query_execution(self, QueryExecutionId):
            sql = self._sql_by_qid[QueryExecutionId]
            if self._is_lookup(sql):
                raise RuntimeError("introspection boom")
            return {"QueryExecution": {"Status": {"State": "FAILED",
                                                  "StateChangeReason": self.fail_reason}}}

    fake = _NoLookupFake("Bytes scanned limit was exceeded")
    with pytest.raises(AthenaEngineScanLimitError) as exc:
        run_athena_query("SELECT id FROM events", CFG, client=fake)
    payload = exc.value.to_dict()
    assert payload["error_type"] == "scan_budget_exceeded"
    assert payload["limit_source"] == "engine"
    # Per-table introspection failed, so the hint degrades to the GENERIC
    # filter+LIMIT phrasing (no specific partition column names) — best-effort,
    # never masks the real classification nor leaks the raw engine reason.
    hint = payload["partition_hint"]
    assert "WHERE filter" in hint and "LIMIT" in hint
    assert "ingest_date" not in hint
    assert "Bytes scanned limit was exceeded" not in payload["error"]


# --- Configurable per-system scanned-bytes budget ---------------------------
#
# The scan budget is now per-system configurable via connection_config
# ("max_scanned_bytes"), with the ADT_ATHENA_MAX_SCANNED_BYTES env as a global
# override and a safe default of None (no cap). All GENERIC — nothing here is
# dataset-specific.


def test_coerce_budget_accepts_int_str_and_none():
    assert _coerce_budget(None) is None
    assert _coerce_budget("") is None
    assert _coerce_budget(1000) == 1000
    assert _coerce_budget("2048") == 2048


def test_coerce_budget_rejects_malformed_and_nonpositive():
    with pytest.raises(AthenaConfigError):
        _coerce_budget("not-an-int")
    with pytest.raises(AthenaConfigError):
        _coerce_budget(0)
    with pytest.raises(AthenaConfigError):
        _coerce_budget(-5)
    with pytest.raises(AthenaConfigError):
        _coerce_budget(True)  # bool is rejected explicitly


def test_resolve_config_preserves_max_scanned_bytes():
    cfg = _resolve_config(dict(CFG, max_scanned_bytes="4096"))
    assert cfg["max_scanned_bytes"] == 4096
    # Absent => None (no per-system cap).
    assert _resolve_config(CFG)["max_scanned_bytes"] is None


def test_resolve_scan_budget_precedence(monkeypatch):
    # 1. per-system config wins over env.
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "9999")
    assert resolve_scan_budget({"max_scanned_bytes": 111}) == 111
    # 2. env used when config unset.
    assert resolve_scan_budget({}) == 9999
    # 3. safe default None when neither set.
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    assert resolve_scan_budget({}) is None
    assert resolve_scan_budget(None) is None


def test_config_budget_over_limit_raises_structured(monkeypatch):
    # No env budget — the limit comes purely from the per-system connection_config.
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    cfg = dict(CFG, max_scanned_bytes=1000)
    fake = FakeAthena(states=["RUNNING"], scanned=[5000])
    with pytest.raises(AthenaScanBudgetError) as exc:
        run_athena_query("SELECT a FROM t", cfg, client=fake)
    err = exc.value
    # Structured, actionable fields (not a bare error string).
    assert err.scanned_bytes == 5000
    assert err.budget_bytes == 1000
    assert isinstance(err.guidance, list) and err.guidance
    assert "scanned-bytes budget" in str(err)
    assert fake.stopped == ["q1"]  # fail-closed cancel fires exactly once


def test_config_budget_under_limit_completes(monkeypatch):
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)
    cfg = dict(CFG, max_scanned_bytes=1000000)
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=500)
    out = run_athena_query("SELECT a FROM t", cfg, client=fake)
    assert out["row_count"] == 1
    assert fake.stopped == []


def test_config_budget_overrides_env(monkeypatch):
    # Env is generous but the per-system config is tight => the tight config wins.
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "10000000")
    cfg = dict(CFG, max_scanned_bytes=1000)
    fake = FakeAthena(states=["RUNNING"], scanned=[5000])
    with pytest.raises(AthenaScanBudgetError):
        run_athena_query("SELECT a FROM t", cfg, client=fake)
    assert fake.stopped == ["q1"]


def test_scan_budget_error_to_dict_shape():
    err = AthenaScanBudgetError("boom | narrow the range",
                                scanned_bytes=5000, budget_bytes=1000,
                                guidance=["do X", "do Y"])
    d = err.to_dict()
    assert d["error_type"] == "scan_budget_exceeded"
    assert d["scanned_bytes"] == 5000
    assert d["budget_bytes"] == 1000
    assert d["guidance"] == ["do X", "do Y"]
    assert "partition_hint" not in d  # omitted when empty


def test_tool_error_serializes_budget_structured_and_plain():
    budget_err = AthenaScanBudgetError("over budget", scanned_bytes=2, budget_bytes=1,
                                       guidance=["narrow"])
    out = _tool_error(budget_err)
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["scanned_bytes"] == 2 and out["budget_bytes"] == 1
    # A non-budget error is fixed and never exposes raw exception text.
    plain = _tool_error(RuntimeError("some other failure"))
    assert plain == {"error": "query_unavailable", "error_type": "tool_error",
                     "error_class": "RuntimeError"}


# --- Structured query timeout ----------------------------------------------

def _timeout_clock(monkeypatch):
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("agents.athena_tools.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("agents.athena_tools.time.sleep", lambda _seconds: None)


def test_queued_timeout_is_runtime_error_with_query_metadata(monkeypatch):
    _timeout_clock(monkeypatch)
    fake = FakeAthena(states=["QUEUED"])
    with pytest.raises(AthenaQueryTimeoutError) as exc:
        run_athena_query("SELECT 1", CFG, client=fake, poll_timeout=1.0)
    err = exc.value
    assert isinstance(err, RuntimeError)
    assert err.query_id == "q1"
    assert err.last_state == "QUEUED"
    assert err.scanned_bytes is None
    assert err.statistics == {}
    assert fake.stopped == ["q1"]


def test_running_timeout_retains_observed_scan_statistics(monkeypatch):
    _timeout_clock(monkeypatch)
    fake = FakeAthena(states=["RUNNING"], scanned=[8192])
    with pytest.raises(AthenaQueryTimeoutError) as exc:
        run_athena_query("SELECT 1", CFG, client=fake, poll_timeout=1.0)
    err = exc.value
    assert err.query_id == "q1"
    assert err.last_state == "RUNNING"
    assert err.scanned_bytes == 8192
    assert err.statistics["DataScannedInBytes"] == 8192
    assert fake.stopped == ["q1"]


def test_tool_error_serializes_timeout_without_query_results():
    err = AthenaQueryTimeoutError(
        query_id="query-456",
        last_state="RUNNING",
        statistics={"DataScannedInBytes": 2048, "EngineExecutionTimeInMillis": 50},
        poll_timeout=10.0,
    )
    out = _tool_error(err)
    assert out == {
        "error": str(err),
        "error_type": "query_timeout",
        "query_id": "query-456",
        "last_state": "RUNNING",
        "scanned_bytes": 2048,
        "statistics": {
            "DataScannedInBytes": 2048,
            "EngineExecutionTimeInMillis": 50,
        },
    }
