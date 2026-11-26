# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Athena reliability completion — Tasks G/H/J/K.

G: tunable DATA-query poll timeout (config > env > 60.0, fail-closed coercion).
H: tunable INTROSPECTION poll bound (config > env > 10.0) at the
   list_partition_columns / _introspect_columns / enrichment call sites.
J: scanned-bytes headroom telemetry on SUCCESSFUL results (conditional — legacy
   shapes without engine statistics stay byte-identical; budget fields only
   when a budget is configured; never on error paths).
K: ONE transparent auto-retry of a PRE-EXECUTION retryable failure inside the
   guarded tool (bounded jitter, explicit note, narrow eligibility gate).

Every poll bound must be FINITE. inf/nan are
   configuration errors on the config, env AND explicit-override paths — inf
   would make the poll loop unbounded, nan would collapse it to a nonsense
   immediate timeout.

All AWS access is mocked via injected fakes — no network, no creds.
"""
import json

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents.athena_tools import (
    AthenaConfigError,
    AthenaScanBudgetError,
    _coerce_timeout,
    _resolve_config,
    resolve_introspection_timeout,
    resolve_poll_timeout,
    run_athena_query,
)

CFG = {"workgroup": "wg", "database": "db", "region": "us-east-1"}

_POLL_ENV = "ADT_ATHENA_POLL_TIMEOUT_SECONDS"
_INTROSPECTION_ENV = "ADT_ATHENA_INTROSPECTION_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def _clean_timeout_env(monkeypatch):
    monkeypatch.delenv(_POLL_ENV, raising=False)
    monkeypatch.delenv(_INTROSPECTION_ENV, raising=False)
    monkeypatch.delenv("ADT_ATHENA_MAX_SCANNED_BYTES", raising=False)


class FakeAthena:
    """Minimal Athena client double (same conventions as test_athena_tools)."""

    def __init__(self, states=None, result_set=None, scanned=None):
        self.states = states or ["SUCCEEDED"]
        self._i = 0
        self.result_set = result_set or {
            "ResultSet": {"ResultSetMetadata": {"ColumnInfo": []}, "Rows": []}}
        self.started = []
        self.stopped = []
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
            value = (self.scanned[min(idx, len(self.scanned) - 1)]
                     if isinstance(self.scanned, list) else self.scanned)
            execution["Statistics"] = {"DataScannedInBytes": value}
        return {"QueryExecution": execution}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)

    def get_query_results(self, **kw):
        return self.result_set


def _result_set(cols, data_rows):
    rows = [{"Data": [{"VarCharValue": c} for c in cols]}]
    for r in data_rows:
        rows.append({"Data": [{"VarCharValue": v} for v in r]})
    return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in cols]},
                          "Rows": rows}}


def _capture_poll(monkeypatch):
    """Patch _poll_athena_query to record the timeout it was handed."""
    captured = {}

    def fake_poll(athena, query_id, timeout, max_scanned):
        captured["timeout"] = timeout
        return "SUCCEEDED", "", {}

    monkeypatch.setattr(at, "_poll_athena_query", fake_poll)
    return captured


# =========================================================================== #
# Task G — tunable DATA-query poll timeout (config > env > default 60.0)
# =========================================================================== #

def test_poll_timeout_precedence_config_env_default(monkeypatch):
    monkeypatch.setenv(_POLL_ENV, "120")
    assert resolve_poll_timeout({"poll_timeout_seconds": 30}) == 30.0
    assert resolve_poll_timeout({}) == 120.0
    monkeypatch.delenv(_POLL_ENV)
    assert resolve_poll_timeout({}) == 60.0
    assert resolve_poll_timeout(None) == 60.0


@pytest.mark.parametrize("bad", ["abc", "0", "-5", 0, -1.5, True, False])
def test_poll_timeout_malformed_fails_closed(bad):
    with pytest.raises(AthenaConfigError):
        resolve_poll_timeout({"poll_timeout_seconds": bad})


def test_poll_timeout_malformed_env_fails_closed(monkeypatch):
    monkeypatch.setenv(_POLL_ENV, "sixty")
    with pytest.raises(AthenaConfigError):
        resolve_poll_timeout({})


def test_coerce_timeout_blank_and_none_mean_unset():
    assert _coerce_timeout(None, source="k") is None
    assert _coerce_timeout("", source="k") is None
    assert _coerce_timeout("  ", source="k") is None
    assert _coerce_timeout("2.5", source="k") == 2.5


def test_resolve_config_preserves_and_coerces_timeout_keys():
    cfg = _resolve_config(dict(CFG, poll_timeout_seconds="45",
                               introspection_timeout_seconds=4))
    assert cfg["poll_timeout_seconds"] == 45.0
    assert cfg["introspection_timeout_seconds"] == 4.0
    # Unset => None (resolvers then fall through to env/default).
    unset = _resolve_config(CFG)
    assert unset["poll_timeout_seconds"] is None
    assert unset["introspection_timeout_seconds"] is None


def test_resolve_config_malformed_timeout_fails_closed_eagerly():
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, poll_timeout_seconds="1h"))
    with pytest.raises(AthenaConfigError):
        _resolve_config(dict(CFG, introspection_timeout_seconds=-1))


def test_data_query_uses_config_poll_timeout(monkeypatch):
    captured = _capture_poll(monkeypatch)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", dict(CFG, poll_timeout_seconds=123), client=fake)
    assert captured["timeout"] == 123.0


def test_data_query_uses_env_poll_timeout_when_config_unset(monkeypatch):
    monkeypatch.setenv(_POLL_ENV, "90.5")
    captured = _capture_poll(monkeypatch)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert captured["timeout"] == 90.5


def test_data_query_defaults_to_sixty_seconds(monkeypatch):
    captured = _capture_poll(monkeypatch)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", CFG, client=fake)
    assert captured["timeout"] == 60.0


def test_explicit_poll_timeout_argument_still_wins(monkeypatch):
    captured = _capture_poll(monkeypatch)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", dict(CFG, poll_timeout_seconds=123),
                     client=fake, poll_timeout=7.0)
    assert captured["timeout"] == 7.0


def test_config_poll_timeout_does_not_touch_introspection(monkeypatch):
    # The DATA-query knob must never widen the short introspection bound.
    assert resolve_introspection_timeout({"poll_timeout_seconds": 900}) == 10.0


# =========================================================================== #
# Task H — tunable introspection timeout (config > env > default 10.0)
# =========================================================================== #

def test_introspection_timeout_precedence_config_env_default(monkeypatch):
    monkeypatch.setenv(_INTROSPECTION_ENV, "7.5")
    assert resolve_introspection_timeout({"introspection_timeout_seconds": 3}) == 3.0
    assert resolve_introspection_timeout({}) == 7.5
    monkeypatch.delenv(_INTROSPECTION_ENV)
    assert resolve_introspection_timeout({}) == 10.0
    assert resolve_introspection_timeout(None) == 10.0


@pytest.mark.parametrize("bad", ["abc", "0", -3, True])
def test_introspection_timeout_malformed_fails_closed(bad):
    with pytest.raises(AthenaConfigError):
        resolve_introspection_timeout({"introspection_timeout_seconds": bad})


def test_list_partition_columns_uses_introspection_timeout(monkeypatch):
    calls = {}

    def fake_run(sql, cfg, **kw):
        calls["poll_timeout"] = kw.get("poll_timeout")
        return {"columns": ["column_name", "data_type", "extra_info"],
                "rows": [["dt", "date", "partition key"]], "row_count": 1}

    monkeypatch.setattr(at, "run_athena_query", fake_run)
    cfg = dict(CFG, poll_timeout_seconds=500, introspection_timeout_seconds=3.5)
    out = at.list_partition_columns("t_task_h_plumbing", cfg)
    assert out == [{"name": "dt", "data_type": "date"}]
    # The introspection knob (3.5) applies — NOT the data-query knob (500).
    assert calls["poll_timeout"] == 3.5


def test_introspection_env_override_reaches_introspect_columns(monkeypatch):
    monkeypatch.setenv(_INTROSPECTION_ENV, "6")
    calls = {}

    def fake_run(sql, cfg, **kw):
        calls["poll_timeout"] = kw.get("poll_timeout")
        return {"columns": ["column_name", "data_type", "extra_info"],
                "rows": [], "row_count": 0}

    monkeypatch.setattr(at, "run_athena_query", fake_run)
    at._introspect_columns(None, _resolve_config(CFG), "t_task_h_env")
    assert calls["poll_timeout"] == 6.0


def test_introspection_keeps_short_default_when_unconfigured(monkeypatch):
    calls = {}

    def fake_run(sql, cfg, **kw):
        calls["poll_timeout"] = kw.get("poll_timeout")
        return {"columns": [], "rows": [], "row_count": 0}

    monkeypatch.setattr(at, "run_athena_query", fake_run)
    at._first_column_values(None, _resolve_config(CFG), "SELECT table_name FROM x")
    assert calls["poll_timeout"] == 10.0


# =========================================================================== #
# Task J — scanned-bytes headroom telemetry (conditional, success-only)
# =========================================================================== #

def test_success_gains_scanned_bytes_and_budget_headroom():
    cfg = dict(CFG, max_scanned_bytes=1000)
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=250)
    out = run_athena_query("SELECT a FROM t", cfg, client=fake, source_system="lake")
    assert out["scanned_bytes"] == 250
    assert out["budget_bytes"] == 1000
    assert out["budget_used_pct"] == 25
    assert isinstance(out["budget_used_pct"], int)


def test_success_without_budget_gains_only_scanned_bytes():
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=4096)
    out = run_athena_query("SELECT a FROM t", CFG, client=fake, source_system="lake")
    assert out["scanned_bytes"] == 4096
    assert "budget_bytes" not in out
    assert "budget_used_pct" not in out


def test_success_without_statistics_keeps_legacy_shape():
    # A client that reports no Statistics (the legacy/minimal shape) must stay
    # byte-identical — none of the three headroom keys may appear.
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]))
    out = run_athena_query("SELECT a FROM t", CFG, client=fake, source_system="lake")
    assert "scanned_bytes" not in out
    assert "budget_bytes" not in out
    assert "budget_used_pct" not in out


def test_unattributed_call_keeps_exact_legacy_shape_even_with_statistics():
    # Internal (no source_system) reads are pinned elsewhere to the EXACT
    # {columns, rows, row_count} shape — headroom must never leak into them.
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=250)
    out = run_athena_query("SELECT a FROM t", dict(CFG, max_scanned_bytes=1000),
                           client=fake)
    assert set(out) == {"columns", "rows", "row_count"}


def test_budget_headroom_full_usage_is_hundred_percent():
    cfg = dict(CFG, max_scanned_bytes=1000)
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=1000)
    out = run_athena_query("SELECT a FROM t", cfg, client=fake, source_system="lake")
    assert out["budget_used_pct"] == 100


def test_env_budget_also_produces_headroom(monkeypatch):
    monkeypatch.setenv("ADT_ATHENA_MAX_SCANNED_BYTES", "2000")
    fake = FakeAthena(result_set=_result_set(["a"], [["1"]]), scanned=500)
    out = run_athena_query("SELECT a FROM t", CFG, client=fake, source_system="lake")
    assert out["budget_bytes"] == 2000
    assert out["budget_used_pct"] == 25


def test_error_paths_never_gain_budget_used_pct():
    # Scan-budget errors already carry scanned/budget — the headroom PCT field
    # is success-only telemetry and must not leak into the structured error.
    cfg = dict(CFG, max_scanned_bytes=100)
    fake = FakeAthena(states=["RUNNING"], scanned=[5000])
    with pytest.raises(AthenaScanBudgetError) as exc:
        run_athena_query("SELECT a FROM t", cfg, client=fake)
    assert "budget_used_pct" not in exc.value.to_dict()


def test_attach_headroom_ignores_non_numeric_scanned():
    result = {"rows": [], "row_count": 0}
    out = at._attach_headroom(result, {"DataScannedInBytes": "weird"}, 100)
    assert out == result
    out = at._attach_headroom(result, {}, 100)
    assert out == result


# =========================================================================== #
# Task K — bounded transparent auto-retry inside the guarded tool
# =========================================================================== #

_SCOPE_OK = dict(applied=False, reason="verified", table="", partition_columns=[])


def _retryable_decision():
    return sa._scope_decision(applied=False, reason="scope_required",
                              table="events", partition_metadata="unknown")


def _install_scope_sequence(monkeypatch, decisions):
    """_scope_athena_sql stub yielding one (sql, decision) per attempt."""
    remaining = list(decisions)
    calls = []

    def fake_scope(sql, cfg, baked_partition_columns=None):
        calls.append(sql)
        return sql, remaining.pop(0)

    monkeypatch.setattr(sa, "_scope_athena_sql", fake_scope)
    return calls


def _install_jitter_capture(monkeypatch):
    sleeps = []
    uniform_args = []

    def fake_uniform(a, b):
        uniform_args.append((a, b))
        return 0.75

    monkeypatch.setattr(sa.random, "uniform", fake_uniform)
    monkeypatch.setattr(sa.time, "sleep", lambda s: sleeps.append(s))
    return sleeps, uniform_args


def test_retryable_scope_failure_retries_once_with_note(monkeypatch):
    calls = _install_scope_sequence(
        monkeypatch, [_retryable_decision(), dict(_SCOPE_OK)])
    monkeypatch.setattr(at, "run_athena_query",
                        lambda sql, cfg, **kw: {"columns": ["c"], "rows": [["1"]],
                                                "row_count": 1})
    sleeps, uniform_args = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM events"))

    assert len(calls) == 2                      # exactly ONE retry
    assert sleeps == [0.75]                     # one jittered pause
    assert uniform_args == [(0.5, 1.5)]         # jitter bounds pinned
    assert out["row_count"] == 1
    assert out["note"] == "retried once after a transient failure"


def test_retry_note_appends_to_existing_scope_note(monkeypatch):
    applied = sa._scope_decision(applied=True, reason="auto_scoped",
                                 table="events", partition_columns=["dt"])
    _install_scope_sequence(monkeypatch, [_retryable_decision(), applied])
    monkeypatch.setattr(at, "run_athena_query",
                        lambda sql, cfg, **kw: {"columns": [], "rows": [],
                                                "row_count": 0})
    _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT count(*) FROM events"))

    assert "auto-scoped" in out["note"]
    assert out["note"].endswith("; retried once after a transient failure")


def test_second_retryable_failure_returned_unchanged_no_third_attempt(monkeypatch):
    calls = _install_scope_sequence(
        monkeypatch, [_retryable_decision(), _retryable_decision()])
    sleeps, _ = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM events"))

    assert len(calls) == 2                      # never a third attempt
    assert len(sleeps) == 1
    assert out["error_type"] == "scope_required"
    assert out["retryable"] is True             # returned unchanged
    assert "note" not in out


def test_non_retryable_scope_error_never_retried(monkeypatch):
    known_unpartitioned = sa._scope_decision(
        applied=False, reason="scope_required", table="plain",
        partition_metadata="known_unpartitioned")
    calls = _install_scope_sequence(monkeypatch, [known_unpartitioned])
    sleeps, _ = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM plain"))

    assert len(calls) == 1
    assert sleeps == []
    assert out["error_type"] == "scope_required"
    assert "retryable" not in out


def test_scan_budget_error_never_retried(monkeypatch):
    # retryable:true on the payload, but it fires MID-EXECUTION and is
    # deterministic for the same SQL — the narrow gate must not retry it.
    calls = _install_scope_sequence(monkeypatch, [dict(_SCOPE_OK)])
    run_calls = []

    def budget_blown(sql, cfg, **kw):
        run_calls.append(sql)
        raise AthenaScanBudgetError("over budget", scanned_bytes=999,
                                    budget_bytes=100, guidance=["narrow"])

    monkeypatch.setattr(at, "run_athena_query", budget_blown)
    sleeps, _ = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM events"))

    assert len(calls) == 1 and len(run_calls) == 1
    assert sleeps == []
    assert out["error_type"] == "scan_budget_exceeded"
    assert out["retryable"] is True


def test_generic_tool_error_never_retried(monkeypatch):
    calls = _install_scope_sequence(monkeypatch, [dict(_SCOPE_OK)])

    def boom(sql, cfg, **kw):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(at, "run_athena_query", boom)
    sleeps, _ = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM events"))

    assert len(calls) == 1
    assert sleeps == []
    assert out["error_type"] == "tool_error"


def test_first_attempt_success_has_no_retry_note(monkeypatch):
    _install_scope_sequence(monkeypatch, [dict(_SCOPE_OK)])
    monkeypatch.setattr(at, "run_athena_query",
                        lambda sql, cfg, **kw: {"columns": [], "rows": [],
                                                "row_count": 0})
    sleeps, _ = _install_jitter_capture(monkeypatch)
    guarded = sa._make_scoped_athena_run_query(CFG)

    out = json.loads(guarded._tool_func(sql="SELECT c FROM events"))

    assert sleeps == []
    assert "note" not in out


def test_retry_eligibility_gate_is_narrow():
    assert sa._athena_retry_eligible(
        {"error": "x", "retryable": True, "error_type": "scope_required"})
    # Success payloads, budget errors, and unlabeled errors never pass.
    assert not sa._athena_retry_eligible({"rows": [], "row_count": 0})
    assert not sa._athena_retry_eligible(
        {"error": "x", "retryable": True, "error_type": "scan_budget_exceeded"})
    assert not sa._athena_retry_eligible(
        {"error": "x", "error_type": "scope_required"})
    assert not sa._athena_retry_eligible(
        {"error": "x", "retryable": "yes", "error_type": "scope_required"})


# =========================================================================== #
# NON-FINITE poll bounds are a configuration error, never a timeout
# =========================================================================== #
# float() parses "inf"/"Infinity"/"nan" happily, and _poll_athena_query builds
# its deadline as ``time.monotonic() + timeout``:
#   * inf  => ``monotonic() < deadline`` is permanently true — an UNBOUNDED poll
#             loop that outlives the request and defeats the whole timeout knob;
#   * nan  => every comparison is false — the poll collapses to an immediate
#             timeout that reports a nonsense bound.
# Both must fail closed at coercion time, on EVERY path (config, env, explicit
# override).

_NON_FINITE = ["inf", "+inf", "-inf", "Infinity", "-Infinity", "INF", "nan",
               "NaN", "-nan", "  inf  ",
               float("inf"), float("-inf"), float("nan")]


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_coerce_timeout_rejects_non_finite(bad):
    with pytest.raises(AthenaConfigError, match="finite"):
        _coerce_timeout(bad, source="k")


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_coerce_timeout_rejects_non_finite_even_without_positivity(bad):
    """The relaxed (explicit-override) mode drops POSITIVITY, never finiteness."""
    with pytest.raises(AthenaConfigError, match="finite"):
        _coerce_timeout(bad, source="poll_timeout", require_positive=False)


@pytest.mark.parametrize("good", [2.5, 0.25, 1, 60, "0.5", "60", "1e2"])
def test_coerce_timeout_accepts_finite_values(good):
    assert _coerce_timeout(good, source="k") == float(good)


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_poll_timeout_config_rejects_non_finite(bad):
    with pytest.raises(AthenaConfigError, match="finite"):
        resolve_poll_timeout({"poll_timeout_seconds": bad})


@pytest.mark.parametrize("bad", ["inf", "-inf", "Infinity", "nan"])
def test_poll_timeout_env_rejects_non_finite(monkeypatch, bad):
    monkeypatch.setenv(_POLL_ENV, bad)
    with pytest.raises(AthenaConfigError, match="finite"):
        resolve_poll_timeout({})


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_introspection_timeout_config_rejects_non_finite(bad):
    with pytest.raises(AthenaConfigError, match="finite"):
        resolve_introspection_timeout({"introspection_timeout_seconds": bad})


@pytest.mark.parametrize("bad", ["inf", "Infinity", "nan"])
def test_introspection_timeout_env_rejects_non_finite(monkeypatch, bad):
    monkeypatch.setenv(_INTROSPECTION_ENV, bad)
    with pytest.raises(AthenaConfigError, match="finite"):
        resolve_introspection_timeout({})


@pytest.mark.parametrize("key", ["poll_timeout_seconds", "introspection_timeout_seconds"])
@pytest.mark.parametrize("bad", ["inf", "nan", float("inf"), float("nan")])
def test_resolve_config_rejects_non_finite_eagerly(key, bad):
    with pytest.raises(AthenaConfigError, match="finite"):
        _resolve_config(dict(CFG, **{key: bad}))


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_explicit_poll_timeout_override_rejects_non_finite(bad):
    """The explicit override BYPASSES resolve_poll_timeout, so it carries its own
    gate — and it fails BEFORE any query is started (nothing to leak/cancel)."""
    fake = FakeAthena(result_set=_result_set(["a"], []))
    with pytest.raises(AthenaConfigError, match="finite"):
        run_athena_query("SELECT a FROM t", CFG, client=fake, poll_timeout=bad)
    assert fake.started == []


def test_explicit_zero_poll_timeout_stays_legal(monkeypatch):
    """Positivity is deliberately NOT enforced on the explicit override: a zero
    deadline is a valid 'poll once then time out' probe (pinned by
    test_athena_tools.test_run_query_times_out)."""
    from agents.athena_tools import AthenaQueryTimeoutError

    fake = FakeAthena(states=["QUEUED"])
    with pytest.raises(AthenaQueryTimeoutError):
        run_athena_query("SELECT 1", CFG, client=fake, poll_timeout=0.0)
    assert fake.stopped == ["q1"]


def test_explicit_finite_poll_timeout_reaches_the_poll_loop(monkeypatch):
    captured = _capture_poll(monkeypatch)
    fake = FakeAthena(result_set=_result_set(["a"], []))
    run_athena_query("SELECT a FROM t", CFG, client=fake, poll_timeout=3.5)
    assert captured["timeout"] == 3.5


# --------------------------------------------------------------------------- #
# Absent-marker boundary hardening: the completeness marker is authoritative
# ONLY at a clean entry boundary. Trailing garbage voids it (table stays
# ABSENT -> live discovery still runs), so a corrupted desc can never
# suppress partition discovery.
# --------------------------------------------------------------------------- #
import pytest as _pytest


@_pytest.mark.parametrize("desc,expected", [
    ("plain(a: int) (no partition keys)EVIL", {}),
    ("plain(a: int) (no partition keys) garbage more", {}),
    ("plain(a: int) (no partition keys)x. next(b: int)", {}),
    ("plain(a: int)(no partition keys)", {"plain": []}),
    ("plain(a: int) (no partition keys). next(b: int)", {"plain": []}),
    ("plain(a: int) (no partition keys), other(c: int) (no partition keys)",
     {"plain": [], "other": []}),
    ("plain(a: int) (no partition keys)", {"plain": []}),
])
def test_absent_marker_requires_clean_entry_boundary(desc, expected):
    import agents.system_agent as sa
    assert sa._athena_partition_columns_from_desc(desc) == expected
