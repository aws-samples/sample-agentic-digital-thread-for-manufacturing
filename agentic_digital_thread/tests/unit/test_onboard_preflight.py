# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the E1 onboarding preflight core (``agents.onboard_preflight``).

Fully offline — the connection seams (``_resolve``/``_probe``) and the
per-type samplers are monkeypatched; the PostgreSQL sampler tests run against
a fake ``agents.pgdriver.connect`` double. Pins (ADR-0053):

- an ``assume_role_denied`` probe (the ``test_probe_assume_role_preflight.py``
  fixture shape) ⇒ verdict ``blocked`` with ZERO sample reads attempted and
  no ARN echo;
- ≥1 failed table sample read ⇒ ``degraded``; a ``skipped:*``
  physical-columns completeness ⇒ ``degraded`` with the honest warning;
- all probes + sample reads pass ⇒ ``ready``;
- NO sample VALUES in the result — a canary planted in the fake rows never
  reaches the json-dumped payload (rules-engine constraint);
- sample reads run STRICTLY SERIALLY (recorded call order);
- a wholesale listing failure degrades honestly (never a silent ``ready``).
"""
import json
import sys
import time
from pathlib import Path

import pytest

# Make `agents...` importable (agentic_digital_thread/ is two levels up).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard_preflight as op  # noqa: E402

_CANARY = "CANARY-VALUE-9Z"

# The exact detail probe_connection returns for the denied reason (pinned by
# tests/unit/test_probe_assume_role_preflight.py — no ARN, actionable copy).
_DENIED_PROBE = {
    "status": "fail", "reason": "assume_role_denied", "latency_ms": 12,
    "connection_type": "PostgreSQL",
    "detail": (
        "ADT's deploy has not granted sts:AssumeRole for this role (or the "
        "data-account trust/ExternalId rejects it). Redeploy once with "
        "--cross-account-role convention, or add this role ARN to the explicit "
        "grant list. See docs/cross-account-onboarding.md"
        "#dynamic-onboarding-what-must-exist-ahead-of-time-deploy-once-checklist"
    ),
}
_OK_PROBE = {"status": "pass", "reason": "ok", "detail": "SELECT 1 ok",
             "latency_ms": 8, "connection_type": "PostgreSQL"}

_PG_RESOLVED = {"system_id": "erp", "connection_type": "PostgreSQL",
                "connection_string": "postgresql://user@db.example.com:5432/erp",
                "schema": "erp"}


def _far_deadline() -> float:
    return time.monotonic() + 300


# ── fake pgdriver.connect double (for the REAL _pf_postgres sampler) ────────

def _text(query) -> str:
    """The statement text of a str or a ``pgdriver.sql`` composition."""
    from agents.pgdriver import sql
    if isinstance(query, sql.Composed):
        return "".join(_text(part) for part in query.seq)
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return ".".join('"' + part + '"' for part in query.strings)
    return query


class _FakeCursor:
    def __init__(self, log, fail_tables=()):
        self._log = log
        self._fail = set(fail_tables)
        self._rows: list = []

    def execute(self, query, params=None):
        query = _text(query)
        if "information_schema.tables" in query:
            self._log.append("list-tables")
            self._rows = [("quality_checks",), ("work_orders",)]
        elif "information_schema.columns" in query:
            self._log.append("count-columns")
            self._rows = [(7,)]
        else:  # a per-table SELECT 1 sample read
            table = query.split('"')[3]
            self._log.append(f"sample:{table}")
            if table in self._fail:
                raise PermissionError("permission denied for relation")
            self._rows = [(_CANARY,)]  # canary row content — must never leak

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, log, fail_tables=()):
        self._log = log
        self._fail = fail_tables
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._log, self._fail)

    def close(self):
        self._log.append("close")


def _install_fake_pg(monkeypatch, log, fail_tables=()):
    from agents import pgdriver
    monkeypatch.setattr(pgdriver, "connect",
                        lambda conn_str, **kw: _FakeConn(log, fail_tables))
    monkeypatch.setattr("agents.connection_security.validate_connection_target",
                        lambda *a, **k: "")


# ── verdicts ────────────────────────────────────────────────────────────────

def test_assume_role_denied_blocks(monkeypatch):
    """A denied AssumeRole probe ⇒ blocked; ZERO sample reads; no ARN echo."""
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_DENIED_PROBE))
    reads: list = []
    monkeypatch.setattr(op, "_sample_reads",
                        lambda *a: reads.append(a)
                        or ([], dict(op._EMPTY_COMPLETENESS), []))

    result = op.run_preflight("erp")

    assert result["verdict"] == "blocked"
    assert result["connection"] == {
        "status": "fail", "reason": "assume_role_denied",
        "detail": _DENIED_PROBE["detail"], "latency_ms": 12}
    assert reads == [], "sample reads must NOT run when the probe fails"
    assert result["tables"] == []
    assert result["warnings"] and "assume_role_denied" in result["warnings"][0]
    blob = json.dumps(result)
    assert "arn:" not in blob.lower()
    assert "111122223333" not in blob


def test_sample_read_failure_degrades(monkeypatch):
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))

    def sampler(resolved, system_id, deadline):
        return ([{"name": "work_orders", "sample_read": "ok", "detail": ""},
                 {"name": "quality_checks", "sample_read": "failed",
                  "detail": "PermissionError"}],
                {"tables_visible": 2, "tables_sample_read": 1,
                 "columns_discovered": 7, "physical_columns": "n/a"})

    monkeypatch.setitem(op._SAMPLERS, "postgres", sampler)

    result = op.run_preflight("erp")

    assert result["verdict"] == "degraded"
    assert result["completeness"]["tables_sample_read"] == 1
    assert any("quality_checks" in w and "PermissionError" in w
               for w in result["warnings"])


def test_athena_physical_skip_degrades_with_warning(monkeypatch):
    resolved = {"system_id": "lake", "connection_type": "Amazon Athena",
                "workgroup": "wg", "database": "db"}
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(resolved))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))

    def sampler(res, system_id, deadline):
        return ([{"name": "events", "sample_read": "ok", "detail": ""}],
                {"tables_visible": 1, "tables_sample_read": 1,
                 "columns_discovered": 12,
                 "physical_columns": "skipped:AthenaConfigError"})

    monkeypatch.setitem(op._SAMPLERS, "athena", sampler)

    result = op.run_preflight("lake")

    assert result["verdict"] == "degraded"
    assert any("physical-column discovery would be skipped" in w
               and "skipped:AthenaConfigError" in w for w in result["warnings"])


def test_all_ok_ready(monkeypatch):
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))
    log: list = []
    _install_fake_pg(monkeypatch, log)

    result = op.run_preflight("erp")

    assert result["verdict"] == "ready"
    assert result["warnings"] == []
    assert [t["sample_read"] for t in result["tables"]] == ["ok", "ok"]
    assert result["completeness"] == {
        "tables_visible": 2, "tables_sample_read": 2,
        "columns_discovered": 7, "physical_columns": "n/a"}
    assert isinstance(result["elapsed_seconds"], float)


def test_verdict_derivation_is_pure_and_pinned():
    """The pinned pure derivation, exercised directly."""
    ok_conn = {"status": "pass"}
    assert op.preflight_verdict({"status": "fail"}, [], "n/a") == "blocked"
    assert op.preflight_verdict(
        ok_conn, [{"sample_read": "failed"}], "n/a") == "degraded"
    assert op.preflight_verdict(ok_conn, [], "skipped:Boom") == "degraded"
    assert op.preflight_verdict(
        ok_conn, [{"sample_read": "ok"}], "ok") == "ready"
    assert op.preflight_verdict(ok_conn, [], "n/a") == "ready"


# ── no values / serial / honest wholesale failure ───────────────────────────

def test_no_values_in_result(monkeypatch):
    """Fake rows carry a canary string — the json-dumped result must not."""
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))
    log: list = []
    _install_fake_pg(monkeypatch, log)

    result = op.run_preflight("erp")

    blob = json.dumps(result)
    assert _CANARY not in blob
    assert result["verdict"] == "ready"
    # Failure detail is the exception CLASS only — never message content.
    log2: list = []
    _install_fake_pg(monkeypatch, log2, fail_tables={"quality_checks"})
    failed = op.run_preflight("erp")
    entry = next(t for t in failed["tables"] if t["name"] == "quality_checks")
    assert entry == {"name": "quality_checks", "sample_read": "failed",
                     "detail": "PermissionError"}
    assert "permission denied for relation" not in json.dumps(failed)


def test_sample_reads_serial(monkeypatch):
    """Per-table reads run one at a time, in listing order, after the
    bounded catalog reads — never fanned out."""
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))
    log: list = []
    _install_fake_pg(monkeypatch, log)

    op.run_preflight("erp")

    assert log == ["list-tables", "count-columns",
                   "sample:quality_checks", "sample:work_orders", "close"]


def test_wholesale_listing_failure_degrades(monkeypatch):
    """A sampler crash (e.g. the table listing itself) is an honest
    degradation — one failed '(table listing)' read — never a silent ready."""
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))

    def sampler(resolved, system_id, deadline):
        raise ConnectionError("host db.example.com unreachable")

    monkeypatch.setitem(op._SAMPLERS, "postgres", sampler)

    result = op.run_preflight("erp")

    assert result["verdict"] == "degraded"
    assert result["tables"] == [{"name": "(table listing)",
                                 "sample_read": "failed",
                                 "detail": "ConnectionError"}]
    assert "db.example.com" not in json.dumps(result)  # class only, no hosts


def test_wall_clamp_raises_timeout(monkeypatch):
    """An exceeded deadline propagates PreflightTimeoutError (the worker
    stores the honest 'preflight timed out' error)."""
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(_PG_RESOLVED))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))
    log: list = []
    _install_fake_pg(monkeypatch, log)

    with pytest.raises(op.PreflightTimeoutError):
        op.run_preflight("erp", deadline=time.monotonic() - 1)


def test_unsupported_type_warns_and_stays_probe_only(monkeypatch):
    resolved = {"system_id": "x", "connection_type": "Carrier Pigeon"}
    monkeypatch.setattr(op, "_resolve", lambda sid: dict(resolved))
    monkeypatch.setattr(op, "_probe", lambda sid, r: dict(_OK_PROBE))

    result = op.run_preflight("x")

    assert result["verdict"] == "ready"  # probe passed; nothing to sample
    assert any("no sample-read support" in w for w in result["warnings"])
    assert result["tables"] == []


# ── first-onboard bootstrap (the wizard-blocking regression, 2026-07-31) ────
#
# Preflight runs BEFORE discover, but resolve_connection is node-only — a
# bundled seed system has no node config until its first provision, so without
# the bootstrap the diagnostic gate blocked EVERY first catalog onboard
# ("Preflight Blocked — connection failed", observed live via the e2e walk).

def test_bootstrap_fills_seed_pg_connection(monkeypatch):
    """Unregistered seed pg system + seed env ⇒ the demo DSN is bootstrapped,
    exactly like discover's first-onboard path (same routing home, ADR-0044)."""
    monkeypatch.setattr(op, "_catalog_entry",
                        lambda sid: {"schema": "cmms_maintenance",
                                     "connection_type": "PostgreSQL"})
    monkeypatch.setattr("agents.demo.db_routing.demo_pg_dsn",
                        lambda schema: f"postgresql://u@seed-host/db?options=-csearch_path%3D{schema}")
    out = op._bootstrap_unregistered({"system_id": "cmms", "connection_string": "",
                                      "_unconfigured": True}, "cmms")
    assert "seed-host" in out["connection_string"]
    assert out["schema"] == "cmms_maintenance"
    assert "_unconfigured" not in out


def test_bootstrap_never_overrides_a_resolved_connection(monkeypatch):
    """A registered system's node connection wins — the bootstrap is a no-op."""
    called: list = []
    monkeypatch.setattr(op, "_catalog_entry", lambda sid: called.append(sid) or {})
    resolved = {"system_id": "s", "connection_string": "postgresql://real"}
    assert op._bootstrap_unregistered(dict(resolved), "s") == resolved
    assert called == [], "the catalog must not even be consulted"


def test_bootstrap_noop_for_non_postgres_and_missing_seed_env(monkeypatch):
    """Seedless pg envs keep the honest blocked verdict; a typed connector with
    no resolvable connection carries its catalog type/config (so probe_connection
    doesn't misjudge it as PostgreSQL) but still adds NO connection_string, so it
    stays honestly blocked at the probe."""
    monkeypatch.setattr(op, "_catalog_entry",
                        lambda sid: {"schema": "x", "connection_type": "Athena"})
    before = {"connection_string": ""}
    out = op._bootstrap_unregistered(dict(before), "a")
    # Typed-connector pre-node bootstrap: the catalog connection_type is carried
    # (fixes the false "misjudged as PostgreSQL" block) but no connection_string
    # is fabricated, so the honest blocked verdict is preserved.
    assert out.get("connection_type") == "Athena"
    assert not out.get("connection_string")
    # PostgreSQL but no seed env: demo_pg_dsn raising must degrade, not raise.
    monkeypatch.setattr(op, "_catalog_entry",
                        lambda sid: {"schema": "x", "connection_type": "PostgreSQL"})
    def boom(schema):
        raise RuntimeError("no POSTGRES_HOST")
    monkeypatch.setattr("agents.demo.db_routing.demo_pg_dsn", boom)
    assert op._bootstrap_unregistered(dict(before), "p") == before


def test_run_preflight_probes_the_bootstrapped_connection(monkeypatch):
    """End-to-end through run_preflight: the probe must SEE the bootstrapped
    DSN (the defect was blocked-before-discover, so pin the wiring, not just
    the helper)."""
    monkeypatch.setattr(op, "_resolve",
                        lambda sid: {"system_id": sid, "connection_string": "",
                                     "_unconfigured": True})
    monkeypatch.setattr(op, "_catalog_entry",
                        lambda sid: {"schema": "cmms_maintenance",
                                     "connection_type": "PostgreSQL"})
    monkeypatch.setattr("agents.demo.db_routing.demo_pg_dsn",
                        lambda schema: "postgresql://u@seed-host/db")
    seen: dict = {}
    def probe(sid, resolved):
        seen.update(resolved)
        return dict(_OK_PROBE)
    monkeypatch.setattr(op, "_probe", probe)
    monkeypatch.setattr(op, "_sample_reads",
                        lambda *a: ([], dict(op._EMPTY_COMPLETENESS), []))
    result = op.run_preflight("cmms")
    assert seen.get("connection_string") == "postgresql://u@seed-host/db"
    assert result["connection"]["status"] == "pass"
