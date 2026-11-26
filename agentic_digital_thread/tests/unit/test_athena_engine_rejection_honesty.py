# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests: Athena ENGINE-REJECTION honesty.

An Athena/Trino rejection means the SYSTEM ANSWERED — the engine was reachable
and evaluated the query, then refused it (too much data to scan, or invalid
SQL). Measured live (2026-10-08): 50 of 62 Athena tool errors were
the engine's "Bytes scanned limit was exceeded", yet the path collapsed every
one to ``query_unavailable`` with the computed partition hint thrown away, and
the agent then told users the system was DOWN (it was up).

These tests pin the honest mapping, built generically with no dataset knowledge:

1. An engine scan-limit FAILED state raises a STRUCTURED error whose ``to_dict``
   returns ``error_type: scan_budget_exceeded`` (pinned taxonomy), retryable,
   a ``limit_source`` marker, the partition hint computed today, and the
   bounded-degradation suggestion — never a bare ``query_unavailable``.
2. A deterministic SQL rejection carrying a known Trino/Athena error NAME from a
   CLOSED allowlist maps to ``error_type: invalid_request`` with the matched
   NAME as a separate field and fixed guidance — the engine is reachable; fix
   the query. COLUMN_NOT_FOUND / TABLE_NOT_FOUND keep schema_mismatch; anything
   else keeps today's ``query_unavailable``.
3. The new fixed strings never contain a transient/auth marker the orchestrator
   would retry on, nor match the chat_client timeout classifier.
4. Every new ``error_type`` stays inside the pinned taxonomy.

Fully offline — no AWS clients; synthetic names only.
"""
from __future__ import annotations

import re

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents.chat_client import _ERROR_TEXT_CLASSIFIERS, classify_error_text
from agents.orchestrator import (
    _AUTH_TOOL_ERROR_MARKERS,
    _TRANSIENT_TOOL_ERROR_MARKERS,
)
from agents.runtime_integrity import _TOOL_ENUMS

_CFG = {"workgroup": "wg", "database": "lake_db", "catalog": "", "region": "us-west-2",
        "output_location": ""}

_PARTITION_WORDS = re.compile(r"partition (column|predicate)", re.IGNORECASE)
_DOWN_WORDS = re.compile(
    r"\b(unavailable|not responding|is down|outage|not reachable)\b", re.IGNORECASE
)


class _StubAthena:
    """Minimal Athena client: stop is a no-op; introspection returns nothing so
    the partition hint degrades to the generic phrasing (no live calls)."""

    def stop_query_execution(self, **kwargs):
        return None


class _FailingAthena:
    """Offline Athena double that drives the PRODUCTION path: the user query
    polls to FAILED with ``reason``; any information_schema enrichment lookup
    succeeds-empty (so _partition_hint_from_tables degrades to the generic
    phrasing without a live call). Used to prove classification through
    ``run_athena_query`` / ``_raise_failed_query`` end to end, not the helper."""

    def __init__(self, reason: str):
        self.reason = reason
        self._sql = {}
        self._n = 0

    def start_query_execution(self, **kw):
        self._n += 1
        qid = f"q{self._n}"
        self._sql[qid] = kw["QueryString"]
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        sql = self._sql[QueryExecutionId].lower()
        if "information_schema" in sql:
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        return {"QueryExecution": {"Status": {
            "State": "FAILED", "StateChangeReason": self.reason}}}

    def stop_query_execution(self, QueryExecutionId):
        return None

    def get_query_results(self, **kw):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": []}, "Rows": []}}


def _tool_error_through_run(reason: str) -> dict:
    """Drive the FULL production path: run a query that polls to FAILED with
    ``reason``, then map the raised exception exactly as the registered tool
    does (``_tool_error``). This is what the Mode A tool returns to the model."""
    try:
        at.run_athena_query("SELECT 1 FROM events", _CFG, client=_FailingAthena(reason))
    except Exception as exc:  # noqa: BLE001 - mirror the tool wrapper
        return at._tool_error(exc)
    raise AssertionError("query did not fail")


# =========================================================================== #
# 1. Engine scan-limit FAILED state -> structured scan_budget_exceeded error
# =========================================================================== #
def test_engine_scan_limit_failed_raises_structured_error():
    """A FAILED state whose reason matches the engine scan-limit markers must
    raise the structured engine-scan-limit error, NOT a plain RuntimeError that
    _tool_error would flatten to query_unavailable."""
    reason = "Bytes scanned limit was exceeded"
    with pytest.raises(at.AthenaEngineScanLimitError) as excinfo:
        at._raise_failed_query(
            _StubAthena(), _CFG, "SELECT count(*) FROM events",
            "FAILED", reason, enrich=True,
            statistics={"DataScannedInBytes": 5_000_000},
        )
    # Still a RuntimeError subclass so existing except-RuntimeError paths hold.
    assert isinstance(excinfo.value, RuntimeError)


def test_engine_scan_limit_to_dict_is_honest_and_structured():
    reason = "Bytes scanned limit was exceeded"
    try:
        at._raise_failed_query(
            _StubAthena(), _CFG, "SELECT count(*) FROM events",
            "FAILED", reason, enrich=True,
            statistics={"DataScannedInBytes": 5_000_000},
        )
    except at.AthenaEngineScanLimitError as e:
        d = e.to_dict()
    assert d["error_type"] == "scan_budget_exceeded"
    assert d["retryable"] is True
    assert d["limit_source"] == "engine"
    assert d["scanned_bytes"] == 5_000_000
    # The bounded-degradation suggestion rides it (advisory only).
    assert d["suggestion"]["strategy"] == "bounded_estimate"
    # A partition hint is carried (generic phrasing when introspection is empty).
    assert d.get("partition_hint")
    # Honesty: the fixed strings must NOT describe the system as down/unavailable.
    text = " ".join(str(v) for v in d.values())
    assert not _DOWN_WORDS.search(text), text


def test_engine_scan_limit_tool_error_passes_structured_dict_through():
    """The guarded tool catches the structured error and _tool_error must
    serialize it via to_dict() — never flatten it to query_unavailable."""
    err = at.AthenaEngineScanLimitError(
        "engine scan limit", limit_source="engine", scanned_bytes=42,
        partition_hint=" | add a WHERE filter and a LIMIT",
    )
    out = at._tool_error(err)
    assert out["error"] != "query_unavailable"
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["limit_source"] == "engine"
    assert out["scanned_bytes"] == 42


def test_engine_scan_limit_scanned_bytes_optional():
    """When the engine reported no DataScannedInBytes, the field is simply
    absent — never a fabricated zero."""
    err = at.AthenaEngineScanLimitError("engine scan limit", limit_source="engine")
    d = err.to_dict()
    assert "scanned_bytes" not in d


# =========================================================================== #
# 2. Deterministic SQL rejection -> invalid_request with matched NAME
# =========================================================================== #
_INVALID_NAMES = (
    "SYNTAX_ERROR",
    "EXPRESSION_NOT_AGGREGATE",
    "FUNCTION_NOT_FOUND",
    "INVALID_FUNCTION_ARGUMENT",
    "TYPE_MISMATCH",
    "INVALID_CAST_ARGUMENT",
    "NOT_SUPPORTED",
    "AMBIGUOUS_NAME",
)


@pytest.mark.parametrize("name", _INVALID_NAMES)
def test_known_trino_error_name_maps_to_invalid_request(name):
    exc = RuntimeError(f"Athena query failed: {name}: the engine rejected this")
    out = at._tool_error(exc)
    assert out["error_type"] == "invalid_request"
    assert out["error_name"] == name
    assert "guidance" in out
    # Honest: the engine is reachable; the SQL is the problem.
    guidance = out["guidance"].lower()
    assert "reachable" in guidance or "rejected the sql" in guidance
    assert "athena_describe_table" in guidance


def test_invalid_request_name_field_is_allowlist_only_never_free_text():
    """The error_name is the matched allowlist token, never the raw engine
    message (which can quote data values). Uses the real Athena grammar
    (``NAME: line L:C: ...``)."""
    exc = RuntimeError(
        "Athena query failed: SYNTAX_ERROR: line 1:8: mismatched input 'FORM' "
        "near column secret_value_12345"
    )
    out = at._tool_error(exc)
    assert out["error_name"] == "SYNTAX_ERROR"
    serialized = " ".join(str(v) for v in out.values())
    assert "secret_value_12345" not in serialized
    assert "FORM" not in serialized


def test_column_not_found_still_maps_to_schema_mismatch():
    exc = RuntimeError("Athena query failed: COLUMN_NOT_FOUND: Column 'x' cannot be resolved")
    out = at._tool_error(exc)
    assert out["error_type"] == "schema_mismatch"


def test_table_not_found_still_maps_to_schema_mismatch():
    exc = RuntimeError("Athena query failed: TABLE_NOT_FOUND: line 1:15: Table does not exist")
    out = at._tool_error(exc)
    assert out["error_type"] == "schema_mismatch"


def test_unknown_failure_still_maps_to_query_unavailable():
    """Everything outside the allowlists keeps today's query_unavailable."""
    out = at._tool_error(RuntimeError("some other failure"))
    assert out == {"error": "query_unavailable", "error_type": "tool_error",
                   "error_class": "RuntimeError"}


def test_generic_exhausted_resources_stays_query_unavailable_through_production_path():
    """REGRESSION GUARD: drive the FULL production path — a FAILED state
    whose reason is generic resource exhaustion ('exhausted resources') is NOT a
    bytes-scanned cutoff, so it must stay query_unavailable, never be reclassified
    as scan_budget_exceeded. Calling _tool_error directly would bypass
    _raise_failed_query and pass for the wrong reason."""
    out = _tool_error_through_run("Query exhausted resources at this scale factor")
    assert out["error"] == "query_unavailable"
    assert out["error_type"] == "tool_error"


# =========================================================================== #
# 3. Fixed strings never trip orchestrator retry / timeout classifiers
# =========================================================================== #
def _all_new_fixed_strings() -> list[str]:
    engine = at.AthenaEngineScanLimitError(
        "engine scan limit", limit_source="engine", scanned_bytes=1,
        partition_hint=" | add a WHERE filter and a LIMIT",
    ).to_dict()
    invalid = at._tool_error(
        RuntimeError("Athena query failed: EXPRESSION_NOT_AGGREGATE: x"))
    strings: list[str] = []
    for payload in (engine, invalid):
        for value in payload.values():
            if isinstance(value, str):
                strings.append(value)
            elif isinstance(value, list):
                strings.extend(str(v) for v in value)
            elif isinstance(value, dict):
                strings.extend(str(v) for v in value.values())
    return strings


def test_new_fixed_strings_avoid_transient_and_auth_markers():
    for text in _all_new_fixed_strings():
        low = text.lower()
        for marker in _TRANSIENT_TOOL_ERROR_MARKERS:
            assert marker not in low, (marker, text)
        for marker in _AUTH_TOOL_ERROR_MARKERS:
            assert marker not in low, (marker, text)


def test_new_fixed_strings_do_not_classify_as_timeout():
    for text in _all_new_fixed_strings():
        assert classify_error_text(text) != "timeout", text
    # Belt-and-suspenders: the raw timeout pattern itself must not match.
    timeout_pattern = dict(_ERROR_TEXT_CLASSIFIERS)["timeout"]
    for text in _all_new_fixed_strings():
        assert not timeout_pattern.search(text), text


# =========================================================================== #
# 4. Taxonomy — the new error_types stay pinned
# =========================================================================== #
def test_engine_rejection_error_types_stay_within_pinned_taxonomy():
    emitted = {
        at.AthenaEngineScanLimitError(
            "x", limit_source="engine").to_dict()["error_type"],
        at._tool_error(
            RuntimeError("Athena query failed: SYNTAX_ERROR: x"))["error_type"],
    }
    assert emitted <= _TOOL_ENUMS["safe_error_category"]


# =========================================================================== #
# 5. retry gate still excludes scan_budget_exceeded (narrow gate preserved)
# =========================================================================== #
def test_engine_scan_limit_is_not_retry_eligible_in_system_agent():
    payload = at.AthenaEngineScanLimitError("x", limit_source="engine").to_dict()
    assert sa._athena_retry_eligible(payload) is False


# =========================================================================== #
# 6. Prompt — one sentence that an engine rejection means the system answered
# =========================================================================== #
def test_athena_prompt_says_engine_rejection_means_system_answered():
    prompt = sa._compose_system_prompt(
        system_id="lake-athena",
        system_name="Lake Athena",
        schema="lake_db",
        system_prompt="You are the Athena agent.",
        tables_desc="",
        connection_type="Athena",
    )
    low = prompt.lower()
    assert "scan_budget_exceeded" in low
    assert "invalid_request" in low
    # It must instruct NOT to describe the system as down/unavailable.
    assert "answered" in low
    assert re.search(r"never (describe|say).{0,40}(unavailable|down|not responding)", low)


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "SiteWise (Direct)"])
def test_engine_rejection_sentence_is_athena_only(connection_type):
    prompt = sa._compose_system_prompt(
        system_id="s", system_name="S", schema="db", system_prompt="x",
        tables_desc="", connection_type=connection_type,
    )
    assert "scan_budget_exceeded or invalid_request" not in prompt


# =========================================================================== #
# Error-NAME matching only at an error-code position
# =========================================================================== #
@pytest.mark.parametrize("name", _INVALID_NAMES + ("MISSING_GROUP_BY",))
def test_name_at_code_position_maps_to_invalid_request(name):
    """Real Athena grammar: the error code leads the reason as ``NAME: ...``
    (optionally after the app's 'Athena query failed: ' prefix)."""
    out = _tool_error_through_run(f"{name}: line 1:8: the engine rejected this")
    assert out["error_type"] == "invalid_request"
    assert out["error_name"] == name


@pytest.mark.parametrize("name", _INVALID_NAMES)
def test_name_inside_quoted_value_does_not_match(name):
    """A quoted row/data value that happens to contain an allowlisted NAME must
    NOT convert an unknown failure into a deterministic SQL rejection. The lead
    error code is a DIFFERENT (non-allowlisted) code."""
    out = _tool_error_through_run(
        f"GENERIC_INTERNAL_ERROR: value '{name}' caused a failure"
    )
    assert out["error"] == "query_unavailable", name
    assert out["error_type"] == "tool_error"


@pytest.mark.parametrize("name", _INVALID_NAMES)
def test_name_inside_larger_identifier_does_not_match(name):
    """A larger identifier that merely CONTAINS an allowlisted NAME as a
    substring (e.g. prefixed/suffixed) is not the engine's error code."""
    out = _tool_error_through_run(f"MY_{name}_EXTENDED: line 1:1: nope")
    assert out["error"] == "query_unavailable", name
    assert out["error_type"] == "tool_error"


def test_name_in_quotes_under_generic_lead_code_stays_query_unavailable():
    """A quoted NOT_SUPPORTED behind a GENERIC_INTERNAL_ERROR lead code must
    stay query_unavailable (a substring match once misread it)."""
    out = _tool_error_through_run(
        "GENERIC_INTERNAL_ERROR: value 'NOT_SUPPORTED' caused failure"
    )
    assert out["error"] == "query_unavailable"
    assert "error_name" not in out


# =========================================================================== #
# Scan-limit only on an explicit bytes-cutoff diagnostic,
#                schema NAMEs win first, generic exhaustion stays as-is.
# =========================================================================== #
def test_explicit_bytes_scanned_cutoff_classifies_as_engine_scan_limit():
    out = _tool_error_through_run("Bytes scanned limit was exceeded")
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["limit_source"] == "engine"


def test_schema_name_wins_over_scan_substring_through_production_path():
    """A real COLUMN_NOT_FOUND whose message happens to quote the words
    'bytes scanned' must stay schema_mismatch — named schema errors are
    classified BEFORE any scan-limit check in _raise_failed_query."""
    out = _tool_error_through_run(
        "COLUMN_NOT_FOUND: line 1:8: Column 'bytes scanned' cannot be resolved"
    )
    assert out["error_type"] == "schema_mismatch"


def test_table_not_found_wins_over_scan_substring_through_production_path():
    out = _tool_error_through_run(
        "TABLE_NOT_FOUND: line 1:15: Table 'scan limit' does not exist"
    )
    assert out["error_type"] == "schema_mismatch"


@pytest.mark.parametrize("reason", [
    "Query exhausted resources at this scale factor",
    "EXHAUSTED_RESOURCES: not enough memory for the hash join",
    "Encountered too many errors talking to a worker node",
])
def test_generic_resource_exhaustion_is_not_a_bytes_cutoff(reason):
    """Generic engine resource exhaustion is NOT a bytes-scanned cutoff — it
    keeps today's query_unavailable mapping, not scan_budget_exceeded."""
    out = _tool_error_through_run(reason)
    assert out["error_type"] != "scan_budget_exceeded", reason
    assert out["error"] == "query_unavailable"


# =========================================================================== #
# Mode B must map the structured errors through _tool_error
# =========================================================================== #
def _mode_b_athena(monkeypatch, *, raises=None, decision=None):
    """Drive the registered Mode B run path (_mode_b_run_query -> _mode_b_athena_query)
    with run_athena_query stubbed to raise ``raises`` and scoping stubbed to an
    unapplied/unpartitioned decision (so the error payload is a straight map)."""
    decision = decision or sa._scope_decision(applied=False, reason="unpartitioned")
    monkeypatch.setattr(sa, "_scope_athena_sql", lambda sql, cfg, **kw: (sql, decision))

    def boom(sql, connection_config, **kw):
        raise raises

    monkeypatch.setattr(at, "run_athena_query", boom)
    return sa._mode_b_run_query(
        "Athena", "SELECT count(*) FROM events", "lake_db", None, _CFG, "",
    )


def test_mode_b_engine_scan_limit_maps_to_structured_payload(monkeypatch):
    """Mode B must not let AthenaEngineScanLimitError escape as an MCP
    tool exception. It must return the structured scan_budget_exceeded payload
    with the partition hint preserved."""
    err = at.AthenaEngineScanLimitError(
        "engine scan limit", limit_source="engine", scanned_bytes=7,
        partition_hint=" | add a WHERE filter on the partition column and a LIMIT",
    )
    out = _mode_b_athena(monkeypatch, raises=err)
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["retryable"] is True
    assert out["limit_source"] == "engine"
    assert out["scanned_bytes"] == 7
    assert "partition" in out["partition_hint"].lower()
    # The scope decision still rides the payload, exactly as today.
    assert out["scope"]["reason"] == "unpartitioned"


def test_mode_b_scan_budget_and_timeout_map_to_structured_payload(monkeypatch):
    budget = at.AthenaScanBudgetError(
        "over budget", scanned_bytes=9, budget_bytes=1, guidance=["narrow"])
    out = _mode_b_athena(monkeypatch, raises=budget)
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["scope"]["reason"] == "unpartitioned"

    timeout = at.AthenaQueryTimeoutError(
        query_id="q", last_state="RUNNING", poll_timeout=1.0)
    out = _mode_b_athena(monkeypatch, raises=timeout)
    assert out["error_type"] == "query_timeout"


def test_mode_b_named_invalid_request_maps_to_structured_payload(monkeypatch):
    """A deterministic named rejection (plain RuntimeError carrying the engine's
    error code) must map to invalid_request in Mode B too."""
    err = RuntimeError("Athena query failed: EXPRESSION_NOT_AGGREGATE: line 1:9: nope")
    out = _mode_b_athena(monkeypatch, raises=err)
    assert out["error_type"] == "invalid_request"
    assert out["error_name"] == "EXPRESSION_NOT_AGGREGATE"
    assert out["scope"]["reason"] == "unpartitioned"


def test_mode_b_schema_error_keeps_live_schema_enrichment(monkeypatch):
    """Schema-resolution errors keep the existing _mode_b_enrich_schema_error
    path (live schema hint), NOT the fixed _tool_error schema_mismatch guidance."""
    monkeypatch.setattr(
        sa, "_mode_b_describe_schema",
        lambda *a, **kw: "events(id varchar, ts timestamp)")
    err = RuntimeError("COLUMN_NOT_FOUND: line 1:8: Column 'x' cannot be resolved")
    out = _mode_b_athena(monkeypatch, raises=err)
    assert out["error_type"] == "schema_mismatch"
    assert out["schema_hint"] == "events(id varchar, ts timestamp)"


def test_mode_b_unknown_error_still_raises(monkeypatch):
    """Anything outside the mapped classes keeps today's behaviour — it
    propagates (the MCP wrapper reports a tool failure), unchanged."""
    with pytest.raises(RuntimeError, match="some other failure"):
        _mode_b_athena(monkeypatch, raises=RuntimeError("some other failure"))


# =========================================================================== #
# Anchored wrapper parsing (shared _leading_engine_code)
# =========================================================================== #
_NONSUCCESS_STATES = sorted(s for s in at._DONE if s != "SUCCEEDED")


@pytest.mark.parametrize("state", _NONSUCCESS_STATES)
@pytest.mark.parametrize("name", _INVALID_NAMES + ("MISSING_GROUP_BY",))
def test_every_nonsuccess_state_wrapper_is_parsed(state, name):
    """_raise_failed_query emits 'Athena query {state}: ...' for EVERY
    non-success _DONE state. The parser must recognize each wrapper, so a named
    rejection behind 'failed:' AND behind 'cancelled:' both map to invalid_request."""
    wrapped = f"Athena query {state.lower()}: {name}: line 1:1: nope"
    out = at._tool_error(RuntimeError(wrapped))
    assert out["error_type"] == "invalid_request", wrapped
    assert out["error_name"] == name


def test_embedded_complete_wrapper_sequence_does_not_match():
    """A COMPLETE 'Athena query failed: SYNTAX_ERROR:' sequence embedded LATER in
    another message must NOT match — the parser is anchored at the string start."""
    exc = RuntimeError(
        "wrapper: 'Athena query failed: SYNTAX_ERROR:' was a data value"
    )
    assert at._invalid_request_name(str(exc)) == ""
    out = at._tool_error(exc)
    assert out["error"] == "query_unavailable"
    assert "error_name" not in out


def test_cancelled_wrapper_named_rejection_maps_to_invalid_request():
    """'Athena query cancelled: SYNTAX_ERROR: ...' returns '' from a
    find('failed:') parser; the anchored parser handles it."""
    assert at._invalid_request_name(
        "Athena query cancelled: SYNTAX_ERROR: line 1:1: bad") == "SYNTAX_ERROR"


def test_arbitrary_mode_b_exception_string_does_not_match():
    """An arbitrary Mode B exception string (no leading engine code) is never a
    named rejection."""
    for text in (
        "connection reset by peer",
        "An error occurred (ThrottlingException) when calling StartQueryExecution",
        "'SYNTAX_ERROR' appeared as a quoted word mid sentence",
        "boto core endpoint resolution failed",
    ):
        assert at._invalid_request_name(text) == "", text


def test_leading_engine_code_shared_helper_is_anchored():
    assert at._leading_engine_code("SYNTAX_ERROR: x") == "SYNTAX_ERROR"
    assert at._leading_engine_code("Athena query failed: TYPE_MISMATCH: x") == "TYPE_MISMATCH"
    assert at._leading_engine_code("Athena query cancelled: NOT_SUPPORTED: x") == "NOT_SUPPORTED"
    # Not anchored / not a code position:
    assert at._leading_engine_code("prefix NOT_SUPPORTED: x") == ""
    assert at._leading_engine_code("value 'SYNTAX_ERROR:' here") == ""
    assert at._leading_engine_code("Bytes scanned limit was exceeded") == ""


# =========================================================================== #
# Scan limit recognised STRUCTURALLY, never a quoted phrase
# =========================================================================== #
def test_quoted_scan_phrase_under_generic_code_stays_query_unavailable():
    """A quoted 'bytes scanned limit' value behind a different
    leading engine code must NOT classify as a bytes-scanned cutoff."""
    for phrase in ("bytes scanned limit", "bytes scanned cutoff"):
        out = _tool_error_through_run(
            f"GENERIC_INTERNAL_ERROR: value '{phrase}' was rejected")
        assert out["error"] == "query_unavailable", phrase
        assert out["error_type"] == "tool_error"


def test_quoted_scan_phrase_under_allowlisted_code_maps_to_that_name():
    """A quoted scan phrase behind an ALLOWLISTED leading code must map to that
    code's invalid_request — the scan classifier must not steal it first."""
    out = _tool_error_through_run(
        "INVALID_FUNCTION_ARGUMENT: value 'bytes scanned limit' was rejected")
    assert out["error_type"] == "invalid_request"
    assert out["error_name"] == "INVALID_FUNCTION_ARGUMENT"


def test_real_bytes_cutoff_wording_maps_to_scan_budget():
    out = _tool_error_through_run("Bytes scanned limit was exceeded")
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["limit_source"] == "engine"


def test_bytes_cutoff_behind_cancellation_prefix_maps_to_scan_budget():
    """The measured wording can arrive behind cancellation prose."""
    for prefix in ("Query cancelled: ", "Query cancelled! : ",
                   "Query exhausted its data scan budget. "):
        out = _tool_error_through_run(f"{prefix}Bytes scanned limit was exceeded")
        assert out["error_type"] == "scan_budget_exceeded", prefix


def test_bytes_scanned_without_exceeded_is_not_a_cutoff():
    """The cutoff shape REQUIRES 'exceeded' — a bare 'bytes scanned limit' phrase
    with no 'exceeded' (and no leading cutoff code) is not a cutoff diagnostic."""
    assert at._is_scan_limit("the bytes scanned limit for this workgroup") is False
    out = _tool_error_through_run("Query note: bytes scanned limit is 10 GB")
    assert out["error_type"] != "scan_budget_exceeded"
    assert out["error"] == "query_unavailable"


def test_is_scan_limit_rejects_leading_foreign_code_even_with_full_shape():
    """Even the full cutoff shape, if it sits behind a DIFFERENT leading engine
    code, is that engine error — not a bytes cutoff."""
    assert at._is_scan_limit(
        "NOT_SUPPORTED: bytes scanned limit was exceeded somewhere") is False


# =========================================================================== #
# The real terminal state is CANCELLED; only the wrapper is case-insensitive
# =========================================================================== #
class _CancelledAthena(_FailingAthena):
    """Same offline double, but the data query terminates as CANCELLED — the
    state a workgroup bytes-scanned cutoff actually reports."""

    def get_query_execution(self, QueryExecutionId):
        sql = self._sql[QueryExecutionId].lower()
        if "information_schema" in sql:
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        return {"QueryExecution": {"Status": {
            "State": "CANCELLED", "StateChangeReason": self.reason}}}


def test_cancelled_bytes_scanned_cutoff_maps_to_scan_budget_exceeded():
    """The observed runtime case: state CANCELLED, reason exactly
    'Bytes scanned limit was exceeded', through run_athena_query."""
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        at.run_athena_query("SELECT 1 FROM events", _CFG,
                            client=_CancelledAthena("Bytes scanned limit was exceeded"))
    out = at._tool_error(info.value)
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["limit_source"] == "engine"
    assert out["retryable"] is True


@pytest.mark.parametrize("prose", ["Query", "Error", "Reason"])
def test_title_case_prose_prefix_is_not_a_leading_engine_code(prose):
    """Engine codes are upper-snake; a title-case 'Word:' prose prefix is not a
    code, so it must not veto a real bytes-scanned cutoff."""
    reason = f"{prose}: Bytes scanned limit was exceeded"
    assert at._leading_engine_code(reason) == ""
    assert at._is_scan_limit(reason) is True


def test_lowercase_code_token_is_not_an_engine_code():
    assert at._leading_engine_code("syntax_error: line 1:1: bad") == ""
    assert at._invalid_request_name("syntax_error: line 1:1: bad") == ""


@pytest.mark.parametrize("wrapper", ["Athena query failed: ", "ATHENA QUERY CANCELLED: ",
                                     "athena Query Failed:  "])
def test_wrapper_words_stay_case_insensitive(wrapper):
    assert at._leading_engine_code(f"{wrapper}SYNTAX_ERROR: line 1:1: bad") == "SYNTAX_ERROR"
