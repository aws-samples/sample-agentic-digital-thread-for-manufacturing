# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the E1 preflight gate in scripts/reonboard.py
(``--preflight``, Wave-3 Task 3.1 / ADR-0053 D-E1).

Fully offline (DI-seam stubs, no boto3/HTTP — the test_reonboard_lib style).
Pinned semantics:
  * default OFF — no preflight HTTP calls, results identical to before;
  * verdict ``blocked`` ⇒ that system is ABORTED BEFORE its delete phase
    (``_reonboard_one`` never runs for it) with result "preflight-blocked",
    and later systems still run;
  * ``degraded`` ⇒ warnings logged + proceed; ``ready`` ⇒ proceed;
  * an errored / deadline-expired / unavailable preflight is INCONCLUSIVE
    and proceeds (diagnostic gate, not authz — only an explicit blocked
    verdict stops a system);
  * exit status: ``_report_outcome`` returns 1 when any system was blocked.
"""
import itertools
import logging
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reonboard  # noqa: E402
from reonboard import (  # noqa: E402
    PREFLIGHT_ROUTE,
    _report_outcome,
    preflight_gate,
    reonboard_systems,
)


# --------------------------------------------------------------------------- #
# Fakes (DI seams — no boto3, no HTTP)
# --------------------------------------------------------------------------- #
class _FakeApp:
    """REST stub serving the preflight POST/GET job contract per system."""

    def __init__(self, systems, jobs=None):
        self._systems = list(systems)
        self._jobs = dict(jobs or {})  # {system_id: poll response dict}
        self.calls: list[tuple] = []

    def catalog(self):
        return {sid: {"connection_type": "PostgreSQL", "schema": "s"}
                for sid in self._systems}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == PREFLIGHT_ROUTE:
            return {"job_id": f"job-{(body or {}).get('system_id')}"}
        if method == "GET" and path.startswith(PREFLIGHT_ROUTE + "/job-"):
            sid = path.rsplit("/job-", 1)[-1]
            return dict(self._jobs.get(sid, {}))
        return {}

    def preflight_posts(self):
        return [b.get("system_id") for m, p, b in self.calls
                if m == "POST" and p == PREFLIGHT_ROUTE]


def _done(verdict, warnings=(), connection=None):
    return {"status": "done",
            "result": {"verdict": verdict, "warnings": list(warnings),
                       "connection": connection or {}}}


def _resolver(cm, sys_list, env):
    return {sid: {"connection_string": "postgresql://u:p@h/db",
                  "connection_type": "PostgreSQL", "schema": "s"}
            for sid in sys_list}


def _run(app, systems, monkeypatch, **kwargs):
    """reonboard_systems with _reonboard_one stubbed to a recorder."""
    ran: list[str] = []
    monkeypatch.setattr(
        reonboard, "_reonboard_one",
        lambda a, c, g, sid, ci, cm, pf, dl, log, wk: ran.append(sid))
    results = reonboard_systems(
        app, control=object(), gateway_id="gw-1", systems=systems,
        connection_resolver=_resolver, sweep_blocked=False,
        log=lambda *a, **k: None, sleep=lambda s: None, **kwargs)
    return results, ran


# --------------------------------------------------------------------------- #
# Default OFF — byte-stable existing contract
# --------------------------------------------------------------------------- #
def test_default_off_makes_no_preflight_calls(monkeypatch):
    app = _FakeApp(["sys-a", "sys-b"])
    results, ran = _run(app, ["sys-a", "sys-b"], monkeypatch)
    assert results == {"sys-a": "ready", "sys-b": "ready"}
    assert ran == ["sys-a", "sys-b"]
    assert app.preflight_posts() == []


# --------------------------------------------------------------------------- #
# Verdict semantics
# --------------------------------------------------------------------------- #
def test_blocked_aborts_that_system_before_delete(monkeypatch, caplog):
    """blocked ⇒ _reonboard_one NEVER runs for that system (nothing torn
    down); later systems still run; the honest verdict is logged."""
    app = _FakeApp(["sys-bad", "sys-ok"], jobs={
        "sys-bad": _done("blocked", connection={
            "status": "fail", "reason": "assume_role_denied",
            "detail": "role assumption denied"}),
        "sys-ok": _done("ready"),
    })
    with caplog.at_level(logging.ERROR, logger="reonboard"):
        results, ran = _run(app, ["sys-bad", "sys-ok"], monkeypatch,
                            preflight=True)
    assert results == {"sys-bad": "preflight-blocked", "sys-ok": "ready"}
    assert ran == ["sys-ok"]
    assert app.preflight_posts() == ["sys-bad", "sys-ok"]
    text = caplog.text
    assert "BLOCKED" in text and "assume_role_denied" in text
    assert "nothing was deleted" in text


def test_degraded_logs_warnings_and_proceeds(monkeypatch):
    app = _FakeApp(["sys-a"], jobs={"sys-a": _done(
        "degraded", warnings=["sample reads could not run (OSError)"])})
    logged: list[str] = []
    ran: list[str] = []
    monkeypatch.setattr(
        reonboard, "_reonboard_one",
        lambda a, c, g, sid, ci, cm, pf, dl, log, wk: ran.append(sid))
    results = reonboard_systems(
        app, control=object(), gateway_id="gw-1", systems=["sys-a"],
        connection_resolver=_resolver, sweep_blocked=False,
        log=logged.append, sleep=lambda s: None, preflight=True)
    assert results == {"sys-a": "ready"}
    assert ran == ["sys-a"]
    assert any("sample reads could not run" in line for line in logged)
    assert any("DEGRADED" in line for line in logged)


def test_ready_proceeds(monkeypatch):
    app = _FakeApp(["sys-a"], jobs={"sys-a": _done("ready")})
    results, ran = _run(app, ["sys-a"], monkeypatch, preflight=True)
    assert results == {"sys-a": "ready"}
    assert ran == ["sys-a"]


# --------------------------------------------------------------------------- #
# Inconclusive outcomes proceed (diagnostic gate, not authz)
# --------------------------------------------------------------------------- #
def test_error_job_is_inconclusive_and_proceeds(monkeypatch):
    app = _FakeApp(["sys-a"], jobs={"sys-a": {
        "status": "error", "error": "preflight timed out"}})
    results, ran = _run(app, ["sys-a"], monkeypatch, preflight=True)
    assert results == {"sys-a": "ready"}
    assert ran == ["sys-a"]


def test_route_unavailable_is_inconclusive_and_proceeds(monkeypatch):
    """An older env without the preflight route (POST raises) proceeds."""
    app = _FakeApp(["sys-a"])

    def boom(method, path, body=None):
        if path.startswith(PREFLIGHT_ROUTE):
            raise OSError("HTTP 404")
        return {}

    app.call = boom
    results, ran = _run(app, ["sys-a"], monkeypatch, preflight=True)
    assert results == {"sys-a": "ready"}
    assert ran == ["sys-a"]


def test_poll_deadline_folds_to_inconclusive_proceed():
    """A job stuck 'running' past the client deadline is inconclusive."""
    app = _FakeApp(["sys-a"], jobs={"sys-a": {"status": "running"}})
    logged: list[str] = []
    clock = itertools.count(start=0, step=120)  # 3 polls blow the 360 s cap
    proceed = preflight_gate(
        app, "sys-a", log=logged.append, sleep=lambda s: None,
        monotonic=lambda: next(clock))
    assert proceed is True
    assert any("inconclusive" in line for line in logged)


# --------------------------------------------------------------------------- #
# Exit status
# --------------------------------------------------------------------------- #
def test_report_outcome_exit_codes(caplog):
    assert _report_outcome({"a": "ready", "b": "ready"}) == 0
    with caplog.at_level(logging.ERROR, logger="reonboard"):
        assert _report_outcome({"a": "ready",
                                "b": "preflight-blocked"}) == 1
    assert "INCOMPLETE" in caplog.text and "b" in caplog.text


def test_failed_login_logs_only_the_status_never_the_reply(monkeypatch, caplog):
    import argparse

    class _LoginApp:
        def __init__(self, base_url):
            pass

        def call(self, method, path, body=None):
            return {"status": "denied", "detail": "echo pw-fixture", "session": "sess-fixture"}

    monkeypatch.setattr(reonboard, "App", _LoginApp)
    monkeypatch.setattr(reonboard, "_resolve_password", lambda *a: "pw-fixture")
    args = argparse.Namespace(base_url="https://x.example", systems="sys-a", all=False,
                              gateway_id="gw-1", password="", profile="", region="",
                              username="ADT")
    with caplog.at_level(logging.ERROR, logger="reonboard"):
        assert reonboard._prepare_session(args) is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "login failed" in text and "denied" in text
    assert "pw-fixture" not in text and "sess-fixture" not in text
